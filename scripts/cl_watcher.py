#!/usr/bin/env python3
"""CL Flight Deck & Autonomous Watcher (`cl_watcher.py`).

Discovers active conversations (`⦿`) across Jetski projects that reference:
  - Skia Gerrit CLs (`skia-review.googlesource.com`, 7-digit IDs)
  - Chromium Gerrit CLs (`chromium-review.googlesource.com`, 7-digit IDs)
  - Google3 Piper CLs (`cl/<9-10 digits>`)
  - Flutter GitHub PRs (`flutter/flutter`, `PR #...`)

Responsibilities:
1. Polls Gerrit REST API, `g4 describe`, and `gh pr view` for live CL/PR status,
   patchsets, comments, CQ dry-run / submit votes, and merge SHA.
2. For merged Skia CLs, checks post-submit health against `status.skia.org` and
   the 4 outgoing autorollers (`Android`, `Chrome`, `Google3`, `Flutter`) via
   the local `skia-gardener` daemon (`http://localhost:8080/api/autorollers` and
   `/api/incidents`) and `git merge-base --is-ancestor` in `skia_main`.
3. Queries Gmail via the local MCP OAuth token (`gmailmcp.googleapis.com`) to
   report `INBOX` vs `TRASH` message counts per CL/PR and archive `INBOX`
   threads of merged changes (`batch_unlabel_threads` with `includeTrash=true`).
4. Updates a compact progress badge on the conversation title and wakes the
   target conversation via `agentapi send-message` only on significant transitions.
5. Serves the live "Flight Deck" HTML dashboard in English: what needs me first,
   then lanes by who acts next, filtered by project.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import glob
import http.client
import http.server
import json
import os
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

HOOKS_DIR = os.path.expanduser("~/.gemini/config/hooks")
if HOOKS_DIR not in sys.path:
  sys.path.insert(0, HOOKS_DIR)

import chat_lifecycle as cl  # pylint: disable=g-import-not-at-top

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPTS_DIR not in sys.path:
  sys.path.insert(0, SCRIPTS_DIR)
import audit_gate  # pylint: disable=g-import-not-at-top
import triage_store  # pylint: disable=g-import-not-at-top

CANARY_ADVISOR_DIR = os.path.normpath(
    os.path.join(SCRIPTS_DIR, "..", "skills", "skia-canary-advisor", "scripts")
)
if CANARY_ADVISOR_DIR not in sys.path:
  sys.path.insert(0, CANARY_ADVISOR_DIR)
import canary_advisor  # pylint: disable=g-import-not-at-top
import finalize_review  # pylint: disable=g-import-not-at-top

DEFAULT_PORT = 8089
DEFAULT_SKIA_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_main")
DEFAULT_GARDENER_URL = "http://localhost:8080"
DEFAULT_STATE_FILE = os.path.expanduser(
    "~/.gemini/config/logs/cl_watcher_state.json"
)
STATE_FILE = DEFAULT_STATE_FILE
# Non-zero only inside serve_forever so CLI runs never clobber the live daemon's ui_version
_SERVING_PORT = 0


# Redirect triage_store next to STATE_FILE when unit tests patch watcher state into a temp dir
def _triage_results_dir() -> str | None:
  if os.path.abspath(STATE_FILE) == os.path.abspath(DEFAULT_STATE_FILE):
    return None
  return os.path.join(os.path.dirname(os.path.abspath(STATE_FILE)), "triage_results")


# Fall back to 2-arg load_triage_result call when unit tests mock it with positional args only
def _load_triage_for_watcher(
    kind: str, tid: str, results_dir: str | None
) -> dict[str, Any] | None:
  if results_dir is None:
    return triage_store.load_triage_result(kind, tid)
  try:
    return triage_store.load_triage_result(kind, tid, results_dir=results_dir)
  except TypeError:
    return triage_store.load_triage_result(kind, tid)
OPEN_CHAT_REQ_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-open-chat.json"
)
FLIGHT_DECK_INFO_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-flight-deck.json"
)
ANNOTATIONS_DIR = os.path.expanduser("~/.gemini/jetski/annotations")
PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
# Root directory for per-project chat state files that record follow-up CLs
ROADMAPS_DIR = os.path.expanduser("~/.gemini/config/roadmaps")
IGNORED_CLS_FILE = os.path.expanduser(
    "~/.gemini/config/skills/review-triage/ignored_cls.txt"
)
# Patterns and optional manual map for bidirectional fix-for CL/PR cross-linking
MANUAL_FIX_LINKS_FILE = os.path.expanduser(
    "~/.gemini/config/fix_for_links.json"
)
# Headings and git-config lines in chat state files that identify follow-up fix CLs
RE_STATE_FIX_SECTION = re.compile(
    r"^##+\s*(?:[^\n]*\b(?:fix|follow[\s\-]*up|reland)\s+(?:cl|pr|change)\b|"
    r"(?:fix|follow[\s\-]*up|reland)\b)",
    re.IGNORECASE,
)
RE_STATE_GERRIT_ISSUE = re.compile(r"\bgerritissue\s+(\d{6,10})\b", re.IGNORECASE)
RE_BUG_TAG = re.compile(r"(?im)^\s*(?:Bug|Fixed|Fixes|Issue)\s*:\s*(.+)$")
RE_BUG_TOKEN = re.compile(
    r"(?i)\b(?:b/|skia:|crbug\.com/|chromium:|issue\s*#?|b)(\d{4,})\b"
)
RE_EXPLICIT_FIX_CL = re.compile(
    r"(?i)\b(?:fix(?:es|ed)?(?:\s+breakage\s+from|\s+regression\s+(?:in|from))?"
    r"|reland(?:s|ed)?(?:\s+of)?|revert(?:s|ed)?(?:\s+of)?|follow[- ]?up\s+(?:to|for)"
    r"|original\s+cl\s*:)\b[^\n]{0,80}?"
    r"(?:skia-review\.googlesource\.com/c/skia/\+/"
    r"|dart-review\.googlesource\.com/c/sdk/\+/"
    r"|flutter/flutter/pull/|http://cl/|cl/|(?:#|CL\s*|PR\s*#?))(\d{4,10})\b"
)
RE_EXPLICIT_FIX_SHA = re.compile(
    r"(?i)\b(?:fix(?:es|ed)?(?:\s+breakage\s+from|\s+regression\s+(?:in|from))?"
    r"|revert(?:s|ed)?(?:\s+of)?|reland(?:s|ed)?(?:\s+of)?|introduced\s+(?:in|by)"
    r"|broke\s+in)\s+(?:commit\s+)?([0-9a-f]{7,40})\b"
)
RE_QUOTED_REVERT_REL = re.compile(r'(?i)^\s*(?:Revert|Reland)\s+"([^"]+)"')
RE_SKIA_BOT_NAME = re.compile(
    r"\b((?:Test|Build|Perf|Housekeeper)-[A-Za-z0-9_.\-]+)"
)
RE_SKIA_BOT_PAIR = re.compile(
    r"\b((?:Test|Build|Perf)-[A-Za-z0-9_.]+)\s+and\s+"
    r"((?:Test|Build|Perf)-[A-Za-z0-9_.]+)\s+(?:\.\.\.|…)\s*(-[A-Za-z0-9_.\-]+)"
)
RE_FAILURE_CUE = re.compile(
    r"(?i)\b(?:without\s+this\s+change\b[^\n.]*\bfail"
    r"|fail(?:s|ed|ing)?\s+(?:on|with|in)\b"
    r"|fix(?:es|ed)?\s+(?:the\s+)?(?:build|test|bot|failure|breakage"
    r"|regression|crash|asan|msan|tsan|ubsan)|broke\b|broken\s+on\b)"
)
RE_TEST_SYMBOL = re.compile(
    r"\b([A-Za-z0-9_]*(?:_[bB]\d{5,}|Test[A-Za-z0-9_]*))\b"
)
RE_FAILING_TEST_REASON = re.compile(r"matching failing test '([^']+)'")
RE_CRASH_TEST_NAME = re.compile(r"\[([A-Za-z0-9_]+)\]:")
MCP_TOKENS_FILE = os.path.expanduser("~/.gemini/jetski/mcp_oauth_tokens.json")
GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
NOTIFY_SELF_BIN = "/google/bin/releases/g3n-team/notify_self"
PEN_URL_SH = (
    "/google/src/files/head/depot/google3/security/uberproxy/tools/contrib/pen_url.sh"
)
GERRIT_SKIA_HOST = "skia-review.googlesource.com"
GERRIT_CHROMIUM_HOST = "chromium-review.googlesource.com"
GERRIT_HOSTS = (GERRIT_SKIA_HOST, GERRIT_CHROMIUM_HOST)
# Active Skia Googlers who provide 94% of 2nd approvals on our external Skia reviews
SECOND_REVIEWER_CANDIDATES = (
    {
        "email": "alexisdavidc@google.com",
        "name": "Alexis Cruz-Ayala",
        "short": "Alexis",
    },
    {
        "email": "fmalita@google.com",
        "name": "Florin Malita",
        "short": "Florin",
    },
)
SELF_EMAILS = {"jlavrova@google.com", "jlavrova@gmail.com"}
# Include @chromium.org alias so public Chromium issue comments/assignments match self
SELF_BUG_EMAILS = {
    "jlavrova@google.com",
    "jlavrova@chromium.org",
    "jlavrova@gmail.com",
}
SELF_GITHUB_LOGINS = {"Rusino", "jlavrova"}
ISSUES_CLI_BIN = "/google/bin/releases/issues-cli/issues"
REMOTE_MCP_BIN = "/google/bin/releases/gemini-cli/tools/remote_mcp"
BUGANIZER_MCP_SERVER = "blade:devtools.buganizer.mcpservice-prod"
BUG_SYNC_TTL_SEC = 300.0
BUG_STALE_BACKLOG_DAYS = 60
BUG_RECENT_CLOSED_DAYS = 14
# Public Chromium and Skia Buganizer component roots used to pick crbug/ over b/
PUBLIC_CRBUG_COMPONENTS = {"1457031", "1363359", "1456822", "1456190", "1579812"}
SECURITY_MERGE_REPORTER = "chrome-release-blintz-continuous-runner@prod.google.com"
SECURITY_MERGE_COMPONENT_ID = "1579812"
CF_BUG_OS = "1223084"
CF_BUG_MERGE = "1223087"
CF_BUG_MERGE_MILESTONE = "1340419"
CF_BUG_FIXED_BY_CODE_CHANGES = "1358989"
CF_BUG_AUTO_CHERRY_PICK = "1419876"
RE_MERGE_TITLE_MILESTONE = re.compile(r"^\[Merge\s+M(\d+)\]\s*(.*)$", re.IGNORECASE)
ANTIGRAVITY_SDK_CLI = (
    "/google/bin/releases/gemini-agents-antigravity-sdk/antigravity_sdk_cli"
)
CANARY_SOAK_MIN_SEC = 24 * 3600.0
RE_CUSTOM_FIELD_ID = re.compile(r"\((\d+)\)\s*$")
RE_CHERRY_PICKED_FROM = re.compile(
    r"\(cherry picked from commit ([0-9a-f]{10,40})\)", re.IGNORECASE
)
RE_CHROME_MILESTONE_BRANCH = re.compile(r"(?:^|/)m(\d{2,4})\b", re.IGNORECASE)
RE_BUG_TITLE_TOKEN = re.compile(
    r"\b(?:crbug(?:\.com)?/|b/|flutter#)\d{4,10}\b",
    re.IGNORECASE,
)
RE_BUGANIZER_EXPLICIT = re.compile(
    r"(?i)(?:https?://(?:b\.corp\.google\.com|issuetracker\.google\.com)/issues/"
    r"|https?://issues\.chromium\.org/issues/"
    r"|\b(?:crbug(?:\.com)?/(?:[a-z0-9_-]+/)?|b/|buganizer[:/]|chromium:|skia:))"
    r"(\d{6,10})\b"
)
RE_GH_FLUTTER_ISSUE_EXPLICIT = re.compile(
    r"(?i)(?:https?://github\.com/flutter/flutter/issues/|\bflutter(?:/flutter)?#)"
    r"(\d{4,7})\b"
)
RE_GH_CLOSING_KEYWORD = re.compile(
    r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?|issue)\s+#(\d{5,7})\b"
)
# Non-flutter GitHub repositories use smaller issue numbers (1-7 digits)
RE_GH_CLOSING_KEYWORD_ANY_REPO = re.compile(
    r"(?i)\b(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?|issue)\s+#(\d{1,7})\b"
)
FLUTTER_TREE_CHECK = "tree-status"
# Cocoon aggregates Luci presubmit jobs under one umbrella check-run and exposes
# tip-of-tree and per-commit post-submit failures on flutter-dashboard
FLUTTER_DASHBOARD_CHECK = "Dashboard Checks"
FLUTTER_BUILD_STATUS_URL = (
    "https://flutter-dashboard.appspot.com/api/public/build-status?repo=flutter"
)
FLUTTER_GET_STATUS_URL = (
    "https://flutter-dashboard.appspot.com/api/public/get-status?repo=flutter"
)
RE_DASHBOARD_SUBJOB = re.compile(r"^\s*-\s*`([^`]+)`", re.MULTILINE)
GITHUB_STATUS_BOTS = {"flutter-dashboard"}

# Match cl/, CL, Gerrit, crrev(.com)/c/, skrev(.com)/, and Gerrit review URLs
RE_CL_ID = re.compile(
    r"(?:\b(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/|"
    r"(?:https?://)?(?:skia|chromium)-review"
    r"(?:\.git\.corp\.google|\.googlesource)\.com/c/(?:[^+\s]*/\+/)?|"
    r"\b(?:CL|Gerrit)\s+#?)(\d{6,10})\b",
    re.IGNORECASE,
)
RE_CL_ID_GROUP = re.compile(
    r"(?P<prefix>\b(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/|"
    r"(?:https?://)?(?:skia|chromium)-review"
    r"(?:\.git\.corp\.google|\.googlesource)\.com/c/(?:[^+\s]*/\+/)?|"
    r"\b(?:CL|Gerrit)\s+#?)"
    r"(?P<first>\d{6,10})"
    r"(?P<rest>(?:(?:\s*(?:[/,&+]|\band\b)\s*|\s+)(?<![a-zA-Z_]/)(?<![a-zA-Z_])\d{6,10}\b)*)",
    re.IGNORECASE,
)
# Avoid matching flutter#160251 when it is a GitHub issue next to a Skia CL,
# unless explicitly prefixed with PR
RE_PR_ID = re.compile(r"(?:\bPR\s*#?|\bpull/)(\d{5,7})\b", re.IGNORECASE)
RE_PR_ID_GROUP = re.compile(
    r"(?P<prefix>\b(?:PR\s*#?|pull/))"
    r"(?P<first>\d{5,7})"
    r"(?P<rest>(?:(?:\s*(?:[/,&+]|\band\b)\s*|\s+#?|\s+)(?<![a-zA-Z_]/)(?<![a-zA-Z_])#?\d{5,7}\b)*)",
    re.IGNORECASE,
)
# Match explicit companion references in PR subjects/bodies (e.g. companion to #195122)
RE_COMPANION_PR_REF = re.compile(
    r"(?:companion\s+(?:(?:to|for)\s+)?(?:PR\s*#?|#)|"
    r"(?:re|for)\s+(?:PR\s*#?|#)|"
    r"(?:hypotheses|hypothesis|check|repro)\s+(?:about|for|on)\s+(?:PR\s*#?|#))"
    r"(\d{5,7})\b",
    re.IGNORECASE,
)
# Match explicit companion references to Gerrit changes in subjects and commit messages
RE_COMPANION_CL_REF = re.compile(
    r"(?:companion\s+(?:(?:tests?|fix|change|cl)\s+)?(?:(?:to|for)\s+)?"
    r"(?:CL\s*|cl/|crrev(?:\.com)?/c/|skrev(?:\.com)?/|"
    r"https?://[a-z0-9.-]+/c/(?:[^/\s]+/)*\+/)|"
    r"(?:hypotheses|hypothesis|check|repro)\s+(?:about|for|on)\s+"
    r"(?:CL\s*|cl/|crrev(?:\.com)?/c/|skrev(?:\.com)?/|"
    r"https?://[a-z0-9.-]+/c/(?:[^/\s]+/)*\+/))"
    r"(\d{5,8})\b",
    re.IGNORECASE,
)
# Match GitHub companion head branch naming (companion-pr-<id>)
RE_COMPANION_BRANCH = re.compile(
    r"^companion-(?:pr|cl)?-?(\d{5,8})\b",
    re.IGNORECASE,
)
# Strip any trailing roll badge we previously attached to a title
TITLE_TOPIC_MAX = 38
RE_ROLL_BADGE = re.compile(r"\s*\[(?:⏳|✓|🔴|🔄)[^\]]*\]\s*$")
# Automated roll CLs/PRs belong to the Skia Gardener rather than personal Flight Deck rows
RE_AUTOROLL_SUBJECT = re.compile(
    r"^Roll\s+(?:Skia\b|\S+\s+from\s+[0-9a-f]{7,40}\s+to\s+[0-9a-f]{7,40}\b)",
    re.IGNORECASE,
)
# Match explicit WIP/Draft and DO NOT SUBMIT prefixes in CL and PR subjects
RE_DRAFT_SUBJECT = re.compile(
    r"^(?:\[(?!wip\b|draft\b|do\s+not\s+submit\b)[^\]]+\]\s*)*"
    r"(?:\[(?:wip|draft|do\s+not\s+submit)[^\]]*\]\s*|"
    r"(?:wip|draft|do\s+not\s+submit)\b[:\s\-]*)",
    re.IGNORECASE,
)
# Matches commit or reviewer notes indicating a patchset was a rebase without code edits
RE_REBASE_MSG = re.compile(
    r"(\bwas rebased\b|\brebased on\b|\brebased onto\b|"
    r"\brebased?\b.*\bno code change\b|\bno code change\b)",
    re.IGNORECASE,
)
# Flight Deck chats discuss dashboard behavior rather than owning tracked CLs/PRs
EXCLUDED_WATCHER_PROJECTS = frozenset({"Flight Deck"})
# Domain projects take precedence when a CL/PR is also mentioned in a meta project
META_PROJECT_NAMES = frozenset({
    "Flight Deck",
    "Improving Experience",
    "Reviews of e-mails, meetings and such",
    "sad-ru-googlers-ai-digest",
    "Personal",
    "Code Review organizer",
    "Breaking Chats",
    "Unassigned",
})

_GMAIL_TOKEN_CACHE: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_GMAIL_LOCK = threading.Lock()
# Cache tip-of-tree and immutable base-commit check failures across PR polls
_FLUTTER_TREE_CACHE: dict[str, Any] = {"tasks": set(), "expires_at": 0.0}
_FLUTTER_BASE_CACHE: dict[str, set[str]] = {}
_FLUTTER_TREE_LOCK = threading.Lock()
# Cache the startup mtime so --serve can re-exec when cl_watcher.py changes on disk
_SCRIPT_PATH = os.path.abspath(__file__)
try:
  _INITIAL_SCRIPT_MTIME = os.path.getmtime(_SCRIPT_PATH)
except OSError:
  _INITIAL_SCRIPT_MTIME = 0.0

EN_MONTHS = {
    1: "Jan",
    2: "Feb",
    3: "Mar",
    4: "Apr",
    5: "May",
    6: "Jun",
    7: "Jul",
    8: "Aug",
    9: "Sep",
    10: "Oct",
    11: "Nov",
    12: "Dec",
}


def now_et_str() -> str:
  try:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
  except Exception:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def parse_gerrit_or_iso_time(ts_str: str) -> datetime | None:
  """Parses Gerrit, GitHub ISO-8601, or Piper local timestamps."""
  if not ts_str:
    return None
  s = ts_str.strip()
  try:
    # Piper describe prints local ET timestamps as YYYY/MM/DD HH:MM:SS
    is_piper = bool(re.match(r"^\d{4}/\d{2}/\d{2}\b", s))
    if is_piper:
      s = s[:10].replace("/", "-") + s[10:]
    if s.endswith("Z"):
      s = s[:-1]
    if "." in s:
      head, frac = s.split(".", 1)
      frac_digits = re.sub(r"[^0-9]", "", frac)[:6]
      s = f"{head}.{frac_digits}" if frac_digits else head
    s = s.replace("T", " ")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
      # Piper describe local timestamps are in America/New_York; others default to UTC
      dt = dt.replace(
          tzinfo=ZoneInfo("America/New_York") if is_piper else timezone.utc
      )
    return dt
  except Exception:
    return None


def latest_timestamp(*candidates: str) -> str:
  """Returns the candidate string with the latest parsed timestamp."""
  best_str = ""
  best_ts = -1.0
  for c in candidates:
    if not c:
      continue
    dt = parse_gerrit_or_iso_time(c)
    if dt is not None:
      ts = dt.timestamp()
      # Isolate the newest event timestamp across candidate transitions
      if ts > best_ts:
        best_ts = ts
        best_str = c
  return best_str


def format_et_and_age(ts_str: str) -> tuple[str, str]:
  """Returns ('Oct 26 at 11:15 ET', '42m ago') in America/New_York time."""
  dt = parse_gerrit_or_iso_time(ts_str)
  if not dt:
    return "", ""
  try:
    et_tz = ZoneInfo("America/New_York")
    dt_et = dt.astimezone(et_tz)
    now_et = datetime.now(et_tz)
    diff_sec = max(0, int((now_et - dt_et).total_seconds()))
    if diff_sec < 90:
      age = "just now"
    elif diff_sec < 3600:
      age = f"{diff_sec // 60}m ago"
    elif diff_sec < 86400:
      hrs = diff_sec // 3600
      mins = (diff_sec % 3600) // 60
      age = f"{hrs}h {mins}m ago" if mins else f"{hrs}h ago"
    else:
      days = diff_sec // 86400
      age = f"{days}d ago"

    m_str = EN_MONTHS.get(dt_et.month, str(dt_et.month))
    if dt_et.date() == now_et.date():
      et_fmt = dt_et.strftime("%H:%M ET")
    else:
      et_fmt = f"{m_str} {dt_et.day} at {dt_et.strftime('%H:%M')} ET"
    return et_fmt, age
  except Exception:
    return "", ""


def get_gmail_access_token() -> str:
  """Obtains a fresh OAuth access token for the Gmail MCP server."""
  with _GMAIL_LOCK:
    now = time.time()
    if _GMAIL_TOKEN_CACHE["access_token"] and now < _GMAIL_TOKEN_CACHE["expires_at"]:
      return str(_GMAIL_TOKEN_CACHE["access_token"])
    try:
      if not os.path.isfile(MCP_TOKENS_FILE):
        return ""
      with open(MCP_TOKENS_FILE, "r", encoding="utf-8") as f:
        all_tokens = json.load(f)
      entry = all_tokens.get(GMAIL_MCP_URL) or {}
      tok = entry.get("token") or {}
      refresh_tok = tok.get("refresh_token")
      client_id = entry.get("client_id")
      client_secret = entry.get("client_secret")
      token_url = entry.get("token_url") or "https://oauth2.googleapis.com/token"
      if not (refresh_tok and client_id and client_secret):
        return ""
      form = urllib.parse.urlencode({
          "client_id": client_id,
          "client_secret": client_secret,
          "refresh_token": refresh_tok,
          "grant_type": "refresh_token",
      }).encode("utf-8")
      req = urllib.request.Request(token_url, data=form)
      with urllib.request.urlopen(req, timeout=8) as resp:
        resp_data = json.loads(resp.read().decode("utf-8", errors="replace"))
      access_tok = str(resp_data.get("access_token") or "")
      expires_in = int(resp_data.get("expires_in") or 3500)
      if access_tok:
        _GMAIL_TOKEN_CACHE["access_token"] = access_tok
        _GMAIL_TOKEN_CACHE["expires_at"] = now + max(60, expires_in - 120)
      return access_tok
    except Exception:
      return ""


def call_gmail_mcp(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
  """Invokes a tool on https://gmailmcp.googleapis.com/mcp/v1 via JSON-RPC."""
  tok = get_gmail_access_token()
  if not tok:
    return {}
  payload = json.dumps({
      "jsonrpc": "2.0",
      "id": 1,
      "method": "tools/call",
      "params": {"name": tool_name, "arguments": arguments},
  }).encode("utf-8")
  req = urllib.request.Request(
      GMAIL_MCP_URL,
      data=payload,
      headers={
          "Authorization": f"Bearer {tok}",
          "Content-Type": "application/json",
          "Accept": "application/json, text/event-stream",
      },
  )
  try:
    with urllib.request.urlopen(req, timeout=10) as resp:
      raw = json.loads(resp.read().decode("utf-8", errors="replace"))
    content = (raw.get("result") or {}).get("content") or []
    if content and isinstance(content[0], dict) and content[0].get("text"):
      return json.loads(content[0]["text"])
  except Exception:
    pass
  return {}


FLIGHT_DECK_GMAIL_LABEL_NAME = "Flight Deck"
_GMAIL_LABEL_ID_CACHE: dict[str, str] = {}


def ensure_flight_deck_gmail_label() -> str:
  """Finds or creates the user label 'Flight Deck' in Gmail and caches its ID."""
  cached = _GMAIL_LABEL_ID_CACHE.get(FLIGHT_DECK_GMAIL_LABEL_NAME)
  if cached:
    return cached
  data = call_gmail_mcp("list_labels", {})
  labels = (data.get("userLabels") or []) + (data.get("systemLabels") or [])
  for lbl in labels:
    if not isinstance(lbl, dict):
      continue
    name = str(lbl.get("name") or "").strip().lower()
    if name == FLIGHT_DECK_GMAIL_LABEL_NAME.lower():
      lid = str(lbl.get("id") or "")
      if lid:
        _GMAIL_LABEL_ID_CACHE[FLIGHT_DECK_GMAIL_LABEL_NAME] = lid
        return lid
  # Create label if missing
  created = call_gmail_mcp(
      "create_label",
      {
          "name": FLIGHT_DECK_GMAIL_LABEL_NAME,
          "labelListVisibility": "LABEL_SHOW",
          "messageListVisibility": "SHOW",
      },
  )
  lid = str(created.get("id") or "")
  if lid:
    _GMAIL_LABEL_ID_CACHE[FLIGHT_DECK_GMAIL_LABEL_NAME] = lid
    return lid
  return ""


def fetch_gmail_status_for_id(target_id: str) -> dict[str, Any]:
  """Searches Gmail (including TRASH) for notification threads matching a CL/PR ID."""
  res: dict[str, Any] = {
      "thread_ids": [],
      "inbox_thread_ids": [],
      "inbox_messages": [],
      "total_msgs": 0,
      "inbox_msgs": 0,
      "trash_msgs": 0,
      "unread_msgs": 0,
      "threads": [],
  }
  if not target_id:
    return res
  data = call_gmail_mcp(
      "search_threads",
      {"query": str(target_id), "includeTrash": True, "pageSize": 10},
  )
  threads = data.get("threads") or []
  matched_threads = []
  id_re = re.compile(rf"(?<!\d){re.escape(str(target_id))}(?!\d)")
  for th in threads:
    if not isinstance(th, dict):
      continue
    msgs = th.get("messages") or []
    # Match threads where the CL/PR number is in subject/snippet from a code review mailer
    subj_or_snippet_match = any(
        id_re.search(f"{m.get('subject') or ''} {m.get('snippet') or ''}")
        for m in msgs
    )
    bot_match = any(
        any(
            b in str(m.get("sender") or "").lower()
            for b in ("gerritcodereview", "skcq-be", "github.com", "critique")
        )
        for m in msgs
    )
    if subj_or_snippet_match and bot_match:
      matched_threads.append(th)

  # Fallback if subject did not explicitly include the ID on a short thread
  if not matched_threads:
    for th in threads:
      if not isinstance(th, dict):
        continue
      msgs = th.get("messages") or []
      if any(id_re.search(str(m.get("subject") or "")) for m in msgs):
        matched_threads.append(th)

  for th in matched_threads:
    tid = str(th.get("id") or "")
    if not tid:
      continue
    res["thread_ids"].append(tid)
    msgs = th.get("messages") or []
    # Threads with more messages than returned in summary need full thread metadata
    if int(th.get("messageCount") or 0) > len(msgs):
      full_th = call_gmail_mcp(
          "get_thread",
          {"threadId": tid, "messageFormat": "METADATA_ONLY"},
      )
      full_msgs = (full_th.get("thread") or {}).get("messages")
      if isinstance(full_msgs, list) and full_msgs:
        msgs = full_msgs
    # Thread IDs mean nothing to a reader; the dashboard names a thread by who
    # sends it and links it through Gmail's own viewUrl
    senders = " ".join(str(m.get("sender") or "").lower() for m in msgs)
    source = next(
        (
            label
            for key, label in (
                ("gerritcodereview", "Gerrit"),
                ("github.com", "GitHub"),
                ("critique", "Critique"),
            )
            if key in senders
        ),
        "Mail",
    )
    res["threads"].append({
        "url": str(th.get("viewUrl") or ""),
        "source": source,
        "msgs": len(msgs),
        "last_date": str(msgs[-1].get("date") or "") if msgs else "",
    })
    th_has_inbox = False
    for m in msgs:
      res["total_msgs"] += 1
      labels = set(m.get("labelIds") or [])
      if "INBOX" in labels:
        res["inbox_msgs"] += 1
        th_has_inbox = True
        res["inbox_messages"].append({
            "id": str(m.get("id") or ""),
            "thread_id": tid,
            "date": str(m.get("date") or ""),
            "unread": "UNREAD" in labels,
        })
      if "TRASH" in labels:
        res["trash_msgs"] += 1
      if "UNREAD" in labels:
        res["unread_msgs"] += 1
    if th_has_inbox:
      res["inbox_thread_ids"].append(tid)

  return res


def archive_gmail_threads_for_id(
    target_id: str,
    thread_ids: list[str] | None = None,
    message_ids: list[str] | None = None,
) -> dict[str, Any]:
  """Removes INBOX/UNREAD labels and adds Flight Deck label to threads/messages."""
  tids = list(thread_ids or [])
  mids = list(message_ids or [])
  if not tids and not mids and target_id:
    gstat = fetch_gmail_status_for_id(target_id)
    tids = gstat.get("inbox_thread_ids") or gstat.get("thread_ids") or []
  if not tids and not mids:
    return {"ok": True, "archived": [], "archived_messages": []}
  label_id = ensure_flight_deck_gmail_label()
  if tids:
    if label_id:
      call_gmail_mcp(
          "batch_label_threads",
          {"threadIds": tids, "labelIds": [label_id]},
      )
    call_gmail_mcp(
        "batch_unlabel_threads",
        {"threadIds": tids, "labelIds": ["INBOX", "UNREAD"]},
    )
  if mids:
    if label_id:
      call_gmail_mcp(
          "batch_label_messages",
          {"messageIds": mids, "labelIds": [label_id]},
      )
    call_gmail_mcp(
        "batch_unlabel_messages",
        {"messageIds": mids, "labelIds": ["INBOX", "UNREAD"]},
    )
  return {
      "ok": True,
      "archived": tids,
      "archived_messages": mids,
      "label_id": label_id,
  }


def _parse_ts(val: Any) -> float:
  dt = parse_gerrit_or_iso_time(str(val or ""))
  return dt.timestamp() if dt else 0.0


def compute_processed_mail_cutoff_ts(record: dict[str, Any]) -> float:
  """Computes timestamp watermark up to which processed emails should be swept from INBOX."""
  status = str(record.get("status") or "").upper()
  if (
      status in ("MERGED", "ABANDONED", "CLOSED", "SUBMITTED")
      or record.get("reviewer_removed")
  ):
    return float("inf")
  cands: list[float] = []
  if record.get("conversation_id") and not record.get("chat_busy"):
    cands.append(_parse_ts(record.get("chat_last_modified")))
  last_author = str(record.get("last_msg_author") or "")
  if last_author in SELF_EMAILS or last_author in SELF_GITHUB_LOGINS:
    cands.append(_parse_ts(record.get("last_msg_date")) + 120.0)
  phase = str(record.get("phase") or "")
  if not record.get("in_my_attention", True) and phase in (
      "WAITING_ON_OTHERS",
      "POST_SUBMIT_ROLLING",
      "READY_TO_CLOSE",
  ):
    cands.append(
        max(
            _parse_ts(record.get("last_msg_date")),
            _parse_ts(record.get("status_changed_raw")),
        )
        + 120.0
    )
  if (
      record.get("self_review_state")
      and int(record.get("self_review_ps") or 0) >= int(record.get("patchset") or 1)
      and not record.get("in_my_attention", True)
  ):
    cands.append(
        max(
            _parse_ts(record.get("last_msg_date")),
            _parse_ts(record.get("status_changed_raw")),
        )
        + 120.0
    )
  triage = record.get("triage")
  if (
      isinstance(triage, dict)
      and not triage.get("is_stale")
      and not record.get("in_my_attention", True)
  ):
    cands.append(_parse_ts(triage.get("triaged_at")))
  return max(cands, default=0.0)


def sweep_processed_gmail_for_record(
    record: dict[str, Any], gstat: dict[str, Any]
) -> int:
  """Moves processed Gerrit/GitHub notification emails out of INBOX into Flight Deck."""
  if not isinstance(gstat, dict):
    return 0
  inbox_msgs = list(gstat.get("inbox_messages") or [])
  inbox_tids = list(gstat.get("inbox_thread_ids") or [])
  if not inbox_msgs and not inbox_tids:
    return 0
  tid = str(record.get("target_id") or "")
  cutoff = compute_processed_mail_cutoff_ts(record)
  if cutoff <= 0.0:
    return 0
  if cutoff == float("inf"):
    archive_gmail_threads_for_id(tid, thread_ids=inbox_tids)
    swept_count = gstat.get("inbox_msgs") or len(inbox_msgs) or len(inbox_tids)
    gstat["inbox_msgs"] = 0
    gstat["inbox_thread_ids"] = []
    gstat["inbox_messages"] = []
    gstat["unread_msgs"] = 0
    return swept_count

  to_sweep: list[dict[str, Any]] = []
  remaining: list[dict[str, Any]] = []
  for m in inbox_msgs:
    m_ts = _parse_ts(m.get("date"))
    if 0.0 < m_ts <= cutoff:
      to_sweep.append(m)
    else:
      remaining.append(m)

  if not to_sweep:
    return 0

  swept_mids = [m["id"] for m in to_sweep if m.get("id")]
  # Check if any threads have all their inbox messages swept
  remaining_tids = {m["thread_id"] for m in remaining if m.get("thread_id")}
  fully_swept_tids = [t for t in inbox_tids if t not in remaining_tids]

  archive_gmail_threads_for_id(
      tid,
      thread_ids=fully_swept_tids if fully_swept_tids else None,
      message_ids=swept_mids,
  )

  gstat["inbox_messages"] = remaining
  gstat["inbox_msgs"] = len(remaining)
  gstat["inbox_thread_ids"] = sorted(remaining_tids)
  gstat["unread_msgs"] = sum(1 for m in remaining if m.get("unread"))
  return len(swept_mids)


def load_watcher_state() -> dict[str, Any]:
  try:
    if os.path.isfile(STATE_FILE):
      with open(STATE_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
      if isinstance(data, dict):
        return data
  except Exception:
    pass
  return {"items": {}, "events": [], "last_sync": ""}


def save_watcher_state(state: dict[str, Any]) -> None:
  try:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = f"{STATE_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)
  except Exception:
    pass
  # Avoid overwriting live daemon discovery metadata from mocked tests or non-serving CLI runs
  if STATE_FILE == DEFAULT_STATE_FILE and (
      _SERVING_PORT > 0 or not os.path.isfile(FLIGHT_DECK_INFO_FILE)
  ):
    export_flight_deck_info(_SERVING_PORT or DEFAULT_PORT)


def get_pen_url(port: int) -> str:
  try:
    if os.path.isfile(PEN_URL_SH):
      res = subprocess.run(
          [PEN_URL_SH, "--protocol=HTTP", "-p", str(port)],
          capture_output=True,
          text=True,
          timeout=5,
          check=False,
      )
      if res.returncode == 0 and res.stdout.strip().startswith("https://"):
        return res.stdout.strip()
  except Exception:
    pass
  return f"http://localhost:{port}"


_FLIGHT_DECK_PEN_URL: dict[int, str] = {}


def flight_deck_pen_url(port: int = DEFAULT_PORT) -> str:
  """Returns the Flight Deck dashboard PEN URL, cached per port."""
  if port in _FLIGHT_DECK_PEN_URL:
    return _FLIGHT_DECK_PEN_URL[port]
  url = get_pen_url(port)
  if url.startswith("https://"):
    _FLIGHT_DECK_PEN_URL[port] = url
    return url
  # Avoid caching transient loopback fallbacks when pen_url.sh times out at startup
  try:
    if os.path.isfile(FLIGHT_DECK_INFO_FILE):
      with open(FLIGHT_DECK_INFO_FILE, "r", encoding="utf-8") as f:
        saved = json.load(f)
      prev_pen = str((saved or {}).get("pen_url") or "").strip()
      prev_base = re.sub(r"[?#].*$", "", prev_pen)
      if prev_base.startswith("https://"):
        return prev_base
  except Exception:
    pass
  return url


def export_flight_deck_info(port: int = DEFAULT_PORT) -> None:
  """Writes local and PEN URLs to brain so Jetski Web can embed Flight Deck."""
  try:
    ver = int(_INITIAL_SCRIPT_MTIME) if _INITIAL_SCRIPT_MTIME > 0 else 0
    ver_q = f"?v={ver}" if ver > 0 else ""
    # Include script mtime query param so embedded drawer reloads on server restart
    pen_url = f"{flight_deck_pen_url(port)}{ver_q}"
    local_url = f"http://localhost:{port}{ver_q}"
    # Record script mtime version for client auto-reload
    data = {
        "pen_url": pen_url,
        "local_url": local_url,
        "port": port,
        "ui_version": ver,
    }
    os.makedirs(os.path.dirname(FLIGHT_DECK_INFO_FILE), exist_ok=True)
    tmp = f"{FLIGHT_DECK_INFO_FILE}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp, FLIGHT_DECK_INFO_FILE)
  except Exception:
    pass


_GARDENER_PEN_URL: list[str] = []


def jetski_box_url() -> str:
  """Returns the Jetski Web base URL for this Cloudtop, same form as stop_guard.py uses."""
  # conversation:// links open only inside the Jetski app; Jetski Web opens from any browser
  return f"https://jetski.corp.google.com/box/{socket.gethostname().split('.')[0]}"


def gardener_pen_url() -> str:
  """Returns the Gardener dashboard PEN URL, resolved once per process."""
  if not _GARDENER_PEN_URL:
    port = urllib.parse.urlparse(DEFAULT_GARDENER_URL).port or 8080
    _GARDENER_PEN_URL.append(get_pen_url(port))
  return _GARDENER_PEN_URL[0]


def load_project_names() -> dict[str, str]:
  res: dict[str, str] = {}
  for pfile in glob.glob(os.path.join(PROJECTS_DIR, "*.json")):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = str(pdata.get("id") or "").strip()
      pname = cl.clean_base_title(str(pdata.get("name") or "").strip())
      # Strip activity badge suffix (" · ⟳ 1 ● 2")
      pname = re.sub(
          r"(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$", "", pname
      ).strip()
      if pid and pname:
        res[pid] = pname
    except Exception:
      continue
  return res


def _chat_candidate_rank(
    item: dict[str, Any],
) -> tuple[bool, bool, bool, str]:
  """Ranks candidate chats for one CL/PR: active > domain project > 1:1 target > newest."""
  # Prefer active chat over archived, domain project over meta tooling,
  # dedicated 1:1 chat over multi-target, then newest
  return (
      not bool(item.get("chat_archived", False)),
      item.get("project_name", "Unassigned") not in META_PROJECT_NAMES,
      bool(item.get("single_target", True)),
      str(item.get("chat_last_modified") or ""),
  )


def is_chat_busy(summary: dict[str, Any], title: str = "") -> bool:
  """Returns True when the chat is actively running a turn, subagent, task, or waiting timer."""
  # Если чат ждет ответа от человека (WAITING), он не должен считаться busy.
  ws = summary.get("waitingSteps") or summary.get("waiting_steps")
  if ws:
    return False

  # A parent chat awaiting subagents or background commands reports IDLE until wakeup
  if summary.get("status") == "CASCADE_RUN_STATUS_RUNNING":
    return True
  if summary.get("hasRunningSubagents"):
    return True
  if int(summary.get("backgroundTaskCount") or 0) > 0:
    return True
  return "⏳" in title


# Hold chat_busy briefly after agentapi send-message/start before LS reports RUNNING
CHAT_WAKE_GRACE_SEC = 15.0


def overlay_live_chat_busy(state: dict[str, Any]) -> None:
  """Refreshes `chat_busy` across cached items and bugs from the local Language Server."""
  items = state.get("items") or {}
  bugs = state.get("bugs") or {}
  if not items and not bugs:
    return
  summaries = cl._fetch_ls_summaries_for_audit() or {}
  if not summaries:
    return
  now_ts = time.time()
  for item in items.values():
    cid = str(item.get("conversation_id") or "")
    s = summaries.get(cid)
    # Recompute busy status from the latest LS trajectory snapshot without re-polling Gerrit
    if isinstance(s, dict):
      ann = s.get("annotations") or {}
      title = str(ann.get("title") or s.get("summary") or item.get("title") or "").strip()
      # Archived titles keep frozen ⏳ markers; pass empty title so only live execution counts
      busy = is_chat_busy(s, "" if ann.get("archived") else title)
      if not busy and now_ts - float(item.get("chat_woken_ts") or 0.0) < CHAT_WAKE_GRACE_SEC:
        busy = True
      item["chat_busy"] = busy
      if not busy and item.get("review_target_ps"):
        item["reviewed_ps"] = max(
            int(item.get("reviewed_ps") or 0),
            int(item.get("review_target_ps") or 0),
        )
      # If review triage is already completed and saved, mark chat not busy
      if _is_assigned_reviewer_record(item):
        tr = triage_store.load_triage_result(
            str(item.get("kind") or "gerrit"), str(item.get("target_id") or "")
        )
        cur_ps = int(item.get("patchset") or item.get("review_target_ps") or 1)
        if (
            tr
            and tr.get("review_outcome")
            and int(tr.get("triaged_patchset") or 0) >= cur_ps
        ):
          item["reviewed_ps"] = max(int(item.get("reviewed_ps") or 0), cur_ps)
          if (
              s.get("status") == "CASCADE_RUN_STATUS_IDLE"
              and not s.get("hasRunningSubagents")
              and not int(s.get("backgroundTaskCount") or 0) > 0
          ):
            item["chat_busy"] = False
  # Keep bug cards and their linked CL subtask rows synchronized with live LS chat activity
  for bug in bugs.values():
    if not isinstance(bug, dict):
      continue
    bcid = str(bug.get("conversation_id") or "")
    bs = summaries.get(bcid)
    # Refresh the parent bug conversation busy state and wake grace window
    if isinstance(bs, dict):
      ann = bs.get("annotations") or {}
      btitle = str(ann.get("title") or bs.get("summary") or bug.get("chat_title") or "").strip()
      bbusy = is_chat_busy(bs, "" if ann.get("archived") else btitle)
      # Preserve busy indicator immediately after agentapi send-message before LS reports RUNNING
      if not bbusy and now_ts - float(bug.get("chat_woken_ts") or 0.0) < CHAT_WAKE_GRACE_SEC:
        bbusy = True
      bug["chat_busy"] = bbusy
    # Refresh separate subtask CL chats independently so non-shared review/CL chats stay live
    for sub in bug.get("linked_cls") or []:
      if not isinstance(sub, dict):
        continue
      scid = str(sub.get("conversation_id") or "")
      ss = summaries.get(scid)
      if isinstance(ss, dict):
        sann = ss.get("annotations") or {}
        stitle = str(sann.get("title") or ss.get("summary") or "").strip()
        sub["chat_busy"] = is_chat_busy(ss, "" if sann.get("archived") else stitle)


def extract_cl_ids_with_hosts(text: str) -> dict[str, str]:
  """Extracts all Gerrit/Piper CL IDs from a title, preserving Gerrit host hints.

  Handles multi-CL lists separated by slashes, commas, ampersands, or spaces
  without colliding with bug numbers like b/557269199.
  """
  cls_hosts: dict[str, str] = {}
  for m in RE_CL_ID_GROUP.finditer(text):
    pref = m.group("prefix").lower()
    # Map shortlink and full review URL prefixes to their Gerrit host
    hint = (
        GERRIT_CHROMIUM_HOST
        if ("crrev" in pref or "chromium-review" in pref)
        else (
            GERRIT_SKIA_HOST
            if ("skrev" in pref or "skia-review" in pref)
            else ""
        )
    )
    first = m.group("first")
    if first not in cls_hosts or (hint and not cls_hosts[first]):
      cls_hosts[first] = hint
    rest = m.group("rest")
    if rest:
      for extra in re.finditer(r"\b(\d{6,10})\b", rest):
        eid = extra.group(1)
        if eid not in cls_hosts or (hint and not cls_hosts[eid]):
          cls_hosts[eid] = hint
  return cls_hosts


def extract_pr_ids(text: str) -> list[str]:
  """Extracts GitHub PR numbers from a title, including grouped PR lists."""
  prs: list[str] = []
  for m in RE_PR_ID_GROUP.finditer(text):
    prs.append(m.group("first"))
    rest = m.group("rest")
    if rest:
      for extra in re.finditer(r"\b(\d{5,7})\b", rest):
        prs.append(extra.group(1))
  # Deduplicate while preserving first appearance order
  return list(dict.fromkeys(prs))


def extract_state_file_targets(
    project_id: str,
    conv_id: str,
    roadmaps_dir: str | None = None,
) -> tuple[dict[str, str], list[str]]:
  """Extracts follow-up fix CLs and PRs recorded in a chat's chain state file."""
  if not project_id or not conv_id:
    return {}, []
  rdir = ROADMAPS_DIR if roadmaps_dir is None else roadmaps_dir
  state_path = os.path.join(rdir, project_id, "state", f"{conv_id}.md")
  if not os.path.isfile(state_path):
    return {}, []
  extra_cls: dict[str, str] = {}
  extra_prs: list[str] = []
  in_fix_section = False
  try:
    with open(state_path, "r", encoding="utf-8") as f:
      for raw_line in f:
        line = raw_line.strip()
        if not line:
          continue
        # Extract from main H1 header (e.g. "# State: ... fix CL 1411716")
        if line.startswith("# ") and not line.startswith("##"):
          for cid_str, h in extract_cl_ids_with_hosts(line).items():
            if cid_str not in extra_cls or (h and not extra_cls[cid_str]):
              extra_cls[cid_str] = h
          for pr_str in extract_pr_ids(line):
            if pr_str not in extra_prs:
              extra_prs.append(pr_str)
          continue
        # Detect start of a dedicated follow-up or fix section
        if line.startswith("##"):
          in_fix_section = bool(RE_STATE_FIX_SECTION.match(line))
          if in_fix_section:
            for cid_str, h in extract_cl_ids_with_hosts(line).items():
              if cid_str not in extra_cls or (h and not extra_cls[cid_str]):
                extra_cls[cid_str] = h
            for pr_str in extract_pr_ids(line):
              if pr_str not in extra_prs:
                extra_prs.append(pr_str)
          continue
        # Extract bullet points naming fix CLs or PRs while inside the fix section
        if in_fix_section and (line.startswith("-") or line.startswith("*")):
          low = line.lower()
          if any(k in low for k in ("incident:", "spin-off", "conversation://")):
            continue
          for cid_str, h in extract_cl_ids_with_hosts(line).items():
            if cid_str not in extra_cls or (h and not extra_cls[cid_str]):
              extra_cls[cid_str] = h
          for pr_str in extract_pr_ids(line):
            if pr_str not in extra_prs:
              extra_prs.append(pr_str)
        # Match git config branch.<name>.gerritissue markers recorded in state notes
        for gm in RE_STATE_GERRIT_ISSUE.finditer(line):
          gid = gm.group(1)
          if gid not in extra_cls:
            extra_cls[gid] = ""
  except OSError:
    pass
  return extra_cls, extra_prs


def extract_bug_refs_from_text(
    text: str,
    default_tracker: str = "",
    default_repo: str = "flutter/flutter",
) -> list[str]:
  """Extracts namespaced bug keys (`buganizer:<id>`, `gh_issue:<owner>/<repo>#<n>`)."""
  if not text:
    return []
  found: list[str] = []

  def _add(k: str) -> None:
    if k and k not in found:
      found.append(k)

  for m in RE_BUGANIZER_EXPLICIT.finditer(text):
    _add(f"buganizer:{m.group(1)}")
  for m in RE_GH_FLUTTER_ISSUE_EXPLICIT.finditer(text):
    _add(f"gh_issue:flutter/flutter#{m.group(1)}")

  if default_tracker == "buganizer":
    # Bare >=6-digit numbers on Gerrit/Piper Bug:/Fixed: lines refer to Buganizer/crbug
    for tag_m in RE_BUG_TAG.finditer(text):
      line_val = tag_m.group(1)
      for bm in re.finditer(r"(?<![#\w/])(\d{6,10})\b", line_val):
        _add(f"buganizer:{bm.group(1)}")
  elif default_tracker == "github":
    # Match closing keywords like Fixes #193731 while ignoring PR #195122 references
    repo_norm = str(default_repo or "").strip()
    if repo_norm.startswith("github.com/"):
      repo_norm = repo_norm[len("github.com/") :]
    repo_norm = repo_norm or "flutter/flutter"
    kw_re = (
        RE_GH_CLOSING_KEYWORD
        if repo_norm == "flutter/flutter"
        else RE_GH_CLOSING_KEYWORD_ANY_REPO
    )
    for km in kw_re.finditer(text):
      _add(f"gh_issue:{repo_norm}#{km.group(1)}")
  return found


def discover_bug_chats(
    include_archived: bool = False,
) -> dict[str, dict[str, Any]]:
  """Scans Jetski conversations for bug shortlinks (`b/`, `crbug/`, `flutter#`)."""
  summaries = cl._fetch_ls_summaries_for_audit() or {}
  proj_names = load_project_names()
  candidates: list[dict[str, Any]] = []

  for cid, s in summaries.items():
    if not isinstance(s, dict):
      continue
    ann = s.get("annotations") or {}
    is_archived = bool(ann.get("archived"))
    if is_archived and not include_archived:
      continue
    meta = s.get("trajectoryMetadata") or {}
    if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
      continue
    source_meta = meta.get("sourceMetadata") or {}
    if (
        source_meta.get("tool")
        or source_meta.get("hideFromConversationList")
        or source_meta.get("hide_from_conversation_list")
    ):
      continue
    title = str(ann.get("title") or s.get("summary") or "").strip()
    if not is_archived and "⦿" not in title and "⏳" not in title:
      continue
    pid = str(meta.get("projectId") or "").strip()
    pname = proj_names.get(pid, "Unassigned")
    # Skip meta, digest, and personal projects while keeping outside-of-project ('Unassigned') chats
    if pname != "Unassigned" and pname in META_PROJECT_NAMES:
      continue
    bug_keys = extract_bug_refs_from_text(title)
    if not bug_keys:
      continue
    chat_busy = is_chat_busy(s, "" if is_archived else title)
    last_mod = str(meta.get("lastModifiedTime") or meta.get("createdAt") or "")
    single_bug = len(bug_keys) == 1
    for bkey in bug_keys:
      candidates.append({
          "bug_key": bkey,
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": single_bug,
          "chat_busy": chat_busy,
          "chat_archived": is_archived,
      })

  best: dict[str, dict[str, Any]] = {}
  for cand in candidates:
    bkey = cand["bug_key"]
    prev = best.get(bkey)
    if prev is None or _chat_candidate_rank(cand) > _chat_candidate_rank(prev):
      best[bkey] = cand
  return best


def discover_active_cl_chats(
    include_archived: bool = False,
) -> list[dict[str, Any]]:
  """Scans conversations and maps them to CLs or GitHub PRs."""
  summaries = cl._fetch_ls_summaries_for_audit() or {}
  proj_names = load_project_names()
  discovered: list[dict[str, Any]] = []

  for cid, s in summaries.items():
    if not isinstance(s, dict):
      continue
    ann = s.get("annotations") or {}
    is_archived = bool(ann.get("archived"))
    if is_archived and not include_archived:
      continue
    meta = s.get("trajectoryMetadata") or {}
    if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
      continue
    source_meta = meta.get("sourceMetadata") or {}
    if (
        source_meta.get("tool")
        or source_meta.get("hideFromConversationList")
        or source_meta.get("hide_from_conversation_list")
    ):
      continue
    title = str(ann.get("title") or s.get("summary") or "").strip()
    # Active chats require ⦿ (needs user) or ⏳ (waiting timer/external)
    if not is_archived and "⦿" not in title and "⏳" not in title:
      continue
    # Archived titles keep frozen ⏳ markers; pass empty title so only live execution counts
    chat_busy = is_chat_busy(s, "" if is_archived else title)

    pid = str(meta.get("projectId") or "").strip()
    pname = proj_names.get(pid, "Unassigned")
    # Ignore meta-chats in Flight Deck that mention a CL/PR as a bug example
    if pname in EXCLUDED_WATCHER_PROJECTS:
      continue
    last_mod = str(meta.get("lastModifiedTime") or meta.get("createdAt") or "")

    cls_hosts = extract_cl_ids_with_hosts(title)
    cls_found = list(cls_hosts.keys())
    prs_found = extract_pr_ids(title)
    # Allow bug-titled chats through so state-file fix CLs/PRs are still discovered
    bugs_found = extract_bug_refs_from_text(title)
    if not cls_found and not prs_found and not bugs_found:
      continue

    # Discover additional follow-up CLs/PRs recorded in the chat state file
    extra_cls, extra_prs = extract_state_file_targets(pid, cid)
    state_cls = {k: v for k, v in extra_cls.items() if k not in cls_hosts}
    state_prs = [p for p in extra_prs if p not in prs_found]
    # Pure bug chats without any fix CL/PR yet belong to discover_bug_chats only
    if not cls_found and not prs_found and not state_cls and not state_prs:
      continue

    # A slash list like "PR #194466/#194475" or multiple IDs means the chat is not 1:1 with one CL
    multi_slash = bool(
        re.search(
            r"(?:PR\s*#?\d+|(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/\d+|CL\s+\d+)"
            r"\s*[/,&+]\s*#?\d+",
            title,
            re.IGNORECASE,
        )
    )
    # Multiple CL/PR targets mean the chat does not represent a single 1:1 CL;
    # a 1-bug chat whose state file records its single fix CL/PR remains 1:1
    total_cl_targets = (
        len(cls_found) + len(prs_found) + len(state_cls) + len(state_prs)
    )
    has_multiple = total_cl_targets > 1 or len(bugs_found) > 1
    single_target = not has_multiple and not multi_slash
    state_single_target = (
        single_target
        and not cls_found
        and not prs_found
        and len(bugs_found) == 1
        and total_cl_targets == 1
    )

    for cl_id in cls_found:
      kind = "piper" if len(cl_id) >= 9 else "gerrit"
      discovered.append({
          "item_key": f"{kind}:{cl_id}",
          "kind": kind,
          "target_id": cl_id,
          "preferred_host": cls_hosts.get(cl_id, ""),
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": single_target,
          "chat_busy": chat_busy,
          "chat_archived": is_archived,
      })
    # Append state-file CLs; keep single_target only when a 1-bug chat owns 1 fix CL
    for cl_id, s_host in state_cls.items():
      kind = "piper" if len(cl_id) >= 9 else "gerrit"
      discovered.append({
          "item_key": f"{kind}:{cl_id}",
          "kind": kind,
          "target_id": cl_id,
          "preferred_host": s_host,
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": state_single_target,
          "chat_busy": chat_busy,
          "chat_archived": is_archived,
      })
    for pr_id in prs_found:
      discovered.append({
          "item_key": f"github:{pr_id}",
          "kind": "github",
          "target_id": pr_id,
          "preferred_host": "",
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": single_target,
          "chat_busy": chat_busy,
          "chat_archived": is_archived,
      })
    # Append state-file PRs; keep single_target only when a 1-bug chat owns 1 fix PR
    for pr_id in state_prs:
      discovered.append({
          "item_key": f"github:{pr_id}",
          "kind": "github",
          "target_id": pr_id,
          "preferred_host": "",
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": state_single_target,
          "chat_busy": chat_busy,
          "chat_archived": is_archived,
      })

  # Collapse multiple chats referencing the same CL/PR to one canonical owner
  best_by_key: dict[str, dict[str, Any]] = {}
  for item in discovered:
    ikey = item["item_key"]
    prev = best_by_key.get(ikey)
    # Replace when this is the first candidate or outranks the existing candidate
    if prev is None or _chat_candidate_rank(item) > _chat_candidate_rank(prev):
      best_by_key[ikey] = item

  deduped = list(best_by_key.values())
  # Keep newest chat modification first for deterministic dashboard sort
  deduped.sort(key=lambda x: x["chat_last_modified"], reverse=True)
  return deduped


def attach_archived_chats_to_open_changes(
    remote_unchatted: list[dict[str, Any]],
    all_chats: list[dict[str, Any]] | None = None,
) -> None:
  """Links open remote changes with no active chat to their archived chat if one exists."""
  if not remote_unchatted:
    return
  candidates = (
      all_chats
      if all_chats is not None
      else discover_active_cl_chats(include_archived=True)
  )
  by_key: dict[str, dict[str, Any]] = {
      c["item_key"]: c for c in candidates if c.get("conversation_id")
  }
  for item in remote_unchatted:
    if item.get("conversation_id"):
      continue
    ikey = item.get("item_key")
    matched = by_key.get(ikey)
    if matched:
      item["conversation_id"] = matched["conversation_id"]
      item["title"] = matched["title"]
      item["project_id"] = matched["project_id"]
      item["project_name"] = matched["project_name"]
      item["chat_last_modified"] = matched.get("chat_last_modified", "")
      item["chat_busy"] = matched.get("chat_busy", False)
      item["chat_archived"] = matched.get("chat_archived", True)
      item["single_target"] = matched.get("single_target", True)


_RELATED_CLS_CACHE: dict[str, list[str]] = {}
_RELATED_STACK_ORDER_CACHE: dict[str, list[str]] = {}
_RELATED_RAW_CHANGES_CACHE: dict[str, list[dict[str, Any]]] = {}


# Read public Gerrit REST JSON and fall back to authenticated /a/ on HTTP 403/429 rate limits
def _gerrit_http_get_json(
    host: str, path_and_query: str, timeout: int = 8
) -> Any:
  rel = path_and_query if path_and_query.startswith("/") else f"/{path_and_query}"
  anon_url = f"https://{host}{rel}"
  req = urllib.request.Request(
      anon_url, headers={"User-Agent": "CLFlightDeck/1.0"}
  )
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      raw = resp.read().decode("utf-8", errors="replace")
  except urllib.error.HTTPError as e:
    # Only anonymous rate limits or quota blocks warrant retrying via /a/ with LUCI OAuth
    if e.code not in (403, 429):
      raise
    tok = canary_advisor._gerrit_luci_token()
    if not tok:
      raise
    auth_url = f"https://{host}/a{rel}"
    auth_req = urllib.request.Request(
        auth_url,
        headers={
            "User-Agent": "CLFlightDeck/1.0",
            "Authorization": f"Bearer {tok}",
        },
    )
    try:
      with urllib.request.urlopen(auth_req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e2:
      # Refresh cached OAuth token once when Gerrit /a/ rejects an expired Bearer token
      if e2.code not in (400, 401, 403):
        raise
      canary_advisor._LUCI_TOKEN_CACHE = ("", 0.0)
      fresh = canary_advisor._gerrit_luci_token(force_refresh=True)
      if not fresh or fresh == tok:
        raise
      retry_req = urllib.request.Request(
          auth_url,
          headers={
              "User-Agent": "CLFlightDeck/1.0",
              "Authorization": f"Bearer {fresh}",
          },
      )
      with urllib.request.urlopen(retry_req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
  # Strip Gerrit XSSI magic prefix before decoding JSON
  if raw.startswith(")]}'"):
    raw = raw[4:]
  return json.loads(raw)


def fetch_gerrit_related_cl_ids(host: str, cl_id: str) -> list[str]:
  """Fetches related change IDs from Gerrit's revision chain endpoint."""
  cache_key = f"{host}:{cl_id}"
  if cache_key in _RELATED_CLS_CACHE:
    return _RELATED_CLS_CACHE[cache_key]

  related_ids: list[str] = []
  try:
    data = _gerrit_http_get_json(
        host, f"/changes/{cl_id}/revisions/current/related", timeout=8
    )
    changes = [ch for ch in (data.get("changes") or []) if isinstance(ch, dict)]
    # Retain full revision chain including MERGED/ABANDONED entries for companion parent matching
    _RELATED_RAW_CHANGES_CACHE[cache_key] = changes
    # Build graph of active (not MERGED/ABANDONED) changes in the revision stack
    active_changes = [
        ch
        for ch in changes
        if str(ch.get("status") or "").upper() not in ("MERGED", "ABANDONED")
    ]
    # Record bottom-up stack order across all active changes in the revision chain
    ordered_stack_nums: list[str] = []
    for ch in reversed(active_changes):
      num = str(ch.get("_change_number") or "").strip()
      if num and num not in ordered_stack_nums:
        ordered_stack_nums.append(num)
    if ordered_stack_nums:
      for s_num in ordered_stack_nums:
        _RELATED_STACK_ORDER_CACHE[f"{host}:{s_num}"] = ordered_stack_nums
    else:
      _RELATED_STACK_ORDER_CACHE[cache_key] = [str(cl_id)]

    by_commit: dict[str, dict[str, Any]] = {}
    children_of: dict[str, list[str]] = {}
    target_commit = ""
    for ch in active_changes:
      csha = str(((ch.get("commit") or {}).get("commit")) or "").strip()
      if not csha:
        continue
      by_commit[csha] = ch
      if str(ch.get("_change_number") or "").strip() == str(cl_id):
        target_commit = csha
      for p in (ch.get("commit") or {}).get("parents") or []:
        psha = str(p.get("commit") or "").strip()
        if psha:
          children_of.setdefault(psha, []).append(csha)

    if target_commit and target_commit in by_commit:
      visited: set[str] = set()
      stack = [target_commit]
      while stack:
        cur = stack.pop()
        if cur in visited or cur not in by_commit:
          continue
        visited.add(cur)
        for p in (by_commit[cur].get("commit") or {}).get("parents") or []:
          psha = str(p.get("commit") or "").strip()
          if psha in by_commit and psha not in visited:
            stack.append(psha)
        for ch_sha in children_of.get(cur, []):
          if ch_sha in by_commit and ch_sha not in visited:
            stack.append(ch_sha)
      for ch in active_changes:
        csha = str(((ch.get("commit") or {}).get("commit")) or "").strip()
        num = str(ch.get("_change_number") or "").strip()
        if csha in visited and num and num != str(cl_id) and num not in related_ids:
          related_ids.append(num)
    else:
      for ch in active_changes:
        num = str(ch.get("_change_number") or "").strip()
        if num and num != str(cl_id) and num not in related_ids:
          related_ids.append(num)
  except Exception:
    # Fail open when Gerrit /related is unreachable or change has no revisions
    if cache_key not in _RELATED_STACK_ORDER_CACHE:
      _RELATED_STACK_ORDER_CACHE[cache_key] = [str(cl_id)]

  _RELATED_CLS_CACHE[cache_key] = related_ids
  return related_ids


def fetch_gerrit_stack_order_ids(host: str, cl_id: str) -> list[str]:
  """Returns change IDs in the revision stack ordered bottom-up (base parent to tip)."""
  cache_key = f"{host}:{cl_id}"
  if cache_key in _RELATED_STACK_ORDER_CACHE:
    return _RELATED_STACK_ORDER_CACHE[cache_key]
  fetch_gerrit_related_cl_ids(host, cl_id)
  return _RELATED_STACK_ORDER_CACHE.get(cache_key, [str(cl_id)])


def _resolve_gerrit_stack_companion_parent(
    host: str,
    tid: str,
    subj: str,
    info: dict[str, Any],
    fetched_cache: dict[str, dict[str, Any]],
    related_ids: Iterable[str],
) -> tuple[str, int]:
  """Resolves the direct non-self parent change key and base PS for a stacked self CL."""
  # A non-WIP self CL already sent out to reviewers without companion markers is standalone
  if (
      "in_development" in info
      and not info.get("is_wip")
      and not info.get("is_draft")
      and not info.get("in_development")
      and "companion" not in subj.lower()
  ):
    return "", 0
  # SkoreText feature CLs are never temporary review companions for external changes
  if subj.lstrip().startswith("[SkoreText]") and "companion" not in subj.lower():
    return "", 0

  has_companion_kw = "companion" in subj.lower()
  # Index commit SHAs -> (change_num, is_self, is_comp_subj, base_ps, parent_sha)
  sha_to_entry: dict[str, tuple[str, bool, bool, int, str]] = {}
  for fk, finfo in fetched_cache.items():
    if not fk.startswith("gerrit:") or not isinstance(finfo, dict):
      continue
    fnum = fk.split(":", 1)[1]
    fis_self = bool(finfo.get("is_self", False))
    fis_comp = "companion" in str(finfo.get("subject") or "").lower()
    fpsha = str(finfo.get("parent_commit") or "").strip()
    f_merged = str(finfo.get("status") or "").upper() == "MERGED"
    revs = finfo.get("revision_ps_by_sha") or {}
    # CQ creates a new patchset on submit, so the final revision of a merged CL is main
    last_ps = max((int(ps or 0) for ps in revs.values()), default=0) if f_merged else 0
    for rsha, rps in revs.items():
      if rsha and not (f_merged and int(rps or 0) == last_ps):
        sha_to_entry[str(rsha)] = (fnum, fis_self, fis_comp, int(rps or 0), fpsha)

  start_parent_sha = str(info.get("parent_commit") or "").strip()
  self_emails_lc = {e.lower() for e in SELF_EMAILS}
  raw_changes = _RELATED_RAW_CHANGES_CACHE.get(f"{host}:{tid}") or []
  for ch in raw_changes:
    cnum = str(ch.get("_change_number") or "").strip()
    commit_obj = ch.get("commit") or {}
    csha = str(commit_obj.get("commit") or "").strip()
    parents = commit_obj.get("parents") or []
    psha = (
        str(parents[0].get("commit") or "").strip()
        if parents and isinstance(parents[0], dict)
        else ""
    )
    f_override = fetched_cache.get(f"gerrit:{cnum}")
    if isinstance(f_override, dict) and "is_self" in f_override:
      cis_self = bool(f_override["is_self"])
    else:
      a_email = (
          str(((commit_obj.get("author") or {}).get("email")) or "")
          .strip()
          .lower()
      )
      cis_self = a_email in self_emails_lc
    csubj = str(
        (f_override.get("subject") if isinstance(f_override, dict) else None)
        or commit_obj.get("subject")
        or ""
    )
    cis_comp = "companion" in csubj.lower()
    rev_num = int(ch.get("_revision_number") or 0)
    c_status = str(
        (f_override.get("status") if isinstance(f_override, dict) else None)
        or ch.get("status")
        or ""
    ).upper()
    cur_rev_num = int(ch.get("_current_revision_number") or rev_num or 0)
    # Skip the submitted tip revision of a merged CL so main-based stacks stop walking here
    if c_status == "MERGED" and rev_num == cur_rev_num:
      continue
    if csha and csha not in sha_to_entry:
      sha_to_entry[csha] = (cnum, cis_self, cis_comp, rev_num, psha)
    if cnum == tid and not start_parent_sha and psha:
      start_parent_sha = psha

  # Walk parent_commit across any intermediate self-companions to find the non-self parent
  if start_parent_sha:
    cur_sha = start_parent_sha
    visited: set[str] = set()
    self_fallback: tuple[str, int] = ("", 0)
    for _ in range(32):
      if not cur_sha or cur_sha in visited or cur_sha not in sha_to_entry:
        break
      visited.add(cur_sha)
      cnum, cis_self, cis_comp, rev_num, next_psha = sha_to_entry[cur_sha]
      if cnum == tid:
        break
      if not cis_self:
        return f"gerrit:{cnum}", rev_num
      # Record non-companion self parent only as fallback when no external ancestor exists
      if has_companion_kw and not cis_comp and not self_fallback[0]:
        self_fallback = (f"gerrit:{cnum}", rev_num)
      cur_sha = next_psha
    return self_fallback

  # Fall back to active related IDs only when parent_commit metadata is absent
  self_rel_fallback = ""
  for rel_id in related_ids:
    if str(rel_id) == tid:
      continue
    cand_key = f"gerrit:{rel_id}"
    rel_info = fetched_cache.get(cand_key) or {}
    if not bool(rel_info.get("is_self", False)):
      return cand_key, 0
    if (
        has_companion_kw
        and not self_rel_fallback
        and "companion" not in str(rel_info.get("subject") or "").lower()
    ):
      self_rel_fallback = cand_key
  return self_rel_fallback, 0


def link_companion_changes_to_parent_chats(
    active_discovered: list[dict[str, Any]],
    fetched_cache: dict[str, dict[str, Any]],
    all_chat_candidates: list[dict[str, Any]] | None = None,
) -> None:
  """Links companion CLs or PRs to their parent change and chat."""
  # Index known chatted changes from active discovered and any archived chats
  known_chats: dict[str, dict[str, Any]] = {
      d["item_key"]: d
      for d in active_discovered
      if d.get("conversation_id")
  }
  if all_chat_candidates is not None:
    for c in all_chat_candidates:
      ckey = c.get("item_key")
      if ckey and c.get("conversation_id") and ckey not in known_chats:
        known_chats[ckey] = c
  by_item_key: dict[str, dict[str, Any]] = {
      d["item_key"]: d for d in active_discovered
  }

  for d in active_discovered:
    ikey = d.get("item_key", "")
    info = fetched_cache.get(ikey) or {}
    subj = str(info.get("subject") or d.get("title") or "")
    d_is_self = bool(info.get("is_self", False))
    parent: dict[str, Any] | None = None
    parent_key = ""
    base_ps_hint = 0
    is_comp = False

    if d.get("kind") == "gerrit":
      tid = str(d.get("target_id") or "")
      host = str(
          d.get("preferred_host")
          or info.get("host")
          or GERRIT_SKIA_HOST
      )
      m_cl = RE_COMPANION_CL_REF.search(subj) or RE_COMPANION_CL_REF.search(
          str(info.get("commit_message") or "")
      )
      if m_cl and d_is_self:
        cand_key = f"gerrit:{m_cl.group(1)}"
        parent = known_chats.get(cand_key) or by_item_key.get(cand_key)
        parent_key = cand_key
        is_comp = True
      else:
        related = fetch_gerrit_related_cl_ids(host, tid)
        if d_is_self:
          comp_cand_key, comp_ps = _resolve_gerrit_stack_companion_parent(
              host, tid, subj, info, fetched_cache, related
          )
          cand_obj = (
              known_chats.get(comp_cand_key) or by_item_key.get(comp_cand_key)
              if comp_cand_key
              else None
          )
          # Prevent cyclic A <-> B companion links
          if comp_cand_key and not (
              cand_obj and cand_obj.get("companion_to") == ikey
          ):
            parent = cand_obj
            parent_key = comp_cand_key
            base_ps_hint = comp_ps
            is_comp = True
        # Inherit an active stack sibling's chat across multi-CL self-authored stacks
        if not is_comp and d_is_self and not d.get("conversation_id"):
          for rel_id in related:
            if str(rel_id) == tid:
              continue
            cand_key = f"gerrit:{rel_id}"
            cand_parent = known_chats.get(cand_key) or by_item_key.get(cand_key)
            if not cand_parent or not cand_parent.get("conversation_id"):
              continue
            rel_info = fetched_cache.get(cand_key) or {}
            if str(rel_info.get("status") or "").upper() in (
                "MERGED",
                "ABANDONED",
            ):
              continue
            if bool(rel_info.get("is_self", False)):
              parent = cand_parent
              break
    elif d.get("kind") == "github":
      m = RE_COMPANION_PR_REF.search(subj) or RE_COMPANION_PR_REF.search(
          str(info.get("commit_message") or "")
      )
      branch_m = RE_COMPANION_BRANCH.match(
          str(info.get("head_ref") or info.get("head_ref_name") or "")
      )
      ref_id = m.group(1) if m else (branch_m.group(1) if branch_m else "")
      if ref_id and d_is_self:
        cand_key = f"github:{ref_id}"
        parent = known_chats.get(cand_key) or by_item_key.get(cand_key)
        parent_key = cand_key
        is_comp = True

    if is_comp and parent_key:
      d["companion_to"] = parent_key
      d["is_companion"] = True
      if base_ps_hint > 0:
        d["companion_base_ps_hint"] = base_ps_hint

    if parent and parent.get("conversation_id"):
      if not d.get("conversation_id"):
        d["conversation_id"] = parent["conversation_id"]
        d["title"] = parent.get("title", "")
        d["project_id"] = parent.get("project_id", "")
        d["project_name"] = parent.get("project_name", "")
        d["chat_last_modified"] = parent.get("chat_last_modified", "")
        d["chat_busy"] = parent.get("chat_busy", False)
        d["chat_archived"] = parent.get("chat_archived", False)
      d["single_target"] = False
      # Keep parent single_target intact when attaching a companion to a 1:1 review chat
      if not is_comp:
        parent["single_target"] = False

    # Propagate SkoreText project across unchatted changes in the same open stack
    if d.get("kind") == "gerrit" and not d.get("conversation_id"):
      host = str(
          d.get("preferred_host")
          or info.get("host")
          or GERRIT_SKIA_HOST
      )
      related = fetch_gerrit_related_cl_ids(host, d.get("target_id", ""))
      for rel_id in related:
        cand_key = f"gerrit:{rel_id}"
        rel_item = by_item_key.get(cand_key)
        if rel_item and rel_item.get("project_name") == "SkoreText":
          if d.get("project_name") in ("SkParagraph", "Skia", "Unassigned", ""):
            d["project_id"] = rel_item.get("project_id", "")
            d["project_name"] = "SkoreText"
            break


def _normalize_item_key_ref(raw: str, default_kind: str = "gerrit") -> str:
  """Normalizes an item key or numeric CL/PR ID into `<kind>:<id>`."""
  s = str(raw or "").strip()
  if not s:
    return ""
  if ":" in s:
    return s
  return f"{default_kind}:{s}"


def load_manual_fix_links(path: str = MANUAL_FIX_LINKS_FILE) -> dict[str, str]:
  """Loads optional manual fix-to-broken CL overrides from JSON."""
  if not os.path.isfile(path):
    return {}
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    if not isinstance(data, dict):
      return {}
    out: dict[str, str] = {}
    for k, v in data.items():
      nk = _normalize_item_key_ref(str(k))
      kind = nk.split(":", 1)[0] if ":" in nk else "gerrit"
      nv = _normalize_item_key_ref(str(v), default_kind=kind)
      if nk and nv:
        out[nk] = nv
    return out
  except Exception:
    return {}


def extract_bug_ids(
    subject: str,
    commit_message: str = "",
    topic: str = "",
    hashtags: list[str] | tuple[str, ...] = (),
) -> set[str]:
  """Extracts normalized numeric bug IDs from commit footers, subject, topic, and hashtags."""
  bugs: set[str] = set()
  for m in RE_BUG_TAG.finditer(commit_message or ""):
    line_val = m.group(1)
    for tm in RE_BUG_TOKEN.finditer(line_val):
      bugs.add(tm.group(1))
    for bm in re.finditer(r"\b(\d{5,})\b", line_val):
      bugs.add(bm.group(1))
  blob = "\n".join(
      [subject or "", commit_message or "", topic or ""]
      + [str(h) for h in (hashtags or ())]
  )
  for tm in RE_BUG_TOKEN.finditer(blob):
    bugs.add(tm.group(1))
  return bugs


def extract_bot_mentions_and_failure_cues(
    subject: str, commit_message: str = ""
) -> tuple[set[str], set[tuple[str, str]], bool]:
  """Extracts full Skia bot names, abbreviated bot pairs, and failure cues from text."""
  blob = f"{subject or ''}\n{commit_message or ''}"
  full_bots = {m.group(1).rstrip(".,;:") for m in RE_SKIA_BOT_NAME.finditer(blob)}
  pairs: set[tuple[str, str]] = set()
  # Expand shorthand like "Test-Mac14 and Test-Mac15 ...-Debug-All-NativeFonts_Fontations"
  for pm in RE_SKIA_BOT_PAIR.finditer(blob):
    b1, b2, suffix = pm.group(1), pm.group(2), pm.group(3).rstrip(".,;:")
    pairs.add((b1, suffix))
    pairs.add((b2, suffix))
  has_cue = bool(RE_FAILURE_CUE.search(blob))
  return full_bots, pairs, has_cue


def bot_matches_mentions(
    bot_name: str, full_bots: set[str], pairs: set[tuple[str, str]]
) -> bool:
  """Returns True when `bot_name` matches a full bot token or an abbreviated prefix/suffix pair."""
  if not bot_name:
    return False
  if bot_name in full_bots:
    return True
  for fb in full_bots:
    if len(fb) >= 10 and bot_name.startswith(fb):
      return True
  for pref, suf in pairs:
    if bot_name.startswith(pref) and bot_name.endswith(suf):
      return True
  return False


def extract_test_identifiers(
    subject: str,
    commit_message: str = "",
    files: list[str] | tuple[str, ...] = (),
) -> tuple[set[str], set[str]]:
  """Extracts touched test file paths and unit test symbol names from a change."""
  test_files = {
      str(f)
      for f in (files or ())
      if "test" in str(f).lower() or str(f).startswith("tests/")
  }
  blob = f"{subject or ''}\n{commit_message or ''}"
  test_symbols = {m.group(1) for m in RE_TEST_SYMBOL.finditer(blob)}
  return test_files, test_symbols


def _collect_broken_bots_from_item(item: dict[str, Any]) -> list[dict[str, str]]:
  """Returns deduplicated blamed post-submit bot records from a merged item."""
  ps = item.get("post_submit") or {}
  bots: list[dict[str, str]] = []
  seen: set[str] = set()
  for b in (item.get("broken_bots") or []) + (ps.get("broken_bots") or []):
    if isinstance(b, dict) and b.get("task_name"):
      tname = str(b["task_name"])
      if tname not in seen:
        seen.add(tname)
        bots.append({
            "task_name": tname,
            "task_url": str(b.get("task_url") or ""),
            "swarming_url": str(b.get("swarming_url") or ""),
        })
  for inc in ps.get("incidents") or []:
    if not isinstance(inc, dict) or inc.get("verdict") != "blamed":
      continue
    for a in inc.get("affected_tasks") or []:
      if isinstance(a, dict) and a.get("task_name"):
        tname = str(a["task_name"])
        if tname not in seen:
          seen.add(tname)
          bots.append({
              "task_name": tname,
              "task_url": str(a.get("task_url") or inc.get("task_url") or ""),
              "swarming_url": str(
                  a.get("swarming_url") or inc.get("swarming_url") or ""
              ),
          })
    tname = str(inc.get("task_name") or "")
    if tname and tname not in seen:
      seen.add(tname)
      bots.append({
          "task_name": tname,
          "task_url": str(inc.get("task_url") or ""),
          "swarming_url": str(inc.get("swarming_url") or ""),
      })
  return bots


def detect_fix_for_pairs(
    items_by_key: dict[str, dict[str, Any]],
    manual_links: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
  """Detects fix CLs/PRs and links each to the merged CL/PR whose breakage it addresses."""
  manual = (
      manual_links if manual_links is not None else load_manual_fix_links()
  )
  merged_candidates: dict[str, dict[str, Any]] = {}
  for ikey, item in items_by_key.items():
    if not isinstance(item, dict):
      continue
    if str(item.get("status") or "").upper() == "MERGED":
      merged_candidates[ikey] = item

  fix_map: dict[str, dict[str, Any]] = {}
  for f_key, f_item in items_by_key.items():
    if not isinstance(f_item, dict):
      continue
    f_status = str(f_item.get("status") or "").upper()
    if f_status in ("ABANDONED", "CLOSED") or f_item.get("companion_to"):
      continue
    f_kind = str(f_item.get("kind") or "")
    if not f_kind and ":" in f_key:
      f_kind = f_key.split(":", 1)[0]
    f_tid = str(f_item.get("target_id") or "")
    if not f_tid and ":" in f_key:
      f_tid = f_key.split(":", 1)[1]
    f_subj = str(f_item.get("subject") or "")
    f_msg = str(f_item.get("commit_message") or "")
    f_topic = str(f_item.get("topic") or "").strip()
    f_hashtags = list(f_item.get("hashtags") or ())
    f_files = list(f_item.get("files") or ())
    f_bugs = extract_bug_ids(f_subj, f_msg, f_topic, f_hashtags)
    f_full_bots, f_bot_pairs, f_has_cue = extract_bot_mentions_and_failure_cues(
        f_subj, f_msg
    )
    f_test_files, f_test_syms = extract_test_identifiers(
        f_subj, f_msg, f_files
    )
    f_blob = f"{f_subj}\n{f_msg}"
    explicit_cls = {
        m.group(1)
        for m in RE_EXPLICIT_FIX_CL.finditer(f_blob)
        if m.group(1) != f_tid
    }
    explicit_shas = {m.group(1) for m in RE_EXPLICIT_FIX_SHA.finditer(f_blob)}
    qm = RE_QUOTED_REVERT_REL.match(f_subj)
    quoted_subj = qm.group(1).strip() if qm else ""

    f_sub_dt = parse_gerrit_or_iso_time(str(f_item.get("submitted") or ""))
    f_cre_dt = parse_gerrit_or_iso_time(str(f_item.get("created") or ""))

    best_m_key = ""
    best_score = 0
    best_reasons: list[str] = []
    best_bots: list[dict[str, str]] = []

    manual_target = manual.get(f_key) or manual.get(
        f"{f_kind}:{f_tid}", ""
    )
    for m_key, m_item in merged_candidates.items():
      if m_key == f_key:
        continue
      m_kind = str(m_item.get("kind") or "")
      if not m_kind and ":" in m_key:
        m_kind = m_key.split(":", 1)[0]
      if f_kind and m_kind and f_kind != m_kind:
        continue
      m_tid = str(m_item.get("target_id") or "")
      if not m_tid and ":" in m_key:
        m_tid = m_key.split(":", 1)[1]
      m_rev = str(m_item.get("revision") or "")
      m_subj = str(m_item.get("subject") or "")
      m_msg = str(m_item.get("commit_message") or "")
      m_topic = str(m_item.get("topic") or "").strip()
      m_hashtags = list(m_item.get("hashtags") or ())
      m_files = list(m_item.get("files") or ())
      m_sub_dt = parse_gerrit_or_iso_time(str(m_item.get("submitted") or ""))

      # When both changes are already merged, the fix must not predate the broken CL
      if f_status == "MERGED" and f_sub_dt and m_sub_dt and f_sub_dt < m_sub_dt:
        continue

      m_broken_bots = _collect_broken_bots_from_item(m_item)
      score = 0
      reasons: list[str] = []

      if manual_target and manual_target in (m_key, f"{m_kind}:{m_tid}"):
        score += 1000
        reasons.append("manual_override")

      if m_tid and m_tid in explicit_cls:
        score += 500
        reasons.append(f"explicit_cl:{m_tid}")
      if m_rev and any(_same_commit(sha, m_rev) for sha in explicit_shas):
        score += 500
        reasons.append(f"explicit_sha:{m_rev[:10]}")
      if quoted_subj and m_subj and quoted_subj.lower() == m_subj.strip().lower():
        score += 450
        reasons.append("revert_or_reland_subject")

      same_owner = bool(
          (f_item.get("is_self") and m_item.get("is_self"))
          or (
              f_item.get("owner_email")
              and f_item.get("owner_email") == m_item.get("owner_email")
          )
      )
      # Correlated bug/topic/bot/test signals require same author and plausible timing
      if same_owner:
        timing_ok = True
        if m_sub_dt and f_cre_dt:
          delta_sec = (f_cre_dt - m_sub_dt).total_seconds()
          if delta_sec < -3600 or delta_sec > 14 * 86400:
            timing_ok = False
        if timing_ok:
          m_bugs = extract_bug_ids(m_subj, m_msg, m_topic, m_hashtags)
          m_test_files, m_test_syms = extract_test_identifiers(
              m_subj, m_msg, m_files
          )
          m_ps = m_item.get("post_submit") or {}
          for inc in m_ps.get("incidents") or []:
            if isinstance(inc, dict) and inc.get("failing_test"):
              m_test_syms.add(str(inc["failing_test"]))

          shared_bugs = f_bugs & m_bugs
          shared_topic = bool(f_topic and f_topic == m_topic)
          shared_test_syms = f_test_syms & m_test_syms
          shared_test_files = f_test_files & m_test_files

          f_mentions_m_bot = any(
              bot_matches_mentions(b["task_name"], f_full_bots, f_bot_pairs)
              for b in m_broken_bots
          )
          m_has_alert = bool(m_ps.get("has_alert") or m_broken_bots)
          f_has_bot_and_cue = bool((f_full_bots or f_bot_pairs) and f_has_cue)
          f_test_only_followup = bool(
              f_files
              and all(
                  "test" in str(fp).lower() or str(fp).startswith("tests/")
                  for fp in f_files
              )
              and (shared_test_files or shared_test_syms)
              and f_has_cue
          )

          # Follow-up CL produced in the same conversation as the broken CL
          same_chat = bool(
              f_item.get("conversation_id")
              and f_item.get("conversation_id") == m_item.get("conversation_id")
          )
          has_link_signal = bool(
              shared_bugs
              or shared_topic
              or shared_test_syms
              or shared_test_files
              or same_chat
          )
          has_breakage_signal = bool(
              f_mentions_m_bot
              or m_has_alert
              or f_has_bot_and_cue
              or f_test_only_followup
              or same_chat
          )
          if has_link_signal and has_breakage_signal:
            # Same chat strongly indicates a direct follow-up fix
            if same_chat:
              score += 140
              reasons.append("same_chat_followup")
            if shared_bugs:
              score += 120
              reasons.append(f"shared_bug:{sorted(shared_bugs)[0]}")
            if shared_topic:
              score += 80
              reasons.append(f"shared_topic:{f_topic}")
            if shared_test_syms:
              score += 110
              reasons.append(f"shared_test_symbol:{sorted(shared_test_syms)[0]}")
            if shared_test_files:
              score += 70
              reasons.append(f"shared_test_file:{sorted(shared_test_files)[0]}")
            if f_mentions_m_bot:
              score += 150
              reasons.append("mentions_blamed_bot")
            elif m_has_alert:
              score += 90
              reasons.append("parent_post_submit_alert")
            elif f_has_bot_and_cue:
              score += 60
              reasons.append("mentions_bot_failure")

      if score > best_score:
        best_score = score
        best_m_key = m_key
        best_reasons = reasons
        matched_bots = list(m_broken_bots)
        seen_bnames = {b["task_name"] for b in matched_bots}
        for fb in sorted(f_full_bots):
          if (
              len(fb) > 12
              and fb not in seen_bnames
              and not any(bn.startswith(fb) for bn in seen_bnames)
          ):
            seen_bnames.add(fb)
            matched_bots.append(
                {"task_name": fb, "task_url": "", "swarming_url": ""}
            )
        for pref, suf in sorted(f_bot_pairs):
          if not any(
              bn.startswith(pref) and bn.endswith(suf) for bn in seen_bnames
          ):
            synth = f"{pref}...{suf}"
            seen_bnames.add(synth)
            matched_bots.append(
                {"task_name": synth, "task_url": "", "swarming_url": ""}
            )
        best_bots = matched_bots

    if best_m_key and best_score >= 100:
      fix_map[f_key] = {
          "broken_key": best_m_key,
          "reasons": best_reasons,
          "matched_bots": best_bots,
      }
  return fix_map


def link_fix_changes_to_parent_chats(
    active_discovered: list[dict[str, Any]],
    fetched_cache: dict[str, dict[str, Any]],
    all_chat_candidates: list[dict[str, Any]] | None = None,
) -> None:
  """Tags fix CLs/PRs with `fixes_breakage_from` and shares the broken CL's chat when unchatted."""
  by_item_key: dict[str, dict[str, Any]] = {}
  combined_info: dict[str, dict[str, Any]] = {}
  for d in active_discovered:
    ikey = str(d.get("item_key") or "")
    if not ikey:
      continue
    if ikey not in by_item_key or d.get("conversation_id"):
      by_item_key[ikey] = d
    info = fetched_cache.get(ikey) or {}
    if info.get("ok"):
      combined_info[ikey] = {**d, **info}

  known_chats: dict[str, dict[str, Any]] = {}
  for c in all_chat_candidates or []:
    ikey = str(c.get("item_key") or "")
    if ikey and c.get("conversation_id") and ikey not in known_chats:
      known_chats[ikey] = c

  fix_map = detect_fix_for_pairs(combined_info)
  for d in active_discovered:
    ikey = str(d.get("item_key") or "")
    match = fix_map.get(ikey)
    if not match:
      continue
    broken_key = str(match["broken_key"])
    d["fixes_breakage_from"] = broken_key
    d["fixes_breakage_bots"] = match.get("matched_bots") or []
    parent = known_chats.get(broken_key) or by_item_key.get(broken_key)
    if parent and parent.get("conversation_id"):
      if not d.get("conversation_id"):
        d["conversation_id"] = parent["conversation_id"]
        d["title"] = parent.get("title", "")
        d["project_id"] = parent.get("project_id", "")
        d["project_name"] = parent.get("project_name", "")
        d["chat_last_modified"] = parent.get("chat_last_modified", "")
        d["chat_busy"] = parent.get("chat_busy", False)
        d["chat_archived"] = parent.get("chat_archived", False)
      d["single_target"] = False
      parent["single_target"] = False


def _item_created_sort_key(
    d: dict[str, Any], fetched_cache: dict[str, dict[str, Any]]
) -> float:
  """Extracts creation timestamp as float seconds for FIFO queue ordering."""
  ikey = str(d.get("item_key") or "")
  info = fetched_cache.get(ikey) or {}
  for ts_val in (
      info.get("created"),
      d.get("created"),
      info.get("updated"),
      d.get("updated"),
  ):
    if ts_val:
      dt = parse_gerrit_or_iso_time(str(ts_val))
      if dt:
        return dt.timestamp()
  return 0.0


def order_and_link_stack_changes(
    active_discovered: list[dict[str, Any]],
    fetched_cache: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
  """Computes stack ancestors, parents, and depth; orders queue bottom-up and FIFO."""
  # Index GitHub PR items by head branch name and head commit SHA for stack linking
  gh_head_to_item: dict[str, dict[str, Any]] = {}
  gh_sha_to_item: dict[str, dict[str, Any]] = {}
  for d in active_discovered:
    if d.get("kind") == "github":
      ikey = str(d.get("item_key") or "")
      info = fetched_cache.get(ikey) or {}
      href = str(info.get("head_ref") or d.get("head_ref") or "").strip()
      hsha = str(info.get("head_sha") or d.get("head_sha") or "").strip()
      if href:
        gh_head_to_item[href] = d
      if hsha:
        gh_sha_to_item[hsha] = d

  # Index Gerrit changes by target ID
  gerrit_by_tid: dict[str, dict[str, Any]] = {
      str(d.get("target_id") or ""): d
      for d in active_discovered
      if d.get("kind") == "gerrit"
  }

  for d in active_discovered:
    ikey = str(d.get("item_key") or "")
    kind = str(d.get("kind") or "")
    info = fetched_cache.get(ikey) or {}
    stack_ancestors: list[str] = []
    parent_ikey = ""

    # Companion CLs are linked to their parent chat via link_companion_changes
    if d.get("companion_to"):
      d["stack_ancestors"] = []
      d["stack_parent_ikey"] = ""
      d["stack_depth"] = 0
      d["stack_root_ikey"] = ""
      d["stack_size"] = 1
      d["stack_index"] = 0
      d["stack_ikeys"] = []
      continue

    if kind == "gerrit":
      tid = str(d.get("target_id") or "")
      host = str(d.get("preferred_host") or info.get("host") or GERRIT_SKIA_HOST)
      stack_nums = fetch_gerrit_stack_order_ids(host, tid)
      if tid in stack_nums and len(stack_nums) > 1:
        idx = stack_nums.index(tid)
        for anc_tid in stack_nums[:idx]:
          anc_item = gerrit_by_tid.get(anc_tid)
          anc_key = (
              str(anc_item.get("item_key"))
              if anc_item and anc_item.get("item_key")
              else f"gerrit:{anc_tid}"
          )
          stack_ancestors.append(anc_key)
        if idx > 0:
          direct_num = stack_nums[idx - 1]
          direct_item = gerrit_by_tid.get(direct_num)
          parent_ikey = (
              str(direct_item.get("item_key"))
              if direct_item and direct_item.get("item_key")
              else f"gerrit:{direct_num}"
          )
        d["stack_depth"] = idx
      else:
        d["stack_depth"] = 0

    elif kind == "github":
      bref = str(info.get("base_ref") or d.get("base_ref") or "").strip()
      gh_parent = gh_head_to_item.get(bref) if bref else None
      if not gh_parent:
        commits = info.get("commit_shas") or d.get("commit_shas") or []
        for csha in commits:
          cand = gh_sha_to_item.get(str(csha).strip())
          if cand and cand != d:
            gh_parent = cand
            break
      if gh_parent and gh_parent != d:
        p_key = str(gh_parent.get("item_key") or "")
        parent_ikey = p_key
        # Trace ancestors bottom-up to prevent cycles
        cur: dict[str, Any] | None = gh_parent
        visited: set[str] = {ikey}
        while cur:
          cur_key = str(cur.get("item_key") or "")
          if not cur_key or cur_key in visited:
            break
          visited.add(cur_key)
          stack_ancestors.insert(0, cur_key)
          cur_info = fetched_cache.get(cur_key) or {}
          next_bref = str(
              cur_info.get("base_ref") or cur.get("base_ref") or ""
          ).strip()
          cur = gh_head_to_item.get(next_bref) if next_bref else None
        d["stack_depth"] = len(stack_ancestors)
      else:
        d["stack_depth"] = 0
    else:
      d["stack_depth"] = 0

    d["stack_ancestors"] = stack_ancestors
    d["stack_parent_ikey"] = parent_ikey

  # Order bottom-up (stack parents before children) and FIFO (older first)
  active_discovered.sort(
      key=lambda it: (
          int(it.get("stack_depth") or 0),
          _item_created_sort_key(it, fetched_cache),
          int(it.get("target_id"))
          if str(it.get("target_id", "")).isdigit()
          else 10**18,
      )
  )

  # Group items by stack root and record topological stack position and chain keys
  stacks_by_root: dict[str, list[str]] = {}
  for d in active_discovered:
    if d.get("companion_to"):
      continue
    ikey = str(d.get("item_key") or "")
    ancs = d.get("stack_ancestors") or []
    cand_root = str(ancs[0]) if ancs else ikey
    if cand_root:
      stacks_by_root.setdefault(cand_root, []).append(ikey)

  for d in active_discovered:
    if d.get("companion_to"):
      continue
    ikey = str(d.get("item_key") or "")
    ancs = d.get("stack_ancestors") or []
    cand_root = str(ancs[0]) if ancs else ikey
    members = stacks_by_root.get(cand_root) or [ikey]
    if len(members) >= 2:
      d["stack_root_ikey"] = cand_root
      d["stack_size"] = len(members)
      d["stack_index"] = members.index(ikey) + 1 if ikey in members else 0
      d["stack_ikeys"] = list(members)
    else:
      d["stack_root_ikey"] = ""
      d["stack_size"] = 1
      d["stack_index"] = 0
      d["stack_ikeys"] = []

  return active_discovered


def _has_unready_open_stack_parent(
    ikey: str,
    active_by_ikey: dict[str, dict[str, Any]],
    fetched_cache: dict[str, dict[str, Any]],
    new_items: dict[str, Any],
    prev_items: dict[str, Any],
) -> bool:
  """Returns True if any open ancestor in the stack is still unreviewed or busy."""
  d = active_by_ikey.get(ikey)
  if not d:
    _, d_match = _lookup_state_item(new_items, ikey)
    d = d_match or _lookup_state_item(prev_items, ikey)[1]
  if not d:
    return False

  ancestors = list(d.get("stack_ancestors") or [])
  if not ancestors:
    return False

  for anc_key in ancestors:
    _, anc_rec = _lookup_state_item(new_items, anc_key)
    if not anc_rec:
      _, anc_prev = _lookup_state_item(prev_items, anc_key)
      anc_rec = anc_prev or active_by_ikey.get(anc_key)
    anc_info = fetched_cache.get(anc_key) or {}
    if not anc_rec and not anc_info:
      continue

    status = str(
        (anc_rec or {}).get("status") or anc_info.get("status") or ""
    ).upper()
    # Merged or closed parents do not block review of stacked descendants
    if status in ("MERGED", "ABANDONED", "CLOSED"):
      continue

    # Active chat currently running triage or review blocks child auto-start
    if anc_rec and anc_rec.get("chat_busy"):
      return True

    anc_kind = str(
        (anc_rec or {}).get("kind")
        or anc_info.get("kind")
        or (anc_key.split(":")[0] if ":" in anc_key else "gerrit")
    )
    anc_tid = str(
        (anc_rec or {}).get("target_id")
        or anc_info.get("target_id")
        or (anc_key.split(":")[-1] if ":" in anc_key else "")
    )
    anc_ps = int(
        (anc_rec or {}).get("patchset")
        or anc_info.get("patchset")
        or (anc_rec or {}).get("review_target_ps")
        or 1
    )

    # Check whether parent was already approved or reviewed on host platform
    self_rev_st = (anc_rec or {}).get("self_review_state") or anc_info.get(
        "self_review_state"
    )
    self_rev_ps = int(
        (anc_rec or {}).get("self_review_ps")
        or anc_info.get("self_review_ps")
        or 0
    )
    self_appr = bool(
        (anc_rec or {}).get("self_approved") or anc_info.get("self_approved")
    )
    if (self_rev_st and self_rev_ps >= anc_ps) or self_appr:
      continue

    # Check whether parent review was completed and saved in triage_store
    tr = triage_store.load_triage_result(anc_kind, anc_tid)
    if tr and int(tr.get("patchset") or 0) >= anc_ps and tr.get("verdict"):
      continue

    # Check whether chat already completed review for the current patchset
    if (
        anc_rec
        and int(anc_rec.get("reviewed_ps") or 0) >= anc_ps
        and not anc_rec.get("chat_busy")
    ):
      continue

    # When parent review is not assigned to self, check if external approvals exist
    is_my_rev = (
        _is_assigned_reviewer_record(anc_rec)
        if anc_rec
        else _is_assigned_reviewer_record(active_by_ikey.get(anc_key) or {})
    )
    if not is_my_rev:
      appr_cnt = int(
          (anc_rec or {}).get("approved_count")
          or anc_info.get("approved_count")
          or 0
      )
      miss_appr = (anc_rec or {}).get("missing_approvals")
      if miss_appr is None:
        miss_appr = anc_info.get("missing_approvals")
      if appr_cnt > 0 or miss_appr == 0:
        continue

    # Ancestor is still open and unready
    return True

  return False


def _build_fix_entry(rec: dict[str, Any], fix_self_label: str) -> dict[str, Any]:
  """Summarizes a fix CL/PR's operational lifecycle stage for display on broken CLs."""
  status = str(rec.get("status") or "").upper()
  cq_st = str(rec.get("cq_status") or "")
  is_wip = bool(rec.get("is_wip") or rec.get("is_draft"))
  in_dev = bool(rec.get("in_development"))
  phase = str(rec.get("phase") or "")
  cq_failed = bool(rec.get("cq_failed"))
  approved_count = int(rec.get("approved_count") or 0)
  missing_approvals = int(rec.get("missing_approvals") or 0)

  # Merged fix CLs track post-submit roll propagation into downstream products
  if status == "MERGED":
    f_ps = rec.get("post_submit") or {}
    if f_ps.get("all_passed"):
      stage = "shipped"
      stage_label = "Fix shipped"
      status_summary = "merged · ✓All Rolls"
    elif "passed_rolls" in f_ps or f_ps.get("total_rolls"):
      stage = "merged_rolling"
      stage_label = "Fix landed · rolling"
      total_r = (
          len(f_ps.get("rolls") or ()) or int(f_ps.get("total_rolls") or 4)
      )
      passed_r = f_ps.get("passed_rolls", 0)
      status_summary = f"merged · {passed_r}/{total_r} rolls"
    else:
      stage = "merged_rolling"
      stage_label = "Fix landed · rolling"
      status_summary = "merged"
  # Active submission in CQ takes priority before general review states
  elif phase == "SUBMITTING" or cq_st.startswith("CQ+2"):
    stage = "submitting"
    stage_label = "Fix submitting"
    status_summary = "submitting"
  # CQ failures require intervention before review can proceed
  elif cq_failed:
    stage = "cq_failed"
    stage_label = "Fix failed CQ"
    status_summary = "CQ failed"
  # WIP and draft fixes remain in development until marked ready
  elif is_wip:
    stage = "in_progress"
    stage_label = "Fix in progress"
    wip_name = str(
        rec.get("wip_label")
        or ("Draft" if rec.get("is_draft") else "WIP")
    )
    status_summary = (
        f"{wip_name} · {cq_st}"
        if (cq_st and cq_st != "Idle")
        else wip_name
    )
  # Unassigned changes wait for initial reviewer assignment
  elif in_dev:
    stage = "in_progress"
    stage_label = "Fix in progress"
    status_summary = (
        f"needs reviewers · {cq_st}"
        if (cq_st and cq_st != "Idle")
        else "needs reviewers"
    )
  # Dry run verification underway prior to full approval
  elif cq_st.startswith("CQ+") or "running" in cq_st or "Dry Run" in cq_st:
    stage = "in_cq"
    stage_label = (
        "Fix in dry run"
        if rec.get("kind") != "github"
        else "Fix in checks"
    )
    status_summary = cq_st
  # Approved fixes ready for landing
  elif approved_count > 0 and missing_approvals == 0:
    stage = "approved"
    stage_label = "Fix approved"
    status_summary = "approved"
  # Open reviews awaiting author response
  elif rec.get("is_self") and phase == "NEEDS_YOUR_ACTION":
    stage = "needs_reply"
    stage_label = "Fix needs your reply"
    status_summary = "needs reply"
  else:
    raw_waiting = rec.get("waiting_on") or []
    waiting_reviewers = [
        str(w.get("id") or "").split("@")[0]
        for w in raw_waiting
        if isinstance(w, dict) and w.get("active") and w.get("id")
    ]
    stage = "in_review"
    stage_label = "Fix awaiting review"
    if waiting_reviewers:
      names = ", ".join(waiting_reviewers[:2])
      cr_sum = str(rec.get("cr_summary") or "")
      status_summary = (
          f"waiting on {names} · CR {cr_sum}"
          if (approved_count > 0 and cr_sum)
          else f"waiting on {names}"
      )
    elif rec.get("reviewer_status"):
      status_summary = str(rec.get("reviewer_status")).lower()
    else:
      status_summary = "in review"

  fix_age = str(
      rec.get("status_changed_age")
      or rec.get("last_msg_age")
      or rec.get("submitted_age")
      or ""
  )
  return {
      "item_key": str(rec.get("item_key") or ""),
      "target_id": str(rec.get("target_id") or ""),
      "label": fix_self_label,
      "url": str(rec.get("url") or ""),
      "subject": str(rec.get("subject") or ""),
      "status": status,
      "stage": stage,
      "stage_label": stage_label,
      "status_summary": status_summary,
      "age": fix_age,
      "is_wip": is_wip,
  }


def load_ignored_cls(path: str = IGNORED_CLS_FILE) -> set[str]:
  """Reads permanently ignored Gerrit CL IDs from review-triage's ignore list."""
  ignored: set[str] = set()
  if not os.path.isfile(path):
    return ignored
  try:
    with open(path, "r", encoding="utf-8") as f:
      for raw in f:
        line = raw.split("#", 1)[0].strip()
        if line:
          ignored.add(line)
  except OSError:
    pass
  return ignored


def infer_project_for_change(
    kind: str,
    host: str,
    subject: str,
    proj_names: dict[str, str],
    *,
    commit_message: str = "",
    files: list[str] | tuple[str, ...] = (),
    hashtags: list[str] | tuple[str, ...] = (),
    topic: str = "",
) -> tuple[str, str]:
  """Maps an unchatted remote CL/PR to the matching Jetski project (id, name)."""
  by_name = {name.lower(): (pid, name) for pid, name in proj_names.items()}

  def pick(*candidates: str) -> tuple[str, str]:
    for cand in candidates:
      hit = by_name.get(cand.lower())
      if hit:
        return hit
    return ("", candidates[0] if candidates else "Unassigned")

  subj_low = (subject or "").lower()
  if kind == "github":
    if "skparagraph" in subj_low and "web" not in subj_low:
      return pick("SkParagraph", "WebParagraph")
    return pick("WebParagraph")
  if host == GERRIT_CHROMIUM_HOST:
    # Canvas TextMetrics / ShapedText / TextCluster CLs belong to TextClusters API
    if any(
        kw in subj_low
        for kw in ("[canvas]", "textmetrics", "shapedtext", "textcluster", "canvas2d")
    ):
      return pick("TextClusters API", "Chromium")
    return pick("Chromium", "TextClusters API")
  # CoreText / SkoreText macOS layout pipeline belongs to SkoreText
  msg_low = (commit_message or "").lower()
  topic_low = (topic or "").lower()
  hashtags_low = {str(h).lower() for h in hashtags}
  files_low = [str(f).lower() for f in files]
  is_skoretext = (
      any(kw in subj_low for kw in ("skoretext", "coretext"))
      or any(kw in topic_low for kw in ("skoretext", "coretext"))
      or any("skoretext" in h or "coretext" in h for h in hashtags_low)
      or any(
          kw in msg_low
          for kw in (
              "skoretext",
              "coretext pipeline",
              "coretext layout",
              "coretext shaping",
          )
      )
      or any("coretext" in f or "_mac_ct" in f for f in files_low)
  )
  if is_skoretext:
    return pick("SkoreText", "Skia")
  if "lazy" in subj_low and "android" in subj_low:
    return pick("Lazy Android", "Skia")
  if any(
      kw in subj_low
      for kw in ("skparagraph", "skunicode", "skshaper", "paragraph")
  ):
    return pick("SkParagraph", "Skia")
  return pick("Skia")


def discover_open_remote_changes(
    already_chatted_keys: set[str],
    proj_names: dict[str, str] | None = None,
    ignored_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
  """Queries Gerrit and GitHub for open changes involving the user that lack a chat."""
  pnames = proj_names if proj_names is not None else load_project_names()
  ignored = ignored_ids if ignored_ids is not None else load_ignored_cls()
  seen_keys = set(already_chatted_keys)
  remote: list[dict[str, Any]] = []

  def _remote_item(
      ikey: str,
      kind: str,
      num: str,
      host: str,
      pid: str,
      pname: str,
      repo_slug: str = "",
  ) -> dict[str, Any]:
    out_item: dict[str, Any] = {
        "item_key": ikey,
        "kind": kind,
        "target_id": num,
        "preferred_host": host,
        "conversation_id": "",
        "title": "",
        "project_id": pid,
        "project_name": pname,
        "chat_last_modified": "",
        "single_target": True,
        "chat_busy": False,
    }
    if repo_slug:
      out_item["repo_slug"] = repo_slug
    return out_item

  # Include self-authored changes merged in the last 3 days (plus reviewed Skia
  # changes where Gardener tracks post-submit tree/roll health)
  for host in GERRIT_HOSTS:
    merged_clause = (
        "(owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com"
        "+OR+reviewer:jlavrova@google.com+OR+reviewer:jlavrova@gmail.com)"
        if host == GERRIT_SKIA_HOST
        else "(owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com)"
    )
    gerrit_q = (
        "((status:open+((owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com)"
        "+OR+((reviewer:jlavrova@google.com+OR+reviewer:jlavrova@gmail.com)+-is:wip)))"
        f"+OR+(status:merged+{merged_clause}+-age:3d))"
    )
    try:
      changes = _gerrit_http_get_json(
          host, f"/changes/?q={gerrit_q}&n=100", timeout=10
      )
    except (OSError, TimeoutError, ValueError, http.client.HTTPException):
      continue
    if not isinstance(changes, list):
      continue
    for ch in changes:
      if not isinstance(ch, dict):
        continue
      num = str(ch.get("_number") or "").strip()
      if not num or num in ignored:
        continue
      ikey = f"gerrit:{num}"
      if ikey in seen_keys:
        continue
      subj = str(ch.get("subject") or "")
      if RE_AUTOROLL_SUBJECT.match(subj):
        continue
      seen_keys.add(ikey)
      pid, pname = infer_project_for_change("gerrit", host, subj, pnames)
      remote.append(_remote_item(ikey, "gerrit", num, host, pid, pname))

  # GitHub Search rejects parenthesized OR across qualifiers with HTTP 422
  gh_queries = (
      "is:open is:pr author:Rusino",
      "is:open is:pr involves:Rusino -author:Rusino",
  )
  for gh_q in gh_queries:
    try:
      out = subprocess.check_output(
          [
              "gh",
              "api",
              "-X",
              "GET",
              "search/issues",
              "-f",
              f"q={gh_q}",
              "-f",
              "per_page=30",
          ],
          stderr=subprocess.DEVNULL,
          text=True,
          timeout=10,
      )
      prs = (json.loads(out) or {}).get("items") or []
    except (OSError, subprocess.SubprocessError, ValueError):
      continue
    if not isinstance(prs, list):
      continue
    for pr in prs:
      if not isinstance(pr, dict):
        continue
      num = str(pr.get("number") or "").strip()
      html_url = str(pr.get("html_url") or "")
      m_repo = re.search(r"github\.com/([^/]+/[^/]+)/", html_url)
      if not m_repo:
        m_repo = re.search(r"/repos/([^/]+/[^/]+)$", str(pr.get("repository_url") or ""))
      repo_slug = m_repo.group(1) if m_repo else "flutter/flutter"
      if not num or num in ignored or f"{repo_slug}#{num}" in ignored:
        continue
      ikey = f"github:{num}"
      if ikey in seen_keys:
        continue
      subj = str(pr.get("title") or "")
      if RE_AUTOROLL_SUBJECT.match(subj):
        continue
      seen_keys.add(ikey)
      pid, pname = infer_project_for_change("github", f"github.com/{repo_slug}", subj, pnames)
      remote.append(
          _remote_item(
              ikey,
              "github",
              num,
              f"github.com/{repo_slug}",
              pid,
              pname,
              repo_slug=repo_slug,
          )
      )

  return remote


def fetch_gardener_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
  """Fetches autorollers and active incidents from local skia-gardener."""
  rollers: dict[str, Any] = {}
  incidents: list[dict[str, Any]] = []
  try:
    with urllib.request.urlopen(f"{DEFAULT_GARDENER_URL}/api/autorollers", timeout=4) as r:
      arr = json.loads(r.read().decode("utf-8", errors="replace")) or []
      for item in arr:
        if isinstance(item, dict) and item.get("name"):
          rollers[item["name"]] = item
  except Exception:
    pass
  try:
    with urllib.request.urlopen(f"{DEFAULT_GARDENER_URL}/api/incidents", timeout=4) as r:
      incidents = json.loads(r.read().decode("utf-8", errors="replace")) or []
  except Exception:
    pass
  return rollers, incidents


def _git_is_ancestor(skia_dir: str, ancestor_sha: str, descendant_sha: str) -> bool:
  if not ancestor_sha or not descendant_sha or not os.path.isdir(skia_dir):
    return False
  try:
    res = subprocess.run(
        ["git", "-C", skia_dir, "merge-base", "--is-ancestor", ancestor_sha, descendant_sha],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=3,
        check=False,
    )
    return res.returncode == 0
  except Exception:
    return False


def _same_commit(a: str, b: str) -> bool:
  return bool(a) and bool(b) and (a.startswith(b[:10]) or b.startswith(a[:10]))


def classify_incident_for_commit(
    inc: dict[str, Any], merged_sha: str
) -> dict[str, Any] | None:
  """Maps a Skia Gardener incident to its verdict on `merged_sha`, or None if unrelated.

  The verdict mirrors the Gardener overlay: only the incident's chosen culprit can be
  accused, and flakes or low-confidence single-bot blames stay informational.
  """
  blame = inc.get("blame") or {}
  task = inc.get("task") or {}
  culprit = str(blame.get("commit_hash") or "")
  is_culprit = _same_commit(culprit, merged_sha)
  in_blamelist = any(
      _same_commit(str(c), merged_sha) for c in task.get("commits") or []
  )
  if not is_culprit and not in_blamelist:
    return None
  affected = inc.get("affected_tasks") or []
  is_flake = bool(inc.get("is_intermittent_flake")) or any(
      a.get("is_intermittent_flake") for a in affected
  )
  is_compile = any(
      str(a.get("classification")) in ("COMPILE_ERROR", "COMPILE_FAILURE")
      for a in affected
  )
  score = int(blame.get("confidence_score") or 0)
  low_conf = blame.get("confidence") == "Low" or 0 < score < 65
  multi_bot = max(int(inc.get("bot_count") or 0), len(affected)) >= 2
  # The overlay treats compile breaks and multi-bot regressions as certain even
  # at low confidence, so only single-bot test failures are downgraded
  if not is_culprit:
    verdict = "blamelist"
  elif is_flake:
    verdict = "flake"
  elif low_conf and not (is_compile or multi_bot):
    verdict = "low_confidence"
  else:
    verdict = "blamed"
  negatives = [
      str(f.get("reason") or "")
      for f in blame.get("confidence_factors") or []
      if int(f.get("impact") or 0) < 0
  ]
  # Preserve every affected task on the incident so multi-bot breakages show all bots
  affected_list: list[dict[str, str]] = []
  for a in affected:
    if not isinstance(a, dict) or not a.get("name"):
      continue
    a_id = str(a.get("id") or "")
    a_sw = str(a.get("swarming_task_id") or "")
    affected_list.append({
        "task_name": str(a.get("name") or ""),
        "task_url": (
            f"https://task-scheduler.skia.org/task/{a_id}" if a_id else ""
        ),
        "swarming_url": (
            f"https://chromium-swarm.appspot.com/task?id={a_sw}"
            if a_sw
            else ""
        ),
        "latest_main_rev": str(a.get("latest_main_rev") or ""),
    })
  # Extract the failing unit test symbol when Gardener records it in blame or crash text
  failing_test = ""
  reason_sources = [str(blame.get("reason") or "")] + [
      str(f.get("reason") or "")
      for f in blame.get("confidence_factors") or []
      if isinstance(f, dict)
  ]
  for rsrc in reason_sources:
    m_test = RE_FAILING_TEST_REASON.search(rsrc)
    if m_test:
      failing_test = m_test.group(1)
      break
  if not failing_test:
    m_crash = RE_CRASH_TEST_NAME.search(str(blame.get("crash_analysis") or ""))
    if m_crash:
      failing_test = m_crash.group(1)
  swarming_id = str(task.get("swarming_task_id") or "")
  return {
      "verdict": verdict,
      "task_name": str(task.get("name") or "bot"),
      "task_url": (
          f"https://task-scheduler.skia.org/task/{task['id']}"
          if task.get("id")
          else ""
      ),
      "swarming_url": (
          f"https://chromium-swarm.appspot.com/task?id={swarming_id}"
          if swarming_id
          else ""
      ),
      "blamelist_size": len(task.get("commits") or []),
      "culprit_short": culprit[:7],
      "culprit_commit_url": (
          f"https://skia.googlesource.com/skia/+/{culprit}" if culprit else ""
      ),
      "culprit_cl": str(blame.get("gerrit_change_number") or ""),
      "culprit_cl_url": str(blame.get("gerrit_url") or ""),
      "culprit_subject": str(blame.get("subject") or ""),
      "confidence_score": score,
      "reason": negatives[0] if negatives else "",
      # Multi-bot and test metadata for fix-for matching and incident expansion
      "affected_tasks": affected_list,
      "failing_test": failing_test,
      "is_compile": is_compile,
      "multi_bot": multi_bot,
  }


def build_post_submit_advice(
    res: dict[str, Any], is_self: bool = True, is_external: bool = False
) -> str:
  """Produces deterministic action advice for landed commits with post-submit alerts."""
  if not res.get("has_alert"):
    return ""

  blamed = [
      h for h in res.get("incidents") or [] if h.get("verdict") == "blamed"
  ]
  broken_bots = res.get("broken_bots") or []
  rolls = res.get("rolls") or {}
  failed_rolls = [
      rname
      for rname, rinfo in rolls.items()
      if (rinfo or {}).get("state") == "🔴"
  ]

  # Case A: Blamed on Skia post-submit tree
  if blamed:
    is_compile = any(h.get("is_compile") for h in blamed)
    multi_bot = (
        any(h.get("multi_bot") for h in blamed)
        or len(broken_bots) >= 2
        or len(blamed) >= 2
    )
    if is_compile or multi_bot:
      base = (
          "Compile or multi-bot tree breakage: revert immediately unless a"
          " trivial fix is ready"
      )
    else:
      failing_test = next(
          (h.get("failing_test") for h in blamed if h.get("failing_test")), ""
      )
      if failing_test:
        base = (
            f"Single-bot failure in {failing_test}: check platform/font-backend"
            " metric assumptions or upload a fix-for CL (revert if not quick)"
        )
      else:
        bot_names = [
            b.get("task_name") for b in broken_bots if b.get("task_name")
        ]
        bot_str = f" on {bot_names[0]}" if bot_names else ""
        base = (
            f"Post-submit bot failure{bot_str}: inspect task logs via 'See"
            " failure' and prepare a fix-for CL or revert"
        )
    if is_external and not is_self:
      base += (
          ". External author cannot self-submit a revert or fix — sponsor or"
          " land it"
      )
    return base

  # Case B: Blamed in a failing autoroller
  if failed_rolls:
    if "Google3" in failed_rolls:
      base = (
          "Google3 autoroll blames this commit: external/OSS authors cannot see"
          " internal TAP failures — click 'Start chat' to run skia-roll-triage"
      )
    else:
      rlist = ", ".join(failed_rolls)
      base = (
          f"{rlist} autoroll blames this commit: inspect roll trybot logs or"
          " revert on Skia main to unblock the roller"
      )
    if is_external and not is_self:
      base += ". External author needs your CR+1/CQ+2 to land a fix or revert"
    return base

  return "Post-submit alert: inspect task logs and verify tree health"


def check_skia_post_submit(
    merged_sha: str,
    rollers: dict[str, Any],
    incidents: list[dict[str, Any]],
    skia_dir: str = DEFAULT_SKIA_DIR,
    cl_id: str = "",
    is_self: bool = True,
    is_external: bool = False,
) -> dict[str, Any]:
  """Checks Skia tree status and the 4 outgoing autorollers for a merged Skia commit."""
  res: dict[str, Any] = {
      "merged_sha": merged_sha,
      "skia_status": "⏳",
      "skia_status_detail": "Waiting for post-submit tasks",
      "rolls": {
          "Android": {"state": "⏳", "detail": "Not rolled yet"},
          "Chrome": {"state": "⏳", "detail": "Not rolled yet"},
          "Google3": {"state": "⏳", "detail": "Not rolled yet"},
          "Flutter": {"state": "⏳", "detail": "Not rolled yet"},
      },
      "incidents": [],
      # Flattened list of blamed post-submit bot tasks across all incidents
      "broken_bots": [],
      "passed_rolls": 0,
      "all_passed": False,
      "has_alert": False,
      "alert_reason": "",
      "recommended_action": "",
      "compact_badge": "",
  }
  if not merged_sha:
    return res

  hits = [
      h
      for h in (classify_incident_for_commit(inc, merged_sha) for inc in incidents)
      if h
  ]
  res["incidents"] = hits
  blamed = [h for h in hits if h["verdict"] == "blamed"]
  if blamed:
    broken_bots: list[dict[str, str]] = []
    seen_bot_names: set[str] = set()
    for h in blamed:
      aff = h.get("affected_tasks") or []
      # Prefer affected tasks pinned to merged_sha before falling back to all affected tasks
      matched_aff = [
          a
          for a in aff
          if not a.get("latest_main_rev")
          or _same_commit(str(a.get("latest_main_rev") or ""), merged_sha)
      ] or list(aff)
      for a in matched_aff:
        tname = str(a.get("task_name") or "")
        if tname and tname not in seen_bot_names:
          seen_bot_names.add(tname)
          broken_bots.append({
              "task_name": tname,
              "task_url": str(a.get("task_url") or h.get("task_url") or ""),
              "swarming_url": str(
                  a.get("swarming_url") or h.get("swarming_url") or ""
              ),
          })
      primary_name = str(h.get("task_name") or "")
      if primary_name and primary_name not in seen_bot_names:
        seen_bot_names.add(primary_name)
        broken_bots.append({
            "task_name": primary_name,
            "task_url": str(h.get("task_url") or ""),
            "swarming_url": str(h.get("swarming_url") or ""),
        })
    res["broken_bots"] = broken_bots
    res["skia_status"] = "🔴"
    # List up to 3 distinct broken bots in the tooltip and alert summary
    bot_summary = [b["task_name"] for b in broken_bots[:3]] or [
        h["task_name"] for h in blamed[:2]
    ]
    res["skia_status_detail"] = (
        f"Blamed by Gardener in: {', '.join(bot_summary)}"
    )
    res["has_alert"] = True
    res["alert_reason"] = res["skia_status_detail"]
  elif hits:
    res["skia_status"] = "⚠️"
    res["skia_status_detail"] = "Near a failing task, not blamed by Gardener"
  else:
    res["skia_status"] = "✓"
    res["skia_status_detail"] = "Clean on status.skia.org"

  passed_rolls = 0
  for rname in ("Android", "Chrome", "Google3", "Flutter"):
    r = rollers.get(rname) or {}
    last_rev = str(r.get("last_roll_rev") or "")
    curr_rev = str(r.get("current_roll_rev") or "")
    num_failed = int(r.get("num_failed") or 0)
    diag = r.get("diagnosis") or {}
    roll_url = str(r.get("url") or "")

    if _git_is_ancestor(skia_dir, merged_sha, last_rev):
      res["rolls"][rname] = {
          "state": "✓",
          "detail": f"Rolled ({last_rev[:8]})",
          "url": roll_url,
          "rev": last_rev,
      }
      passed_rolls += 1
    elif _git_is_ancestor(skia_dir, merged_sha, curr_rev):
      # A failing roll carries every commit in its range; accuse this one only when
      # the Gardener puts the fault on Skia and names it, since upstream verdicts
      # list exonerated Skia commits in the same summary text
      summary = str(diag.get("culprit_summary") or "")
      named = diag.get("fault_category") == "Skia" and (
          (cl_id and cl_id in summary) or merged_sha[:7] in summary
      )
      if num_failed > 0 and named:
        res["rolls"][rname] = {
            "state": "🔴",
            "detail": f"Blamed in failing roll ({curr_rev[:8]}, failed={num_failed})",
            "url": roll_url,
            "rev": curr_rev,
        }
        res["has_alert"] = True
        if not res["alert_reason"]:
          res["alert_reason"] = f"{rname} roll blames this commit ({curr_rev[:8]})"
      elif num_failed > 0:
        res["rolls"][rname] = {
            "state": "🟠",
            "detail": f"In failing roll, not blamed ({curr_rev[:8]}, failed={num_failed})",
            "url": roll_url,
            "rev": curr_rev,
        }
      else:
        res["rolls"][rname] = {
            "state": "🔄",
            "detail": f"In CQ ({curr_rev[:8]})",
            "url": roll_url,
            "rev": curr_rev,
        }
    else:
      res["rolls"][rname] = {
          "state": "⏳",
          "detail": "Waiting for roll window",
          "url": roll_url,
          "rev": "",
      }

  res["passed_rolls"] = passed_rolls

  res["all_passed"] = (
      res["skia_status"] == "✓" and passed_rolls == 4 and not res["has_alert"]
  )

  if res["has_alert"]:
    res["compact_badge"] = f"[🔴Alert · {passed_rolls}/4 rolls]"
  elif res["all_passed"]:
    res["compact_badge"] = "[✓All Rolls]"
  else:
    st_tok = "✓St" if res["skia_status"] == "✓" else "⏳St"
    res["compact_badge"] = f"[{st_tok} · {passed_rolls}/4 rolls]"

  res["recommended_action"] = build_post_submit_advice(
      res, is_self=is_self, is_external=is_external
  )

  return res


_ACCOUNT_ACTIVE: dict[str, bool] = {}


def google_account_active(email: str) -> bool:
  """Reports whether a @google.com account still exists; other domains count as active.

  Attention sets keep former employees, and pinging them is wasted effort.
  """
  if not email.endswith("@google.com"):
    return True
  ldap = email.split("@", 1)[0]
  if ldap not in _ACCOUNT_ACTIVE:
    try:
      res = subprocess.run(
          ["id", "-u", ldap],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=3,
          check=False,
      )
      _ACCOUNT_ACTIVE[ldap] = res.returncode == 0
    except Exception:
      return True
  return _ACCOUNT_ACTIVE[ldap]


def gerrit_mergeable(host: str, cl_id: str) -> bool:
  """Asks Gerrit whether the current patchset still applies; unknown counts as yes."""
  try:
    data = _gerrit_http_get_json(
        host, f"/changes/{cl_id}/revisions/current/mergeable", timeout=8
    )
    return bool(data.get("mergeable", True)) if isinstance(data, dict) else True
  except Exception:
    return True


def compute_gerrit_reviewer_pings(
    host: str,
    cl_id: str,
    project: str,
    status: str,
    is_wip: bool,
    is_draft: bool,
    min_cr: int,
    missing_approvals: int,
    approved_count: int,
    approved_emails: set[str],
    is_self: bool,
    is_reviewer: bool,
    ps_num: int,
    revs: dict[str, Any],
    messages: list[dict[str, Any]],
    reviewer_accounts: list[dict[str, Any]],
    cr_votes: list[tuple[str, int]],
    att_set: list[str],
    owner_email: str,
    removed_emails: set[str],
) -> list[dict[str, Any]]:
  """Detects reviewers who need to review or whose +1 was lost on rebase/update"""
  if status != "NEW" or is_wip or is_draft or min_cr < 0 or missing_approvals <= 0:
    return []
  # Only surface reviewer pings on our own CLs or external CLs where we already gave CR+1
  self_approved = bool(approved_emails & SELF_EMAILS)
  if not (is_self or (is_reviewer and self_approved)):
    return []

  rev_by_ps: dict[int, dict[str, Any]] = {}
  for r in revs.values():
    if isinstance(r, dict) and r.get("_number"):
      try:
        rev_by_ps[int(r["_number"])] = r
      except (ValueError, TypeError):
        continue

  rebase_ps: set[int] = set()
  for p in range(2, ps_num + 1):
    kind = str(rev_by_ps.get(p, {}).get("kind") or "")
    desc = str(rev_by_ps.get(p, {}).get("description") or "")
    if kind in (
        "TRIVIAL_REBASE",
        "NO_CODE_CHANGE",
        "NO_CHANGE",
        "MERGE_FIRST_PARENT_UPDATE",
    ):
      rebase_ps.add(p)
    elif RE_REBASE_MSG.search(desc):
      rebase_ps.add(p)
    elif any(
        RE_REBASE_MSG.search(str(m.get("message") or ""))
        for m in messages
        if int(m.get("_revision_number") or 0) == p
    ):
      rebase_ps.add(p)

  # Track chronological votes and review comments per reviewer
  plus_one_ps: dict[str, int] = {}
  revoked_emails: set[str] = set()
  comment_ps: dict[str, int] = {}
  reviewer_names: dict[str, str] = {}

  for acc in reviewer_accounts:
    if isinstance(acc, dict) and acc.get("email"):
      reviewer_names[str(acc["email"])] = str(acc.get("name") or acc["email"])

  for m in messages:
    rev_n = int(m.get("_revision_number") or 0)
    auth = m.get("author") or {}
    auth_email = str(auth.get("email") or "")
    if auth.get("name"):
      reviewer_names.setdefault(auth_email, str(auth["name"]))
    txt = str(m.get("message") or "")
    if not auth_email:
      continue
    # Reviewer vote or comment events
    if "Code-Review+1" in txt or "Code-Review+2" in txt:
      plus_one_ps[auth_email] = rev_n
      revoked_emails.discard(auth_email)
    elif "-Code-Review" in txt or "Code-Review-1" in txt or "Code-Review-2" in txt:
      revoked_emails.add(auth_email)
      plus_one_ps.pop(auth_email, None)
    elif auth_email != owner_email and not auth_email.endswith(".gserviceaccount.com"):
      comment_ps[auth_email] = max(comment_ps.get(auth_email, 0), rev_n)

  # Candidate reviewers: assigned or past reviewers who have not approved current PS
  def is_eligible_reviewer(email: str) -> bool:
    if (
        not email
        or email == owner_email
        or email in SELF_EMAILS
        or email.endswith(".gserviceaccount.com")
        or email in removed_emails
        or email in approved_emails
    ):
      return False
    # Non-self CLs only need pings to Googler or Chromium reviewers for second approval
    if not is_self and not email.endswith(("@google.com", "@chromium.org")):
      return False
    return True

  candidate_emails: set[str] = set()
  for acc in reviewer_accounts:
    if isinstance(acc, dict) and acc.get("email"):
      e = str(acc["email"])
      if is_eligible_reviewer(e):
        candidate_emails.add(e)

  for e in plus_one_ps.keys():
    if is_eligible_reviewer(e):
      candidate_emails.add(e)

  pings: list[dict[str, Any]] = []
  base_cl_url = (
      f"https://{host}/c/{project}/+/{cl_id}"
      if project
      else f"https://{host}/c/{cl_id}"
  )

  for e in sorted(candidate_emails):
    last_approved = plus_one_ps.get(e)
    last_comment = comment_ps.get(e)
    prev_ps: int | None = None
    had_approval = False
    reason = ""

    if last_approved is not None and last_approved < ps_num and e not in revoked_emails:
      had_approval = True
      prev_ps = last_approved
      all_rebase = all(p in rebase_ps for p in range(last_approved + 1, ps_num + 1))
      reason = "lost_on_rebase" if all_rebase else "updated_after_approval"
    elif last_comment is not None and last_comment < ps_num:
      prev_ps = last_comment
      reason = "updated_after_review"
    elif approved_count >= 1 and missing_approvals > 0:
      reason = "needs_second_approval"
    elif is_self:
      reason = "awaiting_review"
    else:
      continue

    if prev_ps is not None and prev_ps < ps_num:
      diff_url = f"{base_cl_url}/{prev_ps}..{ps_num}"
    else:
      diff_url = base_cl_url

    pings.append({
        "reviewer": e,
        "reviewer_name": reviewer_names.get(e, e),
        "active": google_account_active(e),
        "in_attention_set": e in att_set,
        "reason": reason,
        "prev_ps": prev_ps,
        "current_ps": ps_num,
        "had_approval": had_approval,
        "diff_url": diff_url,
    })

  return pings


def compute_github_reviewer_pings(
    pr_id: str,
    state: str,
    is_wip: bool,
    is_draft: bool,
    missing_approvals: int,
    approved_count: int,
    is_self: bool,
    is_reviewer: bool,
    human_reviewers: list[str],
    approved_by: set[str],
    changes_requested_by: set[str],
    reviews: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    req_logins: list[str] | None = None,
    repo_slug: str = "flutter/flutter",
) -> list[dict[str, Any]]:
  """Detects reviewers who need to review or re-review on GitHub"""
  if state != "OPEN" or is_wip or is_draft or missing_approvals <= 0:
    return []
  # Allow reviewer pings on PRs we approved or where our prior approval was dismissed on push
  self_had_approval = bool(approved_by & SELF_GITHUB_LOGINS) or any(
      str((r.get("author") or {}).get("login") or "") in SELF_GITHUB_LOGINS
      and str(r.get("state") or "").upper() in ("APPROVED", "DISMISSED")
      for r in reviews
  )
  if not (is_self or (is_reviewer and self_had_approval)):
    return []

  pings: list[dict[str, Any]] = []
  eff_repo = str(repo_slug or "").strip() or "flutter/flutter"
  base_pr_url = f"https://github.com/{eff_repo}/pull/{pr_id}"
  latest_commit_date = ""
  for cm in commits:
    d = str(cm.get("committedDate") or cm.get("authoredDate") or "")
    if d and d > latest_commit_date:
      latest_commit_date = d

  req_set = {u for u in (req_logins or []) if u}
  rev_by_author: dict[str, dict[str, Any]] = {}
  ever_approved_or_dismissed: set[str] = set()
  for r in reviews:
    login = str((r.get("author") or {}).get("login") or "")
    if login and login not in SELF_GITHUB_LOGINS:
      rev_by_author[login] = r
      # Flutter branch protection resets approved reviews to DISMISSED on push
      if str(r.get("state") or "").upper() in ("APPROVED", "DISMISSED"):
        ever_approved_or_dismissed.add(login)

  for login in sorted(set(human_reviewers) - approved_by):
    rev = rev_by_author.get(login)
    had_approval = login in ever_approved_or_dismissed
    in_attention_set = login in req_set
    reason = ""
    if rev:
      sub_date = str(rev.get("submittedAt") or "")
      st = str(rev.get("state") or "").upper()
      # DISMISSED state represents a prior approval invalidated by new commits
      if st == "DISMISSED":
        reason = "updated_after_approval"
      elif latest_commit_date and sub_date and sub_date < latest_commit_date:
        reason = "updated_after_approval" if had_approval else "updated_after_review"
    if not reason:
      # Prior approval dismissed on push still requires a second reviewer
      if (approved_count >= 1 or self_had_approval) and missing_approvals > 0:
        reason = "needs_second_approval"
      elif is_self:
        reason = "awaiting_review"
      else:
        continue

    pings.append({
        "reviewer": login,
        "reviewer_name": login,
        "active": True,
        "in_attention_set": in_attention_set,
        "reason": reason,
        "prev_ps": None,
        "current_ps": 1,
        "had_approval": had_approval,
        "diff_url": f"{base_pr_url}/files",
    })
  return pings


def rerequest_github_reviewers(
    pr_id: str, reviewers: list[str], repo_slug: str = "flutter/flutter"
) -> bool:
  """Re-requests review on a GitHub PR via the requested_reviewers API"""
  clean = [str(u).strip() for u in reviewers if str(u).strip()]
  if not clean:
    return False
  eff_repo = str(repo_slug or "").strip() or "flutter/flutter"
  try:
    proc = subprocess.run(
        [
            "gh",
            "api",
            f"repos/{eff_repo}/pulls/{pr_id}/requested_reviewers",
            "--method",
            "POST",
            "--input",
            "-",
        ],
        input=json.dumps({"reviewers": clean}),
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return proc.returncode == 0
  except Exception:
    return False


def auto_rerequest_github_reviewers(
    pr_id: str,
    head_sha: str,
    pings: list[dict[str, Any]],
    rerequest_ledger: dict[str, Any],
    *,
    do_request: bool = True,
    repo_slug: str = "flutter/flutter",
) -> list[str]:
  """Automatically re-requests GitHub reviewers whose review was updated or dismissed on push"""
  if not pr_id or not head_sha or not pings:
    return []
  to_request: list[str] = []
  # Only re-request reviewers who already reviewed/approved and were dropped from reviewRequests
  rerequest_reasons = {"updated_after_approval", "updated_after_review", "lost_on_rebase"}
  for p in pings:
    rev = str(p.get("reviewer") or "").strip()
    if not rev or p.get("in_attention_set", True):
      continue
    if str(p.get("reason") or "") not in rerequest_reasons:
      continue
    ledger_key = f"{pr_id}:{head_sha}:{rev}"
    if ledger_key in rerequest_ledger:
      p["auto_rerequested"] = True
      p["in_attention_set"] = True
      continue
    if do_request:
      to_request.append(rev)
  if not to_request:
    return []
  eff_repo = str(repo_slug or "").strip() or "flutter/flutter"
  # Omit default repo_slug keyword so 2-arg mocks on flutter/flutter keep working
  ok_req = (
      rerequest_github_reviewers(pr_id, to_request)
      if eff_repo == "flutter/flutter"
      else rerequest_github_reviewers(pr_id, to_request, repo_slug=eff_repo)
  )
  if not ok_req:
    return []
  now_ts = time.time()
  for rev in to_request:
    rerequest_ledger[f"{pr_id}:{head_sha}:{rev}"] = now_ts
  requested_set = set(to_request)
  for p in pings:
    if str(p.get("reviewer") or "").strip() in requested_set:
      p["auto_rerequested"] = True
      p["in_attention_set"] = True
  return to_request


RE_GERRIT_COMMENT_COUNT = re.compile(r"\((\d+)\s+comments?\)", re.IGNORECASE)
RE_GERRIT_BARE_COMMENT_HEADER = re.compile(
    r"^Patch Set \d+:\s*(?:Code-Review[+-]\d+\s*)?(?:\(\d+\s+comments?\))?\s*$",
    re.IGNORECASE,
)


def _parse_gerrit_self_review(
    messages: list[dict[str, Any]], self_cr_vote: int, ps_num: int
) -> tuple[str, int, int]:
  """Extracts (self_review_state, self_review_ps, self_comment_count) from Gerrit messages"""
  state = ""
  review_ps = 0
  comment_count = 0
  for m in messages:
    if not isinstance(m, dict):
      continue
    email = str(((m.get("author") or {}).get("email")) or "")
    if email not in SELF_EMAILS:
      continue
    rev_n = int(m.get("_revision_number") or ps_num)
    txt = str(m.get("message") or "").strip()
    if not txt:
      continue
    for cm in RE_GERRIT_COMMENT_COUNT.finditer(txt):
      comment_count += int(cm.group(1))
    if "Code-Review-1" in txt or "Code-Review-2" in txt:
      state = "changes_requested"
      review_ps = rev_n
    elif "Code-Review+1" in txt or "Code-Review+2" in txt:
      state = "approved"
      review_ps = rev_n
    elif "-Code-Review" in txt:
      state = "commented" if comment_count > 0 else ""
    else:
      body = re.sub(r"^Patch Set \d+:\s*", "", txt).strip()
      if body and not body.startswith("Uploaded patch set"):
        # A comment on a newer patchset after Gerrit reset an older +1/-1 supersedes that vote
        if rev_n > review_ps and self_cr_vote == 0 and state in ("approved", "changes_requested"):
          state = "commented"
          review_ps = rev_n
        elif state not in ("approved", "changes_requested"):
          state = "commented"
          review_ps = max(review_ps, rev_n)
  if self_cr_vote > 0:
    state = "approved"
    if review_ps == 0:
      review_ps = ps_num
  elif self_cr_vote < 0:
    state = "changes_requested"
    if review_ps == 0:
      review_ps = ps_num
  return state, review_ps, comment_count


def fetch_gerrit_comments(
    host: str, cl_id: str, preloaded: dict[str, Any] | None = None
) -> dict[str, list[dict[str, Any]]]:
  """Fetches `/changes/{cl_id}/comments` from Gerrit or returns preloaded test comments"""
  if isinstance(preloaded, dict):
    return preloaded
  try:
    data = _gerrit_http_get_json(host, f"/changes/{cl_id}/comments", timeout=8)
    return data if isinstance(data, dict) else {}
  except Exception:
    return {}


def _analyze_gerrit_self_threads(
    comments_by_path: dict[str, list[dict[str, Any]]], owner_email: str
) -> dict[str, Any]:
  """Summarizes comment threads where the user participated and tracks author replies"""
  res: dict[str, Any] = {
      "self_threads_total": 0,
      "self_threads_unresolved": 0,
      "self_threads_resolved": 0,
      "open_external_threads": 0,
      "author_replied_to_me": False,
      "author_reply_snippet": "",
      "latest_reply_by_author": {},
  }
  if not isinstance(comments_by_path, dict) or not comments_by_path:
    return res

  all_comments: list[dict[str, Any]] = []
  parent_of: dict[str, str] = {}
  for path, clist in comments_by_path.items():
    if not isinstance(clist, list):
      continue
    for c in clist:
      if not isinstance(c, dict) or not c.get("id"):
        continue
      cid = str(c["id"])
      c_copy = dict(c)
      c_copy["_path"] = str(path)
      all_comments.append(c_copy)
      if c.get("in_reply_to"):
        parent_of[cid] = str(c["in_reply_to"])

  def get_root(cid: str) -> str:
    seen: set[str] = set()
    cur = cid
    while cur in parent_of and cur not in seen:
      seen.add(cur)
      cur = parent_of[cur]
    return cur

  threads: dict[str, list[dict[str, Any]]] = {}
  for c in all_comments:
    threads.setdefault(get_root(str(c["id"])), []).append(c)

  latest_author_reply_ts = ""
  latest_by_author: dict[str, tuple[str, str]] = {}
  for t in threads.values():
    t.sort(
        key=lambda x: (
            str(x.get("updated") or ""),
            int(x.get("patch_set") or 0),
        )
    )
    for c in t:
      c_email = str(((c.get("author") or {}).get("email")) or "")
      c_msg = str(c.get("message") or "").strip().replace("\n", " ")
      c_ts = str(c.get("updated") or "")
      if c_email and c_msg:
        prev_ts, _ = latest_by_author.get(c_email, ("", ""))
        if c_ts >= prev_ts:
          latest_by_author[c_email] = (c_ts, c_msg)

    leaf = t[-1]
    root_email = str(((t[0].get("author") or {}).get("email")) or "")
    # Count unresolved threads opened by reviewers/bots other than self for Tier 1 routing
    if root_email not in SELF_EMAILS and bool(leaf.get("unresolved", False)):
      res["open_external_threads"] += 1
    has_self = any(
        str(((c.get("author") or {}).get("email")) or "") in SELF_EMAILS for c in t
    )
    if not has_self:
      continue
    res["self_threads_total"] += 1
    if bool(leaf.get("unresolved", False)):
      res["self_threads_unresolved"] += 1
    else:
      res["self_threads_resolved"] += 1
    last_self_idx = max(
        i
        for i, c in enumerate(t)
        if str(((c.get("author") or {}).get("email")) or "") in SELF_EMAILS
    )
    for reply in t[last_self_idx + 1 :]:
      r_email = str(((reply.get("author") or {}).get("email")) or "")
      if r_email and r_email not in SELF_EMAILS:
        res["author_replied_to_me"] = True
        r_ts = str(reply.get("updated") or "")
        r_msg = str(reply.get("message") or "").strip().replace("\n", " ")
        if r_ts >= latest_author_reply_ts and r_msg:
          latest_author_reply_ts = r_ts
          res["author_reply_snippet"] = r_msg[:180]

  res["latest_reply_by_author"] = {k: v[1] for k, v in latest_by_author.items()}
  return res


# Patterns for Gerrit autogenerated system messages and rebase vote-copy footers
RE_GERRIT_COPIED_VOTES = re.compile(r"\s*Copied Votes:\s*\*[\s\S]*$", re.IGNORECASE)
RE_GERRIT_CODE_OWNERS_MSG = re.compile(
    r"who was added as reviewer owns the following files:", re.IGNORECASE
)
RE_GERRIT_NOISE_BODY = re.compile(
    r"^(?:Uploaded patch set \d+\.?|Patch Set \d+ was rebased\.?|"
    r"Hashtag (?:added|removed):.*)$",
    re.IGNORECASE,
)
GERRIT_NOISE_TAG_PREFIXES = (
    "autogenerated:gerrit:code-owners",
    "autogenerated:gerrit:setHashtag",
)
GERRIT_STALE_CQ_TAG_PREFIXES = (
    "autogenerated:skcq",
    "autogenerated:cq",
)
GERRIT_STALE_CQ_PHRASES = (
    "Removing from SkCQ because verifiers have failed:",
    "This CL failed the SkCQ",
    "passed the SkCQ dry run",
    "SkCQ is trying the patch",
    "This CL is being verified by SkCQ",
    "Failed builds:",
    "Tryjobs failed",
)


def _extract_gerrit_last_message(
    messages: list[dict[str, Any]],
    latest_reply_by_author: dict[str, str] | None = None,
    current_ps: int = 0,
) -> tuple[str, str, str, str]:
  """Returns (author_email, author_name, date, coalesced_text) for the latest Gerrit turn"""
  if not messages:
    return "bot", "bot", "", ""
  eff_ps = max(
      [
          current_ps,
          *(
              int(m.get("_revision_number") or 0)
              for m in messages
              if isinstance(m, dict)
          ),
      ]
  )

  def _clean_msg(m: dict[str, Any]) -> str:
    txt = str(m.get("message") or "").strip().replace("\n", " ")
    for acc in m.get("accounts_in_message") or []:
      if isinstance(acc, dict) and acc.get("_account_id"):
        label = str(acc.get("email") or acc.get("name") or acc["_account_id"])
        txt = txt.replace(f"<GERRIT_ACCOUNT_{acc['_account_id']}>", label)
    # Strip Gerrit rebase vote-copy footers so snippets only show human text
    txt = RE_GERRIT_COPIED_VOTES.sub("", txt).strip()
    return txt

  def _is_noise_msg(m: dict[str, Any], cleaned_txt: str) -> bool:
    tag = str(m.get("tag") or "")
    if any(tag.startswith(p) for p in GERRIT_NOISE_TAG_PREFIXES):
      return True
    body = re.sub(r"^Patch Set \d+:\s*", "", cleaned_txt).strip()
    if not body:
      return True
    # Automated CQ vote-removal notes carry no diagnostic detail alongside the verifier reply
    if body.startswith("Removed Commit-Queue"):
      return True
    # Once a newer patchset exists, older CQ bot verdicts no longer reflect the active code
    msg_ps = int(m.get("_revision_number") or 0)
    if 0 < msg_ps < eff_ps:
      auth_email = str((m.get("author") or {}).get("email") or "").lower()
      # Scope phrase matching to bot accounts so human comments mentioning tryjobs stay visible
      is_bot_msg = (
          not auth_email
          or auth_email == "bot"
          or auth_email.endswith(".gserviceaccount.com")
          or "commit-bot@" in auth_email
          or "gerritcodereview" in auth_email
          or tag.startswith("autogenerated:")
      )
      if any(tag.startswith(p) for p in GERRIT_STALE_CQ_TAG_PREFIXES) or (
          is_bot_msg and any(p in body for p in GERRIT_STALE_CQ_PHRASES)
      ):
        return True
    if RE_GERRIT_CODE_OWNERS_MSG.search(body):
      return True
    if RE_GERRIT_NOISE_BODY.match(body):
      return True
    return False

  # Find the newest message that carries human review content rather than noise
  last_idx = -1
  last_text = ""
  for i in range(len(messages) - 1, -1, -1):
    m = messages[i]
    if not isinstance(m, dict):
      continue
    cleaned = _clean_msg(m)
    if not _is_noise_msg(m, cleaned):
      last_idx = i
      last_text = cleaned
      break

  if last_idx < 0:
    fallback = messages[-1] if isinstance(messages[-1], dict) else {}
    auth_obj = fallback.get("author") or {}
    return (
        str(auth_obj.get("email") or "bot"),
        str(auth_obj.get("name") or ""),
        str(fallback.get("date") or ""),
        "",
    )

  last_msg = messages[last_idx]
  auth_obj = last_msg.get("author") or {}
  last_author = str(auth_obj.get("email") or "bot")
  last_name = str(auth_obj.get("name") or "")
  last_date = str(last_msg.get("date") or "")

  if RE_GERRIT_BARE_COMMENT_HEADER.match(last_text):
    parts: list[str] = []
    for prev in reversed(messages[:last_idx]):
      if not isinstance(prev, dict):
        continue
      p_auth = prev.get("author") or {}
      p_email = str(p_auth.get("email") or "")
      if p_email != last_author:
        break
      if not last_name and p_auth.get("name"):
        last_name = str(p_auth["name"])
      p_txt = _clean_msg(prev)
      # Skip autogenerated code-owners and hashtag notes when coalescing bare headers
      if _is_noise_msg(prev, p_txt):
        continue
      p_body = re.sub(r"^Patch Set \d+:\s*", "", p_txt).strip()
      if (
          p_body
          and not RE_GERRIT_BARE_COMMENT_HEADER.match(p_txt)
          and not re.match(r"^Uploaded patch set \d+\.?$", p_body, re.IGNORECASE)
      ):
        parts.append(p_body)
        break
    inline_reply = (latest_reply_by_author or {}).get(last_author, "").strip()
    if inline_reply and inline_reply not in parts:
      parts.append(f"({inline_reply})" if parts else inline_reply)
    if parts:
      prefix_m = re.match(r"^(Patch Set \d+:)", last_text)
      prefix = f"{prefix_m.group(1)} " if prefix_m else ""
      last_text = f"{prefix}{' · '.join(parts)}"
  return last_author, last_name, last_date, last_text


_SKIA_CQ_SYNC_CACHE: dict[tuple[str, int], bool] = {}
_SKIA_CQ_SYNC_LOCK = threading.Lock()


def check_skia_cq_sync_infra_failure(cl_id: str, ps_num: int) -> bool:
  """Returns True when Skia tryjobs on (cl_id, ps_num) failed solely on pre-task sync/rebase"""
  if not str(cl_id).isdigit() or ps_num <= 0:
    return False
  cache_key = (str(cl_id), int(ps_num))
  with _SKIA_CQ_SYNC_LOCK:
    if cache_key in _SKIA_CQ_SYNC_CACHE:
      return _SKIA_CQ_SYNC_CACHE[cache_key]
  payload = json.dumps({
      "predicate": {
          "gerritChanges": [{
              "host": GERRIT_SKIA_HOST,
              "project": "skia",
              "change": int(cl_id),
              "patchset": int(ps_num),
          }]
      },
      "pageSize": 25,
      "fields": "builds.*.status,builds.*.summaryMarkdown",
  }).encode("utf-8")
  req = urllib.request.Request(
      "https://cr-buildbucket.appspot.com/prpc/buildbucket.v2.Builds/SearchBuilds",
      data=payload,
      headers={
          "Content-Type": "application/json",
          "Accept": "application/json",
          "User-Agent": "CLFlightDeck/1.0",
      },
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=6) as resp:
      raw = resp.read().decode("utf-8", errors="replace").strip()
    if raw.startswith(")]}'"):
      raw = raw[4:].strip()
    if not raw:
      return False
    data = json.loads(raw)
  except (OSError, TimeoutError, ValueError, http.client.HTTPException):
    return False
  if not isinstance(data, dict):
    return False
  builds = data.get("builds")
  if not isinstance(builds, list) or not builds:
    return False
  has_sync_infra = False
  has_pending = False
  for b in builds:
    if not isinstance(b, dict):
      continue
    st = str(b.get("status") or "")
    # Any real compile/test FAILURE takes precedence over pre-task sync infra errors
    if st == "FAILURE":
      with _SKIA_CQ_SYNC_LOCK:
        if len(_SKIA_CQ_SYNC_CACHE) >= 512:
          _SKIA_CQ_SYNC_CACHE.clear()
        _SKIA_CQ_SYNC_CACHE[cache_key] = False
      return False
    if st in ("SCHEDULED", "STARTED"):
      has_pending = True
    elif st == "INFRA_FAILURE":
      low = str(b.get("summaryMarkdown") or b.get("summary_markdown") or "").lower()
      if "not mergeable" in low or (
          "failed to start job: syncing" in low and "gclient.py sync" in low
      ):
        has_sync_infra = True
  if not has_pending:
    with _SKIA_CQ_SYNC_LOCK:
      if len(_SKIA_CQ_SYNC_CACHE) >= 512:
        _SKIA_CQ_SYNC_CACHE.clear()
      _SKIA_CQ_SYNC_CACHE[cache_key] = has_sync_infra
  return has_sync_infra


def fetch_gerrit_cl_status(
    cl_id: str,
    rollers: dict[str, Any],
    incidents: list[dict[str, Any]],
    skia_dir: str = DEFAULT_SKIA_DIR,
    preferred_host: str = "",
) -> dict[str, Any]:
  """Probes Skia and Chromium Gerrit for a CL."""
  if preferred_host in GERRIT_HOSTS:
    hosts = (preferred_host, *[h for h in GERRIT_HOSTS if h != preferred_host])
  elif cl_id.isdigit() and int(cl_id) >= 4_000_000:
    # Chromium Gerrit IDs are in the millions while Skia Gerrit IDs are ~1.4M
    hosts = (GERRIT_CHROMIUM_HOST, GERRIT_SKIA_HOST)
  else:
    hosts = GERRIT_HOSTS

  for host in hosts:
    try:
      opts = (
          # Request ALL_COMMITS so non-current patchsets retain commit.parents for rebase diffing
          "o=CURRENT_REVISION&o=ALL_COMMITS&o=CURRENT_FILES&o=ALL_REVISIONS"
          "&o=MESSAGES&o=DETAILED_ACCOUNTS&o=LABELS&o=SUBMIT_REQUIREMENTS"
      )
      d = _gerrit_http_get_json(
          host, f"/changes/{cl_id}/detail?{opts}", timeout=8
      )

      status = str(d.get("status") or "NEW")
      subject = str(d.get("subject") or "")
      owner_obj = d.get("owner") or {}
      owner_email = str(owner_obj.get("email") or "")
      owner_name = str(owner_obj.get("name") or owner_email)
      is_self = owner_email in SELF_EMAILS
      is_external = (
          not owner_email.endswith("@google.com")
          and not owner_email.endswith("@chromium.org")
      )

      cur_sha = str(d.get("current_revision") or "")
      revs = d.get("revisions") or {}
      cur_rev = revs.get(cur_sha) or {}
      ps_num = int(cur_rev.get("_number") or 1)
      cur_parents = ((cur_rev.get("commit") or {}).get("parents")) or []
      parent_commit = (
          str(cur_parents[0].get("commit") or "")
          if cur_parents and isinstance(cur_parents[0], dict)
          else ""
      )
      revision_ps_by_sha = {
          str(sha): int(r.get("_number") or 0)
          for sha, r in revs.items()
          if isinstance(r, dict) and r.get("_number")
      }
      revisions_by_ps: dict[int, dict[str, str]] = {}
      for sha, r in revs.items():
        # Skip malformed revision entries that lack a patchset number
        if not isinstance(r, dict) or not r.get("_number"):
          continue
        r_num = int(r["_number"])
        r_parents = ((r.get("commit") or {}).get("parents")) or []
        # Extract first parent commit SHA to detect upstream module overlap across rebases
        r_parent = (
            str(r_parents[0].get("commit") or "")
            if r_parents and isinstance(r_parents[0], dict)
            else ""
        )
        revisions_by_ps[r_num] = {
            "sha": str(sha),
            "kind": str(r.get("kind") or ""),
            "parent": r_parent,
        }
      commit_msg = str(((cur_rev.get("commit") or {}).get("message")) or "")
      files_dict = cur_rev.get("files") or {}
      files = sorted(str(f) for f in files_dict.keys() if f != "/COMMIT_MSG")
      hashtags = [str(h) for h in (d.get("hashtags") or []) if h]
      topic = str(d.get("topic") or "")
      branch = re.sub(r"^refs/heads/", "", str(d.get("branch") or "").strip())
      cherry_pick_of_change = (
          str(d["cherry_pick_of_change"])
          if d.get("cherry_pick_of_change")
          else ""
      )
      cherry_pick_of_patch_set = int(d.get("cherry_pick_of_patch_set") or 0)

      att_set = [
          str((v.get("account") or {}).get("email") or "")
          for v in (d.get("attention_set") or {}).values()
      ]
      self_att_reasons = [
          str(v.get("reason") or "")
          for v in (d.get("attention_set") or {}).values()
          if isinstance(v, dict)
          and str((v.get("account") or {}).get("email") or "") in SELF_EMAILS
          and v.get("reason")
      ]
      attention_reason = self_att_reasons[-1] if self_att_reasons else ""
      in_my_attention = any(e in SELF_EMAILS for e in att_set)
      # Only my own changes track which reviewers are blocking submission
      waiting_on = (
          [
              {"id": e, "active": google_account_active(e)}
              for e in att_set
              # Exclude self and bot accounts from reviewer ping targets
              if e and e not in SELF_EMAILS and not e.endswith(".gserviceaccount.com")
          ]
          if is_self
          else []
      )
      # Removing yourself from reviewers records state="REMOVED" in reviewer_updates
      self_updates = [
          u
          for u in (d.get("reviewer_updates") or [])
          if isinstance(u, dict)
          and str((u.get("reviewer") or {}).get("email") or "") in SELF_EMAILS
      ]
      reviewer_removed = (
          not is_self
          and bool(self_updates)
          and str(self_updates[-1].get("state") or "") == "REMOVED"
      )
      removed_at = (
          str(self_updates[-1].get("updated") or "") if reviewer_removed else ""
      )
      if reviewer_removed:
        in_my_attention = False
        waiting_on = []
      messages = d.get("messages") or []
      reviewer_accounts = (d.get("reviewers") or {}).get("REVIEWER") or []
      cc_accounts = (d.get("reviewers") or {}).get("CC") or []
      reviewer_emails = {
          str(r.get("email") or "")
          for r in reviewer_accounts
          if isinstance(r, dict)
      }
      cc_emails = {
          str(r.get("email") or "")
          for r in cc_accounts
          if isinstance(r, dict)
      }

      labels = d.get("labels") or {}
      cr_votes = [
          (str(a.get("email") or ""), int(a.get("value") or 0))
          for a in ((labels.get("Code-Review") or {}).get("all") or [])
          if a.get("value")
      ]
      cq_votes = [
          (str(a.get("email") or ""), int(a.get("value") or 0))
          for a in ((labels.get("Commit-Queue") or {}).get("all") or [])
          if a.get("value")
      ]
      max_cr = max([v for _, v in cr_votes], default=0)
      min_cr = min([v for _, v in cr_votes], default=0)
      max_cq = max([v for _, v in cq_votes], default=0)
      self_cr_vote = max((v for e, v in cr_votes if e in SELF_EMAILS), default=0)

      self_review_state, self_review_ps, self_comment_count = (
          _parse_gerrit_self_review(messages, self_cr_vote, ps_num)
      )

      is_reviewer = not is_self and (
          bool(reviewer_emails & SELF_EMAILS)
          or (bool(cc_emails & SELF_EMAILS) and bool(self_review_state))
          or bool(self_review_state)
      )
      role = (
          "author"
          if is_self
          else (
              # Preserve reviewer role for author-column labels even when in Watching
              "watching"
              if (not is_reviewer and not reviewer_removed)
              else ("reviewer (ext)" if is_external else "reviewer (goog)")
          )
      )
      # Exclude the owner and service accounts when checking whether review has started
      non_owner_reviewers = [
          str(r.get("email") or r.get("name") or "")
          for r in reviewer_accounts
          if isinstance(r, dict)
          and str(r.get("email") or "") != owner_email
          and not (is_self and str(r.get("email") or "") in SELF_EMAILS)
          and not str(r.get("email") or "").endswith(".gserviceaccount.com")
          and (r.get("email") or r.get("name"))
      ]
      is_wip = status == "NEW" and (
          bool(d.get("work_in_progress"))
          or bool(RE_DRAFT_SUBJECT.match(subject))
      )
      # Distinguish explicit Gerrit WIP flag from subject-prefix draft markers
      wip_label = (
          "WIP"
          if (status == "NEW" and bool(d.get("work_in_progress")))
          else ("Draft" if is_wip else "")
      )
      # Draft and in-development buckets track only my own pre-review changes
      is_draft = is_self and is_wip
      in_development = (
          is_self
          and status == "NEW"
          and not is_draft
          and not non_owner_reviewers
      )
      # External authors on Skia and Chromium Gerrit need 2 Googler approvals (Review-Enforcement)
      required_approvals = 2 if is_external else 1
      # External Gerrit Review-Enforcement counts only @google.com votes toward 2 approvals
      approved_emails = {
          e
          for e, v in cr_votes
          if v > 0
          and e
          and e != owner_email
          and (not is_external or e.lower().endswith("@google.com"))
      }
      approved_count = len(approved_emails)
      self_active_reviewer = (
          {"jlavrova@google.com"}
          if (
              is_reviewer
              and (
                  reviewer_emails & SELF_EMAILS
                  or cc_emails & SELF_EMAILS
                  or bool(self_review_state)
              )
          )
          else set()
      )
      # External Gerrit Review-Enforcement counts only @google.com reviewers toward 2 approvals
      eligible_assigned_reviewers = {
          e
          for e in (set(non_owner_reviewers) | approved_emails | self_active_reviewer)
          if e and (not is_external or e.lower().endswith("@google.com"))
      }
      assigned_reviewers_count = len(eligible_assigned_reviewers)
      missing_reviewers_to_assign = (
          max(0, required_approvals - assigned_reviewers_count)
          if status == "NEW"
          else 0
      )
      submit_reqs = d.get("submit_requirements") or []
      unsatisfied_review_reqs = [
          str(sr.get("name") or "")
          for sr in submit_reqs
          if isinstance(sr, dict)
          and str(sr.get("status") or "") == "UNSATISFIED"
          and str(sr.get("name") or "")
          in ("Code-Review", "Review-Enforcement", "Owners-Override")
      ]
      missing_approvals = (
          max(
              0,
              required_approvals - approved_count,
              1 if (unsatisfied_review_reqs and min_cr >= 0) else 0,
          )
          if status == "NEW"
          else 0
      )
      if status != "NEW" or min_cr < 0:
        reviewer_status = ""
      elif assigned_reviewers_count == 0:
        reviewer_status = (
            f"Needs {required_approvals} reviewers"
            if required_approvals > 1
            else "No reviewers assigned"
        )
      elif missing_reviewers_to_assign > 0:
        reviewer_status = (
            f"Needs +{missing_reviewers_to_assign} more reviewer"
            f"{'s' if missing_reviewers_to_assign > 1 else ''} "
            f"({assigned_reviewers_count}/{required_approvals} assigned)"
        )
      elif missing_approvals > 0:
        reviewer_status = (
            f"Needs +{missing_approvals} CR ({approved_count}/{required_approvals} approved)"
            if required_approvals > 1
            else "Awaiting CR+1"
        )
      else:
        reviewer_status = "Ready to submit"
      # Once I approved a non-self CL on the current code revision, any remaining review
      # or dry-run attention belongs to the author or second reviewer
      if not is_self and self_cr_vote > 0:
        in_my_attention = False
      # WIP and unreviewed CLs keep participants in attention_set without needing a reply
      if is_wip or in_development:
        in_my_attention = False
        waiting_on = []
      # Non-self changes not currently in my attention sit in Watching unless I reviewed them
      is_watching = (
          not is_self
          and not reviewer_removed
          and not in_my_attention
          and not bool(self_cr_vote > 0)
          and self_review_state not in ("approved", "commented", "changes_requested")
      )
      merge_conflict = status == "NEW" and not gerrit_mergeable(host, cl_id)

      comments_by_path: dict[str, list[dict[str, Any]]] = {}
      has_any_inline_comments = any(
          bool(RE_GERRIT_COMMENT_COUNT.search(str(m.get("message") or "")))
          for m in messages
          if isinstance(m, dict)
      )
      if (
          isinstance(d.get("_comments"), dict)
          or self_comment_count > 0
          or "comment" in attention_reason.lower()
          or (status == "NEW" and has_any_inline_comments)
      ):
        comments_by_path = fetch_gerrit_comments(
            host, cl_id, preloaded=d.get("_comments")
        )
      thread_stats = _analyze_gerrit_self_threads(
          comments_by_path, owner_email
      )

      self_approval_ps = 0
      for m in messages:
        if not isinstance(m, dict):
          continue
        m_email = str(((m.get("author") or {}).get("email")) or "")
        if m_email not in SELF_EMAILS:
          continue
        m_txt = str(m.get("message") or "")
        if "Code-Review+1" in m_txt or "Code-Review+2" in m_txt:
          self_approval_ps = int(m.get("_revision_number") or ps_num)
        elif "-Code-Review" in m_txt or "Code-Review-1" in m_txt:
          self_approval_ps = 0
      if self_cr_vote > 0 and self_approval_ps == 0:
        self_approval_ps = ps_num

      # Sponsor responsibility covers our own commits, external authors we reviewed,
      # departed Googlers, and approved changes inside our text modules
      self_approved = bool(
          self_cr_vote > 0
          or self_approval_ps > 0
          or (approved_emails & SELF_EMAILS)
      )
      is_sponsored_external = bool(
          not is_self and is_external and (self_approved or is_reviewer)
      )
      touches_my_text_dirs = any(
          f.startswith(
              ("modules/skparagraph/", "modules/skunicode/", "modules/skshaper/")
          )
          for f in files
      )
      should_own_post_submit_alert = bool(
          is_self
          or is_sponsored_external
          or (not is_external and not google_account_active(owner_email))
          or (self_approved and touches_my_text_dirs)
      )
      # Only page on external pre-submit CQ failures when CR+1 is active on the current PS
      should_own_cq_alert = bool(is_self or (is_external and self_cr_vote > 0))

      last_msg_author, last_msg_name, last_msg_date, last_msg_text = (
          _extract_gerrit_last_message(
              messages,
              thread_stats.get("latest_reply_by_author"),
              current_ps=ps_num,
          )
      )
      last_msg_author_name = last_msg_name or (
          owner_name if last_msg_author == owner_email else ""
      )

      # Gather candidate timestamps across attention set, votes, and patchsets
      updated_raw = str(d.get("updated") or "")
      created_raw = str(d.get("created") or "")
      rev_created_raw = str(cur_rev.get("created") or "")
      self_att_dates = [
          str(v.get("last_update") or "")
          for v in (d.get("attention_set") or {}).values()
          if isinstance(v, dict)
          and str((v.get("account") or {}).get("email") or "") in SELF_EMAILS
      ]
      other_att_dates = [
          str(v.get("last_update") or "")
          for v in (d.get("attention_set") or {}).values()
          if isinstance(v, dict)
          and str((v.get("account") or {}).get("email") or "") not in SELF_EMAILS
      ]
      cq_vote_dates = [
          str(a.get("date") or "")
          for a in ((labels.get("Commit-Queue") or {}).get("all") or [])
          if isinstance(a, dict) and a.get("value")
      ]

      cq_failed = False
      cq_sync_rebase_needed = False
      cq_status = "Idle"
      cq_msg_date = ""
      if max_cq == 2:
        cq_status = "CQ+2 (Submitting)"
      elif max_cq == 1:
        cq_status = "CQ+1 (Dry Run)"
      # Scan all messages on ps_num so later inline replies do not erase a CQ verdict
      for m in reversed(messages):
        if not isinstance(m, dict) or int(m.get("_revision_number") or 0) != ps_num:
          continue
        txt = str(m.get("message") or "")
        if (
            "Removing from SkCQ because verifiers have failed:" in txt
            or "This CL failed the SkCQ" in txt
            or "Failed builds:" in txt
            or "Tryjobs failed" in txt
        ):
          if max_cq == 0:
            cq_failed = True
            cq_status = "🔴 CQ Failed"
            cq_msg_date = str(m.get("date") or "")
          break
        if "passed the SkCQ dry run" in txt and max_cq == 0:
          cq_status = "✓ Dry Run Passed"
          cq_msg_date = str(m.get("date") or "")
          break
      if cq_failed and host == GERRIT_SKIA_HOST and status == "NEW":
        cq_sync_rebase_needed = check_skia_cq_sync_infra_failure(cl_id, ps_num)
        if cq_sync_rebase_needed:
          cq_status = "🔴 CQ Sync Failed (Rebase)"

      # External authors cannot add a 2nd Googler or vote CQ+2 without Googler sponsor action
      sponsor_action = ""
      sponsor_action_reason = ""
      if (
          status == "NEW"
          and not is_self
          and self_cr_vote > 0
          and not is_wip
          and not reviewer_removed
          and min_cr >= 0
          and not cq_failed
          and max_cq == 0
      ):
        any_unsatisfied_sr = any(
            isinstance(sr, dict) and str(sr.get("status") or "") == "UNSATISFIED"
            for sr in submit_reqs
        )
        # Review-Enforcement blocks external submit until a 2nd Googler is assigned
        if is_external and missing_reviewers_to_assign > 0:
          sponsor_action = "assign_second_reviewer"
          sponsor_action_reason = "Add 2nd Googler reviewer"
        # External Skia contributors lack Commit-Queue+2 permission even after 2/2 approvals
        elif (
            host == GERRIT_SKIA_HOST
            and missing_approvals == 0
            and submit_reqs
            and not any_unsatisfied_sr
        ):
          if not merge_conflict:
            sponsor_action = "submit_cq"
            sponsor_action_reason = (
                f"Ready for Googler CQ+2 ({approved_count}/{required_approvals} CR+1)"
            )
          # Mute the rebase prompt once we already asked the author to rebase in the latest reply
          elif not (last_msg_author in SELF_EMAILS and "rebase" in last_msg_text.lower()):
            sponsor_action = "submit_cq_conflict"
            sponsor_action_reason = (
                f"Approved ({approved_count}/{required_approvals} CR+1)"
                " — rebase in Gerrit or ask author to rebase"
            )

      submitted_raw = str(d.get("submitted") or "")
      submitted_et, submitted_age = format_et_and_age(submitted_raw)
      last_msg_et, last_msg_age = format_et_and_age(last_msg_date)

      # Anchor status change timestamp to the event driving the current phase
      if status == "MERGED":
        status_changed_raw = submitted_raw or latest_timestamp(
            last_msg_date, updated_raw
        )
      elif status == "ABANDONED":
        status_changed_raw = latest_timestamp(last_msg_date, updated_raw)
      # Alert on pre-submit CQ failures for our own changes or sponsored external CLs
      elif cq_failed and should_own_cq_alert:
        status_changed_raw = cq_msg_date or latest_timestamp(
            last_msg_date, updated_raw
        )
      elif max_cq in (1, 2):
        status_changed_raw = latest_timestamp(
            *cq_vote_dates, cq_msg_date, last_msg_date, updated_raw
        )
      elif in_my_attention:
        status_changed_raw = latest_timestamp(
            *self_att_dates, last_msg_date, updated_raw
        )
      else:
        status_changed_raw = latest_timestamp(
            rev_created_raw,
            *other_att_dates,
            last_msg_date,
            updated_raw,
            created_raw,
        )
      status_changed_et, status_changed_age = format_et_and_age(
          status_changed_raw
      )

      post_submit = None
      if status == "MERGED" and host == "skia-review.googlesource.com":
        post_submit = check_skia_post_submit(
            cur_sha,
            rollers,
            incidents,
            skia_dir=skia_dir,
            cl_id=cl_id,
            is_self=is_self,
            is_external=is_external,
        )

      if status == "ABANDONED":
        phase = "ABANDONED"
      elif status == "MERGED":
        if post_submit:
          if post_submit["has_alert"]:
            if should_own_post_submit_alert:
              phase = "ALERT_ROLL_OR_STATUS"
            else:
              # Active internal colleague's CL outside our text dirs stays in Landing
              phase = "POST_SUBMIT_ROLLING"
          elif post_submit["all_passed"]:
            phase = "READY_TO_CLOSE"
          else:
            phase = "POST_SUBMIT_ROLLING"
        else:
          phase = "READY_TO_CLOSE"
      else:
        # Pre-submit CQ failures belong to the CL author or sponsoring reviewer
        if cq_failed and should_own_cq_alert:
          phase = "ALERT_CQ_FAILED"
        elif max_cq == 2:
          phase = "SUBMITTING"
        elif in_my_attention:
          phase = "NEEDS_YOUR_ACTION"
        else:
          phase = "WAITING_ON_OTHERS"

      # Exclude reviewers who were explicitly removed from the change
      removed_emails = {
          str((u.get("reviewer") or {}).get("email") or "")
          for u in (d.get("reviewer_updates") or [])
          if isinstance(u, dict) and str(u.get("state") or "") == "REMOVED"
      }
      suggested_second_reviewers: list[dict[str, str]] = []
      # Only Skia uses a root-wide '*' OWNERS file; Chromium enforces directory-specific OWNERS
      if (sponsor_action == "assign_second_reviewer" or (is_external and required_approvals > 1 and assigned_reviewers_count > 0 and self_cr_vote <= 0)) and host == GERRIT_SKIA_HOST:
        # CanvasKit, sfnt, and port-only CLs fit Florin best; text modules fit Alexis first
        prefer_florin = (
            any(
                f.startswith(("modules/canvaskit/", "src/sfnt/", "src/ports/"))
                for f in files
            )
            and not touches_my_text_dirs
        )
        ordered_candidates = (
            tuple(reversed(SECOND_REVIEWER_CANDIDATES))
            if prefer_florin
            else SECOND_REVIEWER_CANDIDATES
        )
        excluded_second = {
            e.lower()
            for e in (
                eligible_assigned_reviewers
                | removed_emails
                | {owner_email}
                | SELF_EMAILS
            )
            if e
        }
        for cand in ordered_candidates:
          c_email = cand["email"]
          if c_email.lower() in excluded_second:
            continue
          if not google_account_active(c_email):
            continue
          suggested_second_reviewers.append(dict(cand))
      project = str(d.get("project") or "")
      if missing_reviewers_to_assign > 0 and is_external and required_approvals > 1 and assigned_reviewers_count > 0 and self_cr_vote <= 0:
        suffix = (
            f"({' / '.join(r['short'] for r in suggested_second_reviewers)})"
            if suggested_second_reviewers
            else f"({assigned_reviewers_count}/{required_approvals})"
        )
        reviewer_status = f"After your CR+1: +1 Googler {suffix}"

      # Compute reviewer ping candidates for lost +1s and missing second approvals
      reviewer_pings = compute_gerrit_reviewer_pings(
          host=host,
          cl_id=cl_id,
          project=project,
          status=status,
          is_wip=is_wip,
          is_draft=is_draft,
          min_cr=min_cr,
          missing_approvals=missing_approvals,
          approved_count=approved_count,
          approved_emails=approved_emails,
          is_self=is_self,
          is_reviewer=is_reviewer,
          ps_num=ps_num,
          revs=revs,
          messages=messages,
          reviewer_accounts=reviewer_accounts,
          cr_votes=cr_votes,
          att_set=att_set,
          owner_email=owner_email,
          removed_emails=removed_emails,
      )

      return {
          "ok": True,
          "host": host,
          "url": f"https://{host}/c/{cl_id}",
          "status": status,
          "phase": phase,
          "subject": subject,
          "owner_name": owner_name,
          "owner_email": owner_email,
          "role": role,
          "is_self": is_self,
          "is_external": is_external,
          "is_sponsored_external": is_sponsored_external,
          "is_draft": is_draft,
          "is_wip": is_wip,
          "wip_label": wip_label,
          "in_development": in_development,
          "is_watching": is_watching,
          "reviewer_removed": reviewer_removed,
          "removed_at": removed_at,
          "required_approvals": required_approvals,
          "approved_count": approved_count,
          "assigned_reviewers_count": assigned_reviewers_count,
          "assigned_reviewers": sorted(eligible_assigned_reviewers - SELF_EMAILS),
          "missing_reviewers_to_assign": missing_reviewers_to_assign,
          "missing_approvals": missing_approvals,
          "reviewer_status": reviewer_status,
          "sponsor_action": sponsor_action,
          "sponsor_action_reason": sponsor_action_reason,
          "suggested_second_reviewers": suggested_second_reviewers,
          # Per-reviewer ping entries for lost +1s and missing 2nd approvals
          "reviewer_pings": reviewer_pings,
          "self_cr_vote": self_cr_vote,
          "self_approved": bool(self_cr_vote > 0),
          "self_approval_ps": self_approval_ps,
          "project": str(d.get("project") or ""),
          "parent_commit": parent_commit,
          "revision_ps_by_sha": revision_ps_by_sha,
          "revisions_by_ps": revisions_by_ps,
          "patchset": ps_num,
          "revision": cur_sha,
          "commit_message": commit_msg,
          "files": files,
          "hashtags": hashtags,
          "topic": topic,
          "branch": branch,
          "cherry_pick_of_change": cherry_pick_of_change,
          "cherry_pick_of_patch_set": cherry_pick_of_patch_set,
          "commit_url": (
              f"https://{host.replace('-review', '')}/{d.get('project')}/+/{cur_sha}"
              if status == "MERGED" and d.get("project")
              else ""
          ),
          "in_my_attention": in_my_attention,
          "waiting_on": waiting_on,
          "merge_conflict": merge_conflict,
          "tree_red": False,
          "cr_summary": (
              f"+{max_cr} ({approved_count}/{required_approvals})"
              if (
                  max_cr > 0
                  and required_approvals > 1
                  and approved_count < required_approvals
              )
              else (
                  f"+{max_cr}"
                  if max_cr > 0
                  else (str(min_cr) if min_cr < 0 else "0")
              )
          ),
          "cq_status": cq_status,
          "cq_failed": cq_failed,
          "cq_sync_rebase_needed": cq_sync_rebase_needed,
          "attention_reason": attention_reason,
          "self_review_state": self_review_state,
          "self_review_ps": self_review_ps,
          "self_comment_count": self_comment_count,
          "self_threads_total": thread_stats["self_threads_total"],
          "self_threads_unresolved": thread_stats["self_threads_unresolved"],
          "self_threads_resolved": thread_stats["self_threads_resolved"],
          "open_external_threads": thread_stats["open_external_threads"],
          "author_replied_to_me": thread_stats["author_replied_to_me"],
          "author_reply_snippet": thread_stats["author_reply_snippet"],
          "last_msg_author": last_msg_author,
          "last_msg_author_name": last_msg_author_name,
          "last_msg_date": last_msg_date,
          "last_msg_et": last_msg_et,
          "last_msg_age": last_msg_age,
          "last_msg_snippet": last_msg_text[:180],
          # Creation timestamp bounds the window for correlating follow-up fix CLs
          "created": created_raw,
          "submitted": submitted_raw,
          "submitted_et": submitted_et,
          "submitted_age": submitted_age,
          "status_changed_raw": status_changed_raw,
          "status_changed_et": status_changed_et,
          "status_changed_age": status_changed_age,
          "post_submit": post_submit,
      }
    except urllib.error.HTTPError as e:
      # Only HTTP 404 may fall through to the second Gerrit host; transient failures
      # must abort rather than matching a colliding CL ID on another Gerrit instance
      if e.code == 404:
        continue
      return {"ok": False, "error": f"CL {cl_id} HTTP {e.code} on {host}"}
    except (OSError, TimeoutError, ValueError, http.client.HTTPException) as e:
      return {"ok": False, "error": f"CL {cl_id} error on {host}: {e}"}
  return {"ok": False, "error": f"CL {cl_id} not found on Gerrit"}


def fetch_commit_failed_checks(
    sha_or_ref: str, failing: set[str]
) -> tuple[bool, set[str]]:
  """Returns (ok, failed_names) from GitHub check-runs on `sha_or_ref`."""
  try:
    out = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/flutter/flutter/commits/{sha_or_ref}/check-runs?per_page=100",
        ],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    d = json.loads(out)
    runs = d.get("check_runs")
    # Non-check-run payloads (e.g. unit test mocks returning a PR fixture) signal
    # that baseline subtraction should be skipped
    if not isinstance(runs, list):
      return False, set()
    # Keep each check name's latest run by id so a green rerun clears an earlier failure
    latest: dict[str, tuple[int, str]] = {}
    for r in runs:
      if not isinstance(r, dict):
        continue
      name = str(r.get("name") or "")
      if not name:
        continue
      rid = int(r.get("id") or 0)
      conc = str(r.get("conclusion") or r.get("state") or "").upper()
      prev = latest.get(name)
      # Higher check-run id means a newer attempt on the same commit
      if prev is None or rid >= prev[0]:
        latest[name] = (rid, conc)
    return True, {name for name, (_, conc) in latest.items() if conc in failing}
  except Exception:
    return False, set()


def fetch_flutter_dashboard_json(url: str) -> dict[str, Any]:
  """Fetches JSON from flutter-dashboard, falling back to IPv4 curl on IPv6 timeout."""
  try:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=8) as resp:
      return json.loads(resp.read().decode("utf-8"))
  except Exception:
    # Python's urlopen can exhaust its timeout on blackholed IPv6 routes before trying IPv4
    try:
      out = subprocess.check_output(
          ["curl", "-4", "-fsSL", "--max-time", "5", url],
          stderr=subprocess.DEVNULL,
          text=True,
          timeout=6,
      )
      return json.loads(out)
    except Exception:
      return {}


def fetch_flutter_tree_failures(failing: set[str]) -> tuple[bool, set[str]]:
  """Returns (ok, failing_tasks) across flutter/flutter master and Cocoon build-status."""
  now = time.time()
  with _FLUTTER_TREE_LOCK:
    # Reuse tip-of-tree status across all PRs within the same polling cycle
    if (
        _FLUTTER_TREE_CACHE["expires_at"] > now
        and _FLUTTER_TREE_CACHE["tasks"] is not None
    ):
      return True, set(_FLUTTER_TREE_CACHE["tasks"])

  ok, broken = fetch_commit_failed_checks("master", failing)
  # Abort baseline lookup when GitHub check-runs are unavailable
  if not ok:
    return False, set()
  bdata = fetch_flutter_dashboard_json(FLUTTER_BUILD_STATUS_URL)
  # Merge Cocoon post-submit failures with GitHub Actions check-run failures on master
  for task in bdata.get("failingTasks") or []:
    if task:
      broken.add(str(task))

  with _FLUTTER_TREE_LOCK:
    _FLUTTER_TREE_CACHE["tasks"] = set(broken)
    _FLUTTER_TREE_CACHE["expires_at"] = now + 45.0
  return True, broken


def fetch_pr_base_failed_checks(pr_id: str, failing: set[str]) -> set[str]:
  """Fetches check-run and Cocoon task names that failed on the PR's base commit."""
  try:
    out = subprocess.check_output(
        ["gh", "api", f"repos/flutter/flutter/pulls/{pr_id}"],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    d = json.loads(out)
    base_sha = str((d.get("base") or {}).get("sha") or "")
    if not base_sha:
      return set()
    with _FLUTTER_TREE_LOCK:
      cached = _FLUTTER_BASE_CACHE.get(base_sha)
    # A commit's post-submit result rarely changes once recorded, so serve from cache
    if cached is not None:
      return set(cached)
    ok, failed = fetch_commit_failed_checks(base_sha, failing)
    sdata = fetch_flutter_dashboard_json(FLUTTER_GET_STATUS_URL)
    # Also include Cocoon post-submit tasks that failed on this exact base commit
    for entry in sdata.get("Commits") or []:
      commit_sha = str(((entry.get("Commit") or {}).get("Sha")) or "")
      if commit_sha == base_sha:
        for task in entry.get("Tasks") or []:
          if str(task.get("Status") or "") in ("Failed", "Infra Failure"):
            bname = str(task.get("BuilderName") or "")
            if bname:
              failed.add(bname)
        break
    if ok:
      with _FLUTTER_TREE_LOCK:
        if len(_FLUTTER_BASE_CACHE) >= 64:
          _FLUTTER_BASE_CACHE.clear()
        _FLUTTER_BASE_CACHE[base_sha] = set(failed)
    return failed
  except Exception:
    return set()


def fetch_dashboard_failed_subjobs(head_sha: str) -> list[str]:
  """Extracts failing presubmit job names from Cocoon's `Dashboard Checks` summary."""
  if not head_sha:
    return []
  try:
    out = subprocess.check_output(
        [
            "gh",
            "api",
            f"repos/flutter/flutter/commits/{head_sha}/check-runs"
            "?check_name=Dashboard%20Checks",
        ],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    d = json.loads(out)
    runs = [r for r in d.get("check_runs") or [] if isinstance(r, dict)]
    if not runs:
      return []
    # Inspect the latest Dashboard Checks run and parse its markdown bullet list
    latest = max(runs, key=lambda r: int(r.get("id") or 0))
    text = str((latest.get("output") or {}).get("text") or "")
    return [m.strip() for m in RE_DASHBOARD_SUBJOB.findall(text) if m.strip()]
  except Exception:
    return []


def partition_github_failed_checks(
    pr_id: str, head_sha: str, failed_checks: list[str], failing: set[str]
) -> tuple[list[str], list[str]]:
  """Splits PR check failures into (own_failed_checks, tree_failed_checks)."""
  if not failed_checks:
    return [], []
  ok, tree_broken = fetch_flutter_tree_failures(failing)
  # Fall back to treating all failures as PR-owned when tree status cannot be queried
  if not ok:
    return list(failed_checks), []

  candidates: list[str] = []
  for c in failed_checks:
    # Expand the umbrella Dashboard Checks run into individual failing Luci job names
    if c == FLUTTER_DASHBOARD_CHECK:
      subjobs = fetch_dashboard_failed_subjobs(head_sha)
      if subjobs:
        for job in subjobs:
          if job not in candidates:
            candidates.append(job)
        continue
    if c not in candidates:
      candidates.append(c)

  # Check the PR's base commit when tip-of-tree master has already moved past a breakage
  if any(c not in tree_broken for c in candidates):
    tree_broken |= fetch_pr_base_failed_checks(pr_id, failing)

  own_failed = [c for c in candidates if c not in tree_broken]
  tree_failed = [c for c in candidates if c in tree_broken]
  return own_failed, tree_failed


def fetch_github_pr_status(
    pr_id: str, repo_slug: str = "flutter/flutter"
) -> dict[str, Any]:
  """Fetches GitHub PR status via `gh pr view` across Flutter and standalone repos."""
  eff_repo = str(repo_slug or "").strip() or "flutter/flutter"
  if eff_repo.startswith("github.com/"):
    eff_repo = eff_repo[len("github.com/") :]
  is_flutter_repo = eff_repo.lower() == "flutter/flutter"
  try:
    out = subprocess.check_output(
        [
            "gh",
            "pr",
            "view",
            str(pr_id),
            "--repo",
            eff_repo,
            "--json",
            # Include body so fix-for detection can parse PR bug/revert references;
            # omit closingIssuesReferences because gh 2.46 rejects that field on Linux
            "number,title,body,state,isDraft,author,mergedAt,headRefOid,headRefName,"
            "baseRefName,reviewDecision,latestReviews,reviews,comments,"
            "commits,statusCheckRollup,labels,url,mergeable,reviewRequests,"
            "createdAt,updatedAt,closedAt,files",
        ],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=12,
    )
    d = json.loads(out)
    state = str(d.get("state") or "OPEN")
    subject = str(d.get("title") or f"PR #{pr_id}")
    author_login = str((d.get("author") or {}).get("login") or "")
    is_self = author_login in SELF_GITHUB_LOGINS
    head_sha = str(d.get("headRefOid") or "")
    head_ref = str(d.get("headRefName") or "")
    base_ref = str(d.get("baseRefName") or "")
    commit_shas = [
        str(cm.get("oid") or "").strip()
        for cm in (d.get("commits") or [])
        if isinstance(cm, dict) and cm.get("oid")
    ]
    review_decision = str(d.get("reviewDecision") or "")
    is_wip = state == "OPEN" and (
        bool(d.get("isDraft")) or bool(RE_DRAFT_SUBJECT.match(subject))
    )
    # Distinguish native GitHub Draft PRs from [WIP] title tags
    wip_label = (
        "Draft"
        if (state == "OPEN" and bool(d.get("isDraft")))
        else ("WIP" if is_wip else "")
    )
    req_logins = [
        str(r.get("login") or r.get("name") or "")
        for r in d.get("reviewRequests") or []
        if r.get("login") or r.get("name")
    ]
    rev_logins = [
        str((r.get("author") or {}).get("login") or "")
        for r in d.get("reviews") or []
    ]
    cmt_logins = [
        str((c.get("author") or {}).get("login") or "")
        for c in d.get("comments") or []
    ]
    is_reviewer = not is_self and bool(
        set(req_logins + rev_logins + cmt_logins) & SELF_GITHUB_LOGINS
    )
    # Keep reviewer role independent of is_watching so the author column labels colleagues
    role = "author" if is_self else ("reviewer" if is_reviewer else "watching")
    # Automated bot reviews do not mean a PR has been sent to a human reviewer
    human_reviewers = [
        u
        for u in [*req_logins, *rev_logins, *cmt_logins]
        if u
        and u != author_login
        and not (is_self and u in SELF_GITHUB_LOGINS)
        and u not in GITHUB_STATUS_BOTS
        and u != "gemini-code-assist"
        and not u.endswith("[bot]")
        and not u.endswith("-bot")
    ]
    is_draft = is_self and is_wip
    in_development = (
        is_self and state == "OPEN" and not is_draft and not human_reviewers
    )

    checks = d.get("statusCheckRollup") or []
    # GitHub freezes mergeable=CONFLICTING on closed PRs; only open branches need rebase
    merge_conflict = state == "OPEN" and str(d.get("mergeable") or "") == "CONFLICTING"
    # Flutter's Dashboard Checks turn ACTION_REQUIRED when presubmit cannot start on
    # a conflicting branch; the Conflict status already reports that
    failing = {"FAILURE", "ERROR", "TIMED_OUT"}
    if not merge_conflict:
      failing.add("ACTION_REQUIRED")
    # Check runs carry name/conclusion, legacy status contexts carry context/state
    failed_all = [
        str(c.get("name") or c.get("context") or "")
        for c in checks
        if str(c.get("conclusion") or c.get("state") or "").upper() in failing
    ]
    # tree-status fails on every open PR while the flutter/flutter tree is red; it
    # says nothing about this PR, so it must not raise a CQ alert
    tree_red = is_flutter_repo and (FLUTTER_TREE_CHECK in failed_all)
    failed_checks = [
        c for c in failed_all if not (is_flutter_repo and c == FLUTTER_TREE_CHECK)
    ]
    # Subtract checks already failing on master or the PR's base commit on flutter/flutter
    if is_flutter_repo:
      own_failed_checks, tree_failed_checks = partition_github_failed_checks(
          str(pr_id), head_sha, failed_checks, failing
      )
    else:
      own_failed_checks, tree_failed_checks = list(failed_checks), []
    label_names = {
        str(lbl.get("name") or "")
        for lbl in (d.get("labels") or [])
        if isinstance(lbl, dict)
    }

    # Ignore dormant queued gate checks that stay queued until label, review, or merge queue
    def _is_active_pending_check(c: dict[str, Any]) -> bool:
      st = str(c.get("status") or "").upper()
      if st not in ("IN_PROGRESS", "QUEUED", "PENDING"):
        return False
      name = str(c.get("name") or "")
      if name == "Awaiting CICD label":
        return False
      if name == "Merge Queue Guard" and st == "QUEUED":
        return False
      if is_wip and name == "Dashboard Checks" and st == "QUEUED":
        return False
      return True

    pending_checks = [
        str(c.get("name") or "")
        for c in checks
        if _is_active_pending_check(c)
    ]
    # External Flutter PRs stay queued until a flutter-hackers member adds the CICD label
    awaiting_cicd_label = (
        state == "OPEN"
        and "CICD" not in label_names
        and any(
            str(c.get("name") or "") == "Awaiting CICD label"
            and str(c.get("status") or "").upper() in ("QUEUED", "IN_PROGRESS", "PENDING")
            for c in checks
        )
    )

    # Only PR-specific failures count as red CQ; tree-broken checks show a tree badge
    if own_failed_checks:
      cq_status = f"🔴 {len(own_failed_checks)} check(s) failed"
    elif tree_failed_checks:
      if len(tree_failed_checks) == 1:
        cq_status = f"🌲 Tree broken ({tree_failed_checks[0]})"
      else:
        cq_status = f"🌲 Tree broken ({len(tree_failed_checks)} checks)"
    elif tree_red:
      cq_status = "🌲 Tree is red"
    elif awaiting_cicd_label:
      cq_status = "🏷️ Needs CICD label"
    elif pending_checks:
      cq_status = f"🔄 {len(pending_checks)} running"
    else:
      cq_status = "✓ Checks green"

    reviews = d.get("reviews") or []
    # flutter/flutter requires 2 approvals on external PRs; reviewDecision reflects branch rules
    latest_reviews = [
        r for r in (d.get("latestReviews") or reviews) if isinstance(r, dict)
    ]
    approved_by: set[str] = set()
    changes_requested_by: set[str] = set()
    for r in latest_reviews:
      r_login = str((r.get("author") or {}).get("login") or "")
      r_state = str(r.get("state") or "").upper()
      r_assoc = str(r.get("authorAssociation") or "").upper()
      if not r_login or r_login == author_login:
        continue
      if r_state == "APPROVED":
        # Only repository members/owners satisfy branch protection and Cocoon approval rules
        if r_assoc in ("", "MEMBER", "OWNER"):
          approved_by.add(r_login)
        changes_requested_by.discard(r_login)
      elif r_state == "CHANGES_REQUESTED":
        changes_requested_by.add(r_login)
        approved_by.discard(r_login)
    approved_count = len(approved_by)
    active_review_candidates = (
        {u for u in req_logins if u and u != author_login}
        | approved_by
        | changes_requested_by
        | {
            u
            for u in rev_logins
            if u
            and u != author_login
            and u not in GITHUB_STATUS_BOTS
            and u != "gemini-code-assist"
            and not u.endswith("[bot]")
            and not u.endswith("-bot")
        }
    )
    author_assocs = {
        str(entry.get("authorAssociation") or "").upper()
        for entry in [
            *(d.get("reviews") or []),
            *(d.get("latestReviews") or []),
            *(d.get("comments") or []),
        ]
        if isinstance(entry, dict)
        and str((entry.get("author") or {}).get("login") or "") == author_login
        and entry.get("authorAssociation")
    }
    # Non-member authors on flutter/flutter need 2 member approvals under Cocoon auto-submit
    is_external_contributor = not is_self and (
        bool(author_assocs and not (author_assocs & {"MEMBER", "OWNER"}))
        or awaiting_cicd_label
        or any(str(c.get("name") or "") == "Awaiting CICD label" for c in checks)
    )
    assigned_reviewers_count = len(active_review_candidates)
    required_approvals = (
        2
        if (
            is_flutter_repo
            and (
                (review_decision == "REVIEW_REQUIRED" and approved_count >= 1)
                or is_external_contributor
            )
        )
        else 1
    )
    missing_reviewers_to_assign = (
        max(0, required_approvals - assigned_reviewers_count)
        if state == "OPEN"
        else 0
    )
    if state != "OPEN":
      missing_approvals = 0
    elif review_decision == "APPROVED" and required_approvals == 1:
      missing_approvals = 0
    elif review_decision == "REVIEW_REQUIRED":
      missing_approvals = max(1, required_approvals - approved_count)
    else:
      missing_approvals = max(0, required_approvals - approved_count)

    if state != "OPEN":
      reviewer_status = ""
    elif changes_requested_by or review_decision == "CHANGES_REQUESTED":
      reviewer_status = "Changes requested"
    elif assigned_reviewers_count == 0:
      reviewer_status = "No reviewers assigned"
    elif missing_reviewers_to_assign > 0:
      reviewer_status = (
          f"Needs +{missing_reviewers_to_assign} more reviewer"
          f"{'s' if missing_reviewers_to_assign > 1 else ''} "
          f"({assigned_reviewers_count}/{required_approvals} assigned)"
      )
    elif missing_approvals > 0:
      reviewer_status = (
          f"Needs +{missing_approvals} approval"
          f"{'s' if missing_approvals > 1 else ''} "
          f"({approved_count}/{required_approvals} approved)"
          if required_approvals > 1
          else "Awaiting approval"
      )
    else:
      reviewer_status = "Approved"

    comments = d.get("comments") or []
    last_actor = author_login
    last_snippet = ""
    last_date = ""
    events = []
    for r in reviews:
      if r.get("submittedAt"):
        events.append((
            str(r.get("submittedAt")),
            str((r.get("author") or {}).get("login") or ""),
            f"[{r.get('state')}] {str(r.get('body') or '')}".strip(),
        ))
    for c in comments:
      # Status bots repeat what the Conflict and Tree red statuses already show,
      # and their posts must not read as a reviewer waiting for my reply
      if str((c.get("author") or {}).get("login") or "") in GITHUB_STATUS_BOTS:
        continue
      if c.get("createdAt"):
        events.append((
            str(c.get("createdAt")),
            str((c.get("author") or {}).get("login") or ""),
            str(c.get("body") or "").strip(),
        ))
    # Subsequent commits address earlier review feedback on the PR timeline
    for cm in d.get("commits") or []:
      cm_date = str(cm.get("committedDate") or cm.get("authoredDate") or "")
      if not cm_date:
        continue
      cm_authors = cm.get("authors") or []
      cm_actor = author_login
      for ca in cm_authors:
        if not isinstance(ca, dict):
          continue
        ca_login = str(ca.get("login") or "")
        ca_email = str(ca.get("email") or "")
        if ca_login in SELF_GITHUB_LOGINS or ca_email in SELF_EMAILS:
          cm_actor = ca_login if ca_login in SELF_GITHUB_LOGINS else "Rusino"
          break
        if ca_login:
          cm_actor = ca_login
      headline = str(cm.get("messageHeadline") or cm.get("oid") or "").strip()
      events.append((cm_date, cm_actor, f"[COMMIT] {headline}".strip()))
    if events:
      events.sort(key=lambda x: x[0])
      last_date, last_actor, last_snippet = events[-1]
      last_snippet = last_snippet.replace("\n", " ")[:180]

    # Gather lifecycle and check run completion dates for status transition tracking
    merged_raw = str(d.get("mergedAt") or "")
    created_raw = str(d.get("createdAt") or "")
    updated_raw = str(d.get("updatedAt") or "")
    closed_raw = str(d.get("closedAt") or "")
    own_failed_set = set(own_failed_checks)
    failed_check_dates = [
        str(c.get("completedAt") or c.get("startedAt") or "")
        for c in checks
        if str(c.get("name") or c.get("context") or "") in own_failed_set
    ]
    pending_check_dates = [
        str(c.get("startedAt") or "")
        for c in checks
        if _is_active_pending_check(c)
    ]

    submitted_et, submitted_age = format_et_and_age(merged_raw)
    last_msg_et, last_msg_age = format_et_and_age(last_date)

    # Check whether I already approved this non-self PR without a fresh review request
    self_reviews = [
        r
        for r in reviews
        if str((r.get("author") or {}).get("login") or "") in SELF_GITHUB_LOGINS
        and r.get("state")
    ]
    self_approved = (
        not is_self
        and bool(self_reviews)
        and str(self_reviews[-1].get("state") or "").upper() == "APPROVED"
        and not (set(req_logins) & SELF_GITHUB_LOGINS)
    )
    # Treat a dismissed prior approval without a new review request as waiting on co-reviewer/author
    self_approval_dismissed = (
        not is_self
        and bool(self_reviews)
        and str(self_reviews[-1].get("state") or "").upper() == "DISMISSED"
        and not (set(req_logins) & SELF_GITHUB_LOGINS)
    )
    self_comments = [
        c
        for c in comments
        if str((c.get("author") or {}).get("login") or "") in SELF_GITHUB_LOGINS
    ]
    if self_approved:
      self_review_state = "approved"
    elif any(
        str(r.get("state") or "").upper() == "CHANGES_REQUESTED"
        for r in self_reviews
    ):
      self_review_state = "changes_requested"
    elif self_reviews or self_comments:
      self_review_state = "commented"
    else:
      self_review_state = ""
    self_comment_count = len(self_reviews) + len(self_comments)

    self_reviewed_waiting = (
        not is_self
        and bool(self_review_state)
        and not (set(req_logins) & SELF_GITHUB_LOGINS)
        and (
            self_approved
            or self_approval_dismissed
            or last_snippet.startswith("[COMMIT]")
        )
    )
    in_my_attention = (
        state == "OPEN"
        and not is_wip
        and not in_development
        and (is_self or is_reviewer)
        and not self_reviewed_waiting
        and bool(last_actor)
        and last_actor not in SELF_GITHUB_LOGINS
    )
    # Keep PRs we reviewed or approved in Waiting on others rather than passive Watching
    is_watching = (
        not is_self
        and not in_my_attention
        and not self_approved
        and self_review_state not in ("approved", "commented", "changes_requested")
    )

    # Map the status change date to the latest event driving the current PR phase
    if state == "MERGED":
      status_changed_raw = merged_raw or latest_timestamp(
          closed_raw, updated_raw, last_date
      )
    elif state == "CLOSED":
      status_changed_raw = latest_timestamp(
          closed_raw, last_date, updated_raw
      )
    # Only my own PRs enter the CQ failure alert phase
    elif own_failed_checks and is_self:
      status_changed_raw = latest_timestamp(
          *failed_check_dates, last_date, updated_raw
      )
    elif pending_checks:
      status_changed_raw = latest_timestamp(
          *pending_check_dates, last_date, updated_raw
      )
    else:
      status_changed_raw = latest_timestamp(
          last_date, updated_raw, created_raw
      )
    status_changed_et, status_changed_age = format_et_and_age(
        status_changed_raw
    )

    if state == "MERGED":
      phase = "READY_TO_CLOSE"
    elif state == "CLOSED":
      phase = "ABANDONED"
    # Raise a CQ alert only on my own PR when non-tree checks failed
    elif own_failed_checks and is_self:
      phase = "ALERT_CQ_FAILED"
    # Non-self PRs where I am not awaiting a reply stay out of NEEDS_YOUR_ACTION
    elif in_my_attention:
      phase = "NEEDS_YOUR_ACTION"
    else:
      phase = "WAITING_ON_OTHERS"

    # Compute reviewer ping candidates from PR reviews and commit timestamps
    reviewer_pings = compute_github_reviewer_pings(
        pr_id=str(pr_id),
        state=state,
        is_wip=is_wip,
        is_draft=is_draft,
        missing_approvals=missing_approvals,
        approved_count=approved_count,
        is_self=is_self,
        is_reviewer=is_reviewer,
        human_reviewers=human_reviewers,
        approved_by=approved_by,
        changes_requested_by=changes_requested_by,
        reviews=reviews,
        commits=d.get("commits") or [],
        req_logins=req_logins,
        repo_slug=eff_repo,
    )

    return {
        "ok": True,
        "host": f"github.com/{eff_repo}",
        "repo_slug": eff_repo,
        "url": str(
            d.get("url") or f"https://github.com/{eff_repo}/pull/{pr_id}"
        ),
        "status": state,
        "phase": phase,
        "subject": subject,
        "owner_name": author_login,
        "owner_email": author_login,
        "role": role,
        "is_self": is_self,
        "is_external": not is_self,
        "self_approved": self_approved,
        "self_review_state": self_review_state,
        "self_comment_count": self_comment_count,
        "is_draft": is_draft,
        "is_wip": is_wip,
        "wip_label": wip_label,
        "head_sha": head_sha,
        "head_ref": head_ref,
        "base_ref": base_ref,
        "commit_shas": commit_shas,
        "review_decision": review_decision,
        "required_approvals": required_approvals,
        "approved_count": approved_count,
        "assigned_reviewers_count": assigned_reviewers_count,
        "missing_reviewers_to_assign": missing_reviewers_to_assign,
        "missing_approvals": missing_approvals,
        "reviewer_status": reviewer_status,
        # Per-reviewer ping entries for stale reviews and missing 2nd approvals
        "reviewer_pings": reviewer_pings,
        "in_development": in_development,
        "is_watching": is_watching,
        "patchset": len(reviews) + 1,
        "revision": head_sha[:10],
        # Expose changed PR paths so triage_store detects lib/web_ui/lib/ corpus gates
        "files": sorted(
            str(f.get("path") or "").strip()
            for f in (d.get("files") or [])
            if isinstance(f, dict) and f.get("path")
        ),
        # Expose PR description text under commit_message for shared fix-for extractors
        "commit_message": str(d.get("body") or ""),
        # Combine optional closingIssuesReferences with free-text Bug:/Fixes references
        "linked_bug_keys": list(
            dict.fromkeys([
                *(
                    "gh_issue:"
                    f"{str((ci.get('repository') or {}).get('nameWithOwner') or eff_repo)}"
                    f"#{ci.get('number')}"
                    for ci in (d.get("closingIssuesReferences") or [])
                    if isinstance(ci, dict) and ci.get("number")
                ),
                *extract_bug_refs_from_text(
                    f"{subject}\n{str(d.get('body') or '')}",
                    default_tracker="github",
                    default_repo=eff_repo,
                ),
            ])
        ),
        "commit_url": (
            f"https://github.com/{eff_repo}/commit/{head_sha}" if head_sha else ""
        ),
        "in_my_attention": phase in ("NEEDS_YOUR_ACTION", "ALERT_CQ_FAILED"),
        "cr_summary": (
            f"✓ {approved_count}/{required_approvals}"
            if (approved_count > 0 and missing_approvals > 0)
            else (
                f"✓ {approved_count} app"
                if approved_count > 0
                else (
                    "✗ changes req"
                    if (changes_requested_by or review_decision == "CHANGES_REQUESTED")
                    else f"{len(reviews)} rev"
                )
            )
        ),
        "cq_status": cq_status,
        "awaiting_cicd_label": awaiting_cicd_label,
        # Expose both PR-owned and tree-broken check lists for UI and diagnostics
        "cq_failed": bool(own_failed_checks),
        "own_failed_checks": own_failed_checks,
        "tree_failed_checks": tree_failed_checks,
        "tree_red": tree_red,
        "merge_conflict": merge_conflict,
        # Only my own PRs list reviewers blocking submission
        "waiting_on": (
            [
                {"id": str(r.get("login") or r.get("name") or ""), "active": True}
                for r in d.get("reviewRequests") or []
                if r.get("login") or r.get("name")
            ]
            if is_self
            else []
        ),
        "last_msg_author": last_actor,
        "last_msg_date": last_date,
        "last_msg_et": last_msg_et,
        "last_msg_age": last_msg_age,
        "last_msg_snippet": last_snippet,
        # Creation timestamp bounds the window for correlating follow-up fix PRs
        "created": created_raw,
        "submitted": merged_raw,
        "submitted_et": submitted_et,
        "submitted_age": submitted_age,
        "status_changed_raw": status_changed_raw,
        "status_changed_et": status_changed_et,
        "status_changed_age": status_changed_age,
        "post_submit": None,
    }
  except Exception as e:
    return {"ok": False, "error": f"PR #{pr_id} error: {e}"}


def fetch_piper_cl_status(cl_id: str) -> dict[str, Any]:
  """Fetches Google3 Piper CL status via `g4 describe -s`."""
  try:
    out = subprocess.check_output(
        ["g4", "describe", "-s", str(cl_id)],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=10,
    )
    first_line = out.splitlines()[0] if out.splitlines() else ""
    status = (
        "SUBMITTED"
        if " *submitted* " in first_line or " submitted " in first_line
        else "PENDING"
    )
    owner = "unknown"
    m = re.match(r"^Change\s+\d+\s+by\s+([^\s@]+)", first_line)
    if m:
      owner = m.group(1)
    is_self = owner == "jlavrova"
    desc_lines = []
    for line in out.splitlines()[1:]:
      if line.startswith("Affected files"):
        break
      if line.strip():
        desc_lines.append(line.strip())
    subject = desc_lines[0] if desc_lines else f"cl/{cl_id}"
    # Parse R= tags to detect whether any non-owner reviewer is assigned in Critique
    reviewers = []
    for line in desc_lines:
      m_rev = re.match(r"^R=(.+)$", line)
      if m_rev:
        reviewers.extend(
            r.strip()
            for r in m_rev.group(1).split(",")
            if r.strip() and r.strip() != owner
        )
    is_reviewer = not is_self and "jlavrova" in reviewers
    # Non-self Piper CLs sit in Watching while preserving reviewer role metadata
    is_watching = not is_self
    is_open = status != "SUBMITTED"
    is_wip = is_open and bool(RE_DRAFT_SUBJECT.match(subject))
    wip_label = "WIP" if is_wip else ""
    is_draft = is_self and is_wip
    in_development = is_self and is_open and not is_draft and not reviewers
    assigned_reviewers_count = len(reviewers)
    missing_reviewers_to_assign = 1 if (is_open and not reviewers) else 0
    missing_approvals = 1 if is_open else 0
    reviewer_status = (
        "No reviewers assigned"
        if (is_open and not reviewers)
        else ("In review" if is_open else "")
    )
    phase = "READY_TO_CLOSE" if status == "SUBMITTED" else "WAITING_ON_OTHERS"
    # Extract timestamp from describe header to track submit or last edit time
    m_ts = re.search(
        r"\bon\s+(\d{4}/\d{2}/\d{2}\s+\d{2}:\d{2}:\d{2})\b", first_line
    )
    piper_ts = m_ts.group(1) if m_ts else ""
    piper_et, piper_age = format_et_and_age(piper_ts)
    return {
        "ok": True,
        "host": "critique.corp.google.com",
        "url": f"http://cl/{cl_id}",
        "status": "MERGED" if status == "SUBMITTED" else "NEW",
        "phase": phase,
        "subject": subject,
        "owner_name": owner,
        "owner_email": f"{owner}@google.com",
        # Keep reviewer role separate from is_watching for colleague label formatting
        "role": (
            "author"
            if is_self
            else ("reviewer (goog)" if is_reviewer else "watching")
        ),
        "is_self": is_self,
        "is_external": False,
        "is_draft": is_draft,
        "is_wip": is_wip,
        "wip_label": wip_label,
        "required_approvals": 1,
        "approved_count": 0,
        "assigned_reviewers_count": assigned_reviewers_count,
        "missing_reviewers_to_assign": missing_reviewers_to_assign,
        "missing_approvals": missing_approvals,
        "reviewer_status": reviewer_status,
        # Piper CLs do not use Skia 2-reviewer Gerrit attention sets
        "reviewer_pings": [],
        "in_development": in_development,
        "is_watching": is_watching,
        "patchset": 1,
        "revision": cl_id,
        # Expose Piper description and timestamp for fix-for correlation
        "commit_message": "\n".join(desc_lines),
        "created": piper_ts,
        "in_my_attention": False,
        "cr_summary": "—",
        "cq_status": status,
        "cq_failed": False,
        "last_msg_author": owner,
        "last_msg_date": piper_ts,
        "last_msg_et": piper_et,
        "last_msg_age": piper_age,
        "last_msg_snippet": subject,
        "submitted": piper_ts if status == "SUBMITTED" else "",
        "submitted_et": piper_et if status == "SUBMITTED" else "",
        "submitted_age": piper_age if status == "SUBMITTED" else "",
        "status_changed_raw": piper_ts,
        "status_changed_et": piper_et,
        "status_changed_age": piper_age,
        "post_submit": None,
    }
  except Exception as e:
    return {"ok": False, "error": f"cl/{cl_id} error: {e}"}


def send_desktop_alert(title: str, message: str, url: str = "") -> None:
  if not os.path.isfile(NOTIFY_SELF_BIN):
    return
  cmd = [
      NOTIFY_SELF_BIN,
      f"--summary={title}",
      f"--message={message}",
      "--severity=red",
  ]
  if url:
    cmd.append(f"--uri={url}")
  try:
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
  except Exception:
    pass


def wake_conversation(conv_id: str, message: str) -> bool:
  """Sends a wakeup message to a Jetski conversation via LS RPC with agentapi fallback"""
  if not conv_id or not message:
    return False
  try:
    resp = cl.ls_rpc(
        "SendUserCascadeMessage",
        {"cascadeId": conv_id, "items": [{"text": message}], "blocking": False},
    )
    if isinstance(resp, dict):
      return True
  except Exception:
    pass
  env = dict(os.environ)
  env.pop("ANTIGRAVITY_SOURCE_METADATA", None)
  try:
    res = subprocess.run(
        ["agentapi", "send-message", conv_id, message],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return res.returncode == 0
  except Exception:
    return False


def finalize_conversation(conv_id: str) -> bool:
  """Finalizes a conversation chain via chat_lifecycle.py."""
  if not conv_id:
    return False
  try:
    res = subprocess.run(
        [
            "python3",
            os.path.join(HOOKS_DIR, "chat_lifecycle.py"),
            "finalize",
            conv_id,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    return res.returncode == 0
  except Exception:
    return False


def is_autoroll_change(cl_info: dict[str, Any]) -> bool:
  """Returns whether a CL/PR is an automated roll handled by Skia Gardener."""
  # Manual rolls authored by the user still belong on the personal Flight Deck
  if cl_info.get("is_self"):
    return False
  owner_name = str(cl_info.get("owner_name") or "").lower()
  owner_email = str(cl_info.get("owner_email") or "").lower()
  # Match roller bot logins and Gerrit IAM service accounts
  if (
      "autoroll" in owner_name
      or "autoroll" in owner_email
      or owner_email.endswith(".gserviceaccount.com")
  ):
    return True
  subject = str(cl_info.get("subject") or "").strip()
  return bool(RE_AUTOROLL_SUBJECT.match(subject))


def should_finalize_removed_reviewer_chat(
    chat_item: dict[str, Any], cl_info: dict[str, Any]
) -> bool:
  """Returns whether a 1:1 CL chat should be finalized after reviewer self-removal."""
  if (
      not chat_item.get("conversation_id")
      or not cl_info.get("reviewer_removed")
      or not chat_item.get("single_target", True)
  ):
    return False
  removed_dt = parse_gerrit_or_iso_time(str(cl_info.get("removed_at") or ""))
  chat_dt = parse_gerrit_or_iso_time(
      str(chat_item.get("chat_last_modified") or "")
  )
  # Keep a newly opened chat alive when the user starts it after removing themselves
  if removed_dt and chat_dt and chat_dt > removed_dt:
    return False
  return True


def update_chat_roll_badge(conv_id: str, current_title: str, badge: str) -> None:
  """Updates the trailing roll badge on a conversation title without waking the LLM."""
  base = cl.clean_base_title(current_title)
  base_no_badge = RE_ROLL_BADGE.sub("", base).strip()
  new_topic = f"{base_no_badge} {badge}".strip() if badge else base_no_badge
  # "[HH:MM] ⦿ " takes 10 characters and titles must stay under 50, so the topic is
  # cut to make room for the badge rather than dropping it
  if len(new_topic) > TITLE_TOPIC_MAX and badge:
    avail = max(12, TITLE_TOPIC_MAX - 1 - len(badge))
    # Discovery reads CL, PR, or bug tokens from the title, so a cut must never split them
    raw_tokens = [
        m.group(0)
        for r in (RE_CL_ID_GROUP, RE_PR_ID_GROUP, RE_BUG_TITLE_TOKEN)
        for m in r.finditer(base_no_badge)
    ]
    rest = base_no_badge
    tokens: list[str] = []
    for tok in raw_tokens:
      rest = rest.replace(tok, " ")
      # Shorten crrev.com/c/ or skrev.com/ when a long alert badge leaves <19 chars
      if len(tok) > avail:
        tok = re.sub(r"\.com/", "/", tok, flags=re.IGNORECASE)
      tokens.append(tok)
    kept = " ".join(tokens + rest.split())
    new_topic = f"{kept[:avail].rstrip()} {badge}"
  if new_topic == base:
    return
  try:
    cl.set_conversation_topic(conv_id, new_topic)
  except Exception:
    pass


def _build_start_chat_topic(rec: dict[str, Any]) -> str:
  """Builds a validated <38-char chat topic naming the shortlink and change subject."""
  # Accept both CL/PR records (kind/target_id/subject) and bug records (source/id/title)
  kind = str(rec.get("kind") or rec.get("source") or "gerrit")
  tid = str(rec.get("target_id") or rec.get("id") or "")
  host = str(rec.get("host") or rec.get("preferred_host") or "")
  fallback_noun = "patchset"
  if kind in ("buganizer", "gh_issue", "github_issue"):
    id_tok = str(rec.get("short_id") or (f"flutter#{tid}" if "gh" in kind else f"b/{tid}"))
    default_sub = "Flutter" if "gh" in kind else "Skia"
    fallback_noun = "issue"
  elif kind == "github":
    id_tok = f"PR #{tid}"
    default_sub = "Flutter"
  elif kind == "gerrit" and host == GERRIT_CHROMIUM_HOST:
    id_tok = f"crrev/c/{tid}"
    default_sub = "Chromium"
  elif kind == "gerrit":
    # Use "Gerrit <id>" so titles never resolve to Google3 Piper via cl/<id>
    id_tok = f"Gerrit {tid}"
    default_sub = "Skia"
  else:
    id_tok = f"cl/{tid}"
    default_sub = "Skia"

  def _fit_words(text: str) -> str:
    words: list[str] = []
    for w_tok in text.split():
      cand = f"{id_tok} {' '.join([*words, w_tok])}"
      if len(cand) > TITLE_TOPIC_MAX:
        break
      words.append(w_tok)
    if not words and text:
      rem = max(4, TITLE_TOPIC_MAX - 1 - len(id_tok))
      words = [text[:rem].rstrip()]
    return f"{id_tok} {' '.join(words)}"[:TITLE_TOPIC_MAX].rstrip() if words else ""

  raw_subj = str(rec.get("subject") or rec.get("title") or "")
  if _is_placeholder_bug_title(raw_subj):
    raw_subj = ""
  clean_subj = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", raw_subj).strip()
  unbracketed = re.sub(r"[\[\]]+", " ", raw_subj).strip()
  proj_name = str(rec.get("project_name") or "").strip()
  # Fall back to bracketed tags, domain project name, or subsystem so GENERIC_TOPIC_WORDS
  # subjects like "[canvas] Fix bug" still pass validate_chat_topic
  sources = [clean_subj, unbracketed]
  if proj_name and proj_name not in META_PROJECT_NAMES:
    sources.append(f"{proj_name} {fallback_noun}")
  sources.append(f"{default_sub} {fallback_noun}")

  for src in sources:
    cand = _fit_words(src)
    if not cand:
      continue
    try:
      return cl.validate_chat_topic(cand)
    except cl.InvalidTopicError:
      continue
  return cl.validate_chat_topic(
      f"{id_tok} {default_sub} {fallback_noun}"[:TITLE_TOPIC_MAX].rstrip()
  )


def _lookup_state_item(
    items: dict[str, Any], ikey: str
) -> tuple[str, dict[str, Any] | None]:
  """Finds a tracked state item by `item_key`, returning `(dict_key, record)`."""
  unchatted_key = f"{ikey}@unchatted"
  if isinstance(items.get(unchatted_key), dict):
    return unchatted_key, items[unchatted_key]
  for k, v in items.items():
    if isinstance(v, dict) and v.get("item_key") == ikey:
      return k, v
  return "", None


def _discover_existing_chat_for_item(
    ikey: str, rec: dict[str, Any] | None
) -> tuple[str, bool, str]:
  """Searches active, companion-parent, and archived chats for `ikey`."""
  by_key = {
      c["item_key"]: c
      for c in discover_active_cl_chats(include_archived=True)
      if c.get("item_key") and c.get("conversation_id")
  }
  cand = by_key.get(ikey)
  if cand and not cand.get("chat_archived"):
    return str(cand["conversation_id"]), False, ""

  # Reuse parent chat for companion Gerrit CLs or GitHub PRs when candidates exist
  parent_keys: list[str] = []
  rec_is_self = (
      True if not rec or "is_self" not in rec else bool(rec.get("is_self"))
  )
  is_companion_ref = False
  known_comp_to = str((rec or {}).get("companion_to") or "").strip()
  if by_key and known_comp_to and rec_is_self:
    parent_keys = [known_comp_to]
    is_companion_ref = True
  elif by_key and ikey.startswith("gerrit:"):
    tid = ikey.split(":", 1)[1]
    host = str(
        (rec or {}).get("host")
        or (rec or {}).get("preferred_host")
        or GERRIT_SKIA_HOST
    )
    subj = str((rec or {}).get("subject") or (rec or {}).get("title") or "")
    cmsg = str((rec or {}).get("commit_message") or "")
    m_cl = RE_COMPANION_CL_REF.search(subj) or RE_COMPANION_CL_REF.search(cmsg)
    if m_cl and rec_is_self:
      parent_keys = [f"gerrit:{m_cl.group(1)}"]
      is_companion_ref = True
    else:
      parent_keys = [
          f"gerrit:{rel_id}" for rel_id in fetch_gerrit_related_cl_ids(host, tid)
      ]
      is_companion_ref = rec_is_self and ("companion" in subj.lower())
  elif by_key and ikey.startswith("github:"):
    subj = str((rec or {}).get("subject") or (rec or {}).get("title") or "")
    cmsg = str((rec or {}).get("commit_message") or "")
    head_ref = str(
        (rec or {}).get("head_ref") or (rec or {}).get("head_ref_name") or ""
    )
    m = RE_COMPANION_PR_REF.search(subj) or RE_COMPANION_PR_REF.search(cmsg)
    branch_m = RE_COMPANION_BRANCH.match(head_ref)
    ref_id = m.group(1) if m else (branch_m.group(1) if branch_m else "")
    if ref_id and rec_is_self:
      parent_keys = [f"github:{ref_id}"]
      is_companion_ref = True
  for parent_key in parent_keys:
    parent = by_key.get(parent_key)
    if parent:
      parent_is_self = bool(parent.get("is_self", False))
      is_cand_comp = rec_is_self and (not parent_is_self or is_companion_ref)
      can_inherit_stack_chat = (
          is_cand_comp
          or (rec_is_self and parent_is_self)
      )
      if not can_inherit_stack_chat:
        continue
      comp_key = parent_key if is_cand_comp else ""
      return (
          str(parent["conversation_id"]),
          bool(parent.get("chat_archived")),
          comp_key,
      )

  if cand:
    return str(cand["conversation_id"]), True, ""
  return "", False, ""


def _rekey_state_item_chat(
    items: dict[str, Any],
    ikey: str,
    matched_key: str,
    rec: dict[str, Any],
    cid: str,
    *,
    chat_archived: bool | None = None,
    companion_to: str = "",
) -> None:
  """Binds `rec` to `cid` and moves its entry under `ikey@cid`."""
  if matched_key:
    items.pop(matched_key, None)
  items.pop(f"{ikey}@unchatted", None)
  rec["conversation_id"] = cid
  rec["composite_key"] = f"{ikey}@{cid}"
  if chat_archived is not None:
    rec["chat_archived"] = chat_archived
  if companion_to:
    rec["companion_to"] = companion_to
  items[f"{ikey}@{cid}"] = rec


def _is_assigned_reviewer_record(rec: dict[str, Any]) -> bool:
  """Returns True if the user is an assigned reviewer on an open external CL/PR"""
  if not isinstance(rec, dict):
    return False
  if rec.get("is_self") or rec.get("companion_to") or rec.get("is_companion"):
    return False
  if rec.get("reviewer_removed"):
    return False
  role = str(rec.get("role") or "").strip()
  if role:
    return role.startswith("reviewer")
  return not bool(rec.get("is_watching"))


def _start_pinned_chat_for_record(
    items: dict[str, Any],
    ikey: str,
    matched_key: str,
    rec: dict[str, Any],
    prompt: str,
    model_tier: str = "pro",
) -> dict[str, Any]:
  """Creates a project-pinned visible Jetski chat for `rec` with `prompt`."""
  proj_ref = str(rec.get("project_name") or rec.get("project_id") or "").strip()
  try:
    project_id = (
        cl.resolve_project_id(proj_ref) if proj_ref else "outside-of-project"
    )
  except ValueError:
    project_id = str(rec.get("project_id") or "").strip() or "outside-of-project"

  topic = _build_start_chat_topic(rec)
  full_title = f"[{cl.get_current_time_str()}] ⦿ {topic}"
  try:
    model_info = cl.resolve_handoff_model(
        "", tier=model_tier or "pro", project_id=project_id
    )
    new_cid = cl.start_conversation_exact(
        model_enum=model_info["enum"],
        title=full_title,
        prompt=prompt,
        project_id=project_id,
    )
  except (OSError, RuntimeError, ValueError, KeyError) as exc:
    return {"ok": False, "error": str(exc)}

  _rekey_state_item_chat(
      items, ikey, matched_key, rec, new_cid, chat_archived=False
  )
  rec["title"] = full_title
  rec["project_id"] = project_id
  rec["chat_busy"] = True
  rec["chat_woken_ts"] = time.time()
  rec["chat_last_modified"] = datetime.now(timezone.utc).isoformat()
  if _is_assigned_reviewer_record(rec):
    rec["review_target_ps"] = int(rec.get("patchset") or 1)
  return {
      "ok": True,
      "started": True,
      "conversation_id": new_cid,
      "title": full_title,
      "project_id": project_id,
      "chat_url": f"{jetski_box_url()}/c/{new_cid}",
  }


def _start_chat_for_bug(
    state: dict[str, Any], bug_key: str, *, save: bool = True
) -> dict[str, Any]:
  """Reuses an existing bug or 1:1 self-CL chat, or spawns a pinned bug chat."""
  bugs: dict[str, Any] = state.setdefault("bugs", {})
  bug = bugs.get(bug_key)
  if not isinstance(bug, dict):
    return {"ok": False, "error": f"Unknown bug {bug_key}"}

  bug_chats = discover_bug_chats(include_archived=True)
  cand = bug_chats.get(bug_key)
  active_cid = str(
      (cand or {}).get("conversation_id") or bug.get("conversation_id") or ""
  ).strip()
  if not active_cid:
    # Reuse a linked 1:1 self-authored CL chat, ignoring multi-bug umbrella CLs
    self_cl_cids = [
        str(sub.get("conversation_id") or "").strip()
        for sub in (bug.get("linked_cls") or [])
        if isinstance(sub, dict)
        and sub.get("is_self")
        and sub.get("shared_1_to_1", True)
        and sub.get("conversation_id")
    ]
    if len(set(self_cl_cids)) == 1:
      active_cid = self_cl_cids[0]

  if active_cid:
    bug["conversation_id"] = active_cid
    if cand:
      bug["chat_archived"] = bool(cand.get("chat_archived"))
    if save:
      save_watcher_state(state)
    return {
        "ok": True,
        "already_existed": True,
        "conversation_id": active_cid,
        "chat_url": f"{jetski_box_url()}/c/{active_cid}",
    }

  proj_ref = str(bug.get("project_name") or bug.get("project_id") or "").strip()
  try:
    # Keep internal Jetski/OneQuota bugs pinned to Improving Experience while excluding meta chats
    project_id = (
        cl.resolve_project_id(proj_ref)
        if (
            proj_ref
            and (proj_ref == "Improving Experience" or proj_ref not in META_PROJECT_NAMES)
        )
        else "outside-of-project"
    )
  except ValueError:
    project_id = str(bug.get("project_id") or "").strip() or "outside-of-project"

  rec_for_topic = {
      **bug,
      "kind": "gh_issue" if bug_key.startswith("gh_issue:") else "buganizer",
  }
  topic = _build_start_chat_topic(rec_for_topic)
  full_title = f"[{cl.get_current_time_str()}] ⦿ {topic}"
  url = str(bug.get("url") or bug.get("short_id") or bug_key)
  btitle = str(bug.get("title") or bug_key)
  # Collaborator and mentioned bugs ask for domain review and a draft comment rather than ownership
  if bug.get("is_collaborator") or bug.get("is_mentioned"):
    owner_lbl = str(bug.get("assignee") or "коллеги")
    role_note = (
        f"Тебя упомянули в баге {url} ('{btitle}')."
        if bug.get("is_mentioned") and not bug.get("is_collaborator")
        else f"Ты указана как Collaborator в баге {url} ('{btitle}', владелец: {owner_lbl})."
    )
    prompt = (
        f"{role_note} Проверь описание, комментарии и код, следуй навыку bug-triage "
        "(~/.gemini/config/skills/bug-triage/SKILL.md), и подготовь техническое ревью, "
        "решение/PoC (WIP CL, если уместно) и черновик комментария для бага "
        "(не публикуй комментарий без подтверждения)."
    )
  else:
    prompt = (
        f"Исследуй баг {url} ('{btitle}'). Следуй навыку bug-triage "
        "(~/.gemini/config/skills/bug-triage/SKILL.md): проанализируй причину, "
        "предложи решение, при необходимости создай silent WIP CL (PS1 = failing test, "
        "PS2 = fix) и подготовь черновик ответа для трекера (не публикуй ответ без подтверждения)."
    )
  try:
    model_info = cl.resolve_handoff_model("", tier="pro", project_id=project_id)
    new_cid = cl.start_conversation_exact(
        model_enum=model_info["enum"],
        title=full_title,
        prompt=prompt,
        project_id=project_id,
    )
  except (OSError, RuntimeError, ValueError, KeyError) as exc:
    return {"ok": False, "error": str(exc)}

  bug["conversation_id"] = new_cid
  bug["chat_title"] = full_title
  bug["chat_archived"] = False
  bug["project_id"] = project_id
  bug["chat_busy"] = True
  bug["chat_woken_ts"] = time.time()
  bug["chat_last_modified"] = datetime.now(timezone.utc).isoformat()
  # Propagate newly spawned bug chat to any unchatted self CL linked to this bug
  link_bugs_and_cls(bugs, state.setdefault("items", {}))
  if save:
    save_watcher_state(state)
  return {
      "ok": True,
      "started": True,
      "conversation_id": new_cid,
      "title": full_title,
      "project_id": project_id,
      "chat_url": f"{jetski_box_url()}/c/{new_cid}",
  }


# Canonical CLI clause so Gerrit reviewer wakeups save unpublished drafts instead of chat-only text
GERRIT_SAVE_DRAFT_CMD = "python3 ~/.gemini/config/scripts/gerrit_draft.py save"


def _build_review_triage_prompt(ikey: str, url: str, subj: str, ps: int) -> str:
  draft_clause = (
      f"сохрани неопубликованный черновик ответа в Gerrit через {GERRIT_SAVE_DRAFT_CMD}, "
      if ikey.startswith("gerrit:")
      else ""
  )
  return (
      f"Проведи review-triage для {url} ('{subj}', PS{ps}), подготовь разбор ревью, "
      f"{draft_clause}(никогда не публикуй комментарии ревью без моего подтверждения) "
      "и запиши вердикт через triage_store.py save-review."
  )


def start_chat_for_item(item_key: str) -> dict[str, Any]:
  """Starts a project-pinned visible Jetski chat for an unchatted CL/PR or bug item."""
  ikey = str(item_key or "").strip()
  if not ikey:
    return {"ok": False, "error": "Missing item_key"}

  state = load_watcher_state()
  # Dispatch bug keys to the bug-specific chat launcher and state linker
  if ikey.startswith(("buganizer:", "gh_issue:")):
    return _start_chat_for_bug(state, ikey)

  items: dict[str, Any] = state.setdefault("items", {})
  matched_key, rec = _lookup_state_item(items, ikey)
  triage_dir = _triage_results_dir()

  # Reuse an existing active, companion-parent, or archived chat before spawning a new one
  existing_cid, is_arch, comp_to = _discover_existing_chat_for_item(ikey, rec)
  active_cid = existing_cid or (
      str(rec.get("conversation_id") or "").strip() if isinstance(rec, dict) else ""
  )
  if active_cid:
    if isinstance(rec, dict):
      if not str(rec.get("conversation_id") or "").strip():
        _rekey_state_item_chat(
            items,
            ikey,
            matched_key,
            rec,
            active_cid,
            chat_archived=True if is_arch else None,
            companion_to=comp_to,
        )
      # Trigger incremental deep audit if a new patchset was uploaded after previous review
      if _is_assigned_reviewer_record(rec) and not rec.get("chat_busy"):
        cur_ps = int(rec.get("patchset") or 1)
        rev_ps = int(rec.get("reviewed_ps") or 0)
        tgt_ps = int(rec.get("review_target_ps") or 0)
        if rev_ps > 0 and cur_ps > rev_ps and cur_ps > tgt_ps:
          r_kind = str(rec.get("kind") or "gerrit")
          r_tid = str(rec.get("target_id") or ikey.split(":", 1)[-1])
          prev_tr = triage_store.load_triage_result(
              r_kind, r_tid, results_dir=triage_dir
          )
          wake_msg = audit_gate.build_audit_prompt(
              tier=2,
              item=rec,
              prev_triage=prev_tr,
              delta_info={"delta_kind": "code_change"},
          )
          woken = wake_conversation(active_cid, wake_msg)
          if woken:
            rec["review_target_ps"] = cur_ps
            rec["chat_busy"] = True
            rec["chat_woken_ts"] = time.time()
            # Clear deferred wakeup watermarks and record claim once manually woken
            rec.pop("pending_wake_from_ps", None)
            rec.pop("pending_wake_from_msg_date", None)
            rec["audited_msg_date"] = str(rec.get("last_msg_date") or "")
            state.setdefault("review_claims", {})[f"{ikey}:{cur_ps}"] = {
                "cid": active_cid,
                "ts": time.time(),
            }
            triage_store.record_audit_status(
                r_tid,
                kind=r_kind,
                patchset=cur_ps,
                revision=str(rec.get("head_sha") or rec.get("revision") or ""),
                audit_state="running",
                results_dir=triage_dir,
            )
            save_watcher_state(state)
            return {
                "ok": True,
                "already_existed": True,
                "woken": True,
                "conversation_id": active_cid,
                "chat_url": f"{jetski_box_url()}/c/{active_cid}",
            }
      save_watcher_state(state)
    return {
        "ok": True,
        "already_existed": True,
        "conversation_id": active_cid,
        "chat_url": f"{jetski_box_url()}/c/{active_cid}",
    }

  if not isinstance(rec, dict):
    return {"ok": False, "error": f"Unknown item {ikey}"}

  existing_cid = str(rec.get("conversation_id") or "").strip()
  if existing_cid:
    return {
        "ok": True,
        "already_existed": True,
        "conversation_id": existing_cid,
        "chat_url": f"{jetski_box_url()}/c/{existing_cid}",
    }

  url = str(rec.get("url") or ikey)
  subj = str(rec.get("subject") or ikey)
  ps = int(rec.get("patchset") or 1)
  r_kind = str(rec.get("kind") or "gerrit")
  r_tid = str(rec.get("target_id") or ikey.split(":", 1)[-1])
  if rec.get("is_self"):
    prompt = (
        f"Отслеживай и помогай с моим изменением {url} ('{subj}', PS{ps}). "
        "Проверь текущий статус, комментарии ревью и проверки CI, и кратко напиши, "
        "что требует моего внимания."
    )
  elif _is_assigned_reviewer_record(rec):
    prev_tr = triage_store.load_triage_result(
        r_kind, r_tid, results_dir=triage_dir
    )
    prompt = audit_gate.build_audit_prompt(
        tier=2,
        item=rec,
        prev_triage=prev_tr,
        delta_info={"delta_kind": "initial"},
    )
  else:
    # Build Gerrit/GitHub initial review prompt with canonical unpublished Gerrit draft save
    prompt = _build_review_triage_prompt(ikey, url, subj, ps)

  res = _start_pinned_chat_for_record(items, ikey, matched_key, rec, prompt)
  if res.get("ok"):
    if _is_assigned_reviewer_record(rec):
      cur_rev = str(rec.get("head_sha") or rec.get("revision") or "")
      claim_key = (
          f"{ikey}:{ps}:{cur_rev[:10]}" if (r_kind == "github" and cur_rev) else f"{ikey}:{ps}"
      )
      state.setdefault("review_claims", {})[claim_key] = {
          "cid": str(res.get("conversation_id") or ""),
          "ts": time.time(),
      }
      triage_store.record_audit_status(
          r_tid,
          kind=r_kind,
          patchset=ps,
          revision=cur_rev,
          audit_state="running",
          results_dir=triage_dir,
      )
    save_watcher_state(state)
  return res


def save_gerrit_draft_for_item(
    item_key: str, draft_text: str = ""
) -> dict[str, Any]:
  """Saves or updates a /PATCHSET_LEVEL Gerrit draft in-place on explicit user request."""
  ikey = str(item_key or "").strip()
  if not ikey:
    return {"ok": False, "error": "Missing item_key"}
  state = load_watcher_state()
  items: dict[str, Any] = state.get("items") or {}
  _, rec = _lookup_state_item(items, ikey)
  rec_dict = rec if isinstance(rec, dict) else {}
  kind = str(
      rec_dict.get("kind") or ("github" if ikey.startswith("github:") else "gerrit")
  )
  tid = str(rec_dict.get("target_id") or ikey.split(":", 1)[-1]).strip()
  # Guard endpoint to numeric Gerrit change IDs since GitHub lacks a draft comment REST equivalent
  if kind != "gerrit" or not tid.isdigit():
    return {"ok": False, "error": "Save to Gerrit Draft only supports Gerrit CLs"}

  tr = triage_store.load_triage_result(
      "gerrit", tid, results_dir=_triage_results_dir()
  )
  msg = (draft_text or str((tr or {}).get("draft") or "")).strip()
  if not msg:
    return {"ok": False, "error": "No review draft text available"}

  host = str(
      rec_dict.get("host") or rec_dict.get("preferred_host") or GERRIT_SKIA_HOST
  ).strip()
  eff_ps = int(
      rec_dict.get("patchset") or (tr or {}).get("triaged_patchset") or 1
  )
  tok = canary_advisor._gerrit_luci_token()
  if not tok:
    return {
        "ok": False,
        "error": "Failed to obtain LUCI OAuth token (run luci-auth login)",
    }

  base_url = f"https://{host}/a/changes/{tid}/revisions/{eff_ps}/drafts"
  headers = {
      "Authorization": f"Bearer {tok}",
      "User-Agent": "FlightDeck-Watcher",
  }
  # Probe existing /PATCHSET_LEVEL drafts first to update in-place rather than stacking duplicates
  existing_id = ""
  try:
    get_req = urllib.request.Request(base_url, headers=headers, method="GET")
    with urllib.request.urlopen(get_req, timeout=12) as resp:
      raw = resp.read().decode("utf-8", errors="replace")
      if raw.startswith(")]}'"):
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw[4:]
      existing_map = json.loads(raw or "{}")
      ps_drafts = (
          existing_map.get("/PATCHSET_LEVEL")
          if isinstance(existing_map, dict)
          else None
      )
      if isinstance(ps_drafts, list):
        for d_obj in ps_drafts:
          if isinstance(d_obj, dict) and d_obj.get("id"):
            existing_id = str(d_obj["id"])
            break
  except (OSError, ValueError, http.client.HTTPException):
    existing_id = ""

  # Target existing draft ID when present so Gerrit overwrites the single patchset-level draft
  put_url = (
      f"{base_url}/{urllib.parse.quote(existing_id, safe='')}"
      if existing_id
      else base_url
  )
  payload = {"path": "/PATCHSET_LEVEL", "message": msg, "unresolved": True}
  try:
    put_req = urllib.request.Request(
        put_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json; charset=utf-8",
        },
        method="PUT",
    )
    with urllib.request.urlopen(put_req, timeout=15) as resp:
      raw_put = resp.read().decode("utf-8", errors="replace")
      if raw_put.startswith(")]}'"):
        raw_put = raw_put.split("\n", 1)[1] if "\n" in raw_put else raw_put[4:]
      saved_obj = json.loads(raw_put or "{}")
  except (OSError, ValueError, http.client.HTTPException) as exc:
    return {"ok": False, "error": f"Gerrit draft PUT failed: {exc}"}

  saved_id = (
      str(saved_obj.get("id") or existing_id)
      if isinstance(saved_obj, dict)
      else existing_id
  )
  return {
      "ok": True,
      "item_key": f"gerrit:{tid}",
      "patchset": eff_ps,
      "draft_id": saved_id,
      "updated_existing": bool(existing_id),
      "cl_url": f"https://{host}/c/{tid}/{eff_ps}",
  }


def request_open_in_existing_tab(
    conv_id: str, timeout_sec: float = 1.8
) -> bool:
  """Asks an open Jetski Web tab to navigate in-place to `conv_id` and waits for ack."""
  cid = str(conv_id or "").strip()
  if not cid:
    return False
  ann_file = os.path.join(ANNOTATIONS_DIR, f"{cid}.pbtxt")
  before_mtime = 0.0
  before_txt = ""
  if os.path.isfile(ann_file):
    try:
      before_mtime = os.path.getmtime(ann_file)
      with open(ann_file, "r", encoding="utf-8", errors="ignore") as f:
        before_txt = f.read()
    except OSError:
      pass
  req_ts_ms = int(time.time() * 1000)
  req_id = f"req-{req_ts_ms}-{cid[:8]}"
  payload = {
      "requestId": req_id,
      "cascadeId": cid,
      "requestedAt": req_ts_ms,
  }
  try:
    os.makedirs(os.path.dirname(OPEN_CHAT_REQ_FILE), exist_ok=True)
    tmp_path = f"{OPEN_CHAT_REQ_FILE}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, OPEN_CHAT_REQ_FILE)
  except OSError:
    return False

  if timeout_sec <= 0:
    return False
  deadline = time.time() + timeout_sec
  while time.time() < deadline:
    time.sleep(0.05)
    if os.path.isfile(ann_file):
      try:
        cur_mtime = os.path.getmtime(ann_file)
        with open(ann_file, "r", encoding="utf-8", errors="ignore") as f:
          cur_txt = f.read()
        if cur_mtime > before_mtime or cur_txt != before_txt:
          return True
      except OSError:
        pass
  return False


def open_chat_for_id(
    conv_id: str, timeout_sec: float = 1.8
) -> dict[str, Any]:
  """Handles `/api/open_chat` by routing in-place when an open Jetski tab acknowledges."""
  cid = str(conv_id or "").strip()
  if not cid:
    return {"ok": False, "error": "Missing conversation_id"}
  reused = request_open_in_existing_tab(cid, timeout_sec=timeout_sec)
  return {
      "ok": True,
      "conversation_id": cid,
      "chat_url": f"{jetski_box_url()}/c/{cid}",
      "reused_tab": reused,
  }


def _build_rebase_prompt(rec: dict[str, Any]) -> str:
  """Builds the conflict-resolution prompt sent to the CL/PR's owning agent chat."""
  url = str(rec.get("url") or rec.get("item_key") or "")
  subj = str(rec.get("subject") or url)
  ps = int(rec.get("patchset") or 1)
  if rec.get("kind") == "github":
    return (
        f"[FLIGHT-DECK REBASE] В {url} ('{subj}') возник merge conflict с upstream "
        "main/master. Сделай rebase ветки этого PR на свежий upstream main в локальном "
        "worktree (создай изолированный worktree, если основной checkout грязный или занят "
        "лизом), разреши все merge conflicts с сохранением доменных инвариантов и поведения, "
        "прогони релевантные unit-тесты (плюс run_webparagraph_corpus.py, если затронут "
        "продакшн-код в lib/web_ui/lib/) и проверки amnesia-precommit, после чего запушь "
        "обновлённую ветку в PR. Не мержи PR и не включай auto-merge."
    )
  return (
      f"[FLIGHT-DECK REBASE] В {url} ('{subj}', PS{ps}) возник merge conflict с "
      "origin/main. Сделай fetch и rebase этого CL на свежий origin/main в локальном "
      "worktree (создай изолированный worktree, если основной checkout грязный или занят "
      "лизом), разреши все merge conflicts с сохранением доменных инвариантов и поведения, "
      "прогони релевантные unit-тесты (плюс run_flutter_paragraph_tests.py, если затронут "
      "продакшн-код в modules/{skparagraph,skunicode,skshaper}/{src,include}/) и проверки "
      "amnesia-precommit, после чего загрузи обновлённый patchset через `git cl upload`. "
      "Никогда не ставь CQ+1, CQ+2 или Auto-Submit+1."
  )


def rebase_item(item_key: str) -> dict[str, Any]:
  """Wakes (or starts) the chat owning a conflicting self-authored CL/PR to rebase it."""
  ikey = str(item_key or "").strip()
  if not ikey:
    return {"ok": False, "error": "Missing item_key"}

  state = load_watcher_state()
  items: dict[str, Any] = state.setdefault("items", {})
  events: list[dict[str, Any]] = state.setdefault("events", [])
  matched_key, rec = _lookup_state_item(items, ikey)
  if not isinstance(rec, dict):
    return {"ok": False, "error": f"Unknown item {ikey}"}
  if not rec.get("is_self"):
    return {"ok": False, "error": f"Cannot rebase non-self change {ikey}"}
  if rec.get("status") not in ("NEW", "OPEN"):
    return {"ok": False, "error": f"Change {ikey} is not open"}
  if not rec.get("merge_conflict"):
    return {"ok": False, "error": f"Change {ikey} has no merge conflict"}

  prompt = _build_rebase_prompt(rec)
  cid = str(rec.get("conversation_id") or "").strip()
  is_arch: bool | None = None
  comp_to = ""
  if not cid:
    disc_cid, disc_arch, comp_to = _discover_existing_chat_for_item(ikey, rec)
    if disc_cid:
      cid = disc_cid
      is_arch = True if disc_arch else False

  if cid:
    # Follow any handoff descendants so rebase tasks never hit an older mid-chain piece
    chain = cl.discover_conversation_chain(cid, include_descendants=True)
    target_cid = chain[-1] if chain else cid
    summaries = cl._fetch_ls_summaries_for_audit() or {}
    s = summaries.get(target_cid)
    now_ts = time.time()
    in_grace = any(
        isinstance(it, dict)
        and it.get("conversation_id") == target_cid
        and now_ts - float(it.get("chat_woken_ts") or 0.0) < CHAT_WAKE_GRACE_SEC
        for it in items.values()
    )
    ls_busy = False
    target_arch = False if target_cid != cid else is_arch
    if isinstance(s, dict):
      ann = s.get("annotations") or {}
      target_arch = bool(ann.get("archived"))
      title = str(
          ann.get("title") or s.get("summary") or rec.get("title") or ""
      ).strip()
      ls_busy = is_chat_busy(s, "" if target_arch else title)
    if ls_busy or in_grace:
      _rekey_state_item_chat(
          items,
          ikey,
          matched_key,
          rec,
          target_cid,
          chat_archived=target_arch,
          companion_to=comp_to,
      )
      rec["chat_busy"] = True
      save_watcher_state(state)
      return {
          "ok": True,
          "already_busy": True,
          "conversation_id": target_cid,
          "chat_url": f"{jetski_box_url()}/c/{target_cid}",
      }

    if not wake_conversation(target_cid, prompt):
      return {"ok": False, "error": f"Failed to wake conversation {target_cid}"}

    _rekey_state_item_chat(
        items,
        ikey,
        matched_key,
        rec,
        target_cid,
        chat_archived=target_arch,
        companion_to=comp_to,
    )
    rec["chat_busy"] = True
    rec["chat_woken_ts"] = time.time()
    rec["chat_last_modified"] = datetime.now(timezone.utc).isoformat()
    res: dict[str, Any] = {
        "ok": True,
        "woken": True,
        "conversation_id": target_cid,
        "chat_url": f"{jetski_box_url()}/c/{target_cid}",
    }
  else:
    res = _start_pinned_chat_for_record(items, ikey, matched_key, rec, prompt)
    if not res.get("ok"):
      return res
    target_cid = str(res["conversation_id"])

  tid = str(rec.get("target_id") or ikey)
  events.insert(
      0,
      {
          "id": f"ev-{int(time.time() * 1000)}-{tid}",
          "time": now_et_str(),
          "item_key": ikey,
          "target_id": tid,
          "conversation_id": target_cid,
          "project_name": str(rec.get("project_name") or "Unassigned"),
          "critical": False,
          "message": (
              f"[FLIGHT-DECK] 🔧 Dispatched rebase task for {ikey} "
              f"('{rec.get('subject') or ikey}') to chat {target_cid[:8]}."
          ),
      },
  )
  state["events"] = events[:50]
  save_watcher_state(state)
  return res


def _get_gerrit_bearer_token(host: str) -> str:
  """Obtains Gerrit Bearer token from git-credential-luci for `host`."""
  try:
    proc = subprocess.run(
        ["git-credential-luci", "get"],
        input=f"protocol=https\nhost={host}\n\n",
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    for line in proc.stdout.splitlines():
      if line.startswith("password="):
        return line.split("=", 1)[1].strip()
  except (OSError, subprocess.SubprocessError):
    pass
  return ""


def fetch_gerrit_companion_threads(
    host: str,
    parent_cl_id: str,
    companion_cl_ids: list[str],
    *,
    skia_dir: str = DEFAULT_SKIA_DIR,
) -> dict[str, dict[str, Any]]:
  """Inspects comment threads on parent Gerrit CL mentioning companion CL IDs."""
  if not companion_cl_ids or not parent_cl_id or skia_dir == "/nonexistent":
    return {}
  comments_by_path = fetch_gerrit_comments(host, parent_cl_id)
  if not comments_by_path:
    return {}

  # Collect all comments and map each comment ID to its root thread ID
  all_comments: list[dict[str, Any]] = []
  parent_of: dict[str, str] = {}
  for path, clist in comments_by_path.items():
    if not isinstance(clist, list):
      continue
    for c in clist:
      if not isinstance(c, dict):
        continue
      cid = str(c.get("id") or "")
      if not cid:
        continue
      c_copy = dict(c)
      c_copy["_path"] = str(path)
      all_comments.append(c_copy)
      in_reply = str(c.get("in_reply_to") or "")
      if in_reply:
        parent_of[cid] = in_reply

  def get_root(cid: str) -> str:
    visited = set()
    curr = cid
    while curr in parent_of and curr not in visited:
      visited.add(curr)
      curr = parent_of[curr]
    return curr

  threads_by_root: dict[str, list[dict[str, Any]]] = {}
  for c in all_comments:
    cid = str(c.get("id") or "")
    root = get_root(cid)
    threads_by_root.setdefault(root, []).append(c)

  # Sort comments in each thread chronologically by updated time and patchset
  for t in threads_by_root.values():
    t.sort(
        key=lambda x: (
            str(x.get("updated") or ""),
            int(x.get("patch_set") or 0),
        )
    )

  out: dict[str, dict[str, Any]] = {}
  for comp_id in companion_cl_ids:
    comp_str = str(comp_id)
    pat = re.compile(rf"\b{re.escape(comp_str)}\b")
    matching_threads: list[list[dict[str, Any]]] = []
    unresolved_leaves: list[dict[str, Any]] = []

    for t in threads_by_root.values():
      if any(pat.search(str(c.get("message") or "")) for c in t):
        matching_threads.append(t)
        leaf = t[-1]
        if bool(leaf.get("unresolved", False)):
          unresolved_leaves.append({
              "id": str(leaf.get("id") or ""),
              "path": str(leaf.get("_path") or "/PATCHSET_LEVEL"),
              "patch_set": int(leaf.get("patch_set") or 1),
              "line": leaf.get("line"),
          })

    mentioned = bool(matching_threads)
    out[comp_str] = {
        "mentioned": mentioned,
        "all_resolved": mentioned and not unresolved_leaves,
        "any_unresolved": bool(unresolved_leaves),
        "unresolved_leaves": unresolved_leaves,
    }
  return out


def resolve_gerrit_companion_threads(
    host: str,
    parent_cl_id: str,
    companion_cl_id: str,
    token: str = "",
) -> dict[str, Any]:
  """Resolves only the comment threads mentioning companion_cl_id on parent Gerrit CL."""
  thread_info = fetch_gerrit_companion_threads(
      host, parent_cl_id, [companion_cl_id], skia_dir=""
  )
  info = thread_info.get(str(companion_cl_id)) or {}
  leaves = info.get("unresolved_leaves") or []
  if not leaves:
    return {"ok": True, "resolved_count": 0}

  bearer = token or _get_gerrit_bearer_token(host)
  if not bearer:
    return {"ok": False, "error": f"No Gerrit credential for {host}"}

  # Group unresolved leaves by their own patchset because Gerrit rejects cross-PS in_reply_to
  by_ps_and_path: dict[int, dict[str, list[dict[str, Any]]]] = {}
  for leaf in leaves:
    leaf_ps = int(leaf.get("patch_set") or 1)
    p = str(leaf.get("path") or "/PATCHSET_LEVEL")
    comment_payload: dict[str, Any] = {
        "in_reply_to": leaf["id"],
        "message": "Abandoned companion WIP CL.",
        "unresolved": False,
    }
    if leaf.get("line"):
      comment_payload["line"] = leaf["line"]
    by_ps_and_path.setdefault(leaf_ps, {}).setdefault(p, []).append(comment_payload)

  resolved_count = 0
  errors: list[str] = []
  for leaf_ps in sorted(by_ps_and_path):
    by_path = by_ps_and_path[leaf_ps]
    ps_leaf_count = sum(len(v) for v in by_path.values())
    review_url = f"https://{host}/a/changes/{parent_cl_id}/revisions/{leaf_ps}/review"
    req_body = json.dumps({"comments": by_path, "notify": "NONE"}).encode("utf-8")
    req = urllib.request.Request(
        review_url,
        data=req_body,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "flight-deck-cl-watcher/1.0",
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()
      resolved_count += ps_leaf_count
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
      errors.append(f"PS{leaf_ps}: {exc}")

  if errors:
    return {"ok": False, "error": "; ".join(errors), "resolved_count": resolved_count}
  return {"ok": True, "resolved_count": resolved_count}


def abandon_companion_items(
    item_keys: list[str],
    reason: str = "",
    *,
    state: dict[str, Any] | None = None,
    auto: bool = False,
) -> dict[str, Any]:
  """Abandons companion CLs or PRs and resolves parent Gerrit review comments."""
  own_state = state is None
  if own_state:
    state = load_watcher_state()
  assert state is not None
  items = state.setdefault("items", {})
  events = state.setdefault("events", [])
  abandoned: list[str] = []
  errors: list[str] = []
  resolved_total = 0

  for ikey in item_keys:
    ikey = str(ikey).strip()
    if not ikey:
      continue
    # Find matching record(s) in state
    matching_entries = [
        (k, v)
        for k, v in items.items()
        if isinstance(v, dict)
        and (v.get("item_key") == ikey or k.startswith(f"{ikey}@") or k == ikey)
    ]
    if not matching_entries:
      errors.append(f"{ikey}: not found in state")
      continue

    rec = matching_entries[0][1]
    if not rec.get("is_self"):
      errors.append(f"{ikey}: cannot abandon external author's change")
      continue
    if auto and str(rec.get("status") or "").upper() not in ("NEW", "OPEN"):
      continue

    kind = str(rec.get("kind") or "")
    tid = str(rec.get("target_id") or "")
    host = str(rec.get("host") or GERRIT_SKIA_HOST)
    parent_key = str(rec.get("companion_to") or "")
    item_reason = reason or str(rec.get("ready_to_abandon_reason") or "").strip()

    if kind == "gerrit":
      bearer = _get_gerrit_bearer_token(host)
      if not bearer:
        errors.append(f"{ikey}: no Gerrit credential for {host}")
        continue
      url = f"https://{host}/a/changes/{tid}/abandon"
      abandon_msg = (
          f"Auto-abandoning companion WIP CL: {item_reason}."
          if (auto and item_reason)
          else (item_reason or "Abandoning companion WIP CL.")
      )
      # Suppress Gerrit abandon mail so silent %wip sandboxes never spam CC'd parent authors
      body = json.dumps({"message": abandon_msg, "notify": "NONE"}).encode("utf-8")
      req = urllib.request.Request(
          url,
          data=body,
          headers={
              "Authorization": f"Bearer {bearer}",
              "Content-Type": "application/json; charset=UTF-8",
              "User-Agent": "flight-deck-cl-watcher/1.0",
          },
          method="POST",
      )
      try:
        with urllib.request.urlopen(req, timeout=10) as resp:
          resp.read()
      except urllib.error.HTTPError as h_err:
        # HTTP 409 means the change is already abandoned
        if h_err.code != 409:
          errors.append(f"{ikey}: HTTP {h_err.code} abandon failed")
          continue
      except (OSError, urllib.error.URLError, TimeoutError) as exc:
        errors.append(f"{ikey}: abandon request failed ({exc})")
        continue

      # Automatically resolve the companion comment thread on parent Gerrit CL
      if parent_key.startswith("gerrit:"):
        parent_tid = parent_key.split(":", 1)[1]
        res_comments = resolve_gerrit_companion_threads(
            host, parent_tid, tid, token=bearer
        )
        resolved_total += int(res_comments.get("resolved_count") or 0)

    elif kind == "github":
      cmd = [
          "gh", "pr", "close", tid, "--repo", "flutter/flutter", "--delete-branch"
      ]
      proc = subprocess.run(
          cmd, capture_output=True, text=True, timeout=15, check=False
      )
      if proc.returncode != 0:
        cmd_fallback = ["gh", "pr", "close", tid, "--repo", "flutter/flutter"]
        proc2 = subprocess.run(
            cmd_fallback, capture_output=True, text=True, timeout=15, check=False
        )
        if proc2.returncode != 0:
          err_msg = proc2.stderr.strip() or proc.stderr.strip()
          errors.append(f"{ikey}: gh pr close failed: {err_msg}")
          continue
    else:
      errors.append(f"{ikey}: unsupported platform {kind}")
      continue

    # Update companion record status in state
    now_iso = datetime.now(timezone.utc).isoformat()
    et_str, age_str = format_et_and_age(now_iso)
    for _, m_rec in matching_entries:
      m_rec["status"] = "ABANDONED" if kind == "gerrit" else "CLOSED"
      m_rec["phase"] = "ABANDONED"
      m_rec["in_my_attention"] = False
      m_rec["ready_to_abandon"] = False
      m_rec["auto_abandon_eligible"] = False
      m_rec["companion_state"] = "abandoned"
      m_rec["companion_duplicate"] = False
      m_rec["status_changed_raw"] = now_iso
      m_rec["status_changed_et"] = et_str
      m_rec["status_changed_age"] = age_str

    # Update parent record companions list
    if parent_key:
      for p_rec in items.values():
        if isinstance(p_rec, dict) and p_rec.get("item_key") == parent_key:
          p_rec["companions"] = [
              c
              for c in p_rec.get("companions", [])
              if isinstance(c, dict) and c.get("item_key") != ikey
          ]

    abandoned.append(ikey)
    prefix = (
        "[CL-WATCHER] 🗑️ Auto-abandoned"
        if auto
        else "[FLIGHT-DECK] 🗑️ Abandoned"
    )
    reason_sfx = f" — {item_reason}" if (auto and item_reason) else ""
    events.insert(
        0,
        {
            "id": f"ev-{int(time.time() * 1000)}-{tid}",
            "time": now_et_str(),
            "item_key": ikey,
            "target_id": tid,
            "conversation_id": rec.get("conversation_id", ""),
            "project_name": str(rec.get("project_name") or "Unassigned"),
            "critical": False,
            "message": (
                f"{prefix} companion {ikey} "
                f"('{rec.get('subject') or ikey}'){reason_sfx}."
            ),
        },
    )

  state["events"] = events[:50]
  if own_state:
    save_watcher_state(state)
  return {
      "ok": len(errors) == 0,
      "abandoned": abandoned,
      "resolved_threads": resolved_total,
      "errors": errors,
  }


def assign_second_reviewer_items(
    item_keys: list[str],
    reviewer_email: str,
    *,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Adds a @google.com second reviewer on Gerrit CLs and updates cached state."""
  rev_email = str(reviewer_email or "").strip()
  self_lower = {e.lower() for e in SELF_EMAILS}
  if (
      "@" not in rev_email
      or not rev_email.lower().endswith("@google.com")
      or rev_email.lower() in self_lower
  ):
    return {
        "ok": False,
        "assigned": [],
        "errors": [f"invalid Googler reviewer email: {rev_email}"],
    }

  own_state = state is None
  lookup_state = load_watcher_state() if own_state else state
  assert lookup_state is not None
  lookup_items = lookup_state.get("items") or {}
  assigned_meta: list[tuple[str, str]] = []
  errors: list[str] = []
  dedup_keys = list(dict.fromkeys(str(k or "").strip() for k in item_keys))

  for ikey in dedup_keys:
    if not ikey:
      continue
    matching_lookup = [
        v
        for k, v in lookup_items.items()
        if isinstance(v, dict)
        and (
            v.get("item_key") == ikey
            or k.startswith(f"{ikey}@")
            or k == ikey
        )
    ]
    if not matching_lookup:
      errors.append(f"{ikey}: not found in state")
      continue

    rec = matching_lookup[0]
    kind = str(rec.get("kind") or "gerrit")
    tid = str(rec.get("target_id") or "")
    host = str(rec.get("host") or GERRIT_SKIA_HOST)
    if kind != "gerrit" or not tid:
      errors.append(f"{ikey}: unsupported platform {kind}")
      continue

    bearer = _get_gerrit_bearer_token(host)
    if not bearer:
      errors.append(f"{ikey}: no Gerrit credential for {host}")
      continue

    url = f"https://{host}/a/changes/{tid}/reviewers"
    body = json.dumps({"reviewer": rev_email}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {bearer}",
            "Content-Type": "application/json; charset=UTF-8",
            "User-Agent": "flight-deck-cl-watcher/1.0",
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=10) as resp:
        raw_resp = resp.read().decode("utf-8", errors="replace").strip()
    except urllib.error.HTTPError as h_err:
      errors.append(f"{ikey}: HTTP {h_err.code} add reviewer failed")
      continue
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
      errors.append(f"{ikey}: add reviewer request failed ({exc})")
      continue

    if raw_resp.startswith(")]}'"):
      raw_resp = raw_resp[4:].strip()
    # Gerrit /reviewers returns HTTP 200 with an AddReviewerResult error field on failure
    if raw_resp:
      try:
        resp_obj = json.loads(raw_resp)
      except ValueError:
        resp_obj = None
      if isinstance(resp_obj, dict) and resp_obj.get("error"):
        errors.append(f"{ikey}: {resp_obj['error']}")
        continue

    assigned_meta.append((ikey, tid))

  assigned = [ikey for ikey, _ in assigned_meta]
  # Re-read watcher state after network I/O so concurrent sync_once flushes are preserved
  if assigned_meta:
    target_state = load_watcher_state() if own_state else state
    assert target_state is not None
    items = target_state.setdefault("items", {})
    events = target_state.setdefault("events", [])
    for ikey, tid in assigned_meta:
      matching_entries = [
          (k, v)
          for k, v in items.items()
          if isinstance(v, dict)
          and (
              v.get("item_key") == ikey
              or k.startswith(f"{ikey}@")
              or k == ikey
          )
      ]
      conv_id = ""
      proj_name = "Unassigned"
      for _, m_rec in matching_entries:
        conv_id = conv_id or str(m_rec.get("conversation_id") or "")
        proj_name = str(m_rec.get("project_name") or proj_name)
        req_app = int(m_rec.get("required_approvals") or 2)
        prev_assigned = int(m_rec.get("assigned_reviewers_count") or 1)
        pings = [
            p for p in (m_rec.get("reviewer_pings") or []) if isinstance(p, dict)
        ]
        cur_revs = {
            str(r) for r in (m_rec.get("assigned_reviewers") or []) if r
        }
        already_listed = (
            any(
                str(p.get("reviewer") or "").lower() == rev_email.lower()
                for p in pings
            )
            or rev_email.lower() in {r.lower() for r in cur_revs}
        )
        cur_revs.add(rev_email)
        m_rec["assigned_reviewers"] = sorted(cur_revs)
        new_assigned = prev_assigned if already_listed else (prev_assigned + 1)
        miss_rev = max(0, req_app - new_assigned)
        m_rec["assigned_reviewers_count"] = new_assigned
        m_rec["missing_reviewers_to_assign"] = miss_rev
        if miss_rev == 0 and m_rec.get("sponsor_action") == "assign_second_reviewer":
          m_rec["sponsor_action"] = ""
          m_rec["sponsor_action_reason"] = ""
        m_rec["suggested_second_reviewers"] = []
        miss_app = int(m_rec.get("missing_approvals") or 1)
        app_cnt = int(m_rec.get("approved_count") or 1)
        if miss_rev == 0 and miss_app > 0:
          m_rec["reviewer_status"] = (
              f"Needs +{miss_app} CR ({app_cnt}/{req_app} approved)"
          )
        if not already_listed:
          pings.append({
              "reviewer": rev_email,
              "reason": "needs_second_approval",
              "active": True,
              "in_attention_set": True,
              "current_ps": int(m_rec.get("patchset") or 1),
          })
        m_rec["reviewer_pings"] = pings
        w_on = [
            w_entry
            for w_entry in (m_rec.get("waiting_on") or [])
            if isinstance(w_entry, dict)
        ]
        if not any(
            str(w_entry.get("id") or "").lower() == rev_email.lower()
            for w_entry in w_on
        ):
          w_on.append({"id": rev_email, "active": True})
        m_rec["waiting_on"] = w_on

      events.insert(
          0,
          {
              "id": f"ev-{int(time.time() * 1000)}-{tid}",
              "time": now_et_str(),
              "item_key": ikey,
              "target_id": tid,
              "conversation_id": conv_id,
              "project_name": proj_name,
              "critical": False,
              "message": f"[FLIGHT-DECK] 👤 Added 2nd reviewer {rev_email} on {ikey}.",
          },
      )
    target_state["events"] = events[:50]
    if own_state:
      save_watcher_state(target_state)

  return {
      "ok": len(errors) == 0 and len(assigned) > 0,
      "assigned": assigned,
      "reviewer": rev_email,
      "errors": errors,
  }


def compute_companion_states(
    new_items: dict[str, Any],
    *,
    skia_dir: str = DEFAULT_SKIA_DIR,
    parent_fallback_cache: dict[str, dict[str, Any]] | None = None,
) -> None:
  """Suppresses companion noise, computes ready-to-abandon triggers, and deduplicates."""
  by_ikey: dict[str, list[dict[str, Any]]] = {}
  for rec in new_items.values():
    if isinstance(rec, dict) and rec.get("item_key"):
      by_ikey.setdefault(str(rec["item_key"]), []).append(rec)

  # Suppress author-centric noise on all companion records
  for rec in new_items.values():
    if not isinstance(rec, dict) or not rec.get("companion_to"):
      continue
    rec["is_companion"] = True
    rec["merge_conflict"] = False
    rec["missing_reviewers_to_assign"] = 0
    rec["assigned_reviewers"] = []
    rec["missing_approvals"] = 0
    rec["reviewer_status"] = ""
    rec["sponsor_action"] = ""
    rec["sponsor_action_reason"] = ""
    rec["suggested_second_reviewers"] = []
    rec["reviewer_pings"] = []
    rec["cq_failed"] = False
    rec["cq_sync_rebase_needed"] = False
    rec["tree_red"] = False
    rec["is_draft"] = False
    rec["in_development"] = False
    rec["is_watching"] = False
    rec["auto_abandon_eligible"] = False
    if str(rec.get("status") or "").upper() in ("NEW", "OPEN"):
      rec["in_my_attention"] = False
      rec["phase"] = "WAITING_ON_OTHERS"
      rec["companion_state"] = "active"
      rec["ready_to_abandon"] = False
      rec["ready_to_abandon_reason"] = ""
      rec["companion_duplicate"] = False
    else:
      rec["companion_state"] = "abandoned"
      rec["ready_to_abandon"] = False
      rec["ready_to_abandon_reason"] = ""
      rec["companion_duplicate"] = False

  # Group companions by parent item key
  comps_by_parent: dict[str, list[dict[str, Any]]] = {}
  for rec in new_items.values():
    if isinstance(rec, dict) and rec.get("companion_to"):
      comps_by_parent.setdefault(str(rec["companion_to"]), []).append(rec)

  for parent_key, comp_recs in comps_by_parent.items():
    parent_recs = by_ikey.get(parent_key) or []
    parent_rec = (
        parent_recs[0]
        if parent_recs
        else ((parent_fallback_cache or {}).get(parent_key) or {})
    )
    parent_status = str(parent_rec.get("status") or "").upper()
    parent_ps = int(parent_rec.get("patchset") or 0)
    parent_self_approved = bool(parent_rec.get("self_approved", False))
    parent_approval_ps = int(
        parent_rec.get("self_approval_ps") or parent_ps or 0
    )
    parent_revs = parent_rec.get("revision_ps_by_sha") or {}
    parent_head_sha = str(parent_rec.get("head_sha") or "").strip()

    # Map companion revisions so chained A -> B companions share base_ps and avoid false dedup
    comp_parent_by_sha: dict[str, str] = {}
    comp_ikey_by_sha: dict[str, str] = {}
    comp_ikey_by_head_ref: dict[str, str] = {}
    for cr in comp_recs:
      cr_ikey = str(cr.get("item_key") or "")
      cr_pcommit = str(cr.get("parent_commit") or "").strip()
      for rev_sha in (cr.get("revision_ps_by_sha") or {}).keys():
        if rev_sha:
          comp_parent_by_sha[str(rev_sha)] = cr_pcommit
          if cr_ikey:
            comp_ikey_by_sha[str(rev_sha)] = cr_ikey
      href = str(cr.get("head_ref") or "").strip()
      if href and cr_ikey:
        comp_ikey_by_head_ref[href] = cr_ikey

    # Map open companions by unique item_key and record parent/child companion chains
    open_comps_by_ikey: dict[str, list[dict[str, Any]]] = {}
    comp_chain_adj: dict[str, set[str]] = {}
    for r in comp_recs:
      if str(r.get("status") or "").upper() in ("NEW", "OPEN"):
        r_ikey = str(r.get("item_key") or "")
        open_comps_by_ikey.setdefault(r_ikey, []).append(r)
        comp_chain_adj.setdefault(r_ikey, set())
        cur_sha = str(r.get("parent_commit") or "").strip()
        visited_shas: set[str] = set()
        for _ in range(32):
          if not cur_sha or cur_sha in parent_revs or cur_sha in visited_shas:
            break
          visited_shas.add(cur_sha)
          anc_ikey = comp_ikey_by_sha.get(cur_sha, "")
          if anc_ikey and anc_ikey != r_ikey:
            comp_chain_adj.setdefault(r_ikey, set()).add(anc_ikey)
            comp_chain_adj.setdefault(anc_ikey, set()).add(r_ikey)
          if cur_sha not in comp_parent_by_sha:
            break
          cur_sha = comp_parent_by_sha[cur_sha]
        bref = str(r.get("base_ref") or "").strip()
        anc_gh = comp_ikey_by_head_ref.get(bref, "") if bref else ""
        if anc_gh and anc_gh != r_ikey:
          comp_chain_adj.setdefault(r_ikey, set()).add(anc_gh)
          comp_chain_adj.setdefault(anc_gh, set()).add(r_ikey)
        base_ps = int(parent_revs.get(cur_sha, 0)) or int(
            r.get("companion_base_ps_hint") or 0
        )
        r["companion_base_ps"] = base_ps

    thread_map: dict[str, dict[str, Any]] = {}
    if parent_key.startswith("gerrit:") and open_comps_by_ikey:
      p_host = str(
          parent_rec.get("host")
          or comp_recs[0].get("host")
          or GERRIT_SKIA_HOST
      )
      p_tid = parent_key.split(":", 1)[1]
      target_ids = [
          r_list[0].get("target_id", "")
          for r_list in open_comps_by_ikey.values()
          if r_list[0].get("target_id")
      ]
      thread_map = fetch_gerrit_companion_threads(
          p_host, p_tid, target_ids, skia_dir=skia_dir
      )

    # Evaluate deterministic ready-to-abandon and auto-abandon triggers
    for ikey, r_list in open_comps_by_ikey.items():
      r0 = r_list[0]
      kind = str(r0.get("kind") or "")
      tid = str(r0.get("target_id") or "")
      t_info = thread_map.get(tid) or {}
      mentioned = bool(t_info.get("mentioned", False))
      all_resolved = bool(t_info.get("all_resolved", False))
      base_ps = int(r0.get("companion_base_ps") or 0)
      comp_shas = set(r0.get("commit_shas") or ())
      # Require approval on a revision strictly newer than the companion's base patchset
      approved_newer = (
          (base_ps > 0 and parent_approval_ps > base_ps)
          if kind != "github"
          else (not parent_head_sha or parent_head_sha not in comp_shas)
      )
      p_label = str(r0.get("companion_to_label") or parent_key)
      reason = ""
      auto_eligible = False

      if parent_status in ("MERGED", "ABANDONED", "CLOSED", "SUBMITTED"):
        reason = f"Parent {p_label} is {parent_status.lower()}"
        auto_eligible = True
      elif parent_self_approved and approved_newer:
        ps_sfx = (
            f" (PS{parent_approval_ps})"
            if (kind != "github" and parent_approval_ps)
            else ""
        )
        reason = f"You approved {p_label}{ps_sfx}"
        auto_eligible = True
      elif all_resolved and (base_ps == 0 or parent_ps > base_ps):
        reason = f"Companion thread resolved on {p_label}"

      for r in r_list:
        r["companion_mentioned"] = mentioned
        r["companion_thread_resolved"] = all_resolved
        r["ready_to_abandon"] = bool(reason)
        r["ready_to_abandon_reason"] = reason
        r["auto_abandon_eligible"] = auto_eligible

    # Deduplicate parallel open companion chains while keeping stacked A -> B companions intact
    if len(open_comps_by_ikey) > 1:
      def comp_rank_key(k: str) -> tuple[int, int, float, int]:
        first_r = open_comps_by_ikey[k][0]
        ment = 1 if first_r.get("companion_mentioned") else 0
        bps = int(first_r.get("companion_base_ps") or 0)
        act = float(first_r.get("activity_ts") or 0.0)
        tid_str = str(first_r.get("target_id") or "")
        tid_int = int(tid_str) if tid_str.isdigit() else 0
        return (ment, bps, act, tid_int)

      seen_comp: set[str] = set()
      components: list[list[str]] = []
      for k in open_comps_by_ikey:
        if k in seen_comp:
          continue
        comp_group: list[str] = []
        queue = [k]
        while queue:
          cur_k = queue.pop()
          if cur_k in seen_comp or cur_k not in open_comps_by_ikey:
            continue
          seen_comp.add(cur_k)
          comp_group.append(cur_k)
          for nb in comp_chain_adj.get(cur_k, ()):
            if nb not in seen_comp:
              queue.append(nb)
        components.append(comp_group)

      if len(components) > 1:
        components.sort(
            key=lambda grp: max(comp_rank_key(x) for x in grp), reverse=True
        )
        canonical_key = max(components[0], key=comp_rank_key)
        can_r0 = open_comps_by_ikey[canonical_key][0]
        can_label = (
            f"CL {can_r0.get('target_id')}"
            if can_r0.get("kind") == "gerrit"
            else f"PR #{can_r0.get('target_id')}"
        )
        for dup_grp in components[1:]:
          for dup_key in dup_grp:
            for r in open_comps_by_ikey[dup_key]:
              r["companion_duplicate"] = True
              r["companion_superseded_by"] = can_label
              r["ready_to_abandon"] = True
              r["auto_abandon_eligible"] = True
              if not r.get("ready_to_abandon_reason"):
                r["ready_to_abandon_reason"] = (
                    f"Duplicate companion (superseded by {can_label})"
                )

    # Route ready-to-abandon companions to Needs You and build plain-text status note
    for r in comp_recs:
      if str(r.get("status") or "").upper() in ("NEW", "OPEN"):
        if r.get("ready_to_abandon"):
          r["companion_state"] = (
              "duplicate" if r.get("companion_duplicate") else "ready_to_abandon"
          )
          r["in_my_attention"] = True
          r["phase"] = "NEEDS_YOUR_ACTION"
        else:
          r["companion_state"] = "active"
        cq_st = str(r.get("cq_status") or "")
        cq_part = f" · {cq_st}" if (cq_st and cq_st != "Idle") else ""
        if r.get("ready_to_abandon_reason"):
          r["companion_status_note"] = str(r["ready_to_abandon_reason"])
        elif r.get("companion_base_ps"):
          bps = int(r["companion_base_ps"])
          if parent_status in ("NEW", "OPEN") and parent_ps > bps:
            r["companion_status_note"] = (
                f"Stacked on old PS{bps} (parent at PS{parent_ps}){cq_part}"
            )
          else:
            r["companion_status_note"] = (
                f"Stacked on PS{bps}{cq_part} · waiting on author"
            )
        elif cq_part:
          r["companion_status_note"] = f"{cq_st} · waiting on author"
        else:
          r["companion_status_note"] = "Waiting on author"

    # Synchronize enriched metadata onto the parent record's companions list
    for p_rec in parent_recs:
      # Exclude abandoned or closed companions from parent view
      active_comps = [
          c
          for c in p_rec.get("companions", [])
          if isinstance(c, dict)
          and c.get("status") not in ("ABANDONED", "CLOSED")
      ]
      for c in active_comps:
        matching_r = next(
            (r for r in comp_recs if r.get("item_key") == c.get("item_key")),
            None,
        )
        if matching_r:
          c["status"] = matching_r.get("status", "")
          c["patchset"] = matching_r.get("patchset", 1)
          c["base_ps"] = matching_r.get("companion_base_ps", 0)
          c["companion_state"] = matching_r.get("companion_state", "active")
          c["ready_to_abandon"] = matching_r.get("ready_to_abandon", False)
          c["ready_to_abandon_reason"] = matching_r.get(
              "ready_to_abandon_reason", ""
          )
          c["companion_duplicate"] = matching_r.get(
              "companion_duplicate", False
          )
          c["companion_status_note"] = matching_r.get(
              "companion_status_note", ""
          )
      p_rec["companions"] = active_comps


def _is_bug_bot_author(author: str) -> bool:
  """Returns True for automated commit/CI bots whose bug comments need no human reply."""
  low = (author or "").strip().lower()
  if not low:
    return True
  bot_tokens = (
      "gitwatcher@",
      "gerritcodereview",
      "skcq-be",
      "bugdroid",
      "autoroll",
      "github-actions",
      "fluttergithubbot",
      "dependabot",
      "gemini-code-assist",
  )
  if any(tok in low for tok in bot_tokens):
    return True
  return low.endswith(("[bot]", "-bot", ".gserviceaccount.com"))


def _unwrap_mcp_or_json_text(raw_text: str) -> str:
  """Extracts plaintext payload from remote_mcp or JSON-wrapped CLI output."""
  trimmed = (raw_text or "").strip()
  if not trimmed.startswith("{"):
    return raw_text or ""
  try:
    data = json.loads(trimmed)
  except ValueError:
    return raw_text or ""
  if not isinstance(data, dict):
    return raw_text or ""
  if isinstance(data.get("result"), str):
    return data["result"]
  res_obj = data.get("result")
  if isinstance(res_obj, dict):
    content = res_obj.get("content")
    if isinstance(content, list) and content and isinstance(content[0], dict):
      if isinstance(content[0].get("text"), str):
        return _unwrap_mcp_or_json_text(content[0]["text"])
  return raw_text or ""


def _is_placeholder_bug_title(title: str) -> bool:
  """Returns True when `title` is empty, redacted, or a synthesized fallback label."""
  t = (title or "").strip()
  if not t or t.lower() == "[redacted]":
    return True
  if t.startswith("[External title redacted"):
    return True
  return bool(
      re.match(r"^Bug\s+(?:\d+|buganizer:|gh_issue:)", t, re.IGNORECASE)
  )


def _format_redacted_fallback_title(bug: dict[str, Any]) -> str:
  """Builds an explicit metadata label when Buganizer redacts an external issue title."""
  tags = str(bug.get("component_tags") or "").strip()
  rep = str(bug.get("reporter") or "").strip()
  parts = ["[External title redacted]"]
  if tags:
    parts.append(f"· {tags}")
  if rep:
    parts.append(f"({rep})")
  return " ".join(parts)


def _normalize_public_gerrit_url(raw_url: str) -> str:
  """Converts internal `.git.corp.google.com` Gerrit URLs to public `.googlesource.com`."""
  return re.sub(
      r"\.git\.corp\.google\.com/",
      ".googlesource.com/",
      (raw_url or "").strip().rstrip(",.;)"),
      flags=re.IGNORECASE,
  )


def extract_merge_milestone(title: str) -> int | None:
  """Extracts the integer milestone N from a `[Merge M<N>] ...` issue title."""
  m = RE_MERGE_TITLE_MILESTONE.match((title or "").strip())
  return int(m.group(1)) if m else None


def _bug_merge_milestone(bug: dict[str, Any]) -> int | None:
  """Resolves the target Chrome milestone from custom field 1340419 or the `[Merge M<N>]` title."""
  cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
  raw_ms = str(cf.get(CF_BUG_MERGE_MILESTONE) or bug.get("merge_milestone") or "").strip()
  if raw_ms.isdigit():
    return int(raw_ms)
  return extract_merge_milestone(str(bug.get("title") or ""))


def is_merge_request_bug(bug: dict[str, Any]) -> bool:
  """Returns True when `bug` is a Chrome release `[Merge M<NN>]` child issue in any state."""
  if not isinstance(bug, dict):
    return False
  if str(bug.get("source") or "buganizer") != "buganizer":
    return False
  if bool(bug.get("is_merge_request_bug")) or bool(bug.get("is_security_merge_request")):
    return True
  comp_id = str(bug.get("component_id") or "").strip()
  reporter = str(bug.get("reporter") or "").strip().lower()
  comp_tags = str(bug.get("component_tags") or "").strip()
  cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
  has_merge_origin = (
      comp_id == SECURITY_MERGE_COMPONENT_ID
      or reporter == SECURITY_MERGE_REPORTER
      or "Merge Request" in comp_tags
  )
  has_merge_marker = (
      extract_merge_milestone(str(bug.get("title") or "")) is not None
      or bool(str(cf.get(CF_BUG_MERGE) or "").strip())
  )
  return bool(has_merge_origin and has_merge_marker)


def _finalize_buganizer_entry(entry: dict[str, Any]) -> dict[str, Any]:
  """Computes canonical bug_key, tracker_flavor (b vs crbug), short_id, and URL."""
  issue_id = str(entry.get("id") or "").strip()
  comp_id = str(entry.get("component_id") or "").strip()
  comp_tags = str(entry.get("component_tags") or "").strip()
  assignee = str(entry.get("assignee") or "").strip().lower()
  reporter = str(entry.get("reporter") or "").strip().lower()
  gerrit_ids = list(entry.get("gerrit_cl_ids") or [])
  # Normalize 'DUPLICATE of <id>' so closed duplicates never leak into open lanes
  raw_status = str(entry.get("status") or "ASSIGNED").strip().upper()
  dup_of = str(entry.get("duplicate_of") or "").strip()
  m_dup = re.match(r"^DUPLICATE(?:\s+OF\s+(\d+))?\b", raw_status)
  if m_dup:
    raw_status = "DUPLICATE"
    if m_dup.group(1) and not dup_of:
      dup_of = m_dup.group(1)
  is_crbug = bool(
      comp_tags
      or comp_id in PUBLIC_CRBUG_COMPONENTS
      or assignee.endswith("@chromium.org")
      or reporter.endswith("@chromium.org")
      or gerrit_ids
  )
  flavor = "crbug" if is_crbug else "b"
  short_id = f"{flavor}/{issue_id}"
  url = (
      f"https://issues.chromium.org/issues/{issue_id}"
      if is_crbug
      else f"https://b.corp.google.com/issues/{issue_id}"
  )
  raw_title = str(entry.get("title") or "").strip()
  redacted = bool(entry.get("title_redacted")) or raw_title.lower() == "[redacted]"
  if redacted:
    raw_title = ""
  mod_raw = str(
      entry.get("modified_time")
      or entry.get("major_modified_time")
      or entry.get("created_time")
      or ""
  )
  mod_et, mod_age = format_et_and_age(mod_raw)
  out = {
      **entry,
      "bug_key": f"buganizer:{issue_id}",
      "source": "buganizer",
      "id": issue_id,
      "status": raw_status,
      "duplicate_of": dup_of,
      "tracker_flavor": flavor,
      "short_id": short_id,
      "url": url,
      "title": raw_title,
      "title_redacted": redacted,
      "modified_et": mod_et,
      "modified_age": mod_age,
      "modified_ts": _parse_ts(mod_raw),
  }
  out["is_merge_request_bug"] = is_merge_request_bug(out)
  ms_num = _bug_merge_milestone(out) if out["is_merge_request_bug"] else None
  if ms_num is not None:
    out["merge_milestone"] = ms_num
  return out


RE_GERRIT_ANY_URL_ID = re.compile(
    r"https?://(?:skia|chromium)-review"
    r"(?:\.git\.corp\.google|\.googlesource)\.com/"
    r"(?:c/(?:[^+\s]*/\+/)?)?(\d{6,10})\b",
    re.IGNORECASE,
)


def parse_buganizer_blocks(output_text: str) -> list[dict[str, Any]]:
  """Parses get_bugs_with_external, issues search, or batch-get output blocks."""
  text = _unwrap_mcp_or_json_text(output_text)
  if not text.strip():
    return []

  results: list[dict[str, Any]] = []
  if "Issue ID:" in text:
    blocks = re.split(r"(?=Issue ID:\s*\d+)", text)
    for block in blocks:
      m_id = re.search(r"Issue ID:\s*(\d+)", block)
      if not m_id:
        continue
      issue_id = m_id.group(1)
      entry: dict[str, Any] = {
          "id": issue_id,
          "status": "ASSIGNED",
          "priority": "P2",
          "severity": "",
          "issue_type": "BUG",
          "component_id": "",
          "component_tags": "",
          "assignee": "",
          "reporter": "",
          "title": "",
          "created_time": "",
          "modified_time": "",
          "major_modified_time": "",
          "resolved_time": "",
          "due_date": "",
          "parent_issue_id": "",
          "custom_fields": {},
          "slo": {},
          "gerrit_cl_ids": [],
          "gerrit_cl_urls": {},
      }
      pending_cf = ""
      in_slo = False
      for raw_line in block.splitlines():
        sline = raw_line.strip()
        if not sline or sline == "---" or sline.startswith("Note:"):
          in_slo = False
          continue
        if in_slo and raw_line.startswith("  "):
          slo_m = re.match(r"([A-Za-z ]+SLO):\s*(\S+)", sline)
          if slo_m:
            slo_key = slo_m.group(1).strip().lower().replace(" ", "_")
            entry["slo"][slo_key] = slo_m.group(2).strip()
          continue
        in_slo = False
        if sline == "SLO Status:":
          in_slo = True
          continue
        kv = re.match(r"^([A-Za-z][A-Za-z0-9 _-]*):\s*(.*)$", sline)
        if not kv:
          continue
        k, val = kv.group(1).strip(), kv.group(2).strip()
        if k == "Title":
          entry["title"] = val
        elif k == "Component ID":
          entry["component_id"] = val
        elif k == "Assignee":
          entry["assignee"] = val
        elif k == "Reporter":
          entry["reporter"] = val
        elif k == "Status":
          entry["status"] = val.upper()
        elif k == "Priority":
          entry["priority"] = val.upper()
        elif k == "Severity":
          entry["severity"] = val.upper()
        elif k == "Issue Type":
          entry["issue_type"] = val.upper()
        elif k == "Created Time":
          entry["created_time"] = val
        elif k == "Modified Time":
          entry["modified_time"] = val
        elif k == "Major Modified Time":
          entry["major_modified_time"] = val
        elif k == "Resolved Time":
          entry["resolved_time"] = val
        elif k == "Due Date":
          entry["due_date"] = val
        elif k in ("Parent Issue IDs", "Parent Issue ID"):
          m_parent = re.search(r"(\d+)", val)
          if m_parent:
            entry["parent_issue_id"] = m_parent.group(1)
        elif k == "Gerrit changes":
          cids: list[str] = []
          url_map: dict[str, str] = {}
          for m_g in RE_GERRIT_ANY_URL_ID.finditer(val):
            gid = m_g.group(1)
            cids.append(gid)
            url_map.setdefault(gid, _normalize_public_gerrit_url(m_g.group(0)))
          entry["gerrit_cl_ids"] = list(dict.fromkeys(cids))
          entry["gerrit_cl_urls"] = url_map
        elif k == "Custom Field":
          pending_cf = val
        elif k == "Custom Field Value":
          if "Component Tags" in pending_cf and not entry["component_tags"]:
            entry["component_tags"] = val
          # Concatenate repeated Custom Field Value entries under their numeric field ID
          m_cf = RE_CUSTOM_FIELD_ID.search(pending_cf)
          if m_cf and val:
            cf_id = m_cf.group(1)
            prev_cf = entry["custom_fields"].get(cf_id, "")
            if not prev_cf:
              entry["custom_fields"][cf_id] = val
            elif val not in prev_cf:
              entry["custom_fields"][cf_id] = f"{prev_cf}, {val}"
          pending_cf = ""
      results.append(_finalize_buganizer_entry(entry))
    return results

  # Parse compact `- Issue <id>: <title> [<STATUS>] (Priority: P1, Assignee: ...)` batch-get lines
  cur: dict[str, Any] | None = None
  for raw_line in text.splitlines():
    m_row = re.match(
        r"^\s*-\s*Issue\s+(\d+):\s*(.+?)\s+\[([A-Z0-9_ ]+)\]\s*\(([^)]*)\)",
        raw_line,
    )
    if m_row:
      if cur is not None:
        results.append(_finalize_buganizer_entry(cur))
      meta_blob = m_row.group(4)
      m_pri = re.search(r"Priority:\s*(P\d)", meta_blob, re.IGNORECASE)
      m_asg = re.search(r"Assignee:\s*([^,\s)]+)", meta_blob, re.IGNORECASE)
      cur = {
          "id": m_row.group(1),
          "title": m_row.group(2).strip(),
          "status": m_row.group(3).upper(),
          "priority": m_pri.group(1).upper() if m_pri else "P2",
          "assignee": m_asg.group(1).strip() if m_asg else "",
          "severity": "",
          "issue_type": "BUG",
          "slo": {},
          "gerrit_cl_ids": [],
      }
      continue
    if cur is not None:
      m_sev = re.search(r"Severity:\s*(S\d)", raw_line, re.IGNORECASE)
      m_typ = re.search(r"Type:\s*([A-Z_]+)", raw_line, re.IGNORECASE)
      if m_sev:
        cur["severity"] = m_sev.group(1).upper()
      if m_typ:
        cur["issue_type"] = m_typ.group(1).upper()
  if cur is not None:
    results.append(_finalize_buganizer_entry(cur))
  return results


# Bump whenever parse_rendered_issue_comments changes so cached bugs re-render once
COMMENTS_PARSER_VERSION = 2

# Terminates each comment body only at the next top-level Comment #N header or EOF
# because render_issue_with_external uses blank lines between comments and --- inside bodies
RE_RENDERED_ISSUE_COMMENT = re.compile(
    r"^Comment\s+#(\d+)\s+by\s+(\S+)\s+at\s+([^\r\n]+?):[ \t]*(?:\r?\n(.*?))?"
    r"(?=^Comment\s+#\d+\s+by\s+\S+\s+at\s+[^\r\n]+?:[ \t]*(?:\r?\n|\Z)|\Z)",
    re.MULTILINE | re.DOTALL,
)


def _bug_comments_mod_stamp(bug: dict[str, Any]) -> str:
  """Builds a versioned modification token covering both Modified Time and Major Modified Time."""
  mod_tok = str(
      bug.get("modified_time") or bug.get("major_modified_time") or ""
  ).strip()
  if not mod_tok:
    return ""
  maj_tok = str(bug.get("major_modified_time") or "").strip()
  return f"v{COMMENTS_PARSER_VERSION}:{mod_tok}|{maj_tok}"


def parse_rendered_issue_comments(rendered_text: str) -> dict[str, Any]:
  """Extracts latest human vs self comment metadata and CL links from rendered bug text."""
  text = _unwrap_mcp_or_json_text(rendered_text)
  res: dict[str, Any] = {
      "last_human_comment_author": "",
      "last_human_comment_at": "",
      "last_human_comment_snippet": "",
      "last_self_comment_at": "",
      "comment_cl_ids": [],
      "comment_cl_urls": {},
  }
  if not text:
    return res

  cl_ids: list[str] = []
  cl_urls: dict[str, str] = {}
  self_lower = {e.lower() for e in SELF_BUG_EMAILS} | {
      u.lower() for u in SELF_GITHUB_LOGINS
  }
  for m in RE_RENDERED_ISSUE_COMMENT.finditer(text):
    author = m.group(2).strip()
    author_low = author.lower()
    ts_raw = m.group(3).strip()
    body = (m.group(4) or "").strip()
    # Extract CL links only from gitwatcher commit-landing bots or self comments
    if "gitwatcher" in author_low or author_low in self_lower:
      for m_g in RE_GERRIT_ANY_URL_ID.finditer(body):
        gid = m_g.group(1)
        cl_ids.append(gid)
        cl_urls.setdefault(gid, _normalize_public_gerrit_url(m_g.group(0)))
    # Keep self comments separate so last_human_comment_* preserves the latest non-self human
    if author_low in self_lower:
      res["last_self_comment_at"] = ts_raw
      continue
    if _is_bug_bot_author(author):
      continue
    # Strip collapsed HTML zippy blocks before building the single-line preview
    clean_body = re.sub(r"<details[\s\S]*?</details>", "", body)
    clean_body = re.sub(r"\*Comment created using[^\n]*\*", "", clean_body)
    # Drop standalone markdown horizontal rules and Buganizer edit footers from snippets
    clean_lines = [
        ln
        for ln in clean_body.splitlines()
        if ln.strip() != "---"
        and not re.match(r"^Last modified on \d{4}-\d{2}-\d{2}T\S+$", ln.strip())
    ]
    snippet = " ".join(" ".join(clean_lines).split())[:180]
    res["last_human_comment_author"] = author
    res["last_human_comment_at"] = ts_raw
    res["last_human_comment_snippet"] = snippet
  res["comment_cl_ids"] = list(dict.fromkeys(cl_ids))
  res["comment_cl_urls"] = cl_urls
  return res


def merge_bug_record_with_prev(
    curr: dict[str, Any], prev: dict[str, Any] | None
) -> dict[str, Any]:
  """Merges fresh bug fields with cached state, preserving real titles across redaction."""
  merged = dict(curr)
  if isinstance(prev, dict):
    prev_title = str(prev.get("title") or "").strip()
    # Only carry over a genuine unredacted title, never a stale 'Bug <id>' placeholder
    if (
        (merged.get("title_redacted") or not merged.get("title"))
        and prev_title
        and not prev.get("title_redacted")
        and not _is_placeholder_bug_title(prev_title)
    ):
      merged["title"] = prev_title
      merged["title_redacted"] = False
    # Keep closed state_reason across transient gh api enrichment failures
    if (
        merged.get("status") == "CLOSED"
        and not merged.get("state_reason")
        and prev.get("status") == "CLOSED"
        and prev.get("state_reason")
    ):
      merged["state_reason"] = str(prev["state_reason"])
    if prev.get("tracker_flavor") == "crbug" and merged.get("tracker_flavor") != "crbug":
      merged["tracker_flavor"] = "crbug"
      merged["short_id"] = f"crbug/{merged['id']}"
      merged["url"] = f"https://issues.chromium.org/issues/{merged['id']}"
    comment_keys = {
        "last_human_comment_author",
        "last_human_comment_at",
        "last_human_comment_snippet",
        "last_self_comment_at",
        "comment_cl_ids",
        "comment_cl_urls",
        "comments_checked_major_mod",
    }
    for carry_key in (
        "acked_ts",
        "duplicate_of",
        "parent_issue_id",
        "custom_fields",
        "gerrit_cl_urls",
        "conversation_id",
        "chat_title",
        "chat_archived",
        "chat_busy",
        "chat_woken_ts",
        "chat_last_modified",
        "last_human_comment_author",
        "last_human_comment_at",
        "last_human_comment_snippet",
        "last_self_comment_at",
        "comment_cl_ids",
        "comment_cl_urls",
        "comments_checked_major_mod",
    ):
      # Respect empty comment fields when the caller freshly parsed comments (e.g. GitHub issues)
      if carry_key in comment_keys and carry_key in curr:
        continue
      if carry_key not in merged or merged.get(carry_key) in ("", None, [], {}):
        if carry_key in prev and prev[carry_key] not in ("", None):
          merged[carry_key] = prev[carry_key]
    # Keep non-default project pins on chatted bugs while letting default 'Skia' re-infer
    if (
        prev.get("conversation_id")
        and prev.get("project_name") not in ("", "Unassigned", "Skia", None)
        and merged.get("project_name") in ("", None)
    ):
      merged["project_id"] = str(prev.get("project_id") or "")
      merged["project_name"] = str(prev["project_name"])
  if not merged.get("title"):
    if merged.get("title_redacted"):
      merged["title"] = _format_redacted_fallback_title(merged)
    else:
      merged["title"] = f"Bug {merged.get('id') or merged.get('bug_key')}"
  return merged


def _extract_topic_from_bug_chat_title(chat_title: str) -> str:
  """Extracts human topic prose from `[HH:MM] ⦿ crbug/<id> <topic>` chat titles."""
  # Strip time prefixes, status/legacy «» markers, and compound bug+CL shortlink prefixes
  t = re.sub(
      r"^(?:«\s*)?(?:\[\d{1,2}:\d{2}\]\s*)?(?:[⦿⏳✓✔•◦«»]\s*)*(?:\[[^\]]*\]\s*)*",
      "",
      (chat_title or "").strip(),
  )
  t = re.sub(r"\s*»$", "", t).strip()
  t = re.sub(
      r"^(?:(?:crbug(?:\.com)?/|b/|cl/|crrev(?:\.com)?/c/|skrev(?:\.com)?/|"
      r"gerrit\s+|flutter#|pr\s*#|bug\s+|issue\s+#?|#)\d+\b[:\s—–-]*)+",
      "",
      t,
      flags=re.IGNORECASE,
  ).strip()
  if len(t) < 3 or _is_placeholder_bug_title(t):
    return ""
  if re.fullmatch(
      r"(?:skia|skparagraph|skoretext|webparagraph|chromium|flutter)\s+issue",
      t,
      re.IGNORECASE,
  ):
    return ""
  return t


def _resolve_redacted_bug_title(
    bug: dict[str, Any], all_bugs: dict[str, dict[str, Any]]
) -> None:
  """Derives a human-readable fallback title from local chat, CL, or duplicate metadata."""
  cur_title = str(bug.get("title") or "").strip()
  if not bug.get("title_redacted") and not _is_placeholder_bug_title(cur_title):
    return

  chat_topic = _extract_topic_from_bug_chat_title(str(bug.get("chat_title") or ""))
  if chat_topic:
    bug["title"] = chat_topic
    return

  linked = [
      c
      for c in (bug.get("linked_cls") or [])
      if isinstance(c, dict)
      and str(c.get("status") or "").upper() != "LINKED"
      and str(c.get("subject") or "").strip()
  ]
  linked.sort(key=lambda c: 0 if c.get("is_self") else 1)
  if linked:
    sub = linked[0]
    subj = str(sub.get("subject") or "").strip()
    lbl = str(sub.get("label") or "").strip()
    bug["title"] = f"{subj} (via {lbl})" if lbl and lbl not in subj else subj
    return

  dup_id = str(bug.get("duplicate_of") or "").strip()
  if dup_id:
    canon = all_bugs.get(f"buganizer:{dup_id}")
    canon_short = (
        str(canon.get("short_id") or "")
        if isinstance(canon, dict)
        else ""
    ) or f"{bug.get('tracker_flavor') or 'b'}/{dup_id}"
    canon_title = (
        str(canon.get("title") or "").strip() if isinstance(canon, dict) else ""
    )
    if (
        isinstance(canon, dict)
        and canon_title
        and not canon.get("title_redacted")
        and not _is_placeholder_bug_title(canon_title)
    ):
      bug["title"] = f"Duplicate of {canon_short}: {canon_title}"
    else:
      bug["title"] = f"Duplicate of {canon_short}"
    return

  if bug.get("title_redacted") or _is_placeholder_bug_title(cur_title):
    bug["title"] = _format_redacted_fallback_title(bug)


def call_buganizer_mcp_batch(
    calls: list[tuple[str, dict[str, Any]]], timeout_sec: float = 25.0
) -> list[str]:
  """Executes multiple Buganizer MCP tool calls over a single remote_mcp session."""
  if not calls or not os.path.isfile(REMOTE_MCP_BIN):
    return ["" for _ in calls]
  cmd = [
      REMOTE_MCP_BIN,
      "--use_loas_transact_dat=True",
      f"--mcp_server={BUGANIZER_MCP_SERVER}",
  ]
  proc: subprocess.Popen[str] | None = None
  outputs = ["" for _ in calls]
  try:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.stdin is None or proc.stdout is None:
      return outputs
    init_req = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "flight_deck", "version": "1.0"},
        },
    }
    proc.stdin.write(json.dumps(init_req) + "\n")
    proc.stdin.flush()
    proc.stdout.readline()

    for idx, (tool_name, args) in enumerate(calls):
      req = {
          "jsonrpc": "2.0",
          "id": idx + 2,
          "method": "tools/call",
          "params": {"name": tool_name, "arguments": args},
      }
      proc.stdin.write(json.dumps(req) + "\n")
      proc.stdin.flush()
      line = proc.stdout.readline()
      if line:
        outputs[idx] = _unwrap_mcp_or_json_text(line)
    proc.stdin.close()
    proc.wait(timeout=min(10.0, timeout_sec))
  except (OSError, subprocess.SubprocessError, ValueError):
    pass
  finally:
    if proc is not None and proc.poll() is None:
      try:
        proc.kill()
        proc.wait(timeout=3)
      except (OSError, subprocess.SubprocessError):
        pass
  return outputs


def fetch_buganizer_fallback(query: str, limit: int = 100) -> list[dict[str, Any]]:
  """Queries issues-cli readonly search when remote_mcp is unavailable."""
  if not os.path.isfile(ISSUES_CLI_BIN):
    return []
  try:
    res = subprocess.run(
        [
            ISSUES_CLI_BIN,
            "readonly",
            "search",
            "--query",
            query,
            "--limit",
            str(limit),
            "--verbose",
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if res.returncode == 0:
      return parse_buganizer_blocks(res.stdout)
  except (OSError, subprocess.SubprocessError):
    pass
  return []


def _run_gh_json(
    cmd: list[str],
    label: str,
    timeout_sec: int,
    errors_out: list[str] | None = None,
    ignore_not_found: bool = False,
) -> Any:
  """Runs a `gh` JSON command, logging stderr and recording failures on error."""
  try:
    res = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout_sec,
        check=False,
    )
  except (OSError, subprocess.SubprocessError) as exc:
    msg = f"GitHub {label}: {exc}"
    print(f"[cl_watcher] {msg}", file=sys.stderr, flush=True)
    if errors_out is not None:
      errors_out.append(msg)
    return None
  if res.returncode != 0:
    detail = " ".join((res.stderr or res.stdout or "").split())[:200]
    msg = (
        f"GitHub {label} exited {res.returncode}: {detail}"
        if detail
        else f"GitHub {label} exited {res.returncode}"
    )
    print(f"[cl_watcher] {msg}", file=sys.stderr, flush=True)
    # Deleted or non-issue numbers in PR text are expected misses, not broken syncs
    is_missing = ignore_not_found and (
        "could not resolve to an issue" in detail.lower()
        or "http 404" in detail.lower()
    )
    if errors_out is not None and not is_missing:
      errors_out.append(msg)
    return None
  try:
    return json.loads(res.stdout or "null")
  except ValueError as exc:
    msg = f"GitHub {label} invalid JSON: {exc}"
    print(f"[cl_watcher] {msg}", file=sys.stderr, flush=True)
    if errors_out is not None:
      errors_out.append(msg)
    return None


# Query tail timeline items directly so long multi-year issues do not paginate from comment 1
GITHUB_ISSUE_TIMELINE_GQL = """
query($owner:String!, $name:String!, $num:Int!) {
  repository(owner:$owner, name:$name) {
    issue(number:$num) {
      number title url state createdAt updatedAt lastEditedAt
      editor { login }
      author { login }
      body
      assignees(first:20) { nodes { login } }
      timelineItems(
        last:50,
        itemTypes:[ISSUE_COMMENT, ASSIGNED_EVENT, REOPENED_EVENT]
      ) {
        nodes {
          __typename
          ... on IssueComment { author { login } body createdAt updatedAt }
          ... on AssignedEvent {
            actor { login }
            assignee { ... on User { login } }
            createdAt
          }
          ... on ReopenedEvent { actor { login } createdAt }
        }
      }
    }
  }
}
"""


# Collapse single-line non-quoted snippet from GitHub issue or comment markdown
def _clean_github_issue_snippet(text: str, max_len: int = 180) -> str:
  if not text:
    return ""
  cleaned = re.sub(r"<!--.*?-->", " ", text, flags=re.S)
  non_quoted = [
      ln for ln in cleaned.splitlines()
      if ln.strip() and not ln.lstrip().startswith(">")
  ]
  src = " ".join(non_quoted) if non_quoted else cleaned
  collapsed = " ".join(src.split())
  return collapsed[:max_len]


# Evaluate GraphQL issue timeline and suppress non-assigned mentions when Rusino replied last
def _evaluate_github_mention_issue(
    repo_slug: str, issue_num: str, self_gh: set[str]
) -> dict[str, Any] | None:
  if not repo_slug or "/" not in repo_slug or not str(issue_num).isdigit():
    return None
  owner, name = repo_slug.split("/", 1)
  try:
    out = subprocess.check_output(
        [
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={GITHUB_ISSUE_TIMELINE_GQL}",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-F",
            f"num={int(issue_num)}",
        ],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=12,
    )
    payload = json.loads(out)
  except (OSError, subprocess.SubprocessError, ValueError):
    return None

  issue = (((payload or {}).get("data") or {}).get("repository") or {}).get("issue")
  if not isinstance(issue, dict):
    return None
  state_str = str(issue.get("state") or "OPEN").upper()
  if state_str == "CLOSED":
    return None

  assignees = [
      str(n.get("login") or "")
      for n in ((issue.get("assignees") or {}).get("nodes") or [])
      if isinstance(n, dict) and n.get("login")
  ]
  issue_author = str(((issue.get("author") or {}).get("login") or "ghost")).strip()
  created_raw = str(issue.get("createdAt") or "")
  created_ts = _parse_ts(created_raw)
  editor = str(((issue.get("editor") or {}).get("login") or "")).strip()
  edited_raw = str(issue.get("lastEditedAt") or "")
  edited_ts = _parse_ts(edited_raw) if edited_raw else 0.0

  my_last_ts = 0.0
  my_last_iso = ""
  my_last_idx = -1
  if issue_author.lower() in self_gh and created_ts > 0.0:
    my_last_ts = created_ts
    my_last_iso = created_raw
  my_edit_ts = edited_ts if (editor.lower() in self_gh and edited_ts > 0.0) else 0.0
  if my_edit_ts > my_last_ts:
    my_last_ts = my_edit_ts
    my_last_iso = edited_raw

  nodes = [
      n
      for n in ((issue.get("timelineItems") or {}).get("nodes") or [])
      if isinstance(n, dict)
  ]
  comments: list[tuple[int, str, str, str, str]] = []
  events: list[tuple[float, str, str, str]] = []
  for idx, node in enumerate(nodes):
    kind = str(node.get("__typename") or "")
    if kind == "IssueComment":
      login = str(((node.get("author") or {}).get("login") or "ghost")).strip()
      c_at = str(node.get("createdAt") or "")
      u_at = str(node.get("updatedAt") or c_at)
      body = str(node.get("body") or "")
      if login.lower() in self_gh:
        my_last_idx = idx
        c_ts = _parse_ts(c_at)
        if c_ts >= my_last_ts:
          my_last_ts = c_ts
          my_last_iso = c_at
        continue
      if _is_bug_bot_author(login):
        continue
      comments.append((idx, login, body, c_at, u_at))
    elif kind in ("AssignedEvent", "ReopenedEvent"):
      actor = str(((node.get("actor") or {}).get("login") or "")).strip()
      ev_at = str(node.get("createdAt") or "")
      ev_ts = _parse_ts(ev_at)
      if not actor or actor.lower() in self_gh or _is_bug_bot_author(actor):
        continue
      if my_last_ts > 0.0 and ev_ts <= my_last_ts:
        continue
      if kind == "AssignedEvent":
        tgt = str(((node.get("assignee") or {}).get("login") or "")).strip()
        if tgt.lower() in self_gh:
          events.append((ev_ts, ev_at, actor, f"Assigned by {actor}"))
      else:
        events.append((ev_ts, ev_at, actor, f"Reopened by {actor}"))

  mention_re = re.compile(r"(?<![\w-])@rusino\b", re.I)
  newer_human: list[tuple[float, int, str, str, str]] = []
  for idx, login, body, c_at, u_at in comments:
    c_ts = _parse_ts(c_at)
    u_ts = _parse_ts(u_at) if u_at else c_ts
    if idx > my_last_idx and (my_edit_ts <= 0.0 or c_ts > my_edit_ts):
      newer_human.append((c_ts, idx, c_at, login, _clean_github_issue_snippet(body)))
    elif my_last_ts > 0.0 and u_ts > my_last_ts and mention_re.search(body):
      newer_human.append((u_ts, idx, u_at, login, _clean_github_issue_snippet(body)))

  body_text = str(issue.get("body") or "")
  if (
      edited_ts > 0.0
      and editor
      and editor.lower() not in self_gh
      and not _is_bug_bot_author(editor)
      and (my_last_ts <= 0.0 or (edited_ts > my_last_ts and mention_re.search(body_text)))
  ):
    events.append((edited_ts, edited_raw, editor, _clean_github_issue_snippet(body_text)))
  if (
      my_last_ts <= 0.0
      and issue_author
      and issue_author.lower() not in self_gh
      and not _is_bug_bot_author(issue_author)
  ):
    events.append((created_ts, created_raw, issue_author, _clean_github_issue_snippet(body_text)))

  best_comment = max(newer_human, key=lambda x: (x[0], x[1])) if newer_human else None
  events.sort(key=lambda x: x[0])
  best_event = events[-1] if events else None
  if best_comment is None and best_event is None:
    return None

  if best_comment and best_event and (best_event[0] - best_comment[0]) > 300.0:
    _, act_iso, act_author, act_snip = best_event
  elif best_comment:
    _, _, act_iso, act_author, act_snip = best_comment
  else:
    assert best_event is not None
    _, act_iso, act_author, act_snip = best_event

  num_s = str(issue.get("number") or issue_num)
  mod_raw = str(issue.get("updatedAt") or act_iso or created_raw)
  mod_et, mod_age = format_et_and_age(mod_raw)
  short_id = (
      f"flutter#{num_s}"
      if repo_slug.lower() == "flutter/flutter"
      else f"{repo_slug}#{num_s}"
  )
  return {
      "bug_key": f"gh_issue:{repo_slug}#{num_s}",
      "source": "github",
      "id": num_s,
      "tracker_flavor": "github",
      "short_id": short_id,
      "url": str(issue.get("url") or f"https://github.com/{repo_slug}/issues/{num_s}"),
      "title": str(issue.get("title") or f"{short_id}"),
      "status": state_str,
      "state_reason": "",
      "priority": "P2",
      "severity": "",
      "issue_type": "BUG",
      "assignee": ", ".join(assignees),
      "reporter": issue_author,
      "labels": [],
      "created_time": created_raw,
      "modified_time": mod_raw,
      "modified_et": mod_et,
      "modified_age": mod_age,
      "modified_ts": _parse_ts(mod_raw),
      "slo": {},
      "due_date": "",
      "gerrit_cl_ids": [],
      "from_self_query": False,
      "is_mentioned": True,
      "last_human_comment_author": act_author,
      "last_human_comment_at": act_iso,
      "last_human_comment_snippet": act_snip,
      "last_self_comment_at": my_last_iso,
  }


def fetch_github_bugs(
    extra_issue_nums: set[str] | None = None,
    errors_out: list[str] | None = None,
) -> list[dict[str, Any]]:
  """Fetches assigned GitHub issues and unanswered @Rusino issue mentions across repos."""
  closed_since = (
      datetime.now(timezone.utc) - timedelta(days=BUG_RECENT_CLOSED_DAYS)
  ).strftime("%Y-%m-%d")
  # Omit stateReason from --json because Debian gh 2.46 rejects that field on Linux
  fields = (
      "number,title,state,author,assignees,labels,comments,"
      "url,createdAt,updatedAt,closedAt,milestone"
  )
  raw_by_num: dict[str, dict[str, Any]] = {}
  self_search_nums: set[str] = set()
  searches = (
      ("is:open assignee:Rusino", "100"),
      (f"is:closed assignee:Rusino closed:>={closed_since}", "50"),
  )
  for sq, lim in searches:
    items = _run_gh_json(
        [
            "gh",
            "issue",
            "list",
            "--repo",
            "flutter/flutter",
            "--search",
            sq,
            "--limit",
            lim,
            "--json",
            fields,
        ],
        f"issue list ({sq})",
        12,
        errors_out=errors_out,
    )
    for item in items or []:
      if isinstance(item, dict) and item.get("number"):
        num_s = str(item["number"])
        raw_by_num[num_s] = item
        self_search_nums.add(num_s)

  for num in sorted(extra_issue_nums or set()):
    if not num or num in raw_by_num:
      continue
    item = _run_gh_json(
        [
            "gh",
            "issue",
            "view",
            str(num),
            "--repo",
            "flutter/flutter",
            "--json",
            fields,
        ],
        f"issue view #{num}",
        10,
        errors_out=errors_out,
        ignore_not_found=True,
    )
    if isinstance(item, dict) and item.get("number"):
      raw_by_num[str(item["number"])] = item

  # Enrich closed issues with REST state_reason so NOT_PLANNED routes to Closed vs Fixed
  closed_nums = [
      num
      for num, d in raw_by_num.items()
      if str(d.get("state") or "").upper() == "CLOSED"
      and not d.get("stateReason")
      and not d.get("state_reason")
  ]
  if any(num in self_search_nums for num in closed_nums):
    rest_closed = _run_gh_json(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            (
                "q=repo:flutter/flutter is:issue is:closed assignee:Rusino "
                f"closed:>={closed_since}"
            ),
            "-f",
            "per_page=50",
        ],
        "closed issue state_reason search",
        10,
        errors_out=errors_out,
    )
    if isinstance(rest_closed, dict):
      for r_it in rest_closed.get("items") or []:
        if isinstance(r_it, dict) and r_it.get("number"):
          r_num = str(r_it["number"])
          if r_num in raw_by_num and r_it.get("state_reason"):
            raw_by_num[r_num]["state_reason"] = r_it["state_reason"]
  # Fall back to per-issue REST lookup for extra PR-referenced closed issues
  for c_num in closed_nums:
    if (
        raw_by_num[c_num].get("state_reason")
        or raw_by_num[c_num].get("stateReason")
    ):
      continue
    r_issue = _run_gh_json(
        ["gh", "api", f"repos/flutter/flutter/issues/{c_num}"],
        f"issue #{c_num} state_reason",
        10,
        errors_out=errors_out,
        ignore_not_found=True,
    )
    if isinstance(r_issue, dict) and r_issue.get("state_reason"):
      raw_by_num[c_num]["state_reason"] = r_issue["state_reason"]

  bugs: list[dict[str, Any]] = []
  seen_bkeys: set[str] = set()
  self_gh = {u.lower() for u in SELF_GITHUB_LOGINS}
  for num, d in raw_by_num.items():
    labels = [
        str(lbl.get("name") or "")
        for lbl in (d.get("labels") or [])
        if isinstance(lbl, dict) and lbl.get("name")
    ]
    pri = next(
        (
            lbl.upper()
            for lbl in labels
            if lbl.upper() in ("P0", "P1", "P2", "P3", "P4")
        ),
        "P2",
    )
    last_human_author = ""
    last_human_at = ""
    last_human_snip = ""
    last_self_at = ""
    for c in d.get("comments") or []:
      if not isinstance(c, dict):
        continue
      c_login = str((c.get("author") or {}).get("login") or "").strip()
      c_at = str(c.get("createdAt") or "").strip()
      c_body = str(c.get("body") or "").strip()
      # Keep self replies separate on GitHub issues to match Buganizer comment tracking
      if c_login.lower() in self_gh:
        last_self_at = c_at
        continue
      if _is_bug_bot_author(c_login):
        continue
      last_human_author = c_login
      last_human_at = c_at
      last_human_snip = " ".join(c_body.split())[:180]

    mod_raw = str(d.get("updatedAt") or d.get("createdAt") or "")
    mod_et, mod_age = format_et_and_age(mod_raw)
    assignees = [
        str(a.get("login") or "")
        for a in (d.get("assignees") or [])
        if isinstance(a, dict) and a.get("login")
    ]
    bkey = f"gh_issue:flutter/flutter#{num}"
    seen_bkeys.add(bkey)
    bugs.append({
        "bug_key": bkey,
        "source": "github",
        "id": num,
        "tracker_flavor": "github",
        "short_id": f"flutter#{num}",
        "url": str(d.get("url") or f"https://github.com/flutter/flutter/issues/{num}"),
        "title": str(d.get("title") or f"Flutter issue #{num}"),
        "status": str(d.get("state") or "OPEN").upper(),
        "state_reason": str(
            d.get("stateReason") or d.get("state_reason") or ""
        ).upper(),
        "priority": pri,
        "severity": "",
        "issue_type": "BUG",
        "assignee": ", ".join(assignees),
        "reporter": str((d.get("author") or {}).get("login") or ""),
        "labels": labels,
        "created_time": str(d.get("createdAt") or ""),
        "modified_time": mod_raw,
        "modified_et": mod_et,
        "modified_age": mod_age,
        "modified_ts": _parse_ts(mod_raw),
        "slo": {},
        "due_date": "",
        "gerrit_cl_ids": [],
        "from_self_query": num in self_search_nums,
        "last_human_comment_author": last_human_author,
        "last_human_comment_at": last_human_at,
        "last_human_comment_snippet": last_human_snip,
        "last_self_comment_at": last_self_at,
    })

  # Discover open non-assigned GitHub issues involving or notifying @Rusino across all repos
  mention_candidates: list[tuple[str, str]] = []
  seen_cand: set[tuple[str, str]] = set()
  try:
    out_inv = subprocess.check_output(
        [
            "gh",
            "api",
            "-X",
            "GET",
            "search/issues",
            "-f",
            "q=is:open is:issue involves:Rusino -assignee:Rusino",
            "-f",
            "sort=updated",
            "-f",
            "per_page=30",
        ],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=12,
    )
    for it in (json.loads(out_inv) or {}).get("items") or []:
      # Ignore PRs returned by GitHub's unified issues/PR search endpoint
      if not isinstance(it, dict) or it.get("pull_request"):
        continue
      m_repo = re.search(
          r"github\.com/([^/]+/[^/]+)/issues/(\d+)", str(it.get("html_url") or "")
      )
      if m_repo:
        pair = (m_repo.group(1), m_repo.group(2))
        # Deduplicate against already-loaded assigned issues so each issue is fetched once
        if pair not in seen_cand and f"gh_issue:{pair[0]}#{pair[1]}" not in seen_bkeys:
          seen_cand.add(pair)
          mention_candidates.append(pair)
  except (OSError, subprocess.SubprocessError, ValueError):
    pass

  # Supplement search with notifications API to catch cross-repo mentions before search indexing
  try:
    out_notif = subprocess.check_output(
        ["gh", "api", "notifications?all=true&per_page=30"],
        stderr=subprocess.DEVNULL,
        text=True,
        timeout=10,
    )
    for n in json.loads(out_notif) or []:
      if not isinstance(n, dict):
        continue
      subj_obj = n.get("subject") or {}
      if str(subj_obj.get("type") or "") != "Issue":
        continue
      m_api = re.search(
          r"/repos/([^/]+/[^/]+)/issues/(\d+)$", str(subj_obj.get("url") or "")
      )
      if m_api:
        pair = (m_api.group(1), m_api.group(2))
        if pair not in seen_cand and f"gh_issue:{pair[0]}#{pair[1]}" not in seen_bkeys:
          seen_cand.add(pair)
          mention_candidates.append(pair)
  except (OSError, subprocess.SubprocessError, ValueError):
    pass

  for repo_slug, m_num in mention_candidates[:20]:
    evaluated = _evaluate_github_mention_issue(repo_slug, m_num, self_gh)
    if evaluated is not None:
      bugs.append(evaluated)
  return bugs


def infer_project_for_bug(
    bug: dict[str, Any], proj_names: dict[str, str]
) -> tuple[str, str]:
  """Maps an unchatted bug to the matching Jetski project `(project_id, project_name)`."""
  by_name = {name.lower(): (pid, name) for pid, name in proj_names.items()}

  def pick(*candidates: str) -> tuple[str, str]:
    for cand in candidates:
      hit = by_name.get(cand.lower())
      if hit:
        return hit
    return ("", candidates[0] if candidates else "Skia")

  comp_id = str(bug.get("component_id") or "").strip()
  blob = " ".join([
      str(bug.get("title") or ""),
      str(bug.get("component_tags") or ""),
      str(bug.get("reporter") or ""),
      " ".join(str(x) for x in (bug.get("labels") or ())),
  ]).lower()
  # Route internal Jetski (component 2271566) and OneQuota bugs out of Skia
  if comp_id == "2271566" or any(k in blob for k in ("jetski", "onequota")):
    return pick("Improving Experience")
  if any(k in blob for k in ("skoretext", "coretext", "ctfont", "_mac_ct")):
    return pick("SkoreText", "Skia")
  if "webparagraph" in blob or (
      bug.get("source") == "github" and ("platform-web" in blob or "[web]" in blob)
  ):
    return pick("WebParagraph")
  if any(k in blob for k in ("textcluster", "textmetrics", "shapedtext", "canvas2d")):
    return pick("TextClusters API", "Chromium", "Skia")
  if any(k in blob for k in ("skparagraph", "skunicode", "skshaper", "paragraph")):
    return pick("SkParagraph", "Skia")
  if bug.get("source") == "github":
    return pick("SkParagraph", "WebParagraph")
  return pick("Skia")


def _summarize_cl_for_bug_subtask(rec: dict[str, Any]) -> str:
  """Builds a compact 1-line status summary for a CL subtask row under a bug."""
  st = str(rec.get("status") or "").upper()
  if st == "MERGED":
    ps = rec.get("post_submit") or {}
    if ps.get("all_passed"):
      return "Merged · Shipped to 4/4"
    return "Merged"
  if st in ("ABANDONED", "CLOSED"):
    return st.capitalize()
  if rec.get("merge_conflict"):
    return "Conflict"
  if rec.get("is_draft") or rec.get("is_wip"):
    return str(rec.get("wip_label") or "WIP")
  parts = [
      str(rec.get("cr_summary") or "").strip(),
      str(rec.get("cq_status") or "").strip(),
      str(rec.get("reviewer_status") or "").strip(),
  ]
  kept = [p for p in parts if p and p != "Idle"]
  return " · ".join(kept[:2]) if kept else "In review"


def link_bugs_and_cls(
    bugs_by_key: dict[str, dict[str, Any]],
    items_by_key: dict[str, Any],
    bug_chats: dict[str, dict[str, Any]] | None = None,
) -> None:
  """Cross-links bugs and CLs/PRs and unifies 1:1 shared bug/CL conversations."""
  if bug_chats:
    for bkey, bchat in bug_chats.items():
      bug = bugs_by_key.get(bkey)
      if isinstance(bug, dict) and bchat.get("conversation_id"):
        # Refresh matching chat metadata so moved or newly archived bug chats update project
        if (
            not bug.get("conversation_id")
            or bug.get("chat_archived")
            or bug.get("conversation_id") == bchat["conversation_id"]
        ):
          bug["conversation_id"] = bchat["conversation_id"]
          bug["chat_title"] = bchat.get("title", "")
          bug["chat_archived"] = bool(bchat.get("chat_archived"))
          bug["chat_busy"] = bool(bchat.get("chat_busy"))
          bug["chat_last_modified"] = bchat.get("chat_last_modified", "")
          if bchat.get("project_name") not in META_PROJECT_NAMES:
            bug["project_id"] = bchat.get("project_id", "")
            bug["project_name"] = bchat.get("project_name", bug.get("project_name", ""))

  # Group non-companion CL state entries by canonical item_key
  cl_entries_by_ikey: dict[str, list[tuple[str, dict[str, Any]]]] = {}
  for dkey, rec in list(items_by_key.items()):
    if not isinstance(rec, dict):
      continue
    if rec.get("is_companion") or rec.get("companion_to"):
      continue
    ikey = str(rec.get("item_key") or "").strip()
    if not ikey and ":" in dkey:
      ikey = dkey.split("@", 1)[0]
    if ikey:
      cl_entries_by_ikey.setdefault(ikey, []).append((dkey, rec))

  bug_to_cls: dict[str, list[str]] = {bk: [] for bk in bugs_by_key}
  cl_to_bugs: dict[str, list[str]] = {ik: [] for ik in cl_entries_by_ikey}

  def _add_edge(bkey: str, ikey: str) -> None:
    if bkey in bug_to_cls and ikey not in bug_to_cls[bkey]:
      bug_to_cls[bkey].append(ikey)
    if ikey in cl_to_bugs and bkey not in cl_to_bugs[ikey]:
      cl_to_bugs[ikey].append(bkey)

  # Collect links from Buganizer Gerrit changes and gitwatcher comment URLs
  for bkey, bug in bugs_by_key.items():
    if not isinstance(bug, dict):
      continue
    for gid in [
        *(bug.get("gerrit_cl_ids") or []),
        *(bug.get("comment_cl_ids") or []),
    ]:
      ikey = f"gerrit:{gid}"
      _add_edge(bkey, ikey)

  # Collect links from CL/PR commit messages, footers, and closingIssuesReferences
  for ikey, pairs in cl_entries_by_ikey.items():
    sample = pairs[0][1]
    kind = str(sample.get("kind") or ("github" if ikey.startswith("github:") else "gerrit"))
    default_trk = "github" if kind == "github" else "buganizer"
    blob = "\n".join([
        str(sample.get("subject") or ""),
        str(sample.get("commit_message") or ""),
        str(sample.get("topic") or ""),
        *(str(h) for h in (sample.get("hashtags") or ())),
    ])
    refs = list(
        dict.fromkeys([
            *(str(k) for k in (sample.get("linked_bug_keys") or []) if k),
            *extract_bug_refs_from_text(
                blob,
                default_tracker=default_trk,
                default_repo=str(sample.get("host") or ""),
            ),
        ])
    )
    for bkey in refs:
      _add_edge(bkey, ikey)

  # Pass 1: Propagate an active bug chat onto unchatted 1:1 self-authored fix CLs
  for bkey, cl_ikeys in bug_to_cls.items():
    bug = bugs_by_key.get(bkey)
    if not isinstance(bug, dict):
      continue
    bcid = str(bug.get("conversation_id") or "").strip()
    if not bcid or bug.get("chat_archived"):
      continue
    self_ikeys = [
        ik
        for ik in cl_ikeys
        if ik in cl_entries_by_ikey
        and cl_entries_by_ikey[ik][0][1].get("is_self")
        and len(cl_to_bugs.get(ik, [])) <= 1
    ]
    for ik in self_ikeys:
      for dkey, rec in list(cl_entries_by_ikey[ik]):
        if not str(rec.get("conversation_id") or "").strip():
          _rekey_state_item_chat(
              items_by_key, ik, dkey, rec, bcid, chat_archived=False
          )
          rec["single_target"] = len(self_ikeys) == 1
          rec["chat_busy"] = bool(bug.get("chat_busy"))
          cl_entries_by_ikey[ik] = [(f"{ik}@{bcid}", rec)]

  # Pass 2: Propagate an active 1:1 self-authored CL chat onto its unchatted parent bug;
  # multi-bug umbrella CLs still supply project_name without claiming the bug's conversation
  for bkey, cl_ikeys in bug_to_cls.items():
    bug = bugs_by_key.get(bkey)
    if not isinstance(bug, dict):
      continue
    # Still recover project_name from a shared CL chat when merge_bug_record cleared 'Skia'
    has_active_chat = bool(
        str(bug.get("conversation_id") or "").strip() and not bug.get("chat_archived")
    )
    if has_active_chat and bug.get("project_name") not in ("", "Unassigned", None):
      continue
    self_chatted: list[tuple[str, dict[str, Any]]] = []
    for ik in cl_ikeys:
      for _, rec in cl_entries_by_ikey.get(ik, []):
        if (
            rec.get("is_self")
            and rec.get("conversation_id")
            and not rec.get("chat_archived")
        ):
          self_chatted.append((ik, rec))
    if self_chatted:
      chosen_ik, chosen = self_chatted[0]
      if not has_active_chat and len(cl_to_bugs.get(chosen_ik, [])) <= 1:
        bug["conversation_id"] = str(chosen["conversation_id"])
        bug["chat_archived"] = False
        bug["chat_busy"] = any(bool(r.get("chat_busy")) for _, r in self_chatted)
      if (
          bug.get("project_name") in ("", "Unassigned", None)
          and chosen.get("project_name")
          and chosen.get("project_name") not in META_PROJECT_NAMES
      ):
        bug["project_id"] = str(chosen.get("project_id") or "")
        bug["project_name"] = str(chosen.get("project_name") or "")

  # Populate bidirectional display lists on bugs and CL items
  for bkey, bug in bugs_by_key.items():
    if not isinstance(bug, dict):
      continue
    bcid = str(bug.get("conversation_id") or "").strip()
    subtasks: list[dict[str, Any]] = []
    for ik in bug_to_cls.get(bkey, []):
      pairs = cl_entries_by_ikey.get(ik)
      if pairs:
        rec = pairs[0][1]
        tid = str(rec.get("target_id") or ik.split(":", 1)[-1])
        kind = str(rec.get("kind") or "gerrit")
        lbl = (
            f"PR {tid}"
            if kind == "github"
            else (f"cl/{tid}" if kind == "piper" else f"CL {tid}")
        )
        scid = str(rec.get("conversation_id") or "").strip()
        subtasks.append({
            "item_key": ik,
            "kind": kind,
            "target_id": tid,
            "label": lbl,
            "subject": str(rec.get("subject") or lbl),
            "url": str(rec.get("url") or ""),
            "status": str(rec.get("status") or "NEW").upper(),
            "branch": str(rec.get("branch") or ""),
            "is_cherry_pick": bool(rec.get("is_cherry_pick")),
            "is_self": bool(rec.get("is_self")),
            "is_wip": bool(rec.get("is_wip") or rec.get("is_draft")),
            "shared_1_to_1": len(cl_to_bugs.get(ik, [])) <= 1,
            "in_needs_you": bool(
                rec.get("in_my_attention") 
                or (rec.get("merge_conflict") and rec.get("is_self"))
                or bool(rec.get("sponsor_action"))
            ),
            "state_summary": _summarize_cl_for_bug_subtask(rec),
            "submitted_ts": float(rec.get("submitted_ts") or 0.0),
            "activity_ts": float(rec.get("activity_ts") or 0.0),
            "conversation_id": scid,
            "chat_archived": bool(rec.get("chat_archived")),
            "chat_busy": bool(rec.get("chat_busy")),
            "shared_chat": bool(bcid and scid and bcid == scid),
        })
      elif ik.startswith("gerrit:"):
        gid = ik.split(":", 1)[1]
        known_urls = {
            **(bug.get("comment_cl_urls") or {}),
            **(bug.get("gerrit_cl_urls") or {}),
        }
        g_url = str(known_urls.get(gid) or f"https://{GERRIT_SKIA_HOST}/c/skia/+/{gid}")
        subtasks.append({
            "item_key": ik,
            "kind": "gerrit",
            "target_id": gid,
            "label": f"CL {gid}",
            "subject": f"Gerrit {gid}",
            "url": g_url,
            "status": "LINKED",
            "is_self": False,
            "shared_1_to_1": False,
            "in_needs_you": False,
            "state_summary": "Linked in Buganizer",
            "submitted_ts": 0.0,
            "activity_ts": 0.0,
            "conversation_id": "",
            "chat_archived": False,
            "chat_busy": False,
            "shared_chat": False,
        })
    bug["linked_cls"] = subtasks

  for bug in bugs_by_key.values():
    if isinstance(bug, dict):
      _resolve_redacted_bug_title(bug, bugs_by_key)

  for ikey, pairs in cl_entries_by_ikey.items():
    b_badges: list[dict[str, Any]] = []
    for bkey in cl_to_bugs.get(ikey, []):
      bug = bugs_by_key.get(bkey)
      if isinstance(bug, dict):
        b_badges.append({
            "bug_key": bkey,
            "short_id": str(bug.get("short_id") or bkey),
            "title": str(bug.get("title") or ""),
            "priority": str(bug.get("priority") or ""),
            "severity": str(bug.get("severity") or ""),
            "status": str(bug.get("status") or ""),
            "url": str(bug.get("url") or ""),
        })
      elif bkey.startswith("buganizer:"):
        bid = bkey.split(":", 1)[1]
        b_badges.append({
            "bug_key": bkey,
            "short_id": f"b/{bid}",
            "title": "",
            "priority": "",
            "severity": "",
            "status": "",
            "url": f"https://b.corp.google.com/issues/{bid}",
        })
      elif bkey.startswith("gh_issue:flutter/flutter#"):
        gnum = bkey.split("#", 1)[1]
        b_badges.append({
            "bug_key": bkey,
            "short_id": f"flutter#{gnum}",
            "title": "",
            "priority": "",
            "severity": "",
            "status": "",
            "url": f"https://github.com/flutter/flutter/issues/{gnum}",
        })
    for _, rec in pairs:
      rec["linked_bugs"] = b_badges


def _is_self_bug_assignee(assignee: str) -> bool:
  """Returns True when `assignee` includes one of the user's Buganizer or GitHub identities."""
  self_lower = {e.lower() for e in SELF_BUG_EMAILS} | {
      u.lower() for u in SELF_GITHUB_LOGINS
  }
  tokens = [
      t.strip().lower() for t in re.split(r"[,\s]+", assignee or "") if t.strip()
  ]
  return any(t in self_lower for t in tokens)


def is_security_merge_request(bug: dict[str, Any]) -> bool:
  """Checks all 7 release-blintz predicates for an active auto-cherry-pick merge request."""
  if str(bug.get("source") or "buganizer") != "buganizer":
    return False
  status = str(bug.get("status") or "").upper()
  if status not in ("NEW", "ASSIGNED", "ACCEPTED", "OPEN"):
    return False
  if str(bug.get("reporter") or "").strip().lower() != SECURITY_MERGE_REPORTER:
    return False
  if extract_merge_milestone(str(bug.get("title") or "")) is None:
    return False
  if not str(bug.get("parent_issue_id") or "").strip():
    return False
  if str(bug.get("component_id") or "").strip() != SECURITY_MERGE_COMPONENT_ID:
    return False
  if bool(bug.get("is_collaborator")) or not _is_self_bug_assignee(
      str(bug.get("assignee") or "")
  ):
    return False
  cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
  if str(cf.get(CF_BUG_MERGE) or "").strip().lower() != "pending":
    return False
  if str(cf.get(CF_BUG_AUTO_CHERRY_PICK) or "").strip().lower() != "yes":
    return False
  return True


def _parse_gerrit_host_and_cl_urls(raw_text: str) -> list[tuple[str, str, str]]:
  """Returns deduplicated `(host, cl_id, normalized_url)` tuples from Gerrit URL text."""
  seen: set[tuple[str, str]] = set()
  out: list[tuple[str, str, str]] = []
  for m in RE_GERRIT_ANY_URL_ID.finditer(raw_text or ""):
    norm_url = _normalize_public_gerrit_url(m.group(0))
    parsed = urllib.parse.urlparse(norm_url)
    host = parsed.netloc.lower() or GERRIT_SKIA_HOST
    cid = m.group(1)
    if (host, cid) not in seen:
      seen.add((host, cid))
      out.append((host, cid, norm_url))
  return out


def _refresh_skia_merge_refs(skia_dir: str, milestones: list[int]) -> None:
  """Fetches origin/main and requested chrome/m<NN> milestone refs in local Skia."""
  if not skia_dir or not os.path.isdir(skia_dir):
    return
  unique_ms = sorted(set(milestones))
  refspecs = ["refs/heads/main:refs/remotes/origin/main"] + [
      f"refs/heads/chrome/m{ms}:refs/remotes/origin/chrome/m{ms}"
      for ms in unique_ms
  ]
  res = subprocess.run(
      ["git", "-C", skia_dir, "fetch", "origin", *refspecs, "--quiet"],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      timeout=25,
      check=False,
  )
  # Retry per-ref when a newly requested milestone branch does not exist on origin yet
  if res.returncode != 0:
    for spec in refspecs:
      subprocess.run(
          ["git", "-C", skia_dir, "fetch", "origin", spec, "--quiet"],
          stdout=subprocess.DEVNULL,
          stderr=subprocess.DEVNULL,
          timeout=12,
          check=False,
      )


def _fetch_merge_cl_detail(host: str, cl_id: str) -> dict[str, Any]:
  """Fetches Gerrit status, merged commit SHA, subject, and touched file paths for a CL."""
  try:
    data = _gerrit_http_get_json(
        host,
        f"/changes/{cl_id}?o=CURRENT_REVISION&o=CURRENT_COMMIT&o=CURRENT_FILES",
        timeout=10,
    )
    if not isinstance(data, dict):
      return {"ok": False, "cl_id": str(cl_id)}
    project = str(data.get("project") or "")
    if project == "skia":
      repo = "Skia"
    elif project == "chromium/src":
      repo = "Chromium"
    else:
      repo = project.rsplit("/", 1)[-1].capitalize() or "Skia"
    cur_rev = str(data.get("current_revision") or "")
    rev_obj = (data.get("revisions") or {}).get(cur_rev) or {}
    files_obj = rev_obj.get("files") if isinstance(rev_obj, dict) else {}
    files = sorted(
        str(p)
        for p in (files_obj or {}).keys()
        if str(p) and str(p) != "/COMMIT_MSG"
    )
    commit_obj = rev_obj.get("commit") if isinstance(rev_obj, dict) else {}
    subject = str(
        (commit_obj or {}).get("subject") or data.get("subject") or ""
    ).strip()
    url = (
        f"https://{host}/c/{project}/+/{cl_id}"
        if project
        else f"https://{host}/c/{cl_id}"
    )
    return {
        "ok": True,
        "cl_id": str(cl_id),
        "url": url,
        "host": host,
        "project": project,
        "branch": str(data.get("branch") or ""),
        "repo": repo,
        "status": str(data.get("status") or "").upper(),
        "sha": cur_rev,
        "short_sha": cur_rev[:10],
        "subject": subject,
        "submitted": str(data.get("submitted") or data.get("updated") or ""),
        "files": files,
    }
  except (OSError, urllib.error.URLError, ValueError, KeyError):
    return {"ok": False, "cl_id": str(cl_id)}


def _is_git_ancestor(repo_dir: str, anc_sha: str, desc_ref: str) -> bool:
  """Returns True when `anc_sha` is an ancestor of `desc_ref` in `repo_dir`."""
  if not repo_dir or not os.path.isdir(repo_dir) or not anc_sha or not desc_ref:
    return False
  try:
    res = subprocess.run(
        ["git", "-C", repo_dir, "merge-base", "--is-ancestor", anc_sha, desc_ref],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )
    return res.returncode == 0
  except (OSError, subprocess.SubprocessError):
    return False


def _parse_chrome_version_tuple(ver: str) -> tuple[int, ...]:
  parts = [p for p in (ver or "").strip().split(".") if p.isdigit()]
  return tuple(int(p) for p in parts) if len(parts) >= 3 else ()


def _http_get_json_direct(url: str, timeout: int = 8) -> Any:
  req = urllib.request.Request(url, headers={"User-Agent": "CLFlightDeck/1.0"})
  with urllib.request.urlopen(req, timeout=timeout) as resp:
    return json.loads(resp.read().decode("utf-8", errors="replace"))


def _fetch_skia_roll_and_canary(
    skia_dir: str, fix_shas: list[str], latest_submitted: str
) -> dict[str, Any]:
  """Finds the first Chromium Roll Skia commit containing `fix_shas` and its Canary soak start."""
  base_ts = _parse_ts(latest_submitted) or (time.time() - 7 * 86400.0)
  after_d = datetime.fromtimestamp(base_ts - 86400.0, tz=timezone.utc).strftime(
      "%Y-%m-%d"
  )
  before_d = datetime.fromtimestamp(
      base_ts + 14 * 86400.0, tz=timezone.utc
  ).strftime("%Y-%m-%d")
  q = (
      "/changes/?q=project:chromium/src+status:merged+%22Roll+Skia+from%22"
      f"+after:{after_d}+before:{before_d}&n=90&o=CURRENT_REVISION&o=CURRENT_COMMIT"
  )
  try:
    raw_changes = _gerrit_http_get_json(GERRIT_CHROMIUM_HOST, q, timeout=12)
  except (OSError, urllib.error.URLError, ValueError):
    return {"rolled": False}
  if not isinstance(raw_changes, list):
    return {"rolled": False}

  cands: list[tuple[float, dict[str, Any]]] = []
  for ch in raw_changes:
    if isinstance(ch, dict):
      sub_ts = _parse_ts(ch.get("submitted") or ch.get("updated") or "")
      if sub_ts > 0:
        cands.append((sub_ts, ch))
  cands.sort(key=lambda pair: pair[0])

  roll_sha = ""
  roll_cl_id = ""
  roll_ts = 0.0
  for sub_ts, ch in cands:
    cur_rev = str(ch.get("current_revision") or "")
    rev_obj = (ch.get("revisions") or {}).get(cur_rev) or {}
    msg = str(((rev_obj.get("commit") or {}).get("message")) or "")
    subj = str(ch.get("subject") or "")
    m_log = re.search(r"\+log/[0-9a-f]{7,40}\.\.([0-9a-f]{7,40})\b", msg)
    m_subj = re.search(
        r"Roll\s+Skia\s+from\s+[0-9a-f]{7,40}\s+to\s+([0-9a-f]{7,40})\b",
        subj,
        re.IGNORECASE,
    )
    target_skia = m_log.group(1) if m_log else (m_subj.group(1) if m_subj else "")
    if target_skia and all(
        _is_git_ancestor(skia_dir, fsha, target_skia) for fsha in fix_shas
    ):
      roll_sha = cur_rev
      roll_cl_id = str(ch.get("_number") or "")
      roll_ts = sub_ts
      break

  if not roll_sha:
    return {"rolled": False}

  try:
    dt_roll = datetime.fromtimestamp(roll_ts, tz=ZoneInfo("America/New_York"))
  except (OSError, ValueError):
    dt_roll = datetime.fromtimestamp(roll_ts, tz=timezone.utc)
  roll_date_str = f"{dt_roll.strftime('%b')} {dt_roll.day}"

  # Prefer ChromiumDash shipped Canary version and release timestamp when reachable
  canary_ver = ""
  canary_ts = 0.0
  dash_reached = False
  earliest_tuple: tuple[int, ...] = ()
  try:
    fc = _http_get_json_direct(
        f"https://chromiumdash.appspot.com/fetch_commit?commit={roll_sha}",
        timeout=8,
    )
    if isinstance(fc, dict):
      dash_reached = True
      earliest_tuple = _parse_chrome_version_tuple(str(fc.get("earliest") or ""))
  except (OSError, urllib.error.URLError, ValueError):
    pass

  # Query Windows Canary first and Mac second because Chrome has no Linux Canary channel
  for plat in ("Windows", "Mac"):
    try:
      rels = _http_get_json_direct(
          "https://chromiumdash.appspot.com/fetch_releases"
          f"?channel=Canary&platform={plat}&num=35",
          timeout=8,
      )
    except (OSError, urllib.error.URLError, ValueError):
      continue
    if not isinstance(rels, list) or not rels:
      continue
    dash_reached = True
    ordered = sorted(rels, key=lambda r: float((r or {}).get("time") or 0.0))
    for rel in ordered:
      if not isinstance(rel, dict):
        continue
      ver_s = str(rel.get("version") or "").strip()
      ver_t = _parse_chrome_version_tuple(ver_s)
      rel_skia = str(((rel.get("hashes") or {}).get("skia")) or "").strip()
      matches_ver = bool(earliest_tuple and ver_t and ver_t >= earliest_tuple)
      matches_skia = bool(
          rel_skia
          and all(_is_git_ancestor(skia_dir, fsha, rel_skia) for fsha in fix_shas)
      )
      if matches_ver or matches_skia:
        canary_ver = ver_s
        raw_ms = float(rel.get("time") or 0.0)
        canary_ts = raw_ms / 1000.0 if raw_ms > 1e11 else raw_ms
        break
    if canary_ver:
      break

  return {
      "rolled": True,
      "roll_sha": roll_sha,
      "roll_cl_id": roll_cl_id,
      "roll_ts": roll_ts,
      "roll_date_str": roll_date_str,
      "canary_version": canary_ver,
      "canary_ts": canary_ts,
      # Fall back to roll_ts only when ChromiumDash is unreachable; if reachable and absent, wait
      "in_canary": bool(canary_ver) or not dash_reached,
  }


def _check_skia_main_revert(
    skia_dir: str, sha: str, subject: str
) -> tuple[bool, str]:
  """Checks that `sha` exists locally and has not been reverted on `origin/main`."""
  if not skia_dir or not os.path.isdir(skia_dir):
    return False, "Local Skia repository checkout is missing"
  cat_res = subprocess.run(
      ["git", "-C", skia_dir, "cat-file", "-e", f"{sha}^{{commit}}"],
      stdout=subprocess.DEVNULL,
      stderr=subprocess.DEVNULL,
      timeout=8,
      check=False,
  )
  if cat_res.returncode != 0:
    return False, f"Fix commit {sha[:10]} not found in local Skia checkout"

  log_res = subprocess.run(
      [
          "git",
          "-C",
          skia_dir,
          "log",
          "--format=%H%x1f%s%x1f%b%x1e",
          f"{sha}..refs/remotes/origin/main",
      ],
      capture_output=True,
      text=True,
      timeout=12,
      check=False,
  )
  if log_res.returncode != 0:
    return False, "Could not inspect origin/main history in Skia checkout"

  rev_pat = re.compile(
      rf"This reverts commit {re.escape(sha[:10])}[0-9a-f]*", re.IGNORECASE
  )
  subj_clean = (subject or "").strip()
  for rec in log_res.stdout.split("\x1e"):
    rec_s = rec.strip()
    if not rec_s:
      continue
    parts = rec_s.split("\x1f")
    if len(parts) < 3:
      continue
    c_sha, c_subj, c_body = parts[0].strip(), parts[1].strip(), parts[2]
    is_subj_rev = bool(
        subj_clean and c_subj.lower() == f'revert "{subj_clean}"'.lower()
    )
    if rev_pat.search(c_body) or is_subj_rev:
      msg = f"Fix {sha[:10]} was reverted on Skia main by {c_sha[:10]} ({c_subj})"
      return False, msg
  return True, ""


def _trial_cherry_pick_skia(
    skia_dir: str,
    fix_shas: list[str],
    milestone: int,
    state: dict[str, Any] | None = None,
) -> dict[str, Any]:
  """Runs a detached temporary worktree `git cherry-pick -x` onto `chrome/m<NN>`."""
  if not skia_dir or not os.path.isdir(skia_dir):
    return {
        "ok": False,
        "milestone": milestone,
        "conflicts": [],
        "reason": f"Local Skia checkout unavailable for chrome/m{milestone}",
    }
  tip_sha = ""
  for ref in (
      f"refs/remotes/origin/chrome/m{milestone}",
      f"refs/heads/chrome/m{milestone}",
  ):
    rev_res = subprocess.run(
        ["git", "-C", skia_dir, "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if rev_res.returncode == 0 and rev_res.stdout.strip():
      tip_sha = rev_res.stdout.strip()
      break
  if not tip_sha:
    return {
        "ok": False,
        "milestone": milestone,
        "conflicts": [],
        "reason": f"Branch chrome/m{milestone} not found in Skia repository",
    }

  cache_key = f"{','.join(fix_shas)}|m{milestone}|{tip_sha}"
  cp_cache = (
      state.setdefault("merge_cherry_pick_cache", {})
      if isinstance(state, dict)
      else None
  )
  if isinstance(cp_cache, dict) and isinstance(cp_cache.get(cache_key), dict):
    return dict(cp_cache[cache_key])

  wt_dir = tempfile.mkdtemp(prefix=f"fd_cp_m{milestone}_")
  try:
    add_res = subprocess.run(
        ["git", "-C", skia_dir, "worktree", "add", "--detach", wt_dir, tip_sha],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if add_res.returncode != 0:
      out = {
          "ok": False,
          "milestone": milestone,
          "conflicts": [],
          "reason": f"Failed to create trial worktree for chrome/m{milestone}",
      }
    else:
      cp_res = subprocess.run(
          [
              "git",
              "-C",
              wt_dir,
              "-c",
              "user.name=Flight Deck",
              "-c",
              "user.email=jlavrova@google.com",
              "cherry-pick",
              "-x",
              *fix_shas,
          ],
          capture_output=True,
          text=True,
          timeout=20,
          check=False,
      )
      if cp_res.returncode == 0:
        out = {
            "ok": True,
            "milestone": milestone,
            "conflicts": [],
            "reason": "",
        }
      else:
        diff_res = subprocess.run(
            ["git", "-C", wt_dir, "diff", "--name-only", "--diff-filter=U"],
            capture_output=True,
            text=True,
            timeout=8,
            check=False,
        )
        conflicts = sorted({
            ln.strip() for ln in diff_res.stdout.splitlines() if ln.strip()
        })
        subprocess.run(
            ["git", "-C", wt_dir, "cherry-pick", "--abort"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=8,
            check=False,
        )
        if conflicts:
          reason = (
              f"Cherry-pick conflict on chrome/m{milestone}: "
              f"{', '.join(conflicts)}"
          )
        else:
          err_snip = " ".join((cp_res.stderr or cp_res.stdout or "").split())[:140]
          reason = (
              f"Cherry-pick failed on chrome/m{milestone}: "
              f"{err_snip or 'non-zero exit'}"
          )
        out = {
            "ok": False,
            "milestone": milestone,
            "conflicts": conflicts,
            "reason": reason,
        }
  finally:
    subprocess.run(
        ["git", "-C", skia_dir, "worktree", "remove", "--force", wt_dir],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=12,
        check=False,
    )
    shutil.rmtree(wt_dir, ignore_errors=True)
    subprocess.run(
        ["git", "-C", skia_dir, "worktree", "prune"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=8,
        check=False,
    )

  if isinstance(cp_cache, dict):
    if len(cp_cache) >= 256:
      cp_cache.clear()
    cp_cache[cache_key] = dict(out)
  return out


def _join_english_list(items: list[str]) -> str:
  if not items:
    return ""
  if len(items) == 1:
    return items[0]
  if len(items) == 2:
    return f"{items[0]} and {items[1]}"
  return f"{', '.join(items[:-1])}, and {items[-1]}"


def _format_merge_branches(milestones: list[int]) -> str:
  if not milestones:
    return "Skia release branches"
  first = f"Skia chrome/m{milestones[0]}"
  if len(milestones) == 1:
    return first
  tail = [f"m{ms}" for ms in milestones[1:]]
  return _join_english_list([first, *tail])


def _summarize_merge_diff_risk(
    skia_dir: str,
    fix_cls: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    allow_llm: bool = False,
) -> str:
  """Drafts a concise regression-risk clause from the git diff with deterministic fallback."""
  fix_shas = [str(c.get("sha") or "") for c in fix_cls if c.get("sha")]
  cache_key = ",".join(fix_shas)
  summaries = (
      state.setdefault("merge_risk_summaries", {})
      if isinstance(state, dict)
      else None
  )
  if isinstance(summaries, dict) and summaries.get(cache_key):
    return str(summaries[cache_key])

  prod_files: list[str] = []
  add_lines = 0
  del_lines = 0
  diff_chunks: list[str] = []
  if skia_dir and os.path.isdir(skia_dir):
    for sha in fix_shas:
      ns_res = subprocess.run(
          ["git", "-C", skia_dir, "show", "--numstat", "--format=", sha],
          capture_output=True,
          text=True,
          timeout=8,
          check=False,
      )
      for ln in ns_res.stdout.splitlines():
        cols = ln.split("\t")
        if len(cols) == 3:
          fpath = cols[2].strip()
          if not fpath.startswith("tests/"):
            if fpath not in prod_files:
              prod_files.append(fpath)
            if cols[0].isdigit():
              add_lines += int(cols[0])
            if cols[1].isdigit():
              del_lines += int(cols[1])
      if allow_llm:
        sh_res = subprocess.run(
            ["git", "-C", skia_dir, "show", "--stat", "--unified=3", sha],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if sh_res.returncode == 0 and sh_res.stdout:
          diff_chunks.append(sh_res.stdout[:8000])

  if allow_llm and diff_chunks and os.path.isfile(ANTIGRAVITY_SDK_CLI):
    prompt = (
        "Complete the clause after 'Regression risk is low: ' in 12 to 25 plain "
        "technical English words summarizing what this Skia security fix diff "
        "changes and why regression risk is low. Output ONLY that single clause "
        "(no 'Regression risk is low:' prefix, no quotes, no trailing period, "
        "no markdown bullets).\n\n"
        + "\n\n".join(diff_chunks)[:12000]
    )
    try:
      sdk_res = subprocess.run(
          [ANTIGRAVITY_SDK_CLI, "--generate", f"--prompt={prompt}"],
          capture_output=True,
          text=True,
          timeout=38,
          check=False,
      )
      raw_lines: list[str] = []
      for ln in sdk_res.stdout.splitlines():
        s_ln = ln.strip()
        if s_ln.startswith("Logs for language server ->"):
          log_path = s_ln.split("->", 1)[1].strip()
          if log_path.startswith("/tmp/agy_") and os.path.isfile(log_path):
            try:
              os.remove(log_path)
            except OSError:
              pass
          continue
        if s_ln.startswith("If this is your first time running the language server"):
          continue
        if s_ln:
          raw_lines.append(s_ln)
      cand = " ".join(raw_lines).strip().strip("\"'")
      cand = re.sub(
          r"^regression\s+risk\s+is\s+low\s*:\s*", "", cand, flags=re.IGNORECASE
      ).rstrip(".")
      if 12 <= len(cand) <= 240:
        if cand[0].isupper() and not cand.split(" ", 1)[0].startswith("Sk"):
          cand = cand[0].lower() + cand[1:]
        if isinstance(summaries, dict):
          summaries[cache_key] = cand
        return cand
    except (OSError, subprocess.SubprocessError):
      pass

  if not prod_files:
    for c in fix_cls:
      for fpath in c.get("files") or []:
        if not str(fpath).startswith("tests/") and str(fpath) not in prod_files:
          prod_files.append(str(fpath))
  target_desc = (
      _join_english_list(prod_files[:2])
      if prod_files
      else "the affected Skia component"
  )
  if len(prod_files) > 2:
    rem = len(prod_files) - 2
    target_desc = f"{', '.join(prod_files[:2])}, and {rem} other file(s)"
  delta_part = (
      f" (+{add_lines}/-{del_lines} non-test lines)"
      if (add_lines or del_lines)
      else ""
  )
  fallback = (
      f"the change is localized to {target_desc}{delta_part} with unit test coverage"
  )
  if isinstance(summaries, dict) and allow_llm:
    summaries[cache_key] = fallback
  return fallback


def _normalize_os_tokens(raw_os: str) -> list[str]:
  return sorted(
      {tok.strip() for tok in (raw_os or "").split(",") if tok.strip()},
      key=str.lower,
  )


def evaluate_security_merge_groups(
    bugs: dict[str, dict[str, Any]],
    *,
    skia_dir: str = DEFAULT_SKIA_DIR,
    now_ts: float | None = None,
    state: dict[str, Any] | None = None,
    parent_lookup: dict[str, dict[str, Any]] | None = None,
    allow_llm: bool = False,
) -> dict[str, dict[str, Any]]:
  """Groups active Security merge requests by parent bug and evaluates all 6 merge gates."""
  cur_now = time.time() if now_ts is None else now_ts
  lookup = parent_lookup if isinstance(parent_lookup, dict) else bugs
  confirmed_bugs = (
      state.get("confirmed_merge_bugs")
      if isinstance(state, dict)
      and isinstance(state.get("confirmed_merge_bugs"), dict)
      else {}
  )

  by_parent: dict[str, list[tuple[int, dict[str, Any]]]] = {}
  all_milestones: set[int] = set()
  for bug in bugs.values():
    if not isinstance(bug, dict) or not is_security_merge_request(bug):
      continue
    ms = extract_merge_milestone(str(bug.get("title") or ""))
    pid = str(bug.get("parent_issue_id") or "").strip()
    if ms is None or not pid:
      continue
    by_parent.setdefault(pid, []).append((ms, bug))
    all_milestones.add(ms)

  if not by_parent:
    return {}

  _refresh_skia_merge_refs(skia_dir, sorted(all_milestones))

  groups: dict[str, dict[str, Any]] = {}
  for parent_id, pairs in sorted(by_parent.items()):
    pairs.sort(key=lambda item: (item[0], str(item[1].get("id") or "")))
    milestones = sorted({ms for ms, _ in pairs})
    parent = lookup.get(f"buganizer:{parent_id}") or bugs.get(
        f"buganizer:{parent_id}"
    )
    parent_dict = parent if isinstance(parent, dict) else {}
    parent_cf = (
        parent_dict.get("custom_fields")
        if isinstance(parent_dict.get("custom_fields"), dict)
        else {}
    )

    first_child = pairs[0][1]
    m_strip = RE_MERGE_TITLE_MILESTONE.match(str(first_child.get("title") or ""))
    fallback_title = (
        m_strip.group(2).strip()
        if m_strip
        else str(first_child.get("title") or "")
    )
    parent_title = str(parent_dict.get("title") or fallback_title).strip()
    proj_name = str(
        parent_dict.get("project_name")
        or first_child.get("project_name")
        or "Skia"
    )
    proj_id = str(
        parent_dict.get("project_id") or first_child.get("project_id") or ""
    )
    for _, child_b in pairs:
      child_b["is_security_merge_request"] = True
      child_b["merge_group_parent_id"] = parent_id
      if proj_name and proj_name != "Unassigned":
        child_b["project_name"] = proj_name
        child_b["project_id"] = proj_id

    blocking_reasons: list[str] = []

    # Require parent custom field 1358989 "Fixed By Code Changes" to list merged Skia CLs
    raw_fixed_by = str(parent_cf.get(CF_BUG_FIXED_BY_CODE_CHANGES) or "").strip()
    parsed_fixed = _parse_gerrit_host_and_cl_urls(raw_fixed_by)
    fix_cls: list[dict[str, Any]] = []
    g1_ok = True
    g1_reason = ""
    if not parent_dict:
      g1_ok = False
      g1_reason = f"Parent crbug/{parent_id} metadata could not be loaded"
    elif not raw_fixed_by or raw_fixed_by.upper() in ("NA", "N/A"):
      g1_ok = False
      g1_reason = (
          f"Parent crbug/{parent_id} custom field 'Fixed By Code Changes' "
          "(1358989) is empty"
      )
    elif not parsed_fixed:
      g1_ok = False
      g1_reason = (
          "Parent 'Fixed By Code Changes' has no valid Gerrit URL: "
          f"{raw_fixed_by[:100]}"
      )
    else:
      for host, cid, _ in parsed_fixed:
        det = _fetch_merge_cl_detail(host, cid)
        if not det.get("ok"):
          g1_ok = False
          g1_reason = f"Could not fetch Gerrit details for fix CL {cid}"
          break
        if str(det.get("status") or "").upper() != "MERGED" or not det.get("sha"):
          g1_ok = False
          g1_reason = (
              f"Fix CL {cid} is not merged yet "
              f"(status: {det.get('status') or 'UNKNOWN'})"
          )
          break
        if str(det.get("project") or "") != "skia":
          g1_ok = False
          g1_reason = (
              f"Fix CL {cid} belongs to {det.get('project') or host} "
              "(automated cherry-pick supports Skia only)"
          )
          break
        fix_cls.append(det)
    if not g1_ok and g1_reason:
      blocking_reasons.append(g1_reason)

    # Require every other CL on parent Code Changes to touch only tests/ (fail closed on empty)
    fixed_ids = {c["cl_id"] for c in fix_cls} | {cid for _, cid, _ in parsed_fixed}
    parent_cl_ids = [
        str(cid)
        for cid in (parent_dict.get("gerrit_cl_ids") or [])
        if str(cid) and str(cid) not in fixed_ids
    ]
    parent_urls = (
        parent_dict.get("gerrit_cl_urls")
        if isinstance(parent_dict.get("gerrit_cl_urls"), dict)
        else {}
    )
    followup_cls: list[dict[str, Any]] = []
    g2_ok = bool(parent_dict)
    g2_reason = ""
    for f_cid in parent_cl_ids:
      raw_u = str(parent_urls.get(f_cid) or "")
      parsed_u = _parse_gerrit_host_and_cl_urls(raw_u)
      f_host = parsed_u[0][0] if parsed_u else GERRIT_SKIA_HOST
      f_det = _fetch_merge_cl_detail(f_host, f_cid)
      if not f_det.get("ok"):
        g2_ok = False
        g2_reason = f"Could not inspect follow-up CL {f_cid} on parent issue"
        blocking_reasons.append(g2_reason)
        continue
      # Ignore release-branch cherry-picks already linked back to the parent bug
      if str(f_det.get("branch") or "").startswith("chrome/m"):
        continue
      f_files = [str(p) for p in (f_det.get("files") or []) if str(p)]
      if not f_files:
        g2_ok = False
        g2_reason = (
            f"Follow-up CL {f_cid} returned an empty file list "
            "(cannot prove tests/-only)"
        )
        blocking_reasons.append(g2_reason)
        continue
      non_test = [p for p in f_files if not p.startswith("tests/")]
      if non_test:
        g2_ok = False
        g2_reason = (
            f"Follow-up CL {f_cid} touches production files "
            f"({', '.join(non_test[:3])}) and may also need a merge"
        )
        blocking_reasons.append(g2_reason)
        continue
      followup_cls.append(f_det)

    # Require at least 24h Canary soak (ChromiumDash Canary preferred, Roll Skia fallback)
    canary_info: dict[str, Any] = {}
    soak_days = 0
    soak_hours = 0.0
    g3_ok = False
    g3_reason = ""
    if g1_ok and fix_cls:
      fix_shas = [str(c["sha"]) for c in fix_cls]
      sub_times = sorted(
          (str(c.get("submitted") or "") for c in fix_cls),
          key=_parse_ts,
      )
      latest_sub = sub_times[-1] if sub_times else ""
      canary_info = _fetch_skia_roll_and_canary(skia_dir, fix_shas, latest_sub)
      if not canary_info.get("rolled"):
        g3_reason = "Fix has not rolled into Chromium main yet"
      elif not canary_info.get("in_canary"):
        r_date = str(canary_info.get("roll_date_str") or "recently")
        g3_reason = f"Rolled into Chromium ({r_date}), waiting for Canary release"
      else:
        soak_start_ts = float(canary_info.get("canary_ts") or 0.0) or float(
            canary_info.get("roll_ts") or 0.0
        )
        if soak_start_ts <= 0.0:
          g3_reason = "Could not determine Chromium roll / Canary soak start time"
        else:
          soak_sec = cur_now - soak_start_ts
          soak_hours = round(max(0.0, soak_sec) / 3600.0, 1)
          soak_days = max(0, int(soak_sec // 86400))
          if soak_sec < CANARY_SOAK_MIN_SEC:
            ready_ts = soak_start_ts + CANARY_SOAK_MIN_SEC
            try:
              dt_ready = datetime.fromtimestamp(
                  ready_ts, tz=ZoneInfo("America/New_York")
              )
              wait_str = dt_ready.strftime("%b %d, %H:%M ET")
            except (OSError, ValueError):
              dt_ready = datetime.fromtimestamp(ready_ts, tz=timezone.utc)
              wait_str = dt_ready.strftime("%b %d, %H:%M UTC")
            hrs = max(0, int(soak_sec // 3600))
            g3_reason = f"Canary soak under 24h ({hrs}h) — wait until {wait_str}"
          else:
            g3_ok = True
      if not g3_ok and g3_reason:
        blocking_reasons.append(g3_reason)

    # Verify that no fix commit was reverted on Skia origin/main
    g4_ok = False
    g4_reason = ""
    if g1_ok and fix_cls:
      g4_ok = True
      for c in fix_cls:
        ok_rev, rev_msg = _check_skia_main_revert(
            skia_dir, str(c["sha"]), str(c.get("subject") or "")
        )
        if not ok_rev:
          g4_ok = False
          g4_reason = rev_msg
          blocking_reasons.append(rev_msg)
          break

    # Verify clean trial cherry-pick -x onto every target chrome/m<NN> branch
    cp_results: list[dict[str, Any]] = []
    g5_ok = False
    g5_reason = ""
    if g1_ok and fix_cls:
      fix_shas = [str(c["sha"]) for c in fix_cls]
      g5_ok = True
      for ms in milestones:
        cp_res = _trial_cherry_pick_skia(skia_dir, fix_shas, ms, state=state)
        cp_results.append(cp_res)
        if not cp_res.get("ok"):
          g5_ok = False
          r_txt = str(cp_res.get("reason") or f"Cherry-pick failed on chrome/m{ms}")
          if not g5_reason:
            g5_reason = r_txt
          blocking_reasons.append(r_txt)

    # Surface OS custom field (1223084) mismatch against parent as a non-blocking warning
    parent_os_tokens = _normalize_os_tokens(str(parent_cf.get(CF_BUG_OS) or ""))
    os_mismatches: list[str] = []
    requests_out: list[dict[str, Any]] = []
    for ms, child_b in pairs:
      c_cf = (
          child_b.get("custom_fields")
          if isinstance(child_b.get("custom_fields"), dict)
          else {}
      )
      c_os_tokens = _normalize_os_tokens(str(c_cf.get(CF_BUG_OS) or ""))
      if c_os_tokens != parent_os_tokens:
        os_mismatches.append(f"M{ms} ({', '.join(c_os_tokens) or 'empty'})")
      bid = str(child_b.get("id") or "")
      already_done = bool(
          _parse_ts(child_b.get("last_self_comment_at")) > 0.0
          or bid in confirmed_bugs
      )
      requests_out.append({
          "id": bid,
          "bug_key": str(child_b.get("bug_key") or f"buganizer:{bid}"),
          "short_id": str(child_b.get("short_id") or f"crbug/{bid}"),
          "milestone": ms,
          "url": str(
              child_b.get("url") or f"https://issues.chromium.org/issues/{bid}"
          ),
          "os": ", ".join(c_os_tokens),
          "already_commented": already_done,
      })

    os_match_ok = len(os_mismatches) == 0 and bool(parent_os_tokens)
    os_warning = ""
    if os_mismatches:
      p_os_str = ", ".join(parent_os_tokens) or "empty"
      os_warning = f"OS mismatch vs parent ({p_os_str}): {'; '.join(os_mismatches)}"
    elif not parent_os_tokens and parent_dict:
      os_warning = f"Parent crbug/{parent_id} OS field (1223084) is empty"

    hard_gates_ok = bool(g1_ok and g2_ok and g3_ok and g4_ok and g5_ok)
    comment_draft = ""
    if hard_gates_ok:
      fix_parts = [
          f"{c['url']} ({c['repo']} {c['short_sha']})" for c in fix_cls
      ]
      sentences = [f"Fix: {', '.join(fix_parts)}."]
      if len(followup_cls) == 1:
        f_id = followup_cls[0]["cl_id"]
        sentences.append(f"The test-only follow-up {f_id} does not need a merge.")
      elif len(followup_cls) > 1:
        f_list_str = _join_english_list([c["cl_id"] for c in followup_cls])
        sentences.append(
            f"The test-only follow-ups {f_list_str} do not need a merge."
        )
      roll_d = str(canary_info.get("roll_date_str") or "trunk")
      day_word = "day" if soak_days == 1 else "days"
      sentences.append(
          f"It rolled into Chromium on {roll_d} and has soaked in Canary for "
          f"{soak_days} {day_word} without known issues."
      )
      if os_match_ok:
        sentences.append("OS list is correct.")
      risk_clause = _summarize_merge_diff_risk(
          skia_dir, fix_cls, state=state, allow_llm=allow_llm
      )
      sentences.append(f"Regression risk is low: {risk_clause}.")
      branches_str = _format_merge_branches(milestones)
      sentences.append(
          f"It cherry-picks cleanly onto {branches_str}. Requesting merge approval."
      )
      comment_draft = " ".join(sentences)

    ms_label = "/".join(f"M{ms}" for ms in milestones)
    priorities = sorted(
        str(b.get("priority") or "P2").upper() for _, b in pairs
    )
    severities = sorted(
        str(b.get("severity") or "").upper()
        for _, b in pairs
        if str(b.get("severity") or "").strip()
    )
    groups[parent_id] = {
        "group_key": f"merge_group:{parent_id}",
        "parent_id": parent_id,
        "parent_short_id": str(
            parent_dict.get("short_id") or f"crbug/{parent_id}"
        ),
        "parent_url": str(
            parent_dict.get("url")
            or f"https://issues.chromium.org/issues/{parent_id}"
        ),
        "parent_title": parent_title,
        "project_id": proj_id,
        "project_name": proj_name,
        "priority": priorities[0] if priorities else "P1",
        "severity": severities[0] if severities else "",
        "milestones": milestones,
        "milestones_label": ms_label,
        "button_label": f"Подтвердить merge {ms_label}",
        "requests": requests_out,
        "all_already_commented": all(
            bool(r.get("already_commented")) for r in requests_out
        ),
        "hard_gates_ok": hard_gates_ok,
        "blocking_reasons": blocking_reasons,
        "os_warning": os_warning,
        "comment_draft": comment_draft,
        "gates": {
            "fixed_by_cls": {
                "ok": g1_ok,
                "cls": [
                    {
                        "cl_id": c["cl_id"],
                        "url": c["url"],
                        "short_sha": c["short_sha"],
                    }
                    for c in fix_cls
                ],
                "reason": g1_reason,
            },
            "test_only_followups": {
                "ok": g2_ok,
                "followups": [c["cl_id"] for c in followup_cls],
                "reason": g2_reason,
            },
            "canary_soak": {
                "ok": g3_ok,
                "rolled": bool(canary_info.get("rolled")),
                "roll_date_str": str(canary_info.get("roll_date_str") or ""),
                "roll_cl_id": str(canary_info.get("roll_cl_id") or ""),
                "canary_version": str(canary_info.get("canary_version") or ""),
                "soak_days": soak_days,
                "soak_hours": soak_hours,
                "reason": g3_reason,
            },
            "no_revert": {"ok": g4_ok, "reason": g4_reason},
            "cherry_pick": {"ok": g5_ok, "results": cp_results, "reason": g5_reason},
            "os_match": {
                "ok": os_match_ok,
                "parent_os": ", ".join(parent_os_tokens),
                "warning": os_warning,
            },
        },
    }
  return groups


def confirm_security_merge_group(
    parent_id: str, comment_override: str = ""
) -> dict[str, Any]:
  """Posts the merge approval comment via Buganizer MCP and adds safe-cc (never reassigns)."""
  pid = str(parent_id or "").strip().removeprefix("merge_group:")
  if not pid:
    return {"ok": False, "error": "Missing parent_id"}
  state = load_watcher_state()
  groups = state.get("merge_request_groups") or {}
  grp = groups.get(pid)
  if not isinstance(grp, dict):
    return {"ok": False, "error": f"Unknown merge group {pid}"}
  if not grp.get("hard_gates_ok"):
    return {"ok": False, "error": "Cannot confirm merge while hard gates fail"}

  comment_md = str(comment_override or grp.get("comment_draft") or "").strip()
  if not comment_md:
    return {"ok": False, "error": "Merge comment draft is empty"}

  requests = [
      r for r in (grp.get("requests") or []) if isinstance(r, dict) and r.get("id")
  ]
  if not requests:
    return {"ok": False, "error": "No merge request issues in group"}

  confirmed_map = state.setdefault("confirmed_merge_bugs", {})
  bugs_map = state.get("bugs") or {}
  errors: list[str] = []
  commented_ids: list[str] = []
  cc_ids: list[str] = []

  to_comment = [r for r in requests if not r.get("already_commented")]
  comment_ok_ids: set[str] = {
      str(r["id"]) for r in requests if r.get("already_commented")
  }
  if to_comment:
    mcp_calls = [
        (
            "add_buganizer_comment",
            {"issueId": str(r["id"]), "commentMarkdown": comment_md},
        )
        for r in to_comment
    ]
    mcp_outs = call_buganizer_mcp_batch(mcp_calls, timeout_sec=30.0)
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for r, out_txt in zip(to_comment, mcp_outs):
      bid = str(r["id"])
      clean_out = (out_txt or "").strip()
      if clean_out and not clean_out.lower().startswith(("error", "failed")):
        r["already_commented"] = True
        comment_ok_ids.add(bid)
        commented_ids.append(bid)
        confirmed_map[bid] = {"ts": time.time(), "parent_id": pid}
        b_rec = bugs_map.get(f"buganizer:{bid}")
        if isinstance(b_rec, dict):
          b_rec["last_self_comment_at"] = now_iso
          classify_bug_status(b_rec)
      else:
        snip = clean_out[:120] or "empty response"
        errors.append(f"crbug/{bid}: MCP comment failed ({snip})")

  # Only safe-cc is permitted here; reassignment runs separately on explicit user action
  for r in requests:
    bid = str(r["id"])
    if bid not in comment_ok_ids:
      continue
    try:
      cc_res = subprocess.run(
          [
              ISSUES_CLI_BIN,
              "mutate",
              "update",
              "safe-cc",
              bid,
              "jlavrova@google.com",
          ],
          capture_output=True,
          text=True,
          timeout=15,
          check=False,
      )
      if cc_res.returncode == 0:
        cc_ids.append(bid)
      else:
        err_msg = (cc_res.stderr or cc_res.stdout or "").strip()[:120]
        errors.append(
            f"crbug/{bid}: safe-cc exited {cc_res.returncode} ({err_msg})"
        )
    except (OSError, subprocess.SubprocessError) as exc:
      errors.append(f"crbug/{bid}: safe-cc error ({exc})")

  grp["all_already_commented"] = all(
      bool(r.get("already_commented")) for r in requests
  )
  save_watcher_state(state)
  urls = [
      str(r.get("url") or f"https://issues.chromium.org/issues/{r['id']}")
      for r in requests
  ]
  return {
      "ok": len(errors) == 0,
      "parent_id": pid,
      "urls": urls,
      "commented_ids": commented_ids,
      "cc_ids": cc_ids,
      "errors": errors,
  }


def classify_bug_status(
    bug: dict[str, Any], now_ts: float | None = None
) -> dict[str, Any]:
  """Classifies a bug into `you` / `overdue` / `has_cl` / `assigned` / `fixed` / `closed`."""
  cur_now = time.time() if now_ts is None else now_ts
  status = str(bug.get("status") or "OPEN").upper()
  source = str(bug.get("source") or "buganizer")
  state_reason = str(bug.get("state_reason") or "").upper()

  bug["is_overdue"] = False
  bug["slo_summary"] = ""
  bug["needs_you"] = False
  bug["why_needs_you"] = ""
  bug["is_stale_backlog"] = False
  bug["merge_action_phase"] = ""
  is_sec_merge = is_security_merge_request(bug)
  bug["is_security_merge_request"] = is_sec_merge
  bug["merge_group_parent_id"] = (
      str(bug.get("parent_issue_id") or "").strip() if is_sec_merge else ""
  )
  is_merge_req = is_merge_request_bug(bug)
  bug["is_merge_request_bug"] = is_merge_req
  ms_num = _bug_merge_milestone(bug) if is_merge_req else None
  if ms_num is not None:
    bug["merge_milestone"] = ms_num

  # Classify terminal upstream states into Fixed vs Closed (obsolete / duplicate / not planned)
  if source == "github" and status == "CLOSED":
    bug["lane"] = "closed" if state_reason == "NOT_PLANNED" else "fixed"
    return bug
  if status in ("FIXED", "VERIFIED"):
    bug["lane"] = "fixed"
    return bug
  if status.startswith("DUPLICATE") or status in (
      "OBSOLETE",
      "NOT_REPRODUCIBLE",
      "INFEASIBLE",
      "INTENDED_BEHAVIOR",
      "WONT_FIX",
      "CLOSED",
  ):
    bug["lane"] = "closed"
    return bug

  # Check Due Date and active SLO deadlines; ignore Assignment SLO once already ASSIGNED
  slo = bug.get("slo") if isinstance(bug.get("slo"), dict) else {}
  deadlines: list[tuple[str, float, str]] = []
  due_raw = str(bug.get("due_date") or "").strip()
  if due_raw:
    dts = _parse_ts(due_raw)
    if dts > 0:
      deadlines.append(("Due date", dts, due_raw))
  if status == "NEW" and slo.get("assignment_slo"):
    ats = _parse_ts(slo["assignment_slo"])
    if ats > 0:
      deadlines.append(("Assignment SLO", ats, str(slo["assignment_slo"])))
  for key, label in (("response_slo", "Response SLO"), ("closure_slo", "Closure SLO")):
    raw_s = str(slo.get(key) or "").strip()
    if raw_s:
      sts = _parse_ts(raw_s)
      if sts > 0:
        deadlines.append((label, sts, raw_s))

  overdue_notes: list[str] = []
  soon_notes: list[str] = []
  for label, dts, raw_s in deadlines:
    et_str, _ = format_et_and_age(raw_s)
    if dts < cur_now:
      overdue_days = max(1, int((cur_now - dts) // 86400)) if (cur_now - dts) >= 86400 else 0
      age_txt = f"{overdue_days}d overdue" if overdue_days else "overdue"
      overdue_notes.append(f"{label} {age_txt} ({et_str or raw_s})")
    elif dts - cur_now <= 86400:
      hrs = max(1, int((dts - cur_now) // 3600))
      soon_notes.append(f"{label} due in {hrs}h ({et_str or raw_s})")

  # Collaborators and non-assigned mentions are not responsible for the assignee's SLO deadlines
  is_collab = bool(bug.get("is_collaborator"))
  is_mentioned = bool(bug.get("is_mentioned"))
  is_non_owner = is_collab or (
      is_mentioned and not _is_self_bug_assignee(str(bug.get("assignee") or ""))
  )
  if not is_non_owner:
    if overdue_notes:
      bug["is_overdue"] = True
      bug["slo_summary"] = overdue_notes[0]
    elif soon_notes:
      bug["slo_summary"] = soon_notes[0]

  linked_cls = [c for c in (bug.get("linked_cls") or []) if isinstance(c, dict)]
  has_cl = len(linked_cls) > 0
  only_wip_cls = has_cl and all(bool(c.get("is_wip")) for c in linked_cls)
  real_statuses = [str(c.get("status") or "").upper() for c in linked_cls]
  all_cls_merged = has_cl and all(s == "MERGED" for s in real_statuses)
  latest_merged_ts = max(
      (float(c.get("submitted_ts") or 0.0) for c in linked_cls),
      default=0.0,
  )
  bug["latest_cl_merged_ts"] = latest_merged_ts

  acked_ts = float(bug.get("acked_ts") or 0.0)
  self_lower = {e.lower() for e in SELF_BUG_EMAILS} | {
      u.lower() for u in SELF_GITHUB_LOGINS
  }
  human_author = str(bug.get("last_human_comment_author") or "").strip()
  human_ts = _parse_ts(bug.get("last_human_comment_at"))
  self_ts = _parse_ts(bug.get("last_self_comment_at"))

  # Security merge requests carry the merged trunk CL from creation, so evaluate their
  # confirm -> reassign -> merges@chromium.org -> manual cherry-pick lifecycle before
  # generic collaborator or "All linked CLs merged" closure rules
  if is_merge_req:
    is_self_assigned = _is_self_bug_assignee(str(bug.get("assignee") or ""))
    if not is_self_assigned:
      bug["lane"] = "has_cl" if has_cl else "assigned"
      return bug
    cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
    merge_val = str(cf.get(CF_BUG_MERGE) or bug.get("merge_status") or "").strip()
    merge_low = merge_val.lower()
    ms_label = f"M{ms_num}" if ms_num is not None else "Merge"
    mod_ts_val = float(bug.get("modified_ts") or _parse_ts(bug.get("modified_time")) or 0.0)
    explicitly_acked = acked_ts > 0.0 and acked_ts >= mod_ts_val
    has_self_comment = bool(str(bug.get("last_self_comment_at") or "").strip()) or self_ts > 0.0
    if not explicitly_acked:
      if merge_low == "pending":
        bug["needs_you"] = True
        bug["lane"] = "you"
        # Self-commenting does not complete the handoff while assignee is still jlavrova
        if has_self_comment:
          bug["merge_action_phase"] = "reassign"
          bug["why_needs_you"] = (
              f"{ms_label} merge comment posted — reassign to merges@chromium.org"
          )
        else:
          bug["merge_action_phase"] = "confirm"
          bug["why_needs_you"] = f"{ms_label} merge request pending — confirm"
        return bug
      if merge_low.startswith("approved"):
        has_release_branch_cl = any(
            bool(c.get("is_cherry_pick"))
            or (
                str(c.get("branch") or "").strip()
                and str(c.get("branch") or "").strip().lower() not in ("main", "master")
            )
            for c in linked_cls
        )
        if not has_release_branch_cl:
          branch_hint = f"chrome/m{ms_num}" if ms_num is not None else "release branch"
          bug["needs_you"] = True
          bug["lane"] = "you"
          bug["merge_action_phase"] = "manual_cherry_pick"
          bug["why_needs_you"] = (
              f"{ms_label} merge approved — manual cherry-pick onto {branch_hint} needed"
          )
          return bug
    if bug["is_overdue"]:
      bug["lane"] = "overdue"
      return bug
    bug["lane"] = "has_cl" if has_cl else "assigned"
    return bug
  # Treat a self comment as an implicit ack across comment, urgent, and post-merge triggers
  effective_ack_ts = max(self_ts, acked_ts)

  # Active or merged non-WIP self fix CLs implicitly acknowledge earlier bug comments
  is_self_assigned = _is_self_bug_assignee(str(bug.get("assignee") or ""))
  non_wip_self_cl_ts = max(
      (
          max(float(c.get("activity_ts") or 0.0), float(c.get("submitted_ts") or 0.0))
          for c in linked_cls
          if c.get("is_self")
          and not c.get("is_wip")
          and str(c.get("status") or "").upper() in ("NEW", "OPEN", "MERGED")
      ),
      default=0.0,
  )
  comment_ack_ts = max(effective_ack_ts, non_wip_self_cl_ts)

  # Issues pulled in only via a CL reference require prior participation or an explicit mention
  cl_only_bug = (
      has_cl
      and not is_self_assigned
      and not is_collab
      and not is_mentioned
      and not bool(bug.get("from_self_query"))
  )
  has_mention_cue = bool(
      re.search(
          r"(?:@|\b)(?:rusino|jlavrova)\b",
          str(bug.get("last_human_comment_snippet") or ""),
          re.I,
      )
  )
  unacked_comment = (
      bool(human_author)
      and human_author.lower() not in self_lower
      and human_ts > comment_ack_ts
      and (not cl_only_bug or self_ts > 0.0 or has_mention_cue)
  )

  pri = str(bug.get("priority") or "P2").upper()
  itype = str(bug.get("issue_type") or "BUG").upper()
  created_ts = _parse_ts(bug.get("created_time"))
  recent_created = created_ts > 0 and (cur_now - created_ts) <= 7 * 86400
  # Only the assignee owns CL creation and issue closure when linked CLs merge
  unacked_urgent = (
      not is_non_owner
      and (not has_cl or only_wip_cls)
      and effective_ack_ts <= 0.0
      and (pri in ("P0", "P1") or itype == "VULNERABILITY")
      and (status == "NEW" or recent_created)
  )
  all_merged_unacked = (
      not is_non_owner
      and all_cls_merged
      and (
          effective_ack_ts < latest_merged_ts
          if latest_merged_ts > 0.0
          else acked_ts < 1.0
      )
  )

  if unacked_comment:
    snip = str(bug.get("last_human_comment_snippet") or "").strip()
    prefix = (
        "Collaborator · "
        if is_collab
        else ("Mentioned · " if is_mentioned else "")
    )
    bug["needs_you"] = True
    bug["why_needs_you"] = (
        f"{prefix}New comment from {human_author}: {snip}"
        if snip
        else f"{prefix}New comment from {human_author}"
    )
  # Surface open collaborator bugs in Needs you until the user comments or acknowledges
  elif is_collab and self_ts <= 0.0 and acked_ts <= 0.0:
    owner_raw = str(bug.get("assignee") or "").split("@", 1)[0].strip()
    owner_part = (
        f" (owner: {owner_raw})" if owner_raw and owner_raw.upper() != "N/A" else ""
    )
    bug["needs_you"] = True
    bug["why_needs_you"] = f"Collaborator{owner_part} — needs your review / input"
  elif all_merged_unacked:
    bug["needs_you"] = True
    bug["why_needs_you"] = "All linked CLs merged — ready to mark Fixed?"
  elif unacked_urgent:
    bug["needs_you"] = True
    if only_wip_cls:
      bug["why_needs_you"] = (
          f"New {pri} vulnerability — WIP CL ready for review"
          if itype == "VULNERABILITY"
          else f"New {pri} issue — WIP CL ready for review"
      )
    else:
      bug["why_needs_you"] = (
          f"New {pri} vulnerability needs triage"
          if itype == "VULNERABILITY"
          else f"New {pri} issue without a CL yet"
      )

  if bug["needs_you"]:
    bug["lane"] = "you"
    return bug
  if bug["is_overdue"]:
    bug["lane"] = "overdue"
    return bug
  if has_cl:
    bug["lane"] = "has_cl"
    return bug

  mod_ts = float(bug.get("modified_ts") or _parse_ts(bug.get("modified_time")) or 0.0)
  age_days = (cur_now - mod_ts) / 86400.0 if mod_ts > 0 else 999.0
  bug["is_stale_backlog"] = (
      pri not in ("P0", "P1")
      and not bool(bug.get("conversation_id"))
      and age_days > BUG_STALE_BACKLOG_DAYS
  )
  bug["lane"] = "assigned"
  return bug


def should_autostart_bug_triage(bug: dict[str, Any]) -> bool:
  """Returns True when an unchatted Needs You bug should auto-launch a bug-triage chat."""
  if not isinstance(bug, dict):
    return False
  # Release merge requests follow the 6-gate confirm/reassign flow rather than bug-triage
  if is_merge_request_bug(bug) or bool(bug.get("is_security_merge_request")):
    return False
  # Unassigned @Rusino GitHub mentions surface passively without spawning LLM chats
  if bool(bug.get("is_mentioned")):
    return False
  if bug.get("conversation_id") or not bug.get("needs_you"):
    return False
  if str(bug.get("why_needs_you") or "").startswith("All linked CLs merged"):
    return False
  # Open non-WIP linked CLs already have an implementation in review
  if any(
      isinstance(c, dict)
      and str(c.get("status") or "").upper() in ("NEW", "OPEN")
      and not c.get("is_wip")
      for c in (bug.get("linked_cls") or [])
  ):
    return False
  return True


def _extract_fixed_by_gerrit_ids(bug: dict[str, Any]) -> list[str]:
  """Extracts Gerrit CL IDs from custom field 1358989 (`Fixed By Code Changes`)."""
  cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
  raw = str(cf.get(CF_BUG_FIXED_BY_CODE_CHANGES) or "").strip()
  if not raw or raw.upper() == "NA":
    return []
  ids = [m.group(1) for m in RE_GERRIT_ANY_URL_ID.finditer(raw)]
  return list(dict.fromkeys(ids))


def discover_merge_linked_gerrit_ids(bugs: dict[str, Any]) -> list[str]:
  """Collects trunk fix CLs and cherry-pick CLs referenced by open `[Merge M<NN>]` issues."""
  if not isinstance(bugs, dict):
    return []
  gids: list[str] = []
  for bug in bugs.values():
    if not isinstance(bug, dict) or not is_merge_request_bug(bug):
      continue
    st = str(bug.get("status") or "").upper()
    if st not in ("NEW", "ASSIGNED", "ACCEPTED", "OPEN"):
      continue
    child_ids = [
        *(str(x) for x in (bug.get("gerrit_cl_ids") or []) if str(x).isdigit()),
        *(str(x) for x in (bug.get("comment_cl_ids") or []) if str(x).isdigit()),
    ]
    gids.extend(child_ids)
    pid = str(bug.get("parent_issue_id") or "").strip()
    parent = bugs.get(f"buganizer:{pid}") if pid else None
    if isinstance(parent, dict):
      fixed_ids = _extract_fixed_by_gerrit_ids(parent)
      if fixed_ids:
        gids.extend(fixed_ids)
      elif not child_ids:
        gids.extend(
            str(x) for x in (parent.get("gerrit_cl_ids") or []) if str(x).isdigit()
        )
  return list(dict.fromkeys(gids))


def _milestone_from_cl_record(rec: dict[str, Any]) -> int | None:
  """Extracts milestone integer from `chrome/m<NN>` branch or `[M<NN>]` subject."""
  m_br = RE_CHROME_MILESTONE_BRANCH.search(str(rec.get("branch") or ""))
  if m_br:
    return int(m_br.group(1))
  m_subj = re.match(
      r"^\[M(\d{2,4})\]", str(rec.get("subject") or "").strip(), re.IGNORECASE
  )
  return int(m_subj.group(1)) if m_subj else None


def attach_merge_children_to_cls_and_bugs(
    bugs: dict[str, Any], items: dict[str, Any]
) -> None:
  """Detects cherry-pick CLs and attaches sorted `merge_children` to parent CLs and bugs."""
  by_ikey: dict[str, list[dict[str, Any]]] = {}
  sha_to_ikey: dict[str, str] = {}
  for dkey, rec in (items or {}).items():
    if not isinstance(rec, dict):
      continue
    rec.pop("merge_children", None)
    rec["has_active_merges"] = False
    ikey = str(rec.get("item_key") or dkey.split("@", 1)[0]).strip()
    if not ikey:
      continue
    by_ikey.setdefault(ikey, []).append(rec)
    # Map trunk commit SHAs back to canonical item_key for `(cherry picked from commit ...)`
    if ikey.startswith("gerrit:") and not str(rec.get("cherry_pick_of_change") or "").strip():
      br_low = str(rec.get("branch") or "").strip().lower()
      if br_low in ("", "main", "master"):
        rev_sha = str(rec.get("revision") or "").strip().lower()
        if rev_sha:
          sha_to_ikey[rev_sha] = ikey
        for rsha in (rec.get("revision_ps_by_sha") or {}).keys():
          if rsha:
            sha_to_ikey.setdefault(str(rsha).lower(), ikey)

  for bug in (bugs or {}).values():
    if isinstance(bug, dict):
      bug.pop("merge_children", None)
      bug["has_active_merges"] = False

  # Mark cherry-pick CLs via Gerrit metadata or `(cherry picked from commit <sha>)` trailer
  cp_by_parent_and_ms: dict[tuple[str, int], dict[str, Any]] = {}
  for ikey, recs in by_ikey.items():
    sample = recs[0]
    parent_ikey = ""
    cp_change = str(sample.get("cherry_pick_of_change") or "").strip()
    if cp_change:
      parent_ikey = f"gerrit:{cp_change}"
    else:
      m_cp = RE_CHERRY_PICKED_FROM.search(str(sample.get("commit_message") or ""))
      if m_cp:
        src_sha = m_cp.group(1).lower()
        parent_ikey = sha_to_ikey.get(src_sha, "")
        if not parent_ikey:
          for full_sha, cand_ik in sha_to_ikey.items():
            if full_sha.startswith(src_sha) or src_sha.startswith(full_sha):
              parent_ikey = cand_ik
              break
    is_cp = bool(parent_ikey or cp_change or sample.get("is_cherry_pick"))
    if is_cp:
      ms_cl = _milestone_from_cl_record(sample)
      for r in recs:
        r["is_cherry_pick"] = True
        if parent_ikey:
          r["cherry_pick_of"] = parent_ikey
        elif r.get("cherry_pick_of"):
          parent_ikey = str(r["cherry_pick_of"])
      if parent_ikey and ms_cl is not None:
        cp_by_parent_and_ms[(parent_ikey, ms_cl)] = sample

  # Group merge request bugs by parent_issue_id (or standalone bug_key when unparented)
  merge_groups: dict[str, list[dict[str, Any]]] = {}
  for bkey, bug in (bugs or {}).items():
    if not isinstance(bug, dict) or not is_merge_request_bug(bug):
      continue
    pid = str(bug.get("parent_issue_id") or "").strip()
    gk = pid if pid else f"solo:{bkey}"
    merge_groups.setdefault(gk, []).append(bug)

  covered_cp_ikeys: set[str] = set()
  for gk, group_bugs in merge_groups.items():
    parent_id = "" if gk.startswith("solo:") else gk
    parent_bug = (bugs or {}).get(f"buganizer:{parent_id}") if parent_id else None
    parent_ikeys: list[str] = []
    if isinstance(parent_bug, dict):
      parent_ikeys.extend(f"gerrit:{gid}" for gid in _extract_fixed_by_gerrit_ids(parent_bug))
    for mb in group_bugs:
      for gid in [*(mb.get("gerrit_cl_ids") or []), *(mb.get("comment_cl_ids") or [])]:
        cand_ik = f"gerrit:{gid}"
        cand_recs = by_ikey.get(cand_ik) or []
        if cand_recs and cand_recs[0].get("is_cherry_pick") and cand_recs[0].get("cherry_pick_of"):
          parent_ikeys.append(str(cand_recs[0]["cherry_pick_of"]))
    if not parent_ikeys:
      for mb in group_bugs:
        for gid in mb.get("gerrit_cl_ids") or []:
          cand_ik = f"gerrit:{gid}"
          cand_recs = by_ikey.get(cand_ik) or []
          if not cand_recs or not cand_recs[0].get("is_cherry_pick"):
            parent_ikeys.append(cand_ik)
    parent_ikeys = list(dict.fromkeys(parent_ikeys))
    parent_tids = {ik.split(":", 1)[-1] for ik in parent_ikeys}

    children: list[dict[str, Any]] = []
    sorted_group_bugs = sorted(
        group_bugs,
        key=lambda b: (_bug_merge_milestone(b) or 9999, str(b.get("id") or "")),
    )
    for mb in sorted_group_bugs:
      ms_val = _bug_merge_milestone(mb) or 0
      bkey = str(mb.get("bug_key") or f"buganizer:{mb.get('id')}")
      cf = mb.get("custom_fields") if isinstance(mb.get("custom_fields"), dict) else {}
      merge_status = str(cf.get(CF_BUG_MERGE) or mb.get("merge_status") or "").strip()

      cp_rec: dict[str, Any] | None = None
      cp_ikey = ""
      for gid in [*(mb.get("gerrit_cl_ids") or []), *(mb.get("comment_cl_ids") or [])]:
        if str(gid) in parent_tids:
          continue
        cand_ik = f"gerrit:{gid}"
        if cand_ik in by_ikey:
          cp_rec = by_ikey[cand_ik][0]
          cp_ikey = cand_ik
          break
        if not cp_ikey:
          cp_ikey = cand_ik
      if cp_rec is None and ms_val:
        for pik in parent_ikeys:
          matched = cp_by_parent_and_ms.get((pik, ms_val))
          if matched is not None:
            cp_rec = matched
            cp_ikey = str(matched.get("item_key") or "")
            break

      if cp_rec is not None:
        covered_cp_ikeys.add(cp_ikey)
        tid = str(cp_rec.get("target_id") or cp_ikey.split(":", 1)[-1])
        cl_label = f"CL {tid}"
        cl_url = str(cp_rec.get("url") or f"https://{GERRIT_SKIA_HOST}/c/skia/+/{tid}")
        cl_status = str(cp_rec.get("status") or "NEW").upper()
        cl_summary = _summarize_cl_for_bug_subtask(cp_rec)
        cl_needs_you = bool(
            cp_rec.get("in_my_attention")
            or (cp_rec.get("merge_conflict") and cp_rec.get("is_self"))
            or str(cp_rec.get("phase") or "").startswith("ALERT_")
        )
      elif cp_ikey:
        covered_cp_ikeys.add(cp_ikey)
        tid = cp_ikey.split(":", 1)[-1]
        known_urls = {**(mb.get("comment_cl_urls") or {}), **(mb.get("gerrit_cl_urls") or {})}
        cl_label = f"CL {tid}"
        cl_url = str(known_urls.get(tid) or f"https://{GERRIT_SKIA_HOST}/c/skia/+/{tid}")
        cl_status = "LINKED"
        cl_summary = "Linked"
        cl_needs_you = False
      else:
        cl_label = ""
        cl_url = ""
        cl_status = ""
        cl_summary = ""
        cl_needs_you = False

      children.append({
          "milestone": ms_val,
          "milestone_label": f"M{ms_val}" if ms_val else "Merge",
          "bug_key": bkey,
          "bug_short_id": str(mb.get("short_id") or bkey),
          "bug_url": str(mb.get("url") or ""),
          "bug_status": str(mb.get("status") or ""),
          "bug_needs_you": bool(mb.get("needs_you")),
          "merge_status": merge_status,
          "cl_item_key": cp_ikey,
          "cl_label": cl_label,
          "cl_url": cl_url,
          "cl_status": cl_status,
          "cl_summary": cl_summary,
          "cl_needs_you": cl_needs_you,
      })

    active = any(
        str(c.get("bug_status") or "").upper() in ("NEW", "ASSIGNED", "ACCEPTED", "OPEN")
        or str(c.get("cl_status") or "").upper() in ("NEW", "OPEN", "LINKED")
        or bool(c.get("cl_needs_you"))
        for c in children
    )
    if isinstance(parent_bug, dict):
      parent_bug["merge_children"] = list(children)
      parent_bug["has_active_merges"] = active
    for pik in parent_ikeys:
      for p_rec in by_ikey.get(pik, []):
        existing = list(p_rec.get("merge_children") or [])
        existing.extend(children)
        existing.sort(key=lambda c: (int(c.get("milestone") or 9999), str(c.get("bug_key") or "")))
        p_rec["merge_children"] = existing
        p_rec["has_active_merges"] = bool(p_rec.get("has_active_merges") or active)

  # Attach standalone cherry-pick CLs that have no `[Merge Mxx]` child issue
  for ikey, recs in by_ikey.items():
    if ikey in covered_cp_ikeys:
      continue
    sample = recs[0]
    pik = str(sample.get("cherry_pick_of") or "").strip()
    if not sample.get("is_cherry_pick") or not pik or pik not in by_ikey:
      continue
    ms_val = _milestone_from_cl_record(sample) or 0
    tid = str(sample.get("target_id") or ikey.split(":", 1)[-1])
    cl_status = str(sample.get("status") or "NEW").upper()
    cl_needs_you = bool(
        sample.get("in_my_attention")
        or (sample.get("merge_conflict") and sample.get("is_self"))
        or str(sample.get("phase") or "").startswith("ALERT_")
    )
    entry = {
        "milestone": ms_val,
        "milestone_label": f"M{ms_val}" if ms_val else (str(sample.get("branch") or "Cherry-pick")),
        "bug_key": "",
        "bug_short_id": "",
        "bug_url": "",
        "bug_status": "",
        "bug_needs_you": False,
        "merge_status": "",
        "cl_item_key": ikey,
        "cl_label": f"CL {tid}",
        "cl_url": str(sample.get("url") or f"https://{GERRIT_SKIA_HOST}/c/skia/+/{tid}"),
        "cl_status": cl_status,
        "cl_summary": _summarize_cl_for_bug_subtask(sample),
        "cl_needs_you": cl_needs_you,
    }
    active_cp = cl_status in ("NEW", "OPEN") or cl_needs_you
    for p_rec in by_ikey[pik]:
      existing = list(p_rec.get("merge_children") or [])
      existing.append(entry)
      existing.sort(
          key=lambda c: (int(c.get("milestone") or 9999), str(c.get("cl_item_key") or ""))
      )
      p_rec["merge_children"] = existing
      p_rec["has_active_merges"] = bool(p_rec.get("has_active_merges") or active_cp)


def build_buganizer_bulk_query_url(issue_ids: list[str]) -> str:
  """Builds a single Buganizer search URL covering multiple issue IDs for bulk editing."""
  clean = [str(i).strip() for i in issue_ids if str(i).strip().isdigit()]
  if not clean:
    return ""
  joined = "%20OR%20".join(clean)
  return f"https://issues.chromium.org/issues?q=id:({joined})"


def _mcp_mutation_succeeded(output_text: str) -> bool:
  """Returns True when a Buganizer MCP mutation response carries no RPC failure code."""
  txt = (output_text or "").strip()
  if not txt:
    return False
  up = txt.upper()
  return not any(
      code in up
      for code in (
          "FAILED_PRECONDITION",
          "PERMISSION_DENIED",
          "INVALID_ARGUMENT",
          "NOT_FOUND",
          "UNAUTHENTICATED",
          "INTERNAL:",
          "ERROR:",
      )
  )


def reassign_security_merge_batch(
    bugs: dict[str, Any],
    issue_ids: list[str],
    comment_markdown: str = "",
) -> dict[str, Any]:
  """Runs sequential collaborator -> reassign -> read-back MCP batches with bulk-URL fallback."""
  clean_ids = list(
      dict.fromkeys(str(i).strip() for i in issue_ids if str(i).strip().isdigit())
  )
  if not clean_ids:
    return {"ok": False, "error": "No valid issue_ids provided"}

  # Enforce strict standing-rule scope before issuing any Buganizer write call
  for bid in clean_ids:
    bug = (bugs or {}).get(f"buganizer:{bid}")
    if not isinstance(bug, dict):
      return {"ok": False, "error": f"Unknown issue {bid}"}
    cf = bug.get("custom_fields") if isinstance(bug.get("custom_fields"), dict) else {}
    in_scope = (
        str(bug.get("reporter") or "").strip().lower() == SECURITY_MERGE_REPORTER
        and str(bug.get("component_id") or "").strip() == SECURITY_MERGE_COMPONENT_ID
        and extract_merge_milestone(str(bug.get("title") or "")) is not None
        and bool(str(bug.get("parent_issue_id") or "").strip())
        and str(cf.get(CF_BUG_MERGE) or "").strip().lower() == "pending"
        and _is_self_bug_assignee(str(bug.get("assignee") or ""))
    )
    if not in_scope:
      return {
          "ok": False,
          "error": f"Issue {bid} is outside security merge reassign scope",
      }

  # Phase 1: Post comment (if needed) and add jlavrova@google.com to Collaborators
  cmt = (comment_markdown or "").strip()
  phase1_calls: list[tuple[str, dict[str, Any]]] = []
  collab_idx_by_bid: dict[str, int] = {}
  comment_idx_by_bid: dict[str, int] = {}
  for bid in clean_ids:
    bug = bugs[f"buganizer:{bid}"]
    if cmt and not str(bug.get("last_self_comment_at") or "").strip():
      comment_idx_by_bid[bid] = len(phase1_calls)
      phase1_calls.append(
          ("add_buganizer_comment", {"issueId": bid, "commentMarkdown": cmt})
      )
    collab_idx_by_bid[bid] = len(phase1_calls)
    phase1_calls.append((
        "safe_add_collaborator",
        {"issueId": bid, "collaborator": "jlavrova@google.com"},
    ))
  p1_outs = call_buganizer_mcp_batch(phase1_calls)
  now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
  errors: dict[str, str] = {}
  collab_ok_ids: list[str] = []
  for bid in clean_ids:
    c_idx = comment_idx_by_bid.get(bid)
    if c_idx is not None and c_idx < len(p1_outs) and _mcp_mutation_succeeded(p1_outs[c_idx]):
      bugs[f"buganizer:{bid}"]["last_self_comment_at"] = now_iso
    col_idx = collab_idx_by_bid[bid]
    col_out = p1_outs[col_idx] if col_idx < len(p1_outs) else ""
    if _mcp_mutation_succeeded(col_out):
      collab_ok_ids.append(bid)
    else:
      errors[bid] = col_out or "safe_add_collaborator failed"

  # Phase 2: Reassign to merges@chromium.org in a separate MCP session after Collaborator settles
  p2_ok_set: set[str] = set()
  if collab_ok_ids:
    phase2_calls = [
        ("safe_reassign_issue", {"issueId": bid, "assignee": "merges@chromium.org"})
        for bid in collab_ok_ids
    ]
    p2_outs = call_buganizer_mcp_batch(phase2_calls)
    for bid, out in zip(collab_ok_ids, p2_outs):
      if _mcp_mutation_succeeded(out):
        p2_ok_set.add(bid)
      else:
        errors[bid] = out or "safe_reassign_issue failed"

  # Phase 3: Read back live issue state and verify writer access retention
  id_q = " OR ".join(f"id:{bid}" for bid in clean_ids)
  phase3_calls: list[tuple[str, dict[str, Any]]] = [
      (
          "get_bugs_with_external",
          {"query": id_q, "maxResults": max(10, len(clean_ids) * 2), "verbose": True},
      ),
      *[("get_user_access", {"issueId": bid, "relation": "writer"}) for bid in clean_ids],
  ]
  p3_outs = call_buganizer_mcp_batch(phase3_calls)
  parsed_rb = parse_buganizer_blocks(p3_outs[0]) if p3_outs else []
  rb_by_id = {str(b.get("id") or ""): b for b in parsed_rb}

  reassigned_ids: list[str] = []
  failed_ids: list[str] = []
  for idx, bid in enumerate(clean_ids):
    acc_raw = p3_outs[idx + 1] if (idx + 1) < len(p3_outs) else ""
    writer_ok = "true" in acc_raw.lower()
    rb = rb_by_id.get(bid)
    bug = bugs[f"buganizer:{bid}"]
    if isinstance(rb, dict) and rb.get("assignee"):
      bug["assignee"] = rb["assignee"]
    rb_assignee = str((rb or bug).get("assignee") or "").strip().lower()
    if bid in p2_ok_set and rb_assignee == "merges@chromium.org" and writer_ok:
      bug["is_collaborator"] = True
      reassigned_ids.append(bid)
    else:
      failed_ids.append(bid)
      if bid not in errors:
        errors[bid] = (
            f"Read-back mismatch (assignee={rb_assignee or '?'}, writer={writer_ok})"
        )
    classify_bug_status(bug)

  return {
      "ok": True,
      "reassigned_ids": reassigned_ids,
      "failed_ids": failed_ids,
      "bulk_reassign_url": build_buganizer_bulk_query_url(failed_ids),
      "errors": errors,
  }


def reassign_security_merge_for_state(
    issue_ids: list[str],
    parent_id: str = "",
    comment_markdown: str = "",
) -> dict[str, Any]:
  """Executes `reassign_security_merge_batch` against persisted Flight Deck state."""
  state = load_watcher_state()
  bugs = state.setdefault("bugs", {})
  target_ids = [str(i).strip() for i in issue_ids if str(i).strip().isdigit()]
  if not target_ids and parent_id:
    for b in bugs.values():
      if (
          isinstance(b, dict)
          and is_merge_request_bug(b)
          and str(b.get("parent_issue_id") or "").strip() == str(parent_id).strip()
          and _is_self_bug_assignee(str(b.get("assignee") or ""))
      ):
        target_ids.append(str(b.get("id") or ""))
  res = reassign_security_merge_batch(bugs, target_ids, comment_markdown=comment_markdown)
  if res.get("ok"):
    attach_merge_children_to_cls_and_bugs(bugs, state.get("items") or {})
    save_watcher_state(state)
  return res


def _should_retain_bug_in_view(
    bug: dict[str, Any],
    primary_self_bkeys: set[str] | None = None,
) -> bool:
  """Retains a bug in the Bugs tab when owned, collaborated, self-CL-linked, or unacked mention."""
  bkey = str(bug.get("bug_key") or "")
  if primary_self_bkeys is not None and bkey in primary_self_bkeys:
    return True
  if bool(bug.get("is_collaborator")):
    return True
  if _is_self_bug_assignee(str(bug.get("assignee") or "")):
    return True
  if any(
      isinstance(c, dict) and bool(c.get("is_self"))
      for c in (bug.get("linked_cls") or [])
  ):
    return True
  # Non-assigned @mentions stay visible only while unacknowledged or linked to an active chat
  if bool(bug.get("is_mentioned")):
    if str(bug.get("conversation_id") or "").strip() and not bug.get("chat_archived"):
      return True
    human_ts = _parse_ts(bug.get("last_human_comment_at"))
    eff_ack = max(
        _parse_ts(bug.get("last_self_comment_at")),
        float(bug.get("acked_ts") or 0.0),
    )
    return human_ts > eff_ack
  return False


def sync_bugs_into_state(
    state: dict[str, Any],
    *,
    force: bool = False,
    wake_chats: bool = True,
    proj_names: dict[str, str] | None = None,
    skia_dir: str = DEFAULT_SKIA_DIR,
) -> None:
  """Polls Buganizer/crbug and GitHub issues with a 5-min TTL and links them to CLs."""
  pnames = proj_names if proj_names is not None else load_project_names()
  prev_bugs: dict[str, Any] = state.get("bugs") or {}
  bug_acks: dict[str, Any] = state.setdefault("bug_acks", {})
  items: dict[str, Any] = state.setdefault("items", {})
  events: list[dict[str, Any]] = state.setdefault("events", [])
  bug_chats = discover_bug_chats(include_archived=False)
  all_bug_chats = discover_bug_chats(include_archived=True)
  merged_chats = {**all_bug_chats, **bug_chats}

  now_ts = time.time()
  last_bug_ts = float(state.get("last_bug_sync_ts") or 0.0)
  if not force and prev_bugs and (now_ts - last_bug_ts) < BUG_SYNC_TTL_SEC:
    # Refresh chat links, CL subtasks, and prune stale non-self entries between 5-min polls
    link_bugs_and_cls(prev_bugs, items, bug_chats=merged_chats)
    fast_merge_parents = {
        f"buganizer:{str(b.get('parent_issue_id')).strip()}"
        for b in prev_bugs.values()
        if isinstance(b, dict)
        and is_merge_request_bug(b)
        and str(b.get("parent_issue_id") or "").strip().isdigit()
        and _should_retain_bug_in_view(b)
    }
    pruned_fast: dict[str, dict[str, Any]] = {}
    for bk, b in prev_bugs.items():
      if not isinstance(b, dict):
        continue
      if bk in bug_acks:
        b["acked_ts"] = max(float(b.get("acked_ts") or 0.0), float(bug_acks[bk] or 0.0))
      if bk not in fast_merge_parents and not _should_retain_bug_in_view(b):
        continue
      pruned_fast[bk] = b
    for b in pruned_fast.values():
      _resolve_redacted_bug_title(b, pruned_fast)
      classify_bug_status(b, now_ts=now_ts)
    attach_merge_children_to_cls_and_bugs(pruned_fast, items)
    state["bugs"] = pruned_fast
    active_pids = {
        str(b.get("merge_group_parent_id") or "")
        for b in pruned_fast.values()
        if b.get("is_security_merge_request") and b.get("merge_group_parent_id")
    }
    prev_groups = state.get("merge_request_groups")
    if isinstance(prev_groups, dict):
      state["merge_request_groups"] = {
          pid: grp for pid, grp in prev_groups.items() if pid in active_pids
      }
    return

  # Gather external bug IDs referenced by tracked CLs/PRs (for badges and self-CL bugs)
  ref_buganizer_ids: set[str] = set()
  ref_gh_nums: set[str] = set()
  self_cl_bkeys: set[str] = set()
  for rec in items.values():
    if not isinstance(rec, dict):
      continue
    kind = str(rec.get("kind") or "gerrit")
    blob = "\n".join([
        str(rec.get("subject") or ""),
        str(rec.get("commit_message") or ""),
        str(rec.get("topic") or ""),
    ])
    for bkey in [
        *(str(k) for k in (rec.get("linked_bug_keys") or []) if k),
        *extract_bug_refs_from_text(
            blob,
            default_tracker="github" if kind == "github" else "buganizer",
            default_repo=str(rec.get("host") or ""),
        ),
    ]:
      if rec.get("is_self"):
        self_cl_bkeys.add(bkey)
      if bkey.startswith("buganizer:"):
        ref_buganizer_ids.add(bkey.split(":", 1)[1])
      elif bkey.startswith("gh_issue:flutter/flutter#"):
        ref_gh_nums.add(bkey.split("#", 1)[1])

  closed_since = (
      datetime.now(timezone.utc) - timedelta(days=BUG_RECENT_CLOSED_DAYS)
  ).strftime("%Y-%m-%d")
  open_q = (
      "(assignee:jlavrova@google.com OR assignee:jlavrova@chromium.org "
      "OR collaborator:jlavrova@google.com OR collaborator:jlavrova@chromium.org) status:open"
  )
  closed_q = (
      "(assignee:jlavrova@google.com OR assignee:jlavrova@chromium.org "
      "OR collaborator:jlavrova@google.com OR collaborator:jlavrova@chromium.org) "
      f"-status:open modified>={closed_since}"
  )
  mcp_calls: list[tuple[str, dict[str, Any]]] = [
      ("get_bugs_with_external", {"query": open_q, "maxResults": 200, "verbose": True}),
      ("get_bugs_with_external", {"query": closed_q, "maxResults": 50, "verbose": True}),
  ]
  mcp_outputs = call_buganizer_mcp_batch(mcp_calls)
  fetched_bz = parse_buganizer_blocks(mcp_outputs[0]) + parse_buganizer_blocks(
      mcp_outputs[1]
  )
  if not fetched_bz:
    fetched_bz = fetch_buganizer_fallback(open_q, limit=150)

  primary_self_bkeys = {str(b.get("bug_key") or "") for b in fetched_bz}
  seen_bz_ids = {str(b.get("id") or "") for b in fetched_bz}
  # Reconcile previously open Buganizer issues missing from open_q + referenced external bugs
  prev_open_ids = {
      str(b.get("id") or "")
      for b in prev_bugs.values()
      if isinstance(b, dict)
      and b.get("source") == "buganizer"
      and str(b.get("status") or "").upper() in ("NEW", "ASSIGNED", "ACCEPTED", "OPEN")
  }
  parent_bz_ids = {
      str(b.get("parent_issue_id") or "").strip()
      for b in (*fetched_bz, *(v for v in prev_bugs.values() if isinstance(v, dict)))
      if isinstance(b, dict) and str(b.get("parent_issue_id") or "").strip().isdigit()
  }
  missing_bz_ids = sorted(
      (ref_buganizer_ids | prev_open_ids | parent_bz_ids) - seen_bz_ids
  )
  if missing_bz_ids:
    id_q = " OR ".join(f"id:{bid}" for bid in missing_bz_ids[:40])
    extra_out = call_buganizer_mcp_batch(
        [("get_bugs_with_external", {"query": id_q, "maxResults": 50, "verbose": True})]
    )
    fetched_bz.extend(parse_buganizer_blocks(extra_out[0]))

  all_bugs: dict[str, dict[str, Any]] = {}
  render_cands: list[tuple[tuple[int, int, float], str]] = []
  recent_cutoff_ts = now_ts - 30 * 86400
  for raw_b in fetched_bz:
    bkey = raw_b["bug_key"]
    prev_entry = prev_bugs.get(bkey) if isinstance(prev_bugs.get(bkey), dict) else {}
    merged_b = merge_bug_record_with_prev(raw_b, prev_entry)
    # Recompute collaborator role from live query membership so removed/reassigned bugs drop out
    merged_b["is_collaborator"] = bool(
        bkey in primary_self_bkeys
        and not _is_self_bug_assignee(str(merged_b.get("assignee") or ""))
    )
    all_bugs[bkey] = merged_b
    is_self_bug = (
        bkey in primary_self_bkeys
        or bkey in self_cl_bkeys
        or _is_self_bug_assignee(str(merged_b.get("assignee") or ""))
    )
    st = str(merged_b.get("status") or "").upper()
    mod_stamp = _bug_comments_mod_stamp(merged_b)
    is_active_cand = (
        is_self_bug
        and st in ("NEW", "ASSIGNED", "ACCEPTED")
        and (
            bool(merged_b.get("is_collaborator"))
            or str(merged_b.get("priority") or "") in ("P0", "P1")
            or float(merged_b.get("modified_ts") or 0.0) >= recent_cutoff_ts
            or bkey in merged_chats
            or bool(prev_entry.get("needs_you"))
        )
    )
    if (
        is_active_cand
        and mod_stamp
        and mod_stamp != str(merged_b.get("comments_checked_major_mod") or "")
    ):
      # Prioritize currently Needs Me and chatted bugs ahead of passive 30-day activity
      prio_key = (
          1 if prev_entry.get("needs_you") else 0,
          1 if bkey in merged_chats else 0,
          float(merged_b.get("modified_ts") or 0.0),
      )
      render_cands.append((prio_key, merged_b["id"]))

  to_render_ids = [bid for _, bid in sorted(render_cands, reverse=True)[:12]]
  if to_render_ids:
    render_calls = [
        ("render_issue_with_external", {"issueId": bid}) for bid in to_render_ids
    ]
    rendered_outs = call_buganizer_mcp_batch(render_calls)
    for bid, r_out in zip(to_render_ids, rendered_outs):
      b_obj = all_bugs.get(f"buganizer:{bid}")
      if isinstance(b_obj, dict) and r_out:
        cinfo = parse_rendered_issue_comments(r_out)
        b_obj.update(cinfo)
        b_obj["comments_checked_major_mod"] = _bug_comments_mod_stamp(b_obj)

  gh_errors: list[str] = []
  fetched_gh = fetch_github_bugs(
      extra_issue_nums=ref_gh_nums, errors_out=gh_errors
  )
  for gh_b in fetched_gh:
    bkey = gh_b["bug_key"]
    if gh_b.get("from_self_query"):
      primary_self_bkeys.add(bkey)
    merged_gh = merge_bug_record_with_prev(gh_b, prev_bugs.get(bkey))
    merged_gh["is_collaborator"] = False
    merged_gh["is_mentioned"] = bool(gh_b.get("is_mentioned"))
    all_bugs[bkey] = merged_gh
  # Keep cached GitHub bugs across transient or partial gh CLI/auth failures
  if gh_errors:
    for bkey, prev_b in prev_bugs.items():
      if (
          isinstance(prev_b, dict)
          and prev_b.get("source") == "github"
          and bkey not in all_bugs
      ):
        all_bugs[bkey] = dict(prev_b)
        if prev_b.get("from_self_query") or _is_self_bug_assignee(
            str(prev_b.get("assignee") or "")
        ):
          primary_self_bkeys.add(bkey)
  state["bug_sync_errors"] = gh_errors

  # Restore persistent acked_ts watermarks so pruned @mentions do not resurrect on 5-min syncs
  for bkey, bug in all_bugs.items():
    if bkey in bug_acks:
      bug["acked_ts"] = max(
          float(bug.get("acked_ts") or 0.0), float(bug_acks[bkey] or 0.0)
      )

  # Link bugs <-> CLs BEFORE project fallback so self-CL chats propagate project & titles
  link_bugs_and_cls(all_bugs, items, bug_chats=merged_chats)
  for bug in all_bugs.values():
    _resolve_redacted_bug_title(bug, all_bugs)
    if not bug.get("project_name") or bug.get("project_name") == "Unassigned":
      pid, pname = infer_project_for_bug(bug, pnames)
      bug["project_id"] = pid
      bug["project_name"] = pname

  merge_parent_bkeys = {
      f"buganizer:{str(b.get('parent_issue_id')).strip()}"
      for b in all_bugs.values()
      # Keep closed parent bugs visible while at least one child merge request stays in view
      if isinstance(b, dict)
      and is_merge_request_bug(b)
      and str(b.get("parent_issue_id") or "").strip().isdigit()
      and _should_retain_bug_in_view(b, primary_self_bkeys)
  }
  # Keep reviewer-only colleague bugs for CL chips, but exclude them from the Bugs tab
  retained_bugs: dict[str, dict[str, Any]] = {}
  for bkey, bug in all_bugs.items():
    if bkey not in merge_parent_bkeys and not _should_retain_bug_in_view(
        bug, primary_self_bkeys
    ):
      continue
    retained_bugs[bkey] = bug
    classify_bug_status(bug, now_ts=now_ts)

  attach_merge_children_to_cls_and_bugs(retained_bugs, items)
  state["bugs"] = retained_bugs
  state["merge_request_groups"] = evaluate_security_merge_groups(
      retained_bugs,
      skia_dir=skia_dir,
      now_ts=now_ts,
      state=state,
      parent_lookup=all_bugs,
      allow_llm=True,
  )

  review_claims: dict[str, Any] = state.setdefault("review_claims", {})
  busy_cids: set[str] = set()
  # Count unique active reviewer and bug chats toward the 2-chat background concurrency cap
  for d in items.values():
    if not isinstance(d, dict) or not _is_assigned_reviewer_record(d) or d.get("chat_archived"):
      continue
    dcid = str(d.get("conversation_id") or "").strip()
    woken_ts = float(d.get("chat_woken_ts") or 0.0)
    in_grace = bool(woken_ts and now_ts - woken_ts < CHAT_WAKE_GRACE_SEC)
    if dcid and (bool(d.get("chat_busy")) or in_grace):
      busy_cids.add(dcid)
  for b in retained_bugs.values():
    if not isinstance(b, dict) or b.get("chat_archived"):
      continue
    bcid = str(b.get("conversation_id") or "").strip()
    woken_ts = float(b.get("chat_woken_ts") or 0.0)
    in_grace = bool(woken_ts and now_ts - woken_ts < CHAT_WAKE_GRACE_SEC)
    if bcid and (bool(b.get("chat_busy")) or in_grace):
      busy_cids.add(bcid)
  auto_started_bugs = 0

  for bkey, bug in retained_bugs.items():
    # Security merge requests are handled via the grouped merge card, not bug-triage chats
    if bug.get("is_security_merge_request"):
      continue
    prev_b = prev_bugs.get(bkey) if isinstance(prev_bugs.get(bkey), dict) else {}
    # Wake active bug chat when a new human comment arrives upstream
    # Require human_ts > max(self_ts, acked_ts) so self replies or non-comment Needs Me never wake
    unacked_human_comment = _parse_ts(bug.get("last_human_comment_at")) > max(
        _parse_ts(bug.get("last_self_comment_at")),
        float(bug.get("acked_ts") or 0.0),
    )
    if (
        wake_chats
        and bug.get("conversation_id")
        and not bug.get("chat_archived")
        and bug.get("needs_you")
        and bug.get("last_human_comment_at")
        and unacked_human_comment
        and bug.get("last_human_comment_at") != prev_b.get("last_human_comment_at")
        and prev_b
    ):
      wmsg = (
          f"[BUG-WATCHER] 💬 Новый комментарий в {bug.get('short_id')} "
          f"('{bug.get('title')}') от {bug.get('last_human_comment_author')}: "
          f"{bug.get('last_human_comment_snippet')}. Проведи анализ по bug-triage "
          "(~/.gemini/config/skills/bug-triage/SKILL.md), при необходимости обнови/создай "
          "WIP CL (PS1 = тест, PS2 = фикс) и подготовь черновик ответа."
      )
      wcid = str(bug["conversation_id"])
      wake_conversation(wcid, wmsg)
      busy_cids.add(wcid)
      events.insert(
          0,
          {
              "id": f"ev-bug-{int(now_ts * 1000)}-{bug.get('id')}",
              "time": now_et_str(),
              "item_key": bkey,
              "target_id": str(bug.get("id") or ""),
              "conversation_id": wcid,
              "project_name": str(bug.get("project_name") or "Skia"),
              "critical": False,
              "message": wmsg,
          },
      )
      del events[50:]

  # Launch highest-priority unchatted Needs You bugs first (P0 -> P4, newest activity first)
  pri_ranks = {"P0": 0, "P1": 1, "P2": 2, "P3": 3, "P4": 4}
  autostart_cands = [
      (bkey, bug)
      for bkey, bug in retained_bugs.items()
      if wake_chats and should_autostart_bug_triage(bug)
  ]
  autostart_cands.sort(
      key=lambda kv: (
          pri_ranks.get(str(kv[1].get("priority") or "P2").upper(), 2),
          -float(kv[1].get("modified_ts") or _parse_ts(kv[1].get("modified_time")) or 0.0),
          kv[0],
      )
  )
  for bkey, bug in autostart_cands:
    stamp = bug.get("last_human_comment_at") or bug.get("created_time") or "init"
    claim_key = f"bug:{bkey}:{stamp}"
    # Throttle background launches to 1 new bug chat per sync and <2 busy review/bug chats total
    if claim_key not in review_claims and auto_started_bugs < 1 and len(busy_cids) < 2:
      res = _start_chat_for_bug(state, bkey, save=False)
      if res.get("ok") and res.get("started"):
        new_cid = str(res.get("conversation_id") or "")
        review_claims[claim_key] = {"cid": new_cid, "ts": now_ts}
        auto_started_bugs += 1
        if new_cid:
          busy_cids.add(new_cid)
        short_lbl = bug.get("short_id") or bkey
        events.insert(
            0,
            {
                "id": f"ev-bug-start-{int(now_ts * 1000)}-{bug.get('id')}",
                "time": now_et_str(),
                "item_key": bkey,
                "target_id": str(bug.get("id") or ""),
                "conversation_id": new_cid,
                "project_name": str(bug.get("project_name") or "Skia"),
                "critical": False,
                "message": (
                    f"[BUG-WATCHER] 🔍 Auto-started bug triage for {short_lbl} "
                    f"in chat {new_cid[:8]}."
                ),
            },
        )
        del events[50:]

  state["last_bug_sync_ts"] = now_ts


def ack_bug_items(bug_keys: list[str]) -> dict[str, Any]:
  """Marks one or more Needs You bug keys as acknowledged in a single state update."""
  keys = [str(k).strip() for k in bug_keys if str(k).strip()]
  if not keys:
    return {"ok": False, "error": "Missing bug_key"}
  state = load_watcher_state()
  bugs = state.get("bugs") or {}
  now_ts = time.time()
  acked: list[str] = []
  last_lane = "assigned"
  for bkey in keys:
    # Support R70's merge_group:<parent_id> synthetic key alongside explicit bug keys
    if bkey.startswith("merge_group:"):
      pid = bkey.split(":", 1)[1].strip()
      for bug in bugs.values():
        if not isinstance(bug, dict):
          continue
        b_pid = str(
            bug.get("parent_issue_id") or bug.get("merge_group_parent_id") or ""
        ).strip()
        if b_pid == pid:
          bug["acked_ts"] = max(now_ts, float(bug.get("modified_ts") or 0.0))
          classify_bug_status(bug, now_ts=now_ts)
          last_lane = str(bug.get("lane") or "has_cl")
          if bkey not in acked:
            acked.append(bkey)
      continue
    bug = bugs.get(bkey)
    if not isinstance(bug, dict):
      continue
    acked_val = max(
        now_ts,
        float(bug.get("modified_ts") or 0.0),
        _parse_ts(bug.get("last_human_comment_at")),
        float(bug.get("latest_cl_merged_ts") or 0.0),
    )
    bug["acked_ts"] = acked_val
    state.setdefault("bug_acks", {})[bkey] = acked_val
    classify_bug_status(bug, now_ts=now_ts)
    last_lane = str(bug.get("lane") or "assigned")
    if not _should_retain_bug_in_view(bug):
      bugs.pop(bkey, None)
      last_lane = "dismissed"
    acked.append(bkey)
  if not acked:
    return {"ok": False, "error": f"Unknown bug {keys[0]}"}
  attach_merge_children_to_cls_and_bugs(bugs, state.get("items") or {})
  save_watcher_state(state)
  return {"ok": True, "bug_key": acked[0], "bug_keys": acked, "lane": last_lane}


def ack_bug_item(bug_key: str) -> dict[str, Any]:
  """Marks a Needs You bug comment or merged-CL prompt as acknowledged."""
  bkey = str(bug_key or "").strip()
  if not bkey:
    return {"ok": False, "error": "Missing bug_key"}
  return ack_bug_items([bkey])


def sync_once(
    wake_chats: bool = True,
    update_titles: bool = True,
    skia_dir: str = DEFAULT_SKIA_DIR,
    sync_bugs: bool = False,
    force_bugs: bool = False,
) -> dict[str, Any]:
  """Performs one full discovery and status poll across active and unchatted CLs/PRs."""
  # Clear per-poll Gerrit /related caches so status transitions and rebases stay fresh
  _RELATED_CLS_CACHE.clear()
  _RELATED_STACK_ORDER_CACHE.clear()
  _RELATED_RAW_CHANGES_CACHE.clear()

  state = load_watcher_state()
  prev_items: dict[str, Any] = state.get("items") or {}
  events: list[dict[str, Any]] = state.get("events") or []
  review_claims: dict[str, Any] = state.setdefault("review_claims", {})
  github_rerequest_ledger: dict[str, Any] = state.setdefault(
      "github_rerequest_ledger", {}
  )
  auto_started_count = 0

  # Refresh skia_main git refs so merge-base checks are accurate
  if os.path.isdir(skia_dir):
    subprocess.run(
        ["git", "-C", skia_dir, "fetch", "origin", "main", "--quiet"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=10,
        check=False,
    )

  rollers, incidents = fetch_gardener_snapshot()
  proj_names = load_project_names()
  chatted = discover_active_cl_chats()
  live_busy_by_cid = {
      str(c["conversation_id"]): bool(c.get("chat_busy"))
      for c in chatted
      if isinstance(c, dict) and c.get("conversation_id")
  }
  now_sync_ts = time.time()
  busy_review_cids: set[str] = set()
  for d_prev in prev_items.values():
    # Count only active assigned-reviewer chats toward the 2-chat concurrency cap
    if not isinstance(d_prev, dict) or not _is_assigned_reviewer_record(d_prev):
      continue
    prev_cid_chk = str(d_prev.get("conversation_id") or "")
    if not prev_cid_chk:
      continue
    woken_ts = float(d_prev.get("chat_woken_ts") or 0.0)
    # Keep newly woken chats counted during the grace window before LS reports RUNNING
    in_grace = bool(woken_ts and now_sync_ts - woken_ts < CHAT_WAKE_GRACE_SEC)
    is_live_busy = (
        live_busy_by_cid[prev_cid_chk]
        if prev_cid_chk in live_busy_by_cid
        else bool(d_prev.get("chat_busy"))
    )
    if in_grace or is_live_busy:
      busy_review_cids.add(prev_cid_chk)
  busy_review_chats = len(busy_review_cids)
  remote_unchatted = discover_open_remote_changes(
      {d["item_key"] for d in chatted},
      proj_names=proj_names,
  )
  all_chats = discover_active_cl_chats(include_archived=True)
  known_ikeys = {
      str(d.get("item_key") or "") for d in (chatted + remote_unchatted)
  }
  merge_seeded: list[dict[str, Any]] = []
  for mgid in discover_merge_linked_gerrit_ids(state.get("bugs") or {}):
    m_ikey = f"gerrit:{mgid}"
    # Skip CLs already discovered from active chats or open Gerrit queries
    if m_ikey in known_ikeys:
      continue
    known_ikeys.add(m_ikey)
    merge_seeded.append({
        "item_key": m_ikey,
        "kind": "gerrit",
        "target_id": mgid,
        "conversation_id": "",
        "title": f"Gerrit {mgid}",
        "project_id": "",
        "project_name": "Skia",
        "single_target": False,
    })
  attach_archived_chats_to_open_changes(
      remote_unchatted + merge_seeded, all_chats=all_chats
  )
  discovered = chatted + remote_unchatted + merge_seeded

  # Cache fetched CL/PR metadata so multiple chats referencing the same CL don't re-fetch
  fetched_cache: dict[str, dict[str, Any]] = {}
  active_discovered: list[dict[str, Any]] = []
  for d in discovered:
    ikey = d["item_key"]
    tid = d["target_id"]
    if ikey not in fetched_cache:
      if d["kind"] == "gerrit":
        fetched_cache[ikey] = fetch_gerrit_cl_status(
            tid,
            rollers,
            incidents,
            skia_dir=skia_dir,
            preferred_host=str(d.get("preferred_host") or ""),
        )
      elif d["kind"] == "github":
        _, prev_gh_rec = _lookup_state_item(prev_items, ikey)
        raw_gh_repo = str(
            d.get("repo_slug")
            or (prev_gh_rec or {}).get("repo_slug")
            or d.get("preferred_host")
            or "flutter/flutter"
        ).strip()
        if raw_gh_repo.startswith("github.com/"):
          raw_gh_repo = raw_gh_repo[len("github.com/") :]
        gh_repo_slug = raw_gh_repo or "flutter/flutter"
        # Omit default repo_slug keyword so 1-arg mocks on flutter/flutter keep working
        fetched_cache[ikey] = (
            fetch_github_pr_status(tid)
            if gh_repo_slug == "flutter/flutter"
            else fetch_github_pr_status(tid, repo_slug=gh_repo_slug)
        )
      else:
        fetched_cache[ikey] = fetch_piper_cl_status(tid)
    info = fetched_cache[ikey]
    # Drop broken lookups and automated rolls before querying Gmail or building rows
    if not info.get("ok") or is_autoroll_change(info):
      continue
    # Skip unchatted remote changes from which the user already removed themselves
    if not d.get("conversation_id") and info.get("reviewer_removed"):
      continue
    # Re-infer project with full commit message, files, hashtags, and topic for unchatted changes
    if not d.get("conversation_id") and d.get("kind") == "gerrit":
      re_pid, re_pname = infer_project_for_change(
          "gerrit",
          str(info.get("host") or d.get("preferred_host") or GERRIT_SKIA_HOST),
          str(info.get("subject") or d.get("title") or ""),
          proj_names,
          commit_message=str(info.get("commit_message") or ""),
          files=info.get("files") or (),
          hashtags=info.get("hashtags") or (),
          topic=str(info.get("topic") or ""),
      )
      if re_pid and re_pname:
        d["project_id"] = re_pid
        d["project_name"] = re_pname
    # Seed historical breakage and fix linkage from previous items if present
    _, prev_d = _lookup_state_item(prev_items, ikey)
    if prev_d:
      # Carry forward known broken bots when the live record has not re-queried them yet
      if not d.get("broken_bots") and prev_d.get("broken_bots"):
        d["broken_bots"] = list(prev_d["broken_bots"])
      # Preserve existing upstream breakage pointer across sync iterations
      if not d.get("fixes_breakage_from") and prev_d.get("fixes_breakage_from"):
        d["fixes_breakage_from"] = prev_d["fixes_breakage_from"]
      # Preserve matched bot list across sync iterations
      if not d.get("fixes_breakage_bots") and prev_d.get("fixes_breakage_bots"):
        d["fixes_breakage_bots"] = list(prev_d["fixes_breakage_bots"])
    active_discovered.append(d)

  link_companion_changes_to_parent_chats(
      active_discovered, fetched_cache, all_chat_candidates=all_chats
  )
  # Preserve companion_to when a parent merges or closes unless the companion was rebased away
  for d in active_discovered:
    if d.get("companion_to"):
      continue
    ikey = str(d.get("item_key") or "")
    info = fetched_cache.get(ikey) or {}
    if not info.get("is_self"):
      continue
    _, prev_d = _lookup_state_item(prev_items, ikey)
    prev_comp_to = str((prev_d or {}).get("companion_to") or "").strip()
    if not prev_comp_to:
      continue
    if d.get("kind") == "gerrit":
      cur_pc = str(info.get("parent_commit") or "").strip()
      prev_pc = str((prev_d or {}).get("parent_commit") or "").strip()
      if cur_pc and prev_pc and cur_pc != prev_pc:
        continue
    elif d.get("kind") == "github":
      cur_br = str(info.get("base_ref") or "").strip()
      prev_br = str((prev_d or {}).get("base_ref") or "").strip()
      if cur_br and prev_br and cur_br != prev_br:
        continue
    d["companion_to"] = prev_comp_to
    d["is_companion"] = True

  # Fetch status for unchatted or closed companion parents omitted from open discovery
  for d in active_discovered:
    comp_to = str(d.get("companion_to") or "").strip()
    if not comp_to or comp_to in fetched_cache:
      continue
    d_info = fetched_cache.get(str(d.get("item_key") or "")) or {}
    if str(d_info.get("status") or "").upper() not in ("NEW", "OPEN"):
      continue
    comp_kind, _, comp_tid = comp_to.partition(":")
    if not comp_tid:
      continue
    if comp_kind == "gerrit":
      fetched_cache[comp_to] = fetch_gerrit_cl_status(
          comp_tid,
          rollers,
          incidents,
          skia_dir=skia_dir,
          preferred_host=str(
              d.get("preferred_host") or d_info.get("host") or ""
          ),
      )
    elif comp_kind == "github":
      raw_comp_repo = str(
          d.get("repo_slug")
          or d_info.get("repo_slug")
          or d_info.get("host")
          or "flutter/flutter"
      ).strip()
      if raw_comp_repo.startswith("github.com/"):
        raw_comp_repo = raw_comp_repo[len("github.com/") :]
      comp_repo_slug = raw_comp_repo or "flutter/flutter"
      fetched_cache[comp_to] = (
          fetch_github_pr_status(comp_tid)
          if comp_repo_slug == "flutter/flutter"
          else fetch_github_pr_status(comp_tid, repo_slug=comp_repo_slug)
      )
    elif comp_kind == "piper":
      fetched_cache[comp_to] = fetch_piper_cl_status(comp_tid)

  # CQ submits a fresh patchset, so the final revision of a merged CL exists only on main:
  # a CL or stack rooted on it was uploaded onto main and is not a companion of the merged CL
  sha_to_parent: dict[str, str] = {}
  for fk, finfo in fetched_cache.items():
    if not fk.startswith("gerrit:") or not isinstance(finfo, dict):
      continue
    fpsha = str(finfo.get("parent_commit") or "").strip()
    for rsha in (finfo.get("revision_ps_by_sha") or {}).keys():
      if rsha and fpsha:
        sha_to_parent[str(rsha)] = fpsha
  for d in active_discovered:
    comp_to = str(d.get("companion_to") or "").strip()
    if not comp_to.startswith("gerrit:"):
      continue
    d_info = fetched_cache.get(str(d.get("item_key") or "")) or {}
    d_subj = str(d_info.get("subject") or d.get("title") or "").lstrip()
    # Drop stale companion links on SkoreText feature CLs that lack explicit companion markers
    if d_subj.startswith("[SkoreText]") and "companion" not in d_subj.lower():
      for k in ("companion_to", "is_companion", "companion_base_ps_hint"):
        d.pop(k, None)
      continue
    p_info = fetched_cache.get(comp_to) or {}
    if str(p_info.get("status") or "").upper() != "MERGED":
      continue
    p_revs = p_info.get("revision_ps_by_sha") or {}
    if not p_revs:
      continue
    cur_sha = str(d_info.get("parent_commit") or "").strip()
    seen_shas: set[str] = set()
    # Walk across intermediate self-stack commits to find the root revision in the merged parent
    for _ in range(32):
      if not cur_sha or cur_sha in p_revs or cur_sha in seen_shas:
        break
      seen_shas.add(cur_sha)
      cur_sha = sha_to_parent.get(cur_sha, "")
    last_ps = max(int(ps or 0) for ps in p_revs.values())
    base_ps = int(p_revs.get(cur_sha) or d.get("companion_base_ps_hint") or 0)
    # A base on an older patchset was stacked before the merge, so it stays a companion
    if base_ps == last_ps:
      for k in ("companion_to", "is_companion", "companion_base_ps_hint"):
        d.pop(k, None)
  # Match follow-up fix CLs/PRs to broken merged CLs/PRs and share parent chats
  link_fix_changes_to_parent_chats(
      active_discovered, fetched_cache, all_chat_candidates=all_chats
  )

  # Prune unchatted/archived MERGED changes whose rolls are already complete and have no fix links
  fix_targets = {
      str(d.get("fixes_breakage_from"))
      for d in active_discovered
      if d.get("fixes_breakage_from")
  }
  open_companion_parents = {
      str(d.get("companion_to"))
      for d in active_discovered
      if d.get("companion_to")
      and str((fetched_cache.get(d["item_key"]) or {}).get("status") or "").upper()
      in ("NEW", "OPEN")
  }
  merge_retained_ikeys = {
      f"gerrit:{gid}" for gid in discover_merge_linked_gerrit_ids(state.get("bugs") or {})
  }
  open_cherry_pick_parents = {
      f"gerrit:{(fetched_cache.get(d['item_key']) or {}).get('cherry_pick_of_change')}"
      for d in active_discovered
      if (fetched_cache.get(d["item_key"]) or {}).get("cherry_pick_of_change")
      and str((fetched_cache.get(d["item_key"]) or {}).get("status") or "").upper()
      in ("NEW", "OPEN")
  }
  kept_active: list[dict[str, Any]] = []
  for d in active_discovered:
    ikey = d["item_key"]
    info = fetched_cache.get(ikey) or {}
    st = str(info.get("status") or "").upper()
    # Filter completed merged changes that have no active chat, companion, or fix relationship
    if st == "MERGED":
      ps = info.get("post_submit") or {}
      cid = str(d.get("conversation_id") or "")
      is_arch = bool(d.get("chat_archived", False))
      has_active_chat = bool(cid and not is_arch)
      is_fix = bool(d.get("fixes_breakage_from"))
      has_fix = bool(ikey in fix_targets)
      has_open_companion = bool(ikey in open_companion_parents)
      has_active_merge = bool(
          ikey in merge_retained_ikeys or ikey in open_cherry_pick_parents
      )
      # Only Skia commits have post_submit roll tracking; None means nothing is rolling
      rolling = bool(ps and (not ps.get("all_passed") or ps.get("has_alert")))
      _, prev_d = _lookup_state_item(prev_items, ikey)
      just_merged = bool(
          prev_d and str(prev_d.get("status") or "").upper() != "MERGED"
      )
      # Keep merged change if chatted, fixing/fixed, holding an open companion/merge, or rolling
      if not (
          has_active_chat
          or is_fix
          or has_fix
          or has_open_companion
          or has_active_merge
          or rolling
          or just_merged
      ):
        continue
    kept_active.append(d)
  active_discovered = order_and_link_stack_changes(kept_active, fetched_cache)
  active_by_ikey = {str(d.get("item_key") or ""): d for d in active_discovered}

  unique_tids = sorted(
      {d["target_id"] for d in active_discovered if d.get("target_id")}
  )
  gmail_cache: dict[str, dict[str, Any]] = {}
  # Fetch Gmail thread statuses in parallel across unique non-autoroll target IDs
  if unique_tids:
    with ThreadPoolExecutor(max_workers=6) as pool:
      for tid, gstat in zip(unique_tids, pool.map(fetch_gmail_status_for_id, unique_tids)):
        gmail_cache[tid] = gstat

  new_items: dict[str, Any] = {}
  ls_summaries: dict[str, Any] | None = None
  triage_dir = _triage_results_dir()
  preflight_cache: dict[str, Any] | None = None

  for d in active_discovered:
    ikey = d["item_key"]
    cid = str(d.get("conversation_id") or "")
    tid = d["target_id"]
    composite_key = f"{ikey}@{cid}" if cid else f"{ikey}@unchatted"
    info = fetched_cache[ikey]

    gstat = gmail_cache.get(tid) or {
        "thread_ids": [],
        "inbox_thread_ids": [],
        "total_msgs": 0,
        "inbox_msgs": 0,
        "trash_msgs": 0,
        "unread_msgs": 0,
    }

    # Leaving a 1:1 CL review in Gerrit ends our work on it, so archive mail and close the chat
    if should_finalize_removed_reviewer_chat(d, info):
      if gstat.get("inbox_thread_ids"):
        archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
      finalize_conversation(cid)
      events.insert(
          0,
          {
              "id": f"ev-{int(time.time() * 1000)}-{tid}",
              "time": now_et_str(),
              "item_key": ikey,
              "target_id": tid,
              "conversation_id": cid,
              "project_name": d["project_name"],
              "critical": False,
              "message": (
                  f"[CL-WATCHER] 🚫 Removed from reviewers on {ikey} "
                  f"('{info['subject']}') — archived Gmail and finalized chat "
                  f"{cid[:8]}."
              ),
          },
      )
      events = events[:50]
      continue

    record = {
        **d,
        **info,
        "gmail": gstat,
        "composite_key": composite_key,
        "updated_at": now_et_str(),
        "submitted_ts": (
            parse_gerrit_or_iso_time(info.get("submitted") or "")
            or datetime.fromtimestamp(0, timezone.utc)
        ).timestamp(),
        # Sort key for rows in the same status group: newest review or submit event
        "activity_ts": max(
            (
                dt.timestamp()
                for dt in (
                    parse_gerrit_or_iso_time(
                        info.get("status_changed_raw") or ""
                    ),
                    parse_gerrit_or_iso_time(info.get("last_msg_date") or ""),
                    parse_gerrit_or_iso_time(info.get("submitted") or ""),
                )
                if dt
            ),
            default=0.0,
        ),
    }

    prev = prev_items.get(composite_key) or (
        prev_items.get(f"{ikey}@unchatted") if cid else None
    )
    if prev is None:
      _, prev_fb = _lookup_state_item(prev_items, ikey)
      if prev_fb:
        prev_cid = str(prev_fb.get("conversation_id") or "")
        # Keep handoff tail rekeyed by rebase_item when root title holds the CL ID
        if (
            prev_cid
            and prev_cid != cid
            and cid
            and cl.discover_conversation_chain(cid, include_descendants=True)[-1:]
            == [prev_cid]
        ):
          if ls_summaries is None:
            ls_summaries = cl._fetch_ls_summaries_for_audit() or {}
          cid = prev_cid
          composite_key = f"{ikey}@{cid}"
          record["conversation_id"] = cid
          record["composite_key"] = composite_key
          s_tail = ls_summaries.get(cid)
          if isinstance(s_tail, dict):
            ann = s_tail.get("annotations") or {}
            tail_arch = bool(ann.get("archived"))
            tail_title = str(
                ann.get("title")
                or s_tail.get("summary")
                or prev_fb.get("title")
                or ""
            ).strip()
            meta = s_tail.get("trajectoryMetadata") or {}
            record["title"] = tail_title
            record["chat_archived"] = tail_arch
            record["chat_last_modified"] = str(
                meta.get("lastModifiedTime") or meta.get("createdAt") or ""
            )
            record["chat_busy"] = is_chat_busy(
                s_tail, "" if tail_arch else tail_title
            )
          else:
            record["title"] = str(prev_fb.get("title") or "")
            record["chat_archived"] = bool(prev_fb.get("chat_archived", False))
            record["chat_busy"] = bool(prev_fb.get("chat_busy", False))
        prev = prev_fb
    prev = prev or {}
    # Inherit review target and progress only when the chat identity remains unchanged
    if cid and prev.get("conversation_id") == cid:
      if prev.get("review_target_ps"):
        record["review_target_ps"] = prev["review_target_ps"]
      if prev.get("reviewed_ps"):
        record["reviewed_ps"] = prev["reviewed_ps"]
    # Preserve optimistic chat_busy across sync cycles before LS flips the chat to RUNNING
    prev_woken = float(prev.get("chat_woken_ts") or 0.0)
    if (
        cid
        and prev.get("conversation_id") == cid
        and prev_woken
        and time.time() - prev_woken < CHAT_WAKE_GRACE_SEC
    ):
      record["chat_woken_ts"] = prev_woken
      record["chat_busy"] = True

    if cid and _is_assigned_reviewer_record(record) and not record.get("chat_busy"):
      # Throttled children keep their pre-transition patchset floor until woken
      prior_reviewed_floor = (
          prev.get("reviewed_ps")
          or prev.get("pending_wake_from_ps")
          or (
              prev.get("patchset")
              if (prev and prev.get("conversation_id") == cid and "reviewed_ps" not in prev)
              else 0
          )
      )
      record["reviewed_ps"] = max(
          int(record.get("reviewed_ps") or 0),
          int(
              record.get("review_target_ps")
              or prior_reviewed_floor
              or (0 if prev else (record.get("patchset") or 1))
          ),
      )
    if prev.get("audited_msg_date"):
      record["audited_msg_date"] = prev["audited_msg_date"]
    elif not prev:
      record["audited_msg_date"] = str(info.get("last_msg_date") or "")
    post = info.get("post_submit") or {}
    if post:
      # Key post-submit rolls on state changes to track duration in each status bucket
      post_state_key = (
          f"{post.get('skia_status')}:{post.get('passed_rolls')}:"
          f"{post.get('all_passed')}:{post.get('has_alert')}"
      )
      prev_post = (prev.get("post_submit") or {}) if prev else {}
      if (
          prev_post.get("state_key") == post_state_key
          and prev_post.get("status_changed_raw")
      ):
        # Keep earlier transition timestamp while post-submit roll state remains identical
        post_ts_raw = str(prev_post["status_changed_raw"])
      elif (
          not prev_post
          and not post.get("all_passed")
          and not post.get("has_alert")
          and info.get("submitted")
      ):
        # Newly tracked in-flight roll inherits the CL submit timestamp
        post_ts_raw = str(info["submitted"])
      else:
        # First poll witnessing a state transition records the current wall-clock
        post_ts_raw = datetime.now(timezone.utc).isoformat()
      post["state_key"] = post_state_key
      post["status_changed_raw"] = post_ts_raw
      post_et, post_age = format_et_and_age(post_ts_raw)
      post["status_changed_et"] = post_et
      post["status_changed_age"] = post_age

    badge = post.get("compact_badge", "")

    # Keep the broken parent CL's roll badge on a shared chat rather than a companion or fix CL
    if (
        cid
        and update_titles
        and badge
        and not d.get("companion_to")
        and not d.get("fixes_breakage_from")
    ):
      update_chat_roll_badge(cid, str(record.get("title") or ""), badge)

    # Detect significant transitions only when `prev` existed (no spam on initial seed)
    if prev:
      prev_status = prev.get("status")
      prev_ps = prev.get("patchset")
      prev_msg_date = prev.get("last_msg_date")
      prev_phase = prev.get("phase")
      prev_all_passed = (prev.get("post_submit") or {}).get("all_passed")

      transition_msg = ""
      is_critical = False
      already_woken = False

      if prev_status != "MERGED" and info["status"] == "MERGED":
        # Automatically archive Gmail threads from INBOX on merge
        if gstat.get("inbox_thread_ids"):
          archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
          gstat["inbox_msgs"] = 0
          gstat["inbox_thread_ids"] = []
          gstat["inbox_messages"] = []
        transition_msg = (
            f"[CL-WATCHER] ✅ {ikey} ('{info['subject']}') только что замержен "
            f"(ревизия {info['revision'][:10]}). Треды Gmail во входящих (INBOX) для "
            f"{tid} автоматически заархивированы. Сообщи начальный статус Skia Status "
            f"и чеклист отслеживания 4 автороллов."
        )
      elif (
          info["phase"] == "ALERT_ROLL_OR_STATUS"
          and prev_phase != "ALERT_ROLL_OR_STATUS"
      ):
        is_critical = True
        reason = (
            post.get("alert_reason") or "Post-submit regression or roll failure"
        )
        advice = post.get("recommended_action") or ""
        advice_str = f" Рекомендация: {advice}." if advice else ""
        transition_msg = (
            f"[CL-WATCHER] 🔴 CRITICAL ALERT для {ikey} ({info['revision'][:10]}): "
            f"{reason}.{advice_str} Немедленно проверь skia-gardener / status.skia.org и подготовь "
            f"диагностику."
        )
      elif (
          info["phase"] == "ALERT_CQ_FAILED"
          and prev_phase != "ALERT_CQ_FAILED"
          and not info.get("is_draft")
          and not info.get("in_development")
      ):
        # Skip critical wakeup on pre-review changes still in On your desk
        is_critical = True
        ext_note = (
            " (внешний автор, твой аппрув)"
            if info.get("is_external") and not info.get("is_self")
            else ""
        )
        action_hint = (
            "Task Scheduler не смог применить патчсет при gclient.py sync "
            "(нужен ребейз на свежий main)."
            if info.get("cq_sync_rebase_needed")
            else "Проверь причину падения и предложи фикс."
        )
        transition_msg = (
            f"[CL-WATCHER] 🔴 Упал CQ / Presubmit на {ikey}{ext_note} "
            f"(PS{info['patchset']}): {info['last_msg_snippet']}. {action_hint}"
        )
      elif post.get("all_passed") and not prev_all_passed:
        if gstat.get("inbox_thread_ids"):
          archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
          gstat["inbox_msgs"] = 0
          gstat["inbox_thread_ids"] = []
          gstat["inbox_messages"] = []
        transition_msg = (
            f"[CL-WATCHER] 🏁 Все 4 авторолла (Android, Chrome, Google3, Flutter) и "
            f"Skia Status успешно прошли для {ikey} ({info['revision'][:10]})! "
            f"Оставшиеся треды Gmail для {tid} заархивированы из INBOX. "
            f"Спроси пользователя, стоит ли финализировать этот чат."
        )
      elif (
          prev_phase == "ALERT_ROLL_OR_STATUS"
          and info["phase"] != "ALERT_ROLL_OR_STATUS"
      ):
        # An earlier critical wakeup must be withdrawn explicitly, or the chat keeps
        # chasing a culprit the Gardener no longer names. It sits below the all-rolls
        # branch so that branch still archives Gmail when both happen in one sync
        transition_msg = (
            f"[CL-WATCHER] ✅ Алерт снят для {ikey} ({info['revision'][:10]}): "
            f"Skia Gardener не винит этот коммит "
            f"({post.get('skia_status_detail') or 'no matching incident'}). "
            f"Игнорируй предыдущий CRITICAL ALERT."
        )
      elif (
          not _is_assigned_reviewer_record(record)
          and info["status"] in ("NEW", "OPEN")
          and (info["patchset"] != prev_ps or info["last_msg_date"] != prev_msg_date)
          and info["last_msg_author"] not in SELF_EMAILS
          and info["last_msg_author"] not in SELF_GITHUB_LOGINS
          # Ignore SkCQ dry-run bot messages so they do not wake the chat for a reply
          and not info["last_msg_author"].endswith(".gserviceaccount.com")
          and info["in_my_attention"]
      ):
        # Instruct non-self Gerrit follow-ups to save an unpublished draft via gerrit_draft.py
        draft_action = (
            f"сохрани неопубликованный черновик ответа в Gerrit через {GERRIT_SAVE_DRAFT_CMD} "
            "(не публикуй его), покажи черновик в чате "
            if ikey.startswith("gerrit:") and not record.get("is_self")
            else "подготовь черновик ответа "
        )
        transition_msg = (
            f"[CL-WATCHER] 💬 Новое обновление в {ikey} (PS{info['patchset']}) от "
            f"{info['last_msg_author']}: {info['last_msg_snippet']}. "
            f"Проведи инкрементальный триаж/анализ, {draft_action}"
            f"и запиши вердикт через triage_store.py save-review."
        )

      if transition_msg:
        ev = {
            "id": f"ev-{int(time.time() * 1000)}-{tid}",
            "time": now_et_str(),
            "item_key": ikey,
            "target_id": tid,
            "conversation_id": cid,
            "project_name": d["project_name"],
            "critical": is_critical,
            "message": transition_msg,
        }
        events.insert(0, ev)
        events = events[:50]
        if is_critical:
          send_desktop_alert(
              f"CL Alert: {ikey}", transition_msg, info.get("url", "")
          )
        if cid and wake_chats and not already_woken:
          wake_conversation(cid, transition_msg)

    # Route assigned reviewer CLs/PRs through deterministic Tier 0/1/2 pre-gate
    is_reviewer_candidate = (
        _is_assigned_reviewer_record(record)
        and str(info.get("status") or "").upper() in ("NEW", "OPEN")
        and not info.get("is_wip")
        and not info.get("is_draft")
        and bool(info.get("in_my_attention"))
        and not record.get("chat_busy")
        and (not cid or bool(prev))
    )
    if is_reviewer_candidate:
      prev_triage = _load_triage_for_watcher(
          d["kind"], str(tid), triage_dir
      )
      cur_ps = int(info.get("patchset") or 1)
      cur_rev = str(info.get("head_sha") or info.get("revision") or "")
      audited_ps = max(
          int((prev_triage or {}).get("triaged_patchset") or 0),
          int(record.get("reviewed_ps") or 0),
          int(record.get("review_target_ps") or 0),
      )
      audited_rev = str((prev_triage or {}).get("triaged_revision") or "")
      if not audited_rev and prev and int(prev.get("patchset") or 0) == audited_ps:
        audited_rev = str(prev.get("head_sha") or prev.get("revision") or "")
      delta_info = audit_gate.classify_patchset_delta(
          kind=d["kind"],
          host=str(
              info.get("host") or d.get("preferred_host") or GERRIT_SKIA_HOST
          ),
          repo_slug=str(
              info.get("repo_slug")
              or d.get("repo_slug")
              or info.get("project")
              or ("flutter/flutter" if d["kind"] == "github" else "skia")
          ),
          target_id=str(tid),
          prev_ps=audited_ps,
          cur_ps=cur_ps,
          prev_rev=audited_rev,
          cur_rev=cur_rev,
          revisions_by_ps=info.get("revisions_by_ps"),
          changed_files=info.get("files") or (),
          skia_git_dir=skia_dir if d["kind"] == "gerrit" else "",
      )
      last_author = str(info.get("last_msg_author") or "")
      last_date = str(info.get("last_msg_date") or "")
      watermarked_msg_date = str(
          prev.get("audited_msg_date") or prev.get("last_msg_date") or ""
      )
      has_new_ext_msg = bool(
          prev
          and last_date
          and last_date != watermarked_msg_date
          and last_author not in SELF_EMAILS
          and last_author not in SELF_GITHUB_LOGINS
          and not last_author.endswith(".gserviceaccount.com")
      )
      if preflight_cache is None and (
          delta_info["delta_kind"]
          not in ("unchanged", "rebase_only", "commit_msg_only")
          or delta_info.get("upstream_module_overlap")
          or has_new_ext_msg
      ):
        preflight_cache = audit_gate.check_audit_preflight()
      action = audit_gate.decide_audit_action(
          item=record,
          prev_triage=prev_triage,
          delta_info=delta_info,
          has_new_external_msg=has_new_ext_msg,
          msg_snippet=str(info.get("last_msg_snippet") or ""),
          open_external_threads=int(info.get("open_external_threads") or 0),
          preflight=preflight_cache,
      )
      tier = int(action.get("tier", -1))
      if tier == 0:
        triage_store.record_tier0_delta_verdict(
            target_id=str(tid),
            kind=d["kind"],
            patchset=cur_ps,
            revision=cur_rev,
            delta_kind=str(action.get("delta_kind") or "rebase_only"),
            summary=str(action.get("summary") or ""),
            draft=str(action.get("draft") or ""),
            files=info.get("files") or (),
            results_dir=triage_dir,
        )
        record["reviewed_ps"] = max(int(record.get("reviewed_ps") or 0), cur_ps)
        record["review_target_ps"] = max(
            int(record.get("review_target_ps") or 0), cur_ps
        )
        record["audited_msg_date"] = last_date
        if action.get("claim_key"):
          review_claims[str(action["claim_key"])] = {
              "cid": cid or "tier0",
              "ts": time.time(),
          }
      elif action.get("audit_state") == "blocked_auth":
        triage_store.record_audit_status(
            target_id=str(tid),
            kind=d["kind"],
            patchset=cur_ps,
            revision=cur_rev,
            audit_state="blocked_auth",
            audit_error=str(action.get("reason") or "LOAS expired"),
            results_dir=triage_dir,
        )
      elif tier == -1:
        record["audited_msg_date"] = last_date
      elif wake_chats and tier in (1, 2):
        claim_key = str(action.get("claim_key") or f"{ikey}:{cur_ps}")
        if (
            claim_key not in review_claims
            and auto_started_count < 1
            and busy_review_chats < 2
            and not _has_unready_open_stack_parent(
                ikey, active_by_ikey, fetched_cache, new_items, prev_items
            )
        ):
          prompt = str(action.get("prompt") or "")
          if cid:
            woken = wake_conversation(cid, prompt)
            if woken:
              record["review_target_ps"] = cur_ps
              record["chat_busy"] = True
              record["chat_woken_ts"] = time.time()
              record["audited_msg_date"] = last_date
              review_claims[claim_key] = {"cid": cid, "ts": time.time()}
              auto_started_count += 1
              busy_review_chats += 1
              triage_store.record_audit_status(
                  target_id=str(tid),
                  kind=d["kind"],
                  patchset=cur_ps,
                  revision=cur_rev,
                  audit_state="running",
                  results_dir=triage_dir,
              )
              events.insert(
                  0,
                  {
                      "id": f"ev-{int(time.time() * 1000)}-{tid}",
                      "time": now_et_str(),
                      "item_key": ikey,
                      "target_id": tid,
                      "conversation_id": cid,
                      "project_name": d["project_name"],
                      "critical": False,
                      "message": (
                          f"[CL-WATCHER] 🔍 Triggered Tier {tier} audit for "
                          f"{ikey} (PS{cur_ps}) in chat {cid[:8]}."
                      ),
                  },
              )
              events = events[:50]
          else:
            m_tier = str(action.get("model_tier") or "pro")
            # Pass model_tier only when non-default so 5-arg start mocks keep working
            res = (
                _start_pinned_chat_for_record(
                    new_items, ikey, composite_key, record, prompt
                )
                if m_tier == "pro"
                else _start_pinned_chat_for_record(
                    new_items,
                    ikey,
                    composite_key,
                    record,
                    prompt,
                    model_tier=m_tier,
                )
            )
            if res.get("ok"):
              cid = str(res.get("conversation_id") or "")
              composite_key = str(record.get("composite_key") or composite_key)
              record["audited_msg_date"] = last_date
              review_claims[claim_key] = {"cid": cid, "ts": time.time()}
              auto_started_count += 1
              busy_review_chats += 1
              triage_store.record_audit_status(
                  target_id=str(tid),
                  kind=d["kind"],
                  patchset=cur_ps,
                  revision=cur_rev,
                  audit_state="running",
                  results_dir=triage_dir,
              )
              events.insert(
                  0,
                  {
                      "id": f"ev-{int(time.time() * 1000)}-{tid}",
                      "time": now_et_str(),
                      "item_key": ikey,
                      "target_id": tid,
                      "conversation_id": cid,
                      "project_name": d["project_name"],
                      "critical": False,
                      "message": (
                          f"[CL-WATCHER] 🔍 Auto-started code review for {ikey} "
                          f"(PS{cur_ps}) in chat {cid[:8]}."
                      ),
                  },
              )
              events = events[:50]

    # Auto-rerequest GitHub reviewers dropped from reviewRequests after author push
    if d.get("kind") == "github" and record.get("reviewer_pings"):
      rereq_users = auto_rerequest_github_reviewers(
          str(tid),
          str(info.get("head_sha") or info.get("revision") or ""),
          record["reviewer_pings"],
          github_rerequest_ledger,
          do_request=bool(wake_chats),
          repo_slug=str(
              info.get("repo_slug") or d.get("repo_slug") or "flutter/flutter"
          ),
      )
      if rereq_users:
        events.insert(
            0,
            {
                "id": f"ev-{int(time.time() * 1000)}-{tid}-rereq",
                "time": now_et_str(),
                "item_key": ikey,
                "target_id": tid,
                "conversation_id": cid,
                "project_name": d["project_name"],
                "critical": False,
                "message": (
                    f"[CL-WATCHER] 🔄 Auto re-requested GitHub review from "
                    f"{', '.join(rereq_users)} on {ikey} ('{info['subject']}')."
                ),
            },
        )
        events = events[:50]

    sweep_processed_gmail_for_record(record, gstat)
    new_items[composite_key] = record

  # Cross-link companion CLs/PRs and their parent CLs/PRs so the UI can badge both sides
  by_ikey: dict[str, list[dict[str, Any]]] = {}
  for rec in new_items.values():
    if isinstance(rec, dict) and rec.get("item_key"):
      by_ikey.setdefault(str(rec["item_key"]), []).append(rec)

  for rec in new_items.values():
    if not isinstance(rec, dict):
      continue
    comp_to = str(rec.get("companion_to") or "")
    if not comp_to:
      continue
    comp_kind, _, comp_tid = comp_to.partition(":")
    parent_recs = by_ikey.get(comp_to) or []
    parent_url = str(parent_recs[0].get("url") or "") if parent_recs else (
        f"https://github.com/flutter/flutter/pull/{comp_tid}"
        if comp_kind == "github"
        else (
            f"http://cl/{comp_tid}"
            if comp_kind == "piper"
            else f"https://{rec.get('host') or GERRIT_SKIA_HOST}/c/{comp_tid}"
        )
    )
    parent_label = (
        f"PR #{comp_tid}"
        if comp_kind == "github"
        else (f"cl/{comp_tid}" if comp_kind == "piper" else f"CL {comp_tid}")
    )
    rec["companion_to_label"] = parent_label
    rec["companion_to_url"] = parent_url
    comp_self_label = (
        f"PR #{rec.get('target_id')}"
        if rec.get("kind") == "github"
        else (
            f"cl/{rec.get('target_id')}"
            if rec.get("kind") == "piper"
            else f"CL {rec.get('target_id')}"
        )
    )
    comp_entry = {
        "item_key": str(rec.get("item_key") or ""),
        "target_id": str(rec.get("target_id") or ""),
        "label": comp_self_label,
        "url": str(rec.get("url") or ""),
        "is_wip": bool(rec.get("is_wip") or rec.get("is_draft")),
    }
    for p_rec in parent_recs:
      c_list = p_rec.setdefault("companions", [])
      if not any(
          isinstance(x, dict) and x.get("item_key") == comp_entry["item_key"]
          for x in c_list
      ):
        c_list.append(comp_entry)

  # Suppress noise, calculate ready-to-abandon triggers, and deduplicate companions
  compute_companion_states(
      new_items, skia_dir=skia_dir, parent_fallback_cache=fetched_cache
  )

  for rec in new_items.values():
    if not isinstance(rec, dict):
      continue
    # Promote post-submit blamed bots to the top-level record for UI banners
    if not rec.get("broken_bots"):
      rec_bots = _collect_broken_bots_from_item(rec)
      if rec_bots:
        rec["broken_bots"] = rec_bots
    fix_from = str(rec.get("fixes_breakage_from") or "")
    if not fix_from:
      continue
    brk_kind, _, brk_tid = fix_from.partition(":")
    parent_recs = by_ikey.get(fix_from) or []
    parent_url = (
        str(parent_recs[0].get("url") or "")
        if parent_recs
        else (
            f"https://github.com/flutter/flutter/pull/{brk_tid}"
            if brk_kind == "github"
            else (
                f"http://cl/{brk_tid}"
                if brk_kind == "piper"
                else f"https://{rec.get('host') or GERRIT_SKIA_HOST}/c/{brk_tid}"
            )
        )
    )
    parent_label = (
        f"PR #{brk_tid}"
        if brk_kind == "github"
        else (
            f"cl/{brk_tid}" if brk_kind == "piper" else f"Gerrit {brk_tid}"
        )
    )
    rec["fixes_breakage_label"] = parent_label
    rec["fixes_breakage_url"] = parent_url
    merged_bots: list[dict[str, str]] = []
    seen_mb: set[str] = set()
    parent_bots = (
        _collect_broken_bots_from_item(parent_recs[0]) if parent_recs else []
    )
    # Combine live Gardener blamed bots on the broken CL with bots named in the fix commit
    for b in parent_bots + list(rec.get("fixes_breakage_bots") or []):
      if isinstance(b, dict) and b.get("task_name"):
        bn = str(b["task_name"])
        if bn not in seen_mb:
          seen_mb.add(bn)
          merged_bots.append(b)
    rec["fixes_breakage_bots"] = merged_bots

    fix_self_label = (
        f"PR #{rec.get('target_id')}"
        if rec.get("kind") == "github"
        else (
            f"cl/{rec.get('target_id')}"
            if rec.get("kind") == "piper"
            else f"Gerrit {rec.get('target_id')}"
        )
    )
    fix_entry = _build_fix_entry(rec, fix_self_label)
    # Attach the fix summary to every record representing the broken parent CL
    for p_rec in parent_recs:
      f_list = p_rec.setdefault("fixes", [])
      # Deduplicate when multiple composite keys share the same fix CL
      if not any(
          isinstance(x, dict) and x.get("item_key") == fix_entry["item_key"]
          for x in f_list
      ):
        f_list.append(fix_entry)
      # Backfill broken_bots on the parent when bots came from the fix CL description
      if merged_bots and not p_rec.get("broken_bots"):
        p_rec["broken_bots"] = list(merged_bots)

  state["items"] = new_items
  state["review_claims"] = review_claims
  state["github_rerequest_ledger"] = github_rerequest_ledger
  state["events"] = events
  # Automatically abandon companions whose parent closed, received newer CR+1, or was superseded
  if wake_chats:
    auto_abandon_keys: list[str] = []
    for rec in new_items.values():
      if (
          isinstance(rec, dict)
          and rec.get("is_companion")
          and rec.get("is_self")
          and str(rec.get("status") or "").upper() in ("NEW", "OPEN")
          and rec.get("ready_to_abandon")
          and rec.get("auto_abandon_eligible")
      ):
        ikey = str(rec.get("item_key") or "").strip()
        if ikey and ikey not in auto_abandon_keys:
          auto_abandon_keys.append(ikey)
    if auto_abandon_keys:
      abandon_companion_items(auto_abandon_keys, state=state, auto=True)
      events = state["events"]

  triage_store.overlay_triage_on_items(
      new_items.values(),
      results_dir=triage_dir,
      ingest_tmp=(triage_dir is None),
  )
  if sync_bugs:
    sync_bugs_into_state(
        state,
        force=force_bugs,
        wake_chats=wake_chats,
        proj_names=proj_names,
        skia_dir=skia_dir,
    )
    present_ikeys = {
        str(r.get("item_key") or "")
        for r in new_items.values()
        if isinstance(r, dict)
    }
    backfilled = False
    for mgid in discover_merge_linked_gerrit_ids(state.get("bugs") or {}):
      m_ikey = f"gerrit:{mgid}"
      if m_ikey in present_ikeys:
        continue
      # Fetch status immediately when sync_bugs discovers a merge-linked CL mid-poll
      m_info = fetch_gerrit_cl_status(
          mgid, rollers, incidents, skia_dir=skia_dir
      )
      if not m_info.get("ok") or is_autoroll_change(m_info):
        continue
      present_ikeys.add(m_ikey)
      backfilled = True
      new_items[f"{m_ikey}@unchatted"] = {
          "item_key": m_ikey,
          "kind": "gerrit",
          "target_id": mgid,
          "conversation_id": "",
          "title": f"Gerrit {mgid}",
          "project_id": "",
          "project_name": "Skia",
          "single_target": False,
          **m_info,
          "composite_key": f"{m_ikey}@unchatted",
          "updated_at": now_et_str(),
          "submitted_ts": (
              parse_gerrit_or_iso_time(m_info.get("submitted") or "")
              or datetime.fromtimestamp(0, timezone.utc)
          ).timestamp(),
          "activity_ts": (
              parse_gerrit_or_iso_time(m_info.get("status_changed_raw") or "")
              or datetime.fromtimestamp(0, timezone.utc)
          ).timestamp(),
      }
    # Recompute bug<->CL and merge_children cross-links once backfilled CLs join new_items
    if backfilled:
      link_bugs_and_cls(state["bugs"], new_items)
      for b in state["bugs"].values():
        if isinstance(b, dict):
          classify_bug_status(b)
      attach_merge_children_to_cls_and_bugs(state["bugs"], new_items)
  elif state.get("bugs"):
    # Keep in-memory bug<->CL cross-links synchronized when running fast CL-only syncs
    link_bugs_and_cls(state["bugs"], new_items)
    for b in state["bugs"].values():
      if isinstance(b, dict):
        classify_bug_status(b)
    attach_merge_children_to_cls_and_bugs(state["bugs"], new_items)
  state["last_sync"] = now_et_str()
  state["gardener_url"] = gardener_pen_url()
  state["rollers_summary"] = {
      k: {
          "last_roll_rev": v.get("last_roll_rev") or "",
          "current_roll_rev": v.get("current_roll_rev") or "",
          "url": v.get("url") or "",
          "num_failed": v.get("num_failed", 0),
          "num_behind": v.get("num_behind", 0),
          "badge": (v.get("diagnosis") or {}).get("fault_badge", ""),
      }
      for k, v in rollers.items()
      if k in ("Android", "Chrome", "Google3", "Flutter")
  }
  # Overlay real-time chat busy states right before saving the snapshot
  overlay_live_chat_busy(state)
  save_watcher_state(state)
  return state


DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flight Deck</title>
<style>
  :root {
    --paper: #fbfbfd;
    --card: #ffffff;
    --ink: #1d1d1f;
    --graphite: #86868b;
    --hairline: #e5e5ea;
    --blue: #0071e3;
    --green: #34c759;
    /* Burnt amber achieves >=4.5:1 WCAG contrast on light #fbfbfd and #f4f4f6 surfaces */
    --amber: #b45309;
    --red: #ff3b30;
    --red-wash: #fff2f1;
    --shadow: 0 1px 2px rgba(0,0,0,.04), 0 8px 28px rgba(0,0,0,.06);
    --sans: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", "Helvetica Neue",
      sans-serif;
    --display: -apple-system, BlinkMacSystemFont, "SF Pro Display", "Inter", "Helvetica Neue",
      sans-serif;
    --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, monospace;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --paper: #000000;
      --card: #111113;
      --ink: #f5f5f7;
      --graphite: #8e8e93;
      --hairline: #2c2c2e;
      --blue: #2997ff;
      --amber: #fbbf24;
      --red-wash: #2a0f0d;
      --shadow: 0 0 0 1px #1c1c1e;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    background: var(--paper);
    color: var(--ink);
    font: 11.5px/1.3 var(--sans);
    font-variant-numeric: tabular-nums;
  }
  a { color: inherit; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 20px; }

  .bar {
    position: sticky; top: 0; z-index: 5;
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    background: color-mix(in srgb, var(--paper) 78%, transparent);
    border-bottom: 1px solid var(--hairline);
  }
  .bar .wrap { display: flex; align-items: center; gap: 16px; height: 38px; }
  .brand { font: 600 12.5px var(--display); letter-spacing: -.01em; }
  .rollers { display: flex; gap: 12px; margin-left: auto; font-size: 11px; color: var(--graphite); }
  .rollers a { display: inline-flex; align-items: center; gap: 5px; }
  .rollers a:hover { color: var(--ink); text-decoration: none; }
  .synced { font-size: 11px; color: var(--graphite); white-space: nowrap; }
  .link-btn {
    background: none; border: 0; padding: 0; color: var(--blue); font: 11px var(--sans);
    cursor: pointer;
  }

  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
  .ok { background: var(--green); }
  .wait { background: transparent; box-shadow: inset 0 0 0 1.5px var(--graphite); }
  .moving { background: var(--amber); animation: pulse 1.6s ease-in-out infinite; }
  /* Scope to .dot so .fix-stage-head.warn keeps its transparent background */
  .dot.warn { background: var(--amber); }
  .bad { background: var(--red); }
  @keyframes pulse { 50% { opacity: .35; } }

  .projects { display: flex; flex-wrap: wrap; gap: 4px; padding-top: 10px; }
  .proj {
    border: 0; background: transparent; color: var(--graphite); cursor: pointer;
    font: 500 11px var(--sans); padding: 3px 8px; border-radius: 980px;
  }
  .proj:hover { color: var(--ink); }
  .proj .n { margin-left: 3px; opacity: .7; }
  .proj.on { background: var(--ink); color: var(--paper); }

  .hero { padding: 18px 0 12px; }
  .hero h1 {
    font: 700 18px/1.15 var(--display);
    letter-spacing: -.02em;
    max-width: 28ch;
    text-wrap: balance;
  }
  .hero h1 .soft { color: var(--graphite); }
  .hero p { margin-top: 4px; font-size: 11.5px; color: var(--graphite); letter-spacing: -.005em; }
  /* Collapse top margin when secondary counts live in the .sec-tabs bar instead of #subline */
  .hero p:empty { display: none; }

  .focus { display: grid; gap: 10px; }
  .card {
    background: var(--card);
    border-radius: 12px;
    box-shadow: var(--shadow);
    padding: 12px 14px;
    display: grid; grid-template-columns: 1fr auto; gap: 4px 14px; align-items: start;
  }
  .card.alert { background: var(--red-wash); }
  .eyebrow { font-size: 10.5px; font-weight: 600; color: var(--graphite); }
  .eyebrow .busy-pill {
    display: inline-flex; align-items: center; gap: 4px; color: var(--amber);
  }
  .eyebrow .verdict-pill {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 1px 6px; border-radius: 980px; font-size: 10px; font-weight: 600;
  }
  .eyebrow .verdict-pill.v-clean {
    background: color-mix(in srgb, var(--green) 16%, transparent); color: var(--green);
  }
  .eyebrow .verdict-pill.v-nits, .eyebrow .verdict-pill.v-companion_tests {
    background: color-mix(in srgb, var(--blue) 15%, transparent); color: var(--blue);
  }
  .eyebrow .verdict-pill.v-companion_fix, .eyebrow .verdict-pill.v-corpus_blocked {
    background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber);
  }
  .eyebrow .verdict-pill.v-pushback, .eyebrow .verdict-pill.v-corpus_failed {
    background: color-mix(in srgb, var(--red) 14%, transparent); color: var(--red);
  }
  .eyebrow .verdict-pill.v-reassign, .eyebrow .verdict-pill.v-stale {
    background: color-mix(in srgb, var(--ink) 8%, transparent); color: var(--graphite);
  }
  .eyebrow .verdict-pill.v-auditing {
    background: color-mix(in srgb, var(--blue) 15%, transparent); color: var(--blue);
  }
  .eyebrow .verdict-pill.v-blocked_auth {
    background: color-mix(in srgb, var(--red) 15%, transparent); color: var(--red);
  }
  .card.alert .eyebrow { color: var(--red); }
  .card h2 { font: 600 13px/1.25 var(--display); letter-spacing: -.01em; margin: 2px 0 3px; }
  .card .why { font-size: 11.5px; line-height: 1.35; color: var(--graphite); }
  .card .why b { color: var(--ink); font-weight: 500; }
  .card .actions { grid-row: 1; grid-column: 2; display: flex; flex-direction: column; gap: 5px; }
  .card .path, .card .incidents { grid-column: 1 / -1; }
  .card .path { margin-top: 10px; }

  .btn {
    display: inline-flex; justify-content: center; align-items: center;
    height: 24px; padding: 0 10px; border-radius: 980px; border: 0; background: none;
    font: 500 11px var(--sans); white-space: nowrap; cursor: pointer;
  }
  .btn:hover { text-decoration: none; }
  .btn.primary { background: var(--blue); color: #fff; }
  .btn.primary:not(:disabled):hover { filter: brightness(1.08); }
  .btn.secondary { background: color-mix(in srgb, var(--ink) 7%, transparent); color: var(--ink); }
  .btn.secondary:not(:disabled):hover {
    background: color-mix(in srgb, var(--ink) 12%, transparent);
  }
  .btn.quiet { height: 20px; color: var(--blue); }
  .btn.quiet:not(:disabled):hover { text-decoration: underline; }
  .btn:disabled, .link-btn:disabled { opacity: .45; cursor: default; }
  a:focus-visible, button:focus-visible, summary:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--blue) 45%, transparent); outline-offset: 2px;
  }

  .path { display: flex; align-items: center; font-size: 10.5px; color: var(--graphite); }
  /* Keep in-CQ roll dots blue so amber stays reserved for unblamed roll warnings */
  .path .dot.moving { background: var(--blue); }
  .stop { display: flex; flex-direction: column; align-items: center; gap: 4px; min-width: 48px; }
  .stop:hover { text-decoration: none; color: var(--ink); }
  .stop .dot { width: 8px; height: 8px; }
  .stop.is-ok span { color: var(--ink); }
  .stop.is-moving span { color: var(--blue); font-weight: 500; }
  .stop.is-warn span { color: var(--amber); font-weight: 600; }
  .stop.is-bad span { color: var(--red); font-weight: 600; }
  .leg {
    height: 2px; background: var(--hairline); margin-bottom: 12px;
    flex: 0 1 120px; min-width: 20px;
  }
  .leg.done { background: var(--green); }
  .leg.broken {
    background: repeating-linear-gradient(90deg, var(--red) 0 6px, transparent 6px 10px);
  }
  .fan { display: flex; gap: 20px; }

  .incidents { margin-top: 8px; display: grid; gap: 4px; font-size: 11px; }
  .incident { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
  .incident .why-line { color: var(--graphite); }
  .chip {
    font: 10.5px var(--mono); padding: 1px 6px; border-radius: 5px;
    background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red);
  }

  section.lane { margin-top: 22px; }
  .lane-head {
    display: flex; align-items: baseline; gap: 8px;
    padding-bottom: 6px; border-bottom: 1px solid var(--hairline);
  }
  .lane-head h3 { font: 600 12.5px var(--display); letter-spacing: -.01em; }
  .lane-head .n { color: var(--graphite); font-size: 11px; }
  .lane-head .hint { margin-left: auto; font-size: 10.5px; color: var(--graphite); }
  /* Single-select switcher bar keeps secondary sections collapsed below Needs you by default */
  .sec-tabs {
    margin-top: 22px; padding-bottom: 8px; border-bottom: 1px solid var(--hairline);
    display: flex; flex-wrap: wrap; align-items: center; gap: 5px;
  }
  .sec-tab {
    border: 1px solid var(--hairline); background: var(--card); color: var(--graphite);
    font: 500 11px var(--sans); padding: 3px 10px; border-radius: 980px; cursor: pointer;
    display: inline-flex; align-items: center; gap: 5px;
  }
  .sec-tab:hover {
    color: var(--ink); border-color: color-mix(in srgb, var(--ink) 20%, transparent);
  }
  .sec-tab.on { background: var(--ink); color: var(--paper); border-color: var(--ink); }
  .sec-tab .n { font-size: 10.5px; opacity: .75; }
  section.lane.sec-panel { margin-top: 12px; }
  .sec-panel[hidden] { display: none !important; }
  .sec-panel .pings-wrap { margin-top: 0; }

  .row {
    display: grid; grid-template-columns: minmax(0, 1fr) 300px 150px;
    gap: 14px; align-items: center;
    padding: 7px 0; border-bottom: 1px solid var(--hairline);
  }
  .title { font-size: 11.5px; font-weight: 500; line-height: 1.3; letter-spacing: -.005em; }
  .meta { font-size: 10.5px; line-height: 1.25; color: var(--graphite); margin-top: 1px; }
  .meta .cl { font-family: var(--mono); font-size: 10px; }
  /* Style linked CL identifiers in lane rows and expanded steps */
  .meta .cl a { color: var(--blue); font-weight: 600; text-decoration: none; }
  .meta .cl a:hover { text-decoration: underline; }
  .meta .proj-tag { font-weight: 600; }
  .meta .chat { color: var(--blue); }
  .state { font-size: 11px; line-height: 1.3; color: var(--graphite); }
  .state b { color: var(--ink); font-weight: 500; }
  .state .red, .why .red { color: var(--red); }
  .state .amber, .why .amber { color: var(--amber); }
  .badge {
    display: inline-flex; align-items: center; gap: 3px;
    font: 600 10px/1.2 var(--sans); letter-spacing: .01em;
    padding: 1px 5px; border-radius: 5px; vertical-align: middle;
    margin-right: 4px; white-space: nowrap;
  }
  .badge.wip {
    background: color-mix(in srgb, var(--amber) 16%, transparent);
    color: var(--amber);
  }
  .badge.companion {
    background: color-mix(in srgb, var(--blue) 14%, transparent);
    color: var(--blue);
  }
  .badge.has-companion {
    background: color-mix(in srgb, var(--ink) 8%, transparent);
    color: var(--graphite);
  }
  /* Badges and inline banners for bidirectional fix-for CL/PR links */
  .badge.fix-for {
    background: color-mix(in srgb, var(--amber) 18%, transparent);
    color: var(--amber);
  }
  .badge.has-fix {
    background: color-mix(in srgb, var(--green) 16%, transparent);
    color: var(--green);
  }
  .badge.self-reviewed {
    background: color-mix(in srgb, var(--blue) 14%, transparent);
    color: var(--blue);
  }
  .badge.self-approved {
    background: color-mix(in srgb, var(--green) 16%, transparent);
    color: var(--green);
  }
  .eyebrow .fix-stage-pill {
    display: inline-flex; align-items: center; padding: 1px 6px; border-radius: 980px;
    font-size: 10px; font-weight: 600;
  }
  .fix-stage-pill.good, .fix-stage-head.good { color: var(--green); }
  .fix-stage-pill.good { background: color-mix(in srgb, var(--green) 15%, transparent); }
  .fix-stage-pill.progress, .fix-stage-head.progress { color: var(--blue); }
  .fix-stage-pill.progress { background: color-mix(in srgb, var(--blue) 14%, transparent); }
  .fix-stage-pill.warn, .fix-stage-head.warn { color: var(--amber); }
  .fix-stage-pill.warn { background: color-mix(in srgb, var(--amber) 18%, transparent); }
  .fix-stage-pill.bad, .fix-stage-head.bad { color: var(--red); }
  .fix-stage-pill.bad { background: color-mix(in srgb, var(--red) 15%, transparent); }
  .fix-primary-status {
    font-size: 11.5px; color: var(--ink); margin-bottom: 2px; line-height: 1.35;
  }
  .fix-primary-status .fix-stage-head { font-weight: 600; }
  .fix-primary-status .fix-detail { color: var(--graphite); font-size: 11px; }
  .fix-primary-status .fix-subj { color: var(--ink); }
  .why-context { font-size: 11px; color: var(--graphite); }
  .fix-banner {
    margin-top: 6px; padding: 5px 8px; border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 4.5%, transparent);
    font-size: 11px; line-height: 1.35; color: var(--ink);
  }
  .row .fix-banner {
    margin-top: 4px; padding: 4px 7px; font-size: 10.5px;
  }
  .badge.warn {
    background: color-mix(in srgb, var(--red) 13%, transparent);
    color: var(--red);
  }
  .badge.triage-mechanical {
    background: color-mix(in srgb, var(--green) 16%, transparent);
    color: var(--green);
  }
  .badge.triage-reassign {
    background: color-mix(in srgb, var(--amber) 16%, transparent);
    color: var(--amber);
  }
  .badge.triage-surgical_audit {
    background: color-mix(in srgb, var(--red) 14%, transparent);
    color: var(--red);
  }
  .badge.triage-pushback {
    background: color-mix(in srgb, var(--amber) 22%, transparent);
    color: var(--amber);
  }
  .badge.triage-stale {
    background: color-mix(in srgb, var(--red) 12%, transparent);
    color: var(--red);
  }
  .badge.triage-clean {
    background: color-mix(in srgb, var(--green) 16%, transparent);
    color: var(--green);
  }
  .badge.triage-nits, .badge.triage-companion_tests {
    background: color-mix(in srgb, var(--blue) 15%, transparent);
    color: var(--blue);
  }
  .badge.triage-companion_fix, .badge.triage-corpus_blocked {
    background: color-mix(in srgb, var(--amber) 18%, transparent);
    color: var(--amber);
  }
  .badge.triage-corpus_failed {
    background: color-mix(in srgb, var(--red) 15%, transparent);
    color: var(--red);
  }
  .badge.triage-auditing {
    background: color-mix(in srgb, var(--blue) 15%, transparent);
    color: var(--blue);
  }
  .badge.triage-blocked_auth {
    background: color-mix(in srgb, var(--red) 15%, transparent);
    color: var(--red);
  }
  .badge.triage-delta {
    background: color-mix(in srgb, var(--ink) 8%, transparent);
    color: var(--graphite);
  }
  .triage-hunks, .triage-threads {
    flex-basis: 100%; display: flex; flex-wrap: wrap; align-items: center;
    gap: 4px 8px; font-size: 10.5px; color: var(--graphite); margin-top: 2px;
  }
  .triage-hunk-chip {
    font-family: var(--mono); font-size: 10px; padding: 1px 6px; border-radius: 5px;
    background: color-mix(in srgb, var(--blue) 10%, transparent); color: var(--blue);
    text-decoration: none;
  }
  .triage-hunk-chip:hover { text-decoration: underline; }
  .triage-modal-section {
    margin-bottom: 10px; padding: 8px 10px; border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 3.5%, transparent); font-size: 11px;
  }
  .triage-modal-section h4 {
    margin: 0 0 4px; font: 600 10.5px var(--sans); color: var(--graphite);
    text-transform: uppercase; letter-spacing: .03em;
  }
  .card .triage-box {
    grid-column: 1 / -1; margin-top: 8px; padding: 6px 10px; border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 4%, transparent);
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px; font-size: 11px;
  }
  .row .triage-box {
    margin-top: 4px; display: flex; flex-wrap: wrap; align-items: center;
    gap: 4px 6px; font-size: 10.5px;
  }
  .triage-time {
    font-family: var(--mono); font-size: 10px; font-weight: 600; color: var(--ink);
  }
  .triage-summary { color: var(--ink); font-weight: 500; }
  .triage-meta { color: var(--graphite); font-size: 10.5px; }
  .triage-actions { display: inline-flex; gap: 5px; margin-left: auto; }
  .triage-btn {
    border: 1px solid var(--hairline); background: var(--card); color: var(--ink);
    font: 500 10.5px var(--sans); padding: 1px 7px; border-radius: 980px; cursor: pointer;
  }
  .triage-btn:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); }
  .triage-btn.primary {
    border-color: color-mix(in srgb, var(--blue) 35%, transparent); color: var(--blue);
  }
  .triage-btn.approve {
    border-color: color-mix(in srgb, var(--green) 45%, transparent);
    background: color-mix(in srgb, var(--green) 12%, transparent);
    color: color-mix(in srgb, var(--green) 72%, var(--ink));
    font-weight: 600;
  }
  .triage-btn.approve:hover {
    background: color-mix(in srgb, var(--green) 20%, transparent);
  }
  .finalize-sec {
    margin-bottom: 12px; padding: 9px 11px; border-radius: 8px;
    border: 1px solid var(--hairline); background: color-mix(in srgb, var(--ink) 2.5%, transparent);
  }
  .finalize-sec h4 {
    font: 600 11px var(--sans); color: var(--graphite); text-transform: uppercase;
    letter-spacing: 0.03em; margin-bottom: 6px;
  }
  .finalize-gates { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 10px; }
  .finalize-item {
    display: flex; align-items: flex-start; gap: 7px; padding: 5px 0;
    border-top: 1px solid var(--hairline); font-size: 11.5px; line-height: 1.38;
  }
  .finalize-item:first-of-type { border-top: 0; padding-top: 2px; }
  .finalize-warn {
    padding: 8px 10px; border-radius: 8px; margin-bottom: 10px;
    background: var(--red-wash); color: var(--red); font-weight: 500;
  }
  .finalize-ok {
    padding: 8px 10px; border-radius: 8px; margin-bottom: 10px;
    background: color-mix(in srgb, var(--green) 14%, transparent); color: var(--ink);
  }
  .finalize-foot {
    display: flex; align-items: center; justify-content: flex-end; gap: 8px; margin-top: 10px;
  }
  .triage-btn.danger, .btn.danger {
    border-color: color-mix(in srgb, var(--red) 45%, transparent);
    background: color-mix(in srgb, var(--red) 12%, transparent);
    color: var(--red);
  }
  .companion-banner {
    margin-top: 6px; padding: 5px 8px; border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 4.5%, transparent);
    font-size: 11px; line-height: 1.35; color: var(--ink);
    display: flex; flex-wrap: wrap; align-items: center; gap: 4px 8px;
  }
  .row .companion-banner {
    margin-top: 4px; padding: 4px 7px; font-size: 10.5px;
  }
  .triage-modal-backdrop {
    position: fixed; inset: 0; z-index: 100;
    background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .triage-modal {
    background: var(--card); color: var(--ink); border-radius: 12px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2); max-width: 760px; width: 100%;
    max-height: 85vh; display: flex; flex-direction: column; overflow: hidden;
  }
  .triage-modal-head {
    padding: 12px 16px; border-bottom: 1px solid var(--hairline);
    display: flex; align-items: center; gap: 10px;
  }
  .triage-modal-head h3 { font: 600 13px var(--display); margin: 0; }
  .triage-modal-close {
    margin-left: auto; background: none; border: 0; font-size: 14px; color: var(--graphite);
    cursor: pointer; padding: 2px 6px; border-radius: 6px;
  }
  .triage-modal-close:hover { color: var(--ink); }
  .triage-modal-tabs {
    display: flex; gap: 4px; padding: 8px 16px 0; border-bottom: 1px solid var(--hairline);
  }
  .triage-tab {
    border: 0; background: none; padding: 5px 10px; font: 500 11px var(--sans);
    color: var(--graphite); cursor: pointer; border-bottom: 2px solid transparent;
  }
  .triage-tab.active { color: var(--blue); border-bottom-color: var(--blue); font-weight: 600; }
  .triage-modal-body {
    padding: 12px 16px; overflow-y: auto; flex: 1; font-size: 11.5px;
  }
  .triage-pre {
    background: color-mix(in srgb, var(--ink) 4%, transparent); border-radius: 8px;
    padding: 10px 12px; font-family: var(--mono); font-size: 11px; line-height: 1.45;
    white-space: pre-wrap; word-break: break-word; user-select: text;
  }
  .when { text-align: right; font-size: 10.5px; color: var(--graphite); }
  /* Inline roll strip uses phrasing layout so it stays valid inside summary spans */
  .path.path-inline {
    display: inline-flex; flex-direction: column; align-items: flex-start;
    gap: 2px; font-size: 10.5px; line-height: 1.3;
  }
  .row .path.path-inline { display: flex; }
  .path-stops {
    display: flex; flex-wrap: wrap; align-items: center; gap: 2px 4px;
  }
  .path.path-inline .stop {
    display: inline-flex; flex-direction: row; align-items: center;
    gap: 3px; min-width: 0; white-space: nowrap;
  }
  .path.path-inline .stop .dot { width: 7px; height: 7px; }
  .path.path-inline .stop:hover span { text-decoration: underline; }
  .path-sep { color: var(--graphite); font-size: 10px; user-select: none; }
  .path-notes {
    display: flex; flex-wrap: wrap; align-items: center; gap: 1px 6px;
    font-size: 10px; line-height: 1.25; color: var(--graphite);
  }
  .path-note.is-moving { color: var(--blue); }
  .path-note.is-warn { color: var(--amber); }
  .path-note.is-bad { color: var(--red); font-weight: 500; }

  .empty-note { padding: 20px 0; color: var(--graphite); font-size: 11.5px; }
  footer { margin: 40px 0 24px; font-size: 10.5px; color: var(--graphite); }

  /* Grouped Reviewer Pings panel */
  .pings-wrap { margin-top: 18px; display: grid; gap: 10px; }
  .pings-head {
    display: flex; align-items: baseline; gap: 8px;
    padding-bottom: 6px; border-bottom: 1px solid var(--hairline);
  }
  .pings-head h3 { font: 600 12.5px var(--display); letter-spacing: -.01em; margin: 0; }
  .pings-head .n { color: var(--graphite); font-size: 11px; }
  .pings-head .hint { margin-left: auto; font-size: 10.5px; color: var(--graphite); }
  .ping-card {
    background: var(--card); border-radius: 10px; box-shadow: var(--shadow);
    padding: 10px 12px; display: grid; gap: 8px;
  }
  .ping-card-top {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }
  .ping-who { font: 600 12px var(--sans); color: var(--ink); }
  .ping-counts { font-size: 10.5px; color: var(--graphite); }
  .ping-actions { margin-left: auto; display: flex; gap: 6px; align-items: center; }
  .ping-list { display: grid; gap: 6px; }
  .ping-row {
    display: flex; align-items: baseline; gap: 4px 6px; font-size: 11.5px;
    padding: 5px 8px; border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 3.5%, transparent);
    flex-wrap: wrap;
  }
  .ping-row.excluded { opacity: .48; }
  .ping-row input[type="checkbox"] {
    margin: 0 2px 0 0; cursor: pointer; accent-color: var(--blue);
    transform: translateY(1px);
  }
  .ping-row .ping-cl-link { color: var(--blue); font-weight: 600; font-family: var(--mono); }
  .ping-row a.ping-subj { color: var(--ink); font-weight: 500; }
  .ping-row a.ping-subj:hover { color: var(--blue); text-decoration: underline; }
  .ping-row .ping-links {
    margin-left: auto; display: inline-flex; align-items: baseline; gap: 8px;
    font: 500 10.5px var(--mono); white-space: nowrap;
  }
  .ping-row .ping-diff { color: var(--blue); }
  .ping-row .ping-why {
    flex: 1 0 100%; padding-left: 18px; font-size: 10.5px; color: var(--graphite);
  }
  .ping-row .ping-why strong { color: var(--ink); font-weight: 600; }
  .badge.ping-rebase {
    background: color-mix(in srgb, var(--blue) 15%, transparent); color: var(--blue);
  }
  .badge.ping-updated {
    background: color-mix(in srgb, var(--amber) 18%, transparent); color: var(--amber);
  }
  .badge.ping-second {
    background: color-mix(in srgb, var(--ink) 9%, transparent); color: var(--ink);
  }
  .badge.ping-off-att {
    background: color-mix(in srgb, var(--red) 13%, transparent); color: var(--red);
  }

  /* Variant C: Visual CL/PR chain grouping and stack container styles */
  .badge.stack-step {
    background: color-mix(in srgb, var(--ink) 8%, transparent);
    color: var(--ink); font-family: var(--mono); font-weight: 600;
  }
  .stack-group {
    border: 1px solid var(--hairline); border-radius: 12px;
    background: color-mix(in srgb, var(--card) 60%, var(--paper));
    padding: 10px 12px; display: grid; gap: 8px; box-shadow: var(--shadow);
  }
  .stack-head {
    display: grid; gap: 6px; padding-bottom: 6px;
    border-bottom: 1px solid var(--hairline);
  }
  .stack-head-top {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  .stack-head-title {
    font: 600 12.5px var(--display); letter-spacing: -.01em; color: var(--ink);
  }
  .stack-head-meta { font-size: 11px; color: var(--graphite); }
  .stack-head-actions {
    margin-left: auto; display: inline-flex; align-items: center; gap: 6px; flex-wrap: wrap;
  }
  .stack-spine {
    display: flex; align-items: center; gap: 4px; overflow-x: auto;
    padding: 2px 0; -webkit-overflow-scrolling: touch;
  }
  .spine-step {
    display: inline-flex; align-items: center; gap: 4px; padding: 2px 7px;
    border-radius: 999px; font: 500 10.5px var(--mono); white-space: nowrap;
    background: color-mix(in srgb, var(--ink) 6%, transparent);
    color: var(--ink); text-decoration: none; border: 1px solid transparent;
  }
  .spine-step:hover { text-decoration: underline; border-color: var(--hairline); }
  .spine-step.spine-you {
    background: color-mix(in srgb, var(--blue) 14%, transparent);
    color: var(--blue); font-weight: 600;
    border-color: color-mix(in srgb, var(--blue) 30%, transparent);
  }
  .spine-step.spine-ok {
    background: color-mix(in srgb, var(--green) 12%, transparent);
    color: var(--green);
  }
  .spine-step.spine-warn {
    background: color-mix(in srgb, var(--amber) 16%, transparent);
    color: var(--amber);
  }
  .spine-step.spine-bad {
    background: color-mix(in srgb, var(--red) 14%, transparent);
    color: var(--red);
  }
  .spine-step.spine-merged {
    background: color-mix(in srgb, var(--purple) 12%, transparent);
    color: var(--purple);
  }
  .spine-arrow {
    font-size: 10px; color: var(--graphite); user-select: none;
  }
  details.stack-step-collapsed {
    border-radius: 8px; padding: 4px 8px;
    background: color-mix(in srgb, var(--ink) 2.5%, transparent);
    border: 1px dashed var(--hairline); font-size: 11.5px;
  }
  details.stack-step-collapsed > summary {
    cursor: pointer; display: flex; align-items: baseline; gap: 8px;
    color: var(--graphite); list-style: none; user-select: none; flex-wrap: wrap;
  }
  details.stack-step-collapsed > summary::-webkit-details-marker { display: none; }
  details.stack-step-collapsed > summary .stack-step-cl {
    font-family: var(--mono); font-weight: 600; color: var(--blue);
    text-decoration: none;
  }
  details.stack-step-collapsed > summary a.stack-step-cl:hover { text-decoration: underline; }
  details.stack-step-collapsed > summary .stack-step-chat a,
  details.stack-step-collapsed > summary .stack-step-chat button {
    color: var(--blue); text-decoration: none;
  }
  details.stack-step-collapsed > summary .stack-step-chat a:hover,
  details.stack-step-collapsed > summary .stack-step-chat button:hover {
    text-decoration: underline;
  }
  details.stack-step-collapsed > summary .stack-step-subj {
    color: var(--ink); font-weight: 500;
  }
  details.stack-step-collapsed > summary .stack-step-hint {
    margin-left: auto; font-size: 10px; color: var(--graphite);
  }
  details.stack-step-collapsed[open] > summary {
    padding-bottom: 6px; border-bottom: 1px solid var(--hairline);
    margin-bottom: 6px;
  }
  details.stack-step-collapsed .stack-step-expanded { padding: 4px 0; }
  .stack-blocker-banner {
    background: color-mix(in srgb, var(--amber) 14%, transparent);
    color: var(--ink); border-left: 3px solid var(--amber);
    padding: 4px 8px; border-radius: 6px; font-size: 11px;
    margin-top: 4px; display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
  }
  .stack-blocker-banner b { color: var(--amber); }
  .stack-blocker-banner a { color: var(--blue); text-decoration: underline; font-weight: 600; }
  .stack-lane-group {
    border-left: 2px solid color-mix(in srgb, var(--blue) 35%, transparent);
    padding-left: 8px; margin: 4px 0; display: grid; gap: 2px;
  }
  .stack-lane-head {
    display: flex; align-items: center; gap: 6px; padding: 3px 0;
    font-size: 10.5px; color: var(--graphite); flex-wrap: wrap;
  }

  @media (max-width: 860px) {
    .hero h1 { font-size: 16px; }
    .card { grid-template-columns: 1fr; padding: 12px; }
    .card .actions { grid-row: auto; grid-column: 1; flex-direction: row; }
    .row { grid-template-columns: 1fr; gap: 4px; }
    .when { text-align: left; }
    .rollers .name { display: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .moving { animation: none; }
  }

  /* Compact layout for embedded drawer mode inside Jetski Web */
  html.embedded { --paper: #f4f4f6; }
  @media (prefers-color-scheme: dark) {
    html.embedded { --paper: #111114; --card: #19191d; }
  }
  html.embedded body { font-size: 11.5px; line-height: 1.3; }
  html.embedded .wrap { padding: 0 12px; max-width: 100%; }
  html.embedded .bar .wrap { height: 34px; gap: 10px; }
  html.embedded .brand { font-size: 12px; }
  html.embedded .rollers { display: none; }
  html.embedded .hero { padding: 12px 0 8px; }
  html.embedded .hero h1 { font-size: 15px; }
  html.embedded .hero p { margin-top: 3px; font-size: 11px; }
  html.embedded .projects { padding-top: 8px; gap: 3px; }
  html.embedded .proj { font-size: 11px; padding: 2px 7px; }
  html.embedded .card { padding: 10px 12px; border-radius: 10px; gap: 4px 12px; }
  html.embedded .card h2 { font-size: 12.5px; }
  html.embedded .card .why { font-size: 11.5px; }
  html.embedded .row { grid-template-columns: 1fr; gap: 2px; padding: 6px 0; }
  html.embedded section.lane { margin-top: 18px; }
  html.embedded section.lane.sec-panel { margin-top: 10px; }
  html.embedded .sec-tabs { margin-top: 14px; gap: 4px; padding-bottom: 6px; }
  html.embedded .sec-tab { font-size: 10.5px; padding: 2px 8px; }
  html.embedded .lane-head h3 { font-size: 12px; }
  html.embedded footer { margin: 28px 0 16px; font-size: 10px; }

  /* Segmented [CLs | Bugs] view switch and bug-CL parent/subtask rows */
  .view-switch {
    display: inline-flex; align-items: center; gap: 2px; padding: 2px;
    border-radius: 980px; background: color-mix(in srgb, var(--ink) 7%, transparent);
  }
  .view-tab {
    border: 0; background: transparent; color: var(--graphite); cursor: pointer;
    font: 600 11px var(--sans); padding: 3px 10px; border-radius: 980px;
    display: inline-flex; align-items: center; gap: 5px;
  }
  .view-tab:hover { color: var(--ink); }
  .view-tab.on {
    background: var(--card); color: var(--ink);
    box-shadow: 0 1px 2px rgba(0,0,0,.08);
  }
  .view-tab .v-count { font-weight: 500; opacity: .75; font-size: 10.5px; }
  .view-tab .v-you {
    display: inline-flex; align-items: center; gap: 3px;
    color: var(--blue); font-weight: 700; font-size: 10.5px;
  }
  .badge.bug-link {
    background: color-mix(in srgb, var(--blue) 12%, transparent);
    color: var(--blue); font-family: var(--mono);
  }
  .badge.pri-high {
    background: color-mix(in srgb, var(--red) 14%, transparent); color: var(--red);
  }
  .badge.pri-med {
    background: color-mix(in srgb, var(--ink) 8%, transparent); color: var(--graphite);
  }
  .bug-subtasks { margin-top: 6px; display: grid; gap: 3px; grid-column: 1 / -1; }
  .row .bug-subtasks { margin-top: 4px; }
  .bug-subtask {
    display: flex; align-items: baseline; gap: 6px; flex-wrap: wrap;
    padding: 3px 8px; border-radius: 7px;
    background: color-mix(in srgb, var(--ink) 3.8%, transparent);
    font-size: 10.5px; color: var(--graphite);
  }
  .bug-subtask .cl-ref { font-family: var(--mono); font-weight: 600; color: var(--blue); }
  .bug-subtask .sub-subj { color: var(--ink); font-weight: 500; }
  details.subtask-more > summary {
    cursor: pointer; list-style: none; font-size: 10.5px; color: var(--blue);
    padding: 2px 8px; user-select: none;
  }
  details.subtask-more > summary::-webkit-details-marker { display: none; }
  details.subtask-more[open] > .subtask-more-body { margin-top: 3px; display: grid; gap: 3px; }
  .merge-gates {
    margin-top: 5px; display: flex; flex-wrap: wrap; gap: 4px; font-size: 10.5px;
  }
  .merge-gate-pill {
    display: inline-flex; align-items: center; gap: 3px; padding: 1px 6px;
    border-radius: 6px; font-weight: 500;
    background: color-mix(in srgb, var(--green) 14%, transparent); color: var(--green);
  }
  .merge-gate-pill.fail {
    background: color-mix(in srgb, var(--red) 14%, transparent); color: var(--red);
  }
  .merge-warn {
    grid-column: 1 / -1; margin-top: 5px; padding: 5px 8px; border-radius: 7px;
    background: color-mix(in srgb, var(--amber) 14%, transparent);
    border-left: 3px solid var(--amber); color: var(--ink); font-size: 11px;
  }
  .merge-blockers {
    grid-column: 1 / -1; margin-top: 6px; padding: 6px 9px; border-radius: 8px;
    background: color-mix(in srgb, var(--red) 11%, transparent);
    border-left: 3px solid var(--red); color: var(--red);
    font-size: 11px; line-height: 1.4; display: grid; gap: 3px;
  }
  .merge-draft-wrap { grid-column: 1 / -1; margin-top: 6px; display: grid; gap: 4px; }
  .merge-draft-label { font-size: 10px; font-weight: 600; color: var(--graphite); }
  .merge-comment-draft {
    width: 100%; border: 1px solid var(--hairline); border-radius: 8px;
    background: color-mix(in srgb, var(--ink) 3%, var(--card)); color: var(--ink);
    padding: 7px 9px; font: 11px/1.4 var(--mono); resize: vertical;
  }
</style>
</head>
<body>
<header class="bar">
  <div class="wrap">
    <div class="brand" style="cursor: pointer;" onclick="window.parent.postMessage({type: 'jetski-flight-deck-close'}, '*')" title="Hide Flight Deck panel">✈️ Flight Deck</div>
    <div class="view-switch" id="view-switch" role="tablist" aria-label="View"></div>
    <nav class="rollers" id="rollers" aria-label="Skia autorollers"></nav>
    <div class="synced" id="synced">Loading…</div>
    <button class="link-btn" id="refresh-btn" onclick="triggerRefresh()">Refresh</button>
  </div>
</header>

<main class="wrap">
  <nav class="projects" id="projects" aria-label="Projects"></nav>
  <div class="hero">
    <h1 id="headline"></h1>
    <p id="subline"></p>
  </div>
  <div class="focus" id="focus"></div>
  <div class="processing" id="processing" style="display: grid; gap: 4px; margin-bottom: 15px;"></div>
  <div id="lanes"></div>
  <footer id="footer"></footer>
</main>
<div id="triage-modal-root"></div>
<div id="ping-modal-root"></div>

<script>
const PRODUCTS = ['Android', 'Chrome', 'Google3', 'Flutter'];
const COUNT_WORDS = ['Nothing', 'One thing', 'Two things', 'Three things', 'Four things',
  'Five things'];
const PROJECT_ORDER = ['SkParagraph', 'SkoreText', 'Skia', 'WebParagraph', 'Skia Gardener', 'TextClusters API'];
const FLUTTER_DASHBOARD_URL = 'https://flutter-dashboard.appspot.com/#/build';
const STATUS_SKIA_URL = 'https://status.skia.org/repo/skia';
// Merged CLs older than this are chats left open after the work ended
const OLD_MERGED_DAYS = 3;

// Bounds /api/status sockets and retries boot quickly before the 60s steady-state interval
const POLL_INTERVAL_MS = 60000;
const FETCH_STATUS_TIMEOUT_MS = 10000;
const INITIAL_RETRY_DELAY_MS = 2500;
const CACHED_STATUS_STORAGE_KEY = 'flight_deck_cached_status_v1';
const OPEN_DETAILS_STORAGE_KEY = 'flight_deck_open_details_v1';
const SCROLL_Y_STORAGE_KEY = 'flight_deck_scroll_y_v1';
let cachedData = null;
// Distinguishes warm storage hydration from live /api/status responses for heartbeats and retries
let hasLiveStatus = false;
// Tracks server script mtime to trigger page reload on daemon re-exec
let cachedUiVersion = null;
// Stores last rendered payload signature so unchanged 60s polls skip innerHTML churn
let lastRenderedDataSig = '';
let pendingDeferredRender = false;
let pendingBootScrollY = 0;
let bootListenersBound = false;
let pollIntervalTimer = 0;
// Serializes overlapping poll and ping fetches and tracks fast boot retries
let fetchInFlight = false;
let initialRetryTimer = 0;
let selectedProject = 'ALL';
let selectedView = 'cls';
// Preserves at most one open secondary section per view across polls and reloads
let selectedSection = {cls: null, bugs: null};
// Retains expanded <details data-details-key> disclosures and armed confirmation buttons
const openDetailsKeys = new Set();
const confirmingButtons = new Set();

function esc(s) {
  return String(s ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function link(url, html, cls, title) {
  const c = cls ? ` class="${cls}"` : '';
  const t = title ? ` title="${esc(title)}"` : '';
  return url ? `<a${c}${t} href="${esc(url)}" target="_blank" rel="noopener">${html}</a>` : html;
}

function shortActor(raw, it) {
  const s = String(raw || '');
  // CQ and Gerrit service accounts all read as one bot
  if (
    s.includes('skcq-be') ||
    s.includes('gerritcodereview') ||
    s.endsWith('.gserviceaccount.com') ||
    s === 'bot'
  ) {
    return 'SkCQ';
  }
  if (it) {
    if (it.last_msg_author_name && s === String(it.last_msg_author || '')) {
      return String(it.last_msg_author_name);
    }
    if (it.owner_name && s === String(it.owner_email || '')) {
      return String(it.owner_name);
    }
  }
  const local = s.split('@')[0];
  if (local.length <= 2 && s.includes('@')) {
    return s;
  }
  return local;
}

// Maps a Gerrit email, GitHub login, or Piper LDAP to the page that profiles that person
function actorUrl(it, raw) {
  const s = String(raw || '');
  if (!s || shortActor(s) === 'SkCQ') return '';
  if (it.kind === 'github') return `https://github.com/${encodeURIComponent(s)}`;
  if (s.endsWith('@google.com')) {
    return `https://moma.corp.google.com/person/${encodeURIComponent(s.split('@')[0])}`;
  }
  if (it.kind === 'piper') return `https://moma.corp.google.com/person/${encodeURIComponent(s)}`;
  if (s.includes('@') && it.host) return `https://${it.host}/q/owner:${encodeURIComponent(s)}`;
  return '';
}

function checksUrl(it) {
  if (it.kind === 'github') return `${it.url}/checks`;
  if (it.kind === 'gerrit') return `${it.url}?tab=checks`;
  return it.url;
}

// App-scheme chat links open only inside the Jetski app; Jetski Web opens from any browser
function chatUrl(it, data) {
  const base = (data && data.jetski_box_url) || '';
  return base && it.conversation_id ? `${base}/c/${encodeURIComponent(it.conversation_id)}` : '';
}

function clLabel(it) {
  if (it.kind === 'github') return `PR ${it.target_id}`;
  if (it.kind === 'piper') return `cl/${it.target_id}`;
  return `CL ${it.target_id}`;
}

// Returns concise display label for the change type across platforms
function clTypeLabel(it) {
  // GitHub pull requests use PR nomenclature
  if (it.kind === 'github') return 'PR';
  // Internal Piper changes use Piper CL label
  if (it.kind === 'piper') return 'Piper CL';
  return 'CL';
}

// The project tag already names the component, so drop the "[component] " prefix
function cleanSubject(s) {
  return String(s || '').replace(/^\[[^\]]+\]\s*/, '');
}

// Returns the explicit WIP or Draft label when an open change is not ready for submit
function wipLabel(it) {
  if (it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') return '';
  if (it.wip_label) return String(it.wip_label);
  if (it.is_wip) return it.kind === 'github' ? 'Draft' : 'WIP';
  if (it.is_draft) return 'Draft';
  return '';
}

function isCompanion(it) {
  return Boolean(it.companion_to);
}

// Returns active companions on a parent item that are ready to abandon
function readyCompanionsOf(it) {
  const comps = Array.isArray(it && it.companions) ? it.companions : [];
  return comps.filter(c =>
    c && c.ready_to_abandon && c.status !== 'ABANDONED' && c.status !== 'CLOSED'
  );
}

function hasReadyCompanion(it) {
  return readyCompanionsOf(it).length > 0;
}

// Filters out standalone companion and cherry-pick rows when their parent CL/PR is in items
function visibleItemsFromData(data) {
  if (!data || !data.items) return [];
  const raw = Object.values(data.items);
  const parentKeys = new Set(
    raw.filter(it => it && !isCompanion(it) && !it.is_cherry_pick)
      .map(it => it.item_key || `${it.kind || 'gerrit'}:${it.target_id}`)
  );
  return raw.filter(it => {
    if (!it) return false;
    if (it.is_cherry_pick && it.cherry_pick_of) {
      if (it.status === 'ABANDONED' || it.status === 'CLOSED') return false;
      // Hide cherry-pick CLs as standalone rows when the parent CL renders a Merges strip
      return !parentKeys.has(String(it.cherry_pick_of));
    }
    if (!isCompanion(it)) return true;
    // Abandoned or closed companions never render as standalone rows
    if (it.status === 'ABANDONED' || it.status === 'CLOSED') return false;
    // Keep open orphan companions visible only when the parent CL/PR is absent
    return !parentKeys.has(String(it.companion_to || ''));
  });
}

function selfReviewBadgeHtml(it) {
  if (!it || it.is_self || isCompanion(it)) return '';
  if (it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') return '';
  const out = [];
  const curPs = Number(it.patchset || 1);
  const revPs = Number(it.self_review_ps || it.self_approval_ps || 0);
  const cmtCount = Number(it.self_comment_count || 0);
  const cmtPart = cmtCount > 0 ? ` (${cmtCount} comment${cmtCount === 1 ? '' : 's'})` : '';
  if (it.self_approved) {
    const psPart = revPs > 0 ? ` (PS${revPs})` : '';
    out.push(`<span class="badge self-approved">✓ You gave CR+1${esc(psPart)}</span>`);
  } else if (it.self_review_state === 'approved') {
    // Approval was reset on a newer patchset without a new vote
    const baseLabel = revPs > 0 ? `✓ Gave CR+1 on PS${revPs}` : '✓ Gave CR+1';
    if (revPs > 0 && curPs > revPs && it.kind === 'gerrit' && it.url) {
      const diffUrl = `${it.url.replace(/\/+$/, '')}/${revPs}..${curPs}`;
      const text = `${esc(baseLabel)} → PS${esc(curPs)} uploaded`;
      out.push(link(diffUrl, text, 'badge self-reviewed', `Open diff PS${revPs}..PS${curPs}`));
    } else {
      const text = (revPs > 0 && curPs > revPs)
        ? `${esc(baseLabel)} → PS${esc(curPs)} uploaded`
        : esc(baseLabel);
      out.push(`<span class="badge self-reviewed">${text}</span>`);
    }
  } else if (it.self_review_state === 'commented' || it.self_review_state === 'changes_requested') {
    const baseLabel = revPs > 0
      ? `💬 Reviewed PS${revPs}${cmtPart}`
      : `💬 Reviewed${cmtPart}`;
    if (revPs > 0 && curPs > revPs && it.kind === 'gerrit' && it.url) {
      const diffUrl = `${it.url.replace(/\/+$/, '')}/${revPs}..${curPs}`;
      const text = `${esc(baseLabel)} → PS${esc(curPs)} uploaded`;
      out.push(link(diffUrl, text, 'badge self-reviewed', `Open diff PS${revPs}..PS${curPs}`));
    } else {
      const text = (revPs > 0 && curPs > revPs)
        ? `${esc(baseLabel)} → PS${esc(curPs)} uploaded`
        : esc(baseLabel);
      out.push(`<span class="badge self-reviewed">${text}</span>`);
    }
  }
  if (it.author_replied_to_me) {
    const tot = Number(it.self_threads_total || 0);
    const res = Number(it.self_threads_resolved || 0);
    const threadPart = tot > 0 ? ` · ${res}/${tot} resolved` : '';
    const tip = it.author_reply_snippet ? `Author reply: ${it.author_reply_snippet}` : 'Author replied to your review comment';
    out.push(`<span class="badge self-reviewed" title="${esc(tip)}">↩ Author replied${esc(threadPart)}</span>`);
  }
  return out.join('');
}

let cachedStackByIkey = new Map();

function itemKeyOf(it) {
  if (!it) return '';
  return String(it.item_key || `${it.kind || 'gerrit'}:${it.target_id || ''}`);
}

// Resolves the topological root item key for a stacked CL/PR chain
function resolveStackRootKey(it, byIkey) {
  if (!it) return '';
  if (it.stack_root_ikey) return String(it.stack_root_ikey);
  const ancs = Array.isArray(it.stack_ancestors) ? it.stack_ancestors : [];
  if (ancs.length && ancs[0]) return String(ancs[0]);
  let cur = it;
  const visited = new Set();
  while (cur && cur.stack_parent_ikey) {
    const pKey = String(cur.stack_parent_ikey);
    if (visited.has(pKey)) break;
    visited.add(pKey);
    const parent = byIkey ? byIkey.get(pKey) : null;
    if (!parent) return pKey;
    cur = parent;
  }
  return itemKeyOf(cur || it);
}

// Sorts items in the same stack bottom-up: root ancestor first, newest child last
function stackSortCompare(a, b) {
  const ia = Number(a._stackPos || a.stack_index || 0);
  const ib = Number(b._stackPos || b.stack_index || 0);
  if (ia > 0 && ib > 0 && ia !== ib) return ia - ib;
  const da = Number(a.stack_depth || (a.stack_ancestors && a.stack_ancestors.length) || 0);
  const db = Number(b.stack_depth || (b.stack_ancestors && b.stack_ancestors.length) || 0);
  if (da !== db) return da - db;
  const ta = Number(a.target_id || 0);
  const tb = Number(b.target_id || 0);
  if (ta && tb && ta !== tb) return ta - tb;
  return itemKeyOf(a).localeCompare(itemKeyOf(b));
}

// Builds stack index and links multi-item chains for grouped visual rendering
function buildStackContext(items) {
  cachedStackByIkey = new Map();
  const list = Array.isArray(items) ? items : [];
  for (const it of list) {
    if (it) cachedStackByIkey.set(itemKeyOf(it), it);
  }

  const byRoot = new Map();
  for (const it of list) {
    if (!it || isCompanion(it)) continue;
    const rKey = resolveStackRootKey(it, cachedStackByIkey);
    if (!byRoot.has(rKey)) byRoot.set(rKey, []);
    byRoot.get(rKey).push(it);
  }

  for (const [rKey, members] of byRoot.entries()) {
    members.sort(stackSortCompare);
    const total = members.length;
    const isMulti = total >= 2;
    for (let i = 0; i < members.length; i++) {
      const it = members[i];
      if (isMulti || Number(it.stack_size || 0) >= 2) {
        it._stackRoot = rKey;
        it._stackPos = it.stack_index || (i + 1);
        it._stackTotal = it.stack_size || total;
        let chainMembers = members;
        if (Array.isArray(it.stack_ikeys) && it.stack_ikeys.length >= 2) {
          const resolved = it.stack_ikeys.map(k => cachedStackByIkey.get(k)).filter(Boolean);
          if (resolved.length >= members.length) chainMembers = resolved;
        }
        it._stackMembers = chainMembers;
      } else {
        it._stackRoot = '';
        it._stackPos = 0;
        it._stackTotal = 1;
        it._stackMembers = [it];
      }
    }
  }
}

function stackStepBadgeHtml(it) {
  if (!it || isCompanion(it)) return '';
  const pos = Number(it._stackPos || it.stack_index || 0);
  const tot = Number(it._stackTotal || it.stack_size || 0);
  if (pos >= 1 && tot >= 2) {
    const tip = `Stack position ${pos} of ${tot}`;
    return `<span class="badge stack-step" title="${esc(tip)}">⛓ ${pos}/${tot}</span>`;
  }
  return '';
}

// Identifies unmerged upstream stack parents that hold descendants from landing or review
function upstreamBlockersOf(it, data) {
  if (!it || isCompanion(it)) return [];
  const ancestors = Array.isArray(it.stack_ancestors) && it.stack_ancestors.length
    ? it.stack_ancestors
    : (it.stack_parent_ikey ? [it.stack_parent_ikey] : []);
  if (!ancestors.length) return [];

  const byIkey = cachedStackByIkey;
  const rawItems = (data && data.items) || {};
  const blockers = [];

  for (const ancKey of ancestors) {
    const anc = byIkey.get(ancKey) || rawItems[ancKey] || null;
    if (!anc) continue;
    const st = String(anc.status || '').toUpperCase();
    if (st === 'MERGED' || st === 'ABANDONED' || st === 'CLOSED') continue;

    const reasons = [];
    if (anc.merge_conflict) reasons.push('conflict');
    if (anc.cq_failed || anc.phase === 'ALERT_CQ_FAILED') reasons.push('CQ failed');

    const tr = anc.triage;
    if (tr && !anc.self_approved) {
      const v = String(tr.review_outcome || tr.verdict || '').toUpperCase();
      const cStat = effectiveCorpusStatus(anc);
      if (v === 'PUSHBACK' || v.includes('PUSHBACK') || v.includes('DO_NOT_LAND')) {
        reasons.push('Pushback');
      } else if (v === 'COMPANION_FIX' || v.includes('FIX')) {
        reasons.push('Companion fix');
      // Unverified or failing corpus on an unapproved parent blocks approving child CLs
      } else if (cStat === 'failed') {
        reasons.push(`${corpusDisplayName(tr)} failed`);
      } else if (cStat === 'pending' && GATED_LGTM_OUTCOMES.has(v)) {
        reasons.push(`${corpusDisplayName(tr)} not run`);
      } else if (v === 'COMPANION_TESTS' || v.includes('TESTS')) {
        reasons.push('Companion tests');
      } else if (v === 'NITS' || v.includes('NITS')) {
        reasons.push('NITS');
      } else if (v === 'SURGICAL_AUDIT' || v.includes('SURGICAL')) {
        reasons.push('Surgical audit');
      }
    }

    const comps = Array.isArray(anc.companions) ? anc.companions : [];
    const openComps = comps.filter(
      c => c && c.status !== 'ABANDONED' && c.status !== 'CLOSED'
    );
    if (openComps.length && !reasons.some(r => r.startsWith('Companion'))) {
      reasons.push('Companion');
    }

    if (!anc.is_self) {
      if (anc.self_review_state === 'changes_requested') {
        reasons.push('Changes requested');
      } else if (anc.self_review_state === 'commented' && !anc.self_approved) {
        if (!reasons.includes('NITS')) reasons.push('Comments');
      }
    }

    if (anc.in_my_attention && !anc.self_approved && !reasons.length) {
      reasons.push('Needs review');
    }

    // Unmerged stack parents missing a 2nd reviewer or +1 still block child landing
    if (Number(anc.missing_reviewers_to_assign || 0) > 0) {
      reasons.push('Needs 2nd reviewer');
    } else if (anc.self_approved && Number(anc.missing_approvals || 0) > 0) {
      reasons.push('Needs 2nd +1');
    }

    if (reasons.length) {
      blockers.push({ancestor: anc, reasons: [...new Set(reasons)]});
    }
  }
  return blockers;
}

// Renders an amber banner warning that an upstream parent holds the stack
function stackBlockerBannerHtml(it, data) {
  const blockers = upstreamBlockersOf(it, data);
  if (!blockers.length) return '';
  const parts = blockers.map(b => {
    const anc = b.ancestor;
    const pos = Number(anc._stackPos || anc.stack_index || 0);
    const tot = Number(anc._stackTotal || anc.stack_size || 0);
    const stepPrefix = (pos >= 1 && tot >= 2) ? `[${pos}/${tot}] ` : '';
    const ancLink = link(anc.url, `${stepPrefix}${esc(clLabel(anc))}`);
    const rStr = b.reasons.join(', ');
    return `${ancLink} (${esc(rStr)})`;
  });
  const joined = parts.join(' & ');
  const tip = 'Updating parent will rebase this CL/PR; review or fix parent first';
  return `<div class="stack-blocker-banner" title="${esc(tip)}">` +
    `⏸️ <b>Parent holds stack:</b> ${joined} — updating parent will rebase this CL</div>`;
}

// Orders suggested 2nd reviewers and promotes anyone already assigned on a stack sibling
function suggestedReviewersFor(it) {
  if (!it || it.sponsor_action !== 'assign_second_reviewer') return [];
  const raw = (it.suggested_second_reviewers || []).filter(c => c && c.email);
  if (raw.length <= 1) return raw;
  const onStack = new Set();
  for (const m of (it._stackMembers || [])) {
    if (!m || m === it) continue;
    for (const r of (m.assigned_reviewers || [])) {
      if (r) onStack.add(String(r).toLowerCase());
    }
    for (const p of (m.reviewer_pings || [])) {
      if (p && p.reviewer) onStack.add(String(p.reviewer).toLowerCase());
    }
    for (const w of (m.waiting_on || [])) {
      if (w && w.id) onStack.add(String(w.id).toLowerCase());
    }
  }
  if (!onStack.size) return raw;
  // Keep stack siblings under one 2nd reviewer when one member already assigned them
  return [
    ...raw.filter(c => onStack.has(String(c.email).toLowerCase())),
    ...raw.filter(c => !onStack.has(String(c.email).toLowerCase())),
  ];
}

// Renders 1-click Gerrit reviewer buttons for a single card or stack batch header
function assignReviewerButtonsHtml(itemKeys, cands, batchTarget) {
  if (!itemKeys.length || !cands.length) return '';
  const keysJson = esc(JSON.stringify(itemKeys));
  return cands.map((c, idx) => {
    const shortName = esc(c.short || c.name || shortActor(c.email));
    const tip = esc(`Add ${c.name || c.email} (${c.email}) on Gerrit`);
    const cls = batchTarget
      ? (idx === 0 ? 'triage-btn primary' : 'triage-btn')
      : (idx === 0 ? 'btn primary' : 'btn secondary');
    const label = batchTarget
      ? `${idx === 0 ? '👤 ' : ''}+ ${shortName} to all ${esc(batchTarget)}`
      : (idx === 0 ? `👤 + ${shortName} (2nd reviewer)` : `+ ${shortName}`);
    return `<button class="${cls}" title="${tip}" ` +
      `onclick="assignReviewer(${keysJson}, ${esc(JSON.stringify(c.email))}, this)">` +
      `${label}</button>`;
  }).join('');
}

// Formats short status text, icon, and tone for a step in the horizontal chain spine
function spineStepShortState(m) {
  if (m.status === 'MERGED') return {short: 'Merged', icon: '✓', tone: 'spine-merged'};
  if (m.status === 'ABANDONED' || m.status === 'CLOSED') {
    return {short: 'Closed', icon: '—', tone: 'spine-step'};
  }
  if (m.cq_failed || m.phase === 'ALERT_CQ_FAILED') {
    return {short: 'CQ fail', icon: '✗', tone: 'spine-bad'};
  }
  // Surface pending sponsor actions before generic CR+1 so missing 2nd reviewers stand out
  if (needsSponsorAction(m)) {
    const act = m.sponsor_action;
    if (act === 'assign_second_reviewer') {
      return {short: 'CR+1 · +2nd rev', icon: '👤', tone: 'spine-you'};
    }
    if (act === 'submit_cq_conflict') {
      return {short: 'CR+1 · rebase', icon: '⚠️', tone: 'spine-warn'};
    }
    return {short: 'Ready CQ+2', icon: '🚀', tone: 'spine-you'};
  }
  if (m.self_approved || m.self_review_state === 'approved') {
    return {short: 'CR+1', icon: '✓', tone: 'spine-ok'};
  }
  if (m.merge_conflict) return {short: 'Conflict', icon: '⚠️', tone: 'spine-warn'};
  if (isPostSubmitAlert(m)) return {short: 'Alert', icon: '🔴', tone: 'spine-bad'};
  if (m.chat_busy) return {short: 'Working', icon: '⏳', tone: 'spine-you'};

  const tr = m.triage;
  // Stale reviews or unverified corpus gates must never mark a stack spine step green
  if (tr && tr.review_outcome && !isTriageStale(m)) {
    const out = String(tr.review_outcome).toUpperCase();
    const cStat = effectiveCorpusStatus(m);
    if (GATED_LGTM_OUTCOMES.has(out) && tr.corpus_required) {
      if (cStat === 'failed') return {short: 'Corpus fail', icon: '🔴', tone: 'spine-bad'};
      if (cStat === 'pending') return {short: 'Corpus needed', icon: '⏳', tone: 'spine-warn'};
      if (cStat === 'waived') return {short: 'Corpus waived', icon: '⚠️', tone: 'spine-warn'};
    }
    if (out === 'CLEAN') return {short: 'Clean', icon: '🟢', tone: 'spine-ok'};
    if (out === 'NITS') return {short: 'NITS', icon: '🔵', tone: 'spine-warn'};
    if (out === 'COMPANION_FIX') return {short: 'Fix', icon: '🟠', tone: 'spine-warn'};
    if (out === 'COMPANION_TESTS') return {short: 'Tests', icon: '🟣', tone: 'spine-warn'};
    if (out === 'PUSHBACK') return {short: 'Pushback', icon: '🔴', tone: 'spine-bad'};
    if (out === 'REASSIGN') return {short: 'Reassign', icon: '⚪', tone: 'spine-step'};
  }

  const lane = laneOf(m);
  if (lane === 'you') return {short: 'Needs you', icon: '⦿', tone: 'spine-you'};
  if (lane === 'landing') return {short: 'Landing', icon: '🛫', tone: 'spine-ok'};
  if (lane === 'desk') {
    return {short: m.is_draft ? 'Draft' : 'Desk', icon: '📝', tone: 'spine-warn'};
  }
  if (m.self_review_state === 'commented') {
    return {short: 'Reviewed', icon: '💬', tone: 'spine-step'};
  }
  return {short: 'Waiting', icon: '⏳', tone: 'spine-step'};
}

// Renders the 1-line interactive horizontal chain spine across all stack members
function stackSpineHtml(members) {
  const list = Array.isArray(members) ? members : [];
  if (list.length < 2) return '';
  const total = list.length;
  const steps = list.map((m, idx) => {
    const pos = Number(m._stackPos || m.stack_index || (idx + 1));
    const tot = Number(m._stackTotal || m.stack_size || total);
    const info = spineStepShortState(m);
    const label = `[${pos}/${tot} ${info.icon} ${esc(info.short)}]`;
    const tip = `${clLabel(m)}: ${cleanSubject(m.subject)} (${info.short})`;
    return link(m.url, label, `spine-step ${info.tone}`, tip);
  });
  return `<div class="stack-spine">${steps.join('<span class="spine-arrow">→</span>')}</div>`;
}

// Groups stacked changes in Needs You into a single container with a spine and collapsed siblings
function renderFocusSection(youItems, shownItems, data) {
  const list = Array.isArray(youItems) ? youItems : [];
  if (!list.length) return '';

  const seenRoots = new Set();
  const htmlParts = [];

  for (const it of list) {
    const rootKey = it._stackRoot;
    const members = it._stackMembers || [];
    // Standalone item (not in a stack of >= 2) renders standard focus card
    if (!rootKey || members.length < 2) {
      htmlParts.push(focusCard(it, data));
      continue;
    }

    if (seenRoots.has(rootKey)) continue;
    seenRoots.add(rootKey);

    const rootItem = members[0] || it;
    const author = authorLabel(rootItem);
    const project = esc(rootItem.project_name || 'Skia');
    const total = members.length;
    const kindBase = rootItem.kind === 'github' ? 'PR' : 'CL';
    const kindPlural = kindBase + (total === 1 ? '' : 's');
    const youMembers = members.filter(m => laneOf(m) === 'you');
    const need2nd = youMembers.filter(
      m => needsSponsorAction(m) && m.sponsor_action === 'assign_second_reviewer'
    );
    const readyCq = youMembers.filter(
      m => needsSponsorAction(m) && String(m.sponsor_action || '').startsWith('submit_cq')
    );
    const otherYou = youMembers.length - need2nd.length - readyCq.length;
    const approved = members.filter(
      m => m.self_approved || m.self_review_state === 'approved'
    ).length;

    // Name sponsor actions explicitly so approved stacks never say '2 need you, 2 approved'
    const n2Label = `${need2nd.length === 1 ? 'needs' : 'need'} 2nd reviewer`;
    let countsRaw;
    if (!otherYou && need2nd.length && !readyCq.length) {
      const p = need2nd.length === approved ? n2Label : `${need2nd.length} ${n2Label}`;
      countsRaw = approved ? `${approved} approved · ${p}` : p;
    } else if (!otherYou && readyCq.length && !need2nd.length) {
      const p = readyCq.length === approved ? 'ready for CQ+2' : `${readyCq.length} ready for CQ+2`;
      countsRaw = approved ? `${approved} approved · ${p}` : p;
    } else {
      const parts = [];
      if (otherYou) parts.push(`${otherYou} need${otherYou === 1 ? 's' : ''} you`);
      if (need2nd.length) parts.push(`${need2nd.length} ${n2Label}`);
      if (readyCq.length) parts.push(`${readyCq.length} ready for CQ+2`);
      if (approved) parts.push(`${approved} approved`);
      countsRaw = parts.join(', ');
    }
    const countsDesc = esc(countsRaw);

    // Offer 1-click batch assignment when >= 2 stack CLs need a 2nd Googler reviewer
    let batchHtml = '';
    if (need2nd.length >= 2) {
      const keys = need2nd.map(itemKeyOf).filter(Boolean);
      const btns = assignReviewerButtonsHtml(
        keys, suggestedReviewersFor(need2nd[0]), `${keys.length} ${kindBase}s`
      );
      if (btns) batchHtml = `<span class="stack-head-actions">${btns}</span>`;
    }

    const headHtml = `
      <div class="stack-head">
        <div class="stack-head-top">
          <span class="stack-head-title">🔗 Stack (${total} ${kindPlural})</span>
          <span class="stack-head-meta">${project} · by ${author} · ${countsDesc}</span>
          ${batchHtml}
        </div>
        ${stackSpineHtml(members)}
      </div>`;

    const memberCards = members.map((m, idx) => {
      const pos = Number(m._stackPos || m.stack_index || (idx + 1));
      const tot = Number(m._stackTotal || m.stack_size || total);
      if (laneOf(m) === 'you') {
        return focusCard(m, data);
      }
      // Stack sibling residing in another lane renders as collapsed 1-line separator
      const stepBadge = `<span class="badge stack-step">⛓ ${pos}/${tot}</span>`;
      // Strip the focus-card trailing separator dot so the pill sits cleanly inside <summary>
      const verdictPill = reviewVerdictEyebrowHtml(m).replace(/\s*·\s*$/, '');
      const clText = esc(clLabel(m));
      // Direct link to host review UI so collapsed siblings can be inspected in one click
      const clLink = m.url
        ? `<a class="stack-step-cl" href="${esc(m.url)}" target="_blank" rel="noopener" ` +
          `onclick="event.stopPropagation()">${clText}</a>`
        : `<span class="stack-step-cl">${clText}</span>`;
      const subjText = esc(cleanSubject(m.subject));
      const stateText = stateHtml(m);
      const chatHtml = chatActionHtml(m, data, true);
      // Surface direct review chat link on collapsed sibling so parent chat is immediately accessible
      const chatSpan = chatHtml
        ? `<span class="stack-step-chat" onclick="event.stopPropagation()">· ${chatHtml}</span>`
        : '';
      const stackDetailsKey = `stack:${itemKeyOf(m)}`;
      const stackOpenAttr = openDetailsKeys.has(stackDetailsKey) ? ' open' : '';
      return `
        <details class="stack-step-collapsed" ` +
          `data-details-key="${esc(stackDetailsKey)}"${stackOpenAttr}>
          <summary>
            ${stepBadge}
            ${verdictPill}
            ${clLink}
            <span class="stack-step-subj">${subjText}</span>
            <span>— ${stateText}</span>
            ${chatSpan}
            <span class="stack-step-hint">click to expand</span>
          </summary>
          <div class="stack-step-expanded">${rowHtml(m, data)}</div>
        </details>`;
    }).join('');

    htmlParts.push(`
      <section class="stack-group">
        ${headHtml}
        ${memberCards}
      </section>`);
  }

  return htmlParts.join('');
}

// Groups stacked changes within status lanes when multiple members appear together
function renderLaneItemsHtml(laneItems, data) {
  const list = Array.isArray(laneItems) ? laneItems : [];
  if (!list.length) return '';

  const stackCounts = new Map();
  for (const it of list) {
    if (it._stackRoot && (it._stackMembers || []).length >= 2) {
      stackCounts.set(it._stackRoot, (stackCounts.get(it._stackRoot) || 0) + 1);
    }
  }

  const seenRoots = new Set();
  const htmlParts = [];

  for (const it of list) {
    const rKey = it._stackRoot;
    const isMultiInLane = rKey && (stackCounts.get(rKey) || 0) >= 2;
    if (!isMultiInLane) {
      htmlParts.push(rowHtml(it, data));
      continue;
    }

    if (seenRoots.has(rKey)) continue;
    seenRoots.add(rKey);

    const membersInLane = list.filter(m => m._stackRoot === rKey).sort(stackSortCompare);
    const fullMembers = it._stackMembers || membersInLane;
    const rootItem = fullMembers[0] || it;
    const author = authorLabel(rootItem);
    const project = esc(rootItem.project_name || 'Skia');
    const total = fullMembers.length;
    const kindPlural = (rootItem.kind === 'github' ? 'PR' : 'CL') + (total === 1 ? '' : 's');

    const headHtml = `
      <div class="stack-lane-head">
        <b>🔗 Stack (${membersInLane.length} of ${total} ${kindPlural})</b> · ` +
        `${project} · by ${author}
      </div>
      ${stackSpineHtml(fullMembers)}`;

    const rows = membersInLane.map(m => rowHtml(m, data)).join('');
    htmlParts.push(`
      <div class="stack-lane-group">
        ${headHtml}
        ${rows}
      </div>`);
  }

  return htmlParts.join('');
}

function badgesHtml(it, opts) {
  const out = [];
  const stepBadge = stackStepBadgeHtml(it);
  if (stepBadge) out.push(stepBadge);
  const wip = wipLabel(it);
  if (wip) out.push(`<span class="badge wip">${esc(wip)}</span>`);
  const selfRev = selfReviewBadgeHtml(it);
  if (selfRev) out.push(selfRev);
  if (isCompanion(it)) {
    const refLabel = it.companion_to_label || String(it.companion_to).replace(/^.*:/, '#');
    const text = `Companion → ${esc(refLabel)}`;
    out.push(it.companion_to_url
      ? link(it.companion_to_url, text, 'badge companion', 'Open parent CL/PR')
      : `<span class="badge companion">${text}</span>`);
  }
  const comps = Array.isArray(it.companions) ? it.companions : [];
  for (const c of comps) {
    if (!c || !c.label) continue;
    const text = `Has companion ${esc(c.label)}${c.is_wip ? ' (WIP)' : ''}`;
    out.push(c.url
      ? link(c.url, text, 'badge has-companion', 'Open companion CL/PR')
      : `<span class="badge has-companion">${text}</span>`);
  }
  // Show bidirectional fix-for badges on both the fix CL and the broken merged CL
  if (it.fixes_breakage_from) {
    const refLabel = it.fixes_breakage_label ||
      String(it.fixes_breakage_from).replace(/^.*:/, 'CL ');
    const text = `Fixes breakage from → ${esc(refLabel)}`;
    out.push(it.fixes_breakage_url
      ? link(it.fixes_breakage_url, text, 'badge fix-for', 'Open broken CL/PR')
      : `<span class="badge fix-for">${text}</span>`);
  }
  if (!opts || !opts.omitFixes) {
    const fixes = Array.isArray(it.fixes) ? it.fixes : [];
    for (const f of fixes) {
      if (!f || !f.label) continue;
      const meta = [f.status_summary, f.age].filter(Boolean).join(', ');
      const text = `fix: ${esc(f.label)}${meta ? ` (${esc(meta)})` : ''}`;
      out.push(f.url
        ? link(f.url, text, 'badge has-fix', f.subject || 'Open fix CL/PR')
        : `<span class="badge has-fix">${text}</span>`);
    }
  }
  return out.join('');
}

// Compacts long Skia bot names to prefix…build-config for inline banners
function compactBotName(name) {
  const s = String(name || '');
  const pat = '^((?:Test|Build|Perf)-[A-Za-z0-9_.]+)-(?:Clang|GCC|MSVC)-.*?' +
    '-((?:Debug|Release)-[A-Za-z0-9_.\\-]+)$';
  const m = s.match(new RegExp(pat));
  if (m) return `${m[1]}…${m[2]}`;
  return shortTask(s);
}

// Formats a list of bot dicts as linked chips with full task_name tooltips
function formatBotsListHtml(bots) {
  const list = Array.isArray(bots) ? bots : [];
  if (!list.length) return 'post-submit bots';
  return list.map(b => {
    const name = typeof b === 'string' ? b : (b.task_name || 'bot');
    const url = (typeof b === 'object' && b) ? (b.task_url || b.swarming_url || '') : '';
    const label = `<span class="chip" title="${esc(name)}">${esc(compactBotName(name))}</span>`;
    return url ? link(url, label) : label;
  }).join(' ');
}

// Renders 'Broke <bots> · fix: <CL> (status, age)' on the broken merged CL
function brokenAndFixBannerHtml(it) {
  const fixes = Array.isArray(it.fixes) ? it.fixes : [];
  const bots = Array.isArray(it.broken_bots) && it.broken_bots.length
    ? it.broken_bots
    : ((it.post_submit && Array.isArray(it.post_submit.broken_bots))
        ? it.post_submit.broken_bots : []);
  if (!fixes.length && !bots.length) return '';
  const parts = [];
  if (bots.length) {
    parts.push(`Broke ${formatBotsListHtml(bots)}`);
  }
  for (const f of fixes) {
    if (!f || !f.label) continue;
    const meta = [f.status_summary, f.age].filter(Boolean).join(', ');
    const fLink = f.url
      ? link(f.url, `<b>${esc(f.label)}</b>`, '', f.subject || '')
      : `<b>${esc(f.label)}</b>`;
    parts.push(`fix: ${fLink}${meta ? ` (${esc(meta)})` : ''}`);
  }
  // Avoid duplicating the incident list on post-submit alert cards when no fix CL exists yet
  if (!fixes.length && isPostSubmitAlert(it)) return '';
  return `<div class="fix-banner">${parts.join(' · ')}</div>`;
}

// Renders 'Fixes breakage from → <CL> · Red bots: <bots>' on the fix CL
function fixesBreakageBannerHtml(it) {
  if (!it.fixes_breakage_from) return '';
  const refLabel = it.fixes_breakage_label || String(it.fixes_breakage_from);
  const refLink = it.fixes_breakage_url
    ? link(it.fixes_breakage_url, `<b>${esc(refLabel)}</b>`)
    : `<b>${esc(refLabel)}</b>`;
  const bots = Array.isArray(it.fixes_breakage_bots) ? it.fixes_breakage_bots : [];
  const botsPart = bots.length ? ` · Red bots: ${formatBotsListHtml(bots)}` : '';
  return `<div class="fix-banner">Fixes breakage from → ${refLink}${botsPart}</div>`;
}

// Renders an inline or card button that triggers two-click companion abandon
function abandonActionHtml(itemKeys, label, cls) {
  const keys = Array.isArray(itemKeys) ? itemKeys : [itemKeys];
  const valid = keys.filter(Boolean);
  if (!valid.length) return '';
  const keysJson = esc(JSON.stringify(valid));
  const btnCls = cls || 'triage-btn';
  const text = esc(label || '🗑️ Abandon');
  return `<button class="${btnCls}" onclick="abandonCompanions(${keysJson}, this)">` +
    `${text}</button>`;
}

// Renders active companions on the parent CL card/row with state badges and abandon actions
function companionsBannerHtml(it) {
  const comps = (Array.isArray(it.companions) ? it.companions : [])
    .filter(c => c && c.label && c.status !== 'ABANDONED' && c.status !== 'CLOSED');
  if (!comps.length) return '';
  const itemsHtml = comps.map(c => {
    const cLink = c.url ? link(c.url, `<b>${esc(c.label)}</b>`) : `<b>${esc(c.label)}</b>`;
    const stateBadge = c.companion_duplicate
      ? `<span class="badge warn">⚠️ Duplicate</span>`
      : (c.ready_to_abandon ? `<span class="badge has-fix">✅ Ready to abandon</span>` : '');
    const note = c.companion_status_note
      ? ` <span class="triage-meta">(${esc(c.companion_status_note)})</span>`
      : '';
    const abBtn = c.item_key
      ? ` ${abandonActionHtml([c.item_key], '🗑️ Abandon', 'triage-btn')}`
      : '';
    const psPart = c.patchset ? ` PS${esc(c.patchset)}` : '';
    return `<span>Companion ${cLink}${psPart} ${stateBadge}${note}${abBtn}</span>`;
  }).join(' · ');
  const dupKeys = comps.filter(c => c.companion_duplicate && c.item_key).map(c => c.item_key);
  const readyKeys = comps.filter(c => c.ready_to_abandon && c.item_key).map(c => c.item_key);
  let batchBtn = '';
  if (readyKeys.length > 1) {
    batchBtn = abandonActionHtml(
      readyKeys,
      `🗑️ Abandon all companions (${readyKeys.length})`,
      'triage-btn primary'
    );
  } else if (dupKeys.length >= 1 && comps.length > 1) {
    batchBtn = abandonActionHtml(
      dupKeys,
      `🗑️ Abandon ${dupKeys.length} duplicate${dupKeys.length > 1 ? 's' : ''}`,
      'triage-btn primary'
    );
  }
  const actionsSpan = batchBtn ? `<span class="triage-actions">${batchBtn}</span>` : '';
  return `<div class="companion-banner">${itemsHtml}${actionsSpan}</div>`;
}

// Maps fix CL stage and summary to display pill title and tone
function fixStageInfo(f) {
  if (!f) return {stage: '', stageLabel: 'Fix', tone: 'progress'};
  const stage = String(f.stage || '').toLowerCase();
  const summary = String(f.status_summary || '').toLowerCase();
  const status = String(f.status || '').toUpperCase();
  let stageLabel = f.stage_label || '';
  let tone = 'progress';

  if (stage === 'shipped' || summary.includes('all rolls')) {
    stageLabel = stageLabel || 'Fix shipped';
    tone = 'good';
  } else if (stage === 'merged_rolling' || status === 'MERGED' || summary.startsWith('merged')) {
    stageLabel = stageLabel || 'Fix landed · rolling';
    tone = 'good';
  } else if (stage === 'approved' || summary === 'approved') {
    stageLabel = stageLabel || 'Fix approved';
    tone = 'good';
  } else if (stage === 'submitting' || summary === 'submitting') {
    stageLabel = stageLabel || 'Fix submitting';
    tone = 'progress';
  } else if (stage === 'in_cq' || summary.includes('cq+') || summary.includes('dry run')) {
    stageLabel = stageLabel || 'Fix in dry run';
    tone = 'progress';
  } else if (stage === 'cq_failed' || summary.includes('cq failed')) {
    stageLabel = stageLabel || 'Fix failed CQ';
    tone = 'bad';
  } else if (stage === 'needs_reply' || summary.includes('needs reply')) {
    stageLabel = stageLabel || 'Fix needs reply';
    tone = 'warn';
  } else if (
    stage === 'in_progress' ||
    f.is_wip ||
    summary.includes('wip') ||
    summary.includes('draft') ||
    summary.includes('needs reviewers')
  ) {
    stageLabel = stageLabel || 'Fix in progress';
    tone = 'warn';
  } else {
    stageLabel = stageLabel || 'Fix awaiting review';
    tone = 'progress';
  }
  return {stage, stageLabel, tone};
}

// Renders prominent fix status line at 17px in focus card why section
function fixPrimaryStatusHtml(it) {
  const fixes = Array.isArray(it.fixes) ? it.fixes : [];
  if (!fixes.length) return '';
  return fixes.map(f => {
    if (!f || !f.label) return '';
    const info = fixStageInfo(f);
    const meta = [f.status_summary, f.age].filter(Boolean).join(', ');
    const fLink = f.url
      ? link(f.url, `<b>${esc(f.label)}</b>`, '', f.subject || 'Open fix')
      : `<b>${esc(f.label)}</b>`;
    const metaSpan = meta ? ` <span class="fix-detail">(${esc(meta)})</span>` : '';
    const subjClean = cleanSubject(f.subject);
    const subjSpan = subjClean ? ` <span class="fix-subj">— ${esc(subjClean)}</span>` : '';
    return `<div class="fix-primary-status">` +
      `<span class="fix-stage-head ${esc(info.tone)}">${esc(info.stageLabel)}:</span> ` +
      `${fLink}${metaSpan}${subjSpan}</div>`;
  }).join('');
}

// Renders Open fix action buttons for focus cards
function openFixActionHtml(it) {
  const fixes = Array.isArray(it.fixes) ? it.fixes : [];
  const withUrl = fixes.filter(f => f && f.url);
  if (!withUrl.length) return '';
  if (withUrl.length === 1) {
    const f = withUrl[0];
    const tip = [f.label, cleanSubject(f.subject)].filter(Boolean).join(': ');
    return link(f.url, 'Open fix', 'btn secondary', tip);
  }
  return withUrl.map(f => {
    const tip = [f.label, cleanSubject(f.subject)].filter(Boolean).join(': ');
    const shortLabel = f.target_id ? `Open fix (${f.target_id})` : 'Open fix';
    return link(f.url, esc(shortLabel), 'btn secondary', tip);
  }).join('');
}

function reviewerGapHtml(it) {
  if (isCompanion(it)) return '';
  if (it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') return '';
  const missRev = Number(it.missing_reviewers_to_assign || 0);
  const reqApp = Number(it.required_approvals || 1);
  const assigned = Number(it.assigned_reviewers_count || 0);
  if (missRev > 0) {
    if (assigned === 0) {
      return `<span class="amber">${reqApp > 1 ? `Needs ${reqApp} reviewers` : 'No reviewers assigned'}</span>`;
    }
    if (it.is_external && reqApp > 1 && assigned > 0 && !it.self_approved && (it.self_cr_vote || 0) <= 0) {
      let suffix = `${assigned}/${reqApp}`;
      if (it.suggested_second_reviewers && it.suggested_second_reviewers.length > 0) {
        suffix = it.suggested_second_reviewers.map(r => r.short).join(' / ');
      }
      return `<span class="soft">After your CR+1: +1 Googler (${suffix})</span>`;
    }
    return `<span class="amber">Needs +${missRev} reviewer${missRev > 1 ? 's' : ''} (${assigned}/${reqApp})</span>`;
  }
  const missApp = Number(it.missing_approvals || 0);
  const approved = Number(it.approved_count || 0);
  if (missApp > 0 && reqApp > 1 && (approved > 0 || !it.is_self)) {
    return `<span class="amber">Needs +${missApp} CR (${approved}/${reqApp})</span>`;
  }
  return '';
}

function authorLabel(it) {
  if (it.is_self) return 'You';
  const who = it.owner_name || shortActor(it.owner_email);
  return it.is_external ? `${who} (external)` : who;
}

// Gardener task names are long build configs; keep the parts a human scans for
function shortTask(name) {
  const parts = String(name || '').split('-');
  const os = (parts.find(p => /^(Ubuntu|Win|Mac|Android|Debian)/.test(p)) || '')
    .replace(/^([A-Za-z]+)(\d)/, '$1 $2');
  const tail = (parts[parts.length - 1] || '').split('_')
    .filter(t => !/^(Upload|All)$/.test(t));
  const build = parts.includes('Debug') ? 'Debug' : (parts.includes('Release') ? 'Release' : '');
  return [tail.join(' '), build, os].filter(Boolean).join(' · ') || String(name || '');
}

function formatStatusTime(it, fallbackEt, fallbackAge) {
  const et = it.status_changed_et || fallbackEt || it.last_msg_et || it.submitted_et || '';
  const age = it.status_changed_age || fallbackAge || it.last_msg_age || it.submitted_age || '';
  if (et && age) return `${esc(et)} (${esc(age)})`;
  return esc(et || age);
}

// One predicate drives the headline count and the focus cards
function needsMyReply(it) {
  return Boolean(it.in_my_attention) && it.phase !== 'ALERT_CQ_FAILED' &&
    !(it.cq_failed && it.is_self) &&
    it.phase !== 'SUBMITTING' && !it.is_draft && !it.in_development &&
    // On my own CL a dry-run bot note keeps me in attention_set without asking a question
    !(it.is_self && shortActor(it.last_msg_author) === 'SkCQ');
}

function isAlert(it) {
  return (it.phase || '').startsWith('ALERT_');
}

// A post-submit alert means a landed commit broke something; a CQ failure is still pre-submit
function isPostSubmitAlert(it) {
  return isAlert(it) && it.phase !== 'ALERT_CQ_FAILED';
}

// External-author Gerrit CLs with our CR+1 need Googler action to add a 2nd reviewer or CQ+2
function needsSponsorAction(it) {
  if (!it || it.is_self || isCompanion(it)) return false;
  if (it.status !== 'NEW' && it.status !== 'OPEN') return false;
  if (it.is_wip || it.is_draft || it.reviewer_removed) return false;
  const act = String(it.sponsor_action || '');
  if (!act) return false;
  // Hold CQ+2 until unmerged stack ancestors also have all approvals and no conflict
  if (act === 'submit_cq') {
    const ancs = Array.isArray(it.stack_ancestors) ? it.stack_ancestors : [];
    for (const ancKey of ancs) {
      const anc = cachedStackByIkey.get(ancKey);
      if (!anc) continue;
      const st = String(anc.status || '').toUpperCase();
      if (st === 'MERGED' || st === 'ABANDONED' || st === 'CLOSED') continue;
      if (Number(anc.missing_approvals || 0) > 0 || anc.merge_conflict ||
          anc.cq_failed || anc.is_wip) {
        return false;
      }
    }
  }
  return true;
}

// Alerts count as needing me even on merged CLs, since a blamed commit needs a reaction
function statusOf(it) {
  const preReview = Boolean(it.is_draft || it.in_development);
  // Pre-review drafts and no-reviewer changes stay out of Needs You until reviewers are added
  if (isPostSubmitAlert(it) || (!preReview && (isAlert(it) || needsMyReply(it)))) {
    return 'attention';
  }
  if (it.status === 'ABANDONED' || it.status === 'CLOSED') return 'abandoned';
  // Surface parent CLs in Needs You when an attached companion is ready to abandon
  if (hasReadyCompanion(it)) return 'attention';
  // Surface parent CLs when an attached release-branch cherry-pick CL needs Gerrit action
  if (Array.isArray(it.merge_children) && it.merge_children.some(mc => mc && mc.cl_needs_you)) {
    return 'attention';
  }
  // Sponsored external CLs stay out of Gerrit attention_set yet still need Googler action
  if (needsSponsorAction(it)) return 'attention';
  // Open orphan companions wait on the parent author unless marked ready to abandon
  if (isCompanion(it)) {
    return it.ready_to_abandon ? 'attention' : 'waiting';
  }
  if (it.status === 'MERGED') {
    const ageDays = (Date.now() / 1000 - (it.submitted_ts || 0)) / 86400;
    return ageDays > OLD_MERGED_DAYS ? 'old' : 'merged';
  }
  // Non-participant changes sit in Watching rather than personal workflow queues
  if (it.is_watching || (!it.is_self && it.reviewer_removed)) return 'watching';
  // A conflict on an in-review change needs rebase now; pre-review conflicts stay in On your desk
  if (it.merge_conflict && it.is_self && !preReview) return 'conflict';
  if (it.is_draft) return 'draft';
  if (it.in_development) return 'dev';
  const cq = String(it.cq_status || '');
  const isApproved = Number(it.approved_count || 0) > 0 &&
    Number(it.missing_approvals || 0) === 0;
  // Submitting or fully approved changes with active checks belong in Landing
  if (it.phase === 'SUBMITTING' || cq.startsWith('CQ+2') ||
      (isApproved && (cq.startsWith('CQ+') || cq.includes('running')))) {
    return 'cq';
  }
  if (it.tree_red) return 'tree';
  return 'waiting';
}

// Lanes group statuses by who has to act next; order here is display order
const LANES = [
  {id: 'landing', title: 'Landing',
    hint: 'Commit queue, then Skia → Android · Chrome · Google3 · Flutter'},
  {id: 'desk', title: 'On your desk', hint: 'Drafts and changes without reviewers'},
  {id: 'waiting', title: 'Waiting on others', hint: 'Nothing to do until they reply'},
  {id: 'watching', title: 'Watching', hint: 'Other people’s changes'},
];

function laneOf(it) {
  const st = statusOf(it);
  if (st === 'attention' || st === 'conflict') return 'you';
  if (st === 'cq') return 'landing';
  if (st === 'merged') {
    // Keep merged parent CLs visible in Landing until all release-branch merges settle
    if (it.has_active_merges) return 'landing';
    // A merged CL without post-submit data has nothing left to track
    const ps = it.post_submit;
    return ps && !ps.all_passed ? 'landing' : 'done';
  }
  if (st === 'old' && it.has_active_merges) return 'landing';
  if (st === 'draft' || st === 'dev') return 'desk';
  if (st === 'waiting' || st === 'tree') return 'waiting';
  if (st === 'watching') return 'watching';
  return 'done';
}

function rollClass(state) {
  if (state === '✓') return 'ok';
  if (state === '🔄') return 'moving';
  // Map unblamed roll failures and nearby tree flakes to amber warnings
  if (state === '🟠' || state === '⚠️') return 'warn';
  if (state === '✗' || state === '🔴') return 'bad';
  return 'wait';
}

// Summarizes abnormal or active Skia tree and roll states below the inline stop strip
function flightPathNotesHtml(ps, rolls) {
  const notes = [];
  const skiaState = ps.skia_status || '✓';
  // Surface Skia tree incidents only when the commit is nearby or blamed
  if (skiaState === '⚠️') {
    const incs = Array.isArray(ps.incidents) ? ps.incidents : [];
    const first = incs[0] || null;
    const extra = incs.length > 1 ? ` +${incs.length - 1}` : '';
    const botHtml = (first && first.task_name)
      ? link(first.task_url || STATUS_SKIA_URL,
          `${esc(compactBotName(first.task_name))}${extra}`, '', first.task_name)
      : 'failing task';
    notes.push(`<span class="path-note is-warn">Skia: near ${botHtml} (not blamed)</span>`);
  } else if (skiaState === '🔴') {
    const bots = Array.isArray(ps.broken_bots) ? ps.broken_bots : [];
    const first = bots[0] || null;
    const extra = bots.length > 1 ? ` +${bots.length - 1}` : '';
    const botHtml = (first && first.task_name)
      ? link(first.task_url || first.swarming_url || STATUS_SKIA_URL,
          `in ${esc(compactBotName(first.task_name))}${extra}`, '', first.task_name)
      : 'by Gardener';
    notes.push(`<span class="path-note is-bad">Skia: blamed ${botHtml}</span>`);
  }
  // Omit quiet rolled/waiting stops from the second line and surface only active CQ or failures
  for (const p of PRODUCTS) {
    const r = rolls[p] || {};
    const detail = String(r.detail || '');
    const failMatch = detail.match(/failed=(\d+)/);
    const failSuffix = failMatch ? ` (${failMatch[1]} failed)` : '';
    const url = r.url || STATUS_SKIA_URL;
    if (r.state === '🔄') {
      const body = link(url, `${esc(p)}: in CQ`, '', `${p}: ${detail || 'In CQ'}`);
      notes.push(`<span class="path-note is-moving">${body}</span>`);
    } else if (r.state === '🟠') {
      const label = `${esc(p)}: failing roll, not blamed${failSuffix}`;
      const body = link(url, label, '', `${p}: ${detail}`);
      notes.push(`<span class="path-note is-warn">${body}</span>`);
    } else if (r.state === '🔴' || r.state === '✗') {
      const label = `${esc(p)}: blamed in roll${failSuffix}`;
      const body = link(url, label, '', `${p}: ${detail}`);
      notes.push(`<span class="path-note is-bad">${body}</span>`);
    }
  }
  if (!notes.length) return '';
  return `<span class="path-notes">${notes.join('<span class="path-sep">·</span>')}</span>`;
}

function flightPath(it, withLabels) {
  const ps = it.post_submit || {};
  const rolls = ps.rolls || {};
  // Include nearby flake task names in the Skia tooltip when not blamed
  const skiaState = ps.skia_status || '✓';
  const skiaCls = rollClass(skiaState);
  const incs = Array.isArray(ps.incidents) ? ps.incidents : [];
  const incNames = incs.map(x => x && x.task_name).filter(Boolean).slice(0, 2);
  const skiaBaseTip = ps.skia_status_detail || 'Clean on status.skia.org';
  const skiaTip = (skiaState === '⚠️' && incNames.length)
    ? `Skia: ${skiaBaseTip} (${incNames.join(', ')})`
    : `Skia: ${skiaBaseTip}`;
  const skiaStop = link(STATUS_SKIA_URL,
    `<i class="dot ${skiaCls}"></i><span>Skia</span>`,
    `stop is-${skiaCls}`, skiaTip);
  // Use span phrasing elements so inline rows stay valid inside collapsed stack summaries
  if (!withLabels) {
    const prodStops = PRODUCTS.map(p => {
      const r = rolls[p] || {};
      const cls = rollClass(r.state);
      return link(r.url || STATUS_SKIA_URL,
        `<i class="dot ${cls}"></i><span>${p}</span>`,
        `stop is-${cls}`, `${p}: ${r.detail || 'waiting'}`);
    }).join('<span class="path-sep">·</span>');
    const stopsHtml = `<span class="path-stops">${skiaStop}` +
      `<span class="path-sep">→</span>${prodStops}</span>`;
    return `<span class="path path-inline">${stopsHtml}${flightPathNotesHtml(ps, rolls)}</span>`;
  }
  // Rolls into the four products are independent, so they fan out instead of chaining;
  // post-submit CLs already landed in Skia unless the tree blames them or awaits merge
  const legCls = skiaCls === 'bad' ? 'broken' : (skiaCls === 'wait' ? '' : 'done');
  let html = `<div class="path">${skiaStop}<i class="leg${legCls ? ' ' + legCls : ''}"></i>` +
    '<div class="fan">';
  for (const p of PRODUCTS) {
    const r = rolls[p] || {};
    const cls = rollClass(r.state);
    html += link(r.url || STATUS_SKIA_URL,
      `<i class="dot ${cls}"></i><span>${p}</span>`,
      `stop is-${cls}`, `${p}: ${r.detail || 'waiting'}`);
  }
  return html + '</div></div>';
}

const INCIDENT_VERDICT_LABEL = {
  blamed: 'Blamed',
  flake: 'Flake',
  low_confidence: 'Low-confidence suspect',
  blamelist: 'In blamelist only',
};

// Expands multi-bot blamed incidents so every affected task appears in the card
function expandIncidentsForDisplay(ps) {
  const raw = (ps && Array.isArray(ps.incidents)) ? ps.incidents : [];
  const out = [];
  const seen = new Set();
  for (const h of raw) {
    const aff = Array.isArray(h.affected_tasks) ? h.affected_tasks : [];
    // Split a multi-bot blamed incident into one entry per distinct affected bot
    if (h.verdict === 'blamed' && aff.length > 1) {
      for (const a of aff) {
        const name = a.task_name || h.task_name;
        if (seen.has(name)) continue;
        seen.add(name);
        out.push({
          ...h,
          task_name: name,
          task_url: a.task_url || h.task_url,
          swarming_url: a.swarming_url || h.swarming_url,
        });
      }
    } else {
      if (seen.has(h.task_name)) continue;
      seen.add(h.task_name);
      out.push(h);
    }
  }
  return out;
}

function incidentHtml(h) {
  const label = INCIDENT_VERDICT_LABEL[h.verdict] || esc(h.verdict);
  const task = link(h.task_url, `<span class="chip" title="${esc(h.task_name)}">` +
    `${esc(shortTask(h.task_name))}</span>`);
  const log = h.swarming_url ? link(h.swarming_url, 'log') : '';
  const score = h.confidence_score ? `${esc(h.confidence_score)}% confidence` : '';
  const why = [label, score, log].filter(Boolean).join(' · ');
  return `<div class="incident">${task}<span class="why-line">${why}</span></div>`;
}

function waitingOnHtml(it) {
  const people = (it.waiting_on || []).map(p => {
    const who = link(actorUrl(it, p.id), esc(shortActor(p.id, it)));
    return p.active ? who : `<s>${who}</s> (left Google)`;
  });
  if (people.length) return `Waiting on <b>${people.join(', ')}</b>`;
  if (!it.is_self &&
      (it.self_review_state === 'commented' || it.self_review_state === 'changes_requested')) {
    const authorWho = link(
      actorUrl(it, it.owner_email || it.owner_name),
      esc(shortActor(it.owner_email || it.owner_name, it))
    );
    return `Waiting on <b>${authorWho}</b>`;
  }
  return '';
}

// Formats per-reviewer ping status for the row state column
function reviewerPingInlineHtml(it) {
  if (it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') return '';
  const pings = Array.isArray(it.reviewer_pings) ? it.reviewer_pings : [];
  if (!pings.length) return '';
  return pings.map(p => {
    const who = link(actorUrl(it, p.reviewer), esc(shortActor(p.reviewer)));
    const prev = p.prev_ps
      ? `PS${esc(p.prev_ps)}→PS${esc(p.current_ps || it.patchset || 1)}`
      : '';
    const diffLink = (p.diff_url && prev) ? link(p.diff_url, prev) : prev;
    let tag = '';
    // Map each ping category to its badge and inter-patchset diff link
    if (p.reason === 'lost_on_rebase') {
      const extra = diffLink ? ' ' + diffLink : '';
      tag = `<span class="badge ping-rebase">🔄 +1 lost on rebase${extra}</span>`;
    } else if (p.reason === 'updated_after_approval') {
      const extra = diffLink ? ' ' + diffLink : '';
      tag = `<span class="badge ping-updated">📝 updated after +1${extra}</span>`;
    } else if (p.reason === 'updated_after_review') {
      const extra = diffLink ? ' ' + diffLink : '';
      tag = `<span class="badge ping-updated">📝 updated after review${extra}</span>`;
    } else if (p.reason === 'needs_second_approval') {
      tag = `<span class="badge ping-second">⏳ needs 2nd +1</span>`;
    } else {
      tag = `<span class="badge ping-second">⏳ awaiting review</span>`;
    }
    // Flag reviewers dropped from Gerrit attention_set or GitHub reviewRequests
    const isGh = it.kind === 'github';
    const offAtt = p.auto_rerequested
      ? `<span class="badge ping-rebase" ` +
        `title="Flight Deck automatically re-requested review on GitHub">🔄 re-requested</span>`
      : (p.in_attention_set === false
        ? `<span class="badge ping-off-att" title="${isGh
            ? 'Author updated PR without clicking Re-request review on GitHub'
            : 'Not in Gerrit attention set'}">${isGh
            ? '🔕 no re-request' : '🔕 off attention'}</span>`
        : '');
    return `${tag}${offAtt} <b>${who}</b>`;
  }).join(' · ');
}

// One plain-language line per row: what state the change is in and why
function stateHtml(it) {
  const cr = it.cr_summary ? `CR ${esc(it.cr_summary)}` : '';
  const cq = it.cq_status ? link(checksUrl(it), esc(it.cq_status)) : '';
  const gap = reviewerGapHtml(it);
  const wip = wipLabel(it);
  const wipTag = (wip && wip !== 'Draft') ? `<b>${esc(wip)}</b>` : (wip || '');
  const compNote = isCompanion(it)
    ? `Companion to ${esc(it.companion_to_label || it.companion_to)}`
    : '';
  const review = [cr, cq].filter(Boolean).join(' · ');
  if (it.status === 'ABANDONED' || it.status === 'CLOSED') {
    return it.status === 'ABANDONED' ? 'Abandoned' : 'Closed';
  }
  // Companion rows show parent linkage, stacked status note, and an inline Abandon button
  if (isCompanion(it)) {
    const parentRef = it.companion_to_label || it.companion_to;
    const note = it.companion_status_note || 'Waiting on author';
    const abBtn = it.item_key
      ? ` · ${abandonActionHtml([it.item_key], '🗑️ Abandon', 'triage-btn')}`
      : '';
    return [wipTag, `Companion to ${esc(parentRef)}`, esc(note)].filter(Boolean).join(' · ') +
      abBtn;
  }
  if (it.status === 'MERGED') {
    const ps = it.post_submit;
    if (ps && ps.all_passed) return 'Shipped to all 4 products';
    if (ps) return flightPath(it, false);
    return 'Merged';
  }
  if (it.phase === 'SUBMITTING') return `Submitting · ${link(checksUrl(it), 'SkCQ')}`;
  const st = statusOf(it);
  // GitHub has no commit queue; a running check set is the closest state
  if (st === 'cq') return `${it.kind === 'github' ? 'Checks' : 'In the commit queue'} · ${cq}`;
  if (st === 'draft' || st === 'dev') {
    // Keep conflict and inline rebase visible inside On your desk rows
    const conflictRebase = it.merge_conflict
      ? ['<span class="red">Conflict</span>', rebaseActionHtml(it, 'triage-btn')].filter(Boolean)
          .join(' ')
      : '';
    if (st === 'draft') {
      return [wipTag || 'Draft', compNote, conflictRebase, gap, cq || 'no CQ run']
        .filter(Boolean).join(' · ');
    }
    return [wipTag, compNote, conflictRebase, gap || 'No reviewers yet', cq || 'no CQ run']
      .filter(Boolean).join(' · ');
  }
  if (st === 'tree') {
    return `<span class="red">${link(FLUTTER_DASHBOARD_URL, 'flutter/flutter tree')} is red;` +
      ' landing waits for it</span>';
  }
  // Surface per-reviewer ping status in the state column ahead of generic waiting labels
  const pingNote = reviewerPingInlineHtml(it);
  // Non-self CLs with merge conflicts stay in Waiting on others and still need a Conflict tag
  const conflictTag = it.merge_conflict ? '<span class="red">Conflict</span>' : '';
  if (st === 'watching') {
    return [wipTag, compNote, conflictTag, pingNote, review, gap]
      .filter(Boolean).join(' · ') || 'Watching';
  }
  return [wipTag, compNote, conflictTag, pingNote || waitingOnHtml(it) || 'In review', review, gap]
    .filter(Boolean).join(' · ');
}

// Row time is when the change last changed state, in ET with its age
function rowTime(it) {
  if (it.status === 'MERGED') {
    const ps = it.post_submit;
    if (ps && ps.all_passed && ps.status_changed_et) {
      return `${esc(ps.status_changed_et)} (${esc(ps.status_changed_age)})`;
    }
    return formatStatusTime(it, it.submitted_et, it.submitted_age);
  }
  return formatStatusTime(it);
}

// Assigned reviewer changes use the code review button state machine
function isReviewerChange(it) {
  if (!it || it.is_self || isCompanion(it)) return false;
  if (it.reviewer_removed) return false;
  const role = String(it.role || '').trim();
  if (role) return role.startsWith('reviewer');
  return !it.is_watching;
}

function chatActionHtml(it, data, compact) {
  const isRev = isReviewerChange(it);
  const chat = chatUrl(it, data);
  const curPs = Number(it.patchset || 1);
  const revPs = Number(it.reviewed_ps || 0);
  const tgtPs = Number(it.review_target_ps || 0);
  if (chat) {
    if (isRev) {
      const cidJson = esc(JSON.stringify(it.conversation_id || ''));
      const urlJson = esc(JSON.stringify(chat));
      if (it.chat_busy) {
        const label = compact ? '⏳ preparing review…' : '⏳ Preparing code review…';
        const cls = compact ? 'chat' : 'btn quiet';
        return `<a class="${cls}" href="${esc(chat)}" target="jetski-chat" ` +
          `title="Code review is running in Jetski; click to watch live" ` +
          `onclick="return openChat(${cidJson}, ${urlJson}, event)">${label}</a>`;
      }
      if (it.item_key && revPs > 0 && curPs > revPs && curPs > tgtPs) {
        const cls = compact ? 'link-btn chat' : 'btn primary';
        const label = compact ? `start review (PS${curPs})` : `Start code review (PS${curPs})`;
        return `<button class="${cls}" ` +
          `onclick="startChat(${esc(JSON.stringify(it.item_key))}, this)">${label}</button>`;
      }
      const alreadyResponded = !it.in_my_attention &&
        (Boolean(it.self_approved) || Boolean(it.self_review_state));
      if (alreadyResponded) {
        const label = it.chat_archived
          ? (compact ? 'review chat (archived)' : 'Open review chat (archived)')
          : (compact ? 'review chat' : 'Open review chat');
        const cls = compact ? 'chat' : 'btn quiet';
        return `<a class="${cls}" href="${esc(chat)}" target="jetski-chat" ` +
          `onclick="return openChat(${cidJson}, ${urlJson}, event)">${label}</a>`;
      }
      const label = it.chat_archived
        ? (compact ? 'look at code review (archived)' : 'Look at code review (archived)')
        : (compact ? 'look at code review' : 'Look at code review');
      const cls = compact ? 'chat' : 'btn primary';
      return `<a class="${cls}" href="${esc(chat)}" target="jetski-chat" ` +
        `onclick="return openChat(${cidJson}, ${urlJson}, event)">${label}</a>`;
    }
    const label = it.chat_archived
      ? (compact ? 'chat (archived)' : 'Open chat (archived)')
      : (compact ? 'chat' : 'Open chat');
    const cls = compact ? 'chat' : 'btn quiet';
    const cidJson = esc(JSON.stringify(it.conversation_id || ''));
    const urlJson = esc(JSON.stringify(chat));
    return `<a class="${cls}" href="${esc(chat)}" target="jetski-chat" ` +
      `onclick="return openChat(${cidJson}, ${urlJson}, event)">${label}</a>`;
  }
  if (!it.item_key) return '';
  if (isRev) {
    const cls = compact ? 'link-btn chat' : 'btn primary';
    const label = compact ? 'start code review' : 'Start code review';
    return `<button class="${cls}" ` +
      `onclick="startChat(${esc(JSON.stringify(it.item_key))}, this)">${label}</button>`;
  }
  const cls = compact ? 'link-btn chat' : 'btn quiet';
  const label = compact ? 'start chat' : 'Start chat';
  return `<button class="${cls}" onclick="startChat(${esc(JSON.stringify(it.item_key))}, this)">` +
    `${label}</button>`;
}

// Server-side rebase fails on conflicts, so only my own open changes wake a local chat
function canRebase(it) {
  if (isCompanion(it)) return false;
  return Boolean(it.merge_conflict && it.is_self) &&
    (it.status === 'NEW' || it.status === 'OPEN') && Boolean(it.item_key);
}

// cls overrides the button style (e.g. triage-btn) when embedded in lane rows
function rebaseActionHtml(it, cls) {
  if (!canRebase(it)) return '';
  const btnCls = cls || 'btn primary';
  const dis = it.chat_busy ? ' disabled title="Chat is already working"' : '';
  return `<button class="${btnCls}"${dis} ` +
    `onclick="rebaseItem(${esc(JSON.stringify(it.item_key))}, this)">🔧 Rebase</button>`;
}

function findItemByKey(key) {
  const data = cachedData;
  if (!data || !data.items) return null;
  if (data.items[key]) return data.items[key];
  return Object.values(data.items).find(
    x => x.item_key === key || x.target_id === key
  ) || null;
}

let currentTriageModalKey = null;
let currentTriageModalTab = 'report';
let finalizeModalState = {
  key: null, loading: false, submitting: false, preview: null, error: '', result: null,
  draftIds: new Set(), threadLeaves: new Set(), companionIds: new Set(), canaries: new Set(),
};

// Gate one-click finalization to open non-WIP external/colleague Gerrit CLs with green CLEAN triage
function canFinalizeCleanReview(it) {
  if (!it || it.kind !== 'gerrit' || it.is_self || isCompanion(it)) return false;
  if (it.status !== 'NEW' || it.is_wip || it.self_approved) return false;
  if (Number(it.self_cr_vote || 0) > 0 || isTriageStale(it)) return false;
  const tr = it.triage;
  if (!tr || String(tr.review_outcome || '').toUpperCase() !== 'CLEAN') return false;
  const cStat = effectiveCorpusStatus(it);
  return cStat === 'passed' || cStat === 'not_required' || cStat === 'waived';
}

// Fetch a read-only Gerrit/ASAN/corpus dry-run preview before enabling Confirm CR+1 & publish
async function openFinalizeModal(key) {
  const it = findItemByKey(key);
  if (!it || !it.triage) return;
  currentTriageModalKey = key;
  currentTriageModalTab = 'finalize';
  finalizeModalState = {
    key, loading: true, submitting: false, preview: null, error: '', result: null,
    draftIds: new Set(), threadLeaves: new Set(), companionIds: new Set(), canaries: new Set(),
  };
  renderTriageModal(it);
  try {
    const resp = await fetch('/api/approve_review_preview', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        item_key: key, target_id: it.target_id, host: it.host || '',
      }),
    });
    const data = await resp.json();
    if (finalizeModalState.key !== key) return;
    if (!data || !data.ok) {
      finalizeModalState.loading = false;
      finalizeModalState.error = (data && data.error) || 'Failed to load finalize preview';
    } else {
      finalizeModalState.loading = false;
      finalizeModalState.preview = data;
      finalizeModalState.draftIds = new Set(
        (data.drafts || []).filter(d => d.selected_by_default).map(d => String(d.id))
      );
      finalizeModalState.threadLeaves = new Set(
        (data.own_unresolved_threads || [])
          .filter(t => t.selected_by_default)
          .map(t => String(t.leaf_id))
      );
      finalizeModalState.companionIds = new Set(
        (data.companions || [])
          .filter(c => c.action === 'abandon' && c.selected_by_default)
          .map(c => String(c.id))
      );
      finalizeModalState.canaries = new Set(data.deferred_canaries || []);
    }
  } catch (err) {
    if (finalizeModalState.key !== key) return;
    finalizeModalState.loading = false;
    finalizeModalState.error = String(err);
  }
  const latest = findItemByKey(key) || it;
  renderTriageModal(latest);
}

function toggleFinalizeChoice(bucket, id, checked) {
  const setObj = finalizeModalState[bucket];
  if (!setObj) return;
  if (checked) setObj.add(String(id));
  else setObj.delete(String(id));
}

// Submit the user-checked whitelist of drafts, own threads, companions, and post-CR+1 canaries
async function submitFinalizeReview(key) {
  const st = finalizeModalState;
  if (!st || st.key !== key || !st.preview || st.submitting) return;
  st.submitting = true;
  st.error = '';
  const it = findItemByKey(key);
  if (it) renderTriageModal(it);
  try {
    const resp = await fetch('/api/approve_review', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        item_key: key,
        target_id: st.preview.target_id,
        host: st.preview.host || '',
        expected_revision: st.preview.revision || '',
        draft_ids: Array.from(st.draftIds),
        resolve_thread_leaf_ids: Array.from(st.threadLeaves),
        abandon_companion_ids: Array.from(st.companionIds),
        trigger_canaries: Array.from(st.canaries),
      }),
    });
    const res = await resp.json();
    st.submitting = false;
    if (!res || !res.ok) {
      st.error = (res && res.error) || 'Finalize request failed';
    } else {
      st.result = res;
      await fetchState();
    }
  } catch (err) {
    st.submitting = false;
    st.error = String(err);
  }
  const latest = findItemByKey(key) || it;
  if (latest) renderTriageModal(latest);
}

function renderFinalizeTabHtml(it) {
  const key = it.item_key || it.target_id;
  const keyJson = esc(JSON.stringify(key));
  const st = finalizeModalState;
  if (!st || st.key !== key || st.loading) {
    return '<div class="finalize-sec">' +
      'Checking live Gerrit drafts, threads, ASAN &amp; corpus…</div>';
  }
  if (st.result && st.result.ok) {
    const r = st.result;
    const parts = [`Voted Code-Review+1 on PS${esc(r.patchset)}`];
    parts.push(`published ${esc((r.published_draft_ids || []).length)} draft(s)`);
    if (r.resolved_threads_count) {
      parts.push(`closed ${esc(r.resolved_threads_count)} own thread(s)`);
    }
    if ((r.abandoned_companion_ids || []).length) {
      parts.push(`abandoned companion ${esc(r.abandoned_companion_ids.join(', '))}`);
    }
    if ((r.triggered_canaries || []).length) {
      parts.push(`triggered ${esc(r.triggered_canaries.join(', '))}`);
    }
    const gerritBtn = link(r.url || it.url, 'Open in Gerrit', 'triage-btn primary');
    return `<div class="finalize-ok"><b>✅ Review finalized:</b> ${parts.join(' · ')}.</div>` +
      `<div class="finalize-foot">${gerritBtn}` +
      `<button class="triage-btn" onclick="closeTriageModal()">Done</button></div>`;
  }
  let errBanner = st.error ? `<div class="finalize-warn">${esc(st.error)}</div>` : '';
  const p = st.preview;
  if (!p) {
    return errBanner + `<div class="finalize-foot">` +
      `<button class="triage-btn" onclick="openFinalizeModal(${keyJson})">Retry</button></div>`;
  }
  if ((p.blockers || []).length) {
    errBanner += `<div class="finalize-warn"><b>Blocked:</b> ` +
      `${p.blockers.map(esc).join(' · ')}</div>`;
  }
  const g = p.gates || {};
  const fc = (g.flutter_corpus && g.flutter_corpus.status) || 'not_required';
  const gc = (g.g3_corpus && g.g3_corpus.status) || 'not_required';
  const ac = (g.asan && g.asan.status) || 'not_required';
  const revShort = String(p.revision || '').slice(0, 10);
  const gatesHtml = `<div class="finalize-gates">` +
    `<span class="badge">PS${esc(p.patchset)} · ${esc(revShort)}</span>` +
    `<span class="badge">Flutter corpus: ${esc(fc)}</span>` +
    `<span class="badge">google3 A/B: ${esc(gc)}</span>` +
    `<span class="badge">ASAN: ${esc(ac)}</span></div>`;

  let draftsBody = '';
  if ((p.drafts || []).length) {
    draftsBody = p.drafts.map(d => {
      const checked = st.draftIds.has(String(d.id)) ? 'checked' : '';
      const idJson = esc(JSON.stringify(String(d.id)));
      const warn = d.warning ? ` <span class="red">(${esc(d.warning)})</span>` : '';
      return `<label class="finalize-item">` +
        `<input type="checkbox" ${checked} ` +
        `onchange="toggleFinalizeChoice('draftIds', ${idJson}, this.checked)">` +
        `<div><b>PS${esc(d.patchset)} · ${esc(d.path)}</b> ` +
        `<span class="triage-meta">(${esc(d.id)})</span>${warn}` +
        `<div class="triage-pre" style="margin-top:4px;">${esc(d.message)}</div></div></label>`;
    }).join('');
  } else if (p.triage_draft_fallback) {
    draftsBody = `<div class="finalize-item"><div>` +
      `<b>PS${esc(p.patchset)} · /PATCHSET_LEVEL (from triage_store)</b>` +
      `<div class="triage-pre" style="margin-top:4px;">${esc(p.triage_draft_fallback)}</div>` +
      `</div></div>`;
  } else {
    draftsBody = `<div class="triage-meta">No pending drafts (will post bare Code-Review+1)</div>`;
  }

  let threadsBody = '';
  if ((p.own_unresolved_threads || []).length) {
    threadsBody = p.own_unresolved_threads.map(t => {
      const checked = st.threadLeaves.has(String(t.leaf_id)) ? 'checked' : '';
      const lidJson = esc(JSON.stringify(String(t.leaf_id)));
      return `<label class="finalize-item">` +
        `<input type="checkbox" ${checked} ` +
        `onchange="toggleFinalizeChoice('threadLeaves', ${lidJson}, this.checked)">` +
        `<div><b>PS${esc(t.patchset)} · ${esc(t.path)}</b> → reply <code>Done</code> ` +
        `(resolve)<div class="triage-meta">${esc(t.root_snippet)}</div></div></label>`;
    }).join('');
  } else {
    threadsBody = `<div class="triage-meta">No open own threads on prior patchsets</div>`;
  }
  if ((p.other_unresolved_threads || []).length) {
    threadsBody += `<div class="triage-meta" style="margin-top:5px;">` +
      `Note: ${esc(p.other_unresolved_threads.length)} unresolved thread(s) from other ` +
      `reviewers remain untouched</div>`;
  }

  let compBody = '';
  if ((p.companions || []).length) {
    compBody = p.companions.map(c => {
      if (c.action === 'abandon') {
        const checked = st.companionIds.has(String(c.id)) ? 'checked' : '';
        const cidJson = esc(JSON.stringify(String(c.id)));
        return `<label class="finalize-item"><input type="checkbox" ${checked} ` +
          `onchange="toggleFinalizeChoice('companionIds', ${cidJson}, this.checked)">` +
          `<div>${link(c.url, `CL ${esc(c.id)}`)} — ${esc(c.label)}</div></label>`;
      }
      return `<div class="finalize-item"><span class="triage-meta">` +
        `${link(c.url, `CL ${esc(c.id)}`)}: ${esc(c.label)}</span></div>`;
    }).join('');
  } else {
    compBody = `<div class="triage-meta">No companion WIP CL linked</div>`;
  }

  let canBody = '';
  if ((p.deferred_canaries || []).length) {
    canBody = p.deferred_canaries.map(c => {
      const checked = st.canaries.has(String(c)) ? 'checked' : '';
      const cJson = esc(JSON.stringify(String(c)));
      return `<label class="finalize-item"><input type="checkbox" ${checked} ` +
        `onchange="toggleFinalizeChoice('canaries', ${cJson}, this.checked)">` +
        `<div>Trigger <b>${esc(c)}</b> + background watcher after CR+1</div></label>`;
    }).join('');
  } else {
    canBody = `<div class="triage-meta">None needed after CR+1</div>`;
  }

  const disabledAttr = (!p.ready || st.submitting)
    ? 'disabled style="opacity:0.5;cursor:not-allowed;"'
    : '';
  const btnText = st.submitting ? 'Publishing…' : '✅ Confirm CR+1 & publish';
  return `${errBanner}${gatesHtml}` +
    `<div class="finalize-sec"><h4>1. Publish Drafts + Vote Code-Review+1 ` +
    `(PUBLISH_ALL_REVISIONS whitelist)</h4>${draftsBody}</div>` +
    `<div class="finalize-sec"><h4>2. Own Prior-Patchset Unresolved Threads</h4>` +
    `${threadsBody}</div>` +
    `<div class="finalize-sec"><h4>3. Companion WIP CL Cleanup</h4>${compBody}</div>` +
    `<div class="finalize-sec"><h4>4. Post-CR+1 External Canaries</h4>${canBody}</div>` +
    `<div class="finalize-foot">` +
    `<button class="triage-btn" onclick="openFinalizeModal(${keyJson})">Refresh preview</button>` +
    `<button class="triage-btn approve" ${disabledAttr} ` +
    `onclick="submitFinalizeReview(${keyJson})">${btnText}</button></div>`;
}

function openTriageModal(key, tab) {
  const it = findItemByKey(key);
  if (!it || !it.triage) return;
  currentTriageModalKey = key;
  currentTriageModalTab = tab || (it.triage.draft ? 'draft' : 'report');
  renderTriageModal(it);
}

function closeTriageModal() {
  currentTriageModalKey = null;
  const root = typeof document !== 'undefined'
    ? document.getElementById('triage-modal-root') : null;
  if (root) root.innerHTML = '';
}

// Review outcomes that encourage CR+1 and must block until the mandatory corpus gate passes
const GATED_LGTM_OUTCOMES = new Set(['CLEAN', 'NITS', 'COMPANION_TESTS']);
const OUTCOME_BADGES = {
  CLEAN: ['🟢', 'Clean (LGTM)', 'Clean'],
  NITS: ['🔵', 'Minor nits', 'Minor nits'],
  COMPANION_TESTS: ['🟣', 'Logic OK + test companion', 'Logic OK + tests'],
  COMPANION_FIX: ['🟠', 'Defects + companion fix', 'Defects + fix'],
  PUSHBACK: ['🔴', 'Do not land / Pushback', 'Pushback'],
  REASSIGN: ['⚪', 'Out of domain', 'Out of domain'],
};

function isTriageStale(it) {
  const tr = it && it.triage;
  if (!tr) return false;
  if (tr.is_stale) return true;
  const revPs = Number(tr.triaged_patchset || it.reviewed_ps || 0);
  return revPs > 0 && Number(it.patchset || 0) > revPs;
}

function corpusDisplayName(tr) {
  if (tr && tr.corpus_script === 'run_webparagraph_corpus.py') {
    return 'WebParagraph corpus';
  } else if (tr && tr.corpus_script === 'run_g3_paragraph_tests.py') {
    return 'Google3 corpus';
  }
  return 'Flutter corpus';
}

// Evaluates corpus gate state and resets passed to pending if patchset or commit drifted
function effectiveCorpusStatus(it) {
  const tr = it && it.triage;
  if (!tr || !tr.corpus_required) return 'not_required';
  if (isTriageStale(it)) return 'pending';
  const st = String(tr.corpus_status || '').toLowerCase();
  if (st === 'failed' || st === 'waived') return st;
  if (st === 'passed') {
    const curPs = Number(it.patchset || tr.triaged_patchset || 1);
    const testedPs = Number(tr.corpus_tested_patchset || tr.triaged_patchset || 0);
    return (testedPs > 0 && curPs === testedPs) ? 'passed' : 'pending';
  }
  return 'pending';
}

// Computes triage badge and eyebrow labels without allowing green LGTM when corpus is blocked
function triageBadgeInfo(it) {
  const tr = (it && it.triage) || {};
  const auditState = String(tr.audit_state || '').toLowerCase();
  if (auditState === 'running' && Boolean(it && it.chat_busy)) {
    const tgtPs = Number(tr.audit_target_patchset || (it && it.patchset) || 1);
    const lbl = `🔄 Auditing PS${tgtPs}…`;
    return {cls: 'triage-auditing', pillCls: 'v-auditing', label: lbl, eyebrow: lbl};
  }
  if (auditState === 'blocked_auth') {
    const lbl = '🔒 Audit paused: run gcert';
    return {cls: 'triage-blocked_auth', pillCls: 'v-blocked_auth', label: lbl, eyebrow: lbl};
  }
  const out = String(tr.review_outcome || tr.verdict || 'SURGICAL_AUDIT').toUpperCase();
  const revPs = Number(tr.triaged_patchset || (it && it.reviewed_ps) || (it && it.patchset) || 1);
  const curPs = Number((it && it.patchset) || 1);
  const stale = isTriageStale(it);
  const cName = corpusDisplayName(tr);
  const cStat = effectiveCorpusStatus(it);
  if (GATED_LGTM_OUTCOMES.has(out) && tr.corpus_required) {
    if (stale) {
      const eyebrow = curPs > revPs
        ? `⏳ Review PS${revPs} → PS${curPs} uploaded (${cName} not run)`
        : `⏳ Review PS${revPs} stale: new commit (${cName} not run)`;
      return {
        cls: 'triage-corpus_blocked', pillCls: 'v-corpus_blocked',
        label: `⏳ LGTM blocked: ${cName} not run`, eyebrow,
      };
    }
    if (cStat === 'pending') {
      const lbl = `⏳ LGTM blocked: ${cName} not run`;
      return {cls: 'triage-corpus_blocked', pillCls: 'v-corpus_blocked', label: lbl, eyebrow: lbl};
    }
    if (cStat === 'failed') {
      const lbl = `🔴 LGTM blocked: ${cName} failed`;
      return {cls: 'triage-corpus_failed', pillCls: 'v-corpus_failed', label: lbl, eyebrow: lbl};
    }
    if (cStat === 'waived') {
      const short = (OUTCOME_BADGES[out] && OUTCOME_BADGES[out][2]) || out;
      return {
        cls: 'triage-corpus_blocked', pillCls: 'v-corpus_blocked',
        label: `⚠️ ${short} (${cName} waived)`,
        eyebrow: `⚠️ Review PS${revPs}: ${short} (${cName} waived)`,
      };
    }
  }
  const meta = OUTCOME_BADGES[out];
  const rawEye = String(tr.review_eyebrow_label || '');
  const baseEye = (rawEye && !rawEye.includes('LGTM blocked') && !rawEye.includes('waived'))
    ? rawEye
    : (meta ? `${meta[0]} Review PS${revPs}: ${meta[1]}`
            : `Review PS${revPs}: ${tr.verdict_label || out}`);
  const eyebrow = stale
    ? (curPs > revPs ? `${baseEye} → PS${curPs} uploaded` : `${baseEye} (new commit)`)
    : baseEye;
  if (tr.review_outcome && meta) {
    return {
      cls: `triage-${out.toLowerCase()}`,
      pillCls: stale ? 'v-stale' : `v-${out.toLowerCase()}`,
      label: `${meta[0]} ${meta[1]}`, eyebrow,
    };
  }
  return {
    cls: `triage-${String(tr.verdict || 'SURGICAL_AUDIT').toLowerCase()}`,
    pillCls: stale ? 'v-stale' : `v-${out.toLowerCase()}`,
    label: String(tr.verdict_label || tr.verdict || 'SURGICAL AUDIT'), eyebrow,
  };
}

function coreHunkUrl(it, hunk, ps) {
  const baseUrl = String((it && it.url) || '').replace(/\/+$/, '');
  const fpath = String((hunk && hunk.file) || '').replace(/^\/+/, '');
  if (!baseUrl || !fpath) return baseUrl;
  if (it && it.kind === 'gerrit') {
    const mLine = String((hunk && hunk.lines) || '').match(/\d+/);
    const anchor = mLine ? `#${mLine[0]}` : '';
    return `${baseUrl}/${ps}/${fpath}${anchor}`;
  }
  return `${baseUrl}/files`;
}

// Convert URLs and CL/PR/bug references in triage report text into safe clickable anchors
function linkifyTriageText(raw, it) {
  const text = String(raw || '');
  if (!text) return '';
  const gHost = (it && it.kind === 'gerrit' && it.host)
    ? String(it.host) : 'skia-review.googlesource.com';
  const ghRepo = (it && it.repo_slug) ? String(it.repo_slug) : 'flutter/flutter';
  const tokenRe = new RegExp(
    '(https?://[^\\s<>)"\'`]+)' +
    '|\\bGerrit\\s+(\\d{5,9})(?:\\s+PS(\\d+))?\\b' +
    '|\\bcrrev/c/(\\d{5,9})(?:\\s+PS(\\d+))?\\b' +
    '|\\bPR\\s+#(\\d{3,8})\\b' +
    '|\\bb/(\\d{6,12})\\b' +
    '|\\bcrbug(?:/|\\.com/)(\\d{5,12})\\b',
    'g'
  );
  let out = '';
  let lastIdx = 0;
  let m;
  while ((m = tokenRe.exec(text)) !== null) {
    out += esc(text.slice(lastIdx, m.index));
    let matched = m[0];
    let trailing = '';
    let href = '';
    if (m[1]) {
      while (/[.,;:!?]$/.test(matched)) {
        trailing = matched.slice(-1) + trailing;
        matched = matched.slice(0, -1);
      }
      href = matched;
    } else if (m[2]) {
      href = `https://${gHost}/c/${m[2]}${m[3] ? '/' + m[3] : ''}`;
    } else if (m[4]) {
      href = `https://chromium-review.googlesource.com/c/${m[4]}${m[5] ? '/' + m[5] : ''}`;
    } else if (m[6]) {
      href = `https://github.com/${ghRepo}/pull/${m[6]}`;
    } else if (m[7]) {
      href = `https://b.corp.google.com/issues/${m[7]}`;
    } else if (m[8]) {
      href = `https://issues.chromium.org/issues/${m[8]}`;
    }
    out += href
      ? `<a href="${esc(href)}" target="_blank" rel="noopener">${esc(matched)}</a>${esc(trailing)}`
      : esc(m[0]);
    lastIdx = m.index + m[0].length;
  }
  out += esc(text.slice(lastIdx));
  return out;
}

function renderTriageModal(it) {
  const root = typeof document !== 'undefined'
    ? document.getElementById('triage-modal-root') : null;
  if (!root || !it || !it.triage) return;
  const tr = it.triage;
  const bInfo = triageBadgeInfo(it);
  const key = it.item_key || it.target_id;
  const keyJson = esc(JSON.stringify(key));
  const hasDraft = Boolean(tr.draft && tr.draft.trim());
  const canFin = canFinalizeCleanReview(it) || currentTriageModalTab === 'finalize';
  const activeTab = currentTriageModalTab || (hasDraft ? 'draft' : 'report');
  const isFinalizeTab = activeTab === 'finalize';
  const content = activeTab === 'draft'
    ? (tr.draft || 'No draft comment required.')
    : (tr.report_markdown || 'No report available.');
  const revPs = Number(tr.triaged_patchset || it.patchset || 1);
  const hunks = Array.isArray(tr.core_hunks) ? tr.core_hunks : [];
  const threads = Array.isArray(tr.open_thread_verdicts) ? tr.open_thread_verdicts : [];

  let structuredHtml = '';
  if (activeTab === 'report') {
    if (hunks.length) {
      const hLinks = hunks.map(h => {
        const baseName = String(h.file || '').split('/').pop() || h.file;
        const lbl = h.lines ? `${baseName}:${h.lines}` : baseName;
        const note = h.note ? ` — ${esc(h.note)}` : '';
        return `<div><a class="triage-hunk-chip" href="${esc(coreHunkUrl(it, h, revPs))}" ` +
          `target="_blank" rel="noopener">${esc(lbl)}</a>${note}</div>`;
      }).join('');
      structuredHtml += `<div class="triage-modal-section"><h4>Core Hunks</h4>${hLinks}</div>`;
    }
    if (threads.length) {
      const tRows = threads.map(tv => {
        const who = tv.author ? `<b>${esc(tv.author)}</b> ` : '';
        const loc = tv.loc ? `<code>${esc(tv.loc)}</code> ` : '';
        const verd = tv.verdict ? `[${esc(tv.verdict)}] ` : '';
        return `<div>${who}${loc}${verd}${esc(tv.note || '')}</div>`;
      }).join('');
      structuredHtml += `<div class="triage-modal-section">` +
        `<h4>Open Thread Verdicts</h4>${tRows}</div>`;
    }
  }
  const renderedContent = activeTab === 'draft'
    ? esc(content)
    : linkifyTriageText(content, it);
  const saveGerritBtn = (hasDraft && it.kind === 'gerrit')
    ? `<button class="triage-btn" style="margin-bottom: 6px;" ` +
      `onclick="saveGerritDraft(${keyJson}, this)">Save to Gerrit Draft</button>`
    : '';
  const bodyHtml = isFinalizeTab
    ? renderFinalizeTabHtml(it)
    : `${structuredHtml}<div class="triage-pre">${renderedContent}</div>`;

  root.innerHTML = `
    <div class="triage-modal-backdrop" onclick="if (event.target === this) closeTriageModal()">
      <div class="triage-modal">
        <div class="triage-modal-head">
          <span class="badge ${esc(bInfo.cls)}">
            ${esc(bInfo.label)}
          </span>
          <!-- Link CL identifier directly to review page -->
          <h3>${link(it.url, esc(clLabel(it)))}: ${esc(cleanSubject(it.subject))}</h3>
          <button class="triage-modal-close" onclick="closeTriageModal()" title="Close">✕</button>
        </div>
        <div class="triage-modal-tabs">
          ${canFin ? `
            <button class="triage-tab${isFinalizeTab ? ' active' : ''}"
              onclick="openFinalizeModal(${esc(JSON.stringify(key))})">
              Approve &amp; Publish
            </button>` : ''}
          ${hasDraft ? `
            <button class="triage-tab${activeTab === 'draft' ? ' active' : ''}"
              onclick="currentTriageModalTab='draft'; ` +
              `renderTriageModal(findItemByKey(${keyJson}))">
              Review Draft
            </button>` : ''}
          <button class="triage-tab${activeTab === 'report' ? ' active' : ''}"
            onclick="currentTriageModalTab='report'; ` +
            `renderTriageModal(findItemByKey(${keyJson}))">
            Triage Report
          </button>
          ${(hasDraft && !isFinalizeTab) ? `
            <span style="margin-left: auto; display: inline-flex; gap: 6px;">
              ${saveGerritBtn}
              <button class="triage-btn primary" style="margin-bottom: 6px;"
                onclick="copyTriageDraft(${keyJson}, this)">Copy Draft</button>
            </span>` : ''}
        </div>
        <div class="triage-modal-body">
          ${bodyHtml}
        </div>
      </div>
    </div>`;
}

async function saveGerritDraft(key, btn) {
  const it = findItemByKey(key);
  if (!it || it.kind !== 'gerrit' || !it.triage || !it.triage.draft) return;
  const orig = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Saving…';
  }
  try {
    const r = await fetch('/api/save_gerrit_draft', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_key: key}),
    });
    const d = await r.json();
    if (btn) {
      btn.textContent = d && d.ok ? `Saved PS${d.patchset || ''} draft ✓` : 'Save failed';
    }
  } catch (e) {
    if (btn) btn.textContent = 'Save failed';
  } finally {
    if (btn) {
      btn.disabled = false;
      setTimeout(() => { btn.textContent = orig; }, 2400);
    }
  }
}

async function copyTriageDraft(key, btn) {
  const it = findItemByKey(key);
  if (!it || !it.triage || !it.triage.draft) return;
  const orig = btn ? btn.textContent : '';
  let copied = false;
  try {
    if (typeof navigator !== 'undefined' &&
        navigator.clipboard &&
        navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(it.triage.draft);
      copied = true;
    }
  } catch (e) {
    copied = false;
  }
  if (!copied && typeof document !== 'undefined') {
    try {
      const ta = document.createElement('textarea');
      ta.value = it.triage.draft;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      copied = document.execCommand('copy');
      document.body.removeChild(ta);
    } catch (e) {
      copied = false;
    }
  }
  if (btn) {
    btn.textContent = copied ? 'Copied!' : 'Copy failed';
    setTimeout(() => { btn.textContent = orig; }, 1800);
  }
}

function triageHtml(it, compact) {
  if (!it || !it.triage) return '';
  if (it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') return '';
  const tr = it.triage;
  const bInfo = triageBadgeInfo(it);
  const badge = `<span class="badge ${esc(bInfo.cls)}">${esc(bInfo.label)}</span>`;
  const time = tr.review_time_label
    ? `<span class="triage-time">${esc(tr.review_time_label)}</span>` : '';
  const deltaMap = {
    rebase_only: 'rebase-only',
    commit_msg_only: 'msg-only',
    thread_only: 'thread-only',
  };
  const deltaLbl = deltaMap[String(tr.delta_kind || '')] || '';
  const deltaPill = deltaLbl
    ? `<span class="badge triage-delta">${esc(deltaLbl)}</span>` : '';
  const summaryText = (String(tr.audit_state || '') === 'blocked_auth' && tr.audit_error)
    ? tr.audit_error : tr.summary;
  const summary = summaryText
    ? `<span class="triage-summary">${esc(summaryText)}</span>` : '';
  const revPs = Number(tr.triaged_patchset || it.patchset || 1);
  const curPs = Number(it.patchset || 1);
  // GitHub PR pushes keep patchset=1 before any review, so distinguish commit drift from PS drift
  const stale = isTriageStale(it)
    ? `<span class="badge triage-stale">${curPs > revPs
        ? `Stale: PS${esc(revPs)} → PS${esc(curPs)}`
        : 'Stale: new commit pushed'}</span>`
    : '';
  // Surface explicit corpus pass confirmation next to the reviewed patchset
  const cStat = effectiveCorpusStatus(it);
  const corpusMeta = (!isTriageStale(it) && cStat === 'passed')
    ? ` · ${esc(corpusDisplayName(tr))} OK`
    : '';
  const meta = tr.triaged_patchset
    ? `<span class="triage-meta">PS${esc(revPs)}${corpusMeta} · ` +
      `${esc(tr.triaged_at_et || '')}</span>`
    : '';

  const key = it.item_key || it.target_id;
  const keyJson = esc(JSON.stringify(key));
  const hasDraft = Boolean(tr.draft && tr.draft.trim());

  let hunksHtml = '';
  if (!compact && Array.isArray(tr.core_hunks) && tr.core_hunks.length) {
    const chips = tr.core_hunks.map(h => {
      const baseName = String(h.file || '').split('/').pop() || h.file;
      const lbl = h.lines ? `${baseName}:${h.lines}` : baseName;
      const tip = h.note ? `${h.file}:${h.lines} — ${h.note}` : `${h.file}`;
      return `<a class="triage-hunk-chip" href="${esc(coreHunkUrl(it, h, revPs))}" ` +
        `title="${esc(tip)}" target="_blank" rel="noopener">${esc(lbl)}</a>`;
    }).join(' ');
    hunksHtml = `<div class="triage-hunks">Hunks: ${chips}</div>`;
  }

  let actions = '';
  if (canFinalizeCleanReview(it)) {
    actions += `<button class="triage-btn approve" ` +
      `onclick="openFinalizeModal(${keyJson})">✅ Approve &amp; publish</button>`;
  }
  if (hasDraft) {
    actions += `<button class="triage-btn primary" ` +
      `onclick="openTriageModal(${keyJson}, 'draft')">Draft</button>`;
    actions += `<button class="triage-btn" ` +
      `onclick="copyTriageDraft(${keyJson}, this)">Copy</button>`;
    if (it.kind === 'gerrit') {
      actions += `<button class="triage-btn" ` +
        `onclick="saveGerritDraft(${keyJson}, this)">Save to Gerrit Draft</button>`;
    }
  }
  const hasOpenThreadVerdicts =
    Array.isArray(tr.open_thread_verdicts) && tr.open_thread_verdicts.length > 0;
  if (tr.report_markdown || hasOpenThreadVerdicts) {
    actions += `<button class="triage-btn" ` +
      `onclick="openTriageModal(${keyJson}, 'report')">Report</button>`;
  }

  return `<div class="triage-box">${badge}${deltaPill}${time}${stale}${summary}${meta}` +
    `<span class="triage-actions">${actions}</span>${hunksHtml}</div>`;
}

// Tracks which (reviewer::item_key) pairs the user unchecked in the Reviewer Pings panel
const excludedPingItems = {};

function pingExclusionKey(reviewer, itemKey) {
  return `${String(reviewer || '')}::${String(itemKey || '')}`;
}

function isPingItemIncluded(reviewer, itemKey) {
  return !excludedPingItems[pingExclusionKey(reviewer, itemKey)];
}

function togglePingItem(reviewer, itemKey, checked) {
  const k = pingExclusionKey(reviewer, itemKey);
  if (checked === undefined) {
    excludedPingItems[k] = !excludedPingItems[k];
  } else {
    excludedPingItems[k] = !checked;
  }
  if (typeof cachedData !== 'undefined' && cachedData && typeof render === 'function') {
    render();
  }
  if (typeof currentPingModalReviewer !== 'undefined' && currentPingModalReviewer === reviewer) {
    renderPingModal(reviewer);
  }
}

const PING_REASON_ORDER = {
  lost_on_rebase: 0,
  updated_after_approval: 1,
  updated_after_review: 2,
  needs_second_approval: 3,
  awaiting_review: 4,
};

// Aggregates open CLs/PRs by reviewer so one Chat ping covers all items for that colleague
function collectReviewerPingGroups(items) {
  const byRev = new Map();
  for (const it of (items || [])) {
    if (!it || it.status === 'MERGED' || it.status === 'ABANDONED' || it.status === 'CLOSED') {
      continue;
    }
    const pings = Array.isArray(it.reviewer_pings) ? it.reviewer_pings : [];
    for (const p of pings) {
      if (!p || !p.reviewer || p.active === false) continue;
      const rev = String(p.reviewer);
      if (!byRev.has(rev)) {
        byRev.set(rev, {
          reviewer: rev,
          reviewer_name: p.reviewer_name || shortActor(rev),
          items: [],
        });
      }
      const grp = byRev.get(rev);
      const key = it.item_key || `${it.kind || 'gerrit'}:${it.target_id}`;
      grp.items.push({
        item: it,
        item_key: key,
        ping: p,
        included: isPingItemIncluded(rev, key),
      });
    }
  }
  const groups = [...byRev.values()];
  for (const g of groups) {
    g.items.sort((a, b) => {
      const rKeyA = a.item._stackRoot || '';
      const rKeyB = b.item._stackRoot || '';
      if (rKeyA && rKeyB && rKeyA === rKeyB) {
        return stackSortCompare(a.item, b.item);
      }
      const oa = PING_REASON_ORDER[a.ping.reason] ?? 9;
      const ob = PING_REASON_ORDER[b.ping.reason] ?? 9;
      if (oa !== ob) return oa - ob;
      return (b.item.activity_ts || 0) - (a.item.activity_ts || 0);
    });
  }
  groups.sort((a, b) => {
    const aRebase = a.items.filter(x => x.ping.reason === 'lost_on_rebase').length;
    const bRebase = b.items.filter(x => x.ping.reason === 'lost_on_rebase').length;
    if (aRebase !== bRebase) return bRebase - aRebase;
    if (a.items.length !== b.items.length) return b.items.length - a.items.length;
    return a.reviewer.localeCompare(b.reviewer);
  });
  return groups;
}

function reviewerFirstName(nameOrEmail) {
  const raw = String(nameOrEmail || '').trim();
  if (!raw) return 'there';
  if (raw.includes('@')) {
    const local = raw.split('@')[0].split(/[._-]/)[0] || raw.split('@')[0];
    return local.charAt(0).toUpperCase() + local.slice(1);
  }
  return raw.split(/\s+/)[0];
}

// Builds a single ready-to-paste English ping message grouped by reason with PS diff URLs
function buildReviewerPingMessage(reviewer, itemsOverride) {
  const sourceItems = itemsOverride ||
    (typeof cachedData !== 'undefined' && cachedData ? visibleItemsFromData(cachedData) : []);
  const groups = collectReviewerPingGroups(sourceItems);
  const grp = groups.find(g => g.reviewer === reviewer);
  if (!grp) return '';
  const selected = grp.items.filter(x => x.included);
  if (!selected.length) return '';

  const first = reviewerFirstName(grp.reviewer_name || grp.reviewer);
  const count = selected.length;
  const intro = count === 1
    ? `Hi ${first}! When you have a moment, could you take a look at this CL?`
    : `Hi ${first}! When you have a moment, could you take a look at these ${count} CLs?`;

  const rebaseList = selected.filter(x => x.ping.reason === 'lost_on_rebase');
  const updatedAfterPlusOne = selected.filter(x => x.ping.reason === 'updated_after_approval');
  const updatedAfterReview = selected.filter(x => x.ping.reason === 'updated_after_review');
  const secondApproval = selected.filter(
    x => x.ping.reason === 'needs_second_approval' || x.ping.reason === 'awaiting_review'
  );

  const fmtBullet = (entry) => {
    const it = entry.item;
    const p = entry.ping;
    const url = p.diff_url || it.url || '';
    const subj = String(it.subject || '').trim();
    const psNote = (p.prev_ps && p.current_ps && p.prev_ps < p.current_ps)
      ? ` (PS${p.prev_ps} -> PS${p.current_ps})`
      : '';
    return `• ${url} — ${subj}${psNote}`;
  };

  const sections = [intro];
  if (rebaseList.length) {
    sections.push(
      'Lost +1 after rebase (no code changes since your +1):\n' +
      rebaseList.map(fmtBullet).join('\n')
    );
  }
  if (updatedAfterPlusOne.length) {
    sections.push(
      'Updated since your +1:\n' +
      updatedAfterPlusOne.map(fmtBullet).join('\n')
    );
  }
  if (updatedAfterReview.length) {
    sections.push(
      'Updated since your review:\n' +
      updatedAfterReview.map(fmtBullet).join('\n')
    );
  }
  if (secondApproval.length) {
    sections.push(
      'Waiting for a second +1:\n' +
      secondApproval.map(fmtBullet).join('\n')
    );
  }
  return sections.join('\n\n');
}

async function copyTextToClipboard(text) {
  if (!text) return false;
  try {
    if (typeof navigator !== 'undefined' &&
        navigator.clipboard &&
        navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (e) {
    // Fall back to hidden textarea copy below
  }
  if (typeof document !== 'undefined') {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      return Boolean(ok);
    } catch (e) {
      return false;
    }
  }
  return false;
}

async function copyReviewerPing(reviewer, btn, customTextId) {
  let text = '';
  if (customTextId && typeof document !== 'undefined') {
    const el = document.getElementById(customTextId);
    if (el && typeof el.value === 'string') text = el.value;
  }
  if (!text) {
    const shown = currentShownItems();
    text = buildReviewerPingMessage(reviewer, shown);
  }
  if (!text) return;
  const orig = btn ? btn.textContent : '';
  const ok = await copyTextToClipboard(text);
  if (btn) {
    btn.textContent = ok ? '✓ Copied to clipboard!' : 'Copy failed';
    setTimeout(() => { btn.textContent = orig; }, 1800);
  }
}

function currentShownItems() {
  const data = typeof cachedData !== 'undefined' ? cachedData : null;
  const all = visibleItemsFromData(data);
  const projOf = it => it.project_name || 'Unassigned';
  const sel = typeof selectedProject !== 'undefined' ? selectedProject : 'ALL';
  return all.filter(it => sel === 'ALL' || projOf(it) === sel);
}

function pingReasonBadgeHtml(p, it) {
  const cur = p.current_ps || it.patchset || 1;
  const prev = p.prev_ps ? `PS${esc(p.prev_ps)} → PS${esc(cur)}` : '';
  if (p.reason === 'lost_on_rebase') {
    const extra = prev ? ` (${prev})` : '';
    return `<span class="badge ping-rebase">🔄 +1 lost on rebase${extra}</span>`;
  }
  if (p.reason === 'updated_after_approval') {
    const extra = prev ? ` (${prev})` : '';
    return `<span class="badge ping-updated">📝 Updated after +1${extra}</span>`;
  }
  if (p.reason === 'updated_after_review') {
    const extra = prev ? ` (${prev})` : '';
    return `<span class="badge ping-updated">📝 Updated after review${extra}</span>`;
  }
  if (p.reason === 'needs_second_approval') {
    return `<span class="badge ping-second">⏳ Needs 2nd +1 (PS${esc(cur)})</span>`;
  }
  return `<span class="badge ping-second">⏳ Awaiting review</span>`;
}

// Constructs ownership attribution and plain-language action guidance for a reviewer ping
function pingOwnershipAndActionText(p, it) {
  const cur = p.current_ps || it.patchset || 1;
  const prev = p.prev_ps;
  const req = Number(it.required_approvals || 2);
  const approved = Number(it.approved_count || 1);
  const typeLbl = clTypeLabel(it);
  let ownerText;
  // Distinguish own changes from external changes where the user voted CR+1
  if (it.is_self) {
    ownerText = `<strong>Your ${typeLbl}</strong>`;
  } else {
    const author = authorLabel(it);
    const voteLbl = it.kind === 'github' ? 'approved' : 'gave CR+1';
    ownerText = `by <strong>${esc(author)}</strong> · You ${voteLbl} (${approved}/${req})`;
  }

  let actionText;
  // Clarify why the reviewer needs attention based on the detected state
  if (p.reason === 'lost_on_rebase') {
    actionText = prev
      ? `No code changes since their +1 on PS${esc(prev)} — ask to restore CR+1 on PS${esc(cur)}`
      : `No code changes since their +1 — ask to restore CR+1 on PS${esc(cur)}`;
  } else if (p.reason === 'updated_after_approval') {
    actionText = prev
      ? `Code changed between PS${esc(prev)} and PS${esc(cur)} — ` +
        `ask to re-check delta and restore CR+1`
      : `Code updated since their approval — ask to re-check delta on PS${esc(cur)}`;
  } else if (p.reason === 'updated_after_review') {
    actionText = prev
      ? `Updated since their review on PS${esc(prev)} — ask to take another look at PS${esc(cur)}`
      : `Updated since their review — ask to take another look at PS${esc(cur)}`;
  } else if (p.reason === 'needs_second_approval') {
    actionText = it.is_self
      ? `Has ${approved} of ${req} approvals — ask for 2nd CR+1 on PS${esc(cur)}`
      : `Needs a 2nd Googler CR+1 to land — ask for second review on PS${esc(cur)}`;
  } else {
    actionText = `Waiting for review on PS${esc(cur)}`;
  }

  const isGh = it.kind === 'github';
  const attNote = p.auto_rerequested
    ? ' <span style="color:var(--blue)">(Flight Deck re-requested review on GitHub)</span>'
    : (p.in_attention_set === false
      ? ` <span style="color:var(--red)">(${isGh
          ? 'Author did not click Re-request review on GitHub'
          : 'Gerrit dropped them from attention set'})</span>`
      : '');
  return `${ownerText} · ${actionText}${attNote}`;
}

function renderPingsSection(shownItems) {
  const groups = collectReviewerPingGroups(shownItems);
  if (!groups.length) return '';
  const totalCls = new Set();
  for (const g of groups) {
    for (const x of g.items) totalCls.add(x.item_key);
  }
  const cards = groups.map(g => {
    const revJson = esc(JSON.stringify(g.reviewer));
    const selectedCount = g.items.filter(x => x.included).length;
    const nRebase = g.items.filter(x => x.ping.reason === 'lost_on_rebase').length;
    const nUpdated = g.items.filter(
      x => x.ping.reason === 'updated_after_approval' || x.ping.reason === 'updated_after_review'
    ).length;
    const nSecond = g.items.filter(
      x => x.ping.reason === 'needs_second_approval' || x.ping.reason === 'awaiting_review'
    ).length;
    const summaryParts = [];
    if (nRebase) summaryParts.push(`${nRebase} lost +1 on rebase`);
    if (nUpdated) summaryParts.push(`${nUpdated} updated after review/+1`);
    if (nSecond) summaryParts.push(`${nSecond} awaiting 2nd +1`);

    const rows = g.items.map(x => {
      const it = x.item;
      const p = x.ping;
      const keyJson = esc(JSON.stringify(x.item_key));
      const isGh = it.kind === 'github';
      const offAtt = p.auto_rerequested
        ? `<span class="badge ping-rebase" ` +
          `title="Flight Deck automatically re-requested review on GitHub">🔄 re-requested</span>`
        : (p.in_attention_set === false
          ? `<span class="badge ping-off-att" title="${isGh
              ? 'Reviewer is not in GitHub reviewRequests (no circle icon)'
              : 'Reviewer is not in Gerrit attention set'}">${isGh
              ? '🔕 no re-request' : '🔕 off attention set'}</span>`
          : '');
      const hasDelta = Boolean(p.prev_ps && p.current_ps && p.prev_ps < p.current_ps);
      const deltaLink = hasDelta
        ? link(p.diff_url, `Diff PS${esc(p.prev_ps)}..${esc(p.current_ps)} ↗`, 'ping-diff')
        : '';
      const openLabel = `Open ${esc(clTypeLabel(it))} ↗`;
      const openLink = link(it.url, openLabel, 'ping-diff');
      const chatHtml = chatActionHtml(
        it, typeof cachedData !== 'undefined' ? cachedData : null, true
      );
      const linksHtml = `<span class="ping-links">${deltaLink}${openLink}${chatHtml}</span>`;
      const clLink = link(it.url, esc(clLabel(it)), 'ping-cl-link');
      const subjLink = link(it.url, esc(cleanSubject(it.subject)), 'ping-subj');
      const whyHtml = `<div class="ping-why">${pingOwnershipAndActionText(p, it)}</div>`;
      return `
        <div class="ping-row${x.included ? '' : ' excluded'}">
          <input type="checkbox" ${x.included ? 'checked' : ''}
            onchange="togglePingItem(${revJson}, ${keyJson}, this.checked)">
          ${stackStepBadgeHtml(it)}${pingReasonBadgeHtml(p, it)}${offAtt}
          <span class="meta"><span class="proj-tag">${esc(it.project_name)}</span> ·
            <span class="cl">${clLink}</span></span>
          ${subjLink}
          ${linksHtml}
          ${whyHtml}
        </div>`;
    }).join('');

    const copyDis = selectedCount === 0 ? ' disabled' : '';
    const btnLabel = `📋 Copy Chat ping (${selectedCount} CL${selectedCount === 1 ? '' : 's'})`;
    return `
      <div class="ping-card">
        <div class="ping-card-top">
          <span class="ping-who">${esc(g.reviewer_name)} (${esc(shortActor(g.reviewer))})</span>
          <span class="ping-counts">${esc(summaryParts.join(' · '))}</span>
          <div class="ping-actions">
            <a class="triage-btn" href="https://chat.google.com/" target="_blank" rel="noopener"
              title="Open Google Chat to paste the ping message">💬 Open Google Chat ↗</a>
            <button class="triage-btn" onclick="openPingModal(${revJson})">Preview / Edit</button>
            <button class="triage-btn primary"${copyDis}
              onclick="copyReviewerPing(${revJson}, this)">${btnLabel}</button>
          </div>
        </div>
        <div class="ping-list">${rows}</div>
      </div>`;
  }).join('');

  return `
    <section class="pings-wrap">
      <div class="pings-head">
        <h3>Reviewer Pings</h3>
        <span class="n">${totalCls.size} CL${totalCls.size === 1 ? '' : 's'} · ` +
          `${groups.length} reviewer${groups.length === 1 ? '' : 's'}</span>
        <span class="hint">Lost +1 on rebase, updated after +1, or waiting for 2nd approval — ` +
          `copy one message per reviewer</span>
      </div>
      ${cards}
    </section>`;
}

let currentPingModalReviewer = null;

function openPingModal(reviewer) {
  currentPingModalReviewer = reviewer;
  renderPingModal(reviewer);
}

function closePingModal() {
  currentPingModalReviewer = null;
  const root = typeof document !== 'undefined'
    ? document.getElementById('ping-modal-root') : null;
  if (root) root.innerHTML = '';
}

function renderPingModal(reviewer) {
  const root = typeof document !== 'undefined'
    ? document.getElementById('ping-modal-root') : null;
  if (!root) return;
  const shown = currentShownItems();
  const msg = buildReviewerPingMessage(reviewer, shown);
  const revJson = esc(JSON.stringify(reviewer));
  root.innerHTML = `
    <div class="triage-modal-backdrop" onclick="if (event.target === this) closePingModal()">
      <div class="triage-modal">
        <div class="triage-modal-head">
          <h3>Ping message for ${esc(reviewer)}</h3>
          <button class="triage-modal-close" onclick="closePingModal()" title="Close">✕</button>
        </div>
        <div class="triage-modal-body">
          <textarea id="ping-modal-textarea" class="triage-pre"
            style="width: 100%; min-height: 220px; border: 1px solid var(--hairline); ` +
            `resize: vertical;">${esc(msg)}</textarea>
        </div>
        <div class="triage-modal-tabs" style="padding: 12px 20px; ` +
          `border-top: 1px solid var(--hairline); border-bottom: 0;">
          <span class="triage-meta">Edit the message if needed, ` +
            `or toggle CL checkboxes in the card behind.</span>
          <button class="triage-btn primary" style="margin-left: auto;"
            onclick="copyReviewerPing(${revJson}, this, 'ping-modal-textarea')">
            📋 Copy to clipboard
          </button>
        </div>
      </div>
    </div>`;
}

if (typeof document !== 'undefined' && typeof document.addEventListener === 'function') {
  document.addEventListener('keydown', (e) => {
    // Escape dismisses whichever modal is open
    if (e.key === 'Escape') {
      closeTriageModal();
      closePingModal();
      return;
    }
    // Keyboard shortcuts 1/2/b switch between CLs and Bugs outside text inputs
    const tag = e.target && e.target.tagName ? String(e.target.tagName).toUpperCase() : '';
    if (tag === 'INPUT' || tag === 'TEXTAREA' || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === '1') setView('cls');
    else if (e.key === '2') setView('bugs');
    else if (e.key === 'b' || e.key === 'B') setView(selectedView === 'bugs' ? 'cls' : 'bugs');
  });
  // Capture non-bubbling <details> toggle events so expanded disclosures survive re-renders
  document.addEventListener('toggle', (e) => {
    const el = e && e.target;
    if (!el || typeof el.getAttribute !== 'function') return;
    const k = el.getAttribute('data-details-key');
    if (!k) return;
    const isOpen = typeof el.open === 'boolean'
      ? el.open
      : (typeof el.hasAttribute === 'function' && el.hasAttribute('open'));
    if (isOpen) openDetailsKeys.add(k);
    else openDetailsKeys.delete(k);
    persistOpenDetails();
  }, true);
}

// Render completed code review outcome or live audit status pill in focus card eyebrow
function reviewVerdictEyebrowHtml(it) {
  // Author and watching items do not display review outcome badges
  if (!isReviewerChange(it)) return '';
  const tr = it && it.triage;
  const isAuditingLive = Boolean(tr && tr.audit_state === 'running' && it.chat_busy);
  const isBlockedAuth = Boolean(tr && tr.audit_state === 'blocked_auth');
  if (tr && (tr.review_outcome || isAuditingLive || isBlockedAuth)) {
    const bInfo = triageBadgeInfo(it);
    return `<span class="verdict-pill ${esc(bInfo.pillCls)}">${esc(bInfo.eyebrow)}</span> · `;
  }
  // Fall back to active companion label when explicit review JSON is not yet saved
  if (Array.isArray(it.companions)) {
    const comp = it.companions.find(
      c => c && c.status !== 'ABANDONED' && c.status !== 'CLOSED'
    );
    if (comp) {
      const basePs = Number(
        comp.companion_base_ps || comp.base_ps || it.reviewed_ps || it.patchset || 1
      );
      const label = `Review PS${basePs}: Companion ${comp.label || ''}`.trim();
      return `<span class="verdict-pill v-companion_fix">🟠 ${esc(label)}</span> · `;
    }
  }
  return '';
}

// Strips autogenerated noise and prefix text from last message snippet
function cleanMsgSnippet(raw) {
  let s = String(raw || '').replace(/^Patch Set \d+:\s*/, '').trim();
  s = s.replace(
    /[\s\S]*?\bwho was added as reviewer owns the following files:[\s\S]*$/i, ''
  ).trim();
  s = s.replace(/\s*Copied Votes:\s*\*[\s\S]*$/i, '').trim();
  const isNoise =
    /^(?:Uploaded patch set \d+\.?|Patch Set \d+ was rebased\.?|Hashtag (?:added|removed):.*)$/i;
  if (isNoise.test(s)) return '';
  return s;
}

// Checks whether a completed review verdict for current PS should replace the trigger message
function hasFreshReviewWithoutNewerReply(it) {
  if (!isReviewerChange(it) || !it.triage) return false;
  if (isTriageStale(it) || !String(it.triage.summary || '').trim()) return false;
  if (it.author_replied_to_me) return false;
  const author = String(it.last_msg_author || '');
  const isSelf = author.startsWith('jlavrova@') || author === 'Rusino';
  const cleanSnippet = cleanMsgSnippet(it.last_msg_snippet);
  if (cleanSnippet && !isSelf) {
    const msgTs = Date.parse(
      String(it.last_msg_date || '').replace(' ', 'T').replace(/(\.\d{3})\d+$/, '$1Z')
    );
    const triTs = Date.parse(String(it.triage.triaged_at || ''));
    if (!Number.isNaN(msgTs) && !Number.isNaN(triTs) && msgTs > triTs + 60000) {
      return false;
    }
  }
  return true;
}

function formatMsgWhy(it) {
  const snippet = esc(cleanMsgSnippet(it.last_msg_snippet));
  if (snippet) {
    const who = esc(shortActor(it.last_msg_author, it));
    const from = link(actorUrl(it, it.last_msg_author), who);
    return `<b>${from}</b> · ${formatStatusTime(it, '', it.last_msg_age)}: ${snippet}`;
  }
  const label = isReviewerChange(it)
    ? 'Added as reviewer'
    : ('Uploaded PS' + esc(it.patchset || 1));
  const t = formatStatusTime(it, '', it.last_msg_age);
  return t ? `${label} · ${t}` : label;
}

function focusCard(it, data) {
  const chatBtn = chatActionHtml(it, data, false);
  const busyBadge = it.chat_busy
    ? '<span class="busy-pill"><i class="dot moving"></i>Chat is working</span> · '
    : '';
  const subject = esc(cleanSubject(it.subject));
  const cardTitle = `${badgesHtml(it, {omitFixes: true})}${link(it.url, subject)}`;
  const openFixBtn = openFixActionHtml(it);
  const fixPrimary = fixPrimaryStatusHtml(it);
  if (isPostSubmitAlert(it)) {
    const ps = it.post_submit || {};
    // Show all affected tasks on multi-bot incidents and include fix-for banners
    const incidents = expandIncidentsForDisplay(ps);
    const blamed = incidents.some(h => h.verdict === 'blamed');
    const baseEyebrow = blamed
      ? 'Broke the Skia tree'
      : esc(ps.alert_reason || 'Post-submit alert');
    const firstFix = Array.isArray(it.fixes) && it.fixes.length ? it.fixes[0] : null;
    const fixPill = firstFix
      ? ` · <span class="fix-stage-pill ${esc(fixStageInfo(firstFix).tone)}">` +
        `${esc(fixStageInfo(firstFix).stageLabel)}</span>`
      : '';
    const sponsorBadge = (!it.is_self && it.is_external)
      ? ' · <span class="badge">Sponsored external</span>'
      : '';
    const eyebrow = `${baseEyebrow}${fixPill}${sponsorBadge}`;
    const first = incidents[0] || {};
    const primary = link(first.task_url || STATUS_SKIA_URL, 'See failure', 'btn primary');
    const landedAt = formatStatusTime(it, it.submitted_et, it.submitted_age);
    const authorHtml = it.is_self
      ? 'You'
      : link(actorUrl(it, it.owner_email || it.owner_name), esc(authorLabel(it)));
    const landed = it.submitted_et ? `${esc(authorLabel(it))} landed it ${landedAt}.` : '';
    const blame = incidents.length
      ? ` Gardener lists ${incidents.length} failing task${incidents.length > 1 ? 's' : ''}.`
      : '';
    // Prefer live fix-for CL progress over initial breakage triage advice
    let adviceText = '';
    if (firstFix && (firstFix.status === 'MERGED' || firstFix.stage === 'merged')) {
      adviceText = 'Fix/revert already landed — waiting for Gardener bot/roller to cycle green.';
    } else if (firstFix) {
      adviceText = 'Fix/revert CL is in review — land it to clear the breakage.';
    } else if (ps.recommended_action) {
      adviceText = ps.recommended_action;
    }
    const adviceHtml = adviceText
      ? `<div class="why-context"><b>Recommended:</b> ${esc(adviceText)}</div>`
      : '';
    const whyContent = fixPrimary
      ? `${fixPrimary}<div class="why-context">${landed}${blame}</div>${adviceHtml}`
      : `${landed}${blame}${adviceHtml}`;
    return `
    <article class="card alert">
      <div><div class="eyebrow">${busyBadge}${eyebrow} · ${esc(it.project_name)} ·
        ${link(it.url, clLabel(it))} · by ${authorHtml}</div>
        <h2>${cardTitle}</h2>
        <div class="why">${whyContent}</div>
        ${parentBugsHtml(it)}
        <!-- Surface fix-for linkage on broken alert card -->
        ${fixesBreakageBannerHtml(it)}</div>
      <div class="actions">${primary}${openFixBtn}${chatBtn}</div>
      ${incidents.length
        ? `<div class="incidents">${incidents.map(incidentHtml).join('')}</div>` : ''}
      ${it.status === 'MERGED' ? flightPath(it, true) : ''}
    </article>`;
  }
  const kindWord = it.kind === 'github' ? 'PR' : 'CL';
  const conflictNote = it.is_self ? 'Merge conflict: rebase needed' : 'Merge conflict';
  let why;
  let actions;
  if (isCompanion(it) && it.ready_to_abandon) {
    const reason = esc(it.ready_to_abandon_reason || 'Review complete');
    why = `<b>Ready to abandon</b>: ${reason} · ${formatStatusTime(it)}`;
    actions = abandonActionHtml([it.item_key], '🗑️ Abandon companion', 'btn primary') +
      link(it.url, `Open ${kindWord}`, 'btn secondary') + chatBtn;
  } else if (hasReadyCompanion(it) && !needsMyReply(it) && !canRebase(it)) {
    const readyComps = readyCompanionsOf(it);
    const readyKeys = readyComps.map(c => c.item_key).filter(Boolean);
    const firstReason = esc(readyComps[0].ready_to_abandon_reason || 'Review complete');
    const compLabels = readyComps.map(c => esc(c.label || c.item_key)).join(', ');
    why = `<b>Companion ${compLabels} ready to abandon</b>: ${firstReason} · ` +
      formatStatusTime(it);
    const btnLbl = readyKeys.length > 1
      ? `🗑️ Abandon companions (${readyKeys.length})`
      : `🗑️ Abandon ${readyComps[0].label || 'companion'}`;
    actions = abandonActionHtml(readyKeys, btnLbl, 'btn primary') +
      link(it.url, `Open ${kindWord}`, 'btn secondary') + chatBtn;
  } else if (canRebase(it)) {
    const redConflict = `<span class="red">${conflictNote}</span>`;
    // Keep failure and comment links alongside conflict note so CQ red never masks conflict
    if (it.phase === 'ALERT_CQ_FAILED') {
      why = `${redConflict} · ${link(checksUrl(it), 'Checks failed')} · ${formatStatusTime(it)}`;
    } else if (needsMyReply(it)) {
      const msgWhy = formatMsgWhy(it);
      why = `${redConflict} · ${msgWhy}`;
    } else {
      why = `${redConflict} · ${formatStatusTime(it)}`;
    }
    actions = rebaseActionHtml(it) +
      link(it.url, `Open ${kindWord}`, 'btn secondary') + chatBtn;
  } else if (it.phase === 'ALERT_CQ_FAILED') {
    // External authors cannot re-trigger Skia CQ+2 or Canary-G3 without a Googler sponsor
    if (it.cq_sync_rebase_needed) {
      why = (!it.is_self && it.is_external)
        ? `SkCQ sync failed applying patchset (rebase onto main needed) · ` +
          `${formatStatusTime(it)} · Rebase in Gerrit or ask author to rebase`
        : `SkCQ sync failed applying patchset — rebase onto main · ${formatStatusTime(it)}`;
    } else if (!it.is_self && it.is_external) {
      why = `Pre-submit CQ / tryjobs failed after your CR+1 (external author) · ` +
        `${formatStatusTime(it)} · Keep Canary-G3 hold unresolved or help author fix`;
    } else {
      why = `The commit queue failed · ${formatStatusTime(it)}`;
    }
    actions = link(checksUrl(it), 'See checks', 'btn primary') + chatBtn;
  } else if (needsSponsorAction(it)) {
    // Render explicit Googler sponsor callout instead of a stale comment trigger line
    const reason = esc(it.sponsor_action_reason || 'Needs Googler sponsor action');
    const conflictPrefix = it.merge_conflict ? `<span class="red">${conflictNote}</span> · ` : '';
    why = `${conflictPrefix}<b>Sponsor action</b>: ${reason} · ${formatStatusTime(it)}`;
    const cands = suggestedReviewersFor(it);
    if (cands.length && itemKeyOf(it)) {
      actions = assignReviewerButtonsHtml([itemKeyOf(it)], cands, '') +
        link(it.url, `Open ${kindWord}`, 'btn secondary') + chatBtn;
    } else {
      actions = link(it.url, `Open ${kindWord}`, 'btn primary') + chatBtn;
    }
  } else {
    // Omit the trigger message when a current-PS review summary is already shown below
    if (hasFreshReviewWithoutNewerReply(it)) {
      why = it.merge_conflict ? `<span class="red">${conflictNote}</span>` : '';
    } else {
      const msgWhy = formatMsgWhy(it);
      why = it.merge_conflict ? `<span class="red">${conflictNote}</span> · ${msgWhy}` : msgWhy;
    }
    actions = link(it.url, `Open ${kindWord}`, 'btn primary') + chatBtn;
  }
  const gapNote = reviewerGapHtml(it);
  const fullWhy = [why, gapNote].filter(Boolean).join(' · ');
  const whyContent = fixPrimary
    ? `${fixPrimary}<div class="why-context">${fullWhy}</div>`
    : fullWhy;
  // Suppress empty .why wrapper so cards with only a triage box have no blank gap
  const whyBoxHtml = whyContent ? `<div class="why">${whyContent}</div>` : '';
  const actionsWithFix = openFixBtn ? actions.replace(chatBtn, `${openFixBtn}${chatBtn}`) : actions;
  const verdictPill = reviewVerdictEyebrowHtml(it);
  const authorHtml = it.is_self
    ? 'You'
    : link(actorUrl(it, it.owner_email || it.owner_name), esc(authorLabel(it)));
  return `
    <article class="card">
      <div><div class="eyebrow">${busyBadge}${verdictPill}${esc(it.project_name)} · ` +
        `${link(it.url, clLabel(it))} · PS${esc(it.patchset || 1)} · by ${authorHtml}</div>
        <h2>${cardTitle}</h2>
        ${whyBoxHtml}
        ${parentBugsHtml(it)}
        <!-- Surface fix-for linkage on pre-submit focus cards -->
        ${brokenAndFixBannerHtml(it)}${fixesBreakageBannerHtml(it)}
        ${companionsBannerHtml(it)}${mergesBannerHtml(it)}
        ${stackBlockerBannerHtml(it, data)}</div>
      <div class="actions">${actionsWithFix}</div>
      ${triageHtml(it, false)}
    </article>`;
}

// Render clickable parent bug chips on CL cards so authors see linked crbug/b/gh issues
function parentBugsHtml(it) {
  const bugs = Array.isArray(it.linked_bugs) ? it.linked_bugs : [];
  if (!bugs.length) return '';
  return '<div class="meta" style="margin-top:3px">' + bugs.map(b => {
    const pri = b.priority ? ` · ${esc(b.priority)}` : '';
    const tip = b.title ? `${b.short_id}: ${b.title}` : b.short_id;
    return link(
      b.url,
      `<span class="badge bug-link">🐛 ${esc(b.short_id)}${pri}</span>`,
      '',
      tip,
    );
  }).join(' ') + '</div>';
}

function rowHtml(it, data) {
  const who = link(actorUrl(it, it.owner_email || it.owner_name), esc(authorLabel(it)));
  // Link CL identifier directly to review host
  const clAnchor = link(it.url, esc(clLabel(it)));
  const bugChips = (Array.isArray(it.linked_bugs) ? it.linked_bugs : []).map(b => {
    const pri = b.priority ? ` ${esc(b.priority)}` : '';
    const tip = b.title ? `${b.short_id}: ${b.title}` : b.short_id;
    return link(b.url, `<span class="badge bug-link">${esc(b.short_id)}${pri}</span>`, '', tip);
  }).join('');
  const meta = [
    `<span class="proj-tag">${esc(it.project_name)}</span>`,
    `<span class="cl">${clAnchor}</span>`,
    bugChips,
    who,
    chatActionHtml(it, data, true),
  ].filter(Boolean).join(' · ');
  return `
    <div class="row">
      <div><div class="title">${badgesHtml(it)}${link(it.url, esc(cleanSubject(it.subject)))}</div>
        <div class="meta">${meta}</div>
        <!-- Surface fix-for linkage and broken bots on lane rows -->
        ${brokenAndFixBannerHtml(it)}${fixesBreakageBannerHtml(it)}
        ${companionsBannerHtml(it)}${mergesBannerHtml(it)}
        ${stackBlockerBannerHtml(it, data)}
        ${triageHtml(it, true)}</div>
      <div class="state">${stateHtml(it)}</div>
      <div class="when">${rowTime(it)}</div>
    </div>`;
}

// Deduplicates working chats by conversation_id so a shared bug+CL chat counts once
function countUniqueBusyChats(items) {
  const seen = new Set();
  let unkeyed = 0;
  for (const it of items) {
    if (!it || !it.chat_busy) continue;
    if (it.conversation_id) seen.add(it.conversation_id);
    else unkeyed += 1;
  }
  return seen.size + unkeyed;
}

const BUG_LANES = [
  {id: 'overdue', title: 'Overdue', hint: 'Past Response / Closure SLO or Due Date'},
  {id: 'has_cl', title: 'Has CL', hint: 'Open bugs with active or merged CLs / PRs'},
  {id: 'assigned', title: 'Assigned', hint: 'Open assigned bugs without a CL yet'},
];

function bugPriorityBadgeHtml(bug) {
  const pri = String(bug.priority || 'P2').toUpperCase();
  const sev = bug.severity ? ` ${String(bug.severity).toUpperCase()}` : '';
  const cls = (pri === 'P0' || pri === 'P1') ? 'pri-high' : 'pri-med';
  return `<span class="badge ${cls}">${esc(pri)}${esc(sev)}</span>`;
}

function chatAnchorHtml(cid, url, label, cls) {
  return `<a class="${cls}" href="${esc(url)}" target="jetski-chat" ` +
    `onclick="return openChat(${esc(JSON.stringify(cid))}, ${esc(JSON.stringify(url))}, event)">` +
    `${label}</a>`;
}

function bugChatActionHtml(bug, data, compact) {
  const chat = chatUrl(bug, data);
  if (chat && bug.conversation_id) {
    const cls = compact ? 'chat' : 'btn quiet';
    if (bug.chat_busy) {
      return chatAnchorHtml(
        bug.conversation_id, chat, compact ? '⏳ working…' : '⏳ Chat working…', cls
      );
    }
    const lbl = bug.chat_archived
      ? (compact ? 'chat (archived)' : 'Open chat (archived)')
      : (compact ? 'chat' : 'Open chat');
    return chatAnchorHtml(bug.conversation_id, chat, lbl, cls);
  }
  if (!bug.bug_key) return '';
  const cls = compact ? 'link-btn chat' : 'btn quiet';
  return `<button class="${cls}" onclick="startChat(${esc(JSON.stringify(bug.bug_key))}, this)">` +
    `${compact ? 'start chat' : 'Start chat'}</button>`;
}

function bugSubtasksHtml(bug, data) {
  const subs = Array.isArray(bug.linked_cls) ? bug.linked_cls : [];
  if (!subs.length) return '';
  // Promote active/tracked CLs ahead of historical LINKED Buganizer references
  const ordered = [...subs].sort((a, b) =>
    Number(Boolean(b.in_needs_you)) - Number(Boolean(a.in_needs_you)) ||
    Number(a.status === 'LINKED') - Number(b.status === 'LINKED')
  );
  const renderSub = sub => {
    const clLink = link(sub.url, esc(sub.label || sub.item_key), 'cl-ref');
    const cleanSubj = cleanSubject(sub.subject || '');
    // Omit synthetic 'Gerrit <id>' placeholder when the CL number is already in clLink
    const tid = sub.target_id || String(sub.item_key || '').split(':')[1] || '';
    const isPlaceholderSubj = sub.status === 'LINKED' && cleanSubj === `Gerrit ${tid}`;
    const subj = (!isPlaceholderSubj && cleanSubj)
      ? `<span class="sub-subj">${esc(cleanSubj)}</span>` : '';
    const st = sub.state_summary ? `<span>${esc(sub.state_summary)}</span>` : '';
    const needBadge = sub.in_needs_you ? '<span class="badge warn">⦿ Needs you</span>' : '';
    // Omit duplicate Open chat on subtask when CL shares the parent bug conversation
    const cLbl = sub.chat_busy ? '⏳ working…' : (sub.is_self ? 'CL chat' : 'review chat');
    const subChat = (!sub.shared_chat && sub.conversation_id)
      ? chatAnchorHtml(sub.conversation_id, chatUrl(sub, data), cLbl, 'chat')
      : '';
    const parts = [clLink, subj, needBadge, st, subChat].filter(Boolean).join(' · ');
    return `<div class="bug-subtask">↳ ${parts}</div>`;
  };
  // Fold >4 subtasks on umbrella tracking bugs so historical CL lists stay compact
  if (ordered.length > 4) {
    const head = ordered.slice(0, 3).map(renderSub).join('');
    const rest = ordered.slice(3);
    const tail = rest.map(renderSub).join('');
    const subKey = `subtasks:${bug.bug_key || bug.short_id || ''}`;
    const subOpen = openDetailsKeys.has(subKey) ? ' open' : '';
    return `<div class="bug-subtasks">${head}<details class="subtask-more" ` +
      `data-details-key="${esc(subKey)}"${subOpen}>` +
      `<summary>+${rest.length} more linked CLs</summary>` +
      `<div class="subtask-more-body">${tail}</div></details></div>`;
  }
  return `<div class="bug-subtasks">${ordered.map(renderSub).join('')}</div>`;
}


function processingStripItemHtml(it, data, type) {
  const isBug = type === 'bug';
  const chatTitleRaw = isBug ? (it.chat_title || it.title || '') : (it.title || '');
  const chatTitle = esc(chatTitleRaw);
  
  const lastModStr = it.chat_last_modified;
  const lastModMs = lastModStr ? Date.parse(lastModStr) : Date.now();
  const elapsedMinutes = Math.max(0, Math.floor((Date.now() - lastModMs) / 60000));
  
  let itemLink = '';
  let subjText = '';
  if (isBug) {
    const bugText = esc(it.issue_id || '');
    itemLink = `<a class="stack-step-cl" href="https://issuetracker.google.com/issues/${bugText}" target="_blank" onclick="event.stopPropagation()">${bugText}</a>`;
    subjText = esc(it.title || '');
  } else {
    itemLink = `<a class="stack-step-cl" href="${esc(it.url || '')}" target="_blank" onclick="event.stopPropagation()">${esc(clLabel(it))}</a>`;
    subjText = esc(cleanSubject(it.subject));
  }
  
  const contentHtml = isBug ? bugFocusCard(it, data) : focusCard(it, data);
  const chatHtml = isBug ? bugChatActionHtml(it, data, true) : chatActionHtml(it, data, true);
  const chatSpan = chatHtml ? `<span class="stack-step-chat" onclick="event.stopPropagation()">· ${chatHtml}</span>` : '';
  const procId = isBug ? (it.bug_key || it.issue_id || '') : itemKeyOf(it);
  const procKey = `proc:${procId}`;
  const procOpen = openDetailsKeys.has(procKey) ? ' open' : '';
  return `
    <details class="stack-step-collapsed processing-item" ` +
      `data-details-key="${esc(procKey)}"${procOpen}>
      <summary>
        <span class="badge stack-step" style="background: var(--bg-hover);">⏳ Processing</span>
        ${itemLink}
        <span class="stack-step-subj">${subjText}</span>
        <span>— ${chatTitle} (${elapsedMinutes} min)</span>
        ${chatSpan}
        <span class="stack-step-hint">click to expand</span>
      </summary>
      <div class="stack-step-expanded">${contentHtml}</div>
    </details>`;
}

// Renders nested milestone merge request and cherry-pick status pills on parent CLs and bugs
function mergesBannerHtml(entity) {
  const list = Array.isArray(entity && entity.merge_children) ? entity.merge_children : [];
  if (!list.length) return '';
  const items = list.map(mc => {
    if (!mc) return '';
    const msLbl = mc.milestone_label || (mc.milestone ? `M${mc.milestone}` : 'Merge');
    const bugTip = mc.bug_short_id
      ? `${mc.bug_short_id}${mc.merge_status ? ` · ${mc.merge_status}` : ''}`
      : msLbl;
    const msHtml = mc.bug_url
      ? link(mc.bug_url, `<b>${esc(msLbl)}</b>`, '', bugTip)
      : `<b>${esc(msLbl)}</b>`;
    const parts = [msHtml];
    if (mc.cl_label) {
      const clRef = mc.cl_url
        ? link(mc.cl_url, esc(mc.cl_label), 'cl-ref', mc.cl_summary || mc.cl_status || '')
        : `<span class="cl-ref">${esc(mc.cl_label)}</span>`;
      const det = mc.cl_summary || mc.cl_status || '';
      parts.push(det ? `${clRef} <span class="soft">(${esc(det)})</span>` : clRef);
    } else if (mc.merge_status) {
      parts.push(`<span class="soft">${esc(mc.merge_status)}</span>`);
    }
    if (mc.cl_needs_you) {
      parts.push('<span class="badge warn">⦿ Needs you</span>');
    }
    return `<span>${parts.join(' ')}</span>`;
  }).filter(Boolean);
  if (!items.length) return '';
  return `<div class="companion-banner merges-banner">` +
    `<span><b>Merges:</b></span> ${items.join('<span class="path-sep">·</span>')}</div>`;
}

function isMergeRequestBug(bug) {
  if (!bug) return false;
  if (bug.is_merge_request_bug || bug.is_security_merge_request) return true;
  return /^\[Merge\s+M\d+\]/i.test(String(bug.title || '').trim());
}

function bugMilestoneNum(bug) {
  if (bug && Number(bug.merge_milestone) > 0) return Number(bug.merge_milestone);
  const m = String((bug && bug.title) || '').trim().match(/^\[Merge\s+M(\d+)\]/i);
  return m ? Number(m[1]) : 9999;
}

function mergeGroupKeyOfBug(bug) {
  if (!isMergeRequestBug(bug)) return '';
  const pid = String(bug.parent_issue_id || bug.merge_group_parent_id || '').trim();
  return pid ? `merge-parent:${pid}` : '';
}

// Collapses sibling [Merge Mxx] bugs sharing parent_issue_id into one Needs Me unit
function groupNeedYouBugUnits(bugs) {
  const units = [];
  const byGroup = new Map();
  for (const b of bugs) {
    if (!b) continue;
    const gk = mergeGroupKeyOfBug(b);
    if (!gk) {
      units.push({kind: 'single', bug: b});
      continue;
    }
    let bucket = byGroup.get(gk);
    if (!bucket) {
      bucket = {
        kind: 'merge_group',
        parentId: gk.slice('merge-parent:'.length),
        bugs: [],
      };
      byGroup.set(gk, bucket);
      units.push(bucket);
    }
    bucket.bugs.push(b);
  }
  for (const u of units) {
    if (u.kind === 'merge_group') {
      u.bugs.sort((a, b) => bugMilestoneNum(a) - bugMilestoneNum(b));
    }
  }
  return units;
}

// Collapses sibling open [Merge Mxx] rows in Has CL so parent + merges do not spam 3 rows
function collapseMergeLaneBugs(bugs, data) {
  const out = [];
  const byGroup = new Map();
  for (const b of bugs) {
    if (!b) continue;
    const gk = mergeGroupKeyOfBug(b);
    if (!gk) {
      out.push({kind: 'single', bug: b});
      continue;
    }
    const pid = gk.slice('merge-parent:'.length);
    const parentBug = data && data.bugs ? data.bugs[`buganizer:${pid}`] : null;
    if (parentBug && parentBug.lane === b.lane && Array.isArray(parentBug.merge_children) &&
        parentBug.merge_children.length) {
      continue;
    }
    let bucket = byGroup.get(gk);
    if (!bucket) {
      bucket = {kind: 'merge_group', parentId: pid, bugs: []};
      byGroup.set(gk, bucket);
      out.push(bucket);
    }
    bucket.bugs.push(b);
  }
  for (const u of out) {
    if (u.kind === 'merge_group') {
      u.bugs.sort((a, b) => bugMilestoneNum(a) - bugMilestoneNum(b));
    }
  }
  return out;
}

function securityMergeCard(grp) {
  const reqs = Array.isArray(grp.requests) ? grp.requests : [];
  const reqLinks = reqs.map(r =>
    `M${esc(r.milestone)} (${link(r.url, esc(r.short_id || ('crbug/' + r.id)))})`
  ).join(' · ');
  const parentShort = grp.parent_short_id || ('crbug/' + grp.parent_id);
  const gates = grp.gates || {};
  const cs = gates.canary_soak || {};
  const gatePills = [];
  if (gates.fixed_by_cls) {
    gatePills.push(`<span class="merge-gate-pill${gates.fixed_by_cls.ok ? '' : ' fail'}">` +
      `${gates.fixed_by_cls.ok ? '✓' : '✗'} Fixed-By CL</span>`);
  }
  if (gates.test_only_followups) {
    gatePills.push(`<span class="merge-gate-pill${gates.test_only_followups.ok ? '' : ' fail'}">` +
      `${gates.test_only_followups.ok ? '✓' : '✗'} Follow-ups test-only</span>`);
  }
  if (cs.canary_version || cs.soak_days !== undefined) {
    const verTxt = cs.canary_version ? `Canary ${esc(cs.canary_version)} · ` : 'Canary ';
    const csOk = gates.canary_soak ? Boolean(gates.canary_soak.ok) : Boolean(grp.hard_gates_ok);
    gatePills.push(`<span class="merge-gate-pill${csOk ? '' : ' fail'}">` +
      `${csOk ? '✓' : '⏳'} ${verTxt}${esc(cs.soak_days ?? 0)}d soak</span>`);
  }
  if (gates.no_revert) {
    gatePills.push(`<span class="merge-gate-pill${gates.no_revert.ok ? '' : ' fail'}">` +
      `${gates.no_revert.ok ? '✓' : '✗'} No main revert</span>`);
  }
  if (gates.cherry_pick) {
    gatePills.push(`<span class="merge-gate-pill${gates.cherry_pick.ok ? '' : ' fail'}">` +
      `${gates.cherry_pick.ok ? '✓' : '✗'} Clean cherry-pick</span>`);
  }
  const pillsHtml = gatePills.length ? `<div class="merge-gates">${gatePills.join('')}</div>` : '';
  const warnHtml = grp.os_warning
    ? `<div class="merge-warn">⚠️ ${esc(grp.os_warning)}</div>` : '';

  const ackKey = grp.group_key || ('merge_group:' + grp.parent_id);
  const ackBtn = `<button class="btn secondary" ` +
    `onclick="ackBug(${esc(JSON.stringify(ackKey))}, this)">✓ Ack</button>`;

  let actionsHtml = '';
  let bodyExtraHtml = '';
  if (!grp.hard_gates_ok) {
    const reasons = Array.isArray(grp.blocking_reasons) && grp.blocking_reasons.length
      ? grp.blocking_reasons : ['Merge gate check failed'];
    bodyExtraHtml = `<div class="merge-blockers">` +
      reasons.map(r => `<div>⛔ ${esc(r)}</div>`).join('') + `</div>`;
    actionsHtml = `${link(grp.parent_url, 'Open parent', 'btn secondary')}${ackBtn}`;
  } else {
    const btnLbl = grp.button_label || `Подтвердить merge ${grp.milestones_label || ''}`.trim();
    actionsHtml = `<button class="btn primary" ` +
      `onclick="confirmMerge(${esc(JSON.stringify(grp.parent_id || ''))}, this)">` +
      `${esc(btnLbl)}</button>` +
      `${link(grp.parent_url, 'Open parent', 'btn secondary')}${ackBtn}`;
    bodyExtraHtml = `<div class="merge-draft-wrap">` +
      `<div class="merge-draft-label">English comment draft (editable before posting):</div>` +
      `<textarea class="merge-comment-draft" id="merge-draft-${esc(grp.parent_id || '')}" ` +
      `rows="3">${esc(grp.comment_draft || '')}</textarea></div>`;
  }

  const badgeBug = {priority: grp.priority || 'P1', severity: grp.severity || ''};
  const titleTxt = `[Merge ${grp.milestones_label || ''}] ${grp.parent_title || ''}`.trim();
  return `<article class="card alert">` +
    `<div><div class="eyebrow">${esc(grp.project_name || 'Skia')} · ` +
    `Security merge request · Parent ${link(grp.parent_url, esc(parentShort))}` +
    `${reqLinks ? ` · ${reqLinks}` : ''}</div>` +
    `<h2>${bugPriorityBadgeHtml(badgeBug)}${link(grp.parent_url, esc(titleTxt))}</h2>` +
    `${pillsHtml}</div>` +
    `<div class="actions">${actionsHtml}</div>` +
    `${warnHtml}${bodyExtraHtml}</article>`;
}

function groupedMergeFocusCard(siblings, parentId, data) {
  // Delegate Pending gate evaluation to securityMergeCard before comment/reassign phases
  const phases = new Set(siblings.map(b => String(b.merge_action_phase || '')));
  const mGrp = parentId && data && data.merge_request_groups && data.merge_request_groups[parentId];
  if (
    mGrp &&
    typeof securityMergeCard === 'function' &&
    !mGrp.all_already_commented &&
    !phases.has('reassign') &&
    !phases.has('manual_cherry_pick')
  ) {
    return securityMergeCard(mGrp);
  }
  const first = siblings[0];
  const msLabels = siblings.map(b => {
    const n = bugMilestoneNum(b);
    return n < 9999 ? `M${n}` : (b.short_id || b.id || 'Merge');
  });
  const msJoined = msLabels.join('/');
  const baseTitle = String(first.title || '').replace(/^\[Merge\s+M\d+\]\s*/i, '').trim() ||
    first.title || `Merge ${msJoined}`;
  const parentBug = (data && data.bugs && parentId) ? data.bugs[`buganizer:${parentId}`] : null;
  const parentUrl = (parentBug && parentBug.url) ||
    (parentId ? `https://issues.chromium.org/issues/${parentId}` : '');
  const parentChip = parentId
    ? ` · parent ${link(parentUrl, esc(`crbug/${parentId}`))}` : '';
  const childLinks = siblings.map((b, i) =>
    link(b.url, `${esc(b.short_id || b.id)} (${esc(msLabels[i])})`)
  ).join(', ');
  const issueIds = siblings.map(b => String(b.id || '')).filter(Boolean);
  const bugKeys = siblings.map(b => String(b.bug_key || '')).filter(Boolean);
  const bulkUrl = issueIds.length
    ? `https://issues.chromium.org/issues?q=id:(${issueIds.join('%20OR%20')})`
    : first.url;
  let whyTxt = '';
  let primaryBtn = '';
  if (phases.has('reassign') || Boolean(mGrp && mGrp.all_already_commented)) {
    whyTxt = `${msJoined} merge comment posted — reassign to merges@chromium.org`;
    primaryBtn = `<button class="btn primary" onclick="reassignMerge(` +
      `${esc(JSON.stringify(issueIds))}, ${esc(JSON.stringify(parentId || ''))}, this)">` +
      `Reassign ${esc(msJoined)} to merges@chromium.org</button>` +
      link(bulkUrl, `Open ${esc(msJoined)} in Buganizer`, 'btn secondary');
  } else if (phases.has('manual_cherry_pick')) {
    const branches = siblings.map(b => {
      const n = bugMilestoneNum(b);
      return n < 9999 ? `chrome/m${n}` : 'release branch';
    }).join(', ');
    whyTxt = `${msJoined} merge approved — manual cherry-pick onto ${branches} needed`;
    primaryBtn = link(bulkUrl, `Open ${esc(msJoined)}`, 'btn primary');
  } else {
    whyTxt = `${msJoined} merge request pending — confirm`;
    primaryBtn = link(bulkUrl, `Open ${esc(msJoined)}`, 'btn primary');
  }
  const ackBtn = `<button class="btn secondary" ` +
    `onclick="ackBugs(${esc(JSON.stringify(bugKeys))}, this)">✓ Ack</button>`;
  const chatTarget = parentBug || first;
  const chatBtn = bugChatActionHtml(chatTarget, data, false);
  return `<article class="card alert">` +
    `<div><div class="eyebrow">Security merge request · ${esc(first.project_name || 'Skia')}` +
    `${parentChip} · ${childLinks}</div>` +
    `<h2>${bugPriorityBadgeHtml(first)}[${esc(msJoined)}] ${link(bulkUrl, esc(baseTitle))}</h2>` +
    `<div class="why">${esc(whyTxt)}</div>` +
    `<div class="merge-reassign-status"></div></div>` +
    `<div class="actions">${primaryBtn}${ackBtn}${chatBtn}</div>` +
    `${bugSubtasksHtml(first, data)}</article>`;
}

function groupedMergeLaneRowHtml(siblings, parentId, data) {
  if (siblings.length === 1) return bugRowHtml(siblings[0], data);
  const first = siblings[0];
  const msLabels = siblings.map(b => {
    const n = bugMilestoneNum(b);
    return n < 9999 ? `M${n}` : (b.short_id || b.id);
  });
  const msJoined = msLabels.join('/');
  const baseTitle = String(first.title || '').replace(/^\[Merge\s+M\d+\]\s*/i, '').trim() ||
    first.title;
  const issueIds = siblings.map(b => String(b.id || '')).filter(Boolean);
  const bulkUrl = issueIds.length
    ? `https://issues.chromium.org/issues?q=id:(${issueIds.join('%20OR%20')})`
    : first.url;
  const childLinks = siblings.map((b, i) =>
    link(b.url, `${esc(msLabels[i])}`, 'cl-ref', b.short_id || b.id)
  ).join(' · ');
  const ownerActor = (first.assignee && first.assignee !== 'N/A') ? shortActor(first.assignee) : '';
  const parentUrl = parentId ? `https://issues.chromium.org/issues/${parentId}` : '';
  const meta = [
    `<span class="proj-tag">${esc(first.project_name || 'Skia')}</span>`,
    childLinks,
    parentId ? link(parentUrl, esc(`parent crbug/${parentId}`)) : '',
    ownerActor ? `owner: ${esc(ownerActor)}` : '',
  ].filter(Boolean).join(' · ');
  const whenTxt = first.modified_et
    ? `${esc(first.modified_et)}${first.modified_age ? ` (${esc(first.modified_age)})` : ''}` : '';
  return `<div class="row"><div><div class="title">${bugPriorityBadgeHtml(first)}` +
    `[${esc(msJoined)}] ${link(bulkUrl, esc(baseTitle))}</div>` +
    `<div class="meta">${meta}</div></div>` +
    `<div class="state">${siblings.length} merge requests</div>` +
    `<div class="when">${whenTxt}</div></div>`;
}

function bugFocusCard(bug, data) {
  if (isMergeRequestBug(bug)) {
    const pid = String(bug.parent_issue_id || bug.merge_group_parent_id || '').trim();
    return groupedMergeFocusCard([bug], pid, data);
  }
  // Reserve red alert styling on P0 issues for owned bugs, not collaborator consults
  const isAlert = Boolean(bug.is_overdue || bug.issue_type === 'VULNERABILITY' ||
    (bug.priority === 'P0' && !bug.is_collaborator));
  const busyBadge = bug.chat_busy
    ? '<span class="busy-pill"><i class="dot moving"></i>Chat is working</span> · ' : '';
  const ownerActor = (bug.assignee && bug.assignee !== 'N/A') ? shortActor(bug.assignee) : '';
  const collabTag = bug.is_collaborator
    ? ` · Collaborator${ownerActor ? ` (owner: ${esc(ownerActor)})` : ''}` : '';
  const typeTag = (bug.issue_type && bug.issue_type !== 'BUG') ? ` · ${esc(bug.issue_type)}` : '';
  const sloPart = bug.slo_summary ? ` · <span class="red">${esc(bug.slo_summary)}</span>` : '';
  const ackBtn = `<button class="btn secondary" ` +
    `onclick="ackBug(${esc(JSON.stringify(bug.bug_key || ''))}, this)">✓ Ack</button>`;
  return `<article class="card${isAlert ? ' alert' : ''}">` +
    `<div><div class="eyebrow">${busyBadge}${esc(bug.project_name || 'Skia')} · ` +
    `${link(bug.url, esc(bug.short_id || bug.bug_key))}${collabTag}${typeTag}</div>` +
    `<h2>${bugPriorityBadgeHtml(bug)}${link(bug.url, esc(bug.title))}</h2>` +
    `<div class="why">${esc(bug.why_needs_you || 'Needs triage')}${sloPart}</div></div>` +
    `<div class="actions">${link(bug.url, 'Open issue', 'btn primary')}` +
    `${ackBtn}${bugChatActionHtml(bug, data, false)}</div>` +
    `${bugSubtasksHtml(bug, data)}${mergesBannerHtml(bug)}</article>`;
}

function bugRowHtml(bug, data) {
  const ownerActor = (bug.assignee && bug.assignee !== 'N/A') ? shortActor(bug.assignee) : '';
  const actorMeta = bug.is_collaborator
    ? `Collaborator${ownerActor ? ` · owner: ${esc(ownerActor)}` : ''}`
    : (ownerActor ? esc(ownerActor) : '');
  const meta = [
    `<span class="proj-tag">${esc(bug.project_name || 'Skia')}</span>`,
    `<span class="cl">${link(bug.url, esc(bug.short_id || bug.bug_key))}</span>`,
    actorMeta,
    bugChatActionHtml(bug, data, true),
  ].filter(Boolean).join(' · ');
  const subs = Array.isArray(bug.linked_cls) ? bug.linked_cls : [];
  const dupId = String(bug.duplicate_of || '').trim();
  const isDup = String(bug.status || '').toUpperCase() === 'DUPLICATE' && dupId;
  const dupHtml = isDup ? (() => {
    const isCr = bug.tracker_flavor === 'crbug';
    const dupUrl = isCr
      ? `https://issues.chromium.org/issues/${dupId}`
      : `https://issuetracker.google.com/issues/${dupId}`;
    return `DUPLICATE → ${link(dupUrl, esc(`${isCr ? 'crbug' : 'b'}/${dupId}`))}`;
  })() : '';
  const stateTxt = bug.slo_summary
    ? `<span class="${bug.is_overdue ? 'red' : 'amber'}">${esc(bug.slo_summary)}</span>`
    : (dupHtml || (subs.length ? `${subs.length} linked CL${subs.length > 1 ? 's' : ''}`
      : esc(bug.status || 'ASSIGNED')));
  const whenTxt = bug.modified_et
    ? `${esc(bug.modified_et)}${bug.modified_age ? ` (${esc(bug.modified_age)})` : ''}` : '';
  const subtasks = `${bugSubtasksHtml(bug, data)}${mergesBannerHtml(bug)}`;
  return `<div class="row"><div><div class="title">${bugPriorityBadgeHtml(bug)}` +
    `${link(bug.url, esc(bug.title))}</div><div class="meta">${meta}</div>` +
    `${subtasks}</div><div class="state">${stateTxt}</div>` +
    `<div class="when">${whenTxt}</div></div>`;
}

function toggleSection(secId) {
  const v = selectedView === 'bugs' ? 'bugs' : 'cls';
  selectedSection[v] = selectedSection[v] === secId ? null : secId;
  try {
    if (typeof localStorage !== 'undefined') {
      localStorage.setItem(`flight_deck_section_${v}`, selectedSection[v] || '');
    }
  } catch (e) {
    // Ignore storage quota or security errors in sandboxed iframes
  }
  syncLocationHash();
  render();
}

function laneSectionBodyHtml(title, count, hint, itemsHtml) {
  const hintHtml = hint ? `<span class="hint">${hint}</span>` : '';
  return `<div class="lane-head"><h3>${title}</h3><span class="n">${count}</span>` +
    `${hintHtml}</div>${itemsHtml}`;
}

// Renders a single-select toolbar of non-empty secondary sections with 0 or 1 open panel
function renderSecondarySectionsHtml(view, sections) {
  if (!sections.length) return '';
  const activeId = selectedSection[view];
  const openId = sections.some(s => s.id === activeId) ? activeId : null;
  const tabs = sections.map(s => {
    const isOn = openId === s.id;
    return `<button type="button" class="sec-tab${isOn ? ' on' : ''}" ` +
      `aria-expanded="${isOn ? 'true' : 'false'}" ` +
      `onclick="toggleSection(${esc(JSON.stringify(s.id))})">` +
      `${s.title}<span class="n">${s.count}</span></button>`;
  }).join('');
  const panels = sections.map(s => {
    const isOn = openId === s.id;
    const cls = isOn ? 'lane sec-panel open' : 'lane sec-panel';
    const hid = isOn ? '' : ' hidden';
    return `<section class="${cls}" data-section="${esc(s.id)}"${hid}>${s.bodyHtml}</section>`;
  }).join('');
  return `<div class="sec-tabs" role="toolbar" aria-label="Secondary sections">${tabs}</div>` +
    panels;
}

function renderProjectChips(items, projects) {
  const projOf = it => it.project_name || 'Unassigned';
  const chip = (val, lbl, n) => `<button class="proj${selectedProject === val ? ' on' : ''}" ` +
    `onclick="setProject(${esc(JSON.stringify(val))})">${esc(lbl)}` +
    `<span class="n">${n}</span></button>`;
  document.getElementById('projects').innerHTML = chip('ALL', 'All', items.length) +
    projects.map(p => chip(p, p, items.filter(it => projOf(it) === p).length)).join('');
}

function renderViewSwitchHtml(clNeedYou, clTotal, bugNeedYou, bugTotal) {
  const badge = (need, tot) => need > 0
    ? `<span class="v-you">● ${need}</span>` : `<span class="v-count">${tot}</span>`;
  return `<button class="view-tab${selectedView === 'cls' ? ' on' : ''}" role="tab" ` +
    `onclick="setView('cls')">CLs ${badge(clNeedYou, clTotal)}</button>` +
    `<button class="view-tab${selectedView === 'bugs' ? ' on' : ''}" role="tab" ` +
    `onclick="setView('bugs')">Bugs ${badge(bugNeedYou, bugTotal)}</button>`;
}

function syncLocationHash() {
  const parts = [];
  const activeKey = selectedView === 'bugs' ? 'bugs' : 'cls';
  if (selectedView === 'bugs') parts.push('view=bugs');
  if (selectedProject !== 'ALL') parts.push(`project=${encodeURIComponent(selectedProject)}`);
  if (selectedSection[activeKey]) {
    parts.push(`section=${encodeURIComponent(selectedSection[activeKey])}`);
  }
  if (typeof history !== 'undefined' && typeof history.replaceState === 'function') {
    history.replaceState(null, '', parts.length ? `#${parts.join('&')}` : '#');
  }
}

function setView(v) {
  selectedView = v === 'bugs' ? 'bugs' : 'cls';
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem('flight_deck_view', selectedView);
  } catch (e) {
    // Ignore storage quota or security errors in sandboxed iframes
  }
  syncLocationHash();
  render();
}

function busyChatsSentence(busyCount) {
  if (!busyCount) return '';
  if (busyCount === 1) return 'One chat is working.';
  const countWord = (COUNT_WORDS[busyCount] || `${busyCount} things`).replace(/things?$/, 'chats');
  return `${countWord} are working.`;
}

function headlineHtml(n, project, busyCount = 0) {
  const scope = project === 'ALL' ? '' : ` in ${esc(project)}`;
  const busy = busyChatsSentence(busyCount);
  if (n === 0 && busyCount > 0) {
    return `Nothing${scope} needs you right now. <span class="soft">${busy}</span>`;
  }
  if (n === 0) return `Nothing${scope} needs you. <span class="soft">Go build something.</span>`;
  const word = COUNT_WORDS[n] || `${n} things`;
  const base = `${word}${scope} ${n === 1 ? 'needs' : 'need'} you.`;
  return busy ? `${base} <span class="soft">${busy}</span>` : base;
}

function projectsOf(items) {
  const names = [...new Set(items.map(it => it.project_name || 'Unassigned'))];
  return names.sort((a, b) => {
    const ia = PROJECT_ORDER.indexOf(a);
    const ib = PROJECT_ORDER.indexOf(b);
    if (ia !== -1 || ib !== -1) return (ia === -1 ? 99 : ia) - (ib === -1 ? 99 : ib);
    return a.localeCompare(b);
  });
}

function setProject(p) {
  selectedProject = p;
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem('flight_deck_project', p);
  } catch (e) {
    // Ignore storage quota or security errors in sandboxed iframes
  }
  syncLocationHash();
  render();
}

// Posts lifecycle heartbeats to the embedding Jetski drawer when framed
function postParentMessage(payload) {
  if (typeof window === 'undefined' || !window.parent || window.parent === window) return;
  if (typeof window.parent.postMessage !== 'function') return;
  try {
    window.parent.postMessage(payload, '*');
  } catch (e) {
    // Ignore cross-origin frame teardown races
  }
}

// Reads sessionStorage or localStorage without throwing inside sandboxed frames
function readClientStorage(key, sessionFirst = true) {
  const stores = [];
  try {
    if (sessionFirst && typeof sessionStorage !== 'undefined') stores.push(sessionStorage);
  } catch (e) {}
  try {
    if (typeof localStorage !== 'undefined') stores.push(localStorage);
  } catch (e) {}
  try {
    if (!sessionFirst && typeof sessionStorage !== 'undefined') stores.push(sessionStorage);
  } catch (e) {}
  for (const st of stores) {
    try {
      const val = st.getItem(key);
      if (val !== null && val !== undefined) return String(val);
    } catch (e) {}
  }
  return null;
}

// Writes UI state across reloads, tolerating quota or sandbox storage failures
function writeClientStorage(key, val, includeLocal = true) {
  const str = String(val);
  try {
    if (typeof sessionStorage !== 'undefined') sessionStorage.setItem(key, str);
  } catch (e) {}
  if (!includeLocal) return;
  try {
    if (typeof localStorage !== 'undefined') localStorage.setItem(key, str);
  } catch (e) {}
}

// Caps persisted open <details> keys so storage stays small across long sessions
function persistOpenDetails() {
  writeClientStorage(OPEN_DETAILS_STORAGE_KEY, JSON.stringify([...openDetailsKeys].slice(-200)));
}

// Stores scroll offset in sessionStorage so each tab keeps its own viewport position
function persistScrollPosition(y) {
  const cleanY = Math.max(0, Math.round(Number(y) || 0));
  writeClientStorage(SCROLL_Y_STORAGE_KEY, String(cleanY), false);
}

// Excludes transient _stack links and unused multi-hundred-KB item blobs from JSON snapshots
function stripTransientStatusFields(k, v) {
  return k.startsWith('_stack') ||
    k === 'commit_message' ||
    k === 'revision_ps_by_sha' ||
    k === 'gmail'
    ? undefined
    : v;
}

// Persists compact status snapshot without ui_version to avoid warm-boot reload loops
function persistCachedStatus(data) {
  if (!data || typeof data !== 'object') return;
  try {
    const compactJson = JSON.stringify(
      {
        items: data.items || {},
        bugs: data.bugs || {},
        merge_request_groups: data.merge_request_groups || {},
        bug_sync_errors: data.bug_sync_errors || [],
        rollers_summary: data.rollers_summary || {},
        gardener_url: data.gardener_url || '',
        jetski_box_url: data.jetski_box_url || '',
        last_sync: data.last_sync || '',
      },
      stripTransientStatusFields,
    );
    writeClientStorage(CACHED_STATUS_STORAGE_KEY, compactJson);
  } catch (e) {}
}

// Flushes open disclosures and scroll offset before navigating or reloading the document
function reloadDashboardPage() {
  if (typeof location === 'undefined' || typeof location.reload !== 'function') return false;
  syncOpenDetailsFromDom();
  persistOpenDetails();
  persistScrollPosition(captureScrollPosition());
  location.reload();
  return true;
}

// Restores either live in-page scrollY or one-shot boot scrollY after innerHTML replacement
function applyRenderScrollPosition(savedScrollY) {
  const targetY = savedScrollY > 0 ? savedScrollY : pendingBootScrollY;
  pendingBootScrollY = 0;
  restoreScrollPosition(targetY);
  persistScrollPosition(targetY);
}

// Notifies the parent drawer only after a live /api/status payload rendered in this frame
function notifyParentRendered(data) {
  if (!data || !hasLiveStatus) return;
  const ver = Number((data && data.ui_version) || cachedUiVersion || 0);
  postParentMessage({
    type: 'jetski-flight-deck-rendered',
    uiVersion: ver,
    synced: String((data && data.last_sync) || ''),
  });
}

// Computes a cycle-safe content fingerprint excluding unused blobs and sync wall-clock strings
function statusRenderSignature(data) {
  if (!data || typeof data !== 'object') return '';
  return JSON.stringify(
    {
      items: data.items || {},
      bugs: data.bugs || {},
      merge_request_groups: data.merge_request_groups || {},
      bug_sync_errors: data.bug_sync_errors || [],
      rollers_summary: data.rollers_summary || {},
      gardener_url: data.gardener_url || '',
      jetski_box_url: data.jetski_box_url || '',
    },
    stripTransientStatusFields,
  );
}

// Snapshots mounted <details data-details-key> open states before innerHTML replacement
function syncOpenDetailsFromDom() {
  if (typeof document === 'undefined' || typeof document.querySelectorAll !== 'function') return;
  const nodes = document.querySelectorAll('details[data-details-key]');
  if (!nodes || !nodes.length) return;
  let changed = false;
  for (const el of nodes) {
    if (!el || typeof el.getAttribute !== 'function') continue;
    const k = el.getAttribute('data-details-key');
    if (!k) continue;
    const isOpen = typeof el.open === 'boolean'
      ? el.open
      : (typeof el.hasAttribute === 'function' && el.hasAttribute('open'));
    if (isOpen && !openDetailsKeys.has(k)) {
      openDetailsKeys.add(k);
      changed = true;
    } else if (!isOpen && openDetailsKeys.has(k)) {
      openDetailsKeys.delete(k);
      changed = true;
    }
  }
  if (changed) persistOpenDetails();
}

// Reads viewport vertical offset from either window or root documentElement
function captureScrollPosition() {
  const winY = (typeof window !== 'undefined' && typeof window.scrollY === 'number')
    ? window.scrollY : 0;
  const docEl = (typeof document !== 'undefined' && document.documentElement) || null;
  const docY = (docEl && typeof docEl.scrollTop === 'number') ? docEl.scrollTop : 0;
  return Math.max(winY, docY);
}

// Reapplies vertical scroll offset after synchronous innerHTML replacement
function restoreScrollPosition(savedY) {
  if (!(savedY > 0)) return;
  const docEl = (typeof document !== 'undefined' && document.documentElement) || null;
  if (docEl && typeof docEl.scrollTop === 'number') {
    docEl.scrollTop = savedY;
  }
  if (typeof window !== 'undefined' && typeof window.scrollTo === 'function') {
    window.scrollTo(0, savedY);
  } else if (typeof window !== 'undefined' && typeof window.scrollY === 'number') {
    window.scrollY = savedY;
  }
}

// Checks both tracked buttons and DOM attribute so tests and inline buttons pause re-renders
function hasActiveConfirmation() {
  if (confirmingButtons.size > 0) return true;
  if (typeof document !== 'undefined' && typeof document.querySelector === 'function') {
    return Boolean(document.querySelector('[data-confirming="1"]'));
  }
  return false;
}

function render() {
  const data = cachedData;
  if (!data) return;
  syncOpenDetailsFromDom();
  const savedScrollY = captureScrollPosition();
  lastRenderedDataSig = statusRenderSignature(data);
  pendingDeferredRender = false;
  confirmingButtons.clear();
  const all = visibleItemsFromData(data)
    .sort((a, b) => (b.activity_ts || 0) - (a.activity_ts || 0));
  buildStackContext(all);
  const allBugs = Object.values(data.bugs || {})
    .filter(b => b && typeof b === 'object')
    .sort((a, b) => (b.modified_ts || 0) - (a.modified_ts || 0));
  const projOf = it => it.project_name || 'Unassigned';
  const bugProjects = projectsOf(allBugs);
  const projects = projectsOf(all);
  // Reset stale project filter before computing top-bar counts and keep URL hash synced
  const activeProjects = selectedView === 'bugs' ? bugProjects : projects;
  if (selectedProject !== 'ALL' && !activeProjects.includes(selectedProject)) {
    selectedProject = 'ALL';
    syncLocationHash();
  }

  // Compute global Needs You badges for the top-bar [CLs | Bugs] switch
  const projFilteredCls = selectedProject === 'ALL'
    ? all : all.filter(it => projOf(it) === selectedProject);
  const clYouAll = projFilteredCls.filter(it => laneOf(it) === 'you');
  const clNeedCount = clYouAll.filter(it => !it.chat_busy).length;
  const projFilteredBugs = selectedProject === 'ALL'
    ? allBugs : allBugs.filter(b => projOf(b) === selectedProject);
  const bugYouAll = projFilteredBugs.filter(b => (b.lane || 'assigned') === 'you');
  const bugNeedCount = groupNeedYouBugUnits(bugYouAll.filter(b => !b.chat_busy)).length;
  const viewSwitchEl = document.getElementById('view-switch');
  if (viewSwitchEl) {
    viewSwitchEl.innerHTML = renderViewSwitchHtml(
      clNeedCount, projFilteredCls.length, bugNeedCount, projFilteredBugs.length
    );
  }

  const r = data.rollers_summary || {};
  document.getElementById('rollers').innerHTML = PRODUCTS.map(p => {
    const x = r[p] || {};
    const tip = `${p}: ${x.badge || ''} · ${x.num_behind ?? '?'} commits behind`;
    return link(x.url, `<i class="dot ${x.num_failed ? 'moving' : 'ok'}"></i>` +
      `<span class="name">${p}</span>`, '', tip);
  }).join('') + (data.gardener_url ? link(data.gardener_url, 'Gardener') : '');
  // Keep Loading… indicator visible while showing warm-cached DOM before live fetch resolves
  document.getElementById('synced').textContent = hasLiveStatus
    ? (data.last_sync || '')
    : (data.last_sync ? `Loading… · ${data.last_sync}` : 'Loading…');

  // Render the Bugs tab with its own project chips, Needs You focus cards, and lanes
  if (selectedView === 'bugs') {
    renderProjectChips(allBugs, bugProjects);
    const shownBugs = allBugs.filter(
      b => selectedProject === 'ALL' || projOf(b) === selectedProject
    );
    const byBug = {you: [], overdue: [], has_cl: [], assigned: [], fixed: [], closed: []};
    for (const b of shownBugs) byBug[byBug[b.lane] ? b.lane : 'assigned'].push(b);
    // Keep idle bugs needing human action above bugs whose agent chat is already running
    byBug.you.sort((a, b) => Number(Boolean(a.chat_busy)) - Number(Boolean(b.chat_busy)));
    const busyBugs = countUniqueBusyChats(
      [...shownBugs, ...shownBugs.flatMap(b => b.linked_cls || [])]
    );
    const needYouBugs = byBug.you.filter(b => !b.chat_busy);
    const busyYouBugs = byBug.you.filter(b => b.chat_busy);
    const needYouUnits = groupNeedYouBugUnits(needYouBugs);
    const needBugs = needYouUnits.length;
    document.getElementById('headline').innerHTML = headlineHtml(
      needBugs, selectedProject, busyBugs
    );
    // Secondary bug counts now appear on the .sec-tabs pills below #focus
    document.getElementById('subline').textContent = '';
    document.getElementById('focus').innerHTML = needYouUnits.map(u =>
      u.kind === 'merge_group'
        ? groupedMergeFocusCard(u.bugs, u.parentId, data)
        : bugFocusCard(u.bug, data)
    ).join('');
    document.getElementById('processing').innerHTML = busyYouBugs.map(b => processingStripItemHtml(b, data, 'bug')).join('');

    const bugSections = [];
    for (const l of BUG_LANES) {
      const list = byBug[l.id];
      if (!list.length) continue;
      // Split stale (>60d) unchatted assigned bugs into their own Backlog section tab
      if (l.id === 'assigned') {
        const act = list.filter(b => !b.is_stale_backlog);
        const stale = list.filter(b => b.is_stale_backlog);
        if (act.length) {
          bugSections.push({
            id: 'assigned', title: l.title, count: act.length,
            bodyHtml: laneSectionBodyHtml(
              l.title, act.length, l.hint, act.map(b => bugRowHtml(b, data)).join('')
            ),
          });
        }
        if (stale.length) {
          bugSections.push({
            id: 'backlog', title: 'Backlog (>60d)', count: stale.length,
            bodyHtml: laneSectionBodyHtml(
              'Backlog (>60d)', stale.length, 'Unchatted assigned bugs older than 60 days',
              stale.map(b => bugRowHtml(b, data)).join('')
            ),
          });
        }
      } else if (l.id === 'has_cl') {
        // Collapse sibling [Merge Mxx] rows under the parent bug strip when present
        const collapsed = collapseMergeLaneBugs(list, data);
        if (collapsed.length) {
          bugSections.push({
            id: l.id, title: l.title, count: collapsed.length,
            bodyHtml: laneSectionBodyHtml(
              l.title, collapsed.length, l.hint,
              collapsed.map(u =>
                u.kind === 'merge_group'
                  ? groupedMergeLaneRowHtml(u.bugs, u.parentId, data)
                  : bugRowHtml(u.bug, data)
              ).join('')
            ),
          });
        }
      } else {
        bugSections.push({
          id: l.id, title: l.title, count: list.length,
          bodyHtml: laneSectionBodyHtml(
            l.title, list.length, l.hint, list.map(b => bugRowHtml(b, data)).join('')
          ),
        });
      }
    }
    const closedHints = {
      fixed: 'Recently fixed issues',
      closed: 'Won’t fix, duplicate, or obsolete issues',
    };
    for (const [ck, ct] of [['fixed', 'Fixed'], ['closed', 'Closed']]) {
      if (byBug[ck].length) {
        bugSections.push({
          id: ck, title: ct, count: byBug[ck].length,
          bodyHtml: laneSectionBodyHtml(
            ct, byBug[ck].length, closedHints[ck], byBug[ck].map(b => bugRowHtml(b, data)).join('')
          ),
        });
      }
    }
    // Surface upstream bug fetcher errors directly above the Bugs tab lanes
    const bugErrBanner = Array.isArray(data.bug_sync_errors) && data.bug_sync_errors.length
      ? data.bug_sync_errors.filter(Boolean).map(
          e => '<div class="stack-blocker-banner"><b>⚠️ Bug sync warning:</b> ' +
            `<span>${esc(e)}</span></div>`
        ).join('')
      : '';
    document.getElementById('lanes').innerHTML = bugErrBanner + (bugSections.length
      ? renderSecondarySectionsHtml('bugs', bugSections)
      : (shownBugs.length ? '' : '<p class="empty-note">No tracked bugs in this project.</p>'));
    const projCount = bugProjects.length;
    document.getElementById('footer').textContent =
      `${allBugs.length} tracked bugs across ${projCount} projects · refreshes every 5 min`;
    applyRenderScrollPosition(savedScrollY);
    // Bugs view returns early before CL lanes, so emit rendered heartbeat here as well
    notifyParentRendered(data);
    return;
  }

  renderProjectChips(all, projects);

  const shown = all.filter(it => selectedProject === 'ALL' || projOf(it) === selectedProject);
  const by = {you: [], landing: [], desk: [], waiting: [], watching: [], done: []};
  for (const it of shown) by[laneOf(it)].push(it);
  // Idle items needing human action precede chats where the agent is already working.
  // Unaddressed post-submit alerts precede alerts where a fix is already linked.
  by.you.sort((a, b) =>
    Number(Boolean(a.chat_busy)) - Number(Boolean(b.chat_busy)) ||
    Number(isPostSubmitAlert(b)) - Number(isPostSubmitAlert(a)) ||
    (isPostSubmitAlert(a) && isPostSubmitAlert(b)
      ? Number(Boolean(a.fixes && a.fixes.length)) -
        Number(Boolean(b.fixes && b.fixes.length))
      : 0)
  );

  // Changes without reviewers precede explicit drafts inside On your desk
  by.desk.sort((a, b) =>
    Number(Boolean(a.is_draft)) - Number(Boolean(b.is_draft)) ||
    (b.activity_ts || 0) - (a.activity_ts || 0)
  );

  const busyYou = countUniqueBusyChats(by.you);
  const needYouItems = by.you.filter(it => !it.chat_busy);
  const busyYouItems = by.you.filter(it => it.chat_busy);
  const needYou = needYouItems.length;
  document.getElementById('headline').innerHTML = headlineHtml(needYou, selectedProject, busyYou);
  // Secondary lane counts now appear on the .sec-tabs pills below #focus
  document.getElementById('subline').textContent = '';

  document.getElementById('focus').innerHTML = renderFocusSection(needYouItems, shown, data);
  document.getElementById('processing').innerHTML = busyYouItems.map(it => processingStripItemHtml(it, data, 'cl')).join('');

  const clSections = [];
  const pingGroups = collectReviewerPingGroups(shown);
  if (pingGroups.length) {
    const pingClIds = new Set(pingGroups.flatMap(g => g.items.map(x => x.item_key)));
    clSections.push({
      id: 'pings', title: 'Reviewer Pings', count: pingClIds.size,
      bodyHtml: renderPingsSection(shown),
    });
  }
  for (const l of LANES) {
    if (by[l.id].length) {
      clSections.push({
        id: l.id, title: l.title, count: by[l.id].length,
        bodyHtml: laneSectionBodyHtml(
          l.title, by[l.id].length, l.hint, renderLaneItemsHtml(by[l.id], data)
        ),
      });
    }
  }
  if (by.done.length) {
    clSections.push({
      id: 'done', title: 'Done', count: by.done.length,
      bodyHtml: laneSectionBodyHtml(
        'Done', by.done.length, 'Recently merged or abandoned',
        renderLaneItemsHtml(by.done, data)
      ),
    });
  }
  document.getElementById('lanes').innerHTML = clSections.length
    ? renderSecondarySectionsHtml('cls', clSections)
    : (shown.length ? '' : '<p class="empty-note">No open CL or PR chats in this project.</p>');
  document.getElementById('footer').textContent =
    `${all.length} open CL and PR chats across ${projects.length} projects · refreshes every 60 s`;
  applyRenderScrollPosition(savedScrollY);
  notifyParentRendered(data);
}

// Bounds status fetches with an AbortSignal so stalled UberProxy sockets cannot hang Loading
async function fetchWithTimeout(url, opts, timeoutMs) {
  const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
  const timer = ctrl ? setTimeout(() => ctrl.abort(), timeoutMs) : 0;
  // Unref timeout handles in Node test harnesses so completed scripts exit immediately
  if (timer && typeof timer.unref === 'function') timer.unref();
  try {
    const reqOpts = Object.assign({cache: 'no-store'}, opts || {});
    if (ctrl) reqOpts.signal = ctrl.signal;
    return await fetch(url, reqOpts);
  } finally {
    if (timer) clearTimeout(timer);
  }
}

async function fetchState(forceRender = false) {
  // Coalesce concurrent interval ticks and parent pings into one active /api/status request
  if (fetchInFlight) {
    if (forceRender === true) pendingDeferredRender = true;
    return;
  }
  fetchInFlight = true;
  if (initialRetryTimer) {
    clearTimeout(initialRetryTimer);
    initialRetryTimer = 0;
  }
  try {
    // Reject HTTP 502/503 proxy error pages before attempting JSON deserialization
    const resp = await fetchWithTimeout('/api/status', {}, FETCH_STATUS_TIMEOUT_MS);
    if (!resp || !resp.ok) {
      throw new Error(`HTTP ${resp ? resp.status : 0}`);
    }
    const nextData = await resp.json();
    const nextVer = Number((nextData && nextData.ui_version) || 0);
    // Reload document when the server process restarted with updated HTML/JS
    if (cachedUiVersion !== null && nextVer > 0 && nextVer !== cachedUiVersion) {
      if (reloadDashboardPage()) return;
    }
    if (nextVer > 0) cachedUiVersion = nextVer;
    hasLiveStatus = true;
    persistCachedStatus(nextData);
    const nextSig = statusRenderSignature(nextData);
    const syncedEl = document.getElementById('synced');
    const mustRender = forceRender === true || pendingDeferredRender;
    // Defer destructive DOM replacement while a 2-click confirmation button is armed
    if (cachedData && hasActiveConfirmation()) {
      cachedData = nextData;
      if (syncedEl) syncedEl.textContent = (nextData && nextData.last_sync) || '';
      if (mustRender || nextSig !== lastRenderedDataSig) pendingDeferredRender = true;
      notifyParentRendered(cachedData);
      return;
    }
    // Skip redundant DOM rebuilds when status content is identical to the rendered view
    if (!mustRender && cachedData && lastRenderedDataSig && nextSig === lastRenderedDataSig) {
      cachedData = nextData;
      if (syncedEl) syncedEl.textContent = (nextData && nextData.last_sync) || '';
      notifyParentRendered(cachedData);
      return;
    }
    cachedData = nextData;
    render();
  } catch (e) {
    document.getElementById('synced').textContent = 'Connection lost, retrying';
    // Retry faster than the 60s poll until the first live /api/status response succeeds
    if (!hasLiveStatus && !initialRetryTimer) {
      initialRetryTimer = setTimeout(() => {
        initialRetryTimer = 0;
        void fetchState();
      }, INITIAL_RETRY_DELAY_MS);
      if (initialRetryTimer && typeof initialRetryTimer.unref === 'function') {
        initialRetryTimer.unref();
      }
    }
  } finally {
    fetchInFlight = false;
  }
}

async function openChat(convId, url, ev) {
  if (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button === 1) return true;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
    // Stop propagation so clicking chat links inside summary elements does not toggle disclosure
    if (typeof ev.stopPropagation === 'function') ev.stopPropagation();
  }
  const btn = ev && (ev.currentTarget || ev.target) ? (ev.currentTarget || ev.target) : null;
  const origText = btn ? btn.textContent : '';

  // In embedded iframe mode, postMessage directly to parent Jetski Web for immediate navigation
  if (convId && typeof window !== 'undefined' && window.parent && window.parent !== window) {
    try {
      window.parent.postMessage({
        type: 'jetski-flight-deck-open-chat',
        cascadeId: convId,
      }, '*');
      if (btn) {
        btn.textContent = '✓ Opened in Jetski';
        setTimeout(() => {
          if (btn && btn.textContent === '✓ Opened in Jetski') {
            btn.textContent = origText;
          }
        }, 1400);
      }
      fetch('/api/open_chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({conversation_id: convId}),
      }).catch(() => {});
      return false;
    } catch (e) {
      // Fall through to standard navigation on postMessage failure
    }
  }

  if (convId && typeof fetch === 'function') {
    try {
      const resp = await fetch('/api/open_chat', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({conversation_id: convId}),
      });
      const res = await resp.json();
      if (res && res.ok && res.reused_tab) {
        if (btn) {
          btn.textContent = '✓ Opened in Jetski';
          setTimeout(() => {
            if (btn && btn.textContent === '✓ Opened in Jetski') {
              btn.textContent = origText;
            }
          }, 1400);
        }
        return false;
      }
    } catch (e) {
      // Fall back to named window navigation when local server is unreachable
    }
  }
  if (url && typeof window !== 'undefined' && typeof window.open === 'function') {
    window.open(url, 'jetski-chat');
  }
  return false;
}

async function startChat(itemKey, btn) {
  if (!itemKey || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  const it = findItemByKey(itemKey);
  if (btn) {
    btn.disabled = true;
    btn.textContent = isReviewerChange(it) ? '⏳ Preparing code review…' : 'Starting…';
  }
  try {
    const resp = await fetch('/api/start_chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_key: itemKey}),
    });
    const res = await resp.json();
    if (res && res.ok && res.chat_url) {
      await openChat(res.conversation_id || '', res.chat_url);
      await fetchState(true);
      return;
    }
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function rebaseItem(itemKey, btn) {
  if (!itemKey || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Waking chat…';
  }
  try {
    const resp = await fetch('/api/rebase', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_key: itemKey}),
    });
    const res = await resp.json();
    if (res && res.ok) {
      await fetchState(true);
      return;
    }
    if (btn && res && res.error) btn.title = res.error;
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function abandonCompanions(itemKeys, btn) {
  const keys = Array.isArray(itemKeys) ? itemKeys.filter(Boolean) : [itemKeys].filter(Boolean);
  if (!keys.length || (btn && btn.disabled)) return;
  // Require a second click within 3.5s before sending the destructive abandon request
  if (btn && btn.dataset.confirming !== '1') {
    const orig = btn.textContent;
    btn.dataset.confirming = '1';
    btn.dataset.origLabel = orig;
    confirmingButtons.add(btn);
    btn.textContent = keys.length > 1
      ? `Confirm Abandon (${keys.length})?`
      : 'Confirm Abandon?';
    btn.classList.add('danger');
    setTimeout(() => {
      if (btn && btn.dataset.confirming === '1') {
        delete btn.dataset.confirming;
        confirmingButtons.delete(btn);
        btn.classList.remove('danger');
        btn.textContent = btn.dataset.origLabel || orig;
        // Flush any status update that arrived while the 2-click confirmation was armed
        if (pendingDeferredRender && cachedData && !hasActiveConfirmation()) {
          render();
        }
      }
    }, 3500);
    return;
  }
  const orig = (btn && btn.dataset.origLabel) || (btn ? btn.textContent : '');
  if (btn) {
    delete btn.dataset.confirming;
    confirmingButtons.delete(btn);
    btn.disabled = true;
    btn.textContent = 'Abandoning…';
  }
  try {
    const resp = await fetch('/api/abandon', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_keys: keys}),
    });
    const res = await resp.json();
    if (res && res.ok) {
      await fetchState(true);
      return;
    }
    if (btn && res && Array.isArray(res.errors) && res.errors.length) {
      btn.title = res.errors.join('; ');
    }
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.classList.remove('danger');
    btn.textContent = orig;
  }
}

async function ackBugs(bugKeys, btn) {
  const keys = Array.isArray(bugKeys) ? bugKeys.filter(Boolean) : [bugKeys].filter(Boolean);
  if (!keys.length || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Acking…';
  }
  try {
    const resp = await fetch('/api/ack_bug', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({bug_keys: keys, bug_key: keys[0]}),
    });
    const res = await resp.json();
    if (res && res.ok) {
      await fetchState(true);
      return;
    }
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function ackBug(bugKey, btn) {
  return ackBugs([bugKey], btn);
}

async function confirmMerge(parentId, btn) {
  if (!parentId || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  const draftEl = document.getElementById('merge-draft-' + parentId);
  const commentTxt = draftEl && typeof draftEl.value === 'string' ? draftEl.value : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Posting & CC-ing…';
  }
  try {
    const resp = await fetch('/api/confirm_merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({parent_id: parentId, comment: commentTxt}),
    });
    const res = await resp.json();
    if (res && res.ok) {
      await fetchState(true);
      return;
    }
    if (btn && res) {
      const errTxt = res.error || (Array.isArray(res.errors) ? res.errors.join('; ') : '');
      if (errTxt) btn.title = errTxt;
    }
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function reassignMerge(issueIds, parentId, btn, commentMarkdown = '') {
  const ids = Array.isArray(issueIds) ? issueIds.filter(Boolean) : [issueIds].filter(Boolean);
  if ((!ids.length && !parentId) || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Reassigning…';
  }
  try {
    const resp = await fetch('/api/reassign_merge', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        issue_ids: ids,
        parent_id: parentId || '',
        comment_markdown: commentMarkdown || '',
      }),
    });
    const res = await resp.json();
    if (res && res.ok) {
      if (res.bulk_reassign_url) {
        if (typeof window !== 'undefined' && typeof window.open === 'function') {
          window.open(res.bulk_reassign_url, '_blank', 'noopener');
        }
        const card = btn && typeof btn.closest === 'function' ? btn.closest('article') : null;
        const statusEl = card && typeof card.querySelector === 'function'
          ? card.querySelector('.merge-reassign-status') : null;
        if (statusEl) {
          const nFail = Array.isArray(res.failed_ids) ? res.failed_ids.length : ids.length;
          statusEl.innerHTML = `<div class="fix-banner">` +
            `SafeReassignIssue requires Web UI bulk reassign for ${nFail} issue(s): ` +
            `${link(res.bulk_reassign_url, 'Open single bulk-reassign tab ↗')}</div>`;
        }
        if (btn) {
          btn.disabled = false;
          btn.textContent = orig;
        }
        return;
      }
      await fetchState();
      return;
    }
    if (btn && res && res.error) btn.title = res.error;
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function assignReviewer(itemKeys, reviewerEmail, btn) {
  const keys = Array.isArray(itemKeys) ? itemKeys.filter(Boolean) : [itemKeys].filter(Boolean);
  if (!keys.length || !reviewerEmail || (btn && btn.disabled)) return;
  const orig = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = keys.length > 1 ? `Adding to ${keys.length} CLs…` : 'Adding…';
  }
  try {
    const resp = await fetch('/api/add_reviewer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({item_keys: keys, reviewer: reviewerEmail}),
    });
    const res = await resp.json();
    if (res && res.ok) {
      await fetchState(true);
      return;
    }
    if (btn && res && Array.isArray(res.errors) && res.errors.length) {
      btn.title = res.errors.join('; ');
    }
  } catch (e) {
    // Restore button label when the local HTTP call fails
  }
  if (btn) {
    btn.disabled = false;
    btn.textContent = orig;
  }
}

async function triggerRefresh() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = 'Refreshing…';
  try {
    const resp = await fetch('/api/refresh', {method: 'POST'});
    const res = await resp.json().catch(() => null);
    // Wait for the re-execed server to answer /api/status before navigating the iframe
    if (res && res.reloaded && typeof location !== 'undefined' &&
        typeof location.reload === 'function') {
      for (let i = 0; i < 12; i++) {
        await new Promise(r => setTimeout(r, i === 0 ? 80 : 150));
        try {
          const probe = await fetchWithTimeout('/api/status', {}, 2500);
          if (probe && probe.ok) {
            const pData = await probe.json().catch(() => null);
            const pVer = Number((pData && pData.ui_version) || 0);
            if (!cachedUiVersion || !pVer || pVer !== cachedUiVersion) break;
          }
        } catch (e) {
          // Keep polling across the brief re-exec socket gap
        }
      }
      reloadDashboardPage();
      return;
    }
    await fetchState(true);
  } finally {
    btn.textContent = 'Refresh';
  }
}

function boot() {
  hasLiveStatus = false;
  cachedUiVersion = null;
  cachedData = null;
  lastRenderedDataSig = '';
  pendingDeferredRender = false;
  openDetailsKeys.clear();
  const isEmbed = (window.parent && window.parent !== window) ||
    new URLSearchParams(location.search).get('embed') === '1';
  if (isEmbed) {
    document.documentElement.classList.add('embedded');
  }
  // Bind message and scroll listeners once across page boot and test re-invocations
  if (!bootListenersBound && typeof window !== 'undefined' &&
      typeof window.addEventListener === 'function') {
    bootListenersBound = true;
    window.addEventListener('message', (ev) => {
      const msg = ev && ev.data;
      if (!msg || typeof msg !== 'object') return;
      if (msg.type === 'jetski-flight-deck-ping') {
        // Ack liveness only after live /api/status succeeded so broken backends still recover
        if (hasLiveStatus && cachedData) notifyParentRendered(cachedData);
        void fetchState();
      }
    });
    window.addEventListener('scroll', () => {
      persistScrollPosition(captureScrollPosition());
    }, {passive: true});
  }
  const savedProj = readClientStorage('flight_deck_project', false) || 'ALL';
  const savedView = readClientStorage('flight_deck_view', false) || 'cls';
  const savedSecCls = readClientStorage('flight_deck_section_cls', false) || null;
  const savedSecBugs = readClientStorage('flight_deck_section_bugs', false) || null;
  const rawDetails = readClientStorage(OPEN_DETAILS_STORAGE_KEY);
  if (rawDetails) {
    try {
      const parsedKeys = JSON.parse(rawDetails);
      if (Array.isArray(parsedKeys)) {
        for (const k of parsedKeys) {
          if (typeof k === 'string' && k) openDetailsKeys.add(k);
        }
      }
    } catch (e) {}
  }
  pendingBootScrollY = Math.max(0, Number(readClientStorage(SCROLL_Y_STORAGE_KEY)) || 0);
  // URL hash overrides localStorage so direct #view=bugs&section=assigned links open directly
  const hashParams = new URLSearchParams(location.hash.slice(1));
  selectedProject = hashParams.get('project') || savedProj;
  const hashView = hashParams.get('view');
  selectedView = hashView === 'bugs' ? 'bugs' : (savedView === 'bugs' ? 'bugs' : 'cls');
  selectedSection = {cls: savedSecCls, bugs: savedSecBugs};
  const hashSection = hashParams.get('section');
  if (hashSection) {
    selectedSection[selectedView === 'bugs' ? 'bugs' : 'cls'] = hashSection;
  }
  // Hydrate last rendered snapshot immediately so cards stay visible while Loading… is shown
  const rawCachedStatus = readClientStorage(CACHED_STATUS_STORAGE_KEY);
  if (rawCachedStatus) {
    try {
      const parsedStatus = JSON.parse(rawCachedStatus);
      if (parsedStatus && typeof parsedStatus === 'object') {
        cachedData = parsedStatus;
        render();
      }
    } catch (e) {}
  }
  fetchState();
  if (pollIntervalTimer && typeof clearInterval === 'function') clearInterval(pollIntervalTimer);
  pollIntervalTimer = setInterval(fetchState, POLL_INTERVAL_MS);
}

boot();
</script>
</body>
</html>
"""


class FlightDeckHandler(http.server.BaseHTTPRequestHandler):
  """HTTP handler for the CL Flight Deck dashboard."""

  def log_message(self, fmt: str, *args: Any) -> None:
    pass

  def _send_json(self, payload: Any, code: int = 200) -> None:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    try:
      self.send_response(code)
      self.send_header("Content-Type", "application/json; charset=utf-8")
      self.send_header("Cache-Control", "no-store")
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
      # Client or proxy closed the socket before reading the response
      return

  def do_GET(self) -> None:  # pylint: disable=invalid-name
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path == "/api/status":
      state = load_watcher_state()
      if not state.get("items"):
        state = sync_once(wake_chats=False, update_titles=True, sync_bugs=True)
      else:
        overlay_live_chat_busy(state)
        triage_store.overlay_triage_on_items(
            (state.get("items") or {}).values()
        )
      state["jetski_box_url"] = jetski_box_url()
      # Pass daemon script mtime so client detects restarted backend
      state["ui_version"] = int(_INITIAL_SCRIPT_MTIME) if _INITIAL_SCRIPT_MTIME > 0 else 0
      self._send_json(state)
      return

    raw = DASHBOARD_HTML.encode("utf-8")
    try:
      self.send_response(200)
      self.send_header("Content-Type", "text/html; charset=utf-8")
      self.send_header("Cache-Control", "no-store")
      self.send_header("Content-Length", str(len(raw)))
      self.end_headers()
      self.wfile.write(raw)
    except (BrokenPipeError, ConnectionResetError):
      # Client navigated away while HTML document was flushing
      return

  def do_POST(self) -> None:  # pylint: disable=invalid-name
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path in ("/api/refresh", "/api/sync"):
      # Run the updated script when the daemon's in-memory code is stale
      if script_changed_on_disk():
        subprocess.run(
            [sys.executable, _SCRIPT_PATH, "--sync-once"],
            check=False,
            timeout=90,
        )
        state = load_watcher_state()
        self._send_json({
            "ok": True,
            "reloaded": True,
            "count": len(state.get("items") or {}),
        })
        # Delay re-exec briefly so BaseHTTPRequestHandler flushes the 200 JSON body
        reexec_timer = threading.Timer(0.05, reexec_self, args=(self.server,))
        reexec_timer.daemon = True
        reexec_timer.start()
        return
      state = sync_once(
          wake_chats=True, update_titles=True, sync_bugs=True, force_bugs=True
      )
      self._send_json({
          "ok": True,
          "count": len(state.get("items") or {}),
          "bugs_count": len(state.get("bugs") or {}),
      })
      return

    if parsed.path in (
        "/api/start_chat",
        "/api/rebase",
        "/api/open_chat",
        "/api/abandon",
        "/api/ack_bug",
        "/api/confirm_merge",
        "/api/add_reviewer",
        "/api/approve_review_preview",
        "/api/approve_review",
        "/api/reassign_merge",
        "/api/save_gerrit_draft",
    ):
      length = int(self.headers.get("Content-Length") or 0)
      raw_body = (
          self.rfile.read(length).decode("utf-8", errors="replace")
          if length > 0
          else "{}"
      )
      try:
        body = json.loads(raw_body or "{}")
      except ValueError:
        self._send_json({"ok": False, "error": "Invalid JSON"}, code=400)
        return
      bdict = body if isinstance(body, dict) else {}
      if parsed.path == "/api/open_chat":
        cid = str(bdict.get("conversation_id") or "")
        res = open_chat_for_id(cid)
      elif parsed.path == "/api/start_chat":
        res = start_chat_for_item(str(bdict.get("item_key") or ""))
      elif parsed.path == "/api/ack_bug":
        raw_bkeys = bdict.get("bug_keys")
        if isinstance(raw_bkeys, list) and raw_bkeys:
          res = ack_bug_items([str(k) for k in raw_bkeys if k])
        else:
          res = ack_bug_item(
              str(bdict.get("bug_key") or bdict.get("item_key") or "")
          )
      elif parsed.path == "/api/confirm_merge":
        res = confirm_security_merge_group(
            str(bdict.get("parent_id") or ""),
            str(bdict.get("comment") or ""),
        )
      elif parsed.path == "/api/reassign_merge":
        raw_ids = bdict.get("issue_ids")
        ids = (
            [str(x) for x in raw_ids if x]
            if isinstance(raw_ids, list)
            else []
        )
        res = reassign_security_merge_for_state(
            ids,
            parent_id=str(bdict.get("parent_id") or ""),
            comment_markdown=str(bdict.get("comment_markdown") or ""),
        )
      elif parsed.path == "/api/save_gerrit_draft":
        res = save_gerrit_draft_for_item(
            str(bdict.get("item_key") or ""),
            str(bdict.get("draft") or ""),
        )
      elif parsed.path == "/api/add_reviewer":
        raw_keys = bdict.get("item_keys")
        if isinstance(raw_keys, list):
          keys = [str(k) for k in raw_keys if k]
        elif bdict.get("item_key"):
          keys = [str(bdict.get("item_key"))]
        else:
          keys = []
        res = assign_second_reviewer_items(
            keys, str(bdict.get("reviewer") or "")
        )
      elif parsed.path == "/api/abandon":
        raw_keys = bdict.get("item_keys")
        if isinstance(raw_keys, list):
          keys = [str(k) for k in raw_keys if k]
        elif bdict.get("item_key"):
          keys = [str(bdict.get("item_key"))]
        else:
          keys = []
        res = abandon_companion_items(
            keys, reason=str(bdict.get("reason") or "")
        )
      elif parsed.path in ("/api/approve_review_preview", "/api/approve_review"):
        # Route dry-run preview and explicit one-click atomic review publish through finalize_review
        raw_tid = str(bdict.get("target_id") or "").strip()
        raw_ikey = str(bdict.get("item_key") or "").strip()
        if not raw_tid and ":" in raw_ikey:
          raw_tid = raw_ikey.split(":", 1)[1].split("@", 1)[0]
        raw_host = str(bdict.get("host") or "").strip() or GERRIT_SKIA_HOST
        try:
          if parsed.path == "/api/approve_review_preview":
            res = finalize_review.build_finalize_preview(raw_tid, host=raw_host)
          else:
            res = finalize_review.execute_finalize_review(
                raw_tid,
                host=raw_host,
                expected_revision=str(bdict.get("expected_revision") or ""),
                draft_ids=(
                    [str(x) for x in bdict["draft_ids"]]
                    if isinstance(bdict.get("draft_ids"), list)
                    else None
                ),
                resolve_thread_leaf_ids=(
                    [str(x) for x in bdict["resolve_thread_leaf_ids"]]
                    if isinstance(bdict.get("resolve_thread_leaf_ids"), list)
                    else None
                ),
                abandon_companion_ids=(
                    [str(x) for x in bdict["abandon_companion_ids"]]
                    if isinstance(bdict.get("abandon_companion_ids"), list)
                    else None
                ),
                trigger_canaries=(
                    [str(x) for x in bdict["trigger_canaries"]]
                    if isinstance(bdict.get("trigger_canaries"), list)
                    else None
                ),
            )
        except Exception as exc:
          res = {"ok": False, "error": str(exc)}
      else:
        res = rebase_item(str(bdict.get("item_key") or ""))
      self._send_json(res, code=200 if res.get("ok") else 400)
      return

    self._send_json({"error": "Not found"}, code=404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  allow_reuse_address = True
  daemon_threads = True


def script_changed_on_disk(
    path: str = _SCRIPT_PATH, initial_mtime: float = _INITIAL_SCRIPT_MTIME
) -> bool:
  """Returns True when `path` on disk differs in mtime from `initial_mtime` and compiles."""
  if initial_mtime <= 0.0:
    return False
  try:
    # Detect both forward edits and mtime rollbacks (e.g. git checkout)
    if os.path.getmtime(path) == initial_mtime:
      return False
    # Verify syntax before replacing the daemon so a half-written edit cannot crash it
    with open(path, "rb") as f:
      compile(f.read(), path, "exec")
    return True
  except Exception:
    return False


def reexec_self(server: http.server.HTTPServer | None = None) -> None:
  """Closes the HTTP listener and replaces the process with the updated script."""
  if server is not None:
    try:
      server.server_close()
    except Exception:
      pass
  os.execv(sys.executable, [sys.executable, *sys.argv])


def serve_forever(
    port: int, poll_interval_sec: int, max_polls: int = 1000000
) -> None:
  """Runs the HTTP server and bounded background watcher loop."""
  global _SERVING_PORT
  _SERVING_PORT = port
  server = ThreadedHTTPServer(("0.0.0.0", port), FlightDeckHandler)

  def bg_loop() -> None:
    wait_sec = max(30, poll_interval_sec)
    for _ in range(max_polls):
      # Replace the running daemon before polling when cl_watcher.py changed on disk
      if script_changed_on_disk():
        reexec_self(server)
      try:
        sync_once(wake_chats=True, update_titles=True, sync_bugs=True)
      except Exception:
        pass
      # Poll mtime in 2-second slices so commits reload the server without waiting a full cycle
      for _ in range(max(1, wait_sec // 2)):
        time.sleep(2)
        if script_changed_on_disk():
          reexec_self(server)

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()

  # Resolve UberProxy PEN URL off the main thread so the HTTP socket accepts immediately
  def export_and_announce() -> None:
    export_flight_deck_info(port)
    pen = flight_deck_pen_url(port)
    print(
        f"CL Flight Deck listening on http://localhost:{port} (PEN: {pen})",
        flush=True,
    )

  threading.Thread(target=export_and_announce, daemon=True).start()
  server.serve_forever()


def main() -> int:
  parser = argparse.ArgumentParser(
      description="CL Flight Deck & Autonomous Watcher"
  )
  parser.add_argument(
      "--sync-once",
      action="store_true",
      help="Run a single poll and update state",
  )
  parser.add_argument(
      "--no-wake",
      action="store_true",
      help="Do not send agentapi wakeups on sync",
  )
  parser.add_argument(
      "--serve",
      action="store_true",
      help="Run live HTTP dashboard & watcher",
  )
  parser.add_argument(
      "--port", type=int, default=DEFAULT_PORT, help="HTTP server port"
  )
  parser.add_argument(
      "--interval",
      type=int,
      default=120,
      help="Background poll interval in seconds",
  )
  parser.add_argument(
      "--pen-url",
      action="store_true",
      help="Print UberProxy PEN URL for port",
  )
  args = parser.parse_args()

  if args.pen_url:
    print(get_pen_url(args.port))
    return 0

  if args.sync_once:
    state = sync_once(
        wake_chats=not args.no_wake,
        update_titles=True,
        sync_bugs=True,
        force_bugs=True,
    )
    count = len(state.get("items") or {})
    bcount = len(state.get("bugs") or {})
    print(
        f"Synced {count} CL/PR items and {bcount} bugs at {state.get('last_sync')}"
    )
    return 0

  if args.serve:
    serve_forever(args.port, args.interval)
    return 0

  parser.print_help()
  return 0


if __name__ == "__main__":
  sys.exit(main())
 
