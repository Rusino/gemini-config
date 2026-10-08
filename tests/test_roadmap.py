#!/usr/bin/env python3
"""Tests for roadmap.py."""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

HOOKS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "hooks"))
sys.path.insert(0, HOOKS_DIR)

import roadmap

ROADMAP_CLI = os.path.join(HOOKS_DIR, "roadmap.py")


class TestRoadmap(unittest.TestCase):

  def setUp(self):
    self.tmp = tempfile.TemporaryDirectory()
    self.env = patch.dict(
        os.environ,
        {roadmap.ROADMAPS_DIR_ENV: self.tmp.name, "ANTIGRAVITY_PROJECT_ID": "p1"},
    )
    self.env.start()

  def tearDown(self):
    self.env.stop()
    self.tmp.cleanup()

  def roadmap_file(self):
    return os.path.join(self.tmp.name, "p1", "roadmap.json")

  def items(self):
    return {it["id"]: it for it in roadmap.load("p1")["items"]}

  def test_ids_are_stable_and_never_reused(self):
    self.assertEqual(roadmap.add_item("p1", "c1", "First"), "R1 added: First")
    roadmap.add_item("p1", "c1", "Second")
    self.assertTrue(roadmap.add_item("p1", "c1", "Sub a", parent="R1").startswith("R1.1 "))
    self.assertTrue(roadmap.add_item("p1", "c1", "Sub b", parent="R1").startswith("R1.2 "))
    self.assertTrue(roadmap.add_item("p1", "c1", "Deep", parent="R1.2").startswith("R1.2.1 "))
    roadmap.set_status("p1", "c1", "R2", "dropped")
    self.assertTrue(roadmap.add_item("p1", "c1", "Third").startswith("R3 "))
    items = self.items()
    self.assertEqual(sorted(items), ["R1", "R1.1", "R1.2", "R1.2.1", "R2", "R3"])
    self.assertEqual(items["R1.2"]["parent"], "R1")
    self.assertEqual(items["R2"]["status"], "dropped")
    self.assertEqual(items["R1"]["created_by"], "c1")

  def test_text_is_collapsed_to_one_line(self):
    roadmap.add_item("p1", "c1", "  multi\n  line\ttext ")
    self.assertEqual(self.items()["R1"]["text"], "multi line text")

  def test_doing_claims_ownership_and_warns_on_takeover(self):
    roadmap.add_item("p1", "c1", "Task")
    _, warning = roadmap.set_status("p1", "c1", "R1", "doing")
    self.assertEqual(warning, "")
    self.assertEqual(self.items()["R1"]["owner"], "c1")
    msg, warning = roadmap.set_status("p1", "c2", "R1", "doing")
    self.assertEqual(msg, "R1 doing: Task")
    self.assertIn("c1", warning)
    self.assertEqual(self.items()["R1"]["owner"], "c2")
    roadmap.set_status("p1", "c2", "R1", "done")
    self.assertEqual(self.items()["R1"]["owner"], "")
    self.assertEqual(self.items()["R1"]["updated_by"], "c2")

  def test_link_records_chat(self):
    roadmap.add_item("p1", "c1", "Spin off")
    self.assertEqual(
        roadmap.link_item("p1", "c1", "R1", "chat-42"), "R1 linked to conversation://chat-42"
    )
    self.assertEqual(self.items()["R1"]["link"], "chat-42")

  def test_fail_fast_on_bad_input(self):
    roadmap.add_item("p1", "c1", "Task")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.set_status("p1", "c1", "R9", "done")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.set_status("p1", "c1", "R1", "finished")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.add_item("p1", "c1", "   ")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.add_item("p1", "c1", "Orphan", parent="R7")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.link_item("p1", "c1", "R1", "../escape")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.state_path("../p1", "c1")
    self.assertEqual(list(self.items()), ["R1"])

  def test_corrupted_file_is_never_overwritten(self):
    roadmap.add_item("p1", "c1", "Task")
    for broken in ("{not json", json.dumps({"version": 1, "next_id": 1, "items": [
        {"id": "R1", "parent": "", "text": "x", "status": "todo", "next_child": 1}]})):
      with self.subTest(broken=broken[:20]):
        with open(self.roadmap_file(), "w", encoding="utf-8") as f:
          f.write(broken)
        with self.assertRaises(roadmap.RoadmapError):
          roadmap.add_item("p1", "c1", "More")
        with open(self.roadmap_file(), encoding="utf-8") as f:
          self.assertEqual(f.read(), broken)

  def test_render_is_compact(self):
    roadmap.add_item("p1", "c1", "Epic done")
    roadmap.add_item("p1", "c1", "Sub done", parent="R1")
    roadmap.add_item("p1", "c1", "Sub dropped", parent="R1")
    roadmap.add_item("p1", "c1", "Epic open")
    roadmap.add_item("p1", "c1", "Sub doing", parent="R2")
    roadmap.add_item("p1", "c1", "Epic done with open child")
    roadmap.add_item("p1", "c1", "Open child", parent="R3")
    roadmap.add_item("p1", "c1", "Abandoned")
    roadmap.set_status("p1", "c1", "R1.1", "done")
    roadmap.set_status("p1", "c1", "R1.2", "dropped")
    roadmap.set_status("p1", "c1", "R1", "done")
    roadmap.set_status("p1", "0123456789abcdef", "R2.1", "doing")
    roadmap.set_status("p1", "c1", "R3", "done")
    roadmap.set_status("p1", "c1", "R4", "dropped")
    roadmap.link_item("p1", "c1", "R4", "fedcba9876543210")
    self.assertEqual(
        roadmap.show("p1").splitlines(),
        [
            "Roadmap (8): 3 done · 1 doing · 2 todo · 2 dropped",
            "✓ R1 Epic done (+2)",
            "☐ R2 Epic open",
            "  ▸ R2.1 Sub doing [01234567]",
            "✓ R3 Epic done with open child",
            "  ☐ R3.1 Open child",
            "✗ R4 Abandoned → fedcba98",
        ],
    )

  def test_empty_render(self):
    self.assertEqual(roadmap.show("p1"), "Roadmap: empty")

  def test_state_move_hands_over_file_and_owned_items(self):
    roadmap.add_item("p1", "old", "Mine")
    roadmap.add_item("p1", "other", "Theirs")
    roadmap.set_status("p1", "old", "R1", "doing")
    roadmap.set_status("p1", "other", "R2", "doing")
    src = roadmap.state_path("p1", "old")
    os.makedirs(os.path.dirname(src))
    with open(src, "w", encoding="utf-8") as f:
      f.write("next: run tests\n")
    roadmap.state_move("p1", "old", "new")
    dst = roadmap.state_path("p1", "new")
    self.assertFalse(os.path.exists(src))
    with open(dst, encoding="utf-8") as f:
      self.assertEqual(f.read(), "next: run tests\n")
    self.assertEqual(self.items()["R1"]["owner"], "new")
    self.assertEqual(self.items()["R2"]["owner"], "other")
    self.assertEqual(roadmap.state_move("p1", "old", "new"), f"state already moved to {dst}")
    with open(dst, encoding="utf-8") as f:
      self.assertEqual(f.read(), "next: run tests\n")
    with open(src, "w", encoding="utf-8") as f:
      f.write("x")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.state_move("p1", "old", "new")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.state_move("p1", "nobody", "new2")

  def test_state_move_rolls_back_file_when_roadmap_write_fails(self):
    roadmap.add_item("p1", "old", "Mine")
    roadmap.set_status("p1", "old", "R1", "doing")
    src = roadmap.state_path("p1", "old")
    os.makedirs(os.path.dirname(src))
    with open(src, "w", encoding="utf-8") as f:
      f.write("state\n")
    with patch.object(roadmap, "_write_atomic", side_effect=OSError("disk full")):
      with self.assertRaises(OSError):
        roadmap.state_move("p1", "old", "new")
    self.assertTrue(os.path.isfile(src))
    self.assertFalse(os.path.exists(roadmap.state_path("p1", "new")))
    self.assertEqual(self.items()["R1"]["owner"], "old")

  def test_lock_timeout_fails_fast(self):
    roadmap.add_item("p1", "c1", "Task")
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, sys, time; f = open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX);"
         " print('locked', flush=True); time.sleep(30)",
         os.path.join(self.tmp.name, "p1", "roadmap.lock")],
        stdout=subprocess.PIPE, text=True,
    )
    try:
      self.assertEqual(holder.stdout.readline().strip(), "locked")
      with patch.dict(os.environ, {roadmap.LOCK_TIMEOUT_ENV: "0.1"}):
        with self.assertRaisesRegex(roadmap.RoadmapError, "locked by another chat"):
          roadmap.add_item("p1", "c2", "Blocked")
    finally:
      holder.kill()
      holder.wait()
      holder.stdout.close()
    self.assertEqual(list(self.items()), ["R1"])

  def test_non_string_owner_is_corruption(self):
    roadmap.add_item("p1", "c1", "Task")
    with open(self.roadmap_file(), encoding="utf-8") as f:
      doc = json.load(f)
    doc["items"][0]["owner"] = 42
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.validate(doc)

  def test_journal_records_every_change(self):
    roadmap.add_item("p1", "c1", "Task")
    roadmap.set_status("p1", "c2", "R1", "doing")
    with open(os.path.join(self.tmp.name, "p1", "journal.jsonl"), encoding="utf-8") as f:
      recs = [json.loads(line) for line in f]
    self.assertEqual([(r["op"], r["conv"]) for r in recs], [("add", "c1"), ("set", "c2")])
    self.assertTrue(all(r["ts"] for r in recs))

  def test_outside_project_has_no_roadmap(self):
    for env_pid, db_pid in (("outside-of-project", "proj"), ("", ""), ("", "outside-of-project")):
      with self.subTest(env=env_pid, db=db_pid):
        with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": env_pid}), \
             patch("context_guard.get_conversation_db_info", return_value=("t", db_pid)), \
             patch.object(sys, "argv", ["roadmap.py", "add", "c1", "Task"]), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
          self.assertNotEqual(roadmap.main(), 0)
          self.assertIn("outside any Jetski project", err.getvalue())
          self.assertEqual(err.getvalue().count("\n"), 1)
    self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "outside-of-project")))

  def test_cli_round_trip(self):
    def cli(*args):
      return subprocess.run(
          [sys.executable, ROADMAP_CLI, *args], capture_output=True, text=True, check=False
      )

    res = cli("add", "c1", "Via CLI")
    self.assertEqual((res.returncode, res.stdout), (0, "R1 added: Via CLI\n"))
    res = cli("set", "c1", "R1", "bogus")
    self.assertEqual(res.returncode, 1)
    self.assertIn("bad status", res.stderr)
    res = cli("state-path", "c1")
    self.assertEqual(res.stdout.strip(), os.path.join(self.tmp.name, "p1", "state", "c1.md"))
    self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "p1", "state")))
    self.assertEqual(cli("show").stdout.splitlines()[1], "☐ R1 Via CLI")
    self.assertEqual(cli("frobnicate").returncode, 2)

  def test_concurrent_adds_lose_nothing(self):
    workers, per_worker = 6, 15
    script = (
        "import sys; sys.path.insert(0, sys.argv[1]); import roadmap\n"
        "for i in range(int(sys.argv[3])):\n"
        "  roadmap.add_item('p1', sys.argv[2], f'{sys.argv[2]} item {i}')\n"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", script, HOOKS_DIR, f"w{n}", str(per_worker)])
        for n in range(workers)
    ]
    for p in procs:
      self.assertEqual(p.wait(timeout=60), 0)
    with open(self.roadmap_file(), encoding="utf-8") as f:
      doc = json.load(f)
    roadmap.validate(doc)
    ids = [it["id"] for it in doc["items"]]
    self.assertEqual(sorted(ids, key=lambda s: int(s[1:])), [f"R{i}" for i in range(1, workers * per_worker + 1)])
    self.assertEqual(doc["next_id"], workers * per_worker + 1)
    with open(os.path.join(self.tmp.name, "p1", "journal.jsonl"), encoding="utf-8") as f:
      self.assertEqual(len(f.readlines()), workers * per_worker)


if __name__ == "__main__":
  unittest.main()
