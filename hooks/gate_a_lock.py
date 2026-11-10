#!/usr/bin/env python3
"""gate_a_lock.py — SHA-256 hash lock for confirmed failing Gate A contract tests.

Pins confirmed failing contract test files after Gate A by canonical realpath, SHA-256
digest, conversation handoff chain root, worktree, and lock timestamp (inspired by
v8_formal pinned_spec_hashes). Enforced in pre_tool_guard.py across the locking chat,
its subagents, and handoff continuations so Gate B production fixes never weaken tests.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Iterator, Sequence
import contextlib
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time

DEFAULT_LEDGER = os.path.expanduser("~/.gemini/config/ledgers/gate_a_locks.json")
GATE_A_LOCK_TTL_S = 7 * 86400
MAX_HISTORY_ENTRIES = 200
MAX_SUBAGENT_HOPS = 8
MAX_CHAIN_HOPS = 32
MAX_CHAIN_DESCENDANTS = 64

EDIT_TOOLS = frozenset({
    "write_to_file",
    "replace_file_content",
    "multi_replace_file_content",
    "write_file",
    "edit_file",
})

HANDOFF_PARENT_RE = re.compile(
    r"(?:continu(?:ing|e)\b|\u043f\u0440\u043e\u0434\u043e\u043b\u0436(?:\u0430\u0435\u043c|\u0438)\b)"
    r"(?:(?!(?:split out of|separated from|coordinated from))[^\"\n]){0,200}?"
    r"(?:conversation://|Conversation ID:\s*|/brain/)([a-zA-Z0-9_\-]+)",
    re.IGNORECASE,
)

HEREDOC_RE = re.compile(
    r"<<(-)?\s*(['\"]?)(\w+)\2([^\n]*)\n.*?^(?(1)\t*)\3[ \t]*$",
    re.DOTALL | re.MULTILINE,
)

REDIRECT_TARGET_RE = re.compile(
    r"(?<![-=<>])(?:\d*>>?|\d*>\|?|&>>?)(?!>)\s*(['\"]?[^'\"\s;&|()<>]+['\"]?)"
)

SCRIPT_WRITE_HINT_RE = re.compile(
    r"(?:\bopen\s*\(|\.write(?:_text|_bytes)?\s*\(|\.(?:unlink|rename|replace)\s*\("
    r"|\bos\.(?:replace|rename|remove|unlink|truncate)\b|\bshutil\.(?:copy\w*|move)\b"
    r"|\bwriteFileSync\b|\bfs\.write|\bFile\.write)"
)

LEGACY_CLOSED_RE = re.compile(r"^\s*\[\d{1,2}:\d{2}\]\s*(?:‹✓›|«»|[«»])(?:\s|$)")


def ledger_path() -> str:
  """Returns the filesystem path of the Gate A hash lock JSON ledger."""
  return os.path.expanduser(os.environ.get("GATE_A_LOCK_FILE") or DEFAULT_LEDGER)


def compute_file_sha256(path: str) -> str:
  """Computes the lowercase hexadecimal SHA-256 digest of a file on disk."""
  digest = hashlib.sha256()
  with open(path, "rb") as handle:
    for _ in range(1_000_000):
      chunk = handle.read(65536)
      if not chunk:
        break
      digest.update(chunk)
  return digest.hexdigest()


def _utc_iso(epoch: float) -> str:
  """Formats a UNIX timestamp as a second-precision UTC ISO-8601 string."""
  return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _env_conversation_id() -> str:
  """Reads the active conversation ID from Jetski or Antigravity environment variables."""
  return (
      os.environ.get("CONVERSATION_ID", "").strip()
      or os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "").strip()
  )


def _read_ledger_unlocked(path: str) -> dict:
  """Reads and parses the JSON lock ledger, returning an empty mapping on missing/corrupt data."""
  if not os.path.isfile(path):
    return {"locks": {}, "history": []}
  try:
    with open(path, "r", encoding="utf-8") as handle:
      data = json.load(handle)
    if isinstance(data, dict):
      locks = data.get("locks") if isinstance(data.get("locks"), dict) else {}
      history = data.get("history") if isinstance(data.get("history"), list) else []
      return {"locks": dict(locks), "history": list(history)}
  except (OSError, ValueError, TypeError):
    pass
  return {"locks": {}, "history": []}


def _write_ledger_unlocked(path: str, data: dict) -> None:
  """Atomically writes the Gate A lock ledger JSON payload to disk."""
  parent = os.path.dirname(path)
  if parent:
    os.makedirs(parent, exist_ok=True)
  history = data.get("history") if isinstance(data.get("history"), list) else []
  payload = {
      "locks": data.get("locks") if isinstance(data.get("locks"), dict) else {},
      "history": history[-MAX_HISTORY_ENTRIES:],
  }
  tmp = f"{path}.tmp.{os.getpid()}"
  try:
    with open(tmp, "w", encoding="utf-8") as handle:
      json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
      handle.write("\n")
    os.replace(tmp, path)
  except BaseException:
    if os.path.exists(tmp):
      try:
        os.unlink(tmp)
      except OSError:
        pass
    raise


def _resolve_worktree(file_path: str, explicit_worktree: str = "") -> str:
  """Resolves the enclosing Git worktree realpath for file_path, or its parent directory."""
  if explicit_worktree:
    return os.path.realpath(os.path.expanduser(explicit_worktree))
  parent = os.path.dirname(os.path.realpath(file_path))
  if not os.path.isdir(parent):
    return parent
  try:
    res = subprocess.run(
        ["git", "-C", parent, "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        timeout=1.5,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
      return os.path.realpath(res.stdout.strip())
  except (OSError, subprocess.SubprocessError):
    pass
  return parent


def _load_context_guard():
  """Imports sibling context_guard whether invoked as a script or via importlib."""
  hooks_dir = os.path.dirname(os.path.abspath(__file__))
  if hooks_dir not in sys.path:
    sys.path.insert(0, hooks_dir)
  import context_guard

  return context_guard


def _app_data_dirs(hint_path: str = "") -> list[str]:
  """Collects existing Jetski/Antigravity app data directories from env vars and hint_path."""
  context_guard = _load_context_guard()

  candidates: list[str] = []
  for env_key in ("ANTIGRAVITY_APP_DATA_DIR", "JETSKI_APP_DATA_DIR"):
    val = os.environ.get(env_key, "").strip()
    if val:
      candidates.append(os.path.abspath(os.path.expanduser(val)))
  for d in context_guard._candidate_app_data_dirs(hint_path):
    candidates.append(d)
  out: list[str] = []
  seen: set[str] = set()
  for d in candidates:
    if d and d not in seen and os.path.isdir(d):
      seen.add(d)
      out.append(d)
  return out


def _transcript_for_conv(conv_id: str, transcript_path: str = "", hint_path: str = "") -> str:
  """Finds the transcript.jsonl path for conv_id using transcript_path or app data dirs."""
  if transcript_path and conv_id and f"/{conv_id}/" in transcript_path.replace("\\", "/"):
    if os.path.isfile(transcript_path):
      return transcript_path
  for ad in _app_data_dirs(hint_path or transcript_path):
    cand = os.path.join(ad, "brain", conv_id, ".system_generated", "logs", "transcript.jsonl")
    if os.path.isfile(cand):
      return cand
  return ""


def resolve_top_conversation_id(
    conv_id: str,
    parent_conv_id: str = "",
    transcript_path: str = "",
    hint_path: str = "",
) -> str:
  """Walks subagent parent links up to the owning top-level chat piece."""
  context_guard = _load_context_guard()

  cur = (parent_conv_id or conv_id or "").strip()
  if not cur:
    return ""
  seen: set[str] = set()
  for _ in range(MAX_SUBAGENT_HOPS):
    if cur in seen:
      break
    seen.add(cur)
    tr = _transcript_for_conv(cur, transcript_path if cur == conv_id else "", hint_path)
    parent = context_guard.detect_subagent_parent({
        "conversationId": cur,
        "transcriptPath": tr,
    })
    if not parent or parent == cur:
      break
    cur = parent
  return cur


def _step0_handoff_parent(transcript_file: str) -> str:
  """Extracts the handoff predecessor conversation ID from step 0 USER_INPUT if present."""
  if not transcript_file or not os.path.isfile(transcript_file):
    return ""
  try:
    with open(transcript_file, "r", encoding="utf-8", errors="ignore") as handle:
      first_line = handle.readline()
    if not first_line:
      return ""
    obj = json.loads(first_line)
    if not isinstance(obj, dict):
      return ""
    if obj.get("type") != "USER_INPUT" or obj.get("step_index", 0) != 0:
      return ""
    m = HANDOFF_PARENT_RE.search(str(obj.get("content") or ""))
    return m.group(1) if m else ""
  except (OSError, ValueError, TypeError):
    return ""


def _guard_handoff_child(cid: str, app_dirs: Sequence[str]) -> str:
  """Returns the direct non-next-task handoff target recorded in cid's guard state, if any."""
  for ad in app_dirs:
    guard_file = os.path.join(ad, "brain", cid, "scratch", ".context_guard_state.json")
    if not os.path.isfile(guard_file):
      continue
    try:
      with open(guard_file, "r", encoding="utf-8") as handle:
        gdata = json.load(handle)
      if isinstance(gdata, dict):
        owner = str(gdata.get("owner") or "").strip()
        to_cid = str(gdata.get("handed_off_to") or "").strip()
        kind = str(gdata.get("handoff_kind") or "handoff")
        if (not owner or owner == cid) and to_cid and kind != "next-task":
          return to_cid
    except (OSError, ValueError, TypeError):
      pass
  return ""


