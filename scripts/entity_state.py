#!/usr/bin/env python3
"""Deterministic backend state machine for Flight Deck CL/PR items and bugs."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import math
import time
from typing import Any

OLD_MERGED_DAYS = 3

# Review outcomes that allow or encourage CR+1 and must block until required corpus gates pass
GATED_LGTM_OUTCOMES: frozenset[str] = frozenset({"CLEAN", "NITS", "COMPANION_TESTS"})

OUTCOME_BADGES: dict[str, tuple[str, str, str]] = {
    "CLEAN": ("🟢", "Clean (LGTM)", "Clean"),
    "NITS": ("🔵", "Minor nits", "Minor nits"),
    "COMPANION_TESTS": ("🟣", "Logic OK + test companion", "Logic OK + tests"),
    "COMPANION_FIX": ("🟠", "Defects + companion fix", "Defects + fix"),
    "PUSHBACK": ("🔴", "Do not land / Pushback", "Pushback"),
    "REASSIGN": ("⚪", "Out of domain", "Out of domain"),
}


def _to_int(val: Any, default: int = 0) -> int:
  if val is None or isinstance(val, bool):
    return int(val) if isinstance(val, bool) else default
  try:
    fval = float(val)
    if math.isnan(fval) or math.isinf(fval):
      return default
    return int(fval)
  except (ValueError, TypeError):
    return default


def _to_float(val: Any, default: float = 0.0) -> float:
  if val is None or isinstance(val, bool):
    return float(val) if isinstance(val, bool) else default
  try:
    fval = float(val)
    if math.isnan(fval) or math.isinf(fval):
      return default
    return fval
  except (ValueError, TypeError):
    return default


def short_actor(raw: Any, item: Mapping[str, Any] | None = None) -> str:
  """Normalizes an actor identifier, collapsing CQ and Gerrit service accounts into SkCQ."""
  s = str(raw or "")
  if (
      "skcq-be" in s
      or "gerritcodereview" in s
      or s.endswith(".gserviceaccount.com")
      or s == "bot"
  ):
    return "SkCQ"
  if isinstance(item, Mapping):
    if item.get("last_msg_author_name") and s == str(item.get("last_msg_author") or ""):
      return str(item["last_msg_author_name"])
    if item.get("owner_name") and s == str(item.get("owner_email") or ""):
      return str(item["owner_name"])
  local = s.split("@")[0]
  if len(local) <= 2 and "@" in s:
    return s
  return local


def is_companion(item: Mapping[str, Any] | None) -> bool:
  # Treat both explicit is_companion flag and companion_to parent link as companion records
  return bool(
      isinstance(item, Mapping)
      and (item.get("companion_to") or item.get("is_companion"))
  )


def ready_companions_of(item: Mapping[str, Any] | None) -> list[dict[str, Any]]:
  """Returns active companions attached to a parent item that are ready to abandon."""
  if not isinstance(item, Mapping):
    return []
  comps = item.get("companions")
  if not isinstance(comps, list):
    return []
  return [
      c
      for c in comps
      if isinstance(c, dict)
      and bool(c.get("ready_to_abandon"))
      and c.get("status") not in ("ABANDONED", "CLOSED")
  ]


def has_ready_companion(item: Mapping[str, Any] | None) -> bool:
  return len(ready_companions_of(item)) > 0


def item_key_of(item: Mapping[str, Any] | None) -> str:
  if not isinstance(item, Mapping):
    return ""
  ikey = item.get("item_key")
  if ikey:
    return str(ikey)
  return f"{item.get('kind') or 'gerrit'}:{item.get('target_id') or ''}"


def visible_items_from_data(data: Mapping[str, Any] | None) -> list[dict[str, Any]]:
  """Filters out standalone companion and cherry-pick rows when their parent CL/PR is present."""
  if not isinstance(data, Mapping):
    return []
  items_map = data.get("items")
  if not isinstance(items_map, Mapping):
    return []
  raw = [it for it in items_map.values() if isinstance(it, dict)]
  parent_keys = {
      item_key_of(it)
      for it in raw
      if not is_companion(it) and not it.get("is_cherry_pick")
  }
  visible: list[dict[str, Any]] = []
  for it in raw:
    if it.get("is_cherry_pick") and it.get("cherry_pick_of"):
      if it.get("status") in ("ABANDONED", "CLOSED"):
        continue
      # Hide cherry-pick CLs as standalone rows when the parent CL renders a Merges strip
      if str(it.get("cherry_pick_of")) not in parent_keys:
        visible.append(it)
      continue
    if not is_companion(it):
      visible.append(it)
      continue
    if it.get("status") in ("ABANDONED", "CLOSED"):
      continue
    # Keep open orphan companions visible only when the parent CL/PR is absent
    if str(it.get("companion_to") or "") not in parent_keys:
      visible.append(it)
  return visible


def resolve_stack_root_key(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
) -> str:
  """Resolves the topological root item key for a stacked CL/PR chain."""
  if not isinstance(item, Mapping):
    return ""
  if item.get("stack_root_ikey"):
    return str(item["stack_root_ikey"])
  ancs = item.get("stack_ancestors")
  if isinstance(ancs, list) and ancs and ancs[0]:
    return str(ancs[0])
  cur: Mapping[str, Any] | None = item
  visited: set[str] = set()
  while cur and cur.get("stack_parent_ikey"):
    pkey = str(cur["stack_parent_ikey"])
    if pkey in visited:
      break
    visited.add(pkey)
    parent = by_ikey.get(pkey) if by_ikey else None
    if not parent:
      return pkey
    cur = parent
  return item_key_of(cur or item)


def build_stack_index(
    items_or_state: Iterable[dict[str, Any]] | Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
  """Indexes items by canonical item_key and raw map key for stack ancestor lookups."""
  by_ikey: dict[str, dict[str, Any]] = {}
  if isinstance(items_or_state, Mapping):
    if "items" in items_or_state and isinstance(items_or_state.get("items"), Mapping):
      vis = visible_items_from_data(items_or_state)
      for it in vis:
        by_ikey[item_key_of(it)] = it
      for k, it in items_or_state["items"].items():
        if isinstance(it, dict):
          by_ikey.setdefault(item_key_of(it), it)
          by_ikey.setdefault(str(k), it)
      return by_ikey
    for k, it in items_or_state.items():
      if isinstance(it, dict):
        by_ikey[item_key_of(it)] = it
        by_ikey.setdefault(str(k), it)
    return by_ikey
  if isinstance(items_or_state, Iterable):
    for it in items_or_state:
      if isinstance(it, dict):
        by_ikey[item_key_of(it)] = it
  return by_ikey


def is_triage_stale(item: Mapping[str, Any] | None) -> bool:
  """Returns True when the item's patchset or commit revision has drifted past triage."""
  if not isinstance(item, Mapping):
    return False
  tr = item.get("triage")
  if not isinstance(tr, Mapping):
    return False
  if tr.get("is_stale"):
    return True
  # On GitHub PRs, matching commit revision means triage is current regardless of review count
  if item.get("kind") == "github":
    cur_rev = str(item.get("head_sha") or item.get("revision") or "").strip().lower()
    tr_rev = str(tr.get("triaged_revision") or "").strip().lower()
    if (
        len(cur_rev) >= 7
        and len(tr_rev) >= 7
        and (cur_rev.startswith(tr_rev) or tr_rev.startswith(cur_rev))
    ):
      return False
  rev_ps = _to_int(tr.get("triaged_patchset") or item.get("reviewed_ps") or 0)
  return rev_ps > 0 and _to_int(item.get("patchset") or 0) > rev_ps


