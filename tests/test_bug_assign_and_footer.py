#!/usr/bin/env python3
"""Unit tests for R89: auto-detect & assign linked bugs on own CLs/PRs and Bug: footer guard."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

CONFIG_DIR = os.path.expanduser("~/.gemini/config")
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
    w.link_bugs_and_cls(bugs, items)
    bug = bugs["gh_issue:flutter/flutter#110875"]
    self.assertTrue(bug.get("needs_self_assign"))
    self.assertEqual(bug.get("assign_target"), "Rusino")

    w.classify_bug_status(bug, now_ts=1700000000.0)
    self.assertTrue(bug["needs_you"])
    self.assertEqual(bug["lane"], "you")
    self.assertIn("assign flutter#110875 to Rusino", bug["why_needs_you"])

    cl_rec = items["gerrit:1418478@cid1"]
    self.assertEqual(len(cl_rec.get("malformed_bug_footers") or []), 1)
    self.assertTrue(cl_rec["linked_bugs"][0].get("needs_self_assign"))
    self.assertEqual(cl_rec["linked_bugs"][0].get("assign_target"), "Rusino")

    bug_es = entity_state.compute_bug_entity_state(bug)
    self.assertIn("assign_bug", bug_es["available_actions"])
    cl_actions = entity_state.available_actions_of(cl_rec)
    self.assertIn("assign_bug", cl_actions)

    # Once assigned to Rusino, needs_self_assign clears
    bug["assignee"] = "Rusino"
    w.link_bugs_and_cls(bugs, items)
    self.assertFalse(bug.get("needs_self_assign"))
    w.classify_bug_status(bug, now_ts=1700000000.0)
    self.assertFalse(bug["needs_you"])
    self.assertEqual(bug["lane"], "has_cl")

  def test_sync_bugs_bypasses_ttl_when_self_cl_references_uncached_bug(self) -> None:
    now_ts = 1700000000.0
    state = {
        "last_bug_sync_ts": now_ts - 30.0,
        "bugs": {
            "buganizer:11111111": {
                "bug_key": "buganizer:11111111",
                "source": "buganizer",
                "id": "11111111",
                "short_id": "b/11111111",
                "title": "Existing bug",
                "status": "ASSIGNED",
                "assignee": "jlavrova@google.com",
            }
        },
        "items": {
            "gerrit:1418478@cid1": {
                "item_key": "gerrit:1418478",
                "kind": "gerrit",
                "target_id": "1418478",
                "subject": "[SkParagraph] Fix strut",
                "commit_message": "Fix\n\nBug: flutter:110875\n",
                "status": "NEW",
                "is_self": True,
                "conversation_id": "cid1",
            }
        },
    }
    fake_gh_bug = {
        "bug_key": "gh_issue:flutter/flutter#110875",
        "source": "github",
        "id": "110875",
        "short_id": "flutter#110875",
        "title": "Emojis with strut style",
        "status": "OPEN",
        "priority": "P2",
        "assignee": "",
        "url": "https://github.com/flutter/flutter/issues/110875",
    }
    with (
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["", ""]),
        mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]),
        mock.patch.object(w, "fetch_github_bugs", return_value=[fake_gh_bug]) as mock_gh,
        mock.patch.object(w, "evaluate_security_merge_groups", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=False, wake_chats=False, proj_names={})
      mock_gh.assert_called_once()
      self.assertIn("110875", mock_gh.call_args.kwargs.get("extra_issue_nums", set()))
      self.assertIn("gh_issue:flutter/flutter#110875", state["bugs"])
      self.assertTrue(state["bugs"]["gh_issue:flutter/flutter#110875"].get("needs_self_assign"))

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
                  "status": "NEW",
                  "priority": "P2",
                  "assignee": "",
                  "needs_self_assign": True,
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
                      "Bug: b/466929543\n"
                  ),
                  "status": "NEW",
                  "is_self": True,
              }
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
              return_value=[
                  "OK",
                  "Issue 466929543\nAssignee: jlavrova@google.com\nStatus: ASSIGNED\n",
              ],
          ),
          mock.patch.object(
              w,
              "parse_buganizer_blocks",
              return_value=[
                  {
                      "id": "466929543",
                      "assignee": "jlavrova@google.com",
                      "status": "ASSIGNED",
                  }
              ],
          ),
      ):
        res_gh = bug_actions.assign_bug_to_self("gh_issue:flutter/flutter#110875")
        self.assertTrue(res_gh["ok"])
        self.assertEqual(res_gh["assignee"], "Rusino")

        res_bz = bug_actions.assign_bug_to_self("buganizer:466929543")
        self.assertTrue(res_bz["ok"])
        self.assertEqual(res_bz["assignee"], "jlavrova@google.com")

        saved = w.load_watcher_state()
        self.assertFalse(saved["bugs"]["gh_issue:flutter/flutter#110875"]["needs_self_assign"])
        self.assertEqual(saved["bugs"]["gh_issue:flutter/flutter#110875"]["assignee"], "Rusino")
        self.assertFalse(saved["bugs"]["buganizer:466929543"]["needs_self_assign"])
        self.assertEqual(
            saved["bugs"]["buganizer:466929543"]["assignee"], "jlavrova@google.com"
        )


if __name__ == "__main__":
  unittest.main()
