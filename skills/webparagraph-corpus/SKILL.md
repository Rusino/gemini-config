---
name: webparagraph-corpus
description: >-
  Run the targeted two-part Flutter test corpus for WebParagraph changes (Part 1:
  `engine/src/flutter/lib/web_ui` `test/webparagraph/*` and `test/ui/*` text tests via
  `felt`; Part 2: `packages/flutter` paragraph and `TextPainter` tests on Chrome with
  `preferWebParagraph: true`). Use when the user asks to test a branch, PR, or current
  changes against the Flutter test corpus ("на всем корпусе Флаттеровских тестов",
  "прогнать корпус тестов", "проверить на корпусе") or compare multiple branches/PRs
  against a baseline.
---

# WebParagraph Targeted Flutter Corpus Testing

This skill runs the targeted two-part Flutter paragraph/text test corpus for `WebParagraph` (~90 seconds per branch/ref) instead of the 2.5-hour 24-suite `felt test` matrix, and automatically generates a comparative Markdown report (`compare.md`).

## What the Corpus Includes

1. **Part 1 — Engine `web_ui` (`engine/src/flutter/lib/web_ui`, ~55–60s per ref)**:
   - `dart analyze --fatal-infos` (run before any temporary patches)
   - Injects the temporary `forceTestFonts` (`FlutterTest` / `Ahem`) patch (unless `--no-force-test-fonts` is set) so tests with `TestEnvironment(forceTestFonts: true)` use test fonts
   - All `test/webparagraph/*_test.dart` under suite `chrome-dart2js-webparagraph-ui`
   - Shared `dart:ui` text tests under temporary suite `chrome-dart2js-webparagraph-ui-text` (`run-config: chrome-webparagraph`):
     - `test/ui/paragraph_builder_test.dart`
     - `test/ui/paragraph_style_test.dart`
     - `test/ui/text_test.dart`
     - `test/ui/text_style_test.dart`
     - (or all 60 `test/ui/*_test.dart` files with `--full`)
2. **Part 2 — Framework (`packages/flutter`, ~30s per ref)**:
   - Incremental `ninja -C engine/src/out/wasm_release flutter/web_sdk` (~15s)
   - `flutter test --local-web-sdk=wasm_release --platform=chrome --reporter=expanded` (~15s) with `preferWebParagraph: true`, `--enable-experimental-web-platform-features`, and (by default) the `forceTestFonts` (`FlutterTest` / `Ahem`) patch on:
     - `test/rendering/paragraph_intrinsics_test.dart`
     - `test/rendering/paragraph_test.dart`
     - `test/painting/text_painter_test.dart`
     - (or all 916 web unit test files in `packages/flutter/test` sharded in parallel with `--full`)
   - Automatically verifies `[CanvasKit served]: webparagraph/canvaskit.js` in the test log and restores all temporary changes (`felt_config.yaml`, `flutter_web_platform.dart`, `bin/cache/flutter_tools.stamp`, and `flutter/web_sdk`) in a `finally` block.

## Scripts

- Orchestrator: [`run_webparagraph_corpus.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py)
- Log parser & comparator: [`parse_corpus_logs.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/webparagraph-corpus/scripts/parse_corpus_logs.py)

## How to Run

> [!IMPORTANT]
> Because `felt test` and `ninja -C engine/src/out/wasm_release flutter/web_sdk` share the repository build directories, always run refs **sequentially** in a single invocation of [`run_webparagraph_corpus.py`](file:///usr/local/google/home/jlavrova/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py). Never launch parallel instances against the same checkout.

### 1. Compare current working tree changes against `origin/master` (or another baseline)
```bash
python3 ~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py \
  --out /tmp/webparagraph_corpus \
  baseline=origin/master \
  current=WORKTREE
```
*(If the working tree has uncommitted changes in `engine/src/flutter/lib/web_ui`, the script saves them before switching to `baseline`, re-applies them for `WORKTREE`, and restores them on exit.)*

### 2. Compare one or more branches / PRs against a baseline commit
```bash
python3 ~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py \
  --out /tmp/webparagraph_corpus \
  baseline=origin/master \
  pr1=my_branch_1 \
  pr2=my_branch_2
```

### 3. Run on the current working tree only (without baseline comparison)
```bash
python3 ~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py \
  --out /tmp/webparagraph_corpus
```

### Useful Flags
- `--full` — Run the full corpus (all 60 `test/ui/*` files in Part 1 and all 916 `packages/flutter/test` web test files sharded in Part 2).
- `--part all | web-ui | framework` — Run both parts (default `all`), only Part 1 (`web-ui`), or only Part 2 (`framework`).
- `--no-force-test-fonts` — Do not auto-inject `ui_web.TestEnvironment.instance.forceTestFonts` support into `WebParagraph` during Part 1 and Part 2.
- `--skip-analyze` — Skip `dart analyze --fatal-infos` in Part 1.
- `--repo <path>` — Path to the Flutter checkout (defaults to `/usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter`).

## Inspecting Results & Run Attestation

After the run finishes:
1. Read `<out>/compare.md` (also printed to stdout at the end of the run) for:
   - The main summary table
   - **Паспорт прогона (Run Attestation)** and **Вердикт целостности (Integrity Check)**: verifies git SHA, `web_ui` source tree SHA-256 (`web_ui_tree_sha256`), compiled `ddc_outline.dill` SHA-256 (`part2_ddc_outline_sha256`), corpus mode (`FULL` vs `TARGETED`), executed vs expected test files/suites/shards, `WebParagraph` active markers (`CanvasKit (Web Paragraph)` and `webparagraph/canvaskit.js`), and zero compile errors or unfinished suites/shards.
   - **Regressions vs baseline** and **Fixed vs baseline**.
2. **Mandatory Reporting Rule**: Whenever reporting corpus results to the user, always include both the main summary table and the **Паспорт прогона (Run Attestation)** table + **Вердикт целостности** verbatim from `<out>/compare.md`. Never report a run as clean if the Integrity Check is `WARNING / INVALID`.
3. Individual logs and attestation metadata for each `<label>` are stored in `<out>/<label>/`:
   - `attestation.json` — git commit/dirty state, `web_ui` source tree SHA-256, `ninja` action count, `ddc_outline.dill` SHA-256, and expected file counts
   - `analyze.log` — `dart analyze --fatal-infos` output
   - `felt.log`, `felt_summary.txt`, `felt_summary.json` — Part 1 (`web_ui`) raw log and parsed results
   - `flutter.log` (or `flutter_shards/`), `flutter_summary.txt`, `flutter_summary.json` — Part 2 (`packages/flutter`) raw log and parsed results
