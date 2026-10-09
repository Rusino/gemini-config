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
import sys
import time
import traceback

# Must equal pre_tool_guard.FAILURE_MARKER_DEFAULT (pinned by a test)
FAILURE_MARKER_DEFAULT = "~/.gemini/config/logs/pre_tool_guard_failure.json"
# A broken guard rewrites the marker on every gated call; one notice per chat
# per interval is enough to make the agent tell the user
NOTICE_INTERVAL_S = 900


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
