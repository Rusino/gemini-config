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

  def run_pending_stop(self, tmp_dir: str, state: dict) -> dict:
    transcript = os.path.join(tmp_dir, "transcript.jsonl")
    with open(transcript, "w") as f:
      f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "hi"}) + "\n")
    stop_guard.save_state(os.path.join(tmp_dir, "scratch", ".context_guard_state.json"), state)
    data = {
        "conversationId": "c1",
        "transcriptPath": transcript,
        "artifactDirectoryPath": tmp_dir,
        "fullyIdle": True,
        "terminationReason": "MODEL_STOP",
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch.object(stop_guard, "play_stop_sound"):
      stop_guard.main()
    return json.loads(mock_out.getvalue())

  def test_pending_in_project_handoff_blocks_stop_once(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state = {
          "pending_handoff_launch": True,
          "pending_turn_start": True,
          "pending_in_project": True,
          "pending_handoff_file": "/r/proj/state/c1.md",
          "pending_title": "[10:00] Task",
      }
      out = self.run_pending_stop(tmp_dir, state)
      self.assertEqual(out.get("decision"), "continue")
      reason = out["reason"]
      self.assertIn("roadmap.py set c1", reason)
      self.assertIn("/r/proj/state/c1.md", reason)
      self.assertIn("handoff c1 --next", reason)
      self.assertNotIn("handoff_summary", reason)
      state["stop_blocked_handoff"] = True
      self.assertEqual(self.run_pending_stop(tmp_dir, state), {})

  def test_pending_outside_project_handoff_names_summary(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      out = self.run_pending_stop(tmp_dir, {
          "pending_handoff_launch": True,
          "pending_turn_start": True,
          "pending_handoff_file": "/b/c1/handoff_summary_c1.md",
      })
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn('handoff c1 "/b/c1/handoff_summary_c1.md"', out["reason"])
      self.assertNotIn("roadmap.py", out["reason"])

  def test_mid_turn_pending_handoff_allows_stop_when_answering_user(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state_path = os.path.join(tmp_dir, "scratch", ".context_guard_state.json")
      out = self.run_pending_stop(tmp_dir, {
          "pending_handoff_launch": True,
          "pending_turn_start": False,
          "pending_in_project": True,
          "pending_handoff_file": "/r/proj/state/c1.md",
      })
      self.assertEqual(out, {})
      saved = stop_guard.load_state(state_path)
      self.assertFalse(saved.get("pending_handoff_launch"))

  def test_truncated_handoff_output_clears_pending_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      transcript = os.path.join(tmp_dir, "transcript.jsonl")
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "hi"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": "python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1 --next 'go'"
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": "The command exited with code 0.\nOutput:\n<truncated 53 lines>\n▸ R18 roll stuck",
          },
      ]
      with open(transcript, "w") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")
      state_path = os.path.join(tmp_dir, "scratch", ".context_guard_state.json")
      stop_guard.save_state(state_path, {
          "pending_handoff_launch": True,
          "pending_in_project": True,
          "pending_handoff_file": "/r/proj/state/c1.md",
      })
      data = {
          "conversationId": "c1",
          "transcriptPath": transcript,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound") as mock_sound:
        stop_guard.main()
      self.assertEqual(json.loads(mock_out.getvalue()), {})
      mock_sound.assert_called_once_with(is_handoff=True)
      saved = stop_guard.load_state(state_path)
      self.assertFalse(saved.get("pending_handoff_launch"))
      self.assertTrue(saved.get("handoff_completed"))

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

  def test_send_stop_notification_spawns_detached_notify_self(self):
    with patch("os.path.isfile", return_value=True), \
         patch("socket.gethostname", return_value="myhost.c.googlers.com"), \
         patch("chat_lifecycle.get_conversation_title", return_value="[14:00] ⦿ Fix bug"), \
         patch("subprocess.Popen") as mock_popen:
      stop_guard.send_stop_notification("conv-123")
      mock_popen.assert_called_once()
      cmd = mock_popen.call_args[0][0]
      self.assertEqual(cmd[0], stop_guard.NOTIFY_SELF_BIN)
      self.assertIn("--summary=Jetski: [14:00] ⦿ Fix bug", cmd)
      self.assertIn("--uri=https://jetski.corp.google.com/box/myhost/c/conv-123", cmd)
      self.assertTrue(mock_popen.call_args[1].get("start_new_session"))


if __name__ == "__main__":
  unittest.main()
