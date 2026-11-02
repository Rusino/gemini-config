#!/usr/bin/env python3
"""Fail-open entry wrapper for pre_tool_guard.py.

Imports pre_tool_guard inside a try block so a SyntaxError or import crash in
pre_tool_guard.py cannot lock out edit_file, write_file, or run_command.
"""

from __future__ import annotations

import json
import os
import sys
import time
import traceback

FAILURE_MARKER_DEFAULT = "~/.gemini/config/logs/pre_tool_guard_failure.json"


def _record_entry_failure(error: Exception) -> None:
  if isinstance(error, SyntaxError) and error.filename:
    where = f"{os.path.basename(error.filename)}:{error.lineno}"
  else:
    tb = traceback.extract_tb(error.__traceback__) if error.__traceback__ else []
    where = (
        f"{os.path.basename(tb[-1].filename)}:{tb[-1].lineno} in {tb[-1].name}"
        if tb
        else "run_pre_tool_guard.py"
    )
  marker = {
      "ts": time.time(),
      "conv": os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""),
      "error": f"{type(error).__name__}: {error}"[:300],
      "where": where,
  }
  path = os.path.expanduser(
      os.environ.get("JETSKI_PRE_TOOL_GUARD_MARKER") or FAILURE_MARKER_DEFAULT
  )
  try:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(marker, f)
    os.replace(tmp, path)
  except OSError:
    pass


def main() -> None:
  hooks_dir = os.path.dirname(os.path.abspath(__file__))
  if hooks_dir not in sys.path:
    sys.path.insert(0, hooks_dir)
  try:
    import pre_tool_guard

    pre_tool_guard._guarded_main()
  except Exception as exc:
    # A parse/import error in pre_tool_guard.py happens before _guarded_main exists,
    # so fail open here and write the marker for pre_tool_guard_notice.py
    print('{"decision": "allow"}')
    traceback.print_exc(file=sys.stderr)
    _record_entry_failure(exc)


if __name__ == "__main__":
  main()