def _handoff_graph(app_dirs: Sequence[str]) -> tuple[dict[str, str], dict[str, list[str]]]:
  """Builds (parent_of, children_of) handoff maps from jetski-chains.json or small test dirs."""
  parent_of: dict[str, str] = {}
  children_of: dict[str, list[str]] = {}

  def _add_edge(parent_cid: str, child_cid: str) -> None:
    if not parent_cid or not child_cid or parent_cid == child_cid:
      return
    if child_cid not in parent_of:
      parent_of[child_cid] = parent_cid
    kids = children_of.setdefault(parent_cid, [])
    if child_cid not in kids:
      kids.append(child_cid)

  for ad in app_dirs:
    brain_dir = os.path.join(ad, "brain")
    if not os.path.isdir(brain_dir):
      continue
    chain_idx = os.path.join(brain_dir, "jetski-chains.json")
    has_chain_idx = os.path.isfile(chain_idx)
    if has_chain_idx:
      try:
        with open(chain_idx, "r", encoding="utf-8") as handle:
          idx_data = json.load(handle)
        if isinstance(idx_data, dict):
          for ch_cid, p_cid in (idx_data.get("parentOf") or {}).items():
            if isinstance(ch_cid, str) and isinstance(p_cid, str):
              _add_edge(p_cid, ch_cid)
          for p_cid, redir in (idx_data.get("redirects") or {}).items():
            if isinstance(redir, dict) and str(redir.get("kind") or "handoff") != "next-task":
              to_cid = str(redir.get("to") or "").strip()
              _add_edge(p_cid, to_cid)
      except (OSError, ValueError, TypeError):
        pass
      # Avoid scanning thousands of brain subdirectories when jetski-chains.json exists;
      # unindexed recent hops are resolved in O(hops) by resolve_chain_root and is_lock_active
      continue

    try:
      entries = sorted(os.listdir(brain_dir))
    except OSError:
      continue
    if len(entries) > 128:
      continue
    for cid in entries:
      to_cid = _guard_handoff_child(cid, (ad,))
      if to_cid:
        _add_edge(cid, to_cid)
      tr_file = os.path.join(brain_dir, cid, ".system_generated", "logs", "transcript.jsonl")
      p_cid = _step0_handoff_parent(tr_file)
      if p_cid:
        _add_edge(p_cid, cid)
  return parent_of, children_of


