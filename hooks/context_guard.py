#!/usr/bin/env python3
"""PreInvocation hook for Jetski to monitor conversation context size.

Supports two modes:
1. Turn-start handoff (`invocationNum == 0`): when total transcript size or step
   count exceeds the threshold at the start of a user turn, instructs the agent
   to answer the current request and then hand off to a new conversation.
2. Mid-turn circuit breaker (`invocationNum > 0`): when context crosses the
   threshold during a turn OR a single turn drags on for too many tool-call
   iterations (`MAX_TURN_INVOCATIONS`), instructs the agent to stop immediately
   at a safe checkpoint, record failed hypotheses to avoid repeating loops, and
   hand off to a new conversation.
"""

from datetime import datetime
import glob
import json
import os
import re
import sqlite3
import sys
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("JETSKI_TIMEZONE", "America/New_York")

# Thresholds for triggering context handoff:
# ~600 KB of full transcript (~150k tokens) or 160 trajectory steps.
MAX_FULL_TRANSCRIPT_KB = int(os.environ.get("JETSKI_MAX_TRANSCRIPT_KB", "600"))
MAX_STEPS = int(os.environ.get("JETSKI_MAX_STEPS", "160"))
# Maximum tool-call iterations within a single turn before circuit-breaking
# (set high so normal 25-step debugging turns in fresh chats do not hand off early):
MAX_TURN_INVOCATIONS = int(os.environ.get("JETSKI_MAX_TURN_INVOCATIONS", "80"))

# Snooze increments so the hook does not spam while the agent performs the handoff
# or if the user chooses to continue in the same chat:
SNOOZE_KB = 300
SNOOZE_STEPS = 80
SNOOZE_TURN_INVOCATIONS = 40

SUMMARY_DB_PATH = os.path.expanduser(
    "~/.gemini/jetski/conversation_summaries.db"
)
PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")

# Regexes to strip previous continuation prefixes/suffixes on chained handoffs:
CONT_PREFIX_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}\s+(?:продолжение|continue)\]\s*|(?:продолжение|continue)(?:\s+\d{2}:\d{2})?\s*:\s*)+",
    re.IGNORECASE,
)
CONT_SUFFIX_RE = re.compile(
    r"\s*\((?:продолжение|continue)\s+(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}\)\s*$",
    re.IGNORECASE,
)


def get_transcript_size_kb(transcript_path: str) -> float:
  if not transcript_path:
    return 0.0
  log_dir = os.path.dirname(transcript_path)
  full_path = os.path.join(log_dir, "transcript_full.jsonl")
  for path in (full_path, transcript_path):
    try:
      if os.path.exists(path):
        return os.path.getsize(path) / 1024.0
    except OSError:
      pass
  return 0.0


def get_conversation_db_info(conv_id: str) -> tuple[str, str]:
  """Returns (title, project_id) from conversation_summaries.db."""
  if not conv_id or not os.path.exists(SUMMARY_DB_PATH):
    return ("", "")
  try:
    uri = f"file:{SUMMARY_DB_PATH}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1.0) as conn:
      row = conn.execute(
          "SELECT title, project_id FROM conversation_summaries WHERE"
          " conversation_id = ?",
          (conv_id,),
      ).fetchone()
      if row:
        title = str(row[0]).strip() if row[0] else ""
        project_id = str(row[1]).strip() if row[1] else ""
        return (title, project_id)
  except Exception:
    pass
  return ("", "")


def infer_project_id_from_workspaces(workspace_paths: list[str]) -> str:
  """Infers a project_id strictly from the conversation's active workspace_paths."""
  if not workspace_paths or not os.path.isdir(PROJECTS_DIR):
    return ""
  folder_to_project: list[tuple[str, str]] = []
  for pfile in glob.glob(os.path.join(PROJECTS_DIR, "*.json")):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = pdata.get("id", "")
      resources = pdata.get("projectResources", {}).get("resources", [])
      for r in resources:
        uri = r.get("gitFolder", {}).get("folderUri", "")
        if uri.startswith("file://") and pid:
          folder_path = uri[len("file://") :].rstrip("/")
          if folder_path:
            folder_to_project.append((folder_path, pid))
    except Exception:
      pass

  for wp in workspace_paths:
    for folder_path, pid in folder_to_project:
      if wp == folder_path or wp.startswith(folder_path + "/"):
        return pid

  return ""


