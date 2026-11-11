#!/usr/bin/env python3
# Copyright 2026 Google LLC
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

"""Contract tests for R95 zero-token external blocker-CL watching (watched_cls.json)."""

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


def _gerrit_info(
    cl_id: str,
    status: str = "NEW",
    patchset: int = 6,
    subject: str = "Add bazel build support for nanobench on Windows.",
    owner_name: str = "Sarah Rust",
) -> dict:
  return {
      "ok": True,
      "kind": "gerrit",
      "host": "skia-review.googlesource.com",
      "url": f"https://skia-review.googlesource.com/c/{cl_id}",
      "status": status,
      "phase": status if status in ("MERGED", "ABANDONED") else "WAITING_ON_OTHERS",
      "patchset": patchset,
      "revision": f"rev-{cl_id}-{patchset}",
      "subject": subject,
      "owner_name": owner_name,
      "owner_email": "sarahrust@google.com",
      "is_self": False,
      "is_external": False,
      "is_wip": False,
      "in_my_attention": False,
      "last_msg_date": "2026-11-11 20:00:00.000000000",
      "last_msg_author": "sarahrust@google.com",
      "last_msg_snippet": "Uploaded patch set.",
  }


class ClWatcherWatchedClsTest(unittest.TestCase):
  """Verifies R95 watched_cls.json polling, watermarks, badges, and one-shot wakeups."""

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

  def _write_watches(self, entries: list[dict]) -> None:
    with open(self.watches_path, "w", encoding="utf-8") as f:
      json.dump(entries, f, indent=2)

  # R95: polling an open watched CL spends zero LLM wakeups, saves its watermark in state,
  # and attaches a timestamp-free badge summary to the unblocked CL record
  def test_open_watched_cl_saves_watermark_and_attaches_stable_badge(self) -> None:
    self._write_watches([
        {
            "key": "gerrit:1422158",
            "host": "skia-review.googlesource.com",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "1401a6c3-2a60-491f-9f22-000b3f950317",
            "note": "Wire ParagraphBench.cpp into Bazel //bench:nanobench",
        }
    ])

    def fake_fetch(cl_id, *_args, **_kwargs):
      if str(cl_id) == "1422158":
        return _gerrit_info("1422158", status="NEW", patchset=6)
      if str(cl_id) == "1423199":
        info = _gerrit_info(
            "1423199",
            status="NEW",
            patchset=3,
            subject="[SkParagraph] Rework nanobench suite",
            owner_name="Julia Lavrova",
        )
        info["is_self"] = True
        info["is_wip"] = True
        info["owner_email"] = "jlavrova@google.com"
        return info
      return {"ok": False, "error": "unexpected"}

    chatted = [
        {
            "item_key": "gerrit:1423199",
            "kind": "gerrit",
            "target_id": "1423199",
            "conversation_id": "1401a6c3-2a60-491f-9f22-000b3f950317",
            "title": "[16:45] ⦿ Gerrit 1423199 R80 Skia text nanobench",
            "project_id": "proj-skp",
            "project_name": "SkParagraph",
            "single_target": True,
            "chat_archived": False,
            "chat_busy": False,
        }
    ]
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
        mock.patch.object(cl_watcher, "overlay_live_chat_busy"),
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(cl_watcher, "wake_conversation") as mock_wake,
        mock.patch.object(cl_watcher.quota_hold, "scan_and_enqueue_halted_chats"),
        mock.patch.object(
            cl_watcher.quota_hold, "flush_ready_quota_holds", return_value=[]
        ),
    ):
      state = cl_watcher.sync_once(
          wake_chats=True, update_titles=False, skia_dir="/nonexistent"
      )

    mock_alert.assert_not_called()
    mock_wake.assert_not_called()
    w_entry = state.get("watched_cls", {}).get("gerrit:1422158")
    self.assertIsInstance(w_entry, dict)
    self.assertEqual(w_entry["status"], "NEW")
    self.assertEqual(w_entry["patchset"], 6)
    self.assertEqual(w_entry["alerted_status"], "")
    self.assertEqual(w_entry["woken_status"], "")
    self.assertTrue(w_entry.get("checked_at"))

    unblocked_rec = state["items"]["gerrit:1423199@1401a6c3-2a60-491f-9f22-000b3f950317"]
    badges = unblocked_rec.get("blocked_by_watches")
    self.assertIsInstance(badges, list)
    self.assertEqual(len(badges), 1)
    self.assertEqual(badges[0]["key"], "gerrit:1422158")
    self.assertEqual(badges[0]["status"], "NEW")
    self.assertEqual(badges[0]["patchset"], 6)
    self.assertNotIn("checked_at", badges[0])

  # R95: transitioning from NEW to MERGED emits one critical event, fires one desktop alert,
  # wakes the latest chain leaf of the unblocked CL chat, and stays idempotent on repeat polls
  def test_transition_to_merged_alerts_and_wakes_unblocked_chat_once(self) -> None:
    self._write_watches([
        {
            "key": "gerrit:1422158",
            "host": "skia-review.googlesource.com",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "root-1401a6c3",
            "note": "Rebase 1423199 onto main and enable //bench:nanobench",
        }
    ])
    state: dict = {"items": {}, "events": []}
    unblocked_rec = {
        "item_key": "gerrit:1423199",
        "kind": "gerrit",
        "target_id": "1423199",
        "host": "skia-review.googlesource.com",
        "url": "https://skia-review.googlesource.com/c/1423199",
        "conversation_id": "mid-piece-id",
        "project_name": "SkParagraph",
        "status": "NEW",
    }
    by_ikey = {"gerrit:1423199": [unblocked_rec]}
    cur_status = {"val": "NEW"}

    def fake_fetch(cl_id, *_args, **_kwargs):
      return _gerrit_info(str(cl_id), status=cur_status["val"], patchset=7)

    with (
        mock.patch.object(cl_watcher, "fetch_gerrit_cl_status", side_effect=fake_fetch),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            return_value=["root-id", "mid-piece-id", "latest-leaf-id"],
        ),
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(
            cl_watcher, "wake_conversation", return_value=True
        ) as mock_wake,
    ):
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey=by_ikey,
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      mock_alert.assert_not_called()
      mock_wake.assert_not_called()

      cur_status["val"] = "MERGED"
      unblocked_rec.pop("blocked_by_watches", None)
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey=by_ikey,
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      self.assertEqual(mock_alert.call_count, 1)
      self.assertEqual(mock_wake.call_count, 1)
      wake_cid, wake_msg = mock_wake.call_args.args[:2]
      self.assertEqual(wake_cid, "latest-leaf-id")
      self.assertEqual(mock_wake.call_args.kwargs.get("item_key"), "gerrit:1423199")
      self.assertIn("1422158", wake_msg)
      self.assertIn("1423199", wake_msg)
      self.assertIn("MERGED", wake_msg)
      self.assertIn("Rebase 1423199 onto main", wake_msg)
      self.assertEqual(len(state["events"]), 1)
      self.assertTrue(state["events"][0]["critical"])
      self.assertEqual(state["events"][0]["conversation_id"], "latest-leaf-id")
      self.assertEqual(
          state["watched_cls"]["gerrit:1422158"]["alerted_status"], "MERGED"
      )
      self.assertEqual(
          state["watched_cls"]["gerrit:1422158"]["woken_status"], "MERGED"
      )

      # Repeat poll at MERGED must not emit duplicate events, alerts, or wakeups
      unblocked_rec.pop("blocked_by_watches", None)
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey=by_ikey,
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      self.assertEqual(mock_alert.call_count, 1)
      self.assertEqual(mock_wake.call_count, 1)
      self.assertEqual(len(state["events"]), 1)

  # R95: when the unblocked CL has no active item record, ABANDONED falls back to the latest
  # leaf of fallback_chain_id so unchatted or temporarily absent WIP CLs still wake their owner
  def test_transition_to_abandoned_uses_fallback_chain_leaf_when_unchatted(self) -> None:
    self._write_watches([
        {
            "key": "gerrit:1422158",
            "host": "skia-review.googlesource.com",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "1401a6c3-root",
            "note": "Check if Sarah moved the change to a new CL",
        }
    ])
    state: dict = {"items": {}, "events": []}
    with (
        mock.patch.object(
            cl_watcher,
            "fetch_gerrit_cl_status",
            return_value=_gerrit_info("1422158", status="ABANDONED", patchset=6),
        ),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            return_value=["1401a6c3-root", "1401a6c3-leaf"],
        ) as mock_chain,
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(
            cl_watcher, "wake_conversation", return_value=True
        ) as mock_wake,
    ):
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey={},
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )

    mock_chain.assert_called_once_with("1401a6c3-root", include_descendants=True)
    self.assertEqual(mock_alert.call_count, 1)
    self.assertEqual(mock_wake.call_count, 1)
    self.assertEqual(mock_wake.call_args.args[0], "1401a6c3-leaf")
    self.assertIn("ABANDONED", mock_wake.call_args.args[1])
    self.assertEqual(
        state["watched_cls"]["gerrit:1422158"]["alerted_status"], "ABANDONED"
    )
    self.assertEqual(
        state["watched_cls"]["gerrit:1422158"]["woken_status"], "ABANDONED"
    )

  # R95: --no-wake (wake_chats=False) suppresses wake_conversation without swallowing the
  # pending chat wakeup on the next live daemon poll
  def test_no_wake_suppresses_wakeup_without_losing_subsequent_live_wakeup(self) -> None:
    self._write_watches([
        {
            "key": "gerrit:1422158",
            "host": "skia-review.googlesource.com",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "chain-1423199",
        }
    ])
    state: dict = {"items": {}, "events": []}
    with (
        mock.patch.object(
            cl_watcher,
            "fetch_gerrit_cl_status",
            return_value=_gerrit_info("1422158", status="MERGED", patchset=6),
        ),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            return_value=["chain-1423199"],
        ),
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(
            cl_watcher, "wake_conversation", return_value=True
        ) as mock_wake,
    ):
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey={},
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=False,
      )
      self.assertEqual(mock_alert.call_count, 1)
      mock_wake.assert_not_called()
      self.assertEqual(state["watched_cls"]["gerrit:1422158"]["woken_status"], "")

      # Transient wake failure (not quota-held) retries on the next poll without re-alerting
      with (
          mock.patch.object(cl_watcher, "wake_conversation", return_value=False) as mock_fail_wake,
          mock.patch.object(cl_watcher.quota_hold, "load_hold_queue", return_value={}),
      ):
        cl_watcher.poll_watched_cls(
            state=state,
            by_ikey={},
            fetched_cache={},
            rollers={},
            incidents=[],
            events=state["events"],
            wake_chats=True,
        )
        self.assertEqual(mock_fail_wake.call_count, 1)
        self.assertEqual(mock_alert.call_count, 1)
        self.assertEqual(state["watched_cls"]["gerrit:1422158"]["woken_status"], "")

      # Subsequent live poll delivers the chat wakeup once without duplicating the alert
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey={},
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      self.assertEqual(mock_alert.call_count, 1)
      self.assertEqual(mock_wake.call_count, 1)
      self.assertEqual(
          state["watched_cls"]["gerrit:1422158"]["woken_status"], "MERGED"
      )

  # R95: GitHub repo#num keys route to fetch_github_pr_status and transient fetch failures
  # preserve prior watermarks without firing false alerts
  def test_github_watched_pr_closed_and_transient_fetch_error_resilience(self) -> None:
    self._write_watches([
        {
            "key": "github:flutter/engine#55123",
            "fallback_chain_id": "gh-chain-1",
            "note": "Sync upstream engine PR",
        }
    ])
    state: dict = {"items": {}, "events": []}
    responses = [
        {"ok": False, "error": "gh timeout"},
        {
            "ok": True,
            "kind": "github",
            "repo_slug": "flutter/engine",
            "url": "https://github.com/flutter/engine/pull/55123",
            "status": "CLOSED",
            "patchset": 2,
            "subject": "Refactor engine flags",
            "owner_name": "octocat",
        },
    ]
    with (
        mock.patch.object(
            cl_watcher, "fetch_github_pr_status", side_effect=responses
        ) as mock_gh,
        mock.patch.object(
            cl_watcher.cl, "discover_conversation_chain", return_value=["gh-chain-1"]
        ),
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(
            cl_watcher, "wake_conversation", return_value=True
        ) as mock_wake,
    ):
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey={},
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      mock_alert.assert_not_called()
      mock_wake.assert_not_called()

      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey={},
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
      self.assertEqual(mock_gh.call_count, 2)
      self.assertEqual(mock_gh.call_args.args[0], "55123")
      self.assertEqual(mock_gh.call_args.kwargs.get("repo_slug"), "flutter/engine")
      self.assertEqual(mock_alert.call_count, 1)
      self.assertEqual(mock_wake.call_count, 1)
      self.assertIn("CLOSED", mock_wake.call_args.args[1])

  # B1: concurrent sync_once sealing alerted_status/woken_status on disk must survive 3-way merge
  # without resurrecting entries removed from watched_cls.json
  def test_merge_concurrent_state_preserves_watched_cls_watermarks(self) -> None:
    disk_on_file = {
        "watched_cls": {
            "gerrit:1422158": {
                "key": "gerrit:1422158",
                "status": "MERGED",
                "alerted_status": "MERGED",
                "woken_status": "MERGED",
            },
            "gerrit:999999": {
                "key": "gerrit:999999",
                "status": "NEW",
                "alerted_status": "",
                "woken_status": "",
            },
        }
    }
    with open(self.state_path, "w", encoding="utf-8") as f:
      json.dump(disk_on_file, f)
    baseline = {
        "watched_cls": {
            "gerrit:1422158": {
                "key": "gerrit:1422158",
                "status": "NEW",
                "alerted_status": "",
                "woken_status": "",
            },
            "gerrit:999999": {
                "key": "gerrit:999999",
                "status": "NEW",
                "alerted_status": "",
                "woken_status": "",
            },
        }
    }
    in_flight = {
        "watched_cls": {
            "gerrit:1422158": {
                "key": "gerrit:1422158",
                "status": "NEW",
                "alerted_status": "",
                "woken_status": "",
            }
        }
    }
    cl_watcher.merge_concurrent_state_mutations(in_flight, baseline)
    w = in_flight["watched_cls"]["gerrit:1422158"]
    self.assertEqual(w["alerted_status"], "MERGED")
    self.assertEqual(w["woken_status"], "MERGED")
    self.assertNotIn("gerrit:999999", in_flight["watched_cls"])

  # B2 & S3: cross-forge Gerrit unblocks, repo-qualified GitHub unblocks, Piper unblocks, and
  # flutter/flutter#num normalization resolve cleanly without corrupting host URLs or keys
  def test_cross_forge_repo_qualified_and_piper_unblocks_and_key_norm(self) -> None:
    self._write_watches([
        {"key": "github:flutter/flutter#194980"},
        {"key": "github:194980"},
        {
            "key": "github:flutter/engine#55123",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "chain-g",
        },
        {
            "key": "gerrit:1422158",
            "unblocks": "github:flutter/engine#55124",
        },
        {
            "key": "gerrit:1422159",
            "unblocks": "piper:1008075988",
            "fallback_chain_id": "chain-p",
        },
    ])
    parsed = cl_watcher.load_watched_cls()
    self.assertEqual([e["key"] for e in parsed], [
        "github:194980",
        "github:flutter/engine#55123",
        "gerrit:1422158",
        "gerrit:1422159",
    ])
    state: dict = {"items": {}, "events": []}
    gh_unblocked = {
        "item_key": "github:55124",
        "kind": "github",
        "target_id": "55124",
        "repo_slug": "flutter/engine",
        "url": "https://github.com/flutter/engine/pull/55124",
        "conversation_id": "gh-cid-55124",
        "status": "OPEN",
    }
    by_ikey = {"github:55124": [gh_unblocked]}
    with (
        mock.patch.object(
            cl_watcher,
            "fetch_github_pr_status",
            side_effect=lambda tid, **_kw: {
                "ok": True,
                "kind": "github",
                "repo_slug": "flutter/engine" if str(tid) == "55123" else "flutter/flutter",
                "url": f"https://github.com/flutter/engine/pull/{tid}",
                "status": "MERGED" if str(tid) == "55123" else "OPEN",
                "patchset": 1,
                "subject": "PR",
                "owner_name": "dev",
            },
        ),
        mock.patch.object(
            cl_watcher,
            "fetch_gerrit_cl_status",
            side_effect=lambda cid, *_a, **_kw: _gerrit_info(str(cid), status="MERGED"),
        ),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            side_effect=lambda cid, **_kw: [cid],
        ),
        mock.patch.object(cl_watcher, "send_desktop_alert"),
        mock.patch.object(cl_watcher, "wake_conversation", return_value=True) as mock_wake,
    ):
      cl_watcher.poll_watched_cls(
          state=state,
          by_ikey=by_ikey,
          fetched_cache={},
          rollers={},
          incidents=[],
          events=state["events"],
          wake_chats=True,
      )
    self.assertEqual(len(gh_unblocked.get("blocked_by_watches") or []), 1)
    msgs_by_cid = {c.args[0]: c.args[1] for c in mock_wake.call_args_list}
    self.assertIn(
        "[Skia CL 1423199](https://skia-review.googlesource.com/c/1423199)",
        msgs_by_cid["chain-g"],
    )
    self.assertIn(
        "[PR #55124](https://github.com/flutter/engine/pull/55124)",
        msgs_by_cid["gh-cid-55124"],
    )
    self.assertIn(
        "[cl/1008075988](http://cl/1008075988)",
        msgs_by_cid["chain-p"],
    )

  # B3 & B4: reopening a CLOSED/ABANDONED watch resets terminal watermarks so re-closing alerts
  # again, and transient fetch errors retain cached blocked_by_watches badges on rebuilt items
  def test_reopen_resets_terminal_watermarks_and_error_keeps_cached_badge(self) -> None:
    self._write_watches([
        {
            "key": "gerrit:1422158",
            "unblocks": "gerrit:1423199",
            "fallback_chain_id": "chain-1423199",
        }
    ])
    state: dict = {"items": {}, "events": []}
    responses = [
        _gerrit_info("1422158", status="ABANDONED", patchset=6),
        _gerrit_info("1422158", status="NEW", patchset=7),
        {"ok": False, "error": "timeout"},
        _gerrit_info("1422158", status="ABANDONED", patchset=7),
    ]
    with (
        mock.patch.object(cl_watcher, "fetch_gerrit_cl_status", side_effect=responses),
        mock.patch.object(
            cl_watcher.cl,
            "discover_conversation_chain",
            return_value=["chain-1423199"],
        ),
        mock.patch.object(cl_watcher, "send_desktop_alert") as mock_alert,
        mock.patch.object(cl_watcher, "wake_conversation", return_value=True) as mock_wake,
    ):
      for i in range(4):
        fresh_unblocked = {"item_key": "gerrit:1423199", "status": "NEW"}
        cl_watcher.poll_watched_cls(
            state=state,
            by_ikey={"gerrit:1423199": [fresh_unblocked]},
            fetched_cache={},
            rollers={},
            incidents=[],
            events=state["events"],
            wake_chats=True,
        )
        if i == 1:
          self.assertEqual(state["watched_cls"]["gerrit:1422158"]["alerted_status"], "")
          self.assertEqual(state["watched_cls"]["gerrit:1422158"]["woken_status"], "")
        if i == 2:
          # Poll 3 failed transiently: rebuilt item must still carry cached PS7 NEW badge
          badges = fresh_unblocked.get("blocked_by_watches") or []
          self.assertEqual(len(badges), 1)
          self.assertEqual(badges[0]["status"], "NEW")
          self.assertEqual(badges[0]["patchset"], 7)
      self.assertEqual(mock_alert.call_count, 2)
      self.assertEqual(mock_wake.call_count, 2)

  # R95: unit tests that redirect STATE_FILE or mock load_watcher_state without patching
  # WATCHED_CLS_FILE must not leak entries from the live ~/.gemini/config/watched_cls.json
  def test_load_watched_cls_isolates_redirected_or_mocked_watcher_state(self) -> None:
    live_default = os.path.join(self._tmp.name, "live_watched_cls.json")
    with open(live_default, "w", encoding="utf-8") as f:
      json.dump([{"key": "gerrit:1422158"}], f)
    other_state = os.path.join(self._tmp.name, "isolated", "state.json")
    os.makedirs(os.path.dirname(other_state), exist_ok=True)
    with (
        mock.patch.object(cl_watcher, "DEFAULT_WATCHED_CLS_FILE", live_default),
        mock.patch.object(cl_watcher, "WATCHED_CLS_FILE", live_default),
    ):
      with mock.patch.object(cl_watcher, "STATE_FILE", other_state):
        self.assertEqual(cl_watcher.load_watched_cls(), [])
      with (
          mock.patch.object(cl_watcher, "STATE_FILE", cl_watcher.DEFAULT_STATE_FILE),
          mock.patch.object(cl_watcher, "load_watcher_state", return_value={}),
      ):
        self.assertEqual(cl_watcher.load_watched_cls(), [])
      with mock.patch.object(cl_watcher, "STATE_FILE", cl_watcher.DEFAULT_STATE_FILE):
        self.assertEqual([e["key"] for e in cl_watcher.load_watched_cls()], ["gerrit:1422158"])


if __name__ == "__main__":
  unittest.main()
