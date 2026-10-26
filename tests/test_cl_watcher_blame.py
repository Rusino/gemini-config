#!/usr/bin/env python3
"""Contract tests for how cl_watcher turns Skia Gardener incidents into CL alerts."""

import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher as w  # pylint: disable=g-import-not-at-top

CULPRIT = "6961f82ce1a70712b7ee4f9b32064fede2fa6f25"
BYSTANDER = "4a09b7aac543649ba599e6456a982e1c54fc4a88"


def make_incident(
    commits, culprit=CULPRIT, flake=False, confidence="High", score=85, bots=1
):
  return {
      "task": {
          "commits": commits,
          "name": "Test-Win11-MSVC-Golo-GPU-QuadroP400-x86_64-Debug-All",
          "id": "COj9M2DnF7eZNfMJY2d8",
          "swarming_task_id": "7b91bb11c6d94310",
      },
      "affected_tasks": [
          {"classification": "TEST_FAILURE", "is_intermittent_flake": flake}
      ] * bots,
      "bot_count": bots,
      "is_intermittent_flake": flake,
      "blame": {
          "commit_hash": culprit,
          "confidence": confidence,
          "confidence_score": score,
          "gerrit_change_number": "1400736",
          "gerrit_url": "https://skia-review.googlesource.com/c/skia/+/1400736",
      },
  }


class IncidentVerdictTest(unittest.TestCase):

  def test_blamelist_bystander_is_not_accused(self):
    inc = make_incident([CULPRIT, BYSTANDER])
    res = w.check_skia_post_submit(BYSTANDER, {}, [inc], skia_dir="/nonexistent")
    self.assertFalse(res["has_alert"])
    self.assertEqual(res["incidents"][0]["verdict"], "blamelist")
    self.assertEqual(res["incidents"][0]["culprit_cl"], "1400736")

  def test_confident_culprit_raises_alert(self):
    inc = make_incident([CULPRIT, BYSTANDER])
    res = w.check_skia_post_submit(CULPRIT, {}, [inc], skia_dir="/nonexistent")
    self.assertTrue(res["has_alert"])
    self.assertEqual(res["skia_status"], "🔴")

  def test_flake_culprit_stays_informational(self):
    inc = make_incident([CULPRIT], flake=True)
    res = w.check_skia_post_submit(CULPRIT, {}, [inc], skia_dir="/nonexistent")
    self.assertFalse(res["has_alert"])
    self.assertEqual(res["incidents"][0]["verdict"], "flake")

  def test_low_confidence_single_bot_stays_informational(self):
    inc = make_incident([CULPRIT], confidence="Low", score=15)
    res = w.check_skia_post_submit(CULPRIT, {}, [inc], skia_dir="/nonexistent")
    self.assertFalse(res["has_alert"])
    self.assertEqual(res["incidents"][0]["verdict"], "low_confidence")

  def test_low_confidence_multi_bot_still_alerts(self):
    inc = make_incident([CULPRIT], confidence="Low", score=15, bots=3)
    res = w.check_skia_post_submit(CULPRIT, {}, [inc], skia_dir="/nonexistent")
    self.assertTrue(res["has_alert"])

  def test_unrelated_incident_is_ignored(self):
    inc = make_incident([CULPRIT])
    res = w.check_skia_post_submit(BYSTANDER, {}, [inc], skia_dir="/nonexistent")
    self.assertEqual(res["incidents"], [])
    self.assertEqual(res["skia_status"], "✓")


class RollVerdictTest(unittest.TestCase):

  def setUp(self):
    self._orig = w._git_is_ancestor
    w._git_is_ancestor = lambda _dir, _sha, rev: rev == "curr"

  def tearDown(self):
    w._git_is_ancestor = self._orig

  def roll(self, category, summary):
    return {
        "Chrome": {
            "current_roll_rev": "curr",
            "last_roll_rev": "last",
            "num_failed": 1,
            "url": "https://autoroll.skia.org/r/skia-autoroll",
            "diagnosis": {"fault_category": category, "culprit_summary": summary},
        }
    }

  def test_upstream_roll_listing_exonerated_commit_does_not_alert(self):
    summary = f"Chrome TryJob flake. Excluded 3 non-Chrome commit(s) ({BYSTANDER[:7]})."
    res = w.check_skia_post_submit(
        BYSTANDER, self.roll("Upstream", summary), [], cl_id="1403456"
    )
    self.assertFalse(res["has_alert"])
    self.assertEqual(res["rolls"]["Chrome"]["state"], "🟠")

  def test_skia_fault_roll_naming_cl_alerts(self):
    res = w.check_skia_post_submit(
        BYSTANDER, self.roll("Skia", "Broken by CL 1403456"), [], cl_id="1403456"
    )
    self.assertTrue(res["has_alert"])
    self.assertEqual(res["rolls"]["Chrome"]["state"], "🔴")


