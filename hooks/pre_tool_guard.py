#!/usr/bin/env python3
"""PreToolUse hook for Jetski to prevent hallucinations, blind edits, and broken handoffs.

Implements nine mechanical guards:
1. Handoff Command & Post-Handoff Guard:
   - Blocks any file edits or duplicate `new-conversation` calls in a conversation
     that has already handed off in the current turn.
   - Blocks invalid `agentapi` subcommands (`start`, `create`, etc.) with the exact
     valid command.
   - Auto-rewrites (`overwrite`) `agentapi new-conversation` commands if they are
     missing `env -u ANTIGRAVITY_SOURCE_METADATA`, `ANTIGRAVITY_PROJECT_ID`, or
     `--title="[HH:MM продолжение] ..."`.
2. Automatic Handoff Summary Enrichment:
   - Right before a handoff executes, automatically appends an objective
     machine-generated section (`git status -s`, `git diff --stat`, modified
     files, and recent commands with exit codes) to the conversation's
     `handoff_summary*.md` (outside-project chats) or refreshes it in the chain
     state file (in-project chats), so the new chat never needs to grep old
     transcripts.
3. Read-Before-Edit Guard:
   - Blocks `replace_file_content` / `multi_replace_file_content` / `edit_file`
     on existing source files if the file was never viewed (`view_file` /
     `read_file`) or written earlier in the current conversation.
4. Generated / Gitignored File Guard:
   - Blocks editing source files inside build caches or `.gitignore`d directories
     (e.g., `bin/cache/`, `out/`, `build/`) inside a Git repository.
5. Roadmap Guard:
   - Blocks direct file edits under the roadmaps dir except the conversation's
     own `state/<conversationId>.md`; roadmap changes go through `roadmap.py`.
6. Batch Workflow Verification Guard:
   - Blocks `run_workflow` scripts that fan out over multiple agents (`parallel` or
     `pipeline` with `agent(...)`) without gating results via `advanced.verify`
     (unless explicitly marked `# no-verify` for read-only discovery).
7. Code Pre-Commit Audit Guard (`CODE-CHECK GUARD`):
   - Blocks `git commit`, `git cl upload`, and `gh pr create` after modifying a
     tracked source file unless both `audit_comments.py` and `ruthless_audit.py`
     have been executed since the last source edit.
8. Google Docs Grounding Guard (`DOC-CHECK GUARD`):
   - Blocks `call_mcp_tool` (`update_doc`) unless `check_doc_grounding.py` has
     run since the last consumed `update_doc` turn.
9. Cross-Project Scope Guard (`CROSS-PROJECT SCOPE GUARD`):
   - Blocks editing files or running `git commit` / `git cl upload` / `gh pr create`
     inside a workspace directory that belongs exclusively to another Jetski project,
     unless the user confirmed cross-project execution in a subsequent turn and
     `.cross_project_override.json` was written.
"""

# Every gated tool call of every chat runs this file, including the calls that
# edit it, so a half-applied change locks out all agents (a broken sibling hook
# only fails open): make each edit self-contained or mv a finished copy into
# place (it must stay executable), then run tests/run_all_tests.py

from __future__ import annotations
import ast
import glob
import json
import os
import re
import shlex
import subprocess
import sys
import time
import traceback

# Sibling hooks are imported where they are used: a broken one then raises
# inside _guarded_main, which fails open and leaves a marker for the notice
# hook, instead of exiting non-zero at import and failing every gated call of
# every chat

EDIT_TOOLS = {
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write_file",
    "edit_file",
}

MODIFY_TOOLS = {
    "replace_file_content",
    "multi_replace_file_content",
    "edit_file",
}

READ_TOOLS = {
    "view_file",
    "read_file",
}

WRITE_NEW_TOOLS = {
    "write_to_file",
    "write_file",
}


def get_tool_target_file(tool_args: dict) -> str:
  """Extracts the target file path across Antigravity and Jetski Web tool schemas."""
  for key in ("TargetFile", "Path", "AbsolutePath"):
    val = tool_args.get(key)
    if val:
      return str(val).strip()
  return ""

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
AGENTAPI_SEND_MSG_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*/)?agentapi\s+send-message\b"
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


def _clean_cmd_token(tok: str) -> str:
  cleaned = re.sub(r"^(?:[A-Za-z_][A-Za-z0-9_]*=)?[\$\(`]+", "", tok)
  return os.path.basename(cleaned)


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
    if _clean_cmd_token(tok) == "agentapi" and nxt == "new-conversation":
      n += 1
  return n


def is_agentapi_new_conversation_launch(cmd: str) -> bool:
  """True only if the command really *invokes* `agentapi new-conversation`."""
  return count_agentapi_new_conversation_launches(cmd) > 0


# Match both continuation handoffs and next-task transitions as chat-launching CLI calls
CHAT_LIFECYCLE_HANDOFF_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*/)?chat_lifecycle\.py\s+(?:handoff|next-task)\b"
)


def count_chat_lifecycle_handoff_launches(cmd: str) -> int:
  """Number of real `chat_lifecycle.py handoff|next-task` invocations in the command line."""
  cmd = strip_heredocs(cmd)
  try:
    tokens = shlex.split(cmd, posix=True)
  except ValueError:
    return len(CHAT_LIFECYCLE_HANDOFF_RE.findall(cmd))
  n = 0
  # Ignore help queries so `next-task --help` never marks the conversation closed
  for i, (tok, nxt) in enumerate(zip(tokens, tokens[1:])):
    if _clean_cmd_token(tok) == "chat_lifecycle.py" and nxt in ("handoff", "next-task"):
      if nxt == "next-task" and any(a in ("-h", "--help", "help") for a in tokens[i + 2 :]):
        continue
      n += 1
  return n


# Detect next-task launches so pre_tool_guard skips requiring an existing old state file
def is_chat_lifecycle_next_task_launch(cmd: str) -> bool:
  """True only if the command really invokes `chat_lifecycle.py next-task` (excluding --help)"""
  cmd = strip_heredocs(cmd)
  try:
    tokens = shlex.split(cmd, posix=True)
  except ValueError:
    return bool(
        re.search(
            r"(?:^|(?<=[\s;&|(`]))(?:\S*/)?chat_lifecycle\.py\s+next-task\b(?!\s+(?:--help|-h|help)\b)",
            cmd,
        )
    )
  for i, (tok, nxt) in enumerate(zip(tokens, tokens[1:])):
    if _clean_cmd_token(tok) == "chat_lifecycle.py" and nxt == "next-task":
      if not any(a in ("-h", "--help", "help") for a in tokens[i + 2 :]):
        return True
  return False


def is_chat_lifecycle_handoff_launch(cmd: str) -> bool:
  """True only if the command really *invokes* `chat_lifecycle.py handoff` or `next-task`."""
  return count_chat_lifecycle_handoff_launches(cmd) > 0


def is_agent_internal_file(file_path: str) -> bool:
  """Returns True if the file is an agent artifact or customization file (not project code)."""
  if not file_path:
    return True
  from context_guard import APP_DATA_DIR_CANDIDATES
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
          if tname in READ_TOOLS or tname in WRITE_NEW_TOOLS:
            p = get_tool_target_file(targs)
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


GIT_COMMIT_OR_UPLOAD_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:"
    r"git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+(?:commit|cl\s+upload)"
    r"|gh\s+pr\s+create"
    r")\b"
)

# Match upload and push commands separately from local commits so the heavy
# Flutter corpus gate triggers only when publishing changes externally
GIT_UPLOAD_OR_PUSH_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:"
    r"git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+(?:cl\s+upload|push)"
    r"|gh\s+pr\s+create"
    r")\b"
)

# Restrict corpus gating to production C++/Dart/TS directories (excluding tests/slides)
SKIA_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)modules/(?:skparagraph|skunicode|skshaper)/(?:src|include)/"
)
WEBPARAGRAPH_CORPUS_PATH_RE = re.compile(
    r"(?:^|/)(?:engine/src/flutter/)?lib/web_ui/lib/"
)
CODE_CORPUS_EXTENSIONS = {
    ".c",
    ".cc",
    ".cpp",
    ".cxx",
    ".h",
    ".hh",
    ".hpp",
    ".hxx",
    ".mm",
    ".dart",
    ".ts",
}


def _non_inspection_cmd_segments(cmd: str) -> str:
  """Strip heredocs and drop shell segments that start with read-only inspection commands."""
  cmd_code = strip_heredocs(cmd)
  kept = []
  for seg in re.split(r"&&|\|\||;|\||\n", cmd_code):
    s = seg.strip()
    if not s:
      continue
    if re.match(r"^(?:grep|egrep|fgrep|rg|git\s+grep|echo|cat)\b", s):
      continue
    kept.append(s)
  return " ; ".join(kept)


def is_git_commit_or_upload_command(cmd: str) -> bool:
  """True if cmd invokes git commit, git cl upload, or gh pr create outside heredocs/grep."""
  cmd_code = _non_inspection_cmd_segments(cmd)
  if not cmd_code:
    return False
  if re.search(r"\b(?:commit|upload|create)\s+(?:--help|-h|help)\b", cmd_code):
    return False
  return bool(GIT_COMMIT_OR_UPLOAD_RE.search(cmd_code))


# Detect external publishing commands (upload, push, PR creation) outside grep/help calls
def is_git_upload_or_push_command(cmd: str) -> bool:
  cmd_code = _non_inspection_cmd_segments(cmd)
  if not cmd_code:
    return False
  if re.search(
      r"\b(?:upload|push|create)\s+(?:--help|-h|help)\b", cmd_code
  ):
    return False
  return bool(GIT_UPLOAD_OR_PUSH_RE.search(cmd_code))


def _load_transcript_steps(
    transcript_path: str, current_step_idx: int = -1
) -> list[dict]:
  """Loads ordered JSONL transcript steps prior to current_step_idx."""
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
  return [
      steps_by_idx[idx]
      for idx in sorted(steps_by_idx.keys())
      if current_step_idx < 0 or idx < current_step_idx
  ]


def _invokes_script(cmd: str, script_name: str) -> bool:
  """True if any non-inspection command segment in cmd executes script_name."""
  cmd_code = strip_heredocs(cmd)
  if script_name not in cmd_code:
    return False
  for seg in re.split(r"&&|\|\||;|\||\n", cmd_code):
    seg = seg.strip()
    if not seg or script_name not in seg:
      continue
    if re.match(
        r"^(?:grep|egrep|fgrep|rg|git|ls|cat|head|tail|echo|find|wc|stat)\b",
        seg,
    ):
      continue
    return True
  return False


def unverified_source_edit_before_commit(
    transcript_path: str, current_cmd: str = "", current_step_idx: int = -1
) -> str:
  """Basename of last edited source file if both pre-commit audits have not run since."""
  steps = _load_transcript_steps(transcript_path, current_step_idx)
  if not steps:
    return ""
  last_edited_source = ""
  has_audit_comments = False
  has_ruthless_audit = False
  for step in steps:
    if step.get("type") != "PLANNER_RESPONSE":
      continue
    for tc in step.get("tool_calls") or []:
      tname = tc.get("name", "")
      targs = tc.get("args") or {}
      if tname in EDIT_TOOLS:
        tf = get_tool_target_file(targs)
        if is_tracked_source_file(tf):
          last_edited_source = os.path.basename(
              tf.strip().strip('"').strip("'")
          )
          has_audit_comments = False
          has_ruthless_audit = False
      elif tname == "run_command":
        c = str(targs.get("CommandLine", ""))
        if _invokes_script(c, "audit_comments.py"):
          has_audit_comments = True
        if _invokes_script(c, "ruthless_audit.py"):
          has_ruthless_audit = True

  if _invokes_script(current_cmd, "audit_comments.py"):
    has_audit_comments = True
  if _invokes_script(current_cmd, "ruthless_audit.py"):
    has_ruthless_audit = True

  if last_edited_source and not (has_audit_comments and has_ruthless_audit):
    return last_edited_source
  return ""


