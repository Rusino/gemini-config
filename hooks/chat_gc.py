#!/usr/bin/env python3
"""Three-tier conversation garbage collector and FTS5 cold archive."""

from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import os
import re
import shutil
import sqlite3
import subprocess
from typing import Any, Callable

import chat_lifecycle as cl

UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
HEX_REF_RE = re.compile(
    r"\b([0-9a-f]{8}(?:-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})?)\b",
    re.IGNORECASE,
)
PBTXT_ARCHIVED_RE = re.compile(r"\barchived\s*:\s*true\b")
PBTXT_TITLE_RE = re.compile(r'title\s*:\s*"((?:[^"\\]|\\.)*)"')

DEFAULT_GC_CONFIG = {
    "scratch_gc_days": 7,
    "task_log_gc_days": 30,
    "trash_delete_days": 7,
    "cold_archive_days": 90,
}
DEFAULT_TRIAGE_DELETE_PROJECTS = {
    "skia-g3-roll-check": 30,
}
DEFAULT_GC_LOG_FILE = os.path.expanduser("~/.gemini/config/logs/chat_gc.jsonl")
DEFAULT_ROADMAPS_DIR = os.path.expanduser("~/.gemini/config/roadmaps")

BUILD_SUBDIRS = (
    "out",
    "bazel-bin",
    "bazel-out",
    "bazel-testlogs",
    ".dart_tool",
    "node_modules",
    "__pycache__",
    "target",
)
DELIVERABLE_EXTS = (".md", ".html", ".pdf", ".png", ".svg")
COLD_COPY_EXTS = (".md", ".html", ".pdf", ".png", ".svg", ".url.json")
ACTIVE_STATUSES = frozenset({
    "CASCADE_RUN_STATUS_RUNNING",
    "CASCADE_RUN_STATUS_BUSY",
    "CASCADE_RUN_STATUS_CANCELING",
    "2",
    "3",
    "4",
})


def load_retention_config(path: str = "") -> dict[str, Any]:
  """Loads count-based retention and 3-tier GC settings from JSON."""
  cfg_path = path or cl.DEFAULT_PROJECT_RETENTION_FILE
  keep: dict[str, int] = dict(cl.DEFAULT_AUTO_ARCHIVE_RETENTION)
  triage_del: dict[str, int] = dict(DEFAULT_TRIAGE_DELETE_PROJECTS)
  gc_cfg: dict[str, int] = dict(DEFAULT_GC_CONFIG)

  if os.path.isfile(cfg_path):
    try:
      with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
      if isinstance(data, dict):
        # Support both legacy flat {"proj": N} and structured {"keep": {...}}
        for k, v in data.items():
          if isinstance(v, int) and k not in ("keep", "gc", "triage_hard_delete_projects"):
            keep[k] = v
        if isinstance(data.get("keep"), dict):
          for k, v in data["keep"].items():
            if isinstance(v, int):
              keep[k] = v
        if isinstance(data.get("triage_hard_delete_projects"), dict):
          for k, v in data["triage_hard_delete_projects"].items():
            if isinstance(v, int):
              triage_del[k] = v
        if isinstance(data.get("gc"), dict):
          for k, v in data["gc"].items():
            if isinstance(v, int) and k in gc_cfg:
              gc_cfg[k] = v
    except Exception:
      pass

  return {
      "keep": keep,
      "triage_hard_delete_projects": triage_del,
      "gc": gc_cfg,
  }


def _parse_iso(ts: str) -> datetime | None:
  if not ts:
    return None
  try:
    norm = ts.strip().replace(" ", "T")
    if norm.endswith("Z"):
      norm = norm[:-1] + "+00:00"
    dt = datetime.fromisoformat(norm)
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    return dt
  except ValueError:
    return None


def _dir_size_bytes(path: str) -> int:
  total = 0
  if os.path.isfile(path) and not os.path.islink(path):
    try:
      return os.path.getsize(path)
    except OSError:
      return 0
  for root, _, files in os.walk(path, followlinks=False):
    for fn in files:
      fp = os.path.join(root, fn)
      if not os.path.islink(fp):
        try:
          total += os.path.getsize(fp)
        except OSError:
          pass
  return total


def _scan_proc_active_cids() -> set[str]:
  """Finds conversation UUIDs referenced by live host processes or open FDs."""
  active: set[str] = set()
  for env_k in ("ANTIGRAVITY_CONVERSATION_ID", "CONVERSATION_ID"):
    val = os.environ.get(env_k, "").strip().lower()
    if UUID_RE.match(val):
      active.add(val)

  uid = os.getuid()
  if not os.path.isdir("/proc"):
    return active

  uuid_in_path = re.compile(
      r"(?:brain|conversations)/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
      re.IGNORECASE,
  )
  for entry in os.listdir("/proc"):
    if not entry.isdigit():
      continue
    proc_dir = os.path.join("/proc", entry)
    try:
      if os.stat(proc_dir).st_uid != uid:
        continue
    except OSError:
      continue
    for sub in ("cmdline",):
      try:
        with open(os.path.join(proc_dir, sub), "rb") as f:
          raw = f.read(16384).decode("utf-8", errors="ignore")
        for m in uuid_in_path.finditer(raw):
          active.add(m.group(1).lower())
      except OSError:
        pass
    try:
      cwd_target = os.readlink(os.path.join(proc_dir, "cwd"))
      for m in uuid_in_path.finditer(cwd_target):
        active.add(m.group(1).lower())
    except OSError:
      pass
    fd_dir = os.path.join(proc_dir, "fd")
    try:
      for fd in os.listdir(fd_dir)[:128]:
        try:
          target = os.readlink(os.path.join(fd_dir, fd))
          for m in uuid_in_path.finditer(target):
            active.add(m.group(1).lower())
        except OSError:
          pass
    except OSError:
      pass
  return active


