---
name: paragraph-bug-prioritization
description: >-
  Impact-driven bug discovery and prioritization pipeline for WebParagraph and SkParagraph using
  nonlinear user-pain scoring (`score_nl = S_nl[S_release] * R * P`), cached google3 + GitHub
  reach profiles, and compound-reach zoom scans. Trigger when asked to "приоритизируй баги",
  "отранжируй баги параграфа", "пересчитай приоритеты WebParagraph", "приоритизируй SkParagraph",
  or "run paragraph bug prioritization".
---

# Impact-Driven Paragraph Bug Prioritization (`paragraph-bug-prioritization`)

Prioritize `WebParagraph` and `SkParagraph` bugs by real production user pain rather than test-suite noise or debug-only assertion counts:

$$\text{score}_{\text{nl}} = S_{\text{nl}}(S_{\text{release}}) \times R \times P(\text{trigger})$$

where:
- **$S_{\text{release}} \in \{0..5\}$** is the **release-mode** user-visible severity mapped to nonlinear weights $S_{\text{nl}} = \{0: 0,\; 1: 1,\; 2: 2,\; 3: 4,\; 4: 10,\; 5: 30\}$ (`S5` release crash/throw = 30; `S4` dropped/extra lines or broken overflow contract = 10; `S3` wrong caret/selection/hit-test/box order = 4; `S2` visible metric/alignment shift = 2; `S1` sub-pixel/cosmetic = 1; `S0` debug-only assert or non-UI `toString`/`==` = 0).
- **$R \in [0, 1]$** is the weighted ecosystem reach `(2 * R_g3 + R_gh) / 3` across live production apps (`~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_web.json` for `WebParagraph`, `~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_skp.json` for `SkParagraph`), refined via Phase 3 `scan_reach.py zoom` whenever a bug requires a compound condition.
- **$P \in \{1.0,\; 0.3,\; 0.05,\; 0.01\}$** is the conditional trigger probability within apps that use the feature (`1.0` = every call site, `0.3` = common layout/input pattern, `0.05` = narrow/compound edge case, `0.01` = rare corner case).

---

## Core Invariants

1. **Cached Ecosystem Reach First (Minutes, Not Hours)**:
   - Never re-run full 30-feature `google3` + GitHub scans from scratch when re-ranking bugs after landing PRs. Start directly from `~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_web.json` (32 live `google3` web apps + 25 live GitHub web apps) or `~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_skp.json` (33 live `google3` Flutter apps including mobile-only apps such as `gpay` + 49 live GitHub Flutter apps).
   - Run `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py refresh-cached` only when incorporating fresh `1b/` or `1c/` snapshots.
2. **Backend-Specific Phase 2 Oracles (`WebParagraph` $\neq$ `SkParagraph`)**:
   - **`WebParagraph` mode (`~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_wp_diff.py`)**: Uses `CanvasKit SkParagraph` as a live same-API reference oracle inside `felt` (`chrome-dart2js-webparagraph-ui` vs. `chrome-dart2js-canvaskit-canvaskit`), comparing exceptions, domain invariants, line counts, line ranges, `LineMetrics`, glyph/placeholder boxes, and hit-testing.
   - **`SkParagraph` mode (`~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_skp_invariants.py`)**: Never uses `WebParagraph` as an oracle for `SkParagraph`. Instead uses (a) oracle-free metamorphic and algebraic invariants (`check-invariants`: line range tiling, character box non-inversion, `maxIntrinsicWidth` no-wrap, `minIntrinsicWidth` no-overflow, zero crashes), (b) C++ source marker extraction (`scan-todos`), and (c) non-web-only `flutter/flutter` issue triage (`fetch-issues`).
3. **Release-Mode Severity Gate (`FELT_NO_ASSERTS=1` / Release Builds)**:
   - Never assign `S5` (`S_nl = 30`) from a debug-only `assert()`. Verify whether the input still throws (`StateError`, `ArgumentError`, `RangeError`) or corrupts layout (`S4`/`S3`/`S2`) when assertions are disabled (`discover_wp_diff.py run-felt --release`). `score_and_rank.py` flags any `S5` candidate without `release_verified: true` as `[UNVERIFIED_RELEASE_SEVERITY]`.
