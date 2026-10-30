# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.expanduser("~/.gemini/config/scripts"))
import cl_watcher as w  # noqa: E402


class ClWatcherCompanionTest(unittest.TestCase):

  def test_external_stack_is_not_marked_companion(self):
    ext_owner = {"email": "external@author.com", "name": "External Author"}
    items = {
        "gerrit:1408156": {
            "item_key": "gerrit:1408156",
            "target_id": "1408156",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "is_external": True,
            "status": "NEW",
            "subject": "[skparagraph] Add text baseline metrics",
            "owner": ext_owner,
            "conversation_id": "c-ext-stack",
        },
        "gerrit:1408158": {
            "item_key": "gerrit:1408158",
            "target_id": "1408158",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "is_external": True,
            "status": "NEW",
            "subject": "[skparagraph] Expose baseline query in Paragraph",
            "owner": ext_owner,
            "conversation_id": "",
        },
    }
    discovered = list(items.values())
    with mock.patch.object(
        w, "fetch_gerrit_related_cl_ids", return_value={"1408156", "1408158"}
    ):
      w.link_companion_changes_to_parent_chats(discovered, items)

    self.assertNotIn("companion_to", items["gerrit:1408156"])
    self.assertNotIn("companion_to", items["gerrit:1408158"])
    self.assertNotIn("companions", items["gerrit:1408156"])

  def test_self_wip_stack_not_marked_companion_no_cycle(self):
    self_owner = {"email": "jlavrova@google.com", "name": "Julia Lavrova"}
    items = {
        "gerrit:8680531": {
            "item_key": "gerrit:8680531",
            "target_id": "8680531",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": True,
            "is_external": False,
            "status": "NEW",
            "subject": "Part 1: TextClusters data model",
            "owner": self_owner,
        },
        "gerrit:8680532": {
            "item_key": "gerrit:8680532",
            "target_id": "8680532",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": True,
            "is_external": False,
            "status": "NEW",
            "subject": "Part 2: TextClusters visual renderer",
            "owner": self_owner,
        },
    }
    discovered = list(items.values())
    with mock.patch.object(
        w, "fetch_gerrit_related_cl_ids", return_value={"8680531", "8680532"}
    ):
      w.link_companion_changes_to_parent_chats(discovered, items)

    self.assertNotIn("companion_to", items["gerrit:8680531"])
    self.assertNotIn("companion_to", items["gerrit:8680532"])

  def test_true_gerrit_and_github_companions_linked_and_noise_suppressed(self):
    items = {
        "gerrit:1409697": {
            "item_key": "gerrit:1409697",
            "target_id": "1409697",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "is_external": True,
            "status": "NEW",
            "patchset": 3,
            "subject": "[skparagraph] First line indent",
            "conversation_id": "c-parent-1409697",
        },
        "gerrit:1410416": {
            "item_key": "gerrit:1410416",
            "target_id": "1410416",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": True,
            "is_external": False,
            "status": "NEW",
            "patchset": 1,
            "subject": "[skparagraph] WIP companion for first line indent",
            "merge_conflict": True,
            "missing_reviewers_to_assign": 1,
            "missing_approvals": 1,
            "reviewer_status": "No reviewers assigned",
            "reviewer_pings": [{"reviewer": "someone@google.com", "active": True}],
            "cq_failed": True,
            "tree_red": True,
        },
        "github:195384": {
            "item_key": "github:195384",
            "target_id": "195384",
            "kind": "github",
            "is_self": False,
            "status": "OPEN",
            "subject": "[web_paragraph] BiDi multi-run layout",
            "conversation_id": "c-gh-195384",
        },
        "github:195386": {
            "item_key": "github:195386",
            "target_id": "195386",
            "kind": "github",
            "is_self": True,
            "status": "OPEN",
            "head_ref": "companion-pr-195384",
            "subject": "[web_paragraph] Contract test companion",
            "merge_conflict": True,
            "missing_reviewers_to_assign": 2,
        },
    }
    discovered = list(items.values())
    with mock.patch.object(
        w, "fetch_gerrit_related_cl_ids", return_value={"1409697", "1410416"}
    ):
      w.link_companion_changes_to_parent_chats(discovered, items)

    self.assertEqual(items["gerrit:1410416"]["companion_to"], "gerrit:1409697")
    self.assertEqual(items["github:195386"]["companion_to"], "github:195384")

    w.compute_companion_states(items, skia_dir="/nonexistent")

    comp_gerrit = items["gerrit:1410416"]
    self.assertTrue(comp_gerrit["is_companion"])
    self.assertFalse(comp_gerrit["merge_conflict"])
    self.assertEqual(comp_gerrit["missing_reviewers_to_assign"], 0)
    self.assertEqual(comp_gerrit["missing_approvals"], 0)
    self.assertEqual(comp_gerrit["reviewer_status"], "")
    self.assertEqual(comp_gerrit["reviewer_pings"], [])
    self.assertFalse(comp_gerrit["cq_failed"])
    self.assertFalse(comp_gerrit["tree_red"])

    comp_gh = items["github:195386"]
    self.assertTrue(comp_gh["is_companion"])
    self.assertFalse(comp_gh["merge_conflict"])
    self.assertEqual(comp_gh["missing_reviewers_to_assign"], 0)

  def test_ready_to_abandon_triggers(self):
    items = {
        "gerrit:1409697": {
            "item_key": "gerrit:1409697",
            "target_id": "1409697",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "status": "NEW",
            "patchset": 3,
            "self_approved": True,
            "self_approval_ps": 3,
            "revision_ps_by_sha": {"parent_sha_123": 2},
        },
        "gerrit:1410416": {
            "item_key": "gerrit:1410416",
            "target_id": "1410416",
            "kind": "gerrit",
            "is_self": True,
            "status": "NEW",
            "parent_commit": "parent_sha_123",
            "companion_to": "gerrit:1409697",
            "companion_to_label": "CL 1409697",
        },
    }
    w.compute_companion_states(items, skia_dir="/nonexistent")
    comp = items["gerrit:1410416"]
    self.assertTrue(comp["ready_to_abandon"])
    self.assertEqual(comp["ready_to_abandon_reason"], "You approved CL 1409697 (PS3)")
    self.assertEqual(comp["companion_state"], "ready_to_abandon")
    self.assertTrue(comp["in_my_attention"])

    items["gerrit:1409697"]["self_approved"] = False
    items["gerrit:1409697"]["status"] = "MERGED"
    w.compute_companion_states(items, skia_dir="/nonexistent")
    self.assertTrue(items["gerrit:1410416"]["ready_to_abandon"])
    self.assertEqual(
        items["gerrit:1410416"]["ready_to_abandon_reason"],
        "Parent CL 1409697 is merged",
    )

    items["gerrit:1409697"]["status"] = "NEW"
    fake_thread = {
        "1410416": {"mentioned": True, "all_resolved": True, "open_count": 0}
    }
    with mock.patch.object(
        w, "fetch_gerrit_companion_threads", return_value=fake_thread
    ):
      w.compute_companion_states(items, skia_dir="/nonexistent")
    self.assertTrue(items["gerrit:1410416"]["ready_to_abandon"])
    self.assertEqual(
        items["gerrit:1410416"]["ready_to_abandon_reason"],
        "Companion thread resolved on CL 1409697",
    )

  def test_multiple_companions_deduplication(self):
    items = {
        "gerrit:1409436": {
            "item_key": "gerrit:1409436",
            "target_id": "1409436",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "status": "NEW",
            "patchset": 4,
            "self_approved": False,
            "revision_ps_by_sha": {"sha_ps1": 1, "sha_ps2": 2},
            "companions": [
                {"item_key": "gerrit:1411136", "label": "CL 1411136"},
                {"item_key": "gerrit:1411156", "label": "CL 1411156"},
            ],
        },
        "gerrit:1411136": {
            "item_key": "gerrit:1411136",
            "target_id": "1411136",
            "kind": "gerrit",
            "is_self": True,
            "status": "NEW",
            "patchset": 1,
            "parent_commit": "sha_ps1",
            "companion_to": "gerrit:1409436",
            "companion_to_label": "CL 1409436",
            "activity_ts": 1000.0,
        },
        "gerrit:1411156": {
            "item_key": "gerrit:1411156",
            "target_id": "1411156",
            "kind": "gerrit",
            "is_self": True,
            "status": "NEW",
            "patchset": 1,
            "parent_commit": "sha_ps2",
            "companion_to": "gerrit:1409436",
            "companion_to_label": "CL 1409436",
            "activity_ts": 2000.0,
        },
    }
    fake_threads = {
        "1411136": {"mentioned": False, "all_resolved": False},
        "1411156": {"mentioned": True, "all_resolved": False},
    }
    with mock.patch.object(
        w, "fetch_gerrit_companion_threads", return_value=fake_threads
    ):
      w.compute_companion_states(items, skia_dir="/nonexistent")

    can_comp = items["gerrit:1411156"]
    dup_comp = items["gerrit:1411136"]
    self.assertFalse(can_comp["companion_duplicate"])
    self.assertFalse(can_comp["ready_to_abandon"])
    self.assertEqual(can_comp["companion_state"], "active")

    self.assertTrue(dup_comp["companion_duplicate"])
    self.assertTrue(dup_comp["ready_to_abandon"])
    self.assertEqual(dup_comp["companion_state"], "duplicate")
    self.assertIn("superseded by CL 1411156", dup_comp["ready_to_abandon_reason"])

    parent_comps = items["gerrit:1409436"]["companions"]
    c136 = next(c for c in parent_comps if c["item_key"] == "gerrit:1411136")
    c156 = next(c for c in parent_comps if c["item_key"] == "gerrit:1411156")
    self.assertTrue(c136["companion_duplicate"])
    self.assertFalse(c156["companion_duplicate"])

  def test_abandon_companion_items_and_thread_resolution(self):
    state = {
        "items": {
            "gerrit:1409697": {
                "item_key": "gerrit:1409697",
                "target_id": "1409697",
                "host": w.GERRIT_SKIA_HOST,
            },
            "gerrit:1410416": {
                "item_key": "gerrit:1410416",
                "target_id": "1410416",
                "kind": "gerrit",
                "host": w.GERRIT_SKIA_HOST,
                "is_self": True,
                "status": "NEW",
                "subject": "Fix edge case companion",
                "companion_to": "gerrit:1409697",
            },
            "github:195386": {
                "item_key": "github:195386",
                "target_id": "195386",
                "kind": "github",
                "is_self": True,
                "status": "OPEN",
                "subject": "WIP companion PR",
                "companion_to": "github:195384",
            },
        },
        "events": [],
    }

    gerrit_comments_payload = {
        "/PATCHSET_LEVEL": [
            {
                "id": "c_hold",
                "unresolved": True,
                "message": "I leave this comment unresolved on purpose: Canary-G3",
            },
            {
                "id": "c_comp_parent",
                "unresolved": True,
                "message": (
                    "Uploaded companion CL https://skia-review.googlesource.com/c/skia/+/1410416"
                ),
            },
        ]
    }

    posted_urls: list[str] = []
    posted_bodies: list[bytes] = []

    def fake_urlopen(req, timeout=30):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      posted_urls.append(url)
      if hasattr(req, "data") and req.data:
        posted_bodies.append(req.data)
      resp = mock.MagicMock()
      resp.status = 200
      if "comments" in url and not (hasattr(req, "data") and req.data):
        raw = b")]}'\n" + json.dumps(gerrit_comments_payload).encode("utf-8")
      else:
        raw = b")]}'\n{}"
      resp.read.return_value = raw
      resp.__enter__.return_value = resp
      return resp

    gh_cmds: list[list[str]] = []

    def fake_run(cmd, **kwargs):
      gh_cmds.append(cmd)
      proc = mock.MagicMock()
      proc.returncode = 0
      proc.stdout = ""
      proc.stderr = ""
      return proc

    with mock.patch.object(w, "load_watcher_state", return_value=state), \
         mock.patch.object(w, "save_watcher_state") as mock_save, \
         mock.patch.object(w, "_get_gerrit_bearer_token", return_value="fake_token"), \
         mock.patch.object(w.urllib.request, "urlopen", side_effect=fake_urlopen), \
         mock.patch.object(w.subprocess, "run", side_effect=fake_run):
      res = w.abandon_companion_items(
          ["gerrit:1410416", "github:195386"]
      )

    self.assertTrue(res["ok"])
    self.assertEqual(res["abandoned"], ["gerrit:1410416", "github:195386"])
    self.assertEqual(res["resolved_threads"], 1)

    self.assertTrue(
        any("/a/changes/1410416/abandon" in u for u in posted_urls)
    )

    review_post = next(
        (json.loads(b.decode("utf-8")) for b in posted_bodies if b"drafts" in b or b"comments" in b),
        None,
    )
    self.assertIsNotNone(review_post)
    comments = review_post.get("comments", {}).get("/PATCHSET_LEVEL", [])
    self.assertEqual(len(comments), 1)
    self.assertEqual(comments[0].get("in_reply_to"), "c_comp_parent")
    self.assertFalse(comments[0].get("unresolved"))

    self.assertTrue(
        any(cmd[:3] == ["gh", "pr", "close"] and "195386" in cmd for cmd in gh_cmds)
    )

    self.assertEqual(state["items"]["gerrit:1410416"]["status"], "ABANDONED")
    self.assertEqual(state["items"]["github:195386"]["status"], "CLOSED")
    mock_save.assert_called_once()

  def test_dashboard_js_companion_rendering(self):
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + """
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};
const activeComp = {
  item_key: 'gerrit:1410416', kind: 'gerrit', target_id: '1410416', status: 'NEW',
  phase: 'WAITING_ON_OTHERS', is_self: true, is_external: false, in_my_attention: false,
  is_draft: false, in_development: false, is_watching: false,
  project_name: 'Skia', subject: '[skparagraph] Fix edge cases',
  patchset: 2, url: 'https://skia-review.googlesource.com/c/1410416',
  is_wip: true, wip_label: 'WIP',
  companion_to: 'gerrit:1409697', companion_to_label: 'CL 1409697',
  companion_state: 'active', ready_to_abandon: false,
  companion_status_note: 'Waiting on author',
  merge_conflict: false, missing_reviewers_to_assign: 0,
};
const readyComp = {
  item_key: 'gerrit:1410418', kind: 'gerrit', target_id: '1410418', status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION', is_self: true, is_external: false, in_my_attention: true,
  is_draft: false, in_development: false, is_watching: false,
  project_name: 'Skia', subject: '[skparagraph] Approved companion',
  patchset: 1, url: 'https://skia-review.googlesource.com/c/1410418',
  is_wip: true, wip_label: 'WIP',
  companion_to: 'gerrit:1409697', companion_to_label: 'CL 1409697',
  companion_state: 'ready_to_abandon', ready_to_abandon: true,
  ready_to_abandon_reason: 'You approved CL 1409697 (PS3)',
  companion_status_note: 'You approved CL 1409697 (PS3)',
  merge_conflict: false, missing_reviewers_to_assign: 0,
};
const parentCl = {
  item_key: 'gerrit:1409697', kind: 'gerrit', target_id: '1409697', status: 'NEW',
  phase: 'WAITING_ON_REVIEWERS', is_self: false, is_external: true, in_my_attention: false,
  project_name: 'Skia', subject: '[skparagraph] First line indent',
  patchset: 3, url: 'https://skia-review.googlesource.com/c/1409697',
  companions: [
    {
      item_key: 'gerrit:1410416', label: 'CL 1410416',
      url: 'https://skia-review.googlesource.com/c/1410416', patchset: 2,
      companion_duplicate: true, ready_to_abandon: true,
      companion_status_note: 'Duplicate companion',
    },
    {
      item_key: 'gerrit:1410418', label: 'CL 1410418',
      url: 'https://skia-review.googlesource.com/c/1410418', patchset: 1,
      companion_duplicate: false, ready_to_abandon: true,
      companion_status_note: 'Ready to abandon',
    },
  ],
};
console.log(JSON.stringify({
  activeStatus: statusOf(activeComp),
  activeLane: laneOf(activeComp),
  activeCanRebase: canRebase(activeComp),
  activeRow: rowHtml(activeComp, box),
  activeState: stateHtml(activeComp),
  readyStatus: statusOf(readyComp),
  readyLane: laneOf(readyComp),
  readyCard: focusCard(readyComp, box),
  parentBanner: companionsBannerHtml(parentCl),
}));
"""
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    out = json.loads(proc.stdout.strip())

    self.assertEqual(out["activeStatus"], "waiting")
    self.assertEqual(out["activeLane"], "waiting")
    self.assertFalse(out["activeCanRebase"])
    self.assertIn("Companion → CL 1409697", out["activeRow"])
    self.assertIn("Waiting on author", out["activeState"])
    self.assertIn("🗑️ Abandon", out["activeState"])
    self.assertNotIn("No reviewers assigned", out["activeState"])

    self.assertEqual(out["readyStatus"], "attention")
    self.assertEqual(out["readyLane"], "you")
    self.assertIn("Ready to abandon", out["readyCard"])
    self.assertIn("You approved CL 1409697 (PS3)", out["readyCard"])
    self.assertIn("🗑️ Abandon companion", out["readyCard"])

    self.assertIn("⚠️ Duplicate", out["parentBanner"])
    self.assertIn("✅ Ready to abandon", out["parentBanner"])
    self.assertIn("🗑️ Abandon all companions (2)", out["parentBanner"])


if __name__ == "__main__":
  unittest.main()