def resolve_chain_root(
    conv_id: str,
    parent_conv_id: str = "",
    transcript_path: str = "",
    hint_path: str = "",
) -> str:
  """Resolves subagent and handoff links to the root conversation ID of the active chain."""
  hint = hint_path or transcript_path
  top_cid = resolve_top_conversation_id(conv_id, parent_conv_id, transcript_path, hint)
  if not top_cid:
    return ""
  app_dirs = _app_data_dirs(hint)
  parent_of, _ = _handoff_graph(app_dirs)
  cur = top_cid
  seen: set[str] = set()
  for _ in range(MAX_CHAIN_HOPS):
    if cur in seen:
      break
    seen.add(cur)
    nxt = parent_of.get(cur, "")
    if not nxt:
      tr_cur = _transcript_for_conv(cur, transcript_path if cur == top_cid else "", hint)
      nxt = _step0_handoff_parent(tr_cur)
    if not nxt or nxt == cur:
      break
    cur = nxt
  return cur


def _is_conv_archived_or_closed(cid: str, hint_path: str, app_dirs: Sequence[str]) -> bool:
  """Returns True when cid is archived in annotations pbtxt or closed via next-task."""
  context_guard = _load_context_guard()

  if context_guard._is_archived_in_pbtxt(cid, hint_path):
    return True
  for ad in app_dirs:
    if context_guard._is_archived_in_pbtxt(cid, ad):
      return True
    pb_file = os.path.join(ad, "annotations", f"{cid}.pbtxt")
    if os.path.isfile(pb_file):
      try:
        with open(pb_file, "r", encoding="utf-8", errors="ignore") as handle:
          raw = handle.read()
        if re.search(r"\barchived\s*:\s*true\b", raw):
          return True
        m_title = re.search(r'\btitle\s*:\s*"((?:\\.|[^"\\])*)"', raw)
        if m_title and LEGACY_CLOSED_RE.match(m_title.group(1)):
          return True
      except OSError:
        pass
    guard_file = os.path.join(ad, "brain", cid, "scratch", ".context_guard_state.json")
    if os.path.isfile(guard_file):
      try:
        with open(guard_file, "r", encoding="utf-8") as handle:
          gdata = json.load(handle)
        if (
            isinstance(gdata, dict)
            and gdata.get("handoff_completed")
            and str(gdata.get("handoff_kind") or "") == "next-task"
        ):
          return True
      except (OSError, ValueError, TypeError):
        pass
  return False


def is_lock_active(
    entry: dict,
    hint_path: str = "",
    now: float | None = None,
    _graph: tuple[dict[str, str], dict[str, list[str]]] | None = None,
    _app_dirs: Sequence[str] | None = None,
) -> bool:
  """Returns True if entry has not expired and its owning handoff chain has an open tip."""
  if not isinstance(entry, dict) or not entry.get("sha256") or not entry.get("path"):
    return False
  cur_now = time.time() if now is None else now
  locked_at = float(entry.get("locked_at") or 0.0)
  if locked_at > 0 and cur_now - locked_at > GATE_A_LOCK_TTL_S:
    return False
  chain_root = str(entry.get("chain_root") or "").strip()
  if not chain_root:
    return True
  app_dirs = list(_app_dirs) if _app_dirs is not None else _app_data_dirs(hint_path)
  _, children_of = _graph if _graph is not None else _handoff_graph(app_dirs)

  # Walk handoff descendants from chain_root; any open leaf keeps the chain lock live
  queue: deque[str] = deque([chain_root])
  visited: set[str] = set()
  tips: list[str] = []
  for _ in range(MAX_CHAIN_DESCENDANTS):
    if not queue:
      break
    node = queue.popleft()
    if node in visited:
      continue
    visited.add(node)
    raw_kids = list(children_of.get(node, []))
    guard_kid = _guard_handoff_child(node, app_dirs)
    if guard_kid and guard_kid not in raw_kids:
      raw_kids.append(guard_kid)
    kids = [k for k in raw_kids if k not in visited]
    if not kids:
      tips.append(node)
    else:
      queue.extend(kids)
  if not tips:
    tips = [chain_root]
  return any(not _is_conv_archived_or_closed(tip, hint_path, app_dirs) for tip in tips)


