#!/usr/bin/env python3
"""Tests for skia_patch_ab.py stats, verdicts and submodule restore (no engine build)."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import skia_patch_ab as ab


def _bench_json(entries: dict[str, float], unit: str = "ns", aggregate: bool = False) -> str:
  rows = [{"name": n, "run_type": "iteration", "cpu_time": t, "time_unit": unit}
          for n, t in entries.items()]
  if aggregate:
    rows += [{"name": n + "_mean", "run_type": "aggregate", "cpu_time": 1.0, "time_unit": unit}
             for n in entries]
  return json.dumps({"context": {}, "benchmarks": rows})


def _rounds(base_ns: float, factor: float, n: int, noise: float, seed: int):
  rng = random.Random(seed)
  base, patch = [], []
  for _ in range(n):
    b = base_ns * (1 + rng.uniform(-noise, noise))
    base.append({"Long": b})
    patch.append({"Long": b * factor * (1 + rng.uniform(-noise, noise))})
  return base, patch


class StatsTest(unittest.TestCase):

  def test_median(self):
    self.assertEqual(ab.median([3.0, 1.0, 2.0]), 2.0)
    self.assertEqual(ab.median([4.0, 1.0, 2.0, 3.0]), 2.5)
    with self.assertRaises(ValueError):
      ab.median([])

  def test_bootstrap_is_seeded_and_brackets_median(self):
    vals = [1.0 + 0.01 * i for i in range(20)]
    lo, hi = ab.bootstrap_median_ci(vals)
    self.assertEqual((lo, hi), ab.bootstrap_median_ci(vals))
    self.assertLessEqual(lo, ab.median(vals))
    self.assertGreaterEqual(hi, ab.median(vals))
    self.assertEqual(ab.bootstrap_median_ci([1.1] * 10), (1.1, 1.1))

  def test_verdict_edges(self):
    self.assertEqual(ab.verdict(1.03, 1.10, 0.02), "REGRESSION")
    self.assertEqual(ab.verdict(1.02, 1.10, 0.02), "NEUTRAL")
    self.assertEqual(ab.verdict(0.90, 0.97, 0.02), "IMPROVEMENT")
    self.assertEqual(ab.verdict(0.90, 0.98, 0.02), "NEUTRAL")
    self.assertEqual(ab.verdict(0.95, 1.05, 0.02), "NEUTRAL")

  def test_daf82dad48_sized_slowdown_is_regression(self):
    base, patch = _rounds(1_000_000, 1.216, 20, 0.02, seed=1)
    rows = ab.analyze(base, patch, 0.02)
    self.assertEqual(len(rows), 1)
    self.assertEqual(rows[0]["verdict"], "REGRESSION")
    self.assertAlmostEqual(rows[0]["ratio"], 1.216, delta=0.02)
    self.assertEqual(ab.overall_verdict(rows), "REGRESSION")

  def test_noise_only_is_neutral(self):
    base, patch = _rounds(1_000_000, 1.0, 20, 0.03, seed=2)
    rows = ab.analyze(base, patch, 0.02)
    self.assertEqual(rows[0]["verdict"], "NEUTRAL")

  def test_speedup_is_improvement(self):
    base, patch = _rounds(1_000_000, 0.90, 20, 0.01, seed=3)
    rows = ab.analyze(base, patch, 0.02)
    self.assertEqual(ab.overall_verdict(rows), "IMPROVEMENT")

  def test_one_regression_dominates_overall(self):
    rows = [{"verdict": "IMPROVEMENT"}, {"verdict": "REGRESSION"}, {"verdict": "NEUTRAL"}]
    self.assertEqual(ab.overall_verdict(rows), "REGRESSION")
    self.assertEqual(ab.overall_verdict([{"verdict": "IMPROVEMENT"}, {"verdict": "NEUTRAL"}]),
                     "NEUTRAL")

  def test_analyze_pairs_only_rounds_with_both_sides(self):
    base = [{"A": 100.0}, {"A": 100.0}, {"A": 100.0}]
    patch = [{"A": 110.0}, {}, {"A": 110.0}]
    rows = ab.analyze(base, patch, 0.02)
    self.assertEqual(rows[0]["rounds"], 2)
    self.assertAlmostEqual(rows[0]["ratio"], 1.1)

  def test_parse_skips_aggregates_and_scales_units(self):
    parsed = ab.parse_benchmark_json(_bench_json({"X": 2.5}, unit="us", aggregate=True))
    self.assertEqual(parsed, {"X": 2500.0})
    self.assertEqual(ab.parse_benchmark_json(json.dumps({"benchmarks": []})), {})


class CliTest(unittest.TestCase):

  def test_bad_args_exit_1_not_regression_code(self):
    with self.assertRaises(SystemExit) as cm:
      ab._parse_args(["--out", "/tmp/x"])
    self.assertEqual(cm.exception.code, ab.EXIT_USAGE)


def _git(repo: str, *args: str) -> str:
  return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                        text=True).stdout.strip()


class SubmoduleGuardTest(unittest.TestCase):

  def test_restore_resets_edits_untracked_files_and_head(self):
    with tempfile.TemporaryDirectory() as repo:
      _git(repo, "init", "-q")
      _git(repo, "config", "user.email", "t@example.com")
      _git(repo, "config", "user.name", "t")
      with open(os.path.join(repo, "a.h"), "w") as f:
        f.write("v1\n")
      _git(repo, "add", "a.h")
      _git(repo, "commit", "-qm", "c1")
      first = _git(repo, "rev-parse", "HEAD")
      with open(os.path.join(repo, "a.h"), "w") as f:
        f.write("v2\n")
      _git(repo, "commit", "-qam", "c2")
      guard = ab.SubmoduleGuard(repo)
      _git(repo, "checkout", "-q", "--detach", first)
      with open(os.path.join(repo, "a.h"), "w") as f:
        f.write("patched\n")
      os.makedirs(os.path.join(repo, "new"))
      with open(os.path.join(repo, "new", "b.h"), "w") as f:
        f.write("x\n")
      self.assertTrue(guard.restore())
      self.assertEqual(_git(repo, "status", "--porcelain"), "")
      self.assertEqual(_git(repo, "rev-parse", "HEAD"), guard.orig_sha)
      self.assertNotEqual(guard.orig_ref, guard.orig_sha)


if __name__ == "__main__":
  unittest.main()
