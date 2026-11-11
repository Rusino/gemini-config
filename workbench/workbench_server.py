#!/usr/bin/env python3
"""Workbench HTTP server (:8090) and passive background sync daemon."""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
import contextlib
import html as html_lib
import http.server
import json
import math
import os
import re
import socketserver
import subprocess
import sys
import threading
import time
from typing import Any
import urllib.parse

WORKBENCH_DIR = os.path.dirname(os.path.abspath(__file__))
if WORKBENCH_DIR not in sys.path:
  sys.path.insert(0, WORKBENCH_DIR)

import ingestor  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top
import workctl  # pylint: disable=g-import-not-at-top

DEFAULT_PORT = 8090
DEFAULT_INFO_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-workbench.json"
)
PEN_URL_SH = (
    "/google/src/files/head/depot/google3/security/uberproxy/tools/contrib/pen_url.sh"
)
DEFAULT_WORKBENCH_PEN_URL = (
    "https://b2607f8b048001000013f0f0cac12c3b11f9a000000000000000001.proxy.googlers.com"
)

try:
  _INITIAL_MTIME = int(os.path.getmtime(os.path.abspath(__file__)))
except OSError:
  _INITIAL_MTIME = int(time.time())


def resolve_pen_url(port: int = DEFAULT_PORT) -> str:
  if port == DEFAULT_PORT:
    fallback = DEFAULT_WORKBENCH_PEN_URL
  else:
    fallback = f"http://localhost:{port}"
  try:
    if os.path.isfile(PEN_URL_SH):
      res = subprocess.run(
          [PEN_URL_SH, "--protocol=HTTP", "-p", str(port)],
          capture_output=True,
          text=True,
          timeout=5,
          check=False,
      )
      out = res.stdout.strip()
      if res.returncode == 0 and out.startswith("https://"):
        return out
  except Exception:
    pass
  return fallback


def export_workbench_info(
    port: int = DEFAULT_PORT, info_path: str = DEFAULT_INFO_FILE
) -> dict[str, Any]:
  ver = _INITIAL_MTIME
  ver_q = f"?v={ver}" if ver > 0 else ""
  pen_base = resolve_pen_url(port)
  data = {
      "pen_url": f"{pen_base}{ver_q}",
      "local_url": f"http://localhost:{port}{ver_q}",
      "port": port,
      "ui_version": ver,
  }
  try:
    os.makedirs(os.path.dirname(os.path.abspath(info_path)), exist_ok=True)
    tmp = f"{info_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(data, f, indent=2)
    os.replace(tmp, info_path)
  except OSError:
    pass
  return data


def _esc(val: Any) -> str:
  return html_lib.escape(str(val or ""), quote=True)


_RE_ANCHOR_TAG = re.compile(
    r'<a\s+[^>]*href=["\'](https?://[^"\'<>]+)["\'][^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_RE_HTML_DRAFT_TAG = re.compile(
    r"</?(?:p|div|ul|ol|li|br|pre|a|b|strong|i|em|code|blockquote|h[1-6])\b",
    re.IGNORECASE,
)
_RE_SCRIPT_STYLE_BLOCK = re.compile(
    r"<(?:script|style)\b[^>]*>[\s\S]*?</(?:script|style)>",
    re.IGNORECASE,
)
_RE_PLAIN_URL = re.compile(r"https?://[^\s<>\"']+")
_RE_BLOCK_BREAK = re.compile(r"</?(?:p|div|li|ul|ol|br|pre|h[1-6])\b[^>]*>", re.IGNORECASE)
_RE_ANY_TAG = re.compile(r"<[^>]+>")


def _detect_draft_format(text: str) -> str:
  """Classifies draft content as html only when real HTML markup tags are present."""
  return "html" if _RE_HTML_DRAFT_TAG.search(str(text or "")) else "text"


def _linkify_escaped_plain_text(raw: str) -> str:
  """Escapes plain-text draft content preserving angle-bracket types while linkifying URLs."""
  parts: list[str] = []
  last_end = 0
  for m in _RE_PLAIN_URL.finditer(raw):
    parts.append(_esc(raw[last_end : m.start()]))
    url, suffix = ingestor._split_url_trailing_punct(m.group(0))
    if url:
      esc_u = _esc(url)
      parts.append(f'<a href="{esc_u}" target="_blank" rel="noopener">{esc_u}</a>')
    parts.append(_esc(suffix))
    last_end = m.end()
  parts.append(_esc(raw[last_end:]))
  return re.sub(r"\n{2,}", "\n", "".join(parts)).strip().replace("\n", "<br>")


def _render_safe_draft_preview(rd: dict[str, Any] | None) -> str:
  """Renders an XSS-safe HTML preview of a reply draft preserving only https?:// anchors."""
  if not rd or not str(rd.get("body") or "").strip():
    return ""
  raw = str(rd.get("body") or "").strip()
  fmt_mode = (
      "html"
      if (str(rd.get("format") or "").lower() == "html" or _detect_draft_format(raw) == "html")
      else "text"
  )
  if fmt_mode == "text":
    return _linkify_escaped_plain_text(raw)

  anchors: list[str] = []

  def _stash_anchor(m: re.Match[str]) -> str:
    """Replaces an https?:// anchor with a token while unescaping and re-escaping once."""
    href_clean = html_lib.unescape(m.group(1).strip())
    if not re.match(r"^https?://", href_clean, re.IGNORECASE):
      return m.group(2)
    lbl_clean = html_lib.unescape(_RE_ANY_TAG.sub("", m.group(2)).strip()) or href_clean
    idx = len(anchors)
    anchors.append(
        f'<a href="{_esc(href_clean)}" target="_blank" rel="noopener">{_esc(lbl_clean)}</a>'
    )
    return f"@@WB_LINK_{idx}@@"

  no_script = _RE_SCRIPT_STYLE_BLOCK.sub("", raw)
  stashed = _RE_ANCHOR_TAG.sub(_stash_anchor, no_script)
  lined = _RE_BLOCK_BREAK.sub("\n", stashed)
  stripped = _RE_ANY_TAG.sub("", lined)
  escaped = _esc(html_lib.unescape(stripped))
  collapsed = re.sub(r"\n{2,}", "\n", escaped).strip().replace("\n", "<br>")
  for idx, safe_a in enumerate(anchors):
    collapsed = collapsed.replace(f"@@WB_LINK_{idx}@@", safe_a)
  return collapsed


_RE_STAGE_EMOJI = re.compile(r"^(?:[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200D]+\s*)+")
_RE_CL_DIGITS = re.compile(r"\b(\d{5,10})\b")
_RE_CL_TITLE_PREFIX = re.compile(
    r"^(?:(?:gerrit:[^:\s]+:\d+|github_pr:[^:\s]+:\d+|piper:\d+)"
    r"|(?:Gerrit|Skia)\s+\d+(?:\s+PS\d+)?"
    r"|PR\s*#\d+(?:\s+PS\d+)?"
    r"|\d{5,10})(?:\s*[:·-]\s*|\s*$)",
    re.IGNORECASE,
)
_RE_CM_GH_ISSUE = re.compile(r"github\.com/([^/\s#]+/[^/\s#]+)/issues/(\d+)\b", re.IGNORECASE)
_RE_CM_SHORT_FLUTTER = re.compile(
    r"(?:flutter#|(?<![A-Za-z0-9_/])#)(\d{4,6})\b",
    re.IGNORECASE,
)
_RE_CM_BUGANIZER = re.compile(
    r"(?:(?<![A-Za-z0-9_./-])b/|issuetracker\.google\.com/issues/)(\d{6,})\b",
    re.IGNORECASE,
)
_RE_CM_CRBUG = re.compile(
    r"(?:crbug\.com/|issues\.chromium\.org/issues/)(\d+)\b",
    re.IGNORECASE,
)


def _clean_stage_text(raw: Any) -> str:
  """Strips leading Unicode emoji/pictographs per ' · ' segment while keeping words."""
  txt = str(raw or "").strip()
  parts = [_RE_STAGE_EMOJI.sub("", seg).strip() or seg.strip() for seg in txt.split(" · ")]
  cleaned = " · ".join(p for p in parts if p)
  return cleaned or txt


def _clean_cl_subject(raw_title: str, ast: dict[str, Any], short_lbl: str) -> str:
  """Strips redundant Gerrit/PR/key prefixes from a CL title, falling back to state.subject."""
  raw = str(raw_title or "").strip()
  cleaned = _RE_CL_TITLE_PREFIX.sub("", raw).strip()
  if not cleaned or cleaned.lower() == short_lbl.lower():
    return str(ast.get("subject") or "").strip()
  return cleaned


def _split_card_headline(w: dict[str, Any]) -> tuple[str, str]:
  """Extracts headline and verified artifact counterparty without inventing one from ' — '."""
  raw_title = str(w.get("title") or "").strip()
  origin_key = str((w.get("origin") or {}).get("artifact_key") or "")
  ranked: list[tuple[tuple[int, int, int], str]] = []
  for a in w.get("artifacts") or []:
    if not isinstance(a, dict) or a.get("role") == "context_only":
      continue
    cp = str((a.get("state") or {}).get("counterparty") or "").strip()
    if not cp:
      continue
    akey = str(a.get("artifact_key") or "")
    akind = str(a.get("kind") or "")
    prio = (
        0 if (origin_key and akey == origin_key) else 1,
        1 if akind in ("github_pr", "github_issue") else 0,
        0 if a.get("role") == "primary" else 1,
    )
    ranked.append((prio, cp))
  ranked.sort(key=lambda item: item[0])
  candidates: list[str] = []
  seen_lower: set[str] = set()
  for _, cp in ranked:
    low = cp.lower()
    if low not in seen_lower:
      seen_lower.add(low)
      candidates.append(cp)
  if not candidates:
    return raw_title, ""
  best_cp = candidates[0]
  if " — " in raw_title:
    head, tail = raw_title.rsplit(" — ", 1)
    if head.strip() and tail.strip():
      tail_low = tail.strip().lower()
      for cand in candidates:
        if cand.lower() == tail_low:
          return head.strip(), cand
  return raw_title, best_cp


def _short_issue_id(art: dict[str, Any]) -> str:
  """Formats canonical issue keys into flutter#<N>, <owner>/<repo>#<N>, b/<N>, or crbug/<N>."""
  akey = str(art.get("artifact_key") or "")
  if akey.startswith(("github_issue:", "buganizer:")):
    return work_store.format_short_bug_id(akey)
  parts = akey.split(":")
  if akey.startswith("crbug:") and len(parts) >= 2:
    return f"crbug/{parts[-1]}"
  title = str(art.get("title") or "")
  return title.split(":", 1)[0].strip() or akey


def _short_cl_label(akey: str, fallback_label: str) -> tuple[str, str]:
  """Returns (compact_cl_label, patchset_tag) for folded and expanded peer spines."""
  ps_match = re.search(r"\b(PS\d+)\b", fallback_label)
  ps_tag = ps_match.group(1) if ps_match else ""
  parts = akey.split(":")
  if akey.startswith("gerrit:") and len(parts) >= 3:
    proj = parts[1]
    prefix = "Skia" if proj == "skia" else proj.capitalize()
    return f"{prefix} {parts[2]}", ps_tag
  if akey.startswith("github_pr:") and len(parts) >= 3:
    return f"PR #{parts[2]}", ps_tag
  if akey.startswith("piper:") and len(parts) >= 2:
    p_lbl = "cl" + "/" + parts[1]
    return p_lbl, ps_tag
  clean_lbl = re.sub(r"\s+PS\d+\b", "", fallback_label).strip()
  return (clean_lbl or akey), ps_tag


def _cl_id_tokens(akey: str) -> set[str]:
  """Extracts numeric change IDs from an artifact key for bug/companion cross-matching."""
  return set(_RE_CL_DIGITS.findall(akey))


def _extract_ssr_cl_bug_keys(ast: Mapping[str, Any]) -> set[str]:
  """Collects canonical and crbug keys linked to a CL across metadata and commit text."""
  keys = set(work_store.extract_cl_bug_keys(dict(ast)))
  # Preserve explicit crbug:<N> / crbug/<N> keys across slim_ui_projection -> SSR passes
  for k in ast.get("linked_bug_keys") or ():
    ks = str(k or "").strip()
    if ks.startswith("crbug:"):
      keys.add(ks)
    elif ks.lower().startswith("crbug/") and ks[6:].isdigit():
      keys.add(f"crbug:{ks[6:]}")
  for lb in ast.get("linked_bugs") or ():
    if not isinstance(lb, Mapping):
      continue
    ik = str(lb.get("item_key") or "").strip()
    if ik.startswith("crbug:"):
      keys.add(ik)
    elif ik.lower().startswith("crbug/") and ik[6:].isdigit():
      keys.add(f"crbug:{ik[6:]}")
    num = str(lb.get("number") or lb.get("id") or lb.get("target_id") or "").strip()
    if num.isdigit() and "crbug" in str(lb.get("kind") or "").lower():
      keys.add(f"crbug:{num}")
  cm = str(ast.get("commit_message") or "")
  for m in _RE_CM_GH_ISSUE.finditer(cm):
    keys.add(f"github_issue:{m.group(1)}:{m.group(2)}")
  for m in _RE_CM_SHORT_FLUTTER.finditer(cm):
    keys.add(f"github_issue:flutter/flutter:{m.group(1)}")
  for m in _RE_CM_BUGANIZER.finditer(cm):
    keys.add(f"buganizer:{m.group(1)}")
  for m in _RE_CM_CRBUG.finditer(cm):
    keys.add(f"crbug:{m.group(1)}")
  return keys


_SLIM_STR_STATE_KEYS = (
    "status",
    "subject",
    "wip_label",
    "updated_et",
    "status_changed_et",
    "last_msg_et",
    "counterparty",
    "cascade_id",
)
_SLIM_BOOL_STATE_KEYS = (
    "is_wip",
    "is_draft",
    "in_development",
    "merge_conflict",
    "is_stacked",
    "is_active_correspondence",
    "is_self",
    "is_mine",
)
_SLIM_OBLIGATION_KEYS = (
    "obligation_id",
    "work_id",
    "target_artifact_key",
    "kind",
    "description",
    "trigger_expr",
    "status",
    "stalled_alert",
    "wb_reply_draft",
    "progress_summary",
    "correspondence_mode",
    "ambiguity_prompt",
    "ambiguity_event_key",
)


def _slim_rolls(raw_rolls: Any) -> dict[str, bool]:
  """Compresses verbose per-target autoroll dicts into compact boolean roll indicators."""
  if not isinstance(raw_rolls, Mapping):
    return {}
  out: dict[str, bool] = {}
  # Normalize heterogeneous cl_watcher dicts, '✓' status markers, and booleans into True/False
  for k, r in raw_rolls.items():
    if isinstance(r, bool):
      out[str(k)] = r
    elif isinstance(r, Mapping):
      out[str(k)] = bool(r.get("rolled") or r.get("state") == "✓")
    elif isinstance(r, str):
      out[str(k)] = r.strip() == "✓"
    else:
      out[str(k)] = bool(r)
  return out


def _slim_artifact(art: Mapping[str, Any]) -> dict[str, Any]:
  """Projects one artifact to UI-visible fields while preserving bug and draft cross-links."""
  out: dict[str, Any] = {
      "artifact_key": str(art.get("artifact_key") or ""),
      "kind": str(art.get("kind") or ""),
      "title": str(art.get("title") or ""),
      "url": str(art.get("url") or ""),
      "role": str(art.get("role") or ""),
  }
  for opt_k in ("icon", "source", "link_reason", "why_badge", "why_tooltip"):
    val = art.get(opt_k)
    if val:
      out[opt_k] = val

  ast = art.get("state")
  if not isinstance(ast, Mapping):
    out["state"] = {}
    return out

  slim_st: dict[str, Any] = {}
  for sk in _SLIM_STR_STATE_KEYS:
    sval = str(ast.get(sk) or "").strip()
    if sval:
      slim_st[sk] = sval
  for bk in _SLIM_BOOL_STATE_KEYS:
    if ast.get(bk):
      slim_st[bk] = True
  for nk in ("patchset", "message_count"):
    if ast.get(nk):
      slim_st[nk] = ast[nk]
  # Preserve explicit 0-reviewer counts so On Desk pre-review badges survive payload slimming
  if "assigned_reviewers_count" in ast and ast["assigned_reviewers_count"] is not None:
    slim_st["assigned_reviewers_count"] = int(ast["assigned_reviewers_count"] or 0)

  # Fold commit_message / bug_ids / linked_bugs into canonical linked_bug_keys before stripping text
  if out["kind"] in ("gerrit", "github_pr", "piper") or any(
      ast.get(k) for k in ("linked_bug_keys", "linked_bugs", "bug_ids", "commit_message")
  ):
    bkeys = sorted(_extract_ssr_cl_bug_keys(ast))
    if bkeys:
      slim_st["linked_bug_keys"] = bkeys

  # Retain only cross-link identifiers on issue linked_cls without multi-KB watcher payloads
  raw_lcls = ast.get("linked_cls")
  if isinstance(raw_lcls, list) and raw_lcls:
    slim_lcls = [
        {"target_id": str(lc.get("target_id") or ""), "item_key": str(lc.get("item_key") or "")}
        for lc in raw_lcls
        if isinstance(lc, Mapping) and (lc.get("target_id") or lc.get("item_key"))
    ]
    if slim_lcls:
      slim_st["linked_cls"] = slim_lcls

  # Collapse post_submit autoroll dictionaries into boolean dots before dropping raw build logs
  ps_obj = ast.get("post_submit")
  rolls_src = ps_obj.get("rolls") if isinstance(ps_obj, Mapping) else None
  slim_r = _slim_rolls(rolls_src or ast.get("rolls"))
  if slim_r:
    slim_st["rolls"] = slim_r

  # Keep only status/lane from entity_state so On Desk dev badges work without comment arrays
  est = ast.get("entity_state")
  if isinstance(est, Mapping):
    slim_est = {k: est[k] for k in ("status", "lane") if est.get(k)}
    if slim_est:
      slim_st["entity_state"] = slim_est

  # Extract mentioned CL keys/digits from Gmail draft body/snippet before dropping raw draft text
  if out["kind"] == "gmail_draft":
    draft_txt = f"{ast.get('body') or ''}\n{ast.get('snippet') or ''}"
    raw_tokens = (
        *(str(x).strip() for x in (ast.get("mentioned_keys") or ())),
        *ingestor.extract_draft_mentioned_keys(out["title"], draft_txt),
        *_RE_CL_DIGITS.findall(draft_txt),
    )
    merged_m = list(dict.fromkeys(tok for tok in raw_tokens if tok))
    if merged_m:
      slim_st["mentioned_keys"] = merged_m

  out["state"] = slim_st
  return out


def _build_card_search_text(card: Mapping[str, Any]) -> str:
  """Builds a deduplicated lowercase search index across card metadata, obligations, and peers."""
  parts: list[str] = []
  seen: set[str] = set()

  def _add(val: Any) -> None:
    """Appends unique lowercase text fragments so repeated artifact titles do not bloat JSON."""
    s = str(val or "").strip().lower()
    if s and s not in seen:
      seen.add(s)
      parts.append(s)

  # Index top-level card identity, triage reasons, origin, and inline reply draft fields
  for k in ("work_id", "title", "lane", "project_id", "roadmap_item_key"):
    _add(card.get(k))
  for r in card.get("reasons") or ():
    _add(r)
  orig = card.get("origin")
  if isinstance(orig, Mapping):
    _add(orig.get("label"))
    _add(orig.get("artifact_key"))
    _add(orig.get("url"))
  rd = card.get("reply_draft")
  if isinstance(rd, Mapping):
    _add(rd.get("subject"))
    _add(rd.get("body"))
    _add(rd.get("to"))
  # Index open/waiting obligation summaries, ambiguity prompts, and checklist deliverable rows
  for ob in card.get("obligations") or ():
    for ok in (
        "description",
        "progress_summary",
        "target_artifact_key",
        "trigger_expr",
        "ambiguity_prompt",
    ):
      _add(ob.get(ok))
  for chk in card.get("checklist") or ():
    for ck in ("bug_key", "short_id", "title", "stage_label", "url"):
      _add(chk.get(ck))
    for c in chk.get("cls") or ():
      for clk in ("artifact_key", "label", "subject", "url"):
        _add(c.get(clk))
      c_key = str(c.get("artifact_key") or "")
      if c_key:
        _add(_short_cl_label(c_key, str(c.get("label") or ""))[0])
  # Index artifact keys, URLs, tooltips, chat IDs, short CL labels, and short bug aliases
  for a in card.get("artifacts") or ():
    akey = str(a.get("artifact_key") or "")
    akind = str(a.get("kind") or "")
    for ak in ("artifact_key", "kind", "title", "url", "why_badge", "why_tooltip"):
      _add(a.get(ak))
    if akind in ("github_issue", "buganizer", "crbug"):
      _add(_short_issue_id(dict(a)))
      if akind == "crbug" and ":" in akey:
        _add(f"crbug/{akey.split(':')[-1]}")
    elif akind in ("gerrit", "github_pr", "piper") and akey:
      _add(_short_cl_label(akey, str(a.get("title") or ""))[0])
    ast = a.get("state")
    if isinstance(ast, Mapping):
      for sk in ("subject", "counterparty", "wip_label", "status", "cascade_id"):
        _add(ast.get(sk))
      for bk in ast.get("linked_bug_keys") or ():
        _add(bk)
        _add(_short_issue_id({"artifact_key": bk}))
  return " ".join(parts)


def slim_ui_projection(projection: Mapping[str, Any]) -> dict[str, Any]:
  """Strips multi-megabyte raw thread/CL blobs from a projection and attaches card search_text."""
  slim_works: list[dict[str, Any]] = []
  for w in projection.get("works") or ():
    cw = dict(w)
    cw["artifacts"] = [_slim_artifact(a) for a in (w.get("artifacts") or ())]
    cw["obligations"] = [
        {k: ob[k] for k in _SLIM_OBLIGATION_KEYS if k in ob}
        for ob in (w.get("obligations") or ())
    ]
    # Compress nested checklist CL roll dictionaries into compact booleans
    if isinstance(w.get("checklist"), list):
      slim_chk: list[dict[str, Any]] = []
      for item in w["checklist"]:
        ci = dict(item)
        if isinstance(item.get("cls"), list):
          slim_cls: list[dict[str, Any]] = []
          for c in item["cls"]:
            cc = dict(c)
            if "rolls" in cc:
              cc["rolls"] = _slim_rolls(cc["rolls"])
            slim_cls.append(cc)
          ci["cls"] = slim_cls
        slim_chk.append(ci)
      cw["checklist"] = slim_chk
    cw["search_text"] = _build_card_search_text(cw)
    slim_works.append(cw)

  # Preserve unanswered_tray (including thread_id) intact while dropping unused candidates list
  out = dict(projection)
  out["works"] = slim_works
  out.pop("candidates", None)
  return out


def _build_ssr_checklist_peers(
    checklist: list[dict[str, Any]],
    art_by_key: dict[str, dict[str, Any]],
    covered: set[str],
) -> list[dict[str, Any]]:
  """Converts checklist entries into baseline peer rows and records covered artifact keys."""
  peers: list[dict[str, Any]] = []
  for idx, item in enumerate(checklist):
    bkey = str(item.get("bug_key") or "")
    if bkey:
      covered.add(bkey)
    cls_out: list[dict[str, Any]] = []
    cl_tokens: set[str] = set()
    for c in item.get("cls") or []:
      ckey = str(c.get("artifact_key") or "")
      if ckey:
        covered.add(ckey)
        cl_tokens |= _cl_id_tokens(ckey)
      art = art_by_key.get(ckey) or {}
      ast = art.get("state") or {}
      short_lbl, ps_tag = _short_cl_label(ckey, str(c.get("label") or ckey))
      if not ps_tag and ast.get("patchset"):
        ps_tag = f"PS{ast['patchset']}"
      why_b = str(art.get("why_badge") or "")
      short_bug = str(item.get("short_id") or "")
      # Omit redundant fix badge when the CL already sits inside that exact bug peer
      if short_bug and why_b == f"фикс {short_bug}":
        why_b = ""
      cls_out.append({
          "artifact_key": ckey,
          "short_label": short_lbl,
          "ps_tag": ps_tag,
          "url": str(c.get("url") or art.get("url") or "#"),
          "subject": _clean_cl_subject(str(c.get("subject") or ""), ast, short_lbl),
          "status": str(c.get("status") or ast.get("status") or ""),
          "merge_conflict": bool(c.get("merge_conflict")),
          "is_wip": bool(c.get("is_wip") or ast.get("is_draft")),
          "why_badge": why_b,
      })
    peers.append({
        "peer_key": bkey or f"chk:{idx}",
        "bug_key": bkey,
        "short_id": str(item.get("short_id") or bkey),
        "title": str(item.get("title") or ""),
        "url": str(item.get("url") or "#"),
        "bug_status": str(item.get("bug_status") or "OPEN"),
        "stage": str(item.get("stage") or ""),
        "stage_tone": str(item.get("stage_tone") or "muted"),
        "stage_label": _clean_stage_text(item.get("stage_label") or item.get("stage") or ""),
        "draft_outdated": bool(item.get("draft_outdated")),
        "extra_issues": [],
        "cls": cls_out,
        "cl_tokens": cl_tokens,
        "from_checklist": True,
    })
  return peers


