---
name: dual-version-bug-slide
description: >-
  Transform a reported bug (GitHub issue, Buganizer ID, or regression report) into
  a rigorous contract unit test and an interactive dual-engine comparison slide ("how
  it was" vs "how it is / how it should be"). Enforces hostile mutation testing,
  loop guards, English-only UI/code, and strict code comment invariants. Trigger
  when the user provides a bug link/ID and asks to reproduce, test, or visualize it
  across two versions ("создай слайд по багу", "проверь ошибку на двух версиях",
  "сделай слайд сравнения", "сделай визуализацию до/после").
---

# Dual-Version Bug Slide Curator (`dual-version-bug-slide`)

Transform bug reports into verifiable regression tests and interactive dual-engine comparison slides that visually contrast legacy baseline behavior with candidate or target implementations.

---

## 1. Core Invariants & Anti-Slop Contract

1. **Strict English-Only:**
   - All generated C++ code, slide titles, subtitles, card headers, descriptive prose, assertion messages, and UI text must be written strictly in English.
   - No Cyrillic or localized text in compiled source files or binaries.

2. **Code Comment Discipline (Integration with `code-comments`):**
   - **Why over What:** Explain timeless architectural invariants, hardware/API quirks, or why a check is omitted; never restate C++ syntax.
   - **Length & Punctuation:** Keep comments to 1–4 lines. Periods (`.`) are allowed between sentences within a multi-line comment block, but **strictly forbidden at the very end** of a single-line comment or at the end of the last line of a comment block.
   - **Zero Noise:** No numbered tutorial steps (`// 1. Setup`), markdown formatting (`**bold**`), or ASCII dividers (`// ===`).
   - **Ownership:** Never rewrite or delete pre-existing or user-written comments without explicit permission.

3. **Fail-Fast Dimensional Honesty:**
   - Never hardcode bounding boxes, caret offsets, or advance widths with magic numbers (`x + 120.0f`).
   - Dimensions must be queried dynamically via runtime API calls (`paragraph->getRectsForRange`, `Run::positions()`, `Run::advance()`).

4. **Roadmap Honesty (No Faking):**
   - If candidate behavior is not yet implemented in the codebase, the status is **`UNRESOLVED / KNOWN DEFECT (ROADMAP)`**.
   - The right card must feature an amber banner: `[NOT IMPLEMENTED IN CURRENT BUILD (ROADMAP)]`.
   - The unit test must be guarded with `DEF_TEST_DISABLED` and explain the exact failing contract.
   - If manual simulation is necessary (e.g. inserting `\n` to model strict line breaks), mark it explicitly: `[MANUALLY SIMULATED FOR ROADMAP]`.

5. **Crash Isolation:**
   - Legacy code that triggers `SkASSERT`, `abort()`, or unhandled exceptions must **never** be executed inside the GUI `viewer` draw loop.
   - Such cases use the **Crash Anatomy** rendering mode (static structural diagram with the exact asserted condition and stack).

6. **The Invariant Triplet:**
   - Every comparison slide must end with three mandatory lines in the bottom comparison card:
     1. `Legacy Version (Baseline):` exact structural cause of failure.
     2. `Candidate Version (New Engine):` architectural mechanism resolving the issue.
     3. `Architectural Invariant:` fundamental property preserved by the contract.

---

## 2. Phased Workflow

```
Input: Bug Link / ID + Dual-Version Context (Backends / Commits)
                           │
           [Phase 1: Universal Defect Analysis]
           - Input Vector (text, styles, constraints, locale)
           - Contract Invariant (monotonicity, 2D anchors, UTF-16)
           - Failure Mode (crash, topological, metric, semantic)
                           │
           [Phase 2: Gate A — Hostile Test & Mutation Probe]
           - Route to target test file
           - Loop Guard: max 3 test attempts, max 2 mutation probes
           - Stop-the-Line: if test is tautological -> STOP, escalate
                           │
           [Phase 3: State & Render Mode Classification]
           - FIXED + LIVE_EXECUTION (soft metric mismatch resolved)
           - FIXED + CRASH_ISOLATED (fatal crash resolved; legacy isolated)
           - UNRESOLVED (ROADMAP) (unimplemented; DEF_TEST_DISABLED)
                           │
           [Phase 4: Slide Authoring (English + code-comments)]
           - Subclass ParagraphBugSlide_Base
           - Left card: Legacy (live or crash anatomy)
           - Right card: Candidate (live or roadmap banner)
           - Bottom card: The Invariant Triplet
                           │
           [Phase 5: Build, Headless Render & Catalog Integration]
           - Register in ParagraphBugsSlide.cpp (DEF_SLIDE)
           - Update Slide 0 Dashboard with verified balance (X Passing | Y Roadmap)
           - Headless PNG render (1050x750)
           - Update Markdown carousel and slides_gallery.html
```

---

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
1. Search codebase for existing test references: `git grep -i "<issue_id>" modules/skparagraph/tests/`.
2. If none, route by subsystem:
   - Paragraph layout/wrapping $\to$ `modules/skparagraph/tests/ParagraphTest.cpp`.
   - Fonts/Typefaces/Descriptors $\to$ `modules/skparagraph/tests/TypefaceTest.cpp` or `FontCollectionTest.cpp`.
   - Unicode/Word boundaries $\to$ `modules/skparagraph/tests/SkUnicodeTest.cpp`.
3. Canonical regression fallback: dedicated bug suite (e.g. `modules/skparagraph/tests/SkParagraphFlutterBugsTest.cpp` or `SkParagraphSkiaBugsTest.cpp`).
4. **State the chosen file and rationale in the turn response before editing.**

#### Loop Guard Limits
- **Maximum Test Formulation Attempts: 3.**
  - Attempt 1: Direct translation of repro steps into C++ test.
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
| **1. Soft Defect Fixed** | `FIXED` (Green) | `LIVE_EXECUTION` (makeHarfBuzzParagraph) | `LIVE_EXECUTION` (makeCoreTextParagraph) | `DEF_TEST` |
| **2. Crash Defect Fixed** | `FIXED` (Green) | `CRASH_ISOLATED` (Static Crash Anatomy) | `LIVE_EXECUTION` (makeCoreTextParagraph) | `DEF_TEST` |
| **3. Unresolved Roadmap** | `UNRESOLVED` (Amber) | `LIVE_EXECUTION` or `CRASH_ISOLATED` | `TARGET_MOCK` + Warning Banner | `DEF_TEST_DISABLED` |

---

### Phase 4: Slide Authoring Pattern

Author the slide in `modules/skparagraph/slides/ParagraphBugsSlide.cpp` inheriting from `ParagraphBugSlide_Base`:

```cpp
// Verifies that keycap emoji sequences form an atomic cluster in CoreText
class ParagraphSlide_Flutter_121680_KeycapEmoji : public ParagraphBugSlide_Base {
public:
    void draw(SkCanvas* canvas) override {
        drawComparisonSlide(
            canvas,
            "FLUTTER ISSUE 121680",
            "Keycap Emoji Sequence Grapheme Clustering",
            "FIXED IN CORETEXT",
            SkColorSetRGB(22, 163, 74),
            "CoreText shapes 3-codepoint keycap sequences as an atomic Apple Color Emoji glyph",
            "Legacy (HarfBuzz / ICU)",
            "Splits keycap into decomposed clusters",
            [this](const SkRect& r) { drawLegacy(r); },
            "Candidate (CoreText / Apple)",
            "Atomic single cluster representation",
            [this](const SkRect& r) { drawCandidate(r); },
            "HarfBuzz splits [1] + [VS-16] + [Keycap] across separate runs, trapping caret inside",
            "CoreText unifies sequence into 1 glyph, advancing caret across entire cluster",
            "Grapheme Cluster Invariant: User-perceived characters must behave atomically"
        );
    }
};
```

---

### Phase 5: Build, Headless Render & Catalog Integration

1. **Register Slide:**
   - Append `DEF_SLIDE(return new ParagraphSlide_<Name>();)` to `ParagraphBugsSlide.cpp`.
   - Add entry to `bugs[]` array in `ParagraphSlide_BugsOverview`.
   - Update header string to match exact arithmetic count: `"13 TESTS: X PASSING | Y ROADMAP"`.
2. **Build and Render:**
   ```bash
   ninja -C out/Debug viewer && \
   ./<render_tool> <output_dir>
   ```
3. **Visual Verification:**
   - Confirm zero tofu (missing glyph boxes).
   - Verify all text stays within card boundaries with no clipping.
   - Verify warning banners on any roadmap slides.
4. **Catalog Synchronization:**
   - Update Markdown carousel artifact (`DualEngine_Typography_Bugs_Carousel.md`).
   - Update interactive gallery (`slides_gallery.html`).
