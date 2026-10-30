#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for bidirectional fix-for CL/PR linking in Flight Deck."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher  # pylint: disable=g-import-not-at-top


class ClWatcherFixForTest(unittest.TestCase):
  """Tests fix-for signal extraction, pair detection, and post-submit task handling."""

  def test_extract_bug_ids(self) -> None:
    subj = "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent"
    msg = (
        "Without this change Test-Mac14 and Test-Mac15 fail with ...\n\n"
        "Bug: b/576664769\n"
        "Change-Id: I1234567\n"
    )
    bugs = cl_watcher.extract_bug_ids(subj, msg, topic="b576664769")
    self.assertIn("576664769", bugs)

  def test_extract_bot_mentions_and_pairs(self) -> None:
    msg = (
        "Without this change Test-Mac14 and Test-Mac15 "
        "...-Debug-All-NativeFonts_Fontations fail with assertion."
    )
    full_bots, pairs, has_cue = cl_watcher.extract_bot_mentions_and_failure_cues(
        "Fix bounds check", msg
    )
    self.assertTrue(has_cue)
    self.assertTrue(
        cl_watcher.bot_matches_mentions(
            "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
            full_bots,
            pairs,
        )
    )
    self.assertTrue(
        cl_watcher.bot_matches_mentions(
            "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations",
            full_bots,
            pairs,
        )
    )
    self.assertFalse(
        cl_watcher.bot_matches_mentions(
            "Test-Linux-Clang-GCE-CPU-AVX2-x86_64-Debug-All",
            full_bots,
            pairs,
        )
    )

  def test_extract_test_identifiers(self) -> None:
    subj = "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent"
    msg = "Updates SkRemoteGlyphCacheTest to verify bounds."
    files = ["src/core/SkRemoteGlyphCache.cpp", "tests/SkRemoteGlyphCacheTest.cpp"]
    test_files, test_symbols = cl_watcher.extract_test_identifiers(subj, msg, files)
    self.assertIn("tests/SkRemoteGlyphCacheTest.cpp", test_files)
    self.assertIn("SkRemoteGlyphCache_b576664769", test_symbols)

  def test_classify_incident_captures_multiple_affected_tasks_and_test(self) -> None:
    inc = {
        "id": "inc-42",
        "task": {
            "name": (
                "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations"
            ),
            "id": "task-mac15",
            "swarming_task_id": "swarm-mac15",
            "commits": ["3d70807b95"],
        },
        "blame": {
            "commit_hash": "3d70807b95",
            "confidence": "High",
            "confidence_score": 90,
            "reason": "matching failing test 'SkRemoteGlyphCache_b576664769'",
        },
        "affected_tasks": [
            {
                "name": (
                    "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations"
                ),
                "id": "task-mac15",
                "swarming_task_id": "swarm-mac15",
                "latest_main_rev": "3d70807b95",
            },
            {
                "name": (
                    "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations"
                ),
                "id": "task-mac14",
                "swarming_task_id": "swarm-mac14",
                "latest_main_rev": "3d70807b95",
            },
        ],
    }
    h = cl_watcher.classify_incident_for_commit(inc, "3d70807b95")
    self.assertIsNotNone(h)
    self.assertEqual(h["verdict"], "blamed")
    self.assertEqual(h["failing_test"], "SkRemoteGlyphCache_b576664769")
    self.assertEqual(len(h["affected_tasks"]), 2)

    ps_res = cl_watcher.check_skia_post_submit(
        merged_sha="3d70807b95",
        rollers={},
        incidents=[inc],
    )
    self.assertEqual(ps_res["skia_status"], "🔴")
    self.assertEqual(len(ps_res["broken_bots"]), 2)
    bot_names = [b["task_name"] for b in ps_res["broken_bots"]]
    self.assertIn(
        "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations",
        bot_names,
    )
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        bot_names,
    )

  def test_detect_fix_for_real_world_gerrit_pair(self) -> None:
    broken_cl = {
        "item_key": "gerrit:1404196",
        "kind": "gerrit",
        "target_id": "1404196",
        "status": "MERGED",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Fix integer overflow and oversized glyph rasterization in SDF generation",
        "commit_message": "Fix integer overflow\n\nBug: b/576664769\nChange-Id: I1404196",
        "topic": "",
        "hashtags": [],
        "files": ["src/core/SkRemoteGlyphCache.cpp", "tests/SkRemoteGlyphCacheTest.cpp"],
        "revision": "3d70807b95",
        "submitted": "2026-10-30T16:08:00Z",
        "post_submit": {
            "has_alert": True,
            "broken_bots": [
                {
                    "task_name": (
                        "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations"
                    ),
                    "task_url": "https://task-scheduler.skia.org/task/mac15",
                    "swarming_url": "https://chromium-swarm.appspot.com/task?id=s-mac15",
                },
                {
                    "task_name": (
                        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations"
                    ),
                    "task_url": "https://task-scheduler.skia.org/task/mac14",
                    "swarming_url": "https://chromium-swarm.appspot.com/task?id=s-mac14",
                },
            ],
            "incidents": [{
                "verdict": "blamed",
                "failing_test": "SkRemoteGlyphCache_b576664769",
            }],
        },
    }

    fix_cl = {
        "item_key": "gerrit:1411716",
        "kind": "gerrit",
        "target_id": "1411716",
        "status": "NEW",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent",
        "commit_message": (
            "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent\n\n"
            "Without this change Test-Mac14 and Test-Mac15 "
            "...-Debug-All-NativeFonts_Fontations fail with assertion error.\n\n"
            "Bug: b/576664769\n"
            "Change-Id: I1411716"
        ),
        "topic": "b576664769",
        "hashtags": [],
        "files": ["tests/SkRemoteGlyphCacheTest.cpp"],
        "revision": "a1b2c3d4e5",
        "created": "2026-10-30T16:52:00Z",
    }

    items = {
        "gerrit:1404196": broken_cl,
        "gerrit:1411716": fix_cl,
    }

    pairs = cl_watcher.detect_fix_for_pairs(items)
    self.assertIn("gerrit:1411716", pairs)
    match = pairs["gerrit:1411716"]
    self.assertEqual(match["broken_key"], "gerrit:1404196")
    self.assertEqual(len(match["matched_bots"]), 2)
    matched_names = [b["task_name"] for b in match["matched_bots"]]
    self.assertIn(
        "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations",
        matched_names,
    )
    self.assertIn(
        "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations",
        matched_names,
    )

  def test_negative_cases_do_not_link(self) -> None:
    broken_cl = {
        "item_key": "gerrit:1404196",
        "kind": "gerrit",
        "target_id": "1404196",
        "status": "MERGED",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Fix integer overflow",
        "commit_message": "Fix integer overflow\n\nBug: b/576664769",
        "files": ["src/core/SkRemoteGlyphCache.cpp"],
        "revision": "3d70807b95",
        "submitted": "2026-10-30T16:08:00Z",
        "post_submit": {"has_alert": False, "broken_bots": []},
    }
    unrelated_cl = {
        "item_key": "gerrit:1411657",
        "kind": "gerrit",
        "target_id": "1411657",
        "status": "NEW",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Add paragraph cache metrics",
        "commit_message": "Add paragraph cache metrics\n\nBug: b/9999999",
        "files": ["modules/skparagraph/src/ParagraphImpl.cpp"],
        "revision": "feedbeef01",
        "created": "2026-10-30T16:45:00Z",
    }
    diff_author_cl = {
        "item_key": "gerrit:1408496",
        "kind": "gerrit",
        "target_id": "1408496",
        "status": "NEW",
        "is_self": False,
        "owner_email": "other@google.com",
        "subject": "Update SkFont",
        "commit_message": "Update SkFont\n\nBug: b/576664769",
        "files": ["src/core/SkFont.cpp"],
        "revision": "deadbeef02",
        "created": "2026-10-30T16:50:00Z",
    }
    items = {
        "gerrit:1404196": broken_cl,
        "gerrit:1411657": unrelated_cl,
        "gerrit:1408496": diff_author_cl,
    }
    pairs = cl_watcher.detect_fix_for_pairs(items)
    self.assertNotIn("gerrit:1411657", pairs)
    self.assertNotIn("gerrit:1408496", pairs)

  def test_link_fix_changes_inherits_parent_chat_and_explicit_revert(self) -> None:
    active_discovered = [
        {
            "item_key": "gerrit:1404196",
            "kind": "gerrit",
            "target_id": "1404196",
            "conversation_id": "chat-1404196",
            "title": "[12:08] ⦿ Gerrit 1404196 SDF overflow",
            "project_id": "proj-skia",
            "project_name": "Skia",
            "single_target": True,
        },
        {
            "item_key": "gerrit:1411716",
            "kind": "gerrit",
            "target_id": "1411716",
            "conversation_id": "",
            "title": "Unchatted Gerrit CL 1411716",
            "project_id": "",
            "project_name": "Skia",
            "single_target": False,
        },
    ]
    fetched_cache = {
        "gerrit:1404196": {
            "ok": True,
            "status": "MERGED",
            "is_self": True,
            "owner_email": "jlavrova@google.com",
            "subject": "Fix integer overflow in SDF generation",
            "commit_message": "Fix integer overflow\n\nBug: b/576664769",
            "files": ["tests/SkRemoteGlyphCacheTest.cpp"],
            "revision": "3d70807b95",
            "submitted": "2026-10-30T16:08:00Z",
            "post_submit": {"has_alert": True, "broken_bots": []},
        },
        "gerrit:1411716": {
            "ok": True,
            "status": "NEW",
            "is_self": True,
            "owner_email": "jlavrova@google.com",
            "subject": 'Revert "Fix integer overflow in SDF generation"',
            "commit_message": "This reverts commit 3d70807b95.",
            "files": ["tests/SkRemoteGlyphCacheTest.cpp"],
            "revision": "abcdef1234",
            "created": "2026-10-30T16:52:00Z",
        },
    }
    cl_watcher.link_fix_changes_to_parent_chats(active_discovered, fetched_cache)
    self.assertEqual(
        active_discovered[1].get("fixes_breakage_from"), "gerrit:1404196"
    )
    self.assertEqual(
        active_discovered[1].get("conversation_id"), "chat-1404196"
    )
    self.assertFalse(active_discovered[0]["single_target"])


if __name__ == "__main__":
  unittest.main()