4. **Phase 3 Compound Reach Zoom-In Gate (`[NEEDS_ZOOM_SCAN]`)**:
   - Single-feature reach (`F01`–`F30` in `~/.gemini/config/skills/paragraph-bug-prioritization/data/features.json`) overestimates compound bug reach by $5\times$–$15\times$ when a bug requires a rare literal or asset configuration inside a ubiquitous widget (e.g., `\u200B`-only text inside `TextField`, or variable font `.ttf` with `fvar` table + `fontWeight` without `FontVariation`).
   - `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/score_and_rank.py` automatically marks any top-10 candidate with coarse `R >= 0.50` and `P <= 0.05` (without a measured `zoom_reach`) as `[NEEDS_ZOOM_SCAN]`. Before freezing or unfreezing roadmap items, run `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py zoom` for every flagged top-10 candidate and record `zoom_reach` and `zoom_P`.
5. **`cs` Completeness & `CTRL` Silent-Failure Gate**:
   - Every `google3` `cs` scan in `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py` runs with at most 4 threads, checks for the explicit `"These are all the results"` or `"No results found"` marker, includes the `CTRL` probe (`^(import|part|library|export) `), and retries sequentially up to 3 rounds if any query is incomplete or `CTRL == 0` on an app root with $\ge 50$ Dart files.

---

## Directory Layout

- `~/.gemini/config/skills/paragraph-bug-prioritization/data/features.json`: Canonical 30-feature taxonomy (`F01`–`F30`) with Dart (`regex`), `pubspec.yaml` (`pubspec_regex`), and C++ `SkParagraph` (`cpp_regex`) patterns.
- `~/.gemini/config/skills/paragraph-bug-prioritization/data/g3_apps.json`: Metadata, liveness weights, tiers (`tier1 = 3`, `normal = 2`, `internal = 1`), locales, and `web_in_reach` / `skp_in_reach` flags for `google3` Flutter roots.
- `~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_web.json`: Precomputed `WebParagraph` reach vector (`R`, `R_g3_with_locales`, `R_gh`).
- `~/.gemini/config/skills/paragraph-bug-prioritization/data/reach_skp.json`: Precomputed `SkParagraph` reach vector (`R`, `R_g3_with_locales`, `R_gh`).
- `~/.gemini/config/skills/paragraph-bug-prioritization/data/wp_candidates.json`: Curated `WebParagraph` bug candidates (`N01`–`N23` + roadmap items) with reproducers and Phase 3 `zoom_reach` measurements.
- `~/.gemini/config/skills/paragraph-bug-prioritization/resources/zz_harness.dart`: Shared Dart differential test harness injected automatically by `discover_wp_diff.py run-felt`.
- `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py`: CLI for refreshing cached reach profiles (`refresh-cached`) and running Phase 3 compound-reach micro-scans (`zoom`).
- `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_wp_diff.py`: CLI for running the `WebParagraph` vs. `CanvasKit SkParagraph` `felt` differential harness (`run-felt`) and grouping hierarchical diffs (`diff-results`).
- `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_skp_invariants.py`: CLI for oracle-free `SkParagraph` invariant checks (`check-invariants`), C++ source marker scanning (`scan-todos`), and `flutter/flutter` issue mapping (`fetch-issues`).
- `~/.gemini/config/skills/paragraph-bug-prioritization/scripts/score_and_rank.py`: Deterministic nonlinear `S x R x P` scorer with `[NEEDS_ZOOM_SCAN]` and `[UNVERIFIED_RELEASE_SEVERITY]` gates.

---

## Workflow

### Step 1: Select or Refresh Ecosystem Reach Profile

For standard prioritization runs, use the cached reach profile directly (`--profile web` or `--profile skp`). If the underlying `1b/` or `1c/` ecosystem scan in `~/.gemini/jetski/scratch/wp_prioritization` was updated, refresh the cached profiles:

```bash
python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py refresh-cached
```

---

### Step 2: Run Backend-Specific Bug Discovery (Phase 2)

#### Mode A: `WebParagraph` Differential & Release-Severity Discovery

