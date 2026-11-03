#!/usr/bin/env python3
"""Tests for stop_guard.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import mock_open, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import stop_guard


class TestStopGuard(unittest.TestCase):

  def setUp(self):
    self._patch_dossier = patch("stop_guard.spawn_background_dossier_update")
    self._patch_dossier.start()

  def tearDown(self):
    self._patch_dossier.stop()

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

  def test_play_stop_sound_respects_enable_g3_notifications(self):
    cfg_disabled = '{"userSettings": {"enableG3Notifications": false}}'
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=cfg_disabled)), \
         patch("time.sleep"), \
         patch("stop_guard.send_stop_notification") as mock_notify:
      stop_guard.play_stop_sound(is_handoff=False)
      mock_notify.assert_not_called()

    cfg_enabled = '{"userSettings": {"enableG3Notifications": true}}'
    with patch("os.path.exists", return_value=True), \
         patch("builtins.open", mock_open(read_data=cfg_enabled)), \
         patch("time.sleep"), \
         patch("stop_guard.send_stop_notification") as mock_notify:
      stop_guard.play_stop_sound(is_handoff=False)
      mock_notify.assert_called_once()

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

  def test_review_after_triage_cl_without_save_review_blocks_stop(self):
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
              "content": "triage finished",
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n| Step | Status |\n|---|---|\n"
                  "Verdict: **Case 1: Ideal CL**"
              ),
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("save-review", out["reason"])

  def test_review_after_triage_cl_with_save_review_allows_stop(self):
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome CLEAN --summary 'All tests pass'"
                          )
                      },
                  },
              ],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "content": "triage finished",
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n| Step | Status |\n|---|---|\n"
                  "Verdict: **Case 1: Ideal CL**"
              ),
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out, {})

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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome PUSHBACK --summary 'Fundamental flaw'"
                          )
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome PUSHBACK --summary 'Fundamental flaw'"
                          )
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
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED"
                  " (`run_flutter_paragraph_tests.py`)** |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/"
                              "flutter-test-paragraph-engine/scripts/"
                              "run_flutter_paragraph_tests.py"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome CLEAN --summary 'All 2400 tests passed'"
                          )
                      },
                  },
              ],
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome PUSHBACK --summary 'Fundamental flaw'"
                          )
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

  def test_asan_canary_guard_blocks_when_required_bot_not_triggered(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL 1408157</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/skia-canary-advisor/"
                          "scripts/canary_advisor.py advise --cl 1408157"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "AGENT ACTION (execute this command):\n"
                  "  git cl try -i 1408157 -B skia/skia.primary -b Bot-ASAN -b Canary-Flutter\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
          },
      ]
      res = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[ASAN/CANARY GUARD]", res.get("reason", ""))
      self.assertIn("Bot-ASAN", res.get("reason", ""))
      self.assertIn("Canary-Flutter", res.get("reason", ""))

  def test_asan_canary_guard_allows_when_bots_triggered(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL 1408157</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/skia-canary-advisor/"
                          "scripts/canary_advisor.py advise --cl 1408157"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "AGENT ACTION (execute this command):\n"
                  "  git cl try -i 1408157 -B skia/skia.primary -b Bot-ASAN -b Canary-Flutter\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "git cl try -i 1408157 -B skia/skia.primary -b Bot-ASAN -b Canary-Flutter"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": "The command exited with code 0.\n",
          },
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "content": "Triggered trybots and started background watcher.",
          },
      ]
      # Stopping to wait without recommending approval satisfies ASAN/CANARY GUARD
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

      # Approving before tryjobs finish is blocked by TRYJOB GATE GUARD
      premature_steps = steps[:-1] + [{
          "step_index": 6,
          "type": "PLANNER_RESPONSE",
          "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
      }]
      with tempfile.TemporaryDirectory() as tmp_premature:
        res_premature = self.run_discipline_stop(tmp_premature, premature_steps)
      self.assertEqual(res_premature.get("decision"), "continue")
      self.assertIn("[TRYJOB GATE GUARD]", res_premature.get("reason", ""))
      self.assertIn("never checked", res_premature.get("reason", ""))

      # Approving after TRYJOBS: ALL GREEN succeeds
      green_steps = premature_steps[:-1] + [
          {
              "step_index": 6,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 0.\n\nTRYJOBS: ALL GREEN\n",
          },
          {
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
          },
      ]
      with tempfile.TemporaryDirectory() as tmp_green:
        self.assertEqual(self.run_discipline_stop(tmp_green, green_steps), {})

      # Red font trybot blocks approval until fixed or marked UNVERIFIED ON TRYBOTS
      red_steps = premature_steps[:-1] + [
          {
              "step_index": 6,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "Command exited with code 0.\n\n"
                  "TRYJOBS: NOT GREEN (Test-Mac14-NativeFonts_Fontations)\n"
              ),
          },
          {
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
          },
      ]
      with tempfile.TemporaryDirectory() as tmp_red:
        res_red = self.run_discipline_stop(tmp_red, red_steps)
      self.assertEqual(res_red.get("decision"), "continue")
      self.assertIn("[TRYJOB GATE GUARD]", res_red.get("reason", ""))
      self.assertIn("not green", res_red.get("reason", ""))

      # Scheduling new builds invalidates an earlier TRYJOBS: ALL GREEN verdict
      stale_steps = green_steps[:-1] + [
          {
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "git cl try -i 1408157 -B skia/skia.primary -b Bot-ASAN -b Canary-Flutter"
                      )
                  },
              }],
          },
          {
              "step_index": 8,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 0.\n",
          },
          {
              "step_index": 9,
              "type": "PLANNER_RESPONSE",
              "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
          },
      ]
      with tempfile.TemporaryDirectory() as tmp_stale:
        res_stale = self.run_discipline_stop(tmp_stale, stale_steps)
      self.assertEqual(res_stale.get("decision"), "continue")
      self.assertIn("[TRYJOB GATE GUARD]", res_stale.get("reason", ""))
      self.assertIn("never checked", res_stale.get("reason", ""))

  def test_asan_canary_guard_accepts_luci_auth_context_and_rejects_failed_try(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      base_steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review CL 1408157</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "python3 ~/.gemini/config/skills/skia-canary-advisor/"
                          "scripts/canary_advisor.py advise --cl 1408157"
                      )
                  },
              }],
          },
          {
              "step_index": 3,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "AGENT ACTION (execute this command):\n"
                  "  luci-auth context -- git cl try -i 1408157 -B skia/skia.primary"
                  " -b Bot-ASAN -b Canary-Chromium\n"
              ),
          },
      ]
      final_step = {
          "step_index": 8,
          "type": "PLANNER_RESPONSE",
          "content": "### Execution Ledger\n- Triage: done\n\n**Case 1: Ideal CL**",
      }

      # Non-zero exit code from git cl try must NOT satisfy ASAN/CANARY GUARD
      failed_steps = base_steps + [
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "git cl try -i 1408157 -B skia/skia.primary"
                          " -b Bot-ASAN -b Canary-Chromium"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 1.\n\nSSOAuthenticator: exit status 1\n",
          },
          final_step,
      ]
      with tempfile.TemporaryDirectory() as tmp_fail:
        res_fail = self.run_discipline_stop(tmp_fail, failed_steps)
      self.assertEqual(res_fail.get("decision"), "continue")
      self.assertIn("[ASAN/CANARY GUARD]", res_fail.get("reason", ""))
      self.assertIn("Bot-ASAN", res_fail.get("reason", ""))

      # Masked `|| true` failure with SSOAuthenticator error must also be rejected
      masked_steps = base_steps + [
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "git cl try -i 1408157 -B skia/skia.primary"
                          " -b Bot-ASAN -b Canary-Chromium || true"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 0.\n\nSSOAuthenticator: exit status 1\n",
          },
          final_step,
      ]
      with tempfile.TemporaryDirectory() as tmp_masked:
        res_masked = self.run_discipline_stop(tmp_masked, masked_steps)
      self.assertEqual(res_masked.get("decision"), "continue")
      self.assertIn("[ASAN/CANARY GUARD]", res_masked.get("reason", ""))

      # Clean exit 0 under luci-auth context + green triage satisfies both tryjob guards
      triage_green_step = {
          "step_index": 75,
          "type": "GENERIC",
          "status": "DONE",
          "content": "Command exited with code 0.\n\nTRYJOBS: ALL GREEN\n",
      }
      ok_steps = failed_steps[:-1] + [
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": (
                          "luci-auth context -- git cl try -i 1408157 -B skia/skia.primary"
                          " -b Bot-ASAN -b Canary-Chromium"
                      )
                  },
              }],
          },
          {
              "step_index": 7,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 0.\n\nScheduled builds\n",
          },
          triage_green_step,
          {**final_step, "step_index": 80},
      ]
      with tempfile.TemporaryDirectory() as tmp_ok:
        self.assertEqual(self.run_discipline_stop(tmp_ok, ok_steps), {})

      # Sibling read_file output containing exit-code or TRYJOBS text must not spoof command output
      mixed_steps = base_steps + [
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {"name": "read_file", "args": {"Path": "/tmp/notes.txt"}},
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "luci-auth context -- git cl try -i 1408157 -B skia/skia.primary"
                              " -b Bot-ASAN -b Canary-Chromium"
                          )
                      },
                  },
              ],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  "Created At: 2026-11-03T16:00:00Z\n"
                  "Completed At: 2026-11-03T16:00:01Z\n"
                  "Showing lines 1-3 of 3\n"
                  "Command exited with code 1.\n"
                  "TRYJOBS: ALL GREEN\n"
              ),
          },
          {
              "step_index": 6,
              "type": "GENERIC",
              "status": "DONE",
              "content": "Command exited with code 0.\n\nScheduled builds\n",
          },
          {**final_step, "step_index": 80},
      ]
      with tempfile.TemporaryDirectory() as tmp_mixed_unchecked:
        res_mixed = self.run_discipline_stop(tmp_mixed_unchecked, mixed_steps)
      self.assertNotIn("[ASAN/CANARY GUARD]", res_mixed.get("reason", ""))
      self.assertIn("[TRYJOB GATE GUARD]", res_mixed.get("reason", ""))

      with tempfile.TemporaryDirectory() as tmp_mixed:
        self.assertEqual(
            self.run_discipline_stop(
                tmp_mixed, mixed_steps[:-1] + [triage_green_step, {**final_step, "step_index": 80}]
            ),
            {},
        )

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

  # Raw LS titles with concrete words but no [HH:MM] marker do not satisfy pending_title_guard
  def test_title_guard_blocks_raw_ls_title_lacking_lifecycle_prefix(self):
    with tempfile.TemporaryDirectory() as tmp_dir, \
         tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as f_trans:
      scratch_dir = os.path.join(tmp_dir, "scratch")
      os.makedirs(scratch_dir, exist_ok=True)
      st_file = os.path.join(scratch_dir, ".context_guard_state.json")
      with open(st_file, "w", encoding="utf-8") as f:
        json.dump({"pending_title_guard": True}, f)

      steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Help with crash"},
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
           patch(
               "chat_lifecycle.get_conversation_title",
               return_value="Fontations Stream OOM Analysis",
           ), \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      res = json.loads(mock_out.getvalue())
      self.assertEqual(res.get("decision"), "continue")
      self.assertIn("[TITLE GUARD]", res.get("reason", ""))

      # Once prefixed by chat_lifecycle.py set-title, stop_guard allows turn completion
      with open(st_file, "w", encoding="utf-8") as f:
        json.dump({"pending_title_guard": True}, f)
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out2, \
           patch(
               "chat_lifecycle.get_conversation_title",
               return_value="[10:08] ⦿ Fontations Stream OOM Analysis",
           ), \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      self.assertEqual(json.loads(mock_out2.getvalue()), {})

  def test_macos_subagent_without_parent_field_skips_stop_guard(self):
    parent_id = "0687185b-9c27-41f8-9a0a-bfec291f51f8"
    sub_id = "b8d01615-02b4-46ed-93ee-3774ec3a473f"
    with tempfile.TemporaryDirectory() as app_dir:
      sub_brain = os.path.join(app_dir, "brain", sub_id)
      scratch_dir = os.path.join(sub_brain, "scratch")
      tx_dir = os.path.join(sub_brain, ".system_generated", "logs")
      os.makedirs(scratch_dir, exist_ok=True)
      os.makedirs(tx_dir, exist_ok=True)
      with open(os.path.join(scratch_dir, ".context_guard_state.json"), "w", encoding="utf-8") as f:
        json.dump({"pending_title_guard": True}, f)

      tx_path = os.path.join(tx_dir, "transcript.jsonl")
      with open(tx_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "step_index": 0,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "content": (
                f"<SYSTEM_MESSAGE>\n[Message] timestamp=2026-10-29T23:33:42Z "
                f"sender={parent_id} priority=MESSAGE_PRIORITY_HIGH content=Run\n"
                "</SYSTEM_MESSAGE>"
            ),
        }) + "\n")
        f.write(
            json.dumps({"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Done."})
            + "\n"
        )

      data = {
          "conversationId": sub_id,
          "transcriptPath": tx_path,
          "artifactDirectoryPath": sub_brain,
          "fullyIdle": True,
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch("chat_lifecycle.get_conversation_title", return_value=""), \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
      self.assertEqual(json.loads(mock_out.getvalue()), {})

  def test_copied_predecessor_scratch_state_does_not_rearm_handoff_completed(self):
    with tempfile.TemporaryDirectory() as app_dir:
      brain_dir = os.path.join(app_dir, "brain", "c1")
      scratch_dir = os.path.join(brain_dir, "scratch")
      os.makedirs(scratch_dir, exist_ok=True)
      state_file = os.path.join(scratch_dir, ".context_guard_state.json")
      with open(state_file, "w", encoding="utf-8") as f:
        json.dump({"handed_off_to": "c1", "handoff_completed": False}, f)
      tx_path = os.path.join(brain_dir, "transcript.jsonl")
      with open(tx_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "hi"}) + "\n")
        f.write(json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "content": "ok"}) + "\n")
      data = {
          "conversationId": "c1",
          "transcriptPath": tx_path,
          "artifactDirectoryPath": brain_dir,
          "fullyIdle": True,
          "terminationReason": "NO_TOOL_CALL",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO), \
           patch("chat_lifecycle.get_conversation_title", return_value="[10:00] ⦿ Fix hook"), \
           patch.object(stop_guard, "play_stop_sound") as mock_sound:
        stop_guard.main()
      mock_sound.assert_called_once_with(is_handoff=False)
      self.assertFalse(os.path.exists(state_file))

  def test_skparagraph_review_approval_with_background_task_corpus_allows_stop(self):
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome CLEAN --summary 'All 2400 tests passed'"
                          )
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
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED "
                  "(`run_flutter_paragraph_tests.py`)** |\n"
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
                          "run_flutter_paragraph_tests.py --out /tmp/flutter_gate"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "RUNNING",
              "content": (
                  "Tool is running as a background task with task id: c1/task-38\n"
                  "Task Description: python3 run_flutter_paragraph_tests.py"
              ),
          },
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "content": "Waiting for corpus in background (task-38)",
          },
          {
              "step_index": 7,
              "type": "SYSTEM_MESSAGE",
              "status": "DONE",
              "content": (
                  '<SYSTEM_MESSAGE>\n[Message] sender=c1/task-38\n'
                  'Task id "c1/task-38" finished with result:\n'
                  "Command exited with code 0.\n</SYSTEM_MESSAGE>"
              ),
          },
          {
              "step_index": 8,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n- Flutter corpus: done\n\n"
                  "**Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_skparagraph_review_approval_with_failed_background_task_corpus_blocks_stop(self):
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome CLEAN --summary 'All 2400 tests passed'"
                          )
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
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED "
                  "(`run_flutter_paragraph_tests.py`)** |\n"
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
                          "run_flutter_paragraph_tests.py --out /tmp/flutter_gate"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "RUNNING",
              "content": (
                  "Tool is running as a background task with task id: c1/task-38\n"
                  "Task Description: python3 run_flutter_paragraph_tests.py"
              ),
          },
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "content": "Waiting for corpus in background (task-38)",
          },
          {
              "step_index": 7,
              "type": "SYSTEM_MESSAGE",
              "status": "DONE",
              "content": (
                  '<SYSTEM_MESSAGE>\n[Message] sender=c1/task-38\n'
                  'Task id "c1/task-38" finished with result:\n'
                  "Command exited with code 1.\n</SYSTEM_MESSAGE>"
              ),
          },
          {
              "step_index": 8,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n- Flutter corpus: failed\n\n"
                  "**Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      out = self.run_discipline_stop(tmp_dir, steps)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("[REVIEW CORPUS GUARD]", out.get("reason", ""))

  def test_skparagraph_review_approval_with_subagent_corpus_report_allows_stop(self):
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
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 12345"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 12345 --patchset 1 "
                              "--outcome CLEAN --summary 'All 2400 tests passed'"
                          )
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
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED "
                  "(`run_flutter_paragraph_tests.py`)** |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "invoke_subagent",
                  "args": {
                      "Prompt": (
                          "Run run_flutter_paragraph_tests.py on CL 12345 and report "
                          "compare.md with Integrity Check"
                      )
                  },
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  'Created the following subagent:\n{\n  "conversationId": "sub-123"\n}'
              ),
          },
          {
              "step_index": 6,
              "type": "SYSTEM_MESSAGE",
              "status": "DONE",
              "content": (
                  "<SYSTEM_MESSAGE>\n[Message] sender=sub-123\n"
                  "### Test Gate Results for Skia CL\n"
                  "Exit status: 0\n"
                  "**Вердикт целостности (Integrity Check):** "
                  "`VALID (все пакеты тестов завершены штатно)`\n"
                  "</SYSTEM_MESSAGE>"
              ),
          },
          {
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n- Flutter corpus: done\n\n"
                  "**Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_webparagraph_review_approval_with_compare_md_read_allows_stop(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      compare_path = os.path.join(tmp_dir, "webparagraph_compare.md")
      with open(compare_path, "w", encoding="utf-8") as f:
        f.write(
            "# WebParagraph Corpus Report (baseline = candidate)\n\n"
            "## Паспорт прогона (Run Attestation)\n"
            "**Вердикт целостности (Integrity Check):** `VALID (все сьюты завершены)`\n"
        )
      steps = [
          {
              "step_index": 1,
              "type": "USER_INPUT",
              "content": "<USER_REQUEST>review PR</USER_REQUEST>",
          },
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/skills/review-triage/"
                              "scripts/triage_cl.py 99999"
                          )
                      },
                  },
                  {
                      "name": "run_command",
                      "args": {
                          "CommandLine": (
                              "python3 ~/.gemini/config/scripts/triage_store.py "
                              "save-review --target 99999 --patchset 1 "
                              "--outcome CLEAN --summary 'All pass'"
                          )
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
                  "| **Flutter Corpus Gate** | 🚨 **REQUIRED "
                  "(`run_webparagraph_corpus.py`)** |\n"
              ),
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "read_file",
                  "args": {"Path": compare_path},
              }],
          },
          {
              "step_index": 5,
              "type": "GENERIC",
              "status": "DONE",
              "content": (
                  f"File Path: {compare_path}\n"
                  "# WebParagraph Corpus Report\n"
                  "**Вердикт целостности (Integrity Check):** `VALID (все сьюты завершены)`\n"
              ),
          },
          {
              "step_index": 6,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "### Execution Ledger\n- Triage: done\n- Corpus: done\n\n"
                  "**Case 1: Ideal CL**. LGTM!"
              ),
          },
      ]
      self.assertEqual(self.run_discipline_stop(tmp_dir, steps), {})

  def test_background_verification_command_after_code_edit_allows_stop(self):
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
              "status": "RUNNING",
              "content": (
                  "Tool is running as a background task with task id: c1/task-10\n"
                  "Task Description: python3 -m unittest"
              ),
          },
          {
              "step_index": 5,
              "type": "PLANNER_RESPONSE",
              "content": "Waiting for tests in background",
          },
          {
              "step_index": 6,
              "type": "SYSTEM_MESSAGE",
              "status": "DONE",
              "content": (
                  '<SYSTEM_MESSAGE>\n[Message] sender=c1/task-10\n'
                  'Task id "c1/task-10" finished with result:\n'
                  "Command exited with code 0.\nRan 10 tests.\nOK\n</SYSTEM_MESSAGE>"
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
          "terminationReason": "MODEL_STOP",
      }
      with patch("sys.stdin", io.StringIO(json.dumps(data))), \
           patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
           patch.object(stop_guard, "play_stop_sound"):
        stop_guard.main()
        out = json.loads(mock_out.getvalue())
        self.assertEqual(out, {})

  def _run_stop_with_steps(
      self, tmp_dir: str, steps: list[dict], fully_idle: bool = True
  ) -> dict:
    transcript = os.path.join(tmp_dir, "transcript.jsonl")
    with open(transcript, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")
    data = {
        "conversationId": "c1",
        "transcriptPath": transcript,
        "artifactDirectoryPath": tmp_dir,
        "fullyIdle": fully_idle,
        "terminationReason": "NO_TOOL_CALL",
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch.object(stop_guard, "play_stop_sound"):
      stop_guard.main()
    return json.loads(mock_out.getvalue())

  def test_parallel_subagent_plain_text_question_blocks_stop_once(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      # Declarative status note while subagent runs (fullyIdle=False) is allowed
      ok_steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Запусти проверку"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{"name": "invoke_subagent", "args": {"Role": "Runner"}}],
          },
          {
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "content": "Запустила baseline субагента, вернусь с результатами.",
          },
      ]
      self.assertEqual(
          self._run_stop_with_steps(tmp_dir, ok_steps, fully_idle=False), {}
      )

      # Asking a question in plain text while subagent runs (fullyIdle=False) blocks once
      bad_steps = ok_steps[:-1] + [{
          "step_index": 3,
          "type": "PLANNER_RESPONSE",
          "content": "Пока он работает, вопрос к тебе: на чём проверяем?",
      }]
      out = self._run_stop_with_steps(tmp_dir, bad_steps, fully_idle=False)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("[PARALLEL SUBAGENT QUESTION GUARD]", out["reason"])
      # Second attempt in the same wakeup does not loop forever
      self.assertEqual(
          self._run_stop_with_steps(tmp_dir, bad_steps, fully_idle=False), {}
      )

  def test_plain_text_choice_question_blocks_stop_unless_ask_question_called(self):
    with tempfile.TemporaryDirectory() as tmp_dir:
      # 1. Multi-option plain-text choice question blocks once
      choice_steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Что дальше"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "Нужен твой выбор, на чём проверять:\n"
                  "1. Локальный Chromium\n"
                  "2. CitC google3\n\n"
                  "Какой вариант берём?"
              ),
          },
      ]
      out = self._run_stop_with_steps(tmp_dir, choice_steps, fully_idle=True)
      self.assertEqual(out.get("decision"), "continue")
      self.assertIn("[ASK_QUESTION GUARD]", out["reason"])
      self.assertEqual(
          self._run_stop_with_steps(tmp_dir, choice_steps, fully_idle=True), {}
      )

    with tempfile.TemporaryDirectory() as tmp_dir2:
      # 2. Plain-text confirmation question blocks once
      confirm_steps = [
          {"step_index": 10, "type": "USER_INPUT", "content": "Посмотри варианты"},
          {
              "step_index": 11,
              "type": "PLANNER_RESPONSE",
              "content": "Хочешь, сделаю оба пункта прямо сейчас?",
          },
      ]
      out2 = self._run_stop_with_steps(tmp_dir2, confirm_steps, fully_idle=True)
      self.assertEqual(out2.get("decision"), "continue")
      self.assertIn("[ASK_QUESTION GUARD]", out2["reason"])

    with tempfile.TemporaryDirectory() as tmp_dir3:
      # 3. Open-ended factual question without discrete choices is allowed
      open_steps = [
          {"step_index": 20, "type": "USER_INPUT", "content": "Проверь мой док"},
          {
              "step_index": 21,
              "type": "PLANNER_RESPONSE",
              "content": "Не нашла документ по этому имени в Drive — подскажи точный URL?",
          },
      ]
      self.assertEqual(
          self._run_stop_with_steps(tmp_dir3, open_steps, fully_idle=True), {}
      )

    with tempfile.TemporaryDirectory() as tmp_dir4:
      # 4. Calling ask_question satisfies the guard
      tool_steps = [
          {"step_index": 30, "type": "USER_INPUT", "content": "Что дальше"},
          {
              "step_index": 31,
              "type": "PLANNER_RESPONSE",
              "content": "Какой вариант берём?",
              "tool_calls": [{
                  "name": "ask_question",
                  "args": {
                      "questions": [{
                          "question": "Какой вариант берём?",
                          "options": ["(Recommended) Chromium", "Google3"],
                      }]
                  },
              }],
          },
      ]
      self.assertEqual(
          self._run_stop_with_steps(tmp_dir4, tool_steps, fully_idle=True), {}
      )

    with tempfile.TemporaryDirectory() as tmp_dir5:
      # 5. Earlier answered ask_question does NOT mask a later plain-text question (SG-B1 + SG-B2)
      stale_aq_steps = [
          {"step_index": 40, "type": "USER_INPUT", "content": "Начни работу"},
          {
              "step_index": 41,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{"name": "ask_question", "args": {"questions": []}}],
          },
          {"step_index": 42, "type": "GENERIC", "status": "DONE", "content": "Option A"},
          {
              "step_index": 43,
              "type": "PLANNER_RESPONSE",
              "content": "Which option?\n- (Recommended) Local build\n- Remote build",
          },
          {"step_index": 44, "type": "PLANNER_RESPONSE", "content": ""},
      ]
      out5 = self._run_stop_with_steps(tmp_dir5, stale_aq_steps, fully_idle=True)
      self.assertEqual(out5.get("decision"), "continue")
      self.assertIn("[ASK_QUESTION GUARD]", out5["reason"])

    with tempfile.TemporaryDirectory() as tmp_dir6:
      # 6. Latin 'ok?' and Russian 'ли' confirmation questions also require ask_question (SG-B8)
      li_steps = [
          {"step_index": 50, "type": "USER_INPUT", "content": "Проверь"},
          {"step_index": 51, "type": "PLANNER_RESPONSE", "content": "Запускаем ли полный прогон?"},
      ]
      out6 = self._run_stop_with_steps(tmp_dir6, li_steps, fully_idle=True)
      self.assertEqual(out6.get("decision"), "continue")
      self.assertIn("[ASK_QUESTION GUARD]", out6["reason"])


if __name__ == "__main__":
  unittest.main()
