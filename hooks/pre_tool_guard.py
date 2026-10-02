#!/usr/bin/env python3
"""PreToolUse hook for Jetski to prevent hallucinations, blind edits, and broken handoffs.

Implements four mechanical guards:
1. Handoff Command & Post-Handoff Guard:
   - Blocks any file edits or duplicate `new-conversation` calls in a conversation
     that has already handed off in the current turn.
   - Blocks invalid `agentapi` subcommands (`start`, `create`, etc.) with the exact
     valid command.
   - Auto-rewrites (`overwrite`) `agentapi new-conversation` commands if they are
     missing `env -u ANTIGRAVITY_SOURCE_METADATA`, `ANTIGRAVITY_PROJECT_ID`, or
     `--title="[HH:MM продолжение] ..."`.
2. Automatic Handoff Summary Enrichment:
   - Right before `agentapi new-conversation` executes, automatically appends an
     objective machine-generated section (`git status -s`, `git diff --stat`,
     modified files, and recent commands with exit codes) to the conversation's
     `handoff_summary*.md` so the new chat never needs to grep old transcripts.
3. Read-Before-Edit Guard:
   - Blocks `replace_file_content` / `multi_replace_file_content` on existing
     source files if the file was never viewed (`view_file`) or written earlier
     in the current conversation.
4. Generated / Gitignored File Guard:
   - Blocks editing source files inside build caches or `.gitignore`d directories
     (e.g., `bin/cache/`, `out/`, `build/`) inside a Git repository.
"""

import glob
import json
import os
import re
import shlex
import subprocess
import sys

from context_guard import (
    APP_DATA_DIR_CANDIDATES,
    build_agentapi_prefix,
    build_continuation_title,
    get_handoff_launch_step_in_turn,
    load_state,
    parse_current_turn_steps,
)

EDIT_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
}

MODIFY_TOOLS = {
    "replace_file_content",
    "multi_replace_file_content",
}

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

EXIT_CODE_RE = re.compile(r"The command exited with code (\d+)\.")
AUTO_SNAPSHOT_HEADER = "## Automatic State Snapshot (Git & Recent Commands)"

# A *real* `agentapi new-conversation` invocation: the `agentapi` token must be
# at the start of the line or follow a shell separator / whitespace (not a quote),
# so `grep "…new-conversation…"`, `echo '…agentapi new-conversation…'` and similar
# mentions inside string literals do not trigger the launch rewrite.
AGENTAPI_NEW_CONV_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*/)?agentapi\s+new-conversation\b"
)
# `<<EOF` / `<<'EOF'` / `<<-"EOF"` ... body ... `EOF` — the body is data (commit
# messages, scripts), never a command. With `<<-` the terminator may be
# tab-indented.
HEREDOC_RE = re.compile(
    r"<<(-)?\s*(['\"]?)(\w+)\2[^\n]*\n.*?^(?(1)\t*)\3[ \t]*$",
    re.DOTALL | re.MULTILINE,
)


def strip_heredocs(cmd: str) -> str:
  return HEREDOC_RE.sub(r"<<\3 [heredoc body stripped]", cmd)


def count_agentapi_new_conversation_launches(cmd: str) -> int:
  """Number of real `agentapi new-conversation` invocations in the command line.

  Heredoc bodies are ignored and the rest is tokenized with shlex, so quoted
  mentions (grep patterns, --notes text, echo strings) are single tokens and
  never count. Falls back to AGENTAPI_NEW_CONV_RE when quoting is unbalanced.
  """
  cmd = strip_heredocs(cmd)
  try:
    tokens = shlex.split(cmd, posix=True)
  except ValueError:
    return len(AGENTAPI_NEW_CONV_RE.findall(cmd))
  n = 0
  for tok, nxt in zip(tokens, tokens[1:]):
    if os.path.basename(tok.lstrip("$(`")) == "agentapi" and nxt == "new-conversation":
      n += 1
  return n


