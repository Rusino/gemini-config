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

DEFAULT_PORT = 8089
DEFAULT_SKIA_DIR = os.path.expanduser("~/.gemini/jetski/scratch/skia_main")
DEFAULT_GARDENER_URL = "http://localhost:8080"
STATE_FILE = os.path.expanduser("~/.gemini/config/logs/cl_watcher_state.json")
PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
IGNORED_CLS_FILE = os.path.expanduser(
    "~/.gemini/config/skills/review-triage/ignored_cls.txt"
)
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

# Match cl/, CL, crrev(.com)/c/, and skrev(.com)/ shortlinks in chat titles
RE_CL_ID = re.compile(
    r"(?:\b(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/|\bCL\s+)(\d{6,10})\b",
    re.IGNORECASE,
)
RE_CL_ID_GROUP = re.compile(
    r"(?P<prefix>\b(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/|\bCL\s+)"
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
    r"(?:companion\s+(?:to\s+)?(?:PR\s*#?|#)|(?:re|for)\s+(?:PR\s*#?|#))(\d{5,7})\b",
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
# Match explicit WIP/Draft prefixes in CL and PR subjects, after optional [module] tags
RE_DRAFT_SUBJECT = re.compile(
    r"^(?:\[(?!wip\]|draft\])[^\]]+\]\s*)*"
    r"(?:\[(?:wip|draft)\]\s*|wip\b[:\s\-]*|draft\s*[:\-]\s*)",
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
    hint = (
        GERRIT_CHROMIUM_HOST
        if "crrev" in pref
        else (GERRIT_SKIA_HOST if "skrev" in pref else "")
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
    # A slash list like "PR #194466/#194475" or multiple IDs means the chat is not 1:1 with one CL
    multi_slash = bool(
        re.search(
            r"(?:PR\s*#?\d+|(?:crrev(?:\.com)?/c|skrev(?:\.com)?|cl)/\d+|CL\s+\d+)"
            r"\s*[/,&+]\s*#?\d+",
            title,
            re.IGNORECASE,
        )
    )
    single_target = (len(cls_found) + len(prs_found) == 1) and not multi_slash

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
    for ch in data.get("changes") or []:
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
  """Links unchatted companion CLs or PRs to their parent change's chat."""
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

  for d in active_discovered:
    if d.get("conversation_id"):
      continue
    ikey = d.get("item_key", "")
    info = fetched_cache.get(ikey) or {}
    parent: dict[str, Any] | None = None
    parent_key = ""

    if d.get("kind") == "gerrit":
      host = str(
          d.get("preferred_host")
          or info.get("host")
          or GERRIT_SKIA_HOST
      )
      related = fetch_gerrit_related_cl_ids(host, d.get("target_id", ""))
      for rel_id in related:
        cand_key = f"gerrit:{rel_id}"
        if cand_key in known_chats:
          parent = known_chats[cand_key]
          parent_key = cand_key
          break
    elif d.get("kind") == "github":
      subj = str(info.get("subject") or d.get("title") or "")
      m = RE_COMPANION_PR_REF.search(subj)
      if m:
        cand_key = f"github:{m.group(1)}"
        if cand_key in known_chats:
          parent = known_chats[cand_key]
          parent_key = cand_key

    if parent and parent.get("conversation_id"):
      d["conversation_id"] = parent["conversation_id"]
      d["title"] = parent.get("title", "")
      d["project_id"] = parent.get("project_id", "")
      d["project_name"] = parent.get("project_name", "")
      d["chat_last_modified"] = parent.get("chat_last_modified", "")
      d["chat_busy"] = parent.get("chat_busy", False)
      d["chat_archived"] = parent.get("chat_archived", False)
      d["single_target"] = False
      d["companion_to"] = parent_key
      parent["single_target"] = False


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
    kind: str, host: str, subject: str, proj_names: dict[str, str]
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
  if "skoretext" in subj_low or "coretext" in subj_low:
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

  gerrit_q = (
      "status:open+((owner:jlavrova@google.com+OR+owner:jlavrova@gmail.com)"
      "+OR+((reviewer:jlavrova@google.com+OR+reviewer:jlavrova@gmail.com)+-is:wip))"
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
    res["skia_status"] = "🔴"
    res["skia_status_detail"] = (
        f"Blamed by Gardener in: {', '.join(h['task_name'] for h in blamed[:2])}"
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
          "o=CURRENT_REVISION&o=ALL_REVISIONS&o=MESSAGES"
          "&o=DETAILED_ACCOUNTS&o=LABELS"
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
          "in_development": in_development,
          "is_watching": is_watching,
          "reviewer_removed": reviewer_removed,
          "removed_at": removed_at,
          "patchset": ps_num,
          "revision": cur_sha,
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
              f"+{max_cr}" if max_cr > 0 else (str(min_cr) if min_cr < 0 else "0")
          ),
          "cq_status": cq_status,
          "cq_failed": cq_failed,
          "last_msg_author": last_msg_author,
          "last_msg_date": last_msg_date,
          "last_msg_et": last_msg_et,
          "last_msg_age": last_msg_age,
          "last_msg_snippet": last_msg_text[:180],
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
            "number,title,state,isDraft,author,mergedAt,headRefOid,reviews,comments,"
            # Include commits so author pushes after a review update the PR timeline
            "commits,statusCheckRollup,url,mergeable,reviewRequests,"
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
    is_wip = state == "OPEN" and (
        bool(d.get("isDraft")) or bool(RE_DRAFT_SUBJECT.match(subject))
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
    elif pending_checks:
      cq_status = f"🔄 {len(pending_checks)} running"
    else:
      cq_status = "✓ Checks green"

    reviews = d.get("reviews") or []
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
        "is_draft": is_draft,
        "in_development": in_development,
        "is_watching": is_watching,
        "patchset": len(reviews) + 1,
        "revision": head_sha[:10],
        "commit_url": (
            f"https://github.com/flutter/flutter/commit/{head_sha}" if head_sha else ""
        ),
        "in_my_attention": phase in ("NEEDS_YOUR_ACTION", "ALERT_CQ_FAILED"),
        "cr_summary": f"{len(reviews)} rev",
        "cq_status": cq_status,
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
    is_draft = is_self and is_open and bool(RE_DRAFT_SUBJECT.match(subject))
    in_development = is_self and is_open and not is_draft and not reviewers
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
        "in_development": in_development,
        "is_watching": is_watching,
        "patchset": 1,
        "revision": cl_id,
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
  if by_key and ikey.startswith("gerrit:"):
    tid = ikey.split(":", 1)[1]
    host = str(
        (rec or {}).get("host")
        or (rec or {}).get("preferred_host")
        or GERRIT_SKIA_HOST
    )
    parent_keys = [
        f"gerrit:{rel_id}" for rel_id in fetch_gerrit_related_cl_ids(host, tid)
    ]
  elif by_key and ikey.startswith("github:"):
    subj = str((rec or {}).get("subject") or (rec or {}).get("title") or "")
    m = RE_COMPANION_PR_REF.search(subj)
    if m:
      parent_keys = [f"github:{m.group(1)}"]
  for parent_key in parent_keys:
    parent = by_key.get(parent_key)
    if parent:
      return (
          str(parent["conversation_id"]),
          bool(parent.get("chat_archived")),
          parent_key,
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
        f"Track and assist with my change {url} ('{subj}', PS{ps}). "
        "Inspect its current status, review comments, and CI checks, and summarize "
        "anything that needs my attention."
    )
  else:
    prompt = (
        f"Please run review-triage on {url} ('{subj}', PS{ps}) and prepare a code "
        "review analysis (never publish review comments without my confirmation)."
    )

  res = _start_pinned_chat_for_record(items, ikey, matched_key, rec, prompt)
  if res.get("ok"):
    save_watcher_state(state)
  return res


def _build_rebase_prompt(rec: dict[str, Any]) -> str:
  """Builds the conflict-resolution prompt sent to the CL/PR's owning agent chat."""
  url = str(rec.get("url") or rec.get("item_key") or "")
  subj = str(rec.get("subject") or url)
  ps = int(rec.get("patchset") or 1)
  if rec.get("kind") == "github":
    return (
        f"[FLIGHT-DECK REBASE] {url} ('{subj}') has a merge conflict with upstream main/master. "
        "Please rebase this PR branch onto the latest upstream main branch in its local worktree "
        "(create an isolated worktree if the main checkout is dirty or leased), resolve all merge "
        "conflicts while preserving domain invariants and behavior, rerun the relevant unit tests "
        "(plus run_webparagraph_corpus.py if lib/web_ui/lib/ production code is touched) and "
        "amnesia-precommit checks, and push the rebased branch to update the PR. "
        "Do not merge the PR or enable auto-merge."
    )
  return (
      f"[FLIGHT-DECK REBASE] {url} ('{subj}', PS{ps}) has a merge conflict with origin/main. "
      "Please fetch and rebase this CL onto the latest origin/main in its local worktree "
      "(create an isolated worktree if the main checkout is dirty or leased), resolve all merge "
      "conflicts while preserving domain invariants and behavior, rerun the relevant unit tests "
      "(plus run_flutter_paragraph_tests.py if modules/{skparagraph,skunicode,skshaper}/"
      "{src,include}/ production code is touched) and amnesia-precommit checks, and run "
      "`git cl upload` to upload the rebased patchset. Never vote CQ+1, CQ+2, or Auto-Submit+1."
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
  chatted = discover_active_cl_chats()
  remote_unchatted = discover_open_remote_changes(
      {d["item_key"] for d in chatted}
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
    active_discovered.append(d)

  link_companion_changes_to_parent_chats(
      active_discovered, fetched_cache, all_chat_candidates=all_chats
  )

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

    if cid and update_titles and badge and not d.get("companion_to"):
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
            f"[CL-WATCHER] ✅ {ikey} ('{info['subject']}') has just MERGED "
            f"(revision {info['revision'][:10]}). Gmail INBOX threads for {tid} "
            f"were automatically archived. Please report the initial Skia Status "
            f"and 4-autoroll watch checklist."
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
            f"[CL-WATCHER] 🔴 CRITICAL ALERT for {ikey} ({info['revision'][:10]}): "
            f"{reason}. Please inspect skia-gardener / status.skia.org immediately "
            f"and prepare a diagnosis."
        )
      elif info["phase"] == "ALERT_CQ_FAILED" and prev_phase != "ALERT_CQ_FAILED":
        is_critical = True
        transition_msg = (
            f"[CL-WATCHER] 🔴 CQ / Presubmit failed on {ikey} (PS{info['patchset']}): "
            f"{info['last_msg_snippet']}. Please inspect the failure and propose a fix."
        )
      elif post.get("all_passed") and not prev_all_passed:
        if gstat.get("inbox_thread_ids"):
          archive_gmail_threads_for_id(tid, gstat["inbox_thread_ids"])
          gstat["inbox_msgs"] = 0
          gstat["inbox_thread_ids"] = []
        transition_msg = (
            f"[CL-WATCHER] 🏁 All 4 autorolls (Android, Chrome, Google3, Flutter) and "
            f"Skia Status have passed cleanly for {ikey} ({info['revision'][:10]})! "
            f"Remaining Gmail threads for {tid} have been archived from INBOX. "
            f"Ask the user if we should finalize this chat."
        )
      elif (
          prev_phase == "ALERT_ROLL_OR_STATUS"
          and info["phase"] != "ALERT_ROLL_OR_STATUS"
      ):
        # An earlier critical wakeup must be withdrawn explicitly, or the chat keeps
        # chasing a culprit the Gardener no longer names. It sits below the all-rolls
        # branch so that branch still archives Gmail when both happen in one sync
        transition_msg = (
            f"[CL-WATCHER] ✅ Alert withdrawn for {ikey} ({info['revision'][:10]}): "
            f"Skia Gardener does not blame this commit "
            f"({post.get('skia_status_detail') or 'no matching incident'}). "
            f"Disregard the earlier CRITICAL ALERT."
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
            f"[CL-WATCHER] 💬 New update on {ikey} (PS{info['patchset']}) from "
            f"{info['last_msg_author']}: {info['last_msg_snippet']}. "
            f"Please run incremental triage/analysis and draft a response."
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
  .when { text-align: right; font-size: 13px; color: var(--graphite); }
  .row .path { font-size: 13px; }
  .row .stop { min-width: 0; }
  .row .stop span { display: none; }
  .row .leg { flex: 0 0 32px; min-width: 0; margin-bottom: 0; }
  .row .fan { gap: 10px; }
  .row .path-label { margin-left: 12px; white-space: nowrap; }

  .empty-note { padding: 40px 0; color: var(--graphite); font-size: 17px; }
  footer { margin: 96px 0 48px; font-size: 12px; color: var(--graphite); }

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
  <div id="lanes"></div>
  <footer id="footer"></footer>
</main>

<script>
const PRODUCTS = ['Android', 'Chrome', 'Google3', 'Flutter'];
const COUNT_WORDS = ['Nothing', 'One thing', 'Two things', 'Three things', 'Four things',
  'Five things'];
const PROJECT_ORDER = ['SkParagraph', 'Skia', 'WebParagraph', 'Skia Gardener', 'TextClusters API'];
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

// The project tag already names the component, so drop the "[component] " prefix
function cleanSubject(s) {
  return String(s || '').replace(/^\[[^\]]+\]\s*/, '');
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
  if (isAlert(it) || needsMyReply(it)) return 'attention';
  if (it.status === 'ABANDONED' || it.status === 'CLOSED') return 'abandoned';
  if (it.status === 'MERGED') {
    const ageDays = (Date.now() / 1000 - (it.submitted_ts || 0)) / 86400;
    return ageDays > OLD_MERGED_DAYS ? 'old' : 'merged';
  }
  // Non-participant changes sit in Watching rather than personal workflow queues
  if (it.is_watching || (!it.is_self && it.reviewer_removed)) return 'watching';
  // A conflict needs my rebase now; active CQ runs precede draft/dev and red tree
  if (it.merge_conflict && it.is_self) return 'conflict';
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

// One plain-language line per row: what state the change is in and why
function stateHtml(it) {
  const cr = it.cr_summary ? `CR ${esc(it.cr_summary)}` : '';
  const cq = it.cq_status ? link(checksUrl(it), esc(it.cq_status)) : '';
  const review = [cr, cq].filter(Boolean).join(' · ');
  if (it.status === 'ABANDONED' || it.status === 'CLOSED') {
    return it.status === 'ABANDONED' ? 'Abandoned' : 'Closed';
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
  if (st === 'draft') return `Draft · ${cq || 'no CQ run'}`;
  if (st === 'dev') return `No reviewers yet · ${cq || 'no CQ run'}`;
  if (st === 'tree') {
    return `<span class="red">${link(FLUTTER_DASHBOARD_URL, 'flutter/flutter tree')} is red;` +
      ' landing waits for it</span>';
  }
  if (st === 'watching') return review || 'Watching';
  return [waitingOnHtml(it) || 'In review', review].filter(Boolean).join(' · ');
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
    return compact ? link(chat, label, 'chat') : link(chat, label, 'btn quiet');
  }
  if (!it.item_key) return '';
  const cls = compact ? 'link-btn chat' : 'btn quiet';
  const label = compact ? 'start chat' : 'Start chat';
  return `<button class="${cls}" onclick="startChat(${esc(JSON.stringify(it.item_key))}, this)">` +
    `${label}</button>`;
}

// Server-side rebase fails on conflicts, so only my own open changes wake a local chat
function canRebase(it) {
  return Boolean(it.merge_conflict && it.is_self) &&
    (it.status === 'NEW' || it.status === 'OPEN') && Boolean(it.item_key);
}

function rebaseActionHtml(it) {
  if (!canRebase(it)) return '';
  const dis = it.chat_busy ? ' disabled title="Chat is already working"' : '';
  return `<button class="btn primary"${dis} ` +
    `onclick="rebaseItem(${esc(JSON.stringify(it.item_key))}, this)">🔧 Rebase</button>`;
}

function focusCard(it, data) {
  const chatBtn = chatActionHtml(it, data, false);
  const busyBadge = it.chat_busy
    ? '<span class="busy-pill"><i class="dot moving"></i>Chat is working</span> · '
    : '';
  const subject = esc(cleanSubject(it.subject));
  if (isPostSubmitAlert(it)) {
    const ps = it.post_submit || {};
    const incidents = ps.incidents || [];
    const blamed = incidents.some(h => h.verdict === 'blamed');
    const eyebrow = blamed ? 'Broke the Skia tree' : esc(ps.alert_reason || 'Post-submit alert');
    const first = incidents[0] || {};
    const primary = link(first.task_url || STATUS_SKIA_URL, 'See failure', 'btn primary');
    const landedAt = formatStatusTime(it, it.submitted_et, it.submitted_age);
    const landed = it.submitted_et ? `${esc(authorLabel(it))} landed it ${landedAt}.` : '';
    const blame = incidents.length
      ? ` Gardener lists ${incidents.length} failing task${incidents.length > 1 ? 's' : ''}.`
      : '';
    return `
    <article class="card alert">
      <div><div class="eyebrow">${busyBadge}${eyebrow} · ${esc(it.project_name)} ·
        ${link(it.url, clLabel(it))}</div>
        <h2>${link(it.url, subject)}</h2>
        <div class="why">${landed}${blame}</div></div>
      <div class="actions">${primary}${chatBtn}</div>
      ${incidents.length
        ? `<div class="incidents">${incidents.map(incidentHtml).join('')}</div>` : ''}
      ${it.status === 'MERGED' ? flightPath(it, true) : ''}
    </article>`;
  }
  const kindWord = it.kind === 'github' ? 'PR' : 'CL';
  const conflictNote = it.is_self ? 'Merge conflict: rebase needed' : 'Merge conflict';
  let why;
  let actions;
  if (canRebase(it)) {
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
  return `
    <article class="card">
      <div><div class="eyebrow">${busyBadge}${esc(it.project_name)} · ${link(it.url, clLabel(it))} ·
        PS${esc(it.patchset || 1)}</div>
        <h2>${link(it.url, subject)}</h2>
        <div class="why">${why}</div></div>
      <div class="actions">${actions}</div>
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
      <div><div class="title">${link(it.url, esc(cleanSubject(it.subject)))}</div>
        <div class="meta">${meta}</div></div>
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
  const all = Object.values(data.items || {})
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
  // Idle items needing human action precede chats where the agent is already working
  by.you.sort((a, b) =>
    Number(Boolean(a.chat_busy)) - Number(Boolean(b.chat_busy)) ||
    Number(isPostSubmitAlert(b)) - Number(isPostSubmitAlert(a))
  );

  const busyYou = by.you.filter(it => it.chat_busy).length;
  const needYou = by.you.length - busyYou;
  document.getElementById('headline').innerHTML = headlineHtml(needYou, selectedProject, busyYou);
  const sub = LANES.filter(l => by[l.id].length)
    .map(l => `${by[l.id].length} ${l.title.toLowerCase()}`);
  if (by.done.length) sub.push(`${by.done.length} done`);
  document.getElementById('subline').textContent = sub.length ? `${sub.join(' · ')}.` : '';

  document.getElementById('focus').innerHTML = by.you.map(it => focusCard(it, data)).join('');

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
      window.open(res.chat_url, '_blank', 'noopener');
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

    if parsed.path in ("/api/start_chat", "/api/rebase"):
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
      ikey = str((body if isinstance(body, dict) else {}).get("item_key") or "")
      res = (
          start_chat_for_item(ikey)
          if parsed.path == "/api/start_chat"
          else rebase_item(ikey)
      )
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
    print(
        f"Synced {len(state.get('items') or {})} active CL/PR chat items at {state.get('last_sync')}"
    )
    return 0

  if args.serve:
    serve_forever(args.port, args.interval)
    return 0

  parser.print_help()
  return 0


if __name__ == "__main__":
  sys.exit(main())
