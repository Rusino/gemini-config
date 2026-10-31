#!/usr/bin/env python3
"""Contract tests for Gmail Flight Deck label cleanup, thread matching, and cutoff watermark."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top


class GmailFlightDeckCleanupTest(unittest.TestCase):

  def setUp(self):
    super().setUp()
    w._GMAIL_LABEL_ID_CACHE.clear()

  def test_ensure_flight_deck_label_cached(self):
    w._GMAIL_LABEL_ID_CACHE[w.FLIGHT_DECK_GMAIL_LABEL_NAME] = "Label_cached"
    with mock.patch.object(w, "call_gmail_mcp") as mock_mcp:
      lid = w.ensure_flight_deck_gmail_label()
      mock_mcp.assert_not_called()
    self.assertEqual(lid, "Label_cached")

  def test_ensure_flight_deck_label_found_in_list(self):
    fake_list = {
        "userLabels": [
            {"id": "Label_1", "name": "Work"},
            {"id": "Label_2", "name": "Flight Deck"},
        ]
    }
    with mock.patch.object(w, "call_gmail_mcp", return_value=fake_list) as mock_mcp:
      lid = w.ensure_flight_deck_gmail_label()
      mock_mcp.assert_called_once_with("list_labels", {})
    self.assertEqual(lid, "Label_2")
    self.assertEqual(w._GMAIL_LABEL_ID_CACHE.get("Flight Deck"), "Label_2")

  def test_ensure_flight_deck_label_created_if_missing(self):
    fake_list = {"userLabels": [{"id": "Label_1", "name": "Work"}]}
    fake_create = {"id": "Label_new_fd", "name": "Flight Deck"}

    def side_effect(tool, args):
      if tool == "list_labels":
        return fake_list
      if tool == "create_label":
        return fake_create
      return {}

    with mock.patch.object(w, "call_gmail_mcp", side_effect=side_effect) as mock_mcp:
      lid = w.ensure_flight_deck_gmail_label()
      self.assertEqual(mock_mcp.call_count, 2)
    self.assertEqual(lid, "Label_new_fd")
    self.assertEqual(w._GMAIL_LABEL_ID_CACHE.get("Flight Deck"), "Label_new_fd")

  def test_fetch_gmail_status_matches_snippet_and_loads_multi_message_thread(self):
    # Chromium Gerrit email format: CL 8663938 is only in snippet, not in subject
    fake_search = {
        "threads": [
            {
                "id": "th_chromium_1",
                "messageCount": 7,
                "viewUrl": "https://mail.google.com/th_chromium_1",
                "messages": [
                    {
                        "id": "m1",
                        "sender": "gerritcodereview@chromium.org",
                        "subject": "Change in chromium/src[main]: Add text feature",
                        "snippet": "Review of https://chromium-review.googlesource.com/c/8663938",
                        "labelIds": ["INBOX"],
                        "date": "2026-10-30T10:00:00Z",
                    }
                ],
            }
        ]
    }
    full_thread = {
        "thread": {
            "id": "th_chromium_1",
            "messages": [
                {
                    "id": "m1",
                    "sender": "gerritcodereview@chromium.org",
                    "subject": "Change in chromium/src[main]: Add text feature",
                    "snippet": "Review of https://chromium-review.googlesource.com/c/8663938",
                    "labelIds": ["INBOX"],
                    "date": "2026-10-30T10:00:00Z",
                },
                {
                    "id": "m2",
                    "sender": "gerritcodereview@chromium.org",
                    "subject": "Change in chromium/src[main]: Add text feature",
                    "snippet": "Patch Set 2 published",
                    "labelIds": ["INBOX", "UNREAD"],
                    "date": "2026-10-30T12:00:00Z",
                },
                {
                    "id": "m3",
                    "sender": "gerritcodereview@chromium.org",
                    "subject": "Change in chromium/src[main]: Add text feature",
                    "snippet": "Patch Set 3 published",
                    "labelIds": ["TRASH"],
                    "date": "2026-10-30T14:00:00Z",
                },
            ],
        }
    }

    def side_effect(tool, args):
      if tool == "search_threads":
        return fake_search
      if tool == "get_thread":
        return full_thread
      return {}

    with mock.patch.object(w, "call_gmail_mcp", side_effect=side_effect):
      gstat = w.fetch_gmail_status_for_id("8663938")

    self.assertEqual(gstat["thread_ids"], ["th_chromium_1"])
    self.assertEqual(gstat["inbox_thread_ids"], ["th_chromium_1"])
    self.assertEqual(gstat["total_msgs"], 3)
    self.assertEqual(gstat["inbox_msgs"], 2)
    self.assertEqual(gstat["unread_msgs"], 1)
    self.assertEqual(gstat["trash_msgs"], 1)
    self.assertEqual(len(gstat["inbox_messages"]), 2)
    self.assertEqual(gstat["inbox_messages"][0]["id"], "m1")
    self.assertEqual(gstat["inbox_messages"][1]["id"], "m2")
    self.assertTrue(gstat["inbox_messages"][1]["unread"])

  def test_fetch_gmail_status_word_boundary_isolation(self):
    # Target 1234 must not match 12345
    fake_search = {
        "threads": [
            {
                "id": "th_unrelated",
                "messageCount": 1,
                "messages": [
                    {
                        "id": "m_other",
                        "sender": "gerritcodereview@google.com",
                        "subject": "Change 12345 in skia: Fix build",
                        "snippet": "Change 12345",
                        "labelIds": ["INBOX"],
                        "date": "2026-10-30T10:00:00Z",
                    }
                ],
            }
        ]
    }
    with mock.patch.object(w, "call_gmail_mcp", return_value=fake_search):
      gstat = w.fetch_gmail_status_for_id("1234")
    self.assertEqual(gstat["thread_ids"], [])
    self.assertEqual(gstat["inbox_msgs"], 0)

  def test_archive_gmail_threads_labels_flight_deck_and_unlabels_inbox(self):
    calls = []

    def fake_mcp(tool, args):
      calls.append((tool, args))
      if tool == "list_labels":
        return {"userLabels": [{"id": "Label_FD", "name": "Flight Deck"}]}
      return {"ok": True}

    with mock.patch.object(w, "call_gmail_mcp", side_effect=fake_mcp):
      res = w.archive_gmail_threads_for_id(
          "8663938",
          thread_ids=["th1"],
          message_ids=["m1", "m2"],
      )

    self.assertTrue(res["ok"])
    self.assertEqual(res["label_id"], "Label_FD")
    tools_called = [c[0] for c in calls]
    self.assertIn("batch_label_threads", tools_called)
    self.assertIn("batch_unlabel_threads", tools_called)
    self.assertIn("batch_label_messages", tools_called)
    self.assertIn("batch_unlabel_messages", tools_called)

  def test_compute_cutoff_merged_or_removed(self):
    rec_merged = {"status": "MERGED"}
    self.assertEqual(w.compute_processed_mail_cutoff_ts(rec_merged), float("inf"))

    rec_removed = {"status": "NEW", "reviewer_removed": True}
    self.assertEqual(w.compute_processed_mail_cutoff_ts(rec_removed), float("inf"))

  def test_compute_cutoff_waiting_on_others_not_in_attention(self):
    rec = {
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "in_my_attention": False,
        "last_msg_date": "2026-10-30 17:00:00.000000000",
        "status_changed_raw": "2026-10-30 17:01:00.000000000",
        "last_msg_author": "jlavrova@google.com",
    }
    cutoff = w.compute_processed_mail_cutoff_ts(rec)
    expected = w.parse_gerrit_or_iso_time("2026-10-30 17:01:00.000000000").timestamp() + 120.0
    self.assertEqual(cutoff, expected)

  def test_compute_cutoff_needs_attention_active_review_returns_zero(self):
    rec = {
        "status": "NEW",
        "phase": "NEEDS_YOUR_ACTION",
        "in_my_attention": True,
        "chat_busy": True,
        "last_msg_date": "2026-10-31 15:00:00.000000000",
        "last_msg_author": "external_author@gmail.com",
    }
    cutoff = w.compute_processed_mail_cutoff_ts(rec)
    self.assertEqual(cutoff, 0.0)

  def test_sweep_processed_gmail_sweeps_messages_up_to_cutoff(self):
    w._GMAIL_LABEL_ID_CACHE[w.FLIGHT_DECK_GMAIL_LABEL_NAME] = "Label_FD"
    record = {
        "target_id": "1409637",
        "status": "NEW",
        "phase": "WAITING_ON_OTHERS",
        "in_my_attention": False,
        "last_msg_date": "2026-10-30 17:00:00.000000000",
        "last_msg_author": "jlavrova@google.com",
    }
    gstat = {
        "thread_ids": ["th_1"],
        "inbox_thread_ids": ["th_1"],
        "inbox_msgs": 2,
        "unread_msgs": 1,
        "inbox_messages": [
            {
                "id": "m_processed",
                "thread_id": "th_1",
                "date": "2026-10-30T16:00:00Z",
                "unread": True,
            },
            {
                "id": "m_future",
                "thread_id": "th_1",
                "date": "2026-10-30T18:00:00Z",
                "unread": False,
            },
        ],
    }

    archived_calls = []

    def fake_archive(target_id, thread_ids=None, message_ids=None):
      archived_calls.append({"tids": thread_ids, "mids": message_ids})
      return {"ok": True}

    with mock.patch.object(w, "archive_gmail_threads_for_id", side_effect=fake_archive):
      swept_count = w.sweep_processed_gmail_for_record(record, gstat)

    self.assertEqual(swept_count, 1)
    self.assertEqual(archived_calls, [{"tids": None, "mids": ["m_processed"]}])
    self.assertEqual(gstat["inbox_msgs"], 1)
    self.assertEqual(gstat["unread_msgs"], 0)
    self.assertEqual(len(gstat["inbox_messages"]), 1)
    self.assertEqual(gstat["inbox_messages"][0]["id"], "m_future")
    self.assertEqual(gstat["inbox_thread_ids"], ["th_1"])


if __name__ == "__main__":
  unittest.main()
