#!/usr/bin/env python3
"""Stop hook for Jetski to prevent reporting completion after unverified code edits.

Checks the current turn in transcript.jsonl:
- If the agent modified a source code file (`write_to_file`, `replace_file_content`,
  `multi_replace_file_content`, `write_file`, or `edit_file`) and did NOT run any
  verification command (`run_command`) after the last code edit (or the last command
  after the edit failed with a non-zero exit code), blocks the stop once
  (`decision: "continue"`) and instructs the agent to verify the change first.
- If the current user request names a skill whose SKILL.md was never read in the
  conversation, or an artifact written this turn is a Case 1-5 review report
  without an `Execution Ledger` section or a testharness test without `assert_*`,
  blocks the stop once per user turn with a `[REVIEW DISCIPLINE GUARD]` reason.
"""

import json
import os
import re
import shlex
import socket
import subprocess
import sys

from pre_tool_guard import (
    EDIT_TOOLS,
    SOURCE_EXTENSIONS,
    SOURCE_FILENAMES,
    get_tool_target_file,
    is_tracked_source_file,
)

NOTIFY_SELF_BIN = "/google/bin/releases/g3n-team/notify_self"
_CURRENT_CONV_ID = ""

# Tool results may indent this line (e.g. "\n\n\t\t\t\tThe command exited ..." on
# macOS Jetski), so allow leading whitespace.
EXIT_CODE_RE = re.compile(r"^\s*The command exited with code (\d+)\.", re.MULTILINE)
# Recurring cron ticks emit an iteration marker in their wakeup system message
CRON_WAKEUP_RE = re.compile(r"\(iteration\s+\d+\)")

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


def send_stop_notification(conv_id: str) -> None:
  """Dispatches a detached G3N desktop notification for a completed turn."""
  if not os.path.isfile(NOTIFY_SELF_BIN):
    return
  title = ""
  if conv_id:
    try:
      from chat_lifecycle import get_conversation_title

      title = get_conversation_title(conv_id)
    except Exception:
      pass
  summary = f"Jetski: {title}" if title else "Jetski"
  short_host = socket.gethostname().split(".")[0]
  uri = (
      f"https://jetski.corp.google.com/box/{short_host}/c/{conv_id}"
      if conv_id and short_host
      else ""
  )
  cmd = [
      NOTIFY_SELF_BIN,
      f"--summary={summary}",
      "--message=Ответ в чате готов",
      "--severity=green",
  ]
  if uri:
    cmd.append(f"--uri={uri}")
  try:
    # notify_self cold-starts a SAR archive that can exceed the 5s Stop hook timeout
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
  except Exception:
    pass


def play_stop_sound(is_handoff: bool) -> None:
  """Controls browser completion chime and notifications via config.json (no CRD required)."""
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
      user_settings["enableNotificationsForSpecialEvents"] = False
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
          "u = c.setdefault('userSettings', {}); "
          "u['enableSoundsForSpecialEvents'] = True; "
          "u['enableNotificationsForSpecialEvents'] = True; "
          "json.dump(c, open(p, 'w'), indent=2)"
      ], start_new_session=True)
      # Give fsnotify time to propagate config change to browser before Idle
      time.sleep(0.3)
    else:
      if (
          user_settings.get("enableSoundsForSpecialEvents") is False
          or user_settings.get("enableNotificationsForSpecialEvents") is False
      ):
        user_settings["enableSoundsForSpecialEvents"] = True
        user_settings["enableNotificationsForSpecialEvents"] = True
        with open(config_path, "w", encoding="utf-8") as f:
          json.dump(config, f, indent=2)
        time.sleep(0.3)
      send_stop_notification(_CURRENT_CONV_ID)
  except Exception:
    pass


