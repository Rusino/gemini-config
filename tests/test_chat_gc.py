#!/usr/bin/env python3
"""Contract unit tests for 3-tier chat garbage collection and cold archive."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_HOOKS_DIR = os.path.join(_ROOT_DIR, "hooks")
if _HOOKS_DIR not in sys.path:
  sys.path.insert(0, _HOOKS_DIR)

import chat_gc


def _iso(dt: datetime) -> str:
  return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_json(path: str, obj: object) -> None:
  os.makedirs(os.path.dirname(path), exist_ok=True)
  with open(path, "w", encoding="utf-8") as f:
    json.dump(obj, f)


def _write_transcript(
    brain_dir: str,
    cid: str,
    created_at: datetime,
    user_text: str,
    agent_text: str = "",
    handoff_parent: str = "",
) -> str:
  log_dir = os.path.join(brain_dir, cid, ".system_generated", "logs")
  os.makedirs(log_dir, exist_ok=True)
  prompt = user_text
  if handoff_parent:
    prompt = (
        f"Continuing from conversation://{handoff_parent} "
        f"(handoff summary ~/.gemini/jetski/brain/{handoff_parent}/handoff_summary.md).\n"
        + user_text
    )
  lines = [
      json.dumps({
          "step_index": 0,
          "source": "USER_EXPLICIT",
          "type": "USER_INPUT",
          "status": "DONE",
          "created_at": _iso(created_at),
          "content": prompt,
      })
  ]
  if agent_text:
    lines.append(
        json.dumps({
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": _iso(created_at + timedelta(seconds=10)),
            "content": agent_text,
        })
    )
  tr_path = os.path.join(log_dir, "transcript.jsonl")
  with open(tr_path, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
  mtime = created_at.timestamp()
  os.utime(tr_path, (mtime, mtime))
  return tr_path


class ChatGcTest(unittest.TestCase):

  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory()
    self.base = self.tmp.name
    self.app_dir = os.path.join(self.base, "jetski")
    self.brain_dir = os.path.join(self.app_dir, "brain")
    self.conv_dir = os.path.join(self.app_dir, "conversations")
    self.ann_dir = os.path.join(self.app_dir, "annotations")
    self.cold_dir = os.path.join(self.app_dir, "cold_archive")
    self.roadmaps_dir = os.path.join(self.base, "roadmaps")
    self.retention_file = os.path.join(self.base, "projects_retention.json")
    self.log_file = os.path.join(self.base, "logs", "chat_gc.jsonl")
    for d in (self.brain_dir, self.conv_dir, self.ann_dir, self.roadmaps_dir):
      os.makedirs(d, exist_ok=True)
    self.now = datetime(2026, 10, 30, 12, 0, 0, tzinfo=timezone.utc)
    _write_json(
        self.retention_file,
        {
            "keep": {"skia-g3-roll-check": 1, "debug_skia_roll_issue": 3},
            "triage_hard_delete_projects": {"proj-roll-uuid": 30},
            "gc": {
                "scratch_gc_days": 7,
                "task_log_gc_days": 30,
                "trash_delete_days": 7,
                "cold_archive_days": 90,
            },
        },
    )

  def tearDown(self) -> None:
    self.tmp.cleanup()

  def _touch_conv_db(self, cid: str, dt: datetime, size_bytes: int = 4096) -> str:
    p = os.path.join(self.conv_dir, f"{cid}.db")
    with open(p, "wb") as f:
      f.write(b"x" * size_bytes)
    ts = dt.timestamp()
    os.utime(p, (ts, ts))
    return p

  def _touch_archived_pbtxt(self, cid: str, title: str = "Test chat") -> str:
    p = os.path.join(self.ann_dir, f"{cid}.pbtxt")
    with open(p, "w", encoding="utf-8") as f:
      f.write(f'title:"{title}"  archived:true\n')
    return p

  def test_tier1_scratch_gc_preserves_guard_and_protects_open_roadmap_and_active_chains(
      self,
  ) -> None:
    cid_clean = "11111111-1111-1111-1111-111111111111"
    cid_roadmap = "22222222-2222-2222-2222-222222222222"
    cid_chain_old = "33333333-3333-3333-3333-333333333333"
    cid_chain_new = "44444444-4444-4444-4444-444444444444"
    old_dt = self.now - timedelta(days=12)
    recent_dt = self.now - timedelta(days=2)

    for cid in (cid_clean, cid_roadmap, cid_chain_old):
      _write_transcript(self.brain_dir, cid, old_dt, "Bug repro", "Fixed layout")
      self._touch_conv_db(cid, old_dt)
      self._touch_archived_pbtxt(cid)
      sc = os.path.join(self.brain_dir, cid, "scratch")
      os.makedirs(os.path.join(sc, "build_dir"), exist_ok=True)
      with open(os.path.join(sc, "build_dir", "libskia.a"), "wb") as f:
        f.write(b"A" * 65536)
      with open(os.path.join(sc, ".context_guard_state.json"), "w", encoding="utf-8") as f:
        f.write('{"handed_off_to": ""}')
      with open(os.path.join(self.brain_dir, cid, "report.md"), "w", encoding="utf-8") as f:
        f.write("# Deliverable\n")

    # Link cid_chain_new (recent, visible) as continuation of cid_chain_old
    _write_transcript(
        self.brain_dir,
        cid_chain_new,
        recent_dt,
        "Part 2",
        "Done",
        handoff_parent=cid_chain_old,
    )
    self._touch_conv_db(cid_chain_new, recent_dt)

    # Mark cid_roadmap as referenced by an open todo item
    _write_json(
        os.path.join(self.roadmaps_dir, "projA", "roadmap.json"),
        {
            "items": [{
                "id": "R3",
                "status": "todo",
                "text": "FontCollection thread safety -> 22222222",
            }]
        },
    )

    summaries = {
        cid_clean: {
            "summary": "Clean archived",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        },
        cid_roadmap: {
            "summary": "Open roadmap archived",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        },
        cid_chain_old: {
            "summary": "Old piece of active chain",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        },
        cid_chain_new: {
            "summary": "Visible tail",
            "stepCount": 4,
            "lastModifiedTime": _iso(recent_dt),
            "annotations": {},
            "trajectoryMetadata": {"createdAt": _iso(recent_dt)},
        },
    }

    # Dry-run must not remove files
    dry = chat_gc.run_gc(
        apply=False,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=lambda _: None,
        active_cids_override=set(),
    )
    self.assertIn(cid_clean, dry["tier1_scratch"]["cids"])
    self.assertNotIn(cid_roadmap, dry["tier1_scratch"]["cids"])
    self.assertNotIn(cid_chain_old, dry["tier1_scratch"]["cids"])
    self.assertTrue(
        os.path.exists(os.path.join(self.brain_dir, cid_clean, "scratch", "build_dir"))
    )

    # Apply cleans cid_clean scratch while keeping .context_guard_state.json and report.md
    res = chat_gc.run_gc(
        apply=True,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=lambda _: None,
        active_cids_override=set(),
    )
    self.assertEqual(res["tier1_scratch"]["cids"], [cid_clean])
    self.assertFalse(
        os.path.exists(os.path.join(self.brain_dir, cid_clean, "scratch", "build_dir"))
    )
    self.assertTrue(
        os.path.isfile(
            os.path.join(self.brain_dir, cid_clean, "scratch", ".context_guard_state.json")
        )
    )
    self.assertTrue(os.path.isfile(os.path.join(self.brain_dir, cid_clean, "report.md")))
    self.assertTrue(
        os.path.exists(os.path.join(self.brain_dir, cid_roadmap, "scratch", "build_dir"))
    )
    self.assertTrue(
        os.path.exists(os.path.join(self.brain_dir, cid_chain_old, "scratch", "build_dir"))
    )

  def test_tier1_git_safety_preserves_dirty_checkout_and_saves_ahead_patch(self) -> None:
    origin_repo = os.path.join(self.base, "origin_skia.git")
    subprocess.run(["git", "init", "--bare", origin_repo], check=True, capture_output=True)
    seed_repo = os.path.join(self.base, "seed_skia")
    subprocess.run(["git", "clone", origin_repo, seed_repo], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", seed_repo, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", seed_repo, "config", "user.name", "Tester"], check=True)
    with open(os.path.join(seed_repo, "src.cpp"), "w", encoding="utf-8") as f:
      f.write("int x = 1;\n")
    subprocess.run(["git", "-C", seed_repo, "add", "src.cpp"], check=True)
    subprocess.run(
        ["git", "-C", seed_repo, "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", seed_repo, "push", "-u", "origin", "HEAD:main"],
        check=True,
        capture_output=True,
    )

    old_dt = self.now - timedelta(days=10)
    cid_dirty = "55555555-5555-5555-5555-555555555555"
    cid_ahead = "66666666-6666-6666-6666-666666666666"
    for cid in (cid_dirty, cid_ahead):
      _write_transcript(self.brain_dir, cid, old_dt, "Test", "Reply")
      self._touch_conv_db(cid, old_dt)
      self._touch_archived_pbtxt(cid)

    # Clone with uncommitted tracked edit + heavy out/ build dir
    dirty_clone = os.path.join(self.brain_dir, cid_dirty, "scratch", "skia")
    subprocess.run(
        ["git", "clone", "-b", "main", origin_repo, dirty_clone],
        check=True,
        capture_output=True,
    )
    with open(os.path.join(dirty_clone, "src.cpp"), "w", encoding="utf-8") as f:
      f.write("int x = 2; // uncommitted\n")
    os.makedirs(os.path.join(dirty_clone, "out", "Release"), exist_ok=True)
    with open(os.path.join(dirty_clone, "out", "Release", "dm"), "wb") as f:
      f.write(b"B" * 32768)

    # Standalone clone with clean working tree but 1 unpushed local commit
    ahead_clone = os.path.join(self.brain_dir, cid_ahead, "scratch", "skia")
    subprocess.run(
        ["git", "clone", "-b", "main", origin_repo, ahead_clone],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", ahead_clone, "config", "user.email", "test@example.com"], check=True
    )
    subprocess.run(["git", "-C", ahead_clone, "config", "user.name", "Tester"], check=True)
    with open(os.path.join(ahead_clone, "src.cpp"), "w", encoding="utf-8") as f:
      f.write("int x = 42;\n")
    subprocess.run(
        ["git", "-C", ahead_clone, "commit", "-am", "Local fix"],
        check=True,
        capture_output=True,
    )

    summaries = {
        cid: {
            "summary": "Git test",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        }
        for cid in (cid_dirty, cid_ahead)
    }

    res = chat_gc.run_gc(
        apply=True,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=lambda _: None,
        active_cids_override=set(),
    )

    # Dirty tracked edit must stay intact, while its out/ build directory is pruned
    self.assertTrue(os.path.isfile(os.path.join(dirty_clone, "src.cpp")))
    self.assertFalse(os.path.exists(os.path.join(dirty_clone, "out")))
    self.assertTrue(res["tier1_scratch"]["preserved_dirty_git"])

    # Clean ahead clone is exported to .saved_patches before removing the clone
    self.assertFalse(os.path.exists(ahead_clone))
    patch_dir = os.path.join(self.brain_dir, cid_ahead, "scratch", ".saved_patches")
    self.assertTrue(os.path.isdir(patch_dir))
    patches = os.listdir(patch_dir)
    self.assertEqual(len(patches), 1)
    with open(os.path.join(patch_dir, patches[0]), "r", encoding="utf-8") as f:
      self.assertIn("Local fix", f.read())

  def test_tier2_hard_deletes_trash_and_subagents_and_routine_triage(self) -> None:
    old_8d = self.now - timedelta(days=8)
    old_35d = self.now - timedelta(days=35)
    cid_tool = "77777777-7777-7777-7777-777777777777"
    cid_empty = "88888888-8888-8888-8888-888888888888"
    cid_roll = "99999999-9999-9999-9999-999999999999"
    cid_real = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    cid_sub = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

    _write_transcript(self.brain_dir, cid_tool, old_8d, "Debate arm", "Arm reply")
    _write_transcript(self.brain_dir, cid_empty, old_8d, "Accidental start", "")
    _write_transcript(self.brain_dir, cid_roll, old_35d, "Triage roll", "All green")
    _write_transcript(self.brain_dir, cid_real, old_35d, "Investigate FaceCache", "Root cause")
    for cid, dt in (
        (cid_tool, old_8d),
        (cid_empty, old_8d),
        (cid_roll, old_35d),
        (cid_real, old_35d),
        (cid_sub, old_8d),
    ):
      self._touch_conv_db(cid, dt)
      self._touch_archived_pbtxt(cid)

    # Subagent row in conversation_summaries.db (absent from GetAllCascadeTrajectories)
    sdb = os.path.join(self.app_dir, "conversation_summaries.db")
    conn = sqlite3.connect(sdb)
    with conn:
      conn.execute(
          "CREATE TABLE conversation_summaries ("
          "conversation_id text PRIMARY KEY, title text, step_count integer,"
          "last_modified_time text, status text, project_id text,"
          "parent_conversation_id text, not_fully_idle numeric)"
      )
      conn.execute(
          "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
          (cid_sub, "", 12, _iso(old_8d), "", "", cid_real, 0),
      )
    conn.close()

    summaries = {
        cid_tool: {
            "summary": "Hidden tool",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_8d),
            "annotations": {},
            "trajectoryMetadata": {
                "createdAt": _iso(old_8d),
                "sourceMetadata": {"tool": "agentapi"},
            },
        },
        cid_empty: {
            "summary": "Aborted",
            "stepCount": 1,
            "lastModifiedTime": _iso(old_8d),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_8d)},
        },
        cid_roll: {
            "summary": "Roll check",
            "stepCount": 6,
            "lastModifiedTime": _iso(old_35d),
            "annotations": {"archived": True},
            "trajectoryMetadata": {
                "createdAt": _iso(old_35d),
                "projectId": "proj-roll-uuid",
            },
        },
        cid_real: {
            "summary": "FaceCache race",
            "stepCount": 14,
            "lastModifiedTime": _iso(old_35d),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_35d)},
        },
    }

    deleted_rpcs: list[str] = []
    res = chat_gc.run_gc(
        apply=True,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=deleted_rpcs.append,
        active_cids_override=set(),
    )

    deleted_set = set(res["tier2_hard_delete"]["cids"])
    self.assertEqual(deleted_set, {cid_tool, cid_empty, cid_roll, cid_sub})
    self.assertNotIn(cid_real, deleted_set)
    for cid in (cid_tool, cid_empty, cid_roll, cid_sub):
      self.assertIn(cid, deleted_rpcs)
      self.assertFalse(os.path.exists(os.path.join(self.brain_dir, cid)))
      self.assertFalse(os.path.exists(os.path.join(self.conv_dir, f"{cid}.db")))
    self.assertTrue(os.path.exists(os.path.join(self.brain_dir, cid_real)))

  def test_tier3_evicts_closed_90d_chain_to_cold_archive_and_searches_fts5(self) -> None:
    old_100d = self.now - timedelta(days=100)
    old_95d = self.now - timedelta(days=95)
    c1 = "cccccccc-cccc-cccc-cccc-cccccccccccc"
    c2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"

    _write_transcript(
        self.brain_dir,
        c1,
        old_100d,
        "Why did SkGlyphClusterizer split ligatures?",
        "Rejected: per-glyph interpolation because clusters share glyphs across U+200B.",
    )
    _write_transcript(
        self.brain_dir,
        c2,
        old_95d,
        "Finish clusterizer CL",
        "Uploaded cl/1399999 with contract test.",
        handoff_parent=c1,
    )
    with open(os.path.join(self.brain_dir, c2, "design_notes.md"), "w", encoding="utf-8") as f:
      f.write("# Clusterizer invariant\nNever split default-ignorable codepoints.\n")

    pairs = (
        (c1, old_100d, "Clusterizer part 1"),
        (c2, old_95d, "Clusterizer part 2"),
    )
    for cid, dt, title in pairs:
      self._touch_conv_db(cid, dt)
      self._touch_archived_pbtxt(cid, title=title)

    summaries = {
        c1: {
            "summary": "Clusterizer part 1",
            "stepCount": 8,
            "lastModifiedTime": _iso(old_100d),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_100d), "projectId": "skp-proj"},
        },
        c2: {
            "summary": "Clusterizer part 2",
            "stepCount": 10,
            "lastModifiedTime": _iso(old_95d),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_95d), "projectId": "skp-proj"},
        },
    }

    deleted_rpcs: list[str] = []
    res = chat_gc.run_gc(
        apply=True,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=deleted_rpcs.append,
        active_cids_override=set(),
    )

    self.assertEqual(set(res["tier3_cold_archive"]["cids"]), {c1, c2})
    self.assertEqual(set(deleted_rpcs), {c1, c2})
    self.assertFalse(os.path.exists(os.path.join(self.brain_dir, c1)))
    self.assertFalse(os.path.exists(os.path.join(self.brain_dir, c2)))
    self.assertTrue(
        os.path.isfile(os.path.join(self.cold_dir, "artifacts", c2, "design_notes.md"))
    )

    # FTS5 cold search handles domain identifiers and punctuation safely
    hits = chat_gc.search_cold_archive("SkGlyphClusterizer U+200B", cold_dir=self.cold_dir)
    self.assertEqual(len(hits), 1)
    self.assertEqual(hits[0]["cascadeId"], c1)
    self.assertEqual(hits[0]["piece"], 1)
    self.assertEqual(hits[0]["chain_length"], 2)

    restored = chat_gc.get_cold_conversation(c1, cold_dir=self.cold_dir)
    self.assertEqual(restored["title"], "Clusterizer part 1")
    self.assertTrue(any("Rejected: per-glyph" in m["text"] for m in restored["messages"]))

  def test_dangling_and_non_uuid_handoff_references_handled_safely(self) -> None:
    old_dt = self.now - timedelta(days=10)
    c1 = "12121212-1212-1212-1212-121212121212"
    c2 = "34343434-3434-3434-3434-343434343434"

    # Non-UUID handoff parent string in transcript
    _write_transcript(
        self.brain_dir,
        c1,
        old_dt,
        "Prompt",
        "Reply",
        handoff_parent="parent-1",
    )
    self._touch_conv_db(c1, old_dt)
    self._touch_archived_pbtxt(c1)
    os.makedirs(os.path.join(self.brain_dir, c1, "scratch"), exist_ok=True)
    with open(os.path.join(self.brain_dir, c1, "scratch", "dummy.txt"), "w") as f:
      f.write("junk\n")

    # Dangling UUID in .context_guard_state.json pointing to non-existent conversation
    _write_transcript(self.brain_dir, c2, old_dt, "Prompt 2", "Reply 2")
    self._touch_conv_db(c2, old_dt)
    self._touch_archived_pbtxt(c2)
    guard_path = os.path.join(self.brain_dir, c2, "scratch", ".context_guard_state.json")
    # Self-loop or foreign-owner guard state copied into c2's scratch must not create a parent cycle
    _write_json(guard_path, {"handed_off_to": c2, "handoff_kind": "handoff"})
    parent_of, _, _ = chat_gc._scan_transcript_graph_in_dirs([self.app_dir])
    self.assertNotIn(c2, parent_of)
    _write_json(guard_path, {"owner": c1, "handed_off_to": c2, "handoff_kind": "handoff"})
    parent_of, _, _ = chat_gc._scan_transcript_graph_in_dirs([self.app_dir])
    self.assertNotIn(c2, parent_of)
    _write_json(
        guard_path,
        {"handed_off_to": "99999999-9999-9999-9999-999999999999", "handoff_kind": "handoff"},
    )
    with open(os.path.join(self.brain_dir, c2, "scratch", "dummy.txt"), "w") as f:
      f.write("junk 2\n")

    summaries = {
        c1: {
            "summary": "Non-UUID test",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        },
        c2: {
            "summary": "Dangling test",
            "stepCount": 4,
            "lastModifiedTime": _iso(old_dt),
            "annotations": {"archived": True},
            "trajectoryMetadata": {"createdAt": _iso(old_dt)},
        },
    }

    # Must run without KeyError or crash
    rep = chat_gc.run_gc(
        apply=False,
        app_dirs=[self.app_dir],
        roadmaps_dir=self.roadmaps_dir,
        retention_file=self.retention_file,
        cold_dir=self.cold_dir,
        log_file=self.log_file,
        now=self.now,
        ls_summaries=summaries,
        delete_rpc_fn=lambda _: None,
        active_cids_override=set(),
    )
    self.assertIn(c1, rep["tier1_scratch"]["cids"])
    self.assertIn(c2, rep["tier1_scratch"]["cids"])

  def test_chat_lifecycle_retention_rules_and_cli_dispatch(self) -> None:
    import io
    from unittest import mock
    import chat_lifecycle as cl

    with mock.patch.object(cl, "DEFAULT_PROJECT_RETENTION_FILE", self.retention_file):
      rules = cl.load_retention_rules()
      self.assertEqual(rules.get("skia-g3-roll-check"), 1)
      self.assertEqual(rules.get("debug_skia_roll_issue"), 3)

    # Test gc CLI dispatch
    buf = io.StringIO()
    with mock.patch("sys.stdout", buf), mock.patch(
        "chat_gc.run_gc", return_value={"mock": True}
    ) as mock_run:
      ret = cl.main(["gc", "--apply", "--scratch-days", "14"])
      self.assertEqual(ret, 0)
      mock_run.assert_called_once_with(
          apply=True, scratch_days=14, trash_days=None, cold_days=None
      )
      self.assertIn('"mock": true', buf.getvalue())


if __name__ == "__main__":
  unittest.main()
