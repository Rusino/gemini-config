#!/usr/bin/env python3
"""Passive multi-source ingestor for Workbench (transcripts, cl_watcher_state, Gmail, roadmaps)."""

from __future__ import annotations

import base64
import copy
from collections.abc import Mapping
from datetime import datetime
from email.utils import formataddr, getaddresses
import fcntl
import glob
import hashlib
import html as html_lib
import json
import math
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
import urllib.parse

# Expose hooks/chat_lifecycle.py so Workbench launches visible chats with exact model/title pinning
HOOKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"
)
if HOOKS_DIR not in sys.path:
  sys.path.insert(0, HOOKS_DIR)

import chat_lifecycle as cl  # pylint: disable=g-import-not-at-top
import work_store  # pylint: disable=g-import-not-at-top

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
DEFAULT_PROJECTS_DIR = os.path.expanduser("~/.gemini/config/projects")
OPEN_CHAT_REQ_FILE = os.path.expanduser(
    "~/.gemini/jetski/brain/jetski-open-chat.json"
)
ANNOTATIONS_DIR = os.path.expanduser("~/.gemini/jetski/annotations")
GMAIL_BIN = "/google/bin/releases/gemini-agents-gmail/gmail"
MAX_OPEN_CHAT_TIMEOUT_SEC = 5.0
_RE_SAFE_CONV_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{3,127}$")
# Cap small-group human threads above typical colleague CC lists while cutting 39-person blasts
MAX_HUMAN_GROUP_RECIPIENTS = 8


def jetski_box_url() -> str:
  """Returns the Jetski Web base URL derived from this Cloudtop short hostname."""
  return f"https://jetski.corp.google.com/box/{socket.gethostname().split('.')[0]}"


def is_valid_conversation_id(conv_id: str) -> bool:
  """Returns True when conv_id is a path-safe UUID or conversation slug."""
  return bool(_RE_SAFE_CONV_ID.match(str(conv_id or "").strip()))

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

# Match Gerrit URLs plus human "Skia CL <N>" / "Chromium CL <N>" subject prefixes
RE_SKIA_GERRIT = re.compile(
    r"(?:skia-review\.googlesource\.com/c/skia/\+/|\bGerrit\s+"
    r"|\bSkia\s+(?:Gerrit\s+)?CL\s+#?|\b--change[=\s]+)(\d{6,8})\b",
    re.IGNORECASE,
)
RE_CHROMIUM_GERRIT = re.compile(
    r"(?:chromium-review\.googlesource\.com/c/chromium/src/\+/|\bcrrev(?:\.com)?/c/"
    r"|\bChromium\s+(?:Gerrit\s+)?CL\s+#?)(\d{6,8})\b",
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
    "add_comment_reply",
    "resolve_comment_thread",
    "send_message",
    "send_message_markdown",
})

# Match markdown links and MCP Drive/Docs JSON payloads so read-only specs get real doc titles
_RE_MD_GDOC_LINK = re.compile(
    r"\[([^\]\n]{3,160})\]\(\s*https?://docs\.google\.com/document/d/"
    r"([A-Za-z0-9_\-]{20,64})[^)]*\)",
    re.IGNORECASE,
)
_RE_JSON_GDOC_ID_TITLE = re.compile(
    r'"(?:id|fileId|documentId)"\s*:\s*"([A-Za-z0-9_\-]{20,64})"[^{}]{0,400}?'
    r'"(?:title|name)"\s*:\s*"([^"\\\n]{3,160})"',
)
_RE_JSON_GDOC_TITLE_ID = re.compile(
    r'"(?:title|name)"\s*:\s*"([^"\\\n]{3,160})"[^{}]{0,400}?'
    r'"(?:id|fileId|documentId)"\s*:\s*"([A-Za-z0-9_\-]{20,64})"',
)
_RE_GDOC_DISCO_URL = re.compile(
    r"docs\.google\.com/document/d/([A-Za-z0-9_\-]{20,64})/"
    r"[^\s)\"']*?[?#&]disco=([A-Za-z0-9._\-]+)",
    re.IGNORECASE,
)
_RE_GDOC_NOTIFY_TITLE = re.compile(
    r"docos/notify\?(?:[^)\s\"']*?[?&])?title=([^&\"'\s)]+)",
    re.IGNORECASE,
)
_RE_GDOC_FOLLOWING_DOC = re.compile(
    r"following\s+document\s*\r?\n\s*([^\r\n(]{3,180})",
    re.IGNORECASE,
)
_RE_GDOC_SHARE_SUBJECT = re.compile(
    r"^(?:document|presentation|spreadsheet)\s+shared\s+with\s+you:\s*[\"“']?(.+?)[\"”']?\s*$",
    re.IGNORECASE,
)