SKILL_GLOBS = (
    "~/.gemini/config/skills/*/SKILL.md",
    "~/.gemini/config/plugins/*/skills/*/SKILL.md",
)
READ_TOOLS = {"read_file", "view_file"}
USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*?)</USER_REQUEST>", re.DOTALL)
CASE_CLASSIFICATION_RE = re.compile(r"(?:\*\*Case\s*[1-5]\b|\bCase\s*[1-5]\s*:)")
# Patterns detecting review approval verdicts and user or environment corpus exemptions
REVIEW_APPROVAL_RE = re.compile(
    r"(?<!not\s)(?<!no\s)(?<!do\snot\s)\b(?:Case\s*[123]\b|LGTM\b|Code-Review\+1|safe\s+to\s+approve\b)",
    re.IGNORECASE,
)
UNVERIFIED_FLUTTER_RE = re.compile(
    r"\bUNVERIFIED[_\s]+IN[_\s]+FLUTTER\b", re.IGNORECASE
)
USER_CORPUS_OPTOUT_RE = re.compile(
    r"(?:без\s+(?:прогона\s+)?(?:корпуса|флаттера|flutter)|"
    r"только\s+текстом|без\s+компаньона|быстрый\s+взгляд|"
    r"without\s+(?:flutter\s+)?corpus|skip\s+(?:flutter\s+)?corpus|prose[\s-]+only)",
    re.IGNORECASE,
)
LEDGER_HEADING_RE = re.compile(r"^#+\s.*Execution Ledger", re.MULTILINE | re.IGNORECASE)
WPT_ASSERT_RE = re.compile(r"\bassert_\w+\s*\(")
# Match triage_cl.py corpus gate markers and fallback file paths in triage output
SKIA_CORPUS_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(])modules/(?:skparagraph|skunicode|skshaper)/(?:src|include)/[^\s`'\"#)]+"
    r"\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|mm)\b"
)
WEBPARAGRAPH_CORPUS_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(])(?:engine/src/flutter/)?lib/web_ui/lib/[^\s`'\"#)]+\.dart\b"
)
TRIAGE_CORPUS_MARKER_RE = re.compile(
    r"REQUIRED\s*\(`(run_flutter_paragraph_tests\.py|run_webparagraph_corpus\.py)`\)"
    r"|"
    r'"required":\s*true[\s\S]{0,160}?"script":\s*"(run_flutter_paragraph_tests\.py|run_webparagraph_corpus\.py)"'
)
# Match Skia C++ paths across modules, core, ports, tests, and bench
SKIA_CPP_PATH_RE = re.compile(
    r"(?:^|[\s`'\"(/])(?:modules/[^/\s`'\"]+/(?:src|include)|src|include|tests?|bench|gm)/[^\s`'\"#)]+\.(?:c|cc|cpp|cxx|h|hh|hpp|hxx|mm)\b"
)
SKIA_GERRIT_RE = re.compile(
    r"https?://skia-review\.googlesource\.com/(?:c/skia/\+/|changes/)?(\d{6,8})"
)
AGENT_ACTION_TRY_RE = re.compile(
    r"AGENT ACTION \(execute this command[^\)]*\):\s*\n\s*(git cl try[^\n]+)"
)
GIT_CL_TRY_BOT_RE = re.compile(r"-b\s+([A-Za-z0-9_.-]+)")
USER_TRYJOB_OPTOUT_RE = re.compile(
    r"(?:без\s+(?:asan|асан|try|трай|канаре|canar)|skip\s+(?:asan|try|canar)|no\s+(?:asan|try|canar))",
    re.IGNORECASE,
)


# Verify that script_name is executed rather than merely inspected via grep/cat
def _invokes_script_in_cmd(cmd: str, script_name: str) -> bool:
  if script_name not in cmd:
    return False
  for seg in re.split(r"&&|\|\||;|\||\n", cmd):
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


# Detect roadmap.py set ... done invocations outside heredocs and read-only grep/echo
def is_roadmap_set_done_command(cmd: str) -> bool:
  from pre_tool_guard import _clean_cmd_token, strip_heredocs

  cmd_code = strip_heredocs(cmd)
  for seg in re.split(r"&&|\|\||;|\n", cmd_code):
    seg = seg.strip()
    if not seg or "roadmap" not in seg:
      continue
    if re.match(r"^(?:grep|egrep|fgrep|rg|echo|cat)\b", seg):
      continue
    try:
      tokens = shlex.split(seg, posix=True)
    except ValueError:
      tokens = seg.split()
    for i, tok in enumerate(tokens):
      if _clean_cmd_token(tok) == "roadmap.py":
        rest = tokens[i + 1 :]
        if len(rest) >= 4 and rest[0] == "set" and rest[3] == "done":
          return True
        if len(rest) == 3 and rest[0] == "set" and rest[2] == "done":
          return True
  return False


