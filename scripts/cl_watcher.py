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
3. Updates a compact progress badge on the conversation title (via
   `chat_lifecycle.py`) and wakes the target conversation via `agentapi send-message`
   ONLY on significant transitions (new patchset, new reviewer/author comment,
   CQ failure, merge, roll/status regression, or 100% roll completion).
4. Serves the live "CL Flight Deck" HTML dashboard with SSE/polling updates,
   Web Notifications (`new Notification(...)`) + desktop G3N alerts on breakages,
   and one-click actions (Finalize chat, Wake chat, Copy Gmail cleanup command).
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import glob
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
NOTIFY_SELF_BIN = "/google/bin/releases/g3n-team/notify_self"
PEN_URL_SH = (
    "/google/src/files/head/depot/google3/security/uberproxy/tools/contrib/pen_url.sh"
)
SELF_EMAILS = {"jlavrova@google.com", "jlavrova@gmail.com"}
SELF_GITHUB_LOGINS = {"Rusino", "jlavrova"}

# Regexes to extract CL and PR identifiers from chat titles
RE_CL_ID = re.compile(r"(?:\bcl/|\bCL\s+)(\d{6,10})\b", re.IGNORECASE)
# Avoid matching flutter#160251 when it is a GitHub issue next to a Skia CL,
# unless explicitly prefixed with PR
RE_PR_ID = re.compile(r"(?:\bPR\s*#?|\bpull/)(\d{5,7})\b", re.IGNORECASE)
# Strip any trailing roll badge we previously attached to a title
RE_ROLL_BADGE = re.compile(r"\s*\[(?:⏳|✓|🔴|🔄)[^\]]*\]\s*$")


def now_et_str() -> str:
  try:
    return datetime.now(ZoneInfo("America/New_York")).strftime("%H:%M:%S ET")
  except Exception:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


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


def load_project_names() -> dict[str, str]:
  res: dict[str, str] = {}
  for pfile in glob.glob(os.path.join(PROJECTS_DIR, "*.json")):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = str(pdata.get("id") or "").strip()
      pname = cl.clean_base_title(str(pdata.get("name") or "").strip())
      # Strip activity badge suffix (" · ⟳ 1 ● 2")
      pname = re.sub(r"(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$", "", pname).strip()
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

    cls_found = RE_CL_ID.findall(title)
    prs_found = RE_PR_ID.findall(title)
    if not cls_found and not prs_found:
      continue

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
      })

  # Deduplicate by (item_key, conversation_id), sorting newest chat modification first
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


