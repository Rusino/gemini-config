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
import subprocess
import sys
import time
from datetime import datetime, timezone

ROADMAPS_DIR_ENV = "JETSKI_ROADMAPS_DIR"
DEFAULT_ROADMAPS_DIR = "~/.gemini/config/roadmaps"
# Static JSON snapshot served via /static/artifacts/ so sidebar project headers show open counts
ROADMAP_INDEX_FILE_NAME = "jetski-roadmaps.json"
APP_DATA_DIR_CANDIDATES = ("~/.gemini/jetski", "~/.gemini/antigravity")
OUTSIDE_PROJECT = "outside-of-project"
SCHEMA_VERSION = 1

STATUSES = ("todo", "doing", "done", "dropped")
CLOSED_STATUSES = ("done", "dropped")
GLYPHS = {"done": "✓", "doing": "▸", "todo": "☐", "dropped": "✗"}
CLOSED_COLLAPSE_THRESHOLD = 5
CLOSED_KEEP_RECENT = 3

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
  roadmap.py edit <conv_id> <ID> "<new text>"     Reword an item (ID/status/link kept)
  roadmap.py reorder <conv_id> <ID> [<ID> ...]    Listed siblings go first in this
                                                  order, the rest keep their order
  roadmap.py move <conv_id> <ID> --before <ID> | --after <ID> | --top | --bottom
                                                  Move an item among its siblings;
                                                  sub-items move with their parent
  roadmap.py show [conv_id] [--all]               Compact render in stored order
                                                  (--all shows all closed items)
  roadmap.py state-path <conv_id>                 Path of the chain state file
  roadmap.py state-move <old_conv_id> <new_conv_id>
                                                  Hand the state file (and owned
                                                  `doing` items) to a continuation
  roadmap.py export-index                         Export jetski-roadmaps.json for UI

The project comes from conversation_summaries.db, else $ANTIGRAVITY_PROJECT_ID.
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
  pid = ""
  if conv_id:
    # Lazy import: context_guard is a hook module and pulls in sqlite3 only
    # when the env var is missing (hooks, tests)
    # Query the DB before $ANTIGRAVITY_PROJECT_ID because the env var is fixed
    # at turn start and stays stale after a mid-turn move_chats_to_project call
    from context_guard import get_conversation_db_info

    _, pid = get_conversation_db_info(conv_id)
  if not pid:
    pid = os.environ.get("ANTIGRAVITY_PROJECT_ID", "").strip()
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
  # Optional baton tracks which chat already proposed the current next todo item
  baton = doc.get("baton")
  if baton is not None:
    if not isinstance(baton, dict) or not all(
        isinstance(baton.get(k), str) and baton[k].strip() for k in ("conv", "item")
    ):
      raise RoadmapError("corrupted roadmap.json: bad baton")
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
  # Refresh the static UI roadmap snapshot after releasing the per-project lock
  export_roadmap_index()
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


# Computes the next-task baton state under the roadmap lock when an item changes status
def _evaluate_baton(doc: dict, conv_id: str, status: str) -> str:
  if status == "doing":
    doc.pop("baton", None)
    return ""
  if status not in CLOSED_STATUSES:
    return ""

  doing_items = [it for it in doc["items"] if it["status"] == "doing"]
  if doing_items:
    doc.pop("baton", None)
    active_desc = ", ".join(
        f"{it['id']} [{it['owner'][:8]}]" if it.get("owner") else it["id"]
        for it in doing_items
    )
    return (
        f"[NEXT-TASK BATON: HOLD — {len(doing_items)} item(s) still doing"
        f" ({active_desc}); do NOT propose next-task from this chat]"
    )

  top_todos = [
      it for it in doc["items"] if not it.get("parent") and it["status"] == "todo"
  ]
  any_todos = [it for it in doc["items"] if it["status"] == "todo"]
  next_todo = (top_todos or any_todos or [None])[0]
  if not next_todo:
    doc.pop("baton", None)
    return "[NEXT-TASK BATON: ALL DONE — 0 doing, 0 todo remaining in project]"

  cur_baton = doc.get("baton")
  holder = cur_baton.get("conv", "") if isinstance(cur_baton, dict) else ""
  held_item = cur_baton.get("item", "") if isinstance(cur_baton, dict) else ""
  # Prevent duplicate next-task proposals when another chat already holds the baton for next_todo
  if holder and holder != conv_id and held_item == next_todo["id"]:
    return (
        f"[NEXT-TASK BATON: HOLD — {next_todo['id']} is already proposed by"
        f" conversation {holder[:8]}; do NOT propose next-task from this chat]"
    )

  doc["baton"] = {"conv": conv_id, "item": next_todo["id"]}
  return (
      f"[NEXT-TASK BATON: YOURS — 0 doing in project; propose next todo"
      f" {next_todo['id']} ({next_todo['text']})]"
  )


