#!/usr/bin/env python3
"""Tests for the CHANGE-ID LEASE guard and the stack-aware WORKTREE LEASE advice."""

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import context_guard  # pylint: disable=g-import-not-at-top
import pre_tool_guard  # pylint: disable=g-import-not-at-top

CHANGE_ID = "I" + "b" * 40
OTHER_ID = "I" + "c" * 40
TITLES = {"c1": "[10:00] ⦿ SkoreText stack", "c2": "[11:00] ⦿ Perf side task",
          "c3": "[12:00] ⦿ SkoreText continuation"}


class ChangeIdLeaseGuardTest(unittest.TestCase):

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    tmp = self._tmp.name
    self.roadmaps = os.path.join(tmp, "roadmaps")
    os.makedirs(os.path.join(self.roadmaps, "proj-1"))
    for p in (
        # The user's global git config (hooks, signing) must not leak into test repos
        patch.dict(os.environ, {"JETSKI_ROADMAPS_DIR": self.roadmaps,
                                "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}),
        patch.object(context_guard, "get_conversation_db_info",
                     side_effect=lambda cid, _h="": (TITLES.get(cid, ""), "proj-1")),
        patch.object(context_guard, "_is_archived_in_pbtxt", return_value=False),
    ):
      p.start()
      self.addCleanup(p.stop)
    self.repo = os.path.join(tmp, "skia")
    self.wt2 = os.path.join(tmp, "skia_wt2")
    subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
    self.git(self.repo, "config", "user.email", "t@example.com")
    self.git(self.repo, "config", "user.name", "Test")
    self.write(self.repo, "a.cpp", "int a = 1;\n")
    self.git(self.repo, "commit", "-q", "-m", "base")
    self.git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    self.git(self.repo, "checkout", "-q", "-b", "stack")
    self.write(self.repo, "a.cpp", "int a = 2;\n")
    self.git(self.repo, "commit", "-q", "-m", f"[SkoreText] Part 1\n\nChange-Id: {CHANGE_ID}\n")
    self.stack_sha = self.git(self.repo, "rev-parse", "HEAD")
    self.git(self.repo, "worktree", "add", "-q", "-b", "side", self.wt2, "HEAD")
    self.common = os.path.join(self.repo, ".git")

  def tearDown(self):
    self._tmp.cleanup()

  def git(self, cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True).stdout.strip()

  def write(self, cwd, rel, text):
    with open(os.path.join(cwd, rel), "w", encoding="utf-8") as f:
      f.write(text)
    self.git(cwd, "add", rel)

  def art(self, cid):
    return os.path.join(self._tmp.name, "brain", cid)

  def denial(self, cmd, conv, parent=""):
    return pre_tool_guard.change_id_lease_denial(cmd, conv, parent, "", self.art(conv),
                                                 [self.repo])

  def leases(self):
    with open(os.path.join(self.common, pre_tool_guard.CHANGE_LEASE_FILE)) as f:
      return json.load(f)["changes"]

  def set_lease_ts(self, ts, conv="c1"):
    data = {"changes": {CHANGE_ID: {"conv_id": conv, "title": TITLES[conv], "ts": ts,
                                    "worktree": self.repo, "branch": "stack"}}}
    with open(os.path.join(self.common, pre_tool_guard.CHANGE_LEASE_FILE), "w") as f:
      json.dump(data, f)

  def journal(self, *records):
    path = os.path.join(self.roadmaps, "proj-1", "journal.jsonl")
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 1))
    with open(path, "a", encoding="utf-8") as f:
      for rec in records:
        f.write(json.dumps({"ts": stamp, **rec}) + "\n")

  # The owner leases its stack on amend; another live chat in a sibling worktree of the same
  # repo is then denied every stack-rewriting command, with a patch-file hand-off path
  def test_active_owner_blocks_second_chat_in_other_worktree(self):
    self.assertEqual(self.denial(f"git -C {self.repo} commit --amend --no-edit", "c1"), "")
    lease = self.leases()[CHANGE_ID]
    self.assertEqual((lease["conv_id"], lease["branch"]), ("c1", "stack"))
    self.git(self.wt2, "checkout", "-q", "-B", "side", self.stack_sha)
    for cmd in (f"git -C {self.wt2} commit --amend --no-edit",
                f"git -C {self.wt2} rebase -i origin/main",
                f"git -C {self.wt2} cl upload",
                f"git -C {self.wt2} push origin HEAD:refs/for/main",
                f"git -C {self.wt2} commit -m Fix -m 'Change-Id: {CHANGE_ID}'"):
      with self.subTest(cmd=cmd):
        reason = self.denial(cmd, "c2")
        self.assertIn(f"[CHANGE-ID LEASE GUARD] CL `{CHANGE_ID}` is owned by "
                      f"`{TITLES['c1']}` (conversation://c1, active 1m ago, branch `stack`", reason)
        self.assertIn(f"git diff > {self.art('c2')}/scratch/<name>.patch", reason)
        self.assertIn("the owner sets its roadmap item todo/done", reason)
    self.git(self.wt2, "checkout", "-q", "-B", "side", "refs/remotes/origin/main")
    self.assertIn("CHANGE-ID LEASE GUARD",
                  self.denial(f"git -C {self.wt2} cherry-pick {self.stack_sha}", "c2"))
    # Unrelated work stays free: a new commit with its own Change-Id
    self.assertEqual(self.denial(f"git -C {self.wt2} commit -m x -m 'Change-Id: {OTHER_ID}'",
                                 "c2"), "")

  # Amending or rebasing one's own CL on top of a foreign stack rewrites only that CL
  def test_own_top_cl_on_foreign_stack_is_free(self):
    self.set_lease_ts(time.time())
    self.git(self.wt2, "checkout", "-q", "-B", "side", self.stack_sha)
    self.write(self.wt2, "b.cpp", "int b;\n")
    self.git(self.wt2, "commit", "-q", "-m", f"Top\n\nChange-Id: {OTHER_ID}\n")
    self.assertEqual(self.denial(f"git -C {self.wt2} commit --amend --no-edit", "c2"), "")
    self.assertEqual(
        self.denial(f"git -C {self.wt2} rebase --onto {self.stack_sha} {self.stack_sha}", "c2"),
        "")
    self.assertIn("CHANGE-ID LEASE GUARD",
                  self.denial(f"git -C {self.wt2} rebase -i origin/main", "c2"))

  # Each invocation is checked in its own repo: an inspection of the leased repo in the same
  # line neither denies nor claims, and a cd before the command picks the repo
  def test_lease_uses_repo_of_each_invocation(self):
    self.set_lease_ts(time.time())
    other = os.path.join(self._tmp.name, "other")
    subprocess.run(["git", "init", "-q", other], check=True)
    self.assertEqual(
        self.denial(f"git -C {other} commit --amend --no-edit && git -C {self.repo} log -1",
                    "c2"), "")
    self.assertEqual(self.leases()[CHANGE_ID]["conv_id"], "c1")
    self.assertIn("CHANGE-ID LEASE GUARD",
                  self.denial(f"cd {self.repo} && git commit --amend --no-edit", "c2"))

  # A no-commit cherry-pick for a local A/B is checked but never takes over a stale lease, and
  # option values such as -m 1 are not read as revisions
  def test_no_commit_cherry_pick_does_not_claim(self):
    self.set_lease_ts(time.time() - 2 * pre_tool_guard.WORKTREE_LEASE_TTL_S)
    cmd = f"git -C {self.wt2} cherry-pick -n -m 1 {self.stack_sha}"
    self.assertEqual(self.denial(cmd, "c2"), "")
    self.assertEqual(self.leases()[CHANGE_ID]["conv_id"], "c1")

  # Subagents act for their parent, so they never collide with the parent's own leases
  def test_subagent_counts_as_parent(self):
    self.set_lease_ts(time.time())
    self.assertEqual(self.denial(f"git -C {self.repo} commit --amend --no-edit", "s1", "c1"), "")
    self.assertEqual(self.leases()[CHANGE_ID]["conv_id"], "c1")

  # A lease whose owner went quiet past the TTL is taken over without asking
  def test_stale_owner_is_taken_over(self):
    self.set_lease_ts(time.time() - 2 * pre_tool_guard.WORKTREE_LEASE_TTL_S)
    self.assertEqual(self.denial(f"git -C {self.repo} commit --amend --no-edit", "c2"), "")
    self.assertEqual(self.leases()[CHANGE_ID]["conv_id"], "c2")

  # The user-confirmed parallel-chat override lifts the check, as for worktree leases
  def test_override_file_lifts_check(self):
    self.set_lease_ts(time.time())
    override, _ = pre_tool_guard._parallel_guard_paths(self.art("c2"), "c2")
    os.makedirs(os.path.dirname(override))
    with open(override, "w") as f:
      f.write("{}")
    self.assertEqual(self.denial(f"git -C {self.repo} commit --amend --no-edit", "c2"), "")

  # roadmap.py hands a stack over: set todo/done releases it, state-move passes it on
  def test_roadmap_release_and_state_move(self):
    cmd = f"git -C {self.repo} commit --amend --no-edit"
    self.set_lease_ts(time.time())
    self.journal({"conv": "c1", "op": "set", "id": "R1", "status": "done"})
    self.assertEqual(self.denial(cmd, "c2"), "")
    self.set_lease_ts(time.time())
    self.journal({"conv": "c1", "op": "state-move", "to": "c3", "items": ["R1"]})
    self.assertIn("conversation://c3", self.denial(cmd, "c2"))
    self.assertEqual(self.denial(cmd, "c3"), "")

  # A worktree whose branch carries the owner's stack must not get "make a new worktree" advice
  def test_worktree_lease_advice_names_owned_stack(self):
    now = time.time()
    git_dir = os.path.join(self.repo, ".git")
    with open(os.path.join(git_dir, "jetski_lease.json"), "w") as f:
      json.dump({"conv_id": "c1", "title": TITLES["c1"], "ts": now, "worktree": self.repo}, f)
    override, state = pre_tool_guard._parallel_guard_paths(self.art("c2"), "c2")

    def advice():
      return pre_tool_guard._check_and_claim_worktree_lease(
          git_dir, self.repo, "c2", TITLES["c2"], override, state, "", now)

    self.set_lease_ts(now)
    reason = advice()
    self.assertIn("carries a CL stack owned by that chat", reason)
    self.assertNotIn("worktree add", reason)
    os.remove(os.path.join(self.common, pre_tool_guard.CHANGE_LEASE_FILE))
    self.assertIn("worktree add", advice())

  # A roadmap doing item keeps an owner's lease alive past the TTL (WORKTREE_DOING_LEASE_TTL_S)
  def test_doing_item_extends_lease(self):
    now = time.time()
    old = now - 2 * pre_tool_guard.WORKTREE_LEASE_TTL_S
    self.assertFalse(
        pre_tool_guard._is_worktree_lease_unexpired("c1", old, self.repo, "", now)[0])
    with open(os.path.join(self.roadmaps, "proj-1", "roadmap.json"), "w") as f:
      json.dump({"items": [{"id": "R1", "status": "doing", "owner": "c1"}]}, f)
    self.assertTrue(
        pre_tool_guard._is_worktree_lease_unexpired("c1", old, self.repo, "", now)[0])

  # The hook entry point runs the guard for run_command
  def test_main_denies_foreign_stack_command(self):
    self.set_lease_ts(time.time())
    payload = {"conversationId": "c2", "artifactDirectoryPath": self.art("c2"),
               "workspacePaths": [self.wt2],
               "toolCall": {"name": "run_command",
                            "args": {"CommandLine": f"git -C {self.repo} rebase origin/main"}}}
    with patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         patch("sys.stdout", new_callable=io.StringIO) as out:
      pre_tool_guard.main()
    result = json.loads(out.getvalue())
    self.assertEqual(result["decision"], "deny", result)
    self.assertIn("[CHANGE-ID LEASE GUARD]", result["reason"])


if __name__ == "__main__":
  unittest.main()