# Strip C/C++/Dart/TS comments while keeping string literals intact so comment-only edits
# do not trigger the heavy Flutter corpus requirement
def _strip_c_style_comments(text: str) -> str:
  pattern = re.compile(
      r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'|/\*[\s\S]*?\*/|//[^\n]*'
  )

  def _repl(m: re.Match) -> str:
    s = m.group(0)
    if s.startswith("//") or s.startswith("/*"):
      return ""
    return s

  return pattern.sub(_repl, text)


# Distinguish executable code changes from comment or whitespace edits in tool arguments
def is_executable_code_edit(tool_name: str, tool_args: dict) -> bool:
  tf = get_tool_target_file(tool_args)
  if not is_tracked_source_file(tf):
    return False
  clean = tf.strip().strip('"').strip("'").replace("\\", "/")
  _, ext = os.path.splitext(clean)
  if ext.lower() not in CODE_CORPUS_EXTENSIONS:
    return False
  if tool_name in WRITE_NEW_TOOLS:
    content = str(
        tool_args.get("Content") or tool_args.get("CodeContent") or ""
    )
    return bool(_strip_c_style_comments(content).strip())

  chunks = tool_args.get("ReplacementChunks")
  if isinstance(chunks, list) and chunks:
    pairs = [
        (
            str(c.get("TargetContent") or ""),
            str(c.get("ReplacementContent") or ""),
        )
        for c in chunks
        if isinstance(c, dict)
    ]
  else:
    pairs = [(
        str(tool_args.get("Target") or tool_args.get("TargetContent") or ""),
        str(
            tool_args.get("Replacement")
            or tool_args.get("ReplacementContent")
            or ""
        ),
    )]
  for target, replacement in pairs:
    t_code = [
        ln.strip()
        for ln in _strip_c_style_comments(target).splitlines()
        if ln.strip()
    ]
    r_code = [
        ln.strip()
        for ln in _strip_c_style_comments(replacement).splitlines()
        if ln.strip()
    ]
    if t_code != r_code:
      return True
  return False


# Scan unified git diff lines to detect whether any non-comment executable code changed
def diff_text_has_executable_code_changes(diff_text: str) -> bool:
  in_block_comment = False
  for raw_line in diff_text.splitlines():
    if raw_line.startswith(("+++", "---", "@@", "diff --git", "index ")):
      in_block_comment = False
      continue
    if not raw_line or raw_line[0] not in ("+", "-"):
      continue
    line = raw_line[1:].strip()
    if not line:
      continue
    while line:
      if in_block_comment:
        end_idx = line.find("*/")
        if end_idx < 0:
          line = ""
          break
        in_block_comment = False
        line = line[end_idx + 2 :].strip()
      else:
        if line.startswith("//"):
          line = ""
          break
        start_idx = line.find("/*")
        line_comment_idx = line.find("//")
        if 0 <= line_comment_idx < (start_idx if start_idx >= 0 else len(line)):
          code_prefix = line[:line_comment_idx].strip()
          if code_prefix:
            return True
          line = ""
          break
        if start_idx == 0:
          end_idx = line.find("*/", 2)
          if end_idx < 0:
            in_block_comment = True
            line = ""
            break
          line = line[end_idx + 2 :].strip()
        elif start_idx > 0:
          return True
        else:
          return True
  return False


# Verify against the actual git repository diff when on disk so reverted edits do not block upload
def _git_branch_has_executable_code_changes(
    file_path: str, pathspec: str | tuple[str, ...]
) -> bool | None:
  clean = os.path.abspath(
      os.path.expanduser(file_path.strip().strip('"').strip("'"))
  )
  parent_dir = os.path.dirname(clean)
  if not os.path.isdir(parent_dir):
    return None
  try:
    top = subprocess.run(
        ["git", "-C", parent_dir, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if top.returncode != 0:
      return None
    repo_root = top.stdout.strip()
    base_ref = "HEAD~1"
    for cand in ("@{u}", "origin/main", "origin/master"):
      mb = subprocess.run(
          ["git", "-C", repo_root, "merge-base", "HEAD", cand],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      if mb.returncode == 0 and mb.stdout.strip():
        base_ref = mb.stdout.strip()
        break
    specs = [pathspec] if isinstance(pathspec, str) else list(pathspec)
    diff_res = subprocess.run(
        ["git", "-C", repo_root, "diff", "-U0", base_ref, "--", *specs],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    if diff_res.returncode != 0:
      return None
    return diff_text_has_executable_code_changes(diff_res.stdout or "")
  except Exception:
    return None


# Track whether executable production code in SkParagraph or WebParagraph changed since the last corpus run
def unverified_corpus_before_upload_or_push(
    transcript_path: str, current_cmd: str = "", current_step_idx: int = -1
) -> tuple[str, str, str]:
  steps = _load_transcript_steps(transcript_path, current_step_idx)
  if not steps:
    return ("", "", "")
  last_skia_file = ""
  last_skia_full_path = ""
  has_skia_corpus = False
  last_wp_file = ""
  last_wp_full_path = ""
  has_wp_corpus = False

  for step in steps:
    if step.get("type") != "PLANNER_RESPONSE":
      continue
    for tc in step.get("tool_calls") or []:
      tname = tc.get("name", "")
      targs = tc.get("args") or {}
      if tname in EDIT_TOOLS:
        tf = get_tool_target_file(targs)
        norm_tf = tf.strip().strip('"').strip("'").replace("\\", "/")
        if SKIA_CORPUS_PATH_RE.search(norm_tf) and is_executable_code_edit(
            tname, targs
        ):
          last_skia_file = os.path.basename(norm_tf)
          last_skia_full_path = norm_tf
          has_skia_corpus = False
        elif WEBPARAGRAPH_CORPUS_PATH_RE.search(
            norm_tf
        ) and is_executable_code_edit(tname, targs):
          last_wp_file = os.path.basename(norm_tf)
          last_wp_full_path = norm_tf
          has_wp_corpus = False
      elif tname == "run_command":
        c = str(targs.get("CommandLine", ""))
        if _invokes_script(c, "run_flutter_paragraph_tests.py"):
          has_skia_corpus = True
        if _invokes_script(c, "run_webparagraph_corpus.py"):
          has_wp_corpus = True

  if _invokes_script(current_cmd, "run_flutter_paragraph_tests.py"):
    has_skia_corpus = True
  if _invokes_script(current_cmd, "run_webparagraph_corpus.py"):
    has_wp_corpus = True

  if last_skia_file and not has_skia_corpus:
    git_has_code = _git_branch_has_executable_code_changes(
        last_skia_full_path, "modules/"
    )
    if git_has_code is not False:
      return (
          "SkParagraph/SkUnicode/SkShaper",
          last_skia_file,
          "~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py",
      )

  if last_wp_file and not has_wp_corpus:
    git_has_code = _git_branch_has_executable_code_changes(
        last_wp_full_path,
        ("engine/src/flutter/lib/web_ui/lib/", "lib/web_ui/lib/"),
    )
    if git_has_code is not False:
      return (
          "WebParagraph (web_ui)",
          last_wp_file,
          "~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py",
      )

  return ("", "", "")


# Subjects that claim a speedup; a perf claim without measurements is the failure mode the
# perf-optimization-loop skill exists to prevent
PERF_CLAIM_RE = re.compile(
    r"\b(?:speed(?:s|ed)?[\s-]*up|speedup|faster|optimi[sz](?:e|ing|ation|er)\w*"
    r"|improve[sd]?\s+(?:perf(?:ormance)?|latency|throughput)"
    r"|reduce[sd]?\s+(?:allocations?|copies|overhead|latency))\b"
    r"|\bускор\w*|\bбыстрее\b|\bоптимиз\w*|\bпроизводительн\w*",
    re.IGNORECASE,
)
PERF_TRAILER_RE = re.compile(r"^Perf:\s*\S", re.MULTILINE)
PERF_EXEMPT_RE = re.compile(r"^Perf-Exempt:\s*\S", re.MULTILINE)
PERF_SKILL_DIR = "~/.gemini/config/skills/perf-optimization-loop"
# opt_branch_audit.py writes the audited HEAD here; see its main()
PERF_AUDIT_STAMP = os.path.join("optimizations", ".audited")
GIT_COMMIT_AT_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+commit\b"
)
# The skill targets SkParagraph / SkShaper / WebParagraph; other repos are not gated
PERF_REPO_MARKERS = ("modules/skparagraph", "engine/src/flutter/lib/web_ui", "lib/web_ui/lib")


def _perf_repo_root(cmd: str, workspace_paths: list[str]) -> str:
  cmd_code = strip_heredocs(cmd)
  explicit = re.search(r"(?:^|[\s;&|(`])cd\s+|\bgit\s+-C\s+", cmd_code)
  dirs = _extract_cmd_target_dirs(cmd, workspace_paths)
  # Without cd / -C the shell runs in the first workspace, which is also the relative base
  repo_dir = dirs[-1] if explicit and dirs else next(
      (os.path.expanduser(wp) for wp in workspace_paths if wp), "")
  if not repo_dir or not os.path.isdir(repo_dir):
    return ""
  try:
    top = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--show-toplevel"],
        capture_output=True, text=True, timeout=1.5, check=False,
    )
  except Exception:
    return ""
  root = top.stdout.strip() if top.returncode == 0 else ""
  if root and any(os.path.isdir(os.path.join(root, m)) for m in PERF_REPO_MARKERS):
    return root
  return ""


def _commit_message_from_cmd(cmd: str) -> str:
  """Message of the first `git commit` in cmd: its own heredoc, an absolute -F file, or -m."""
  spans = [(m.start(), m.end()) for m in HEREDOC_RE.finditer(cmd)]
  start = next((m.start() for m in GIT_COMMIT_AT_RE.finditer(cmd)
                if not any(s <= m.start() < e for s, e in spans)), -1)
  if start < 0:
    return ""
  # The commit segment ends at the next top-level separator outside heredocs and quotes
  tail = cmd[start:]
  seg_end = len(tail)
  for m in re.finditer(r"&&|\|\||;|\n", strip_heredocs(tail)):
    seg_end = m.start()
    break
  seg = strip_heredocs(tail)[:seg_end]
  # `-F -` and `-m "$(cat <<EOF` read the heredoc that belongs to this very command
  if re.search(r"(?:-F|--file)(?:\s+|=)-(?:\s|$)|\$\(\s*cat\s+<<", seg):
    m = next((h for h in HEREDOC_RE.finditer(cmd) if h.start() >= start), None)
    if m:
      body = m.group(0).split("\n", 1)[1] if "\n" in m.group(0) else ""
      return body.rsplit("\n", 1)[0]
    return ""
  try:
    tokens = shlex.split(seg, posix=True)
  except ValueError:
    return ""
  parts: list[str] = []
  for i, tok in enumerate(tokens):
    nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
    # Combined short flags such as -am / -qm take the message as the next token
    if re.fullmatch(r"-[a-zA-Z]*m", tok) or tok == "--message":
      parts.append(nxt)
    elif tok.startswith("--message="):
      parts.append(tok.split("=", 1)[1])
    elif tok in ("-F", "--file") and os.path.isabs(os.path.expanduser(nxt)):
      # Relative paths resolve against the shell cwd, which the hook cannot see
      try:
        with open(os.path.expanduser(nxt), encoding="utf-8", errors="replace") as f:
          parts.append(f.read())
      except OSError:
        pass
  return "\n\n".join(p for p in parts if p)


# Deny a commit whose subject claims a speedup but carries no Perf: measurement trailer
def perf_claim_commit_denial(cmd: str, workspace_paths: list[str]) -> str:
  if not is_git_commit_or_upload_command(cmd):
    return ""
  msg = _commit_message_from_cmd(cmd).strip()
  if not msg:
    return ""
  subject = msg.splitlines()[0]
  if (not PERF_CLAIM_RE.search(subject) or subject.startswith('Revert "')
      or PERF_TRAILER_RE.search(msg) or PERF_EXEMPT_RE.search(msg)):
    return ""
  if not _perf_repo_root(cmd, workspace_paths):
    return ""
  return (
      f"[PERF-CLAIM GUARD] The commit subject claims a speedup (`{subject[:80]}`) but the "
      "message has no `Perf:` trailer. Follow 1 optimization = 1 measured commit: measure "
      "at least 2 interleaved rounds, run "
      f"`python3 {PERF_SKILL_DIR}/scripts/bench_compare.py old.r1,old.r2 new.r1,new.r2` and "
      "paste its `Perf:` lines into the message, then record the win with `opt_ledger.py add`. "
      "If the change is not a performance claim (e.g. \"optimize imports\"), add a "
      "`Perf-Exempt: <reason>` line instead."
  )


def _git_out(root: str, *args: str) -> str:
  try:
    res = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                         timeout=2.0, check=False)
  except Exception:
    return ""
  return res.stdout.strip() if res.returncode == 0 else ""


def _perf_claim_commits_to_publish(root: str) -> list[str]:
  # Forks keep origin behind upstream, so origin/main would pull in other people's commits
  base = next((b for b in (_git_out(root, "merge-base", "HEAD", c) for c in
                           ("@{u}", "upstream/main", "upstream/master", "origin/main",
                            "origin/master")) if b), "")
  # GIT_AUTHOR_IDENT honours GIT_AUTHOR_EMAIL as well as user.email, like `git commit` does
  m = re.search(r"<([^>]+)>", _git_out(root, "var", "GIT_AUTHOR_IDENT"))
  email = m.group(1) if m else ""
  if not base or not email:
    return []
  log = _git_out(root, "log", "-n", "50", f"--author={email}", "--format=%h%x00%B%x01",
                 f"{base}..HEAD")
  hits = []
  for rec in log.split("\x01"):
    if "\x00" not in rec:
      continue
    sha, body = rec.strip("\n").split("\x00", 1)
    subject = body.strip().splitlines()[0] if body.strip() else ""
    if (PERF_CLAIM_RE.search(subject) and not subject.startswith('Revert "')
        and not PERF_EXEMPT_RE.search(body)):
      hits.append(f"{sha} {subject[:60]}")
  return hits


# Deny publishing perf-claim commits until opt_branch_audit.py has audited the current HEAD
def perf_audit_before_upload_denial(cmd: str, workspace_paths: list[str]) -> str:
  if _invokes_script(cmd, "opt_branch_audit.py"):
    return ""
  if re.search(r"\bpush\b[^;&|\n]*\s(?:--delete|-d|--tags)\b", strip_heredocs(cmd)):
    return ""
  root = _perf_repo_root(cmd, workspace_paths)
  if not root:
    return ""
  hits = _perf_claim_commits_to_publish(root)
  if not hits:
    return ""
  head = _git_out(root, "rev-parse", "HEAD")
  common = _git_out(root, "rev-parse", "--path-format=absolute", "--git-common-dir")
  try:
    with open(os.path.join(common, PERF_AUDIT_STAMP), encoding="utf-8") as f:
      if head and f.read().strip() == head:
        return ""
  except OSError:
    pass
  listed = "; ".join(hits[:5])
  return (
      f"[PERF-AUDIT GUARD] The branch about to be published has performance-claim commits "
      f"({listed}) and `opt_branch_audit.py` has not audited the current HEAD. Run "
      f"`python3 {PERF_SKILL_DIR}/scripts/opt_branch_audit.py --repo {root} <base> HEAD`, "
      "review MACHINERY / SINGLE_BENCH / NO_MEASUREMENT flags (prefer removing work over "
      "adding caches), then publish. The audit is required even when commits carry `Perf:`; "
      "commits that are not perf claims can carry a `Perf-Exempt: <reason>` line."
  )


def was_doc_grounding_run_in_chat(
    transcript_path: str, current_step_idx: int = -1
) -> bool:
  """True if check_doc_grounding.py ran since the last consumed update_doc turn."""
  if not transcript_path or not os.path.exists(transcript_path):
    return True
  steps = _load_transcript_steps(transcript_path, current_step_idx)
  if not steps:
    return True
  grounding_available = False
  consumed_by_update_doc = False
  for step in steps:
    stype = step.get("type", "")
    if stype == "USER_INPUT":
      if consumed_by_update_doc:
        grounding_available = False
        consumed_by_update_doc = False
    elif stype == "PLANNER_RESPONSE":
      for tc in step.get("tool_calls") or []:
        tname = tc.get("name", "")
        targs = tc.get("args") or {}
        if tname == "run_command":
          c = str(targs.get("CommandLine", ""))
          if _invokes_script(c, "check_doc_grounding.py"):
            grounding_available = True
            consumed_by_update_doc = False
        elif (
            tname == "call_mcp_tool"
            and str(targs.get("ToolName", "")) == "update_doc"
        ) or tname.endswith("_update_doc"):
          consumed_by_update_doc = True
  return grounding_available


# Cap consecutive source-file reads in a single main-chat turn without a subagent
MAX_TURN_SOURCE_READ_CALLS = int(
    os.environ.get("JETSKI_MAX_TURN_SOURCE_READS", "15")
)
MAX_TURN_SOURCE_READ_FILES = int(
    os.environ.get("JETSKI_MAX_TURN_SOURCE_FILES", "4")
)
_NON_SOURCE_READ_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".pdf")


