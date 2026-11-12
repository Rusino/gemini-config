#!/usr/bin/env python3
"""Contract tests for post-review CL finalization (Variant A) and watcher helpers."""

from __future__ import annotations

import copy
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
                "is_wip": True,
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

  def test_g3_waiver_overrides_failed_compare_md_only_on_its_patchset(self) -> None:
    import finalize_review as fr

    failed_md = os.path.join(self.tmp_dir, "g3_cl_1409697_failed", "compare.md")
    os.makedirs(os.path.dirname(failed_md), exist_ok=True)
    with open(failed_md, "w", encoding="utf-8") as fh:
      fh.write(
          "# Google3 Paragraph Corpus Comparison (baseline = `baseline`)\n\n"
          "**Вердикт целостности (Integrity Check):** `VALID`\n"
          "| `cand` | `cl:1409697/6` (`6e8dd008c2`) |\n"
          "**Выполнено тестов: 1139 | Новых падений (Regressions): 1 | Флипов FAIL→PASS: 0**\n"
      )

    def gate(cur_ps: int, cur_sha: str, triage: dict[str, Any]) -> str:
      return fr._evaluate_g3_corpus_gate(
          "1409697",
          cur_ps,
          cur_sha,
          ["modules/skparagraph/src/ParagraphImpl.cpp"],
          "[skparagraph] Add first-line indentation",
          {"g3_corpus_compare_md": failed_md, "g3_corpus_required": True, **triage},
          [],
          True,
      )["status"]

    waived_ps6 = {
        "g3_corpus_status": "waived",
        "triaged_patchset": 6,
        "triaged_revision": "6e8dd008c2",
    }
    self.assertEqual(gate(6, "6e8dd008c2", waived_ps6), "waived")
    self.assertEqual(gate(6, "6e8dd008c2", {"g3_corpus_status": "failed"}), "failed")
    self.assertEqual(gate(6, "aaaaaaa111", waived_ps6), "failed")
    self.assertEqual(gate(7, "6e8dd008c2", waived_ps6), "failed")

  def test_evaluate_cq_dry_run_gate_skcq_luci_cv_and_equivalent_patchsets(self) -> None:
    import finalize_review as fr

    base = _sample_change_info(cl_num=1409697, cur_ps=6)
    base["revisions"]["11d7fba59d8c98bf13b3dcc22da7497866fe530a"]["kind"] = "REWORK"
    base["revisions"][base["current_revision"]]["kind"] = "TRIVIAL_REBASE"
    self.assertEqual(fr.compute_equivalent_patchsets(base, 6), [5, 6])

    skcq_pass_ps5 = json.loads(json.dumps(base))
    skcq_pass_ps5["messages"] = [
        {
            "_revision_number": 5,
            "message": (
                "Patch Set 5: -Commit-Queue\n\n"
                "Change has been successfully Dry-Run'd: "
                "https://skia-review.googlesource.com/1409697 passed the SkCQ dry run"
            ),
        }
    ]
    g_pass = fr.evaluate_cq_dry_run_gate(skcq_pass_ps5, cur_ps=6)
    self.assertEqual(g_pass["status"], "passed")
    self.assertIn("equivalent PS5", g_pass["detail"])

    rework_ps6 = json.loads(json.dumps(skcq_pass_ps5))
    rework_ps6["revisions"][rework_ps6["current_revision"]]["kind"] = "REWORK"
    self.assertEqual(fr.evaluate_cq_dry_run_gate(rework_ps6, cur_ps=6)["status"], "not_run")

    luci_pass = _sample_change_info(cl_num=8663938, cur_ps=6)
    luci_pass["messages"] = [
        {
            "_revision_number": 6,
            "tag": "autogenerated:cq:dry-run",
            "message": "Patch Set 6: -Commit-Queue\n\nDry run: This CL has passed the CQ dry run.",
        }
    ]
    self.assertEqual(fr.evaluate_cq_dry_run_gate(luci_pass, cur_ps=6)["status"], "passed")

    luci_fail = _sample_change_info(cl_num=8663938, cur_ps=6)
    luci_fail["messages"] = [
        {
            "_revision_number": 6,
            "tag": "autogenerated:cq:dry-run",
            "message": (
                "Patch Set 6: -Commit-Queue\n\n"
                "Dry run: This CL failed the CQ dry run.\n\nFailed Tryjobs:\n* linux-rel"
            ),
        }
    ]
    self.assertEqual(fr.evaluate_cq_dry_run_gate(luci_fail, cur_ps=6)["status"], "failed")

    running = _sample_change_info(cl_num=1409697, cur_ps=6)
    running["labels"]["Commit-Queue"] = {
        "all": [{"email": "jlavrova@google.com", "value": 1}]
    }
    self.assertEqual(fr.evaluate_cq_dry_run_gate(running, cur_ps=6)["status"], "running")

  def test_arm_disarm_and_fast_path_publish_when_cq_already_passed(self) -> None:
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
                  "gerrit:1499999": {
                      "item_key": "gerrit:1499999",
                      "kind": "gerrit",
                      "target_id": "1499999",
                      "host": "skia-review.googlesource.com",
                      "status": "NEW",
                      "patchset": 3,
                      "revision": core_sha,
                      "is_self": False,
                      "is_wip": False,
                      "self_cr_vote": 0,
                      "self_approved": False,
                      "in_my_attention": True,
                      "phase": "NEEDS_YOUR_ACTION",
                      "triage": {
                          "review_outcome": "CLEAN",
                          "verdict": "CLEAN",
                          "triaged_patchset": 3,
                          "corpus_required": False,
                      },
                  }
              },
              "events": [],
          },
          sf,
      )

    mutations: list[tuple[str, str, dict | None]] = []
    cq_passed = False

    def fake_api(host, method, path, payload=None):
      if method != "GET":
        mutations.append((method, path, payload))
      if method == "GET" and path.startswith("/changes/1499999?"):
        info = _sample_change_info(
            cl_num=1499999,
            cur_ps=3,
            cur_sha=core_sha,
            files=["include/core/SkCanvas.h", "src/core/SkCanvas.cpp"],
        )
        if cq_passed:
          info["messages"] = [
              {
                  "_revision_number": 3,
                  "message": "Patch Set 3: passed the SkCQ dry run",
              }
          ]
        return info
      if method == "GET" and path == "/changes/1499999/comments":
        return {}
      if method == "GET" and path == "/changes/1499999/drafts":
        return {
            "/PATCHSET_LEVEL": [
                {
                    "id": "ps3_lgtm_draft",
                    "patch_set": 3,
                    "unresolved": False,
                    "message": "Looks good, thanks! LGTM.",
                }
            ]
        }
      if method == "GET" and path.startswith("/changes/1499998"):
        return {
            "_number": 1499998,
            "status": "ABANDONED",
            "work_in_progress": True,
            "owner": {"email": "jlavrova@google.com"},
        }
      if method == "POST" and path == "/changes/1499999/revisions/3/review":
        return {"labels": (payload or {}).get("labels", {})}
      raise AssertionError(f"Unexpected API {method} {path}")

    fake_asan = {
        "Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN": {
            "id": "8700000000000000002",
            "status": "SUCCESS",
        }
    }

    arm_res = fr.arm_finalize_review(
        "1499999",
        expected_revision=core_sha,
        draft_ids=["ps3_lgtm_draft"],
        results_dir=self.results_dir,
        state_path=self.state_path,
        search_dirs=[self.tmp_dir],
        has_citc=True,
        api_fn=fake_api,
        bb_fn=lambda cl, ps: fake_asan,
    )
    self.assertTrue(arm_res["ok"], arm_res)
    self.assertTrue(arm_res["armed"])
    self.assertEqual(arm_res["mode"], "armed")
    self.assertTrue(arm_res["triggered_cq_dry_run"])
    self.assertEqual(len(mutations), 1)
    self.assertEqual(mutations[0][2]["labels"], {"Commit-Queue": 1})
    self.assertEqual(mutations[0][2]["notify"], "NONE")
    self.assertTrue(mutations[0][2]["ignore_automatic_attention_set_rules"])
    self.assertNotIn("draft_ids_to_publish", mutations[0][2])

    with open(self.state_path, encoding="utf-8") as sf_in:
      st_armed = json.load(sf_in)
    rec = st_armed["items"]["gerrit:1499999"]
    self.assertIsInstance(rec.get("armed_finalize"), dict)
    self.assertEqual(rec["armed_finalize"]["draft_ids"], ["ps3_lgtm_draft"])
    self.assertFalse(rec["in_my_attention"])
    self.assertEqual(rec["phase"], "WAITING_ON_OTHERS")

    disarm_res = fr.disarm_finalize_review("1499999", state_path=self.state_path)
    self.assertTrue(disarm_res["ok"])
    self.assertEqual(disarm_res["mode"], "disarmed")
    self.assertFalse(disarm_res["armed"])
    with open(self.state_path, encoding="utf-8") as sf_in:
      st_disarmed = json.load(sf_in)
    rec2 = st_disarmed["items"]["gerrit:1499999"]
    self.assertIsNone(rec2.get("armed_finalize"))
    self.assertTrue(rec2["in_my_attention"])
    self.assertEqual(rec2["phase"], "NEEDS_YOUR_ACTION")

    mutations.clear()
    cq_passed = True
    fast_res = fr.arm_finalize_review(
        "1499999",
        expected_revision=core_sha,
        draft_ids=["ps3_lgtm_draft"],
        results_dir=self.results_dir,
        state_path=self.state_path,
        search_dirs=[self.tmp_dir],
        has_citc=True,
        api_fn=fake_api,
        bb_fn=lambda cl, ps: fake_asan,
        cmd_runner=lambda cmd_list, background=False: (0, "ok"),
    )
    self.assertTrue(fast_res["ok"], fast_res)
    self.assertFalse(fast_res["armed"])
    self.assertEqual(fast_res["mode"], "published")
    self.assertEqual(len(mutations), 1)
    self.assertEqual(mutations[0][2]["labels"], {"Code-Review": 1})
    self.assertEqual(mutations[0][2]["draft_ids_to_publish"], ["ps3_lgtm_draft"])


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
  revision: 'sha_ps6',
  is_self: false,
  is_wip: false,
  self_approved: false,
  self_cr_vote: 0,
  cq_status: '',
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
const cqRunningItem = JSON.parse(JSON.stringify(cleanItem));
cqRunningItem.cq_status = 'CQ+1 (Dry Run)';
const cqPassedItem = JSON.parse(JSON.stringify(cleanItem));
cqPassedItem.cq_status = '✓ Dry Run Passed';
const armedItem = JSON.parse(JSON.stringify(cleanItem));
armedItem.cq_status = 'CQ+1 (Dry Run)';
armedItem.armed_finalize = {
  patchset: 6,
  expected_revision: 'sha_ps6',
  draft_ids: ['d1'],
  armed_at: 1700000000
};
const blockedItem = JSON.parse(JSON.stringify(cleanItem));
blockedItem.triage.corpus_status = 'pending';
const approvedItem = JSON.parse(JSON.stringify(cleanItem));
approvedItem.self_approved = true;
approvedItem.self_cr_vote = 1;
console.log(JSON.stringify({
  cleanHtml: triageHtml(cleanItem, false),
  cqRunningHtml: triageHtml(cqRunningItem, false),
  cqPassedHtml: triageHtml(cqPassedItem, false),
  armedHtml: triageHtml(armedItem, false),
  blockedHtml: triageHtml(blockedItem, false),
  approvedHtml: triageHtml(approvedItem, false),
  skcqFresh: hasFreshReviewWithoutNewerReply({
    ...cleanItem,
    last_msg_author: 'skia-commit-bot@chromium.org',
    last_msg_snippet: 'Patch Set 6: Commit-Queue+1\\n\\nDry run: This CL passed the CQ dry run.',
    last_msg_date: '2026-11-06 14:00:00.000000000',
    triage: {...cleanItem.triage, triaged_at: '2026-11-06T12:00:00Z'}
  }),
}));
"""
    )
    proc = subprocess.run(
        [node, "-"], input=harness, capture_output=True, text=True, timeout=10, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertIn("Run CQ Dry Run → auto CR+1", out["cleanHtml"])
    self.assertIn("openFinalizeModal", out["cleanHtml"])
    self.assertIn("Arm auto CR+1 after CQ Dry Run", out["cqRunningHtml"])
    self.assertIn("Publish CR+1 (CQ ✓)", out["cqPassedHtml"])
    self.assertIn("Armed: CQ Dry Run → CR+1", out["armedHtml"])
    self.assertIn("Cancel auto CR+1", out["armedHtml"])
    self.assertIn("disarmFinalizeReview", out["armedHtml"])
    self.assertTrue(out["skcqFresh"])
    self.assertNotIn("openFinalizeModal", out["blockedHtml"])
    self.assertNotIn("openFinalizeModal", out["approvedHtml"])

  def test_watcher_concurrent_merge_preserves_arm_and_propagates_disarm(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      sp = os.path.join(tmpdir, "state.json")
      with mock.patch.object(w, "STATE_FILE", sp):
        # Case 1: Armed out-of-band while a poll started from an un-armed snapshot
        base_state = {"items": {"gerrit:1409697": {"item_key": "gerrit:1409697", "cq_status": ""}}}
        disk_armed = {
            "items": {
                "gerrit:1409697": {
                    "item_key": "gerrit:1409697",
                    "cq_status": "CQ+1 (Dry Run)",
                    "in_my_attention": False,
                    "phase": "WAITING_ON_OTHERS",
                    "self_triggered_dry_run_ps": 6,
                    "armed_finalize": {"patchset": 6, "expected_revision": "sha6"},
                }
            }
        }
        with open(sp, "w", encoding="utf-8") as f:
          json.dump(disk_armed, f)
        saving_poll = {
            "items": {
                "gerrit:1409697": {
                    "item_key": "gerrit:1409697",
                    "cq_status": "",
                    "in_my_attention": True,
                    "phase": "NEEDS_YOUR_ACTION",
                }
            }
        }
        w.merge_concurrent_state_mutations(saving_poll, base_state)
        rec = saving_poll["items"]["gerrit:1409697"]
        self.assertEqual(rec["armed_finalize"], {"patchset": 6, "expected_revision": "sha6"})
        self.assertFalse(rec["in_my_attention"])
        self.assertEqual(rec["phase"], "WAITING_ON_OTHERS")

        # Case 2: Disarmed out-of-band while a poll started from an armed snapshot
        base_armed = copy.deepcopy(disk_armed)
        disk_disarmed = {
            "items": {
                "gerrit:1409697": {
                    "item_key": "gerrit:1409697",
                    "cq_status": "CQ+1 (Dry Run)",
                    "in_my_attention": True,
                    "phase": "NEEDS_YOUR_ACTION",
                }
            }
        }
        with open(sp, "w", encoding="utf-8") as f:
          json.dump(disk_disarmed, f)
        saving_armed_poll = copy.deepcopy(disk_armed)
        w.merge_concurrent_state_mutations(saving_armed_poll, base_armed)
        self.assertNotIn("armed_finalize", saving_armed_poll["items"]["gerrit:1409697"])


if __name__ == "__main__":
  unittest.main()
