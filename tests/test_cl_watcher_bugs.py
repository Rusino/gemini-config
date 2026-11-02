#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for R3 Flight Deck Bugs view, sources, statuses, and bug-CL linking."""

from __future__ import annotations

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
            "Comment #1 by jlavrova@google.com at 2026-10-29T12:00:00Z:\n"
            "Landing fix.\n\n---\n\n"
            "Comment #2 by dx-workflow-gitwatcher@prod.google.com at 2026-10-30T17:48:13Z:\n"
            "Project: skia\n"
        )
    })
    cinfo_bot = w.parse_rendered_issue_comments(bot_only)
    self.assertEqual(cinfo_bot["last_human_comment_author"], "jlavrova@google.com")

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
const lanesOut = getEl('lanes').innerHTML;
assert(focusOut.includes('crbug/579861086'), 'Bugs focus must render Needs You bug');
assert(focusOut.includes('CL 1413001'), 'Bug focus card must render subtask CL 1413001');
assert(focusOut.includes('CL 1413999'), 'Bug focus card must render subtask CL 1413999');
assert(focusOut.includes('ackBug('), 'Needs you bug must render Ack button');

// Shared chat cid-shared appears on parent bug, NOT duplicated on CL 1413001 subtask;
// separate chat cid-separate DOES appear on CL 1413999 subtask
const cidSharedOpenCount = focusOut.split('openChat(&quot;cid-shared&quot;').length - 1;
assert.strictEqual(cidSharedOpenCount, 1, 'Shared chat button must render once on parent bug');
const subtasksHtml = focusOut.split('<div class="bug-subtasks">')[1] || '';
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

if __name__ == "__main__":
  unittest.main()
