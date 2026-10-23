#!/usr/bin/env python3
"""Post-loop audit lens: "remove work rather than add machinery".

Ranks every commit in <base>..<head> by its measured win (from `Perf:` trailers written by
bench_compare.py, NOISE metrics excluded) and by win per changed source line, and flags:
  NO_MEASUREMENT   no Perf: trailer (breaks 1 optimization = 1 measured commit)
  SINGLE_BENCH     the win is measured on one input only (possible overfitting)
  REGRESSES_OTHER  some Perf: metric has a REGRESSION verdict
  MACHINERY        adds caches, memo tables, lookup maps or new static state
  NOT_IN_LEDGER    measured commit missing from the optimizations ledger
and tags REMOVES_WORK when a win comes with a net decrease of source lines.
"""

import argparse
import re
import subprocess
import sys

# Resolved via the script's own directory, which Python puts first on sys.path
import opt_ledger

PERF_RE = re.compile(r"^Perf:\s+(\S+)\s.*?\(([+-]?[\d.]+)%.*\)\s*"
                     r"(WIN|NOISE|REGRESSION|INCONSISTENT|UNREPLICATED)?\s*$", re.M)
TEST_RE = re.compile(r"(^|/)(tests?|testing|benchmarks?|bench)/|_test\.|Test\.|_tests?\.|"
                     r"\.(md|txt|json|yaml|png)$")
MACHINERY_RE = re.compile(
    r"\b(cache|cached|memo\w*|lut|lookup|unordered_map|flat_hash_map|HashMap|LinkedHashMap|"
    r"SkTHashMap|THashMap|SkLRUCache|Expando|WeakMap|Map<|Set<)\b|"
    r"^\s*static\s+(?!constexpr|inline|const\b)[\w:<>,\s]+?\s+\w+\s*[;={]", re.I)
COMMENT_RE = re.compile(r"^\s*(//|#|\*|/\*)")


def git(repo, *args):
    return subprocess.run(["git", "-C", repo, *args], capture_output=True, text=True,
                          check=True).stdout


def counted(delta, verdict, want, threshold):
    # Hand-written trailers have no verdict, so fall back to the point delta and threshold
    if verdict:
        return verdict == want
    return delta < -threshold if want == "WIN" else delta > threshold


def commit_info(repo, sha, threshold):
    body = git(repo, "show", "-s", "--format=%B", sha)
    subject = body.splitlines()[0] if body else ""
    perf = [(m.group(1), float(m.group(2)), m.group(3)) for m in PERF_RE.finditer(body)]
    added = deleted = 0
    for line in git(repo, "show", "--numstat", "--format=", sha).splitlines():
        a, d, path = line.split("\t", 2)
        # Binary files report "-" and tests/docs do not count toward the cost of a win
        if a == "-" or TEST_RE.search(path):
            continue
        added, deleted = added + int(a), deleted + int(d)
    machinery = []
    # Explicit prefixes keep the "+++ b/" parsing independent of the user's diff.noprefix
    diff = git(repo, "show", "-U0", "--format=", "--src-prefix=a/", "--dst-prefix=b/", sha)
    current = ""
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("+") and current and not TEST_RE.search(current):
            if not COMMENT_RE.match(line[1:]) and MACHINERY_RE.search(line[1:]):
                machinery.append(line[1:].strip()[:80])
    # NOISE metrics prove nothing either way, so only verdict-backed deltas count
    win = max([-d for _, d, v in perf if counted(d, v, "WIN", threshold)], default=0.0)
    flags = []
    if not perf:
        flags.append("NO_MEASUREMENT")
    # Each measured metric is expected to be a distinct input; one input may be overfitted
    if win > 0 and len({name for name, _, v in perf if v != "REGRESSION"}) < 2:
        flags.append("SINGLE_BENCH")
    if any(counted(d, v, "REGRESSION", threshold) for _, d, v in perf):
        flags.append("REGRESSES_OTHER")
    if machinery:
        flags.append("MACHINERY")
    if win > 0 and added < deleted:
        flags.append("REMOVES_WORK")
    churn = added + deleted
    return {"sha": sha[:10], "subject": subject[:70], "perf": perf, "win": win,
            "added": added, "deleted": deleted, "churn": churn,
            "win_per_loc": win / max(churn, 1), "flags": flags, "machinery": machinery}


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base", help="Base ref (e.g. origin/main or merge-base)")
    p.add_argument("head", nargs="?", default="HEAD")
    p.add_argument("--repo", default=".")
    p.add_argument("--paths", nargs="*", default=[], help="Limit to commits touching paths")
    p.add_argument("--threshold", type=float, default=1.0,
                   help="|delta%%| that counts for trailers without a verdict")
    p.add_argument("--no-ledger", action="store_true", help="Skip the ledger cross-check")
    args = p.parse_args()

    shas = git(args.repo, "rev-list", "--no-merges", "--reverse",
               f"{args.base}..{args.head}", "--", *args.paths).split()
    if not shas:
        sys.exit("No commits in range")
    rows = [commit_info(args.repo, s, args.threshold) for s in shas]

    if not args.no_ledger:
        entries = []
        for path in opt_ledger.ledger_dir(args.repo).glob("*.txt"):
            entries += [e for e in opt_ledger.read_entries(path) if e["status"] == "WIN"]
        for r in rows:
            # Rebases rewrite SHAs, so a WIN whose hypothesis matches the subject also counts
            if r["perf"] and not any(
                    e["sha"].startswith(r["sha"]) or r["sha"].startswith(e["sha"]) or
                    opt_ledger.similarity(e["hypothesis"], r["subject"]) >= 0.5
                    for e in entries):
                r["flags"].append("NOT_IN_LEDGER")

    def table(title, ordered):
        print(f"\n### {title}\n")
        print("| # | sha | subject | win % | +/- src LOC | win %/LOC | flags |")
        print("|---|---|---|---:|---:|---:|---|")
        for i, r in enumerate(ordered, 1):
            print(f"| {i} | `{r['sha']}` | {r['subject']} | {r['win']:.1f} | "
                  f"+{r['added']}/-{r['deleted']} | {r['win_per_loc']:.3f} | "
                  f"{', '.join(r['flags'])} |")

    print(f"## Perf branch audit: {args.base}..{args.head} ({len(rows)} commits)")
    table("Ranked by win", sorted(rows, key=lambda r: -r["win"]))
    table("Ranked by win per changed source line",
          sorted(rows, key=lambda r: -r["win_per_loc"]))

    print("\n### Machinery added (review: can the work be removed instead?)\n")
    any_machinery = False
    for r in rows:
        for line in r["machinery"][:5]:
            any_machinery = True
            print(f"- `{r['sha']}` `{line}`")
    if not any_machinery:
        print("- none")

    counts = {}
    for r in rows:
        for f in r["flags"]:
            counts[f] = counts.get(f, 0) + 1
    print("\n### Flag summary\n")
    for f, n in sorted(counts.items()):
        print(f"- {f}: {n}")
    if not counts:
        print("- clean")


if __name__ == "__main__":
    main()