def exploration_circuit_breaker_denial(
    tool_name: str,
    tool_args: dict,
    parent_conv_id: str,
    transcript_path: str,
    current_step_idx: int = -1,
) -> str:
  """Blocks runaway source-file reading sprees in the main chat without a subagent"""
  if parent_conv_id or tool_name not in READ_TOOLS:
    return ""
  target_file = get_tool_target_file(tool_args)
  if not target_file or is_agent_internal_file(target_file):
    return ""
  if target_file.lower().endswith(_NON_SOURCE_READ_EXTS):
    return ""

  steps = _load_transcript_steps(transcript_path, current_step_idx)
  read_calls = 0
  distinct_files: set[str] = set()
  for step in reversed(steps):
    stype = step.get("type", "")
    if stype == "USER_INPUT":
      break
    if stype != "PLANNER_RESPONSE":
      continue
    tcalls = step.get("tool_calls") or []
    reset_streak = False
    # Reset the read streak whenever the main chat delegates to a subagent or edits code
    for tc in tcalls:
      tname = tc.get("name", "")
      if tname == "invoke_subagent" or tname in EDIT_TOOLS:
        reset_streak = True
        break
    if reset_streak:
      break
    for tc in tcalls:
      tname = tc.get("name", "")
      if tname in READ_TOOLS:
        f = get_tool_target_file(tc.get("args") or {})
        if (
            f
            and not is_agent_internal_file(f)
            and not f.lower().endswith(_NON_SOURCE_READ_EXTS)
        ):
          read_calls += 1
          distinct_files.add(
              os.path.abspath(os.path.expanduser(f.strip('"').strip("'")))
          )

  # Deny further main-chat reads once both call and file-breadth caps are reached
  if (
      read_calls >= MAX_TURN_SOURCE_READ_CALLS
      and len(distinct_files) >= MAX_TURN_SOURCE_READ_FILES
  ):
    return (
        f"[EXPLORATION CIRCUIT BREAKER] You have already made {read_calls} file-read "
        f"calls across {len(distinct_files)} source files in this turn without delegating "
        "to a subagent or editing code. Reading dozens of file chunks in the main chat "
        "bloats the context window and causes generation collapse. Either answer the "
        "user now with the facts already gathered, or delegate any further multi-file "
        "exploration to `invoke_subagent` (`TypeName=\"research\"` or `\"self\"`)."
    )
  return ""


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
    artifact_dir: str,
    transcript_path: str,
    workspace_paths: list[str],
    state_file: str = "",
) -> None:
  """Appends an objective Git & command snapshot to the handoff summary.

  Outside projects the target is the newest handoff_summary*.md, enriched once.
  In projects it is the chain state file, which travels along the whole chain,
  so a stale snapshot from an earlier chat is replaced instead of kept.
  """
  if state_file:
    target_summary = state_file
  else:
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
      if not state_file:
        return
      cut = existing.find("\n---\n\n" + AUTO_SNAPSHOT_HEADER)
      existing = existing[: cut if cut >= 0 else existing.find(AUTO_SNAPSHOT_HEADER)]
  except Exception:
    return

  sections = ["", "---", "", AUTO_SNAPSHOT_HEADER, ""]

  # 1. Collect edited files, recent commands, and dialogue tail from transcript.jsonl
  edited_files: list[str] = []
  recent_cmds: list[str] = []
  last_user_msg = ""
  last_assistant_msg = ""
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
      import roadmap

      pending_cmds: list[str] = []
      for idx in sorted(steps_by_idx.keys()):
        step = steps_by_idx[idx]
        stype = step.get("type", "")
        if stype == "USER_INPUT":
          raw_u = str(step.get("content", "")).strip()
          m_req = re.search(
              r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", raw_u, re.DOTALL
          )
          clean_u = m_req.group(1).strip() if m_req else raw_u
          if clean_u:
            last_user_msg = clean_u
        elif stype == "PLANNER_RESPONSE":
          raw_a = str(step.get("content", "")).strip()
          if raw_a:
            last_assistant_msg = raw_a
          for tc in step.get("tool_calls") or []:
            tname = tc.get("name", "")
            targs = tc.get("args") or {}
            if tname in EDIT_TOOLS:
              tf = get_tool_target_file(targs)
              # Keep ~/.gemini/config edits across handoffs while filtering state/brain noise
              if (
                  tf
                  and not roadmap._is_ignored_internal_path(tf)
                  and tf not in edited_files
              ):
                edited_files.append(tf)
            elif tname == "run_command":
              cmd_str = str(targs.get("CommandLine", "")).strip().replace("\n", " ")
              if "new-conversation" not in cmd_str:
                pending_cmds.append(cmd_str[:140])
        elif stype == "GENERIC" and pending_cmds:
          content_s = str(step.get("content", ""))
          if "\nFile Path: " in content_s[:200]:
            continue
          pending_cmd = pending_cmds.pop(0)
          m = EXIT_CODE_RE.search(content_s)
          code_str = f"exit {m.group(1)}" if m else "async/running"
          recent_cmds.append(f"- `[{code_str}]` `{pending_cmd}`")
    except Exception:
      pass

  if last_assistant_msg or last_user_msg:
    sections.append("### Recent Dialogue Context")
    if last_assistant_msg:
      tail_a = (
          last_assistant_msg[-2500:]
          if len(last_assistant_msg) > 2500
          else last_assistant_msg
      )
      sections.append("**Last Assistant Response in Previous Chat:**")
      sections.append(tail_a)
      sections.append("")
    if last_user_msg:
      sections.append("**Latest User Message:**")
      sections.append(last_user_msg[:1500])
      sections.append("")

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
    if state_file:
      # Rewritten right before the handoff moves it, so a hook killed at its
      # timeout must leave the old content rather than a truncated file
      tmp = f"{target_summary}.tmp.{os.getpid()}"
      with open(tmp, "w", encoding="utf-8") as f:
        f.write(existing.rstrip("\n") + "\n" + "\n".join(sections) + "\n")
      os.replace(tmp, target_summary)
    else:
      with open(target_summary, "a", encoding="utf-8") as f:
        f.write("\n".join(sections) + "\n")
  except Exception:
    pass


