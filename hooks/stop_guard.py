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

from __future__ import annotations

import json
import os
import re
import shlex
import socket
import stat
import subprocess
import sys
import tempfile

from pre_tool_guard import (
    EDIT_TOOLS,
    SOURCE_EXTENSIONS,
    SOURCE_FILENAMES,
    get_tool_target_file,
    is_tracked_source_file,
    strip_heredocs,
)

NOTIFY_SELF_BIN = "/google/bin/releases/g3n-team/notify_self"
_CURRENT_CONV_ID = ""

# Match command exit codes across Antigravity, Linux Jetski Web, and background tasks
EXIT_CODE_RE = re.compile(
    r"^\s*(?:The\s+)?[Cc]ommand exited with code (\d+)\.", re.MULTILINE
)
# Recurring cron ticks emit an iteration marker in their wakeup system message
CRON_WAKEUP_RE = re.compile(r"\(iteration\s+\d+\)")
# Background task launch marker with its unique task identifier
BG_TASK_LAUNCH_RE = re.compile(
    r"Tool is running as a background task with task id:\s*(\S+)"
)
BG_TASK_DONE_RE = re.compile(r'Task id\s+"([^"]+)"\s+finished with result:')
SUBAGENT_CREATED_RE = re.compile(r'"conversationId":\s*"([^"]+)"')
SYS_MSG_SENDER_RE = re.compile(r"\bsender=([^\s]+)")
VALID_INTEGRITY_RE = re.compile(
    r"(?:Integrity Check|Вердикт целостности)[^`\n]*`VALID\b", re.IGNORECASE
)
INVALID_INTEGRITY_RE = re.compile(
    r"(?:Integrity Check|Вердикт целостности)[^`\n]*`(?:WARNING|INVALID)\b",
    re.IGNORECASE,
)
SUBAGENT_EXIT_ZERO_RE = re.compile(
    r"(?:exit\s+(?:code|status)\s*[:=`]*\s*`?0\b|exited\s+with\s+code\s+0\b|\bexit=0\b)",
    re.IGNORECASE,
)
COMPARE_REPORT_PATH_RE = re.compile(r"(/[^\s`'\")]*compare\.md)\b")


def _corpus_scripts_in_text(text: str) -> set[str]:
  res = set()
  if "run_flutter_paragraph_tests.py" in text or "flutter-test-paragraph-engine" in text:
    res.add("run_flutter_paragraph_tests.py")
  if "run_webparagraph_corpus.py" in text or "webparagraph-corpus" in text:
    res.add("run_webparagraph_corpus.py")
  return res


def _corpus_scripts_from_compare_report(content: str, path_hint: str = "") -> set[str]:
  # Only valid integrity verdicts without warnings count toward corpus gates
  if not VALID_INTEGRITY_RE.search(content) or INVALID_INTEGRITY_RE.search(content):
    return set()
  matched = set()
  combined = f"{path_hint}\n{content}"
  # Match Flutter engine SkParagraph test suite report markers
  if (
      "Flutter Paragraph Engine Test Comparison" in content
      or "все пакеты тестов завершены штатно" in content
      or "flutter_tester" in content
      or ("run_flutter_paragraph_tests.py" in content and "webparagraph" not in content.lower())
      or ("flutter_paragraph" in path_hint.lower() and "webparagraph" not in combined.lower())
  ):
    matched.add("run_flutter_paragraph_tests.py")
  # Match WebParagraph test suite report markers
  if (
      "WebParagraph Corpus Report" in content
      or "WebParagraph активен" in content
      or "run_webparagraph_corpus.py" in content
      or "webparagraph" in combined.lower()
  ):
    matched.add("run_webparagraph_corpus.py")
  return matched


def _check_compare_md_paths(text: str) -> set[str]:
  matched = set()
  for m in COMPARE_REPORT_PATH_RE.finditer(text):
    p = m.group(1).strip()
    if os.path.isfile(p):
      try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
          content = f.read()
        matched.update(_corpus_scripts_from_compare_report(content, path_hint=p))
      except Exception:
        pass
  return matched

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


def load_state(state_path: str, conv_id: str = "") -> dict:
  try:
    from context_guard import load_state as cg_load_state

    return cg_load_state(state_path, conv_id)
  except Exception:
    pass
  try:
    if os.path.exists(state_path):
      with open(state_path, "r", encoding="utf-8") as f:
        return json.load(f)
  except Exception:
    pass
  return {}


