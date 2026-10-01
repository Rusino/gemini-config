#!/usr/bin/env python3
"""Tests for context_guard.py."""

import io
import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks")))

import context_guard


class TestContextGuard(unittest.TestCase):

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

  def test_reopen_on_new_turn(self):
    # Simulates a user starting a new turn in a finalized conversation
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
         patch.object(context_guard, "get_transcript_size_kb", return_value=10.0):
      context_guard.main()
      mock_reopen.assert_called_once_with("c1")

  def test_no_reopen_when_user_asks_question_only(self):
    # User explicitly states it's just a question / do not change status
    data = {
        "conversationId": "c1",
        "invocationNum": 0,
        "initialNumSteps": 10,
        "lastUserInput": "Это просто вопрос, не меняй статус: где лежит конфиг?",
    }
    with patch("sys.stdin", io.StringIO(json.dumps(data))), \
         patch("sys.stdout", new_callable=io.StringIO) as mock_out, \
         patch.object(context_guard, "get_conversation_db_info", return_value=("[10:00] «» Some Task", "proj")), \
         patch("chat_lifecycle.reopen_conversation") as mock_reopen, \
         patch.object(context_guard, "get_transcript_size_kb", return_value=10.0):
      context_guard.main()
      mock_reopen.assert_not_called()


if __name__ == "__main__":
  unittest.main()