def discover_skills() -> dict:
  """Maps skill directory names to their SKILL.md paths.

  Only hyphenated or underscored names qualify, so plain words in a request
  (e.g. "browser") never count as naming a skill.
  """
  import glob

  skills = {}
  for pattern in SKILL_GLOBS:
    for path in glob.glob(os.path.expanduser(pattern)):
      name = os.path.basename(os.path.dirname(path))
      if "-" in name or "_" in name:
        skills[name] = path
  return skills


def _unquote_path(value) -> str:
  return str(value or "").strip().strip('"').strip("'")


# Audit transcript for unread named skills, missing Execution Ledgers, and unverified corpus gates
def analyze_review_discipline(transcript_path: str, artifact_dir: str) -> dict:
  """Collects skill-reading, review-report, and corpus-gate violations for the current turn.

  A skill counts as named when its directory name appears in the current
  turn's user request; it counts as read when any step of the conversation
  read its SKILL.md. Artifacts written in this turn and the final planner
  response are checked for Execution Ledger and mandatory Flutter corpus runs.
  """
  # Include corpus_issues alongside skill and artifact checks
  result = {
      "last_user_idx": -1,
      "unread_skills": [],
      "artifact_issues": [],
      "corpus_issues": [],
      "asan_canary_issues": [],
  }
  if not transcript_path or not os.path.exists(transcript_path):
    return result
  steps = []
  try:
    with open(transcript_path, "r", encoding="utf-8") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          obj = json.loads(line)
        except Exception:
          continue
        if obj.get("step_index") is not None:
          steps.append(obj)
  except Exception:
    return result
  steps.sort(key=lambda s: int(s["step_index"]))

  request_text = ""
  # Collect all user prompts in the conversation to honor earlier corpus opt-outs
  all_user_requests = []
  for step in steps:
    if step.get("type") == "USER_INPUT":
      result["last_user_idx"] = int(step["step_index"])
      content = str(step.get("content", ""))
      match = USER_REQUEST_RE.search(content)
      request_text = match.group(1) if match else content
      all_user_requests.append(request_text)

  read_blobs = []
  written_artifacts = []
  # Track triage corpus requirements and passing corpus test executions across steps
  last_planner_text = ""
  required_corpus_script = ""
  required_corpus_domain = ""
  corpus_passed = {
      "run_flutter_paragraph_tests.py": False,
      "run_webparagraph_corpus.py": False,
  }
  pending_triage = False
  pending_corpus_script = ""
  triggered_try_bots = set()
  required_agent_bots = set()
  required_agent_try_cmd = ""
  canary_advisor_invoked = False
  skia_review_cl = ""
  skia_cpp_detected = False
  pending_canary_advisor = False

  # Walk planner tool calls and command outputs in chronological order
  for step in steps:
    stype = step.get("type")
    if stype == "PLANNER_RESPONSE":
      in_turn = int(step["step_index"]) > result["last_user_idx"]
      if in_turn and str(step.get("content", "")).strip():
        last_planner_text = str(step.get("content", ""))
      pending_triage = False
      pending_corpus_script = ""
      for tc in step.get("tool_calls") or []:
        tname = tc.get("name", "")
        targs = tc.get("args") or {}
        if tname in READ_TOOLS:
          read_blobs.append(_unquote_path(get_tool_target_file(targs)))
        elif tname == "invoke_subagent":
          read_blobs.append(str(targs.get("Prompt", "")))
        elif tname == "run_command":
          cmd = str(targs.get("CommandLine", ""))
          read_blobs.append(cmd)
          if _invokes_script_in_cmd(cmd, "triage_cl.py"):
            pending_triage = True
          if _invokes_script_in_cmd(cmd, "canary_advisor.py"):
            canary_advisor_invoked = True
            pending_canary_advisor = True
          if re.search(r"\bgit\s+cl\s+try\b", cmd):
            for m in GIT_CL_TRY_BOT_RE.finditer(cmd):
              triggered_try_bots.add(m.group(1))
          for c_script in (
              "run_flutter_paragraph_tests.py",
              "run_webparagraph_corpus.py",
          ):
            if _invokes_script_in_cmd(cmd, c_script):
              pending_corpus_script = c_script
        elif tname in EDIT_TOOLS and in_turn and artifact_dir:
          target = os.path.abspath(_unquote_path(get_tool_target_file(targs)))
          if target.startswith(os.path.abspath(artifact_dir) + "/"):
            written_artifacts.append(target)
    elif stype == "GENERIC":
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m_action = AGENT_ACTION_TRY_RE.search(content)
      if m_action:
        cmd_str = m_action.group(1).strip()
        required_agent_try_cmd = cmd_str
        for m in GIT_CL_TRY_BOT_RE.finditer(cmd_str):
          required_agent_bots.add(m.group(1))
      if pending_canary_advisor:
        m_g = SKIA_GERRIT_RE.search(content)
        if m_g:
          skia_review_cl = m_g.group(1)
        pending_canary_advisor = False
      if pending_triage:
        m_gerrit = SKIA_GERRIT_RE.search(content)
        if m_gerrit:
          skia_review_cl = m_gerrit.group(1)
        if SKIA_CPP_PATH_RE.search(content):
          skia_cpp_detected = True
        m_marker = TRIAGE_CORPUS_MARKER_RE.search(content)
        if m_marker:
          script = m_marker.group(1) or m_marker.group(2)
          required_corpus_script = script
          required_corpus_domain = (
              "SkParagraph/SkUnicode/SkShaper"
              if script == "run_flutter_paragraph_tests.py"
              else "WebParagraph (web_ui)"
          )
          corpus_passed[script] = False
        elif (
            SKIA_CORPUS_PATH_RE.search(content)
            and "LGTM for rollback." not in content
        ):
          required_corpus_script = "run_flutter_paragraph_tests.py"
          required_corpus_domain = "SkParagraph/SkUnicode/SkShaper"
          corpus_passed["run_flutter_paragraph_tests.py"] = False
        elif (
            WEBPARAGRAPH_CORPUS_PATH_RE.search(content)
            and "LGTM for rollback." not in content
        ):
          required_corpus_script = "run_webparagraph_corpus.py"
          required_corpus_domain = "WebParagraph (web_ui)"
          corpus_passed["run_webparagraph_corpus.py"] = False
        pending_triage = False
      if pending_corpus_script:
        m_exit = EXIT_CODE_RE.search(content)
        if (
            m_exit
            and int(m_exit.group(1)) == 0
            and step.get("status") == "DONE"
        ):
          corpus_passed[pending_corpus_script] = True
        pending_corpus_script = ""

  for name in sorted(discover_skills()):
    if not re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", request_text):
      continue
    suffix = f"{name}/SKILL.md"
    if not any(suffix in blob for blob in read_blobs):
      result["unread_skills"].append(name)

  # Retain artifact bodies so inline and artifact review checks can inspect both
  artifact_texts = []
  for path in dict.fromkeys(written_artifacts):
    try:
      with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    except Exception:
      continue
    artifact_texts.append(text)
    base = os.path.basename(path)
    # A 5-case review verdict without a step ledger hides skipped steps
    if path.endswith(".md") and CASE_CLASSIFICATION_RE.search(text):
      if not LEDGER_HEADING_RE.search(text):
        result["artifact_issues"].append(
            f"`{base}` classifies a review (Case 1-5) but has no `Execution Ledger` "
            "section listing every skill step as done / skipped (with reason)"
        )
    # A testharness test without assertions passes on any behavior
    if path.endswith(".html") and "testharness.js" in text:
      if not WPT_ASSERT_RE.search(text):
        result["artifact_issues"].append(
            f"`{base}` is a testharness test with no `assert_*` call, so it cannot fail"
        )

  # Also require Execution Ledger when a Case 1-5 review verdict is delivered inline in chat
  if (
      last_planner_text
      and CASE_CLASSIFICATION_RE.search(last_planner_text)
      and not LEDGER_HEADING_RE.search(last_planner_text)
      and not any(LEDGER_HEADING_RE.search(t) for t in artifact_texts)
  ):
    result["artifact_issues"].append(
        "Your chat response classifies a review (Case 1-5) but has no `### Execution Ledger` "
        "section listing every skill step as done / skipped (with reason)"
    )

  # Enforce mandatory Flutter corpus verification before approving a reviewed CL
  combined_review_out = "\n".join([last_planner_text] + artifact_texts)
  user_opted_out = any(USER_CORPUS_OPTOUT_RE.search(u) for u in all_user_requests)
  if (
      required_corpus_script
      and not corpus_passed.get(required_corpus_script)
      and not user_opted_out
      and REVIEW_APPROVAL_RE.search(combined_review_out)
      and not UNVERIFIED_FLUTTER_RE.search(combined_review_out)
  ):
    script_path = (
        "~/.gemini/config/skills/flutter-test-paragraph-engine/"
        "scripts/run_flutter_paragraph_tests.py"
        if required_corpus_script == "run_flutter_paragraph_tests.py"
        else "~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py"
    )
    result["corpus_issues"].append(
        f"[REVIEW CORPUS GUARD] The reviewed CL modifies `{required_corpus_domain}` "
        "production code, and your response recommends approval (`Case 1-3` / `LGTM`), "
        f"but `{required_corpus_script}` has not passed (`exit code 0`) on the author's "
        "patchset in this conversation. Check out the author's patchset and run "
        f"`python3 {script_path}` before approving (or explicitly mark the review as "
        "`UNVERIFIED IN FLUTTER` if the environment prevents running it)"
    )

  # Enforce mandatory ASAN try job and external-author canary try jobs
  user_opted_out_try = any(USER_TRYJOB_OPTOUT_RE.search(u) for u in all_user_requests)
  if not user_opted_out_try:
    missing_try_bots = required_agent_bots - triggered_try_bots
    if missing_try_bots:
      result["asan_canary_issues"].append(
          "[ASAN/CANARY GUARD] `skia-canary-advisor` specified required try job(s) for the agent to run, "
          f"but the following bot(s) were not triggered via `git cl try`: {', '.join(sorted(missing_try_bots))}. "
          f"Execute this command now:\n  {required_agent_try_cmd}"
      )
    elif (
        skia_review_cl
        and skia_cpp_detected
        and not canary_advisor_invoked
        and (
            CASE_CLASSIFICATION_RE.search(combined_review_out)
            or REVIEW_APPROVAL_RE.search(combined_review_out)
        )
    ):
      result["asan_canary_issues"].append(
          f"[ASAN/CANARY GUARD] You are reviewing Skia CL {skia_review_cl} (modifying C++ code), "
          "but `skia-canary-advisor` was never executed in this conversation. "
          f"Run `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py advise --cl {skia_review_cl}` "
          "to select and auto-trigger the required ASAN try job (and canary for external authors) before completing the review"
      )

  return result


