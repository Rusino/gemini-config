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


CHAT_LIFECYCLE_HANDOFF_RE = re.compile(
    r"(?:^|(?<=[\s;&|(`]))(?:\S*/)?chat_lifecycle\.py\s+handoff\b"
)


def count_chat_lifecycle_handoff_launches(cmd: str) -> int:
  """Number of real `chat_lifecycle.py handoff` invocations in the command line."""
  cmd = strip_heredocs(cmd)
  try:
    tokens = shlex.split(cmd, posix=True)
  except ValueError:
    return len(CHAT_LIFECYCLE_HANDOFF_RE.findall(cmd))
  n = 0
  for tok, nxt in zip(tokens, tokens[1:]):
    if _clean_cmd_token(tok) == "chat_lifecycle.py" and nxt == "handoff":
      n += 1
  return n


def is_chat_lifecycle_handoff_launch(cmd: str) -> bool:
  """True only if the command really *invokes* `chat_lifecycle.py handoff`."""
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


def is_git_commit_or_upload_command(cmd: str) -> bool:
  """True if cmd invokes git commit, git cl upload, or gh pr create outside heredocs/grep."""
  cmd_code = strip_heredocs(cmd)
  if re.match(r"^\s*(?:grep|egrep|fgrep|rg|git\s+grep|echo|cat)\b", cmd_code):
    return False
  if re.search(r"\b(?:commit|upload|create)\s+(?:--help|-h|help)\b", cmd_code):
    return False
  return bool(GIT_COMMIT_OR_UPLOAD_RE.search(cmd_code))


# Detect external publishing commands (upload, push, PR creation) outside grep/help calls
def is_git_upload_or_push_command(cmd: str) -> bool:
  cmd_code = strip_heredocs(cmd)
  if re.match(r"^\s*(?:grep|egrep|fgrep|rg|git\s+grep|echo|cat)\b", cmd_code):
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
    file_path: str, pathspec: str
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
    diff_res = subprocess.run(
        ["git", "-C", repo_root, "diff", "-U0", base_ref, "--", pathspec],
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
        last_wp_full_path, "engine/src/flutter/lib/web_ui/lib/"
    )
    if git_has_code is not False:
      return (
          "WebParagraph (web_ui)",
          last_wp_file,
          "~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py",
      )

  return ("", "", "")


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
      pending_cmd = None
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


def _extract_cmd_target_dirs(cmd: str, workspace_paths: list[str]) -> list[str]:
  """Candidate directories targeted by a git commit / upload command."""
  cmd_code = strip_heredocs(cmd)
  dirs: list[str] = []
  for m in re.finditer(r"(?:^|[\s;&|(`])cd\s+([^\s;&|)`\"']+)", cmd_code):
    dirs.append(os.path.abspath(os.path.expanduser(m.group(1))))
  for m in re.finditer(r"\bgit\s+-C\s+([^\s;&|)`\"']+)", cmd_code):
    dirs.append(os.path.abspath(os.path.expanduser(m.group(1))))
  from context_guard import WORKSPACE_ROOT_RE

  for m in WORKSPACE_ROOT_RE.findall(cmd_code):
    dirs.append(os.path.abspath(os.path.expanduser(m)))
  if not dirs:
    for wp in workspace_paths:
      if wp:
        dirs.append(os.path.abspath(os.path.expanduser(wp)))
  return dirs


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
                "ask the user whether to forward the request via `agentapi send-message` or "
                "execute it here, and wait for the user's reply in the next turn."
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
      f'`agentapi send-message {active_chat[0]} "<user message verbatim>"`'
      if active_chat
      else '`agentapi send-message <target_conv_id> "<user message verbatim>"`'
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

      if is_chat_lifecycle_handoff_launch(cmd) and not re.search(
          r"\bhandoff\s+(?:--help|-h|help)\b", cmd
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

        has_notes_flag = "--notes" in strip_heredocs(cmd)
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
    if is_git_commit_or_upload_command(cmd_str) or is_git_upload_or_push_command(
        cmd_str
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
