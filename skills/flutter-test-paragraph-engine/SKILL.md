---
name: flutter-test-paragraph-engine
description: >-
  Build the Flutter engine with custom Skia or SkParagraph modifications and run the canonical ~2100 test suite covering text layout, rendering, selection, editable text, and input fields.
  Trigger this skill when the user asks to "протестируй параграф во Флаттере", "протестируй параграф", "прогони тесты параграфа", "test paragraph in Flutter", or when verifying custom SkParagraph/Skia changes against the Flutter framework.
---

# Testing Custom Skia / SkParagraph in Flutter Engine

This skill provides the end-to-end workflow for compiling custom Skia/SkParagraph changes within the local Flutter engine and running all relevant Flutter framework text tests.

## Contents
- [Repository Paths & Structure](#repository-paths--structure)
- [Workflow Checklist](#workflow-checklist)
- [1. Building the Local Engine](#1-building-the-local-engine)
- [2. Engine Smoke Tests (C++)](#2-engine-smoke-tests-c)
- [3. Verifying the Build Artifacts](#3-verifying-the-build-artifacts)
- [4. Running the Canonical Flutter Text Suite](#4-running-the-canonical-flutter-text-suite)
- [Common Pitfalls & Gotchas](#common-pitfalls--gotchas)

---

## Repository Paths & Structure

* **Flutter Framework**: `/Users/jlavrova/Sources/flutter`
* **Flutter Engine Source**: `/Users/jlavrova/Sources/flutter/engine/src`
* **Skia Submodule**: `/Users/jlavrova/Sources/flutter/engine/src/flutter/third_party/skia`
* **Engine Build Output**: `/Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64`

---

## Workflow Checklist

- [ ] **Step 1**: Checkout or apply Skia changes in `flutter/third_party/skia`.
- [ ] **Step 2**: Build `flutter_tester`, `sky_engine`, and C++ test binaries with ninja.
- [ ] **Step 3**: Run C++ smoke tests (`txt_unittests`, `ui_unittests`).
- [ ] **Step 4**: Verify that `flutter_tester` was rebuilt after the Skia commit and contains the new code.
- [ ] **Step 5**: Run the Flutter framework Dart text tests in 3 batches (~2100 tests total).
- [ ] **Step 6**: Report results and verify absence of regressions.

---

## 1. Building the Local Engine

When modifying `skparagraph` or Skia, build `flutter_tester` together with `sky_engine` and unit tests:

```bash
# Working directory: /Users/jlavrova/Sources/flutter/engine/src
ninja -C out/host_debug_unopt_arm64 flutter_tester sky_engine txt_unittests ui_unittests
```

> **Why `sky_engine` is required:**
> `flutter test` relies on Dart bindings (`dart:ui`) from `sky_engine`. If `sky_engine` is omitted from the build, `flutter test` might run with mismatched SDK artifacts or fail during kernel snapshot generation.

---

## 2. Engine Smoke Tests (C++)

Before running Dart tests, run engine unittests to quickly catch low-level assertion failures or regressions:

```bash
# Working directory: /Users/jlavrova/Sources/flutter/engine/src
./out/host_debug_unopt_arm64/txt_unittests
./out/host_debug_unopt_arm64/ui_unittests
```

---

## 3. Verifying the Build Artifacts

To guarantee that the tests run against the newly built code (and not an old cached binary):

1. **Check Timestamps:**
   ```bash
   ls -la /Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64/flutter_tester
   ls -la /Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64/obj/flutter/third_party/skia/modules/skparagraph/src/skparagraph.*.o
   ```
   Ensure the `.o` files and `flutter_tester` timestamps are newer than the modified source files.

2. **Verify Process Invocation:**
   Run one quick test with `-v` to ensure `flutter_tester` is executed from the local engine directory:
   ```bash
   # Working directory: /Users/jlavrova/Sources/flutter
   ./bin/flutter test \
     --local-engine=host_debug_unopt_arm64 \
     --local-engine-host=host_debug_unopt_arm64 \
     --local-engine-src-path=/Users/jlavrova/Sources/flutter/engine/src \
     -v packages/flutter/test/services/text_boundary_test.dart 2>&1 | grep "Starting flutter_tester process"
   ```
   Expected output contains:
   `Starting flutter_tester process with command=[/Users/jlavrova/Sources/flutter/engine/src/out/host_debug_unopt_arm64/flutter_tester, ...]`

---

## 4. Running the Canonical Flutter Text Suite

Running the full Flutter framework test suite (30,000+ tests) is impractical for iterative SkParagraph work. Instead, run the targeted **2100-test text suite** divided into three logical batches.

All commands run from `/Users/jlavrova/Sources/flutter`.

### Batch 1: Text Layout & Rendering (~210 tests)
Verifies low-level paragraph layout, intrinsics, text painter, spans, and rich text:
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

### Batch 2: Editing & Selection (~792 tests)
Verifies text selection, caret geometry, selection handles, and editable text:
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

### Batch 3: Input Fields, Boundaries & Deltas (~1098 tests)
Verifies Cupertino & Material text fields, delta text editing, and text boundaries:
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