def roadmap_edit_denial(target_file: str, conv_id: str) -> str:
  """Deny reason for a direct edit under the roadmaps dir, or "" if allowed.

  Concurrent chats share roadmap.json and only roadmap.py serializes their
  writes; a chat may edit its own chain state file and nothing else there.
  """
  if not target_file:
    return ""
  from roadmap import roadmaps_root
  root = os.path.realpath(roadmaps_root())
  path = os.path.realpath(
      os.path.expanduser(target_file.strip().strip('"').strip("'"))
  )
  if not path.startswith(root + os.sep):
    return ""
  rel = os.path.relpath(path, root).split(os.sep)
  if conv_id and len(rel) == 3 and rel[1] == "state" and rel[2] == f"{conv_id}.md":
    return ""
  return (
      f"[ROADMAP GUARD] Direct edits of `{target_file}` are blocked: roadmap files are shared by "
      "concurrent chats of the project. Use "
      "`python3 ~/.gemini/config/hooks/roadmap.py add|set|link|edit|reorder|move ...`; "
      "the only file you may edit there is your own chain state file "
      f"(`python3 ~/.gemini/config/hooks/roadmap.py state-path {conv_id or '<conversation id>'}`)."
  )


# Track the latest USER_INPUT step so self-overrides in the same turn are rejected
def _last_user_step_index(transcript_path: str) -> int:
  steps = _load_transcript_steps(transcript_path)
  for step in reversed(steps):
    if step.get("type") == "USER_INPUT":
      return int(step.get("step_index", 0))
  return 0


def _cross_project_paths(artifact_dir: str, conv_id: str) -> tuple[str, str]:
  base = os.path.join(artifact_dir, "scratch") if artifact_dir else "/tmp"
  suffix = "" if artifact_dir else f"_{conv_id}"
  return (
      os.path.join(base, f".cross_project_override{suffix}.json"),
      os.path.join(base, f".cross_project_guard_state{suffix}.json"),
  )


# Return candidate directories targeted by a git or build command
def _extract_cmd_target_dirs(cmd: str, workspace_paths: list[str]) -> list[str]:
  """Candidate directories targeted by a git or build command."""
  cmd_code = strip_heredocs(cmd)
  from context_guard import WORKSPACE_ROOT_RE

  path_arg = r"[\"']?([^\"'\s;&|)`]+)[\"']?"
  patterns = [
      ("cd", re.compile(r"(?:^|[\s;&|(`])cd\s+" + path_arg)),
      ("arg", re.compile(r"\bgit\s+-C\s+" + path_arg)),
      ("arg", re.compile(r"\b(?:auto)?(?:ninja|siso)\s+-C\s+" + path_arg)),
      ("arg", WORKSPACE_ROOT_RE),
  ]
  matches = sorted(
      (m.start(), kind, m.group(1))
      for kind, rx in patterns
      for m in rx.finditer(cmd_code)
  )
  # Relative paths (`ninja -C out/Static`) are relative to the shell's cwd after the last
  # `cd`, never to the hook process's cwd, which may be a foreign project's root
  base = next((os.path.expanduser(wp) for wp in workspace_paths if wp), "")
  dirs: list[str] = []
  for _, kind, raw in matches:
    path = os.path.expanduser(raw)
    if not os.path.isabs(path):
      if not base:
        continue
      path = os.path.join(base, path)
    path = os.path.normpath(path)
    dirs.append(path)
    if kind == "cd":
      base = path
  if not dirs:
    for wp in workspace_paths:
      if wp:
        dirs.append(os.path.abspath(os.path.expanduser(wp)))
  return dirs


# Expire worktree and file leases after 30 minutes of inactivity
WORKTREE_LEASE_TTL_S = int(os.environ.get("JETSKI_WORKTREE_LEASE_TTL_S", "1800"))
# Extend worktree lease to 2 hours while the owner chat holds a doing item in roadmap.json
WORKTREE_DOING_LEASE_TTL_S = int(
    os.environ.get("JETSKI_WORKTREE_DOING_LEASE_TTL_S", "7200")
)

WORKTREE_BUILD_PROC_RE = re.compile(
    r"\b(?:ninja|siso|felt|flutter|dart|dm|clang\+\+|run_flutter_paragraph_tests"
    r"|run_webparagraph_corpus|run_skparagraph_tests)\b"
)

WORKTREE_CLAIMING_CMD_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:"
    r"git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+"
    r"(?:checkout|switch|reset|rebase|cherry-pick|merge|commit|stash|clean|am|apply)\b"
    r"|(?:auto)?ninja\b|siso\b|felt\s+test\b|flutter\s+test\b|gn\s+gen\b"
    r")"
)


def _parallel_guard_paths(artifact_dir: str, conv_id: str) -> tuple[str, str]:
  base = os.path.join(artifact_dir, "scratch") if artifact_dir else "/tmp"
  suffix = "" if artifact_dir else f"_{conv_id}"
  return (
      os.path.join(base, f".parallel_chat_override{suffix}.json"),
      os.path.join(base, f".parallel_chat_guard_state{suffix}.json"),
  )


def _record_parallel_guard_block(guard_state_path: str, transcript_path: str) -> None:
  try:
    os.makedirs(os.path.dirname(guard_state_path), exist_ok=True)
    with open(guard_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {"blocked_at_user_step": _last_user_step_index(transcript_path)}, f
      )
  except Exception:
    pass