def _attach_or_promote_ssr_bugs(
    arts: list[dict[str, Any]],
    art_by_key: dict[str, dict[str, Any]],
    peers: list[dict[str, Any]],
    covered: set[str],
    promote_roles: set[str],
) -> None:
  """Attaches companion bugs sharing a CL onto existing peers or promotes allowed bug roles."""
  bug_kinds = {"github_issue", "buganizer", "crbug"}
  for a in arts:
    akey = str(a.get("artifact_key") or "")
    role = str(a.get("role") or "")
    if not akey or akey in covered or role == "context_only":
      continue
    if str(a.get("kind") or "") not in bug_kinds:
      continue
    ast = a.get("state") or {}
    ref_tokens = set(_RE_CL_DIGITS.findall(str(a.get("why_badge") or "")))
    for lc in ast.get("linked_cls") or []:
      if isinstance(lc, dict):
        ref_tokens |= set(_RE_CL_DIGITS.findall(
            f"{lc.get('target_id', '')} {lc.get('item_key', '')}"
        ))
    matched_peer = None
    for p in peers:
      if ref_tokens & p["cl_tokens"]:
        matched_peer = p
        break
      for c in p["cls"]:
        cl_art = art_by_key.get(c["artifact_key"]) or {}
        if akey in _extract_ssr_cl_bug_keys(cl_art.get("state") or {}):
          matched_peer = p
          break
      if matched_peer is not None:
        break
    if matched_peer is not None:
      matched_peer["extra_issues"].append({
          "artifact_key": akey,
          "short_id": _short_issue_id(a),
          "url": str(a.get("url") or "#"),
          "title": str(a.get("title") or ""),
      })
      covered.add(akey)
    elif role in promote_roles:
      sid = _short_issue_id(a)
      raw_t = str(a.get("title") or "")
      clean_t = raw_t.split(":", 1)[1].strip() if ":" in raw_t else raw_t
      st_lbl = _clean_stage_text(ast.get("status") or "Open")
      peers.append({
          "peer_key": akey,
          "bug_key": akey,
          "short_id": sid,
          "title": clean_t,
          "url": str(a.get("url") or "#"),
          "bug_status": str(ast.get("status") or "OPEN"),
          "stage": "open",
          "stage_tone": "muted",
          "stage_label": st_lbl,
          "draft_outdated": False,
          "extra_issues": [],
          "cls": [],
          "cl_tokens": set(),
          "from_checklist": False,
      })
      covered.add(akey)


def _ssr_cl_desk_kind(ast: dict[str, Any], title: str) -> str:
  """Classifies an open CL/PR state into 'dns', 'wip', 'norev', or '' for SSR peer badges."""
  if str(ast.get("status") or "OPEN").strip().upper() not in ("NEW", "OPEN"):
    return ""
  raw_u = str(ast.get("subject") or title or "").strip().upper()
  if "DO NOT SUBMIT" in raw_u or re.sub(
      r"^(?:\d+\s*:\s*)?(?:\[[^\]]*\]\s*)*", "", raw_u
  ).startswith("CONTROL:"):
    return "dns"
  est = ast.get("entity_state") if isinstance(ast.get("entity_state"), dict) else {}
  if bool(
      ast.get("is_wip")
      or ast.get("is_draft")
      or ast.get("wip_label")
      or est.get("status") == "draft"
  ):
    return "wip"
  # Honor lane=="desk" only on status-less stubs so explicit waiting/conflict states hold
  if bool(
      ast.get("in_development")
      or est.get("status") == "dev"
      or (not est.get("status") and est.get("lane") == "desk")
      or ("assigned_reviewers_count" in ast and int(ast.get("assigned_reviewers_count") or 0) == 0)
  ):
    return "norev"
  return ""


def _refresh_ssr_non_checklist_stages(peers: list[dict[str, Any]]) -> None:
  """Updates stage badges on promoted non-checklist bug peers once uncovered CLs attach."""
  for p in peers:
    if p.get("from_checklist") or not p.get("bug_key") or not p["cls"]:
      continue
    if any(c["merge_conflict"] for c in p["cls"]):
      p["stage"], p["stage_tone"], p["stage_label"] = "wip", "urgent", "Merge conflict"
    elif all(c["status"] in ("MERGED", "SUBMITTED") for c in p["cls"]):
      p["stage"], p["stage_tone"], p["stage_label"] = "merged", "ok", "Merged"
    elif any(c["is_wip"] or c.get("desk_kind") == "wip" for c in p["cls"]):
      p["stage"], p["stage_tone"], p["stage_label"] = "wip", "warn", "WIP"
    elif any(c.get("desk_kind") == "dns" for c in p["cls"]):
      p["stage"], p["stage_tone"], p["stage_label"] = "on_desk", "warn", "DO NOT SUBMIT"
    elif any(c.get("desk_kind") == "norev" for c in p["cls"]):
      p["stage"], p["stage_tone"], p["stage_label"] = "on_desk", "warn", "No reviewers"
    else:
      p["stage"], p["stage_tone"], p["stage_label"] = "review", "wait", "In review"


def _attach_ssr_uncovered_cls(
    arts: list[dict[str, Any]],
    peers: list[dict[str, Any]],
    covered: set[str],
) -> None:
  """Attaches uncovered primary/channel CL, PR, and Piper artifacts so zero CLs orphan as pills."""
  cl_kinds = {"gerrit", "github_pr", "piper"}
  ordered_cls = sorted(
      (
          a
          for a in arts
          if a.get("role") != "context_only"
          and str(a.get("kind") or "") in cl_kinds
          and str(a.get("artifact_key") or "") not in covered
      ),
      key=lambda x: 0 if x.get("role") == "primary" else 1,
  )
  for a in ordered_cls:
    akey = str(a.get("artifact_key") or "")
    if not akey or akey in covered:
      continue
    ast = a.get("state") or {}
    raw_title = str(a.get("title") or "")
    short_lbl, ps_tag = _short_cl_label(akey, raw_title or akey)
    if not ps_tag and ast.get("patchset"):
      ps_tag = f"PS{ast['patchset']}"
    subj = _clean_cl_subject(raw_title, ast, short_lbl)
    why_b = str(a.get("why_badge") or "")
    desk_kind = _ssr_cl_desk_kind(ast, raw_title)
    is_wip = bool(ast.get("is_wip") or ast.get("is_draft") or desk_kind == "wip")
    st_str = str(ast.get("status") or "OPEN")
    cl_entry = {
        "artifact_key": akey,
        "short_label": short_lbl,
        "ps_tag": ps_tag,
        "url": str(a.get("url") or "#"),
        "subject": subj,
        "status": st_str,
        "merge_conflict": bool(ast.get("merge_conflict")),
        "is_wip": is_wip,
        "desk_kind": desk_kind,
        "why_badge": why_b,
    }
    lbugs = _extract_ssr_cl_bug_keys(ast)
    why_tokens = set(_RE_CL_DIGITS.findall(why_b))
    target_peer = None
    for p in peers:
      if p["bug_key"] and p["bug_key"] in lbugs:
        target_peer = p
        break
      if p["short_id"] and p["short_id"] in why_b:
        target_peer = p
        break
      if why_tokens & p["cl_tokens"]:
        target_peer = p
        break
    # Single-open-bug cards co-locate an uncovered primary CL (e.g. Piper flag) onto that bug
    if target_peer is None and a.get("role") == "primary":
      open_bug_peers = [
          p for p in peers if p["bug_key"] and p["bug_status"].upper() != "CLOSED"
      ]
      if len(open_bug_peers) == 1:
        target_peer = open_bug_peers[0]
    if target_peer is not None:
      target_peer["cls"].append(cl_entry)
      target_peer["cl_tokens"] |= _cl_id_tokens(akey)
    else:
      is_done_st = st_str in ("MERGED", "SUBMITTED")
      if ast.get("merge_conflict"):
        s_tone, s_lbl = "urgent", "Merge conflict"
      elif is_done_st:
        s_tone, s_lbl = "ok", "Merged"
      elif desk_kind == "dns":
        s_tone, s_lbl = "warn", "DO NOT SUBMIT"
      elif is_wip:
        s_tone, s_lbl = "warn", str(ast.get("wip_label") or ("Draft" if ast.get("is_draft") else "WIP"))
      elif desk_kind == "norev":
        s_tone, s_lbl = "warn", "No reviewers"
      else:
        s_tone, s_lbl = "wait", "In review"
      peers.append({
          "peer_key": akey,
          "bug_key": "",
          "short_id": short_lbl,
          "title": subj,
          "url": str(a.get("url") or "#"),
          "bug_status": "OPEN",
          "stage": st_str.lower(),
          "stage_tone": s_tone,
          "stage_label": s_lbl,
          "draft_outdated": False,
          "extra_issues": [],
          "cls": [cl_entry],
          "cl_tokens": _cl_id_tokens(akey),
          "from_checklist": False,
      })
    covered.add(akey)


def _build_ssr_peers(w: dict[str, Any]) -> tuple[list[dict[str, Any]], set[str]]:
  """Runs 4-phase peer assembly across checklist rows, primary bugs, CLs, and companion bugs."""
  arts = w.get("artifacts") or []
  art_by_key = {str(a.get("artifact_key") or ""): a for a in arts if a.get("artifact_key")}
  covered: set[str] = set()
  peers = _build_ssr_checklist_peers(w.get("checklist") or [], art_by_key, covered)
  _attach_or_promote_ssr_bugs(arts, art_by_key, peers, covered, {"primary"})
  _attach_ssr_uncovered_cls(arts, peers, covered)
  _refresh_ssr_non_checklist_stages(peers)
  _attach_or_promote_ssr_bugs(arts, art_by_key, peers, covered, {"primary", "channel"})
  return peers, covered


def _is_ssr_corr_art(a_item: dict[str, Any]) -> bool:
  """Returns True when an artifact is a correspondence item (including Gmail drafts)."""
  ak = str(a_item.get("kind") or "")
  ast = a_item.get("state") or {}
  if ak in ("gmail_thread", "gmail_draft", "gchat_thread", "gdoc_thread"):
    return True
  return ak == "gdoc" and bool(ast.get("is_active_correspondence"))


def _is_ssr_discarded_draft(a_item: dict[str, Any]) -> bool:
  """Returns True when a Gmail draft artifact has DISCARDED status."""
  if str(a_item.get("kind") or "") != "gmail_draft":
    return False
  return str((a_item.get("state") or {}).get("status") or "DRAFT").upper() == "DISCARDED"


def _render_ssr_peer_row(wid: str, peer: dict[str, Any]) -> str:
  """Renders one deliverable peer row and its CL spine for initial SSR markup."""
  cl_chips: list[str] = []
  folded_cls: list[str] = []
  is_standalone_single = (not peer["bug_key"]) and len(peer["cls"]) == 1
  for c in peer["cls"]:
    if not (not peer["bug_key"] and c["artifact_key"] == peer["peer_key"]):
      conf_cls = " is-conflict" if c["merge_conflict"] else ""
      folded_cls.append(
          f'<a class="re-folded-cl{conf_cls}" href="{_esc(c["url"])}" '
          f'target="_blank" rel="noopener">{_esc(c["short_label"])}</a>'
      )
    ps_ssr = f' <span class="re-ps">{_esc(c["ps_tag"])}</span>' if c["ps_tag"] else ""
    show_subj = (
        bool(c["subject"])
        and c["subject"] != c["short_label"]
        and not (is_standalone_single and c["subject"] == peer["title"])
    )
    subj_ssr = f' <span class="wb-cl-subj">{_esc(c["subject"])}</span>' if show_subj else ""
    why_ssr = (
        f' <span class="wb-why">· {_esc(c["why_badge"])}</span>'
        if c["why_badge"]
        else ""
    )
    cl_chips.append(
        f'<div class="wb-cl-chip re-cl-row">'
        f'<a class="re-cl-link" href="{_esc(c["url"])}" '
        f'target="_blank" rel="noopener">{_esc(c["short_label"])}</a>'
        f"{ps_ssr}{subj_ssr}{why_ssr}</div>"
    )
  extra_ssr = "".join(
      f'<a class="re-extra-bug" href="{_esc(ex["url"])}" '
      f'target="_blank" rel="noopener">+{_esc(ex["short_id"])}</a>'
      for ex in peer["extra_issues"]
  )
  empty_spine_ssr = (
      '<div class="wb-chk-cls re-spine">'
      '<div class="re-empty-spine">No linked CLs yet</div></div>'
  )
  cls_ssr = (
      f'<div class="wb-chk-cls re-spine">{"".join(cl_chips)}</div>'
      if cl_chips
      else empty_spine_ssr
  )
  outdated_ssr = (
      '<span class="wb-draft-warn">Not in draft</span>'
      if peer["draft_outdated"]
      else ""
  )
  return (
      f'<div class="wb-chk-row re-peer" data-stage="{_esc(peer["stage"])}" '
      f'data-peer-key="{_esc(peer["peer_key"])}" data-peer-open="true">'
      f'<div class="re-peer-hdr" data-peer-hdr="{_esc(peer["peer_key"])}" '
      f'data-peer-work="{wid}">'
      f'<div class="re-peer-lead">'
      f'<a class="wb-bug-id re-peer-id" href="{_esc(peer["url"])}" '
      f'target="_blank" rel="noopener">{_esc(peer["short_id"])}</a>'
      f"{extra_ssr}"
      f'<span class="wb-bug-title re-peer-title">{_esc(peer["title"])}</span></div>'
      f'<div class="re-peer-folded-cls">{"".join(folded_cls)}</div>'
      f'<div class="re-peer-badges">'
      f'<span class="wb-stage-badge re-stage" '
      f'data-tone="{_esc(peer["stage_tone"])}">'
      f'{_esc(peer["stage_label"])}</span>{outdated_ssr}</div></div>'
      f"{cls_ssr}</div>"
  )


def _render_ssr_card(w: dict[str, Any]) -> str:
  """Renders one editorial Work card into initial SSR HTML matching client band structure."""
  wid = _esc(w.get("work_id"))
  full_title = _esc(w.get("title"))
  clean_head, counterparty = _split_card_headline(w)
  head_esc = _esc(clean_head)
  cp_ssr = (
      f'<span class="re-counterparty">{_esc(counterparty)}</span>'
      if counterparty
      else ""
  )
  raw_lane = str(w.get("lane") or "")
  lane = _esc(raw_lane)
  collapsed_attr = "false" if raw_lane == "needs-you" else "true"
  origin = w.get("origin") or {}
  origin_ssr = (
      f'<div class="wb-origin-row"><span class="wb-origin" '
      f'title="{_esc(origin.get("label"))}">'
      f'{_esc(origin.get("label"))}</span></div>'
      if origin.get("label")
      else ""
  )
  reasons_ssr = " · ".join(
      _esc(_clean_stage_text(r)) for r in (w.get("reasons") or []) if str(r or "").strip()
  )
  decision_ssr = (
      f'<div class="re-decision"><div class="re-decision-txt">{reasons_ssr}</div></div>'
      if reasons_ssr
      else ""
  )
  obs_parts: list[str] = []
  for ob in w.get("obligations") or []:
    if ob.get("status") == "done":
      continue
    ob_id_esc = _esc(ob.get("obligation_id"))
    prog = _esc(ob.get("progress_summary") or "")
    prog_html = f'<div class="wb-ob-prog">{prog}</div>' if prog else ""
    ambig_p = _esc(ob.get("ambiguity_prompt") or "")
    ambig_ev = _esc(ob.get("ambiguity_event_key") or "")
    ambig_ssr = (
        f'<div class="wb-ob-ambig"><span>{ambig_p}</span>'
        f'<div class="wb-ob-btns">'
        f'<button class="wb-btn" data-ob-keep="{ob_id_esc}" '
        f'data-ob-event="{ambig_ev}" type="button">Оставить обязательство</button>'
        f'<button class="wb-btn" data-ob-close-topic="{ob_id_esc}" '
        f'data-ob-event="{ambig_ev}" type="button">Тема закрыта ✓</button>'
        f"</div></div>"
        if ambig_p
        else ""
    )
    wait_btn_ssr = (
        f'<button class="wb-btn" data-ob-wait="{ob_id_esc}" type="button">'
        f"Ответила → жду</button>"
        if ob.get("kind") == "correspondence" and ob.get("status") == "ready"
        else ""
    )
    obs_parts.append(
        f'<div class="wb-ob re-loop-row" data-status="{_esc(ob.get("status"))}">'
        f'<div class="wb-ob-main"><span>{_esc(ob.get("description"))}</span>'
        f"{prog_html}{ambig_ssr}</div>"
        f'<div class="wb-ob-btns">{wait_btn_ssr}'
        f'<button class="wb-btn" data-ob-done="{ob_id_esc}" type="button">✓</button>'
        f"</div></div>"
    )
  obs_html = f'<div class="wb-obs">{"".join(obs_parts)}</div>' if obs_parts else ""
  peers_ssr, chk_covered = _build_ssr_peers(w)
  chk_rows_ssr = [_render_ssr_peer_row(wid, peer) for peer in peers_ssr]
  deliv_band_ssr = (
      f'<div class="re-band"><div class="wb-checklist re-peer-list">'
      f'{"".join(chk_rows_ssr)}</div></div>'
      if chk_rows_ssr
      else ""
  )
  arts = w.get("artifacts") or []
  corr_arts_ssr = [
      a
      for a in arts
      if a.get("role") != "context_only"
      and _is_ssr_corr_art(a)
      and not _is_ssr_discarded_draft(a)
  ]
  chat_arts_ssr = [
      a
      for a in arts
      if a.get("role") != "context_only"
      and str(a.get("artifact_key") or "") not in chk_covered
      and str(a.get("kind") or "") == "chat_chain"
  ]
  main_arts_ssr = [
      a
      for a in arts
      if a.get("role") != "context_only"
      and str(a.get("artifact_key") or "") not in chk_covered
      and not _is_ssr_corr_art(a)
      and str(a.get("kind") or "") != "chat_chain"
  ]
  mention_arts_ssr = [
      a
      for a in arts
      if a.get("role") == "context_only"
      and str(a.get("artifact_key") or "") not in chk_covered
  ]
  corr_pills_ssr = "".join(
      f'<div class="wb-pill re-corr-item" data-kind="{_esc(a.get("kind"))}" '
      f'data-role="{_esc(a.get("role"))}" title="{_esc(a.get("why_tooltip") or "")}">'
      f'{_esc(a.get("title"))}'
      + (
          f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
          if a.get("why_badge")
          else ""
      )
      + "</div>"
      for a in corr_arts_ssr
  )
  corr_html = f'<div class="wb-correspondence">{corr_pills_ssr}</div>' if corr_arts_ssr else ""
  rd_obj = w.get("reply_draft")
  rd_ssr = ""
  if isinstance(rd_obj, dict) and str(rd_obj.get("body") or "").strip():
    rd_prev = _render_safe_draft_preview(rd_obj)
    rd_upd = _esc(rd_obj.get("updated_et") or "")
    rd_ssr = (
        f'<div class="wb-reply-draft">'
        f'<div class="wb-reply-draft-hdr">'
        f"<span>Live reply draft · {rd_upd}</span></div>"
        f'<div class="wb-reply-draft-body">{rd_prev}</div></div>'
    )
  loop_band_ssr = (
      f'<div class="re-band">{obs_html}{corr_html}{rd_ssr}</div>'
      if (obs_html or corr_html or rd_ssr)
      else ""
  )
  chats_ssr = (
      '<div class="re-chats-row">'
      + "".join(
          f'<span class="re-chat-chip">{_esc(a.get("title"))}</span>'
          for a in chat_arts_ssr[:4]
      )
      + "</div>"
      if chat_arts_ssr
      else ""
  )
  pills_html = "".join(
      f'<div class="wb-pill" data-kind="{_esc(a.get("kind"))}" '
      f'data-role="{_esc(a.get("role"))}" title="{_esc(a.get("why_tooltip") or "")}">'
      f'{_esc(a.get("title"))}'
      + (
          f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
          if a.get("why_badge")
          else ""
      )
      + "</div>"
      for a in main_arts_ssr
  )
  pills_wrap_ssr = f'<div class="wb-pills">{pills_html}</div>' if pills_html else ""
  mentions_inner_ssr = "".join(
      f'<div class="wb-pill" data-kind="{_esc(a.get("kind"))}" data-role="context_only" '
      f'title="{_esc(a.get("why_tooltip") or "")}">'
      f'{_esc(a.get("title"))}'
      + (
          f' <span class="wb-why">· {_esc(a.get("why_badge"))}</span>'
          if a.get("why_badge")
          else ""
      )
      + "</div>"
      for a in mention_arts_ssr
  )
  mentions_cnt = len(mention_arts_ssr)
  mentions_lbl = f"{mentions_cnt} chat mention" + ("" if mentions_cnt == 1 else "s")
  # Keep an empty flex child when there are zero mentions so card actions stay right-aligned
  mentions_ssr = (
      f'<details class="wb-mentions re-mentions">'
      f'<summary title="📎 Упомянуто в чате ({mentions_cnt})">'
      f"{mentions_lbl}</summary>"
      f'<div class="wb-mentions-list">{mentions_inner_ssr}</div></details>'
      if mention_arts_ssr
      else "<span></span>"
  )
  # First click reuses any active chat idempotently; cards with a chip request a new chat
  force_new_ssr = ' data-force-new="true"' if chat_arts_ssr else ""
  chat_tip_ssr = (
      "Start an additional chat for this card"
      if chat_arts_ssr
      else "Start or open chat for this card"
  )
  start_chat_btn_ssr = (
      f'<button class="wb-btn" data-start-chat="{wid}"{force_new_ssr} '
      f'title="{chat_tip_ssr}" type="button">+ Chat</button>'
  )
  # Virtual cards materialize into persisted Works on + Chat before exposing persisted controls
  persisted_btns_ssr = ""
  if not bool(w.get("is_virtual")):
    add_draft_ssr = (
        f'<button class="wb-btn" data-edit-draft="{wid}" type="button">+ Reply draft</button>'
        if not rd_ssr
        else ""
    )
    persisted_btns_ssr = (
        f"{add_draft_ssr}"
        f'<button class="wb-btn" data-add-ob="{wid}" type="button">+ Obligation</button>'
        f'<button class="wb-btn" data-add-link="{wid}" type="button">+ Link</button>'
        f'<button class="wb-btn" data-work-done="{wid}" type="button">✓ Done</button>'
    )
  actions_ssr = f'<div class="wb-actions">{start_chat_btn_ssr}{persisted_btns_ssr}</div>'
  util_wrap_ssr = f'<div class="re-utility-row">{mentions_ssr}{actions_ssr}</div>'
  footer_ssr = f'<div class="re-footer">{chats_ssr}{pills_wrap_ssr}{util_wrap_ssr}</div>'
  # Mirror renderCardHeader status pill tones so initial SSR paint has zero hydration flash
  if raw_lane == "done":
    status_pill_ssr = '<span class="re-status-pill" data-tone="ready">✅ Done</span>'
  else:
    pill_tone, pill_lbl = {
        "needs-you": ("urgent", "Needs You"),
        "on-desk": ("warn", "On Desk"),
        "waiting": ("wait", "Waiting"),
        "": ("wait", "Waiting"),
    }.get(raw_lane, ("wait", lane))
    status_pill_ssr = (
        f'<span class="re-status-pill" data-tone="{pill_tone}">'
        f'<span class="re-dot"></span>{pill_lbl}</span>'
    )
  return (
      f'<article class="wb-card" data-work-id="{wid}" data-lane="{lane}" '
      f'data-collapsed="{collapsed_attr}">'
      f'<div class="wb-card-hdr" data-toggle-card="{wid}"><div class="wb-hdr-stack">'
      f'<div class="re-eyebrow">{status_pill_ssr}'
      f"{cp_ssr}{origin_ssr}</div>"
      f'<div class="re-headline-row">'
      f'<strong class="wb-card-title re-headline" title="{full_title}">{head_esc}</strong>'
      f"</div></div></div>"
      f'<div class="wb-card-body">{decision_ssr}{loop_band_ssr}{deliv_band_ssr}'
      f"{footer_ssr}</div></article>"
  )


