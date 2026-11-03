#!/usr/bin/env python3
"""Tests for auto_ship.py and the stop_guard auto-ship trigger."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
)

import auto_ship
import stop_guard


def _git(repo: str, *args: str) -> str:
  env = {
      **os.environ,
      "GIT_AUTHOR_NAME": "t",
      "GIT_AUTHOR_EMAIL": "t@example.com",
      "GIT_COMMITTER_NAME": "t",
      "GIT_COMMITTER_EMAIL": "t@example.com",
  }
  return subprocess.run(
      ["git", "-C", repo, *args],
      capture_output=True,
      text=True,
      check=True,
      env=env,
  ).stdout.strip()


def _edit_step(idx: int, path: str) -> dict:
  return {
      "step_index": idx,
      "type": "PLANNER_RESPONSE",
      "tool_calls": [
          {"name": "write_file", "args": {"Path": path, "Content": "x"}}
      ],
  }


class RepoFixture:
  """Bare origin + clone on main with one pushed commit."""

  def __init__(self, root: str):
    self.origin = os.path.join(root, "origin.git")
    self.repo = os.path.realpath(os.path.join(root, "repo"))
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", self.origin], check=True
    )
    subprocess.run(
        ["git", "clone", "-q", self.origin, self.repo],
        check=True,
        capture_output=True,
    )
    _git(self.repo, "checkout", "-q", "-B", "main")
    self.write("seed.txt", "seed\n")
    _git(self.repo, "add", "seed.txt")
    _git(self.repo, "commit", "-q", "-m", "seed")
    _git(self.repo, "push", "-q", "-u", "origin", "main")

  def write(self, rel: str, text: str) -> str:
    path = os.path.join(self.repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
    return path


class TestAutoShip(unittest.TestCase):

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.root = self._tmp.name
    self.fx = RepoFixture(self.root)
    self.transcript = os.path.join(self.root, "transcript.jsonl")
    self._env = patch.dict(os.environ, {"JETSKI_AUTO_SHIP_REPOS": self.fx.repo})
    self._env.start()

  def tearDown(self):
    self._env.stop()
    self._tmp.cleanup()

  def write_transcript(self, steps: list) -> None:
    with open(self.transcript, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")

  def test_plan_ships_only_files_edited_by_this_chat(self):
    own = self.fx.write("hooks/own.py", "print(1)\n")
    foreign = self.fx.write("hooks/foreign.py", "print(2)\n")
    # Running a command that names foreign.py must NOT make it own_dirty
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do it"},
        _edit_step(2, own),
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {"CommandLine": f"python3 {foreign}"},
            }],
        },
    ])
    plan = auto_ship.plan_ship("c1", self.transcript)
    self.assertEqual(len(plan), 1)
    self.assertEqual(plan[0]["repo"], self.fx.repo)
    self.assertEqual(plan[0]["files"], ["hooks/own.py"])
    self.assertTrue(plan[0]["turn_touched"])

  def test_plan_ignores_repos_outside_personal_allowlist(self):
    own = self.fx.write("own.py", "print(1)\n")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do it"},
        _edit_step(2, own),
    ])
    with patch.dict(os.environ, {"JETSKI_AUTO_SHIP_REPOS": "/nonexistent/repo"}):
      self.assertEqual(auto_ship.plan_ship("c1", self.transcript), [])

  def test_plan_marks_edits_from_earlier_turns_as_not_turn_touched(self):
    own = self.fx.write("own.py", "print(1)\n")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "edit"},
        _edit_step(2, own),
        {"step_index": 3, "type": "USER_INPUT", "content": "what time is it"},
    ])
    plan = auto_ship.plan_ship("c1", self.transcript)
    self.assertEqual(plan[0]["files"], ["own.py"])
    self.assertFalse(plan[0]["turn_touched"])

  def test_plan_parses_markdown_file_links_from_chain_state(self):
    own = self.fx.write("hooks/from_prev_chat.py", "print(1)\n")
    state_file = os.path.join(self.root, "state.md")
    with open(state_file, "w", encoding="utf-8") as f:
      f.write(
          "## Current State\n"
          "### Files Modified by Tools in This Conversation\n"
          f"- [file://{own}](file://{own}) (new)\n"
          "### Verified\n"
      )
    self.write_transcript(
        [{"step_index": 1, "type": "USER_INPUT", "content": "continue"}]
    )
    import roadmap

    with (
        patch.object(roadmap, "project_id_for", return_value="p1"),
        patch.object(roadmap, "state_path", return_value=state_file),
    ):
      plan = auto_ship.plan_ship("c1", self.transcript)
    self.assertEqual(len(plan), 1)
    self.assertEqual(plan[0]["files"], ["hooks/from_prev_chat.py"])

  def test_ship_commits_only_own_files_pushes_and_prints_line(self):
    own = self.fx.write("own.py", "print(1)\n")
    self.fx.write("foreign.py", "print(2)\n")
    self.fx.write("foreign_staged.py", "print(3)\n")
    _git(self.fx.repo, "add", "foreign_staged.py")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do it"},
        _edit_step(2, own),
    ])
    out = io.StringIO()
    code = auto_ship.ship(
        "c1", self.transcript, "Add own.py", out=out, run_post_push=False
    )
    self.assertEqual(code, 0, out.getvalue())
    head = _git(self.fx.repo, "rev-parse", "HEAD")
    remote = subprocess.run(
        ["git", "--git-dir", self.fx.origin, "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    self.assertEqual(head, remote)
    self.assertEqual(
        _git(self.fx.repo, "show", "--name-only", "--format=", "HEAD"), "own.py"
    )
    porcelain = _git(self.fx.repo, "status", "--porcelain")
    self.assertIn("?? foreign.py", porcelain)
    self.assertIn("A  foreign_staged.py", porcelain)
    self.assertIn(
        f"committed and pushed: {head[:7]} -> origin/main", out.getvalue()
    )
    reg_path = os.path.join(self.fx.repo, ".git", "jetski_commits.json")
    with open(reg_path, encoding="utf-8") as f:
      reg = json.load(f)
    self.assertEqual(reg["commits"][head]["conv_id"], "c1")

  def test_ship_pushes_existing_own_unpushed_commit_when_tree_clean(self):
    self.fx.write("own.py", "print(1)\n")
    _git(self.fx.repo, "add", "own.py")
    _git(self.fx.repo, "commit", "-q", "-m", "Already committed")
    head = _git(self.fx.repo, "rev-parse", "HEAD")
    with open(
        os.path.join(self.fx.repo, ".git", "jetski_commits.json"),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump({"commits": {head: {"conv_id": "c1", "repo": self.fx.repo}}}, f)
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do it"},
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {
                    "CommandLine": f"git -C {self.fx.repo} commit -m x -- own.py"
                },
            }],
        },
    ])
    plan = auto_ship.plan_ship("c1", self.transcript)
    self.assertEqual(len(plan), 1)
    self.assertEqual(plan[0]["files"], [])
    self.assertEqual(plan[0]["own_unpushed"], [head])
    self.assertTrue(plan[0]["turn_touched"])
    out = io.StringIO()
    code = auto_ship.ship("c1", self.transcript, "", out=out, run_post_push=False)
    self.assertEqual(code, 0, out.getvalue())
    self.assertIn(
        f"committed and pushed: {head[:7]} -> origin/main", out.getvalue()
    )

  def test_ship_refuses_push_over_foreign_unpushed_commit(self):
    self.fx.write("other.py", "print(3)\n")
    _git(self.fx.repo, "add", "other.py")
    _git(self.fx.repo, "commit", "-q", "-m", "other chat commit")
    foreign_sha = _git(self.fx.repo, "rev-parse", "HEAD")
    with open(
        os.path.join(self.fx.repo, ".git", "jetski_commits.json"),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump(
          {"commits": {foreign_sha: {"conv_id": "other", "repo": self.fx.repo}}},
          f,
      )
    own = self.fx.write("own.py", "print(1)\n")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do it"},
        _edit_step(2, own),
    ])
    out = io.StringIO()
    with patch(
        "pre_tool_guard._is_conv_still_active_tip",
        return_value=(True, "Other chat"),
    ):
      code = auto_ship.ship(
          "c1", self.transcript, "Add own.py", out=out, run_post_push=False
      )
    self.assertNotEqual(code, 0)
    self.assertIn("FOREIGN-COMMIT", out.getvalue())
    self.assertNotIn("committed and pushed", out.getvalue())
    remote = subprocess.run(
        ["git", "--git-dir", self.fx.origin, "rev-parse", "main"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    self.assertNotEqual(remote, foreign_sha)

  def test_post_push_steps_map_changed_paths_to_services(self):
    gardener = os.path.realpath(
        os.path.expanduser("~/.gemini/jetski/scratch/skia_gardener")
    )
    config = os.path.realpath(os.path.expanduser("~/.gemini/config"))
    g_steps = [c for c, _ in auto_ship.post_push_steps(gardener, ["server.go", "README.md"])]
    self.assertIn(["go", "build", "-o", "skia-gardener", "."], g_steps)
    self.assertIn(
        ["systemctl", "--user", "restart", "skia-gardener.service"], g_steps
    )
    ov_steps = [c for c, _ in auto_ship.post_push_steps(gardener, ["overlay/main.go"])]
    self.assertIn(
        ["go", "build", "-o", "overlay/skia-status-overlay", "./overlay"],
        ov_steps,
    )
    self.assertEqual(auto_ship.post_push_steps(gardener, ["README.md"]), [])
    c_steps = [
        c for c, _ in auto_ship.post_push_steps(config, ["scripts/cl_watcher.py"])
    ]
    self.assertIn(
        ["systemctl", "--user", "restart", "cl-flight-deck.service"], c_steps
    )
    lc_steps = [
        c for c, _ in auto_ship.post_push_steps(config, ["hooks/chat_lifecycle.py"])
    ]
    self.assertIn(
        ["systemctl", "--user", "restart", "cl-flight-deck.service"], lc_steps
    )
    js_steps = [
        c
        for c, _ in auto_ship.post_push_steps(
            config, ["scripts/jetski_closed_filter.js"]
        )
    ]
    self.assertIn(
        ["python3", "scripts/patch_jetski_hub_binary.py"], js_steps
    )
    self.assertEqual(auto_ship.post_push_steps(config, ["rules/code_style.md"]), [])

  def test_ship_commits_deleted_and_renamed_files(self):
    self.fx.write("del_staged.py", "a\n")
    self.fx.write("del_unstaged.py", "b\n")
    self.fx.write("old_name.py", "c\n")
    _git(self.fx.repo, "add", "del_staged.py", "del_unstaged.py", "old_name.py")
    _git(self.fx.repo, "commit", "-q", "-m", "add targets")
    _git(self.fx.repo, "push", "-q", "origin", "main")

    _git(self.fx.repo, "rm", "-q", "del_staged.py")
    os.remove(os.path.join(self.fx.repo, "del_unstaged.py"))
    _git(self.fx.repo, "mv", "old_name.py", "new_name.py")
    self.fx.write("foreign_staged.py", "keep staged\n")
    _git(self.fx.repo, "add", "foreign_staged.py")

    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "clean up"},
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {
                    "CommandLine": (
                        f"git -C {self.fx.repo} rm del_staged.py del_unstaged.py && "
                        f"git -C {self.fx.repo} mv old_name.py new_name.py"
                    )
                },
            }],
        },
    ])
    out = io.StringIO()
    code = auto_ship.ship(
        "c1", self.transcript, "Remove old files", out=out, run_post_push=False
    )
    self.assertEqual(code, 0, out.getvalue())
    status_head = _git(self.fx.repo, "show", "--name-status", "--format=", "HEAD")
    self.assertIn("del_staged.py", status_head)
    self.assertIn("del_unstaged.py", status_head)
    self.assertIn("new_name.py", status_head)
    self.assertNotIn("foreign_staged.py", status_head)
    self.assertIn("A  foreign_staged.py", _git(self.fx.repo, "status", "--porcelain"))

  def test_ship_foreign_guard_records_block_state_and_handles_shallow_path(self):
    import pre_tool_guard

    self.fx.write("other.py", "print(3)\n")
    _git(self.fx.repo, "add", "other.py")
    _git(self.fx.repo, "commit", "-q", "-m", "other chat commit")
    foreign_sha = _git(self.fx.repo, "rev-parse", "HEAD")
    with open(
        os.path.join(self.fx.repo, ".git", "jetski_commits.json"),
        "w",
        encoding="utf-8",
    ) as f:
      json.dump(
          {"commits": {foreign_sha: {"conv_id": "other", "repo": self.fx.repo}}},
          f,
      )
    own = self.fx.write("own.py", "print(1)\n")

    art_dir = os.path.join(self.root, "brain", "c1")
    deep_trans = os.path.join(
        art_dir, ".system_generated", "logs", "transcript.jsonl"
    )
    os.makedirs(os.path.dirname(deep_trans), exist_ok=True)
    steps = [
        {"step_index": 7, "type": "USER_INPUT", "content": "ship it"},
        _edit_step(8, own),
    ]
    with open(deep_trans, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")

    out = io.StringIO()
    with patch(
        "pre_tool_guard._is_conv_still_active_tip",
        return_value=(True, "Other chat"),
    ):
      code = auto_ship.ship(
          "c1", deep_trans, "Add own.py", out=out, run_post_push=False
      )
    self.assertEqual(code, 1)
    denial = pre_tool_guard.parallel_chat_isolation_denial(
        "write_file",
        {"Path": os.path.join(art_dir, "scratch", ".parallel_chat_override.json")},
        "c1",
        "",
        deep_trans,
        art_dir,
        [],
    )
    self.assertIn("PARALLEL-CHAT GUARD", denial)

    # Shallow test transcript path must fall back to /tmp rather than deriving "/"
    shallow = "/tmp/test_shallow_autoship_transcript.jsonl"
    tmp_state = "/tmp/.parallel_chat_guard_state_c_shallow.json"
    try:
      with open(shallow, "w", encoding="utf-8") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")
      if os.path.exists(tmp_state):
        os.remove(tmp_state)
      with patch(
          "pre_tool_guard._is_conv_still_active_tip",
          return_value=(True, "Other chat"),
      ):
        code2 = auto_ship.ship(
            "c_shallow", shallow, "Add own.py", out=io.StringIO(), run_post_push=False
        )
      self.assertEqual(code2, 1)
      self.assertTrue(os.path.isfile(tmp_state))
    finally:
      for p in (shallow, tmp_state):
        if os.path.exists(p):
          os.remove(p)

  def test_handoff_snapshot_records_config_edits_and_multiple_sections(self):
    import pre_tool_guard
    import roadmap

    manual_own = self.fx.write("hooks/manual_listed.py", "1\n")
    cfg_root = os.path.abspath(os.path.expanduser("~/.gemini/config"))
    snap_cfg = os.path.join(cfg_root, "hooks", "auto_ship.py")
    ignored_rm = os.path.join(cfg_root, "roadmaps", "p1", "state", "c1.md")
    ignored_brain = os.path.join(self.root, "brain", "c1", "artifact.md")

    state_file = os.path.join(self.root, "state.md")
    with open(state_file, "w", encoding="utf-8") as f:
      f.write(
          "# Goal\n"
          "### Files Modified by Tools in This Conversation\n"
          f"- [manual_listed.py](file://{manual_own})\n"
          "## Immediate Next Step\n"
          "- Run tests\n"
      )
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "handoff"},
        _edit_step(2, snap_cfg),
        _edit_step(3, ignored_rm),
        _edit_step(4, ignored_brain),
    ])
    pre_tool_guard.enrich_handoff_summary_files(
        self.root, self.transcript, [], state_file=state_file
    )
    with open(state_file, encoding="utf-8") as f:
      enriched = f.read()
    self.assertIn(f"file://{snap_cfg}", enriched)
    self.assertNotIn(f"file://{ignored_rm}", enriched)
    self.assertNotIn(f"file://{ignored_brain}", enriched)

    snap_own = self.fx.write("hooks/snap_listed.py", "2\n")
    with open(state_file, "w", encoding="utf-8") as f:
      f.write(enriched.replace(snap_cfg, snap_own))
    self.write_transcript(
        [{"step_index": 1, "type": "USER_INPUT", "content": "continue"}]
    )
    with (
        patch.object(roadmap, "project_id_for", return_value="p1"),
        patch.object(roadmap, "state_path", return_value=state_file),
    ):
      plan = auto_ship.plan_ship("c1", self.transcript)
      rm_touched, _, _ = roadmap._collect_chat_vcs_inputs(
          "c1", "p1", self.transcript
      )
    self.assertEqual(
        set(plan[0]["files"]), {"hooks/manual_listed.py", "hooks/snap_listed.py"}
    )
    self.assertIn(os.path.realpath(manual_own), rm_touched)
    self.assertIn(os.path.realpath(snap_own), rm_touched)

  def test_mac_sync_ahead_of_origin_and_timeout_safe(self):
    mac_clone = os.path.realpath(os.path.join(self.root, "mac_ahead"))
    subprocess.run(
        ["git", "clone", "-q", self.fx.origin, mac_clone],
        check=True,
        capture_output=True,
    )
    with open(os.path.join(mac_clone, "local.md"), "w", encoding="utf-8") as f:
      f.write("ahead\n")
    _git(mac_clone, "add", "local.md")
    _git(mac_clone, "commit", "-q", "-m", "local ahead")

    res = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="darwin", min_interval_s=0
    )
    self.assertEqual(res["status"], "up_to_date")

    orig_run = subprocess.run

    def timeout_on_fetch(cmd, *args, **kwargs):
      if "fetch" in cmd:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)
      return orig_run(cmd, *args, **kwargs)

    with patch.object(auto_ship.subprocess, "run", side_effect=timeout_on_fetch):
      res_to = auto_ship.maybe_sync_mac_clone(
          repo=mac_clone, platform="darwin", min_interval_s=0
      )
    self.assertEqual(res_to["status"], "fetch_failed")

  def test_mac_sync_fast_forwards_clean_clone_and_throttles(self):
    mac_clone = os.path.realpath(os.path.join(self.root, "mac_clone"))
    subprocess.run(
        ["git", "clone", "-q", self.fx.origin, mac_clone],
        check=True,
        capture_output=True,
    )
    self.fx.write("new_rule.md", "hi\n")
    _git(self.fx.repo, "add", "new_rule.md")
    _git(self.fx.repo, "commit", "-q", "-m", "rule")
    _git(self.fx.repo, "push", "-q", "origin", "main")
    expected = _git(self.fx.repo, "rev-parse", "HEAD")

    res = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="darwin", min_interval_s=300
    )
    self.assertEqual(res["status"], "updated")
    self.assertEqual(_git(mac_clone, "rev-parse", "HEAD"), expected)
    # Immediate second call is throttled without touching git
    res2 = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="darwin", min_interval_s=300
    )
    self.assertEqual(res2["status"], "throttled")
    # Linux is also supported and finds the tree up to date
    res3 = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="linux", min_interval_s=0
    )
    self.assertEqual(res3["status"], "up_to_date")
    # Unsupported platform is a no-op
    res4 = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="win32", min_interval_s=0
    )
    self.assertEqual(res4["status"], "skip_platform")

  def test_sync_reports_behind_count_for_dirty_and_diverged_clones(self):
    clone = os.path.realpath(os.path.join(self.root, "clone_dirty"))
    subprocess.run(
        ["git", "clone", "-q", self.fx.origin, clone],
        check=True,
        capture_output=True,
    )
    # Dirty clone with no remote changes
    with open(os.path.join(clone, "seed.txt"), "w", encoding="utf-8") as f:
      f.write("modified locally\n")
    res_clean_rem = auto_ship.maybe_sync_mac_clone(
        repo=clone, platform="linux", min_interval_s=0
    )
    self.assertEqual(res_clean_rem["status"], "dirty")
    self.assertEqual(res_clean_rem.get("behind"), 0)
    self.assertFalse(res_clean_rem.get("unknown"))

    # Remote advances while clone is dirty
    self.fx.write("remote_new.txt", "remote\n")
    _git(self.fx.repo, "add", "remote_new.txt")
    _git(self.fx.repo, "commit", "-q", "-m", "remote commit")
    _git(self.fx.repo, "push", "-q", "origin", "main")

    res_dirty_behind = auto_ship.maybe_sync_mac_clone(
        repo=clone, platform="linux", min_interval_s=0
    )
    self.assertEqual(res_dirty_behind["status"], "dirty")
    self.assertEqual(res_dirty_behind.get("behind"), 1)
    self.assertFalse(res_dirty_behind.get("unknown"))
    self.assertFalse(os.path.exists(os.path.join(clone, "remote_new.txt")))

    # Clean working tree with local unpushed commit diverges from remote
    _git(clone, "checkout", "-q", "--", "seed.txt")
    with open(os.path.join(clone, "local.txt"), "w", encoding="utf-8") as f:
      f.write("local\n")
    _git(clone, "add", "local.txt")
    _git(clone, "commit", "-q", "-m", "local commit")
    res_div = auto_ship.maybe_sync_mac_clone(
        repo=clone, platform="linux", min_interval_s=0
    )
    self.assertEqual(res_div["status"], "diverged")
    self.assertEqual(res_div.get("behind"), 1)
    self.assertEqual(res_div.get("ahead"), 1)

  def test_sync_respects_worktree_lease_file_lease_and_foreign_unpushed(self):
    clone = os.path.realpath(os.path.join(self.root, "clone_leases"))
    subprocess.run(
        ["git", "clone", "-q", self.fx.origin, clone],
        check=True,
        capture_output=True,
    )
    self.fx.write("hooks/foo.py", "remote foo\n")
    _git(self.fx.repo, "add", "hooks/foo.py")
    _git(self.fx.repo, "commit", "-q", "-m", "remote foo")
    _git(self.fx.repo, "push", "-q", "origin", "main")

    # Active worktree lease held by another conversation blocks merge
    with (
        patch("pre_tool_guard._is_conv_still_active_tip", return_value=(True, "Other chat")),
        patch("pre_tool_guard._is_worktree_lease_unexpired", return_value=(True, time.time())),
    ):
      with open(os.path.join(clone, ".git", "jetski_lease.json"), "w", encoding="utf-8") as f:
        json.dump({"conv_id": "other_cid", "title": "Other chat", "ts": time.time()}, f)
      res_wt = auto_ship.maybe_sync_mac_clone(
          repo=clone, platform="linux", min_interval_s=0, conv_id="c1"
      )
      self.assertEqual(res_wt["status"], "leased")
      self.assertEqual(res_wt.get("behind"), 1)

    # Active file lease on a file in incoming range blocks merge
    os.remove(os.path.join(clone, ".git", "jetski_lease.json"))
    with (
        patch("pre_tool_guard._is_conv_still_active_tip", return_value=(True, "Other chat")),
        patch("pre_tool_guard._latest_conv_activity_ts", return_value=time.time()),
    ):
      with open(
          os.path.join(clone, ".git", "jetski_file_leases.json"), "w", encoding="utf-8"
      ) as f:
        json.dump({"files": {"hooks/foo.py": {"conv_id": "other_cid", "ts": time.time()}}}, f)
      res_fl = auto_ship.maybe_sync_mac_clone(
          repo=clone, platform="linux", min_interval_s=0, conv_id="c1"
      )
      self.assertEqual(res_fl["status"], "leased")
      self.assertEqual(res_fl.get("behind"), 1)

    # Foreign unpushed commit blocks auto-pull as diverged
    os.remove(os.path.join(clone, ".git", "jetski_file_leases.json"))
    with open(os.path.join(clone, "local_foreign.py"), "w", encoding="utf-8") as f:
      f.write("foreign\n")
    _git(clone, "add", "local_foreign.py")
    _git(clone, "commit", "-q", "-m", "foreign commit")
    foreign_sha = _git(clone, "rev-parse", "HEAD")
    with open(
        os.path.join(clone, ".git", "jetski_commits.json"), "w", encoding="utf-8"
    ) as f:
      json.dump({"commits": {foreign_sha: {"conv_id": "other_cid", "repo": clone}}}, f)
    with patch("pre_tool_guard._is_conv_still_active_tip", return_value=(True, "Other chat")):
      res_foreign = auto_ship.maybe_sync_mac_clone(
          repo=clone, platform="linux", min_interval_s=0, conv_id="c1"
      )
      self.assertEqual(res_foreign["status"], "diverged")
      self.assertEqual(res_foreign.get("behind"), 1)
      self.assertTrue(bool(res_foreign.get("foreign_unpushed")))

  def test_sync_returns_changed_files_and_force_api_bypasses_throttle(self):
    clone = os.path.realpath(os.path.join(self.root, "clone_changed"))
    subprocess.run(
        ["git", "clone", "-q", self.fx.origin, clone],
        check=True,
        capture_output=True,
    )
    self.fx.write("rules/code_style.md", "rule\n")
    self.fx.write("skills/foo/SKILL.md", "skill\n")
    self.fx.write("hooks/bar.py", "hook\n")
    _git(self.fx.repo, "add", "rules/code_style.md", "skills/foo/SKILL.md", "hooks/bar.py")
    _git(self.fx.repo, "commit", "-q", "-m", "update configs")
    _git(self.fx.repo, "push", "-q", "origin", "main")

    res = auto_ship.maybe_sync_mac_clone(
        repo=clone, platform="linux", min_interval_s=300
    )
    self.assertEqual(res["status"], "updated")
    self.assertEqual(
        set(res.get("changed_files") or []),
        {"rules/code_style.md", "skills/foo/SKILL.md", "hooks/bar.py"},
    )

    # Next remote commit is throttled under min_interval_s=300
    self.fx.write("rules/new.md", "more\n")
    _git(self.fx.repo, "add", "rules/new.md")
    _git(self.fx.repo, "commit", "-q", "-m", "new rule")
    _git(self.fx.repo, "push", "-q", "origin", "main")

    res_th = auto_ship.maybe_sync_mac_clone(
        repo=clone, platform="linux", min_interval_s=300
    )
    self.assertEqual(res_th["status"], "throttled")

    # Forced sync bypasses throttle and pulls
    res_forced = auto_ship.force_sync_config_clone(repo=clone, platform="linux")
    self.assertEqual(res_forced["status"], "updated")
    self.assertEqual(res_forced.get("changed_files"), ["rules/new.md"])

    # CLI --force flag also bypasses throttle
    self.fx.write("rules/cli.md", "cli\n")
    _git(self.fx.repo, "add", "rules/cli.md")
    _git(self.fx.repo, "commit", "-q", "-m", "cli rule")
    _git(self.fx.repo, "push", "-q", "origin", "main")
    code = auto_ship._cli(["mac-sync", "--repo", clone, "--force", "--min-interval", "300"])
    self.assertEqual(code, 0)
    self.assertTrue(os.path.exists(os.path.join(clone, "rules/cli.md")))

  def test_roadmap_vcs_baton_action_reports_auto_ship_for_personal_repo(self):
    import roadmap

    own = self.fx.write("own.py", "print(1)\n")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "do work"},
        _edit_step(2, own),
    ])
    out = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=self.transcript)
    self.assertIn("[VCS BATON: PENDING", out)
    self.assertIn("auto_ship.py ship c1", out)
    self.assertNotIn("call ask_question", out)

    # On a feature branch, personal repo falls back to ask_question
    _git(self.fx.repo, "checkout", "-q", "-b", "feat")
    out_feat = roadmap.evaluate_vcs_baton(
        "c1", "p1", transcript_path=self.transcript
    )
    self.assertIn("call ask_question", out_feat)
    info = {
        "roadmap_done_idx": 2,
        "asked_question": False,
        "vcs_baton_lines": [out_feat],
    }
    self.assertIn(
        "[VCS BATON GUARD]",
        stop_guard.vcs_baton_guard_reason(info, "c1", self.transcript),
    )

    # Detached linked worktree of a personal repo auto-ships without ask_question
    _git(self.fx.repo, "checkout", "-q", "main")
    wt = os.path.realpath(os.path.join(self.root, "baton_wt"))
    _git(self.fx.repo, "worktree", "add", "-q", "--detach", wt, "origin/main")
    wt_own = os.path.join(wt, "wt_own.py")
    with open(wt_own, "w", encoding="utf-8") as f:
      f.write("print(2)\n")
    self.write_transcript([
        {"step_index": 3, "type": "USER_INPUT", "content": "work in detached wt"},
        _edit_step(4, wt_own),
    ])
    out_wt = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=self.transcript)
    self.assertIn("[VCS BATON: PENDING", out_wt)
    self.assertIn("auto_ship.py ship c1", out_wt)
    self.assertNotIn("call ask_question", out_wt)

  def test_post_push_linked_config_worktree_deploys_filter_and_syncs_main(self):
    wt = os.path.realpath(os.path.join(self.root, "linked_wt"))
    _git(self.fx.repo, "worktree", "add", "-q", "--detach", wt, "origin/main")
    js_rel = "scripts/jetski_closed_filter.js"
    watcher_rel = "scripts/cl_watcher.py"
    patcher_rel = "scripts/patch_jetski_hub_binary.py"
    marker_file = os.path.join(self.root, "synced_js_from_cwd.txt")
    for base in (self.fx.repo, wt):
      os.makedirs(os.path.join(base, "scripts"), exist_ok=True)
    with open(os.path.join(wt, patcher_rel), "w", encoding="utf-8") as f:
      f.write(
          "import os\n"
          f"with open({marker_file!r}, 'w', encoding='utf-8') as out:\n"
          "  out.write(os.path.realpath(os.getcwd()))\n"
      )
    js_path = os.path.join(wt, js_rel)
    watcher_path = os.path.join(wt, watcher_rel)
    with open(js_path, "w", encoding="utf-8") as f:
      f.write("// v1\n")
    with open(watcher_path, "w", encoding="utf-8") as f:
      f.write("# watcher v1\n")
    self.write_transcript([
        {"step_index": 1, "type": "USER_INPUT", "content": "ship from worktree"},
        _edit_step(2, js_path),
        _edit_step(3, watcher_path),
        _edit_step(4, os.path.join(wt, patcher_rel)),
    ])

    real_run = auto_ship._run_cmd
    restarted_units: list[tuple[str, str]] = []

    def fake_run(cmd, timeout=None, cwd=None):
      if cmd[:3] == ["systemctl", "--user", "cat"]:
        return subprocess.CompletedProcess(cmd, 0, "loaded\n", "")
      if cmd[:3] == ["systemctl", "--user", "restart"]:
        restarted_units.append((cmd[3], os.path.realpath(cwd or "")))
        return subprocess.CompletedProcess(cmd, 0, "", "")
      return real_run(cmd, timeout=timeout, cwd=cwd)

    out = io.StringIO()
    with (
        patch.dict(os.environ, {"JETSKI_CONFIG_REPO": self.fx.repo}),
        patch.object(auto_ship, "_run_cmd", side_effect=fake_run),
    ):
      code = auto_ship.ship(
          "c1",
          self.transcript,
          "Ship Flight Deck from linked worktree",
          out=out,
          run_post_push=True,
          repo_filter=wt,
      )
    self.assertEqual(code, 0, out.getvalue())
    self.assertIn("committed and pushed:", out.getvalue())
    wt_head = _git(wt, "rev-parse", "HEAD")
    self.assertEqual(
        _git(self.fx.repo, "rev-parse", "HEAD"),
        wt_head,
        "Clean primary config checkout must fast-forward to linked worktree commit",
    )
    with open(marker_file, encoding="utf-8") as f:
      self.assertEqual(
          f.read().strip(),
          wt,
          "patch_jetski_hub_binary.py must run inside the linked worktree cwd",
      )
    self.assertIn(
        ("cl-flight-deck.service", self.fx.repo),
        restarted_units,
        "Synced primary config checkout must restart cl-flight-deck.service",
    )

    # Dirty primary checkout deploys filter JS from wt while skipping stale service restart
    restarted_units.clear()
    self.fx.write(watcher_rel, "# foreign dirty edit\n")
    with open(js_path, "w", encoding="utf-8") as f:
      f.write("// v2\n")
    with open(watcher_path, "w", encoding="utf-8") as f:
      f.write("# watcher v2\n")
    self.write_transcript([
        {"step_index": 5, "type": "USER_INPUT", "content": "ship v2"},
        _edit_step(6, js_path),
        _edit_step(7, watcher_path),
    ])
    out2 = io.StringIO()
    with (
        patch.dict(os.environ, {"JETSKI_CONFIG_REPO": self.fx.repo}),
        patch.object(auto_ship, "_run_cmd", side_effect=fake_run),
    ):
      code2 = auto_ship.ship(
          "c1",
          self.transcript,
          "Ship v2 while main dirty",
          out=out2,
          run_post_push=True,
          repo_filter=wt,
      )
    self.assertEqual(code2, 0, out2.getvalue())
    self.assertNotIn(
        "cl-flight-deck.service",
        [u for u, _ in restarted_units],
        "Dirty primary checkout must not restart stale cl-flight-deck.service",
    )
    self.assertIn("dirty", out2.getvalue())

    # Linked worktree of skia_gardener fast-forwards primary checkout and builds/restarts there
    gard_fx = RepoFixture(os.path.join(self.root, "gardener"))
    gard_wt = os.path.realpath(os.path.join(self.root, "gardener_wt"))
    _git(gard_fx.repo, "worktree", "add", "-q", "--detach", gard_wt, "origin/main")
    go_path = os.path.join(gard_wt, "server.go")
    with open(go_path, "w", encoding="utf-8") as f:
      f.write("package main\n")
    self.write_transcript([
        {"step_index": 8, "type": "USER_INPUT", "content": "ship gardener wt"},
        _edit_step(9, go_path),
    ])
    built_cmds: list[tuple[list[str], str]] = []
    restarted_units.clear()

    def fake_gard_run(cmd, timeout=None, cwd=None):
      if cmd[:3] == ["go", "build", "-o"]:
        built_cmds.append((list(cmd), os.path.realpath(cwd or "")))
        return subprocess.CompletedProcess(cmd, 0, "", "")
      return fake_run(cmd, timeout=timeout, cwd=cwd)

    out3 = io.StringIO()
    with (
        patch.dict(
            os.environ,
            {
                "JETSKI_AUTO_SHIP_REPOS": f"{self.fx.repo}:{gard_fx.repo}",
                "JETSKI_CONFIG_REPO": self.fx.repo,
                "JETSKI_GARDENER_REPO": gard_fx.repo,
            },
        ),
        patch.object(auto_ship, "_run_cmd", side_effect=fake_gard_run),
    ):
      code3 = auto_ship.ship(
          "c1",
          self.transcript,
          "Ship gardener from linked worktree",
          out=out3,
          run_post_push=True,
          repo_filter=gard_wt,
      )
    self.assertEqual(code3, 0, out3.getvalue())
    self.assertEqual(
        _git(gard_fx.repo, "rev-parse", "HEAD"),
        _git(gard_wt, "rev-parse", "HEAD"),
        "Clean primary skia_gardener checkout must fast-forward to linked worktree commit",
    )
    self.assertIn(
        (["go", "build", "-o", "skia-gardener", "."], gard_fx.repo),
        built_cmds,
        "go build must run in synced primary skia_gardener checkout",
    )
    self.assertIn(
        ("skia-gardener.service", gard_fx.repo),
        restarted_units,
        "Synced primary skia_gardener checkout must restart skia-gardener.service",
    )


class TestStopGuardAutoShip(unittest.TestCase):

  PLAN = [{
      "repo": "/home/u/.gemini/config",
      "files": ["hooks/own.py"],
      "own_unpushed": [],
      "turn_touched": True,
      "foreign_unpushed": [],
  }]

  def run_stop(self, steps: list, plan: list, tmp_dir: str) -> dict:
    transcript = os.path.join(tmp_dir, "transcript.jsonl")
    with open(transcript, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")
    data = {
        "conversationId": "c1",
        "transcriptPath": transcript,
        "artifactDirectoryPath": tmp_dir,
        "fullyIdle": True,
        "terminationReason": "NO_TOOL_CALL",
    }
    with (
        patch("sys.stdin", io.StringIO(json.dumps(data))),
        patch("sys.stdout", new_callable=io.StringIO) as mock_out,
        patch.object(stop_guard, "play_stop_sound"),
        patch.object(auto_ship, "plan_ship", return_value=plan),
    ):
      stop_guard.main()
    return json.loads(mock_out.getvalue())

  def verified_turn(self, request: str = "fix the hook") -> list:
    return [
        {"step_index": 1, "type": "USER_INPUT", "content": request},
        _edit_step(2, "/home/u/.gemini/config/hooks/own.py"),
        {
            "step_index": 3,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {"CommandLine": "python3 tests/run_all_tests.py"},
            }],
        },
        {
            "step_index": 4,
            "type": "GENERIC",
            "status": "DONE",
            "content": "The command exited with code 0.\nOK",
        },
    ]

  def test_blocks_stop_once_per_turn_with_ship_command(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      res = self.run_stop(self.verified_turn(), self.PLAN, tmp_dir)
      self.assertEqual(res.get("decision"), "continue")
      reason = res.get("reason", "")
      self.assertIn("[AUTO-SHIP]", reason)
      self.assertIn("auto_ship.py ship c1", reason)
      self.assertIn("hooks/own.py", reason)
      self.assertEqual(self.run_stop(self.verified_turn(), self.PLAN, tmp_dir), {})

  def test_opt_out_phrase_in_request_skips_auto_ship(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = self.verified_turn("поправь хук, но не коммить")
      self.assertEqual(self.run_stop(steps, self.PLAN, tmp_dir), {})

  def test_no_block_when_files_were_not_touched_this_turn(self):
    plan = [{**self.PLAN[0], "turn_touched": False}]
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.assertEqual(self.run_stop(self.verified_turn(), plan, tmp_dir), {})

  def test_no_block_after_successful_ship_in_turn(self):
    steps = self.verified_turn() + [
        {
            "step_index": 5,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {
                    "CommandLine": (
                        "python3 ~/.gemini/config/hooks/auto_ship.py ship c1 -m 'Fix'"
                    )
                },
            }],
        },
        {
            "step_index": 6,
            "type": "GENERIC",
            "status": "DONE",
            "content": (
                "The command exited with code 0.\n"
                "committed and pushed: abc1234 -> origin/main"
            ),
        },
    ]
    with tempfile.TemporaryDirectory() as tmp_dir:
      self.assertEqual(self.run_stop(steps, [], tmp_dir), {})

  def test_second_repo_still_blocks_after_first_repo_shipped_in_turn(self):
    gardener_plan = [{
        "repo": "/home/u/.gemini/jetski/scratch/skia_gardener",
        "files": ["server.go"],
        "own_unpushed": [],
        "turn_touched": True,
        "foreign_unpushed": [],
    }]
    steps = self.verified_turn() + [
        {
            "step_index": 5,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {
                    "CommandLine": (
                        "python3 ~/.gemini/config/hooks/auto_ship.py ship c1 "
                        "--repo ~/.gemini/config -m 'Fix hook'"
                    )
                },
            }],
        },
        {
            "step_index": 6,
            "type": "GENERIC",
            "status": "DONE",
            "content": (
                "The command exited with code 0.\n"
                "committed and pushed: abc1234 -> origin/main"
            ),
        },
        _edit_step(7, "/home/u/.gemini/jetski/scratch/skia_gardener/server.go"),
        {
            "step_index": 8,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{
                "name": "run_command",
                "args": {"CommandLine": "go test ./..."},
            }],
        },
        {
            "step_index": 9,
            "type": "GENERIC",
            "status": "DONE",
            "content": "The command exited with code 0.\nok",
        },
    ]
    with tempfile.TemporaryDirectory() as tmp_dir:
      res = self.run_stop(steps, gardener_plan, tmp_dir)
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("skia_gardener", res.get("reason", ""))

  def test_no_block_when_last_command_failed(self):
    steps = self.verified_turn()[:-1] + [{
        "step_index": 4,
        "type": "GENERIC",
        "status": "DONE",
        "content": (
            "The command exited with code 1.\n"
            "[FOREIGN-COMMIT PUSH GUARD] Refusing auto-push"
        ),
    }]
    with tempfile.TemporaryDirectory() as tmp_dir:
      transcript = os.path.join(tmp_dir, "transcript.jsonl")
      with open(transcript, "w", encoding="utf-8") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")
      turn_info = stop_guard.analyze_current_turn(transcript)
      with patch.object(auto_ship, "plan_ship", return_value=self.PLAN):
        reason, deferred = stop_guard.auto_ship_guard_reason(
            turn_info, {}, "c1", transcript
        )
      self.assertEqual((reason, deferred), ("", False))

  def test_dative_pronoun_mne_kommit_is_not_treated_as_opt_out(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      res = self.run_stop(
          self.verified_turn("поправь хук и покажи мне коммит"),
          self.PLAN,
          tmp_dir,
      )
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[AUTO-SHIP]", res.get("reason", ""))

  def test_code_check_guard_covers_auto_ship_ship_for_tracked_sources(self):
    import pre_tool_guard

    with tempfile.TemporaryDirectory() as tmp_dir:
      transcript = os.path.join(tmp_dir, "transcript.jsonl")
      # Non-tmp path so is_tracked_source_file treats it as source code
      real_src = "/home/u/.gemini/jetski/scratch/skia_gardener/server.go"
      with open(transcript, "w", encoding="utf-8") as f:
        f.write(
            json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "fix"})
            + "\n"
        )
        f.write(json.dumps(_edit_step(2, real_src)) + "\n")
      payload = {
          "conversationId": "c1",
          "transcriptPath": transcript,
          "artifactDirectoryPath": os.path.join(tmp_dir, "brain", "c1"),
          "toolCall": {
              "name": "run_command",
              "args": {
                  "CommandLine": (
                      "python3 ~/.gemini/config/hooks/auto_ship.py ship c1 "
                      "--repo /home/u/.gemini/jetski/scratch/skia_gardener -m 'Fix'"
                  )
              },
          },
      }
      with (
          patch("sys.stdin", io.StringIO(json.dumps(payload))),
          patch("sys.stdout", new_callable=io.StringIO) as mock_out,
      ):
        pre_tool_guard.main()
      out = json.loads(mock_out.getvalue())
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("[CODE-CHECK GUARD]", out.get("reason", ""))

  def test_vcs_baton_guard_defers_personal_repo_lines_to_auto_ship(self):
    info = {
        "roadmap_done_idx": 2,
        "asked_question": False,
        "vcs_baton_lines": [
            "[VCS BATON: PENDING — repo=/home/u/.gemini/config; own_dirty=a.py; action: x]"
        ],
    }
    with patch.dict(os.environ, {"JETSKI_AUTO_SHIP_REPOS": "/home/u/.gemini/config"}):
      self.assertEqual(stop_guard.vcs_baton_guard_reason(info, "c1", ""), "")


if __name__ == "__main__":
  unittest.main()
