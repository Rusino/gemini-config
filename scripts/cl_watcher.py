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
   report `INBOX` vs `TRASH` message counts per CL/PR and support one-click
   `INBOX` archiving (`batch_unlabel_threads` with `includeTrash=true`).
4. Updates a compact progress badge on the conversation title and wakes the
   target conversation via `agentapi send-message` only on significant transitions.
5. Serves the live 5-column Markdown-styled "CL Flight Deck" HTML dashboard in English.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import glob
import http.server
import json
import os
import re
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
MCP_TOKENS_FILE = os.path.expanduser("~/.gemini/jetski/mcp_oauth_tokens.json")
GMAIL_MCP_URL = "https://gmailmcp.googleapis.com/mcp/v1"
NOTIFY_SELF_BIN = "/google/bin/releases/g3n-team/notify_self"
PEN_URL_SH = (
    "/google/src/files/head/depot/google3/security/uberproxy/tools/contrib/pen_url.sh"
)
SELF_EMAILS = {"jlavrova@google.com", "jlavrova@gmail.com"}
SELF_GITHUB_LOGINS = {"Rusino", "jlavrova"}
FLUTTER_TREE_CHECK = "tree-status"
GITHUB_STATUS_BOTS = {"flutter-dashboard"}

# Regexes to extract CL and PR identifiers from chat titles
RE_CL_ID = re.compile(r"(?:\bcl/|\bCL\s+)(\d{6,10})\b", re.IGNORECASE)
# Avoid matching flutter#160251 when it is a GitHub issue next to a Skia CL,
# unless explicitly prefixed with PR
RE_PR_ID = re.compile(r"(?:\bPR\s*#?|\bpull/)(\d{5,7})\b", re.IGNORECASE)
# Strip any trailing roll badge we previously attached to a title
TITLE_TOPIC_MAX = 38
RE_ROLL_BADGE = re.compile(r"\s*\[(?:⏳|✓|🔴|🔄)[^\]]*\]\s*$")
# Automated roll CLs/PRs belong to the Skia Gardener rather than personal Flight Deck rows
RE_AUTOROLL_SUBJECT = re.compile(
    r"^Roll\s+(?:Skia\b|\S+\s+from\s+[0-9a-f]{7,40}\s+to\s+[0-9a-f]{7,40}\b)",
    re.IGNORECASE,
)

_GMAIL_TOKEN_CACHE: dict[str, Any] = {"access_token": "", "expires_at": 0.0}
_GMAIL_LOCK = threading.Lock()

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
  """Parses Gerrit ('2026-10-26 15:15:24.000000000') or ISO-8601 UTC timestamps."""
  if not ts_str:
    return None
  s = ts_str.strip()
  try:
    if s.endswith("Z"):
      s = s[:-1]
    if "." in s:
      head, frac = s.split(".", 1)
      frac_digits = re.sub(r"[^0-9]", "", frac)[:6]
      s = f"{head}.{frac_digits}" if frac_digits else head
    s = s.replace("T", " ")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return dt
  except Exception:
    return None


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


