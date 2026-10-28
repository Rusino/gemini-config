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

  def test_jetski_web_edit_file_blocks_stop_when_unverified(self):
    fake_src = "/workspace/project/Paragraph.cpp"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Fix paragraph"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{"name": "edit_file", "args": {"Path": fake_src}}],
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

  def run_discipline_stop(self, tmp_dir, steps, state=None, files=None):
    transcript = os.path.join(tmp_dir, "transcript.jsonl")
    with open(transcript, "w") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")
    for path, text in (files or {}).items():
      with open(path, "w") as f:
        f.write(text)
    if state is not None:
      stop_guard.save_state(os.path.join(tmp_dir, "scratch", ".context_guard_state.json"), state)
    data = {
        "conversationId": "c1",
        "transcriptPath": transcript,
        "artifactDirectoryPath": tmp_dir,
        "fullyIdle": True,
        "terminationReason": "MODEL_STOP",
    }
    skills = {
        "review-triage": "/s/review-triage/SKILL.md",
        "companion-cl-review": "/s/companion-cl-review/SKILL.md",
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch.object(stop_guard, "discover_skills", return_value=skills), \
         patch.object(stop_guard, "play_stop_sound"):
      stop_guard.main()
    return json.loads(mock_out.getvalue())

  def review_request(self, idx=1):
    return {
        "step_index": idx,
        "type": "USER_INPUT",
        "content": "<USER_REQUEST>Follow `review-triage` + companion-cl-review</USER_REQUEST>",
    }

  def test_unread_named_skill_blocks_stop_once_per_turn(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          self.review_request(),
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "read_file",
                  "args": {"Path": '"/s/review-triage/SKILL.md"'},
              }],
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("`companion-cl-review`", out["reason"])
      self.assertNotIn("`review-triage`", out["reason"])
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_skill_read_via_command_or_earlier_turn_allows_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "warmup"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "read_file", "args": {"Path": "/s/review-triage/SKILL.md"}},
                  {
                      "name": "run_command",
                      "args": {"CommandLine": "cat /s/companion-cl-review/SKILL.md"},
                  },
              ],
          },
          self.review_request(3),
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_skill_name_outside_user_request_is_ignored(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [{
          "step_index": 1,
          "type": "USER_INPUT",
          "content": "<USER_REQUEST>hi</USER_REQUEST>\n[radar] review-triage chat open",
      }]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_review_report_without_ledger_and_assertless_wpt_block_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      report = os.path.join(tmp_dir, "review_report.md")
      wpt = os.path.join(tmp_dir, "repro.html")
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>go</USER_REQUEST>"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "write_file", "args": {"Path": report}},
                  {"name": "write_file", "args": {"Path": wpt}},
              ],
          },
      ]
      files = {
          report: "# Report\n**Case 4: Fundamental Flaw**\n",
          wpt: '<script src="/resources/testharness.js"></script><script>test(() => {});</script>',
      }
      out = self.run_discipline_stop(tmp_dir, steps, files=files)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("Execution Ledger", out["reason"])
      self.assertIn("repro.html", out["reason"])

  def test_review_report_with_ledger_and_asserting_wpt_allows_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      report = os.path.join(tmp_dir, "review_report.md")
      wpt = os.path.join(tmp_dir, "repro.html")
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>go</USER_REQUEST>"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "write_file", "args": {"Path": report}},
                  {"name": "edit_file", "args": {"Path": wpt}},
              ],
          },
      ]
      files = {
          report: "# Report\n## Execution Ledger\n| step | done |\n**Case 4**\n",
          wpt: '<script src="/resources/testharness.js"></script>'
               "<script>test(() => { assert_equals(1, 1); });</script>",
      }
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps, files=files), {})

  # Verify inline chat review verdicts and mandatory Flutter corpus gating on reviewed CLs
  def test_inline_chat_case_classification_without_ledger_blocks_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "content": "Verdict: **Case 1: Ideal CL**. LGTM.",
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("Execution Ledger", out["reason"])

  def test_skparagraph_review_approval_without_flutter_corpus_blocks_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/review-triage/"
                          "scripts/triage_cl.py 12345"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED"
                  " (`run_flutter_paragraph_tests.py`)** | Touches"
                  " `SkParagraph/SkUnicode/SkShaper` (`ParagraphImpl.cpp`) |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n\n"
                  "Verdict: **Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("[REVIEW CORPUS GUARD]", out["reason"])
      self.assertIn("run_flutter_paragraph_tests.py", out["reason"])

  def test_skparagraph_review_approval_with_passing_flutter_corpus_allows_stop(
      self,
  ):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/review-triage/"
                          "scripts/triage_cl.py 12345"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED"
                  " (`run_flutter_paragraph_tests.py`)** |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/"
                          "flutter-test-paragraph-engine/scripts/"
                          "run_flutter_paragraph_tests.py"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": "The command exited with code 0.\nAll 2400 tests passed.",
          },
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n- Flutter corpus:"
                  " done\n\n**Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_skparagraph_review_pushback_case4_allows_stop_without_flutter_corpus(
      self,
  ):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/review-triage/"
                          "scripts/triage_cl.py 12345"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED"
                  " (`run_flutter_paragraph_tests.py`)** |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n\n"
                  "**Case 4: Fundamental Flaw**. Do not submit."
              ),
          },
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_schedule_tool_call_promotes_conversation_to_waiting_state(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      transcript = os.path.join(tmp_dir, "transcript.jsonl")
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>poll every 5m</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "schedule",
                  "args": {"CronExpression": "*/5 * * * *", "Prompt": "tick"},
              }],
          },
      ]
      with open(transcript, "w", encoding="utf-8") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")
      data = {
          "conversationId": "12345678-1234-1234-1234-123456789abc",
          "transcriptPath": transcript,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": False,
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), patch(
          "sys.stdout", new_callable=io.StringIO
      ) as mock_out, patch(
          "chat_lifecycle.set_conversation_wait_state"
      ) as mock_wait:
        stop_guard.main()
        self.assertEqual(json.loads(mock_out.getvalue()), {})
        mock_wait.assert_called_once_with(
            "12345678-1234-1234-1234-123456789abc", waiting=True
        )

      # Once the scheduled task ends or is killed and fullyIdle becomes True,
      # stop_guard restores the auto-promoted chat back to ⦿ (waiting=False)
      data["fullyIdle"] = True
      data["terminationReason"] = "NO_TOOL_CALL"
      with patch("sys.stdin", io.StringIO(json.dumps(data))), patch(
          "sys.stdout", new_callable=io.StringIO
      ) as mock_out, patch(
          "chat_lifecycle.set_conversation_wait_state"
      ) as mock_wait, patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        self.assertEqual(json.loads(mock_out.getvalue()), {})
        mock_wait.assert_called_once_with(
            "12345678-1234-1234-1234-123456789abc", waiting=False
        )

  def test_duration_seconds_schedule_does_not_promote_to_waiting(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      transcript = os.path.join(tmp_dir, "transcript.jsonl")
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>wait 60s</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "schedule",
                  "args": {"DurationSeconds": 60, "TimerCondition": "task-1", "Prompt": "tick"},
              }],
          },
      ]
      with open(transcript, "w", encoding="utf-8") as f:
        for s in steps:
          f.write(json.dumps(s) + "\n")
      data = {
          "conversationId": "12345678-1234-1234-1234-123456789abc",
          "transcriptPath": transcript,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": False,
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), patch(
          "sys.stdout", new_callable=io.StringIO
      ) as mock_out, patch(
          "chat_lifecycle.set_conversation_wait_state"
      ) as mock_wait:
        stop_guard.main()
        self.assertEqual(json.loads(mock_out.getvalue()), {})
        mock_wait.assert_not_called()

  def test_termination_reason_no_tool_call_runs_verification_guard(self):
    fake_src = "/workspace/project/feature.py"
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Please implement feature"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "write_file",
                  "args": {"Path": fake_src, "Content": "x = 1"},
              }],
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
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        stop_guard.main()
      res = json.loads(mock_out.getvalue())
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[VERIFICATION GUARD]", res.get("reason", ""))

  def test_vcs_baton_pending_blocks_stop_once_per_turn_without_ask_question(self):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "finish task"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "python3 ~/.gemini/config/hooks/roadmap.py set c1 R1 done"},
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "R1 done: Task 1\n"
                  "[VCS BATON: PENDING — repo=/work/repo; own_dirty=foo.py; foreign_dirty=none; "
                  "unpushed=none; gerrit=n/a; action: call ask_question]\n"
                  "[NEXT-TASK BATON: YOURS — propose R2]\n"
              ),
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
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out:
        stop_guard.main()
      res = json.loads(mock_out.getvalue())
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[VCS BATON GUARD]", res.get("reason", ""))
      self.assertIn("own_dirty=foo.py", res.get("reason", ""))

      # Second stop call in same user turn allows stop
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      self.assertEqual(json.loads(mock_out.getvalue()), {})

  def test_vcs_baton_pending_allows_stop_when_ask_question_called(self):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "finish task"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {"CommandLine": "python3 ~/.gemini/config/hooks/roadmap.py set c1 R1 done"},
                  },
                  {
                      "name": "ask_question",
                      "args": {
                          "questions": [{
                              "question": "Commit and push?",
                              "options": ["(Recommended) Commit + push", "Commit only", "Not now"],
                          }]
                      },
                  },
              ],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "[VCS BATON: PENDING — repo=/work/repo; own_dirty=foo.py; action: call ask_question]\n"
              ),
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
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      self.assertEqual(json.loads(mock_out.getvalue()), {})

  def test_vcs_baton_clean_allows_stop(self):
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl") as f_trans, \
         tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "finish task"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {"CommandLine": "python3 ~/.gemini/config/hooks/roadmap.py set c1 R1 done"},
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "The command exited with code 0.\n"
                  "R1 done: Task 1\n"
                  "[VCS BATON: CLEAN — repo=/work/repo; own_dirty=none; unpushed=none; action: report]\n"
              ),
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
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      self.assertEqual(json.loads(mock_out.getvalue()), {})

  def test_title_guard_blocks_stop_when_pending_and_not_titled(self):
    with tempfile.TemporaryDirectory() as tmp_dir, \
         tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as f_trans:
      scratch_dir = os.path.join(tmp_dir, "scratch")
      os.makedirs(scratch_dir, exist_ok=True)
      st_file = os.path.join(scratch_dir, ".context_guard_state.json")
      with open(st_file, "w", encoding="utf-8") as f:
        json.dump({"pending_title_guard": True}, f)

      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Help with code"},
          {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Done."},
      ]
      for s in steps:
        f_trans.write(json.dumps(s) + "\n")
      f_trans.flush()

      data = {
          "conversationId": "c1",
          "transcriptPath": f_trans.name,
          "artifactDirectoryPath": tmp_dir,
          "fullyIdle": True,
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("chat_lifecycle.get_conversation_title", return_value="[10:00] ⦿ Investigation"), \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      res = json.loads(mock_out.getvalue())
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[TITLE GUARD]", res.get("reason", ""))


if __name__ == "__main__":
  unittest.main()
