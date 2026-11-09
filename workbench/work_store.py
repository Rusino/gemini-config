#!/usr/bin/env python3
"""SQLite WAL work graph, 3-valued obligation evaluator, and lane projection for Workbench."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Mapping
import contextlib
from datetime import datetime, timezone
import glob
import hashlib
import json
import math
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
DEFAULT_PROJECTS_GLOB = os.path.expanduser("~/.gemini/config/projects/*.json")
DEFAULT_ROADMAPS_GLOB = os.path.expanduser("~/.gemini/jetski/roadmaps/*/state/*.md")
DEFAULT_DOSSIERS_PATH = os.path.expanduser("~/.gemini/jetski/brain/jetski-dossiers.json")
DEFAULT_SUMMARIES_DB = os.path.expanduser("~/.gemini/jetski/conversation_summaries.db")
DEFAULT_BRAIN_ROOT = os.path.expanduser("~/.gemini/jetski/brain")
ET_ZONE = ZoneInfo("America/New_York")
STALLED_OBLIGATION_DAYS = 7
CANDIDATE_TTL_DAYS = 7
HUB_BUG_COUNT_THRESHOLD = 3
HUB_CORRESPONDENCE_TASK_THRESHOLD = 3

CONFIG_PROJECT_IDS = frozenset({
    "04508c43-54a0-4003-8051-7129cd997b88",  # Improving Experience
    "5a2a0f80-96c2-4334-a58d-7631310d4f72",  # Flight Deck
    "61f270de-563d-45a4-b9eb-1c0bccf80cc9",  # My Work
    "a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3",  # Breaking Chats
    "1c95149f-2787-4209-976d-350eb2a6532c",  # Code Review organizer
    "601195ef-244b-4030-b288-efe5e738cb44",  # Partnership with AI
    "e914e1c4-006a-433c-8270-52eff8775667",  # Skia Gardener
})
CONFIG_PROJECT_PREFIXES = frozenset(pid[:8] for pid in CONFIG_PROJECT_IDS)
CONFIG_PROJECT_NAMES = frozenset({
    "improving experience",
    "flight deck",
    "my work",
    "breaking chats",
    "code review organizer",
    "partnership with ai",
    "skia gardener",
    "gemini-config",
    ".gemini/config",
    "workbench",
})

_RE_AUDIT_PREFIX = re.compile(
    r"\[?flight[\s\-]deck\s+(?:tier\s+\d+\s+)?(?:incremental\s+)?audit[^\]]*\]?:?",
    re.IGNORECASE,
)
_RE_PRODUCT_HEAD = re.compile(
    r"^(?:review(?:-triage)?\s+|companion\s+draft\s+pr\s+for\s+)?"
    r"(?:pr\s+#\d+|gerrit\s+\d+|crrev/c/\d+|\d{6,8}:\s+\[)",
    re.IGNORECASE,
)
_CONFIG_TEXT_PATTERNS = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\.gemini/config",
        r"\bgemini-config\b",
        r"\bimproving experience\b",
        r"\bbreaking chats\b",
        r"\bcode review organizer\b",
        r"\bpartnership with ai\b",
        r"\bskia[\s_]gardener\b",
        r"\bworkbench\b",
        r"\bflight[\s\-]deck\b",
        r"\bui-variants\b",
        r"\[title guard\]",
        r"clickable links and gerrit auto-title",
        r"gerrit review draft save enforcement",
        r"false skia blame in flutter roll",
        r"notice of jetski best-of-n policy",
        r"\bskill\.md\b",
        r"\b(?:check_doc_grounding|pre_tool_guard|stop_guard|decision_guard"
        r"|chat_lifecycle|cl_watcher|auto_ship)\.py\b",
        r"веб-страниц\w*\s+мониторинга\s+активных\s+слей",
        r"генераци\w*\s+заголовков\s+ревью-чатов",
        r"пропускаем\s+полный\s+корпус\s+тестов\s+флаттера",
        r"правила\s+именования\s+чатов",
        r"промпты\s+субагентов\s+в\s+плагине\s+keeper",
        r"воркфлоу\s+в\s+наших\s+скиллах",
        r"перед\s+тем,\s+как\s+я\s+выставлю\s+\+1\s+код\s+ревью",
    )
)


def is_config_project(project_id: str) -> bool:
  """Returns True when project_id identifies a ~/.gemini/config or tooling project."""
  p = str(project_id or "").strip().lower()
  if not p:
    return False
  if (
      p in CONFIG_PROJECT_IDS
      or (len(p) >= 8 and p[:8] in CONFIG_PROJECT_PREFIXES)
      or p in CONFIG_PROJECT_NAMES
  ):
    return True
  return ".gemini/config" in p or "gemini-config" in p


def is_config_text(text: str) -> bool:
  """Returns True when a Work or chat title describes ~/.gemini/config or tooling work."""
  raw = str(text or "").strip()
  if not raw:
    return False
  # Preserve product CL/PR audit titles stamped by Flight Deck's review launcher
  stripped = _RE_AUDIT_PREFIX.sub("", raw).strip()
  if not stripped:
    return False
  if _RE_PRODUCT_HEAD.match(stripped) and "lgtm blocked status" not in stripped.lower():
    return False
  return any(rx.search(stripped) for rx in _CONFIG_TEXT_PATTERNS)


_CHAT_CLASS_CACHE: dict[str, Any] = {
    "ts": 0.0,
    "config_cids": set(),
    "product_cids": set(),
    "subagent_cids": set(),
}


def get_local_chat_classification(
    now: float | None = None,
) -> tuple[set[str], set[str], set[str]]:
  """Returns cached (config_cids, product_cids, subagent_cids) from local metadata."""
  cur_now = time.time() if now is None else now
  if (
      cur_now - float(_CHAT_CLASS_CACHE.get("ts") or 0.0) < 30.0
      and _CHAT_CLASS_CACHE["config_cids"]
  ):
    return (
        _CHAT_CLASS_CACHE["config_cids"],
        _CHAT_CLASS_CACHE["product_cids"],
        _CHAT_CLASS_CACHE["subagent_cids"],
    )

  config_cids: set[str] = set()
  product_cids: set[str] = set()
  subagent_cids: set[str] = set()

  for pfile in glob.glob(DEFAULT_PROJECTS_GLOB):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      pid = str(pdata.get("id") or "")
      pname = str(pdata.get("name") or "")
      target_set = (
          config_cids
          if (is_config_project(pid) or is_config_project(pname))
          else product_cids
      )
      for c in pdata.get("conversations") or []:
        cid = str(c.get("id") if isinstance(c, Mapping) else c).strip()
        if cid:
          target_set.add(cid)
          target_set.add(cid[:8])
    except Exception:
      pass

  for sfile in glob.glob(DEFAULT_ROADMAPS_GLOB):
    parts = sfile.split(os.sep)
    if len(parts) >= 3:
      pid = parts[-3]
      cid = os.path.splitext(parts[-1])[0]
      target_set = config_cids if is_config_project(pid) else product_cids
      if cid:
        target_set.add(cid)
        target_set.add(cid[:8])
      try:
        with open(sfile, "r", encoding="utf-8", errors="replace") as sf:
          stext = sf.read(16384)
        for m_c in re.finditer(
            r"conversation://([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
            stext,
            re.IGNORECASE,
        ):
          rc = m_c.group(1)
          target_set.add(rc)
          target_set.add(rc[:8])
      except OSError:
        pass

  if os.path.isfile(DEFAULT_DOSSIERS_PATH):
    try:
      with open(DEFAULT_DOSSIERS_PATH, "r", encoding="utf-8") as f:
        raw_d = json.load(f) or {}
      dossiers = raw_d.get("items") or raw_d.get("dossiers") or {}
      if isinstance(dossiers, Mapping):
        for cid, d in dossiers.items():
          if not isinstance(d, Mapping):
            continue
          pid = str(
              d.get("projectId") or d.get("project_id") or d.get("project_name") or ""
          )
          title = str(d.get("title") or d.get("heading") or "")
          rcid = str(d.get("root_id") or "")
          if is_config_project(pid) or is_config_text(title):
            config_cids.add(str(cid))
            config_cids.add(str(cid)[:8])
            if rcid:
              config_cids.add(rcid)
              config_cids.add(rcid[:8])
          elif pid:
            product_cids.add(str(cid))
            product_cids.add(str(cid)[:8])
            if rcid:
              product_cids.add(rcid)
              product_cids.add(rcid[:8])
    except Exception:
      pass

  if os.path.isfile(DEFAULT_SUMMARIES_DB):
    try:
      sconn = sqlite3.connect(DEFAULT_SUMMARIES_DB, timeout=2.0)
      sconn.row_factory = sqlite3.Row
      try:
        tables = [
            str(r[0])
            for r in sconn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        for tname in tables:
          cols = {
              str(r[1])
              for r in sconn.execute(f"PRAGMA table_info({tname})").fetchall()
          }
          if "conversation_id" in cols and "parent_conversation_id" in cols:
            for r in sconn.execute(
                f"SELECT conversation_id FROM {tname} "
                f"WHERE (parent_conversation_id IS NOT NULL AND parent_conversation_id != '') "
                f"OR COALESCE(nesting_depth, 0) > 0 "
                f"OR (agent_name IS NOT NULL AND agent_name != '')"
            ):
              cid = str(r["conversation_id"] or "")
              if cid:
                subagent_cids.add(cid)
                subagent_cids.add(cid[:8])
      finally:
        sconn.close()
    except Exception:
      pass

  _CHAT_CLASS_CACHE["ts"] = cur_now
  _CHAT_CLASS_CACHE["config_cids"] = config_cids
  _CHAT_CLASS_CACHE["product_cids"] = product_cids
  _CHAT_CLASS_CACHE["subagent_cids"] = subagent_cids
  return config_cids, product_cids, subagent_cids


_PROD_WRITE_CMDS = (
    "gerrit_draft.py save",
    "finalize_review.py",
    "git cl upload",
    "gh pr create",
    "gh pr edit",
    "git push",
    "create-draft",
    "update-draft",
    "gmail mutate send",
    "gmail mutate reply",
    "bug_actions.py close_fixed",
    "bug_actions.py reassign_merge",
)
_NON_PRODUCT_PATHS = (
    "/.gemini/config/",
    "/.gemini/jetski/brain/",
    "/.gemini/jetski/roadmaps/",
    "/tmp/",
    "/var/",
)


def is_transcript_config_only(transcript_path: str) -> bool:
  """Returns True when a transcript edits ~/.gemini/config without any product repo writes."""
  if not transcript_path or not os.path.isfile(transcript_path):
    return False
  has_prod_write = False
  has_cfg_edit = False
  try:
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
      for line in f:
        if not line.strip():
          continue
        try:
          step = json.loads(line)
        except Exception:
          continue
        if not isinstance(step, Mapping):
          continue
        for tc in step.get("tool_calls") or []:
          if not isinstance(tc, Mapping):
            continue
          tname = str(tc.get("name") or "")
          args = tc.get("args") or tc.get("arguments") or {}
          if not isinstance(args, Mapping):
            continue
          if tname in ("edit_file", "write_file"):
            p = str(args.get("Path") or "")
            if "/.gemini/config/" in p:
              has_cfg_edit = True
            elif p and not any(x in p for x in _NON_PRODUCT_PATHS):
              has_prod_write = True
          elif tname == "run_command":
            cmd = str(args.get("CommandLine") or "")
            if any(pat in cmd for pat in _PROD_WRITE_CMDS):
              has_prod_write = True
            if (
                "auto_ship.py" in cmd
                or "patch_pre_tool_guard" in cmd
                or "fix_pre_tool" in cmd
                or ("/.gemini/config/" in cmd and any(w in cmd for w in ("sed ", "cat <<")))
            ):
              has_cfg_edit = True
          elif tname in ("update_doc", "send_message_markdown"):
            has_prod_write = True
          elif tname == "call_mcp_tool":
            t_mcp = str(args.get("ToolName") or "")
            if t_mcp in (
                "update_doc",
                "create_file",
                "add_comment_reply",
                "resolve_comment_thread",
                "send_message",
                "send_message_markdown",
            ):
              has_prod_write = True
  except OSError:
    return False
  return has_cfg_edit and not has_prod_write


# Product tracker prefixes exempt from config-project purging when cited in diagnostic chats
_PRODUCT_ENTITY_PREFIXES = (
    "gerrit:",
    "github_pr:",
    "piper:",
    "buganizer:",
    "github_issue:",
)


def is_config_artifact(
    artifact_key: str,
    title: str = "",
    state: Mapping[str, Any] | None = None,
    project_id: str = "",
) -> bool:
  """Returns True when an artifact represents a config/subagent chat or config roadmap item."""
  akey = str(artifact_key or "")
  # External tracker entities remain product artifacts even when discussed in a config project
  if akey.startswith(_PRODUCT_ENTITY_PREFIXES):
    return False
  st = state or {}
  # Classify chat_chain artifacts by their own transcript edits before falling back to project_id
  if akey.startswith("chat_chain:"):
    cid = akey.split(":", 1)[-1]
    casc = str(st.get("cascade_id") or cid)
    rcid = str(st.get("root_cid") or cid)
    config_cids, product_cids, subagent_cids = get_local_chat_classification()
    for cand in (cid, casc, rcid, cid[:8], casc[:8], rcid[:8]):
      if cand and (cand in config_cids or cand in subagent_cids):
        return True
    for cand in (cid, casc, rcid, cid[:8], casc[:8], rcid[:8]):
      if cand and cand in product_cids:
        return False
    for check_cid in {cid, casc, rcid}:
      if len(check_cid) >= 32:
        tpath = os.path.join(
            DEFAULT_BRAIN_ROOT, check_cid, ".system_generated", "logs", "transcript.jsonl"
        )
        if is_transcript_config_only(tpath):
          config_cids.add(check_cid)
          config_cids.add(check_cid[:8])
          return True
  if is_config_project(project_id) or is_config_project(
      str(st.get("project_id") or st.get("project") or "")
  ):
    return True
  if is_config_text(title) or is_config_text(str(st.get("subject") or "")):
    return True
  if akey.startswith("roadmap_item:"):
    r_proj = akey.split(":", 2)[1] if akey.count(":") >= 2 else ""
    if is_config_project(r_proj):
      return True
  return False

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
    "gdoc_thread": "🗨",
    "buganizer": "🐞",
    "github_issue": "🐞",
    "gerrit": "🔧",
    "github_pr": "🔧",
    "piper": "🔧",
    "chat_chain": "🤖",
    "roadmap_item": "🗺",
}


def format_et_timestamp(ts: float, now: float | None = None) -> str:
  """Formats a positive epoch timestamp in New York time with a relative age suffix."""
  if not ts or not math.isfinite(ts) or ts <= 0:
    return ""
  # Anchor both ET date rollover and relative age to one clock snapshot for deterministic tests
  cur_now = time.time() if now is None else now
  dt = datetime.fromtimestamp(ts, tz=ET_ZONE)
  now_dt = datetime.fromtimestamp(cur_now, tz=ET_ZONE)
  delta_s = max(0, int(cur_now - ts))
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


_RE_SUBSECOND_OVERFLOW = re.compile(r"(\.\d{6})\d+")


def _parse_domain_ts(val: Any) -> float:
  """Parses numeric epoch seconds, ISO-8601/Gerrit UTC, or Piper ET dates into positive seconds."""
  # Reject booleans (int subclass in Python), non-finite floats, and Go zero-time sentinels
  if val is None or isinstance(val, bool):
    return 0.0
  if isinstance(val, (int, float)):
    ts_f = float(val)
    return ts_f if math.isfinite(ts_f) and ts_f > 0.0 else 0.0
  s = str(val).strip()
  if not s or s.startswith("0001-"):
    return 0.0
  try:
    ts_num = float(s)
    return ts_num if math.isfinite(ts_num) and ts_num > 0.0 else 0.0
  except ValueError:
    pass
  # Clamp 9-digit nanosecond Gerrit/SQLite timestamps so Python <3.11 parses them identically
  iso_s = _RE_SUBSECOND_OVERFLOW.sub(r"\1", s)
  if iso_s.endswith(("Z", "z")):
    iso_s = iso_s[:-1] + "+00:00"
  try:
    dt = datetime.fromisoformat(iso_s)
    # Naive Gerrit timestamps ("YYYY-MM-DD HH:MM:SS.fffffffff") are UTC regardless of host TZ
    if dt.tzinfo is None:
      dt = dt.replace(tzinfo=timezone.utc)
    ts_iso = dt.timestamp()
    return ts_iso if ts_iso > 0.0 else 0.0
  except ValueError:
    pass
  # Piper g4 describe emits America/New_York wall-clock slash timestamps without a TZ suffix
  try:
    ts_piper = datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=ET_ZONE).timestamp()
    return ts_piper if ts_piper > 0.0 else 0.0
  except ValueError:
    return 0.0


def _first_positive_ts(state: Mapping[str, Any], keys: tuple[str, ...]) -> float:
  """Returns the first positive timestamp found under keys in state, or 0.0."""
  for k in keys:
    ts = _parse_domain_ts(state.get(k))
    if ts > 0.0:
      return ts
  return 0.0


def _max_positive_ts(state: Mapping[str, Any], keys: tuple[str, ...]) -> float:
  """Returns the maximum positive timestamp across keys in state, or 0.0."""
  best = 0.0
  for k in keys:
    ts = _parse_domain_ts(state.get(k))
    if ts > best:
      best = ts
  return best


def _extract_artifact_timestamps(art: Mapping[str, Any]) -> tuple[float, float, int]:
  """Extracts (activity_ts, origin_ts, origin_tier) for one non-context domain artifact."""
  akey = str(art.get("artifact_key") or "")
  kind = str(art.get("kind") or artifact_kind_from_key(akey))
  st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
  title = str(art.get("title") or "")
  link_reason = str(art.get("link_reason") or "")

  # Keep origin_ts strictly on creation keys so fresh replies/audits never reset work age
  if kind in ("gmail_thread", "gchat_thread", "gdoc_thread") or (
      kind == "gdoc" and is_active_correspondence_artifact("gdoc", st)
  ):
    act_ts = _max_positive_ts(
        st,
        (
            "last_message_ts", "counterparty_reply_ts", "mcp_reply_ts",
            "manual_wait_ts", "resolved_at", "created_ts", "origin_ts",
        ),
    )
    orig_ts = _first_positive_ts(st, ("origin_ts", "created_ts"))
    return act_ts, orig_ts, (1 if orig_ts > 0.0 else 0)

  # Sent or vanished Gmail drafts hold stale timestamps owned by the thread, not a live draft
  if kind == "gmail_draft":
    if str(st.get("status") or "DRAFT").upper() != "DRAFT":
      return 0.0, 0.0, 0
    act_ts = _max_positive_ts(st, ("draft_updated_ts", "created_ts", "origin_ts"))
    orig_ts = _first_positive_ts(st, ("origin_ts", "created_ts"))
    return act_ts, orig_ts, (1 if orig_ts > 0.0 else 0)

  if kind in ("gerrit", "github_pr", "github", "piper"):
    # Exclude local audited_msg_date unless no tracker timestamp exists (e.g. virtual Piper CL)
    act_ts = _max_positive_ts(
        st,
        (
            "activity_ts", "updated_ts", "submitted_ts", "last_msg_date",
            "status_changed_raw", "created_ts", "created", "origin_ts",
        ),
    )
    if act_ts <= 0.0:
      act_ts = _max_positive_ts(st, ("audited_msg_date", "chat_last_modified"))
    orig_ts = _first_positive_ts(st, ("created_ts", "created", "origin_ts"))
    if orig_ts <= 0.0:
      return act_ts, 0.0, 0
    # Companion WIP CLs are review/test scaffolding and must not override the parent CL's age
    tier = 2 if is_companion_artifact(akey, st, title, link_reason) else 1
    return act_ts, orig_ts, tier

  if kind in ("buganizer", "github_issue"):
    act_ts = _max_positive_ts(
        st,
        (
            "modified_ts", "last_human_comment_at", "last_self_comment_at",
            "major_modified_time", "modified_time", "resolved_time",
            "created_time", "origin_ts",
        ),
    )
    # Keep multi-year upstream issue dates in Tier 3 so bound works keep their own start age
    orig_ts = _first_positive_ts(st, ("origin_ts", "created_time"))
    return act_ts, orig_ts, (3 if orig_ts > 0.0 else 0)

  if kind == "chat_chain":
    act_ts = _max_positive_ts(
        st, ("last_activity_ts", "updated_ts", "last_modified_time", "created_ts", "origin_ts")
    )
    orig_ts = _first_positive_ts(st, ("origin_ts", "created_ts"))
    return act_ts, orig_ts, (1 if orig_ts > 0.0 else 0)

  return 0.0, 0.0, 0


def compute_card_timestamps(
    work_row: Mapping[str, Any] | None,
    w_arts: Iterable[Mapping[str, Any]],
    w_obs: Iterable[Mapping[str, Any]],
    reply_draft: Mapping[str, Any] | None,
) -> tuple[float, float]:
  """Computes (activity_at, origin_at) from non-context artifacts, drafts, and obligations."""
  act_candidates: list[float] = []
  tier1_origins: list[float] = []
  own_origins: list[float] = []
  issue_origins: list[float] = []

  for art in w_arts:
    # Context-only chat mentions must never skew either recent activity or work age
    if str(art.get("role") or "") == "context_only":
      continue
    act_ts, orig_ts, orig_tier = _extract_artifact_timestamps(art)
    if act_ts > 0.0:
      act_candidates.append(act_ts)
    if orig_tier == 1:
      tier1_origins.append(orig_ts)
    elif orig_tier == 2:
      own_origins.append(orig_ts)
    elif orig_tier == 3:
      issue_origins.append(orig_ts)

  # Saving or materializing an inline reply draft counts as fresh work activity
  if isinstance(reply_draft, Mapping):
    rd_ts = _max_positive_ts(reply_draft, ("updated_at", "materialized_at"))
    if rd_ts > 0.0:
      act_candidates.append(rd_ts)

  # Fired obligations surface fresh triage activity; obligation and work creation bound own age
  for ob in w_obs:
    if str(ob.get("status") or "") == "done":
      continue
    if str(ob.get("status") or "") == "ready":
      sat_ts = _parse_domain_ts(ob.get("satisfied_at"))
      if sat_ts > 0.0:
        act_candidates.append(sat_ts)
    ob_orig = _parse_domain_ts(ob.get("created_at"))
    if ob_orig > 0.0:
      own_origins.append(ob_orig)

  work_created = _parse_domain_ts((work_row or {}).get("created_at"))
  if work_created > 0.0:
    own_origins.append(work_created)

  # Return 0.0 when no domain or stored clock exists so unknown-timestamp cards always sort last
  activity_at = max(act_candidates, default=0.0)
  if activity_at <= 0.0 and work_row is not None:
    activity_at = _parse_domain_ts(work_row.get("updated_at")) or work_created

  if tier1_origins:
    origin_at = min(tier1_origins)
  elif own_origins:
    origin_at = min(own_origins)
  elif issue_origins:
    origin_at = min(issue_origins)
  else:
    origin_at = activity_at

  # Guard against clock skew between local SQLite creation and earlier external timestamps
  if activity_at > 0.0 and origin_at > activity_at:
    origin_at = activity_at
  return activity_at, origin_at


def format_short_et_date(ts: float) -> str:
  if not ts or not math.isfinite(ts) or ts <= 0:
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
    "gdoc:",
    "gdoc_thread:",
    "chat_chain:",
)

_RE_COURTESY_CLOSE = re.compile(
    r"\b(?:thank\s+you|thanks|got\s+it|sounds\s+good|sgtm|lgtm"
    r"|i\s+agree\s+with\s+you|do\s+whatever\s+you\s+want|no\s+worries|all\s+set|reverted)\b",
    re.IGNORECASE,
)
_RE_OPEN_ACTION_CUES = re.compile(
    r"\b(?:ptal|please\s+(?:check|review|advise|take)|could\s+you|can\s+you|would\s+you"
    r"|wdyt|let\s+me\s+know|tomorrow|looking\s+into|will\s+send|follow\s+up)\b",
    re.IGNORECASE,
)


_RE_GH_NOTIF_TARGET = re.compile(
    r"\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\][^\n]*\((Issue|PR)\s+#(\d+)\)",
    re.IGNORECASE,
)


def extract_github_notif_target(subject: str) -> str:
  """Extracts canonical github_issue or github_pr key from a GitHub notification subject."""
  m = _RE_GH_NOTIF_TARGET.search(str(subject or ""))
  if not m:
    return ""
  repo = m.group(1)
  kind = "github_pr" if m.group(2).lower() == "pr" else "github_issue"
  num = m.group(3)
  return f"{kind}:{repo}:{num}"


def _get_github_target_state(
    st: Mapping[str, Any],
    artifacts_by_key: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Any]:
  """Looks up the linked GitHub Issue/PR state dict for a GitHub notification thread."""
  if not artifacts_by_key:
    return {}
  target_key = str(st.get("gh_target_key") or "") or extract_github_notif_target(
      str(st.get("root_subject") or "")
  )
  if not target_key:
    return {}
  t_entry = artifacts_by_key.get(target_key)
  if not isinstance(t_entry, Mapping):
    return {}
  raw_st = t_entry.get("state") if "state" in t_entry else t_entry
  return raw_st if isinstance(raw_st, Mapping) else {}


def _resolve_github_thread_turns(
    st: Mapping[str, Any] | None,
    target_st: Mapping[str, Any] | None = None,
) -> tuple[float, float]:
  """Returns (eff_ext_ts, gh_self_ts) reconciling Gmail and GitHub comment turns."""
  s = st or {}
  ts = target_st or {}
  last_msg_ts = float(s.get("last_message_ts") or 0.0)
  gh_human_ts = max(
      float(s.get("gh_human_reply_ts") or 0.0),
      float(ts.get("last_human_comment_at") or 0.0),
  )
  gmail_ext_ts = 0.0 if bool(s.get("last_from_me")) else last_msg_ts
  # Allow 30s Gmail delivery lag only relative to the latest known GitHub human comment
  if gh_human_ts > 0 and last_msg_ts <= gh_human_ts + 30.0:
    ext_ts = gh_human_ts
  else:
    ext_ts = max(gh_human_ts, gmail_ext_ts)
  self_ts = max(
      float(s.get("gh_self_reply_ts") or 0.0),
      float(ts.get("last_self_comment_at") or 0.0),
  )
  return ext_ts, self_ts


# Exclude passive reference GDocs, trashed unjoined GitHub notifications, and bot emails
def is_active_correspondence_artifact(
    kind: str, state: Mapping[str, Any] | None = None
) -> bool:
  """Returns True when an artifact owns active correspondence obligations."""
  st = state or {}
  if kind == "gchat_thread":
    return True
  if kind == "gdoc_thread":
    return True
  if kind == "gdoc":
    return bool(st.get("is_active_correspondence"))
  if kind == "gmail_thread":
    if st.get("is_github_issue_thread") or st.get("is_github_pr_thread"):
      # Trashed notifications the user never joined stop owning correspondence obligations
      return not (bool(st.get("in_trash_or_spam")) and not bool(st.get("sent_by_me")))
    if st.get("is_gdoc_comment"):
      return True
    if st.get("is_drive_share"):
      return bool(st.get("started_by_me") or st.get("is_active_correspondence"))
    return not bool(st.get("is_automated"))
  return False


def is_github_thread_resolved(
    state: Mapping[str, Any] | None,
    artifacts_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
  """Returns True when a GitHub notification thread is trashed, locked, or cleanly closed."""
  st = state or {}
  if not (st.get("is_github_issue_thread") or st.get("is_github_pr_thread")):
    return False
  if bool(st.get("in_trash_or_spam")) and not bool(st.get("sent_by_me")):
    return True
  target_st = _get_github_target_state(st, artifacts_by_key)
  synced_at = float(target_st.get("synced_at") or 0.0)
  if bool(target_st.get("locked")) or (synced_at <= 0 and bool(st.get("gh_locked"))):
    return True
  t_status = str(target_st.get("status") or "").strip().upper()
  last_msg_ts = float(st.get("last_message_ts") or 0.0)
  ext_ts, gh_self_ts = _resolve_github_thread_turns(st, target_st)
  self_ts = max(
      gh_self_ts,
      last_msg_ts if bool(st.get("last_from_me")) else 0.0,
  )
  if t_status in ("CLOSED", "MERGED"):
    terminal_at = max(
        float(target_st.get("closed_at") or 0.0),
        float(target_st.get("merged_at") or 0.0),
    )
    if terminal_at > 0:
      return True if ext_ts <= terminal_at else self_ts >= ext_ts
    if ext_ts <= 0:
      return True
    return self_ts >= ext_ts
  if not t_status and synced_at <= 0 and bool(st.get("gh_thread_closed")):
    gh_closed_ts = float(st.get("gh_closed_ts") or 0.0)
    return gh_closed_ts <= 0 or ext_ts <= gh_closed_ts
  return False


# Flag courtesy sign-offs, external GDoc resolves, and direct inbound shares once per message_id
def detect_borderline_correspondence_closure(
    kind: str, state: Mapping[str, Any] | None = None
) -> tuple[str, str]:
  st = state or {}
  last_ts = float(st.get("last_message_ts") or 0.0)
  # Key decisions per message so a later reply on the same thread can ask again if needed
  event_key = str(st.get("last_message_id") or "").strip()
  if not event_key and last_ts > 0:
    event_key = f"ts:{int(last_ts)}"
  if not event_key:
    return "", ""
  decisions = st.get("ambiguity_decisions")
  if isinstance(decisions, Mapping) and event_key in decisions:
    return "", ""
  if kind == "gdoc_thread" and bool(st.get("resolved_externally")):
    return "Ветка комментариев в Google Doc помечена решённой — обязательство закрыто?", event_key
  if (kind == "gdoc" or bool(st.get("is_drive_share"))) and not bool(
      st.get("started_by_me", True)
  ):
    return (
        "Документ расшарен напрямую — нужно ответить/отревьюить или закрыть обязательство?",
        event_key,
    )
  raw_snip = str(st.get("last_unquoted_text") or st.get("last_snippet") or "").strip()
  if not raw_snip:
    return "", ""
  # Strip inline Gmail attribution tails ("On Mon, ...") and reject questions or action cues
  snip = re.split(
      r"\bOn\s+(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun|[A-Z][a-z]{2},\s)", raw_snip, maxsplit=1
  )[0].strip()
  if not snip or len(snip) > 220 or "?" in snip:
    return "", ""
  if _RE_OPEN_ACTION_CUES.search(snip):
    return "", ""
  # Outbound single-message openers ending with "Thanks!" are initial asks, not thread closures
  if _RE_COURTESY_CLOSE.search(snip):
    msg_cnt = int(st.get("message_count") or 1)
    # Multi-turn history is guaranteed when msg_cnt >= 2 or an inbound reply follows our message
    is_multi_turn = msg_cnt >= 2 or (
        bool(st.get("sent_by_me")) and not bool(st.get("last_from_me"))
    )
    if is_multi_turn or "i agree with you" in snip.lower():
      return "Похоже на завершающую реплику — тема закрыта или остаётся обязательство?", event_key
  return "", ""


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
  text = re.sub(r"^//[0-9a-f\-]{36}\)\s*:\s*", "", text, flags=re.IGNORECASE)
  lower = text.lower()
  for pref in _STATE_TITLE_PREFIXES:
    if lower.startswith(pref):
      text = text[len(pref) :].lstrip(" —-:").strip()
      break
  return text


# Match companion probe subjects and companion-creation chat titles across Gerrit and GitHub
_RE_COMPANION_TITLE = re.compile(
    r"(?:\[companion\s+(?:wip|draft)\]"
    r"|\bcompanion\s+(?:wip|draft(?:\s+pr)?|cls?|prs?|fixes|fix|tests?|changes?|for|to)\b"
    r"|\[do\s+not\s+submit\].*(?:crrev|gerrit|pr\s*#|companion|hypothes))",
    re.IGNORECASE,
)


def is_companion_title(title: str) -> bool:
  """Returns True when a Work or artifact title names a companion probe or companion chat."""
  t = clean_work_title(title)
  if not t:
    return False
  return bool(_RE_COMPANION_TITLE.search(t))


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
  # Treat bare tracker IDs and unlabelled companion tags as placeholders so real subjects win
  if re.match(
      r"^(?:(?:flutter|[a-z0-9_.-]+/[a-z0-9_.-]+)#\d+|(?:b|crbug)/\d+|pr\s*#?\d+"
      r"|gerrit\s+\d+|crrev(?:\.com)?/c/\d+|cl/\d+)$",
      tl,
  ):
    return True
  if re.match(r"^(?:\d+:\s*)?\[companion\s+(?:wip|draft)\]$", tl):
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

  # Partition linked artifacts into human correspondence vs technical entities with timestamps
  correspondence_arts: list[tuple[float, dict[str, Any]]] = []
  other_arts: list[tuple[float, dict[str, Any]]] = []
  for a in w_arts:
    st = a.get("state") or {}
    ots = float(st.get("origin_ts") or st.get("created_ts") or 0.0)
    kind = str(a.get("kind") or "")
    # Group non-context human correspondence separately so it can act as causal root
    if a.get("role") != "context_only" and is_active_correspondence_artifact(kind, st):
      correspondence_arts.append((ots, a))
    elif ots > 0 and a.get("role") != "context_only":
      # Keep companion probes behind real parent artifacts when resolving causal origin
      is_comp = is_companion_artifact(
          str(a.get("artifact_key") or ""),
          st if isinstance(st, Mapping) else None,
          str(a.get("title") or ""),
          str(a.get("link_reason") or ""),
      ) or is_companion_title(str(a.get("title") or ""))
      if not is_comp:
        other_arts.append((ots, a))

  # Prefer primary non-GH-notification email threads first, then earlier timestamps
  correspondence_arts.sort(
      key=lambda x: (
          0
          if (
              x[1].get("role") == "primary"
              and not (
                  (x[1].get("state") or {}).get("is_github_issue_thread")
                  or (x[1].get("state") or {}).get("is_github_pr_thread")
              )
          )
          else (1 if x[1].get("role") == "primary" else 2),
          x[0] if x[0] > 0 else float("inf"),
      )
  )
  other_arts.sort(key=lambda x: x[0])

  origin_art: dict[str, Any] | None = None
  origin_ts = 0.0
  earliest_other_ts = other_arts[0][0] if other_arts else 0.0
  has_active_draft = any(
      a.get("kind") == "gmail_draft"
      and a.get("role") != "context_only"
      and str((a.get("state") or {}).get("status") or "DRAFT").upper() == "DRAFT"
      for a in w_arts
  )

  # Primary correspondence root, active draft thread, or correspondence predating work wins
  if correspondence_arts:
    c_ts, c_art = correspondence_arts[0]
    if (
        c_art.get("role") == "primary"
        or c_art.get("link_reason") == "gmail_root"
        or has_active_draft
        or (c_ts > 0 and (earliest_other_ts <= 0 or c_ts <= earliest_other_ts + 86400))
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
    elif kind in ("gdoc_thread", "gdoc"):
      cp = str(st.get("counterparty") or "").strip()
      doc_t = str(st.get("doc_title") or origin_art.get("title") or "").strip()
      parts = [p for p in (dt_str, cp) if p]
      detail = f" ({' · '.join(parts)})" if parts else ""
      label = f"🌱 Origin: 📄 {doc_t}{detail}"
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
  if origin_art is not None and origin_art.get("kind") in (
      "gmail_thread",
      "gchat_thread",
      "gdoc_thread",
      "gdoc",
  ):
    st = origin_art.get("state") or {}
    if origin_art.get("kind") in ("gdoc_thread", "gdoc"):
      root_subj = str(st.get("doc_title") or "").strip()
      if not root_subj:
        cand_t = clean_work_title(str(origin_art.get("title") or ""))
        if not is_placeholder_title(cand_t, str(origin_art.get("artifact_key") or "")):
          root_subj = cand_t
    else:
      root_subj = str(st.get("root_subject") or "").strip()
    # Strip reply/forward prefixes when falling back to the raw artifact title
    if not root_subj and origin_art.get("kind") in ("gmail_thread", "gchat_thread"):
      root_subj = re.sub(
          r"^(?:re|fwd|fw)\s*:\s*",
          "",
          str(origin_art.get("title") or ""),
          flags=re.IGNORECASE,
      ).strip()
    cp = str(st.get("counterparty") or "").strip()
    # Append the counterparty name when not already present in the thread subject
    if root_subj and not is_placeholder_title(root_subj):
      if cp and cp.lower() not in root_subj.lower():
        return f"{root_subj} — {cp}", origin_info
      return root_subj, origin_info

  # Discard cached titles that came from a context_only digest thread
  is_stale_context_corr_title = False
  for a in w_arts:
    if a.get("role") == "context_only" and a.get("kind") in (
        "gmail_thread",
        "gchat_thread",
    ):
      st_a = a.get("state") or {}
      r_subj = str(st_a.get("root_subject") or "").strip()
      if r_subj and cleaned.lower().startswith(r_subj.lower()):
        is_stale_context_corr_title = True
        break

  # Never let a companion creation chat or probe title stand in for the parent change's title
  if (
      not is_placeholder_title(cleaned)
      and not is_config_text(cleaned)
      and not is_companion_title(cleaned)
      and not is_stale_context_corr_title
  ):
    return cleaned, origin_info

  # Upgrade placeholder/raw-key, companion, or config-polluted titles from non-companion artifacts
  for role_filter in (("primary",), ("channel",), ("context_only",)):
    for a in w_arts:
      if a.get("role") not in role_filter:
        continue
      a_st = a.get("state") if isinstance(a.get("state"), Mapping) else None
      # Skip companion artifacts so the parent CL/PR or correspondence subject wins
      if is_companion_artifact(
          str(a.get("artifact_key") or ""),
          a_st,
          str(a.get("title") or ""),
          str(a.get("link_reason") or ""),
      ):
        continue
      cand = clean_work_title(str(a.get("title") or ""))
      if (
          not is_placeholder_title(cand, str(a.get("artifact_key") or ""))
          and not is_config_text(cand)
          and not is_companion_title(cand)
      ):
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
      if "#" in rest:
        lhs, num = rest.split("#", 1)
        repo_slug = lhs.strip() if "/" in lhs else "flutter/flutter"
        return f"github_pr:{repo_slug}:{num.strip()}"
      if ":" in rest:
        return f"github_pr:{rest}"
      return f"github_pr:flutter/flutter:{rest.lstrip('#')}"
    if prefix in ("github_issue", "flutter_issue", "flutter", "gh_issue"):
      if "#" in rest:
        # Preserve explicit owner/repo slugs on gh_issue:<owner>/<repo>#<N> keys
        lhs, num = rest.split("#", 1)
        repo_slug = lhs.strip() if "/" in lhs else "flutter/flutter"
        return f"github_issue:{repo_slug}:{num.strip()}"
      if ":" in rest:
        return f"github_issue:{rest}"
      return f"github_issue:flutter/flutter:{rest.lstrip('#')}"
    if prefix in ("buganizer", "b", "crbug"):
      clean_bid = re.sub(r"^(?:b|crbug)/", "", rest, flags=re.IGNORECASE).strip()
      return f"buganizer:{clean_bid}"
    if prefix in (
        "piper",
        "gmail_thread",
        "gmail_draft",
        "gchat_thread",
        "gdoc",
        "gdoc_thread",
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
    if "#" in tid:
      lhs, num = tid.split("#", 1)
      repo_slug = lhs.strip() if "/" in lhs else "flutter/flutter"
      return f"github_pr:{repo_slug}:{num.strip()}"
    if ":" in tid:
      return f"github_pr:{tid}"
    return f"github_pr:flutter/flutter:{tid.lstrip('#')}"
  if k in ("github_issue", "flutter_issue", "flutter", "gh_issue"):
    if "#" in tid:
      # Mirror 1-arg owner/repo#N parsing so 2-arg callers never emit un-canonicalized slugs
      lhs, num = tid.split("#", 1)
      repo_slug = lhs.strip() if "/" in lhs else "flutter/flutter"
      return f"github_issue:{repo_slug}:{num.strip()}"
    if ":" in tid:
      return f"github_issue:{tid}"
    return f"github_issue:flutter/flutter:{tid.lstrip('#')}"
  if k in ("buganizer", "b", "crbug"):
    clean_tid = re.sub(r"^(?:b|crbug)/", "", tid, flags=re.IGNORECASE).strip()
    return f"buganizer:{clean_tid}"
  if k in (
      "piper",
      "gmail_thread",
      "gmail_draft",
      "gchat_thread",
      "gdoc",
      "gdoc_thread",
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


def canonicalize_companion_parent(
    artifact_key: str,
    state: Mapping[str, Any] | None = None,
    title: str = "",
) -> str:
  """Resolves a companion CL/PR's canonical parent artifact_key across Skia/Chromium/GitHub."""
  st = state or {}
  child_key = canonicalize_artifact_key(
      artifact_key, host=str(st.get("host") or "")
  )
  # Inherit the child's Chromium Gerrit host when companion_to omits the repo (e.g. gerrit:8595771)
  inferred_host = str(st.get("host") or "")
  if not inferred_host and child_key.startswith("gerrit:chromium:"):
    inferred_host = "chromium"

  raw_parent = str(st.get("companion_to") or "").strip()
  if raw_parent:
    if ":" in raw_parent:
      cand = canonicalize_artifact_key(raw_parent, host=inferred_host)
    else:
      inferred_kind = "github_pr" if child_key.startswith("github_pr:") else "gerrit"
      cand = canonicalize_artifact_key(
          inferred_kind, raw_parent.lstrip("#"), host=inferred_host
      )
    return cand if cand != child_key else ""

  # Search subject and fallback title together so parent IDs in either field are discovered
  subj = " | ".join(
      s
      for s in (
          str(st.get("subject") or "").strip(),
          str(st.get("title") or "").strip(),
          str(title or "").strip(),
      )
      if s
  )
  if not (st.get("is_companion") or is_companion_title(subj)):
    return ""
  m_cr = re.search(r"\bcrrev(?:\.com)?/c/(\d{6,8})\b", subj, re.IGNORECASE)
  if m_cr:
    cand = f"gerrit:chromium:{m_cr.group(1)}"
    if cand != child_key:
      return cand
  m_sk = re.search(
      r"(?:skia-review\.googlesource\.com/c/skia/\+/|\bGerrit\s+)(\d{6,8})\b",
      subj,
      re.IGNORECASE,
  )
  if m_sk:
    cand = f"gerrit:skia:{m_sk.group(1)}"
    if cand != child_key:
      return cand
  # Match parent (#N) / PR #N in GitHub companion Draft PR titles while skipping self-references
  if child_key.startswith("github_pr:"):
    for m_gh in re.finditer(r"(?:#|PR\s*#|pull/)(\d{4,7})\b", subj, re.IGNORECASE):
      cand = f"github_pr:flutter/flutter:{m_gh.group(1)}"
      if cand != child_key:
        return cand
  elif child_key.startswith("gerrit:"):
    m_for = re.search(r"\b(?:for|on)\s+(\d{6,8})\b", subj, re.IGNORECASE)
    if m_for:
      cand = canonicalize_artifact_key("gerrit", m_for.group(1), host=inferred_host)
      if cand != child_key:
        return cand
  return ""