def discover_active_cl_chats() -> list[dict[str, Any]]:
  """Scans active (`⦿`) conversations and maps them to CLs or GitHub PRs."""
  summaries = cl._fetch_ls_summaries_for_audit() or {}
  proj_names = load_project_names()
  discovered: list[dict[str, Any]] = []

  for cid, s in summaries.items():
    if not isinstance(s, dict):
      continue
    ann = s.get("annotations") or {}
    if ann.get("archived"):
      continue
    meta = s.get("trajectoryMetadata") or {}
    if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
      continue
    if (meta.get("sourceMetadata") or {}).get("tool"):
      continue
    title = str(ann.get("title") or s.get("summary") or "").strip()
    if "⦿" not in title:
      continue

    pid = str(meta.get("projectId") or "").strip()
    pname = proj_names.get(pid, "Unassigned")
    last_mod = str(meta.get("lastModifiedTime") or meta.get("createdAt") or "")

    # Deduplicate IDs when a title repeats the same CL or PR number
    cls_found = list(dict.fromkeys(RE_CL_ID.findall(title)))
    prs_found = list(dict.fromkeys(RE_PR_ID.findall(title)))
    if not cls_found and not prs_found:
      continue
    # A slash list like "PR #194466/#194475" or multiple IDs means the chat is not 1:1 with one CL
    multi_slash = bool(
        re.search(
            r"(?:PR\s*#?\d+|cl/\d+|CL\s+\d+)\s*[/,&+]\s*#?\d+",
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
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": single_target,
      })
    for pr_id in prs_found:
      discovered.append({
          "item_key": f"github:{pr_id}",
          "kind": "github",
          "target_id": pr_id,
          "conversation_id": cid,
          "title": title,
          "project_id": pid,
          "project_name": pname,
          "chat_last_modified": last_mod,
          "single_target": single_target,
      })

  # Sort newest chat modification first
  discovered.sort(key=lambda x: x["chat_last_modified"], reverse=True)
  return discovered


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
) -> dict[str, Any]:
  """Probes Skia and Chromium Gerrit for a CL."""
  for host in ("skia-review.googlesource.com", "chromium-review.googlesource.com"):
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
      role = (
          "author"
          if is_self
          else ("reviewer (ext)" if is_external else "reviewer (goog)")
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
      waiting_on = [
          {"id": e, "active": google_account_active(e)}
          for e in att_set
          if e and e not in SELF_EMAILS and not e.endswith(".gserviceaccount.com")
      ]
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
      merge_conflict = status == "NEW" and not gerrit_mergeable(host, cl_id)

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

      cq_failed = False
      cq_status = "Idle"
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
          break
        if "passed the SkCQ dry run" in txt and max_cq == 0:
          cq_status = "✓ Dry Run Passed"
          break

      submitted_raw = str(d.get("submitted") or "")
      submitted_et, submitted_age = format_et_and_age(submitted_raw)
      last_msg_et, last_msg_age = format_et_and_age(last_msg_date)

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
        # Once removed from reviewers, CQ failures belong to the author and must not alert us
        if cq_failed and not reviewer_removed:
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
          "post_submit": post_submit,
      }
    except Exception:
      continue
  return {"ok": False, "error": f"CL {cl_id} not found on Gerrit"}


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
            "number,title,state,author,mergedAt,headRefOid,reviews,comments,"
            "statusCheckRollup,url,mergeable,reviewRequests",
        ],
        stderr=subprocess.STDOUT,
        text=True,
        timeout=12,
    )
    d = json.loads(out)
    state = str(d.get("state") or "OPEN")
    author_login = str((d.get("author") or {}).get("login") or "")
    is_self = author_login in SELF_GITHUB_LOGINS
    role = "author" if is_self else "reviewer"
    head_sha = str(d.get("headRefOid") or "")

    checks = d.get("statusCheckRollup") or []
    merge_conflict = str(d.get("mergeable") or "") == "CONFLICTING"
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
    pending_checks = [
        str(c.get("name") or "")
        for c in checks
        if str(c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
    ]

    if failed_checks:
      cq_status = f"🔴 {len(failed_checks)} check(s) failed"
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
    if events:
      events.sort(key=lambda x: x[0])
      last_date, last_actor, last_snippet = events[-1]
      last_snippet = last_snippet.replace("\n", " ")[:180]

    merged_raw = str(d.get("mergedAt") or "")
    submitted_et, submitted_age = format_et_and_age(merged_raw)
    last_msg_et, last_msg_age = format_et_and_age(last_date)

    if state == "MERGED":
      phase = "READY_TO_CLOSE"
    elif state == "CLOSED":
      phase = "ABANDONED"
    elif failed_checks:
      phase = "ALERT_CQ_FAILED"
    elif last_actor and last_actor not in SELF_GITHUB_LOGINS:
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
        "subject": str(d.get("title") or f"PR #{pr_id}"),
        "owner_name": author_login,
        "owner_email": author_login,
        "role": role,
        "is_self": is_self,
        "is_external": not is_self,
        "patchset": len(reviews) + 1,
        "revision": head_sha[:10],
        "commit_url": (
            f"https://github.com/flutter/flutter/commit/{head_sha}" if head_sha else ""
        ),
        "in_my_attention": phase in ("NEEDS_YOUR_ACTION", "ALERT_CQ_FAILED"),
        "cr_summary": f"{len(reviews)} rev",
        "cq_status": cq_status,
        "cq_failed": bool(failed_checks),
        "tree_red": tree_red,
        "merge_conflict": merge_conflict,
        "waiting_on": [
            {"id": str(r.get("login") or r.get("name") or ""), "active": True}
            for r in d.get("reviewRequests") or []
            if r.get("login") or r.get("name")
        ],
        "last_msg_author": last_actor,
        "last_msg_date": last_date,
        "last_msg_et": last_msg_et,
        "last_msg_age": last_msg_age,
        "last_msg_snippet": last_snippet,
        "submitted": merged_raw,
        "submitted_et": submitted_et,
        "submitted_age": submitted_age,
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
    phase = "READY_TO_CLOSE" if status == "SUBMITTED" else "WAITING_ON_OTHERS"
    return {
        "ok": True,
        "host": "critique.corp.google.com",
        "url": f"http://cl/{cl_id}",
        "status": "MERGED" if status == "SUBMITTED" else "NEW",
        "phase": phase,
        "subject": subject,
        "owner_name": owner,
        "owner_email": f"{owner}@google.com",
        "role": "author" if is_self else "reviewer (goog)",
        "is_self": is_self,
        "is_external": False,
        "patchset": 1,
        "revision": cl_id,
        "in_my_attention": False,
        "cr_summary": "—",
        "cq_status": status,
        "cq_failed": False,
        "last_msg_author": owner,
        "last_msg_date": "",
        "last_msg_et": "",
        "last_msg_age": "",
        "last_msg_snippet": subject,
        "submitted": "",
        "submitted_et": "",
        "submitted_age": "",
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
  if not cl_info.get("reviewer_removed") or not chat_item.get("single_target", True):
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
    tokens = [m.group(0) for r in (RE_CL_ID, RE_PR_ID) for m in r.finditer(base_no_badge)]
    rest = base_no_badge
    for tok in tokens:
      rest = rest.replace(tok, " ")
    kept = " ".join(tokens + rest.split())
    new_topic = f"{kept[:avail].rstrip()} {badge}"
  if new_topic == base:
    return
  try:
    cl.set_conversation_topic(conv_id, new_topic)
  except Exception:
    pass


def sync_once(
    wake_chats: bool = True,
    update_titles: bool = True,
    skia_dir: str = DEFAULT_SKIA_DIR,
) -> dict[str, Any]:
  """Performs one full discovery and status poll across all active CL/PR chats."""
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
  discovered = discover_active_cl_chats()

  # Cache fetched CL/PR metadata so multiple chats referencing the same CL don't re-fetch
  fetched_cache: dict[str, dict[str, Any]] = {}
  active_discovered: list[dict[str, Any]] = []
  for d in discovered:
    ikey = d["item_key"]
    tid = d["target_id"]
    if ikey not in fetched_cache:
      if d["kind"] == "gerrit":
        fetched_cache[ikey] = fetch_gerrit_cl_status(
            tid, rollers, incidents, skia_dir=skia_dir
        )
      elif d["kind"] == "github":
        fetched_cache[ikey] = fetch_github_pr_status(tid)
      else:
        fetched_cache[ikey] = fetch_piper_cl_status(tid)
    info = fetched_cache[ikey]
    # Drop broken lookups and automated rolls before querying Gmail or building rows
    if not info.get("ok") or is_autoroll_change(info):
      continue
    active_discovered.append(d)

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

  for d in active_discovered:
    ikey = d["item_key"]
    cid = d["conversation_id"]
    tid = d["target_id"]
    composite_key = f"{ikey}@{cid}"
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
                    parse_gerrit_or_iso_time(info.get("last_msg_date") or ""),
                    parse_gerrit_or_iso_time(info.get("submitted") or ""),
                )
                if dt
            ),
            default=0.0,
        ),
    }

    prev = prev_items.get(composite_key) or {}
    post = info.get("post_submit") or {}
    badge = post.get("compact_badge", "")

    if update_titles and badge:
      update_chat_roll_badge(cid, d["title"], badge)

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
        if wake_chats:
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
<title>CL &amp; PR Flight Deck · Skia &amp; Flutter</title>
<style>
  :root {
    --bg: #ffffff;
    --bg-subtle: #f6f8fa;
    --row-hover: #f3f6f9;
    --border: #d0d7de;
    --text: #1f2328;
    --muted: #59636e;
    --link: #0969da;
    --code-bg: rgba(175, 184, 193, 0.22);
    --alert-row: #ffebe9;
    --warn-bg: #fff8c5;
    --warn-border: #d4a72c;
    --ok-text: #1a7f37;
    --danger-text: #cf222e;
  }
  [data-theme="dark"] {
    --bg: #0d1117;
    --bg-subtle: #161b22;
    --row-hover: #1c2128;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --link: #58a6ff;
    --code-bg: rgba(110, 118, 129, 0.28);
    --alert-row: rgba(248, 81, 73, 0.12);
    --warn-bg: rgba(210, 153, 34, 0.15);
    --warn-border: rgba(210, 153, 34, 0.45);
    --ok-text: #3fb950;
    --danger-text: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 24px 32px 48px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans", Helvetica, Arial, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    max-width: 1560px;
    margin-left: auto;
    margin-right: auto;
  }
  .top-header {
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    border-bottom: 1px solid var(--border);
    padding-bottom: 12px;
    margin-bottom: 16px;
  }
  .top-header h1 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
  }
  .meta-line {
    color: var(--muted);
    font-size: 13px;
    margin-top: 4px;
  }
  .toolbar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px;
  }
  button {
    background: var(--bg-subtle);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 5px 11px;
    font-size: 12.5px;
    font-weight: 500;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  button:hover {
    background: var(--row-hover);
  }
  button.active-tab {
    background: var(--link);
    color: #ffffff;
    border-color: var(--link);
  }
  button.btn-sm {
    padding: 2px 8px;
    font-size: 11.5px;
    border-radius: 5px;
  }
  button.btn-warn {
    background: var(--warn-bg);
    border-color: var(--warn-border);
    color: var(--text);
    font-weight: 600;
  }
  .project-tabs {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 6px;
    margin-bottom: 18px;
  }
  .rollers-inline {
    background: var(--bg-subtle);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 8px 14px;
    margin-bottom: 18px;
    font-size: 13px;
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 18px;
  }
  .section-title {
    font-size: 16px;
    font-weight: 600;
    margin: 22px 0 10px 0;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  /* Markdown-style table */
  table.md-table {
    width: 100%;
    border-collapse: collapse;
    margin-bottom: 20px;
    font-size: 13.5px;
    border: 1px solid var(--border);
  }
  table.md-table th,
  table.md-table td {
    border: 1px solid var(--border);
    padding: 10px 13px;
    text-align: left;
    vertical-align: top;
  }
  table.md-table th {
    background: var(--bg-subtle);
    font-weight: 600;
    font-size: 13px;
  }
  table.md-table tr:nth-child(even) td {
    background: rgba(175, 184, 193, 0.04);
  }
  table.md-table tr:hover td {
    background: var(--row-hover);
  }
  table.md-table tr.row-alert td {
    background: var(--alert-row);
  }
  code {
    font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    font-size: 12px;
    background: var(--code-bg);
    padding: 2px 5px;
    border-radius: 4px;
    word-break: break-word;
  }
  a {
    color: var(--link);
    text-decoration: none;
  }
  a:hover {
    text-decoration: underline;
  }
  .muted {
    color: var(--muted);
    font-size: 12.5px;
  }
  .cell-actions {
    margin-top: 7px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .filter-row {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 10px;
  }
  .filter-row .project-tabs {
    margin-bottom: 0;
  }
  .filter-label {
    width: 56px;
    color: var(--muted);
    font-size: 12px;
    font-weight: 600;
    text-transform: uppercase;
  }
  button.seg {
    font-size: 13px;
    padding: 6px 12px;
  }
  button.chip {
    border-radius: 999px;
    padding: 2px 10px;
    font-size: 12px;
    background: transparent;
  }
  button.chip.active-tab {
    background: var(--link);
  }
  button.empty:not(.active-tab) {
    opacity: 0.45;
  }
  button .count {
    font-variant-numeric: tabular-nums;
    opacity: 0.8;
  }
  .col-chat { width: 21%; }  .col-chat { width: 21%; }
  .col-cl { width: 25%; }
  .col-gerrit { width: 20%; }
  .col-rolls { width: 17%; }
  .col-gmail { width: 17%; }
</style>
</head>
<body>

<div class="top-header">
  <div>
    <h1>Active CL &amp; PR Flight Deck (<span id="total-count">0</span> open chats)</h1>
    <div class="meta-line" id="sync-status">Syncing...</div>
  </div>
  <div class="toolbar">
    <button id="notif-btn" onclick="enableNotifications()">🔔 Browser Alerts</button>
    <button onclick="testNotification()">🔊 Test Sound</button>
    <button onclick="toggleTheme()" id="theme-btn">🌙 Dark Theme</button>
    <button onclick="triggerRefresh()">⟳ Poll Now</button>
  </div>
</div>

<div class="rollers-inline" id="rollers-bar"></div>

<div class="filter-row">
  <span class="filter-label">Status</span>
  <div class="project-tabs" id="status-tabs"></div>
</div>
<div class="filter-row">
  <span class="filter-label">Project</span>
  <div class="project-tabs" id="project-tabs"></div>
</div>

<div id="tables-container"></div>

<script>
let cachedData = null;
let selectedProject = 'ALL';
let selectedStatus = 'attention';
let seenEventIds = new Set();
let initialLoadDone = false;

function initTheme() {
  const saved = localStorage.getItem('cl_deck_theme') || 'light';
  document.documentElement.setAttribute('data-theme', saved);
  updateThemeBtn(saved);
}

function toggleTheme() {
  const cur = document.documentElement.getAttribute('data-theme') || 'light';
  const next = cur === 'dark' ? 'light' : 'dark';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('cl_deck_theme', next);
  updateThemeBtn(next);
}

function updateThemeBtn(theme) {
  const btn = document.getElementById('theme-btn');
  btn.textContent = theme === 'dark' ? '☀️ Light Theme' : '🌙 Dark Theme';
}

function esc(s) {
  return String(s || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function shortActor(raw) {
  const s = String(raw || '');
  if (s.includes('skcq-be') || s.includes('gerritcodereview')) return 'SkCQ';
  if (s.endsWith('@google.com')) return s.replace('@google.com', '@');
  if (s.endsWith('@chromium.org')) return s.replace('@chromium.org', '@chromium');
  return s;
}

function updateNotifButton() {
  const btn = document.getElementById('notif-btn');
  if (!('Notification' in window)) {
    btn.textContent = '🔕 Alerts Unsupported';
    return;
  }
  if (Notification.permission === 'granted') {
    btn.textContent = '🔔 Browser Alerts: ON';
  } else if (Notification.permission === 'denied') {
    btn.textContent = '🔕 Browser Alerts: Blocked';
  }
}

async function enableNotifications() {
  if (!('Notification' in window)) return;
  await Notification.requestPermission();
  updateNotifButton();
}

function playAlertBeep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'triangle';
    osc.frequency.setValueAtTime(660, ctx.currentTime);
    osc.frequency.setValueAtTime(880, ctx.currentTime + 0.12);
    gain.gain.setValueAtTime(0.18, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + 0.4);
  } catch (e) {}
}

function fireBrowserNotification(title, body, critical) {
  if (critical) playAlertBeep();
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body, requireInteraction: Boolean(critical) });
  }
}

