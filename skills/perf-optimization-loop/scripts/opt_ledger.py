#!/usr/bin/env python3
"""Hypothesis ledger for iterative performance work (optimizations.txt protocol).

Every tested hypothesis is recorded once, whether it won or was rejected, so a continuation
chat (after /compact or handoff) never re-tries an idea that was already measured.

The ledger lives inside the repository's git common dir, so it is shared by all worktrees,
never tracked by git, and survives chat boundaries:
    $(git rev-parse --git-common-dir)/optimizations/<branch>.txt
"""

import argparse
import datetime
import pathlib
import re
import subprocess
import sys

STATUSES = ("WIN", "REJECTED", "NOISE")
SEP = " | "
FIELDS = ("time", "status", "delta", "sha", "bench", "hypothesis", "reason")


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def ledger_dir(repo):
    common = pathlib.Path(git(repo, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = (pathlib.Path(repo) / common).resolve()
    return common / "optimizations"


def ledger_path(repo, branch):
    if not branch:
        branch = git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    # Detached HEAD has no branch name; key the ledger by commit so it is still unique
    if branch == "HEAD":
        branch = "detached-" + git(repo, "rev-parse", "--short", "HEAD")
    return ledger_dir(repo) / (re.sub(r"[^A-Za-z0-9._-]+", "_", branch) + ".txt")


def parse_line(line):
    parts = line.rstrip("\n").split(SEP)
    if line.startswith("#") or len(parts) != len(FIELDS):
        return None
    return dict(zip(FIELDS, parts))


def read_entries(path):
    if not path.exists():
        return []
    return [e for e in map(parse_line, path.read_text().splitlines()) if e]


def tokens(text):
    # Crude plural folding so "paragraphs" and "paragraph" count as the same idea word
    return {t.rstrip("s") for t in re.findall(r"[a-z0-9_]+", text.lower()) if len(t) > 2}


def similarity(a, b):
    # Token Jaccard only: character-level ratios score unrelated English sentences ~0.4
    ta, tb = tokens(a), tokens(b)
    return len(ta & tb) / len(ta | tb) if ta and tb else 0.0


def cmd_add(args):
    if args.status == "WIN" and (args.delta is None or not args.sha):
        sys.exit("WIN requires --delta and --sha (1 optimization = 1 measured commit)")
    if args.status in ("REJECTED", "NOISE") and not args.reason:
        sys.exit(f"{args.status} requires --reason so the idea is not retried blindly")
    for field in (args.hypothesis, args.reason or "", args.bench or ""):
        if SEP.strip() in field or "\n" in field:
            sys.exit("Fields must not contain '|' or newlines")
    # Store a verified 12-char SHA so "HEAD" or a typo cannot silently break audit matching
    try:
        sha = git(args.repo, "rev-parse", "--short=12", "--verify", args.sha + "^{commit}") \
            if args.sha else None
    except subprocess.CalledProcessError:
        sys.exit(f"--sha {args.sha} is not a commit in {args.repo}")
    path = ledger_path(args.repo, args.branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.write_text("# " + SEP.join(FIELDS) + "\n")
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    delta = "-" if args.delta is None else f"{args.delta:+.2f}%"
    row = [now, args.status, delta, sha or "-", args.bench or "-", args.hypothesis,
           args.reason or "-"]
    with path.open("a") as f:
        f.write(SEP.join(row) + "\n")
    print(f"{path}: {SEP.join(row)}")


def cmd_check(args):
    paths = sorted(ledger_dir(args.repo).glob("*.txt"))
    hits = []
    for path in paths:
        for e in read_entries(path):
            score = similarity(args.query, e["hypothesis"])
            if score >= args.threshold:
                hits.append((score, path.stem, e))
    hits.sort(key=lambda h: -h[0])
    if not hits:
        print("NO_MATCH: hypothesis not in any ledger of this repo, safe to try")
        return
    print(f"SEEN: {len(hits)} similar entries, read them before re-trying")
    for score, branch, e in hits[: args.limit]:
        print(f"  {score:.2f} [{branch}] {e['status']} {e['delta']} {e['sha']} "
              f"{e['hypothesis']} -- {e['reason']}")


def cmd_show(args):
    paths = (sorted(ledger_dir(args.repo).glob("*.txt")) if args.all_branches
             else [ledger_path(args.repo, args.branch)])
    for path in paths:
        entries = read_entries(path)
        print(f"== {path} ({len(entries)} entries)")
        for e in entries:
            print(f"  {e['time']} {e['status']:<8} {e['delta']:>8} {e['sha']:<10} "
                  f"{e['bench']}: {e['hypothesis']}" +
                  ("" if e["reason"] == "-" else f" -- {e['reason']}"))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--repo", default=".", help="Path inside the git repository")
    p.add_argument("--branch", help="Ledger key (default: current branch)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("path", help="Print the ledger path for the branch")

    a = sub.add_parser("add", help="Record a tested hypothesis")
    a.add_argument("--status", required=True, choices=STATUSES)
    a.add_argument("--hypothesis", required=True)
    a.add_argument("--delta", type=float, help="Median change in percent (negative = faster)")
    a.add_argument("--sha", help="Commit that landed the win")
    a.add_argument("--bench", help="Benchmark/metric the delta refers to")
    a.add_argument("--reason", help="Why rejected / noise, or a short note")

    c = sub.add_parser("check", help="Search all ledgers of this repo for a similar idea")
    c.add_argument("query")
    c.add_argument("--threshold", type=float, default=0.2)
    c.add_argument("--limit", type=int, default=10)

    s = sub.add_parser("show", help="Print the ledger")
    s.add_argument("--all-branches", action="store_true")

    args = p.parse_args()
    if args.cmd == "path":
        print(ledger_path(args.repo, args.branch))
    else:
        {"add": cmd_add, "check": cmd_check, "show": cmd_show}[args.cmd](args)


if __name__ == "__main__":
    main()