def is_companion_artifact(
    artifact_key: str,
    state: Mapping[str, Any] | None = None,
    title: str = "",
    link_reason: str = "",
) -> bool:
  """Returns True when a CL/PR artifact is a review/probe companion rather than a standalone CL."""
  akey = str(artifact_key or "")
  if akey and not akey.startswith(("gerrit:", "github_pr:", "piper:", "github:", "cl:", "pr:")):
    return False
  st = state or {}
  est = st.get("entity_state") if isinstance(st.get("entity_state"), Mapping) else {}
  if (
      st.get("is_companion")
      or st.get("companion_to")
      or est.get("is_companion")
      or link_reason == "companion_wip"
  ):
    return True
  subj = str(st.get("subject") or st.get("title") or "")
  return is_companion_title(subj) or is_companion_title(title)


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
  if is_companion_artifact(artifact_key, st, subj) or st.get("is_cherry_pick"):
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


_RE_COMMIT_FLUTTER_BUG = re.compile(
    r"(?:github\.com/flutter/flutter/issues/|\bflutter#)(\d{4,7})\b",
    re.IGNORECASE,
)
_RE_COMMIT_BUGANIZER = re.compile(
    r"(?:(?:b\.corp\.google\.com|issues\.chromium\.org)/issues/"
    r"|\b(?:b|crbug(?:\.com)?)/)(\d{7,10})\b",
    re.IGNORECASE,
)


def extract_cl_bug_keys(state: Mapping[str, Any] | None) -> list[str]:
  """Extracts canonical bug keys linked to a CL/PR via linked_bugs, bug_ids, or commit_message."""
  st = state or {}
  seen: dict[str, None] = {}
  # Respect pre-normalized linked_bug_keys stored during cl_watcher_state ingestion
  for k in st.get("linked_bug_keys") or []:
    if k:
      seen[canonicalize_artifact_key(str(k))] = None
  # Parse structured linked_bugs entries via canonicalize_watcher_bug so repo slugs stay intact
  for lb in st.get("linked_bugs") or []:
    if not isinstance(lb, Mapping):
      continue
    b_ck, _, _, _ = canonicalize_watcher_bug("", lb)
    if b_ck:
      seen[b_ck] = None
  # Parse legacy bug_ids list when present
  for bid in st.get("bug_ids") or []:
    b_str = str(bid).strip()
    if b_str.isdigit():
      kind = "flutter_issue" if len(b_str) <= 6 else "buganizer"
      seen[canonicalize_artifact_key(kind, b_str)] = None
  # Parse Bug: / Part of / Fixes URLs and shortlinks from commit_message
  cm = str(st.get("commit_message") or "")
  if cm:
    for m in _RE_COMMIT_FLUTTER_BUG.finditer(cm):
      seen[canonicalize_artifact_key("flutter_issue", m.group(1))] = None
    for m in _RE_COMMIT_BUGANIZER.finditer(cm):
      seen[canonicalize_artifact_key("buganizer", m.group(1))] = None
  return list(seen.keys())


def format_short_cl_id(
    artifact_key: str, state: Mapping[str, Any] | None = None
) -> str:
  """Formats a compact human-readable CL/PR label with patchset number when known."""
  st = state or {}
  parts = str(artifact_key or "").split(":")
  kind = parts[0] if parts else ""
  tid = parts[-1] if parts else str(artifact_key or "")
  ps = int(st.get("patchset") or 0)
  ps_part = f" PS{ps}" if ps > 0 else ""
  if kind == "gerrit":
    if len(parts) >= 3 and parts[1] == "chromium":
      return f"crrev/c/{tid}{ps_part}"
    return f"Gerrit {tid}{ps_part}"
  if kind == "github_pr":
    return f"PR #{tid}{ps_part}"
  if kind == "piper":
    return f"cl/{tid}"
  return f"{tid}{ps_part}"


def format_short_bug_id(artifact_key: str) -> str:
  """Formats a canonical bug artifact_key into flutter#<N> or b/<N>."""
  parts = str(artifact_key or "").split(":")
  tid = parts[-1] if parts else str(artifact_key or "")
  if str(artifact_key or "").startswith("github_issue:"):
    # Keep non-flutter/flutter GitHub repos unambiguous in short labels
    if len(parts) >= 3 and parts[1] != "flutter/flutter":
      return f"{parts[1]}#{tid}"
    return f"flutter#{tid}"
  if str(artifact_key or "").startswith("buganizer:"):
    return f"b/{tid}"
  return tid


_RE_GH_ISSUE_URL = re.compile(
    r"github\.com/([^/]+/[^/]+)/issues/(\d+)", re.IGNORECASE
)
_MY_BUG_ASSIGNEES = frozenset({"jlavrova", "rusino"})


def canonicalize_watcher_bug(
    raw_key: str, bval: Mapping[str, Any]
) -> tuple[str, str, str, str]:
  """Resolves a cl_watcher_state bug entry into (canonical_key, kind, title, url)."""
  rk = str(bval.get("bug_key") or bval.get("short_id") or raw_key or "").strip()
  url = str(bval.get("url") or "").strip()
  src = str(bval.get("source") or bval.get("tracker_flavor") or "").strip().lower()
  desc = str(bval.get("title") or bval.get("summary") or "").strip()

  # Detect GitHub issues via #, key prefix, source metadata, github URL, or <=6-digit bare IDs
  m_gh_url = _RE_GH_ISSUE_URL.search(url)
  rk_low = rk.lower()
  is_bug_url = "issues.chromium.org" in url.lower() or "b.corp.google.com" in url.lower()
  is_gh = (
      "#" in rk
      or rk_low.startswith(("gh_issue:", "github_issue:", "flutter_issue:"))
      or src == "github"
      or m_gh_url is not None
      or (
          not rk_low.startswith(("buganizer:", "b/", "crbug"))
          and not is_bug_url
          and rk.isdigit()
          and len(rk) <= 6
      )
  )
  if is_gh:
    # Prefer canonical URL slug when available before falling back to key # syntax
    if m_gh_url is not None:
      repo_slug, num = m_gh_url.group(1), m_gh_url.group(2)
    elif "#" in rk:
      rest = rk.split(":", 1)[1] if rk_low.startswith(("gh_issue:", "github_issue:")) else rk
      lhs, rhs = rest.split("#", 1)
      repo_slug = lhs.strip() if "/" in lhs else "flutter/flutter"
      num = rhs.strip()
    elif rk_low.startswith("github_issue:") and rk.count(":") >= 2:
      _, repo_slug, num = rk.split(":", 2)
      repo_slug, num = repo_slug.strip(), num.strip()
    else:
      repo_slug = "flutter/flutter"
      num = str(bval.get("bug_id") or bval.get("id") or rk.split(":")[-1]).strip().lstrip("#")
    if not num.isdigit():
      return "", "", "", ""
    ckey = f"github_issue:{repo_slug}:{num}"
    short_lbl = format_short_bug_id(ckey)
    title = f"{short_lbl}: {desc}" if desc else short_lbl
    gh_url = url if m_gh_url is not None else f"https://github.com/{repo_slug}/issues/{num}"
    return ckey, "github_issue", title, gh_url

  # Split on both ':' and '/' so 'b/384910293' and 'crbug.com/384910293' yield bare numeric IDs
  raw_bid = str(bval.get("bug_id") or bval.get("id") or rk).strip()
  bid = re.split(r"[:/]", raw_bid)[-1].strip()
  if not bid.isdigit():
    return "", "", "", ""
  ckey = canonicalize_artifact_key("buganizer", bid)
  title = f"b/{bid}: {desc}" if desc else f"b/{bid}"
  return ckey, "buganizer", title, url or f"https://issues.chromium.org/issues/{bid}"


def is_bug_assigned_to_me(state: Mapping[str, Any] | None) -> bool:
  """Returns True when a Buganizer or GitHub issue is assigned to jlavrova or Rusino."""
  if not isinstance(state, Mapping):
    return False
  est = state.get("entity_state") if isinstance(state.get("entity_state"), Mapping) else {}
  raw = str(state.get("assignee") or est.get("assignee") or "").strip().lower()
  if not raw:
    return False
  # Handle multi-assignee GitHub strings (e.g. 'mdebbar, Rusino') and Google email suffixes
  for tok in re.split(r"[,;\s<>]+", raw):
    clean = tok.strip()
    if clean in _MY_BUG_ASSIGNEES or clean.startswith("jlavrova@"):
      return True
  return False


def _default_artifact_url(artifact_key: str) -> str:
  """Builds the canonical web URL for an external tracker artifact_key."""
  akey = str(artifact_key or "")
  num = akey.split(":")[-1]
  if akey.startswith("gerrit:chromium:"):
    return f"https://chromium-review.googlesource.com/c/chromium/src/+/{num}"
  if akey.startswith("gerrit:"):
    return f"https://skia-review.googlesource.com/c/skia/+/{num}"
  if akey.startswith("github_pr:"):
    parts = akey.split(":")
    repo_slug = parts[1] if len(parts) >= 3 and "/" in parts[1] else "flutter/flutter"
    return f"https://github.com/{repo_slug}/pull/{num}"
  if akey.startswith("github_issue:"):
    parts = akey.split(":")
    repo_slug = parts[1] if len(parts) >= 3 and "/" in parts[1] else "flutter/flutter"
    return f"https://github.com/{repo_slug}/issues/{num}"
  if akey.startswith("piper:"):
    return f"http://cl/{num}"
  if akey.startswith("buganizer:"):
    return f"https://issues.chromium.org/issues/{num}"
  if akey.startswith("gmail_thread:"):
    return f"https://mail.google.com/mail/u/0/#inbox/{num}"
  if akey.startswith("gmail_draft:"):
    return f"https://mail.google.com/mail/u/0/#drafts/{num}"
  if akey.startswith("gdoc_thread:"):
    parts = akey.split(":", 2)
    if len(parts) == 3:
      return f"https://docs.google.com/document/d/{parts[1]}/edit?disco={parts[2]}"
  if akey.startswith("gdoc:"):
    doc_id = akey.split(":", 1)[1]
    return f"https://docs.google.com/document/d/{doc_id}/edit"
  if akey.startswith("chat_chain:"):
    return f"/c/{num}"
  return ""


# Build correspondence channel activation metadata for manual UI/CLI create and link paths
def manual_correspondence_state(
    artifact_key: str,
    existing_state: Mapping[str, Any] | None = None,
    *,
    is_channel_slot: bool = False,
) -> dict[str, Any] | None:
  ck = str(artifact_key or "")
  if not (
      ck.startswith(("gmail_thread:", "gchat_thread:", "gdoc_thread:"))
      or (is_channel_slot and ck.startswith("gdoc:"))
  ):
    return None
  st = existing_state or {}
  extra: dict[str, Any] = {"is_active_correspondence": True}
  if "last_from_me" not in st:
    extra["promoted_inbound"] = True
  return extra


def _is_own_cl(st: Mapping[str, Any], est: Mapping[str, Any]) -> bool:
  """Returns True when a CL/PR belongs to Julia rather than an external reviewee."""
  role_l = str(st.get("role") or "").strip().lower()
  return (
      bool(st.get("is_self"))
      or bool(st.get("is_mine"))
      or role_l == "author"
      or (
          "is_self" not in st
          and "is_mine" not in st
          and not role_l
          and not bool(st.get("is_external"))
          and not bool(est.get("is_reviewer_change"))
      )
  )


def _evaluate_companion_alerts(
    akey: str,
    st: Mapping[str, Any],
    parent_key: str = "",
    parent_st: Mapping[str, Any] | None = None,
) -> tuple[list[str], list[str]]:
  """Returns (orphan_reasons, comment_reasons) for a companion probe relative to its parent."""
  orphan_reasons: list[str] = []
  comment_reasons: list[str] = []
  est = st.get("entity_state") if isinstance(st.get("entity_state"), Mapping) else {}
  comp_status = str(st.get("status") or "").strip().upper()
  short_comp = format_short_cl_id(akey, st)
  # Surface the parent card when an external human comments on a non-closed companion
  if comp_status not in ("ABANDONED", "CLOSED", "MERGED"):
    has_human_comment = (
        int(st.get("open_external_threads") or 0) > 0
        or bool(st.get("author_replied_to_me"))
        or bool(st.get("unanswered_human"))
        or bool(est.get("needs_my_reply"))
    )
    if has_human_comment:
      comment_reasons.append(f"💬 Comment on companion: {short_comp}")
  # Require explicit NEW/OPEN status so aged-out transcript stubs do not fire false alerts
  if comp_status in ("NEW", "OPEN"):
    p_status = str((parent_st or {}).get("status") or "").strip().upper()
    parent_terminal = p_status in ("MERGED", "CLOSED", "ABANDONED")
    if parent_terminal or bool(st.get("ready_to_abandon")):
      if parent_key:
        short_parent = format_short_cl_id(parent_key, parent_st)
        state_lbl = p_status.lower() if parent_terminal else "closed"
        p_desc = f"{short_parent} {state_lbl}"
      else:
        p_desc = "parent closed"
      orphan_reasons.append(f"🧹 Close orphaned companion: {short_comp} ({p_desc})")
  return orphan_reasons, comment_reasons


def _evaluate_cl_needs_you(akey: str, st: Mapping[str, Any]) -> str:
  """Returns the Needs You reason for a non-companion CL/PR/bug, or '' when waiting/done."""
  est = st.get("entity_state") if isinstance(st.get("entity_state"), Mapping) else {}
  if akey.startswith(("buganizer:", "github_issue:")):
    # Closed tracker issues never keep a Work in Needs You even if stale attention flags remain
    if str(st.get("status") or "").strip().upper() in CLOSED_BUG_STATUSES:
      return ""
    bug_needs_you = (
        bool(st.get("needs_you"))
        or st.get("lane") == "you"
        or bool(est.get("needs_you"))
        or est.get("lane") == "you"
        or bool(est.get("action_required"))
        or est.get("category") == "needs_me"
    )
    if not bug_needs_you:
      return ""
    why_bug = str(
        st.get("why_needs_you") or est.get("reason") or st.get("attention_reason") or ""
    ).strip()
    return why_bug or f"Needs action: {format_short_bug_id(akey)}"

  # Suppress stale cached flags once a CL/PR is closed, abandoned, or merged without post-submit
  status_u = str(st.get("status") or "NEW").strip().upper()
  if status_u in ("ABANDONED", "CLOSED"):
    return ""
  is_open_cl = status_u in ("NEW", "OPEN")
  if status_u in ("MERGED", "SUBMITTED") and not (
      bool(est.get("is_post_submit_alert"))
      or bool(est.get("action_required"))
      or est.get("lane") == "you"
  ):
    return ""

  is_wip = bool(st.get("is_wip") or st.get("is_draft") or st.get("wip_label"))
  is_own = _is_own_cl(st, est)
  own_conflict = is_open_cl and is_own and not is_wip and bool(st.get("merge_conflict"))
  # Promote clean non-conflicted external reviews awaiting CQ Dry Run -> auto CR+1 finalization
  clean_review_ready = (
      is_open_cl
      and not is_own
      and not is_wip
      and not bool(st.get("merge_conflict"))
      and bool(est.get("can_finalize_clean_review"))
      and not bool(est.get("is_armed_finalize"))
  )
  # Ignore synthetic conflict status on foreign PRs while honoring real review/alert lanes
  est_lane_you = est.get("lane") == "you" and (is_own or est.get("status") != "conflict")
  needs_you = (
      est.get("category") == "needs_me"
      or bool(est.get("action_required"))
      or est_lane_you
      or bool(est.get("is_post_submit_alert"))
      or own_conflict
      or clean_review_ready
  )
  if not needs_you:
    return ""
  short_cl = format_short_cl_id(akey, st)
  if own_conflict or (is_open_cl and is_own and est.get("status") == "conflict"):
    return f"⚠️ Merge conflict: {short_cl}"
  # Prioritize clean-review promotion before passive waiting reasons stored in attention_reason
  if clean_review_ready:
    return f"🟢 Ready to CR+1: {short_cl}"
  # Read both explicit entity_state.reason and cl_watcher's item-level attention_reason
  explicit_reason = str(est.get("reason") or st.get("attention_reason") or "").strip()
  if explicit_reason and (is_own or "conflict" not in explicit_reason.lower()):
    return explicit_reason
  # Suppress spine_short_state 'Conflict' leaked by entity_state.py on foreign PRs
  spine = str((est.get("spine_short_state") or {}).get("short") or "").strip()
  if spine and (is_own or "conflict" not in spine.lower()):
    return f"{spine}: {short_cl}"
  if not is_own and (bool(est.get("is_reviewer_change")) or st.get("role") == "reviewer"):
    return f"Needs review: {short_cl}"
  return f"Needs action: {short_cl}"