function testNotification() {
  fireBrowserNotification(
    '🔴 CL Flight Deck Test Alert',
    'Browser notifications and audio chime are active.',
    true
  );
}

function link(url, html) {
  return url ? `<a href="${esc(url)}" target="_blank" rel="noopener">${html}</a>` : html;
}

// Maps a Gerrit email, GitHub login, or Piper LDAP to the page that profiles that person
function actorUrl(it, raw) {
  const s = String(raw || '');
  if (!s || s === 'bot' || shortActor(s) === 'SkCQ') return '';
  if (it.kind === 'github') return `https://github.com/${encodeURIComponent(s)}`;
  if (s.endsWith('@google.com')) {
    return `https://moma.corp.google.com/person/${encodeURIComponent(s.split('@')[0])}`;
  }
  if (it.kind === 'piper') return `https://moma.corp.google.com/person/${encodeURIComponent(s)}`;
  if (s.includes('@') && it.host) return `https://${it.host}/q/owner:${encodeURIComponent(s)}`;
  return '';
}

function actorHtml(it, raw) {
  return link(actorUrl(it, raw), `<code>${esc(shortActor(raw))}</code>`);
}

function checksUrl(it) {
  if (it.kind === 'github') return `${it.url}/checks`;
  if (it.kind === 'gerrit') return `${it.url}?tab=checks`;
  return it.url;
}

