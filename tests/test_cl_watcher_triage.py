#!/usr/bin/env python3
"""Unit tests for review triage store, overlay, and Flight Deck rendering."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import triage_store as ts  # pylint: disable=g-import-not-at-top


class TriageStoreParsingTest(unittest.TestCase):

  def setUp(self):
    self.test_dir = tempfile.mkdtemp(prefix="triage_test_")

  def tearDown(self):
    shutil.rmtree(self.test_dir, ignore_errors=True)

  def test_parse_queue_triage_markdown_gerrit_and_github(self):
    md = """
### Детали по 1409697
## Review Triage: Fix SkParagraph font scanner bounds
- **URL:** https://skia-review.googlesource.com/c/skia/+/1409697
- **Verdict:** [SURGICAL_AUDIT]
- **Why:** Modifies paragraph layout loop; verify zero-allocation contract.
- | **Patchset** | `PS3` | 15 files |

**Ready-to-post Draft (English):**
> LGTM pending corpus gate.

---

### Детали по flutter/flutter#140560
## Review Triage: Update web text painter caret
- **URL:** https://github.com/flutter/flutter/pull/140560
- **Verdict:** [MECHANICAL]
- **Why:** Documentation only; no code changes.
- | **Patchset** | `PS1` | 1 file |
"""
    items = ts.parse_queue_triage_markdown(md, source_path="/tmp/triage_test.md")
    self.assertEqual(len(items), 2)

    g_item = items[0]
    self.assertEqual(g_item["target_id"], "1409697")
    self.assertEqual(g_item["kind"], "gerrit")
    self.assertEqual(g_item["item_key"], "gerrit:1409697")
    self.assertEqual(g_item["verdict"], "SURGICAL_AUDIT")
    self.assertEqual(g_item["triaged_patchset"], 3)
    self.assertIn("Modifies paragraph layout", g_item["summary"])
    self.assertEqual(g_item["draft"], "LGTM pending corpus gate.")

    gh_item = items[1]
    self.assertEqual(gh_item["target_id"], "140560")
    self.assertEqual(gh_item["kind"], "github")
    self.assertEqual(gh_item["item_key"], "github:140560")
    self.assertEqual(gh_item["verdict"], "MECHANICAL")
    self.assertEqual(gh_item["triaged_patchset"], 1)
    self.assertEqual(gh_item["review_time_label"], "~1m")

  def test_parse_wf_audit_markdown_with_draft_code_block(self):
    md = """
# Deep Audit for CL 1409696
- **Category:** Surgical audit
- **Review time:** about 3 min
- **Patchset:** PS2

**Ready-to-copy draft:**
```markdown
Please check zero-allocation hot path in ParagraphImpl::layout.
```
"""
    items = ts.parse_wf_audit_markdown(md, source_path="/tmp/wf312_test.md")
    self.assertEqual(len(items), 1)
    it = items[0]
    self.assertEqual(it["target_id"], "1409696")
    self.assertEqual(it["kind"], "gerrit")
    self.assertEqual(it["verdict"], "SURGICAL_AUDIT")
    self.assertEqual(it["review_time_label"], "~3m")
    self.assertEqual(it["triaged_patchset"], 2)
    self.assertEqual(
        it["draft"], "Please check zero-allocation hot path in ParagraphImpl::layout."
    )

  def test_parse_wf_audit_infers_review_outcome_and_eyebrow(self):
    md = """
# Deep Audit for CL 1409697
- **Category:** Surgical audit
- **Review time:** about 2 min
- **Patchset:** PS5