def review_discipline_reason(info: dict) -> str:
  parts = []
  for name in info.get("unread_skills", []):
    parts.append(
        f"- The request names skill `{name}`, but its SKILL.md was never read in this "
        "conversation. Read it now and redo any step it requires that you skipped."
    )
  for issue in info.get("artifact_issues", []):
    parts.append(f"- {issue}.")
  # Append any unverified review corpus violations to the stop block reason
  for issue in info.get("corpus_issues", []):
    parts.append(f"- {issue}.")
  for issue in info.get("asan_canary_issues", []):
    parts.append(f"- {issue}.")
  if not parts:
    return ""
  return (
      "[REVIEW DISCIPLINE GUARD] Do not finish yet:\n"
      + "\n".join(parts)
      + "\nAny step you cannot perform must be reported to the user as skipped or "
      "UNVERIFIED, never as done."
  )


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

  from context_guard import (
      is_chat_lifecycle_handoff_command,
      is_handoff_launch_command,
  )

  last_code_edit_idx = -1
  last_edited_file = ""
  last_cmd_idx = -1
  last_cmd_exit_code = None
  pending_cmds: list[tuple[int, bool, bool, bool, bool, bool]] = []
  launched_new_conv = False
  scheduled_timer = False
  asked_question = False
  roadmap_done_idx = -1
  vcs_baton_lines: list[str] = []
  git_publish_after_done = False
  set_title_succeeded = False

  for idx in ordered_indices:
    if idx <= last_user_idx:
      continue
    step = steps_by_idx[idx]
    stype = step.get("type", "")
    tool_calls = step.get("tool_calls") or []

    if stype == "PLANNER_RESPONSE" and tool_calls:
      pending_cmds.clear()
      for tc in tool_calls:
        tname = tc.get("name", "")
        targs = tc.get("args") or {}
        if tname == "ask_question":
          asked_question = True
        elif tname == "schedule" and bool(targs.get("CronExpression")):
          scheduled_timer = True
        elif (
            tname == "manage_task"
            and str(targs.get("Action", "")).lower() == "kill"
        ):
          scheduled_timer = False
        elif tname in EDIT_TOOLS:
          target = get_tool_target_file(targs)
          if is_tracked_source_file(target):
            last_code_edit_idx = idx
            last_edited_file = target.strip().strip('"').strip("'")
        elif tname == "run_command":
          cmd = str(targs.get("CommandLine", ""))
          is_handoff = is_handoff_launch_command(cmd)
          is_cli = is_chat_lifecycle_handoff_command(cmd) if is_handoff else False
          is_rm_done = is_roadmap_set_done_command(cmd)
          is_set_title = bool(re.search(r"\bchat_lifecycle\.py\s+set-title\b", cmd))
          is_git_pub = bool(
              re.search(
                  r"\b(?:git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+(?:push|commit|cl\s+upload)|gh\s+pr\s+create)\b",
                  cmd,
              )
          )
          if (
              not is_handoff
              and not is_rm_done
              and not is_set_title
              and is_inspection_command(cmd)
          ):
            continue
          last_cmd_idx = idx
          pending_cmds.append((idx, is_handoff, is_cli, is_rm_done, is_git_pub, is_set_title))

    elif stype == "GENERIC" and pending_cmds:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content)
      if m:
        c_idx, is_handoff, is_cli, is_rm_done, is_git_pub, is_set_title = pending_cmds.pop(0)
        code = int(m.group(1))
        if last_cmd_exit_code is None or code != 0 or not pending_cmds:
          last_cmd_exit_code = code
        launched_marker = (
            '"newConversation"' in content
            or '"new_conversation_id"' in content
            or is_cli
        )
        if (
            is_handoff
            and code == 0
            and step.get("status") == "DONE"
            and launched_marker
        ):
          launched_new_conv = True
        if is_rm_done and code == 0:
          roadmap_done_idx = c_idx
          for ln in content.splitlines():
            if "[VCS BATON:" in ln:
              vcs_baton_lines.append(ln.strip())
        if is_git_pub and code == 0 and roadmap_done_idx >= 0 and c_idx >= roadmap_done_idx:
          git_publish_after_done = True
        if is_set_title and code == 0:
          set_title_succeeded = True

    # Keep cron chats marked as scheduled across recurring wakeup ticks
    elif stype == "SYSTEM_MESSAGE":
      if CRON_WAKEUP_RE.search(str(step.get("content", ""))):
        scheduled_timer = True

  return {
      "last_user_idx": last_user_idx,
      "last_code_edit_idx": last_code_edit_idx,
      "last_edited_file": last_edited_file,
      "last_cmd_idx": last_cmd_idx,
      "last_cmd_exit_code": last_cmd_exit_code,
      "launched_new_conv": launched_new_conv,
      "scheduled_timer": scheduled_timer,
      "asked_question": asked_question,
      "roadmap_done_idx": roadmap_done_idx,
      "vcs_baton_lines": vcs_baton_lines,
      "git_publish_after_done": git_publish_after_done,
      "set_title_succeeded": set_title_succeeded,
  }