def save_state(state_path: str, state: dict, conv_id: str = "") -> None:
  try:
    from context_guard import save_state as cg_save_state

    cg_save_state(state_path, state, conv_id)
    return
  except Exception:
    pass
  try:
    os.makedirs(os.path.dirname(state_path), exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
      json.dump(state, f, indent=2)
  except Exception:
    pass


def spawn_background_dossier_update(conv_id: str, transcript_path: str) -> None:
  """Spawns detached background process to pre-warm the Russian AI TL;DR dossier."""
  if not conv_id:
    return
  roadmap_script = os.path.expanduser("~/.gemini/config/hooks/roadmap.py")
  if not os.path.isfile(roadmap_script):
    return
  cmd = [sys.executable, roadmap_script, "update-dossier", conv_id]
  if transcript_path and os.path.isfile(transcript_path):
    cmd.extend(["--transcript", transcript_path])
  try:
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
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
      # G3 desktop notifications are opt-in to avoid intrusive popups
      if user_settings.get("enableG3Notifications", False):
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
# Match `canary_advisor.py try` advisor lines plus legacy `git cl try` and
# `luci-auth context -- git cl try` ones
AGENT_ACTION_TRY_RE = re.compile(
    r"AGENT ACTION \(execute this command[^\)]*\):\s*\n\s*"
    r"((?:(?:\S*/)?luci-auth\s+context(?:\s+[^\n]*?)?(?:\s+--)?\s+)?git\s+cl\s+try[^\n]+"
    r"|python3\s+\S*canary_advisor\.py\s+try\b[^\n]+)"
)
GIT_CL_TRY_BOT_RE = re.compile(r"-b\s+([A-Za-z0-9_.-]+)")
# Reject `git cl try` failures even when shell exit status was masked with `|| true`
GIT_CL_TRY_ERROR_RE = re.compile(
    r"(?:SSOAuthenticator:|^\s*ERROR:|Can't trigger tryjobs:|^TRIGGER: FAILED)",
    re.MULTILINE,
)
# Only `canary_advisor.py try` prints this, after Buildbucket lists every requested build.
# Exit 0 alone is no proof: on Gerrit 1407718 PS18 a `git cl try` failure hid behind a pipe
TRIGGER_VERIFIED_RE = re.compile(r"^TRIGGER: VERIFIED\b", re.MULTILINE)
USER_TRYJOB_OPTOUT_RE = re.compile(
    r"(?:без\s+(?:asan|асан|try|трай|канаре|canar)|skip\s+(?:asan|try|canar)|no\s+(?:asan|try|canar))",
    re.IGNORECASE,
)
# canary_advisor.py output that makes the local google3 A/B corpus a gate for approval
G3_REQUIRED_RE = re.compile(
    r"## LOCAL google3-test-paragraph \[STRONG\]|AGENT ACTION \(LOCAL gate google3-test-paragraph"
)
G3_ACTION_CMD_RE = re.compile(
    r"AGENT ACTION \(LOCAL gate google3-test-paragraph[^\n]*\n\s*(python3 [^\n]+)"
)
G3_REPORT_TITLE = "# Google3 Paragraph Corpus Comparison"
G3_VALID_RE = re.compile(r"Integrity Check\):\*\*\s*`VALID")
G3_REGRESSIONS_RE = re.compile(r"Новых падений \(Regressions\):\s*(\d+)")
UNVERIFIED_G3_RE = re.compile(r"\bUNVERIFIED[_\s]+IN[_\s]+GOOGLE3\b", re.IGNORECASE)
USER_G3_OPTOUT_RE = re.compile(
    r"(?:без\s+(?:прогона\s+)?(?:гугл\s*3|google3|g3)|skip\s+(?:the\s+)?(?:google3|g3))",
    re.IGNORECASE,
)
# Tryjob verdicts from `canary_advisor.py triage/watch` and `git cl try-results`
TRYJOBS_VERDICT_RE = re.compile(r"^TRYJOBS: (ALL GREEN|NOT GREEN[^\n]*)", re.MULTILINE)
CANARY_DRAFT_STATUS_RE = re.compile(r"^CANARY DRAFT: (OK|MISSING)\b", re.MULTILINE)
TRY_RESULTS_TOTAL_RE = re.compile(r"^Total: (\d+) tryjobs?", re.MULTILINE)
TRY_RESULTS_NOT_GREEN_RE = re.compile(
    r"^(?:Failures|Infra Failures|Started|Scheduled|Canceled):\s*$", re.MULTILINE
)
UNVERIFIED_TRYBOTS_RE = re.compile(r"\bUNVERIFIED[_\s]+ON[_\s]+TRYBOTS\b", re.IGNORECASE)


# Returns True / False for a green / not green tryjob report, None when text has no report
def _tryjob_report_green(text: str):
  verdicts = TRYJOBS_VERDICT_RE.findall(text)
  if verdicts:
    if verdicts[-1] != "ALL GREEN":
      return False
    # Red canaries cleared as unrelated still require a saved sanitized /PATCHSET_LEVEL draft
    draft_statuses = CANARY_DRAFT_STATUS_RE.findall(text)
    if draft_statuses and draft_statuses[-1] == "MISSING":
      return False
    return True
  totals = TRY_RESULTS_TOTAL_RE.findall(text)
  if totals:
    if int(totals[-1]) == 0:
      return False
    return not TRY_RESULTS_NOT_GREEN_RE.search(text)
  return None


# A google3 compare.md counts as passed only with a VALID integrity verdict and 0 regressions
def _g3_report_passed(text: str) -> bool:
  if G3_REPORT_TITLE not in text or not G3_VALID_RE.search(text):
    return False
  regs = [int(n) for n in G3_REGRESSIONS_RE.findall(text)]
  return bool(regs) and all(n == 0 for n in regs)


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


# Extract `-b <bot>` targets from real `git cl try` / `canary_advisor.py try` shell segments
# outside grep/echo
def _extract_try_bots_from_cmd(cmd: str) -> set[str]:
  bots: set[str] = set()
  for seg in re.split(r"&&|\|\||;|\||\n", strip_heredocs(cmd)):
    seg = seg.strip()
    if not seg or not re.search(r"\bgit\s+cl\s+try\b|canary_advisor\.py\s+try\b", seg):
      continue
    if re.match(r"^(?:grep|egrep|fgrep|rg|git\s+grep|echo|cat)\b", seg):
      continue
    for m in GIT_CL_TRY_BOT_RE.finditer(seg):
      bots.add(m.group(1))
  return bots


# Require exit code 0, no depot_tools error output (even under `|| true`), and the Buildbucket
# confirmation that only `canary_advisor.py try` prints
def _try_output_succeeded(content: str, status: str = "") -> bool:
  if status == "ERROR" or GIT_CL_TRY_ERROR_RE.search(content):
    return False
  if not TRIGGER_VERIFIED_RE.search(content):
    return False
  m_exit = EXIT_CODE_RE.search(content)
  return bool(m_exit and int(m_exit.group(1)) == 0)


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
        # Strip --force so set ... done detection matches forced completions too
        rest = [t for t in tokens[i + 1 :] if t != "--force"]
        if len(rest) >= 4 and rest[0] == "set" and rest[3] == "done":
          return True
        if len(rest) == 3 and rest[0] == "set" and rest[2] == "done":
          return True
  return False


PLUGIN_SKILL_CACHE_FILE = os.path.expanduser(
    "~/.cache/jetski/stop_guard_plugin_skills.json"
)
_MEM_PLUGIN_SKILL_CACHE: dict[str, list[str]] | None = None


def _root_symlink_aliases(prefixes: tuple[str, ...]) -> tuple[str, ...]:
  """Returns prefixes rewritten through top-level symlinks of '/' (macOS /var -> /private/var).

  A link target written as /var/... must match a realpath prefix under /private/var. Only
  readlink runs on the entries of '/', so no target (possibly a FUSE mount) is followed
  """
  try:
    entries = list(os.scandir(os.sep))
  except OSError:
    return ()
  aliases: list[str] = []
  for entry in entries:
    if not entry.is_symlink():
      continue
    try:
      target = os.path.normpath(os.path.join(os.sep, os.readlink(entry.path)))
    except OSError:
      continue
    target_prefix = target.rstrip(os.sep) + os.sep
    for prefix in prefixes:
      if prefix.startswith(target_prefix):
        alias = entry.path.rstrip(os.sep) + os.sep + prefix[len(target_prefix):]
        if alias not in prefixes and alias not in aliases:
          aliases.append(alias)
  return tuple(aliases)


def _is_external_symlink(
    entry: os.DirEntry, base_dir: str, local_prefixes: tuple[str, ...]
) -> str:
  """Returns raw readlink target when entry points outside local_prefixes and base_dir."""
  if not entry.is_symlink():
    return ""
  try:
    raw_target = os.readlink(entry.path)
  except OSError:
    return ""
  resolved = os.path.abspath(
      os.path.join(os.path.dirname(entry.path), raw_target)
  )
  base_prefixes = (
      os.path.abspath(base_dir).rstrip(os.sep) + os.sep,
      os.path.realpath(base_dir).rstrip(os.sep) + os.sep,
  )
  # A symlinked base_dir resolves to /private/var on macOS; targets may keep the /var spelling
  base_prefixes += _root_symlink_aliases(base_prefixes)
  if any(resolved.startswith(p) for p in base_prefixes + local_prefixes):
    return ""
  return raw_target


def _scan_skill_dir(
    skills_dir: str,
    local_prefixes: tuple[str, ...],
    skills: dict[str, str],
) -> bool:
  """Populates skills from skills_dir and returns True when scanned without transient I/O errors."""
  try:
    entries = list(os.scandir(skills_dir))
  except FileNotFoundError:
    # Missing skills/ is authoritative only when the parent plugin directory itself is reachable
    try:
      return stat.S_ISDIR(os.stat(os.path.dirname(skills_dir)).st_mode)
    except OSError:
      return False
  except NotADirectoryError:
    return True
  except OSError:
    return False
  clean = True
  for entry in entries:
    name = entry.name
    if ("-" not in name and "_" not in name) or name.startswith("."):
      continue
    skill_md = os.path.join(entry.path, "SKILL.md")
    # External skill symlinks sit on network FUSE mounts whose stats stall on upstream churn
    if _is_external_symlink(entry, skills_dir, local_prefixes):
      skills[name] = skill_md
      continue
    try:
      if stat.S_ISREG(os.stat(skill_md).st_mode):
        skills[name] = skill_md
    except (FileNotFoundError, NotADirectoryError):
      continue
    except OSError:
      clean = False
  return clean


def _cached_external_plugin_skills(
    plugin_path: str, raw_target: str, local_prefixes: tuple[str, ...]
) -> list[str]:
  """Returns cached skill directory names for an external plugin symlink."""
  global _MEM_PLUGIN_SKILL_CACHE
  # Key indefinitely by (plugin_path, raw_target) so head churn never triggers periodic FUSE stats
  if _MEM_PLUGIN_SKILL_CACHE is None:
    loaded: dict[str, list[str]] = {}
    try:
      with open(PLUGIN_SKILL_CACHE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
      if isinstance(data, dict):
        loaded = {
            str(k): [str(x) for x in v]
            for k, v in data.items()
            if isinstance(v, list)
        }
    except Exception:
      loaded = {}
    _MEM_PLUGIN_SKILL_CACHE = loaded

  cache_key = f"{plugin_path}->{raw_target}"
  if cache_key in _MEM_PLUGIN_SKILL_CACHE:
    return _MEM_PLUGIN_SKILL_CACHE[cache_key]

  found: dict[str, str] = {}
  clean = _scan_skill_dir(
      os.path.join(plugin_path, "skills"), local_prefixes, found
  )
  names = sorted(found)
  # Skip caching on transient FUSE or LOAS errors so empty [] never poisons future hook runs
  if not clean:
    return names
  _MEM_PLUGIN_SKILL_CACHE[cache_key] = names
  # Atomic replace prevents partial JSON reads when parallel test workers warm the cache
  try:
    os.makedirs(os.path.dirname(PLUGIN_SKILL_CACHE_FILE), exist_ok=True)
    tmp_path = f"{PLUGIN_SKILL_CACHE_FILE}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(_MEM_PLUGIN_SKILL_CACHE, f, indent=2, sort_keys=True)
    os.replace(tmp_path, PLUGIN_SKILL_CACHE_FILE)
  except OSError:
    pass
  return names


def discover_skills() -> dict:
  """Maps hyphenated or underscored skill directory names to their SKILL.md paths."""
  import glob

  skills: dict[str, str] = {}
  suffix = os.path.join("*", "SKILL.md")
  plugin_mid = os.path.join("*", "skills")
  home_dir = os.path.expanduser("~")
  tmp_dir = tempfile.gettempdir()
  # Include realpath for symlinked home/temp dirs while excluding '/' so root HOME stays external
  local_prefixes = tuple(
      dict.fromkeys(
          prefix
          for p in (
              home_dir,
              os.path.realpath(home_dir),
              tmp_dir,
              os.path.realpath(tmp_dir),
          )
          if (prefix := os.path.abspath(p).rstrip(os.sep) + os.sep) != os.sep
      )
  )
  # realpath gives /private/var on macOS while link targets keep the /var spelling
  local_prefixes += _root_symlink_aliases(local_prefixes)
  for pattern in SKILL_GLOBS:
    expanded = os.path.expanduser(pattern)
    if not expanded.endswith(suffix):
      for path in glob.glob(expanded):
        name = os.path.basename(os.path.dirname(path))
        if "-" in name or "_" in name:
          skills[name] = path
      continue
    parent_glob = expanded[: -len(suffix)].rstrip(os.sep)
    # Inspect plugin entries via scandir before globbing */skills to avoid FUSE plugin stats
    if parent_glob.endswith(os.sep + plugin_mid):
      plugins_root = parent_glob[: -(len(plugin_mid) + 1)].rstrip(os.sep)
      if not glob.has_magic(plugins_root):
        try:
          plugin_entries = list(os.scandir(plugins_root))
        except OSError:
          continue
        for p_entry in plugin_entries:
          if p_entry.name.startswith("."):
            continue
          ext_target = _is_external_symlink(p_entry, plugins_root, local_prefixes)
          if ext_target:
            for s_name in _cached_external_plugin_skills(
                p_entry.path, ext_target, local_prefixes
            ):
              skills[s_name] = os.path.join(
                  p_entry.path, "skills", s_name, "SKILL.md"
              )
            continue
          _scan_skill_dir(
              os.path.join(p_entry.path, "skills"), local_prefixes, skills
          )
        continue
    skill_dirs = (
        [parent_glob]
        if not glob.has_magic(parent_glob)
        else glob.glob(parent_glob)
    )
    for skills_dir in skill_dirs:
      _scan_skill_dir(skills_dir, local_prefixes, skills)
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
  g3_required = False
  g3_passed = False
  g3_cmd = ""
  tryjobs_green = None
  skia_review_cl = ""
  skia_cpp_detected = False
  pending_canary_advisor = False
  # Track review discipline and triage persistence across turn steps
  triage_target_seen = False
  triage_store_saved = False
  # Background task and subagent tracking for Flutter/WebParagraph corpus runs
  bg_corpus_tasks: dict[str, str] = {}
  bg_try_tasks: dict[str, set[str]] = {}
  pending_run_cmds: list[set[str]] = []
  pending_manage_try_bots: set[str] = set()
  pending_subagent_scripts: set[str] = set()
  subagent_corpus: dict[str, set[str]] = {}
  pending_manage_task_script = ""
  pending_read_path = ""

  # Walk planner tool calls and command outputs in chronological order
  for step in steps:
    stype = step.get("type")
    if stype == "PLANNER_RESPONSE":
      in_turn = int(step["step_index"]) > result["last_user_idx"]
      if in_turn and str(step.get("content", "")).strip():
        last_planner_text = str(step.get("content", ""))
      # Reset per-step pending triggers before parsing tools in this turn
      pending_triage = False
      pending_corpus_script = ""
      # Track corpus scripts delegated to subagents or task checks
      pending_run_cmds = []
      pending_manage_try_bots = set()
      pending_subagent_scripts = set()
      pending_manage_task_script = ""
      pending_read_path = ""
      for tc in step.get("tool_calls") or []:
        tname = tc.get("name", "")
        targs = tc.get("args") or {}
        if tname in READ_TOOLS:
          p = _unquote_path(get_tool_target_file(targs))
          read_blobs.append(p)
          pending_read_path = p
          # Inspect compare.md paths immediately when viewed or read
          if p.endswith("compare.md"):
            for s in _check_compare_md_paths(p):
              corpus_passed[s] = True
        elif tname == "invoke_subagent":
          prompt = str(targs.get("Prompt", ""))
          read_blobs.append(prompt)
          # Track corpus test gates delegated to isolated subagents
          pending_subagent_scripts.update(_corpus_scripts_in_text(prompt))
        elif tname == "manage_task":
          action = str(targs.get("Action", "")).lower()
          tid = str(targs.get("TaskId", "")).strip()
          # Match status checks on tracked background corpus tasks
          if action == "status":
            matched_s = bg_corpus_tasks.get(tid) or bg_corpus_tasks.get(tid.split("/")[-1])
            if matched_s:
              pending_manage_task_script = matched_s
            matched_try = bg_try_tasks.get(tid) or bg_try_tasks.get(tid.split("/")[-1])
            if matched_try:
              pending_manage_try_bots = set(matched_try)
        elif tname == "run_command":
          cmd = str(targs.get("CommandLine", ""))
          read_blobs.append(cmd)
          # Validate compare.md paths embedded in shell commands
          for s in _check_compare_md_paths(cmd):
            corpus_passed[s] = True
          # Track triage_cl execution and subsequent triage_store save-review calls
          if _invokes_script_in_cmd(cmd, "triage_cl.py"):
            pending_triage = True
            triage_target_seen = True
          if _invokes_script_in_cmd(cmd, "triage_store.py") and (
              "save-review" in cmd or "save-audit" in cmd
          ):
            triage_store_saved = True
          if _invokes_script_in_cmd(cmd, "canary_advisor.py"):
            canary_advisor_invoked = True
            pending_canary_advisor = True
          # Defer crediting trybots until the matching GENERIC/task output exits 0 cleanly
          pending_run_cmds.append(_extract_try_bots_from_cmd(cmd))
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
      # Check command or file output for valid compare.md attestation reports
      for s in _corpus_scripts_from_compare_report(content, path_hint=pending_read_path):
        corpus_passed[s] = True
      for s in _check_compare_md_paths(content):
        corpus_passed[s] = True
      if _g3_report_passed(content):
        g3_passed = True

      # Skip view_file (`File Path:`) and read_file (`Showing lines`) payloads before command checks
      if "\nFile Path: " in content[:200] or "\nShowing lines " in content[:200]:
        pending_read_path = ""
        continue

      # Map newly created subagent id to its requested corpus scripts
      m_sub = SUBAGENT_CREATED_RE.search(content)
      if m_sub and pending_subagent_scripts:
        subagent_corpus[m_sub.group(1).strip()] = set(pending_subagent_scripts)
        pending_subagent_scripts = set()

      # Map background task id to its corpus script or tryjob invocation
      m_bg = BG_TASK_LAUNCH_RE.search(content)
      if m_bg:
        tid = m_bg.group(1).strip()
        bg_scripts = _corpus_scripts_in_text(content)
        if not bg_scripts and pending_corpus_script:
          bg_scripts = {pending_corpus_script}
        for s in bg_scripts:
          bg_corpus_tasks[tid] = s
          bg_corpus_tasks[tid.split("/")[-1]] = s
        try_bots = pending_run_cmds.pop(0) if pending_run_cmds else set()
        if try_bots:
          bg_try_tasks[tid] = try_bots
          bg_try_tasks[tid.split("/")[-1]] = try_bots
      elif pending_run_cmds and (
          EXIT_CODE_RE.search(content) or step.get("status") == "ERROR"
      ):
        try_bots = pending_run_cmds.pop(0)
        if try_bots and _try_output_succeeded(content, str(step.get("status") or "")):
          triggered_try_bots.update(try_bots)
          # Scheduling new builds invalidates any earlier green tryjob verdict
          tryjobs_green = None
      if pending_manage_try_bots:
        if _try_output_succeeded(content, str(step.get("status") or "")):
          triggered_try_bots.update(pending_manage_try_bots)
          tryjobs_green = None
        pending_manage_try_bots = set()

      m_action = AGENT_ACTION_TRY_RE.search(content)
      if m_action:
        cmd_str = m_action.group(1).strip()
        required_agent_try_cmd = cmd_str
        tryjobs_green = None
        for m in GIT_CL_TRY_BOT_RE.finditer(cmd_str):
          required_agent_bots.add(m.group(1))
      tj = _tryjob_report_green(content)
      if tj is not None:
        tryjobs_green = tj
      if pending_canary_advisor:
        if G3_REQUIRED_RE.search(content):
          g3_required = True
          m_g3 = G3_ACTION_CMD_RE.search(content)
          if m_g3:
            g3_cmd = m_g3.group(1).strip()
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
      # Record synchronous test or task-status completion
      if pending_corpus_script or pending_manage_task_script:
        m_exit = EXIT_CODE_RE.search(content)
        if (
            m_exit
            and int(m_exit.group(1)) == 0
            and step.get("status") == "DONE"
        ):
          # Mark the synchronously completed corpus script or task as passed
          target_s = pending_corpus_script or pending_manage_task_script
          corpus_passed[target_s] = True
        pending_corpus_script = ""
        pending_manage_task_script = ""

    elif stype == "SYSTEM_MESSAGE":
      content = str(step.get("content", ""))
      if _g3_report_passed(content):
        g3_passed = True
      for s in _corpus_scripts_from_compare_report(content):
        corpus_passed[s] = True
      for s in _check_compare_md_paths(content):
        corpus_passed[s] = True

      # Check background task completion
      m_td = BG_TASK_DONE_RE.search(content)
      sender = ""
      m_snd = SYS_MSG_SENDER_RE.search(content)
      if m_snd:
        sender = m_snd.group(1).strip()
      task_id = m_td.group(1).strip() if m_td else sender
      bg_script = bg_corpus_tasks.get(task_id) or bg_corpus_tasks.get(task_id.split("/")[-1])
      if bg_script:
        m_exit = EXIT_CODE_RE.search(content)
        if m_exit and int(m_exit.group(1)) == 0:
          corpus_passed[bg_script] = True
      bg_try = bg_try_tasks.get(task_id) or bg_try_tasks.get(task_id.split("/")[-1])
      if bg_try and _try_output_succeeded(content, str(step.get("status") or "")):
        triggered_try_bots.update(bg_try)
        tryjobs_green = None
      tj = _tryjob_report_green(content)
      if tj is not None:
        tryjobs_green = tj

      # Check subagent completion report
      cand_scripts = set(subagent_corpus.get(sender, set())) | _corpus_scripts_in_text(content)
      if cand_scripts and not INVALID_INTEGRITY_RE.search(content):
        if VALID_INTEGRITY_RE.search(content) or SUBAGENT_EXIT_ZERO_RE.search(content):
          rep_scripts = _corpus_scripts_from_compare_report(content)
          for s in cand_scripts:
            if not rep_scripts or s in rep_scripts or s in content:
              corpus_passed[s] = True

  # Every discover_skills key contains '-' or '_', so skip scanning when neither appears in request
  if "-" in request_text or "_" in request_text:
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

  # Require saving review verdict for Flight Deck when a review classifies Case 1-5
  if (
      triage_target_seen
      and CASE_CLASSIFICATION_RE.search(combined_review_out)
      and not triage_store_saved
  ):
    result["artifact_issues"].append(
        "Your review classifies a Case 1-5 outcome after running `triage_cl.py`, "
        "but you did not save the review verdict for Flight Deck via "
        "`python3 ~/.gemini/config/scripts/triage_store.py save-review --target <ID> "
        "--patchset <PS> --outcome <CLEAN|NITS|COMPANION_TESTS|COMPANION_FIX|PUSHBACK|REASSIGN> "
        '--summary "<1-line finding summary>"`'
    )

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

  # Block approval of paragraph CLs until the google3 A/B corpus reports VALID with 0 regressions
  user_opted_out_g3 = any(
      USER_G3_OPTOUT_RE.search(u) or USER_CORPUS_OPTOUT_RE.search(u) for u in all_user_requests
  )
  if (
      g3_required
      and not g3_passed
      and not user_opted_out_g3
      and REVIEW_APPROVAL_RE.search(combined_review_out)
      and not UNVERIFIED_G3_RE.search(combined_review_out)
  ):
    run_hint = g3_cmd or (
        "python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/"
        "run_g3_paragraph_tests.py --out /tmp/g3_cl_<N> --create-citc --tier all "
        "baseline=G3_HEAD cand=cl:<N>/<PS>"
    )
    result["corpus_issues"].append(
        "[GOOGLE3 GATE GUARD] `canary_advisor.py` made the local google3 A/B corpus a gate "
        "for this SkParagraph/SkUnicode CL, and your response recommends approval (`Case 1-3` / "
        "`LGTM` / `Code-Review+1`), but no google3 `compare.md` with a `VALID` Integrity Check "
        "and 0 regressions was seen in this conversation. Withdraw the approval wording, run "
        f"`{run_hint}` in the background and report `google3 A/B` as pending in the Execution "
        "Ledger (or mark the review `UNVERIFIED IN GOOGLE3` if the environment prevents it)"
    )

  # Enforce mandatory ASAN try job and external-author canary try jobs
  user_opted_out_try = any(USER_TRYJOB_OPTOUT_RE.search(u) for u in all_user_requests)
  # A killed watcher let a red Mac trybot through on Gerrit 1400176 PS2, so approval waits
  # for a green tryjob report after the advisor's bots were triggered
  if (
      not user_opted_out_try
      and required_agent_bots
      and required_agent_bots <= triggered_try_bots
      and tryjobs_green is not True
      and REVIEW_APPROVAL_RE.search(combined_review_out)
      and not UNVERIFIED_TRYBOTS_RE.search(combined_review_out)
  ):
    state = "not green" if tryjobs_green is False else "never checked"
    result["asan_canary_issues"].append(
        f"[TRYJOB GATE GUARD] The tryjobs triggered from `canary_advisor.py` are {state}, but "
        "your response recommends approval (`Case 1-3` / `LGTM` / `Code-Review+1`). Run "
        "`python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py triage "
        "--cl <N> --ps <PS> --save-draft` and approve only after it prints `TRYJOBS: ALL GREEN` "
        "without `CANARY DRAFT: MISSING` (or mark the review `UNVERIFIED ON TRYBOTS`)"
    )
  if not user_opted_out_try:
    missing_try_bots = required_agent_bots - triggered_try_bots
    if missing_try_bots:
      result["asan_canary_issues"].append(
          "[ASAN/CANARY GUARD] `skia-canary-advisor` specified required try job(s) for the agent to run, "
          f"but the following bot(s) were not confirmed by `canary_advisor.py try` "
          f"(`TRIGGER: VERIFIED`): {', '.join(sorted(missing_try_bots))}. "
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
  last_user_request = ""
  # Ledger acks are timestamped, so the turn boundary is needed as a time, not a step index
  last_user_ts = ""
  for idx in ordered_indices:
    if steps_by_idx[idx].get("type") == "USER_INPUT":
      last_user_idx = idx
      last_user_ts = str(steps_by_idx[idx].get("created_at", ""))
      raw_u = str(steps_by_idx[idx].get("content", ""))
      m_u = USER_REQUEST_RE.search(raw_u)
      last_user_request = m_u.group(1) if m_u else raw_u

  from context_guard import (
      is_chat_lifecycle_handoff_command,
      is_handoff_launch_command,
  )

  last_code_edit_idx = -1
  last_edited_file = ""
  last_cmd_idx = -1
  last_cmd_exit_code = None
  pending_cmds: list[tuple[int, bool, bool, bool, bool, bool]] = []
  # Map background task id to its pending command details
  bg_pending_cmds: dict[str, tuple[int, bool, bool, bool, bool, bool]] = {}
  launched_new_conv = False
  scheduled_timer = False
  asked_question = False
  roadmap_done_idx = -1
  vcs_baton_lines: list[str] = []
  git_publish_after_done = False
  set_title_succeeded = False
  # The decision guard checks the final reply against contract triage made after this user turn
  last_planner_text = ""
  # Track non-question planner responses in the current turn so post-wakeup stubs do not drop context
  planner_texts_since_ask: list[str] = []
  # Track latest wakeup index so parallel-subagent question blocks key per wakeup
  last_wakeup_idx = -1
  # Exempt compact [CLICKABLE LINKS GUARD] postscripts from final_context_preservation_guard
  is_links_postscript_step = False

  for idx in ordered_indices:
    if idx <= last_user_idx:
      continue
    step = steps_by_idx[idx]
    stype = step.get("type", "")
    tool_calls = step.get("tool_calls") or []
    # Scope asked_question to the latest non-empty planner step so answered calls do not leak
    if stype == "PLANNER_RESPONSE":
      step_text = str(step.get("content", ""))
      # Answered ask_question steps are choice breakdowns, not earlier final reports
      has_ask = any(tc.get("name") == "ask_question" for tc in tool_calls)
      if has_ask:
        planner_texts_since_ask.clear()
        is_links_postscript_step = False
      elif step_text.strip():
        planner_texts_since_ask.append(step_text.strip())
      if tool_calls:
        is_links_postscript_step = False
      if step_text.strip():
        last_planner_text = step_text
      # Keep asked_question scoped to the latest non-empty or tool-calling step
      if step_text.strip() or tool_calls:
        asked_question = has_ask

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
                  r"\b(?:git(?:\s+(?:-[Cc]\s+\S+|--[a-z-]+(?:=\S+)?))*\s+"
                  r"(?:push|commit|cl\s+upload)|gh\s+pr\s+create"
                  r"|auto_ship\.py\s+ship)\b",
                  cmd,
              )
          )
          if (
              not is_handoff
              and not is_rm_done
              and not is_set_title
              and not is_git_pub
              and is_inspection_command(cmd)
          ):
            continue
          last_cmd_idx = idx
          pending_cmds.append((
              idx,
              is_handoff,
              is_cli,
              is_rm_done,
              is_git_pub,
              is_set_title,
          ))

    elif stype == "GENERIC" and pending_cmds:
      content = str(step.get("content", ""))
      # Ignore view_file and read_file outputs so quoted exit-code strings never pop pending_cmds
      if "\nFile Path: " in content[:200] or "\nShowing lines " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content)
      if m:
        (
            c_idx,
            is_handoff,
            is_cli,
            is_rm_done,
            is_git_pub,
            is_set_title,
        ) = pending_cmds.pop(0)
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
      else:
        # Move command to background table when tool call yields a background task id
        m_bg = BG_TASK_LAUNCH_RE.search(content)
        if m_bg:
          tid = m_bg.group(1).strip()
          item = pending_cmds.pop(0)
          bg_pending_cmds[tid] = item
          bg_pending_cmds[tid.split("/")[-1]] = item

    # Keep cron chats marked as scheduled across recurring wakeup ticks
    elif stype == "SYSTEM_MESSAGE":
      # Key parallel-subagent question blocks per wakeup so each wakeup gets one guard check
      last_wakeup_idx = idx
      # Inspect system message body for timer recurrence or background completion
      content = str(step.get("content", ""))
      is_links_postscript_step = "[CLICKABLE LINKS GUARD]" in content and (
          "stop hook blocked termination" in content.lower()
      )
      if CRON_WAKEUP_RE.search(content):
        scheduled_timer = True
      m_exit = EXIT_CODE_RE.search(content)
      # Resolve background task completion delivered via system message
      if m_exit:
        task_id = ""
        m_td = BG_TASK_DONE_RE.search(content)
        if m_td:
          task_id = m_td.group(1).strip()
        else:
          m_snd = SYS_MSG_SENDER_RE.search(content)
          if m_snd:
            task_id = m_snd.group(1).strip()
        item = bg_pending_cmds.get(task_id) or bg_pending_cmds.get(task_id.split("/")[-1])
        if item:
          (
              c_idx,
              is_handoff,
              is_cli,
              is_rm_done,
              is_git_pub,
              is_set_title,
          ) = item
          code = int(m_exit.group(1))
          last_cmd_exit_code = code
          launched_marker = (
              '"newConversation"' in content
              or '"new_conversation_id"' in content
              or is_cli
          )
          if is_handoff and code == 0 and launched_marker:
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

  # Capture longest prior planner response in this turn to detect post-wakeup context truncation
  pre_final_max_planner_text = (
      max(planner_texts_since_ask[:-1], key=len)
      if len(planner_texts_since_ask) >= 2
      else ""
  )
  # Continuation turns must end with a self-contained chain summary when finishing the task
  is_continuation_turn = (
      "Continuing unfinished work from previous conversation ("
      in last_user_request
  )

  return {
      "last_user_idx": last_user_idx,
      "last_user_request": last_user_request,
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
      "last_user_ts": last_user_ts,
      "last_planner_text": last_planner_text,
      # Expose prior planner response and continuation flag to final context preservation guard
      "pre_final_max_planner_text": pre_final_max_planner_text,
      "is_continuation_turn": is_continuation_turn,
      # Expose wakeup boundary to parallel-subagent question guard
      "last_wakeup_idx": last_wakeup_idx,
      "is_links_postscript_step": is_links_postscript_step,
  }


