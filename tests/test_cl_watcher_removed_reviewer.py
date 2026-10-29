#!/usr/bin/env python3
"""Contract tests for tracking Gerrit reviewer self-removal in cl_watcher."""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


def _mock_gerrit_response(payload: dict) -> mock.MagicMock:
  raw = (")]}'\n" + json.dumps(payload)).encode("utf-8")
  resp = mock.MagicMock()
  resp.read.return_value = raw
  resp.__enter__.return_value = resp
  resp.__exit__.return_value = False
  return resp


class RemovedReviewerTest(unittest.TestCase):

  def test_detects_removed_reviewer_in_gerrit_detail(self):
    detail = {
        "status": "NEW",
        "subject": "Fix SkPDF glyph widths",
        "owner": {"name": "William Selna", "email": "wselna123@gmail.com"},
        "current_revision": "abc1234567890",
        "revisions": {"abc1234567890": {"_number": 2}},
        "reviewer_updates": [
            {
                "updated": "2026-10-18 22:32:16.000000000",
                "reviewer": {"email": "jlavrova@google.com"},
                "updated_by": {"email": "wselna123@gmail.com"},
                "state": "REVIEWER",
            },
            {
                "updated": "2026-10-26 19:46:45.000000000",
                "reviewer": {"email": "jlavrova@google.com"},
                "updated_by": {"email": "jlavrova@google.com"},
                "state": "REMOVED",
            },
        ],
        "messages": [
            {
                "_revision_number": 2,
                "author": {"email": "jlavrova@google.com"},
                "date": "2026-10-26 19:46:45.000000000",
                "message": "Removed reviewer <GERRIT_ACCOUNT_8956>.",
                "accounts_in_message": [
                    {"_account_id": 8956, "email": "jlavrova@google.com"}
                ],
            }
        ],
    }
    with mock.patch(
        "urllib.request.urlopen", return_value=_mock_gerrit_response(detail)
    ):
      with mock.patch.object(w, "gerrit_mergeable", return_value=True):
        info = w.fetch_gerrit_cl_status(
            "1397897", {}, [], skia_dir="/nonexistent"
        )

    self.assertTrue(info["ok"])
    self.assertTrue(info["reviewer_removed"])
    self.assertEqual(info["removed_at"], "2026-10-26 19:46:45.000000000")
    self.assertIn("jlavrova@google.com", info["last_msg_snippet"])
    self.assertNotIn("<GERRIT_ACCOUNT_8956>", info["last_msg_snippet"])

  def test_cc_reviewer_is_not_marked_removed(self):
    detail = {
        "status": "NEW",
        "subject": "SkGlyphID 32-bit",
        "owner": {"name": "Author", "email": "ext@example.com"},
        "current_revision": "def1234567890",
        "revisions": {"def1234567890": {"_number": 1}},
        "reviewer_updates": [
            {
                "updated": "2026-10-21 17:57:21.000000000",
                "reviewer": {"email": "jlavrova@google.com"},
                "updated_by": {"email": "hcm@google.com"},
                "state": "CC",
            }
        ],
    }
    with mock.patch(
        "urllib.request.urlopen", return_value=_mock_gerrit_response(detail)
    ):
      with mock.patch.object(w, "gerrit_mergeable", return_value=True):
        info = w.fetch_gerrit_cl_status(
            "1382237", {}, [], skia_dir="/nonexistent"
        )

    self.assertFalse(info["reviewer_removed"])

  def test_sync_once_finalizes_single_cl_chat_when_reviewer_removed(self):
    discovered = [
        {
            "item_key": "gerrit:1397897",
            "kind": "gerrit",
            "target_id": "1397897",
            "conversation_id": "87b59df9-ee2f-4736-b009-0f718e0978f7",
            "title": "[09:51] ⦿ cl/1397897",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "2026-10-19T13:51:23Z",
            "single_target": True,
        }
    ]
    cl_info = {
        "ok": True,
        "host": "skia-review.googlesource.com",
        "url": "https://skia-review.googlesource.com/c/1397897",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "subject": "Fix SkPDF glyph widths",
        "owner_name": "William Selna",
        "owner_email": "wselna123@gmail.com",
        "role": "reviewer (ext)",
        "is_self": False,
        "is_external": True,
        "reviewer_removed": True,
        "removed_at": "2026-10-26 19:46:45.000000000",
        "patchset": 2,
        "revision": "abc1234567",
        "in_my_attention": False,
        "waiting_on": [],
        "merge_conflict": False,
        "tree_red": False,
        "cr_summary": "0",
        "cq_status": "Idle",
        "cq_failed": False,
        "last_msg_author": "jlavrova@google.com",
        "last_msg_date": "2026-10-26 19:46:45.000000000",
        "last_msg_et": "15:46 ET",
        "last_msg_age": "5m ago",
        "last_msg_snippet": "Removed reviewer jlavrova@google.com.",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "post_submit": None,
    }
    gstat = {
        "thread_ids": ["th-1"],
        "inbox_thread_ids": ["th-1"],
        "total_msgs": 2,
        "inbox_msgs": 1,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), mock.patch.object(
          w, "fetch_gardener_snapshot", return_value=({}, [])
      ), mock.patch.object(
          w, "discover_active_cl_chats", return_value=discovered
      ), mock.patch.object(
          w, "discover_open_remote_changes", return_value=[]
      ), mock.patch.object(
          w, "fetch_gmail_status_for_id", return_value=gstat
      ), mock.patch.object(
          w, "fetch_gerrit_cl_status", return_value=cl_info
      ), mock.patch.object(
          w, "archive_gmail_threads_for_id"
      ) as mock_arch, mock.patch.object(
          w, "finalize_conversation", return_value=True
      ) as mock_fin:
        state = w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    self.assertEqual(state["items"], {})
    mock_arch.assert_called_once_with("1397897", ["th-1"])
    mock_fin.assert_called_once_with("87b59df9-ee2f-4736-b009-0f718e0978f7")

  def test_sync_once_does_not_finalize_multi_cl_chat(self):
    discovered = [
        {
            "item_key": "gerrit:1397897",
            "kind": "gerrit",
            "target_id": "1397897",
            "conversation_id": "multi-cid-1234",
            "title": "[09:51] ⦿ cl/1397897 and cl/1397896",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "chat_last_modified": "2026-10-19T13:51:23Z",
            "single_target": False,
        }
    ]
    cl_info = {
        "ok": True,
        "host": "skia-review.googlesource.com",
        "url": "https://skia-review.googlesource.com/c/1397897",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "subject": "Fix SkPDF glyph widths",
        "owner_name": "William Selna",
        "owner_email": "wselna123@gmail.com",
        "role": "reviewer (ext)",
        "is_self": False,
        "is_external": True,
        "reviewer_removed": True,
        "removed_at": "2026-10-26 19:46:45.000000000",
        "patchset": 2,
        "revision": "abc1234567",
        "in_my_attention": False,
        "waiting_on": [],
        "merge_conflict": False,
        "tree_red": False,
        "cr_summary": "0",
        "cq_status": "Idle",
        "cq_failed": False,
        "last_msg_author": "jlavrova@google.com",
        "last_msg_date": "2026-10-26 19:46:45.000000000",
        "last_msg_et": "15:46 ET",
        "last_msg_age": "5m ago",
        "last_msg_snippet": "Removed reviewer jlavrova@google.com.",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "post_submit": None,
    }
    gstat = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), mock.patch.object(
          w, "fetch_gardener_snapshot", return_value=({}, [])
      ), mock.patch.object(
          w, "discover_active_cl_chats", return_value=discovered
      ), mock.patch.object(
          w, "discover_open_remote_changes", return_value=[]
      ), mock.patch.object(
          w, "fetch_gmail_status_for_id", return_value=gstat
      ), mock.patch.object(
          w, "fetch_gerrit_cl_status", return_value=cl_info
      ), mock.patch.object(
          w, "finalize_conversation", return_value=True
      ) as mock_fin:
        w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    mock_fin.assert_not_called()

  def test_sync_once_keeps_chat_modified_after_removal(self):
    discovered = [
        {
            "item_key": "gerrit:1397897",
            "kind": "gerrit",
            "target_id": "1397897",
            "conversation_id": "new-chat-after-removal",
            "title": "[15:48] ⦿ cl/1397897",
            "project_id": "proj-fd",
            "project_name": "Flight Deck",
            "chat_last_modified": "2026-10-26T19:48:05Z",
            "single_target": True,
        }
    ]
    cl_info = {
        "ok": True,
        "host": "skia-review.googlesource.com",
        "url": "https://skia-review.googlesource.com/c/1397897",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "subject": "Fix SkPDF glyph widths",
        "owner_name": "William Selna",
        "owner_email": "wselna123@gmail.com",
        "role": "reviewer (ext)",
        "is_self": False,
        "is_external": True,
        "reviewer_removed": True,
        "removed_at": "2026-10-26 19:46:45.000000000",
        "patchset": 2,
        "revision": "abc1234567",
        "in_my_attention": False,
        "waiting_on": [],
        "merge_conflict": False,
        "tree_red": False,
        "cr_summary": "0",
        "cq_status": "Idle",
        "cq_failed": False,
        "last_msg_author": "jlavrova@google.com",
        "last_msg_date": "2026-10-26 19:46:45.000000000",
        "last_msg_et": "15:46 ET",
        "last_msg_age": "5m ago",
        "last_msg_snippet": "Removed reviewer jlavrova@google.com.",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "post_submit": None,
    }
    gstat = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), mock.patch.object(
          w, "fetch_gardener_snapshot", return_value=({}, [])
      ), mock.patch.object(
          w, "discover_active_cl_chats", return_value=discovered
      ), mock.patch.object(
          w, "discover_open_remote_changes", return_value=[]
      ), mock.patch.object(
          w, "fetch_gmail_status_for_id", return_value=gstat
      ), mock.patch.object(
          w, "fetch_gerrit_cl_status", return_value=cl_info
      ), mock.patch.object(
          w, "finalize_conversation", return_value=True
      ) as mock_fin:
        w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    mock_fin.assert_not_called()


if __name__ == "__main__":
  unittest.main()
