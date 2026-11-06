#!/usr/bin/env python3
"""Unit tests for R89: merge-time bug auto-assignment, conflict prompt, and Bug: footer guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(CONFIG_DIR, "scripts")
HOOKS_DIR = os.path.join(CONFIG_DIR, "hooks")
for _d in (SCRIPTS_DIR, HOOKS_DIR):
  if _d not in sys.path:
    sys.path.insert(0, _d)

import bug_actions  # pylint: disable=g-import-not-at-top
import cl_watcher as w  # pylint: disable=g-import-not-at-top
import entity_state  # pylint: disable=g-import-not-at-top


class BugAssignAndFooterTest(unittest.TestCase):

  def test_extract_bug_refs_and_detect_malformed_gerrit_footers(self) -> None:
    msg_colon = (
        "[SkParagraph] Fix emojis with strut style\n\n"
        "Bug: flutter:110875\n"
        "Change-Id: I1234567890abcdef\n"
    )
    # Colon syntax must still resolve to the GitHub issue key so watcher links the bug
    refs = w.extract_bug_refs_from_text(msg_colon, default_tracker="buganizer")
    self.assertEqual(refs, ["gh_issue:flutter/flutter#110875"])

    malformed = w.detect_malformed_bug_footers(msg_colon, kind="gerrit")
    self.assertEqual(len(malformed), 1)
    self.assertEqual(malformed[0]["raw"], "flutter:110875")
    self.assertEqual(
        malformed[0]["suggested"],
        "https://github.com/flutter/flutter/issues/110875",
    )

    msg_hash = "Subject\n\nFixed: flutter#110875, #123456\n"
    malformed_hash = w.detect_malformed_bug_footers(msg_hash, kind="gerrit")
    self.assertEqual(
        [m["raw"] for m in malformed_hash],
        ["flutter#110875", "#123456"],
    )

    msg_valid = (
        "Subject\n\n"
        "Bug: https://github.com/flutter/flutter/issues/110875\n"
        "Bug: b/466929543, 396452388\n"
    )
    self.assertEqual(w.detect_malformed_bug_footers(msg_valid, kind="gerrit"), [])
    self.assertEqual(w.detect_malformed_bug_footers(msg_colon, kind="github"), [])

  def test_link_bugs_and_classify_flags_unassigned_bug_on_own_cl(self) -> None:
    bugs = {
        "gh_issue:flutter/flutter#110875": {
            "bug_key": "gh_issue:flutter/flutter#110875",
            "source": "github",
            "id": "110875",
            "short_id": "flutter#110875",
            "title": "Emojis with strut style are clipped",
            "status": "OPEN",
            "priority": "P2",
            "assignee": "",
            "url": "https://github.com/flutter/flutter/issues/110875",
        }
    }
    items = {
        "gerrit:1418478@cid1": {
            "item_key": "gerrit:1418478",
            "kind": "gerrit",
            "target_id": "1418478",
            "subject": "[SkParagraph] Fix emojis with strut style",
            "commit_message": (
                "[SkParagraph] Fix emojis with strut style\n\n"
                "Bug: flutter:110875\n"
            ),
            "status": "NEW",
            "is_self": True,
            "conversation_id": "cid1",
            "url": "https://skia-review.googlesource.com/c/skia/+/1418478",
        }
    }
    # Open/WIP self CL must NOT trigger needs_self_assign before landing
    w.link_bugs_and_cls(bugs, items)
    bug = bugs["gh_issue:flutter/flutter#110875"]
    cl_rec = items["gerrit:1418478@cid1"]
    self.assertFalse(bug.get("needs_self_assign"))
    self.assertEqual(len(cl_rec.get("malformed_bug_footers") or []), 1)

    # Once the 1:1 self CL merges, needs_self_assign activates without conflict
    cl_rec["status"] = "MERGED"
    cl_rec["submitted_ts"] = 1700000100.0
    w.link_bugs_and_cls(bugs, items)
    self.assertTrue(bug.get("needs_self_assign"))
    self.assertFalse(bug.get("self_assign_conflict"))
    self.assertEqual(bug.get("assign_target"), "Rusino")

    w.classify_bug_status(bug, now_ts=1700000200.0)
    self.assertTrue(bug["needs_you"])
    self.assertEqual(bug["lane"], "you")
    self.assertIn("assign flutter#110875 to Rusino", bug["why_needs_you"])

    bug_es = entity_state.compute_bug_entity_state(bug)
    self.assertIn("assign_bug", bug_es["available_actions"])
    self.assertNotIn("add_bug_collaborator", bug_es["available_actions"])

    # If another owner holds the bug, self_assign_conflict is set and collaborator action appears
    bug["assignee"] = "other_dev"
    w.link_bugs_and_cls(bugs, items)
    self.assertTrue(bug.get("needs_self_assign"))
    self.assertTrue(bug.get("self_assign_conflict"))
    self.assertIn("owner: other_dev", bug.get("self_assign_conflict_reason", ""))
    w.classify_bug_status(bug, now_ts=1700000200.0)
    self.assertTrue(bug["needs_you"])
    self.assertIn("shared issue", bug["why_needs_you"])
    conflict_es = entity_state.compute_bug_entity_state(bug)
    self.assertIn("assign_bug", conflict_es["available_actions"])
    self.assertIn("add_bug_collaborator", conflict_es["available_actions"])

    # Once Rusino is added as co-assignee, needs_self_assign clears
    bug["assignee"] = "other_dev, Rusino"
    w.link_bugs_and_cls(bugs, items)
    self.assertFalse(bug.get("needs_self_assign"))
    self.assertFalse(bug.get("self_assign_conflict"))

  def test_sync_bugs_bypasses_ttl_when_self_cl_references_uncached_bug(self) -> None:
    now_ts = 1700000000.0
    state = {
        "last_bug_sync_ts": now_ts - 30.0,
        "bugs": {},
        "items": {
            "gerrit:1418478@cid1": {
                "item_key": "gerrit:1418478",
                "kind": "gerrit",
                "target_id": "1418478",
                "subject": "[SkParagraph] Fix strut",
                "commit_message": "Fix\n\nBug: flutter:110875\n",
                "status": "MERGED",
                "submitted_ts": now_ts - 10.0,
                "is_self": True,
                "conversation_id": "cid1",
            },
            "gerrit:1419999@cid2": {
                "item_key": "gerrit:1419999",
                "kind": "gerrit",
                "target_id": "1419999",
                "subject": "[SkParagraph] Shared bug fix",
                "commit_message": "Fix\n\nBug: b/466929543\n",
                "status": "MERGED",
                "submitted_ts": now_ts - 10.0,
                "is_self": True,
                "conversation_id": "cid2",
            },
        },
    }
    # Even if GitHub auto-closed the issue on merge, clean unowned bug gets auto-assigned
    fake_gh_bug = {
        "bug_key": "gh_issue:flutter/flutter#110875",
        "source": "github",
        "id": "110875",
        "short_id": "flutter#110875",
        "title": "Emojis with strut style",
        "status": "CLOSED",
        "state_reason": "COMPLETED",
        "priority": "P2",
        "assignee": "",
        "url": "https://github.com/flutter/flutter/issues/110875",
    }
    # Shared bug owned by another engineer must NOT be auto-assigned; surfaces conflict in Needs You
    fake_bz_conflict_bug = {
        "bug_key": "buganizer:466929543",
        "source": "buganizer",
        "id": "466929543",
        "short_id": "b/466929543",
        "title": "Shared paragraph bug",
        "status": "ASSIGNED",
        "priority": "P2",
        "assignee": "colleague@google.com",
        "url": "https://b.corp.google.com/issues/466929543",
    }

    def fake_assign_single(bkey: str, mode: str = "assignee"):
      self.assertEqual(bkey, "gh_issue:flutter/flutter#110875")
      self.assertEqual(mode, "assignee")
      return {
          "ok": True,
          "bug_key": bkey,
          "source": "github",
          "id": "110875",
          "mode": "assignee",
          "assignee": "Rusino",
      }

    with (
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["", ""]),
        mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]),
        mock.patch.object(
            w,
            "parse_buganizer_blocks",
            side_effect=[[], [], [fake_bz_conflict_bug]],
        ),
        mock.patch.object(
            w, "fetch_github_bugs", return_value=[fake_gh_bug]
        ) as mock_gh,
        mock.patch.object(
            w, "_assign_single_bug_to_self", side_effect=fake_assign_single
        ) as mock_auto_assign,
        mock.patch.object(w, "evaluate_security_merge_groups", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=False, wake_chats=False, proj_names={})
      mock_gh.assert_called_once()
      self.assertIn("110875", mock_gh.call_args.kwargs.get("extra_issue_nums", set()))
      mock_auto_assign.assert_called_once_with(
          "gh_issue:flutter/flutter#110875", mode="assignee"
      )
      self.assertIn("gh_issue:flutter/flutter#110875", state.get("auto_assigned_bugs", {}))
      self.assertTrue(
          any(
              "Auto-assigned" in str(ev.get("message") or "")
              for ev in state.get("events", [])
          )
      )
      # Conflict bug stays in state["bugs"] with self_assign_conflict=True and Needs You
      bz_bug = state["bugs"]["buganizer:466929543"]
      self.assertTrue(bz_bug.get("needs_self_assign"))
      self.assertTrue(bz_bug.get("self_assign_conflict"))
      self.assertTrue(bz_bug.get("needs_you"))
      self.assertEqual(bz_bug.get("lane"), "you")

  def test_assign_bug_to_self_github_and_buganizer(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      state_file = os.path.join(tmp, "state.json")
      initial_state = {
          "bugs": {
              "gh_issue:flutter/flutter#110875": {
                  "bug_key": "gh_issue:flutter/flutter#110875",
                  "source": "github",
                  "id": "110875",
                  "short_id": "flutter#110875",
                  "title": "Emojis with strut style",
                  "status": "OPEN",
                  "priority": "P2",
                  "assignee": "",
                  "needs_self_assign": True,
                  "assign_target": "Rusino",
                  "url": "https://github.com/flutter/flutter/issues/110875",
              },
              "buganizer:466929543": {
                  "bug_key": "buganizer:466929543",
                  "source": "buganizer",
                  "id": "466929543",
                  "short_id": "b/466929543",
                  "title": "Paragraph layout issue",
                  "status": "ASSIGNED",
                  "priority": "P2",
                  "assignee": "colleague@google.com",
                  "needs_self_assign": True,
                  "self_assign_conflict": True,
                  "self_assign_conflict_reason": "owner: colleague",
                  "assign_target": "jlavrova@google.com",
                  "url": "https://b.corp.google.com/issues/466929543",
              },
          },
          "items": {
              "gerrit:1418478@cid1": {
                  "item_key": "gerrit:1418478",
                  "kind": "gerrit",
                  "target_id": "1418478",
                  "subject": "Fix",
                  "commit_message": (
                      "Fix\n\n"
                      "Bug: https://github.com/flutter/flutter/issues/110875\n"
                  ),
                  "status": "MERGED",
                  "submitted_ts": 1700000100.0,
                  "is_self": True,
              },
              "gerrit:1419999@cid2": {
                  "item_key": "gerrit:1419999",
                  "kind": "gerrit",
                  "target_id": "1419999",
                  "subject": "Fix 2",
                  "commit_message": "Fix 2\n\nBug: b/466929543\n",
                  "status": "MERGED",
                  "submitted_ts": 1700000100.0,
                  "is_self": True,
              },
          },
      }
      with open(state_file, "w", encoding="utf-8") as f:
        json.dump(initial_state, f)

      def fake_run(cmd, **kwargs):
        del kwargs
        if "edit" in cmd:
          return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if "view" in cmd:
          return subprocess.CompletedProcess(
              cmd,
              0,
              stdout=json.dumps({"assignees": [{"login": "Rusino"}]}),
              stderr="",
          )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="unexpected")

      with (
          mock.patch.object(w, "STATE_FILE", state_file),
          mock.patch.object(w.subprocess, "run", side_effect=fake_run),
          mock.patch.object(
              w,
              "call_buganizer_mcp_batch",
              return_value=["OK"],
          ) as mock_mcp,
      ):
        res_gh = bug_actions.assign_bug_to_self("gh_issue:flutter/flutter#110875")
        self.assertTrue(res_gh["ok"])
        self.assertEqual(res_gh["assignee"], "Rusino")

        # Collaborator mode on conflict Buganizer issue calls safe_add_collaborator
        res_bz = bug_actions.assign_bug_to_self(
            "buganizer:466929543", mode="collaborator"
        )
        self.assertTrue(res_bz["ok"])
        self.assertTrue(res_bz["is_collaborator"])
        mock_mcp.assert_called_once_with([
            (
                "safe_add_collaborator",
                {"issueId": "466929543", "collaborator": "jlavrova@google.com"},
            )
        ])

        saved = w.load_watcher_state()
        self.assertFalse(saved["bugs"]["gh_issue:flutter/flutter#110875"]["needs_self_assign"])
        self.assertEqual(saved["bugs"]["gh_issue:flutter/flutter#110875"]["assignee"], "Rusino")
        self.assertFalse(saved["bugs"]["buganizer:466929543"]["needs_self_assign"])
        self.assertFalse(saved["bugs"]["buganizer:466929543"]["self_assign_conflict"])
        self.assertTrue(saved["bugs"]["buganizer:466929543"]["is_collaborator"])
        self.assertEqual(
            saved["bugs"]["buganizer:466929543"]["assignee"], "colleague@google.com"
        )


if __name__ == "__main__":
  unittest.main()
