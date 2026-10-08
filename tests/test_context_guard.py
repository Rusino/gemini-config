#!/usr/bin/env python3
"""Tests for context_guard.py."""

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import chat_lifecycle
import context_guard


def gm_response(used: int, cap: int = 256000, ckpt: int | None = -1) -> dict:
  csm = {"contextWindowMetadata": {"estimatedTokensUsed": used, "maxContextTokens": cap}}
  # The live LS omits checkpointIndex for checkpoint #0 (proto3 zero value)
  if ckpt is not None:
    csm["checkpointIndex"] = ckpt
  return {"generatorMetadata": [{"chatModel": {"chatStartMetadata": csm}}]}


class TestContextGuard(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.artifact_dir = os.path.join(self.tmp.name, "brain", "c1")
    log_dir = os.path.join(self.artifact_dir, ".system_generated", "logs")
    os.makedirs(log_dir)
    self.transcript = os.path.join(log_dir, "transcript.jsonl")
    open(self.transcript, "w").close()
    self.log_path = os.path.join(self.tmp.name, "guard.jsonl")
    self.env = patch.dict(os.environ, {
        "JETSKI_CONTEXT_GUARD_LOG": self.log_path,
        "JETSKI_ROADMAPS_DIR": os.path.join(self.tmp.name, "roadmaps"),
    })
    self.env.start()
    self.db_info = patch.object(
        context_guard, "get_conversation_db_info", return_value=("[10:00] ⦿ Task", "proj-1")
    )
    self.mock_db = self.db_info.start()
    self.rpc = patch.object(chat_lifecycle, "ls_rpc", return_value=gm_response(1000))
    self.mock_rpc = self.rpc.start()

  def tearDown(self):
    self.rpc.stop()
    self.db_info.stop()
    self.env.stop()
    self.tmp.cleanup()

  def run_guard(self, inv: int = 0, steps: int = 10, **extra) -> dict:
    data = {
        "conversationId": "c1",
        "invocationNum": inv,
        "initialNumSteps": steps,
        "transcriptPath": self.transcript,
        "artifactDirectoryPath": self.artifact_dir,
        **extra,
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as out:
      context_guard.main()
    return json.loads(out.getvalue())

  def message(self, out: dict) -> str:
    return "\n".join(s["ephemeralMessage"] for s in out.get("injectSteps", []))

  def last_log(self) -> dict:
    with open(self.log_path, encoding="utf-8") as f:
      return json.loads(f.readlines()[-1])

  def state(self) -> dict:
    return context_guard.load_state(
        os.path.join(self.artifact_dir, "scratch", ".context_guard_state.json")
    )

  def write_transcript(self, *steps: dict) -> None:
    with open(self.transcript, "w", encoding="utf-8") as f:
      for i, s in enumerate(steps):
        f.write(json.dumps({"step_index": i, **s}) + "\n")

  def test_handed_off_chat_is_closed_on_later_turns(self):
    self.mock_rpc.return_value = gm_response(230000)
    self.write_transcript(
        {"type": "USER_INPUT", "content": "go"},
        {"type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {
            "CommandLine": "python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff c1"}}]},
        {"type": "GENERIC", "status": "DONE", "content":
            'The command exited with code 0.\nOutput:\n{"new_conversation_id": "child-9"}'},
    )
    self.assertEqual(self.run_guard(inv=5), {})
    self.assertEqual(self.state()["handed_off_to"], "child-9")
    self.write_transcript({"type": "USER_INPUT", "content": "one more question"})
    msg = self.message(self.run_guard(inv=0))
    self.assertIn("CLOSED CHAT", msg)
    self.assertIn("agentapi send-message child-9", msg)
    self.assertNotIn("HANDOFF REQUIRED", msg)
    self.assertEqual(self.last_log()["level"], "closed")
    self.assertEqual(self.run_guard(inv=1), {})
    self.assertFalse(self.state().get("pending_handoff_launch"))
    self.write_transcript({"type": "USER_INPUT", "content": "no, thanks"})
    msg = self.message(self.run_guard(inv=0))
    self.assertIn("old, closed chat", msg)
    self.assertIn("agentapi send-message child-9", msg)

  def test_build_agentapi_prefix_with_project_id(self):
    with patch.object(
        context_guard,
        "get_conversation_db_info",
        return_value=("Title", "my-project-123"),
    ):
      prefix = context_guard.build_agentapi_prefix("c1", "", [])
      self.assertIn("env -u ANTIGRAVITY_SOURCE_METADATA", prefix)
      self.assertIn('ANTIGRAVITY_PROJECT_ID="my-project-123"', prefix)
      self.assertIn("agentapi new-conversation", prefix)

  def test_build_agentapi_prefix_outside_project(self):
    with patch.object(
        context_guard,
        "get_conversation_db_info",
        return_value=("Title", "outside-of-project"),
    ), patch.object(
        context_guard,
        "infer_project_id_from_workspaces",
        return_value="",
    ):
      prefix = context_guard.build_agentapi_prefix("c1", "", [])
      self.assertIn("env -u ANTIGRAVITY_SOURCE_METADATA", prefix)
      self.assertIn('ANTIGRAVITY_PROJECT_ID="outside-of-project"', prefix)
      self.assertIn("agentapi new-conversation", prefix)

  def test_no_auto_reopen_on_new_turn(self):
    # A new user turn in a finalized conversation must NOT touch its title:
    # markers are applied only mechanically by chat_lifecycle.py commands.
    data = {
        "conversationId": "c1",
        "invocationNum": 0,
        "initialNumSteps": 10,
        "lastUserInput": "Давай продолжим тестирование",
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch.object(context_guard, "get_conversation_db_info", return_value=("[10:00] «» Some Task", "proj")), \
         patch("chat_lifecycle.reopen_conversation") as mock_reopen, \
         patch("chat_lifecycle.update_conversation_title") as mock_update, \
         patch.object(context_guard, "get_transcript_size_kb", return_value=10.0):
      context_guard.main()
      mock_reopen.assert_not_called()
      mock_update.assert_not_called()

  def test_below_soft_is_silent_and_skips_sqlite(self):
    self.mock_rpc.return_value = gm_response(100000)
    self.assertEqual(self.run_guard(inv=3), {})
    self.mock_db.assert_not_called()
    log = self.last_log()
    self.assertEqual((log["level"], log["pct"], log["fallback"]), ("none", 39.1, False))
    self.assertIn("ms", log)

  def test_soft_reminds_at_micro_boundary_and_throttles(self):
    self.mock_rpc.return_value = gm_response(160000)
    msg = self.message(self.run_guard(inv=2, steps=100))
    self.assertIn("SOFT LIMIT", msg)
    self.assertIn("micro-boundary", msg)
    self.assertNotIn("kill_all", msg)
    self.assertEqual(self.last_log()["level"], "soft")
    self.assertEqual(self.run_guard(inv=3, steps=102), {})
    self.assertIn("SOFT LIMIT", self.message(self.run_guard(inv=12, steps=120)))
    self.assertFalse(self.state().get("pending_handoff_launch"))

  def test_hard_mid_turn_reminds_until_launched(self):
    self.mock_rpc.return_value = gm_response(210000)
    msg = self.message(self.run_guard(inv=3))
    self.assertIn("MID-TURN HANDOFF", msg)
    self.assertEqual(self.last_log()["level"], "hard")
    self.assertTrue(self.state()["pending_handoff_launch"])
    reminder = self.message(self.run_guard(inv=4))
    self.assertIn("CONTEXT GUARD REMINDER", reminder)
    self.assertIn("does not exist yet", reminder)
    self.assertEqual(self.last_log()["level"], "pending")

  def test_turn_start_hard_answers_first_without_immediate_reminder(self):
    self.mock_rpc.return_value = gm_response(210000)
    msg = self.message(self.run_guard(inv=0))
    self.assertIn("HANDOFF REQUIRED", msg)
    self.assertIn("First answer the user's current request", msg)
    self.assertTrue(self.state()["pending_turn_start"])
    for inv in (1, 2, 3):
      self.assertEqual(self.run_guard(inv=inv), {})

  def test_hard_is_not_snoozed_across_turns(self):
    self.mock_rpc.return_value = gm_response(210000)
    self.assertIn("HANDOFF REQUIRED", self.message(self.run_guard(inv=0, steps=10)))
    self.run_guard(inv=1, steps=12)
    self.assertIn("HANDOFF REQUIRED", self.message(self.run_guard(inv=0, steps=20)))

  def test_compaction_triggers_late_handoff(self):
    self.mock_rpc.return_value = gm_response(60000, ckpt=4)
    msg = self.message(self.run_guard(inv=0))
    self.assertIn("HANDOFF REQUIRED", msg)
    self.assertIn("compacted", msg)
    self.assertEqual(self.last_log()["level"], "late")
    self.assertEqual(self.last_log()["ckpt"], 4)

  def test_first_compaction_omits_checkpoint_index(self):
    self.mock_rpc.return_value = gm_response(40000, ckpt=None)
    self.assertIn("compacted", self.message(self.run_guard(inv=0)))
    self.assertEqual((self.last_log()["level"], self.last_log()["ckpt"]), ("late", 0))

  def test_rpc_failure_estimate_reminds_but_never_forces_handoff(self):
    self.mock_rpc.side_effect = RuntimeError("LS down")
    with open(self.transcript, "w") as f:
      f.write("x" * 450_000)
    msg = self.message(self.run_guard(inv=0, steps=100))
    log = self.last_log()
    self.assertTrue(log["fallback"])
    self.assertIn("LS down", log["err"])
    self.assertEqual(log["tokens"], 225_000)
    self.assertIn("estimated from transcript size", msg)
    self.assertEqual(log["level"], "soft")
    self.assertFalse(self.state().get("pending_handoff_launch"))

  def test_in_project_flow_uses_roadmap_and_state_file(self):
    self.mock_rpc.return_value = gm_response(210000)
    msg = self.message(self.run_guard(inv=0))
    state_file = os.path.join(self.tmp.name, "roadmaps", "proj-1", "state", "c1.md")
    self.assertIn("roadmap.py set c1", msg)
    self.assertIn(state_file, msg)
    self.assertNotIn("handoff_summary", msg)
    self.assertEqual(self.state()["pending_handoff_file"], state_file)
    self.assertTrue(self.state()["pending_in_project"])

  def test_outside_project_flow_keeps_handoff_summary(self):
    self.mock_db.return_value = ("[10:00] ⦿ Task", "outside-of-project")
    self.mock_rpc.return_value = gm_response(210000)
    msg = self.message(self.run_guard(inv=0))
    self.assertIn(os.path.join(self.artifact_dir, "handoff_summary_Task_c1.md"), msg)
    self.assertNotIn("roadmap.py", msg)
    self.assertFalse(self.state()["pending_in_project"])

  def test_internal_error_prints_empty_json_and_logs(self):
    with patch.object(context_guard, "measure_context", side_effect=KeyError("boom")):
      self.assertEqual(self.run_guard(inv=1), {})
    self.assertEqual(self.last_log()["level"], "error")

  def test_subagents_are_skipped_without_logging(self):
    self.assertEqual(self.run_guard(inv=1, parentConversationId="p"), {})
    self.assertFalse(os.path.exists(self.log_path))
    self.mock_rpc.assert_not_called()

  def test_generator_metadata_offset_follows_previous_count(self):
    resp = gm_response(1000)
    resp["generatorMetadata"] = resp["generatorMetadata"] * 5
    self.mock_rpc.return_value = resp
    self.run_guard(inv=1)
    self.assertEqual(self.state()["gm_count"], 5)
    self.run_guard(inv=2)
    self.assertEqual(self.mock_rpc.call_args[0][1]["generatorMetadataOffset"], 4)


if __name__ == "__main__":
  unittest.main()