def load_active_locks(hint_path: str = "", now: float | None = None) -> dict[str, dict]:
  """Loads all currently active Gate A file locks keyed by canonical realpath."""
  path = ledger_path()
  if not os.path.isfile(path):
    return {}
  data = _read_ledger_unlocked(path)
  raw_locks = data.get("locks") or {}
  if not raw_locks:
    return {}
  app_dirs = _app_data_dirs(hint_path)
  graph = _handoff_graph(app_dirs)
  cur_now = time.time() if now is None else now
  active: dict[str, dict] = {}
  for real_p, entry in raw_locks.items():
    if isinstance(entry, dict) and is_lock_active(
        entry, hint_path=hint_path, now=cur_now, _graph=graph, _app_dirs=app_dirs
    ):
      active[os.path.realpath(real_p)] = entry
  return active


@contextlib.contextmanager
def _locked_ledger(
    hint_path: str, now: float
) -> Iterator[tuple[dict[str, dict], list[dict]]]:
  """Acquires an exclusive flock on the ledger, prunes stale locks, and saves on exit."""
  path = ledger_path()
  parent = os.path.dirname(path)
  if parent:
    os.makedirs(parent, exist_ok=True)
  fd = os.open(path + ".lock", os.O_RDWR | os.O_CREAT, 0o644)
  try:
    fcntl.flock(fd, fcntl.LOCK_EX)
    data = _read_ledger_unlocked(path)
    app_dirs = _app_data_dirs(hint_path)
    graph = _handoff_graph(app_dirs)
    kept = {
        os.path.realpath(k): v
        for k, v in (data.get("locks") or {}).items()
        if isinstance(v, dict)
        and is_lock_active(v, hint_path=hint_path, now=now, _graph=graph, _app_dirs=app_dirs)
    }
    history = list(data.get("history") or [])
    yield kept, history
    data["locks"] = kept
    data["history"] = history
    _write_ledger_unlocked(path, data)
  finally:
    os.close(fd)


def lock_files(
    files: Sequence[str],
    conv_id: str = "",
    parent_conv_id: str = "",
    worktree: str = "",
    hint_path: str = "",
) -> list[dict]:
  """Pins existing non-empty Gate A test files by canonical realpath and SHA-256."""
  if not files:
    raise ValueError("At least one Gate A test file path is required.")
  eff_conv = (conv_id or _env_conversation_id()).strip()
  chain_root = resolve_chain_root(eff_conv, parent_conv_id=parent_conv_id, hint_path=hint_path)
  now = time.time()
  now_iso = _utc_iso(now)

  prepared: list[dict] = []
  for raw_f in files:
    real_p = os.path.realpath(os.path.expanduser(str(raw_f).strip()))
    if not os.path.isfile(real_p):
      raise FileNotFoundError(f"Gate A test file does not exist: {real_p}")
    if os.path.getsize(real_p) == 0:
      raise ValueError(f"Cannot lock empty Gate A test file: {real_p}")
    sha = compute_file_sha256(real_p)
    wt = _resolve_worktree(real_p, worktree)
    prepared.append({
        "path": real_p,
        "sha256": sha,
        "chain_root": chain_root,
        "conv_id": eff_conv,
        "worktree": wt,
        "locked_at": now,
        "locked_at_iso": now_iso,
    })

  with _locked_ledger(hint_path, now) as (kept, history):
    for entry in prepared:
      kept[entry["path"]] = entry
      history.append({
          "action": "lock",
          "path": entry["path"],
          "sha256": entry["sha256"],
          "chain_root": entry["chain_root"],
          "conv_id": entry["conv_id"],
          "ts": now,
          "ts_iso": now_iso,
      })
  return prepared


def _select_target_locks(
    active: dict[str, dict],
    files: Sequence[str],
    chain_root: str,
    worktree: str,
) -> tuple[dict[str, dict], list[str]]:
  """Selects matching locks from active and reports any explicitly requested unlocked paths."""
  if files:
    selected: dict[str, dict] = {}
    missing_locks: list[str] = []
    for raw_f in files:
      real_p = os.path.realpath(os.path.expanduser(str(raw_f).strip()))
      if real_p in active:
        selected[real_p] = active[real_p]
      else:
        missing_locks.append(real_p)
    return selected, missing_locks

  real_wt = os.path.realpath(os.path.expanduser(worktree)) if worktree else ""
  selected = {}
  for real_p, entry in active.items():
    if real_wt:
      ewt = str(entry.get("worktree") or "")
      if ewt != real_wt and not real_p.startswith(real_wt + os.sep):
        continue
    elif chain_root:
      echain = str(entry.get("chain_root") or "")
      if echain and echain != chain_root:
        continue
    selected[real_p] = entry
  return selected, []


def verify_locks(
    files: Sequence[str] = (),
    conv_id: str = "",
    parent_conv_id: str = "",
    worktree: str = "",
    hint_path: str = "",
) -> tuple[bool, list[str], list[dict]]:
  """Verifies on-disk SHA-256 digests against active Gate A locks, returning (ok, errors, rows)."""
  eff_conv = (conv_id or _env_conversation_id()).strip()
  chain_root = resolve_chain_root(eff_conv, parent_conv_id=parent_conv_id, hint_path=hint_path)
  active = load_active_locks(hint_path=hint_path)
  selected, unlocked_paths = _select_target_locks(active, files, chain_root, worktree)

  errors: list[str] = []
  rows: list[dict] = []
  for up in unlocked_paths:
    errors.append(f"No active Gate A hash lock found for {up}")
  for real_p, entry in sorted(selected.items()):
    expected = str(entry.get("sha256") or "")
    if not os.path.isfile(real_p):
      actual = "<missing>"
      state = "MISSING"
      errors.append(
          f"Gate A locked test file is missing on disk: {real_p} (expected {expected})"
      )
    else:
      actual = compute_file_sha256(real_p)
      if actual.lower() != expected.lower():
        state = "TAMPERED"
        errors.append(
            f"Gate A hash mismatch for {real_p}: expected {expected}, got {actual}"
        )
      else:
        state = "OK"
    rows.append({**entry, "actual_sha256": actual, "integrity": state})
  return len(errors) == 0, errors, rows


