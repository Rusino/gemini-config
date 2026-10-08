#!/usr/bin/env python3
"""Live project activity badge hook & watcher for Jetski / Antigravity sidebar.

When sidebar project folders are collapsed (`Group By: Project`), Jetski's
`GroupHeader` renders only `project.name` and the workspace path without any
running-chat indicator. Meanwhile, the Language Server runs an `fsnotify`
watcher on `~/.gemini/config/projects/*.json` and streams changes to the UI
via `ProjectUpdatesStream`.

This script reads the initial snapshot from the local Connect-RPC
`JetboxSubscribeToSummaries` stream (with fallback to `GetAllCascadeTrajectories`),
counts active (`CASCADE_RUN_STATUS_RUNNING` / `notFullyIdle`), blocked
(`waitingSteps`), and unread finished (`lastModifiedTime > lastUserViewTime`)
top-level conversations per project, and atomically updates the `"name"` field
in `~/.gemini/config/projects/<project_id>.json` with a compact suffix badge
(e.g. ` · ⟳ 1`, ` · ⚠ 1`, ` · ● 1`, or ` · ⟳ 1 ● 2`).

Modes of operation:
- Hook mode (`--event PreInvocation` / `--event Stop`):
  Reads the hook stdin payload, immediately outputs `{}` on stdout so the LLM
  turn is never delayed, and spawns a detached background process (`--bg-trigger`)
  that updates badges immediately and maintains a single `flock`-guarded polling
  watcher (`--watch`) until all conversations across all projects are idle and read.
- CLI mode (`--sync-once`, `--watch`, `--clear`, `--status`):
  Can be invoked manually or from tests to inspect or reset project badges.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import fcntl
import glob
import json
import os
import re
import socket
import struct
import subprocess
import sys
import time
from typing import Any
from urllib.parse import unquote
import urllib.request

DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
DEFAULT_ANNOTATIONS_DIR = os.path.expanduser("~/.gemini/jetski/annotations")
DEFAULT_LS_ADDRESS = "localhost:5387"

# Matches one or more trailing activity badges such as:
#   " · ⟳ 1", " · ⚠ 2", " · ● 1", " · ⟳ 1 ⚠ 1 ● 2"
BADGE_RE = re.compile(
    r"(?:\s*·\s*(?:[⟳⚠●]\s*\d+)(?:\s+(?:[⟳⚠●]\s*\d+))*)+$"
)

# Handed-off predecessors (▸, ✓) and finalized chats («, ‹✓›, », «») are no
# longer active and should not be counted as unread once their turn finishes
PREDECESSOR_TITLE_RE = re.compile(
    r"^\s*\[\d{1,2}:\d{2}\]\s*(?:‹✓›|«»|[▸✓«»])(?:\s|$)"
)

WATCH_POLL_INTERVAL_SEC = 1.5
STOP_SETTLE_DELAY_SEC = 0.4
IDLE_POLLS_BEFORE_EXIT = 3
MAX_WATCH_DURATION_SEC = 86400


def _uid_suffix() -> str:
  try:
    return str(os.getuid())
  except AttributeError:
    return "default"


def _conn_cache_path() -> str:
  return f"/tmp/jetski_ls_conn_{_uid_suffix()}.json"


def _sync_lock_path() -> str:
  return f"/tmp/jetski_project_badge_sync_{_uid_suffix()}.lock"


def _watcher_lock_path() -> str:
  return f"/tmp/jetski_project_badge_watcher_{_uid_suffix()}.lock"


def _view_state_path() -> str:
  return f"/tmp/jetski_project_badge_view_{_uid_suffix()}.json"


def strip_activity_badge(name: str) -> str:
  """Removes any trailing activity badge from a project name."""
  if not name:
    return ""
  return BADGE_RE.sub("", name).rstrip()


def format_project_name(
    base_name: str,
    running_count: int = 0,
    blocked_count: int = 0,
    unread_count: int = 0,
) -> str:
  """Formats a project name with an optional activity badge suffix."""
  clean_base = strip_activity_badge(base_name)
  if not clean_base:
    return base_name
  parts: list[str] = []
  if running_count > 0:
    parts.append(f"⟳ {running_count}")
  if blocked_count > 0:
    parts.append(f"⚠ {blocked_count}")
  if unread_count > 0:
    parts.append(f"● {unread_count}")
  if not parts:
    return clean_base
  return f"{clean_base} · {' '.join(parts)}"


def _file_uri_to_path(uri: str) -> str:
  if not uri or not uri.startswith("file://"):
    return ""
  return unquote(uri[len("file://") :]).rstrip("/")


def load_projects(
    projects_dir: str,
) -> tuple[dict[str, tuple[str, dict[str, Any]]], list[tuple[str, str]]]:
  """Loads all project JSON files from `projects_dir`.

  Returns:
    - known_projects: mapping of `project_id -> (file_path, parsed_json)`
    - folder_to_project: list of `(folder_path, project_id)` sorted by
      descending path length (most specific workspace path first).
  """
  known_projects: dict[str, tuple[str, dict[str, Any]]] = {}
  folder_to_project: list[tuple[str, str]] = []
  if not os.path.isdir(projects_dir):
    return known_projects, folder_to_project

  for pfile in sorted(glob.glob(os.path.join(projects_dir, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      if not isinstance(pdata, dict):
        continue
      pid = pdata.get("id", "")
      if not pid:
        continue
      known_projects[pid] = (pfile, pdata)
      resources = (pdata.get("projectResources") or {}).get("resources") or []
      for r in resources:
        if not isinstance(r, dict):
          continue
        for uri in (
            (r.get("gitFolder") or {}).get("folderUri", ""),
            r.get("folderUri", ""),
        ):
          folder_path = _file_uri_to_path(uri)
          if folder_path:
            folder_to_project.append((folder_path, pid))
    except (OSError, ValueError):
      continue

  folder_to_project.sort(key=lambda item: len(item[0]), reverse=True)
  return known_projects, folder_to_project


def match_workspace_to_project(
    workspace_paths: list[str], folder_to_project: list[tuple[str, str]]
) -> str:
  """Matches workspace paths against project resource folders."""
  for raw_ws in workspace_paths:
    if not raw_ws:
      continue
    ws = (
        _file_uri_to_path(raw_ws)
        if raw_ws.startswith("file://")
        else raw_ws.rstrip("/")
    )
    if not ws:
      continue
    for folder_path, pid in folder_to_project:
      if ws == folder_path or ws.startswith(folder_path + "/"):
        return pid
  return ""


def _read_cached_conn() -> tuple[str, str]:
  cache_file = _conn_cache_path()
  try:
    with open(cache_file, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data.get("ls_address", ""), data.get("csrf_token", "")
  except (OSError, ValueError):
    return "", ""


def _write_cached_conn(ls_address: str, csrf_token: str) -> None:
  if not ls_address or not csrf_token:
    return
  cache_file = _conn_cache_path()
  tmp_file = f"{cache_file}.tmp.{os.getpid()}"
  try:
    fd = os.open(tmp_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
      json.dump({"ls_address": ls_address, "csrf_token": csrf_token}, f)
    os.replace(tmp_file, cache_file)
  except OSError:
    try:
      os.unlink(tmp_file)
    except OSError:
      pass


def _discover_from_http_index(ls_address: str) -> str:
  """Extracts csrfToken from `window.__APP_CONFIG__` on the Jetbox web index."""
  addr = ls_address.removeprefix("http://").removeprefix("https://")
  url = f"http://{addr}/"
  try:
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=1.0) as resp:
      html = resp.read().decode("utf-8", errors="ignore")
    m = re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html)
    if m:
      return m.group(1)
  except Exception:
    pass
  return ""


def _discover_from_proc() -> tuple[str, str]:
  """Scans /proc/<pid>/environ on Linux for ANTIGRAVITY_LS_ADDRESS and CSRF."""
  ls_addr = ""
  csrf = ""
  if not os.path.isdir("/proc"):
    return ls_addr, csrf
  try:
    for pid_dir in os.listdir("/proc"):
      if not pid_dir.isdigit():
        continue
      try:
        with open(f"/proc/{pid_dir}/environ", "rb") as f:
          raw = f.read()
        if b"ANTIGRAVITY_CSRF_TOKEN=" not in raw:
          continue
        for item in raw.split(b"\x00"):
          if item.startswith(b"ANTIGRAVITY_LS_ADDRESS=") and not ls_addr:
            ls_addr = item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
          elif item.startswith(b"ANTIGRAVITY_CSRF_TOKEN=") and not csrf:
            csrf = item.split(b"=", 1)[1].decode("utf-8", errors="ignore")
        if ls_addr and csrf:
          return ls_addr, csrf
      except (OSError, PermissionError):
        continue
  except OSError:
    pass
  return ls_addr, csrf


def _discover_from_ps() -> tuple[str, str]:
  """Fallback discovery from `ps` command-line flags (e.g. macOS Electron)."""
  ls_addr = ""
  csrf = ""
  try:
    out = subprocess.check_output(
        ["ps", "-axo", "command"], stderr=subprocess.DEVNULL, timeout=1.5
    ).decode("utf-8", errors="ignore")
    for line in out.splitlines():
      if "csrf_token" not in line:
        continue
      m_csrf = re.search(r"--csrf_token[=\s]+([A-Za-z0-9_-]+)", line)
      if m_csrf:
        csrf = m_csrf.group(1)
      m_port = re.search(r"--(?:http_server_)?port[=\s]+(\d+)", line)
      if m_port:
        ls_addr = f"localhost:{m_port.group(1)}"
      if csrf:
        break
  except Exception:
    pass
  return ls_addr, csrf


def _read_exact(stream: Any, n: int) -> bytes:
  """Reads exactly `n` bytes from `stream` or returns fewer on EOF."""
  buf = bytearray()
  while len(buf) < n:
    chunk = stream.read(n - len(buf))
    if not chunk:
      break
    buf.extend(chunk)
  return bytes(buf)


def _fetch_jetbox_summaries_burst(
    clean_addr: str, token: str
) -> dict[str, Any] | None:
  """Reads the initial snapshot burst from `JetboxSubscribeToSummaries`.

  `GetAllCascadeTrajectories` builds summaries without `WithNotFullyIdle`, so
  conversations whose main turn is `CASCADE_RUN_STATUS_IDLE` while waiting on a
  subagent, background `run_command` task, or `schedule` timer omit
  `notFullyIdle`. `JetboxSubscribeToSummaries` streams from
  `jetboxSummariesStore`, which includes `notFullyIdle: true` for all such
  conversations (matching the Jetski sidebar spinner).
  """
  url = (
      f"http://{clean_addr}"
      "/exa.language_server_pb.LanguageServerService/JetboxSubscribeToSummaries"
  )
  body = b"{}"
  envelope = struct.pack(">BI", 0, len(body)) + body
  req = urllib.request.Request(
      url,
      data=envelope,
      headers={
          "Content-Type": "application/connect+json",
          "x-codeium-csrf-token": token,
      },
      method="POST",
  )
  merged: dict[str, Any] = {}
  packets = 0
  with urllib.request.urlopen(req, timeout=2.0) as resp:
    raw_sock = getattr(getattr(getattr(resp, "fp", None), "raw", None), "_sock", None)
    while True:
      try:
        header = _read_exact(resp, 5)
      except (TimeoutError, socket.timeout):
        break
      if len(header) < 5:
        break
      flags, length = struct.unpack(">BI", header)
      if flags & 0x02:
        # End-of-stream trailer frame
        break
      if raw_sock is not None:
        raw_sock.settimeout(1.0)
      raw_payload = _read_exact(resp, length)
      if len(raw_payload) < length:
        break
      payload = json.loads(raw_payload.decode("utf-8"))
      if isinstance(payload, dict):
        updates = payload.get("updates")
        if isinstance(updates, dict):
          merged.update(updates)
      packets += 1
      if raw_sock is not None:
        # Use a short idle timeout to drain any remaining initial snapshot
        # packets (sent in batches of 100) without waiting for future events.
        raw_sock.settimeout(0.04)
      elif len(merged) < 100:
        break
  return merged if packets > 0 else None


def fetch_trajectories(
    ls_address: str = "", csrf_token: str = ""
) -> dict[str, Any] | None:
  """Fetches `trajectorySummaries` from the local Language Server.

  Prefers `JetboxSubscribeToSummaries` (which includes `notFullyIdle` for
  conversations waiting on subagents, timers, or background tasks) and falls
  back to `GetAllCascadeTrajectories`. Automatically resolves and caches
  `(ls_address, csrf_token)` if not provided or if a cached token became stale
  after a server restart.
  """
  candidates: list[tuple[str, str]] = []
  env_addr = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "")
  env_csrf = os.environ.get("ANTIGRAVITY_CSRF_TOKEN", "")
  if ls_address and csrf_token:
    candidates.append((ls_address, csrf_token))
  if env_addr and env_csrf:
    candidates.append((env_addr, env_csrf))
  cached_addr, cached_csrf = _read_cached_conn()
  if cached_addr and cached_csrf:
    candidates.append((cached_addr, cached_csrf))

  def _try_call(addr: str, token: str) -> dict[str, Any] | None:
    clean_addr = addr.removeprefix("http://").removeprefix("https://")
    try:
      stream_summaries = _fetch_jetbox_summaries_burst(clean_addr, token)
      if stream_summaries is not None:
        return stream_summaries
    except Exception:
      pass

    url = (
        f"http://{clean_addr}"
        "/exa.language_server_pb.LanguageServerService/GetAllCascadeTrajectories"
    )
    req = urllib.request.Request(
        url,
        data=b'{"excludeSubtrajectories": true}',
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=2.0) as resp:
      payload = json.loads(resp.read().decode("utf-8"))
    if isinstance(payload, dict):
      return payload.get("trajectorySummaries") or {}
    return {}

  for addr, token in candidates:
    try:
      res = _try_call(addr, token)
      if res is not None:
        _write_cached_conn(addr, token)
        return res
    except Exception:
      continue

  # Full discovery fallback (needed inside hooks where LS does not export
  # ANTIGRAVITY_CSRF_TOKEN in its own environment).
  base_addr = env_addr or cached_addr or DEFAULT_LS_ADDRESS
  idx_csrf = _discover_from_http_index(base_addr)
  if idx_csrf:
    try:
      res = _try_call(base_addr, idx_csrf)
      if res is not None:
        _write_cached_conn(base_addr, idx_csrf)
        return res
    except Exception:
      pass

  proc_addr, proc_csrf = _discover_from_proc()
  if proc_csrf:
    addr = proc_addr or base_addr
    try:
      res = _try_call(addr, proc_csrf)
      if res is not None:
        _write_cached_conn(addr, proc_csrf)
        return res
    except Exception:
      pass

  ps_addr, ps_csrf = _discover_from_ps()
  if ps_csrf:
    addr = ps_addr or base_addr
    try:
      res = _try_call(addr, ps_csrf)
      if res is not None:
        _write_cached_conn(addr, ps_csrf)
        return res
    except Exception:
      pass

  return None


def _parse_iso_timestamp(ts: str) -> float:
  if not ts or not isinstance(ts, str):
    return 0.0
  try:
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
  except ValueError:
    return 0.0


def _is_visible_top_level(summary: dict[str, Any]) -> bool:
  annotations = summary.get("annotations") or {}
  if annotations.get("archived"):
    return False
  meta = summary.get("trajectoryMetadata") or {}
  if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
    return False
  source_meta = meta.get("sourceMetadata") or {}
  if source_meta.get("tool"):
    return False
  return True


def _is_handed_off_predecessor(summary: dict[str, Any]) -> bool:
  annotations = summary.get("annotations") or {}
  for candidate in (annotations.get("title"), summary.get("summary")):
    if isinstance(candidate, str) and PREDECESSOR_TITLE_RE.match(candidate):
      return True
  return False


def _mark_predecessors_read(summaries: dict[str, Any]) -> None:
  """Stamps `lastUserViewTime` on idle handed-off predecessor conversations.

  When a conversation splits via `handoff`, its final turn finishes a few
  seconds after the continuation is spawned, leaving `lastModifiedTime` newer
  than `lastUserViewTime`. Updating `lastUserViewTime` to `lastModifiedTime + 1ms`
  clears Jetski's sidebar unread dot on intermediate chats without disturbing
  `max_luvt` of the user's active conversation.
  """
  addr, token = _read_cached_conn()
  if not addr or not token:
    return
  clean_addr = addr.removeprefix("http://").removeprefix("https://")
  url = (
      f"http://{clean_addr}"
      "/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"
  )

  for cid, summary in summaries.items():
    if not isinstance(summary, dict) or not _is_visible_top_level(summary):
      continue
    if not _is_handed_off_predecessor(summary):
      continue
    if (
        summary.get("status") == "CASCADE_RUN_STATUS_RUNNING"
        or summary.get("notFullyIdle")
        or summary.get("waitingSteps")
    ):
      continue
    annotations = summary.get("annotations") or {}
    lmt = _parse_iso_timestamp(summary.get("lastModifiedTime", ""))
    luvt = _parse_iso_timestamp(annotations.get("lastUserViewTime", ""))
    if lmt <= 0.0 or (lmt <= luvt and not annotations.get("markedAsUnread")):
      continue
    stamp_iso = (
        datetime.fromtimestamp(lmt + 0.001, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    payload = json.dumps({
        "cascadeIds": [cid],
        "annotations": {
            "lastUserViewTime": stamp_iso,
            "markedAsUnread": False,
        },
        "mergeAnnotations": True,
    }).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-codeium-csrf-token": token,
        },
        method="POST",
    )
    try:
      with urllib.request.urlopen(req, timeout=1.5):
        pass
    except Exception:
      pass


def resolve_currently_viewing_cid(
    summaries: dict[str, Any],
    annotations_dir: str = DEFAULT_ANNOTATIONS_DIR,
    state_file: str = "",
) -> tuple[str, dict[str, float]]:
  """Identifies the currently open conversation and tracks its viewed step time.

  When the user switches from `prev` to `curr`, `conversationViewingMiddleware`
  updates `lastUserViewTime` on `[prev, curr]` in a single RPC and the server
  writes `prev.pbtxt` before `curr.pbtxt`. Breaking `lastUserViewTime` ties via
  `.pbtxt` `st_mtime_ns` and caching the winner for that `max_luvt` timestamp
  deterministically identifies `curr` even if `prev.pbtxt` is later touched by
  background title updates.
  """
  max_luvt = 0.0
  for summary in summaries.values():
    if (
        not isinstance(summary, dict)
        or not _is_visible_top_level(summary)
        or _is_handed_off_predecessor(summary)
    ):
      continue
    luvt = _parse_iso_timestamp(
        (summary.get("annotations") or {}).get("lastUserViewTime", "")
    )
    if luvt > max_luvt:
      max_luvt = luvt

  if max_luvt <= 0.0:
    return "", {}

  cache_path = state_file or _view_state_path()
  cached_max_luvt = 0.0
  cached_cid = ""
  viewed_lmt: dict[str, float] = {}
  try:
    with open(cache_path, "r", encoding="utf-8") as f:
      cached = json.load(f)
    if isinstance(cached, dict):
      cached_max_luvt = float(cached.get("max_luvt") or 0.0)
      cached_cid = str(cached.get("current_cid") or "")
      raw_viewed = cached.get("viewed_lmt")
      if isinstance(raw_viewed, dict):
        viewed_lmt = {
            str(k): float(v)
            for k, v in raw_viewed.items()
            if isinstance(v, (int, float))
        }
  except (OSError, ValueError, TypeError):
    pass

  if (
      abs(max_luvt - cached_max_luvt) <= 1e-6
      and cached_cid
      and cached_cid in summaries
      and not _is_handed_off_predecessor(summaries[cached_cid])
  ):
    current_cid = cached_cid
  else:
    best_key = (-1.0, -1)
    current_cid = ""
    for cid, summary in summaries.items():
      if (
          not isinstance(summary, dict)
          or not _is_visible_top_level(summary)
          or _is_handed_off_predecessor(summary)
      ):
        continue
      luvt = _parse_iso_timestamp(
          (summary.get("annotations") or {}).get("lastUserViewTime", "")
      )
      if max_luvt - luvt <= 0.005:
        pbtxt = os.path.join(annotations_dir, f"{cid}.pbtxt")
        try:
          mtime_ns = os.stat(pbtxt).st_mtime_ns
        except OSError:
          mtime_ns = 0
        key = (luvt, mtime_ns)
        if key > best_key:
          best_key = key
          current_cid = cid

  dirty = (
      abs(max_luvt - cached_max_luvt) > 1e-6 or current_cid != cached_cid
  )
  if current_cid and isinstance(summaries.get(current_cid), dict):
    cur_lmt = _parse_iso_timestamp(
        summaries[current_cid].get("lastModifiedTime", "")
    )
    if cur_lmt > viewed_lmt.get(current_cid, 0.0):
      viewed_lmt[current_cid] = cur_lmt
      dirty = True

  if dirty:
    if len(viewed_lmt) > 64:
      sorted_items = sorted(viewed_lmt.items(), key=lambda x: x[1], reverse=True)
      viewed_lmt = dict(sorted_items[:64])
    tmp_path = f"{cache_path}.tmp.{os.getpid()}"
    try:
      with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "max_luvt": max_luvt,
                "current_cid": current_cid,
                "viewed_lmt": viewed_lmt,
            },
            f,
        )
      os.replace(tmp_path, cache_path)
    except OSError:
      try:
        os.unlink(tmp_path)
      except OSError:
        pass

  return current_cid, viewed_lmt


def is_conversation_unread(
    cid: str,
    summary: dict[str, Any],
    currently_viewing_cid: str = "",
    viewed_lmt: dict[str, float] | None = None,
) -> bool:
  """Returns True if an idle conversation has unread updates."""
  if _is_handed_off_predecessor(summary):
    return False
  annotations = summary.get("annotations") or {}
  if annotations.get("markedAsUnread"):
    return True
  if currently_viewing_cid and cid == currently_viewing_cid:
    return False
  lmt = _parse_iso_timestamp(summary.get("lastModifiedTime", ""))
  if lmt <= 0.0:
    return False
  luvt = _parse_iso_timestamp(annotations.get("lastUserViewTime", ""))
  effective_view = max(luvt, (viewed_lmt or {}).get(cid, 0.0))
  return lmt > effective_view


def compute_project_activity(
    summaries: dict[str, Any],
    known_projects: dict[str, tuple[str, dict[str, Any]]],
    folder_to_project: list[tuple[str, str]],
    force_running_cid: str = "",
    force_running_project_id: str = "",
    force_running_workspaces: list[str] | None = None,
    currently_viewing_cid: str = "",
    viewed_lmt: dict[str, float] | None = None,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
  """Computes `(running_counts, blocked_counts, unread_counts)` per project ID."""
  running_counts: dict[str, int] = defaultdict(int)
  blocked_counts: dict[str, int] = defaultdict(int)
  unread_counts: dict[str, int] = defaultdict(int)
  seen_forced = False

  for cid, summary in summaries.items():
    if not isinstance(summary, dict) or not _is_visible_top_level(summary):
      continue

    meta = summary.get("trajectoryMetadata") or {}
    pid = meta.get("projectId") or ""
    if not pid or pid not in known_projects:
      ws_uris: list[str] = []
      for w in summary.get("workspaces") or []:
        if isinstance(w, dict) and w.get("workspaceFolderAbsoluteUri"):
          ws_uris.append(w["workspaceFolderAbsoluteUri"])
      for u in meta.get("workspaceUris") or []:
        if isinstance(u, str):
          ws_uris.append(u)
      pid = match_workspace_to_project(ws_uris, folder_to_project)

    if not pid or pid not in known_projects:
      continue

    waiting_steps = summary.get("waitingSteps") or []
    is_running = (
        summary.get("status") == "CASCADE_RUN_STATUS_RUNNING"
        or bool(summary.get("notFullyIdle"))
    )
    if force_running_cid and cid == force_running_cid:
      seen_forced = True
      is_running = True

    if waiting_steps:
      blocked_counts[pid] += 1
    elif is_running:
      running_counts[pid] += 1
    elif is_conversation_unread(
        cid,
        summary,
        currently_viewing_cid=currently_viewing_cid,
        viewed_lmt=viewed_lmt,
    ):
      unread_counts[pid] += 1

  # If PreInvocation fired for a brand-new conversation before its summary or
  # project assignment appeared in GetAllCascadeTrajectories:
  if force_running_cid and not seen_forced:
    pid = force_running_project_id
    if (not pid or pid not in known_projects) and force_running_workspaces:
      pid = match_workspace_to_project(
          force_running_workspaces, folder_to_project
      )
    if pid and pid in known_projects:
      running_counts[pid] += 1

  return dict(running_counts), dict(blocked_counts), dict(unread_counts)


def apply_project_badges(
    projects_dir: str,
    running_counts: dict[str, int],
    blocked_counts: dict[str, int],
    unread_counts: dict[str, int] | None = None,
) -> list[tuple[str, str, str]]:
  """Atomically updates project JSON files whose badge changed.

  Returns a list of `(project_id, old_name, new_name)` for modified projects.
  """
  known_projects, _ = load_projects(projects_dir)
  changes: list[tuple[str, str, str]] = []
  unreads = unread_counts or {}

  for pid, (pfile, _) in known_projects.items():
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        fresh_data = json.load(f)
      if not isinstance(fresh_data, dict):
        continue
      old_name = fresh_data.get("name", "")
      if not isinstance(old_name, str) or not old_name:
        continue
      new_name = format_project_name(
          old_name,
          running_count=running_counts.get(pid, 0),
          blocked_count=blocked_counts.get(pid, 0),
          unread_count=unreads.get(pid, 0),
      )
      if new_name == old_name:
        continue
      fresh_data["name"] = new_name
      tmp_path = f"{pfile}.tmp.{os.getpid()}"
      with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(fresh_data, f, indent=2, ensure_ascii=False)
        f.write("\n")
      os.replace(tmp_path, pfile)
      changes.append((pid, old_name, new_name))
    except (OSError, ValueError):
      continue

  return changes


def sync_once(
    projects_dir: str = DEFAULT_PROJECTS_DIR,
    force_running_cid: str = "",
    force_running_project_id: str = "",
    force_running_workspaces: list[str] | None = None,
) -> tuple[bool, int, list[tuple[str, str, str]]]:
  """Performs a single locked synchronization of project activity badges.

  Returns:
    `(rpc_ok, total_tracked_chats, modified_projects)`
  """
  os.makedirs(os.path.dirname(_sync_lock_path()), exist_ok=True)
  with open(_sync_lock_path(), "w", encoding="utf-8") as lock_f:
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    try:
      summaries = fetch_trajectories()
      if summaries is None:
        return False, 0, []
      _mark_predecessors_read(summaries)
      known_projects, folder_to_project = load_projects(projects_dir)
      currently_viewing_cid, viewed_lmt = resolve_currently_viewing_cid(
          summaries
      )
      running_counts, blocked_counts, unread_counts = compute_project_activity(
          summaries,
          known_projects,
          folder_to_project,
          force_running_cid=force_running_cid,
          force_running_project_id=force_running_project_id,
          force_running_workspaces=force_running_workspaces,
          currently_viewing_cid=currently_viewing_cid,
          viewed_lmt=viewed_lmt,
      )
      changes = apply_project_badges(
          projects_dir, running_counts, blocked_counts, unread_counts
      )
      total_tracked = (
          sum(running_counts.values())
          + sum(blocked_counts.values())
          + sum(unread_counts.values())
      )
      return True, total_tracked, changes
    finally:
      fcntl.flock(lock_f, fcntl.LOCK_UN)


def clear_all_badges(
    projects_dir: str = DEFAULT_PROJECTS_DIR,
) -> list[tuple[str, str, str]]:
  """Strips all activity badges from all projects in `projects_dir`."""
  with open(_sync_lock_path(), "w", encoding="utf-8") as lock_f:
    fcntl.flock(lock_f, fcntl.LOCK_EX)
    try:
      return apply_project_badges(projects_dir, {}, {}, {})
    finally:
      fcntl.flock(lock_f, fcntl.LOCK_UN)


def run_watcher_loop(projects_dir: str = DEFAULT_PROJECTS_DIR) -> None:
  """Runs a singleton background polling loop until all projects are idle and read."""
  lock_f = open(_watcher_lock_path(), "w", encoding="utf-8")
  try:
    fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)
  except BlockingIOError:
    lock_f.close()
    return

  try:
    lock_f.write(str(os.getpid()))
    lock_f.flush()
    start_ts = time.monotonic()
    idle_streak = 0
    rpc_fail_streak = 0

    while (time.monotonic() - start_ts) < MAX_WATCH_DURATION_SEC:
      time.sleep(WATCH_POLL_INTERVAL_SEC)
      rpc_ok, total_tracked, _ = sync_once(projects_dir=projects_dir)
      if not rpc_ok:
        rpc_fail_streak += 1
        if rpc_fail_streak >= 2:
          # Language Server stopped or became unreachable; leave clean names
          clear_all_badges(projects_dir=projects_dir)
          break
        continue

      rpc_fail_streak = 0
      if total_tracked == 0:
        idle_streak += 1
        if idle_streak >= IDLE_POLLS_BEFORE_EXIT:
          break
      else:
        idle_streak = 0
  finally:
    try:
      fcntl.flock(lock_f, fcntl.LOCK_UN)
    except OSError:
      pass
    lock_f.close()


def spawn_detached_trigger(
    event: str,
    cid: str,
    project_id: str,
    workspace_paths: list[str],
    projects_dir: str,
) -> None:
  """Spawns `--bg-trigger` in a detached session without holding hook stdio."""
  cmd = [
      sys.executable,
      os.path.abspath(__file__),
      "--bg-trigger",
      "--event",
      event,
      "--projects-dir",
      projects_dir,
  ]
  if cid:
    cmd.extend(["--cid", cid])
  if project_id:
    cmd.extend(["--project-id", project_id])
  if workspace_paths:
    cmd.extend(["--workspaces-json", json.dumps(workspace_paths)])

  try:
    subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
  except OSError:
    pass


def handle_hook_invocation(event: str, projects_dir: str) -> None:
  """Handles `PreInvocation` or `Stop` hook execution in <10ms."""
  raw_stdin = ""
  try:
    if not sys.stdin.isatty():
      raw_stdin = sys.stdin.read()
  except OSError:
    pass

  payload: dict[str, Any] = {}
  if raw_stdin.strip():
    try:
      parsed = json.loads(raw_stdin)
      if isinstance(parsed, dict):
        payload = parsed
    except ValueError:
      pass

  cid = (
      payload.get("conversationId")
      or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
      or ""
  )
  project_id = os.environ.get("ANTIGRAVITY_PROJECT_ID", "")
  workspace_paths = payload.get("workspacePaths") or []
  if not isinstance(workspace_paths, list):
    workspace_paths = []

  # Immediately emit `{}` and flush so the hook never blocks the agent turn
  sys.stdout.write("{}\n")
  sys.stdout.flush()

  spawn_detached_trigger(
      event=event,
      cid=cid,
      project_id=project_id,
      workspace_paths=workspace_paths,
      projects_dir=projects_dir,
  )


def handle_bg_trigger(
    event: str,
    cid: str,
    project_id: str,
    workspace_paths: list[str],
    projects_dir: str,
) -> None:
  """Runs inside the detached background child spawned by the hook."""
  if event == "PreInvocation":
    sync_once(
        projects_dir=projects_dir,
        force_running_cid=cid,
        force_running_project_id=project_id,
        force_running_workspaces=workspace_paths,
    )
    run_watcher_loop(projects_dir=projects_dir)
  elif event == "Stop":
    # Wait briefly for the Language Server to transition the stopping
    # conversation from CASCADE_RUN_STATUS_RUNNING to CASCADE_RUN_STATUS_IDLE
    time.sleep(STOP_SETTLE_DELAY_SEC)
    _, total_tracked, _ = sync_once(projects_dir=projects_dir)
    if total_tracked > 0:
      run_watcher_loop(projects_dir=projects_dir)


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Sync active conversation badges into Jetski project names."
  )
  parser.add_argument(
      "--event",
      choices=["PreInvocation", "Stop"],
      help="Run in hook mode for the specified lifecycle event.",
  )
  parser.add_argument(
      "--bg-trigger",
      action="store_true",
      help="Internal detached worker mode spawned by hook invocation.",
  )
  parser.add_argument(
      "--sync-once",
      action="store_true",
      help="Run a single synchronous badge update and exit.",
  )
  parser.add_argument(
      "--watch",
      action="store_true",
      help="Run the singleton background watcher until all projects are idle.",
  )
  parser.add_argument(
      "--clear",
      action="store_true",
      help="Remove all activity badges from all project JSON files.",
  )
  parser.add_argument(
      "--status",
      action="store_true",
      help="Print current running/blocked/unread conversation counts per project.",
  )
  parser.add_argument("--cid", default="", help="Conversation ID context.")
  parser.add_argument("--project-id", default="", help="Project ID hint.")
  parser.add_argument(
      "--workspaces-json", default="", help="JSON list of workspace paths."
  )
  parser.add_argument(
      "--projects-dir",
      default=DEFAULT_PROJECTS_DIR,
      help="Path to ~/.gemini/config/projects directory.",
  )
  args = parser.parse_args()

  workspaces: list[str] = []
  if args.workspaces_json:
    try:
      parsed_ws = json.loads(args.workspaces_json)
      if isinstance(parsed_ws, list):
        workspaces = [str(x) for x in parsed_ws]
    except ValueError:
      pass

  if args.bg_trigger:
    handle_bg_trigger(
        event=args.event or "PreInvocation",
        cid=args.cid,
        project_id=args.project_id,
        workspace_paths=workspaces,
        projects_dir=args.projects_dir,
    )
    return

  if args.event:
    handle_hook_invocation(event=args.event, projects_dir=args.projects_dir)
    return

  if args.clear:
    changes = clear_all_badges(projects_dir=args.projects_dir)
    for pid, old_name, new_name in changes:
      print(f"{pid}: {old_name!r} -> {new_name!r}")
    return

  if args.status:
    summaries = fetch_trajectories()
    if summaries is None:
      print("ERROR: Could not connect to Language Server.", file=sys.stderr)
      sys.exit(1)
    known_projects, folder_to_project = load_projects(args.projects_dir)
    currently_viewing_cid, viewed_lmt = resolve_currently_viewing_cid(summaries)
    running_counts, blocked_counts, unread_counts = compute_project_activity(
        summaries,
        known_projects,
        folder_to_project,
        currently_viewing_cid=currently_viewing_cid,
        viewed_lmt=viewed_lmt,
    )
    for pid, (_, pdata) in known_projects.items():
      base = strip_activity_badge(pdata.get("name", ""))
      r = running_counts.get(pid, 0)
      b = blocked_counts.get(pid, 0)
      u = unread_counts.get(pid, 0)
      print(f"{pid}  {base:<24}  running={r}  blocked={b}  unread={u}")
    return

  if args.sync_once:
    rpc_ok, total_tracked, changes = sync_once(
        projects_dir=args.projects_dir,
        force_running_cid=args.cid,
        force_running_project_id=args.project_id,
        force_running_workspaces=workspaces,
    )
    if not rpc_ok:
      print("ERROR: Could not connect to Language Server.", file=sys.stderr)
      sys.exit(1)
    for pid, old_name, new_name in changes:
      print(f"{pid}: {old_name!r} -> {new_name!r}")
    print(f"Total tracked conversations: {total_tracked}")
    return

  if args.watch:
    run_watcher_loop(projects_dir=args.projects_dir)
    return

  # Default when invoked without flags as a hook: treat as PreInvocation
  handle_hook_invocation(event="PreInvocation", projects_dir=args.projects_dir)


if __name__ == "__main__":
  main()
