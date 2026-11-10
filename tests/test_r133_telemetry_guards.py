#!/usr/bin/env python3
"""Contract tests for R133 telemetry-efficiency hook guards."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(
    0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
)

import clickable_links_guard
import context_guard
import pre_tool_guard
import stop_guard


class TestR133TelemetryGuards(unittest.TestCase):

  def _run_pre_tool(self, payload: dict) -> dict:
    with (
        patch("sys.stdin", io.StringIO(json.dumps(payload))),
        patch("sys.stdout", new_callable=io.StringIO) as out,
    ):
      pre_tool_guard.main()
    return json.loads(out.getvalue())

  def _write_jsonl(self, path: str, steps: list[dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s, ensure_ascii=False) + "\n")

  # Verifies intermediate WaitMsBeforeAsync (1000..9999) and bare 500ms calls are blocked
  def test_run_command_wait_ms_guard(self) -> None:
    for bad_ms in (1000, 3000, 5000, 8000, 9999, "5000", 250):
      res = self._run_pre_tool({
          "conversationId": "c-wait",
          "toolCall": {
              "name": "run_command",
              "args": {"CommandLine": "echo hi", "WaitMsBeforeAsync": bad_ms},
          },
      })
      self.assertEqual(res.get("decision"), "deny", f"Expected deny for {bad_ms}")
      self.assertIn("[WAIT-MS GUARD]", res.get("reason", ""))

    # Non-daemon 500ms without NotificationTimeoutSeconds must be blocked
    res_no_notif = self._run_pre_tool({
        "conversationId": "c-wait",
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "ninja -C out/Release", "WaitMsBeforeAsync": 500},
        },
    })
    self.assertEqual(res_no_notif.get("decision"), "deny")
    self.assertIn("NotificationTimeoutSeconds", res_no_notif.get("reason", ""))

    # 10000ms sync, 500ms + NotificationTimeoutSeconds, and 500ms IsDaemon=True must pass
    for good_args in (
        {"CommandLine": "echo sync", "WaitMsBeforeAsync": 10000},
        {"CommandLine": "echo sync_str", "WaitMsBeforeAsync": "10000"},
        {
            "CommandLine": "ninja -C out/Release dm",
            "WaitMsBeforeAsync": 500,
            "NotificationTimeoutSeconds": 600,
        },
        {
            "CommandLine": "python3 -m http.server 8123",
            "WaitMsBeforeAsync": 500,
            "IsDaemon": True,
        },
    ):
      res_ok = self._run_pre_tool({
          "conversationId": "c-wait",
          "toolCall": {"name": "run_command", "args": good_args},
      })
      self.assertEqual(res_ok.get("decision"), "allow", f"Unexpected deny: {res_ok}")

  # Detached jobs inside a synchronous run_command outlive the call with no wakeup; redirections,
  # `&&`, quoted or heredoc `&`, and `&` jobs joined by `wait` stay synchronous and must pass
  def test_sync_run_command_blocks_detached_jobs(self) -> None:
    def decide(cmd: str, **extra) -> dict:
      args = {"CommandLine": cmd, "WaitMsBeforeAsync": 10000, **extra}
      return self._run_pre_tool({
          "conversationId": "c-detach",
          "toolCall": {"name": "run_command", "args": args},
      })

    for bad in (
        "nohup ./run_queue.sh > q.log 2>&1 &",
        "ssh host 'cd x && nohup ./r.sh >/dev/null 2>&1 &'",
        "./runner.sh & disown",
        "setsid ./runner.sh >/dev/null 2>&1 < /dev/null",
        "./build.sh > b.log 2>&1 &\necho started",
        "sleep 100 & echo $!",
    ):
      res = decide(bad)
      self.assertEqual(res.get("decision"), "deny", f"Expected deny for {bad!r}")
      self.assertIn("[DETACHED-JOB GUARD]", res.get("reason", ""))

    for good in (
        "make && make test 2>&1 | tail -5",
        "cmd >/dev/null 2>&1; echo $?",
        "curl -s 'https://x.test/?a=1&b=2'",
        'echo "a & b"',
        "python3 - <<'EOF'\nx = 1 & 3\nEOF",
        "a.sh & b.sh & wait",
        "cmd |& tee log",
        "echo nohup-free &>/dev/null",
        "git commit -m 'deny nohup, setsid or a lone & job'",
    ):
      res = decide(good)
      self.assertEqual(res.get("decision"), "allow", f"Unexpected deny for {good!r}: {res}")

    # The 500 ms + notification path is the sanctioned way to start long jobs
    res_bg = decide("nohup ./run_queue.sh &", WaitMsBeforeAsync=500, NotificationTimeoutSeconds=600)
    self.assertEqual(res_bg.get("decision"), "allow")

  # Verifies at most 1 status/polling check is allowed per window until a reactive wakeup arrives
  def test_manual_polling_loop_guard_across_manage_task_subagents_and_schedule(self) -> None:
    with tempfile.TemporaryDirectory() as tmp_dir:
      tx = os.path.join(tmp_dir, "transcript.jsonl")
      base_steps = [
          {"step_index": 0, "type": "USER_INPUT", "content": "Run corpus"},
          {
              "step_index": 1,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "run_command",
                  "args": {
                      "CommandLine": "ninja",
                      "WaitMsBeforeAsync": 500,
                      "NotificationTimeoutSeconds": 600,
                  },
              }],
          },
          {
              "step_index": 2,
              "type": "GENERIC",
              "status": "RUNNING",
              "content": "Tool is running as a background task with task id: c1/task-7",
          },
      ]
      self._write_jsonl(tx, base_steps)

      # First status check in the window is allowed
      first = self._run_pre_tool({
          "conversationId": "c1",
          "transcriptPath": tx,
          "stepIdx": 4,
          "toolCall": {
              "name": "manage_task",
              "args": {"Action": "status", "TaskId": "c1/task-7"},
          },
      })
      self.assertEqual(first.get("decision"), "allow")

      # Record that first status check; repeated polling via manage_task, manage_subagents,
      # or one-shot schedule must all be blocked while kill remains allowed
      polled_steps = base_steps + [
          {
              "step_index": 3,
              "type": "PLANNER_RESPONSE",
              "tool_calls": [{
                  "name": "manage_task",
                  "args": {"Action": "status", "TaskId": "c1/task-7"},
              }],
          },
          {"step_index": 4, "type": "GENERIC", "content": "Task c1/task-7 is running"},
      ]
      self._write_jsonl(tx, polled_steps)

      for blocked_call in (
          {"name": "manage_task", "args": {"Action": "status", "TaskId": "c1/task-7"}},
          {"name": "manage_task", "args": {"Action": "list"}},
          {"name": "manage_subagents", "args": {"Action": "list"}},
          {
              "name": "schedule",
              "args": {"DurationSeconds": 60, "Prompt": "Check task progress"},
          },
      ):
        out = self._run_pre_tool({
            "conversationId": "c1",
            "transcriptPath": tx,
            "stepIdx": 6,
            "toolCall": blocked_call,
        })
        self.assertEqual(out.get("decision"), "deny", f"Expected deny for {blocked_call}")
        self.assertIn("[POLLING GUARD]", out.get("reason", ""))
        self.assertIn("0 tools", out.get("reason", ""))

      # Non-polling kill and recurring CronExpression schedules remain allowed
      self.assertEqual(
          self._run_pre_tool({
              "conversationId": "c1",
              "transcriptPath": tx,
              "stepIdx": 6,
              "toolCall": {
                  "name": "manage_task",
                  "args": {"Action": "kill", "TaskId": "c1/task-7"},
              },
          }).get("decision"),
          "allow",
      )
      self.assertEqual(
          self._run_pre_tool({
              "conversationId": "c1",
              "transcriptPath": tx,
              "stepIdx": 6,
              "toolCall": {
                  "name": "schedule",
                  "args": {"CronExpression": "*/5 * * * *", "Prompt": "Tick"},
              },
          }).get("decision"),
          "allow",
      )

      # One-shot timer SYSTEM_MESSAGE wakeup does NOT reset the polling budget
      timer_wakeup_steps = polled_steps + [{
          "step_index": 5,
          "type": "SYSTEM_MESSAGE",
          "content": (
              "<SYSTEM_MESSAGE>\n[Message] timestamp=2026-11-08T23:00:00Z "
              "sender=c1/task-9 priority=MESSAGE_PRIORITY_HIGH "
              "content=Check progress of current leg\n</SYSTEM_MESSAGE>"
          ),
      }]
      self._write_jsonl(tx, timer_wakeup_steps)
      self.assertEqual(
          self._run_pre_tool({
              "conversationId": "c1",
              "transcriptPath": tx,
              "stepIdx": 7,
              "toolCall": {
                  "name": "schedule",
                  "args": {"DurationSeconds": 60, "Prompt": "Check progress again"},
              },
          }).get("decision"),
          "deny",
      )

      # Reactive task completion SYSTEM_MESSAGE resets the window for a fresh status check
      reactive_steps = timer_wakeup_steps + [{
          "step_index": 6,
          "type": "SYSTEM_MESSAGE",
          "content": (
              '<SYSTEM_MESSAGE>\n[Message] sender=c1/task-7 content=Task id "c1/task-7" '
              "finished with result:\nCommand exited with code 0.\n</SYSTEM_MESSAGE>"
          ),
      }]
      self._write_jsonl(tx, reactive_steps)
      self.assertEqual(
          self._run_pre_tool({
              "conversationId": "c1",
              "transcriptPath": tx,
              "stepIdx": 8,
              "toolCall": {"name": "manage_task", "args": {"Action": "list"}},
          }).get("decision"),
          "allow",
      )

  # Verifies subagent conversations warn at 45 planner steps and hard-block non-send_message at 60
  def test_subagent_step_budget_warning_at_45_and_hard_block_at_60(self) -> None:
    parent_uuid = "165db142-d402-4898-928f-1c8bf2bce80c"
    sub_uuid = "b16b1032-e263-4eb9-80d7-6e1e6b297e87"

    def build_sub_steps(n_planner: int) -> list[dict]:
      steps: list[dict] = [{
          "step_index": 0,
          "source": "SYSTEM",
          "type": "SYSTEM_MESSAGE",
          "content": (
              f"[Message] timestamp=2026-11-08T00:00:00Z sender={parent_uuid} "
              "priority=MESSAGE_PRIORITY_HIGH content=Bisect regression"
          ),
      }]
      for i in range(n_planner):
        steps.append({
            "step_index": 2 * i + 1,
            "type": "PLANNER_RESPONSE",
            "content": f"step {i}",
        })
      return steps

    with tempfile.TemporaryDirectory() as tmp_dir:
      tx = os.path.join(tmp_dir, "transcript.jsonl")
      log_file = os.path.join(tmp_dir, "cg.jsonl")

      with patch.dict(os.environ, {"JETSKI_CONTEXT_GUARD_LOG": log_file}):
        # 44 planner steps: silent skip
        self._write_jsonl(tx, build_sub_steps(44))
        log44: dict = {}
        out44 = context_guard.run(
            {"conversationId": sub_uuid, "transcriptPath": tx}, log44
        )
        self.assertEqual(out44, {})
        self.assertEqual(log44.get("level"), "skip")

        # 45 planner steps: context_guard injects wrap-up warning; tools still allowed (< 60)
        self._write_jsonl(tx, build_sub_steps(45))
        log45: dict = {}
        out45 = context_guard.run(
            {"conversationId": sub_uuid, "transcriptPath": tx}, log45
        )
        msg45 = "\n".join(s["ephemeralMessage"] for s in out45.get("injectSteps", []))
        self.assertEqual(log45.get("level"), "subagent_warn")
        self.assertIn("[SUBAGENT STEP BUDGET WARNING]", msg45)
        self.assertIn(parent_uuid, msg45)
        self.assertEqual(
            self._run_pre_tool({
                "conversationId": sub_uuid,
                "transcriptPath": tx,
                "toolCall": {
                    "name": "run_command",
                    "args": {"CommandLine": "echo check", "WaitMsBeforeAsync": 10000},
                },
            }).get("decision"),
            "allow",
        )

        # 60 planner steps: pre_tool_guard hard-blocks all tools except send_message
        self._write_jsonl(tx, build_sub_steps(60))
        log60: dict = {}
        out60 = context_guard.run(
            {"conversationId": sub_uuid, "transcriptPath": tx}, log60
        )
        msg60 = "\n".join(s["ephemeralMessage"] for s in out60.get("injectSteps", []))
        self.assertEqual(log60.get("level"), "subagent_hard")
        self.assertIn("[SUBAGENT STEP BUDGET EXCEEDED]", msg60)

        for blocked_tool in (
            {
                "name": "run_command",
                "args": {"CommandLine": "echo no", "WaitMsBeforeAsync": 10000},
            },
            {"name": "read_file", "args": {"Path": "/tmp/a.txt"}},
            {"name": "search_web", "args": {"query": "skia"}},
        ):
          denied = self._run_pre_tool({
              "conversationId": sub_uuid,
              "transcriptPath": tx,
              "toolCall": blocked_tool,
          })
          self.assertEqual(denied.get("decision"), "deny")
          self.assertIn("[SUBAGENT STEP BUDGET GUARD]", denied.get("reason", ""))

        allowed_send = self._run_pre_tool({
            "conversationId": sub_uuid,
            "transcriptPath": tx,
            "toolCall": {
                "name": "send_message",
                "args": {"Recipient": parent_uuid, "Message": "Partial findings"},
            },
        })
        self.assertEqual(allowed_send.get("decision"), "allow")

  # Verifies mechanical/lookup/skeptic subagents require explicit Model='flash'
  def test_subagent_model_inheritance_hygiene(self) -> None:
    for sub_type in ("research-google", "jetski-guide", "keeper_skeptic", "scout"):
      for bad_args in (
          {"TypeName": sub_type, "Role": "Codebase Researcher", "Prompt": "Find X"},
          {
              "TypeName": sub_type,
              "Model": "inherit",
              "Role": "Codebase Researcher",
              "Prompt": "Find X",
          },
      ):
        out = self._run_pre_tool({
            "conversationId": "c-sub",
            "toolCall": {"name": "invoke_subagent", "args": bad_args},
        })
        self.assertEqual(out.get("decision"), "deny", f"Expected deny for {bad_args}")
        self.assertIn("[SUBAGENT-MODEL GUARD]", out.get("reason", ""))
        self.assertIn('Model="flash"', out.get("reason", ""))

      ok = self._run_pre_tool({
          "conversationId": "c-sub",
          "toolCall": {
              "name": "invoke_subagent",
              "args": {
                  "TypeName": sub_type,
                  "Model": "flash",
                  "Role": "Codebase Researcher",
                  "Prompt": "Find X",
              },
          },
      })
      self.assertEqual(ok.get("decision"), "allow")

  # Verifies relaxed clickable links allow prose back-references while gating tables/lists/cl/<N>/hex IDs
  def test_relaxed_clickable_links_guard_prose_reuse_vs_tables_lists_and_bans(self) -> None:
    prose_reuse = (
        "Я проверила [Skia CL 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1), "
        "[PR #194980](https://github.com/flutter/flutter/pull/194980), "
        "[b/580852205](https://b.corp.google.com/issues/580852205) и сборку "
        "[Canary-G3](https://ci.chromium.org/b/8666879335458403073).\n\n"
        "Во втором абзаце Skia CL 1414016 на PS1, PR #194980, b/580852205 и "
        "build 8666879335458403073 упомянуты повторно без дублирования URL."
    )
    self.assertEqual(clickable_links_guard.find_unlinked_entities(prose_reuse), [])

    # Table row without a local link must still block even when linked in prose above
    unlinked_table = (
        "Сводка по [Skia CL 1414016](https://skia-review.googlesource.com/c/skia/+/1414016):\n\n"
        "| CL | Статус |\n"
        "| :--- | :--- |\n"
        "| Skia CL 1414016 | LGTM |\n"
    )
    self.assertIn(
        "CL 1414016", clickable_links_guard.find_unlinked_entities(unlinked_table)
    )

    # List item without a local link must still block even when linked in prose above
    unlinked_list = (
        "Сводка по [Skia CL 1414016](https://skia-review.googlesource.com/c/skia/+/1414016):\n\n"
        "- Skia CL 1414016 готов к отправке.\n"
    )
    self.assertIn(
        "CL 1414016", clickable_links_guard.find_unlinked_entities(unlinked_list)
    )

    # Linked table row and linked list item repeating the same CL inside the row/item pass
    linked_table_and_list = (
        "| CL | Примечание |\n"
        "| :--- | :--- |\n"
        "| [Skia CL 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1) | PS1 зелёный |\n\n"
        "- [Skia CL 1414016 PS1](https://skia-review.googlesource.com/c/skia/+/1414016/1): "
        "в CL 1414016 на PS1 замечаний нет.\n"
    )
    self.assertEqual(
        clickable_links_guard.find_unlinked_entities(linked_table_and_list), []
    )

    # Bare cl/<5-8 digits> and bare xxxxxxxx_xxxxxxxx comment IDs remain banned everywhere
    banned_cl_shortlink = (
        "Проверила [Skia CL 1414016](https://skia-review.googlesource.com/c/skia/+/1414016).\n\n"
        "После ребейза залью в cl/1414016."
    )
    self.assertEqual(
        clickable_links_guard.find_unlinked_entities(banned_cl_shortlink),
        ["cl/1414016"],
    )

    banned_comment_hex = (
        "Черновик [`77640dee_0908b885`](https://skia-review.googlesource.com/c/skia/+/1414016/1).\n\n"
        "Второй абзац упоминает `77640dee_0908b885` без ссылки."
    )
    self.assertEqual(
        clickable_links_guard.find_unlinked_entities(banned_comment_hex),
        ["77640dee_0908b885"],
    )

    # Compact links postscript after [CLICKABLE LINKS GUARD] must not trigger final_context_preservation
    with tempfile.TemporaryDirectory() as tmp_dir:
      tx = os.path.join(tmp_dir, "transcript.jsonl")
      self._write_jsonl(tx, [
          {"step_index": 1, "type": "USER_INPUT", "content": "Статус"},
          {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "А" * 600},
          {
              "step_index": 3,
              "type": "SYSTEM_MESSAGE",
              "content": "Stop hook blocked termination: [CLICKABLE LINKS GUARD] ...",
          },
          {
              "step_index": 4,
              "type": "PLANNER_RESPONSE",
              "content": (
                  "**Ссылки:** [Skia CL 1414016]"
                  "(https://skia-review.googlesource.com/c/skia/+/1414016)"
              ),
          },
      ])
      info = stop_guard.analyze_current_turn(tx)
      self.assertTrue(info.get("is_links_postscript_step"))
      self.assertEqual(stop_guard.final_context_preservation_guard_reason(info), "")


if __name__ == "__main__":
  unittest.main()
