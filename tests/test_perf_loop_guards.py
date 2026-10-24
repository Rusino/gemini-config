#!/usr/bin/env python3
"""Tests for the perf-optimization-loop trigger (context_guard) and guards (pre_tool_guard)."""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import context_guard
import pre_tool_guard

AUDIT = os.path.expanduser(
    "~/.gemini/config/skills/perf-optimization-loop/scripts/opt_branch_audit.py")


def _git(repo, *args):
  subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True)


class TestPerfLoopNotice(unittest.TestCase):

  def test_perf_prompts_trigger(self):
    for prompt in ("ускорь layout в WebParagraph", "сделай perf-цикл", "Speed up shaping",
                   "сравни производительность двух версий", "run nanobench on this",
                   "замерь ускорение", "optimize line breaking"):
      self.assertIn("[PERF LOOP TRIGGER]", context_guard.build_perf_loop_notice(prompt), prompt)

  def test_unrelated_and_continuation_prompts_do_not_trigger(self):
    for prompt in ("почини caret на trailing spaces", "UI замерла после resize",
                   "optimistic locking in the cache", "Superformance is not a word",
                   "Continuing unfinished work from previous conversation: speed up layout"):
      self.assertEqual(context_guard.build_perf_loop_notice(prompt), "", prompt)


class _PerfRepo(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.repo = os.path.join(self.tmp.name, "skia")
    os.makedirs(os.path.join(self.repo, "modules", "skparagraph"))
    subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
    _git(self.repo, "config", "user.email", "t@example.com")
    _git(self.repo, "config", "user.name", "Test")
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", "base")
    _git(self.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    self.other = os.path.join(self.tmp.name, "other")
    subprocess.run(["git", "init", "-q", "-b", "main", self.other], check=True)

  def tearDown(self):
    self.tmp.cleanup()


class TestPerfClaimCommitGuard(_PerfRepo):

  def _deny(self, commit_part, repo=None):
    return pre_tool_guard.perf_claim_commit_denial(f"cd {repo or self.repo} && {commit_part}",
                                                   [])

  def test_speedup_subject_without_trailer_is_denied(self):
    self.assertIn("[PERF-CLAIM GUARD]", self._deny('git commit -m "Speed up line breaking"'))
    self.assertIn("[PERF-CLAIM GUARD]", self._deny('git commit -am "Optimizations for wrap"'))

  def test_own_heredoc_forms_are_parsed(self):
    self.assertIn("[PERF-CLAIM GUARD]", self._deny(
        "git commit -q -F - <<'EOF'\nAvoid copies to make layout faster\n\nBody\nEOF"))
    self.assertIn("[PERF-CLAIM GUARD]", self._deny(
        "git commit -m \"$(cat <<'EOF'\nSpeed up layout\n\nBody\nEOF\n)\""))

  def test_foreign_heredoc_is_not_the_commit_message(self):
    cmd = ("cat > notes.md <<'EOF'\nOptimize later\nEOF\n"
           'git commit -am "Fix caret" && gh pr create --body-file - <<\'EOF\'\nfaster\nEOF')
    self.assertEqual(self._deny(cmd), "")
    cmd = "cat > a.cc <<'EOF'\n// x\nEOF\ngit commit -F - <<'EOF'\nSpeed up X\nEOF"
    self.assertIn("[PERF-CLAIM GUARD]", self._deny(cmd))

  def test_trailer_exemption_revert_and_non_perf_allow(self):
    self.assertEqual(self._deny(
        "git commit -F - <<'EOF'\nSpeed up line breaking\n\nPerf: long 14.2 -> 12.1 ms "
        "(-14.8%, 95% CI [-15.1, -14.2], n=60/60, rounds=2) WIN\nEOF"), "")
    self.assertEqual(self._deny(
        'git commit -m "Optimize imports" -m "Perf-Exempt: not a runtime change"'), "")
    self.assertEqual(self._deny('git commit -m \'Revert "Speed up layout"\''), "")
    self.assertEqual(self._deny('git commit -m "Fix caret"'), "")
    self.assertEqual(self._deny('echo "git commit -m Speed up"'), "")

  def test_repos_outside_the_skill_domain_are_not_gated(self):
    self.assertEqual(self._deny('git commit -m "Make context_guard faster"', self.other), "")


class TestPerfAuditUploadGuard(_PerfRepo):

  def _deny(self, cmd=None):
    return pre_tool_guard.perf_audit_before_upload_denial(
        cmd or f"cd {self.repo} && git push origin main", [])

  def _audit(self):
    subprocess.run([sys.executable, AUDIT, "--repo", self.repo, "--no-ledger", "origin/main"],
                   check=True, capture_output=True)

  def test_unaudited_perf_commit_is_denied_until_audit_of_current_head(self):
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", "Speed up layout")
    self.assertIn("[PERF-AUDIT GUARD]", self._deny())
    self._audit()
    self.assertEqual(self._deny(), "")
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", "Fix caret")
    self.assertIn("[PERF-AUDIT GUARD]", self._deny())

  def test_audit_in_the_same_command_and_branch_deletion_allow(self):
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", "Speed up layout")
    self.assertEqual(self._deny(
        f"python3 {AUDIT} --repo {self.repo} origin/main && cd {self.repo} && git push"), "")
    self.assertEqual(self._deny(f"cd {self.repo} && git push origin --delete old"), "")

  def test_exempt_reverted_and_foreign_author_commits_allow(self):
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", "Optimize imports",
         "-m", "Perf-Exempt: tooling only")
    _git(self.repo, "commit", "-q", "--allow-empty", "-m", 'Revert "Speed up layout"')
    _git(self.repo, "commit", "-q", "--author=Up <upstream@example.com>",
         "--allow-empty", "-m", "Speed up upstream thing")
    self.assertEqual(self._deny(), "")


if __name__ == "__main__":
  unittest.main()