# Patterns used to detect plain-text user choice or parallel-subagent questions at turn end
FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
OPTION_LINE_RE = re.compile(
    r"^(?:[-*]\s+)?(?:\*\*|__)?(?:"
    r"[1-9][.)]|[a-dA-D][.)]|вариант\s+\d+|option\s+\d+|\(?recommended\)?"
    r")\b",
    re.IGNORECASE,
)
BULLET_LINE_RE = re.compile(r"^(?:[-*•]|[1-9][.)]|[a-dA-D][.)])\s+\S")
OPEN_ENDED_PASTE_RE = re.compile(
    r"\b(?:пришли\w*|скинь\w*|подскажи\w*|укажи\w*|вставь\w*"
    r"|paste|provide\s+the|send\s+the|share\s+the)\b"
    r".*?\b(?:url|ссылк\w*|пут[ьи]|названи\w*|имя|id|лог|стек)\b",
    re.IGNORECASE,
)
CHOICE_OR_CONFIRM_TAIL_RE = re.compile(
    r"(?:"
    r"\b(?:какой|какую|какое|какие)\s+(?:вариант\w*|опци\w*|подход\w*|путь|из\b)"
    r"|\bчто\s+(?:выбираеш\w*|предпочитаеш\w*|берём|берем|делаем)\b"
    r"|\bхочешь\b|\bсдела(?:ть|ю|ем)\b|\bделаем\b"
    r"|\bзапуска(?:ть|ем)\b|\bзапуст(?:ить|им)\b|\bзапущу\b"
    r"|\bоткр(?:ыть|ою|оем|ываем)\b|\bначин(?:ать|аем)\b|\bначн(?:у|ём|ем)\b"
    r"|\bпродолж(?:ать|у|им|аем)\b|\bберём\b|\bберем\b"
    r"|\bоставля(?:ть|ем)\b|\bостав(?:ить|лю|им)\b"
    r"|\bпримен(?:ять|ить|ю|им)\b|\bкоммит(?:ить|им)\b|\bпуш(?:ить|им)\b"
    r"|\bправ(?:ить|лю|им)\b|\bчин(?:ить|ю|им)\b|\bсогласна\b"
    r"|\b(?:ок|ok)\s*[?？]|\bли\b"
    r"|\bили\b"
    r"|\bwhich\s+(?:option|approach|variant|one)\b"
    r"|\b(?:should|shall)\s+(?:i|we)\b"
    r"|\bwould\s+you\s+like\b|\bdo\s+you\s+want\b"
    r"|\bproceed\b|\bgo\s+ahead\b|\bready\s+to\b"
    r"|\bor\b"
    r")",
    re.IGNORECASE,
)