function commitUrl(sha) {
  return sha ? `https://skia.googlesource.com/skia/+/${sha}` : '';
}

function formatAuthorAndCl(it) {
  let label = `CL ${esc(it.target_id)}`;
  if (it.kind === 'github') label = `PR #${esc(it.target_id)}`;
  else if (it.kind === 'piper') label = `cl/${esc(it.target_id)}`;

  const who = actorHtml(it, it.owner_email || it.owner_name);
  let roleDesc = '';
  if (it.is_self) {
    roleDesc = `${who} — your CL`;
  } else if (it.is_external) {
    roleDesc = `${who} — external author`;
  } else {
    roleDesc = `${who} — colleague`;
  }
  const subject = link(it.url, `<em>${esc(it.subject)}</em>`);
  return `<strong>${link(it.url, label)}</strong> (${roleDesc})<br>${subject}`;
}

const FLUTTER_DASHBOARD_URL = 'https://flutter-dashboard.appspot.com/#/build';

// Copies a ping draft and opens the place to paste it: Chat for Googlers, the review
// page for everyone else
function pingPerson(btn) {
  if (navigator.clipboard) navigator.clipboard.writeText(btn.dataset.text);
  window.open(btn.dataset.url, '_blank', 'noopener');
  btn.textContent = '✓ Copied';
}

