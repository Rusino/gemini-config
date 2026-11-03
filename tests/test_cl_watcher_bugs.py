#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for R3 Flight Deck Bugs view, sources, statuses, and bug-CL linking."""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


SAMPLE_MCP_VERBOSE = json.dumps({
    "result": (
        'Showing first 3 issue(s) matching query "...":\n'
        "Issue ID: 580403511\n"
        "Component ID: 2271566\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: onequota-bon-monitor@prod.google.com\n"
        "Status: NEW\n"
        "Priority: P1\n"
        "Issue Type: BUG\n"
        "Title: [Action Required] Notice of Jetski Best-of-N Policy Violation\n\n"
        "Created Time: 2026-11-01T04:01:59.896Z\n"
        "Modified Time: 2026-11-01T18:58:56.907Z\n"
        "Major Modified Time: 2026-11-01T18:58:56.907Z\n"
        "Severity: S2\n"
        "SLO Status:\n"
        "  Response SLO: 2026-11-07T01:00:00Z\n"
        "  Closure SLO: 2026-12-01T04:01:59.896Z\n\n"
        "---\n\n"
        "Issue ID: 576664769\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: vmiura@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P1\n"
        "Issue Type: VULNERABILITY\n"
        "Title: Potential heap OOB write via integer overflow in SkDistanceFieldGen\n\n"
        "Created Time: 2026-10-22T15:56:27.408Z\n"
        "Modified Time: 2026-10-30T21:17:18.238Z\n"
        "Major Modified Time: 2026-10-30T21:17:16.294Z\n"
        "Gerrit changes: https://skia-review.git.corp.google.com/c/skia/+/1404196, "
        "https://skia-review.git.corp.google.com/c/skia/+/1411716\n"
        "Severity: S1\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Internals>Skia\n"
        "SLO Status:\n"
        "  Assignment SLO: 2026-10-29T15:56:27.408Z\n"
        "  Response SLO: 2026-11-06T21:17:18.238Z\n"
        "  Closure SLO: 2026-11-19T15:56:27.408Z\n"
    )
})

SAMPLE_BATCH_GET_REDACTED = (
    "Input Sanitization: some of the content in these issues was made by an external user\n\n"
    "Found 1 issues:\n"
    "- Issue 576664769: [redacted] [ASSIGNED] (Priority: P1, Assignee: jlavrova@google.com)\n"
    "  Severity: S1, Type: VULNERABILITY\n"
)

SAMPLE_RENDERED_ISSUE = json.dumps({
    "result": (
        "Issue ID: 576664769\n"
        "Status: ASSIGNED\n"
        "Description by vmiura@google.com at 2026-10-22T15:56:27.408Z:\n"
        "Initial vulnerability report.\n\n---\n\n"
        "Comment #5 by jlavrova@google.com at 2026-10-29T12:00:00Z:\n"
        "Uploaded fix.\n\n---\n\n"
        "Comment #6 by dx-workflow-gitwatcher@prod.google.com at 2026-10-30T17:48:13.743Z:\n"
        "Link: https://skia-review.googlesource.com/1411716\n\n---\n\n"
        "Comment #7 by yyanagisawa@google.com at 2026-10-30T21:17:16.294Z:\n"
        "CodeMender attached a follow-up note for review.\n"
    )
})

# Structural fixture from crbug.com/466929543 render_issue_with_external (14 entries:
# Description #1 + Comment #2..#14 separated by blank lines, with --- rules inside #3)
SAMPLE_RENDERED_466929543 = (
    "Issue ID: 466929543\n"
    "Component ID: 1457031\n"
    "Assignee: jlavrova@google.com\n"
    "Reporter: ishitatsuyuki@google.com\n"
    "Status: ASSIGNED\n"
    "Priority: P2\n"
    "Issue Type: BUG\n"
    "Title: UseAndroidNDKFontAPI breaks Japanese font in Google Search App\n\n"
    "Created Time: 2025-12-08T07:50:24.570Z\n"
    "Modified Time: 2026-11-02T21:49:00.678Z\n"
    "Description by ishitatsuyuki@google.com at 2025-12-08T07:50:24.570Z:\n"
    "Version\n\n"
    "```\n"
    "  Package [com.google.android.webview] (baeb8af):\n"
    "    versionCode=699813532 minSdk=26 targetSdk=35\n"
    "```\n\n"
    "Search \"日本語\" in Google App -> Serif Font (broken)\n\n"
    "Attaching screenshot and bugreport in threads below\n\n"
    "Blocking Issue IDs: 297067890\n"
    "Note: Run with verbose=true to see severity, effort, descendant issue counts.\n\n"
    "Comment #2 by ishitatsuyuki@google.com at 2025-12-08T07:52:13.029Z:\n"
    "I see https://skia-review.googlesource.com/1116616 but I don't know if it helps since it"
    " hasn't trickled down to stable. If there's an easy way to test it out I'm happy to test.\n\n"
    "Comment #3 by ishitatsuyuki@google.com at 2025-12-08T07:56:33.161Z:\n"
    "---\n\n"
    "### What\n\n"
    "#### More information\n\n"
    "Attaching bug report and screenshots\n\n"
    "#### User experience\n\n"
    "**Has the effect of this issue on your device usage changed, such as lost time or work?**\\\n"
    "Slight\n\n"
    "---\n\n"
    "### When\n\n"
    "#### Time and frequency\n\n"
    "**Time when bug report was triggered:** Dec 8, 2025  4:50 PM  GMT+09:00\n\n"
    "**Has the frequency of this issue changed?**\\\n"
    "Every time\n\n"
    "---\n\n"
    "### Where\n\n"
    "#### Build and device data\n\n"
    "**- Build Number:** aosp_arm64-userdebug\\\n"
    "**- Serial Number:** REDACTED\\\n"
    "**- Is headless system user mode:** No\n\n"
    "---\n\n"
    "**Filed via bug reporter template.**\n\n"
    "Comment #4 by ishitatsuyuki@google.com at 2025-12-08T07:59:31.156Z:\n"
    "Sensitive data has been redacted from this bug report.\n\n"
    "**Do not share, post, or distribute any portion or all of a bug report.**\n\n"
    "Comment #5 by bungeman@google.com at 2025-12-08T15:51:01.944Z:\n"
    "The issue here is probably that the Android NDK Font API does not preserve any order of the"
    " fonts reported (they are reported internally from an `unordered_set`).\n"
    "Last modified on 2025-12-08T16:22:18.056Z\n\n"
    "Comment #6 by ishitatsuyuki@google.com at 2025-12-16T04:26:58.747Z:\n"
    "Per https://chromium-review.googlesource.com/7239069 and"
    " https://chromium-review.googlesource.com/7242463 the feature should be now disabled on ToT"
    " (M145) and M144 in the latest versions.\n"
    "Last modified on 2025-12-16T04:27:07.419Z\n\n"
    "Comment #7 by ishitatsuyuki@google.com at 2025-12-16T04:27:51.764Z:\n"
    "I can close this once a version trickles down that I can confirm the fix, or otherwise feel"
    " free to close it earlier if you are confident.\n\n"
    "Comment #8 by bungeman@google.com at 2025-12-16T15:35:16.376Z:\n"
    "This issue should be left open. The underlying issue has not been fixed (probably needs a fix"
    " in Android), the symptoms have just been papered over for the moment.\n\n"
    "Comment #9 by ishitatsuyuki@google.com at 2025-12-16T15:36:17.318Z:\n"
    "That sounds good to me, thanks for clarifying.\n\n"
    "Comment #10 by nyakkala@google.com at 2026-01-19T05:17:46.577Z:\n"
    "Removing the Triage Team Chaselist hotlist as the component is not related to search.\n\n"
    "Comment #11 by patilr@google.com at 2026-02-20T03:53:57.830Z:\n"
    "This issue is currently in violation of the 30-day P1/P0 SLO. Please transition this to"
    " P2/P3 within 48 hours if it is still a valid issue.\n\n"
    "Comment #12 by bungeman@google.com at 2026-02-20T14:47:34.282Z:\n"
    "Demoting to P2 since the use of the NDK Font API is currently disabled due to it crashing on"
    " use.\n\n"
    "Comment #13 by mingtse@google.com at 2026-10-30T10:06:15.298Z:\n"
    "**[VoU 1P_APP automated triage]**\n"
    "- Severity S4 -> S2: The issue affects font rendering in a core application, but a practical"
    " workaround exists as the feature is currently disabled by default.\n\n"
    "_If you have any concerns about this change, please adjust the priority/severity or remove"
    " the hotlist and kindly leave a comment. Thank you._\n\n"
    "Comment #14 by jlavrova@google.com at 2026-11-02T21:49:00.678Z:\n"
    "I agree with the diagnosis in #5. ASystemFontIterator_open returns fonts from a"
    " std::unordered_set, so the fallback order is random.\n\n"
    "I don't think this is urgent right now. kUseAndroidNDKFontAPI is disabled by default in"
    " Chromium (crbug.com/461659286), and Flutter does not use the NDK font manager."
)


