#!/usr/bin/env python3
"""PreInvocation hook for Jetski to monitor conversation context size.

The metric is the Language Server's own estimate of the context window usage
(estimatedTokensUsed / maxContextTokens of the last model call). Levels:
1. Soft (SOFT_PCT): a throttled reminder to hand off at the nearest
   micro-boundary (test finished, hypothesis settled, commit, subagent back).
2. Hard (HARD_PCT, or history already compacted): hand off now. At turn start
   (`invocationNum == 0`) the agent first answers the user and stop_guard blocks
   a stop without handoff; mid-turn it is reminded on every step until the
   continuation is launched. Nothing is snoozed across turns.
3. Mid-turn circuit breaker: a single turn with too many tool-call iterations
   (`MAX_TURN_INVOCATIONS`) hands off as well.
Every invocation appends one JSONL record to the guard log so silent misses
are visible afterwards.
"""

from datetime import datetime, timezone
import glob
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import unquote
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("JETSKI_TIMEZONE", "America/New_York")

# Percent of the model's context window
SOFT_PCT = float(os.environ.get("JETSKI_CONTEXT_SOFT_PCT", "60"))
HARD_PCT = float(os.environ.get("JETSKI_CONTEXT_HARD_PCT", "80"))
RPC_TIMEOUT_S = 1.5
# Fallback when the RPC fails: real transcripts average ~2 bytes per token
# (a 360 KB transcript_full.jsonl was 176k tokens), not the ~4 often assumed
FALLBACK_BYTES_PER_TOKEN = 2.0
FALLBACK_MAX_TOKENS = 256000
# initialNumSteps grows by ~2 per invocation, so this re-reminds about every
# 10 invocations without persisting a counter on every call
SOFT_REMIND_EVERY_STEPS = 20
# Maximum tool-call iterations within a single turn before circuit-breaking
# (set high so normal 25-step debugging turns in fresh chats do not hand off early):
MAX_TURN_INVOCATIONS = int(os.environ.get("JETSKI_MAX_TURN_INVOCATIONS", "80"))
# Lets the agent finish the handoff tool calls without re-tripping the breaker
SNOOZE_TURN_INVOCATIONS = 40

DEFAULT_LOG_PATH = "~/.gemini/config/logs/context_guard.jsonl"
LOG_MAX_BYTES = 1_000_000
LIFECYCLE_CLI = "~/.gemini/config/hooks/chat_lifecycle.py"
ROADMAP_CLI = "~/.gemini/config/hooks/roadmap.py"

# App data dirs of the supported clients, in lookup order. Antigravity keeps
# conversation_summaries.db (with project ids) in ~/.gemini/antigravity; some
# Jetski builds use ~/.gemini/jetski (standalone macOS Jetski has no DB at all).
APP_DATA_DIR_CANDIDATES = ("~/.gemini/antigravity", "~/.gemini/jetski")
SUMMARY_DB_NAME = "conversation_summaries.db"
PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")

# Regexes to strip previous continuation prefixes/suffixes on chained handoffs:
CONT_PREFIX_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\s+(?:продолжение|continue))?\]\s*|(?:продолжение|continue)(?:\s+\d{2}:\d{2})?\s*:\s*)+",
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


def app_data_dir_from_path(path: str) -> str:
  """Returns <appDataDir> for a path like <appDataDir>/brain/<conv-id>/..., or ""."""
  if not path:
    return ""
  parts = os.path.abspath(os.path.expanduser(path)).split(os.sep)
  for i in range(len(parts) - 2, 0, -1):
    if parts[i] == "brain":
      return os.sep.join(parts[:i]) or os.sep
  return ""


def summary_db_paths(hint_path: str = "") -> list[str]:
  """Existing conversation_summaries.db paths, most likely first (deduplicated)."""
  dirs = [app_data_dir_from_path(hint_path)] + [
      os.path.expanduser(d) for d in APP_DATA_DIR_CANDIDATES
  ]
  paths: list[str] = []
  for d in dirs:
    if not d:
      continue
    p = os.path.join(os.path.abspath(d), SUMMARY_DB_NAME)
    if p not in paths and os.path.isfile(p):
      paths.append(p)
  return paths