# Strips fenced code blocks and returns non-empty prose lines
def _non_code_lines(text: str) -> list[str]:
  if not text:
    return []
  stripped = FENCED_CODE_RE.sub("", text)
  return [ln.strip() for ln in stripped.splitlines() if ln.strip()]


# Returns True when a prose line ends with a question mark rather than a bare URL
def _line_is_question(line: str) -> bool:
  tail = line.rstrip("*_\"'`»)] ")
  if not tail or tail.startswith(("http://", "https://")):
    return False
  return tail.endswith("?") or tail.endswith("？")


# Extracts (question_line, option_count) when a turn ends with a question or question+options
def _extract_trailing_question(text: str) -> tuple[str, int]:
  lines = _non_code_lines(text)
  if not lines:
    return "", 0
  opt_count = sum(1 for ln in lines if OPTION_LINE_RE.match(ln))
  if _line_is_question(lines[-1]):
    return lines[-1], opt_count
  # Catch questions placed immediately above a trailing >=2-item bullet or numbered list
  idx = len(lines) - 1
  while idx >= 0 and BULLET_LINE_RE.match(lines[idx]):
    idx -= 1
  trailing_bullets = len(lines) - 1 - idx
  if trailing_bullets >= 2 and idx >= 0 and _line_is_question(lines[idx]):
    return lines[idx], max(opt_count, trailing_bullets)
  return "", 0


