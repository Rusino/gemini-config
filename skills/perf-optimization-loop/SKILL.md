---
name: perf-optimization-loop
description: >-
  Iterative C++/Dart/TS performance optimization protocol for SkParagraph, WebParagraph, and
  SkShaper (`opt_ledger.py`, `bench_compare.py` bootstrap 95% CI `Perf:` trailers, and
  `opt_branch_audit.py`). Trigger when asked to "оптимизируй производительность", "ускорь layout",
  "сделай perf-цикл", "замерь ускорение", "проверь perf-ветку", "сравни производительность двух
  версий", or "perf optimization loop".
---

# Performance Optimization Loop (`perf-optimization-loop`)

## Scripts

- Ledger: [`opt_ledger.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/perf-optimization-loop/scripts/opt_ledger.py)
- Statistics + trailers: [`bench_compare.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/perf-optimization-loop/scripts/bench_compare.py)
- Branch audit: [`opt_branch_audit.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/perf-optimization-loop/scripts/opt_branch_audit.py)

Set `S=~/.gemini/config/skills/perf-optimization-loop/scripts` in the commands below.

## The Ledger

One plain-text file per branch at `$(git rev-parse --git-common-dir)/optimizations/<branch>.txt`.
It is shared by all worktrees of the repo, never tracked by git, and outlives any chat.

```bash
python3 $S/opt_ledger.py --repo <repo> path
python3 $S/opt_ledger.py --repo <repo> check "cache shaped runs per style"
python3 $S/opt_ledger.py --repo <repo> show [--all-branches]
python3 $S/opt_ledger.py --repo <repo> add --status WIN --delta -7.4 --sha abc1234 \
  --bench layout_large --hypothesis "skip second measureText for single-run lines"
python3 $S/opt_ledger.py --repo <repo> add --status REJECTED --bench layout_large \
  --hypothesis "memoize font string" --reason "+0.3%, inside CI; adds a Map per paragraph"
```

`WIN` requires `--delta` and `--sha`; `REJECTED` / `NOISE` require `--reason`.

## The Loop

Before the first iteration, fix the benchmark: the exact command, build config (release or
profile, never debug), inputs (several sizes and scripts, not one string), sample count
(≥ 10, prefer 30) and output format. Record the baseline once per session.

`bench_compare.py` accepts nanobench `--outResultsFile` JSON (metric `<bench>/<config>`),
hyperfine `--export-json` (one command per file is named by `--metric`; for several commands
per file use `hyperfine --command-name`), `{"metric": [samples]}` JSON, or one number per
line. Every metric must be lower-is-better, and each metric should be a distinct input
(e.g. `short`, `long`, `multistyle`), because the audit treats one measured metric as one
input.

For each hypothesis:

1. **Check** `opt_ledger.py check "<idea>"`. If `SEEN` shows a `REJECTED` / `NOISE` entry, do
   not retry unless the code it depended on changed; say why when you retry.
2. **Profile first**: pick the hypothesis from a profile (perf, Chrome DevTools, `--trace`),
   not from intuition. Prefer ideas that delete work (a pass, a copy, an allocation, a
   re-shape) over ideas that add machinery (caches, tables, new state).
3. **Measure** baseline and candidate with the same binary config in at least 2 rounds,
   interleaved (A B A B), and pass the round files comma-separated in the same order:
   `bench_compare.py old.r1.json,old.r2.json new.r1.json,new.r2.json --unit us`.
   - **Preflight every run**: `uptime` load average well below `nproc` and no parallel
     `ninja` / `clang` / `felt` / `flutter test` (`ps aux`); re-check after the last round.
     Concurrent builds shift whole runs by 2–3x and produce fake −60% / +180% "results".
   - A single round can show a confident `REGRESSION` that other rounds do not reproduce;
     that is why a pooled `WIN` / `REGRESSION` becomes `INCONSISTENT` unless every round
     agrees, and `UNREPLICATED` with one round.
4. **Decide** from the verdict:
   - `WIN` on the target metric and no `REGRESSION` elsewhere: commit (step 5)
   - `NOISE`: revert, `add --status NOISE --reason "<delta, CI>"`
   - `INCONSISTENT` / `UNREPLICATED`: not a result yet; check machine load and re-measure
   - `REGRESSION` or test failures: revert, `add --status REJECTED --reason "<why>"`
5. **Commit** exactly one optimization per commit. Paste all `Perf:` trailers from
   `bench_compare.py` (they end with the verdict) into the message above `Assisted-by:` /
   `Change-Id:`, then `add --status WIN --delta <target delta> --sha HEAD`.
6. Re-run correctness tests (`skia-test-paragraph`, `webparagraph-corpus`) at least every few
   commits and always before the audit.

Never batch two ideas into one commit: the audit and any later bisect need per-idea numbers.

## Post-Loop Audit: Remove Work Rather Than Add Machinery

```bash
python3 $S/opt_branch_audit.py --repo <repo> origin/main HEAD [--paths <dir> ...]
```

It prints two rankings (by win and by `win % / changed source LOC`, tests and docs excluded;
`NOISE` metrics never count as a win), the added lines that look like machinery, and
per-commit flags: `NO_MEASUREMENT`, `SINGLE_BENCH`, `REGRESSES_OTHER`, `MACHINERY`,
`NOT_IN_LEDGER` (matched by SHA or, after a rebase, by hypothesis vs. subject), and the
positive tag `REMOVES_WORK`. Then review by hand:

- For every `MACHINERY` commit ask whether the cached work can be removed or done once
  structurally instead; a cache needs an invalidation story and a memory bound in the message.
- Commits at the bottom of the per-LOC ranking with small wins are candidates to drop from
  the branch before review.
- `SINGLE_BENCH` wins must be re-measured on at least one other input (different length,
  script, or style mix) before upload.

## Automatic Enforcement

- `hooks/context_guard.py` injects `[PERF LOOP TRIGGER]` at turn start when the prompt uses
  perf vocabulary (ускорь, производительность, замер, perf, speed up, benchmark, ...).
- `hooks/pre_tool_guard.py`, only in repos with `modules/skparagraph` or `lib/web_ui/lib`:
  - `[PERF-CLAIM GUARD]` denies a `git commit` whose subject claims a speedup without a
    `Perf:` trailer.
  - `[PERF-AUDIT GUARD]` denies `git push` / `git cl upload` / `gh pr create` while your own
    unpublished commits include speedup claims and `opt_branch_audit.py` has not audited the
    current HEAD (it records the audited HEAD in `.git/optimizations/.audited`). The audit is
    required even when every commit carries `Perf:`.
  - A commit that is not a runtime perf claim (e.g. "Optimize imports") opts out with a
    `Perf-Exempt: <reason>` line; `Revert "..."` subjects are ignored.

## Comparing Two Versions (no loop)

To compare e.g. a week-old revision with HEAD, collect samples for both revisions with the
same harness, then run `bench_compare.py` and, optionally, `opt_branch_audit.py <old> <new>`
for a per-commit view of the range.
