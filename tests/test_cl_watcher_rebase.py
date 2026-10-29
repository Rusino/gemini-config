#!/usr/bin/env python3
"""Contract tests for R6 Conflict Rebase action in cl_watcher.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class ClWatcherRebaseTest(unittest.TestCase):

  def _write_state(self, tmp: str, state: dict) -> str:
    path = os.path.join(tmp, "state.json")
    with open(path, "w", encoding="utf-8") as f:
      json.dump(state, f)
    return path

  def test_rebase_item_wakes_chain_tail_and_marks_busy(self):
    initial = {
        "items": {
            "gerrit:1408158@c-root": {
                "item_key": "gerrit:1408158",
                "kind": "gerrit",
                "target_id": "1408158",
                "host": "skia-review.googlesource.com",
                "url": "https://skia-review.googlesource.com/c/skia/+/1408158",
                "subject": "[skparagraph] Use std::string",
                "status": "NEW",
                "phase": "WAITING_ON_OTHERS",
                "patchset": 4,
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "c-root",
                "project_id": "p-skp",
                "project_name": "SkParagraph",
                "chat_busy": False,
                "composite_key": "gerrit:1408158@c-root",
            }
        },
        "events": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl,
               "discover_conversation_chain",
               return_value=["c-root", "c-tail"],
           ) as mock_chain, \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value={}
           ), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake:
        res = w.rebase_item("gerrit:1408158")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertTrue(res.get("woken"))
    self.assertEqual(res["conversation_id"], "c-tail")
    self.assertTrue(res["chat_url"].endswith("/c/c-tail"))
    mock_chain.assert_called_once_with("c-root", include_descendants=True)
    mock_wake.assert_called_once()
    woken_cid, msg = mock_wake.call_args.args
    self.assertEqual(woken_cid, "c-tail")
    self.assertIn("https://skia-review.googlesource.com/c/skia/+/1408158", msg)
    self.assertIn("origin/main", msg)
    self.assertIn("git cl upload", msg)
    self.assertIn("CQ+1", msg)
    self.assertNotIn("gerrit:1408158@c-root", saved["items"])
    rec = saved["items"]["gerrit:1408158@c-tail"]
    self.assertTrue(rec["chat_busy"])
    self.assertGreater(rec.get("chat_woken_ts", 0), 0)
    self.assertEqual(len(saved["events"]), 1)

  def test_rebase_item_starts_project_pinned_chat_when_unchatted(self):
    initial = {
        "items": {
            "github:193514@unchatted": {
                "item_key": "github:193514",
                "kind": "github",
                "target_id": "193514",
                "url": "https://github.com/flutter/flutter/pull/193514",
                "subject": "[web_paragraph] Fix getLineBoundary soft wrap affinity",
                "status": "OPEN",
                "phase": "ALERT_CQ_FAILED",
                "patchset": 2,
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "",
                "project_id": "p-wp",
                "project_name": "WebParagraph",
                "chat_busy": False,
                "composite_key": "github:193514@unchatted",
            }
        },
        "events": [],
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[]), \
           mock.patch.object(w.cl, "resolve_project_id", return_value="p-wp"), \
           mock.patch.object(
               w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
           ), \
           mock.patch.object(
               w.cl, "start_conversation_exact", return_value="c-new-pr"
           ) as mock_start, \
           mock.patch.object(w, "wake_conversation") as mock_wake:
        res = w.rebase_item("github:193514")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertTrue(res.get("started"))
    self.assertEqual(res["conversation_id"], "c-new-pr")
    mock_wake.assert_not_called()
    mock_start.assert_called_once()
    kwargs = mock_start.call_args.kwargs
    self.assertEqual(kwargs["project_id"], "p-wp")
    self.assertIn("PR #193514", kwargs["title"])
    self.assertIn("https://github.com/flutter/flutter/pull/193514", kwargs["prompt"])
    self.assertIn("run_webparagraph_corpus.py", kwargs["prompt"])
    self.assertNotIn("github:193514@unchatted", saved["items"])
    self.assertTrue(saved["items"]["github:193514@c-new-pr"]["chat_busy"])

  def test_rebase_item_rejects_non_self_closed_or_clean_changes(self):
    initial = {
        "items": {
            "gerrit:8595771@c1": {
                "item_key": "gerrit:8595771",
                "kind": "gerrit",
                "target_id": "8595771",
                "status": "NEW",
                "is_self": False,
                "merge_conflict": True,
                "conversation_id": "c1",
            },
            "github:193773@c2": {
                "item_key": "github:193773",
                "kind": "github",
                "target_id": "193773",
                "status": "MERGED",
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "c2",
            },
            "gerrit:1404136@c3": {
                "item_key": "gerrit:1404136",
                "kind": "gerrit",
                "target_id": "1404136",
                "status": "NEW",
                "is_self": True,
                "merge_conflict": False,
                "conversation_id": "c3",
            },
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "wake_conversation") as mock_wake, \
           mock.patch.object(w.cl, "start_conversation_exact") as mock_start:
        for ikey in ("gerrit:8595771", "github:193773", "gerrit:1404136", "gerrit:999"):
          res = w.rebase_item(ikey)
          self.assertFalse(res["ok"], ikey)
      mock_wake.assert_not_called()
      mock_start.assert_not_called()

  def test_rebase_item_skips_duplicate_wake_when_chat_busy(self):
    initial = {
        "items": {
            "github:195120@c-arch": {
                "item_key": "github:195120",
                "kind": "github",
                "target_id": "195120",
                "url": "https://github.com/flutter/flutter/pull/195120",
                "subject": "[web] Lazy word segmentation",
                "status": "OPEN",
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "c-arch",
                "chat_archived": True,
                "chat_busy": False,
            }
        }
    }
    summaries = {
        "c-arch": {
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "annotations": {"title": "[12:02] » PR #195120 Lazy word", "archived": True},
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl, "discover_conversation_chain", return_value=["c-arch"]
           ), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(w, "wake_conversation") as mock_wake:
        res = w.rebase_item("github:195120")

    self.assertTrue(res["ok"])
    self.assertTrue(res.get("already_busy"))
    mock_wake.assert_not_called()

  def test_overlay_grace_window_and_running_archived_chat_busy(self):
    summaries = {
        "c-just-woken": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[16:30] ⦿ PR #195120 Lazy word", "archived": False},
            "trajectoryMetadata": {
                "projectId": "p-wp",
                "lastModifiedTime": "2026-10-29T20:30:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
        "c-arch-running": {
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "annotations": {"title": "[12:00] ⏳ PR #195122 Layout perf", "archived": True},
            "trajectoryMetadata": {
                "projectId": "p-wp",
                "lastModifiedTime": "2026-10-29T20:30:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
        "c-arch-idle-timer": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[11:00] ⏳ PR #194000 Old timer", "archived": True},
            "trajectoryMetadata": {
                "projectId": "p-wp",
                "lastModifiedTime": "2026-10-28T20:30:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
    }
    state = {
        "items": {
            "github:195120@c-just-woken": {
                "conversation_id": "c-just-woken",
                "chat_busy": True,
                "chat_woken_ts": time.time(),
            }
        }
    }
    with mock.patch.object(
        w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
    ), mock.patch.object(w, "load_project_names", return_value={"p-wp": "WebParagraph"}):
      w.overlay_live_chat_busy(state)
      discovered = w.discover_active_cl_chats(include_archived=True)

    self.assertTrue(state["items"]["github:195120@c-just-woken"]["chat_busy"])
    by_key = {d["item_key"]: d for d in discovered}
    self.assertTrue(by_key["github:195122"]["chat_busy"])
    self.assertFalse(by_key["github:194000"]["chat_busy"])

  def test_closed_github_pr_does_not_report_merge_conflict(self):
    payload = {
        "number": 190001,
        "title": "[web_paragraph] Closed draft",
        "state": "CLOSED",
        "isDraft": False,
        "author": {"login": "Rusino"},
        "mergedAt": None,
        "headRefOid": "deadbeef1234",
        "reviews": [],
        "comments": [],
        "commits": [],
        "statusCheckRollup": [],
        "url": "https://github.com/flutter/flutter/pull/190001",
        "mergeable": "CONFLICTING",
        "reviewRequests": [],
        "createdAt": "2026-10-20T10:00:00Z",
        "updatedAt": "2026-10-21T10:00:00Z",
        "closedAt": "2026-10-21T10:00:00Z",
    }
    with mock.patch.object(w.subprocess, "check_output", return_value=json.dumps(payload)):
      info = w.fetch_github_pr_status("190001")
    self.assertTrue(info["ok"])
    self.assertFalse(info["merge_conflict"])

  def test_http_post_api_rebase_routes_to_rebase_item(self):
    body = b'{"item_key": "gerrit:1408158"}'
    handler = w.FlightDeckHandler.__new__(w.FlightDeckHandler)
    handler.path = "/api/rebase"
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    sent = {}

    def capture(payload, code=200):
      sent["payload"] = payload
      sent["code"] = code

    handler._send_json = capture
    with mock.patch.object(
        w, "rebase_item", return_value={"ok": True, "conversation_id": "c-1"}
    ) as mock_rebase:
      handler.do_POST()
    mock_rebase.assert_called_once_with("gerrit:1408158")
    self.assertEqual(sent["code"], 200)
    self.assertTrue(sent["payload"]["ok"])

  def test_dashboard_renders_rebase_action_only_for_own_open_conflicts(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
const ownConflict = {
  item_key: 'gerrit:1408158', kind: 'gerrit', target_id: '1408158', status: 'NEW',
  phase: 'WAITING_ON_OTHERS', is_self: true, merge_conflict: true,
  in_my_attention: false, is_draft: false, in_development: false, is_watching: false,
  conversation_id: 'c-own', project_name: 'SkParagraph',
  subject: '[skparagraph] Use std::string', patchset: 4,
  url: 'https://skia-review.googlesource.com/c/skia/+/1408158',
  status_changed_et: '14:00 ET', status_changed_age: '2h ago', chat_busy: false
};
const ownConflictCqFailed = Object.assign({}, ownConflict, {
  item_key: 'github:195122', kind: 'github', target_id: '195122', status: 'OPEN',
  phase: 'ALERT_CQ_FAILED', conversation_id: '', chat_archived: false,
  url: 'https://github.com/flutter/flutter/pull/195122'
});
const ownConflictBusy = Object.assign({}, ownConflict, {chat_busy: true});
const otherConflictAttention = Object.assign({}, ownConflict, {
  item_key: 'gerrit:8595771', target_id: '8595771', is_self: false,
  in_my_attention: true, phase: 'NEEDS_YOUR_ACTION',
  last_msg_author: 'author@chromium.org', last_msg_age: '10m ago',
  last_msg_snippet: 'PTAL at the updated API'
});
const otherConflictWatching = Object.assign({}, ownConflict, {
  item_key: 'gerrit:1382237', target_id: '1382237', is_self: false,
  is_watching: true
});
console.log(JSON.stringify({
  ownLane: laneOf(ownConflict),
  ownCard: focusCard(ownConflict, box),
  cqCard: focusCard(ownConflictCqFailed, box),
  busyCard: focusCard(ownConflictBusy, box),
  otherAttCard: focusCard(otherConflictAttention, box),
  otherWatchRow: rowHtml(otherConflictWatching, box),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["ownLane"], "you")
    self.assertIn("🔧 Rebase", out["ownCard"])
    self.assertIn("rebaseItem(", out["ownCard"])
    self.assertIn("Open CL", out["ownCard"])
    self.assertIn("Open chat", out["ownCard"])

    self.assertIn("🔧 Rebase", out["cqCard"])
    self.assertIn("Merge conflict: rebase needed", out["cqCard"])
    self.assertIn("Checks failed", out["cqCard"])
    self.assertIn("Open PR", out["cqCard"])
    self.assertIn("Start chat", out["cqCard"])

    self.assertIn("disabled", out["busyCard"])

    self.assertNotIn("🔧 Rebase", out["otherAttCard"])
    self.assertNotIn("rebaseItem(", out["otherAttCard"])
    self.assertIn("PTAL at the updated API", out["otherAttCard"])
    self.assertIn("Open CL", out["otherAttCard"])

    self.assertNotIn("rebaseItem(", out["otherWatchRow"])
    self.assertNotIn("🔧 rebase", out["otherWatchRow"])

  def test_sync_once_preserves_rekeyed_handoff_chain_tail(self):
    initial = {
        "items": {
            "gerrit:1408158@c-tail": {
                "item_key": "gerrit:1408158",
                "kind": "gerrit",
                "target_id": "1408158",
                "status": "NEW",
                "phase": "WAITING_ON_OTHERS",
                "patchset": 4,
                "last_msg_date": "2026-10-29 19:00:00.000000000",
                "conversation_id": "c-tail",
                "title": "[14:00] ⦿ SkParagraph string refactor",
                "chat_archived": False,
                "chat_busy": True,
                "chat_woken_ts": time.time(),
                "composite_key": "gerrit:1408158@c-tail",
            }
        },
        "events": [],
    }
    summaries = {
        "c-root": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {
                "title": "[10:00] ⦿ cl/1408158 Use std::string",
                "archived": True,
            },
            "trajectoryMetadata": {
                "projectId": "p-skp",
                "lastModifiedTime": "2026-10-29T14:00:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
        "c-tail": {
            "status": "CASCADE_RUN_STATUS_RUNNING",
            "annotations": {
                "title": "[14:00] ⦿ SkParagraph string refactor",
                "archived": False,
            },
            "trajectoryMetadata": {
                "projectId": "p-skp",
                "lastModifiedTime": "2026-10-29T18:00:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        },
    }
    remote = [{
        "item_key": "gerrit:1408158",
        "kind": "gerrit",
        "target_id": "1408158",
        "preferred_host": "skia-review.googlesource.com",
        "conversation_id": "",
        "title": "",
        "project_id": "p-skp",
        "project_name": "SkParagraph",
        "chat_last_modified": "",
        "single_target": True,
        "chat_busy": False,
    }]
    gerrit_info = {
        "ok": True,
        "host": "skia-review.googlesource.com",
        "url": "https://skia-review.googlesource.com/c/skia/+/1408158",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "subject": "[skparagraph] Use std::string",
        "owner_name": "Julia Lavrova",
        "owner_email": "jlavrova@google.com",
        "role": "author",
        "is_self": True,
        "is_external": False,
        "is_draft": False,
        "in_development": False,
        "is_watching": False,
        "reviewer_removed": False,
        "patchset": 4,
        "revision": "rev4444",
        "in_my_attention": False,
        "waiting_on": [],
        "merge_conflict": True,
        "tree_red": False,
        "cr_summary": "+1",
        "cq_status": "",
        "cq_failed": False,
        "last_msg_author": "jlavrova@google.com",
        "last_msg_date": "2026-10-29 19:00:00.000000000",
        "last_msg_et": "15:00 ET",
        "last_msg_age": "5m ago",
        "last_msg_snippet": "Patch Set 4",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "status_changed_raw": "2026-10-29 19:00:00.000000000",
        "status_changed_et": "15:00 ET",
        "status_changed_age": "5m ago",
        "post_submit": None,
    }
    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(
               w.cl,
               "discover_conversation_chain",
               side_effect=lambda cid, include_descendants=True: (
                   ["c-root", "c-tail"] if cid == "c-root" else [cid]
               ),
           ), \
           mock.patch.object(w, "load_project_names", return_value={"p-skp": "SkParagraph"}), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=remote), \
           mock.patch.object(w, "fetch_gerrit_cl_status", return_value=gerrit_info), \
           mock.patch.object(w, "fetch_gerrit_related_cl_ids", return_value=[]), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail):
        state = w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    self.assertNotIn("gerrit:1408158@c-root", state["items"])
    self.assertIn("gerrit:1408158@c-tail", state["items"])
    rec = state["items"]["gerrit:1408158@c-tail"]
    self.assertEqual(rec["conversation_id"], "c-tail")
    self.assertFalse(rec["chat_archived"])
    self.assertTrue(rec["chat_busy"])

  def test_rebase_item_honors_wake_grace_window_on_repeat_post(self):
    initial = {
        "items": {
            "gerrit:1408158@c-tail": {
                "item_key": "gerrit:1408158",
                "kind": "gerrit",
                "target_id": "1408158",
                "url": "https://skia-review.googlesource.com/c/skia/+/1408158",
                "subject": "[skparagraph] Use std::string",
                "status": "NEW",
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "c-tail",
                "chat_archived": False,
                "chat_busy": False,
                "composite_key": "gerrit:1408158@c-tail",
            }
        },
        "events": [],
    }
    summaries = {
        "c-tail": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[14:00] ⦿ cl/1408158 Use std::string", "archived": False},
        }
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl, "discover_conversation_chain", return_value=["c-tail"]
           ), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake:
        first = w.rebase_item("gerrit:1408158")
        second = w.rebase_item("gerrit:1408158")

    self.assertTrue(first["ok"])
    self.assertTrue(first.get("woken"))
    self.assertTrue(second["ok"])
    self.assertTrue(second.get("already_busy"))
    mock_wake.assert_called_once()

  def test_rebase_item_refreshes_chat_archived_from_chain_tail_summary(self):
    initial = {
        "items": {
            "gerrit:1408158@c-root": {
                "item_key": "gerrit:1408158",
                "kind": "gerrit",
                "target_id": "1408158",
                "url": "https://skia-review.googlesource.com/c/skia/+/1408158",
                "subject": "[skparagraph] Use std::string",
                "status": "NEW",
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "c-root",
                "chat_archived": True,
                "chat_busy": False,
                "composite_key": "gerrit:1408158@c-root",
            }
        },
        "events": [],
    }
    summaries = {
        "c-root": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[10:00] ⦿ cl/1408158 Use std::string", "archived": True},
        },
        "c-tail": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[14:00] ⦿ SkParagraph refactor", "archived": False},
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(
               w.cl, "discover_conversation_chain", return_value=["c-root", "c-tail"]
           ), \
           mock.patch.object(
               w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(w, "wake_conversation", return_value=True):
        res = w.rebase_item("gerrit:1408158")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertFalse(saved["items"]["gerrit:1408158@c-tail"]["chat_archived"])

  def test_rebase_item_discovers_companion_pr_parent_chat(self):
    initial = {
        "items": {
            "github:195200@unchatted": {
                "item_key": "github:195200",
                "kind": "github",
                "target_id": "195200",
                "url": "https://github.com/flutter/flutter/pull/195200",
                "subject": "[web_paragraph] companion to #195122: test coverage",
                "status": "OPEN",
                "is_self": True,
                "merge_conflict": True,
                "conversation_id": "",
                "chat_busy": False,
                "composite_key": "github:195200@unchatted",
            }
        },
        "events": [],
    }
    parent_chats = [{
        "item_key": "github:195122",
        "conversation_id": "c-parent",
        "chat_archived": False,
    }]
    with tempfile.TemporaryDirectory() as tmp:
      state_path = self._write_state(tmp, initial)
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=parent_chats), \
           mock.patch.object(
               w.cl, "discover_conversation_chain", return_value=["c-parent"]
           ), \
           mock.patch.object(w.cl, "_fetch_ls_summaries_for_audit", return_value={}), \
           mock.patch.object(
               w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
           ), \
           mock.patch.object(w, "wake_conversation", return_value=True) as mock_wake, \
           mock.patch.object(
               w.cl, "start_conversation_exact", return_value="c-wrong"
           ) as mock_start:
        res = w.rebase_item("github:195200")
        with open(state_path, encoding="utf-8") as sf:
          saved = json.load(sf)

    self.assertTrue(res["ok"])
    self.assertEqual(res["conversation_id"], "c-parent")
    mock_start.assert_not_called()
    mock_wake.assert_called_once()
    rec = saved["items"]["github:195200@c-parent"]
    self.assertEqual(rec["companion_to"], "github:195122")
    self.assertFalse(rec["chat_archived"])

  def test_removed_reviewer_conflict_routes_to_watching_lane(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const removedReviewerConflict = {
  item_key: 'gerrit:8599999', kind: 'gerrit', target_id: '8599999', status: 'NEW',
  phase: 'WAITING_ON_OTHERS', is_self: false, reviewer_removed: true,
  merge_conflict: true, in_my_attention: false, is_draft: false,
  in_development: false, is_watching: false
};
console.log(JSON.stringify({
  status: statusOf(removedReviewerConflict),
  lane: laneOf(removedReviewerConflict),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())
    self.assertEqual(out["status"], "watching")
    self.assertEqual(out["lane"], "watching")


if __name__ == "__main__":
  unittest.main()
