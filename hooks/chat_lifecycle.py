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


def get_current_time_str() -> str:
  try:
    tz = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(tz).strftime("%H:%M")
  except Exception:
    return datetime.now().strftime("%H:%M")


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


def update_conversation_title(conv_id: str, new_title: str) -> bool:
  """Updates conversation title in both annotations pbtxt and SQLite db."""
  if not conv_id or not new_title:
    return False

  updated_any = False
  for app_dir in find_app_data_dirs():
    # 1. Update annotations .pbtxt
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
      updated_any = True
    except Exception:
      pass

    # 2. Update conversation_summaries.db
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
        updated_any = True
      except Exception:
        pass

  return updated_any


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


def finalize_conversation_chain(current_conv_id: str, chain_ids: list[str] = None) -> list[str]:
  """Finalizes the whole chain or single conversation to closed lifecycle markers."""
  if not chain_ids:
    chain_ids = [current_conv_id] if current_conv_id else []

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


if __name__ == "__main__":
  if len(sys.argv) > 2 and sys.argv[1] == "set":
    cid = sys.argv[2]
    title = sys.argv[3] if len(sys.argv) > 3 else ""
    update_conversation_title(cid, title)
  elif len(sys.argv) > 1 and sys.argv[1] == "finalize":
    cids = sys.argv[2:]
    finalize_conversation_chain(cids[0] if cids else "", cids)
