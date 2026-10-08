---
name: flutter-test-paragraph-engine
description: >-
  Build the Flutter engine with custom Skia or SkParagraph modifications and run the canonical ~2100 test suite covering text layout, rendering, selection, editable text, and input fields.
  Trigger this skill when the user asks to "протестируй параграф во Флаттере", "протестируй параграф", "прогони тесты параграфа", "test paragraph in Flutter", or when verifying custom SkParagraph/Skia changes against the Flutter framework.
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

## Repository Paths & Structure

* **Flutter Framework**: `/Users/jlavrova/Sources/flutter`
* **Flutter Engine Source**: `/Users/jlavrova/Sources/flutter/engine/src`
* **Skia Submodule**: `/Users/jlavrova/Sources/flutter/engine/src/flutter/third_party/skia`
* **Engine Build Output**: `/Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64`

---

## Automated Scripts

- Orchestrator: [`run_flutter_paragraph_tests.py`](file:///Users/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py)
- Log parser & comparator: [`parse_flutter_paragraph_tests.py`](file:///Users/jlavrova/.gemini/config/skills/flutter-test-paragraph-engine/scripts/parse_flutter_paragraph_tests.py)

---

## How to Run

### 1. Compare current Skia submodule changes against baseline
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=origin/main \
  current=WORKTREE
```

### 2. Compare two branches or commits in the Skia submodule
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests \
  baseline=origin/main \
  fix=my_skia_branch
```

### 3. Run on current working tree only
```bash
python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py \
  --out /tmp/flutter_paragraph_tests
```

### Useful Flags
- `--batches <all|1,2,3>` — Choose specific test batches (default: `all`).
- `--skip-build` — Skip running ninja if the engine is already built.
- `--skip-cpp` — Skip C++ smoke tests (`txt_unittests`, `ui_unittests`).
- `--resume` — Skip re-running refs whose `attestation.json` and `summary.json` already exist.

---

## Inspecting Results & Run Attestation

After the run finishes:
1. View `<out>/compare.md` (also printed to stdout) for:
   - Primary test outcome table across C++ smoke tests and Batches 1–3.
   - **Паспорт прогона (Run Attestation)** and **Вердикт целостности (Integrity Check)**: verifies git SHA, Skia source tree SHA-256 (`source_tree_sha256`), rebuilt `flutter_tester` binary SHA-256 (`binary_sha256`) and mtime, ninja build action counter, executed batches, and test completeness (`finished_cleanly`).
   - **Regressions vs baseline** and **Fixed vs baseline**.
2. Individual logs and metadata are stored in `<out>/<label>/`:
   - `attestation.json` — objective run metadata, SHA-256 hashes, ninja actions, exact test commands, exit codes, and signals.
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

#### Batch 3: Input Fields, Boundaries & Deltas (~1098 tests)
```bash
./bin/flutter test \
  --local-engine=host_debug_unopt_arm64 \
  --local-engine-host=host_debug_unopt_arm64 \
  --local-engine-src-path=/Users/jlavrova/Sources/flutter/engine/src \
  packages/flutter/test/material/text_field_test.dart \
  packages/flutter/test/cupertino/text_field_test.dart \
  packages/flutter/test/services/text_boundary_test.dart \
  packages/flutter/test/services/text_editing_delta_test.dart
```

---

## Common Pitfalls & Gotchas

1. **Flag Omission**: Always pass all three engine flags (`--local-engine`, `--local-engine-host`, and `--local-engine-src-path`). If `--local-engine-host` is omitted on macOS arm64, `flutter` might fail to resolve host artifacts.
2. **Missing `sky_engine`**: Building only `flutter_tester` without `sky_engine` leads to compilation failures in framework tests when `dart:ui` declarations change or cache is invalidated.
3. **`selectable_text_test.dart` Location**: Note that `selectable_text_test.dart` is under `packages/flutter/test/material/`, not `widgets/`.
