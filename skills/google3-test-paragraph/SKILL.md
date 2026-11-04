---
name: google3-test-paragraph
description: >-
  Run local google3 A/B transitive reverse-dependency tests in CitC for SkParagraph and SkUnicode
  changes (1,139 fast targets in ~10-13 min per leg, 12 slow targets over 600s separately) with
  autoroller include/BUILD rewrites and Run Attestation. Trigger when asked to "протестируй
  параграф в гугл3", "прогони google3 A/B для skparagraph", "проверь skparagraph в google3", or
  "test paragraph in google3".
---

# Testing Custom Skia / SkParagraph / SkUnicode in `google3` (`google3-test-paragraph`)

This skill runs a local `google3` A/B test corpus in a CitC workspace for changes to `modules/skparagraph/` and `modules/skunicode/` without creating numbered Piper CLs or waiting ~55 minutes for `Canary-G3` (~17–20k fixed TAP targets in `third_party/skia/METADATA`). On Linux Cloudtop (`/google/src/cloud/`), this corpus **replaces `Canary-G3`** for `modules/skparagraph/` and `modules/skunicode/` (running before `CR+1` with zero submit hold).

Why this corpus replaces `Canary-G3` for `skparagraph` / `skunicode` and complements `flutter-test-paragraph-engine`:
- Out of **1,890** transitive `google3` test targets (BFS depth 1–4 from `skparagraph` and `skunicode`, excluding `//third_party/flutter_engine:skia`, `//third_party/skia/HEAD:skia_build_test`, and the eternal 3600s non-paragraph `skottie_renderer_test` from the BFS traversal so `skia_build_test` does not pull in non-paragraph core Skia consumers), **741** are `manual|notap|no_presubmit` and **1,149** are runnable on Forge; pinning `//third_party/skia/HEAD:skia_build_test` and `//third_party/skia/HEAD:skia_build_test_linux` directly into the fast tier gives **1,151** runnable targets.
- `Canary-G3` runs only **157** of those 1,149 transitive runnable paragraph/unicode tests (**159** of 1,151 including the 2 pinned `skia_build_test*` gates), leaving **992 runnable `google3` tests** (`925 cc_test`, `38 java_test`, `10` iOS, `6` WASM) completely uncovered until downstream teams break post-roll.
- Skia's own `modules/skparagraph/tests/SkParagraphTest.cpp` has **0 reverse dependencies** in `google3` (always run `skia-test-paragraph` for Skia unit tests and `flutter-test-paragraph-engine` for Flutter tests).
- Splitting at `600s` yields a **`fast` tier of 1,139 targets (`1,137` transitive tests `<= 600s` + `2` pinned `skia_build_test*` gates, ~10–13 min per uncached leg)** and a **`slow` tier of 12 tests (>600s, run separately on demand)**. Clean `baseline=G3_HEAD` legs are cached automatically in `~/.cache/google3-test-paragraph/` keyed by `(citc_base_cl, g3_skia_revision, targets_sha256, flaky_attempts)` (max 6 entries), so repeated reviews at the same `google3` base run only the candidate leg (~10–13 min total).
- **Auto-Slow-Tier Policy**: By default, use `--tier fast` during intermediate/WIP active development. However, when making the final check before giving `CR+1` on a CL (or before `amnesia-precommit` upload), you MUST append `--tier all` to cover the slow tier tests.

