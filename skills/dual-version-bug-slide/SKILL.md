---
name: dual-version-bug-slide
description: >-
  Transform a reported bug (GitHub issue, Buganizer ID, or regression report) into a contract
  unit test and an interactive dual-version comparison slide ("how it was" vs "how it is / should be").
  Trigger when the user asks to "создай слайд по багу", "проверь ошибку на двух версиях",
  "сделай слайд сравнения", "сделай визуализацию до/после", or "слайд".
---

# Dual-Version Bug Slide Curator (`dual-version-bug-slide`)

Transform bug reports in `SkParagraph`, `SkUnicode`, `SkShaper`, `WebParagraph`, or `Skia` into verifiable regression tests and interactive before/after comparison slides that visually contrast legacy baseline behavior with candidate or target implementations.

---

## 1. Core Invariants & Anti-Slop Contract

1. **Strict English-Only:**
   - All generated code, slide titles, subtitles, card headers, descriptive prose, assertion messages, and UI text must be written strictly in English.
   - No Cyrillic or localized text in compiled source files or binaries.

2. **Code Comment Discipline (Integration with `code-comments`):**
   - **Why over What:** Explain timeless architectural invariants, hardware/API quirks, or why a check is omitted; never restate syntax.
   - **Length & Punctuation:** Keep comments to 1–4 lines. Periods (`.`) are allowed between sentences within a multi-line comment block, but **strictly forbidden at the very end** of a single-line comment or at the end of the last line of a comment block (except `.dart` files).
   - **Zero Noise:** No numbered tutorial steps (`// 1. Setup`), markdown formatting (`**bold**`), or ASCII dividers (`// ===`).
   - **Ownership:** Never rewrite or delete pre-existing or user-written comments without explicit permission.

3. **Fail-Fast Dimensional Honesty:**
   - Never hardcode bounding boxes, caret offsets, or advance widths with magic numbers (`x + 120.0f`).
   - Dimensions must be queried dynamically via runtime API calls (`paragraph->getRectsForRange`, `Run::positions()`, `Run::advance()`, `getBoxesForRange`).

4. **Roadmap Honesty (No Faking):**
   - If candidate behavior is not yet implemented in the codebase, the status is **`UNRESOLVED / KNOWN DEFECT (ROADMAP)`**.
   - The right card must feature an amber banner: `[NOT IMPLEMENTED IN CURRENT BUILD (ROADMAP)]`.
   - The unit test must be guarded with `DEF_TEST_DISABLED` (or `skip: true`) and explain the exact failing contract.
   - If manual simulation is necessary (e.g. inserting `\n` to model strict line breaks), mark it explicitly: `[MANUALLY SIMULATED FOR ROADMAP]`.

5. **Crash Isolation:**
   - Legacy code that triggers `SkASSERT`, `abort()`, or unhandled exceptions must **never** be executed inside a live UI draw loop.
   - Such cases use the **Crash Anatomy** rendering mode (static structural diagram with the exact asserted condition and stack).

6. **The Invariant Triplet:**
   - Every comparison slide must end with three mandatory lines in the bottom comparison card:
     1. `Legacy Version (Baseline):` exact structural cause of failure.
     2. `Candidate Version (New Engine):` architectural mechanism resolving the issue.
     3. `Architectural Invariant:` fundamental property preserved by the contract.

7. **Public Artifact & Visual Proof Hygiene:**
   - Rendered slide titles, subtitles, cards, code comments, and catalog artifacts must be safe to attach to public Gerrit CLs, GitHub PRs, and external issue trackers.
   - Never embed internal corporate shortlinks (`go/`, `goto.google.com`, `cl/`, `cr/`, `sponge`, `critique`), non-public `b/<id>` bug numbers, internal depot/CitC paths, or machine-local home paths in visible slide text or metadata. Always use public issue identifiers (`FLUTTER ISSUE <id>`, `crbug.com/<id>`, `skia:<id>`) or short commit SHAs.

---

## 2. Phased Workflow

### Phase 1: Universal Defect Analysis

Decompose the reported bug along three orthogonal axes:
1. **Input Vector:**
   - Text code points, combining characters, BiDi control marks (`ALM`, `RLM`), ZWJ sequences, astral plane surrogates.
   - Styling parameters: font sizes, families, layout constraints (tight width), `baselineShift`, multi-style span splits.