function waitingOnHtml(it) {
  const people = it.waiting_on || [];
  if (people.length === 0) return '';
  const items = people.map(p => {
    const who = actorHtml(it, p.id);
    if (!p.active) return `<s>${who}</s> (left Google)`;
    let url = it.url;
    let text = `Hi ${shortActor(p.id)}, friendly ping on ${it.url} ("${it.subject}"). ` +
      'Could you take a look when you have a moment? Thanks!';
    if (it.kind === 'github') {
      text = `@${p.id} friendly ping on this PR when you have a moment. Thanks!`;
    } else if (p.id.endsWith('@google.com')) {
      url = `https://moma.corp.google.com/chat?with=${encodeURIComponent(p.id.split('@')[0])}`;
    }
    const btn = `<button class="btn-sm" title="Copy a ping draft and open where to paste it" ` +
      `data-url="${esc(url)}" data-text="${esc(text)}" onclick="pingPerson(this)">` +
      '📋 Ping</button>';
    return `${who} ${btn}`;
  });
  return `<div class="muted" style="margin-top:4px">Waiting on: ${items.join(', ')}</div>`;
}

// Notes for conditions that sit next to review state: they decide the status chip
// but do not replace the review summary
function statusNotesHtml(it) {
  if (it.status !== 'NEW' && it.status !== 'OPEN') return '';
  let notes = '';
  if (it.merge_conflict) {
    notes += '<div style="color:var(--danger-text)">⛔ Merge conflict: rebase needed</div>';
  }
  if (it.tree_red) {
    notes += `<div class="muted">🌲 ${link(FLUTTER_DASHBOARD_URL, 'flutter/flutter tree')} ` +
      'is red; landing waits for it</div>';
  }
  if (['waiting', 'cq', 'tree', 'conflict'].includes(statusOf(it))) notes += waitingOnHtml(it);
  return notes;
}

function formatGerritStatusCell(it) {
  return statusCoreHtml(it) + statusNotesHtml(it);
}

function statusCoreHtml(it) {
  const ps = `PS${it.patchset || 1}`;
  if (it.status === 'MERGED') {
    const post = it.post_submit;
    const isDone = !post || post.all_passed;
    const icon = (post && post.has_alert) ? '🔴' : (isDone ? '🏁' : '🟢');
    const sha = link(it.commit_url, `<code>${esc((it.revision || '').slice(0, 10))}</code>`);
    let timeLine = '';
    if (it.submitted_et) {
      const agePart = it.submitted_age ? ` (${esc(it.submitted_age)})` : '';
      timeLine = `Submitted ${esc(it.submitted_et)}${agePart}, commit ${sha}.`;
    } else {
      timeLine = `Submitted, commit ${sha}.`;
    }
    return `${icon} <strong>${link(it.url, '<code>MERGED</code>')} (${ps})</strong>` +
      `<br><span class="muted">${timeLine}</span>`;
  }

  if (it.status === 'ABANDONED' || it.status === 'CLOSED') {
    return `⚪ <strong>${link(it.url, `<code>${esc(it.status)}</code>`)} (${ps})</strong>`;
  }

  // Active NEW / OPEN CL or PR
  const lastWhoHtml = actorHtml(it, it.last_msg_author);
  const statusHtml = link(it.url, `<code>${esc(it.status)}</code>`);
  const cqHtml = link(checksUrl(it), esc(it.cq_status));
  const snippetHtml = link(it.url, esc(it.last_msg_snippet));
  if (it.cq_failed) {
    return `🔴 <strong>${statusHtml} (${ps} · ${cqHtml})</strong>` +
      `<br><span class="muted">Latest from ${lastWhoHtml}: ${snippetHtml}</span>`;
  }
  if (it.phase === 'SUBMITTING') {
    const agePart = it.last_msg_age ? ` (${esc(it.last_msg_age)})` : '';
    const skcq = link(checksUrl(it), '<code>SkCQ</code>');
    return `🟡 <strong>${statusHtml} (${ps} in CQ+2)</strong>` +
      `<br><span class="muted">Submitted to CQ${agePart}, running ${skcq}.</span>`;
  }
  if (needsMyReply(it)) {
    const agePart = it.last_msg_age ? ` (${esc(it.last_msg_age)})` : '';
    return `⚡ <strong>${statusHtml} (${ps} · needs your reply)</strong><br>` +
      `<span class="muted">CR: ${esc(it.cr_summary)} · ${cqHtml}. ` +
      `From ${lastWhoHtml}${agePart}: ${snippetHtml}</span>`;
  }
  return `⏸️ <strong>${statusHtml} (${ps})</strong>` +
    `<br><span class="muted">In progress (CR: ${esc(it.cr_summary)} · ${cqHtml}).</span>`;
}

// One predicate drives both the ⚡ badge and the sort, so the top rows are exactly the
// ⚡ ones. A bot message on my own CL keeps me in the attention set but asks nothing
function needsMyReply(it) {
  return Boolean(it.in_my_attention) && !it.cq_failed && it.phase !== 'SUBMITTING' &&
    !(it.is_self && shortActor(it.last_msg_author) === 'SkCQ');
}

// Merged CLs older than this are chats left open after the work ended
const OLD_MERGED_DAYS = 3;

// Status filters in display and sort order; one classifier drives the chips, their
// counts, and the row order, so a row never shows up under two statuses
const STATUSES = [
  {id: 'attention', label: '⚡ Needs you'},
  {id: 'conflict', label: '⛔ Conflict'},
  {id: 'tree', label: '🌲 Tree red'},
  {id: 'cq', label: '⏳ CQ running'},
  {id: 'waiting', label: '⏸ Waiting on others'},
  {id: 'merged', label: '🟢 Merged'},
  {id: 'old', label: '📦 OLD'},
  {id: 'abandoned', label: '⚪ Abandoned'},
];

function isAlert(it) {
  return (it.phase || '').startsWith('ALERT_');
}

