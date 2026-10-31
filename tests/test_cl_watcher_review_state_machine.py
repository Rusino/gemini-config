#!/usr/bin/env python3
"""Contract tests for code review button state machine and automatic review triggers."""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class CodeReviewStateMachineFrontendTest(unittest.TestCase):

  def _eval_js(self, js_code: str) -> dict:
    script_body = w.DASHBOARD_HTML.split("<script>", 1)[1].split("</script>", 1)[0]
    harness = script_body.split("\nboot();", 1)[0] + "\n" + js_code
    proc = subprocess.run(
        ["node", "-e", harness], capture_output=True, text=True, check=True
    )
    return json.loads(proc.stdout.strip())

  def test_reviewer_button_labels_across_lifecycle_states(self):
    js = """
const box = {jetski_box_url: 'https://jetski.corp.google.com/box/host'};

// 1. Unchatted reviewer CL
const unchattedReviewer = {
  item_key: 'gerrit:1411900', kind: 'gerrit', target_id: '1411900', status: 'NEW',
  phase: 'NEEDS_YOUR_ACTION', is_self: false, role: 'reviewer (goog)',
  in_my_attention: true, is_draft: false, in_development: false, is_watching: false,
  conversation_id: '', project_name: 'Skia', subject: 'Fix font metrics', patchset: 1,
  url: 'https://skia-review.googlesource.com/c/skia/+/1411900'
};

// 2. Busy reviewer CL while triage is executing
const busyReviewer = Object.assign({}, unchattedReviewer, {
  conversation_id: 'cid-busy-1', chat_busy: true
});

// 3. Completed review on current patchset
const completedReviewer = Object.assign({}, unchattedReviewer, {
  conversation_id: 'cid-done-1', chat_busy: false, patchset: 2, reviewed_ps: 2
});

// 3b. Archived completed review
const archivedReviewer = Object.assign({}, completedReviewer, {
  chat_archived: true
});

// 4. Newer patchset uploaded after completed review
const updatedReviewer = Object.assign({}, completedReviewer, {
  patchset: 3, reviewed_ps: 2, review_target_ps: 2
});

// 5. Reviewer CL already approved or commented by user on current PS
const approvedReviewer = Object.assign({}, completedReviewer, {
  in_my_attention: false, self_approved: true, self_approval_ps: 2
});

// 6. Own CL and Watching CL
const ownCl = Object.assign({}, unchattedReviewer, {
  is_self: true, role: 'author'
});
const watchingCl = Object.assign({}, unchattedReviewer, {
  role: 'watching', is_watching: true, in_my_attention: false
});

console.log(JSON.stringify({
  unchattedCard: chatActionHtml(unchattedReviewer, box, false),
  unchattedRow: chatActionHtml(unchattedReviewer, box, true),
  busyCard: chatActionHtml(busyReviewer, box, false),
  busyRow: chatActionHtml(busyReviewer, box, true),
  completedCard: chatActionHtml(completedReviewer, box, false),
  completedRow: chatActionHtml(completedReviewer, box, true),
  archivedCard: chatActionHtml(archivedReviewer, box, false),
  archivedRow: chatActionHtml(archivedReviewer, box, true),
  updatedCard: chatActionHtml(updatedReviewer, box, false),
  updatedRow: chatActionHtml(updatedReviewer, box, true),
  approvedCard: chatActionHtml(approvedReviewer, box, false),
  approvedRow: chatActionHtml(approvedReviewer, box, true),
  ownUnchattedCard: chatActionHtml(ownCl, box, false),
  watchingUnchattedCard: chatActionHtml(watchingCl, box, false)
}));
"""
    out = self._eval_js(js)

    # 1. Unchatted reviewer change
    self.assertIn("Start code review", out["unchattedCard"])
    self.assertIn("start code review", out["unchattedRow"])
    self.assertIn("startChat(", out["unchattedCard"])

    # 2. Busy reviewer change
    self.assertIn("⏳ Preparing code review…", out["busyCard"])
    self.assertIn("⏳ preparing review…", out["busyRow"])
    self.assertIn("openChat(", out["busyCard"])

    # 3. Completed review on current PS
    self.assertIn("Look at code review", out["completedCard"])
    self.assertIn("look at code review", out["completedRow"])
    self.assertIn("openChat(", out["completedCard"])

    # 3b. Archived completed review
    self.assertIn("Look at code review (archived)", out["archivedCard"])
    self.assertIn("look at code review (archived)", out["archivedRow"])

    # 4. Newer patchset uploaded
    self.assertIn("Start code review (PS3)", out["updatedCard"])
    self.assertIn("start review (PS3)", out["updatedRow"])
    self.assertIn("startChat(", out["updatedCard"])

    # 5. Reviewer CL already approved/commented by user
    self.assertIn("Open review chat", out["approvedCard"])
    self.assertIn("review chat", out["approvedRow"])

    # 6. Own CL and Watching CL retain standard Start chat
    self.assertIn("Start chat", out["ownUnchattedCard"])
    self.assertIn("Start chat", out["watchingUnchattedCard"])


