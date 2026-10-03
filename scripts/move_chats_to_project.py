#!/usr/bin/env python3
"""Move conversations into a specified project in Jetski / Antigravity.

Updates:
1. `~/.gemini/jetski/conversations/<cid>.db` (canonical trajectory metadata proto blob)
2. `~/.gemini/jetski/conversation_summaries.db` (SQLite summaries cache)
3. `~/.gemini/jetski/jetbox_summaries_proto.pb` (in-memory state persistence proto)
4. Optionally restarts `jetski-hub.service` safely in the background via systemd-run.
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys

_HOOKS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "hooks")
)
if _HOOKS_DIR not in sys.path:
  sys.path.insert(0, _HOOKS_DIR)

import chat_lifecycle  # pylint: disable=g-import-not-at-top


def decode_varint(data: bytes, offset: int) -> tuple[int, int]:
  res = 0
  shift = 0
  while True:
    b = data[offset]
    offset += 1
    res |= (b & 0x7F) << shift
    if not (b & 0x80):
      break
    shift += 7
  return res, offset


def encode_varint(val: int) -> bytes:
  res = bytearray()
  while val > 0x7F:
    res.append((val & 0x7F) | 0x80)
    val >>= 7
  res.append(val & 0x7F)
  return bytes(res)


def parse_msg(data: bytes) -> list[tuple[int, int, int, any]]:
  """Parses a protobuf message into a list of (tag, field_num, wire_type, value)."""
  fields = []
  offset = 0
  while offset < len(data):
    tag, offset = decode_varint(data, offset)
    fn = tag >> 3
    wt = tag & 7
    if wt == 0:
      val, offset = decode_varint(data, offset)
      fields.append((tag, fn, wt, val))
    elif wt == 1:
      val = data[offset : offset + 8]
      offset += 8
      fields.append((tag, fn, wt, val))
    elif wt == 2:
      length, offset = decode_varint(data, offset)
      val = data[offset : offset + length]
      offset += length
      fields.append((tag, fn, wt, val))
    elif wt == 5:
      val = data[offset : offset + 4]
      offset += 4
      fields.append((tag, fn, wt, val))
    else:
      raise ValueError(f"Unknown wire type {wt} at {offset}")
  return fields


def serialize_msg(fields: list[tuple[int, int, int, any]]) -> bytes:
  """Serializes fields back into protobuf wire format."""
  out = bytearray()
  for tag, _, wt, val in fields:
    out.extend(encode_varint(tag))
    if wt == 0:
      out.extend(encode_varint(val))
    elif wt in (1, 5):
      out.extend(val)
    elif wt == 2:
      out.extend(encode_varint(len(val)))
      out.extend(val)
  return bytes(out)


def update_metadata_project(meta_bytes: bytes, new_project_id: str) -> bytes:
  """Updates or inserts project_id (field 18, wire type 2) in CortexTrajectoryMetadata."""
  meta_fields = parse_msg(meta_bytes) if meta_bytes else []
  new_meta_fields = []
  found = False
  for tag, fn, wt, val in meta_fields:
    if fn == 18 and wt == 2:
      new_val = new_project_id.encode("utf-8")
      new_meta_fields.append((tag, fn, wt, new_val))
      found = True
    else:
      new_meta_fields.append((tag, fn, wt, val))
  if not found:
    tag = (18 << 3) | 2
    new_meta_fields.append((tag, 18, 2, new_project_id.encode("utf-8")))
  return serialize_msg(new_meta_fields)


def update_summary_project(summary_bytes: bytes, new_project_id: str) -> bytes:
  """Updates trajectory_metadata (field 17) inside CascadeTrajectorySummary."""
  sum_fields = parse_msg(summary_bytes)
  new_sum_fields = []
  found_meta = False
  for tag, fn, wt, val in sum_fields:
    if fn == 17 and wt == 2:
      new_meta = update_metadata_project(val, new_project_id)
      new_sum_fields.append((tag, fn, wt, new_meta))
      found_meta = True
    else:
      new_sum_fields.append((tag, fn, wt, val))
  if not found_meta:
    meta_bytes = update_metadata_project(b"", new_project_id)
    tag = (17 << 3) | 2
    new_sum_fields.append((tag, 17, 2, meta_bytes))
  return serialize_msg(new_sum_fields)


def update_conversation_db(
    app_data_dir: str, conv_id: str, new_project_id: str
) -> bool:
  """Updates trajectory_metadata_blob in conversations/<cid>.db."""
  db_path = os.path.join(app_data_dir, "conversations", f"{conv_id}.db")
  if not os.path.isfile(db_path):
    print(f"[WARN] Conversation DB not found: {db_path}", file=sys.stderr)
    return False

  try:
    conn = sqlite3.connect(db_path, timeout=5.0)
    cur = conn.cursor()
    cur.execute('SELECT data FROM trajectory_metadata_blob WHERE id = "main"')
    row = cur.fetchone()
    old_data = row[0] if row else b""
    new_data = update_metadata_project(old_data, new_project_id)
    cur.execute(
        "INSERT INTO trajectory_metadata_blob (id, data) VALUES ('main', ?) "
        "ON CONFLICT(id) DO UPDATE SET data = excluded.data",
        (new_data,),
    )
    conn.commit()
    conn.close()
    return True
  except Exception as e:
    print(
        f"[ERROR] Failed to update conversation DB {conv_id}: {e}",
        file=sys.stderr,
    )
    return False


def update_summaries_db(
    app_data_dir: str, conv_ids: list[str], new_project_id: str
) -> int:
  """Updates project_id and raw_summary in conversation_summaries.db."""
  sum_db = os.path.join(app_data_dir, "conversation_summaries.db")
  if not os.path.isfile(sum_db):
    return 0

  try:
    conn = sqlite3.connect(sum_db, timeout=5.0)
    cur = conn.cursor()
    updated = 0
    for cid in conv_ids:
      cur.execute(
          "SELECT raw_summary FROM conversation_summaries WHERE"
          " conversation_id = ?",
          (cid,),
      )
      row = cur.fetchone()
      if row and row[0]:
        new_raw = update_summary_project(row[0], new_project_id)
        cur.execute(
            "UPDATE conversation_summaries SET project_id = ?, raw_summary = ?"
            " WHERE conversation_id = ?",
            (new_project_id, new_raw, cid),
        )
      else:
        cur.execute(
            "UPDATE conversation_summaries SET project_id = ? WHERE"
            " conversation_id = ?",
            (new_project_id, cid),
        )
      updated += cur.rowcount
    conn.commit()
    conn.close()
    return updated
  except Exception as e:
    print(
        f"[ERROR] Failed to update conversation_summaries.db: {e}",
        file=sys.stderr,
    )
    return 0


def update_summaries_proto(
    app_data_dir: str, conv_ids: set[str], new_project_id: str
) -> int:
  """Updates project_id in jetbox_summaries_proto.pb."""
  pb_path = os.path.join(app_data_dir, "jetbox_summaries_proto.pb")
  if not os.path.isfile(pb_path):
    print(f"[WARN] Summaries proto not found: {pb_path}", file=sys.stderr)
    return 0

  with open(pb_path, "rb") as f:
    data = f.read()

  top_fields = parse_msg(data)
  updated_count = 0
  new_top_fields = []

  for tag, fn, wt, val in top_fields:
    if fn == 1 and wt == 2:  # Map entry for summaries
      entry_fields = parse_msg(val)
      entry_key = None
      for _, efn, ewt, evalue in entry_fields:
        if efn == 1 and ewt == 2:
          entry_key = evalue.decode("utf-8", errors="ignore")
          break
      if entry_key in conv_ids:
        new_entry_fields = []
        for etag, efn, ewt, evalue in entry_fields:
          if efn == 2 and ewt == 2:
            new_val = update_summary_project(evalue, new_project_id)
            new_entry_fields.append((etag, efn, ewt, new_val))
          else:
            new_entry_fields.append((etag, efn, ewt, evalue))
        new_top_fields.append((tag, fn, wt, serialize_msg(new_entry_fields)))
        updated_count += 1
      else:
        new_top_fields.append((tag, fn, wt, val))
    else:
      new_top_fields.append((tag, fn, wt, val))

  new_bytes = serialize_msg(new_top_fields)
  tmp_path = pb_path + ".tmp"
  with open(tmp_path, "wb") as f:
    f.write(new_bytes)
  os.replace(tmp_path, pb_path)
  return updated_count


def resolve_project_id(project_arg: str, projects_dir: str = "") -> str:
  """Resolves a project name or UUID via chat_lifecycle.resolve_project_id."""
  return chat_lifecycle.resolve_project_id(
      project_arg, projects_dir=projects_dir
  )


def expand_conversations_with_chains(conv_ids: list[str]) -> list[str]:
  """Expands each conversation ID to include its full handoff chain (ancestors & descendants)."""
  expanded: list[str] = []
  seen: set[str] = set()
  for cid in conv_ids:
    if not cid:
      continue
    chain = chat_lifecycle.discover_conversation_chain(
        cid, include_descendants=True, respect_boundaries=True
    )
    for item in chain or [cid]:
      if item and item not in seen:
        seen.add(item)
        expanded.append(item)
  return expanded


def _load_known_project_names(projects_dir: str = "") -> dict[str, str]:
  """Returns mapping of project_id -> clean project name."""
  pdir = projects_dir or os.path.expanduser(
      os.environ.get(
          "ANTIGRAVITY_PROJECTS_DIR", chat_lifecycle.DEFAULT_PROJECTS_DIR
      )
  )
  names: dict[str, str] = {}
  if not os.path.isdir(pdir):
    return names
  for pfile in sorted(glob.glob(os.path.join(pdir, "*.json"))):
    try:
      with open(pfile, "r", encoding="utf-8") as f:
        pdata = json.load(f)
      if isinstance(pdata, dict):
        pid = str(pdata.get("id") or "").strip()
        pname = chat_lifecycle.strip_activity_badge(
            str(pdata.get("name") or "").strip()
        )
        if pid:
          names[pid] = pname or pid
    except (OSError, ValueError):
      continue
  return names


def list_unassigned_conversations(
    app_data_dir: str = "", projects_dir: str = ""
) -> list[dict]:
  """Returns a list of top-level chains that contain unassigned (outside-of-project) chats."""
  known_projects = _load_known_project_names(projects_dir=projects_dir)

  ls_summaries = chat_lifecycle._fetch_ls_summaries_for_audit()
  ls_online = ls_summaries is not None

  app_dirs = (
      [app_data_dir] if app_data_dir else chat_lifecycle.find_app_data_dirs()
  )
  archived_on_disk: set[str] = set()
  db_titles: dict[str, str] = {}
  db_projects: dict[str, str] = {}

  for adir in app_dirs:
    ann_dir = os.path.join(adir, chat_lifecycle.ANNOTATIONS_DIR_NAME)
    if os.path.isdir(ann_dir):
      for fn in sorted(os.listdir(ann_dir)):
        if not fn.endswith(".pbtxt"):
          continue
        cid = fn[:-6]
        try:
          with open(os.path.join(ann_dir, fn), "r", encoding="utf-8") as f:
            txt = f.read()
          if re.search(r"\barchived\s*:\s*true\b", txt):
            archived_on_disk.add(cid)
        except OSError:
          pass

    sum_db = os.path.join(adir, chat_lifecycle.SUMMARY_DB_NAME)
    if os.path.isfile(sum_db):
      try:
        conn = sqlite3.connect(f"file:{sum_db}?mode=ro", uri=True, timeout=2.0)
        cur = conn.cursor()
        try:
          cur.execute(
              "SELECT conversation_id, title, project_id FROM conversation_summaries"
          )
          for cid, title, pid in cur.fetchall():
            if cid not in db_titles and title:
              db_titles[cid] = str(title).strip()
            if cid not in db_projects and pid:
              db_projects[cid] = str(pid).strip()
        except sqlite3.OperationalError:
          cur.execute("SELECT conversation_id, title FROM conversation_summaries")
          for cid, title in cur.fetchall():
            if cid not in db_titles and title:
              db_titles[cid] = str(title).strip()
        conn.close()
      except Exception:
        pass

  raw_parent, _, created_at_of = chat_lifecycle._scan_transcript_graph()

  visible_ids: set[str] = set()
  ls_titles: dict[str, str] = {}
  ls_projects: dict[str, str] = {}
  if ls_online and ls_summaries is not None:
    for cid, s in ls_summaries.items():
      if not isinstance(s, dict):
        continue
      ann = s.get("annotations") or {}
      if ann.get("archived") or cid in archived_on_disk:
        continue
      meta = s.get("trajectoryMetadata") or {}
      if meta.get("parentConversationId") or meta.get("isBattleModeFork"):
        continue
      if (meta.get("sourceMetadata") or {}).get("tool"):
        continue
      visible_ids.add(cid)
      ls_t = str(ann.get("title") or s.get("summary") or "").strip()
      if ls_t:
        ls_titles[cid] = ls_t
      pid = str(meta.get("projectId") or "").strip()
      if pid:
        ls_projects[cid] = pid
      cat = str(meta.get("createdAt") or "").strip()
      if cat:
        created_at_of[cid] = cat
  else:
    for cid in set(db_titles.keys()) | set(db_projects.keys()):
      if cid not in archived_on_disk:
        visible_ids.add(cid)

  def _TitleOf(cid: str) -> str:
    return (
        chat_lifecycle.get_conversation_title(cid)
        or db_titles.get(cid)
        or ls_titles.get(cid)
        or ""
    )

  def _ProjectOf(cid: str) -> str:
    pid = ls_projects.get(cid) or db_projects.get(cid) or "outside-of-project"
    return pid if pid else "outside-of-project"

  def _IsUnassigned(cid: str) -> bool:
    pid = _ProjectOf(cid)
    if not pid or pid == "outside-of-project":
      return True
    if known_projects and pid not in known_projects:
      return True
    return False

  vis_parent: dict[str, str] = {}
  vis_children: dict[str, list[str]] = {}
  for cid in sorted(visible_ids):
    curr = raw_parent.get(cid)
    my_topic = chat_lifecycle.clean_base_title(_TitleOf(cid))
    seen_anc = {cid}
    while curr and curr not in seen_anc:
      seen_anc.add(curr)
      if curr in visible_ids:
        if chat_lifecycle._is_task_boundary(curr, cid):
          break
        p_topic = chat_lifecycle.clean_base_title(_TitleOf(curr))
        if (
            p_topic == my_topic
            or not my_topic
            or not p_topic
            or my_topic == "Investigation"
            or p_topic == "Investigation"
        ):
          vis_parent[cid] = curr
          vis_children.setdefault(curr, []).append(cid)
          break
      curr = raw_parent.get(curr)

  roots = sorted(
      (c for c in visible_ids if c not in vis_parent),
      key=lambda c: (created_at_of.get(c, ""), c),
  )

  results: list[dict] = []
  for root_id in roots:
    chain = chat_lifecycle._order_chain_from_root(
        root_id, vis_children, created_at_of, respect_boundaries=False
    )
    unassigned_ids = [c for c in chain if _IsUnassigned(c)]
    if not unassigned_ids:
      continue
    assigned_projects = sorted({
        known_projects.get(_ProjectOf(c), _ProjectOf(c))
        for c in chain
        if not _IsUnassigned(c)
    })
    tail_title = _TitleOf(chain[-1]) or _TitleOf(root_id)
    results.append({
        "root_id": root_id,
        "tail_id": chain[-1],
        "chain_ids": chain,
        "unassigned_ids": unassigned_ids,
        "chain_length": len(chain),
        "unassigned_count": len(unassigned_ids),
        "title": tail_title,
        "base_topic": chat_lifecycle.clean_base_title(tail_title),
        "assigned_projects_in_chain": assigned_projects,
    })
  return results


def format_unassigned_report(items: list[dict]) -> str:
  """Formats the output of list_unassigned_conversations for terminal display."""
  total_unassigned = sum(item["unassigned_count"] for item in items)
  lines = [
      f"Unassigned conversations: {total_unassigned} across {len(items)} chain(s)/task(s):"
  ]
  for item in items:
    clen = item["chain_length"]
    ucnt = item["unassigned_count"]
    root_id = item["root_id"]
    tail_id = item["tail_id"]
    title = item["title"] or "(untitled)"
    if clen == 1:
      chain_tag = "single   "
      id_desc = root_id
    elif ucnt == clen:
      chain_tag = f"chain({clen:2d})"
      id_desc = f"{root_id} .. {tail_id[:8]}"
    else:
      projs = ", ".join(item.get("assigned_projects_in_chain") or [])
      chain_tag = f"part({ucnt}/{clen})"
      id_desc = f"{', '.join(item['unassigned_ids'])} (rest in {projs})"
    lines.append(f"  [{chain_tag}] {id_desc}  —  {title}")
  return "\n".join(lines)


def get_running_conversations() -> list[tuple[str, str]]:
  """Returns (cid, title) for conversations currently in RUNNING status in LS."""
  summaries = chat_lifecycle._fetch_ls_summaries_for_audit()
  if not summaries:
    return []
  running: list[tuple[str, str]] = []
  for cid, info in summaries.items():
    if not isinstance(info, dict):
      continue
    status = str(info.get("status") or "")
    if "RUNNING" in status:
      ann = info.get("annotations") or {}
      title = str(ann.get("title") or info.get("summary") or "").strip()
      running.append((cid, title))
  return sorted(running)


def wait_for_idle_ls(
    poll_sec: float = 3.0,
    required_quiet_checks: int = 2,
    max_wait_sec: float = 3600.0,
) -> bool:
  """Blocks until no conversation is RUNNING in LS for required_quiet_checks polls."""
  import time  # pylint: disable=g-import-not-at-top

  deadline = time.monotonic() + max_wait_sec
  quiet = 0
  while time.monotonic() < deadline:
    running = get_running_conversations()
    if not running:
      quiet += 1
      if quiet >= required_quiet_checks:
        print("[INFO] All conversations are idle; proceeding with hub restart.")
        sys.stdout.flush()
        return True
    else:
      quiet = 0
      desc = ", ".join(f"{cid[:8]} ({t or 'untitled'})" for cid, t in running)
      print(f"[WAIT] Waiting for {len(running)} running chat(s) to finish: {desc}")
      sys.stdout.flush()
    time.sleep(poll_sec)
  print("[WARN] Timed out waiting for running chats to finish.", file=sys.stderr)
  return False


def schedule_offline_migration(
    script_path: str,
    project_id: str,
    conv_ids: list[str],
    delay_sec: int = 3,
):
  """Schedules waiting for idle chats, stopping jetski-hub, migrating offline, and restarting."""
  quoted_cids = " ".join(f"'{cid}'" for cid in conv_ids)
  abs_script = os.path.abspath(script_path)
  wait_cmd = f"/usr/bin/python3 '{abs_script}' --wait-idle"
  script_cmd = (
      f"/usr/bin/python3 '{abs_script}' --project-id '{project_id}'"
      f" {quoted_cids}"
  )
  log_file = "/tmp/move_chats_offline.log"
  cmd = (
      f"/bin/bash -c 'sleep {delay_sec} && "
      f"{wait_cmd} > {log_file} 2>&1 && "
      f"systemctl --user stop jetski-hub.service >> {log_file} 2>&1 && "
      f"{script_cmd} >> {log_file} 2>&1 && "
      f"systemctl --user start jetski-hub.service >> {log_file} 2>&1'"
  )
  try:
    subprocess.run(
        ["systemd-run", "--user", "/bin/bash", "-c", cmd],
        check=True,
        capture_output=True,
        text=True,
    )
    print(
        "[INFO] Scheduled offline migration via systemd-run (will wait until all"
        " running chats are idle before restarting jetski-hub)."
    )
    print(f"[INFO] Log will be written to {log_file}")
  except Exception as e:
    print(f"[WARN] Failed to schedule offline migration: {e}", file=sys.stderr)


def main():
  parser = argparse.ArgumentParser(
      description="Move conversations into a project in Jetski / Antigravity"
  )
  parser.add_argument(
      "--project",
      "--project-id",
      dest="project",
      default="",
      help=(
          "Target project name or UUID (e.g. 'WebParagraph' or"
          " a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3)"
      ),
  )
  parser.add_argument(
      "--projects-dir",
      default="",
      help="Optional path to projects directory (default: ~/.gemini/config/projects)",
  )
  parser.add_argument(
      "--chain",
      action="store_true",
      help=(
          "Automatically expand each conversation ID to its full handoff chain"
          " (ancestors and descendants)"
      ),
  )
  parser.add_argument(
      "--list-unassigned",
      action="store_true",
      help=(
          "Read-only: list all top-level conversations and chains not assigned"
          " to any project"
      ),
  )
  parser.add_argument(
      "--json",
      action="store_true",
      help="Output JSON when used with --list-unassigned",
  )
  parser.add_argument(
      "--restart",
      action="store_true",
      help="Schedule jetski-hub daemon restart once all chats are idle to apply to UI",
  )
  parser.add_argument(
      "--wait-idle",
      action="store_true",
      help="Internal helper: block until no conversation is RUNNING in LS",
  )
  parser.add_argument(
      "conversations",
      nargs="*",
      help="One or more conversation IDs to move",
  )
  args = parser.parse_args()

  if args.wait_idle:
    sys.exit(0 if wait_for_idle_ls() else 1)

  app_data_dir = os.path.expanduser(
      os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "~/.gemini/jetski")
  )

  if args.list_unassigned:
    items = list_unassigned_conversations(
        app_data_dir=app_data_dir if os.path.isdir(app_data_dir) else "",
        projects_dir=args.projects_dir,
    )
    if args.json:
      print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
      print(format_unassigned_report(items))
    return

  if not args.project:
    parser.error("--project / --project-id is required unless --list-unassigned is set")
  if not args.conversations:
    parser.error("at least one conversation ID is required")

  try:
    target_project = resolve_project_id(
        args.project, projects_dir=args.projects_dir
    )
  except ValueError as e:
    print(f"[ERROR] {e}", file=sys.stderr)
    sys.exit(2)

  conv_ids = list(dict.fromkeys(args.conversations))
  if args.chain:
    conv_ids = expand_conversations_with_chains(conv_ids)

  # If restart requested, run everything offline while hub daemon is stopped
  # so that in-memory cache flush on shutdown does not overwrite disk changes
  if args.restart:
    schedule_offline_migration(
        sys.argv[0], target_project, conv_ids, delay_sec=3
    )
    return

  if not os.path.isdir(app_data_dir):
    print(f"[ERROR] App data dir not found: {app_data_dir}", file=sys.stderr)
    sys.exit(1)

  print(
      f"Moving {len(conv_ids)} conversation(s) to project '{target_project}'..."
  )

  # 1. Update individual conversation DBs
  db_count = 0
  for cid in conv_ids:
    if update_conversation_db(app_data_dir, cid, target_project):
      db_count += 1
  print(f"[1/3] Updated {db_count}/{len(conv_ids)} conversation SQLite files.")

  # 2. Update conversation_summaries.db
  sum_count = update_summaries_db(app_data_dir, conv_ids, target_project)
  print(f"[2/3] Updated {sum_count} row(s) in conversation_summaries.db.")

  # 3. Update jetbox_summaries_proto.pb
  pb_count = update_summaries_proto(
      app_data_dir, set(conv_ids), target_project
  )
  print(f"[3/3] Updated {pb_count} entry/entries in jetbox_summaries_proto.pb.")

  print("Done!")


if __name__ == "__main__":
  main()
