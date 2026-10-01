#!/usr/bin/env python3
"""Tests for pre_tool_guard.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import pre_tool_guard


class TestPreToolGuard(unittest.TestCase):

  def test_is_tracked_source_file(self):
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/main.py"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/source.cc"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/header.h"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/BUILD.bazel"))
    self.assertTrue(pre_tool_guard.is_tracked_source_file("/workspace/project/CMakeLists.txt"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/workspace/project/README.md"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/workspace/project/notes.txt"))
    self.assertFalse(pre_tool_guard.is_tracked_source_file("/tmp/scratch.py"))
    self.assertFalse(
        pre_tool_guard.is_tracked_source_file(
            os.path.expanduser("~/.gemini/config/hooks/chat_lifecycle.py")
        )
    )

  def test_read_before_edit_guard_unseen_file(self):
    fake_project_dir = "/workspace/dummy_project"
    fake_src = os.path.join(fake_project_dir, "feature.py")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      # Empty transcript: file was never read
      f_trans.write("")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "replace_file_content",
              "args": {"TargetFile": fake_src},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "deny")
        self.assertIn("READ-BEFORE-EDIT", out.get("reason", ""))

  def test_read_before_edit_guard_seen_file(self):
    fake_project_dir = "/workspace/dummy_project"
    fake_src = os.path.join(fake_project_dir, "feature.py")
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans:
      view_step = {
          "step_index": 1,
          "type": "PLANNER_RESPONSE",
          "tool_calls": [
              {"name": "view_file", "args": {"AbsolutePath": fake_src}}
          ],
      }
      f_trans.write(json.dumps(view_step) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "toolCall": {
              "name": "replace_file_content",
              "args": {"TargetFile": fake_src},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("os.path.exists", side_effect=lambda p: p in (fake_src, f_trans.name)):
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "allow")

  def test_agentapi_subcommand_guard(self):
    bad_commands = [
        "agentapi start 'hello'",
        "agentapi start-conversation 'hello'",
        "agentapi create 'hello'",
        "agentapi conversation 'hello'",
    ]
    for cmd in bad_commands:
      with self.subTest(cmd=cmd):
        data = {
            "conversationId": "c1",
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": cmd},
            },
        }
        with patch("sys.stdin", io.StringIO(json.dumps(data))), \
             patch("sys.stdout", new_callable=io.StringIO) as mock_out:
          pre_tool_guard.main()
          out = json.loads(mock_out.getvalue())
          self.assertEqual(out.get("decision"), "deny")
          self.assertIn("does not exist", out.get("reason", ""))

  def test_agentapi_multiple_new_conversation_guard(self):
    cmd = 'OUT=$(agentapi new-conversation "prompt") && agentapi new-conversation "prompt2"'
    data = {
        "conversationId": "c1",
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": cmd},
        },
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out:
      pre_tool_guard.main()
      out = json.loads(mock_out.getvalue())
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn("multiple times", out.get("reason", ""))

  def test_agentapi_auto_rewrite(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      summary_file = os.path.join(tmp_dir, "handoff_summary_123.md")
      with open(summary_file, "w") as f:
        f.write("# Summary\n")

      cmd = f'agentapi new-conversation "Read {summary_file}"'
      data = {
          "conversationId": "c1",
          "artifactDirectoryPath": tmp_dir,
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": cmd},
          },
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        pre_tool_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out.get("decision"), "allow")
        self.assertIn("overwrite", out)
        rewritten = out["overwrite"]["CommandLine"]
        self.assertIn("env -u ANTIGRAVITY_SOURCE_METADATA", rewritten)
        self.assertIn("--title=", rewritten)
        self.assertIn("--model=pro", rewritten)


if __name__ == "__main__":
  unittest.main()
