#!/usr/bin/env python3
"""PostToolUse hook: put a newly uploaded CL or PR into the chat title.

A chat that starts from a bug gets its CL later, and CL Flight Deck finds chats only
by the CL or PR in their title. When `git cl upload` or `gh pr create` prints the
review URL, this hook prepends `cl/<N>` or `PR #<N>` to the topic, keeping the bug
ID. Chats whose title already names a CL or PR keep it: in review chats that is the
reviewed change, and a companion WIP CL must not replace it.

The hook never blocks or rewrites the tool result; it always prints `{}`.
"""

from __future__ import annotations

import json
import os
import re
import sys

HOOKS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(HOOKS_DIR), "scripts")
for _d in (HOOKS_DIR, SCRIPTS_DIR):
  if _d not in sys.path:
    sys.path.insert(0, _d)

UPLOAD_CMD_RE = re.compile(r"\bgit\s+cl\s+upload\b|\bgh\s+pr\s+create\b")
GERRIT_URL_RE = re.compile(
    r"https://(?:skia|chromium)-review\.(?:googlesource\.com|git\.corp\.google\.com)"
    r"/c/\S+?/\+/(\d{6,8})\b"
)
GITHUB_PR_URL_RE = re.compile(r"https://github\.com/flutter/flutter/pull/(\d{5,7})\b")
# "[HH:MM] ⦿ " takes 10 characters and titles must stay under 50
MAX_TOPIC_LEN = 38


def topic_with_change(title: str, result: str) -> str:
  """Returns the new topic, or "" when the title already names a change or none was made."""
  import chat_lifecycle as cl  # pylint: disable=g-import-not-at-top
  import cl_watcher as w  # pylint: disable=g-import-not-at-top

  base = cl.clean_base_title(title)
  if w.RE_CL_ID.search(base) or w.RE_PR_ID.search(base):
    return ""
  m = GERRIT_URL_RE.search(result)
  if m:
    tag = f"cl/{m.group(1)}"
  else:
    m = GITHUB_PR_URL_RE.search(result)
    if not m:
      return ""
    tag = f"PR #{m.group(1)}"
  return f"{tag} {base}"[:MAX_TOPIC_LEN].rstrip()


def main() -> int:
  try:
    payload = json.load(sys.stdin)
    call = payload.get("toolCall") or {}
    cmd = str((call.get("args") or {}).get("CommandLine") or "")
    cid = str(payload.get("conversationId") or "")
    result = str(payload.get("result") or "")
    if call.get("name") == "run_command" and cid and UPLOAD_CMD_RE.search(cmd):
      import chat_lifecycle as cl  # pylint: disable=g-import-not-at-top

      topic = topic_with_change(cl.get_conversation_title(cid), result)
      if topic:
        cl.set_conversation_topic(cid, topic)
  except Exception:  # pylint: disable=broad-except
    # A title hook must never break the upload it observes
    pass
  print("{}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