## Contents
- [Workspace & Data Layout](#workspace-data-layout)
- [Automated Scripts](#automated-scripts)
- [How to Run](#how-to-run)
- [Refreshing Cached Target Lists](#refreshing-cached-target-lists)
- [Inspecting Results & Run Attestation](#inspecting-results-run-attestation)
- [Model Routing (Cost & Context Hygiene)](#model-routing-cost-context-hygiene)
- [Mandatory Reporting Rule](#mandatory-reporting-rule)
- [Common Pitfalls & Gotchas](#common-pitfalls-gotchas)

---

## Workspace & Data Layout

- **CitC Client (Linux Cloudtop, or corp macOS via SSH)**: `/google/src/cloud/jlavrova/skia_g3_ab/google3` (auto-falls back to `skia_g3_pilot`, or creates `skia_g3_ab` when `--create-citc` is passed; when invoked on macOS without a local `/google/src/cloud` mount, [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py) forwards execution over multiplexed SSH `ControlPersist=8h` to `<user>-for-jetski.c.googlers.com` and pulls `compare.md` + per-leg JSON summaries back into `--out`)
- **Mirrored Skia Root in `google3`**: `<citc>/google3/third_party/skia/HEAD` (pinned revision read from `third_party/skia/HEAD/METADATA`)
- **Local Skia Git Checkout**: `~/.gemini/jetski/scratch/skia` (or `--skia-repo <path>`)
- **Cached Target Lists**:
  - [`fast_targets.txt`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/data/fast_targets.txt) (`1,139` runnable targets: `1,137` transitive `<= 600s` + `2` pinned `skia_build_test*` gates)
  - [`slow_targets.txt`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/data/slow_targets.txt) (`12` runnable tests `> 600s`)
  - [`targets_manifest.json`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/data/targets_manifest.json) (generation timestamp, base Piper CL, `g3_skia_revision`, seeds, counts)

---

## Automated Scripts

- Orchestrator: [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py)
- Autoroller include & `BUILD.bazel` -> `BUILD` rewriter: [`autoroller_rewrites.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/autoroller_rewrites.py)
- BEP JSONL & `blaze.log` comparator: [`parse_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/parse_g3_paragraph_tests.py)
- Depserver BFS target list refresher: [`refresh_g3_targets.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/refresh_g3_targets.py)

---

## How to Run

**CitC Hygiene Invariant**: `--out` **must** point outside `/google/src/cloud/` (for example `/tmp/g3_paragraph_tests`). [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py) hard-aborts if `--out` resolves inside CitC, stages candidate edits directly in `<citc>/google3/third_party/skia/HEAD/...` without creating a Piper CL, and reverts the CitC tree in `finally`.

### 1. Fast-tier A/B against local Skia working tree / branch (default: `fast`, ~20–25 min total)
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py \
  --out /tmp/g3_paragraph_tests \
  --skia-repo ~/.gemini/jetski/scratch/skia \
  baseline=G3_HEAD \
  current=WORKTREE
```

### 2. Fast-tier A/B against a Gerrit CL (`cl:<change>` or `cl:<change>/<ps>`) or commit
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py \
  --out /tmp/g3_paragraph_tests_cl \
  baseline=G3_HEAD \
  cand=cl:1409697/5
```

### 3. Run the 12 slow tests (`--tier slow`) or combined 1,151 corpus (`--tier all`)
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py \
  --out /tmp/g3_paragraph_tests_slow \
  --tier slow \
  baseline=G3_HEAD \
  current=WORKTREE
```

### 4. Re-compare existing BEP files or run directories without re-running Blaze
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/parse_g3_paragraph_tests.py compare \
  --targets-file ~/.gemini/config/skills/google3-test-paragraph/data/fast_targets.txt \
  --out /tmp/g3_paragraph_tests/compare.md \
  baseline=/tmp/g3_paragraph_tests/baseline \
  current=/tmp/g3_paragraph_tests/current
```

### Useful Flags (`run_g3_paragraph_tests.py`)
- `--tier <fast|slow|all>` — Target tier to run (default: `fast` = 1,139 targets; `slow` = 12 targets `>600s`; `all` = 1,151 targets).
- `--targets-file <path>` — Run a custom newline-delimited list of `google3` test labels instead of a cached tier.
- `--citc-client <name>` — CitC client name under `/google/src/cloud/<user>/` (default: auto-detects `skia_g3_ab`, then `skia_g3_pilot`).
- `--create-citc` — Create `skia_g3_ab` via `g4 citc` if no reusable CitC client exists.
- `--wait-lock-sec <sec>` — Seconds to wait for the exclusive per-CitC-client `flock` (`~/.cache/google3-test-paragraph/locks/citc_<client>.lock`, default: `0` = fail fast if another run holds the client).
- `--skia-repo <path>` — Local Skia git repository used for `WORKTREE` or commit refs (default: `~/.gemini/jetski/scratch/skia`, then Flutter submodule Skia).
- `--base-ref <git_ref>` — Override base commit when computing `WORKTREE` or branch commit range.
- `--flaky-attempts <N>` — Pass `--flaky_test_attempts=<N>` to `blaze test` (default: `1`; unlike `Canary-G3`'s `SUBMIT_DRY_RUN` mode in `autoroller_lib.py:1433`, local `blaze test` does not deflake unless `--flaky-attempts` is set, so `compare.md` reports flips in both directions).
- `--no-baseline-cache` — Force re-running `baseline=G3_HEAD` instead of reusing a cached baseline leg (`compare.md` marks reused legs with `` `cached` ``).
- `--baseline-cache-dir <path>` — Override baseline cache directory outside CitC (default: `~/.cache/google3-test-paragraph`, max 6 entries).
- `--resume` — Skip legs whose `attestation.json` and `summary.json` already exist in `--out`.
- `--allow-stale-targets` — Proceed even if `targets_manifest.json` is older than `--max-targets-age-days` (default `30`), recording a warning in `attestation.json`.
- `--allow-dirty-citc` — Proceed even if the CitC client already had open/modified files before staging (flagged `WARNING / INVALID` in `compare.md`).
- `--ssh-host <host>` — Remote Linux Cloudtop host used when `/google/src/cloud` is not mounted locally (default: `$GOOGLE3_PARAGRAPH_SSH_HOST`, `~/.config/google3-test-paragraph/ssh_host`, or `<user>-for-jetski.c.googlers.com`; supports `G3_HEAD`, `cl:<change>/<ps>`, `WORKTREE`, and local commits).
- `--no-ssh` — Disable automatic SSH forwarding when `/google/src/cloud` is not mounted locally.

---

## Refreshing Cached Target Lists

Target lists drift slowly as `google3` teams add or rename tests. Refresh every ~30 days (or when `run_g3_paragraph_tests.py` reports `targets_manifest.json` stale):

```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/refresh_g3_targets.py \
  --citc-client skia_g3_ab
```

Pass `--bep <path/to/full_bep.json> --slow-threshold-sec 600` after a `--tier all` run to re-partition newly slow (`>600s`) targets into `slow_targets.txt`.

---

## Inspecting Results & Run Attestation

`<out>/compare.md` (also printed to stdout) contains:
1. **Primary Outcome Table**: `Passed`, `Failed`, `Flaky`, `Timeout`, `Build/Analysis Fail`, `Skipped`, `Regressions`, and `Flips to Pass`.
2. **Паспорт прогона (Run Attestation)** and **Вердикт целостности (Integrity Check)**: verifies BEP `buildFinished` completion, CitC client & base CL consistency, `g3_skia_revision`, distinct `source_tree_sha256` between baseline and candidate, rewritten/deleted file counts, Blaze executed action & `CppCompile` counts, Sponge link (`http://sponge2/<uuid>`), and clean CitC restoration (`citc_restored_clean`).
3. **Per-Candidate Breakdowns** (flips reported in **both** directions):
   - `Regressions (PASS/FLAKY on baseline -> FAIL here)`
   - `Newly Passing / Baseline Flake Flips (FAIL on baseline -> PASS/FLAKY here)`
   - `Flaky Status Shifts (PASSED <-> FLAKY)`
   - `Pre-existing Failures (failing on both baseline and candidate)`

---

## Model Routing (Cost & Context Hygiene)

- **Direct runs in Plan Mode**: Do not execute [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py) on the planning model. Call `exit_plan_mode` with the exact command line, a ban on unrequested `--allow-dirty-citc` / `--allow-stale-targets` bypass flags, and instructions to paste `<out>/compare.md` verbatim without column/row truncation (`Re-plan if:` pre-flight/staging fails, Integrity Check is `WARNING / INVALID`, or regressions appear).
- **In-workflow runs & failure triage (`amnesia-precommit` / `review-triage`)**: Delegate script execution and `blaze.log` / Sponge failure extraction to `invoke_subagent(TypeName="self", Model="flash")` so multi-megabyte Blaze logs and BEP files stay out of the main context window, then read `<out>/compare.md` directly.

---

## Mandatory Reporting Rule

Whenever presenting `google3` corpus results to the user, **always** include verbatim:
1. The primary test outcome table.
2. The complete **Паспорт прогона (Run Attestation)** section including the **Вердикт целостности (Integrity Check)** banner and all table columns without truncation.

If Integrity Check is `WARNING / INVALID`, never report the run as clean.

---

## Common Pitfalls & Gotchas

1. **Blaze Analysis OOM Without `-Xmx32G`**: Default 10 GB JVM heap OOMs while analyzing 1,100+ `google3` targets across C++, Java, iOS, and WASM rules. Always keep `--host_jvm_args=-Xmx32G` and `--config=minimize_memory` (built into [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py)).
2. **Never Apply Raw `git diff | patch -p1` to `third_party/skia/HEAD`**: Files in `third_party/skia/HEAD` have `#include "..."` rewritten to `#include "third_party/skia/HEAD/..."`, `<hb.h>` rewritten to `"third_party/harfbuzz/src/hb.h"`, and `BUILD.bazel` rewritten into `BUILD` (`chrome/skia/autoroller/autoroller_lib.py`). Raw `patch -p1` fails near `#include` context or trips `layering_check`; [`run_g3_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py) stages onto a real git worktree at `g3_skia_revision` and runs [`autoroller_rewrites.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/google3-test-paragraph/scripts/autoroller_rewrites.py).
3. **GN vs. Bazel Filegroup Drift (`BUILD.bazel`)**: Adding or deleting `.cpp`/`.h` files in `modules/skparagraph/skparagraph.gni` or `modules/skunicode/skunicode.gni` without updating `modules/*/src/BUILD.bazel` or `modules/*/include/BUILD.bazel` compiles in `dm` and Flutter but breaks `google3`. `check_bazel_filegroup_sync` fails fast before invoking Blaze.
4. **Stack-Base Skew (`g4 sync` vs Candidate Rebase)**: When a candidate CL/branch sits on an older Skia base behind `g3_skia_revision` (for example across an upstream `modules/skparagraph/src/` struct refactor) or depends on unrolled `main` commits ahead of `g3_skia_revision`, `stage_candidate_in_worktree` emits a `[STACK-BASE SKEW WARNING]` and `compare.md` marks Integrity Check `WARNING / INVALID` (also flagging any candidate `FAILED_TO_BUILD` / `ANALYSIS_FAILURE` drop vs baseline). Rebase stale candidate stacks onto `main` / `g3_skia_revision`, or run `(cd /google/src/cloud/jlavrova/skia_g3_ab/google3 && g4 sync)` when CitC lags `google3` HEAD.