def is_agentapi_new_conversation_launch(cmd: str) -> bool:
  """True only if the command really *invokes* `agentapi new-conversation`."""
  return count_agentapi_new_conversation_launches(cmd) > 0


def is_agent_internal_file(file_path: str) -> bool:
  """Returns True if the file is an agent artifact or customization file (not project code)."""
  if not file_path:
    return True
  clean = os.path.abspath(os.path.expanduser(file_path.strip().strip('"').strip("'")))
  config_dir = os.path.abspath(os.path.expanduser("~/.gemini/config"))
  internal_dirs = [config_dir] + [
      os.path.abspath(os.path.expanduser(d + "/brain"))
      for d in APP_DATA_DIR_CANDIDATES
  ]
  if clean.startswith("/tmp/") or any(
      clean == d or clean.startswith(d + "/") for d in internal_dirs
  ):
    return True
  return False


def is_tracked_source_file(file_path: str) -> bool:
  if not file_path or is_agent_internal_file(file_path):
    return False
  clean = file_path.strip().strip('"').strip("'")
  basename = os.path.basename(clean)
  if basename in SOURCE_FILENAMES:
    return True
  _, ext = os.path.splitext(basename)
  return ext.lower() in SOURCE_EXTENSIONS


def was_file_read_or_written_in_chat(
    transcript_path: str, target_file: str, current_step_idx: int = -1
) -> bool:
  """Checks if target_file was ever viewed or written in prior steps of the current conversation."""
  if not transcript_path or not os.path.exists(transcript_path):
    return True  # Fail-open if transcript is unavailable
  target_real = os.path.realpath(
      os.path.expanduser(target_file.strip().strip('"').strip("'"))
  )
  try:
    with open(transcript_path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line or "PLANNER_RESPONSE" not in line:
          continue
        try:
          obj = json.loads(line)
        except Exception:
          continue
        if obj.get("type") != "PLANNER_RESPONSE":
          continue
        idx = obj.get("step_index")
        if (
            current_step_idx >= 0
            and idx is not None
            and int(idx) >= current_step_idx
        ):
          continue
        for tc in obj.get("tool_calls") or []:
          tname = tc.get("name", "")
          targs = tc.get("args") or {}
          if tname == "view_file":
            p = targs.get("AbsolutePath", "")
          elif tname == "write_to_file":
            p = targs.get("TargetFile", "")
          else:
            continue
          if p:
            p_real = os.path.realpath(
                os.path.expanduser(str(p).strip().strip('"').strip("'"))
            )
            if p_real == target_real:
              return True
  except Exception:
    return True
  return False


def is_gitignored_or_build_cache(target_file: str) -> tuple[bool, str]:
  """Checks if target_file is inside a Git repo and is ignored by Git or in a known build cache."""
  clean = os.path.abspath(
      os.path.expanduser(target_file.strip().strip('"').strip("'"))
  )
  # Check explicit build cache directory patterns
  norm_parts = clean.replace("\\", "/")
  for cache_marker in ("/bin/cache/", "/.dart_tool/", "/CMakeFiles/"):
    if cache_marker in norm_parts:
      return True, f"path is inside a build cache (`{cache_marker}`)"

  parent_dir = os.path.dirname(clean)
  if not os.path.isdir(parent_dir):
    return False, ""

  try:
    # Check if inside a git work tree
    res_wt = subprocess.run(
        ["git", "-C", parent_dir, "rev-parse", "--is-inside-work-tree"],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if res_wt.returncode != 0:
      return False, ""

    # Check if git check-ignore matches this file
    res_ig = subprocess.run(
        ["git", "-C", parent_dir, "check-ignore", "-q", clean],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if res_ig.returncode == 0:
      return True, "file is matched by the repository's `.gitignore`"
  except Exception:
    pass
  return False, ""


def enrich_handoff_summary_files(
    artifact_dir: str, transcript_path: str, workspace_paths: list[str]
) -> None:
  """Appends an objective Git & command snapshot to handoff_summary*.md if not already present."""
  if not artifact_dir or not os.path.isdir(artifact_dir):
    return
  summary_files = sorted(
      glob.glob(os.path.join(artifact_dir, "handoff_summary*.md")),
      key=lambda p: os.path.getmtime(p),
      reverse=True,
  )
  if not summary_files:
    return
  target_summary = summary_files[0]
  try:
    with open(target_summary, "r", encoding="utf-8") as f:
      existing = f.read()
    if AUTO_SNAPSHOT_HEADER in existing:
      return
  except Exception:
    return

  sections = ["", "---", "", AUTO_SNAPSHOT_HEADER, ""]

  # 1. Collect edited files & last 5 commands from transcript.jsonl
  edited_files: list[str] = []
  recent_cmds: list[str] = []
  if transcript_path and os.path.exists(transcript_path):
    try:
      steps_by_idx = {}
      with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
          if not line.strip():
            continue
          try:
            obj = json.loads(line)
            idx = obj.get("step_index")
            if idx is not None:
              steps_by_idx[int(idx)] = obj
          except Exception:
            continue
      pending_cmd = None
      for idx in sorted(steps_by_idx.keys()):
        step = steps_by_idx[idx]
        stype = step.get("type", "")
        if stype == "PLANNER_RESPONSE":
          for tc in step.get("tool_calls") or []:
            tname = tc.get("name", "")
            targs = tc.get("args") or {}
            if tname in EDIT_TOOLS:
              tf = str(targs.get("TargetFile", "")).strip()
              if tf and not is_agent_internal_file(tf) and tf not in edited_files:
                edited_files.append(tf)
            elif tname == "run_command":
              cmd_str = str(targs.get("CommandLine", "")).strip().replace("\n", " ")
              if "new-conversation" not in cmd_str:
                pending_cmd = cmd_str[:140]
        elif stype == "GENERIC" and pending_cmd:
          m = EXIT_CODE_RE.search(str(step.get("content", "")))
          code_str = f"exit {m.group(1)}" if m else "async/running"
          recent_cmds.append(f"- `[{code_str}]` `{pending_cmd}`")
          pending_cmd = None
    except Exception:
      pass

  if edited_files:
    sections.append("### Files Modified by Tools in This Conversation")
    for ef in edited_files[-15:]:
      sections.append(f"- [`{os.path.basename(ef)}`](file://{ef}) (`{ef}`)")
    sections.append("")

  if recent_cmds:
    sections.append("### Recent Commands Executed")
    sections.extend(recent_cmds[-6:])
    sections.append("")

  # 2. Collect git status -s and git diff --stat for workspace_paths
  for wp in workspace_paths[:3]:
    if not os.path.isdir(wp):
      continue
    try:
      st = subprocess.run(
          ["git", "-C", wp, "status", "-s", "-uno"],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      df = subprocess.run(
          ["git", "-C", wp, "diff", "--stat"],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      st_out = (st.stdout or "").strip()
      df_out = (df.stdout or "").strip()
      if st_out or df_out:
        sections.append(f"### Git Status in `{wp}`")
        sections.append("```text")
        if st_out:
          sections.append(st_out[:1500])
        if df_out:
          sections.append(df_out[:1500])
        sections.append("```")
        sections.append("")
    except Exception:
      pass

  try:
    with open(target_summary, "a", encoding="utf-8") as f:
      f.write("\n".join(sections) + "\n")
  except Exception:
    pass


def main() -> None:
  try:
    raw_input = sys.stdin.read().strip()
    if not raw_input:
      print('{"decision": "allow"}')
      return
    data = json.loads(raw_input)
  except Exception:
    print('{"decision": "allow"}')
    return

  if data.get("isBattleMode"):
    print('{"decision": "allow"}')
    return

  tool_call = data.get("toolCall") or {}
  tool_name = tool_call.get("name", "")
  tool_args = tool_call.get("args") or {}

  conv_id = data.get("conversationId", "")
  parent_conv_id = data.get("parentConversationId", "")
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")
  workspace_paths = data.get("workspacePaths") or []

  # 1. Top-level conversation handoff guards
  if not parent_conv_id:
    state_path = (
        os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
        if artifact_dir
        else f"/tmp/jetski_context_guard_{conv_id}.json"
    )
    state = load_state(state_path)
    launch_step = get_handoff_launch_step_in_turn(transcript_path)

    # 1a. If this conversation has ALREADY launched a continuation in the current turn,
    # hard-block any further file edits or duplicate new-conversation launches!
    if launch_step >= 0 or state.get("handoff_completed"):
      if tool_name in EDIT_TOOLS:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[PRE-TOOL GUARD] This conversation has already launched a continuation chat (at step #{launch_step})! "
                        "Editing files in the old conversation is blocked to prevent race conditions with the new conversation. "
                        "End your turn immediately."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return
      if tool_name == "run_command":
        cmd = str(tool_args.get("CommandLine", ""))
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[PRE-TOOL GUARD] This conversation has already launched a continuation chat (at step #{launch_step})! "
                        "Running new commands or launching duplicate continuation chats in the old conversation is blocked. "
                        "If background tasks are running, terminate them via manage_task (kill) and end your turn."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

    # 1b. Inspect `agentapi` and daemon commands in run_command
    if tool_name == "run_command":
      cmd = str(tool_args.get("CommandLine", "")).strip()
      is_daemon = str(tool_args.get("IsDaemon", "")).lower() == "true"
      if re.search(r"\btail\s+-f\b", cmd) and (
          is_daemon or ".system_generated/tasks/" in cmd
      ):
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        "[PRE-TOOL GUARD] Do not run `tail -f` as a background daemon or on `.system_generated/tasks/` logs — "
                        "it never terminates on its own and leaves zombie tasks spinning in the conversation! "
                        "Wait for the task completion message, or read a snapshot via `view_file` or `tail -n 50`."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

      if "agentapi" in cmd and not re.match(
          r"^\s*(?:python3?|grep|egrep|fgrep|rg|git\s+grep|echo|cat)\b", cmd
      ):
        new_title = state.get("pending_title") or build_continuation_title(
            conv_id, ""
        )
        cmd_prefix = state.get("pending_cmd_prefix") or build_agentapi_prefix(
            conv_id, transcript_path, workspace_paths
        )
        # Catch hallucinated agentapi subcommands (e.g. `agentapi start`, `agentapi create`)
        cmd_code = strip_heredocs(cmd)
        bad_subcmd = re.search(
            r"\bagentapi\s+(start(?:-conversation)?|create(?:-conversation)?|conversation)\b",
            cmd_code,
        )
        if bad_subcmd:
          print(
              json.dumps(
                  {
                      "decision": "deny",
                      "reason": (
                          f"[PRE-TOOL GUARD] Subcommand `agentapi {bad_subcmd.group(1)}` does not exist! "
                          f"Use this EXACT command:\n`{cmd_prefix} --model=pro --title=\"{new_title}\" \"<prompt>\"`"
                      ),
                  },
                  ensure_ascii=False,
              )
          )
          return

        if count_agentapi_new_conversation_launches(cmd) > 1:
          print(
              json.dumps(
                  {
                      "decision": "deny",
                      "reason": (
                          "[PRE-TOOL GUARD] Do not run `agentapi new-conversation` multiple times or inside `$()` in a single command! "
                          "Launch exactly ONE chat, verify it in the SAME command, and wait for its completion."
                      ),
                  },
                  ensure_ascii=False,
              )
          )
          return

        # If calling `agentapi new-conversation`, verify handoff_summary exists,
        # enrich handoff_summary*.md on disk, and auto-inject missing env / --title!
        # Only a *real* invocation counts — not `grep`/`echo` mentioning the string.
        if is_agentapi_new_conversation_launch(cmd) and not re.search(
            r"\bnew-conversation\s+(?:--help|-h|help)\b", cmd
        ):
          ref_match = re.search(r"(/\S*handoff_summary[^\s\"']*\.md)", cmd)
          missing_summary = None
          if ref_match and not os.path.exists(ref_match.group(1)):
            missing_summary = ref_match.group(1)
          elif (
              state.get("pending_handoff_launch")
              and artifact_dir
              and not glob.glob(os.path.join(artifact_dir, "handoff_summary*.md"))
          ):
            missing_summary = state.get("pending_handoff_file") or os.path.join(
                artifact_dir, "handoff_summary.md"
            )
          if missing_summary:
            print(
                json.dumps(
                    {
                        "decision": "deny",
                        "reason": (
                            f"[PRE-TOOL GUARD] Cannot launch continuation conversation yet: the summary file `{missing_summary}` "
                            "does not exist on disk! First create the summary file using `write_to_file` (`UserFacing: true`), "
                            "and only then call `agentapi new-conversation`."
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return

          if artifact_dir:
            tasks_dir = os.path.join(artifact_dir, ".system_generated", "tasks")
            try:
              subprocess.run(["pkill", "-f", tasks_dir], check=False, timeout=1.0)
            except Exception:
              pass

          enrich_handoff_summary_files(
              artifact_dir, transcript_path, workspace_paths
          )
          needs_rewrite = False
          rewritten = cmd

          # Replace bare `/.../agentapi new-conversation` or `agentapi new-conversation`
          # with `cmd_prefix` if `ANTIGRAVITY_SOURCE_METADATA` is missing
          if "ANTIGRAVITY_SOURCE_METADATA" not in rewritten:
            rewritten = AGENTAPI_NEW_CONV_RE.sub(
                lambda _m: cmd_prefix, rewritten, count=1
            )
            needs_rewrite = True

          # Inject missing flags right after the real `agentapi new-conversation`
          # token (never into a quoted mention or the prompt text).
          if "--title" not in rewritten:
            rewritten = AGENTAPI_NEW_CONV_RE.sub(
                lambda m: f'{m.group(0)} --title="{new_title}"', rewritten, count=1
            )
            needs_rewrite = True

          if "--model" not in rewritten:
            rewritten = AGENTAPI_NEW_CONV_RE.sub(
                lambda m: f"{m.group(0)} --model=pro", rewritten, count=1
            )
            needs_rewrite = True

          if needs_rewrite and rewritten != cmd:
            print(
                json.dumps(
                    {"decision": "allow", "overwrite": {"CommandLine": rewritten}},
                    ensure_ascii=False,
                )
            )
            return

  # 2. Guards for source file edits (apply to both main agent and subagents)
  if tool_name in EDIT_TOOLS:
    target_file = str(tool_args.get("TargetFile", "")).strip()
    if is_tracked_source_file(target_file):
      # Guard 3: Block editing gitignored / build-cache files
      ignored, reason_detail = is_gitignored_or_build_cache(target_file)
      if ignored:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[GENERATED-FILE GUARD] Editing `{target_file}` is blocked: {reason_detail}. "
                        "Do not edit generated artifacts or build caches directly — "
                        "edit the tracked source file in the repository and rebuild the project."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

      # Guard 2: Read-Before-Edit on existing files for replace_file_content / multi_replace_file_content
      if tool_name in MODIFY_TOOLS:
        clean_path = os.path.expanduser(target_file.strip('"').strip("'"))
        if os.path.exists(clean_path) and not was_file_read_or_written_in_chat(
            transcript_path, clean_path, int(data.get("stepIdx", -1))
        ):
          print(
              json.dumps(
                  {
                      "decision": "deny",
                      "reason": (
                          f"[READ-BEFORE-EDIT GUARD] You are attempting to modify an existing file `{os.path.basename(clean_path)}` "
                          "without reading it via `view_file` in the current conversation! "
                          "Do not edit code blindly from memory or from a `handoff_summary`: "
                          "first read the target section of the file via `view_file`, verify exact signatures and context, "
                          "and only then call `replace_file_content`."
                      ),
                  },
                  ensure_ascii=False,
              )
          )
          return

  print('{"decision": "allow"}')


if __name__ == "__main__":
  main()
