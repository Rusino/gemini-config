#!/usr/bin/env python3
"""Tests for auto_ship.py and the stop_guard auto-ship trigger."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
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
    # Non-Darwin is always a no-op
    res3 = auto_ship.maybe_sync_mac_clone(
        repo=mac_clone, platform="linux", min_interval_s=0
    )
    self.assertEqual(res3["status"], "skip_platform")

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
