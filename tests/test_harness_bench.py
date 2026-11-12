#!/usr/bin/env python3
"""Unit tests for the R142 Frozen Bugs + Safe Decoys (TPR - FPR) harness benchmark."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

BENCH_DIR = Path(__file__).resolve().parent / "harness_bench"
if str(BENCH_DIR) not in sys.path:
  sys.path.insert(0, str(BENCH_DIR))

import run_benchmark


class HarnessBenchmarkTest(unittest.TestCase):
  """Verifies curated INCIDENTS.md frozen bugs and safe decoys achieve TPR=1.0, FPR=0.0."""

  def test_curated_benchmark_achieves_perfect_net_score(self):
    summary = run_benchmark.run_benchmark()
    self.assertGreaterEqual(summary["frozen_bugs"], 6)
    self.assertGreaterEqual(summary["safe_decoys"], 6)
    self.assertEqual(summary["tpr"], 1.0, f"Missed frozen bug: {summary['results']}")
    self.assertEqual(summary["fpr"], 0.0, f"False positive on safe decoy: {summary['results']}")
    self.assertEqual(summary["net_score"], 1.0)

  def test_false_positive_on_decoy_reduces_net_score(self):
    # Injecting a scope disclaimer into a safe_decoy must raise FPR to 1.0 and lower TPR - FPR to 0.0
    payload = {
        "benchmark": "synthetic-fp-check",
        "cases": [
            {
                "id": "BUG-1",
                "kind": "frozen_bug",
                "evaluator": "commit_msg",
                "expected_categories": ["commit_disclaimer"],
                "commit_msg": "Subject\n\nNot covered: RTL runs.\n",
            },
            {
                "id": "DECOY-POLLUTED",
                "kind": "safe_decoy",
                "evaluator": "commit_msg",
                "expected_categories": [],
                "commit_msg": "Subject\n\nNot covered: accidental disclaimer on decoy.\n",
            },
        ],
    }
    with tempfile.TemporaryDirectory() as tmp:
      cpath = Path(tmp) / "cases.json"
      cpath.write_text(json.dumps(payload), encoding="utf-8")
      summary = run_benchmark.run_benchmark(cpath)
      self.assertEqual(summary["tpr"], 1.0)
      self.assertEqual(summary["fpr"], 1.0)
      self.assertEqual(summary["net_score"], 0.0)
      self.assertEqual(run_benchmark.main(["--cases", str(cpath), "--min-net-score", "1.0"]), 1)


if __name__ == "__main__":
  unittest.main()