# Blocks plain-text questions while background work keeps the chat hidden in Working
def parallel_subagent_question_reason(info: dict) -> str:
  if (
      info.get("asked_question")
      or info.get("scheduled_timer")
      or info.get("launched_new_conv")
  ):
    return ""
  q_line, _ = _extract_trailing_question(str(info.get("last_planner_text") or ""))
  if not q_line:
    return ""
  return (
      "[PARALLEL SUBAGENT QUESTION GUARD] Background work is still running "
      "(`fullyIdle=false`), so this conversation remains in `Working` and will NOT "
      "surface in `Needs Me`, yet your message ends with a plain-text question to "
      "the user. Do not ask parallel plain-text questions while subagents or "
      "background commands are running: either wait silently for the background "
      "work to finish and ask together with its results, or if you need the user's "
      "decision right now, call the `ask_question` tool."
  )


# Requires ask_question for end-of-turn choices and action confirmations
def ask_question_guard_reason(info: dict) -> str:
  if info.get("asked_question") or info.get("launched_new_conv"):
    return ""
  q_line, option_count = _extract_trailing_question(
      str(info.get("last_planner_text") or "")
  )
  if not q_line:
    return ""
  # Without >=2 explicit options, exempt open-ended URL/path paste requests
  if option_count < 2:
    if OPEN_ENDED_PASTE_RE.search(q_line):
      return ""
    if not CHOICE_OR_CONFIRM_TAIL_RE.search(q_line):
      return ""
  return (
      "[ASK_QUESTION GUARD] Your response ends with a choice or confirmation "
      "question in plain text without calling `ask_question`. Plain-text questions "
      "leave the conversation unblocked (`DONE` instead of `WAITING` / urgent `🔥` "
      "in `Needs Me`). Call the `ask_question` tool with concrete options (placing "
      "`(Recommended) ...` first) instead of asking in plain text."
  )