def _query_summary_db(db_path: str, conv_id: str):
  """Returns the (title, project_id) row or None.

  `mode=ro` cannot open a WAL database whose -shm file is absent (e.g. while
  the client is not holding it open), so fall back to `immutable=1`, which may
  miss the newest un-checkpointed rows but never writes.
  """
  query = (
      "SELECT title, project_id FROM conversation_summaries WHERE"
      " conversation_id = ?"
  )
  for flags in ("mode=ro", "mode=ro&immutable=1"):
    try:
      conn = sqlite3.connect(f"file:{db_path}?{flags}", uri=True, timeout=1.0)
      try:
        return conn.execute(query, (conv_id,)).fetchone()
      finally:
        conn.close()
    except Exception:
      continue
  return None


def get_conversation_db_info(
    conv_id: str, hint_path: str = ""
) -> tuple[str, str]:
  """Returns (title, project_id) from conversation_summaries.db.

  `hint_path` (transcript or artifact path) selects the current client's app
  data dir first; otherwise the known client dirs are searched in order.
  """
  if not conv_id:
    return ("", "")
  for db_path in summary_db_paths(hint_path):
    row = _query_summary_db(db_path, conv_id)
    if row:
      title = str(row[0]).strip() if row[0] else ""
      project_id = str(row[1]).strip() if row[1] else ""
      return (title, project_id)
  return ("", "")


def _file_uri_to_path(uri: str) -> str:
  if not uri.startswith("file://"):
    return ""
  return unquote(uri[len("file://") :]).rstrip("/")


