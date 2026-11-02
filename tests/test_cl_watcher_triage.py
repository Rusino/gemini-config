#!/usr/bin/env python3
"""Unit tests for review triage store, overlay, and Flight Deck rendering."""

from __future__ import annotations

import contextlib
import io
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
    md_pending = """
# Deep Audit for CL 1409697
- **Category:** Surgical audit
- **Review time:** about 2 min
- **Patchset:** PS5

**Ready-to-copy draft:**
```markdown
LGTM pending corpus gate.
```
"""
    items = ts.parse_wf_audit_markdown(md_pending, source_path="/tmp/wf312_1409697.md")
    self.assertEqual(len(items), 1)
    it = items[0]
    self.assertEqual(it["review_outcome"], "CLEAN")
    self.assertTrue(it["corpus_required"])
    self.assertEqual(it["corpus_status"], "pending")
    self.assertEqual(it["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")

    md_clean = """
# Deep Audit for CL 1409698
- **Category:** Mechanical
- **Review time:** about 1 min
- **Patchset:** PS2

**Ready-to-copy draft:**
```markdown
LGTM. Verified mechanical churn; no unintended side effects.
```
"""
    clean_items = ts.parse_wf_audit_markdown(md_clean, source_path="/tmp/wf312_1409698.md")
    self.assertEqual(len(clean_items), 1)
    self.assertEqual(clean_items[0]["review_outcome"], "CLEAN")
    self.assertEqual(clean_items[0]["review_eyebrow_label"], "🟢 Review PS2: Clean (LGTM)")

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

  def test_save_review_verdict_truncates_on_word_boundary(self):
    ts.save_review_verdict(
        target_id="1414016",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Short summary",
        results_dir=self.test_dir,
    )
    res1 = ts.load_all_triage_results(results_dir=self.test_dir)["gerrit:1414016"]
    self.assertEqual(res1["summary"], "Short summary")

    long_sum = (
        "Correct fix: hit width now uses the glyph's own justification shift; "
        "author tests fail-before/pass-after, 4 extra adversarial cases pass, "
        "dm tests pass, flutter corpus clean and all tests green"
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary=long_sum,
        results_dir=self.test_dir,
    )
    res2 = ts.load_all_triage_results(results_dir=self.test_dir)["gerrit:1414017"]
    self.assertTrue(len(res2["summary"]) <= 180)
    self.assertTrue(res2["summary"].endswith("…"))
    self.assertNotIn("all tests green", res2["summary"])
    self.assertIn("4 extra adversarial cases pass", res2["summary"])

  def _write_corpus_fixture(
      self,
      sub_name: str,
      kind: str = "flutter",
      integrity: str = "VALID (все пакеты тестов завершены штатно)",
      regressions: int = 0,
      cand_sha: str = "7bca43577fd2",
      cand_ref: str = "ext:7bca43577f",
      dirty_files: list[str] | None = None,
      timestamp_utc: str = "2026-11-02T21:49:33Z",
  ) -> str:
    out_dir = os.path.join(self.test_dir, sub_name)
    base_dir = os.path.join(out_dir, "baseline")
    cand_dir = os.path.join(out_dir, "candidate")
    os.makedirs(base_dir, exist_ok=True)
    os.makedirs(cand_dir, exist_ok=True)
    with open(os.path.join(base_dir, "attestation.json"), "w", encoding="utf-8") as f:
      json.dump(
          {"label": "baseline", "ref": "HEAD", "git_sha": "ab9b798555a6", "dirty_files": []},
          f,
      )
    with open(os.path.join(cand_dir, "attestation.json"), "w", encoding="utf-8") as f:
      json.dump(
          {
              "label": "candidate",
              "ref": cand_ref,
              "git_sha": cand_sha,
              "dirty_files": list(dirty_files or []),
              "timestamp_utc": timestamp_utc,
          },
          f,
      )
    reg_col = f"**{regressions}**" if regressions > 0 else "0"
    if kind == "webparagraph":
      header = "# WebParagraph Corpus Report (baseline = `baseline`)"
      table = (
          "| Ref | `dart analyze` | Part 1a | Part 1b | Part 2 | Regressions | Fixed |\n"
          "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |\n"
          "| `baseline` | PASS | `+209 -1` | `+26 -6` | `+76 -37` | *(baseline)* | *(baseline)* |\n"
          f"| `candidate` | PASS | `+209 -1` | `+26 -6` | `+76 -37` | {reg_col} | 0 |"
      )
    elif kind == "skia_dm":
      header = "# Skia SkParagraph Native Tests (baseline = `baseline`)"
      table = (
          "| Ref | Tests | Regressions | Fixed |\n"
          "| :--- | :---: | :---: | :---: |\n"
          "| `baseline` | `+320 -0` | *(baseline)* | *(baseline)* |\n"
          f"| `candidate` | `+320 -0` | {reg_col} | 0 |"
      )
    else:
      header = "# Flutter Paragraph Engine Test Comparison (baseline = `baseline`)"
      table = (
          "| Ref | C++ Smoke Tests | Full Framework Corpus (Sharded) | Regressions | Fixed |\n"
          "| :--- | :---: | :---: | :---: | :---: |\n"
          "| `baseline` | `+99 -0` | `+20098 -1` | *(baseline)* | *(baseline)* |\n"
          f"| `candidate` | `+99 -0` | `+20098 -1` | {reg_col} | 0 |"
      )
    compare_md = os.path.join(out_dir, "compare.md")
    with open(compare_md, "w", encoding="utf-8") as f:
      f.write(
          f"{header}\n\n{table}\n\n"
          "## Паспорт прогона (Run Attestation)\n\n"
          f"**Вердикт целостности (Integrity Check):** `{integrity}`\n\n"
          "## `candidate` vs `baseline`\n\n"
          f"**Выполнено тестов: 20198 | Было фейлов: 1 | Починено: 0 | Поломано: {regressions}**\n"
      )
    return compare_md

  def test_save_review_corpus_gate_states_and_cli_attestation_enforcement(self):
    # Explicit required and pending states block green LGTM on CLEAN and NITS
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Hit width uses glyph's own justification shift",
        corpus="required",
        results_dir=self.test_dir,
    )
    res_req = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertTrue(res_req["corpus_required"])
    self.assertEqual(res_req["corpus_status"], "pending")
    self.assertEqual(res_req["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")
    self.assertEqual(res_req["verdict_tone"], "corpus_blocked")

    ts.save_review_verdict(
        target_id="1414018",
        kind="gerrit",
        patchset=2,
        outcome="NITS",
        summary="Minor comment nit",
        corpus="pending",
        results_dir=self.test_dir,
    )
    res_pend = ts.load_triage_result("gerrit", "1414018", results_dir=self.test_dir)
    self.assertEqual(res_pend["corpus_status"], "pending")
    self.assertEqual(res_pend["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")

    # Failed corpus blocks LGTM with failed verdict
    ts.save_review_verdict(
        target_id="1414019",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Looks clean statically",
        corpus="failed",
        results_dir=self.test_dir,
    )
    res_fail = ts.load_triage_result("gerrit", "1414019", results_dir=self.test_dir)
    self.assertEqual(res_fail["corpus_status"], "failed")
    self.assertEqual(res_fail["review_eyebrow_label"], "🔴 LGTM blocked: Flutter corpus failed")
    self.assertEqual(res_fail["verdict_tone"], "corpus_failed")

    # Waived corpus records waiver without turning green
    ts.save_review_verdict(
        target_id="1414020",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Text-only quick check requested by user",
        corpus="waived",
        results_dir=self.test_dir,
    )
    res_waive = ts.load_triage_result("gerrit", "1414020", results_dir=self.test_dir)
    self.assertEqual(res_waive["corpus_status"], "waived")
    self.assertIn("waived", res_waive["review_eyebrow_label"].lower())
    self.assertNotIn("🟢", res_waive["review_eyebrow_label"])
    self.assertEqual(res_waive["verdict_tone"], "corpus_blocked")

    # Unattested passed request via verify_attestation=True downgrades to pending
    empty_search = os.path.join(self.test_dir, "empty_tmp")
    os.makedirs(empty_search, exist_ok=True)
    err_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf):
      ts.save_review_verdict(
          target_id="1414021",
          kind="gerrit",
          patchset=1,
          outcome="CLEAN",
          summary="Claimed passed without running corpus",
          corpus="passed",
          verify_attestation=True,
          compare_search_dirs=[empty_search],
          results_dir=self.test_dir,
      )
    self.assertIn("recording corpus_status=pending", err_buf.getvalue())
    res_unatt = ts.load_triage_result("gerrit", "1414021", results_dir=self.test_dir)
    self.assertEqual(res_unatt["corpus_status"], "pending")
    self.assertEqual(res_unatt["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")

    # Valid compare.md Run Attestation unlocks green Clean (LGTM)
    valid_md = self._write_corpus_fixture(
        "flutter_1414021", cand_sha="7bca43577fd2", regressions=0
    )
    ts.save_review_verdict(
        target_id="1414021",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="All Flutter paragraph tests green",
        corpus="passed",
        revision="7bca43577fd2b0f9b3cd00a30dfc60239fd4022f",
        compare_md=valid_md,
        verify_attestation=True,
        results_dir=self.test_dir,
    )
    res_ok = ts.load_triage_result("gerrit", "1414021", results_dir=self.test_dir)
    self.assertEqual(res_ok["corpus_status"], "passed")
    self.assertEqual(res_ok["corpus_tested_patchset"], 1)
    self.assertEqual(res_ok["review_eyebrow_label"], "🟢 Review PS1: Clean (LGTM)")
    self.assertEqual(res_ok["verdict_tone"], "clean")

  def test_auto_derive_corpus_from_triage_cl_and_compare_md_attestation(self):
    # Incident Gerrit 1414017 PS1: triage_cl flags Flutter Corpus Gate REQUIRED
    triage_json = {
        "metadata": {
            "id": "1414017",
            "subject": "[skparagraph] Shift the trailing space of an RTL run when justifying",
            "patchset": 1,
            "thread_context": {
                "current_ps": 1,
                "current_sha": "7bca43577fd2b0f9b3cd00a30dfc60239fd4022f",
            },
        },
        "analysis": {
            "category": "SURGICAL_AUDIT",
            "reason": "Core domain logic modification",
            "corpus_gate": {
                "required": True,
                "domain": "SkParagraph/SkUnicode/SkShaper",
                "script": "run_flutter_paragraph_tests.py",
                "files": ["modules/skparagraph/src/TextLine.cpp"],
            },
        },
    }
    triage_md = (
        "## Review Triage: [skparagraph] Shift the trailing space\n"
        "### 📊 Churn & Profile\n"
        "| **Flutter Corpus Gate** | 🚨 **REQUIRED (`run_flutter_paragraph_tests.py`)** | "
        "Touches `SkParagraph/SkUnicode/SkShaper` (`TextLine.cpp`) |\n"
    )
    ts.save_from_triage_cl_output(
        "https://skia-review.googlesource.com/c/skia/+/1414017",
        triage_json,
        triage_md,
        results_dir=self.test_dir,
    )

    search_root = os.path.join(self.test_dir, "corpus_runs")
    os.makedirs(search_root, exist_ok=True)

    # Saving CLEAN while corpus has not run derives pending and blocks green LGTM
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Static audit + Skia dm pass; Flutter checkout leased by another chat",
        compare_search_dirs=[search_root],
        results_dir=self.test_dir,
    )
    r1 = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertTrue(r1["corpus_required"])
    self.assertEqual(r1["corpus_status"], "pending")
    self.assertEqual(r1["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")

    # Native Skia dm compare.md must never satisfy the Flutter corpus gate
    dm_md = self._write_corpus_fixture(
        "corpus_runs/skia_dm_1414017", kind="skia_dm", cand_sha="7bca43577fd2"
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Only dm ran",
        compare_md=dm_md,
        results_dir=self.test_dir,
    )
    r_dm = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertNotEqual(r_dm["corpus_status"], "passed")

    # WARNING / INVALID Integrity Check marks corpus failed
    inv_md = self._write_corpus_fixture(
        "corpus_runs/invalid_1414017",
        integrity="WARNING / INVALID (1 issue(s))",
        cand_sha="7bca43577fd2",
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Incomplete shards",
        compare_md=inv_md,
        results_dir=self.test_dir,
    )
    r_inv = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertEqual(r_inv["corpus_status"], "failed")
    self.assertEqual(r_inv["review_eyebrow_label"], "🔴 LGTM blocked: Flutter corpus failed")

    # Regressions > 0 marks corpus failed even when Integrity Check is VALID
    reg_md = self._write_corpus_fixture(
        "corpus_runs/reg_1414017", regressions=2, cand_sha="7bca43577fd2"
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Has regressions",
        compare_md=reg_md,
        results_dir=self.test_dir,
    )
    r_reg = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertEqual(r_reg["corpus_status"], "failed")

    # Valid compare.md in search_dirs matching PS1's commit SHA derives passed automatically
    self._write_corpus_fixture(
        "corpus_runs/flutter_paragraph_tests_1414017",
        regressions=0,
        cand_sha="7bca43577fd2",
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Flutter corpus 0 regressions",
        compare_search_dirs=[search_root],
        results_dir=self.test_dir,
    )
    r_pass = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertEqual(r_pass["corpus_status"], "passed")
    self.assertEqual(r_pass["review_eyebrow_label"], "🟢 Review PS1: Clean (LGTM)")

  def test_new_patchset_resets_corpus_gate_and_rejects_stale_ps1_attestation(self):
    search_root = os.path.join(self.test_dir, "ps_reset_runs")
    os.makedirs(search_root, exist_ok=True)
    ps1_md = self._write_corpus_fixture(
        "ps_reset_runs/flutter_1414017",
        cand_sha="7bca43577fd2",
        timestamp_utc="2026-11-02T21:49:33Z",
    )
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="PS1 verified in Flutter",
        revision="7bca43577fd2b0f9b3cd00a30dfc60239fd4022f",
        compare_md=ps1_md,
        results_dir=self.test_dir,
    )

    # Uploading PS2 resets overlaid corpus_status from passed to pending
    items = [{
        "item_key": "gerrit:1414017",
        "target_id": "1414017",
        "kind": "gerrit",
        "patchset": 2,
        "revision": "9999888877776666555544443333222211110000",
        "files": ["modules/skparagraph/src/TextLine.cpp"],
    }]
    ts.overlay_triage_on_items(items, results_dir=self.test_dir, ingest_tmp=False)
    self.assertTrue(items[0]["triage"]["is_stale"])
    self.assertEqual(items[0]["triage"]["corpus_status"], "pending")

    # Minimal PS2 triage payload without verdict_label must not retain PS1's green LGTM label
    ts.save_triage_result(
        {
            "target_id": "1414017",
            "kind": "gerrit",
            "verdict": "SURGICAL_AUDIT",
            "summary": "Minimal queue update on PS2",
            "triaged_patchset": 2,
            "source": "queue_triage",
        },
        results_dir=self.test_dir,
    )
    min_q = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertNotIn("Clean", min_q.get("verdict_label", ""))
    self.assertEqual(min_q.get("verdict_label"), "SURGICAL AUDIT")

    # Queue triage running on PS2 must not inherit PS1's CLEAN outcome or passed corpus
    ps2_triage_json = {
        "metadata": {
            "id": "1414017",
            "subject": "[skparagraph] Shift trailing space",
            "patchset": 2,
            "thread_context": {
                "current_ps": 2,
                "current_sha": "9999888877776666555544443333222211110000",
            },
        },
        "analysis": {
            "category": "SURGICAL_AUDIT",
            "reason": "Updated TextLine.cpp",
            "corpus_gate": {
                "required": True,
                "domain": "SkParagraph/SkUnicode/SkShaper",
                "script": "run_flutter_paragraph_tests.py",
                "files": ["modules/skparagraph/src/TextLine.cpp"],
            },
        },
    }
    ts.save_from_triage_cl_output(
        "https://skia-review.googlesource.com/c/skia/+/1414017",
        ps2_triage_json,
        "## Review Triage\n",
        results_dir=self.test_dir,
    )
    after_q = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertEqual(after_q["triaged_patchset"], 2)
    self.assertEqual(after_q.get("review_outcome", ""), "")
    self.assertEqual(after_q["corpus_status"], "pending")

    # Saving CLEAN on PS2 while search_root only has PS1's compare.md keeps gate pending
    ts.save_review_verdict(
        target_id="1414017",
        kind="gerrit",
        patchset=2,
        outcome="CLEAN",
        summary="Checked PS2 diff statically",
        revision="9999888877776666555544443333222211110000",
        compare_search_dirs=[search_root],
        results_dir=self.test_dir,
    )
    ps2_rev = ts.load_triage_result("gerrit", "1414017", results_dir=self.test_dir)
    self.assertEqual(ps2_rev["corpus_status"], "pending")
    self.assertEqual(ps2_rev["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")

  def test_webparagraph_corpus_gate_on_github_pr_and_commit_push_reset(self):
    wp_files = ["engine/src/flutter/lib/web_ui/lib/src/engine/web_paragraph/paragraph.dart"]
    # Saving NITS without webparagraph-corpus blocks approval with WebParagraph pill
    ts.save_review_verdict(
        target_id="195600",
        kind="github",
        patchset=1,
        outcome="NITS",
        summary="Minor naming nit in WebParagraph",
        revision="aaaa111122223333444455556666777788889999",
        files=wp_files,
        results_dir=self.test_dir,
    )
    wp_pend = ts.load_triage_result("github", "195600", results_dir=self.test_dir)
    self.assertTrue(wp_pend["corpus_required"])
    self.assertEqual(wp_pend["corpus_script"], "run_webparagraph_corpus.py")
    self.assertEqual(wp_pend["corpus_status"], "pending")
    self.assertEqual(
        wp_pend["review_eyebrow_label"], "⏳ LGTM blocked: WebParagraph corpus not run"
    )

    # Valid WebParagraph compare.md on exact PR head SHA unlocks Minor nits badge
    wp_md = self._write_corpus_fixture(
        "wp_195600",
        kind="webparagraph",
        integrity="VALID (все сьюты завершены, WebParagraph активен)",
        regressions=0,
        cand_sha="aaaa11112222",
        cand_ref="aaaa11112222",
    )
    ts.save_review_verdict(
        target_id="195600",
        kind="github",
        patchset=1,
        outcome="NITS",
        summary="Minor naming nit; webparagraph corpus green",
        revision="aaaa111122223333444455556666777788889999",
        files=wp_files,
        compare_md=wp_md,
        results_dir=self.test_dir,
    )
    wp_ok = ts.load_triage_result("github", "195600", results_dir=self.test_dir)
    self.assertEqual(wp_ok["corpus_status"], "passed")
    self.assertEqual(wp_ok["review_eyebrow_label"], "🔵 Review PS1: Minor nits")

    # Author pushes a new commit on GitHub (patchset stays 1 when len(reviews)==0, revision changes)
    pr_items = [{
        "item_key": "github:195600",
        "target_id": "195600",
        "kind": "github",
        "patchset": 1,
        "revision": "bbbb44445555",
        "head_sha": "bbbb444455556666777788889999000011112222",
        "files": wp_files,
    }]
    ts.overlay_triage_on_items(pr_items, results_dir=self.test_dir, ingest_tmp=False)
    self.assertTrue(pr_items[0]["triage"]["is_stale"])
    self.assertEqual(pr_items[0]["triage"]["corpus_status"], "pending")

    # Re-running triage_cl on the new PR commit (patchset still 1) resets review and revision
    new_commit_triage = {
        "metadata": {
            "id": "195600",
            "subject": "Fix WebParagraph line height",
            "patchset": 1,
            "head_sha": "bbbb444455556666777788889999000011112222",
        },
        "analysis": {
            "category": "SURGICAL_AUDIT",
            "reason": "Updated paragraph.dart",
            "corpus_gate": {
                "required": True,
                "domain": "WebParagraph (web_ui)",
                "script": "run_webparagraph_corpus.py",
                "files": wp_files,
            },
        },
    }
    ts.save_from_triage_cl_output(
        "https://github.com/flutter/flutter/pull/195600",
        new_commit_triage,
        "## Review Triage\n",
        results_dir=self.test_dir,
    )
    after_push_q = ts.load_triage_result("github", "195600", results_dir=self.test_dir)
    self.assertEqual(
        after_push_q["triaged_revision"], "bbbb444455556666777788889999000011112222"
    )
    self.assertEqual(after_push_q.get("review_outcome", ""), "")
    self.assertEqual(after_push_q["corpus_status"], "pending")

  def test_worktree_dirty_corpus_attestation_matches_ps1_and_rejects_ps2(self):
    wt_root = os.path.join(self.test_dir, "wt_runs")
    os.makedirs(wt_root, exist_ok=True)
    # Flutter engine tests apply Skia CL patches onto deps_skia_revision as a dirty WORKTREE
    self._write_corpus_fixture(
        "wt_runs/flutter_paragraph_tests_1414016",
        cand_sha="ab9b798555a6",
        cand_ref="WORKTREE",
        dirty_files=["modules/skparagraph/src/TextLine.cpp"],
    )
    ts.save_review_verdict(
        target_id="1414016",
        kind="gerrit",
        patchset=1,
        outcome="CLEAN",
        summary="Hit width uses glyph's own justification shift; Flutter corpus clean",
        revision="8675bb8ba911223344556677889900aabbccddee",
        files=["modules/skparagraph/src/TextLine.cpp"],
        compare_search_dirs=[wt_root],
        results_dir=self.test_dir,
    )
    ps1 = ts.load_triage_result("gerrit", "1414016", results_dir=self.test_dir)
    self.assertEqual(ps1["corpus_status"], "passed")
    self.assertEqual(ps1["review_eyebrow_label"], "🟢 Review PS1: Clean (LGTM)")

    # Untimestamped PS1 WORKTREE dir must not satisfy PS2 without a ps2 tag
    ts.save_review_verdict(
        target_id="1414016",
        kind="gerrit",
        patchset=2,
        outcome="CLEAN",
        summary="Checked PS2 statically",
        revision="ccddeeff00112233445566778899aabbccddeeff",
        files=["modules/skparagraph/src/TextLine.cpp"],
        compare_search_dirs=[wt_root],
        results_dir=self.test_dir,
    )
    ps2 = ts.load_triage_result("gerrit", "1414016", results_dir=self.test_dir)
    self.assertEqual(ps2["corpus_status"], "pending")
    self.assertEqual(ps2["review_eyebrow_label"], "⏳ LGTM blocked: Flutter corpus not run")


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
        ["node", "-"], input=harness, capture_output=True, text=True, check=True
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
        ["node", "-"], input=harness, capture_output=True, text=True, check=True
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

  def test_focus_card_suppresses_noise_and_redundant_why_when_review_ready(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const itemBeforeTriage = {
  item_key: 'gerrit:1414017',
  target_id: '1414017',
  kind: 'gerrit',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  role: 'reviewer (ext)',
  patchset: 1,
  url: 'https://example.com/1414017',
  subject: '[skparagraph] Shift trailing space',
  project_name: 'SkParagraph',
  in_my_attention: true,
  last_msg_author: 'dbebawy@gmail.com',
  last_msg_snippet: 'jlavrova@google.com, who was added as reviewer owns the following files: * `TextLine.cpp`',
  last_msg_et: '13:53 ET',
  last_msg_age: '1h ago',
};

const itemAfterTriage = Object.assign({}, itemBeforeTriage, {
  triage: {
    verdict: 'CLEAN',
    verdict_label: '🟢 Clean (LGTM)',
    summary: "Correct fix: hit width now uses glyph's own shift",
    triaged_patchset: 1,
    triaged_at: '2026-11-02T19:38:09Z',
    is_stale: false,
    draft: 'Looks good.',
  }
});

const itemAuthorReplied = Object.assign({}, itemAfterTriage, {
  author_replied_to_me: true,
  last_msg_author: 'dbebawy@gmail.com',
  last_msg_snippet: 'Thanks Julia! Addressed your question.',
  last_msg_date: '2026-11-02 20:10:00.000000000',
});

console.log(JSON.stringify({
  beforeTriageHtml: focusCard(itemBeforeTriage, {jetski_box_url: ''}),
  afterTriageHtml: focusCard(itemAfterTriage, {jetski_box_url: ''}),
  authorRepliedHtml: focusCard(itemAuthorReplied, {jetski_box_url: ''}),
}));
"""
    proc = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())

    before = out["beforeTriageHtml"]
    self.assertNotIn("owns the following files", before)
    self.assertIn("Added as reviewer", before)

    after = out["afterTriageHtml"]
    self.assertNotIn('<div class="why">', after)
    self.assertIn("triage-box", after)
    self.assertIn("Correct fix: hit width now uses glyph's own shift", after)

    replied = out["authorRepliedHtml"]
    self.assertIn('<div class="why">', replied)
    self.assertIn("Thanks Julia! Addressed your question.", replied)
    self.assertIn("triage-box", replied)

  def test_flight_deck_corpus_gate_blocking_and_patchset_reset_rendering(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const skpPending = {
  item_key: 'gerrit:1414017',
  target_id: '1414017',
  kind: 'gerrit',
  host: 'skia-review.googlesource.com',
  role: 'reviewer (ext)',
  status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION',
  patchset: 1,
  reviewed_ps: 1,
  url: 'https://skia-review.googlesource.com/c/skia/+/1414017',
  subject: '[skparagraph] Shift trailing space of RTL run',
  project_name: 'SkParagraph',
  owner_name: 'David Bebawy',
  owner_email: 'dbebawy@gmail.com',
  is_external: true,
  triage: {
    verdict: 'CLEAN',
    verdict_label: '🟢 Clean (LGTM)',
    review_outcome: 'CLEAN',
    review_eyebrow_label: '🟢 Review PS1: Clean (LGTM)',
    corpus_required: true,
    corpus_status: 'pending',
    corpus_script: 'run_flutter_paragraph_tests.py',
    summary: 'Static logic and Skia dm pass; Flutter checkout leased',
    triaged_patchset: 1,
    triaged_at_et: 'Nov 02, 16:50 ET',
    is_stale: false,
    draft: 'Looks good.',
  }
};

const wpNitsPending = Object.assign({}, skpPending, {
  item_key: 'github:195600',
  target_id: '195600',
  kind: 'github',
  project_name: 'WebParagraph',
  triage: Object.assign({}, skpPending.triage, {
    verdict: 'NITS',
    verdict_label: '🔵 Minor nits',
    review_outcome: 'NITS',
    review_eyebrow_label: '🔵 Review PS1: Minor nits',
    corpus_script: 'run_webparagraph_corpus.py',
    corpus_status: 'pending',
  })
});

const skpFailed = Object.assign({}, skpPending, {
  triage: Object.assign({}, skpPending.triage, {
    corpus_status: 'failed',
  })
});

const skpWaived = Object.assign({}, skpPending, {
  triage: Object.assign({}, skpPending.triage, {
    corpus_status: 'waived',
  })
});

const skpPassedPs1 = Object.assign({}, skpPending, {
  triage: Object.assign({}, skpPending.triage, {
    corpus_status: 'passed',
    corpus_tested_patchset: 1,
  })
});

const skpUploadedPs2 = Object.assign({}, skpPassedPs1, {
  patchset: 2,
});

console.log(JSON.stringify({
  skpPendingCard: focusCard(skpPending, {jetski_box_url: ''}),
  skpPendingRow: rowHtml(skpPending, {jetski_box_url: ''}),
  skpPendingSpine: spineStepShortState(skpPending),
  wpNitsEyebrow: reviewVerdictEyebrowHtml(wpNitsPending),
  skpFailedCard: focusCard(skpFailed, {jetski_box_url: ''}),
  skpWaivedCard: focusCard(skpWaived, {jetski_box_url: ''}),
  skpPassedCard: focusCard(skpPassedPs1, {jetski_box_url: ''}),
  skpPs2Card: focusCard(skpUploadedPs2, {jetski_box_url: ''}),
  skpPs2Spine: spineStepShortState(skpUploadedPs2),
}));
"""
    proc = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())

    # Pending SkParagraph corpus renders amber blocked pill and never green LGTM
    self.assertIn("v-corpus_blocked", out["skpPendingCard"])
    self.assertIn("⏳ LGTM blocked: Flutter corpus not run", out["skpPendingCard"])
    self.assertNotIn("v-clean", out["skpPendingCard"])
    self.assertNotIn("triage-clean", out["skpPendingCard"])
    self.assertNotIn("🟢", out["skpPendingCard"])
    self.assertIn("⏳ LGTM blocked: Flutter corpus not run", out["skpPendingRow"])
    self.assertNotIn("triage-clean", out["skpPendingRow"])
    self.assertNotEqual(out["skpPendingSpine"]["tone"], "spine-ok")

    # Pending WebParagraph corpus on NITS renders amber blocked pill
    self.assertIn("v-corpus_blocked", out["wpNitsEyebrow"])
    self.assertIn("⏳ LGTM blocked: WebParagraph corpus not run", out["wpNitsEyebrow"])
    self.assertNotIn("v-nits", out["wpNitsEyebrow"])

    # Failed and waived corpus never render green
    self.assertIn("v-corpus_failed", out["skpFailedCard"])
    self.assertIn("🔴 LGTM blocked: Flutter corpus failed", out["skpFailedCard"])
    self.assertNotIn("v-clean", out["skpFailedCard"])
    self.assertIn("v-corpus_blocked", out["skpWaivedCard"])
    self.assertNotIn("v-clean", out["skpWaivedCard"])
    self.assertNotIn("triage-clean", out["skpWaivedCard"])

    # Passed Integrity Check on exact patchset renders green Clean (LGTM)
    self.assertIn("v-clean", out["skpPassedCard"])
    self.assertIn("🟢 Review PS1: Clean (LGTM)", out["skpPassedCard"])
    self.assertIn("triage-clean", out["skpPassedCard"])
    self.assertIn("Flutter corpus OK", out["skpPassedCard"])

    # Uploading PS2 resets gate to pending and drops green badges immediately
    self.assertNotIn("v-clean", out["skpPs2Card"])
    self.assertNotIn("triage-clean", out["skpPs2Card"])
    self.assertIn("PS2", out["skpPs2Card"])
    self.assertNotEqual(out["skpPs2Spine"]["tone"], "spine-ok")


if __name__ == "__main__":
  unittest.main()