def corpus_display_name(triage: Mapping[str, Any] | None) -> str:
  script = str((triage or {}).get("corpus_script") or "") if isinstance(triage, Mapping) else ""
  if script == "run_webparagraph_corpus.py":
    return "WebParagraph corpus"
  if script == "run_g3_paragraph_tests.py":
    return "Google3 corpus"
  return "Flutter corpus"


def effective_corpus_status(item: Mapping[str, Any] | None) -> str:
  """Evaluates corpus gate state and resets passed to pending if patchset or commit drifted."""
  if not isinstance(item, Mapping):
    return "not_required"
  tr = item.get("triage")
  if not isinstance(tr, Mapping) or not tr.get("corpus_required"):
    return "not_required"
  if is_triage_stale(item):
    return "pending"
  st = str(tr.get("corpus_status") or "").lower()
  if st in ("failed", "waived"):
    return st
  if st == "passed":
    cur_ps = _to_int(item.get("patchset") or tr.get("triaged_patchset") or 1)
    tested_ps = _to_int(tr.get("corpus_tested_patchset") or tr.get("triaged_patchset") or 0)
    # On GitHub PRs, matching commit revision satisfies the passed corpus gate
    if item.get("kind") == "github" and not is_triage_stale(item):
      cur_rev = str(item.get("head_sha") or item.get("revision") or "").strip().lower()
      tr_rev = str(
          tr.get("corpus_tested_sha") or tr.get("triaged_revision") or ""
      ).strip().lower()
      if (
          len(cur_rev) >= 7
          and len(tr_rev) >= 7
          and (cur_rev.startswith(tr_rev) or tr_rev.startswith(cur_rev))
      ):
        return "passed"
    return "passed" if (tested_ps > 0 and cur_ps == tested_ps) else "pending"
  return "pending"


def effective_g3_corpus_status(item: Mapping[str, Any] | None) -> str:
  """Evaluates Google3 Paragraph A/B gate state and resets passed to pending on newer patchsets."""
  if not isinstance(item, Mapping):
    return "not_required"
  tr = item.get("triage")
  if (
      not isinstance(tr, Mapping)
      or not tr.get("g3_corpus_required")
      or item.get("kind") == "github"
  ):
    return "not_required"
  if is_triage_stale(item):
    return "pending"
  st = str(tr.get("g3_corpus_status") or "").lower()
  if st in ("failed", "waived"):
    return st
  if st == "passed":
    cur_ps = _to_int(item.get("patchset") or tr.get("triaged_patchset") or 1)
    tested_ps = _to_int(tr.get("g3_corpus_tested_patchset") or tr.get("triaged_patchset") or 0)
    return "passed" if (tested_ps > 0 and cur_ps == tested_ps) else "pending"
  return "pending"


def join_gate_names(names: Sequence[str] | None) -> str:
  """Shortens Flutter corpus to Flutter only when paired with google3 A/B in combined pills."""
  if not names:
    return "Flutter corpus"
  if len(names) == 1:
    return names[0]
  return " + ".join("Flutter" if n == "Flutter corpus" else n for n in names)