def vcs_baton_guard_reason(
    info: dict, conv_id: str, transcript_path: str
) -> str:
  if info.get("roadmap_done_idx", -1) < 0:
    return ""
  if info.get("asked_question"):
    return ""

  vcs_lines = list(info.get("vcs_baton_lines") or [])
  if info.get("git_publish_after_done") or not vcs_lines:
    try:
      import roadmap

      refreshed = roadmap.evaluate_vcs_baton(
          conv_id, transcript_path=transcript_path
      )
      if refreshed:
        vcs_lines = [
            ln.strip()
            for ln in refreshed.splitlines()
            if ln.strip().startswith("[VCS BATON:")
        ]
    except Exception:
      pass

  pending_lines = [ln for ln in vcs_lines if "[VCS BATON: PENDING" in ln]
  if not pending_lines:
    return ""

  baton_details = "\n".join(pending_lines)
  return (
      "[VCS BATON GUARD] You marked a roadmap item `done` in this turn while VCS baton is PENDING, "
      "but did not call `ask_question`!\n"
      f"{baton_details}\n"
      "Before ending your turn, call `ask_question` with options: "
      "`(Recommended) Commit + push`, `Commit only`, `Not now` "
      "(remember: stage/commit ONLY your own touched files with explicit pathspecs; for Gerrit repos, "
      "`Commit + push` runs `amnesia-precommit` + mandatory corpus gate + `git cl upload`, never CQ)."
  )


