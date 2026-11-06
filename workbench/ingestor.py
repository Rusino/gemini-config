#!/usr/bin/env python3
"""Passive multi-source ingestor for Workbench (transcripts, cl_watcher_state, Gmail, roadmaps)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime
import fcntl
import glob
import html as html_lib
import json
import os
import re
import subprocess
import time
from typing import Any

import work_store

DEFAULT_CL_WATCHER_STATE = os.path.expanduser(
    "~/.gemini/config/logs/cl_watcher_state.json"
)
DEFAULT_WATCHER_STATE = os.path.expanduser(
    "~/.gemini/config/logs/workbench/watcher_state.json"
)
DEFAULT_BRAIN_DIR = os.path.expanduser("~/.gemini/jetski/brain")
DEFAULT_DOSSIERS_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-dossiers.json"
)
GMAIL_BIN = "/google/bin/releases/gemini-agents-gmail/gmail"

RE_SKIA_GERRIT = re.compile(
    r"(?:skia-review\.googlesource\.com/c/skia/\+/|\bGerrit\s+|\b--change[=\s]+)(\d{6,8})\b",
    re.IGNORECASE,
)
RE_CHROMIUM_GERRIT = re.compile(
    r"(?:chromium-review\.googlesource\.com/c/chromium/src/\+/|\bcrrev(?:\.com)?/c/)(\d{6,8})\b",
    re.IGNORECASE,
)
RE_GITHUB_PR = re.compile(
    r"(?:github\.com/flutter/flutter/pull/|\bPR\s+#)(\d{4,7})\b",
    re.IGNORECASE,
)
RE_FLUTTER_ISSUE = re.compile(
    r"(?:github\.com/flutter/flutter/issues/|\bflutter#)(\d{4,7})\b",
    re.IGNORECASE,
)
RE_BUGANIZER = re.compile(
    r"(?:(?:b\.corp\.google\.com|issues\.chromium\.org)/issues/"
    r"|\b(?:b|crbug(?:\.com)?)/"
    r"|\bbug_actions\.py\s+(?:render|close_fixed|reassign_merge)\s+)(\d{7,10})\b",
    re.IGNORECASE,
)
RE_GMAIL_THREAD = re.compile(
    r"(?:mail\.google\.com/mail/u/\d+/#(?:inbox|sent|all)/"
    r"|\bthread:|--thread[=\s]+)([0-9a-f]{15,16})\b",
    re.IGNORECASE,
)
RE_GMAIL_DRAFT = re.compile(
    r"(?:mail\.google\.com/mail/u/\d+/#drafts/"
    r"|\bdraft:|(?:update-draft|get-draft)\s+)(r-?\d{15,22})\b",
    re.IGNORECASE,
)
RE_GDOC = re.compile(
    r"docs\.google\.com/document/d/([A-Za-z0-9_\-]{20,64})",
    re.IGNORECASE,
)
RE_ROOT_CONV = re.compile(
    r"root conversation \(conversation://([0-9a-f\-]{36})\)",
    re.IGNORECASE,
)
RE_PREV_CONV = re.compile(
    r"previous conversation \(conversation://([0-9a-f\-]{36})\)",
    re.IGNORECASE,
)
DUMMY_IDS = frozenset({
    "12345",
    "123456",
    "1234567",
    "12345678",
    "123456789",
})

WRITE_CMD_PATTERNS = (
    "gerrit_draft.py save",
    "finalize_review.py",
    "git cl upload",
    "gh pr create",
    "create-draft",
    "update-draft",
    "gmail mutate send",
    "gmail mutate reply",
    "bug_actions.py close_fixed",
    "bug_actions.py reassign_merge",
)

WRITE_TOOL_NAMES = frozenset({
    "update_doc",
    "create_file",
    "send_message_markdown",
})


def extract_artifacts_from_text(text: str) -> list[tuple[str, str, str]]:
  """Returns list of (artifact_key, kind, default_url) in document appearance order."""
  found: dict[str, tuple[int, tuple[str, str, str]]] = {}
  if not text:
    return []

  def _add(pos: int, key: str, kind: str, url: str) -> None:
    if key not in found or pos < found[key][0]:
      found[key] = (pos, (key, kind, url))

  for m in RE_SKIA_GERRIT.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"gerrit:skia:{num}",
          "gerrit",
          f"https://skia-review.googlesource.com/c/skia/+/{num}",
      )

  for m in RE_CHROMIUM_GERRIT.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"gerrit:chromium:{num}",
          "gerrit",
          f"https://chromium-review.googlesource.com/c/chromium/src/+/{num}",
      )

  for m in RE_GITHUB_PR.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"github_pr:flutter/flutter:{num}",
          "github_pr",
          f"https://github.com/flutter/flutter/pull/{num}",
      )

  for m in RE_FLUTTER_ISSUE.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"github_issue:flutter/flutter:{num}",
          "github_issue",
          f"https://github.com/flutter/flutter/issues/{num}",
      )

  for m in RE_BUGANIZER.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"buganizer:{num}",
          "buganizer",
          f"https://issues.chromium.org/issues/{num}",
      )

  for m in RE_GMAIL_THREAD.finditer(text):
    tid = m.group(1).lower()
    _add(
        m.start(),
        f"gmail_thread:{tid}",
        "gmail_thread",
        f"https://mail.google.com/mail/u/0/#inbox/{tid}",
    )

  for m in RE_GMAIL_DRAFT.finditer(text):
    did = m.group(1)
    _add(
        m.start(),
        f"gmail_draft:{did}",
        "gmail_draft",
        f"https://mail.google.com/mail/u/0/#drafts/{did}",
    )

  for m in RE_GDOC.finditer(text):
    doc_id = m.group(1)
    _add(
        m.start(),
        f"gdoc:{doc_id}",
        "gdoc",
        f"https://docs.google.com/document/d/{doc_id}/edit",
    )

  return [item for _, item in sorted(found.values(), key=lambda x: x[0])]


_RE_INLINE_FLUTTER_TITLE = re.compile(
    r"(?:#|flutter#)(\d{4,7})\b\s*[—–\-:]\s*([^\n(]+)",
    re.IGNORECASE,
)
_RE_INLINE_BUGANIZER_TITLE = re.compile(
    r"\b(?:b|crbug)/(\d{7,10})\b\s*[—–\-:]\s*([^\n(]+)",
    re.IGNORECASE,
)


def extract_bug_titles_from_html_or_text(raw: str) -> dict[str, str]:
  """Extracts human bug titles from structured email/draft bullet items."""
  if not raw:
    return {}
  # Convert block-level HTML breaks to newlines before stripping tags
  norm = re.sub(r"</?(?:li|br|p|div|tr)[^>]*>", "\n", raw, flags=re.IGNORECASE)
  norm = html_lib.unescape(re.sub(r"<[^>]+>", " ", norm))
  titles: dict[str, str] = {}
  for line in norm.splitlines():
    clean_line = " ".join(line.split()).strip()
    if not clean_line:
      continue
    for m in _RE_INLINE_FLUTTER_TITLE.finditer(clean_line):
      num = m.group(1)
      desc = m.group(2).strip(" —–-:;,.()")
      if num not in DUMMY_IDS and len(desc) >= 6:
        ck = work_store.canonicalize_artifact_key("flutter_issue", num)
        titles[ck] = desc
    for m in _RE_INLINE_BUGANIZER_TITLE.finditer(clean_line):
      num = m.group(1)
      desc = m.group(2).strip(" —–-:;,.()")
      if num not in DUMMY_IDS and len(desc) >= 6:
        ck = work_store.canonicalize_artifact_key("buganizer", num)
        titles[ck] = desc
  return titles


def _is_write_step(step: Mapping[str, Any]) -> bool:
  tool_calls = step.get("tool_calls")
  if not isinstance(tool_calls, list):
    return False
  for tc in tool_calls:
    if not isinstance(tc, Mapping):
      continue
    tname = str(tc.get("name") or "")
    if tname in WRITE_TOOL_NAMES:
      return True
    args = tc.get("args") or tc.get("arguments") or {}
    if isinstance(args, Mapping):
      if tname in ("edit_file", "write_file"):
        p = str(args.get("Path") or "")
        if p and not any(
            x in p
            for x in (
                "/.gemini/config/",
                "/.gemini/jetski/brain/",
                "/.gemini/jetski/roadmaps/",
                "/tmp/",
                "/var/",
            )
        ):
          return True
      cmd = str(args.get("CommandLine") or "")
      if any(pat in cmd for pat in WRITE_CMD_PATTERNS):
        return True
  return False


def _parse_iso_ts(iso_str: str) -> float:
  """Parses an ISO-8601 timestamp string into POSIX epoch seconds."""
  s = str(iso_str or "").strip()
  if not s:
    return 0.0
  try:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
  except Exception:
    return 0.0


def _read_first_step_ts(transcript_path: str) -> float:
  """Extracts the creation timestamp of the earliest step in a transcript file."""
  if not os.path.isfile(transcript_path):
    return 0.0
  try:
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
      for _ in range(5):
        line = f.readline()
        if not line:
          break
        line = line.strip()
        if not line:
          continue
        step = json.loads(line)
        if isinstance(step, Mapping) and step.get("created_at"):
          ts = _parse_iso_ts(str(step.get("created_at")))
          if ts > 0:
            return ts
  except Exception:
    pass
  return 0.0


def _clean_user_prompt(raw: str) -> str:
  text = raw
  idx = text.find("<SYSTEM_MESSAGE>")
  if idx != -1:
    text = text[:idx]
  idx2 = text.find("<ADDITIONAL_METADATA>")
  if idx2 != -1:
    text = text[:idx2]
  text = text.replace("<USER_REQUEST>", "").replace("</USER_REQUEST>", "").strip()
  for marker in (
      "Latest user request",
      "Original user request from root conversation",
      "Next immediate task",
  ):
    pos = text.find(marker)
    if pos != -1:
      paren_colon = text.find("):", pos)
      colon = (
          paren_colon + 1
          if (paren_colon != -1 and paren_colon - pos < 80)
          else text.find(":", pos)
      )
      if colon != -1:
        sub = text[colon + 1 :].lstrip(" )").strip()
        sub = (
            sub.split("\nNext immediate task:")[0]
            .split("\nWhen finishing the task")[0]
            .split("\nThis prompt is auto-generated")[0]
            .strip()
        )
        if sub:
          return work_store.clean_work_title(sub)[:140]
  if text.startswith("Continuing unfinished work from previous conversation"):
    return ""
  return work_store.clean_work_title(text.splitlines()[0].strip())[:140] if text else ""


def _extract_step_texts(step: Mapping[str, Any]) -> tuple[list[str], list[str]]:
  """Returns (anchor_texts, aux_texts) from a transcript step."""
  anchor_texts: list[str] = []
  aux_texts: list[str] = []
  stype = str(step.get("type") or "")
  if stype == "USER_INPUT":
    content = str(step.get("content") or "")
    idx = content.find("<SYSTEM_MESSAGE>")
    if idx != -1:
      content = content[:idx]
    # Skip auto-generated continuation prompts so old baton text does not anchor unrelated CLs
    cleaned_c = content.replace("<USER_REQUEST>", "").strip()
    if cleaned_c and not cleaned_c.startswith(
        "Continuing unfinished work from previous conversation"
    ):
      anchor_texts.append(content)
  elif stype == "PLANNER_RESPONSE":
    content = str(step.get("content") or "")
    if content.strip():
      arts = extract_artifacts_from_text(content)
      prim_count = sum(
          1
          for _, k, _ in arts
          if k in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
      )
      if prim_count <= 3:
        aux_texts.append(content)

  tool_calls = step.get("tool_calls")
  if isinstance(tool_calls, list):
    for tc in tool_calls:
      if not isinstance(tc, Mapping):
        continue
      tname = str(tc.get("name") or "")
      if tname in ("write_file", "edit_file", "read_file"):
        continue
      args = tc.get("args") or tc.get("arguments") or {}
      if isinstance(args, Mapping):
        if tname == "run_command":
          cmd = str(args.get("CommandLine") or "")
          if cmd:
            if any(pat in cmd for pat in WRITE_CMD_PATTERNS):
              anchor_texts.append(cmd)
            else:
              aux_texts.append(cmd)
        elif tname in (
            "call_mcp_tool",
            "update_doc",
            "create_file",
            "send_message",
            "send_message_markdown",
        ):
          blob = json.dumps(args, ensure_ascii=False)
          if tname in WRITE_TOOL_NAMES:
            anchor_texts.append(blob)
          else:
            aux_texts.append(blob)
  return anchor_texts, aux_texts


def ingest_transcript_file(
    store: work_store.WorkStore,
    conv_id: str,
    root_cid: str,
    transcript_path: str,
    cursors: dict[str, dict[str, float | int]],
    chat_title: str = "",
    project_id: str = "",
    dossiers: Mapping[str, Any] | None = None,
    now: float | None = None,
) -> bool:
  """Incrementally scans a transcript.jsonl file and applies Read vs Write ingestion rules."""
  cur_now = time.time() if now is None else now
  if not os.path.isfile(transcript_path):
    return False
  try:
    st = os.stat(transcript_path)
  except OSError:
    return False

  prev = cursors.get(conv_id) or {}
  prev_mtime = float(prev.get("mtime") or 0.0)
  prev_offset = int(prev.get("offset") or 0)
  if st.st_mtime <= prev_mtime and st.st_size == prev_offset:
    return False

  has_write = bool(prev.get("has_write"))
  extracted: dict[str, tuple[str, str, str]] = {}
  anchor_keys: set[str] = set()
  if chat_title:
    for akey, akind, aurl in extract_artifacts_from_text(chat_title):
      extracted[akey] = (akey, akind, aurl)
      anchor_keys.add(akey)

  first_user_prompt = ""
  first_step_ts = 0.0
  detected_root = root_cid or conv_id
  try:
    with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
      for line in f:
        line = line.strip()
        if not line:
          continue
        try:
          step = json.loads(line)
        except Exception:
          continue
        if not isinstance(step, Mapping):
          continue
        if first_step_ts <= 0 and step.get("created_at"):
          first_step_ts = _parse_iso_ts(str(step.get("created_at")))
        if step.get("type") == "USER_INPUT" and not first_user_prompt:
          raw_u = str(step.get("content") or "")
          m_root = RE_ROOT_CONV.search(raw_u) or RE_PREV_CONV.search(raw_u)
          if m_root and (not root_cid or root_cid == conv_id):
            detected_root = m_root.group(1)
          first_user_prompt = _clean_user_prompt(raw_u)
        if _is_write_step(step):
          has_write = True
        anc_snippets, aux_snippets = _extract_step_texts(step)
        for snippet in anc_snippets:
          step_arts = extract_artifacts_from_text(snippet)
          if len(step_arts) > 5:
            continue
          for akey, akind, aurl in step_arts:
            extracted[akey] = (akey, akind, aurl)
            anchor_keys.add(akey)
        for snippet in aux_snippets:
          step_arts = extract_artifacts_from_text(snippet)
          if len(step_arts) > 4:
            continue
          for akey, akind, aurl in step_arts:
            extracted[akey] = (akey, akind, aurl)
  except OSError:
    return False

  cursors[conv_id] = {
      "mtime": st.st_mtime,
      "offset": st.st_size,
      "has_write": has_write,
  }
  if not extracted:
    return False

  if dossiers and isinstance(dossiers, Mapping) and not chat_title:
    rd = dossiers.get(detected_root) or dossiers.get(detected_root[:8]) or {}
    if isinstance(rd, Mapping):
      raw_h = work_store.clean_work_title(str(rd.get("title") or rd.get("heading") or ""))
      if raw_h:
        chat_title = raw_h
        for akey, akind, aurl in extract_artifacts_from_text(chat_title):
          extracted[akey] = (akey, akind, aurl)
          anchor_keys.add(akey)
      if not project_id:
        project_id = str(
            rd.get("project_name") or rd.get("projectId") or rd.get("project_id") or ""
        )
  else:
    chat_title = work_store.clean_work_title(chat_title)

  # Attribute continuation pieces to the root conversation's earliest step timestamp
  chain_origin_ts = first_step_ts
  if detected_root and detected_root != conv_id:
    brain_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(transcript_path))))
    root_tpath = os.path.join(
        brain_root, detected_root, ".system_generated", "logs", "transcript.jsonl"
    )
    rts = _read_first_step_ts(root_tpath)
    if rts > 0 and (chain_origin_ts <= 0 or rts < chain_origin_ts):
      chain_origin_ts = rts

  chain_key = f"chat_chain:{detected_root}"
  if (
      work_store.is_config_project(project_id)
      or work_store.is_config_text(chat_title)
      or work_store.is_config_text(first_user_prompt)
      or work_store.is_config_artifact(
          chain_key,
          title=chat_title,
          state={"cascade_id": conv_id, "root_cid": detected_root},
          project_id=project_id,
      )
      or work_store.is_transcript_config_only(transcript_path)
  ):
    return False

  if not has_write:
    # Read-only session: never mint a Work card; store artifacts in 7-day candidate buffer
    for akey, akind, aurl in extracted.values():
      if akind == "gmail_draft":
        continue
      store.record_candidate(
          akey,
          kind=akind,
          title=akey,
          url=aurl,
          chat_chain_id=detected_root,
          now=cur_now,
      )
    return True

  # Write session: bound -> attach-first -> mint
  all_primaries = [
      (akey, akind, aurl)
      for akey, akind, aurl in extracted.values()
      if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
  ]
  # Check stored artifact metadata so companion and hub CLs are excluded from primary anchors
  def _is_stored_hub(akey: str) -> bool:
    row = store.get_artifact(akey) or {}
    raw_st = row.get("state_json")
    try:
      st = json.loads(raw_st) if isinstance(raw_st, str) and raw_st else {}
    except Exception:
      st = {}
    return work_store.is_hub_artifact(akey, st)

  non_hub_primaries = [
      (akey, akind, aurl)
      for akey, akind, aurl in all_primaries
      if not _is_stored_hub(akey)
  ]
  title_primaries: list[tuple[str, str, str]] = []
  if chat_title:
    title_keys = {
        akey
        for akey, akind, _ in extract_artifacts_from_text(chat_title)
        if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
    }
    title_primaries = [p for p in non_hub_primaries if p[0] in title_keys]

  anchored_primaries = [p for p in non_hub_primaries if p[0] in anchor_keys]
  if chat_title:
    if title_primaries:
      primaries = title_primaries[:3]
    # Avoid promoting a single mention when a coordinator chat spans >3 distinct bugs/CLs
    elif len(anchored_primaries) == 1 and len(non_hub_primaries) <= 3:
      primaries = anchored_primaries
    else:
      primaries = []
  elif anchored_primaries:
    primaries = anchored_primaries[:2] if len(anchored_primaries) <= 2 else []
  else:
    primaries = non_hub_primaries[:2] if len(non_hub_primaries) <= 2 else []

  prim_set = {x[0] for x in primaries}
  context_primaries = [p for p in all_primaries if p[0] not in prim_set]

  channels = [
      (akey, akind, aurl)
      for akey, akind, aurl in extracted.values()
      if akind in ("gmail_thread", "gchat_thread")
  ]

  work_id = store.find_work_by_artifact(chain_key)
  if not work_id:
    for akey, _, _ in primaries:
      work_id = store.find_work_by_artifact(akey, roles=("primary",))
      if work_id:
        break
  if not work_id and len(channels) == 1:
    work_id = store.find_work_by_artifact(channels[0][0], roles=("primary", "channel"))

  if not work_id:
    if not primaries and not channels:
      return False
    primary_title = ""
    if primaries:
      existing_art = store.get_artifact(primaries[0][0])
      primary_title = str((existing_art or {}).get("title") or primaries[0][0])
    default_title = work_store.clean_work_title(
        chat_title
        or primary_title
        or first_user_prompt
        or f"Work {conv_id[:8]}"
    )
    work_id = store.create_work(default_title, project_id=project_id, now=cur_now)

  existing_chain = store.get_artifact(chain_key) or {}
  prev_chain_title = work_store.clean_work_title(str(existing_chain.get("title") or ""))
  if prev_chain_title.startswith("Chat ") or prev_chain_title.startswith("Continuing unfinished"):
    prev_chain_title = ""
  resolved_chain_title = (
      chat_title
      or prev_chain_title
      or first_user_prompt[:60]
      or f"Chat {detected_root[:8]}"
  )
  chain_state: dict[str, Any] = {"cascade_id": conv_id, "root_cid": detected_root}
  if chain_origin_ts > 0:
    chain_state["origin_ts"] = chain_origin_ts
  store.upsert_artifact(
      chain_key,
      kind="chat_chain",
      title=resolved_chain_title,
      url=f"/c/{conv_id}",
      state=chain_state,
      now=cur_now,
  )
  store.link_artifact(work_id, chain_key, role="channel", source="auto", now=cur_now)

  for akey, akind, aurl in primaries:
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    existing_owner = store.find_work_by_artifact(akey, roles=("primary",))
    if existing_owner and existing_owner != work_id:
      store.link_artifact(work_id, akey, role="context_only", source="auto", now=cur_now)
    else:
      store.link_artifact(work_id, akey, role="primary", source="auto", now=cur_now)

  for akey, akind, aurl in context_primaries:
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    store.link_artifact(work_id, akey, role="context_only", source="auto", now=cur_now)

  # Single human thread in write session attaches as channel; multiple go to candidate buffer
  if len(channels) == 1:
    akey, akind, aurl = channels[0]
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    store.link_artifact(work_id, akey, role="channel", source="auto", now=cur_now)
  else:
    for akey, akind, aurl in channels:
      store.record_candidate(
          akey,
          kind=akind,
          title=akey,
          url=aurl,
          chat_chain_id=detected_root,
          now=cur_now,
      )
  return True


def load_cl_watcher_snapshot(path: str = DEFAULT_CL_WATCHER_STATE) -> dict[str, Any]:
  """Passively reads cl_watcher_state.json under a shared lock without mutating it."""
  if not os.path.isfile(path):
    return {"items": {}, "bugs": {}}
  try:
    lock_path = f"{path}.lock"
    if os.path.exists(lock_path):
      with open(lock_path, "r", encoding="utf-8") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_SH)
        try:
          with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        finally:
          fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    else:
      with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, dict) else {"items": {}, "bugs": {}}
  except Exception:
    return {"items": {}, "bugs": {}}


def ingest_cl_watcher_state(
    store: work_store.WorkStore,
    cl_state: Mapping[str, Any],
    now: float | None = None,
) -> None:
  """Syncs multi-entity works (CL + bug, CL stack, or CL + chat + email) from cl_watcher_state."""
  cur_now = time.time() if now is None else now
  items = cl_state.get("items") or {}
  bugs = cl_state.get("bugs") or {}
  if not isinstance(items, Mapping):
    return

  # Upsert all known bugs first so CL->bug links have titles and statuses
  if isinstance(bugs, Mapping):
    for bkey, bval in bugs.items():
      if not isinstance(bval, Mapping):
        continue
      bid = str(bval.get("bug_id") or bval.get("id") or bkey).split(":")[-1]
      if not bid:
        continue
      ckey = work_store.canonicalize_artifact_key("buganizer", bid)
      store.upsert_artifact(
          ckey,
          kind="buganizer",
          title=f"b/{bid}: {bval.get('title') or bval.get('summary') or ''}".strip(": "),
          url=str(bval.get("url") or f"https://issues.chromium.org/issues/{bid}"),
          state=dict(bval),
          now=cur_now,
      )

  for raw_key, item in items.items():
    if not isinstance(item, Mapping):
      continue
    kind = str(item.get("kind") or "gerrit")
    tid = str(item.get("target_id") or raw_key)
    host = str(item.get("host") or "")
    ckey = work_store.canonicalize_artifact_key(kind, tid, host=host)
    subj = str(item.get("subject") or item.get("title") or ckey)
    url = str(item.get("url") or "")
    linked_bug_keys = work_store.extract_cl_bug_keys(item)
    cids = [str(c) for c in (item.get("conversations") or []) if c]
    single_cid = str(item.get("conversation_id") or "").strip()
    if single_cid and single_cid not in cids:
      cids.append(single_cid)

    # Upsert rich bug metadata from linked_bugs and commit_message references
    lb_meta: dict[str, dict[str, str]] = {}
    for lb in item.get("linked_bugs") or []:
      if not isinstance(lb, Mapping):
        continue
      raw_bk = str(lb.get("bug_key") or lb.get("short_id") or "").strip()
      if not raw_bk:
        continue
      if "#" in raw_bk:
        num = raw_bk.split("#")[-1].strip()
      else:
        num = re.split(r"[:/]", raw_bk)[-1].strip()
      if not num.isdigit():
        continue
      b_ck = work_store.canonicalize_artifact_key(
          "flutter_issue" if ("#" in raw_bk or len(num) <= 6) else "buganizer",
          num,
      )
      lb_meta[b_ck] = {
          "title": str(lb.get("title") or "").strip(),
          "status": str(lb.get("status") or "").strip(),
          "url": str(lb.get("url") or "").strip(),
      }

    for b_ckey in linked_bug_keys:
      meta = lb_meta.get(b_ckey) or {}
      short_b = work_store.format_short_bug_id(b_ckey)
      b_desc = meta.get("title") or ""
      b_title = f"{short_b}: {b_desc}" if b_desc else short_b
      b_num = b_ckey.split(":")[-1]
      b_url = meta.get("url") or (
          f"https://github.com/flutter/flutter/issues/{b_num}"
          if b_ckey.startswith("github_issue:")
          else f"https://issues.chromium.org/issues/{b_num}"
      )
      b_state: dict[str, Any] = {"short_id": short_b}
      if b_desc:
        b_state["bug_title"] = b_desc
      if meta.get("status"):
        b_state["status"] = meta["status"]
      store.upsert_artifact(
          b_ckey,
          kind=work_store.artifact_kind_from_key(b_ckey),
          title=b_title,
          url=b_url,
          state=b_state,
          now=cur_now,
      )

    item_state = dict(item)
    item_state["linked_bug_keys"] = linked_bug_keys
    created_ts = float(item.get("created_ts") or 0.0)
    if created_ts > 0:
      item_state["origin_ts"] = created_ts
    store.upsert_artifact(
        ckey,
        kind=work_store.artifact_kind_from_key(ckey),
        title=f"{tid}: {subj}",
        url=url,
        state=item_state,
        now=cur_now,
    )
    if item.get("companion_to") or item.get("is_companion") or item.get("is_cherry_pick"):
      continue

    # Check if this CL is a hub ([SkoreText] or >3 bugs); hubs never auto-merge works
    if work_store.is_hub_artifact(ckey, item_state, linked_bug_count=len(linked_bug_keys)):
      continue

    # Attach this CL to all active Works that already track its bug(s) or stack parent
    matched_wids: list[str] = []
    for w_cand in store.find_all_works_by_artifact(ckey, roles=("primary", "channel")):
      if w_cand not in matched_wids:
        matched_wids.append(w_cand)
    for b_ckey in linked_bug_keys:
      for w_cand in store.find_all_works_by_artifact(b_ckey, roles=("primary",)):
        if w_cand not in matched_wids:
          matched_wids.append(w_cand)
    stack_parent = str(item.get("stack_parent_ikey") or "").strip()
    if stack_parent:
      p_ckey = work_store.canonicalize_artifact_key(stack_parent)
      for w_cand in store.find_all_works_by_artifact(p_ckey, roles=("primary",)):
        if w_cand not in matched_wids:
          matched_wids.append(w_cand)

    has_multi_entities = (
        bool(linked_bug_keys)
        or bool(item.get("fixes_cls"))
        or bool(item.get("fixed_by_cls"))
    )
    if not matched_wids and not has_multi_entities:
      continue

    if not matched_wids:
      matched_wids = [
          store.create_work(
              subj,
              project_id=str(item.get("project") or ""),
              now=cur_now,
          )
      ]
    for wid in matched_wids:
      store.link_artifact(wid, ckey, role="primary", source="auto", now=cur_now)
      for b_ckey in linked_bug_keys:
        store.link_artifact(wid, b_ckey, role="primary", source="auto", now=cur_now)

    primary_wid = matched_wids[0]
    for cid in cids[:2]:
      ch_key = f"chat_chain:{cid}"
      ch_title = str(item.get("chat_title") or f"Chat {cid[:8]}")
      if work_store.is_config_artifact(
          ch_key,
          title=ch_title,
          state={"cascade_id": cid},
      ):
        continue
      store.upsert_artifact(
          ch_key,
          kind="chat_chain",
          title=ch_title,
          url=f"/c/{cid}",
          state={"cascade_id": cid},
          now=cur_now,
      )
      store.link_artifact(primary_wid, ch_key, role="channel", source="auto", now=cur_now)


def _clean_display_name(raw_addr: str) -> str:
  part = str(raw_addr or "").split(",")[0].strip()
  name = part.split("<")[0].strip().strip('"').strip("'")
  if name:
    return name
  return part.strip("<> ")


def _extract_msg_headers(msg: Mapping[str, Any]) -> dict[str, str]:
  payload = msg.get("payload") if isinstance(msg.get("payload"), Mapping) else {}
  headers = payload.get("headers") if isinstance(payload, Mapping) else []
  hdrs = {
      str(h.get("name") or "").lower(): str(h.get("value") or "")
      for h in (headers or [])
      if isinstance(h, Mapping) and h.get("name")
  }
  for k in ("from", "to", "cc", "subject"):
    if k not in hdrs and msg.get(k):
      hdrs[k] = str(msg.get(k) or "")
  return hdrs


def _fetch_gmail_thread_provenance(
    thread_id: str,
    thread_cache: dict[str, Any],
    now: float,
) -> dict[str, Any]:
  """Fetches first-message date, sender/recipient, and reply provenance for a Gmail thread."""
  tid = str(thread_id or "").strip()
  if not tid:
    return {}
  cached = thread_cache.get(tid)
  if isinstance(cached, Mapping) and (now - float(cached.get("fetched_at") or 0.0) < 600):
    return dict(cached)
  try:
    res = subprocess.run(
        [GMAIL_BIN, "readonly", "get-thread", tid, "--json"],
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    if res.returncode != 0 or not res.stdout.strip():
      return dict(cached) if isinstance(cached, Mapping) else {"thread_id": tid}
    td = json.loads(res.stdout)
    msgs = (
        td.get("messages")
        if isinstance(td, Mapping)
        else (td if isinstance(td, list) else [])
    ) or []
    if not msgs or not isinstance(msgs[0], Mapping):
      return {"thread_id": tid}

    m0 = msgs[0]
    h0 = _extract_msg_headers(m0)
    origin_ts = float(int(m0.get("internalDate") or 0)) / 1000.0 if m0.get("internalDate") else 0.0
    raw_subj = h0.get("subject") or ""
    root_subj = re.sub(r"^(?:re|fwd|fw)\s*:\s*", "", raw_subj, flags=re.IGNORECASE).strip()
    from0 = h0.get("from") or ""
    started_by_me = "jlavrova" in from0.lower() or "julia lavrova" in from0.lower()
    origin_from = _clean_display_name(from0)

    counterparty = ""
    counterparty_reply_ts = 0.0
    snippets: list[str] = [root_subj] if root_subj else []
    for m in msgs:
      if not isinstance(m, Mapping):
        continue
      hm = _extract_msg_headers(m)
      snip = str(m.get("snippet") or "")
      if snip:
        snippets.append(snip)
      f_hdr = hm.get("from") or ""
      t_hdr = hm.get("to") or ""
      is_me = "jlavrova" in f_hdr.lower() or "julia lavrova" in f_hdr.lower()
      if not is_me and f_hdr:
        cand = _clean_display_name(f_hdr)
        if cand and not counterparty:
          counterparty = cand
        mts = float(int(m.get("internalDate") or 0)) / 1000.0 if m.get("internalDate") else 0.0
        if mts > counterparty_reply_ts:
          counterparty_reply_ts = mts
      elif is_me and t_hdr and not counterparty:
        for addr in t_hdr.split(","):
          if "jlavrova" not in addr.lower() and "julia lavrova" not in addr.lower():
            cand = _clean_display_name(addr)
            if cand:
              counterparty = cand
              break

    prov = {
        "thread_id": tid,
        "origin_ts": origin_ts,
        "root_subject": root_subj,
        "origin_from": origin_from,
        "started_by_me": started_by_me,
        "counterparty": counterparty,
        "counterparty_reply_ts": counterparty_reply_ts,
        "message_count": len(msgs),
        "thread_text": "\n".join(snippets),
        "fetched_at": now,
    }
    thread_cache[tid] = prov
    return dict(prov)
  except Exception:
    return dict(cached) if isinstance(cached, Mapping) else {"thread_id": tid}


def ingest_gmail_drafts_and_threads(
    store: work_store.WorkStore,
    watcher_state: dict[str, Any],
    now: float | None = None,
) -> list[dict[str, Any]]:
  """Passively queries Gmail CLI for drafts and recent human threads and reconciles obligations."""
  cur_now = time.time() if now is None else now
  unanswered: list[dict[str, Any]] = []
  if not os.path.isfile(GMAIL_BIN):
    return unanswered

  thread_cache = watcher_state.setdefault("gmail_threads", {})

  # 1. Inspect current drafts via gmail readonly drafts --json
  active_draft_ids: set[str] = set()
  try:
    res = subprocess.run(
        [GMAIL_BIN, "readonly", "drafts", "--json"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
      draft_list = json.loads(res.stdout)
      if isinstance(draft_list, dict):
        draft_list = draft_list.get("drafts") or []
      if isinstance(draft_list, list):
        for d_stub in draft_list[:10]:
          did = str((d_stub or {}).get("id") or "").strip()
          if not did:
            continue
          active_draft_ids.add(did)
          d_res = subprocess.run(
              [GMAIL_BIN, "readonly", "get-draft", did, "--json"],
              capture_output=True,
              text=True,
              timeout=6,
              check=False,
          )
          if d_res.returncode != 0 or not d_res.stdout.strip():
            continue
          d_full = json.loads(d_res.stdout)
          msg = d_full.get("message") if isinstance(d_full, Mapping) else {}
          payload = (msg or {}).get("payload") if isinstance(msg, Mapping) else {}
          hdr_map = _extract_msg_headers(msg if isinstance(msg, Mapping) else {})
          thread_id = str(
              (msg or {}).get("threadId")
              or (
                  (d_stub.get("message") or {}).get("threadId")
                  if isinstance(d_stub.get("message"), Mapping)
                  else ""
              )
              or d_stub.get("threadId")
              or ""
          ).strip()
          subj = str(
              hdr_map.get("subject")
              or d_full.get("subject")
              or (msg or {}).get("subject")
              or f"Draft {did[:8]}"
          )
          to_hdr = str(hdr_map.get("to") or d_full.get("to") or (msg or {}).get("to") or "")
          body_b64 = str(((payload or {}).get("body") or {}).get("data") or "")
          decoded_body = ""
          if body_b64:
            try:
              decoded_body = base64.urlsafe_b64decode(body_b64 + "===").decode(
                  "utf-8", errors="ignore"
              )
            except Exception:
              decoded_body = ""
          # Strip quoted history (`<blockquote...`) before extracting draft references
          unquoted_body = decoded_body.split("<blockquote")[0] if decoded_body else ""
          extracted = extract_artifacts_from_text(f"{subj}\n{unquoted_body}")
          inline_bug_titles = extract_bug_titles_from_html_or_text(unquoted_body)
          d_key = f"gmail_draft:{did}"
          to_name = _clean_display_name(to_hdr) if to_hdr else ""
          primaries = [
              (akey, akind, aurl)
              for akey, akind, aurl in extracted
              if akind in ("gerrit", "github_pr", "buganizer", "github_issue", "gdoc")
          ]
          mentioned_keys = [akey for akey, _, _ in primaries]
          draft_bug_keys = [
              akey
              for akey, akind, _ in primaries
              if akind in ("buganizer", "github_issue")
          ]
          store.upsert_artifact(
              d_key,
              kind="gmail_draft",
              title=f"Draft: {subj}" + (f" ({to_name})" if to_name else ""),
              url=f"https://mail.google.com/mail/u/0/#drafts/{did}",
              state={
                  "status": "DRAFT",
                  "thread_id": thread_id,
                  "to": to_hdr,
                  "mentioned_keys": mentioned_keys,
                  "draft_bug_keys": draft_bug_keys,
              },
              now=cur_now,
          )
          if not primaries:
            continue
          wid = store.find_work_by_artifact(d_key, roles=("primary", "channel"))
          if not wid and thread_id:
            wid = store.find_work_by_artifact(
                f"gmail_thread:{thread_id}", roles=("primary", "channel")
            )
          if not wid:
            for akey, _, _ in primaries:
              wid = store.find_work_by_artifact(akey, roles=("primary",))
              if wid:
                break
          if not wid:
            wid = store.create_work(subj, now=cur_now)
          store.link_artifact(wid, d_key, role="channel", source="auto", now=cur_now)
          if thread_id:
            t_key = f"gmail_thread:{thread_id}"
            t_prov = _fetch_gmail_thread_provenance(thread_id, thread_cache, cur_now)
            if not t_prov.get("counterparty") and to_name:
              t_prov["counterparty"] = to_name
            if not t_prov.get("root_subject"):
              t_prov["root_subject"] = re.sub(
                  r"^(?:re|fwd|fw)\s*:\s*", "", subj, flags=re.IGNORECASE
              ).strip()
            t_title = str(t_prov.get("root_subject") or subj)
            cp = str(t_prov.get("counterparty") or "")
            if cp and cp.lower() not in t_title.lower():
              t_title = f"{t_title} ({cp})"
            store.upsert_artifact(
                t_key,
                kind="gmail_thread",
                title=t_title,
                url=f"https://mail.google.com/mail/u/0/#inbox/{thread_id}",
                state=t_prov,
                now=cur_now,
            )
            store.link_artifact(wid, t_key, role="channel", source="auto", now=cur_now)
          for akey, akind, aurl in primaries:
            b_desc = inline_bug_titles.get(akey, "")
            if b_desc:
              short_b = work_store.format_short_bug_id(akey)
              art_title = f"{short_b}: {b_desc}"
              art_state: dict[str, Any] | None = {"short_id": short_b, "bug_title": b_desc}
            else:
              art_title = akey
              art_state = None
            store.upsert_artifact(
                akey,
                kind=akind,
                title=art_title,
                url=aurl,
                state=art_state,
                now=cur_now,
            )
            store.link_artifact(wid, akey, role="primary", source="auto", now=cur_now)

          # Attach any CLs/PRs in the artifact store that fix the draft's primary bugs
          store.reconcile_work_bug_cls(wid, now=cur_now)

          # Build a single consolidated obligation for the draft across all linked CLs/bugs
          if "flutter" in subj.lower() or draft_bug_keys:
            w_roles = store.get_work_artifact_roles(wid)
            primary_gerrit = sorted(
                k for k, r in w_roles.items() if r == "primary" and k.startswith("gerrit:")
            )
            primary_prs = sorted(
                k for k, r in w_roles.items() if r == "primary" and k.startswith("github_pr:")
            )
            preds: list[str] = []
            for gk in primary_gerrit:
              preds.append(f"cl_merged({gk}) AND rolled_into(Flutter, {gk})")
            for pk in primary_prs:
              preds.append(f"cl_merged({pk})")
            if not preds and draft_bug_keys:
              for bk in draft_bug_keys:
                preds.append(f"bug_closed({bk})")
            if preds:
              if len(draft_bug_keys) >= 2:
                ob_desc = (
                    f"Send draft reply in '{subj}' once all {len(draft_bug_keys)} "
                    f"bugs land & roll into Flutter"
                )
              elif len(primary_gerrit) == 1 and not primary_prs:
                cl_num = primary_gerrit[0].split(":")[-1]
                ob_desc = f"Send draft reply in '{subj}' once Gerrit {cl_num} rolls into Flutter"
              else:
                ob_desc = f"Send draft reply in '{subj}' once all linked CLs roll into Flutter"
              store.replace_draft_obligation(
                  wid,
                  draft_key=d_key,
                  description=ob_desc,
                  trigger_expr=" AND ".join(preds),
                  done_matcher=f"reply_sent({d_key})",
                  now=cur_now,
              )
  except Exception:
    pass

  # Reconcile previously tracked drafts that disappeared -> mark SENT
  prev_drafts = set(watcher_state.get("active_draft_ids") or [])
  if active_draft_ids:
    for gone_did in prev_drafts - active_draft_ids:
      d_key = f"gmail_draft:{gone_did}"
      store.upsert_artifact(d_key, kind="gmail_draft", state={"status": "SENT"}, now=cur_now)
    watcher_state["active_draft_ids"] = sorted(active_draft_ids)

  # 2. Link recent human correspondence threads to matching Works and collect unanswered tray
  try:
    res = subprocess.run(
        [
            GMAIL_BIN,
            "readonly",
            "search",
            (
                "newer_than:21d -from:noreply -from:gerrit -from:github.com"
                " -from:buganizer -from:autoroll -from:luci"
            ),
            "--max-results=18",
            "--json",
        ],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
      msgs = json.loads(res.stdout)
      if isinstance(msgs, dict):
        msgs = msgs.get("messages") or msgs.get("threads") or []
      if isinstance(msgs, list):
        seen_tids: set[str] = set()
        for m in msgs[:15]:
          if not isinstance(m, Mapping):
            continue
          tid = str(m.get("threadId") or m.get("id") or "").strip()
          if not tid or tid in seen_tids:
            continue
          seen_tids.add(tid)
          t_prov = _fetch_gmail_thread_provenance(tid, thread_cache, cur_now)
          cp = str(t_prov.get("counterparty") or "").strip()
          root_subj = str(t_prov.get("root_subject") or m.get("subject") or "").strip()
          if not cp or "unsubscribe" in cp.lower() or "noreply" in cp.lower():
            continue
          t_key = f"gmail_thread:{tid}"
          t_arts = extract_artifacts_from_text(str(t_prov.get("thread_text") or root_subj))
          t_primaries = [
              (akey, akind, aurl)
              for akey, akind, aurl in t_arts
              if akind in ("gerrit", "github_pr", "buganizer", "github_issue", "gdoc")
          ]
          bound_wid = store.find_work_by_artifact(t_key, roles=("primary", "channel"))
          if not bound_wid:
            for akey, _, _ in t_primaries:
              bound_wid = store.find_work_by_artifact(akey, roles=("primary",))
              if bound_wid:
                break
          if bound_wid:
            t_title = f"{root_subj} ({cp})" if cp.lower() not in root_subj.lower() else root_subj
            store.upsert_artifact(
                t_key,
                kind="gmail_thread",
                title=t_title or f"Thread {tid[:8]}",
                url=f"https://mail.google.com/mail/u/0/#inbox/{tid}",
                state=t_prov,
                now=cur_now,
            )
            store.link_artifact(bound_wid, t_key, role="channel", source="auto", now=cur_now)
          elif m.get("from") and "jlavrova" not in str(m.get("from")).lower():
            unanswered.append({
                "artifact_key": t_key,
                "thread_id": tid,
                "sender": cp,
                "subject": root_subj or f"Thread {tid[:8]}",
                "snippet": str(m.get("snippet") or "")[:140],
                "url": f"https://mail.google.com/mail/u/0/#inbox/{tid}",
                "bound_work_id": "",
            })
  except Exception:
    pass

  return unanswered


def run_sync_cycle(
    store: work_store.WorkStore,
    cl_state_path: str = DEFAULT_CL_WATCHER_STATE,
    watcher_state_path: str = DEFAULT_WATCHER_STATE,
    brain_dir: str = DEFAULT_BRAIN_DIR,
    include_gmail: bool = True,
    now: float | None = None,
) -> dict[str, Any]:
  """Runs one passive sync cycle across cl_watcher_state, recent transcripts, and Gmail."""
  cur_now = time.time() if now is None else now
  w_state: dict[str, Any] = {}
  if os.path.isfile(watcher_state_path):
    try:
      with open(watcher_state_path, "r", encoding="utf-8") as f:
        w_state = json.load(f)
    except Exception:
      w_state = {}

  cursors = w_state.setdefault("transcript_cursors", {})
  cl_state = load_cl_watcher_snapshot(cl_state_path)
  ingest_cl_watcher_state(store, cl_state, now=cur_now)

  # Scan transcripts modified in the last 14 days
  dossiers: dict[str, Any] = {}
  if os.path.isfile(DEFAULT_DOSSIERS_FILE):
    try:
      with open(DEFAULT_DOSSIERS_FILE, "r", encoding="utf-8") as f:
        raw_d = json.load(f) or {}
        dossiers = raw_d.get("items") or raw_d.get("dossiers") or {}
    except Exception:
      dossiers = {}

  if os.path.isdir(brain_dir):
    cutoff_mtime = cur_now - 14 * 86400
    pattern = os.path.join(brain_dir, "*", ".system_generated", "logs", "transcript.jsonl")
    candidates_t: list[tuple[float, str]] = []
    for tpath in glob.glob(pattern):
      try:
        mt = os.path.getmtime(tpath)
        if mt < cutoff_mtime:
          continue
        candidates_t.append((mt, tpath))
      except OSError:
        continue
    candidates_t.sort(key=lambda x: x[0])
    for _, tpath in candidates_t:
      parts = tpath.split(os.sep)
      conv_id = parts[-4] if len(parts) >= 4 else ""
      if not conv_id:
        continue
      d_info = (
          (dossiers.get(conv_id) or dossiers.get(conv_id[:8]))
          if isinstance(dossiers, Mapping)
          else None
      ) or {}
      root_cid = str(d_info.get("root_id") or conv_id)
      raw_heading = str(d_info.get("title") or d_info.get("heading") or "")
      if raw_heading.startswith("Chain State:"):
        raw_heading = raw_heading[len("Chain State:") :].strip()
      proj_id = str(
          d_info.get("project_name")
          or d_info.get("projectId")
          or d_info.get("project_id")
          or ""
      )
      ingest_transcript_file(
          store,
          conv_id=conv_id,
          root_cid=root_cid,
          transcript_path=tpath,
          cursors=cursors,
          chat_title=raw_heading,
          project_id=proj_id,
          dossiers=dossiers,
          now=cur_now,
      )

  unanswered = w_state.get("unanswered_tray") or []
  if include_gmail and (cur_now - float(w_state.get("last_gmail_sync") or 0.0) >= 90):
    unanswered = ingest_gmail_drafts_and_threads(store, w_state, now=cur_now)
    w_state["unanswered_tray"] = unanswered
    w_state["last_gmail_sync"] = cur_now

  store.purge_config_artifacts_and_works(now=cur_now)
  w_state["last_sync"] = cur_now
  try:
    os.makedirs(os.path.dirname(watcher_state_path), exist_ok=True)
    tmp = f"{watcher_state_path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(w_state, f, indent=2)
    os.replace(tmp, watcher_state_path)
  except OSError:
    pass

  return store.build_dashboard_projection(
      cl_watcher_state=cl_state,
      unanswered_threads=unanswered,
      now=cur_now,
  )
