#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Unit tests for bidirectional fix-for CL/PR linking in Flight Deck."""

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

import cl_watcher  # pylint: disable=g-import-not-at-top

MAC14_BOT = (
    "Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations"
)
MAC15_BOT = (
    "Test-Mac15-Clang-MacBookPro15.1-CPU-AppleIntel-x86_64-Debug-All-NativeFonts_Fontations"
)


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
    self.assertTrue(cl_watcher.bot_matches_mentions(MAC14_BOT, full_bots, pairs))
    self.assertTrue(cl_watcher.bot_matches_mentions(MAC15_BOT, full_bots, pairs))
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
            "name": MAC15_BOT,
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
                "name": MAC15_BOT,
                "id": "task-mac15",
                "swarming_task_id": "swarm-mac15",
                "latest_main_rev": "3d70807b95",
            },
            {
                "name": MAC14_BOT,
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
    self.assertIn(MAC15_BOT, bot_names)
    self.assertIn(MAC14_BOT, bot_names)

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
                    "task_name": MAC15_BOT,
                    "task_url": "https://task-scheduler.skia.org/task/mac15",
                    "swarming_url": "https://chromium-swarm.appspot.com/task?id=s-mac15",
                },
                {
                    "task_name": MAC14_BOT,
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
    self.assertIn(MAC15_BOT, matched_names)
    self.assertIn(MAC14_BOT, matched_names)

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
            "subject": "Revert SDF overflow change",
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

  def test_extract_cl_ids_matches_gerrit_label_and_url(self) -> None:
    text_label = "[13:24] ⦿ Gerrit 1411716 PS1 bounds check fix"
    cls_hosts = cl_watcher.extract_cl_ids_with_hosts(text_label)
    self.assertIn("1411716", cls_hosts)

    text_url = "https://skia-review.googlesource.com/c/skia/+/1411716/1"
    cls_hosts_url = cl_watcher.extract_cl_ids_with_hosts(text_url)
    self.assertIn("1411716", cls_hosts_url)
    self.assertEqual(
        cls_hosts_url["1411716"], cl_watcher.GERRIT_SKIA_HOST
    )

    text_cr = "https://chromium-review.googlesource.com/c/chromium/src/+/8683229"
    cls_hosts_cr = cl_watcher.extract_cl_ids_with_hosts(text_cr)
    self.assertIn("8683229", cls_hosts_cr)
    self.assertEqual(
        cls_hosts_cr["8683229"], cl_watcher.GERRIT_CHROMIUM_HOST
    )

  def test_extract_state_file_targets_discovers_same_chat_fix_cl(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
      proj_dir = os.path.join(tmp_dir, "proj-skia", "state")
      os.makedirs(proj_dir, exist_ok=True)
      state_file = os.path.join(proj_dir, "chat-d900b6b5.md")
      with open(state_file, "w", encoding="utf-8") as f:
        f.write(
            "# State: b/576664769 SDF overflow (R13) — post-land Mac break, fix CL 1411716\n\n"
            "## Fix CL\n"
            "- [Gerrit 1411716 PS1](https://skia-review.googlesource.com/c/skia/+/1411716/1)\n"
            "- Worktree linked via git config branch.fix.gerritissue 1411716\n"
        )

      extra_cls, extra_prs = cl_watcher.extract_state_file_targets(
          "proj-skia", "chat-d900b6b5", roadmaps_dir=tmp_dir
      )
      self.assertIn("1411716", extra_cls)
      self.assertEqual(
          extra_cls["1411716"], cl_watcher.GERRIT_SKIA_HOST
      )
      self.assertEqual(extra_prs, [])

      summaries = {
          "chat-d900b6b5": {
              "annotations": {
                  "title": "[13:24] ⏳ cl/1404196 b/5766 [🔴Alert · 1/4 rolls]",
                  "archived": False,
              },
              "trajectoryMetadata": {
                  "projectId": "proj-skia",
                  "lastModifiedTime": "2026-10-30T17:24:00Z",
                  "parentConversationId": "",
                  "isBattleModeFork": False,
                  "sourceMetadata": None,
              },
          }
      }
      proj_names = {"proj-skia": "Skia"}
      with mock.patch.object(
          cl_watcher.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
      ), mock.patch.object(
          cl_watcher, "load_project_names", return_value=proj_names
      ), mock.patch.object(
          cl_watcher, "ROADMAPS_DIR", tmp_dir
      ):
        discovered = cl_watcher.discover_active_cl_chats()

      by_key = {d["item_key"]: d for d in discovered}
      self.assertIn("gerrit:1404196", by_key)
      self.assertIn("gerrit:1411716", by_key)
      self.assertEqual(by_key["gerrit:1404196"]["conversation_id"], "chat-d900b6b5")
      self.assertEqual(by_key["gerrit:1411716"]["conversation_id"], "chat-d900b6b5")
      self.assertFalse(by_key["gerrit:1404196"]["single_target"])
      self.assertFalse(by_key["gerrit:1411716"]["single_target"])

  def test_detect_fix_for_merged_fix_cl_and_same_chat(self) -> None:
    broken_cl = {
        "item_key": "gerrit:1404196",
        "kind": "gerrit",
        "target_id": "1404196",
        "conversation_id": "chat-d900b6b5",
        "status": "MERGED",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Fix integer overflow in SDF generation",
        "commit_message": "Fix integer overflow\n\nBug: b/576664769\nChange-Id: I1404196",
        "files": ["src/core/SkRemoteGlyphCache.cpp", "tests/SkRemoteGlyphCacheTest.cpp"],
        "revision": "3d70807b95",
        "submitted": "2026-10-30T16:08:24Z",
        "post_submit": {
            "has_alert": True,
            "broken_bots": [{
                "task_name": MAC14_BOT,
            }],
        },
    }
    fix_cl = {
        "item_key": "gerrit:1411716",
        "kind": "gerrit",
        "target_id": "1411716",
        "conversation_id": "chat-d900b6b5",
        "status": "MERGED",
        "is_self": True,
        "owner_email": "jlavrova@google.com",
        "subject": "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent",
        "commit_message": (
            "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent\n\n"
            "Without this change Test-Mac14 ... fails.\n\n"
            "Bug: b/576664769\n"
            "Change-Id: I1411716"
        ),
        "files": ["tests/SkRemoteGlyphCacheTest.cpp"],
        "revision": "a1b2c3d4e5",
        "created": "2026-10-30T16:52:00Z",
        "submitted": "2026-10-30T17:47:59Z",
        "post_submit": {
            "all_passed": False,
            "passed_rolls": 1,
            "rolls": {"Android": {}, "Chrome": {}, "Google3": {}, "Flutter": {}},
        },
    }
    pairs = cl_watcher.detect_fix_for_pairs({
        "gerrit:1404196": broken_cl,
        "gerrit:1411716": fix_cl,
    })
    self.assertIn("gerrit:1411716", pairs)
    self.assertEqual(pairs["gerrit:1411716"]["broken_key"], "gerrit:1404196")
    self.assertIn("same_chat_followup", pairs["gerrit:1411716"]["reasons"])

  def test_sync_once_fix_for_record_assembly_and_ui_banners(self) -> None:
    summaries = {
        "chat-d900b6b5": {
            "annotations": {
                "title": "[13:24] ⏳ cl/1404196 b/5766 [🔴Alert · 1/4 rolls]",
                "archived": False,
            },
            "trajectoryMetadata": {
                "projectId": "proj-skia",
                "lastModifiedTime": "2026-10-30T17:24:00Z",
                "parentConversationId": "",
                "isBattleModeFork": False,
                "sourceMetadata": None,
            },
        }
    }
    proj_names = {"proj-skia": "Skia"}
    empty_gmail = {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
        "threads": [],
    }

    def fake_gerrit_status(cl_id, *args, **kwargs):
      if cl_id == "1404196":
        return {
            "ok": True,
            "status": "MERGED",
            "phase": "ALERT_ROLL_OR_STATUS",
            "is_self": True,
            "owner_email": "jlavrova@google.com",
            "owner_name": "Julia Lavrova",
            "subject": "Fix integer overflow in SDF generation",
            "commit_message": "Fix integer overflow\n\nBug: b/576664769",
            "files": ["src/core/SkRemoteGlyphCache.cpp", "tests/SkRemoteGlyphCacheTest.cpp"],
            "revision": "3d70807b95",
            "submitted": "2026-10-30T16:08:24Z",
            "post_submit": {
                "has_alert": True,
                "skia_status": "🔴",
                "skia_status_detail": "Blamed by Gardener in Mac14",
                "broken_bots": [{"task_name": MAC14_BOT, "task_url": "https://task/mac14"}],
                "incidents": [{"verdict": "blamed", "task_name": MAC14_BOT}],
                "passed_rolls": 1,
                "all_passed": False,
                "compact_badge": "[🔴Alert · 1/4 rolls]",
            },
            "broken_bots": [{"task_name": MAC14_BOT, "task_url": "https://task/mac14"}],
            "url": "https://skia-review.googlesource.com/c/1404196",
            "patchset": 6,
            "activity_ts": 1793000000.0,
        }
      if cl_id == "1411716":
        return {
            "ok": True,
            "status": "MERGED",
            "phase": "POST_SUBMIT_ROLLING",
            "is_self": True,
            "owner_email": "jlavrova@google.com",
            "owner_name": "Julia Lavrova",
            "subject": "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent",
            "commit_message": (
                "Make SkRemoteGlyphCache_b576664769 bounds check scaler independent\n\n"
                "Without this change Test-Mac14 fails.\n\n"
                "Bug: b/576664769\n"
            ),
            "files": ["tests/SkRemoteGlyphCacheTest.cpp"],
            "revision": "a1b2c3d4e5",
            "submitted": "2026-10-30T17:47:59Z",
            "post_submit": {
                "has_alert": False,
                "skia_status": "✓",
                "skia_status_detail": "Clean on status.skia.org",
                "broken_bots": [],
                "incidents": [],
                "passed_rolls": 1,
                "all_passed": False,
                "rolls": {"Android": {}, "Chrome": {}, "Google3": {}, "Flutter": {}},
                "compact_badge": "[✓St · 1/4 rolls]",
            },
            "url": "https://skia-review.googlesource.com/c/1411716",
            "patchset": 2,
            "activity_ts": 1793001000.0,
        }
      return {"ok": False}

    with tempfile.TemporaryDirectory() as tmp_dir:
      proj_dir = os.path.join(tmp_dir, "proj-skia", "state")
      os.makedirs(proj_dir, exist_ok=True)
      state_file = os.path.join(proj_dir, "chat-d900b6b5.md")
      with open(state_file, "w", encoding="utf-8") as f:
        f.write(
            "# State: SDF overflow (R13) — post-land Mac break, fix CL 1411716\n\n"
            "## Fix CL\n"
            "- [Gerrit 1411716 PS1](https://skia-review.googlesource.com/c/skia/+/1411716/1)\n"
        )
      state_path = os.path.join(tmp_dir, "state.json")

      with mock.patch.object(cl_watcher, "STATE_FILE", state_path), \
           mock.patch.object(cl_watcher, "ROADMAPS_DIR", tmp_dir), \
           mock.patch.object(
               cl_watcher.cl, "_fetch_ls_summaries_for_audit", return_value=summaries
           ), \
           mock.patch.object(cl_watcher, "load_project_names", return_value=proj_names), \
           mock.patch.object(cl_watcher, "fetch_gardener_snapshot", return_value=({}, [])), \
           mock.patch.object(cl_watcher, "discover_open_remote_changes", return_value=[]), \
           mock.patch.object(
               cl_watcher, "fetch_gerrit_cl_status", side_effect=fake_gerrit_status
           ), \
           mock.patch.object(
               cl_watcher, "fetch_gmail_status_for_id", return_value=empty_gmail
           ):
        state = cl_watcher.sync_once(
            wake_chats=False, update_titles=False, skia_dir="/nonexistent"
        )

    items = state.get("items") or {}
    broken_rec = items.get("gerrit:1404196@chat-d900b6b5")
    fix_rec = items.get("gerrit:1411716@chat-d900b6b5")
    self.assertIsNotNone(broken_rec)
    self.assertIsNotNone(fix_rec)

    # 1. Broken CL has fixes entry with roll progress summary
    fixes = broken_rec.get("fixes") or []
    self.assertEqual(len(fixes), 1)
    self.assertEqual(fixes[0]["item_key"], "gerrit:1411716")
    self.assertEqual(fixes[0]["label"], "Gerrit 1411716")
    self.assertEqual(fixes[0]["status_summary"], "merged · 1/4 rolls")

    # 2. Fix CL points back to broken CL and carries broken bots
    self.assertEqual(fix_rec.get("fixes_breakage_from"), "gerrit:1404196")
    self.assertEqual(fix_rec.get("fixes_breakage_label"), "Gerrit 1404196")
    self.assertEqual(
        fix_rec.get("fixes_breakage_url"),
        "https://skia-review.googlesource.com/c/1404196",
    )
    self.assertTrue(len(fix_rec.get("fixes_breakage_bots") or []) >= 1)

    # 3. UI banner formatting produces valid HTML with labels
    js_code = r"""
const assert = require('assert');
const html = process.argv[1];
const brokenJson = JSON.parse(process.argv[2]);
const fixJson = JSON.parse(process.argv[3]);
const scriptMatch = html.match(/<script>([\s\S]*?)<\/script>/);
assert(scriptMatch, 'Script block found');
const scriptContent = scriptMatch[1].split('\nboot();')[0];

global.navigator = { clipboard: { writeText: async () => {} } };
global.window = {};
global.location = { hash: '' };
global.document = {
  getElementById: () => ({ innerHTML: '', textContent: '', style: {} }),
  addEventListener: () => {}
};

eval(scriptContent);

const brokenBanner = brokenAndFixBannerHtml(brokenJson);
assert(brokenBanner.includes('fix:'), 'fix: label present in banner: ' + brokenBanner);
assert(brokenBanner.includes('Gerrit 1411716'), 'Gerrit 1411716 present: ' + brokenBanner);
assert(brokenBanner.includes('merged · 1/4 rolls'), 'roll progress present: ' + brokenBanner);

const fixBanner = fixesBreakageBannerHtml(fixJson);
assert(fixBanner.includes('Fixes breakage from →'), 'Fixes label present: ' + fixBanner);
assert(fixBanner.includes('Gerrit 1404196'), 'Gerrit 1404196 present: ' + fixBanner);

console.log('UI banners rendered successfully');
process.exit(0);
"""
    cmd = [
        "node",
        "-e",
        js_code,
        cl_watcher.DASHBOARD_HTML,
        json.dumps(broken_rec),
        json.dumps(fix_rec),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    self.assertEqual(res.returncode, 0, f"Node output: {res.stdout}\nError: {res.stderr}")


if __name__ == "__main__":
  unittest.main()