def title_guard_reason(info: dict, state: dict, conv_id: str) -> str:
  if info.get("set_title_succeeded"):
    return ""
  if not state.get("pending_title_guard"):
    return ""
  try:
    from chat_lifecycle import clean_base_title, get_conversation_title, is_valid_chat_topic

    cur_title = get_conversation_title(conv_id)
    if is_valid_chat_topic(clean_base_title(cur_title)):
      return ""
  except Exception:
    return ""
  return (
      f"[TITLE GUARD] This conversation still has a missing, single-word, or overly generic "
      f"title ({cur_title or 'untitled'!r}). Before finishing your turn, run "
      f"`python3 ~/.gemini/config/hooks/chat_lifecycle.py set-title {conv_id} "
      '"<Concrete 2-6 word topic>"` (< 50 chars, naming the concrete '
      "subsystem/feature/bug/PR/CL and action; never single-word or generic like "
      "'Investigation' or 'Debugging')."
  )


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
  # Jetski's Go runtime passes NO_TOOL_CALL on normal turn completion
  if term_reason not in ("", "MODEL_STOP", "NO_TOOL_CALL"):
    print("{}")
    return

  global _CURRENT_CONV_ID
  conv_id = data.get("conversationId", "")
  _CURRENT_CONV_ID = conv_id
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")

  state_path = (
      os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      if artifact_dir
      else f"/tmp/jetski_context_guard_{conv_id}.json"
  )
  state = load_state(state_path)

  info = analyze_current_turn(transcript_path)
  if not data.get("fullyIdle", True):
    # Promote a chat with an active schedule to ⏳ when not fullyIdle
    if conv_id and info.get("scheduled_timer"):
      try:
        from chat_lifecycle import set_conversation_wait_state

        set_conversation_wait_state(conv_id, waiting=True)
        if not state.get("auto_waiting_schedule"):
          state["auto_waiting_schedule"] = True
          save_state(state_path, state)
      except Exception:
        pass
    print("{}")
    return

  # Once all scheduled tasks finish or are killed and the chat reaches fullyIdle,
  # restore an auto-promoted chat from ⏳ back to ⦿ (Needs Me)
  if conv_id and state.get("auto_waiting_schedule"):
    try:
      from chat_lifecycle import set_conversation_wait_state

      set_conversation_wait_state(conv_id, waiting=False)
      state.pop("auto_waiting_schedule", None)
      save_state(state_path, state)
    except Exception:
      pass

  if (
      info.get("launched_new_conv")
      or state.get("handoff_completed")
      or state.get("handed_off_to")
  ):
    if state.get("pending_handoff_launch") or not state.get(
        "handoff_completed"
    ):
      state["pending_handoff_launch"] = False
      state["handoff_completed"] = True
      save_state(state_path, state)
    play_stop_sound(is_handoff=True)
    print("{}")
    return

  # A mid-turn handoff trigger applies only while continuing autonomous work;
  # once the agent answers the user and stops to wait for their reply, keep
  # this chat open (⦿) so the answer stays visible and replyable here
  if state.get("pending_handoff_launch") and not state.get("pending_turn_start"):
    state["pending_handoff_launch"] = False
    save_state(state_path, state)

  # If a turn-start handoff was required and the agent tries to stop without
  # having launched the continuation conversation, block once and force the launch!
  if (
      state.get("pending_handoff_launch")
      and state.get("pending_turn_start")
      and not state.get("stop_blocked_handoff")
  ):
    state["stop_blocked_handoff"] = True
    save_state(state_path, state)
    new_title = state.get("pending_title") or "[handoff]"
    short_id = conv_id[:8] if conv_id else "unknown"
    default_handoff = (
        os.path.join(artifact_dir, f"handoff_summary_{short_id}.md")
        if artifact_dir
        else f"/tmp/handoff_summary_{short_id}.md"
    )
    handoff_file = state.get("pending_handoff_file") or default_handoff
    if state.get("pending_in_project"):
      steps = (
          f"1. Update roadmap statuses: `python3 ~/.gemini/config/hooks/roadmap.py set {conv_id} <ID> done|doing|todo`.\n"
          f"2. Overwrite the chain state file `{handoff_file}` (goal, current state with evidence, failed hypotheses, "
          f"running tasks and how to restart them, exact next step).\n"
          f'3. Run `python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff {conv_id} --next "<1-line summary of next immediate action>"`.\n'
          "4. End your turn with a 1-line handoff summary and the link"
          f" `[👉 {new_title}](conversation://<new_conversation_id>)` (never dump the roadmap)."
      )
    else:
      steps = (
          f"1. Make sure the summary `{handoff_file}` is written and current.\n"
          f'2. Run `python3 ~/.gemini/config/hooks/chat_lifecycle.py handoff {conv_id} "{handoff_file}" --next "<1-line summary of next immediate action>"`.\n'
          f"3. Give the user the link `[👉 {new_title}](conversation://<new_conversation_id>)`."
      )
    reason = (
        "[HANDOFF GUARD] A context handoff is pending, but you attempted to finish your turn "
        f"WITHOUT launching the continuation conversation. Do it now:\n{steps}"
    )
    print(
        json.dumps(
            {"decision": "continue", "reason": reason}, ensure_ascii=False
        )
    )
    return

  discipline = analyze_review_discipline(transcript_path, artifact_dir)
  discipline_reason = review_discipline_reason(discipline)
  # Blocking once per user turn keeps a stubborn violation from looping forever
  if (
      discipline_reason
      and state.get("discipline_blocked_turn") != discipline["last_user_idx"]
  ):
    state["discipline_blocked_turn"] = discipline["last_user_idx"]
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": discipline_reason},
            ensure_ascii=False,
        )
    )
    return

  last_edit_idx = info.get("last_code_edit_idx", -1)
  if last_edit_idx >= 0 and state.get("last_blocked_edit_idx") != last_edit_idx:
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

  vcs_reason = vcs_baton_guard_reason(info, conv_id, transcript_path)
  if (
      vcs_reason
      and state.get("vcs_baton_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["vcs_baton_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": vcs_reason},
            ensure_ascii=False,
        )
    )
    return

  if state.get("pending_title_guard") and info.get("set_title_succeeded"):
    state.pop("pending_title_guard", None)
    save_state(state_path, state)

  t_reason = title_guard_reason(info, state, conv_id)
  if (
      t_reason
      and state.get("title_guard_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["title_guard_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": t_reason},
            ensure_ascii=False,
        )
    )
    return

  play_stop_sound(is_handoff=False)
  print("{}")


if __name__ == "__main__":
  main()