def render_workbench_html(
    projection: Mapping[str, Any], ui_version: int, *, _already_slim: bool = False
) -> str:
  """Renders the standalone/embedded Workbench HTML dashboard from a slimmed UI projection."""
  slim_proj = projection if _already_slim else slim_ui_projection(projection)
  counts = slim_proj.get("counts") or {}
  metrics = slim_proj.get("metrics") or {}
  works = slim_proj.get("works") or []
  tray = slim_proj.get("unanswered_tray") or []
  synced_et = _esc(slim_proj.get("synced_et") or "")
  initial_json = json.dumps(slim_proj, ensure_ascii=False).replace("</", "<\\/")
  jetski_box_json = json.dumps(ingestor.jetski_box_url(), ensure_ascii=False)
  ssr_body = "\n".join(_render_ssr_card(w) for w in works[:40])
  precision_pct = int(round(float(metrics.get("link_precision", 1.0)) * 100))
  # Compute initial SSR chip counts directly from works for activeFilter='all', type='all'
  active_works_ssr = [w for w in works if w.get("lane") != "done"]
  ssr_all_cnt = len(active_works_ssr)
  ssr_needs_cnt = sum(1 for w in works if w.get("lane") == "needs-you")
  ssr_desk_cnt = sum(1 for w in works if w.get("lane") == "on-desk")
  ssr_wait_cnt = sum(1 for w in works if w.get("lane") == "waiting")
  ssr_done_cnt = sum(1 for w in works if w.get("lane") == "done")
  ssr_types = {
      t: sum(1 for w in active_works_ssr if t in (w.get("work_types") or ()))
      for t in ("replies", "reviews", "my-bugs", "my-cls")
  }
  ssr_replies_cnt = ssr_types["replies"]
  ssr_reviews_cnt = ssr_types["reviews"]
  ssr_bugs_cnt = ssr_types["my-bugs"]
  ssr_cls_cnt = ssr_types["my-cls"]

  return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Workbench</title>
<style>
  :root {{
    --bg: #fbfbfd;
    --surface: #ffffff;
    --surface-hover: #f5f5f7;
    --border: #e5e5ea;
    --text: #1d1d1f;
    --muted: #86868b;
    --accent: #0071e3;
    --urgent: #ff3b30;
    --ready: #15803d;
    --warn: #b45309;
    --shadow: 0 1px 2px rgba(0, 0, 0, 0.04), 0 8px 28px rgba(0, 0, 0, 0.06);
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    font-size: 12.5px;
    line-height: 1.4;
    -webkit-font-smoothing: antialiased;
  }}
  .wb-top {{
    position: sticky;
    top: 0;
    z-index: 20;
    background: rgba(251, 251, 253, 0.92);
    backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
    padding: 8px 12px;
  }}
  .wb-hdr-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }}
  .wb-brand {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-weight: 700;
    font-size: 13.5px;
    letter-spacing: -0.01em;
    color: var(--text);
  }}
  .wb-sub {{
    color: var(--muted);
    font-size: 11px;
    font-weight: 400;
  }}
  .wb-group-hdr {{
    display: block;
    margin: 14px 2px 6px;
    padding: 0;
    border: 0;
    background: none;
    color: var(--muted);
    font: inherit;
    font-weight: 600;
    text-align: left;
  }}
  .wb-drafts-toggle {{
    cursor: pointer;
  }}
  .wb-actions {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}
  .wb-btn {{
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    border-radius: 6px;
    padding: 3px 8px;
    font-size: 11.5px;
    font-weight: 500;
    box-shadow: 0 1px 1px rgba(0, 0, 0, 0.02);
  }}
  .wb-btn:hover {{
    background: var(--surface-hover);
    border-color: rgba(0, 113, 227, 0.45);
  }}
  .wb-btn-primary {{
    background: rgba(0, 113, 227, 0.09);
    border-color: rgba(0, 113, 227, 0.35);
    color: var(--accent);
    font-weight: 600;
  }}
  .wb-kpis {{
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 6px;
    margin-top: 8px;
  }}
  .wb-kpi {{
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 4px 7px;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03);
  }}
  .wb-kpi-val {{
    font-size: 14px;
    font-weight: 700;
  }}
  .wb-kpi-lbl {{
    color: var(--muted);
    font-size: 10px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .wb-filters, .wb-type-filters {{
    display: flex;
    align-items: center;
    gap: 5px;
    margin-top: 8px;
    flex-wrap: wrap;
  }}
  .wb-type-filters {{
    margin-top: 5px;
  }}
  .wb-filter-lbl {{
    color: var(--muted);
    font-size: 10.5px;
    font-weight: 600;
    margin-right: 2px;
  }}
  .wb-chip {{
    cursor: pointer;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--muted);
    border-radius: 999px;
    padding: 2px 9px;
    font-size: 11px;
  }}
  .wb-chip-cnt {{
    opacity: 0.78;
    font-weight: 500;
    font-variant-numeric: tabular-nums;
  }}
  .wb-chip[data-active="true"] {{
    background: rgba(0, 113, 227, 0.10);
    color: var(--accent);
    border-color: rgba(0, 113, 227, 0.45);
    font-weight: 600;
  }}
  .wb-filter-tools {{
    margin-left: auto;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
  }}
  .wb-sort-select {{
    height: 24px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    padding: 0 6px;
    font-size: 11px;
    font-weight: 500;
    cursor: pointer;
  }}
  .wb-sort-select:hover {{
    border-color: rgba(0, 113, 227, 0.45);
  }}
  .wb-sort-dir {{
    height: 24px;
    padding: 0 7px;
    font-size: 11px;
    line-height: 1;
    display: inline-flex;
    align-items: center;
    white-space: nowrap;
  }}
  .wb-btn-icon {{
    width: 26px;
    height: 24px;
    padding: 0;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    line-height: 1;
    color: var(--muted);
    flex-shrink: 0;
  }}
  .wb-btn-icon:hover {{
    color: var(--accent);
  }}
  .wb-btn-icon:disabled {{
    opacity: 0.4;
    cursor: default;
    pointer-events: none;
  }}
  /* Right-align search in Focus bar and allow shrinking to 110px before flex-wrap breaks */
  .wb-search {{
    margin-left: auto;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 6px;
    height: 24px;
    padding: 2px 8px;
    font-size: 11.5px;
    flex: 1 1 110px;
    min-width: 110px;
    max-width: 165px;
  }}
  .wb-main {{
    padding: 10px 12px 28px;
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .wb-form {{
    display: none;
    background: var(--surface);
    border: 1px solid rgba(0, 113, 227, 0.35);
    box-shadow: var(--shadow);
    border-radius: 8px;
    padding: 9px;
    gap: 6px;
    flex-direction: column;
  }}
  .wb-form[data-open="true"] {{
    display: flex;
  }}
  .wb-input {{
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--text);
    border-radius: 5px;
    padding: 4px 8px;
    font-size: 12px;
    width: 100%;
  }}
  .wb-tray {{
    background: #fffbeb;
    border: 1px solid rgba(180, 83, 9, 0.25);
    border-radius: 8px;
    padding: 7px 10px;
  }}
  .wb-tray-hdr {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-weight: 600;
    color: var(--warn);
    font-size: 11.5px;
    cursor: pointer;
  }}
  .wb-tray-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 5px 0;
    border-top: 1px solid rgba(180, 83, 9, 0.12);
    font-size: 11.5px;
  }}
  .wb-tray-btns {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
  }}
  .wb-btn-dismiss {{
    padding: 3px 6px;
    color: var(--muted);
  }}
  .wb-btn-dismiss:hover {{
    color: var(--urgent);
    border-color: rgba(217, 48, 37, 0.45);
    background: rgba(217, 48, 37, 0.06);
  }}
  .wb-sec-hdr {{
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 11.5px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
    margin: 4px 0 2px;
  }}
  #wb-cards-container {{
    display: flex;
    flex-direction: column;
    gap: 10px;
  }}
  .wb-card {{
    background: var(--surface);
    border: 1px solid var(--border);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.03), 0 4px 16px rgba(0, 0, 0, 0.035);
    border-radius: 12px;
    overflow: hidden;
    display: flex;
    flex-direction: column;
    transition: border-color 0.14s ease, box-shadow 0.14s ease;
  }}
  .wb-card:hover {{
    border-color: rgba(0, 0, 0, 0.14);
  }}
  .wb-card[data-lane="needs-you"] {{
    border-left: 3px solid var(--urgent);
  }}
  .wb-card[data-lane="waiting"] {{
    border-left: 3px solid var(--accent);
  }}
  .wb-card[data-lane="on-desk"] {{
    border-left: 3px solid var(--warn);
  }}
  .wb-card[data-lane="done"] {{
    opacity: 0.72;
  }}
  .wb-card-hdr {{
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 11px 14px 10px;
    cursor: pointer;
    background: var(--surface);
    transition: background 0.12s ease;
  }}
  .wb-card[data-collapsed="true"] .wb-card-hdr {{
    padding: 9px 14px;
  }}
  .wb-card-hdr:hover {{
    background: #fafafa;
  }}
  .wb-hdr-stack {{
    display: flex;
    flex-direction: column;
    gap: 4px;
    min-width: 0;
    width: 100%;
  }}
  .re-eyebrow {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
    min-width: 0;
    font-size: 11px;
    color: var(--muted);
  }}
  .re-eyebrow-left, .re-eyebrow-right {{
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex-wrap: wrap;
    min-width: 0;
  }}
  .re-eyebrow-right {{
    margin-left: auto;
    flex-shrink: 0;
  }}
  .re-status-pill {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 1.5px 7px;
    border-radius: 999px;
    font-size: 10.5px;
    font-weight: 600;
    letter-spacing: -0.01em;
    background: #eff6ff;
    color: var(--accent);
    border: 1px solid rgba(0, 113, 227, 0.2);
  }}
  .re-status-pill[data-tone="urgent"] {{
    background: #fff2f1;
    color: var(--urgent);
    border-color: rgba(255, 59, 48, 0.24);
  }}
  .re-status-pill[data-tone="warn"] {{
    background: #fffbeb;
    color: var(--warn);
    border-color: rgba(180, 83, 9, 0.25);
  }}
  .re-status-pill[data-tone="ready"] {{
    background: #f0fdf4;
    color: var(--ready);
    border-color: rgba(21, 128, 61, 0.24);
  }}
  .re-dot {{
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: currentColor;
    flex-shrink: 0;
  }}
  .re-counterparty {{
    font-weight: 600;
    color: #3a3a3c;
    font-size: 11px;
  }}
  .wb-origin-row {{
    display: inline-flex;
    align-items: center;
    min-width: 0;
    max-width: 100%;
  }}
  .wb-origin {{
    display: inline-block;
    vertical-align: middle;
    padding: 1px 6px;
    border-radius: 5px;
    font-size: 10.5px;
    font-weight: 500;
    background: #f5f5f7;
    border: 1px solid rgba(0, 0, 0, 0.06);
    color: #515154;
    text-decoration: none;
    max-width: 280px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  a.wb-origin:hover {{
    border-color: rgba(0, 113, 227, 0.4);
    color: var(--accent);
    background: #eff6ff;
  }}
  .re-chevron {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 16px;
    height: 16px;
    color: var(--muted);
    transition: transform 0.14s ease, color 0.12s ease;
  }}
  .wb-card[data-collapsed="false"] .re-chevron {{
    transform: rotate(90deg);
  }}
  .wb-card-hdr:hover .re-chevron {{
    color: var(--accent);
  }}
  .re-headline-row {{
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 10px;
    min-width: 0;
  }}
  .wb-card-title {{
    font-weight: 650;
    font-size: 13.5px;
    line-height: 1.32;
    letter-spacing: -0.012em;
    color: var(--text);
    word-break: break-word;
    min-width: 0;
    flex: 1;
  }}
  .re-headline-sub {{
    font-size: 11px;
    font-weight: 500;
    color: var(--muted);
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }}
  .re-collapsed-summary {{
    display: none;
    font-size: 11px;
    color: var(--muted);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}
  .wb-card[data-collapsed="true"] .re-collapsed-summary {{
    display: block;
  }}
  .wb-card-body {{
    display: flex;
    flex-direction: column;
    border-top: 1px solid var(--border);
  }}
  .wb-card[data-collapsed="true"] .wb-card-body {{
    display: none;
  }}
  .re-decision {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 7px 14px;
    background: #fffbeb;
    border-bottom: 1px solid rgba(180, 83, 9, 0.14);
    color: #92400e;
    font-size: 11.5px;
  }}
  .wb-card[data-lane="needs-you"] .re-decision {{
    background: #fff5f5;
    border-bottom-color: rgba(255, 59, 48, 0.14);
    color: #991b1b;
  }}
  .re-decision-txt {{
    display: flex;
    align-items: center;
    gap: 6px;
    min-width: 0;
    flex: 1;
    font-weight: 500;
  }}
  .re-band {{
    display: flex;
    flex-direction: column;
    gap: 7px;
    padding: 9px 14px;
    border-bottom: 1px solid rgba(0, 0, 0, 0.05);
  }}
  .re-band:last-child {{
    border-bottom: none;
  }}
  .re-band-hdr {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .re-band-lbl {{
    font-size: 10px;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: 0.05em;
    color: var(--muted);
  }}
  .re-fold-bar {{
    display: inline-flex;
    align-items: center;
    gap: 2px;
    background: #f2f2f7;
    padding: 2px;
    border-radius: 6px;
  }}
  .re-fold-btn {{
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 10.5px;
    font-weight: 500;
    padding: 1.5px 7px;
    border-radius: 4px;
  }}
  .re-fold-btn:hover {{
    color: var(--text);
  }}
  .re-fold-btn[data-active="true"] {{
    background: var(--surface);
    color: var(--text);
    font-weight: 600;
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  }}
  .wb-obs {{
    display: flex;
    flex-direction: column;
    gap: 5px;
  }}
  .wb-ob {{
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 8px;
    padding: 6px 9px;
    border-radius: 8px;
    background: #f8fafc;
    border: 1px solid rgba(0, 0, 0, 0.06);
    font-size: 11.5px;
  }}
  .wb-ob[data-status="ready"] {{
    background: #f0fdf4;
    border-color: rgba(21, 128, 61, 0.28);
  }}
  .wb-ob[data-stalled="true"] {{
    background: #fff2f1;
    border-color: rgba(255, 59, 48, 0.28);
  }}
  .wb-ob-main {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    min-width: 0;
    flex: 1;
  }}
  .wb-ob-prog {{
    font-size: 10.5px;
    font-weight: 600;
    color: var(--accent);
    background: #eff6ff;
    border: 1px solid rgba(0, 113, 227, 0.18);
    padding: 1px 7px;
    border-radius: 999px;
    font-variant-numeric: tabular-nums;
  }}
  .wb-ob-btns {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
    flex-shrink: 0;
  }}
  .wb-ob-ambig {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    flex-wrap: wrap;
    width: 100%;
    margin-top: 2px;
    padding: 4px 8px;
    border-radius: 6px;
    background: #fffbeb;
    border: 1px solid rgba(180, 83, 9, 0.24);
    color: var(--warn);
    font-size: 11px;
  }}
  .wb-correspondence {{
    display: flex;
    flex-direction: column;
    gap: 3px;
  }}
  .re-corr-row {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 7px;
    border-radius: 6px;
    background: #f8fafc;
    border: 1px solid rgba(0, 0, 0, 0.05);
    font-size: 11.5px;
    min-width: 0;
  }}
  .re-corr-row:hover {{
    border-color: rgba(0, 113, 227, 0.3);
    background: #f2f7ff;
  }}
  .re-corr-link {{
    color: var(--text);
    text-decoration: none;
    font-weight: 500;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
    flex: 1;
  }}
  .re-corr-link:hover {{
    color: var(--accent);
  }}
  .re-msg-cnt {{
    font-size: 10.5px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
    flex-shrink: 0;
  }}
  .wb-checklist {{
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: var(--surface);
    overflow: hidden;
  }}
  .wb-chk-row.re-peer {{
    display: flex;
    flex-direction: column;
    border-top: 1px solid rgba(0, 0, 0, 0.06);
    background: var(--surface);
  }}
  .wb-chk-row.re-peer:first-child {{
    border-top: none;
  }}
  .re-peer-hdr {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 7px;
    min-height: 34px;
    padding: 6px 10px;
    cursor: pointer;
    min-width: 0;
    transition: background 0.1s ease;
  }}
  .re-peer-hdr:hover {{
    background: #f9f9fb;
  }}
  .re-peer-caret {{
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 13px;
    height: 13px;
    color: var(--muted);
    flex-shrink: 0;
    transition: transform 0.12s ease;
  }}
  .re-peer[data-peer-open="true"] .re-peer-caret {{
    transform: rotate(90deg);
  }}
  .re-peer-lead {{
    display: flex;
    align-items: baseline;
    gap: 6px;
    flex: 1 1 160px;
    min-width: 140px;
    overflow: hidden;
  }}
  .wb-bug-id {{
    font-weight: 650;
    font-size: 11.5px;
    color: var(--accent);
    text-decoration: none;
    white-space: nowrap;
    flex-shrink: 0;
  }}
  .wb-bug-id:hover {{
    text-decoration: underline;
  }}
  .re-extra-wrap {{
    display: inline-flex;
    align-items: center;
    gap: 2px;
    flex-shrink: 0;
  }}
  .re-extra-bug {{
    font-size: 10.5px;
    font-weight: 600;
    color: #0369a1;
    background: #e0f2fe;
    padding: 0 5px;
    border-radius: 4px;
    text-decoration: none;
    white-space: nowrap;
    flex-shrink: 0;
  }}
  .re-extra-bug:hover {{
    text-decoration: underline;
  }}
  .wb-bug-title {{
    font-size: 11.5px;
    color: #3a3a3c;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }}
  .re-peer[data-peer-open="true"] .wb-bug-title {{
    white-space: normal;
  }}
  .re-peer-folded-cls {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-wrap: wrap;
    flex-shrink: 1;
    min-width: 0;
  }}
  .re-peer[data-peer-open="true"] .re-peer-folded-cls {{
    display: none;
  }}
  .re-folded-cl {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10.5px;
    font-weight: 600;
    background: #f2f4f7;
    border: 1px solid rgba(0, 0, 0, 0.08);
    color: var(--accent);
    text-decoration: none;
    white-space: nowrap;
    font-variant-numeric: tabular-nums;
  }}
  .re-folded-cl:hover {{
    border-color: rgba(0, 113, 227, 0.45);
    background: #eff6ff;
  }}
  .re-folded-cl.is-conflict {{
    background: #fff2f1;
    border-color: rgba(255, 59, 48, 0.35);
    color: var(--urgent);
  }}
  .re-peer-badges {{
    display: inline-flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
    margin-left: auto;
  }}
  .wb-stage-badge {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 999px;
    font-size: 10px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .wb-stage-badge[data-tone="ready"] {{
    background: #dcfce7;
    color: var(--ready);
  }}
  .wb-stage-badge[data-tone="ok"] {{
    background: #e0f2fe;
    color: #0369a1;
  }}
  .wb-stage-badge[data-tone="wait"] {{
    background: #eff6ff;
    color: var(--accent);
  }}
  .wb-stage-badge[data-tone="warn"] {{
    background: #fffbeb;
    color: var(--warn);
  }}
  .wb-stage-badge[data-tone="urgent"] {{
    background: #fff2f1;
    color: var(--urgent);
  }}
  .wb-stage-badge[data-tone="muted"] {{
    background: #f2f2f7;
    color: var(--muted);
  }}
  .wb-draft-warn {{
    display: inline-flex;
    align-items: center;
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
    background: #fffbeb;
    color: var(--warn);
    border: 1px dashed rgba(180, 83, 9, 0.4);
    white-space: nowrap;
  }}
  .wb-chk-cls {{
    display: flex;
    flex-direction: column;
    gap: 4px;
    padding: 2px 10px 8px 29px;
    background: #fbfbfd;
    border-top: 1px solid rgba(0, 0, 0, 0.04);
    width: 100%;
  }}
  .re-peer[data-peer-open="false"] .wb-chk-cls {{
    display: none;
  }}
  .wb-cl-chip {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px;
    padding: 3px 6px;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid rgba(0, 0, 0, 0.06);
    color: var(--text);
    font-size: 11px;
    width: 100%;
  }}
  .re-cl-link {{
    color: var(--accent);
    text-decoration: none;
    font-weight: 600;
    font-variant-numeric: tabular-nums;
    white-space: nowrap;
  }}
  .re-cl-link:hover {{
    text-decoration: underline;
  }}
  .re-ps {{
    font-size: 10px;
    font-weight: 600;
    color: var(--muted);
    background: #f2f2f7;
    padding: 0 4px;
    border-radius: 3px;
  }}
  .wb-cl-subj {{
    color: #48484a;
    font-size: 11px;
    flex: 1 1 160px;
    min-width: 120px;
  }}
  .wb-cl-tag {{
    font-size: 9.5px;
    padding: 0.5px 5px;
    border-radius: 4px;
    font-weight: 600;
    white-space: nowrap;
  }}
  .re-empty-spine {{
    font-size: 10.5px;
    color: var(--muted);
    padding: 2px 0;
  }}
  .re-footer {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 14px 9px;
    background: #fafafa;
  }}
  .re-chats-row {{
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 5px;
  }}
  .re-chat-chip {{
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    font-size: 10.5px;
    color: #3a3a3c;
    max-width: 100%;
  }}
  .re-chat-main {{
    color: inherit;
    text-decoration: none;
    font-weight: 500;
    max-width: 260px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }}
  .re-chat-main:hover {{
    color: var(--accent);
  }}
  .re-chat-piece {{
    color: var(--accent);
    text-decoration: none;
    font-weight: 600;
    padding: 0 3px;
    border-radius: 3px;
    background: #eff6ff;
    font-variant-numeric: tabular-nums;
  }}
  .re-chat-piece:hover {{
    background: #dbeafe;
  }}
  .wb-pills {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
  }}
  .wb-pill {{
    display: inline-flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 5px;
    padding: 3px 8px;
    border-radius: 6px;
    background: var(--surface);
    border: 1px solid var(--border);
    color: var(--text);
    text-decoration: none;
    font-size: 11px;
    max-width: 100%;
  }}
  .wb-pill[data-role="context_only"] {{
    opacity: 0.84;
    border-style: dashed;
  }}
  .wb-why {{
    color: var(--muted);
    font-size: 10px;
    font-weight: 500;
  }}
  .re-utility-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
  }}
  .wb-mentions {{
    font-size: 11px;
    color: var(--muted);
  }}
  .wb-mentions summary {{
    cursor: pointer;
    user-select: none;
    font-size: 10.5px;
    font-weight: 500;
    color: var(--muted);
    padding: 2px 0;
  }}
  .wb-mentions summary:hover {{
    color: var(--accent);
  }}
  .wb-mentions-list {{
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 4px;
    margin-top: 5px;
    padding-left: 6px;
  }}
  .re-unlink, .wb-cut {{
    cursor: pointer;
    border: none;
    background: transparent;
    color: var(--muted);
    font-size: 11px;
    line-height: 1;
    padding: 1px 3px;
    border-radius: 3px;
    opacity: 0.42;
    transition: opacity 0.1s ease, color 0.1s ease;
  }}
  .re-peer-hdr:hover .re-unlink,
  .re-extra-wrap:hover .re-unlink,
  .wb-cl-chip:hover .re-unlink,
  .re-corr-row:hover .re-unlink,
  .re-chat-chip:hover .re-unlink,
  .wb-pill:hover .re-unlink {{
    opacity: 0.9;
  }}
  .re-unlink:hover, .wb-cut:hover {{
    opacity: 1;
    color: var(--urgent);
    background: rgba(255, 59, 48, 0.08);
  }}
  .wb-rolls {{
    display: inline-flex;
    gap: 2px;
  }}
  .wb-roll-dot {{
    font-size: 9.5px;
    font-weight: 600;
    padding: 0 4px;
    border-radius: 3px;
    background: #f1f5f9;
    border: 1px solid #e2e8f0;
    color: var(--muted);
  }}
  .wb-roll-dot[data-rolled="true"] {{
    background: #dcfce7;
    border-color: rgba(21, 128, 61, 0.28);
    color: var(--ready);
  }}
  .wb-reply-draft {{
    display: flex;
    flex-direction: column;
    gap: 6px;
    padding: 8px 10px;
    border-radius: 8px;
    background: #f5f9ff;
    border: 1px solid rgba(0, 113, 227, 0.2);
    font-size: 11.5px;
  }}
  .wb-reply-draft-hdr {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    flex-wrap: wrap;
    font-size: 11px;
    font-weight: 600;
    color: #0369a1;
  }}
  .wb-reply-draft-body {{
    color: var(--text);
    line-height: 1.45;
    word-break: break-word;
  }}
  .wb-reply-draft-body a {{
    color: var(--accent);
    text-decoration: none;
  }}
  .wb-reply-draft-body a:hover {{
    text-decoration: underline;
  }}
  .re-sync-strip {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    flex-wrap: wrap;
    padding: 4px 8px;
    border-radius: 6px;
    background: #fffbeb;
    border: 1px dashed rgba(180, 83, 9, 0.35);
    color: #92400e;
    font-size: 10.5px;
  }}
  .wb-reply-textarea {{
    width: 100%;
    min-height: 84px;
    resize: vertical;
    font-family: inherit;
    font-size: 11.5px;
    padding: 6px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: #ffffff;
    color: var(--text);
  }}