// Alerts count as needing me even on merged CLs, since a blamed commit needs a reaction
function statusOf(it) {
  if (isAlert(it) || needsMyReply(it)) return 'attention';
  if (it.status === 'NEW' || it.status === 'OPEN') {
    // A conflict needs my rebase now; a red tree clears by itself, so it comes second
    if (it.merge_conflict) return 'conflict';
    if (it.tree_red) return 'tree';
  }
  if (it.status === 'ABANDONED' || it.status === 'CLOSED') return 'abandoned';
  if (it.status === 'MERGED') {
    const ageDays = (Date.now() / 1000 - (it.submitted_ts || 0)) / 86400;
    return ageDays > OLD_MERGED_DAYS ? 'old' : 'merged';
  }
  const cq = String(it.cq_status || '');
  if (it.phase === 'SUBMITTING' || cq.startsWith('CQ+') || cq.includes('running')) return 'cq';
  return 'waiting';
}

function compareRows(a, b) {
  const sa = STATUSES.findIndex(x => x.id === statusOf(a));
  const sb = STATUSES.findIndex(x => x.id === statusOf(b));
  return sa - sb || Number(isAlert(b)) - Number(isAlert(a)) ||
    (b.activity_ts || 0) - (a.activity_ts || 0);
}

const INCIDENT_VERDICT_LABEL = {
  blamed: '🔴 blamed',
  flake: '⚪ flake',
  low_confidence: '⚠️ low-confidence suspect',
  blamelist: '⚪ in blamelist only'
};

function formatIncidentLine(h) {
  const task = link(h.task_url, `<code>${esc(h.task_name)}</code>`);
  const swarm = h.swarming_url ? ` ${link(h.swarming_url, 'log')}` : '';
  let culprit = '';
  if (h.verdict === 'blamelist' && h.culprit_short) {
    const cl = h.culprit_cl ? link(h.culprit_cl_url, `CL ${esc(h.culprit_cl)}`) : '';
    const sha = link(h.culprit_commit_url, `<code>${esc(h.culprit_short)}</code>`);
    culprit = `; Gardener culprit: ${sha} ${cl}`;
  } else if (h.confidence_score) {
    culprit = ` (${h.confidence_score}%)`;
  }
  const why = h.reason ? `<br><span class="muted">${esc(h.reason)}</span>` : '';
  const label = INCIDENT_VERDICT_LABEL[h.verdict] || esc(h.verdict);
  return `<div>${label}: ${task}${swarm}${culprit}${why}</div>`;
}

function formatRollsCell(it) {
  if (it.status !== 'MERGED') {
    if (it.phase === 'SUBMITTING') {
      return `Waiting for submit`;
    }
    return `<span class="muted">In review / development</span>`;
  }
  const ps = it.post_submit;
  if (!ps) {
    return `<strong><code>✓ Merged</code></strong>`;
  }
  const statusLink = (html) => link('https://status.skia.org/repo/skia', html);
  if (ps.all_passed) {
    return `<strong><code>${statusLink('✓Status')} · ✓And ✓Chr ✓G3 ✓Flt</code></strong>` +
      `<br><span class="muted">(all 4/4 rolls passed!)</span>`;
  }
  const r = ps.rolls || {};
  const rollHtml = (name, label) => {
    const x = r[name] || {};
    return link(x.url, `<code title="${esc(x.detail || '')}">${label}: ${x.state || '⏳'}</code>`);
  };
  const cnt = ps.passed_rolls ?? 0;
  let header = `<code>${statusLink(`${esc(ps.skia_status)}Status`)} · ${cnt}/4 rolls</code>`;
  if (ps.has_alert) {
    const reason = `<code>🔴 ${esc(ps.alert_reason || 'Roll/Status Alert')}</code>`;
    header = `<strong style="color:var(--danger-text)">${statusLink(reason)}</strong>`;
  }
  const incidents = (ps.incidents || []).map(formatIncidentLine).join('');
  const gardener = cachedData && cachedData.gardener_url
    ? ` · ${link(cachedData.gardener_url, 'Gardener')}`
    : '';
  const rolls = [['Android', 'Android'], ['Chrome', 'Chrome'], ['Google3', 'G3'],
    ['Flutter', 'Flutter']].map(([name, label]) => rollHtml(name, label)).join(', ');
  const incidentsHtml = incidents
    ? `<div class="muted" style="margin-top:4px">${incidents}</div>`
    : '';
  return `${header}<br><span class="muted">(${rolls})${gardener}</span>${incidentsHtml}`;
}

function formatGmailCell(it) {
  const g = it.gmail || {};
  const tids = g.thread_ids || [];
  const inboxTids = g.inbox_thread_ids || [];
  const total = g.total_msgs || 0;
  const inb = g.inbox_msgs || 0;
  const tr = g.trash_msgs || 0;

  const searchUrl = 'https://mail.google.com/mail/u/0/#search/' +
    encodeURIComponent('in:anywhere ' + it.target_id);
  if (tids.length === 0) {
    return `<span class="muted">${link(searchUrl, 'No mail')}</span>`;
  }
  // State saved before threads[] existed has only IDs; fall back to one search link
  const threads = g.threads && g.threads.length
    ? g.threads
    : [{url: searchUrl, source: 'Mail', msgs: total}];
  const tidLabel = threads
    .map(t => link(t.url || searchUrl, `📧 ${esc(t.source)} thread`))
    .join(', ');

  if (inb > 0) {
    const trashPart = tr > 0 ? `${tr} in <code>TRASH</code>, ` : '';
    const tidJson = esc(JSON.stringify(inboxTids.length ? inboxTids : tids));
    return `⚠️ ${tidLabel} (${total} msgs: ${trashPart}<strong>${inb} in <code>INBOX</code>!</strong>)
      <div class="cell-actions">
        <button class="btn-sm btn-warn"
          title="Remove these threads from Gmail INBOX; they stay in All Mail"
          onclick='archiveGmail(this, "${esc(it.target_id)}", ${tidJson})'>📥 Archive</button>
      </div>`;
  }
  if (tr > 0) {
    const arch = Math.max(0, total - tr);
    const archPart = arch > 0 ? `, ${arch} archived` : '';
    return `${tidLabel} (${tr} in <code>TRASH</code>${archPart})`;
  }
  return `${tidLabel} (${total} msgs archived)`;
}

