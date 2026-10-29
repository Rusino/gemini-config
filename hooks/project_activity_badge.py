#!/usr/bin/env python3
"""Live project activity badge hook & watcher for Jetski / Antigravity sidebar.

When sidebar project folders are collapsed (`Group By: Project`), Jetski's
`GroupHeader` renders only `project.name` and the workspace path without any
running-chat indicator. Meanwhile, the Language Server runs an `fsnotify`
watcher on `~/.gemini/config/projects/*.json` and streams changes to the UI
via `ProjectUpdatesStream`.

This script reads the initial snapshot from the local Connect-RPC
`JetboxSubscribeToSummaries` stream (with fallback to `GetAllCascadeTrajectories`),
counts active (`CASCADE_RUN_STATUS_RUNNING` / `notFullyIdle`), blocked
(`waitingSteps`), and unread finished (`lastModifiedTime > lastUserViewTime`)
top-level conversations per project, and atomically updates the `"name"` field
in `~/.gemini/config/projects/<project_id>.json` with a compact suffix badge
(e.g. ` · ⟳ 1`, ` · ⚠ 1`, ` · ● 1`, or ` · ⟳ 1 ● 2`).

Modes of operation:
- Hook mode (`--event PreInvocation` / `--event Stop`):
  Reads the hook stdin payload, immediately outputs `{}` on stdout so the LLM
  turn is never delayed, and spawns a detached background process (`--bg-trigger`)
  that updates badges immediately and maintains a single `flock`-guarded polling
  watcher (`--watch`) until all conversations across all projects are idle and read.
- CLI mode (`--sync-once`, `--watch`, `--clear`, `--status`):
  Can be invoked manually or from tests to inspect or reset project badges.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
import fcntl
import glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from typing import Any
from urllib.parse import unquote
import urllib.request

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
DEFAULT_ANNOTATIONS_DIR = os.path.expanduser("~/.gemini/jetski/annotations")
DEFAULT_LS_ADDRESS = "localhost:5387"

# Matches one or more trailing activity badges such as:
#   " · ⟳ 1", " · ⚠ 2", " · ● 1", " · ⟳ 1 ⚠ 1 ● 2"
BADGE_RE = re.compile(
    r"(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$"
)

# Open leaf chats marked with ⏳ keep notFullyIdle=True between cron ticks, so
# only an actively executing turn (CASCADE_RUN_STATUS_RUNNING) counts as running
WAITING_TITLE_RE = re.compile(
    r"^\s*\[\d{1,2}:\d{2}\]\s*⏳(?:\s|$)"
)

WATCH_POLL_INTERVAL_SEC = 1.5
STOP_SETTLE_DELAY_SEC = 0.4
IDLE_POLLS_BEFORE_EXIT = 3
MAX_WATCH_DURATION_SEC = 86400


def _uid_suffix() -> str:
  try:
    return str(os.getuid())
  except AttributeError:
    return "default"


def _conn_cache_path() -> str:
  return f"/tmp/jetski_ls_conn_{_uid_suffix()}.json"


def _sync_lock_path() -> str:
  return f"/tmp/jetski_project_badge_sync_{_uid_suffix()}.lock"


def _watcher_lock_path() -> str:
  return f"/tmp/jetski_project_badge_watcher_{_uid_suffix()}.lock"


def _view_state_path() -> str:
  return f"/tmp/jetski_project_badge_view_{_uid_suffix()}.json"


def _traj_cache_path() -> str:
  return f"/tmp/jetski_project_badge_traj_{_uid_suffix()}.json"


# Status and step type values across Connect-JSON strings and numeric proto enums
WAITING_STEP_STATUSES = frozenset({"CORTEX_STEP_STATUS_WAITING", 9, "WAITING"})
DONE_STEP_STATUSES = frozenset({"CORTEX_STEP_STATUS_DONE", 3, "DONE"})
TERMINAL_STEP_STATUSES = frozenset({
    "CORTEX_STEP_STATUS_CANCELED",
    6,
    "CANCELED",
    "CORTEX_STEP_STATUS_ERROR",
    7,
    "ERROR",
    "CORTEX_STEP_STATUS_INTERRUPTED",
    12,
    "INTERRUPTED",
})
ACTIVE_RUN_STATUSES = frozenset({
    "CASCADE_RUN_STATUS_RUNNING",
    "CASCADE_RUN_STATUS_BUSY",
    "CASCADE_RUN_STATUS_CANCELING",
    2,
    3,
    4,
})
ERROR_TERM_REASONS = frozenset({"EXECUTOR_TERMINATION_REASON_ERROR", 1, "ERROR"})
CANCELED_TERM_REASONS = frozenset({
    "EXECUTOR_TERMINATION_REASON_USER_CANCELED",
    2,
    "USER_CANCELED",
})
USER_INPUT_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_USER_INPUT", 14})
PLANNER_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_PLANNER_RESPONSE", 15})
SYSTEM_MSG_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_SYSTEM_MESSAGE", 101})
INVOKE_SUBAGENT_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_INVOKE_SUBAGENT", 127})
ASK_QUESTION_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_ASK_QUESTION", 138})
GENERIC_STEP_TYPES = frozenset({"CORTEX_STEP_TYPE_GENERIC", 132})
# Post-response bookkeeping steps that appear after PLANNER_RESPONSE on finished turns
TRANSPARENT_TAIL_STEP_TYPES = frozenset({
    "CORTEX_STEP_TYPE_FINISH",
    2,
    "CORTEX_STEP_TYPE_CHECKPOINT",
    23,
    "CORTEX_STEP_TYPE_SUGGESTED_RESPONSES",
    27,
    "CORTEX_STEP_TYPE_MEMORY",
    38,
    "CORTEX_STEP_TYPE_EPHEMERAL_MESSAGE",
    90,
    "CORTEX_STEP_TYPE_CONVERSATION_HISTORY",
    98,
    "CORTEX_STEP_TYPE_KNOWLEDGE_ARTIFACTS",
    99,
    "CORTEX_STEP_TYPE_KNOWLEDGE_GENERATION",
    102,
    "CORTEX_STEP_TYPE_DIRECTORY_RULES",
    139,
})
TRAJ_CACHE_MAX_ENTRIES = 128
RECENT_TRAJ_CHECK_WINDOW_SEC = 21600.0
SILENT_SUBAGENT_GRACE_SEC = 30.0
STALE_IDLE_TURN_SEC = 900.0
ONGOING_BATTLE_END_TYPES = frozenset({0, "", None, "BATTLE_END_TYPE_UNSPECIFIED"})


def _step_oneof_payload(step: dict[str, Any], camel: str, snake: str) -> dict[str, Any] | None:
  if not isinstance(step, dict):
    return None
  direct = step.get(camel) or step.get(snake)
  if isinstance(direct, dict):
    return direct
  inner = step.get("step")
  if isinstance(inner, dict):
    if inner.get("case") in (camel, snake):
      val = inner.get("value")
      if isinstance(val, dict):
        return val
    direct_inner = inner.get(camel) or inner.get(snake)
    if isinstance(direct_inner, dict):
      return direct_inner
  return None


def parse_step_interaction(
    step: dict[str, Any], require_waiting_status: bool = True
) -> str | None:
  """Extracts user-facing detail when a step is blocked on permission, question, or input."""
  if not isinstance(step, dict):
    return None
  if require_waiting_status and step.get("status") not in WAITING_STEP_STATUSES:
    return None

  requested = (
      step.get("requestedInteraction")
      or step.get("requested_interaction")
      or {}
  )
  if not isinstance(requested, dict):
    requested = {}

  # Support both Connect-JSON fields and @bufbuild/protobuf v2 {case, value} oneof
  oneof = requested.get("interaction")
  oneof_case = oneof.get("case") if isinstance(oneof, dict) else ""
  oneof_val = oneof.get("value") if isinstance(oneof, dict) else None

  perm = requested.get("permission")
  if not isinstance(perm, dict) and oneof_case == "permission" and isinstance(oneof_val, dict):
    perm = oneof_val

  ask = requested.get("ask_question") or requested.get("askQuestion")
  if not isinstance(ask, dict) and oneof_case in ("askQuestion", "ask_question"):
    if isinstance(oneof_val, dict):
      ask = oneof_val
  if not isinstance(ask, dict) and isinstance(oneof, dict):
    ask_oneof = oneof.get("askQuestion") or oneof.get("ask_question")
    if isinstance(ask_oneof, dict):
      ask = ask_oneof
  if not isinstance(ask, dict) and step.get("type") in ASK_QUESTION_STEP_TYPES:
    ask = _step_oneof_payload(step, "askQuestion", "ask_question")
  if not isinstance(ask, dict):
    generic = (
        _step_oneof_payload(step, "generic", "generic")
        or (step if step.get("type") in GENERIC_STEP_TYPES else {})
    )
    if isinstance(generic, dict) and generic:
      gen_res = generic.get("result") if isinstance(generic.get("result"), dict) else generic
      gen_payload = (
          gen_res.get("payload")
          or gen_res.get("resultStruct")
          or gen_res.get("result_struct")
          or gen_res.get("resultValue")
          or gen_res.get("result_value")
      )
      gen_req = (
          generic.get("requestedInteraction")
          or generic.get("requested_interaction")
          or (
              gen_res.get("requestedInteraction")
              if isinstance(gen_res, dict)
              else None
          )
          or (
              gen_res.get("requested_interaction")
              if isinstance(gen_res, dict)
              else None
          )
      )
      if isinstance(gen_req, dict):
        gen_inter = (
            gen_req.get("interaction")
            if isinstance(gen_req.get("interaction"), dict)
            else {}
        )
        ask_candidate = (
            gen_req.get("askQuestion")
            or gen_req.get("ask_question")
            or (
                gen_inter.get("value")
                if gen_inter.get("case") in ("askQuestion", "ask_question")
                else None
            )
            or gen_inter.get("askQuestion")
            or gen_inter.get("ask_question")
        )
        if isinstance(ask_candidate, dict):
          ask = ask_candidate
      if not isinstance(ask, dict) and isinstance(gen_payload, dict):
        if isinstance(gen_payload.get("askQuestion"), dict):
          ask = gen_payload["askQuestion"]
        elif isinstance(gen_payload.get("ask_question"), dict):
          ask = gen_payload["ask_question"]
        elif isinstance(gen_payload.get("questions"), list):
          ask = gen_payload
      if not isinstance(ask, dict):
        if isinstance(generic.get("askQuestion"), dict):
          ask = generic["askQuestion"]
        elif isinstance(generic.get("ask_question"), dict):
          ask = generic["ask_question"]
        elif isinstance(generic.get("questions"), list):
          ask = generic
  if not isinstance(ask, dict):
    if isinstance(step.get("questions"), list) or isinstance(step.get("question"), str):
      ask = step

  elic = requested.get("elicitation")
  if not isinstance(elic, dict) and oneof_case == "elicitation" and isinstance(oneof_val, dict):
    elic = oneof_val

  detail = "Waiting for user input"
  if isinstance(perm, dict):
    res = perm.get("resource") if isinstance(perm.get("resource"), dict) else {}
    action = (
        res.get("action")
        or perm.get("actionDescription")
        or perm.get("action_description")
        or perm.get("toolName")
        or "command"
    )
    target = (
        res.get("target")
        or perm.get("reason")
        or perm.get("command")
        or ""
    )
    detail = f"Permission required for {action}: {target}".rstrip(": ")
  elif isinstance(ask, dict):
    questions = ask.get("questions") if isinstance(ask.get("questions"), list) else []
    q_text = ""
    if questions and isinstance(questions[0], dict):
      q_text = str(questions[0].get("question") or "")
    elif questions and isinstance(questions[0], str):
      q_text = questions[0]
    else:
      q_text = str(ask.get("question") or "")
    detail = f"Awaiting answer: {q_text or 'Question'}"
  elif isinstance(elic, dict):
    msg = (
        elic.get("message")
        or elic.get("url")
        or elic.get("serverName")
        or elic.get("server_name")
        or ""
    )
    detail = f"Input required: {msg}".rstrip(": ")

  if detail == "Waiting for user input":
    meta = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
    pr = _step_oneof_payload(step, "plannerResponse", "planner_response") or {}
    pr_calls = pr.get("toolCalls") or pr.get("tool_calls")
    pr_call = (
        pr_calls[0]
        if isinstance(pr_calls, list) and pr_calls and isinstance(pr_calls[0], dict)
        else {}
    )
    tool_call = (
        meta.get("modifiedToolCall")
        or meta.get("modified_tool_call")
        or meta.get("toolCall")
        or meta.get("tool_call")
        or step.get("toolCall")
        or step.get("tool_call")
        or pr_call
        or {}
    )
    if not isinstance(tool_call, dict):
      tool_call = {}
    step_type = str(step.get("type") or "")
    type_fallback = (
        step_type.removeprefix("CORTEX_STEP_TYPE_").lower()
        if step_type.startswith("CORTEX_STEP_TYPE_")
        else ""
    )
    tool_name = str(tool_call.get("name") or type_fallback or "")
    args_raw = tool_call.get("argumentsJson") or tool_call.get("arguments_json")
    args_obj: dict[str, Any] = {}
    if isinstance(args_raw, str) and args_raw:
      try:
        parsed = json.loads(args_raw)
        if isinstance(parsed, dict):
          args_obj = parsed
      except ValueError:
        pass
    elif isinstance(args_raw, dict):
      args_obj = args_raw

    if tool_name in ("ask_question", "askQuestion"):
      q_list = args_obj.get("questions")
      q_text = ""
      if isinstance(q_list, list) and q_list:
        if isinstance(q_list[0], dict):
          q_text = str(q_list[0].get("question") or "")
        elif isinstance(q_list[0], str):
          q_text = q_list[0]
      elif args_obj.get("question"):
        q_text = str(args_obj.get("question") or "")
      detail = f"Awaiting answer: {q_text or 'Question'}"
    else:
      run_cmd = _step_oneof_payload(step, "runCommand", "run_command") or {}
      tool_summary = args_obj.get("toolSummary") or args_obj.get("tool_summary")
      cmd = (
          args_obj.get("CommandLine")
          or args_obj.get("command_line")
          or run_cmd.get("commandLine")
          or run_cmd.get("command_line")
      )
      if tool_summary:
        detail = f"Permission required for {tool_summary}"
      elif cmd:
        detail = f"Permission required for command: {cmd}"
      elif tool_name and tool_name not in ("unspecified", "generic"):
        detail = f"Permission required for {tool_name}"

  return detail


def extract_summary_waiting_detail(summary: dict[str, Any]) -> str | None:
  """Returns blocked interaction detail from `summary.waitingSteps`, if any."""
  waiting_steps = summary.get("waitingSteps") or summary.get("waiting_steps")
  if not isinstance(waiting_steps, list) or not waiting_steps:
    return None
  best_entry: dict[str, Any] | None = None
  best_idx: int | None = None
  for entry in waiting_steps:
    if not isinstance(entry, dict):
      continue
    raw_idx = entry.get("stepIndex", entry.get("step_index", entry.get("index", 0)))
    idx = int(raw_idx) if isinstance(raw_idx, (int, float)) else 0
    if best_idx is None or idx < best_idx:
      best_idx = idx
      best_entry = entry
  if best_entry is not None:
    step_obj = (
        best_entry.get("step")
        if isinstance(best_entry.get("step"), dict)
        else best_entry
    )
    if isinstance(step_obj, dict):
      parsed = parse_step_interaction(step_obj, require_waiting_status=False)
      if parsed:
        return parsed
  return "Waiting for user input"


def _extract_subagents_and_kills(
    steps: list[dict[str, Any]],
) -> tuple[dict[str, str], dict[str, int], set[str], int]:
  """Extracts spawned subagent roles, invocation indices, killed IDs, and last kill_all index."""
  subagents: dict[str, str] = {}
  invoke_indices: dict[str, int] = {}
  killed_ids: set[str] = set()
  last_kill_all_idx = -1

  for idx, step in enumerate(steps):
    if not isinstance(step, dict):
      continue
    inv = _step_oneof_payload(step, "invokeSubagent", "invoke_subagent")
    if not inv and step.get("type") in INVOKE_SUBAGENT_STEP_TYPES:
      inv = step
    if inv:
      specs = inv.get("subagents") if isinstance(inv.get("subagents"), list) else []
      results = inv.get("results") if isinstance(inv.get("results"), list) else []
      for r_idx, res in enumerate(results):
        if not isinstance(res, dict):
          continue
        child_cid = res.get("conversationId") or res.get("conversation_id")
        if not child_cid or not isinstance(child_cid, str):
          continue
        role = ""
        if r_idx < len(specs) and isinstance(specs[r_idx], dict):
          role = str(
              specs[r_idx].get("role")
              or specs[r_idx].get("typeName")
              or specs[r_idx].get("type_name")
              or ""
          )
        subagents[child_cid] = role or "Subagent"
        invoke_indices[child_cid] = idx
        killed_ids.discard(child_cid)
      legacy_cid = inv.get("conversationId") or inv.get("conversation_id")
      if isinstance(legacy_cid, str) and legacy_cid and legacy_cid not in subagents:
        legacy_role = str(inv.get("subagentName") or inv.get("subagent_name") or "")
        subagents[legacy_cid] = legacy_role or "Subagent"
        invoke_indices[legacy_cid] = idx
        killed_ids.discard(legacy_cid)

    # Inspect both plannerResponse.toolCalls and executed toolCall metadata for kills
    tool_calls: list[Any] = []
    pr = _step_oneof_payload(step, "plannerResponse", "planner_response")
    if pr:
      tcs = pr.get("toolCalls") or pr.get("tool_calls")
      if isinstance(tcs, list):
        tool_calls.extend(tcs)
    meta = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
    meta_tc = meta.get("toolCall") or meta.get("tool_call") or step.get("toolCall")
    if isinstance(meta_tc, dict):
      tool_calls.append(meta_tc)

    for tc in tool_calls:
      if not isinstance(tc, dict) or tc.get("name") != "manage_subagents":
        continue
      args_raw = tc.get("argumentsJson") or tc.get("arguments_json") or "{}"
      try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
      except ValueError:
        continue
      if not isinstance(args, dict):
        continue
      action = str(args.get("Action") or args.get("action") or "").lower()
      if action == "kill_all":
        last_kill_all_idx = idx
        killed_ids.update(subagents.keys())
      elif action == "kill":
        cids = args.get("ConversationIds") or args.get("conversation_ids") or []
        if isinstance(cids, list):
          for kid in cids:
            if isinstance(kid, str) and kid:
              killed_ids.add(kid)

  return subagents, invoke_indices, killed_ids, last_kill_all_idx


def _planner_has_tool_calls(step: dict[str, Any]) -> bool:
  pr = _step_oneof_payload(step, "plannerResponse", "planner_response") or {}
  tool_calls = pr.get("toolCalls") or pr.get("tool_calls")
  return isinstance(tool_calls, list) and len(tool_calls) > 0


def _step_spawns_subagent(step: dict[str, Any]) -> bool:
  if step.get("type") in INVOKE_SUBAGENT_STEP_TYPES:
    return True
  if _step_oneof_payload(step, "invokeSubagent", "invoke_subagent"):
    return True
  pr = _step_oneof_payload(step, "plannerResponse", "planner_response") or {}
  tcs = pr.get("toolCalls") or pr.get("tool_calls") or []
  if isinstance(tcs, list):
    for tc in tcs:
      if isinstance(tc, dict) and tc.get("name") in ("invoke_subagent", "swarm"):
        return True
  meta = step.get("metadata") if isinstance(step.get("metadata"), dict) else {}
  mtc = meta.get("toolCall") or meta.get("tool_call") or step.get("toolCall")
  return isinstance(mtc, dict) and mtc.get("name") in ("invoke_subagent", "swarm")


def evaluate_trajectory_status(
    traj_payload: dict[str, Any],
    extra_active_subagents: dict[str, str] | None = None,
    silent_subagents: set[str] | None = None,
    is_waiting_chat: bool = False,
) -> tuple[str, str]:
  """Evaluates trajectory steps for trailing WAITING blocks and subagent synthesis.

  Follows central_todo `poll_cascade_status()` priority order while handling
  multi-turn Jetski chats, parallel tool calls, and post-response checkpoints:
  1. Trailing `CORTEX_STEP_STATUS_WAITING` steps BEFORE `RUNNING` and `IDLE`.
  2. Active `RUNNING`/`BUSY`/`CANCELING` status.
  3. Terminal `ERROR`/`USER_CANCELED` once idle (`jetski_cortex.proto:752`).
  4. Coordinator final `PLANNER_RESPONSE` synthesis (`DONE`, no `toolCalls`)
     strictly after the last active subagent `SYSTEM_MESSAGE` report.
  5. Unfulfilled tool calls / pending wakeups before concluding `IDLE`.
  """
  if not isinstance(traj_payload, dict):
    return "unknown", ""

  raw_status = traj_payload.get("status", "")
  traj = traj_payload.get("trajectory")
  if not isinstance(traj, dict):
    traj = traj_payload
  meta = traj.get("metadata") if isinstance(traj.get("metadata"), dict) else {}
  raw_steps = traj.get("steps")
  steps = raw_steps if isinstance(raw_steps, list) else []
  termination_reason = (
      traj_payload.get("latestExecutorTerminationReason")
      or traj_payload.get("latest_executor_termination_reason")
      or traj.get("latestExecutorTerminationReason")
      or traj.get("latest_executor_termination_reason")
      or meta.get("latestExecutorTerminationReason")
      or meta.get("latest_executor_termination_reason")
      or ""
  )

  # Check trailing WAITING steps before RUNNING or IDLE; stop only at USER_INPUT
  # or a concluding no-tool-call DONE PLANNER_RESPONSE so parallel DONE sibling
  # tool calls never mask a WAITING step in the same invocation
  for step in reversed(steps):
    if not isinstance(step, dict):
      continue
    waiting_detail = parse_step_interaction(step, require_waiting_status=True)
    if waiting_detail:
      return "blocked", waiting_detail
    step_type = step.get("type")
    if step_type in USER_INPUT_STEP_TYPES:
      break
    if (
        step_type in PLANNER_STEP_TYPES
        and step.get("status") in DONE_STEP_STATUSES
        and not _planner_has_tool_calls(step)
    ):
      break

  if raw_status in ACTIVE_RUN_STATUSES:
    return "running", "Agent turn running"

  # latestExecutorTerminationReason is not cleared on new turns (jetski_cortex.proto:752),
  # so inspect it only after confirming no step is WAITING and status is not RUNNING
  if termination_reason in ERROR_TERM_REASONS:
    return "error", "Execution stopped due to error"
  if termination_reason in CANCELED_TERM_REASONS:
    return "canceled", "Canceled by user"

  last_user_input_idx = -1
  for idx, step in enumerate(steps):
    if isinstance(step, dict) and step.get("type") in USER_INPUT_STEP_TYPES:
      last_user_input_idx = idx

  step_subagents, invoke_indices, killed_ids, last_kill_all_idx = (
      _extract_subagents_and_kills(steps)
  )
  active_subagents: dict[str, str] = {
      cid: role for cid, role in step_subagents.items() if cid not in killed_ids
  }
  kill_all_in_turn = last_kill_all_idx > last_user_input_idx
  if extra_active_subagents and not kill_all_in_turn:
    for cid, role in extra_active_subagents.items():
      if cid not in killed_ids and cid not in active_subagents:
        active_subagents[cid] = role or "Subagent"

  if active_subagents:
    last_report_idx: dict[str, int] = {}
    for idx, step in enumerate(steps):
      if not isinstance(step, dict) or step.get("type") not in SYSTEM_MSG_STEP_TYPES:
        continue
      sys_msg = _step_oneof_payload(step, "systemMessage", "system_message") or {}
      msg_text = str(sys_msg.get("message") or "")
      agent_msg = sys_msg.get("agentMessage") or sys_msg.get("agent_message") or {}
      sender = str(agent_msg.get("sender") or "") if isinstance(agent_msg, dict) else ""
      for cid in active_subagents:
        if sender == cid or f"sender={cid}" in msg_text or cid in msg_text:
          last_report_idx[cid] = idx

    silent_ids = silent_subagents or set()
    turn_spawned = any(
        isinstance(steps[i], dict) and _step_spawns_subagent(steps[i])
        for i in range(last_user_input_idx + 1, len(steps))
    )
    # Active subagents spawned in the current user turn must deliver a report unless
    # their own trajectory already aborted or went idle past the silent grace window
    for cid in active_subagents:
      if cid in silent_ids:
        continue
      if cid in invoke_indices:
        inv_idx = invoke_indices[cid]
      elif turn_spawned:
        inv_idx = last_user_input_idx + 1
      else:
        continue
      if inv_idx > last_user_input_idx and last_report_idx.get(cid, -1) <= inv_idx:
        return "running", "Waiting for subagent reports"

    # Any subagent report after the latest user turn (or at the tail) requires a
    # subsequent completed PLANNER_RESPONSE without toolCalls to synthesize findings
    latest_report = max(last_report_idx.values(), default=-1)
    if latest_report > last_user_input_idx:
      has_synthesis = False
      terminated_after_report = False
      for idx in range(len(steps) - 1, latest_report, -1):
        step = steps[idx]
        if not isinstance(step, dict):
          continue
        if step.get("status") in TERMINAL_STEP_STATUSES:
          terminated_after_report = True
          break
        if (
            step.get("type") in PLANNER_STEP_TYPES
            and step.get("status") in DONE_STEP_STATUSES
            and not _planner_has_tool_calls(step)
        ):
          has_synthesis = True
          break
      if not has_synthesis and not terminated_after_report:
        return "running", "Synthesizing subagent findings"

  # Verify the turn after last_user_input_idx concluded rather than pausing mid-tool-loop
  for idx in range(len(steps) - 1, last_user_input_idx, -1):
    step = steps[idx]
    if not isinstance(step, dict):
      continue
    step_type = step.get("type")
    if step_type in TRANSPARENT_TAIL_STEP_TYPES:
      continue
    step_status = step.get("status")
    if step_status in TERMINAL_STEP_STATUSES:
      return "canceled", "Canceled"
    if (
        step_type in PLANNER_STEP_TYPES
        and step_status in DONE_STEP_STATUSES
        and not _planner_has_tool_calls(step)
    ):
      return "idle", "Complete"
    if is_waiting_chat:
      return "idle", "Scheduled"
    return "running", "Agent turn in progress"

  return "idle", "Idle"


def strip_activity_badge(name: str) -> str:
  """Removes any trailing activity badge from a project name."""
  if not name:
    return ""
  return BADGE_RE.sub("", name).rstrip()


def format_project_name(
    base_name: str,
    running_count: int = 0,
    blocked_count: int = 0,
    unread_count: int = 0,
) -> str:
  """Formats a project name with an optional activity badge suffix."""
  clean_base = strip_activity_badge(base_name)
  if not clean_base:
    return base_name
  parts: list[str] = []
  if running_count > 0:
    parts.append(f"⟳ {running_count}")
  if blocked_count > 0:
    parts.append(f"⚠ {blocked_count}")
  if unread_count > 0:
    parts.append(f"● {unread_count}")
  if not parts:
    return clean_base
  return f"{clean_base} · {' '.join(parts)}"


def _file_uri_to_path(uri: str) -> str:
  if not uri or not uri.startswith("file://"):
    return ""
  return unquote(uri[len("file://") :]).rstrip("/")


def load_projects(
    projects_dir: str,
) -> tuple[dict[str, tuple[str, dict[str, Any]]], list[tuple[str, str]]]:
  """Loads all project JSON files from `projects_dir`.

  Returns:
    - known_projects: mapping of `project_id -> (file_path, parsed_json)`
    - folder_to_project: list of `(folder_path, project_id)` sorted by
      descending path length (most specific workspace path first).
  """
  known_projects: dict[str, tuple[str, dict[str, Any]]] = {}
  folder_to_project: list[tuple[str, str]] = []
  if not os.path.isdir(projects_dir):
    return known_projects, folder_to_project

  for pfile in sorted(glob.glob(os.path.join(projects_dir, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      if not isinstance(pdata, dict):
        continue
      pid = pdata.get("id", "")
      if not pid:
        continue
      known_projects[pid] = (pfile, pdata)
      resources = (pdata.get("projectResources") or {}).get("resources") or []
      for r in resources:
        if not isinstance(r, dict):
          continue
        for uri in (
            (r.get("gitFolder") or {}).get("folderUri", ""),
            r.get("folderUri", ""),
        ):
          folder_path = _file_uri_to_path(uri)
          if folder_path:
            folder_to_project.append((folder_path, pid))
    except (OSError, ValueError):
      continue

  folder_to_project.sort(key=lambda item: len(item[0]), reverse=True)
  return known_projects, folder_to_project


def match_workspace_to_project(
    workspace_paths: list[str], folder_to_project: list[tuple[str, str]]
) -> str:
  """Matches workspace paths against project resource folders."""
  for raw_ws in workspace_paths:
    if not raw_ws:
      continue
    ws = (
        _file_uri_to_path(raw_ws)
        if raw_ws.startswith("file://")
        else raw_ws.rstrip("/")
    )
    if not ws:
      continue
    for folder_path, pid in folder_to_project:
      if ws == folder_path or ws.startswith(folder_path + "/"):
        return pid
  return ""


def _read_cached_conn() -> tuple[str, str]:
  cache_file = _conn_cache_path()
  try:
    with open(cache_file, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data.get("ls_address", ""), data.get("csrf_token", "")
  except (OSError, ValueError):
    return "", ""


def _write_cached_conn(ls_address: str, csrf_token: str) -> None:
  if not ls_address or not csrf_token:
    return
  cache_file = _conn_cache_path()
  tmp_file = f"{cache_file}.tmp.{os.getpid()}"
  try:
    fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump({"ls_address": ls_address, "csrf_token": csrf_token}, f)
    os.replace(tmp_file, cache_file)
  except OSError:
    try:
      os.unlink(tmp_file)
    except OSError:
      pass


def _urlopen_ls(req: urllib.request.Request, timeout: float):
  # Block unmocked HTTP calls to the live Language Server during unit tests
  if (
      "unittest" in sys.modules
      and not os.environ.get("JETSKI_TEST_ALLOW_LS_DISCOVERY")
      and getattr(urllib.request.urlopen, "__module__", "") == "urllib.request"
  ):
    raise urllib.error.URLError("Blocked live LS request during unittest")
  return urllib.request.urlopen(req, timeout=timeout)


def _discover_from_http_index(ls_address: str) -> str:
  """Extracts csrfToken from `window.__APP_CONFIG__` on the Jetbox web index."""
  addr = ls_address.removeprefix("http://").removeprefix("https://")
  url = f"http://{addr}/"
  try:
    req = urllib.request.Request(url, method="GET")
    with _urlopen_ls(req, timeout=1.0) as resp:
      html = resp.read().decode("utf-8", errors="ignore")
    m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
    if m:
      return m.group(1)
  except Exception:
    pass
  return ""


def _discover_from_proc() -> tuple[str, str]:
  """Scans /proc/<pid>/environ on Linux for ANTIGRAVITY_LS_ADDRESS and CSRF."""
  ls_addr = ""
  csrf = ""
  if not os.path.isdir("/proc"):
    return ls_addr, csrf
  try:
    for pid_dir in os.listdir("/proc"):
      if not pid_dir.isdigit():
        continue
      try:
        with open(f"/proc/{pid_dir}/environ", "rb") as f:
          raw = f.read()
        if b"ANTIGRAVITY_CSRF_TOKEN=" not in raw:
          continue
        for item in raw.split(b"\x00"):
          if item.startswith(b"ANTIGRAVITY_LS_ADDRESS=") and not ls_addr:
            ls_addr = item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
          elif item.startswith(b"ANTIGRAVITY_CSRF_TOKEN=") and not csrf:
            csrf = item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
        if ls_addr and csrf:
          return ls_addr, csrf
      except (OSError, PermissionError):
        continue
  except OSError:
    pass
  return ls_addr, csrf


def _discover_from_ps() -> tuple[str, str]:
  """Fallback discovery from `ps` command-line flags (e.g. macOS Electron)."""
  ls_addr = ""
  csrf = ""
  try:
    out = subprocess.check_output(
        ["ps", "-axo", "command"], stderr=subprocess.DEVNULL, timeout=1.5
    ).decode("utf-8", errors="ignore")
    for line in out.splitlines():
      if "csrf_token" not in line:
        continue
      m_csrf = re.search(r"--csrf_token[=\s]+([A-Za-z0-9_-]+)", line)
      if m_csrf:
        csrf = m_csrf.group(1)
      m_port = re.search(r"--(?:http_server_)?port[=\s]+(\d+)", line)
      if m_port:
        ls_addr = f"localhost:{m_port.group(1)}"
      if csrf:
        break
  except Exception:
    pass
  return ls_addr, csrf


def _read_exact(stream: Any, n: int) -> bytes:
  """Reads exactly `n` bytes from `stream` or returns fewer on EOF."""
  buf = bytearray()
  while len(buf) < n:
    chunk = stream.read(n - len(buf))
    if not chunk:
      break
    buf.extend(chunk)
  return bytes(buf)


def _fetch_jetbox_summaries_burst(
    clean_addr: str, token: str
) -> dict[str, Any] | None:
  """Reads the initial snapshot burst from `JetboxSubscribeToSummaries`.

  `GetAllCascadeTrajectories` builds summaries without `WithNotFullyIdle`, so
  conversations whose main turn is `CASCADE_RUN_STATUS_IDLE` while waiting on a
  subagent, background `run_command` task, or `schedule` timer omit
  `notFullyIdle`. `JetboxSubscribeToSummaries` streams from
  `jetboxSummariesStore`, which includes `notFullyIdle: true` for all such
  conversations (matching the Jetski sidebar spinner).
  """
  url = (
      f"http://{clean_addr}"
      "/exa.language_server_pb.LanguageServerService/JetboxSubscribeToSummaries"
  )
  body = b"{}"
  envelope = struct.pack(">BI", 0, len(body)) + body
  req = urllib.request.Request(
      url,
      data=envelope,
      headers={
          "Content-Type": "application/connect+json",
          "x-codeium-csrf-token": token,
      },
      method="POST",
  )
  merged: dict[str, Any] = {}
  packets = 0
  with _urlopen_ls(req, timeout=2.0) as resp:
    raw_sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    while True:
      try:
        header = _read_exact(resp, 5)
      except (TimeoutError, socket.timeout):
        break
      if len(header) < 5:
        break
      flags, length = struct.unpack(">BI", header)
      if flags & 0x02:
        # End-of-stream trailer frame
        break
      if raw_sock is not None:
        raw_sock.settimeout(1.0)
      raw_payload = _read_exact(resp, length)
      if len(raw_payload) < length:
        break
      payload = json.loads(raw_payload.decode("utf-8"))
      if isinstance(payload, dict):
        updates = payload.get("updates")
        if isinstance(updates, dict):
          merged.update(updates)
      packets += 1
      if raw_sock is not None:
        # Use a short idle timeout to drain any remaining initial snapshot
        # packets (sent in batches of 100) without waiting for future events.
        raw_sock.settimeout(0.04)
      elif len(merged) < 100:
        break
  return merged if packets > 0 else None


def fetch_trajectories(
    ls_address: str = "", csrf_token: str = ""
) -> dict[str, Any] | None:
  """Fetches `trajectorySummaries` from the local Language Server.

  Prefers `JetboxSubscribeToSummaries` (which includes `notFullyIdle` for
  conversations waiting on subagents, timers, or background tasks) and falls
  back to `GetAllCascadeTrajectories`. Automatically resolves and caches
  `(ls_address, csrf_token)` if not provided or if a cached token became stale
  after a server restart.
  """
  candidates: list[tuple[str, str]] = []
  env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
  env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
  if ls_address and csrf_token:
    candidates.append((ls_address, csrf_token))
  if env_addr and env_csrf:
    candidates.append((env_addr, env_csrf))
  cached_addr, cached_csrf = _read_cached_conn()
  if cached_addr and cached_csrf:
    candidates.append((cached_addr, cached_csrf))

  def _try_call(addr: str, token: str) -> dict[str, Any] | None:
    clean_addr = addr.removeprefix("http://").removeprefix("https://")
    try:
      stream_summaries = _fetch_jetbox_summaries_burst(clean_addr, token)
      if stream_summaries is not None:
        return stream_summaries
    except Exception:
      pass

    url = (
        f"http://{clean_addr}"
        "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
    )
    # Include subtrajectories in fallback mode so child subagent statuses map to parents
    req = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
        },
        method="POST",
    )
    with _urlopen_ls(req, timeout=2.0) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
      return payload.get("trajectorySummaries") or {}
    return {}

  for addr, token in candidates:
    try:
      res = _try_call(addr, token)
      if res is not None:
        _write_cached_conn(addr, token)
        return res
    except Exception:
      continue

  # Full discovery fallback (needed inside hooks where LS does not export
  # ANTIGRAVITY_CSRF_TOKEN in its own environment).
  base_addr = env_addr or cached_addr or DEFAULT_LS_ADDRESS
  idx_csrf = _discover_from_http_index(base_addr)
  if idx_csrf:
    try:
      res = _try_call(base_addr, idx_csrf)
      if res is not None:
        _write_cached_conn(base_addr, idx_csrf)
        return res
    except Exception:
      pass

  proc_addr, proc_csrf = _discover_from_proc()
  if proc_csrf:
    addr = proc_addr or base_addr
    try:
      res = _try_call(addr, proc_csrf)
      if res is not None:
        _write_cached_conn(addr, proc_csrf)
        return res
    except Exception:
      pass

  ps_addr, ps_csrf = _discover_from_ps()
  if ps_csrf:
    addr = ps_addr or base_addr
    try:
      res = _try_call(addr, ps_csrf)
      if res is not None:
        _write_cached_conn(addr, ps_csrf)
        return res
    except Exception:
      pass

  return None


def fetch_single_trajectory(
    cid: str, ls_address: str = "", csrf_token: str = ""
) -> dict[str, Any] | None:
  """Fetches a single `PROD_UI` trajectory, appending any truncated tail steps."""
  if not cid:
    return None
  addr = ls_address or os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
  token = csrf_token or os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
  if not addr or not token:
    cached_addr, cached_csrf = _read_cached_conn()
    addr = addr or cached_addr
    token = token or cached_csrf
  if not addr or not token:
    return None

  clean_addr = addr.removeprefix("http://").removeprefix("https://")
  base_url = f"http://{clean_addr}/exa.language_server_pb.LanguageServerService"
  headers = {
      "Content-Type": "application/json",
      "x-codeium-csrf-token": token,
  }
  req = urllib.request.Request(
      f"{base_url}/GetCascadeTrajectory",
      data=json.dumps({
          "cascadeId": cid,
          "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI",
      }).encode("utf-8"),
      headers=headers,
      method="POST",
  )
  try:
    with _urlopen_ls(req, timeout=1.5) as resp:
      data = json.loads(resp.read().decode("utf-8"))
  except Exception:
    return None
  if not isinstance(data, dict):
    return None

  traj = data.get("trajectory")
  steps = traj.get("steps") if isinstance(traj, dict) else None
  total = data.get("numTotalSteps") or data.get("num_total_steps") or 0
  # Fetch remaining pages when GetCascadeTrajectory truncates at GRPCMaxMessageSize
  if isinstance(steps, list) and isinstance(total, int):
    for _ in range(8):
      if not (0 < len(steps) < total):
        break
      tail_req = urllib.request.Request(
          f"{base_url}/GetCascadeTrajectorySteps",
          data=json.dumps({
              "cascadeId": cid,
              "stepOffset": len(steps),
              "trajectoryVerbosity": "CLIENT_TRAJECTORY_VERBOSITY_PROD_UI",
          }).encode("utf-8"),
          headers=headers,
          method="POST",
      )
      try:
        with _urlopen_ls(tail_req, timeout=1.5) as tail_resp:
          tail_data = json.loads(tail_resp.read().decode("utf-8"))
        tail_steps = tail_data.get("steps") if isinstance(tail_data, dict) else None
        if not isinstance(tail_steps, list) or not tail_steps:
          break
        steps.extend(tail_steps)
      except Exception:
        break
  return data


def _load_traj_eval_cache(cache_path: str = "") -> dict[str, dict[str, Any]]:
  path = cache_path or _traj_cache_path()
  try:
    with open(path, "r", encoding="utf-8") as f:
      raw = json.load(f)
    if isinstance(raw, dict):
      return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, dict)}
  except (OSError, ValueError):
    pass
  return {}


def _save_traj_eval_cache(
    cache: dict[str, dict[str, Any]], cache_path: str = ""
) -> None:
  path = cache_path or _traj_cache_path()
  if len(cache) > TRAJ_CACHE_MAX_ENTRIES:
    ranked = sorted(
        cache.items(),
        key=lambda item: float(item[1].get("ts") or 0.0),
        reverse=True,
    )
    cache = dict(ranked[:TRAJ_CACHE_MAX_ENTRIES])
  tmp_path = f"{path}.tmp.{os.getpid()}"
  try:
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump(cache, f)
    os.replace(tmp_path, path)
  except OSError:
    try:
      os.unlink(tmp_path)
    except OSError:
      pass


def _parse_iso_timestamp(ts: str) -> float:
  if not ts or not isinstance(ts, str):
    return 0.0
  try:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
  except ValueError:
    return 0.0


def _is_visible_top_level(summary: dict[str, Any]) -> bool:
  annotations = summary.get("annotations") or {}
  if annotations.get("archived"):
    return False
  meta = summary.get("trajectoryMetadata") or {}
  if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
    return False
  source_meta = meta.get("sourceMetadata") or {}
  if (
      source_meta.get("tool")
      or source_meta.get("hideFromConversationList")
      or source_meta.get("hide_from_conversation_list")
  ):
    return False
  return True


# Returns True when a conversation title carries the explicit ⏳ waiting marker
def _is_waiting_conversation(summary: dict[str, Any]) -> bool:
  annotations = summary.get("annotations") or {}
  # Check both annotations.title and summary.summary so unsynced RPC snapshots still match
  for candidate in (annotations.get("title"), summary.get("summary")):
    if isinstance(candidate, str) and WAITING_TITLE_RE.match(candidate):
      return True
  return False


def resolve_currently_viewing_cid(
    summaries: dict[str, Any],
    annotations_dir: str = DEFAULT_ANNOTATIONS_DIR,
    state_file: str = "",
) -> tuple[str, dict[str, float]]:
  """Identifies the currently open conversation and tracks its viewed step time.

  When the user switches from `prev` to `curr`, `conversationViewingMiddleware`
  updates `lastUserViewTime` on `[prev, curr]` in a single RPC and the server
  writes `prev.pbtxt` before `curr.pbtxt`. Breaking `lastUserViewTime` ties via
  `.pbtxt` `st_mtime_ns` and caching the winner for that `max_luvt` timestamp
  deterministically identifies `curr` even if `prev.pbtxt` is later touched by
  background title updates.
  """
  max_luvt = 0.0
  for summary in summaries.values():
    if (
        not isinstance(summary, dict)
        or not _is_visible_top_level(summary)
    ):
      continue
    luvt = _parse_iso_timestamp(
        (summary.get("annotations") or {}).get("lastUserViewTime", "")
    )
    if luvt > max_luvt:
      max_luvt = luvt

  if max_luvt <= 0.0:
    return "", {}

  cache_path = state_file or _view_state_path()
  cached_max_luvt = 0.0
  cached_cid = ""
  viewed_lmt: dict[str, float] = {}
  try:
    with open(cache_path, "r", encoding="utf-8") as f:
      cached = json.load(f)
    if isinstance(cached, dict):
      cached_max_luvt = float(cached.get("max_luvt") or 0.0)
      cached_cid = str(cached.get("current_cid") or "")
      raw_viewed = cached.get("viewed_lmt")
      if isinstance(raw_viewed, dict):
        viewed_lmt = {
            str(k): float(v)
            for k, v in raw_viewed.items()
            if isinstance(v, (int, float))
        }
  except (OSError, ValueError, TypeError):
    pass

  if (
      abs(max_luvt - cached_max_luvt) <= 1e-6
      and cached_cid
      and cached_cid in summaries
      and _is_visible_top_level(summaries[cached_cid])
  ):
    current_cid = cached_cid
  else:
    best_key = (-1.0, -1)
    current_cid = ""
    for cid, summary in summaries.items():
      if (
          not isinstance(summary, dict)
          or not _is_visible_top_level(summary)
      ):
        continue
      luvt = _parse_iso_timestamp(
          (summary.get("annotations") or {}).get("lastUserViewTime", "")
      )
      if max_luvt - luvt <= 0.005:
        pbtxt = os.path.join(annotations_dir, f"{cid}.pbtxt")
        try:
          mtime_ns = os.stat(pbtxt).st_mtime_ns
        except OSError:
          mtime_ns = 0
        key = (luvt, mtime_ns)
        if key > best_key:
          best_key = key
          current_cid = cid

  dirty = (
      abs(max_luvt - cached_max_luvt) > 1e-6 or current_cid != cached_cid
  )
  if current_cid and isinstance(summaries.get(current_cid), dict):
    cur_lmt = _parse_iso_timestamp(
        summaries[current_cid].get("lastModifiedTime", "")
    )
    if cur_lmt > viewed_lmt.get(current_cid, 0.0):
      viewed_lmt[current_cid] = cur_lmt
      dirty = True

  if dirty:
    if len(viewed_lmt) > 64:
      sorted_items = sorted(viewed_lmt.items(), key=lambda x: x[1], reverse=True)
      viewed_lmt = dict(sorted_items[:64])
    tmp_path = f"{cache_path}.tmp.{os.getpid()}"
    try:
      with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "max_luvt": max_luvt,
                "current_cid": current_cid,
                "viewed_lmt": viewed_lmt,
            },
            f,
        )
      os.replace(tmp_path, cache_path)
    except OSError:
      try:
        os.unlink(tmp_path)
      except OSError:
        pass

  return current_cid, viewed_lmt


def is_conversation_unread(
    cid: str,
    summary: dict[str, Any],
    currently_viewing_cid: str = "",
    viewed_lmt: dict[str, float] | None = None,
) -> bool:
  """Returns True if an idle conversation has unread updates."""
  # Background cron/waiting chats (⏳) and archived (finished or handed-off) chats do not light
  # up unread project badges
  if not _is_visible_top_level(summary) or _is_waiting_conversation(summary):
    return False
  annotations = summary.get("annotations") or {}
  if annotations.get("markedAsUnread"):
    return True
  if currently_viewing_cid and cid == currently_viewing_cid:
    return False
  lmt = _parse_iso_timestamp(summary.get("lastModifiedTime", ""))
  if lmt <= 0.0:
    return False
  luvt = _parse_iso_timestamp(annotations.get("lastUserViewTime", ""))
  effective_view = max(luvt, (viewed_lmt or {}).get(cid, 0.0))
  return lmt > effective_view


def _build_descendant_index(
    summaries: dict[str, Any],
) -> dict[str, list[tuple[str, str]]]:
  """Maps each parent conversation ID to its non-killed child subagents and active battle arms."""
  children_by_parent: dict[str, list[tuple[str, str]]] = defaultdict(list)
  seen_edges: set[tuple[str, str]] = set()

  for child_cid, child_summary in summaries.items():
    if not isinstance(child_summary, dict) or bool(child_summary.get("killed")):
      continue
    meta = child_summary.get("trajectoryMetadata") or {}
    parent_cid = meta.get("parentConversationId") or ""
    if not isinstance(parent_cid, str) or not parent_cid:
      continue
    annotations = child_summary.get("annotations") or {}
    label = str(
        annotations.get("title")
        or child_summary.get("summary")
        or "Subagent"
    ).strip()
    edge = (parent_cid, child_cid)
    if edge not in seen_edges:
      seen_edges.add(edge)
      children_by_parent[parent_cid].append((child_cid, label or "Subagent"))

  for parent_cid, parent_summary in summaries.items():
    if not isinstance(parent_summary, dict):
      continue
    battle = (
        parent_summary.get("battleModeInfo")
        or parent_summary.get("battle_mode_info")
    )
    if not isinstance(battle, dict):
      continue
    end_type = battle.get("endType", battle.get("end_type", 0))
    winner = (
        battle.get("winningConversationId")
        or battle.get("winning_conversation_id")
    )
    if end_type not in ONGOING_BATTLE_END_TYPES or winner:
      continue
    arm_ids = (
        battle.get("childrenConversationIds")
        or battle.get("children_conversation_ids")
        or []
    )
    eliminated = battle.get("eliminated") or []
    if not isinstance(arm_ids, list):
      continue
    for idx, arm_cid in enumerate(arm_ids):
      if not isinstance(arm_cid, str) or not arm_cid:
        continue
      if isinstance(eliminated, list) and idx < len(eliminated) and bool(eliminated[idx]):
        continue
      arm_summary = summaries.get(arm_cid)
      if isinstance(arm_summary, dict) and bool(arm_summary.get("killed")):
        continue
      edge = (parent_cid, arm_cid)
      if edge not in seen_edges:
        seen_edges.add(edge)
        children_by_parent[parent_cid].append((arm_cid, f"Battle arm {idx + 1}"))

  return dict(children_by_parent)


def _merge_summary_into_traj(
    summary: dict[str, Any], traj_payload: dict[str, Any]
) -> dict[str, Any]:
  merged = dict(traj_payload)
  if not merged.get("status") and summary.get("status") is not None:
    merged["status"] = summary.get("status")
  term = (
      summary.get("latestExecutorTerminationReason")
      or summary.get("latest_executor_termination_reason")
  )
  if (
      term is not None
      and not merged.get("latestExecutorTerminationReason")
      and not merged.get("latest_executor_termination_reason")
  ):
    merged["latestExecutorTerminationReason"] = term
  return merged


def _summary_sig(cid: str, summary: dict[str, Any]) -> str:
  ws = summary.get("waitingSteps") or summary.get("waiting_steps")
  ws_len = len(ws) if isinstance(ws, list) else 0
  term = (
      summary.get("latestExecutorTerminationReason")
      or summary.get("latest_executor_termination_reason")
      or ""
  )
  return (
      f"{cid}:{summary.get('stepCount', '')}:{summary.get('lastModifiedTime', '')}:"
      f"{summary.get('status', '')}:{int(bool(summary.get('notFullyIdle')))}:"
      f"{int(bool(summary.get('hasRunningSubagents')))}:{ws_len}:{term}:"
      f"{int(bool(summary.get('killed')))}:{int(bool(summary.get('interrupted')))}:"
      f"{int(_is_waiting_conversation(summary))}"
  )


def _battle_arm_ids(summary: dict[str, Any]) -> set[str]:
  battle = summary.get("battleModeInfo") or summary.get("battle_mode_info")
  if not isinstance(battle, dict):
    return set()
  arms = (
      battle.get("childrenConversationIds")
      or battle.get("children_conversation_ids")
      or []
  )
  return {a for a in arms if isinstance(a, str) and a}


def _is_silent_subagent_summary(
    s_sum: dict[str, Any], now_ts: float | None
) -> bool:
  if not s_sum:
    return False
  term = (
      s_sum.get("latestExecutorTerminationReason")
      or s_sum.get("latest_executor_termination_reason")
  )
  if term in ERROR_TERM_REASONS or term in CANCELED_TERM_REASONS:
    return True
  lmt = _parse_iso_timestamp(
      s_sum.get("lastModifiedTime") or s_sum.get("createdTime") or ""
  )
  return (
      now_ts is not None
      and lmt > 0.0
      and (now_ts - lmt) > SILENT_SUBAGENT_GRACE_SEC
  )


def resolve_conversation_activity(
    cid: str,
    summary: dict[str, Any],
    summaries: dict[str, Any],
    children_by_parent: dict[str, list[tuple[str, str]]] | None = None,
    trajectories: dict[str, dict[str, Any]] | None = None,
    traj_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    eval_cache: dict[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
    force_running: bool = False,
    currently_viewing_cid: str = "",
    viewed_lmt: dict[str, float] | None = None,
) -> tuple[str, str]:
  """Resolves `(bucket, detail)` for a visible top-level conversation.

  Priority order:
  1. `blocked` (summary `waitingSteps`, trailing `WAITING` step, or blocked active
     descendant subagent / ongoing battle arm) — checked BEFORE `running` and `idle`.
  2. `running` (active run status, non-`⏳` `notFullyIdle`, active descendant,
     `force_running`, or pending coordinator `PLANNER_RESPONSE` synthesis after
     active subagent reports).
  3. `unread` (idle finished chat newer than `lastUserViewTime`).
  4. `idle`.
  """
  self_wait = extract_summary_waiting_detail(summary)
  if self_wait and self_wait != "Waiting for user input":
    return "blocked", self_wait

  child_index = (
      children_by_parent
      if children_by_parent is not None
      else _build_descendant_index(summaries)
  )
  is_waiting_chat = _is_waiting_conversation(summary)
  lmt = _parse_iso_timestamp(summary.get("lastModifiedTime", ""))
  self_active = (
      summary.get("status") in ACTIVE_RUN_STATUSES
      or (not is_waiting_chat and bool(summary.get("notFullyIdle")))
      or bool(summary.get("hasRunningSubagents"))
  )

  # Collect candidate descendant tree first for cache signature & fast summary checks
  raw_tree: list[str] = []
  seen_tree: set[str] = {cid}
  q_ids = [c for c, _ in child_index.get(cid, [])]
  while q_ids:
    nxt = q_ids.pop(0)
    if nxt in seen_tree:
      continue
    seen_tree.add(nxt)
    raw_tree.append(nxt)
    for gc, _ in child_index.get(nxt, []):
      if gc not in seen_tree:
        q_ids.append(gc)

  # Check cached evaluation when neither self nor any descendant summary changed
  tree_sig = "|".join(
      [_summary_sig(cid, summary)]
      + [_summary_sig(d, summaries.get(d, {})) for d in raw_tree]
  )
  if eval_cache is not None and trajectories is None and not force_running:
    cached_entry = eval_cache.get(cid)
    if isinstance(cached_entry, dict) and cached_entry.get("sig") == tree_sig:
      c_state = str(cached_entry.get("state") or "")
      c_detail = str(cached_entry.get("detail") or "")
      c_ts = float(cached_entry.get("ts") or 0.0)
      grace_stale = (
          c_detail == "Waiting for subagent reports"
          and now_ts is not None
          and (now_ts - c_ts) > SILENT_SUBAGENT_GRACE_SEC
      )
      if not grace_stale and c_state in ("blocked", "running", "idle_traj"):
        if c_state == "blocked":
          return "blocked", c_detail
        if c_state == "running":
          return "running", c_detail
        if is_conversation_unread(
            cid,
            summary,
            currently_viewing_cid=currently_viewing_cid,
            viewed_lmt=viewed_lmt,
        ):
          return "unread", ""
        return "idle", c_detail

  def _get_traj(target_cid: str, target_summary: dict[str, Any]) -> dict[str, Any] | None:
    if trajectories is not None and target_cid in trajectories:
      return trajectories[target_cid]
    if traj_fetcher is None:
      return None
    t_lmt = _parse_iso_timestamp(target_summary.get("lastModifiedTime", ""))
    t_active = (
        target_summary.get("status") in ACTIVE_RUN_STATUSES
        or bool(target_summary.get("notFullyIdle"))
        or bool(target_summary.get("hasRunningSubagents"))
    )
    has_children = bool(child_index.get(target_cid))
    is_recent = (
        now_ts is None
        or t_lmt <= 0.0
        or (now_ts - t_lmt) <= RECENT_TRAJ_CHECK_WINDOW_SEC
        or target_cid == currently_viewing_cid
    )
    if not (t_active or has_children or is_recent):
      return None
    return traj_fetcher(target_cid)

  def _extract_steps(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
      return []
    inner = payload.get("trajectory")
    src = inner if isinstance(inner, dict) else payload
    raw_s = src.get("steps")
    return raw_s if isinstance(raw_s, list) else []

  root_traj = _get_traj(cid, summary)
  root_steps = _extract_steps(root_traj)
  root_step_subs, _, root_killed, root_last_kill_all = (
      _extract_subagents_and_kills(root_steps) if root_steps else ({}, {}, set(), -1)
  )
  root_last_ui = -1
  for idx, s in enumerate(root_steps):
    if isinstance(s, dict) and s.get("type") in USER_INPUT_STEP_TYPES:
      root_last_ui = idx
  root_kill_all_active = root_last_kill_all > root_last_ui

  # Check self trajectory for trailing WAITING step BEFORE inspecting RUNNING
  if root_traj is not None:
    early_state, early_detail = evaluate_trajectory_status(
        _merge_summary_into_traj(summary, root_traj),
        is_waiting_chat=is_waiting_chat,
    )
    if early_state == "blocked":
      if eval_cache is not None and trajectories is None:
        eval_cache[cid] = {
            "sig": tree_sig,
            "state": "blocked",
            "detail": early_detail,
            "ts": now_ts or time.time(),
        }
      return "blocked", early_detail
  if self_wait:
    if eval_cache is not None and trajectories is None:
      eval_cache[cid] = {
          "sig": tree_sig,
          "state": "blocked",
          "detail": self_wait,
          "ts": now_ts or time.time(),
      }
    return "blocked", self_wait

  # Walk non-killed descendants in BFS order so blocked subagents/arms surface with role
  direct_active_children: dict[str, str] = {}
  silent_children: set[str] = set()
  root_battle_arms = _battle_arm_ids(summary)
  any_descendant_running = False
  visited: set[str] = {cid}
  # Queue items: (desc_cid, label, is_direct, parent_killed, parent_kill_all, roles)
  bfs_queue: list[tuple[str, str, bool, set[str], bool, dict[str, str]]] = [
      (c_cid, c_label, True, root_killed, root_kill_all_active, root_step_subs)
      for c_cid, c_label in child_index.get(cid, [])
  ]
  while bfs_queue:
    d_cid, d_label, is_direct, p_killed, p_kill_all, p_roles = bfs_queue.pop(0)
    if d_cid in visited:
      continue
    visited.add(d_cid)
    if d_cid in p_killed or (p_kill_all and d_cid not in p_roles):
      continue
    d_summary = summaries.get(d_cid)
    if not isinstance(d_summary, dict):
      d_summary = {}
    if bool(d_summary.get("killed")):
      continue

    role = p_roles.get(d_cid) or d_label or "Subagent"
    d_wait = extract_summary_waiting_detail(d_summary)
    if d_wait:
      blocked_msg = f"[{role}] {d_wait}"
      if eval_cache is not None and trajectories is None:
        eval_cache[cid] = {
            "sig": tree_sig,
            "state": "blocked",
            "detail": blocked_msg,
            "ts": now_ts or time.time(),
        }
      return "blocked", blocked_msg

    d_traj = _get_traj(d_cid, d_summary)
    d_steps = _extract_steps(d_traj)
    d_subs, _, d_killed, d_last_kill_all = (
        _extract_subagents_and_kills(d_steps) if d_steps else ({}, {}, set(), -1)
    )
    d_last_ui = -1
    for idx, s in enumerate(d_steps):
      if isinstance(s, dict) and s.get("type") in USER_INPUT_STEP_TYPES:
        d_last_ui = idx

    d_traj_state = ""
    if d_traj is not None:
      d_traj_state, d_traj_detail = evaluate_trajectory_status(
          _merge_summary_into_traj(d_summary, d_traj),
          is_waiting_chat=False,
      )
      if d_traj_state == "blocked":
        blocked_msg = f"[{role}] {d_traj_detail}"
        if eval_cache is not None and trajectories is None:
          eval_cache[cid] = {
              "sig": tree_sig,
              "state": "blocked",
              "detail": blocked_msg,
              "ts": now_ts or time.time(),
          }
        return "blocked", blocked_msg

    d_running = (
        d_summary.get("status") in ACTIVE_RUN_STATUSES
        or bool(d_summary.get("notFullyIdle"))
        or bool(d_summary.get("hasRunningSubagents"))
        or d_traj_state == "running"
    )
    if d_running:
      any_descendant_running = True

    # Battle Mode arms never send SYSTEM_MESSAGE reports, so exclude them from synthesis checks
    is_battle_arm = (
        d_cid in root_battle_arms
        or bool(d_summary.get("isBattleModeFork"))
        or bool((d_summary.get("trajectoryMetadata") or {}).get("isBattleModeFork"))
    )
    if is_direct and not is_battle_arm:
      direct_active_children[d_cid] = role
      if not d_running and _is_silent_subagent_summary(d_summary, now_ts):
        silent_children.add(d_cid)

    for gc_cid, gc_label in child_index.get(d_cid, []):
      if gc_cid not in visited:
        bfs_queue.append((
            gc_cid,
            gc_label,
            False,
            d_killed,
            d_last_kill_all > d_last_ui,
            d_subs,
        ))

  # Also mark step-spawned subagents whose summary in `summaries` aborted or went stale-idle
  for s_cid in root_step_subs:
    if s_cid in silent_children or s_cid not in summaries:
      continue
    s_sum = summaries.get(s_cid)
    if not isinstance(s_sum, dict):
      continue
    if bool(s_sum.get("killed")):
      silent_children.add(s_cid)
      continue
    s_running = (
        s_sum.get("status") in ACTIVE_RUN_STATUSES
        or bool(s_sum.get("notFullyIdle"))
    )
    if not s_running and _is_silent_subagent_summary(s_sum, now_ts):
      silent_children.add(s_cid)

  if force_running:
    return "running", "Agent turn starting"

  if (
      summary.get("status") in ACTIVE_RUN_STATUSES
      or (not is_waiting_chat and bool(summary.get("notFullyIdle")))
      or any_descendant_running
  ):
    if eval_cache is not None and trajectories is None:
      eval_cache[cid] = {
          "sig": tree_sig,
          "state": "running",
          "detail": "Agent turn running",
          "ts": now_ts or time.time(),
      }
    return "running", "Agent turn running"

  if root_traj is not None and not bool(summary.get("interrupted")):
    traj_state, traj_detail = evaluate_trajectory_status(
        _merge_summary_into_traj(summary, root_traj),
        extra_active_subagents=direct_active_children,
        silent_subagents=silent_children,
        is_waiting_chat=is_waiting_chat,
    )
    stale_idle_turn = (
        now_ts is not None
        and lmt > 0.0
        and (now_ts - lmt) > STALE_IDLE_TURN_SEC
        and not self_active
    )
    if traj_state == "running" and not stale_idle_turn:
      if eval_cache is not None and trajectories is None:
        eval_cache[cid] = {
            "sig": tree_sig,
            "state": "running",
            "detail": traj_detail,
            "ts": now_ts or time.time(),
        }
      return "running", traj_detail
    if eval_cache is not None and trajectories is None:
      eval_cache[cid] = {
          "sig": tree_sig,
          "state": "idle_traj",
          "detail": traj_detail,
          "ts": now_ts or time.time(),
      }

  if is_conversation_unread(
      cid,
      summary,
      currently_viewing_cid=currently_viewing_cid,
      viewed_lmt=viewed_lmt,
  ):
    return "unread", ""
  return "idle", ""


def compute_project_activity(
    summaries: dict[str, Any],
    known_projects: dict[str, tuple[str, dict[str, Any]]],
    folder_to_project: list[tuple[str, str]],
    force_running_cid: str = "",
    force_running_project_id: str = "",
    force_running_workspaces: list[str] | None = None,
    currently_viewing_cid: str = "",
    viewed_lmt: dict[str, float] | None = None,
    trajectories: dict[str, dict[str, Any]] | None = None,
    traj_fetcher: Callable[[str], dict[str, Any] | None] | None = None,
    eval_cache: dict[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
  """Computes `(running_counts, blocked_counts, unread_counts)` per project ID."""
  running_counts: dict[str, int] = defaultdict(int)
  blocked_counts: dict[str, int] = defaultdict(int)
  unread_counts: dict[str, int] = defaultdict(int)
  seen_forced = False
  children_by_parent = _build_descendant_index(summaries)

  for cid, summary in summaries.items():
    if not isinstance(summary, dict) or not _is_visible_top_level(summary):
      continue

    meta = summary.get("trajectoryMetadata") or {}
    pid = meta.get("projectId") or ""
    if not pid or pid not in known_projects:
      ws_uris: list[str] = []
      for w in summary.get("workspaces") or []:
        if isinstance(w, dict) and w.get("workspaceFolderAbsoluteUri"):
          ws_uris.append(w["workspaceFolderAbsoluteUri"])
      for u in meta.get("workspaceUris") or []:
        if isinstance(u, str):
          ws_uris.append(u)
      pid = match_workspace_to_project(ws_uris, folder_to_project)

    if not pid or pid not in known_projects:
      continue

    is_forced = bool(force_running_cid and cid == force_running_cid)
    if is_forced:
      seen_forced = True

    bucket, _ = resolve_conversation_activity(
        cid,
        summary,
        summaries,
        children_by_parent=children_by_parent,
        trajectories=trajectories,
        traj_fetcher=traj_fetcher,
        eval_cache=eval_cache,
        now_ts=now_ts,
        force_running=is_forced,
        currently_viewing_cid=currently_viewing_cid,
        viewed_lmt=viewed_lmt,
    )
    if bucket == "blocked":
      blocked_counts[pid] += 1
    elif bucket == "running":
      running_counts[pid] += 1
    elif bucket == "unread":
      unread_counts[pid] += 1

  # If PreInvocation fired for a brand-new conversation before its summary or
  # project assignment appeared in GetAllCascadeTrajectories:
  if force_running_cid and not seen_forced:
    pid = force_running_project_id
    if (not pid or pid not in known_projects) and force_running_workspaces:
      pid = match_workspace_to_project(
          force_running_workspaces, folder_to_project
      )
    if pid and pid in known_projects:
      running_counts[pid] += 1

  return dict(running_counts), dict(blocked_counts), dict(unread_counts)


def apply_project_badges(
    projects_dir: str,
    running_counts: dict[str, int],
    blocked_counts: dict[str, int],
    unread_counts: dict[str, int] | None = None,
) -> list[tuple[str, str, str]]:
  """Atomically updates project JSON files whose badge changed.

  Returns a list of `(project_id, old_name, new_name)` for modified projects.
  """
  known_projects, _ = load_projects(projects_dir)
  changes: list[tuple[str, str, str]] = []
  unreads = unread_counts or {}

  for pid, (pfile, _) in known_projects.items():
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        fresh_data = json.load(f)
      if not isinstance(fresh_data, dict):
        continue
      old_name = fresh_data.get("name", "")
      if not isinstance(old_name, str) or not old_name:
        continue
      new_name = format_project_name(
          old_name,
          running_count=running_counts.get(pid, 0),
          blocked_count=blocked_counts.get(pid, 0),
          unread_count=unreads.get(pid, 0),
      )
      if new_name == old_name:
        continue
      fresh_data["name"] = new_name
      tmp_path = f"{pfile}.tmp.{os.getpid()}"
      with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(fresh_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
      os.replace(tmp_path, pfile)
      changes.append((pid, old_name, new_name))
    except (OSError, ValueError):
      continue

  return changes


def sync_once(
    projects_dir: str = DEFAULT_PROJECTS_DIR,
    force_running_cid: str = "",
    force_running_project_id: str = "",
    force_running_workspaces: list[str] | None = None,
) -> tuple[bool, int, list[tuple[str, str, str]]]:
  """Performs a single locked synchronization of project activity badges.

  Returns:
    `(rpc_ok, total_tracked_chats, modified_projects)`
  """
  os.makedirs(os.path.dirname(_sync_lock_path()), exist_ok=True)
  with open(_sync_lock_path(), "w", encoding="utf-8") as lock_f:
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    try:
      summaries = fetch_trajectories()
      if summaries is None:
        return False, 0, []
      known_projects, folder_to_project = load_projects(projects_dir)
      currently_viewing_cid, viewed_lmt = resolve_currently_viewing_cid(
          summaries
      )
      # Persist trajectory evaluations across watcher ticks so idle polls issue 0 step RPCs
      eval_cache = _load_traj_eval_cache()
      before_cache = json.dumps(eval_cache, sort_keys=True)
      running_counts, blocked_counts, unread_counts = compute_project_activity(
          summaries,
          known_projects,
          folder_to_project,
          force_running_cid=force_running_cid,
          force_running_project_id=force_running_project_id,
          force_running_workspaces=force_running_workspaces,
          currently_viewing_cid=currently_viewing_cid,
          viewed_lmt=viewed_lmt,
          traj_fetcher=fetch_single_trajectory,
          eval_cache=eval_cache,
          now_ts=time.time(),
      )
      if json.dumps(eval_cache, sort_keys=True) != before_cache:
        _save_traj_eval_cache(eval_cache)
      changes = apply_project_badges(
          projects_dir, running_counts, blocked_counts, unread_counts
      )
      total_tracked = (
          sum(running_counts.values())
          + sum(blocked_counts.values())
          + sum(unread_counts.values())
      )
      return True, total_tracked, changes
    finally:
      fcntl.flock(lock_f, fcntl.LOCK_UN)


def clear_all_badges(
    projects_dir: str = DEFAULT_PROJECTS_DIR,
) -> list[tuple[str, str, str]]:
  """Strips all activity badges from all projects in `projects_dir`."""
  with open(_sync_lock_path(), "w", encoding="utf-8") as lock_f:
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    try:
      return apply_project_badges(projects_dir, {}, {}, {})
    finally:
      fcntl.flock(lock_f, fcntl.LOCK_UN)


def run_watcher_loop(projects_dir: str = DEFAULT_PROJECTS_DIR) -> None:
  """Runs a singleton background polling loop until all projects are idle and read."""
  lock_f = open(_watcher_lock_path(), "w", encoding="utf-8")
  try:
    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    lock_f.close()
    return

  try:
    lock_f.write(str(os.getpid()))
    lock_f.flush()
    start_ts = time.monotonic()
    idle_streak = 0
    rpc_fail_streak = 0

    while (time.monotonic() - start_ts) < MAX_WATCH_DURATION_SEC:
      time.sleep(WATCH_POLL_INTERVAL_SEC)
      rpc_ok, total_tracked, _ = sync_once(projects_dir=projects_dir)
      if not rpc_ok:
        rpc_fail_streak += 1
        if rpc_fail_streak >= 2:
          # Language Server stopped or became unreachable; leave clean names
          clear_all_badges(projects_dir=projects_dir)
          break
        continue

      rpc_fail_streak = 0
      if total_tracked == 0:
        idle_streak += 1
        if idle_streak >= IDLE_POLLS_BEFORE_EXIT:
          break
      else:
        idle_streak = 0
  finally:
    try:
      fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
      pass
    lock_f.close()


def spawn_detached_trigger(
    event: str,
    cid: str,
    project_id: str,
    workspace_paths: list[str],
    projects_dir: str,
) -> None:
  """Spawns `--bg-trigger` in a detached session without holding hook stdio."""
  cmd = [
      sys.executable,
      os.path.abspath(__file__),
      "--bg-trigger",
      "--event",
      event,
      "--projects-dir",
      projects_dir,
  ]
  if cid:
    cmd.extend(["--cid", cid])
  if project_id:
    cmd.extend(["--project-id", project_id])
  if workspace_paths:
    cmd.extend(["--workspaces-json", json.dumps(workspace_paths)])

  try:
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
  except OSError:
    pass


# Atomic `.tmp` -> `mv` replacements drop 0755; restore `+x` before sibling hooks run
def ensure_hooks_executable(hooks_dir: str | None = None) -> list[str]:
  target_dir = hooks_dir or os.path.dirname(os.path.abspath(__file__))
  restored: list[str] = []
  try:
    for entry in os.scandir(target_dir):
      if entry.name.endswith(".py") and entry.is_file(follow_symlinks=False):
        mode = entry.stat(follow_symlinks=False).st_mode
        if (mode & 0o111) != 0o111:
          os.chmod(entry.path, mode | 0o111)
          restored.append(entry.name)
  except OSError:
    pass
  return restored


def handle_hook_invocation(event: str, projects_dir: str) -> None:
  """Handles `PreInvocation` or `Stop` hook execution in <10ms."""
  ensure_hooks_executable()
  raw_stdin = ""
  try:
    if not sys.stdin.isatty():
      raw_stdin = sys.stdin.read()
  except OSError:
    pass

  payload: dict[str, Any] = {}
  if raw_stdin.strip():
    try:
      parsed = json.loads(raw_stdin)
      if isinstance(parsed, dict):
        payload = parsed
    except ValueError:
      pass

  cid = (
      payload.get("conversationId")
      or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
      or ""
  )
  project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
  workspace_paths = payload.get("workspacePaths") or []
  if not isinstance(workspace_paths, list):
    workspace_paths = []

  # Immediately emit `{}` and flush so the hook never blocks the agent turn
  sys.stdout.write("{}\n")
  sys.stdout.flush()

  spawn_detached_trigger(
      event=event,
      cid=cid,
      project_id=project_id,
      workspace_paths=workspace_paths,
      projects_dir=projects_dir,
  )


def handle_bg_trigger(
    event: str,
    cid: str,
    project_id: str,
    workspace_paths: list[str],
    projects_dir: str,
) -> None:
  """Runs inside the detached background child spawned by the hook."""
  if event == "PreInvocation":
    sync_once(
        projects_dir=projects_dir,
        force_running_cid=cid,
        force_running_project_id=project_id,
        force_running_workspaces=workspace_paths,
    )
    run_watcher_loop(projects_dir=projects_dir)
  elif event == "Stop":
    # Wait briefly for the Language Server to transition the stopping
    # conversation from CASCADE_RUN_STATUS_RUNNING to CASCADE_RUN_STATUS_IDLE
    time.sleep(STOP_SETTLE_DELAY_SEC)
    _, total_tracked, _ = sync_once(projects_dir=projects_dir)
    if total_tracked > 0:
      run_watcher_loop(projects_dir=projects_dir)


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Sync active conversation badges into Jetski project names."
  )
  parser.add_argument(
      "--event",
      choices=["PreInvocation", "Stop"],
      help="Run in hook mode for the specified lifecycle event.",
  )
  parser.add_argument(
      "--bg-trigger",
      action="store_true",
      help="Internal detached worker mode spawned by hook invocation.",
  )
  parser.add_argument(
      "--sync-once",
      action="store_true",
      help="Run a single synchronous badge update and exit.",
  )
  parser.add_argument(
      "--watch",
      action="store_true",
      help="Run the singleton background watcher until all projects are idle.",
  )
  parser.add_argument(
      "--clear",
      action="store_true",
      help="Remove all activity badges from all project JSON files.",
  )
  parser.add_argument(
      "--status",
      action="store_true",
      help="Print current running/blocked/unread conversation counts per project.",
  )
  parser.add_argument("--cid", default="", help="Conversation ID context.")
  parser.add_argument("--project-id", default="", help="Project ID hint.")
  parser.add_argument(
      "--workspaces-json", default="", help="JSON list of workspace paths."
  )
  parser.add_argument(
      "--projects-dir",
      default=DEFAULT_PROJECTS_DIR,
      help="Path to ~/.gemini/config/projects directory.",
  )
  args = parser.parse_args()

  workspaces: list[str] = []
  if args.workspaces_json:
    try:
      parsed_ws = json.loads(args.workspaces_json)
      if isinstance(parsed_ws, list):
        workspaces = [str(x) for x in parsed_ws]
    except ValueError:
      pass

  if args.bg_trigger:
    handle_bg_trigger(
        event=args.event or "PreInvocation",
        cid=args.cid,
        project_id=args.project_id,
        workspace_paths=workspaces,
        projects_dir=args.projects_dir,
    )
    return

  if args.event:
    handle_hook_invocation(event=args.event, projects_dir=args.projects_dir)
    return

  if args.clear:
    changes = clear_all_badges(projects_dir=args.projects_dir)
    for pid, old_name, new_name in changes:
      print(f"{pid}: {old_name!r} -> {new_name!r}")
    return

  if args.status:
    summaries = fetch_trajectories()
    if summaries is None:
      print("ERROR: Could not connect to Language Server.", file=sys.stderr)
      sys.exit(1)
    known_projects, folder_to_project = load_projects(args.projects_dir)
    currently_viewing_cid, viewed_lmt = resolve_currently_viewing_cid(summaries)
    eval_cache = _load_traj_eval_cache()
    running_counts, blocked_counts, unread_counts = compute_project_activity(
        summaries,
        known_projects,
        folder_to_project,
        currently_viewing_cid=currently_viewing_cid,
        viewed_lmt=viewed_lmt,
        traj_fetcher=fetch_single_trajectory,
        eval_cache=eval_cache,
        now_ts=time.time(),
    )
    for pid, (_, pdata) in known_projects.items():
      base = strip_activity_badge(pdata.get("name", ""))
      r = running_counts.get(pid, 0)
      b = blocked_counts.get(pid, 0)
      u = unread_counts.get(pid, 0)
      print(f"{pid}  {base:<24}  running={r}  blocked={b}  unread={u}")
    return

  if args.sync_once:
    rpc_ok, total_tracked, changes = sync_once(
        projects_dir=args.projects_dir,
        force_running_cid=args.cid,
        force_running_project_id=args.project_id,
        force_running_workspaces=workspaces,
    )
    if not rpc_ok:
      print("ERROR: Could not connect to Language Server.", file=sys.stderr)
      sys.exit(1)
    for pid, old_name, new_name in changes:
      print(f"{pid}: {old_name!r} -> {new_name!r}")
    print(f"Total tracked conversations: {total_tracked}")
    return

  if args.watch:
    run_watcher_loop(projects_dir=args.projects_dir)
    return

  # Default when invoked without flags as a hook: treat as PreInvocation
  handle_hook_invocation(event="PreInvocation", projects_dir=args.projects_dir)


if __name__ == "__main__":
  main()