1. Run `felt` on the case corpus in both debug and release (`--release` sets `FELT_NO_ASSERTS=1`) inside a linked Flutter engine worktree:
   ```bash
   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_wp_diff.py run-felt \
     --worktree <flutter_worktree> \
     --cases <cases_json> \
     --out-results <debug_results_json>

   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_wp_diff.py run-felt \
     --worktree <flutter_worktree> \
     --cases <cases_json> \
     --out-results <release_results_json> \
     --release
   ```
2. Group divergences by root-cause signature:
   ```bash
   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_wp_diff.py diff-results \
     --cases <cases_json> \
     --results <debug_results_json> \
     --out-groups <groups_json>
   ```

#### Mode B: `SkParagraph` Invariant & Issue Discovery

1. Check oracle-free domain invariants (`inv_line_tiling`, `inv_lineB_contains`, `inv_wordB_contains`, `inv_box_outside_x`, `inv_box_outside_y`, `inv_wrap_at_maxIW`, `inv_overflow_at_minIW`, `inv_inverted_box`) on `SkParagraph` layout outputs:
   ```bash
   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_skp_invariants.py check-invariants \
     --cases <cases_json> \
     --results <results_json> \
     --side skp
   ```
2. Scan `SkParagraph` C++ sources for marker comments (`scan-todos`) and/or fetch open non-web-only `flutter/flutter` typography issues mapped to `F01`–`F30`:
   ```bash
   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_skp_invariants.py scan-todos \
     --skparagraph-dir <skparagraph_dir> \
     --out <out_todos_json>

   python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/discover_skp_invariants.py fetch-issues \
     --label "a: typography" \
     --out <out_gh_issues_json>
   ```

---

### Step 3: Score Candidates & Check Gates (`score_and_rank.py`)

Run the nonlinear scorer on the candidate file:

```bash
python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/score_and_rank.py \
  --profile web \
  --candidates ~/.gemini/config/skills/paragraph-bug-prioritization/data/wp_candidates.json \
  --out-json <out_ranking_json> \
  --out-md <out_ranking_md>
```

Inspect the `Gates` column in the generated Markdown table:
- If any candidate has `[UNVERIFIED_RELEASE_SEVERITY]`, verify its behavior in release mode (`FELT_NO_ASSERTS=1` for `WebParagraph`, or release `dm` / `txt_unittests` for `SkParagraph`) and update `severity_release` and `"release_verified": true`.
- If any top-10 candidate has `[NEEDS_ZOOM_SCAN]`, proceed to Step 4 to measure its exact compound reach before finalizing the ranking.

---

### Step 4: Phase 3 Compound Reach Zoom-In (`scan_reach.py zoom`)

When a top-10 candidate is flagged with `[NEEDS_ZOOM_SCAN]` (or depends on a specific combination of API calls, character literals, or font tables), run a targeted `google3` zoom scan across all live in-reach apps (with automatic `CTRL` verification and optional OpenType `fvar` table inspection):

```bash
# Example 1: Measure reach of U+200B (ZWSP) literals in Dart sources
python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py zoom \
  --profile web \
  --query 'ZWSP=(\\u200[bB]|\x{200B}|\\u\{200[bB]\}|0x200[bB])' \
  --out <out_zoom_zwsp_json>

# Example 2: Measure reach of variable fonts (fvar table) combined with fontWeight
python3 ~/.gemini/config/skills/paragraph-bug-prioritization/scripts/scan_reach.py zoom \
  --profile web \
  --check-ttf-fvar \
  --query 'FW=(fontWeight:|FontWeight\.w|FontWeight\.bold)' \
  --query 'FV=(FontVariation\(|fontVariations:)' \
  --out <out_zoom_vf_json>
```

Inspect `sample_hits` in the output JSON to separate strict affected call sites from benign hits, record `"zoom_reach"`, `"zoom_P"`, and `"zoom_basis"` in the candidate entry, and re-run `score_and_rank.py`.

---

### Step 5: Sync Top Priorities with `roadmap.py`

Once the top candidates pass all gates (`OK` in the `Gates` column), update the project roadmap via `python3 ~/.gemini/config/hooks/roadmap.py`:
- Group the top unfixed bugs under a parent Epic item with sub-items ordered by `score_nl` descending.
- Mark lower-priority items outside the top cutoff as `[FROZEN]` so active engineering bandwidth stays focused on the highest-impact user pain.