def combined_gate_state(item: Mapping[str, Any] | None) -> dict[str, Any]:
  """Combines Flutter/WebParagraph corpus and Google3 A/B gates so green LGTM requires both."""
  tr = (item.get("triage") if isinstance(item, Mapping) else None) or {}
  if not isinstance(tr, Mapping):
    tr = {}
  gates: list[tuple[str, str]] = []
  if tr.get("corpus_required"):
    gates.append((corpus_display_name(tr), effective_corpus_status(item)))
  if tr.get("g3_corpus_required") and (
      not isinstance(item, Mapping) or item.get("kind") != "github"
  ):
    gates.append(("google3 A/B", effective_g3_corpus_status(item)))
  if not gates:
    return {"anyRequired": False, "status": "not_required", "activeLabel": "", "passedLabel": ""}
  passed = [n for n, st in gates if st == "passed"]
  passed_label = join_gate_names(passed) if passed else ""
  failed = [n for n, st in gates if st == "failed"]
  if failed:
    return {
        "anyRequired": True,
        "status": "failed",
        "activeLabel": join_gate_names(failed),
        "passedLabel": passed_label,
    }
  pending = [n for n, st in gates if st == "pending"]
  if pending:
    return {
        "anyRequired": True,
        "status": "pending",
        "activeLabel": join_gate_names(pending),
        "passedLabel": passed_label,
    }
  waived = [n for n, st in gates if st == "waived"]
  if waived:
    return {
        "anyRequired": True,
        "status": "waived",
        "activeLabel": join_gate_names(waived),
        "passedLabel": passed_label,
    }
  return {
      "anyRequired": True,
      "status": "passed",
      "activeLabel": passed_label,
      "passedLabel": passed_label,
  }


def triage_badge_info(item: Mapping[str, Any] | None) -> dict[str, str]:
  """Computes triage badge and eyebrow labels without allowing green LGTM when corpus is blocked."""
  tr = (item.get("triage") if isinstance(item, Mapping) else None) or {}
  if not isinstance(tr, Mapping):
    tr = {}
  audit_state = str(tr.get("audit_state") or "").lower()
  chat_busy = bool(isinstance(item, Mapping) and item.get("chat_busy"))
  if audit_state == "running" and chat_busy:
    tgt_ps = _to_int(
        tr.get("audit_target_patchset")
        or (item.get("patchset") if isinstance(item, Mapping) else None)
        or 1
    )
    lbl = f"🔄 Auditing PS{tgt_ps}…"
    return {"cls": "triage-auditing", "pillCls": "v-auditing", "label": lbl, "eyebrow": lbl}
  if audit_state == "blocked_auth":
    lbl = "🔒 Audit paused: run gcert"
    return {
        "cls": "triage-blocked_auth",
        "pillCls": "v-blocked_auth",
        "label": lbl,
        "eyebrow": lbl,
    }
  out = str(tr.get("review_outcome") or tr.get("verdict") or "SURGICAL_AUDIT").upper()
  rev_ps = _to_int(
      tr.get("triaged_patchset")
      or (item.get("reviewed_ps") if isinstance(item, Mapping) else None)
      or (item.get("patchset") if isinstance(item, Mapping) else None)
      or 1
  )
  cur_ps = _to_int((item.get("patchset") if isinstance(item, Mapping) else None) or 1)
  stale = is_triage_stale(item)
  g_state = combined_gate_state(item)
  if out in GATED_LGTM_OUTCOMES and g_state["anyRequired"]:
    c_name = g_state["activeLabel"]
    if stale:
      act = f"running {c_name}…" if chat_busy else f"run {c_name}"
      ico = "⏳" if chat_busy else "⚠️"
      eyebrow = (
          f"{ico} Review PS{rev_ps} → PS{cur_ps} uploaded ({act})"
          if cur_ps > rev_ps
          else f"{ico} Review PS{rev_ps} stale: new commit ({act})"
      )
      return {
          "cls": "triage-corpus_blocked",
          "pillCls": "v-corpus_blocked",
          "label": f"⏳ Running {c_name}…" if chat_busy else f"⚠️ LGTM blocked: run {c_name}",
          "eyebrow": eyebrow,
      }
    if g_state["status"] == "pending":
      lbl = f"⏳ Running {c_name}…" if chat_busy else f"⚠️ LGTM blocked: run {c_name}"
      return {
          "cls": "triage-corpus_blocked",
          "pillCls": "v-corpus_blocked",
          "label": lbl,
          "eyebrow": lbl,
      }
    if g_state["status"] == "failed":
      lbl = f"🔴 LGTM blocked: {c_name} failed"
      return {
          "cls": "triage-corpus_failed",
          "pillCls": "v-corpus_failed",
          "label": lbl,
          "eyebrow": lbl,
      }
    if g_state["status"] == "waived":
      meta = OUTCOME_BADGES.get(out)
      short = meta[2] if meta else out
      return {
          "cls": "triage-corpus_blocked",
          "pillCls": "v-corpus_blocked",
          "label": f"⚠️ {short} ({c_name} waived)",
          "eyebrow": f"⚠️ Review PS{rev_ps}: {short} ({c_name} waived)",
      }
  meta = OUTCOME_BADGES.get(out)
  raw_eye = str(tr.get("review_eyebrow_label") or "")
  if raw_eye and "LGTM blocked" not in raw_eye and "waived" not in raw_eye:
    base_eye = raw_eye
  elif meta:
    base_eye = f"{meta[0]} Review PS{rev_ps}: {meta[1]}"
  else:
    base_eye = f"Review PS{rev_ps}: {tr.get('verdict_label') or out}"
  eyebrow = (
      (f"{base_eye} → PS{cur_ps} uploaded" if cur_ps > rev_ps else f"{base_eye} (new commit)")
      if stale
      else base_eye
  )
  if tr.get("review_outcome") and meta:
    return {
        "cls": f"triage-{out.lower()}",
        "pillCls": "v-stale" if stale else f"v-{out.lower()}",
        "label": f"{meta[0]} {meta[1]}",
        "eyebrow": eyebrow,
    }
  verdict_cls = str(tr.get("verdict") or "SURGICAL_AUDIT").lower()
  return {
      "cls": f"triage-{verdict_cls}",
      "pillCls": "v-stale" if stale else f"v-{out.lower()}",
      "label": str(tr.get("verdict_label") or tr.get("verdict") or "SURGICAL AUDIT"),
      "eyebrow": eyebrow,
  }