def _is_correspondence_silence_ping(
    state: Mapping[str, Any] | None,
    artifacts_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
  """Returns True when a correspondence obligation woke up from outbound 5-7d silence."""
  st = state or {}
  target_st = _get_github_target_state(st, artifacts_by_key)
  eff_ext_ts, gh_self_ts = _resolve_github_thread_turns(st, target_st)
  last_ts = float(st.get("last_message_ts") or st.get("origin_ts") or 0.0)
  # Mirror _eval_single_predicate so manual wait, MCP/GitHub replies, and acks match 7d pings
  override_ts = max(
      float(st.get("manual_wait_ts") or 0.0),
      float(st.get("mcp_reply_ts") or 0.0),
      float(st.get("ack_message_ts") or 0.0),
      gh_self_ts,
  )
  gh_human_ts = max(
      float(st.get("gh_human_reply_ts") or 0.0),
      float(target_st.get("last_human_comment_at") or 0.0),
  )
  # Newer GitHub human comment before Gmail delivery must read as Reply needed, not silence ping
  if gh_human_ts > max(last_ts, override_ts):
    return False
  from_me = (
      bool(st.get("last_from_me"))
      if "last_from_me" in st
      else (not bool(st.get("promoted_inbound")))
  )
  if from_me:
    return True
  compare_ts = eff_ext_ts if eff_ext_ts > 0 else float(st.get("origin_ts") or 0.0)
  return override_ts > 0.0 and override_ts >= compare_ts


def _is_reviewer_cl_state(st: Mapping[str, Any], est: Mapping[str, Any]) -> bool:
  """Returns True when Julia is an active reviewer (not CC, watcher, or removed) on a CL/PR."""
  if bool(st.get("is_self")) or bool(st.get("is_mine")):
    return False
  if bool(st.get("reviewer_removed")) or bool(st.get("is_watching")):
    return False
  role_l = str(st.get("role") or "").strip().lower()
  if role_l:
    if role_l in ("author", "owner", "cc", "watcher", "watching"):
      return False
    if role_l.startswith("reviewer"):
      return True
  if "is_reviewer_change" in est:
    return bool(est.get("is_reviewer_change"))
  if bool(st.get("is_external")):
    return True
  return st.get("is_self") is False or st.get("is_mine") is False


def _is_own_cl_state(st: Mapping[str, Any], est: Mapping[str, Any]) -> bool:
  """Returns True when a CL/PR belongs to Julia and is not an unhydrated empty stub."""
  if not st and not est:
    return False
  role_l = str(st.get("role") or "").strip().lower()
  if bool(st.get("is_self")) or bool(st.get("is_mine")) or role_l == "author":
    return True
  # Require tracker metadata so empty chat-mention stubs do not masquerade as own CLs
  has_tracker_meta = any(
      k in st for k in ("owner", "author", "status", "entity_state", "subject")
  )
  return has_tracker_meta and _is_own_cl(st, est)


def _compute_is_multi_channel(w_arts: Iterable[Mapping[str, Any]]) -> bool:
  """Returns True when a card spans >=2 distinct non-context, non-companion channels/entities."""
  units: set[str] = set()
  linked_gdoc_ids: set[str] = set()
  for art in w_arts:
    if str(art.get("role") or "") == "context_only":
      continue
    akey = str(art.get("artifact_key") or "")
    if akey.startswith("gdoc:") and ":" in akey:
      linked_gdoc_ids.add(akey.split(":", 1)[1])
    elif akey.startswith("gdoc_thread:") and akey.count(":") >= 2:
      linked_gdoc_ids.add(akey.split(":", 2)[1])

  for art in w_arts:
    if str(art.get("role") or "") == "context_only":
      continue
    akey = str(art.get("artifact_key") or "")
    kind = str(art.get("kind") or artifact_kind_from_key(akey))
    st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
    # Companion WIP probes belong to their parent review/CL and do not add a channel
    if kind in ("gerrit", "github_pr", "piper") and is_companion_artifact(
        akey, st, str(art.get("title") or ""), str(art.get("link_reason") or "")
    ):
      continue
    if kind == "chat_chain":
      units.add("channel:chat")
    elif kind == "gmail_draft":
      if str(st.get("status") or "DRAFT").upper() != "DRAFT":
        continue
      tid = str(st.get("thread_id") or "").strip()
      units.add(f"mail:{tid}" if tid else f"draft:{akey}")
    elif kind == "gmail_thread":
      # Collapse GDoc comment/share notification emails into the same channel as their doc
      gd_id = str(st.get("gdoc_id") or "").strip()
      if gd_id and gd_id in linked_gdoc_ids and bool(
          st.get("is_gdoc_comment") or st.get("is_drive_share")
      ):
        units.add(f"gdoc:{gd_id}")
      else:
        tid = akey.split(":", 1)[1] if ":" in akey else akey
        units.add(f"mail:{tid}")
    elif kind == "gdoc_thread" and akey.count(":") >= 2:
      units.add(f"gdoc:{akey.split(':', 2)[1]}")
    elif kind == "gdoc":
      doc_id = akey.split(":", 1)[1] if ":" in akey else akey
      units.add(f"gdoc:{doc_id}")
    elif kind == "roadmap_item":
      continue
    elif akey:
      units.add(akey)
  return len(units) >= 2


def _collect_suppressed_threads(
    w_arts: list[dict[str, Any]], w_obs: list[dict[str, Any]]
) -> set[str]:
  """Returns correspondence artifact_keys covered by parked drafts or superseded by GDoc rows."""
  suppressed: set[str] = set()
  art_state_by_key: dict[str, Mapping[str, Any]] = {}
  all_art_by_key: dict[str, Mapping[str, Any]] = {}
  gmail_channels: list[str] = []
  linked_non_ctx_keys: set[str] = set()
  linked_gdoc_thread_docs: set[str] = set()
  for art in w_arts:
    ak = str(art.get("artifact_key") or "")
    if ak:
      all_art_by_key[ak] = art
    if str(art.get("role") or "") == "context_only":
      continue
    linked_non_ctx_keys.add(ak)
    if ak.startswith("gdoc_thread:") and ak.count(":") >= 2:
      linked_gdoc_thread_docs.add(ak.split(":", 2)[1])
    art_state_by_key[ak] = art.get("state") if isinstance(art.get("state"), Mapping) else {}
    if ak.startswith("gmail_thread:"):
      gmail_channels.append(ak)

  # Suppress inbound thread alerts already covered by an open auto-gated reply/Gmail draft
  for ob in w_obs:
    if str(ob.get("status") or "") != "open":
      continue
    t_key = str(ob.get("target_artifact_key") or "")
    try:
      ob_meta = json.loads(str(ob.get("meta_json") or "{}"))
    except Exception:
      ob_meta = {}
    is_wb_ob = bool(
        ob.get("wb_reply_draft")
        or ob_meta.get("wb_reply_draft")
        or str(ob.get("obligation_id") or "").startswith("ob-wbdraft-")
    )
    if is_wb_ob:
      d_upd_ts = float(ob_meta.get("draft_updated_ts") or ob.get("created_at") or 0.0)
      if t_key.startswith("gmail_thread:") and _is_thread_covered_by_draft_ts(
          art_state_by_key.get(t_key), d_upd_ts, all_art_by_key
      ):
        suppressed.add(t_key)
    if t_key.startswith("gmail_draft:"):
      d_st = art_state_by_key.get(t_key) or {}
      d_upd_ts = float(d_st.get("draft_updated_ts") or ob.get("created_at") or 0.0)
      d_tid = str(d_st.get("thread_id") or "").strip()
      cand_t = (
          f"gmail_thread:{d_tid}"
          if d_tid
          else (gmail_channels[0] if len(gmail_channels) == 1 else "")
      )
      if cand_t and _is_thread_covered_by_draft_ts(
          art_state_by_key.get(cand_t), d_upd_ts, all_art_by_key
      ):
        suppressed.add(cand_t)

  # Suppress raw GDoc/Drive notification transport threads superseded by a linked GDoc entity
  for ak in gmail_channels:
    st = art_state_by_key.get(ak) or {}
    gt_ref = str(st.get("gdoc_thread_key") or "")
    sd_ref = str(st.get("shared_doc_key") or "")
    gd_id = str(st.get("gdoc_id") or "")
    if (
        (gt_ref and gt_ref in linked_non_ctx_keys)
        or (sd_ref and sd_ref in linked_non_ctx_keys)
        or (
            bool(st.get("is_gdoc_comment") or st.get("is_drive_share"))
            and bool(gd_id)
            and (f"gdoc:{gd_id}" in linked_non_ctx_keys or gd_id in linked_gdoc_thread_docs)
        )
    ):
      suppressed.add(ak)
  return suppressed


def _classify_card_types(
    lane: str,
    w_arts: list[dict[str, Any]],
    w_obs: list[dict[str, Any]],
    reply_draft: Mapping[str, Any] | None,
) -> tuple[list[str], list[str]]:
  """Computes (work_types, needs_you_types) over ('replies', 'reviews', 'my-bugs', 'my-cls')."""
  work_types: set[str] = set()
  needs_types: set[str] = set()
  is_done = lane == "done"
  is_needs = lane == "needs-you"

  has_open_wb_ob = False
  has_ready_wb_ob = False
  gated_draft_keys: set[str] = set()
  suppressed_threads = _collect_suppressed_threads(w_arts, w_obs)
  art_state_map = {str(a.get("artifact_key") or ""): a for a in w_arts}
  non_comp_cls: dict[str, dict[str, Any]] = {}

  # Classify correspondence obligations and identify parked auto-gated Gmail drafts
  for ob in w_obs:
    ob_st = str(ob.get("status") or "")
    ob_kind = str(ob.get("kind") or "")
    t_key = str(ob.get("target_artifact_key") or "")
    is_wb_ob = bool(
        ob.get("wb_reply_draft")
        or str(ob.get("obligation_id") or "").startswith("ob-wbdraft-")
    )
    is_corr_ob = (
        ob_kind in ("correspondence", "reply")
        or is_wb_ob
        or t_key.startswith(("gmail_", "gchat_", "gdoc"))
    )
    if is_corr_ob and (is_done or ob_st != "done"):
      work_types.add("replies")
    if ob_st == "open":
      if is_wb_ob:
        has_open_wb_ob = True
      if t_key.startswith("gmail_draft:"):
        gated_draft_keys.add(t_key)
    elif ob_st == "ready":
      if is_wb_ob:
        has_ready_wb_ob = True
      if is_needs and is_corr_ob:
        needs_types.add("replies")
    if is_needs and bool(ob.get("stalled_alert")) and is_corr_ob:
      needs_types.add("replies")

  # Live inline reply drafts stay in Replies, while parked auto-gated ones stay non-urgent
  if reply_draft is not None:
    work_types.add("replies")
    mat_dk = str(reply_draft.get("materialized_draft_key") or "")
    if mat_dk and (has_open_wb_ob or has_ready_wb_ob):
      gated_draft_keys.add(mat_dk)
    if is_needs and not has_open_wb_ob and not has_ready_wb_ob:
      needs_types.add("replies")

  # Index primary non-companion CLs first so companion alerts can resolve their parent role
  for art in w_arts:
    if str(art.get("role") or "") == "context_only":
      continue
    akey = str(art.get("artifact_key") or "")
    kind = str(art.get("kind") or "")
    st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
    if kind in ("gerrit", "github_pr", "piper") and not is_companion_artifact(
        akey, st, str(art.get("title") or ""), str(art.get("link_reason") or "")
    ):
      non_comp_cls[akey] = art

  for art in w_arts:
    if str(art.get("role") or "") == "context_only":
      continue
    akey = str(art.get("artifact_key") or "")
    kind = str(art.get("kind") or "")
    st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
    est = st.get("entity_state") if isinstance(st.get("entity_state"), Mapping) else {}

    if kind == "gmail_draft":
      is_live_draft = str(st.get("status") or "DRAFT").upper() == "DRAFT"
      if is_done or is_live_draft:
        work_types.add("replies")
      if is_needs and is_live_draft and akey not in gated_draft_keys:
        needs_types.add("replies")
    elif is_active_correspondence_artifact(kind, st):
      gh_resolved = is_github_thread_resolved(st, art_state_map)
      if is_done or not gh_resolved:
        work_types.add("replies")
      if (
          is_needs
          and not gh_resolved
          and bool(st.get("unanswered_human"))
          and akey not in suppressed_threads
      ):
        needs_types.add("replies")
    elif kind in ("buganizer", "github_issue"):
      st_upper = str(st.get("status") or "NEW").strip().upper()
      # Only issues assigned to jlavrova / Rusino belong in My Bugs
      if is_bug_assigned_to_me(st) and (is_done or st_upper not in CLOSED_BUG_STATUSES):
        work_types.add("my-bugs")
        if is_needs:
          needs_types.add("my-bugs")
    elif kind in ("gerrit", "github_pr", "piper"):
      if is_companion_artifact(
          akey, st, str(art.get("title") or ""), str(art.get("link_reason") or "")
      ):
        # Attribute companion probes and any active alerts to their parent CL's work_types
        p_key = canonicalize_companion_parent(akey, st, str(art.get("title") or ""))
        p_art = non_comp_cls.get(p_key) if p_key else None
        if p_art is None and not p_key and len(non_comp_cls) == 1:
          p_key, p_art = next(iter(non_comp_cls.items()))
        p_st = (
            p_art.get("state")
            if (p_art is not None and isinstance(p_art.get("state"), Mapping))
            else None
        )
        p_est = (
            p_st.get("entity_state")
            if (isinstance(p_st, Mapping) and isinstance(p_st.get("entity_state"), Mapping))
            else {}
        )
        comp_type = (
            "my-cls"
            if (isinstance(p_st, Mapping) and _is_own_cl_state(p_st, p_est))
            else "reviews"
        )
        work_types.add(comp_type)
        if is_needs:
          o_r, c_r = _evaluate_companion_alerts(akey, st, p_key, p_st)
          if o_r or c_r:
            needs_types.add(comp_type)
        continue

      cl_hot = is_needs and str(st.get("status") or "NEW").upper() not in (
          "ABANDONED",
          "CLOSED",
      ) and bool(_evaluate_cl_needs_you(akey, st))
      if _is_reviewer_cl_state(st, est):
        work_types.add("reviews")
        if cl_hot:
          needs_types.add("reviews")
      elif _is_own_cl_state(st, est):
        work_types.add("my-cls")
        if cl_hot:
          needs_types.add("my-cls")

  ordered = ("replies", "reviews", "my-bugs", "my-cls")
  return (
      [t for t in ordered if t in work_types],
      [t for t in ordered if t in needs_types],
  )


def compute_artifact_provenance(
    art: Mapping[str, Any],
    w_arts: list[dict[str, Any]],
) -> tuple[str, str]:
  """Returns (why_badge, why_tooltip) explaining how an artifact is connected to its Work."""
  akey = str(art.get("artifact_key") or "")
  kind = str(art.get("kind") or "")
  role = str(art.get("role") or "primary")
  source = str(art.get("source") or "auto")
  reason = str(art.get("link_reason") or "").strip()
  detail = str(art.get("link_detail") or "").strip()
  st = art.get("state") if isinstance(art.get("state"), Mapping) else {}

  primary_cls = [
      a
      for a in w_arts
      if a.get("role") == "primary"
      and a.get("kind") in ("gerrit", "github_pr", "piper")
      and not is_companion_artifact(
          str(a.get("artifact_key") or ""),
          a.get("state") if isinstance(a.get("state"), Mapping) else None,
          str(a.get("title") or ""),
          str(a.get("link_reason") or ""),
      )
  ]
  primary_bugs = [
      a
      for a in w_arts
      if a.get("role") == "primary" and a.get("kind") in ("buganizer", "github_issue")
  ]
  has_draft = any(a.get("kind") == "gmail_draft" for a in w_arts)
  has_email = any(a.get("kind") == "gmail_thread" for a in w_arts)

  if source == "manual" or reason == "manual":
    return "вручную", detail or "Привязано к карточке вручную пользователем"

  # Label companion probes as subordinate to their parent CL/PR rather than standalone deliverables
  if is_companion_artifact(akey, st, str(art.get("title") or ""), reason):
    comp_parent = canonicalize_companion_parent(akey, st, str(art.get("title") or ""))
    parent_num = comp_parent.split(":")[-1] if comp_parent else ""
    if not parent_num and primary_cls:
      parent_num = str(primary_cls[0].get("artifact_key") or "").split(":")[-1]
    badge = f"companion WIP к {parent_num}" if parent_num else "companion WIP"
    tip = detail or (
        f"Тестовый/проверочный companion WIP СЛ к ревью {parent_num}"
        if parent_num
        else "Тестовый/проверочный companion WIP СЛ"
    )
    return badge, tip

  if reason == "gmail_digest_ref":
    return (
        "упомянуто в сводке",
        detail or "Упомянуто в сводном письме (открытые карточки не слиты)",
    )

  if role == "context_only" or reason == "chat_mention":
    badge = "упомянут в чате"
    for a in w_arts:
      if a.get("kind") == "chat_chain":
        cid_short = str(a.get("artifact_key") or "").split(":")[-1][:8]
        if cid_short and cid_short in detail:
          badge = f"упомянут в чате {cid_short}"
          break
    tip = detail or "Попутно упомянут по ходу обсуждения в чате (не склеивает карточки)"
    return badge, tip

  if kind in ("gerrit", "github_pr", "piper"):
    cl_bugs = [
        bk
        for bk in extract_cl_bug_keys(st)
        if any(b.get("artifact_key") == bk for b in w_arts)
    ]
    if cl_bugs or reason == "bug_trailer":
      b_labels = [format_short_bug_id(bk) for bk in cl_bugs[:2]]
      if not b_labels and primary_bugs:
        b_labels = [format_short_bug_id(str(primary_bugs[0].get("artifact_key")))]
      b_str = ", ".join(b_labels)
      badge = f"фикс {b_str}" if b_str else "по багу"
      tip = detail or (f"В описании СЛ указан баг {b_str}" if b_str else "Связан через Bug: в описании СЛ")
      return badge, tip
    stack_p = str(st.get("stack_parent_ikey") or "").strip()
    if stack_p or reason == "stack_child":
      p_num = stack_p.split(":")[-1] if stack_p else ""
      badge = f"стек от {p_num}" if p_num else "в стеке Gerrit"
      tip = detail or (f"Дочерний СЛ в стеке Gerrit поверх {p_num}" if p_num else "Связан общим стеком Gerrit")
      return badge, tip
    if any(
        "ревью" in str(c.get("title") or "").lower()
        or "review" in str(c.get("title") or "").lower()
        for c in w_arts
        if c.get("kind") == "chat_chain"
    ):
      return "тема ревью", detail or "Целевой СЛ на код-ревью из заголовка/первого сообщения чата"
    return "основной СЛ", detail or "Главный СЛ этой задачи"

  if kind in ("buganizer", "github_issue"):
    referencing_cls = []
    for c in w_arts:
      if c.get("kind") in ("gerrit", "github_pr", "piper"):
        c_st = c.get("state") if isinstance(c.get("state"), Mapping) else {}
        if akey in extract_cl_bug_keys(c_st):
          referencing_cls.append(str(c.get("artifact_key") or "").split(":")[-1])
    if referencing_cls:
      c_str = ", ".join(referencing_cls[:2])
      return (
          f"в описании {c_str}",
          detail or f"Указан в Bug: трейлере описания СЛ {c_str}",
      )
    if reason == "gmail_draft_ref" or has_draft:
      return "из черновика письма", detail or "Упомянут в черновике письма Gmail"
    if reason == "gmail_thread_ref" or has_email:
      return "из письма", detail or "Упомянут в цепочке писем Gmail"
    return "тема чата", detail or "Целевой баг из первого сообщения или заголовка чата"

  if kind == "gmail_draft":
    return "черновик ответа", detail or "Черновик письма в Gmail со ссылками на артефакты задачи"

  if kind == "gmail_thread":
    if st.get("is_github_pr_thread") or reason == "github_pr_thread":
      return "дискуссия в PR", detail or "Обсуждение в комментариях GitHub PR"
    if st.get("is_github_issue_thread") or reason == "github_issue_thread":
      return "дискуссия в issue", detail or "Живое обсуждение в комментариях GitHub Issue"
    if role == "primary" or reason == "gmail_root":
      return "основа карточки", detail or "Корневая переписка, объединяющая артефакты задачи"
    return "переписка по задаче", detail or "Цепочка писем в Gmail, связанная с артефактами задачи"

  if kind == "gdoc_thread":
    return "комментарий в доке", detail or "Живая ветка комментариев в Google Doc"

  if kind == "gdoc":
    if st.get("is_active_correspondence"):
      badge = "основа карточки" if role == "primary" else "переписка в доке"
      return badge, detail or "Расшаренный для обсуждения документ Google Docs"
    return "документ задачи", detail or "Связанный документ Google Docs"

  if kind == "chat_chain":
    t_lower = str(art.get("title") or "").lower()
    is_rev = "ревью" in t_lower or "review" in t_lower
    if primary_cls:
      cl_num = str(primary_cls[0].get("artifact_key") or "").split(":")[-1]
      badge = f"ревью {cl_num}" if is_rev else f"чат по {cl_num}"
      tip = detail or f"Рабочий чат по СЛ {cl_num}"
      return badge, tip
    if primary_bugs:
      b_short = format_short_bug_id(str(primary_bugs[0].get("artifact_key") or ""))
      badge = f"чат по {b_short}"
      tip = detail or f"Рабочий чат по багу {b_short}"
      return badge, tip
    return "чат задачи", detail or "Рабочий чат этой задачи"

  return reason or "связано", detail or "Связанный артефакт задачи"


_DUMMY_DRAFT_IDS = frozenset({
    "12345",
    "123456",
    "1234567",
    "12345678",
    "123456789",
})
_RE_DRAFT_GH_HOVERCARD_ATTR = re.compile(
    r'data-(?:url|hydro-click)=(["\'])(?:(?!\1).)*?'
    r"github\.com/flutter/flutter/issues/\d+(?:(?!\1).)*?\1",
    re.IGNORECASE,
)
_RE_DRAFT_MENTION_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"(?:skia-review\.googlesource\.com/c/skia/\+/"
            r"|\b(?:Skia\s+)?CL\s+|\bGerrit\s+)(\d{6,8})\b",
            re.IGNORECASE,
        ),
        "gerrit:skia:",
    ),
    (
        re.compile(
            r"(?:chromium-review\.googlesource\.com/c/chromium/src/\+/"
            r"|\bcrrev(?:\.com)?/c/)(\d{6,8})\b",
            re.IGNORECASE,
        ),
        "gerrit:chromium:",
    ),
    (
        re.compile(
            r"(?:github\.com/flutter/flutter/pull/|\bPR\s+#)(\d{4,7})\b",
            re.IGNORECASE,
        ),
        "github_pr:flutter/flutter:",
    ),
    (
        re.compile(
            r"(?:github\.com/flutter/flutter/issues/|\bflutter#)(\d{4,7})\b",
            re.IGNORECASE,
        ),
        "github_issue:flutter/flutter:",
    ),
    (
        re.compile(r"(?:http://cl/|\bcl/|\bCL\s+)(\d{9,10})\b", re.IGNORECASE),
        "piper:",
    ),
    (
        re.compile(
            r"(?:(?:b\.corp\.google\.com|issues\.chromium\.org)/issues/"
            r"|\b(?:b|crbug(?:\.com)?)/)(\d{7,10})\b",
            re.IGNORECASE,
        ),
        "buganizer:",
    ),
    (
        re.compile(r"docs\.google\.com/document/d/([A-Za-z0-9_\-]{20,64})", re.IGNORECASE),
        "gdoc:",
    ),
)


def _wb_draft_obligation_id(work_id: str) -> str:
  """Returns the deterministic obligation ID for a Work's live Workbench reply draft."""
  digest = hashlib.sha1(f"wbdraft:{work_id}".encode("utf-8")).hexdigest()[:10]
  return f"ob-wbdraft-{digest}"