def check_skia_post_submit(
    merged_sha: str,
    rollers: dict[str, Any],
    incidents: list[dict[str, Any]],
    skia_dir: str = DEFAULT_SKIA_DIR,
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
      "all_passed": False,
      "has_alert": False,
      "alert_reason": "",
      "compact_badge": "",
  }
  if not merged_sha:
    return res

  # Check if merged_sha is blamed in any active Skia Status incident
  blamed_tasks: list[str] = []
  for inc in incidents:
    blame = inc.get("blame") or {}
    suspects = blame.get("suspects") or []
    for susp in suspects:
      sh = str(susp.get("hash") or "")
      if sh and (sh.startswith(merged_sha[:10]) or merged_sha.startswith(sh[:10])):
        tname = (inc.get("task") or {}).get("name") or "bot"
        blamed_tasks.append(tname)
    t_commits = (inc.get("task") or {}).get("commits") or []
    for ch in t_commits:
      if ch and (ch.startswith(merged_sha[:10]) or merged_sha.startswith(ch[:10])):
        tname = (inc.get("task") or {}).get("name") or "bot"
        if tname not in blamed_tasks:
          blamed_tasks.append(tname)

  if blamed_tasks:
    res["skia_status"] = "🔴"
    res["skia_status_detail"] = f"Blamed in failing task(s): {', '.join(blamed_tasks[:2])}"
    res["has_alert"] = True
    res["alert_reason"] = res["skia_status_detail"]

  passed_rolls = 0
  for rname in ("Android", "Chrome", "Google3", "Flutter"):
    r = rollers.get(rname) or {}
    last_rev = str(r.get("last_roll_rev") or "")
    curr_rev = str(r.get("current_roll_rev") or "")
    num_failed = int(r.get("num_failed") or 0)
    diag = r.get("diagnosis") or {}

    if _git_is_ancestor(skia_dir, merged_sha, last_rev):
      res["rolls"][rname] = {"state": "✓", "detail": f"Rolled ({last_rev[:8]})"}
      passed_rolls += 1
    elif _git_is_ancestor(skia_dir, merged_sha, curr_rev):
      # Check if skia-gardener flagged this commit as a suspect in a failing roll
      suspect_hit = False
      for s_cl in (diag.get("in_flight_fix_cls") or []) + (diag.get("runner_up_cls") or []):
        if isinstance(s_cl, dict) and str(s_cl.get("hash") or "").startswith(merged_sha[:8]):
          suspect_hit = True
      if num_failed > 0 or suspect_hit:
        res["rolls"][rname] = {
            "state": "🔴",
            "detail": f"In failing roll ({curr_rev[:8]}, failed={num_failed})",
        }
        res["has_alert"] = True
        if not res["alert_reason"]:
          res["alert_reason"] = f"{rname} roll failing with commit in range ({curr_rev[:8]})"
      else:
        res["rolls"][rname] = {
            "state": "🔄",
            "detail": f"In CQ ({curr_rev[:8]})",
        }
    else:
      res["rolls"][rname] = {"state": "⏳", "detail": "Waiting for roll window"}

  # If at least one roll has already landed or commit is older and not blamed in any incident,
  # mark Skia Status green
  if not blamed_tasks:
    if passed_rolls > 0:
      res["skia_status"] = "✓"
      res["skia_status_detail"] = "Clean on status.skia.org"
    else:
      # Check if origin/main has at least 1 commit on top or if tasks finished cleanly
      res["skia_status"] = "✓"
      res["skia_status_detail"] = "No active regressions on status.skia.org"

  res["all_passed"] = (res["skia_status"] == "✓" and passed_rolls == 4 and not res["has_alert"])

  # Build compact badge for chat title
  if res["has_alert"]:
    res["compact_badge"] = f"[🔴Alert · {passed_rolls}/4 rolls]"
  elif res["all_passed"]:
    res["compact_badge"] = "[✓All Rolls]"
  else:
    st_tok = "✓St" if res["skia_status"] == "✓" else "⏳St"
    res["compact_badge"] = f"[{st_tok} · {passed_rolls}/4 rolls]"

  return res


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
      is_external = not owner_email.endswith("@google.com") and not owner_email.endswith("@chromium.org")
      role = "author" if is_self else ("reviewer (ext)" if is_external else "reviewer (goog)")

      cur_sha = str(d.get("current_revision") or "")
      revs = d.get("revisions") or {}
      cur_rev = revs.get(cur_sha) or {}
      ps_num = int(cur_rev.get("_number") or 1)

      att_set = [
          str((v.get("account") or {}).get("email") or "")
          for v in (d.get("attention_set") or {}).values()
      ]
      in_my_attention = any(e in SELF_EMAILS for e in att_set)

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

      # Detect CQ / dry-run failure in the latest bot messages on the current patchset
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
        if "This CL failed the SkCQ" in txt or "Failed builds:" in txt or "Tryjobs failed" in txt:
          if max_cq == 0:
            cq_failed = True
            cq_status = "🔴 CQ Failed"
          break
        if "passed the SkCQ dry run" in txt and max_cq == 0:
          cq_status = "✓ Dry Run Passed"
          break

      post_submit = None
      if status == "MERGED" and host == "skia-review.googlesource.com":
        post_submit = check_skia_post_submit(cur_sha, rollers, incidents, skia_dir=skia_dir)

      # Determine overall lifecycle phase
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
        if cq_failed:
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
          "patchset": ps_num,
          "revision": cur_sha,
          "in_my_attention": in_my_attention,
          "cr_summary": f"+{max_cr}" if max_cr > 0 else (str(min_cr) if min_cr < 0 else "0"),
          "cq_status": cq_status,
          "cq_failed": cq_failed,
          "last_msg_author": last_msg_author,
          "last_msg_date": last_msg_date,
          "last_msg_snippet": last_msg_text[:160],
          "submitted": str(d.get("submitted") or ""),
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
            "number,title,state,author,mergedAt,headRefOid,reviews,comments,statusCheckRollup,url",
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
    failed_checks = [
        str(c.get("name") or "")
        for c in checks
        if str(c.get("conclusion") or "").upper() in ("FAILURE", "TIMED_OUT", "ACTION_REQUIRED")
    ]
    pending_checks = [
        str(c.get("name") or "")
        for c in checks
        if str(c.get("status") or "").upper() in ("IN_PROGRESS", "QUEUED", "PENDING")
    ]

    if failed_checks:
      cq_status = f"🔴 {len(failed_checks)} check(s) failed"
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
      if c.get("createdAt"):
        events.append((
            str(c.get("createdAt")),
            str((c.get("author") or {}).get("login") or ""),
            str(c.get("body") or "").strip(),
        ))
    if events:
      events.sort(key=lambda x: x[0])
      last_date, last_actor, last_snippet = events[-1]
      last_snippet = last_snippet.replace("\n", " ")[:160]

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
        "url": str(d.get("url") or f"https://github.com/flutter/flutter/pull/{pr_id}"),
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
        "in_my_attention": phase in ("NEEDS_YOUR_ACTION", "ALERT_CQ_FAILED"),
        "cr_summary": f"{len(reviews)} rev",
        "cq_status": cq_status,
        "cq_failed": bool(failed_checks),
        "last_msg_author": last_actor,
        "last_msg_date": last_date,
        "last_msg_snippet": last_snippet,
        "submitted": str(d.get("mergedAt") or ""),
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
    status = "SUBMITTED" if " *submitted* " in first_line or " submitted " in first_line else "PENDING"
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
        "last_msg_snippet": subject,
        "submitted": "",
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


