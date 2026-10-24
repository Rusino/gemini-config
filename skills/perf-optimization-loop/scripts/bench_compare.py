#!/usr/bin/env python3
"""Compare two benchmark sample sets and emit a verdict plus commit-message Perf: trailers.

Inputs (auto-detected per file):
  * Skia nanobench --outResultsFile JSON (metric "<bench>/<config>", samples in ms)
  * hyperfine --export-json output (a single command is named by --metric, several commands
    by their command line, so name them with hyperfine --command-name)
  * JSON object {"metric": [samples...], ...}
  * plain text, one number per line (metric name from --metric)

All metrics must be lower-is-better (time, allocations, bytes). Verdict per metric uses the
median and a seeded bootstrap 95% CI of the median ratio over all rounds pooled:
  WIN when the whole CI is below -threshold, REGRESSION when above +threshold, else NOISE.
A WIN/REGRESSION becomes UNREPLICATED with fewer than --min-rounds rounds, and INCONSISTENT
when any single round alone does not give the same verdict.
"""

import argparse
import json
import pathlib
import random
import re
import statistics
import sys


def load(path, metric):
    text = pathlib.Path(path).read_text()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {metric: [float(x) for x in text.split()]}
    if isinstance(data, (int, float)):
        data = [data]
    if isinstance(data, dict) and isinstance(data.get("results"), dict):
        out = {f"{bench}/{config}": [float(s) for s in v["samples"]]
               for bench, configs in data["results"].items()
               for config, v in configs.items() if isinstance(v, dict) and "samples" in v}
    elif isinstance(data, dict) and isinstance(data.get("results"), list):
        runs = data["results"]
        # One command per file is the usual A/B setup, and its command line differs per build
        if len(runs) == 1:
            out = {metric: [float(t) for t in runs[0]["times"]]}
        else:
            out = {r.get("command", f"cmd{i}"): [float(t) for t in r["times"]]
                   for i, r in enumerate(runs)}
    elif isinstance(data, dict):
        out = {k: [float(x) for x in v] for k, v in data.items()}
    else:
        out = {metric: [float(x) for x in data]}
    # Trailers are parsed back as whitespace-separated tokens, so names must be one token
    return {re.sub(r"\s+", "_", k.strip()): v for k, v in out.items()}


def bootstrap_ci(a, b, rounds, rng):
    ratios = []
    for _ in range(rounds):
        ma = statistics.median(rng.choices(a, k=len(a)))
        mb = statistics.median(rng.choices(b, k=len(b)))
        ratios.append((mb / ma - 1.0) * 100.0 if ma else 0.0)
    ratios.sort()
    return ratios[int(0.025 * rounds)], ratios[int(0.975 * rounds) - 1]


def fmt(x):
    return f"{x:.4g}"


def verdict_of(a, b, args, rng):
    lo, hi = bootstrap_ci(a, b, args.resamples, rng)
    return lo, hi, ("WIN" if hi < -args.threshold else
                    "REGRESSION" if lo > args.threshold else "NOISE")


def load_rounds(paths, metric):
    return [load(path, metric) for path in paths.split(",")]


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("before", help="Baseline file, or comma-separated files, one per round")
    p.add_argument("after", help="Candidate file(s), same round order as before")
    p.add_argument("--metric", default="time", help="Name for single-metric inputs")
    p.add_argument("--unit", default="", help="Unit label for trailers, e.g. us, ms, s")
    p.add_argument("--bench", default="", help="Benchmark name prefix for trailers")
    p.add_argument("--threshold", type=float, default=1.0,
                   help="Minimum |delta%%| the CI must clear to count as WIN/REGRESSION")
    p.add_argument("--resamples", type=int, default=2000)
    p.add_argument("--min-samples", type=int, default=10)
    p.add_argument("--min-rounds", type=int, default=2,
                   help="Rounds required before WIN/REGRESSION is trusted")
    args = p.parse_args()

    before_rounds = load_rounds(args.before, args.metric)
    after_rounds = load_rounds(args.after, args.metric)
    if len(before_rounds) != len(after_rounds):
        sys.exit("before and after need the same number of round files")
    nrounds = len(before_rounds)
    before = {m: [x for r in before_rounds for x in r.get(m, [])] for m in before_rounds[0]}
    after = {m: [x for r in after_rounds for x in r.get(m, [])] for m in after_rounds[0]}
    common = [m for m in before if m in after]
    if not common:
        sys.exit("No common metrics between inputs")
    rng = random.Random(0)
    rows, trailers = [], []
    for m in common:
        a, b = before[m], after[m]
        ma = statistics.median(a) if a else 0.0
        # A CI from a handful of samples is meaningless, so refuse instead of guessing
        if min(len(a), len(b)) < args.min_samples:
            rows.append({"metric": m, "verdict": "TOO_FEW_SAMPLES", "n": [len(a), len(b)]})
            continue
        # A relative delta is undefined against a zero baseline (e.g. an allocation count)
        if ma == 0:
            rows.append({"metric": m, "verdict": "ZERO_BASELINE", "n": [len(a), len(b)]})
            continue
        mb = statistics.median(b)
        delta = (mb / ma - 1.0) * 100.0
        lo, hi, verdict = verdict_of(a, b, args, rng)
        # The pooled CI only sees within-run noise; machine load or thermal state shifts whole
        # runs, so a real effect must reproduce in every round on its own
        per_round = [verdict_of(ra.get(m, []), rb.get(m, []), args, rng)[2]
                     for ra, rb in zip(before_rounds, after_rounds)
                     if min(len(ra.get(m, [])), len(rb.get(m, []))) >= args.min_samples]
        if verdict != "NOISE":
            if nrounds < args.min_rounds:
                verdict = "UNREPLICATED"
            elif any(v != verdict for v in per_round):
                verdict = "INCONSISTENT"
        rows.append({"metric": m, "before": ma, "after": mb, "delta": delta, "ci": [lo, hi],
                     "n": [len(a), len(b)], "rounds": "/".join(per_round),
                     "verdict": verdict})
        name = f"{args.bench}/{m}" if args.bench else m
        unit = f" {args.unit}" if args.unit else ""
        trailers.append(f"Perf: {name} {fmt(ma)} -> {fmt(mb)}{unit} ({delta:+.1f}%, "
                        f"95% CI [{lo:+.1f}, {hi:+.1f}], n={len(a)}/{len(b)}, "
                        f"rounds={nrounds}) {verdict}")

    print(f"Rounds: {nrounds}")
    print("| metric | before | after | delta | 95% CI | n | per-round | verdict |")
    print("|---|---:|---:|---:|---|---|---|---|")
    for r in rows:
        if "delta" not in r:
            print(f"| {r['metric']} | | | | | {r['n'][0]}/{r['n'][1]} | | {r['verdict']} |")
            continue
        print(f"| {r['metric']} | {fmt(r['before'])} | {fmt(r['after'])} | "
              f"{r['delta']:+.1f}% | [{r['ci'][0]:+.1f}, {r['ci'][1]:+.1f}] | "
              f"{r['n'][0]}/{r['n'][1]} | {r['rounds']} | {r['verdict']} |")
    print("\nCommit-message trailers:")
    print("\n".join(trailers))


if __name__ == "__main__":
    main()
