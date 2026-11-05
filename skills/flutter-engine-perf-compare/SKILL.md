---
name: flutter-engine-perf-compare
description: Compare SkParagraph/txt performance (speed and memory) across two commits or dates in the Flutter Engine using txt_benchmarks.
---

# Flutter Engine SkParagraph Performance Comparison (A/B Test)

This skill automates A/B testing of SkParagraph (`txt_benchmarks`) in the Flutter Engine across two points in time (SHAs or dates). It handles checking out the engine, syncing dependencies (`gclient sync`), building the benchmarks, running them with JSON output, and generating a comparative Markdown report.

## When to use
Trigger this skill when asked to:
- "Сравни производительность СкПараграфа с <date> по <date>"
- "Измерь разницу в скорости/памяти между коммитами"
- "Сделай A/B тест txt_benchmarks во Flutter Engine"
- "compare SkParagraph performance across time"

## Prerequisites
- You must run this within a standard Flutter Engine checkout (where `src/flutter` and `src/.gclient` exist).
- The current working directory for the script should be the `src` directory of the engine (e.g., `/usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter/engine/src`).

## How to use

Run the provided Python script `perf_compare.py`:

```bash
python3 ~/.gemini/config/skills/flutter-engine-perf-compare/scripts/perf_compare.py \
  --engine-dir <path_to_engine_src> \
  --baseline <old_sha_or_date> \
  --target <new_sha_or_branch> \
  --filter "<optional_benchmark_filter>"
```

### Arguments:
- `--engine-dir`: Path to the `src` directory of the Flutter Engine.
- `--baseline`: The starting point (git SHA, branch, or date like `"2 weeks ago"`). The script uses `git rev-parse` or `git rev-list -n 1 --before="<date>"` to find the exact commit in `src/flutter`.
- `--target`: The ending point (usually `HEAD` or a specific branch/SHA).
- `--filter`: (Optional) Google benchmark filter, e.g., `Paragraph*` or `*bidi*`. Defaults to `*`.
- `--build-dir`: (Optional) Ninja output directory (relative to `src`), defaults to `out/host_release`.

## What the script does
1.  **Preparation**: Saves the current branch state.
2.  **Baseline**:
    - Checks out the baseline commit in `src/flutter`.
    - Runs `gclient sync -D` in `src`.
    - Builds `txt_benchmarks` using `ninja -C <build-dir> txt_benchmarks`.
    - Runs the benchmark, dumping results to `baseline.json`.
3.  **Target**:
    - Checks out the target commit in `src/flutter`.
    - Runs `gclient sync -D` in `src`.
    - Builds `txt_benchmarks`.
    - Runs the benchmark, dumping results to `target.json`.
4.  **Reporting**:
    - Compares `baseline.json` and `target.json`.
    - Prints a Markdown table showing CPU Time and Memory changes.
5.  **Cleanup**: Restores the original branch in `src/flutter`.

## Important Notes on Memory Tracking
`txt_benchmarks` uses Google Benchmark. Google Benchmark tracks real/cpu time by default. It can track memory allocations if the benchmark is instrumented or run under a tool, but natively it requires the platform to support memory queries or custom counters. The script will parse whatever custom counters `txt_benchmarks` outputs. If memory isn't explicitly output by `txt_benchmarks` via counters, the script will focus on CPU time.

## Skia Patch A/B Gate (`skia_patch_ab.py`)

Mandatory pre-upload speed gate for Skia diffs that touch executable C/C++ code under
`modules/{skparagraph,skunicode,skshaper}/{src,include}/`, `include/core/`,
`include/private/base/`, `src/base/`, or `src/core/` (`amnesia-precommit` Phase 4, enforced by
`[PERF-CHECK GUARD]` in `hooks/pre_tool_guard.py`). Correctness corpora do not measure speed:
Skia `daf82dad48` (`SkASSERT_RELEASE` in the `SkSpan` ctor) slowed `LongLayout` by 21.6% unnoticed.

```bash
python3 ~/.gemini/config/skills/flutter-engine-perf-compare/scripts/skia_patch_ab.py \
  --skia-repo <skia checkout or worktree with the change> --out /tmp/skia_patch_ab
```

- The patch is `git -C <skia-repo> diff --binary <base>`, so uncommitted edits count; untracked
  files do not (the script warns). `--base` defaults to `merge-base HEAD origin/main`.
- It builds `txt_benchmarks` twice in the shared engine checkout (`--engine-src`, default
  `~/.gemini/jetski/scratch/flutter/engine/src`, `--build-dir out/host_release`) with the Skia
  submodule at `<base>` and at `<base>` + patch, then always restores the submodule (also on
  Ctrl-C/SIGTERM). It refuses a dirty submodule and waits for `run_flutter_paragraph_tests.py`
  and other `skia_patch_ab.py` runs.
- Rounds (`--rounds 20`) run both binaries interleaved with rotating order, pinned by `taskset`
  to `--cpu` (default `nproc-17`). Per benchmark it reports the median per-round patch/base
  `cpu_time` ratio and a seeded bootstrap 95% CI of that median.
- Outputs: `<out>/report.md` (table + attestation) and `<out>/attestation.json` (base SHA, patch
  sha256, binary sha256s, rounds, cpu, filter, load before/after).

| Verdict | Meaning | Exit |
|---|---|---|
| `UNAFFECTED` | Empty diff, or both binaries byte-identical (change does not reach `txt_benchmarks`) | 0 |
| `NEUTRAL` | CI overlaps `[1 - threshold, 1 + threshold]` (`--threshold 0.02`) | 0 |
| `IMPROVEMENT` | Every benchmark has CI high `< 1 - threshold` | 0 |
| `REGRESSION` | Any benchmark has CI low `> 1 + threshold`; blocks upload until fixed or the user accepts it | 2 |

Other exits: 1 bad arguments, 3 setup/build/run error (ninja log tail printed; a `--base` far
from the Skia revision pinned in Flutter `DEPS` may not build), 4 submodule restore failed
(inspect `flutter/third_party/skia` by hand before anything else uses it).

Runtime: two incremental `ninja` builds (a few minutes after a header change in `include/core/`)
plus about 5–8 minutes for 20 rounds with the default filter. Put the `report.md` table rows
into the CL `Tested:` block. Tests: `python3 -m unittest test_skia_patch_ab` in `scripts/`.
