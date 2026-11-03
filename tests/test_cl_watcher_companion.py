# Copyright 2026 Google LLC.
# SPDX-License-Identifier: Apache-2.0

import copy
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

    items["gerrit:1410416"]["ready_to_abandon"] = False
    items["gerrit:1410416"]["ready_to_abandon_reason"] = ""
    items["gerrit:1410416"]["cq_status"] = "CQ+1 running"
    fake_thread_none = {}
    with mock.patch.object(
        w, "fetch_gerrit_companion_threads", return_value=fake_thread_none
    ):
      w.compute_companion_states(items, skia_dir="/nonexistent")
    self.assertEqual(
        items["gerrit:1410416"]["companion_status_note"],
        "Stacked on old PS2 (parent at PS3) · CQ+1 running",
    )

    items["gerrit:1409697"]["patchset"] = 2
    with mock.patch.object(
        w, "fetch_gerrit_companion_threads", return_value=fake_thread_none
    ):
      w.compute_companion_states(items, skia_dir="/nonexistent")
    self.assertEqual(
        items["gerrit:1410416"]["companion_status_note"],
        "Stacked on PS2 · CQ+1 running · waiting on author",
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
      ready_to_abandon_reason: 'Duplicate companion',
      companion_status_note: 'Duplicate companion',
    },
    {
      item_key: 'gerrit:1410418', label: 'CL 1410418',
      url: 'https://skia-review.googlesource.com/c/1410418', patchset: 1,
      companion_duplicate: false, ready_to_abandon: true,
      ready_to_abandon_reason: 'Ready to abandon',
      companion_status_note: 'Ready to abandon',
    },
  ],
};
const orphanComp = {
  ...activeComp,
  item_key: 'gerrit:999999',
  target_id: '999999',
  companion_to: 'gerrit:missing',
};
const visibleAll = visibleItemsFromData({
  items: {
    'gerrit:1409697': parentCl,
    'gerrit:1410416': activeComp,
    'gerrit:1410418': readyComp,
    'gerrit:999999': orphanComp,
  },
});
console.log(JSON.stringify({
  activeStatus: statusOf(activeComp),
  activeLane: laneOf(activeComp),
  activeCanRebase: canRebase(activeComp),
  activeRow: rowHtml(activeComp, box),
  activeState: stateHtml(activeComp),
  readyStatus: statusOf(readyComp),
  readyLane: laneOf(readyComp),
  readyCard: focusCard(readyComp, box),
  parentStatus: statusOf(parentCl),
  parentLane: laneOf(parentCl),
  parentCard: focusCard(parentCl, box),
  parentBanner: companionsBannerHtml(parentCl),
  visibleKeys: visibleAll.map(x => x.item_key),
}));
"""
    # Feed DASHBOARD_HTML harness via stdin to stay below Linux 128 KB MAX_ARG_STRLEN
    proc = subprocess.run(
        ["node", "-"], input=harness, capture_output=True, text=True, check=True
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

    self.assertEqual(out["parentStatus"], "attention")
    self.assertEqual(out["parentLane"], "you")
    self.assertIn("Companion CL 1410416, CL 1410418 ready to abandon", out["parentCard"])
    self.assertIn("🗑️ Abandon companions (2)", out["parentCard"])
    self.assertEqual(out["visibleKeys"], ["gerrit:1409697", "gerrit:999999"])

    self.assertIn("⚠️ Duplicate", out["parentBanner"])
    self.assertIn("✅ Ready to abandon", out["parentBanner"])
    self.assertIn("🗑️ Abandon all companions (2)", out["parentBanner"])

  def test_stack_companion_matches_direct_parent_commit_and_chained_companions(self):
    self_owner = {"email": "jlavrova@google.com", "name": "Julia Lavrova"}
    ext_owner = {"email": "fmalita@chromium.org", "name": "Florin Malita"}
    items = {
        "gerrit:8663938": {
            "item_key": "gerrit:8663938",
            "target_id": "8663938",
            "kind": "gerrit",
            "host": "chromium-review.googlesource.com",
            "is_self": False,
            "is_external": True,
            "status": "NEW",
            "patchset": 19,
            "self_approved": True,
            "self_approval_ps": 19,
            "owner": ext_owner,
            "revision_ps_by_sha": {"sha_root_ps18": 18, "sha_root_ps19": 19},
            "parent_commit": "sha_main",
        },
        "gerrit:8663827": {
            "item_key": "gerrit:8663827",
            "target_id": "8663827",
            "kind": "gerrit",
            "host": "chromium-review.googlesource.com",
            "is_self": False,
            "is_external": True,
            "status": "NEW",
            "patchset": 13,
            "self_approved": False,
            "self_approval_ps": 0,
            "owner": ext_owner,
            "revision_ps_by_sha": {"sha_mid_ps13": 13},
            "parent_commit": "sha_root_ps18",
        },
        "gerrit:8687026": {
            "item_key": "gerrit:8687026",
            "target_id": "8687026",
            "kind": "gerrit",
            "host": "chromium-review.googlesource.com",
            "is_self": True,
            "is_external": False,
            "is_wip": True,
            "status": "NEW",
            "patchset": 1,
            "subject": "[canvas] WIP companion: Draw GlyphRuns from ShapedText",
            "owner": self_owner,
            "revision_ps_by_sha": {"sha_comp_a": 1},
            "parent_commit": "sha_mid_ps13",
        },
        "gerrit:8687027": {
            "item_key": "gerrit:8687027",
            "target_id": "8687027",
            "kind": "gerrit",
            "host": "chromium-review.googlesource.com",
            "is_self": True,
            "is_external": False,
            "is_wip": True,
            "status": "NEW",
            "patchset": 1,
            "subject": "[canvas] WIP companion: Draw all GlyphRuns in one Draw",
            "owner": self_owner,
            "revision_ps_by_sha": {"sha_comp_b": 1},
            "parent_commit": "sha_comp_a",
        },
    }
    discovered = [
        items["gerrit:8663938"],
        items["gerrit:8687026"],
        items["gerrit:8687027"],
    ]
    raw_related = [
        {
            "_change_number": 8687027,
            "_revision_number": 1,
            "_current_revision_number": 1,
            "status": "NEW",
            "commit": {
                "commit": "sha_comp_b",
                "parents": [{"commit": "sha_comp_a"}],
                "author": {"email": "jlavrova@google.com"},
            },
        },
        {
            "_change_number": 8687026,
            "_revision_number": 1,
            "_current_revision_number": 1,
            "status": "NEW",
            "commit": {
                "commit": "sha_comp_a",
                "parents": [{"commit": "sha_mid_ps13"}],
                "author": {"email": "jlavrova@google.com"},
            },
        },
        {
            "_change_number": 8663827,
            "_revision_number": 13,
            "_current_revision_number": 13,
            "status": "NEW",
            "commit": {
                "commit": "sha_mid_ps13",
                "parents": [{"commit": "sha_root_ps18"}],
                "author": {"email": "fmalita@chromium.org"},
            },
        },
        {
            "_change_number": 8663938,
            "_revision_number": 18,
            "_current_revision_number": 19,
            "status": "NEW",
            "commit": {
                "commit": "sha_root_ps18",
                "parents": [{"commit": "sha_main"}],
                "author": {"email": "fmalita@chromium.org"},
            },
        },
    ]
    host = "chromium-review.googlesource.com"
    cache_patch = {
        f"{host}:8687026": raw_related,
        f"{host}:8687027": raw_related,
    }
    with mock.patch.object(
        w,
        "fetch_gerrit_related_cl_ids",
        return_value=["8687027", "8687026", "8663827", "8663938"],
    ), mock.patch.dict(
        getattr(w, "_RELATED_RAW_CHANGES_CACHE", {}), cache_patch, clear=False
    ):
      w.link_companion_changes_to_parent_chats(discovered, items)

    self.assertEqual(items["gerrit:8687026"].get("companion_to"), "gerrit:8663827")
    self.assertEqual(items["gerrit:8687027"].get("companion_to"), "gerrit:8663827")

    active_items = {
        "gerrit:8663938": items["gerrit:8663938"],
        "gerrit:8687026": items["gerrit:8687026"],
        "gerrit:8687027": items["gerrit:8687027"],
    }
    w.compute_companion_states(
        active_items, skia_dir="/nonexistent", parent_fallback_cache=items
    )
    for k in ("gerrit:8687026", "gerrit:8687027"):
      self.assertEqual(active_items[k]["companion_base_ps"], 13)
      self.assertFalse(active_items[k]["companion_duplicate"])
      self.assertFalse(active_items[k]["ready_to_abandon"])
      self.assertEqual(active_items[k]["companion_state"], "active")
      self.assertEqual(
          active_items[k]["companion_status_note"],
          "Stacked on PS13 · waiting on author",
      )

    silent_info = {
        "is_self": True,
        "is_wip": True,
        "parent_commit": "sha_comp_a",
    }
    with mock.patch.dict(
        getattr(w, "_RELATED_RAW_CHANGES_CACHE", {}), cache_patch, clear=False
    ):
      p_key, p_ps = w._resolve_gerrit_stack_companion_parent(
          host,
          "8687027",
          "[canvas] Draw all GlyphRuns in one Draw",
          silent_info,
          items,
          ["8687027", "8687026", "8663827", "8663938"],
      )
    self.assertEqual((p_key, p_ps), ("gerrit:8663827", 13))

    fallback_key, _ = w._resolve_gerrit_stack_companion_parent(
        host,
        "8687027",
        "[canvas] WIP companion part 2",
        {"is_self": True, "is_wip": True},
        items,
        ["8687027", "8687026", "8663827"],
    )
    self.assertEqual(fallback_key, "gerrit:8663827")

  def test_same_patchset_approval_keeps_companion_active_and_stale_base_note(self):
    items = {
        "gerrit:1412380": {
            "item_key": "gerrit:1412380",
            "target_id": "1412380",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": False,
            "status": "NEW",
            "patchset": 2,
            "self_approved": True,
            "self_approval_ps": 2,
            "revision_ps_by_sha": {"sha_ps1": 1, "sha_ps2": 2},
        },
        "gerrit:1412999": {
            "item_key": "gerrit:1412999",
            "target_id": "1412999",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": True,
            "is_wip": True,
            "status": "NEW",
            "patchset": 1,
            "parent_commit": "sha_ps2",
            "companion_to": "gerrit:1412380",
            "companion_to_label": "CL 1412380",
        },
    }
    resolved_same_ps = {
        "1412999": {"mentioned": True, "all_resolved": True, "open_count": 0}
    }
    with mock.patch.object(
        w, "fetch_gerrit_companion_threads", return_value=resolved_same_ps
    ):
      w.compute_companion_states(items, skia_dir="/nonexistent")

    comp = items["gerrit:1412999"]
    self.assertFalse(comp["ready_to_abandon"])
    self.assertFalse(comp.get("auto_abandon_eligible", False))
    self.assertEqual(comp["companion_state"], "active")
    self.assertEqual(comp["companion_status_note"], "Stacked on PS2 · waiting on author")

    items["gerrit:1412380"]["patchset"] = 3
    items["gerrit:1412380"]["self_approved"] = False
    items["gerrit:1412380"]["self_approval_ps"] = 0
    items["gerrit:1412380"]["revision_ps_by_sha"]["sha_ps3"] = 3
    with mock.patch.object(w, "fetch_gerrit_companion_threads", return_value={}):
      w.compute_companion_states(items, skia_dir="/nonexistent")

    self.assertFalse(comp["ready_to_abandon"])
    self.assertEqual(
        comp["companion_status_note"], "Stacked on old PS2 (parent at PS3)"
    )

    items["gerrit:1412380"]["self_approved"] = True
    items["gerrit:1412380"]["self_approval_ps"] = 3
    with mock.patch.object(w, "fetch_gerrit_companion_threads", return_value={}):
      w.compute_companion_states(items, skia_dir="/nonexistent")

    self.assertTrue(comp["ready_to_abandon"])
    self.assertTrue(comp.get("auto_abandon_eligible", False))
    self.assertEqual(comp["ready_to_abandon_reason"], "You approved CL 1412380 (PS3)")

  def test_preserve_companion_to_when_parent_merged_or_abandoned(self):
    items = {
        "gerrit:1410416": {
            "item_key": "gerrit:1410416",
            "target_id": "1410416",
            "kind": "gerrit",
            "host": w.GERRIT_SKIA_HOST,
            "is_self": True,
            "is_wip": True,
            "status": "NEW",
            "patchset": 1,
            "subject": "[skparagraph] Add edge-case tests",
            "parent_commit": "sha_ps2",
        },
    }
    raw_related = [
        {
            "_change_number": 1410416,
            "_revision_number": 1,
            "_current_revision_number": 1,
            "status": "NEW",
            "commit": {
                "commit": "sha_comp",
                "parents": [{"commit": "sha_ps2"}],
                "author": {"email": "jlavrova@google.com"},
            },
        },
        {
            "_change_number": 1409697,
            "_revision_number": 2,
            "_current_revision_number": 5,
            "status": "MERGED",
            "commit": {
                "commit": "sha_ps2",
                "parents": [{"commit": "sha_main"}],
                "author": {"email": "external@author.com"},
            },
        },
    ]
    fake_resp = mock.MagicMock()
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.read.return_value = (
        ")]}'\n" + json.dumps({"changes": raw_related})
    ).encode("utf-8")
    with mock.patch.dict(w._RELATED_CLS_CACHE, {}, clear=True), mock.patch.dict(
        w._RELATED_RAW_CHANGES_CACHE, {}, clear=True
    ), mock.patch("urllib.request.urlopen", return_value=fake_resp):
      rel_ids = w.fetch_gerrit_related_cl_ids(w.GERRIT_SKIA_HOST, "1410416")
      self.assertEqual(rel_ids, [])
      w.link_companion_changes_to_parent_chats(list(items.values()), items)

    self.assertEqual(items["gerrit:1410416"].get("companion_to"), "gerrit:1409697")
    self.assertEqual(items["gerrit:1410416"].get("companion_base_ps_hint"), 2)

  def test_sync_once_auto_abandons_eligible_companions(self):
    prev_state = {
        "items": {
            "gerrit:1410001@unchatted": {
                "item_key": "gerrit:1410001",
                "target_id": "1410001",
                "kind": "gerrit",
                "host": w.GERRIT_SKIA_HOST,
                "is_self": True,
                "is_wip": True,
                "status": "NEW",
                "parent_commit": "sha_p1_ps1",
                "companion_to": "gerrit:1400001",
            },
            "gerrit:1410006@unchatted": {
                "item_key": "gerrit:1410006",
                "target_id": "1410006",
                "kind": "gerrit",
                "host": w.GERRIT_SKIA_HOST,
                "is_self": True,
                "is_wip": False,
                "status": "NEW",
                "parent_commit": "sha_p1_ps1",
                "companion_to": "gerrit:1400001",
            },
            "gerrit:1410007@unchatted": {
                "item_key": "gerrit:1410007",
                "target_id": "1410007",
                "kind": "gerrit",
                "host": w.GERRIT_SKIA_HOST,
                "is_self": True,
                "is_wip": False,
                "status": "NEW",
                "parent_commit": "sha_p1_ps2",
                "companion_to": "gerrit:1400001",
            },
            "gerrit:1410008@unchatted": {
                "item_key": "gerrit:1410008",
                "target_id": "1410008",
                "kind": "gerrit",
                "host": w.GERRIT_SKIA_HOST,
                "is_self": True,
                "is_wip": False,
                "status": "NEW",
                "parent_commit": "rev_1410007",
                "companion_to": "gerrit:1400001",
            },
        },
        "events": [],
    }
    remote_changes = [
        {"item_key": "gerrit:1410001", "target_id": "1410001", "kind": "gerrit"},
        {"item_key": "gerrit:1410006", "target_id": "1410006", "kind": "gerrit"},
        {"item_key": "gerrit:1410007", "target_id": "1410007", "kind": "gerrit"},
        {"item_key": "gerrit:1410008", "target_id": "1410008", "kind": "gerrit"},
        {"item_key": "gerrit:1400002", "target_id": "1400002", "kind": "gerrit"},
        {"item_key": "gerrit:1410002", "target_id": "1410002", "kind": "gerrit"},
        {"item_key": "gerrit:1400003", "target_id": "1400003", "kind": "gerrit"},
        {"item_key": "gerrit:1410003", "target_id": "1410003", "kind": "gerrit"},
        {"item_key": "gerrit:1400004", "target_id": "1400004", "kind": "gerrit"},
        {"item_key": "gerrit:1410004", "target_id": "1410004", "kind": "gerrit"},
        {"item_key": "gerrit:1410005", "target_id": "1410005", "kind": "gerrit"},
    ]

    def base_info(tid: str, **overrides):
      d = {
          "ok": True,
          "host": w.GERRIT_SKIA_HOST,
          "url": f"https://{w.GERRIT_SKIA_HOST}/c/skia/+/{tid}",
          "status": "NEW",
          "phase": "WAITING_ON_OTHERS",
          "subject": f"[skparagraph] CL {tid}",
          "owner_name": "Author",
          "owner_email": "ext@author.com",
          "role": "reviewer",
          "is_self": False,
          "is_external": True,
          "is_wip": False,
          "is_draft": False,
          "in_development": False,
          "is_watching": False,
          "reviewer_removed": False,
          "self_approved": False,
          "self_approval_ps": 0,
          "patchset": 1,
          "revision": f"rev_{tid}",
          "parent_commit": "sha_main",
          "revision_ps_by_sha": {f"rev_{tid}": 1},
          "commit_message": "",
          "files": [],
          "hashtags": [],
          "topic": "",
          "in_my_attention": False,
          "last_msg_author": "ext@author.com",
          "last_msg_date": "2026-11-01 12:00:00.000000000",
          "last_msg_snippet": "Uploaded patch set",
          "status_changed_raw": "2026-11-01 12:00:00.000000000",
          "submitted": "",
          "post_submit": {},
      }
      d.update(overrides)
      return d

    cl_infos = {
        "1400001": base_info(
            "1400001",
            status="MERGED",
            patchset=2,
            revision_ps_by_sha={"sha_p1_ps1": 1, "sha_p1_ps2": 2},
        ),
        "1410001": base_info(
            "1410001",
            is_self=True,
            is_external=False,
            is_wip=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Companion for 1400001",
            parent_commit="sha_p1_ps1",
        ),
        "1410006": base_info(
            "1410006",
            is_self=True,
            is_external=False,
            is_wip=False,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Standalone CL rebased onto main",
            parent_commit="sha_main_new",
        ),
        "1410007": base_info(
            "1410007",
            is_self=True,
            is_external=False,
            in_development=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[SkoreText] Own CL uploaded onto the merged 1400001",
            parent_commit="sha_p1_ps2",
        ),
        "1410008": base_info(
            "1410008",
            is_self=True,
            is_external=False,
            in_development=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Second CL in stack on merged 1400001",
            parent_commit="rev_1410007",
        ),
        "1400002": base_info(
            "1400002",
            patchset=3,
            self_approved=True,
            self_approval_ps=3,
            revision_ps_by_sha={"sha_p2_ps2": 2, "sha_p2_ps3": 3},
        ),
        "1410002": base_info(
            "1410002",
            is_self=True,
            is_external=False,
            is_wip=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Companion for cl/1400002",
            parent_commit="sha_p2_ps2",
        ),
        "1400003": base_info(
            "1400003",
            patchset=2,
            self_approved=True,
            self_approval_ps=2,
            revision_ps_by_sha={"sha_p3_ps2": 2},
        ),
        "1410003": base_info(
            "1410003",
            is_self=True,
            is_external=False,
            is_wip=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Companion for cl/1400003",
            parent_commit="sha_p3_ps2",
        ),
        "1400004": base_info(
            "1400004",
            patchset=2,
            self_approved=False,
            revision_ps_by_sha={"sha_p4_ps1": 1, "sha_p4_ps2": 2},
        ),
        "1410004": base_info(
            "1410004",
            is_self=True,
            is_external=False,
            is_wip=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Companion for cl/1400004",
            parent_commit="sha_p4_ps1",
        ),
        "1410005": base_info(
            "1410005",
            is_self=True,
            is_external=False,
            is_wip=True,
            role="author",
            owner_email="jlavrova@google.com",
            subject="[skparagraph] Companion for cl/1400004",
            parent_commit="sha_p4_ps2",
        ),
    }

    abandoned_urls: list[str] = []

    def fake_urlopen(req, timeout=30):
      url = req.full_url if hasattr(req, "full_url") else str(req)
      if "/abandon" in url:
        abandoned_urls.append(url)
      resp = mock.MagicMock()
      resp.status = 200
      resp.read.return_value = b")]}'\n{}"
      resp.__enter__.return_value = resp
      return resp

    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    saved_states: list[dict] = []

    def run_sync(wake: bool) -> dict:
      with mock.patch.object(
          w, "load_watcher_state", return_value=copy.deepcopy(prev_state)
      ), mock.patch.object(
          w, "save_watcher_state", side_effect=lambda s: saved_states.append(s)
      ), mock.patch.object(
          w, "fetch_gardener_snapshot", return_value=({}, [])
      ), mock.patch.object(
          w, "load_project_names", return_value={}
      ), mock.patch.object(
          w, "discover_active_cl_chats", return_value=[]
      ), mock.patch.object(
          w,
          "discover_open_remote_changes",
          side_effect=lambda *a, **kw: [dict(x) for x in remote_changes],
      ), mock.patch.object(
          w,
          "fetch_gerrit_cl_status",
          side_effect=lambda tid, *a, **kw: dict(cl_infos[str(tid)]),
      ), mock.patch.object(
          w, "fetch_gerrit_related_cl_ids", return_value=[]
      ), mock.patch.object(
          w, "fetch_gerrit_stack_order_ids", side_effect=lambda h, t: [t]
      ), mock.patch.object(
          w, "fetch_gerrit_companion_threads", return_value={}
      ), mock.patch.object(
          w, "fetch_gmail_status_for_id", return_value=empty_gmail
      ), mock.patch.object(
          w, "_get_gerrit_bearer_token", return_value="tok"
      ), mock.patch.object(
          w.urllib.request, "urlopen", side_effect=fake_urlopen
      ), mock.patch.object(
          w, "overlay_live_chat_busy"
      ):
        return w.sync_once(
            wake_chats=wake, update_titles=False, skia_dir="/nonexistent"
        )

    dry_state = run_sync(False)
    self.assertEqual(abandoned_urls, [])
    self.assertEqual(dry_state["items"]["gerrit:1410001@unchatted"]["status"], "NEW")
    self.assertNotIn("companion_to", dry_state["items"]["gerrit:1410006@unchatted"])

    out_state = run_sync(True)

    self.assertTrue(any("/1410001/abandon" in u for u in abandoned_urls))
    self.assertTrue(any("/1410002/abandon" in u for u in abandoned_urls))
    self.assertTrue(any("/1410004/abandon" in u for u in abandoned_urls))
    self.assertFalse(any("/1410003/abandon" in u for u in abandoned_urls))
    self.assertFalse(any("/1410005/abandon" in u for u in abandoned_urls))
    self.assertFalse(any("/1410006/abandon" in u for u in abandoned_urls))
    self.assertFalse(any("/1410007/abandon" in u for u in abandoned_urls))
    self.assertFalse(any("/1410008/abandon" in u for u in abandoned_urls))

    items_out = out_state["items"]
    self.assertEqual(items_out["gerrit:1410001@unchatted"]["status"], "ABANDONED")
    self.assertEqual(items_out["gerrit:1410002@unchatted"]["status"], "ABANDONED")
    self.assertEqual(items_out["gerrit:1410004@unchatted"]["status"], "ABANDONED")
    self.assertEqual(items_out["gerrit:1410003@unchatted"]["status"], "NEW")
    self.assertEqual(items_out["gerrit:1410005@unchatted"]["status"], "NEW")
    self.assertEqual(items_out["gerrit:1410006@unchatted"]["status"], "NEW")
    self.assertNotIn("companion_to", items_out["gerrit:1410006@unchatted"])
    self.assertEqual(items_out["gerrit:1410007@unchatted"]["status"], "NEW")
    self.assertNotIn("companion_to", items_out["gerrit:1410007@unchatted"])
    self.assertEqual(items_out["gerrit:1410008@unchatted"]["status"], "NEW")
    self.assertNotIn("companion_to", items_out["gerrit:1410008@unchatted"])


if __name__ == "__main__":
  unittest.main()