AUTO_SHIP_OPTOUT_RE = re.compile(
    r"\b(?:не\s+коммит\w*|не\s+пуш\w*|без\s+коммит\w*|без\s+пуш\w*|только\s+локально\b"
    r"|do\s+not\s+(?:commit|push)\b|don'?t\s+(?:commit|push)\b"
    r"|no\s+(?:commit|push)\b|without\s+(?:committing|pushing)\b|not\s+now\b)",
    re.IGNORECASE,
)
VCS_BATON_REPO_RE = re.compile(r"repo=([^;]+);")


def _is_personal_vcs_baton_line(line: str) -> bool:
  if "action: call ask_question" in line:
    return False
  if "action: personal repo auto-ship" in line:
    return True
  m = VCS_BATON_REPO_RE.search(line)
  if not m:
    return False
  try:
    import auto_ship

    repo = m.group(1).strip()
    if not auto_ship.is_personal_repo(repo):
      return False
    return auto_ship._repo_branch(repo) in ("", "main")
  except Exception:
    return False


# Trigger auto-ship for personal repos; keep ask_question for Gerrit and other GitHub repos
def auto_ship_guard_reason(
    info: dict, state: dict, conv_id: str, transcript_path: str
) -> tuple[str, bool]:
  if info.get("last_cmd_exit_code") not in (None, 0):
    return "", False
  if AUTO_SHIP_OPTOUT_RE.search(str(info.get("last_user_request") or "")):
    return "", False
  try:
    import auto_ship

    plan = auto_ship.plan_ship(conv_id, transcript_path)
  except Exception:
    return "", False
  if not plan:
    return "", False

  force_trigger = bool(
      info.get("roadmap_done_idx", -1) >= 0
      or state.get("auto_ship_deferred_question")
  )
  eligible = [e for e in plan if e.get("turn_touched") or force_trigger]
  if not eligible:
    return "", False
  if info.get("asked_question"):
    return "", True

  parts: list[str] = []
  for entry in eligible:
    repo = entry.get("repo", "")
    files = entry.get("files") or []
    unpushed = entry.get("own_unpushed") or []
    summary = (
        ", ".join(files[:8])
        if files
        else f"{len(unpushed)} unpushed own commit(s)"
    )
    msg_flag = ' -m "<concise English commit message>"' if files else ""
    parts.append(
        f"- `{repo}` ({summary}):\n"
        f"  `python3 ~/.gemini/config/hooks/auto_ship.py ship {conv_id} "
        f"--repo {repo}{msg_flag}`"
    )
  # Instruct the agent to keep the full task summary alongside the auto-ship commit line
  return (
      "[AUTO-SHIP] Personal repository changes are ready to commit and push:\n"
      + "\n".join(parts)
      + "\nExecute the command(s) above now, then repeat your full task "
      "summary in your final reply together with the exact "
      "`committed and pushed: <sha> -> origin/main` line (never reply with "
      "only a short commit stub; leave foreign files untouched; if "
      "`[FOREIGN-COMMIT PUSH GUARD]` rejects the push, ask the user before "
      "overriding; if edits are intentionally mid-task, skip and write "
      "`auto-ship deferred: <reason>`).",
      False,
  )


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

  # Personal repos are handled automatically by auto_ship_guard_reason without ask_question
  pending_lines = [
      ln
      for ln in vcs_lines
      if "[VCS BATON: PENDING" in ln and not _is_personal_vcs_baton_line(ln)
  ]
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
    from chat_lifecycle import (
        clean_base_title,
        get_conversation_title,
        has_lifecycle_title_prefix,
        is_valid_chat_topic,
    )

    cur_title = get_conversation_title(conv_id)
    # Require both [HH:MM] <marker> and a concrete topic so raw LS titles cannot bypass set-title
    if has_lifecycle_title_prefix(cur_title) and is_valid_chat_topic(
        clean_base_title(cur_title)
    ):
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


