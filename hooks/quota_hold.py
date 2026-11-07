#!/usr/bin/env python3
"""Quota-exhaustion hold queue and post-reset auto-resume manager (R132).

Holds non-triage conversations and background Flight Deck wakeups/auto-starts when
all allowed senior models in a project profile drop below the 5% emergency floor
(or hit 0% / mid-turn QUOTA_EXHAUSTED), and resumes them on a senior model after
the quota reset window passes.
"""

from __future__ import annotations

from datetime import datetime, timezone
import fcntl
import json
import os
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
      data: dict[str, Any] = {"items": {}}
      if os.path.isfile(path):
        try:
          with open(path, "r", encoding="utf-8") as f:
            loaded = json.load(f)
          if isinstance(loaded, dict) and isinstance(loaded.get("items"), dict):
            data = loaded
        except (OSError, ValueError):
          data = {"items": {}}
      res, dirty = fn(data)
      if dirty:
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
          json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
      return res
    finally:
      fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)


# Reads a snapshot of all currently held items from the queue file
def load_hold_queue(queue_path: str = "") -> dict[str, dict[str, Any]]:
  path = get_queue_path(queue_path)
  if not os.path.isfile(path):
    return {}
  try:
    with open(path, "r", encoding="utf-8") as f:
      data = json.load(f)
    if isinstance(data, dict) and isinstance(data.get("items"), dict):
      return dict(data["items"])
  except (OSError, ValueError):
    pass
  return {}


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
        prof = resolve_conversation_profile(cid)
        if prof == "triage":
          continue
        qstate = get_profile_quota_state(avail_models, prof)
        enqueue_quota_hold(
            kind="mid_turn_halt",
            profile=prof,
            conversation_id=cid,
            message=DEFAULT_RESUME_PROMPT,
            reset_ts=qstate["reset_ts"],
            reset_time=qstate["reset_time"],
            queue_path=queue_path,
        )
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
    ready, best = is_hold_ready_to_resume(item, avail_models, now_ts=now)
    if not ready:
      continue
    cid = str(item.get("conversation_id") or "").strip()
    hold_id = str(item.get("id") or "")
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