# Tool results may indent this line (e.g. "\n\n\t\t\t\tThe command exited ..." on
# macOS Jetski), so allow leading whitespace; otherwise the guard never fires.
EXIT_CODE_RE = re.compile(r"^\s*The command exited with code (\d+)\.", re.MULTILINE)
FAILURE_STREAK_THRESHOLD = int(os.environ.get("JETSKI_FAIL_STREAK", "2"))
BENIGN_EXIT1_CMD_RE = re.compile(
    r"^\s*(?:git\s+(?:grep|diff|check-ignore)|grep|egrep|fgrep|rg)\b"
)


def parse_current_turn_steps(transcript_path: str) -> list[tuple[int, dict]]:
  """Returns ordered (step_index, step_obj) after the last USER_INPUT step."""
  if not transcript_path or not os.path.exists(transcript_path):
    return []
  steps_by_idx = {}
  try:
    with open(transcript_path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          obj = json.loads(line)
          idx = obj.get("step_index")
          if idx is not None:
            steps_by_idx[int(idx)] = obj
        except Exception:
          continue
  except Exception:
    return []
  if not steps_by_idx:
    return []
  ordered_indices = sorted(steps_by_idx.keys())
  last_user_idx = -1
  for idx in ordered_indices:
    if steps_by_idx[idx].get("type") == "USER_INPUT":
      last_user_idx = idx
  return [
      (idx, steps_by_idx[idx]) for idx in ordered_indices if idx > last_user_idx
  ]


def get_consecutive_cmd_failures(transcript_path: str) -> tuple[int, int]:
  """Returns (consecutive_failures_at_tail, last_cmd_step_idx) for the current turn."""
  turn_steps = parse_current_turn_steps(transcript_path)
  if not turn_steps:
    return 0, -1
  streak = 0
  last_cmd_idx = -1
  pending_cmd_idx = None
  pending_cmd_str = ""
  for idx, step in turn_steps:
    stype = step.get("type", "")
    tool_calls = step.get("tool_calls") or []
    if stype == "PLANNER_RESPONSE" and tool_calls:
      for tc in tool_calls:
        if tc.get("name") == "run_command":
          pending_cmd_idx = idx
          pending_cmd_str = str((tc.get("args") or {}).get("CommandLine", ""))
    elif stype == "GENERIC" and pending_cmd_idx is not None:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content)
      if m:
        code = int(m.group(1))
        # Ignore exit code 1 from grep/rg/git check-ignore ("no matches found")
        if code == 1 and BENIGN_EXIT1_CMD_RE.search(pending_cmd_str):
          pending_cmd_idx = None
          pending_cmd_str = ""
          continue
        last_cmd_idx = pending_cmd_idx
        if code != 0:
          streak += 1
        else:
          streak = 0
        pending_cmd_idx = None
        pending_cmd_str = ""
  return streak, last_cmd_idx


def build_agentapi_prefix(
    conv_id: str, _transcript_path: str, workspace_paths: list[str]
) -> str:
  """Builds the command prefix for agentapi new-conversation with project_id.

  Always unsets ANTIGRAVITY_SOURCE_METADATA so the new conversation does not
  receive parentConversationId (which would make hooks treat it as a subagent).
  Preserves the conversation's project_id if it belongs to a project, or
  explicitly unsets ANTIGRAVITY_PROJECT_ID if the conversation is outside-of-project.
  """
  _, current_project_id = get_conversation_db_info(conv_id)
  if current_project_id and current_project_id != "outside-of-project":
    return (
        "env -u ANTIGRAVITY_SOURCE_METADATA"
        f' ANTIGRAVITY_PROJECT_ID="{current_project_id}" agentapi'
        " new-conversation"
    )

  target_pid = infer_project_id_from_workspaces(workspace_paths)
  if target_pid:
    return (
        "env -u ANTIGRAVITY_SOURCE_METADATA"
        f' ANTIGRAVITY_PROJECT_ID="{target_pid}" agentapi new-conversation'
    )
  return (
      'env -u ANTIGRAVITY_SOURCE_METADATA ANTIGRAVITY_PROJECT_ID="outside-of-project"'
      " agentapi new-conversation"
  )


def get_handoff_launch_step_in_turn(transcript_path: str) -> int:
  """Returns step_index where PLANNER_RESPONSE actually succeeded running agentapi new-conversation in this turn, or -1."""
  turn_steps = parse_current_turn_steps(transcript_path)
  launch_idx = -1
  pending_launch_idx = None
  for idx, step in turn_steps:
    stype = step.get("type", "")
    if stype == "PLANNER_RESPONSE":
      pending_launch_idx = None
      for tc in step.get("tool_calls") or []:
        if tc.get("name") == "run_command":
          cmd = str((tc.get("args") or {}).get("CommandLine", ""))
          if "agentapi" in cmd and "new-conversation" in cmd:
            pending_launch_idx = idx
    elif stype == "GENERIC" and pending_launch_idx is not None:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      if (
          step.get("status") == "DONE"
          and "The command exited with code 0." in content[:300]
          and '"newConversation"' in content
      ):
        launch_idx = pending_launch_idx
        pending_launch_idx = None
  return launch_idx


