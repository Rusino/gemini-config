#!/usr/bin/env python3
"""Unit tests for R80 Phase 1 fixes in cl_watcher.py, bug_actions.py, and prepare-commit-msg."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import urllib.error

SCRIPTS_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts"
)
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)

import bug_actions  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top


class R80Phase1FixesTest(unittest.TestCase):

  def test_dashboard_html_has_no_unrequested_search_box(self):
    self.assertNotIn('id="search-box"', w.DASHBOARD_HTML)
    self.assertNotIn("updateSearchQuery", w.DASHBOARD_HTML)

  def test_locks_are_reentrant(self):
    self.assertEqual(type(w._SYNC_LOCK).__name__, "RLock")
    self.assertEqual(type(w._STATE_IO_LOCK).__name__, "RLock")

  def test_merge_concurrent_state_mutations_preserves_ack_reassign_and_rekey(self):
    baseline = {
        "items": {
            "gerrit:1408158@unchatted": {
                "item_key": "gerrit:1408158",
                "composite_key": "gerrit:1408158@unchatted",
                "conversation_id": "",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "self_approved": False,
                "chat_busy": False,
            },
            "gerrit:1410000@c1": {
                "item_key": "gerrit:1410000",
                "composite_key": "gerrit:1410000@c1",
                "conversation_id": "c1",
                "status": "NEW",
                "phase": "WAITING_ON_REVIEWERS",
                "self_approved": False,
            },
        },
        "bugs": {
            "buganizer:5001": {
                "id": "5001",
                "bug_key": "buganizer:5001",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": True,
                "acked_ts": 100.0,
                "modified_ts": 200.0,
                "last_human_comment_at": "2026-11-04 10:00:00 UTC",
                "last_human_comment_by": "reviewer@google.com",
            },
            "buganizer:5002": {
                "id": "5002",
                "bug_key": "buganizer:5002",
                "title": "[Merge M144] Fix OOB",
                "component_id": "1579812",
                "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
                "parent_issue_id": "5000",
                "merge_status": "Pending",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": False,
            },
        },
        "bug_acks": {"buganizer:5001": 100.0},
        "confirmed_merge_bugs": {},
    }

    # Concurrent mutations written to disk while sync_once was polling
    disk_during_sync = {
        "items": {
            "gerrit:1408158@new-cid": {
                "item_key": "gerrit:1408158",
                "composite_key": "gerrit:1408158@new-cid",
                "conversation_id": "new-cid",
                "title": "Gerrit 1408158 test",
                "status": "NEW",
                "phase": "NEEDS_YOUR_ACTION",
                "self_approved": False,
                "chat_busy": True,
            },
            "gerrit:1410000@c1": {
                "item_key": "gerrit:1410000",
                "composite_key": "gerrit:1410000@c1",
                "conversation_id": "c1",
                "status": "ABANDONED",
                "phase": "ABANDONED",
                "self_approved": True,
            },
        },
        "bugs": {
            "buganizer:5001": {
                "id": "5001",
                "bug_key": "buganizer:5001",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
                "is_collaborator": True,
                "acked_ts": 9999999999.0,
                "modified_ts": 200.0,
            },
            "buganizer:5002": {
                "id": "5002",
                "bug_key": "buganizer:5002",
                "title": "[Merge M144] Fix OOB",
                "component_id": "1579812",
                "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
                "parent_issue_id": "5000",
                "merge_status": "Pending",
                "status": "ASSIGNED",
                "assignee": "merges@chromium.org",
                "is_collaborator": True,
            },
        },
        "bug_acks": {"buganizer:5001": 9999999999.0},
        "confirmed_merge_bugs": {"5000": True},
    }

    # New state computed by sync_once from stale baseline
    new_state = json.loads(json.dumps(baseline))

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path):
        w.save_watcher_state(disk_during_sync)
        w.merge_concurrent_state_mutations(new_state, baseline)

    self.assertEqual(new_state["bug_acks"]["buganizer:5001"], 9999999999.0)
    self.assertEqual(
        new_state["bugs"]["buganizer:5001"]["acked_ts"], 9999999999.0
    )
    self.assertEqual(
        new_state["bugs"]["buganizer:5002"]["assignee"], "merges@chromium.org"
    )
    self.assertTrue(new_state["bugs"]["buganizer:5002"]["is_collaborator"])
    self.assertTrue(new_state["confirmed_merge_bugs"].get("5000"))
    self.assertNotIn("gerrit:1408158@unchatted", new_state["items"])
    self.assertIn("gerrit:1408158@new-cid", new_state["items"])
    self.assertTrue(new_state["items"]["gerrit:1408158@new-cid"]["chat_busy"])
    self.assertEqual(
        new_state["items"]["gerrit:1410000@c1"]["status"], "ABANDONED"
    )
    self.assertTrue(new_state["items"]["gerrit:1410000@c1"]["self_approved"])

  def test_fetch_gerrit_cl_status_skia_404_does_not_fall_back_to_chromium(self):
    probed = []

    def fake_urlopen_404(req, timeout=8):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      probed.append(url)
      raise urllib.error.HTTPError(url, 404, "Not Found", {}, io.BytesIO(b""))

    with mock.patch.object(
        w.urllib.request, "urlopen", side_effect=fake_urlopen_404
    ):
      res = w.fetch_gerrit_cl_status(
          "1404136",
          {},
          [],
          skia_dir="/nonexistent",
          preferred_host="skia-review.googlesource.com",
      )

    self.assertFalse(res["ok"])
    self.assertEqual(len(probed), 1)
    self.assertIn("skia-review.googlesource.com", probed[0])

  def test_link_bugs_and_cls_and_classify_bug_status_created_ts_fallback(self):
    bugs = {
        "buganizer:4001": {
            "id": "4001",
            "bug_key": "buganizer:4001",
            "status": "ASSIGNED",
            "assignee": "jlavrova@google.com",
            "is_collaborator": False,
            "last_human_comment_at": "2026-11-01 10:00:00 UTC",
            "last_human_comment_author": "reporter@google.com",
            "gerrit_cl_ids": ["1414016"],
        }
    }
    items = {
        "gerrit:1414016@c1": {
            "item_key": "gerrit:1414016",
            "kind": "gerrit",
            "target_id": "1414016",
            "status": "NEW",
            "is_self": True,
            "is_wip": False,
            "is_draft": False,
            "created": "2026-11-02 12:00:00.000000000",
            "activity_ts": 1793620800.0,
        }
    }
    w.link_bugs_and_cls(bugs, items)
    linked = bugs["buganizer:4001"]["linked_cls"]
    self.assertEqual(len(linked), 1)
    self.assertGreater(linked[0]["created_ts"], 0.0)

    # Even if created_ts is cleared to 0, activity_ts fallback acknowledges the earlier comment
    linked[0]["created_ts"] = 0.0
    w.classify_bug_status(bugs["buganizer:4001"])
    self.assertNotEqual(bugs["buganizer:4001"].get("lane"), "you")

  def test_reassign_security_merge_batch_tolerates_phase3_readback_timeout(self):
    bugs = {
        "buganizer:6001": {
            "id": "6001",
            "bug_key": "buganizer:6001",
            "title": "[Merge M144] Security fix",
            "component_id": "1579812",
            "reporter": "chrome-release-blintz-continuous-runner@prod.google.com",
            "parent_issue_id": "6000",
            "merge_status": "Pending",
            "status": "ASSIGNED",
            "assignee": "jlavrova@google.com",
            "is_collaborator": False,
        }
    }
    # Phase 1 succeeds, Phase 2 succeeds, Phase 3 read-back returns empty strings on timeout
    side_effects = [
        ["ok comment", "ok collab"],
        ["ok reassigned"],
        ["", ""],
    ]
    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=side_effects
    ):
      res = w.reassign_security_merge_batch(bugs, ["6001"])

    self.assertTrue(res["ok"])
    self.assertEqual(res["reassigned_ids"], ["6001"])
    self.assertEqual(res["failed_ids"], [])
    self.assertEqual(
        bugs["buganizer:6001"]["assignee"], "merges@chromium.org"
    )
    self.assertTrue(bugs["buganizer:6001"]["is_collaborator"])

  def test_bug_actions_scope_and_validation(self):
    # 1. close_fixed rejects empty or non-Gerrit URL
    bad_close = bug_actions.close_bug_with_fixed_by("576664769", "")
    self.assertFalse(bad_close["ok"])
    bad_url = bug_actions.close_bug_with_fixed_by(
        "576664769", "https://example.com/123"
    )
    self.assertFalse(bad_url["ok"])

    # Valid close_fixed
    with mock.patch.object(
        w,
        "call_buganizer_mcp_batch",
        return_value=[
            "ok field",
            "ok status",
            "Issue 576664769 status: FIXED https://skia-review.googlesource.com/c/skia/+/1404196",
        ],
    ):
      ok_close = bug_actions.close_bug_with_fixed_by(
          "576664769", "https://skia-review.googlesource.com/c/skia/+/1404196"
      )
    self.assertTrue(ok_close["ok"])
    self.assertEqual(ok_close["status"], "FIXED")

    # 2. render enforces authorization scope and wraps in <untrusted_bug_content>
    with mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=["assignee: someone@else.com"]
    ):
      denied = bug_actions.render_issue_with_external_auth(
          "999999", state={"bugs": {}}
      )
    self.assertFalse(denied["ok"])
    self.assertIn("not in Flight Deck state", denied["error"])

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=["Public bug description"]
    ):
      allowed = bug_actions.render_issue_with_external_auth(
          "466929543",
          state={"bugs": {"buganizer:466929543": {"id": "466929543"}}},
      )
    self.assertTrue(allowed["ok"])
    self.assertIn("<untrusted_bug_content", allowed["content"])
    self.assertIn("Public bug description", allowed["content"])

    # 3. reassign_merge blocks when merge_request_groups gates failed
    fake_state = {
        "bugs": {
            "buganizer:7001": {
                "id": "7001",
                "parent_issue_id": "7000",
            }
        },
        "merge_request_groups": {
            "7000": {
                "all_gates_passed": False,
                "failed_gates": ["24h Canary soak pending"],
            }
        },
    }
    with mock.patch.object(w, "load_watcher_state", return_value=fake_state):
      blocked = bug_actions.reassign_security_merge("7001")
    self.assertFalse(blocked["ok"])
    self.assertIn("Pre-gates not satisfied", blocked["error"])

  def test_prepare_commit_msg_hook_inserts_assisted_by_before_change_id(self):
    hook_path = os.path.expanduser(
        "~/.gemini/config/githooks/prepare-commit-msg"
    )
    with tempfile.TemporaryDirectory() as tmp:
      msg_file = os.path.join(tmp, "COMMIT_EDITMSG")
      with open(msg_file, "w", encoding="utf-8") as f:
        f.write(
            "Fix paragraph layout edge case\n\nChange-Id: I1234567890abcdef\n"
        )
      subprocess.run([hook_path, msg_file, "message"], cwd=tmp, check=True)
      with open(msg_file, "r", encoding="utf-8") as f:
        out = f.read()
    self.assertIn("Assisted-by:", out)
    self.assertLess(out.index("Assisted-by:"), out.index("Change-Id:"))


if __name__ == "__main__":
  unittest.main()