# Updates item status and returns (message, ownership warning, baton notice)
def set_status_with_baton(
    project_id: str, conv_id: str, item_id: str, status: str
) -> tuple[str, str, str]:
  if status not in STATUSES:
    raise RoadmapError(f"bad status '{status}' (expected {'|'.join(STATUSES)})")
  warning: list[str] = []
  baton_notice: list[str] = []

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
    # Compute baton notice inside the lock so parallel chats serialize on doing == 0
    notice = _evaluate_baton(doc, conv_id, status)
    if notice:
      baton_notice.append(notice)
    return f"{item_id} {status}: {it['text']}", "set", {"id": item_id, "status": status}

  msg = _mutate(project_id, conv_id, apply)
  return msg, (warning[0] if warning else ""), (baton_notice[0] if baton_notice else "")


# Preserves the 2-tuple (message, warning) contract for callers that do not print baton hints
def set_status(project_id: str, conv_id: str, item_id: str, status: str) -> tuple[str, str]:
  msg, warn, _ = set_status_with_baton(project_id, conv_id, item_id, status)
  return msg, warn


def link_item(project_id: str, conv_id: str, item_id: str, chat_id: str) -> str:
  _check_safe_id("chat id", chat_id)

  def apply(doc: dict):
    it = _find(doc, item_id)
    it["link"] = chat_id
    _touch(it, conv_id)
    return f"{item_id} linked to conversation://{chat_id}", "link", {"id": item_id, "chat": chat_id}

  return _mutate(project_id, conv_id, apply)


def edit_item(project_id: str, conv_id: str, item_id: str, text: str) -> str:
  """Rewords an item; ID, status, owner, link and position stay as they are."""
  clean = " ".join((text or "").split())
  if not clean:
    raise RoadmapError("item text must not be empty")

  def apply(doc: dict):
    it = _find(doc, item_id)
    old = it["text"]
    it["text"] = clean
    _touch(it, conv_id)
    return f"{item_id} edited: {clean}", "edit", {"id": item_id, "old": old, "text": clean}

  return _mutate(project_id, conv_id, apply)


def _place_siblings(doc: dict, parent: str, order: list[str]) -> None:
  """Rewrites the slots of parent's children in doc["items"] to follow order.

  The tree is rendered by grouping items per parent in list order, so only the
  relative order of siblings matters: descendants follow their ancestor
  wherever they sit in the list, and IDs never change.
  """
  items = doc["items"]
  slots = [i for i, it in enumerate(items) if (it.get("parent") or "") == parent]
  assert sorted(items[i]["id"] for i in slots) == sorted(order), "order must permute siblings"
  by_id = {items[i]["id"]: items[i] for i in slots}
  for slot, iid in zip(slots, order):
    items[slot] = by_id[iid]


def _siblings_of(doc: dict, item_id: str) -> tuple[str, list[str]]:
  parent = _find(doc, item_id).get("parent") or ""
  return parent, [it["id"] for it in doc["items"] if (it.get("parent") or "") == parent]


def reorder_items(project_id: str, conv_id: str, item_ids: list[str]) -> str:
  """Puts item_ids first among their siblings in the given order; the rest keep
  their relative order after them. All item_ids must share one parent."""
  if not item_ids:
    raise RoadmapError("reorder needs at least one item ID")
  if len(set(item_ids)) != len(item_ids):
    raise RoadmapError("reorder: an item ID is listed twice")

  def apply(doc: dict):
    parent, siblings = _siblings_of(doc, item_ids[0])
    for iid in item_ids[1:]:
      if _siblings_of(doc, iid)[0] != parent:
        raise RoadmapError(f"reorder: {iid} and {item_ids[0]} have different parents")
    listed = set(item_ids)
    _place_siblings(doc, parent, item_ids + [s for s in siblings if s not in listed])
    # Order changes deliberately leave updated_at alone: it ranks which closed
    # items stay visible in the collapsed render, and a move is not progress
    scope = parent or "top level"
    return f"reordered {scope}: {' '.join(item_ids)} first", "reorder", {"ids": item_ids}

  return _mutate(project_id, conv_id, apply)


MOVE_POSITIONS = ("before", "after", "top", "bottom")


def move_item(
    project_id: str, conv_id: str, item_id: str, position: str, target: str = ""
) -> str:
  """Moves one item (with its subtree) within its siblings."""
  if position not in MOVE_POSITIONS:
    raise RoadmapError(f"bad move position '{position}'")
  if (position in ("before", "after")) != bool(target):
    raise RoadmapError(f"move --{position} {'needs' if not target else 'takes no'} target ID")
  if target == item_id:
    raise RoadmapError(f"cannot move {item_id} relative to itself")

  def apply(doc: dict):
    parent, siblings = _siblings_of(doc, item_id)
    siblings.remove(item_id)
    if position == "top":
      at = 0
    elif position == "bottom":
      at = len(siblings)
    else:
      if _siblings_of(doc, target)[0] != parent:
        raise RoadmapError(f"move: {target} is not a sibling of {item_id}")
      at = siblings.index(target) + (position == "after")
    siblings.insert(at, item_id)
    _place_siblings(doc, parent, siblings)
    where = f"{position} {target}" if target else f"to {position}"
    return (
        f"{item_id} moved {where}",
        "move",
        {"id": item_id, "position": position, "target": target},
    )

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
    # Transfer baton ownership along with doing items when a chain hands off
    baton_moved = False
    if isinstance(doc.get("baton"), dict) and doc["baton"].get("conv") == old_conv:
      doc["baton"]["conv"] = new_conv
      baton_moved = True
    os.replace(src, dst)
    if moved or baton_moved:
      try:
        validate(doc)
        _write_atomic(os.path.join(pdir, "roadmap.json"), doc)
      except BaseException:
        os.replace(dst, src)
        raise
    _journal(pdir, old_conv, "state-move", to=new_conv, items=moved)
  # Update exported owner tags in jetski-roadmaps.json after releasing the project lock
  if moved or baton_moved:
    export_roadmap_index()
  owned = f"; {len(moved)} doing item(s) now owned by {new_conv[:8]}" if moved else ""
  return f"state moved to {dst}{owned}"


