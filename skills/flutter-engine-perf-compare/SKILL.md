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

## Cloudtop Build + Remote Measure Workflow

For bisects and long A/B chains: build on the Cloudtop (many cores), measure on a dedicated
workstation (quiet, `performance` governor, one isolated core). Scripts in `scripts/`:

| Script | Runs on | Does |
|---|---|---|
| `remote_build_push.sh` | Cloudtop | builds one point (Skia sha + LTO mode), stages and pushes it |
| `perf_remote.py` | Cloudtop | `check`, `has`, `push`, `enqueue`, `status`, `fetch` over ssh |
| `make_plan.py` | Cloudtop | writes an interleaved-rounds plan for a chain of points |
| `remote_runner.sh` | workstation | resumable queue: runs plan lines, writes `raw/<out>.json` |
| `eval_rounds.py` | Cloudtop | per-step median ratio, bootstrap 95% CI, per-round agreement |

Settings come from flags or env; nothing personal is hardcoded in the scripts:

```bash
export PERF_HOST=jlavrova.rdu.corp.google.com   # measuring workstation
export PERF_REMOTE_DIR=perf_weekly              # remote root, relative to remote $HOME
export PERF_CM=${PERF_CM:-/tmp/${USER}_perf_cm}  # ControlPath, the scripts' default
S=~/.gemini/config/skills/flutter-engine-perf-compare/scripts
CO=~/.gemini/jetski/scratch/perf_step0/sweep2   # Flutter monorepo checkout used for builds
```

**One ControlMaster, opened by the user.** Every ssh/rsync uses `-o BatchMode=yes -o
ControlMaster=no -o ControlPath=$PERF_CM`, because each new ssh costs her a security-key touch.
When the master is dead, every script exits 3 and prints the command; ask her to run it once:

```bash
ssh -fNM -o ControlPersist=10h -o ControlPath=$PERF_CM $PERF_HOST
python3 $S/perf_remote.py check
```

Never open another ssh as a workaround. The remote root needs `txt_fonts/` (created from the
first pushed point's fonts) and `ext_fonts/` (fonts for `txt_ext_benchmarks --font-dir`).

**Build and push a point** (one per call; staged name `<sha10>_<mode>`, shared by every agent):

```bash
bash $S/remote_build_push.sh --checkout $CO --sha <40-hex skia sha> --mode nolto \
  [--flutter-sha <sha>] [--ext-src <skparagraph_ext_benchmarks.cc>] [--skia-mirror <skia repo>]
```

- It checks the master first and exits 0 at once if the remote `bin/<point>/.complete` exists,
  so check `python3 $S/perf_remote.py has <point>` and reuse other agents' points instead of
  building again. A local `perf_stage/<point>` with the same sha is pushed without a rebuild.
- `--mode`: `nolto` (gn `--no-lto`), `lto` (Full LTO, what Flutter ships on Linux) or `thinlto`
  (gn LTO with `-flto` patched to `-flto=thin` in `build/config/compiler/BUILD.gn` for the
  build only). Out dir `out/perf_<mode>`. One build at a time per checkout (flock); patched
  files and the Skia checkout are restored on exit, and it refuses to start over local edits.
- The push uploads into `bin/<point>.tmp`, checks `SHA256SUMS`, links the fixture path baked
  into the binary to `txt_fonts`, renames the dir to `bin/<point>` and touches `.complete` last.
  The runner never starts a point without `.complete`: never push by hand with plain `rsync`
  (a manual push without `.complete` once blocked the queue for 3 hours); rerun the push.

**Plan, enqueue, collect:**

```bash
python3 $S/make_plan.py --prefix bis1 --rounds 3 --reps 10 --out /tmp/bis1.txt \
  [--anchor <point> --anchor-every 4] <point A> <point B> <point C>
python3 $S/perf_remote.py enqueue /tmp/bis1.txt
python3 $S/perf_remote.py status
python3 $S/perf_remote.py fetch <local raw dir>
python3 $S/eval_rounds.py --raw <local raw dir> --prefix bis1 --rounds 3 <A> <B> <C>
```

- Odd rounds run the points in order, even rounds reversed, with a `BARRIER` after each round.
  A point that is not pushed yet is passed over until the next `BARRIER` (`STRICT=1` on the
  runner keeps strict order). Points listed in the remote `skip_points` file are ignored.
- `enqueue` uploads `remote_runner.sh` and the plan, appends it to `lanes.txt` (earlier lanes
  have priority, and the runner rereads the file every pass) and starts the runner detached on
  the remote side unless it already runs. Runner env: `CPU` (default 11), `SIB` (SMT sibling,
  default 23), `LOAD_MAX`, `BUSY_MAX`, `WARMUP`. Its log is `logs/runner.log` on the remote.
  An older ad-hoc `rdu_runner.sh` still running there uses the same default core: stop it (or
  set another `CPU`) first, since the single-runner lock covers only `remote_runner.sh`.
- `eval_rounds.py` marks a step `CONFIRMED` only when all rounds exist, every round agrees on
  the sign, `|ratio - 1| > --threshold` (default 0.02) and the CI excludes 1.

**Launching from the agent.** A build takes minutes to tens of minutes: start
`remote_build_push.sh` with `run_command` `WaitMsBeforeAsync=500` and
`NotificationTimeoutSeconds` (e.g. 3600), never with `nohup` or `&` inside a synchronous call
(a hook blocks it). Only the remote runner detaches,
on the remote side, inside `perf_remote.py enqueue`. Do not poll; wait for the notification.

Tests: `python3 -m unittest test_flutter_engine_perf_runner` in `~/.gemini/config/tests`.
