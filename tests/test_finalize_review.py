#!/usr/bin/env python3
"""Contract tests for post-review CL finalization (Variant A) and watcher helpers."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SCRIPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import triage_store as ts  # pylint: disable=g-import-not-at-top


def _sample_change_info(
    *,
    cl_num: int = 1409697,
    cur_ps: int = 6,
    cur_sha: str = "6e8dd008c261a01f67d988adb0927077758b2c84",
    owner_email: str = "wselna123@gmail.com",
    status: str = "NEW",
    wip: bool = False,
    self_vote: int = 0,
    mergeable: bool = True,
    files: list[str] | None = None,
) -> dict:
  file_list = files or [
      "modules/skparagraph/include/Paragraph.h",
      "modules/skparagraph/src/ParagraphImpl.cpp",
      "modules/skparagraph/tests/SkParagraphTest.cpp",
  ]
  rev_files = {p: {"lines_inserted": 10} for p in file_list}
  return {
      "_number": cl_num,
      "status": status,
      "work_in_progress": wip,
      "mergeable": mergeable,
      "subject": "[skparagraph] Add first-line indentation to Paragraph::layout",
      "owner": {"email": owner_email, "name": "William Selna"},
      "current_revision": cur_sha,
      "current_revision_number": cur_ps,
      "revisions": {
          "11d7fba59d8c98bf13b3dcc22da7497866fe530a": {"_number": 5, "files": rev_files},
          cur_sha: {"_number": cur_ps, "files": rev_files},
      },
      "labels": {
          "Code-Review": {
              "all": [
                  {"email": "jlavrova@google.com", "value": self_vote},
                  {"email": "alexisdavidc@google.com", "value": 0},
              ]
          }
      },
      "submit_requirements": [
          {"name": "Code-Review", "status": "UNSATISFIED"},
          {"name": "No-Unresolved-Comments", "status": "UNSATISFIED"},
      ],
  }


class WatcherAndTriageBugfixTest(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp_dir = tempfile.mkdtemp(prefix="r72_bugfix_")

  def tearDown(self) -> None:
    shutil.rmtree(self.tmp_dir, ignore_errors=True)

  def test_resolve_companion_threads_groups_by_leaf_patchset(self) -> None:
    posted_urls: list[str] = []
    posted_bodies: list[dict] = []

    def fake_urlopen(req, timeout=10):
      posted_urls.append(req.full_url)
      posted_bodies.append(json.loads(req.data.decode("utf-8")))
      resp = mock.MagicMock()
      resp.read.return_value = b")]}'\n{}"
      resp.__enter__.return_value = resp
      return resp

    fake_threads = {
        "1410416": {
            "mentioned": True,
            "all_resolved": False,
            "any_unresolved": True,
            "unresolved_leaves": [
                {
                    "id": "ps2_leaf_id",
                    "path": "/PATCHSET_LEVEL",
                    "patch_set": 2,
                    "line": None,
                },
                {
                    "id": "ps4_inline_id",
                    "path": "modules/skparagraph/include/Paragraph.h",
                    "patch_set": 4,
                    "line": 281,
                },
            ],
        }
    }
    with mock.patch.object(w, "fetch_gerrit_companion_threads", return_value=fake_threads):
      with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
        res = w.resolve_gerrit_companion_threads(
            "skia-review.googlesource.com", "1409697", "1410416", token="tok-123"
        )

    self.assertTrue(res["ok"])
    self.assertEqual(res["resolved_count"], 2)
    self.assertEqual(
        posted_urls,
        [
            "https://skia-review.googlesource.com/a/changes/1409697/revisions/2/review",
            "https://skia-review.googlesource.com/a/changes/1409697/revisions/4/review",
        ],
    )
    self.assertEqual(posted_bodies[0]["notify"], "NONE")
    self.assertFalse(
        posted_bodies[0]["comments"]["/PATCHSET_LEVEL"][0]["unresolved"]
    )
    self.assertEqual(
        posted_bodies[1]["comments"]["modules/skparagraph/include/Paragraph.h"][0]["line"],
        281,
    )

  def test_abandon_companion_sends_notify_none(self) -> None:
    captured: list[dict] = []

    def fake_urlopen(req, timeout=10):
      captured.append({"url": req.full_url, "body": json.loads(req.data.decode("utf-8"))})
      resp = mock.MagicMock()
      resp.read.return_value = b")]}'\n{}"
      resp.__enter__.return_value = resp
      return resp

    state = {
        "items": {
            "gerrit:1410416": {
                "item_key": "gerrit:1410416",
                "kind": "gerrit",
                "target_id": "1410416",
                "host": "skia-review.googlesource.com",
                "is_self": True,
                "status": "NEW",
                "companion_to": "gerrit:1409697",
                "ready_to_abandon_reason": "You approved CL 1409697 (PS6)",
            }
        },
        "events": [],
    }
    with mock.patch.object(w, "_get_gerrit_bearer_token", return_value="tok"):
      with mock.patch.object(
          w, "resolve_gerrit_companion_threads", return_value={"ok": True, "resolved_count": 0}
      ):
        with mock.patch("urllib.request.urlopen", side_effect=fake_urlopen):
          res = w.abandon_companion_items(["gerrit:1410416"], state=state, auto=True)

    self.assertEqual(res["abandoned"], ["gerrit:1410416"])
    self.assertEqual(len(captured), 1)
    self.assertEqual(captured[0]["body"].get("notify"), "NONE")

  def test_triage_markdown_prefers_parent_cl_url_over_companion_url(self) -> None:
    md = (
        "### Детали по 1409697\n"
        "## Review Triage: [skparagraph] Add first-line indentation\n"
        "**Verdict:** [SURGICAL_AUDIT]\n"
        "**Why:** Core domain logic.\n"
        "| **Patchset** | `PS6` | Follow-up |\n"
        "Prior thread references https://skia-review.googlesource.com/c/skia/+/1410416 "
        "and parent https://skia-review.googlesource.com/c/skia/+/1409697/6.\n"
    )
    parsed = ts.parse_queue_triage_markdown(md)
    self.assertEqual(len(parsed), 1)
    self.assertIn("1409697", parsed[0]["url"])
    self.assertNotIn("1410416", parsed[0]["url"])

    gh_md = (
        "### Детали по flutter#195366\n"
        "## Review Triage: Fix WebParagraph strut metrics\n"
        "**Verdict:** [SURGICAL_AUDIT]\n"
        "**Why:** Mirrors Skia https://skia-review.googlesource.com/c/skia/+/1409697.\n"
        "| **Patchset** | `PS1` | Initial |\n"
        "PR link: https://github.com/flutter/flutter/pull/195366\n"
    )
    gh_parsed = ts.parse_queue_triage_markdown(gh_md)
    self.assertEqual(len(gh_parsed), 1)
    self.assertEqual(gh_parsed[0]["kind"], "github")
    self.assertEqual(gh_parsed[0]["url"], "https://github.com/flutter/flutter/pull/195366")


class FinalizeReviewPipelineTest(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp_dir = tempfile.mkdtemp(prefix="r72_finalize_")
    self.results_dir = os.path.join(self.tmp_dir, "triage_results")
    os.makedirs(self.results_dir, exist_ok=True)
    self.state_path = os.path.join(self.tmp_dir, "cl_watcher_state.json")
    self.flutter_md = os.path.join(self.tmp_dir, "flutter_ps6", "compare.md")
    os.makedirs(os.path.dirname(self.flutter_md), exist_ok=True)
    with open(self.flutter_md, "w", encoding="utf-8") as fh:
      fh.write(
          "# Flutter Paragraph Engine Test Comparison\n\n"
          "**Вердикт целостности (Integrity Check):** `VALID`\n"
          "Поломано: 0\n"
          "| `cand` | `6e8dd008c2` |\n"
      )
    self.g3_md = os.path.join(self.tmp_dir, "g3_cl_1409697_ps6", "compare.md")
    os.makedirs(os.path.dirname(self.g3_md), exist_ok=True)
    with open(self.g3_md, "w", encoding="utf-8") as fh:
      fh.write(
          "# Google3 Paragraph Corpus Comparison (baseline = `baseline`)\n\n"
          "**Вердикт целостности (Integrity Check):** `VALID`\n"
          "| `cand` | `cl:1409697/6` (`6e8dd008c2`) |\n"
          "**Выполнено тестов: 1140 | Новых падений (Regressions): 0 | Флипов FAIL→PASS: 0**\n"
      )

  def tearDown(self) -> None:
    shutil.rmtree(self.tmp_dir, ignore_errors=True)

  def _seed_clean_triage(self, companion_id: str = "1410416") -> None:
    ts.save_review_verdict(
        target_id="1409697",
        kind="gerrit",
        patchset=6,
        outcome="CLEAN",
        summary="PS6 = PS5 rebased; all gates pass",
        draft="Thanks for the rebase! All tests pass on PS6. LGTM.",
        companion_id=companion_id,
        corpus="passed",
        compare_md=self.flutter_md,
        g3_compare_md=self.g3_md,
        revision="6e8dd008c2",
        results_dir=self.results_dir,
    )

  def test_stage_own_thread_drafts_deduplicates_in_place(self) -> None:
    import finalize_review as fr

    calls: list[tuple[str, str, dict | None]] = []

    def fake_api(host, method, path, payload=None):
      calls.append((method, path, payload))
      if method == "GET" and path.startswith("/changes/1409697?"):
        return _sample_change_info()
      if method == "GET" and path == "/changes/1409697/comments":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "6088a82d_d9cbcc2e",
                    "patch_set": 5,
                    "unresolved": True,
                    "updated": "2026-10-30 22:00:00.000000000",
                    "author": {"email": "jlavrova@google.com"},
                    "message": "Could you please rebase onto current main?",
                },
                {
                    "id": "other_rev_thread",
                    "patch_set": 5,
                    "unresolved": True,
                    "updated": "2026-10-30 22:05:00.000000000",
                    "author": {"email": "alexisdavidc@google.com"},
                    "message": "Nit on naming",
                },
            ]
        }
      if method == "GET" and path == "/changes/1409697/revisions/5/drafts":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "existing_done_draft",
                    "patch_set": 5,
                    "in_reply_to": "6088a82d_d9cbcc2e",
                    "unresolved": True,
                    "message": "Old draft",
                }
            ]
        }
      if method == "PUT":
        return {"id": "existing_done_draft", **(payload or {})}
      raise AssertionError(f"Unexpected call: {method} {path}")

    staged = fr.stage_own_thread_drafts(
        "1409697",
        host="skia-review.googlesource.com",
        auto_rebase=True,
        api_fn=fake_api,
    )
    self.assertEqual(len(staged), 1)
    self.assertEqual(staged[0]["draft_id"], "existing_done_draft")
    put_calls = [c for c in calls if c[0] == "PUT"]
    self.assertEqual(len(put_calls), 1)
    self.assertEqual(
        put_calls[0][1], "/changes/1409697/revisions/5/drafts/existing_done_draft"
    )
    self.assertEqual(put_calls[0][2]["message"], "Done")
    self.assertFalse(put_calls[0][2]["unresolved"])

  def test_preview_mirroring_live_1409697_state(self) -> None:
    import finalize_review as fr

    self._seed_clean_triage(companion_id="1410416")

    def fake_api(host, method, path, payload=None):
      self.assertEqual(method, "GET")
      if path.startswith("/changes/1409697?"):
        return _sample_change_info()
      if path == "/changes/1409697/comments":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "6088a82d_d9cbcc2e",
                    "patch_set": 5,
                    "unresolved": True,
                    "updated": "2026-10-30 22:00:00.000000000",
                    "author": {"email": "jlavrova@google.com"},
                    "message": "Could you please rebase onto current main?",
                },
                {
                    "id": "e905d3b4_7c30f0c6",
                    "patch_set": 5,
                    "unresolved": False,
                    "updated": "2026-11-03 12:27:00.000000000",
                    "author": {"email": "wselna123@gmail.com"},
                    "message": "It should be now fixed, I uploaded a patch",
                },
            ]
        }
      if path == "/changes/1409697/drafts":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "8a80ef6a_f44bd041",
                    "patch_set": 6,
                    "unresolved": False,
                    "message": "Thanks for the rebase! LGTM.",
                },
                {
                    "id": "stale_ps3_draft",
                    "patch_set": 3,
                    "unresolved": True,
                    "message": "Forgotten stale draft from PS3",
                },
            ]
        }
      if path.startswith("/changes/1410416"):
        return {
            "_number": 1410416,
            "status": "ABANDONED",
            "work_in_progress": True,
            "subject": "[skparagraph] WIP companion",
            "owner": {"email": "jlavrova@google.com"},
        }
      raise AssertionError(f"Unexpected GET {path}")

    fake_builds = {
        "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload": {
            "id": "8700000000000000001",
            "status": "SUCCESS",
        }
    }
    preview = fr.build_finalize_preview(
        "1409697",
        results_dir=self.results_dir,
        search_dirs=[self.tmp_dir],
        has_citc=True,
        api_fn=fake_api,
        bb_fn=lambda cl, ps: fake_builds,
    )
    self.assertTrue(preview["ready"], preview.get("blockers"))
    self.assertEqual(preview["blockers"], [])
    self.assertEqual(preview["patchset"], 6)
    self.assertEqual(preview["gates"]["flutter_corpus"]["status"], "passed")
    self.assertEqual(preview["gates"]["g3_corpus"]["status"], "passed")
    self.assertEqual(preview["gates"]["asan"]["status"], "passed")

    by_draft = {d["id"]: d for d in preview["drafts"]}
    self.assertTrue(by_draft["8a80ef6a_f44bd041"]["selected_by_default"])
    self.assertFalse(by_draft["stale_ps3_draft"]["selected_by_default"])
    self.assertIn("Unexpected", by_draft["stale_ps3_draft"]["warning"])

    self.assertEqual(len(preview["own_unresolved_threads"]), 1)
    t0 = preview["own_unresolved_threads"][0]
    self.assertEqual(t0["leaf_id"], "6088a82d_d9cbcc2e")
    self.assertEqual(t0["patchset"], 5)
    self.assertTrue(t0["selected_by_default"])

    self.assertEqual(len(preview["companions"]), 1)
    self.assertEqual(preview["companions"][0]["action"], "noop_already_closed")
    self.assertEqual(preview["deferred_canaries"], [])

  def test_preview_blocks_on_revision_drift_and_failed_asan(self) -> None:
    import finalize_review as fr

    self._seed_clean_triage()

    def drifted_api(host, method, path, payload=None):
      if path.startswith("/changes/1409697?"):
        return _sample_change_info(
            cur_ps=7, cur_sha="deadbeef99999999999999999999999999999999"
        )
      if path == "/changes/1409697/comments":
        return {}
      if path == "/changes/1409697/drafts":
        return {}
      return {"status": "ABANDONED", "owner": {"email": "jlavrova@google.com"}}

    preview = fr.build_finalize_preview(
        "1409697",
        results_dir=self.results_dir,
        search_dirs=[self.tmp_dir],
        has_citc=True,
        api_fn=drifted_api,
        bb_fn=lambda cl, ps: {},
    )
    self.assertFalse(preview["ready"])
    joined = " | ".join(preview["blockers"])
    self.assertIn("PS6", joined)
    self.assertIn("PS7", joined)
    self.assertIn("ASAN", joined)

  def test_execute_atomic_publish_open_companion_abandon_and_deferred_canary(self) -> None:
    import finalize_review as fr

    core_sha = "abcdef1234567890abcdef1234567890abcdef12"
    ts.save_review_verdict(
        target_id="1499999",
        kind="gerrit",
        patchset=3,
        outcome="CLEAN",
        summary="Clean core header fix",
        draft="Looks good, thanks! LGTM.",
        companion_id="1499998",
        corpus="not_required",
        revision="abcdef1234",
        results_dir=self.results_dir,
    )
    with open(self.state_path, "w", encoding="utf-8") as sf:
      json.dump(
          {
              "items": {
                  "gerrit:1499999@chat1": {
                      "item_key": "gerrit:1499999",
                      "kind": "gerrit",
                      "target_id": "1499999",
                      "host": "skia-review.googlesource.com",
                      "status": "NEW",
                      "patchset": 3,
                      "is_self": False,
                      "is_external": True,
                      "required_approvals": 2,
                      "approved_count": 0,
                      "missing_approvals": 2,
                      "missing_reviewers_to_assign": 0,
                      "self_cr_vote": 0,
                      "self_approved": False,
                      "in_my_attention": True,
                      "phase": "NEEDS_YOUR_ACTION",
                      "self_threads_unresolved": 1,
                      "self_threads_resolved": 0,
                      "companions": [{"item_key": "gerrit:1499998", "target_id": "1499998"}],
                  }
              },
              "events": [],
          },
          sf,
      )

    mutations: list[tuple[str, str, dict | None]] = []
    triggered_cmds: list[list[str]] = []

    def fake_api(host, method, path, payload=None):
      if method != "GET":
        mutations.append((method, path, payload))
      if method == "GET" and path.startswith("/changes/1499999?"):
        return _sample_change_info(
            cl_num=1499999,
            cur_ps=3,
            cur_sha=core_sha,
            files=["include/core/SkCanvas.h", "src/core/SkCanvas.cpp"],
        )
      if method == "GET" and path == "/changes/1499999/comments":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "ps2_rebase_leaf",
                    "patch_set": 2,
                    "unresolved": True,
                    "updated": "2026-11-01 10:00:00.000000000",
                    "author": {"email": "jlavrova@google.com"},
                    "message": "Please rebase onto main",
                }
            ]
        }
      if method == "GET" and path == "/changes/1499999/drafts":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "ps3_lgtm_draft",
                    "patch_set": 3,
                    "unresolved": False,
                    "message": "Looks good, thanks! LGTM.",
                },
                {
                    "id": "ps1_forgotten_draft",
                    "patch_set": 1,
                    "unresolved": True,
                    "message": "Do not publish me",
                },
            ]
        }
      if method == "GET" and path == "/changes/1499999/revisions/2/drafts":
        return {}
      if method == "PUT" and path == "/changes/1499999/revisions/2/drafts":
        return {"id": "minted_ps2_done_draft", **(payload or {})}
      if method == "POST" and path == "/changes/1499999/revisions/3/review":
        return {"labels": {"Code-Review": 1}}
      if method == "GET" and path.startswith("/changes/1499998"):
        return {
            "_number": 1499998,
            "status": "NEW",
            "work_in_progress": True,
            "subject": "[core] WIP companion",
            "owner": {"email": "jlavrova@google.com"},
        }
      if method == "POST" and path == "/changes/1499998/abandon":
        return {"status": "ABANDONED"}
      raise AssertionError(f"Unexpected API {method} {path}")

    fake_asan = {
        "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN": {
            "id": "8700000000000000002",
            "status": "SUCCESS",
        }
    }

    def fake_runner(cmd_list, background=False):
      triggered_cmds.append((list(cmd_list), background))
      return 0, "ok"

    res = fr.execute_finalize_review(
        "1499999",
        expected_revision=core_sha,
        draft_ids=["ps3_lgtm_draft"],
        resolve_thread_leaf_ids=["ps2_rebase_leaf"],
        abandon_companion_ids=["1499998"],
        trigger_canaries=["Canary-G3"],
        results_dir=self.results_dir,
        state_path=self.state_path,
        search_dirs=[self.tmp_dir],
        has_citc=True,
        api_fn=fake_api,
        bb_fn=lambda cl, ps: fake_asan,
        cmd_runner=fake_runner,
    )
    self.assertTrue(res["ok"], res)
    self.assertEqual(
        res["published_draft_ids"],
        ["ps3_lgtm_draft", "minted_ps2_done_draft"],
    )
    review_posts = [m for m in mutations if m[0] == "POST" and m[1].endswith("/review")]
    self.assertEqual(len(review_posts), 1)
    rbody = review_posts[0][2]
    self.assertEqual(rbody["labels"], {"Code-Review": 1})
    self.assertEqual(rbody["drafts"], "PUBLISH_ALL_REVISIONS")
    self.assertEqual(
        rbody["draft_ids_to_publish"],
        ["ps3_lgtm_draft", "minted_ps2_done_draft"],
    )
    self.assertNotIn("ps1_forgotten_draft", rbody["draft_ids_to_publish"])

    abandon_posts = [m for m in mutations if m[0] == "POST" and m[1].endswith("/abandon")]
    self.assertEqual(len(abandon_posts), 1)
    self.assertEqual(abandon_posts[0][2]["notify"], "NONE")

    self.assertEqual(len(triggered_cmds), 2)
    self.assertIn("Canary-G3", " ".join(triggered_cmds[0][0]))
    self.assertFalse(triggered_cmds[0][1])
    self.assertIn("watch", triggered_cmds[1][0])
    self.assertTrue(triggered_cmds[1][1])

    with open(self.state_path, encoding="utf-8") as sf_in:
      updated_state = json.load(sf_in)
    rec = updated_state["items"]["gerrit:1499999@chat1"]
    self.assertTrue(rec["self_approved"])
    self.assertEqual(rec["self_cr_vote"], 1)
    self.assertEqual(rec["self_approval_ps"], 3)
    self.assertFalse(rec["in_my_attention"])
    self.assertEqual(rec["companions"], [])

  def test_waived_flutter_corpus_does_not_waive_pending_g3_corpus(self) -> None:
    import finalize_review as fr

    pending_g3 = fr._evaluate_g3_corpus_gate(
        "1409697",
        6,
        "6e8dd008c2",
        ["modules/skparagraph/src/ParagraphImpl.cpp"],
        "[skparagraph] Add first-line indentation",
        {
            "corpus_required": True,
            "corpus_status": "waived",
            "g3_corpus_required": True,
            "g3_corpus_status": "pending",
            "triaged_patchset": 6,
        },
        [],
        True,
    )
    self.assertEqual(pending_g3["status"], "pending")

    explicitly_waived_g3 = fr._evaluate_g3_corpus_gate(
        "1409697",
        6,
        "6e8dd008c2",
        ["modules/skparagraph/src/ParagraphImpl.cpp"],
        "[skparagraph] Add first-line indentation",
        {
            "corpus_required": True,
            "corpus_status": "passed",
            "g3_corpus_required": True,
            "g3_corpus_status": "waived",
            "triaged_patchset": 6,
        },
        [],
        True,
    )
    self.assertEqual(explicitly_waived_g3["status"], "waived")


class FlightDeckFinalizeUiTest(unittest.TestCase):

  def test_dashboard_js_renders_approve_and_publish_only_when_clean_and_unapproved(self) -> None:
    node = shutil.which("node")
    if not node:
      self.skipTest("node runtime unavailable")
    m_start = w.DASHBOARD_HTML.index("<script>") + len("<script>")
    m_end = w.DASHBOARD_HTML.rindex("</script>")
    js_src = w.DASHBOARD_HTML[m_start:m_end].replace("boot();", "")
    harness = (
        js_src
        + """
