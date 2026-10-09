#!/usr/bin/env python3
"""Tests for pre_tool_guard.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import pre_tool_guard

HOOK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks", "pre_tool_guard.py"))


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

  def run_guard(self, data: dict) -> dict:
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out:
      pre_tool_guard.main()
    return json.loads(mock_out.getvalue())

  def test_internal_error_fails_open_with_valid_json(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      marker = os.path.join(tmp_dir, "marker.json")
      with patch.object(pre_tool_guard, "roadmap_edit_denial", side_effect=OSError("boom")) as guard, \
           patch.dict(os.environ, {"JETSKI_PRE_TOOL_GUARD_MARKER": marker}), \
           patch("sys.stdin", io.StringIO(json.dumps({"toolCall": {"name": "write_to_file", "args": {}}}))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("sys.stderr", new_callable=io.StringIO) as mock_err:
        pre_tool_guard._guarded_main()
      guard.assert_called_once()
      self.assertEqual(json.loads(mock_out.getvalue()), {"decision": "allow"})
      self.assertIn("OSError: boom", mock_err.getvalue())
      with open(marker) as f:
        self.assertEqual(json.load(f)["error"], "OSError: boom")

  def test_unwritable_marker_still_fails_open(self):
    with patch.object(pre_tool_guard, "roadmap_edit_denial", side_effect=OSError("boom")), \
         patch.dict(os.environ, {"JETSKI_PRE_TOOL_GUARD_MARKER": "/dev/null/logs/marker.json"}), \
         patch("sys.stdin", io.StringIO(json.dumps({"toolCall": {"name": "write_to_file", "args": {}}}))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch("sys.stderr", new_callable=io.StringIO) as mock_err:
      pre_tool_guard._guarded_main()
    self.assertEqual(json.loads(mock_out.getvalue()), {"decision": "allow"})
    self.assertIn("OSError: boom", mock_err.getvalue())

  def test_entry_point_runs_the_guards(self):
    # Runs the file the way hooks.json does (path + shebang, cwd = config root):
    # tests that call main() cannot see a dead entry point, which fails open and
    # still prints a valid allow
    conv = f"smoke-{uuid.uuid4().hex}"
    with tempfile.TemporaryDirectory() as tmp_dir:
      marker = os.path.join(tmp_dir, "marker.json")
      cases = [
          (os.path.join(tmp_dir, "p1", "roadmap.json"), "deny"),
          (os.path.join(tmp_dir, "p1", "state", f"{conv}.md"), "allow"),
      ]
      for target, expected in cases:
        with self.subTest(expected=expected):
          proc = subprocess.run(
              [HOOK],
              input=json.dumps({
                  "conversationId": conv,
                  "toolCall": {"name": "write_to_file", "args": {"TargetFile": target}},
              }),
              capture_output=True,
              text=True,
              timeout=10,
              cwd=os.path.dirname(os.path.dirname(HOOK)),
              env={**os.environ, "JETSKI_ROADMAPS_DIR": tmp_dir, "JETSKI_PRE_TOOL_GUARD_MARKER": marker},
          )
          self.assertEqual((proc.returncode, proc.stderr), (0, ""))
          self.assertEqual(json.loads(proc.stdout).get("decision"), expected)
      self.assertFalse(os.path.exists(marker))

  def test_roadmap_files_are_edited_only_via_cli(self):
    with tempfile.TemporaryDirectory() as tmp_dir, \
         patch.dict(os.environ, {"JETSKI_ROADMAPS_DIR": tmp_dir}):
      cases = [
          ("c1", "", "p1/roadmap.json", "deny"),
          ("c1", "", "p1/journal.jsonl", "deny"),
          ("c1", "", "p1/state/c2.md", "deny"),
          ("c1", "", "p1/state/../roadmap.json", "deny"),
          ("sub", "c1", "p1/state/c1.md", "deny"),
          ("c1", "", "p1/state/c1.md", "allow"),
      ]
      for conv, parent, rel, expected in cases:
        for tool in ("write_to_file", "replace_file_content", "multi_replace_file_content"):
          with self.subTest(conv=conv, rel=rel, tool=tool):
            out = self.run_guard({
                "conversationId": conv,
                "parentConversationId": parent,
                "toolCall": {"name": tool, "args": {"TargetFile": os.path.join(tmp_dir, rel)}},
            })
            self.assertEqual(out.get("decision"), expected)
            if expected == "deny":
              self.assertIn("roadmap.py", out.get("reason", ""))

  def test_in_project_handoff_requires_and_refreshes_state_file(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      state_file = os.path.join(tmp_dir, "roadmaps", "p1", "state", "c1.md")
      artifact_dir = os.path.join(tmp_dir, "brain", "c1")
      pre_tool_guard_state = os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      os.makedirs(os.path.dirname(pre_tool_guard_state))
      with open(pre_tool_guard_state, "w") as f:
        json.dump({
            "pending_handoff_launch": True,
            "pending_in_project": True,
            "pending_handoff_file": state_file,
        }, f)
      data = {
          "conversationId": "c1",
          "artifactDirectoryPath": artifact_dir,
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": 'python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1 --next "go"'},
          },
      }
      out = self.run_guard(data)
      self.assertEqual(out.get("decision"), "deny")
      self.assertIn(state_file, out["reason"])

      os.makedirs(os.path.dirname(state_file))
      with open(state_file, "w") as f:
        f.write("# State\nnext: go\n")
      for _ in range(2):
        self.assertEqual(self.run_guard(data).get("decision"), "allow")
      with open(state_file) as f:
        content = f.read()
      self.assertTrue(content.startswith("# State\nnext: go\n"))
      self.assertEqual(content.count(pre_tool_guard.AUTO_SNAPSHOT_HEADER), 1)
      self.assertFalse(os.path.exists(os.path.join(artifact_dir, "handoff_summary.md")))


if __name__ == "__main__":
  unittest.main()