def _is_thread_covered_by_draft_ts(
    thread_state: Mapping[str, Any] | None,
    draft_updated_ts: float,
    artifacts_by_key: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
  """Returns True when no unacknowledged counterparty message is newer than the draft."""
  st = thread_state or {}
  target_st = _get_github_target_state(st, artifacts_by_key)
  eff_ext_ts, gh_self_ts = _resolve_github_thread_turns(st, target_st)
  gh_human_ts = max(
      float(st.get("gh_human_reply_ts") or 0.0),
      float(target_st.get("last_human_comment_at") or 0.0),
  )
  last_msg_ts = float(st.get("last_message_ts") or 0.0)
  if bool(st.get("last_from_me")) and gh_human_ts <= max(last_msg_ts, gh_self_ts):
    return True
  latest_ext_ts = max(eff_ext_ts, gh_human_ts)
  if latest_ext_ts <= 0.0:
    return draft_updated_ts > 0.0
  covered_floor = max(
      draft_updated_ts,
      float(st.get("ack_message_ts") or 0.0),
      float(st.get("manual_wait_ts") or 0.0),
      float(st.get("mcp_reply_ts") or 0.0),
      float(st.get("resolved_at") or 0.0),
      gh_self_ts,
  )
  return covered_floor >= latest_ext_ts


def _active_reply_draft_from_meta(raw_meta_json: Any) -> dict[str, Any] | None:
  """Extracts an active unsent reply_draft dict from a Work's meta_json payload."""
  if isinstance(raw_meta_json, Mapping):
    meta = dict(raw_meta_json)
  else:
    try:
      meta = json.loads(str(raw_meta_json or "{}"))
    except Exception:
      meta = {}
  rd = meta.get("reply_draft")
  if not isinstance(rd, Mapping):
    return None
  if float(rd.get("sent_at") or 0.0) > 0.0:
    return None
  if not str(rd.get("body") or "").strip():
    return None
  return dict(rd)


def _extract_draft_mentioned_keys(text: str) -> list[str]:
  """Extracts canonical CL/PR/bug/doc artifact keys referenced in a reply draft."""
  raw = str(text or "")
  if not raw:
    return []
  # Mask GitHub PR hovercard /issues/<N> attributes so PR links never emit phantom issues
  masked_issue_text = _RE_DRAFT_GH_HOVERCARD_ATTR.sub(
      lambda m: " " * len(m.group(0)), raw
  )
  hits: list[tuple[int, str]] = []
  pr_nums: set[str] = set()
  for rx, prefix in _RE_DRAFT_MENTION_PATTERNS:
    scan_target = masked_issue_text if prefix == "github_issue:flutter/flutter:" else raw
    for m in rx.finditer(scan_target):
      num = m.group(1)
      if prefix != "gdoc:" and num in _DUMMY_DRAFT_IDS:
        continue
      if prefix == "github_pr:flutter/flutter:":
        pr_nums.add(num)
      hits.append((m.start(), f"{prefix}{num}"))
  hits.sort(key=lambda x: x[0])
  ordered: list[str] = []
  seen: set[str] = set()
  for _, key in hits:
    if key.startswith("github_issue:flutter/flutter:") and key.split(":")[-1] in pr_nums:
      continue
    ck = canonicalize_artifact_key(key)
    if ck not in seen:
      seen.add(ck)
      ordered.append(ck)
  return ordered


def _merge_reply_draft_meta(
    target_meta_raw: Any, source_meta_raw: Any
) -> dict[str, Any] | None:
  """Copies an active newer reply_draft from source_meta into target_meta; returns updated dict."""
  try:
    t_meta = (
        dict(target_meta_raw)
        if isinstance(target_meta_raw, Mapping)
        else json.loads(str(target_meta_raw or "{}"))
    )
  except Exception:
    t_meta = {}
  src_rd = _active_reply_draft_from_meta(source_meta_raw)
  if src_rd is None:
    return None
  tgt_rd = _active_reply_draft_from_meta(t_meta)
  if tgt_rd is not None and float(tgt_rd.get("updated_at") or 0.0) >= float(
      src_rd.get("updated_at") or 0.0
  ):
    return None
  t_meta["reply_draft"] = src_rd
  return t_meta


def _build_auto_gate_spec(
    work_title: str,
    w_roles: Mapping[str, str],
    art_rows: Mapping[str, Mapping[str, Any]],
    reply_draft: Mapping[str, Any],
) -> tuple[str, str]:
  """Builds (trigger_expr, description) across non-companion primary CLs/bugs for auto_gate."""
  mentioned = [canonicalize_artifact_key(str(k)) for k in (reply_draft.get("mentioned_keys") or [])]
  subj_lbl = str(reply_draft.get("subject") or "").strip()
  if not subj_lbl:
    t_key = str(reply_draft.get("thread_key") or "")
    t_row = art_rows.get(t_key) or {}
    t_st = t_row.get("state") if isinstance(t_row.get("state"), Mapping) else {}
    subj_lbl = str(t_st.get("root_subject") or t_row.get("title") or work_title).strip()

  is_flutter = "flutter" in work_title.lower() or "flutter" in subj_lbl.lower()
  if not is_flutter:
    for ak, role in w_roles.items():
      if role == "context_only":
        continue
      if ak.startswith(("github_issue:flutter/flutter:", "github_pr:flutter/flutter:")):
        is_flutter = True
        break
      ar = art_rows.get(ak) or {}
      if ak.startswith("gmail_thread:") and "flutter" in str(ar.get("title") or "").lower():
        is_flutter = True
        break
  if not is_flutter and any(k.startswith("github_issue:flutter/flutter:") for k in mentioned):
    is_flutter = True

  # Exclude review scratchpad companion CLs so abandoned probes never block draft readiness
  primary_cls: list[str] = []
  primary_bugs: list[str] = []
  for ak in sorted(w_roles.keys()):
    if w_roles[ak] != "primary":
      continue
    if ak.startswith(("gerrit:", "github_pr:", "piper:")):
      ar = art_rows.get(ak) or {}
      st = ar.get("state") if isinstance(ar.get("state"), Mapping) else {}
      if not is_companion_artifact(ak, st, str(ar.get("title") or "")):
        primary_cls.append(ak)
    elif ak.startswith(("buganizer:", "github_issue:")):
      primary_bugs.append(ak)

  preds: list[str] = []
  for ck in primary_cls:
    if ck.startswith("gerrit:skia:") and is_flutter:
      preds.append(f"cl_merged({ck}) AND rolled_into(Flutter, {ck})")
    else:
      preds.append(f"cl_merged({ck})")
  if not preds:
    for bk in primary_bugs:
      preds.append(f"bug_closed({bk})")
  if not preds:
    return "", ""

  short_subj = subj_lbl or work_title or "thread"
  if primary_cls:
    suffix = "land & roll into Flutter" if is_flutter else "merge"
    desc = f"Materialize & send reply in '{short_subj}' once linked CLs {suffix}"
  else:
    desc = f"Materialize & send reply in '{short_subj}' once linked bugs close"
  return " AND ".join(preds), desc


def build_work_checklist(
    w_arts: list[dict[str, Any]],
    art_rows: Mapping[str, dict[str, Any]],
    reply_draft: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
  """Builds the per-bug CL/PR pipeline checklist and obligation progress summary."""
  draft_mentioned_keys: set[str] = set()
  ordered_bug_keys: list[str] = []
  has_draft = False
  has_wb_reply_draft = False

  if reply_draft and str(reply_draft.get("body") or "").strip():
    has_draft = True
    has_wb_reply_draft = True
    wb_mks = reply_draft.get("mentioned_keys") or _extract_draft_mentioned_keys(
        f"{reply_draft.get('subject') or ''}\n{reply_draft.get('body') or ''}"
    )
    for mk in wb_mks:
      ck = canonicalize_artifact_key(str(mk))
      draft_mentioned_keys.add(ck)
      if ck.startswith(("github_issue:", "buganizer:")) and ck not in ordered_bug_keys:
        ordered_bug_keys.append(ck)

  # Preserve the exact bug order from the active Gmail draft when one is attached
  for a in w_arts:
    if a.get("kind") != "gmail_draft":
      continue
    st = a.get("state") or {}
    if str(st.get("status") or "DRAFT").upper() != "DRAFT":
      continue
    has_draft = True
    for mk in st.get("mentioned_keys") or []:
      ck = canonicalize_artifact_key(str(mk))
      draft_mentioned_keys.add(ck)
      if ck.startswith(("github_issue:", "buganizer:")) and ck not in ordered_bug_keys:
        ordered_bug_keys.append(ck)
    for bk in st.get("draft_bug_keys") or []:
      ck = canonicalize_artifact_key(str(bk))
      draft_mentioned_keys.add(ck)
      if ck not in ordered_bug_keys:
        ordered_bug_keys.append(ck)

  # When no draft bug list is present, build the checklist from the Work's primary bugs
  if not ordered_bug_keys:
    for a in w_arts:
      if a.get("role") != "primary":
        continue
      if a.get("kind") in ("buganizer", "github_issue"):
        ck = str(a.get("artifact_key") or "")
        if ck and ck not in ordered_bug_keys:
          ordered_bug_keys.append(ck)

  if not ordered_bug_keys or (len(ordered_bug_keys) == 1 and not has_draft):
    return [], ""

  bug_set = set(ordered_bug_keys)
  # Collect candidate CL/PR artifacts from the Work and global artifact store matching these bugs
  cand_cls: dict[str, dict[str, Any]] = {}
  for a in w_arts:
    if a.get("kind") in ("gerrit", "github_pr", "piper"):
      cand_cls[str(a["artifact_key"])] = a
  for akey, ar in art_rows.items():
    if akey in cand_cls:
      continue
    kind = str(ar.get("kind") or artifact_kind_from_key(akey))
    if kind not in ("gerrit", "github_pr", "piper"):
      continue
    st = ar.get("state") or {}
    if set(extract_cl_bug_keys(st)) & bug_set:
      cand_cls[akey] = {
          "artifact_key": akey,
          "kind": kind,
          "title": str(ar.get("title") or akey),
          "url": str(ar.get("url") or ""),
          "state": st,
      }

  # Map each valid CL/PR to its bug(s), propagating stacked child CLs onto their parent's bug
  cls_by_bug: dict[str, list[tuple[str, dict[str, Any], bool]]] = {
      bk: [] for bk in ordered_bug_keys
  }
  parent_to_bug: dict[str, str] = {}
  deferred_stacked: list[tuple[str, dict[str, Any], str]] = []
  bug_titles_from_cls: dict[str, str] = {}
  bug_status_from_cls: dict[str, str] = {}

  for ckey, c_art in cand_cls.items():
    st = c_art.get("state") or {}
    if not st.get("status"):
      continue
    if is_hub_artifact(ckey, st):
      continue
    for lb in st.get("linked_bugs") or []:
      if isinstance(lb, Mapping):
        b_k, _, _, _ = canonicalize_watcher_bug("", lb)
        if b_k:
          if lb.get("title") and not bug_titles_from_cls.get(b_k):
            bug_titles_from_cls[b_k] = str(lb["title"]).strip()
          if lb.get("status") and not bug_status_from_cls.get(b_k):
            bug_status_from_cls[b_k] = str(lb["status"]).strip()

    b_keys = [bk for bk in extract_cl_bug_keys(st) if bk in bug_set]
    if b_keys:
      for bk in b_keys:
        cls_by_bug[bk].append((ckey, c_art, False))
        parent_to_bug[ckey] = bk
    elif st.get("stack_parent_ikey"):
      p_ckey = canonicalize_artifact_key(str(st.get("stack_parent_ikey")))
      deferred_stacked.append((ckey, c_art, p_ckey))

  for ckey, c_art, p_ckey in deferred_stacked:
    target_bk = parent_to_bug.get(p_ckey)
    if target_bk and all(x[0] != ckey for x in cls_by_bug[target_bk]):
      cls_by_bug[target_bk].append((ckey, c_art, True))

  checklist: list[dict[str, Any]] = []
  n_rolled = 0
  n_merged = 0
  n_review = 0
  n_wip = 0
  n_conflict = 0
  n_outdated = 0

  for bk in ordered_bug_keys:
    b_row = art_rows.get(bk) or {}
    b_st = b_row.get("state") or {}
    short_id = format_short_bug_id(bk)
    raw_b_title = str(
        b_st.get("bug_title")
        or bug_titles_from_cls.get(bk)
        or b_row.get("title")
        or ""
    ).strip()
    # Strip leading short_id prefix ("flutter#160251: ") so short_id and description are distinct
    for pref in (f"{short_id}:", f"{short_id} —", f"{short_id} -", short_id, bk):
      if raw_b_title.lower().startswith(pref.lower()):
        raw_b_title = raw_b_title[len(pref) :].lstrip(" :—–-").strip()
        break
    b_url = str(b_row.get("url") or "") or _default_artifact_url(bk)
    b_status = str(b_st.get("status") or bug_status_from_cls.get(bk) or "").upper()

    cl_items: list[dict[str, Any]] = []
    for ckey, c_art, is_stacked in cls_by_bug.get(bk, []):
      st = c_art.get("state") or {}
      c_status = str(st.get("status") or "NEW").upper()
      is_wip = bool(st.get("is_wip") or st.get("is_draft") or st.get("wip_label"))
      default_wip = "Draft" if st.get("is_draft") else "WIP"
      wip_lbl = str(st.get("wip_label") or default_wip) if is_wip else ""
      est = st.get("entity_state") or {}
      conflict = bool(st.get("merge_conflict") or est.get("status") == "conflict")
      rolls = st.get("rolls") if isinstance(st.get("rolls"), Mapping) else {}
      fl_roll = rolls.get("Flutter") if isinstance(rolls, Mapping) else None
      rolled_fl = (
          (bool(fl_roll) if isinstance(fl_roll, bool) else bool((fl_roll or {}).get("rolled")))
          if ckey.startswith("gerrit:")
          else (c_status in ("MERGED", "SUBMITTED"))
      )
      in_draft = (not draft_mentioned_keys and not has_wb_reply_draft) or (
          ckey in draft_mentioned_keys
      )
      upd = str(st.get("status_changed_et") or st.get("last_msg_et") or "")
      age = str(st.get("status_changed_age") or st.get("last_msg_age") or "")
      upd_label = f"{upd} ({age})" if (upd and age and age not in upd) else upd
      cl_items.append({
          "artifact_key": ckey,
          "label": format_short_cl_id(ckey, st),
          "url": str(c_art.get("url") or st.get("url") or ""),
          "subject": str(st.get("subject") or c_art.get("title") or ckey),
          "status": c_status,
          "is_wip": is_wip,
          "wip_label": wip_lbl,
          "merge_conflict": conflict,
          "cr_summary": str(st.get("cr_summary") or ""),
          "reviewer_status": str(st.get("reviewer_status") or ""),
          "assigned_reviewers_count": int(st.get("assigned_reviewers_count") or 0),
          "rolled_flutter": rolled_fl,
          "in_draft": in_draft,
          "is_stacked": is_stacked,
          "updated_et": upd_label,
          "rolls": dict(rolls) if isinstance(rolls, Mapping) else {},
      })

    draft_outdated = bool(
        has_draft
        and (draft_mentioned_keys or has_wb_reply_draft)
        and cl_items
        and any(not c["in_draft"] for c in cl_items)
    )
    if draft_outdated:
      n_outdated += 1

    if not cl_items:
      stage = "no_cl"
      stage_tone = "muted"
      stage_label = "🔍 No CL linked yet"
    elif all(c["rolled_flutter"] for c in cl_items):
      stage = "rolled"
      stage_tone = "ready"
      stage_label = "🚀 Rolled into Flutter"
      n_rolled += 1
    elif all(c["status"] == "MERGED" for c in cl_items):
      stage = "merged_waiting_roll"
      stage_tone = "ok"
      stage_label = "⏳ Merged · waiting Flutter roll"
      n_merged += 1
    else:
      has_wip = any(c["is_wip"] for c in cl_items)
      has_conflict = any(c["merge_conflict"] for c in cl_items)
      if has_conflict:
        n_conflict += 1
      if has_wip:
        stage = "wip"
        stage_tone = "urgent" if has_conflict else "warn"
        parts_s = ["📝 WIP"]
        if has_conflict:
          parts_s.append("⚠️ Conflict")
        if all(c["assigned_reviewers_count"] == 0 for c in cl_items):
          parts_s.append("No reviewers")
        stage_label = " · ".join(parts_s)
        n_wip += 1
      elif has_conflict:
        stage = "conflict"
        stage_tone = "urgent"
        cr_s = cl_items[0].get("cr_summary") or ""
        cr_part = f" · CR {cr_s}" if cr_s and cr_s != "0" else ""
        stage_label = f"⚠️ Conflict (needs rebase){cr_part}"
      else:
        stage = "review"
        stage_tone = "wait"
        rev_s = cl_items[0].get("reviewer_status") or ""
        stage_label = f"👀 In review · {rev_s}" if rev_s else "👀 In review"
        n_review += 1

    checklist.append({
        "bug_key": bk,
        "short_id": short_id,
        "title": raw_b_title,
        "url": b_url,
        "bug_status": b_status,
        "stage": stage,
        "stage_tone": stage_tone,
        "stage_label": stage_label,
        "draft_outdated": draft_outdated,
        "cls": cl_items,
    })

  total = len(checklist)
  prog_parts = [f"{n_rolled}/{total} rolled"]
  if n_merged:
    prog_parts.append(f"{n_merged} merged")
  if n_review:
    prog_parts.append(f"{n_review} in review")
  if n_wip:
    prog_parts.append(f"{n_wip} WIP")
  if n_conflict:
    prog_parts.append(f"{n_conflict} conflict" if n_conflict == 1 else f"{n_conflict} conflicts")
  if n_outdated:
    prog_parts.append(f"{n_outdated} not in draft")
  return checklist, " · ".join(prog_parts)


def _eval_single_predicate(
    pred: str,
    artifacts_by_key: Mapping[str, Mapping[str, Any]],
    work_row: Mapping[str, Any],
    now: float,
) -> str:
  """Evaluates one obligation predicate into 3-valued logic ('true'|'false'|'unknown')."""
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
    # Piper CLs report SUBMITTED once landed while Gerrit and GitHub PRs report MERGED
    return "true" if status in ("MERGED", "SUBMITTED") else "false"

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

  m_corr = re.match(
      r"^correspondence_turn\(\s*([^,)]+?)(?:\s*,\s*(\d+))?\s*\)$",
      expr,
      re.IGNORECASE,
  )
  if m_corr:
    akey = canonicalize_artifact_key(m_corr.group(1))
    silence_days = int(m_corr.group(2) or STALLED_OBLIGATION_DAYS)
    art = artifacts_by_key.get(akey)
    if not art:
      return "unknown"
    st = art.get("state") or {}
    if bool(st.get("resolved")) and bool(st.get("resolved_by_me")):
      return "false"
    if is_github_thread_resolved(st, artifacts_by_key):
      return "false"
    last_ts = float(st.get("last_message_ts") or st.get("origin_ts") or 0.0)
    gh_target_st = _get_github_target_state(st, artifacts_by_key)
    eff_ext_ts, gh_self_ts = _resolve_github_thread_turns(st, gh_target_st)
    override_ts = max(
        float(st.get("manual_wait_ts") or 0.0),
        float(st.get("mcp_reply_ts") or 0.0),
        float(st.get("ack_message_ts") or 0.0),
        gh_self_ts,
    )
    gh_human_ts = max(
        float(st.get("gh_human_reply_ts") or 0.0),
        float(gh_target_st.get("last_human_comment_at") or 0.0),
    )
    if "last_from_me" in st:
      from_me = bool(st.get("last_from_me"))
    else:
      from_me = not bool(st.get("promoted_inbound"))
    # Verbal wait or MCP/GitHub reply newer than inbound turns flips obligation to waiting
    if not from_me and override_ts > 0 and override_ts >= eff_ext_ts:
      from_me = True
    # Newer external GitHub comment after last Gmail pull and self reply re-triggers reply
    if gh_human_ts > max(last_ts, override_ts):
      from_me = False
    if not from_me:
      return "true"
    anchor_ts = max(
        eff_ext_ts,
        last_ts if bool(st.get("last_from_me")) else 0.0,
        override_ts,
    )
    if anchor_ts <= 0:
      anchor_ts = float(work_row.get("created_at") or 0.0)
    if anchor_ts <= 0:
      return "false"
    return "true" if (now - anchor_ts) >= silence_days * 86400 else "false"

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
    trigger_eval: str = "true",
    ready_at: float = 0.0,
) -> bool:
  """Checks whether a post-ready reply, draft send, or GDoc resolve satisfies the obligation."""
  created_at = float(obligation.get("created_at") or 0.0)
  target_key = str(obligation.get("target_artifact_key") or "").strip()
  matcher = str(obligation.get("done_matcher") or "").strip()
  ob_kind = str(obligation.get("kind") or "reply").strip()

  # End-to-end correspondence umbrellas close on explicit resolve or resolved GitHub thread
  if ob_kind == "correspondence":
    if not target_key:
      return False
    art = artifacts_by_key.get(canonicalize_artifact_key(target_key))
    if not art:
      return False
    st = art.get("state") or {}
    return (
        (bool(st.get("resolved")) and bool(st.get("resolved_by_me")))
        or is_github_thread_resolved(st, artifacts_by_key)
    )

  m_reply = re.match(
      r"^(?:reply_sent|draft_sent)\(\s*([^)]+?)\s*\)$", matcher, re.IGNORECASE
  )
  check_key = canonicalize_artifact_key(m_reply.group(1)) if m_reply else target_key
  if not check_key:
    return False
  art = artifacts_by_key.get(check_key)
  if art:
    st = art.get("state") or {}
    kind = str(art.get("kind") or "")
    if kind == "gmail_draft" and str(st.get("status") or "").upper() == "SENT":
      return True
    # Require the obligation trigger to have fired before counting a reply as satisfying the promise
    if trigger_eval == "true" and kind in ("gmail_thread", "gchat_thread", "gdoc_thread"):
      last_from_me = bool(st.get("last_from_me"))
      last_ts = float(st.get("last_message_ts") or 0.0)
      if last_from_me and last_ts > max(created_at, ready_at):
        return True
  # Also allow direct post-ready thread replies when done_matcher targeted a materialized draft
  canon_target = canonicalize_artifact_key(target_key) if target_key else ""
  if trigger_eval == "true" and canon_target and canon_target != check_key:
    t_art = artifacts_by_key.get(canon_target)
    if t_art and str(t_art.get("kind") or "") in ("gmail_thread", "gchat_thread", "gdoc_thread"):
      t_st = t_art.get("state") or {}
      if bool(t_st.get("last_from_me")) and float(t_st.get("last_message_ts") or 0.0) > max(
          created_at, ready_at
      ):
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
          link_reason TEXT DEFAULT '',
          link_detail TEXT DEFAULT '',
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
          satisfied_at REAL DEFAULT 0,
          meta_json TEXT DEFAULT '{}'
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
      wl_cols = {
          str(r[1])
          for r in conn.execute("PRAGMA table_info(work_links)").fetchall()
      }
      if "link_reason" not in wl_cols:
        conn.execute("ALTER TABLE work_links ADD COLUMN link_reason TEXT DEFAULT ''")
      if "link_detail" not in wl_cols:
        conn.execute("ALTER TABLE work_links ADD COLUMN link_detail TEXT DEFAULT ''")
      ob_cols = {
          str(r[1])
          for r in conn.execute("PRAGMA table_info(obligations)").fetchall()
      }
      if "meta_json" not in ob_cols:
        conn.execute("ALTER TABLE obligations ADD COLUMN meta_json TEXT DEFAULT '{}'")

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
    """Creates or updates a Work in place while preserving status, snooze_until, and created_at."""
    cur_now = time.time() if now is None else now
    if not work_id:
      digest = hashlib.sha1(f"{title}:{project_id}:{cur_now}".encode("utf-8")).hexdigest()[:10]
      work_id = f"work-{digest}"
    clean_title = title.strip()
    clean_proj = project_id.strip()
    clean_roadmap = roadmap_item_key.strip()
    with self._lock, self._connect() as conn:
      existing_w = conn.execute(
          "SELECT title, status, project_id, roadmap_item_key, created_at, meta_json "
          "FROM works WHERE work_id = ?",
          (work_id,),
      ).fetchone()
      if existing_w is None:
        conn.execute(
            "INSERT INTO works "
            "(work_id, title, status, project_id, roadmap_item_key, "
            "created_at, updated_at, meta_json) "
            "VALUES (?, ?, 'active', ?, ?, ?, ?, ?)",
            (
                work_id,
                clean_title,
                clean_proj,
                clean_roadmap,
                cur_now,
                cur_now,
                json.dumps(dict(meta) if meta else {}),
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

      prev_meta: dict[str, Any] = {}
      if existing_w["meta_json"]:
        try:
          prev_meta = json.loads(existing_w["meta_json"])
        except Exception:
          prev_meta = {}
      merged_meta = dict(prev_meta)
      if meta:
        merged_meta.update(dict(meta))
      # Skip no-op sync passes; UPDATE in place so status, snooze_until, and work_minted stay intact
      if (
          str(existing_w["title"] or "") == clean_title
          and str(existing_w["project_id"] or "") == clean_proj
          and str(existing_w["roadmap_item_key"] or "") == clean_roadmap
          and merged_meta == prev_meta
      ):
        return work_id
      conn.execute(
          "UPDATE works SET title = ?, project_id = ?, roadmap_item_key = ?, "
          "updated_at = ?, meta_json = ? WHERE work_id = ?",
          (clean_title, clean_proj, clean_roadmap, cur_now, json.dumps(merged_meta), work_id),
      )
    return work_id

  def find_work_by_artifact(
      self, artifact_key: str, roles: tuple[str, ...] = ("primary", "channel")
  ) -> str | None:
    wids = self.find_all_works_by_artifact(artifact_key, roles=roles)
    return wids[0] if wids else None

  def find_all_works_by_artifact(
      self, artifact_key: str, roles: tuple[str, ...] = ("primary", "channel")
  ) -> list[str]:
    """Returns all active work_ids linked to artifact_key with any of the given roles."""
    akey = canonicalize_artifact_key(artifact_key)
    placeholders = ",".join("?" for _ in roles)
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          f"SELECT wl.work_id FROM work_links wl "
          f"JOIN works w ON w.work_id = wl.work_id "
          f"WHERE wl.artifact_key = ? AND wl.role IN ({placeholders}) "
          f"AND w.status NOT IN ('done', 'archived') "
          f"ORDER BY w.updated_at DESC",
          (akey, *roles),
      ).fetchall()
      return [str(r["work_id"]) for r in rows]

  def get_artifact(self, artifact_key: str) -> dict[str, Any] | None:
    akey = canonicalize_artifact_key(artifact_key)
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT * FROM artifacts WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      if not row:
        return None
      d = dict(row)
      try:
        d["state"] = json.loads(d.get("state_json") or "{}")
      except Exception:
        d["state"] = {}
      return d

  def dismiss_tray_thread(self, artifact_key: str, now: float | None = None) -> str:
    """Marks a Gmail/Chat thread artifact as dismissed from the Unanswered Human Threads tray."""
    cur_now = time.time() if now is None else now
    raw = str(artifact_key or "").strip()
    if not raw:
      raise ValueError("Empty tray thread artifact_key")
    if raw.lower().startswith("thread:"):
      raw = "gmail_thread:" + raw.split(":", 1)[1].strip()
    elif ":" not in raw and re.fullmatch(r"[0-9a-f]{15,16}", raw, flags=re.IGNORECASE):
      raw = f"gmail_thread:{raw.lower()}"
    akey = canonicalize_artifact_key(raw)
    akind = artifact_kind_from_key(akey)
    if akind not in ("gmail_thread", "gchat_thread", "gdoc_thread"):
      raise ValueError(f"Unsupported tray thread artifact_key: {artifact_key}")
    self.upsert_artifact(
        akey,
        kind=akind,
        state={"tray_dismissed": True, "tray_dismissed_at": cur_now},
        now=cur_now,
    )
    with self._lock, self._connect() as conn:
      self._log_event(
          conn,
          "tray_dismissed",
          artifact_key=akey,
          payload={"dismissed_at": cur_now},
          now=cur_now,
      )
    return akey

  def list_dismissed_tray_keys(self) -> set[str]:
    """Returns canonical artifact_keys dismissed from the Unanswered Human Threads tray."""
    dismissed: set[str] = set()
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT artifact_key, state_json FROM artifacts "
          "WHERE kind IN ('gmail_thread', 'gchat_thread', 'gdoc_thread') "
          "AND state_json LIKE '%tray_dismissed%'"
      ).fetchall()
    for r in rows:
      try:
        st = json.loads(r["state_json"] or "{}")
      except Exception:
        st = {}
      if isinstance(st, Mapping) and bool(st.get("tray_dismissed")):
        dismissed.add(canonicalize_artifact_key(str(r["artifact_key"])))
    return dismissed

  def list_completed_thread_timestamps(self) -> dict[str, float]:
    """Returns max completion timestamp per thread linked to a completed Work."""
    completed: dict[str, float] = {}
    with self._lock, self._connect() as conn:
      rows = conn.execute(
          "SELECT wl.artifact_key, MAX(w.updated_at) AS done_ts "
          "FROM work_links wl JOIN works w ON w.work_id = wl.work_id "
          "WHERE w.status = 'done' AND wl.role IN ('primary', 'channel') "
          "GROUP BY wl.artifact_key"
      ).fetchall()
    for r in rows:
      akey = canonicalize_artifact_key(str(r["artifact_key"] or ""))
      if akey.startswith(("gmail_thread:", "gchat_thread:", "gdoc_thread:", "gdoc:")):
        completed[akey] = float(r["done_ts"] or 0.0)
    return completed

  def rekey_gdoc_thread_artifact(
      self, old_key: str, new_key: str, now: float | None = None
  ) -> dict[str, Any]:
    """Migrates a Drive-ID gdoc_thread row to its canonical ?disco=doco.<id> key."""
    cur_now = time.time() if now is None else now
    if not old_key or not new_key or old_key == new_key:
      return {}
    with self._lock, self._connect() as conn:
      old_row = conn.execute(
          "SELECT * FROM artifacts WHERE artifact_key = ?", (old_key,)
      ).fetchone()
      if not old_row:
        return {}
      try:
        old_st = json.loads(old_row["state_json"] or "{}")
      except Exception:
        old_st = {}
      new_row = conn.execute(
          "SELECT * FROM artifacts WHERE artifact_key = ?", (new_key,)
      ).fetchone()
      if not new_row:
        conn.execute(
            "INSERT INTO artifacts (artifact_key, kind, title, url, state_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                new_key,
                str(old_row["kind"] or "gdoc_thread"),
                str(old_row["title"] or new_key),
                str(old_row["url"] or ""),
                json.dumps(old_st),
                cur_now,
            ),
        )
      conn.execute(
          "INSERT OR IGNORE INTO work_links "
          "(work_id, artifact_key, role, source, confidence, created_at, link_reason, link_detail) "
          "SELECT work_id, ?, role, source, confidence, created_at, link_reason, link_detail "
          "FROM work_links WHERE artifact_key = ?",
          (new_key, old_key),
      )
      conn.execute("DELETE FROM work_links WHERE artifact_key = ?", (old_key,))
      conn.execute(
          "UPDATE obligations SET target_artifact_key = ? WHERE target_artifact_key = ?",
          (new_key, old_key),
      )
      conn.execute("DELETE FROM artifacts WHERE artifact_key = ?", (old_key,))
      return old_st

  def upsert_artifact(
      self,
      artifact_key: str,
      kind: str = "",
      title: str = "",
      url: str = "",
      state: Mapping[str, Any] | None = None,
      now: float | None = None,
  ) -> str:
    """Upserts an artifact row, merging state_json monotonically and returning its canonical key."""
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
        prev_last_ts = float(merged_state.get("last_message_ts") or 0.0)
        prev_gh_self_ts = float(merged_state.get("gh_self_reply_ts") or 0.0)
        prev_gh_human_ts = float(merged_state.get("gh_human_reply_ts") or 0.0)
        merged_state.update(dict(state))
        new_gh_self_ts = float(merged_state.get("gh_self_reply_ts") or 0.0)
        if prev_gh_self_ts > 0 and new_gh_self_ts > 0:
          merged_state["gh_self_reply_ts"] = max(prev_gh_self_ts, new_gh_self_ts)
        elif prev_gh_self_ts > 0:
          merged_state["gh_self_reply_ts"] = prev_gh_self_ts
        new_gh_human_ts = float(merged_state.get("gh_human_reply_ts") or 0.0)
        if prev_gh_human_ts > 0 and new_gh_human_ts > 0:
          merged_state["gh_human_reply_ts"] = max(prev_gh_human_ts, new_gh_human_ts)
        elif prev_gh_human_ts > 0:
          merged_state["gh_human_reply_ts"] = prev_gh_human_ts
        if prev_ots > 0 and new_ots > 0:
          merged_state["origin_ts"] = min(prev_ots, new_ots)
        elif prev_ots > 0:
          merged_state["origin_ts"] = prev_ots
        # Keep monotonic message timestamps and user/MCP/GitHub reply overrides across syncs
        mcp_ts = float(merged_state.get("mcp_reply_ts") or 0.0)
        resolved_ts = float(merged_state.get("resolved_at") or 0.0)
        gh_self_ts = float(merged_state.get("gh_self_reply_ts") or 0.0)
        gh_human_ts = float(merged_state.get("gh_human_reply_ts") or 0.0)
        cur_msg_ts = float(merged_state.get("last_message_ts") or 0.0)
        if prev_last_ts > cur_msg_ts and max(mcp_ts, resolved_ts, gh_self_ts) >= prev_last_ts:
          merged_state["last_message_ts"] = prev_last_ts
          cur_msg_ts = prev_last_ts
        # Account for 30s Gmail delivery lag relative to gh_human_ts before forcing outbound state
        if gh_human_ts > 0 and cur_msg_ts <= gh_human_ts + 30.0:
          eff_ext_ts = gh_human_ts
        else:
          eff_ext_ts = max(cur_msg_ts, gh_human_ts)
        eff_self_ts = max(mcp_ts, gh_self_ts)
        if gh_human_ts > max(eff_self_ts, cur_msg_ts) and not bool(
            merged_state.get("gh_thread_closed") or merged_state.get("gh_locked")
        ):
          merged_state["last_from_me"] = False
          trashed_unjoined = bool(merged_state.get("in_trash_or_spam")) and not bool(
              merged_state.get("sent_by_me")
          )
          if (
              "gh_human_reply_ts" not in state
              and not trashed_unjoined
              and (cur_now - gh_human_ts) <= 14 * 86400
          ):
            merged_state["unanswered_human"] = True
        elif eff_self_ts > 0 and eff_ext_ts <= eff_self_ts:
          merged_state["last_from_me"] = True
        ack_floor = max(
            float(merged_state.get("ack_message_ts") or 0.0),
            float(merged_state.get("manual_wait_ts") or 0.0),
            mcp_ts,
            resolved_ts,
            gh_self_ts,
        )
        if ack_floor > 0 and eff_ext_ts <= ack_floor:
          merged_state["unanswered_human"] = False
        # Outbound replies satisfy inbound unanswered_human even on partial state updates
        if bool(merged_state.get("last_from_me")):
          merged_state["unanswered_human"] = False
        # Trashed unjoined threads never remain flagged as unanswered inbound human mail
        if bool(merged_state.get("in_trash_or_spam")) and not bool(
            merged_state.get("sent_by_me")
        ):
          merged_state["unanswered_human"] = False
        # Clear sticky MCP resolve flags when a newer comment reopens the #disco thread
        if resolved_ts > 0 and "resolved" not in state and cur_msg_ts > resolved_ts:
          merged_state["resolved"] = False
          merged_state["resolved_by_me"] = False
          merged_state["resolved_externally"] = False
      # Pin initial timestamp on Gmail drafts so 90s re-syncs never advance draft_updated_ts
      if (
          akind == "gmail_draft"
          and str(merged_state.get("status") or "DRAFT").upper() == "DRAFT"
          and not merged_state.get("draft_updated_ts")
      ):
        merged_state["draft_updated_ts"] = cur_now
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
          "(artifact_key, kind, title, url, chat_chain_id, "
          "first_seen_at, last_seen_at, state_json) "
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
      link_reason: str = "",
      link_detail: str = "",
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
          "SELECT title, state_json FROM artifacts WHERE artifact_key = ?",
          (akey,),
      ).fetchone()
      art_title = str(art_row["title"] or "") if art_row else ""
      art_state: dict[str, Any] = {}
      if art_row and art_row["state_json"]:
        try:
          art_state = json.loads(art_row["state_json"])
        except Exception:
          art_state = {}
      if source != "manual" and is_config_artifact(akey, title=art_title, state=art_state):
        return None

      existing_link = conn.execute(
          "SELECT role, source, confidence, link_reason, link_detail, created_at "
          "FROM work_links WHERE work_id = ? AND artifact_key = ?",
          (work_id, akey),
      ).fetchone()
      prev_role = str(existing_link["role"] or "") if existing_link else ""
      prev_source = str(existing_link["source"] or "") if existing_link else ""
      prev_conf = float(existing_link["confidence"] or 0.0) if existing_link else 0.0
      prev_reason = str(existing_link["link_reason"] or "") if existing_link else ""
      prev_detail = str(existing_link["link_detail"] or "") if existing_link else ""
      created_ts = float(existing_link["created_at"]) if existing_link else cur_now

      # Never let background auto sync passes downgrade or overwrite a manually pinned link
      if existing_link is not None and prev_source == "manual" and source != "manual":
        return prev_role or role

      req_reason = link_reason.strip()
      req_detail = link_detail.strip()
      final_reason = req_reason or prev_reason or ("manual" if source == "manual" else "")
      final_detail = req_detail or prev_detail

      effective_role = role
      if (
          is_hub_artifact(akey, art_state)
          and source != "manual"
          and final_reason != "companion_wip"
      ):
        effective_role = "context_only"
      # Never promote a mid-chat mention to primary without an explicit structural link_reason
      if (
          existing_link
          and prev_role == "context_only"
          and prev_reason == "chat_mention"
          and source != "manual"
          and req_reason in ("", "chat_mention")
      ):
        effective_role = "context_only"

      # Keep established auto link_reason/detail across competing sync passes unless companion_wip
      if (
          existing_link is not None
          and prev_role == effective_role
          and source != "manual"
          and prev_reason
          and req_reason != "companion_wip"
      ):
        final_reason = prev_reason
        final_detail = prev_detail
      final_conf = max(prev_conf, confidence) if existing_link is not None else confidence

      # Skip redundant SQLite writes on unchanged links so periodic syncs do not clobber updated_at
      if (
          existing_link is not None
          and prev_role == effective_role
          and prev_source == source
          and prev_conf == final_conf
          and prev_reason == final_reason
          and prev_detail == final_detail
      ):
        return effective_role

      conn.execute(
          "INSERT OR REPLACE INTO work_links "
          "(work_id, artifact_key, role, source, confidence, created_at, link_reason, link_detail) "
          "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (
              work_id,
              akey,
              effective_role,
              source,
              final_conf,
              created_ts,
              final_reason,
              final_detail,
          ),
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
          payload={
              "role": effective_role,
              "source": source,
              "link_reason": final_reason,
          },
          now=cur_now,
      )
      return effective_role

  def unlink_artifact(
      self, work_id: str, artifact_key: str, now: float | None = None
  ) -> None:
    """Detaches artifact_key from work_id, records a tombstone, and refreshes obligations."""
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    with self._lock, self._connect() as conn:
      conn.execute(
          "DELETE FROM work_links WHERE work_id = ? AND artifact_key = ?",
          (work_id, akey),
      )
      # Clear stale reply_draft thread bindings when detaching the bound email thread
      w_row = conn.execute(
          "SELECT meta_json FROM works WHERE work_id = ?", (work_id,)
      ).fetchone()
      if w_row:
        try:
          w_meta = json.loads(w_row["meta_json"] or "{}")
        except Exception:
          w_meta = {}
        rd = w_meta.get("reply_draft")
        if isinstance(rd, Mapping) and str(rd.get("thread_key") or "").strip() == akey:
          rd_copy = dict(rd)
          rd_copy["thread_key"] = ""
          w_meta["reply_draft"] = rd_copy
          conn.execute(
              "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
              (json.dumps(w_meta), cur_now, work_id),
          )
      conn.execute(
          "UPDATE obligations SET target_artifact_key = '' "
          "WHERE work_id = ? AND target_artifact_key = ? "
          "AND obligation_id LIKE 'ob-wbdraft-%'",
          (work_id, akey),
      )
      # Close correspondence umbrellas when their underlying channel is detached from the Work
      conn.execute(
          "UPDATE obligations SET status = 'done', satisfied_at = ? "
          "WHERE work_id = ? AND target_artifact_key = ? "
          "AND kind = 'correspondence' AND status IN ('open', 'ready')",
          (cur_now, work_id, akey),
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
    self.reconcile_correspondence_obligations(work_id, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)

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
      # Preserve existing status and ready_at meta_json when re-adding an active obligation
      if existing:
        conn.execute(
            "UPDATE obligations SET description = ?, trigger_expr = ?, done_matcher = ? "
            "WHERE obligation_id = ?",
            (
                description.strip(),
                trigger_expr.strip() or "always",
                done_matcher.strip(),
                obligation_id,
            ),
        )
      else:
        conn.execute(
            "INSERT INTO obligations "
            "(obligation_id, work_id, target_artifact_key, kind, description, "
            "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at, meta_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'open', 'unknown', ?, 0, '{}')",
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
    if kind != "correspondence":
      self.reconcile_correspondence_obligations(work_id, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)
    return obligation_id

  def replace_draft_obligation(
      self,
      work_id: str,
      draft_key: str,
      description: str,
      trigger_expr: str,
      done_matcher: str = "",
      now: float | None = None,
  ) -> str:
    """Replaces stale per-CL auto obligations on a draft with one consolidated obligation."""
    cur_now = time.time() if now is None else now
    tkey = canonicalize_artifact_key(draft_key)
    did_short = hashlib.sha1(f"{work_id}:{tkey}".encode("utf-8")).hexdigest()[:10]
    ob_id = f"ob-draft-{did_short}"
    with self._lock, self._connect() as conn:
      # Remove legacy duplicate auto obligations targeting the same draft in this Work
      conn.execute(
          "DELETE FROM obligations "
          "WHERE work_id = ? AND target_artifact_key = ? "
          "AND obligation_id != ? AND status != 'done'",
          (work_id, tkey, ob_id),
      )
      existing = conn.execute(
          "SELECT status, created_at FROM obligations WHERE obligation_id = ?",
          (ob_id,),
      ).fetchone()
      if existing and existing["status"] == "done":
        return ob_id
      final_desc = description.strip()
      final_trig = trigger_expr.strip() or "always"
      final_done = done_matcher.strip() or f"reply_sent({tkey})"
      # Update in place so 90s Gmail syncs preserve ready_at and never emit duplicate ready events
      if existing:
        conn.execute(
            "UPDATE obligations SET description = ?, trigger_expr = ?, done_matcher = ? "
            "WHERE obligation_id = ?",
            (final_desc, final_trig, final_done, ob_id),
        )
      else:
        conn.execute(
            "INSERT INTO obligations "
            "(obligation_id, work_id, target_artifact_key, kind, description, "
            "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at, meta_json) "
            "VALUES (?, ?, ?, 'reply', ?, ?, ?, 'open', 'unknown', ?, 0, '{}')",
            (ob_id, work_id, tkey, final_desc, final_trig, final_done, cur_now),
        )
    self.reconcile_correspondence_obligations(work_id, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)
    return ob_id

  def save_reply_draft(
      self,
      work_id: str,
      body: str,
      *,
      fmt: str = "html",
      thread_key: str = "",
      to: str = "",
      cc: str = "",
      subject: str = "",
      trigger_expr: str = "",
      auto_gate: bool | None = None,
      mentioned_keys: Iterable[str] | None = None,
      now: float | None = None,
  ) -> dict[str, Any]:
    """Saves or updates a Work's live reply draft in works.meta_json without touching Gmail."""
    cur_now = time.time() if now is None else now
    clean_body = str(body or "").strip()
    if not clean_body:
      raise ValueError("Reply draft body cannot be empty")
    norm_fmt = "text" if str(fmt or "").strip().lower() == "text" else "html"

    with self._lock, self._connect() as conn:
      w_row = conn.execute(
          "SELECT title, meta_json FROM works WHERE work_id = ?", (work_id,)
      ).fetchone()
      if not w_row:
        raise ValueError(f"Unknown work_id: {work_id}")
      work_title = str(w_row["title"] or "")
      try:
        w_meta = json.loads(w_row["meta_json"] or "{}")
      except Exception:
        w_meta = {}
      prev_rd = w_meta.get("reply_draft") if isinstance(w_meta.get("reply_draft"), Mapping) else {}
      if float(prev_rd.get("sent_at") or 0.0) > 0.0:
        prev_rd = {}

      wl_rows = conn.execute(
          "SELECT artifact_key, role FROM work_links WHERE work_id = ? ORDER BY created_at ASC",
          (work_id,),
      ).fetchall()
      w_roles = {str(r["artifact_key"]): str(r["role"]) for r in wl_rows}
      art_rows: dict[str, dict[str, Any]] = {}
      for r in conn.execute("SELECT * FROM artifacts").fetchall():
        ad = dict(r)
        try:
          ad["state"] = json.loads(ad.get("state_json") or "{}")
        except Exception:
          ad["state"] = {}
        art_rows[str(ad["artifact_key"])] = ad

      eff_to = str(to.strip() or prev_rd.get("to") or "")
      eff_cc = str(cc.strip() or prev_rd.get("cc") or "")
      eff_subj = str(subject.strip() or prev_rd.get("subject") or "")
      resolved_thread = canonicalize_artifact_key(thread_key) if thread_key.strip() else ""
      if not resolved_thread:
        prev_tkey = str(prev_rd.get("thread_key") or "").strip()
        if prev_tkey in w_roles:
          resolved_thread = prev_tkey
      is_standalone_compose = bool(
          to.strip() and subject.strip() and not thread_key.strip() and not resolved_thread
      )
      if not resolved_thread and not is_standalone_compose:
        # Prefer primary human correspondence over channel; skip automated bot notification threads
        for pref_role in ("primary", "channel"):
          for ak, r_val in w_roles.items():
            if r_val != pref_role or not ak.startswith("gmail_thread:"):
              continue
            ak_st = (art_rows.get(ak) or {}).get("state")
            if is_active_correspondence_artifact(
                "gmail_thread", ak_st if isinstance(ak_st, Mapping) else {}
            ):
              resolved_thread = ak
              break
          if resolved_thread:
            break

      extracted_keys = (
          [canonicalize_artifact_key(str(k)) for k in mentioned_keys]
          if mentioned_keys is not None
          else _extract_draft_mentioned_keys(f"{eff_subj}\n{clean_body}")
      )
      use_auto_gate = (
          bool(prev_rd.get("auto_gate", True)) if auto_gate is None else bool(auto_gate)
      )
      mat_key = str(prev_rd.get("materialized_draft_key") or "")
      mat_at = float(prev_rd.get("materialized_at") or 0.0)
      # Preserve updated_at across no-op saves so materialized drafts are not flagged outdated
      same_body = str(prev_rd.get("body") or "").strip() == clean_body
      unchanged_content = (
          bool(prev_rd)
          and same_body
          and prev_rd.get("format") == norm_fmt
          and prev_rd.get("to", "") == eff_to
          and prev_rd.get("cc", "") == eff_cc
          and prev_rd.get("subject", "") == eff_subj
          and prev_rd.get("thread_key", "") == resolved_thread
      )
      effective_upd = (
          float(prev_rd.get("updated_at") or cur_now) if unchanged_content else cur_now
      )

      rd_payload: dict[str, Any] = {
          "body": clean_body,
          "format": norm_fmt,
          "thread_key": resolved_thread,
          "to": eff_to,
          "cc": eff_cc,
          "subject": eff_subj,
          "mentioned_keys": extracted_keys,
          "auto_gate": use_auto_gate,
          "updated_at": effective_upd,
          "materialized_draft_key": mat_key,
          "materialized_at": mat_at,
          "sent_at": 0.0,
      }
      w_meta["reply_draft"] = rd_payload
      conn.execute(
          "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
          (json.dumps(w_meta), cur_now, work_id),
      )

      explicit_trig = str(trigger_expr or "").strip()
      auto_trig, auto_desc = (
          _build_auto_gate_spec(work_title, w_roles, art_rows, rd_payload)
          if (use_auto_gate and not explicit_trig)
          else ("", "")
      )
      final_trig = explicit_trig or auto_trig
      ob_id = _wb_draft_obligation_id(work_id)
      existing_ob = conn.execute(
          "SELECT status, meta_json FROM obligations WHERE obligation_id = ?", (ob_id,)
      ).fetchone()

      if final_trig:
        t_art = art_rows.get(resolved_thread) or {}
        t_st = t_art.get("state") if isinstance(t_art.get("state"), Mapping) else {}
        subj_label = (
            rd_payload["subject"]
            or str(t_st.get("root_subject") or t_art.get("title") or work_title).strip()
            or work_id
        )
        final_desc = auto_desc or f"Materialize & send reply in '{subj_label}'"
        done_ref = mat_key or resolved_thread
        final_done = f"reply_sent({done_ref})" if done_ref else ""
        ob_meta: dict[str, Any] = {}
        if existing_ob and existing_ob["meta_json"]:
          try:
            ob_meta = json.loads(existing_ob["meta_json"])
          except Exception:
            ob_meta = {}
        ob_meta["wb_reply_draft"] = True
        ob_meta["auto_gate"] = use_auto_gate and not explicit_trig
        ob_meta["draft_updated_ts"] = cur_now
        if existing_ob:
          next_st = "open" if str(existing_ob["status"]) == "done" else str(existing_ob["status"])
          conn.execute(
              "UPDATE obligations SET target_artifact_key = ?, description = ?, "
              "trigger_expr = ?, done_matcher = ?, status = ?, satisfied_at = 0, meta_json = ? "
              "WHERE obligation_id = ?",
              (
                  resolved_thread,
                  final_desc,
                  final_trig,
                  final_done,
                  next_st,
                  json.dumps(ob_meta),
                  ob_id,
              ),
          )
        else:
          conn.execute(
              "INSERT INTO obligations "
              "(obligation_id, work_id, target_artifact_key, kind, description, "
              "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at, meta_json) "
              "VALUES (?, ?, ?, 'reply', ?, ?, ?, 'open', 'unknown', ?, 0, ?)",
              (
                  ob_id,
                  work_id,
                  resolved_thread,
                  final_desc,
                  final_trig,
                  final_done,
                  cur_now,
                  json.dumps(ob_meta),
              ),
          )
      elif existing_ob:
        try:
          ob_meta = json.loads(existing_ob["meta_json"] or "{}")
        except Exception:
          ob_meta = {}
        # Retire stale parked obligations when auto_gate is disabled or has zero primary targets
        if (not use_auto_gate) or bool(ob_meta.get("auto_gate", True)):
          conn.execute(
              "UPDATE obligations SET status = 'done', satisfied_at = ? WHERE obligation_id = ?",
              (cur_now, ob_id),
          )
        else:
          ob_meta["draft_updated_ts"] = cur_now
          conn.execute(
              "UPDATE obligations SET meta_json = ? WHERE obligation_id = ?",
              (json.dumps(ob_meta), ob_id),
          )

      self._log_event(
          conn,
          "reply_draft_saved",
          work_id=work_id,
          artifact_key=resolved_thread,
          payload={"format": norm_fmt, "auto_gate": use_auto_gate},
          now=cur_now,
      )

    self.reconcile_correspondence_obligations(work_id, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)
    return dict(rd_payload)

  def get_reply_draft(self, work_id: str) -> dict[str, Any] | None:
    """Returns the active unsent reply_draft dict for work_id, or None when absent/sent."""
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT meta_json FROM works WHERE work_id = ?", (work_id,)
      ).fetchone()
      if not row:
        return None
      return _active_reply_draft_from_meta(row["meta_json"])

  def clear_reply_draft(self, work_id: str, now: float | None = None) -> None:
    """Clears the live reply draft on work_id and restores normal correspondence tracking."""
    cur_now = time.time() if now is None else now
    ob_id = _wb_draft_obligation_id(work_id)
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT meta_json FROM works WHERE work_id = ?", (work_id,)
      ).fetchone()
      if row:
        try:
          w_meta = json.loads(row["meta_json"] or "{}")
        except Exception:
          w_meta = {}
        w_meta.pop("reply_draft", None)
        conn.execute(
            "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
            (json.dumps(w_meta), cur_now, work_id),
        )
      conn.execute(
          "UPDATE obligations SET status = 'done', satisfied_at = ? "
          "WHERE work_id = ? AND (obligation_id = ? OR obligation_id LIKE 'ob-wbdraft-%') "
          "AND status IN ('open', 'ready')",
          (cur_now, work_id, ob_id),
      )
      self._log_event(conn, "reply_draft_cleared", work_id=work_id, now=cur_now)
    self.reconcile_correspondence_obligations(work_id, now=cur_now)

  def list_open_gmail_draft_ids(self) -> tuple[set[str], set[str]]:
    """Returns (all_open_draft_ids, tracked_open_draft_ids) for active gmail_draft rows."""
    all_open: set[str] = set()
    tracked_open: set[str] = set()
    with self._lock, self._connect() as conn:
      linked_keys = {
          str(r["artifact_key"])
          for r in conn.execute(
              "SELECT DISTINCT artifact_key FROM work_links "
              "WHERE artifact_key LIKE 'gmail_draft:%'"
          ).fetchall()
      }
      rows = conn.execute(
          "SELECT artifact_key, state_json FROM artifacts WHERE kind = 'gmail_draft'"
      ).fetchall()
      for r in rows:
        akey = str(r["artifact_key"] or "")
        if not akey.startswith("gmail_draft:"):
          continue
        did = akey.split(":", 1)[1].strip()
        if not did:
          continue
        try:
          st = json.loads(r["state_json"] or "{}")
        except Exception:
          st = {}
        if str(st.get("status") or "DRAFT").upper() != "DRAFT":
          continue
        all_open.add(did)
        if akey in linked_keys or st.get("from_workbench_draft") or st.get("mentioned_keys"):
          tracked_open.add(did)
    return all_open, tracked_open

  def mark_materialized_draft_discarded(
      self, draft_key: str, now: float | None = None
  ) -> None:
    """Marks a discarded Gmail draft DISCARDED and detaches it from any live reply_draft."""
    cur_now = time.time() if now is None else now
    d_key = canonicalize_artifact_key(draft_key)
    touched_wids: list[str] = []
    with self._lock, self._connect() as conn:
      ar = conn.execute(
          "SELECT state_json FROM artifacts WHERE artifact_key = ?", (d_key,)
      ).fetchone()
      st: dict[str, Any] = {}
      if ar:
        try:
          st = json.loads(ar["state_json"] or "{}")
        except Exception:
          st = {}
      st["status"] = "DISCARDED"
      conn.execute(
          "UPDATE artifacts SET state_json = ?, updated_at = ? WHERE artifact_key = ?",
          (json.dumps(st), cur_now, d_key),
      )
      for w_row in conn.execute("SELECT work_id, meta_json FROM works").fetchall():
        wid = str(w_row["work_id"])
        try:
          w_meta = json.loads(w_row["meta_json"] or "{}")
        except Exception:
          w_meta = {}
        rd = w_meta.get("reply_draft")
        if not isinstance(rd, Mapping) or rd.get("materialized_draft_key") != d_key:
          continue
        rd_copy = dict(rd)
        rd_copy["materialized_draft_key"] = ""
        rd_copy["materialized_at"] = 0.0
        w_meta["reply_draft"] = rd_copy
        conn.execute(
            "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
            (json.dumps(w_meta), cur_now, wid),
        )
        t_ref = str(rd_copy.get("thread_key") or "").strip()
        done_m = f"reply_sent({t_ref})" if t_ref else ""
        conn.execute(
            "UPDATE obligations SET done_matcher = ? "
            "WHERE obligation_id = ? AND status IN ('open', 'ready')",
            (done_m, _wb_draft_obligation_id(wid)),
        )
        touched_wids.append(wid)
    for wid in touched_wids:
      self.reconcile_correspondence_obligations(wid, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)

  def record_materialized_reply_draft(
      self,
      work_id: str,
      draft_id: str,
      *,
      thread_id: str = "",
      subject: str = "",
      to_hdr: str = "",
      mentioned_keys: Iterable[str] | None = None,
      now: float | None = None,
  ) -> str:
    """Links a newly created Gmail draft back onto work_id's reply_draft and obligation."""
    cur_now = time.time() if now is None else now
    did = str(draft_id or "").strip()
    if did.startswith("gmail_draft:"):
      did = did.split(":", 1)[1]
    d_key = f"gmail_draft:{did}"
    rd = self.get_reply_draft(work_id) or {}
    prev_mat_key = str(rd.get("materialized_draft_key") or "").strip()
    if prev_mat_key.startswith("gmail_draft:") and prev_mat_key != d_key:
      # Mark superseded force-new Gmail draft DISCARDED so closing the old tab never triggers SENT
      self.upsert_artifact(
          prev_mat_key,
          kind="gmail_draft",
          state={"status": "DISCARDED", "superseded_by": d_key},
          now=cur_now,
      )
    mks = (
        [canonicalize_artifact_key(str(k)) for k in mentioned_keys]
        if mentioned_keys is not None
        else list(rd.get("mentioned_keys") or [])
    )
    bug_keys = [k for k in mks if k.startswith(("buganizer:", "github_issue:"))]
    eff_tid = str(thread_id or "").strip()
    if not eff_tid and str(rd.get("thread_key") or "").startswith("gmail_thread:"):
      eff_tid = str(rd["thread_key"]).split(":", 1)[1]
    eff_subj = str(subject or rd.get("subject") or f"Draft {did[:8]}").strip()
    self.upsert_artifact(
        d_key,
        kind="gmail_draft",
        title=f"Draft: {eff_subj}",
        url=f"https://mail.google.com/mail/u/0/#drafts/{did}",
        state={
            "status": "DRAFT",
            "thread_id": eff_tid,
            "to": str(to_hdr or rd.get("to") or ""),
            "mentioned_keys": mks,
            "draft_bug_keys": bug_keys,
            "draft_updated_ts": cur_now,
            "from_workbench_draft": True,
        },
        now=cur_now,
    )
    self.link_artifact(
        work_id, d_key, role="channel", source="manual", link_reason="wb_reply_draft", now=cur_now
    )
    ob_id = _wb_draft_obligation_id(work_id)
    t_key = f"gmail_thread:{eff_tid}" if eff_tid else str(rd.get("thread_key") or d_key)
    with self._lock, self._connect() as conn:
      w_row = conn.execute(
          "SELECT title, meta_json FROM works WHERE work_id = ?", (work_id,)
      ).fetchone()
      work_title = str(w_row["title"] or "") if w_row else work_id
      try:
        w_meta = json.loads((w_row["meta_json"] if w_row else "") or "{}")
      except Exception:
        w_meta = {}
      cur_rd = dict(w_meta.get("reply_draft") or {})
      cur_rd["materialized_draft_key"] = d_key
      cur_rd["materialized_at"] = cur_now
      if eff_tid and not cur_rd.get("thread_key"):
        cur_rd["thread_key"] = f"gmail_thread:{eff_tid}"
      w_meta["reply_draft"] = cur_rd
      conn.execute(
          "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
          (json.dumps(w_meta), cur_now, work_id),
      )
      ex_ob = conn.execute(
          "SELECT status, meta_json FROM obligations WHERE obligation_id = ?", (ob_id,)
      ).fetchone()
      if ex_ob and str(ex_ob["status"]) in ("open", "ready"):
        try:
          ob_meta = json.loads(ex_ob["meta_json"] or "{}")
        except Exception:
          ob_meta = {}
        ob_meta["wb_reply_draft"] = True
        ob_meta["draft_updated_ts"] = max(
            float(ob_meta.get("draft_updated_ts") or 0.0), cur_now
        )
        conn.execute(
            "UPDATE obligations SET done_matcher = ?, meta_json = ? WHERE obligation_id = ?",
            (f"reply_sent({d_key})", json.dumps(ob_meta), ob_id),
        )
      else:
        ob_meta = {
            "wb_reply_draft": True,
            "auto_gate": False,
            "draft_updated_ts": cur_now,
            "ready_at": cur_now,
        }
        conn.execute(
            "INSERT OR REPLACE INTO obligations "
            "(obligation_id, work_id, target_artifact_key, kind, description, "
            "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at, meta_json) "
            "VALUES (?, ?, ?, 'reply', ?, 'always', ?, 'ready', 'true', ?, 0, ?)",
            (
                ob_id,
                work_id,
                t_key,
                f"Send Gmail draft in '{eff_subj or work_title}'",
                f"reply_sent({d_key})",
                cur_now,
                json.dumps(ob_meta),
            ),
        )
      self._log_event(
          conn,
          "reply_draft_materialized",
          work_id=work_id,
          artifact_key=d_key,
          payload={"thread_id": eff_tid},
          now=cur_now,
      )
    self.reconcile_correspondence_obligations(work_id, now=cur_now)
    self.evaluate_all_obligations(now=cur_now)
    return d_key

  def reconcile_work_bug_cls(
      self, work_id: str, now: float | None = None
  ) -> list[str]:
    """Links any stored non-hub CL/PR fixing a primary/draft bug of work_id onto work_id."""
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      wl_rows = conn.execute(
          "SELECT artifact_key, role FROM work_links WHERE work_id = ?",
          (work_id,),
      ).fetchall()
      art_rows = {
          str(r["artifact_key"]): dict(r)
          for r in conn.execute("SELECT * FROM artifacts").fetchall()
      }

    bug_keys: set[str] = set()
    linked_cls: set[str] = set()
    # Skip re-linking CLs already attached as primary while keeping linked_cls for stack children
    primary_cls: set[str] = set()
    for r in wl_rows:
      akey = str(r["artifact_key"])
      role = str(r["role"])
      if role == "primary" and akey.startswith(("github_issue:", "buganizer:")):
        bug_keys.add(akey)
      elif akey.startswith(("gerrit:", "github_pr:", "piper:")):
        linked_cls.add(akey)
        if role == "primary":
          primary_cls.add(akey)
      elif akey.startswith("gmail_draft:"):
        ar = art_rows.get(akey) or {}
        try:
          st = json.loads(ar.get("state_json") or "{}")
        except Exception:
          st = {}
        for bk in st.get("draft_bug_keys") or []:
          bug_keys.add(canonicalize_artifact_key(str(bk)))

    if not bug_keys:
      return []

    newly_linked: list[str] = []
    # Match direct bug-fixing CLs first, then stacked child CLs whose parent matched
    for pass_num in (0, 1):
      for akey, ar in art_rows.items():
        if not akey.startswith(("gerrit:", "github_pr:", "piper:")):
          continue
        try:
          st = json.loads(ar.get("state_json") or "{}")
        except Exception:
          st = {}
        if not st.get("status") or is_hub_artifact(akey, st):
          continue
        if pass_num == 0:
          c_bugs = set(extract_cl_bug_keys(st))
          matched_bugs = sorted(c_bugs & bug_keys)
          if matched_bugs:
            linked_cls.add(akey)
            if akey not in primary_cls:
              b_short = ", ".join(format_short_bug_id(b) for b in matched_bugs[:2])
              self.link_artifact(
                  work_id,
                  akey,
                  role="primary",
                  source="auto",
                  link_reason="bug_trailer",
                  link_detail=f"В описании СЛ указан баг {b_short}",
                  now=cur_now,
              )
              primary_cls.add(akey)
              newly_linked.append(akey)
        else:
          p_raw = str(st.get("stack_parent_ikey") or "").strip()
          if p_raw:
            p_ckey = canonicalize_artifact_key(p_raw)
            if p_ckey in linked_cls:
              linked_cls.add(akey)
              if akey not in primary_cls:
                p_num = p_ckey.split(":")[-1]
                self.link_artifact(
                    work_id,
                    akey,
                    role="primary",
                    source="auto",
                    link_reason="stack_child",
                    link_detail=f"Дочерний СЛ в стеке Gerrit поверх {p_num}",
                    now=cur_now,
                )
                primary_cls.add(akey)
                newly_linked.append(akey)
    return newly_linked

  def mark_obligation_done(
      self, obligation_id: str, now: float | None = None
  ) -> None:
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT work_id, target_artifact_key, meta_json "
          "FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      ack_ts = cur_now
      if row:
        tkey = str(row["target_artifact_key"] or "").strip()
        try:
          ob_meta = json.loads(row["meta_json"] or "{}")
        except Exception:
          ob_meta = {}
        ob_meta.pop("superseded_by_specific", None)
        if tkey:
          ar = conn.execute(
              "SELECT state_json FROM artifacts WHERE artifact_key = ?", (tkey,)
          ).fetchone()
          if ar:
            try:
              st = json.loads(ar["state_json"] or "{}")
            except Exception:
              st = {}
            msg_ts = float(st.get("last_message_ts") or 0.0)
            ack_ts = max(cur_now, msg_ts)
            st["ack_message_ts"] = ack_ts
            st["unanswered_human"] = False
            conn.execute(
                "UPDATE artifacts SET state_json = ?, updated_at = ? WHERE artifact_key = ?",
                (json.dumps(st), cur_now, tkey),
            )
        conn.execute(
            "UPDATE obligations SET status = 'done', satisfied_at = ?, meta_json = ? "
            "WHERE obligation_id = ?",
            (ack_ts, json.dumps(ob_meta), obligation_id),
        )
        # Clear the Work's active reply_draft when the user manually checks off ob-wbdraft-*
        if obligation_id.startswith("ob-wbdraft-") or bool(ob_meta.get("wb_reply_draft")):
          wid_s = str(row["work_id"] or "")
          w_r = conn.execute(
              "SELECT meta_json FROM works WHERE work_id = ?", (wid_s,)
          ).fetchone()
          if w_r:
            try:
              wm = json.loads(w_r["meta_json"] or "{}")
            except Exception:
              wm = {}
            if isinstance(wm.get("reply_draft"), Mapping):
              rd_done = dict(wm["reply_draft"])
              rd_done["sent_at"] = cur_now
              mat_k = str(rd_done.get("materialized_draft_key") or "").strip()
              wm["reply_draft"] = rd_done
              conn.execute(
                  "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
                  (json.dumps(wm), cur_now, wid_s),
              )
              if mat_k.startswith("gmail_draft:"):
                m_ar = conn.execute(
                    "SELECT state_json FROM artifacts WHERE artifact_key = ?", (mat_k,)
                ).fetchone()
                if m_ar:
                  try:
                    m_st = json.loads(m_ar["state_json"] or "{}")
                  except Exception:
                    m_st = {}
                  if str(m_st.get("status") or "DRAFT").upper() == "DRAFT":
                    m_st["status"] = "DISCARDED"
                    conn.execute(
                        "UPDATE artifacts SET state_json = ?, updated_at = ? "
                        "WHERE artifact_key = ?",
                        (json.dumps(m_st), cur_now, mat_k),
                    )
        self._log_event(
            conn,
            "obligation_done",
            work_id=str(row["work_id"]),
            artifact_key=tkey,
            payload={"obligation_id": obligation_id},
            now=cur_now,
        )
      else:
        conn.execute(
            "UPDATE obligations SET status = 'done', satisfied_at = ? WHERE obligation_id = ?",
            (cur_now, obligation_id),
        )

  def mark_obligation_waiting(
      self, obligation_id: str, now: float | None = None
  ) -> None:
    """Moves a correspondence obligation to open/Waiting after an out-of-band or verbal reply."""
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT work_id, target_artifact_key, meta_json "
          "FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      if not row:
        return
      wid = str(row["work_id"] or "")
      tkey = str(row["target_artifact_key"] or "").strip()
      try:
        ob_meta = json.loads(row["meta_json"] or "{}")
      except Exception:
        ob_meta = {}
      ob_meta.pop("ready_at", None)
      # Advance manual_wait_ts past last_message_ts to start a fresh 7d silence window
      if tkey:
        ar = conn.execute(
            "SELECT state_json FROM artifacts WHERE artifact_key = ?", (tkey,)
        ).fetchone()
        if ar:
          try:
            st = json.loads(ar["state_json"] or "{}")
          except Exception:
            st = {}
          msg_ts = float(st.get("last_message_ts") or 0.0)
          st["manual_wait_ts"] = max(cur_now, msg_ts)
          st["unanswered_human"] = False
          conn.execute(
              "UPDATE artifacts SET state_json = ?, updated_at = ? WHERE artifact_key = ?",
              (json.dumps(st), cur_now, tkey),
          )
      conn.execute(
          "UPDATE obligations SET status = 'open', last_eval = 'false', meta_json = ? "
          "WHERE obligation_id = ?",
          (json.dumps(ob_meta), obligation_id),
      )
      self._log_event(
          conn,
          "obligation_waiting",
          work_id=wid,
          artifact_key=tkey,
          payload={"obligation_id": obligation_id},
          now=cur_now,
      )
    self.evaluate_all_obligations(now=cur_now)

  def resolve_obligation_ambiguity(
      self,
      obligation_id: str,
      decision: str,
      event_key: str = "",
      now: float | None = None,
  ) -> None:
    """Records a 1-click user ruling ('keep' or 'close') for a borderline closure message."""
    cur_now = time.time() if now is None else now
    dec = str(decision or "").strip().lower()
    if dec not in ("keep", "close"):
      return
    tkey = ""
    wid = ""
    with self._lock, self._connect() as conn:
      row = conn.execute(
          "SELECT work_id, target_artifact_key FROM obligations WHERE obligation_id = ?",
          (obligation_id,),
      ).fetchone()
      if not row:
        return
      wid = str(row["work_id"] or "")
      tkey = str(row["target_artifact_key"] or "").strip()
      # Persist ruling under event_key on the target artifact so syncs never re-prompt
      resolved_ev = str(event_key or "").strip()
      if tkey:
        ar = conn.execute(
            "SELECT kind, state_json FROM artifacts WHERE artifact_key = ?", (tkey,)
        ).fetchone()
        if ar:
          try:
            st = json.loads(ar["state_json"] or "{}")
          except Exception:
            st = {}
          if not resolved_ev:
            _, resolved_ev = detect_borderline_correspondence_closure(
                str(ar["kind"] or ""), st
            )
          if not resolved_ev:
            resolved_ev = str(st.get("last_message_id") or "").strip()
          if resolved_ev:
            decisions = dict(st.get("ambiguity_decisions") or {})
            decisions[resolved_ev] = dec
            st["ambiguity_decisions"] = decisions
            conn.execute(
                "UPDATE artifacts SET state_json = ?, updated_at = ? WHERE artifact_key = ?",
                (json.dumps(st), cur_now, tkey),
            )
      self._log_event(
          conn,
          "obligation_ambiguity_resolved",
          work_id=wid,
          artifact_key=tkey,
          payload={"obligation_id": obligation_id, "decision": dec, "event_key": resolved_ev},
          now=cur_now,
      )
    if dec == "close":
      self.mark_obligation_done(obligation_id, now=cur_now)
    else:
      self.evaluate_all_obligations(now=cur_now)

  def reconcile_correspondence_obligations(
      self, work_id: str | None = None, now: float | None = None
  ) -> None:
    """Ensures active correspondence channels carry end-to-end ownership umbrellas."""
    cur_now = time.time() if now is None else now
    # Evaluate sent drafts and replies first so satisfied promises hand off to 7d Waiting
    self.evaluate_all_obligations(now=cur_now)
    with self._lock, self._connect() as conn:
      if work_id:
        w_rows = conn.execute(
            "SELECT * FROM works WHERE work_id = ? AND status NOT IN ('done', 'archived')",
            (work_id,),
        ).fetchall()
      else:
        w_rows = conn.execute(
            "SELECT * FROM works WHERE status NOT IN ('done', 'archived')"
        ).fetchall()
      if not w_rows:
        return
      art_rows = {
          str(r["artifact_key"]): dict(r)
          for r in conn.execute("SELECT * FROM artifacts").fetchall()
      }
      for ar in art_rows.values():
        try:
          ar["state"] = json.loads(ar.get("state_json") or "{}")
        except Exception:
          ar["state"] = {}

      for wr in w_rows:
        wid = str(wr["work_id"])
        wl_rows = conn.execute(
            "SELECT artifact_key, role FROM work_links "
            "WHERE work_id = ? AND role IN ('primary', 'channel') "
            "ORDER BY created_at ASC",
            (wid,),
        ).fetchall()
        ob_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM obligations WHERE work_id = ?", (wid,)
            ).fetchall()
        ]
        linked_keys = [str(r["artifact_key"]) for r in wl_rows]
        linked_set = set(linked_keys)
        gmail_channels = [k for k in linked_keys if k.startswith("gmail_thread:")]
        gdoc_ids_with_threads = {
            k.split(":", 2)[1]
            for k in linked_keys
            if k.startswith("gdoc_thread:") and k.count(":") >= 2
        }

        # Close orphaned umbrellas and canonicalize migrated ob-corr IDs after merge/split
        existing_corr_by_target: dict[str, dict[str, Any]] = {}
        for o in ob_rows:
          if str(o.get("kind") or "") != "correspondence":
            continue
          t_ak = str(o.get("target_artifact_key") or "").strip()
          cur_oid = str(o.get("obligation_id") or "")
          if not t_ak or t_ak not in linked_set:
            if str(o.get("status") or "") in ("open", "ready"):
              conn.execute(
                  "UPDATE obligations SET status = 'done', satisfied_at = ? "
                  "WHERE obligation_id = ?",
                  (cur_now, cur_oid),
              )
            continue
          canon_oid = f"ob-corr-{hashlib.sha1(f'{wid}:{t_ak}'.encode('utf-8')).hexdigest()[:10]}"
          if cur_oid != canon_oid:
            if any(str(x.get("obligation_id") or "") == canon_oid for x in ob_rows):
              conn.execute("DELETE FROM obligations WHERE obligation_id = ?", (cur_oid,))
              continue
            conn.execute(
                "UPDATE obligations SET obligation_id = ? WHERE obligation_id = ?",
                (canon_oid, cur_oid),
            )
            o["obligation_id"] = canon_oid
          if t_ak in existing_corr_by_target:
            conn.execute(
                "DELETE FROM obligations WHERE obligation_id = ?", (o["obligation_id"],)
            )
            continue
          existing_corr_by_target[t_ak] = o

        # Canonicalize migrated ob-wbdraft IDs after merge_works and sync auto_gate triggers
        canon_wb_oid = _wb_draft_obligation_id(wid)
        for o in list(ob_rows):
          cur_oid = str(o.get("obligation_id") or "")
          if not cur_oid.startswith("ob-wbdraft-") or cur_oid == canon_wb_oid:
            continue
          if any(str(x.get("obligation_id") or "") == canon_wb_oid for x in ob_rows):
            conn.execute("DELETE FROM obligations WHERE obligation_id = ?", (cur_oid,))
            ob_rows.remove(o)
          else:
            conn.execute(
                "UPDATE obligations SET obligation_id = ? WHERE obligation_id = ?",
                (canon_wb_oid, cur_oid),
            )
            o["obligation_id"] = canon_wb_oid

        # Refresh auto-gated reply-draft triggers whenever primary CLs or bugs change on the Work
        active_rd = _active_reply_draft_from_meta(wr["meta_json"])
        if active_rd is not None and bool(active_rd.get("auto_gate")):
          w_roles_map = {str(r["artifact_key"]): str(r["role"]) for r in wl_rows}
          auto_trig, auto_desc = _build_auto_gate_spec(
              str(wr["title"] or ""), w_roles_map, art_rows, active_rd
          )
          existing_wb = next(
              (x for x in ob_rows if str(x.get("obligation_id") or "") == canon_wb_oid),
              None,
          )
          if auto_trig:
            rd_tkey = str(active_rd.get("thread_key") or "").strip()
            if rd_tkey and rd_tkey not in linked_set:
              rd_tkey = ""
            is_standalone_rd = bool(
                str(active_rd.get("to") or "").strip()
                and str(active_rd.get("subject") or "").strip()
                and not rd_tkey
            )
            if not rd_tkey and not is_standalone_rd:
              for gk in gmail_channels:
                gk_st = (art_rows.get(gk) or {}).get("state")
                if is_active_correspondence_artifact(
                    "gmail_thread", gk_st if isinstance(gk_st, Mapping) else {}
                ):
                  rd_tkey = gk
                  break
            mat_key = str(active_rd.get("materialized_draft_key") or "").strip()
            done_ref = mat_key or rd_tkey
            done_m = f"reply_sent({done_ref})" if done_ref else ""
            if existing_wb is not None:
              try:
                wb_meta = json.loads(existing_wb.get("meta_json") or "{}")
              except Exception:
                wb_meta = {}
              if wb_meta.get("auto_gate", True):
                wb_meta["wb_reply_draft"] = True
                wb_meta["auto_gate"] = True
                wb_meta["draft_updated_ts"] = float(
                    active_rd.get("updated_at") or wb_meta.get("draft_updated_ts") or cur_now
                )
                next_st = (
                    "open"
                    if str(existing_wb.get("status") or "") == "done"
                    else str(existing_wb.get("status") or "open")
                )
                conn.execute(
                    "UPDATE obligations SET target_artifact_key = ?, description = ?, "
                    "trigger_expr = ?, done_matcher = ?, status = ?, meta_json = ? "
                    "WHERE obligation_id = ?",
                    (
                        rd_tkey,
                        auto_desc,
                        auto_trig,
                        done_m,
                        next_st,
                        json.dumps(wb_meta),
                        canon_wb_oid,
                    ),
                )
                existing_wb["target_artifact_key"] = rd_tkey
                existing_wb["description"] = auto_desc
                existing_wb["trigger_expr"] = auto_trig
                existing_wb["done_matcher"] = done_m
                existing_wb["status"] = next_st
                existing_wb["meta_json"] = json.dumps(wb_meta)
            else:
              wb_meta = {
                  "wb_reply_draft": True,
                  "auto_gate": True,
                  "draft_updated_ts": float(active_rd.get("updated_at") or cur_now),
              }
              conn.execute(
                  "INSERT INTO obligations "
                  "(obligation_id, work_id, target_artifact_key, kind, description, "
                  "trigger_expr, done_matcher, status, last_eval, created_at, "
                  "satisfied_at, meta_json) "
                  "VALUES (?, ?, ?, 'reply', ?, ?, ?, 'open', 'unknown', ?, 0, ?)",
                  (
                      canon_wb_oid,
                      wid,
                      rd_tkey,
                      auto_desc,
                      auto_trig,
                      done_m,
                      cur_now,
                      json.dumps(wb_meta),
                  ),
              )
              ob_rows.append({
                  "obligation_id": canon_wb_oid,
                  "work_id": wid,
                  "target_artifact_key": rd_tkey,
                  "kind": "reply",
                  "description": auto_desc,
                  "trigger_expr": auto_trig,
                  "done_matcher": done_m,
                  "status": "open",
                  "last_eval": "unknown",
                  "created_at": cur_now,
                  "satisfied_at": 0,
                  "meta_json": json.dumps(wb_meta),
              })
          elif existing_wb is not None and str(existing_wb.get("status") or "") in (
              "open",
              "ready",
          ):
            try:
              wb_meta = json.loads(existing_wb.get("meta_json") or "{}")
            except Exception:
              wb_meta = {}
            # Retire stale auto_gate obligations when the last primary CL/bug is unlinked
            if wb_meta.get("auto_gate", True):
              conn.execute(
                  "UPDATE obligations SET status = 'done', satisfied_at = ? "
                  "WHERE obligation_id = ?",
                  (cur_now, canon_wb_oid),
              )
              existing_wb["status"] = "done"

        # Collect per-channel specific obligations so CL/bug obligations leave other channels live
        superseded_targets: set[str] = set()
        has_untargeted_reply = False
        for o in ob_rows:
          if str(o.get("kind") or "") == "correspondence":
            continue
          if str(o.get("status") or "") not in ("open", "ready"):
            continue
          try:
            o_meta = json.loads(o.get("meta_json") or "{}")
          except Exception:
            o_meta = {}
          is_wb_draft_ob = bool(o_meta.get("wb_reply_draft")) or str(
              o.get("obligation_id") or ""
          ).startswith("ob-wbdraft-")
          o_tkey = str(o.get("target_artifact_key") or "").strip()
          if not o_tkey:
            # Standalone untargeted reply_draft notes must never silence unrelated gdoc/chat items
            if str(o.get("kind") or "") == "reply" and not is_wb_draft_ob:
              has_untargeted_reply = True
            continue
          if o_tkey.startswith("gmail_draft:"):
            superseded_targets.add(o_tkey)
            d_row = art_rows.get(o_tkey) or {}
            d_st = d_row.get("state") or {}
            d_upd_ts = float(
                d_st.get("draft_updated_ts")
                or d_row.get("updated_at")
                or o.get("created_at")
                or 0.0
            )
            d_tid = str(d_st.get("thread_id") or "").strip()
            cand_tkey = (
                f"gmail_thread:{d_tid}"
                if d_tid
                else (gmail_channels[0] if len(gmail_channels) == 1 else "")
            )
            if cand_tkey and _is_thread_covered_by_draft_ts(
                (art_rows.get(cand_tkey) or {}).get("state"), d_upd_ts, art_rows
            ):
              superseded_targets.add(cand_tkey)
          elif is_wb_draft_ob:
            # Parked Workbench reply drafts stop superseding once a newer counterparty reply lands
            d_upd_ts = float(
                o_meta.get("draft_updated_ts")
                or (active_rd or {}).get("updated_at")
                or o.get("created_at")
                or 0.0
            )
            if _is_thread_covered_by_draft_ts(
                (art_rows.get(o_tkey) or {}).get("state"), d_upd_ts, art_rows
            ):
              superseded_targets.add(o_tkey)
          else:
            superseded_targets.add(o_tkey)

        for akey in linked_keys:
          ar = art_rows.get(akey)
          if not ar:
            continue
          akind = str(ar.get("kind") or artifact_kind_from_key(akey))
          st = ar.get("state") if isinstance(ar.get("state"), Mapping) else {}
          if is_config_artifact(akey, title=str(ar.get("title") or ""), state=st):
            continue
          if not is_active_correspondence_artifact(akind, st):
            prev_inactive = existing_corr_by_target.get(akey)
            if prev_inactive is not None and str(prev_inactive.get("status") or "") in (
                "open",
                "ready",
            ):
              conn.execute(
                  "UPDATE obligations SET status = 'done', satisfied_at = ? "
                  "WHERE obligation_id = ?",
                  (cur_now, prev_inactive["obligation_id"]),
              )
            continue

          # Concrete #disco threads supersede doc-level umbrellas and notification transport threads
          is_superseded_transport = False
          if akind == "gdoc":
            doc_id = akey.split(":", 1)[1] if ":" in akey else ""
            is_superseded_transport = doc_id in gdoc_ids_with_threads
          elif akind == "gmail_thread":
            gt_ref = str(st.get("gdoc_thread_key") or "")
            sd_ref = str(st.get("shared_doc_key") or "")
            gd_id = str(st.get("gdoc_id") or "")
            is_superseded_transport = (
                bool(gt_ref and gt_ref in linked_set)
                or bool(sd_ref and sd_ref in linked_set)
                or (
                    bool(st.get("is_gdoc_comment") or st.get("is_drive_share"))
                    and bool(gd_id)
                    and (f"gdoc:{gd_id}" in linked_set or gd_id in gdoc_ids_with_threads)
                )
            )
          if is_superseded_transport:
            conn.execute(
                "UPDATE obligations SET status = 'done', satisfied_at = ? "
                "WHERE work_id = ? AND kind = 'correspondence' "
                "AND target_artifact_key = ? AND status IN ('open', 'ready')",
                (cur_now, wid, akey),
            )
            continue

          if akey in superseded_targets or has_untargeted_reply:
            prev_sup = existing_corr_by_target.get(akey)
            if prev_sup is not None and str(prev_sup.get("status") or "") in ("open", "ready"):
              try:
                p_meta = json.loads(prev_sup.get("meta_json") or "{}")
              except Exception:
                p_meta = {}
              p_meta["superseded_by_specific"] = True
              conn.execute(
                  "UPDATE obligations SET status = 'done', satisfied_at = ?, meta_json = ? "
                  "WHERE obligation_id = ?",
                  (cur_now, json.dumps(p_meta), prev_sup["obligation_id"]),
              )
            continue

          # Deterministic ID per (work_id, artifact_key) preserves obligation history across syncs
          digest = hashlib.sha1(f"{wid}:{akey}".encode("utf-8")).hexdigest()[:10]
          ob_id = f"ob-corr-{digest}"
          cp = str(st.get("counterparty") or "").strip()
          if akind == "gdoc_thread":
            doc_t = str(st.get("doc_title") or ar.get("title") or akey).strip()
            cp_part = f" ({cp})" if cp and cp.lower() not in doc_t.lower() else ""
            desc = f"Follow up / resolve comment in '{doc_t}'{cp_part}"
          elif akind == "gdoc":
            doc_t = str(st.get("doc_title") or ar.get("title") or akey).strip()
            cp_part = f" ({cp})" if cp and cp.lower() not in doc_t.lower() else ""
            desc = f"Follow up on shared doc '{doc_t}'{cp_part}"
          else:
            subj = str(st.get("root_subject") or ar.get("title") or akey).strip()
            cp_part = f" ({cp})" if cp and cp.lower() not in subj.lower() else ""
            desc = f"Close loop in '{subj}'{cp_part}"
          trig = f"correspondence_turn({akey}, {STALLED_OBLIGATION_DAYS})"

          prev_ob = existing_corr_by_target.get(akey)
          from_me = bool(st.get("last_from_me"))
          gh_target_st = _get_github_target_state(st, art_rows)
          eff_ext_ts, gh_self_ts = _resolve_github_thread_turns(st, gh_target_st)
          gh_human_ts = max(
              float(st.get("gh_human_reply_ts") or 0.0),
              float(gh_target_st.get("last_human_comment_at") or 0.0),
          )
          is_self_resolved = (
              (bool(st.get("resolved")) and bool(st.get("resolved_by_me")))
              or is_github_thread_resolved(st, art_rows)
          )
          # Reopen when a specific draft/reply finished or a newer counterparty message arrived
          if prev_ob is not None:
            if str(prev_ob.get("status") or "") == "done":
              try:
                prev_meta = json.loads(prev_ob.get("meta_json") or "{}")
              except Exception:
                prev_meta = {}
              was_superseded = bool(prev_meta.pop("superseded_by_specific", False))
              done_floor = max(
                  float(prev_ob.get("satisfied_at") or 0.0),
                  float(st.get("ack_message_ts") or 0.0),
                  float(st.get("resolved_at") or 0.0),
              )
              should_reopen = (
                  was_superseded
                  or (not from_me and eff_ext_ts > done_floor)
                  or (gh_human_ts > max(done_floor, gh_self_ts))
              )
              if should_reopen and not is_self_resolved:
                conn.execute(
                    "UPDATE obligations SET status = 'open', last_eval = 'unknown', "
                    "satisfied_at = 0, description = ?, trigger_expr = ?, meta_json = ? "
                    "WHERE obligation_id = ?",
                    (desc, trig, json.dumps(prev_meta), ob_id),
                )
            elif is_self_resolved:
              conn.execute(
                  "UPDATE obligations SET status = 'done', satisfied_at = ? "
                  "WHERE obligation_id = ?",
                  (cur_now, ob_id),
              )
            else:
              conn.execute(
                  "UPDATE obligations SET description = ?, trigger_expr = ? "
                  "WHERE obligation_id = ?",
                  (desc, trig, ob_id),
              )
            continue

          if is_self_resolved:
            continue
          conn.execute(
              "INSERT INTO obligations "
              "(obligation_id, work_id, target_artifact_key, kind, description, "
              "trigger_expr, done_matcher, status, last_eval, created_at, satisfied_at, meta_json) "
              "VALUES (?, ?, ?, 'correspondence', ?, ?, '', 'open', 'unknown', ?, 0, '{}')",
              (ob_id, wid, akey, desc, trig, cur_now),
          )
          self._log_event(
              conn,
              "obligation_created",
              work_id=wid,
              artifact_key=akey,
              payload={"obligation_id": ob_id, "trigger_expr": trig, "auto_correspondence": True},
              now=cur_now,
          )
    self.evaluate_all_obligations(now=cur_now)

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
          d.get("kind") != "correspondence"
          and d.get("status") == "open"
          and d.get("last_eval") in ("false", "unknown")
          and age_s >= STALLED_OBLIGATION_DAYS * 86400
      )
      return d

  def evaluate_all_obligations(self, now: float | None = None) -> None:
    """Re-evaluates open/ready obligations and marks satisfied obligations and reply_drafts done."""
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
        try:
          ob_meta = json.loads(ob.get("meta_json") or "{}")
        except Exception:
          ob_meta = {}

        res = evaluate_trigger(
            str(ob.get("trigger_expr") or "always"),
            artifacts_by_key,
            w_row,
            now=cur_now,
        )
        new_status = "ready" if res == "true" else "open"
        meta_changed = False
        if new_status == "ready" and ob.get("status") != "ready":
          ob_meta["ready_at"] = cur_now
          meta_changed = True
        elif new_status != "ready" and "ready_at" in ob_meta:
          ob_meta.pop("ready_at", None)
          meta_changed = True

        ready_at = float(ob_meta.get("ready_at") or 0.0)
        if _is_done_matched(ob, artifacts_by_key, trigger_eval=res, ready_at=ready_at):
          conn.execute(
              "UPDATE obligations SET status = 'done', satisfied_at = ?, meta_json = ? "
              "WHERE obligation_id = ?",
              (cur_now, json.dumps(ob_meta), ob_id),
          )
          # Mark the Workbench reply_draft sent once its obligation completes so the banner clears
          if ob_id.startswith("ob-wbdraft-") or bool(ob_meta.get("wb_reply_draft")):
            try:
              wm = json.loads(w_row.get("meta_json") or "{}")
            except Exception:
              wm = {}
            if isinstance(wm.get("reply_draft"), Mapping):
              rd_done = dict(wm["reply_draft"])
              rd_done["sent_at"] = cur_now
              wm["reply_draft"] = rd_done
              conn.execute(
                  "UPDATE works SET meta_json = ? WHERE work_id = ?",
                  (json.dumps(wm), str(ob["work_id"])),
              )
              w_row["meta_json"] = json.dumps(wm)
          self._log_event(
              conn,
              "obligation_done",
              work_id=str(ob["work_id"]),
              artifact_key=str(ob.get("target_artifact_key") or ""),
              payload={"obligation_id": ob_id, "auto": True},
              now=cur_now,
          )
          continue

        if new_status != ob.get("status") or res != ob.get("last_eval") or meta_changed:
          conn.execute(
              "UPDATE obligations SET status = ?, last_eval = ?, meta_json = ? "
              "WHERE obligation_id = ?",
              (new_status, res, json.dumps(ob_meta), ob_id),
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
      self,
      target_work_id: str,
      source_work_id: str,
      source: str = "manual",
      now: float | None = None,
  ) -> None:
    """Merges source_work_id links, obligations, and newer reply_draft into target_work_id."""
    if target_work_id == source_work_id:
      return
    cur_now = time.time() if now is None else now
    with self._lock, self._connect() as conn:
      conn.execute(
          "INSERT OR IGNORE INTO work_links "
          "(work_id, artifact_key, role, source, confidence, created_at, link_reason, link_detail) "
          "SELECT ?, artifact_key, role, ?, confidence, ?, link_reason, link_detail "
          "FROM work_links WHERE work_id = ? "
          "AND (? = 'manual' OR artifact_key NOT IN ("
          "  SELECT artifact_key FROM negative_links WHERE work_id = ?"
          "))",
          (target_work_id, source, cur_now, source_work_id, source, target_work_id),
      )
      conn.execute(
          "UPDATE obligations SET work_id = ? WHERE work_id = ?",
          (target_work_id, source_work_id),
      )
      conn.execute(
          "DELETE FROM work_links WHERE work_id = ?",
          (source_work_id,),
      )
      t_meta_row = conn.execute(
          "SELECT meta_json FROM works WHERE work_id = ?", (target_work_id,)
      ).fetchone()
      s_meta_row = conn.execute(
          "SELECT meta_json FROM works WHERE work_id = ?", (source_work_id,)
      ).fetchone()
      merged_meta = _merge_reply_draft_meta(
          t_meta_row["meta_json"] if t_meta_row else "{}",
          s_meta_row["meta_json"] if s_meta_row else "{}",
      )
      conn.execute(
          "UPDATE works SET status = 'archived', updated_at = ? WHERE work_id = ?",
          (cur_now, source_work_id),
      )
      if merged_meta is not None:
        conn.execute(
            "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
            (json.dumps(merged_meta), cur_now, target_work_id),
        )
      else:
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
    """Splits artifact_key out of source_work_id into a new Work while keeping card drafts."""
    cur_now = time.time() if now is None else now
    akey = canonicalize_artifact_key(artifact_key)
    self.unlink_artifact(source_work_id, akey, now=cur_now)
    new_wid = self.create_work(new_title, now=cur_now)
    self.link_artifact(new_wid, akey, role="primary", source="manual", now=cur_now)
    with self._lock, self._connect() as conn:
      # Keep ob-wbdraft-* paired with source_work_id's reply_draft metadata
      conn.execute(
          "UPDATE obligations SET work_id = ? "
          "WHERE work_id = ? AND target_artifact_key = ? "
          "AND obligation_id NOT LIKE 'ob-wbdraft-%'",
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
    self.reconcile_correspondence_obligations(source_work_id, now=cur_now)
    self.reconcile_correspondence_obligations(new_wid, now=cur_now)
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
    gated_draft_keys: set[str] = set()
    suppressed_threads = _collect_suppressed_threads(artifacts, obligations)
    has_open_wb_draft_ob = False
    has_ready_wb_draft_ob = False
    ready_target_keys: set[str] = set()
    art_state_by_key: dict[str, Mapping[str, Any]] = {}
    all_art_by_key: dict[str, Mapping[str, Any]] = {}
    for art in artifacts:
      ak = str(art.get("artifact_key") or "")
      all_art_by_key[ak] = art
      if art.get("role") == "context_only":
        continue
      art_state_by_key[ak] = (
          art.get("state") if isinstance(art.get("state"), Mapping) else {}
      )

    for ob in obligations:
      t_key = str(ob.get("target_artifact_key") or "")
      try:
        ob_meta = json.loads(ob.get("meta_json") or "{}")
      except Exception:
        ob_meta = {}
      is_wb_ob = bool(
          ob.get("wb_reply_draft")
          or ob_meta.get("wb_reply_draft")
          or str(ob.get("obligation_id") or "").startswith("ob-wbdraft-")
      )
      if ob.get("status") == "open":
        if is_wb_ob:
          has_open_wb_draft_ob = True
        if t_key.startswith("gmail_draft:"):
          gated_draft_keys.add(t_key)
      if ob.get("status") == "ready":
        if is_wb_ob:
          has_ready_wb_draft_ob = True
        if t_key:
          ready_target_keys.add(t_key)
        # Separate outbound 5-7d silence reminders from genuine inbound messages needing a reply
        if ob.get("kind") == "correspondence":
          st_t = art_state_by_key.get(t_key)
          if is_github_thread_resolved(st_t, all_art_by_key):
            continue
          if _is_correspondence_silence_ping(st_t, all_art_by_key):
            reasons.append(f"⏰ 7d silence ping: {ob.get('description')}")
          else:
            reasons.append(f"📨 Reply needed: {ob.get('description')}")
        else:
          reasons.append(f"🔔 Ready: {ob.get('description')}")
      elif ob.get("stalled_alert"):
        reasons.append(f"⚠️ Stalled >7d: {ob.get('description')}")

    active_rd = _active_reply_draft_from_meta(work_row.get("meta_json"))
    mat_dk = str((active_rd or {}).get("materialized_draft_key") or "")
    if mat_dk and (has_open_wb_draft_ob or has_ready_wb_draft_ob):
      gated_draft_keys.add(mat_dk)

    # Index non-companion primary CLs/PRs so companion alerts resolve their parent state
    parent_cls_by_key: dict[str, dict[str, Any]] = {}
    for art in artifacts:
      if art.get("role") == "context_only":
        continue
      akey = str(art.get("artifact_key") or "")
      kind = str(art.get("kind") or "")
      st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
      if kind in ("gerrit", "github_pr", "piper") and not is_companion_artifact(
          akey, st, str(art.get("title") or ""), str(art.get("link_reason") or "")
      ):
        parent_cls_by_key[akey] = art

    for art in artifacts:
      if art.get("role") == "context_only":
        continue
      akey = str(art.get("artifact_key") or "")
      kind = str(art.get("kind") or "")
      st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
      if kind == "gmail_draft" and str(st.get("status") or "DRAFT").upper() == "DRAFT":
        # Drafts gated by an open obligation are parked until the trigger becomes ready
        if akey not in gated_draft_keys:
          reasons.append(f"📝 Unsent draft: {art.get('title')}")
      elif (
          kind in ("gmail_thread", "gchat_thread", "gdoc_thread")
          and bool(st.get("unanswered_human"))
          and akey not in ready_target_keys
          and akey not in suppressed_threads
      ):
        if not (bool(st.get("in_trash_or_spam")) and not bool(st.get("sent_by_me"))) and not (
            is_github_thread_resolved(st, all_art_by_key)
        ):
          t_st = _get_github_target_state(st, all_art_by_key)
          ext_ts, gh_self_ts = _resolve_github_thread_turns(st, t_st)
          if not (gh_self_ts > 0 and gh_self_ts >= ext_ts):
            reasons.append(f"📨 Unanswered message: {art.get('title')}")
      elif kind == "chat_chain" and str(st.get("bucket") or "") == "needs-me":
        reasons.append(f"🤖 Chat waiting on you: {art.get('title')}")
      elif kind in ("gerrit", "github_pr", "piper") and is_companion_artifact(
          akey, st, str(art.get("title") or ""), str(art.get("link_reason") or "")
      ):
        # Resolve parent CL state so companion comments and orphaned cleanup roll up onto the Work
        p_key = canonicalize_companion_parent(akey, st, str(art.get("title") or ""))
        p_art = parent_cls_by_key.get(p_key) if p_key else None
        if p_art is None and not p_key and len(parent_cls_by_key) == 1:
          p_key, p_art = next(iter(parent_cls_by_key.items()))
        p_st = (
            p_art.get("state")
            if (p_art is not None and isinstance(p_art.get("state"), Mapping))
            else None
        )
        orphan_r, comment_r = _evaluate_companion_alerts(akey, st, p_key, p_st)
        reasons.extend(orphan_r)
        reasons.extend(comment_r)
      elif kind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue"):
        cl_reason = _evaluate_cl_needs_you(akey, st)
        if cl_reason:
          reasons.append(cl_reason)

    if active_rd is not None and not has_open_wb_draft_ob and not has_ready_wb_draft_ob:
      if not any(r.startswith("📝 Unsent") for r in reasons):
        reasons.append("📝 Unsent reply draft")

    if reasons:
      return "needs-you", reasons

    open_obs = [ob for ob in obligations if ob.get("status") in ("open", "ready")]
    # Exclude companion probes from active_cls so open companions do not block terminal parents
    non_comp_cls = list(parent_cls_by_key.values())
    active_cls = [
        a
        for a in non_comp_cls
        if str((a.get("state") or {}).get("status") or "NEW").upper() in ("NEW", "OPEN")
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
      # Complete the Work once all primary non-companion CLs reached a terminal state
      has_terminal_cl = bool(non_comp_cls) and all(
          str((a.get("state") or {}).get("status") or "").upper()
          in ("MERGED", "CLOSED", "ABANDONED")
          for a in non_comp_cls
      )
      if has_terminal_cl:
        all_merged = all(
            str((a.get("state") or {}).get("status") or "").upper() == "MERGED"
            for a in non_comp_cls
        )
        done_msg = (
            "All CLs merged and obligations satisfied"
            if all_merged
            else "All CLs closed and obligations satisfied"
        )
        return "done", [done_msg]
      # Complete CL-less cards once tracked obligations finish or closed issues/GH threads resolve
      has_non_ctx_chat = any(
          a.get("role") != "context_only" and a.get("kind") == "chat_chain"
          for a in artifacts
      )
      has_closed_bug_or_resolved_gh = any(
          a.get("role") != "context_only"
          and (
              (
                  a.get("kind") in ("buganizer", "github_issue")
                  and str((a.get("state") or {}).get("status") or "").upper()
                  in CLOSED_BUG_STATUSES
              )
              or (
                  a.get("kind") == "gmail_thread"
                  and is_github_thread_resolved(
                      a.get("state") if isinstance(a.get("state"), Mapping) else {},
                      all_art_by_key,
                  )
              )
          )
          for a in artifacts
      )
      has_unresolved_corr = any(
          a.get("role") != "context_only"
          and is_active_correspondence_artifact(
              str(a.get("kind") or ""),
              a.get("state") if isinstance(a.get("state"), Mapping) else {},
          )
          and not (
              is_github_thread_resolved(
                  a.get("state") if isinstance(a.get("state"), Mapping) else {},
                  all_art_by_key,
              )
              or (
                  bool((a.get("state") or {}).get("resolved"))
                  and bool((a.get("state") or {}).get("resolved_by_me"))
              )
          )
          for a in artifacts
      )
      if not non_comp_cls and (
          obligations
          or (has_closed_bug_or_resolved_gh and not has_non_ctx_chat and not has_unresolved_corr)
      ):
        done_msg = (
            "All obligations satisfied"
            if obligations
            else "All linked issues closed and obligations satisfied"
        )
        return "done", [done_msg]

    wait_reasons: list[str] = []
    for ob in open_obs:
      prog = str(ob.get("progress_summary") or "")
      prog_suffix = f" ({prog})" if prog else ""
      wait_reasons.append(f"⏳ Parked: {ob.get('description')}{prog_suffix}")
    if not wait_reasons:
      wait_reasons.append("In progress / waiting on external actors")
    return "waiting", wait_reasons

  def purge_config_artifacts_and_works(
      self, now: float | None = None
  ) -> dict[str, int]:
    """Removes config/subagent artifacts and archives Works that only tracked config activity."""
    cur_now = time.time() if now is None else now
    links_removed = 0
    works_archived = 0
    works_repaired = 0
    with self._lock, self._connect() as conn:
      art_rows = {
          str(r["artifact_key"]): dict(r)
          for r in conn.execute("SELECT * FROM artifacts").fetchall()
      }

      # Safely decode JSON state dicts when repairing or filtering SQLite artifact rows
      def _parse_art_state(ar: Mapping[str, Any] | None) -> dict[str, Any]:
        if not ar:
          return {}
        try:
          st_val = json.loads(str(ar.get("state_json") or "{}"))
          return dict(st_val) if isinstance(st_val, Mapping) else {}
        except Exception:
          return {}

      # Normalize companion_to pointers and link_reason across all stored companion artifacts
      for akey, ar in list(art_rows.items()):
        st = _parse_art_state(ar)
        if not is_companion_artifact(akey, st, str(ar.get("title") or "")):
          continue
        norm_parent = canonicalize_companion_parent(akey, st, str(ar.get("title") or ""))
        if not st.get("is_companion") or (norm_parent and st.get("companion_to") != norm_parent):
          st["is_companion"] = True
          if norm_parent:
            st["companion_to"] = norm_parent
          s_json = json.dumps(st)
          conn.execute(
              "UPDATE artifacts SET state_json = ?, updated_at = ? WHERE artifact_key = ?",
              (s_json, cur_now, akey),
          )
          ar["state_json"] = s_json
        conn.execute(
            "UPDATE work_links SET link_reason = 'companion_wip' "
            "WHERE artifact_key = ? AND role = 'primary' AND source != 'manual' "
            "AND (link_reason IS NULL OR link_reason = '' OR link_reason = 'chat_mention')",
            (akey,),
        )

      # Migrate legacy buganizer:<id> rows that actually hold GitHub issues before deduping
      for akey, ar in list(art_rows.items()):
        if not akey.startswith("buganizer:"):
          continue
        st = _parse_art_state(ar)
        st_url = str(st.get("url") or "").strip()
        ar_url = str(ar.get("url") or "").strip()
        # Ignore generic chromium/buganizer fallback URLs when state holds GitHub metadata
        cand_url = (
            st_url
            if (
                _RE_GH_ISSUE_URL.search(st_url)
                or not ar_url
                or "issues.chromium.org" in ar_url.lower()
                or "b.corp.google.com" in ar_url.lower()
            )
            else ar_url
        )
        src = str(st.get("source") or st.get("tracker_flavor") or "").strip().lower()
        raw_bk = str(st.get("bug_key") or "").strip()
        is_legacy_gh = (
            src == "github"
            or raw_bk.lower().startswith(("gh_issue:", "github_issue:", "flutter_issue:"))
            or "#" in raw_bk
            or _RE_GH_ISSUE_URL.search(cand_url) is not None
        )
        if not is_legacy_gh:
          continue
        eff_gh_url = cand_url if _RE_GH_ISSUE_URL.search(cand_url) else ""
        b_desc = str(st.get("bug_title") or st.get("title") or st.get("summary") or "").strip()
        if b_desc and not st.get("bug_title"):
          st["bug_title"] = b_desc
        gh_ckey, gh_kind, gh_title, gh_url = canonicalize_watcher_bug(
            akey, {**st, "url": eff_gh_url}
        )
        if not gh_ckey or gh_ckey == akey:
          continue
        # Insert canonical github_issue row or merge state/title/url without losing newer data
        gh_existing = art_rows.get(gh_ckey)
        if gh_existing is None:
          conn.execute(
              "INSERT INTO artifacts (artifact_key, kind, title, url, state_json, updated_at) "
              "VALUES (?, ?, ?, ?, ?, ?)",
              (gh_ckey, gh_kind, gh_title, gh_url, json.dumps(st), cur_now),
          )
          art_rows[gh_ckey] = {
              "artifact_key": gh_ckey,
              "kind": gh_kind,
              "title": gh_title,
              "url": gh_url,
              "state_json": json.dumps(st),
          }
        else:
          gh_st = _parse_art_state(gh_existing)
          merged_gh = {**st, **gh_st}
          old_title = str(gh_existing.get("title") or "")
          new_title = (
              gh_title
              if (gh_title and is_placeholder_title(old_title, gh_ckey))
              else old_title
          )
          old_url = str(gh_existing.get("url") or "").strip()
          new_url = (
              gh_url
              if (gh_url and (not old_url or _RE_GH_ISSUE_URL.search(old_url) is None))
              else old_url
          )
          if merged_gh != gh_st or new_title != old_title or new_url != old_url:
            s_json = json.dumps(merged_gh)
            conn.execute(
                "UPDATE artifacts SET title = ?, url = ?, state_json = ?, updated_at = ? "
                "WHERE artifact_key = ?",
                (new_title, new_url, s_json, cur_now, gh_ckey),
            )
            gh_existing["title"] = new_title
            gh_existing["url"] = new_url
            gh_existing["state_json"] = s_json
        # Re-point links, obligations (including trigger_expr), and tombstones before deletion
        conn.execute(
            "INSERT OR IGNORE INTO work_links "
            "(work_id, artifact_key, role, source, confidence, created_at, "
            "link_reason, link_detail) "
            "SELECT work_id, ?, role, source, confidence, created_at, link_reason, link_detail "
            "FROM work_links WHERE artifact_key = ?",
            (gh_ckey, akey),
        )
        conn.execute("DELETE FROM work_links WHERE artifact_key = ?", (akey,))
        conn.execute(
            "UPDATE obligations SET target_artifact_key = ? WHERE target_artifact_key = ?",
            (gh_ckey, akey),
        )
        for ob_r in conn.execute(
            "SELECT obligation_id, trigger_expr FROM obligations WHERE instr(trigger_expr, ?) > 0",
            (akey,),
        ).fetchall():
          old_expr = str(ob_r["trigger_expr"] or "")
          new_expr = re.sub(
              rf"(?<![A-Za-z0-9_:/.-]){re.escape(akey)}(?![A-Za-z0-9_:/.-])",
              gh_ckey,
              old_expr,
          )
          if new_expr != old_expr:
            conn.execute(
                "UPDATE obligations SET trigger_expr = ? WHERE obligation_id = ?",
                (new_expr, str(ob_r["obligation_id"])),
            )
        conn.execute(
            "INSERT OR IGNORE INTO negative_links (work_id, artifact_key, created_at) "
            "SELECT work_id, ?, created_at FROM negative_links WHERE artifact_key = ?",
            (gh_ckey, akey),
        )
        conn.execute("DELETE FROM negative_links WHERE artifact_key = ?", (akey,))
        conn.execute("DELETE FROM candidate_buffer WHERE artifact_key = ?", (akey,))
        conn.execute("DELETE FROM artifacts WHERE artifact_key = ?", (akey,))
        art_rows.pop(akey, None)

      # Deduplicate GitHub's shared PR/Issue counter when HTML hovercards minted placeholders
      gh_pr_nums = {
          k.split(":")[-1]
          for k in art_rows
          if k.startswith("github_pr:flutter/flutter:")
      }
      gh_issue_nums = {
          k.split(":")[-1]
          for k in art_rows
          if k.startswith("github_issue:flutter/flutter:")
      }
      # Inspect colliding numbers and drop only the unexpanded placeholder side
      for num in sorted(gh_pr_nums & gh_issue_nums):
        p_key = f"github_pr:flutter/flutter:{num}"
        i_key = f"github_issue:flutter/flutter:{num}"
        p_row = art_rows.get(p_key) or {}
        i_row = art_rows.get(i_key) or {}
        p_st = _parse_art_state(p_row)
        i_st = _parse_art_state(i_row)
        p_is_ph = is_placeholder_title(str(p_row.get("title") or ""), p_key) and not p_st
        i_is_ph = (
            is_placeholder_title(str(i_row.get("title") or ""), i_key)
            and not i_st.get("bug_title")
        )
        # Explicit /pull/<N> or PR #<N> wins whenever the issue side has no real bug_title
        if i_is_ph:
          # Promote subject/companion PRs onto Works that held the mis-extracted issue key
          i_links = conn.execute(
              "SELECT work_id, role FROM work_links WHERE artifact_key = ?", (i_key,)
          ).fetchall()
          for il in i_links:
            wid_i = str(il["work_id"])
            neg_p = conn.execute(
                "SELECT 1 FROM negative_links WHERE work_id = ? AND artifact_key = ?",
                (wid_i, p_key),
            ).fetchone()
            # Respect explicit user unlink tombstones on p_key
            if neg_p is not None:
              continue
            other_prim_cnt = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM work_links "
                    "WHERE work_id = ? AND role = 'primary' AND artifact_key != ?",
                    (wid_i, i_key),
                ).fetchone()["c"]
            )
            w_r = conn.execute(
                "SELECT title FROM works WHERE work_id = ?", (wid_i,)
            ).fetchone()
            w_t = str(w_r["title"] or "") if w_r else ""
            mail_rows = conn.execute(
                "SELECT a.title FROM work_links wl JOIN artifacts a "
                "ON a.artifact_key = wl.artifact_key "
                "WHERE wl.work_id = ? AND wl.role != 'context_only' "
                "AND (wl.artifact_key LIKE 'gmail_thread:%' "
                "OR wl.artifact_key LIKE 'gmail_draft:%' "
                "OR wl.artifact_key LIKE 'gchat_thread:%')",
                (wid_i,),
            ).fetchall()
            mail_blob = " ".join([w_t, *(str(mr["title"] or "") for mr in mail_rows)])
            # Re-link companion PRs, subject-referenced PRs, or sole-primary Draft/Email Works
            if is_companion_artifact(p_key, p_st, str(p_row.get("title") or "")):
              conn.execute(
                  "INSERT OR REPLACE INTO work_links "
                  "(work_id, artifact_key, role, source, confidence, created_at, "
                  "link_reason, link_detail) VALUES (?, ?, 'primary', 'auto', 1.0, ?, "
                  "'companion_wip', ?)",
                  (wid_i, p_key, cur_now, f"Companion PR #{num}"),
              )
            elif (
                re.search(rf"(?:PR\s*#|pull/|#){re.escape(num)}\b", mail_blob, re.IGNORECASE)
                or (il["role"] == "primary" and other_prim_cnt == 0)
            ):
              conn.execute(
                  "INSERT OR IGNORE INTO work_links "
                  "(work_id, artifact_key, role, source, confidence, created_at, "
                  "link_reason, link_detail) VALUES (?, ?, 'primary', 'auto', 1.0, ?, "
                  "'gmail_thread_ref', ?)",
                  (wid_i, p_key, cur_now, f"PR #{num} from correspondence subject"),
              )
          cur = conn.execute("DELETE FROM work_links WHERE artifact_key = ?", (i_key,))
          links_removed += int(cur.rowcount or 0)
          conn.execute("DELETE FROM candidate_buffer WHERE artifact_key = ?", (i_key,))
          conn.execute("DELETE FROM artifacts WHERE artifact_key = ?", (i_key,))
          art_rows.pop(i_key, None)
        elif p_is_ph and not i_is_ph:
          # Real GitHub issue with bug_title wins over an empty placeholder PR row
          cur = conn.execute("DELETE FROM work_links WHERE artifact_key = ?", (p_key,))
          links_removed += int(cur.rowcount or 0)
          conn.execute("DELETE FROM candidate_buffer WHERE artifact_key = ?", (p_key,))
          conn.execute("DELETE FROM artifacts WHERE artifact_key = ?", (p_key,))
          art_rows.pop(p_key, None)

      bad_art_keys: set[str] = set()
      for akey, ar in art_rows.items():
        st = _parse_art_state(ar)
        if is_config_artifact(akey, title=str(ar.get("title") or ""), state=st):
          bad_art_keys.add(akey)

      for r in conn.execute("SELECT DISTINCT artifact_key FROM work_links").fetchall():
        akey = str(r["artifact_key"])
        if akey not in bad_art_keys and is_config_artifact(akey):
          bad_art_keys.add(akey)

      for akey in bad_art_keys:
        cur = conn.execute("DELETE FROM work_links WHERE artifact_key = ?", (akey,))
        links_removed += int(cur.rowcount or 0)
        conn.execute("DELETE FROM candidate_buffer WHERE artifact_key = ?", (akey,))
        if akey.startswith(("chat_chain:", "roadmap_item:")):
          conn.execute("DELETE FROM artifacts WHERE artifact_key = ?", (akey,))
          art_rows.pop(akey, None)

      for r in conn.execute("SELECT DISTINCT chat_chain_id FROM candidate_buffer").fetchall():
        cid = str(r["chat_chain_id"] or "").strip()
        if cid and is_config_artifact(f"chat_chain:{cid}"):
          conn.execute("DELETE FROM candidate_buffer WHERE chat_chain_id = ?", (cid,))

      # Sort active non-companion Works ahead of done and companion-titled Works
      pre_work_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM works WHERE status != 'archived' ORDER BY updated_at DESC"
          ).fetchall()
      ]
      pre_work_rows.sort(
          key=lambda r: (
              1 if str(r.get("status") or "") == "done" else 0,
              1 if is_companion_title(str(r.get("title") or "")) else 0,
          )
      )
      # Map each primary non-companion CL/PR to its canonical owning Work
      primary_cl_owner: dict[str, str] = {}
      for w in pre_work_rows:
        wid = str(w["work_id"])
        for lr in conn.execute(
            "SELECT artifact_key, role, link_reason FROM work_links WHERE work_id = ?",
            (wid,),
        ).fetchall():
          ak = str(lr["artifact_key"])
          if lr["role"] != "primary" or not ak.startswith(("gerrit:", "github_pr:", "piper:")):
            continue
          ar = art_rows.get(ak) or {}
          if not is_companion_artifact(
              ak, _parse_art_state(ar), str(ar.get("title") or ""), str(lr["link_reason"] or "")
          ):
            primary_cl_owner.setdefault(ak, wid)

      # Reconcile split companion cards and companion-only Works onto their parent CL/PR
      for w in pre_work_rows:
        wid = str(w["work_id"])
        w_is_comp_titled = is_companion_title(str(w.get("title") or ""))
        w_links = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM work_links WHERE work_id = ? ORDER BY created_at ASC",
                (wid,),
            ).fetchall()
        ]
        if not w_links:
          continue
        non_comp_prim_cls: set[str] = set()
        comp_parents: list[tuple[str, str]] = []
        # Partition linked CLs/PRs into companion probes vs standalone primary changes
        for lr in w_links:
          ak = str(lr["artifact_key"])
          if not ak.startswith(("gerrit:", "github_pr:", "piper:")):
            continue
          ar = art_rows.get(ak) or {}
          st = _parse_art_state(ar)
          t_str = str(ar.get("title") or "")
          l_rsn = str(lr.get("link_reason") or "")
          if is_companion_artifact(ak, st, t_str, l_rsn):
            p_k = canonicalize_companion_parent(ak, st, t_str)
            if p_k:
              comp_parents.append((ak, p_k))
          elif lr["role"] == "primary":
            non_comp_prim_cls.add(ak)

        # Ensure companions sitting alongside their primary parent carry companion_wip provenance
        for comp_ak, p_k in comp_parents:
          if p_k in non_comp_prim_cls:
            conn.execute(
                "UPDATE work_links SET role = 'primary', link_reason = 'companion_wip' "
                "WHERE work_id = ? AND artifact_key = ? AND source != 'manual'",
                (wid, comp_ak),
            )

        # Merge duplicate companion-titled Works when another Work already owns their parent CL/PR
        merge_owner_wid = ""
        if comp_parents:
          for _, p_k in comp_parents:
            cand_owner = primary_cl_owner.get(p_k, "")
            if cand_owner and cand_owner != wid and (not non_comp_prim_cls or w_is_comp_titled):
              merge_owner_wid = cand_owner
              break
        elif w_is_comp_titled and len(non_comp_prim_cls) == 1:
          sole_p = next(iter(non_comp_prim_cls))
          cand_owner = primary_cl_owner.get(sole_p, "")
          if cand_owner and cand_owner != wid:
            merge_owner_wid = cand_owner

        # Transfer links and obligations to the real parent Work while respecting negative_links
        if merge_owner_wid:
          conn.execute(
              "INSERT OR IGNORE INTO work_links "
              "(work_id, artifact_key, role, source, confidence, created_at, "
              "link_reason, link_detail) "
              "SELECT ?, artifact_key, role, 'auto', confidence, ?, link_reason, link_detail "
              "FROM work_links WHERE work_id = ? "
              "AND artifact_key NOT IN ("
              "  SELECT artifact_key FROM negative_links WHERE work_id = ?"
              ")",
              (merge_owner_wid, cur_now, wid, merge_owner_wid),
          )
          for comp_ak, _ in comp_parents:
            conn.execute(
                "UPDATE work_links SET role = 'primary', link_reason = 'companion_wip' "
                "WHERE work_id = ? AND artifact_key = ? AND source != 'manual'",
                (merge_owner_wid, comp_ak),
            )
          conn.execute(
              "UPDATE obligations SET work_id = ? WHERE work_id = ?",
              (merge_owner_wid, wid),
          )
          ow_row = conn.execute(
              "SELECT meta_json FROM works WHERE work_id = ?", (merge_owner_wid,)
          ).fetchone()
          m_meta = _merge_reply_draft_meta(
              ow_row["meta_json"] if ow_row else "{}", w.get("meta_json") or "{}"
          )
          if m_meta is not None:
            conn.execute(
                "UPDATE works SET meta_json = ?, updated_at = ? WHERE work_id = ?",
                (json.dumps(m_meta), cur_now, merge_owner_wid),
            )
          conn.execute("DELETE FROM work_links WHERE work_id = ?", (wid,))
          conn.execute(
              "UPDATE works SET status = 'archived', updated_at = ? WHERE work_id = ?",
              (cur_now, wid),
          )
          works_archived += 1
          works_repaired += 1
          continue

        # Promote the missing parent CL/PR to primary when a Work only had a companion primary
        if non_comp_prim_cls or not comp_parents:
          continue
        comp_ak, p_k = comp_parents[0]
        if p_k not in art_rows:
          p_kind = artifact_kind_from_key(p_k)
          conn.execute(
              "INSERT OR IGNORE INTO artifacts "
              "(artifact_key, kind, title, url, state_json, updated_at) "
              "VALUES (?, ?, ?, '', '{}', ?)",
              (p_k, p_kind, p_k, cur_now),
          )
          art_rows[p_k] = {
              "artifact_key": p_k,
              "kind": p_kind,
              "title": p_k,
              "url": "",
              "state_json": "{}",
          }
        conn.execute(
            "INSERT OR REPLACE INTO work_links "
            "(work_id, artifact_key, role, source, confidence, created_at, "
            "link_reason, link_detail) VALUES (?, ?, 'primary', 'auto', 1.0, ?, "
            "'chat_topic', ?)",
            (wid, p_k, cur_now, f"Parent of companion {comp_ak.split(':')[-1]}"),
        )
        primary_cl_owner[p_k] = wid
        works_repaired += 1

      work_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM works WHERE status != 'archived'"
          ).fetchall()
      ]
      for w in work_rows:
        wid = str(w["work_id"])
        w_links = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM work_links WHERE work_id = ? ORDER BY created_at ASC",
                (wid,),
            ).fetchall()
        ]
        primaries = [l for l in w_links if l["role"] == "primary"]
        channels = [l for l in w_links if l["role"] == "channel"]
        w_title_str = str(w.get("title") or "")
        cfg_proj = is_config_project(str(w.get("project_id") or ""))
        cfg_title = is_config_text(w_title_str)
        comp_title = is_companion_title(w_title_str)
        try:
          w_meta = json.loads(w.get("meta_json") or "{}")
        except Exception:
          w_meta = {}
        has_active_rd = _active_reply_draft_from_meta(w_meta) is not None
        is_manual_work = (
            bool(w_meta.get("title_manual"))
            or has_active_rd
            or any(str(l.get("source") or "") == "manual" for l in w_links)
        )
        non_ctx_keys = [str(l["artifact_key"]) for l in primaries + channels]
        gmail_keys = [k for k in non_ctx_keys if k.startswith("gmail_thread:")]
        only_email_or_doc = bool(gmail_keys) and all(
            k.startswith(("gmail_thread:", "gdoc:", "gdoc_thread:")) for k in non_ctx_keys
        )
        ineligible_standalone_email = False
        if not is_manual_work and only_email_or_doc:
          ineligible_standalone_email = True
          if any(
              k.startswith("gdoc_thread:")
              or bool(_parse_art_state(art_rows.get(k)).get("is_active_correspondence"))
              for k in non_ctx_keys
          ):
            ineligible_standalone_email = False
          for gk in gmail_keys:
            if not ineligible_standalone_email:
              break
            st_g = _parse_art_state(art_rows.get(gk))
            if (
                st_g.get("sent_by_me")
                or st_g.get("is_github_issue_thread")
                or st_g.get("is_github_pr_thread")
                or st_g.get("is_gdoc_comment")
                or st_g.get("is_active_correspondence")
            ):
              ineligible_standalone_email = False
              break
            if st_g.get("tray_dismissed"):
              continue
            if int(st_g.get("schema_ver") or 0) >= 4 and (
                st_g.get("is_automated")
                or st_g.get("is_drive_share")
                or st_g.get("in_trash_or_spam")
                or not bool(st_g.get("in_inbox", True))
                or not bool(st_g.get("has_personal_category", True))
                or st_g.get("has_non_personal_category")
                or st_g.get("is_bulk_or_list")
                or not bool(st_g.get("me_addressed"))
                or not (1 <= int(st_g.get("recipient_count") or 0) <= 8)
            ):
              continue
            ineligible_standalone_email = False
            break
        if not has_active_rd and (
            (not primaries and not (is_manual_work and channels))
            or ((cfg_proj or cfg_title) and not channels)
            or ineligible_standalone_email
        ):
          conn.execute(
              "UPDATE works SET status = 'archived', updated_at = ? WHERE work_id = ?",
              (cur_now, wid),
          )
          conn.execute("DELETE FROM work_links WHERE work_id = ?", (wid,))
          works_archived += 1
          continue
        if cfg_proj or cfg_title or comp_title or is_placeholder_title(w_title_str):
          w_arts: list[dict[str, Any]] = []
          for wl in w_links:
            ak = str(wl["artifact_key"])
            ar = art_rows.get(ak) or {}
            st = _parse_art_state(ar)
            w_arts.append({
                "artifact_key": ak,
                "kind": str(ar.get("kind") or artifact_kind_from_key(ak)),
                "title": clean_work_title(str(ar.get("title") or ak)) or ak,
                "url": str(ar.get("url") or ""),
                "role": str(wl.get("role") or "primary"),
                "link_reason": str(wl.get("link_reason") or ""),
                "state": st,
            })
          new_proj = "" if cfg_proj else str(w.get("project_id") or "")
          new_title, _ = resolve_work_origin_and_title(w, w_arts)
          conn.execute(
              "UPDATE works SET project_id = ?, title = ?, updated_at = ? WHERE work_id = ?",
              (new_proj, new_title or w_title_str, cur_now, wid),
          )
          works_repaired += 1

      for w in work_rows:
        wid = str(w["work_id"])
        w_links = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM work_links WHERE work_id = ?",
                (wid,),
            ).fetchall()
        ]
        l_map = {str(r["artifact_key"]): r for r in w_links}
        if (
            "gerrit:skia:1408158" in l_map
            and l_map["gerrit:skia:1408158"].get("role") == "primary"
            and "github_issue:flutter/flutter:90063" in l_map
            and l_map["github_issue:flutter/flutter:90063"].get("role") == "primary"
        ):
          # Split flutter#90063 + Gerrit 1407016 + chats cfdcb811/9db15645 into their own Work
          split_wid = "work-90063-r42"
          conn.execute(
              "INSERT OR REPLACE INTO works "
              "(work_id, title, status, project_id, roadmap_item_key, created_at, updated_at, meta_json) "
              "VALUES (?, ?, 'active', '', '', ?, ?, '{}')",
              (
                  split_wid,
                  "1407016: [skparagraph] Keep line breaking of long words linear (flutter#90063)",
                  cur_now,
                  cur_now,
              ),
          )
          move_specs = [
              (
                  "github_issue:flutter/flutter:90063",
                  "primary",
                  "bug_trailer",
                  "Целевой баг из первого сообщения чата cfdcb811 и описания СЛ 1407016",
              ),
              (
                  "gerrit:skia:1407016",
                  "primary",
                  "bug_trailer",
                  "В описании СЛ 1407016 указан баг flutter#90063",
              ),
              (
                  "chat_chain:cfdcb811-6aca-4ae3-ba88-cbadbb3162a7",
                  "channel",
                  "chat_topic",
                  "Планировочный чат по багу flutter#90063 (R42)",
              ),
              (
                  "chat_chain:9db15645-df20-4126-aec2-c753c45eefe1",
                  "channel",
                  "cl_watcher_chat",
                  "Чат разработки СЛ 1407016 (flutter#90063)",
              ),
              (
                  "gerrit:skia:1409696",
                  "context_only",
                  "chat_mention",
                  "Попутно упомянут в чате cfdcb811 (шаг 33)",
              ),
          ]
          for ak, r_role, r_reason, r_detail in move_specs:
            if ak in l_map:
              conn.execute(
                  "DELETE FROM work_links WHERE work_id = ? AND artifact_key = ?",
                  (wid, ak),
              )
              conn.execute(
                  "INSERT OR REPLACE INTO work_links "
                  "(work_id, artifact_key, role, source, confidence, created_at, link_reason, link_detail) "
                  "VALUES (?, ?, ?, 'auto', 1.0, ?, ?, ?)",
                  (split_wid, ak, r_role, cur_now, r_reason, r_detail),
              )
          # Also record 1408158 on split_wid as a context_only mid-chat mention from step 339 of cfdcb811
          conn.execute(
              "INSERT OR REPLACE INTO work_links "
              "(work_id, artifact_key, role, source, confidence, created_at, link_reason, link_detail) "
              "VALUES (?, 'gerrit:skia:1408158', 'context_only', 'auto', 1.0, ?, 'chat_mention', ?)",
              (
                  split_wid,
                  cur_now,
                  "Попутно упомянут в чате cfdcb811 (шаг 339)",
              ),
          )
          conn.execute(
              "UPDATE work_links SET link_reason = 'chat_topic', "
              "link_detail = 'Целевой СЛ на ревью в чате b771ef3c (шаг 0)' "
              "WHERE work_id = ? AND artifact_key = 'gerrit:skia:1408158'",
              (wid,),
          )
          conn.execute(
              "UPDATE work_links SET link_reason = 'chat_topic', "
              "link_detail = 'Ревью-чат по СЛ 1408158' "
              "WHERE work_id = ? AND artifact_key = 'chat_chain:b771ef3c-b77f-4c91-a6ce-09209da6fd95'",
              (wid,),
          )
          works_repaired += 1

      works_repaired += self._demote_collateral_gh_issue_links(conn, art_rows, cur_now)

    return {
        "links_removed": links_removed,
        "works_archived": works_archived,
        "works_repaired": works_repaired,
    }

  def _demote_collateral_gh_issue_links(
      self,
      conn: sqlite3.Connection,
      art_rows: Mapping[str, Mapping[str, Any]],
      cur_now: float,
  ) -> int:
    """Demotes collateral body-cited github_issue links on GitHub issue notification Works."""
    repaired = 0
    work_ids = [
        str(r["work_id"])
        for r in conn.execute("SELECT work_id FROM works WHERE status != 'archived'").fetchall()
    ]
    for wid in work_ids:
      wl_rows = [
          dict(r)
          for r in conn.execute(
              "SELECT * FROM work_links WHERE work_id = ?", (wid,)
          ).fetchall()
      ]
      gh_issue_targets: set[str] = set()
      issue_thread_subjects: list[str] = []
      has_non_issue_mail_or_draft = False
      cl_bug_keys: set[str] = set()
      for wl in wl_rows:
        akey = str(wl["artifact_key"])
        role = str(wl.get("role") or "")
        ar = art_rows.get(akey) or {}
        try:
          st = json.loads(str(ar.get("state_json") or "{}"))
        except Exception:
          st = {}
        if not isinstance(st, Mapping):
          st = {}
        if akey.startswith("gmail_thread:") and role in ("primary", "channel"):
          gh_t = str(st.get("gh_target_key") or "") or extract_github_notif_target(
              str(st.get("root_subject") or ar.get("title") or "")
          )
          if gh_t.startswith("github_issue:") or bool(st.get("is_github_issue_thread")):
            if gh_t.startswith("github_issue:"):
              gh_issue_targets.add(gh_t)
            subj = str(st.get("root_subject") or "").strip().lower()
            if subj:
              issue_thread_subjects.append(subj)
          else:
            has_non_issue_mail_or_draft = True
        elif akey.startswith("gmail_draft:") and role in ("primary", "channel"):
          has_non_issue_mail_or_draft = True
        elif artifact_kind_from_key(akey) in ("gerrit", "github_pr", "piper"):
          cl_bug_keys.update(extract_cl_bug_keys(st))

      if not gh_issue_targets:
        continue
      for wl in wl_rows:
        akey = str(wl["artifact_key"])
        if not akey.startswith("github_issue:"):
          continue
        if (
            str(wl.get("role") or "") != "primary"
            or str(wl.get("source") or "") == "manual"
            or str(wl.get("link_reason") or "") != "gmail_thread_ref"
        ):
          continue
        if akey in gh_issue_targets or akey in cl_bug_keys:
          continue
        detail_low = str(wl.get("link_detail") or "").lower()
        from_issue_thread = any(
            s[:45] in detail_low for s in issue_thread_subjects if s
        )
        if not has_non_issue_mail_or_draft or from_issue_thread:
          conn.execute(
              "UPDATE work_links SET role = 'context_only' "
              "WHERE work_id = ? AND artifact_key = ?",
              (wid, akey),
          )
          repaired += 1
    if repaired:
      self._log_event(
          conn,
          "collateral_gh_issue_demoted",
          payload={"count": repaired},
          now=cur_now,
      )
    return repaired

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

    # Overlay live cl_watcher_state bug metadata in-memory so assignee/status stay fresh
    cl_bugs_raw = (cl_watcher_state or {}).get("bugs") or {}
    live_bugs_by_ckey: dict[str, tuple[str, str, str, dict[str, Any]]] = {}
    if isinstance(cl_bugs_raw, Mapping):
      for raw_bk, bval in cl_bugs_raw.items():
        if not isinstance(bval, Mapping):
          continue
        b_ck, b_kind, b_title, b_url = canonicalize_watcher_bug(str(raw_bk), bval)
        if not b_ck:
          continue
        b_st = dict(bval)
        b_desc = str(bval.get("title") or bval.get("summary") or "").strip()
        if b_desc and not b_st.get("bug_title"):
          b_st["bug_title"] = b_desc
        live_bugs_by_ckey[b_ck] = (b_kind, b_title, b_url, b_st)
        # Merge live tracker fields over cached SQLite state and upgrade placeholder titles
        existing_ar = art_rows.get(b_ck)
        if existing_ar is not None:
          prev_raw = existing_ar.get("state")
          prev_st = prev_raw if isinstance(prev_raw, Mapping) else {}
          existing_ar["state"] = {**prev_st, **b_st}
          if b_title and is_placeholder_title(str(existing_ar.get("title") or ""), b_ck):
            existing_ar["title"] = b_title
          if b_url and not existing_ar.get("url"):
            existing_ar["url"] = b_url
        else:
          art_rows[b_ck] = {
              "artifact_key": b_ck,
              "kind": b_kind,
              "title": b_title,
              "url": b_url,
              "state": b_st,
          }

    active_work_ids = {str(w["work_id"]) for w in work_rows}
    links_by_work: dict[str, list[dict[str, Any]]] = {}
    # Passing context_only mentions never suppress standalone virtual cards in cl_watcher_state
    bound_artifact_keys: set[str] = set()
    for lr in link_rows:
      wid = str(lr["work_id"])
      akey = str(lr["artifact_key"])
      if wid in active_work_ids and str(lr.get("role") or "") != "context_only":
        bound_artifact_keys.add(akey)
      links_by_work.setdefault(wid, []).append(lr)

    obs_by_work: dict[str, list[dict[str, Any]]] = {}
    for obr in ob_rows:
      wid = str(obr["work_id"])
      age_s = cur_now - float(obr.get("created_at") or cur_now)
      obr["stalled_alert"] = (
          obr.get("kind") != "correspondence"
          and obr.get("status") == "open"
          and obr.get("last_eval") in ("false", "unknown")
          and age_s >= STALLED_OBLIGATION_DAYS * 86400
      )
      obs_by_work.setdefault(wid, []).append(obr)

    cards: list[dict[str, Any]] = []
    title_updates: list[tuple[str, str]] = []
    for w in work_rows:
      wid = str(w["work_id"])
      raw_proj = str(w.get("project_id") or "")
      raw_w_title = str(w.get("title") or "")
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
        if is_config_artifact(
            akey,
            title=str(ar.get("title") or ""),
            state=ar.get("state") or {},
        ):
          continue
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
            "link_reason": str(wl.get("link_reason") or ""),
            "link_detail": str(wl.get("link_detail") or ""),
            "state": ar.get("state") or {},
        })
      for art_item in w_arts:
        why_badge, why_tooltip = compute_artifact_provenance(art_item, w_arts)
        art_item["why_badge"] = why_badge
        art_item["why_tooltip"] = why_tooltip
        if not art_item["link_reason"]:
          if art_item["role"] == "context_only":
            art_item["link_reason"] = "chat_mention"
          elif is_companion_artifact(
              str(art_item.get("artifact_key") or ""),
              art_item.get("state") if isinstance(art_item.get("state"), Mapping) else None,
              str(art_item.get("title") or ""),
          ):
            art_item["link_reason"] = "companion_wip"
          else:
            art_item["link_reason"] = "auto"
      # Order correspondence first, then parent CLs/bugs, then nested companions, then mentions
      def _is_corr_item(item_a: Mapping[str, Any]) -> bool:
        k_a = str(item_a.get("kind") or "")
        st_a = item_a.get("state") if isinstance(item_a.get("state"), Mapping) else None
        return k_a == "gmail_draft" or is_active_correspondence_artifact(k_a, st_a)

      w_arts.sort(
          key=lambda a: (
              0
              if (a.get("role") == "primary" and _is_corr_item(a))
              else (
                  1
                  if (a.get("role") != "context_only" and _is_corr_item(a))
                  else (
                      2
                      if (
                          a.get("role") != "context_only"
                          and not is_companion_artifact(
                              str(a.get("artifact_key") or ""),
                              a.get("state") if isinstance(a.get("state"), Mapping) else None,
                              str(a.get("title") or ""),
                              str(a.get("link_reason") or ""),
                          )
                      )
                      else (3 if a.get("role") != "context_only" else 4)
                  )
              )
          )
      )
      has_primary = any(a.get("role") == "primary" for a in w_arts)
      has_channel = any(a.get("role") == "channel" for a in w_arts)
      if (is_config_project(raw_proj) or is_config_text(raw_w_title)) and not (
          has_primary and has_channel
      ):
        continue
      if not w_arts and (is_config_project(raw_proj) or is_config_text(raw_w_title)):
        continue
      effective_proj = "" if is_config_project(raw_proj) else raw_proj
      active_rd = _active_reply_draft_from_meta(w.get("meta_json"))
      w_obs = obs_by_work.get(wid, [])
      checklist, prog_summary = build_work_checklist(
          w_arts, art_rows, reply_draft=active_rd
      )
      for ob in w_obs:
        try:
          ob_m = json.loads(ob.get("meta_json") or "{}")
        except Exception:
          ob_m = {}
        ob["wb_reply_draft"] = bool(
            ob_m.get("wb_reply_draft")
            or str(ob.get("obligation_id") or "").startswith("ob-wbdraft-")
        )
        if ob.get("status") == "done":
          continue
        if prog_summary:
          ob["progress_summary"] = prog_summary
        t_ob_key = str(ob.get("target_artifact_key") or "").strip()
        if t_ob_key and t_ob_key in art_rows:
          t_ar = art_rows[t_ob_key]
          t_st = t_ar.get("state") if isinstance(t_ar.get("state"), Mapping) else None
          # Expose inbound_reply vs silence_ping to the UI obligation badge renderer
          if ob.get("kind") == "correspondence" and ob.get("status") == "ready":
            ob["correspondence_mode"] = (
                "silence_ping"
                if _is_correspondence_silence_ping(t_st, art_rows)
                else "inbound_reply"
            )
          p_txt, ev_k = detect_borderline_correspondence_closure(
              str(t_ar.get("kind") or artifact_kind_from_key(t_ob_key)),
              t_st,
          )
          if p_txt and ev_k:
            ob["ambiguity_prompt"] = p_txt
            ob["ambiguity_event_key"] = ev_k
      # Resolve causal origin and upgrade downstream/placeholder work titles
      resolved_title, origin_info = resolve_work_origin_and_title(w, w_arts)
      if resolved_title and resolved_title != str(w["title"]):
        title_updates.append((resolved_title, wid))
      lane, reasons = self.compute_work_lane(w, w_arts, w_obs)
      card_reply_draft: dict[str, Any] | None = None
      if active_rd is not None:
        rd_upd = float(active_rd.get("updated_at") or cur_now)
        mat_key = str(active_rd.get("materialized_draft_key") or "")
        mat_ts = float(active_rd.get("materialized_at") or 0.0)
        mat_did = mat_key.split(":", 1)[1] if mat_key.startswith("gmail_draft:") else ""
        mat_st = ((art_rows.get(mat_key) or {}).get("state") or {}) if mat_key else {}
        mat_active = bool(mat_did) and str(mat_st.get("status") or "DRAFT").upper() == "DRAFT"
        card_reply_draft = {
            **active_rd,
            "updated_et": format_et_timestamp(rd_upd, now=cur_now),
            "materialized_url": (
                f"https://mail.google.com/mail/u/0/#drafts/{mat_did}" if mat_active else ""
            ),
            "needs_rematerialize": bool(mat_active and mat_ts > 0.0 and rd_upd > mat_ts + 1e-6),
        }
      # Expose real domain activity on updated_at too so legacy CLI/API callers see true recency
      activity_ts, origin_ts_val = compute_card_timestamps(w, w_arts, w_obs, card_reply_draft)
      has_urgent_ob = any(
          o.get("status") == "ready" or bool(o.get("stalled_alert"))
          for o in w_obs
          if o.get("status") != "done"
      )
      work_types, needs_you_types = _classify_card_types(
          lane, w_arts, w_obs, card_reply_draft
      )
      act_et = format_et_timestamp(activity_ts, now=cur_now)
      cards.append({
          "work_id": wid,
          "title": resolved_title or str(w["title"]),
          "origin": origin_info,
          "status": str(w["status"]),
          "lane": lane,
          "reasons": reasons,
          "project_id": effective_proj,
          "roadmap_item_key": str(w.get("roadmap_item_key") or ""),
          "updated_at": activity_ts,
          "updated_et": act_et,
          "activity_at": activity_ts,
          "activity_et": act_et,
          "origin_at": origin_ts_val,
          "origin_et": format_et_timestamp(origin_ts_val, now=cur_now),
          "has_urgent_obligation": has_urgent_ob,
          "is_virtual": False,
          "is_multi_channel": _compute_is_multi_channel(w_arts),
          "work_types": work_types,
          "needs_you_types": needs_you_types,
          "reply_draft": card_reply_draft,
          "checklist": checklist,
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
      # Group unbound companions under their virtual parent CL/PR instead of standalone cards
      non_comp_virtuals: dict[str, Mapping[str, Any]] = {}
      unbound_companions_by_parent: dict[str, list[tuple[str, Mapping[str, Any]]]] = {}
      for raw_key, item in cl_items.items():
        if not isinstance(item, Mapping):
          continue
        kind = str(item.get("kind") or "gerrit")
        tid = str(item.get("target_id") or raw_key)
        host = str(item.get("host") or "")
        c_key = canonicalize_artifact_key(kind, tid, host=host)
        subj_s = str(item.get("subject") or item.get("title") or "")
        if is_companion_artifact(c_key, item, subj_s) or item.get("is_cherry_pick"):
          if is_companion_artifact(c_key, item, subj_s) and c_key not in bound_artifact_keys:
            p_key = canonicalize_companion_parent(c_key, item, subj_s)
            if p_key and p_key not in bound_artifact_keys:
              unbound_companions_by_parent.setdefault(p_key, []).append((c_key, item))
          continue
        if c_key not in bound_artifact_keys:
          non_comp_virtuals[c_key] = item

      # Synthesize or hydrate aged-out parents referenced by unbound open companions
      for p_key, p_comps in unbound_companions_by_parent.items():
        if p_key not in non_comp_virtuals and p_key not in bound_artifact_keys:
          stored_p = art_rows.get(p_key) or {}
          stored_st = (
              dict(stored_p.get("state"))
              if isinstance(stored_p.get("state"), Mapping)
              else {}
          )
          if not stored_st.get("status") and any(
              bool(c_item.get("ready_to_abandon")) for _, c_item in p_comps
          ):
            stored_st["status"] = "CLOSED"
          stored_t = clean_work_title(str(stored_p.get("title") or ""))
          if not stored_st.get("subject") and not is_placeholder_title(stored_t, p_key):
            stored_st["subject"] = stored_t
          if not stored_st.get("url"):
            stored_st["url"] = str(stored_p.get("url") or "") or _default_artifact_url(p_key)
          stored_st.setdefault("is_external", True)
          non_comp_virtuals[p_key] = stored_st

      for c_key, item in non_comp_virtuals.items():
        status = str(item.get("status") or "NEW").upper()
        est = item.get("entity_state") if isinstance(item.get("entity_state"), Mapping) else {}
        attached_comps = unbound_companions_by_parent.get(c_key, [])
        orphan_reasons: list[str] = []
        comp_comment_reasons: list[str] = []
        comp_pills: list[dict[str, Any]] = []
        # Roll companion human comments and orphaned-companion cleanup up onto the virtual parent
        for comp_key, comp_item in attached_comps:
          o_r, c_r = _evaluate_companion_alerts(comp_key, comp_item, c_key, item)
          orphan_reasons.extend(o_r)
          comp_comment_reasons.extend(c_r)
          comp_pills.append({
              "artifact_key": comp_key,
              "kind": artifact_kind_from_key(comp_key),
              "icon": PILL_ICONS.get(artifact_kind_from_key(comp_key), "🔧"),
              "title": f"{comp_key.split(':')[-1]} · {comp_item.get('subject') or ''}".strip(" ·"),
              "url": str(comp_item.get("url") or ""),
              "role": "primary",
              "source": "virtual",
              "link_reason": "companion_wip",
              "why_badge": f"companion WIP к {c_key.split(':')[-1]}",
              "state": dict(comp_item),
          })

        comp_alerts = bool(orphan_reasons or comp_comment_reasons)
        # Keep terminal virtual parents visible only when a post-submit or companion alert is live
        if status in ("ABANDONED", "CLOSED") and not comp_alerts:
          continue
        if (
            status == "MERGED"
            and not (
                est.get("action_required")
                or est.get("lane") == "you"
                or est.get("is_post_submit_alert")
            )
            and not comp_alerts
        ):
          continue

        v_reasons: list[str] = [*orphan_reasons, *comp_comment_reasons]
        if status not in ("ABANDONED", "CLOSED"):
          cl_v_reason = _evaluate_cl_needs_you(c_key, item)
          if cl_v_reason:
            v_reasons.append(cl_v_reason)
        # Attach linked bug pills so unbound/hub virtual CLs retain assigned bugs in My Bugs
        linked_b_pills: list[dict[str, Any]] = []
        for bk_linked in extract_cl_bug_keys(item):
          bound_artifact_keys.add(bk_linked)
          b_ar = art_rows.get(bk_linked)
          if not b_ar:
            continue
          b_ar_st = b_ar.get("state") if isinstance(b_ar.get("state"), Mapping) else {}
          b_ar_kind = str(b_ar.get("kind") or artifact_kind_from_key(bk_linked))
          b_hot_r = _evaluate_cl_needs_you(bk_linked, b_ar_st)
          if b_hot_r:
            v_reasons.append(b_hot_r)
          linked_b_pills.append({
              "artifact_key": bk_linked,
              "kind": b_ar_kind,
              "icon": PILL_ICONS.get(b_ar_kind, "🐞"),
              "title": clean_work_title(str(b_ar.get("title") or bk_linked)) or bk_linked,
              "url": str(b_ar.get("url") or "") or _default_artifact_url(bk_linked),
              "role": "primary",
              "source": "virtual",
              "state": dict(b_ar_st),
          })
        lane = "needs-you" if v_reasons else "waiting"
        if not v_reasons:
          fallback_r = str(
              est.get("reason") or item.get("attention_reason") or status
          )
          if fallback_r:
            v_reasons.append(fallback_r)
        # Strip config project stamps from virtual cards when discussed in a config conversation
        raw_v_proj = str(item.get("project") or "")
        v_proj = "" if is_config_project(raw_v_proj) else raw_v_proj
        v_title = str(item.get("subject") or item.get("title") or format_short_cl_id(c_key, item))
        # Build virtual parent, linked bug, and companion pills so timestamps & types stay aligned
        v_arts = [
            {
                "artifact_key": c_key,
                "kind": artifact_kind_from_key(c_key),
                "icon": PILL_ICONS.get(artifact_kind_from_key(c_key), "🔧"),
                "title": f"{c_key.split(':')[-1]} · {item.get('subject') or ''}".strip(" ·"),
                "url": str(item.get("url") or ""),
                "role": "primary",
                "source": "virtual",
                "state": dict(item),
            },
            *linked_b_pills,
            *comp_pills,
        ]
        v_act_ts, v_orig_ts = compute_card_timestamps(None, v_arts, [], None)
        v_act_et = format_et_timestamp(v_act_ts, now=cur_now)
        v_work_types, v_needs_types = _classify_card_types(lane, v_arts, [], None)
        cards.append({
            "work_id": f"virt-{c_key}",
            "title": v_title,
            "status": "active",
            "lane": lane,
            "reasons": v_reasons,
            "project_id": v_proj,
            "roadmap_item_key": "",
            "updated_at": v_act_ts,
            "updated_et": v_act_et,
            "activity_at": v_act_ts,
            "activity_et": v_act_et,
            "origin_at": v_orig_ts,
            "origin_et": format_et_timestamp(v_orig_ts, now=cur_now),
            "has_urgent_obligation": False,
            "is_virtual": True,
            "is_multi_channel": _compute_is_multi_channel(v_arts),
            "work_types": v_work_types,
            "needs_you_types": v_needs_types,
            "artifacts": v_arts,
            "obligations": [],
        })

    # Project fresh unbound open bugs assigned to jlavrova / Rusino (excluding stale backlog)
    for b_ckey, (b_kind, _, b_url, b_st) in live_bugs_by_ckey.items():
      if b_ckey in bound_artifact_keys:
        continue
      if not is_bug_assigned_to_me(b_st):
        continue
      b_status = str(b_st.get("status") or "NEW").strip().upper()
      if b_status in CLOSED_BUG_STATUSES:
        continue
      b_est = b_st.get("entity_state") if isinstance(b_st.get("entity_state"), Mapping) else {}
      # Omit stale backlog tickets so My Bugs shows only active Works and fresh assigned bugs
      if bool(b_st.get("is_stale_backlog") or b_est.get("is_stale_backlog")):
        continue
      b_desc = str(b_st.get("title") or b_st.get("summary") or "").strip()
      short_b = format_short_bug_id(b_ckey)
      bug_hot_reason = _evaluate_cl_needs_you(b_ckey, b_st)
      b_lane = "needs-you" if bug_hot_reason else "waiting"
      if bug_hot_reason:
        b_reasons = [bug_hot_reason]
      else:
        has_prior_cl = b_st.get("lane") == "has_cl" or b_est.get("lane") == "has_cl"
        b_reasons = [
            "Assigned bug (prior CLs on bug)" if has_prior_cl else "Assigned bug (no CL yet)"
        ]
      b_arts = [{
          "artifact_key": b_ckey,
          "kind": b_kind,
          "icon": PILL_ICONS.get(b_kind, "🐞"),
          "title": f"{short_b} · {b_desc}".strip(" ·"),
          "url": b_url,
          "role": "primary",
          "source": "virtual",
          "why_badge": "назначен на тебя",
          "why_tooltip": "Открытый баг из трекера, назначенный на тебя",
          "state": dict(b_st),
      }]
      b_act_ts, b_orig_ts = compute_card_timestamps(None, b_arts, [], None)
      b_act_et = format_et_timestamp(b_act_ts, now=cur_now)
      cards.append({
          "work_id": f"virt-{b_ckey}",
          "title": b_desc or short_b,
          "status": "active",
          "lane": b_lane,
          "reasons": b_reasons,
          "project_id": "",
          "roadmap_item_key": "",
          "updated_at": b_act_ts,
          "updated_et": b_act_et,
          "activity_at": b_act_ts,
          "activity_et": b_act_et,
          "origin_at": b_orig_ts,
          "origin_et": format_et_timestamp(b_orig_ts, now=cur_now),
          "has_urgent_obligation": False,
          "is_virtual": True,
          "is_multi_channel": False,
          "work_types": ["my-bugs"],
          "needs_you_types": ["my-bugs"] if b_lane == "needs-you" else [],
          "artifacts": b_arts,
          "obligations": [],
      })

    # Rank by lane and urgent obligations first, pushing missing/zero timestamps after dated cards
    lane_rank = {"needs-you": 0, "waiting": 1, "blocked": 2, "done": 3}
    cards.sort(
        key=lambda c: (
            lane_rank.get(c["lane"], 9),
            0 if c.get("has_urgent_obligation") else 1,
            1 if float(c.get("activity_at") or c.get("updated_at") or 0.0) <= 0.0 else 0,
            -float(c.get("activity_at") or c.get("updated_at") or 0.0),
            str(c.get("work_id") or ""),
        )
    )
    # Exclude dismissed threads, active linked threads, and completed threads without newer replies
    dismissed_keys = self.list_dismissed_tray_keys()
    completed_thread_ts = self.list_completed_thread_timestamps()
    with self._lock, self._connect() as conn:
      active_linked_keys = {
          canonicalize_artifact_key(str(r["artifact_key"]))
          for r in conn.execute(
              "SELECT wl.artifact_key FROM work_links wl "
              "JOIN works w ON w.work_id = wl.work_id "
              "WHERE w.status NOT IN ('done', 'archived')"
          ).fetchall()
      }
    tray: list[dict[str, Any]] = []
    for t in unanswered_threads or []:
      t_akey = canonicalize_artifact_key(str(t.get("artifact_key") or ""))
      if t_akey in dismissed_keys or t_akey in active_linked_keys:
        continue
      done_ts = completed_thread_ts.get(t_akey)
      if done_ts is not None and float(t.get("last_message_ts") or 0.0) <= done_ts:
        continue
      tray.append(t)
    active_cards = [c for c in cards if c["lane"] != "done"]
    needs_cards = [c for c in cards if c["lane"] == "needs-you"]
    counts = {
        "all_active": len(active_cards),
        "needs_you": len(needs_cards),
        "waiting": sum(1 for c in cards if c["lane"] == "waiting"),
        "done": sum(1 for c in cards if c["lane"] == "done"),
        "multi_channel": sum(1 for c in active_cards if c.get("is_multi_channel")),
        "replies": sum(1 for c in active_cards if "replies" in (c.get("work_types") or ())),
        "reviews": sum(1 for c in active_cards if "reviews" in (c.get("work_types") or ())),
        "my_bugs": sum(1 for c in active_cards if "my-bugs" in (c.get("work_types") or ())),
        "my_cls": sum(1 for c in active_cards if "my-cls" in (c.get("work_types") or ())),
        "needs_reply": sum(
            1 for c in needs_cards if "replies" in (c.get("needs_you_types") or ())
        ),
        "needs_review": sum(
            1 for c in needs_cards if "reviews" in (c.get("needs_you_types") or ())
        ),
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
        "synced_et": format_et_timestamp(cur_now, now=cur_now),
    }
