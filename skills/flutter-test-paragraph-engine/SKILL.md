---
name: flutter-test-paragraph-engine
description: >-
  Build the Flutter engine with custom Skia/SkParagraph changes and run the canonical ~2400
  Flutter text/paragraph/editable/dropdown tests (`run_flutter_paragraph_tests.py`, or `--full`
  sharded mode). Trigger when asked to "протестируй параграф во Флаттере", "протестируй параграф",
  "прогони тесты параграфа", "test paragraph in Flutter", or before uploading SkParagraph/SkUnicode
  changes.
---

# Testing Custom Skia / SkParagraph in Flutter Engine

This skill provides the automated and end-to-end workflow for compiling custom Skia/SkParagraph changes within the local Flutter engine and running all relevant Flutter framework text tests with a tamper-evident **Паспорт прогона (Run Attestation)** and automated **Integrity Check (Вердикт целостности)**.

## Contents
- [Repository Paths & Structure](#repository-paths--structure)
- [Automated Scripts](#automated-scripts)
- [How to Run](#how-to-run)
- [Inspecting Results & Run Attestation](#inspecting-results--run-attestation)
- [Mandatory Reporting Rule](#mandatory-reporting-rule)
- [Common Pitfalls & Gotchas](#common-pitfalls--gotchas)

---

## Repository Paths & Structure (Auto-Detected on Linux Jetski Cloud & macOS)

* **Flutter Framework**: `~/.gemini/jetski/scratch/flutter` (Jetski Cloud) or `/Users/jlavrova/Sources/flutter` (macOS)
* **Flutter Engine Source**: `<repo>/engine/src`
* **Skia Submodule**: `<repo>/engine/src/flutter/third_party/skia`
* **Engine Build Output**: `<repo>/engine/src/out/host_debug_unopt` (Linux) or `<repo>/engine/src/out/host_debug_unopt_arm64` (macOS)

---

## Automated Scripts

- Orchestrator: [`run_flutter_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py)
- Log parser & comparator: [`parse_flutter_paragraph_tests.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/parse_flutter_paragraph_tests.py)

---

## How to Run

By default, `--unicode` is `icu` (standard Flutter ICU backend). Apple Unicode replacement (`skia_use_apple_unicode = true`, `skia_use_icu = false`) is **opt-in** via `--unicode apple` or the per-target `@apple` suffix, and `args.gn` is automatically restored on exit.

### 1. Compare current Skia submodule changes against baseline (default ICU)
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=HEAD \
  current=WORKTREE
```

### 2. Run or compare with opt-in Apple Unicode replacement (`@apple` / `--unicode apple`)
Compare baseline on standard ICU against a branch from `--external-skia` (`ext:<ref>`) with full Apple Unicode replacement:
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=HEAD@icu \
  apple_cand=ext:HEAD@apple
```
Or run a single target with `--unicode apple`:
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  --unicode apple \
  apple_cand=ext:HEAD
```

### 3. Run on current working tree only
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests
```

### 4. Run the full sharded Flutter framework corpus (`--full`)
Run all ~925 `packages/flutter/test/**/*_test.dart` files sharded across parallel workers via local `flutter_tester`:
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests_full \
  --full \
  baseline=HEAD \
  current=WORKTREE
```

### Useful Flags
- `--unicode <icu|apple|keep>` — Default `SkUnicode` backend when target does not specify `@icu` or `@apple` (default: `icu`; use `apple` only when explicitly testing Apple Unicode replacement, or `keep` to leave `args.gn` untouched).
- `ext:<ref>` target prefix — Fetches `<ref>` from `--external-skia` (`/Users/jlavrova/Sources/skia` or `~/.gemini/jetski/scratch/skia`) and checks out `modules/skunicode`, `modules/skparagraph`, `modules/skshaper` onto the Flutter Skia submodule.
- `--batches <all|1,2,3>` — Choose specific test batches (default: `all`).
- `--full` — Run all `packages/flutter/test/**/*_test.dart` files sharded across parallel workers via local `flutter_tester` instead of only Batches 1–3.
- `--flutter-shards <N>` — Number of parallel `flutter test` shard processes in `--full` mode (default: `8`).
- `--flutter-concurrency <N>` — Number of concurrent `flutter_tester` processes (`-j`) per shard in `--full` mode (default: `4`).
- `--skip-build` — Skip running ninja if the engine is already built.
- `--skip-cpp` — Skip C++ smoke tests (`txt_unittests`, `ui_unittests`).
- `--resume` — Skip re-running refs whose `attestation.json` and `summary.json` already exist.
- `--allow-skia-base-mismatch` — Run even though the Skia submodule `HEAD` differs from `skia_revision` in Flutter `DEPS` (recorded as `skia_base_matches_deps: false` in `attestation.json`); without it the script aborts before touching anything.
- `--allow-dirty-flutter` — Run even if the outer Flutter repository has uncommitted changes (recorded as `flutter_dirty_files` in `attestation.json` and flagged by the Integrity Check); without it the script aborts so leftover Flutter test/engine edits cannot pollute clean Skia runs.
- `--allow-dirty-skia` — Allow uncommitted changes in the Skia submodule even when no target uses `WORKTREE` (stashes and restores them via `worktree_skia.patch`).

---

## Inspecting Results & Run Attestation

After the run finishes:
1. View `<out>/compare.md` (also printed to stdout) for:
   - Primary test outcome table across C++ smoke tests and Batches 1–3.
   - **Паспорт прогона (Run Attestation)** and **Вердикт целостности (Integrity Check)**: verifies git SHA, active `SkUnicode` backend (`Apple (CFString/CoreText)` vs `ICU` via `args.gn` and `nm` binary symbol inspection on `txt_unittests`/`flutter_tester`), Skia source tree SHA-256 (`source_tree_sha256`), rebuilt `flutter_tester` binary SHA-256 (`binary_sha256`) and mtime, ninja build action counter, executed batches, and test completeness (`finished_cleanly`).
   - **Regressions vs baseline** and **Fixed vs baseline**.
2. Individual logs and metadata are stored in `<out>/<label>/`:
   - `attestation.json` — objective run metadata, active Unicode backend (`unicode_backend`, `sym_apple_make`, `sym_icu_make`), SHA-256 hashes, ninja actions, exact test commands, exit codes, and signals.
   - `summary.json` — parsed test counts per batch and failure lists.
   - `ninja.log` — raw compilation output.
   - `txt_unittests.log`, `ui_unittests.log` — C++ smoke test logs.
   - `batch1.log`, `batch2.log`, `batch3.log` — Dart framework test logs.

---

## Mandatory Reporting Rule

Whenever presenting test results to the user, the agent **must** include both:
1. The primary test outcome table.
2. The complete **Паспорт прогона (Run Attestation)** section including the **Вердикт целостности (Integrity Check)** banner and all table columns without truncation.

If the integrity check verdict is `WARNING / INVALID`, never report the run as clean; report the exact invariant violations prominently.

---

## Common Pitfalls & Gotchas

1. **Flag Omission**: Always pass all three engine flags (`--local-engine`, `--local-engine-host`, and `--local-engine-src-path`). If `--local-engine-host` is omitted on macOS arm64, `flutter` might fail to resolve host artifacts.
2. **Missing `sky_engine`**: Building only `flutter_tester` without `sky_engine` leads to compilation failures in framework tests when `dart:ui` declarations change or cache is invalidated.
3. **`selectable_text_test.dart` Location**: Note that `selectable_text_test.dart` is under `packages/flutter/test/material/`, not `widgets/`.
4. **Apple Unicode Engine Glue Is Off `master`**: On macOS the Flutter-side glue for `--unicode apple` / `@apple` lives on branch `apple-unicode-engine-glue` in the Flutter Framework repo, not on `master`. Apply it with `git show apple-unicode-engine-glue | git apply` before an Apple run and revert with `git show apple-unicode-engine-glue | git apply -R` afterwards; the script aborts before building if the glue is missing.
5. **Skia Submodule Must Match `DEPS` After Updating Flutter**: `baseline=HEAD`, `ext:<ref>` and `WORKTREE` all build on the current submodule `HEAD`, so after pulling Flutter run `gclient sync -D` from the Flutter Framework repo root (commit or back up any submodule work first). The script aborts when the submodule `HEAD` differs from `DEPS` `skia_revision` unless `--allow-skia-base-mismatch` is passed.