UNRELIABLE_BANNER_RE = re.compile(
    r"⚠️.*(?:ненад[её]жной\s+младшей\s+моделью|unreliable\s+junior\s+model)",
    re.IGNORECASE | re.DOTALL,
)


# Requires a visible warning banner when a non-triage turn ran on an unreliable junior model
def unreliable_model_guard_reason(info: dict, state: dict) -> str:
  label = str((state or {}).get("unreliable_model_label") or "").strip()
  # Skip check when the active turn ran on a flagship model or triage project
  if not label:
    return ""
  reply = str(
      info.get("final_planner_text") or info.get("last_planner_text") or ""
  ).strip()
  # Empty or tool-only turns have no user-visible reply to banner
  if not reply:
    return ""
  # Accept either Russian or English warning banner within the top 600 chars
  if UNRELIABLE_BANNER_RE.search(reply[:600]):
    return ""
  return (
      "[UNRELIABLE MODEL WARNING GUARD] You are running on an unreliable"
      f" junior model (`{label}`), and your reply does not start with the"
      " mandatory warning banner. Prepend the exact banner at the top of your"
      f" response (`> ⚠️ **ВНИМАНИЕ: Ответ подготовлен ненадёжной младшей моделью ({label}).**`"
      " or `> ⚠️ **WARNING: This response was generated by an unreliable"
      f" junior model ({label}).**`) and output your complete reply."
  )