def _collect_roadmap_refs(
    roadmaps_dir: str, all_known_cids: set[str]
) -> tuple[set[str], set[str]]:
  """Returns (open_roadmap_cids, all_roadmap_cids) matched by UUID or 8-hex prefix."""
  open_tokens: set[str] = set()
  all_tokens: set[str] = set()
  if os.path.isdir(roadmaps_dir):
    for entry in sorted(os.listdir(roadmaps_dir)):
      rm_file = os.path.join(roadmaps_dir, entry, "roadmap.json")
      if not os.path.isfile(rm_file):
        continue
      try:
        with open(rm_file, "r", encoding="utf-8") as f:
          data = json.load(f)
      except Exception:
        continue
      items = data.get("items") if isinstance(data, dict) else None
      if not isinstance(items, list):
        continue
      for item in items:
        if not isinstance(item, dict):
          continue
        status = str(item.get("status") or "").strip().lower()
        blob = json.dumps(item, ensure_ascii=False)
        refs = {m.group(1).lower() for m in HEX_REF_RE.finditer(blob)}
        all_tokens.update(refs)
        if status not in ("done", "dropped"):
          open_tokens.update(refs)

  def _expand(tokens: set[str]) -> set[str]:
    matched: set[str] = set()
    prefixes = [t for t in tokens if len(t) == 8]
    exact = {t for t in tokens if len(t) == 36}
    for cid in all_known_cids:
      low = cid.lower()
      if low in exact or any(low.startswith(p) for p in prefixes):
        matched.add(cid)
    return matched

  return _expand(open_tokens), _expand(all_tokens)


