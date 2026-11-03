#!/usr/bin/env python3
"""Tests for ask_question_context_denial in pre_tool_guard.py."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
)

import pre_tool_guard

HOOK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "hooks", "run_pre_tool_guard.py")
)


class TestPreToolGuardAskQuestion(unittest.TestCase):

  @staticmethod
  def _encode_proto_varint(val: int) -> bytes:
    out = bytearray()
    for _ in range(10):
      if val < 0x80:
        out.append(val)
        return bytes(out)
      out.append((val & 0x7F) | 0x80)
      val >>= 7
    out.append(val)
    return bytes(out)

  @classmethod
  def _build_planner_step_blob(cls, response: str, thinking: str = "") -> bytes:
    pr = bytearray()
    if response:
      rb = response.encode("utf-8")
      pr.append(0x0A)
      pr.extend(cls._encode_proto_varint(len(rb)))
      pr.extend(rb)
    if thinking:
      tb = thinking.encode("utf-8")
      pr.append(0x1A)
      pr.extend(cls._encode_proto_varint(len(tb)))
      pr.extend(tb)
    step = bytearray([0x08, 0x0F, 0x20, 0x02, 0x2A, 0x02, 0x08, 0x01, 0xA2, 0x01])
    step.extend(cls._encode_proto_varint(len(pr)))
    step.extend(pr)
    return bytes(step)

  def test_hooks_json_matches_ask_question(self) -> None:
    hooks_json = os.path.join(os.path.dirname(HOOK), "..", "hooks.json")
    with open(hooks_json, encoding="utf-8") as hf:
      cfg = json.load(hf)
    matchers = [
        entry.get("matcher", "")
        for spec in cfg.values()
        for entry in spec.get("PreToolUse", [])
    ]
    self.assertTrue(
        any(re.search(m, "ask_question") for m in matchers if m),
        f"hooks.json PreToolUse matcher must cover ask_question, got {matchers}",
    )

  def test_blocks_empty_visible_content_incident(self) -> None:
    choice_args = {
        "questions": [{
            "question": "Какой вариант выбираем для реализации?",
            "options": [
                "(Recommended) Перевести git cl на luci-auth context",
                "Перевести только git cl try на luci-auth context",
                "Оставить CorpSSO как есть с предупреждением за 4ч",
                "Ничего не менять (Status Quo)",
            ],
        }]
    }
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as tf:
      steps = [
          {"step_index": 0, "type": "USER_INPUT", "content": "Проверь gcert"},
          {"step_index": 101, "type": "GENERIC", "content": "No background tasks."},
          {
              "step_index": 102,
              "type": "PLANNER_RESPONSE",
              "content": "",
              "thinking": "Drafted full options comparison inside thinking only.",
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          },
      ]
      for st in steps:
        tf.write(json.dumps(st, ensure_ascii=False) + "\n")
      tf.flush()

      proc = subprocess.run(
          [sys.executable, HOOK],
          input=json.dumps({
              "conversationId": "c-522d0e70",
              "transcriptPath": tf.name,
              "stepIdx": 103,
              "toolCall": {"name": "ask_question", "args": choice_args},
          }),
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(proc.returncode, 0, proc.stderr)
      res = json.loads(proc.stdout)
      self.assertEqual(res["decision"], "deny")
      self.assertIn("[ASK_QUESTION CONTEXT GUARD]", res["reason"])
      self.assertIn("thinking", res["reason"])

  def test_allows_same_step_and_stop_hook_recovery(self) -> None:
    choice_args = {
        "questions": [{
            "question": "Какой вариант выбираем?",
            "options": [
                "(Recommended) Вариант 1: luci-auth context",
                "Вариант 2: только git cl try",
                "Вариант 3: Status Quo",
            ],
        }]
    }
    valid_breakdown = (
        "Разобрала оба механизма авторизации и три варианта:\n\n"
        "- **Вариант 1 (Recommended)**: переводит `git cl` на 7-дневный `LOAS2`.\n"
        "- **Вариант 2**: чинит только `git cl try`, но оставляет `upload` на `CorpSSO`.\n"
        "- **Вариант 3**: ничего не меняет, оставляет ежедневный `glogin`."
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
      tp = os.path.join(tmp_dir, "transcript.jsonl")
      tfp = os.path.join(tmp_dir, "transcript_full.jsonl")
      # Compact transcript truncates content while transcript_full preserves it
      with open(tp, "w", encoding="utf-8") as f_short:
        f_short.write(
            json.dumps({
                "step_index": 10,
                "type": "PLANNER_RESPONSE",
                "content": "Разобрала...",
                "tool_calls": [
                    {"name": "manage_task", "args": {"Action": "list"}},
                    {"name": "ask_question", "args": choice_args},
                ],
                "truncated_fields": ["content"],
            })
            + "\n"
            + json.dumps({
                "step_index": 11,
                "type": "GENERIC",
                "content": "No background tasks are currently running.",
            })
            + "\n"
        )
      with open(tfp, "w", encoding="utf-8") as f_full:
        f_full.write(
            json.dumps({
                "step_index": 10,
                "type": "PLANNER_RESPONSE",
                "content": valid_breakdown,
                "tool_calls": [
                    {"name": "manage_task", "args": {"Action": "list"}},
                    {"name": "ask_question", "args": choice_args},
                ],
            })
            + "\n"
            + json.dumps({
                "step_index": 11,
                "type": "GENERIC",
                "content": "No background tasks are currently running.",
            })
            + "\n"
        )
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(choice_args, "c-ok", tp, 12),
          "",
      )

      # Sibling non-ask_question tool denied by another guard must not hide same-step prose
      with open(tfp, "w", encoding="utf-8") as f_sib_deny:
        f_sib_deny.write(
            json.dumps({
                "step_index": 10,
                "type": "PLANNER_RESPONSE",
                "content": valid_breakdown,
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "git status"}},
                    {"name": "ask_question", "args": choice_args},
                ],
            })
            + "\n"
            + json.dumps({
                "step_index": 11,
                "type": "GENERIC",
                "status": "ERROR",
                "content": (
                    "tool call denied by pre-tool hook: [WORKTREE LEASE GUARD]"
                ),
            })
            + "\n"
        )
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(choice_args, "c-sib", tp, 12),
          "",
      )

      # Two-step recovery after stop_guard [ASK_QUESTION GUARD] must also pass
      recovery_steps = [
          {"step_index": 20, "type": "USER_INPUT", "content": "Что думаешь?"},
          {
              "step_index": 21,
              "type": "PLANNER_RESPONSE",
              "content": valid_breakdown,
              "tool_calls": [],
          },
          {
              "step_index": 22,
              "type": "SYSTEM_MESSAGE",
              "content": (
                  "stop hook blocked termination due to reason: "
                  "[ASK_QUESTION GUARD] Call the ask_question tool."
              ),
          },
          {
              "step_index": 23,
              "type": "EPHEMERAL_MESSAGE",
              "content": "[CONTEXT GUARD: SOFT LIMIT] Context is at 62%.",
          },
          {
              "step_index": 24,
              "type": "PLANNER_RESPONSE",
              "content": "",
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          },
      ]
      with open(tfp, "w", encoding="utf-8") as f_rec:
        for st in recovery_steps:
          f_rec.write(json.dumps(st, ensure_ascii=False) + "\n")
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(choice_args, "c-rec", tp, 25),
          "",
      )

  def test_blocks_stale_text_stubs_and_answered_boundaries(self) -> None:
    choice_args = {
        "questions": [{
            "question": "Куда сохранять черновик?",
            "options": [
                "(Recommended) Хранить как unpublished draft на Gerrit",
                "Хранить только локально во Flight Deck",
            ],
        }]
    }
    long_breakdown = (
        "Сравнение двух мест хранения черновика ревью:\n\n"
        "- **Gerrit unpublished draft (Recommended)**: сразу виден в UI Gerrit.\n"
        "- **Только локально во Flight Deck**: требует ручного копирования в Gerrit."
    )
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as tf:
      # Stale text before a tool execution must not satisfy a later empty ask_question
      stale_steps = [
          {"step_index": 1, "type": "USER_INPUT", "content": "Проверь"},
          {
              "step_index": 2,
              "type": "PLANNER_RESPONSE",
              "content": long_breakdown,
              "tool_calls": [{"name": "run_command", "args": {"CommandLine": "ls"}}],
          },
          {"step_index": 3, "type": "GENERIC", "content": "Command exited with code 0."},
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": "",
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          },
      ]
      for st in stale_steps:
        tf.write(json.dumps(st, ensure_ascii=False) + "\n")
      tf.flush()
      self.assertIn(
          "[ASK_QUESTION CONTEXT GUARD]",
          pre_tool_guard.ask_question_context_denial(choice_args, "c-stale", tf.name, 5),
      )

      # Stub 1-liner or <thought>-only content must also be blocked on substantive choices
      tf.seek(0)
      tf.truncate()
      tf.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "content": "<thought>" + long_breakdown + "</thought>Выбери вариант ниже:",
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          })
          + "\n"
      )
      tf.flush()
      denial_stub = pre_tool_guard.ask_question_context_denial(
          choice_args, "c-stub", tf.name, 2
      )
      self.assertIn("need >= 120 chars across >= 2 lines", denial_stub)

      # Answered A1: boundary resets prior context for a second ask_question in the same turn
      tf.seek(0)
      tf.truncate()
      for st in [
          {
              "step_index": 10,
              "type": "PLANNER_RESPONSE",
              "content": long_breakdown,
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          },
          {
              "step_index": 11,
              "type": "GENERIC",
              "content": "Created At: 2026-11-03T16:00:00Z\nA1: Хранить локально",
          },
          {
              "step_index": 12,
              "type": "PLANNER_RESPONSE",
              "content": "",
              "tool_calls": [{"name": "ask_question", "args": choice_args}],
          },
      ]:
        tf.write(json.dumps(st, ensure_ascii=False) + "\n")
      tf.flush()
      self.assertIn(
          "[ASK_QUESTION CONTEXT GUARD]",
          pre_tool_guard.ask_question_context_denial(choice_args, "c-a1", tf.name, 13),
      )

  def test_procedural_cruxy_and_loop_cap(self) -> None:
    vcs_args = {
        "questions": [{
            "question": "Commit and upload changes?",
            "options": ["(Recommended) Commit + push", "Commit only", "Not now"],
        }]
    }
    cruxy_args = {
        "questions": [{
            "question": "Which starter kit should this project use?",
            "options": [
                "(Recommended) Search Vibes: search results (search_vibes)",
                "Blank Canvas: start from scratch (material_genux_kit)",
            ],
        }]
    }
    with tempfile.NamedTemporaryFile("w+", suffix=".jsonl") as tf:
      tf.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "go"}) + "\n")
      tf.flush()
      # cruxy-new starter picker is explicitly exempted by SKILL.md
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(cruxy_args, "c-cruxy", tf.name, 1),
          "",
      )
      # Empty procedural VCS confirmation is still blocked
      self.assertIn(
          "[ASK_QUESTION CONTEXT GUARD]",
          pre_tool_guard.ask_question_context_denial(vcs_args, "c-vcs0", tf.name, 1),
      )
      # Concise 1-sentence summary satisfies procedural VCS confirmation
      tf.write(
          json.dumps({
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "content": "Правка и контрактный тест готовы, обе проверки прошли чисто.",
              "tool_calls": [{"name": "ask_question", "args": vcs_args}],
          })
          + "\n"
      )
      tf.flush()
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(vcs_args, "c-vcs1", tf.name, 2),
          "",
      )

      # Consecutive denial cap guarantees loop termination after 3 denials
      tf.seek(0)
      tf.truncate()
      tf.write(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "go"}) + "\n")
      for idx in range(1, 7, 2):
        tf.write(
            json.dumps({
                "step_index": idx,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "ask_question", "args": vcs_args}],
            })
            + "\n"
            + json.dumps({
                "step_index": idx + 1,
                "type": "GENERIC",
                "status": "ERROR",
                "content": "tool call denied by pre-tool hook: [ASK_QUESTION CONTEXT GUARD]",
            })
            + "\n"
        )
      tf.write(
          json.dumps({
              "step_index": 7,
              "type": "PLANNER_RESPONSE",
              "content": "",
              "tool_calls": [{"name": "ask_question", "args": vcs_args}],
          })
          + "\n"
      )
      tf.flush()
      self.assertEqual(
          pre_tool_guard.ask_question_context_denial(vcs_args, "c-cap", tf.name, 8),
          "",
      )

  def test_reads_live_sqlite_generating_planner_step(self) -> None:
    choice_args = {
        "questions": [{
            "question": "Какой режим выбираем?",
            "options": [
                "(Recommended) Режим A: авто-сброс утром",
                "Режим B: плашка с подтверждением",
                "Режим C: Status Quo",
            ],
        }]
    }
    valid_breakdown = (
        "Разбор трёх режимов утреннего сброса модели:\n\n"
        "- **Режим A (Recommended)**: автоматически возвращает дефолт после даунгрейда.\n"
        "- **Режим B**: показывает плашку, требуя лишний клик каждое утро.\n"
        "- **Режим C**: оставляет вчерашнюю младшую модель навсегда."
    )
    with tempfile.TemporaryDirectory() as tmp_dir:
      cid = "conv-live-sqlite"
      conv_dir = os.path.join(tmp_dir, "conversations")
      logs_dir = os.path.join(tmp_dir, "brain", cid, ".system_generated", "logs")
      os.makedirs(conv_dir, exist_ok=True)
      os.makedirs(logs_dir, exist_ok=True)
      tp = os.path.join(logs_dir, "transcript.jsonl")
      with open(tp, "w", encoding="utf-8") as tf:
        tf.write(
            json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "Проверь"})
            + "\n"
        )
      db_path = os.path.join(conv_dir, f"{cid}.db")
      conn = sqlite3.connect(db_path)
      conn.execute(
          "CREATE TABLE steps (idx integer PRIMARY KEY, step_type integer, "
          "status integer, step_format integer, step_payload blob)"
      )
      conn.execute(
          "INSERT INTO steps VALUES (?, 15, 2, 0, ?)",
          (1, self._build_planner_step_blob("", "Hidden thought only")),
      )
      conn.commit()

      with patch.dict(os.environ, {"JETSKI_CONVERSATIONS_DIR": conv_dir}):
        denied = pre_tool_guard.ask_question_context_denial(choice_args, cid, tp, 2)
        self.assertIn("[ASK_QUESTION CONTEXT GUARD]", denied)
        self.assertIn("thinking", denied)

        conn.execute(
            "UPDATE steps SET step_payload = ? WHERE idx = 1",
            (self._build_planner_step_blob(valid_breakdown, "Brief thought"),),
        )
        conn.commit()
        allowed = pre_tool_guard.ask_question_context_denial(choice_args, cid, tp, 2)
        self.assertEqual(allowed, "")
      conn.close()


if __name__ == "__main__":
  unittest.main()