const cleanItem = {
  item_key: 'gerrit:1409697',
  kind: 'gerrit',
  target_id: '1409697',
  status: 'NEW',
  patchset: 6,
  is_self: false,
  is_wip: false,
  self_approved: false,
  self_cr_vote: 0,
  triage: {
    review_outcome: 'CLEAN',
    verdict: 'CLEAN',
    triaged_patchset: 6,
    corpus_required: true,
    corpus_status: 'passed',
    corpus_tested_patchset: 6,
    corpus_script: 'run_flutter_paragraph_tests.py',
    draft: 'LGTM.',
    summary: 'All gates passed',
    triaged_at_et: 'Nov 03, 11:34 ET'
  }
};
const blockedItem = JSON.parse(JSON.stringify(cleanItem));
blockedItem.triage.corpus_status = 'pending';
const approvedItem = JSON.parse(JSON.stringify(cleanItem));
approvedItem.self_approved = true;
approvedItem.self_cr_vote = 1;
console.log(JSON.stringify({
  cleanHtml: triageHtml(cleanItem, false),
  blockedHtml: triageHtml(blockedItem, false),
  approvedHtml: triageHtml(approvedItem, false),
}));
"""
    )
    proc = subprocess.run(
        [node, "-"], input=harness, capture_output=True, text=True, timeout=10, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertIn("Approve & publish", out["cleanHtml"].replace("&amp;", "&"))
    self.assertIn("openFinalizeModal", out["cleanHtml"])
    self.assertNotIn("openFinalizeModal", out["blockedHtml"])
    self.assertNotIn("openFinalizeModal", out["approvedHtml"])


if __name__ == "__main__":
  unittest.main()
