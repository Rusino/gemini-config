#!/usr/bin/env python3
"""Tests for the Cloudtop build + remote measure scripts of flutter-engine-perf-compare."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SKILL_DIR = (pathlib.Path(__file__).resolve().parent.parent
             / "skills/flutter-engine-perf-compare/scripts")
if str(SKILL_DIR) not in sys.path:
  sys.path.insert(0, str(SKILL_DIR))

import eval_rounds
import make_plan
import perf_remote

FILTERS = {"txt": "T$", "ext": "E$"}


class MakePlanTest(unittest.TestCase):

  # Drift cancels only if every round runs each point once and the order alternates
  def test_rounds_alternate_forward_and_reverse_with_barriers(self):
    lines = make_plan.plan_lines("p", ["a", "b", "c"], 3, 10, ["txt"], FILTERS)
    self.assertEqual(lines, [
        "p_a_r1_txt a txt 10 T$", "p_b_r1_txt b txt 10 T$", "p_c_r1_txt c txt 10 T$",
        "BARRIER",
        "p_c_r2_txt c txt 10 T$", "p_b_r2_txt b txt 10 T$", "p_a_r2_txt a txt 10 T$",
        "BARRIER",
        "p_a_r3_txt a txt 10 T$", "p_b_r3_txt b txt 10 T$", "p_c_r3_txt c txt 10 T$",
    ])

  # The runner splits on whitespace and eval_rounds reads <prefix>_<point>_r<r>_<kind>
  def test_kinds_and_names_match_the_evaluator(self):
    lines = make_plan.plan_lines("p", ["a"], 1, 5, ["txt", "ext"], FILTERS)
    self.assertEqual(lines, ["p_a_r1_txt a txt 5 T$", "p_a_r1_ext a ext 5 E$"])
    self.assertTrue(all(len(l.split()) == 5 for l in lines))

  # Anchors open each round and repeat every N points, with distinct output names
  def test_anchor_slots(self):
    lines = make_plan.plan_lines("p", ["a", "b", "c"], 2, 1, ["txt"], FILTERS, "z", 2)
    outs = [l.split()[0] for l in lines]
    self.assertEqual(outs, [
        "p_z_r1a0_txt", "p_a_r1_txt", "p_b_r1_txt", "p_z_r1a1_txt", "p_c_r1_txt", "BARRIER",
        "p_z_r2a0_txt", "p_c_r2_txt", "p_b_r2_txt", "p_z_r2a1_txt", "p_a_r2_txt",
    ])

  # A space would shift the runner's fields; duplicates would overwrite raw outputs
  def test_rejects_bad_tokens(self):
    for pts, filters in ((["a b"], FILTERS), (["a", "a"], FILTERS),
                         (["a"], {"txt": "x y", "ext": "E"})):
      with self.assertRaises(ValueError):
        make_plan.plan_lines("p", pts, 1, 1, ["txt"], filters)


class PerfRemoteTest(unittest.TestCase):

  # A new non-mux ssh costs the user a security-key touch; every call must reuse the master
  def test_ssh_and_rsync_only_reuse_the_master(self):
    want = ["-o", "BatchMode=yes", "-o", "ControlMaster=no", "-o", "ControlPath=/tmp/cm"]
    self.assertEqual(perf_remote.ssh_argv("h", "/tmp/cm", "true"), ["ssh", *want, "h", "true"])
    r = perf_remote.rsync_argv("/tmp/cm", ["a", "b"], "h:d/")
    self.assertEqual(r[:3], ["rsync", "-a", "-e"])
    self.assertEqual(r[3], " ".join(["ssh", *want]))
    self.assertEqual(r[-3:], ["a", "b", "h:d/"])

  # A dead master must not fall back to a fresh ssh; the user gets the one-time command
  def test_dead_master_prints_open_command(self):
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 255))
    with mock.patch("sys.stderr") as err:
      self.assertFalse(perf_remote.check_master("h", "/tmp/cm", run=run))
    argv = run.call_args[0][0]
    self.assertEqual(argv[:3], ["ssh", "-O", "check"])
    text = "".join(c[0][0] for c in err.write.call_args_list)
    self.assertIn("ssh -fNM -o ControlPersist=10h -o ControlPath=/tmp/cm h", text)

  # main() must stop before any remote call when the master is dead
  def test_main_exits_without_master(self):
    with mock.patch.object(perf_remote, "check_master", return_value=False), \
         mock.patch.object(perf_remote, "_run") as run:
      rc = perf_remote.main(["--host", "h", "has", "pt"])
    self.assertEqual(rc, perf_remote.EXIT_NO_MASTER)
    run.assert_not_called()

  # The runner starts a point on .complete; it must come after the checksum check and mv
  def test_push_marks_complete_last(self):
    s = perf_remote.push_remote_script("perf", "abc_nolto")
    i_sum, i_mv, i_done = (s.index("sha256sum -c"), s.index("mv $T bin/abc_nolto"),
                           s.index("touch bin/abc_nolto/.complete"))
    self.assertLess(i_sum, i_mv)
    self.assertLess(i_mv, i_done)
    self.assertTrue(s.rstrip().endswith("touch bin/abc_nolto/.complete"))
    self.assertTrue(s.startswith("set -e;"))

  # Pushing goes to <point>.tmp first and never copies a stale .complete from the stage
  def test_push_uploads_into_tmp_dir(self):
    with tempfile.TemporaryDirectory() as d:
      pathlib.Path(d, "SHA256SUMS").write_text("")
      with mock.patch.object(perf_remote, "check_master", return_value=True), \
           mock.patch.object(perf_remote, "_run", return_value=0) as run:
        self.assertEqual(perf_remote.main(["--host", "h", "push", d, "pt"]), 0)
    calls = [c[0][0] for c in run.call_args_list]
    self.assertEqual(calls[1][0], "rsync")
    self.assertEqual(calls[1][-1], "h:perf_weekly/bin/pt.tmp/")
    self.assertIn(".complete", calls[1])
    self.assertIn("touch bin/pt/.complete", calls[2][-1])

  # Enqueue starts the background runner when .runner.lock is free instead of matching its own
  # shell command line, and reports RUNNING without spawning a duplicate when locked
  def test_enqueue_remote_script_runs(self):
    with tempfile.TemporaryDirectory() as root:
      r = pathlib.Path(root)
      (r / "lanes.txt").write_text("plans/a.txt plans/b.txt\n")
      (r / "remote_runner.sh").write_text("#!/bin/sh\necho ran >> started\n")
      (r / "remote_runner.sh").chmod(0o755)
      out = subprocess.run(
          ["bash", "-c", perf_remote.enqueue_remote_script(root, ["b.txt", "c.txt"])],
          capture_output=True, text=True, check=True).stdout
      self.assertEqual((r / "lanes.txt").read_text(), "plans/a.txt plans/b.txt\nplans/c.txt\n")
      self.assertEqual(out.strip(), "STARTED")
      deadline = time.monotonic() + 2.0
      while not (r / "started").is_file() and time.monotonic() < deadline:
        time.sleep(0.02)
      self.assertEqual((r / "started").read_text(), "ran\n")

      holder = subprocess.Popen(["flock", str(r / ".runner.lock"), "sleep", "5"])
      try:
        time.sleep(0.05)
        locked_out = subprocess.run(
            ["bash", "-c", perf_remote.enqueue_remote_script(root, ["d.txt"])],
            capture_output=True, text=True, check=True).stdout
        self.assertEqual(locked_out.strip(), "RUNNING")
        self.assertEqual((r / "started").read_text(), "ran\n")
      finally:
        holder.terminate()
        holder.wait(timeout=2)

  # Runs the real publish script locally: a good stage becomes bin/<point> with .complete
  # and linked fonts, a checksum mismatch publishes nothing
  def test_push_remote_script_runs(self):
    for corrupt in (False, True):
      with tempfile.TemporaryDirectory() as root:
        tmp = pathlib.Path(root, "bin/pt.tmp")
        (tmp / "assets").mkdir(parents=True)
        (tmp / "txt_benchmarks").write_text("bin")
        (tmp / "assets/f.ttf").write_text("font")
        fx = pathlib.Path(root, "out/gen/flutter/txt/assets")
        (tmp / "META").write_text(f"name=pt\nfixtures_path={fx}\n")
        sums = subprocess.run(["sha256sum", "txt_benchmarks", "assets/f.ttf"], cwd=tmp,
                              capture_output=True, text=True, check=True).stdout
        (tmp / "SHA256SUMS").write_text(sums)
        if corrupt:
          (tmp / "txt_benchmarks").write_text("changed")
        r = subprocess.run(["bash", "-c", perf_remote.push_remote_script(root, "pt")],
                           capture_output=True, text=True, check=False)
        done = pathlib.Path(root, "bin/pt/.complete")
        if corrupt:
          self.assertNotEqual(r.returncode, 0)
          self.assertFalse(done.exists())
          self.assertFalse(pathlib.Path(root, "bin/pt").exists())
        else:
          self.assertEqual(r.returncode, 0, r.stderr)
          self.assertTrue(done.exists())
          self.assertFalse(tmp.exists())
          self.assertEqual((fx / "f.ttf").read_text(), "font")


def _bench_json(path: str, values: dict[str, list[float]]) -> None:
  """Write a minimal Google Benchmark JSON with one iteration entry per sample."""
  rows = [{"name": f"F/{b}", "run_type": "iteration", "cpu_time": v}
          for b, vs in values.items() for v in vs]
  pathlib.Path(path).write_text(json.dumps({"benchmarks": rows}))


class EvalRoundsTest(unittest.TestCase):

  # A shift seen in every round with a CI clear of 1 is CONFIRMED; a flat bench is not
  def test_consistent_shift_is_confirmed(self):
    with tempfile.TemporaryDirectory() as d:
      for r in (1, 2, 3):
        _bench_json(f"{d}/p_a_r{r}_txt.json", {"X": [100, 101, 99, 100], "Y": [50, 51, 49]})
        _bench_json(f"{d}/p_b_r{r}_txt.json", {"X": [110, 111, 109, 110], "Y": [50, 49, 51]})
      data = eval_rounds.load_chain(d, "p", ["a", "b"], 3)
      steps = eval_rounds.eval_chain(data, ["a", "b"], 3, ["X", "Y"], 0.02)
    self.assertEqual(len(steps), 1)
    self.assertTrue(steps[0]["complete"])
    self.assertTrue(steps[0]["bench"]["X"]["confirmed"])
    self.assertFalse(steps[0]["bench"]["Y"]["confirmed"])

  # One missing round makes the step incomplete, so nothing can be CONFIRMED yet
  def test_missing_round_is_not_confirmed(self):
    with tempfile.TemporaryDirectory() as d:
      for r in (1, 2):
        _bench_json(f"{d}/p_a_r{r}_txt.json", {"X": [100, 100]})
        _bench_json(f"{d}/p_b_r{r}_txt.json", {"X": [150, 150]})
      data = eval_rounds.load_chain(d, "p", ["a", "b"], 3)
      steps = eval_rounds.eval_chain(data, ["a", "b"], 3, ["X"], 0.02)
    self.assertFalse(steps[0]["complete"])
    self.assertFalse(steps[0]["bench"]["X"]["confirmed"])


FAKE_BENCH = """#!/bin/sh
for a in "$@"; do case $a in --benchmark_out=*) out=${a#--benchmark_out=} ;; esac; done
echo '{"benchmarks": []}' > "$out"
"""


# remote_runner.sh runs only on the Linux measuring workstation (/proc/loadavg, /proc/stat,
# taskset); macOS has none of them, nor GNU timeout
@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("taskset")
                     and shutil.which("timeout"), "remote_runner.sh needs Linux, taskset, timeout")
class RemoteRunnerTest(unittest.TestCase):
  """Runs remote_runner.sh locally against fake binaries in a temp root."""

  def _root(self, d: str, points: dict[str, bool], plan: list[str]) -> pathlib.Path:
    """Build bin/<point> (with .complete if True), plans/p.txt and lanes.txt under d."""
    root = pathlib.Path(d)
    for pt, complete in points.items():
      b = root / "bin" / pt
      b.mkdir(parents=True)
      (b / "txt_benchmarks").write_text(FAKE_BENCH)
      (b / "txt_benchmarks").chmod(0o755)
      if complete:
        (b / ".complete").write_text("")
    (root / "plans").mkdir()
    (root / "plans/p.txt").write_text("\n".join(plan) + "\n")
    (root / "lanes.txt").write_text("plans/p.txt\n")
    return root

  def _run(self, root: pathlib.Path, timeout: int, strict: str = "0") -> None:
    """Run the runner with idle checks disabled, killing it after timeout seconds."""
    env = dict(os.environ, PERF_ROOT=str(root), CPU="0", SIB="", LOAD_MAX="1000000",
               BUSY_MAX="101", WARMUP="0", STRICT=strict)
    subprocess.run(["timeout", str(timeout), "bash", str(SKILL_DIR / "remote_runner.sh")],
                   env=env, check=False, capture_output=True)

  # Everything complete: every line runs in one pass, a missing binary fails just that line
  def test_runs_all_lines_and_exits(self):
    with tempfile.TemporaryDirectory() as d:
      root = self._root(d, {"a": True, "c": True},
                        ["o_a a txt 1 ALL", "BARRIER", "o_c c txt 1 ALL", "o_x c ext 1 ALL"])
      self._run(root, 30)
      self.assertTrue((root / "raw/o_a.json").exists())
      self.assertTrue((root / "raw/o_c.json").exists())
      self.assertTrue((root / "raw/o_x.failed").exists())
      self.assertIn("DONE", (root / "logs/runner.log").read_text())

  # A point without .complete (manual push) is passed over, but the BARRIER holds the next
  # round; in STRICT mode it blocks the rest of the plan
  def test_incomplete_point_is_held_at_barrier(self):
    plan = ["o_a a txt 1 ALL", "o_b b txt 1 ALL", "o_c c txt 1 ALL", "BARRIER",
            "o_d c txt 1 ALL"]
    for strict, ran_c in (("0", True), ("1", False)):
      with tempfile.TemporaryDirectory() as d:
        root = self._root(d, {"a": True, "b": False, "c": True}, plan)
        self._run(root, 5, strict)
        raw = root / "raw"
        self.assertTrue((raw / "o_a.json").exists())
        self.assertFalse((raw / "o_b.json").exists())
        self.assertEqual((raw / "o_c.json").exists(), ran_c)
        self.assertFalse((raw / "o_d.json").exists())


class ShellSyntaxTest(unittest.TestCase):

  # The shell halves are not imported anywhere else, so syntax errors would surface on a host
  def test_bash_n(self):
    for name in ("remote_build_push.sh", "remote_runner.sh"):
      path = SKILL_DIR / name
      r = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True, check=False)
      self.assertEqual(r.returncode, 0, f"{name}: {r.stderr}")
      self.assertTrue(os.access(path, os.X_OK), f"{name} is not executable")


  # Upstream Skia switched utils to skia_source_set, so remote_build_push.sh must inspect
  # Flutter's flutter/skia/modules/skparagraph/BUILD.gn when wiring txt_ext_benchmarks
  def test_ext_target_links_flutter_skia_utils(self):
    git_env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    with tempfile.TemporaryDirectory() as d:
      co = pathlib.Path(d) / "flutter"
      eng = co / "engine/src"
      sk = eng / "flutter/third_party/skia"
      for rel, body in (
          ("flutter/txt/benchmarks/.keep", ""),
          ("flutter/txt/BUILD.gn", "# base\n"),
          ("flutter/txt/src/skia/paragraph_builder_skia.cc", "// base\n"),
          ("build/config/compiler/BUILD.gn", "# cfg\n"),
          ("flutter/skia/modules/skparagraph/BUILD.gn", 'skia_component("utils") {}\n'),
          ("out/perf_nolto/build.ninja", "# pregenerated\n"),
          ("out/perf_nolto/args.gn", "enable_lto = false\n"),
      ):
        p = eng / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
      clang = eng / "flutter/buildtools/linux-x64/clang/bin/clang++"
      clang.parent.mkdir(parents=True, exist_ok=True)
      clang.write_text("#!/bin/sh\necho fake-clang\n")
      clang.chmod(0o755)
      subprocess.run(["git", "init", "-q", str(co)], env=git_env, check=True)
      subprocess.run(["git", "-C", str(co), "add", "."], env=git_env, check=True)
      subprocess.run(["git", "-C", str(co), "commit", "-qm", "init"], env=git_env, check=True)

      for rel, body in (
          ("modules/skparagraph/include/ParagraphStyle.h",
           "setRenderSoftHyphens setSoftHyphenString\n"),
          ("modules/skparagraph/BUILD.gn", 'skia_source_set("utils") {}\n'),
      ):
        p = sk / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
      subprocess.run(["git", "init", "-q", str(sk)], env=git_env, check=True)
      subprocess.run(["git", "-C", str(sk), "add", "."], env=git_env, check=True)
      subprocess.run(["git", "-C", str(sk), "commit", "-qm", "sk"], env=git_env, check=True)
      sha = subprocess.run(
          ["git", "-C", str(sk), "rev-parse", "HEAD"],
          capture_output=True, text=True, check=True).stdout.strip()

      ext_cc = pathlib.Path(d) / "ext.cc"
      ext_cc.write_text("int main() { return 0; }\n")
      ninja = pathlib.Path(d) / "fake_ninja.sh"
      captured = pathlib.Path(d) / "captured_txt_build.gn"
      ninja.write_text(
          f"#!/bin/sh\ncp flutter/txt/BUILD.gn {captured}\n"
          "touch out/perf_nolto/txt_benchmarks out/perf_nolto/txt_ext_benchmarks "
          "out/perf_nolto/icudtl.dat\n"
      )
      ninja.chmod(0o755)
      res = subprocess.run(
          ["bash", str(SKILL_DIR / "remote_build_push.sh"), "--checkout", str(co),
           "--sha", sha, "--mode", "nolto", "--ext-src", str(ext_cc), "--no-push"],
          env={**os.environ, "NINJA": str(ninja)},
          capture_output=True, text=True, check=False)
      self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
      self.assertIn('"//flutter/skia/modules/skparagraph:utils"', captured.read_text())
      self.assertEqual((eng / "flutter/txt/BUILD.gn").read_text(), "# base\n")


if __name__ == "__main__":
  unittest.main()