# Releases the next-task baton when any of the given conversations is finalized
def clear_baton(project_id: str, conv_ids: set[str] | list[str]) -> bool:
  if not project_id or not conv_ids:
    return False
  pdir = project_dir(project_id)
  if not os.path.exists(os.path.join(pdir, "roadmap.json")):
    return False
  target_set = set(conv_ids)
  # Defer export_roadmap_index until after the per-project lock is closed
  cleared = False
  with _locked(pdir):
    doc = load(project_id)
    baton = doc.get("baton")
    if isinstance(baton, dict) and baton.get("conv") in target_set:
      doc.pop("baton", None)
      validate(doc)
      _write_atomic(os.path.join(pdir, "roadmap.json"), doc)
      cleared = True
  if cleared:
    export_roadmap_index()
  return cleared


# Returns active app data directories, keeping test runs isolated when JETSKI_ROADMAPS_DIR is set
def find_app_data_dirs() -> list[str]:
  default_dirs = {
      os.path.abspath(os.path.expanduser(c)) for c in APP_DATA_DIR_CANDIDATES
  }
  env_dir = os.environ.get("ANTIGRAVITY_APP_DATA_DIR")
  if os.environ.get(ROADMAPS_DIR_ENV):
    # Never touch live user app data dirs when JETSKI_ROADMAPS_DIR points to a test dir
    if env_dir:
      exp = os.path.abspath(os.path.expanduser(env_dir))
      if exp not in default_dirs and os.path.isdir(exp):
        return [exp]
    return []
  res: list[str] = []
  if env_dir:
    exp = os.path.abspath(os.path.expanduser(env_dir))
    if os.path.isdir(exp):
      res.append(exp)
  for candidate in APP_DATA_DIR_CANDIDATES:
    exp = os.path.abspath(os.path.expanduser(candidate))
    if os.path.isdir(exp) and exp not in res:
      res.append(exp)
  return res


# Summarizes leaf/effective task counts and open items for the sidebar project header badge
def summarize_doc(doc: dict) -> dict:
  items = doc.get("items") or []
  counts = {s: 0 for s in STATUSES}
  children: dict[str, list[dict]] = {}
  for it in items:
    children.setdefault(it.get("parent") or "", []).append(it)

  def descendants(iid: str) -> list[dict]:
    out = []
    for ch in children.get(iid, []):
      out.append(ch)
      out.extend(descendants(ch["id"]))
    return out

  for it in items:
    desc = descendants(it["id"])
    # Count leaf tasks directly; only count a parent container if it remains open after
    # all of its descendants were closed, avoiding parent + child double-counting
    if not desc:
      counts[it["status"]] += 1
    elif it["status"] not in CLOSED_STATUSES and all(
        d["status"] in CLOSED_STATUSES for d in desc
    ):
      counts[it["status"]] += 1

  open_items: list[dict] = []

  def collect_open(parent_id: str, depth: int) -> None:
    for it in children.get(parent_id, []):
      desc = descendants(it["id"])
      has_open_desc = any(d["status"] not in CLOSED_STATUSES for d in desc)
      if it["status"] not in CLOSED_STATUSES or has_open_desc:
        if it["status"] not in CLOSED_STATUSES:
          open_items.append({
              "id": it["id"],
              "status": it["status"],
              "text": it["text"],
              "depth": depth,
              "owner": (it.get("owner") or "")[:8],
          })
        collect_open(it["id"], depth + (1 if it["status"] not in CLOSED_STATUSES else 0))

  collect_open("", 0)
  doing = counts["doing"]
  todo = counts["todo"]
  done = counts["done"]
  dropped = counts["dropped"]
  open_count = doing + todo
  return {
      "doing": doing,
      "todo": todo,
      "done": done,
      "dropped": dropped,
      "open": open_count,
      "total": done + open_count,
      "openItems": open_items,
  }