**Ready-to-copy draft:**
```markdown
LGTM pending corpus gate.
```
"""
    items = ts.parse_wf_audit_markdown(md, source_path="/tmp/wf312_1409697.md")
    self.assertEqual(len(items), 1)
    it = items[0]
    self.assertEqual(it["review_outcome"], "CLEAN")
    self.assertEqual(it["review_eyebrow_label"], "🟢 Review PS5: Clean (LGTM)")

  def test_link_companion_changes_prevents_stack_chat_leak(self):
    cand_parent = {
        "item_key": "gerrit:1412378",
        "target_id": "1412378",
        "kind": "gerrit",
        "host": w.GERRIT_SKIA_HOST,
        "is_self": False,
        "status": "NEW",
        "subject": "Add HarfBuzz font backend",
        "conversation_id": "review-chat-1",
        "single_target": True,
    }
    sibling = {
        "item_key": "gerrit:1412379",
        "target_id": "1412379",
        "kind": "gerrit",
        "host": w.GERRIT_SKIA_HOST,
        "is_self": False,
        "status": "NEW",
        "subject": "Use HarfBuzz font backend in Paragraph",
        "conversation_id": "",
    }
    items = {cand_parent["item_key"]: cand_parent, sibling["item_key"]: sibling}
    from unittest import mock
    with mock.patch.object(
        w, "fetch_gerrit_related_cl_ids", return_value={"1412378", "1412379"}
    ):
      w.link_companion_changes_to_parent_chats([sibling], items)

    # Sibling must not inherit the review chat from a single_target external review
    self.assertEqual(sibling.get("conversation_id"), "")
    self.assertTrue(cand_parent.get("single_target"))

  def test_discover_existing_chat_prevents_external_stack_leak(self):
    """External siblings in a Gerrit stack must not borrow each other's 1:1 review chats."""
    sibling_rec = {
        "item_key": "gerrit:1412384",
        "target_id": "1412384",
        "is_self": False,
        "subject": "Derive the half letter spacing",
        "patchset": 2,
    }
    fake_chats = [{
        "item_key": "gerrit:1412378",
        "conversation_id": "review-chat-1412378",
        "chat_archived": False,
        "is_self": False,
        "single_target": True,
    }]
    from unittest import mock
    with mock.patch.object(w, "discover_active_cl_chats", return_value=fake_chats):
      with mock.patch.object(
          w, "fetch_gerrit_related_cl_ids", return_value=["1412378", "1412384"]
      ):
        cid, is_arch, comp_to = w._discover_existing_chat_for_item(
            "gerrit:1412384", sibling_rec
        )
    self.assertEqual(cid, "")
    self.assertFalse(is_arch)
    self.assertEqual(comp_to, "")

  def test_companion_attached_to_external_cl_does_not_leak_chat_to_stack_siblings(self):
    active_discovered = [
        {
            "item_key": "gerrit:1412380",
            "kind": "gerrit",
            "target_id": "1412380",
            "conversation_id": "chat-1412380",
            "title": "Review CL 1412380",
            "single_target": True,
            "chat_busy": True,
            "chat_archived": False,
        },
        {
            "item_key": "gerrit:1412417",
            "kind": "gerrit",
            "target_id": "1412417",
            "conversation_id": "",
            "title": "Companion tests",
            "single_target": True,
            "chat_busy": False,
            "chat_archived": False,
        },
        {
            "item_key": "gerrit:1412376",
            "kind": "gerrit",
            "target_id": "1412376",
            "conversation_id": "",
            "title": "Derive the half letter spacing",
            "single_target": True,
            "chat_busy": False,
            "chat_archived": False,
        },
    ]
    fetched_cache = {
        "gerrit:1412380": {
            "is_self": False,
            "status": "NEW",
            "subject": "External change in stack",
        },
        "gerrit:1412417": {
            "is_self": True,
            "status": "NEW",
            "subject": "WIP: companion tests",
            "commit_message": (
                "Companion tests for https://skia-review.googlesource.com/c/skia/+/1412380\n"
            ),
        },
        "gerrit:1412376": {
            "is_self": False,
            "status": "NEW",
            "subject": "Derive the half letter spacing",
        },
    }
    from unittest import mock
    with mock.patch.object(
        w,
        "fetch_gerrit_related_cl_ids",
        return_value=["1412380", "1412417", "1412376"],
    ):
      w.link_companion_changes_to_parent_chats(active_discovered, fetched_cache)

    parent_rec = next(d for d in active_discovered if d["item_key"] == "gerrit:1412380")
    comp_rec = next(d for d in active_discovered if d["item_key"] == "gerrit:1412417")
    sibling_rec = next(d for d in active_discovered if d["item_key"] == "gerrit:1412376")

    # Companion is linked to parent review chat
    self.assertEqual(comp_rec.get("companion_to"), "gerrit:1412380")
    self.assertTrue(comp_rec.get("is_companion"))
    self.assertEqual(comp_rec.get("conversation_id"), "chat-1412380")
    self.assertTrue(comp_rec.get("chat_busy"))

    # Parent retains single_target so stack siblings never inherit its chat
    self.assertTrue(parent_rec.get("single_target"))

    # Stack sibling remains unchatted and not busy
    self.assertEqual(sibling_rec.get("conversation_id"), "")
    self.assertFalse(sibling_rec.get("chat_busy"))

  def test_save_and_load_audit_preserves_audit_over_queue_triage(self):
    audit_data = {
        "target_id": "1409697",
        "kind": "gerrit",
        "verdict": "SURGICAL_AUDIT",
        "verdict_label": "SURGICAL AUDIT",
        "summary": "LGTM pending corpus gate; needs your re-vote",
        "review_time_label": "~2m",
        "triaged_patchset": 4,
        "source": "audit",
        "draft": "LGTM PS4.",
    }
    ts.save_triage_result(audit_data, results_dir=self.test_dir)

    queue_data = {
        "target_id": "1409697",
        "kind": "gerrit",
        "verdict": "MECHANICAL",
        "verdict_label": "MECHANICAL",
        "summary": "Automated triage summary",
        "review_time_label": "~1m",
        "triaged_patchset": 4,
        "source": "queue_triage",
        "draft": "",
    }
    ts.save_triage_result(queue_data, results_dir=self.test_dir)

    loaded = ts.load_all_triage_results(results_dir=self.test_dir)
    res = loaded["gerrit:1409697"]
    self.assertEqual(res["verdict"], "SURGICAL_AUDIT")
    self.assertEqual(res["summary"], "LGTM pending corpus gate; needs your re-vote")
    self.assertEqual(res["draft"], "LGTM PS4.")

  def test_overlay_triage_on_items_and_staleness_calculation(self):
    audit_data = {
        "target_id": "1409697",
        "kind": "gerrit",
        "verdict": "SURGICAL_AUDIT",
        "summary": "Needs re-vote",
        "triaged_patchset": 3,
        "triaged_at_et": "Oct 30, 10:00 ET",
        "review_time_label": "~2m",
        "source": "audit",
    }
    ts.save_triage_result(audit_data, results_dir=self.test_dir)

    items = [
        {"item_key": "gerrit:1409697", "target_id": "1409697", "patchset": 4},
        {"item_key": "gerrit:1409696", "target_id": "1409696", "patchset": 1},
    ]
    ts.overlay_triage_on_items(items, results_dir=self.test_dir, ingest_tmp=False)

    self.assertIn("triage", items[0])
    self.assertTrue(items[0]["triage"]["is_stale"])
    self.assertEqual(items[0]["triage"]["current_patchset"], 4)
    self.assertEqual(items[0]["triage"]["triaged_patchset"], 3)
    self.assertNotIn("triage", items[1])

  def test_normalize_review_outcome(self):
    self.assertEqual(ts.normalize_review_outcome("CLEAN"), "CLEAN")
    self.assertEqual(ts.normalize_review_outcome("lgtm"), "CLEAN")
    self.assertEqual(ts.normalize_review_outcome("Case 1"), "CLEAN")
    self.assertEqual(ts.normalize_review_outcome("nits"), "NITS")
    self.assertEqual(ts.normalize_review_outcome("case_2"), "NITS")
    self.assertEqual(ts.normalize_review_outcome("companion_tests"), "COMPANION_TESTS")
    self.assertEqual(ts.normalize_review_outcome("case 3"), "COMPANION_TESTS")
    self.assertEqual(ts.normalize_review_outcome("pushback"), "PUSHBACK")
    self.assertEqual(ts.normalize_review_outcome("case 4"), "PUSHBACK")
    self.assertEqual(ts.normalize_review_outcome("companion_fix"), "COMPANION_FIX")
    self.assertEqual(ts.normalize_review_outcome("case 5"), "COMPANION_FIX")
    self.assertEqual(ts.normalize_review_outcome("reassign"), "REASSIGN")
    with self.assertRaises(ValueError):
      ts.normalize_review_outcome("invalid_outcome")

  def test_save_review_verdict_and_source_priority(self):
    p = ts.save_review_verdict(
        target_id="1411905",
        kind="gerrit",
        patchset=2,
        outcome="COMPANION_FIX",
        summary="Caret offset miscomputed in BiDi runs",
        draft="LGTM with companion CL 1411906.",
        results_dir=self.test_dir,
    )
    self.assertTrue(p.exists())
    all_res = ts.load_all_triage_results(results_dir=self.test_dir)
    item = all_res["gerrit:1411905"]
    self.assertEqual(item["review_outcome"], "COMPANION_FIX")
    self.assertEqual(item["review_eyebrow_label"], "🟠 Review PS2: Defects + companion fix")
    self.assertEqual(item["source"], "review")
    self.assertEqual(item["summary"], "Caret offset miscomputed in BiDi runs")

    # Queue triage on the same patchset must not overwrite the deep review outcome
    queue_data = {
        "target_id": "1411905",
        "kind": "gerrit",
        "verdict": "SURGICAL_AUDIT",
        "summary": "Old queue triage summary",
        "triaged_patchset": 2,
        "source": "queue_triage",
    }
    ts.save_triage_result(queue_data, results_dir=self.test_dir)
    reloaded = ts.load_all_triage_results(results_dir=self.test_dir)["gerrit:1411905"]
    self.assertEqual(reloaded["review_outcome"], "COMPANION_FIX")
    self.assertEqual(reloaded["source"], "review")
    self.assertEqual(reloaded["summary"], "Caret offset miscomputed in BiDi runs")

  def test_overlay_triage_sets_reviewed_ps_on_item(self):
    ts.save_review_verdict(
        target_id="1411906",
        kind="gerrit",
        patchset=3,
        outcome="CLEAN",
        summary="All tests pass clean",
        results_dir=self.test_dir,
    )
    items = [{"item_key": "gerrit:1411906", "target_id": "1411906", "patchset": 3}]
    ts.overlay_triage_on_items(items, results_dir=self.test_dir, ingest_tmp=False)
    self.assertEqual(items[0]["reviewed_ps"], 3)
    self.assertIn("triage", items[0])
    self.assertEqual(items[0]["triage"]["review_outcome"], "CLEAN")


