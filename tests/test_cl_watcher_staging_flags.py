#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Contract tests for R97 hybrid staged SK_IGNORE_*/SK_LEGACY_* ifdef cleanup tracker."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import cl_watcher  # pylint: disable=g-import-not-at-top


def _make_skia_cl_info(
    cl_id: str,
    *,
    status: str = "MERGED",
    commit_msg: str = "",
    files: list[str] | None = None,
    all_passed: bool = False,
    is_self: bool = True,
    revision: str = "abc1234567890",
) -> dict:
  post_submit = None
  if status == "MERGED":
    post_submit = {
        "skia_status": "✓",
        "passed_rolls": 4 if all_passed else 2,
        "all_passed": all_passed,
        "has_alert": False,
        "compact_badge": "[✓All Rolls]" if all_passed else "[2/4 Rolls]",
        "rolls": {},
    }
  staged = cl_watcher.extract_staged_ifdef_macros(commit_msg, files or [])
  return {
      "ok": True,
      "kind": "gerrit",
      "host": "skia-review.googlesource.com",
      "url": f"https://skia-review.googlesource.com/c/{cl_id}",
      "status": status,
      "phase": (
          "READY_TO_CLOSE"
          if (status == "MERGED" and all_passed)
          else ("POST_SUBMIT_ROLLING" if status == "MERGED" else "WAITING_ON_OTHERS")
      ),
      "patchset": 4,
      "revision": revision,
      "subject": commit_msg.splitlines()[0] if commit_msg else f"Change {cl_id}",
      "commit_message": commit_msg,
      "files": list(files or ["modules/skparagraph/src/TextLine.cpp"]),
      "staged_ifdef_macros": staged,
      "owner_name": "Julia Lavrova" if is_self else "Other Dev",
      "owner_email": "jlavrova@google.com" if is_self else "other@google.com",
      "is_self": is_self,
      "is_external": False,
      "is_wip": False,
      "in_my_attention": False,
      "last_msg_date": "2026-11-13 15:00:00.000000000",
      "last_msg_author": "jlavrova@google.com",
      "last_msg_snippet": "Patch Set 4.",
      "post_submit": post_submit,
  }


