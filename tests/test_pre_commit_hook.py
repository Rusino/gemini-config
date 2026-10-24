#!/usr/bin/env python3
"""Tests for githooks/pre-commit"""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parent.parent / "githooks"

# Stands in for the real suite: fails when BROKEN is part of the snapshot and
# with a distinct code when repo-local git variables leak into the test run
STUB_RUNNER = """import os, sys
if "GIT_INDEX_FILE" in os.environ:
  sys.exit(2)
root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.exit(1 if os.path.exists(os.path.join(root, "BROKEN")) else 0)
"""


class PreCommitHookTest(unittest.TestCase):

  def setUp(self):
    self.repo = tempfile.mkdtemp(prefix="pre-commit-hook-test-")
    self.addCleanup(shutil.rmtree, self.repo)
    # Keeps the user's git config and any outer hook environment out of the
    # throwaway repository
    self.env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")
    self.git("init", "-q")
    self.git("config", "user.name", "Test")
    self.git("config", "user.email", "test@example.com")
    self.git("config", "commit.gpgsign", "false")
    self.git("config", "core.hooksPath", str(HOOKS_DIR))
    Path(self.repo, "tests").mkdir()
    Path(self.repo, "tests", "run_all_tests.py").write_text(STUB_RUNNER)
    self.git("add", "tests/run_all_tests.py")

  def git(self, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=self.repo, env=self.env,
        capture_output=True, text=True, check=check)

  def test_failing_snapshot_aborts_commit(self):
    Path(self.repo, "BROKEN").touch()
    self.git("add", "BROKEN")
    result = self.git("commit", "-q", "-m", "broken", check=False)
    self.assertNotEqual(result.returncode, 0, result.stderr)
    self.assertIn("commit aborted", result.stderr)
    head = self.git("rev-parse", "--verify", "-q", "HEAD", check=False)
    self.assertNotEqual(head.returncode, 0)

  def test_unstaged_breakage_does_not_block_commit(self):
    Path(self.repo, "BROKEN").touch()
    result = self.git("commit", "-q", "-m", "clean", check=False)
    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertIn("passed on the staged snapshot", result.stderr)


if __name__ == "__main__":
  unittest.main()
