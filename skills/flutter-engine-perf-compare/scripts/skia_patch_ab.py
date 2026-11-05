#!/usr/bin/env python3
"""A/B speed gate for a Skia diff on Flutter's SkParagraph txt_benchmarks.

Builds txt_benchmarks twice in the Flutter engine checkout (Skia submodule at
--base, then --base plus the diff of --skia-repo), runs both binaries in
interleaved rounds pinned to one core, and reports the median per-round
patch/base cpu_time ratio with a bootstrap 95% CI per benchmark.

Exit codes: 0 = UNAFFECTED / NEUTRAL / IMPROVEMENT, 2 = REGRESSION,
1 = bad arguments, 3 = setup, build or run error, 4 = submodule restore failed.
"""

from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import random
import shutil
import signal
import subprocess
import sys
import time

DEFAULT_ENGINE_SRC = os.path.expanduser("~/.gemini/jetski/scratch/flutter/engine/src")
DEFAULT_FILTER = "SkParagraphFixture/(ShortLayout|LongLayout|JustifyLayout|TextBigO/1024)$"
SUBMODULE_REL = "flutter/third_party/skia"
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20261105
# Peer runs serialize on this lock; pgrep alone would make two waiting peers wait forever
PEER_LOCK = "/tmp/skia_patch_ab.lock"
OTHER_SUBMODULE_USERS = "run_flutter_paragraph_tests.py"
TIME_UNIT_NS = {"ns": 1.0, "us": 1e3, "ms": 1e6, "s": 1e9}

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REGRESSION = 2
EXIT_ERROR = 3
EXIT_RESTORE_FAILED = 4


class GateError(Exception):
  """Setup, build or run failure that maps to EXIT_ERROR."""


class _Parser(argparse.ArgumentParser):
  # argparse exits with 2 on bad input, which would read as REGRESSION
  def error(self, message: str):
    self.print_usage(sys.stderr)
    sys.stderr.write(f"{self.prog}: error: {message}\n")
    sys.exit(EXIT_USAGE)


def median(values: list[float]) -> float:
  s = sorted(values)
  n = len(s)
  if n == 0:
    raise ValueError("median of empty list")
  mid = n // 2
  return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def bootstrap_median_ci(
    values: list[float],
    resamples: int = BOOTSTRAP_RESAMPLES,
    seed: int = BOOTSTRAP_SEED,
) -> tuple[float, float]:
  """Percentile bootstrap 95% CI of the median; seeded so reruns on the same data agree."""
  rng = random.Random(seed)
  n = len(values)
  meds = sorted(median([values[rng.randrange(n)] for _ in range(n)]) for _ in range(resamples))
  lo = meds[int(0.025 * (resamples - 1))]
  hi = meds[int(round(0.975 * (resamples - 1)))]
  return lo, hi


def verdict(ci_low: float, ci_high: float, threshold: float) -> str:
  if ci_low > 1.0 + threshold:
    return "REGRESSION"
  if ci_high < 1.0 - threshold:
    return "IMPROVEMENT"
  return "NEUTRAL"


def parse_benchmark_json(text: str) -> dict[str, float]:
  """Maps benchmark name to cpu_time in ns, skipping aggregate rows."""
  data = json.loads(text)
  out: dict[str, float] = {}
  for b in data.get("benchmarks", []):
    if b.get("run_type") == "aggregate" or "cpu_time" not in b:
      continue
    name = b["name"]
    if name in out:
      continue
    scale = TIME_UNIT_NS.get(b.get("time_unit", "ns"))
    if scale is None:
      raise GateError(f"unknown time_unit {b.get('time_unit')!r} for {name}")
    out[name] = float(b["cpu_time"]) * scale
  return out