class CodeReviewStateMachineBackendTest(unittest.TestCase):

  def test_is_assigned_reviewer_record_contract(self):
    self.assertTrue(
        w._is_assigned_reviewer_record({
            "is_self": False,
            "role": "reviewer (goog)",
            "is_watching": False,
        })
    )
    self.assertTrue(
        w._is_assigned_reviewer_record({
            "is_self": False,
            "role": "reviewer",
            "is_watching": False,
        })
    )
    self.assertFalse(
        w._is_assigned_reviewer_record({
            "is_self": True,
            "role": "author",
        })
    )
    self.assertFalse(
        w._is_assigned_reviewer_record({
            "is_self": False,
            "role": "watching",
            "is_watching": True,
        })
    )
    self.assertFalse(
        w._is_assigned_reviewer_record({
            "is_self": False,
            "role": "reviewer (goog)",
            "reviewer_removed": True,
        })
    )
    self.assertFalse(
        w._is_assigned_reviewer_record({
            "is_self": False,
            "companion_to": "gerrit:12345",
        })
    )

  def test_start_chat_wakes_existing_conversation_on_new_patchset(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state_file = os.path.join(tmp_dir, "cl_watcher_state.json")
      rec = {
          "item_key": "gerrit:1411901",
          "target_id": "1411901",
          "kind": "gerrit",
          "subject": "Add layout cache",
          "patchset": 3,
          "reviewed_ps": 2,
          "review_target_ps": 2,
          "conversation_id": "cid-rev-1411901",
          "is_self": False,
          "role": "reviewer (goog)",
          "chat_busy": False,
          "url": "https://skia-review.googlesource.com/c/skia/+/1411901",
      }
      initial_state = {
          "items": {"gerrit:1411901@cid-rev-1411901": rec},
          "events": [],
      }
      with open(state_file, "w", encoding="utf-8") as f:
        json.dump(initial_state, f)

      with mock.patch.object(w, "STATE_FILE", state_file), mock.patch.object(
          w, "wake_conversation", return_value=True
      ) as mock_wake:
        res = w.start_chat_for_item("gerrit:1411901")

      self.assertTrue(res.get("ok"))
      self.assertTrue(res.get("woken"))
      mock_wake.assert_called_once()
      self.assertIn("PS3", mock_wake.call_args[0][1])

      with open(state_file, "r", encoding="utf-8") as f:
        saved = json.load(f)
      saved_rec = saved["items"]["gerrit:1411901@cid-rev-1411901"]
      self.assertEqual(saved_rec["review_target_ps"], 3)
      self.assertTrue(saved_rec["chat_busy"])

  def test_overlay_live_chat_busy_promotes_reviewed_ps_on_completion(self):
    state = {
        "items": {
            "gerrit:1411902@cid-busy": {
                "conversation_id": "cid-busy",
                "is_self": False,
                "role": "reviewer (goog)",
                "chat_busy": True,
                "patchset": 2,
                "reviewed_ps": 1,
                "review_target_ps": 2,
            }
        }
    }
    fake_summaries = {
        "cid-busy": {
            "status": "CASCADE_RUN_STATUS_IDLE",
            "annotations": {"title": "[12:00] ⦿ cl/1411902 layout"},
        }
    }
    with mock.patch.object(
        w.cl, "_fetch_ls_summaries_for_audit", return_value=fake_summaries
    ):
      w.overlay_live_chat_busy(state)

    item = state["items"]["gerrit:1411902@cid-busy"]
    self.assertFalse(item["chat_busy"])
    self.assertEqual(item["reviewed_ps"], 2)

  def test_sync_once_throttled_auto_start_for_reviewer_changes(self):
    remote = [
        {
            "item_key": "gerrit:1001",
            "kind": "gerrit",
            "target_id": "1001",
            "preferred_host": "skia-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
        {
            "item_key": "gerrit:1002",
            "kind": "gerrit",
            "target_id": "1002",
            "preferred_host": "skia-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
        {
            "item_key": "gerrit:1003",
            "kind": "gerrit",
            "target_id": "1003",
            "preferred_host": "skia-review.googlesource.com",
            "conversation_id": "",
            "title": "",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "",
            "single_target": True,
            "chat_busy": False,
        },
    ]

    def fake_status(cl_id, *args, **kwargs):
      if cl_id == "1001":
        return {
            "ok": True,
            "host": "skia-review.googlesource.com",
            "url": "https://skia-review.googlesource.com/c/skia/+/1001",
            "status": "NEW",
            "phase": "NEEDS_YOUR_ACTION",
            "subject": "Skia font test",
            "owner_name": "Alice",
            "owner_email": "alice@example.com",
            "role": "reviewer (goog)",
            "is_self": False,
            "is_external": False,
            "is_draft": False,
            "in_development": False,
            "is_watching": False,
            "reviewer_removed": False,
            "patchset": 2,
            "in_my_attention": True,
        }
      if cl_id == "1002":
        return {
            "ok": True,
            "host": "skia-review.googlesource.com",
            "url": "https://skia-review.googlesource.com/c/skia/+/1002",
            "status": "NEW",
            "phase": "NEEDS_YOUR_ACTION",
            "subject": "Skia color test",
            "owner_name": "Bob",
            "owner_email": "bob@example.com",
            "role": "reviewer (goog)",
            "is_self": False,
            "is_external": False,
            "is_draft": False,
            "in_development": False,
            "is_watching": False,
            "reviewer_removed": False,
            "patchset": 1,
            "in_my_attention": True,
        }
      return {
          "ok": True,
          "host": "skia-review.googlesource.com",
          "url": "https://skia-review.googlesource.com/c/skia/+/1003",
          "status": "NEW",
          "phase": "WAITING_ON_REVIEWERS",
          "subject": "Skia watching change",
          "owner_name": "Charlie",
          "owner_email": "charlie@example.com",
          "role": "watching",
          "is_self": False,
          "is_external": False,
          "is_draft": False,
          "in_development": False,
          "is_watching": True,
          "reviewer_removed": False,
          "patchset": 1,
          "in_my_attention": False,
      }

    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }

    def fake_start_pinned(items, ikey, matched_key, rec, prompt):
      new_cid = f"cid-auto-{ikey}"
      w._rekey_state_item_chat(
          items, ikey, matched_key, rec, new_cid, chat_archived=False
      )
      rec["chat_busy"] = True
      rec["review_target_ps"] = int(rec.get("patchset") or 1)
      return {
          "ok": True,
          "started": True,
          "conversation_id": new_cid,
      }

    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), \
           mock.patch.object(w, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(w, "discover_active_cl_chats", return_value=[]), \
           mock.patch.object(w, "discover_open_remote_changes", return_value=remote), \
           mock.patch.object(w, "fetch_gerrit_cl_status", side_effect=fake_status), \
           mock.patch.object(w, "fetch_gmail_status_for_id", return_value=empty_gmail), \
           mock.patch.object(w, "wake_conversation"), \
           mock.patch.object(
               w, "_start_pinned_chat_for_record", side_effect=fake_start_pinned
           ) as mock_start:
        state = w.sync_once(wake_chats=True, skia_dir="/nonexistent")

    self.assertEqual(mock_start.call_count, 1)
    self.assertIn("gerrit:1001:2", state["review_claims"])
    self.assertNotIn("gerrit:1002:1", state["review_claims"])
    self.assertNotIn("gerrit:1003:1", state["review_claims"])
    auto_events = [
        ev for ev in state["events"]
        if "Auto-started code review for gerrit:1001" in ev["message"]
    ]
    self.assertEqual(len(auto_events), 1)


if __name__ == "__main__":
  unittest.main()
