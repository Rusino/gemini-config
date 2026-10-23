---
name: webparagraph-sync
description: Synchronize upstream Dart WebParagraph changes (bugfixes, layout improvements, and tests from flutter/engine web_ui) into the standalone TypeScript Paragraph port (Rusino/paragraph). Trigger when the user asks to "синхронизируй вебпараграф", "подтяни изменения из web_paragraph", "перенеси фикс из вебпараграфа", "обнови TS параграф из флаттера", or "sync webparagraph to TS".
---

# WebParagraph Upstream -> TypeScript Sync (`webparagraph-sync`)

End-to-end agent workflow for porting upstream Dart `WebParagraph` commits, PRs, or branch diffs into the standalone TypeScript library (`Rusino/paragraph`) without requiring the user to inspect diffs manually.

---

## 1. Repositories & Watermark State

- **Upstream (Flutter Engine `web_ui`)**:
  - Root: `/usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter`
  - Source: `engine/src/flutter/lib/web_ui/lib/src/engine/web_paragraph/`
  - Tests: `engine/src/flutter/lib/web_ui/test/webparagraph/`
- **Downstream (TypeScript Port `Rusino/paragraph`)**:
  - Root: `/usr/local/google/home/jlavrova/.gemini/jetski/scratch/webparagraph-ts`
  - Source: `src/*.ts`
  - Tests: `tests/webparagraph/*.test.ts` (13 suites ported 1:1 from `test/webparagraph/*_test.dart`), `tests/paragraph_fixtures.test.ts`, `tests/types.test.ts`
- **Last Synced Upstream Commit (Watermark SHA)**:
  - `6c1ebba9f074a16e2a789e34dc0abf86cb0193a9` (`[web_paragraph] Apply StrutStyle to line metrics and placeholders`, includes `#193734` and `webparagraph_text_height_behavior`)

> Always update **Last Synced Upstream Commit** in both `.agents/skills/webparagraph-sync/SKILL.md` (in `Rusino/paragraph`) and `~/.gemini/config/skills/webparagraph-sync/SKILL.md` after completing a sync.

---

## 2. Dart -> TypeScript File & Symbol Map

All `Web*` class prefixes in Dart (`WebParagraph`, `WebTextLayout`, `WebCluster`, etc.) are stripped in TypeScript (`Paragraph`, `TextLayout`, `Cluster`).

| Upstream Dart File (`web_paragraph/`) | Downstream TS File (`src/`) | Mapped Classes & Responsibilities |
|---|---|---|
| `paragraph.dart` | `src/paragraph.ts` | `Paragraph`, `ParagraphBuilder` |
| `paragraph.dart` | `src/paragraph_style.ts` | `ParagraphStyle`, `StrutStyle`, `TextStyle` |
| `paragraph.dart` | `src/styles.ts` | `StyleElements`, `ResolvedTextStyle`, `ResolvedStrutStyle`, `StyleNode`, `RootStyleNode`, `ChildStyleNode` |
| `paragraph.dart` | `src/spans.ts` | `ParagraphSpan`, `TextSpan`, `PlaceholderSpan` |
| `paragraph.dart` | `src/clusters.ts` | `ClusterRange`, `TextClusterMapping`, `Cluster`, `TextCluster`, `EmptyCluster`, `PlaceholderCluster` |
| `layout.dart` | `src/layout.ts` | `LayoutBidiRun`, `TextLayout` (`extractTextClusters`, `addLine`, `formatLines`, `getBoxesForRange`, `getPositionForOffset`, `getGlyphInfoAt`, `getWordBoundary`, `getLineBoundary`) |
| `layout.dart` | `src/lines.ts` | `LineBlock`, `TextBlock`, `PlaceholderBlock`, `EllipsisBlock`, `TextLine` |
| `layout.dart` + `font_collection.dart` | `src/measurer.ts` | `MeasureProvider`, `OracleFontMeasureProvider`, `CanvasMeasureProvider` |
| `wrapper.dart` | `src/wrapper.ts` | `TextWrapper`, `LineBuilder` (4-zone cluster state machine, greedy breaking, backward `ellipsize`) |
| `decorations.dart` | `src/decorations.ts` | `CanvasDecorationPainter` (`calculateThickness`, `fillDecorations`, wavy/double/dashed/solid lines) |
| `painter.dart` | `src/painter.ts` | `ParagraphPainter`, `ParagraphVisitor`, `LayoutVisitorPainter`, `LayoutTreeInspector`, `CanvasParagraphPainter` |
| `bidi.dart` + `code_unit_flags.dart` | `src/providers.ts` | `BidiProvider`, `CodeUnitClassifier`, `ClusterProvider`, `CodeUnitFlags`, browser/WASM default implementations |
| `dart:ui` primitives | `src/types.ts` | `Rect`, `Offset`, `Size`, `TextBox`, `TextRange`, `TextPosition`, `GlyphInfo`, `LineMetrics`, enums |