def build_continuation_title(conv_id: str, fallback_text: str = "") -> str:
  raw_title, _ = get_conversation_db_info(conv_id)
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()

  probe_text = base_title or fallback_text
  has_cyrillic = bool(re.search(r"[а-яА-ЯёЁ]", probe_text))
  cont_word = "продолжение" if has_cyrillic else "continue"
  try:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    now_tz = datetime.now(tz)
    cutoff = datetime(2026, 9, 28, 6, 0, tzinfo=tz)
    time_str = (
        now_tz.strftime("%H:%M")
        if now_tz >= cutoff
        else datetime.now(ZoneInfo("UTC")).strftime("%H:%M")
    )
  except Exception:
    time_str = datetime.now().strftime("%H:%M")

  if base_title:
    return f"[{time_str} {cont_word}] {base_title}"
  return f"[{time_str} {cont_word}] <Chat Title>"


def load_state(state_path: str) -> dict:
  try:
    if os.path.exists(state_path):
      with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)
  except Exception:
    pass
  return {}


def save_state(state_path: str, state: dict) -> None:
  try:
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
      json.dump(state, f, indent=2)
  except Exception:
    pass


def main() -> None:
  try:
    raw_input = sys.stdin.read().strip()
    if not raw_input:
      print("{}")
      return
    data = json.loads(raw_input)
  except Exception:
    print("{}")
    return

  # Skip battle mode forks and subagents
  if data.get("isBattleMode") or data.get("parentConversationId"):
    print("{}")
    return

  conv_id = data.get("conversationId", "")
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")
  workspace_paths = data.get("workspacePaths", [])
  steps = int(data.get("initialNumSteps", 0))
  invocation_num = int(data.get("invocationNum", 0))
  last_user_input = data.get("lastUserInput", "")

  size_kb = get_transcript_size_kb(transcript_path)

  state_path = (
      os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      if artifact_dir
      else f"/tmp/jetski_context_guard_{conv_id}.json"
  )
  state = load_state(state_path)

  raw_title, _ = get_conversation_db_info(conv_id)
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()
  slug = re.sub(r"[^\w\-]+", "_", base_title, flags=re.UNICODE).strip("_")[:40]
  short_id = conv_id[:8] if conv_id else "unknown"
  handoff_filename = (
      f"handoff_summary_{slug}_{short_id}.md"
      if slug
      else f"handoff_summary_{short_id}.md"
  )
  handoff_file = (
      os.path.join(artifact_dir, handoff_filename)
      if artifact_dir
      else f"/tmp/{handoff_filename}"
  )

  # Reset per-turn invocation threshold at the start of each new user turn
  if invocation_num == 0:
    next_turn_inv_threshold = MAX_TURN_INVOCATIONS
    if (
        state.get("next_turn_inv_threshold") != MAX_TURN_INVOCATIONS
        or state.get("pending_handoff_launch")
        or state.get("handoff_completed")
    ):
      state["next_turn_inv_threshold"] = MAX_TURN_INVOCATIONS
      state["pending_handoff_launch"] = False
      state["handoff_completed"] = False
      save_state(state_path, state)

    # Safety net: ensure browser sounds are re-enabled when new turn/chat starts
    try:
      config_path = os.path.expanduser("~/.gemini/config/config.json")
      if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
          c = json.load(f)
        user_settings = c.setdefault("userSettings", {})
        if (
            user_settings.get("enableSoundsForSpecialEvents") is False
            or "enableSoundsForSpecialEvents" in c
        ):
          c.pop("enableSoundsForSpecialEvents", None)
          user_settings["enableSoundsForSpecialEvents"] = True
          with open(config_path, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2)
    except Exception:
      pass
  else:
    next_turn_inv_threshold = int(
        state.get("next_turn_inv_threshold", MAX_TURN_INVOCATIONS)
    )

  if invocation_num > 0:
    launch_step = get_handoff_launch_step_in_turn(transcript_path)
    if launch_step >= 0:
      if state.get("pending_handoff_launch") or not state.get(
          "handoff_completed"
      ):
        state["pending_handoff_launch"] = False
        state["handoff_completed"] = True
        save_state(state_path, state)
      # Check if a background task / subagent woke the conversation up after handoff
      # or if the agent is still calling tools after launching the continuation:
      turn_steps = parse_current_turn_steps(transcript_path)
      steps_after_launch = [
          s for idx, s in turn_steps if idx > launch_step and s.get("type") != "GENERIC"
      ]
      if steps_after_launch:
        stop_msg = (
            f"[CONTEXT GUARD: CHAT ALREADY HANDED OFF] Warning: this conversation ALREADY launched a continuation chat "
            f"(at step #{launch_step}), but resumed execution (e.g., woken up by a background `task` or subagent). "
            f"It is STRICTLY FORBIDDEN to continue debugging, editing files, or launching a second continuation chat here! "
            f"If any background tasks remain active, terminate them via `manage_task` (`kill`) and end your turn immediately."
        )
        print(
            json.dumps(
                {"injectSteps": [{"ephemeralMessage": stop_msg}]},
                ensure_ascii=False,
            )
        )
        return
      print("{}")
      return

  # Follow-up check: if handoff was triggered in a previous step of this turn,
  # and the agent wrote handoff_summary.md in a separate step without calling
  # agentapi new-conversation yet, remind it on EVERY step until it launches the chat!
  if invocation_num > 0 and state.get("pending_handoff_launch"):
    new_title = state.get("pending_title") or build_continuation_title(
        conv_id, last_user_input
    )
    cmd_prefix = state.get("pending_cmd_prefix") or build_agentapi_prefix(
        conv_id, transcript_path, workspace_paths
    )
    summary_exists = os.path.exists(handoff_file)
    step1_text = (
        ""
        if summary_exists
        else (
            f"1. FIRST create the summary artifact `{handoff_file}` via `write_to_file` (`UserFacing: true`) "
            f"(it does NOT exist on disk yet — do not launch the continuation chat without creating it!).\n2. THEN "
        )
    )
    reminder_msg = (
        f"[CONTEXT GUARD REMINDER] You have NOT completed the handoff yet! "
        f"IMMEDIATELY stop all other investigation/debugging actions:\n"
        f"{step1_text}call `run_command` with this EXACT command (including `env -u` and `--title`):\n"
        f'   `{cmd_prefix} --model=pro --title="{new_title}" '
        f'"Continuing unfinished task from previous conversation (conversation://{conv_id}), interrupted due to context limits. '
        f"Read {handoff_file} via view_file, review completed steps and discarded hypotheses, and continue from the next step.\"`\n"
        f"After receiving `conversationId`, immediately finish your turn and provide a clickable link to the user: "
        f"`[👉 {new_title}](conversation://<new_conversation_id>)`."
    )
    print(
        json.dumps(
            {"injectSteps": [{"ephemeralMessage": reminder_msg}]},
            ensure_ascii=False,
        )
    )
    return

  next_kb_threshold = float(
      state.get("next_kb_threshold", MAX_FULL_TRANSCRIPT_KB)
  )
  next_steps_threshold = int(state.get("next_steps_threshold", MAX_STEPS))

  size_or_steps_exceeded = (
      size_kb >= next_kb_threshold or steps >= next_steps_threshold
  )
  turn_loop_exceeded = (
      invocation_num > 0 and invocation_num >= next_turn_inv_threshold
  )

  if not (size_or_steps_exceeded or turn_loop_exceeded):
    if invocation_num > 0:
      streak, last_cmd_idx = get_consecutive_cmd_failures(transcript_path)
      if (
          streak >= FAILURE_STREAK_THRESHOLD
          and streak % FAILURE_STREAK_THRESHOLD == 0
          and last_cmd_idx != state.get("last_warned_fail_step")
      ):
        state["last_warned_fail_step"] = last_cmd_idx
        save_state(state_path, state)
        fail_msg = (
            f"[TWO-STRIKE DEBUG GUARD] Warning: {streak} consecutive commands in the current turn failed. "
            f"DO NOT make a third blind guess! Stop, read the exact error output and source declarations (`.h` / docs), "
            f"revert broken edits if needed (`git diff` / `git checkout`), and revise your hypothesis before proceeding."
        )
        print(
            json.dumps(
                {"injectSteps": [{"ephemeralMessage": fail_msg}]},
                ensure_ascii=False,
            )
        )
        return
    print("{}")
    return

  new_title = build_continuation_title(conv_id, last_user_input)
  cmd_prefix = build_agentapi_prefix(conv_id, transcript_path, workspace_paths)

  # Update state with snoozed thresholds so tool calls needed for the handoff
  # itself (write_to_file + run_command) do not re-trigger the main alert,
  # while setting pending_handoff_launch=True to catch split tool calls.
  new_state = {
      "last_triggered_kb": round(size_kb, 1),
      "last_triggered_steps": steps,
      "last_triggered_invocation_num": invocation_num,
      "pending_handoff_launch": True,
      "handoff_completed": False,
      "pending_title": new_title,
      "pending_cmd_prefix": cmd_prefix,
      "pending_handoff_file": handoff_file,
      "next_kb_threshold": (
          round(max(size_kb, next_kb_threshold) + SNOOZE_KB, 1)
          if size_or_steps_exceeded
          else next_kb_threshold
      ),
      "next_steps_threshold": (
          max(steps, next_steps_threshold) + SNOOZE_STEPS
          if size_or_steps_exceeded
          else next_steps_threshold
      ),
      "next_turn_inv_threshold": (
          max(invocation_num, next_turn_inv_threshold) + SNOOZE_TURN_INVOCATIONS
          if invocation_num > 0
          else MAX_TURN_INVOCATIONS
      ),
  }
  save_state(state_path, new_state)

  if invocation_num > 0:
    reason = (
        f"single-turn iteration limit exceeded ({invocation_num} consecutive steps)"
        if turn_loop_exceeded and not size_or_steps_exceeded
        else f"context threshold exceeded mid-turn ({size_kb:.0f} KB / {steps} steps, turn iteration #{invocation_num})"
    )
    msg = (
        f"[CONTEXT GUARD: MID-TURN CIRCUIT BREAKER] Warning: {reason}. "
        f"Continuing complex debugging or code generation in an oversized context will degrade quality and cause errors.\n"
        f"Follow the mid-turn emergency handoff protocol:\n"
        f"1. DO NOT try to finish the entire task in this conversation. If any file is currently left in a broken/half-edited state, bring it to a clean checkpoint and stop further attempts. "
        f"If any background tasks or subagents are running in this conversation, terminate them first via `manage_task` (`kill`) / `manage_subagents` (`kill_all`) so they do not wake this chat up after handoff!\n"
        f"2. Create or update the artifact `{handoff_file}` (via write_to_file, UserFacing: true), documenting:\n"
        f"   - Original goal of the task;\n"
        f"   - What has been completed and which files were modified;\n"
        f"   - Which hypotheses/approaches were tested and DID NOT work (to avoid repeating them in the new chat);\n"
        f"   - Exact next step to resume from;\n"
        f"   - Link `[Previous Conversation](conversation://{conv_id})`.\n"
        f"3. SIMULTANEOUSLY (in the same step or immediately next) launch the new conversation via run_command (use this EXACT command and `--title`):\n"
        f'   `{cmd_prefix} --model=pro --title="{new_title}" '
        f'"Continuing unfinished task from previous conversation (conversation://{conv_id}), interrupted due to context limits. '
        f"Read {handoff_file} via view_file, review completed steps and discarded hypotheses, and continue from the next step.\"`\n"
        f"4. Immediately finish your turn, explain to the user which safe checkpoint you stopped at, and provide the link: "
        f"`[👉 {new_title}](conversation://<new_conversation_id>)`."
    )
  else:
    msg = (
        f"[CONTEXT GUARD ALERT] Current conversation size has reached the threshold "
        f"({size_kb:.0f} KB / {steps} steps). To prevent context degradation and hallucinations, "
        f"perform an automatic handoff to a new conversation:\n"
        f"1. First, completely answer the user's current request.\n"
        f"2. Create or update the summary file `{handoff_file}` (via write_to_file, UserFacing: true), "
        f"recording: task goal, key decisions made, modified files, "
        f"current status, next steps, and a link `[Previous Conversation](conversation://{conv_id})`.\n"
        f"3. Call `run_command` to launch the new conversation (use this EXACT command and `--title`):\n"
        f'   `{cmd_prefix} --model=pro --title="{new_title}" '
        f'"Continuing work from previous conversation (conversation://{conv_id}). '
        f"Read the context file {handoff_file} via view_file and briefly confirm readiness to continue.\"`\n"
        f"4. At the very end of your response to the user, include a prominent clickable link "
        f"to the new conversation: `[👉 {new_title}](conversation://<new_conversation_id>)`."
    )

  result = {"injectSteps": [{"ephemeralMessage": msg}]}
  print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
  main()
