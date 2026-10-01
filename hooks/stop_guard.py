#!/usr/bin/env python3
"""Stop hook for Jetski to prevent reporting completion after unverified code edits.

Checks the current turn in transcript.jsonl:
- If the agent modified a source code file (`write_to_file`, `replace_file_content`,
  or `multi_replace_file_content`) and did NOT run any verification command
  (`run_command`) after the last code edit (or the last command after the edit
  failed with a non-zero exit code), blocks the stop once (`decision: "continue"`)
  and instructs the agent to verify the change first.
"""

import json
import os
import re
import subprocess
import sys

SOURCE_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".py",
    ".rs",
    ".go",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".dart",
    ".java",
    ".kt",
    ".gn",
    ".gni",
    ".sh",
    ".cmake",
}
SOURCE_FILENAMES = {
    "CMakeLists.txt",
    "Makefile",
    "BUILD",
    "BUILD.bazel",
    "Cargo.toml",
    "package.json",
    "pubspec.yaml",
}

EDIT_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

# Tool results may indent this line (e.g. "\n\n\t\t\t\tThe command exited ..." on
# macOS Jetski), so allow leading whitespace.
EXIT_CODE_RE = re.compile(r"^\s*The command exited with code (\d+)\.", re.MULTILINE)

# Read-only inspection commands neither verify an edit (a `git diff` exiting 0 is
# not a build/test) nor indicate a broken build (`grep`/`rg`/`git grep` exit 1
# just means "no matches"), so they must not update verification tracking.
INSPECTION_SEGMENT_RE = re.compile(
    r"^(?:timeout\s+\S+\s+)?(?:"
    r"git(?:\s+-C\s+\S+)?\s+(?:status|diff|log|show|grep|check-ignore|branch"
    r"|rev-parse|ls-files|blame|remote)"
    r"|grep|egrep|fgrep|rg|ls|cat|head|tail|wc|find|fd|pwd|echo|which|file"
    r"|stat|tree|cd"
    r")\b"
)


def is_inspection_command(cmd: str) -> bool:
  """True if every segment of a (possibly chained/piped) command is read-only."""
  segments = [s.strip() for s in re.split(r"&&|\|\||;|\|", cmd) if s.strip()]
  return bool(segments) and all(INSPECTION_SEGMENT_RE.match(s) for s in segments)


def is_tracked_source_file(file_path: str) -> bool:
  if not file_path:
    return False
  clean = os.path.abspath(
      os.path.expanduser(file_path.strip().strip('"').strip("'"))
  )
  # Ignore agent customization/artifact files under ~/.gemini/config and
  # ~/.gemini/{antigravity,jetski}/brain (keep in sync with
  # context_guard.APP_DATA_DIR_CANDIDATES), but DO NOT ignore user project
  # workspaces under ~/.gemini/<client>/scratch!
  config_dir = os.path.abspath(os.path.expanduser("~/.gemini/config"))
  internal_dirs = [config_dir] + [
      os.path.abspath(os.path.expanduser(f"~/.gemini/{client}/brain"))
      for client in ("antigravity", "jetski")
  ]
  if clean.startswith("/tmp/") or any(
      clean == d or clean.startswith(d + "/") for d in internal_dirs
  ):
    return False
  basename = os.path.basename(clean)
  if basename in SOURCE_FILENAMES:
    return True
  _, ext = os.path.splitext(basename)
  return ext.lower() in SOURCE_EXTENSIONS


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