class FlightDeckTriageDashboardRenderingTest(unittest.TestCase):

  def test_dashboard_triage_html_and_card_rendering(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const itemWithTriage = {
  item_key: 'gerrit:1409697',
  target_id: '1409697',
  kind: 'gerrit',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  patchset: 4,
  url: 'https://example.com/1409697',
  subject: '[skparagraph] Fix caret bounds',
  project_name: 'SkParagraph',
  triage: {
    verdict: 'SURGICAL_AUDIT',
    verdict_label: 'SURGICAL AUDIT',
    summary: 'LGTM pending corpus gate; needs your re-vote',
    review_time_label: '~2m',
    triaged_patchset: 3,
    triaged_at_et: 'Oct 30, 10:00 ET',
    is_stale: true,
    draft: 'Please run corpus test.',
    report_markdown: '### Audit Report\\nAll tests passed.',
  }
};

const itemMerged = Object.assign({}, itemWithTriage, {status: 'MERGED'});

console.log(JSON.stringify({
  focusHtml: focusCard(itemWithTriage, {jetski_box_url: ''}),
  rowHtmlStr: rowHtml(itemWithTriage, {jetski_box_url: ''}),
  triageCompact: triageHtml(itemWithTriage, true),
  triageFull: triageHtml(itemWithTriage, false),
  mergedTriage: triageHtml(itemMerged, false),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())

    compact = out["triageCompact"]
    self.assertIn("triage-surgical_audit", compact)
    self.assertIn("SURGICAL AUDIT", compact)
    self.assertIn("~2m", compact)
    self.assertIn("Stale: PS3 → PS4", compact)
    self.assertIn("LGTM pending corpus gate", compact)
    self.assertIn("Oct 30, 10:00 ET", compact)
    self.assertIn("Draft", compact)
    self.assertIn("Copy", compact)
    self.assertIn("Report", compact)

    self.assertIn("triage-box", out["focusHtml"])
    self.assertIn("triage-box", out["rowHtmlStr"])
    self.assertEqual(out["mergedTriage"], "")

  def test_dashboard_review_verdict_eyebrow_and_pill_rendering(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const baseItem = {
  item_key: 'gerrit:1411905',
  target_id: '1411905',
  kind: 'gerrit',
  host: 'skia-review.googlesource.com',
  role: 'reviewer (goog)',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  patchset: 2,
  reviewed_ps: 2,
  url: 'https://example.com/1411905',
  subject: '[skparagraph] Fix bidi shaping',
  project_name: 'Skia',
  owner_name: 'KC Yeung',
  owner_email: 'I@YeungKC.com',
  is_external: true,
  triage: {
    verdict: 'COMPANION_FIX',
    verdict_label: '🟠 Defects + companion fix',
    review_outcome: 'COMPANION_FIX',
    review_outcome_label: '🟠 Defects + companion fix',
    review_eyebrow_label: '🟠 Review PS2: Defects + companion fix',
    summary: 'Caret offset miscomputed in BiDi runs',
    triaged_patchset: 2,
    triaged_at_et: 'Oct 31, 18:30 ET',
    is_stale: false,
    draft: 'Draft comment with companion CL 1411906.',
    report_markdown: '### Report\\nFound 2 defects.',
  }
};

const cleanItem = Object.assign({}, baseItem, {
  triage: Object.assign({}, baseItem.triage, {
    verdict: 'CLEAN',
    verdict_label: '🟢 Clean (LGTM)',
    review_outcome: 'CLEAN',
    review_eyebrow_label: '🟢 Review PS2: Clean (LGTM)',
    summary: 'All invariants verified',
  })
});

const staleItem = Object.assign({}, cleanItem, {
  patchset: 3,
  triage: Object.assign({}, cleanItem.triage, {
    is_stale: true,
  })
});

const companionFallbackItem = {
  item_key: 'gerrit:1411907',
  target_id: '1411907',
  kind: 'gerrit',
  role: 'reviewer',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  patchset: 2,
  url: 'https://example.com/1411907',
  subject: 'Feature branch',
  project_name: 'Skia',
  companions: [
    {item_key: 'gerrit:1411908', label: 'CL 1411908', status: 'NEW', companion_base_ps: 2}
  ]
};

const nonReviewerItem = Object.assign({}, baseItem, {
  role: 'author',
  is_self: true,
});

console.log(JSON.stringify({
  eyebrowCompanionFix: reviewVerdictEyebrowHtml(baseItem),
  eyebrowClean: reviewVerdictEyebrowHtml(cleanItem),
  eyebrowStale: reviewVerdictEyebrowHtml(staleItem),
  eyebrowFallback: reviewVerdictEyebrowHtml(companionFallbackItem),
  eyebrowNonReviewer: reviewVerdictEyebrowHtml(nonReviewerItem),
  focusClean: focusCard(cleanItem, {jetski_box_url: ''}),
  focusCompanionFix: focusCard(baseItem, {jetski_box_url: ''}),
  focusSelf: focusCard(nonReviewerItem, {jetski_box_url: ''}),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())

    self.assertIn("v-companion_fix", out["eyebrowCompanionFix"])
    self.assertIn("🟠 Review PS2: Defects + companion fix", out["eyebrowCompanionFix"])

    self.assertIn("v-clean", out["eyebrowClean"])
    self.assertIn("🟢 Review PS2: Clean (LGTM)", out["eyebrowClean"])

    self.assertIn("v-stale", out["eyebrowStale"])
    self.assertIn("→ PS3 uploaded", out["eyebrowStale"])

    self.assertIn("v-companion_fix", out["eyebrowFallback"])
    self.assertIn("Companion CL 1411908", out["eyebrowFallback"])

    self.assertEqual(out["eyebrowNonReviewer"], "")

    self.assertIn("v-clean", out["focusClean"])
    self.assertIn("triage-clean", out["focusClean"])
    self.assertIn("All invariants verified", out["focusClean"])
    self.assertIn("by <a", out["focusClean"])
    self.assertIn("KC Yeung (external)", out["focusClean"])

    self.assertIn("v-companion_fix", out["focusCompanionFix"])
    self.assertIn("triage-companion_fix", out["focusCompanionFix"])
    self.assertIn("Caret offset miscomputed in BiDi runs", out["focusCompanionFix"])
    self.assertIn("by <a", out["focusCompanionFix"])

    self.assertIn("by You", out["focusSelf"])


if __name__ == "__main__":
  unittest.main()
