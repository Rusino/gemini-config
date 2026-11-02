#!/usr/bin/env python3
# Copyright 2026 Google LLC
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for R59 Flight Deck bug ownership scope, collaborator bugs, and CL links."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class ClWatcherBugScopeTest(unittest.TestCase):

  def _make_summary(
      self, title: str, project_id: str, archived: bool = False
  ) -> dict[str, object]:
    return {
        "annotations": {"title": title, "archived": archived},
        "trajectoryMetadata": {
            "projectId": project_id,
            "lastModifiedTime": "2026-11-02T17:00:00Z",
            "parentConversationId": "",
            "isBattleModeFork": False,
            "sourceMetadata": None,
        },
        "status": "CASCADE_RUN_STATUS_IDLE",
    }

  def test_discover_bug_chats_skips_all_meta_projects_except_unassigned(self) -> None:
    proj_names = {
        "p-ie": "Improving Experience",
        "p-rev": "Reviews of e-mails, meetings and such",
        "p-dig": "sad-ru-googlers-ai-digest",
        "p-per": "Personal",
        "p-cro": "Code Review organizer",
        "p-bc": "Breaking Chats",
        "p-fd": "Flight Deck",
        "p-skia": "Skia",
    }
    summaries = {
        "c-ie": self._make_summary(
            "[11:48] \u29bf b/580882306 Gerrit draft dedup guard", "p-ie"
        ),
        "c-rev": self._make_summary(
            "[10:15] \u29bf b/580882306 Chrome AI Discuss digest", "p-rev"
        ),
        "c-dig": self._make_summary(
            "[09:00] \u29bf b/580882306 daily summary", "p-dig", archived=True
        ),
        "c-per": self._make_summary(
            "[08:00] \u29bf b/580403511 quota note", "p-per"
        ),
        "c-cro": self._make_summary(
            "[07:30] \u29bf crbug/580882306 triage sweep", "p-cro"
        ),
        "c-bc": self._make_summary(
            "[07:00] \u29bf b/580882306 experiment", "p-bc"
        ),
        "c-fd": self._make_summary(
            "[12:33] \u29bf b/580882306 false positive in Bugs", "p-fd"
        ),
        "c-skia": self._make_summary(
            "[15:21] \u29bf crbug/576664769 SkDistanceFieldGen overflow", "p-skia"
        ),
        "c-unassigned": self._make_summary(
            "[15:30] \u29bf crbug/580852205 collaborator review", ""
        ),
    }
    with mock.patch.object(
        w.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
    ), mock.patch.object(w, "load_project_names", return_value=proj_names):
      active = w.discover_bug_chats(include_archived=False)
      with_archived = w.discover_bug_chats(include_archived=True)

    self.assertNotIn("buganizer:580882306", active)
    self.assertNotIn("buganizer:580882306", with_archived)
    self.assertNotIn("buganizer:580403511", active)
    self.assertIn("buganizer:576664769", active)
    self.assertEqual(active["buganizer:576664769"]["conversation_id"], "c-skia")
    self.assertIn("buganizer:580852205", active)
    self.assertEqual(active["buganizer:580852205"]["conversation_id"], "c-unassigned")

  def test_parse_rendered_issue_comments_ignores_third_party_description_cl_urls(
      self,
  ) -> None:
    rendered = json.dumps({
        "result": (
            "Issue ID: 580882306\n"
            "Status: NEW\n"
            "Description by arthurmilchior@google.com at 2026-11-02T15:09:51Z:\n"
            "Observed on https://chromium-review.git.corp.google.com/c/"
            "chromium/tools/depot_tools/+/8704639 while replying.\n\n---\n\n"
            "Comment #1 by reporter@google.com at 2026-11-02T15:20:00Z:\n"
            "Bisected to https://skia-review.googlesource.com/c/skia/+/1399999\n\n---\n\n"
            "Comment #2 by dx-workflow-gitwatcher@prod.google.com at 2026-11-02T15:25:00Z:\n"
            "Link: https://skia-review.googlesource.com/c/skia/+/1411716\n\n---\n\n"
            "Comment #3 by jlavrova@google.com at 2026-11-02T15:30:00Z:\n"
            "Follow-up in https://skia-review.googlesource.com/c/skia/+/1411800\n"
        )
    })
    cinfo = w.parse_rendered_issue_comments(rendered)
    self.assertNotIn("8704639", cinfo["comment_cl_ids"])
    self.assertNotIn("1399999", cinfo["comment_cl_ids"])
    self.assertEqual(cinfo["comment_cl_ids"], ["1411716", "1411800"])

  def test_sync_bugs_retains_collaborator_excludes_non_self_and_prunes_ttl_cache(
      self,
  ) -> None:
    open_out = (
        "Issue ID: 576664769\n"
        "Component ID: 1457031\n"
        "Assignee: jlavrova@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P1\n"
        "Title: Potential heap OOB write in SkDistanceFieldGen\n"
        "Modified Time: 2026-11-02T12:00:00Z\n\n---\n\n"
        "Issue ID: 580852205\n"
        "Component ID: 1456769\n"
        "Assignee: fmalita@chromium.org\n"
        "Reporter: fmalita@chromium.org\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Issue Type: FEATURE_REQUEST\n"
        "Title: Define consistent range semantics for ShapedText / TextMetrics\n"
        "Created Time: 2026-10-01T19:40:14Z\n"
        "Modified Time: 2026-11-02T19:40:18Z\n"
        "Custom Field: Component Tags (1222907)\n"
        "Custom Field Value: Blink>Canvas\n"
    )
    extra_out = (
        "Issue ID: 580882306\n"
        "Component ID: 75371\n"
        "Assignee: N/A\n"
        "Reporter: arthurmilchior@google.com\n"
        "Status: NEW\n"
        "Priority: P1\n"
        "Title: Invisible draft can be posted on your behalf\n\n---\n\n"
        "Issue ID: 576507252\n"
        "Component ID: 1457031\n"
        "Assignee: brettos@google.com\n"
        "Status: ASSIGNED\n"
        "Priority: P2\n"
        "Title: textToGlyphs return value inconsistency\n\n---\n\n"
        "Issue ID: 590000001\n"
        "Component ID: 1363359\n"
        "Assignee: N/A\n"
        "Status: NEW\n"
        "Priority: P2\n"
        "Title: CoreText axis clamping crash\n"
    )
    queries: list[str] = []
    rendered_ids: list[str] = []

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        if name == "get_bugs_with_external":
          q = str(args.get("query") or "")
          queries.append(q)
          if "status:open" in q and "-status:open" not in q:
            outs.append(open_out)
          elif "id:" in q:
            outs.append(extra_out)
          else:
            outs.append("")
        elif name == "render_issue_with_external":
          rendered_ids.append(str(args.get("issueId") or ""))
          outs.append("")
        else:
          outs.append("")
      return outs

    state: dict[str, object] = {
        "items": {
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
            }
        },
        "bugs": {
            "buganizer:580882306": {
                "bug_key": "buganizer:580882306",
                "source": "buganizer",
                "id": "580882306",
                "status": "NEW",
                "assignee": "N/A",
                "title": "Invisible draft can be posted on your behalf",
            },
            "buganizer:580852205": {
                "bug_key": "buganizer:580852205",
                "source": "buganizer",
                "id": "580852205",
                "status": "ASSIGNED",
                "assignee": "fmalita@chromium.org",
                "is_collaborator": True,
                "title": "Define consistent range semantics for ShapedText / TextMetrics",
            },
        },
        "last_bug_sync_ts": time.time(),
    }
    fake_bug_chats = {
        "buganizer:576507252": {
            "conversation_id": "cid-reassigned-triage",
            "title": "[14:00] \u29bf b/576507252 textToGlyphs inconsistency",
            "chat_archived": False,
            "chat_busy": False,
            "project_id": "proj-skia",
            "project_name": "Skia",
        }
    }

    proj_map = {
        "proj-skia": "Skia",
        "proj-skore": "SkoreText",
        "proj-tc": "TextClusters API",
    }
    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value=fake_bug_chats
    ):
      # Fast-path within 5-min TTL must evict cached non-self bugs while keeping collaborators
      w.sync_bugs_into_state(
          state,
          force=False,
          wake_chats=False,
          proj_names=proj_map,
      )
      self.assertNotIn("buganizer:580882306", state["bugs"])
      self.assertIn("buganizer:580852205", state["bugs"])

      # Full sync retains collaborator bugs in Needs you and drops non-self chatted bugs
      w.sync_bugs_into_state(
          state,
          force=True,
          wake_chats=False,
          proj_names=proj_map,
      )

    self.assertTrue(any("collaborator:jlavrova@google.com" in q for q in queries))
    self.assertNotIn("580882306", rendered_ids)
    self.assertNotIn("576507252", rendered_ids)

    bugs = state["bugs"]
    assert isinstance(bugs, dict)
    self.assertNotIn("buganizer:580882306", bugs)
    self.assertNotIn("buganizer:576507252", bugs)
    self.assertIn("buganizer:576664769", bugs)
    self.assertIn("buganizer:590000001", bugs)
    self.assertFalse(bugs["buganizer:590000001"].get("is_collaborator"))

    collab = bugs["buganizer:580852205"]
    self.assertTrue(collab["is_collaborator"])
    self.assertEqual(collab["lane"], "you")
    self.assertTrue(collab["needs_you"])
    self.assertIn("Collaborator (owner: fmalita)", collab["why_needs_you"])
    self.assertIn("needs your review / input", collab["why_needs_you"])

  def test_collaborator_bug_lifecycle_no_close_prompt_no_foreign_slo_and_review_chat_prompt(
      self,
  ) -> None:
    now_ts = w._parse_ts("2026-11-02T20:00:00Z")
    collab_bug: dict[str, object] = {
        "bug_key": "buganizer:580852205",
        "source": "buganizer",
        "id": "580852205",
        "short_id": "crbug/580852205",
        "url": "https://issues.chromium.org/issues/580852205",
        "status": "ASSIGNED",
        "priority": "P2",
        "issue_type": "FEATURE_REQUEST",
        "assignee": "fmalita@chromium.org",
        "is_collaborator": True,
        "title": "Define consistent range semantics for ShapedText / TextMetrics",
        "slo": {"closure_slo": "2026-11-01T00:00:00Z"},
        "linked_cls": [
            {"item_key": "gerrit:8660604", "status": "MERGED", "submitted_ts": now_ts - 600}
        ],
    }
    # Unreviewed collaborator bug enters Needs you without foreign Overdue or close prompt
    w.classify_bug_status(collab_bug, now_ts=now_ts)
    self.assertFalse(collab_bug["is_overdue"])
    self.assertEqual(collab_bug["lane"], "you")
    self.assertNotIn("ready to mark Fixed", str(collab_bug["why_needs_you"]))
    self.assertIn("Collaborator (owner: fmalita)", str(collab_bug["why_needs_you"]))

    # Once user comments on the bug, it leaves Needs you without prompting to mark Fixed
    collab_bug["last_self_comment_at"] = "2026-11-02T19:55:00Z"
    w.classify_bug_status(collab_bug, now_ts=now_ts)
    self.assertFalse(collab_bug["needs_you"])
    self.assertEqual(collab_bug["lane"], "has_cl")

    # Follow-up comment from owner puts it back into Needs you with Collaborator prefix
    collab_bug["last_human_comment_author"] = "fmalita@chromium.org"
    collab_bug["last_human_comment_at"] = "2026-11-02T19:59:00Z"
    collab_bug["last_human_comment_snippet"] = "Updated Option B WPTs, PTAL"
    w.classify_bug_status(collab_bug, now_ts=now_ts)
    self.assertEqual(collab_bug["lane"], "you")
    self.assertIn(
        "Collaborator · New comment from fmalita@chromium.org",
        str(collab_bug["why_needs_you"]),
    )

    # Start chat on collaborator bug uses review/input prompt rather than fix-implementation prompt
    captured: dict[str, object] = {}

    def fake_start(**kwargs: object) -> str:
      captured.update(kwargs)
      return "cid-collab-new"

    state = {"bugs": {"buganizer:580852205": dict(collab_bug)}, "items": {}}
    with mock.patch.object(w, "discover_bug_chats", return_value={}), mock.patch.object(
        w.cl, "resolve_project_id", return_value="proj-tc"
    ), mock.patch.object(
        w.cl, "resolve_handoff_model", return_value={"enum": "MODEL_PRO"}
    ), mock.patch.object(
        w.cl, "start_conversation_exact", side_effect=fake_start
    ), mock.patch.object(
        w, "save_watcher_state"
    ):
      res = w._start_chat_for_bug(state, "buganizer:580852205")
    self.assertTrue(res["ok"])
    self.assertIn("Collaborator", str(captured.get("prompt") or ""))

    # Dashboard HTML renders Collaborator badge in both focus card and row meta
    js_code = r"""
const fs = require('fs');
const assert = require('assert');
const html = fs.readFileSync(0, 'utf8');
const m = html.match(/<script>([\s\S]*?)<\/script>/);
let script = m[1].replace(/\nboot\(\);\s*$/, '\n');
global.window = { location: { hash: '' } };
global.location = global.window.location;
global.history = { replaceState: () => {} };
global.document = {
  getElementById: () => ({ style: {}, innerHTML: '', textContent: '' }),
  querySelectorAll: () => [],
  addEventListener: () => {},
  hidden: false,
};
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
global.setInterval = () => 1;
const fn = new Function(script + `\n
const b = {
  bug_key: 'buganizer:580852205',
  short_id: 'crbug/580852205',
  url: 'https://issues.chromium.org/issues/580852205',
  project_name: 'TextClusters API',
  priority: 'P2',
  issue_type: 'FEATURE_REQUEST',
  assignee: 'fmalita@chromium.org',
  is_collaborator: true,
  title: 'Define consistent range semantics',
  why_needs_you: 'Collaborator (owner: fmalita) — needs your review / input',
  linked_cls: [],
};
const card = bugFocusCard(b, {});
const row = bugRowHtml(b, {});
assert(card.includes('Collaborator'), 'Focus card must show Collaborator badge: ' + card);
assert(row.includes('Collaborator'), 'Bug row must show Collaborator meta: ' + row);
`);
fn();
"""
    node_res = subprocess.run(
        ["node", "-e", js_code],
        input=w.DASHBOARD_HTML,
        capture_output=True,
        text=True,
        check=False,
    )
    self.assertEqual(node_res.returncode, 0, f"Stderr: {node_res.stderr}")

  def test_infer_project_for_bug_maps_jetski_and_onequota_to_improving_experience(
      self,
  ) -> None:
    proj_names = {
        "p-skia": "Skia",
        "p-ie": "Improving Experience",
    }
    jetski_bug = {
        "bug_key": "buganizer:580403511",
        "source": "buganizer",
        "component_id": "2271566",
        "title": "[Action Required] Notice of Jetski Best-of-N Policy Violation",
    }
    pid, pname = w.infer_project_for_bug(jetski_bug, proj_names)
    self.assertEqual((pid, pname), ("p-ie", "Improving Experience"))

    # Stale default 'Skia' cached on a meta-project bug chat must heal on sync
    closed_out = (
        "Issue ID: 580403511\n"
        "Component ID: 2271566\n"
        "Assignee: jlavrova@google.com\n"
        "Reporter: onequota-bon-monitor@prod.google.com\n"
        "Status: FIXED\n"
        "Priority: P1\n"
        "Title: [Action Required] Notice of Jetski Best-of-N Policy Violation\n"
        "Modified Time: 2026-11-02T16:20:32Z\n"
    )
    state: dict[str, object] = {
        "items": {},
        "bugs": {
            "buganizer:580403511": {
                "bug_key": "buganizer:580403511",
                "source": "buganizer",
                "id": "580403511",
                "conversation_id": "cfcedab1-3456-46b9-8c3d-927f754e97e3",
                "chat_archived": True,
                "project_id": "p-skia",
                "project_name": "Skia",
            }
        },
    }

    def fake_mcp(calls: list[tuple[str, dict[str, object]]]) -> list[str]:
      outs: list[str] = []
      for name, args in calls:
        q = str(args.get("query") or "")
        outs.append(
            closed_out if name == "get_bugs_with_external" and "-status:open" in q else ""
        )
      return outs

    with mock.patch.object(
        w, "call_buganizer_mcp_batch", side_effect=fake_mcp
    ), mock.patch.object(
        w, "fetch_github_bugs", return_value=[]
    ), mock.patch.object(
        w, "discover_bug_chats", return_value={}
    ):
      w.sync_bugs_into_state(state, force=True, wake_chats=False, proj_names=proj_names)

    healed = state["bugs"]["buganizer:580403511"]
    self.assertEqual(
        healed["conversation_id"], "cfcedab1-3456-46b9-8c3d-927f754e97e3"
    )
    self.assertEqual(healed["project_id"], "p-ie")
    self.assertEqual(healed["project_name"], "Improving Experience")


if __name__ == "__main__":
  unittest.main()