def unlock_files(
    reason: str,
    files: Sequence[str] = (),
    conv_id: str = "",
    parent_conv_id: str = "",
    worktree: str = "",
    hint_path: str = "",
) -> list[dict]:
  """Unlocks Gate A test files when given a non-empty audit reason and logs the event."""
  clean_reason = (reason or "").strip()
  if not clean_reason:
    raise ValueError(
        "Unlocking a Gate A test requires a non-empty --reason explaining why Gate A must change "
        "(e.g. adding a new test case or fixing a typo in Gate A itself)."
    )
  eff_conv = (conv_id or _env_conversation_id()).strip()
  chain_root = resolve_chain_root(eff_conv, parent_conv_id=parent_conv_id, hint_path=hint_path)
  now = time.time()
  now_iso = _utc_iso(now)

  removed: list[dict] = []
  with _locked_ledger(hint_path, now) as (kept, history):
    selected, _ = _select_target_locks(kept, files, chain_root, worktree)
    for real_p, entry in sorted(selected.items()):
      kept.pop(real_p, None)
      removed.append(entry)
      history.append({
          "action": "unlock",
          "path": real_p,
          "sha256": entry.get("sha256", ""),
          "chain_root": entry.get("chain_root", ""),
          "conv_id": eff_conv,
          "reason": clean_reason,
          "ts": now,
          "ts_iso": now_iso,
      })
  return removed


def clear_locks(
    files: Sequence[str] = (),
    conv_id: str = "",
    parent_conv_id: str = "",
    worktree: str = "",
    hint_path: str = "",
) -> list[dict]:
  """Releases intact Gate A locks after Gate B passes, refusing tampered files without unlock."""
  eff_conv = (conv_id or _env_conversation_id()).strip()
  chain_root = resolve_chain_root(eff_conv, parent_conv_id=parent_conv_id, hint_path=hint_path)
  now = time.time()
  now_iso = _utc_iso(now)

  cleared: list[dict] = []
  with _locked_ledger(hint_path, now) as (kept, history):
    selected, _ = _select_target_locks(kept, files, chain_root, worktree)
    for real_p, entry in sorted(selected.items()):
      expected = str(entry.get("sha256") or "")
      if not os.path.isfile(real_p):
        raise ValueError(
            f"Cannot clear Gate A lock for missing file {real_p} (expected {expected}). "
            "Restore the Gate A test file or run 'gate_a_lock.py unlock --reason \"...\"'."
        )
      actual = compute_file_sha256(real_p)
      if actual.lower() != expected.lower():
        raise ValueError(
            f"Gate A hash mismatch for {real_p}: expected {expected}, got {actual}. "
            "Restore the Gate A test file or run 'gate_a_lock.py unlock --reason \"...\"'."
        )
    for real_p, entry in sorted(selected.items()):
      kept.pop(real_p, None)
      cleared.append(entry)
      history.append({
          "action": "clear",
          "path": real_p,
          "sha256": entry.get("sha256", ""),
          "chain_root": entry.get("chain_root", ""),
          "conv_id": eff_conv,
          "ts": now,
          "ts_iso": now_iso,
      })
  return cleared


def _extract_edit_target(tool_args: dict) -> str:
  """Extracts the target file path across Jetski Web and Antigravity edit tool schemas."""
  for key in ("TargetFile", "Path", "AbsolutePath"):
    val = tool_args.get(key)
    if val:
      return str(val).strip()
  return ""


def _simulated_edit_sha256(tool_name: str, tool_args: dict, real_target: str) -> str:
  """Computes the resulting SHA-256 if an edit tool were applied, or '' if indeterminate."""
  existing = ""
  if os.path.isfile(real_target):
    try:
      with open(real_target, "r", encoding="utf-8") as handle:
        existing = handle.read()
    except OSError:
      return ""
  if tool_name in ("write_file", "write_to_file"):
    if "Content" in tool_args:
      body = str(tool_args.get("Content") or "")
    elif "CodeContent" in tool_args:
      body = str(tool_args.get("CodeContent") or "")
    else:
      return ""
    new_text = existing + body if tool_args.get("Append") else body
    return hashlib.sha256(new_text.encode("utf-8")).hexdigest()

  chunks = tool_args.get("ReplacementChunks")
  chunk_list = chunks if isinstance(chunks, list) and chunks else [tool_args]
  working = existing
  for chunk in chunk_list:
    if not isinstance(chunk, dict):
      return ""
    target = str(chunk.get("Target") or chunk.get("TargetContent") or "")
    if not target or target not in working:
      return ""
    repl = str(
        chunk["Replacement"]
        if "Replacement" in chunk
        else chunk.get("ReplacementContent") or ""
    )
    working = working.replace(target, repl)
  return hashlib.sha256(working.encode("utf-8")).hexdigest()


