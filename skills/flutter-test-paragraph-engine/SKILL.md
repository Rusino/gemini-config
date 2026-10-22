---
name: flutter-test-paragraph-engine
description: >-
  Build the Flutter engine with custom Skia or SkParagraph modifications and run the canonical ~2400 test suite covering text layout, rendering, selection, editable text, input fields, and dropdown menus (or all ~925 framework test files via --full sharded mode, using standard ICU by default or opt-in full Apple Unicode replacement via --unicode apple / @apple).
  Trigger this skill when the user asks to "протестируй параграф во Флаттере", "протестируй параграф", "прогони тесты параграфа", "проверь во Флаттере с подменой на Apple Unicode", "протестируй во Флаттере с Аппл юникодом", "test paragraph in Flutter", or when verifying custom SkParagraph/SkUnicode/Skia changes against the Flutter framework.
---

# Testing Custom Skia / SkParagraph in Flutter Engine

This skill provides the automated and end-to-end workflow for compiling custom Skia/SkParagraph changes within the local Flutter engine and running all relevant Flutter framework text tests with a tamper-evident **Паспорт прогона (Run Attestation)** and automated **Integrity Check (Вердикт целостности)**.

## Contents
- [Repository Paths & Structure](#repository-paths--structure)
- [Automated Scripts](#automated-scripts)
- [How to Run](#how-to-run)
- [Inspecting Results & Run Attestation](#inspecting-results--run-attestation)
- [Mandatory Reporting Rule](#mandatory-reporting-rule)
- [Manual Step-by-Step Workflow](#manual-step-by-step-workflow)
- [Common Pitfalls & Gotchas](#common-pitfalls--gotchas)

---

## Repository Paths & Structure (Auto-Detected on macOS & Jetski Cloud)

* **Flutter Framework**: `/Users/jlavrova/Sources/flutter` (macOS) or `~/.gemini/jetski/scratch/flutter` (Jetski Cloud)
* **Flutter Engine Source**: `<repo>/engine/src`
* **Skia Submodule**: `<repo>/engine/src/flutter/third_party/skia`
* **Engine Build Output**: `<repo>/engine/src/out/host_debug_unopt_arm64` (macOS) or `<repo>/engine/src/out/host_debug_unopt` (Linux / Jetski Cloud)

---

## Automated Scripts

- Orchestrator: [`run_flutter_paragraph_tests.py`](file:///Users/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py)
- Log parser & comparator: [`parse_flutter_paragraph_tests.py`](file:///Users/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/parse_flutter_paragraph_tests.py)

---

## How to Run

By default, `--unicode` is `icu` (standard Flutter ICU backend). Apple Unicode replacement (`skia_use_apple_unicode = true`, `skia_use_icu = false`) is **opt-in** via `--unicode apple` or the per-target `@apple` suffix, and `args.gn` is automatically restored on exit.

### 1. Compare current Skia submodule changes against baseline (default ICU)
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=origin/main \
  current=WORKTREE
```

### 2. Run or compare with opt-in Apple Unicode replacement (`@apple` / `--unicode apple`)
Compare baseline on standard ICU against a branch from `/Users/jlavrova/Sources/skia` (`ext:<ref>`) with full Apple Unicode replacement:
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=origin/main@icu \
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
  baseline=origin/main \
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

## Manual Step-by-Step Workflow

If running steps manually without the orchestrator:

### 1. Building the Local Engine

```bash
# Working directory: /Users/jlavrova/Sources/flutter/engine/src
ninja -C out/host_debug_unopt_arm64 flutter_tester sky_engine txt_unittests ui_unittests
```

### 2. Engine Smoke Tests (C++)

```bash
# Working directory: /Users/jlavrova/Sources/flutter/engine/src
./out/host_debug_unopt_arm64/txt_unittests
./out/host_debug_unopt_arm64/ui_unittests
```

### 3. Verifying Build Artifacts

Ensure the `.o` files and `flutter_tester` timestamps are newer than modified source files:
```bash
ls -la /Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64/flutter_tester
```

### 4. Running the Flutter Text Suite in 3 Batches

From `/Users/jlavrova/Sources/flutter`:

#### Batch 1: Text Layout & Rendering (~210 tests)
```bash
./bin/flutter test \
  --local-engine=host_debug_unopt_arm64 \
  --local-engine-host=host_debug_unopt_arm64 \
  --local-engine-src-path=/Users/jlavrova/Sources/flutter/engine/src \
  packages/flutter/test/rendering/paragraph_test.dart \
  packages/flutter/test/rendering/paragraph_intrinsics_test.dart \
  packages/flutter/test/painting/text_painter_test.dart \
  packages/flutter/test/painting/text_painter_rtl_test.dart \
  packages/flutter/test/painting/text_span_test.dart \
  packages/flutter/test/painting/text_style_test.dart \
  packages/flutter/test/widgets/rich_text_test.dart \
  packages/flutter/test/widgets/text_test.dart
```

#### Batch 2: Editing & Selection (~792 tests)
```bash
./bin/flutter test \
  --local-engine=host_debug_unopt_arm64 \
  --local-engine-host=host_debug_unopt_arm64 \
  --local-engine-src-path=/Users/jlavrova/Sources/flutter/engine/src \
  packages/flutter/test/widgets/editable_text_test.dart \
  packages/flutter/test/widgets/editable_text_styles_test.dart \
  packages/flutter/test/material/selectable_text_test.dart \
  packages/flutter/test/widgets/text_selection_test.dart
```

#### Batch 3: Input Fields, Dropdown Menus, Boundaries & Deltas (~1294 tests)
```bash
./bin/flutter test \
  --local-engine=host_debug_unopt_arm64 \
  --local-engine-host=host_debug_unopt_arm64 \
  --local-engine-src-path=/Users/jlavrova/Sources/flutter/engine/src \
  packages/flutter/test/material/text_field_test.dart \
  packages/flutter/test/cupertino/text_field_test.dart \
  packages/flutter/test/material/dropdown_menu_test.dart \
  packages/flutter/test/services/text_boundary_test.dart \
  packages/flutter/test/services/text_editing_delta_test.dart
```

---

## Common Pitfalls & Gotchas

1. **Flag Omission**: Always pass all three engine flags (`--local-engine`, `--local-engine-host`, and `--local-engine-src-path`). If `--local-engine-host` is omitted on macOS arm64, `flutter` might fail to resolve host artifacts.
2. **Missing `sky_engine`**: Building only `flutter_tester` without `sky_engine` leads to compilation failures in framework tests when `dart:ui` declarations change or cache is invalidated.
3. **`selectable_text_test.dart` Location**: Note that `selectable_text_test.dart` is under `packages/flutter/test/material/`, not `widgets/`.
