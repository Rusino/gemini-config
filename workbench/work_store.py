#!/usr/bin/env python3
"""SQLite WAL work graph, 3-valued obligation evaluator, and lane projection for Workbench."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
import contextlib
from datetime import datetime
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

DEFAULT_DB_PATH = os.path.expanduser(
    "~/.gemini/config/logs/workbench/work_graph.db"
)
ET_ZONE = ZoneInfo("America/New_York")
STALLED_OBLIGATION_DAYS = 7
CANDIDATE_TTL_DAYS = 7
HUB_BUG_COUNT_THRESHOLD = 3

CLOSED_BUG_STATUSES = frozenset({
    "FIXED",
    "VERIFIED",
    "CLOSED",
    "OBSOLETE",
    "WONTFIX",
    "DUPLICATE",
    "INTENDED_BEHAVIOR",
})

PILL_ICONS: dict[str, str] = {
    "gmail_thread": "✉",
    "gmail_draft": "📝",
    "gchat_thread": "💬",
    "gdoc": "📄",
    "buganizer": "🐞",
    "github_issue": "🐞",
    "gerrit": "🔧",
    "github_pr": "🔧",
    "piper": "🔧",
    "chat_chain": "🤖",
    "roadmap_item": "🗺",
}


def format_et_timestamp(ts: float) -> str:
  if not ts or ts <= 0:
    return ""
  dt = datetime.fromtimestamp(ts, tz=ET_ZONE)
  now_dt = datetime.now(tz=ET_ZONE)
  delta_s = max(0, int(time.time() - ts))
  if delta_s < 60:
    rel = "just now"
  elif delta_s < 3600:
    rel = f"{delta_s // 60}m ago"
  elif delta_s < 86400:
    rel = f"{delta_s // 3600}h ago"
  else:
    rel = f"{delta_s // 86400}d ago"
  if dt.date() == now_dt.date():
    return f"{dt.strftime('%H:%M')} ET ({rel})"
  return f"{dt.strftime('%b %-d at %H:%M')} ET ({rel})"


def format_short_et_date(ts: float) -> str:
  if not ts or ts <= 0:
    return ""
  dt = datetime.fromtimestamp(ts, tz=ET_ZONE)
  return dt.strftime("%b %-d")


_STATE_TITLE_PREFIXES = (
    "handoff state:",
    "handoff summary:",
    "chain state —",
    "chain state -",
    "chain state:",
    "state:",
)

_PLACEHOLDER_KEY_PREFIXES = (
    "gerrit:",
    "github_pr:",
    "github_issue:",
    "buganizer:",
    "piper:",
    "gmail_thread:",
    "gmail_draft:",
    "gchat_thread:",
    "chat_chain:",
)


def clean_work_title(raw_title: str) -> str:
  """Strips internal baton prefixes and auto-generated continuation footers from titles."""
  text = str(raw_title or "").strip()
  if not text:
    return ""
  for marker in (
      "\n\nThis prompt is auto-generated",
      "\nThis prompt is auto-generated",
      "\nNext immediate task:",
      "\nWhen finishing the task",
  ):
    pos = text.find(marker)
    if pos != -1:
      text = text[:pos].strip()
  text = " ".join(text.splitlines()).strip()
  lower = text.lower()
  for pref in _STATE_TITLE_PREFIXES:
    if lower.startswith(pref):
      text = text[len(pref) :].lstrip(" —-:").strip()
      break
  return text


def is_placeholder_title(title: str, artifact_key: str = "") -> bool:
  """Returns True when a title is an unexpanded artifact key or generic placeholder."""
  t = clean_work_title(title)
  if not t:
    return True
  if artifact_key and t == artifact_key:
    return True
  tl = t.lower()
  if any(tl.startswith(p) for p in _PLACEHOLDER_KEY_PREFIXES):
    return True
  if tl in ("goal", "untitled", "work"):
    return True
  if re.match(r"^(?:work|chat|draft|thread)\s+[0-9a-f]{6,}$", tl):
    return True
  return False


def resolve_work_origin_and_title(
    work_row: Mapping[str, Any],
    w_arts: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None]:
  """Determines the chronological/causal origin artifact and best human title for a Work."""
  raw_title = str(work_row.get("title") or "")
  cleaned = clean_work_title(raw_title)
  meta: dict[str, Any] = {}
  raw_meta = work_row.get("meta_json")
  if isinstance(raw_meta, str) and raw_meta:
    try:
      meta = json.loads(raw_meta)
    except Exception:
      meta = {}

  # Partition linked artifacts into human correspondence vs technical entities with creation timestamps
  correspondence_arts: list[tuple[float, dict[str, Any]]] = []
  other_arts: list[tuple[float, dict[str, Any]]] = []
  for a in w_arts:
    st = a.get("state") or {}
    ots = float(st.get("origin_ts") or st.get("created_ts") or 0.0)
    kind = str(a.get("kind") or "")
    # Group email and chat threads separately so correspondence can be tested as causal root
    if kind in ("gmail_thread", "gchat_thread"):
      correspondence_arts.append((ots, a))
    elif ots > 0 and a.get("role") != "context_only":
      other_arts.append((ots, a))

  correspondence_arts.sort(key=lambda x: (x[0] if x[0] > 0 else float("inf")))
  other_arts.sort(key=lambda x: x[0])

  origin_art: dict[str, Any] | None = None
  origin_ts = 0.0
  earliest_other_ts = other_arts[0][0] if other_arts else 0.0
  code_or_bug_count = sum(
      1
      for a in w_arts
      if a.get("kind") in ("gerrit", "github_pr", "piper", "buganizer", "github_issue")
  )

  # Human correspondence predating implementation (or coordinating >=2 items) is the root cause
  if correspondence_arts:
    c_ts, c_art = correspondence_arts[0]
    if (
        (c_ts > 0 and (earliest_other_ts <= 0 or c_ts <= earliest_other_ts + 86400))
        or code_or_bug_count >= 2
    ):
      origin_art = c_art
      origin_ts = c_ts
  # Fall back to the earliest technical or chat artifact when no correspondence predates the work
  if origin_art is None and other_arts:
    origin_ts, origin_art = other_arts[0]

  origin_info: dict[str, Any] | None = None
  if origin_art is not None:
    st = origin_art.get("state") or {}
    kind = str(origin_art.get("kind") or "")
    dt_str = format_short_et_date(origin_ts) if origin_ts > 0 else ""
    # Format Gmail provenance with direction (Julia <-> counterparty), latest reply date, and count
    if kind == "gmail_thread":
      cp = str(st.get("counterparty") or "").strip()
      started_by_me = bool(st.get("started_by_me", True))
      who = (
          f"Julia → {cp}"
          if (started_by_me and cp)
          else (f"{cp} → Julia" if cp else str(st.get("origin_from") or ""))
      )
      reply_ts = float(st.get("counterparty_reply_ts") or 0.0)
      reply_part = (
          f" · reply {format_short_et_date(reply_ts)}"
          if reply_ts > origin_ts + 60
          else ""
      )
      msg_cnt = int(st.get("message_count") or 0)
      cnt_part = f" · {msg_cnt} msgs" if msg_cnt > 1 else ""
      parts = [p for p in (dt_str, who) if p]
      detail = f" ({' · '.join(parts)}{reply_part}{cnt_part})" if parts else ""
      label = f"🌱 Origin: ✉ {st.get('root_subject') or origin_art.get('title')}{detail}"
    else:
      short_id = str(origin_art.get("artifact_key") or "").split(":")[-1][:12]
      dt_part = f" {dt_str}" if dt_str else ""
      label = f"🌱 Origin: {origin_art.get('icon', '🔗')}{dt_part} ({short_id})"
    origin_info = {
        "artifact_key": origin_art.get("artifact_key", ""),
        "kind": kind,
        "icon": origin_art.get("icon", "🔗"),
        "url": origin_art.get("url", ""),
        "origin_ts": origin_ts,
        "label": label,
    }

  # Preserve user-specified titles while still attaching computed origin provenance
  if meta.get("title_manual"):
    return raw_title, origin_info

  # When the root cause is a correspondence thread, promote its subject + counterparty as title
  if origin_art is not None and origin_art.get("kind") in ("gmail_thread", "gchat_thread"):
    st = origin_art.get("state") or {}
    root_subj = str(st.get("root_subject") or "").strip()
    # Strip reply/forward prefixes when falling back to the raw artifact title
    if not root_subj:
      root_subj = re.sub(
          r"^(?:re|fwd|fw)\s*:\s*",
          "",
          str(origin_art.get("title") or ""),
          flags=re.IGNORECASE,
      ).strip()
    cp = str(st.get("counterparty") or "").strip()
    # Append the counterparty name when not already present in the thread subject
    if root_subj:
      if cp and cp.lower() not in root_subj.lower():
        return f"{root_subj} — {cp}", origin_info
      return root_subj, origin_info

  if not is_placeholder_title(cleaned):
    return cleaned, origin_info

  # Upgrade placeholder/raw-key work titles from rich primary, channel, or context artifacts
  for role_filter in (("primary",), ("channel",), ("context_only",)):
    for a in w_arts:
      if a.get("role") not in role_filter:
        continue
      cand = clean_work_title(str(a.get("title") or ""))
      if not is_placeholder_title(cand, str(a.get("artifact_key") or "")):
        return cand, origin_info

  return (cleaned or raw_title), origin_info


def canonicalize_artifact_key(
    kind_or_key: str, target_id: str = "", host: str = ""
) -> str:
  """Normalizes external entity references into a single canonical artifact_key."""
  raw = str(kind_or_key or "").strip()
  tid = str(target_id or "").strip()
  if not tid and ":" in raw:
    parts = raw.split(":", 1)
    prefix = parts[0].lower().strip()
    rest = parts[1].strip()
    if prefix in ("gerrit", "cl"):
      if ":" in rest:
        sub, num = rest.split(":", 1)
        sub_l = sub.lower()
        repo = "chromium" if "chrom" in sub_l else "skia"
        return f"gerrit:{repo}:{num.strip()}"
      repo = "chromium" if "chrom" in host.lower() else "skia"
      return f"gerrit:{repo}:{rest}"
    if prefix in ("github", "github_pr", "pr"):
      if ":" in rest:
        return f"github_pr:{rest}"
      return f"github_pr:flutter/flutter:{rest.lstrip('#')}"
    if prefix in ("github_issue", "flutter_issue", "flutter"):
      if ":" in rest:
        return f"github_issue:{rest}"
      return f"github_issue:flutter/flutter:{rest.lstrip('#')}"
    if prefix in ("buganizer", "b", "crbug"):
      return f"buganizer:{rest}"
    if prefix in (
        "piper",
        "gmail_thread",
        "gmail_draft",
        "gchat_thread",
        "gdoc",
        "chat_chain",
        "roadmap_item",
    ):
      return f"{prefix}:{rest}"
    return raw

  k = raw.lower()
  if k in ("gerrit", "gerrit:skia", "skia_cl"):
    repo = "chromium" if "chrom" in host.lower() else "skia"
    return f"gerrit:{repo}:{tid}"
  if k in ("gerrit:chromium", "chromium_cl", "crrev"):
    return f"gerrit:chromium:{tid}"
  if k in ("github", "github_pr", "pr"):
    if ":" in tid:
      return f"github_pr:{tid}"
    return f"github_pr:flutter/flutter:{tid.lstrip('#')}"
  if k in ("github_issue", "flutter_issue", "flutter"):
    if ":" in tid:
      return f"github_issue:{tid}"
    return f"github_issue:flutter/flutter:{tid.lstrip('#')}"
  if k in ("buganizer", "b", "crbug"):
    return f"buganizer:{tid}"
  if k in (
      "piper",
      "gmail_thread",
      "gmail_draft",
      "gchat_thread",
      "gdoc",
      "chat_chain",
      "roadmap_item",
  ):
    return f"{k}:{tid}"
  return f"{k}:{tid}" if tid else raw


def artifact_kind_from_key(artifact_key: str) -> str:
  prefix = str(artifact_key or "").split(":", 1)[0].lower()
  if prefix in PILL_ICONS:
    return prefix
  return "gerrit"


def is_hub_artifact(
    artifact_key: str,
    state: Mapping[str, Any] | None = None,
    linked_bug_count: int = 0,
) -> bool:
  """Detects umbrella CLs/epics (e.g. [SkoreText] or >3 bugs) to prevent false merges."""
  st = state or {}
  subj = str(st.get("subject") or st.get("title") or "")
  # Force umbrella [SkoreText] CLs and DO NOT SUBMIT companion probes to context_only
  if "[SkoreText]" in subj or "SkoreText" in subj or "DO NOT SUBMIT" in subj:
    return True
  # Companion and cherry-pick CLs are review attributes and must not merge distinct works
  if st.get("companion_to") or st.get("is_companion") or st.get("is_cherry_pick"):
    return True
  bug_ids = st.get("bug_ids")
  count = max(
      linked_bug_count,
      len(bug_ids) if isinstance(bug_ids, (list, tuple, set)) else 0,
  )
  if artifact_key.startswith(("gerrit:", "github_pr:", "piper:")) and (
      count > HUB_BUG_COUNT_THRESHOLD
  ):
    return True
  return bool(st.get("is_hub"))


def _eval_single_predicate(
    pred: str,
    artifacts_by_key: Mapping[str, Mapping[str, Any]],
    work_row: Mapping[str, Any],
    now: float,
) -> str:
  expr = pred.strip()
  if not expr or expr.lower() == "always":
    return "true"

  m_merged = re.match(r"^cl_merged\(\s*([^)]+?)\s*\)$", expr, re.IGNORECASE)
  if m_merged:
    akey = canonicalize_artifact_key(m_merged.group(1))
    art = artifacts_by_key.get(akey)
    if not art:
      return "unknown"
    status = str((art.get("state") or {}).get("status") or "").upper()
    if not status:
      return "unknown"
    return "true" if status == "MERGED" else "false"

  m_roll = re.match(
      r"^rolled_into\(\s*([A-Za-z0-9_]+)\s*,\s*([^)]+?)\s*\)$",
      expr,
      re.IGNORECASE,
  )
  if m_roll:
    target = m_roll.group(1).strip()
    akey = canonicalize_artifact_key(m_roll.group(2))
    art = artifacts_by_key.get(akey)
    if not art:
      return "unknown"
    st = art.get("state") or {}
    status = str(st.get("status") or "").upper()
    if status and status != "MERGED":
      return "false"
    rolls = st.get("rolls")
    if not isinstance(rolls, Mapping) or not rolls:
      return "unknown"
    # Match target case-insensitively across Android, Chrome, Google3, Flutter
    roll_entry = None
    for rk, rv in rolls.items():
      if rk.lower() == target.lower():
        roll_entry = rv
        break
    if roll_entry is None:
      return "unknown"
    if isinstance(roll_entry, bool):
      return "true" if roll_entry else "false"
    if isinstance(roll_entry, Mapping):
      if "rolled" not in roll_entry or roll_entry.get("rolled") is None:
        return "unknown"
      return "true" if bool(roll_entry.get("rolled")) else "false"
    return "unknown"

  m_bug = re.match(r"^bug_closed\(\s*([^)]+?)\s*\)$", expr, re.IGNORECASE)
  if m_bug:
    akey = canonicalize_artifact_key(m_bug.group(1))
    art = artifacts_by_key.get(akey)
    if not art:
      return "unknown"
    status = str((art.get("state") or {}).get("status") or "").upper()
    if not status:
      return "unknown"
    return "true" if status in CLOSED_BUG_STATUSES else "false"

  m_stalled = re.match(r"^stalled_days\(\s*(\d+)\s*\)$", expr, re.IGNORECASE)
  if m_stalled:
    days = int(m_stalled.group(1))
    updated_at = float(work_row.get("updated_at") or 0.0)
    if updated_at <= 0:
      return "unknown"
    return "true" if (now - updated_at) >= days * 86400 else "false"

  return "unknown"


def evaluate_trigger(
    trigger_expr: str,
    artifacts_by_key: Mapping[str, Mapping[str, Any]],
    work_row: Mapping[str, Any],
    now: float | None = None,
) -> str:
  """Evaluates a conjunction of predicates into 3-valued logic ('true'|'false'|'unknown')."""
  cur_now = time.time() if now is None else now
  raw = str(trigger_expr or "").strip()
  if not raw:
    return "true"
  parts = [p.strip() for p in re.split(r"\bAND\b", raw, flags=re.IGNORECASE) if p.strip()]
  has_unknown = False
  for part in parts:
    val = _eval_single_predicate(part, artifacts_by_key, work_row, cur_now)
    if val == "false":
      return "false"
    if val == "unknown":
      has_unknown = True
  return "unknown" if has_unknown else "true"


def _is_done_matched(
    obligation: Mapping[str, Any],
    artifacts_by_key: Mapping[str, Mapping[str, Any]],
) -> bool:
  """Checks whether a post-creation reply or draft send satisfies the obligation."""
  created_at = float(obligation.get("created_at") or 0.0)
  target_key = str(obligation.get("target_artifact_key") or "").strip()
  matcher = str(obligation.get("done_matcher") or "").strip()

  m_reply = re.match(r"^reply_sent\(\s*([^)]+?)\s*\)$", matcher, re.IGNORECASE)
  check_key = canonicalize_artifact_key(m_reply.group(1)) if m_reply else target_key
  if not check_key:
    return False
  art = artifacts_by_key.get(check_key)
  if not art:
    return False
  st = art.get("state") or {}
  kind = str(art.get("kind") or "")
  if kind == "gmail_draft" and str(st.get("status") or "").upper() == "SENT":
    return True
  if kind in ("gmail_thread", "gchat_thread"):
    last_from_me = bool(st.get("last_from_me"))
    last_ts = float(st.get("last_message_ts") or 0.0)
    if last_from_me and last_ts > created_at:
      return True
  return False


class WorkStore:
  """Thread-safe SQLite WAL backing store for Works, Artifacts, Links, and Obligations."""

  def __init__(self, db_path: str | None = None) -> None:
    self.db_path = os.path.abspath(
        os.path.expanduser(
            db_path or os.environ.get("WORKBENCH_DB_PATH") or DEFAULT_DB_PATH
        )
    )
    os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
    self._lock = threading.RLock()
    self._init_schema()

  @contextlib.contextmanager
  def _connect(self) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(self.db_path, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
      with conn:
        yield conn
    finally:
      conn.close()

  def _init_schema(self) -> None:
    with self._lock, self._connect() as conn:
      conn.execute("PRAGMA journal_mode=WAL")
      conn.executescript("""
        CREATE TABLE IF NOT EXISTS works (
          work_id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'active',
          project_id TEXT DEFAULT '',
          roadmap_item_key TEXT DEFAULT '',
          created_at REAL NOT NULL,
          updated_at REAL NOT NULL,
          snooze_until REAL DEFAULT 0,
          meta_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS artifacts (
          artifact_key TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          title TEXT DEFAULT '',
          url TEXT DEFAULT '',
          state_json TEXT DEFAULT '{}',
          updated_at REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS work_links (
          work_id TEXT NOT NULL,
          artifact_key TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'primary',
          source TEXT NOT NULL DEFAULT 'auto',
          confidence REAL DEFAULT 1.0,
          created_at REAL NOT NULL,
          PRIMARY KEY (work_id, artifact_key)
        );
        CREATE TABLE IF NOT EXISTS negative_links (
          work_id TEXT NOT NULL,
          artifact_key TEXT NOT NULL,
          created_at REAL NOT NULL,
          PRIMARY KEY (work_id, artifact_key)
        );
        CREATE TABLE IF NOT EXISTS obligations (
          obligation_id TEXT PRIMARY KEY,
          work_id TEXT NOT NULL,
          target_artifact_key TEXT DEFAULT '',
          kind TEXT NOT NULL DEFAULT 'reply',
          description TEXT NOT NULL,
          trigger_expr TEXT NOT NULL DEFAULT 'always',
          done_matcher TEXT DEFAULT '',
          status TEXT NOT NULL DEFAULT 'open',
          last_eval TEXT NOT NULL DEFAULT 'unknown',
          created_at REAL NOT NULL,
          satisfied_at REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS candidate_buffer (
          artifact_key TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          title TEXT DEFAULT '',
          url TEXT DEFAULT '',
          chat_chain_id TEXT DEFAULT '',
          first_seen_at REAL NOT NULL,
          last_seen_at REAL NOT NULL,
          state_json TEXT DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS events (
          event_id INTEGER PRIMARY KEY AUTOINCREMENT,
          ts REAL NOT NULL,
          event_type TEXT NOT NULL,
          work_id TEXT DEFAULT '',
          artifact_key TEXT DEFAULT '',
          payload_json TEXT DEFAULT '{}'
        );
      """)

  def _log_event(
      self,
      conn: sqlite3.Connection,
      event_type: str,
      work_id: str = "",
      artifact_key: str = "",
      payload: Mapping[str, Any] | None = None,
      now: float | None = None,
  ) -> None:
    ts = time.time() if now is None else now
    conn.execute(
        "INSERT INTO events (ts, event_type, work_id, artifact_key, payload_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (ts, event_type, work_id, artifact_key, json.dumps(dict(payload or {}))),
    )

  def create_work(
      self,
      title: str,
      project_id: str = "",
      roadmap_item_key: str = "",
      work_id: str = "",
      meta: Mapping[str, Any] | None = None,
      now: float | None = None,
  ) -> str:
    cur_now = time.time() if now is None else now
    if not work_id:
      digest = hashlib.sha1(f"{title}:{project_id}:{cur_now}".encode("utf-8")).hexdigest()[:10]
      work_id = f"work-{digest}"
    with self._lock, self._connect() as conn:
      conn.execute(
          "INSERT OR REPLACE INTO works "
          "(work_id, title, status, project_id, roadmap_item_key, created_at, updated_at, meta_json) "
          "VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
          (
              work_id,
              title.strip(),
              project_id.strip(),
              roadmap_item_key.strip(),
              cur_now,
              cur_now,
              json.dumps(dict(meta or {})),
          ),
      )
      self._log_event(
          conn,
          "work_minted",
          work_id=work_id,
          payload={"title": title, "project_id": project_id},
          now=cur_now,
      )
    return work_id

  def find_work_by_artifact(
      self, artifact_key: str, roles: tuple[str, ...] = ("primary", "channel")
  ) -> str | None:
    akey = canonicalize_artifact_key(artifact_key)
    placeholders = ",".join("?" for _ in roles)
    with self._lock, self._connect() as conn:
      row = conn.execute(
          f"SELECT wl.work_id FROM work_links wl "
          f"JOIN works w ON w.work_id = wl.work_id "
          f"WHERE wl.artifact_key = ? AND wl.role IN ({placeholders}) "
          f"AND w.status NOT IN ('done', 'archived') "
          f"ORDER BY w.updated_at DESC LIMIT 1",
          (akey, *roles),
      ).fetchone()
      return str(row["work_id"]) if row else None

  def get_artifact(self, artifact_key: str) -> dict[str, Any] | None:
    akey = canonicalize_artifact_key(artifact_key)
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT * FROM artifacts WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      return dict(row) if row else None

  def upsert_artifact(
      self,
      artifact_key: str,
      kind: str = "",
      title: str = "",
      url: str = "",
      state: Mapping[str, Any] | None = None,
      now: float | None = None,
  ) -> str:
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    akind = kind or artifact_kind_from_key(akey)
    with self._lock, self._connect() as conn:
      existing = conn.execute(
          "SELECT title, url, state_json FROM artifacts WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      merged_state: dict[str, Any] = {}
      if existing and existing["state_json"]:
        try:
          merged_state = json.loads(existing["state_json"])
        except Exception:
          merged_state = {}
      if state:
        # Preserve the earliest known creation timestamp across incremental sync passes
        prev_ots = float(merged_state.get("origin_ts") or 0.0)
        new_ots = float(state.get("origin_ts") or 0.0)
        merged_state.update(dict(state))
        if prev_ots > 0 and new_ots > 0:
          merged_state["origin_ts"] = min(prev_ots, new_ots)
        elif prev_ots > 0:
          merged_state["origin_ts"] = prev_ots
      # Never overwrite a descriptive stored title with a raw artifact_key placeholder
      existing_title = str(existing["title"] or "") if existing else ""
      if (
          existing_title
          and not is_placeholder_title(existing_title, akey)
          and is_placeholder_title(title, akey)
      ):
        final_title = existing_title
      else:
        final_title = title or existing_title or akey
      final_url = url or (existing["url"] if existing else "")
      conn.execute(
          "INSERT OR REPLACE INTO artifacts "
          "(artifact_key, kind, title, url, state_json, updated_at) "
          "VALUES (?, ?, ?, ?, ?, ?)",
          (
              akey,
              akind,
              final_title,
              final_url,
              json.dumps(merged_state),
              cur_now,
          ),
      )
    return akey

  def record_candidate(
      self,
      artifact_key: str,
      kind: str = "",
      title: str = "",
      url: str = "",
      chat_chain_id: str = "",
      state: Mapping[str, Any] | None = None,
      now: float | None = None,
  ) -> None:
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    akind = kind or artifact_kind_from_key(akey)
    with self._lock, self._connect() as conn:
      existing = conn.execute(
          "SELECT first_seen_at, title, url, state_json FROM candidate_buffer "
          "WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      first_seen = float(existing["first_seen_at"]) if existing else cur_now
      merged_state: dict[str, Any] = {}
      if existing and existing["state_json"]:
        try:
          merged_state = json.loads(existing["state_json"])
        except Exception:
          merged_state = {}
      if state:
        merged_state.update(dict(state))
      conn.execute(
          "INSERT OR REPLACE INTO candidate_buffer "
          "(artifact_key, kind, title, url, chat_chain_id, first_seen_at, last_seen_at, state_json) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (
              akey,
              akind,
              title or (existing["title"] if existing else "") or akey,
              url or (existing["url"] if existing else ""),
              chat_chain_id,
              first_seen,
              cur_now,
              json.dumps(merged_state),
          ),
      )

  def list_candidates(self, now: float | None = None) -> list[dict[str, Any]]:
    cur_now = time.time() if now is None else now
    cutoff = cur_now - CANDIDATE_TTL_DAYS * 86400
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT * FROM candidate_buffer WHERE last_seen_at >= ? ORDER BY last_seen_at DESC",
          (cutoff,),
      ).fetchall()
      return [dict(r) for r in rows]

  def link_artifact(
      self,
      work_id: str,
      artifact_key: str,
      role: str = "primary",
      source: str = "auto",
      confidence: float = 1.0,
      now: float | None = None,
  ) -> str | None:
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    with self._lock, self._connect() as conn:
      tomb = conn.execute(
          "SELECT 1 FROM negative_links WHERE work_id = ? AND artifact_key = ?",
          (work_id, akey),
      ).fetchone()
      if tomb:
        if source != "manual":
          return None
        conn.execute(
            "DELETE FROM negative_links WHERE work_id = ? AND artifact_key = ?",
            (work_id, akey),
        )

      art_row = conn.execute(
          "SELECT state_json FROM artifacts WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      art_state: dict[str, Any] = {}
      if art_row and art_row["state_json"]:
        try:
          art_state = json.loads(art_row["state_json"])
        except Exception:
          art_state = {}
      effective_role = role
      if is_hub_artifact(akey, art_state) and source != "manual":
        effective_role = "context_only"

      conn.execute(
          "INSERT OR REPLACE INTO work_links "
          "(work_id, artifact_key, role, source, confidence, created_at) "
          "VALUES (?, ?, ?, ?, ?, ?)",
          (work_id, akey, effective_role, source, confidence, cur_now),
      )
      conn.execute(
          "UPDATE works SET updated_at = ? WHERE work_id = ?",
          (cur_now, work_id),
      )
      self._log_event(
          conn,
          "link_added",
          work_id=work_id,
          artifact_key=akey,
          payload={"role": effective_role, "source": source},
          now=cur_now,
      )
      return effective_role

  def unlink_artifact(
      self, work_id: str, artifact_key: str, now: float | None = None
  ) -> None:
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    with self._lock, self._connect() as conn:
      conn.execute(
          "DELETE FROM work_links WHERE work_id = ? AND artifact_key = ?",
          (work_id, akey),
      )
      conn.execute(
          "INSERT OR REPLACE INTO negative_links (work_id, artifact_key, created_at) "
          "VALUES (?, ?, ?)",
          (work_id, akey, cur_now),
      )
      self._log_event(
          conn,
          "link_detached",
          work_id=work_id,
          artifact_key=akey,
          now=cur_now,
      )

  def get_work_artifact_keys(self, work_id: str) -> list[str]:
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT artifact_key FROM work_links WHERE work_id = ? ORDER BY created_at ASC",
          (work_id,),
      ).fetchall()
      return [str(r["artifact_key"]) for r in rows]

  def get_work_artifact_roles(self, work_id: str) -> dict[str, str]:
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT artifact_key, role FROM work_links WHERE work_id = ?",
          (work_id,),
      ).fetchall()
      return {str(r["artifact_key"]): str(r["role"]) for r in rows}

  def add_obligation(
      self,
      work_id: str,
      description: str,
      trigger_expr: str = "always",
      target_artifact_key: str = "",
      kind: str = "reply",
      done_matcher: str = "",
      obligation_id: str = "",
      now: float | None = None,
  ) -> str:
    cur_now = time.time() if now is None else now
    tkey = canonicalize_artifact_key(target_artifact_key) if target_artifact_key else ""
    if not obligation_id:
      digest = hashlib.sha1(
          f"{work_id}:{description}:{trigger_expr}:{tkey}".encode("utf-8")
      ).hexdigest()[:10]
      obligation_id = f"ob-{digest}"
    with self._lock, self._connect() as conn:
      existing = conn.execute(
          "SELECT status FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      if existing and existing["status"] == "done":
        return obligation_id
      conn.execute(
          "INSERT OR REPLACE INTO obligations "
          "(obligation_id, work_id, target_artifact_key, kind, description, "
          "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 'unknown', ?, 0)",
          (
              obligation_id,
              work_id,
              tkey,
              kind,
              description.strip(),
              trigger_expr.strip() or "always",
              done_matcher.strip(),
              cur_now,
          ),
      )
      self._log_event(
          conn,
          "obligation_created",
          work_id=work_id,
          artifact_key=tkey,
          payload={"obligation_id": obligation_id, "trigger_expr": trigger_expr},
          now=cur_now,
      )
    self.evaluate_all_obligations(now=cur_now)
    return obligation_id

  def mark_obligation_done(
      self, obligation_id: str, now: float | None = None
  ) -> None:
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT work_id, target_artifact_key FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      conn.execute(
          "UPDATE obligations SET status = 'done', satisfied_at = ? WHERE obligation_id = ?",
          (cur_now, obligation_id),
      )
      if row:
        self._log_event(
            conn,
            "obligation_done",
            work_id=str(row["work_id"]),
            artifact_key=str(row["target_artifact_key"] or ""),
            payload={"obligation_id": obligation_id},
            now=cur_now,
        )

  def get_obligation(
      self, obligation_id: str, now: float | None = None
  ) -> dict[str, Any] | None:
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT * FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      if not row:
        return None
      d = dict(row)
      age_s = cur_now - float(d.get("created_at") or cur_now)
      d["stalled_alert"] = (
          d.get("status") == "open"
          and d.get("last_eval") in ("false", "unknown")
          and age_s >= STALLED_OBLIGATION_DAYS * 86400
      )
      return d

  def evaluate_all_obligations(self, now: float | None = None) -> None:
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      art_rows = conn.execute("SELECT * FROM artifacts").fetchall()
      artifacts_by_key: dict[str, dict[str, Any]] = {}
      for ar in art_rows:
        ad = dict(ar)
        try:
          ad["state"] = json.loads(ad.get("state_json") or "{}")
        except Exception:
          ad["state"] = {}
        artifacts_by_key[str(ad["artifact_key"])] = ad

      work_rows = {
          str(r["work_id"]): dict(r)
          for r in conn.execute("SELECT * FROM works").fetchall()
      }
      ob_rows = conn.execute(
          "SELECT * FROM obligations WHERE status IN ('open', 'ready')"
      ).fetchall()
      for obr in ob_rows:
        ob = dict(obr)
        ob_id = str(ob["obligation_id"])
        w_row = work_rows.get(str(ob["work_id"]), {})
        if _is_done_matched(ob, artifacts_by_key):
          conn.execute(
              "UPDATE obligations SET status = 'done', satisfied_at = ? "
              "WHERE obligation_id = ?",
              (cur_now, ob_id),
          )
          self._log_event(
              conn,
              "obligation_done",
              work_id=str(ob["work_id"]),
              artifact_key=str(ob.get("target_artifact_key") or ""),
              payload={"obligation_id": ob_id, "auto": True},
              now=cur_now,
          )
          continue

        res = evaluate_trigger(
            str(ob.get("trigger_expr") or "always"),
            artifacts_by_key,
            w_row,
            now=cur_now,
        )
        new_status = "ready" if res == "true" else "open"
        if new_status != ob.get("status") or res != ob.get("last_eval"):
          conn.execute(
              "UPDATE obligations SET status = ?, last_eval = ? WHERE obligation_id = ?",
              (new_status, res, ob_id),
          )
          if new_status == "ready" and ob.get("status") != "ready":
            self._log_event(
                conn,
                "obligation_ready",
                work_id=str(ob["work_id"]),
                artifact_key=str(ob.get("target_artifact_key") or ""),
                payload={"obligation_id": ob_id},
                now=cur_now,
            )

  def merge_works(
      self, target_work_id: str, source_work_id: str, now: float | None = None
  ) -> None:
    if target_work_id == source_work_id:
      return
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      conn.execute(
          "INSERT OR IGNORE INTO work_links "
          "(work_id, artifact_key, role, source, confidence, created_at) "
          "SELECT ?, artifact_key, role, 'manual', confidence, ? "
          "FROM work_links WHERE work_id = ?",
          (target_work_id, cur_now, source_work_id),
      )
      conn.execute(
          "UPDATE obligations SET work_id = ? WHERE work_id = ?",
          (target_work_id, source_work_id),
      )
      conn.execute(
          "DELETE FROM work_links WHERE work_id = ?",
          (source_work_id,),
      )
      conn.execute(
          "UPDATE works SET status = 'archived', updated_at = ? WHERE work_id = ?",
          (cur_now, source_work_id),
      )
      conn.execute(
          "UPDATE works SET updated_at = ? WHERE work_id = ?",
          (cur_now, target_work_id),
      )
      self._log_event(
          conn,
          "work_merged",
          work_id=target_work_id,
          payload={"source_work_id": source_work_id},
          now=cur_now,
      )

  def split_work(
      self,
      source_work_id: str,
      artifact_key: str,
      new_title: str,
      now: float | None = None,
  ) -> str:
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    self.unlink_artifact(source_work_id, akey, now=cur_now)
    new_wid = self.create_work(new_title, now=cur_now)
    self.link_artifact(new_wid, akey, role="primary", source="manual", now=cur_now)
    with self._lock, self._connect() as conn:
      conn.execute(
          "UPDATE obligations SET work_id = ? "
          "WHERE work_id = ? AND target_artifact_key = ?",
          (new_wid, source_work_id, akey),
      )
      self._log_event(
          conn,
          "work_split",
          work_id=new_wid,
          artifact_key=akey,
          payload={"from_work_id": source_work_id},
          now=cur_now,
      )
    return new_wid

  def complete_work(
      self,
      work_id: str,
      roadmap_cb: Callable[[str, str], None] | None = None,
      now: float | None = None,
  ) -> None:
    cur_now = time.time() if now is None else now
    roadmap_key = ""
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT roadmap_item_key FROM works WHERE work_id = ?",
          (work_id,),
      ).fetchone()
      if row:
        roadmap_key = str(row["roadmap_item_key"] or "").strip()
      conn.execute(
          "UPDATE works SET status = 'done', updated_at = ? WHERE work_id = ?",
          (cur_now, work_id),
      )
      conn.execute(
          "UPDATE obligations SET status = 'done', satisfied_at = ? "
          "WHERE work_id = ? AND status IN ('open', 'ready')",
          (cur_now, work_id),
      )
      self._log_event(conn, "work_done", work_id=work_id, now=cur_now)

    if roadmap_key and ":" in roadmap_key and roadmap_cb is not None:
      proj_id, item_id = roadmap_key.split(":", 1)
      roadmap_cb(proj_id.strip(), item_id.strip())

  def list_works(self, include_archived: bool = False) -> list[dict[str, Any]]:
    with self._lock, self._connect() as conn:
      if include_archived:
        rows = conn.execute(
            "SELECT * FROM works ORDER BY updated_at DESC"
        ).fetchall()
      else:
        rows = conn.execute(
            "SELECT * FROM works WHERE status != 'archived' ORDER BY updated_at DESC"
        ).fetchall()
      return [dict(r) for r in rows]

  def compute_metrics(
      self, days: int = 14, now: float | None = None
  ) -> dict[str, Any]:
    cur_now = time.time() if now is None else now
    cutoff = cur_now - days * 86400
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT event_type, COUNT(*) AS cnt FROM events "
          "WHERE ts >= ? GROUP BY event_type",
          (cutoff,),
      ).fetchall()
      counts = {str(r["event_type"]): int(r["cnt"]) for r in rows}
      added = counts.get("link_added", 0)
      detached = counts.get("link_detached", 0)
      precision = 1.0 if added == 0 else max(0.0, (added - detached) / added)
      return {
          "window_days": days,
          "work_minted": counts.get("work_minted", 0),
          "link_added": added,
          "link_detached": detached,
          "work_merged": counts.get("work_merged", 0),
          "work_split": counts.get("work_split", 0),
          "obligation_created": counts.get("obligation_created", 0),
          "obligation_ready": counts.get("obligation_ready", 0),
          "obligation_done": counts.get("obligation_done", 0),
          "work_done": counts.get("work_done", 0),
          "link_precision": round(precision, 4),
      }

  def compute_work_lane(
      self,
      work_row: Mapping[str, Any],
      artifacts: list[dict[str, Any]],
      obligations: list[dict[str, Any]],
  ) -> tuple[str, list[str]]:
    """Computes unified Work lane ('needs-you'|'waiting'|'blocked'|'done') and reasons."""
    status = str(work_row.get("status") or "active")
    if status == "done":
      return "done", ["Completed"]
    if status == "blocked":
      return "blocked", ["Blocked"]

    reasons: list[str] = []
    for ob in obligations:
      if ob.get("status") == "ready":
        reasons.append(f"🔔 Ready: {ob.get('description')}")
      elif ob.get("stalled_alert"):
        reasons.append(f"⚠️ Stalled >7d: {ob.get('description')}")

    for art in artifacts:
      if art.get("role") == "context_only":
        continue
      kind = str(art.get("kind") or "")
      st = art.get("state") or {}
      if kind == "gmail_draft" and str(st.get("status") or "DRAFT").upper() == "DRAFT":
        reasons.append(f"📝 Unsent draft: {art.get('title')}")
      elif kind in ("gmail_thread", "gchat_thread") and bool(st.get("unanswered_human")):
        reasons.append(f"📨 Unanswered message: {art.get('title')}")
      elif kind == "chat_chain" and str(st.get("bucket") or "") == "needs-me":
        reasons.append(f"🤖 Chat waiting on you: {art.get('title')}")
      elif kind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue"):
        est = st.get("entity_state") or {}
        if est.get("category") == "needs_me" or bool(est.get("action_required")):
          reasons.append(str(est.get("reason") or f"Needs action: {art.get('title')}"))

    if reasons:
      return "needs-you", reasons

    open_obs = [ob for ob in obligations if ob.get("status") in ("open", "ready")]
    active_cls = [
        a
        for a in artifacts
        if a.get("role") != "context_only"
        and a.get("kind") in ("gerrit", "github_pr", "piper")
        and str((a.get("state") or {}).get("status") or "NEW").upper() == "NEW"
    ]
    open_bugs = [
        a
        for a in artifacts
        if a.get("role") != "context_only"
        and a.get("kind") in ("buganizer", "github_issue")
        and str((a.get("state") or {}).get("status") or "NEW").upper()
        not in CLOSED_BUG_STATUSES
    ]
    if not open_obs and not active_cls and not open_bugs and artifacts:
      # Check if all primary CLs are merged and there are no unsent drafts
      has_merged = any(
          str((a.get("state") or {}).get("status") or "").upper() == "MERGED"
          for a in artifacts
      )
      if has_merged:
        return "done", ["All CLs merged and obligations satisfied"]

    wait_reasons: list[str] = []
    for ob in open_obs:
      wait_reasons.append(f"⏳ Waiting: {ob.get('description')} ({ob.get('trigger_expr')})")
    if not wait_reasons:
      wait_reasons.append("In progress / waiting on external actors")
    return "waiting", wait_reasons

  def build_dashboard_projection(
      self,
      cl_watcher_state: Mapping[str, Any] | None = None,
      unanswered_threads: list[dict[str, Any]] | None = None,
      now: float | None = None,
  ) -> dict[str, Any]:
    """Builds the complete UI projection combining persisted Works and virtual 1-entity cards."""
    cur_now = time.time() if now is None else now
    self.evaluate_all_obligations(now=cur_now)

    with self._lock, self._connect() as conn:
      work_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM works WHERE status != 'archived' ORDER BY updated_at DESC"
          ).fetchall()
      ]
      art_rows = {
          str(r["artifact_key"]): dict(r)
          for r in conn.execute("SELECT * FROM artifacts").fetchall()
      }
      for ad in art_rows.values():
        try:
          ad["state"] = json.loads(ad.get("state_json") or "{}")
        except Exception:
          ad["state"] = {}

      link_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM work_links ORDER BY created_at ASC"
          ).fetchall()
      ]
      ob_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM obligations ORDER BY created_at ASC"
          ).fetchall()
      ]

    links_by_work: dict[str, list[dict[str, Any]]] = {}
    bound_artifact_keys: set[str] = set()
    for lr in link_rows:
      wid = str(lr["work_id"])
      akey = str(lr["artifact_key"])
      bound_artifact_keys.add(akey)
      links_by_work.setdefault(wid, []).append(lr)

    obs_by_work: dict[str, list[dict[str, Any]]] = {}
    for obr in ob_rows:
      wid = str(obr["work_id"])
      age_s = cur_now - float(obr.get("created_at") or cur_now)
      obr["stalled_alert"] = (
          obr.get("status") == "open"
          and obr.get("last_eval") in ("false", "unknown")
          and age_s >= STALLED_OBLIGATION_DAYS * 86400
      )
      obs_by_work.setdefault(wid, []).append(obr)

    cards: list[dict[str, Any]] = []
    title_updates: list[tuple[str, str]] = []
    for w in work_rows:
      wid = str(w["work_id"])
      w_links = links_by_work.get(wid, [])
      w_arts: list[dict[str, Any]] = []
      kinds_seen: set[str] = set()
      for wl in w_links:
        akey = str(wl["artifact_key"])
        ar = art_rows.get(akey, {
            "artifact_key": akey,
            "kind": artifact_kind_from_key(akey),
            "title": akey,
            "url": "",
            "state": {},
        })
        kind = str(ar.get("kind") or artifact_kind_from_key(akey))
        kinds_seen.add(kind)
        w_arts.append({
            "artifact_key": akey,
            "kind": kind,
            "icon": PILL_ICONS.get(kind, "🔗"),
            "title": clean_work_title(str(ar.get("title") or akey)) or akey,
            "url": str(ar.get("url") or ""),
            "role": str(wl.get("role") or "primary"),
            "source": str(wl.get("source") or "auto"),
            "state": ar.get("state") or {},
        })
      w_obs = obs_by_work.get(wid, [])
      # Resolve causal origin and upgrade downstream/placeholder work titles
      resolved_title, origin_info = resolve_work_origin_and_title(w, w_arts)
      if resolved_title and resolved_title != str(w["title"]):
        title_updates.append((resolved_title, wid))
      lane, reasons = self.compute_work_lane(w, w_arts, w_obs)
      cards.append({
          "work_id": wid,
          "title": resolved_title or str(w["title"]),
          "origin": origin_info,
          "status": str(w["status"]),
          "lane": lane,
          "reasons": reasons,
          "project_id": str(w.get("project_id") or ""),
          "roadmap_item_key": str(w.get("roadmap_item_key") or ""),
          "updated_at": float(w.get("updated_at") or cur_now),
          "updated_et": format_et_timestamp(float(w.get("updated_at") or cur_now)),
          "is_virtual": False,
          "is_multi_channel": len(kinds_seen) >= 2 or len(w_arts) >= 2 or len(w_obs) >= 1,
          "artifacts": w_arts,
          "obligations": w_obs,
      })

    # Persist upgraded titles back to SQLite so CLI and subsequent reads stay consistent
    if title_updates:
      with self._lock, self._connect() as conn:
        conn.executemany(
            "UPDATE works SET title = ? WHERE work_id = ?",
            title_updates,
        )

    # Project standalone unbound CLs and bugs from cl_watcher_state in-memory (0 SQLite writes)
    cl_items = (cl_watcher_state or {}).get("items") or {}
    if isinstance(cl_items, Mapping):
      for raw_key, item in cl_items.items():
        if not isinstance(item, Mapping):
          continue
        if item.get("companion_to") or item.get("is_companion") or item.get("is_cherry_pick"):
          continue
        status = str(item.get("status") or "NEW").upper()
        if status in ("ABANDONED", "CLOSED"):
          continue
        kind = str(item.get("kind") or "gerrit")
        tid = str(item.get("target_id") or raw_key)
        host = str(item.get("host") or "")
        c_key = canonicalize_artifact_key(kind, tid, host=host)
        if c_key in bound_artifact_keys:
          continue
        est = item.get("entity_state") or {}
        if status == "MERGED" and not est.get("action_required"):
          continue
        lane = (
            "needs-you"
            if (est.get("category") == "needs_me" or est.get("action_required"))
            else "waiting"
        )
        reason = str(est.get("reason") or status)
        updated_ts = float(item.get("updated_ts") or cur_now)
        cards.append({
            "work_id": f"virt-{c_key}",
            "title": str(item.get("subject") or item.get("title") or c_key),
            "status": "active",
            "lane": lane,
            "reasons": [reason] if reason else [],
            "project_id": str(item.get("project") or ""),
            "roadmap_item_key": "",
            "updated_at": updated_ts,
            "updated_et": format_et_timestamp(updated_ts),
            "is_virtual": True,
            "is_multi_channel": False,
            "artifacts": [{
                "artifact_key": c_key,
                "kind": artifact_kind_from_key(c_key),
                "icon": PILL_ICONS.get(artifact_kind_from_key(c_key), "🔧"),
                "title": f"{c_key.split(':')[-1]} · {item.get('subject') or ''}".strip(" ·"),
                "url": str(item.get("url") or ""),
                "role": "primary",
                "source": "virtual",
                "state": dict(item),
            }],
            "obligations": [],
        })

    lane_rank = {"needs-you": 0, "waiting": 1, "blocked": 2, "done": 3}
    cards.sort(
        key=lambda c: (
            lane_rank.get(c["lane"], 9),
            0 if not c["is_virtual"] else 1,
            -float(c.get("updated_at") or 0.0),
        )
    )
    tray = list(unanswered_threads or [])
    counts = {
        "needs_you": sum(1 for c in cards if c["lane"] == "needs-you"),
        "waiting": sum(1 for c in cards if c["lane"] == "waiting"),
        "done": sum(1 for c in cards if c["lane"] == "done"),
        "multi_channel": sum(1 for c in cards if c.get("is_multi_channel")),
        "obligations_open": sum(
            1
            for c in cards
            for o in c.get("obligations", [])
            if o.get("status") in ("open", "ready")
        ),
        "unanswered": len(tray),
    }
    return {
        "works": cards,
        "unanswered_tray": tray,
        "candidates": self.list_candidates(now=cur_now)[:25],
        "metrics": self.compute_metrics(days=14, now=cur_now),
        "counts": counts,
        "synced_et": format_et_timestamp(cur_now),
    }