def _format_edit_denial(entry: dict) -> str:
  """Formats the Contract Honesty denial message when a locked Gate A test is targeted."""
  path = entry["path"]
  sha = str(entry.get("sha256") or "")[:12]
  chain = str(entry.get("chain_root") or "local")[:8]
  return (
      f"[GATE-A HASH LOCK] Contract Honesty violation: `{path}` is pinned by a Gate A "
      f"SHA-256 hash lock (`{sha}...`, chain `{chain}`). While fixing production code "
      "(Gate B), do not weaken, edit, or overwrite the confirmed failing Gate A contract "
      "test — fix the production code to satisfy the contract instead. If you genuinely "
      "need to add a new test case or fix a typo in Gate A itself, unlock explicitly first:\n"
      f"`python3 ~/.gemini/config/hooks/gate_a_lock.py unlock --reason \"<why Gate A must "
      f"change>\" {shlex.quote(path)}`"
  )


def _format_tamper_denial(entry: dict, actual_sha: str) -> str:
  """Formats the Contract Honesty denial message when a locked file changed on disk."""
  path = entry["path"]
  expected = str(entry.get("sha256") or "")
  return (
      f"[GATE-A HASH LOCK] Contract Honesty violation (SHA-256 integrity mismatch): "
      f"`{path}` was modified on disk outside `edit_file` (expected `{expected}`, got "
      f"`{actual_sha}`). Gate A contract tests must remain byte-identical while fixing "
      f"production code (Gate B). Restore `{path}` to its pinned Gate A contents, or if "
      "you genuinely need to add a new test case or fix a typo in Gate A itself, unlock "
      "explicitly:\n"
      f"`python3 ~/.gemini/config/hooks/gate_a_lock.py unlock --reason \"<why Gate A must "
      f"change>\" {shlex.quote(path)}`"
  )


def _is_gate_a_lock_cli_segment(seg: str) -> bool:
  """Returns True when a shell command segment solely invokes gate_a_lock.py."""
  try:
    tokens = shlex.split(seg, posix=True)
  except ValueError:
    return False
  idx = 0
  for _ in range(len(tokens)):
    if idx >= len(tokens):
      break
    tok = tokens[idx]
    if "=" in tok and not tok.startswith("-"):
      idx += 1
      continue
    base = os.path.basename(tok)
    if base in ("env", "nice", "nohup"):
      idx += 1
      continue
    if base in ("python", "python3"):
      idx += 1
      while idx < len(tokens) and tokens[idx].startswith("-"):
        idx += 1
      break
    break
  if idx >= len(tokens):
    return False
  return os.path.basename(tokens[idx]) == "gate_a_lock.py"


def _resolve_candidate_token(
    raw_tok: str, base_dirs: Sequence[str], active_locks: dict[str, dict]
) -> dict | None:
  """Returns the matching lock entry if raw_tok resolves to any locked realpath."""
  tok = raw_tok.strip().strip('"').strip("'")
  if tok.startswith("of="):
    tok = tok[3:].strip().strip('"').strip("'")
  if not tok or tok.startswith("-"):
    return None
  expanded = os.path.expanduser(tok)
  if os.path.isabs(expanded):
    return active_locks.get(os.path.realpath(expanded))
  for base in base_dirs:
    if not base:
      continue
    cand = os.path.realpath(os.path.join(base, expanded))
    if cand in active_locks:
      return active_locks[cand]
  return None