</style>
</head>
<body>
  <header class="wb-top">
    <div class="wb-hdr-row">
      <div class="wb-brand">
        <span>🛠️ Workbench</span>
        <span class="wb-sub" id="wb-synced">Synced {synced_et}</span>
      </div>
      <div class="wb-actions">
        <button class="wb-btn wb-btn-primary" id="wb-new-btn" type="button">+ New Work</button>
        <button class="wb-btn" id="wb-sync-btn" type="button">⟳ Sync</button>
      </div>
    </div>
    <div class="wb-kpis">
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-needs" style="color:var(--urgent)">{int(counts.get("needs_you", 0))}</div>
        <div class="wb-kpi-lbl">Needs You</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-wait" style="color:var(--accent)">{int(counts.get("waiting", 0))}</div>
        <div class="wb-kpi-lbl">Waiting</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-obs" style="color:var(--ready)">{int(counts.get("obligations_open", 0))}</div>
        <div class="wb-kpi-lbl">Obligations</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-tray" style="color:var(--warn)">{len(tray)}</div>
        <div class="wb-kpi-lbl">Unanswered</div>
      </div>
      <div class="wb-kpi">
        <div class="wb-kpi-val" id="kpi-prec">{precision_pct}%</div>
        <div class="wb-kpi-lbl">14d Precision</div>
      </div>
    </div>
    <div class="wb-filters">
      <button class="wb-chip" data-filter="all" data-base-label="All"
              data-count="{ssr_all_cnt}" data-active="true" type="button">
        All <span class="wb-chip-cnt">({ssr_all_cnt})</span></button>
      <button class="wb-chip" data-filter="needs-you" data-base-label="🚨 Needs You"
              data-count="{ssr_needs_cnt}" type="button">
        🚨 Needs You <span class="wb-chip-cnt">({ssr_needs_cnt})</span></button>
      <button class="wb-chip" data-filter="on-desk" data-base-label="📝 On Desk"
              data-count="{ssr_desk_cnt}" type="button">
        📝 On Desk <span class="wb-chip-cnt">({ssr_desk_cnt})</span></button>
      <button class="wb-chip" data-filter="waiting" data-base-label="⏳ Waiting"
              data-count="{ssr_wait_cnt}" type="button">
        ⏳ Waiting <span class="wb-chip-cnt">({ssr_wait_cnt})</span></button>
      <button class="wb-chip" data-filter="done" data-base-label="✅ Done"
              data-count="{ssr_done_cnt}" type="button">
        ✅ Done <span class="wb-chip-cnt">({ssr_done_cnt})</span></button>
      <div class="wb-filter-tools">
        <select class="wb-sort-select" id="wb-sort-mode" aria-label="Sort cards">
          <option value="triage">⚡ Triage</option>
          <option value="activity">🕒 Activity</option>
          <option value="age">⏳ Age</option>
        </select>
        <button class="wb-btn wb-sort-dir" id="wb-sort-dir" type="button" data-dir="desc"
                title="Triage: Needs You &amp; urgent first, newest activity">↓ New</button>
        <button class="wb-btn wb-btn-icon" id="wb-toggle-collapse" type="button"
                data-action="collapse" aria-label="⇈ Collapse visible cards"
                title="⇈ Collapse visible cards">⇈</button>
      </div>
    </div>
    <div class="wb-type-filters">
      <span class="wb-filter-lbl">Focus:</span>
      <button class="wb-chip" data-type="all" data-base-label="All types"
              data-count="{ssr_all_cnt}" data-active="true" type="button">
        All types <span class="wb-chip-cnt">({ssr_all_cnt})</span></button>
      <button class="wb-chip" data-type="replies" data-base-label="📨 Replies"
              data-count="{ssr_replies_cnt}" type="button">
        📨 Replies <span class="wb-chip-cnt">({ssr_replies_cnt})</span></button>
      <button class="wb-chip" data-type="reviews" data-base-label="👀 Reviews"
              data-count="{ssr_reviews_cnt}" type="button">
        👀 Reviews <span class="wb-chip-cnt">({ssr_reviews_cnt})</span></button>
      <button class="wb-chip" data-type="my-bugs" data-base-label="🐛 My Bugs"
              data-count="{ssr_bugs_cnt}" type="button">
        🐛 My Bugs <span class="wb-chip-cnt">({ssr_bugs_cnt})</span></button>
      <button class="wb-chip" data-type="my-cls" data-base-label="🔧 My CLs"
              data-count="{ssr_cls_cnt}" type="button">
        🔧 My CLs <span class="wb-chip-cnt">({ssr_cls_cnt})</span></button>
      <input class="wb-search" id="wb-search" type="search"
             placeholder="Filter works, CLs, bugs...">
    </div>
  </header>

  <main class="wb-main">
    <form class="wb-form" id="wb-new-form">
      <input class="wb-input" id="wb-new-title" placeholder="Work title (e.g. Renzo Flutter strut metrics follow-up)" required>
      <input class="wb-input" id="wb-new-primary" placeholder="Primary artifacts (comma-separated, e.g. gerrit:1414545, flutter#50168)">
      <input class="wb-input" id="wb-new-channel" placeholder="Channels (comma-separated, e.g. gmail_draft:r2068112471352956917)">
      <input class="wb-input" id="wb-new-oblige" placeholder="Optional obligation (e.g. Reply to Renzo once rolled)">
      <input class="wb-input" id="wb-new-when" placeholder="Trigger (e.g. rolled_into(Flutter, gerrit:skia:1414545) or always)">
      <div class="wb-actions" style="justify-content:flex-end">
        <button class="wb-btn" id="wb-cancel-new" type="button">Cancel</button>
        <button class="wb-btn wb-btn-primary" type="submit">Create Work</button>
      </div>
    </form>

    <section id="wb-tray-container"></section>
    <section id="wb-cards-container">{ssr_body}</section>
  </main>