def analyze(
    base_rounds: list[dict[str, float]],
    patch_rounds: list[dict[str, float]],
    threshold: float,
) -> list[dict]:
  """Per-benchmark paired ratios; a round counts only when both binaries reported it."""
  names = sorted({n for r in base_rounds for n in r} & {n for r in patch_rounds for n in r})
  rows = []
  for name in names:
    pairs = [
        (b[name], p[name])
        for b, p in zip(base_rounds, patch_rounds)
        if name in b and name in p and b[name] > 0
    ]
    if not pairs:
      continue
    ratios = [p / b for b, p in pairs]
    lo, hi = bootstrap_median_ci(ratios)
    rows.append({
        "name": name,
        "rounds": len(pairs),
        "base_median_ns": median([b for b, _ in pairs]),
        "patch_median_ns": median([p for _, p in pairs]),
        "ratio": median(ratios),
        "ci_low": lo,
        "ci_high": hi,
        "verdict": verdict(lo, hi, threshold),
    })
  return rows


def overall_verdict(rows: list[dict]) -> str:
  verdicts = {r["verdict"] for r in rows}
  if "REGRESSION" in verdicts:
    return "REGRESSION"
  if verdicts == {"IMPROVEMENT"}:
    return "IMPROVEMENT"
  return "NEUTRAL"


def _run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
  return subprocess.run(cmd, check=False, text=True, capture_output=True, **kw)


def _git(repo: str, *args: str) -> str:
  res = _run(["git", "-C", repo, *args])
  if res.returncode != 0:
    raise GateError(f"git -C {repo} {' '.join(args)} failed:\n{res.stderr.strip()}")
  return res.stdout.strip()


def _sha256_file(path: str) -> str:
  h = hashlib.sha256()
  with open(path, "rb") as f:
    for chunk in iter(lambda: f.read(1 << 20), b""):
      h.update(chunk)
  return h.hexdigest()


def _load_snapshot() -> dict:
  up = _run(["uptime"]).stdout.strip()
  try:
    with open("/proc/loadavg", encoding="utf-8") as f:
      load = f.read().strip()
  except OSError:
    load = ""
  return {"time": datetime.datetime.now().isoformat(timespec="seconds"), "uptime": up,
          "loadavg": load}