def _command_mutates_locked_path(
    cmd: str,
    active_locks: dict[str, dict],
    cwd: str,
    workspace_paths: Sequence[str],
) -> tuple[dict | None, bool]:
  """Checks if cmd mutates a locked path; returns (hit_entry, bypass_tamper_scan)."""
  stripped = HEREDOC_RE.sub(r"<<\3\4", cmd)
  segments = [s.strip() for s in re.split(r"&&|\|\||;|\||\n", stripped) if s.strip()]
  if not segments:
    return None, False

  base_dirs: list[str] = []
  for b in [cwd, *workspace_paths, *(e.get("worktree", "") for e in active_locks.values())]:
    if b:
      rb = os.path.realpath(os.path.expanduser(b))
      if rb not in base_dirs:
        base_dirs.append(rb)
  cur_cwd = base_dirs[0] if base_dirs else os.getcwd()
  if cur_cwd not in base_dirs:
    base_dirs.append(cur_cwd)

  bypass_tamper_scan = True
  for seg in segments:
    if _is_gate_a_lock_cli_segment(seg):
      continue

    # Check shell output redirections (> locked, >> locked, &> locked)
    for m_redir in REDIRECT_TARGET_RE.finditer(seg):
      hit = _resolve_candidate_token(m_redir.group(1), [cur_cwd, *base_dirs], active_locks)
      if hit is not None:
        return hit, False

    try:
      tokens = shlex.split(seg, posix=True)
    except ValueError:
      tokens = seg.split()
    if not tokens:
      continue

    idx = 0
    for _ in range(len(tokens)):
      if idx >= len(tokens):
        break
      t = tokens[idx]
      if "=" in t and not t.startswith("-"):
        idx += 1
        continue
      if os.path.basename(t) in ("env", "nice", "nohup"):
        idx += 1
        continue
      break
    if idx >= len(tokens):
      continue

    prog = os.path.basename(tokens[idx])
    args = tokens[idx + 1 :]
    if prog == "cd" and args:
      nxt_dir = os.path.expanduser(args[0])
      cur_cwd = os.path.realpath(
          nxt_dir if os.path.isabs(nxt_dir) else os.path.join(cur_cwd, nxt_dir)
      )
      if cur_cwd not in base_dirs:
        base_dirs.insert(0, cur_cwd)
      continue

    seg_is_tamper_restore = False
    cand_bases = [cur_cwd, *base_dirs]
    has_sed_inplace = prog in ("sed", "gsed") and any(
        a == "-i" or a.startswith("-i") or a.startswith("--in-place")
        or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
        for a in args
    )
    has_perl_inplace = prog == "perl" and any(
        a.startswith("-i") or (a.startswith("-") and not a.startswith("--") and "i" in a[1:])
        for a in args
    )
    has_awk_inplace = prog in ("awk", "gawk") and "inplace" in args

    if (
        has_sed_inplace
        or has_perl_inplace
        or has_awk_inplace
        or prog in ("tee", "rm", "unlink", "shred", "truncate", "touch", "patch")
    ):
      for a in args:
        hit = _resolve_candidate_token(a, cand_bases, active_locks)
        if hit is not None:
          return hit, False

    elif prog in ("cp", "mv", "install", "ln", "rsync"):
      non_flags = [a for a in args if not a.startswith("-")]
      for a in non_flags[1:] if len(non_flags) >= 2 else non_flags:
        hit = _resolve_candidate_token(a, cand_bases, active_locks)
        if hit is not None:
          return hit, False

    elif prog == "dd":
      for a in args:
        if a.startswith("of="):
          hit = _resolve_candidate_token(a, cand_bases, active_locks)
          if hit is not None:
            return hit, False

    elif prog == "git":
      git_dir = cur_cwd
      k = 0
      for _ in range(len(args)):
        if k >= len(args) or not args[k].startswith("-"):
          break
        if args[k] == "-C" and k + 1 < len(args):
          gd = os.path.expanduser(args[k + 1])
          git_dir = os.path.realpath(gd if os.path.isabs(gd) else os.path.join(cur_cwd, gd))
          k += 2
        elif args[k] in ("-c", "--git-dir", "--work-tree"):
          k += 2
        else:
          k += 1
      if k < len(args):
        subcmd = args[k]
        sub_args = args[k + 1 :]
        if subcmd in ("checkout", "restore", "reset", "rm", "mv", "clean", "apply"):
          restored_any_tampered = False
          for a in sub_args:
            hit = _resolve_candidate_token(a, [git_dir, *cand_bases], active_locks)
            if hit is not None:
              # Block reverting/clobbering an intact locked file; allow restoring a tampered one
              if subcmd in ("checkout", "restore"):
                on_disk = (
                    compute_file_sha256(hit["path"]) if os.path.isfile(hit["path"]) else ""
                )
                if not on_disk or on_disk.lower() != str(hit.get("sha256") or "").lower():
                  restored_any_tampered = True
                  continue
              return hit, False
          if restored_any_tampered:
            seg_is_tamper_restore = True

    elif prog in ("python", "python3", "node", "ruby", "perl", "bash", "sh", "zsh"):
      if SCRIPT_WRITE_HINT_RE.search(cmd):
        for real_p, entry in active_locks.items():
          bname = os.path.basename(real_p)
          if real_p in cmd or bname in cmd:
            return entry, False

    if not seg_is_tamper_restore:
      bypass_tamper_scan = False

  return None, bypass_tamper_scan


def denial_for_tool_call(
    tool_name: str,
    tool_args: dict,
    conv_id: str,
    parent_conv_id: str = "",
    transcript_path: str = "",
    artifact_dir: str = "",
    workspace_paths: Sequence[str] = (),
) -> str:
  """Returns a [GATE-A HASH LOCK] denial reason when a tool call violates an active lock."""
  if tool_name not in EDIT_TOOLS and tool_name != "run_command":
    return ""
  hint = transcript_path or artifact_dir
  active_locks = load_active_locks(hint_path=hint)
  if not active_locks:
    return ""

  caller_chain = resolve_chain_root(
      conv_id, parent_conv_id=parent_conv_id, transcript_path=transcript_path, hint_path=hint
  )
  own_locks = {
      p: entry
      for p, entry in active_locks.items()
      if not entry.get("chain_root") or (caller_chain and entry.get("chain_root") == caller_chain)
  }

  if tool_name in EDIT_TOOLS:
    raw_target = _extract_edit_target(tool_args)
    restored_target = ""
    if raw_target:
      real_target = os.path.realpath(
          os.path.expanduser(raw_target.strip().strip('"').strip("'"))
      )
      if real_target in active_locks:
        entry = active_locks[real_target]
        expected = str(entry.get("sha256") or "")
        on_disk = compute_file_sha256(real_target) if os.path.isfile(real_target) else ""
        if on_disk.lower() != expected.lower():
          restored = _simulated_edit_sha256(tool_name, tool_args, real_target)
          if restored and restored.lower() == expected.lower():
            restored_target = real_target
          else:
            return _format_edit_denial(entry)
        else:
          return _format_edit_denial(entry)

    for real_p, entry in sorted(own_locks.items()):
      if real_p == restored_target:
        continue
      expected = str(entry.get("sha256") or "")
      actual = compute_file_sha256(real_p) if os.path.isfile(real_p) else "<missing>"
      if actual.lower() != expected.lower():
        return _format_tamper_denial(entry, actual)
    return ""

  cmd_str = str(tool_args.get("CommandLine") or "").strip()
  if not cmd_str:
    return ""
  cwd = str(tool_args.get("Cwd") or "").strip()
  hit_entry, bypass_tamper_scan = _command_mutates_locked_path(
      cmd_str, active_locks, cwd, workspace_paths
  )
  if hit_entry is not None:
    return _format_edit_denial(hit_entry)
  if bypass_tamper_scan:
    return ""

  for real_p, entry in sorted(own_locks.items()):
    expected = str(entry.get("sha256") or "")
    actual = compute_file_sha256(real_p) if os.path.isfile(real_p) else "<missing>"
    if actual.lower() != expected.lower():
      return _format_tamper_denial(entry, actual)
  return ""


