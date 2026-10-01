#!/usr/bin/env python3
"""Move conversations into a specified project in Jetski / Antigravity.

Updates:
1. `~/.gemini/jetski/conversations/<cid>.db` (canonical trajectory metadata proto blob)
2. `~/.gemini/jetski/conversation_summaries.db` (SQLite summaries cache)
3. `~/.gemini/jetski/jetbox_summaries_proto.pb` (in-memory state persistence proto)
4. Optionally restarts `jetski-hub.service` safely in the background via systemd-run.
"""

import argparse
import os
import sqlite3
import subprocess
import sys


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
  """Updates project_id in conversation_summaries.db."""
  sum_db = os.path.join(app_data_dir, "conversation_summaries.db")
  if not os.path.isfile(sum_db):
    return 0

  try:
    conn = sqlite3.connect(sum_db, timeout=5.0)
    cur = conn.cursor()
    updated = 0
    for cid in conv_ids:
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


def schedule_daemon_restart(delay_sec: int = 2):
  """Schedules a restart of jetski-hub.service in a separate systemd unit."""
  cmd = (
      f"/bin/bash -c 'sleep {delay_sec} && systemctl --user restart"
      " jetski-hub.service'"
  )
  try:
    subprocess.run(
        ["systemd-run", "--user", "/bin/bash", "-c", cmd],
        check=True,
        capture_output=True,
        text=True,
    )
    print(
        f"[INFO] Scheduled jetski-hub daemon restart in {delay_sec}s via"
        " systemd-run."
    )
  except Exception as e:
    print(f"[WARN] Failed to schedule systemd restart: {e}", file=sys.stderr)


def main():
  parser = argparse.ArgumentParser(
      description="Move conversations into a project in Jetski / Antigravity"
  )
  parser.add_argument(
      "--project-id",
      required=True,
      help="Target project ID (e.g. a29f214d-9fb4-46fd-a8f6-7d60a7e2aaa3)",
  )
  parser.add_argument(
      "--restart",
      action="store_true",
      help="Schedule jetski-hub daemon restart to apply immediately to UI",
  )
  parser.add_argument(
      "conversations", nargs="+", help="One or more conversation IDs to move"
  )
  args = parser.parse_args()

  app_data_dir = os.path.expanduser(
      os.environ.get("ANTIGRAVITY_APP_DATA_DIR", "~/.gemini/jetski")
  )
  if not os.path.isdir(app_data_dir):
    print(f"[ERROR] App data dir not found: {app_data_dir}", file=sys.stderr)
    sys.exit(1)

  target_project = args.project_id
  conv_ids = list(dict.fromkeys(args.conversations))
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

  # 4. Optional daemon restart
  if args.restart:
    schedule_daemon_restart(delay_sec=2)

  print("Done!")


if __name__ == "__main__":
  main()
