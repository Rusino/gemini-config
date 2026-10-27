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
        {
            roadmap.ROADMAPS_DIR_ENV: self.tmp.name,
            "ANTIGRAVITY_PROJECT_ID": "p1",
            "ANTIGRAVITY_CONVERSATION_ID": "c1",
        },
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

  def test_render_collapses_old_closed_items_above_threshold(self):
    for i in range(1, 9):
      roadmap.add_item("p1", "c1", f"Task {i}")
    roadmap.add_item("p1", "c1", "Dropped child", parent="R7")
    roadmap.set_status("p1", "c1", "R7.1", "dropped")
    for rid in ("R1", "R3", "R4", "R5", "R6"):
      roadmap.set_status("p1", "c1", rid, "done")
    # 5 closed top-level items: at threshold, nothing is hidden yet
    self.assertNotIn("closed items hidden", roadmap.show("p1"))

    roadmap.set_status("p1", "c1", "R7", "dropped")
    # Close R2 last with a newer timestamp so out-of-order completion stays in the tail
    with patch.object(roadmap, "_now", return_value="2099-01-01T00:00:00Z"):
      roadmap.set_status("p1", "c1", "R2", "done")

    self.assertEqual(
        roadmap.show("p1").splitlines(),
        [
            "Roadmap (9): 6 done · 0 doing · 1 todo · 2 dropped",
            "… 4 closed items hidden (R1, R3–R5; run `roadmap.py show --all`)",
            "✓ R2 Task 2",
            "✓ R6 Task 6",
            "✗ R7 Task 7 (+1)",
            "☐ R8 Task 8",
        ],
    )
    full = roadmap.show("p1", show_all=True).splitlines()
    self.assertNotIn("closed items hidden", "\n".join(full))
    self.assertIn("✓ R1 Task 1", full)
    self.assertIn("✓ R4 Task 4", full)
    self.assertIn("  ✗ R7.1 Dropped child", full)

  def test_render_collapses_old_closed_subitems_under_open_epic(self):
    roadmap.add_item("p1", "c1", "Open epic")
    for i in range(1, 8):
      roadmap.add_item("p1", "c1", f"Sub {i}", parent="R1")
    for i in range(1, 7):
      roadmap.set_status("p1", "c1", f"R1.{i}", "done")
    self.assertEqual(
        roadmap.show("p1").splitlines(),
        [
            "Roadmap (8): 6 done · 0 doing · 2 todo · 0 dropped",
            "☐ R1 Open epic",
            "  … 3 closed items hidden (R1.1–R1.3)",
            "  ✓ R1.4 Sub 4",
            "  ✓ R1.5 Sub 5",
            "  ✓ R1.6 Sub 6",
            "  ☐ R1.7 Sub 7",
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
    for env_pid, db_pid in (("proj", "outside-of-project"), ("", ""), ("outside-of-project", "")):
      with self.subTest(env=env_pid, db=db_pid):
        with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": env_pid}), \
             patch("context_guard.get_conversation_db_info", return_value=("t", db_pid)), \
             patch.object(sys, "argv", ["roadmap.py", "add", "c1", "Task"]), \
             patch("sys.stderr", new_callable=io.StringIO) as err:
          self.assertNotEqual(roadmap.main(), 0)
          self.assertIn("outside any Jetski project", err.getvalue())
          self.assertEqual(err.getvalue().count("\n"), 1)
    self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "outside-of-project")))

  def test_project_id_for_prefers_db_over_stale_env(self):
    with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": "stale-proj"}), \
         patch("context_guard.get_conversation_db_info", return_value=("t", "new-proj")):
      self.assertEqual(roadmap.project_id_for("c1"), "new-proj")
    with patch.dict(os.environ, {"ANTIGRAVITY_PROJECT_ID": "fallback-proj"}), \
         patch("context_guard.get_conversation_db_info", return_value=("", "")):
      self.assertEqual(roadmap.project_id_for("c1"), "fallback-proj")

  def _run_cli(self, *args):
    out = io.StringIO()
    err = io.StringIO()
    with (
        patch.object(sys, "argv", ["roadmap.py", *args]),
        patch("sys.stdout", out),
        patch("sys.stderr", err),
    ):
      rc = roadmap.main()
    res = unittest.mock.MagicMock()
    res.returncode = rc
    res.stdout = out.getvalue()
    res.stderr = err.getvalue()
    return res

  def test_cli_round_trip(self):
    cli = self._run_cli
    res = cli("add", "c1", "Via CLI")
    self.assertEqual((res.returncode, res.stdout), (0, "R1 added: Via CLI\n"))
    res = cli("set", "c1", "R1", "bogus")
    self.assertEqual(res.returncode, 1)
    self.assertIn("bad status", res.stderr)
    res = cli("state-path", "c1")
    self.assertEqual(res.stdout.strip(), os.path.join(self.tmp.name, "p1", "state", "c1.md"))
    self.assertTrue(os.path.isdir(os.path.join(self.tmp.name, "p1", "state")))
    self.assertEqual(cli("show").stdout.splitlines()[1], "☐ R1 Via CLI")
    self.assertEqual(cli("show", "c1").stdout.splitlines()[1], "☐ R1 Via CLI")
    for i in range(2, 8):
      roadmap.add_item("p1", "c1", f"Done {i}")
      roadmap.set_status("p1", "c1", f"R{i}", "done")
    self.assertIn("closed items hidden", cli("show").stdout)
    self.assertNotIn("closed items hidden", cli("show", "--all").stdout)
    self.assertNotIn("closed items hidden", cli("show", "c1", "--all").stdout)
    self.assertNotIn("closed items hidden", cli("show", "--all", "c1").stdout)
    self.assertEqual(cli("show", "../bad").returncode, 1)
    self.assertEqual(cli("show", "c1", "c2").returncode, 2)
    self.assertEqual(cli("show", "--all", "--all").returncode, 2)
    self.assertEqual(cli("frobnicate").returncode, 2)

  def order(self, show_all=True):
    return [ln.split()[1] for ln in roadmap.show("p1", show_all=show_all).splitlines()[1:]]

  def test_edit_keeps_id_status_owner_link_and_position(self):
    for t in ("A", "B"):
      roadmap.add_item("p1", "c1", t)
    roadmap.set_status("p1", "c1", "R1", "doing")
    roadmap.link_item("p1", "c1", "R1", "chat-9")
    self.assertEqual(roadmap.edit_item("p1", "c2", "R1", " new\n text "), "R1 edited: new text")
    it = self.items()["R1"]
    self.assertEqual(
        (it["text"], it["status"], it["owner"], it["link"], it["updated_by"]),
        ("new text", "doing", "c1", "chat-9", "c2"),
    )
    self.assertEqual(self.order(), ["R1", "R2"])
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.edit_item("p1", "c1", "R1", "  ")
    with self.assertRaises(roadmap.RoadmapError):
      roadmap.edit_item("p1", "c1", "R9", "x")

  def test_reorder_moves_subtrees_and_keeps_ids(self):
    for t in ("A", "B", "C", "D"):
      roadmap.add_item("p1", "c1", t)
    roadmap.add_item("p1", "c1", "B1", parent="R2")
    roadmap.add_item("p1", "c1", "B2", parent="R2")
    roadmap.add_item("p1", "c1", "A1", parent="R1")
    before = {iid: dict(it) for iid, it in self.items().items()}
    roadmap.reorder_items("p1", "c2", ["R3", "R2"])
    self.assertEqual(self.order(), ["R3", "R2", "R2.1", "R2.2", "R1", "R1.1", "R4"])
    self.assertEqual(self.items(), before)
    roadmap.add_item("p1", "c1", "E")
    self.assertEqual(self.order()[-1], "R5")
    roadmap.reorder_items("p1", "c1", ["R2.2"])
    self.assertEqual(self.order()[:4], ["R3", "R2", "R2.2", "R2.1"])
    for bad in (["R1", "R2.1"], ["R1", "R1"], ["R9"], []):
      with self.assertRaises(roadmap.RoadmapError):
        roadmap.reorder_items("p1", "c1", bad)

  def test_move_positions_and_sibling_scope(self):
    for t in ("A", "B", "C"):
      roadmap.add_item("p1", "c1", t)
    roadmap.add_item("p1", "c1", "C1", parent="R3")
    roadmap.add_item("p1", "c1", "C2", parent="R3")
    self.assertEqual(roadmap.move_item("p1", "c1", "R3", "top"), "R3 moved to top")
    self.assertEqual(self.order(), ["R3", "R3.1", "R3.2", "R1", "R2"])
    roadmap.move_item("p1", "c1", "R3", "after", "R1")
    self.assertEqual(self.order(), ["R1", "R3", "R3.1", "R3.2", "R2"])
    roadmap.move_item("p1", "c1", "R2", "before", "R1")
    roadmap.move_item("p1", "c1", "R3.1", "bottom")
    self.assertEqual(self.order(), ["R2", "R1", "R3", "R3.2", "R3.1"])
    roadmap.move_item("p1", "c1", "R2", "bottom")
    self.assertEqual(self.order(), ["R1", "R3", "R3.2", "R3.1", "R2"])
    for args in (
        ("R3.1", "before", "R1"),
        ("R1", "before", "R1"),
        ("R1", "before", ""),
        ("R1", "top", "R2"),
        ("R1", "sideways", ""),
        ("R9", "top", ""),
    ):
      with self.assertRaises(roadmap.RoadmapError):
        roadmap.move_item("p1", "c1", *args)
    self.assertEqual(self.order(), ["R1", "R3", "R3.2", "R3.1", "R2"])

  def test_reorder_keeps_closed_item_collapse_ranking(self):
    for i in range(1, 8):
      roadmap.add_item("p1", "c1", f"Done {i}")
      roadmap.set_status("p1", "c1", f"R{i}", "done")
    stamps = {iid: it["updated_at"] for iid, it in self.items().items()}
    roadmap.reorder_items("p1", "c1", ["R7", "R1"])
    roadmap.move_item("p1", "c1", "R2", "top")
    self.assertEqual({iid: it["updated_at"] for iid, it in self.items().items()}, stamps)
    self.assertEqual(self.order(), ["R2", "R7", "R1", "R3", "R4", "R5", "R6"])

  def test_cli_edit_reorder_move(self):
    cli = self._run_cli
    for t in ("A", "B", "C"):
      roadmap.add_item("p1", "c1", t)
    self.assertEqual(cli("edit", "c1", "R2", "B2").returncode, 0)
    self.assertEqual(cli("reorder", "c1", "R3").returncode, 0)
    self.assertEqual(cli("move", "c1", "R1", "--before", "R3").returncode, 0)
    self.assertEqual(cli("move", "c1", "R2", "--top").returncode, 0)
    lines = cli("show", "c1").stdout.splitlines()[1:]
    self.assertEqual(lines, ["☐ R2 B2", "☐ R1 A", "☐ R3 C"])
    self.assertEqual(cli("move", "c1", "R1", "--top", "R2").returncode, 2)
    self.assertEqual(cli("move", "c1", "R1", "--before").returncode, 2)
    self.assertEqual(cli("move", "c1", "R1", "R2").returncode, 2)
    self.assertEqual(cli("move", "c1", "R1", "--sideways").returncode, 1)
    self.assertEqual(cli("reorder", "c1").returncode, 2)
    self.assertEqual(cli("edit", "c1", "R1").returncode, 2)
    with open(os.path.join(self.tmp.name, "p1", "journal.jsonl"), encoding="utf-8") as f:
      ops = [json.loads(ln)["op"] for ln in f]
    self.assertEqual(ops[-4:], ["edit", "reorder", "move", "move"])

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

  def test_last_chat_baton_holds_until_last_doing_finishes_and_prevents_duplicate_proposals(self):
    cli = self._run_cli
    for t in ("Task 1", "Task 2", "Task 3", "Task 4"):
      roadmap.add_item("p1", "c1", t)
    roadmap.set_status("p1", "c1", "R1", "doing")
    roadmap.set_status("p1", "c2", "R2", "doing")
    roadmap.set_status("p1", "c3", "R3", "doing")

    # c1 finishes R1 while R2 and R3 are still doing -> HOLD
    r1 = cli("set", "c1", "R1", "done")
    self.assertEqual(r1.returncode, 0)
    self.assertIn("[NEXT-TASK BATON: HOLD", r1.stdout)
    self.assertIn("R2 [c2]", r1.stdout)
    self.assertIn("R3 [c3]", r1.stdout)

    # c2 finishes R2 while R3 is still doing -> HOLD
    r2 = cli("set", "c2", "R2", "done")
    self.assertEqual(r2.returncode, 0)
    self.assertIn("[NEXT-TASK BATON: HOLD", r2.stdout)
    self.assertIn("R3 [c3]", r2.stdout)

    # c3 finishes R3 (last doing item -> 0 doing) -> YOURS for R4
    r3 = cli("set", "c3", "R3", "done")
    self.assertEqual(r3.returncode, 0)
    self.assertIn("[NEXT-TASK BATON: YOURS", r3.stdout)
    self.assertIn("R4", r3.stdout)

    # If another chat c0 closes an un-claimed item while c3 already holds the baton for R4 -> HOLD
    roadmap.add_item("p1", "c0", "Extra task")
    r5 = cli("set", "c0", "R5", "done")
    self.assertEqual(r5.returncode, 0)
    self.assertIn("[NEXT-TASK BATON: HOLD", r5.stdout)
    self.assertIn("c3", r5.stdout)

    # Once R4 is also done -> ALL DONE
    r4 = cli("set", "c3", "R4", "done")
    self.assertEqual(r4.returncode, 0)
    self.assertIn("[NEXT-TASK BATON: ALL DONE", r4.stdout)

    # Corrupted baton dicts (empty or missing conv/item) are rejected by validate()
    doc = roadmap.load("p1")
    for bad_baton in ({}, {"conv": "c1"}, {"conv": "", "item": "R1"}, "c1"):
      doc["baton"] = bad_baton
      with self.assertRaises(roadmap.RoadmapError):
        roadmap.validate(doc)

  def test_summarize_and_export_roadmap_index(self):
    roadmap.add_item("p1", "c1", "Parent epic")
    roadmap.add_item("p1", "c1", "Child done", parent="R1")
    roadmap.add_item("p1", "c1", "Child doing", parent="R1")
    roadmap.add_item("p1", "c1", "Standalone todo")
    roadmap.add_item("p1", "c1", "Dropped item")
    roadmap.set_status("p1", "c1", "R1", "doing")
    roadmap.set_status("p1", "c1", "R1.1", "done")
    roadmap.set_status("p1", "c2", "R1.2", "doing")
    roadmap.set_status("p1", "c1", "R3", "dropped")

    index_path = os.path.join(self.tmp.name, roadmap.ROADMAP_INDEX_FILE_NAME)
    self.assertTrue(os.path.isfile(index_path))
    with open(index_path, encoding="utf-8") as f:
      payload = json.load(f)
    summary = payload["projects"]["p1"]
    # Leaf tasks are R1.1 (done), R1.2 (doing), R2 (todo), R3 (dropped); parent R1 is not
    # double-counted on top of R1.1/R1.2, and dropped R3 is excluded from total
    self.assertEqual(summary["done"], 1)
    self.assertEqual(summary["doing"], 1)
    self.assertEqual(summary["todo"], 1)
    self.assertEqual(summary["dropped"], 1)
    self.assertEqual(summary["open"], 2)
    self.assertEqual(summary["total"], 3)
    self.assertEqual(
        [(it["id"], it["status"], it["depth"], it["owner"]) for it in summary["openItems"]],
        [("R1", "doing", 0, "c1"), ("R1.2", "doing", 1, "c2"), ("R2", "todo", 0, "")],
    )

    # If R1.2 finishes while parent R1 is still doing, R1 remains counted as 1 open item
    roadmap.set_status("p1", "c2", "R1.2", "done")
    summary_after = roadmap.summarize_doc(roadmap.load("p1"))
    self.assertEqual(summary_after["doing"], 1)
    self.assertEqual(summary_after["todo"], 1)
    self.assertEqual(summary_after["open"], 2)

    r_cli = self._run_cli("export-index")
    self.assertEqual(r_cli.returncode, 0)
    self.assertIn("exported 1 project roadmap(s)", r_cli.stdout)

    # When JETSKI_ROADMAPS_DIR is set, live production app data dirs in ANTIGRAVITY_APP_DATA_DIR
    # must never be returned by find_app_data_dirs
    with patch.dict(
        os.environ,
        {
            roadmap.ROADMAPS_DIR_ENV: self.tmp.name,
            "ANTIGRAVITY_APP_DATA_DIR": os.path.expanduser("~/.gemini/jetski"),
        },
    ):
      self.assertEqual(roadmap.find_app_data_dirs(), [])
      app_dir = os.path.join(self.tmp.name, "custom_app_data")
      os.makedirs(app_dir, exist_ok=True)
    with patch.dict(
        os.environ,
        {
            roadmap.ROADMAPS_DIR_ENV: self.tmp.name,
            "ANTIGRAVITY_APP_DATA_DIR": app_dir,
        },
    ):
      self.assertEqual(roadmap.find_app_data_dirs(), [app_dir])
      roadmap.export_roadmap_index()
      self.assertTrue(
          os.path.isfile(os.path.join(app_dir, "brain", roadmap.ROADMAP_INDEX_FILE_NAME))
      )

  def test_evaluate_vcs_baton_no_repos_touched(self):
    self.assertEqual(
        roadmap.evaluate_vcs_baton("c1", "p1"),
        "[VCS BATON: CLEAN — no git repositories touched in this chat]",
    )

  def test_evaluate_vcs_baton_own_dirty_vs_foreign_and_unpushed(self):
    repo_dir = os.path.join(self.tmp.name, "my_repo")
    remote_dir = os.path.join(self.tmp.name, "remote_repo.git")
    subprocess.run(["git", "init", "--bare", remote_dir], check=True, capture_output=True)
    subprocess.run(["git", "init", repo_dir], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", repo_dir, "remote", "add", "origin", remote_dir], check=True)

    # Initial commit pushed to origin/main
    f_init = os.path.join(repo_dir, "init.txt")
    with open(f_init, "w") as f:
      f.write("base")
    subprocess.run(["git", "-C", repo_dir, "add", "init.txt"], check=True)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", repo_dir, "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", repo_dir, "push", "-u", "origin", "main"], check=True)

    # Create file_a (own touched) and file_b (foreign dirty)
    f_a = os.path.join(repo_dir, "file_a.py")
    f_b = os.path.join(repo_dir, "file_b.py")
    with open(f_a, "w") as f:
      f.write("print('a')")
    with open(f_b, "w") as f:
      f.write("print('b')")

    tpath = os.path.join(self.tmp.name, "transcript.jsonl")
    steps = [
        {"step_index": 1, "type": "USER_INPUT", "content": "do work"},
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [
                {"name": "write_file", "args": {"Path": f_a}},
            ],
        },
    ]
    with open(tpath, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")

    out = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: PENDING", out)
    self.assertIn("own_dirty=file_a.py", out)
    self.assertIn("foreign_dirty=file_b.py", out)
    self.assertIn("action: call ask_question", out)

    # Commit only file_a (unpushed)
    subprocess.run(["git", "-C", repo_dir, "add", "file_a.py"], check=True)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "add a", "--", "file_a.py"], check=True)
    out2 = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: PENDING", out2)
    self.assertIn("own_dirty=none", out2)
    self.assertIn("foreign_dirty=file_b.py", out2)
    self.assertIn("unpushed=", out2)
    self.assertIn("add a", out2)

    # Push file_a commit -> CLEAN
    subprocess.run(["git", "-C", repo_dir, "push"], check=True)
    out3 = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: CLEAN", out3)
    self.assertIn("own_dirty=none", out3)
    self.assertIn("foreign_dirty=file_b.py", out3)
    self.assertIn("unpushed=none", out3)
    self.assertIn("action: report", out3)

  def test_evaluate_vcs_baton_gerrit_upload_states(self):
    repo_dir = os.path.join(self.tmp.name, "gerrit_repo")
    remote_dir = os.path.join(self.tmp.name, "gerrit_remote.git")
    subprocess.run(["git", "init", "--bare", remote_dir], check=True, capture_output=True)
    subprocess.run(["git", "init", repo_dir], check=True, capture_output=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.name", "Tester"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", repo_dir, "config", "gerrit.host", "true"], check=True)
    subprocess.run(["git", "-C", repo_dir, "remote", "add", "origin", remote_dir], check=True)

    f_init = os.path.join(repo_dir, "init.txt")
    with open(f_init, "w") as f:
      f.write("base")
    subprocess.run(["git", "-C", repo_dir, "add", "init.txt"], check=True)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "init"], check=True)
    subprocess.run(["git", "-C", repo_dir, "branch", "-M", "main"], check=True)
    subprocess.run(["git", "-C", repo_dir, "push", "-u", "origin", "main"], check=True)

    subprocess.run(["git", "-C", repo_dir, "checkout", "-b", "cl-fix"], check=True)
    f_fix = os.path.join(repo_dir, "fix.cpp")
    with open(f_fix, "w") as f:
      f.write("void fix() {}")
    subprocess.run(["git", "-C", repo_dir, "add", "fix.cpp"], check=True)
    subprocess.run(["git", "-C", repo_dir, "commit", "-m", "fix bug"], check=True)
    head_sha = subprocess.check_output(
        ["git", "-C", repo_dir, "rev-parse", "HEAD"], text=True
    ).strip()

    tpath = os.path.join(self.tmp.name, "transcript_gerrit.jsonl")
    steps = [
        {"step_index": 1, "type": "USER_INPUT", "content": "fix bug"},
        {
            "step_index": 2,
            "type": "PLANNER_RESPONSE",
            "tool_calls": [{"name": "write_file", "args": {"Path": f_fix}}],
        },
    ]
    with open(tpath, "w", encoding="utf-8") as f:
      for s in steps:
        f.write(json.dumps(s) + "\n")

    # Not uploaded yet -> PENDING
    out1 = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: PENDING", out1)
    self.assertIn("gerrit=not uploaded", out1)
    self.assertIn("amnesia-precommit", out1)

    # Set gerritissue with matching last-upload-hash -> CLEAN
    subprocess.run(
        ["git", "-C", repo_dir, "config", "branch.cl-fix.gerritissue", "1409999"], check=True
    )
    subprocess.run(
        ["git", "-C", repo_dir, "config", "branch.cl-fix.gerritpatchset", "1"], check=True
    )
    subprocess.run(
        ["git", "-C", repo_dir, "config", "branch.cl-fix.last-upload-hash", head_sha], check=True
    )
    out2 = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: CLEAN", out2)
    self.assertIn("cl/1409999 PS 1", out2)
    self.assertIn("uploaded", out2)

    # Make another commit so upload is stale -> PENDING
    with open(f_fix, "a") as f:
      f.write("\n// extra")
    subprocess.run(["git", "-C", repo_dir, "commit", "-am", "extra"], check=True)
    out3 = roadmap.evaluate_vcs_baton("c1", "p1", transcript_path=tpath)
    self.assertIn("[VCS BATON: PENDING", out3)
    self.assertIn("stale", out3)


if __name__ == "__main__":
  unittest.main()