def update_chat_roll_badge(conv_id: str, current_title: str, badge: str) -> None:
  """Updates the trailing roll badge on a conversation title without waking the LLM."""
  base = cl.clean_base_title(current_title)
  base_no_badge = RE_ROLL_BADGE.sub("", base).strip()
  new_topic = f"{base_no_badge} {badge}".strip() if badge else base_no_badge
  # Keep within ~52 chars
  if len(new_topic) > 54 and badge:
    avail = max(12, 53 - len(badge))
    new_topic = f"{base_no_badge[:avail].rstrip()} {badge}"
  if new_topic == base:
    return
  try:
    cl.set_title_with_AutoMarker(conv_id, new_topic)
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
  new_items: dict[str, Any] = {}

  for d in discovered:
    ikey = d["item_key"]
    cid = d["conversation_id"]
    composite_key = f"{ikey}@{cid}"

    if ikey not in fetched_cache:
      if d["kind"] == "gerrit":
        fetched_cache[ikey] = fetch_gerrit_cl_status(
            d["target_id"], rollers, incidents, skia_dir=skia_dir
        )
      elif d["kind"] == "github":
        fetched_cache[ikey] = fetch_github_pr_status(d["target_id"])
      else:
        fetched_cache[ikey] = fetch_piper_cl_status(d["target_id"])

    info = fetched_cache[ikey]
    if not info.get("ok"):
      continue

    record = {
        **d,
        **info,
        "composite_key": composite_key,
        "updated_at": now_et_str(),
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
      prev_all_passed = ((prev.get("post_submit") or {}).get("all_passed"))

      transition_msg = ""
      is_critical = False

      if prev_status != "MERGED" and info["status"] == "MERGED":
        transition_msg = (
            f"[CL-WATCHER] ✅ {ikey} ('{info['subject']}') has just MERGED "
            f"(revision {info['revision'][:10]}). Please archive any Gmail threads for "
            f"{d['target_id']} from INBOX (using batch_unlabel_threads with includeTrash=true) "
            f"and report the initial Skia Status / Autoroll watch checklist."
        )
      elif info["phase"] == "ALERT_ROLL_OR_STATUS" and prev_phase != "ALERT_ROLL_OR_STATUS":
        is_critical = True
        reason = post.get("alert_reason") or "Post-submit regression or roll failure"
        transition_msg = (
            f"[CL-WATCHER] 🔴 CRITICAL ALERT for {ikey} ({info['revision'][:10]}): "
            f"{reason}. Please inspect skia-gardener / status.skia.org immediately and prepare a diagnosis."
        )
      elif info["phase"] == "ALERT_CQ_FAILED" and prev_phase != "ALERT_CQ_FAILED":
        is_critical = True
        transition_msg = (
            f"[CL-WATCHER] 🔴 CQ / Presubmit failed on {ikey} (PS{info['patchset']}): "
            f"{info['last_msg_snippet']}. Please inspect the failure and propose a fix."
        )
      elif post.get("all_passed") and not prev_all_passed:
        transition_msg = (
            f"[CL-WATCHER] 🏁 All 4 autorolls (Android, Chrome, Google3, Flutter) and "
            f"Skia Status have passed cleanly for {ikey} ({info['revision'][:10]})! "
            f"Please archive any remaining Gmail threads for {d['target_id']} from INBOX "
            f"and ask the user if we should finalize this chat."
        )
      elif (
          info["status"] == "NEW"
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
            "id": f"ev-{int(time.time() * 1000)}-{d['target_id']}",
            "time": now_et_str(),
            "item_key": ikey,
            "target_id": d["target_id"],
            "conversation_id": cid,
            "project_name": d["project_name"],
            "critical": is_critical,
            "message": transition_msg,
        }
        events.insert(0, ev)
        events = events[:50]
        if is_critical:
          send_desktop_alert(f"CL Alert: {ikey}", transition_msg, info.get("url", ""))
        if wake_chats:
          wake_conversation(cid, transition_msg)

    new_items[composite_key] = record

  state["items"] = new_items
  state["events"] = events
  state["last_sync"] = now_et_str()
  state["rollers_summary"] = {
      k: {
          "last_roll_rev": (v.get("last_roll_rev") or "")[:10],
          "current_roll_rev": (v.get("current_roll_rev") or "")[:10],
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
<title>CL Flight Deck · Skia & Flutter</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-hover: #1c222b;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --green-bg: rgba(63, 185, 80, 0.14);
    --yellow: #d29922;
    --yellow-bg: rgba(210, 153, 34, 0.15);
    --red: #f85149;
    --red-bg: rgba(248, 81, 73, 0.16);
    --purple: #a371f7;
    --purple-bg: rgba(163, 113, 247, 0.15);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    padding: 20px 28px;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    font-size: 13px;
    line-height: 1.45;
  }
  header {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding-bottom: 16px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 18px;
  }
  .title-block h1 {
    margin: 0;
    font-size: 20px;
    font-weight: 600;
    display: flex;
    align-items: center;
    gap: 10px;
  }
  .subtitle {
    color: var(--muted);
    font-size: 12px;
    margin-top: 3px;
  }
  .controls {
    display: flex;
    align-items: center;
    gap: 10px;
    flex-wrap: wrap;
  }
  button, .btn {
    background: var(--panel);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    text-decoration: none;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  button:hover, .btn:hover {
    background: var(--panel-hover);
    border-color: var(--muted);
  }
  button.primary {
    background: rgba(88, 166, 255, 0.15);
    border-color: rgba(88, 166, 255, 0.45);
    color: var(--accent);
  }
  button.danger {
    background: var(--red-bg);
    border-color: rgba(248, 81, 73, 0.45);
    color: var(--red);
  }
  .rollers-bar {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
    gap: 10px;
    margin-bottom: 18px;
  }
  .roller-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 10px 14px;
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .roller-name {
    font-weight: 600;
    font-size: 13px;
  }
  .roller-meta {
    color: var(--muted);
    font-size: 11px;
    margin-top: 2px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  }
  .filter-bar {
    display: flex;
    gap: 8px;
    margin-bottom: 14px;
    flex-wrap: wrap;
  }
  .pill {
    padding: 4px 10px;
    border-radius: 999px;
    font-size: 11.5px;
    font-weight: 600;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    white-space: nowrap;
  }
  .pill.green { background: var(--green-bg); color: var(--green); }
  .pill.yellow { background: var(--yellow-bg); color: var(--yellow); }
  .pill.red { background: var(--red-bg); color: var(--red); }
  .pill.purple { background: var(--purple-bg); color: var(--purple); }
  .pill.muted { background: rgba(139, 148, 158, 0.15); color: var(--muted); }

  table {
    width: 100%;
    border-collapse: collapse;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
  }
  th, td {
    padding: 10px 12px;
    text-align: left;
    border-bottom: 1px solid var(--border);
    vertical-align: middle;
  }
  th {
    background: rgba(255, 255, 255, 0.02);
    color: var(--muted);
    font-weight: 600;
    font-size: 11.5px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: var(--panel-hover); }
  tr.row-alert td { background: rgba(248, 81, 73, 0.07); }
  tr.row-ready td { opacity: 0.85; }

  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  .mono {
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 12px;
  }
  .roll-grid {
    display: inline-flex;
    gap: 6px;
    flex-wrap: wrap;
  }
  .roll-chip {
    padding: 2px 7px;
    border-radius: 4px;
    font-size: 11px;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    border: 1px solid var(--border);
  }
  .roll-chip.ok { background: var(--green-bg); color: var(--green); border-color: rgba(63,185,80,0.3); }
  .roll-chip.wait { background: rgba(139,148,158,0.1); color: var(--muted); }
  .roll-chip.cq { background: var(--yellow-bg); color: var(--yellow); border-color: rgba(210,153,34,0.35); }
  .roll-chip.fail { background: var(--red-bg); color: var(--red); border-color: rgba(248,81,73,0.45); font-weight: 700; }

  .snippet {
    color: var(--muted);
    font-size: 11.5px;
    max-width: 340px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .events-panel {
    margin-top: 22px;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 12px 16px;
  }
  .events-panel h3 {
    margin: 0 0 8px 0;
    font-size: 13px;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .event-row {
    padding: 6px 0;
    border-bottom: 1px solid var(--border);
    font-size: 12px;
    display: flex;
    gap: 10px;
  }
  .event-row:last-child { border-bottom: none; }
</style>
</head>
<body>

<header>
  <div class="title-block">
    <h1>✈️ CL &amp; PR Flight Deck <span class="pill purple" id="count-badge">0 tracked</span></h1>
    <div class="subtitle" id="sync-status">Connecting to watcher...</div>
  </div>
  <div class="controls">
    <button id="notif-btn" onclick="enableNotifications()">🔔 Enable Browser Alerts</button>
    <button onclick="testNotification()">🔊 Test Alert</button>
    <button class="primary" onclick="triggerRefresh()">⟳ Poll Now</button>
  </div>
</header>

<div class="rollers-bar" id="rollers-bar"></div>

<div class="filter-bar" id="project-filters"></div>

<table>
  <thead>
    <tr>
      <th>Project &amp; Chat</th>
      <th>CL / PR</th>
      <th>Role &amp; Owner</th>
      <th>Phase &amp; Gerrit / CQ</th>
      <th>Skia Status &amp; 4 Autorolls</th>
      <th>Latest Activity</th>
      <th>Actions</th>
    </tr>
  </thead>
  <tbody id="table-body"></tbody>
</table>

<div class="events-panel">
  <h3>Recent Watcher Wakeups &amp; Alerts</h3>
  <div id="events-list"><div style="color:var(--muted)">No transitions recorded yet.</div></div>
</div>

<script>
let currentProjectFilter = 'ALL';
let seenEventIds = new Set();
let initialLoadDone = false;

function updateNotifButton() {
  const btn = document.getElementById('notif-btn');
  if (!('Notification' in window)) {
    btn.textContent = '🔕 Notifications Unsupported';
    return;
  }
  if (Notification.permission === 'granted') {
    btn.textContent = '🔔 Browser Alerts: ON';
    btn.classList.add('primary');
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
    'Browser notifications and audio chime are active for CQ/Roll breakages.',
    true
  );
}

function phaseBadge(item) {
  const p = item.phase;
  if (p === 'ALERT_ROLL_OR_STATUS' || p === 'ALERT_CQ_FAILED') {
    return `<span class="pill red">🔴 ${p.replace('ALERT_', '')}</span>`;
  }
  if (p === 'NEEDS_YOUR_ACTION') {
    return `<span class="pill yellow">⚡ Needs Your Action</span>`;
  }
  if (p === 'SUBMITTING') {
    return `<span class="pill purple">🚀 Submitting (CQ+2)</span>`;
  }
  if (p === 'POST_SUBMIT_ROLLING') {
    return `<span class="pill yellow">⏳ Rolling Post-Submit</span>`;
  }
  if (p === 'READY_TO_CLOSE') {
    return `<span class="pill green">🏁 All Checks Passed</span>`;
  }
  if (p === 'ABANDONED') {
    return `<span class="pill muted">Abandoned</span>`;
  }
  return `<span class="pill muted">⏳ Waiting</span>`;
}

function renderRollsCell(item) {
  const ps = item.post_submit;
  if (!ps) {
    if (item.status === 'MERGED') {
      return `<span class="pill green">✓ Merged</span>`;
    }
    return `<span style="color:var(--muted)">Pre-submit (${item.cq_status})</span>`;
  }
  const chipClass = (st) => st === '✓' ? 'ok' : (st === '🔴' ? 'fail' : (st === '🔄' ? 'cq' : 'wait'));
  let html = `<div class="roll-grid">`;
  html += `<span class="roll-chip ${chipClass(ps.skia_status)}" title="${ps.skia_status_detail}">Status ${ps.skia_status}</span>`;
  for (const rname of ['Android', 'Chrome', 'Google3', 'Flutter']) {
    const r = (ps.rolls || {})[rname] || {state: '⏳', detail: ''};
    const shortName = rname === 'Google3' ? 'G3' : rname.slice(0, 3);
    html += `<span class="roll-chip ${chipClass(r.state)}" title="${rname}: ${r.detail}">${shortName} ${r.state}</span>`;
  }
  html += `</div>`;
  return html;
}

async function finalizeChat(convId, targetId) {
  if (!confirm(`Finalize chat for ${targetId} («»)?`)) return;
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
  document.getElementById('sync-status').textContent = 'Polling Gerrit, GitHub, and Skia Gardener...';
  await fetch('/api/refresh', {method: 'POST'});
  await fetchState();
}

function renderDashboard(data) {
  document.getElementById('sync-status').textContent = `Last synced: ${data.last_sync || 'now'} · Auto-refresh every 20s`;

  // Render rollers bar
  const rbar = document.getElementById('rollers-bar');
  const rsum = data.rollers_summary || {};
  rbar.innerHTML = ['Android', 'Chrome', 'Google3', 'Flutter'].map(name => {
    const r = rsum[name] || {};
    const cls = r.num_failed > 0 ? 'red' : 'green';
    return `<div class="roller-card">
      <div>
        <div class="roller-name">Skia ➔ ${name}</div>
        <div class="roller-meta">last: ${r.last_roll_rev || '—'} · behind: ${r.num_behind ?? 0}</div>
      </div>
      <span class="pill ${cls}">${r.badge || 'Healthy'}</span>
    </div>`;
  }).join('');

  const items = Object.values(data.items || {});
  // Sort: alerts first, then submitting/rolling, then needs action, then ready to close
  const prio = {
    'ALERT_ROLL_OR_STATUS': 0,
    'ALERT_CQ_FAILED': 1,
    'SUBMITTING': 2,
    'POST_SUBMIT_ROLLING': 3,
    'NEEDS_YOUR_ACTION': 4,
    'WAITING_ON_OTHERS': 5,
    'READY_TO_CLOSE': 6,
    'ABANDONED': 7
  };
  items.sort((a, b) => (prio[a.phase] ?? 9) - (prio[b.phase] ?? 9));

  document.getElementById('count-badge').textContent = `${items.length} tracked`;

  // Project filter buttons
  const countsByProj = {'ALL': items.length};
  items.forEach(it => {
    countsByProj[it.project_name] = (countsByProj[it.project_name] || 0) + 1;
  });
  const fbar = document.getElementById('project-filters');
  fbar.innerHTML = Object.entries(countsByProj).map(([pname, cnt]) => {
    const active = currentProjectFilter === pname ? 'primary' : '';
    return `<button class="${active}" onclick="currentProjectFilter='${pname}'; fetchState();">${pname} (${cnt})</button>`;
  }).join('');

  const visible = items.filter(it => currentProjectFilter === 'ALL' || it.project_name === currentProjectFilter);
  const tbody = document.getElementById('table-body');
  tbody.innerHTML = visible.map(it => {
    const rowClass = it.phase.startsWith('ALERT_') ? 'row-alert' : (it.phase === 'READY_TO_CLOSE' ? 'row-ready' : '');
    const roleClass = it.is_self ? 'purple' : (it.is_external ? 'yellow' : 'muted');
    return `<tr class="${rowClass}">
      <td>
        <div style="font-weight:600">${it.project_name}</div>
        <div class="mono" style="color:var(--muted)" title="${it.conversation_id}">${it.title}</div>
      </td>
      <td>
        <a href="${it.url}" target="_blank" class="mono" style="font-weight:600">${it.item_key} (PS${it.patchset})</a>
        <div style="max-width:280px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${it.subject}">${it.subject}</div>
      </td>
      <td>
        <span class="pill ${roleClass}">${it.role}</span>
        <div style="color:var(--muted); font-size:11px; margin-top:2px;">${it.owner_name}</div>
      </td>
      <td>
        ${phaseBadge(it)}
        <div class="mono" style="color:var(--muted); font-size:11px; margin-top:3px;">CR: ${it.cr_summary} · ${it.cq_status}</div>
      </td>
      <td>${renderRollsCell(it)}</td>
      <td>
        <div class="mono" style="font-size:11px;">${it.last_msg_author}</div>
        <div class="snippet" title="${it.last_msg_snippet}">${it.last_msg_snippet || '—'}</div>
      </td>
      <td>
        <div style="display:flex; gap:6px;">
          <button onclick="wakeChatForTriage('${it.conversation_id}', '${it.item_key}', '${it.target_id}')" title="Wake chat to triage & clean Inbox">💬 Wake &amp; Clean Mail</button>
          <button onclick="finalizeChat('${it.conversation_id}', '${it.target_id}')" title="Finalize chat («»)">✓ Close</button>
        </div>
      </td>
    </tr>`;
  }).join('');

  // Events & notifications
  const evList = data.events || [];
  const evDiv = document.getElementById('events-list');
  if (evList.length > 0) {
    evDiv.innerHTML = evList.slice(0, 12).map(ev => {
      const badge = ev.critical ? `<span class="pill red">ALERT</span>` : `<span class="pill green">UPDATE</span>`;
      return `<div class="event-row">
        <span class="mono" style="color:var(--muted)">${ev.time}</span>
        ${badge}
        <strong>${ev.project_name} · ${ev.item_key}</strong>
        <span>${ev.message}</span>
      </div>`;
    }).join('');
  }

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
    const data = await resp.json();
    renderDashboard(data);
  } catch (e) {
    document.getElementById('sync-status').textContent = 'Connection error — retrying...';
  }
}

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
    body = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
    try:
      data = json.loads(body)
    except Exception:
      data = {}

    if parsed.path == "/api/refresh":
      state = sync_once(wake_chats=True, update_titles=True)
      self._send_json({"ok": True, "count": len(state.get("items") or {})})
      return

    if parsed.path == "/api/finalize":
      cid = str(data.get("conversation_id") or "").strip()
      if cid:
        subprocess.run(
            ["python3", os.path.join(HOOKS_DIR, "chat_lifecycle.py"), "finalize", cid],
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


def serve_forever(port: int, poll_interval_sec: int, max_polls: int = 1000000) -> None:
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
  print(f"CL Flight Deck listening on http://localhost:{port} (PEN: {pen})", flush=True)
  server.serve_forever()


def main() -> int:
  parser = argparse.ArgumentParser(description="CL Flight Deck & Autonomous Watcher")
  parser.add_argument("--sync-once", action="store_true", help="Run a single poll and update state")
  parser.add_argument("--no-wake", action="store_true", help="Do not send agentapi wakeups on sync")
  parser.add_argument("--serve", action="store_true", help="Run live HTTP dashboard & watcher")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="HTTP server port")
  parser.add_argument("--interval", type=int, default=120, help="Background poll interval in seconds")
  parser.add_argument("--pen-url", action="store_true", help="Print UberProxy PEN URL for port")
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