def play_stop_sound(is_handoff: bool) -> None:
  """Controls browser completion chime via config.json (no CRD required)."""
  import time
  config_path = os.path.expanduser("~/.gemini/config/config.json")
  try:
    if os.path.exists(config_path):
      with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)
    else:
      config = {}

    config.pop("enableSoundsForSpecialEvents", None)
    user_settings = config.setdefault("userSettings", {})

    if is_handoff:
      user_settings["enableSoundsForSpecialEvents"] = False
      with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
      # Restore to true after 2 seconds via detached background process
      subprocess.Popen([
          sys.executable, "-c",
          "import json, time, os; "
          "time.sleep(2); "
          "p = os.path.expanduser('~/.gemini/config/config.json'); "
          "c = json.load(open(p)) if os.path.exists(p) else {}; "
          "c.pop('enableSoundsForSpecialEvents', None); "
          "c.setdefault('userSettings', {})['enableSoundsForSpecialEvents'] = True; "
          "json.dump(c, open(p, 'w'), indent=2)"
      ], start_new_session=True)
      # Give fsnotify time to propagate config change to browser before Idle
      time.sleep(0.3)
    else:
      if user_settings.get("enableSoundsForSpecialEvents") is False:
        user_settings["enableSoundsForSpecialEvents"] = True
        with open(config_path, "w", encoding="utf-8") as f:
          json.dump(config, f, indent=2)
        time.sleep(0.3)
  except Exception:
    pass