<script>
(() => {{
  const UI_VERSION = {int(ui_version)};
  const JETSKI_BOX_URL = {jetski_box_json};
  // Bump storage key to v2 so legacy v1 global bulk flags cannot overwrite lane defaults
  const COLLAPSE_STORAGE_KEY = 'wb_collapsed_v2';
  // Persist sort mode and direction separately from per-card collapse overrides
  const SORT_STORAGE_KEY = 'wb_sort_v1';
  // Persist deliverable peer fold modes and per-peer overrides across 30s state refreshes
  const PEER_FOLD_STORAGE_KEY = 'wb_peer_fold_v1';
  // Persist the Черновики fold separately; drafts start collapsed until she opens them
  const DRAFTS_STORAGE_KEY = 'wb_drafts_v1';
  let draftsOpen = false;
  let state = {initial_json};
  let activeFilter = 'all';
  let activeTypeFilter = 'all';
  let searchQuery = '';
  let sortMode = 'triage';
  let sortDir = 'desc';
  let collapsedOverrides = {{}};
  let peerFoldModes = {{}};
  let peerOpenOverrides = {{}};
  // Keep search-time toggles in memory so clearing search restores saved card states
  let searchCollapsedOverrides = {{}};
  // Track open inline reply-draft editors per card across periodic projection refreshes
  let openDraftEditors = {{}};

  function unescapeHtml(s) {{
    const ta = document.createElement('textarea');
    ta.innerHTML = String(s || '');
    return ta.value;
  }}

  function detectDraftFormat(s) {{
    return /<\\/?(?:p|div|ul|ol|li|br|pre|a|b|strong|i|em|code|blockquote|h[1-6])\\b/i.test(
      String(s || '')
    ) ? 'html' : 'text';
  }}

  function linkifyPlainDraft(raw) {{
    const urlRe = /https?:\\/\\/[^\\s<>"']+/g;
    let out = '';
    let last = 0;
    let m;
    while ((m = urlRe.exec(raw)) !== null) {{
      out += esc(raw.slice(last, m.index));
      let u = m[0];
      let suffix = '';
      while (u.length > 0) {{
        const ch = u[u.length - 1];
        const openP = (u.match(/\\(/g) || []).length;
        const closeP = (u.match(/\\)/g) || []).length;
        if ('.,;:!?]'.includes(ch) || (ch === ')' && closeP > openP)) {{
          suffix = ch + suffix;
          u = u.slice(0, -1);
        }} else {{
          break;
        }}
      }}
      if (u) {{
        const eu = esc(u);
        out += `<a href="${{eu}}" target="_blank" rel="noopener">${{eu}}</a>`;
      }}
      out += esc(suffix);
      last = m.index + m[0].length;
    }}
    out += esc(raw.slice(last));
    return out.replace(/\\n{{2,}}/g, '\\n').trim().replace(/\\n/g, '<br>');
  }}

  // Strip arbitrary HTML tags from draft previews while keeping safe https?:// links clickable
  function renderDraftPreview(rd) {{
    if (!rd || !rd.body) return '';
    const raw = String(rd.body).trim();
    if (!raw) return '';
    const fmt = (String(rd.format || '').toLowerCase() === 'html'
      || detectDraftFormat(raw) === 'html') ? 'html' : 'text';
    if (fmt === 'text') return linkifyPlainDraft(raw);
    const anchors = [];
    const noScript = raw.replace(/<(?:script|style)\\b[^>]*>[\\s\\S]*?<\\/(?:script|style)>/gi, '');
    const stashed = noScript.replace(
      /<a\\s+[^>]*href=["'](https?:\\/\\/[^"'<>]+)["'][^>]*>([\\s\\S]*?)<\\/a>/gi,
      (_, href, inner) => {{
        const cleanHref = unescapeHtml(String(href || '').trim());
        if (!/^https?:\\/\\//i.test(cleanHref)) return inner || '';
        const strippedInner = String(inner || '').replace(/<[^>]+>/g, '').trim();
        const cleanLbl = unescapeHtml(strippedInner) || cleanHref;
        const idx = anchors.length;
        anchors.push(
          `<a href="${{esc(cleanHref)}}" target="_blank" rel="noopener">${{esc(cleanLbl)}}</a>`
        );
        return `@@WB_LINK_${{idx}}@@`;
      }}
    );
    const stripped = stashed
      .replace(/<\\/?(?:p|div|li|ul|ol|br|pre|h[1-6])\\b[^>]*>/gi, '\\n')
      .replace(/<[^>]+>/g, '');
    let out = esc(unescapeHtml(stripped))
      .replace(/\\n{{2,}}/g, '\\n').trim().replace(/\\n/g, '<br>');
    anchors.forEach((safeA, idx) => {{
      out = out.split(`@@WB_LINK_${{idx}}@@`).join(safeA);
    }});
    return out;
  }}
  try {{
    const raw = localStorage.getItem(COLLAPSE_STORAGE_KEY);
    if (raw) {{
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed === 'object') {{
        collapsedOverrides = parsed.overrides || {{}};
      }}
    }}
  }} catch {{}}

  // Restore user sort preferences across reloads while rejecting unknown enum values
  try {{
    const rawSort = localStorage.getItem(SORT_STORAGE_KEY);
    if (rawSort) {{
      const parsedSort = JSON.parse(rawSort);
      if (parsedSort && typeof parsedSort === 'object') {{
        if (['triage', 'activity', 'age'].includes(parsedSort.mode)) {{
          sortMode = parsedSort.mode;
        }}
        if (['desc', 'asc'].includes(parsedSort.dir)) {{
          sortDir = parsedSort.dir;
        }}
      }}
    }}
  }} catch {{}}

  try {{
    const rawDrafts = localStorage.getItem(DRAFTS_STORAGE_KEY);
    if (rawDrafts) draftsOpen = JSON.parse(rawDrafts).open === true;
  }} catch {{}}

  function saveDraftsPrefs() {{
    try {{
      localStorage.setItem(DRAFTS_STORAGE_KEY, JSON.stringify({{ open: draftsOpen }}));
    }} catch {{}}
  }}

  // Persist active sort mode and direction to localStorage without failing in private contexts
  function saveSortPrefs() {{
    try {{
      localStorage.setItem(
        SORT_STORAGE_KEY,
        JSON.stringify({{ mode: sortMode, dir: sortDir }})
      );
    }} catch {{}}
  }}

  // Sync the sort select, direction pill label, and tooltip with current sort state
  function updateSortControlsUI() {{
    const modeEl = document.getElementById('wb-sort-mode');
    if (modeEl && modeEl.value !== sortMode) modeEl.value = sortMode;
    const dirEl = document.getElementById('wb-sort-dir');
    if (!dirEl) return;
    dirEl.setAttribute('data-dir', sortDir);
    const lbl = sortDir === 'desc' ? '↓ New' : '↑ Old';
    if (dirEl.textContent !== lbl) dirEl.textContent = lbl;
    let tip = 'Triage: Needs You & urgent first, newest activity';
    if (sortMode === 'triage' && sortDir === 'asc') {{
      tip = 'Triage: Needs You & urgent first, oldest activity';
    }} else if (sortMode === 'activity') {{
      tip = sortDir === 'desc'
        ? 'Activity: most recent activity first'
        : 'Activity: quietest / oldest activity first';
    }} else if (sortMode === 'age') {{
      tip = sortDir === 'asc'
        ? 'Age: longest-open first (oldest origin)'
        : 'Age: recently opened first (newest origin)';
    }}
    dirEl.setAttribute('title', tip);
    dirEl.setAttribute('aria-label', tip);
  }}

  function saveCollapsePrefs() {{
    try {{
      localStorage.setItem(
        COLLAPSE_STORAGE_KEY,
        JSON.stringify({{ overrides: collapsedOverrides }})
      );
    }} catch {{}}
  }}

  function isSearchActive() {{
    return Boolean(searchQuery && searchQuery.trim());
  }}

  function defaultCardCollapsed(w) {{
    return (w && w.lane) !== 'needs-you';
  }}

  function isCardCollapsed(w) {{
    const wid = String((w && w.work_id) || '');
    if (isSearchActive()) {{
      if (wid && Object.prototype.hasOwnProperty.call(searchCollapsedOverrides, wid)) {{
        return Boolean(searchCollapsedOverrides[wid]);
      }}
      // Auto-expand search hits so matched fields in Waiting/Done are visible immediately
      return false;
    }}
    if (wid && Object.prototype.hasOwnProperty.call(collapsedOverrides, wid)) {{
      return Boolean(collapsedOverrides[wid]);
    }}
    return defaultCardCollapsed(w);
  }}

  function setCardCollapsed(w, collapsed) {{
    const wid = String((w && w.work_id) || '');
    if (!wid) return;
    const next = Boolean(collapsed);
    if (isSearchActive()) {{
      if (!next) {{
        delete searchCollapsedOverrides[wid];
      }} else {{
        searchCollapsedOverrides[wid] = true;
      }}
      return;
    }}
    // Drop redundant entries matching lane defaults so smart defaults stay intact
    if (next === defaultCardCollapsed(w)) {{
      delete collapsedOverrides[wid];
    }} else {{
      collapsedOverrides[wid] = next;
    }}
  }}

  // Prefer precomputed backend urgency flag, falling back to live obligation status inspection
  function hasUrgentOb(w) {{
    if (w && typeof w.has_urgent_obligation === 'boolean') {{
      return w.has_urgent_obligation;
    }}
    return Boolean(
      w && Array.isArray(w.obligations) && w.obligations.some(
        (o) => o && o.status !== 'done' && (o.status === 'ready' || o.stalled_alert)
      )
    );
  }}

  // Read positive domain activity seconds, returning 0 for missing or non-finite values
  function getActivityTs(w) {{
    const v = Number((w && (w.activity_at ?? w.updated_at)) || 0);
    return Number.isFinite(v) && v > 0 ? v : 0;
  }}

  // Read positive work origin seconds, returning 0 for missing or non-finite values
  function getOriginTs(w) {{
    const v = Number((w && (w.origin_at ?? w.created_at)) || 0);
    return Number.isFinite(v) && v > 0 ? v : 0;
  }}

  // Keep missing or zero timestamps at the bottom regardless of sort direction
  function cmpTimestamps(ta, tb, dir) {{
    if (ta <= 0 && tb <= 0) return 0;
    if (ta <= 0) return 1;
    if (tb <= 0) return -1;
    if (ta === tb) return 0;
    return dir === 'asc' ? (ta - tb) : (tb - ta);
  }}

  // Order a shallow copy of cards by Triage priority, pure Activity recency, or origin Age
  function sortWorks(items) {{
    const laneRank = {{ 'needs-you': 0, 'on-desk': 1, waiting: 2, blocked: 3, done: 4 }};
    return items.slice().sort((a, b) => {{
      if (sortMode === 'triage') {{
        const la = laneRank[a && a.lane] ?? 9;
        const lb = laneRank[b && b.lane] ?? 9;
        if (la !== lb) return la - lb;
        const ua = hasUrgentOb(a) ? 0 : 1;
        const ub = hasUrgentOb(b) ? 0 : 1;
        if (ua !== ub) return ua - ub;
        // Sub-rank On Desk cards by tier (0=no-reviewer, 1=WIP, 2=DO NOT SUBMIT probe)
        if (a && a.lane === 'on-desk') {{
          const da = Number(a.desk_tier ?? 1);
          const db = Number(b.desk_tier ?? 1);
          if (da !== db) return da - db;
        }}
        const tc = cmpTimestamps(getActivityTs(a), getActivityTs(b), sortDir);
        if (tc !== 0) return tc;
      }} else if (sortMode === 'activity') {{
        const tc = cmpTimestamps(getActivityTs(a), getActivityTs(b), sortDir);
        if (tc !== 0) return tc;
      }} else if (sortMode === 'age') {{
        // Break equal creation ages by most/least recent activity in the same direction
        const oc = cmpTimestamps(getOriginTs(a), getOriginTs(b), sortDir);
        if (oc !== 0) return oc;
        const ac = cmpTimestamps(getActivityTs(a), getActivityTs(b), sortDir);
        if (ac !== 0) return ac;
      }}
      const wa = String((a && a.work_id) || '');
      const wb = String((b && b.work_id) || '');
      return wa < wb ? -1 : (wa > wb ? 1 : 0);
    }});
  }}

  // Partition works into active lanes ('all', 'needs-you', 'on-desk', 'waiting') vs 'done'
  function matchesLane(w, laneFilter) {{
    const lane = (w && w.lane) || '';
    if (laneFilter === 'done') return lane === 'done';
    // Hide completed works outside the Done tab so active lanes stay focused
    if (lane === 'done') return false;
    if (laneFilter === 'needs-you') return lane === 'needs-you';
    if (laneFilter === 'on-desk') return lane === 'on-desk';
    if (laneFilter === 'waiting') return lane === 'waiting';
    return true;
  }}

  // Scope Needs You slices to urgent artifact types while All/Waiting/Done slice by membership
  function matchesTypeForLane(w, laneFilter, typeFilter) {{
    if (!typeFilter || typeFilter === 'all') return true;
    const workTypes = Array.isArray(w && w.work_types) ? w.work_types : [];
    if (laneFilter === 'needs-you' && typeFilter !== 'my-bugs') {{
      const needsTypes = Array.isArray(w && w.needs_you_types) ? w.needs_you_types : workTypes;
      return needsTypes.includes(typeFilter);
    }}
    return workTypes.includes(typeFilter);
  }}

  // Evaluate whether card w belongs to a (laneFilter, typeFilter, query) slice
  function cardMatchesSlice(w, laneFilter, typeFilter, q) {{
    if (!matchesLane(w, laneFilter)) return false;
    if (!matchesTypeForLane(w, laneFilter, typeFilter)) return false;
    // Match against precomputed search_text to avoid O(cards * chips) JSON.stringify passes
    if (q) {{
      const hay = (w && typeof w.search_text === 'string')
        ? w.search_text
        : JSON.stringify(w).toLowerCase();
      if (!hay.includes(q)) return false;
    }}
    return true;
  }}

  // Refresh active states and live (N) badges across both Lane and Type filter chip rows
  function updateFilterChipsUI() {{
    const allWorks = (state && state.works) || [];
    const q = searchQuery ? searchQuery.trim().toLowerCase() : '';
    for (const chip of document.querySelectorAll('.wb-chip')) {{
      const laneAttr = chip.getAttribute('data-filter');
      const typeAttr = chip.getAttribute('data-type');
      let cnt = 0;
      if (typeAttr !== null) {{
        chip.setAttribute('data-active', typeAttr === activeTypeFilter ? 'true' : 'false');
        for (const w of allWorks) {{
          if (cardMatchesSlice(w, activeFilter, typeAttr, q)) cnt += 1;
        }}
      }} else if (laneAttr !== null) {{
        chip.setAttribute('data-active', laneAttr === activeFilter ? 'true' : 'false');
        for (const w of allWorks) {{
          if (cardMatchesSlice(w, laneAttr, activeTypeFilter, q)) cnt += 1;
        }}
      }} else {{
        continue;
      }}
      // Skip redundant DOM attribute and innerHTML writes when chip counts are unchanged
      const prevCnt = chip.getAttribute('data-count');
      const nextCnt = String(cnt);
      if (prevCnt !== nextCnt) {{
        chip.setAttribute('data-count', nextCnt);
      }}
      const baseLbl = chip.getAttribute('data-base-label');
      if (baseLbl) {{
        const nextHtml = `${{esc(baseLbl)}} <span class="wb-chip-cnt">(${{cnt}})</span>`;
        if (chip.innerHTML !== nextHtml) {{
          chip.innerHTML = nextHtml;
        }}
      }}
    }}
  }}

  // Return the visible cards matching the active Lane chip, Type chip, search input, and sort mode
  function getVisibleWorks() {{
    const q = searchQuery ? searchQuery.trim().toLowerCase() : '';
    const filtered = ((state && state.works) || []).filter(
      (w) => cardMatchesSlice(w, activeFilter, activeTypeFilter, q)
    );
    return sortWorks(filtered);
  }}

  function updateCollapseToggleUI(works) {{
    const btn = document.getElementById('wb-toggle-collapse');
    if (!btn) return;
    let expandedCount = 0;
    let collapsedCount = 0;
    for (const w of works) {{
      if (isCardCollapsed(w)) {{
        collapsedCount += 1;
      }} else {{
        expandedCount += 1;
      }}
    }}
    const totalCount = expandedCount + collapsedCount;
    // Collapse-first: collapse whenever any visible card is open
    const shouldCollapse = totalCount === 0 || expandedCount > 0;
    const action = shouldCollapse ? 'collapse' : 'expand';
    const icon = shouldCollapse ? '⇈' : '⇊';
    let label = '⇈ Collapse visible cards';
    if (totalCount > 0) {{
      label = shouldCollapse
        ? `⇈ Collapse visible cards (${{expandedCount}}/${{totalCount}} open)`
        : `⇊ Expand visible cards (${{collapsedCount}} collapsed)`;
    }}
    btn.disabled = totalCount === 0;
    btn.setAttribute('data-action', action);
    btn.setAttribute('data-expanded-count', String(expandedCount));
    btn.setAttribute('data-collapsed-count', String(collapsedCount));
    btn.setAttribute('aria-label', label);
    btn.setAttribute('title', label);
    if (btn.textContent !== icon) btn.textContent = icon;
  }}

  function notifyParentRendered() {{
    try {{
      if (window.parent && window.parent !== window) {{
        const synced = (state && state.synced_et) || '';
        window.parent.postMessage(
          {{ type: 'jetski-flight-deck-rendered', uiVersion: UI_VERSION, synced }},
          '*'
        );
        window.parent.postMessage(
          {{ type: 'jetski-workbench-rendered', uiVersion: UI_VERSION, synced }},
          '*'
        );
      }}
    }} catch {{}}
  }}

  window.addEventListener('message', (ev) => {{
    const d = ev && ev.data;
    if (d && (d.type === 'jetski-flight-deck-ping' || d.type === 'jetski-workbench-ping')) {{
      notifyParentRendered();
    }}
  }});
  // Bind SSR filter chips upfront so clicks respond before full card-body setup finishes
  for (const chip of document.querySelectorAll('.wb-chip')) {{
    chip.addEventListener('click', () => {{
      const typeAttr = chip.getAttribute('data-type');
      const laneAttr = chip.getAttribute('data-filter');
      if (typeAttr !== null) {{
        activeTypeFilter = typeAttr || 'all';
      }} else if (laneAttr !== null) {{
        activeFilter = laneAttr || 'all';
      }}
      render();
    }});
  }}

  function esc(s) {{
    const d = document.createElement('div');
    d.textContent = String(s || '');
    return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }}

  // Carry collapse and peer-fold overrides across virt-<key> -> work-<hash> promotion
  function migrateVirtualCardPrefs(oldWid, newWid) {{
    if (!oldWid || !newWid || oldWid === newWid) return;
    let movedCollapse = false;
    if (Object.prototype.hasOwnProperty.call(collapsedOverrides, oldWid)) {{
      collapsedOverrides[newWid] = collapsedOverrides[oldWid];
      delete collapsedOverrides[oldWid];
      movedCollapse = true;
    }}
    if (Object.prototype.hasOwnProperty.call(searchCollapsedOverrides, oldWid)) {{
      searchCollapsedOverrides[newWid] = searchCollapsedOverrides[oldWid];
      delete searchCollapsedOverrides[oldWid];
    }}
    if (movedCollapse) saveCollapsePrefs();

    let movedFold = false;
    if (Object.prototype.hasOwnProperty.call(peerFoldModes, oldWid)) {{
      peerFoldModes[newWid] = peerFoldModes[oldWid];
      delete peerFoldModes[oldWid];
      movedFold = true;
    }}
    const oldPrefix = `${{oldWid}}::`;
    for (const k of Object.keys(peerOpenOverrides)) {{
      if (k.startsWith(oldPrefix)) {{
        peerOpenOverrides[`${{newWid}}::${{k.slice(oldPrefix.length)}}`] = peerOpenOverrides[k];
        delete peerOpenOverrides[k];
        movedFold = true;
      }}
    }}
    if (movedFold) savePeerFoldPrefs();
  }}

  async function postJson(url, body) {{
    try {{
      const resp = await fetch(url, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(body || {{}}),
      }});
      if (!resp.ok) {{
        let errMsg = `Request failed (HTTP ${{resp.status}})`;
        try {{
          const errData = await resp.json();
          if (errData && errData.error) errMsg = String(errData.error);
        }} catch (_) {{}}
        window.alert(errMsg);
        return false;
      }}
      state = await resp.json();
      if (body && body.work_id && state && state.started_chat && state.started_chat.work_id) {{
        migrateVirtualCardPrefs(String(body.work_id), String(state.started_chat.work_id));
      }}
      render();
      lastRenderedSig = projectionRenderSignature(state);
      return true;
    }} catch (err) {{
      window.alert(`Network error: ${{err && err.message ? err.message : err}}`);
      return false;
    }}
  }}

  // Open a Jetski conversation via iframe postMessage or standalone /api/chat/open + named tab
  async function openChatInJetski(cid, fallbackUrl) {{
    if (!cid) return;
    const defaultUrl = fallbackUrl || `${{JETSKI_BOX_URL}}/c/${{encodeURIComponent(cid)}}`;
    // Embedded sidecar notifies its parent frame immediately and writes the non-blocking IPC file
    if (window.parent && window.parent !== window) {{
      window.parent.postMessage(
        {{ type: 'jetski-flight-deck-open-chat', cascadeId: cid, chatUrl: defaultUrl }},
        '*'
      );
      fetch('/api/chat/open', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ conversation_id: cid, timeout_sec: 0.0 }}),
      }}).catch(() => {{}});
      return;
    }}
    let targetUrl = defaultUrl;
    try {{
      const resp = await fetch('/api/chat/open', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ conversation_id: cid }}),
      }});
      const data = resp.ok ? await resp.json() : null;
      if (data && data.reused_tab) return;
      if (data && data.chat_url) targetUrl = data.chat_url;
    }} catch (_) {{}}
    // Fall back to location.assign if browser popup blockers reject async window.open
    const win = window.open(targetUrl, 'jetski-chat');
    if (!win && window.location && typeof window.location.assign === 'function') {{
      window.location.assign(targetUrl);
    }}
  }}

  const SVG_CHEVRON = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M6 4l4 4-4 4"/></svg>';
  const SVG_CHEVRON_SM = '<svg width="10" height="10" viewBox="0 0 16 16" fill="none" '
    + 'stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M6 4l4 4-4 4"/></svg>';

  try {{
    const rawFold = localStorage.getItem(PEER_FOLD_STORAGE_KEY);
    if (rawFold) {{
      const parsedFold = JSON.parse(rawFold);
      if (parsedFold && typeof parsedFold === 'object') {{
        peerFoldModes = parsedFold.modes || {{}};
        peerOpenOverrides = parsedFold.overrides || {{}};
      }}
    }}
  }} catch {{}}

  // Persist per-card fold mode and per-peer overrides in localStorage
  function savePeerFoldPrefs() {{
    try {{
      localStorage.setItem(
        PEER_FOLD_STORAGE_KEY,
        JSON.stringify({{ modes: peerFoldModes, overrides: peerOpenOverrides }})
      );
    }} catch {{}}
  }}

  const RE_EMOJI_PREFIX =
    /^(?:[\\p{{Extended_Pictographic}}\\u2600-\\u27BF\\uFE0F\\u200D]+\\s*)+/u;
  const RE_ORIGIN_PREFIX =
    /^(?:[\\p{{Extended_Pictographic}}\\u2600-\\u27BF\\uFE0F\\u200D]+\\s*)*Origin:\\s*/iu;
  const RE_CL_TITLE_PREFIX = new RegExp(
    `^(?:${{[
      /(?:gerrit:[^:\\s]+:\\d+|github_pr:[^:\\s]+:\\d+|piper:\\d+)/.source,
      /(?:Gerrit|Skia)\\s+\\d+(?:\\s+PS\\d+)?/.source,
      /PR\\s*#\\d+(?:\\s+PS\\d+)?/.source,
      /\\d{{5,10}}/.source,
    ].join('|')}})${{/(?:\\s*[:·-]\\s*|\\s*$)/.source}}`,
    'i'
  );

  // Strip leading Unicode emoji/pictographs per ' · ' segment while preserving words and quotes
  function cleanEmojiPrefix(s) {{
    const raw = String(s || '').trim();
    const cleaned = raw
      .split(' · ')
      .map((seg) => seg.trim().replace(RE_EMOJI_PREFIX, '').trim() || seg.trim())
      .filter(Boolean)
      .join(' · ');
    return cleaned || raw;
  }}

  // Strip redundant Gerrit/PR/key prefixes from a CL title, falling back to state.subject
  function cleanClSubject(rawTitle, ast, shortLabel) {{
    const raw = String(rawTitle || '').trim();
    const cleaned = raw.replace(RE_CL_TITLE_PREFIX, '').trim();
    if (!cleaned || cleaned.toLowerCase() === String(shortLabel || '').toLowerCase()) {{
      return String((ast && ast.subject) || '').trim();
    }}
    return cleaned;
  }}

  // Split 'Title — Counterparty' only when a verified artifact counterparty exists on the card
  function splitCardHeadline(w) {{
    const rawTitle = String((w && w.title) || '').trim();
    const originKey = String((w && w.origin && w.origin.artifact_key) || '');
    const ranked = [];
    for (const a of ((w && w.artifacts) || [])) {{
      if (!a || a.role === 'context_only') continue;
      const cp = String((a.state && a.state.counterparty) || '').trim();
      if (!cp) continue;
      const akey = String(a.artifact_key || '');
      const akind = String(a.kind || '');
      const p0 = (originKey && akey === originKey) ? 0 : 1;
      const p1 = (akind === 'github_pr' || akind === 'github_issue') ? 1 : 0;
      const p2 = a.role === 'primary' ? 0 : 1;
      ranked.push({{ score: p0 * 100 + p1 * 10 + p2, cp }});
    }}
    ranked.sort((x, y) => x.score - y.score);
    const candidates = [];
    const seen = new Set();
    for (const item of ranked) {{
      const low = item.cp.toLowerCase();
      if (!seen.has(low)) {{
        seen.add(low);
        candidates.push(item.cp);
      }}
    }}
    if (!candidates.length) return {{ headline: rawTitle, counterparty: '' }};
    const bestCp = candidates[0];
    const sepIdx = rawTitle.lastIndexOf(' — ');
    if (sepIdx > 0) {{
      const head = rawTitle.slice(0, sepIdx).trim();
      const tail = rawTitle.slice(sepIdx + 3).trim();
      if (head && tail) {{
        const matched = candidates.find((c) => c.toLowerCase() === tail.toLowerCase());
        if (matched) return {{ headline: head, counterparty: matched }};
      }}
    }}
    return {{ headline: rawTitle, counterparty: bestCp }};
  }}

  // Condense verbose origin badge text into 'Origin · <meta>' while keeping full tooltip
  function compactOriginLabel(o) {{
    const raw = String((o && o.label) || '').trim();
    if (!raw) return '';
    const parenIdx = raw.lastIndexOf('(');
    if (parenIdx > 0 && raw.endsWith(')')) {{
      return 'Origin · ' + raw.slice(parenIdx + 1, -1);
    }}
    return 'Origin · ' + cleanEmojiPrefix(raw.replace(RE_ORIGIN_PREFIX, ''));
  }}

  // Format canonical issue keys into flutter#<N>, <owner>/<repo>#<N>, b/<N>, or crbug/<N>
  function shortIssueId(art) {{
    const akey = String((art && art.artifact_key) || '');
    const parts = akey.split(':');
    const tid = parts[parts.length - 1] || akey;
    if (akey.startsWith('github_issue:') && parts.length >= 3) {{
      return parts[1] !== 'flutter/flutter' ? `${{parts[1]}}#${{tid}}` : `flutter#${{tid}}`;
    }}
    if (akey.startsWith('buganizer:') && parts.length >= 2) {{
      const bPfx = 'b' + '/';
      return bPfx + tid;
    }}
    if (akey.startsWith('crbug:') && parts.length >= 2) {{
      const crPfx = 'crbug' + '/';
      return crPfx + tid;
    }}
    const t = String((art && art.title) || '');
    const idx = t.indexOf(':');
    return (idx > 0 ? t.slice(0, idx).trim() : t.trim()) || akey;
  }}

  // Split CL artifact key and label into compact link text and optional patchset tag
  function shortClParts(akey, fallbackLabel) {{
    const lbl = String(fallbackLabel || '');
    const psMatch = lbl.match(/\\b(PS\\d+)\\b/);
    const psTag = psMatch ? psMatch[1] : '';
    const parts = String(akey || '').split(':');
    if (akey.startsWith('gerrit:') && parts.length >= 3) {{
      const proj = parts[1] === 'skia'
        ? 'Skia'
        : (parts[1].charAt(0).toUpperCase() + parts[1].slice(1));
      return {{ shortLabel: `${{proj}} ${{parts[2]}}`, psTag }};
    }}
    if (akey.startsWith('github_pr:') && parts.length >= 3) {{
      return {{ shortLabel: `PR #${{parts[2]}}`, psTag }};
    }}
    if (akey.startsWith('piper:') && parts.length >= 2) {{
      const pfx = 'cl' + '/';
      return {{ shortLabel: pfx + parts[1], psTag }};
    }}
    const clean = lbl.replace(/\\s+PS\\d+\\b/, '').trim();
    return {{ shortLabel: clean || akey, psTag }};
  }}

  // Extract 5-to-10-digit CL/PR/Piper numeric tokens for cross-linking bugs and CLs
  function extractClDigits(text) {{
    return String(text || '').match(/\\b\\d{{5,10}}\\b/g) || [];
  }}

  // Normalize bug tokens from linked_bug_keys, linked_bugs, bug_ids, and commit_message
  function normalizeBugKeyJs(rawKey) {{
    const s = String(rawKey || '').trim();
    if (!s) return '';
    if (s.startsWith('github_issue:') || s.startsWith('buganizer:') || s.startsWith('crbug:')) {{
      return s;
    }}
    const flMatch = s.match(/^flutter#(\\d+)$/i);
    if (flMatch) return `github_issue:flutter/flutter:${{flMatch[1]}}`;
    const ghMatch = s.match(/^([A-Za-z0-9_.-]+\\/[A-Za-z0-9_.-]+)#(\\d+)$/);
    if (ghMatch) return `github_issue:${{ghMatch[1]}}:${{ghMatch[2]}}`;
    const bPfx = 'b' + '/';
    if (s.startsWith(bPfx) && /^\\d+$/.test(s.slice(2))) return `buganizer:${{s.slice(2)}}`;
    const crPfx = 'crbug' + '/';
    if (s.startsWith(crPfx) && /^\\d+$/.test(s.slice(6))) return `crbug:${{s.slice(6)}}`;
    return s;
  }}

  // Extract canonical bug keys referenced by a CL state across metadata fields and commit message
  function extractClBugKeysJs(ast) {{
    const st = ast || {{}};
    const out = new Set();
    for (const k of (st.linked_bug_keys || [])) {{
      const nk = normalizeBugKeyJs(k);
      if (nk) out.add(nk);
    }}
    for (const lb of (st.linked_bugs || [])) {{
      if (!lb || typeof lb !== 'object') continue;
      if (lb.item_key) out.add(normalizeBugKeyJs(lb.item_key));
      const num = String(lb.number || lb.id || lb.target_id || '').trim();
      const repo = String(lb.repo || 'flutter/flutter').trim();
      if (/^\\d+$/.test(num)) {{
        if (String(lb.kind || '').includes('buganizer')) out.add(`buganizer:${{num}}`);
        else if (String(lb.kind || '').includes('crbug')) out.add(`crbug:${{num}}`);
        else out.add(`github_issue:${{repo}}:${{num}}`);
      }}
    }}
    for (const bid of (st.bug_ids || [])) {{
      const bs = String(bid || '').trim();
      if (/^\\d+$/.test(bs)) {{
        out.add(bs.length <= 6 ? `github_issue:flutter/flutter:${{bs}}` : `buganizer:${{bs}}`);
      }}
    }}
    const cm = String(st.commit_message || '');
    if (cm) {{
      const ghIssueRe = /github\\.com\\/([^/\\s#]+\\/[^/\\s#]+)\\/issues\\/(\\d+)\\b/gi;
      for (const m of cm.matchAll(ghIssueRe)) {{
        out.add(`github_issue:${{m[1]}}:${{m[2]}}`);
      }}
      for (const m of cm.matchAll(/(?:flutter#|(?<![A-Za-z0-9_/])#)(\\d{{4,6}})\\b/gi)) {{
        out.add(`github_issue:flutter/flutter:${{m[1]}}`);
      }}
      const bugUrlRe =
        /(?:(?<![A-Za-z0-9_./-])b\\/|issuetracker\\.google\\.com\\/issues\\/)(\\d{{6,}})\\b/gi;
      for (const m of cm.matchAll(bugUrlRe)) out.add(`buganizer:${{m[1]}}`);
      const crbugRe = /(?:crbug\\.com\\/|issues\\.chromium\\.org\\/issues\\/)(\\d+)\\b/gi;
      for (const m of cm.matchAll(crbugRe)) {{
        out.add(`crbug:${{m[1]}}`);
      }}
    }}
    return out;
  }}

  // Read normalized uppercase Gmail draft status, defaulting to DRAFT when unspecified
  function gmailDraftStatus(a) {{
    return String((a && a.state && a.state.status) || 'DRAFT').toUpperCase();
  }}

  // Return true when the card has a live inline reply draft or an active DRAFT Gmail draft
  function hasParkedDraft(w) {{
    if (w && w.reply_draft && String(w.reply_draft.body || '').trim()) return true;
    return Boolean(
      w && Array.isArray(w.artifacts) && w.artifacts.some(
        (a) => a && a.role !== 'context_only'
          && a.kind === 'gmail_draft' && gmailDraftStatus(a) === 'DRAFT'
      )
    );
  }}

  // Collect canonical keys and numeric CL IDs mentioned in active reply drafts on card w
  function collectDraftMentionedTokens(w) {{
    const keys = new Set();
    const digits = new Set();
    const addText = (txt) => {{
      for (const d of extractClDigits(txt)) digits.add(d);
    }};
    const rd = w && w.reply_draft;
    if (rd) {{
      for (const k of (rd.mentioned_keys || [])) {{
        keys.add(String(k));
        addText(k);
      }}
      addText(rd.subject || '');
      addText(rd.body || '');
    }}
    for (const a of ((w && w.artifacts) || [])) {{
      if (!a || a.role === 'context_only' || a.kind !== 'gmail_draft') continue;
      if (gmailDraftStatus(a) !== 'DRAFT') continue;
      const ast = a.state || {{}};
      for (const k of (ast.mentioned_keys || [])) {{
        keys.add(String(k));
        addText(k);
      }}
      addText(ast.body || ast.snippet || a.title || '');
    }}
    return {{ keys, digits }};
  }}

  // Convert checklist items into baseline peer entries and record covered artifact keys
  function buildChecklistPeers(chkList, artByKey, covered) {{
    const peers = [];
    chkList.forEach((item, idx) => {{
      const bkey = String(item.bug_key || '');
      if (bkey) covered.add(bkey);
      const clTokens = new Set();
      const cls = (item.cls || []).map((c) => {{
        const ckey = String(c.artifact_key || '');
        if (ckey) {{
          covered.add(ckey);
          for (const d of extractClDigits(ckey)) clTokens.add(d);
        }}
        const art = artByKey.get(ckey) || {{}};
        const ast = art.state || {{}};
        const {{ shortLabel, psTag }} = shortClParts(ckey, c.label || ckey);
        const finalPs = psTag || (ast.patchset ? `PS${{ast.patchset}}` : '');
        let whyBadge = String(art.why_badge || '');
        if (item.short_id && whyBadge === `фикс ${{item.short_id}}`) whyBadge = '';
        const mergedRolls = (c.rolls && Object.keys(c.rolls).length)
          ? c.rolls
          : ((ast.post_submit && ast.post_submit.rolls) || ast.rolls || {{}});
        return {{
          artifact_key: ckey,
          shortLabel,
          psTag: finalPs,
          url: c.url || art.url || '#',
          subject: cleanClSubject(c.subject || '', ast, shortLabel),
          status: String(c.status || ast.status || ''),
          is_wip: Boolean(c.is_wip || ast.is_draft),
          wip_label: String(c.wip_label || (ast.is_draft ? 'Draft' : '')),
          merge_conflict: Boolean(c.merge_conflict),
          assigned_reviewers_count: c.assigned_reviewers_count ?? 1,
          in_draft: c.in_draft !== false,
          is_stacked: Boolean(c.is_stacked),
          updated_et: String(c.updated_et || ''),
          rolls: mergedRolls,
          why_badge: whyBadge,
        }};
      }});
      peers.push({{
        peerKey: bkey || `chk:${{idx}}`,
        bugKey: bkey,
        shortId: String(item.short_id || bkey),
        title: String(item.title || ''),
        url: String(item.url || '#'),
        bugStatus: String(item.bug_status || 'OPEN'),
        stage: String(item.stage || ''),
        stageTone: String(item.stage_tone || 'muted'),
        stageLabel: cleanEmojiPrefix(item.stage_label || item.stage || ''),
        draftOutdated: Boolean(item.draft_outdated),
        extraIssues: [],
        cls,
        clTokens,
        fromChecklist: true,
      }});
    }});
    return peers;
  }}

  // Attach companion bugs sharing a CL onto existing peers or promote allowed bug roles
  function attachOrPromoteBugs(arts, artByKey, peers, covered, promoteRoles) {{
    const bugKinds = ['github_issue', 'buganizer', 'crbug'];
    for (const a of arts) {{
      if (!a || a.role === 'context_only') continue;
      const akey = String(a.artifact_key || '');
      if (!akey || covered.has(akey) || !bugKinds.includes(a.kind)) continue;
      const ast = a.state || {{}};
      const refTokens = new Set(extractClDigits(a.why_badge || ''));
      for (const lc of (ast.linked_cls || [])) {{
        if (!lc) continue;
        for (const d of extractClDigits(`${{lc.target_id || ''}} ${{lc.item_key || ''}}`)) {{
          refTokens.add(d);
        }}
      }}
      let matchedPeer = null;
      for (const p of peers) {{
        for (const tok of refTokens) {{
          if (p.clTokens.has(tok)) {{
            matchedPeer = p;
            break;
          }}
        }}
        if (matchedPeer) break;
        for (const c of p.cls) {{
          const clArt = artByKey.get(c.artifact_key);
          const lbugs = extractClBugKeysJs(clArt && clArt.state);
          if (lbugs.has(akey)) {{
            matchedPeer = p;
            break;
          }}
        }}
        if (matchedPeer) break;
      }}
      if (matchedPeer) {{
        matchedPeer.extraIssues.push({{
          artifact_key: akey,
          shortId: shortIssueId(a),
          url: String(a.url || '#'),
          title: String(a.title || ''),
        }});
        covered.add(akey);
      }} else if (promoteRoles.includes(a.role)) {{
        const sid = shortIssueId(a);
        const rawT = String(a.title || '');
        const colonIdx = rawT.indexOf(':');
        const cleanT = colonIdx > 0 ? rawT.slice(colonIdx + 1).trim() : rawT;
        const stStr = String(ast.status || 'Open');
        peers.push({{
          peerKey: akey,
          bugKey: akey,
          shortId: sid,
          title: cleanT,
          url: String(a.url || '#'),
          bugStatus: stStr,
          stage: 'open',
          stageTone: 'muted',
          stageLabel: cleanEmojiPrefix(stStr),
          draftOutdated: false,
          extraIssues: [],
          cls: [],
          clTokens: new Set(),
          fromChecklist: false,
        }});
        covered.add(akey);
      }}
    }}
  }}

  // Return true when a Gerrit CL or GitHub PR status is still open
  function isOpenClStatus(st) {{
    const u = String(st || '').trim().toUpperCase();
    return u === 'NEW' || u === 'OPEN';
  }}

  // Classify an open CL/PR state into 'dns', 'wip', 'norev', or '' matching SSR peer badges
  function clDeskKind(ast, title) {{
    const stObj = ast || {{}};
    if (!isOpenClStatus(stObj.status || 'OPEN')) return '';
    const raw = String(stObj.subject || title || '').trim();
    const tail = raw.replace(/^(?:\\d+\\s*:\\s*)?(?:\\[[^\\]]*\\]\\s*)*/, '');
    if (/DO NOT SUBMIT/i.test(raw) || /^CONTROL:/i.test(tail)) return 'dns';
    const est = (stObj.entity_state && typeof stObj.entity_state === 'object')
      ? stObj.entity_state
      : {{}};
    if (stObj.is_wip || stObj.is_draft || stObj.wip_label || est.status === 'draft') return 'wip';
    const explicitZero = (
      stObj.assigned_reviewers_count !== undefined
      && stObj.assigned_reviewers_count !== null
      && Number(stObj.assigned_reviewers_count) === 0
    );
    // Honor lane==='desk' only on status-less stubs so explicit waiting/conflict states hold
    const deskStub = !est.status && est.lane === 'desk';
    if (stObj.in_development || est.status === 'dev' || deskStub || explicitZero) {{
      return 'norev';
    }}
    return '';
  }}

  // Recompute stage badges on promoted non-checklist bug peers once uncovered CLs attach
  function refreshNonChecklistPeerStages(peers) {{
    for (const p of peers) {{
      if (p.fromChecklist || !p.bugKey || !p.cls.length) continue;
      if (p.cls.some((c) => c.merge_conflict)) {{
        p.stage = 'wip';
        p.stageTone = 'urgent';
        p.stageLabel = 'Merge conflict';
      }} else if (p.cls.every((c) => c.status === 'MERGED' || c.status === 'SUBMITTED')) {{
        p.stage = 'merged';
        p.stageTone = 'ok';
        p.stageLabel = 'Merged';
      }} else if (p.cls.some((c) => c.is_wip || c.desk_kind === 'wip')) {{
        p.stage = 'wip';
        p.stageTone = 'warn';
        p.stageLabel = 'WIP';
      }} else if (p.cls.some((c) => c.desk_kind === 'dns')) {{
        p.stage = 'on_desk';
        p.stageTone = 'warn';
        p.stageLabel = 'DO NOT SUBMIT';
      }} else if (p.cls.some((c) => c.desk_kind === 'norev')) {{
        p.stage = 'on_desk';
        p.stageTone = 'warn';
        p.stageLabel = 'No reviewers';
      }} else {{
        p.stage = 'review';
        p.stageTone = 'wait';
        p.stageLabel = 'In review';
      }}
    }}
  }}

  // Attach uncovered primary/channel CL, PR, and Piper artifacts onto peers or promote them
  function attachUncoveredCls(w, arts, peers, covered, hasDraft, draftTokens) {{
    const clKinds = ['gerrit', 'github_pr', 'piper'];
    const uncoveredCls = arts
      .filter((a) => a && a.role !== 'context_only'
        && clKinds.includes(a.kind) && !covered.has(String(a.artifact_key || '')))
      .sort((a, b) => (a.role === 'primary' ? 0 : 1) - (b.role === 'primary' ? 0 : 1));

    for (const a of uncoveredCls) {{
      const akey = String(a.artifact_key || '');
      if (!akey || covered.has(akey)) continue;
      const ast = a.state || {{}};
      const rawTitle = String(a.title || '');
      const {{ shortLabel, psTag }} = shortClParts(akey, rawTitle || akey);
      const finalPs = psTag || (ast.patchset ? `PS${{ast.patchset}}` : '');
      const subj = cleanClSubject(rawTitle, ast, shortLabel);
      const whyBadge = String(a.why_badge || '');
      const mergedRolls = (ast.post_submit && ast.post_submit.rolls) || ast.rolls || {{}};
      const deskKind = clDeskKind(ast, rawTitle);
      const isWip = Boolean(ast.is_wip || ast.is_draft || deskKind === 'wip');
      const wipLbl = String(
        ast.wip_label || (ast.is_draft ? 'Draft' : (isWip ? 'WIP' : ''))
      );
      const keyDigits = extractClDigits(akey);
      const inDraft = !hasDraft
        || draftTokens.keys.has(akey)
        || keyDigits.some((d) => draftTokens.digits.has(d));
      const stStr = String(ast.status || 'OPEN');
      const clEntry = {{
        artifact_key: akey,
        shortLabel,
        psTag: finalPs,
        url: String(a.url || '#'),
        subject: subj,
        status: stStr,
        is_wip: isWip,
        wip_label: wipLbl,
        desk_kind: deskKind,
        merge_conflict: Boolean(ast.merge_conflict),
        assigned_reviewers_count: ast.assigned_reviewers_count ?? (deskKind === 'norev' ? 0 : 1),
        in_draft: inDraft,
        is_stacked: Boolean(ast.is_stacked),
        updated_et: String(ast.updated_et || ast.status_changed_et || ast.last_msg_et || ''),
        rolls: mergedRolls,
        why_badge: whyBadge,
      }};
      const lbugs = extractClBugKeysJs(ast);
      const whyTokens = extractClDigits(whyBadge);
      let targetPeer = null;
      for (const p of peers) {{
        if (p.bugKey && lbugs.has(p.bugKey)) {{
          targetPeer = p;
          break;
        }}
        if (p.shortId && whyBadge.includes(p.shortId)) {{
          targetPeer = p;
          break;
        }}
        if (whyTokens.some((tok) => p.clTokens.has(tok))) {{
          targetPeer = p;
          break;
        }}
      }}
      if (!targetPeer && a.role === 'primary') {{
        const openBugPeers = peers.filter(
          (p) => p.bugKey && p.bugStatus.toUpperCase() !== 'CLOSED'
        );
        if (openBugPeers.length === 1) targetPeer = openBugPeers[0];
      }}
      if (targetPeer) {{
        targetPeer.cls.push(clEntry);
        for (const d of keyDigits) targetPeer.clTokens.add(d);
      }} else {{
        const isDoneSt = stStr === 'MERGED' || stStr === 'SUBMITTED';
        let sTone = 'wait';
        let sLbl = 'In review';
        if (ast.merge_conflict) {{
          sTone = 'urgent';
          sLbl = 'Merge conflict';
        }} else if (isDoneSt) {{
          sTone = 'ok';
          sLbl = 'Merged';
        }} else if (deskKind === 'dns') {{
          sTone = 'warn';
          sLbl = 'DO NOT SUBMIT';
        }} else if (isWip) {{
          sTone = 'warn';
          sLbl = wipLbl || 'WIP';
        }} else if (deskKind === 'norev') {{
          sTone = 'warn';
          sLbl = 'No reviewers';
        }}
        peers.push({{
          peerKey: akey,
          bugKey: '',
          shortId: shortLabel,
          title: subj,
          url: String(a.url || '#'),
          bugStatus: 'OPEN',
          stage: stStr.toLowerCase(),
          stageTone: sTone,
          stageLabel: sLbl,
          draftOutdated: false,
          extraIssues: [],
          cls: [clEntry],
          clTokens: new Set(keyDigits),
          fromChecklist: false,
        }});
      }}
      covered.add(akey);
    }}
  }}

  // Run 4-phase peer assembly across checklist rows, primary bugs, CLs, and companion bugs
  function buildDeliverablePeers(w, hasDraft) {{
    const arts = (w && w.artifacts) || [];
    const artByKey = new Map();
    for (const a of arts) {{
      if (a && a.artifact_key) artByKey.set(String(a.artifact_key), a);
    }}
    const activeDraft = typeof hasDraft === 'boolean' ? hasDraft : hasParkedDraft(w);
    const draftTokens = activeDraft
      ? collectDraftMentionedTokens(w)
      : {{ keys: new Set(), digits: new Set() }};
    const covered = new Set();
    const peers = buildChecklistPeers((w && w.checklist) || [], artByKey, covered);
    attachOrPromoteBugs(arts, artByKey, peers, covered, ['primary']);
    attachUncoveredCls(w, arts, peers, covered, activeDraft, draftTokens);
    refreshNonChecklistPeerStages(peers);
    attachOrPromoteBugs(arts, artByKey, peers, covered, ['primary', 'channel']);
    return {{ peers, covered, artByKey }};
  }}

  // Return true when any CL in the peer needs action (conflict, On Desk/WIP, 0 reviewers, draft lag)
  function peerNeedsAttention(w, peer, hasDraft) {{
    if (peer.draftOutdated) return true;
    for (const c of peer.cls) {{
      if (c.merge_conflict || c.is_wip || c.desk_kind) return true;
      if (isOpenClStatus(c.status) && Number(c.assigned_reviewers_count ?? 1) === 0) return true;
      if (hasDraft && c.in_draft === false) return true;
    }}
    return false;
  }}

  // Compute baseline open state for a peer under Smart, All, or None mode without overrides
  function defaultPeerOpen(w, peer, hasDraft) {{
    const wid = String((w && w.work_id) || '');
    const mode = peerFoldModes[wid] || 'smart';
    if (mode === 'folded') return false;
    if (mode === 'expanded') return true;
    if (peer.bugStatus.toUpperCase() === 'CLOSED' && peer.cls.length === 0) return false;
    if (peerNeedsAttention(w, peer, hasDraft)) return true;
    const lane = (w && w.lane) || '';
    if ((lane === 'needs-you' || lane === 'on-desk') && peer.cls.length > 0) return true;
    return false;
  }}

  // Compute expanded/folded state of a peer row honoring manual per-peer overrides
  function isPeerOpen(w, peer, hasDraft) {{
    const wid = String((w && w.work_id) || '');
    const ovKey = `${{wid}}::${{peer.peerKey}}`;
    if (Object.prototype.hasOwnProperty.call(peerOpenOverrides, ovKey)) {{
      return Boolean(peerOpenOverrides[ovKey]);
    }}
    return defaultPeerOpen(w, peer, hasDraft);
  }}

  // Group multi-piece handoff chains sharing a real title while keeping raw Chat <hex> distinct
  function groupChatChains(chatArts) {{
    const seenCids = new Set();
    const groups = new Map();
    for (const a of chatArts) {{
      const cid = String((a.state && a.state.cascade_id) || a.artifact_key.split(':').pop());
      if (!cid || seenCids.has(cid)) continue;
      seenCids.add(cid);
      const rawTitle = String(a.title || '').trim();
      const isRaw = /^Chat\\s+[0-9a-f]{{6,}}$/i.test(rawTitle);
      const gKey = isRaw ? `raw:${{a.artifact_key}}` : `title:${{rawTitle.toLowerCase()}}`;
      if (!groups.has(gKey)) {{
        groups.set(gKey, {{
          label: rawTitle || cid.slice(0, 8),
          items: [],
        }});
      }}
      groups.get(gKey).items.push({{ cid, artifact_key: a.artifact_key, title: rawTitle }});
    }}
    return Array.from(groups.values());
  }}

  function renderRollDots(art) {{
    const st = (art && art.state) || {{}};
    const rolls = st.rolls;
    if (!rolls || typeof rolls !== 'object') return '';
    const map = [
      ['Android', 'A'],
      ['Chrome', 'C'],
      ['Google3', 'G3'],
      ['Flutter', 'F'],
    ];
    const items = [];
    for (const [k, short] of map) {{
      if (k in rolls) {{
        const r = rolls[k];
        const rolled = typeof r === 'boolean'
          ? r
          : Boolean(r && (r.rolled || r.state === '✓'));
        items.push(
          `<span class="wb-roll-dot" data-rolled="${{rolled}}" `
          + `title="${{k}}: ${{rolled ? 'rolled' : 'pending'}}">${{short}}</span>`
        );
      }}
    }}
    return items.length ? `<span class="wb-rolls">${{items.join('')}}</span>` : '';
  }}

  // Return true when an artifact is a correspondence thread, doc, or Gmail draft
  function isCorrArt(a) {{
    if (!a) return false;
    if (['gmail_thread', 'gmail_draft', 'gchat_thread', 'gdoc_thread'].includes(a.kind)) {{
      return true;
    }}
    return a.kind === 'gdoc' && Boolean(a.state && a.state.is_active_correspondence);
  }}

  // Render the editorial eyebrow, deduplicated headline, and collapsed summary for card w
  function renderCardHeader(w, peers, totalPeerCls, attentionPeers, hasDraft, openObs) {{
    let statusPillHtml = '';
    if (w.lane === 'needs-you') {{
      statusPillHtml = (
        '<span class="re-status-pill" data-tone="urgent">'
        + '<span class="re-dot"></span>Needs You</span>'
      );
    }} else if (w.lane === 'done') {{
      statusPillHtml = '<span class="re-status-pill" data-tone="ready">✅ Done</span>';
    }} else if (w.lane === 'on-desk') {{
      statusPillHtml = (
        '<span class="re-status-pill" data-tone="warn">'
        + '<span class="re-dot"></span>On Desk</span>'
      );
    }} else if (attentionPeers > 0 && peers.length > 0) {{
      const parkedSfx = hasDraft ? ' · Parked' : '';
      statusPillHtml = (
        `<span class="re-status-pill" data-tone="warn">`
        + `<span class="re-dot"></span>Action ${{attentionPeers}}/${{peers.length}}`
        + `${{parkedSfx}}</span>`
      );
    }} else {{
      statusPillHtml = (
        '<span class="re-status-pill" data-tone="wait">'
        + '<span class="re-dot"></span>Waiting</span>'
      );
    }}

    const {{ headline, counterparty }} = splitCardHeadline(w);
    const cpHtml = counterparty
      ? `<span class="re-counterparty">${{esc(counterparty)}}</span>`
      : '';

    let originRowHtml = '';
    if (w.origin && w.origin.label) {{
      const o = w.origin;
      const shortOrig = compactOriginLabel(o);
      const fullOrig = esc(o.label);
      let inner = '';
      if (o.kind === 'chat_chain') {{
        const ocid = String(o.artifact_key || '').split(':').pop();
        inner = `<a class="wb-origin" href="#" data-open-chat="${{esc(ocid)}}" `
          + `title="${{fullOrig}}">${{esc(shortOrig)}}</a>`;
      }} else if (o.url) {{
        inner = `<a class="wb-origin" href="${{esc(o.url)}}" target="_blank" rel="noopener" `
          + `title="${{fullOrig}}">${{esc(shortOrig)}}</a>`;
      }} else {{
        inner = `<span class="wb-origin" title="${{fullOrig}}">${{esc(shortOrig)}}</span>`;
      }}
      originRowHtml = `<div class="wb-origin-row">${{inner}}</div>`;
    }}

    const projLabel = (w.project_id && !/^[0-9a-f]{{8}}-[0-9a-f]{{4}}-/i.test(w.project_id))
      ? `<span>${{esc(w.project_id)}}</span>`
      : '';
    const roadmapTag = w.roadmap_item_key ? `<span>${{esc(w.roadmap_item_key)}}</span>` : '';

    // Surface creation date directly in Age mode, and expose it as a hover tooltip otherwise
    const actEt = String(w.activity_et || w.updated_et || '');
    const origEt = String(w.origin_et || '');
    let timeMetaHtml = `<span>${{esc(actEt)}}</span>`;
    if (sortMode === 'age' && origEt) {{
      const ageTxt = (actEt && actEt !== origEt)
        ? `Opened ${{esc(origEt)}} · Active ${{esc(actEt)}}`
        : `Opened ${{esc(origEt)}}`;
      timeMetaHtml = `<span>${{ageTxt}}</span>`;
    }} else if (origEt && origEt !== actEt) {{
      timeMetaHtml = `<span title="Opened ${{esc(origEt)}}">${{esc(actEt)}}</span>`;
    }}

    const headlineSubHtml = peers.length > 0
      ? `<span class="re-headline-sub">${{peers.length}} deliverable`
        + `${{peers.length === 1 ? '' : 's'}}`
        + `${{totalPeerCls ? ` · ${{totalPeerCls}} CL${{totalPeerCls === 1 ? '' : 's'}}` : ''}}`
        + `</span>`
      : '';

    const firstReason = (w.reasons && w.reasons.length) ? cleanEmojiPrefix(w.reasons[0]) : '';
    const firstProg = (openObs.length && openObs[0].progress_summary)
      ? String(openObs[0].progress_summary)
      : '';
    const collapsedSummaryTxt = [firstReason, firstProg].filter(Boolean).join(' · ');
    const collapsedSummaryHtml = collapsedSummaryTxt
      ? `<div class="re-collapsed-summary">${{esc(collapsedSummaryTxt)}}</div>`
      : '';

    return `
      <div class="wb-card-hdr" data-toggle-card="${{esc(w.work_id)}}">
        <div class="wb-hdr-stack">
          <div class="re-eyebrow">
            <div class="re-eyebrow-left">
              ${{statusPillHtml}}
              ${{cpHtml}}
              ${{originRowHtml}}
              ${{projLabel}}
              ${{roadmapTag}}
            </div>
            <div class="re-eyebrow-right">
              ${{timeMetaHtml}}
              <span class="re-chevron" aria-hidden="true"
                    title="Click to toggle · Alt-click to toggle all visible cards"
                    >${{SVG_CHEVRON}}</span>
            </div>
          </div>
          <div class="re-headline-row">
            <div class="wb-card-title re-headline"
                 title="${{esc(w.title)}}">${{esc(headline)}}</div>
            ${{headlineSubHtml}}
          </div>
          ${{collapsedSummaryHtml}}
        </div>
      </div>
    `;
  }}

  // Render one open correspondence/action obligation row with ambiguity and wait controls
  function renderObligationRow(o) {{
    const isDraftOb = String(o.target_artifact_key || '').startsWith('gmail_draft:');
    const waitTag = o.wb_reply_draft
      ? '<span style="color:var(--accent);font-weight:600">Parked reply note</span>'
      : (isDraftOb
          ? '<span style="color:var(--accent);font-weight:600">Parked draft</span>'
          : '<span style="color:var(--accent);font-weight:600">Waiting</span>');
    let readyTag = '<strong style="color:var(--ready)">READY</strong>';
    if (o.correspondence_mode === 'silence_ping') {{
      readyTag = '<strong style="color:var(--warn)">⏰ 7d SILENCE PING</strong>';
    }} else if (o.correspondence_mode === 'inbound_reply') {{
      readyTag = '<strong style="color:var(--urgent)">📨 NEEDS REPLY</strong>';
    }}
    const statusTag = o.status === 'ready'
      ? readyTag
      : (o.stalled_alert
          ? '<strong style="color:var(--urgent)">STALLED &gt;7d</strong>'
          : waitTag);
    const progHtml = o.progress_summary
      ? `<div class="wb-ob-prog">${{esc(o.progress_summary)}}</div>`
      : '';
    const ambigHtml = o.ambiguity_prompt
      ? `<div class="wb-ob-ambig">
           <span>${{esc(o.ambiguity_prompt)}}</span>
           <div class="wb-ob-btns">
             <button class="wb-btn" data-ob-keep="${{esc(o.obligation_id)}}"
                     data-ob-event="${{esc(o.ambiguity_event_key || '')}}"
                     type="button">Оставить обязательство</button>
             <button class="wb-btn" data-ob-close-topic="${{esc(o.obligation_id)}}"
                     data-ob-event="${{esc(o.ambiguity_event_key || '')}}"
                     type="button">Тема закрыта ✓</button>
           </div>
         </div>`
      : '';
    const waitBtnHtml = (o.kind === 'correspondence' && o.status === 'ready')
      ? `<button class="wb-btn" data-ob-wait="${{esc(o.obligation_id)}}"
                 title="Ответила устно / в другом канале — перевести в ожидание (7д)"
                 type="button">Ответила → жду</button>`
      : '';
    return `
      <div class="wb-ob re-loop-row" data-status="${{esc(o.status)}}"
           data-stalled="${{Boolean(o.stalled_alert)}}"
           title="Trigger: ${{esc(o.trigger_expr)}}">
        <div class="wb-ob-main">
          <div>${{statusTag}} · ${{esc(o.description)}}</div>
          ${{progHtml}}
          ${{ambigHtml}}
        </div>
        <div class="wb-ob-btns">
          ${{waitBtnHtml}}
          <button class="wb-btn" data-ob-done="${{esc(o.obligation_id)}}"
                  type="button">✓</button>
        </div>
      </div>
    `;
  }}

  // Render inline reply-draft editor or preview with Gmail materialization and sync strip
  function renderReplyDraftBox(w, syncStripHtml) {{
    const rd = w.reply_draft || null;
    const isEditingDraft = Boolean(!w.is_virtual && openDraftEditors[w.work_id]);
    if (isEditingDraft) {{
      const edState = openDraftEditors[w.work_id];
      const hasSnap = edState && typeof edState === 'object' && edState.initialized;
      const initBody = hasSnap ? String(edState.body ?? '') : (rd ? String(rd.body || '') : '');
      const initAg = hasSnap ? Boolean(edState.autoGate) : (rd ? Boolean(rd.auto_gate) : true);
      const lblStyle = 'font-size:11px;color:var(--muted);display:inline-flex;'
        + 'align-items:center;gap:4px';
      return `
        <div class="wb-reply-draft">
          <div class="wb-reply-draft-hdr"><span>Edit live reply draft</span></div>
          <textarea class="wb-reply-textarea" id="wb-draft-ta-${{esc(w.work_id)}}"
                    placeholder="HTML or plain-text reply note...">${{esc(initBody)}}</textarea>
          ${{syncStripHtml}}
          <div class="wb-actions" style="justify-content:space-between;flex-wrap:wrap">
            <label style="${{lblStyle}}">
              <input type="checkbox" id="wb-draft-ag-${{esc(w.work_id)}}"
                     ${{initAg ? 'checked' : ''}}>
              Auto-gate on primary CLs/bugs
            </label>
            <div class="wb-ob-btns">
              <button class="wb-btn" data-cancel-draft="${{esc(w.work_id)}}"
                      type="button">Cancel</button>
              <button class="wb-btn wb-btn-primary" data-save-draft="${{esc(w.work_id)}}"
                      type="button">Save</button>
            </div>
          </div>
        </div>
      `;
    }}
    if (!rd || !rd.body) return '';
    const rawMatUrl = String(rd.materialized_url || '');
    const safeMatUrl = /^https?:\\/\\//i.test(rawMatUrl) ? esc(rawMatUrl) : '';
    let matBtns = '';
    if (safeMatUrl && !rd.needs_rematerialize) {{
      matBtns = `<a class="wb-btn wb-btn-primary" href="${{safeMatUrl}}"`
        + ` target="_blank" rel="noopener"`
        + ` style="text-decoration:none">Open Gmail draft ↗</a>`;
    }} else if (safeMatUrl && rd.needs_rematerialize) {{
      matBtns = `
        <button class="wb-btn wb-btn-primary" data-mat-draft="${{esc(w.work_id)}}"
                data-force-new="true" type="button">Sync Gmail draft</button>
        <a class="wb-btn" href="${{safeMatUrl}}" target="_blank" rel="noopener"
           style="text-decoration:none">Old tab ↗</a>
      `;
    }} else {{
      matBtns = `<button class="wb-btn wb-btn-primary" data-mat-draft="${{esc(w.work_id)}}"`
        + ` type="button">Create Gmail draft</button>`;
    }}
    return `
      <div class="wb-reply-draft">
        <div class="wb-reply-draft-hdr">
          <span>Live reply draft · ${{esc(rd.updated_et || '')}}</span>
          <div class="wb-ob-btns">
            ${{matBtns}}
            <button class="wb-btn" data-edit-draft="${{esc(w.work_id)}}"
                    type="button">Edit</button>
            <button class="wb-btn" data-clear-draft="${{esc(w.work_id)}}"
                    type="button">Clear</button>
          </div>
        </div>
        <div class="wb-reply-draft-body">${{renderDraftPreview(rd)}}</div>
        ${{syncStripHtml}}
      </div>
    `;
  }}

  // Render Band 1 combining obligations, correspondence threads/drafts, and inline draft composer
  function renderLoopBand(w, openObs, peers, hasDraft) {{
    const obsItemsHtml = openObs.map(renderObligationRow).join('');
    const obsHtml = obsItemsHtml ? `<div class="wb-obs">${{obsItemsHtml}}</div>` : '';
    const corrPills = (w.artifacts || []).filter(
      (a) => a.role !== 'context_only' && isCorrArt(a)
        && !(a.kind === 'gmail_draft' && gmailDraftStatus(a) === 'DISCARDED')
    );
    const corrRowsHtml = corrPills.map((a) => {{
      const cutBtn = !w.is_virtual
        ? `<button class="re-unlink wb-cut" title="Detach from Work" `
          + `data-unlink-work="${{esc(w.work_id)}}" `
          + `data-unlink-key="${{esc(a.artifact_key)}}" type="button">×</button>`
        : '';
      const msgCnt = a.state && a.state.message_count
        ? `<span class="re-msg-cnt">(${{esc(a.state.message_count)}}) ↗</span>`
        : '';
      const sentTag = (a.kind === 'gmail_draft' && gmailDraftStatus(a) === 'SENT')
        ? '<span style="color:var(--ready);font-size:10px;font-weight:600">[SENT]</span>'
        : '';
      const whySpan = a.why_badge ? `<span class="wb-why">· ${{esc(a.why_badge)}}</span>` : '';
      return `
        <div class="re-corr-row" data-kind="${{esc(a.kind)}}" data-role="${{esc(a.role)}}">
          <a class="re-corr-link" href="${{esc(a.url || '#')}}"
             target="_blank" rel="noopener">${{esc(a.title)}}</a>
          ${{sentTag}}
          ${{whySpan}}
          ${{msgCnt}}
          ${{cutBtn}}
        </div>
      `;
    }}).join('');
    const corrHtml = corrRowsHtml ? `<div class="wb-correspondence">${{corrRowsHtml}}</div>` : '';

    const missingDraftCls = [];
    if (hasDraft) {{
      for (const p of peers) {{
        for (const c of p.cls) {{
          if (c.in_draft === false) missingDraftCls.push(c.shortLabel);
        }}
      }}
    }}
    const syncStripHtml = missingDraftCls.length
      ? `<div class="re-sync-strip"><span>Not in draft yet: `
        + `<strong>${{esc(missingDraftCls.join(', '))}}</strong></span></div>`
      : '';
    const replyDraftHtml = renderReplyDraftBox(w, syncStripHtml);
    if (!obsHtml && !corrHtml && !replyDraftHtml) return '';
    return `<div class="re-band">${{obsHtml}}${{corrHtml}}${{replyDraftHtml}}</div>`;
  }}

  // Render one CL/PR row inside an expanded peer spine without duplicating standalone titles
  function renderPeerSpineClRow(w, peer, c, hasDraft) {{
    const psBadge = c.psTag ? `<span class="re-ps">${{esc(c.psTag)}}</span>` : '';
    const wipBadge = c.is_wip
      ? `<span class="wb-cl-tag" style="background:#fffbeb;color:var(--warn)">`
        + `${{esc(c.wip_label || 'WIP')}}</span>`
      : '';
    const noRevBadge = (
      !c.is_wip && isOpenClStatus(c.status) && Number(c.assigned_reviewers_count ?? 1) === 0
    )
      ? '<span class="wb-cl-tag" style="background:#fffbeb;color:var(--warn)">no reviewers</span>'
      : '';
    const stackBadge = c.is_stacked
      ? '<span class="wb-cl-tag" style="background:#f1f5f9;color:var(--muted)">stacked</span>'
      : '';
    const confBadge = c.merge_conflict
      ? '<span class="wb-cl-tag" style="background:#fff2f1;color:var(--urgent)">conflict</span>'
      : '';
    const draftMissBadge = (hasDraft && c.in_draft === false)
      ? '<span class="wb-cl-tag" style="background:#fffbeb;color:var(--warn)">not in draft</span>'
      : '';
    const isStandaloneDup = (
      peer.bugKey === '' && peer.cls.length === 1 && c.subject === peer.title
    );
    const subjSpan = (c.subject && c.subject !== c.shortLabel && !isStandaloneDup)
      ? `<span class="wb-cl-subj">${{esc(c.subject)}}</span>`
      : '';
    const whySpan = c.why_badge ? `<span class="wb-why">· ${{esc(c.why_badge)}}</span>` : '';
    const updSpan = c.updated_et
      ? `<span style="color:var(--muted);font-size:10px">${{esc(c.updated_et)}}</span>`
      : '';
    const clCutBtn = !w.is_virtual
      ? `<button class="re-unlink wb-cut" title="Detach CL from Work" `
        + `data-unlink-work="${{esc(w.work_id)}}" `
        + `data-unlink-key="${{esc(c.artifact_key)}}" type="button">×</button>`
      : '';
    return `
      <div class="wb-cl-chip re-cl-row">
        <a class="re-cl-link" href="${{esc(c.url || '#')}}"
           target="_blank" rel="noopener">${{esc(c.shortLabel)}}</a>
        ${{psBadge}}
        ${{subjSpan}}
        ${{confBadge}}${{wipBadge}}${{noRevBadge}}${{draftMissBadge}}${{stackBadge}}
        ${{renderRollDots({{ state: {{ rolls: c.rolls }} }})}}
        ${{whySpan}}
        ${{updSpan}}
        ${{clCutBtn}}
      </div>
    `;
  }}

  // Render Band 2 containing Smart/All/None fold controls and co-equal deliverable peers
  function renderDeliverablesBand(w, peers, artByKey, hasDraft) {{
    if (!peers.length) return '';
    const curFoldMode = peerFoldModes[w.work_id] || 'smart';
    const foldBarHtml = peers.length > 1
      ? `<div class="re-fold-bar" role="group" aria-label="Peer fold mode">
           <button class="re-fold-btn" type="button"
                   data-fold-mode="smart" data-fold-work="${{esc(w.work_id)}}"
                   data-active="${{curFoldMode === 'smart'}}">Smart</button>
           <button class="re-fold-btn" type="button"
                   data-fold-mode="folded" data-fold-work="${{esc(w.work_id)}}"
                   data-active="${{curFoldMode === 'folded'}}">All</button>
           <button class="re-fold-btn" type="button"
                   data-fold-mode="expanded" data-fold-work="${{esc(w.work_id)}}"
                   data-active="${{curFoldMode === 'expanded'}}">None</button>
         </div>`
      : '';
    const peerRowsHtml = peers.map((peer) => {{
      const peerOpen = isPeerOpen(w, peer, hasDraft);
      const extraHtml = peer.extraIssues.map((ex) => {{
        const exCut = (!w.is_virtual && artByKey.has(ex.artifact_key))
          ? `<button class="re-unlink wb-cut" title="Detach companion bug" `
            + `data-unlink-work="${{esc(w.work_id)}}" `
            + `data-unlink-key="${{esc(ex.artifact_key)}}" type="button">×</button>`
          : '';
        return `<span class="re-extra-wrap"><a class="re-extra-bug" href="${{esc(ex.url)}}" `
          + `target="_blank" rel="noopener" title="${{esc(ex.title)}}">+${{esc(ex.shortId)}}</a>`
          + `${{exCut}}</span>`;
      }}).join('');
      const foldedClsHtml = peer.cls
        .filter((c) => !(peer.bugKey === '' && c.artifact_key === peer.peerKey))
        .map((c) => {{
          const confCls = c.merge_conflict ? ' is-conflict' : '';
          return `<a class="re-folded-cl${{confCls}}" href="${{esc(c.url || '#')}}" `
            + `target="_blank" rel="noopener">${{esc(c.shortLabel)}}</a>`;
        }}).join('');
      const peerUnlinkKey = peer.bugKey || (peer.cls[0] && peer.cls[0].artifact_key) || '';
      const peerCutBtn = (!w.is_virtual && peerUnlinkKey && artByKey.has(peerUnlinkKey))
        ? `<button class="re-unlink wb-cut" title="Detach from Work" `
          + `data-unlink-work="${{esc(w.work_id)}}" `
          + `data-unlink-key="${{esc(peerUnlinkKey)}}" type="button">×</button>`
        : '';
      const spineClsHtml = peer.cls.length
        ? peer.cls.map((c) => renderPeerSpineClRow(w, peer, c, hasDraft)).join('')
        : '<div class="re-empty-spine">No linked CLs yet</div>';
      const outdatedTag = peer.draftOutdated
        ? '<span class="wb-draft-warn">Not in draft</span>'
        : '';
      return `
        <div class="wb-chk-row re-peer" data-stage="${{esc(peer.stage)}}"
             data-peer-key="${{esc(peer.peerKey)}}" data-peer-open="${{peerOpen}}">
          <div class="re-peer-hdr" data-peer-hdr="${{esc(peer.peerKey)}}"
               data-peer-work="${{esc(w.work_id)}}">
            <span class="re-peer-caret" aria-hidden="true">${{SVG_CHEVRON_SM}}</span>
            <div class="re-peer-lead">
              <a class="wb-bug-id re-peer-id" href="${{esc(peer.url || '#')}}"
                 target="_blank" rel="noopener">${{esc(peer.shortId)}}</a>
              ${{extraHtml}}
              <span class="wb-bug-title re-peer-title"
                    title="${{esc(peer.title)}}">${{esc(peer.title)}}</span>
            </div>
            <div class="re-peer-folded-cls">${{foldedClsHtml}}</div>
            <div class="re-peer-badges">
              <span class="wb-stage-badge re-stage"
                    data-tone="${{esc(peer.stageTone)}}">${{esc(peer.stageLabel)}}</span>
              ${{outdatedTag}}
              ${{peerCutBtn}}
            </div>
          </div>
          <div class="wb-chk-cls re-spine">${{spineClsHtml}}</div>
        </div>
      `;
    }}).join('');
    return `
      <div class="re-band">
        <div class="re-band-hdr">
          <span class="re-band-lbl">Deliverables (${{peers.length}})</span>
          ${{foldBarHtml}}
        </div>
        <div class="wb-checklist re-peer-list">${{peerRowsHtml}}</div>
      </div>
    `;
  }}

  // Render a secondary or chat-mention artifact pill with optional unlink button
  function renderPillItem(w, a) {{
    const cutBtn = !w.is_virtual
      ? `<button class="re-unlink wb-cut" title="Detach from Work" `
        + `data-unlink-work="${{esc(w.work_id)}}" `
        + `data-unlink-key="${{esc(a.artifact_key)}}" type="button">×</button>`
      : '';
    const isChat = a.kind === 'chat_chain';
    const chatCid = (a.state && a.state.cascade_id) || a.artifact_key.split(':').pop();
    const linkAttrs = isChat
      ? `href="#" data-open-chat="${{esc(chatCid)}}"`
      : `href="${{esc(a.url || '#')}}" target="_blank" rel="noopener"`;
    const stBadge = (a.state && a.state.status)
      ? `<span style="color:var(--muted);font-size:10px">[${{esc(a.state.status)}}]</span>`
      : '';
    const whySpan = a.why_badge ? `<span class="wb-why">· ${{esc(a.why_badge)}}</span>` : '';
    const tipAttr = a.why_tooltip ? `title="${{esc(a.why_tooltip)}}"` : '';
    return `
      <div class="wb-pill" data-kind="${{esc(a.kind)}}"
           data-role="${{esc(a.role)}}" ${{tipAttr}}>
        <a ${{linkAttrs}} style="color:inherit;text-decoration:none">${{esc(a.title)}}</a>
        ${{whySpan}}
        ${{stBadge}}
        ${{renderRollDots(a)}}
        ${{cutBtn}}
      </div>
    `;
  }}

  // Render the card footer with grouped chat chips, remaining pills, mentions, and actions
  function renderCardFooter(w, chkCovered) {{
    const chatArts = (w.artifacts || []).filter(
      (a) => a && a.role !== 'context_only' && !chkCovered.has(a.artifact_key)
        && a.kind === 'chat_chain'
    );
    const chatGroups = groupChatChains(chatArts);
    const chatsHtml = chatGroups.length
      ? `<div class="re-chats-row">${{chatGroups.map((g) => {{
          const first = g.items[0];
          const allKeys = g.items.map((it) => it.artifact_key).join(',');
          const piecesHtml = g.items.length > 1
            ? g.items.map(
                (it, i) => `<a class="re-chat-piece" href="#" `
                  + `data-open-chat="${{esc(it.cid)}}">#${{i + 1}}</a>`
              ).join('')
            : '';
          const mainLink = g.items.length > 1
            ? `<span class="re-chat-main" title="${{esc(g.label)}}">${{esc(g.label)}}</span>`
            : `<a class="re-chat-main" href="#" data-open-chat="${{esc(first.cid)}}" `
              + `title="${{esc(g.label)}}">${{esc(g.label)}}</a>`;
          const cutChatBtn = !w.is_virtual
            ? `<button class="re-unlink wb-cut" title="Detach chat" `
              + `data-unlink-work="${{esc(w.work_id)}}" `
              + `data-unlink-key="${{esc(allKeys)}}" type="button">×</button>`
            : '';
          return `<span class="re-chat-chip">${{mainLink}}${{piecesHtml}}${{cutChatBtn}}</span>`;
        }}).join('')}}</div>`
      : '';

    const otherPills = (w.artifacts || []).filter(
      (a) => a && a.role !== 'context_only' && !chkCovered.has(a.artifact_key)
        && !isCorrArt(a) && a.kind !== 'chat_chain'
    );
    const mentionPills = (w.artifacts || []).filter(
      (a) => a && a.role === 'context_only' && !chkCovered.has(a.artifact_key)
    );
    const pillsHtml = otherPills.map((a) => renderPillItem(w, a)).join('');
    const mCnt = mentionPills.length;
    const mLbl = `${{mCnt}} chat mention${{mCnt === 1 ? '' : 's'}}`;
    const mentionItemsHtml = mentionPills.map((a) => renderPillItem(w, a)).join('');
    const mentionsHtml = mCnt
      ? `<details class="wb-mentions re-mentions">
           <summary title="📎 Упомянуто в чате (${{mCnt}})">${{mLbl}}</summary>
           <div class="wb-mentions-list">${{mentionItemsHtml}}</div>
         </details>`
      : '<span></span>';

    const rd = w.reply_draft || null;
    const isEditingDraft = Boolean(!w.is_virtual && openDraftEditors[w.work_id]);
    // Cards that already show a chat chip pass data-force-new so + Chat opens an extra thread
    const forceNewAttr = chatGroups.length > 0 ? ' data-force-new="true"' : '';
    const chatBtnTip = chatGroups.length > 0
      ? 'Start an additional chat for this card'
      : 'Start or open chat for this card';
    const startChatBtn = (
      `<button class="wb-btn" data-start-chat="${{esc(w.work_id)}}"`
      + `${{forceNewAttr}} title="${{chatBtnTip}}" type="button">+ Chat</button>`
    );
    const addDraftBtn = (!w.is_virtual && !rd && !isEditingDraft)
      ? `<button class="wb-btn" data-edit-draft="${{esc(w.work_id)}}"`
        + ` type="button">+ Reply draft</button>`
      : '';
    const acceptBtn = w.owner !== 'user'
      ? `<button class="wb-btn" data-work-accept="${{esc(w.work_id)}}"
                 title="Сделать своей работой" type="button">Принять</button>`
      : '';
    const persistedBtns = !w.is_virtual
      ? `${{addDraftBtn}}
         <button class="wb-btn" data-add-ob="${{esc(w.work_id)}}"
                 type="button">+ Obligation</button>
         <button class="wb-btn" data-add-link="${{esc(w.work_id)}}"
                 type="button">+ Link</button>
         ${{acceptBtn}}
         <button class="wb-btn" data-work-done="${{esc(w.work_id)}}"
                 type="button">✓ Done</button>`
      : '';
    const cardActions = `<div class="wb-actions">${{startChatBtn}}${{persistedBtns}}</div>`;

    return `
      <div class="re-footer">
        ${{chatsHtml}}
        ${{pillsHtml ? `<div class="wb-pills">${{pillsHtml}}</div>` : ''}}
        <div class="re-utility-row">
          ${{mentionsHtml}}
          ${{cardActions}}
        </div>
      </div>
    `;
  }}

  // Render a single editorial Work card across header, decision strip, loop, peers, and footer
  function renderWorkCard(w) {{
    const collapsed = isCardCollapsed(w);
    const openObs = (w.obligations || []).filter((o) => o.status !== 'done');
    const hasDraft = hasParkedDraft(w);
    const {{ peers, covered: chkCovered, artByKey }} = buildDeliverablePeers(w, hasDraft);
    const totalPeerCls = peers.reduce((acc, p) => acc + p.cls.length, 0);
    const attentionPeers = peers.filter((p) => peerNeedsAttention(w, p, hasDraft)).length;
    const headerHtml = renderCardHeader(
      w, peers, totalPeerCls, attentionPeers, hasDraft, openObs
    );
    const reasonsList = (w.reasons || []).map((r) => esc(cleanEmojiPrefix(r))).join(' · ');
    const decisionHtml = reasonsList
      ? `<div class="re-decision"><div class="re-decision-txt">${{reasonsList}}</div></div>`
      : '';
    const loopBandHtml = renderLoopBand(w, openObs, peers, hasDraft);
    const deliverablesBandHtml = renderDeliverablesBand(w, peers, artByKey, hasDraft);
    const footerHtml = renderCardFooter(w, chkCovered);
    const cardAttrs = `data-work-id="${{esc(w.work_id)}}"`
      + ` data-lane="${{esc(w.lane)}}" data-collapsed="${{collapsed}}"`;
    return `
      <article class="wb-card" ${{cardAttrs}}>
        ${{headerHtml}}
        <div class="wb-card-body">
          ${{decisionHtml}}
          ${{loopBandHtml}}
          ${{deliverablesBandHtml}}
          ${{footerHtml}}
        </div>
      </article>
    `;
  }}

  // Her works and every Needs-You card stay in the main group; quiet auto drafts fold away
  function renderGroupedCards(works) {{
    // Activity and Age are flat chronological views; only Triage splits by ownership
    if (sortMode !== 'triage') return works.map(renderWorkCard).join('');
    const isFoldedDraft = (w) => w.owner !== 'user' && w.lane !== 'needs-you';
    const main = works.filter((w) => !isFoldedDraft(w));
    const drafts = works.filter(isFoldedDraft);
    if (!drafts.length) return main.map(renderWorkCard).join('');
    const caret = draftsOpen ? '▾' : '▸';
    return `<div class="wb-group-hdr">Мои работы (${{main.length}})</div>`
      + main.map(renderWorkCard).join('')
      + `<button class="wb-group-hdr wb-drafts-toggle" data-drafts-toggle type="button"`
      + ` aria-expanded="${{draftsOpen}}">${{caret}} Черновики (${{drafts.length}})</button>`
      + `<div class="wb-drafts-body"${{draftsOpen ? '' : ' hidden'}}>`
      + `${{drafts.map(renderWorkCard).join('')}}</div>`;
  }}

  // Refresh KPIs, unanswered tray, filter chips, and card list while preserving live draft typing
  function render() {{
    const counts = (state && state.counts) || {{}};
    const metrics = (state && state.metrics) || {{}};
    document.getElementById('wb-synced').textContent = (
      'Synced ' + ((state && state.synced_et) || '')
    );
    document.getElementById('kpi-needs').textContent = String(counts.needs_you || 0);
    document.getElementById('kpi-wait').textContent = String(counts.waiting || 0);
    document.getElementById('kpi-obs').textContent = String(counts.obligations_open || 0);
    document.getElementById('kpi-tray').textContent = String(counts.unanswered || 0);
    const prec = Math.round(((metrics.link_precision ?? 1) * 100));
    document.getElementById('kpi-prec').textContent = `${{prec}}%`;

    const tray = (state && state.unanswered_tray) || [];
    const trayEl = document.getElementById('wb-tray-container');
    if (tray.length === 0) {{
      trayEl.innerHTML = '';
    }} else {{
      const rows = tray.map((t) => {{
        const safeUrl = /^https?:\\/\\//i.test(String(t.url || '')) ? esc(t.url) : '#';
        return `
        <div class="wb-tray-row">
          <div style="min-width:0;flex:1">
            <strong>${{esc(t.sender)}}</strong> ·
            <a href="${{safeUrl}}" target="_blank" rel="noopener"
               style="color:var(--text)">${{esc(t.subject)}}</a>
          </div>
          <div class="wb-tray-btns">
            <button class="wb-btn" data-promote-thread="${{esc(t.artifact_key)}}"
                    data-promote-subj="${{esc(t.subject)}}" type="button">+ Work</button>
            <button class="wb-btn wb-btn-dismiss" data-dismiss-thread="${{esc(t.artifact_key)}}"
                    title="Dismiss thread" aria-label="Dismiss thread" type="button">✕</button>
          </div>
        </div>
      `;
      }}).join('');
      trayEl.innerHTML = `
        <div class="wb-tray">
          <div class="wb-tray-hdr"><span>Unanswered Human Threads (${{tray.length}})</span></div>
          ${{rows}}
        </div>
      `;
    }}

    updateSortControlsUI();
    updateFilterChipsUI();
    const works = getVisibleWorks();
    updateCollapseToggleUI(works);

    const container = document.getElementById('wb-cards-container');
    // Snapshot live textarea edits and selection so background syncs never wipe unsaved typing
    let focusedDraftWid = null;
    let focusedSelStart = null;
    let focusedSelEnd = null;
    for (const taEl of document.querySelectorAll('.wb-reply-textarea')) {{
      const taId = String(taEl.id || '');
      if (!taId.startsWith('wb-draft-ta-')) continue;
      const wid = taId.slice('wb-draft-ta-'.length);
      if (!openDraftEditors[wid]) continue;
      const agEl = document.getElementById(`wb-draft-ag-${{wid}}`);
      openDraftEditors[wid] = {{
        body: taEl.value,
        autoGate: agEl ? Boolean(agEl.checked) : true,
        initialized: true,
      }};
      if (document.activeElement === taEl) {{
        focusedDraftWid = wid;
        focusedSelStart = taEl.selectionStart;
        focusedSelEnd = taEl.selectionEnd;
      }}
    }}
    if (works.length === 0) {{
      container.innerHTML = (
        '<div style="color:var(--muted);padding:16px 4px">'
        + 'No works match the current filter.</div>'
      );
      return;
    }}

    container.innerHTML = renderGroupedCards(works);
    if (focusedDraftWid && openDraftEditors[focusedDraftWid]) {{
      const nextTa = document.getElementById(`wb-draft-ta-${{focusedDraftWid}}`);
      if (nextTa) {{
        nextTa.focus();
        if (typeof focusedSelStart === 'number' && typeof focusedSelEnd === 'number') {{
          nextTa.setSelectionRange(focusedSelStart, focusedSelEnd);
        }}
      }}
    }}
  }}

  document.getElementById('wb-new-btn').addEventListener('click', () => {{
    const f = document.getElementById('wb-new-form');
    f.setAttribute('data-open', f.getAttribute('data-open') === 'true' ? 'false' : 'true');
  }});
  document.getElementById('wb-cancel-new').addEventListener('click', () => {{
    document.getElementById('wb-new-form').setAttribute('data-open', 'false');
  }});
  document.getElementById('wb-new-form').addEventListener('submit', (e) => {{
    e.preventDefault();
    const title = document.getElementById('wb-new-title').value.trim();
    const primaries = document.getElementById('wb-new-primary').value.split(',').map((s) => s.trim()).filter(Boolean);
    const channels = document.getElementById('wb-new-channel').value.split(',').map((s) => s.trim()).filter(Boolean);
    const obligation = document.getElementById('wb-new-oblige').value.trim();
    const trigger = document.getElementById('wb-new-when').value.trim() || 'always';
    document.getElementById('wb-new-form').setAttribute('data-open', 'false');
    document.getElementById('wb-new-form').reset();
    void postJson('/api/work/create', {{ title, primaries, channels, obligation, trigger }});
  }});

  document.getElementById('wb-sync-btn').addEventListener('click', () => {{
    void postJson('/api/sync', {{}});
  }});

  // Smart-default sort direction on mode switch: Age surfaces longest-open first (asc)
  const sortModeEl = document.getElementById('wb-sort-mode');
  if (sortModeEl) {{
    sortModeEl.addEventListener('change', (e) => {{
      const nextMode = String((e && e.target && e.target.value) || sortModeEl.value || 'triage');
      // Ignore unknown DOM option values so tampered state cannot corrupt sort comparators
      if (['triage', 'activity', 'age'].includes(nextMode)) {{
        sortMode = nextMode;
        sortDir = nextMode === 'age' ? 'asc' : 'desc';
        saveSortPrefs();
        render();
      }}
    }});
  }}
  const sortDirEl = document.getElementById('wb-sort-dir');
  if (sortDirEl) {{
    sortDirEl.addEventListener('click', () => {{
      sortDir = sortDir === 'desc' ? 'asc' : 'desc';
      saveSortPrefs();
      render();
    }});
  }}

  // Scope toolbar toggle to currently visible cards without touching hidden tabs
  document.getElementById('wb-toggle-collapse').addEventListener('click', () => {{
    const works = getVisibleWorks();
    const targetCollapsed = works.some((w) => !isCardCollapsed(w));
    for (const w of works) setCardCollapsed(w, targetCollapsed);
    if (!isSearchActive()) saveCollapsePrefs();
    render();
  }});

  document.getElementById('wb-search').addEventListener('input', (e) => {{
    searchQuery = e.target.value || '';
    // Clear transient search collapse state on every new query
    searchCollapsedOverrides = {{}};
    render();
  }});

  // Toggle one peer row open/closed, deleting redundant overrides when back at mode default
  function togglePeerHeader(peerHdr) {{
    const pkey = peerHdr.getAttribute('data-peer-hdr');
    const wid = peerHdr.getAttribute('data-peer-work');
    if (!wid || !pkey) return;
    const ovKey = `${{wid}}::${{pkey}}`;
    const wObj = ((state && state.works) || []).find((x) => String(x.work_id) === wid);
    if (wObj) {{
      const hasDraft = hasParkedDraft(wObj);
      const {{ peers }} = buildDeliverablePeers(wObj, hasDraft);
      const pObj = peers.find((p) => p.peerKey === pkey);
      if (pObj) {{
        const nextOpen = !isPeerOpen(wObj, pObj, hasDraft);
        if (nextOpen === defaultPeerOpen(wObj, pObj, hasDraft)) {{
          delete peerOpenOverrides[ovKey];
        }} else {{
          peerOpenOverrides[ovKey] = nextOpen;
        }}
        savePeerFoldPrefs();
        render();
        return;
      }}
    }}
    peerOpenOverrides[ovKey] = false;
    savePeerFoldPrefs();
    render();
  }}

  document.body.addEventListener('click', async (e) => {{
    // Exclude interactive controls so inner link or button clicks never toggle peer or card
    const target = e.target.closest('button, a, input, select, textarea, label, summary');
    if (!target) {{
      const sel = window.getSelection ? String(window.getSelection()) : '';
      // Skip collapse or peer toggle when selecting text unless Alt-click requests bulk toggle
      if (!e.altKey && sel) return;
      const peerHdr = e.target.closest('[data-peer-hdr]');
      if (peerHdr) {{
        togglePeerHeader(peerHdr);
        return;
      }}
      const hdr = e.target.closest('[data-toggle-card]');
      if (hdr) {{
        const wid = hdr.getAttribute('data-toggle-card');
        const cardEl = hdr.closest('.wb-card');
        const cur = cardEl ? cardEl.getAttribute('data-collapsed') === 'true' : false;
        const works = getVisibleWorks();
        if (e.altKey) {{
          e.preventDefault();
          const nextCollapsed = !cur;
          for (const w of works) setCardCollapsed(w, nextCollapsed);
        }} else {{
          const clicked = works.find((w) => String(w.work_id) === String(wid));
          if (clicked) {{
            setCardCollapsed(clicked, !cur);
          }} else if (!isSearchActive()) {{
            collapsedOverrides[wid] = !cur;
          }}
        }}
        if (!isSearchActive()) saveCollapsePrefs();
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-fold-mode')) {{
      const mode = target.getAttribute('data-fold-mode');
      const wid = target.getAttribute('data-fold-work');
      if (wid && ['smart', 'folded', 'expanded'].includes(mode)) {{
        peerFoldModes[wid] = mode;
        for (const k of Object.keys(peerOpenOverrides)) {{
          if (k.startsWith(`${{wid}}::`)) delete peerOpenOverrides[k];
        }}
        savePeerFoldPrefs();
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-open-chat')) {{
      e.preventDefault();
      const cid = target.getAttribute('data-open-chat');
      if (cid) await openChatInJetski(cid, '');
      return;
    }}
    // Guard against double-click spawning while start_chat_for_work calls StartCascade
    if (target.hasAttribute('data-start-chat')) {{
      const wid = target.getAttribute('data-start-chat');
      if (!wid || target.disabled) return;
      const forceNew = target.getAttribute('data-force-new') === 'true';
      const origLabel = target.textContent;
      target.disabled = true;
      target.textContent = 'Starting…';
      try {{
        const ok = await postJson('/api/work/start_chat', {{
          work_id: wid,
          force_new: forceNew,
        }});
        if (ok && state && state.started_chat && state.started_chat.conversation_id) {{
          notifyParentRendered();
          await openChatInJetski(
            state.started_chat.conversation_id,
            state.started_chat.chat_url || ''
          );
        }}
      }} finally {{
        target.disabled = false;
        target.textContent = origLabel;
      }}
      return;
    }}
    if (target.hasAttribute('data-unlink-work')) {{
      const wid = target.getAttribute('data-unlink-work');
      const rawKeys = String(target.getAttribute('data-unlink-key') || '')
        .split(',')
        .map((s) => s.trim())
        .filter(Boolean);
      for (const akey of rawKeys) {{
        await postJson('/api/work/unlink', {{ work_id: wid, artifact_key: akey }});
      }}
      return;
    }}
    if (target.hasAttribute('data-ob-done')) {{
      void postJson('/api/obligation/done', {{
        obligation_id: target.getAttribute('data-ob-done'),
      }});
      return;
    }}
    if (target.hasAttribute('data-ob-wait')) {{
      void postJson('/api/obligation/wait', {{
        obligation_id: target.getAttribute('data-ob-wait'),
      }});
      return;
    }}
    if (target.hasAttribute('data-ob-keep')) {{
      void postJson('/api/obligation/resolve_ambiguity', {{
        obligation_id: target.getAttribute('data-ob-keep'),
        decision: 'keep',
        event_key: target.getAttribute('data-ob-event') || '',
      }});
      return;
    }}
    if (target.hasAttribute('data-ob-close-topic')) {{
      void postJson('/api/obligation/resolve_ambiguity', {{
        obligation_id: target.getAttribute('data-ob-close-topic'),
        decision: 'close',
        event_key: target.getAttribute('data-ob-event') || '',
      }});
      return;
    }}
    if (target.hasAttribute('data-drafts-toggle')) {{
      draftsOpen = !draftsOpen;
      saveDraftsPrefs();
      render();
      return;
    }}
    if (target.hasAttribute('data-work-accept')) {{
      void postJson('/api/work/accept', {{
        work_id: target.getAttribute('data-work-accept'),
      }});
      return;
    }}
    if (target.hasAttribute('data-work-done')) {{
      void postJson('/api/work/done', {{
        work_id: target.getAttribute('data-work-done'),
      }});
      return;
    }}
    if (target.hasAttribute('data-promote-thread')) {{
      const akey = target.getAttribute('data-promote-thread');
      const subj = target.getAttribute('data-promote-subj') || akey;
      void postJson('/api/work/create', {{
        title: subj,
        primaries: [],
        channels: [akey],
      }});
      return;
    }}
    if (target.hasAttribute('data-dismiss-thread')) {{
      const akey = target.getAttribute('data-dismiss-thread');
      void postJson('/api/tray/dismiss', {{ artifact_key: akey }});
      return;
    }}
    if (target.hasAttribute('data-add-ob')) {{
      const wid = target.getAttribute('data-add-ob');
      const desc = window.prompt('Obligation description (e.g. Reply to Renzo once rolled):');
      if (!desc) return;
      const trig = window.prompt('Trigger (e.g. rolled_into(Flutter, gerrit:skia:1414545) or always):', 'always') || 'always';
      void postJson('/api/obligation/add', {{ work_id: wid, description: desc, trigger_expr: trig }});
      return;
    }}
    if (target.hasAttribute('data-edit-draft')) {{
      const wid = target.getAttribute('data-edit-draft');
      if (wid) {{
        const wObj = ((state && state.works) || []).find((x) => String(x.work_id) === wid);
        const rd = (wObj && wObj.reply_draft) || null;
        openDraftEditors[wid] = {{
          body: rd ? String(rd.body || '') : '',
          autoGate: rd ? Boolean(rd.auto_gate) : true,
          initialized: true,
        }};
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-cancel-draft')) {{
      const wid = target.getAttribute('data-cancel-draft');
      if (wid) {{
        delete openDraftEditors[wid];
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-save-draft')) {{
      const wid = target.getAttribute('data-save-draft');
      const ta = document.getElementById(`wb-draft-ta-${{wid}}`);
      const ag = document.getElementById(`wb-draft-ag-${{wid}}`);
      const bodyVal = ta ? ta.value.trim() : '';
      if (!wid || !bodyVal) return;
      const fmtVal = detectDraftFormat(bodyVal);
      const ok = await postJson('/api/work/draft/save', {{
        work_id: wid,
        body: bodyVal,
        format: fmtVal,
        auto_gate: ag ? Boolean(ag.checked) : true,
      }});
      if (ok) {{
        delete openDraftEditors[wid];
        render();
      }}
      return;
    }}
    if (target.hasAttribute('data-clear-draft')) {{
      const wid = target.getAttribute('data-clear-draft');
      if (!wid) return;
      delete openDraftEditors[wid];
      void postJson('/api/work/draft/clear', {{ work_id: wid }});
      return;
    }}
    if (target.hasAttribute('data-mat-draft')) {{
      const wid = target.getAttribute('data-mat-draft');
      if (!wid) return;
      void postJson('/api/work/draft/materialize', {{
        work_id: wid,
        force_new: target.getAttribute('data-force-new') === 'true',
      }});
      return;
    }}
    if (target.hasAttribute('data-add-link')) {{
      const wid = target.getAttribute('data-add-link');
      const akey = window.prompt('Artifact key (e.g. gerrit:skia:1414016, buganizer:580955594, gmail_thread:...):');
      if (!akey) return;
      void postJson('/api/work/link', {{ work_id: wid, artifact_key: akey, role: 'primary' }});
    }}
  }});

  const RE_REL_ET_SUFFIX = /\\s*\\((?:just now|(?:\\d+[mhd]\\s+)+ago)\\)$/i;

  // Strip volatile sync clock and relative "(Xm ago)" suffixes to detect real card deltas
  function projectionRenderSignature(st) {{
    if (!st || typeof st !== 'object') return '';
    const payload = {{
      counts: st.counts || {{}},
      metrics: st.metrics || {{}},
      unanswered_tray: st.unanswered_tray || [],
      works: st.works || [],
    }};
    // Drop derived search_text and minute-ticking relative ET suffixes from render comparison
    return JSON.stringify(payload, (k, v) => {{
      if (k === 'synced_et' || k === 'search_text') return undefined;
      if (typeof v === 'string' && k.endsWith('_et')) {{
        return v.replace(RE_REL_ET_SUFFIX, '');
      }}
      return v;
    }});
  }}

  render();
  let lastRenderedSig = projectionRenderSignature(state);
  let pollInFlight = false;
  notifyParentRendered();

  // Poll /api/state every 60s with timeout and skip innerHTML rebuilds when data is unchanged
  async function pollState() {{
    // Serialize background polls so slow network responses never overlap
    if (pollInFlight) return;
    pollInFlight = true;
    const ctrl = typeof AbortController === 'function' ? new AbortController() : null;
    const timer = ctrl ? setTimeout(() => ctrl.abort(), 15000) : 0;
    if (timer && typeof timer.unref === 'function') timer.unref();
    try {{
      const reqOpts = ctrl
        ? {{ cache: 'no-store', signal: ctrl.signal }}
        : {{ cache: 'no-store' }};
      const r = await fetch('/api/state', reqOpts);
      if (!r || !r.ok) return;
      const nextState = await r.json();
      const nextVer = Number((nextState && nextState.ui_version) || 0);
      // Reload full HTML on daemon code updates only when no inline draft editor holds unsaved text
      if (
        UI_VERSION > 0
        && nextVer > 0
        && nextVer !== UI_VERSION
        && Object.keys(openDraftEditors).length === 0
        && window.location
        && typeof window.location.reload === 'function'
      ) {{
        window.location.reload();
        return;
      }}
      const nextSig = projectionRenderSignature(nextState);
      state = nextState;
      // Fast-path no-op syncs: update timestamp and parent watchdog heartbeat without DOM rebuilds
      if (lastRenderedSig && nextSig === lastRenderedSig) {{
        const syncedEl = document.getElementById('wb-synced');
        if (syncedEl) {{
          syncedEl.textContent = 'Synced ' + ((state && state.synced_et) || '');
        }}
        notifyParentRendered();
        return;
      }}
      // Preserve vertical scroll offset across real card list updates
      const prevScrollY = typeof window.scrollY === 'number' ? window.scrollY : 0;
      render();
      lastRenderedSig = nextSig;
      if (typeof window.scrollTo === 'function') {{
        window.scrollTo(0, prevScrollY);
      }}
      notifyParentRendered();
    }} catch {{}} finally {{
      if (timer) clearTimeout(timer);
      pollInFlight = false;
    }}
  }}

  const pollTimer = setInterval(pollState, 60000);
  if (pollTimer && typeof pollTimer.unref === 'function') pollTimer.unref();
}})();
</script>
</body>
</html>"""


def _file_stat_sig(path: str) -> tuple[int, int]:
  """Returns (mtime_ns, size) for path, or (0, 0) when missing."""
  try:
    st = os.stat(path)
    return (st.st_mtime_ns, st.st_size)
  except OSError:
    return (0, 0)


class WorkbenchHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
  allow_reuse_address = True
  daemon_threads = True
  refresh_snapshot: Any = None
  bg_sync_guard: Any = None
  get_cached_projection: Any = None


def create_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    store: work_store.WorkStore | None = None,
    cl_state_path: str = ingestor.DEFAULT_CL_WATCHER_STATE,
    watcher_state_path: str = ingestor.DEFAULT_WATCHER_STATE,
    info_path: str = DEFAULT_INFO_FILE,
) -> WorkbenchHTTPServer:
  """Creates a configured WorkbenchHTTPServer with non-blocking snapshot caching."""
  active_store = store or work_store.WorkStore()
  cache_lock = threading.Lock()
  gen_seq = 0
  bg_sync_depth = 0
  snapshot_cache: dict[str, Any] = {
      "gen": 0,
      "sig": None,
      "proj": None,
      "json_bytes": b"",
      "html_bytes": b"",
  }

  @contextlib.contextmanager
  def bg_sync_guard() -> Iterator[None]:
    """Marks a multi-step sync cycle active so HTTP GETs serve warm cache over torn DB states."""
    nonlocal bg_sync_depth
    with cache_lock:
      bg_sync_depth += 1
    try:
      yield
    finally:
      with cache_lock:
        bg_sync_depth = max(0, bg_sync_depth - 1)

  def source_signature() -> tuple[Any, ...]:
    """Computes a lock-free version tuple across in-process SQLite writes and state files."""
    return (
        active_store.write_seq,
        _file_stat_sig(active_store.db_path),
        _file_stat_sig(f"{active_store.db_path}-wal"),
        _file_stat_sig(cl_state_path),
        _file_stat_sig(watcher_state_path),
    )

  def refresh_snapshot(
      nonblocking: bool = False, fallback: dict[str, Any] | None = None
  ) -> dict[str, Any]:
    """Builds a slimmed UI projection under store._lock and encodes bytes outside the lock."""
    nonlocal gen_seq
    if nonblocking:
      if not active_store._lock.acquire(blocking=False):
        return fallback if fallback is not None else {}
    else:
      active_store._lock.acquire()
    try:
      cl_state = ingestor.load_cl_watcher_snapshot(cl_state_path)
      w_state: dict[str, Any] = {}
      # Fall back to an empty unanswered tray if watcher_state.json is mid-rotation or unreadable
      if os.path.isfile(watcher_state_path):
        try:
          with open(watcher_state_path, "r", encoding="utf-8") as f:
            w_state = json.load(f)
        except Exception:
          w_state = {}
      raw_proj = active_store.build_dashboard_projection(
          cl_watcher_state=cl_state,
          unanswered_threads=w_state.get("unanswered_tray") or [],
      )
      slim_proj = slim_ui_projection(raw_proj)
      slim_proj["ui_version"] = _INITIAL_MTIME
      sig_after = source_signature()
      gen_seq += 1
      snap_gen = gen_seq
    finally:
      active_store._lock.release()
    # Encode UTF-8 JSON and SSR HTML outside active_store._lock so SQLite writers never wait on it
    json_bytes = json.dumps(slim_proj, ensure_ascii=False).encode("utf-8")
    html_bytes = render_workbench_html(
        slim_proj, _INITIAL_MTIME, _already_slim=True
    ).encode("utf-8")
    snap = {
        "gen": snap_gen,
        "sig": sig_after,
        "proj": slim_proj,
        "json_bytes": json_bytes,
        "html_bytes": html_bytes,
    }
    with cache_lock:
      # Never let an older slow HTML encode overwrite a newer POST or sync snapshot generation
      if snap_gen >= int(snapshot_cache["gen"] or 0):
        snapshot_cache.update(snap)
        return snap
      return dict(snapshot_cache)

  def get_http_snapshot() -> dict[str, Any]:
    """Returns a fresh snapshot when uncontended, or the warm cached snapshot during syncs."""
    cur_sig = source_signature()
    with cache_lock:
      if snapshot_cache["proj"] is not None and snapshot_cache["sig"] == cur_sig:
        return dict(snapshot_cache)
      has_warm = snapshot_cache["proj"] is not None
      in_bg_sync = bg_sync_depth > 0
      warm_copy = dict(snapshot_cache)
    # Serve warm cache while multi-step sync runs or store._lock is contended
    if has_warm and in_bg_sync:
      return warm_copy
    if has_warm:
      return refresh_snapshot(nonblocking=True, fallback=warm_copy)
    return refresh_snapshot()

  def current_projection() -> dict[str, Any]:
    """Refreshes the cached slim projection synchronously after POST mutations."""
    return refresh_snapshot()["proj"]

  def get_cached_projection() -> dict[str, Any]:
    """Returns a shallow copy of the currently cached slim projection dictionary."""
    with cache_lock:
      return dict(snapshot_cache["proj"] or {})

  class Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt: str, *args: Any) -> None:
      del fmt, args
      return

    def _send_bytes(self, raw: bytes, content_type: str, status: int = 200) -> None:
      """Writes pre-encoded response bytes while ignoring mid-flight client disconnects."""
      try:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        if content_type.startswith("application/json"):
          self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)
      except (BrokenPipeError, ConnectionResetError):
        return

    def _send_json(self, data: Any, status: int = 200) -> None:
      """Serializes data as UTF-8 JSON and writes it to the HTTP client."""
      raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
      self._send_bytes(raw, "application/json; charset=utf-8", status=status)

    def do_GET(self) -> None:
      """Serves cached or refreshed /api/state JSON and / HTML without blocking on sync locks."""
      parsed = urllib.parse.urlparse(self.path)
      if parsed.path == "/api/state":
        snap = get_http_snapshot()
        self._send_bytes(snap["json_bytes"], "application/json; charset=utf-8")
        return
      if parsed.path == "/favicon.ico":
        self.send_response(204)
        self.end_headers()
        return
      if parsed.path in ("/", "/index.html"):
        snap = get_http_snapshot()
        self._send_bytes(snap["html_bytes"], "text/html; charset=utf-8")
        return
      # Redirect standalone /c/<cid> links (e.g. middle-click) to the Jetski Web box URL
      if parsed.path.startswith("/c/"):
        cid = parsed.path[len("/c/") :].strip("/")
        if not ingestor.is_valid_conversation_id(cid):
          self._send_json({"error": "Invalid conversation_id"}, status=400)
          return
        self.send_response(302)
        self.send_header("Location", f"{ingestor.jetski_box_url()}/c/{cid}")
        self.end_headers()
        return
      self._send_json({"error": "Not found"}, status=404)

    def do_POST(self) -> None:
      """Handles JSON mutation endpoints for Works, Obligations, Links, Sync, and Reply Drafts."""
      parsed = urllib.parse.urlparse(self.path)
      length = int(self.headers.get("Content-Length") or 0)
      raw_body = self.rfile.read(length).decode("utf-8") if length > 0 else "{}"
      try:
        body = json.loads(raw_body) if raw_body else {}
      except Exception:
        body = {}

      if parsed.path == "/api/work/create":
        title = str(body.get("title") or "New Work").strip()
        wid = active_store.create_work(
            title,
            project_id=str(body.get("project_id") or ""),
            roadmap_item_key=str(body.get("roadmap_item_key") or ""),
            meta={"title_manual": True},
            owner="user",
        )
        primaries_in = [str(p) for p in (body.get("primaries") or []) if str(p).strip()]
        channels_in = [str(ch) for ch in (body.get("channels") or []) if str(ch).strip()]
        for p in primaries_in:
          ck = work_store.canonicalize_artifact_key(p)
          active_store.upsert_artifact(ck, title=ck, url=workctl._default_url_for_key(ck))
          active_store.link_artifact(wid, ck, role="primary", source="manual")
        for idx, ch in enumerate(channels_in):
          ck = work_store.canonicalize_artifact_key(ch)
          existing_art = active_store.get_artifact(ck)
          existing_st = (existing_art or {}).get("state") or {}
          extra_st = work_store.manual_correspondence_state(
              ck, existing_st, is_channel_slot=True
          )
          ch_title = title if (not primaries_in and idx == 0 and not existing_art) else ck
          active_store.upsert_artifact(
              ck, title=ch_title, url=workctl._default_url_for_key(ck), state=extra_st
          )
          ch_role = "primary" if (not primaries_in and idx == 0) else "channel"
          active_store.link_artifact(wid, ck, role=ch_role, source="manual")
        ob_desc = str(body.get("obligation") or "").strip()
        if ob_desc:
          active_store.add_obligation(
              wid,
              description=ob_desc,
              trigger_expr=str(body.get("trigger") or "always"),
          )
        active_store.reconcile_correspondence_obligations(wid)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/link":
        wid = str(body.get("work_id") or "")
        ck = work_store.canonicalize_artifact_key(str(body.get("artifact_key") or ""))
        if wid and ck:
          role_val = str(body.get("role") or "primary")
          existing_art = active_store.get_artifact(ck)
          existing_st = (existing_art or {}).get("state") or {}
          extra_st = work_store.manual_correspondence_state(
              ck, existing_st, is_channel_slot=(role_val == "channel")
          )
          active_store.upsert_artifact(
              ck, title=ck, url=workctl._default_url_for_key(ck), state=extra_st
          )
          active_store.link_artifact(wid, ck, role=role_val, source="manual")
          active_store.reconcile_correspondence_obligations(wid)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/unlink":
        wid = str(body.get("work_id") or "")
        ck = str(body.get("artifact_key") or "")
        if wid and ck:
          active_store.unlink_artifact(wid, ck)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/done":
        wid = str(body.get("work_id") or "")
        if wid:
          active_store.complete_work(wid, roadmap_cb=workctl._roadmap_writeback)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/accept":
        wid = str(body.get("work_id") or "")
        if wid:
          active_store.set_work_owner(wid, "user")
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/draft/save":
        wid = str(body.get("work_id") or "").strip()
        draft_body = str(body.get("body") or "").strip()
        if not wid or not draft_body:
          self._send_json({"error": "work_id and non-empty body are required"}, status=400)
          return
        raw_fmt = str(body.get("format") or "").strip().lower()
        fmt_val = raw_fmt if raw_fmt in ("html", "text") else _detect_draft_format(draft_body)
        subj_val = str(body.get("subject") or "")
        m_keys = ingestor.extract_draft_mentioned_keys(subj_val, draft_body)
        ag_val = bool(body["auto_gate"]) if "auto_gate" in body else None
        try:
          active_store.save_reply_draft(
              wid,
              draft_body,
              fmt=fmt_val,
              thread_key=str(body.get("thread_key") or ""),
              to=str(body.get("to") or ""),
              cc=str(body.get("cc") or ""),
              subject=subj_val,
              trigger_expr=str(body.get("trigger_expr") or ""),
              auto_gate=ag_val,
              mentioned_keys=m_keys,
          )
        except ValueError as exc:
          self._send_json({"error": str(exc)}, status=400)
          return
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/draft/clear":
        wid = str(body.get("work_id") or "").strip()
        if wid:
          active_store.clear_reply_draft(wid)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/work/draft/materialize":
        wid = str(body.get("work_id") or "").strip()
        if not wid:
          self._send_json({"error": "work_id is required"}, status=400)
          return
        try:
          mat = ingestor.materialize_reply_draft(
              active_store, wid, force_new=bool(body.get("force_new"))
          )
        except ValueError as exc:
          self._send_json({"error": str(exc)}, status=400)
          return
        except RuntimeError as exc:
          self._send_json({"error": str(exc)}, status=502)
          return
        # Copy cached projection dict before attaching one-shot materialization metadata
        proj = dict(current_projection())
        proj["materialized"] = mat
        self._send_json(proj)
        return

      if parsed.path == "/api/obligation/add":
        wid = str(body.get("work_id") or "")
        desc = str(body.get("description") or "").strip()
        if wid and desc:
          active_store.add_obligation(
              wid,
              description=desc,
              trigger_expr=str(body.get("trigger_expr") or "always"),
              target_artifact_key=str(body.get("target_artifact_key") or ""),
          )
        self._send_json(current_projection())
        return

      if parsed.path == "/api/obligation/done":
        ob_id = str(body.get("obligation_id") or "")
        if ob_id:
          active_store.mark_obligation_done(ob_id)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/obligation/wait":
        ob_id = str(body.get("obligation_id") or "").strip()
        if ob_id:
          active_store.mark_obligation_waiting(ob_id)
        self._send_json(current_projection())
        return

      if parsed.path == "/api/obligation/resolve_ambiguity":
        ob_id = str(body.get("obligation_id") or "").strip()
        decision = str(body.get("decision") or "").strip()
        event_key = str(body.get("event_key") or "").strip()
        if ob_id and decision in ("keep", "close"):
          active_store.resolve_obligation_ambiguity(
              ob_id, decision=decision, event_key=event_key
          )
        self._send_json(current_projection())
        return

      if parsed.path == "/api/tray/dismiss":
        akey = str(body.get("artifact_key") or "").strip()
        if akey:
          try:
            active_store.dismiss_tray_thread(akey)
          except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)
            return
        self._send_json(current_projection())
        return

      # Route standalone :8090 chat clicks through IPC poll before opening a browser tab
      if parsed.path == "/api/chat/open":
        cid = str(body.get("conversation_id") or "").strip()
        if not ingestor.is_valid_conversation_id(cid):
          self._send_json({"error": "Valid conversation_id is required"}, status=400)
          return
        raw_to = body.get("timeout_sec")
        if raw_to is None:
          timeout_sec = 1.8
        elif isinstance(raw_to, bool):
          self._send_json({"error": "Invalid timeout_sec"}, status=400)
          return
        else:
          try:
            timeout_sec = float(raw_to)
          except (TypeError, ValueError):
            self._send_json({"error": "Invalid timeout_sec"}, status=400)
            return
          if not math.isfinite(timeout_sec) or timeout_sec < 0.0:
            self._send_json({"error": "Invalid timeout_sec"}, status=400)
            return
          timeout_sec = min(timeout_sec, ingestor.MAX_OPEN_CHAT_TIMEOUT_SEC)
        self._send_json(ingestor.open_chat_in_jetski(cid, timeout_sec=timeout_sec))
        return

      # Materialize virtual cards on demand, spawn/reuse chat, and return refreshed projection
      if parsed.path == "/api/work/start_chat":
        wid = str(body.get("work_id") or "").strip()
        if not wid:
          self._send_json({"error": "work_id is required"}, status=400)
          return
        try:
          started = ingestor.start_chat_for_work(
              active_store,
              wid,
              cl_state_path=cl_state_path,
              force_new=bool(body.get("force_new")),
          )
        except ValueError as exc:
          self._send_json({"error": str(exc)}, status=400)
          return
        except Exception as exc:
          self._send_json({"error": str(exc)}, status=502)
          return
        # Copy cached projection dict before attaching one-shot started_chat metadata
        proj = dict(current_projection())
        proj["started_chat"] = started
        self._send_json(proj)
        return

      if parsed.path == "/api/sync":
        with bg_sync_guard():
          ingestor.run_sync_cycle(
              active_store,
              cl_state_path=cl_state_path,
              watcher_state_path=watcher_state_path,
              include_gmail=True,
          )
        self._send_json(current_projection())
        return

      self._send_json({"error": "Unknown endpoint"}, status=404)

  srv = WorkbenchHTTPServer((host, port), Handler)
  srv.refresh_snapshot = refresh_snapshot
  srv.bg_sync_guard = bg_sync_guard
  srv.get_cached_projection = get_cached_projection
  actual_port = srv.server_address[1]
  export_workbench_info(actual_port, info_path=info_path)
  return srv


def main(argv: list[str] | None = None) -> int:
  """Runs the Workbench HTTP server and periodic background sync loop."""
  parser = argparse.ArgumentParser(description="Workbench HTTP server & passive sync daemon")
  parser.add_argument("--host", default="0.0.0.0")
  parser.add_argument("--port", type=int, default=DEFAULT_PORT)
  parser.add_argument("--interval", type=int, default=45)
  parser.add_argument("--once", action="store_true", help="Run one sync cycle and exit")
  args = parser.parse_args(argv)

  store = work_store.WorkStore()
  if args.once:
    ingestor.run_sync_cycle(store)
    export_workbench_info(args.port)
    return 0

  srv = create_server(host=args.host, port=args.port, store=store)
  # Warm initial snapshot from SQLite before bg_loop runs the first external sync cycle
  srv.refresh_snapshot()
  stop_event = threading.Event()

  def bg_loop() -> None:
    """Executes periodic ingestor sync cycles and swaps the pre-encoded UI snapshot."""
    while True:
      try:
        with srv.bg_sync_guard():
          ingestor.run_sync_cycle(store)
        srv.refresh_snapshot()
        export_workbench_info(args.port)
      except Exception:
        pass
      if stop_event.wait(max(10, args.interval)):
        break

  t = threading.Thread(target=bg_loop, daemon=True)
  t.start()
  try:
    srv.serve_forever()
  except KeyboardInterrupt:
    pass
  finally:
    stop_event.set()
    srv.server_close()
  return 0


if __name__ == "__main__":
  sys.exit(main())
