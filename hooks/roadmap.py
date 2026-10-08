#!/usr/bin/env python3
"""Per-project Epic Roadmap shared by every chat of one Jetski project.

Storage: <roadmaps>/<project_id>/{roadmap.json, roadmap.lock, journal.jsonl,
state/<conv_id>.md}. Several chats of a project run concurrently, so every
mutation takes an exclusive flock, re-reads and validates roadmap.json,
applies exactly one change and replaces the file atomically. Agents never
rewrite the roadmap wholesale; the per-chain state file is the only file they
edit directly.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

ROADMAPS_DIR_ENV = "JETSKI_ROADMAPS_DIR"
DEFAULT_ROADMAPS_DIR = "~/.gemini/config/roadmaps"
OUTSIDE_PROJECT = "outside-of-project"
SCHEMA_VERSION = 1

STATUSES = ("todo", "doing", "done", "dropped")
CLOSED_STATUSES = ("done", "dropped")
GLYPHS = {"done": "✓", "doing": "▸", "todo": "☐", "dropped": "✗"}

LOCK_TIMEOUT_ENV = "JETSKI_ROADMAP_LOCK_TIMEOUT"
DEFAULT_LOCK_TIMEOUT_S = 5.0
LOCK_POLL_S = 0.02

# Project and conversation ids become path components, so anything outside
# this alphabet (e.g. "..", "/") is rejected before touching the filesystem
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,127}$")
ITEM_ID_RE = re.compile(r"^R[1-9]\d*(?:\.[1-9]\d*)*$")

USAGE = """\
roadmap.py — per-project Epic Roadmap (shared by all chats of the project).

Usage:
  roadmap.py add <conv_id> "<text>" [--parent ID]   Add an item (prints its new ID)
  roadmap.py set <conv_id> <ID> todo|doing|done|dropped
                                                  Change status; `doing` claims it
  roadmap.py link <conv_id> <ID> <chat_id>        Item is handled in its own chat
  roadmap.py show                                 Compact render
  roadmap.py state-path <conv_id>                 Path of the chain state file
  roadmap.py state-move <old_conv_id> <new_conv_id>
                                                  Hand the state file (and owned
                                                  `doing` items) to a continuation

