#!/usr/bin/env python3
"""Chat lifecycle helper for Antigravity and Jetski.

Manages conversation titles, status markers (Scheme Γ), and annotation synchronization.
Synchronizes both `~/.gemini/{jetski,antigravity}/annotations/<id>.pbtxt` (the UI source of truth)
and `conversation_summaries.db` (the SQLite metadata cache).
"""

from datetime import datetime
import glob
import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

DEFAULT_TIMEZONE = os.environ.get("JETSKI_TIMEZONE", "America/New_York")
APP_DATA_DIR_CANDIDATES = ("~/.gemini/jetski", "~/.gemini/antigravity")
SUMMARY_DB_NAME = "conversation_summaries.db"
ANNOTATIONS_DIR_NAME = "annotations"

# Lifecycle marker tokens
MARKER_START = "▸"       # In-progress branch root
MARKER_STEP = "✓"        # In-progress completed step
MARKER_ACTIVE = "⦿"      # In-progress current active focal point
MARKER_CLOSED_START = "«"  # Finalized branch root
MARKER_CLOSED_STEP = "‹✓›" # Finalized intermediate step
MARKER_CLOSED_END = "»"    # Finalized branch tail
MARKER_SINGLE_CLOSED = "«»" # Finalized single-chat task

# Regex matching any existing lifecycle prefix: [HH:MM] [marker] Topic
TITLE_CLEANUP_RE = re.compile(
    r"^(?:\[\d{2}:\d{2}(?:\s+(?:продолжение|continue))?\]\s*(?:[▸✓⦿«»]|‹✓›|«»|\s*)*)"
    r"|^(?:(?:продолжение|continue)(?:\s+\d{2}:\d{2})?\s*:\s*)+",
    re.IGNORECASE,
)
TRAILING_CLEANUP_RE = re.compile(
    r"\s*\((?:продолжение|continue)\s+(?:\d{4}-\d{2}-\d{2}\s+)?\d{2}:\d{2}\)\s*$",
    re.IGNORECASE,
)
TIME_PREFIX_RE = re.compile(r"^\[(\d{2}:\d{2})\]")


def get_current_time_str() -> str:
  try:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz).strftime("%H:%M")
  except Exception:
    return datetime.now().strftime("%H:%M")


def extract_time_prefix(title: str, fallback_time: str = None) -> str:
  if title:
    m = TIME_PREFIX_RE.match(title.strip())
    if m:
      return m.group(1)
  return fallback_time or get_current_time_str()


def clean_base_title(title: str) -> str:
  if not title:
    return ""
  s = TITLE_CLEANUP_RE.sub("", title).strip()
  s = TRAILING_CLEANUP_RE.sub("", s).strip()
  return s


def find_app_data_dirs() -> list[str]:
  res = []
  env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
  if env_dir:
    exp = os.path.abspath(os.path.expanduser(env_dir))
    if os.path.isdir(exp):
      res.append(exp)
  for c in APP_DATA_DIR_CANDIDATES:
    exp = os.path.abspath(os.path.expanduser(c))
    if os.path.isdir(exp) and exp not in res:
      res.append(exp)
  return res


def get_ls_csrf_token(ls_address: str) -> str:
  """Gets the CSRF token from environment or extracts it from the running Hub server."""
  token = os.environ.get("ANTIGRAVITY_CSRF_TOKEN")
  if token:
    return token
  try:
    url = f"http://{ls_address}/" if not ls_address.startswith(("http://", "https://")) else ls_address
    req = urllib.request.Request(url, headers={"User-Agent": "chat_lifecycle"})
    with urllib.request.urlopen(req, timeout=1.0) as resp:
      html = resp.read().decode("utf-8", errors="ignore")
      m = re.search(r'"csrfToken":\s*"([^"]+)"', html)
      if m:
        return m.group(1)
  except Exception:
    pass
  return ""


def update_conversation_title_rpc(conv_id: str, new_title: str) -> bool:
  """Notifies running Language Server daemon via Connect RPC to update annotations.

  This pushes updates immediately to active Web UI streaming subscribers (sidebar),
  avoiding stale in-memory cache until manual chat selection.
  """
  ls_address = os.environ.get("ANTIGRAVITY_LS_ADDRESS", "localhost:5387")
  csrf_token = get_ls_csrf_token(ls_address)
  if not csrf_token:
    return False

  if not ls_address.startswith(("http://", "https://")):
    url = f"http://{ls_address}/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"
  else:
    url = f"{ls_address}/exa.language_server_pb.LanguageServerService/UpdateConversationAnnotations"

  payload = json.dumps({
      "cascade_id": conv_id,
      "annotations": {"title": new_title},
      "merge_annotations": True,
  }).encode("utf-8")

  req = urllib.request.Request(
      url,
      data=payload,
      headers={
          "Content-Type": "application/json",
          "x-codeium-csrf-token": csrf_token,
      },
      method="POST",
  )
  try:
    with urllib.request.urlopen(req, timeout=2.0) as resp:
      return resp.status == 200
  except Exception:
    return False