# Writes jetski-roadmaps.json to the roadmaps root and mirrors it into brain/ for /static/artifacts/
def export_roadmap_index() -> dict:
  root = roadmaps_root()
  projects: dict[str, dict] = {}
  if os.path.isdir(root):
    for entry in sorted(os.listdir(root)):
      if not SAFE_ID_RE.match(entry):
        continue
      rfile = os.path.join(root, entry, "roadmap.json")
      if not os.path.isfile(rfile):
        continue
      try:
        doc = load(entry)
      except (RoadmapError, OSError):
        continue
      projects[entry] = summarize_doc(doc)
  payload = {"version": SCHEMA_VERSION, "projects": projects}
  raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
  targets = [os.path.join(root, ROADMAP_INDEX_FILE_NAME)]
  for app_dir in find_app_data_dirs():
    targets.append(os.path.join(app_dir, "brain", ROADMAP_INDEX_FILE_NAME))
  for dst in targets:
    try:
      os.makedirs(os.path.dirname(dst), exist_ok=True)
      tmp = f"{dst}.tmp.{os.getpid()}"
      with open(tmp, "w", encoding="utf-8") as f:
        f.write(raw)
      os.replace(tmp, dst)
    except OSError:
      pass
  return payload


def _format_id_ranges(ids: list[str]) -> str:
  runs: list[tuple[str, int, int]] = []
  for iid in ids:
    head, sep, tail = iid.rpartition(".")
    prefix = f"{head}." if sep else "R"
    num = int(tail if sep else iid[1:])
    if runs and runs[-1][0] == prefix and runs[-1][2] + 1 == num:
      runs[-1] = (prefix, runs[-1][1], num)
    else:
      runs.append((prefix, num, num))
  return ", ".join(
      f"{p}{s}" if s == e else f"{p}{s}–{p}{e}" for p, s, e in runs
  )


def render(doc: dict, show_all: bool = False) -> str:
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

  def is_closed_subtree(it: dict) -> bool:
    return it["status"] in CLOSED_STATUSES and all(
        d["status"] in CLOSED_STATUSES for d in descendants(it["id"])
    )

  def subtree_updated_at(it: dict) -> str:
    return max(
        [it.get("updated_at") or ""]
        + [d.get("updated_at") or "" for d in descendants(it["id"])]
    )

  lines = [
      f"Roadmap ({len(items)}): {counts['done']} done · {counts['doing']} doing"
      f" · {counts['todo']} todo · {counts['dropped']} dropped"
  ]

  def emit_list(parent_id: str, depth: int) -> None:
    siblings = children.get(parent_id, [])
    closed = [(idx, it) for idx, it in enumerate(siblings) if is_closed_subtree(it)]
    hidden_ids: set[str] = set()
    hidden_order: list[str] = []
    # Keep the most recently updated closed items visible so the handoff summary
    # still shows what just finished even if an early item was closed out of order
    if not show_all and len(closed) > CLOSED_COLLAPSE_THRESHOLD:
      ranked = sorted(closed, key=lambda pair: (subtree_updated_at(pair[1]), pair[0]))
      hidden_ids = {it["id"] for _, it in ranked[:-CLOSED_KEEP_RECENT]}
      hidden_order = [it["id"] for _, it in closed if it["id"] in hidden_ids]
    emitted_summary = False
    for it in siblings:
      if it["id"] in hidden_ids:
        if not emitted_summary:
          emitted_summary = True
          ranges = _format_id_ranges(hidden_order)
          hint = "; run `roadmap.py show --all`" if depth == 0 else ""
          lines.append(
              f"{'  ' * depth}… {len(hidden_order)} closed items hidden ({ranges}{hint})"
          )
        continue
      emit(it, depth)

  def emit(it: dict, depth: int) -> None:
    line = f"{'  ' * depth}{GLYPHS[it['status']]} {it['id']} {it['text']}"
    if it["status"] == "doing" and it.get("owner"):
      line += f" [{it['owner'][:8]}]"
    if it.get("link"):
      line += f" → {it['link'][:8]}"
    desc = descendants(it["id"])
    # A finished epic with no open sub-items carries no actionable detail
    if not show_all and depth == 0 and it["status"] in CLOSED_STATUSES and desc and all(
        d["status"] in CLOSED_STATUSES for d in desc
    ):
      lines.append(f"{line} (+{len(desc)})")
      return
    lines.append(line)
    emit_list(it["id"], depth + 1)

  emit_list("", 0)
  return "\n".join(lines)


def show(project_id: str, show_all: bool = False) -> str:
  return render(load(project_id), show_all=show_all)


def _is_ignored_internal_path(path: str) -> bool:
  if not path:
    return True
  clean = os.path.realpath(os.path.expanduser(path.strip().strip('"').strip("'")))
  from context_guard import app_data_dir_from_path

  if app_data_dir_from_path(clean):
    return True
  norm = clean.replace("\\", "/")
  if any(
      seg in norm
      for seg in (
          "/.gemini/config/roadmaps/",
          "/.gemini/config/logs/",
          "/.gemini/config/projects/",
      )
  ):
    return True
  base = os.path.basename(clean)
  if base.startswith((".cross_project_", ".parallel_chat_", ".context_guard_")):
    return True
  if norm.startswith("/tmp/jetski_") or norm.startswith("/tmp/handoff_summary"):
    return True
  return False