def _scan_transcript_graph_in_dirs(
    app_dirs: list[str],
) -> tuple[dict[str, str], dict[str, list[str]], dict[str, str]]:
  parent_of: dict[str, str] = {}
  children_of: dict[str, list[str]] = {}
  created_at_of: dict[str, str] = {}
  for app_dir in app_dirs:
    brain_dir = os.path.join(app_dir, "brain")
    if not os.path.isdir(brain_dir):
      continue
    for cid in sorted(os.listdir(brain_dir)):
      if not UUID_RE.match(cid):
        continue
      tr_path = os.path.join(brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
      if os.path.isfile(tr_path):
        try:
          with open(tr_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
          if first_line:
            obj = json.loads(first_line)
            if isinstance(obj, dict) and obj.get("type") == "USER_INPUT":
              ts = str(obj.get("created_at") or "")
              if ts and cid not in created_at_of:
                created_at_of[cid] = ts
              m = cl.HANDOFF_PARENT_RE.search(str(obj.get("content") or ""))
              if m and cid not in parent_of:
                pid = m.group(1).strip()
                if UUID_RE.match(pid):
                  parent_of[cid] = pid
                  children_of.setdefault(pid, []).append(cid)
        except Exception:
          pass
      guard_path = os.path.join(brain_dir, cid, "scratch", ".context_guard_state.json")
      if os.path.isfile(guard_path):
        try:
          with open(guard_path, "r", encoding="utf-8") as f:
            gdata = json.load(f)
          if isinstance(gdata, dict):
            owner = str(gdata.get("owner") or "").strip()
            to_cid = str(gdata.get("handed_off_to") or "").strip()
            kind = str(gdata.get("handoff_kind") or "handoff")
            # Ignore foreign or self-referential guard files copied across scratch/ dirs
            if (
                (not owner or owner == cid)
                and to_cid
                and to_cid != cid
                and kind != "next-task"
                and to_cid not in parent_of
            ):
              if UUID_RE.match(to_cid):
                parent_of[to_cid] = cid
                children_of.setdefault(cid, []).append(to_cid)
        except Exception:
          pass
  return parent_of, children_of, created_at_of


def _build_components(
    all_cids: set[str], parent_of: dict[str, str], children_of: dict[str, list[str]]
) -> dict[str, list[str]]:
  comp_of: dict[str, list[str]] = {}
  visited: set[str] = set()
  for cid in sorted(all_cids):
    if cid in visited:
      continue
    stack = [cid]
    comp: list[str] = []
    # Bounded traversal over finite conversation set
    max_steps = len(all_cids) + 1
    steps = 0
    while stack and steps < max_steps:
      steps += 1
      cur = stack.pop()
      if cur in visited:
        continue
      visited.add(cur)
      comp.append(cur)
      p = parent_of.get(cur)
      if p and p in all_cids and p not in visited:
        stack.append(p)
      for ch in children_of.get(cur, ()):
        if ch in all_cids and ch not in visited:
          stack.append(ch)
    comp.sort()
    for m in comp:
      comp_of[m] = comp
  return comp_of


def _has_root_deliverables(brain_cid_dir: str) -> bool:
  if not os.path.isdir(brain_cid_dir):
    return False
  try:
    for fn in os.listdir(brain_cid_dir):
      if fn.startswith(".") or fn.endswith(".metadata.json"):
        continue
      fp = os.path.join(brain_cid_dir, fn)
      if os.path.isfile(fp) and fn.lower().endswith(DELIVERABLE_EXTS):
        return True
  except OSError:
    pass
  return False


def _transcript_has_agent_reply(brain_cid_dir: str) -> bool:
  for name in ("transcript.jsonl", "transcript_full.jsonl"):
    p = os.path.join(brain_cid_dir, ".system_generated", "logs", name)
    if os.path.isfile(p):
      try:
        with open(p, "r", encoding="utf-8") as f:
          for _ in range(64):
            line = f.readline()
            if not line:
              break
            if '"PLANNER_RESPONSE"' in line:
              obj = json.loads(line)
              if str(obj.get("content") or "").strip():
                return True
      except Exception:
        return True
  return False


def _prune_build_subdirs(repo_dir: str, apply: bool) -> int:
  reclaimed = 0
  for sub in BUILD_SUBDIRS:
    target = os.path.join(repo_dir, sub)
    if os.path.exists(target) and not os.path.islink(target):
      reclaimed += _dir_size_bytes(target)
      if apply:
        shutil.rmtree(target, ignore_errors=True)
  return reclaimed


def _clean_git_dir(
    repo_dir: str, patch_out_dir: str, apply: bool
) -> tuple[int, str]:
  """Cleans a git clone or linked worktree safely without losing local work."""
  dot_git = os.path.join(repo_dir, ".git")
  try:
    st = subprocess.run(
        ["git", "-C", repo_dir, "status", "--porcelain", "-uno"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    has_dirty_tracked = st.returncode != 0 or bool(st.stdout.strip())
  except Exception:
    has_dirty_tracked = True

  if has_dirty_tracked:
    return _prune_build_subdirs(repo_dir, apply), repo_dir

  # Linked git worktree: branches live in the parent repo's .git directory
  if os.path.isfile(dot_git):
    common_git_dir = ""
    try:
      with open(dot_git, "r", encoding="utf-8") as f:
        line = f.readline().strip()
      if line.startswith("gitdir:"):
        wt_git_dir = line.split(":", 1)[1].strip()
        if not os.path.isabs(wt_git_dir):
          wt_git_dir = os.path.normpath(os.path.join(repo_dir, wt_git_dir))
        commondir_file = os.path.join(wt_git_dir, "commondir")
        if os.path.isfile(commondir_file):
          with open(commondir_file, "r", encoding="utf-8") as cf:
            rel_common = cf.read().strip()
          common_git_dir = os.path.normpath(os.path.join(wt_git_dir, rel_common))
    except OSError:
      pass
    sz = _dir_size_bytes(repo_dir)
    if apply:
      shutil.rmtree(repo_dir, ignore_errors=True)
      if common_git_dir and os.path.isdir(common_git_dir):
        subprocess.run(
            ["git", f"--git-dir={common_git_dir}", "worktree", "prune"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    return sz, ""

  # Standalone git clone: check for unpushed local commits before removing .git
  base_ref = ""
  for cand in ("@{u}", "origin/main", "origin/master"):
    chk = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--verify", cand],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if chk.returncode == 0:
      base_ref = cand
      break

  ahead_cnt = 0
  if base_ref:
    cnt_res = subprocess.run(
        ["git", "-C", repo_dir, "rev-list", "--count", f"{base_ref}..HEAD"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if cnt_res.returncode == 0:
      try:
        ahead_cnt = int(cnt_res.stdout.strip() or "0")
      except ValueError:
        ahead_cnt = 1
  else:
     ahead_cnt = 1

  if ahead_cnt > 0:
    if ahead_cnt > 50 or not base_ref:
      return _prune_build_subdirs(repo_dir, apply), repo_dir
    if apply:
      fp_res = subprocess.run(
          ["git", "-C", repo_dir, "format-patch", "--stdout", f"{base_ref}..HEAD"],
          capture_output=True,
          timeout=15,
          check=False,
      )
      if fp_res.returncode != 0 or not fp_res.stdout or len(fp_res.stdout) > 5 * 1024 * 1024:
        return _prune_build_subdirs(repo_dir, apply), repo_dir
      os.makedirs(patch_out_dir, exist_ok=True)
      patch_path = os.path.join(patch_out_dir, f"{os.path.basename(repo_dir)}.patch")
      tmp_patch = patch_path + ".tmp"
      with open(tmp_patch, "wb") as pf:
        pf.write(fp_res.stdout)
      os.replace(tmp_patch, patch_path)

  sz = _dir_size_bytes(repo_dir)
  if apply:
    shutil.rmtree(repo_dir, ignore_errors=True)
  return sz, ""


def _clean_scratch_dir(brain_cid_dir: str, apply: bool) -> tuple[int, list[str]]:
  scratch_dir = os.path.join(brain_cid_dir, "scratch")
  if not os.path.isdir(scratch_dir):
    return 0, []
  reclaimed = 0
  preserved_git: list[str] = []
  patch_out_dir = os.path.join(scratch_dir, ".saved_patches")

  for name in sorted(os.listdir(scratch_dir)):
    if name in (".context_guard_state.json", ".saved_patches"):
      continue
    entry_path = os.path.join(scratch_dir, name)
    if os.path.islink(entry_path) or os.path.isfile(entry_path):
      try:
        reclaimed += os.lstat(entry_path).st_size
      except OSError:
        pass
      if apply:
        try:
          os.unlink(entry_path)
        except OSError:
          pass
      continue

    if os.path.isdir(entry_path):
      if os.path.exists(os.path.join(entry_path, ".git")):
        freed, kept = _clean_git_dir(entry_path, patch_out_dir, apply)
        reclaimed += freed
        if kept:
          preserved_git.append(kept)
        continue

      # Check 1 level deeper for nested git clones before removing directory
      nested_kept = False
      for sub in sorted(os.listdir(entry_path)):
        sub_p = os.path.join(entry_path, sub)
        if os.path.isdir(sub_p) and os.path.exists(os.path.join(sub_p, ".git")):
          freed, kept = _clean_git_dir(sub_p, patch_out_dir, apply)
          reclaimed += freed
          if kept:
            preserved_git.append(kept)
            nested_kept = True
      if not nested_kept:
        reclaimed += _dir_size_bytes(entry_path)
        if apply:
          shutil.rmtree(entry_path, ignore_errors=True)

  return reclaimed, preserved_git


def _init_cold_db(cold_dir: str) -> sqlite3.Connection:
  os.makedirs(os.path.join(cold_dir, "blobs"), exist_ok=True)
  os.makedirs(os.path.join(cold_dir, "artifacts"), exist_ok=True)
  db_path = os.path.join(cold_dir, "index.sqlite")
  conn = sqlite3.connect(db_path, timeout=10.0)
  with conn:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS cold_conversations ("
        "cascade_id TEXT PRIMARY KEY,"
        "title TEXT NOT NULL,"
        "project_id TEXT NOT NULL,"
        "created_at TEXT NOT NULL,"
        "last_modified_time TEXT NOT NULL,"
        "chain_root TEXT NOT NULL,"
        "piece_index INTEGER NOT NULL,"
        "chain_length INTEGER NOT NULL,"
        "blob_relpath TEXT NOT NULL,"
        "archived_at TEXT NOT NULL)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS cold_fts USING fts5("
        "cascade_id UNINDEXED,"
        "title,"
        "project_id,"
        "user_body,"
        "agent_body,"
        "tokenize = 'unicode61 remove_diacritics 2')"
    )
  return conn


def _compress_and_verify_blob(raw_bytes: bytes, dst_base_no_ext: str) -> str:
  zstd_bin = shutil.which("zstd")
  if zstd_bin:
    final_path = dst_base_no_ext + ".jsonl.zst"
    tmp_path = final_path + ".tmp"
    proc = subprocess.run(
        [zstd_bin, "-q", "-f", "-o", tmp_path],
        input=raw_bytes,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if proc.returncode == 0 and os.path.isfile(tmp_path):
      chk = subprocess.run(
          [zstd_bin, "-d", "-q", "-c", tmp_path],
          capture_output=True,
          timeout=30,
          check=False,
      )
      if chk.returncode == 0 and chk.stdout == raw_bytes:
        os.replace(tmp_path, final_path)
        return final_path
      try:
        os.unlink(tmp_path)
      except OSError:
        pass

  final_path = dst_base_no_ext + ".jsonl.gz"
  tmp_path = final_path + ".tmp"
  with gzip.open(tmp_path, "wb", compresslevel=6) as gf:
    gf.write(raw_bytes)
  with gzip.open(tmp_path, "rb") as gf:
    if gf.read() != raw_bytes:
      os.unlink(tmp_path)
      raise RuntimeError("Cold archive gzip round-trip verification failed")
  os.replace(tmp_path, final_path)
  return final_path


def _read_cold_blob(blob_path: str) -> bytes:
  if blob_path.endswith(".zst"):
    zstd_bin = shutil.which("zstd")
    if not zstd_bin:
      raise RuntimeError("zstd binary required to read .zst cold archive blob")
    res = subprocess.run(
        [zstd_bin, "-d", "-q", "-c", blob_path],
        capture_output=True,
        timeout=30,
        check=True,
    )
    return res.stdout
  with gzip.open(blob_path, "rb") as gf:
    return gf.read()


def _extract_transcript_bytes_and_bodies(
    cid: str, app_dirs: list[str]
) -> tuple[bytes, str, str]:
  raw_bytes = b""
  for app_dir in app_dirs:
    log_dir = os.path.join(app_dir, "brain", cid, ".system_generated", "logs")
    for fn in ("transcript_full.jsonl", "transcript.jsonl"):
      p = os.path.join(log_dir, fn)
      if os.path.isfile(p):
        with open(p, "rb") as f:
          raw_bytes = f.read()
        if raw_bytes:
          break
    if raw_bytes:
      break

  if not raw_bytes:
    try:
      steps = (
          cl.ls_rpc("GetCascadeTrajectorySteps", {"cascadeId": cid}, timeout=30).get("steps")
          or []
      )
      lines = [json.dumps(s, ensure_ascii=False) for s in steps]
      raw_bytes = ("\n".join(lines) + "\n").encode("utf-8") if lines else b"{}\n"
    except Exception:
      raw_bytes = b"{}\n"

  user_parts: list[str] = []
  agent_parts: list[str] = []
  for line in raw_bytes.decode("utf-8", errors="replace").splitlines():
    if not line.strip():
      continue
    try:
      obj = json.loads(line)
    except ValueError:
      continue
    if not isinstance(obj, dict):
      continue
    stype = str(obj.get("type") or "")
    if stype in ("USER_INPUT", "CORTEX_STEP_TYPE_USER_INPUT"):
      txt = str(
          obj.get("content")
          or (obj.get("userInput") or {}).get("userResponse")
          or ""
      )
      if txt:
        user_parts.append(txt)
    elif stype in ("PLANNER_RESPONSE", "CORTEX_STEP_TYPE_PLANNER_RESPONSE"):
      pr = obj.get("plannerResponse") or {}
      txt = str(
          obj.get("content")
          or pr.get("modifiedResponse")
          or pr.get("response")
          or ""
      )
      if txt:
        agent_parts.append(txt)

  return raw_bytes, "\n\n".join(user_parts), "\n\n".join(agent_parts)


def _purge_conversation_storage(
    cid: str,
    app_dirs: list[str],
    delete_rpc_fn: Callable[[str], None],
    call_rpc: bool = True,
) -> None:
  if call_rpc:
    try:
      delete_rpc_fn(cid)
    except Exception:
      pass
  for app_dir in app_dirs:
    conv_dir = os.path.join(app_dir, "conversations")
    for suffix in (".db", ".db-wal", ".db-shm", ".pb"):
      p = os.path.join(conv_dir, f"{cid}{suffix}")
      if os.path.exists(p):
        try:
          os.unlink(p)
        except OSError:
          pass
    ann_p = os.path.join(app_dir, cl.ANNOTATIONS_DIR_NAME, f"{cid}.pbtxt")
    if os.path.exists(ann_p):
      try:
        os.unlink(ann_p)
      except OSError:
        pass
    brain_p = os.path.join(app_dir, "brain", cid)
    if os.path.exists(brain_p):
      shutil.rmtree(brain_p, ignore_errors=True)


def run_gc(
    apply: bool = False,
    app_dirs: list[str] | None = None,
    roadmaps_dir: str | None = None,
    retention_file: str | None = None,
    cold_dir: str | None = None,
    log_file: str | None = None,
    now: datetime | None = None,
    ls_summaries: dict[str, dict] | None = None,
    delete_rpc_fn: Callable[[str], None] | None = None,
    active_cids_override: set[str] | None = None,
    scratch_days: int | None = None,
    trash_days: int | None = None,
    cold_days: int | None = None,
) -> dict[str, Any]:
  """Executes (or dry-runs) 3-tier chat GC and cold archiving."""
  now_dt = now or datetime.now(timezone.utc)
  dirs = app_dirs if app_dirs is not None else cl.find_app_data_dirs()
  if not dirs:
    dirs = [os.path.expanduser("~/.gemini/jetski")]
  rm_dir = roadmaps_dir if roadmaps_dir is not None else DEFAULT_ROADMAPS_DIR
  cfg = load_retention_config(retention_file or "")
  gc_cfg = cfg["gc"]
  eff_scratch_days = scratch_days if scratch_days is not None else gc_cfg["scratch_gc_days"]
  eff_task_days = gc_cfg["task_log_gc_days"]
  eff_trash_days = trash_days if trash_days is not None else gc_cfg["trash_delete_days"]
  eff_cold_days = cold_days if cold_days is not None else gc_cfg["cold_archive_days"]
  eff_cold_dir = cold_dir or os.path.join(dirs[0], "cold_archive")
  eff_log_file = log_file if log_file is not None else DEFAULT_GC_LOG_FILE

  if delete_rpc_fn is None:
    delete_rpc_fn = lambda cid: cl.ls_rpc("DeleteCascadeTrajectory", {"cascadeId": cid})

  if ls_summaries is None:
    resp = cl.ls_rpc("GetAllCascadeTrajectories", {"excludeSubtrajectories": False})
    summaries: dict[str, dict] = (
        (resp.get("trajectorySummaries") or {}) if isinstance(resp, dict) else {}
    )
  else:
    summaries = ls_summaries

  # Resolve triage hard-delete project IDs
  triage_proj_days: dict[str, int] = {}
  for proj_key, days in cfg["triage_hard_delete_projects"].items():
    triage_proj_days[proj_key] = days
    try:
      pid = cl.resolve_project_id(proj_key)
      if pid:
        triage_proj_days[pid] = days
    except Exception:
      pass

  # Gather on-disk annotations, SQLite rows, conversation DBs, and brain dirs
  archived_pbtxt: set[str] = set()
  pbtxt_titles: dict[str, str] = {}
  db_cids: set[str] = set()
  brain_cids: dict[str, str] = {}
  summary_db_rows: dict[str, dict[str, Any]] = {}

  for d in dirs:
    ann_dir = os.path.join(d, cl.ANNOTATIONS_DIR_NAME)
    if os.path.isdir(ann_dir):
      for fn in sorted(os.listdir(ann_dir)):
        if fn.endswith(".pbtxt"):
          cid = fn[:-6]
          if not UUID_RE.match(cid):
            continue
          try:
            with open(os.path.join(ann_dir, fn), "r", encoding="utf-8") as f:
              txt = f.read()
            if PBTXT_ARCHIVED_RE.search(txt):
              archived_pbtxt.add(cid)
            tm = PBTXT_TITLE_RE.search(txt)
            if tm and cid not in pbtxt_titles:
              pbtxt_titles[cid] = tm.group(1)
          except OSError:
            pass

    c_dir = os.path.join(d, "conversations")
    if os.path.isdir(c_dir):
      for fn in os.listdir(c_dir):
        if fn.endswith(".db"):
          cid = fn[:-3]
          if UUID_RE.match(cid):
            db_cids.add(cid)

    b_dir = os.path.join(d, "brain")
    if os.path.isdir(b_dir):
      for fn in os.listdir(b_dir):
        if UUID_RE.match(fn) and fn not in brain_cids:
          p = os.path.join(b_dir, fn)
          if os.path.isdir(p):
            brain_cids[fn] = p

    sdb_path = os.path.join(d, cl.SUMMARY_DB_NAME)
    if os.path.isfile(sdb_path):
      try:
        conn = sqlite3.connect(f"file:{sdb_path}?mode=ro", uri=True, timeout=2.0)
        cur = conn.execute(
            "SELECT conversation_id, title, step_count, last_modified_time,"
            " status, project_id, parent_conversation_id, not_fully_idle"
            " FROM conversation_summaries"
        )
        for row in cur.fetchall():
          rcid = str(row[0] or "")
          if UUID_RE.match(rcid) and rcid not in summary_db_rows:
            summary_db_rows[rcid] = {
                "title": str(row[1] or ""),
                "stepCount": int(row[2] or 0),
                "lastModifiedTime": str(row[3] or ""),
                "status": str(row[4] or ""),
                "projectId": str(row[5] or ""),
                "parentConversationId": str(row[6] or ""),
                "notFullyIdle": bool(row[7]),
            }
        conn.close()
      except Exception:
        pass

  all_cids = set(summaries) | db_cids | set(brain_cids) | set(summary_db_rows)
  proc_active = (
      set(active_cids_override)
      if active_cids_override is not None
      else _scan_proc_active_cids()
  )
  for cid, s in summaries.items():
    st = str(s.get("status") or "")
    if st in ACTIVE_STATUSES or s.get("notFullyIdle"):
      proc_active.add(cid)
  for cid, r in summary_db_rows.items():
    if r["status"] in ACTIVE_STATUSES or r["notFullyIdle"]:
      proc_active.add(cid)

  open_rm_cids, all_rm_cids = _collect_roadmap_refs(rm_dir, all_cids)
  raw_parent, raw_children, created_at_of = _scan_transcript_graph_in_dirs(dirs)
  parent_of = {k: v for k, v in raw_parent.items() if k in all_cids and v in all_cids}
  children_of = {
      k: [c for c in v if c in all_cids]
      for k, v in raw_children.items()
      if k in all_cids
  }
  graph = (parent_of, children_of, created_at_of)
  comp_of = _build_components(all_cids, parent_of, children_of)

  def _is_hidden_or_sub(cid: str) -> bool:
    s = summaries.get(cid)
    if s:
      meta = s.get("trajectoryMetadata") or {}
      smeta = meta.get("sourceMetadata") or {}
      if (
          smeta.get("tool")
          or smeta.get("hideFromConversationList")
          or smeta.get("hide_from_conversation_list")
          or meta.get("parentConversationId")
          or meta.get("isBattleModeFork")
      ):
        return True
    r = summary_db_rows.get(cid)
    if r and r["parentConversationId"]:
      return True
    return False

  def _is_archived(cid: str) -> bool:
    s = summaries.get(cid)
    if s and (s.get("annotations") or {}).get("archived"):
      return True
    return cid in archived_pbtxt

  def _age_days(cid: str) -> float:
    candidates: list[datetime] = []
    s = summaries.get(cid)
    if s:
      for raw in (
          s.get("lastModifiedTime"),
          (s.get("trajectoryMetadata") or {}).get("createdAt"),
      ):
        dt = _parse_iso(str(raw or ""))
        if dt:
          candidates.append(dt)
    r = summary_db_rows.get(cid)
    if r:
      dt = _parse_iso(r["lastModifiedTime"])
      if dt:
        candidates.append(dt)
    dt_cr = _parse_iso(created_at_of.get(cid, ""))
    if dt_cr:
      candidates.append(dt_cr)
    if not candidates:
      for d in dirs:
        for p in (
            os.path.join(d, "conversations", f"{cid}.db"),
            os.path.join(d, "brain", cid),
        ):
          if os.path.exists(p):
            try:
              candidates.append(
                  datetime.fromtimestamp(os.path.getmtime(p), tz=timezone.utc)
              )
            except OSError:
              pass
    if not candidates:
      return 0.0
    newest = max(candidates)
    return max(0.0, (now_dt - newest).total_seconds() / 86400.0)

  age_map = {cid: _age_days(cid) for cid in all_cids}

  def _comp_protected_open(comp: list[str]) -> bool:
    return any((m in proc_active or m.lower() in proc_active or m in open_rm_cids) for m in comp)

  def _comp_all_archived(comp: list[str]) -> bool:
    for m in comp:
      if m in summaries and not _is_archived(m) and not _is_hidden_or_sub(m):
        return False
      if m not in summaries and not _is_archived(m) and not _is_hidden_or_sub(m):
        if m in db_cids:
          return False
    return True

  # Tier 2: Hard Delete candidates (singletons only, never roadmap-cited or deliverable-bearing)
  tier2_reasons: dict[str, str] = {}
  tier2_bytes = 0
  for cid in sorted(all_cids):
    comp = comp_of[cid]
    if len(comp) > 1 or _comp_protected_open(comp) or cid in all_rm_cids:
      continue
    b_path = brain_cids.get(cid, "")
    if b_path and _has_root_deliverables(b_path):
      continue
    age = age_map[cid]
    s = summaries.get(cid)
    r = summary_db_rows.get(cid)

    reason = ""
    if _is_hidden_or_sub(cid) and age >= eff_trash_days:
      parent_cid = ""
      if s:
        parent_cid = str((s.get("trajectoryMetadata") or {}).get("parentConversationId") or "")
      if not parent_cid and r:
        parent_cid = r["parentConversationId"]
      parent_active = (
          bool(parent_cid)
          and (parent_cid in proc_active or parent_cid.lower() in proc_active)
      )
      if not parent_active:
        reason = "tool_or_subagent"
    elif s and _is_archived(cid) and int(s.get("stepCount") or 0) <= 2 and age >= eff_trash_days:
      if not b_path or not _transcript_has_agent_reply(b_path):
        reason = "empty_aborted"
    elif s and _is_archived(cid):
      meta_pid = str((s.get("trajectoryMetadata") or {}).get("projectId") or "")
      pid = meta_pid or (r["projectId"] if r else "")
      if pid and pid in triage_proj_days and age >= triage_proj_days[pid]:
        reason = "routine_triage"
    elif (
        cid not in summaries
        and cid not in db_cids
        and cid not in summary_db_rows
        and b_path
        and age >= eff_trash_days
    ):
      reason = "orphan_brain"

    if reason:
      tier2_reasons[cid] = reason
      for d in dirs:
        for p in (
            os.path.join(d, "conversations", f"{cid}.db"),
            os.path.join(d, "brain", cid),
        ):
          if os.path.exists(p):
            tier2_bytes += _dir_size_bytes(p)
      if apply:
        _purge_conversation_storage(
            cid, dirs, delete_rpc_fn, call_rpc=(reason != "orphan_brain")
        )

  # Tier 3: Cold Archive whole closed chains >= eff_cold_days
  tier3_cids: list[str] = []
  tier3_bytes = 0
  seen_comps: set[str] = set()
  cold_conn = _init_cold_db(eff_cold_dir) if apply else None
  cold_chains_path = os.path.join(eff_cold_dir, "chains.json")
  cold_chains: dict[str, Any] = {}
  if apply and os.path.isfile(cold_chains_path):
    try:
      with open(cold_chains_path, "r", encoding="utf-8") as f:
        cold_chains = json.load(f)
    except Exception:
      cold_chains = {}

  try:
    for cid in sorted(all_cids):
      if cid in tier2_reasons:
        continue
      comp = comp_of[cid]
      comp_key = comp[0]
      if comp_key in seen_comps:
        continue
      seen_comps.add(comp_key)
      if _comp_protected_open(comp) or not _comp_all_archived(comp):
        continue
      if any(m in tier2_reasons for m in comp):
        continue
      # Chain age is governed by the newest piece in the component
      comp_age = min(age_map[m] for m in comp)
      if comp_age < eff_cold_days:
        continue

      comp_disk = 0
      for m in comp:
        for d in dirs:
          for p in (
              os.path.join(d, "conversations", f"{m}.db"),
              os.path.join(d, "brain", m),
          ):
            if os.path.exists(p):
              comp_disk += _dir_size_bytes(p)

      if apply and cold_conn is not None:
        staged_rows: list[tuple[Any, ...]] = []
        staged_fts: list[tuple[str, str, str, str, str]] = []
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        for m in comp:
          chain_seq = cl._graph_chain(m, graph)
          piece_idx = chain_seq.index(m) + 1 if m in chain_seq else 1
          raw_b, u_body, a_body = _extract_transcript_bytes_and_bodies(m, dirs)
          dst_base = os.path.join(eff_cold_dir, "blobs", m)
          blob_full = _compress_and_verify_blob(raw_b, dst_base)
          blob_rel = os.path.relpath(blob_full, eff_cold_dir)

          # Copy root deliverables (.md/.html/.url.json) into cold_archive/artifacts/<cid>/
          b_dir = brain_cids.get(m, "")
          if b_dir and os.path.isdir(b_dir):
            for fn in sorted(os.listdir(b_dir)):
              if fn.startswith("."):
                continue
              src_f = os.path.join(b_dir, fn)
              if os.path.isfile(src_f) and (
                  fn.lower().endswith(COLD_COPY_EXTS) or fn.endswith(".metadata.json")
              ):
                art_dst = os.path.join(eff_cold_dir, "artifacts", m)
                os.makedirs(art_dst, exist_ok=True)
                shutil.copy2(src_f, os.path.join(art_dst, fn))

          s = summaries.get(m) or {}
          r = summary_db_rows.get(m) or {}
          meta = s.get("trajectoryMetadata") or {}
          title = str(
              s.get("summary") or pbtxt_titles.get(m) or r.get("title") or m
          ).strip()
          pid = str(meta.get("projectId") or r.get("projectId") or "")
          c_at = str(meta.get("createdAt") or created_at_of.get(m) or "")
          lmt = str(s.get("lastModifiedTime") or r.get("lastModifiedTime") or c_at)
          staged_rows.append((
              m,
              title,
              pid,
              c_at,
              lmt,
              chain_seq[0],
              piece_idx,
              len(chain_seq),
              blob_rel,
              now_iso,
          ))
          staged_fts.append((m, title, pid, u_body, a_body))
          if m in parent_of:
            cold_chains.setdefault("parentOf", {})[m] = parent_of[m]

        with cold_conn:
          for row in staged_rows:
            cold_conn.execute("DELETE FROM cold_fts WHERE cascade_id = ?", (row[0],))
            cold_conn.execute(
                "INSERT OR REPLACE INTO cold_conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                row,
            )
          for frow in staged_fts:
            cold_conn.execute(
                "INSERT INTO cold_fts (cascade_id, title, project_id, user_body, agent_body)"
                " VALUES (?, ?, ?, ?, ?)",
                frow,
            )
        tmp_ch = cold_chains_path + ".tmp"
        with open(tmp_ch, "w", encoding="utf-8") as cf:
          json.dump(cold_chains, cf, ensure_ascii=False, sort_keys=True)
        os.replace(tmp_ch, cold_chains_path)

        # Only purge live storage after the entire chain is verified in cold archive
        for m in comp:
          _purge_conversation_storage(m, dirs, delete_rpc_fn, call_rpc=True)

      tier3_cids.extend(comp)
      tier3_bytes += comp_disk
  finally:
    if cold_conn is not None:
      cold_conn.close()

  tier3_set = set(tier3_cids)

  # Tier 1: Scratch & stale task-log GC on remaining archived chains >= eff_scratch_days
  tier1_cids: list[str] = []
  tier1_bytes = 0
  preserved_dirty_git: list[str] = []
  task_logs_deleted = 0
  task_logs_bytes = 0

  for cid in sorted(brain_cids):
    if cid in tier2_reasons or cid in tier3_set:
      continue
    comp = comp_of[cid]
    b_dir = brain_cids[cid]

    # Sweep stale background task logs >= eff_task_days on idle conversations
    if cid not in proc_active and cid.lower() not in proc_active:
      tasks_dir = os.path.join(b_dir, ".system_generated", "tasks")
      if os.path.isdir(tasks_dir):
        for fn in sorted(os.listdir(tasks_dir)):
          if not fn.endswith(".log"):
            continue
          lp = os.path.join(tasks_dir, fn)
          try:
            st = os.stat(lp)
            if (now_dt.timestamp() - st.st_mtime) / 86400.0 >= eff_task_days:
              task_logs_deleted += 1
              task_logs_bytes += st.st_size
              if apply:
                os.unlink(lp)
          except OSError:
            pass

    if _comp_protected_open(comp) or not _comp_all_archived(comp):
      continue
    comp_age = min(age_map[m] for m in comp)
    if comp_age < eff_scratch_days:
      continue

    freed, kept_git = _clean_scratch_dir(b_dir, apply=apply)
    preserved_dirty_git.extend(kept_git)
    if freed > 0:
      tier1_cids.append(cid)
      tier1_bytes += freed

  report = {
      "timestamp": now_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
      "apply": apply,
      "protected_active_count": len(proc_active),
      "protected_open_roadmap_count": len(open_rm_cids),
      "tier1_scratch": {
          "count": len(tier1_cids),
          "bytes": tier1_bytes,
          "cids": tier1_cids,
          "preserved_dirty_git": preserved_dirty_git,
          "task_logs_deleted": task_logs_deleted,
          "task_logs_bytes": task_logs_bytes,
      },
      "tier2_hard_delete": {
          "count": len(tier2_reasons),
          "bytes": tier2_bytes,
          "cids": sorted(tier2_reasons),
          "by_reason": {
              r: sum(1 for v in tier2_reasons.values() if v == r)
              for r in sorted(set(tier2_reasons.values()))
          },
      },
      "tier3_cold_archive": {
          "count": len(tier3_cids),
          "bytes": tier3_bytes,
          "cids": tier3_cids,
          "cold_dir": eff_cold_dir,
      },
      "total_reclaimed_bytes": tier1_bytes + task_logs_bytes + tier2_bytes + tier3_bytes,
  }

  if apply and eff_log_file:
    try:
      os.makedirs(os.path.dirname(eff_log_file), exist_ok=True)
      with open(eff_log_file, "a", encoding="utf-8") as lf:
        lf.write(json.dumps(report, ensure_ascii=False) + "\n")
    except OSError:
      pass

  return report


def _fts5_safe_query(query: str) -> str:
  # Wrap alphanumeric/underscore tokens in quotes so C++ / U+200B / :: never break FTS5 syntax
  tokens = re.findall(r"[\w]+", query, flags=re.UNICODE)
  if not tokens:
    return ""
  return " AND ".join(f'"{t}"' for t in tokens)


def search_cold_archive(
    query: str, cold_dir: str = "", limit: int = 50
) -> list[dict[str, Any]]:
  """Searches cold-archived conversations via SQLite FTS5."""
  dirs = cl.find_app_data_dirs()
  base_cold = cold_dir or os.path.join(
      dirs[0] if dirs else os.path.expanduser("~/.gemini/jetski"), "cold_archive"
  )
  db_path = os.path.join(base_cold, "index.sqlite")
  if not os.path.isfile(db_path):
    return []
  fts_q = _fts5_safe_query(query)
  if not fts_q:
    return []

  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
  try:
    cur = conn.execute(
        "SELECT c.cascade_id, c.title, c.project_id, c.last_modified_time,"
        " c.piece_index, c.chain_length,"
        " snippet(cold_fts, 3, '[', ']', ' … ', 16) AS u_snip,"
        " snippet(cold_fts, 4, '[', ']', ' … ', 16) AS a_snip"
        " FROM cold_fts"
        " JOIN cold_conversations c ON c.cascade_id = cold_fts.cascade_id"
        " WHERE cold_fts MATCH ?"
        " ORDER BY rank LIMIT ?",
        (fts_q, limit),
    )
    hits: list[dict[str, Any]] = []
    for row in cur.fetchall():
      u_snip = str(row[6] or "").strip()
      a_snip = str(row[7] or "").strip()
      who = "agent" if (a_snip and "[" in a_snip) else "user"
      snip = a_snip if who == "agent" else (u_snip or a_snip)
      hits.append({
          "cascadeId": row[0],
          "title": row[1],
          "projectId": row[2],
          "lastModifiedTime": row[3],
          "piece": int(row[4]),
          "chain_length": int(row[5]),
          "who": who,
          "snippet": " ".join(snip.split()),
      })
    return hits
  finally:
    conn.close()


def get_cold_conversation(cid: str, cold_dir: str = "") -> dict[str, Any]:
  """Reads metadata and decompressed user/agent turns from cold_archive."""
  dirs = cl.find_app_data_dirs()
  base_cold = cold_dir or os.path.join(
      dirs[0] if dirs else os.path.expanduser("~/.gemini/jetski"), "cold_archive"
  )
  db_path = os.path.join(base_cold, "index.sqlite")
  if not os.path.isfile(db_path):
    raise RuntimeError(f"Cold archive not initialized at {base_cold}")

  conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
  try:
    row = conn.execute(
        "SELECT cascade_id, title, project_id, created_at, last_modified_time,"
        " chain_root, piece_index, chain_length, blob_relpath"
        " FROM cold_conversations WHERE cascade_id = ? OR cascade_id LIKE ?",
        (cid, f"{cid}%"),
    ).fetchone()
  finally:
    conn.close()

  if not row:
    raise RuntimeError(f"Conversation '{cid}' not found in cold archive")

  blob_path = os.path.join(base_cold, row[8])
  raw = _read_cold_blob(blob_path)
  messages: list[dict[str, Any]] = []
  for idx, line in enumerate(raw.decode("utf-8", errors="replace").splitlines()):
    if not line.strip():
      continue
    try:
      obj = json.loads(line)
    except ValueError:
      continue
    stype = str(obj.get("type") or "")
    if stype in ("USER_INPUT", "CORTEX_STEP_TYPE_USER_INPUT"):
      txt = str(obj.get("content") or (obj.get("userInput") or {}).get("userResponse") or "")
      messages.append({"step": obj.get("step_index", idx), "who": "user", "text": txt})
    elif stype in ("PLANNER_RESPONSE", "CORTEX_STEP_TYPE_PLANNER_RESPONSE"):
      pr = obj.get("plannerResponse") or {}
      txt = str(obj.get("content") or pr.get("modifiedResponse") or pr.get("response") or "")
      messages.append({"step": obj.get("step_index", idx), "who": "agent", "text": txt})

  return {
      "cascadeId": row[0],
      "title": row[1],
      "projectId": row[2],
      "createdAt": row[3],
      "lastModifiedTime": row[4],
      "chainRoot": row[5],
      "piece": int(row[6]),
      "chainLength": int(row[7]),
      "messages": messages,
  }
