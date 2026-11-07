#!/usr/bin/env python3
"""Passive multi-source ingestor for Workbench (transcripts, cl_watcher_state, Gmail, roadmaps)."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from datetime import datetime
from email.utils import getaddresses
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
# Cap small-group human threads above typical colleague CC lists while cutting 39-person blasts
MAX_HUMAN_GROUP_RECIPIENTS = 8

_NON_PERSONAL_CATEGORIES = frozenset({
    "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
    "CATEGORY_SOCIAL",
})
_RE_AUTOMATED_SENDER_ADDR = re.compile(
    r"(?:noreply|no-reply|donotreply|do-not-reply|calendar-notification"
    r"|gemini-notes@google\.com|e-training@google\.com"
    r"|peopleops-help@google\.com|\.appspotmail\.com"
    r"|\.gserviceaccount\.com|\bmdb\.[^@\s]+@|\b(?:organizers|unsubscribe)@)",
    re.IGNORECASE,
)
_RE_CALENDAR_SUBJECT = re.compile(
    r"^(?:Accepted|Declined|Tentative|Invitation|Updated invitation|Canceled event)"
    r"(?:\s+with\s+note)?:",
    re.IGNORECASE,
)
_RE_MEETING_NOTES_SUBJECT = re.compile(r"^Notes:\s*[“\"']", re.IGNORECASE)

RE_SKIA_GERRIT = re.compile(
    r"(?:skia-review\.googlesource\.com/c/skia/\+/|\bGerrit\s+|\b--change[=\s]+)(\d{6,8})\b",
    re.IGNORECASE,
)
RE_CHROMIUM_GERRIT = re.compile(
    r"(?:chromium-review\.googlesource\.com/c/chromium/src/\+/|\bcrrev(?:\.com)?/c/)(\d{6,8})\b",
    re.IGNORECASE,
)
RE_GITHUB_PR = re.compile(
    r"(?:github\.com/flutter/flutter/pull/|\bPR\s+#"
    r"|\[flutter/flutter\][^\n]*?\(PR\s+#)(\d{4,7})\b",
    re.IGNORECASE,
)
RE_FLUTTER_ISSUE = re.compile(
    r"(?:github\.com/flutter/flutter/issues/|\bflutter#"
    r"|\[flutter/flutter\][^\n]*?\(Issue\s+#)(\d{4,7})\b",
    re.IGNORECASE,
)
RE_PIPER_CL = re.compile(
    r"(?:http://cl/|\bcl/|\bCL\s+)(\d{9,10})\b",
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
    r"|\bthread:|--thread[=\s]+|\bget-thread\s+)([0-9a-f]{15,16})\b",
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

  for m in RE_PIPER_CL.finditer(text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"piper:{num}",
          "piper",
          f"http://cl/{num}",
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
_RE_GH_NOTIF_ISSUE_TITLE = re.compile(
    r"\[flutter/flutter\]\s+(.+?)\s+\(Issue\s+#(\d{4,7})\)",
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
    for m in _RE_GH_NOTIF_ISSUE_TITLE.finditer(clean_line):
      desc = m.group(1).strip(" —–-:;,.()")
      num = m.group(2)
      if num not in DUMMY_IDS and len(desc) >= 6:
        ck = work_store.canonicalize_artifact_key("flutter_issue", num)
        titles[ck] = desc
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


def _extract_step_texts(
    step: Mapping[str, Any], is_first_user_turn: bool = False
) -> tuple[list[str], list[str]]:
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
      if is_first_user_turn:
        anchor_texts.append(content)
      else:
        aux_texts.append(content)
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
  companion_cls: dict[str, str] = {}
  mention_steps: dict[str, int] = {}
  if chat_title:
    for akey, akind, aurl in extract_artifacts_from_text(chat_title):
      extracted[akey] = (akey, akind, aurl)
      anchor_keys.add(akey)

  first_user_prompt = ""
  first_user_step_seen = False
  first_step_ts = 0.0
  detected_root = root_cid or conv_id
  re_comp = re.compile(
      r"(?:companion\s+WIP(?:\s+CL)?|WIP[-\s]компаньон)[^0-9\n]{0,80}?"
      r"(?:skia-review\.googlesource\.com/c/skia/\+/|\bGerrit\s+)(\d{6,8})\b"
      r"(?:\s*\(([^)\n]{3,80})\))?",
      re.IGNORECASE,
  )
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
        step_idx = int(step.get("step_index") or 0)
        if first_step_ts <= 0 and step.get("created_at"):
          first_step_ts = _parse_iso_ts(str(step.get("created_at")))
        is_first_u = False
        if step.get("type") == "USER_INPUT":
          raw_u = str(step.get("content") or "")
          if not first_user_prompt:
            m_root = RE_ROOT_CONV.search(raw_u) or RE_PREV_CONV.search(raw_u)
            if m_root and (not root_cid or root_cid == conv_id):
              detected_root = m_root.group(1)
            first_user_prompt = _clean_user_prompt(raw_u)
          if not first_user_step_seen:
            first_user_step_seen = True
            is_first_u = True
            # If step 0 is an auto-generated continuation prompt, anchor on the extracted user request
            if first_user_prompt:
              for akey, akind, aurl in extract_artifacts_from_text(first_user_prompt):
                extracted[akey] = (akey, akind, aurl)
                anchor_keys.add(akey)
        elif step.get("type") == "PLANNER_RESPONSE":
          p_text = str(step.get("content") or "")
          for m_c in re_comp.finditer(p_text):
            c_num = m_c.group(1)
            c_lbl = (m_c.group(2) or "").strip()
            c_akey = work_store.canonicalize_artifact_key("gerrit", c_num)
            companion_cls[c_akey] = c_lbl
            extracted[c_akey] = (
                c_akey,
                "gerrit",
                f"https://skia-review.googlesource.com/c/skia/+/{c_num}",
            )
            mention_steps.setdefault(c_akey, step_idx)
        if _is_write_step(step):
          has_write = True
        anc_snippets, aux_snippets = _extract_step_texts(step, is_first_user_turn=is_first_u)
        for snippet in anc_snippets:
          step_arts = extract_artifacts_from_text(snippet)
          if len(step_arts) > 5:
            continue
          for akey, akind, aurl in step_arts:
            extracted[akey] = (akey, akind, aurl)
            if akey not in companion_cls:
              anchor_keys.add(akey)
            mention_steps.setdefault(akey, step_idx)
        for snippet in aux_snippets:
          step_arts = extract_artifacts_from_text(snippet)
          if len(step_arts) > 4:
            continue
          for akey, akind, aurl in step_arts:
            extracted[akey] = (akey, akind, aurl)
            mention_steps.setdefault(akey, step_idx)
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
      if akey not in companion_cls and not _is_stored_hub(akey)
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
    elif len(anchored_primaries) <= 2 and len(non_hub_primaries) <= 4:
      primaries = anchored_primaries[:2]
    else:
      primaries = []
  elif anchored_primaries:
    primaries = anchored_primaries[:2] if len(anchored_primaries) <= 2 else []
  else:
    # Mid-chat mentions (step > 0) without a step-0/title/write anchor never become primary
    primaries = []

  prim_set = {x[0] for x in primaries}
  context_primaries = [
      p for p in all_primaries if p[0] not in prim_set and p[0] not in companion_cls
  ]

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
  linked_ch = store.link_artifact(
      work_id,
      chain_key,
      role="channel",
      source="auto",
      link_reason="chat_topic",
      link_detail=f"Рабочий чат {detected_root[:8]} по этой задаче",
      now=cur_now,
  )
  if linked_ch is None:
    return False

  reviewed_cl_key = ""
  for akey, akind, aurl in primaries:
    if akind in ("gerrit", "github_pr", "piper") and not reviewed_cl_key:
      reviewed_cl_key = akey
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    existing_owner = store.find_work_by_artifact(akey, roles=("primary",))
    if existing_owner and existing_owner != work_id:
      s_idx = mention_steps.get(akey, 0)
      store.link_artifact(
          work_id,
          akey,
          role="context_only",
          source="auto",
          link_reason="chat_mention",
          link_detail=f"Попутно упомянут в чате {detected_root[:8]} (шаг {s_idx})",
          now=cur_now,
      )
    else:
      store.link_artifact(
          work_id,
          akey,
          role="primary",
          source="auto",
          link_reason="chat_topic",
          link_detail=f"Целевой артефакт чата {detected_root[:8]} (из заголовка или шага 0)",
          now=cur_now,
      )

  for comp_akey, comp_lbl in companion_cls.items():
    comp_num = comp_akey.split(":")[-1]
    parent_num = reviewed_cl_key.split(":")[-1] if reviewed_cl_key else ""
    comp_title = (
        f"{comp_num}: [companion WIP] {comp_lbl}"
        if comp_lbl
        else f"{comp_num}: [companion WIP]"
    )
    comp_state: dict[str, Any] = {
        "is_companion": True,
        "is_wip": True,
        "wip_label": "companion WIP",
    }
    if reviewed_cl_key:
      comp_state["companion_to"] = reviewed_cl_key
    store.upsert_artifact(
        comp_akey,
        kind="gerrit",
        title=comp_title,
        url=f"https://skia-review.googlesource.com/c/skia/+/{comp_num}",
        state=comp_state,
        now=cur_now,
    )
    s_idx = mention_steps.get(comp_akey, 0)
    detail_str = (
        f"Тестовый companion WIP СЛ к ревью {parent_num} (чат {detected_root[:8]}, шаг {s_idx})"
        if parent_num
        else f"Тестовый companion WIP СЛ (чат {detected_root[:8]}, шаг {s_idx})"
    )
    store.link_artifact(
        work_id,
        comp_akey,
        role="primary",
        source="auto",
        link_reason="companion_wip",
        link_detail=detail_str,
        now=cur_now,
    )

  for akey, akind, aurl in context_primaries:
    s_idx = mention_steps.get(akey, 0)
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    store.link_artifact(
        work_id,
        akey,
        role="context_only",
        source="auto",
        link_reason="chat_mention",
        link_detail=f"Попутно упомянут в чате {detected_root[:8]} (шаг {s_idx})",
        now=cur_now,
    )

  # Single human thread in write session attaches as channel when topical, else context_only
  if len(channels) == 1:
    akey, akind, aurl = channels[0]
    store.upsert_artifact(akey, kind=akind, title=akey, url=aurl, now=cur_now)
    prompt_low = f"{chat_title} {first_user_prompt}".lower()
    is_topical_channel = (
        akey in anchor_keys
        or not primaries
        or any(
            w in prompt_low
            for w in ("письм", "переписк", "почт", "email", "mail", "thread", "draft", "черновик")
        )
    )
    store.link_artifact(
        work_id,
        akey,
        role="channel" if is_topical_channel else "context_only",
        source="auto",
        link_reason="chat_topic" if is_topical_channel else "chat_mention",
        now=cur_now,
    )
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
      if linked_bug_keys:
        b_shorts = ", ".join(work_store.format_short_bug_id(b) for b in linked_bug_keys[:2])
        c_reason = "bug_trailer"
        c_detail = f"В описании СЛ {tid} указан баг {b_shorts}"
      elif stack_parent:
        c_reason = "stack_child"
        c_detail = f"Дочерний СЛ в стеке Gerrit поверх {stack_parent.split(':')[-1]}"
      else:
        c_reason = "cl_watcher"
        c_detail = f"Отслеживается в активных СЛ ({tid})"
      store.link_artifact(
          wid,
          ckey,
          role="primary",
          source="auto",
          link_reason=c_reason,
          link_detail=c_detail,
          now=cur_now,
      )
      for b_ckey in linked_bug_keys:
        store.link_artifact(
            wid,
            b_ckey,
            role="primary",
            source="auto",
            link_reason="bug_trailer",
            link_detail=f"Указан в описании СЛ {tid} ({work_store.format_short_bug_id(b_ckey)})",
            now=cur_now,
        )

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
      store.link_artifact(
          primary_wid,
          ch_key,
          role="channel",
          source="auto",
          link_reason="cl_watcher_chat",
          link_detail=f"Рабочий чат по СЛ {tid}",
          now=cur_now,
      )


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


def _decode_gmail_payload_text(
    payload: Mapping[str, Any] | None, strip_quotes: bool = True
) -> str:
  """Recursively decodes MIME text/html and text/plain parts while stripping quoted history."""
  if not isinstance(payload, Mapping):
    return ""
  chunks: list[str] = []

  def _walk(node: Mapping[str, Any]) -> None:
    mime = str(node.get("mimeType") or "").lower()
    parts = node.get("parts")
    body = node.get("body") if isinstance(node.get("body"), Mapping) else {}
    b64 = str(body.get("data") or "")
    if b64 and (mime in ("text/html", "text/plain", "") or not parts):
      try:
        txt = base64.urlsafe_b64decode(b64 + "===").decode("utf-8", errors="ignore")
      except Exception:
        txt = ""
      if txt:
        if strip_quotes:
          txt = re.split(
              r"<blockquote\b|<div\s+class=[\"']?gmail_quote",
              txt,
              maxsplit=1,
              flags=re.IGNORECASE,
          )[0]
          txt = re.split(r"\nOn\s+[^\n]{5,140}\s+wrote:\s*\r?\n", txt, maxsplit=1)[0]
          txt = "\n".join(
              ln for ln in txt.splitlines() if not ln.lstrip().startswith(">")
          )
        chunks.append(txt)
    if isinstance(parts, list):
      for p in parts:
        if isinstance(p, Mapping):
          _walk(p)

  _walk(payload)
  return "\n".join(chunks)


def _is_me_sender(addr: str) -> bool:
  low = str(addr or "").lower()
  if "jlavrova" in low or "julia lavrova" in low:
    return True
  disp = _clean_display_name(addr).lower()
  return disp in ("rusino", "jlavrova", "julia lavrova")


def _extract_recipient_info(
    headers_list: list[Mapping[str, str]],
) -> tuple[bool, int]:
  """Returns (me_addressed_in_to_or_cc, max_unique_recipient_count) across message headers."""
  me_addressed = False
  max_recipients = 0
  for hdrs in headers_list:
    raw_to = hdrs.get("to") or ""
    raw_cc = hdrs.get("cc") or ""
    parsed = getaddresses([raw_to, raw_cc])
    uniq_addrs: set[str] = set()
    for name, addr in parsed:
      norm_addr = addr.strip().lower()
      norm_name = name.strip().lower()
      if norm_addr:
        uniq_addrs.add(norm_addr)
      elif norm_name:
        uniq_addrs.add(norm_name)
      if _is_me_sender(f"{name} <{addr}>"):
        me_addressed = True
    if len(uniq_addrs) > max_recipients:
      max_recipients = len(uniq_addrs)
  return me_addressed, max_recipients


def _is_eligible_inbound_human_thread(prov: Mapping[str, Any]) -> bool:
  """Returns True when an un-replied inbound thread is small-group personal mail to me."""
  if prov.get("is_automated") or prov.get("is_drive_share"):
    return False
  if prov.get("in_trash_or_spam"):
    return False
  if not bool(prov.get("in_inbox", True)):
    return False
  if not bool(prov.get("has_personal_category", True)):
    return False
  if prov.get("has_non_personal_category"):
    return False
  if prov.get("is_bulk_or_list"):
    return False
  if not bool(prov.get("me_addressed")):
    return False
  rec_cnt = int(prov.get("recipient_count") or 0)
  return 1 <= rec_cnt <= MAX_HUMAN_GROUP_RECIPIENTS


def _fetch_gmail_thread_provenance(
    thread_id: str,
    thread_cache: dict[str, Any],
    now: float,
) -> dict[str, Any]:
  """Fetches first-message date, sender/recipient, and full-body text for a Gmail thread."""
  tid = str(thread_id or "").strip()
  if not tid:
    return {}
  cached = thread_cache.get(tid)
  if (
      isinstance(cached, Mapping)
      and int(cached.get("schema_ver") or 0) == 4
      and (now - float(cached.get("fetched_at") or 0.0) < 600)
  ):
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
    origin_ts = (
        float(int(m0.get("internalDate") or 0)) / 1000.0 if m0.get("internalDate") else 0.0
    )
    raw_subj = h0.get("subject") or ""
    root_subj = re.sub(r"^(?:re|fwd|fw)\s*:\s*", "", raw_subj, flags=re.IGNORECASE).strip()
    from0 = h0.get("from") or ""
    to0 = h0.get("to") or ""
    started_by_me = _is_me_sender(from0)
    origin_from = _clean_display_name(from0)
    is_gh_issue = "notifications@github.com" in from0.lower() and bool(
        re.search(r"\(Issue\s+#\d+\)", raw_subj, flags=re.IGNORECASE)
    )
    is_drive_share = bool(
        re.search(r"\(via\s+google\s+(?:docs|slides|sheets|drive)\)", from0, flags=re.IGNORECASE)
        or root_subj.lower().startswith(
            ("document shared with you:", "presentation shared", "spreadsheet shared")
        )
    )
    is_automated = (
        bool(re.match(r"^CL\s+\d{8,10}\s+\((?:XS|S|M|L|XL)\)$", root_subj))
        or bool(_RE_AUTOMATED_SENDER_ADDR.search(from0))
        or bool(_RE_CALENDAR_SUBJECT.match(raw_subj))
        or bool(_RE_MEETING_NOTES_SUBJECT.match(root_subj))
        or "committers@chromium.org" in to0.lower()
    )

    counterparty = ""
    counterparty_reply_ts = 0.0
    last_message_ts = origin_ts
    last_from_me = started_by_me
    sent_by_me = started_by_me
    non_bot_human_count = 0
    last_snippet = ""
    text_blocks: list[str] = [raw_subj] if raw_subj else []
    # Preserve INBOX/TRASH/CATEGORY_PERSONAL only from Gmail labelIds across messages
    has_labels_meta = False
    in_inbox = False
    all_trash_or_spam = True
    last_labels: set[str] = set()
    seen_personal_cat = False
    seen_non_personal_cat = False
    is_bulk_or_list = False
    inbound_hdrs: list[dict[str, str]] = []
    for m in msgs:
      if not isinstance(m, Mapping):
        continue
      hm = _extract_msg_headers(m)
      m_labels = set(m.get("labelIds") or [])
      if "labelIds" in m:
        has_labels_meta = True
        if "INBOX" in m_labels and not ({"TRASH", "SPAM"} & m_labels):
          in_inbox = True
        if not ({"TRASH", "SPAM"} & m_labels):
          all_trash_or_spam = False
      snip = str(m.get("snippet") or "")
      if snip:
        last_snippet = snip
      payload = m.get("payload") if isinstance(m.get("payload"), Mapping) else {}
      body_txt = _decode_gmail_payload_text(payload, strip_quotes=True)
      if body_txt:
        text_blocks.append(body_txt)
      elif snip:
        text_blocks.append(snip)
      f_hdr = hm.get("from") or ""
      t_hdr = hm.get("to") or ""
      mts = float(int(m.get("internalDate") or 0)) / 1000.0 if m.get("internalDate") else 0.0
      is_me = _is_me_sender(f_hdr)
      if is_me:
        sent_by_me = True
      else:
        inbound_hdrs.append(hm)
        if _RE_AUTOMATED_SENDER_ADDR.search(f_hdr):
          is_automated = True
        if "CATEGORY_PERSONAL" in m_labels:
          seen_personal_cat = True
        if _NON_PERSONAL_CATEGORIES & m_labels:
          seen_non_personal_cat = True
        # GitHub @mention notifications intentionally carry List-Id/Precedence: list headers
        if not is_gh_issue:
          prec = str(hm.get("precedence") or "").strip().lower()
          auto_sub = str(hm.get("auto-submitted") or "").strip().lower()
          if (
              hm.get("list-id")
              or hm.get("list-unsubscribe")
              or hm.get("x-google-group-id")
              or hm.get("x-mailing-list")
              or hm.get("feedback-id")
              or prec in ("bulk", "list", "junk")
              or auto_sub not in ("", "no")
          ):
            is_bulk_or_list = True
      if mts >= last_message_ts:
        last_message_ts = mts
        last_from_me = is_me
        last_labels = m_labels
      cand_sender = _clean_display_name(f_hdr)
      is_bot = (
          "[bot]" in cand_sender.lower()
          or bool(_RE_AUTOMATED_SENDER_ADDR.search(f_hdr))
      )
      if not is_bot and cand_sender:
        non_bot_human_count += 1
      if not is_me and f_hdr and not is_bot:
        if cand_sender:
          if not counterparty or is_gh_issue:
            counterparty = cand_sender
        if mts > counterparty_reply_ts:
          counterparty_reply_ts = mts
      elif is_me and t_hdr and not counterparty:
        for addr in t_hdr.split(","):
          if not _is_me_sender(addr) and "noreply" not in addr.lower():
            cand = _clean_display_name(addr)
            if cand:
              counterparty = cand
              break

    in_trash_or_spam = bool({"TRASH", "SPAM"} & last_labels) or (
        has_labels_meta and all_trash_or_spam
    )
    if not has_labels_meta:
      in_inbox = True
    has_personal_category = (not has_labels_meta) or (
        seen_personal_cat and not seen_non_personal_cat
    )
    has_non_personal_category = seen_non_personal_cat
    me_addressed, recipient_count = _extract_recipient_info(inbound_hdrs)

    prov = {
        "schema_ver": 4,
        "thread_id": tid,
        "origin_ts": origin_ts,
        "root_subject": root_subj,
        "origin_from": origin_from,
        "started_by_me": started_by_me,
        "sent_by_me": sent_by_me,
        "last_from_me": last_from_me,
        "last_message_ts": last_message_ts,
        "last_snippet": last_snippet[:160],
        "counterparty": counterparty,
        "counterparty_reply_ts": counterparty_reply_ts,
        "message_count": len(msgs),
        "non_bot_human_count": non_bot_human_count,
        "is_github_issue_thread": is_gh_issue,
        "is_automated": is_automated,
        "is_drive_share": is_drive_share,
        "in_inbox": in_inbox,
        "in_trash_or_spam": in_trash_or_spam,
        "has_personal_category": has_personal_category,
        "has_non_personal_category": has_non_personal_category,
        "is_bulk_or_list": is_bulk_or_list,
        "me_addressed": me_addressed,
        "recipient_count": recipient_count,
        "thread_text": "\n".join(text_blocks),
        "fetched_at": now,
    }
    thread_cache[tid] = prov
    return dict(prov)
  except Exception:
    return dict(cached) if isinstance(cached, Mapping) else {"thread_id": tid}


def _work_has_open_cl(store: work_store.WorkStore, work_id: str) -> bool:
  roles = store.get_work_artifact_roles(work_id)
  for akey, role in roles.items():
    if role != "primary":
      continue
    if akey.startswith(("gerrit:", "github_pr:", "piper:")):
      ar = store.get_artifact(akey) or {}
      st = ar.get("state") if isinstance(ar.get("state"), Mapping) else {}
      if str(st.get("status") or "NEW").upper() in ("NEW", "OPEN"):
        return True
  return False


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
  dismissed_keys = store.list_dismissed_tray_keys()
  completed_thread_ts = store.list_completed_thread_timestamps()

  # 1. Inspect current drafts via gmail readonly drafts --json
  active_draft_ids: set[str] = set()
  draft_thread_ids: set[str] = set()
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
          if thread_id:
            draft_thread_ids.add(thread_id)
          subj = str(
              hdr_map.get("subject")
              or d_full.get("subject")
              or (msg or {}).get("subject")
              or f"Draft {did[:8]}"
          )
          to_hdr = str(hdr_map.get("to") or d_full.get("to") or (msg or {}).get("to") or "")
          unquoted_body = _decode_gmail_payload_text(payload, strip_quotes=True)
          extracted = extract_artifacts_from_text(f"{subj}\n{unquoted_body}")
          inline_bug_titles = extract_bug_titles_from_html_or_text(unquoted_body)
          d_key = f"gmail_draft:{did}"
          to_name = _clean_display_name(to_hdr) if to_hdr else ""
          primaries = [
              (akey, akind, aurl)
              for akey, akind, aurl in extracted
              if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
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
            t_prov["unanswered_human"] = False
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
                url=f"https://mail.google.com/mail/u/0/#all/{thread_id}",
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
              art_title = ""
              art_state = None
            store.upsert_artifact(
                akey,
                kind=akind,
                title=art_title,
                url=aurl,
                state=art_state,
                now=cur_now,
            )
            store.link_artifact(
                wid,
                akey,
                role="primary",
                source="auto",
                link_reason="gmail_draft_ref",
                now=cur_now,
            )

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

  # Reconcile previously tracked drafts that disappeared -> mark SENT even when 0 remain
  prev_drafts = set(watcher_state.get("active_draft_ids") or [])
  for gone_did in prev_drafts - active_draft_ids:
    d_key = f"gmail_draft:{gone_did}"
    store.upsert_artifact(d_key, kind="gmail_draft", state={"status": "SENT"}, now=cur_now)
  watcher_state["active_draft_ids"] = sorted(active_draft_ids)

  # 2. Run 3 targeted Gmail search passes (--max <N>) and link via hybrid correspondence rules
  search_passes = (
      (
          "sent",
          "in:anywhere from:me newer_than:30d -from:noreply -to:committers@chromium.org",
          "40",
      ),
      (
          "inbound",
          (
              "in:inbox category:personal newer_than:21d"
              " -from:noreply -from:no-reply -from:donotreply -from:gerrit -from:github.com"
              " -from:buganizer -from:autoroll -from:luci"
              " -from:accounts@chromium.org -to:committers@chromium.org"
              " -from:forms-receipts-noreply@google.com"
              " -from:calendar-notification@google.com"
              " -from:gemini-notes@google.com -from:e-training@google.com"
          ),
          "30",
      ),
      (
          "gh_issue",
          (
              "newer_than:21d from:notifications@github.com"
              ' (cc:mention@noreply.github.com OR cc:comment@noreply.github.com OR "@Rusino")'
          ),
          "20",
      ),
  )

  seen_tids: set[str] = set()
  for pass_kind, query_str, max_str in search_passes:
    try:
      res = subprocess.run(
          [GMAIL_BIN, "readonly", "search", query_str, "--max", max_str, "--json"],
          capture_output=True,
          text=True,
          timeout=8,
          check=False,
      )
      if res.returncode != 0 or not res.stdout.strip():
        continue
      msgs = json.loads(res.stdout)
      if isinstance(msgs, dict):
        msgs = msgs.get("messages") or msgs.get("threads") or []
      if not isinstance(msgs, list):
        continue
      for m in msgs:
        if not isinstance(m, Mapping):
          continue
        tid = str(m.get("threadId") or m.get("id") or "").strip()
        if not tid or tid in seen_tids:
          continue
        seen_tids.add(tid)
        t_prov = _fetch_gmail_thread_provenance(tid, thread_cache, cur_now)
        cp = str(t_prov.get("counterparty") or "").strip()
        root_subj = str(t_prov.get("root_subject") or m.get("subject") or "").strip()
        if not root_subj or not cp:
          continue
        if any(tok in cp.lower() for tok in ("unsubscribe", "noreply", "organizers")):
          continue
        if pass_kind == "gh_issue":
          if not t_prov.get("is_github_issue_thread"):
            continue
          if int(t_prov.get("non_bot_human_count") or 0) < 1:
            continue
        elif t_prov.get("is_automated") and not t_prov.get("sent_by_me"):
          if store.get_artifact(f"gmail_thread:{tid}") is not None:
            store.upsert_artifact(
                f"gmail_thread:{tid}", kind="gmail_thread", state=t_prov, now=cur_now
            )
          continue

        t_key = f"gmail_thread:{tid}"
        t_url = f"https://mail.google.com/mail/u/0/#all/{tid}"
        t_title = f"{root_subj} ({cp})" if cp.lower() not in root_subj.lower() else root_subj
        full_text = str(t_prov.get("thread_text") or root_subj)
        t_arts = extract_artifacts_from_text(full_text)
        inline_bug_titles = extract_bug_titles_from_html_or_text(full_text)
        raw_primaries = [
            (akey, akind, aurl)
            for akey, akind, aurl in t_arts
            if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
        ]
        tech_primaries = [x for x in raw_primaries if x[1] != "gdoc"]
        is_drive_share = bool(
            t_prov.get("is_drive_share")
            or root_subj.lower().startswith(
                ("document shared with you:", "presentation shared", "spreadsheet shared")
            )
        )
        # Skip automated/Drive share notifications that reference zero technical artifacts
        if not tech_primaries and (t_prov.get("is_automated") or is_drive_share):
          if store.get_artifact(t_key) is not None:
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
          continue

        # Separate passing CL citations in the email body from focused primary targets
        subj_cl_keys = {
            k
            for k, kind, _ in extract_artifacts_from_text(root_subj)
            if kind in ("gerrit", "github_pr", "piper")
        }
        email_bug_keys = {
            k for k, kind, _ in raw_primaries if kind in ("buganizer", "github_issue")
        }
        cp_tokens = {tok for tok in re.split(r"\W+", cp.strip().lower()) if len(tok) >= 3}
        t_primaries: list[tuple[str, str, str]] = []
        context_cl_refs: list[tuple[str, str, str]] = []
        for akey, akind, aurl in raw_primaries:
          if akind in ("gerrit", "github_pr", "piper") and akey not in subj_cl_keys:
            w_owner = store.find_work_by_artifact(akey, roles=("primary",))
            w_roles = store.get_work_artifact_roles(w_owner) if w_owner else {}
            w_bugs = {
                k
                for k, r in w_roles.items()
                if r == "primary" and (k.startswith("buganizer:") or k.startswith("github_issue:"))
            }
            # Body CL belonging to another work when the email subject already names a different CL,
            # or when that work already has a primary email thread with a different counterparty
            w_has_other_cp_email = False
            for k_r, r_val in w_roles.items():
              if k_r.startswith("gmail_thread:") and k_r != t_key and r_val == "primary":
                other_st = (store.get_artifact(k_r) or {}).get("state") or {}
                if not other_st.get("is_github_issue_thread"):
                  other_cp = str(other_st.get("counterparty") or "").strip().lower()
                  other_toks = {tok for tok in re.split(r"\W+", other_cp) if len(tok) >= 3}
                  if other_toks and cp_tokens and not (other_toks & cp_tokens):
                    w_has_other_cp_email = True
                    break
            if (
                w_owner
                and not (w_bugs & email_bug_keys)
                and (
                    (subj_cl_keys and not (set(w_roles.keys()) & subj_cl_keys))
                    or w_has_other_cp_email
                )
            ):
              context_cl_refs.append((akey, akind, aurl))
              continue
            # Foreign CL in body whose author differs from counterparty and shares no bugs
            art_st = (store.get_artifact(akey) or {}).get("state") or {}
            if art_st.get("is_mine") is False and akind in ("gerrit", "github_pr"):
              cl_author = str(art_st.get("author") or "").strip().lower()
              author_tokens = {tok for tok in re.split(r"\W+", cl_author) if len(tok) >= 3}
              cl_bugs = set(art_st.get("linked_bug_keys") or []) | w_bugs
              if (
                  author_tokens
                  and cp_tokens
                  and not (author_tokens & cp_tokens)
                  and not (cl_bugs & email_bug_keys)
              ):
                context_cl_refs.append((akey, akind, aurl))
                continue
          t_primaries.append((akey, akind, aurl))

        is_recent_reply = (cur_now - float(t_prov.get("last_message_ts") or 0.0)) <= 14 * 86400
        t_prov["unanswered_human"] = bool(
            not t_prov.get("last_from_me", True)
            and not t_prov.get("is_automated")
            and tid not in draft_thread_ids
            and is_recent_reply
            and (t_prov.get("sent_by_me") or t_prov.get("is_github_issue_thread"))
        )

        matched_wids: list[str] = []
        existing_tid_wid = store.find_work_by_artifact(t_key, roles=("primary", "channel"))
        if existing_tid_wid:
          matched_wids.append(existing_tid_wid)
        for akey, _, _ in t_primaries:
          w_match = store.find_work_by_artifact(akey, roles=("primary",))
          if w_match and w_match not in matched_wids:
            matched_wids.append(w_match)

        # Skip dismissed, completed, or ineligible bulk/list/promotional inbound mail
        done_ts = completed_thread_ts.get(t_key)
        if (
            done_ts is not None
            and not existing_tid_wid
            and float(t_prov.get("last_message_ts") or 0.0) <= done_ts
        ):
          continue
        if t_key in dismissed_keys and not t_prov.get("sent_by_me"):
          if existing_tid_wid:
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
          continue
        if not t_prov.get("sent_by_me") and not t_prov.get("is_github_issue_thread"):
          ineligible_inbound = (
              (t_prov.get("is_automated") and (not matched_wids or t_prov.get("is_bulk_or_list")))
              or (
                  not t_prov.get("is_automated")
                  and not _is_eligible_inbound_human_thread(t_prov)
              )
          )
          if ineligible_inbound:
            if existing_tid_wid:
              store.upsert_artifact(
                  t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
              )
            continue

        bug_refs = [
            akey for akey, akind, _ in t_primaries if akind in ("buganizer", "github_issue")
        ]
        tracked_cl_refs = [
            akey
            for akey, akind, _ in t_primaries
            if akind in ("gerrit", "github_pr", "piper") and store.get_artifact(akey) is not None
        ]
        open_review_works = sum(1 for w_id in matched_wids if _work_has_open_cl(store, w_id))
        is_digest_hub = (
            len(bug_refs) > work_store.HUB_CORRESPONDENCE_TASK_THRESHOLD
            or (len(bug_refs) + len(tracked_cl_refs)) > 6
            or len(matched_wids) > work_store.HUB_CORRESPONDENCE_TASK_THRESHOLD
            or open_review_works >= 2
        )

        # Case 1: Zero-artifact human correspondence (or un-replied doc-only inbound mail)
        if not t_primaries or (
            not tech_primaries
            and not t_prov.get("sent_by_me")
            and not existing_tid_wid
            and not matched_wids
            and not t_prov.get("is_github_issue_thread")
        ):
          if existing_tid_wid or t_prov.get("sent_by_me"):
            target_wid = existing_tid_wid or store.create_work(root_subj, now=cur_now)
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
            store.link_artifact(
                target_wid,
                t_key,
                role="primary",
                source="auto",
                link_reason="gmail_root",
                link_detail=f"Корневая переписка с {cp}",
                now=cur_now,
            )
            for c_akey, c_akind, c_aurl in context_cl_refs:
              store.upsert_artifact(c_akey, kind=c_akind, title="", url=c_aurl, now=cur_now)
              store.link_artifact(
                  target_wid,
                  c_akey,
                  role="context_only",
                  source="auto",
                  link_reason="gmail_thread_ref",
                  link_detail=f"Упомянут в письме '{root_subj[:50]}'",
                  now=cur_now,
              )
          elif not t_prov.get("last_from_me", True):
            unanswered.append({
                "artifact_key": t_key,
                "thread_id": tid,
                "sender": cp,
                "subject": root_subj,
                "snippet": str(t_prov.get("last_snippet") or m.get("snippet") or "")[:140],
                "url": t_url,
                "last_message_ts": float(t_prov.get("last_message_ts") or 0.0),
                "bound_work_id": "",
            })
          continue

        # Case 2: Automated single-CL reviewlog mail -> attach as channel without promoting
        if t_prov.get("is_automated"):
          if matched_wids:
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
            store.link_artifact(
                matched_wids[0],
                t_key,
                role="channel",
                source="auto",
                link_reason="gmail_thread_ref",
                now=cur_now,
            )
          continue

        # Case 3: Multi-item digest / triage sweep (>3 tasks or >=2 open review Works)
        if is_digest_hub:
          store.upsert_artifact(
              t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
          )
          # Attach digest thread as context_only onto existing open Works without merging them
          for m_wid in matched_wids:
            if m_wid != existing_tid_wid:
              store.link_artifact(
                  m_wid,
                  t_key,
                  role="context_only",
                  source="auto",
                  link_reason="gmail_digest_ref",
                  link_detail=f"Упомянуто в сводном письме '{root_subj[:50]}' ({cp})",
                  now=cur_now,
              )
          if existing_tid_wid or (t_prov.get("sent_by_me") and len(t_primaries) <= 15):
            d_wid = existing_tid_wid or store.create_work(root_subj, now=cur_now)
            store.link_artifact(
                d_wid,
                t_key,
                role="primary",
                source="auto",
                link_reason="gmail_root",
                link_detail=f"Сводная переписка с {cp}",
                now=cur_now,
            )
            if not existing_tid_wid:
              for akey, akind, aurl in t_primaries[:12]:
                if akind in ("buganizer", "github_issue", "gdoc"):
                  b_desc = inline_bug_titles.get(akey, "")
                  short_b = work_store.format_short_bug_id(akey)
                  store.upsert_artifact(
                      akey,
                      kind=akind,
                      title=f"{short_b}: {b_desc}" if b_desc else "",
                      url=aurl,
                      state={"short_id": short_b, "bug_title": b_desc} if b_desc else None,
                      now=cur_now,
                  )
                  store.link_artifact(
                      d_wid,
                      akey,
                      role="primary",
                      source="auto",
                      link_reason="gmail_thread_ref",
                      now=cur_now,
                  )
          continue

        # Case 4: Focused correspondence (<=3 tasks and <2 open review Works) -> correspondence root
        store.upsert_artifact(
            t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
        )
        if not matched_wids:
          target_wid = store.create_work(root_subj, now=cur_now)
        else:
          target_wid = matched_wids[0]
          for extra_wid in matched_wids[1:]:
            store.merge_works(target_wid, extra_wid, source="auto", now=cur_now)

        w_roles_now = store.get_work_artifact_roles(target_wid)
        has_non_issue_email_root = any(
            k.startswith("gmail_thread:")
            and k != t_key
            and r == "primary"
            and not bool(
                ((store.get_artifact(k) or {}).get("state") or {}).get(
                    "is_github_issue_thread"
                )
            )
            for k, r in w_roles_now.items()
        )
        t_role = (
            "channel"
            if (t_prov.get("is_github_issue_thread") and has_non_issue_email_root)
            else "primary"
        )
        t_reason = (
            "github_issue_thread" if t_prov.get("is_github_issue_thread") else "gmail_root"
        )
        store.link_artifact(
            target_wid,
            t_key,
            role=t_role,
            source="auto",
            link_reason=t_reason,
            link_detail=f"Переписка с {cp}: {root_subj[:60]}",
            now=cur_now,
        )

        for akey, akind, aurl in t_primaries:
          existing_art = store.get_artifact(akey)
          if (
              akind in ("gerrit", "github_pr", "piper")
              and existing_art is None
              and akey not in subj_cl_keys
          ):
            short_cl = work_store.format_short_cl_id(akey, {})
            store.upsert_artifact(
                akey,
                kind=akind,
                title=short_cl,
                url=aurl,
                state={"status": "MERGED"},
                now=cur_now,
            )
            store.link_artifact(
                target_wid,
                akey,
                role="context_only",
                source="auto",
                link_reason="gmail_thread_ref",
                link_detail=f"Упомянут в письме '{root_subj[:50]}'",
                now=cur_now,
            )
            continue
          b_desc = inline_bug_titles.get(akey, "")
          if b_desc and akind in ("buganizer", "github_issue"):
            short_b = work_store.format_short_bug_id(akey)
            art_title = f"{short_b}: {b_desc}"
            art_state = {"short_id": short_b, "bug_title": b_desc}
          elif akind in ("gerrit", "github_pr", "piper") and existing_art is None:
            art_title = work_store.format_short_cl_id(akey, {})
            art_state = None
          else:
            art_title = ""
            art_state = None
          store.upsert_artifact(
              akey,
              kind=akind,
              title=art_title,
              url=aurl,
              state=art_state,
              now=cur_now,
          )
          store.link_artifact(
              target_wid,
              akey,
              role="primary",
              source="auto",
              link_reason="gmail_thread_ref",
              link_detail=f"Из переписки '{root_subj[:50]}'",
              now=cur_now,
          )
        for c_akey, c_akind, c_aurl in context_cl_refs:
          store.upsert_artifact(c_akey, kind=c_akind, title="", url=c_aurl, now=cur_now)
          store.link_artifact(
              target_wid,
              c_akey,
              role="context_only",
              source="auto",
              link_reason="gmail_thread_ref",
              link_detail=f"Упомянут в письме '{root_subj[:50]}'",
              now=cur_now,
          )
        store.reconcile_work_bug_cls(target_wid, now=cur_now)
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

  if int(w_state.get("unanswered_tray_schema") or 0) < 4:
    w_state["unanswered_tray"] = []
    w_state["unanswered_tray_schema"] = 4
  unanswered = w_state.get("unanswered_tray") or []
  if include_gmail and (cur_now - float(w_state.get("last_gmail_sync") or 0.0) >= 90):
    unanswered = ingest_gmail_drafts_and_threads(store, w_state, now=cur_now)
    w_state["unanswered_tray"] = unanswered
    w_state["unanswered_tray_schema"] = 4
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