def _find_transcript_path(conv_id: str, transcript_path: str = "") -> str:
  if transcript_path and os.path.isfile(transcript_path):
    return os.path.realpath(transcript_path)
  env_p = os.environ.get("JETSKI_TRANSCRIPT_PATH", "").strip()
  if env_p and os.path.isfile(env_p):
    return os.path.realpath(env_p)
  if not conv_id:
    return ""
  for ad in find_app_data_dirs():
    cand = os.path.join(
        ad, "brain", conv_id, ".system_generated", "logs", "transcript.jsonl"
    )
    if os.path.isfile(cand):
      return os.path.realpath(cand)
  return ""


def _collect_chat_vcs_inputs(
    conv_id: str, project_id: str, transcript_path: str
) -> tuple[set[str], list[str], list[str]]:
  from pre_tool_guard import (
      EDIT_TOOLS,
      _analyze_git_add_args,
      _analyze_git_commit_args,
      _extract_cmd_target_dirs,
      _parse_git_subcommand_invocations,
      get_tool_target_file,
      strip_heredocs,
  )
  from stop_guard import is_inspection_command

  own_touched_files: set[str] = set()
  candidate_dirs: list[str] = []
  non_inspection_cmds: list[str] = []

  if transcript_path and os.path.isfile(transcript_path):
    try:
      with open(transcript_path, "r", encoding="utf-8") as f:
        for line in f:
          if not line.strip() or "PLANNER_RESPONSE" not in line:
            continue
          try:
            obj = json.loads(line)
          except Exception:
            continue
          if obj.get("type") != "PLANNER_RESPONSE":
            continue
          for tc in obj.get("tool_calls") or []:
            tname = tc.get("name", "")
            targs = tc.get("args") or {}
            if tname in EDIT_TOOLS:
              tf = get_tool_target_file(targs)
              if tf and not _is_ignored_internal_path(tf):
                p = os.path.realpath(os.path.expanduser(tf.strip().strip('"').strip("'")))
                own_touched_files.add(p)
                candidate_dirs.append(os.path.dirname(p))
            elif tname == "run_command":
              cmd = str(targs.get("CommandLine", "")).strip()
              if not cmd or is_inspection_command(cmd):
                continue
              cmd_clean = strip_heredocs(cmd)
              if re.search(
                  r"^(?:python3?\s+)?(?:\S*/)?(?:roadmap|chat_lifecycle)\.py\b",
                  cmd_clean.strip(),
              ):
                continue
              non_inspection_cmds.append(cmd)
              for d in _extract_cmd_target_dirs(cmd, []):
                if d and not _is_ignored_internal_path(d) and os.path.isdir(d):
                  candidate_dirs.append(os.path.realpath(d))
              for a_args in _parse_git_subcommand_invocations(cmd, "add"):
                _, paths = _analyze_git_add_args(a_args)
                for p in paths:
                  if not os.path.isabs(p):
                    for cd in reversed(candidate_dirs):
                      cand_p = os.path.join(cd, p)
                      if os.path.exists(cand_p):
                        p = cand_p
                        break
                  if os.path.isabs(p) and not _is_ignored_internal_path(p):
                    own_touched_files.add(os.path.realpath(p))
              for c_args in _parse_git_subcommand_invocations(cmd, "commit"):
                _, paths = _analyze_git_commit_args(c_args)
                for p in paths:
                  if not os.path.isabs(p):
                    for cd in reversed(candidate_dirs):
                      cand_p = os.path.join(cd, p)
                      if os.path.exists(cand_p):
                        p = cand_p
                        break
                  if os.path.isabs(p) and not _is_ignored_internal_path(p):
                    own_touched_files.add(os.path.realpath(p))
    except Exception:
      pass

  if project_id and conv_id:
    try:
      sf = state_path(project_id, conv_id)
      if os.path.isfile(sf):
        with open(sf, "r", encoding="utf-8", errors="ignore") as f:
          txt = f.read()
        mod_idx = txt.find("### Files Modified by Tools in This Conversation")
        if mod_idx >= 0:
          mod_end = txt.find("\n### ", mod_idx + 10)
          mod_chunk = txt[mod_idx : mod_end if mod_end >= 0 else len(txt)]
          for m in re.findall(r"file://(/[^\s`\"')#]+)", mod_chunk):
            if not _is_ignored_internal_path(m) and os.path.exists(m):
              rm = os.path.realpath(m)
              own_touched_files.add(rm)
              candidate_dirs.append(os.path.dirname(rm))
        for gm in re.findall(r"### Git Status in `([^`]+)`", txt):
          clean_g = os.path.realpath(os.path.expanduser(gm.strip()))
          if os.path.isdir(clean_g) and not _is_ignored_internal_path(clean_g):
            candidate_dirs.append(clean_g)
    except Exception:
      pass

  if os.environ.get("JETSKI_VCS_BATON_REPOS"):
    for part in os.environ["JETSKI_VCS_BATON_REPOS"].split(":"):
      if part.strip():
        rp = os.path.realpath(os.path.expanduser(part.strip()))
        if os.path.isdir(rp):
          candidate_dirs.append(rp)

  return own_touched_files, candidate_dirs, non_inspection_cmds