def needs_my_reply(item: Mapping[str, Any] | None) -> bool:
  if not isinstance(item, Mapping):
    return False
  att_reason = str(item.get("attention_reason") or "").lower()
  return (
      bool(item.get("in_my_attention"))
      and item.get("phase") != "ALERT_CQ_FAILED"
      and not (item.get("cq_failed") and item.get("is_self"))
      and item.get("phase") != "SUBMITTING"
      and not item.get("is_draft")
      and not item.get("in_development")
      # On my own CL a dry-run bot note keeps me in attention_set without asking a question
      and not (
          item.get("is_self")
          and (
              short_actor(item.get("last_msg_author")) == "SkCQ"
              or (
                  "dry run succeeded" in att_reason
                  and _to_int(item.get("missing_approvals") or 0) > 0
                  and _to_int(item.get("open_external_threads") or 0) == 0
              )
          )
      )
  )


def is_alert(item: Mapping[str, Any] | None) -> bool:
  if not isinstance(item, Mapping):
    return False
  return str(item.get("phase") or "").startswith("ALERT_")


def is_post_submit_alert(item: Mapping[str, Any] | None) -> bool:
  """A post-submit alert means a landed commit broke something; CQ failure is pre-submit."""
  return is_alert(item) and (item or {}).get("phase") != "ALERT_CQ_FAILED"


def needs_sponsor_action(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
) -> bool:
  """External-author Gerrit CLs with our CR+1 need Googler action to add a 2nd reviewer or CQ+2."""
  if not isinstance(item, Mapping) or item.get("is_self") or is_companion(item):
    return False
  if item.get("status") not in ("NEW", "OPEN"):
    return False
  if item.get("is_wip") or item.get("is_draft") or item.get("reviewer_removed"):
    return False
  act = str(item.get("sponsor_action") or "")
  if not act:
    return False
  # Hold CQ+2 until unmerged stack ancestors also have all approvals and no conflict
  if act == "submit_cq":
    ancs = item.get("stack_ancestors") if isinstance(item.get("stack_ancestors"), list) else []
    for anc_key in ancs:
      key_str = str(anc_key)
      anc = (by_ikey.get(key_str) if by_ikey else None) or (
          raw_items.get(key_str) if raw_items else None
      )
      if not isinstance(anc, Mapping):
        continue
      st = str(anc.get("status") or "").upper()
      if st in ("MERGED", "ABANDONED", "CLOSED"):
        continue
      if (
          _to_int(anc.get("missing_approvals") or 0) > 0
          or anc.get("merge_conflict")
          or anc.get("cq_failed")
          or anc.get("is_wip")
      ):
        return False
  return True