def _build_arg_parser() -> argparse.ArgumentParser:
  """Constructs the CLI argument parser for gate_a_lock.py."""
  parser = argparse.ArgumentParser(
      prog="gate_a_lock.py",
      description="SHA-256 hash lock for confirmed failing Gate A contract test files.",
  )
  sub = parser.add_subparsers(dest="command", required=True)

  p_lock = sub.add_parser("lock", help="Pin confirmed failing Gate A test file(s).")
  p_lock.add_argument("files", nargs="+", help="Gate A test file path(s) to lock.")
  p_lock.add_argument("--conv", default="", help="Conversation ID override.")
  p_lock.add_argument("--worktree", default="", help="Enclosing worktree override.")

  p_status = sub.add_parser("status", help="Show active Gate A hash locks.")
  p_status.add_argument("--conv", default="", help="Filter to conversation chain.")
  p_status.add_argument("--worktree", default="", help="Filter to worktree.")
  p_status.add_argument("--json", action="store_true", help="Emit JSON output.")

  p_verify = sub.add_parser("verify", help="Verify on-disk SHA-256 of locked Gate A files.")
  p_verify.add_argument("files", nargs="*", help="Optional specific file path(s) to verify.")
  p_verify.add_argument("--conv", default="", help="Conversation ID override.")
  p_verify.add_argument("--worktree", default="", help="Worktree override.")

  p_unlock = sub.add_parser("unlock", help="Unlock Gate A file(s) with an audit reason.")
  p_unlock.add_argument("files", nargs="*", help="Optional specific file path(s) to unlock.")
  p_unlock.add_argument(
      "--reason",
      default="",
      help="Required explanation for modifying Gate A (new test case or typo in Gate A).",
  )
  p_unlock.add_argument("--conv", default="", help="Conversation ID override.")
  p_unlock.add_argument("--worktree", default="", help="Worktree override.")

  p_clear = sub.add_parser("clear", help="Release intact Gate A lock(s) after Gate B passes.")
  p_clear.add_argument("files", nargs="*", help="Optional specific file path(s) to clear.")
  p_clear.add_argument("--conv", default="", help="Conversation ID override.")
  p_clear.add_argument("--worktree", default="", help="Worktree override.")
  return parser


def main(argv: Sequence[str] | None = None) -> int:
  """CLI entry point for gate_a_lock.py."""
  parser = _build_arg_parser()
  args = parser.parse_args(argv)

  try:
    if args.command == "lock":
      locked = lock_files(args.files, conv_id=args.conv, worktree=args.worktree)
      for entry in locked:
        chain_lbl = entry["chain_root"] or "local"
        print(
            f"Locked {entry['path']} (sha256={entry['sha256'][:12]}..., chain={chain_lbl})"
        )
      return 0

    if args.command == "status":
      active = load_active_locks()
      chain_root = resolve_chain_root(args.conv) if args.conv else ""
      selected, _ = _select_target_locks(active, (), chain_root, args.worktree)
      rows = []
      for real_p, entry in sorted(selected.items()):
        expected = str(entry.get("sha256") or "")
        if not os.path.isfile(real_p):
          actual, state = "<missing>", "MISSING"
        else:
          actual = compute_file_sha256(real_p)
          state = "OK" if actual.lower() == expected.lower() else "TAMPERED"
        rows.append({**entry, "actual_sha256": actual, "integrity": state})
      if args.json:
        print(json.dumps({"locks": rows}, ensure_ascii=False, indent=2))
      elif not rows:
        print("No active Gate A locks.")
      else:
        for r in rows:
          chain_lbl = r.get("chain_root") or "local"
          print(
              f"[{r['integrity']}] {r['path']} sha256={str(r['sha256'])[:12]}... "
              f"chain={chain_lbl} worktree={r.get('worktree', '')} "
              f"locked_at={r.get('locked_at_iso', '')}"
          )
      return 0

    if args.command == "verify":
      ok, errors, rows = verify_locks(
          args.files, conv_id=args.conv, worktree=args.worktree
      )
      if not ok:
        for err in errors:
          print(f"ERROR: {err}", file=sys.stderr)
        return 1
      print(f"Gate A hash lock verified ({len(rows)} file(s)).")
      return 0

    if args.command == "unlock":
      removed = unlock_files(
          args.reason, args.files, conv_id=args.conv, worktree=args.worktree
      )
      if not removed:
        print("No matching active Gate A locks to unlock.")
      else:
        for entry in removed:
          print(f"Unlocked {entry['path']} (reason: {args.reason.strip()})")
      return 0

    if args.command == "clear":
      cleared = clear_locks(args.files, conv_id=args.conv, worktree=args.worktree)
      if not cleared:
        print("No matching active Gate A locks to clear.")
      else:
        for entry in cleared:
          print(f"Cleared Gate A lock for {entry['path']}")
      return 0
  except (FileNotFoundError, ValueError) as exc:
    print(f"gate_a_lock.py {args.command}: {exc}", file=sys.stderr)
    return 2 if args.command in ("lock", "unlock") else 1

  return 0


if __name__ == "__main__":
  sys.exit(main())
