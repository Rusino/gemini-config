#!/usr/bin/env python3
"""Unit tests for R144 KV prompt-cache prefix stability across hooks, rules, and prompts"""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

CONFIG_ROOT = Path(__file__).resolve().parent.parent
HOOKS_DIR = CONFIG_ROOT / "hooks"
if str(HOOKS_DIR) not in sys.path:
  sys.path.insert(0, str(HOOKS_DIR))

import context_guard
import pre_tool_guard_notice


def _load_check_doc_grounding():
  script_path = (
      CONFIG_ROOT / "skills" / "amnesia-doc-check" / "scripts" / "check_doc_grounding.py"
  )
  spec = importlib.util.spec_from_file_location("check_doc_grounding_r144", script_path)
  assert spec is not None and spec.loader is not None
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


def _common_prefix_len(a: str, b: str) -> int:
  n = min(len(a), len(b))
  for idx in range(n):
    if a[idx] != b[idx]:
      return idx
  return n


class TestKvCachePrefixStability(unittest.TestCase):

  # Verifies that context_guard._inject orders static directives ahead of dynamic telemetry
  # and emits only tail ephemeralMessage steps so system/rule prefixes are never mutated
  def test_context_guard_orders_static_directives_before_dynamic_telemetry(self) -> None:
    dynamic_sync = "[GIT SYNC NOTICE] Workspace `/tmp/r` is 2 commits behind origin/main."
    dynamic_title = "[TITLE GUARD: CONCRETE TOPIC REQUIRED] Current chat title is generic."
    static_lang = context_guard.build_language_notice("ru")
    static_challenge = context_guard.build_critical_challenge_notice("что думаешь?")

    payload = context_guard._inject(
        dynamic_sync, static_lang, dynamic_title, static_challenge
    )
    self.assertEqual(set(payload.keys()), {"injectSteps"})
    msgs = [step["ephemeralMessage"] for step in payload["injectSteps"]]
    self.assertEqual(
        msgs,
        [static_lang, static_challenge, dynamic_sync, dynamic_title],
    )

  # Verifies that context_guard and pre_tool_guard_notice keep static instructions at the
  # message prefix and dynamic token counts/timestamps at the tail across invocations
  def test_context_guard_and_notice_keep_telemetry_at_message_tail(self) -> None:
    tmp = tempfile.mkdtemp()
    self.addCleanup(shutil.rmtree, tmp)
    marker_path = os.path.join(tmp, "marker.json")
    chat = {
        "conversationId": "conv-kv",
        "artifactDirectoryPath": os.path.join(tmp, "brain"),
    }
    with patch.dict(
        os.environ,
        {
            "JETSKI_PRE_TOOL_GUARD_MARKER": marker_path,
            "JETSKI_ROADMAPS_DIR": os.path.join(tmp, "roadmaps"),
        },
    ), patch.object(
        pre_tool_guard_notice, "APP_DATA_DIR_CANDIDATES", (tmp,), create=True
    ):
      pre_tool_guard_notice.notice(chat, 1000.0)
      with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(
            {"ts": 1001.0, "conv": "conv-kv", "error": "OSError: a", "where": "a.py:1"},
            f,
        )
      msg1 = pre_tool_guard_notice.notice(chat, 1002.0)["injectSteps"][0][
          "ephemeralMessage"
      ]
      later = 1002.0 + pre_tool_guard_notice.NOTICE_INTERVAL_S + 10.0
      with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(
            {"ts": later, "conv": "other-id", "error": "ValueError: b", "where": "b.py:9"},
            f,
        )
      msg2 = pre_tool_guard_notice.notice(chat, later + 1.0)["injectSteps"][0][
          "ephemeralMessage"
      ]

    self.assertGreaterEqual(_common_prefix_len(msg1, msg2), 250)
    self.assertTrue(msg1.endswith("OSError: a (a.py:1))"))
    self.assertTrue(msg2.endswith("ValueError: b (b.py:9))"))

    def run_soft(used_tokens: int, step_num: int) -> str:
      m_resp = {
          "generatorMetadata": [
              {
                  "chatModel": {
                      "chatStartMetadata": {
                          "contextWindowMetadata": {
                              "estimatedTokensUsed": used_tokens,
                              "maxContextTokens": 256000,
                          },
                          "checkpointIndex": -1,
                      }
                  }
              }
          ]
      }
      mock_cl = MagicMock()
      mock_cl.ls_rpc.return_value = m_resp
      mock_cl.clean_base_title.return_value = "Valid Multi Word Topic"
      mock_cl.is_valid_chat_topic.return_value = True
      mock_cl.has_lifecycle_title_prefix.return_value = True
      with patch.dict(sys.modules, {"chat_lifecycle": mock_cl}), patch.object(
          context_guard,
          "get_conversation_db_info",
          return_value=("[10:00] ⦿ Valid Multi Word Topic", "proj-kv"),
      ):
        out = context_guard.run(
            {
                "conversationId": "conv-kv-soft",
                "artifactDirectoryPath": os.path.join(tmp, f"soft_{step_num}"),
                "initialNumSteps": step_num,
                "invocationNum": 2,
            },
            {},
        )
      return out["injectSteps"][0]["ephemeralMessage"]

    soft_a = run_soft(160000, 100)
    soft_b = run_soft(175000, 140)
    self.assertGreaterEqual(_common_prefix_len(soft_a, soft_b), 450)

  # Verifies that all tracked always_on rules and subagent Prompt templates pass the
  # check_doc_grounding KV cache prefix audit while synthetic prefix violations fail
  def test_all_always_on_rules_and_subagent_templates_pass_kv_prefix_audit(self) -> None:
    cdg = _load_check_doc_grounding()
    targets = list((CONFIG_ROOT / "rules").glob("*.md"))
    targets.extend((CONFIG_ROOT / "plugins").glob("*/rules/*.md"))
    targets.extend([
        CONFIG_ROOT / "skills" / "amnesia-precommit" / "references" / "hunter_prompt.md",
        CONFIG_ROOT / "skills" / "amnesia-doc-check" / "SKILL.md",
        CONFIG_ROOT / "skills" / "ruthless-code-audit" / "SKILL.md",
    ])
    for fpath in targets:
      text = fpath.read_text(encoding="utf-8")
      warnings = cdg.check_rule_skill_hygiene(
          text.splitlines(), text, fpath=fpath, repos=[CONFIG_ROOT]
      )
      kv_warnings = [w for w in warnings if "KV" in w]
      self.assertEqual(kv_warnings, [], f"Unexpected KV prefix warning in {fpath}: {kv_warnings}")

    bad_prompt = (
        'invoke_subagent(\n    Prompt="""\n'
        "Intended Task Goal & Original User Constraints:\n<goal>\n\n"
        "Instructions:\n1. Check diff.\n"
        "---\nInvocation Payload:\n<tail>\n"
        '"""\n)\n'
    )
    bad_warnings = cdg.check_rule_skill_hygiene(
        bad_prompt.splitlines(), bad_prompt, fpath=None, repos=[CONFIG_ROOT]
    )
    self.assertTrue(
        any("before `Invocation Payload` tail" in w for w in bad_warnings),
        bad_warnings,
    )

    bad_rule = "---\ntrigger: always_on\n---\nCurrent time: $(date +%s)\n"
    rule_warnings = cdg.check_rule_skill_hygiene(
        bad_rule.splitlines(), bad_rule, fpath=None, repos=[CONFIG_ROOT]
    )
    self.assertTrue(
        any("dynamic timestamp token" in w for w in rule_warnings),
        rule_warnings,
    )


if __name__ == "__main__":
  unittest.main()