async function archiveGmail(btn, targetId, threadIds) {
  btn.disabled = true;
  btn.textContent = '⏳ Archiving…';
  try {
    const resp = await fetch('/api/archive_gmail', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({target_id: targetId, thread_ids: threadIds})
    });
    if (!resp.ok) throw new Error(resp.statusText);
    await fetchState();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = '⚠️ Archive failed, retry';
  }
}

async function finalizeChat(convId, targetId) {
  if (!confirm(`Finalize («») chat for ${targetId}?`)) return;
  await fetch('/api/finalize', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({conversation_id: convId, target_id: targetId})
  });
  await fetchState();
}

async function wakeChatForTriage(convId, itemKey, targetId) {
  await fetch('/api/wake', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      conversation_id: convId,
      message: `[CL-WATCHER] Please check the latest status of ${itemKey}, archive any handled Gmail threads for ${targetId} from INBOX, and summarize what action is needed.`
    })
  });
  alert(`Sent wakeup request to chat ${convId.slice(0, 8)}!`);
}

async function triggerRefresh() {
  document.getElementById('sync-status').textContent = 'Polling Gerrit, GitHub, Skia Gardener, and Gmail...';
  await fetch('/api/refresh', {method: 'POST'});
  await fetchState();
}

function renderTable(pname, rows) {
  return `
      <table class="md-table">
        <thead>
          <tr>
            <th class="col-chat">Chat in <code>${esc(pname)}</code></th>
            <th class="col-cl">CL / PR &amp; Author</th>
            <th class="col-gerrit">Gerrit / GitHub Status</th>
            <th class="col-rolls"><code>Skia Status</code> &amp; 4 Rolls</th>
            <th class="col-gmail">Gmail (<code>INBOX</code> vs <code>TRASH</code>)</th>
          </tr>
        </thead>
        <tbody>
          ${renderTableRows(rows)}
        </tbody>
      </table>`;
}

function renderProject(pname, rows) {
  return `
      <div class="section-title">
        <span>Project <code>${esc(pname)}</code> (${rows.length})</span>
      </div>
      ${renderTable(pname, rows)}`;
}

function readHashFilters() {
  const h = new URLSearchParams(location.hash.slice(1));
  selectedStatus = h.get('status') || 'attention';
  selectedProject = h.get('project') || 'ALL';
}

// The URL hash keeps the view across reloads and makes it bookmarkable
function setFilter(kind, value) {
  if (kind === 'status') selectedStatus = value;
  else selectedProject = value;
  const h = new URLSearchParams({status: selectedStatus, project: selectedProject});
  history.replaceState(null, '', `#${h}`);
  renderFromCache();
}

function chipHtml(cls, kind, value, label, count, active) {
  const state = (active ? ' active-tab' : '') + (count === 0 ? ' empty' : '');
  return `<button class="${cls}${state}" ` +
    `onclick="setFilter('${kind}', ${esc(JSON.stringify(value))})">` +
    `${label} <span class="count">${count}</span></button>`;
}

function renderTableRows(rows) {
  return rows.map(it => {
    const rowClass = (it.phase || '').startsWith('ALERT_') ? 'row-alert' : '';
    const shortCid = esc((it.conversation_id || '').slice(0, 8));
    return `<tr class="${rowClass}">
      <td class="col-chat">
        <code>${esc(it.title)}</code> (<code>${shortCid}</code>)
        <div class="cell-actions">
          <button class="btn-sm" onclick="wakeChatForTriage('${esc(it.conversation_id)}', '${esc(it.item_key)}', '${esc(it.target_id)}')">💬 Wake Chat</button>
          <button class="btn-sm" onclick="finalizeChat('${esc(it.conversation_id)}', '${esc(it.target_id)}')">✓ Finalize («»)</button>
        </div>
      </td>
      <td class="col-cl">${formatAuthorAndCl(it)}</td>
      <td class="col-gerrit">${formatGerritStatusCell(it)}</td>
      <td class="col-rolls">${formatRollsCell(it)}</td>
      <td class="col-gmail">${formatGmailCell(it)}</td>
    </tr>`;
  }).join('');
}