class ClWatcherBugsTest(unittest.TestCase):

  def test_parse_buganizer_blocks_and_redacted_fallback_preserves_title(self) -> None:
    parsed = w.parse_buganizer_blocks(SAMPLE_MCP_VERBOSE)
    self.assertEqual(len(parsed), 2)
    b_int, b_ext = parsed[0], parsed[1]

    self.assertEqual(b_int["bug_key"], "buganizer:580403511")
    self.assertEqual(b_int["tracker_flavor"], "b")
    self.assertEqual(b_int["short_id"], "b/580403511")
    self.assertEqual(b_int["url"], "https://b.corp.google.com/issues/580403511")
    self.assertEqual(b_int["priority"], "P1")
    self.assertEqual(b_int["severity"], "S2")
    self.assertEqual(b_int["slo"]["response_slo"], "2026-11-07T01:00:00Z")

    self.assertEqual(b_ext["bug_key"], "buganizer:576664769")
    self.assertEqual(b_ext["tracker_flavor"], "crbug")
    self.assertEqual(b_ext["short_id"], "crbug/576664769")
    self.assertEqual(b_ext["url"], "https://issues.chromium.org/issues/576664769")
    self.assertEqual(b_ext["gerrit_cl_ids"], ["1404196", "1411716"])
    self.assertEqual(b_ext["slo"]["assignment_slo"], "2026-10-29T15:56:27.408Z")

    redacted = w.parse_buganizer_blocks(SAMPLE_BATCH_GET_REDACTED)
    self.assertEqual(len(redacted), 1)
    self.assertTrue(redacted[0]["title_redacted"])
    merged = w.merge_bug_record_with_prev(redacted[0], b_ext)
    self.assertEqual(
        merged["title"],
        "Potential heap OOB write via integer overflow in SkDistanceFieldGen",
    )

  def test_parse_rendered_issue_comments_ignores_gitwatcher_bots(self) -> None:
    cinfo = w.parse_rendered_issue_comments(SAMPLE_RENDERED_ISSUE)
    self.assertEqual(cinfo["last_human_comment_author"], "yyanagisawa@google.com")
    self.assertEqual(cinfo["last_human_comment_at"], "2026-10-30T21:17:16.294Z")
    self.assertIn("CodeMender", cinfo["last_human_comment_snippet"])
    self.assertEqual(cinfo["last_self_comment_at"], "2026-10-29T12:00:00Z")
    self.assertIn("1411716", cinfo["comment_cl_ids"])

    bot_only = json.dumps({
        "result": (
            "Issue ID: 576664769\n"
            "Comment #1 by vmiura@google.com at 2026-10-28T10:00:00Z:\n"
            "Initial triage.\n\n---\n\n"
            "Comment #2 by jlavrova@google.com at 2026-10-29T12:00:00Z:\n"
            "Landing fix.\n\n---\n\n"
            "Comment #3 by dx-workflow-gitwatcher@prod.google.com at 2026-10-30T17:48:13Z:\n"
            "Project: skia\n"
        )
    })
    cinfo_bot = w.parse_rendered_issue_comments(bot_only)
    self.assertEqual(cinfo_bot["last_human_comment_author"], "vmiura@google.com")
    self.assertEqual(cinfo_bot["last_self_comment_at"], "2026-10-29T12:00:00Z")

  def test_parse_rendered_issue_comments_466929543_fixture_parses_all_14_entries(
      self,
  ) -> None:
    cinfo = w.parse_rendered_issue_comments(SAMPLE_RENDERED_466929543)
    self.assertEqual(cinfo["last_human_comment_author"], "mingtse@google.com")
    self.assertEqual(cinfo["last_human_comment_at"], "2026-10-30T10:06:15.298Z")
    self.assertIn("VoU 1P_APP", cinfo["last_human_comment_snippet"])
    self.assertEqual(cinfo["last_self_comment_at"], "2026-11-02T21:49:00.678Z")
    self.assertEqual(cinfo["comment_cl_ids"], [])

    self.assertIsNotNone(
        re.search(
            r"^Description\s+by\s+\S+\s+at\s+[^\r\n]+?:",
            SAMPLE_RENDERED_466929543,
            re.M,
        )
    )
    comment_nums = [
        int(m.group(1))
        for m in w.RE_RENDERED_ISSUE_COMMENT.finditer(SAMPLE_RENDERED_466929543)
    ]
    # Description (#1) + Comment #2..#14 = all 14 entries parsed individually
    self.assertEqual(comment_nums, list(range(2, 15)))
    self.assertEqual(1 + len(comment_nums), 14)

    # When #13 is authored by a bot, last_human_comment_* falls back to #12 (bungeman)
    bot_13 = SAMPLE_RENDERED_466929543.replace(
        "Comment #13 by mingtse@google.com",
        "Comment #13 by triager-bot@prod.gserviceaccount.com",
    )
    cinfo_bot13 = w.parse_rendered_issue_comments(bot_13)
    self.assertEqual(cinfo_bot13["last_human_comment_author"], "bungeman@google.com")
    self.assertEqual(cinfo_bot13["last_human_comment_at"], "2026-02-20T14:47:34.282Z")
    self.assertEqual(cinfo_bot13["last_self_comment_at"], "2026-11-02T21:49:00.678Z")

  def test_self_reply_clears_needs_you_and_sync_does_not_wake_chat(self) -> None:
    now_ts = w._parse_ts("2026-11-02T22:00:00Z")

    # Stale Needs Me clears once last_self_comment_at > last_human_comment_at
    bug_466: dict[str, object] = {
        "bug_key": "buganizer:466929543",
        "source": "buganizer",
        "id": "466929543",
        "short_id": "crbug/466929543",
        "status": "ASSIGNED",
        "priority": "P2",
        "issue_type": "BUG",
        "assignee": "jlavrova@google.com",
        "created_time": "2025-12-08T07:50:24.570Z",
        "modified_time": "2026-11-02T21:49:00.678Z",
        "needs_you": True,
        "why_needs_you": "New comment from ishitatsuyuki@google.com: Sensitive data...",
        "lane": "you",
        "last_human_comment_author": "mingtse@google.com",
        "last_human_comment_at": "2026-10-30T10:06:15.298Z",
        "last_human_comment_snippet": "VoU automated triage",
        "last_self_comment_at": "2026-11-02T21:49:00.678Z",
        "linked_cls": [],
    }
    w.classify_bug_status(bug_466, now_ts=now_ts)
    self.assertFalse(bug_466["needs_you"])
    self.assertEqual(bug_466["why_needs_you"], "")
    self.assertEqual(bug_466["lane"], "assigned")

    # Urgent P1 / vulnerability without a CL also leaves Needs Me once user replies on the bug
    bug_p1: dict[str, object] = {
        "bug_key": "buganizer:599000111",
        "source": "buganizer",
        "id": "599000111",
        "status": "NEW",
        "priority": "P1",
        "issue_type": "VULNERABILITY",
        "assignee": "jlavrova@google.com",
        "created_time": "2026-11-01T10:00:00Z",
        "modified_time": "2026-11-02T21:49:00Z",
        "last_human_comment_author": "reporter@google.com",
        "last_human_comment_at": "2026-11-01T12:00:00Z",
        "last_self_comment_at": "",
        "linked_cls": [],
    }
    w.classify_bug_status(bug_p1, now_ts=now_ts)
    self.assertTrue(bug_p1["needs_you"])
    bug_p1["last_self_comment_at"] = "2026-11-02T21:49:00Z"
    w.classify_bug_status(bug_p1, now_ts=now_ts)
    self.assertFalse(bug_p1["needs_you"])
    self.assertEqual(bug_p1["why_needs_you"], "")

    # Full sync heals a v1-cached bug stuck in Needs Me without waking its conversation
    open_out = (
        "Issue ID: 466929543\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: ishitatsuyuki@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: UseAndroidNDKFontAPI breaks Japanese font in Google Search App\n"
        "Created Time: 2025-12-08T07:50:24.570Z\n"
        "Modified Time: 2026-11-02T21:49:00.678Z\n"
        "Major Modified Time: 2026-10-30T10:06:15.298Z\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Internals>Skia\n"
    )
    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:466929543": {
                "bug_key": "buganizer:466929543",
                "source": "buganizer",
                "id": "466929543",
                "short_id": "crbug/466929543",
                "status": "ASSIGNED",
                "priority": "P2",
                "assignee": "jlavrova@google.com",
                "conversation_id": "d5638b00-7c70-4cc5-820a-3328ae0e538f",
                "chat_archived": False,
                "last_human_comment_author": "ishitatsuyuki@google.com",
                "last_human_comment_at": "2025-12-08T07:59:31.156Z",
                "last_human_comment_snippet": "Sensitive data has been redacted...",
                "last_self_comment_at": "",
                "comments_checked_major_mod": "2026-11-02T21:49:00.678Z",
                "needs_you": True,
                "why_needs_you": "New comment from ishitatsuyuki@google.com: Sensitive data...",
                "lane": "you",
            }
        },
    }

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          outs.append(open_out if "status:open" in q and "-status:open" not in q else "")
        elif name == "render_issue_with_external":
          outs.append(json.dumps({"result": SAMPLE_RENDERED_466929543}))
        else:
          outs.append("")
      return outs

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w,
        "discover_bug_chats",
        return_value={
            "buganizer:466929543": {
                "conversation_id": "d5638b00-7c70-4cc5-820a-3328ae0e538f",
                "title": "[16:32] \u29bf crbug/466929543 Android NDK font order",
                "chat_archived": False,
                "chat_busy": False,
                "project_id": "proj-skia",
                "project_name": "Skia",
            }
        },
    ), mock.patch.object(
        w, "wake_conversation"
    ) as wake_mock:
      w.sync_bugs_into_state(
          state,
          force=True,
          wake_chats=True,
          proj_names={"proj-skia": "Skia"},
      )

    healed_466 = state["bugs"]["buganizer:466929543"]
    self.assertEqual(healed_466["last_human_comment_author"], "mingtse@google.com")
    self.assertEqual(healed_466["last_self_comment_at"], "2026-11-02T21:49:00.678Z")
    self.assertFalse(healed_466["needs_you"])
    self.assertEqual(healed_466["why_needs_you"], "")
    self.assertEqual(healed_466["lane"], "assigned")
    wake_mock.assert_not_called()

  def test_namespaced_bug_extraction_separates_buganizer_and_github(self) -> None:
    gerrit_refs = w.extract_bug_refs_from_text(
        "[SkParagraph] Fix metrics\n\nBug: b/576664769, crbug.com/579861086\n"
        "Bug: skia:40045539, skia:1234\n",
        default_tracker="buganizer",
    )
    self.assertIn("buganizer:576664769", gerrit_refs)
    self.assertIn("buganizer:579861086", gerrit_refs)
    self.assertIn("buganizer:40045539", gerrit_refs)
    # 4-digit legacy Monorail ID must not collide with Buganizer IDs
    self.assertNotIn("buganizer:1234", gerrit_refs)

    gh_refs = w.extract_bug_refs_from_text(
        "Companion to PR #195122\nFixes #193731\n"
        "Also relates to https://github.com/flutter/flutter/issues/188322 and crbug/566820669",
        default_tracker="github",
    )
    self.assertIn("gh_issue:flutter/flutter#193731", gh_refs)
    self.assertIn("gh_issue:flutter/flutter#188322", gh_refs)
    self.assertIn("buganizer:566820669", gh_refs)
    self.assertNotIn("gh_issue:flutter/flutter#195122", gh_refs)

  def test_bug_lane_precedence_slo_trap_and_ack(self) -> None:
    now_ts = w._parse_ts("2026-11-02T15:00:00Z")

    # Already ASSIGNED bug with past Assignment SLO but future Response/Closure SLO is not Overdue
    b_assigned = {
        "bug_key": "buganizer:576664769",
        "source": "buganizer",
        "status": "ASSIGNED",
        "priority": "P1",
        "issue_type": "VULNERABILITY",
        "created_time": "2026-10-20T00:00:00Z",
        "modified_time": "2026-10-30T21:17:18Z",
        "slo": {
            "assignment_slo": "2026-10-29T15:56:27Z",
            "response_slo": "2026-11-06T21:17:18Z",
            "closure_slo": "2026-11-19T15:56:27Z",
        },
        "linked_cls": [
            {"item_key": "gerrit:1404196", "status": "MERGED", "submitted_ts": now_ts - 3600},
            {"item_key": "gerrit:1411716", "status": "MERGED", "submitted_ts": now_ts - 1800},
        ],
    }
    w.classify_bug_status(b_assigned, now_ts=now_ts)
    self.assertFalse(b_assigned["is_overdue"])
    # All linked CLs are MERGED while bug is still ASSIGNED -> surfaces in Needs you
    self.assertEqual(b_assigned["lane"], "you")
    self.assertIn("All linked CLs merged", b_assigned["why_needs_you"])

    # Acknowledging moves it out of Needs you into Has CL
    b_assigned["acked_ts"] = now_ts
    w.classify_bug_status(b_assigned, now_ts=now_ts)
    self.assertEqual(b_assigned["lane"], "has_cl")

    # Open CL with Needs You stays in Has CL (no duplicate bug card in Bugs -> Needs you)
    b_open_cl = {
        "bug_key": "gh_issue:flutter/flutter#193731",
        "source": "github",
        "status": "OPEN",
        "priority": "P2",
        "created_time": "2026-10-10T00:00:00Z",
        "modified_time": "2026-11-01T12:00:00Z",
        "linked_cls": [
            {"item_key": "github:195200", "status": "OPEN", "in_needs_you": True},
        ],
    }
    w.classify_bug_status(b_open_cl, now_ts=now_ts)
    self.assertEqual(b_open_cl["lane"], "has_cl")

    # Overdue Closure SLO routes to Overdue when no unacked human comment exists
    b_overdue = {
        "bug_key": "buganizer:570000001",
        "source": "buganizer",
        "status": "ASSIGNED",
        "priority": "P2",
        "created_time": "2026-09-01T00:00:00Z",
        "modified_time": "2026-10-01T00:00:00Z",
        "slo": {"closure_slo": "2026-11-01T00:00:00Z"},
        "linked_cls": [],
    }
    w.classify_bug_status(b_overdue, now_ts=now_ts)
    self.assertTrue(b_overdue["is_overdue"])
    self.assertEqual(b_overdue["lane"], "overdue")

    # Stale >60d P2 unchatted bug is flagged as backlog inside Assigned
    b_stale = {
        "bug_key": "buganizer:400111222",
        "source": "buganizer",
        "status": "ASSIGNED",
        "priority": "P2",
        "created_time": "2024-01-01T00:00:00Z",
        "modified_time": "2026-07-01T00:00:00Z",
        "linked_cls": [],
    }
    w.classify_bug_status(b_stale, now_ts=now_ts)
    self.assertEqual(b_stale["lane"], "assigned")
    self.assertTrue(b_stale["is_stale_backlog"])

    # Closed states distinguish Fixed vs Closed (not planned / obsolete)
    b_fixed = {"bug_key": "buganizer:1", "status": "FIXED", "linked_cls": []}
    b_closed = {
        "bug_key": "gh_issue:flutter/flutter#2",
        "source": "github",
        "status": "CLOSED",
        "state_reason": "NOT_PLANNED",
        "linked_cls": [],
    }
    w.classify_bug_status(b_fixed, now_ts=now_ts)
    w.classify_bug_status(b_closed, now_ts=now_ts)
    self.assertEqual(b_fixed["lane"], "fixed")
    self.assertEqual(b_closed["lane"], "closed")

  def test_shared_bug_and_cl_chat_inheritance_single_target_and_roll_badge(self) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
      proj_id = "proj-skia"
      cid_bug = "cid-bug-shared"
      state_dir = os.path.join(tmpdir, proj_id, "state")
      os.makedirs(state_dir, exist_ok=True)
      with open(os.path.join(state_dir, f"{cid_bug}.md"), "w", encoding="utf-8") as f:
        f.write(
            "# State: b/579861086 SDF heap overflow\n\n"
            "## Fix CL\n"
            "- Uploaded https://skia-review.googlesource.com/c/skia/+/1413000\n"
        )

      fake_summaries = {
          cid_bug: {
              "annotations": {
                  "title": "[15:21] ⦿ b/579861086 SDF heap overflow triage",
                  "archived": False,
              },
              "trajectoryMetadata": {
                  "projectId": proj_id,
                  "lastModifiedTime": "2026-11-02T14:00:00Z",
              },
              "status": "CASCADE_RUN_STATUS_RUNNING",
          }
      }
      with mock.patch.object(
          w.cl, "_fetch_ls_summaries_for_audit", return_value=fake_summaries
      ), mock.patch.object(
          w, "load_project_names", return_value={proj_id: "Skia"}
      ), mock.patch.object(
          w, "ROADMAPS_DIR", tmpdir
      ):
        cl_chats = w.discover_active_cl_chats()
        bug_chats = w.discover_bug_chats()

      # Bug-titled chat discovers state-file CL 1413000 and keeps single_target=True (1 bug + 1 CL)
      self.assertEqual(len(cl_chats), 1)
      self.assertEqual(cl_chats[0]["item_key"], "gerrit:1413000")
      self.assertEqual(cl_chats[0]["conversation_id"], cid_bug)
      self.assertTrue(cl_chats[0]["single_target"])
      self.assertIn("buganizer:579861086", bug_chats)
      self.assertEqual(bug_chats["buganizer:579861086"]["conversation_id"], cid_bug)

    # Bidirectional chat inheritance + external review isolation
    items = {
        "gerrit:1413001@unchatted": {
            "item_key": "gerrit:1413001",
            "kind": "gerrit",
            "target_id": "1413001",
            "is_self": True,
            "status": "NEW",
            "subject": "Fix SDF overflow",
            "commit_message": "Fix SDF\n\nBug: b/579861086\n",
            "url": "https://skia-review.googlesource.com/c/skia/+/1413001",
            "conversation_id": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
        },
        "gerrit:1413002@cid-cl-owned": {
            "item_key": "gerrit:1413002",
            "kind": "gerrit",
            "target_id": "1413002",
            "is_self": True,
            "status": "NEW",
            "subject": "Fix CoreText CFBoolean axis",
            "commit_message": "Fix axis\n\nBug: b/566820669\n",
            "url": "https://skia-review.googlesource.com/c/skia/+/1413002",
            "conversation_id": "cid-cl-owned",
            "chat_archived": False,
            "chat_busy": True,
            "project_id": "proj-skore",
            "project_name": "SkoreText",
        },
        "gerrit:1413003@cid-ext-rev": {
            "item_key": "gerrit:1413003",
            "kind": "gerrit",
            "target_id": "1413003",
            "is_self": False,
            "role": "reviewer",
            "status": "NEW",
            "subject": "External contributor change",
            "commit_message": "External\n\nBug: b/555000111\n",
            "url": "https://skia-review.googlesource.com/c/skia/+/1413003",
            "conversation_id": "cid-ext-rev",
            "chat_archived": False,
            "project_id": "proj-skia",
            "project_name": "Skia",
        },
    }
    bugs = {
        "buganizer:579861086": {
            "bug_key": "buganizer:579861086",
            "source": "buganizer",
            "id": "579861086",
            "short_id": "crbug/579861086",
            "title": "SDF heap buffer overflow",
            "status": "ASSIGNED",
            "priority": "P1",
            "url": "https://issues.chromium.org/issues/579861086",
            "conversation_id": "cid-bug-579",
            "chat_archived": False,
            "project_id": "proj-skia",
            "project_name": "Skia",
        },
        "buganizer:566820669": {
            "bug_key": "buganizer:566820669",
            "source": "buganizer",
            "id": "566820669",
            "short_id": "crbug/566820669",
            "title": "CoreText variation axes CFBoolean",
            "status": "ASSIGNED",
            "priority": "P2",
            "url": "https://issues.chromium.org/issues/566820669",
            "conversation_id": "",
            "project_id": "",
            "project_name": "Unassigned",
        },
        "buganizer:555000111": {
            "bug_key": "buganizer:555000111",
            "source": "buganizer",
            "id": "555000111",
            "short_id": "crbug/555000111",
            "title": "External bug with reviewer CL",
            "status": "ASSIGNED",
            "priority": "P2",
            "url": "https://issues.chromium.org/issues/555000111",
            "conversation_id": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
        },
    }
    w.link_bugs_and_cls(bugs, items)

    # 1) Unchatted self CL 1413001 inherits active bug chat cid-bug-579
    self.assertIn("gerrit:1413001@cid-bug-579", items)
    self.assertEqual(
        items["gerrit:1413001@cid-bug-579"]["conversation_id"], "cid-bug-579"
    )
    self.assertTrue(bugs["buganizer:579861086"]["linked_cls"][0]["shared_chat"])

    # 2) Unchatted bug 566820669 inherits active self CL chat cid-cl-owned & project SkoreText
    self.assertEqual(bugs["buganizer:566820669"]["conversation_id"], "cid-cl-owned")
    self.assertEqual(bugs["buganizer:566820669"]["project_name"], "SkoreText")
    self.assertTrue(bugs["buganizer:566820669"]["linked_cls"][0]["shared_chat"])

    # 3) External review chat cid-ext-rev stays on CL subtask only, NOT on parent bug
    self.assertEqual(bugs["buganizer:555000111"]["conversation_id"], "")
    self.assertFalse(bugs["buganizer:555000111"]["linked_cls"][0]["shared_chat"])
    self.assertEqual(
        bugs["buganizer:555000111"]["linked_cls"][0]["conversation_id"], "cid-ext-rev"
    )

    # 4) Roll badge truncation preserves bug token intact under 38 chars
    saved_topics: list[str] = []
    with mock.patch.object(
        w.cl, "set_conversation_topic", side_effect=lambda cid, t: saved_topics.append(t)
    ):
      w.update_chat_roll_badge(
          "cid-bug-579",
          "[15:21] ⦿ crbug/579861086 distance field glyph overflow on 32-bit",
          "[⏳ 2/4]",
      )
    self.assertEqual(len(saved_topics), 1)
    self.assertIn("crbug/579861086", saved_topics[0])
    self.assertLessEqual(len(saved_topics[0]), w.TITLE_TOPIC_MAX)

  def test_dashboard_js_cls_bugs_switch_subtasks_and_chat_dedup(self) -> None:
    js_code = r"""
const assert = require('assert');
const fs = require('fs');
const html = fs.readFileSync(0, 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/boot\(\);/g, '');

const els = {};
function getEl(id) {
  if (!els[id]) els[id] = {innerHTML: '', textContent: '', classList: {add(){}, remove(){}}};
  return els[id];
}
global.document = {
  getElementById: getEl,
  documentElement: {classList: {add(){}}},
  addEventListener: () => {},
};
global.window = {parent: null};
global.location = {search: '', hash: ''};
global.history = {
  replaceState: (_s, _t, url) => { global.location.hash = String(url || ''); },
};
global.localStorage = {
  _m: {},
  getItem(k) { return this._m[k] || null; },
  setItem(k, v) { this._m[k] = String(v); },
};

eval(script + `
cachedData = {
  jetski_box_url: 'https://jetski.corp.google.com/box/host',
  last_sync: '10:20:00 ET',
  rollers_summary: {},
  items: {
    'gerrit:1413001@cid-shared': {
      item_key: 'gerrit:1413001',
      target_id: '1413001',
      kind: 'gerrit',
      project_name: 'Skia',
      subject: '[Skia] Fix SDF overflow',
      url: 'https://skia-review.googlesource.com/c/skia/+/1413001',
      status: 'NEW',
      is_self: true,
      in_my_attention: false,
      conversation_id: 'cid-shared',
      chat_busy: true,
      linked_bugs: [{
        bug_key: 'buganizer:579861086',
        short_id: 'crbug/579861086',
        priority: 'P1',
        url: 'https://issues.chromium.org/issues/579861086',
      }],
    },
  },
  bugs: {
    'buganizer:579861086': {
      bug_key: 'buganizer:579861086',
      short_id: 'crbug/579861086',
      title: 'SDF heap buffer overflow',
      project_name: 'Skia',
      priority: 'P1',
      severity: 'S1',
      status: 'ASSIGNED',
      lane: 'you',
      needs_you: true,
      why_needs_you: 'New comment from yyanagisawa@google.com',
      url: 'https://issues.chromium.org/issues/579861086',
      conversation_id: 'cid-shared',
      chat_busy: true,
      modified_et: '10:15 ET',
      modified_age: '5m ago',
      linked_cls: [
        {
          item_key: 'gerrit:1413001',
          label: 'CL 1413001',
          subject: 'Fix SDF overflow',
          url: 'https://skia-review.googlesource.com/c/skia/+/1413001',
          status: 'NEW',
          state_summary: 'Waiting on reviewer',
          conversation_id: 'cid-shared',
          shared_chat: true,
        },
        {
          item_key: 'gerrit:1413999',
          label: 'CL 1413999',
          subject: 'Follow-up test',
          url: 'https://skia-review.googlesource.com/c/skia/+/1413999',
          status: 'NEW',
          state_summary: 'CR +1',
          conversation_id: 'cid-separate',
          shared_chat: false,
        },
      ],
    },
    'buganizer:400111222': {
      bug_key: 'buganizer:400111222',
      short_id: 'crbug/400111222',
      title: 'Old stale backlog bug',
      project_name: 'TextClusters API',
      priority: 'P3',
      status: 'ASSIGNED',
      lane: 'assigned',
      is_stale_backlog: true,
      url: 'https://issues.chromium.org/issues/400111222',
      linked_cls: [],
    },
    'buganizer:40618213': {
      bug_key: 'buganizer:40618213',
      short_id: 'crbug/40618213',
      title: 'Android System Fonts NDK and fonts.xml changes',
      project_name: 'Skia',
      priority: 'P3',
      status: 'ASSIGNED',
      lane: 'has_cl',
      url: 'https://issues.chromium.org/issues/40618213',
      linked_cls: [
        {
          item_key: 'gerrit:101', label: 'CL 101', subject: 'Gerrit 101',
          url: 'https://skia-review.googlesource.com/c/skia/+/101',
          status: 'LINKED', state_summary: 'Linked in Buganizer',
        },
        {
          item_key: 'gerrit:102', label: 'CL 102', subject: 'Gerrit 102',
          url: 'https://skia-review.googlesource.com/c/skia/+/102',
          status: 'LINKED', state_summary: 'Linked in Buganizer',
        },
        {
          item_key: 'gerrit:103', label: 'CL 103', subject: 'Gerrit 103',
          url: 'https://skia-review.googlesource.com/c/skia/+/103',
          status: 'LINKED', state_summary: 'Linked in Buganizer',
        },
        {
          item_key: 'gerrit:104', label: 'CL 104', subject: 'Gerrit 104',
          url: 'https://skia-review.googlesource.com/c/skia/+/104',
          status: 'LINKED', state_summary: 'Linked in Buganizer',
        },
        {
          item_key: 'gerrit:105', label: 'CL 105', subject: 'Active NDK font fix',
          url: 'https://skia-review.googlesource.com/c/skia/+/105',
          status: 'NEW', state_summary: 'CR +1',
        },
        {
          item_key: 'gerrit:106', label: 'CL 106', subject: 'Gerrit 106',
          url: 'https://skia-review.googlesource.com/c/skia/+/106',
          status: 'LINKED', state_summary: 'Linked in Buganizer',
        },
      ],
    },
    'buganizer:579000111': {
      bug_key: 'buganizer:579000111',
      short_id: 'crbug/579000111',
      tracker_flavor: 'crbug',
      title: 'Duplicate crash report',
      project_name: 'Skia',
      priority: 'P1',
      status: 'DUPLICATE',
      duplicate_of: '576664769',
      lane: 'closed',
      url: 'https://issues.chromium.org/issues/579000111',
      linked_cls: [],
    },
  },
};

// Default CLs view renders parent bug badge on CL row and view switcher in top bar
render();
assert(getEl('view-switch').innerHTML.includes('CLs'), 'Must render CLs tab');
assert(getEl('view-switch').innerHTML.includes('Bugs'), 'Must render Bugs tab');
assert(getEl('lanes').innerHTML.includes('crbug/579861086'), 'CL row must show parent bug badge');

// Switch to Bugs view
setView('bugs');
const focusOut = getEl('focus').innerHTML;
const processingOut = getEl('processing').innerHTML;
const lanesOut = getEl('lanes').innerHTML;
assert(processingOut.includes('crbug/579861086'), 'Bugs processing must render busy Needs You bug');
assert(processingOut.includes('CL 1413001'), 'Bug focus card must render subtask CL 1413001');
assert(processingOut.includes('CL 1413999'), 'Bug focus card must render subtask CL 1413999');
assert(processingOut.includes('ackBug('), 'Needs you bug must render Ack button');

// Shared chat cid-shared appears on parent bug, NOT duplicated on CL 1413001 subtask;
// separate chat cid-separate DOES appear on CL 1413999 subtask
const cidSharedOpenCount = processingOut.split('openChat(&quot;cid-shared&quot;').length - 1;
assert.strictEqual(cidSharedOpenCount, 2, 'Shared chat button must render once on parent bug');
const subtasksHtml = processingOut.split('<div class="bug-subtasks">')[1] || '';
assert(!subtasksHtml.includes('cid-shared'), 'Shared chat must not be duplicated on subtask');
assert(
  subtasksHtml.includes('openChat(&quot;cid-separate&quot;'),
  'Separate CL chat must appear on its subtask row',
);

// Busy chat headline deduplicates cid-shared to 1 working chat
assert(
  getEl('headline').innerHTML.includes('One chat is working.'),
  'Headline must deduplicate busy shared chat: ' + getEl('headline').innerHTML,
);

// Stale >60d Assigned bug renders inside collapsed backlog details
assert(lanesOut.includes('Backlog'), 'Assigned lane must render collapsed Backlog section');
assert(lanesOut.includes('crbug/400111222'), 'Backlog must contain stale bug');

// Umbrella bug with >4 subtasks promotes active CLs, drops Gerrit <id> noise, and folds tail
assert(!lanesOut.includes('Gerrit 101'), 'LINKED subtask must omit redundant Gerrit <id> subject');
assert(
  lanesOut.includes('+3 more linked CLs'),
  'Umbrella bug >4 subtasks must fold excess into details',
);
const beforeMore = lanesOut.split('<details class="subtask-more">')[0] || '';
assert(
  beforeMore.includes('Active NDK font fix'),
  'Tracked active CL must stay above folded LINKED tail',
);

// Duplicate closed row links canonical target issue
assert(lanesOut.includes('DUPLICATE →'), 'Duplicate closed bug must render canonical target arrow');
assert(lanesOut.includes('crbug/576664769'), 'Duplicate closed bug must name canonical crbug ID');

// Switching from a bug-only project back to CLs resets selectedProject and syncs URL hash
setProject('TextClusters API');
assert(global.location.hash.includes('project=TextClusters%20API'));
setView('cls');
assert(
  !global.location.hash.includes('project='),
  'Switching to CLs without that project must clear stale project hash: ' + global.location.hash,
);
assert(
  getEl('view-switch').innerHTML.includes('CLs <span class="v-count">1</span>'),
  'View switch badge must reflect reset ALL scope: ' + getEl('view-switch').innerHTML,
);
`);
"""
    res = subprocess.run(
        ["node", "-e", js_code],
        input=w.DASHBOARD_HTML,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode, 0, f"Node stdout: {res.stdout}\nStderr: {res.stderr}"
    )

  def test_duplicate_of_status_normalized_and_routed_to_closed(self) -> None:
    dup_block = (
        "Issue ID: 579861086\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: atijari00@gmail.com\n"
        "Status: DUPLICATE of 576664769\n"
        "Priority: P1\n"
        "Issue Type: VULNERABILITY\n"
        "Title: [redacted]\n"
        "Created Time: 2026-10-30T15:09:28.566Z\n"
        "Modified Time: 2026-11-02T13:43:05.200Z\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Internals>Skia\n"
    )
    parsed = w.parse_buganizer_blocks(dup_block)
    self.assertEqual(len(parsed), 1)
    dup_bug = parsed[0]
    self.assertEqual(dup_bug["status"], "DUPLICATE")
    self.assertEqual(dup_bug["duplicate_of"], "576664769")
    self.assertTrue(dup_bug["title_redacted"])

    now_ts = w._parse_ts("2026-11-02T15:00:00Z")
    w.classify_bug_status(dup_bug, now_ts=now_ts)
    self.assertEqual(dup_bug["lane"], "closed")
    self.assertFalse(dup_bug["needs_you"])

    legacy_cached = {
        "bug_key": "buganizer:579861086",
        "source": "buganizer",
        "status": "DUPLICATE OF 576664769",
        "priority": "P1",
        "issue_type": "VULNERABILITY",
        "created_time": "2026-10-30T15:09:28.566Z",
    }
    w.classify_bug_status(legacy_cached, now_ts=now_ts)
    self.assertEqual(legacy_cached["lane"], "closed")
    self.assertFalse(legacy_cached["needs_you"])

  def test_redacted_title_fallback_chain_and_placeholder_healing(self) -> None:
    raw_redacted = w.parse_buganizer_blocks(
        "Issue ID: 566820669\n"
        "Component ID: 1363359\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: hiking90@gmail.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Title: [redacted]\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Internals>Skia\n"
    )[0]
    stale_prev = {
        "bug_key": "buganizer:566820669",
        "id": "566820669",
        "title": "Bug 566820669",
        "title_redacted": False,
    }
    healed = w.merge_bug_record_with_prev(raw_redacted, stale_prev)
    self.assertTrue(healed["title_redacted"])
    self.assertNotEqual(healed["title"], "Bug 566820669")

    b_chat = {
        **healed,
        "id": "579861086",
        "short_id": "crbug/579861086",
        "chat_title": "[15:21] \u29bf b/579861086 SDF heap overflow triage",
        "duplicate_of": "576664769",
    }
    all_bugs = {
        "buganizer:576664769": {
            "short_id": "crbug/576664769",
            "title": "Potential heap OOB write via integer overflow in SkDistanceFieldGen",
            "title_redacted": False,
        }
    }
    w._resolve_redacted_bug_title(b_chat, all_bugs)
    self.assertEqual(b_chat["title"], "SDF heap overflow triage")

    b_legacy_chat = {
        **healed,
        "id": "575736567",
        "short_id": "crbug/575736567",
        "chat_title": "[13:57] \u00ab\u00bb b/575736567 cl/1400696 fontconfig fallback",
    }
    w._resolve_redacted_bug_title(b_legacy_chat, all_bugs)
    self.assertEqual(b_legacy_chat["title"], "fontconfig fallback")

    b_cl = {
        **healed,
        "id": "41412747",
        "short_id": "crbug/41412747",
        "linked_cls": [{
            "label": "CL 7686597",
            "subject": "skia: add opt-in Linux text correction and LCD filtering",
            "status": "NEW",
            "is_self": False,
        }],
    }
    w._resolve_redacted_bug_title(b_cl, all_bugs)
    self.assertIn("add opt-in Linux text correction", b_cl["title"])
    self.assertIn("CL 7686597", b_cl["title"])

    b_dup = {
        **healed,
        "id": "579861086",
        "short_id": "crbug/579861086",
        "duplicate_of": "576664769",
    }
    w._resolve_redacted_bug_title(b_dup, all_bugs)
    self.assertIn("Duplicate of crbug/576664769", b_dup["title"])
    self.assertIn("SkDistanceFieldGen", b_dup["title"])

    w._resolve_redacted_bug_title(healed, all_bugs)
    self.assertIn("[External title redacted]", healed["title"])
    self.assertIn("hiking90@gmail.com", healed["title"])
    topic = w._build_start_chat_topic({**healed, "kind": "buganizer", "project_name": "Skia"})
    self.assertNotIn("redacted", topic.lower())
    self.assertIn("crbug/566820669", topic)

  def test_sync_bugs_filters_reviewer_colleague_bugs_while_keeping_cl_badges(self) -> None:
    open_out = (
        "Issue ID: 576664769\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P1\n"
        "Title: Potential heap OOB write in SkDistanceFieldGen\n"
        "Modified Time: 2026-10-30T21:17:18Z\n"
    )
    extra_out = (
        "Issue ID: 578524618\n"
        "Component ID: 1456769\n"
        "Assignee: fmalita@chromium.org\n"
        "Reporter: fmalita@chromium.org\n"
        "Status: ASSIGNED\n"
        "Priority: P1\n"
        "Title: Extract ShapedText helpers\n"
        "Gerrit changes: https://chromium-review.git.corp.google.com/c/chromium/src/+/8660604\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Blink>Canvas\n\n---\n\n"
        "Issue ID: 590000001\n"
        "Component ID: 1363359\n"
        "Assignee: N/A\n"
        "Status: NEW\n"
        "Priority: P2\n"
        "Title: [redacted]\n\n---\n\n"
        "Issue ID: 576507252\n"
        "Component ID: 1457031\n"
        "Assignee: brettos@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Title: textToGlyphs return value inconsistency\n"
    )
    state: dict[str, object] = {
        "items": {
            "gerrit:8663938@cid-rev": {
                "item_key": "gerrit:8663938",
                "kind": "gerrit",
                "target_id": "8663938",
                "is_self": False,
                "role": "reviewer",
                "status": "MERGED",
                "subject": "[canvas] Extract ShapedText helpers",
                "commit_message": "Extract\n\nBug: 578524618\n",
                "conversation_id": "cid-rev",
                "project_name": "Skia",
            },
            "gerrit:1413500@cid-self": {
                "item_key": "gerrit:1413500",
                "kind": "gerrit",
                "target_id": "1413500",
                "is_self": True,
                "status": "NEW",
                "subject": "[SkoreText] Fix CoreText axis clamping",
                "commit_message": "Fix\n\nBug: 590000001\n",
                "conversation_id": "cid-self",
                "chat_archived": False,
                "project_id": "proj-skore",
                "project_name": "SkoreText",
            },
        },
        "bugs": {},
    }
    fake_bug_chats = {
        "buganizer:576507252": {
            "conversation_id": "cid-collab-chat",
            "title": "[14:00] \u29bf b/576507252 textToGlyphs inconsistency",
            "chat_archived": False,
            "chat_busy": False,
            "project_id": "proj-skia",
            "project_name": "Skia",
        }
    }

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          if "status:open" in q and "-status:open" not in q:
            outs.append(open_out)
          elif "id:" in q:
            outs.append(extra_out)
          else:
            outs.append("")
        else:
          outs.append("")
      return outs

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value=fake_bug_chats
    ):
      w.sync_bugs_into_state(
          state,
          force=True,
          wake_chats=False,
          proj_names={"proj-skia": "Skia", "proj-skore": "SkoreText"},
      )

    bugs = state["bugs"]
    assert isinstance(bugs, dict)
    # Reviewer-only colleague bug is excluded from Bugs tab but kept as badge on reviewed CL
    self.assertNotIn("buganizer:578524618", bugs)
    rev_badges = state["items"]["gerrit:8663938@cid-rev"]["linked_bugs"]
    self.assertEqual(len(rev_badges), 1)
    self.assertEqual(rev_badges[0]["short_id"], "crbug/578524618")
    self.assertEqual(rev_badges[0]["priority"], "P1")

    # Unassigned bug linked to self-authored CL is retained and inherits SkoreText project + title
    self.assertIn("buganizer:590000001", bugs)
    self.assertEqual(bugs["buganizer:590000001"]["project_name"], "SkoreText")
    self.assertIn("Fix CoreText axis clamping", bugs["buganizer:590000001"]["title"])

    # Colleague-assigned bug without self assignee, collaborator, or self-CL is excluded
    self.assertNotIn("buganizer:576507252", bugs)
    self.assertIn("buganizer:576664769", bugs)

    # Chromium Gerrit URL host is preserved during parse_buganizer_blocks
    parsed_extra = w.parse_buganizer_blocks(extra_out)
    self.assertIn(
        "chromium-review.googlesource.com",
        parsed_extra[0]["gerrit_cl_urls"]["8660604"],
    )

  def test_flight_deck_amber_high_contrast(self) -> None:
    html = w.DASHBOARD_HTML

    def wcag_contrast(hex_a: str, hex_b: str) -> float:
      weights = (0.2126, 0.7152, 0.0722)
      lum_pair = [
          sum(
              w * (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4)
              for w, c in zip(weights, (int(hx[k : k + 2], 16) / 255.0 for k in (0, 2, 4)))
          )
          for hx in (hex_a.lstrip("#"), hex_b.lstrip("#"))
      ]
      return (max(lum_pair) + 0.05) / (min(lum_pair) + 0.05)

    # Light mode :root must use burnt amber (#b45309, >= 4.5:1 on #f4f4f6 and #fbfbfd)
    self.assertFalse(
        "#ff9f0a" in html.lower(),
        "Low-contrast #ff9f0a must not appear in DASHBOARD_HTML",
    )
    light_root = re.search(r":root\s*\{([^}]*)\}", html)
    self.assertIsNotNone(light_root)
    light_amber = re.search(r"--amber:\s*(#[0-9a-fA-F]{6});", light_root.group(1))
    self.assertIsNotNone(light_amber)
    light_hex = light_amber.group(1).lower()
    self.assertEqual(light_hex, "#b45309")
    self.assertGreaterEqual(wcag_contrast(light_hex, "#f4f4f6"), 4.5)
    self.assertGreaterEqual(wcag_contrast(light_hex, "#fbfbfd"), 4.5)

    # Dark mode @media (prefers-color-scheme: dark) :root must override --amber for dark cards
    dark_root = re.search(
        r"@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}",
        html,
    )
    self.assertIsNotNone(dark_root)
    dark_amber = re.search(r"--amber:\s*(#[0-9a-fA-F]{6});", dark_root.group(1))
    self.assertIsNotNone(dark_amber)
    dark_hex = dark_amber.group(1).lower()
    self.assertEqual(dark_hex, "#fbbf24")
    self.assertGreaterEqual(wcag_contrast(dark_hex, "#19191d"), 8.0)

  def test_secondary_section_single_select_switcher_and_poll_persistence(self) -> None:
    js_code = r"""
const assert = require('assert');
const vm = require('vm');
const html = require('fs').readFileSync(0, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Script block found');
const scriptContent = scriptMatch[1].split('\nboot();')[0];

const elements = {};
function getEl(id) {
  if (!elements[id]) elements[id] = { innerHTML: '', textContent: '', style: {} };
  return elements[id];
}

global.document = {
  getElementById: getEl,
  addEventListener: () => {},
  documentElement: { classList: { add: () => {} } },
};
global.window = {};
global.location = { hash: '', search: '', reload: () => {} };
global.history = { replaceState: () => {} };
global.navigator = { clipboard: { writeText: async () => {} } };

const harness = `
cachedData = {
  items: {
    'gerrit:1': {
      item_key: 'gerrit:1', target_id: '1', kind: 'gerrit', project_name: 'Skia',
      status: 'NEW', in_my_attention: true, patchset: 1, subject: 'Needs you item',
      url: 'https://example.com/1',
    },
    'gerrit:2': {
      item_key: 'gerrit:2', target_id: '2', kind: 'gerrit', project_name: 'Skia',
      status: 'NEW', is_draft: true, subject: 'Draft item on desk',
      url: 'https://example.com/2',
    },
    'gerrit:3': {
      item_key: 'gerrit:3', target_id: '3', kind: 'gerrit', project_name: 'Skia',
      status: 'NEW', waiting_on: [{id: 'peer@google.com', active: true}],
      subject: 'Waiting item', url: 'https://example.com/3',
    },
    'gerrit:4': {
      item_key: 'gerrit:4', target_id: '4', kind: 'gerrit', project_name: 'Skia',
      status: 'NEW', is_watching: true, is_self: false,
      subject: 'Watching item', url: 'https://example.com/4',
    },
    'gerrit:5': {
      item_key: 'gerrit:5', target_id: '5', kind: 'gerrit', project_name: 'Skia',
      status: 'MERGED', submitted_ts: Date.now() / 1000 - 86400 * 10,
      subject: 'Old done item', url: 'https://example.com/5',
    },
    'gerrit:6': {
      item_key: 'gerrit:6', target_id: '6', kind: 'gerrit', project_name: 'Skia',
      status: 'NEW', is_self: true, patchset: 2, subject: 'Ping item',
      url: 'https://example.com/6',
      reviewer_pings: [{
        reviewer: 'peer@google.com', reviewer_name: 'Peer',
        reason: 'lost_on_rebase', prev_ps: 1, current_ps: 2,
        had_approval: true, in_attention_set: false,
      }],
    },
  },
  bugs: {
    'buganizer:10': {
      bug_key: 'buganizer:10', short_id: 'b/10', title: 'Needs you bug',
      project_name: 'Skia', lane: 'you', status: 'ASSIGNED', priority: 'P1',
    },
    'buganizer:11': {
      bug_key: 'buganizer:11', short_id: 'b/11', title: 'Assigned active bug',
      project_name: 'Skia', lane: 'assigned', status: 'ASSIGNED', priority: 'P2',
      is_stale_backlog: false,
    },
    'buganizer:12': {
      bug_key: 'buganizer:12', short_id: 'b/12', title: 'Stale backlog bug',
      project_name: 'Skia', lane: 'assigned', status: 'ASSIGNED', priority: 'P3',
      is_stale_backlog: true,
    },
  },
};

render();

assert.strictEqual(getEl('subline').textContent, '', 'Subline must be cleared');
const lanesHtml = getEl('lanes').innerHTML;
assert(lanesHtml.includes('sec-tabs'), 'Must render secondary section tabs bar');
assert(lanesHtml.includes('Reviewer Pings'), 'Must render Reviewer Pings tab');
assert(lanesHtml.includes('On your desk'), 'Must render On your desk tab');
assert(lanesHtml.includes('Waiting on others'), 'Must render Waiting on others tab');
assert(lanesHtml.includes('Watching'), 'Must render Watching tab');
assert(lanesHtml.includes('Done'), 'Must render Done tab');
assert(!lanesHtml.includes('>Landing<'), 'Empty Landing tab must be omitted');

assert.strictEqual(selectedSection.cls, null, 'Default selectedSection.cls must be null');
assert(!lanesHtml.includes('class="lane sec-panel open"'), 'No panel open by default');

toggleSection('desk');
assert.strictEqual(selectedSection.cls, 'desk');
const deskHtml = getEl('lanes').innerHTML;
assert(deskHtml.includes('data-section="desk"'), 'Must have desk panel');
assert(!deskHtml.includes('data-section="desk" hidden'), 'Desk panel must not be hidden');
assert(deskHtml.includes('data-section="waiting" hidden'), 'Waiting panel must be hidden');

toggleSection('waiting');
assert.strictEqual(selectedSection.cls, 'waiting');
const waitHtml = getEl('lanes').innerHTML;
assert(waitHtml.includes('data-section="desk" hidden'), 'Desk panel must now be hidden');
assert(!waitHtml.includes('data-section="waiting" hidden'), 'Waiting panel must be visible');

render();
assert.strictEqual(selectedSection.cls, 'waiting', 'Poll must preserve selected section');
assert(!getEl('lanes').innerHTML.includes('data-section="waiting" hidden'), 'Waiting still open');

toggleSection('waiting');
assert.strictEqual(selectedSection.cls, null, 'Toggling active section must collapse it');
assert(getEl('lanes').innerHTML.includes('data-section="waiting" hidden'), 'Waiting now hidden');

setView('bugs');
assert.strictEqual(selectedSection.bugs, null, 'Default selectedSection.bugs must be null');
const bugLanes = getEl('lanes').innerHTML;
assert(bugLanes.includes('Assigned'), 'Must render Assigned tab');
assert(bugLanes.includes('Backlog (>60d)'), 'Must render Backlog tab');
toggleSection('backlog');
assert.strictEqual(selectedSection.bugs, 'backlog');
assert(!getEl('lanes').innerHTML.includes('data-section="backlog" hidden'), 'Backlog open');
assert(getEl('lanes').innerHTML.includes('data-section="assigned" hidden'), 'Assigned hidden');
`;
vm.runInThisContext(scriptContent + '\n' + harness);
"""
    res = subprocess.run(
        ["node", "-e", js_code],
        input=w.DASHBOARD_HTML,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(
        res.returncode, 0, f"Node stdout: {res.stdout}\nStderr: {res.stderr}"
    )

  def test_bug_triage_prompt_and_wip_cl_lane_retention(self) -> None:
    now_ts = w._parse_ts("2026-11-03T12:00:00Z")

    # 1. Unacked urgent vulnerability with only WIP CLs stays in Needs You with WIP CL note
    bug_wip = {
        "bug_key": "buganizer:580999888",
        "source": "buganizer",
        "status": "NEW",
        "priority": "P1",
        "issue_type": "VULNERABILITY",
        "created_time": "2026-11-03T10:00:00Z",
        "linked_cls": [
            {"item_key": "gerrit:1414001", "status": "NEW", "is_wip": True},
        ],
    }
    w.classify_bug_status(bug_wip, now_ts=now_ts)
    self.assertTrue(bug_wip["needs_you"])
    self.assertEqual(bug_wip["lane"], "you")
    self.assertIn("WIP CL ready for review", bug_wip["why_needs_you"])

    # Once acknowledged, moves to has_cl
    bug_wip["acked_ts"] = now_ts
    w.classify_bug_status(bug_wip, now_ts=now_ts)
    self.assertFalse(bug_wip["needs_you"])
    self.assertEqual(bug_wip["lane"], "has_cl")

    # If linked CL is not WIP, moves out of unacked_urgent directly to has_cl
    bug_active_cl = {
        "bug_key": "buganizer:580999889",
        "source": "buganizer",
        "status": "NEW",
        "priority": "P1",
        "issue_type": "VULNERABILITY",
        "created_time": "2026-11-03T10:00:00Z",
        "linked_cls": [
            {"item_key": "gerrit:1414002", "status": "NEW", "is_wip": False},
        ],
    }
    w.classify_bug_status(bug_active_cl, now_ts=now_ts)
    self.assertFalse(bug_active_cl["needs_you"])
    self.assertEqual(bug_active_cl["lane"], "has_cl")

    # 2. _start_chat_for_bug generates bug-triage and Proof-by-WIP-CL prompt
    state = {
        "bugs": {
            "buganizer:580999888": {
                "bug_key": "buganizer:580999888",
                "id": "580999888",
                "title": "Crash on layout",
                "status": "NEW",
                "priority": "P1",
                "issue_type": "VULNERABILITY",
                "project_name": "Skia",
            },
        },
        "items": {},
    }
    captured_kwargs: dict[str, object] = {}

    def fake_start(**kwargs: object) -> str:
      captured_kwargs.update(kwargs)
      return "cid-bug-new"

    with mock.patch.object(w, "discover_bug_chats", return_value={}), mock.patch.object(
        w.cl, "start_conversation_exact", side_effect=fake_start
    ), mock.patch.object(w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}):
      res = w._start_chat_for_bug(state, "buganizer:580999888", save=False)
      self.assertTrue(res["ok"])
      self.assertTrue(res["started"])
      prompt = str(captured_kwargs.get("prompt") or "")
      self.assertIn("bug-triage", prompt)
      self.assertIn("WIP CL", prompt)

  def test_sync_bugs_throttled_autostart_needs_you(self) -> None:
    now_ts = w._parse_ts("2026-11-03T12:00:00Z")
    state: dict[str, object] = {
        "items": {},
        "bugs": {},
        "review_claims": {},
        "events": [],
    }
    open_out = (
        "Issue ID: 580955594\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: NEW\n"
        "Priority: P0\n"
        "Issue Type: BUG\n"
        "Title: Top #1 crash in SkScalerContextProxy\n"
        "Created Time: 2026-11-02T18:39:06Z\n"
        "Modified Time: 2026-11-03T10:00:00Z\n"
    )
    started_keys: list[str] = []

    def fake_start(
        s: dict[str, object], bkey: str, *, save: bool = True
    ) -> dict[str, object]:
      started_keys.append(bkey)
      b = (s.get("bugs") or {}).get(bkey)
      if isinstance(b, dict):
        b["conversation_id"] = "cid-auto-bug"
      return {"ok": True, "started": True, "conversation_id": "cid-auto-bug"}

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=[open_out, ""]
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value={}
    ), mock.patch.object(
        w, "_start_chat_for_bug", side_effect=fake_start
    ):
      w.sync_bugs_into_state(
          state, force=True, wake_chats=True, proj_names={"proj": "Skia"}
      )

    self.assertEqual(len(started_keys), 1)
    self.assertEqual(started_keys[0], "buganizer:580955594")
    self.assertTrue(
        any("bug:buganizer:580955594" in k for k in state["review_claims"])
    )
    self.assertTrue(
        any("Auto-started bug triage" in ev["message"] for ev in state["events"])
    )

  def test_fetch_github_bugs_gh_2_46_fields_and_closed_state_reason(self) -> None:
    gh_calls: list[list[str]] = []

    def fake_gh(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      gh_calls.append(list(cmd))
      # Debian gh 2.46 rejects stateReason in gh issue list/view --json with exit 1
      if "--json" in cmd:
        fields_arg = cmd[cmd.index("--json") + 1]
        if "stateReason" in fields_arg.split(","):
          return subprocess.CompletedProcess(
              cmd, 1, "", 'Unknown JSON field: "stateReason"\n'
          )
      if cmd[:3] == ["gh", "issue", "list"]:
        sq = cmd[cmd.index("--search") + 1]
        if "is:open" in sq:
          payload = [{
              "number": 194931,
              "title": "[Windows][Impeller 3.47 SDF] CJK text rendering",
              "state": "OPEN",
              "author": {"login": "reporter1"},
              "assignees": [{"login": "Rusino"}],
              "labels": [{"name": "P2"}, {"name": "a: typography"}],
              "comments": [
                  {
                      "author": {"login": "Rusino"},
                      "createdAt": "2026-11-03T16:00:00Z",
                      "body": "Looking into this",
                  },
                  {
                      "author": {"login": "reporter1"},
                      "createdAt": "2026-11-03T17:36:50Z",
                      "body": "Assigned to Rusino with repro details",
                  },
              ],
              "url": "https://github.com/flutter/flutter/issues/194931",
              "createdAt": "2026-11-01T10:00:00Z",
              "updatedAt": "2026-11-03T17:36:50Z",
              "closedAt": None,
              "milestone": None,
          }]
        else:
          payload = [{
              "number": 194296,
              "title": "[WebParagraph] Duplicate issue",
              "state": "CLOSED",
              "author": {"login": "user2"},
              "assignees": [{"login": "Rusino"}],
              "labels": [],
              "comments": [],
              "url": "https://github.com/flutter/flutter/issues/194296",
              "createdAt": "2026-10-25T10:00:00Z",
              "updatedAt": "2026-10-30T10:00:00Z",
              "closedAt": "2026-10-30T10:00:00Z",
              "milestone": None,
          }]
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload), "")
      if cmd[:3] == ["gh", "issue", "view"]:
        payload_one = {
            "number": 190001,
            "title": "Extra referenced closed fix",
            "state": "CLOSED",
            "author": {"login": "user3"},
            "assignees": [],
            "labels": [],
            "comments": [],
            "url": "https://github.com/flutter/flutter/issues/190001",
            "createdAt": "2026-10-01T10:00:00Z",
            "updatedAt": "2026-10-28T10:00:00Z",
            "closedAt": "2026-10-28T10:00:00Z",
            "milestone": None,
        }
        return subprocess.CompletedProcess(cmd, 0, json.dumps(payload_one), "")
      if cmd[:2] == ["gh", "api"] and "search/issues" in cmd:
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps({
                "items": [{"number": 194296, "state_reason": "not_planned"}]
            }),
            "",
        )
      if cmd[:2] == ["gh", "api"] and "repos/flutter/flutter/issues/190001" in cmd:
        return subprocess.CompletedProcess(
            cmd, 0, json.dumps({"number": 190001, "state_reason": "completed"}), ""
        )
      return subprocess.CompletedProcess(cmd, 1, "", "unexpected command")

    def fake_check_output(cmd: list[str], *args: object, **kwargs: object) -> str:
      res = fake_gh(cmd, *args, **kwargs)
      if res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr
        )
      return res.stdout

    errs: list[str] = []
    with mock.patch.object(
        w.subprocess, "run", side_effect=fake_gh
    ), mock.patch.object(
        w.subprocess, "check_output", side_effect=fake_check_output
    ):
      bugs = w.fetch_github_bugs(extra_issue_nums={"190001"}, errors_out=errs)

    self.assertEqual(errs, [])
    by_key = {b["bug_key"]: b for b in bugs}
    self.assertIn("gh_issue:flutter/flutter#194931", by_key)
    open_b = by_key["gh_issue:flutter/flutter#194931"]
    self.assertTrue(open_b["from_self_query"])
    self.assertEqual(open_b["last_human_comment_author"], "reporter1")
    self.assertEqual(open_b["last_self_comment_at"], "2026-11-03T16:00:00Z")

    not_planned = by_key["gh_issue:flutter/flutter#194296"]
    self.assertEqual(not_planned["state_reason"], "NOT_PLANNED")
    w.classify_bug_status(not_planned)
    self.assertEqual(not_planned["lane"], "closed")

    completed_extra = by_key["gh_issue:flutter/flutter#190001"]
    self.assertFalse(completed_extra["from_self_query"])
    self.assertEqual(completed_extra["state_reason"], "COMPLETED")
    w.classify_bug_status(completed_extra)
    self.assertEqual(completed_extra["lane"], "fixed")

  def test_github_bug_fetch_failure_logs_stderr_surfaces_error_and_keeps_cache(
      self,
  ) -> None:
    def failing_gh(
        cmd: list[str], *args: object, **kwargs: object
    ) -> subprocess.CompletedProcess[str]:
      del args, kwargs
      # Simulate partial failure: open-issues search fails while closed search succeeds
      if cmd[:3] == ["gh", "issue", "list"] and any("is:closed" in a for a in cmd):
        return subprocess.CompletedProcess(
            cmd,
            0,
            json.dumps([{
                "number": 194296,
                "title": "[WebParagraph] Closed duplicate",
                "state": "CLOSED",
                "author": {"login": "user2"},
                "assignees": [{"login": "Rusino"}],
                "labels": [],
                "comments": [],
                "url": "https://github.com/flutter/flutter/issues/194296",
                "createdAt": "2026-10-20T10:00:00Z",
                "updatedAt": "2026-10-25T10:00:00Z",
                "closedAt": "2026-10-25T10:00:00Z",
                "milestone": None,
            }]),
            "",
        )
      return subprocess.CompletedProcess(
          cmd, 1, "", 'Unknown JSON field: "stateReason"\n'
      )

    def failing_check_output(
        cmd: list[str], *args: object, **kwargs: object
    ) -> str:
      res = failing_gh(cmd, *args, **kwargs)
      if res.returncode != 0:
        raise subprocess.CalledProcessError(
            res.returncode, cmd, output=res.stdout, stderr=res.stderr
        )
      return res.stdout

    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "gh_issue:flutter/flutter#194931": {
                "bug_key": "gh_issue:flutter/flutter#194931",
                "source": "github",
                "id": "194931",
                "short_id": "flutter#194931",
                "status": "OPEN",
                "priority": "P2",
                "assignee": "Rusino",
                "from_self_query": True,
                "title": "[Windows][Impeller 3.47 SDF] CJK text rendering",
                "created_time": "2026-11-01T10:00:00Z",
                "modified_time": "2026-11-03T17:36:50Z",
                "modified_ts": w._parse_ts("2026-11-03T17:36:50Z"),
                "linked_cls": [],
            },
            "gh_issue:flutter/flutter#194296": {
                "bug_key": "gh_issue:flutter/flutter#194296",
                "source": "github",
                "id": "194296",
                "short_id": "flutter#194296",
                "status": "CLOSED",
                "state_reason": "NOT_PLANNED",
                "priority": "P2",
                "assignee": "Rusino",
                "from_self_query": True,
                "title": "[WebParagraph] Closed duplicate",
                "created_time": "2026-10-20T10:00:00Z",
                "modified_time": "2026-10-25T10:00:00Z",
                "modified_ts": w._parse_ts("2026-10-25T10:00:00Z"),
                "linked_cls": [],
            },
        },
        "review_claims": {},
        "events": [],
    }
    err_buf = io.StringIO()
    with contextlib.redirect_stderr(err_buf), mock.patch.object(
        w.subprocess, "run", side_effect=failing_gh
    ), mock.patch.object(
        w.subprocess, "check_output", side_effect=failing_check_output
    ), mock.patch.object(
        w, "call_buganizer_mcp_batch", return_value=["", ""]
    ), mock.patch.object(
        w, "fetch_buganizer_fallback", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value={}
    ):
      w.sync_bugs_into_state(
          state, force=True, wake_chats=False, proj_names={"p": "SkParagraph"}
      )

    self.assertIn("stateReason", err_buf.getvalue())
    sync_errs = state.get("bug_sync_errors")
    self.assertIsInstance(sync_errs, list)
    self.assertTrue(any("stateReason" in str(e) for e in sync_errs or []))
    bugs_after = state.get("bugs") or {}
    # Cached open GitHub issues survive partial gh CLI failures
    self.assertIn("gh_issue:flutter/flutter#194931", bugs_after)
    # Closed NOT_PLANNED state_reason survives transient gh api enrichment failure
    self.assertEqual(
        bugs_after["gh_issue:flutter/flutter#194296"]["state_reason"],
        "NOT_PLANNED",
    )
    self.assertEqual(
        bugs_after["gh_issue:flutter/flutter#194296"]["lane"],
        "closed",
    )
    self.assertIn("bug_sync_errors", w.DASHBOARD_HTML)

  def test_cl_only_bug_old_comment_and_umbrella_cl_stay_out_of_needs_you(
      self,
  ) -> None:
    now_ts = w._parse_ts("2026-11-03T18:00:00Z")
    cl_ts = w._parse_ts("2026-10-25T10:00:00Z")
    bugs: dict[str, dict[str, object]] = {
        "gh_issue:flutter/flutter#130492": {
            "bug_key": "gh_issue:flutter/flutter#130492",
            "source": "github",
            "id": "130492",
            "short_id": "flutter#130492",
            "status": "OPEN",
            "priority": "P2",
            "assignee": "",
            "from_self_query": False,
            "is_collaborator": False,
            "is_mentioned": False,
            "created_time": "2025-07-01T10:00:00Z",
            "modified_time": "2026-09-10T10:00:00Z",
            "last_human_comment_author": "external-user",
            "last_human_comment_at": "2026-09-10T10:00:00Z",
            "last_human_comment_snippet": "Still reproducible on master",
            "last_self_comment_at": "",
        },
        "gh_issue:flutter/flutter#141080": {
            "bug_key": "gh_issue:flutter/flutter#141080",
            "source": "github",
            "id": "141080",
            "short_id": "flutter#141080",
            "status": "OPEN",
            "priority": "P2",
            "assignee": "",
            "from_self_query": False,
            "created_time": "2025-08-01T10:00:00Z",
            "modified_time": "2026-08-15T10:00:00Z",
            "last_human_comment_author": "external-user",
            "last_human_comment_at": "2026-08-15T10:00:00Z",
            "last_human_comment_snippet": "Related to trailing spaces",
            "last_self_comment_at": "",
        },
        "gh_issue:flutter/flutter#144099": {
            "bug_key": "gh_issue:flutter/flutter#144099",
            "source": "github",
            "id": "144099",
            "short_id": "flutter#144099",
            "status": "OPEN",
            "priority": "P2",
            "assignee": "",
            "from_self_query": False,
            "created_time": "2025-08-02T10:00:00Z",
            "modified_time": "2026-08-16T10:00:00Z",
            "last_human_comment_author": "external-user",
            "last_human_comment_at": "2026-08-16T10:00:00Z",
            "last_human_comment_snippet": "Another trailing space repro",
            "last_self_comment_at": "",
        },
    }
    items: dict[str, dict[str, object]] = {
        "gerrit:1411657@": {
            "item_key": "gerrit:1411657",
            "kind": "gerrit",
            "target_id": "1411657",
            "status": "NEW",
            "is_self": True,
            "is_wip": False,
            "activity_ts": cl_ts,
            "linked_bug_keys": ["gh_issue:flutter/flutter#130492"],
            "project_name": "SkParagraph",
        },
        "gerrit:1389816@cid-umbrella": {
            "item_key": "gerrit:1389816",
            "kind": "gerrit",
            "target_id": "1389816",
            "status": "NEW",
            "is_self": True,
            "is_wip": True,
            "activity_ts": cl_ts,
            "conversation_id": "cid-umbrella",
            "chat_archived": False,
            "chat_busy": False,
            "linked_bug_keys": [
                "gh_issue:flutter/flutter#141080",
                "gh_issue:flutter/flutter#144099",
            ],
            "project_name": "SkParagraph",
        },
    }
    w.link_bugs_and_cls(bugs, items, bug_chats={})

    b130 = bugs["gh_issue:flutter/flutter#130492"]
    w.classify_bug_status(b130, now_ts=now_ts)
    self.assertFalse(b130["needs_you"])
    self.assertEqual(b130["lane"], "has_cl")

    # Unrelated post-CL chatter on an unassigned CL-only issue stays in Has CL
    b130["last_human_comment_at"] = "2026-11-01T12:00:00Z"
    b130["last_human_comment_snippet"] = "+1 any update on stable cherry-pick?"
    w.classify_bug_status(b130, now_ts=now_ts)
    self.assertFalse(b130["needs_you"])
    self.assertEqual(b130["lane"], "has_cl")

    # Explicit @Rusino mention after non_wip_self_cl_ts promotes the issue to Needs You
    b130["last_human_comment_snippet"] = "@Rusino this breaks RTL cursor movement"
    w.classify_bug_status(b130, now_ts=now_ts)
    self.assertTrue(b130["needs_you"])
    self.assertEqual(b130["lane"], "you")

    # Multi-bug umbrella CL propagates project_name without stamping or hijacking its chat
    b141 = bugs["gh_issue:flutter/flutter#141080"]
    self.assertFalse(b141.get("conversation_id"))
    self.assertEqual(b141.get("project_name"), "SkParagraph")
    state = {"bugs": bugs, "items": items}
    with mock.patch.object(w, "discover_bug_chats", return_value={}), mock.patch.object(
        w.cl, "resolve_project_id", return_value="proj-skp"
    ), mock.patch.object(
        w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
    ), mock.patch.object(
        w.cl, "start_conversation_exact", return_value="cid-141080-own"
    ):
      res = w._start_chat_for_bug(state, "gh_issue:flutter/flutter#141080", save=False)
    self.assertTrue(res.get("started"))
    self.assertEqual(res.get("conversation_id"), "cid-141080-own")

  def test_should_autostart_bug_triage_skips_active_non_wip_cl_only(self) -> None:
    base_bug: dict[str, object] = {
        "bug_key": "buganizer:580955594",
        "source": "buganizer",
        "status": "ASSIGNED",
        "priority": "P0",
        "assignee": "jlavrova@google.com",
        "needs_you": True,
        "why_needs_you": "New comment from nehete@google.com",
        "conversation_id": "",
    }
    self.assertTrue(w.should_autostart_bug_triage({**base_bug, "linked_cls": []}))
    # Historical comment CL references ("LINKED") and abandoned CLs do not block triage autostart
    self.assertTrue(
        w.should_autostart_bug_triage({
            **base_bug,
            "linked_cls": [{"item_key": "gerrit:1116616", "status": "LINKED"}],
        })
    )
    self.assertTrue(
        w.should_autostart_bug_triage({
            **base_bug,
            "linked_cls": [
                {"item_key": "gerrit:1400001", "status": "ABANDONED", "is_wip": False}
            ],
        })
    )
    self.assertFalse(
        w.should_autostart_bug_triage({
            **base_bug,
            "linked_cls": [
                {"item_key": "gerrit:1414002", "status": "NEW", "is_wip": False}
            ],
        })
    )

  def test_sync_bugs_autostarts_p0_first_with_deduped_busy_reviewer_and_bug_chats(
      self,
  ) -> None:
    state: dict[str, object] = {
        "items": {
            "gerrit:1415001@cid-self-1": {
                "item_key": "gerrit:1415001",
                "kind": "gerrit",
                "target_id": "1415001",
                "status": "NEW",
                "is_self": True,
                "conversation_id": "cid-self-1",
                "chat_busy": True,
            },
            "gerrit:1415002@cid-self-2": {
                "item_key": "gerrit:1415002",
                "kind": "gerrit",
                "target_id": "1415002",
                "status": "NEW",
                "is_self": True,
                "conversation_id": "cid-self-2",
                "chat_busy": True,
            },
            "gerrit:8663799@cid-rev-shared": {
                "item_key": "gerrit:8663799",
                "kind": "gerrit",
                "target_id": "8663799",
                "status": "NEW",
                "is_self": False,
                "in_my_attention": True,
                "reviewer_emails": ["jlavrova@google.com"],
                "conversation_id": "cid-rev-shared",
                "chat_busy": True,
            },
            "gerrit:8663827@cid-rev-shared": {
                "item_key": "gerrit:8663827",
                "kind": "gerrit",
                "target_id": "8663827",
                "status": "NEW",
                "is_self": False,
                "in_my_attention": True,
                "reviewer_emails": ["jlavrova@google.com"],
                "conversation_id": "cid-rev-shared",
                "chat_busy": True,
            },
        },
        "bugs": {},
        "review_claims": {},
        "events": [],
    }
    open_out = (
        "Issue ID: 580000222\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: BUG\n"
        "Title: Newer P2 ligature issue\n"
        "Created Time: 2026-11-01T10:00:00Z\n"
        "Modified Time: 2026-11-03T18:30:00Z\n"
        "Major Modified Time: 2026-11-03T18:30:00Z\n\n"
        "---\n\n"
        "Issue ID: 580955594\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: NEW\n"
        "Priority: P0\n"
        "Issue Type: BUG\n"
        "Title: Top #1 crash in SkScalerContextProxy\n"
        "Created Time: 2026-11-02T18:39:06Z\n"
        "Modified Time: 2026-11-03T15:00:00Z\n"
        "Major Modified Time: 2026-11-03T15:00:00Z\n"
    )
    rendered_p2 = json.dumps({
        "result": (
            "Issue ID: 580000222\n"
            "Comment #2 by gaaclarke@google.com at 2026-11-03T18:30:00Z:\n"
            "Can you take a look at this ligature issue?\n"
        )
    })
    rendered_p0 = json.dumps({
        "result": (
            "Issue ID: 580955594\n"
            "Comment #2 by nehete@google.com at 2026-11-03T15:00:00Z:\n"
            "Top #1 gpu-process crash on Mac canary.\n"
        )
    })

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          outs.append(open_out if "status:open" in q and "-status:open" not in q else "")
        elif name == "render_issue_with_external":
          iid = str(args.get("issueId") or "")
          outs.append(rendered_p2 if iid == "580000222" else rendered_p0)
        else:
          outs.append("")
      return outs

    started_keys: list[str] = []

    def fake_start(
        s: dict[str, object], bkey: str, *, save: bool = True
    ) -> dict[str, object]:
      del save
      started_keys.append(bkey)
      b = (s.get("bugs") or {}).get(bkey)
      if isinstance(b, dict):
        b["conversation_id"] = f"cid-{bkey}"
        b["chat_busy"] = True
      return {"ok": True, "started": True, "conversation_id": f"cid-{bkey}"}

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value={}
    ), mock.patch.object(
        w, "_start_chat_for_bug", side_effect=fake_start
    ):
      w.sync_bugs_into_state(
          state, force=True, wake_chats=True, proj_names={"proj": "Skia"}
      )

    # Despite 2 busy self-CL chats and 2 records sharing 1 reviewer chat, P0 starts first
    self.assertEqual(started_keys, ["buganizer:580955594"])


if __name__ == "__main__":
  unittest.main()
 