def update_conversation_title(conv_id: str, new_title: str) -> bool:
  """Updates conversation title via Language Server RPC and persists to disk."""
  if not conv_id or not new_title:
    return False

  # 1. Update running Language Server via RPC (pushes live update to UI stream)
  rpc_ok = update_conversation_title_rpc(conv_id, new_title)

  # 2. Synchronize on-disk files & DB as fallback / durability guarantee
  disk_ok = False
  for app_dir in find_app_data_dirs():
    # 2a. Update annotations .pbtxt
    ann_dir = os.path.join(app_dir, ANNOTATIONS_DIR_NAME)
    ann_file = os.path.join(ann_dir, f"{conv_id}.pbtxt")
    try:
      os.makedirs(ann_dir, exist_ok=True)
      if os.path.isfile(ann_file):
        with open(ann_file, "r", encoding="utf-8") as f:
          content = f.read()
        if 'title:"' in content or "title: \"" in content:
          new_content = re.sub(r'title:\s*"[^"]*"', f'title:"{new_title}"', content)
        else:
          new_content = f'title:"{new_title}" {content}'.strip()
      else:
        new_content = f'title:"{new_title}"\n'
      with open(ann_file, "w", encoding="utf-8") as f:
        f.write(new_content)
      disk_ok = True
    except Exception:
      pass

    # 2b. Update conversation_summaries.db
    db_file = os.path.join(app_dir, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        conn = sqlite3.connect(db_file, timeout=2.0)
        with conn:
          conn.execute(
              "UPDATE conversation_summaries SET title = ? WHERE conversation_id = ?",
              (new_title, conv_id),
          )
        conn.close()
        disk_ok = True
      except Exception:
        pass

  return rpc_ok or disk_ok


def get_conversation_title(conv_id: str) -> str:
  for app_dir in find_app_data_dirs():
    ann_file = os.path.join(app_dir, ANNOTATIONS_DIR_NAME, f"{conv_id}.pbtxt")
    if os.path.isfile(ann_file):
      try:
        with open(ann_file, "r", encoding="utf-8") as f:
          m = re.search(r'title:\s*"([^"]*)"', f.read())
          if m:
            return m.group(1).strip()
      except Exception:
        pass
    db_file = os.path.join(app_dir, SUMMARY_DB_NAME)
    if os.path.isfile(db_file):
      try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=1.0)
        cur = conn.cursor()
        cur.execute("SELECT title FROM conversation_summaries WHERE conversation_id = ?", (conv_id,))
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
          return str(row[0]).strip()
      except Exception:
        pass
  return ""


def get_parent_conversation_id(conv_id: str) -> str:
  """Finds parent conversation ID from transcript.jsonl if continued from an earlier chat."""
  if not conv_id:
    return ""
  for app_dir in find_app_data_dirs():
    tr_path = os.path.join(app_dir, "brain", conv_id, ".system_generated", "logs", "transcript.jsonl")
    if os.path.isfile(tr_path):
      try:
        with open(tr_path, "r", encoding="utf-8") as f:
          for line in f:
            if "USER_INPUT" in line:
              m = re.search(r"conversation://([a-zA-Z0-9_\-]+)", line)
              if m:
                return m.group(1)
              break
      except Exception:
        pass
  return ""


def discover_conversation_chain(conv_id: str) -> list[str]:
  """Traces backwards through transcripts to the root, returning [root_id, ..., conv_id]."""
  chain = []
  curr = conv_id
  seen = set()
  while curr and curr not in seen:
    seen.add(curr)
    chain.append(curr)
    curr = get_parent_conversation_id(curr)
  return list(reversed(chain))


def expand_chain_with_ancestors(chain_ids: list[str]) -> list[str]:
  """Ensures chain includes any prior ancestors leading up to chain_ids[0]."""
  if not chain_ids:
    return []
  ancestors = discover_conversation_chain(chain_ids[0])
  full_chain = []
  seen = set()
  for cid in ancestors + chain_ids:
    if cid and cid not in seen:
      seen.add(cid)
      full_chain.append(cid)
  return full_chain