function renderFromCache() {
  if (!cachedData) return;
  const data = cachedData;
  document.getElementById('sync-status').textContent =
    `Last synced: ${data.last_sync || 'now'} · Auto-refresh every 20s`;

  // Render inline rollers summary bar
  const rsum = data.rollers_summary || {};
  document.getElementById('rollers-bar').innerHTML =
    `<strong>Skia Autorollers:</strong> ` +
    ['Android', 'Chrome', 'Google3', 'Flutter'].map(name => {
      const r = rsum[name] || {};
      const icon = (r.num_failed > 0) ? '🔴' : '🟢';
      const rev = r.last_roll_rev
        ? link(commitUrl(r.last_roll_rev), `<code>${esc(r.last_roll_rev.slice(0, 10))}</code>`)
        : '<code>—</code>';
      const inFlight = r.current_roll_rev
        ? `, rolling ${link(commitUrl(r.current_roll_rev),
            `<code>${esc(r.current_roll_rev.slice(0, 7))}</code>`)}`
        : '';
      const behind = `behind: ${r.num_behind ?? 0}${inFlight}`;
      return `<span>${icon} <strong>${link(r.url, name)}</strong>: ${rev} (${behind})</span>`;
    }).join('') +
    `<span>${link('https://status.skia.org/repo/skia', 'status.skia.org')}` +
    (data.gardener_url ? ` · ${link(data.gardener_url, 'Skia Gardener')}` : '') +
    `</span>`;

  const allItems = Object.values(data.items || {}).sort(compareRows);
  document.getElementById('total-count').textContent = allItems.length;

  const projOf = it => it.project_name || 'Unassigned';
  const preferredOrder = ['SkParagraph', 'Skia', 'WebParagraph', 'Skia Gardener', 'TextClusters API'];
  const projNames = [...new Set(allItems.map(projOf))].sort((a, b) => {
    const ia = preferredOrder.indexOf(a);
    const ib = preferredOrder.indexOf(b);
    if (ia !== -1 && ib !== -1) return ia - ib;
    if (ia !== -1) return -1;
    if (ib !== -1) return 1;
    return a.localeCompare(b);
  });
  if (selectedProject !== 'ALL' && !projNames.includes(selectedProject)) {
    selectedProject = 'ALL';
  }

  // Each axis counts rows under the other axis's current choice, so a chip's number
  // always equals the rows shown after clicking it
  const inProject = it => selectedProject === 'ALL' || projOf(it) === selectedProject;
  const inStatus = it => selectedStatus === 'all' || statusOf(it) === selectedStatus;
  const projRows = allItems.filter(inProject);
  const statusCount = id => projRows.filter(it => statusOf(it) === id).length;

  document.getElementById('status-tabs').innerHTML =
    STATUSES.map(st => chipHtml('seg', 'status', st.id, st.label, statusCount(st.id),
      selectedStatus === st.id)).join('') +
    chipHtml('seg', 'status', 'all', 'All', projRows.length, selectedStatus === 'all');

  const statusRows = allItems.filter(inStatus);
  const projCount = p => statusRows.filter(it => projOf(it) === p).length;
  document.getElementById('project-tabs').innerHTML =
    chipHtml('chip', 'project', 'ALL', 'All', statusRows.length, selectedProject === 'ALL') +
    projNames.map(p => chipHtml('chip', 'project', p, esc(p), projCount(p),
      selectedProject === p)).join('');

  const shown = statusRows.filter(inProject);
  const container = document.getElementById('tables-container');
  if (shown.length === 0) {
    const others = STATUSES.filter(st => st.id !== selectedStatus && statusCount(st.id) > 0)
      .map(st => `<a href="#" onclick="event.preventDefault(); ` +
        `setFilter('status', '${st.id}')">${st.label} (${statusCount(st.id)})</a>`)
      .join(' · ');
    const head = selectedStatus === 'attention'
      ? 'Nothing needs you right now.'
      : 'No chats match this filter.';
    container.innerHTML = `<p class="muted">${head}${others ? ` See ${others}.` : ''}</p>`;
  } else {
    const groups = selectedProject === 'ALL'
      ? projNames.filter(p => shown.some(it => projOf(it) === p))
      : [selectedProject];
    container.innerHTML = groups
      .map(p => renderProject(p, shown.filter(it => projOf(it) === p)))
      .join('');
  }

  // Fire browser notifications on new events
  const evList = data.events || [];
  for (const ev of evList) {
    if (!seenEventIds.has(ev.id)) {
      seenEventIds.add(ev.id);
      if (initialLoadDone) {
        fireBrowserNotification(`${ev.project_name}: ${ev.item_key}`, ev.message, ev.critical);
      }
    }
  }
  initialLoadDone = true;
}

async function fetchState() {
  try {
    const resp = await fetch('/api/status');
    cachedData = await resp.json();
    renderFromCache();
  } catch (e) {
    document.getElementById('sync-status').textContent = 'Connection error — retrying...';
  }
}

initTheme();
readHashFilters();
updateNotifButton();
fetchState();
setInterval(fetchState, 20000);
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
    length = int(self.headers.get("Content-Length") or 0)
    body = (
        self.rfile.read(length).decode("utf-8", errors="replace")
        if length > 0
        else "{}"
    )
    try:
      data = json.loads(body)
    except Exception:
      data = {}

    if parsed.path == "/api/refresh":
      state = sync_once(wake_chats=True, update_titles=True)
      self._send_json({"ok": True, "count": len(state.get("items") or {})})
      return

    if parsed.path == "/api/archive_gmail":
      tid = str(data.get("target_id") or "").strip()
      tids = data.get("thread_ids")
      if not isinstance(tids, list):
        tids = None
      res = archive_gmail_threads_for_id(tid, tids)
      # A full sync takes about a minute, which made the button look dead; the
      # next background poll re-reads Gmail anyway
      state = load_watcher_state()
      for item in (state.get("items") or {}).values():
        if item.get("target_id") == tid and item.get("gmail"):
          item["gmail"]["inbox_msgs"] = 0
          item["gmail"]["inbox_thread_ids"] = []
      save_watcher_state(state)
      self._send_json(res)
      return

    if parsed.path == "/api/finalize":
      cid = str(data.get("conversation_id") or "").strip()
      if cid:
        subprocess.run(
            [
                "python3",
                os.path.join(HOOKS_DIR, "chat_lifecycle.py"),
                "finalize",
                cid,
            ],
            check=False,
            timeout=10,
        )
      state = sync_once(wake_chats=False, update_titles=False)
      self._send_json({"ok": True, "count": len(state.get("items") or {})})
      return

    if parsed.path == "/api/wake":
      cid = str(data.get("conversation_id") or "").strip()
      msg = str(data.get("message") or "").strip()
      ok = wake_conversation(cid, msg)
      self._send_json({"ok": ok})
      return

    self._send_json({"error": "Not found"}, code=404)


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  allow_reuse_address = True
  daemon_threads = True


def serve_forever(
    port: int, poll_interval_sec: int, max_polls: int = 1000000
) -> None:
  """Runs the HTTP server and bounded background watcher loop."""

  def bg_loop() -> None:
    for _ in range(max_polls):
      try:
        sync_once(wake_chats=True, update_titles=True)
      except Exception:
        pass
      time.sleep(max(30, poll_interval_sec))

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()

  server = ThreadedHTTPServer(("0.0.0.0", port), FlightDeckHandler)
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