class ClWatcherStagingFlagsTest(unittest.TestCase):
  """Verifies R97 [✓All Rolls] wakeup and 5-stage scanner across Skia and 7 client files."""

  def setUp(self) -> None:
    super().setUp()
    self._tmp = tempfile.TemporaryDirectory()
    self.addCleanup(self._tmp.cleanup)
    self.state_path = os.path.join(self._tmp.name, "cl_watcher_state.json")
    self.watches_path = os.path.join(self._tmp.name, "watched_cls.json")
    p_state = mock.patch.object(cl_watcher, "STATE_FILE", self.state_path)
    p_watch = mock.patch.object(cl_watcher, "WATCHED_CLS_FILE", self.watches_path)
    p_state.start()
    p_watch.start()
    self.addCleanup(p_state.stop)
    self.addCleanup(p_watch.stop)

    # Set up isolated Skia tree and 7 client config files on disk
    self.skia_dir = os.path.join(self._tmp.name, "skia")
    os.makedirs(
        os.path.join(self.skia_dir, "modules", "skparagraph", "src"),
        exist_ok=True,
    )
    os.makedirs(
        os.path.join(self.skia_dir, "modules", "skparagraph", "include"),
        exist_ok=True,
    )
    os.makedirs(
        os.path.join(self.skia_dir, "modules", "skshaper", "include"),
        exist_ok=True,
    )

    self.clients_root = os.path.join(self._tmp.name, "clients")
    self.g3_hdr = os.path.join(self.clients_root, "g3", "SkUserConfig.h")
    self.g3_build = os.path.join(self.clients_root, "g3", "BUILD")
    self.fl_gni = os.path.join(self.clients_root, "flutter", "flutter_defines.gni")
    self.fl_gn = os.path.join(self.clients_root, "flutter", "BUILD.gn")
    self.fl_ck_gn = os.path.join(
        self.clients_root, "flutter", "canvaskit", "BUILD.gn"
    )
    self.cr_hdr = os.path.join(self.clients_root, "chrome", "SkUserConfig.h")
    self.cr_gn = os.path.join(self.clients_root, "chrome", "BUILD.gn")
    for p in (
        self.g3_hdr,
        self.g3_build,
        self.fl_gni,
        self.fl_gn,
        self.fl_ck_gn,
        self.cr_hdr,
        self.cr_gn,
    ):
      os.makedirs(os.path.dirname(p), exist_ok=True)
      with open(p, "w", encoding="utf-8") as f:
        f.write("")

    self.client_config_files = {
        "Google3": (self.g3_hdr, self.g3_build),
        "Flutter": (self.fl_gni, self.fl_gn, self.fl_ck_gn),
        "Chrome": (self.cr_hdr, self.cr_gn),
    }

  # Stub external RPC/process dependencies of sync_once while yielding mock_wake
  @contextlib.contextmanager
  def _patch_sync_once_deps(self, chatted, fake_fetch, chain_ids=None):
    with (
        mock.patch.object(cl_watcher, "fetch_gardener_snapshot", return_value=({}, [])),
        mock.patch.object(cl_watcher, "load_project_names", return_value={}),
        mock.patch.object(cl_watcher, "discover_active_cl_chats", return_value=chatted),
        mock.patch.object(cl_watcher, "discover_open_remote_changes", return_value=[]),
        mock.patch.object(cl_watcher, "fetch_gerrit_cl_status", side_effect=fake_fetch),
        mock.patch.object(
            cl_watcher,
            "fetch_gmail_status_for_id",
            return_value={"inbox_count": 0, "trash_count": 0, "inbox_thread_ids": []},
        ),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            return_value=chain_ids or ["root-conv", "leaf-conv"],
        ),
        mock.patch.object(cl_watcher, "overlay_live_chat_busy"),
        mock.patch.object(
            cl_watcher, "wake_conversation", return_value=True
        ) as mock_wake,
        mock.patch.object(cl_watcher.quota_hold, "scan_and_enqueue_halted_chats"),
        mock.patch.object(
            cl_watcher.quota_hold, "flush_ready_quota_holds", return_value=[]
        ),
    ):
      yield mock_wake

  # R97: extract_staged_ifdef_macros finds SK_IGNORE_* tokens in commit messages or diff snippets
  # while ignoring long-lived API deprecation guards
  def test_extract_staged_ifdef_macros_filters_and_deduplicates(self) -> None:
    msg = (
        "[SkParagraph] Do not include trailing spaces in word width\n\n"
        "Guarded by #ifdef SK_IGNORE_SOFT_BREAK_WORD_WIDTH and "
        "SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES.\n"
        "Also mentions SK_DISABLE_LEGACY_CLIENT_UNICODE."
    )
    macros = cl_watcher.extract_staged_ifdef_macros(
        msg, ["#ifdef SK_IGNORE_SOFT_BREAK_WORD_WIDTH"]
    )
    self.assertEqual(
        macros,
        [
            "SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES",
            "SK_IGNORE_SOFT_BREAK_WORD_WIDTH",
        ],
    )

  # R97: when a self-authored Skia CL with SK_IGNORE_* transitions to [✓All Rolls],
  # cl_watcher wakes the active chain leaf once and seals staged_ifdef_rolls_woken
  def test_all_rolls_passed_wakes_self_skia_cl_with_staged_ifdef_once(self) -> None:
    commit_msg = (
        "[SkParagraph] Fix word break width\n\n"
        "Staged behind SK_IGNORE_SOFT_BREAK_WORD_WIDTH for Google3 and Flutter."
    )
    rolling_state = {"all_passed": False}

    def fake_fetch(cl_id, *_args, **_kwargs):
      return _make_skia_cl_info(
          str(cl_id),
          status="MERGED",
          commit_msg=commit_msg,
          all_passed=rolling_state["all_passed"],
          is_self=True,
          revision="rev1426776",
      )

    chatted = [
        {
            "item_key": "gerrit:1426776",
            "kind": "gerrit",
            "target_id": "1426776",
            "conversation_id": "root-conv-1426776",
            "title": "[15:00] ⦿ Gerrit 1426776 soft break word width",
            "project_id": "proj-skp",
            "project_name": "SkParagraph",
            "single_target": True,
            "chat_archived": False,
            "chat_busy": False,
        }
    ]
    with self._patch_sync_once_deps(
        chatted, fake_fetch, ["root-conv-1426776", "leaf-conv-1426776"]
    ) as mock_wake:
      # Poll 1: initial seed while still rolling (2/4 rolls)
      cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )
      mock_wake.assert_not_called()

      # Poll 2: transitions to all_passed=True with wake_chats=False (--no-wake)
      rolling_state["all_passed"] = True
      state_no_wake = cl_watcher.sync_once(
          wake_chats=False, update_titles=False, skia_dir="/nonexistent"
      )
      mock_wake.assert_not_called()
      rec_nw = next(iter(state_no_wake["items"].values()))
      self.assertTrue(rec_nw.get("staged_ifdef_rolls_pending_wake"))
      self.assertFalse(rec_nw.get("staged_ifdef_rolls_woken"))

      # Poll 3: live poll delivers the pending Stage 3 wakeup to the latest chain leaf
      state_woken = cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )
      self.assertEqual(mock_wake.call_count, 1)
      w_cid, w_msg = mock_wake.call_args.args[:2]
      self.assertEqual(w_cid, "leaf-conv-1426776")
      self.assertIn("SK_IGNORE_SOFT_BREAK_WORD_WIDTH", w_msg)
      self.assertIn("1426776", w_msg)
      self.assertIn("Stage 3", w_msg)
      rec_w = next(iter(state_woken["items"].values()))
      self.assertTrue(rec_w.get("staged_ifdef_rolls_woken"))
      self.assertFalse(rec_w.get("staged_ifdef_rolls_pending_wake"))

      # Poll 4: subsequent poll is a no-op (idempotent watermark)
      cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )
      self.assertEqual(mock_wake.call_count, 1)

  # R97: normal Skia CLs without SK_IGNORE_* stay silent on [✓All Rolls] (no wakeup spam)
  def test_all_rolls_passed_does_not_wake_normal_skia_cl(self) -> None:
    rolling_state = {"all_passed": False}

    def fake_fetch(cl_id, *_args, **_kwargs):
      return _make_skia_cl_info(
          str(cl_id),
          status="MERGED",
          commit_msg="[SkParagraph] Refactor run metrics",
          all_passed=rolling_state["all_passed"],
          is_self=True,
      )

    chatted = [
        {
            "item_key": "gerrit:1400001",
            "kind": "gerrit",
            "target_id": "1400001",
            "conversation_id": "conv-1400001",
            "title": "[15:00] ⦿ Gerrit 1400001 refactor run metrics",
            "project_id": "proj-skp",
            "project_name": "SkParagraph",
            "single_target": True,
            "chat_archived": False,
            "chat_busy": False,
        }
    ]
    with self._patch_sync_once_deps(chatted, fake_fetch) as mock_wake:
      cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )
      rolling_state["all_passed"] = True
      cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )
      mock_wake.assert_not_called()

  # Seed isolated Skia text/font sources and Google3/Flutter config files for Stages 1-5
  def _seed_five_stage_fixtures(self) -> None:
    with open(
        os.path.join(self.skia_dir, "modules", "skparagraph", "src", "TextWrapper.cpp"),
        "w",
        encoding="utf-8",
    ) as f:
      f.write(
          "#ifdef SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES\n"
          "// legacy truncated lines\n"
          "#endif\n"
          "#ifdef SK_IGNORE_SKPARAGRAPH_ELLIPSIS_FIX\n"
          "// legacy ellipsis fix\n"
          "#endif\n"
          "#ifdef SK_IGNORE_ROLLING_IN_FLIGHT\n"
          "// rolling macro\n"
          "#endif\n"
      )
    with open(
        os.path.join(
            self.skia_dir, "modules", "skparagraph", "include", "ParagraphBuilder.h"
        ),
        "w",
        encoding="utf-8",
    ) as f:
      f.write("#ifndef SK_DISABLE_LEGACY_CLIENT_UNICODE\n#endif\n")
    with open(
        os.path.join(self.skia_dir, "modules", "skshaper", "include", "SkShaper.h"),
        "w",
        encoding="utf-8",
    ) as f:
      f.write("#if !defined(SK_DISABLE_LEGACY_SKSHAPER_FUNCTIONS)\n#endif\n")
    with open(self.g3_hdr, "w", encoding="utf-8") as f:
      f.write(
          "// Staging\n"
          "#define SK_LEGACY_BLEND_FOR_DROP_SHADOWS 1\n"
          "#define SK_DISABLE_LEGACY_SHAPER_FACTORY 1\n"
          "#define SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES 1\n"
          "#define SK_IGNORE_SOFT_BREAK_WORD_WIDTH 1\n"
          "// #define SK_IGNORE_COMMENTED_OUT 1\n"
          "/* #define SK_IGNORE_BLOCK_COMMENTED 1 */\n"
      )
    with open(self.fl_gni, "w", encoding="utf-8") as f:
      f.write(
          "flutter_defines = [\n"
          '  # "SK_IGNORE_HASH_COMMENTED",\n'
          '  "SK_IGNORE_ROLLING_IN_FLIGHT",\n'
          "]\n"
      )

  # R97: scan_staging_flags classifies all 5 stages and enforces Stage 1 race guard,
  # Stage 2 roll ancestor guard, and LONG_LIVED_API_GUARDS allowlist
  def test_scan_staging_flags_five_stages_and_safety_guards(self) -> None:
    self._seed_five_stage_fixtures()

    items = {
        "gerrit:1426776@conv1": _make_skia_cl_info(
            "1426776",
            status="NEW",
            commit_msg="Guard with SK_IGNORE_SOFT_BREAK_WORD_WIDTH",
            all_passed=False,
        ),
        "gerrit:1414545@conv2": _make_skia_cl_info(
            "1414545",
            status="MERGED",
            commit_msg="Guard with SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES",
            all_passed=True,
            revision="sha-1414545",
        ),
    }
    rollers = {
        "Google3": {"last_roll_rev": "sha-g3-head"},
        "Flutter": {"last_roll_rev": "sha-flutter-old"},
        "Chrome": {"last_roll_rev": "sha-cr-head"},
    }

    def fake_intro_sha(_skia_dir, macro):
      if macro == "SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES":
        return "sha-1414545"
      if macro == "SK_IGNORE_ROLLING_IN_FLIGHT":
        return "sha-rolling-new"
      return "sha-old"

    def fake_is_ancestor(_skia_dir, ancestor_sha, desc_sha):
      if ancestor_sha == "sha-rolling-new" and desc_sha == "sha-flutter-old":
        return False
      return True

    with (
        mock.patch.object(
            cl_watcher, "_find_macro_intro_commit", side_effect=fake_intro_sha
        ),
        mock.patch.object(
            cl_watcher, "_git_is_ancestor", side_effect=fake_is_ancestor
        ),
    ):
      report = cl_watcher.scan_staging_flags(
          skia_dir=self.skia_dir,
          items=items,
          rollers=rollers,
          client_config_files=self.client_config_files,
      )

    by_macro = {entry["macro"]: entry for entry in report["flags"]}
    # Long-lived guards and commented/non-text macros must be excluded
    self.assertNotIn("SK_DISABLE_LEGACY_CLIENT_UNICODE", by_macro)
    self.assertNotIn("SK_DISABLE_LEGACY_SKSHAPER_FUNCTIONS", by_macro)
    self.assertNotIn("SK_LEGACY_BLEND_FOR_DROP_SHADOWS", by_macro)
    self.assertNotIn("SK_IGNORE_COMMENTED_OUT", by_macro)
    self.assertNotIn("SK_IGNORE_BLOCK_COMMENTED", by_macro)
    self.assertNotIn("SK_IGNORE_HASH_COMMENTED", by_macro)

    # Stage 1: open Skia CL 1426776 prevents flagging pre-landed Google3 define
    s1 = by_macro["SK_IGNORE_SOFT_BREAK_WORD_WIDTH"]
    self.assertEqual(s1["stage"], "STAGE_1_IN_REVIEW")
    self.assertFalse(s1["actionable"])
    self.assertEqual(s1["clients_defining"], ["Google3"])

    # Stage 2: unrolled Skia commit prevents premature Flutter client removal
    s2 = by_macro["SK_IGNORE_ROLLING_IN_FLIGHT"]
    self.assertEqual(s2["stage"], "STAGE_2_ROLLING")
    self.assertFalse(s2["actionable"])
    self.assertEqual(s2["unrolled_clients"], ["Flutter"])

    # Stage 3: rolled into Google3, ready to flip & remove define in Google3
    s3 = by_macro["SK_IGNORE_ELLIPSIZE_TRUNCATED_LINES"]
    self.assertEqual(s3["stage"], "STAGE_3_CLIENT_FLIP")
    self.assertTrue(s3["actionable"])
    self.assertEqual(s3["clients_defining"], ["Google3"])

    # Stage 4: in Skia main, 0 clients define it -> ready to remove #ifdef in Skia
    s4 = by_macro["SK_IGNORE_SKPARAGRAPH_ELLIPSIS_FIX"]
    self.assertEqual(s4["stage"], "STAGE_4_SKIA_CLEANUP")
    self.assertTrue(s4["actionable"])
    self.assertEqual(s4["clients_defining"], [])

    # Stage 5: orphaned define in Google3 SkUserConfig.h, already absent from Skia main
    s5 = by_macro["SK_DISABLE_LEGACY_SHAPER_FACTORY"]
    self.assertEqual(s5["stage"], "STAGE_5_ORPHAN_CLIENT_DEFINE")
    self.assertTrue(s5["actionable"])
    self.assertEqual(s5["clients_defining"], ["Google3"])

    self.assertEqual(report["actionable_count"], 3)

  # R97: weekly watermark runs scan once per ISO week, snooze hides the card for the current
  # week, force=True re-scans and clears snooze, and 3-way state merge preserves watermarks
  def test_weekly_watermark_snooze_and_concurrent_state_merge(self) -> None:
    with open(self.g3_hdr, "w", encoding="utf-8") as f:
      f.write("// Staging\n#define SK_DISABLE_LEGACY_SHAPER_FACTORY 1\n")

    state: dict = {"items": {}, "events": []}
    with mock.patch.object(cl_watcher, "current_iso_week", return_value="2026-W46"):
      ran1 = cl_watcher.maybe_run_weekly_staging_flags_audit(
          state,
          items={},
          rollers={},
          skia_dir=self.skia_dir,
          client_config_files=self.client_config_files,
      )
      self.assertTrue(ran1)
      self.assertEqual(state["staging_flags_audit"]["last_auto_week"], "2026-W46")
      self.assertEqual(state["staging_flags_audit"]["actionable_count"], 1)

      # Same ISO week without force=True must not re-scan
      with mock.patch.object(cl_watcher, "scan_staging_flags") as mock_scan:
        ran2 = cl_watcher.maybe_run_weekly_staging_flags_audit(
            state,
            items={},
            rollers={},
            skia_dir=self.skia_dir,
            client_config_files=self.client_config_files,
        )
        self.assertFalse(ran2)
        mock_scan.assert_not_called()

      # Snoozing hides the card for 2026-W46; force scan clears snooze
      cl_watcher.save_watcher_state(state)
      res_snooze = cl_watcher.snooze_staging_flags_audit()
      self.assertTrue(res_snooze["ok"])
      disk_after_snooze = cl_watcher.load_watcher_state()
      self.assertEqual(
          disk_after_snooze["staging_flags_audit"]["snoozed_week"], "2026-W46"
      )

      # Concurrent sync_once 3-way merge must preserve the user's snooze mutation on disk
      baseline = copy.deepcopy(state)
      in_flight = copy.deepcopy(state)
      cl_watcher.merge_concurrent_state_mutations(in_flight, baseline)
      self.assertEqual(
          in_flight["staging_flags_audit"]["snoozed_week"], "2026-W46"
      )

      # On-demand scan (force=True) re-scans and clears snoozed_week
      res_scan = cl_watcher.trigger_staging_flags_scan(
          skia_dir=self.skia_dir,
          client_config_files=self.client_config_files,
      )
      self.assertTrue(res_scan["ok"])
      self.assertEqual(res_scan["audit"]["snoozed_week"], "")
      self.assertEqual(res_scan["audit"]["actionable_count"], 1)


if __name__ == "__main__":
  unittest.main()