def _parse_mcp_call_args(args: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
  """Extracts (ToolName, decoded Arguments dict) from a call_mcp_tool invocation."""
  tool_name = str(args.get("ToolName") or "").strip()
  raw_inner = args.get("Arguments")
  if isinstance(raw_inner, Mapping):
    return tool_name, dict(raw_inner)
  if isinstance(raw_inner, str) and raw_inner.strip():
    try:
      parsed = json.loads(raw_inner)
      if isinstance(parsed, Mapping):
        return tool_name, dict(parsed)
    except Exception:
      pass
  return tool_name, {}


# GitHub PR notification emails embed /issues/<N> inside pull_request hovercard attributes
_RE_GH_HOVERCARD_DATA_ATTR = re.compile(
    r'data-(?:url|hydro-click)=(["\'])(?:(?!\1).)*?'
    r"github\.com/[A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+/issues/\d+(?:(?!\1).)*?\1",
    re.IGNORECASE,
)
_RE_GH_REPO_URL = re.compile(
    r"github\.com/([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)/(pull|issues)/(\d+)\b",
    re.IGNORECASE,
)
_RE_GH_REPO_NOTIF = re.compile(
    r"\[([A-Za-z0-9_.\-]+/[A-Za-z0-9_.\-]+)\][^\n]*?(\((PR|Issue)\s+#(\d+)\))",
    re.IGNORECASE,
)


def _mask_span(chars: list[str], start: int, end: int) -> None:
  """Replaces [start:end] in a mutable character buffer with spaces preserving offsets."""
  for idx in range(start, end):
    chars[idx] = " "


def extract_artifacts_from_text(text: str) -> list[tuple[str, str, str]]:
  """Returns list of (artifact_key, kind, default_url) in document appearance order."""
  found: dict[str, tuple[int, tuple[str, str, str]]] = {}
  if not text:
    return []

  # Mask GitHub PR hovercard data-url="/issues/<N>" attributes while keeping character offsets
  issue_scan_text = _RE_GH_HOVERCARD_DATA_ATTR.sub(
      lambda m: " " * len(m.group(0)), text
  )

  # Mask repo-scoped GitHub URLs and [owner/repo] (PR/Issue #N) tails before flutter defaults run
  bare_chars = list(text)
  bare_issue_chars = list(issue_scan_text)
  repo_url_matches = list(_RE_GH_REPO_URL.finditer(text))
  repo_issue_url_matches = list(_RE_GH_REPO_URL.finditer(issue_scan_text))
  repo_notif_matches = list(_RE_GH_REPO_NOTIF.finditer(text))
  for m in repo_url_matches:
    _mask_span(bare_chars, m.start(), m.end())
    _mask_span(bare_issue_chars, m.start(), m.end())
  for m in repo_notif_matches:
    _mask_span(bare_chars, m.start(2), m.end(2))
    _mask_span(bare_issue_chars, m.start(2), m.end(2))
  bare_text = "".join(bare_chars)
  bare_issue_text = "".join(bare_issue_chars)

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

  for m in repo_url_matches:
    repo_slug, ref_kind, num = m.group(1), m.group(2).lower(), m.group(3)
    if ref_kind == "pull" and num not in DUMMY_IDS:
      _add(
          m.start(),
          f"github_pr:{repo_slug}:{num}",
          "github_pr",
          f"https://github.com/{repo_slug}/pull/{num}",
      )

  for m in repo_notif_matches:
    repo_slug, ref_kind, num = m.group(1), m.group(3).lower(), m.group(4)
    if ref_kind == "pr" and num not in DUMMY_IDS:
      _add(
          m.start(),
          f"github_pr:{repo_slug}:{num}",
          "github_pr",
          f"https://github.com/{repo_slug}/pull/{num}",
      )

  for m in RE_GITHUB_PR.finditer(bare_text):
    num = m.group(1)
    if num not in DUMMY_IDS:
      _add(
          m.start(),
          f"github_pr:flutter/flutter:{num}",
          "github_pr",
          f"https://github.com/flutter/flutter/pull/{num}",
      )

  # GitHub PRs and issues share one counter per repo; suppress PR numbers in (owner/repo#N)
  for m in repo_issue_url_matches:
    repo_slug, ref_kind, num = m.group(1), m.group(2).lower(), m.group(3)
    if (
        ref_kind == "issues"
        and num not in DUMMY_IDS
        and f"github_pr:{repo_slug}:{num}" not in found
    ):
      _add(
          m.start(),
          f"github_issue:{repo_slug}:{num}",
          "github_issue",
          f"https://github.com/{repo_slug}/issues/{num}",
      )

  for m in repo_notif_matches:
    repo_slug, ref_kind, num = m.group(1), m.group(3).lower(), m.group(4)
    if (
        ref_kind == "issue"
        and num not in DUMMY_IDS
        and f"github_pr:{repo_slug}:{num}" not in found
    ):
      _add(
          m.start(),
          f"github_issue:{repo_slug}:{num}",
          "github_issue",
          f"https://github.com/{repo_slug}/issues/{num}",
      )

  for m in RE_FLUTTER_ISSUE.finditer(bare_issue_text):
    num = m.group(1)
    if num not in DUMMY_IDS and f"github_pr:flutter/flutter:{num}" not in found:
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


def extract_draft_mentioned_keys(subject: str, body: str) -> list[str]:
  """Extracts canonical CL/PR/bug/doc artifact keys referenced in a reply draft."""
  combined = f"{subject}\n{body}"
  allowed = ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
  raw_keys = [
      akey for akey, akind, _ in extract_artifacts_from_text(combined) if akind in allowed
  ] + work_store._extract_draft_mentioned_keys(combined)
  # Suppress duplicate flutter issue keys when the same number matched as a GitHub PR
  pr_nums = {k.split(":")[-1] for k in raw_keys if k.startswith("github_pr:flutter/flutter:")}
  ordered: list[str] = []
  seen: set[str] = set()
  for key in raw_keys:
    if key.startswith("github_issue:flutter/flutter:") and key.split(":")[-1] in pr_nums:
      continue
    ck = work_store.canonicalize_artifact_key(key)
    if ck not in seen:
      seen.add(ck)
      ordered.append(ck)
  return ordered


# Anchor inline bug titles at bullet/line start so prose citations never stamp bug_titles
_RE_INLINE_FLUTTER_TITLE = re.compile(
    r"^(?:[-*•▪▫◦]|\d+[.)]|\[[ xX]\])?\s*"
    r"(?P<pr_prefix>\bPR\s*|\bpull/)?(?:#|flutter#)(\d{4,7})\b\s*[—–\-:]\s*([^\n(]+)",
    re.IGNORECASE,
)
_RE_GH_NOTIF_ISSUE_TITLE = re.compile(
    r"\[flutter/flutter\]\s+(.+?)\s+\(Issue\s+#(\d{4,7})\)",
    re.IGNORECASE,
)
_RE_INLINE_BUGANIZER_TITLE = re.compile(
    r"^(?:[-*•▪▫◦]|\d+[.)]|\[[ xX]\])?\s*"
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
    m = _RE_INLINE_FLUTTER_TITLE.match(clean_line)
    if m and not m.group("pr_prefix"):
      num = m.group(2)
      desc = m.group(3).strip(" —–-:;,.()")
      if num not in DUMMY_IDS and len(desc) >= 6:
        ck = work_store.canonicalize_artifact_key("flutter_issue", num)
        titles[ck] = desc
    m_b = _RE_INLINE_BUGANIZER_TITLE.match(clean_line)
    if m_b:
      num = m_b.group(1)
      desc = m_b.group(2).strip(" —–-:;,.()")
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
      if tname == "call_mcp_tool":
        mcp_tool, _ = _parse_mcp_call_args(args)
        if mcp_tool in WRITE_TOOL_NAMES:
          return True
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
          is_mcp_write = tname in WRITE_TOOL_NAMES
          if tname == "call_mcp_tool":
            mcp_tool, mcp_args = _parse_mcp_call_args(args)
            if mcp_tool in WRITE_TOOL_NAMES:
              is_mcp_write = True
            fid = str(mcp_args.get("fileId") or mcp_args.get("documentId") or "").strip()
            if re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", fid):
              blob = f"https://docs.google.com/document/d/{fid}/edit {blob}"
          if is_mcp_write:
            anchor_texts.append(blob)
          else:
            aux_texts.append(blob)
  return anchor_texts, aux_texts


def _collect_gdoc_metadata_from_step(
    step: Mapping[str, Any],
    gdoc_titles: dict[str, str],
    gdoc_comment_ops: list[tuple[str, str, str, float]],
    fallback_ts: float,
) -> None:
  """Extracts GDoc human titles and MCP comment reply/resolve actions from a step."""
  def _record_title(doc_id: str, raw_title: str) -> None:
    did = str(doc_id or "").strip()
    t_clean = work_store.clean_work_title(str(raw_title or "").strip())
    if (
        re.fullmatch(r"[A-Za-z0-9_\-]{20,64}", did)
        and len(t_clean) >= 3
        and "http://" not in t_clean.lower()
        and "https://" not in t_clean.lower()
        and not work_store.is_placeholder_title(t_clean, f"gdoc:{did}")
    ):
      gdoc_titles[did] = t_clean

  content = str(step.get("content") or "")
  if content:
    for m_md in _RE_MD_GDOC_LINK.finditer(content):
      _record_title(m_md.group(2), m_md.group(1))
    stripped = content.strip()
    parsed_json_ok = False
    if stripped.startswith(("{", "[")):
      try:
        parsed = json.loads(stripped)
        parsed_json_ok = True
        objs: list[Mapping[str, Any]] = []
        if isinstance(parsed, Mapping):
          objs.append(parsed)
          if isinstance(parsed.get("files"), list):
            objs.extend(x for x in parsed["files"] if isinstance(x, Mapping))
        elif isinstance(parsed, list):
          objs.extend(x for x in parsed if isinstance(x, Mapping))
        for obj in objs:
          fid = str(obj.get("id") or obj.get("fileId") or obj.get("documentId") or "")
          ftitle = str(obj.get("title") or obj.get("name") or "")
          if fid and ftitle:
            _record_title(fid, ftitle)
      except Exception:
        pass
    if not parsed_json_ok:
      for m_jt in _RE_JSON_GDOC_ID_TITLE.finditer(content):
        _record_title(m_jt.group(1), m_jt.group(2))
      for m_tj in _RE_JSON_GDOC_TITLE_ID.finditer(content):
        _record_title(m_tj.group(2), m_tj.group(1))

  step_ts = _parse_iso_ts(str(step.get("created_at") or "")) or fallback_ts
  for tc in step.get("tool_calls") or []:
    if not isinstance(tc, Mapping):
      continue
    if str(tc.get("name") or "") != "call_mcp_tool":
      continue
    args = tc.get("args") or tc.get("arguments") or {}
    if not isinstance(args, Mapping):
      continue
    mcp_tool, mcp_args = _parse_mcp_call_args(args)
    if mcp_tool in ("add_comment_reply", "resolve_comment_thread"):
      fid = str(mcp_args.get("fileId") or mcp_args.get("documentId") or "").strip()
      cid_c = str(mcp_args.get("commentId") or "").strip()
      if fid and cid_c:
        gdoc_comment_ops.append((mcp_tool, fid, cid_c, step_ts))


def _apply_transcript_gdoc_comment_ops(
    store: work_store.WorkStore,
    gdoc_comment_ops: list[tuple[str, str, str, float]],
    gdoc_titles: Mapping[str, str],
    fallback_work_id: str,
    cur_now: float,
) -> set[str]:
  """Applies MCP comment reply/resolve ops onto #disco threads owned by each doc's Work."""
  touched_wids: set[str] = set()
  for op_name, fid, cid_c, op_ts in gdoc_comment_ops:
    cand_gt = f"gdoc_thread:{fid}:{cid_c}"
    # Drive API commentId omits the "doco." prefix emitted in Gmail ?disco= notification links
    if store.get_artifact(cand_gt) is None and not cid_c.startswith("doco."):
      alt_gt = f"gdoc_thread:{fid}:doco.{cid_c}"
      if store.get_artifact(alt_gt) is not None:
        cand_gt = alt_gt
    disco_part = cand_gt.split(":", 2)[2]
    gt_art = store.get_artifact(cand_gt) or {}
    gt_prev_st = gt_art.get("state") if isinstance(gt_art.get("state"), Mapping) else {}
    prev_msg_ts = float(gt_prev_st.get("last_message_ts") or 0.0)
    doc_key = f"gdoc:{fid}"
    doc_t = str(
        gt_prev_st.get("doc_title")
        or gdoc_titles.get(fid)
        or (store.get_artifact(doc_key) or {}).get("title")
        or cand_gt
    )
    gt_url = f"https://docs.google.com/document/d/{fid}/edit?disco={disco_part}"
    # Interim MCP reply moves obligation to Waiting; explicit MCP resolve closes the thread
    if op_name == "add_comment_reply":
      op_state: dict[str, Any] = {
          "doc_id": fid,
          "disco_id": disco_part,
          "doc_title": doc_t,
          "sent_by_me": True,
          "last_from_me": True,
          "mcp_reply_ts": op_ts,
          "last_message_ts": max(prev_msg_ts, op_ts),
          "is_active_correspondence": True,
          "unanswered_human": False,
      }
    else:
      op_state = {
          "doc_id": fid,
          "disco_id": disco_part,
          "doc_title": doc_t,
          "resolved": True,
          "resolved_by_me": True,
          "resolved_at": op_ts,
          "unanswered_human": False,
      }
    store.upsert_artifact(
        cand_gt,
        kind="gdoc_thread",
        title=str(gt_art.get("title") or doc_t),
        url=gt_url,
        state=op_state,
        now=cur_now,
    )
    # Attach to the doc's existing Work before falling back to a multi-CL chat Work
    target_wid = (
        store.find_work_by_artifact(cand_gt, roles=("primary", "channel"))
        or store.find_work_by_artifact(doc_key, roles=("primary", "channel"))
        or fallback_work_id
    )
    if not target_wid:
      target_wid = store.create_work(doc_t, now=cur_now)
      store.upsert_artifact(
          doc_key,
          kind="gdoc",
          title=doc_t,
          url=f"https://docs.google.com/document/d/{fid}/edit",
          now=cur_now,
      )
      store.link_artifact(
          target_wid,
          doc_key,
          role="primary",
          source="auto",
          link_reason="gdoc_comment",
          now=cur_now,
      )
    store.link_artifact(
        target_wid,
        cand_gt,
        role="channel",
        source="auto",
        link_reason="gdoc_comment",
        now=cur_now,
    )
    touched_wids.add(target_wid)
  for tw in touched_wids:
    store.reconcile_correspondence_obligations(tw, now=cur_now)
  return touched_wids


def _upsert_and_link_chat_chain(
    store: work_store.WorkStore,
    work_id: str,
    conv_id: str,
    detected_root: str,
    chat_title: str,
    first_user_prompt: str,
    chain_origin_ts: float,
    cur_now: float,
    *,
    source: str = "auto",
    link_reason: str = "chat_topic",
    link_detail: str = "",
) -> str | None:
  """Upserts a chat_chain artifact in store and links it as a channel on work_id."""
  chain_key = f"chat_chain:{detected_root}"
  existing_chain = store.get_artifact(chain_key) or {}
  prev_chain_title = work_store.clean_work_title(str(existing_chain.get("title") or ""))
  # Discard generic ID or baton placeholders so real user prompts can upgrade the chip label
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
  return store.link_artifact(
      work_id,
      chain_key,
      role="channel",
      source=source,
      link_reason=link_reason,
      link_detail=link_detail or f"Рабочий чат {detected_root[:8]} по этой задаче",
      now=cur_now,
  )


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
  # Track (label, kind, url) for companion probes created during review sessions
  companion_cls: dict[str, tuple[str, str, str]] = {}
  mention_steps: dict[str, int] = {}
  gdoc_titles: dict[str, str] = {}
  gdoc_comment_ops: list[tuple[str, str, str, float]] = []
  if chat_title:
    for akey, akind, aurl in extract_artifacts_from_text(chat_title):
      extracted[akey] = (akey, akind, aurl)
      anchor_keys.add(akey)

  first_user_prompt = ""
  first_user_step_seen = False
  first_step_ts = 0.0
  detected_root = root_cid or conv_id
  # Match plain and Markdown-linked companion probes across Skia/Chromium Gerrit and GitHub PRs
  re_comp = re.compile(
      r"(?:companion\s+(?:WIP|Draft)(?:\s+(?:CL|PR))?|WIP[-\s]компаньон|компаньон[-\s](?:ПР|СЛ))"
      r"(?P<gap>[^0-9\[\n]{0,80}?)"
      r"(?:\[[^\]\n]{1,80}\]\(\s*)?"
      r"(?:"
      r"(?:https?://)?skia-review\.googlesource\.com/c/skia/\+/(?P<skia_u>\d{6,8})"
      r"|\b(?:Skia\s+CL|Gerrit)\s+(?P<skia_t>\d{6,8})"
      r"|(?:https?://)?chromium-review\.googlesource\.com/c/chromium/src/\+/(?P<chrom_u>\d{6,8})"
      r"|\b(?:Chromium\s+CL|crrev(?:\.com)?/c/)\s*(?P<chrom_t>\d{6,8})"
      r"|(?:https?://)?github\.com/flutter/flutter/pull/(?P<gh_u>\d{4,7})"
      r"|\bPR\s+#?(?P<gh_t>\d{4,7})"
      r")"
      r"(?:/\d+)?\)?"
      r"(?:\s*\((?P<lbl>[^)\n]{3,80})\))?",
      re.IGNORECASE,
  )
  # Reject prepositions like "companion PR for PR #195366" where the number names the parent PR
  re_parent_prep = re.compile(
      r"\b(?:for|on|to|of|against|к|для|поверх|базе)\b",
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
            if re_parent_prep.search(m_c.group("gap") or ""):
              continue
            c_lbl = (m_c.group("lbl") or "").strip()
            skia_m = m_c.group("skia_u") or m_c.group("skia_t")
            chrom_m = m_c.group("chrom_u") or m_c.group("chrom_t")
            if skia_m:
              c_num = skia_m
              c_akey = f"gerrit:skia:{c_num}"
              c_kind = "gerrit"
              c_url = f"https://skia-review.googlesource.com/c/skia/+/{c_num}"
            elif chrom_m:
              c_num = chrom_m
              c_akey = f"gerrit:chromium:{c_num}"
              c_kind = "gerrit"
              c_url = f"https://chromium-review.googlesource.com/c/chromium/src/+/{c_num}"
            else:
              c_num = m_c.group("gh_u") or m_c.group("gh_t")
              c_akey = f"github_pr:flutter/flutter:{c_num}"
              c_kind = "github_pr"
              c_url = f"https://github.com/flutter/flutter/pull/{c_num}"
            # Never reclassify the reviewed parent CL/PR anchored in step 0 or title as companion
            if c_akey in anchor_keys:
              continue
            companion_cls[c_akey] = (c_lbl, c_kind, c_url)
            extracted[c_akey] = (c_akey, c_kind, c_url)
            mention_steps.setdefault(c_akey, step_idx)
        if _is_write_step(step):
          has_write = True
        _collect_gdoc_metadata_from_step(step, gdoc_titles, gdoc_comment_ops, cur_now)
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

  # Upgrade human titles on stored Google Docs even when the transcript itself is read-only
  for doc_id, doc_title in gdoc_titles.items():
    doc_key = f"gdoc:{doc_id}"
    if store.get_artifact(doc_key) is not None:
      store.upsert_artifact(
          doc_key,
          kind="gdoc",
          title=doc_title,
          url=f"https://docs.google.com/document/d/{doc_id}/edit",
          state={"doc_title": doc_title},
          now=cur_now,
      )

  # Check stored artifact metadata so companion and hub docs/CLs never anchor card ownership
  def _is_stored_hub(akey: str) -> bool:
    st_meta = (store.get_artifact(akey) or {}).get("state")
    return work_store.is_hub_artifact(akey, st_meta if isinstance(st_meta, Mapping) else None)

  if not has_write:
    # Read-only session: never mint a Work card; store artifacts in 7-day candidate buffer
    for akey, akind, aurl in extracted.values():
      if akind == "gmail_draft":
        continue
      cand_title = (
          gdoc_titles.get(akey.split(":", 1)[-1], akey) if akind == "gdoc" else akey
      )
      store.record_candidate(
          akey,
          kind=akind,
          title=cand_title,
          url=aurl,
          chat_chain_id=detected_root,
          now=cur_now,
      )
    # Attach read-only discussion chats only when every anchored doc/CL resolves to one Work
    ro_wid = store.find_work_by_artifact(chain_key)
    if not ro_wid:
      anchored_gdocs = [
          akey for akey, akind, _ in extracted.values() if akind == "gdoc" and akey in anchor_keys
      ]
      anchored_code = [
          akey
          for akey, akind, _ in extracted.values()
          if akind in CODE_PRIMARY_KINDS and akey in anchor_keys
      ]
      if anchored_gdocs and all(not _is_stored_hub(k) for k in anchored_gdocs):
        doc_wids = [
            store.find_all_works_by_artifact(k, roles=("primary", "channel"))
            for k in anchored_gdocs
        ]
        if all(len(ws) == 1 and ws[0] == doc_wids[0][0] for ws in doc_wids):
          cand_wid = doc_wids[0][0]
          if all(
              store.find_all_works_by_artifact(ck, roles=("primary", "channel")) == [cand_wid]
              for ck in anchored_code
          ):
            ro_wid = cand_wid
    if ro_wid:
      _upsert_and_link_chat_chain(
          store,
          ro_wid,
          conv_id,
          detected_root,
          chat_title,
          first_user_prompt,
          chain_origin_ts,
          cur_now,
      )
    return True

  # Write session: bound -> attach-first -> mint
  all_primaries = [
      (akey, akind, aurl)
      for akey, akind, aurl in extracted.values()
      if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
  ]
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
      # Still apply MCP comment replies/resolves when an investigation chat inspected >4 CLs
      if gdoc_comment_ops:
        _apply_transcript_gdoc_comment_ops(
            store, gdoc_comment_ops, gdoc_titles, "", cur_now
        )
        return True
      return False
    primary_title = ""
    if primaries:
      existing_art = store.get_artifact(primaries[0][0])
      primary_title = str((existing_art or {}).get("title") or primaries[0][0])
    # Prefer the real parent CL/PR subject when the chat was titled after creating a companion
    non_ph_primary_title = (
        primary_title
        if (primaries and not work_store.is_placeholder_title(primary_title, primaries[0][0]))
        else ""
    )
    preferred_chat_title = (
        "" if (work_store.is_companion_title(chat_title) and non_ph_primary_title) else chat_title
    )
    default_title = work_store.clean_work_title(
        preferred_chat_title
        or non_ph_primary_title
        or chat_title
        or primary_title
        or first_user_prompt
        or f"Work {conv_id[:8]}"
    )
    work_id = store.create_work(default_title, project_id=project_id, now=cur_now)

  linked_ch = _upsert_and_link_chat_chain(
      store,
      work_id,
      conv_id,
      detected_root,
      chat_title,
      first_user_prompt,
      chain_origin_ts,
      cur_now,
  )
  if linked_ch is None:
    return False

  reviewed_cl_key = ""
  for akey, akind, aurl in primaries:
    if akind in ("gerrit", "github_pr", "piper") and not reviewed_cl_key:
      reviewed_cl_key = akey
    prim_title = (
        gdoc_titles.get(akey.split(":", 1)[-1], akey) if akind == "gdoc" else akey
    )
    prim_state = (
        {"doc_title": prim_title} if (akind == "gdoc" and prim_title != akey) else None
    )
    store.upsert_artifact(
        akey, kind=akind, title=prim_title, url=aurl, state=prim_state, now=cur_now
    )
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

  # Persist companion probes with tracker-specific draft/WIP state nested under the reviewed parent
  for comp_akey, (comp_lbl, comp_kind, comp_url) in companion_cls.items():
    comp_num = comp_akey.split(":")[-1]
    parent_num = reviewed_cl_key.split(":")[-1] if reviewed_cl_key else ""
    comp_tag = "companion Draft" if comp_kind == "github_pr" else "companion WIP"
    comp_title = (
        f"{comp_num}: [{comp_tag}] {comp_lbl}"
        if comp_lbl
        else f"{comp_num}: [{comp_tag}]"
    )
    comp_state: dict[str, Any] = {
        "is_companion": True,
        "is_wip": True,
        "wip_label": comp_tag,
    }
    # GitHub companions are uploaded as Draft PRs rather than Gerrit WIP patchsets
    if comp_kind == "github_pr":
      comp_state["is_draft"] = True
    if reviewed_cl_key:
      comp_state["companion_to"] = reviewed_cl_key
    store.upsert_artifact(
        comp_akey,
        kind=comp_kind,
        title=comp_title,
        url=comp_url,
        state=comp_state,
        now=cur_now,
    )
    s_idx = mention_steps.get(comp_akey, 0)
    detail_str = (
        f"Тестовый {comp_tag} к ревью {parent_num} (чат {detected_root[:8]}, шаг {s_idx})"
        if parent_num
        else f"Тестовый {comp_tag} (чат {detected_root[:8]}, шаг {s_idx})"
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

  touched_wids = _apply_transcript_gdoc_comment_ops(
      store, gdoc_comment_ops, gdoc_titles, work_id, cur_now
  )
  if work_id not in touched_wids:
    store.reconcile_correspondence_obligations(work_id, now=cur_now)
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

  # Upsert all known bugs first with proper GitHub vs Buganizer canonicalization
  synced_bug_keys: set[str] = set()
  if isinstance(bugs, Mapping):
    for bkey, bval in bugs.items():
      if not isinstance(bval, Mapping):
        continue
      ckey, bkind, btitle, burl = work_store.canonicalize_watcher_bug(str(bkey), bval)
      if not ckey:
        continue
      bstate = dict(bval)
      bdesc = str(bval.get("title") or bval.get("summary") or "").strip()
      # Seed bug_title so GitHub PR/Issue collision dedup preserves real issue rows
      if bdesc and not bstate.get("bug_title"):
        bstate["bug_title"] = bdesc
      store.upsert_artifact(
          ckey,
          kind=bkind,
          title=btitle,
          url=burl,
          state=bstate,
          now=cur_now,
      )
      synced_bug_keys.add(ckey)

  # Defer companion attachment until all parent CL/PR Works in this batch are created or matched
  deferred_companions: list[tuple[str, str]] = []
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
      b_ck, _, _, lb_url = work_store.canonicalize_watcher_bug("", lb)
      if not b_ck:
        continue
      lb_meta[b_ck] = {
          "title": str(lb.get("title") or "").strip(),
          "status": str(lb.get("status") or "").strip(),
          "url": str(lb.get("url") or lb_url or "").strip(),
      }

    for b_ckey in linked_bug_keys:
      meta = lb_meta.get(b_ckey) or {}
      short_b = work_store.format_short_bug_id(b_ckey)
      b_desc = meta.get("title") or ""
      b_title = f"{short_b}: {b_desc}" if b_desc else short_b
      b_url = meta.get("url") or work_store._default_artifact_url(b_ckey)
      b_state: dict[str, Any] = {"short_id": short_b}
      if b_desc:
        b_state["bug_title"] = b_desc
      # Keep live tracker status from cl_state['bugs'] authoritative over stale CL-linked snapshots
      if meta.get("status") and b_ckey not in synced_bug_keys:
        b_state["status"] = meta["status"]
      store.upsert_artifact(
          b_ckey,
          kind=work_store.artifact_kind_from_key(b_ckey),
          title=b_title,
          url=b_url,
          state=b_state,
          now=cur_now,
      )

    # Preserve raw project metadata for chat_chain filtering before sanitizing product CL state
    raw_item_proj = str(
        item.get("project_id") or item.get("project_name") or item.get("project") or ""
    )
    item_state = dict(item)
    # Strip config project stamps from product CL/PR state when discussed in a config chat
    if work_store.is_config_project(str(item_state.get("project_id") or "")):
      item_state["project_id"] = ""
    if work_store.is_config_project(str(item_state.get("project_name") or "")):
      item_state["project_name"] = ""
    if work_store.is_config_project(str(item_state.get("project") or "")):
      item_state["project"] = ""
    item_state["linked_bug_keys"] = linked_bug_keys
    created_ts = float(item.get("created_ts") or 0.0)
    if created_ts > 0:
      item_state["origin_ts"] = created_ts
    # Canonicalize companion_to and skip standalone Work creation for companion probes
    is_comp = work_store.is_companion_artifact(ckey, item_state, subj)
    if is_comp:
      parent_ckey = work_store.canonicalize_companion_parent(ckey, item_state, subj)
      item_state["is_companion"] = True
      if parent_ckey:
        item_state["companion_to"] = parent_ckey
        deferred_companions.append((ckey, parent_ckey))
    store.upsert_artifact(
        ckey,
        kind=work_store.artifact_kind_from_key(ckey),
        title=f"{tid}: {subj}",
        url=url,
        state=item_state,
        now=cur_now,
    )
    # Companion probes and cherry-picks never mint standalone Works of their own
    if is_comp or item.get("is_cherry_pick"):
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

    # Never propagate a config project ID onto a newly created product Work
    raw_proj_field = str(item.get("project") or "")
    clean_proj = "" if work_store.is_config_project(raw_proj_field) else raw_proj_field
    if not matched_wids:
      matched_wids = [
          store.create_work(
              subj,
              project_id=clean_proj,
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
      # Apply item-level project_id only to item.conversation_id so sibling product chats survive
      cid_proj = raw_item_proj if cid == single_cid else ""
      ch_title = (
          str(item.get("chat_title") or f"Chat {cid[:8]}")
          if cid == single_cid
          else f"Chat {cid[:8]}"
      )
      if work_store.is_config_artifact(
          ch_key,
          title=ch_title,
          state={"cascade_id": cid},
          project_id=cid_proj,
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

  # Nest active companions inside any persisted Work that tracks their parent CL/PR
  for comp_ckey, parent_ckey in deferred_companions:
    parent_num = parent_ckey.split(":")[-1]
    for p_wid in store.find_all_works_by_artifact(parent_ckey, roles=("primary",)):
      store.link_artifact(
          p_wid,
          comp_ckey,
          role="primary",
          source="auto",
          link_reason="companion_wip",
          link_detail=f"Тестовый companion к {parent_num}",
          now=cur_now,
      )


def _clean_display_name(raw_addr: str) -> str:
  part = str(raw_addr or "").split(",")[0].strip()
  name = part.split("<")[0].strip().strip('"').strip("'")
  # Strip Google Docs/Drive sender wrappers so human colleague names match across channels
  name = re.sub(
      r"\s*\((?:via\s+)?google\s+(?:docs|slides|sheets|drive)\)\s*$",
      "",
      name,
      flags=re.IGNORECASE,
  ).strip()
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


_GH_BOT_LOGINS = frozenset({
    "github-actions",
    "flutter-dashboard",
    "gemini-code-assist",
    "dependabot",
    "fluttergithubbot",
    "skia-autoroll",
    "engine-flutter-autoroll",
    "skia-flutter-autoroll",
    "cocoon",
    "codecov",
    "cla-bot",
    "googlebot",
})

_RE_GH_LOCK_MSG = re.compile(
    r"(?:thread\s+has\s+been\s+(?:automatically\s+)?locked"
    r"|conversation\s+has\s+been\s+locked"
    r"|\blocked\s+(?:as\s+\w+\s+)?and\s+limited\s+conversation\b)",
    re.IGNORECASE,
)
_RE_GH_CLOSED_LINE = re.compile(
    r"(?:(?:^|\n)\s*(?:closed|merged)\s+#(\d+)\b"
    r"|\b(?:closed|merged)\s+#(\d+)\s+(?:as\s+(?:completed|not\s+planned|duplicate)|via\s+|into\s+))",
    re.IGNORECASE,
)
_RE_GH_REOPENED_LINE = re.compile(
    r"(?:^|\n)\s*reopened\s+#(\d+)\b",
    re.IGNORECASE,
)


def _is_github_bot_login(login: str) -> bool:
  """Returns True when a GitHub login or notification sender belongs to automation."""
  low = str(login or "").strip().lower()
  if not low:
    return False
  return (
      low in _GH_BOT_LOGINS
      or low.endswith(("[bot]", "-bot", "_bot"))
      or "autoroll" in low
  )


def _is_self_github_actor(node: Mapping[str, Any]) -> bool:
  """Returns True when a GraphQL comment or review node was authored by Julia (Rusino)."""
  if bool(node.get("viewerDidAuthor")):
    return True
  author = node.get("author")
  login = str(author.get("login") or "") if isinstance(author, Mapping) else ""
  return login.strip().lower() == "rusino"


def _update_gh_notif_closure_state(
    body_txt: str,
    snip: str,
    mts: float,
    gh_closure: dict[str, Any],
    target_num: str = "",
) -> None:
  """Updates lock/close/reopen state from one decoded GitHub notification message."""
  text = f"{body_txt}\n{snip}"
  if _RE_GH_LOCK_MSG.search(text):
    gh_closure["locked"] = True
    gh_closure["closed"] = True
    if mts > float(gh_closure.get("closed_ts") or 0.0):
      gh_closure["closed_ts"] = mts
  for m in _RE_GH_CLOSED_LINE.finditer(text):
    num = m.group(1) or m.group(2)
    if target_num and num != target_num:
      continue
    gh_closure["closed"] = True
    if mts > float(gh_closure.get("closed_ts") or 0.0):
      gh_closure["closed_ts"] = mts
  for m in _RE_GH_REOPENED_LINE.finditer(text):
    if target_num and m.group(1) != target_num:
      continue
    if mts >= float(gh_closure.get("closed_ts") or 0.0):
      gh_closure["closed"] = False
      gh_closure["closed_ts"] = 0.0


def _fetch_gmail_thread_provenance(
    thread_id: str,
    thread_cache: dict[str, Any],
    now: float,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
  """Fetches first-message date, sender/recipient, and full-body text for a Gmail thread."""
  tid = str(thread_id or "").strip()
  if not tid:
    return {}
  cached = thread_cache.get(tid)
  if (
      not force_refresh
      and isinstance(cached, Mapping)
      and int(cached.get("schema_ver") or 0) == 6
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
    gh_target_key = (
        work_store.extract_github_notif_target(root_subj)
        if "notifications@github.com" in from0.lower()
        else ""
    )
    is_gh_issue = gh_target_key.startswith("github_issue:")
    is_gh_pr = gh_target_key.startswith("github_pr:")
    is_gh_notif = is_gh_issue or is_gh_pr
    is_gdoc_comment = "comments-noreply@docs.google.com" in from0.lower()
    is_drive_share = bool(
        "drive-shares-dm-noreply@google.com" in from0.lower()
        or re.search(
            r"\(via\s+google\s+(?:docs|slides|sheets|drive)\)", from0, flags=re.IGNORECASE
        )
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
    last_message_id = str(m0.get("id") or "")
    last_from_me = started_by_me
    sent_by_me = started_by_me
    non_bot_human_count = 0
    last_snippet = ""
    last_unquoted_text = ""
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
    gh_closure: dict[str, Any] = {"locked": False, "closed": False, "closed_ts": 0.0}
    gh_target_num = gh_target_key.rsplit(":", 1)[-1] if gh_target_key else ""
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
      if "comments-noreply@docs.google.com" in f_hdr.lower():
        is_gdoc_comment = True
      if "drive-shares-dm-noreply@google.com" in f_hdr.lower():
        is_drive_share = True
      mts = float(int(m.get("internalDate") or 0)) / 1000.0 if m.get("internalDate") else 0.0
      if is_gh_notif:
        _update_gh_notif_closure_state(
            body_txt, snip, mts, gh_closure, target_num=gh_target_num
        )
      is_me = _is_me_sender(f_hdr)
      cand_sender = _clean_display_name(f_hdr)
      is_gdoc_human_proxy = (
          ("comments-noreply@docs.google.com" in f_hdr.lower())
          or ("drive-shares-dm-noreply@google.com" in f_hdr.lower())
      ) and bool(cand_sender) and cand_sender.lower() not in ("google docs", "google drive")
      is_gh_bot = is_gh_notif and _is_github_bot_login(cand_sender)
      is_bot = (
          "[bot]" in cand_sender.lower()
          or is_gh_bot
          or (not is_gdoc_human_proxy and bool(_RE_AUTOMATED_SENDER_ADDR.search(f_hdr)))
      )
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
        if not is_gh_notif and not is_gdoc_comment:
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
      if mts >= last_message_ts and not is_gh_bot:
        last_message_ts = mts
        last_from_me = is_me
        last_labels = m_labels
        if m.get("id"):
          last_message_id = str(m.get("id"))
        last_unquoted_text = (body_txt.strip() if body_txt else snip)[:400]
      if not is_bot and cand_sender:
        non_bot_human_count += 1
      if not is_me and f_hdr and not is_bot:
        if cand_sender:
          if not counterparty or is_gh_notif or is_gdoc_comment:
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
    full_thread_text = "\n".join(text_blocks)

    gdoc_id = ""
    disco_id = ""
    doc_title = ""
    gdoc_me_engaged = False
    gdoc_resolved = False
    gdoc_resolved_by_me = False
    if is_gdoc_comment or is_drive_share:
      m_disco = _RE_GDOC_DISCO_URL.search(full_thread_text)
      if m_disco:
        gdoc_id = m_disco.group(1)
        disco_id = m_disco.group(2)
      else:
        m_doc_any = RE_GDOC.search(full_thread_text)
        if m_doc_any:
          gdoc_id = m_doc_any.group(1)
        m_d_only = re.search(r"[?#&]disco=([A-Za-z0-9._\-]+)", full_thread_text)
        if m_d_only:
          disco_id = m_d_only.group(1)
      m_nt = _RE_GDOC_NOTIFY_TITLE.search(full_thread_text)
      if m_nt:
        doc_title = urllib.parse.unquote_plus(m_nt.group(1)).strip()
      if not doc_title:
        m_sh = _RE_GDOC_SHARE_SUBJECT.match(root_subj)
        if m_sh:
          doc_title = m_sh.group(1).strip()
      if not doc_title:
        m_fd = _RE_GDOC_FOLLOWING_DOC.search(full_thread_text)
        if m_fd:
          doc_title = m_fd.group(1).strip()
      if is_gdoc_comment:
        if re.search(r"(?:^|\n)\s*(?:Julia Lavrova|jlavrova)\s*\r?\n", full_thread_text, re.I):
          sent_by_me = True
        if re.search(
            r"mentioned\s+you\s+in\s+a\s+comment|assigned\s+to\s+you|@jlavrova|\+jlavrova",
            full_thread_text,
            re.IGNORECASE,
        ):
          gdoc_me_engaged = True
        if re.search(r"\bmarked\s+as\s+resolved\b", last_unquoted_text or full_thread_text, re.I):
          gdoc_resolved = True
          gdoc_resolved_by_me = bool(last_from_me)

    prov = {
        "schema_ver": 6,
        "thread_id": tid,
        "origin_ts": origin_ts,
        "root_subject": root_subj,
        "origin_from": origin_from,
        "started_by_me": started_by_me,
        "sent_by_me": sent_by_me,
        "last_from_me": last_from_me,
        "last_message_ts": last_message_ts,
        "last_message_id": last_message_id,
        "last_snippet": last_snippet[:160],
        "last_unquoted_text": last_unquoted_text,
        "counterparty": counterparty,
        "counterparty_reply_ts": counterparty_reply_ts,
        "message_count": len(msgs),
        "non_bot_human_count": non_bot_human_count,
        "gh_target_key": gh_target_key,
        "is_github_issue_thread": is_gh_issue,
        "is_github_pr_thread": is_gh_pr,
        "gh_locked": bool(gh_closure["locked"]),
        "gh_thread_closed": bool(gh_closure["closed"]),
        "gh_closed_ts": float(gh_closure["closed_ts"]),
        "is_gdoc_comment": is_gdoc_comment,
        "is_automated": is_automated,
        "is_drive_share": is_drive_share,
        "gdoc_id": gdoc_id,
        "disco_id": disco_id,
        "doc_title": doc_title,
        "gdoc_me_engaged": gdoc_me_engaged,
        "gdoc_resolved": gdoc_resolved,
        "gdoc_resolved_by_me": gdoc_resolved_by_me,
        "in_inbox": in_inbox,
        "in_trash_or_spam": in_trash_or_spam,
        "has_personal_category": has_personal_category,
        "has_non_personal_category": has_non_personal_category,
        "is_bulk_or_list": is_bulk_or_list,
        "me_addressed": me_addressed,
        "recipient_count": recipient_count,
        "thread_text": full_thread_text,
        "fetched_at": now,
    }
    thread_cache[tid] = prov
    return dict(prov)
  except Exception:
    return dict(cached) if isinstance(cached, Mapping) else {"thread_id": tid}


def _thread_covered_by_draft(
    tid: str, last_msg_ts: float, draft_ts_by_thread: Mapping[str, float]
) -> bool:
  """Returns True when an active draft on tid was edited at or after last_msg_ts."""
  if not tid:
    return False
  d_ts = float(draft_ts_by_thread.get(tid) or 0.0)
  if d_ts <= 0.0:
    return False
  return last_msg_ts <= 0.0 or last_msg_ts <= d_ts


def _ingest_gdoc_gmail_thread(
    store: work_store.WorkStore,
    t_prov: Mapping[str, Any],
    draft_ts_by_thread: Mapping[str, float],
    dismissed_keys: set[str],
    completed_thread_ts: Mapping[str, float],
    cur_now: float,
) -> bool:
  """Ingests a Google Docs comment thread or direct share email; returns True when handled."""
  is_comment = bool(t_prov.get("is_gdoc_comment"))
  is_share = bool(t_prov.get("is_drive_share"))
  if not is_comment and not is_share:
    return False
  if bool(t_prov.get("in_trash_or_spam")):
    return True
  gdoc_id = str(t_prov.get("gdoc_id") or "").strip()
  if not gdoc_id:
    return True

  tid = str(t_prov.get("thread_id") or "").strip()
  t_key = f"gmail_thread:{tid}" if tid else ""
  cp = str(t_prov.get("counterparty") or "").strip()
  doc_title = str(t_prov.get("doc_title") or "").strip()
  root_subj = str(t_prov.get("root_subject") or "").strip()
  last_msg_ts = float(t_prov.get("last_message_ts") or 0.0)
  doc_key = f"gdoc:{gdoc_id}"
  doc_url = f"https://docs.google.com/document/d/{gdoc_id}/edit"

  if is_comment:
    # Ignore bystander comment digests on uninvolved docs where user was never mentioned
    existing_doc_wid = store.find_work_by_artifact(doc_key, roles=("primary", "channel"))
    eligible = (
        bool(t_prov.get("sent_by_me"))
        or bool(t_prov.get("me_addressed"))
        or bool(t_prov.get("gdoc_me_engaged"))
        or bool(existing_doc_wid)
    )
    if not eligible:
      return True

    # Fall back to Gmail thread ID when older notification templates omit ?disco=
    disco_id = str(t_prov.get("disco_id") or "").strip() or f"thread-{tid}"
    gt_key = f"gdoc_thread:{gdoc_id}:{disco_id}"
    # Merge earlier transcript-created Drive commentId artifact lacking the "doco." prefix
    if disco_id.startswith("doco."):
      bare_gt = f"gdoc_thread:{gdoc_id}:{disco_id[5:]}"
      if store.get_artifact(bare_gt) is not None:
        store.rekey_gdoc_thread_artifact(bare_gt, gt_key, now=cur_now)
    if gt_key in dismissed_keys and not bool(t_prov.get("sent_by_me")):
      return True
    existing_gt_wid = store.find_work_by_artifact(gt_key, roles=("primary", "channel"))
    done_ts_gt = max(
        float(completed_thread_ts.get(gt_key) or 0.0),
        float(completed_thread_ts.get(doc_key) or 0.0),
    )
    if (
        not existing_gt_wid
        and not existing_doc_wid
        and done_ts_gt > 0
        and last_msg_ts <= done_ts_gt
    ):
      return True

    if doc_title:
      store.upsert_artifact(
          doc_key,
          kind="gdoc",
          title=doc_title,
          url=doc_url,
          state={"doc_title": doc_title},
          now=cur_now,
      )
    elif store.get_artifact(doc_key) is None:
      store.upsert_artifact(doc_key, kind="gdoc", title=doc_key, url=doc_url, now=cur_now)

    gt_url = (
        f"https://docs.google.com/document/d/{gdoc_id}/edit?disco={disco_id}"
        if t_prov.get("disco_id")
        else doc_url
    )
    base_lbl = doc_title or root_subj or doc_key
    gt_title = f"{base_lbl} ({cp})" if cp and cp.lower() not in base_lbl.lower() else base_lbl
    wid = (
        existing_gt_wid
        or existing_doc_wid
        or (store.find_work_by_artifact(t_key, roles=("primary", "channel")) if t_key else None)
        or store.create_work(doc_title or root_subj or doc_key, now=cur_now)
    )
    prev_gt = store.get_artifact(gt_key) or {}
    prev_gt_st = prev_gt.get("state") if isinstance(prev_gt.get("state"), Mapping) else {}
    prev_resolved_ts = float(prev_gt_st.get("resolved_at") or 0.0)
    gt_state: dict[str, Any] = {
        "doc_id": gdoc_id,
        "disco_id": disco_id,
        "doc_title": doc_title,
        "thread_id": tid,
        "origin_ts": float(t_prov.get("origin_ts") or 0.0),
        "started_by_me": bool(t_prov.get("started_by_me")),
        "sent_by_me": bool(t_prov.get("sent_by_me")),
        "last_from_me": bool(t_prov.get("last_from_me")),
        "last_message_ts": last_msg_ts,
        "last_message_id": str(t_prov.get("last_message_id") or ""),
        "last_snippet": str(t_prov.get("last_snippet") or ""),
        "last_unquoted_text": str(t_prov.get("last_unquoted_text") or ""),
        "message_count": int(t_prov.get("message_count") or 1),
        "counterparty": cp,
        "is_active_correspondence": True,
        "unanswered_human": (
            not bool(t_prov.get("last_from_me"))
            and not _thread_covered_by_draft(tid, last_msg_ts, draft_ts_by_thread)
        ),
    }
    # External resolves trigger a 1-click confirmation bar instead of silently closing the card
    if bool(t_prov.get("gdoc_resolved")):
      gt_state["resolved"] = True
      if bool(t_prov.get("gdoc_resolved_by_me")):
        gt_state["resolved_by_me"] = True
      else:
        gt_state["resolved_externally"] = True
    elif prev_resolved_ts <= 0 or last_msg_ts > prev_resolved_ts:
      gt_state["resolved"] = False
      gt_state["resolved_by_me"] = False
      gt_state["resolved_externally"] = False
    store.upsert_artifact(
        gt_key, kind="gdoc_thread", title=gt_title, url=gt_url, state=gt_state, now=cur_now
    )
    if t_key and store.get_artifact(t_key) is not None:
      store.upsert_artifact(
          t_key,
          kind="gmail_thread",
          state={**dict(t_prov), "gdoc_thread_key": gt_key},
          now=cur_now,
      )
    store.link_artifact(
        wid, doc_key, role="primary", source="auto", link_reason="gdoc_comment", now=cur_now
    )
    store.link_artifact(
        wid, gt_key, role="channel", source="auto", link_reason="gdoc_comment", now=cur_now
    )
    return True

  # Outbound shares mint/update Works; cold inbound shares only attach to already-tracked docs
  is_outbound_share = bool(t_prov.get("started_by_me") or t_prov.get("sent_by_me"))
  rec_cnt = int(t_prov.get("recipient_count") or 0)
  is_direct_inbound_share = (
      bool(t_prov.get("me_addressed"))
      and 1 <= rec_cnt <= MAX_HUMAN_GROUP_RECIPIENTS
      and not bool(t_prov.get("is_bulk_or_list"))
  )
  existing_share_wid = store.find_work_by_artifact(doc_key, roles=("primary", "channel"))
  if not is_outbound_share and not (is_direct_inbound_share and existing_share_wid):
    return True
  done_ts_doc = float(completed_thread_ts.get(doc_key) or 0.0)
  if not existing_share_wid and done_ts_doc > 0 and last_msg_ts <= done_ts_doc:
    return True
  wid = (
      existing_share_wid
      or (store.find_work_by_artifact(t_key, roles=("primary", "channel")) if t_key else None)
      or store.create_work(doc_title or root_subj or doc_key, now=cur_now)
  )
  share_state: dict[str, Any] = {
      "doc_id": gdoc_id,
      "doc_title": doc_title,
      "share_thread_id": tid,
      "origin_ts": float(t_prov.get("origin_ts") or 0.0),
      "started_by_me": bool(t_prov.get("started_by_me")),
      "sent_by_me": bool(t_prov.get("sent_by_me")),
      "last_from_me": bool(t_prov.get("last_from_me")),
      "last_message_ts": last_msg_ts,
      "last_message_id": str(t_prov.get("last_message_id") or ""),
      "last_snippet": str(t_prov.get("last_snippet") or ""),
      "last_unquoted_text": str(t_prov.get("last_unquoted_text") or ""),
      "message_count": int(t_prov.get("message_count") or 1),
      "counterparty": cp,
      "is_drive_share": True,
      "is_active_correspondence": True,
  }
  store.upsert_artifact(
      doc_key,
      kind="gdoc",
      title=doc_title or doc_key,
      url=doc_url,
      state=share_state,
      now=cur_now,
  )
  if t_key and store.get_artifact(t_key) is not None:
    store.upsert_artifact(
        t_key,
        kind="gmail_thread",
        state={**dict(t_prov), "shared_doc_key": doc_key},
        now=cur_now,
    )
  store.link_artifact(
      wid, doc_key, role="primary", source="auto", link_reason="gdoc_share", now=cur_now
  )
  return True


def _work_has_open_cl(store: work_store.WorkStore, work_id: str) -> bool:
  roles = store.get_work_artifact_roles(work_id)
  for akey, role in roles.items():
    if role != "primary":
      continue
    if akey.startswith(("gerrit:", "github_pr:", "piper:")):
      ar = store.get_artifact(akey) or {}
      st = ar.get("state") if isinstance(ar.get("state"), Mapping) else {}
      # Companion probes are review scratchpads and do not count as active deliverable CLs
      if work_store.is_companion_artifact(akey, st, str(ar.get("title") or "")):
        continue
      if str(st.get("status") or "NEW").upper() in ("NEW", "OPEN"):
        return True
  return False


def _gmail_draft_still_exists(did: str) -> bool:
  """Checks whether a draft ID outside the --max 100 list window still exists in Gmail."""
  try:
    res = subprocess.run(
        [GMAIL_BIN, "readonly", "get-draft", did, "--json"],
        capture_output=True,
        text=True,
        timeout=6,
        check=False,
    )
    return res.returncode == 0 and bool(res.stdout.strip())
  except Exception:
    # Fail closed on transient CLI errors so drafts above 100 are never falsely marked SENT
    return True


def _ingest_single_gmail_draft(
    store: work_store.WorkStore,
    d_stub: Mapping[str, Any],
    did: str,
    thread_cache: dict[str, Any],
    draft_ts_by_thread: dict[str, float],
    cur_now: float,
) -> None:
  """Fetches one active Gmail draft, upserts draft/thread state, and syncs non-WB obligations."""
  d_res = subprocess.run(
      [GMAIL_BIN, "readonly", "get-draft", did, "--json"],
      capture_output=True,
      text=True,
      timeout=6,
      check=False,
  )
  if d_res.returncode != 0 or not d_res.stdout.strip():
    return
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
  unquoted_body = _decode_gmail_payload_text(payload, strip_quotes=True)
  d_key = f"gmail_draft:{did}"
  prev_art = store.get_artifact(d_key) or {}
  prev_st = prev_art.get("state") if isinstance(prev_art.get("state"), Mapping) else {}
  from_wb = bool(prev_st.get("from_workbench_draft"))
  body_sig = hashlib.sha1(f"{subj}\n{to_hdr}\n{unquoted_body}".encode("utf-8")).hexdigest()[:16]

  # Pin draft_updated_ts to Gmail internalDate or content hash so 90s syncs never mask new replies
  raw_idate = (msg or {}).get("internalDate") if isinstance(msg, Mapping) else None
  if raw_idate:
    draft_msg_ts = float(int(raw_idate)) / 1000.0
  elif prev_st.get("draft_updated_ts") and (
      prev_st.get("body_sig") == body_sig or (not prev_st.get("body_sig") and from_wb)
  ):
    draft_msg_ts = float(prev_st["draft_updated_ts"])
  else:
    draft_msg_ts = cur_now

  if thread_id:
    draft_ts_by_thread[thread_id] = max(draft_ts_by_thread.get(thread_id, 0.0), draft_msg_ts)

  extracted = extract_artifacts_from_text(f"{subj}\n{unquoted_body}")
  inline_bug_titles = extract_bug_titles_from_html_or_text(unquoted_body)
  to_name = _clean_display_name(to_hdr) if to_hdr else ""
  primaries = [
      (akey, akind, aurl)
      for akey, akind, aurl in extracted
      if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
  ]
  mentioned_keys = [akey for akey, _, _ in primaries]
  draft_bug_keys = [
      akey for akey, akind, _ in primaries if akind in ("buganizer", "github_issue")
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
          "draft_updated_ts": draft_msg_ts,
          "body_sig": body_sig,
          "from_workbench_draft": from_wb,
      },
      now=cur_now,
  )
  if not primaries and not from_wb:
    return
  wid = store.find_work_by_artifact(d_key, roles=("primary", "channel"))
  if not wid and thread_id:
    wid = store.find_work_by_artifact(f"gmail_thread:{thread_id}", roles=("primary", "channel"))
  if not wid and not from_wb:
    for akey, _, _ in primaries:
      wid = store.find_work_by_artifact(akey, roles=("primary",))
      if wid:
        break
  if not wid:
    if from_wb:
      return
    wid = store.create_work(subj, now=cur_now)
  store.link_artifact(wid, d_key, role="channel", source="auto", now=cur_now)
  if thread_id:
    t_key = f"gmail_thread:{thread_id}"
    t_prov = _fetch_gmail_thread_provenance(thread_id, thread_cache, cur_now)
    last_ts = float(t_prov.get("last_message_ts") or 0.0)
    t_prov["unanswered_human"] = bool(
        not t_prov.get("last_from_me", True)
        and not t_prov.get("is_automated")
        and not _thread_covered_by_draft(thread_id, last_ts, draft_ts_by_thread)
    )
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
    # A draft is a reply on the thread, not a reason to demote a gmail_root primary thread;
    # demoting it made the root pass flip channel <-> primary on every sync
    if store.get_work_artifact_roles(wid).get(t_key) != "primary":
      store.link_artifact(wid, t_key, role="channel", source="auto", now=cur_now)

  # Materialized Workbench reply drafts keep their own ob-wbdraft gate and never promote body refs
  if from_wb:
    return

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
        akey, kind=akind, title=art_title, url=aurl, state=art_state, now=cur_now
    )
    store.link_artifact(
        wid, akey, role="primary", source="auto", link_reason="gmail_draft_ref", now=cur_now
    )

  store.reconcile_work_bug_cls(wid, now=cur_now)

  if "flutter" in subj.lower() or draft_bug_keys:
    w_roles = store.get_work_artifact_roles(wid)

    # Companion probes are abandoned/closed rather than merged, so never gate draft sending
    def _is_comp_cl(k: str) -> bool:
      """Returns True when artifact key k represents a review-companion probe CL or PR."""
      ar = store.get_artifact(k) or {}
      st = ar.get("state") if isinstance(ar.get("state"), Mapping) else {}
      return work_store.is_companion_artifact(k, st, str(ar.get("title") or ""))

    primary_gerrit = sorted(
        k
        for k, r in w_roles.items()
        if r == "primary" and k.startswith("gerrit:") and not _is_comp_cl(k)
    )
    primary_prs = sorted(
        k
        for k, r in w_roles.items()
        if r == "primary" and k.startswith("github_pr:") and not _is_comp_cl(k)
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


def _reconcile_disappeared_gmail_draft(
    store: work_store.WorkStore,
    gone_did: str,
    draft_list_full: bool,
    thread_cache: dict[str, Any],
    draft_ts_by_thread: dict[str, float],
    active_draft_ids: set[str],
    cur_now: float,
) -> None:
  """Classifies a draft absent from the list query as >100-surviving, DISCARDED, or SENT."""
  d_key = f"gmail_draft:{gone_did}"
  d_st = ((store.get_artifact(d_key) or {}).get("state")) or {}
  # Drafts pushed past --max 100 stay active and continue covering their reply thread
  if draft_list_full and _gmail_draft_still_exists(gone_did):
    active_draft_ids.add(gone_did)
    s_tid = str(d_st.get("thread_id") or "").strip()
    s_ts = float(d_st.get("draft_updated_ts") or 0.0)
    if s_tid and s_ts > 0.0:
      draft_ts_by_thread[s_tid] = max(draft_ts_by_thread.get(s_tid, 0.0), s_ts)
    return

  # Verify real outbound mail on the thread so discarded Compose tabs never wipe live drafts
  if d_st.get("from_workbench_draft"):
    d_tid = str(d_st.get("thread_id") or "").strip()
    was_sent = False
    # Standalone compose drafts lack a prior threadId so keep Workbench text unless manually cleared
    if d_tid:
      t_prov = _fetch_gmail_thread_provenance(d_tid, {}, cur_now)
      thread_cache[d_tid] = t_prov
      store.upsert_artifact(f"gmail_thread:{d_tid}", kind="gmail_thread", state=t_prov, now=cur_now)
      draft_ts = float(d_st.get("draft_updated_ts") or 0.0)
      last_ts = float(t_prov.get("last_message_ts") or 0.0)
      was_sent = bool(t_prov.get("last_from_me")) or (
          bool(t_prov.get("sent_by_me")) and last_ts >= draft_ts - 60.0
      )
    if not was_sent:
      store.mark_materialized_draft_discarded(d_key, now=cur_now)
      return

  store.upsert_artifact(d_key, kind="gmail_draft", state={"status": "SENT"}, now=cur_now)


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

  # Union DB open drafts so drafts materialized and sent between syncs still reconcile
  store_open_dids, tracked_open_dids = store.list_open_gmail_draft_ids()
  prev_drafts = set(watcher_state.get("active_draft_ids") or []) | store_open_dids
  active_draft_ids: set[str] = set()
  draft_ts_by_thread: dict[str, float] = {}
  draft_list: list[Any] = []
  drafts_list_ok = False
  try:
    res = subprocess.run(
        [GMAIL_BIN, "readonly", "drafts", "--max", "100", "--json"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if res.returncode == 0 and res.stdout.strip():
      raw_drafts = json.loads(res.stdout)
      if raw_drafts is None:
        draft_list = []
        drafts_list_ok = True
      elif isinstance(raw_drafts, list):
        draft_list = raw_drafts
        drafts_list_ok = True
      elif isinstance(raw_drafts, Mapping) and "error" not in raw_drafts:
        cand_list = raw_drafts.get("drafts")
        if cand_list is None or isinstance(cand_list, list):
          draft_list = cand_list if isinstance(cand_list, list) else []
          drafts_list_ok = True
  except Exception:
    drafts_list_ok = False

  # Collect all active draft IDs up front so per-draft errors or >10 drafts never trigger false SENT
  valid_stubs: list[tuple[int, Mapping[str, Any], str]] = []
  for idx, d_stub in enumerate(draft_list):
    if not isinstance(d_stub, Mapping):
      continue
    did = str(d_stub.get("id") or "").strip()
    if not did:
      continue
    active_draft_ids.add(did)
    stub_tid = str(
        (d_stub.get("message") or {}).get("threadId")
        if isinstance(d_stub.get("message"), Mapping)
        else (d_stub.get("threadId") or "")
    ).strip()
    if stub_tid:
      stored_st = ((store.get_artifact(f"gmail_draft:{did}") or {}).get("state")) or {}
      stored_ts = float(stored_st.get("draft_updated_ts") or 0.0)
      if stored_ts > 0.0:
        draft_ts_by_thread[stub_tid] = max(draft_ts_by_thread.get(stub_tid, 0.0), stored_ts)
    valid_stubs.append((idx, d_stub, did))

  # Cap get-draft calls at 25 for unlinked scratch drafts while refreshing all Work-tracked drafts
  for idx, d_stub, did in valid_stubs:
    if idx >= 25 and did not in tracked_open_dids:
      continue
    try:
      _ingest_single_gmail_draft(
          store, d_stub, did, thread_cache, draft_ts_by_thread, cur_now
      )
    except Exception:
      continue

  # Reconcile disappeared drafts only when the drafts list query succeeded cleanly
  if drafts_list_ok:
    draft_list_full = len(draft_list) >= 100
    for gone_did in prev_drafts - active_draft_ids:
      _reconcile_disappeared_gmail_draft(
          store,
          gone_did,
          draft_list_full,
          thread_cache,
          draft_ts_by_thread,
          active_draft_ids,
          cur_now,
      )
    watcher_state["active_draft_ids"] = sorted(active_draft_ids)

  # Run targeted Gmail search passes and link via hybrid correspondence & GDoc rules
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
      (
          "gdoc",
          (
              "newer_than:21d from:(comments-noreply@docs.google.com"
              " OR drive-shares-dm-noreply@google.com)"
          ),
          "25",
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
        if t_prov.get("is_gdoc_comment") or (
            t_prov.get("is_drive_share")
            and (pass_kind == "gdoc" or bool(t_prov.get("gdoc_id")))
        ):
          _ingest_gdoc_gmail_thread(
              store,
              t_prov,
              draft_ts_by_thread,
              dismissed_keys,
              completed_thread_ts,
              cur_now,
          )
          continue
        cp = str(t_prov.get("counterparty") or "").strip()
        root_subj = str(t_prov.get("root_subject") or m.get("subject") or "").strip()
        if not root_subj or not cp:
          continue
        if any(tok in cp.lower() for tok in ("unsubscribe", "noreply", "organizers")):
          continue
        is_gh_notif = bool(
            t_prov.get("is_github_issue_thread") or t_prov.get("is_github_pr_thread")
        )
        if pass_kind == "gh_issue":
          if not is_gh_notif:
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
        # Skip trashed unjoined mail before artifact extraction so it never mints Works
        if t_prov.get("in_trash_or_spam") and not t_prov.get("sent_by_me"):
          if store.get_artifact(t_key) is not None:
            t_prov["unanswered_human"] = False
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
          continue
        full_text = str(t_prov.get("thread_text") or root_subj)
        t_arts = extract_artifacts_from_text(full_text)
        inline_bug_titles = extract_bug_titles_from_html_or_text(full_text)
        gh_target_key = str(
            t_prov.get("gh_target_key") or work_store.extract_github_notif_target(root_subj)
        )
        raw_primaries = [
            (akey, akind, aurl)
            for akey, akind, aurl in t_arts
            if akind in ("gerrit", "github_pr", "piper", "buganizer", "github_issue", "gdoc")
        ]
        if (
            is_gh_notif
            and gh_target_key.startswith(("github_issue:", "github_pr:"))
            and gh_target_key not in {k for k, _, _ in raw_primaries}
        ):
          gh_kind = "github_pr" if gh_target_key.startswith("github_pr:") else "github_issue"
          raw_primaries.insert(
              0, (gh_target_key, gh_kind, work_store._default_artifact_url(gh_target_key))
          )
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

        # Separate passing CL citations and collateral issue-thread bug citations from root targets
        subj_cl_keys = {
            k
            for k, kind, _ in extract_artifacts_from_text(root_subj)
            if kind in ("gerrit", "github_pr", "piper")
        }
        if is_gh_notif and gh_target_key.startswith("github_pr:"):
          subj_cl_keys.add(gh_target_key)
        email_bug_keys = {
            k for k, kind, _ in raw_primaries if kind in ("buganizer", "github_issue")
        }
        # Collect bug and CL links from subject-named CLs so true stack/bug siblings stay primary
        def _collect_cl_related_keys(st: Mapping[str, Any]) -> set[str]:
          rel: set[str] = set()
          # Normalize bare numeric cherry-pick change IDs alongside canonical item_keys
          for fld in (
              "stack_parent_ikey",
              "stack_root_ikey",
              "cherry_pick_of",
              "cherry_pick_of_change",
          ):
            val = st.get(fld)
            if val:
              raw_v = str(val).strip()
              if fld == "cherry_pick_of_change" and raw_v.isdigit():
                rel.add(work_store.canonicalize_artifact_key(f"gerrit:{raw_v}"))
              else:
                rel.add(work_store.canonicalize_artifact_key(raw_v))
          # Include stack ancestor/member lists and CLs referenced in commit trailers
          for lfld in ("stack_ancestors", "stack_ikeys", "fixes", "fixes_cls", "fixed_by_cls"):
            for elem in st.get(lfld) or []:
              if elem:
                rel.add(work_store.canonicalize_artifact_key(str(elem)))
          for k_ref, kind_ref, _ in extract_artifacts_from_text(
              f"{st.get('subject') or ''}\n{st.get('commit_message') or ''}"
          ):
            if kind_ref in ("gerrit", "github_pr", "piper"):
              rel.add(k_ref)
          return rel

        subj_cl_bugs: set[str] = set()
        subj_cl_rel_refs: set[str] = set()
        for sk in subj_cl_keys:
          s_st = (store.get_artifact(sk) or {}).get("state") or {}
          subj_cl_bugs.update(work_store.extract_cl_bug_keys(s_st))
          subj_cl_rel_refs.update(_collect_cl_related_keys(s_st))
          s_owner = store.find_work_by_artifact(sk, roles=("primary",))
          if s_owner:
            for k_b, r_b in store.get_work_artifact_roles(s_owner).items():
              if r_b == "primary" and k_b.startswith(("buganizer:", "github_issue:")):
                subj_cl_bugs.add(k_b)
        existing_tid_wid = store.find_work_by_artifact(t_key, roles=("primary", "channel"))
        cp_tokens = {tok for tok in re.split(r"\W+", cp.strip().lower()) if len(tok) >= 3}
        t_primaries: list[tuple[str, str, str]] = []
        context_cl_refs: list[tuple[str, str, str]] = []
        context_bug_refs: list[tuple[str, str, str]] = []
        for akey, akind, aurl in raw_primaries:
          if (
              t_prov.get("is_github_issue_thread")
              and gh_target_key.startswith("github_issue:")
              and akind in ("buganizer", "github_issue")
              and akey != gh_target_key
          ):
            context_bug_refs.append((akey, akind, aurl))
            continue
          if akind in ("gerrit", "github_pr", "piper") and akey not in subj_cl_keys:
            w_owner = store.find_work_by_artifact(akey, roles=("primary",))
            w_roles = store.get_work_artifact_roles(w_owner) if w_owner else {}
            w_bugs = {
                k
                for k, r in w_roles.items()
                if r == "primary" and (k.startswith("buganizer:") or k.startswith("github_issue:"))
            }
            # Treat body CLs sharing a bug or stack link with subject CLs as thread peers
            art_st = (store.get_artifact(akey) or {}).get("state") or {}
            cl_bugs = set(work_store.extract_cl_bug_keys(art_st)) | w_bugs
            cl_rel_cls = _collect_cl_related_keys(art_st) if art_st else set()
            is_related_to_subj_cl = bool(
                akey in subj_cl_rel_refs
                or (cl_bugs & (email_bug_keys | subj_cl_bugs))
                or (cl_rel_cls & subj_cl_keys)
            )
            # Body CL belonging to another work when the email subject already names a different CL,
            # or when that work already has a primary email thread with a different counterparty
            w_has_other_cp_email = False
            for k_r, r_val in w_roles.items():
              if k_r.startswith("gmail_thread:") and k_r != t_key and r_val == "primary":
                other_st = (store.get_artifact(k_r) or {}).get("state") or {}
                if not (
                    other_st.get("is_github_issue_thread")
                    or other_st.get("is_github_pr_thread")
                ):
                  other_cp = str(other_st.get("counterparty") or "").strip().lower()
                  other_toks = {tok for tok in re.split(r"\W+", other_cp) if len(tok) >= 3}
                  if other_toks and cp_tokens and not (other_toks & cp_tokens):
                    w_has_other_cp_email = True
                    break
            if (
                w_owner
                and not is_related_to_subj_cl
                and (
                    (subj_cl_keys and not (set(w_roles.keys()) & subj_cl_keys))
                    or w_has_other_cp_email
                )
            ):
              context_cl_refs.append((akey, akind, aurl))
              continue
            # Demote body CLs on subject-scoped threads lacking a shared bug or stack link
            if (
                subj_cl_keys
                and not is_related_to_subj_cl
                and (not w_owner or w_owner != existing_tid_wid)
            ):
              context_cl_refs.append((akey, akind, aurl))
              continue
            # Foreign CL in body whose author differs from counterparty and shares no bugs
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

        matched_wids: list[str] = []
        if existing_tid_wid:
          matched_wids.append(existing_tid_wid)
        for akey, _, _ in t_primaries:
          w_match = store.find_work_by_artifact(akey, roles=("primary",))
          if w_match and w_match not in matched_wids:
            matched_wids.append(w_match)

        is_recent_reply = (cur_now - float(t_prov.get("last_message_ts") or 0.0)) <= 14 * 86400
        existing_t_st = ((store.get_artifact(t_key) or {}).get("state")) or {}
        gh_resolved = _is_gh_notif_resolved(store, t_prov, existing_t_st)
        t_prov["unanswered_human"] = bool(
            not t_prov.get("last_from_me", True)
            and not t_prov.get("is_automated")
            and not gh_resolved
            and not _thread_covered_by_draft(
                tid, float(t_prov.get("last_message_ts") or 0.0), draft_ts_by_thread
            )
            and is_recent_reply
            and (
                t_prov.get("sent_by_me")
                or is_gh_notif
                or bool(existing_tid_wid)
            )
        )

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
        if not t_prov.get("sent_by_me") and not is_gh_notif:
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
            and not is_gh_notif
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
            # Persist cold inbound metadata so promoting via + Work starts in Needs You
            store.upsert_artifact(
                t_key, kind="gmail_thread", title=t_title, url=t_url, state=t_prov, now=cur_now
            )
            # Suppress tray alerts when an active draft (including >100 survivors) covers tid
            if not _thread_covered_by_draft(
                tid, float(t_prov.get("last_message_ts") or 0.0), draft_ts_by_thread
            ):
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
                # A bot mail about a CL already in the work is its channel, not a text mention
                link_reason="gmail_reviewlog",
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
                ((store.get_artifact(k) or {}).get("state") or {}).get("is_github_issue_thread")
                or ((store.get_artifact(k) or {}).get("state") or {}).get("is_github_pr_thread")
            )
            for k, r in w_roles_now.items()
        )
        t_role = "channel" if (is_gh_notif and has_non_issue_email_root) else "primary"
        t_reason = (
            "github_pr_thread"
            if t_prov.get("is_github_pr_thread")
            else ("github_issue_thread" if t_prov.get("is_github_issue_thread") else "gmail_root")
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
        for b_akey, b_akind, b_aurl in context_bug_refs:
          if w_roles_now.get(b_akey) == "primary":
            continue
          b_desc = inline_bug_titles.get(b_akey, "")
          if b_desc:
            short_b = work_store.format_short_bug_id(b_akey)
            b_title = f"{short_b}: {b_desc}"
            b_state: dict[str, Any] | None = {"short_id": short_b, "bug_title": b_desc}
          else:
            b_title = ""
            b_state = None
          store.upsert_artifact(
              b_akey, kind=b_akind, title=b_title, url=b_aurl, state=b_state, now=cur_now
          )
          store.link_artifact(
              target_wid,
              b_akey,
              role="context_only",
              source="auto",
              link_reason="gmail_thread_ref",
              link_detail=f"Упомянут в письме '{root_subj[:50]}'",
              now=cur_now,
          )
        store.reconcile_work_bug_cls(target_wid, now=cur_now)
    except Exception:
      pass

  _refresh_tracked_gmail_threads(store, thread_cache, draft_ts_by_thread, seen_tids, cur_now)
  store.reconcile_correspondence_obligations(now=cur_now)
  return unanswered


def _is_gh_notif_resolved(
    store: work_store.WorkStore,
    t_prov: Mapping[str, Any],
    existing_t_st: Mapping[str, Any],
) -> bool:
  """Returns True when a GitHub notification thread is resolved against stored target state."""
  if not (t_prov.get("is_github_issue_thread") or t_prov.get("is_github_pr_thread")):
    return False
  gh_target_key = str(
      t_prov.get("gh_target_key")
      or existing_t_st.get("gh_target_key")
      or work_store.extract_github_notif_target(
          str(t_prov.get("root_subject") or existing_t_st.get("root_subject") or "")
      )
  )
  gh_target_st = (
      ((store.get_artifact(gh_target_key) or {}).get("state") or {}) if gh_target_key else {}
  )
  gh_arts_map = (
      {gh_target_key: {"state": gh_target_st}}
      if gh_target_key and isinstance(gh_target_st, Mapping)
      else None
  )
  return work_store.is_github_thread_resolved({**existing_t_st, **t_prov}, gh_arts_map)


def _refresh_tracked_gmail_threads(
    store: work_store.WorkStore,
    thread_cache: dict[str, Any],
    draft_ts_by_thread: Mapping[str, float],
    seen_tids: set[str],
    cur_now: float,
    limit: int = 25,
) -> None:
  """Refreshes active tracked Gmail threads missed by 21d search passes (e.g. trashed/locked)."""
  candidates: list[tuple[int, str]] = []
  queued: set[str] = set()
  for w in store.list_works(include_archived=False):
    if str(w.get("status") or "") == "done":
      continue
    w_roles = store.get_work_artifact_roles(str(w["work_id"]))
    for akey, role in w_roles.items():
      if not akey.startswith("gmail_thread:") or role not in ("primary", "channel"):
        continue
      tid = akey.split(":", 1)[1].strip()
      if not tid or tid in seen_tids or tid in queued:
        continue
      queued.add(tid)
      prev_st = ((store.get_artifact(akey) or {}).get("state")) or {}
      # Refresh stale schema or unanswered threads first so active cards never starve
      pri = 0 if (int(prev_st.get("schema_ver") or 0) < 6 or prev_st.get("unanswered_human")) else 1
      candidates.append((pri, tid))

  candidates.sort(key=lambda item: item[0])
  for _, tid in candidates[:limit]:
    seen_tids.add(tid)
    t_prov = _fetch_gmail_thread_provenance(tid, thread_cache, cur_now)
    if t_prov.get("schema_ver"):
      _upsert_refreshed_gmail_thread(store, tid, t_prov, draft_ts_by_thread, cur_now)


def _upsert_refreshed_gmail_thread(
    store: work_store.WorkStore,
    tid: str,
    t_prov: dict[str, Any],
    draft_ts_by_thread: Mapping[str, float],
    cur_now: float,
) -> bool:
  """Classifies fresh thread provenance against stored state, upserts it, returns unanswered.

  Bot mail, her own last reply, trashed unjoined threads, resolved GitHub mirrors, and threads
  covered by a newer draft never count as unanswered; only a new inbound message within 14
  days raises the flag, otherwise the stored flag is kept.
  """
  t_key = f"gmail_thread:{tid}"
  prev_art = store.get_artifact(t_key) or {}
  prev_st = prev_art.get("state") if isinstance(prev_art.get("state"), Mapping) else {}
  prev_msg_ts = float(prev_st.get("last_message_ts") or 0.0)
  new_msg_ts = float(t_prov.get("last_message_ts") or 0.0)
  gh_resolved = _is_gh_notif_resolved(store, t_prov, prev_st)
  trashed_unjoined = bool(t_prov.get("in_trash_or_spam")) and not bool(t_prov.get("sent_by_me"))
  if (
      t_prov.get("last_from_me", True)
      or t_prov.get("is_automated")
      or trashed_unjoined
      or gh_resolved
      or _thread_covered_by_draft(tid, new_msg_ts, draft_ts_by_thread)
  ):
    t_prov["unanswered_human"] = False
  elif new_msg_ts > prev_msg_ts and (cur_now - new_msg_ts) <= 14 * 86400:
    t_prov["unanswered_human"] = True
  else:
    t_prov["unanswered_human"] = bool(prev_st.get("unanswered_human"))

  cp = str(t_prov.get("counterparty") or "").strip()
  root_subj = str(t_prov.get("root_subject") or "").strip()
  t_title = (
      f"{root_subj} ({cp})"
      if (root_subj and cp and cp.lower() not in root_subj.lower())
      else root_subj
  )
  store.upsert_artifact(
      t_key,
      kind="gmail_thread",
      title=t_title,
      url=f"https://mail.google.com/mail/u/0/#all/{tid}",
      state=t_prov,
      now=cur_now,
  )
  return bool(t_prov["unanswered_human"])


def _parse_gh_key(akey: str) -> tuple[str, str, int] | None:
  """Parses a canonical github_issue:<owner>/<repo>:<num> or github_pr key into its components."""
  parts = akey.split(":")
  if len(parts) != 3 or "/" not in parts[1] or not parts[2].isdigit():
    return None
  owner, repo = parts[1].split("/", 1)
  if not re.match(r"^[A-Za-z0-9_.\-]+$", owner) or not re.match(r"^[A-Za-z0-9_.\-]+$", repo):
    return None
  return owner, repo, int(parts[2])


def _collect_github_sync_targets(
    store: work_store.WorkStore,
) -> tuple[list[tuple[str, str, str, int]], dict[str, set[str]]]:
  """Collects active GitHub Issue/PR targets (primary/channel before context_only) and threads."""
  primary_targets: list[tuple[str, str, str, int]] = []
  context_targets: list[tuple[str, str, str, int]] = []
  seen_targets: set[str] = set()
  target_to_gmail_keys: dict[str, set[str]] = {}

  for w in store.list_works(include_archived=False):
    if str(w.get("status") or "") == "done":
      continue
    w_roles = store.get_work_artifact_roles(str(w["work_id"]))
    for akey, role in w_roles.items():
      if akey.startswith("gmail_thread:") and role in ("primary", "channel"):
        art = store.get_artifact(akey) or {}
        st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
        t_key = str(st.get("gh_target_key") or "") or work_store.extract_github_notif_target(
            str(st.get("root_subject") or art.get("title") or "")
        )
        if t_key.startswith(("github_issue:", "github_pr:")):
          target_to_gmail_keys.setdefault(t_key, set()).add(akey)
          if t_key not in seen_targets:
            parsed = _parse_gh_key(t_key)
            if parsed:
              seen_targets.add(t_key)
              primary_targets.append((t_key, parsed[0], parsed[1], parsed[2]))
      elif akey.startswith(("github_issue:", "github_pr:")) and akey not in seen_targets:
        parsed = _parse_gh_key(akey)
        if parsed:
          seen_targets.add(akey)
          entry = (akey, parsed[0], parsed[1], parsed[2])
          if role in ("primary", "channel"):
            primary_targets.append(entry)
          else:
            context_targets.append(entry)

  return primary_targets + context_targets, target_to_gmail_keys


def _build_github_graphql_query(owner: str, repo: str, chunk: list[tuple[str, int]]) -> str:
  """Builds a batched GraphQL issueOrPullRequest query for one repository chunk."""
  fields: list[str] = []
  for idx, (_, num) in enumerate(chunk):
    fields.append(
        f"n{idx}: issueOrPullRequest(number: {num}) {{\n"
        "      __typename\n"
        "      ... on Issue {\n"
        "        number title state stateReason locked closedAt updatedAt url\n"
        "        assignees(first: 10) { nodes { login } }\n"
        "        comments(last: 20) { nodes { createdAt viewerDidAuthor author { login } } }\n"
        "      }\n"
        "      ... on PullRequest {\n"
        "        number title state locked closedAt mergedAt updatedAt url\n"
        "        comments(last: 20) { nodes { createdAt viewerDidAuthor author { login } } }\n"
        "        reviews(last: 20) {\n"
        "          nodes { state submittedAt viewerDidAuthor author { login } }\n"
        "        }\n"
        "      }\n"
        "    }"
    )
  body = "\n    ".join(fields)
  return f'query {{ repository(owner: "{owner}", name: "{repo}") {{\n    {body}\n  }} }}'


def _extract_github_comment_timestamps(node: Mapping[str, Any]) -> tuple[float, float]:
  """Returns (last_self_comment_at, last_human_comment_at) across Issue/PR comments & reviews."""
  last_self_ts = 0.0
  last_human_ts = 0.0
  comments = ((node.get("comments") or {}).get("nodes") or []) if isinstance(
      node.get("comments"), Mapping
  ) else []
  for item in comments:
    if not isinstance(item, Mapping):
      continue
    ts = _parse_iso_ts(str(item.get("createdAt") or ""))
    if ts <= 0:
      continue
    if _is_self_github_actor(item):
      last_self_ts = max(last_self_ts, ts)
    else:
      author = item.get("author")
      login = str(author.get("login") or "") if isinstance(author, Mapping) else ""
      if not _is_github_bot_login(login):
        last_human_ts = max(last_human_ts, ts)

  reviews = ((node.get("reviews") or {}).get("nodes") or []) if isinstance(
      node.get("reviews"), Mapping
  ) else []
  for rev in reviews:
    if not isinstance(rev, Mapping):
      continue
    if str(rev.get("state") or "").strip().upper() == "PENDING":
      continue
    sub_at = str(rev.get("submittedAt") or "").strip()
    if not sub_at:
      continue
    ts = _parse_iso_ts(sub_at)
    if ts <= 0:
      continue
    if _is_self_github_actor(rev):
      last_self_ts = max(last_self_ts, ts)
    else:
      author = rev.get("author")
      login = str(author.get("login") or "") if isinstance(author, Mapping) else ""
      if not _is_github_bot_login(login):
        last_human_ts = max(last_human_ts, ts)
  return last_self_ts, last_human_ts


def _apply_github_node_to_store(
    store: work_store.WorkStore,
    akey: str,
    node: Mapping[str, Any],
    linked_gmail_keys: set[str],
    cur_now: float,
) -> None:
  """Updates one GitHub Issue/PR artifact and its linked Gmail thread correspondence state."""
  gh_title = str(node.get("title") or "").strip()
  gh_status = str(node.get("state") or "").strip().upper()
  gh_locked = bool(node.get("locked"))
  closed_at = _parse_iso_ts(str(node.get("closedAt") or ""))
  merged_at = _parse_iso_ts(str(node.get("mergedAt") or ""))
  updated_at = _parse_iso_ts(str(node.get("updatedAt") or ""))
  gh_url = str(node.get("url") or "").strip()
  last_self_ts, last_human_ts = _extract_github_comment_timestamps(node)

  art_state: dict[str, Any] = {
      "status": gh_status,
      "locked": gh_locked,
      "closed_at": closed_at,
      "merged_at": merged_at,
      "updated_at": updated_at,
      "last_self_comment_at": last_self_ts,
      "last_human_comment_at": last_human_ts,
      "synced_at": cur_now,
  }
  # Clear cached open entity_state once GitHub GraphQL reports the PR or issue closed/merged
  if gh_status in ("MERGED", "CLOSED"):
    art_state["entity_state"] = {}
  if akey.startswith("github_issue:"):
    short_b = work_store.format_short_bug_id(akey)
    art_title = f"{short_b}: {gh_title}" if gh_title else ""
    if gh_title:
      art_state["short_id"] = short_b
      art_state["bug_title"] = gh_title
    state_reason = str(node.get("stateReason") or "").strip().upper()
    if state_reason:
      art_state["state_reason"] = state_reason
    if isinstance(node.get("assignees"), Mapping):
      asg_nodes = node["assignees"].get("nodes") or []
      logins = [
          str(a.get("login") or "").strip()
          for a in asg_nodes
          if isinstance(a, Mapping) and str(a.get("login") or "").strip()
      ]
      art_state["assignee"] = ", ".join(logins)
    akind = "github_issue"
  else:
    num_str = akey.split(":")[-1]
    art_title = f"{num_str}: {gh_title}" if gh_title else ""
    akind = "github_pr"

  store.upsert_artifact(akey, kind=akind, title=art_title, url=gh_url, state=art_state, now=cur_now)

  for t_key in linked_gmail_keys:
    t_art = store.get_artifact(t_key)
    if not t_art:
      continue
    t_st = t_art.get("state") if isinstance(t_art.get("state"), Mapping) else {}
    t_patch: dict[str, Any] = {
        "gh_target_key": akey,
        "gh_locked": gh_locked,
        "gh_thread_closed": gh_status in ("CLOSED", "MERGED"),
        "gh_closed_ts": max(closed_at, merged_at),
    }
    if last_self_ts > 0:
      t_patch["gh_self_reply_ts"] = last_self_ts
    if last_human_ts > 0:
      t_patch["gh_human_reply_ts"] = last_human_ts

    last_msg_ts = float(t_st.get("last_message_ts") or 0.0)
    merged_t_st = {**t_st, **t_patch}
    resolved = work_store.is_github_thread_resolved(merged_t_st, {akey: {"state": art_state}})
    eff_self_ts = max(float(t_st.get("gh_self_reply_ts") or 0.0), last_self_ts)
    if resolved:
      t_patch["unanswered_human"] = False
    elif eff_self_ts > 0 and eff_self_ts >= last_human_ts and (
        eff_self_ts >= last_msg_ts or last_msg_ts <= last_human_ts + 30.0
    ):
      # Account for 30s Gmail delivery lag so pre-sync notification emails do not mask GH replies
      t_patch["last_from_me"] = True
      t_patch["unanswered_human"] = False
    elif last_human_ts > max(last_msg_ts, eff_self_ts) or (
        last_human_ts > eff_self_ts
        and not bool(t_st.get("last_from_me"))
        and last_msg_ts <= last_human_ts + 30.0
    ):
      trashed_unjoined = bool(t_st.get("in_trash_or_spam")) and not bool(t_st.get("sent_by_me"))
      t_patch["last_from_me"] = False
      t_patch["unanswered_human"] = not trashed_unjoined and (cur_now - last_human_ts) <= 14 * 86400

    store.upsert_artifact(t_key, kind="gmail_thread", state=t_patch, now=cur_now)


def _fetch_live_github_nodes(
    gh_targets: list[tuple[str, str, str, int]],
) -> dict[str, Mapping[str, Any]]:
  """Queries live GitHub GraphQL nodes in 30-item repo chunks for Issue/PR card keys."""
  if not gh_targets:
    return {}
  by_repo: dict[tuple[str, str], list[tuple[str, int]]] = {}
  for akey, owner, repo, num in gh_targets:
    by_repo.setdefault((owner, repo), []).append((akey, num))
  found: dict[str, Mapping[str, Any]] = {}
  for (owner, repo), repo_items in by_repo.items():
    for start in range(0, len(repo_items), 30):
      chunk = repo_items[start : start + 30]
      query = _build_github_graphql_query(owner, repo, chunk)
      try:
        res = subprocess.run(
            ["gh", "api", "graphql", "-f", f"query={query}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
      except (subprocess.SubprocessError, OSError):
        continue
      if not (res.stdout or "").strip():
        continue
      try:
        payload = json.loads(res.stdout)
      except json.JSONDecodeError:
        continue
      repo_data = (
          ((payload.get("data") or {}).get("repository") or {})
          if isinstance(payload, Mapping)
          else {}
      )
      if not isinstance(repo_data, Mapping) or not repo_data:
        continue
      by_number = {
          int(v["number"]): v
          for v in repo_data.values()
          if isinstance(v, Mapping) and v.get("number") is not None
      }
      for idx, (akey, num) in enumerate(chunk):
        node = by_number.get(num) or repo_data.get(f"n{idx}")
        if isinstance(node, Mapping):
          found[akey] = node
  return found


def sync_github_artifacts(
    store: work_store.WorkStore,
    watcher_state: dict[str, Any],
    now: float | None = None,
    force: bool = False,
) -> int:
  """Batches gh api graphql queries for non-done Works' GitHub Issues/PRs (300s throttle)."""
  cur_now = time.time() if now is None else now
  if not force and (cur_now - float(watcher_state.get("last_github_sync") or 0.0) < 300):
    return 0
  watcher_state["last_github_sync"] = cur_now

  targets, target_to_gmail_keys = _collect_github_sync_targets(store)
  if not targets:
    return 0

  nodes_by_key = _fetch_live_github_nodes(targets)
  for akey, node in nodes_by_key.items():
    _apply_github_node_to_store(
        store, akey, node, target_to_gmail_keys.get(akey, set()), cur_now
    )
  return len(nodes_by_key)


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

  if int(w_state.get("unanswered_tray_schema") or 0) < 5:
    w_state["unanswered_tray"] = []
    w_state["unanswered_tray_schema"] = 5
  unanswered = w_state.get("unanswered_tray") or []
  if include_gmail and (cur_now - float(w_state.get("last_gmail_sync") or 0.0) >= 90):
    unanswered = ingest_gmail_drafts_and_threads(store, w_state, now=cur_now)
    w_state["unanswered_tray"] = unanswered
    w_state["unanswered_tray_schema"] = 5
    w_state["last_gmail_sync"] = cur_now

  if include_gmail:
    sync_github_artifacts(store, w_state, now=cur_now)

  store.purge_config_artifacts_and_works(now=cur_now)
  store.reconcile_correspondence_obligations(now=cur_now)
  w_state["last_sync"] = cur_now
  try:
    os.makedirs(os.path.dirname(watcher_state_path), exist_ok=True)
    lock_path = f"{watcher_state_path}.lock"
    with open(lock_path, "a", encoding="utf-8") as lf:
      fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
      try:
        tmp = f"{watcher_state_path}.tmp.{os.getpid()}.{threading.get_ident()}"
        with open(tmp, "w", encoding="utf-8") as f:
          json.dump(w_state, f, indent=2)
        os.replace(tmp, watcher_state_path)
      finally:
        fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
  except OSError:
    pass

  return store.build_dashboard_projection(
      cl_watcher_state=cl_state,
      unanswered_threads=unanswered,
      now=cur_now,
  )


_RE_GOOGLE_URL_REDIRECT = re.compile(r"https?://(?:www\.)?google\.com/url\b", re.IGNORECASE)
_RE_BARE_HTTP_URL = re.compile(r"https?://[^\s<>\"']+")


def _split_url_trailing_punct(raw_url: str) -> tuple[str, str]:
  """Splits trailing sentence punctuation and unbalanced closing parens from a bare URL."""
  url = raw_url
  suffix_chars: list[str] = []
  while url:
    ch = url[-1]
    if ch in ".,;:!?]" or (ch == ")" and url.count(")") > url.count("(")):
      suffix_chars.append(ch)
      url = url[:-1]
    else:
      break
  return url, "".join(reversed(suffix_chars))


def _linkify_plain_paragraph(para: str) -> str:
  """Escapes plain-text paragraph text once while wrapping bare https?:// URLs in <a href>."""
  parts: list[str] = []
  last_end = 0
  for m in _RE_BARE_HTTP_URL.finditer(para):
    parts.append(html_lib.escape(para[last_end : m.start()], quote=True))
    clean_url, suffix = _split_url_trailing_punct(m.group(0))
    if clean_url:
      esc_url = html_lib.escape(clean_url, quote=True)
      parts.append(f'<a href="{esc_url}">{esc_url}</a>')
    parts.append(html_lib.escape(suffix, quote=True))
    last_end = m.end()
  parts.append(html_lib.escape(para[last_end:], quote=True))
  return "".join(parts).replace("\n", "<br>")


def _render_plain_text_draft_html(raw_text: str) -> str:
  """Converts a plain-text reply draft into clean HTML with explicit <a href> and <pre> blocks."""
  blocks = re.split(r"(```[\s\S]*?```)", str(raw_text or "").strip())
  html_parts: list[str] = []
  for chunk in blocks:
    if not chunk:
      continue
    # Render fenced code as standalone <pre> blocks instead of inline <code> inside list items
    if chunk.startswith("```") and chunk.endswith("```"):
      inner = re.sub(r"^```[^\n]*\n?", "", chunk[:-3]).rstrip("\n")
      html_parts.append(f"<pre>{html_lib.escape(inner)}</pre>")
      continue
    for para in re.split(r"\n{2,}", chunk.strip()):
      if not para.strip():
        continue
      html_parts.append(f"<p>{_linkify_plain_paragraph(para.strip())}</p>")
  return "\n".join(html_parts) if html_parts else f"<p>{html_lib.escape(raw_text)}</p>"


def _filter_reply_recipients(
    header_values: list[str],
    *,
    exclude_addrs: set[str] | None = None,
    always_strip_me: bool = False,
) -> tuple[str, set[str]]:
  """Deduplicates RFC 2822 recipient headers and strips self when other recipients exist."""
  parsed = [
      (name.strip(), addr.strip())
      for name, addr in getaddresses(header_values)
      if addr.strip()
  ]
  excluded = {a.lower() for a in (exclude_addrs or set())}
  non_me = [
      (n, a)
      for n, a in parsed
      if a.lower() not in excluded and not _is_me_sender(f"{n} <{a}>")
  ]
  # Keep self in To only on self-addressed threads so reply To headers never become empty
  candidates = non_me if (always_strip_me or non_me) else [
      (n, a) for n, a in parsed if a.lower() not in excluded
  ]
  formatted: list[str] = []
  seen: set[str] = set()
  for name, addr in candidates:
    low = addr.lower()
    if low in seen:
      continue
    seen.add(low)
    formatted.append(formataddr((name, addr)) if name else addr)
  return ", ".join(formatted), seen


def _verify_stored_gmail_draft_clean(gmail_bin: str, did: str) -> None:
  """Verifies via get-draft that the stored Gmail draft exists and has zero google.com/url links."""
  ver = subprocess.run(
      [gmail_bin, "readonly", "get-draft", did, "--json"],
      capture_output=True,
      text=True,
      timeout=10,
      check=False,
  )
  if ver.returncode != 0 or not ver.stdout.strip():
    raise RuntimeError(f"Failed to verify created Gmail draft {did}: {ver.stderr.strip()}")
  d_json = json.loads(ver.stdout)
  msg = d_json.get("message") if isinstance(d_json, Mapping) else {}
  payload = (msg or {}).get("payload") if isinstance(msg, Mapping) else {}
  stored_unquoted = _decode_gmail_payload_text(payload, strip_quotes=True)
  if _RE_GOOGLE_URL_REDIRECT.search(stored_unquoted):
    raise ValueError(f"Created Gmail draft {did} contains forbidden google.com/url redirect")


def materialize_reply_draft(
    store: work_store.WorkStore,
    work_id: str,
    *,
    force_new: bool = False,
    gmail_bin: str = GMAIL_BIN,
    now: float | None = None,
) -> dict[str, Any]:
  """Materializes a Work's live reply_draft into Gmail once via gmail mutate create-draft."""
  cur_now = time.time() if now is None else now
  rd = store.get_reply_draft(work_id)
  if not rd or not str(rd.get("body") or "").strip():
    raise ValueError(f"Work {work_id} has no active reply draft to materialize")
  raw_body = str(rd["body"]).strip()
  if _RE_GOOGLE_URL_REDIRECT.search(raw_body):
    raise ValueError("Reply draft body contains forbidden google.com/url redirect link")

  # Reuse an already-materialized Gmail draft when the Workbench text has not changed since
  mat_key = str(rd.get("materialized_draft_key") or "").strip()
  mat_at = float(rd.get("materialized_at") or 0.0)
  upd_at = float(rd.get("updated_at") or 0.0)
  if (
      not force_new
      and mat_key.startswith("gmail_draft:")
      and mat_at > 0.0
      and mat_at + 1e-6 >= upd_at
  ):
    prev_did = mat_key.split(":", 1)[1]
    chk = subprocess.run(
        [gmail_bin, "readonly", "get-draft", prev_did, "--json"],
        capture_output=True,
        text=True,
        timeout=8,
        check=False,
    )
    if chk.returncode == 0 and chk.stdout.strip():
      return {
          "draft_id": prev_did,
          "draft_key": mat_key,
          "url": f"https://mail.google.com/mail/u/0/#drafts/{prev_did}",
          "reused": True,
      }

  fmt = str(rd.get("format") or "html").strip().lower()
  html_body = _render_plain_text_draft_html(raw_body) if fmt == "text" else raw_body

  t_key = str(rd.get("thread_key") or "").strip()
  tid = t_key.split(":", 1)[1] if t_key.startswith("gmail_thread:") else ""
  latest_msg_id = ""
  clean_to = str(rd.get("to") or "").strip()
  clean_cc = str(rd.get("cc") or "").strip()
  subj = str(rd.get("subject") or "").strip()

  if tid:
    t_res = subprocess.run(
        [gmail_bin, "readonly", "get-thread", tid, "--json"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if t_res.returncode == 0 and t_res.stdout.strip():
      t_data = json.loads(t_res.stdout)
      msgs = (
          t_data.get("messages")
          if isinstance(t_data, Mapping)
          else (t_data if isinstance(t_data, list) else [])
      ) or []
      if msgs and isinstance(msgs[-1], Mapping):
        last_msg = msgs[-1]
        latest_msg_id = str(last_msg.get("id") or "").strip()
        hm = _extract_msg_headers(last_msg)
        if not subj:
          subj = str(hm.get("subject") or "").strip()
        # Explicitly pass both To and Cc because gmail CLI skips auto-Cc when --to strips self
        to_seen: set[str] = set()
        if not clean_to:
          reply_src = hm.get("reply-to") or hm.get("from") or ""
          clean_to, to_seen = _filter_reply_recipients([reply_src, hm.get("to") or ""])
        else:
          _, to_seen = _filter_reply_recipients([clean_to])
        if not clean_cc and hm.get("cc"):
          clean_cc, _ = _filter_reply_recipients(
              [hm.get("cc") or ""], exclude_addrs=to_seen, always_strip_me=True
          )

  with tempfile.NamedTemporaryFile(
      mode="w", encoding="utf-8", suffix=".html", delete=False
  ) as tmp_f:
    tmp_f.write(html_body)
    tmp_path = tmp_f.name

  try:
    cmd = [gmail_bin, "mutate", "create-draft"]
    if latest_msg_id:
      cmd.extend(["--message", latest_msg_id, "--all", "--include-history"])
      if clean_to:
        cmd.extend(["--to", clean_to])
      if clean_cc:
        cmd.extend(["--cc", clean_cc])
      if rd.get("subject"):
        cmd.extend(["--subject", str(rd["subject"])])
    elif tid:
      if not clean_to:
        raise ValueError(f"Cannot materialize draft on thread {tid}: missing To recipient")
      cmd.extend(["--thread", tid, "--to", clean_to, "--subject", subj or "Re: Follow-up"])
      if clean_cc:
        cmd.extend(["--cc", clean_cc])
    else:
      if not clean_to or not subj:
        raise ValueError("Standalone Gmail draft requires both To and Subject")
      cmd.extend(["--to", clean_to, "--subject", subj])
      if clean_cc:
        cmd.extend(["--cc", clean_cc])
    cmd.extend(["--html", "--body-file", tmp_path])

    c_res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    if c_res.returncode != 0:
      err_detail = c_res.stderr.strip() or c_res.stdout.strip()
      raise RuntimeError(f"gmail create-draft failed: {err_detail}")
    m_id = re.search(r"ID=([^\s]+)", c_res.stdout or "")
    new_did = m_id.group(1).strip() if m_id else ""
    if not new_did and (c_res.stdout or "").strip().startswith("{"):
      new_did = str((json.loads(c_res.stdout) or {}).get("id") or "").strip()
    if not new_did:
      raise RuntimeError(f"Could not parse draft ID from gmail output: {c_res.stdout!r}")
  finally:
    try:
      os.unlink(tmp_path)
    except OSError:
      pass

  _verify_stored_gmail_draft_clean(gmail_bin, new_did)
  d_key = store.record_materialized_reply_draft(
      work_id,
      new_did,
      thread_id=tid,
      subject=subj,
      to_hdr=clean_to,
      now=cur_now,
  )
  return {
      "draft_id": new_did,
      "draft_key": d_key,
      "url": f"https://mail.google.com/mail/u/0/#drafts/{new_did}",
      "reused": False,
  }


CORRESPONDENCE_PROJECT_NAME = "Reviews of e-mails, meetings and such"
CODE_PRIMARY_KINDS = frozenset({
    "gerrit",
    "github_pr",
    "piper",
    "buganizer",
    "github_issue",
})
# Ordered most-specific first so niche repos/APIs match before broad SkParagraph/Skia fallbacks
_CODE_PROJECT_RULES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("Typescript WebParagraph", (), ("typescript", "ts paragraph", "ts webparagraph")),
    ("WebParagraph", (), ("webparagraph", "web_paragraph", "web_ui")),
    ("TextClusters API", (), ("textclusters", "gettextclusters", "text_clusters")),
    ("Chromium", ("gerrit:chromium:",), ("chromium", "blink")),
    ("SkGlyphID to 32 bits", (), ("skglyphid", "32-bit glyph")),
    ("Lazy Android", (), ("lazy android", "ndk font", "fontmgr_android")),
    (
        "SkParagraph",
        ("github_pr:flutter/", "github_issue:flutter/"),
        ("skparagraph", "skunicode", "skshaper", "textlayout"),
    ),
)


def _load_active_projects(
    projects_dir: str | None = None,
) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
  """Reads projects_dir JSON files and returns (by_token, by_name) for active product projects."""
  target_dir = projects_dir or DEFAULT_PROJECTS_DIR
  by_token: dict[str, tuple[str, str]] = {}
  by_name: dict[str, str] = {}
  for pfile in sorted(glob.glob(os.path.join(target_dir, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
    except (OSError, ValueError):
      continue
    # Never route newly launched chats into archived projects such as SkoreText
    if not isinstance(pdata, Mapping) or bool(pdata.get("archived")):
      continue
    pid = str(pdata.get("id") or "").strip()
    raw_name = str(pdata.get("name") or "").strip()
    clean_name = cl.strip_activity_badge(cl.clean_base_title(raw_name)).strip()
    if not pid or not clean_name:
      continue
    # Exclude meta/config projects (e.g. My Work) so product chats stay visible on Workbench
    if work_store.is_config_project(pid) or work_store.is_config_project(clean_name):
      continue
    pair = (pid, clean_name)
    by_token[pid.lower()] = pair
    if len(pid) >= 8:
      by_token[pid[:8].lower()] = pair
    by_token[clean_name.lower()] = pair
    by_name[clean_name] = pid
  return by_token, by_name


def resolve_work_chat_project(
    card: Mapping[str, Any],
    *,
    projects_dir: str | None = None,
    loaded_projects: tuple[dict[str, tuple[str, str]], dict[str, str]] | None = None,
) -> tuple[str, str]:
  """Resolves (project_id, project_name), routing docs to correspondence and CLs to active repos."""
  by_token, by_name = loaded_projects or _load_active_projects(projects_dir=projects_dir)
  raw_proj = cl.strip_activity_badge(str(card.get("project_id") or "")).strip().lower()
  # Honor any explicit non-archived non-config code project already stamped on the card
  if raw_proj and raw_proj in by_token:
    cand_pid, cand_name = by_token[raw_proj]
    if cand_name != CORRESPONDENCE_PROJECT_NAME:
      return cand_pid, cand_name

  arts = [a for a in (card.get("artifacts") or []) if isinstance(a, Mapping)]
  code_primaries = [
      a
      for a in arts
      if str(a.get("role") or "primary") == "primary"
      and str(a.get("kind") or "") in CODE_PRIMARY_KINDS
  ]
  if code_primaries:
    parts = [str(card.get("title") or "")]
    for a in code_primaries:
      parts.append(str(a.get("artifact_key") or ""))
      parts.append(str(a.get("title") or ""))
      st = a.get("state") if isinstance(a.get("state"), Mapping) else {}
      parts.append(str(st.get("subject") or ""))
      parts.append(str(st.get("project") or ""))
    haystack = " ".join(parts).lower()
    akeys = [str(a.get("artifact_key") or "") for a in code_primaries]
    for proj_name, key_prefixes, keywords in _CODE_PROJECT_RULES:
      has_prefix = bool(key_prefixes) and any(k.startswith(key_prefixes) for k in akeys)
      if (has_prefix or any(kw in haystack for kw in keywords)) and proj_name in by_name:
        return by_name[proj_name], proj_name
    # Default remaining Skia/Buganizer/Piper code cards (including [SkoreText] CLs) to Skia
    if "Skia" in by_name:
      return by_name["Skia"], "Skia"

  # Route Google Doc, email, and meeting correspondence cards to the dedicated reviews project
  if CORRESPONDENCE_PROJECT_NAME in by_name:
    return by_name[CORRESPONDENCE_PROJECT_NAME], CORRESPONDENCE_PROJECT_NAME
  for fallback_name, fallback_pid in by_name.items():
    return fallback_pid, fallback_name
  return "outside-of-project", ""


def open_chat_in_jetski(
    conv_id: str,
    *,
    timeout_sec: float = 1.8,
    req_file: str | None = None,
    annotations_dir: str | None = None,
) -> dict[str, Any]:
  """Writes jetski-open-chat.json for conv_id and polls for tab ack within bounded timeout."""
  cid = str(conv_id or "").strip()
  if not is_valid_conversation_id(cid):
    return {"ok": False, "error": "Invalid conversation_id"}
  try:
    to_val = float(timeout_sec)
  except (TypeError, ValueError):
    to_val = 0.0
  # Clamp non-finite and oversized timeouts so HTTP worker loops provably terminate
  if not math.isfinite(to_val) or to_val <= 0.0:
    to_val = 0.0
  else:
    to_val = min(to_val, MAX_OPEN_CHAT_TIMEOUT_SEC)

  target_req = req_file or OPEN_CHAT_REQ_FILE
  target_ann_dir = annotations_dir or ANNOTATIONS_DIR
  ann_file = os.path.join(target_ann_dir, f"{cid}.pbtxt")
  before_mtime = 0.0
  before_txt = ""
  # Snapshot prior .pbtxt state so Jetski's navigation ack can be detected by mtime or content diff
  if os.path.isfile(ann_file):
    try:
      before_mtime = os.path.getmtime(ann_file)
      with open(ann_file, "r", encoding="utf-8", errors="ignore") as f:
        before_txt = f.read()
    except OSError:
      pass

  req_ts_ms = int(time.time() * 1000)
  payload = {
      "requestId": f"req-{req_ts_ms}-{cid[:8]}",
      "cascadeId": cid,
      "requestedAt": req_ts_ms,
  }
  reused = False
  wrote_req = True
  try:
    os.makedirs(os.path.dirname(target_req), exist_ok=True)
    tmp_path = f"{target_req}.tmp.{os.getpid()}"
    with open(tmp_path, "w", encoding="utf-8") as f:
      json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp_path, target_req)
  except OSError:
    wrote_req = False

  # Poll briefly for an open Jetski Web tab to consume the IPC file before standalone popup fallback
  if wrote_req and to_val > 0.0:
    deadline = time.time() + to_val
    while time.time() < deadline:
      time.sleep(0.05)
      if os.path.isfile(ann_file):
        try:
          cur_mtime = os.path.getmtime(ann_file)
          with open(ann_file, "r", encoding="utf-8", errors="ignore") as f:
            cur_txt = f.read()
          if cur_mtime > before_mtime or cur_txt != before_txt:
            reused = True
            break
        except OSError:
          pass
  return {
      "ok": True,
      "conversation_id": cid,
      "chat_url": f"{jetski_box_url()}/c/{cid}",
      "reused_tab": reused,
  }


def _sanitize_non_config_snippet(text: str) -> str:
  """Replaces _CONFIG_TEXT_PATTERNS matches in text so product chats never trip is_config_text."""
  out = str(text or "")
  for rx in work_store._CONFIG_TEXT_PATTERNS:
    out = rx.sub("[tooling]", out)
  return out


def _short_entity_prefix(artifact_key: str) -> str:
  """Returns a title-safe short ID prefix for artifact_key without cl/<5-8 digits> collisions."""
  akey = str(artifact_key or "")
  if akey.startswith("gerrit:chromium:"):
    return f"crrev/c/{akey.split(':')[-1]}"
  if akey.startswith("gerrit:"):
    return f"Gerrit {akey.split(':')[-1]}"
  if akey.startswith("github_pr:"):
    return f"PR #{akey.split(':')[-1]}"
  if akey.startswith("github_issue:flutter/"):
    return f"flutter#{akey.split(':')[-1]}"
  if akey.startswith("github_issue:"):
    return f"Issue #{akey.split(':')[-1]}"
  if akey.startswith("buganizer:"):
    return f"b/{akey.split(':')[-1]}"
  if akey.startswith("piper:"):
    return f"CL {akey.split(':')[-1]}"
  return ""


def _truncate_words(text: str, max_len: int = 38) -> str:
  """Truncates whitespace-normalized text at a word boundary within max_len chars."""
  collapsed = " ".join(text.split()).strip(" -:/·")
  if len(collapsed) <= max_len:
    return collapsed
  cut = collapsed[:max_len].rsplit(" ", 1)[0].strip(" -:/·")
  return cut if len(cut) >= 10 else collapsed[:max_len].strip(" -:/·")


_RE_BARE_TOPIC_ENTITY = re.compile(
    r"\b(?:crrev/c/\d+|flutter#\d+|b/\d+|gerrit\s+\d+|pr\s+#?\d+|issue\s+#?\d+|cl\s+\d+)\b",
    re.IGNORECASE,
)


def _build_work_chat_topic(card: Mapping[str, Any]) -> str:
  """Derives an English chat topic (<= 38 chars) from card that passes validate_chat_topic."""
  raw_title = work_store.clean_work_title(str(card.get("title") or ""))
  # Strip trailing counterparty suffixes when the base document/CL subject already has >= 2 words
  if " — " in raw_title:
    head_part = raw_title.split(" — ", 1)[0].strip()
    if len(head_part.split()) >= 2:
      raw_title = head_part
  ascii_title = (
      raw_title.replace("—", "-")
      .replace("–", "-")
      .replace("“", '"')
      .replace("”", '"')
      .replace("’", "'")
  )
  # Replace bare cl/<5-8 digits> with Gerrit <N> per clickable_links / title rules
  ascii_title = re.sub(r"\bcl/(\d{5,8})\b", r"Gerrit \1", ascii_title, flags=re.IGNORECASE)
  ascii_title = ascii_title.encode("ascii", "ignore").decode("ascii")
  ascii_title = _sanitize_non_config_snippet(ascii_title)

  arts = [a for a in (card.get("artifacts") or []) if isinstance(a, Mapping)]
  prim_prefix = ""
  prim_num = ""
  has_gdoc = False
  for a in arts:
    if str(a.get("kind") or "") == "gdoc":
      has_gdoc = True
    if not prim_prefix and str(a.get("role") or "primary") == "primary":
      akey = str(a.get("artifact_key") or "")
      prim_prefix = _short_entity_prefix(akey)
      num_m = re.search(r"(\d+)$", akey)
      prim_num = num_m.group(1) if num_m else ""

  # Require non-ID subject words before accepting raw title so bare flutter#<N> falls back cleanly
  stripped_ids = _RE_BARE_TOPIC_ENTITY.sub(" ", ascii_title)
  has_subject_words = bool(re.search(r"[A-Za-z]{2,}", stripped_ids))
  has_prim_num = bool(prim_num and re.search(rf"\b{re.escape(prim_num)}\b", ascii_title))

  # Prefer entity-prefixed subject, then ASCII title, then deterministic multi-word fallbacks
  candidates: list[str] = []
  if prim_prefix and has_subject_words and not has_prim_num:
    stripped_brackets = re.sub(r"^\[[^\]]+\]\s*", "", ascii_title).strip()
    candidates.append(_truncate_words(f"{prim_prefix} {stripped_brackets}"))
  if ascii_title and has_subject_words:
    candidates.append(_truncate_words(ascii_title))
  if prim_prefix:
    candidates.append(_truncate_words(f"{prim_prefix} review follow up"))
  if has_gdoc:
    candidates.append("Google Doc sync action items")
  candidates.append("Card action items follow up")

  for cand in candidates:
    if not cand or work_store.is_config_text(cand):
      continue
    try:
      return cl.validate_chat_topic(cand)
    except Exception:
      continue
  return "Google Doc sync action items"


def _build_work_chat_prompt(card: Mapping[str, Any]) -> str:
  """Builds a sanitized Russian first-turn prompt grounding the agent on card's docs and threads."""
  card_title = _sanitize_non_config_snippet(
      work_store.clean_work_title(str(card.get("title") or "Рабочая карточка"))
  )
  arts = [a for a in (card.get("artifacts") or []) if isinstance(a, Mapping)]
  gdocs = [
      a for a in arts if str(a.get("kind") or "") == "gdoc" and a.get("role") != "context_only"
  ]
  primaries = [
      a
      for a in arts
      if str(a.get("role") or "primary") == "primary" and str(a.get("kind") or "") != "gdoc"
  ]
  threads = [
      a
      for a in arts
      if str(a.get("kind") or "") in ("gdoc_thread", "gmail_thread", "gchat_thread")
      and a.get("role") != "context_only"
  ]
  open_obs = [
      o
      for o in (card.get("obligations") or [])
      if isinstance(o, Mapping) and str(o.get("status") or "") != "done"
  ]

  lines: list[str] = [f'Открой рабочую задачу «{card_title}» и помоги разобрать текущие пункты.']
  # Instruct Drive MCP reading with includeComments: true whenever the card owns a Google Doc
  if gdocs:
    lines.append("")
    lines.append("Документы карточки:")
    for gd in gdocs:
      doc_id = str(gd.get("artifact_key") or "").split(":", 1)[-1]
      doc_url = str(gd.get("url") or f"https://docs.google.com/document/d/{doc_id}/edit")
      doc_title = _sanitize_non_config_snippet(str(gd.get("title") or doc_id))
      lines.append(f"- {doc_title}: {doc_url} (fileId: `{doc_id}`)")
    lines.append(
        "Сначала прочитай содержимое документа и все открытые комментарии через "
        "`google_drive_google_drive` `read_file_content` с `includeComments: true`."
    )
  if primaries:
    lines.append("")
    lines.append("Основные артефакты:")
    for pr in primaries:
      p_title = _sanitize_non_config_snippet(str(pr.get("title") or pr.get("artifact_key") or ""))
      p_url = str(pr.get("url") or "")
      lines.append(f"- {p_title}" + (f": {p_url}" if p_url else ""))

  # Include recent correspondence thread URLs and unquoted snippets so context is self-contained
  if threads:
    lines.append("")
    lines.append("Активные треды и комментарии:")
    for th in threads[:5]:
      st = th.get("state") if isinstance(th.get("state"), Mapping) else {}
      cp = _sanitize_non_config_snippet(str(st.get("counterparty") or ""))
      disco = str(st.get("disco_id") or "")
      th_url = str(th.get("url") or "")
      raw_snip = str(st.get("last_unquoted_text") or st.get("last_snippet") or "").strip()
      clean_snip = _sanitize_non_config_snippet(" ".join(raw_snip.split())[:360])
      hdr = cp or _sanitize_non_config_snippet(str(th.get("title") or th.get("artifact_key") or ""))
      meta_bits = [b for b in (disco, th_url) if b]
      hdr_line = f"- {hdr}" + (f" ({' · '.join(meta_bits)})" if meta_bits else "")
      lines.append(hdr_line)
      if clean_snip:
        lines.append(f"  Цитата: {clean_snip}")

  if open_obs:
    lines.append("")
    lines.append("Открытые обязательства:")
    for ob in open_obs[:5]:
      desc = _sanitize_non_config_snippet(str(ob.get("description") or ""))
      if desc:
        lines.append(f"- {desc}")

  # Enforce read-only safety on turn 1 so comments/docs are never mutated before user confirmation
  lines.append("")
  lines.append(
      "Коротко выпиши по-русски реальные задачи, вопросы и action items на мне (особенно свежие "
      "комментарии и сегодняшние пункты), ничего не публикуй, не резолвь и не редактируй без "
      "моего подтверждения, и жди моих указаний."
  )
  return "\n".join(lines)


def _hydrate_stored_work_card(
    store: work_store.WorkStore, w_row: Mapping[str, Any]
) -> dict[str, Any]:
  """Hydrates artifacts and obligations from SQLite for archived or promoted Work rows."""
  wid = str(w_row.get("work_id") or "")
  roles_map = store.get_work_artifact_roles(wid)
  arts: list[dict[str, Any]] = []
  for akey, role in roles_map.items():
    art_row = store.get_artifact(akey)
    if art_row:
      arts.append({**art_row, "role": role})
  with store._lock, store._connect() as conn:
    ob_rows = conn.execute("SELECT * FROM obligations WHERE work_id = ?", (wid,)).fetchall()
  obs = [dict(r) for r in ob_rows]
  return {**dict(w_row), "artifacts": arts, "obligations": obs, "is_virtual": False}


def _resolve_card_for_chat(
    store: work_store.WorkStore,
    raw_wid: str,
    cl_state_path: str,
    cur_now: float,
) -> dict[str, Any]:
  """Resolves active, already-promoted virtual, or archived Work cards for chat launching."""
  proj = store.build_dashboard_projection(
      cl_watcher_state=load_cl_watcher_snapshot(cl_state_path), now=cur_now
  )
  works = proj.get("works") or []
  card = next((w for w in works if str(w.get("work_id")) == raw_wid), None)
  if card is not None:
    return card

  target_wid = raw_wid
  # Map already-promoted virt-<key> IDs to their persisted Work when projection suppressed virt-
  if raw_wid.startswith("virt-"):
    prim_key = work_store.canonicalize_artifact_key(raw_wid[len("virt-") :])
    promoted = (
        store.find_work_by_artifact(prim_key, roles=("primary", "channel")) if prim_key else None
    )
    if promoted:
      hit = next((w for w in works if str(w.get("work_id")) == promoted), None)
      if hit is not None:
        return hit
      target_wid = promoted

  if not target_wid.startswith("virt-"):
    fallback_w = next(
        (w for w in store.list_works(include_archived=True) if str(w.get("work_id")) == target_wid),
        None,
    )
    if fallback_w is not None:
      return _hydrate_stored_work_card(store, fallback_w)
  raise ValueError(f"Unknown work_id: {raw_wid}")


def _materialize_virtual_card(
    store: work_store.WorkStore,
    card: Mapping[str, Any],
    project_name: str,
    cur_now: float,
) -> dict[str, Any]:
  """Persists a virtual tracker card into store and returns the hydrated materialized card dict."""
  arts = [a for a in (card.get("artifacts") or []) if isinstance(a, Mapping)]
  raw_wid = str(card.get("work_id") or "")
  prim_key = work_store.canonicalize_artifact_key(
      raw_wid[len("virt-") :] if raw_wid.startswith("virt-") else ""
  )
  # Reuse an existing persisted Work when the primary tracker artifact was already promoted
  persisted_wid = store.find_work_by_artifact(prim_key, roles=("primary",)) if prim_key else None
  if not persisted_wid:
    persisted_wid = store.create_work(
        str(card.get("title") or prim_key or "Work"),
        project_id=project_name,
        now=cur_now,
        # Materializing runs only from her explicit + Chat click, so the Work is hers
        owner="user",
    )
  for a in arts:
    akey = work_store.canonicalize_artifact_key(str(a.get("artifact_key") or ""))
    if not akey:
      continue
    kind = str(a.get("kind") or work_store.artifact_kind_from_key(akey))
    role = str(a.get("role") or "primary")
    st = a.get("state") if isinstance(a.get("state"), Mapping) else {}
    store.upsert_artifact(
        akey,
        kind=kind,
        title=str(a.get("title") or akey),
        url=str(a.get("url") or work_store._default_artifact_url(akey)),
        state=st,
        now=cur_now,
    )
    store.link_artifact(
        persisted_wid,
        akey,
        role=role,
        source="manual" if role == "primary" else "auto",
        now=cur_now,
    )
  # Reload stored links so any concurrently linked chat_chain is visible to reuse checks
  stored_row = next(
      (
          w
          for w in store.list_works(include_archived=True)
          if str(w.get("work_id")) == persisted_wid
      ),
      {"work_id": persisted_wid, "title": card.get("title") or "", "project_id": project_name},
  )
  return _hydrate_stored_work_card(store, stored_row)


def start_chat_for_work(
    store: work_store.WorkStore,
    work_id: str,
    *,
    cl_state_path: str = DEFAULT_CL_WATCHER_STATE,
    projects_dir: str | None = None,
    force_new: bool = False,
    now: float | None = None,
) -> dict[str, Any]:
  """Reuses an active linked chat or spawns and links a visible project-routed chat for work_id."""
  cur_now = time.time() if now is None else float(now)
  raw_wid = str(work_id or "").strip()
  if not raw_wid:
    raise ValueError("work_id is required")

  card = _resolve_card_for_chat(store, raw_wid, cl_state_path, cur_now)
  loaded_projects = _load_active_projects(projects_dir=projects_dir)
  project_id, project_name = resolve_work_chat_project(card, loaded_projects=loaded_projects)
  # Materialize only when the resolved card itself is still an unpersisted virtual projection
  if str(card.get("work_id") or "").startswith("virt-") or bool(card.get("is_virtual")):
    card = _materialize_virtual_card(store, card, project_name, cur_now)
  resolved_wid = str(card["work_id"])

  # Reuse an existing non-archived channel chat unless the caller explicitly asked for a new thread
  if not force_new:
    for art in card.get("artifacts") or []:
      if not isinstance(art, Mapping):
        continue
      if str(art.get("kind") or "") != "chat_chain" or art.get("role") == "context_only":
        continue
      st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
      key_cid = str(art.get("artifact_key") or "").split(":", 1)[-1]
      cid = str(st.get("cascade_id") or st.get("root_cid") or key_cid).strip()
      if cid and not cl.is_archived(cid):
        return {
            "ok": True,
            "started": False,
            "already_existed": True,
            "work_id": resolved_wid,
            "conversation_id": cid,
            "title": str(art.get("title") or card.get("title") or ""),
            "project_id": project_id,
            "project_name": project_name,
            "chat_url": f"{jetski_box_url()}/c/{cid}",
        }

  topic = _build_work_chat_topic(card)
  full_title = f"[{cl.get_current_time_str()}] ⦿ {topic}"
  prompt = _build_work_chat_prompt(card)
  model_info = cl.resolve_handoff_model("", tier="pro", explicit="", project_id=project_id)
  new_cid = cl.start_conversation_exact(
      str(model_info["enum"]), full_title, prompt, project_id
  )
  _upsert_and_link_chat_chain(
      store,
      resolved_wid,
      new_cid,
      new_cid,
      topic,
      prompt,
      cur_now,
      cur_now,
      source="manual",
      link_reason="manual",
      link_detail="Чат открыт с карточки",
  )

  # Stamp the resolved non-archived project onto cards with empty/archived/config project_id
  by_token, _ = loaded_projects
  cur_proj = cl.strip_activity_badge(str(card.get("project_id") or "")).strip().lower()
  if project_name and (not cur_proj or cur_proj not in by_token):
    store.create_work(
        str(card.get("title") or topic),
        project_id=project_name,
        roadmap_item_key=str(card.get("roadmap_item_key") or ""),
        work_id=resolved_wid,
        now=cur_now,
    )

  # Force immediate chat classification refresh so build_dashboard_projection includes new_cid
  work_store._CHAT_CLASS_CACHE["ts"] = 0.0
  return {
      "ok": True,
      "started": True,
      "already_existed": False,
      "work_id": resolved_wid,
      "conversation_id": new_cid,
      "title": full_title,
      "project_id": project_id,
      "project_name": project_name,
      "chat_url": f"{jetski_box_url()}/c/{new_cid}",
  }


_LANE_LABELS_RU = {
    "needs-you": "Needs You",
    "on-desk": "On Desk",
    "waiting": "Waiting",
    "blocked": "Blocked",
    "done": "Done",
}


def _load_cl_watcher_module() -> Any:
  """Lazily imports scripts/cl_watcher.py so normal Workbench startup stays lightweight."""
  scripts_dir = os.path.join(
      os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"
  )
  if scripts_dir not in sys.path:
    sys.path.insert(0, scripts_dir)
  import cl_watcher as cw  # pylint: disable=g-import-not-at-top

  return cw


def accept_work_card(
    store: work_store.WorkStore,
    work_id: str,
    *,
    cl_state_path: str = DEFAULT_CL_WATCHER_STATE,
    projects_dir: str | None = None,
    now: float | None = None,
) -> str:
  """Promotes an unowned persisted or virtual card to owner=user and returns its persisted ID."""
  cur_now = time.time() if now is None else float(now)
  raw_wid = str(work_id or "").strip()
  if not raw_wid:
    raise ValueError("work_id is required")
  if raw_wid.startswith("virt-"):
    card = _resolve_card_for_chat(store, raw_wid, cl_state_path, cur_now)
    if str(card.get("work_id") or "").startswith("virt-") or bool(card.get("is_virtual")):
      loaded_projects = _load_active_projects(projects_dir=projects_dir)
      _, project_name = resolve_work_chat_project(card, loaded_projects=loaded_projects)
      mat = _materialize_virtual_card(store, card, project_name, cur_now)
      resolved_wid = str(mat["work_id"])
    else:
      resolved_wid = str(card["work_id"])
    store.set_work_owner(resolved_wid, "user", now=cur_now)
    return resolved_wid
  # set_work_owner silently no-ops on unknown rows, so reject stale IDs before claiming success
  if not any(str(w.get("work_id")) == raw_wid for w in store.list_works(include_archived=True)):
    raise ValueError(f"Unknown work_id: {raw_wid}")
  store.set_work_owner(raw_wid, "user", now=cur_now)
  return raw_wid


def _fetch_live_buganizer_records(bug_ids: list[str]) -> dict[str, dict[str, Any]]:
  """Fetches live Buganizer records for numeric IDs via remote_mcp with batch-get CLI fallback."""
  clean_ids = [bid for bid in dict.fromkeys(bug_ids) if bid.isdigit()]
  if not clean_ids:
    return {}
  cw = _load_cl_watcher_module()
  found: dict[str, dict[str, Any]] = {}
  query = " OR ".join(f"id:{bid}" for bid in clean_ids)
  mcp_args = {"query": query, "maxResults": max(20, len(clean_ids) * 2), "verbose": True}
  outs = cw.call_buganizer_mcp_batch(
      [("get_bugs_with_external", mcp_args)],
      timeout_sec=12.0,
  )
  for rec in cw.parse_buganizer_blocks(outs[0] if outs else ""):
    bid = str(rec.get("id") or "").strip()
    if bid:
      found[f"buganizer:{bid}"] = dict(rec)

  missing = [bid for bid in clean_ids if f"buganizer:{bid}" not in found]
  if missing and os.path.isfile(cw.ISSUES_CLI_BIN):
    try:
      res = subprocess.run(
          [cw.ISSUES_CLI_BIN, "readonly", "batch-get", f"--issue_ids={','.join(missing)}"],
          capture_output=True,
          text=True,
          timeout=12,
          check=False,
      )
    except (OSError, subprocess.SubprocessError):
      res = None
    if res is not None and res.returncode == 0:
      # batch-get prints '[DUPLICATE of N]' while the shared parser expects upper-case status
      raw_out = re.sub(r"\[[^\]\n]*\]", lambda m: m.group(0).upper(), res.stdout or "")
      for rec in cw.parse_buganizer_blocks(raw_out):
        bid = str(rec.get("id") or "").strip()
        if bid:
          found[f"buganizer:{bid}"] = dict(rec)

  # Live refresh must show the newest human comment, so render every fetched bug once
  render_ids = [bid for bid in clean_ids if f"buganizer:{bid}" in found]
  if render_ids:
    rendered = cw.call_buganizer_mcp_batch(
        [("render_issue_with_external", {"issueId": bid}) for bid in render_ids],
        timeout_sec=12.0,
    )
    for bid, r_out in zip(render_ids, rendered or []):
      if cw._is_valid_rendered_issue_output(r_out, bid):  # pylint: disable=protected-access
        found[f"buganizer:{bid}"].update(cw.parse_rendered_issue_comments(r_out))
  return found


def _fetch_live_gerrit_records(gerrit_keys: list[str]) -> dict[str, dict[str, Any]]:
  """Polls live Gerrit detail endpoints for canonical gerrit:<host>:<num> artifact keys."""
  if not gerrit_keys:
    return {}
  cw = _load_cl_watcher_module()
  host_map = {
      "skia": cw.GERRIT_SKIA_HOST,
      "chromium": cw.GERRIT_CHROMIUM_HOST,
  }
  out: dict[str, dict[str, Any]] = {}
  for akey in dict.fromkeys(gerrit_keys):
    parts = akey.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
      continue
    pref_host = host_map.get(parts[1], parts[1])
    info = cw.fetch_gerrit_cl_status(parts[2], {}, [], preferred_host=pref_host)
    if isinstance(info, dict) and (info.get("status") or info.get("subject")):
      out[akey] = info
  return out


def _mutate_cl_watcher_state_file(
    cl_state_path: str,
    bug_updates: Mapping[str, dict[str, Any]],
    bug_removals: set[str],
    item_updates: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
  """Applies targeted bug/CL updates to cl_watcher_state.json under an exclusive file lock."""
  if not os.path.isfile(cl_state_path):
    return {"items": {}, "bugs": {}}
  if not (bug_updates or bug_removals or item_updates):
    return load_cl_watcher_snapshot(cl_state_path)

  cw = _load_cl_watcher_module()
  lock_path = f"{cl_state_path}.lock"
  os.makedirs(os.path.dirname(cl_state_path), exist_ok=True)
  with open(lock_path, "a", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
      try:
        with open(cl_state_path, "r", encoding="utf-8") as f:
          disk_state = json.load(f)
      except Exception:
        disk_state = {"items": {}, "bugs": {}}
      if not isinstance(disk_state, dict):
        disk_state = {"items": {}, "bugs": {}}
      bugs = disk_state.setdefault("bugs", {})
      items = disk_state.setdefault("items", {})
      for bkey in bug_removals:
        bugs.pop(bkey, None)
      for bkey, brec in bug_updates.items():
        if bkey not in bug_removals:
          bugs[bkey] = dict(brec)
      if item_updates and isinstance(items, dict):
        for raw_k, cur_item in list(items.items()):
          if not isinstance(cur_item, dict):
            continue
          ckey = work_store.canonicalize_artifact_key(
              str(cur_item.get("kind") or "gerrit"),
              str(cur_item.get("target_id") or raw_k),
              host=str(cur_item.get("host") or ""),
          )
          if ckey in item_updates:
            merged_item = {**cur_item, **dict(item_updates[ckey])}
            items[raw_k] = merged_item
      if isinstance(bugs, dict) and isinstance(items, dict):
        cw.link_bugs_and_cls(bugs, items)
      try:
        import entity_state  # pylint: disable=g-import-not-at-top

        entity_state.enrich_entity_states(disk_state)
      except Exception:
        pass
      tmp = f"{cl_state_path}.tmp.{os.getpid()}.{threading.get_ident()}"
      with open(tmp, "w", encoding="utf-8") as f:
        json.dump(disk_state, f, indent=2, ensure_ascii=False)
      os.replace(tmp, cl_state_path)
      return disk_state
    finally:
      fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def _describe_card_removal_reason(
    target_keys: list[str],
    store: work_store.WorkStore,
    fresh_bugs: Mapping[str, Mapping[str, Any]],
    fresh_cls: Mapping[str, Mapping[str, Any]],
) -> str:
  """Builds a short Russian explanation when a refreshed card leaves the active projection."""
  for akey in target_keys:
    b_st = fresh_bugs.get(akey) or ((store.get_artifact(akey) or {}).get("state") or {})
    if akey.startswith(("buganizer:", "github_issue:")) and isinstance(b_st, Mapping):
      status_u = str(b_st.get("status") or "").strip().upper()
      if status_u in work_store.CLOSED_BUG_STATUSES:
        return f"Баг закрыт ({status_u})"
      assignee = str(b_st.get("assignee") or "").strip()
      if assignee and not work_store.is_bug_assigned_to_me(b_st):
        return f"Переведён на {assignee}"
      if not assignee and not work_store.is_bug_assigned_to_me(b_st):
        return "Снят с тебя"
    c_st = fresh_cls.get(akey) or ((store.get_artifact(akey) or {}).get("state") or {})
    if akey.startswith(("gerrit:", "github_pr:", "piper:")) and isinstance(c_st, Mapping):
      c_status = str(c_st.get("status") or "").strip().upper()
      if c_status == "MERGED":
        return "СЛ замержен"
      if c_status in ("ABANDONED", "CLOSED"):
        return f"СЛ закрыт ({c_status})"
  return "Ушло из текущего списка"


def _resolve_refresh_targets(
    store: work_store.WorkStore,
    raw_wid: str,
    works_before: list[dict[str, Any]],
) -> tuple[str, dict[str, Any] | None, list[str]]:
  """Returns (resolved_work_id, card_before, canonical non-context artifact keys) for refresh."""
  card_before = next((w for w in works_before if str(w.get("work_id")) == raw_wid), None)
  resolved_wid = raw_wid
  prim_key = ""
  if raw_wid.startswith("virt-"):
    prim_key = work_store.canonicalize_artifact_key(raw_wid[len("virt-") :])
    if not prim_key:
      raise ValueError(f"Unknown work_id: {raw_wid}")
    if card_before is None:
      promoted = store.find_work_by_artifact(prim_key, roles=("primary", "channel"))
      if promoted:
        resolved_wid = promoted
        card_before = next((w for w in works_before if str(w.get("work_id")) == promoted), None)
  elif card_before is None:
    if not any(str(w.get("work_id")) == raw_wid for w in store.list_works(include_archived=True)):
      raise ValueError(f"Unknown work_id: {raw_wid}")

  target_keys: list[str] = [prim_key] if prim_key else []
  raw_keys: list[str] = []
  if card_before is not None:
    raw_keys += [
        str(a.get("artifact_key") or "")
        for a in card_before.get("artifacts") or []
        if isinstance(a, Mapping) and a.get("role") != "context_only"
    ]
  if not resolved_wid.startswith("virt-"):
    raw_keys += [
        k for k, r in store.get_work_artifact_roles(resolved_wid).items() if r != "context_only"
    ]
  for raw_k in raw_keys:
    ck = work_store.canonicalize_artifact_key(raw_k)
    if ck and ck not in target_keys:
      target_keys.append(ck)
  return resolved_wid, card_before, target_keys


def _seed_prev_bug(
    store: work_store.WorkStore, bkey: str, prev_bugs: Mapping[str, Any]
) -> dict[str, Any] | None:
  """Returns the cached watcher bug or, for bugs absent from it, the stored SQLite bug state."""
  prev_b = prev_bugs.get(bkey)
  if isinstance(prev_b, dict):
    return prev_b
  art = store.get_artifact(bkey) or {}
  st = art.get("state") if isinstance(art.get("state"), Mapping) else {}
  if not st:
    return None
  seed = dict(st)
  # merge_bug_record_with_prev reads the real title from prev["title"], kept as bug_title here
  seed["title"] = str(st.get("bug_title") or st.get("title") or "")
  return seed


def _apply_refreshed_bugs(
    store: work_store.WorkStore,
    cw: Any,
    cl_state: Mapping[str, Any],
    fresh_bz_raw: Mapping[str, dict[str, Any]],
    cur_now: float,
) -> tuple[dict[str, dict[str, Any]], set[str], dict[str, dict[str, Any]]]:
  """Merges live Buganizer records the same way sync_bugs_into_state does.

  Returns (bug_updates, bug_removals, merged bugs by artifact key). Links CLs before status
  classification so a bug with an open fix CL never reads as "without a CL".
  """
  prev_bugs = cl_state.get("bugs") if isinstance(cl_state.get("bugs"), Mapping) else {}
  merged: dict[str, dict[str, Any]] = {}
  for bkey, b_curr in fresh_bz_raw.items():
    prev_b = _seed_prev_bug(store, bkey, prev_bugs)
    merged_b = cw.merge_bug_record_with_prev(dict(b_curr), prev_b)
    cw._update_bug_self_assignment_state(  # pylint: disable=protected-access
        merged_b, prev_b, has_prev_source_snapshot=True, now_ts=cur_now
    )
    # Role flags come from sync query membership, which a single-bug lookup cannot recompute
    for flag in ("is_collaborator", "from_self_query"):
      if flag not in b_curr and prev_b and flag in prev_b:
        merged_b[flag] = bool(prev_b[flag])
    merged[bkey] = merged_b

  items_view = copy.deepcopy(cl_state.get("items") or {})
  if isinstance(items_view, dict):
    cw.link_bugs_and_cls(merged, items_view)

  bug_updates: dict[str, dict[str, Any]] = {}
  bug_removals: set[str] = set()
  for bkey, merged_b in merged.items():
    cw.classify_bug_status(merged_b, now_ts=cur_now)
    ckey, bkind, btitle, burl = work_store.canonicalize_watcher_bug(bkey, merged_b)
    bstate = dict(merged_b)
    bdesc = str(merged_b.get("title") or merged_b.get("summary") or "").strip()
    # Never persist redaction fallbacks as the human title of the bug
    if cw._is_placeholder_bug_title(bdesc):  # pylint: disable=protected-access
      bstate.pop("bug_title", None)
    elif not bstate.get("bug_title"):
      bstate["bug_title"] = bdesc
    store.upsert_artifact(
        ckey or bkey, kind=bkind, title=btitle, url=burl, state=bstate, now=cur_now
    )
    if cw._should_retain_bug_in_view(merged_b):  # pylint: disable=protected-access
      bug_updates[bkey] = merged_b
    else:
      bug_removals.add(bkey)
  return bug_updates, bug_removals, merged


def _apply_refreshed_cls(
    store: work_store.WorkStore,
    cw: Any,
    gerrit_keys: list[str],
    gh_pr_targets: list[tuple[str, str, str, int]],
    cur_now: float,
) -> dict[str, dict[str, Any]]:
  """Polls Gerrit CLs and GitHub PRs, attaches fresh entity_state, upserts, returns item updates.

  Live Gerrit/GitHub responses carry no entity_state, and a stale cached one would keep the
  card in its old lane, so the lane is recomputed here before the artifact is stored.
  """
  import entity_state  # pylint: disable=g-import-not-at-top

  fresh: dict[str, dict[str, Any]] = dict(_fetch_live_gerrit_records(gerrit_keys))
  for akey, owner, repo, num in gh_pr_targets:
    info = cw.fetch_github_pr_status(str(num), f"{owner}/{repo}")
    if isinstance(info, dict) and (info.get("status") or info.get("subject")):
      fresh[akey] = info
  item_updates: dict[str, dict[str, Any]] = {}
  for akey, raw_info in fresh.items():
    info = dict(raw_info)
    info["entity_state"] = entity_state.compute_item_entity_state(info, now_ts=cur_now)
    num_s = akey.split(":")[-1]
    subj = str(info.get("subject") or info.get("title") or akey)
    default_url = work_store._default_artifact_url(akey)  # pylint: disable=protected-access
    store.upsert_artifact(
        akey,
        kind="gerrit" if akey.startswith("gerrit:") else "github_pr",
        title=f"{num_s}: {subj}",
        url=str(info.get("url") or default_url),
        state=info,
        now=cur_now,
    )
    item_updates[akey] = info
  return item_updates


def _apply_refreshed_github_issue(
    store: work_store.WorkStore,
    akey: str,
    cl_state: Mapping[str, Any],
    bug_updates: dict[str, dict[str, Any]],
    bug_removals: set[str],
) -> None:
  """Mirrors a refreshed github_issue artifact into its cl_watcher gh_issue:<repo>#<n> entry."""
  parsed = _parse_gh_key(akey)
  st = (store.get_artifact(akey) or {}).get("state") or {}
  if not parsed or not isinstance(st, Mapping):
    return
  bkey = f"gh_issue:{parsed[0]}/{parsed[1]}#{parsed[2]}"
  prev_bugs = cl_state.get("bugs") if isinstance(cl_state.get("bugs"), Mapping) else {}
  prev_b = prev_bugs.get(bkey)
  status_u = str(st.get("status") or "").upper()
  if status_u in work_store.CLOSED_BUG_STATUSES:
    bug_removals.add(bkey)
  elif isinstance(prev_b, dict):
    upd = {**prev_b, "status": status_u or prev_b.get("status")}
    if "assignee" in st:
      upd["assignee"] = st["assignee"]
    if st.get("bug_title"):
      upd["title"] = st["bug_title"]
    bug_updates[bkey] = upd


def _refresh_card_gmail(
    store: work_store.WorkStore,
    target_keys: list[str],
    watcher_state_path: str,
    cur_now: float,
) -> list[Any]:
  """Force-polls the card's Gmail threads and drafts and persists them to watcher_state.

  RPCs run without the lock; afterwards the latest watcher_state is re-read under the lock and
  only the touched thread, draft, and tray entries are merged, so concurrent sync writes such
  as transcript_cursors survive. Returns the updated unanswered tray.
  """
  w_state = _read_watcher_state(watcher_state_path)
  thread_cache = dict(w_state.get("gmail_threads") or {})
  active_draft_ids = set(w_state.get("active_draft_ids") or [])
  fresh_threads: dict[str, Any] = {}
  answered_keys: set[str] = set()
  gone_dids: set[str] = set()
  for ak in target_keys:
    kind, _, ident = ak.partition(":")
    ident = ident.strip()
    if not ident:
      continue
    if kind == "gmail_thread":
      t_prov = _fetch_gmail_thread_provenance(ident, thread_cache, cur_now, force_refresh=True)
      if not t_prov.get("schema_ver"):
        continue
      fresh_threads[ident] = t_prov
      if not _upsert_refreshed_gmail_thread(store, ident, t_prov, {}, cur_now):
        answered_keys.add(ak)
    elif kind == "gmail_draft" and not _gmail_draft_still_exists(ident):
      gone_dids.add(ident)
      active_draft_ids.discard(ident)
      _reconcile_disappeared_gmail_draft(
          store, ident, False, thread_cache, {}, active_draft_ids, cur_now
      )
  if not (fresh_threads or gone_dids) or not watcher_state_path:
    return list(w_state.get("unanswered_tray") or [])

  os.makedirs(os.path.dirname(watcher_state_path), exist_ok=True)
  with open(f"{watcher_state_path}.lock", "a", encoding="utf-8") as lf:
    fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
    try:
      disk = _read_watcher_state(watcher_state_path)
      disk.setdefault("gmail_threads", {}).update(fresh_threads)
      disk["active_draft_ids"] = sorted(
          d for d in (disk.get("active_draft_ids") or []) if d not in gone_dids
      )
      disk["unanswered_tray"] = [
          u
          for u in disk.get("unanswered_tray") or []
          if not (isinstance(u, Mapping) and str(u.get("artifact_key") or "") in answered_keys)
      ]
      tmp_w = f"{watcher_state_path}.tmp.{os.getpid()}.{threading.get_ident()}"
      with open(tmp_w, "w", encoding="utf-8") as wf:
        json.dump(disk, wf, indent=2, ensure_ascii=False)
      os.replace(tmp_w, watcher_state_path)
    finally:
      fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
  return list(disk["unanswered_tray"])


def _read_watcher_state(watcher_state_path: str) -> dict[str, Any]:
  """Reads watcher_state.json, returning {} when it is missing or unreadable."""
  if not watcher_state_path or not os.path.isfile(watcher_state_path):
    return {}
  try:
    with open(watcher_state_path, "r", encoding="utf-8") as f:
      data = json.load(f)
  except (OSError, ValueError):
    return {}
  return data if isinstance(data, dict) else {}


def refresh_work_card(
    store: work_store.WorkStore,
    work_id: str,
    *,
    cl_state_path: str = DEFAULT_CL_WATCHER_STATE,
    watcher_state_path: str = DEFAULT_WATCHER_STATE,
    now: float | None = None,
) -> dict[str, Any]:
  """Polls live upstream trackers ONLY for work_id's artifacts and returns the updated card.

  Phase order matters: Gmail threads refresh before GitHub nodes so a closed PR/Issue can
  resolve its mirror thread, and bugs link to CLs before their lane is classified.
  """
  cur_now = time.time() if now is None else float(now)
  raw_wid = str(work_id or "").strip()
  if not raw_wid:
    raise ValueError("work_id is required")

  unanswered = _read_watcher_state(watcher_state_path).get("unanswered_tray") or []
  cl_state = load_cl_watcher_snapshot(cl_state_path)
  proj_before = store.build_dashboard_projection(
      cl_watcher_state=cl_state, unanswered_threads=unanswered, now=cur_now
  )
  resolved_wid, card_before, target_keys = _resolve_refresh_targets(
      store, raw_wid, proj_before.get("works") or []
  )

  bz_ids = [
      ak.split(":", 1)[1]
      for ak in target_keys
      if ak.startswith("buganizer:") and ak.split(":", 1)[1].isdigit()
  ]
  gerrit_keys = [ak for ak in target_keys if ak.startswith("gerrit:")]
  gh_targets = [
      (ak, *parsed)
      for ak in target_keys
      if ak.startswith(("github_pr:", "github_issue:")) and (parsed := _parse_gh_key(ak))
  ]
  gh_pr_targets = [t for t in gh_targets if t[0].startswith("github_pr:")]
  gh_gmail_keys: dict[str, set[str]] = {}
  for ak in target_keys:
    if ak.startswith("gmail_thread:"):
      st = (store.get_artifact(ak) or {}).get("state") or {}
      gh_t = str(st.get("gh_target_key") or "")
      if gh_t.startswith(("github_pr:", "github_issue:")):
        gh_gmail_keys.setdefault(gh_t, set()).add(ak)

  fresh_bz_raw = _fetch_live_buganizer_records(bz_ids) if bz_ids else {}
  fresh_gh_nodes = _fetch_live_github_nodes(gh_targets) if gh_targets else {}
  cw = _load_cl_watcher_module() if (fresh_bz_raw or gerrit_keys or gh_pr_targets) else None

  bug_updates: dict[str, dict[str, Any]] = {}
  bug_removals: set[str] = set()
  merged_bugs: dict[str, dict[str, Any]] = {}
  if fresh_bz_raw:
    bug_updates, bug_removals, merged_bugs = _apply_refreshed_bugs(
        store, cw, cl_state, fresh_bz_raw, cur_now
    )

  unanswered = _refresh_card_gmail(store, target_keys, watcher_state_path, cur_now)

  for akey, node in fresh_gh_nodes.items():
    _apply_github_node_to_store(store, akey, node, gh_gmail_keys.get(akey, set()), cur_now)
    if akey.startswith("github_issue:"):
      _apply_refreshed_github_issue(store, akey, cl_state, bug_updates, bug_removals)
  item_updates = (
      _apply_refreshed_cls(store, cw, gerrit_keys, gh_pr_targets, cur_now) if cw else {}
  )

  updated_cl_state = _mutate_cl_watcher_state_file(
      cl_state_path, bug_updates, bug_removals, item_updates
  )
  if not resolved_wid.startswith("virt-"):
    store.reconcile_correspondence_obligations(resolved_wid, now=cur_now)

  proj_after = store.build_dashboard_projection(
      cl_watcher_state=updated_cl_state, unanswered_threads=unanswered, now=cur_now
  )
  card_after = next(
      (
          w
          for w in proj_after.get("works") or []
          if str(w.get("work_id")) in (raw_wid, resolved_wid)
      ),
      None,
  )
  prev_lane = str((card_before or {}).get("lane") or "")
  removed_reason = ""
  lane_moved_reason = ""
  if card_after is None:
    removed_reason = _describe_card_removal_reason(target_keys, store, merged_bugs, item_updates)
  else:
    next_lane = str(card_after.get("lane") or "")
    if prev_lane and next_lane and next_lane != prev_lane:
      lane_moved_reason = f"Перемещено в {_LANE_LABELS_RU.get(next_lane, next_lane)}"

  return {
      "ok": True,
      "work_id": raw_wid,
      "resolved_work_id": str((card_after or {}).get("work_id") or resolved_wid),
      "previous_lane": prev_lane,
      "card": card_after,
      "removed_reason": removed_reason,
      "lane_moved_reason": lane_moved_reason,
      "refreshed_keys": target_keys,
      "projection": proj_after,
  }
