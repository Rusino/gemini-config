#!/usr/bin/env python3
"""Tests for stop_guard.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import stop_guard


class TestStopGuard(unittest.TestCase):

  def test_is_inspection_command(self):
    inspection_commands = [
        "git status -s",
        "git diff",
        "git log -n 5",
        "grep -n 'foo' file.txt",
        "rg 'foo'",
        "cat file.txt",
        "ls -la",
        "find . -name '*.py'",
        "git diff && git status",
        "cat file.txt | grep 'foo'",
    ]
    for cmd in inspection_commands:
      with self.subTest(cmd=cmd):
        self.assertTrue(stop_guard.is_inspection_command(cmd))

    action_commands = [
      "pytest",
      "python3 -m unittest",
      "cargo test",
      "bazel test //...",
      "ninja -C out/Default",
      "gcc main.c",
      "python3 script.py",
  ]
    for cmd in action_commands:
      with self.subTest(cmd=cmd):
        self.assertFalse(stop_guard.is_inspection_command(cmd))

  def test_unverified_code_edit_blocks_stop(self):
    fake_src = "/workspace/project/feature.py"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Please implement feature"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "replace_file_content", "args": {"TargetFile": fake_src}}
              ],
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "continue")
        self.assertIn("VERIFICATION GUARD", out.get("reason", ""))

  def test_verified_code_edit_allows_stop(self):
    fake_src = "/workspace/project/feature.py"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Please implement feature"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "replace_file_content", "args": {"TargetFile": fake_src}}
              ],
          },
          {
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "run_command", "args": {"CommandLine": "python3 -m unittest"}}
              ],
          },
          {
              "step_index": 4,
              "type": "GENERIC",
              "content": "The command exited with code 0.\nRan 10 tests.\nOK",
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out, {})

  def test_failing_command_after_edit_blocks_stop(self):
    fake_src = "/workspace/project/feature.py"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Please implement feature"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "replace_file_content", "args": {"TargetFile": fake_src}}
              ],
          },
          {
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "run_command", "args": {"CommandLine": "python3 -m unittest"}}
              ],
          },
          {
              "step_index": 4,
              "type": "GENERIC",
              "content": "The command exited with code 1.\nFAILED (failures=1)",
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "continue")
        self.assertIn("failed with exit code 1", out.get("reason", ""))

  def test_inspection_command_does_not_count_as_verification(self):
    fake_src = "/workspace/project/feature.py"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Please implement feature"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "replace_file_content", "args": {"TargetFile": fake_src}}
              ],
          },
          {
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "run_command", "args": {"CommandLine": "git diff"}}
              ],
          },
          {
              "step_index": 4,
              "type": "GENERIC",
              "content": "The command exited with code 0.\ndiff --git a/feature.py ...",
          },
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        out = json.loads(mock_out.getvalue())
        # git diff is inspection command, so edit is still considered unverified!
        self.assertEqual(out.get("decision"), "continue")
        self.assertIn("VERIFICATION GUARD", out.get("reason", ""))


if __name__ == "__main__":
  unittest.main()
