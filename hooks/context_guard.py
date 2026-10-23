#!/usr/bin/env python3
"""PreInvocation hook for Jetski to monitor conversation context size.

The metric is the Language Server's own estimate of the context window usage
(estimatedTokensUsed / maxContextTokens of the last model call). Levels:
1. Soft (SOFT_PCT): a throttled reminder to hand off at the nearest
   micro-boundary when further autonomous work remains, or finish the answer
   in-place if returning control to the user.
2. Turn-start / Hard (TURN_START_PCT at invocationNum == 0, HARD_PCT mid-turn,
   or history already compacted): at turn start the agent immediately hands off
   the user's new request before answering so the response lands in the new
   chat (enforced by stop_guard); mid-turn it hands off only if continuing
   autonomous work, never after answering the user.
3. Mid-turn circuit breaker: a single turn with too many tool-call iterations
   (`MAX_TURN_INVOCATIONS`) hands off as well.
Every invocation appends one JSONL record to the guard log so silent misses
are visible afterwards.
"""

from datetime import datetime, timezone
import glob
import json
import os
import re
import sqlite3
import sys
import time
from urllib.parse import unquote
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("JETSKI_TIMEZONE", "America/New_York")

# Percent of the model's context window
SOFT_PCT = float(os.environ.get("JETSKI_CONTEXT_SOFT_PCT", "60"))
TURN_START_PCT = float(os.environ.get("JETSKI_CONTEXT_TURN_START_PCT", "65"))
HARD_PCT = float(os.environ.get("JETSKI_CONTEXT_HARD_PCT", "80"))
RPC_TIMEOUT_S = 1.5
# Fallback when the RPC fails: real transcripts average ~2 bytes per token
# (a 360 KB transcript_full.jsonl was 176k tokens), not the ~4 often assumed
FALLBACK_BYTES_PER_TOKEN = 2.0
FALLBACK_MAX_TOKENS = 256000
# initialNumSteps grows by ~2 per invocation, so this re-reminds about every
# 10 invocations without persisting a counter on every call
SOFT_REMIND_EVERY_STEPS = 20
# Maximum tool-call iterations within a single turn before circuit-breaking
# (set high so normal 25-step debugging turns in fresh chats do not hand off early):
MAX_TURN_INVOCATIONS = int(os.environ.get("JETSKI_MAX_TURN_INVOCATIONS", "80"))
# Lets the agent finish the handoff tool calls without re-tripping the breaker
SNOOZE_TURN_INVOCATIONS = 40

DEFAULT_LOG_PATH = "~/.gemini/config/logs/context_guard.jsonl"
LOG_MAX_BYTES = 1_000_000
LIFECYCLE_CLI = "~/.gemini/config/hooks/chat_lifecycle.py"
ROADMAP_CLI = "~/.gemini/config/hooks/roadmap.py"

# App data dirs of the supported clients, in lookup order. Antigravity keeps
# conversation_summaries.db (with project ids) in ~/.gemini/antigravity; some
# Jetski builds use ~/.gemini/jetski (standalone macOS Jetski has no DB at all).
APP_DATA_DIR_CANDIDATES = ("~/.gemini/antigravity", "~/.gemini/jetski")
SUMMARY_DB_NAME = "conversation_summaries.db"
# Allow tests to isolate project metadata without touching ~/.gemini/config/projects
PROJECTS_DIR_ENV = "JETSKI_PROJECTS_DIR"
PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")


def projects_root() -> str:
  # Read JETSKI_PROJECTS_DIR dynamically so unittest.mock.patch.dict works per test
  return os.path.abspath(
      os.path.expanduser(os.environ.get(PROJECTS_DIR_ENV) or PROJECTS_DIR)
  )

# Regexes to strip previous continuation prefixes/suffixes on chained handoffs:
CONT_PREFIX_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\s+(?:продолжение|continue))?\]\s*|(?:продолжение|continue)(?:\s+\d{2}:\d{2})?\s*:\s*)+",
    re.IGNORECASE,
)
CONT_SUFFIX_RE = re.compile(
    r"\s*\((?:продолжение|continue)\s+(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}\)\s*$",
    re.IGNORECASE,
)


def get_transcript_size_kb(transcript_path: str) -> float:
  if not transcript_path:
    return 0.0
  log_dir = os.path.dirname(transcript_path)
  full_path = os.path.join(log_dir, "transcript_full.jsonl")
  for path in (full_path, transcript_path):
    try:
      if os.path.exists(path):
        return os.path.getsize(path) / 1024.0
    except OSError:
      pass
  return 0.0


def app_data_dir_from_path(path: str) -> str:
  """Returns <appDataDir> for a path like <appDataDir>/brain/<conv-id>/..., or ""."""
  if not path:
    return ""
  parts = os.path.abspath(os.path.expanduser(path)).split(os.sep)
  for i in range(len(parts) - 2, 0, -1):
    if parts[i] == "brain":
      return os.sep.join(parts[:i]) or os.sep
  return ""


def summary_db_paths(hint_path: str = "") -> list[str]:
  """Existing conversation_summaries.db paths, most likely first (deduplicated)."""
  dirs = [app_data_dir_from_path(hint_path)] + [
      os.path.expanduser(d) for d in APP_DATA_DIR_CANDIDATES
  ]
  paths: list[str] = []
  for d in dirs:
    if not d:
      continue
    p = os.path.join(os.path.abspath(d), SUMMARY_DB_NAME)
    if p not in paths and os.path.isfile(p):
      paths.append(p)
  return paths


def _query_summary_db(db_path: str, conv_id: str):
  """Returns the (title, project_id) row or None.

  `mode=ro` cannot open a WAL database whose -shm file is absent (e.g. while
  the client is not holding it open), so fall back to `immutable=1`, which may
  miss the newest un-checkpointed rows but never writes.
  """
  query = (
      "SELECT title, project_id FROM conversation_summaries WHERE"
      " conversation_id = ?"
  )
  for flags in ("mode=ro", "mode=ro&immutable=1"):
    try:
      conn = sqlite3.connect(f"file:{db_path}?{flags}", uri=True, timeout=1.0)
      try:
        return conn.execute(query, (conv_id,)).fetchone()
      finally:
        conn.close()
    except Exception:
      continue
  return None


def get_conversation_db_info(
    conv_id: str, hint_path: str = ""
) -> tuple[str, str]:
  """Returns (title, project_id) from conversation_summaries.db.

  `hint_path` (transcript or artifact path) selects the current client's app
  data dir first; otherwise the known client dirs are searched in order.
  """
  if not conv_id:
    return ("", "")
  for db_path in summary_db_paths(hint_path):
    row = _query_summary_db(db_path, conv_id)
    if row:
      title = str(row[0]).strip() if row[0] else ""
      project_id = str(row[1]).strip() if row[1] else ""
      return (title, project_id)
  return ("", "")


def _file_uri_to_path(uri: str) -> str:
  if not uri.startswith("file://"):
    return ""
  return unquote(uri[len("file://") :]).rstrip("/")