def _inspect_repo_vcs_baton(
    top_level: str,
    git_dir: str,
    git_common_dir: str,
    conv_id: str,
    transcript_path: str,
    own_touched_files: set[str],
    non_inspection_cmds: list[str],
) -> str:
  from pre_tool_guard import _active_foreign_file_leases, _sync_repo_commits

  reg = _sync_repo_commits(top_level, git_common_dir)
  own_touched_rels = {
      os.path.relpath(p, top_level)
      for p in own_touched_files
      if p == top_level or p.startswith(top_level + os.sep)
  }
  lease_file = os.path.join(git_dir, "jetski_file_leases.json")
  if os.path.isfile(lease_file):
    try:
      with open(lease_file, "r", encoding="utf-8") as f:
        ldata = json.load(f)
      for rel, info in (ldata.get("files") or {}).items():
        if isinstance(info, dict) and info.get("conv_id") == conv_id:
          own_touched_rels.add(rel)
    except Exception:
      pass

  foreign_leased = _active_foreign_file_leases(git_dir, conv_id, transcript_path)
  own_dirty: list[str] = []
  foreign_dirty: list[str] = []

  try:
    st_res = subprocess.run(
        ["git", "-C", top_level, "status", "--porcelain", "-uall"],
        capture_output=True,
        text=True,
        timeout=2.0,
        check=False,
    )
    if st_res.returncode == 0:
      for ln in st_res.stdout.splitlines():
        if len(ln) > 3:
          raw_p = ln[3:].strip().split(" -> ")[-1].strip('"')
          if not raw_p or _is_ignored_internal_path(os.path.join(top_level, raw_p)):
            continue
          if raw_p in own_touched_rels:
            own_dirty.append(raw_p)
          elif raw_p not in foreign_leased and any(
              raw_p in c or (len(os.path.basename(raw_p)) >= 4 and os.path.basename(raw_p) in c)
              for c in non_inspection_cmds
          ):
            own_dirty.append(raw_p)
            own_touched_rels.add(raw_p)
          else:
            foreign_dirty.append(raw_p)
  except Exception:
    pass

  branch = "HEAD"
  try:
    b_res = subprocess.run(
        ["git", "-C", top_level, "symbolic-ref", "--short", "-q", "HEAD"],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    if b_res.returncode == 0 and b_res.stdout.strip():
      branch = b_res.stdout.strip()
  except Exception:
    pass

  head_sha = ""
  try:
    h_res = subprocess.run(
        ["git", "-C", top_level, "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    if h_res.returncode == 0:
      head_sha = h_res.stdout.strip()
  except Exception:
    pass
  head_short = head_sha[:7] if head_sha else "unknown"

  cfg: dict[str, str] = {}
  try:
    c_res = subprocess.run(
        ["git", "-C", top_level, "config", "--local", "-l"],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    if c_res.returncode == 0:
      for ln in c_res.stdout.splitlines():
        if "=" in ln:
          k, v = ln.split("=", 1)
          cfg[k.strip().lower()] = v.strip()
  except Exception:
    pass

  is_gerrit = (
      cfg.get("gerrit.host", "").lower() == "true"
      or "googlesource.com" in cfg.get("remote.origin.url", "").lower()
      or bool(
          cfg.get(f"branch.{branch}.gerritissue")
          or cfg.get(f"branch.{branch}.gerritserver")
      )
  )

  upstream_ref = ""
  try:
    u_res = subprocess.run(
        ["git", "-C", top_level, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        capture_output=True,
        text=True,
        timeout=1.0,
        check=False,
    )
    if u_res.returncode == 0 and u_res.stdout.strip():
      upstream_ref = u_res.stdout.strip()
  except Exception:
    pass

  if not upstream_ref and branch != "HEAD":
    try:
      r_res = subprocess.run(
          ["git", "-C", top_level, "rev-parse", "--verify", f"refs/remotes/origin/{branch}"],
          capture_output=True,
          text=True,
          timeout=1.0,
          check=False,
      )
      if r_res.returncode == 0:
        upstream_ref = f"origin/{branch}"
    except Exception:
      pass

  if not upstream_ref:
    base_up = cfg.get(f"branch.{branch}.base-upstream", "")
    if base_up:
      upstream_ref = base_up.replace("refs/remotes/", "")

  if not upstream_ref:
    for cand in ("origin/main", "origin/master"):
      try:
        cand_res = subprocess.run(
            ["git", "-C", top_level, "rev-parse", "--verify", f"refs/remotes/{cand}"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        if cand_res.returncode == 0:
          upstream_ref = cand
          break
      except Exception:
        pass

  own_unpushed: list[tuple[str, str]] = []
  foreign_unpushed: list[tuple[str, str]] = []
  if upstream_ref and head_sha:
    try:
      log_res = subprocess.run(
          ["git", "-C", top_level, "log", "--format=%H%x00%s", f"{upstream_ref}..HEAD"],
          capture_output=True,
          text=True,
          timeout=1.5,
          check=False,
      )
      if log_res.returncode == 0:
        commits_map = reg.get("commits") or {}
        for ln in log_res.stdout.splitlines():
          if "\x00" not in ln:
            continue
          sha, subj = [x.strip() for x in ln.split("\x00", 1)]
          if not sha:
            continue
          cinfo = commits_map.get(sha)
          if isinstance(cinfo, dict):
            owner = str(cinfo.get("conv_id") or "")
            if owner == conv_id:
              own_unpushed.append((sha, subj))
            elif owner:
              foreign_unpushed.append((sha, subj))
            else:
              own_unpushed.append((sha, subj))
          elif foreign_leased:
            dt_res = subprocess.run(
                ["git", "-C", top_level, "diff-tree", "--no-commit-id", "--name-only", "-r", sha],
                capture_output=True,
                text=True,
                timeout=1.0,
                check=False,
            )
            c_files = (
                [x.strip() for x in dt_res.stdout.splitlines() if x.strip()]
                if dt_res.returncode == 0
                else []
            )
            if c_files and all(f in foreign_leased for f in c_files) and not any(
                f in own_touched_rels for f in c_files
            ):
              foreign_unpushed.append((sha, subj))
            else:
              own_unpushed.append((sha, subj))
          else:
            own_unpushed.append((sha, subj))
    except Exception:
      pass

  if is_gerrit:
    issue = cfg.get(f"branch.{branch}.gerritissue", "").strip()
    patchset = cfg.get(f"branch.{branch}.gerritpatchset", "").strip()
    last_upload = cfg.get(f"branch.{branch}.last-upload-hash", "").strip()
    ps_label = f" PS {patchset}" if patchset else ""
    cl_label = f"cl/{issue}{ps_label}" if issue else ""
    is_uploaded = bool(issue and ((not last_upload) or (last_upload == head_sha)))
    if issue:
      if is_uploaded:
        gerrit_str = f"{cl_label} (HEAD {head_short} uploaded)"
      else:
        gerrit_str = f"{cl_label} (stale: HEAD {head_short} != uploaded {last_upload[:7]})"
    else:
      if own_unpushed or own_dirty:
        gerrit_str = "not uploaded"
      else:
        gerrit_str = f"none (HEAD {head_short})"

    if issue and is_uploaded:
      unpushed_str = f"uploaded to {cl_label}"
      is_pending = bool(own_dirty)
    else:
      unpushed_str = (
          ", ".join(f'{s[:7]} ("{subj}")' for s, subj in own_unpushed[:3])
          if own_unpushed
          else "none"
      )
      is_pending = bool(own_dirty or own_unpushed or (issue and not is_uploaded))

    if is_pending:
      action_str = (
          'call ask_question ("Commit + push" / "Commit only" / "Not now"; '
          'Gerrit = amnesia-precommit + corpus gate + git cl upload, never CQ)'
      )
    else:
      action_str = f"report to user in plain text: already uploaded ({cl_label or head_short})"

  else:
    gerrit_str = "n/a"
    unpushed_str = (
        ", ".join(f'{s[:7]} ("{subj}")' for s, subj in own_unpushed[:3])
        if own_unpushed
        else f"none ({head_short} → {upstream_ref or 'local'})"
    )
    is_pending = bool(own_dirty or own_unpushed)
    if is_pending:
      action_str = 'call ask_question ("Commit + push" / "Commit only" / "Not now")'
    else:
      action_str = f"report to user in plain text: already committed and pushed ({head_short} → {upstream_ref or 'local'})"

  status_word = "PENDING" if is_pending else "CLEAN"
  own_str = ", ".join(own_dirty[:8]) if own_dirty else "none"
  foreign_str = ", ".join(foreign_dirty[:8]) if foreign_dirty else "none"
  return (
      f"[VCS BATON: {status_word} — repo={top_level}; own_dirty={own_str}; "
      f"foreign_dirty={foreign_str}; unpushed={unpushed_str}; gerrit={gerrit_str}; "
      f"action: {action_str}]"
  )


def evaluate_vcs_baton(
    conv_id: str, project_id: str = "", transcript_path: str = ""
) -> str:
  tpath = _find_transcript_path(conv_id, transcript_path)
  pid = project_id or (project_id_for(conv_id) if conv_id else "")
  own_touched_files, candidate_dirs, non_inspection_cmds = _collect_chat_vcs_inputs(
      conv_id, pid, tpath
  )

  from pre_tool_guard import _resolve_git_dirs

  seen_tops: set[str] = set()
  resolved_repos: list[tuple[str, str, str]] = []
  for d in candidate_dirs:
    info = _resolve_git_dirs(d)
    if info and info[0] not in seen_tops:
      seen_tops.add(info[0])
      resolved_repos.append(info)

  env_repos = set()
  if os.environ.get("JETSKI_VCS_BATON_REPOS"):
    for part in os.environ["JETSKI_VCS_BATON_REPOS"].split(":"):
      if part.strip():
        env_repos.add(os.path.realpath(os.path.expanduser(part.strip())))

  touched_repos: list[tuple[str, str, str]] = []
  for top_level, git_dir, git_common_dir in resolved_repos:
    if top_level in env_repos:
      touched_repos.append((top_level, git_dir, git_common_dir))
      continue
    if any(p == top_level or p.startswith(top_level + os.sep) for p in own_touched_files):
      touched_repos.append((top_level, git_dir, git_common_dir))
      continue
    lease_file = os.path.join(git_dir, "jetski_file_leases.json")
    if os.path.isfile(lease_file):
      try:
        with open(lease_file, "r", encoding="utf-8") as f:
          ldata = json.load(f)
        if any(
            isinstance(v, dict) and v.get("conv_id") == conv_id
            for v in (ldata.get("files") or {}).values()
        ):
          touched_repos.append((top_level, git_dir, git_common_dir))
          continue
      except Exception:
        pass
    wt_lease = os.path.join(git_dir, "jetski_lease.json")
    if os.path.isfile(wt_lease):
      try:
        with open(wt_lease, "r", encoding="utf-8") as f:
          wdata = json.load(f)
        if wdata.get("conv_id") == conv_id:
          touched_repos.append((top_level, git_dir, git_common_dir))
          continue
      except Exception:
        pass
    commits_file = os.path.join(git_common_dir, "jetski_commits.json")
    if os.path.isfile(commits_file):
      try:
        with open(commits_file, "r", encoding="utf-8") as f:
          cdata = json.load(f)
        if any(
            isinstance(v, dict) and v.get("conv_id") == conv_id and v.get("repo") == top_level
            for v in (cdata.get("commits") or {}).values()
        ):
          touched_repos.append((top_level, git_dir, git_common_dir))
          continue
      except Exception:
        pass

  if not touched_repos:
    return "[VCS BATON: CLEAN — no git repositories touched in this chat]"

  lines = [
      _inspect_repo_vcs_baton(
          top_level,
          git_dir,
          git_common_dir,
          conv_id,
          tpath,
          own_touched_files,
          non_inspection_cmds,
      )
      for top_level, git_dir, git_common_dir in touched_repos
  ]
  return "\n".join(lines)


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
    # Emit baton notice on stdout so CLI callers know whether to propose next-task
    msg, warning, baton_notice = set_status_with_baton(pid, args[0], args[1], args[2])
    if warning:
      print(warning, file=sys.stderr)
    print(msg)
    if args[2] == "done":
      vcs_notice = evaluate_vcs_baton(args[0], pid)
      if vcs_notice:
        print(vcs_notice)
    if baton_notice:
      print(baton_notice)
  elif cmd == "link" and len(args) == 3:
    pid = require_project_id(args[0])
    print(link_item(pid, args[0], args[1], args[2]))
  elif cmd == "edit" and len(args) == 3:
    pid = require_project_id(args[0])
    print(edit_item(pid, args[0], args[1], args[2]))
  elif cmd == "reorder" and len(args) >= 2:
    pid = require_project_id(args[0])
    print(reorder_items(pid, args[0], args[1:]))
  # --before/--after take a target ID and --top/--bottom take none; anything else is
  # a usage error (exit 2), while an unknown --flag reaches move_item and fails as bad input
  elif (
      cmd == "move"
      and len(args) in (3, 4)
      and args[2].startswith("--")
      and (len(args) == 4) == (args[2] in ("--before", "--after"))
  ):
    pid = require_project_id(args[0])
    print(move_item(pid, args[0], args[1], args[2][2:], args[3] if len(args) == 4 else ""))
  elif cmd == "show":
    show_all = "--all" in args
    rest = [a for a in args if a != "--all"]
    if len(rest) > 1 or len(args) - len(rest) > 1:
      print(f"roadmap.py: bad arguments for '{cmd}'\n", file=sys.stderr)
      print(USAGE, file=sys.stderr, end="")
      return 2
    cid = (
        _check_safe_id("conversation id", rest[0])
        if rest
        else os.environ.get("ANTIGRAVITY_CONVERSATION_ID", "")
    )
    pid = require_project_id(cid)
    print(show(pid, show_all=show_all))
  elif cmd == "state-path" and len(args) == 1:
    pid = require_project_id(args[0])
    path = state_path(pid, args[0])
    os.makedirs(os.path.dirname(path), exist_ok=True)
    print(path)
  elif cmd == "state-move" and len(args) == 2:
    pid = require_project_id(args[0])
    print(state_move(pid, args[0], args[1]))
  # Allow manual or sync-script refresh of jetski-roadmaps.json without mutating any project
  elif cmd == "export-index" and not args:
    payload = export_roadmap_index()
    print(f"exported {len(payload['projects'])} project roadmap(s)")
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
