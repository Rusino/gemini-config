#!/usr/bin/env python3
"""Evaluate an interleaved-rounds chain measured by remote_runner.sh.

Reads `<raw>/<prefix>_<point>_r<round>_<kind>.json` (Google Benchmark JSON, per-repetition
cpu_time), then for every consecutive pair of points and for first -> last reports per
benchmark the pooled median ratio, a seeded bootstrap 95% CI and the per-round ratios.
A step is CONFIRMED only when all rounds are present, every round agrees on the sign,
|ratio - 1| > --threshold and the CI excludes 1: one round can show a confident shift that
the others do not reproduce. With --anchor, prints the anchor's per-slot medians to show
machine drift during the run.
"""

from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import random
import re
import statistics as st
import sys

BOOT_RESAMPLES = 2000
BOOT_SEED = 1


def load(path: str) -> dict[str, list[float]]:
  """Return per-iteration cpu_time samples keyed by benchmark name without its fixture."""
  out: dict[str, list[float]] = collections.defaultdict(list)
  with open(path) as f:
    for b in json.load(f)["benchmarks"]:
      if b.get("run_type") == "iteration":
        out[b["name"].split("/", 1)[-1]].append(b["cpu_time"])
  return out


def boot_ci(a: list[float], b: list[float]) -> tuple[float, float]:
  """Return the seeded bootstrap 95% CI of median(b) / median(a)."""
  rnd = random.Random(BOOT_SEED)
  rs = sorted(st.median(rnd.choices(b, k=len(b))) / st.median(rnd.choices(a, k=len(a)))
              for _ in range(BOOT_RESAMPLES))
  return rs[int(0.025 * BOOT_RESAMPLES)], rs[int(0.975 * BOOT_RESAMPLES)]


def load_chain(raw: str, prefix: str, points: list[str],
               rounds: int) -> dict[str, dict[int, dict[str, list[float]]]]:
  """Return data[point][round][bench] samples merged over the txt and ext files."""
  data: dict = collections.defaultdict(lambda: collections.defaultdict(dict))
  for p in points:
    for r in range(1, rounds + 1):
      for k in ("txt", "ext"):
        f = os.path.join(raw, f"{prefix}_{p}_r{r}_{k}.json")
        if os.path.exists(f):
          data[p][r].update(load(f))
  return data


def eval_chain(data, points: list[str], rounds: int, benches: list[str],
               threshold: float) -> list[dict]:
  """Return one step dict per evaluated pair (consecutive pairs, then first -> last).

  A round counts for a point only if it has every bench; a pair uses the rounds both
  points have, and is skipped when they share none.
  """
  done = {p: [r for r in range(1, rounds + 1) if all(b in data[p][r] for b in benches)]
          for p in points}
  pairs = list(zip(points, points[1:]))
  if len(points) > 2:
    pairs.append((points[0], points[-1]))
  steps = []
  for pa, pb in pairs:
    common = sorted(set(done[pa]) & set(done[pb]))
    if not common:
      continue
    bench_res = {}
    for b in benches:
      av = sum((data[pa][r][b] for r in common), [])
      bv = sum((data[pb][r][b] for r in common), [])
      ratio = st.median(bv) / st.median(av)
      rr = [st.median(data[pb][r][b]) / st.median(data[pa][r][b]) for r in common]
      lo, hi = boot_ci(av, bv)
      agree = all(x > 1 for x in rr) or all(x < 1 for x in rr)
      conf = (len(common) == rounds and agree and abs(ratio - 1) > threshold
              and not lo <= 1 <= hi)
      bench_res[b] = {"med_a": st.median(av), "med_b": st.median(bv), "ratio": ratio,
                      "ci": [lo, hi], "rounds": rr, "confirmed": conf}
    steps.append({"from": pa, "to": pb, "rounds": common,
                  "complete": len(common) == rounds, "bench": bench_res})
  return steps


def anchor_drift(raw: str, prefix: str, anchor: str) -> dict[str, dict[str, float]]:
  """Return median cpu_time per anchor slot `r<round>a<slot>` and bench."""
  pat = re.compile(re.escape(f"{prefix}_{anchor}_") + r"(r\d+a\d+)_(txt|ext)\.json$")
  out: dict = collections.defaultdict(dict)
  for f in sorted(glob.glob(os.path.join(raw, f"{prefix}_{anchor}_r*a*_*.json"))):
    m = pat.search(os.path.basename(f))
    if m:
      for b, v in load(f).items():
        out[m.group(1)][b] = st.median(v)
  return out


def main(argv: list[str] | None = None) -> int:
  """Parse arguments, print the chain report and optionally write it as JSON."""
  ap = argparse.ArgumentParser(description=__doc__,
                               formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("points", nargs="+", help="chain order, same as make_plan.py")
  ap.add_argument("--raw", required=True, help="dir with raw JSON (perf_remote.py fetch)")
  ap.add_argument("--prefix", required=True)
  ap.add_argument("--rounds", type=int, default=3)
  ap.add_argument("--bench", action="append",
                  help="short benchmark name (repeatable); default: every bench of round 1")
  ap.add_argument("--threshold", type=float, default=0.02)
  ap.add_argument("--anchor")
  ap.add_argument("--json", help="write steps as JSON here")
  a = ap.parse_args(argv)
  data = load_chain(a.raw, a.prefix, a.points, a.rounds)
  benches = a.bench or sorted(set.intersection(*(set(data[p][1]) for p in a.points)))
  if not benches:
    sys.stderr.write("eval_rounds: no benchmark present in round 1 of every point\n")
    return 1
  steps = eval_chain(data, a.points, a.rounds, benches, a.threshold)
  for s in steps:
    print(f"{s['from']} -> {s['to']} (rounds={s['rounds']}, complete={s['complete']}):")
    for b, x in s["bench"].items():
      lo, hi = x["ci"]
      rs = "/".join(f"{100 * (v - 1):+.2f}%" for v in x["rounds"])
      tag = "CONFIRMED" if x["confirmed"] else "noise/unconfirmed"
      print(f"  {b:32s} {100 * (x['ratio'] - 1):+6.2f}% [{100 * (lo - 1):+6.2f}%, "
            f"{100 * (hi - 1):+6.2f}%] rounds=[{rs}] ({x['med_a']:.1f} -> {x['med_b']:.1f})"
            f" {tag}")
  if a.anchor:
    print(f"anchor {a.anchor} medians per slot:")
    for slot, meds in anchor_drift(a.raw, a.prefix, a.anchor).items():
      print(f"  {slot}: " + ", ".join(f"{b}={v:.1f}" for b, v in sorted(meds.items())))
  if a.json:
    with open(a.json, "w") as f:
      json.dump(steps, f, indent=1)
  return 0


if __name__ == "__main__":
  sys.exit(main())