def _resolve_git_dirs(path: str) -> tuple[str, str, str] | None:
  """Returns (top_level, git_dir, git_common_dir) as realpaths, or None."""
  if not path:
    return None
  d = os.path.abspath(os.path.expanduser(path.strip().strip('"').strip("'")))
  for _ in range(5):
    if os.path.isdir(d):
      break
    parent = os.path.dirname(d)
    if not parent or parent == d:
      return None
    d = parent
  else:
    return None
  try:
    res = subprocess.run(
        ["git", "-C", d, "rev-parse", "--show-toplevel", "--git-dir", "--git-common-dir"],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if res.returncode != 0:
      return None
    lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
    if len(lines) < 3:
      return None
    top_level = os.path.realpath(lines[0])
    git_dir = os.path.realpath(
        lines[1] if os.path.isabs(lines[1]) else os.path.join(d, lines[1])
    )
    common_dir = os.path.realpath(
        lines[2] if os.path.isabs(lines[2]) else os.path.join(d, lines[2])
    )
    return top_level, git_dir, common_dir
  except Exception:
    return None


def _is_shared_config_repo(top_level: str) -> bool:
  """True when top_level is ~/.gemini/config (or a test shared-config repo)."""
  real_top = os.path.realpath(top_level)
  cfg = os.path.realpath(os.path.expanduser("~/.gemini/config"))
  if real_top == cfg:
    return True
  env_shared = os.environ.get("JETSKI_SHARED_CONFIG_REPO", "").strip()
  if env_shared and real_top == os.path.realpath(os.path.expanduser(env_shared)):
    return True
  return False


def _is_conv_still_active_tip(
    owner_cid: str, current_cid: str, transcript_path: str
) -> tuple[bool, str]:
  """True if owner_cid is another conversation that has not closed or handed off."""
  if not owner_cid or owner_cid == current_cid:
    return False, ""
  from context_guard import (
      APP_DATA_DIR_CANDIDATES,
      _is_archived_in_pbtxt,
      app_data_dir_from_path,
      get_conversation_db_info,
  )

  title, _ = get_conversation_db_info(owner_cid, transcript_path)
  # Finished and handed-off chats are archived, which releases whatever they leased
  if _is_archived_in_pbtxt(owner_cid, transcript_path):
    return False, title

  cand_dirs = [app_data_dir_from_path(transcript_path)] + [
      os.path.expanduser(d) for d in APP_DATA_DIR_CANDIDATES
  ]
  for ad in cand_dirs:
    if not ad:
      continue
    st_file = os.path.join(ad, "brain", owner_cid, "scratch", ".context_guard_state.json")
    if os.path.isfile(st_file):
      try:
        with open(st_file, "r", encoding="utf-8") as f:
          st = json.load(f)
        if st.get("handoff_completed") or st.get("handed_off_to"):
          return False, title
      except Exception:
        pass
  return True, title


# Return the most recent mtime among owner_cid's transcript, background task logs,
# and spawned subagent transcripts/tasks so long-running tests keep leases alive
def _latest_conv_activity_ts(
    owner_cid: str, base_ts: float, transcript_path: str
) -> float:
  latest = base_ts
  from context_guard import APP_DATA_DIR_CANDIDATES, app_data_dir_from_path

  cand_dirs: list[str] = []
  if transcript_path:
    ad = app_data_dir_from_path(transcript_path)
    if ad:
      cand_dirs.append(ad)
    parts = os.path.abspath(transcript_path).split(os.sep)
    if "brain" in parts:
      idx = parts.index("brain")
      cand_dirs.append(os.sep.join(parts[:idx]))
  for d in APP_DATA_DIR_CANDIDATES:
    cand_dirs.append(os.path.expanduser(d))

  seen_brains: set[str] = set()
  for ad in cand_dirs:
    if not ad:
      continue
    brain_root = os.path.join(ad, "brain")
    if brain_root in seen_brains or not os.path.isdir(brain_root):
      continue
    seen_brains.add(brain_root)

    conv_ids = [owner_cid]
    owner_trans = os.path.join(
        brain_root, owner_cid, ".system_generated", "logs", "transcript.jsonl"
    )
    if os.path.isfile(owner_trans):
      try:
        with open(owner_trans, "r", encoding="utf-8", errors="ignore") as f:
          txt = f.read()
        for sub_cid in re.findall(
            r'"conversationId"\s*:\s*"([0-9a-fA-F-]{8,36})"', txt
        ):
          if sub_cid not in conv_ids:
            conv_ids.append(sub_cid)
      except Exception:
        pass

    for cid in conv_ids:
      sys_dir = os.path.join(brain_root, cid, ".system_generated")
      if not os.path.isdir(sys_dir):
        continue
      for p in (
          [os.path.join(sys_dir, "logs", "transcript.jsonl")]
          + glob.glob(os.path.join(sys_dir, "tasks", "*.log"))
      ):
        try:
          mt = os.path.getmtime(p)
          if mt > latest:
            latest = mt
        except OSError:
          pass
  return latest


# Check whether a live OS build/test process or active roadmap doing item keeps the
# worktree lease valid beyond WORKTREE_LEASE_TTL_S
def _is_worktree_lease_unexpired(
    owner_cid: str,
    base_ts: float,
    top_level: str,
    transcript_path: str,
    now: float,
) -> tuple[bool, float]:
  eff_ts = _latest_conv_activity_ts(owner_cid, base_ts, transcript_path)
  if now - eff_ts < WORKTREE_LEASE_TTL_S:
    return True, eff_ts

  # Check if owner_cid holds a doing item in its project's roadmap.json
  try:
    from context_guard import get_conversation_db_info
    import roadmap

    _, pid = get_conversation_db_info(owner_cid, transcript_path)
    if pid and pid != "outside-of-project":
      rdata = _read_json_file(roadmap.roadmap_path(pid))
      for it in rdata.get("items") or []:
        if (
            isinstance(it, dict)
            and it.get("status") == "doing"
            and (
                it.get("owner") == owner_cid
                or str(it.get("owner") or "").startswith(owner_cid[:8])
            )
        ):
          if now - eff_ts < WORKTREE_DOING_LEASE_TTL_S:
            return True, eff_ts
  except Exception:
    pass

  # Check /proc for an active build/test process referencing top_level
  if os.path.isdir("/proc") and now - base_ts < 14400:
    try:
      for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
          continue
        try:
          with open(
              os.path.join(entry.path, "cmdline"), "rb"
          ) as f:
            raw = f.read(4096)
          if not raw:
            continue
          cmd_s = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
          if top_level in cmd_s and WORKTREE_BUILD_PROC_RE.search(cmd_s):
            return True, now
        except OSError:
          continue
    except OSError:
      pass

  return False, eff_ts


def _read_json_file(path: str) -> dict:
  try:
    if os.path.isfile(path):
      with open(path, "r", encoding="utf-8") as f:
        obj = json.load(f)
      if isinstance(obj, dict):
        return obj
  except Exception:
    pass
  return {}


def _write_json_atomic(path: str, data: dict) -> None:
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
  except Exception:
    pass


def _files_touched_in_chat(
    transcript_path: str, conv_id: str, current_step_idx: int = -1
) -> set[str]:
  """Realpaths of files modified via EDIT_TOOLS in this chat or its project chain state file."""
  touched: set[str] = set()
  for step in _load_transcript_steps(transcript_path, current_step_idx):
    if step.get("type") != "PLANNER_RESPONSE":
      continue
    for tc in step.get("tool_calls") or []:
      if tc.get("name", "") in EDIT_TOOLS:
        tf = get_tool_target_file(tc.get("args") or {})
        if tf:
          touched.add(
              os.path.realpath(os.path.expanduser(tf.strip().strip('"').strip("'")))
          )
  if conv_id:
    try:
      from context_guard import get_conversation_db_info
      import roadmap

      _, pid = get_conversation_db_info(conv_id, transcript_path)
      if pid and pid != "outside-of-project":
        sf = roadmap.state_path(pid, conv_id)
        if os.path.isfile(sf):
          with open(sf, "r", encoding="utf-8", errors="ignore") as f:
            txt = f.read()
          # Exclude `]` so Markdown labels like `[file:///path](file:///path)` stop at `]`
          for m in re.findall(r"file://(/[^\s`\"')\]#]+)", txt):
            touched.add(os.path.realpath(m))
    except Exception:
      pass
  return touched


def _parse_git_subcommand_invocations(cmd: str, target_subcmd: str) -> list[list[str]]:
  """Returns argument lists for each real `git <target_subcmd>` invocation in cmd."""
  cmd_code = strip_heredocs(cmd)
  results: list[list[str]] = []
  for seg in re.split(r"&&|\|\||;|\n", cmd_code):
    seg = seg.strip()
    if not seg or "git" not in seg:
      continue
    if re.match(r"^(?:grep|egrep|fgrep|rg|echo|cat)\b", seg):
      continue
    try:
      tokens = shlex.split(seg, posix=True)
    except ValueError:
      continue
    i = 0
    while i < len(tokens):
      if _clean_cmd_token(tokens[i]) == "git":
        j = i + 1
        while j < len(tokens):
          tok = tokens[j]
          if tok in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            j += 2
          elif tok.startswith("-"):
            j += 1
          else:
            break
        if j < len(tokens) and tokens[j] == target_subcmd:
          results.append(tokens[j + 1 :])
        break
      i += 1
  return results


def _analyze_git_add_args(args_list: list[str]) -> tuple[bool, list[str]]:
  """Returns (is_broad, explicit_paths) for `git add <args>`."""
  is_broad = False
  paths: list[str] = []
  after_dd = False
  for a in args_list:
    if after_dd:
      paths.append(a)
      continue
    if a == "--":
      after_dd = True
      continue
    if a in ("-A", "--all", "-u", "--update"):
      is_broad = True
    elif a.startswith("-") and not a.startswith("--") and ("A" in a or "u" in a):
      is_broad = True
    elif not a.startswith("-"):
      if a in (".", "./", "*"):
        is_broad = True
      else:
        paths.append(a)
  return is_broad, paths


def _analyze_git_commit_args(args_list: list[str]) -> tuple[bool, list[str]]:
  """Returns (is_broad_all, explicit_paths) for `git commit <args>`."""
  is_broad = False
  paths: list[str] = []
  after_dd = False
  i = 0
  opts_with_val = {
      "-m",
      "--message",
      "-F",
      "--file",
      "-c",
      "--reedit-message",
      "-C",
      "--reuse-message",
      "--author",
      "--date",
      "--cleanup",
      "-u",
      "--untracked-files",
      "-S",
      "--gpg-sign",
  }
  while i < len(args_list):
    a = args_list[i]
    if after_dd:
      paths.append(a)
      i += 1
      continue
    if a == "--":
      after_dd = True
      i += 1
      continue
    if a in opts_with_val:
      i += 2
      continue
    if a in ("-a", "--all"):
      is_broad = True
    elif a.startswith("-") and not a.startswith("--"):
      # Combined short flags such as `-am "msg"` or `-ma "msg"`
      flag_chars = a[1:]
      if "m" in flag_chars or "F" in flag_chars or "c" in flag_chars or "C" in flag_chars:
        idx_val = min(
            idx for idx, ch in enumerate(flag_chars) if ch in ("m", "F", "c", "C")
        )
        if "a" in flag_chars[:idx_val]:
          is_broad = True
        if idx_val == len(flag_chars) - 1:
          i += 2
          continue
      elif "a" in flag_chars:
        is_broad = True
    elif not a.startswith("-"):
      if a in (".", "./"):
        is_broad = True
      else:
        paths.append(a)
    i += 1
  return is_broad, paths


def _git_head_sha(repo_root: str) -> str:
  try:
    res = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    if res.returncode == 0:
      return res.stdout.strip()
  except Exception:
    pass
  return ""


def _sync_repo_commits(top_level: str, git_common_dir: str) -> dict:
  """Attributes any newly created commits since the last pending git commit call."""
  reg_path = os.path.join(git_common_dir, "jetski_commits.json")
  reg = _read_json_file(reg_path)
  pending_map = reg.get("pending")
  if not isinstance(pending_map, dict) or top_level not in pending_map:
    return reg
  pend = pending_map.get(top_level) or {}
  head_before = str(pend.get("head_before") or "").strip()
  cur_head = _git_head_sha(top_level)
  if not cur_head or not head_before or cur_head == head_before:
    return reg
  try:
    res = subprocess.run(
        ["git", "-C", top_level, "log", "--format=%H%x00%s", f"{head_before}..{cur_head}"],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if res.returncode == 0:
      commits = reg.setdefault("commits", {})
      now = time.time()
      for ln in res.stdout.splitlines():
        if "\x00" in ln:
          sha, subj = ln.split("\x00", 1)
          sha = sha.strip()
          if sha and sha not in commits:
            commits[sha] = {
                "conv_id": pend.get("conv_id", ""),
                "title": pend.get("title", ""),
                "subject": subj.strip(),
                "repo": top_level,
                "ts": now,
                "pushed_by_owner": bool(pend.get("pushed_by_owner")),
            }
      pending_map.pop(top_level, None)
      _write_json_atomic(reg_path, reg)
  except Exception:
    pass
  return reg


def _active_foreign_file_leases(
    git_dir: str, effective_cid: str, transcript_path: str
) -> dict[str, dict]:
  """Returns {rel_path: lease_info} for files actively leased by other conversations."""
  now = time.time()
  data = _read_json_file(os.path.join(git_dir, "jetski_file_leases.json"))
  files = data.get("files") or {}
  out: dict[str, dict] = {}
  for rel, info in files.items():
    if not isinstance(info, dict):
      continue
    owner_cid = str(info.get("conv_id") or "")
    ts = float(info.get("ts") or 0.0)
    if owner_cid and owner_cid != effective_cid:
      eff_ts = _latest_conv_activity_ts(owner_cid, ts, transcript_path)
      if now - eff_ts < WORKTREE_LEASE_TTL_S:
        still_active, db_title = _is_conv_still_active_tip(
            owner_cid, effective_cid, transcript_path
        )
        if still_active:
          out[rel] = {
              **info,
              "title": db_title or info.get("title") or owner_cid[:8],
              "ts": eff_ts,
          }
  return out


# Classify unpushed branch commits into active foreign labels and own SHAs
def inspect_unpushed_commit_owners(
    top_level: str,
    git_dir: str,
    git_common_dir: str,
    effective_cid: str,
    transcript_path: str,
    step_idx: int = -1,
    reg: dict | None = None,
) -> tuple[list[str], list[str], dict]:
  if reg is None:
    reg = _sync_repo_commits(top_level, git_common_dir)
  upstream = "@{u}"
  up_res = subprocess.run(
      ["git", "-C", top_level, "rev-parse", "--verify", "@{u}"],
      capture_output=True,
      text=True,
      timeout=1.0,
      check=False,
  )
  if up_res.returncode != 0:
    upstream = "origin/main"
  rev_res = subprocess.run(
      ["git", "-C", top_level, "log", "--format=%H%x00%s", f"{upstream}..HEAD"],
      capture_output=True,
      text=True,
      timeout=1.5,
      check=False,
  )
  commits_map = reg.get("commits") if isinstance(reg.get("commits"), dict) else {}
  foreign_leased = _active_foreign_file_leases(
      git_dir, effective_cid, transcript_path
  )
  touched_abs = _files_touched_in_chat(transcript_path, effective_cid, step_idx)
  touched_in_repo = {
      os.path.relpath(p, top_level)
      for p in touched_abs
      if p.startswith(top_level + os.sep)
  }
  foreign_commits: list[str] = []
  own_unpushed_shas: list[str] = []
  if rev_res.returncode != 0:
    return foreign_commits, own_unpushed_shas, reg
  for ln in rev_res.stdout.splitlines():
    if "\x00" not in ln:
      continue
    sha, subj = [x.strip() for x in ln.split("\x00", 1)]
    if not sha:
      continue
    cinfo = commits_map.get(sha)
    if isinstance(cinfo, dict):
      owner_cid = str(cinfo.get("conv_id") or "")
      if owner_cid == effective_cid:
        own_unpushed_shas.append(sha)
      # Archived owner chats have already handed off or dropped their leases
      elif owner_cid:
        still_active, db_title = _is_conv_still_active_tip(
            owner_cid, effective_cid, transcript_path
        )
        if still_active:
          owner_label = db_title or cinfo.get("title") or owner_cid[:8]
          foreign_commits.append(
              f'`{sha[:7]}` ("{subj}", by `{owner_label}` -> '
              f"`conversation://{owner_cid}`)"
          )
    # Manual git commits lack a registry entry; fall back to active file leases
    elif foreign_leased:
      dt_res = subprocess.run(
          [
              "git",
              "-C",
              top_level,
              "diff-tree",
              "--no-commit-id",
              "--name-only",
              "-r",
              sha,
          ],
          capture_output=True,
          text=True,
          timeout=1.0,
          check=False,
      )
      if dt_res.returncode == 0:
        c_files = [x.strip() for x in dt_res.stdout.splitlines() if x.strip()]
        if (
            c_files
            and all(f in foreign_leased for f in c_files)
            and not any(f in touched_in_repo for f in c_files)
        ):
          finfo = foreign_leased[c_files[0]]
          foreign_commits.append(
              f'`{sha[:7]}` ("{subj}", by `{finfo["title"]}` -> '
              f"`conversation://{finfo['conv_id']}`)"
          )
  return foreign_commits, own_unpushed_shas, reg


def _check_and_claim_worktree_lease(
    git_dir: str,
    top_level: str,
    effective_cid: str,
    cur_title: str,
    override_path: str,
    guard_state_path: str,
    transcript_path: str,
    now: float,
) -> str:
  """Verifies or acquires the exclusive per-worktree lease for a code repository."""
  lease_file = os.path.join(git_dir, "jetski_lease.json")
  existing = _read_json_file(lease_file)
  owner_cid = str(existing.get("conv_id") or "")
  ts = float(existing.get("ts") or 0.0)
  if (
      owner_cid
      and owner_cid != effective_cid
      and not os.path.exists(override_path)
  ):
    still_active, db_title = _is_conv_still_active_tip(
        owner_cid, effective_cid, transcript_path
    )
    if still_active:
      unexpired, eff_ts = _is_worktree_lease_unexpired(
          owner_cid, ts, top_level, transcript_path, now
      )
      if unexpired:
        _record_parallel_guard_block(guard_state_path, transcript_path)
        owner_label = db_title or existing.get("title") or owner_cid[:8]
        age_m = int(max(1, (now - eff_ts) // 60))
        return (
            f"[WORKTREE LEASE GUARD] Worktree `{top_level}` is currently leased by "
            f"another active conversation (`{owner_label}` -> `conversation://{owner_cid}`, "
            f"active {age_m}m ago). Sharing a single code worktree or `out/` build directory "
            f"between concurrent chats causes branch, build, and test collisions! "
            f"Create and use a separate git worktree (e.g. "
            f"`git -C {top_level} worktree add {top_level}_wt_{effective_cid[:6]} -b <branch>`) "
            f"or ask the user whether to take over `{top_level}`. Only after the user "
            f"explicitly confirms in a subsequent turn may you write `{override_path}`."
        )
  _write_json_atomic(
      lease_file,
      {
          "conv_id": effective_cid,
          "title": cur_title,
          "ts": now,
          "worktree": top_level,
      },
  )
  return ""


def parallel_chat_isolation_denial(
    tool_name: str,
    tool_args: dict,
    conv_id: str,
    parent_conv_id: str,
    transcript_path: str,
    artifact_dir: str,
    workspace_paths: list[str],
    step_idx: int = -1,
) -> str:
  """Enforces per-worktree leases, shared-repo staging, and commit-ownership push guards."""
  effective_cid = parent_conv_id or conv_id
  if not effective_cid:
    return ""

  override_path, guard_state_path = _parallel_guard_paths(
      artifact_dir, effective_cid
  )

  if tool_name in EDIT_TOOLS:
    raw_target = get_tool_target_file(tool_args)
    if not raw_target:
      return ""
    clean_target = os.path.abspath(
        os.path.expanduser(raw_target.strip().strip('"').strip("'"))
    )
    if clean_target == os.path.abspath(override_path) or os.path.basename(
        clean_target
    ).startswith(".parallel_chat_override"):
      if os.path.exists(guard_state_path):
        gstate = _read_json_file(guard_state_path)
        blocked_step = int(gstate.get("blocked_at_user_step", -1))
        if (
            blocked_step >= 0
            and _last_user_step_index(transcript_path) <= blocked_step
        ):
          return (
              "[PARALLEL-CHAT GUARD] You cannot create `.parallel_chat_override.json` "
              "in the same turn that a parallel-chat guard blocked your action! "
              "Stop immediately, explain the collision to the user, and wait for "
              "the user's confirmation in the next turn."
          )
      return ""

    # Skip agent scratch/brain/roadmap/project/log files
    from context_guard import app_data_dir_from_path, get_conversation_db_info

    if (
        clean_target.startswith("/tmp/jetski_")
        or app_data_dir_from_path(clean_target)
        or any(
            seg in clean_target.replace("\\", "/")
            for seg in (
                "/.gemini/config/logs/",
                "/.gemini/config/roadmaps/",
                "/.gemini/config/projects/",
            )
        )
    ):
      return ""

    git_info = _resolve_git_dirs(clean_target)
    if not git_info:
      return ""
    top_level, git_dir, git_common_dir = git_info
    _sync_repo_commits(top_level, git_common_dir)
    now = time.time()
    cur_title, _ = get_conversation_db_info(effective_cid, transcript_path)

    if _is_shared_config_repo(top_level):
      rel_path = os.path.relpath(os.path.realpath(clean_target), top_level)
      lease_file = os.path.join(git_dir, "jetski_file_leases.json")
      data = _read_json_file(lease_file)
      files_map = data.get("files") if isinstance(data.get("files"), dict) else {}
      existing = files_map.get(rel_path)
      if isinstance(existing, dict):
        owner_cid = str(existing.get("conv_id") or "")
        ts = float(existing.get("ts") or 0.0)
        if (
            owner_cid
            and owner_cid != effective_cid
            and not os.path.exists(override_path)
        ):
          eff_ts = _latest_conv_activity_ts(owner_cid, ts, transcript_path)
          if now - eff_ts < WORKTREE_LEASE_TTL_S:
            still_active, db_title = _is_conv_still_active_tip(
                owner_cid, effective_cid, transcript_path
            )
            if still_active:
              _record_parallel_guard_block(guard_state_path, transcript_path)
              owner_label = db_title or existing.get("title") or owner_cid[:8]
              age_m = int(max(1, (now - eff_ts) // 60))
              return (
                  f"[SHARED-FILE LEASE GUARD] File `{rel_path}` in `{top_level}` is currently "
                  f"leased by another active conversation (`{owner_label}` -> "
                  f"`conversation://{owner_cid}`, edited {age_m}m ago). Do NOT overwrite "
                  f"another active chat's file concurrently! Ask the user first; only after "
                  f"the user explicitly confirms in a subsequent turn may you write "
                  f"`{override_path}`."
              )
      pruned = {
          k: v
          for k, v in files_map.items()
          if isinstance(v, dict)
          and now
          - _latest_conv_activity_ts(
              str(v.get("conv_id") or ""),
              float(v.get("ts") or 0.0),
              transcript_path,
          )
          < WORKTREE_LEASE_TTL_S
      }
      pruned[rel_path] = {
          "conv_id": effective_cid,
          "title": cur_title,
          "ts": now,
      }
      _write_json_atomic(lease_file, {"files": pruned})
      return ""

    # Code repository / worktree: enforce exclusive per-worktree lease
    return _check_and_claim_worktree_lease(
        git_dir,
        top_level,
        effective_cid,
        cur_title,
        override_path,
        guard_state_path,
        transcript_path,
        now,
    )

  if tool_name != "run_command":
    return ""

  cmd_str = str(tool_args.get("CommandLine", "")).strip()
  if not cmd_str:
    return ""
  cmd_code = _non_inspection_cmd_segments(cmd_str)
  if not cmd_code:
    return ""

  cand_dirs = _extract_cmd_target_dirs(cmd_str, workspace_paths)
  if not cand_dirs:
    return ""

  resolved_repos: list[tuple[str, str, str]] = []
  seen_tops: set[str] = set()
  for d in cand_dirs:
    info = _resolve_git_dirs(d)
    if info and info[0] not in seen_tops:
      seen_tops.add(info[0])
      resolved_repos.append(info)
  if not resolved_repos:
    return ""

  from context_guard import get_conversation_db_info

  now = time.time()
  cur_title, _ = get_conversation_db_info(effective_cid, transcript_path)
  add_invocations = _parse_git_subcommand_invocations(cmd_str, "add")
  commit_invocations = _parse_git_subcommand_invocations(cmd_str, "commit")
  push_invocations = [
      args
      for args in _parse_git_subcommand_invocations(cmd_str, "push")
      if not any(a in ("-h", "--help", "help", "--delete", "-d") for a in args)
  ]
  is_claiming_cmd = bool(WORKTREE_CLAIMING_CMD_RE.search(cmd_code))

  for top_level, git_dir, git_common_dir in resolved_repos:
    reg = _sync_repo_commits(top_level, git_common_dir)

    # Code worktree lease check on mutating git / build / test commands
    if not _is_shared_config_repo(top_level) and is_claiming_cmd:
      denial = _check_and_claim_worktree_lease(
          git_dir,
          top_level,
          effective_cid,
          cur_title,
          override_path,
          guard_state_path,
          transcript_path,
          now,
      )
      if denial:
        return denial

    # Shared-worktree per-file stage and commit guard
    if (add_invocations or commit_invocations) and not os.path.exists(override_path):
      touched_abs = _files_touched_in_chat(transcript_path, effective_cid, step_idx)
      lease_data = _read_json_file(os.path.join(git_dir, "jetski_file_leases.json"))
      for rel, linfo in (lease_data.get("files") or {}).items():
        if isinstance(linfo, dict) and linfo.get("conv_id") == effective_cid:
          touched_abs.add(os.path.realpath(os.path.join(top_level, rel)))
      touched_in_repo = {
          os.path.relpath(p, top_level)
          for p in touched_abs
          if p.startswith(top_level + os.sep)
      }
      foreign_leased = _active_foreign_file_leases(
          git_dir, effective_cid, transcript_path
      )

      broad_add = False
      explicit_add_rels: set[str] = set()
      for a_args in add_invocations:
        b, paths = _analyze_git_add_args(a_args)
        if b:
          broad_add = True
        for p in paths:
          abs_p = os.path.realpath(
              p if os.path.isabs(p) else os.path.join(top_level, p)
          )
          if abs_p == top_level:
            broad_add = True
          elif abs_p.startswith(top_level + os.sep):
            explicit_add_rels.add(os.path.relpath(abs_p, top_level))

      broad_commit = False
      pathspec_less_commit = False
      explicit_commit_rels: set[str] = set()
      for c_args in commit_invocations:
        if any(a in ("--amend", "-h", "--help", "help") for a in c_args):
          continue
        b, paths = _analyze_git_commit_args(c_args)
        if b:
          broad_commit = True
        elif not paths:
          pathspec_less_commit = True
        else:
          for p in paths:
            abs_p = os.path.realpath(
                p if os.path.isabs(p) else os.path.join(top_level, p)
            )
            if abs_p == top_level:
              broad_commit = True
            elif abs_p.startswith(top_level + os.sep):
              explicit_commit_rels.add(os.path.relpath(abs_p, top_level))

      # Block explicitly staging or committing a file actively leased by another chat
      for rel in sorted(explicit_add_rels | explicit_commit_rels):
        if rel in foreign_leased and rel not in touched_in_repo:
          finfo = foreign_leased[rel]
          _record_parallel_guard_block(guard_state_path, transcript_path)
          own_list = (
              ", ".join(f"`{x}`" for x in sorted(touched_in_repo)) or "none"
          )
          return (
              f"[SHARED-WORKTREE COMMIT GUARD] Cannot stage or commit `{rel}` in `{top_level}`: "
              f"that file is actively leased by another conversation (`{finfo['title']}` -> "
              f"`conversation://{finfo['conv_id']}`). Stage and commit only the files you "
              f"modified in this conversation ({own_list})."
          )

      # Block broad git add/commit when foreign dirty files exist in the working tree
      if broad_add or broad_commit:
        try:
          st_res = subprocess.run(
              ["git", "-C", top_level, "status", "--porcelain", "-uall"],
              capture_output=True,
              text=True,
              timeout=1.5,
              check=False,
          )
          dirty_rels: list[str] = []
          if st_res.returncode == 0:
            for ln in st_res.stdout.splitlines():
              if len(ln) > 3:
                raw_p = ln[3:].strip().split(" -> ")[-1].strip('"')
                if raw_p:
                  dirty_rels.append(raw_p)
          foreign_dirty = [
              r
              for r in dirty_rels
              if r in foreign_leased
              or (touched_in_repo and r not in touched_in_repo)
          ]
          if foreign_dirty:
            _record_parallel_guard_block(guard_state_path, transcript_path)
            own_hint = (
                " ".join(sorted(touched_in_repo))
                if touched_in_repo
                else "<your_file1> <your_file2>"
            )
            foreign_list = ", ".join(f"`{x}`" for x in foreign_dirty[:5])
            return (
                f"[SHARED-WORKTREE COMMIT GUARD] Working tree `{top_level}` contains modified "
                f"or untracked files from other conversations ({foreign_list}). "
                f"Never run `git add .`, `git add -A`, or `git commit -a` in a shared working "
                f"tree! Stage and commit ONLY your own files explicitly:\n"
                f"`git -C {top_level} add -- {own_hint} && "
                f"git -C {top_level} commit -m \"...\" -- {own_hint}`"
            )
        except Exception:
          pass

      # Block pathspec-less git commit when .git/index already has foreign staged files
      if pathspec_less_commit:
        try:
          cached_res = subprocess.run(
              ["git", "-C", top_level, "diff", "--cached", "--name-only"],
              capture_output=True,
              text=True,
              timeout=1.5,
              check=False,
          )
          if cached_res.returncode == 0:
            staged_rels = [
                ln.strip() for ln in cached_res.stdout.splitlines() if ln.strip()
            ]
            allowed_rels = touched_in_repo | explicit_add_rels
            foreign_staged = [
                r
                for r in staged_rels
                if r in foreign_leased
                or (allowed_rels and r not in allowed_rels)
            ]
            if foreign_staged:
              _record_parallel_guard_block(guard_state_path, transcript_path)
              own_hint = (
                  " ".join(sorted(allowed_rels))
                  if allowed_rels
                  else "<your_file1> <your_file2>"
              )
              foreign_list = ", ".join(f"`{x}`" for x in foreign_staged[:5])
              return (
                  f"[SHARED-WORKTREE COMMIT GUARD] The Git index in `{top_level}` currently has "
                  f"staged file(s) from another conversation ({foreign_list}). "
                  f"Running `git commit` without file pathspecs would sweep them into your commit! "
                  f"Pass explicit file pathspecs to `git commit`:\n"
                  f"`git -C {top_level} commit -m \"...\" -- {own_hint}`"
              )
        except Exception:
          pass

    # Foreign-commit push guard before git push
    if push_invocations and not os.path.exists(override_path):
      try:
        foreign_commits, own_unpushed_shas, reg = (
            inspect_unpushed_commit_owners(
                top_level,
                git_dir,
                git_common_dir,
                effective_cid,
                transcript_path,
                step_idx,
                reg,
            )
        )
        if foreign_commits:
          _record_parallel_guard_block(guard_state_path, transcript_path)
          return (
              f"[FOREIGN-COMMIT PUSH GUARD] Branch in `{top_level}` has unpushed commit(s) "
              f"created by another active conversation: {', '.join(foreign_commits)}. "
              f"Do NOT push another chat's local commits without explicit user permission! "
              f"Ask the user whether to push them together with yours; only after the user "
              f"explicitly confirms in a subsequent turn may you write `{override_path}`."
          )
        if own_unpushed_shas:
          commits_map = (
              reg.get("commits") if isinstance(reg.get("commits"), dict) else {}
          )
          for sha in own_unpushed_shas:
            if isinstance(commits_map.get(sha), dict):
              commits_map[sha]["pushed_by_owner"] = True
          _write_json_atomic(
              os.path.join(git_common_dir, "jetski_commits.json"), reg
          )
      except Exception:
        pass

    # Record pending commit ownership when git commit is about to run
    if commit_invocations:
      cur_head = _git_head_sha(top_level)
      if cur_head:
        reg.setdefault("pending", {})[top_level] = {
            "conv_id": effective_cid,
            "title": cur_title,
            "head_before": cur_head,
            "ts": now,
        }
        if push_invocations:
          reg["pending"][top_level]["pushed_by_owner"] = True
        _write_json_atomic(
            os.path.join(git_common_dir, "jetski_commits.json"), reg
        )

  return ""


def cross_project_write_denial(
    tool_name: str,
    tool_args: dict,
    conv_id: str,
    parent_conv_id: str,
    transcript_path: str,
    artifact_dir: str,
    workspace_paths: list[str],
) -> str:
  """Deny reason when a chat in project A edits or commits inside project B's exclusive root."""
  effective_cid = parent_conv_id or conv_id
  override_path, guard_state_path = _cross_project_paths(
      artifact_dir, effective_cid
  )

  if tool_name in EDIT_TOOLS:
    raw_target = get_tool_target_file(tool_args)
    if not raw_target:
      return ""
    clean_target = os.path.abspath(
        os.path.expanduser(raw_target.strip().strip('"').strip("'"))
    )
    if clean_target == os.path.abspath(override_path) or os.path.basename(
        clean_target
    ).startswith(".cross_project_override"):
      if os.path.exists(guard_state_path):
        try:
          with open(guard_state_path, "r", encoding="utf-8") as f:
            gstate = json.load(f)
          blocked_step = int(gstate.get("blocked_at_user_step", -1))
          if (
              blocked_step >= 0
              and _last_user_step_index(transcript_path) <= blocked_step
          ):
            return (
                "[CROSS-PROJECT SCOPE GUARD] You cannot create `.cross_project_override.json` "
                "in the same turn that a cross-project edit was blocked! Stop immediately, "
                "ask the user whether to forward the request via `env -u ANTIGRAVITY_SOURCE_METADATA "
                "agentapi send-message` or execute it here, and wait for the user's reply in the next turn."
            )
        except Exception:
          pass
      return ""
    candidate_paths = [clean_target]
  elif tool_name == "run_command":
    cmd_str = str(tool_args.get("CommandLine", "")).strip()
    if not is_git_commit_or_upload_command(cmd_str):
      return ""
    candidate_paths = _extract_cmd_target_dirs(cmd_str, workspace_paths)
    if not candidate_paths:
      return ""
  else:
    return ""

  if os.path.exists(override_path):
    return ""

  from context_guard import collect_project_directory, get_conversation_db_info

  _, current_pid = get_conversation_db_info(effective_cid, transcript_path)
  if not current_pid or current_pid == "outside-of-project":
    return ""

  # The roadmaps dir lives inside ~/.gemini/config, which one project owns, yet every
  # project keeps its chains' state files there; a chat may always write its own one
  if tool_name in EDIT_TOOLS:
    import roadmap

    real_target = os.path.realpath(candidate_paths[0])
    for cid in {conv_id, effective_cid} - {""}:
      try:
        own_state = os.path.realpath(roadmap.state_path(current_pid, cid))
      except Exception:
        continue
      if real_target == own_state:
        return ""

  directory = collect_project_directory(
      transcript_path, include_active_chats=True, exclude_conv_id=effective_cid
  )
  excl = directory["exclusive_roots"]
  if not excl:
    return ""

  matched_root, owner_pid = "", ""
  for cand in candidate_paths:
    best_len = -1
    for root_path, pid in excl.items():
      if cand == root_path or cand.startswith(root_path + os.sep):
        if len(root_path) > best_len:
          best_len = len(root_path)
          matched_root = root_path
          owner_pid = pid
    if owner_pid and owner_pid != current_pid:
      break
  else:
    return ""

  try:
    os.makedirs(os.path.dirname(guard_state_path), exist_ok=True)
    with open(guard_state_path, "w", encoding="utf-8") as f:
      json.dump(
          {"blocked_at_user_step": _last_user_step_index(transcript_path)}, f
      )
  except Exception:
    pass

  projects = directory["projects"]
  cur_proj_name = (projects.get(current_pid) or {}).get("name") or current_pid
  owner_proj = projects.get(owner_pid) or {}
  owner_name = owner_proj.get("name") or owner_pid
  active_chat = owner_proj.get("active_chat")
  active_hint = (
      f" (active chat: `{active_chat[1]}` -> `conversation://{active_chat[0]}`)"
      if active_chat
      else ""
  )
  forward_cmd = (
      f'`env -u ANTIGRAVITY_SOURCE_METADATA agentapi send-message {active_chat[0]} "<user message verbatim>"`'
      if active_chat
      else '`env -u ANTIGRAVITY_SOURCE_METADATA agentapi send-message <target_conv_id> "<user message verbatim>"`'
  )
  return (
      f"[CROSS-PROJECT SCOPE GUARD] You are in project `{cur_proj_name}`, but attempting to "
      f"modify or commit files in `{matched_root}`, which belongs exclusively to project "
      f"`{owner_name}`{active_hint}. Do NOT modify foreign project files silently! "
      f"Stop immediately, warn the user that this request targets `{owner_name}`, and ask "
      f"whether to forward the message there via {forward_cmd} or execute it here. "
      f"Only after the user explicitly confirms executing it in this chat in a subsequent turn "
      f"may you write `{override_path}` to unlock cross-project edits."
  )


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

  if tool_name in EDIT_TOOLS:
    denial = roadmap_edit_denial(get_tool_target_file(tool_args), conv_id)
    if denial:
      print(json.dumps({"decision": "deny", "reason": denial}, ensure_ascii=False))
      return

  # Block edits or git commits inside another project's exclusive workspace root
  if tool_name in EDIT_TOOLS or tool_name == "run_command":
    xp_denial = cross_project_write_denial(
        tool_name,
        tool_args,
        conv_id,
        parent_conv_id,
        transcript_path,
        artifact_dir,
        workspace_paths,
    )
    if xp_denial:
      print(
          json.dumps(
              {"decision": "deny", "reason": xp_denial}, ensure_ascii=False
          )
      )
      return

  # 1. Top-level conversation handoff guards
  if not parent_conv_id:
    from context_guard import (
        build_agentapi_prefix,
        build_continuation_title,
        get_handoff_launch_step_in_turn,
        load_state,
        resolve_handoff_target,
    )
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
      launched = (
          f"at step #{launch_step}" if launch_step >= 0 else "recorded by chat_lifecycle.py"
      )
      if state.get("handed_off_to"):
        launched += f", continuation {state['handed_off_to']}"
      if tool_name in EDIT_TOOLS:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[PRE-TOOL GUARD] This conversation has already launched a continuation chat ({launched})! "
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
                        f"[PRE-TOOL GUARD] This conversation has already launched a continuation chat ({launched})! "
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

      # Gate both handoff and next-task against duplicate launches in one shell command
      if is_chat_lifecycle_handoff_launch(cmd) and not re.search(
          r"\b(?:handoff|next-task)\s+(?:--help|-h|help)\b", cmd
      ):
        if count_chat_lifecycle_handoff_launches(cmd) > 1:
          print(
              json.dumps(
                  {
                      "decision": "deny",
                      "reason": (
                          "[PRE-TOOL GUARD] Do not run `chat_lifecycle.py handoff` multiple times in a single command! "
                          "Launch exactly ONE continuation chat."
                      ),
                  },
                  ensure_ascii=False,
              )
          )
          return

        # next-task seeds a fresh state file for the new roadmap item itself
        has_notes_flag = "--notes" in strip_heredocs(cmd) or is_chat_lifecycle_next_task_launch(cmd)
        ref_match = re.search(r"(/\S*handoff_summary[^\s\"']*\.md)", cmd)
        if state.get("pending_handoff_launch") and state.get("pending_handoff_file"):
          target = {
              "in_project": bool(state.get("pending_in_project")),
              "file": state["pending_handoff_file"],
          }
        else:
          target = resolve_handoff_target(conv_id, transcript_path, artifact_dir)
        missing_summary = None
        if not has_notes_flag:
          if ref_match and not os.path.exists(ref_match.group(1)):
            missing_summary = ref_match.group(1)
          elif target["in_project"]:
            # chat_lifecycle seeds the state file from an existing summary
            # argument, so only a chat with neither must be stopped here
            if not os.path.exists(target["file"]) and not ref_match:
              missing_summary = target["file"]
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
                          "and only then call `chat_lifecycle.py handoff`."
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
            artifact_dir,
            transcript_path,
            workspace_paths,
            state_file=target["file"] if target["in_project"] else "",
        )

      cmd_non_inspect = _non_inspection_cmd_segments(cmd)
      if "agentapi" in cmd_non_inspect and not re.match(
          r"^\s*python3?\b", cmd_non_inspect
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

        if (
            AGENTAPI_SEND_MSG_RE.search(cmd)
            and "ANTIGRAVITY_SOURCE_METADATA" not in cmd
        ):
          rewritten = AGENTAPI_SEND_MSG_RE.sub(
              lambda m: f"env -u ANTIGRAVITY_SOURCE_METADATA {m.group(0).lstrip()}",
              cmd,
              count=1,
          )
          if rewritten != cmd:
            print(
                json.dumps(
                    {"decision": "allow", "overwrite": {"CommandLine": rewritten}},
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
              and not state.get("pending_in_project")
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

          # Check for explicit project flag in command line
          target_proj = ""
          proj_match = re.search(r'--project(?:=|\s+)(["\']?)([^"\'\s]+)\1', rewritten)
          if proj_match:
            target_proj = proj_match.group(2)
            rewritten = re.sub(
                r'--project(?:=|\s+)(["\']?)[^"\'\s]+\1', "", rewritten
            ).strip()
            needs_rewrite = True

          # Replace bare `/.../agentapi new-conversation` or `agentapi new-conversation`
          # with prefix if `ANTIGRAVITY_SOURCE_METADATA` is missing
          if target_proj:
            prefix_to_use = (
                "env -u ANTIGRAVITY_SOURCE_METADATA"
                f' ANTIGRAVITY_PROJECT_ID="{target_proj}" agentapi new-conversation'
            )
            rewritten = re.sub(
                r'(?:(?:env\s+)?ANTIGRAVITY_PROJECT_ID=[^\s]+\s+)?(?:\S*/)?agentapi\s+new-conversation\b',
                prefix_to_use,
                rewritten,
                count=1,
            )
            needs_rewrite = True
          elif "ANTIGRAVITY_SOURCE_METADATA" not in rewritten:
            rewritten = re.sub(
                r'(?:(?:env\s+)?ANTIGRAVITY_PROJECT_ID=[^\s]+\s+)?(?:\S*/)?agentapi\s+new-conversation\b',
                cmd_prefix,
                rewritten,
                count=1,
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
    target_file = get_tool_target_file(tool_args)
    if is_tracked_source_file(target_file):
      # Guard 3: Block editing gitignored / build-cache files
      ignored, reason_detail = is_gitignored_or_build_cache(target_file)
      if ignored:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[GENERATED-FILE GUARD] Editing `{target_file}` is blocked: "
                        f"{reason_detail}. Do not edit generated artifacts or build caches "
                        "directly — edit the tracked source file in the repository and "
                        "rebuild the project."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

      # Guard 2: Read-Before-Edit on existing files for replace/edit tools
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
                          "[READ-BEFORE-EDIT GUARD] You are attempting to modify an existing "
                          f"file `{os.path.basename(clean_path)}` without reading it via "
                          "`read_file` / `view_file` in the current conversation! Do not edit "
                          "code blindly from memory or from a `handoff_summary`: first read the "
                          "target section of the file, verify exact signatures and context, and "
                          f"only then call `{tool_name}`."
                      ),
                  },
                  ensure_ascii=False,
              )
          )
          return

  # Guard 7: Pre-commit / pre-upload code audit check (amnesia-precommit Phase 1)
  if tool_name == "run_command":
    cmd_str = str(tool_args.get("CommandLine", "")).strip()
    step_idx = int(data.get("stepIdx", -1))
    # Require fast Phase 1 lint/audit scripts on every commit, upload, PR creation, or push
    is_auto_ship_cmd = _invokes_script(cmd_str, "auto_ship.py") and bool(
        re.search(r"\bauto_ship\.py\s+ship\b", cmd_str)
    )
    if (
        is_git_commit_or_upload_command(cmd_str)
        or is_git_upload_or_push_command(cmd_str)
        or is_auto_ship_cmd
    ):
      unverified_src = unverified_source_edit_before_commit(
          transcript_path, cmd_str, step_idx
      )
      if unverified_src:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[CODE-CHECK GUARD] You modified tracked source code (`{unverified_src}`) "
                        "in this conversation without running both `amnesia-precommit` Phase 1 "
                        "audit scripts since the last code edit! Before `git commit` / "
                        "`git cl upload` / `gh pr create` / `git push`, run:\n"
                        "`python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py "
                        "--diff HEAD --repo <repo>`\n"
                        "`python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/"
                        "ruthless_audit.py <repo> --repo <repo> --diff HEAD`"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

    # A speedup claim must carry its measurements in the same commit (1 win = 1 commit)
    perf_commit = perf_claim_commit_denial(cmd_str, workspace_paths)
    if perf_commit:
      print(json.dumps({"decision": "deny", "reason": perf_commit}, ensure_ascii=False))
      return

    # Require the full Flutter test corpus only on upload/push/PR when executable code changed
    if is_git_upload_or_push_command(cmd_str):
      subsys, edited_code_file, req_script = (
          unverified_corpus_before_upload_or_push(
              transcript_path, cmd_str, step_idx
          )
      )
      if edited_code_file:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        f"[CORPUS-CHECK GUARD] You modified executable production code in "
                        f"`{edited_code_file}` ({subsys}), but have not run the Flutter test "
                        "corpus since the last code edit! Before `git cl upload`, `gh pr create`, "
                        "or `git push`, run:\n"
                        f"`python3 {req_script} --out /tmp/corpus_check`\n"
                        "(Note: comment-only, summary, or commit-message edits do not trigger "
                        "this requirement.)"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return
      # Perf branches get the "remove work rather than add machinery" audit before review
      perf_audit = perf_audit_before_upload_denial(cmd_str, workspace_paths)
      if perf_audit:
        print(json.dumps({"decision": "deny", "reason": perf_audit}, ensure_ascii=False))
        return

  # Guard 8: Google Docs update_doc grounding check (amnesia-doc-check Phase 1)
  if (
      tool_name == "call_mcp_tool"
      and str(tool_args.get("ToolName", "")) == "update_doc"
  ) or tool_name.endswith("_update_doc"):
    if not was_doc_grounding_run_in_chat(
        transcript_path, int(data.get("stepIdx", -1))
    ):
      print(
          json.dumps(
              {
                  "decision": "deny",
                  "reason": (
                      "[DOC-CHECK GUARD] Updating a Google Doc (`update_doc`) requires running "
                      "`check_doc_grounding.py` (and the `amnesia-doc-check` workflow for "
                      "substantive edits) before publishing changes. Save the draft to "
                      "`<artifact_dir>/scratch/draft.md`, run "
                      "`python3 ~/.gemini/config/skills/amnesia-doc-check/scripts/"
                      "check_doc_grounding.py --repo <repo> <draft>`, and then call `update_doc`."
                  ),
              },
              ensure_ascii=False,
          )
      )
      return

  # Batch agent workflows (`parallel`/`pipeline` + `agent`) must gate outputs via
  # `advanced.verify` unless explicitly marked `# no-verify` for read-only discovery
  if tool_name == "run_workflow":
    script = str(tool_args.get("Script", "") or "")
    script_path = os.path.expanduser(str(tool_args.get("ScriptPath", "") or "").strip())
    if not script and script_path and os.path.isfile(script_path):
      try:
        with open(script_path, "r", encoding="utf-8", errors="ignore") as f:
          script = f.read()
      except OSError:
        script = ""
    if script and not re.search(r"#\s*no-verify\b", script, re.IGNORECASE):
      try:
        tree = ast.parse(script)
        called = {
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, (ast.Name, ast.Attribute))
        }
        has_batch = bool(called & {"parallel", "pipeline"})
        has_agent = "agent" in called
        has_verify = "verify" in called
      except SyntaxError:
        code_only = re.sub(r"#[^\n]*", "", script)
        has_batch = bool(re.search(r"\b(?:parallel|pipeline)\s*\(", code_only))
        has_agent = bool(re.search(r"\bagent\s*\(", code_only))
        has_verify = bool(re.search(r"\bverify\s*\(", code_only))
      if has_batch and has_agent and not has_verify:
        print(
            json.dumps(
                {
                    "decision": "deny",
                    "reason": (
                        "[BATCH-VERIFY GUARD] Batch agent workflows (`parallel`/`pipeline` "
                        "with `agent(...)`) must gate worker or batch output with "
                        "`from advanced import verify` (`await verify(result, criteria)`), "
                        "or include `# no-verify` for pure read-only discovery fan-outs."
                    ),
                },
                ensure_ascii=False,
            )
        )
        return

  # Enforce intra-project parallel-chat isolation (worktree leases, shared-repo
  # per-file staging/commits, and unpushed commit ownership before git push)
  if tool_name in EDIT_TOOLS or tool_name == "run_command":
    pc_denial = parallel_chat_isolation_denial(
        tool_name,
        tool_args,
        conv_id,
        parent_conv_id,
        transcript_path,
        artifact_dir,
        workspace_paths,
        int(data.get("stepIdx", -1)),
    )
    if pc_denial:
      print(
          json.dumps(
              {"decision": "deny", "reason": pc_denial}, ensure_ascii=False
          )
      )
      return

  # Require subagent delegation when main-chat source-file reads exceed the turn cap
  if tool_name in READ_TOOLS:
    exp_denial = exploration_circuit_breaker_denial(
        tool_name,
        tool_args,
        parent_conv_id,
        transcript_path,
        int(data.get("stepIdx", -1)),
    )
    if exp_denial:
      print(
          json.dumps(
              {"decision": "deny", "reason": exp_denial}, ensure_ascii=False
          )
      )
      return

  print('{"decision": "allow"}')


# pre_tool_guard_notice.py shows this marker to the agent on its next model
# call: the stderr trace reaches only Jetski's log, where a dead entry point
# went unnoticed for 22 hours
FAILURE_MARKER_DEFAULT = "~/.gemini/config/logs/pre_tool_guard_failure.json"


def _record_failure(error: Exception) -> None:
  frame = traceback.extract_tb(error.__traceback__)[-1]
  marker = {
      "ts": time.time(),
      "conv": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
      "error": f"{type(error).__name__}: {error}"[:300],
      "where": f"{os.path.basename(frame.filename)}:{frame.lineno} in {frame.name}",
  }
  path = os.path.expanduser(
      os.environ.get("JETSKI_PRE_TOOL_GUARD_MARKER") or FAILURE_MARKER_DEFAULT)
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = f"{path}.{os.getpid()}.tmp"
  with open(tmp, "w") as f:
    json.dump(marker, f)
  os.replace(tmp, path)


def _guarded_main() -> None:
  # Gates every tool call of every chat: a bug in one guard must fail open
  # with a valid decision rather than print a traceback
  try:
    main()
  except Exception as error:
    print('{"decision": "allow"}')
    # Exit code 0 keeps the decision valid; without the stderr trace a broken
    # entry point is indistinguishable from guards that allowed the call
    traceback.print_exc()
    # The allow is already printed; an exception here would exit non-zero and
    # turn the fail-open into a failed tool call
    try:
      _record_failure(error)
    except Exception:
      traceback.print_exc()


if __name__ == "__main__":
  _guarded_main()