def status_of(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> str:
  """Classifies an item into its fine-grained workflow status."""
  if not isinstance(item, Mapping):
    return "waiting"
  pre_review = bool(item.get("is_draft") or item.get("in_development"))
  # Pre-review drafts and no-reviewer changes stay out of Needs You until reviewers are added
  if is_post_submit_alert(item) or (
      not pre_review and (is_alert(item) or needs_my_reply(item))
  ):
    return "attention"
  if item.get("status") in ("ABANDONED", "CLOSED"):
    return "abandoned"
  if has_ready_companion(item):
    return "attention"
  merge_children = item.get("merge_children")
  if isinstance(merge_children, list) and any(
      isinstance(mc, Mapping) and mc.get("cl_needs_you") for mc in merge_children
  ):
    return "attention"
  if needs_sponsor_action(item, by_ikey=by_ikey, raw_items=raw_items):
    return "attention"
  if is_companion(item):
    return "attention" if item.get("ready_to_abandon") else "waiting"
  if item.get("status") == "MERGED":
    cur_now = time.time() if now_ts is None else now_ts
    age_days = (cur_now - _to_float(item.get("submitted_ts") or 0.0)) / 86400.0
    return "old" if age_days > OLD_MERGED_DAYS else "merged"
  if item.get("is_watching") or (not item.get("is_self") and item.get("reviewer_removed")):
    return "watching"
  if item.get("merge_conflict") and item.get("is_self") and not pre_review:
    return "conflict"
  if item.get("is_draft"):
    return "draft"
  if item.get("in_development"):
    return "dev"
  cq = str(item.get("cq_status") or "")
  is_approved = (
      _to_int(item.get("approved_count") or 0) > 0
      and _to_int(item.get("missing_approvals") or 0) == 0
  )
  if (
      item.get("phase") == "SUBMITTING"
      or cq.startswith("CQ+2")
      or (is_approved and (cq.startswith("CQ+") or "running" in cq))
  ):
    return "cq"
  if item.get("tree_red"):
    return "tree"
  return "waiting"


def lane_of(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> str:
  """Maps an item's status to its Flight Deck board lane."""
  if not isinstance(item, Mapping):
    return "done"
  st = status_of(item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts)
  if st in ("attention", "conflict"):
    return "you"
  if st == "cq":
    return "landing"
  if st == "merged":
    if item.get("has_active_merges"):
      return "landing"
    ps = item.get("post_submit")
    return "landing" if (isinstance(ps, Mapping) and not ps.get("all_passed")) else "done"
  if st == "old" and item.get("has_active_merges"):
    return "landing"
  if st in ("draft", "dev"):
    return "desk"
  if st in ("waiting", "tree"):
    return "waiting"
  if st == "watching":
    return "watching"
  return "done"


def is_reviewer_change(item: Mapping[str, Any] | None) -> bool:
  """Returns True when the user is an assigned reviewer on an open external CL/PR."""
  if not isinstance(item, Mapping):
    return False
  if item.get("is_self") or is_companion(item) or item.get("is_companion"):
    return False
  if item.get("reviewer_removed"):
    return False
  role = str(item.get("role") or "").strip()
  if role:
    return role.startswith("reviewer")
  return not bool(item.get("is_watching"))


def can_rebase(item: Mapping[str, Any] | None) -> bool:
  if not isinstance(item, Mapping) or is_companion(item):
    return False
  # Allow local rebase when either VCS mergeable or Google testing / SkCQ reports conflict
  has_conflict = bool(item.get("merge_conflict") or item.get("cq_sync_rebase_needed"))
  return (
      bool(has_conflict and item.get("is_self"))
      and item.get("status") in ("NEW", "OPEN")
      and bool(item.get("item_key"))
  )


def can_finalize_clean_review(item: Mapping[str, Any] | None) -> bool:
  """Gates one-click finalization to open non-WIP Gerrit CLs with green CLEAN triage."""
  if (
      not isinstance(item, Mapping)
      or item.get("kind") != "gerrit"
      or item.get("is_self")
      or is_companion(item)
  ):
    return False
  if item.get("status") != "NEW" or item.get("is_wip") or item.get("self_approved"):
    return False
  if _to_int(item.get("self_cr_vote") or 0) > 0 or is_triage_stale(item):
    return False
  tr = item.get("triage")
  if not isinstance(tr, Mapping) or str(tr.get("review_outcome") or "").upper() != "CLEAN":
    return False
  g_state = combined_gate_state(item)
  return g_state["status"] in ("passed", "not_required", "waived")


def spine_step_short_state(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> dict[str, str]:
  """Formats short status text, icon, and tone for a step in the horizontal chain spine."""
  if not isinstance(item, Mapping):
    return {"short": "Waiting", "icon": "⏳", "tone": "spine-step"}
  if item.get("status") == "MERGED":
    return {"short": "Merged", "icon": "✓", "tone": "spine-merged"}
  if item.get("status") in ("ABANDONED", "CLOSED"):
    return {"short": "Closed", "icon": "—", "tone": "spine-step"}
  if item.get("cq_failed") or item.get("phase") == "ALERT_CQ_FAILED":
    return {"short": "CQ fail", "icon": "✗", "tone": "spine-bad"}
  if needs_sponsor_action(item, by_ikey=by_ikey, raw_items=raw_items):
    act = item.get("sponsor_action")
    if act == "assign_second_reviewer":
      return {"short": "CR+1 · +2nd rev", "icon": "👤", "tone": "spine-you"}
    if act == "submit_cq_conflict":
      return {"short": "CR+1 · rebase", "icon": "⚠️", "tone": "spine-warn"}
    return {"short": "Ready CQ+2", "icon": "🚀", "tone": "spine-you"}
  if item.get("self_approved") or item.get("self_review_state") == "approved":
    return {"short": "CR+1", "icon": "✓", "tone": "spine-ok"}
  if item.get("merge_conflict"):
    return {"short": "Conflict", "icon": "⚠️", "tone": "spine-warn"}
  if is_post_submit_alert(item):
    return {"short": "Alert", "icon": "🔴", "tone": "spine-bad"}
  if item.get("chat_busy"):
    return {"short": "Working", "icon": "⏳", "tone": "spine-you"}

  # Review triage outcomes apply only to changes where we act as reviewer, never our own CLs
  tr = item.get("triage") if is_reviewer_change(item) else None
  if isinstance(tr, Mapping) and tr.get("review_outcome") and not is_triage_stale(item):
    out = str(tr["review_outcome"]).upper()
    g_state = combined_gate_state(item)
    if out in GATED_LGTM_OUTCOMES and g_state["anyRequired"]:
      if g_state["status"] == "failed":
        return {"short": "Corpus fail", "icon": "🔴", "tone": "spine-bad"}
      if g_state["status"] == "pending":
        return {"short": "Corpus needed", "icon": "⚠️", "tone": "spine-warn"}
      if g_state["status"] == "waived":
        return {"short": "Corpus waived", "icon": "⚠️", "tone": "spine-warn"}
    if out == "CLEAN":
      return {"short": "Clean", "icon": "🟢", "tone": "spine-ok"}
    if out == "NITS":
      return {"short": "NITS", "icon": "🔵", "tone": "spine-warn"}
    if out == "COMPANION_FIX":
      return {"short": "Fix", "icon": "🟠", "tone": "spine-warn"}
    if out == "COMPANION_TESTS":
      return {"short": "Tests", "icon": "🟣", "tone": "spine-warn"}
    if out == "PUSHBACK":
      return {"short": "Pushback", "icon": "🔴", "tone": "spine-bad"}
    if out == "REASSIGN":
      return {"short": "Reassign", "icon": "⚪", "tone": "spine-step"}

  lane = lane_of(item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts)
  if lane == "you":
    return {"short": "Needs you", "icon": "⦿", "tone": "spine-you"}
  if lane == "landing":
    return {"short": "Landing", "icon": "🛫", "tone": "spine-ok"}
  if lane == "desk":
    return {
        "short": "Draft" if item.get("is_draft") else "Desk",
        "icon": "📝",
        "tone": "spine-warn",
    }
  if item.get("self_review_state") == "commented":
    return {"short": "Reviewed", "icon": "💬", "tone": "spine-step"}
  return {"short": "Waiting", "icon": "⏳", "tone": "spine-step"}


def upstream_blockers_of(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    include_ancestor_obj: bool = False,
) -> list[dict[str, Any]]:
  """Identifies unmerged upstream stack parents that hold descendants from landing or review."""
  if not isinstance(item, Mapping) or is_companion(item):
    return []
  raw_ancs = item.get("stack_ancestors")
  if isinstance(raw_ancs, list) and raw_ancs:
    ancestors = [str(a) for a in raw_ancs if a]
  elif item.get("stack_parent_ikey"):
    ancestors = [str(item["stack_parent_ikey"])]
  else:
    return []
  if not ancestors:
    return []

  blockers: list[dict[str, Any]] = []
  for anc_key in ancestors:
    anc = (by_ikey.get(anc_key) if by_ikey else None) or (
        raw_items.get(anc_key) if raw_items else None
    )
    if not isinstance(anc, Mapping):
      continue
    st = str(anc.get("status") or "").upper()
    if st in ("MERGED", "ABANDONED", "CLOSED"):
      continue

    reasons: list[str] = []
    if anc.get("merge_conflict"):
      reasons.append("conflict")
    if anc.get("cq_failed") or anc.get("phase") == "ALERT_CQ_FAILED":
      reasons.append("CQ failed")

    # Only reviewer ancestors can hold descendants on review-triage outcomes
    tr = anc.get("triage") if is_reviewer_change(anc) else None
    if isinstance(tr, Mapping) and not anc.get("self_approved"):
      v = str(tr.get("review_outcome") or tr.get("verdict") or "").upper()
      g_state = combined_gate_state(anc)
      if v == "PUSHBACK" or "PUSHBACK" in v or "DO_NOT_LAND" in v:
        reasons.append("Pushback")
      elif v == "COMPANION_FIX" or "FIX" in v:
        reasons.append("Companion fix")
      elif g_state["status"] == "failed":
        reasons.append(f"{g_state['activeLabel']} failed")
      elif g_state["status"] == "pending" and v in GATED_LGTM_OUTCOMES:
        reasons.append(f"{g_state['activeLabel']} not run")
      elif v == "COMPANION_TESTS" or "TESTS" in v:
        reasons.append("Companion tests")
      elif v == "NITS" or "NITS" in v:
        reasons.append("NITS")
      elif v == "SURGICAL_AUDIT" or "SURGICAL" in v:
        reasons.append("Surgical audit")

    comps = anc.get("companions") if isinstance(anc.get("companions"), list) else []
    open_comps = [
        c
        for c in comps
        if isinstance(c, Mapping) and c.get("status") not in ("ABANDONED", "CLOSED")
    ]
    if open_comps and not any(r.startswith("Companion") for r in reasons):
      reasons.append("Companion")

    if not anc.get("is_self"):
      if anc.get("self_review_state") == "changes_requested":
        reasons.append("Changes requested")
      elif anc.get("self_review_state") == "commented" and not anc.get("self_approved"):
        if "NITS" not in reasons:
          reasons.append("Comments")

    if anc.get("in_my_attention") and not anc.get("self_approved") and not reasons:
      reasons.append("Needs review")

    if _to_int(anc.get("missing_reviewers_to_assign") or 0) > 0:
      reasons.append("Needs 2nd reviewer")
    elif anc.get("self_approved") and _to_int(anc.get("missing_approvals") or 0) > 0:
      reasons.append("Needs 2nd +1")

    if reasons:
      dedup_reasons = list(dict.fromkeys(reasons))
      entry: dict[str, Any] = {
          "ancestor_key": anc_key,
          "reasons": dedup_reasons,
      }
      if include_ancestor_obj:
        entry["ancestor"] = anc
      blockers.append(entry)
  return blockers


def available_actions_of(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
) -> list[str]:
  """Computes the ordered list of actionable operations enabled for an item."""
  if not isinstance(item, Mapping):
    return []
  actions: list[str] = []
  if can_finalize_clean_review(item):
    actions.append("finalize_clean_review")
  if can_rebase(item):
    actions.append("rebase")
  if (
      (item.get("cq_failed") or item.get("phase") == "ALERT_CQ_FAILED")
      and item.get("status") in ("NEW", "OPEN")
      and bool(item.get("item_key"))
      and not can_rebase(item)
  ):
    actions.append("diagnose_checks")
  if has_ready_companion(item) or (is_companion(item) and item.get("ready_to_abandon")):
    actions.append("abandon_companion")
  if needs_sponsor_action(item, by_ikey=by_ikey, raw_items=raw_items):
    act = str(item.get("sponsor_action") or "")
    if act:
      actions.append(act)
  if item.get("is_self") and any(
      isinstance(lb, Mapping) and lb.get("needs_self_assign")
      for lb in (item.get("linked_bugs") or [])
  ):
    actions.append("assign_bug")
  is_rev = is_reviewer_change(item)
  has_chat = bool(item.get("conversation_id"))
  cur_ps = _to_int(item.get("patchset") or 1)
  rev_ps = _to_int(item.get("reviewed_ps") or 0)
  tgt_ps = _to_int(item.get("review_target_ps") or 0)
  if has_chat:
    if is_rev:
      if item.get("item_key") and rev_ps > 0 and cur_ps > rev_ps and cur_ps > tgt_ps:
        actions.append("start_code_review")
      else:
        actions.append("open_review_chat")
    else:
      actions.append("open_chat")
  elif item.get("item_key"):
    actions.append("start_code_review" if is_rev else "start_chat")
  return actions


def _item_core_sig(item: Mapping[str, Any] | None, now_ts: float | None = None) -> str:
  """Builds a compact string signature of an item's own state-determining fields."""
  if not isinstance(item, Mapping):
    return ""
  b = lambda v: "1" if v else "0"
  n = lambda v: str(_to_int(v, 0))
  s = lambda v: str(v or "")
  cur_now = time.time() if now_ts is None else now_ts
  is_old_merged = (
      "1"
      if s(item.get("status")) == "MERGED"
      and ((cur_now - _to_float(item.get("submitted_ts") or 0.0)) / 86400.0 > OLD_MERGED_DAYS)
      else "0"
  )
  ps = item.get("post_submit")
  ps_pending = "1" if (isinstance(ps, Mapping) and not ps.get("all_passed")) else "0"
  comps = item.get("companions") if isinstance(item.get("companions"), list) else []
  comps_sig = ",".join(
      f"{s(c.get('status'))}:{b(c.get('ready_to_abandon'))}"
      for c in comps
      if isinstance(c, Mapping)
  )
  mcs = item.get("merge_children") if isinstance(item.get("merge_children"), list) else []
  mc_needs = "1" if any(isinstance(mc, Mapping) and mc.get("cl_needs_you") for mc in mcs) else "0"
  tr = item.get("triage")
  if isinstance(tr, Mapping):
    tr_sig = ":".join([
        b(tr.get("is_stale")),
        n(tr.get("triaged_patchset")),
        s(tr.get("triaged_revision")),
        s(tr.get("review_outcome")),
        s(tr.get("verdict")),
        s(tr.get("verdict_label")),
        s(tr.get("review_eyebrow_label")),
        s(tr.get("audit_state")),
        n(tr.get("audit_target_patchset")),
        b(tr.get("corpus_required")),
        s(tr.get("corpus_status")),
        s(tr.get("corpus_script")),
        n(tr.get("corpus_tested_patchset")),
        s(tr.get("corpus_tested_sha")),
        b(tr.get("g3_corpus_required")),
        s(tr.get("g3_corpus_status")),
        n(tr.get("g3_corpus_tested_patchset")),
    ])
  else:
    tr_sig = ""
  return "|".join([
      s(item.get("status")),
      s(item.get("phase")),
      b(item.get("in_my_attention")),
      b(item.get("cq_failed")),
      b(item.get("is_self")),
      b(item.get("is_draft")),
      b(item.get("in_development")),
      b(item.get("is_wip")),
      b(item.get("is_watching")),
      b(item.get("reviewer_removed")),
      s(item.get("role")),
      short_actor(item.get("last_msg_author"), item),
      s(item.get("attention_reason")),
      n(item.get("open_external_threads")),
      s(item.get("companion_to")),
      b(item.get("is_companion")),
      b(item.get("ready_to_abandon")),
      b(item.get("merge_conflict")),
      b(item.get("cq_sync_rebase_needed")),
      s(item.get("cq_status")),
      n(item.get("approved_count")),
      n(item.get("missing_approvals")),
      n(item.get("missing_reviewers_to_assign")),
      b(item.get("tree_red")),
      b(item.get("has_active_merges")),
      is_old_merged,
      ps_pending,
      s(item.get("sponsor_action")),
      b(item.get("self_approved")),
      s(item.get("self_review_state")),
      n(item.get("self_cr_vote")),
      b(item.get("chat_busy")),
      b(item.get("conversation_id")),
      s(item.get("stack_root_ikey")),
      n(item.get("patchset")),
      n(item.get("reviewed_ps")),
      n(item.get("review_target_ps")),
      s(item.get("head_sha")),
      s(item.get("revision")),
      s(item.get("kind")),
      s(item.get("item_key")),
      comps_sig,
      mc_needs,
      tr_sig,
  ])


def entity_input_sig(
    item: Mapping[str, Any] | None,
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> str:
  """Computes the full input signature including upstream stack ancestor states."""
  if not isinstance(item, Mapping):
    return ""
  base = _item_core_sig(item, now_ts=now_ts)
  raw_ancs = item.get("stack_ancestors")
  if isinstance(raw_ancs, list) and raw_ancs:
    ancs = [str(a) for a in raw_ancs if a]
  elif item.get("stack_parent_ikey"):
    ancs = [str(item["stack_parent_ikey"])]
  else:
    ancs = []
  if not ancs:
    return base
  anc_parts: list[str] = []
  for anc_key in ancs:
    anc = (by_ikey.get(anc_key) if by_ikey else None) or (
        raw_items.get(anc_key) if raw_items else None
    )
    anc_parts.append(f"{anc_key}={_item_core_sig(anc, now_ts=now_ts) if anc else ''}")
  return f"{base}||{'~'.join(anc_parts)}"


def compute_item_entity_state(
    item: Mapping[str, Any],
    by_ikey: Mapping[str, dict[str, Any]] | None = None,
    raw_items: Mapping[str, dict[str, Any]] | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
  """Computes the complete backend state dictionary for a single CL/PR item."""
  return {
      "input_sig": entity_input_sig(item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts),
      "status": status_of(item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts),
      "lane": lane_of(item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts),
      "is_companion": is_companion(item),
      "has_ready_companion": has_ready_companion(item),
      "needs_my_reply": needs_my_reply(item),
      "is_alert": is_alert(item),
      "is_post_submit_alert": is_post_submit_alert(item),
      "needs_sponsor_action": needs_sponsor_action(item, by_ikey=by_ikey, raw_items=raw_items),
      "is_reviewer_change": is_reviewer_change(item),
      "can_rebase": can_rebase(item),
      "can_finalize_clean_review": can_finalize_clean_review(item),
      "is_triage_stale": is_triage_stale(item),
      "effective_corpus_status": effective_corpus_status(item),
      "effective_g3_corpus_status": effective_g3_corpus_status(item),
      "combined_gate_state": combined_gate_state(item),
      "triage_badge_info": triage_badge_info(item),
      "spine_short_state": spine_step_short_state(
          item, by_ikey=by_ikey, raw_items=raw_items, now_ts=now_ts
      ),
      # Include canonical stack root key so clients can group stack members without walking parents
      "stack_root_key": resolve_stack_root_key(item, by_ikey=by_ikey),
      "upstream_blockers": upstream_blockers_of(
          item, by_ikey=by_ikey, raw_items=raw_items, include_ancestor_obj=False
      ),
      "available_actions": available_actions_of(item, by_ikey=by_ikey, raw_items=raw_items),
  }


def compute_bug_entity_state(bug: Mapping[str, Any]) -> dict[str, Any]:
  """Computes the backend state summary and available actions for a bug record."""
  lane = str(bug.get("lane") or "assigned")
  needs_you = bool(bug.get("needs_you")) or lane == "you"
  chat_busy = bool(bug.get("chat_busy"))
  actions: list[str] = []
  # Include both pending security merge requests and approved merge-request bugs needing cherry-pick
  if (bug.get("is_security_merge_request") or bug.get("is_merge_request_bug")) and lane == "you":
    phase = str(bug.get("merge_action_phase") or "confirm")
    actions.append(f"security_merge_{phase}")
  if bug.get("needs_self_assign"):
    if bug.get("self_assign_conflict"):
      actions.append("add_bug_collaborator")
    actions.append("assign_bug")
  if needs_you:
    actions.append("ack_bug")
  actions.append("open_chat" if bug.get("conversation_id") else "start_chat")
  return {
      "lane": lane,
      "needs_you": needs_you,
      "actionable_now": needs_you and not chat_busy,
      "is_overdue": bool(bug.get("is_overdue")),
      "is_stale_backlog": bool(bug.get("is_stale_backlog")),
      "available_actions": actions,
  }


def enrich_entity_states(
    state: dict[str, Any],
    now_ts: float | None = None,
) -> dict[str, Any]:
  """Attaches precomputed `entity_state` dicts to all items and bugs in `state`."""
  if not isinstance(state, dict):
    return state
  cur_now = time.time() if now_ts is None else now_ts
  items_map = state.get("items")
  if isinstance(items_map, dict) and items_map:
    by_ikey = build_stack_index(state)
    for item in items_map.values():
      if isinstance(item, dict):
        item["entity_state"] = compute_item_entity_state(
            item, by_ikey=by_ikey, raw_items=items_map, now_ts=cur_now
        )
  bugs_map = state.get("bugs")
  if isinstance(bugs_map, dict) and bugs_map:
    for bug in bugs_map.values():
      if isinstance(bug, dict):
        bug["entity_state"] = compute_bug_entity_state(bug)
  return state