---

## 3. Intentional Divergences & TS-First Fixes (DO NOT REVERT)

When porting upstream Dart changes or re-dumping golden fixtures, **never revert** the following intentional architectural decouplings or TS-first bugfixes (implemented in `a7a3272` and `9999eb5`):

### Architectural Decouplings
1. **Decoupled Painting (`src/painter.ts`)**: `Paragraph.paint(painter: ParagraphPainter, offset: Offset)` takes a `ParagraphPainter` (not a `ui.Canvas`). Traversal logic lives in `LayoutVisitorPainter`, while actual 2D canvas drawing lives in `CanvasParagraphPainter`.
2. **Pluggable Providers (`src/providers.ts`, `src/measurer.ts`)**: ICU/SkUnicode WASM and `CanvasTextMetrics` are abstracted behind `BidiProvider`, `CodeUnitClassifier`, `ClusterProvider`, and `MeasureProvider`.

### Known TS-First Bugfixes (Fixed in TS before Dart)
If an upstream Dart commit fixes one of these, **skip** the redundant code change (keep the TS implementation), port any new test cases, and remove the item from this list:
1. **`TextDecoration.none` bitmask check (`src/decorations.ts`)**: `hasDecoration` checks `mask !== 0` so `TextDecoration.none` (`0`) does not falsely match every decoration mask.
2. **`fontFamilyFallback` inheritance (`src/styles.ts`)**: `ChildStyleNode.resolve()` inherits `fontFamilyFallback` from parent style nodes when not overridden.
3. **Ellipsis state reset on re-layout (`src/lines.ts` / `src/layout.ts`)**: Re-running `layout()` with a new width cleanly resets any prior `ellipsis` block/state from previous layout passes.
4. **Multi-span trailing whitespace accumulation (`src/wrapper.ts`)**: `LineBuilder` accumulates `trailing spaces` continuously across span/block boundaries instead of resetting trailing whitespace width at span transitions.

---

## 4. Automated Sync Procedure

When asked to sync upstream changes (either all changes since the watermark SHA, or a specific commit/PR/branch):

1. **Extract Upstream Diff Automatically**:
   - Run `git -C /usr/local/google/home/jlavrova/.gemini/jetski/scratch/flutter log <watermark_sha>..<target_ref> -p -- engine/src/flutter/lib/web_ui/lib/src/engine/web_paragraph engine/src/flutter/lib/web_ui/test/webparagraph`
   - Do **not** ask the user to read or triage the diff. Summarize which commits are being synced.

2. **Check Working Copy Hygiene**:
   - Run `git -C /usr/local/google/home/jlavrova/.gemini/jetski/scratch/webparagraph-ts status`. If another active chat has uncommitted edits, stop and report before modifying files.

3. **Port Tests First (Gate A)**:
   - Translate new or updated test cases from `engine/src/flutter/lib/web_ui/test/webparagraph/*_test.dart` into the corresponding `tests/*.test.ts` file.
   - Run `npm test` in `/usr/local/google/home/jlavrova/.gemini/jetski/scratch/webparagraph-ts` and confirm the new test fails against unfixed TS code (unless it matches a Known TS-First Bugfix above).

4. **Port Implementation Hunks**:
   - Map each modified Dart class/method to its TS counterpart using the table in Section 2.
   - Apply the change in `src/*.ts`, respecting `<= 100` column width and `code-comments` rules (concise timeless "why" comments, no trailing periods on single-line or block-end comments).

5. **Golden Fixtures Check (if applicable)**:
   - If `fixture_dumper_test.dart` was updated or if layout geometry intentionally changed for existing fixtures, run `python3 scripts/dump_fixtures.py` and inspect `git diff fixtures/` to verify that none of the 4 Known TS-First Bugfixes are regressed.

6. **Verify & Advance Watermark**:
   - Run `npm run build && npm test`.
   - Run `python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo . src/*.ts`.
   - Update **Last Synced Upstream Commit (Watermark SHA)** in Section 1 of this `SKILL.md` to `<target_ref>`.