# Prevents context loss when a post-wakeup/post-ship tail or continuation reply drops the full report
def final_context_preservation_guard_reason(info: dict) -> str:
  if (
      info.get("asked_question")
      or info.get("launched_new_conv")
      or info.get("is_links_postscript_step")
  ):
    return ""
  last_text = str(
      info.get("final_planner_text") or info.get("last_planner_text") or ""
  ).strip()
  if not last_text:
    return ""
  prior_text = str(info.get("pre_final_max_planner_text") or "").strip()
  prior_len = len(prior_text)
  cur_len = len(last_text)
  if prior_len >= 400 and (
      cur_len < 280 or cur_len < int(prior_len * 0.55)
  ):
    return (
        "[FINAL CONTEXT PRESERVATION GUARD] Earlier in this turn you wrote a"
        f" detailed response ({prior_len} chars), but after a background task"
        f" wakeup or follow-up tool step your final reply shrank to {cur_len}"
        " chars, losing earlier context. Combine your full earlier report and"
        " the final task/commit status into a single complete, self-contained"
        " final response so the last message of the chat preserves all context."
    )
  if info.get("is_continuation_turn") and cur_len < 250 and prior_len < 250:
    return (
        "[FINAL CONTEXT PRESERVATION GUARD] This is a continuation conversation"
        " in a handoff chain, and your final reply is too brief to stand on"
        " its own. Output a complete, self-contained summary of the work"
        " across the conversation chain (original goal/request, key changes"
        " and clickable file links across all chain pieces, verification, and"
        " commit/push status) so the user has full context in the final"
        " message without opening earlier chats."
    )
  return ""


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

  # Skip battle mode forks and subagents (including macOS subagents missing parentConversationId)
  from context_guard import resolve_parent_conversation_id

  if data.get("isBattleMode") or resolve_parent_conversation_id(data):
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
  state = load_state(state_path, conv_id)
  if conv_id:
    state.setdefault("owner", conv_id)

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
    # Catch plain-text user questions while background subagents/tasks keep fullyIdle=false
    pq_reason = parallel_subagent_question_reason(info)
    pq_key = f"{info.get('last_user_idx', -1)}:{info.get('last_wakeup_idx', -1)}"
    if pq_reason and state.get("parallel_q_blocked_key") != pq_key:
      state["parallel_q_blocked_key"] = pq_key
      save_state(state_path, state)
      print(
          json.dumps(
              {"decision": "continue", "reason": pq_reason}, ensure_ascii=False
          )
      )
      return
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

  # Check plain-text choice/confirmation questions before auto-ship so unconfirmed edits wait
  aq_reason = ask_question_guard_reason(info)
  if (
      aq_reason
      and state.get("ask_question_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["ask_question_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": aq_reason}, ensure_ascii=False
        )
    )
    return

  # Defer auto-ship until the next turn if this turn already paused on ask_question
  ship_reason, ship_deferred_q = auto_ship_guard_reason(
      info, state, conv_id, transcript_path
  )
  if ship_deferred_q and not state.get("auto_ship_deferred_question"):
    state["auto_ship_deferred_question"] = True
    save_state(state_path, state)
  elif not ship_deferred_q and state.get("auto_ship_deferred_question"):
    state.pop("auto_ship_deferred_question", None)
    save_state(state_path, state)
  if (
      ship_reason
      and state.get("auto_ship_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["auto_ship_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": ship_reason},
            ensure_ascii=False,
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

  # Contract-change triage done by this chat or its subagents must reach the user
  d_reason = ""
  if conv_id and info.get("last_user_ts"):
    try:
      import decision_guard

      d_reason = decision_guard.visibility_reason(
          conv_id, info["last_user_ts"], info.get("last_planner_text", "")
      )
    except Exception:
      d_reason = ""
  if (
      d_reason
      and state.get("decision_guard_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["decision_guard_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(json.dumps({"decision": "continue", "reason": d_reason}, ensure_ascii=False))
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

  # Block turns that show a Gerrit review reply without saving an unpublished Gerrit draft
  gd_reason = ""
  try:
    import gerrit_draft_guard

    gd_reason = gerrit_draft_guard.gerrit_draft_guard_reason(transcript_path)
  except Exception:  # lint: allow-broad-except (guard must never crash stop hook)
    gd_reason = ""
  if (
      gd_reason
      and state.get("gerrit_draft_blocked_turn") != info.get("last_user_idx", -1)
  ):
    state["gerrit_draft_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": gd_reason},
            ensure_ascii=False,
        )
    )
    return

  # Require every mentioned entity to be linked at least once (and in tables/lists) without
  # forcing regeneration when later prose paragraphs name an already-linked entity
  links_reason = ""
  reply_for_links = str(
      info.get("final_planner_text") or info.get("last_planner_text") or ""
  )
  if reply_for_links.strip():
    try:
      import clickable_links_guard

      links_reason = clickable_links_guard.clickable_links_reason(
          reply_for_links
      )
    except Exception:
      links_reason = ""
  if (
      links_reason
      and state.get("clickable_links_blocked_turn")
      != info.get("last_user_idx", -1)
  ):
    state["clickable_links_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": links_reason},
            ensure_ascii=False,
        )
    )
    return

  # Prevent short post-wakeup/post-ship stubs or bare continuation endings from dropping context
  fc_reason = final_context_preservation_guard_reason(info)
  fc_key = f"{info.get('last_user_idx', -1)}:{info.get('last_wakeup_idx', -1)}"
  if fc_reason and state.get("final_context_blocked_key") != fc_key:
    state["final_context_blocked_key"] = fc_key
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": fc_reason},
            ensure_ascii=False,
        )
    )
    return

  # Require an explicit warning banner when a non-triage turn runs on a junior model
  um_reason = unreliable_model_guard_reason(info, state)
  if (
      um_reason
      and state.get("unreliable_model_blocked_turn")
      != info.get("last_user_idx", -1)
  ):
    state["unreliable_model_blocked_turn"] = info.get("last_user_idx", -1)
    save_state(state_path, state)
    print(
        json.dumps(
            {"decision": "continue", "reason": um_reason},
            ensure_ascii=False,
        )
    )
    return

  spawn_background_dossier_update(conv_id, transcript_path)
  play_stop_sound(is_handoff=False)
  print("{}")


if __name__ == "__main__":
  main()