The project comes from $ANTIGRAVITY_PROJECT_ID, else conversation_summaries.db.
Chats outside a project have no roadmap. Never edit roadmap files directly.
"""


class RoadmapError(Exception):
  pass


def roadmaps_root() -> str:
  return os.path.abspath(
      os.path.expanduser(os.environ.get(ROADMAPS_DIR_ENV) or DEFAULT_ROADMAPS_DIR)
  )


def _check_safe_id(kind: str, value: str) -> str:
  if not SAFE_ID_RE.match(value or ""):
    raise RoadmapError(f"invalid {kind} '{value}'")
  return value


def project_dir(project_id: str) -> str:
  return os.path.join(roadmaps_root(), _check_safe_id("project id", project_id))


def state_path(project_id: str, conv_id: str) -> str:
  return os.path.join(
      project_dir(project_id),
      "state",
      _check_safe_id("conversation id", conv_id) + ".md",
  )


def project_id_for(conv_id: str) -> str:
  """Project of conv_id, or "" when the chat is outside any project."""
  pid = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
  if not pid and conv_id:
    # Lazy import: context_guard is a hook module and pulls in sqlite3 only
    # when the env var is missing (hooks, tests)
    from context_guard import get_conversation_db_info

    _, pid = get_conversation_db_info(conv_id)
  return "" if pid in ("", OUTSIDE_PROJECT) else pid


def require_project_id(conv_id: str) -> str:
  pid = project_id_for(conv_id)
  if not pid:
    who = f"conversation {conv_id[:8]}" if conv_id else "this chat"
    raise RoadmapError(
        f"no roadmap: {who} is outside any Jetski project"
        " (outside-project chats keep the handoff_summary file flow)"
    )
  return pid


def _now() -> str:
  return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _empty_doc() -> dict:
  return {"version": SCHEMA_VERSION, "next_id": 1, "items": []}


def validate(doc: object) -> None:
  """Raises RoadmapError unless doc satisfies the schema and ID invariants."""
  if not isinstance(doc, dict) or doc.get("version") != SCHEMA_VERSION:
    raise RoadmapError("corrupted roadmap.json: bad header")
  next_id = doc.get("next_id")
  items = doc.get("items")
  if not isinstance(next_id, int) or next_id < 1 or not isinstance(items, list):
    raise RoadmapError("corrupted roadmap.json: bad next_id/items")
  by_id: dict[str, dict] = {}
  for it in items:
    if not isinstance(it, dict):
      raise RoadmapError("corrupted roadmap.json: item is not an object")
    iid = it.get("id")
    if not isinstance(iid, str) or not ITEM_ID_RE.match(iid) or iid in by_id:
      raise RoadmapError(f"corrupted roadmap.json: bad or duplicate id {iid!r}")
    if not isinstance(it.get("text"), str) or not it["text"].strip():
      raise RoadmapError(f"corrupted roadmap.json: {iid} has no text")
    if it.get("status") not in STATUSES:
      raise RoadmapError(f"corrupted roadmap.json: {iid} has bad status")
    if not isinstance(it.get("next_child"), int) or it["next_child"] < 1:
      raise RoadmapError(f"corrupted roadmap.json: {iid} has bad next_child")
    if not all(isinstance(it.get(k, ""), str) for k in ("parent", "owner", "link")):
      raise RoadmapError(f"corrupted roadmap.json: {iid} has a non-string parent/owner/link")
    by_id[iid] = it
  # Counters must stay ahead of every issued number, otherwise an ID could be
  # reissued and journal entries would point at two different items
  for iid, it in by_id.items():
    head, _, last = iid.rpartition(".")
    parent = it.get("parent") or ""
    if parent != head:
      raise RoadmapError(f"corrupted roadmap.json: {iid} has bad parent")
    if parent:
      if parent not in by_id or int(last) >= by_id[parent]["next_child"]:
        raise RoadmapError(f"corrupted roadmap.json: {iid} breaks parent counter")
    elif int(iid[1:]) >= next_id:
      raise RoadmapError(f"corrupted roadmap.json: {iid} breaks next_id")


def load(project_id: str) -> dict:
  path = os.path.join(project_dir(project_id), "roadmap.json")
  if not os.path.exists(path):
    return _empty_doc()
  try:
    with open(path, "r", encoding="utf-8") as f:
      doc = json.load(f)
  except ValueError as e:
    raise RoadmapError(f"corrupted roadmap.json ({e}); refusing to write") from e
  validate(doc)
  return doc


@contextlib.contextmanager
def _locked(pdir: str):
  os.makedirs(pdir, exist_ok=True)
  try:
    timeout_s = float(os.environ.get(LOCK_TIMEOUT_ENV) or DEFAULT_LOCK_TIMEOUT_S)
  except ValueError:
    raise RoadmapError(f"${LOCK_TIMEOUT_ENV} must be a number of seconds") from None
  fd = os.open(os.path.join(pdir, "roadmap.lock"), os.O_RDWR | os.O_CREAT, 0o644)
  try:
    max_attempts = int(timeout_s / LOCK_POLL_S) + 1
    for attempt in range(max_attempts):
      try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        break
      except BlockingIOError:
        if attempt == max_attempts - 1:
          raise RoadmapError(
              f"roadmap is locked by another chat for >{timeout_s:.0f}s; retry"
          ) from None
        time.sleep(LOCK_POLL_S)
    yield
  finally:
    os.close(fd)


def _write_atomic(path: str, doc: dict) -> None:
  tmp = f"{path}.tmp.{os.getpid()}"
  try:
    with open(tmp, "w", encoding="utf-8") as f:
      json.dump(doc, f, ensure_ascii=False, indent=1)
      f.write("\n")
      f.flush()
      os.fsync(f.fileno())
    os.replace(tmp, path)
  finally:
    if os.path.exists(tmp):
      os.unlink(tmp)
  dfd = os.open(os.path.dirname(path), os.O_RDONLY)
  try:
    os.fsync(dfd)
  finally:
    os.close(dfd)


def _journal(pdir: str, conv_id: str, op: str, **fields) -> None:
  rec = {"ts": _now(), "conv": conv_id, "op": op, **fields}
  with open(os.path.join(pdir, "journal.jsonl"), "a", encoding="utf-8") as f:
    f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _find(doc: dict, item_id: str) -> dict:
  for it in doc["items"]:
    if it["id"] == item_id:
      return it
  raise RoadmapError(f"unknown item {item_id}")


def _touch(item: dict, conv_id: str) -> None:
  item["updated_at"] = _now()
  item["updated_by"] = conv_id


def _mutate(project_id: str, conv_id: str, apply) -> str:
  """Runs apply(doc) -> (message, journal op, journal fields) under the project lock."""
  _check_safe_id("conversation id", conv_id)
  pdir = project_dir(project_id)
  with _locked(pdir):
    doc = load(project_id)
    msg, op, fields = apply(doc)
    validate(doc)
    _write_atomic(os.path.join(pdir, "roadmap.json"), doc)
    _journal(pdir, conv_id, op, **fields)
  return msg


def add_item(project_id: str, conv_id: str, text: str, parent: str = "") -> str:
  clean = " ".join((text or "").split())
  if not clean:
    raise RoadmapError("item text must not be empty")

  def apply(doc: dict):
    if parent:
      p = _find(doc, parent)
      new_id = f"{parent}.{p['next_child']}"
      p["next_child"] += 1
    else:
      new_id = f"R{doc['next_id']}"
      doc["next_id"] += 1
    now = _now()
    doc["items"].append({
        "id": new_id,
        "parent": parent,
        "text": clean,
        "status": "todo",
        "owner": "",
        "link": "",
        "next_child": 1,
        "created_at": now,
        "created_by": conv_id,
        "updated_at": now,
        "updated_by": conv_id,
    })
    return f"{new_id} added: {clean}", "add", {"id": new_id, "text": clean}

  return _mutate(project_id, conv_id, apply)


def set_status(project_id: str, conv_id: str, item_id: str, status: str) -> tuple[str, str]:
  """Returns (message, ownership warning or "")."""
  if status not in STATUSES:
    raise RoadmapError(f"bad status '{status}' (expected {'|'.join(STATUSES)})")
  warning = []

  def apply(doc: dict):
    it = _find(doc, item_id)
    prev_owner = it.get("owner") or ""
    if (
        status == "doing"
        and it["status"] == "doing"
        and prev_owner
        and prev_owner != conv_id
    ):
      warning.append(
          f"warning: {item_id} was 'doing' in conversation {prev_owner[:8]};"
          " ownership moved to this chat"
      )
    it["status"] = status
    it["owner"] = conv_id if status == "doing" else ""
    _touch(it, conv_id)
    return f"{item_id} {status}: {it['text']}", "set", {"id": item_id, "status": status}

  msg = _mutate(project_id, conv_id, apply)
  return msg, (warning[0] if warning else "")


def link_item(project_id: str, conv_id: str, item_id: str, chat_id: str) -> str:
  _check_safe_id("chat id", chat_id)

  def apply(doc: dict):
    it = _find(doc, item_id)
    it["link"] = chat_id
    _touch(it, conv_id)
    return f"{item_id} linked to conversation://{chat_id}", "link", {"id": item_id, "chat": chat_id}

  return _mutate(project_id, conv_id, apply)


def state_move(project_id: str, old_conv: str, new_conv: str) -> str:
  """Moves state/<old>.md to state/<new>.md and hands over old's `doing` items.

  Idempotent: the handoff runs it, and the continuation runs it again when it
  started before the handoff finished, so an already completed move is a no-op.
  """
  src = state_path(project_id, old_conv)
  dst = state_path(project_id, new_conv)
  pdir = project_dir(project_id)
  with _locked(pdir):
    if not os.path.isfile(src):
      if os.path.isfile(dst):
        return f"state already moved to {dst}"
      raise RoadmapError(f"state file {src} does not exist")
    if os.path.exists(dst):
      raise RoadmapError(f"state file {dst} already exists; refusing to overwrite")
    doc = load(project_id)
    moved = []
    for it in doc["items"]:
      if it["status"] == "doing" and it.get("owner") == old_conv:
        it["owner"] = new_conv
        _touch(it, old_conv)
        moved.append(it["id"])
    os.replace(src, dst)
    if moved:
      try:
        validate(doc)
        _write_atomic(os.path.join(pdir, "roadmap.json"), doc)
      except BaseException:
        os.replace(dst, src)
        raise
    _journal(pdir, old_conv, "state-move", to=new_conv, items=moved)
  owned = f"; {len(moved)} doing item(s) now owned by {new_conv[:8]}" if moved else ""
  return f"state moved to {dst}{owned}"


def render(doc: dict) -> str:
  items = doc["items"]
  if not items:
    return "Roadmap: empty"
  counts = {s: 0 for s in STATUSES}
  children: dict[str, list[dict]] = {}
  for it in items:
    counts[it["status"]] += 1
    children.setdefault(it.get("parent") or "", []).append(it)

  def descendants(iid: str) -> list[dict]:
    out = []
    for ch in children.get(iid, []):
      out.append(ch)
      out.extend(descendants(ch["id"]))
    return out

  lines = [
      f"Roadmap ({len(items)}): {counts['done']} done · {counts['doing']} doing"
      f" · {counts['todo']} todo · {counts['dropped']} dropped"
  ]

  def emit(it: dict, depth: int) -> None:
    line = f"{'  ' * depth}{GLYPHS[it['status']]} {it['id']} {it['text']}"
    if it["status"] == "doing" and it.get("owner"):
      line += f" [{it['owner'][:8]}]"
    if it.get("link"):
      line += f" → {it['link'][:8]}"
    desc = descendants(it["id"])
    # A finished epic with no open sub-items carries no actionable detail
    if depth == 0 and it["status"] == "done" and desc and all(
        d["status"] in CLOSED_STATUSES for d in desc
    ):
      lines.append(f"{line} (+{len(desc)})")
      return
    lines.append(line)
    for ch in children.get(it["id"], []):
      emit(ch, depth + 1)

  for top in children.get("", []):
    emit(top, 0)
  return "\n".join(lines)


def show(project_id: str) -> str:
  return render(load(project_id))


def _cli(argv: list[str]) -> int:
  if not argv or argv[0] in ("-h", "--help", "help"):
    print(USAGE, end="")
    return 0 if argv else 2
  cmd, args = argv[0], argv[1:]
  if cmd == "add" and len(args) in (2, 4) and (len(args) == 2 or args[2] == "--parent"):
    pid = require_project_id(args[0])
    print(add_item(pid, args[0], args[1], args[3] if len(args) == 4 else ""))
  elif cmd == "set" and len(args) == 3:
    pid = require_project_id(args[0])
    msg, warning = set_status(pid, args[0], args[1], args[2])
    if warning:
      print(warning, file=sys.stderr)
    print(msg)
  elif cmd == "link" and len(args) == 3:
    pid = require_project_id(args[0])
    print(link_item(pid, args[0], args[1], args[2]))
  elif cmd == "show" and not args:
    pid = require_project_id(os.environ.get("ANTIGRAVITY_CONVERSATION_ID", ""))
    print(show(pid))
  elif cmd == "state-path" and len(args) == 1:
    pid = require_project_id(args[0])
    path = state_path(pid, args[0])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(path)
  elif cmd == "state-move" and len(args) == 2:
    pid = require_project_id(args[0])
    print(state_move(pid, args[0], args[1]))
  else:
    print(f"roadmap.py: bad arguments for '{cmd}'\n", file=sys.stderr)
    print(USAGE, file=sys.stderr, end="")
    return 2
  return 0


def main() -> int:
  try:
    return _cli(sys.argv[1:])
  except (RoadmapError, OSError) as e:
    print(f"roadmap.py: {e}", file=sys.stderr)
    return 1


if __name__ == "__main__":
  sys.exit(main())