def analyze_current_turn(transcript_path: str) -> dict:
  if not transcript_path or not os.path.exists(transcript_path):
    return {}

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
    return {}

  if not steps_by_idx:
    return {}

  ordered_indices = sorted(steps_by_idx.keys())
  # Find the last USER_INPUT step to scope analysis to the current turn
  last_user_idx = -1
  for idx in ordered_indices:
    if steps_by_idx[idx].get("type") == "USER_INPUT":
      last_user_idx = idx

  last_code_edit_idx = -1
  last_edited_file = ""
  last_cmd_idx = -1
  last_cmd_exit_code = None
  pending_cmd_check = False
  pending_new_conv_cmd = False
  launched_new_conv = False

  for idx in ordered_indices:
    if idx <= last_user_idx:
      continue
    step = steps_by_idx[idx]
    stype = step.get("type", "")
    tool_calls = step.get("tool_calls") or []

    if stype == "PLANNER_RESPONSE" and tool_calls:
      pending_new_conv_cmd = False
      for tc in tool_calls:
        tname = tc.get("name", "")
        targs = tc.get("args") or {}
        if tname in EDIT_TOOLS:
          target = targs.get("TargetFile", "")
          if is_tracked_source_file(target):
            last_code_edit_idx = idx
            last_edited_file = target.strip().strip('"').strip("'")
        elif tname == "run_command":
          cmd = str(targs.get("CommandLine", ""))
          if "agentapi" in cmd and "new-conversation" in cmd:
            pending_new_conv_cmd = True
          elif is_inspection_command(cmd):
            continue
          last_cmd_idx = idx
          pending_cmd_check = True

    elif stype == "GENERIC" and pending_cmd_check:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content)
      if m:
        last_cmd_exit_code = int(m.group(1))
        if (
            pending_new_conv_cmd
            and last_cmd_exit_code == 0
            and step.get("status") == "DONE"
            and '"newConversation"' in content
        ):
          launched_new_conv = True
          pending_new_conv_cmd = False
        pending_cmd_check = False

  return {
      "last_code_edit_idx": last_code_edit_idx,
      "last_edited_file": last_edited_file,
      "last_cmd_idx": last_cmd_idx,
      "last_cmd_exit_code": last_cmd_exit_code,
      "launched_new_conv": launched_new_conv,
  }


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

  if data.get("isBattleMode") or data.get("parentConversationId"):
    print("{}")
    return

  term_reason = str(data.get("terminationReason", "")).upper()
  if term_reason and term_reason != "MODEL_STOP":
    print("{}")
    return

  if not data.get("fullyIdle", True):
    print("{}")
    return

  conv_id = data.get("conversationId", "")
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")

  info = analyze_current_turn(transcript_path)

  state_path = (
      os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      if artifact_dir
      else f"/tmp/jetski_context_guard_{conv_id}.json"
  )
  state = load_state(state_path)

  if info.get("launched_new_conv"):
    if state.get("pending_handoff_launch") or not state.get(
        "handoff_completed"
    ):
      state["pending_handoff_launch"] = False
      state["handoff_completed"] = True
      save_state(state_path, state)
    play_stop_sound(is_handoff=True)
    print("{}")
    return

  # If a handoff was triggered in this turn and the agent tries to stop without
  # having called agentapi new-conversation, block once and force the launch!
  if state.get("pending_handoff_launch") and not state.get(
      "stop_blocked_handoff"
  ):
    state["stop_blocked_handoff"] = True
    save_state(state_path, state)
    new_title = state.get("pending_title") or "[handoff]"
    cmd_prefix = (
        state.get("pending_cmd_prefix")
        or "env -u ANTIGRAVITY_SOURCE_METADATA agentapi new-conversation"
    )
    short_id = conv_id[:8] if conv_id else "unknown"
    default_handoff = (
        os.path.join(artifact_dir, f"handoff_summary_{short_id}.md")
        if artifact_dir
        else f"/tmp/handoff_summary_{short_id}.md"
    )
    handoff_file = state.get("pending_handoff_file") or default_handoff
    reason = (
        f"[HANDOFF GUARD] You prepared the summary `{handoff_file}`, but attempted to finish your turn "
        f"WITHOUT launching the continuation conversation! Call `run_command`:\n"
        f'`OUT=$({cmd_prefix} --model=pro --title="{new_title}" '
        f'"Continuing unfinished task from previous conversation (conversation://{conv_id}), interrupted due to context limits. '
        f"Read {handoff_file} via view_file, review completed steps and discarded hypotheses, and continue from the next step.\") && "
        f'ID=$(printf \'%s\' "$OUT" | grep -o \'"conversationId": *"[^"]*"\' | cut -d\'"\' -f4) && '
        f'echo "ID=$ID" && agentapi get-conversation-metadata "$ID" | grep -E \'"sourceMetadata"\'`\n'
        f"and provide the link `[👉 {new_title}](conversation://<new_conversation_id>)` to the user."
    )
    print(
        json.dumps(
            {"decision": "continue", "reason": reason}, ensure_ascii=False
        )
    )
    return

  last_edit_idx = info.get("last_code_edit_idx", -1)
  if last_edit_idx < 0:
    play_stop_sound(is_handoff=False)
    print("{}")
    return

  # Only block once per code edit step index to prevent infinite loops
  if state.get("last_blocked_edit_idx") == last_edit_idx:
    play_stop_sound(is_handoff=False)
    print("{}")
    return

  last_cmd_idx = info.get("last_cmd_idx", -1)
  last_cmd_exit = info.get("last_cmd_exit_code")
  edited_file = os.path.basename(info.get("last_edited_file", "file"))

  if last_cmd_idx < last_edit_idx:
    state["last_blocked_edit_idx"] = last_edit_idx
    save_state(state_path, state)
    reason = (
        f"[VERIFICATION GUARD] You modified source code (`{edited_file}`), but did not run a build, test, "
        f"or syntax verification command (`run_command`) after the last edit. "
        f"Run verification before finishing your response (or explicitly state why automated verification does not apply here)."
    )
    print(
        json.dumps(
            {"decision": "continue", "reason": reason}, ensure_ascii=False
        )
    )
    return

  if last_cmd_exit is not None and last_cmd_exit != 0:
    state["last_blocked_edit_idx"] = last_edit_idx
    save_state(state_path, state)
    reason = (
        f"[VERIFICATION GUARD] After editing `{edited_file}`, the last executed command "
        f"failed with exit code {last_cmd_exit}. Do not finish your turn with broken code: "
        f"fix the error and re-verify, or revert the broken change and explain why."
    )
    print(
        json.dumps(
            {"decision": "continue", "reason": reason}, ensure_ascii=False
        )
    )
    return

  play_stop_sound(is_handoff=False)
  print("{}")


if __name__ == "__main__":
  main()