def infer_project_id_from_workspaces(workspace_paths: list[str]) -> str:
  """Infers a project_id strictly from the conversation's active workspace_paths.

  Considers both `gitFolder.folderUri` and plain `folderUri` resources; the
  most specific (longest) matching folder wins, so a workspace inside
  ~/Sources/skia maps to the skia project rather than a ~/Sources project.
  """
  # Resolve project directory via projects_root() to honor test overrides
  pdir = projects_root()
  if not workspace_paths or not os.path.isdir(pdir):
    return ""
  folder_to_project: list[tuple[str, str]] = []
  # Scan project JSON files in deterministic order
  for pfile in sorted(glob.glob(os.path.join(pdir, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = pdata.get("id", "")
      if not pid:
        continue
      resources = pdata.get("projectResources", {}).get("resources", [])
      for r in resources:
        for uri in (
            (r.get("gitFolder") or {}).get("folderUri", ""),
            r.get("folderUri", ""),
        ):
          folder_path = _file_uri_to_path(uri)
          if folder_path:
            folder_to_project.append((folder_path, pid))
    except Exception:
      pass

  best_len, best_pid = -1, ""
  for wp in workspace_paths:
    wp = wp.rstrip("/")
    for folder_path, pid in folder_to_project:
      if wp == folder_path or wp.startswith(folder_path + "/"):
        if len(folder_path) > best_len:
          best_len, best_pid = len(folder_path), pid
  return best_pid


# Match canonical workspace roots in scratch, ~/Sources, or CitC to detect project ownership
WORKSPACE_ROOT_RE = re.compile(
    r"(/usr/local/google/home/[^/\s`\"'),;]+/(?:\.gemini/jetski/scratch|Sources)/[A-Za-z0-9_.\-]+"
    r"|/google/src/cloud/[^/\s`\"'),;]+/[A-Za-z0-9_.\-]+)"
)
WORKING_DIR_LINE_RE = re.compile(
    r"(?:Working directory|Repository|Repo|Workspace):\s*`?(/[^\s`\"'),;]+)[\`\"'),;]*",
    re.IGNORECASE,
)


def _normalize_workspace_root(path: str, allow_any_dir: bool = False) -> str:
  if not path:
    return ""
  clean = os.path.abspath(os.path.expanduser(path.strip())).rstrip("/")
  m = WORKSPACE_ROOT_RE.match(clean)
  if m:
    return m.group(1).rstrip("/")
  return clean if allow_any_dir else ""


def get_active_chats_by_project(
    hint_path: str = "", exclude_conv_id: str = ""
) -> dict[str, tuple[str, str]]:
  """Returns {project_id: (conv_id, title)} for the newest active chat per project."""
  import roadmap

  r_root = roadmap.roadmaps_root()
  candidates: dict[str, list[tuple[float, int, str, str]]] = {}
  query = (
      "SELECT conversation_id, title, project_id, last_modified_time "
      "FROM conversation_summaries "
      "WHERE parent_conversation_id = '' AND (title LIKE '%⦿%' OR title LIKE '%▸%')"
  )
  for db_path in summary_db_paths(hint_path):
    for flags in ("mode=ro", "mode=ro&immutable=1"):
      try:
        conn = sqlite3.connect(
            f"file:{db_path}?{flags}", uri=True, timeout=1.0
        )
        try:
          rows = conn.execute(query).fetchall()
        finally:
          conn.close()
        for cid, title, pid, lmt in rows:
          cid_s = str(cid or "").strip()
          title_s = str(title or "").strip()
          pid_s = str(pid or "").strip()
          if (
              not cid_s
              or not pid_s
              or pid_s == "outside-of-project"
              or cid_s == exclude_conv_id
          ):
            continue
          is_tip = 1 if "⦿" in title_s else 0
          ts = 0.0
          if lmt and not str(lmt).startswith("0001-"):
            try:
              ts = datetime.fromisoformat(
                  str(lmt).replace("Z", "+00:00")
              ).timestamp()
            except Exception:
              ts = 0.0
          st_file = os.path.join(r_root, pid_s, "state", f"{cid_s}.md")
          try:
            if os.path.isfile(st_file):
              ts = max(ts, os.path.getmtime(st_file))
          except OSError:
            pass
          candidates.setdefault(pid_s, []).append((is_tip, ts, cid_s, title_s))
        break
      except Exception:
        continue

  result: dict[str, tuple[str, str]] = {}
  for pid_s, items in candidates.items():
    items.sort(key=lambda x: (x[0], x[1]), reverse=True)
    result[pid_s] = (items[0][2], items[0][3])

  # Fall back to roadmaps/<pid>/state/*.md when SQLite has no row (e.g. in tests)
  if os.path.isdir(r_root):
    for entry in sorted(os.listdir(r_root)):
      if entry in result:
        continue
      sdir = os.path.join(r_root, entry, "state")
      if not os.path.isdir(sdir):
        continue
      best_mtime, best_cid, best_title = -1.0, "", ""
      for sf in glob.glob(os.path.join(sdir, "*.md")):
        cid_s = os.path.splitext(os.path.basename(sf))[0]
        if cid_s == exclude_conv_id:
          continue
        try:
          mt = os.path.getmtime(sf)
          if mt > best_mtime:
            with open(sf, "r", encoding="utf-8", errors="ignore") as f:
              first_line = f.readline().strip().lstrip("#").strip()
            best_mtime = mt
            best_cid = cid_s
            best_title = first_line or f"⦿ {cid_s[:8]}"
        except OSError:
          continue
      if best_cid:
        result[entry] = (best_cid, best_title)
  return result


def collect_project_directory(
    hint_path: str = "",
    include_active_chats: bool = False,
    exclude_conv_id: str = "",
) -> dict:
  """Maps projects to their workspace roots, active chat, and exclusive roots."""
  import roadmap

  pdir = projects_root()
  r_root = roadmap.roadmaps_root()
  active_by_proj = (
      get_active_chats_by_project(hint_path, exclude_conv_id=exclude_conv_id)
      if include_active_chats
      else {}
  )
  projects: dict[str, dict] = {}
  root_owners: dict[str, list[str]] = {}

  if os.path.isdir(pdir):
    for pfile in sorted(glob.glob(os.path.join(pdir, "*.json"))):
      try:
        with open(pfile, "r", encoding="utf-8") as f:
          pdata = json.load(f)
      except Exception:
        continue
      pid = str(pdata.get("id") or "").strip()
      if not pid:
        continue
      name = str(pdata.get("name") or pid).strip()
      roots: set[str] = set()
      resources = pdata.get("projectResources", {}).get("resources", [])
      for r in resources:
        for uri in (
            (r.get("gitFolder") or {}).get("folderUri", ""),
            r.get("folderUri", ""),
        ):
          fp = _file_uri_to_path(uri)
          norm = _normalize_workspace_root(fp, allow_any_dir=True)
          if norm:
            roots.add(norm)

      sdir = os.path.join(r_root, pid, "state")
      if os.path.isdir(sdir):
        for sf in glob.glob(os.path.join(sdir, "*.md")):
          try:
            with open(sf, "r", encoding="utf-8", errors="ignore") as f:
              txt = f.read()
          except OSError:
            continue
          snap_idx = txt.find("## Automatic State Snapshot")
          body = txt[:snap_idx] if snap_idx >= 0 else txt
          for m in WORKING_DIR_LINE_RE.findall(body):
            norm = _normalize_workspace_root(m, allow_any_dir=True)
            if norm:
              roots.add(norm)
          for m in re.findall(r"file://(/[^\s`\"')#]+)", body):
            norm = _normalize_workspace_root(m, allow_any_dir=False)
            if norm:
              roots.add(norm)
          if snap_idx >= 0:
            snap = txt[snap_idx:]
            for sec_hdr in (
                "### Files Modified by Tools",
                "### Git Status in",
                "### Recent Commands Executed",
            ):
              sec_i = snap.find(sec_hdr)
              if sec_i >= 0:
                sec_end = snap.find("\n### ", sec_i + 4)
                sec_txt = snap[sec_i : sec_end if sec_end >= 0 else len(snap)]
                if sec_hdr == "### Git Status in":
                  for gm in re.findall(r"### Git Status in `([^`]+)`", sec_txt):
                    norm = _normalize_workspace_root(gm, allow_any_dir=True)
                    if norm:
                      roots.add(norm)
                for wm in WORKSPACE_ROOT_RE.findall(sec_txt):
                  roots.add(wm.rstrip("/"))

      sorted_roots = sorted(roots)
      for rt in sorted_roots:
        root_owners.setdefault(rt, []).append(pid)
      projects[pid] = {
          "id": pid,
          "name": name,
          "roots": sorted_roots,
          "active_chat": active_by_proj.get(pid),
      }

  exclusive_roots = {
      rt: owners[0] for rt, owners in root_owners.items() if len(owners) == 1
  }
  return {"projects": projects, "exclusive_roots": exclusive_roots}


# Limit intra-project sibling radar to chats active within the last 2 hours or holding doing items
SIBLING_ACTIVE_WINDOW_S = int(
    os.environ.get("JETSKI_SIBLING_ACTIVE_WINDOW_S", "7200")
)


def get_active_sibling_chats_in_project(
    project_id: str, exclude_conv_id: str = "", hint_path: str = ""
) -> list[dict]:
  """Returns active sibling chats in project_id (owning `doing` items or active within 2h)."""
  if not project_id or project_id == "outside-of-project":
    return []
  import roadmap

  r_root = roadmap.roadmaps_root()
  now = time.time()
  doing_by_owner: dict[str, list[str]] = {}
  try:
    doc = roadmap.load(project_id)
    for it in doc.get("items", []):
      if it.get("status") == "doing" and it.get("owner"):
        doing_by_owner.setdefault(it["owner"], []).append(it["id"])
  except Exception:
    pass

  by_cid: dict[str, dict] = {}
  query = (
      "SELECT conversation_id, title, last_modified_time "
      "FROM conversation_summaries "
      "WHERE project_id = ? AND parent_conversation_id = '' "
      "AND (title LIKE '%⦿%' OR title LIKE '%▸%')"
  )
  for db_path in summary_db_paths(hint_path):
    for flags in ("mode=ro", "mode=ro&immutable=1"):
      try:
        conn = sqlite3.connect(
            f"file:{db_path}?{flags}", uri=True, timeout=1.0
        )
        try:
          rows = conn.execute(query, (project_id,)).fetchall()
        finally:
          conn.close()
        for cid, title, lmt in rows:
          cid_s = str(cid or "").strip()
          title_s = str(title or "").strip()
          if not cid_s or cid_s == exclude_conv_id:
            continue
          ts = 0.0
          if lmt and not str(lmt).startswith("0001-"):
            try:
              ts = datetime.fromisoformat(
                  str(lmt).replace("Z", "+00:00")
              ).timestamp()
            except Exception:
              ts = 0.0
          by_cid[cid_s] = {"cid": cid_s, "title": title_s, "ts": ts, "files": []}
        break
      except Exception:
        continue

  sdir = os.path.join(r_root, project_id, "state")
  if os.path.isdir(sdir):
    for sf in sorted(glob.glob(os.path.join(sdir, "*.md"))):
      cid_s = os.path.splitext(os.path.basename(sf))[0]
      if not cid_s or cid_s == exclude_conv_id:
        continue
      try:
        mt = os.path.getmtime(sf)
        with open(sf, "r", encoding="utf-8", errors="ignore") as f:
          txt = f.read()
      except OSError:
        continue
      first_line = txt.splitlines()[0].lstrip("#").strip() if txt else ""
      rec = by_cid.setdefault(
          cid_s,
          {
              "cid": cid_s,
              "title": first_line or f"⦿ {cid_s[:8]}",
              "ts": mt,
              "files": [],
          },
      )
      rec["ts"] = max(rec["ts"], mt)
      if not rec["title"] and first_line:
        rec["title"] = first_line
      for m in WORKING_DIR_LINE_RE.findall(txt):
        norm = _normalize_workspace_root(m, allow_any_dir=True)
        if norm and norm not in rec["files"]:
          rec["files"].append(norm)
      for m in re.findall(r"file://(/[^\s`\"')#]+)", txt):
        base_f = os.path.basename(m.rstrip("/"))
        if base_f and base_f not in rec["files"]:
          rec["files"].append(base_f)

  for owner_cid in doing_by_owner:
    if owner_cid != exclude_conv_id and owner_cid not in by_cid:
      t_db, _ = get_conversation_db_info(owner_cid, hint_path)
      by_cid[owner_cid] = {
          "cid": owner_cid,
          "title": t_db or f"⦿ {owner_cid[:8]}",
          "ts": now,
          "files": [],
      }

  active_list: list[dict] = []
  for cid_s, rec in by_cid.items():
    doing_items = doing_by_owner.get(cid_s, [])
    age_s = max(0.0, now - rec["ts"]) if rec["ts"] > 0 else float("inf")
    if doing_items or age_s <= SIBLING_ACTIVE_WINDOW_S:
      rec["doing"] = doing_items
      rec["age_min"] = int(age_s // 60) if rec["ts"] > 0 else -1
      active_list.append(rec)

  active_list.sort(
      key=lambda r: (1 if r["doing"] else 0, r["ts"]), reverse=True
  )
  return active_list[:6]


def build_git_sync_notice(conv_id: str, workspace_paths: list[str]) -> str:
  """Notifies the chat if its earlier local commits were pushed/cherry-picked by a sibling chat."""
  if not conv_id:
    return ""
  import subprocess

  cand_dirs = [
      os.path.abspath(os.path.expanduser(w)) for w in (workspace_paths or []) if w
  ]
  if not os.environ.get(PROJECTS_DIR_ENV):
    cfg_dir = os.path.abspath(os.path.expanduser("~/.gemini/config"))
    if cfg_dir not in cand_dirs and os.path.isdir(cfg_dir):
      cand_dirs.append(cfg_dir)

  seen_common: set[str] = set()
  notices: list[str] = []
  for d in cand_dirs:
    if not os.path.isdir(d):
      continue
    try:
      res = subprocess.run(
          ["git", "-C", d, "rev-parse", "--show-toplevel", "--git-common-dir"],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      if res.returncode != 0:
        continue
      lines = [ln.strip() for ln in res.stdout.splitlines() if ln.strip()]
      if len(lines) < 2:
        continue
      top_level = os.path.realpath(lines[0])
      common_dir = os.path.realpath(
          lines[1] if os.path.isabs(lines[1]) else os.path.join(d, lines[1])
      )
      if common_dir in seen_common:
        continue
      seen_common.add(common_dir)
      reg_path = os.path.join(common_dir, "jetski_commits.json")
      if not os.path.isfile(reg_path):
        continue
      with open(reg_path, "r", encoding="utf-8") as f:
        reg = json.load(f)
      commits = reg.get("commits") or {}
      mine = [
          (sha, info)
          for sha, info in commits.items()
          if isinstance(info, dict)
          and info.get("conv_id") == conv_id
          and not info.get("pushed_by_owner")
          and not info.get("notified_pushed")
      ]
      if not mine:
        continue
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
      log_res = subprocess.run(
          ["git", "-C", top_level, "log", upstream, "-n", "30", "--format=%H%x00%s"],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      if log_res.returncode != 0:
        continue
      upstream_by_sha: dict[str, str] = {}
      upstream_by_subj: dict[str, str] = {}
      for ln in log_res.stdout.splitlines():
        if "\x00" in ln:
          h, s = ln.split("\x00", 1)
          upstream_by_sha[h.strip()] = s.strip()
          if s.strip():
            upstream_by_subj.setdefault(s.strip(), h.strip())
      changed = False
      for sha, info in mine:
        subj = str(info.get("subject") or "").strip()
        remote_sha = ""
        if sha in upstream_by_sha:
          remote_sha = sha
        elif subj and subj in upstream_by_subj:
          remote_sha = upstream_by_subj[subj]
        if remote_sha:
          info["notified_pushed"] = True
          info["remote_sha"] = remote_sha
          changed = True
          via = (
              f"as `{remote_sha[:7]}` (rebased/cherry-picked)"
              if remote_sha != sha
              else f"(`{sha[:7]}`)"
          )
          notices.append(
              f'- Commit `{sha[:7]}` ("{subj}") in `{top_level}` was already '
              f"pushed to `{upstream}` {via} by another conversation."
          )
      if changed:
        tmp = f"{reg_path}.tmp.{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
          json.dump(reg, f, ensure_ascii=False, indent=2)
        os.replace(tmp, reg_path)
    except Exception:
      continue

  if not notices:
    return ""
  return (
      "[GIT SYNC NOTICE]\n"
      "While this conversation was waiting, another conversation pushed your "
      "earlier local commit(s):\n"
      + "\n".join(notices)
      + "\nDo not re-push or re-cherry-pick them; check `git status` and "
      "`git log origin/main -n 5` before running further git commands."
  )


def build_scope_radar_notice(
    conv_id: str,
    transcript_path: str,
    workspace_paths: list[str],
    last_user_input: str,
) -> str:
  """Turn-start scope notice listing current and sibling projects/workspaces."""
  if not last_user_input or re.search(
      r"Continuing unfinished work from previous conversation", last_user_input
  ):
    return ""
  directory = collect_project_directory(
      transcript_path, include_active_chats=True, exclude_conv_id=conv_id
  )
  # Emit radar when either sibling chats exist in this project or multiple projects exist
  projects = directory["projects"]

  cur_title, cur_pid = get_conversation_db_info(conv_id, transcript_path)
  if not cur_pid or cur_pid == "outside-of-project":
    cur_pid = infer_project_id_from_workspaces(workspace_paths)
  cur_proj = projects.get(cur_pid) if cur_pid else None
  cur_proj_name = cur_proj["name"] if cur_proj else (cur_pid or "outside-of-project")
  cur_roots = (
      ", ".join(f"`{r}`" for r in cur_proj["roots"])
      if cur_proj and cur_proj["roots"]
      else (
          ", ".join(f"`{w}`" for w in workspace_paths)
          if workspace_paths
          else "none"
      )
  )

  # Collect active sibling chats within the current project to prevent intra-project collisions
  sibling_lines: list[str] = []
  if cur_pid and cur_pid != "outside-of-project":
    for sib in get_active_sibling_chats_in_project(
        cur_pid, exclude_conv_id=conv_id, hint_path=transcript_path
    ):
      stitle = sib["title"] if len(sib["title"]) <= 60 else sib["title"][:57] + "..."
      meta = [f"`{stitle}` (`conversation://{sib['cid']}`)"]
      if sib.get("doing"):
        meta.append(f"doing: {', '.join(sib['doing'])}")
      if sib.get("files"):
        meta.append("touched: " + ", ".join(f"`{x}`" for x in sib["files"][:3]))
      sibling_lines.append("- " + " — ".join(meta))

  # Collect other active projects only when multiple projects are configured
  other_lines: list[str] = []
  if len(projects) >= 2:
    for pid, pinfo in sorted(projects.items(), key=lambda kv: kv[1]["name"]):
      if pid == cur_pid:
        continue
      roots_str = ", ".join(f"`{r}`" for r in pinfo["roots"])
      ac = pinfo.get("active_chat")
      if not roots_str and not ac:
        continue
      parts = [f"- `{pinfo['name']}`"]
      if roots_str:
        parts.append(f"workspace {roots_str}")
      if ac:
        ac_title = ac[1] if len(ac[1]) <= 60 else ac[1][:57] + "..."
        parts.append(f"active chat: `{ac_title}` (`conversation://{ac[0]}`)")
      other_lines.append(" — ".join(parts))

  if not other_lines and not sibling_lines:
    return ""

  # Format combined intra-project sibling and cross-project radar blocks
  blocks = [
      "[SCOPE GUARD: WRONG-CHAT / WRONG-PROJECT RADAR]",
      f"Current chat: `{cur_title or conv_id}` | Project: `{cur_proj_name}` | "
      f"Workspace(s): {cur_roots}",
  ]
  if sibling_lines:
    blocks.append(
        f"Active sibling chats in `{cur_proj_name}` "
        "(avoid colliding on their files/worktrees):"
    )
    blocks.extend(sibling_lines)
  if other_lines:
    blocks.append("Other active projects & workspaces:")
    blocks.extend(other_lines)
  blocks.append(
      "If the user's new request clearly targets another project, workspace, or "
      f"sibling chat topic listed above rather than `{cur_title or cur_proj_name}`, DO NOT start "
      "investigating, reading foreign project files, or running commands! "
      "Reply immediately warning the user which project/chat the request belongs to "
      "(`[👉 <Target Chat Title>](conversation://<target_conv_id>)`), and ask whether "
      'to forward the message via `agentapi send-message <target_conv_id> "<user '
      'message>"` or execute it in this chat.'
  )
  return "\n".join(blocks)


# Match Russian and English evaluation phrases so recency bias does not cause rubber-stamping
CRITICAL_CHALLENGE_RE = re.compile(
    r"(?:"
    r"что(?:\s+ты(?:\s+об\s+этом)?)?\s+думаешь"
    r"|что\s+скажешь"
    r"|как\s+(?:ты\s+)?(?:думаешь|считаешь)"
    r"|поспорь(?:\s*,?\s*если\s+не\s+согласна)?"
    r"|возрази(?:\s*,?\s*если\s+не\s+согласна)?"
    r"|если\s+не\s+согласна"
    r"|покритикуй"
    r"|найди\s+(?:дыры|ошибки|подводные\s+камни|слабые\s+места)"
    r"|проверь\s+альтернативы"
    r"|так\s+ведь\s*\?"
    r"|согласна\s*\?"
    r"|what\s+do\s+you\s+think"
    r"|\bwdyt\b"
    r"|any\s+thoughts\s*\?"
    r"|\bthoughts\s*\?"
    r"|what\s+say\s+you"
    r"|push\s+back(?:\s+if\s+you\s+disagree)?"
    r"|argue\s+if\s+you\s+disagree"
    r"|disagree\s+with\s+me"
    r"|poke\s+holes"
    r"|play\s+devil'?s\s+advocate"
    r"|stress[- ]test\s+(?:this|it)"
    r"|challenge\s+(?:this|me|it)"
    r"|am\s+i\s+missing\s+anything"
    r"|does\s+(?:this|that)\s+make\s+sense\s*\?"
    r")",
    re.IGNORECASE,
)


# Inject a pre-mortem and alternatives directive when the user asks to challenge or evaluate
def build_critical_challenge_notice(last_user_input: str) -> str:
  if not last_user_input or re.search(
      r"Continuing unfinished work from previous conversation", last_user_input
  ):
    return ""
  m = CRITICAL_CHALLENGE_RE.search(last_user_input)
  if not m:
    return ""
  matched = re.sub(r"\s+", " ", m.group(0).strip())
  return (
      "[CRITICAL CHALLENGE TRIGGER: PRE-MORTEM & ALTERNATIVES]\n"
      f'The user explicitly asked for critical evaluation ("{matched}"). '
      "Treat this as a command to run an adversarial pre-mortem, NOT a social cue for approval:\n"
      "1. Falsify first: assume the proposed idea or diagnosis has a flaw or breaks an invariant "
      "(`glyph bounds`, `grapheme cluster`, `trailing spaces`, `strut`, BiDi, selection, caching, "
      "hot-path allocations) and check the actual source code.\n"
      "2. Mandatory alternative: formulate at least one concrete orthogonal or simpler "
      "alternative (including fixing the invariant higher up or deleting code instead of adding a "
      "branch) and compare trade-offs.\n"
      "3. No forced contrarianism: if the proposal genuinely holds up after checking code and "
      "alternatives, do NOT invent cosmetic nitpicks — state the exact failure modes and "
      "alternatives you tested and why the proposal wins."
  )


# Tool results may indent this line (e.g. "\n\n\t\t\t\tThe command exited ..." on
# macOS Jetski), so allow leading whitespace; otherwise the guard never fires.
EXIT_CODE_RE = re.compile(r"^\s*The command exited with code (\d+)\.", re.MULTILINE)
FAILURE_STREAK_THRESHOLD = int(os.environ.get("JETSKI_FAIL_STREAK", "2"))
BENIGN_EXIT1_CMD_RE = re.compile(
    r"^\s*(?:git\s+(?:grep|diff|check-ignore)|grep|egrep|fgrep|rg)\b"
)


def parse_current_turn_steps(transcript_path: str) -> list[tuple[int, dict]]:
  """Returns ordered (step_index, step_obj) after the last USER_INPUT step."""
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
  if not steps_by_idx:
    return []
  ordered_indices = sorted(steps_by_idx.keys())
  last_user_idx = -1
  for idx in ordered_indices:
    if steps_by_idx[idx].get("type") == "USER_INPUT":
      last_user_idx = idx
  return [
      (idx, steps_by_idx[idx]) for idx in ordered_indices if idx > last_user_idx
  ]


MENTION_URL_RE = re.compile(r"^URL: (\S+)", re.MULTILINE)


def last_user_input_mention_urls(transcript_path: str) -> str:
  """Returns the "URL:" lines of @-mentions in the latest USER_INPUT step, one per line.

  lastUserInput carries only the typed text, so a mentioned CL/PR/bug shows up there as a bare
  "@[repo | 123:subject]" label; its link lives in the ADDITIONAL_METADATA block of the
  transcript. Only "URL:" lines are taken because mention descriptions may quote other links
  (e.g. "Reviewed-on:" of a reverted CL).
  """
  if not transcript_path or not os.path.exists(transcript_path):
    return ""
  last_input = ""
  try:
    with open(transcript_path, "r", encoding="utf-8") as f:
      for line in f:
        if '"USER_INPUT"' not in line:
          continue
        try:
          obj = json.loads(line)
        except Exception:
          continue
        if obj.get("type") == "USER_INPUT":
          last_input = str(obj.get("content", ""))
  except Exception:
    return ""
  return "\n".join(MENTION_URL_RE.findall(last_input))


def is_async_wakeup(transcript_path: str, last_user_input: str = "") -> bool:
  """True when invocationNum == 0 is a background-task or subagent wakeup, not a user turn.

  Jetski resets invocationNum to 0 when a background task or subagent wakes the
  conversation after the model paused to wait for it. At PreInvocation time the
  SYSTEM_MESSAGE step is not in transcript.jsonl yet, so the turn since the last
  USER_INPUT has more async launches than SYSTEM_MESSAGE completions.
  """
  if "<SYSTEM_MESSAGE>" in (last_user_input or ""):
    return True
  turn_steps = parse_current_turn_steps(transcript_path)
  if not turn_steps:
    return False
  async_launches = 0
  sys_messages = 0
  for _, step in turn_steps:
    stype = step.get("type", "")
    if stype == "PLANNER_RESPONSE":
      for tc in step.get("tool_calls") or []:
        if tc.get("name") == "invoke_subagent":
          async_launches += 1
    elif stype == "GENERIC":
      content = str(step.get("content", ""))
      if (
          step.get("status") == "RUNNING"
          or "Tool is running as a background task with task id:" in content
      ):
        async_launches += 1
    elif stype == "SYSTEM_MESSAGE":
      sys_messages += 1
  return async_launches > sys_messages


def get_consecutive_cmd_failures(transcript_path: str) -> tuple[int, int]:
  """Returns (consecutive_failures_at_tail, last_cmd_step_idx) for the current turn."""
  turn_steps = parse_current_turn_steps(transcript_path)
  if not turn_steps:
    return 0, -1
  streak = 0
  last_cmd_idx = -1
  pending_cmd_idx = None
  pending_cmd_str = ""
  for idx, step in turn_steps:
    stype = step.get("type", "")
    tool_calls = step.get("tool_calls") or []
    if stype == "PLANNER_RESPONSE" and tool_calls:
      for tc in tool_calls:
        if tc.get("name") == "run_command":
          pending_cmd_idx = idx
          pending_cmd_str = str((tc.get("args") or {}).get("CommandLine", ""))
    elif stype == "GENERIC" and pending_cmd_idx is not None:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content)
      if m:
        code = int(m.group(1))
        # Ignore exit code 1 from grep/rg/git check-ignore ("no matches found")
        if code == 1 and BENIGN_EXIT1_CMD_RE.search(pending_cmd_str):
          pending_cmd_idx = None
          pending_cmd_str = ""
          continue
        last_cmd_idx = pending_cmd_idx
        if code != 0:
          streak += 1
        else:
          streak = 0
        pending_cmd_idx = None
        pending_cmd_str = ""
  return streak, last_cmd_idx


def build_agentapi_prefix(
    conv_id: str, transcript_path: str, workspace_paths: list[str]
) -> str:
  """Builds the command prefix for agentapi new-conversation with project_id.

  Always unsets ANTIGRAVITY_SOURCE_METADATA so the new conversation does not
  receive parentConversationId (which would make hooks treat it as a subagent).
  Preserves the conversation's project_id if it belongs to a project, or
  explicitly unsets ANTIGRAVITY_PROJECT_ID if the conversation is outside-of-project.
  """
  _, current_project_id = get_conversation_db_info(conv_id, transcript_path)
  if current_project_id and current_project_id != "outside-of-project":
    return (
        "env -u ANTIGRAVITY_SOURCE_METADATA"
        f' ANTIGRAVITY_PROJECT_ID="{current_project_id}" agentapi'
        " new-conversation"
    )

  target_pid = infer_project_id_from_workspaces(workspace_paths)
  if target_pid:
    return (
        "env -u ANTIGRAVITY_SOURCE_METADATA"
        f' ANTIGRAVITY_PROJECT_ID="{target_pid}" agentapi new-conversation'
    )
  return (
      'env -u ANTIGRAVITY_SOURCE_METADATA ANTIGRAVITY_PROJECT_ID="outside-of-project"'
      " agentapi new-conversation"
  )


def is_chat_lifecycle_handoff_command(cmd: str) -> bool:
  """True only for a real `chat_lifecycle.py handoff` invocation, never a quoted mention.

  A quoted mention (e.g. in an `agentapi send-message` text) exits 0 as well,
  and since the CLI rule trusts the exit code alone a loose match would close
  the chat. `handoff --help` exits 0 without launching anything either.
  """
  from pre_tool_guard import is_chat_lifecycle_handoff_launch

  if not is_chat_lifecycle_handoff_launch(cmd):
    return False
  return not re.search(r"\bhandoff\s+(?:--help|-h|help)\b", cmd)


def is_handoff_launch_command(cmd: str) -> bool:
  """Returns True if cmd invokes `agentapi new-conversation` or `chat_lifecycle.py handoff`."""
  if "agentapi" in cmd and "new-conversation" in cmd:
    return True
  return is_chat_lifecycle_handoff_command(cmd)


def get_handoff_launch_step_in_turn(transcript_path: str) -> int:
  """Returns step_index where PLANNER_RESPONSE actually succeeded launching a handoff chat in this turn, or -1."""
  turn_steps = parse_current_turn_steps(transcript_path)
  launch_idx = -1
  pending_launch_idx = None
  pending_is_cli = False
  for idx, step in turn_steps:
    stype = step.get("type", "")
    if stype == "PLANNER_RESPONSE":
      pending_launch_idx = None
      for tc in step.get("tool_calls") or []:
        if tc.get("name") == "run_command":
          cmd = str((tc.get("args") or {}).get("CommandLine", ""))
          if is_handoff_launch_command(cmd):
            pending_launch_idx = idx
            pending_is_cli = is_chat_lifecycle_handoff_command(cmd)
    elif stype == "GENERIC" and pending_launch_idx is not None:
      content = str(step.get("content", ""))
      if "\nFile Path: " in content[:200]:
        continue
      m = EXIT_CODE_RE.search(content[:300])
      # The stored tool output keeps only the tail of a long result, so the
      # JSON printed by the CLI may be gone; the CLI exits non-zero whenever
      # no chat was started, which makes its exit code proof enough. A raw
      # agentapi call has no such contract and must still show the JSON
      launched_marker = (
          '"newConversation"' in content
          or '"new_conversation_id"' in content
          or pending_is_cli
      )
      if step.get("status") == "DONE" and m and int(m.group(1)) == 0 and launched_marker:
        launch_idx = pending_launch_idx
        pending_launch_idx = None
  return launch_idx


CONTINUATION_ID_RE = re.compile(r'"(?:new_conversation_id|conversationId)":\s*"([A-Za-z0-9_\-]+)"')


def get_continuation_id(transcript_path: str, launch_step: int) -> str:
  """Id of the chat launched at launch_step, from its command output, or ""."""
  for idx, step in parse_current_turn_steps(transcript_path):
    if idx > launch_step and step.get("type") == "GENERIC":
      m = CONTINUATION_ID_RE.search(str(step.get("content", "")))
      if m:
        return m.group(1)
  return ""


def build_continuation_title(conv_id: str, fallback_text: str = "") -> str:
  raw_title, _ = get_conversation_db_info(conv_id)
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()

  try:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    now_tz = datetime.now(tz)
    cutoff = datetime(2026, 9, 28, 6, 0, tzinfo=tz)
    time_str = (
        now_tz.strftime("%H:%M")
        if now_tz >= cutoff
        else datetime.now(ZoneInfo("UTC")).strftime("%H:%M")
    )
  except Exception:
    time_str = datetime.now().strftime("%H:%M")

  if base_title:
    return f"[{time_str}] {base_title}"
  return f"[{time_str}] <Chat Title>"


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


def measure_context(conv_id: str, transcript_path: str, gm_count_hint: int) -> dict:
  """Context usage of the last model call via the LS RPC, else a KB estimate.

  Returns pct/tokens/max/ckpt plus `fallback`, `err` and `gm_count` (number of
  generator metadata entries). The count is reused as the next offset because
  every entry embeds the full system prompt, so an unpaged response grows by
  ~8 KB per model call.
  """
  m = {"fallback": False, "err": "", "gm_count": gm_count_hint}
  t0 = time.monotonic()
  try:
    import chat_lifecycle

    gm = []
    for off in (gm_count_hint - 1, 0) if gm_count_hint > 1 else (0,):
      # Retry from 0 only on a quick empty answer, never after a slow one,
      # so two RPC timeouts cannot add up past the hook's 5 s budget
      if off == 0 and gm_count_hint > 1 and time.monotonic() - t0 > RPC_TIMEOUT_S:
        break
      resp = chat_lifecycle.ls_rpc(
          "GetCascadeTrajectoryGeneratorMetadata",
          {
              "cascadeId": conv_id,
              "generatorMetadataOffset": off,
              "includeMessages": False,
          },
          timeout=RPC_TIMEOUT_S,
      )
      gm = resp.get("generatorMetadata") or []
      if gm:
        m["gm_count"] = off + len(gm)
        break
    for entry in reversed(gm):
      csm = (entry.get("chatModel") or {}).get("chatStartMetadata") or {}
      cwm = csm.get("contextWindowMetadata") or {}
      if "estimatedTokensUsed" in cwm and int(cwm.get("maxContextTokens") or 0) > 0:
        used = int(cwm["estimatedTokensUsed"])
        cap = int(cwm["maxContextTokens"])
        m.update(
            pct=100.0 * used / cap,
            tokens=used,
            max=cap,
            # The LS sends -1 until the first compaction and then omits the
            # field, because proto3 JSON drops the zero value of checkpoint #0
            ckpt=int(csm.get("checkpointIndex", 0)),
        )
        return m
    m["err"] = "no contextWindowMetadata"
  except Exception as e:
    m["err"] = f"{type(e).__name__}: {e}"[:200]
  tokens = int(
      get_transcript_size_kb(transcript_path) * 1024 / FALLBACK_BYTES_PER_TOKEN
  )
  m.update(
      pct=100.0 * tokens / FALLBACK_MAX_TOKENS,
      tokens=tokens,
      max=FALLBACK_MAX_TOKENS,
      ckpt=-1,
      fallback=True,
  )
  return m


def resolve_handoff_target(
    conv_id: str, transcript_path: str, artifact_dir: str
) -> dict:
  """Where the agent records its state for the handoff (reads SQLite, so trigger paths only).

  In-project chats use the chain state file next to the project roadmap;
  outside-project chats keep the handoff_summary artifact.
  """
  raw_title, project_id = get_conversation_db_info(conv_id, transcript_path)
  if project_id and project_id != "outside-of-project":
    try:
      import roadmap

      return {"in_project": True, "file": roadmap.state_path(project_id, conv_id)}
    except Exception:
      # An id that cannot be a path component (or a broken roadmap module)
      # must not take the guard down; the summary flow still works
      pass
  base_title = CONT_SUFFIX_RE.sub("", raw_title).strip()
  base_title = CONT_PREFIX_RE.sub("", base_title).strip()
  slug = re.sub(r"[^\w\-]+", "_", base_title, flags=re.UNICODE).strip("_")[:40]
  short_id = conv_id[:8] if conv_id else "unknown"
  handoff_filename = (
      f"handoff_summary_{slug}_{short_id}.md"
      if slug
      else f"handoff_summary_{short_id}.md"
  )
  return {
      "in_project": False,
      "file": (
          os.path.join(artifact_dir, handoff_filename)
          if artifact_dir
          else f"/tmp/{handoff_filename}"
      ),
  }


TASKS_NOTE = (
    "Running subagents/background tasks: wait for them if they finish within a"
    " couple of minutes; background tasks are killed when the handoff runs, so"
    " record what was running and the exact command to restart it."
)


def handoff_steps(conv_id: str, target: dict, new_title: str) -> str:
  if target["in_project"]:
    return (
        f"1. Roadmap: `python3 {ROADMAP_CLI} set {conv_id} <ID> done|doing|todo`"
        " (`add` for new work, `edit`/`move`/`reorder` to reword or reprioritize);"
        " never edit roadmap files directly.\n"
        f"2. Overwrite the chain state file `{target['file']}` (write_to_file):"
        " goal, current state with evidence (modified files, last build/test"
        " result), failed hypotheses, running subagents/background tasks and how"
        " to restart them, exact next step. It moves to the continuation as is.\n"
        f'3. Run `python3 {LIFECYCLE_CLI} handoff {conv_id} --next "<1-line next'
        ' action>"`.\n'
        "4. In your final message paste the roadmap block from the handoff output"
        f" and the link `[👉 {new_title}](conversation://<new_conversation_id>)`,"
        " then end the turn."
    )
  return (
      f"1. Write `{target['file']}` (write_to_file, UserFacing: true): goal, Epic"
      " Roadmap checklist ([x] done / [ ] pending, never drop pending items from"
      " earlier handoffs), modified files, failed hypotheses, running"
      " subagents/background tasks and how to restart them, exact next step,"
      f" link `[Previous Conversation](conversation://{conv_id})`.\n"
      f'2. Run `python3 {LIFECYCLE_CLI} handoff {conv_id} "{target["file"]}"'
      ' --next "<1-line next action>"`.\n'
      f"3. End the turn with the link `[👉 {new_title}](conversation://<new_conversation_id>)`."
  )


def _usage_str(m: dict) -> str:
  est = ", estimated from transcript size" if m["fallback"] else ""
  return (
      f"{m['pct']:.0f}% of the context window"
      f" ({m['tokens'] // 1000}k/{m['max'] // 1000}k tokens{est})"
  )


def _inject(*messages: str) -> dict:
  return {"injectSteps": [{"ephemeralMessage": msg} for msg in messages]}


def append_log(record: dict) -> None:
  path = os.path.expanduser(
      os.environ.get("JETSKI_CONTEXT_GUARD_LOG") or DEFAULT_LOG_PATH
  )
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # One-step rotation bounds the log at ~2x LOG_MAX_BYTES; a rotation race
    # between concurrent chats can only drop a few records
    if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
      os.replace(path, path + ".1")
    with open(path, "a", encoding="utf-8") as f:
      f.write(json.dumps(record, ensure_ascii=False) + "\n")
  except Exception:
    pass


def run(data: dict, log: dict) -> dict:
  """Decides the hook output for one PreInvocation event and fills `log`."""
  # Skip battle mode forks and subagents
  if data.get("isBattleMode") or data.get("parentConversationId"):
    log["level"] = "skip"
    return {}

  conv_id = data.get("conversationId", "")
  transcript_path = data.get("transcriptPath", "")
  artifact_dir = data.get("artifactDirectoryPath", "")
  workspace_paths = data.get("workspacePaths", [])
  steps = int(data.get("initialNumSteps", 0))
  invocation_num = int(data.get("invocationNum", 0))
  last_user_input = data.get("lastUserInput", "")
  log.update(conv=conv_id, inv=invocation_num, steps=steps)

  state_path = (
      os.path.join(artifact_dir, "scratch", ".context_guard_state.json")
      if artifact_dir
      else f"/tmp/jetski_context_guard_{conv_id}.json"
  )
  state = load_state(state_path)
  async_wakeup = invocation_num == 0 and is_async_wakeup(
      transcript_path, last_user_input
  )
  is_turn_start = invocation_num == 0 and not async_wakeup

  # Reset per-turn state at the start of each new user turn; a hard trigger
  # that was ignored last turn is re-evaluated below rather than snoozed
  if is_turn_start:
    next_turn_inv_threshold = MAX_TURN_INVOCATIONS
    if (
        state.get("next_turn_inv_threshold") != MAX_TURN_INVOCATIONS
        or state.get("pending_handoff_launch")
        or state.get("handoff_completed")
    ):
      state["next_turn_inv_threshold"] = MAX_TURN_INVOCATIONS
      state["pending_handoff_launch"] = False
      state["pending_turn_start"] = False
      state["handoff_completed"] = False
      save_state(state_path, state)

    # NOTE: No automatic reopening of finalized chats. Lifecycle markers are
    # applied mechanically only by `chat_lifecycle.py` (set-title / handoff /
    # finalize / reopen), never inferred from user wording.

    # Auto-title untitled chat when prompt mentions a PR, CL, bug, or commit
    if last_user_input and conv_id:
      try:
        import chat_lifecycle
        title_source = last_user_input
        mention_urls = last_user_input_mention_urls(transcript_path)
        if mention_urls:
          title_source = f"{last_user_input}\n{mention_urls}"
        auto_title = chat_lifecycle.auto_title_conversation(conv_id, title_source)
        if auto_title:
          log["auto_title"] = auto_title
          state["auto_title_notice"] = (
              f"[TITLE GUARD] Conversation title automatically set to '{auto_title}' "
              f"based on the entity in your prompt. You may refine the topic description via "
              f"'python3 {LIFECYCLE_CLI} set-title {conv_id} \"<Topic>\"' if needed."
          )
          save_state(state_path, state)
      except Exception as e:
        log["auto_title_err"] = str(e)[:200]

    # Safety net: ensure browser sounds are re-enabled when new turn/chat starts
    try:
      config_path = os.path.expanduser("~/.gemini/config/config.json")
      if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as f:
          c = json.load(f)
        user_settings = c.setdefault("userSettings", {})
        if (
            user_settings.get("enableSoundsForSpecialEvents") is False
            or "enableSoundsForSpecialEvents" in c
        ):
          c.pop("enableSoundsForSpecialEvents", None)
          user_settings["enableSoundsForSpecialEvents"] = True
          with open(config_path, "w", encoding="utf-8") as f:
            json.dump(c, f, indent=2)
    except Exception:
      pass
  else:
    next_turn_inv_threshold = int(
        state.get("next_turn_inv_threshold", MAX_TURN_INVOCATIONS)
    )

  if not is_turn_start:
    launch_step = get_handoff_launch_step_in_turn(transcript_path)
    if launch_step >= 0:
      log["level"] = "post_handoff"
      if (
          state.get("pending_handoff_launch")
          or not state.get("handoff_completed")
          or "handed_off_to" not in state
      ):
        state["pending_handoff_launch"] = False
        state["handoff_completed"] = True
        # The CLI records the id itself; the truncated tool output may yield ""
        state["handed_off_to"] = (
            get_continuation_id(transcript_path, launch_step)
            or state.get("handed_off_to", "")
        )
        save_state(state_path, state)
      # Check if a background task / subagent woke the conversation up after handoff
      # or if the agent is still calling tools after launching the continuation:
      turn_steps = parse_current_turn_steps(transcript_path)
      steps_after_launch = [
          s for idx, s in turn_steps if idx > launch_step and s.get("type") != "GENERIC"
      ]
      if steps_after_launch:
        return _inject(
            f"[CONTEXT GUARD: CHAT ALREADY HANDED OFF] Warning: this conversation ALREADY launched a continuation chat "
            f"(at step #{launch_step}), but resumed execution (e.g., woken up by a background `task` or subagent). "
            f"It is STRICTLY FORBIDDEN to continue debugging, editing files, or launching a second continuation chat here! "
            f"If any background tasks remain active, terminate them via `manage_task` (`kill`) and end your turn immediately."
        )
      return {}

  # The chat is closed for good once it handed off: work done here would
  # split the task across two chats, so the user is sent to the continuation
  # once per turn and the context triggers no longer apply
  if "handed_off_to" in state:
    log["level"] = "closed"
    if not is_turn_start:
      return {}
    cont = state["handed_off_to"]
    where = (
        f"conversation://{cont}"
        if cont
        else f"its continuation (`python3 {LIFECYCLE_CLI} status {conv_id}` shows it)"
    )
    forward = (
        f"`agentapi send-message {cont} \"<the user's message verbatim>\"`"
        if cont
        else "`agentapi send-message <continuation id> \"<the user's message verbatim>\"`"
    )
    return _inject(
        f"[CONTEXT GUARD: CLOSED CHAT] This conversation was already handed off to {where}."
        " If this turn was started by a background task or subagent, end it without a reply."
        " Otherwise open your reply by telling the user that this is an old, closed chat and"
        " that continuing here splits the work across chats and causes confusion. Do not work"
        " on the request here; offer to forward the user's message to the continuation and,"
        f" once the user agrees, run {forward} and end the turn."
    )

  m = measure_context(conv_id, transcript_path, int(state.get("gm_count", 0)))
  log.update(
      pct=round(m["pct"], 1),
      tokens=m["tokens"],
      max=m["max"],
      ckpt=m["ckpt"],
      fallback=m["fallback"],
  )
  if m["err"]:
    log["err"] = m["err"]
  if m["gm_count"] != state.get("gm_count", 0):
    state["gm_count"] = m["gm_count"]
    save_state(state_path, state)

  pending = bool(state.get("pending_handoff_launch"))
  # A turn-start trigger already gave the 3-step handoff instructions at inv 0
  # and stop_guard enforces the launch on stop, so skip repeating it on inv 1..N
  turn_start_pending = pending and bool(state.get("pending_turn_start"))
  if not is_turn_start and pending and not turn_start_pending:
    log["level"] = "pending"
    handoff_file = state.get("pending_handoff_file", "")
    missing = (
        ""
        if os.path.exists(handoff_file)
        else f" (`{handoff_file}` does not exist yet: write it first)"
    )
    return _inject(
        "[CONTEXT GUARD REMINDER] The handoff is still pending. If you are"
        " ready to deliver your final answer to the user and wait for their"
        " reply, write the complete answer right here and end the turn WITHOUT"
        " handing off; otherwise stop all other investigation now and hand"
        f" off{missing}:\n{TASKS_NOTE}\n"
        + handoff_steps(
            conv_id,
            {"in_project": bool(state.get("pending_in_project")), "file": handoff_file},
            state.get("pending_title") or "<title>",
        )
    )

  late = m["ckpt"] >= 0
  hard_pct = TURN_START_PCT if is_turn_start else HARD_PCT
  # The transcript-size estimate overcounts (untruncated tool output, history
  # kept past compaction), so it may only nag; stop_guard enforces turn-start ones
  hard = not pending and ((m["pct"] >= hard_pct and not m["fallback"]) or late)
  turn_loop_exceeded = (
      not is_turn_start and invocation_num >= next_turn_inv_threshold
  )

  if not (hard or turn_loop_exceeded):
    messages = []
    if state.get("auto_title_notice"):
      messages.append(state.pop("auto_title_notice"))
      save_state(state_path, state)
    # Inject cross-project / sibling scope radar and git sync notice at turn start
    if is_turn_start and last_user_input:
      radar_notice = build_scope_radar_notice(
          conv_id, transcript_path, workspace_paths, last_user_input
      )
      if radar_notice:
        messages.append(radar_notice)
      sync_notice = build_git_sync_notice(conv_id, workspace_paths)
      if sync_notice:
        messages.append(sync_notice)
      # Inject critical challenge directive at turn start when prompt contains evaluation triggers
      challenge_notice = build_critical_challenge_notice(last_user_input)
      if challenge_notice:
        messages.append(challenge_notice)
    soft_due = steps - int(
        state.get("soft_last_steps", -SOFT_REMIND_EVERY_STEPS)
    ) >= SOFT_REMIND_EVERY_STEPS
    if not pending and m["pct"] >= SOFT_PCT and soft_due:
      log["level"] = "soft"
      state["soft_last_steps"] = steps
      save_state(state_path, state)
      target = resolve_handoff_target(conv_id, transcript_path, artifact_dir)
      messages.append(
          f"[CONTEXT GUARD: SOFT LIMIT] Context is at {_usage_str(m)}. Keep"
          " working, and if further autonomous work remains after the nearest"
          " micro-boundary (test run finished, hypothesis confirmed or"
          " rejected, commit made, subagent returned), hand off so the"
          " continuation chat keeps working. However, if you are ready to"
          " answer the user and wait for their reply, DO NOT hand off in this"
          " turn — write your complete response right here and end the turn"
          f" (the next turn will hand off automatically):\n{TASKS_NOTE}\n"
          + handoff_steps(conv_id, target, build_continuation_title(conv_id))
      )
    if not is_turn_start:
      streak, last_cmd_idx = get_consecutive_cmd_failures(transcript_path)
      if (
          streak >= FAILURE_STREAK_THRESHOLD
          and streak % FAILURE_STREAK_THRESHOLD == 0
          and last_cmd_idx != state.get("last_warned_fail_step")
      ):
        log["fail_streak"] = streak
        state["last_warned_fail_step"] = last_cmd_idx
        save_state(state_path, state)
        messages.append(
            f"[TWO-STRIKE DEBUG GUARD] Warning: {streak} consecutive commands in the current turn failed. "
            f"DO NOT make a third blind guess! Stop, read the exact error output and source declarations (`.h` / docs), "
            f"revert broken edits if needed (`git diff` / `git checkout`), and revise your hypothesis before proceeding."
        )
    return _inject(*messages) if messages else {}

  level = ("late" if late else "hard") if hard else "loop"
  log["level"] = level
  target = resolve_handoff_target(conv_id, transcript_path, artifact_dir)
  new_title = build_continuation_title(conv_id, last_user_input)
  cmd_prefix = build_agentapi_prefix(conv_id, transcript_path, workspace_paths)

  # The state is replaced wholesale so per-trigger flags (stop_guard's
  # stop_blocked_handoff, the fail-streak marker) start fresh
  save_state(state_path, {
      "gm_count": state.get("gm_count", 0),
      "soft_last_steps": state.get("soft_last_steps", -SOFT_REMIND_EVERY_STEPS),
      "last_triggered_level": level,
      "last_triggered_pct": round(m["pct"], 1),
      "last_triggered_invocation_num": invocation_num,
      "pending_handoff_launch": True,
      "pending_turn_start": is_turn_start,
      "handoff_completed": False,
      "pending_title": new_title,
      "pending_cmd_prefix": cmd_prefix,
      "pending_handoff_file": target["file"],
      "pending_in_project": target["in_project"],
      "next_turn_inv_threshold": (
          max(invocation_num, next_turn_inv_threshold) + SNOOZE_TURN_INVOCATIONS
          if not is_turn_start
          else MAX_TURN_INVOCATIONS
      ),
  })

  late_note = (
      " History was already compacted, so earlier details may be gone: take"
      " facts from files, git and test output, not from memory."
      if late
      else ""
  )
  if level == "loop":
    head = (
        "[CONTEXT GUARD: MID-TURN CIRCUIT BREAKER] Single-turn iteration limit"
        f" exceeded ({invocation_num} consecutive steps; context at"
        f" {_usage_str(m)}). If you are ready to deliver your answer to the"
        " user and wait for their reply, write the complete answer right here"
        " and end the turn WITHOUT handing off; otherwise bring any"
        " half-edited file to a clean checkpoint and hand off."
    )
  elif not is_turn_start:
    head = (
        f"[CONTEXT GUARD: MID-TURN HANDOFF] Context is at {_usage_str(m)}.{late_note}"
        " If you are ready to answer the user's request right now and wait for"
        " their reply, DO NOT hand off in this turn — write your complete"
        " response right here and end the turn (the next turn will hand off"
        " automatically). Otherwise, if further autonomous investigation,"
        " edits, or tests are needed, stop at the nearest safe checkpoint (no"
        " half-edited files, no new investigations) and hand off now."
    )
  else:
    head = (
        f"[CONTEXT GUARD: HANDOFF REQUIRED] Context is at {_usage_str(m)}.{late_note}"
        " Do NOT investigate or write the answer to the user's new request in"
        " this chat (an answer written before handoff stays buried in a closed"
        " chat while the continuation opens empty). Immediately hand off the"
        " user's request to a new chat in this turn so the full answer is"
        " delivered there, and reply here only with the continuation link."
    )
  return _inject(f"{head}\n{TASKS_NOTE}\n{handoff_steps(conv_id, target, new_title)}")


def main() -> None:
  t0 = time.monotonic()
  log = {
      "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
      "level": "none",
  }
  out: dict = {}
  try:
    raw_input = sys.stdin.read().strip()
    if raw_input:
      out = run(json.loads(raw_input), log)
  except Exception as e:
    # This hook runs before every model call of every chat: a crash would
    # break them all, so degrade to a no-op and leave a trace in the log
    out = {}
    log["level"] = "error"
    log["err"] = f"{type(e).__name__}: {e}"[:300]
  print(json.dumps(out, ensure_ascii=False))
  log["ms"] = round((time.monotonic() - t0) * 1000)
  if log["level"] != "skip":
    append_log(log)


if __name__ == "__main__":
  main()
