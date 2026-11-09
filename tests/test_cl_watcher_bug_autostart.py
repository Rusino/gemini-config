#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for R91 2-min bug sync, newly-assigned autostart, and MCP/chat guards."""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
sys.path.insert(0, REPO_ROOT)

import cl_watcher as w  # pylint: disable=g-import-not-at-top
import entity_state  # pylint: disable=g-import-not-at-top
from workbench import work_store  # pylint: disable=g-import-not-at-top


class BugAutostartAndResilienceTest(unittest.TestCase):
  """Verifies R91 bug poll cadence, assignment triggers, MCP error safety, and chat unlinking."""

  def _run_sync_recording_autostarts(
      self,
      state: dict[str, object],
      now_ts: float,
      mcp_fn: object,
  ) -> list[str]:
    """Runs `sync_bugs_into_state` with mocked tracker/chat boundaries and returns started keys."""
    started: list[str] = []

    def fake_start(
        s: dict[str, object], bkey: str, *, save: bool = True
    ) -> dict[str, object]:
      del save
      started.append(bkey)
      b = (s.get("bugs") or {}).get(bkey)
      if isinstance(b, dict):
        b["conversation_id"] = f"cid-{bkey}"
        b["chat_busy"] = True
      return {"ok": True, "started": True, "conversation_id": f"cid-{bkey}"}

    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(w, "call_buganizer_mcp_batch", side_effect=mcp_fn),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w, "_start_chat_for_bug", side_effect=fake_start),
    ):
      w.sync_bugs_into_state(state, force=True, wake_chats=True, proj_names={"p": "Skia"})
    return started

  def test_bug_sync_ttl_matches_two_minute_daemon_interval(self) -> None:
    """Checks 2-min bug sync TTL so newly assigned issues do not wait across multiple cycles."""
    self.assertEqual(w.BUG_SYNC_TTL_SEC, 120.0)

    now_ts = 1794250000.0
    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:581436834": {
                "bug_key": "buganizer:581436834",
                "source": "buganizer",
                "id": "581436834",
                "status": "ASSIGNED",
                "priority": "P2",
                "issue_type": "BUG",
                "assignee": "jlavrova@google.com",
                "seen_self_assigned": True,
                "modified_time": "2026-11-05T10:00:00Z",
                "modified_ts": w._parse_ts("2026-11-05T10:00:00Z"),
            }
        },
        "last_bug_sync_ts": now_ts - 110.0,
        "events": [],
    }
    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(w, "call_buganizer_mcp_batch") as mcp_mock,
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=False, wake_chats=False, proj_names={"p": "Skia"})
      mcp_mock.assert_not_called()

    state["last_bug_sync_ts"] = now_ts - 121.0
    open_out = (
        "Issue ID: 581436834\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Existing backlog bug\n"
        "Modified Time: 2026-11-05T10:00:00Z\n"
    )
    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(
            w, "call_buganizer_mcp_batch", return_value=[open_out, "No issues found"]
        ) as mcp_mock,
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=False, wake_chats=False, proj_names={"p": "Skia"})
      self.assertGreaterEqual(mcp_mock.call_count, 1)

  def test_three_week_old_assigned_p2_vulnerability_without_comments_autostarts_first(
      self,
  ) -> None:
    """Checks crbug/576811175 shape enters Needs You and sorts ahead of non-vuln P1 bugs."""
    now_ts = w._parse_ts("2026-11-09T16:00:00Z")
    open_out = (
        "Issue ID: 576811175\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: vmiura@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Severity: S2\n"
        "Issue Type: VULNERABILITY\n"
        "Title: Heap overflow in Skia via FreeType Type 1 stack address PRNG seeding\n"
        "Created Time: 2026-10-22T23:29:24.036Z\n"
        "Modified Time: 2026-11-09T15:34:50.757Z\n"
        "Major Modified Time: 2026-11-09T15:34:50.757Z\n\n"
        "---\n\n"
        "Issue ID: 589000111\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: colleague@google.com\n"
        "Status: NEW\n"
        "Priority: P1\n"
        "Issue Type: BUG\n"
        "Title: Newer non-security P1 layout regression\n"
        "Created Time: 2026-11-09T15:50:00Z\n"
        "Modified Time: 2026-11-09T15:50:00Z\n"
        "Major Modified Time: 2026-11-09T15:50:00Z\n\n"
        "---\n\n"
        "Issue ID: 530266830\n"
        "Component ID: 1363359\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: VULNERABILITY\n"
        "Title: Old September vulnerability already in backlog\n"
        "Created Time: 2026-07-01T20:17:26Z\n"
        "Modified Time: 2026-09-16T10:11:11Z\n\n"
        "---\n\n"
        "Issue ID: 466164398\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P0\n"
        "Issue Type: PROJECT\n"
        "Title: [Flutter] Partner enablement\n"
        "Created Time: 2026-10-01T10:00:00Z\n"
        "Modified Time: 2026-11-08T10:00:00Z\n"
    )
    rendered_576 = json.dumps({
        "result": (
            "Issue ID: 576811175\n"
            "Status: ASSIGNED\n"
            "Description by vmiura@google.com at 2026-10-22T23:29:24.036Z:\n"
            "Vulnerability report without human comments.\n"
        )
    })
    rendered_589 = json.dumps({
        "result": (
            "Issue ID: 589000111\n"
            "Status: NEW\n"
            "Description by colleague@google.com at 2026-11-09T15:50:00Z:\n"
            "Layout regression.\n"
        )
    })

    rendered_ids: list[str] = []

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          outs.append(
              open_out if "status:open" in q and "-status:open" not in q else "No issues found"
          )
        elif name == "render_issue_with_external":
          iid = str(args.get("issueId") or "")
          rendered_ids.append(iid)
          if iid == "576811175":
            outs.append(rendered_576)
          elif iid == "589000111":
            outs.append(rendered_589)
          else:
            outs.append(json.dumps({"result": f"Issue ID: {iid}\nStatus: ASSIGNED\n"}))
        else:
          outs.append("")
      return outs

    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:530266830": {
                "bug_key": "buganizer:530266830",
                "source": "buganizer",
                "id": "530266830",
                "status": "ASSIGNED",
                "priority": "P2",
                "issue_type": "VULNERABILITY",
                "assignee": "jlavrova@google.com",
            },
            "buganizer:466164398": {
                "bug_key": "buganizer:466164398",
                "source": "buganizer",
                "id": "466164398",
                "status": "ASSIGNED",
                "priority": "P0",
                "issue_type": "PROJECT",
                "assignee": "jlavrova@google.com",
            },
        },
        "review_claims": {},
        "events": [],
    }
    started = self._run_sync_recording_autostarts(state, now_ts, fake_mcp)

    bugs = state["bugs"]
    assert isinstance(bugs, dict)
    self.assertTrue(bugs["buganizer:576811175"]["needs_you"])
    self.assertEqual(bugs["buganizer:576811175"]["lane"], "you")
    self.assertFalse(bugs["buganizer:530266830"]["needs_you"])
    self.assertNotIn("530266830", rendered_ids)
    self.assertFalse(bugs["buganizer:466164398"]["needs_you"])
    # P2 VULNERABILITY must launch ahead of the non-vulnerability P1 BUG
    self.assertEqual(started, ["buganizer:576811175"])

  def test_newly_assigned_p2_bug_without_comments_autostarts_without_backlog_stampede(
      self,
  ) -> None:
    """Checks newly assigned P2/P3 bugs trigger triage while pre-existing backlog stays quiet."""
    now_ts = w._parse_ts("2026-11-09T17:00:00Z")
    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:581436834": {
                "bug_key": "buganizer:581436834",
                "source": "buganizer",
                "id": "581436834",
                "status": "ASSIGNED",
                "priority": "P2",
                "issue_type": "BUG",
                "assignee": "jlavrova@google.com",
                "title": "Existing P2 backlog bug",
                "created_time": "2026-10-10T10:00:00Z",
                "modified_time": "2026-11-05T10:00:00Z",
            },
            "buganizer:599111222": {
                "bug_key": "buganizer:599111222",
                "source": "buganizer",
                "id": "599111222",
                "status": "ASSIGNED",
                "priority": "P2",
                "issue_type": "BUG",
                "assignee": "other@google.com",
                "is_collaborator": True,
                "acked_ts": w._parse_ts("2026-11-01T12:00:00Z"),
                "title": "Reassigned from colleague without comment",
                "created_time": "2026-10-01T10:00:00Z",
                "modified_time": "2026-11-01T10:00:00Z",
            },
        },
        "review_claims": {},
        "events": [],
    }
    open_out = (
        "Issue ID: 581436834\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Existing P2 backlog bug\n"
        "Created Time: 2026-10-10T10:00:00Z\n"
        "Modified Time: 2026-11-05T10:00:00Z\n\n"
        "---\n\n"
        "Issue ID: 599111222\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: other@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Reassigned from colleague without comment\n"
        "Created Time: 2026-10-01T10:00:00Z\n"
        "Modified Time: 2026-11-09T16:55:00Z\n\n"
        "---\n\n"
        "Issue ID: 599111333\n"
        "Component ID: 1363359\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: external@chromium.org\n"
        "Status: ASSIGNED\n"
        "Priority: P3\n"
        "Issue Type: BUG\n"
        "Title: Newly routed P3 SkParagraph issue without comment\n"
        "Created Time: 2026-10-15T10:00:00Z\n"
        "Modified Time: 2026-11-09T16:58:00Z\n"
    )

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          outs.append(
              open_out if "status:open" in q and "-status:open" not in q else "No issues found"
          )
        elif name == "render_issue_with_external":
          iid = str(args.get("issueId") or "")
          outs.append(json.dumps({"result": f"Issue ID: {iid}\nStatus: ASSIGNED\n"}))
        else:
          outs.append("")
      return outs

    started = self._run_sync_recording_autostarts(state, now_ts, fake_mcp)

    bugs = state["bugs"]
    assert isinstance(bugs, dict)
    self.assertFalse(bugs["buganizer:581436834"]["needs_you"])
    self.assertEqual(bugs["buganizer:581436834"]["lane"], "assigned")

    self.assertTrue(bugs["buganizer:599111222"]["needs_you"])
    self.assertIn("Newly assigned P2", bugs["buganizer:599111222"]["why_needs_you"])
    self.assertEqual(started, ["buganizer:599111222"])

    self.assertTrue(bugs["buganizer:599111333"]["needs_you"])
    self.assertTrue(w.should_autostart_bug_triage(bugs["buganizer:599111333"]))

    # Acknowledging clears Newly assigned Needs You
    bugs["buganizer:599111333"]["acked_ts"] = now_ts
    w.classify_bug_status(bugs["buganizer:599111333"], now_ts=now_ts)
    self.assertFalse(bugs["buganizer:599111333"]["needs_you"])
    self.assertEqual(bugs["buganizer:599111333"]["lane"], "assigned")

  def test_mcp_render_error_preserves_cached_comments_and_bz_outage_retains_prev_bugs(
      self,
  ) -> None:
    """Checks failed MCP render does not poison comment stamps and Buganizer outages keep cache."""
    now_ts = w._parse_ts("2026-11-09T17:30:00Z")
    open_out = (
        "Issue ID: 576811175\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: VULNERABILITY\n"
        "Title: Heap overflow in Skia\n"
        "Created Time: 2026-10-22T23:29:24Z\n"
        "Modified Time: 2026-11-09T15:34:50Z\n"
        "Major Modified Time: 2026-11-09T15:34:50Z\n"
    )
    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:576811175": {
                "bug_key": "buganizer:576811175",
                "source": "buganizer",
                "id": "576811175",
                "status": "ASSIGNED",
                "priority": "P2",
                "issue_type": "VULNERABILITY",
                "assignee": "jlavrova@google.com",
                "last_human_comment_author": "zork@google.com",
                "last_human_comment_at": "2026-11-06T15:39:36Z",
                "last_human_comment_snippet": "Setting Pri-2 to match security severity S2.",
                "comments_checked_major_mod": "v1:old",
            }
        },
        "review_claims": {},
        "events": [],
    }

    rpc_error = json.dumps({"jsonrpc": "2.0", "id": 2, "error": {"message": "Deadline exceeded"}})

    def failing_render_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        q = str(args.get("query") or "")
        if name == "get_bugs_with_external":
          outs.append(
              open_out if "status:open" in q and "-status:open" not in q else "No issues found"
          )
        elif name == "render_issue_with_external":
          outs.append(rpc_error)
        else:
          outs.append("")
      return outs

    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(w, "call_buganizer_mcp_batch", side_effect=failing_render_mcp),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=True, wake_chats=False, proj_names={"p": "Skia"})

    bug_after_err = state["bugs"]["buganizer:576811175"]
    self.assertEqual(bug_after_err["last_human_comment_author"], "zork@google.com")
    self.assertEqual(bug_after_err["comments_checked_major_mod"], "v1:old")

    # Full Buganizer outage (MCP + fallback both empty) retains cached Buganizer entries
    with (
        mock.patch.object(w.time, "time", return_value=now_ts + 200.0),
        mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["", ""]),
        mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=True, wake_chats=False, proj_names={"p": "Skia"})

    self.assertIn("buganizer:576811175", state["bugs"])
    self.assertEqual(
        state["bugs"]["buganizer:576811175"]["last_human_comment_author"], "zork@google.com"
    )

    # B1: real call_buganizer_mcp_batch tool-level isError response triggers fallback & retention
    init_line = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}) + "\n"
    tool_err_line = (
        json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "isError": True,
                "content": [{"type": "text", "text": "RPC error: DEADLINE_EXCEEDED"}],
            },
        })
        + "\n"
    )
    fake_proc = mock.MagicMock()
    fake_proc.stdin = mock.MagicMock()
    fake_proc.stdout.readline.side_effect = [init_line, tool_err_line, "", "", ""]
    fake_proc.poll.return_value = 0
    with (
        mock.patch.object(w.time, "time", return_value=now_ts + 400.0),
        mock.patch.object(w.os.path, "isfile", return_value=True),
        mock.patch.object(w.subprocess, "Popen", return_value=fake_proc),
        mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]) as fb_mock,
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(state, force=True, wake_chats=False, proj_names={"p": "Skia"})
    fb_mock.assert_called_once()
    self.assertIn("buganizer:576811175", state["bugs"])

  def test_github_empty_seed_and_cold_bz_outage_do_not_drop_or_stampede(self) -> None:
    """Checks per-tracker backlog seed flags handle 0-bug GitHub seeds and cold Buganizer outages."""
    now_ts = w._parse_ts("2026-11-09T18:00:00Z")
    # Part A: GitHub seeds cleanly with 0 issues; a newly assigned P2 Rusino bug on Poll 2 triggers
    gh_state: dict[str, object] = {"items": {}, "bugs": {}, "review_claims": {}, "events": []}
    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["No issues found", ""]),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(gh_state, force=True, wake_chats=True, proj_names={"p": "Skia"})

    newly_assigned_gh = [{
        "bug_key": "gh_issue:flutter/flutter#190555",
        "source": "github",
        "id": "190555",
        "short_id": "flutter#190555",
        "url": "https://github.com/flutter/flutter/issues/190555",
        "title": "Three-week-old Flutter issue newly assigned to Rusino",
        "status": "OPEN",
        "priority": "P2",
        "issue_type": "BUG",
        "assignee": "Rusino",
        "from_self_query": True,
        "created_time": "2026-10-15T10:00:00Z",
        "modified_time": "2026-11-09T18:01:00Z",
        "modified_ts": w._parse_ts("2026-11-09T18:01:00Z"),
        "last_human_comment_author": "",
        "last_human_comment_at": "",
        "last_human_comment_snippet": "",
        "last_self_comment_at": "",
    }]
    started_gh: list[str] = []

    def fake_start_gh(
        s: dict[str, object], bkey: str, *, save: bool = True
    ) -> dict[str, object]:
      del s, save
      started_gh.append(bkey)
      return {"ok": True, "started": True, "conversation_id": f"cid-{bkey}"}

    with (
        mock.patch.object(w.time, "time", return_value=now_ts + 130.0),
        mock.patch.object(w, "call_buganizer_mcp_batch", return_value=["No issues found", ""]),
        mock.patch.object(w, "fetch_github_bugs", return_value=newly_assigned_gh),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w, "_start_chat_for_bug", side_effect=fake_start_gh),
    ):
      w.sync_bugs_into_state(gh_state, force=True, wake_chats=True, proj_names={"p": "Skia"})

    self.assertTrue(gh_state["bugs"]["gh_issue:flutter/flutter#190555"]["needs_you"])
    self.assertEqual(started_gh, ["gh_issue:flutter/flutter#190555"])

    # Part B: Cold Buganizer outage with 1 CL-referenced colleague bug does not mark bz seeded
    colleague_out = (
        "Issue ID: 466929543\n"
        "Component ID: 1457031\n"
        "Assignee: colleague@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Shared colleague bug\n"
        "Created Time: 2026-10-01T10:00:00Z\n"
        "Modified Time: 2026-10-05T10:00:00Z\n"
    )
    cold_state: dict[str, object] = {
        "items": {
            "gerrit:1419999@unchatted": {
                "kind": "gerrit",
                "host": "skia",
                "is_self": True,
                "status": "NEW",
                "subject": "Touch colleague bug",
                "commit_message": "Bug: b/466929543\n",
            }
        },
        "bugs": {},
        "review_claims": {},
        "events": [],
    }
    with (
        mock.patch.object(w.time, "time", return_value=now_ts),
        mock.patch.object(
            w, "call_buganizer_mcp_batch", side_effect=[["", ""], [colleague_out]]
        ),
        mock.patch.object(w, "fetch_buganizer_fallback", return_value=[]),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(cold_state, force=True, wake_chats=False, proj_names={"p": "Skia"})

    backlog_out = (
        "Issue ID: 581436834\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Pre-existing backlog bug\n"
        "Created Time: 2026-10-01T10:00:00Z\n"
        "Modified Time: 2026-10-10T10:00:00Z\n"
    )
    with (
        mock.patch.object(w.time, "time", return_value=now_ts + 130.0),
        mock.patch.object(
            w,
            "call_buganizer_mcp_batch",
            return_value=[backlog_out + "\n---\n" + colleague_out, "No issues found"],
        ),
        mock.patch.object(w, "fetch_github_bugs", return_value=[]),
        mock.patch.object(w, "discover_bug_chats", return_value={}),
    ):
      w.sync_bugs_into_state(cold_state, force=True, wake_chats=False, proj_names={"p": "Skia"})

    self.assertFalse(cold_state["bugs"]["buganizer:581436834"]["needs_you"])

  def test_stale_renamed_bug_chat_cleared_in_link_and_start_chat_while_keeping_meta_and_closed(
      self,
  ) -> None:
    """Checks renamed/unlinked chat IDs clear on open bugs while keeping meta and closed bugs."""
    now_ts = time.time()
    bugs: dict[str, dict[str, object]] = {
        "buganizer:576811175": {
            "bug_key": "buganizer:576811175",
            "source": "buganizer",
            "id": "576811175",
            "short_id": "crbug/576811175",
            "url": "https://issues.chromium.org/issues/576811175",
            "title": "Heap overflow in Skia via FreeType Type 1",
            "status": "ASSIGNED",
            "priority": "P2",
            "issue_type": "VULNERABILITY",
            "assignee": "jlavrova@google.com",
            "project_name": "Skia",
            "conversation_id": "b9bb0e3e-da3d-4dca-9e7b-809501387258",
            "chat_title": "[15:43] ⦿ crbug/576811175 bug watcher poll",
            "chat_archived": False,
            "needs_you": True,
            "why_needs_you": "New P2 vulnerability needs triage",
        },
        "buganizer:580403511": {
            "bug_key": "buganizer:580403511",
            "source": "buganizer",
            "id": "580403511",
            "status": "FIXED",
            "project_name": "Skia",
            "conversation_id": "cfcedab1-3456-46b9-8c3d-927f754e97e3",
            "chat_archived": True,
        },
        "buganizer:577347852": {
            "bug_key": "buganizer:577347852",
            "source": "buganizer",
            "id": "577347852",
            "status": "ASSIGNED",
            "project_name": "Skia",
            "conversation_id": "906fde1b-ccdc-4257-8e0d-37ced3ea3387",
            "chat_woken_ts": now_ts - w.CHAT_WAKE_GRACE_SEC / 2,
            "chat_archived": False,
        },
    }
    w.link_bugs_and_cls(bugs, {}, bug_chats={})
    self.assertEqual(bugs["buganizer:576811175"]["conversation_id"], "")
    self.assertTrue(w.should_autostart_bug_triage(bugs["buganizer:576811175"]))
    # Meta/closed bug and recently spawned grace-window bug keep their conversation_ids
    self.assertEqual(
        bugs["buganizer:580403511"]["conversation_id"],
        "cfcedab1-3456-46b9-8c3d-927f754e97e3",
    )
    self.assertEqual(
        bugs["buganizer:577347852"]["conversation_id"],
        "906fde1b-ccdc-4257-8e0d-37ced3ea3387",
    )

    # _start_chat_for_bug also ignores an unlinked stale conversation_id and spawns a fresh chat
    bugs["buganizer:576811175"]["conversation_id"] = "b9bb0e3e-da3d-4dca-9e7b-809501387258"
    state = {"bugs": bugs, "items": {}}
    with (
        mock.patch.object(w, "discover_bug_chats", return_value={}),
        mock.patch.object(w.cl, "resolve_project_id", return_value="proj-skia"),
        mock.patch.object(w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}),
        mock.patch.object(w.cl, "start_conversation_exact", return_value="cid-fresh-576811175"),
    ):
      res = w._start_chat_for_bug(state, "buganizer:576811175", save=False)
      res_fixed = w._start_chat_for_bug(state, "buganizer:580403511", save=False)
    self.assertTrue(res.get("started"))
    self.assertEqual(res.get("conversation_id"), "cid-fresh-576811175")
    self.assertTrue(res_fixed.get("already_existed"))
    self.assertEqual(res_fixed.get("conversation_id"), "cfcedab1-3456-46b9-8c3d-927f754e97e3")

  def test_entity_state_and_workbench_projection_surface_bug_why_needs_you(self) -> None:
    """Checks entity_state.reason and Workbench _evaluate_cl_needs_you expose bug triage reasons."""
    bug: dict[str, object] = {
        "bug_key": "buganizer:576811175",
        "id": "576811175",
        "short_id": "crbug/576811175",
        "status": "ASSIGNED",
        "lane": "you",
        "needs_you": True,
        "why_needs_you": "New P2 vulnerability needs triage",
    }
    est = entity_state.compute_bug_entity_state(bug)
    bug["entity_state"] = est
    self.assertEqual(est.get("reason"), "New P2 vulnerability needs triage")
    self.assertEqual(
        work_store._evaluate_cl_needs_you("buganizer:576811175", bug),
        "New P2 vulnerability needs triage",
    )


if __name__ == "__main__":
  unittest.main()
