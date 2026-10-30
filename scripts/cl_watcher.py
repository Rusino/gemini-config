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
from datetime import datetime, timezone
import glob
import http.client
import http.server
import json
import os
import re
import socket
import socketserver
import subprocess
import sys
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
import triage_store  # pylint: disable=g-import-not-at-top

DEFAULT_PORT = 8089
DEFAULT_SKIA_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_main")
DEFAULT_GARDENER_URL = "http://localhost:8080"
STATE_FILE = os.path.expanduser("~/.gemini/config/logs/cl_watcher_state.json")
OPEN_CHAT_REQ_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-open-chat.json"
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
SELF_EMAILS = {"jlavrova@google.com", "jlavrova@gmail.com"}
SELF_GITHUB_LOGINS = {"Rusino", "jlavrova"}
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
# Match explicit companion references to Gerrit changes in subjects
RE_COMPANION_CL_REF = re.compile(
    r"(?:companion\s+(?:(?:to|for)\s+)?(?:CL\s*|cl/|crrev(?:\.com)?/c/|skrev(?:\.com)?/)|"
    r"(?:hypotheses|hypothesis|check|repro)\s+(?:about|for|on)\s+"
    r"(?:CL\s*|cl/|crrev(?:\.com)?/c/|skrev(?:\.com)?/))"
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


def fetch_gmail_status_for_id(target_id: str) -> dict[str, Any]:
  """Searches Gmail (including TRASH) for notification threads matching a CL/PR ID."""
  res: dict[str, Any] = {
      "thread_ids": [],
      "inbox_thread_ids": [],
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
  for th in threads:
    if not isinstance(th, dict):
      continue
    msgs = th.get("messages") or []
    # Match threads where the CL/PR number is in the subject or from an automated code review mailer
    subj_match = any(str(target_id) in str(m.get("subject") or "") for m in msgs)
    bot_match = any(
        any(
            b in str(m.get("sender") or "").lower()
            for b in ("gerritcodereview", "skcq-be", "github.com", "critique")
        )
        for m in msgs
    )
    if subj_match and bot_match:
      matched_threads.append(th)

  # Fallback if subject did not explicitly include the ID on a short thread
  if not matched_threads:
    for th in threads:
      if not isinstance(th, dict):
        continue
      msgs = th.get("messages") or []
      if any(str(target_id) in str(m.get("subject") or "") for m in msgs):
        matched_threads.append(th)

  for th in matched_threads:
    tid = str(th.get("id") or "")
    if not tid:
      continue
    res["thread_ids"].append(tid)
    msgs = th.get("messages") or []
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
      if "TRASH" in labels:
        res["trash_msgs"] += 1
      if "UNREAD" in labels:
        res["unread_msgs"] += 1
    if th_has_inbox:
      res["inbox_thread_ids"].append(tid)

  return res


def archive_gmail_threads_for_id(
    target_id: str, thread_ids: list[str] | None = None
) -> dict[str, Any]:
  """Removes INBOX and UNREAD labels from all matching threads (including split TRASH/INBOX threads)."""
  tids = list(thread_ids or [])
  if not tids and target_id:
    gstat = fetch_gmail_status_for_id(target_id)
    tids = gstat.get("inbox_thread_ids") or gstat.get("thread_ids") or []
  if not tids:
    return {"ok": True, "archived": []}
  call_gmail_mcp(
      "batch_unlabel_threads",
      {"threadIds": tids, "labelIds": ["INBOX", "UNREAD"]},
  )
  return {"ok": True, "archived": tids}


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
  """Refreshes `chat_busy` across cached items from the local Language Server."""
  items = state.get("items") or {}
  if not items:
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
    if not cls_found and not prs_found:
      continue

    # Discover additional follow-up CLs/PRs recorded in the chat state file
    extra_cls, extra_prs = extract_state_file_targets(pid, cid)
    state_cls = {k: v for k, v in extra_cls.items() if k not in cls_hosts}
    state_prs = [p for p in extra_prs if p not in prs_found]

    # A slash list like "PR #194466/#194475" or multiple IDs means the chat is not 1:1 with one CL
    multi_slash = bool(
        re.search(
            r"(?:PR\s*#?\d+|(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/\d+|CL\s+\d+)"
            r"\s*[/,&+]\s*#?\d+",
            title,
            re.IGNORECASE,
        )
    )
    # Multiple targets mean the chat does not represent a single 1:1 CL
    has_multiple = (
        len(cls_found) + len(prs_found) + len(state_cls) + len(state_prs) > 1
    )
    single_target = not has_multiple and not multi_slash

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
    # Append state-file CLs to discovered targets without making them single-target
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
          "single_target": False,
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
    # Append state-file PRs to discovered targets without making them single-target
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
          "single_target": False,
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


def fetch_gerrit_related_cl_ids(host: str, cl_id: str) -> list[str]:
  """Fetches related change IDs from Gerrit's revision chain endpoint."""
  cache_key = f"{host}:{cl_id}"
  if cache_key in _RELATED_CLS_CACHE:
    return _RELATED_CLS_CACHE[cache_key]

  url = f"https://{host}/changes/{cl_id}/revisions/current/related"
  related_ids: list[str] = []
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "CLFlightDeck/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
      raw = resp.read().decode("utf-8", errors="replace")
    if raw.startswith(")]}'"):
      raw = raw[4:]
    data = json.loads(raw)
    changes = data.get("changes") or []
    # Build graph of active (not MERGED/ABANDONED) changes in the revision stack
    active_changes = [
        ch
        for ch in changes
        if isinstance(ch, dict)
        and str(ch.get("status") or "").upper() not in ("MERGED", "ABANDONED")
    ]
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
    pass

  _RELATED_CLS_CACHE[cache_key] = related_ids
  return related_ids


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
    is_comp = False

    if d.get("kind") == "gerrit":
      host = str(
          d.get("preferred_host")
          or info.get("host")
          or GERRIT_SKIA_HOST
      )
      m_cl = RE_COMPANION_CL_REF.search(subj)
      if m_cl and d_is_self:
        cand_key = f"gerrit:{m_cl.group(1)}"
        parent = known_chats.get(cand_key) or by_item_key.get(cand_key)
        parent_key = cand_key
        is_comp = True
      else:
        related = fetch_gerrit_related_cl_ids(host, d.get("target_id", ""))
        for rel_id in related:
          if str(rel_id) == str(d.get("target_id", "")):
            continue
          cand_key = f"gerrit:{rel_id}"
          cand_parent = known_chats.get(cand_key) or by_item_key.get(cand_key)
          if not cand_parent:
            continue
          rel_info = fetched_cache.get(cand_key) or {}
          # Never inherit archived chat from a MERGED or ABANDONED change without explicit companion
          if str(rel_info.get("status") or "").upper() in ("MERGED", "ABANDONED"):
            continue
          rel_is_self = bool(rel_info.get("is_self", False))
          # A companion stacks on a non-self CL or carries explicit companion markers in its subject
          is_candidate_comp = (
              d_is_self
              and (not rel_is_self or "companion" in subj.lower())
          )
          # Prevent cyclic A <-> B companion links
          if cand_parent.get("companion_to") == ikey:
            is_candidate_comp = False
          # Multi-CL stacks inherit the shared chat without being labeled companions
          if not d.get("conversation_id") and cand_parent.get("conversation_id"):
            parent = cand_parent
            parent_key = cand_key if is_candidate_comp else ""
            is_comp = is_candidate_comp
            break
          if is_candidate_comp:
            parent = cand_parent
            parent_key = cand_key
            is_comp = True
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
      ikey: str, kind: str, num: str, host: str, pid: str, pname: str
  ) -> dict[str, Any]:
    return {
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

  # Include self-authored changes merged in the last 3 days so post-submit rolls
  # and fixes stay tracked
  gerrit_q = (
      "((status:open+((owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com)"
      "+OR+((reviewer:jlavrova@google.com+OR+reviewer:jlavrova@gmail.com)+-is:wip)))"
      "+OR+(status:merged+(owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com)+-age:3d))"
  )
  for host in GERRIT_HOSTS:
    url = f"https://{host}/changes/?q={gerrit_q}&n=50"
    try:
      req = urllib.request.Request(url, headers={"User-Agent": "CLFlightDeck/1.0"})
      with urllib.request.urlopen(req, timeout=10) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
      changes = json.loads(raw[4:] if raw.startswith(")]}'") else raw)
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
      "repo:flutter/flutter is:open is:pr author:Rusino",
      "repo:flutter/flutter is:open is:pr involves:Rusino -author:Rusino",
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
      if not num or num in ignored:
        continue
      ikey = f"github:{num}"
      if ikey in seen_keys:
        continue
      subj = str(pr.get("title") or "")
      if RE_AUTOROLL_SUBJECT.match(subj):
        continue
      seen_keys.add(ikey)
      pid, pname = infer_project_for_change("github", "", subj, pnames)
      remote.append(_remote_item(ikey, "github", num, "", pid, pname))

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
  }


def check_skia_post_submit(
    merged_sha: str,
    rollers: dict[str, Any],
    incidents: list[dict[str, Any]],
    skia_dir: str = DEFAULT_SKIA_DIR,
    cl_id: str = "",
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
  url = f"https://{host}/changes/{cl_id}/revisions/current/mergeable"
  try:
    req = urllib.request.Request(url, headers={"User-Agent": "CLFlightDeck/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
      raw = resp.read().decode("utf-8", errors="replace")
    return bool(json.loads(raw[4:] if raw.startswith(")]}'") else raw).get("mergeable", True))
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
) -> list[dict[str, Any]]:
  """Detects reviewers who need to review or re-review on GitHub"""
  if state != "OPEN" or is_wip or is_draft or missing_approvals <= 0:
    return []
  # Only surface reviewer pings on our own PRs or PRs where we already approved
  self_approved = bool(approved_by & SELF_GITHUB_LOGINS)
  if not (is_self or (is_reviewer and self_approved)):
    return []

  pings: list[dict[str, Any]] = []
  base_pr_url = f"https://github.com/flutter/flutter/pull/{pr_id}"
  latest_commit_date = ""
  for cm in commits:
    d = str(cm.get("committedDate") or cm.get("authoredDate") or "")
    if d and d > latest_commit_date:
      latest_commit_date = d

  rev_by_author: dict[str, dict[str, Any]] = {}
  for r in reviews:
    login = str((r.get("author") or {}).get("login") or "")
    if login and login not in SELF_GITHUB_LOGINS:
      rev_by_author[login] = r

  for login in sorted(set(human_reviewers) - approved_by):
    rev = rev_by_author.get(login)
    had_approval = False
    reason = ""
    if rev:
      sub_date = str(rev.get("submittedAt") or "")
      st = str(rev.get("state") or "").upper()
      had_approval = (st == "APPROVED")
      if latest_commit_date and sub_date and sub_date < latest_commit_date:
        reason = "updated_after_approval" if had_approval else "updated_after_review"
    if not reason:
      if approved_count >= 1 and missing_approvals > 0:
        reason = "needs_second_approval"
      elif is_self:
        reason = "awaiting_review"
      else:
        continue

    pings.append({
        "reviewer": login,
        "reviewer_name": login,
        "active": True,
        "in_attention_set": True,
        "reason": reason,
        "prev_ps": None,
        "current_ps": 1,
        "had_approval": had_approval,
        "diff_url": f"{base_pr_url}/files",
    })
  return pings


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
          "o=CURRENT_REVISION&o=CURRENT_COMMIT&o=CURRENT_FILES&o=ALL_REVISIONS"
          "&o=MESSAGES&o=DETAILED_ACCOUNTS&o=LABELS&o=SUBMIT_REQUIREMENTS"
      )
      url = f"https://{host}/changes/{cl_id}/detail?{opts}"
      req = urllib.request.Request(url, headers={"User-Agent": "CLFlightDeck/1.0"})
      with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
        if raw.startswith(")]}'"):
          raw = raw[4:]
        d = json.loads(raw)

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
      commit_msg = str(((cur_rev.get("commit") or {}).get("message")) or "")
      files_dict = cur_rev.get("files") or {}
      files = sorted(str(f) for f in files_dict.keys() if f != "/COMMIT_MSG")
      hashtags = [str(h) for h in (d.get("hashtags") or []) if h]
      topic = str(d.get("topic") or "")

      att_set = [
          str((v.get("account") or {}).get("email") or "")
          for v in (d.get("attention_set") or {}).values()
      ]
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
      reviewer_accounts = (d.get("reviewers") or {}).get("REVIEWER") or []
      reviewer_emails = {
          str(r.get("email") or "")
          for r in reviewer_accounts
          if isinstance(r, dict)
      }
      is_reviewer = not is_self and bool(reviewer_emails & SELF_EMAILS)
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
      # External authors on Skia and Chromium Gerrit need 2 Googler approvals (Review-Enforcement)
      required_approvals = 2 if is_external else 1
      approved_emails = {
          e
          for e, v in cr_votes
          if v > 0 and e and e != owner_email
      }
      approved_count = len(approved_emails)
      assigned_reviewers_count = len(set(non_owner_reviewers) | approved_emails)
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
      # Non-self changes not currently in my attention sit in Watching
      is_watching = not is_self and not reviewer_removed and not in_my_attention
      merge_conflict = status == "NEW" and not gerrit_mergeable(host, cl_id)

      messages = d.get("messages") or []
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
      last_msg = messages[-1] if messages else {}
      last_msg_author = str(((last_msg.get("author") or {}).get("email")) or "bot")
      last_msg_date = str(last_msg.get("date") or "")
      last_msg_text = str(last_msg.get("message") or "").strip().replace("\n", " ")
      # NoteDb stores account mentions as <GERRIT_ACCOUNT_N> tokens in message bodies
      for acc in last_msg.get("accounts_in_message") or []:
        if isinstance(acc, dict) and acc.get("_account_id"):
          label = str(acc.get("email") or acc.get("name") or acc["_account_id"])
          last_msg_text = last_msg_text.replace(
              f"<GERRIT_ACCOUNT_{acc['_account_id']}>", label
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
      cq_status = "Idle"
      cq_msg_date = ""
      if max_cq == 2:
        cq_status = "CQ+2 (Submitting)"
      elif max_cq == 1:
        cq_status = "CQ+1 (Dry Run)"
      for m in reversed(messages[-6:]):
        if int(m.get("_revision_number") or 0) != ps_num:
          continue
        txt = str(m.get("message") or "")
        if (
            "This CL failed the SkCQ" in txt
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
      # Only my own changes enter the pre-submit CQ failure alert phase
      elif cq_failed and is_self:
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
            cur_sha, rollers, incidents, skia_dir=skia_dir, cl_id=cl_id
        )

      if status == "ABANDONED":
        phase = "ABANDONED"
      elif status == "MERGED":
        if post_submit:
          if post_submit["has_alert"]:
            phase = "ALERT_ROLL_OR_STATUS"
          elif post_submit["all_passed"]:
            phase = "READY_TO_CLOSE"
          else:
            phase = "POST_SUBMIT_ROLLING"
        else:
          phase = "READY_TO_CLOSE"
      else:
        # Pre-submit CQ failures belong to the CL author
        if cq_failed and is_self:
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
      project = str(d.get("project") or "")
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
          "missing_reviewers_to_assign": missing_reviewers_to_assign,
          "missing_approvals": missing_approvals,
          "reviewer_status": reviewer_status,
          # Per-reviewer ping entries for lost +1s and missing 2nd approvals
          "reviewer_pings": reviewer_pings,
          "self_cr_vote": self_cr_vote,
          "self_approved": bool(self_cr_vote > 0),
          "self_approval_ps": self_approval_ps,
          "parent_commit": parent_commit,
          "revision_ps_by_sha": revision_ps_by_sha,
          "patchset": ps_num,
          "revision": cur_sha,
          "commit_message": commit_msg,
          "files": files,
          "hashtags": hashtags,
          "topic": topic,
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
          "last_msg_author": last_msg_author,
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


def fetch_github_pr_status(pr_id: str) -> dict[str, Any]:
  """Fetches Flutter GitHub PR status via `gh pr view`."""
  try:
    out = subprocess.check_output(
        [
            "gh",
            "pr",
            "view",
            str(pr_id),
            "--repo",
            "flutter/flutter",
            "--json",
            # Include body so fix-for detection can parse PR bug/revert references
            "number,title,body,state,isDraft,author,mergedAt,headRefOid,headRefName,"
            "reviewDecision,latestReviews,reviews,comments,"
            # Include commits so author pushes after a review update the PR timeline
            "commits,statusCheckRollup,labels,url,mergeable,reviewRequests,"
            "createdAt,updatedAt,closedAt",
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
    tree_red = FLUTTER_TREE_CHECK in failed_all
    failed_checks = [c for c in failed_all if c != FLUTTER_TREE_CHECK]
    # Subtract checks already failing on master or the PR's base commit so tree
    # breakages do not trigger a false CQ failure alert on this PR
    own_failed_checks, tree_failed_checks = partition_github_failed_checks(
        str(pr_id), head_sha, failed_checks, failing
    )
    pending_checks = [
        str(c.get("name") or "")
        for c in checks
        if str(c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
    ]
    label_names = {
        str(lbl.get("name") or "")
        for lbl in (d.get("labels") or [])
        if isinstance(lbl, dict)
    }
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
            (review_decision == "REVIEW_REQUIRED" and approved_count >= 1)
            or is_external_contributor
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
        if str(c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
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
    in_my_attention = (
        state == "OPEN"
        and not is_wip
        and not in_development
        and (is_self or is_reviewer)
        and not self_approved
        and bool(last_actor)
        and last_actor not in SELF_GITHUB_LOGINS
    )
    is_watching = not is_self and not in_my_attention

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
    )

    return {
        "ok": True,
        "host": "github.com/flutter/flutter",
        "url": str(
            d.get("url") or f"https://github.com/flutter/flutter/pull/{pr_id}"
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
        "is_draft": is_draft,
        "is_wip": is_wip,
        "wip_label": wip_label,
        "head_ref": head_ref,
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
        # Expose PR description text under commit_message for shared fix-for extractors
        "commit_message": str(d.get("body") or ""),
        "commit_url": (
            f"https://github.com/flutter/flutter/commit/{head_sha}" if head_sha else ""
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
  """Sends a wakeup message to a Jetski conversation via agentapi."""
  if not conv_id or not message:
    return False
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
    # Discovery reads the CL or PR from the title, so a cut must never split it
    raw_tokens = [
        m.group(0)
        for r in (RE_CL_ID_GROUP, RE_PR_ID_GROUP)
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
  kind = str(rec.get("kind") or "gerrit")
  tid = str(rec.get("target_id") or "")
  host = str(rec.get("host") or rec.get("preferred_host") or "")
  if kind == "github":
    id_tok = f"PR #{tid}"
    default_sub = "Flutter"
  elif kind == "gerrit" and host == GERRIT_CHROMIUM_HOST:
    id_tok = f"crrev/c/{tid}"
    default_sub = "Chromium"
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

  raw_subj = str(rec.get("subject") or "")
  clean_subj = re.sub(r"^(?:\[[^\]]+\]\s*)+", "", raw_subj).strip()
  unbracketed = re.sub(r"[\[\]]+", " ", raw_subj).strip()
  proj_name = str(rec.get("project_name") or "").strip()
  # Fall back to bracketed tags, domain project name, or subsystem so GENERIC_TOPIC_WORDS
  # subjects like "[canvas] Fix bug" still pass validate_chat_topic
  sources = [clean_subj, unbracketed]
  if proj_name and proj_name not in META_PROJECT_NAMES:
    sources.append(f"{proj_name} patchset")
  sources.append(f"{default_sub} patchset")

  for src in sources:
    cand = _fit_words(src)
    if not cand:
      continue
    try:
      return cl.validate_chat_topic(cand)
    except cl.InvalidTopicError:
      continue
  return cl.validate_chat_topic(f"{id_tok} {default_sub} patchset"[:TITLE_TOPIC_MAX].rstrip())


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
  if by_key and ikey.startswith("gerrit:"):
    tid = ikey.split(":", 1)[1]
    host = str(
        (rec or {}).get("host")
        or (rec or {}).get("preferred_host")
        or GERRIT_SKIA_HOST
    )
    subj = str((rec or {}).get("subject") or (rec or {}).get("title") or "")
    m_cl = RE_COMPANION_CL_REF.search(subj)
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
    head_ref = str(
        (rec or {}).get("head_ref") or (rec or {}).get("head_ref_name") or ""
    )
    m = RE_COMPANION_PR_REF.search(subj)
    branch_m = RE_COMPANION_BRANCH.match(head_ref)
    ref_id = m.group(1) if m else (branch_m.group(1) if branch_m else "")
    if ref_id and rec_is_self:
      parent_keys = [f"github:{ref_id}"]
      is_companion_ref = True
  for parent_key in parent_keys:
    parent = by_key.get(parent_key)
    if parent:
      comp_key = (
          parent_key
          if (
              is_companion_ref
              or (rec_is_self and not bool(parent.get("is_self", False)))
          )
          else ""
      )
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


def _start_pinned_chat_for_record(
    items: dict[str, Any],
    ikey: str,
    matched_key: str,
    rec: dict[str, Any],
    prompt: str,
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
    model_info = cl.resolve_handoff_model("", tier="pro", project_id=project_id)
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
  return {
      "ok": True,
      "started": True,
      "conversation_id": new_cid,
      "title": full_title,
      "project_id": project_id,
      "chat_url": f"{jetski_box_url()}/c/{new_cid}",
  }


def start_chat_for_item(item_key: str) -> dict[str, Any]:
  """Starts a project-pinned visible Jetski chat for an unchatted CL/PR item."""
  ikey = str(item_key or "").strip()
  if not ikey:
    return {"ok": False, "error": "Missing item_key"}

  state = load_watcher_state()
  items: dict[str, Any] = state.setdefault("items", {})
  matched_key, rec = _lookup_state_item(items, ikey)

  # Reuse an existing active, companion-parent, or archived chat before spawning a new one
  existing_cid, is_arch, comp_to = _discover_existing_chat_for_item(ikey, rec)
  if existing_cid:
    if isinstance(rec, dict) and not str(rec.get("conversation_id") or "").strip():
      _rekey_state_item_chat(
          items,
          ikey,
          matched_key,
          rec,
          existing_cid,
          chat_archived=True if is_arch else None,
          companion_to=comp_to,
      )
      save_watcher_state(state)
    return {
        "ok": True,
        "already_existed": True,
        "conversation_id": existing_cid,
        "chat_url": f"{jetski_box_url()}/c/{existing_cid}",
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
  if rec.get("is_self"):
    prompt = (
        f"Отслеживай и помогай с моим изменением {url} ('{subj}', PS{ps}). "
        "Проверь текущий статус, комментарии ревью и проверки CI, и кратко напиши, "
        "что требует моего внимания."
    )
  else:
    prompt = (
        f"Проведи review-triage для {url} ('{subj}', PS{ps}) и подготовь разбор ревью "
        "(никогда не публикуй комментарии ревью без моего подтверждения)."
    )

  res = _start_pinned_chat_for_record(items, ikey, matched_key, rec, prompt)
  if res.get("ok"):
    save_watcher_state(state)
  return res


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
  url = f"https://{host}/changes/{parent_cl_id}/comments"
  req = urllib.request.Request(
      url, headers={"User-Agent": "flight-deck-cl-watcher/1.0"}
  )
  try:
    with urllib.request.urlopen(req, timeout=8) as resp:
      raw = resp.read().decode("utf-8", errors="replace")
    if raw.startswith(")]}'"):
      raw = raw.split("\n", 1)[1]
    comments_by_path = json.loads(raw)
  except (OSError, urllib.error.URLError, ValueError, TimeoutError):
    return {}

  if not isinstance(comments_by_path, dict):
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

  # Build review payload responding to each unresolved leaf comment with unresolved: False
  by_path: dict[str, list[dict[str, Any]]] = {}
  for leaf in leaves:
    p = str(leaf.get("path") or "/PATCHSET_LEVEL")
    comment_payload: dict[str, Any] = {
        "in_reply_to": leaf["id"],
        "message": "Abandoned companion WIP CL.",
        "unresolved": False,
    }
    if leaf.get("line"):
      comment_payload["line"] = leaf["line"]
    by_path.setdefault(p, []).append(comment_payload)

  review_url = f"https://{host}/a/changes/{parent_cl_id}/revisions/current/review"
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
    return {"ok": True, "resolved_count": len(leaves)}
  except (OSError, urllib.error.URLError, TimeoutError) as exc:
    return {"ok": False, "error": str(exc), "resolved_count": 0}


def abandon_companion_items(
    item_keys: list[str], reason: str = ""
) -> dict[str, Any]:
  """Abandons companion CLs or PRs and resolves parent Gerrit review comments."""
  state = load_watcher_state()
  items = state.get("items") or {}
  events = state.get("events") or []
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

    kind = str(rec.get("kind") or "")
    tid = str(rec.get("target_id") or "")
    host = str(rec.get("host") or GERRIT_SKIA_HOST)
    parent_key = str(rec.get("companion_to") or "")

    if kind == "gerrit":
      bearer = _get_gerrit_bearer_token(host)
      if not bearer:
        errors.append(f"{ikey}: no Gerrit credential for {host}")
        continue
      url = f"https://{host}/a/changes/{tid}/abandon"
      body = json.dumps(
          {"message": reason or "Abandoning companion WIP CL."}
      ).encode("utf-8")
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
                f"[FLIGHT-DECK] 🗑️ Abandoned companion {ikey} "
                f"('{rec.get('subject') or ikey}')."
            ),
        },
    )

  state["events"] = events[:50]
  save_watcher_state(state)
  return {
      "ok": len(errors) == 0,
      "abandoned": abandoned,
      "resolved_threads": resolved_total,
      "errors": errors,
  }


def compute_companion_states(
    new_items: dict[str, Any], *, skia_dir: str = DEFAULT_SKIA_DIR
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
    rec["missing_approvals"] = 0
    rec["reviewer_status"] = ""
    rec["reviewer_pings"] = []
    rec["cq_failed"] = False
    rec["tree_red"] = False
    rec["is_draft"] = False
    rec["in_development"] = False
    rec["is_watching"] = False
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
    parent_rec = parent_recs[0] if parent_recs else {}
    parent_status = str(parent_rec.get("status") or "").upper()
    parent_self_approved = bool(parent_rec.get("self_approved", False))
    parent_approval_ps = int(
        parent_rec.get("self_approval_ps")
        or parent_rec.get("patchset")
        or 0
    )
    parent_revs = parent_rec.get("revision_ps_by_sha") or {}

    # Map open companions by unique item_key
    open_comps_by_ikey: dict[str, list[dict[str, Any]]] = {}
    for r in comp_recs:
      if str(r.get("status") or "").upper() in ("NEW", "OPEN"):
        open_comps_by_ikey.setdefault(str(r.get("item_key") or ""), []).append(r)
        base_ps = int(parent_revs.get(str(r.get("parent_commit") or ""), 0))
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

    # Evaluate deterministic ready-to-abandon triggers
    for ikey, r_list in open_comps_by_ikey.items():
      r0 = r_list[0]
      tid = str(r0.get("target_id") or "")
      t_info = thread_map.get(tid) or {}
      mentioned = bool(t_info.get("mentioned", False))
      all_resolved = bool(t_info.get("all_resolved", False))
      base_ps = int(r0.get("companion_base_ps") or 0)
      p_label = str(r0.get("companion_to_label") or parent_key)
      reason = ""

      if parent_status in ("MERGED", "ABANDONED", "CLOSED"):
        reason = f"Parent {p_label} is {parent_status.lower()}"
      elif parent_self_approved and (base_ps == 0 or parent_approval_ps >= base_ps):
        ps_sfx = f" (PS{parent_approval_ps})" if parent_approval_ps else ""
        reason = f"You approved {p_label}{ps_sfx}"
      elif all_resolved:
        reason = f"Companion thread resolved on {p_label}"

      for r in r_list:
        r["companion_mentioned"] = mentioned
        r["companion_thread_resolved"] = all_resolved
        r["ready_to_abandon"] = bool(reason)
        r["ready_to_abandon_reason"] = reason

    # Deduplicate multiple open companions for the same parent change
    if len(open_comps_by_ikey) > 1:
      def comp_rank_key(k: str) -> tuple[int, int, float, int]:
        first_r = open_comps_by_ikey[k][0]
        ment = 1 if first_r.get("companion_mentioned") else 0
        bps = int(first_r.get("companion_base_ps") or 0)
        act = float(first_r.get("activity_ts") or 0.0)
        tid_str = str(first_r.get("target_id") or "")
        tid_int = int(tid_str) if tid_str.isdigit() else 0
        return (ment, bps, act, tid_int)

      sorted_keys = sorted(
          open_comps_by_ikey.keys(), key=comp_rank_key, reverse=True
      )
      canonical_key = sorted_keys[0]
      can_r0 = open_comps_by_ikey[canonical_key][0]
      can_label = (
          f"CL {can_r0.get('target_id')}"
          if can_r0.get("kind") == "gerrit"
          else f"PR #{can_r0.get('target_id')}"
      )
      for dup_key in sorted_keys[1:]:
        for r in open_comps_by_ikey[dup_key]:
          r["companion_duplicate"] = True
          r["companion_superseded_by"] = can_label
          r["ready_to_abandon"] = True
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
          r["companion_status_note"] = (
              f"Stacked on PS{r['companion_base_ps']}{cq_part} · waiting on author"
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


def sync_once(
    wake_chats: bool = True,
    update_titles: bool = True,
    skia_dir: str = DEFAULT_SKIA_DIR,
) -> dict[str, Any]:
  """Performs one full discovery and status poll across active and unchatted CLs/PRs."""
  state = load_watcher_state()
  prev_items: dict[str, Any] = state.get("items") or {}
  events: list[dict[str, Any]] = state.get("events") or []

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
  remote_unchatted = discover_open_remote_changes(
      {d["item_key"] for d in chatted},
      proj_names=proj_names,
  )
  all_chats = discover_active_cl_chats(include_archived=True)
  attach_archived_chats_to_open_changes(remote_unchatted, all_chats=all_chats)
  discovered = chatted + remote_unchatted

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
        fetched_cache[ikey] = fetch_github_pr_status(tid)
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
      rolling = bool(not ps.get("all_passed") or ps.get("has_alert"))
      _, prev_d = _lookup_state_item(prev_items, ikey)
      just_merged = bool(
          prev_d and str(prev_d.get("status") or "").upper() != "MERGED"
      )
      # Keep merged change if chatted, fixing/fixed, holding an open companion, rolling, or new
      if not (
          has_active_chat
          or is_fix
          or has_fix
          or has_open_companion
          or rolling
          or just_merged
      ):
        continue
    kept_active.append(d)
  active_discovered = kept_active

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

      if prev_status != "MERGED" and info["status"] == "MERGED":
        # Automatically archive Gmail threads from INBOX on merge
        if gstat.get("inbox_thread_ids"):
          archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
          gstat["inbox_msgs"] = 0
          gstat["inbox_thread_ids"] = []
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
        transition_msg = (
            f"[CL-WATCHER] 🔴 CRITICAL ALERT для {ikey} ({info['revision'][:10]}): "
            f"{reason}. Немедленно проверь skia-gardener / status.skia.org и подготовь "
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
        transition_msg = (
            f"[CL-WATCHER] 🔴 Упал CQ / Presubmit на {ikey} (PS{info['patchset']}): "
            f"{info['last_msg_snippet']}. Проверь причину падения и предложи фикс."
        )
      elif post.get("all_passed") and not prev_all_passed:
        if gstat.get("inbox_thread_ids"):
          archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
          gstat["inbox_msgs"] = 0
          gstat["inbox_thread_ids"] = []
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
          info["status"] in ("NEW", "OPEN")
          and (info["patchset"] != prev_ps or info["last_msg_date"] != prev_msg_date)
          and info["last_msg_author"] not in SELF_EMAILS
          and info["last_msg_author"] not in SELF_GITHUB_LOGINS
          # Ignore SkCQ dry-run bot messages so they do not wake the chat for a reply
          and not info["last_msg_author"].endswith(".gserviceaccount.com")
          and info["in_my_attention"]
      ):
        transition_msg = (
            f"[CL-WATCHER] 💬 Новое обновление в {ikey} (PS{info['patchset']}) от "
            f"{info['last_msg_author']}: {info['last_msg_snippet']}. "
            f"Проведи инкрементальный триаж/анализ и подготовь черновик ответа."
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
        if cid and wake_chats:
          wake_conversation(cid, transition_msg)

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
  compute_companion_states(new_items, skia_dir=skia_dir)

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

  triage_store.overlay_triage_on_items(new_items.values())
  state["items"] = new_items
  state["events"] = events
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
    --amber: #ff9f0a;
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
      --red-wash: #2a0f0d;
      --shadow: 0 0 0 1px #1c1c1e;
    }
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html { -webkit-font-smoothing: antialiased; }
  body {
    background: var(--paper);
    color: var(--ink);
    font: 15px/1.45 var(--sans);
    font-variant-numeric: tabular-nums;
  }
  a { color: inherit; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 0 32px; }

  .bar {
    position: sticky; top: 0; z-index: 5;
    backdrop-filter: saturate(180%) blur(20px);
    -webkit-backdrop-filter: saturate(180%) blur(20px);
    background: color-mix(in srgb, var(--paper) 78%, transparent);
    border-bottom: 1px solid var(--hairline);
  }
  .bar .wrap { display: flex; align-items: center; gap: 24px; height: 52px; }
  .brand { font: 600 17px var(--display); letter-spacing: -.01em; }
  .rollers { display: flex; gap: 16px; margin-left: auto; font-size: 13px; color: var(--graphite); }
  .rollers a { display: inline-flex; align-items: center; gap: 6px; }
  .rollers a:hover { color: var(--ink); text-decoration: none; }
  .synced { font-size: 13px; color: var(--graphite); white-space: nowrap; }
  .link-btn {
    background: none; border: 0; padding: 0; color: var(--blue); font: 13px var(--sans);
    cursor: pointer;
  }

  .dot { width: 8px; height: 8px; border-radius: 50%; display: inline-block; flex: none; }
  .ok { background: var(--green); }
  .wait { background: transparent; box-shadow: inset 0 0 0 1.5px var(--graphite); }
  .moving { background: var(--amber); animation: pulse 1.6s ease-in-out infinite; }
  .bad { background: var(--red); }
  @keyframes pulse { 50% { opacity: .35; } }

  .projects { display: flex; flex-wrap: wrap; gap: 6px; padding-top: 20px; }
  .proj {
    border: 0; background: transparent; color: var(--graphite); cursor: pointer;
    font: 500 13px var(--sans); padding: 5px 12px; border-radius: 980px;
  }
  .proj:hover { color: var(--ink); }
  .proj .n { margin-left: 4px; opacity: .7; }
  .proj.on { background: var(--ink); color: var(--paper); }

  .hero { padding: 64px 0 36px; }
  .hero h1 {
    font: 700 64px/1.02 var(--display);
    letter-spacing: -.035em;
    max-width: 22ch;
    text-wrap: balance;
  }
  .hero h1 .soft { color: var(--graphite); }
  .hero p { margin-top: 18px; font-size: 21px; color: var(--graphite); letter-spacing: -.01em; }

  .focus { display: grid; gap: 16px; }
  .card {
    background: var(--card);
    border-radius: 22px;
    box-shadow: var(--shadow);
    padding: 28px 32px;
    display: grid; grid-template-columns: 1fr auto; gap: 8px 32px; align-items: start;
  }
  .card.alert { background: var(--red-wash); }
  .eyebrow { font-size: 13px; font-weight: 600; color: var(--graphite); }
  .eyebrow .busy-pill {
    display: inline-flex; align-items: center; gap: 6px; color: var(--amber);
  }
  .card.alert .eyebrow { color: var(--red); }
  .card h2 { font: 600 26px/1.2 var(--display); letter-spacing: -.02em; margin: 4px 0 6px; }
  .card .why { font-size: 17px; color: var(--graphite); }
  .card .why b { color: var(--ink); font-weight: 500; }
  .card .actions { grid-row: 1; grid-column: 2; display: flex; flex-direction: column; gap: 8px; }
  .card .path, .card .incidents { grid-column: 1 / -1; }
  .card .path { margin-top: 18px; }

  .btn {
    display: inline-flex; justify-content: center; align-items: center;
    height: 36px; padding: 0 18px; border-radius: 980px; border: 0; background: none;
    font: 500 15px var(--sans); white-space: nowrap; cursor: pointer;
  }
  .btn:hover { text-decoration: none; }
  .btn.primary { background: var(--blue); color: #fff; }
  .btn.primary:not(:disabled):hover { filter: brightness(1.08); }
  .btn.secondary { background: color-mix(in srgb, var(--ink) 7%, transparent); color: var(--ink); }
  .btn.secondary:not(:disabled):hover {
    background: color-mix(in srgb, var(--ink) 12%, transparent);
  }
  .btn.quiet { height: 28px; color: var(--blue); }
  .btn.quiet:not(:disabled):hover { text-decoration: underline; }
  .btn:disabled, .link-btn:disabled { opacity: .45; cursor: default; }
  a:focus-visible, button:focus-visible, summary:focus-visible {
    outline: 3px solid color-mix(in srgb, var(--blue) 45%, transparent); outline-offset: 2px;
  }

  .path { display: flex; align-items: center; font-size: 12px; color: var(--graphite); }
  .stop { display: flex; flex-direction: column; align-items: center; gap: 6px; min-width: 64px; }
  .stop:hover { text-decoration: none; color: var(--ink); }
  .stop .dot { width: 10px; height: 10px; }
  .stop.is-bad span { color: var(--red); font-weight: 600; }
  .leg {
    height: 2px; background: var(--hairline); margin-bottom: 18px;
    flex: 0 1 160px; min-width: 24px;
  }
  .leg.done { background: var(--green); }
  .leg.broken {
    background: repeating-linear-gradient(90deg, var(--red) 0 6px, transparent 6px 10px);
  }
  .fan { display: flex; gap: 28px; }

  .incidents { margin-top: 14px; display: grid; gap: 6px; font-size: 14px; }
  .incident { display: flex; gap: 10px; align-items: baseline; flex-wrap: wrap; }
  .incident .why-line { color: var(--graphite); }
  .chip {
    font: 12px var(--mono); padding: 2px 8px; border-radius: 6px;
    background: color-mix(in srgb, var(--red) 12%, transparent); color: var(--red);
  }

  section.lane { margin-top: 56px; }
  .lane-head, details.done > summary {
    display: flex; align-items: baseline; gap: 12px;
    padding-bottom: 12px; border-bottom: 1px solid var(--hairline);
  }
  .lane-head h3, details.done h3 { font: 600 22px var(--display); letter-spacing: -.02em; }
  .lane-head .n, details.done .n { color: var(--graphite); font-size: 17px; }
  .lane-head .hint { margin-left: auto; font-size: 13px; color: var(--graphite); }
  details.done > summary { list-style: none; cursor: pointer; }
  details.done > summary::-webkit-details-marker { display: none; }
  details.done .chev { margin-left: auto; color: var(--blue); font-size: 15px; }
  details.done[open] .chev .closed, details.done:not([open]) .chev .open { display: none; }

  .row {
    display: grid; grid-template-columns: minmax(0, 1fr) 340px 200px;
    gap: 24px; align-items: center;
    padding: 16px 0; border-bottom: 1px solid var(--hairline);
  }
  .title { font-size: 16px; font-weight: 500; letter-spacing: -.005em; }
  .meta { font-size: 13px; color: var(--graphite); margin-top: 2px; }
  .meta .cl { font-family: var(--mono); font-size: 12px; }
  .meta .proj-tag { font-weight: 600; }
  .meta .chat { color: var(--blue); }
  .state { font-size: 14px; color: var(--graphite); }
  .state b { color: var(--ink); font-weight: 500; }
  .state .red, .why .red { color: var(--red); }
  .state .amber, .why .amber { color: var(--amber); }
  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    font: 600 11px/1.3 var(--sans); letter-spacing: .01em;
    padding: 2px 7px; border-radius: 6px; vertical-align: middle;
    margin-right: 6px; white-space: nowrap;
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
  .eyebrow .fix-stage-pill {
    display: inline-flex; align-items: center; padding: 1px 8px; border-radius: 980px;
    font-size: 12px; font-weight: 600;
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
    font-size: 17px; color: var(--ink); margin-bottom: 4px; line-height: 1.4;
  }
  .fix-primary-status .fix-stage-head { font-weight: 600; }
  .fix-primary-status .fix-detail { color: var(--graphite); font-size: 15px; }
  .fix-primary-status .fix-subj { color: var(--ink); }
  .why-context { font-size: 15px; color: var(--graphite); }
  .fix-banner {
    margin-top: 8px; padding: 8px 12px; border-radius: 10px;
    background: color-mix(in srgb, var(--ink) 4.5%, transparent);
    font-size: 13.5px; line-height: 1.45; color: var(--ink);
  }
  .row .fix-banner {
    margin-top: 5px; padding: 5px 9px; font-size: 12.5px;
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
  .card .triage-box {
    grid-column: 1 / -1; margin-top: 12px; padding: 10px 14px; border-radius: 12px;
    background: color-mix(in srgb, var(--ink) 4%, transparent);
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px; font-size: 13px;
  }
  .row .triage-box {
    margin-top: 6px; display: flex; flex-wrap: wrap; align-items: center;
    gap: 4px 8px; font-size: 12.5px;
  }
  .triage-time {
    font-family: var(--mono); font-size: 11.5px; font-weight: 600; color: var(--ink);
  }
  .triage-summary { color: var(--ink); font-weight: 500; }
  .triage-meta { color: var(--graphite); font-size: 12px; }
  .triage-actions { display: inline-flex; gap: 6px; margin-left: auto; }
  .triage-btn {
    border: 1px solid var(--hairline); background: var(--card); color: var(--ink);
    font: 500 11.5px var(--sans); padding: 2px 9px; border-radius: 980px; cursor: pointer;
  }
  .triage-btn:hover { background: color-mix(in srgb, var(--ink) 6%, transparent); }
  .triage-btn.primary {
    border-color: color-mix(in srgb, var(--blue) 35%, transparent); color: var(--blue);
  }
  .triage-btn.danger, .btn.danger {
    border-color: color-mix(in srgb, var(--red) 45%, transparent);
    background: color-mix(in srgb, var(--red) 12%, transparent);
    color: var(--red);
  }
  .companion-banner {
    margin-top: 8px; padding: 8px 12px; border-radius: 10px;
    background: color-mix(in srgb, var(--ink) 4.5%, transparent);
    font-size: 13.5px; line-height: 1.45; color: var(--ink);
    display: flex; flex-wrap: wrap; align-items: center; gap: 6px 10px;
  }
  .row .companion-banner {
    margin-top: 5px; padding: 5px 9px; font-size: 12.5px;
  }
  .triage-modal-backdrop {
    position: fixed; inset: 0; z-index: 100;
    background: rgba(0, 0, 0, 0.45); backdrop-filter: blur(4px);
    display: flex; align-items: center; justify-content: center; padding: 24px;
  }
  .triage-modal {
    background: var(--card); color: var(--ink); border-radius: 16px;
    box-shadow: 0 12px 40px rgba(0, 0, 0, 0.2); max-width: 760px; width: 100%;
    max-height: 85vh; display: flex; flex-direction: column; overflow: hidden;
  }
  .triage-modal-head {
    padding: 16px 20px; border-bottom: 1px solid var(--hairline);
    display: flex; align-items: center; gap: 12px;
  }
  .triage-modal-head h3 { font: 600 17px var(--display); margin: 0; }
  .triage-modal-close {
    margin-left: auto; background: none; border: 0; font-size: 18px; color: var(--graphite);
    cursor: pointer; padding: 4px 8px; border-radius: 6px;
  }
  .triage-modal-close:hover { color: var(--ink); }
  .triage-modal-tabs {
    display: flex; gap: 4px; padding: 10px 20px 0; border-bottom: 1px solid var(--hairline);
  }
  .triage-tab {
    border: 0; background: none; padding: 6px 12px; font: 500 13px var(--sans);
    color: var(--graphite); cursor: pointer; border-bottom: 2px solid transparent;
  }
  .triage-tab.active { color: var(--blue); border-bottom-color: var(--blue); font-weight: 600; }
  .triage-modal-body {
    padding: 16px 20px; overflow-y: auto; flex: 1; font-size: 14px;
  }
  .triage-pre {
    background: color-mix(in srgb, var(--ink) 4%, transparent); border-radius: 8px;
    padding: 12px 14px; font-family: var(--mono); font-size: 12px; line-height: 1.5;
    white-space: pre-wrap; word-break: break-word; user-select: text;
  }
  .when { text-align: right; font-size: 13px; color: var(--graphite); }
  .row .path { font-size: 13px; }
  .row .stop { min-width: 0; }
  .row .stop span { display: none; }
  .row .leg { flex: 0 0 32px; min-width: 0; margin-bottom: 0; }
  .row .fan { gap: 10px; }
  .row .path-label { margin-left: 12px; white-space: nowrap; }

  .empty-note { padding: 40px 0; color: var(--graphite); font-size: 17px; }
  footer { margin: 96px 0 48px; font-size: 12px; color: var(--graphite); }

  /* Grouped Reviewer Pings panel */
  .pings-wrap { margin-top: 28px; display: grid; gap: 14px; }
  .pings-head {
    display: flex; align-items: baseline; gap: 10px;
    padding-bottom: 8px; border-bottom: 1px solid var(--hairline);
  }
  .pings-head h3 { font: 600 20px var(--display); letter-spacing: -.02em; margin: 0; }
  .pings-head .n { color: var(--graphite); font-size: 15px; }
  .pings-head .hint { margin-left: auto; font-size: 13px; color: var(--graphite); }
  .ping-card {
    background: var(--card); border-radius: 16px; box-shadow: var(--shadow);
    padding: 18px 22px; display: grid; gap: 12px;
  }
  .ping-card-top {
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
  }
  .ping-who { font: 600 16px var(--sans); color: var(--ink); }
  .ping-counts { font-size: 13px; color: var(--graphite); }
  .ping-actions { margin-left: auto; display: flex; gap: 8px; align-items: center; }
  .ping-list { display: grid; gap: 8px; }
  .ping-row {
    display: flex; align-items: baseline; gap: 6px 8px; font-size: 14px;
    padding: 8px 12px; border-radius: 10px;
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
    margin-left: auto; display: inline-flex; align-items: baseline; gap: 10px;
    font: 500 12px var(--mono); white-space: nowrap;
  }
  .ping-row .ping-diff { color: var(--blue); }
  .ping-row .ping-why {
    flex: 1 0 100%; padding-left: 22px; font-size: 12.5px; color: var(--graphite);
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

  @media (max-width: 860px) {
    .hero h1 { font-size: 40px; }
    .card { grid-template-columns: 1fr; padding: 22px; }
    .card .actions { grid-row: auto; grid-column: 1; flex-direction: row; }
    .row { grid-template-columns: 1fr; gap: 6px; }
    .when { text-align: left; }
    .rollers .name { display: none; }
  }
  @media (prefers-reduced-motion: reduce) {
    .moving { animation: none; }
  }
</style>
</head>
<body>
<header class="bar">
  <div class="wrap">
    <div class="brand">Flight Deck</div>
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
  <div id="pings"></div>
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

let cachedData = null;
let selectedProject = 'ALL';

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

function shortActor(raw) {
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
  return s.split('@')[0];
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

// Filters out standalone companion rows when their parent CL/PR is present in items
function visibleItemsFromData(data) {
  if (!data || !data.items) return [];
  const raw = Object.values(data.items);
  const parentKeys = new Set(
    raw.filter(it => it && !isCompanion(it))
      .map(it => it.item_key || `${it.kind || 'gerrit'}:${it.target_id}`)
  );
  return raw.filter(it => {
    if (!it) return false;
    if (!isCompanion(it)) return true;
    // Abandoned or closed companions never render as standalone rows
    if (it.status === 'ABANDONED' || it.status === 'CLOSED') return false;
    // Keep open orphan companions visible only when the parent CL/PR is absent
    return !parentKeys.has(String(it.companion_to || ''));
  });
}

function badgesHtml(it, opts) {
  const out = [];
  const wip = wipLabel(it);
  if (wip) out.push(`<span class="badge wip">${esc(wip)}</span>`);
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
    return `<span class="amber">Needs +${missRev} reviewer${missRev > 1 ? 's' : ''} (${assigned}/${reqApp})</span>`;
  }
  const missApp = Number(it.missing_approvals || 0);
  const approved = Number(it.approved_count || 0);
  if (missApp > 0 && reqApp > 1 && approved > 0) {
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
  return Boolean(it.in_my_attention) && !(it.cq_failed && it.is_self) &&
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
  const cq = String(it.cq_status || '');
  if (it.phase === 'SUBMITTING' || cq.startsWith('CQ+') || cq.includes('running')) return 'cq';
  if (it.is_draft) return 'draft';
  if (it.in_development) return 'dev';
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
    // A merged CL without post-submit data has nothing left to track
    const ps = it.post_submit;
    return ps && !ps.all_passed ? 'landing' : 'done';
  }
  if (st === 'draft' || st === 'dev') return 'desk';
  if (st === 'waiting' || st === 'tree') return 'waiting';
  if (st === 'watching') return 'watching';
  return 'done';
}

function rollClass(state) {
  if (state === '✓') return 'ok';
  if (state === '🔄') return 'moving';
  if (state === '✗' || state === '🔴') return 'bad';
  return 'wait';
}

function flightPath(it, withLabels) {
  const ps = it.post_submit || {};
  const rolls = ps.rolls || {};
  const skiaBad = ps.skia_status === '🔴';
  let html = '<div class="path">';
  html += link(STATUS_SKIA_URL,
    `<i class="dot ${skiaBad ? 'bad' : 'ok'}"></i><span>Skia</span>`,
    `stop${skiaBad ? ' is-bad' : ''}`, ps.skia_status_detail || 'Skia');
  // Rolls into the four products are independent, so they fan out instead of chaining
  html += `<i class="leg ${skiaBad ? 'broken' : 'done'}"></i><div class="fan">`;
  for (const p of PRODUCTS) {
    const r = rolls[p] || {};
    html += link(r.url || STATUS_SKIA_URL,
      `<i class="dot ${rollClass(r.state)}"></i><span>${p}</span>`,
      'stop', `${p}: ${r.detail || 'waiting'}`);
  }
  html += '</div>';
  if (!withLabels) {
    const n = PRODUCTS.filter(p => (rolls[p] || {}).state === '✓').length;
    html += `<span class="path-label">${n} of 4 products</span>`;
  }
  return html + '</div>';
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
    const who = link(actorUrl(it, p.id), esc(shortActor(p.id)));
    return p.active ? who : `<s>${who}</s> (left Google)`;
  });
  return people.length ? `Waiting on <b>${people.join(', ')}</b>` : '';
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
    // Flag reviewers dropped from Gerrit attention_set so they can be pinged directly
    const offAtt = p.in_attention_set === false
      ? `<span class="badge ping-off-att" ` +
        `title="Not in Gerrit attention set">🔕 off attention</span>`
      : '';
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
  if (st === 'watching') {
    return [wipTag, compNote, pingNote, review, gap].filter(Boolean).join(' · ') || 'Watching';
  }
  return [wipTag, compNote, pingNote || waitingOnHtml(it) || 'In review', review, gap]
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

function chatActionHtml(it, data, compact) {
  const chat = chatUrl(it, data);
  if (chat) {
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

function renderTriageModal(it) {
  const root = typeof document !== 'undefined'
    ? document.getElementById('triage-modal-root') : null;
  if (!root || !it || !it.triage) return;
  const tr = it.triage;
  const key = it.item_key || it.target_id;
  const hasDraft = Boolean(tr.draft && tr.draft.trim());
  const activeTab = currentTriageModalTab || (hasDraft ? 'draft' : 'report');
  const content = activeTab === 'draft'
    ? (tr.draft || 'No draft comment required.')
    : (tr.report_markdown || 'No report available.');

  root.innerHTML = `
    <div class="triage-modal-backdrop" onclick="if (event.target === this) closeTriageModal()">
      <div class="triage-modal">
        <div class="triage-modal-head">
          <span class="badge triage-${esc(String(tr.verdict || '').toLowerCase())}">
            ${esc(tr.verdict_label || tr.verdict || '')}
          </span>
          <h3>${esc(clLabel(it))}: ${esc(cleanSubject(it.subject))}</h3>
          <button class="triage-modal-close" onclick="closeTriageModal()" title="Close">✕</button>
        </div>
        <div class="triage-modal-tabs">
          ${hasDraft ? `
            <button class="triage-tab${activeTab === 'draft' ? ' active' : ''}"
              onclick="currentTriageModalTab='draft'; ` +
              `renderTriageModal(findItemByKey(${esc(JSON.stringify(key))}))">
              Review Draft
            </button>` : ''}
          <button class="triage-tab${activeTab === 'report' ? ' active' : ''}"
            onclick="currentTriageModalTab='report'; ` +
            `renderTriageModal(findItemByKey(${esc(JSON.stringify(key))}))">
            Triage Report
          </button>
          ${hasDraft ? `
            <button class="triage-btn primary" style="margin-left: auto; margin-bottom: 6px;"
              onclick="copyTriageDraft(${esc(JSON.stringify(key))}, this)">` +
              `Copy Draft</button>` : ''}
        </div>
        <div class="triage-modal-body">
          <div class="triage-pre">${esc(content)}</div>
        </div>
      </div>
    </div>`;
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
  const v = esc(tr.verdict_label || tr.verdict || 'SURGICAL AUDIT');
  const vCls = 'triage-' + esc(String(tr.verdict || 'SURGICAL_AUDIT').toLowerCase());
  const badge = `<span class="badge ${vCls}">${v}</span>`;
  const time = tr.review_time_label
    ? `<span class="triage-time">${esc(tr.review_time_label)}</span>` : '';
  const summary = tr.summary
    ? `<span class="triage-summary">${esc(tr.summary)}</span>` : '';
  const stale = tr.is_stale
    ? `<span class="badge triage-stale">Stale: PS${esc(tr.triaged_patchset || 1)} → ` +
      `PS${esc(it.patchset || 1)}</span>`
    : '';
  const meta = `<span class="triage-meta">PS${esc(tr.triaged_patchset || 1)} · ` +
    `${esc(tr.triaged_at_et || '')}</span>`;

  const key = it.item_key || it.target_id;
  const keyJson = esc(JSON.stringify(key));
  const hasDraft = Boolean(tr.draft && tr.draft.trim());

  let actions = '';
  if (hasDraft) {
    actions += `<button class="triage-btn primary" ` +
      `onclick="openTriageModal(${keyJson}, 'draft')">Draft</button>`;
    actions += `<button class="triage-btn" ` +
      `onclick="copyTriageDraft(${keyJson}, this)">Copy</button>`;
  }
  actions += `<button class="triage-btn" ` +
    `onclick="openTriageModal(${keyJson}, 'report')">Report</button>`;

  return `<div class="triage-box">${badge}${time}${stale}${summary}${meta}` +
    `<span class="triage-actions">${actions}</span></div>`;
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

  const attNote = p.in_attention_set === false
    ? ' <span style="color:var(--red)">(Gerrit dropped them from attention set)</span>'
    : '';
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
      const offAtt = p.in_attention_set === false
        ? `<span class="badge ping-off-att" ` +
          `title="Reviewer is not in Gerrit attention set">🔕 off attention set</span>`
        : '';
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
          ${pingReasonBadgeHtml(p, it)}${offAtt}
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

if (typeof document !== 'undefined') {
  document.addEventListener('keydown', (e) => {
    // Escape dismisses whichever modal is open
    if (e.key === 'Escape') {
      closeTriageModal();
      closePingModal();
    }
  });
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
    const eyebrow = `${baseEyebrow}${fixPill}`;
    const first = incidents[0] || {};
    const primary = link(first.task_url || STATUS_SKIA_URL, 'See failure', 'btn primary');
    const landedAt = formatStatusTime(it, it.submitted_et, it.submitted_age);
    const landed = it.submitted_et ? `${esc(authorLabel(it))} landed it ${landedAt}.` : '';
    const blame = incidents.length
      ? ` Gardener lists ${incidents.length} failing task${incidents.length > 1 ? 's' : ''}.`
      : '';
    const whyContent = fixPrimary
      ? `${fixPrimary}<div class="why-context">${landed}${blame}</div>`
      : `${landed}${blame}`;
    return `
    <article class="card alert">
      <div><div class="eyebrow">${busyBadge}${eyebrow} · ${esc(it.project_name)} ·
        ${link(it.url, clLabel(it))}</div>
        <h2>${cardTitle}</h2>
        <div class="why">${whyContent}</div>
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
      const from = link(actorUrl(it, it.last_msg_author), esc(shortActor(it.last_msg_author)));
      const snippet = esc(String(it.last_msg_snippet || '').replace(/^Patch Set \d+:\s*/, ''));
      why = `${redConflict} · <b>${from}</b> · ${formatStatusTime(it, '', it.last_msg_age)}: ` +
        snippet;
    } else {
      why = `${redConflict} · ${formatStatusTime(it)}`;
    }
    actions = rebaseActionHtml(it) +
      link(it.url, `Open ${kindWord}`, 'btn secondary') + chatBtn;
  } else if (it.phase === 'ALERT_CQ_FAILED') {
    why = `The commit queue failed · ${formatStatusTime(it)}`;
    actions = link(checksUrl(it), 'See checks', 'btn primary') + chatBtn;
  } else {
    const from = link(actorUrl(it, it.last_msg_author), esc(shortActor(it.last_msg_author)));
    const snippet = esc(String(it.last_msg_snippet || '').replace(/^Patch Set \d+:\s*/, ''));
    const msgWhy = `<b>${from}</b> · ${formatStatusTime(it, '', it.last_msg_age)}: ${snippet}`;
    why = it.merge_conflict ? `<span class="red">${conflictNote}</span> · ${msgWhy}` : msgWhy;
    actions = link(it.url, `Open ${kindWord}`, 'btn primary') + chatBtn;
  }
  const gapNote = reviewerGapHtml(it);
  const fullWhy = gapNote ? `${why} · ${gapNote}` : why;
  const whyContent = fixPrimary
    ? `${fixPrimary}<div class="why-context">${fullWhy}</div>`
    : fullWhy;
  const actionsWithFix = openFixBtn ? actions.replace(chatBtn, `${openFixBtn}${chatBtn}`) : actions;
  return `
    <article class="card">
      <div><div class="eyebrow">${busyBadge}${esc(it.project_name)} · ${link(it.url, clLabel(it))} ·
        PS${esc(it.patchset || 1)}</div>
        <h2>${cardTitle}</h2>
        <div class="why">${whyContent}</div>
        <!-- Surface fix-for linkage on pre-submit focus cards -->
        ${brokenAndFixBannerHtml(it)}${fixesBreakageBannerHtml(it)}
        ${companionsBannerHtml(it)}</div>
      <div class="actions">${actionsWithFix}</div>
      ${triageHtml(it, false)}
    </article>`;
}

function rowHtml(it, data) {
  const who = link(actorUrl(it, it.owner_email || it.owner_name), esc(authorLabel(it)));
  const meta = [
    `<span class="proj-tag">${esc(it.project_name)}</span>`,
    `<span class="cl">${esc(clLabel(it))}</span>`,
    who,
    chatActionHtml(it, data, true),
  ].filter(Boolean).join(' · ');
  return `
    <div class="row">
      <div><div class="title">${badgesHtml(it)}${link(it.url, esc(cleanSubject(it.subject)))}</div>
        <div class="meta">${meta}</div>
        <!-- Surface fix-for linkage and broken bots on lane rows -->
        ${brokenAndFixBannerHtml(it)}${fixesBreakageBannerHtml(it)}
        ${companionsBannerHtml(it)}
        ${triageHtml(it, true)}</div>
      <div class="state">${stateHtml(it)}</div>
      <div class="when">${rowTime(it)}</div>
    </div>`;
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
  history.replaceState(null, '', p === 'ALL' ? '#' : `#project=${encodeURIComponent(p)}`);
  render();
}

function render() {
  const data = cachedData;
  if (!data) return;
  const all = visibleItemsFromData(data)
    .sort((a, b) => (b.activity_ts || 0) - (a.activity_ts || 0));
  const projects = projectsOf(all);
  if (selectedProject !== 'ALL' && !projects.includes(selectedProject)) selectedProject = 'ALL';
  const projOf = it => it.project_name || 'Unassigned';

  const chip = (value, label, n) =>
    `<button class="proj${selectedProject === value ? ' on' : ''}" ` +
    `onclick="setProject(${esc(JSON.stringify(value))})">` +
    `${esc(label)}<span class="n">${n}</span></button>`;
  document.getElementById('projects').innerHTML = chip('ALL', 'All', all.length) +
    projects.map(p => chip(p, p, all.filter(it => projOf(it) === p).length)).join('');

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

  const busyYou = by.you.filter(it => it.chat_busy).length;
  const needYou = by.you.length - busyYou;
  document.getElementById('headline').innerHTML = headlineHtml(needYou, selectedProject, busyYou);
  const sub = LANES.filter(l => by[l.id].length)
    .map(l => `${by[l.id].length} ${l.title.toLowerCase()}`);
  if (by.done.length) sub.push(`${by.done.length} done`);
  document.getElementById('subline').textContent = sub.length ? `${sub.join(' · ')}.` : '';

  document.getElementById('focus').innerHTML = by.you.map(it => focusCard(it, data)).join('');

  // Render grouped reviewer ping cards between Needs You and the status lanes
  const pingsEl = document.getElementById('pings');
  if (pingsEl) pingsEl.innerHTML = renderPingsSection(shown);

  let lanes = LANES.filter(l => by[l.id].length).map(l =>
    `<section class="lane"><div class="lane-head"><h3>${l.title}</h3>` +
    `<span class="n">${by[l.id].length}</span><span class="hint">${l.hint}</span></div>` +
    by[l.id].map(it => rowHtml(it, data)).join('') + '</section>').join('');
  if (by.done.length) {
    lanes += '<section class="lane"><details class="done"><summary><h3>Done</h3>' +
      `<span class="n">${by.done.length}</span>` +
      '<span class="chev"><span class="closed">Show</span><span class="open">Hide</span></span>' +
      '</summary>' +
      by.done.map(it => rowHtml(it, data)).join('') + '</details></section>';
  }
  document.getElementById('lanes').innerHTML = lanes ||
    '<p class="empty-note">No open CL or PR chats in this project.</p>';

  const r = data.rollers_summary || {};
  document.getElementById('rollers').innerHTML = PRODUCTS.map(p => {
    const x = r[p] || {};
    const tip = `${p}: ${x.badge || ''} · ${x.num_behind ?? '?'} commits behind`;
    return link(x.url, `<i class="dot ${x.num_failed ? 'moving' : 'ok'}"></i>` +
      `<span class="name">${p}</span>`, '', tip);
  }).join('') + (data.gardener_url ? link(data.gardener_url, 'Gardener') : '');
  document.getElementById('synced').textContent = data.last_sync || '';
  document.getElementById('footer').textContent =
    `${all.length} open CL and PR chats across ${projects.length} projects · refreshes every 20 s`;
}

async function fetchState() {
  try {
    const resp = await fetch('/api/status');
    cachedData = await resp.json();
    render();
  } catch (e) {
    document.getElementById('synced').textContent = 'Connection lost, retrying';
  }
}

async function openChat(convId, url, ev) {
  if (ev) {
    if (ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.button === 1) return true;
    if (typeof ev.preventDefault === 'function') ev.preventDefault();
  }
  const btn = ev && (ev.currentTarget || ev.target) ? (ev.currentTarget || ev.target) : null;
  const origText = btn ? btn.textContent : '';
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
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Starting…';
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
      await fetchState();
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

async function abandonCompanions(itemKeys, btn) {
  const keys = Array.isArray(itemKeys) ? itemKeys.filter(Boolean) : [itemKeys].filter(Boolean);
  if (!keys.length || (btn && btn.disabled)) return;
  // Require a second click within 3.5s before sending the destructive abandon request
  if (btn && btn.dataset.confirming !== '1') {
    const orig = btn.textContent;
    btn.dataset.confirming = '1';
    btn.dataset.origLabel = orig;
    btn.textContent = keys.length > 1
      ? `Confirm Abandon (${keys.length})?`
      : 'Confirm Abandon?';
    btn.classList.add('danger');
    setTimeout(() => {
      if (btn && btn.dataset.confirming === '1') {
        delete btn.dataset.confirming;
        btn.classList.remove('danger');
        btn.textContent = btn.dataset.origLabel || orig;
      }
    }, 3500);
    return;
  }
  const orig = (btn && btn.dataset.origLabel) || (btn ? btn.textContent : '');
  if (btn) {
    delete btn.dataset.confirming;
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
      await fetchState();
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

async function triggerRefresh() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = 'Refreshing…';
  try {
    await fetch('/api/refresh', {method: 'POST'});
    await fetchState();
  } finally {
    btn.textContent = 'Refresh';
  }
}

function boot() {
  selectedProject = new URLSearchParams(location.hash.slice(1)).get('project') || 'ALL';
  fetchState();
  setInterval(fetchState, 20000);
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
    self.send_response(code)
    self.send_header("Content-Type", "application/json; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

  def do_GET(self) -> None:  # pylint: disable=invalid-name
    parsed = urllib.parse.urlparse(self.path)
    if parsed.path == "/api/status":
      state = load_watcher_state()
      if not state.get("items"):
        state = sync_once(wake_chats=False, update_titles=True)
      else:
        overlay_live_chat_busy(state)
        triage_store.overlay_triage_on_items(
            (state.get("items") or {}).values()
        )
      state["jetski_box_url"] = jetski_box_url()
      self._send_json(state)
      return

    raw = DASHBOARD_HTML.encode("utf-8")
    self.send_response(200)
    self.send_header("Content-Type", "text/html; charset=utf-8")
    self.send_header("Cache-Control", "no-store")
    self.send_header("Content-Length", str(len(raw)))
    self.end_headers()
    self.wfile.write(raw)

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
        threading.Thread(
            target=reexec_self, args=(self.server,), daemon=True
        ).start()
        return
      state = sync_once(wake_chats=True, update_titles=True)
      self._send_json({"ok": True, "count": len(state.get("items") or {})})
      return

    if parsed.path in (
        "/api/start_chat",
        "/api/rebase",
        "/api/open_chat",
        "/api/abandon",
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
  """Returns True when `path` on disk is newer than `initial_mtime` and compiles."""
  if initial_mtime <= 0.0:
    return False
  try:
    if os.path.getmtime(path) <= initial_mtime:
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
  server = ThreadedHTTPServer(("0.0.0.0", port), FlightDeckHandler)

  def bg_loop() -> None:
    wait_sec = max(30, poll_interval_sec)
    for _ in range(max_polls):
      # Replace the running daemon before polling when cl_watcher.py changed on disk
      if script_changed_on_disk():
        reexec_self(server)
      try:
        sync_once(wake_chats=True, update_titles=True)
      except Exception:
        pass
      # Poll mtime in 2-second slices so commits reload the server without waiting a full cycle
      for _ in range(max(1, wait_sec // 2)):
        time.sleep(2)
        if script_changed_on_disk():
          reexec_self(server)

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()

  pen = get_pen_url(port)
  print(
      f"CL Flight Deck listening on http://localhost:{port} (PEN: {pen})",
      flush=True,
  )
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
    state = sync_once(wake_chats=not args.no_wake, update_titles=True)
    count = len(state.get("items") or {})
    print(f"Synced {count} active CL/PR chat items at {state.get('last_sync')}")
    return 0

  if args.serve:
    serve_forever(args.port, args.interval)
    return 0

  parser.print_help()
  return 0


if __name__ == "__main__":
  sys.exit(main())