def _ancestor_pids() -> set[int]:
  pids = set()
  pid = os.getpid()
  while pid > 1 and pid not in pids:
    pids.add(pid)
    try:
      with open(f"/proc/{pid}/stat", encoding="utf-8") as f:
        pid = int(f.read().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
      break
  return pids


def _wait_for_submodule_users() -> None:
  # The Flutter corpus runner builds from the same submodule; never swap its sources mid-run
  mine = _ancestor_pids()
  announced = False
  while True:
    res = _run(["pgrep", "-f", OTHER_SUBMODULE_USERS])
    busy = [int(p) for p in res.stdout.split() if p.strip() and int(p) not in mine]
    if not busy:
      return
    if not announced:
      print(f"Waiting for {OTHER_SUBMODULE_USERS} (pids {busy}) to finish...", flush=True)
      announced = True
    time.sleep(30)


def _resolve_default_cpu() -> int:
  n = os.cpu_count() or 1
  # High-numbered cores are the least used by the build and the desktop on cloudtops
  return n - 17 if n > 17 else max(0, n - 1)


def _ninja(build_dir: str, log_path: str, label: str) -> None:
  with open(log_path, "w", encoding="utf-8") as log:
    res = subprocess.run(["ninja", "-C", build_dir, "txt_benchmarks"], stdout=log,
                         stderr=subprocess.STDOUT, check=False)
  if res.returncode != 0:
    with open(log_path, encoding="utf-8", errors="replace") as f:
      tail = "".join(f.readlines()[-40:])
    raise GateError(
        f"ninja txt_benchmarks failed for {label} (log: {log_path}):\n{tail}\n"
        "If --base is far from the Skia revision pinned in Flutter DEPS, the engine may not "
        "build against it; rebase the change near that revision or pass a closer --base.")


class SubmoduleGuard:
  """Restores the shared Skia submodule to its original HEAD on exit, error or signal."""

  def __init__(self, sk: str):
    self.sk = sk
    self.orig_sha = _git(sk, "rev-parse", "HEAD")
    branch = _run(["git", "-C", sk, "symbolic-ref", "-q", "--short", "HEAD"])
    self.orig_ref = branch.stdout.strip() if branch.returncode == 0 else self.orig_sha
    self.restored = False

  def restore(self) -> bool:
    if self.restored:
      return True
    old = {s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
      _run(["git", "-C", self.sk, "checkout", "-q", "-f", "--", "."])
      _run(["git", "-C", self.sk, "clean", "-fdq"])
      _run(["git", "-C", self.sk, "checkout", "-q", self.orig_ref])
      head = _run(["git", "-C", self.sk, "rev-parse", "HEAD"]).stdout.strip()
      dirty = _run(["git", "-C", self.sk, "status", "--porcelain"]).stdout.strip()
      self.restored = head == self.orig_sha and not dirty
      if self.restored:
        print(f"Restored {self.sk} to {self.orig_ref} ({self.orig_sha[:10]}), clean", flush=True)
      else:
        print(f"ERROR: failed to restore {self.sk}: HEAD={head[:10]} expected "
              f"{self.orig_sha[:10]}; dirty:\n{dirty}", file=sys.stderr, flush=True)
      return self.restored
    finally:
      for s, h in old.items():
        signal.signal(s, h)


def _raise_on_signal(signum, _frame):
  raise KeyboardInterrupt(f"signal {signum}")


def _ensure_commit(sk: str, sha: str) -> None:
  if _run(["git", "-C", sk, "cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0:
    return
  print(f"{sha[:10]} missing in {sk}; fetching origin...", flush=True)
  _run(["git", "-C", sk, "fetch", "-q", "origin"])
  if _run(["git", "-C", sk, "cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0:
    return
  _run(["git", "-C", sk, "fetch", "-q", "origin", sha])
  if _run(["git", "-C", sk, "cat-file", "-e", f"{sha}^{{commit}}"]).returncode != 0:
    raise GateError(f"base {sha} is not reachable in {sk} even after `git fetch origin`; "
                    "push the base or pick a --base that exists upstream")


def _build_pair(args, sk: str, base_sha: str, patch_path: str) -> tuple[str, str]:
  guard = SubmoduleGuard(sk)
  old = {s: signal.signal(s, _raise_on_signal) for s in (signal.SIGINT, signal.SIGTERM)}
  base_bin = os.path.join(args.out, "txt_benchmarks_base")
  patch_bin = os.path.join(args.out, "txt_benchmarks_patch")
  built = os.path.join(args.build_dir, "txt_benchmarks")
  try:
    _ensure_commit(sk, base_sha)
    _git(sk, "checkout", "-q", "--detach", base_sha)
    print(f"Building base {base_sha[:10]}...", flush=True)
    _ninja(args.build_dir, os.path.join(args.out, "ninja_base.log"), "base")
    shutil.copy2(built, base_bin)
    res = _run(["git", "-C", sk, "apply", "--binary", "--whitespace=nowarn", patch_path])
    if res.returncode != 0:
      raise GateError(f"git apply of {patch_path} onto {base_sha[:10]} failed:\n{res.stderr}")
    print("Building patch...", flush=True)
    _ninja(args.build_dir, os.path.join(args.out, "ninja_patch.log"), "patch")
    shutil.copy2(built, patch_bin)
  finally:
    ok = guard.restore()
    for s, h in old.items():
      signal.signal(s, h)
    if not ok:
      pending = sys.exc_info()[1]
      if pending is not None:
        print(f"(restore failed while handling: {pending})", file=sys.stderr)
      sys.exit(EXIT_RESTORE_FAILED)
  return base_bin, patch_bin


def _run_rounds(args, bins: list[tuple[str, str]]) -> dict[str, list[dict[str, float]]]:
  runs_dir = os.path.join(args.out, "runs")
  shutil.rmtree(runs_dir, ignore_errors=True)
  os.makedirs(runs_dir)
  results: dict[str, list[dict[str, float]]] = {name: [] for name, _ in bins}
  for r in range(args.rounds):
    # Rotating the order cancels the drift (thermal, page cache) that favors one position
    k = r % len(bins)
    for name, binary in bins[k:] + bins[:k]:
      path = os.path.join(runs_dir, f"{name}_r{r:02d}.json")
      cmd = ["taskset", "-c", str(args.cpu), binary, "--benchmark_format=json",
             f"--benchmark_filter={args.filter}", f"--benchmark_min_time={args.min_time}"]
      res = _run(cmd, cwd=args.out)
      with open(path, "w", encoding="utf-8") as f:
        f.write(res.stdout)
      if res.returncode != 0:
        raise GateError(f"{name} round {r} exited {res.returncode}:\n{res.stderr[-2000:]}")
      parsed = parse_benchmark_json(res.stdout)
      # An empty run is a filter typo or missing data, not a timing
      if not parsed:
        raise GateError(f"{name} round {r} reported zero benchmarks for --filter "
                        f"{args.filter!r} ({path})")
      results[name].append(parsed)
    print(f"round {r + 1}/{args.rounds} done", flush=True)
  return results


def _format_report(att: dict, rows: list[dict]) -> str:
  lines = [f"# Skia patch A/B on txt_benchmarks: **{att['verdict']}**", ""]
  if rows:
    lines += ["| Benchmark | base median ns | patch median ns | ratio | 95% CI | verdict |",
              "|---|---:|---:|---:|---|---|"]
    for r in rows:
      lines.append(
          f"| {r['name']} | {r['base_median_ns']:.0f} | {r['patch_median_ns']:.0f} | "
          f"{r['ratio']:.4f} | [{r['ci_low']:.4f}, {r['ci_high']:.4f}] | {r['verdict']} |")
  else:
    lines.append(att.get("reason", ""))
  lines += ["", "## Attestation", ""]
  for key in ("skia_repo", "skia_base_sha", "patch_sha256", "patch_files", "base_binary_sha256",
              "patch_binary_sha256", "rounds", "cpu", "filter", "min_time", "threshold",
              "load_before", "load_after"):
    if key in att:
      lines.append(f"- `{key}`: {att[key]}")
  return "\n".join(lines) + "\n"


def _write_outputs(out: str, att: dict, rows: list[dict]) -> None:
  att = dict(att, benchmarks=rows)
  with open(os.path.join(out, "attestation.json"), "w", encoding="utf-8") as f:
    json.dump(att, f, indent=2)
  report = _format_report(att, rows)
  with open(os.path.join(out, "report.md"), "w", encoding="utf-8") as f:
    f.write(report)
  print(report)
  print(f"Report: {os.path.join(out, 'report.md')}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
  p = _Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  p.add_argument("--skia-repo", required=True,
                 help="Skia checkout or worktree with the change (working tree counts)")
  p.add_argument("--base", help="base revision (default: merge-base of HEAD and origin/main)")
  p.add_argument("--engine-src", default=DEFAULT_ENGINE_SRC, help="Flutter engine src dir")
  p.add_argument("--build-dir", default="out/host_release",
                 help="ninja out dir, relative to --engine-src unless absolute")
  p.add_argument("--out", required=True, help="output dir for binaries, runs and report")
  p.add_argument("--rounds", type=int, default=20)
  p.add_argument("--cpu", type=int, default=None, help="core to pin to (default: nproc-17)")
  p.add_argument("--filter", default=DEFAULT_FILTER, help="--benchmark_filter regex")
  p.add_argument("--min-time", type=float, default=0.3, help="--benchmark_min_time seconds")
  p.add_argument("--threshold", type=float, default=0.02,
                 help="CI must clear 1 +/- threshold for REGRESSION / IMPROVEMENT")
  args = p.parse_args(argv)
  if args.rounds < 3:
    p.error("--rounds must be at least 3 for a meaningful CI")
  args.skia_repo = os.path.abspath(os.path.expanduser(args.skia_repo))
  args.engine_src = os.path.abspath(os.path.expanduser(args.engine_src))
  args.build_dir = os.path.join(args.engine_src, os.path.expanduser(args.build_dir))
  args.out = os.path.abspath(os.path.expanduser(args.out))
  if args.cpu is None:
    args.cpu = _resolve_default_cpu()
  return args


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  sk = os.path.join(args.engine_src, SUBMODULE_REL)
  os.makedirs(args.out, exist_ok=True)
  try:
    if not os.path.isdir(sk) or not os.path.isdir(args.build_dir):
      raise GateError(f"missing submodule {sk} or build dir {args.build_dir}")
    if os.path.realpath(args.skia_repo) == os.path.realpath(sk):
      raise GateError("--skia-repo must be a separate checkout, not the engine submodule")
    base = args.base or _git(args.skia_repo, "merge-base", "HEAD", "origin/main")
    base_sha = _git(args.skia_repo, "rev-parse", f"{base}^{{commit}}")
    patch = subprocess.run(["git", "-C", args.skia_repo, "diff", "--binary", base_sha],
                           check=True, capture_output=True).stdout
    files = _git(args.skia_repo, "diff", "--name-only", base_sha).splitlines()
    untracked = [ln[3:] for ln in _git(args.skia_repo, "status", "--porcelain").splitlines()
                 if ln.startswith("??")]
    if untracked:
      print(f"WARNING: untracked files are not in the patch: {untracked[:10]}", flush=True)
    patch_path = os.path.join(args.out, "patch.diff")
    with open(patch_path, "wb") as f:
      f.write(patch)
    att = {
        "skia_repo": args.skia_repo, "skia_base_sha": base_sha,
        "patch_sha256": hashlib.sha256(patch).hexdigest(), "patch_files": files,
        "rounds": args.rounds, "cpu": args.cpu, "filter": args.filter,
        "min_time": args.min_time, "threshold": args.threshold,
    }
    if not patch:
      att.update(verdict="UNAFFECTED", reason="Empty diff against base; nothing to measure.")
      _write_outputs(args.out, att, [])
      return EXIT_OK

    with open(PEER_LOCK, "w", encoding="utf-8") as lock:
      print(f"Waiting for lock {PEER_LOCK}...", flush=True)
      fcntl.flock(lock, fcntl.LOCK_EX)
      _wait_for_submodule_users()
      dirty = _git(sk, "status", "--porcelain")
      if dirty:
        raise GateError(f"{sk} is dirty; refusing to touch it:\n{dirty}")
      base_bin, patch_bin = _build_pair(args, sk, base_sha, patch_path)

    att["base_binary_sha256"] = _sha256_file(base_bin)
    att["patch_binary_sha256"] = _sha256_file(patch_bin)
    if att["base_binary_sha256"] == att["patch_binary_sha256"]:
      att.update(verdict="UNAFFECTED",
                 reason="Base and patch binaries are byte-identical; the diff does not reach "
                        "txt_benchmarks.")
      _write_outputs(args.out, att, [])
      return EXIT_OK

    icu = os.path.join(args.out, "icudtl.dat")
    if os.path.lexists(icu):
      os.remove(icu)
    os.symlink(os.path.join(args.build_dir, "icudtl.dat"), icu)
    att["load_before"] = _load_snapshot()
    results = _run_rounds(args, [("base", base_bin), ("patch", patch_bin)])
    att["load_after"] = _load_snapshot()
    rows = analyze(results["base"], results["patch"], args.threshold)
    if not rows:
      raise GateError("no benchmark appeared in both base and patch runs")
    att["verdict"] = overall_verdict(rows)
    _write_outputs(args.out, att, rows)
    return EXIT_REGRESSION if att["verdict"] == "REGRESSION" else EXIT_OK
  except GateError as e:
    print(f"ERROR: {e}", file=sys.stderr)
    return EXIT_ERROR
  except KeyboardInterrupt as e:
    print(f"Interrupted ({e})", file=sys.stderr)
    return EXIT_ERROR


if __name__ == "__main__":
  sys.exit(main())