2. **Contract Invariant:**
   - Domain rule governing expected behavior (e.g. cluster monotonicity, fail-fast dimension bounds, non-negative advances, surrogate pair preservation).
3. **Failure Mode:**
   - *Fatal Crash:* `SkASSERT`, `abort()`, `SIGSEGV`, runtime exception.
   - *Topological Inversion:* out-of-order cluster indices, broken grapheme clusters.
   - *Metric/Geometric Drift:* wrong advance width, baseline drop, selection overlap.
   - *Semantic Loss:* tofu, wrong fallback font family, dropped diacritics.

---

### Phase 2: Gate A — Hostile Test & Mutation Probe

#### Test File Routing Protocol
Identify target test file before writing any code:
1. Search codebase for existing test references (`git grep -i "<issue_id>"`).
2. Route by repository and subsystem:
   - Skia Paragraph (`SkParagraph`) $\to$ `modules/skparagraph/tests/SkParagraphTest.cpp`
   - Skia Unicode (`SkUnicode`) $\to$ `modules/skunicode/tests/SkUnicodeTest.cpp`
   - Flutter `WebParagraph` (Dart) $\to$ `engine/src/flutter/lib/web_ui/test/webparagraph/`
   - TypeScript `WebParagraph` (`Rusino/paragraph`) $\to$ `tests/webparagraph/`
3. **State the chosen file and rationale in the turn response before editing.**

#### Loop Guard Limits
- **Maximum Test Formulation Attempts: 3.**
  - Attempt 1: Direct translation of repro steps into unit test.
  - Attempt 2: Refined constraints (layout width, explicit font, BiDi direction).
  - Attempt 3: Minimal synthetic isolation of the shaper/layout call.
- **Maximum Mutation Probes: 2.**
  - Probe 1 (Assertion Inversion): replace expected assertion with an impossible value (`pos == 999` or inverted boolean). The test **must fail**.
  - Probe 2 (Constraint Perturbation): alter layout bounds (width = 1.0f or empty text). The test **must fail**.

#### Stop-the-Line Rule
If after 3 attempts the test unexpectedly passes on legacy code, or fails to fail when assertions are inverted:
1. **STOP IMMEDIATELY.** Do not proceed to slide authoring.
2. Escalate to the user with a diagnostic summary:
   - Exact code and constraints attempted.
   - Hypotheses for non-reproducibility (e.g. dependency on specific OS font version, missing external font asset, or bug residing in upper framework layer).

---

### Phase 3: State & Render Mode Classification

Classify the test case into one of three execution states:

| State | Status | Legacy Card Mode | Candidate Card Mode | Unit Test |
|---|---|---|---|---|
| **1. Soft Defect Fixed** | `FIXED` (Green) | `LIVE_EXECUTION` (Baseline Commit/Backend) | `LIVE_EXECUTION` (Candidate Commit/Backend) | Passing test |
| **2. Crash Defect Fixed** | `FIXED` (Green) | `CRASH_ISOLATED` (Static Crash Anatomy) | `LIVE_EXECUTION` (Candidate Commit/Backend) | Passing test |
| **3. Unresolved Roadmap** | `UNRESOLVED` (Amber) | `LIVE_EXECUTION` or `CRASH_ISOLATED` | `TARGET_MOCK` + Warning Banner | Disabled test (`DEF_TEST_DISABLED` / `skip: true`) |

---

### Phase 4: Slide Authoring & Headless Browser Verification

1. **Generate Standalone Comparison Slide (`<artifact_dir>/<issue>_slide.html` + PNG)**:
   - Render the two-column comparison card (`Legacy (Baseline)` on the left, `Candidate (Fixed)` on the right) and the bottom **Invariant Triplet** card (`Legacy Version (Baseline):`, `Candidate Version:`, `Architectural Invariant:`) using real measured geometry/metrics extracted from the baseline and candidate runs.
2. **Visual Verification**:
   - Navigate with `browser_navigate` (`ServerName="browser_browser"`), check `browser_get_console_logs`, and capture a screenshot via `browser_capture_screenshot` into `<artifact_dir>/<issue>_slide.png`.
   - Confirm zero tofu, zero text clipping, and clear visual contrast of the defect vs. fix.
