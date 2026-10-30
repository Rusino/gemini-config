#!/usr/bin/env python3
"""PreInvocation hook that tells the agent when pre_tool_guard.py failed open

pre_tool_guard.py allows a tool call when one of its guards raises and records
the failure in a marker file, because its stderr reaches only Jetski's log.
This hook turns a marker newer than the chat's last notice into an ephemeral
message. It stays a separate stdlib-only script so that a broken sibling that
pre_tool_guard.py imports, context_guard.py included, cannot silence it
"""

import json
import os
import re
import sqlite3
import sys
import time
import traceback

# Must equal pre_tool_guard.FAILURE_MARKER_DEFAULT (pinned by a test)
FAILURE_MARKER_DEFAULT = "~/.gemini/config/logs/pre_tool_guard_failure.json"
# A broken guard rewrites the marker on every gated call; one notice per chat
# per interval is enough to make the agent tell the user
NOTICE_INTERVAL_S = 900
APP_DATA_DIR_CANDIDATES = ("~/.gemini/antigravity", "~/.gemini/jetski")
_BRAIN_ROOT_RE = re.compile(r"^(.*?)/brain/[^/]+(?:/|$)")
_SUBAGENT_SENDER_RE = re.compile(
    r"\[Message\]\s+timestamp=\S+\s+sender="
    r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})(?=\s|$)"
)


def _is_subagent(data: dict, conv: str) -> bool:
  if data.get("parentConversationId"):
    return True
  transcript_path = str(data.get("transcriptPath") or "").strip()
  if transcript_path and os.path.isfile(transcript_path):
    try:
      with open(transcript_path, "r", encoding="utf-8", errors="ignore") as f:
        line = f.readline()
      if line:
        step0 = json.loads(line)
        if (
            isinstance(step0, dict)
            and step0.get("step_index") == 0
            and step0.get("source") == "SYSTEM"
            and step0.get("type") == "SYSTEM_MESSAGE"
        ):
          m = _SUBAGENT_SENDER_RE.search(str(step0.get("content") or ""))
          if m and m.group(1) != conv:
            return True
    except Exception:
      pass
  # Kept inline so a broken context_guard.py cannot silence failure notices
  dirs: list[str] = []
  for hint in (transcript_path, str(data.get("artifactDirectoryPath") or "")):
    m = _BRAIN_ROOT_RE.match(hint.replace("\\", "/"))
    if m and os.path.isdir(m.group(1)) and m.group(1) not in dirs:
      dirs.append(m.group(1))
  for raw in APP_DATA_DIR_CANDIDATES:
    d = os.path.abspath(os.path.expanduser(raw))
    if os.path.isdir(d) and d not in dirs:
      dirs.append(d)
  query = (
      "SELECT parent_conversation_id FROM conversation_summaries WHERE"
      " conversation_id = ?"
  )
  for d in dirs:
    db_path = os.path.join(d, "conversation_summaries.db")
    if not os.path.isfile(db_path):
      continue
    for flags in ("mode=ro", "mode=ro&immutable=1"):
      try:
        conn = sqlite3.connect(f"file:{db_path}?{flags}", uri=True, timeout=1.0)
        try:
          row = conn.execute(query, (conv,)).fetchone()
        finally:
          conn.close()
        if row is not None:
          parent = str(row[0] or "").strip()
          return bool(parent and parent != conv)
        break
      except Exception:
        continue
  return False


def failure_marker_path() -> str:
  return os.path.expanduser(
      os.environ.get("JETSKI_PRE_TOOL_GUARD_MARKER") or FAILURE_MARKER_DEFAULT)


def _read_json(path: str) -> dict:
  try:
    with open(path) as f:
      return json.load(f)
  except FileNotFoundError:
    return {}


def _write_json(path: str, value: dict) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  tmp = f"{path}.{os.getpid()}.tmp"
  with open(tmp, "w") as f:
    json.dump(value, f)
  os.replace(tmp, path)


def notice(data: dict, now: float) -> dict:
  conv = data.get("conversationId") or ""
  if not conv or data.get("parentConversationId") or data.get("isBattleMode"):
    return {}
  artifact_dir = data.get("artifactDirectoryPath") or ""
  state_path = (
      os.path.join(artifact_dir, "scratch", ".pre_tool_guard_notice.json")
      if artifact_dir
      else f"/tmp/jetski_pre_tool_guard_notice_{conv}.json"
  )
  state = _read_json(state_path)
  if "quiet_until" not in state:
    # Failures before the chat's first model call happened in other chats; if
    # the guard is still broken, this chat's own calls refresh the marker
    _write_json(state_path, {"quiet_until": now})
    return {}
  marker = _read_json(failure_marker_path())
  if marker.get("ts", 0) <= state["quiet_until"]:
    return {}
  # Defer DB/transcript subagent lookup until a fresh failure marker actually exists
  if _is_subagent(data, conv):
    return {}
  _write_json(state_path, {"quiet_until": now + NOTICE_INTERVAL_S})
  where = "this chat" if marker.get("conv") == conv else f"chat {marker.get('conv') or '?'}"
  when = time.strftime("%H:%MZ", time.gmtime(marker["ts"]))
  return {"injectSteps": [{"ephemeralMessage": (
      f"[PRE-TOOL GUARD FAILED OPEN] At {when} hooks/pre_tool_guard.py raised "
      f"{marker.get('error', '?')} ({marker.get('where', '?')}) on a gated tool call in {where} "
      "and allowed it without running its guards. Until it is fixed, run_command and file edits "
      "may run unguarded: tell the user. To repair it, edit a copy of the file and mv it into "
      "place (see the comment at its top), then run `python3 tests/run_all_tests.py` in "
      "~/.gemini/config."
  )}]}


def main() -> None:
  out = {}
  try:
    raw = sys.stdin.read().strip()
    if raw:
      out = notice(json.loads(raw), time.time())
  except Exception:
    # Jetski only logs a failing PreInvocation hook; an empty answer with the
    # trace on stderr keeps the model call going
    traceback.print_exc()
  print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
  main()