class RollBadgeTitleTest(unittest.TestCase):

  def setUp(self):
    self.calls = []
    self._orig = w.cl.set_conversation_topic
    w.cl.set_conversation_topic = lambda _cid, topic: self.calls.append(topic)

  def tearDown(self):
    w.cl.set_conversation_topic = self._orig

  def test_long_title_keeps_cl_and_fits(self):
    w.update_chat_roll_badge("c", "[16:56] ⦿ Code Review cl/1400736", "[⏳St · 0/4 rolls]")
    self.assertEqual(len(self.calls), 1)
    self.assertRegex(self.calls[0], r"^cl/1400736 ")
    self.assertLessEqual(len(self.calls[0]), w.TITLE_TOPIC_MAX)

  def test_same_badge_does_not_rename(self):
    w.update_chat_roll_badge("c", "[11:17] ⦿ cl/1396956 [⏳St · 0/4 rolls]", "[⏳St · 0/4 rolls]")
    self.assertEqual(self.calls, [])


class AutorollFilterTest(unittest.TestCase):

  def test_detects_github_flutter_autoroll_by_author_and_subject(self):
    self.assertTrue(
        w.is_autoroll_change({
            "is_self": False,
            "owner_name": "engine-flutter-autoroll",
            "owner_email": "engine-flutter-autoroll",
            "subject": "Roll Skia from d131b7789482 to a97602cf660a (4 revisions)",
        })
    )

  def test_detects_gerrit_service_account_roll(self):
    self.assertTrue(
        w.is_autoroll_change({
            "is_self": False,
            "owner_name": "skia-autoroll",
            "owner_email": "skia-autoroll@skia-public.iam.gserviceaccount.com",
            "subject": "Roll Dawn from 112233445566 to 778899aabbcc (2 revisions)",
        })
    )

  def test_detects_piper_skia_roll(self):
    self.assertTrue(
        w.is_autoroll_change({
            "is_self": False,
            "owner_name": "skia-autoroll",
            "owner_email": "skia-autoroll@google.com",
            "subject": "Roll Skia 6961f82ce1..a97602cf66 (8 commits)",
        })
    )

  def test_keeps_human_and_self_changes(self):
    self.assertFalse(
        w.is_autoroll_change({
            "is_self": True,
            "owner_name": "Rusino",
            "owner_email": "jlavrova@google.com",
            "subject": "Roll Skia from d131b7789482 to a97602cf660a",
        })
    )
    self.assertFalse(
        w.is_autoroll_change({
            "is_self": False,
            "owner_name": "Ben Wagner",
            "owner_email": "bungeman@google.com",
            "subject": "[skparagraph] Reset font resolution caches",
        })
    )

  def test_sync_once_skips_autoroll_and_omits_gmail_lookup(self):
    discovered = [
        {
            "item_key": "github:194466",
            "kind": "github",
            "target_id": "194466",
            "conversation_id": "2bda77e8-8f38-4f6d-92c3-8c2f6c7d6027",
            "title": "[15:40] ⦿ PR #194466",
            "project_id": "proj-fd",
            "project_name": "Flight Deck",
            "chat_last_modified": "2026-10-26T19:40:00Z",
            "single_target": True,
        }
    ]
    pr_info = {
        "ok": True,
        "host": "github.com/flutter/flutter",
        "url": "https://github.com/flutter/flutter/pull/194466",
        "status": "OPEN",
        "phase": "NEEDS_YOUR_ACTION",
        "subject": "Roll Skia from d131b7789482 to a97602cf660a (4 revisions)",
        "owner_name": "engine-flutter-autoroll",
        "owner_email": "engine-flutter-autoroll",
        "role": "reviewer",
        "is_self": False,
        "is_external": True,
        "patchset": 1,
        "revision": "a97602cf66",
        "in_my_attention": True,
        "cr_summary": "0 rev",
        "cq_status": "🔴 1 check(s) failed",
        "cq_failed": True,
        "tree_red": False,
        "merge_conflict": False,
        "waiting_on": [],
        "last_msg_author": "engine-flutter-autoroll",
        "last_msg_date": "2026-10-26T19:40:00Z",
        "last_msg_et": "15:40 ET",
        "last_msg_age": "10m ago",
        "last_msg_snippet": "Roll Skia",
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
        "post_submit": None,
    }
    with tempfile.TemporaryDirectory() as tmp:
      state_path = os.path.join(tmp, "state.json")
      with mock.patch.object(w, "STATE_FILE", state_path), mock.patch.object(
          w, "fetch_gardener_snapshot", return_value=({}, [])
      ), mock.patch.object(
          w, "discover_active_cl_chats", return_value=discovered
      ), mock.patch.object(
          w, "fetch_github_pr_status", return_value=pr_info
      ), mock.patch.object(
          w, "fetch_gmail_status_for_id"
      ) as mock_gmail:
        state = w.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    self.assertEqual(state["items"], {})
    mock_gmail.assert_not_called()


if __name__ == "__main__":
  unittest.main()
