#!/usr/bin/env python3
"""Quota-exhaustion hold queue and post-reset auto-resume manager (R132).

Holds non-triage conversations and background Flight Deck wakeups/auto-starts when
all allowed senior models in a project profile drop below the 5% emergency floor
(or hit 0% / mid-turn QUOTA_EXHAUSTED), and resumes them on a senior model after
the quota reset window passes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import argparse
import fcntl
import json
import os
import re
import sys
from typing import Any, Callable

import chat_lifecycle as cl

DEFAULT_QUEUE_PATH = os.path.expanduser("~/.gemini/jetski/quota_hold_queue.json")
QUEUE_PATH_ENV = "JETSKI_QUOTA_HOLD_QUEUE_FILE"
# Require at least 15% senior quota after resetTime before auto-resuming held work
QUOTA_RESUME_FLOOR = cl.QUOTA_HARD_FLOOR
# Default continuation prompt when resuming a chat halted mid-turn or at PreInvocation
DEFAULT_RESUME_PROMPT = (
    "Квота старшей модели восстановилась после сброса — продолжай выполнение "
    "задачи с того шага, на котором остановилась."
)


# Resolves the on-disk path for the quota hold queue JSON file
def get_queue_path(queue_path: str = "") -> str:
  if queue_path:
    return os.path.expanduser(queue_path)
  env_path = os.environ.get(QUEUE_PATH_ENV, "").strip()
  if env_path:
    return os.path.expanduser(env_path)
  return DEFAULT_QUEUE_PATH


def _format_reset_et(reset_ts: float, reset_iso: str = "") -> str:
  if reset_ts > 0:
    try:
      from zoneinfo import ZoneInfo

      dt = datetime.fromtimestamp(reset_ts, tz=ZoneInfo(cl.DEFAULT_TIMEZONE))
      return dt.strftime("%Y-%m-%d %H:%M %Z")
    except Exception:  # lint: allow-broad-except (timezone formatting fallback)
      pass
  return reset_iso or "следующего окна сброса квот"


# Computes senior quota availability and earliest reset timestamp for a project profile
def get_profile_quota_state(
    models: dict[str, Any] | None,
    profile: str,
    current_model: str = "",
    now_ts: float | None = None,
) -> dict[str, Any]:
  clean_profile = (profile or "orchestrator").strip().lower().removeprefix("profile:")
  if clean_profile not in cl.MODEL_PROFILES:
    clean_profile = "orchestrator"
  now = cl._now_ts() if now_ts is None else float(now_ts)

  if clean_profile == "triage" or not isinstance(models, dict) or not models:
    return {
        "profile": clean_profile,
        "found_any": False,
        "max_quota": 1.0,
        "below_emergency_floor": False,
        "all_zero": False,
        "best_model": {},
        "reset_ts": 0.0,
        "reset_time": "",
        "reset_display": "",
    }

  chain = cl.MODEL_PROFILES[clean_profile]
  hits: list[dict[str, Any]] = []
  reset_candidates: list[tuple[float, str]] = []

  for mid in chain:
    hit = cl.lookup_model(models, mid)
    if not hit:
      continue
    raw_det = models.get(hit["id"]) or {}
    # lookup_model reads the wall clock; quota and reset candidates must use the same `now`
    hit = {**hit, "quota": cl.get_model_quota_fraction(raw_det, now_ts=now)}
    hits.append(hit)
    qinfo = raw_det.get("quotaInfo") if isinstance(raw_det, dict) else {}
    r_iso = str((qinfo or {}).get("resetTime") or "").strip()
    r_ts = cl.parse_iso_reset_ts(r_iso) if r_iso else 0.0
    if r_ts > now:
      reset_candidates.append((r_ts, r_iso))

  if current_model:
    cur_hit = cl.lookup_model(models, current_model)
    if cur_hit:
      raw_det = models.get(cur_hit["id"]) or {}
      qinfo = raw_det.get("quotaInfo") if isinstance(raw_det, dict) else {}
      r_iso = str((qinfo or {}).get("resetTime") or "").strip()
      r_ts = cl.parse_iso_reset_ts(r_iso) if r_iso else 0.0
      if r_ts > now:
        reset_candidates.append((r_ts, r_iso))

  if not hits:
    return {
        "profile": clean_profile,
        "found_any": False,
        "max_quota": 1.0,
        "below_emergency_floor": False,
        "all_zero": False,
        "best_model": {},
        "reset_ts": 0.0,
        "reset_time": "",
        "reset_display": "",
    }

  max_quota = max(float(h.get("quota", 0.0)) for h in hits)
  best = cl.resolve_profile_model(models, clean_profile)
  reset_candidates.sort(key=lambda x: x[0])
  earliest_ts, earliest_iso = reset_candidates[0] if reset_candidates else (0.0, "")

  return {
      "profile": clean_profile,
      "found_any": True,
      "max_quota": max_quota,
      "below_emergency_floor": max_quota < cl.EMERGENCY_PIN_FLOOR,
      "all_zero": max_quota <= 0.0,
      "best_model": best,
      "reset_ts": earliest_ts,
      "reset_time": earliest_iso,
      "reset_display": _format_reset_et(earliest_ts, earliest_iso),
  }


# Resolves the effective model profile ('cpp-core', 'orchestrator', or 'triage') for a chat or project
def resolve_conversation_profile(
    conv_id: str = "", project_id: str = ""
) -> str:
  sidecar_id = cl.get_conversation_sidecar_id(conv_id) if conv_id else ""
  if sidecar_id:
    sc_pref = cl.get_project_model_preference(sidecar_id).strip().lower().removeprefix("profile:")
    if sc_pref in cl.MODEL_PROFILES:
      return sc_pref
  pid = project_id or (cl.get_conversation_project_id(conv_id) if conv_id else "")
  if pid and pid != "outside-of-project":
    pref = cl.get_project_model_preference(pid).strip().lower().removeprefix("profile:")
    if pref in cl.MODEL_PROFILES:
      return pref
  return "orchestrator"


def _with_queue_lock(queue_path: str, fn: Callable[[dict[str, Any]], Any]) -> Any:
  path = get_queue_path(queue_path)
  os.makedirs(os.path.dirname(path), exist_ok=True)
  lock_path = f"{path}.lock"
  with open(lock_path, "a+", encoding="utf-8") as lock_f:
    fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
    try:
      data: dict[str, Any] = {"items": {}, "exemptions": {}, "last_hold_ts": {}}
      if os.path.isfile(path):
        try:
          with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
          if isinstance(loaded, dict):
            items = loaded.get("items") if isinstance(loaded.get("items"), dict) else {}
            exemptions = (
                loaded.get("exemptions")
                if isinstance(loaded.get("exemptions"), dict)
                else {}
            )
            last_hold = (
                loaded.get("last_hold_ts")
                if isinstance(loaded.get("last_hold_ts"), dict)
                else {}
            )
            data = {
                **loaded,
                "items": items,
                "exemptions": exemptions,
                "last_hold_ts": last_hold,
            }
        except (OSError, ValueError):
          data = {"items": {}, "exemptions": {}, "last_hold_ts": {}}
      res, dirty = fn(data)
      if dirty:
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
          json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
      return res
    finally:
      fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


# Reads the raw top-level JSON dictionary from the quota-hold queue file
def _read_queue_doc(queue_path: str = "") -> dict[str, Any]:
  path = get_queue_path(queue_path)
  if not os.path.isfile(path):
    return {}
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    return data if isinstance(data, dict) else {}
  except (OSError, ValueError):
    return {}


# Reads a snapshot of all currently held items from the queue file
def load_hold_queue(queue_path: str = "") -> dict[str, dict[str, Any]]:
  items = _read_queue_doc(queue_path).get("items")
  return dict(items) if isinstance(items, dict) else {}


def load_exemptions(queue_path: str = "") -> dict[str, dict[str, Any]]:
  """Reads per-conversation quota-hold exemptions from the queue file.

  Inputs: optional queue_path override.
  Outputs: mapping of conversation_id -> exemption metadata dict.
  Side effects: reads queue JSON from disk without modifying it.
  """
  exemptions = _read_queue_doc(queue_path).get("exemptions")
  return dict(exemptions) if isinstance(exemptions, dict) else {}


def is_conversation_exempt(
    conv_id: str,
    *,
    queue_path: str = "",
    check_chain: bool = True,
) -> bool:
  """Checks whether a conversation or one of its handoff ancestors is exempt from quota hold.

  Inputs: conv_id, optional queue_path, and check_chain flag for ancestor inheritance.
  Outputs: True when exempted, False otherwise.
  Side effects: reads exemptions map and ancestor chain transcripts when exemptions exist.
  """
  cid = (conv_id or "").strip()
  if not cid:
    return False
  exemptions = load_exemptions(queue_path)
  if not exemptions:
    return False
  if cid in exemptions:
    return True
  if check_chain:
    try:
      for anc in cl.discover_conversation_chain(cid, include_descendants=False):
        if anc in exemptions:
          return True
    except Exception:  # lint: allow-broad-except (chain lookup must never crash guard)
      pass
  return False


def unblock_conversation(
    conv_id: str,
    *,
    queue_path: str = "",
    clear_waiting: bool = True,
) -> bool:
  """Removes a conversation from the quota-hold queue and restores its ⦿ title marker.

  Inputs: conv_id, optional queue_path, and clear_waiting flag.
  Outputs: True if a queued hold entry was removed, False otherwise.
  Side effects: updates queue JSON under lock and clears ⏳ wait state via chat_lifecycle.
  """
  cid = (conv_id or "").strip()
  if not cid:
    return False
  hold_id = f"conv:{cid}"

  def _remove(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
    items = data.get("items") or {}
    removed = items.pop(hold_id, None)
    # Keep created_ts when popping so UI Model Selection overrides survive unblock_conversation
    if isinstance(removed, dict) and float(removed.get("created_ts") or 0.0) > 0.0:
      data.setdefault("last_hold_ts", {})[cid] = float(removed["created_ts"])
    return (dict(removed) if isinstance(removed, dict) else None), removed is not None

  removed_entry = _with_queue_lock(queue_path, _remove)
  if clear_waiting and (removed_entry is not None):
    try:
      cl.set_conversation_wait_state(cid, waiting=False)
    except Exception:  # lint: allow-broad-except (title restore is best-effort)
      pass
  return removed_entry is not None


def exempt_conversation(
    conv_id: str,
    *,
    reason: str = "manual_exemption",
    queue_path: str = "",
    clear_waiting: bool = True,
) -> dict[str, Any]:
  """Marks a conversation exempt from quota hold, unqueues its chain, and clears ⏳.

  Inputs: conv_id, human/machine reason, optional queue_path, and clear_waiting flag.
  Outputs: stored exemption record dict.
  Side effects: writes exemptions + removes held items in queue JSON and restores ⦿ title state.
  """
  cid = (conv_id or "").strip()
  if not cid:
    raise ValueError("conversation_id is required")
  now = cl._now_ts()
  now_iso = datetime.now(timezone.utc).isoformat()
  chain_ids = [cid]
  try:
    for rel_cid in cl.discover_conversation_chain(cid, include_descendants=True):
      if rel_cid and rel_cid not in chain_ids:
        chain_ids.append(rel_cid)
  except Exception:  # lint: allow-broad-except (chain lookup is best-effort)
    pass

  def _mutate(data: dict[str, Any]) -> tuple[tuple[dict[str, Any], list[str]], bool]:
    exemptions = data.setdefault("exemptions", {})
    items = data.setdefault("items", {})
    prev = exemptions.get(cid) if isinstance(exemptions.get(cid), dict) else {}
    rec = {
        "conversation_id": cid,
        "reason": (reason or str(prev.get("reason") or "manual_exemption")).strip(),
        "created_at": str(prev.get("created_at") or now_iso),
        "created_ts": float(prev.get("created_ts") or now),
        "updated_at": now_iso,
    }
    exemptions[cid] = rec
    wait_cids = [cid]
    for target_cid in chain_ids:
      if items.pop(f"conv:{target_cid}", None) is not None and target_cid not in wait_cids:
        wait_cids.append(target_cid)
    return (dict(rec), wait_cids), True

  rec, wait_cids = _with_queue_lock(queue_path, _mutate)
  if clear_waiting:
    for w_cid in wait_cids:
      try:
        cl.set_conversation_wait_state(w_cid, waiting=False)
      except Exception:  # lint: allow-broad-except (title restore is best-effort)
        pass
  return rec


def unexempt_conversation(conv_id: str, *, queue_path: str = "") -> bool:
  """Removes an explicit quota-hold exemption for conv_id.

  Inputs: conv_id and optional queue_path.
  Outputs: True if an exemption existed and was removed, False otherwise.
  Side effects: updates queue JSON under lock.
  """
  cid = (conv_id or "").strip()
  if not cid:
    return False

  def _mutate(data: dict[str, Any]) -> tuple[bool, bool]:
    exemptions = data.get("exemptions") or {}
    removed = exemptions.pop(cid, None)
    return removed is not None, removed is not None

  return bool(_with_queue_lock(queue_path, _mutate))


_USER_REQUEST_BLOCK_RE = re.compile(
    r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.DOTALL
)
_USER_SETTINGS_BLOCK_RE = re.compile(
    r"<USER_SETTINGS_CHANGE>\s*(.*?)\s*</USER_SETTINGS_CHANGE>", re.DOTALL
)
_MODEL_SELECTION_CHANGE_RE = re.compile(
    r"The user changed setting `Model Selection` from ([^\n`]+?) to "
    r"([^\n`]+?)(?:\.(?=\s|$)|\n|$)"
)
_EXPLICIT_JUNIOR_CONFIRM_RU_RE = re.compile(
    r"(?:"
    r"(?:можешь|можно|давай|сделай|доделай|выполни|продолж\w*|запусти\w*|работай)\b"
    r"[^\n]{0,80}\bна\s+(?:младш|слаб|лёгк|легк|прост)\w*\s+модел"
    r"|\bна\s+(?:младш|слаб|лёгк|легк)\w*\s+модел\w*[^\n?]{0,40}"
    r"\b(?:сделать|доделать|выполнить|продолжить|достаточно|хватит)\b"
    r"|(?:сделай|включи|добавь)\s+исключение\s+для\s+этого\s+чата"
    r"|разблокируй\s+этот\s+чат"
    r")",
    re.IGNORECASE,
)
_EXPLICIT_JUNIOR_CONFIRM_EN_RE = re.compile(
    r"(?:"
    r"\b(?:do|finish|run|continue|complete|work|can\s+do)\b[^\n]{0,60}"
    r"\bon\s+(?:a\s+|the\s+)?(?:junior|lighter|smaller|weaker|flash)\s+model\b"
    r"|\b(?:bypass|override|ignore)\s+quota[\s_-]*hold\b"
    r")",
    re.IGNORECASE,
)


# Resolves transcript.jsonl for conv_id from an explicit path or Antigravity brain dirs
def _resolve_transcript_path(conv_id: str, transcript_path: str = "") -> str:
  if transcript_path and os.path.isfile(transcript_path):
    return transcript_path
  cid = (conv_id or "").strip()
  if not cid:
    return ""
  for d in cl.find_app_data_dirs():
    cand = os.path.join(d, "brain", cid, ".system_generated", "logs", "transcript.jsonl")
    if os.path.isfile(cand):
      return cand
  return ""


# Walks step-0 handoff parent pointers so user overrides persist across continuation pieces
def _chain_transcripts(conv_id: str, transcript_path: str = "") -> list[tuple[str, str]]:
  tpath = _resolve_transcript_path(conv_id, transcript_path)
  if not tpath:
    return []
  chain: list[tuple[str, str]] = [((conv_id or "").strip(), tpath)]
  seen = {(conv_id or "").strip()}
  cur_path = tpath
  for _ in range(16):
    try:
      with open(cur_path, "r", encoding="utf-8", errors="replace") as f:
        first_line = f.readline().strip()
      first_obj = json.loads(first_line) if first_line else {}
    except (OSError, ValueError):
      break
    if not isinstance(first_obj, dict) or first_obj.get("type") != "USER_INPUT":
      break
    m = cl.HANDOFF_PARENT_RE.search(str(first_obj.get("content") or ""))
    parent_cid = m.group(1).strip() if m else ""
    if not parent_cid or parent_cid in seen:
      break
    seen.add(parent_cid)
    # Check sibling brain dir next to cur_path before scanning global app_data_dirs
    brain_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(cur_path))))
    sib = os.path.join(
        brain_root, parent_cid, ".system_generated", "logs", "transcript.jsonl"
    )
    parent_tpath = sib if os.path.isfile(sib) else _resolve_transcript_path(parent_cid)
    if not parent_tpath:
      break
    chain.append((parent_cid, parent_tpath))
    cur_path = parent_tpath
  chain.reverse()
  return chain


# Loads deduplicated transcript steps ordered by step_index up to current_step_idx
def _iter_transcript_steps(
    transcript_path: str, current_step_idx: int = -1
) -> list[dict[str, Any]]:
  if not transcript_path or not os.path.isfile(transcript_path):
    return []
  steps_by_idx: dict[int, dict[str, Any]] = {}
  fallback_idx = 0
  try:
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
      for raw in f:
        raw = raw.strip()
        if not raw:
          continue
        try:
          obj = json.loads(raw)
        except ValueError:
          continue
        if not isinstance(obj, dict):
          continue
        raw_idx = obj.get("step_index")
        idx = int(raw_idx) if raw_idx is not None else fallback_idx
        fallback_idx = max(fallback_idx, idx + 1)
        steps_by_idx[idx] = obj
  except OSError:
    return []
  return [
      steps_by_idx[i]
      for i in sorted(steps_by_idx)
      if current_step_idx < 0 or i < current_step_idx
  ]


def is_orchestrator_execution_turn(
    transcript_path: str,
    *,
    conv_id: str = "",
    current_step_idx: int = -1,
) -> bool:
  """Detects the two-phase Orchestrator execution phase after exit_plan_mode.

  Inputs: transcript_path (or conv_id fallback) and optional current_step_idx upper bound.
  Outputs: True while executing an approved plan before enter_plan_mode or the next USER_INPUT.
  Side effects: reads transcript JSONL from disk.
  """
  tpath = _resolve_transcript_path(conv_id, transcript_path)
  steps = _iter_transcript_steps(tpath, current_step_idx=current_step_idx)
  if not steps:
    return False
  in_exec = False
  for step in steps:
    stype = str(step.get("type") or "")
    content = str(step.get("content") or "")
    # Synthetic background notifications use <SYSTEM_MESSAGE> and do not end plan execution
    if stype == "USER_INPUT" and "<SYSTEM_MESSAGE>" not in content:
      in_exec = False
      continue
    if stype == "PLANNER_RESPONSE":
      for tc in step.get("tool_calls") or []:
        if not isinstance(tc, dict):
          continue
        tname = str(tc.get("name") or "")
        if tname == "exit_plan_mode":
          in_exec = True
        elif tname == "enter_plan_mode":
          in_exec = False
    elif stype == "SYSTEM_MESSAGE":
      if "Plan mode is off" in content:
        in_exec = True
      elif "[plan review]" in content or "Plan mode is on" in content:
        in_exec = False
  return in_exec


def _load_last_hold_ts(queue_path: str = "") -> dict[str, float]:
  raw = _read_queue_doc(queue_path).get("last_hold_ts")
  if not isinstance(raw, dict):
    return {}
  return {str(k): float(v) for k, v in raw.items() if isinstance(v, (int, float))}


def has_user_quota_hold_override(
    conv_id: str,
    *,
    transcript_path: str = "",
    models: dict[str, Any] | None = None,
    queue_path: str = "",
    current_step_idx: int = -1,
    now_ts: float | None = None,
) -> bool:
  """Checks if the user switched UI Model Selection to an available model or confirmed junior work.

  Inputs: conv_id, optional transcript_path, models catalog, queue_path, step bound, and now_ts.
  Outputs: True when user manually overrode quota hold after hold activation or in message text.
  Side effects: reads transcript JSONL and hold queue snapshot; queries model catalog on UI change.
  """
  cid = (conv_id or "").strip()
  chain = _chain_transcripts(cid, transcript_path)
  if not chain:
    return False

  qdoc = _read_queue_doc(queue_path)
  queued = dict(qdoc["items"]) if isinstance(qdoc.get("items"), dict) else {}
  raw_last = qdoc.get("last_hold_ts")
  last_hold_map = (
      {str(k): float(v) for k, v in raw_last.items() if isinstance(v, (int, float))}
      if isinstance(raw_last, dict)
      else {}
  )
  now = cl._now_ts() if now_ts is None else float(now_ts)
  avail_models: dict[str, Any] | None = models if isinstance(models, dict) else None

  hold_seen = False
  overrode = False
  for idx_in_chain, (piece_cid, piece_tpath) in enumerate(chain):
    is_leaf = idx_in_chain == len(chain) - 1
    steps = _iter_transcript_steps(
        piece_tpath, current_step_idx=(current_step_idx if is_leaf else -1)
    )
    queued_entry = queued.get(f"conv:{piece_cid}") if piece_cid else None
    queued_ts = (
        float(queued_entry.get("created_ts") or 0.0)
        if isinstance(queued_entry, dict)
        else float(last_hold_map.get(piece_cid) or 0.0)
    )
    has_hold_record = isinstance(queued_entry, dict) or queued_ts > 0.0

    for step in steps:
      stype = str(step.get("type") or "")
      content = str(step.get("content") or "")
      if (stype == "EPHEMERAL_MESSAGE" and "[QUOTA HOLD GUARD]" in content) or (
          stype in ("GENERIC", "TOOL_RESULT")
          and "[QUOTA HOLD GUARD] Blocked " in content
          and "Showing lines " not in content
      ):
        hold_seen = True
        continue
      if stype != "USER_INPUT" or "<SYSTEM_MESSAGE>" in content:
        continue

      req_match = _USER_REQUEST_BLOCK_RE.search(content)
      req_text = req_match.group(1) if req_match else content
      if _EXPLICIT_JUNIOR_CONFIRM_RU_RE.search(req_text) or _EXPLICIT_JUNIOR_CONFIRM_EN_RE.search(
          req_text
      ):
        overrode = True

      step_ts = cl.parse_iso_reset_ts(step.get("created_at"))
      after_queued_hold = has_hold_record and (
          step_ts >= queued_ts
          if (step_ts > 0.0 and queued_ts > 0.0)
          else isinstance(queued_entry, dict)
      )

      for settings_match in _USER_SETTINGS_BLOCK_RE.finditer(content):
        for sel_match in _MODEL_SELECTION_CHANGE_RE.finditer(settings_match.group(1)):
          from_model = sel_match.group(1).strip()
          to_model = sel_match.group(2).strip()
          # Initial UI hydration emits `from None to <Model>` unless a hold already ran
          if from_model.lower() == "none" and not (hold_seen or after_queued_hold):
            continue
          if avail_models is None:
            avail_models = (cl.get_available_models() or {}).get("models") or {}
          hit = cl.lookup_model(avail_models, to_model) if avail_models else {}
          if hit:
            raw_det = avail_models.get(hit["id"]) or {}
            quota = cl.get_model_quota_fraction(raw_det, now_ts=now)
            overrode = quota > 0.0
          elif to_model.lower() == "none":
            overrode = False
  return overrode


# Adds or updates a quota-hold entry and marks any bound conversation title with ⏳
def enqueue_quota_hold(
    *,
    kind: str,
    profile: str,
    conversation_id: str = "",
    item_key: str = "",
    message: str = "",
    model_enum: str = "",
    model_id: str = "",
    reset_ts: float = 0.0,
    reset_time: str = "",
    queue_path: str = "",
    mark_waiting: bool = True,
) -> dict[str, Any]:
  cid = (conversation_id or "").strip()
  if cid and is_conversation_exempt(cid, queue_path=queue_path):
    return {}
  ikey = (item_key or "").strip()
  clean_kind = (kind or "wake").strip()
  hold_id = f"conv:{cid}" if cid else f"{clean_kind}:{ikey or 'unknown'}"
  now = cl._now_ts()
  now_iso = datetime.now(timezone.utc).isoformat()

  def _mutate(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    items = data.setdefault("items", {})
    prev = items.get(hold_id) if isinstance(items.get(hold_id), dict) else {}
    # Preserve an explicit wakeup prompt over a generic continuation prompt
    prev_msg = str(prev.get("message") or "").strip()
    new_msg = (message or "").strip()
    if new_msg and new_msg != DEFAULT_RESUME_PROMPT:
      eff_msg = new_msg
    elif prev_msg:
      eff_msg = prev_msg
    else:
      eff_msg = new_msg or DEFAULT_RESUME_PROMPT

    entry = {
        "id": hold_id,
        "kind": clean_kind if not prev else str(prev.get("kind") or clean_kind),
        "conversation_id": cid,
        "item_key": ikey or str(prev.get("item_key") or ""),
        "profile": (profile or str(prev.get("profile") or "orchestrator"))
        .strip()
        .lower()
        .removeprefix("profile:"),
        "model_enum": model_enum or str(prev.get("model_enum") or ""),
        "model_id": model_id or str(prev.get("model_id") or ""),
        "message": eff_msg,
        "reset_ts": float(reset_ts or prev.get("reset_ts") or 0.0),
        "reset_time": reset_time or str(prev.get("reset_time") or ""),
        "created_at": str(prev.get("created_at") or now_iso),
        "created_ts": float(prev.get("created_ts") or now),
        "updated_at": now_iso,
        "title_waiting_set": bool(prev.get("title_waiting_set", False)),
    }
    items[hold_id] = entry
    return dict(entry), True

  entry = _with_queue_lock(queue_path, _mutate)
  if cid and mark_waiting and not entry.get("title_waiting_set"):
    try:
      if cl.set_conversation_wait_state(cid, waiting=True):
        def _mark_set(data: dict[str, Any]) -> tuple[None, bool]:
          rec = (data.get("items") or {}).get(hold_id)
          if isinstance(rec, dict):
            rec["title_waiting_set"] = True
            return None, True
          return None, False

        _with_queue_lock(queue_path, _mark_set)
        entry["title_waiting_set"] = True
    except Exception:  # lint: allow-broad-except (title update is best-effort)
      pass
  return entry


# Checks live trajectory summaries for mid-turn Cortex QUOTA_EXHAUSTED halts
def scan_and_enqueue_halted_chats(
    summaries: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
    queue_path: str = "",
) -> list[str]:
  enqueued: list[str] = []
  avail_models = (
      models
      if isinstance(models, dict)
      else (cl.get_available_models().get("models") or {})
  )
  if isinstance(summaries, dict):
    for cid, s in summaries.items():
      if not isinstance(s, dict):
        continue
      ann = s.get("annotations") or {}
      if isinstance(ann, dict) and str(ann.get("archived") or "").lower() == "true":
        continue
      term = str(s.get("latestExecutorTerminationReason") or "")
      status = str(s.get("status") or "")
      if "QUOTA_EXHAUSTED" in term or "QUOTA_EXHAUSTED" in status:
        if is_conversation_exempt(cid, queue_path=queue_path):
          continue
        if has_user_quota_hold_override(cid, models=avail_models, queue_path=queue_path):
          continue
        prof = resolve_conversation_profile(cid)
        if prof == "triage":
          continue
        qstate = get_profile_quota_state(avail_models, prof)
        added = enqueue_quota_hold(
            kind="mid_turn_halt",
            profile=prof,
            conversation_id=cid,
            message=DEFAULT_RESUME_PROMPT,
            reset_ts=qstate["reset_ts"],
            reset_time=qstate["reset_time"],
            queue_path=queue_path,
        )
        if added:
          enqueued.append(cid)
  return enqueued


# Checks whether a held queue entry is eligible to resume given current catalog quotas and clock
def is_hold_ready_to_resume(
    entry: dict[str, Any],
    models: dict[str, Any],
    now_ts: float | None = None,
) -> tuple[bool, dict[str, Any]]:
  if not isinstance(entry, dict) or not isinstance(models, dict) or not models:
    return False, {}
  now = cl._now_ts() if now_ts is None else float(now_ts)
  prof = str(entry.get("profile") or "orchestrator").strip().lower().removeprefix("profile:")
  best = cl.resolve_profile_model(models, prof)
  if not best:
    return False, {}
  best_quota = float(best.get("quota", 0.0))
  reset_ts = float(entry.get("reset_ts") or 0.0)
  # Resume after reset_ts when senior quota >= 15%, or immediately if a senior pool recovered >= 30%
  if best_quota >= cl.QUOTA_SOFT_FLOOR:
    return True, best
  if reset_ts > 0 and now >= reset_ts and best_quota >= QUOTA_RESUME_FLOOR:
    return True, best
  if reset_ts <= 0 and best_quota >= QUOTA_RESUME_FLOOR:
    return True, best
  return False, {}


# Resumes up to max_resumes held conversations/items whose senior profile quota has recovered
def flush_ready_quota_holds(
    *,
    models: dict[str, Any] | None = None,
    wake_fn: Callable[[str, str], bool] | None = None,
    now_ts: float | None = None,
    max_resumes: int = 2,
    queue_path: str = "",
) -> list[dict[str, Any]]:
  avail_models = (
      models
      if isinstance(models, dict)
      else (cl.get_available_models().get("models") or {})
  )
  if not avail_models:
    return []
  now = cl._now_ts() if now_ts is None else float(now_ts)
  snapshot = load_hold_queue(queue_path)
  if not snapshot:
    return []

  ordered = sorted(
      (v for v in snapshot.values() if isinstance(v, dict)),
      key=lambda r: float(r.get("created_ts") or 0.0),
  )
  resumed: list[dict[str, Any]] = []
  for item in ordered:
    if len(resumed) >= max_resumes:
      break
    cid = str(item.get("conversation_id") or "").strip()
    hold_id = str(item.get("id") or "")
    # Drop stale holds for conversations that were later exempted by the user
    if cid and is_conversation_exempt(cid, queue_path=queue_path):
      unblock_conversation(cid, queue_path=queue_path)
      continue
    ready, best = is_hold_ready_to_resume(item, avail_models, now_ts=now)
    if not ready:
      continue
    if cid:
      if wake_fn is None:
        continue
      msg = str(item.get("message") or DEFAULT_RESUME_PROMPT)
      if item.get("title_waiting_set"):
        try:
          cl.set_conversation_wait_state(cid, waiting=False)
        except Exception:  # lint: allow-broad-except (title restore is best-effort)
          pass
      if not wake_fn(cid, msg):
        continue
    # Remove the resumed entry from the persistent queue
    def _pop_id(data: dict[str, Any], target_id: str = hold_id) -> tuple[bool, bool]:
      items_map = data.get("items") or {}
      if target_id in items_map:
        items_map.pop(target_id, None)
        return True, True
      return False, False

    _with_queue_lock(queue_path, _pop_id)
    resumed.append({**item, "resumed_model": best})
  return resumed


def _build_cli_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(description="Manage quota-hold queue and per-chat exemptions")
  sub = parser.add_subparsers(dest="cmd", required=True)

  p_ex = sub.add_parser("exempt", help="Exempt a conversation from quota hold and unblock it")
  p_ex.add_argument("conversation_id")
  p_ex.add_argument("--reason", default="manual_exemption")

  p_unex = sub.add_parser("unexempt", help="Remove a per-chat quota-hold exemption")
  p_unex.add_argument("conversation_id")

  p_unblk = sub.add_parser("unblock", help="Remove a conversation from the hold queue once")
  p_unblk.add_argument("conversation_id")

  p_st = sub.add_parser("status", help="Show hold queue and exemptions")
  p_st.add_argument("conversation_id", nargs="?", default="")
  return parser


def main(argv: list[str] | None = None) -> int:
  """CLI entrypoint for inspecting, exempting, or unblocking conversations on quota hold.

  Inputs: optional argv token list.
  Outputs: integer process exit code (0 on success).
  Side effects: updates quota_hold_queue.json and prints JSON status to stdout.
  """
  args = _build_cli_parser().parse_args(argv)
  if args.cmd == "exempt":
    rec = exempt_conversation(args.conversation_id, reason=args.reason)
    print(json.dumps({"status": "exempted", "exemption": rec}, ensure_ascii=False, indent=2))
    return 0
  if args.cmd == "unexempt":
    removed = unexempt_conversation(args.conversation_id)
    print(
        json.dumps(
            {"status": "unexempted" if removed else "not_found"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
  if args.cmd == "unblock":
    removed = unblock_conversation(args.conversation_id)
    print(
        json.dumps(
            {"status": "unblocked" if removed else "not_queued"},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0
  cid = (args.conversation_id or "").strip()
  qdoc = _read_queue_doc()
  items = dict(qdoc["items"]) if isinstance(qdoc.get("items"), dict) else {}
  exemptions = dict(qdoc["exemptions"]) if isinstance(qdoc.get("exemptions"), dict) else {}
  if cid:
    out = {
        "conversation_id": cid,
        "is_exempt": is_conversation_exempt(cid),
        "has_user_override": has_user_quota_hold_override(cid),
        "queued_entry": items.get(f"conv:{cid}"),
        "exemption": exemptions.get(cid),
    }
  else:
    out = {"items": items, "exemptions": exemptions}
  print(json.dumps(out, ensure_ascii=False, indent=2))
  return 0


if __name__ == "__main__":
  sys.exit(main())
