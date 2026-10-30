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


if __name__ == "__main__":
  unittest.main()
