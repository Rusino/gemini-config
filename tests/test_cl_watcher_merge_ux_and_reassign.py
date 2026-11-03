#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for security merge Needs Me state machine, CL attachment, and reassign."""

from __future__ import annotations

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


def _sample_bug_blocks(
    *,
    merge_val: str = "Pending",
    assignee: str = "jlavrova@google.com",
    auto_cp: str = "Yes",
    parent_status: str = "FIXED",
) -> str:
  return (
      "Issue ID: 576664769\n"
      "Component ID: 1457031\n"
      "Assignee: jlavrova@google.com\n"
      "Reporter: vmiura@google.com\n"
      f"Status: {parent_status}\n"
      "Priority: P1\n"
      "Issue Type: VULNERABILITY\n"
      "Title: Potential heap OOB write via integer overflow in SkDistanceFieldGen\n"
      "Created Time: 2026-10-22T15:56:27.408Z\n"
      "Modified Time: 2026-11-03T16:51:05.242Z\n"
      "Gerrit changes: https://skia-review.git.corp.google.com/c/skia/+/1404196, "
      "https://skia-review.git.corp.google.com/c/skia/+/1411716, "
      "https://skia-review.git.corp.google.com/c/skia/+/1415456\n"
      "Severity: S1\n"
      "Custom Field: OS (1223084)\n"
      "Custom Field Value: Android, Linux, Windows, ChromeOS\n"
      "Custom Field: Fixed By Code Changes (1358989)\n"
      "Custom Field Value: https://skia-review.googlesource.com/c/skia/+/1404196\n"
      "\n---\n\n"
      + "\n---\n\n".join(
          f"Issue ID: {bid}\n"
          "Component ID: 1579812\n"
          f"Assignee: {assignee}\n"
          "Reporter: chrome-release-blintz-continuous-runner@prod.google.com\n"
          "Status: ASSIGNED\n"
          "Priority: P1\n"
          "Issue Type: VULNERABILITY\n"
          f"Title: [Merge M{ms}] Potential heap OOB write via integer overflow\n"
          "Created Time: 2026-11-03T08:13:28.708Z\n"
          "Modified Time: 2026-11-03T16:51:01.353Z\n"
          "Parent Issue IDs: 576664769\n"
          "Gerrit changes: https://skia-review.git.corp.google.com/c/skia/+/1404196\n"
          "Severity: S1\n"
          "Custom Field: Component Tags (1222907)\n"
          "Custom Field Value: Programs>PMO>Browser>Release>Merge Request\n"
          f"Custom Field: Merge (1223087)\n"
          f"Custom Field Value: {merge_val}\n"
          f"Custom Field: Merge Milestone (1340419)\n"
          f"Custom Field Value: {ms}\n"
          f"Custom Field: Auto Cherry Pick (1419876)\n"
          f"Custom Field Value: {auto_cp}\n"
          for bid, ms in (("581211219", 156), ("581211720", 157), ("581211756", 158))
      )
  )


