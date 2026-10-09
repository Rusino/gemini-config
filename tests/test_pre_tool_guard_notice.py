#!/usr/bin/env python3
"""Tests for pre_tool_guard_notice.py"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
sys.path.insert(0, HOOKS_DIR)

import pre_tool_guard
import pre_tool_guard_notice


class TestPreToolGuardNotice(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, self.tmp)
    self.marker = os.path.join(self.tmp, "marker.json")
    env = patch.dict(os.environ, {
        "JETSKI_PRE_TOOL_GUARD_MARKER": self.marker,
        "JETSKI_ROADMAPS_DIR": os.path.join(self.tmp, "roadmaps"),
        "ANTIGRAVITY_CONVERSATION_ID": "c1",
    })
    env.start()
    self.addCleanup(env.stop)
    self.chat = {"conversationId": "c1", "artifactDirectoryPath": os.path.join(self.tmp, "brain")}

  def write_marker(self, ts):
    with open(self.marker, "w") as f:
      json.dump({"ts": ts, "conv": "c1", "error": "OSError: boom", "where": "x.py:1 in f"}, f)

  def notices(self, out):
    return [step["ephemeralMessage"] for step in out.get("injectSteps", [])]

  def run_hook(self, path, payload):
    return subprocess.run(
        [path], input=json.dumps(payload), capture_output=True, text=True,
        timeout=10, cwd=os.path.dirname(os.path.dirname(path)))

  def test_new_failures_are_noticed_once_per_interval(self):
    notice = pre_tool_guard_notice.notice
    later = 1002 + pre_tool_guard_notice.NOTICE_INTERVAL_S
    self.write_marker(999)
    self.assertEqual(notice(self.chat, 1000), {})
    self.assertEqual(notice(self.chat, 1001), {})
    self.write_marker(1001.5)
    [msg] = self.notices(notice(self.chat, 1002))
    self.assertIn("OSError: boom (x.py:1 in f)", msg)
    self.assertIn("in this chat", msg)
    self.write_marker(1003)
    self.assertEqual(notice(self.chat, 1004), {})
    self.write_marker(later + 1)
    self.assertEqual(len(self.notices(notice(self.chat, later + 2))), 1)

  def test_subagents_and_battle_mode_are_not_noticed(self):
    for extra in ({"parentConversationId": "p1"}, {"isBattleMode": True}):
      with self.subTest(extra=extra):
        chat = {**self.chat, **extra}
        pre_tool_guard_notice.notice(chat, 1000)
        self.write_marker(1001)
        self.assertEqual(pre_tool_guard_notice.notice(chat, 1002), {})

  def test_guard_failure_reaches_the_next_model_call(self):
    # A copy keeps the injected fault away from the live hooks; both hooks run
    # by path, the way hooks.json runs them
    hooks = shutil.copytree(
        HOOKS_DIR, os.path.join(self.tmp, "hooks"), ignore=shutil.ignore_patterns("__pycache__"))
    with open(os.path.join(hooks, "roadmap.py"), "a") as f:
      f.write("\n\ndef roadmaps_root():\n  raise OSError('injected')\n")
    notice = os.path.join(hooks, "pre_tool_guard_notice.py")
    first = self.run_hook(notice, self.chat)
    self.assertEqual((first.returncode, first.stderr, json.loads(first.stdout)), (0, "", {}))
    guard = self.run_hook(os.path.join(hooks, "pre_tool_guard.py"), {
        **self.chat, "toolCall": {"name": "write_to_file", "args": {"TargetFile": "/tmp/x"}}})
    self.assertEqual((guard.returncode, json.loads(guard.stdout)), (0, {"decision": "allow"}))
    self.assertIn("OSError: injected", guard.stderr)
    second = self.run_hook(notice, self.chat)
    self.assertEqual((second.returncode, second.stderr), (0, ""))
    [msg] = self.notices(json.loads(second.stdout))
    self.assertIn("OSError: injected (roadmap.py:", msg)
    self.assertIn("in this chat", msg)

  def test_hooks_json_runs_the_notice_before_every_model_call(self):
    with open(os.path.join(HOOKS_DIR, "..", "hooks.json")) as f:
      specs = json.load(f).values()
    commands = [
        h["command"] for s in specs if s.get("enabled", True) for h in s.get("PreInvocation", [])]
    self.assertIn("~/.gemini/config/hooks/pre_tool_guard_notice.py", commands)
    self.assertTrue(os.access(os.path.join(HOOKS_DIR, "pre_tool_guard_notice.py"), os.X_OK))

  def test_marker_path_matches_the_guard(self):
    self.assertEqual(
        pre_tool_guard_notice.FAILURE_MARKER_DEFAULT, pre_tool_guard.FAILURE_MARKER_DEFAULT)


if __name__ == "__main__":
  unittest.main()