def advance_in_progress_chain(chain_ids: list[str] = None, current_conv_id: str = "") -> list[str]:
  """Updates conversation titles in an active branch to reflect Scheme Γ markers.

  - Root (first) chat: [HH:MM] ▸ Topic
  - Intermediate closed chats: [HH:MM] ✓ Topic
  - Current active chat: [HH:MM] ⦿ Topic
  - Single standalone chat: [HH:MM] ⦿ Topic
  """
  if not chain_ids:
    chain_ids = [current_conv_id] if current_conv_id else []
  chain_ids = expand_chain_with_ancestors(chain_ids)
  if not chain_ids:
    return []

  now_time = get_current_time_str()
  updated = []

  base_topic = ""
  for cid in reversed(chain_ids):
    t = clean_base_title(get_conversation_title(cid))
    if t and t != "Investigation":
      base_topic = t
      break
  if not base_topic:
    base_topic = "Investigation"

  if len(chain_ids) == 1:
    cid = chain_ids[0]
    cur_title = get_conversation_title(cid)
    base = clean_base_title(cur_title) or base_topic
    new_title = f"[{now_time}] {MARKER_ACTIVE} {base}"
    update_conversation_title(cid, new_title)
    updated.append(cid)
    return updated

  # 1. First chat (root of branch) -> ▸
  root_id = chain_ids[0]
  cur_root = get_conversation_title(root_id)
  t_root = extract_time_prefix(cur_root, now_time)
  base_root = clean_base_title(cur_root) or base_topic
  update_conversation_title(root_id, f"[{t_root}] {MARKER_START} {base_root}")
  updated.append(root_id)

  # 2. Intermediate closed steps -> ✓
  for mid_id in chain_ids[1:-1]:
    cur_mid = get_conversation_title(mid_id)
    t_mid = extract_time_prefix(cur_mid, now_time)
    base_mid = clean_base_title(cur_mid) or base_topic
    update_conversation_title(mid_id, f"[{t_mid}] {MARKER_STEP} {base_mid}")
    updated.append(mid_id)

  # 3. Last chat (current active step) -> ⦿
  active_id = chain_ids[-1]
  cur_active = get_conversation_title(active_id)
  base_active = clean_base_title(cur_active) or base_topic
  update_conversation_title(active_id, f"[{now_time}] {MARKER_ACTIVE} {base_active}")
  updated.append(active_id)

  return updated


def finalize_conversation_chain(current_conv_id: str, chain_ids: list[str] = None) -> list[str]:
  """Finalizes the whole chain or single conversation to closed lifecycle markers."""
  if not chain_ids:
    chain_ids = [current_conv_id] if current_conv_id else []
  chain_ids = expand_chain_with_ancestors(chain_ids)

  time_str = get_current_time_str()
  updated = []

  if len(chain_ids) <= 1:
    cid = chain_ids[0] if chain_ids else current_conv_id
    if cid:
      cur_title = get_conversation_title(cid)
      base = clean_base_title(cur_title) or "Investigation"
      final_title = f"[{time_str}] {MARKER_SINGLE_CLOSED} {base}"
      update_conversation_title(cid, final_title)
      updated.append(cid)
    return updated

  # Multiple conversations in chain
  # 1. First chat -> «
  start_id = chain_ids[0]
  base_start = clean_base_title(get_conversation_title(start_id)) or "Investigation"
  update_conversation_title(start_id, f"[{time_str}] {MARKER_CLOSED_START} {base_start}")
  updated.append(start_id)

  # 2. Intermediates -> ‹✓›
  for mid_id in chain_ids[1:-1]:
    base_mid = clean_base_title(get_conversation_title(mid_id)) or "Investigation"
    update_conversation_title(mid_id, f"[{time_str}] {MARKER_CLOSED_STEP} {base_mid}")
    updated.append(mid_id)

  # 3. Last chat -> »
  end_id = chain_ids[-1]
  base_end = clean_base_title(get_conversation_title(end_id)) or "Investigation"
  update_conversation_title(end_id, f"[{time_str}] {MARKER_CLOSED_END} {base_end}")
  updated.append(end_id)

  return updated


def is_finalized_title(title: str) -> bool:
  if not title:
    return False
  return (
      MARKER_SINGLE_CLOSED in title
      or MARKER_CLOSED_END in title
      or MARKER_CLOSED_STEP in title
      or MARKER_CLOSED_START in title
  )


def reopen_conversation(conv_id: str, force: bool = False) -> bool:
  """Reopens a finalized conversation back to active marker ⦿ (and restores ancestors)."""
  cur_title = get_conversation_title(conv_id)
  if not cur_title:
    return False
  if not force and not is_finalized_title(cur_title):
    return False
  updated = advance_in_progress_chain(current_conv_id=conv_id)
  return bool(updated)


if __name__ == "__main__":
  if len(sys.argv) > 2 and sys.argv[1] == "set":
    cid = sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    update_conversation_title(cid, title)
  elif len(sys.argv) > 2 and sys.argv[1] == "reopen":
    cid = sys.argv[2]
    reopen_conversation(cid, force=True)
  elif len(sys.argv) > 1 and sys.argv[1] == "advance":
    cids = sys.argv[2:]
    if len(cids) == 1:
      advance_in_progress_chain(current_conv_id=cids[0])
    else:
      advance_in_progress_chain(chain_ids=cids)
  elif len(sys.argv) > 1 and sys.argv[1] == "finalize":
    cids = sys.argv[2:]
    if len(cids) == 1:
      finalize_conversation_chain(cids[0])
    else:
      finalize_conversation_chain(cids[0] if cids else "", cids)