class MergeUxAndReassignTest(unittest.TestCase):

  def setUp(self) -> None:
    super().setUp()
    self.now_ts = 1793725000.0

  def _parse_bugs(self, **kwargs: str) -> dict[str, dict]:
    rows = w.parse_buganizer_blocks(_sample_bug_blocks(**kwargs))
    return {b["bug_key"]: b for b in rows}

  def test_merge_state_machine_pending_commented_reassigned_and_manual_cp(
      self,
  ) -> None:
    bugs = self._parse_bugs(merge_val="Pending", assignee="jlavrova@google.com")
    m156 = bugs["buganizer:581211219"]
    m156["linked_cls"] = [{
        "item_key": "gerrit:1404196",
        "target_id": "1404196",
        "status": "MERGED",
        "branch": "main",
        "submitted_ts": self.now_ts - 86400,
    }]
    w.classify_bug_status(m156, now_ts=self.now_ts)
    self.assertTrue(m156["needs_you"])
    self.assertEqual(m156["lane"], "you")
    self.assertEqual(m156["merge_action_phase"], "confirm")
    self.assertEqual(m156["why_needs_you"], "M156 merge request pending — confirm")
    self.assertNotIn("ready to mark Fixed", m156["why_needs_you"])

    m156["last_self_comment_at"] = "2026-11-03T16:19:07Z"
    w.classify_bug_status(m156, now_ts=self.now_ts)
    self.assertTrue(m156["needs_you"])
    self.assertEqual(m156["lane"], "you")
    self.assertEqual(m156["merge_action_phase"], "reassign")
    self.assertEqual(
        m156["why_needs_you"],
        "M156 merge comment posted — reassign to merges@chromium.org",
    )

    reassigned = self._parse_bugs(
        merge_val="Pending", assignee="merges@chromium.org"
    )["buganizer:581211219"]
    reassigned["is_collaborator"] = True
    reassigned["linked_cls"] = m156["linked_cls"]
    w.classify_bug_status(reassigned, now_ts=self.now_ts)
    self.assertFalse(reassigned["needs_you"])
    self.assertEqual(reassigned["lane"], "has_cl")

    approved_back = self._parse_bugs(
        merge_val="Approved-156", assignee="jlavrova@google.com", auto_cp="No"
    )["buganizer:581211219"]
    approved_back["linked_cls"] = m156["linked_cls"]
    w.classify_bug_status(approved_back, now_ts=self.now_ts)
    self.assertTrue(approved_back["needs_you"])
    self.assertEqual(approved_back["lane"], "you")
    self.assertEqual(approved_back["merge_action_phase"], "manual_cherry_pick")
    self.assertIn("manual cherry-pick", approved_back["why_needs_you"])

  def test_merge_requests_never_trigger_bug_triage_autostart(self) -> None:
    bugs = self._parse_bugs(merge_val="Pending", assignee="jlavrova@google.com")
    for b in bugs.values():
      w.classify_bug_status(b, now_ts=self.now_ts)
    self.assertTrue(bugs["buganizer:581211219"]["needs_you"])
    self.assertFalse(w.should_autostart_bug_triage(bugs["buganizer:581211219"]))

  def test_cherry_pick_detection_retention_and_bidirectional_attachment(
      self,
  ) -> None:
    bugs = self._parse_bugs(
        merge_val="Approved-156", assignee="merges@chromium.org"
    )
    bugs["buganizer:581211219"]["gerrit_cl_ids"] = ["1404196", "1415456"]
    bugs["buganizer:581211720"]["gerrit_cl_ids"] = ["1404196", "1415277"]
    bugs["buganizer:581211756"]["gerrit_cl_ids"] = ["1404196", "1414478"]

    extra_gids = w.discover_merge_linked_gerrit_ids(bugs)
    self.assertEqual(
        set(extra_gids), {"1404196", "1415456", "1415277", "1414478"}
    )

    items = {
        "gerrit:1404196@unchatted": {
            "item_key": "gerrit:1404196",
            "kind": "gerrit",
            "target_id": "1404196",
            "host": "skia-review.googlesource.com",
            "url": "https://skia-review.googlesource.com/c/skia/+/1404196",
            "subject": "Fix integer overflow in SDF generation",
            "status": "MERGED",
            "branch": "main",
            "is_self": True,
            "project_name": "Skia",
            "revision": "3d70807b9548ec53b4d201afc3094fdfc73af572",
            "post_submit": {"all_passed": True, "has_alert": False},
        },
        "gerrit:1415456@unchatted": {
            "item_key": "gerrit:1415456",
            "kind": "gerrit",
            "target_id": "1415456",
            "host": "skia-review.googlesource.com",
            "url": "https://skia-review.googlesource.com/c/skia/+/1415456",
            "subject": "[M156] Fix integer overflow in SDF generation",
            "status": "NEW",
            "branch": "chrome/m156",
            "cherry_pick_of_change": "1404196",
            "cq_status": "CQ+2 (Submitting)",
            "phase": "SUBMITTING",
            "is_self": False,
            "project_name": "Skia",
        },
        "gerrit:1415277@unchatted": {
            "item_key": "gerrit:1415277",
            "kind": "gerrit",
            "target_id": "1415277",
            "host": "skia-review.googlesource.com",
            "url": "https://skia-review.googlesource.com/c/skia/+/1415277",
            "subject": "[M157] Fix integer overflow in SDF generation",
            "status": "MERGED",
            "branch": "chrome/m157",
            "commit_message": (
                "[M157] Fix\n\n(cherry picked from commit "
                "3d70807b9548ec53b4d201afc3094fdfc73af572)\n"
            ),
            "is_self": False,
            "project_name": "Skia",
        },
    }
    w.attach_merge_children_to_cls_and_bugs(bugs, items)
    parent_cl = items["gerrit:1404196@unchatted"]
    self.assertTrue(items["gerrit:1415456@unchatted"]["is_cherry_pick"])
    self.assertEqual(
        items["gerrit:1415456@unchatted"]["cherry_pick_of"], "gerrit:1404196"
    )
    self.assertEqual(
        items["gerrit:1415277@unchatted"]["cherry_pick_of"], "gerrit:1404196"
    )
    mc = parent_cl["merge_children"]
    self.assertEqual([c["milestone"] for c in mc], [156, 157, 158])
    self.assertEqual(mc[0]["cl_item_key"], "gerrit:1415456")
    self.assertEqual(mc[1]["cl_item_key"], "gerrit:1415277")
    self.assertEqual(mc[2]["bug_key"], "buganizer:581211756")
    self.assertTrue(parent_cl["has_active_merges"])
    self.assertEqual(
        [c["milestone"] for c in bugs["buganizer:576664769"]["merge_children"]],
        [156, 157, 158],
    )

  def test_reassign_batch_runs_sequential_mcp_phases_and_falls_back_to_single_bulk_url(
      self,
  ) -> None:
    bugs = self._parse_bugs(merge_val="Pending", assignee="jlavrova@google.com")
    mcp_batches: list[list[tuple[str, dict]]] = []

    def fake_mcp_ok(calls, timeout_sec=25.0):
      del timeout_sec
      mcp_batches.append(list(calls))
      if calls[0][0] in ("add_buganizer_comment", "safe_add_collaborator"):
        return ["Successfully updated" for _ in calls]
      if calls[0][0] == "safe_reassign_issue":
        return ["Successfully reassigned" for _ in calls]
      out = []
      for name, _ in calls:
        if name == "get_bugs_with_external":
          out.append(_sample_bug_blocks(merge_val="Pending", assignee="merges@chromium.org"))
        else:
          out.append('{"hasAccess": true}')
      return out

    with mock.patch.object(w, "call_buganizer_mcp_batch", side_effect=fake_mcp_ok):
      res_ok = w.reassign_security_merge_batch(
          bugs,
          ["581211219", "581211720", "581211756"],
          comment_markdown=(
              "Fix: https://skia-review.googlesource.com/c/skia/+/1404196. "
              "Requesting merge approval."
          ),
      )

    self.assertTrue(res_ok["ok"], res_ok)
    self.assertEqual(
        res_ok["reassigned_ids"], ["581211219", "581211720", "581211756"]
    )
    self.assertEqual(res_ok["failed_ids"], [])
    self.assertEqual(res_ok["bulk_reassign_url"], "")
    self.assertEqual(len(mcp_batches), 3)
    phase1_tools = {name for name, _ in mcp_batches[0]}
    self.assertIn("safe_add_collaborator", phase1_tools)
    self.assertNotIn("safe_reassign_issue", phase1_tools)
    self.assertIn(
        (
            "safe_add_collaborator",
            {"issueId": "581211219", "collaborator": "jlavrova@google.com"},
        ),
        mcp_batches[0],
    )
    phase2_tools = {name for name, _ in mcp_batches[1]}
    self.assertEqual(phase2_tools, {"safe_reassign_issue"})
    self.assertIn(
        (
            "safe_reassign_issue",
            {"issueId": "581211219", "assignee": "merges@chromium.org"},
        ),
        mcp_batches[1],
    )
    phase3_tools = {name for name, _ in mcp_batches[2]}
    self.assertEqual(phase3_tools, {"get_bugs_with_external", "get_user_access"})

    bugs_fail = self._parse_bugs(
        merge_val="Pending", assignee="jlavrova@google.com"
    )

    def fake_mcp_reassign_denied(calls, timeout_sec=25.0):
      del timeout_sec
      if calls[0][0] == "safe_reassign_issue":
        return [
            "FAILED_PRECONDITION: Cannot safely reassign issue: They lack edit access"
            for _ in calls
        ]
      if calls[0][0] == "get_bugs_with_external":
        return [
            _sample_bug_blocks(merge_val="Pending", assignee="jlavrova@google.com"),
            *['{"hasAccess": true}' for _ in calls[1:]],
        ]
      return ["Successfully updated" for _ in calls]

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp_reassign_denied
    ):
      res_fb = w.reassign_security_merge_batch(
          bugs_fail, ["581211219", "581211720", "581211756"]
      )

    self.assertTrue(res_fb["ok"], res_fb)
    self.assertEqual(res_fb["reassigned_ids"], [])
    self.assertEqual(
        res_fb["failed_ids"], ["581211219", "581211720", "581211756"]
    )
    self.assertEqual(
        res_fb["bulk_reassign_url"],
        "https://issues.chromium.org/issues?q=id:(581211219%20OR%20581211720%20OR%20581211756)",
    )

  def test_dashboard_groups_needs_me_and_renders_merges_strip_on_parent_cl(
      self,
  ) -> None:
    js_code = r"""
const assert = require('node:assert');
const fs = require('node:fs');
const html = fs.readFileSync(0, 'utf8');
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Missing script tag');

const els = {};
function getEl(id) {
  if (!els[id]) els[id] = {innerHTML: '', textContent: '', classList: {add() {}}};
  return els[id];
}
global.document = {
  getElementById: getEl,
  documentElement: {classList: {add() {}}},
  addEventListener() {},
};
global.window = {parent: null, addEventListener() {}, open() {}};
global.location = {search: '', hash: ''};
global.history = {replaceState() {}};
global.localStorage = {getItem() { return null; }, setItem() {}};
global.fetch = async () => ({ok: false});
global.setInterval = () => 0;

let code = scriptMatch[1].replace(/\bboot\(\);/, '');
const driver = () => String.raw`
cachedData = {
  last_sync: '12:55 ET',
  rollers_summary: {},
  items: {
    'gerrit:1404196@unchatted': {
      item_key: 'gerrit:1404196', kind: 'gerrit', target_id: '1404196',
      url: 'https://skia-review.googlesource.com/c/skia/+/1404196',
      subject: 'Fix integer overflow in SDF generation',
      status: 'MERGED', phase: 'READY_TO_CLOSE', is_self: true,
      project_name: 'Skia', submitted_ts: Date.now() / 1000 - 4 * 86400,
      post_submit: {all_passed: true, has_alert: false, rolls: {}},
      has_active_merges: true,
      linked_bugs: [{
        bug_key: 'buganizer:576664769', short_id: 'crbug/576664769',
        priority: 'P1', url: 'https://issues.chromium.org/issues/576664769',
      }],
      merge_children: [
        {milestone: 156, milestone_label: 'M156', bug_key: 'buganizer:581211219',
         bug_short_id: 'crbug/581211219', bug_url: 'https://issues.chromium.org/issues/581211219',
         merge_status: 'Approved-156', cl_item_key: 'gerrit:1415456', cl_label: 'CL 1415456',
         cl_url: 'https://skia-review.googlesource.com/c/skia/+/1415456',
         cl_status: 'NEW', cl_summary: 'CQ+2', cl_needs_you: false},
        {milestone: 157, milestone_label: 'M157', bug_key: 'buganizer:581211720',
         bug_short_id: 'crbug/581211720', bug_url: 'https://issues.chromium.org/issues/581211720',
         merge_status: 'Pending', cl_item_key: '', cl_label: '', cl_url: '',
         cl_status: '', cl_summary: '', cl_needs_you: false},
      ],
    },
    'gerrit:1415456@unchatted': {
      item_key: 'gerrit:1415456', kind: 'gerrit', target_id: '1415456',
      url: 'https://skia-review.googlesource.com/c/skia/+/1415456',
      subject: '[M156] Fix integer overflow in SDF generation',
      status: 'NEW', phase: 'SUBMITTING', is_self: false,
      is_cherry_pick: true, cherry_pick_of: 'gerrit:1404196',
      project_name: 'Skia',
    },
  },
  bugs: {
    'buganizer:580955594': {
      bug_key: 'buganizer:580955594', id: '580955594', short_id: 'crbug/580955594',
      title: 'GPU crash on Mac Canary', project_name: 'Skia', priority: 'P1',
      status: 'ASSIGNED', lane: 'you', needs_you: true,
      why_needs_you: 'New comment from nehete@google.com',
      url: 'https://issues.chromium.org/issues/580955594',
    },
    'buganizer:581211219': {
      bug_key: 'buganizer:581211219', id: '581211219', short_id: 'crbug/581211219',
      title: '[Merge M156] Fix integer overflow', project_name: 'Skia', priority: 'P1',
      status: 'ASSIGNED', lane: 'you', needs_you: true,
      is_merge_request_bug: true, parent_issue_id: '576664769', merge_milestone: 156,
      merge_action_phase: 'reassign',
      why_needs_you: 'M156 merge comment posted — reassign to merges@chromium.org',
      url: 'https://issues.chromium.org/issues/581211219',
    },
    'buganizer:581211720': {
      bug_key: 'buganizer:581211720', id: '581211720', short_id: 'crbug/581211720',
      title: '[Merge M157] Fix integer overflow', project_name: 'Skia', priority: 'P1',
      status: 'ASSIGNED', lane: 'you', needs_you: true,
      is_merge_request_bug: true, parent_issue_id: '576664769', merge_milestone: 157,
      merge_action_phase: 'reassign',
      why_needs_you: 'M157 merge comment posted — reassign to merges@chromium.org',
      url: 'https://issues.chromium.org/issues/581211720',
    },
  },
};

setView('cls');
const vis = visibleItemsFromData(cachedData);
assert.strictEqual(vis.length, 1, 'Cherry-pick CL must not duplicate as standalone row');
assert.strictEqual(laneOf(vis[0]), 'landing', 'Merged parent CL with active merges in Landing');
selectedSection.cls = 'landing';
render();
const lanesHtml = getEl('lanes').innerHTML;
assert(lanesHtml.includes('M156'), 'Parent CL must render M156 merge pill');
assert(lanesHtml.includes('CL 1415456'), 'Parent CL must link cherry-pick CL 1415456');
const swHtml = getEl('view-switch').innerHTML;
assert(swHtml.includes('Bugs <span class="v-you">● 2</span>'),
  '1 regular bug + 2 sibling merge bugs must count as ● 2 in Bugs badge: ' + swHtml);

setView('bugs');
assert(getEl('headline').innerHTML.includes('Two things need you.'),
  'Headline must count sibling merge bugs once: ' + getEl('headline').innerHTML);
const bugFocus = getEl('focus').innerHTML;
assert(bugFocus.includes('M156/M157'), 'Grouped merge focus card must list M156/M157: ' + bugFocus);
assert(bugFocus.includes('reassignMerge('), 'Reassign phase must render 1-click reassignMerge');
`;
eval(code + '\n' + driver());
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


if __name__ == "__main__":
  unittest.main()