def infer_project_id_from_workspaces(workspace_paths: list[str]) -> str:
  """Infers a project_id strictly from the conversation's active workspace_paths.

  Considers both `gitFolder.folderUri` and plain `folderUri` resources; the
  most specific (longest) matching folder wins, so a workspace inside
  ~/Sources/skia maps to the skia project rather than a ~/Sources project.
  """
  if not workspace_paths or not os.path.isdir(PROJECTS_DIR):
    return ""
  folder_to_project: list[tuple[str, str]] = []
  for pfile in sorted(glob.glob(os.path.join(PROJECTS_DIR, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = pdata.get("id", "")
      if not pid:
        continue
      resources = pdata.get("projectResources", {}).get("resources", [])
      for r in resources:
        for uri in (
            (r.get("gitFolder") or {}).get("folderUri", ""),
            r.get("folderUri", ""),
        ):
          folder_path = _file_uri_to_path(uri)
          if folder_path:
            folder_to_project.append((folder_path, pid))
    except Exception:
      pass

  best_len, best_pid = -1, ""
  for wp in workspace_paths:
    wp = wp.rstrip("/")
    for folder_path, pid in folder_to_project:
      if wp == folder_path or wp.startswith(folder_path + "/"):
        if len(folder_path) > best_len:
          best_len, best_pid = len(folder_path), pid
  return best_pid


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
    conv_id: str, transcript_path: str, workspace_paths: list[str]
) -> str:
  """Builds the command prefix for agentapi new-conversation with project_id.

  Always unsets ANTIGRAVITY_SOURCE_METADATA so the new conversation does not
  receive parentConversationId (which would make hooks treat it as a subagent).
  Preserves the conversation's project_id if it belongs to a project, or
  explicitly unsets ANTIGRAVITY_PROJECT_ID if the conversation is outside-of-project.
  """
  _, current_project_id = get_conversation_db_info(conv_id, transcript_path)
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


def is_handoff_launch_command(cmd: str) -> bool:
  """Returns True if cmd invokes `agentapi new-conversation` or `chat_lifecycle.py handoff`."""
  if "agentapi" in cmd and "new-conversation" in cmd:
    return True
  if re.search(r"\bchat_lifecycle\.py\s+handoff\b", cmd):
    return True
  return False


def get_handoff_launch_step_in_turn(transcript_path: str) -> int:
  """Returns step_index where PLANNER_RESPONSE actually succeeded launching a handoff chat in this turn, or -1."""
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
          if is_handoff_launch_command(cmd):
            pending_launch_idx = idx
    elif stype == "GENERIC" and pending_launch_idx is not None:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content[:300])
      if (
          step.get("status") == "DONE"
          and m
          and int(m.group(1)) == 0
          and ('"newConversation"' in content or '"new_conversation_id"' in content)
      ):
        launch_idx = pending_launch_idx
        pending_launch_idx = None
  return launch_idx


CONTINUATION_ID_RE = re.compile(r'"(?:new_conversation_id|conversationId)":\s*"([A-Za-z0-9_\-]+)"')


def get_continuation_id(transcript_path: str, launch_step: int) -> str:
  """Id of the chat launched at launch_step, from its command output, or ""."""
  for idx, step in parse_current_turn_steps(transcript_path):
    if idx > launch_step and step.get("type") == "GENERIC":
      m = CONTINUATION_ID_RE.search(str(step.get("content", "")))
      if m:
        return m.group(1)
  return ""


def build_continuation_title(conv_id: str, fallback_text: str = "") -> str:
  raw_title, _ = get_conversation_db_info(conv_id)
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()

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
    return f"[{time_str}] {base_title}"
  return f"[{time_str}] <Chat Title>"


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


def measure_context(conv_id: str, transcript_path: str, gm_count_hint: int) -> dict:
  """Context usage of the last model call via the LS RPC, else a KB estimate.

  Returns pct/tokens/max/ckpt plus `fallback`, `err` and `gm_count` (number of
  generator metadata entries). The count is reused as the next offset because
  every entry embeds the full system prompt, so an unpaged response grows by
  ~8 KB per model call.
  """
  m = {"fallback": False, "err": "", "gm_count": gm_count_hint}
  t0 = time.monotonic()
  try:
    import chat_lifecycle

    gm = []
    for off in (gm_count_hint - 1, 0) if gm_count_hint > 1 else (0,):
      # Retry from 0 only on a quick empty answer, never after a slow one,
      # so two RPC timeouts cannot add up past the hook's 5 s budget
      if off == 0 and gm_count_hint > 1 and time.monotonic() - t0 > RPC_TIMEOUT_S:
        break
      resp = chat_lifecycle.ls_rpc(
          "GetCascadeTrajectoryGeneratorMetadata",
          {
              "cascadeId": conv_id,
              "generatorMetadataOffset": off,
              "includeMessages": False,
          },
          timeout=RPC_TIMEOUT_S,
      )
      gm = resp.get("generatorMetadata") or []
      if gm:
        m["gm_count"] = off + len(gm)
        break
    for entry in reversed(gm):
      csm = (entry.get("chatModel") or {}).get("chatStartMetadata") or {}
      cwm = csm.get("contextWindowMetadata") or {}
      if "estimatedTokensUsed" in cwm and int(cwm.get("maxContextTokens") or 0) > 0:
        used = int(cwm["estimatedTokensUsed"])
        cap = int(cwm["maxContextTokens"])
        m.update(
            pct=100.0 * used / cap,
            tokens=used,
            max=cap,
            # The LS sends -1 until the first compaction and then omits the
            # field, because proto3 JSON drops the zero value of checkpoint #0
            ckpt=int(csm.get("checkpointIndex", 0)),
        )
        return m
    m["err"] = "no contextWindowMetadata"
  except Exception as e:
    m["err"] = f"{type(e).__name__}: {e}"[:200]
  tokens = int(
      get_transcript_size_kb(transcript_path) * 1024 / FALLBACK_BYTES_PER_TOKEN
  )
  m.update(
      pct=100.0 * tokens / FALLBACK_MAX_TOKENS,
      tokens=tokens,
      max=FALLBACK_MAX_TOKENS,
      ckpt=-1,
      fallback=True,
  )
  return m


def resolve_handoff_target(
    conv_id: str, transcript_path: str, artifact_dir: str
) -> dict:
  """Where the agent records its state for the handoff (reads SQLite, so trigger paths only).

  In-project chats use the chain state file next to the project roadmap;
  outside-project chats keep the handoff_summary artifact.
  """
  raw_title, project_id = get_conversation_db_info(conv_id, transcript_path)
  if project_id and project_id != "outside-of-project":
    try:
      import roadmap

      return {"in_project": True, "file": roadmap.state_path(project_id, conv_id)}
    except Exception:
      # An id that cannot be a path component (or a broken roadmap module)
      # must not take the guard down; the summary flow still works
      pass
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()
  slug = re.sub(r"[^\w\-]+", "_", base_title, flags=re.UNICODE).strip("_")[:40]
  short_id = conv_id[:8] if conv_id else "unknown"
  handoff_filename = (
      f"handoff_summary_{slug}_{short_id}.md"
      if slug
      else f"handoff_summary_{short_id}.md"
  )
  return {
      "in_project": False,
      "file": (
          os.path.join(artifact_dir, handoff_filename)
          if artifact_dir
          else f"/tmp/{handoff_filename}"
      ),
  }


TASKS_NOTE = (
    "Running subagents/background tasks: wait for them if they finish within a"
    " couple of minutes; background tasks are killed when the handoff runs, so"
    " record what was running and the exact command to restart it."
)


def handoff_steps(conv_id: str, target: dict, new_title: str) -> str:
  if target["in_project"]:
    return (
        f"1. Roadmap: `python3 {ROADMAP_CLI} set {conv_id} <ID> done|doing|todo`"
        " (`add` for new work); never edit roadmap files directly.\n"
        f"2. Overwrite the chain state file `{target['file']}` (write_to_file):"
        " goal, current state with evidence (modified files, last build/test"
        " result), failed hypotheses, running subagents/background tasks and how"
        " to restart them, exact next step. It moves to the continuation as is.\n"
        f'3. Run `python3 {LIFECYCLE_CLI} handoff {conv_id} --next "<1-line next'
        ' action>"`.\n'
        "4. In your final message paste the roadmap block from the handoff output"
        f" and the link `[👉 {new_title}](conversation://<new_conversation_id>)`,"
        " then end the turn."
    )
  return (
      f"1. Write `{target['file']}` (write_to_file, UserFacing: true): goal, Epic"
      " Roadmap checklist ([x] done / [ ] pending, never drop pending items from"
      " earlier handoffs), modified files, failed hypotheses, running"
      " subagents/background tasks and how to restart them, exact next step,"
      f" link `[Previous Conversation](conversation://{conv_id})`.\n"
      f'2. Run `python3 {LIFECYCLE_CLI} handoff {conv_id} "{target["file"]}"'
      ' --next "<1-line next action>"`.\n'
      f"3. End the turn with the link `[👉 {new_title}](conversation://<new_conversation_id>)`."
  )


def _usage_str(m: dict) -> str:
  est = ", estimated from transcript size" if m["fallback"] else ""
  return (
      f"{m['pct']:.0f}% of the context window"
      f" ({m['tokens'] // 1000}k/{m['max'] // 1000}k tokens{est})"
  )


def _inject(*messages: str) -> dict:
  return {"injectSteps": [{"ephemeralMessage": msg} for msg in messages]}


def append_log(record: dict) -> None:
  path = os.path.expanduser(
      os.environ.get("JETSKI_CONTEXT_GUARD_LOG") or DEFAULT_LOG_PATH
  )
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # One-step rotation bounds the log at ~2x LOG_MAX_BYTES; a rotation race
    # between concurrent chats can only drop a few records
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
      os.replace(path, path + ".1")
    with open(path, "a", encoding="utf-8") as f:
      f.write(json.dumps(record, ensure_ascii=False) + "\n")
  except Exception:
    pass


def run(data: dict, log: dict) -> dict:
  """Decides the hook output for one PreInvocation event and fills `log`."""
  # Skip battle mode forks and subagents
  if data.get("isBattleMode") or data.get("parentConversationId"):
    log["level"] = "skip"
    return {}

  conv_id = data.get("conversationId", "")
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")
  workspace_paths = data.get("workspacePaths", [])
  steps = int(data.get("initialNumSteps", 0))
  invocation_num = int(data.get("invocationNum", 0))
  last_user_input = data.get("lastUserInput", "")
  log.update(conv=conv_id, inv=invocation_num, steps=steps)

  state_path = (
      os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      if artifact_dir
      else f"/tmp/jetski_context_guard_{conv_id}.json"
  )
  state = load_state(state_path)

  # Reset per-turn state at the start of each new user turn; a hard trigger
  # that was ignored last turn is re-evaluated below rather than snoozed
  if invocation_num == 0:
    next_turn_inv_threshold = MAX_TURN_INVOCATIONS
    if (
        state.get("next_turn_inv_threshold") != MAX_TURN_INVOCATIONS
        or state.get("pending_handoff_launch")
        or state.get("handoff_completed")
    ):
      state["next_turn_inv_threshold"] = MAX_TURN_INVOCATIONS
      state["pending_handoff_launch"] = False
      state["pending_turn_start"] = False
      state["handoff_completed"] = False
      save_state(state_path, state)

    # NOTE: No automatic reopening of finalized chats. Lifecycle markers are
    # applied mechanically only by `chat_lifecycle.py` (set-title / handoff /
    # finalize / reopen), never inferred from user wording.

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
      log["level"] = "post_handoff"
      if (
          state.get("pending_handoff_launch")
          or not state.get("handoff_completed")
          or "handed_off_to" not in state
      ):
        state["pending_handoff_launch"] = False
        state["handoff_completed"] = True
        state["handed_off_to"] = get_continuation_id(transcript_path, launch_step)
        save_state(state_path, state)
      # Check if a background task / subagent woke the conversation up after handoff
      # or if the agent is still calling tools after launching the continuation:
      turn_steps = parse_current_turn_steps(transcript_path)
      steps_after_launch = [
          s for idx, s in turn_steps if idx > launch_step and s.get("type") != "GENERIC"
      ]
      if steps_after_launch:
        return _inject(
            f"[CONTEXT GUARD: CHAT ALREADY HANDED OFF] Warning: this conversation ALREADY launched a continuation chat "
            f"(at step #{launch_step}), but resumed execution (e.g., woken up by a background `task` or subagent). "
            f"It is STRICTLY FORBIDDEN to continue debugging, editing files, or launching a second continuation chat here! "
            f"If any background tasks remain active, terminate them via `manage_task` (`kill`) and end your turn immediately."
        )
      return {}

  # The chat is closed for good once it handed off: work done here would
  # split the task across two chats, so the user is sent to the continuation
  # once per turn and the context triggers no longer apply
  if "handed_off_to" in state:
    log["level"] = "closed"
    if invocation_num > 0:
      return {}
    cont = state["handed_off_to"]
    where = (
        f"conversation://{cont}"
        if cont
        else f"its continuation (`python3 {LIFECYCLE_CLI} status {conv_id}` shows it)"
    )
    forward = (
        f"`agentapi send-message {cont} \"<the user's message verbatim>\"`"
        if cont
        else "`agentapi send-message <continuation id> \"<the user's message verbatim>\"`"
    )
    return _inject(
        f"[CONTEXT GUARD: CLOSED CHAT] This conversation was already handed off to {where}."
        " If this turn was started by a background task or subagent, end it without a reply."
        " Otherwise open your reply by telling the user that this is an old, closed chat and"
        " that continuing here splits the work across chats and causes confusion. Do not work"
        " on the request here; offer to forward the user's message to the continuation and,"
        f" once the user agrees, run {forward} and end the turn."
    )

  m = measure_context(conv_id, transcript_path, int(state.get("gm_count", 0)))
  log.update(
      pct=round(m["pct"], 1),
      tokens=m["tokens"],
      max=m["max"],
      ckpt=m["ckpt"],
      fallback=m["fallback"],
  )
  if m["err"]:
    log["err"] = m["err"]
  if m["gm_count"] != state.get("gm_count", 0):
    state["gm_count"] = m["gm_count"]
    save_state(state_path, state)

  pending = bool(state.get("pending_handoff_launch"))
  # A turn-start trigger lets the agent answer the user first; stop_guard
  # enforces the handoff at the end of the turn, so no mid-turn nagging
  turn_start_pending = pending and bool(state.get("pending_turn_start"))
  if invocation_num > 0 and pending and not turn_start_pending:
    log["level"] = "pending"
    handoff_file = state.get("pending_handoff_file", "")
    missing = (
        ""
        if os.path.exists(handoff_file)
        else f" (`{handoff_file}` does not exist yet: write it first)"
    )
    return _inject(
        "[CONTEXT GUARD REMINDER] The handoff is still pending. Stop all other"
        f" investigation now and hand off{missing}:\n{TASKS_NOTE}\n"
        + handoff_steps(
            conv_id,
            {"in_project": bool(state.get("pending_in_project")), "file": handoff_file},
            state.get("pending_title") or "<title>",
        )
    )

  late = m["ckpt"] >= 0
  # The transcript-size estimate overcounts (untruncated tool output, history
  # kept past compaction), so it may only nag; stop_guard enforces hard ones
  hard = not pending and ((m["pct"] >= HARD_PCT and not m["fallback"]) or late)
  turn_loop_exceeded = (
      invocation_num > 0 and invocation_num >= next_turn_inv_threshold
  )

  if not (hard or turn_loop_exceeded):
    messages = []
    soft_due = steps - int(
        state.get("soft_last_steps", -SOFT_REMIND_EVERY_STEPS)
    ) >= SOFT_REMIND_EVERY_STEPS
    if not pending and m["pct"] >= SOFT_PCT and soft_due:
      log["level"] = "soft"
      state["soft_last_steps"] = steps
      save_state(state_path, state)
      target = resolve_handoff_target(conv_id, transcript_path, artifact_dir)
      messages.append(
          f"[CONTEXT GUARD: SOFT LIMIT] Context is at {_usage_str(m)}. Keep"
          " working, but at the nearest micro-boundary (test run finished,"
          " hypothesis confirmed or rejected, commit made, subagent returned)"
          f" hand off:\n{TASKS_NOTE}\n"
          + handoff_steps(conv_id, target, build_continuation_title(conv_id))
      )
    if invocation_num > 0:
      streak, last_cmd_idx = get_consecutive_cmd_failures(transcript_path)
      if (
          streak >= FAILURE_STREAK_THRESHOLD
          and streak % FAILURE_STREAK_THRESHOLD == 0
          and last_cmd_idx != state.get("last_warned_fail_step")
      ):
        log["fail_streak"] = streak
        state["last_warned_fail_step"] = last_cmd_idx
        save_state(state_path, state)
        messages.append(
            f"[TWO-STRIKE DEBUG GUARD] Warning: {streak} consecutive commands in the current turn failed. "
            f"DO NOT make a third blind guess! Stop, read the exact error output and source declarations (`.h` / docs), "
            f"revert broken edits if needed (`git diff` / `git checkout`), and revise your hypothesis before proceeding."
        )
    return _inject(*messages) if messages else {}

  level = ("late" if late else "hard") if hard else "loop"
  log["level"] = level
  target = resolve_handoff_target(conv_id, transcript_path, artifact_dir)
  new_title = build_continuation_title(conv_id, last_user_input)
  cmd_prefix = build_agentapi_prefix(conv_id, transcript_path, workspace_paths)

  # The state is replaced wholesale so per-trigger flags (stop_guard's
  # stop_blocked_handoff, the fail-streak marker) start fresh
  save_state(state_path, {
      "gm_count": state.get("gm_count", 0),
      "soft_last_steps": state.get("soft_last_steps", -SOFT_REMIND_EVERY_STEPS),
      "last_triggered_level": level,
      "last_triggered_pct": round(m["pct"], 1),
      "last_triggered_invocation_num": invocation_num,
      "pending_handoff_launch": True,
      "pending_turn_start": invocation_num == 0,
      "handoff_completed": False,
      "pending_title": new_title,
      "pending_cmd_prefix": cmd_prefix,
      "pending_handoff_file": target["file"],
      "pending_in_project": target["in_project"],
      "next_turn_inv_threshold": (
          max(invocation_num, next_turn_inv_threshold) + SNOOZE_TURN_INVOCATIONS
          if invocation_num > 0
          else MAX_TURN_INVOCATIONS
      ),
  })

  late_note = (
      " History was already compacted, so earlier details may be gone: take"
      " facts from files, git and test output, not from memory."
      if late
      else ""
  )
  if level == "loop":
    head = (
        "[CONTEXT GUARD: MID-TURN CIRCUIT BREAKER] Single-turn iteration limit"
        f" exceeded ({invocation_num} consecutive steps; context at"
        f" {_usage_str(m)}). Do not try to finish the whole task here: bring any"
        " half-edited file to a clean checkpoint and hand off."
    )
  elif invocation_num > 0:
    head = (
        f"[CONTEXT GUARD: MID-TURN HANDOFF] Context is at {_usage_str(m)}.{late_note}"
        " Stop at the nearest safe checkpoint (no half-edited files, no new"
        " investigations) and hand off now."
    )
  else:
    head = (
        f"[CONTEXT GUARD: HANDOFF REQUIRED] Context is at {_usage_str(m)}.{late_note}"
        " First answer the user's current request completely, then hand off in"
        " this same turn."
    )
  return _inject(f"{head}\n{TASKS_NOTE}\n{handoff_steps(conv_id, target, new_title)}")


def main() -> None:
  t0 = time.monotonic()
  log = {
      "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
      "level": "none",
  }
  out: dict = {}
  try:
    raw_input = sys.stdin.read().strip()
    if raw_input:
      out = run(json.loads(raw_input), log)
  except Exception as e:
    # This hook runs before every model call of every chat: a crash would
    # break them all, so degrade to a no-op and leave a trace in the log
    out = {}
    log["level"] = "error"
    log["err"] = f"{type(e).__name__}: {e}"[:300]
  print(json.dumps(out, ensure_ascii=False))
  log["ms"] = round((time.monotonic() - t0) * 1000)
  if log["level"] != "skip":
    append_log(log)


if __name__ == "__main__":
  main()
