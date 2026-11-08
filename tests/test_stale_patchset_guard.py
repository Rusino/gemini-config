#!/usr/bin/env python3
"""Tests for the STALE-PATCHSET upload guard in pre_tool_guard.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import pre_tool_guard  # pylint: disable=g-import-not-at-top

CHANGE_ID = "I" + "a" * 40


class StalePatchsetGuardTest(unittest.TestCase):

  def setUp(self):
    self._tmp = tempfile.TemporaryDirectory()
    self.repo = os.path.join(self._tmp.name, "skia")
    subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
    self.git("config", "user.email", "t@example.com")
    self.git("config", "user.name", "Test")
    self.git("config", "remote.origin.url", "https://skia.googlesource.com/skia")
    self.write("src/Shared.cpp", "int shared = 1;\n")
    self.write("BUILD.gn", "sources = []\n")
    self.git("commit", "-q", "-m", "Upstream base")
    self.git("update-ref", "refs/remotes/origin/main", "HEAD")
    self.git("checkout", "-q", "-b", "stack")
    msg = f"[SkoreText] Add CoreTextIndexMap\n\nChange-Id: {CHANGE_ID}\n"
    self.write("src/CoreTextIndexMap.cpp", "#if defined(SK_X)\nint map = 1;\n#endif\n")
    self.git("commit", "-q", "-m", msg)
    self.ps13 = self.git("rev-parse", "HEAD")
    # PS14 is the reviewer-requested build-level selection on top of PS13
    self.write("src/CoreTextIndexMap.cpp", "int map = 1;\n")
    self.write("BUILD.gn", "sources = []\nif (skia_use_coretext) {\n  sources += [ \"map\" ]\n}\n")
    self.git("commit", "-q", "--amend", "-m", msg)
    self.ps14 = self.git("rev-parse", "HEAD")
    self.cmd = f"git -C {self.repo} cl upload"

  def tearDown(self):
    self._tmp.cleanup()

  def git(self, *args):
    return subprocess.run(
        ["git", "-C", self.repo, *args], check=True, capture_output=True, text=True
    ).stdout.strip()

  def write(self, rel, text):
    path = os.path.join(self.repo, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
      f.write(text)
    self.git("add", rel)

  def amend_from(self, ps_sha, rel, text):
    self.git("checkout", "-q", "-B", "stack", ps_sha)
    self.write(rel, text)
    self.git("commit", "-q", "--amend", "--no-edit")
    return self.git("rev-parse", "HEAD")

  def gerrit_change(self, current=None):
    return [{
        "_number": 1406897,
        "project": "skia",
        "status": "NEW",
        "current_revision": current or self.ps14,
        "revisions": {
            self.ps13: {"_number": 13, "ref": "refs/changes/97/1406897/13"},
            (current or self.ps14): {"_number": 14, "ref": "refs/changes/97/1406897/14"},
        },
    }]

  def denial(self, cmd=None, response=None, error=None):
    kwargs = {"side_effect": error} if error else {"return_value": response}
    with patch.object(pre_tool_guard, "_gerrit_get_json", **kwargs) as get:
      out = pre_tool_guard.stale_patchset_upload_denial(cmd or self.cmd, [self.repo])
    return out, get

  # The Skia CL 1406897 incident: PS15 built on PS13 drops PS14's build-level selection
  def test_blocks_upload_built_on_previous_patchset(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    reason, get = self.denial(response=self.gerrit_change())
    self.assertIn("[STALE-PATCHSET GUARD]", reason)
    self.assertIn("is built on PS13 but Gerrit has PS14", reason)
    self.assertIn("https://skia-review.googlesource.com/c/skia/+/1406897/14", reason)
    self.assertIn("`BUILD.gn`", reason)
    self.assertIn("Rebase the local commit onto the latest patchset", reason)
    self.assertIn("never treat the latest Gerrit patchset as 'old'", reason)
    url = get.call_args[0][0]
    self.assertTrue(url.startswith("https://skia-review.googlesource.com/changes/?q="), url)
    self.assertIn(CHANGE_ID, url)

  # A new patchset built on the latest one keeps its changes, so the upload goes through
  def test_allows_upload_built_on_latest_patchset(self):
    self.amend_from(self.ps14, "src/Shared.cpp", "int shared = 2;\n")
    self.assertEqual(self.denial(response=self.gerrit_change())[0], "")

  # Amending lines the latest PS added (a reviewer follow-up) is a rewrite of it, not a drop
  def test_allows_amend_that_rewrites_latest_patchset_lines(self):
    build_gn = "sources = []\nif (skia_use_coretext_paragraph) {\n  sources += [ \"map\" ]\n}\n"
    self.amend_from(self.ps14, "BUILD.gn", build_gn)
    self.assertEqual(self.denial(response=self.gerrit_change())[0], "")

  # Re-uploading the latest PS itself, or the same tree, is a no-op for Gerrit content
  def test_allows_reupload_of_latest_patchset(self):
    self.assertEqual(self.denial(response=self.gerrit_change())[0], "")

  # An unknown latest sha cannot be compared, so the agent must fetch it first
  def test_blocks_when_latest_patchset_is_not_local(self):
    self.amend_from(self.ps14, "src/Shared.cpp", "int shared = 2;\n")
    reason, _ = self.denial(response=self.gerrit_change(current="f" * 40))
    self.assertIn("not in the local repo", reason)
    self.assertIn("git fetch origin refs/changes/97/1406897/14", reason)

  # Gerrit failures leave a stderr trace on uploads but never block them
  def test_network_failure_fails_open(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    with patch("sys.stderr", new_callable=io.StringIO) as err:
      reason, _ = self.denial(error=urllib.error.URLError("timed out"))
    self.assertEqual(reason, "")
    self.assertIn("[STALE-PATCHSET GUARD] Gerrit query failed", err.getvalue())

  # A local merge-tree failure is reported on stderr instead of passing silently
  def test_merge_tree_failure_is_reported(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    with patch.object(pre_tool_guard, "_paths_missing_from", return_value=None), \
         patch("sys.stderr", new_callable=io.StringIO) as err:
      reason, _ = self.denial(response=self.gerrit_change())
    self.assertEqual(reason, "")
    self.assertIn("merge-tree failed", err.getvalue())

  # A Gerrit-side rebase moves only upstream files; they are not the CL's changes
  def test_gerrit_rebase_does_not_report_upstream_files(self):
    self.git("checkout", "-q", "-B", "up", "refs/remotes/origin/main")
    self.write("src/Upstream.cpp", "int up = 1;\n")
    self.git("commit", "-q", "-m", "Upstream move")
    self.git("update-ref", "refs/remotes/origin/main", "HEAD")
    self.git("cherry-pick", self.ps13)
    rebased = self.git("rev-parse", "HEAD")
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    change = self.gerrit_change(current=rebased)
    self.assertEqual(self.denial(response=change)[0], "")

  # A stalled Gerrit never keeps the hook alive: the call returns within the budget and
  # leaves no non-daemon thread for interpreter exit to join
  def test_stalled_query_is_bounded(self):
    with patch.object(pre_tool_guard, "_gerrit_get_json", side_effect=lambda u: time.sleep(5)):
      start = time.time()
      results, errors = pre_tool_guard._query_gerrit_changes("h", "p", [CHANGE_ID])
    self.assertLess(time.time() - start, pre_tool_guard.STALE_PS_BUDGET_S + 0.5)
    self.assertEqual(results, {})
    self.assertEqual(len(errors), 1)
    live = [t for t in threading.enumerate()
            if t is not threading.main_thread() and not t.daemon and t.is_alive()]
    self.assertEqual(live, [])

  # Commit plus upload in one command queries Gerrit once: the upload guard covers it
  def test_commit_and_upload_skip_the_warning(self):
    self.amend_from(self.ps14, "src/Shared.cpp", "int shared = 2;\n")
    cmd = f"git -C {self.repo} commit --amend --no-edit && git -C {self.repo} cl upload"
    text, get = self.warning(cmd, self.gerrit_change())
    self.assertEqual(text, "")
    get.assert_not_called()

  # The documented override and non-upload commands skip the Gerrit query entirely
  def test_override_and_non_upload_skip_query(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    for cmd in (f"ALLOW_PATCHSET_OVERWRITE=1 {self.cmd}", f"git -C {self.repo} log -1"):
      with self.subTest(cmd=cmd):
        reason, get = self.denial(cmd=cmd, response=self.gerrit_change())
        self.assertEqual(reason, "")
        get.assert_not_called()

  def warning(self, cmd, response=None, error=None):
    kwargs = {"side_effect": error} if error else {"return_value": response}
    with patch.object(pre_tool_guard, "_gerrit_get_json", **kwargs) as get:
      out = pre_tool_guard.stale_patchset_warning(cmd, [self.repo])
    return out, get

  # Committing on top of PS13 while Gerrit has PS14 warns, naming both patchsets
  def test_commit_on_stale_stack_warns(self):
    stale = self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    text, _ = self.warning(f"cd {self.repo} && git commit -am wip", self.gerrit_change())
    self.assertIn("[STALE-PATCHSET WARNING]", text)
    self.assertIn(f"local commit {stale[:10]} of CL 1406897 is built on PS13 but Gerrit has "
                  "PS14 (https://skia-review.googlesource.com/c/skia/+/1406897/14)", text)
    self.assertIn("Rebase onto the latest patchset before working", text)
    self.assertIn("never treat the latest Gerrit patchset as old", text)
    self.amend_from(self.ps14, "src/Shared.cpp", "int shared = 2;\n")
    self.assertEqual(self.warning(f"git -C {self.repo} commit --amend", self.gerrit_change())[0],
                     "")

  # A worktree checks out an explicit commit-ish, which is the stack that gets compared
  def test_worktree_add_checks_the_commitish(self):
    wt = os.path.join(self._tmp.name, "wt")
    cmd = f"git -C {self.repo} worktree add -b fix {wt} {self.ps13}"
    text, _ = self.warning(cmd, self.gerrit_change())
    self.assertIn(f"local commit {self.ps13[:10]} of CL 1406897 is built on PS13", text)
    cmd = f"git -C {self.repo} worktree add --detach {wt} {self.ps14}"
    self.assertEqual(self.warning(cmd, self.gerrit_change())[0], "")

  # Warnings fail open silently, and repos without a googlesource remote never query Gerrit
  def test_warning_quiet_on_errors_and_non_gerrit_repos(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    cmd = f"git -C {self.repo} commit -m x"
    self.assertEqual(self.warning(cmd, error=urllib.error.URLError("down"))[0], "")
    self.git("config", "remote.origin.url", "https://github.com/google/skia")
    text, get = self.warning(cmd, self.gerrit_change())
    self.assertEqual(text, "")
    get.assert_not_called()

  # The hook allows the command and prepends a stderr echo, so the agent sees the warning
  def test_main_prefixes_command_with_warning(self):
    self.amend_from(self.ps13, "src/Shared.cpp", "int shared = 2;\n")
    wt = os.path.join(self._tmp.name, "wt2")
    cmd = f"git -C {self.repo} worktree add {wt} HEAD"
    payload = {"conversationId": "c-stale", "workspacePaths": [self.repo],
               "toolCall": {"name": "run_command", "args": {"CommandLine": cmd}}}
    with patch.object(pre_tool_guard, "_gerrit_get_json", return_value=self.gerrit_change()), \
         patch("sys.stdin", io.StringIO(json.dumps(payload))), \
         patch("sys.stdout", new_callable=io.StringIO) as out:
      pre_tool_guard.main()
    result = json.loads(out.getvalue())
    self.assertEqual(result["decision"], "allow", result)
    rewritten = result["overwrite"]["CommandLine"]
    self.assertTrue(rewritten.startswith("printf '%s\\n' '[STALE-PATCHSET WARNING]"), rewritten)
    self.assertTrue(rewritten.endswith(f">&2; {cmd}"), rewritten)


if __name__ == "__main__":
  unittest.main()
