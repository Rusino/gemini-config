---
name: amnesia-precommit
description: >-
  Token-efficient pre-commit audit and diff minimizer for Skia, SkParagraph, WebParagraph, and
  Flutter changes (comment/code checks, Hunter + keeper_skeptic refutation, finding_ledger,
  single-rebuild TDD, and corpus/perf gates). Trigger before committing/uploading or when asked to
  "подготовь к коммиту", "минимизируй дифф", "проверь перед пушем", "сделай амнезия-ревью", or
  "run amnesia precommit".
---

# Amnesia Pre-Commit & Diff Minimizer (`amnesia-precommit`)

Execute a strict pre-commit audit and diff minimization workflow before uploading a CL or pushing a PR. Prevents chat-history confirmation bias, strips drive-by changes, and filters out AI reviewer hallucinations using a two-stage **Hunt then Refute** clean-context pipeline and at most one rebuild cycle.

---

## Core Invariants

1. **Zero-Token First Pass**: Always run deterministic scripts (`audit_comments.py` and `ruthless_audit.py`, or `scripts/precommit_razor.py`) before invoking any subagent.
2. **Two-Stage "Hunt then Refute" Pipeline**: Stage A spawns 1 isolated Hunter (`keeper_inquisitor` or `self`) for `<= 5` files in one layer, or 2–3 parallel Hunter shards for `> 5` files / cross-layer diffs, with zero chat history. Stage B merges all `BLOCKING` defects (`B1...`) and `BLOCKING_SCOPE` / non-trivial removals (`S1...`) into **one batched `keeper_skeptic` subagent** to **REFUTE** them from real code, callers, and tests (`OPTIONAL_GOLFING` / `nit:` never blocks upload or forces a standalone rebuild).
3. **Hostile TDD Validation (`Trapsmith` Filter)**: Never alter production logic based solely on an LLM critique; fix a `CONFIRMED` bug **only** if a newly written reproducer unit test fails against the current code.
4. **Single Rebuild Budget**: Batch all confirmed diff-shrinking edits and validated bug fixes into one compilation and test pass (`dm`, `felt`, or `flutter test`); never enter multi-iteration review-compile loops.
5. **Persistent Finding Ledger (`finding_ledger.py`)**: Query `~/.gemini/config/scripts/finding_ledger.py` before Stage A (`ruthless_audit.py` output or `finding_ledger.py query --repo . --diff HEAD`) and pass prior refuted findings into the Hunter prompt; after Stage B, Phase 3 execution refutation, or user pushback, record every newly refuted finding (`--scope repo` or `--scope cl`).

---

## Workflow

### Phase 1: Deterministic Mechanical Razor (0 Subagents)

1. **Inspect Working Tree & Diff Stats**: Run `git status --short` and `git diff HEAD --stat`; record the initial changed line count (`added + deleted`) as the minimization baseline.
2. **Run Comment, Anti-Slop, Public CL Hygiene, & Repo Presubmit Audits** (enforced by `CODE-CHECK GUARD`; or via `scripts/precommit_razor.py`):
   ```bash
   python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo . --check-cl-hygiene
   python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py . --repo . --diff HEAD
   if [ -f PRESUBMIT.py ]; then SKIP_GCE_AUTH_FOR_GIT=1 luci-auth context -- git cl presubmit --force; fi
   ```
   `audit_comments.py` also blocks on added C++/Dart-test lines: `WHOLE_FILE_IFDEF`, `TEMPLATE_NO_WHY`, `FUNC_NO_CONTRACT` (headers), `TEST_NO_HEADER`; `--commit <sha>` audits a committed CL read-only.
3. **Apply Mechanical Fixes**:
   - Fix trailing periods, missing "why" comments on non-trivial 1-line changes, and line length violations (`<= 100` cols).
   - Strip leaked internal corporate markers (`go/`, `goto.google.com`, `cl/`, `cr/`, depot/CitC paths, machine-local home paths, `sponge`, `critique`), code-comment `b/<id>` references (replace with `crbug.com/` or `skia:`), and malformed Gerrit `Change-Id` lines (must be `I` + 40 lowercase hex chars).
   - Strip leftover debug prints, anonymous `TODO`s without `KEEPER-DEBT`, and uncalled helpers.
   - Format modified lines only: `git clang-format` (or `dart format` for Dart files).

4. **Stage 0 Parallel Context Bundle & Governing Doc <-> Code Drift (`R4`)**: After mechanical fixes settle, pre-stage the commit message, `finding_ledger.py` refutations, nearest governing docs + `Doc <-> Code Drift` candidates (`AGENTS.md`, `SKILL.md`, `README.md`, `CLIENT_GUIDE.md`, `ARCHITECT_GUIDE.md`, `g3doc/*.md`; skipped when `NO_DOC_SYNC=<reason>` is in the commit description), optional `transcript.jsonl` user constraints, unified `diff.patch`, and 1-indexed post-patch `Side B` files + companion `.h` headers in one pass so Stage A Hunter shards read them in a single parallel Turn 1 `read_file` batch:
   ```bash
   python3 ~/.gemini/config/scripts/stage_diff_context.py \
     --repo . --diff HEAD \
     --transcript <appDataDir>/brain/<conversation-id>/.system_generated/logs/transcript.jsonl \
     --out-dir <appDataDir>/brain/<conversation-id>/scratch/stage0
   ```

---

### Phase 2: The Amnesiac Shrinker & Inquisitor (Stage A Hunter Subagent(s))

Read [`references/hunter_prompt.md`](references/hunter_prompt.md) and spawn the Stage A `keeper_inquisitor` Hunter subagent(s) (`1` subagent for `<= 5` files in a single layer; `2–3` parallel shards for `> 5` files or cross-layer diffs per `recommended_topology` / `recommended_shards` from `stage_diff_context.py`, split `<= 5` files per shard or *Shard 1: Scope, Diff Golfing & Dropped Guards (`git log -S`)* vs *Shard 2: Pattern Completeness, Callers & Domain Invariants*) with the exact prompt, 7 evaluation axes (including the Fix Completeness axis — fallbacks, discriminating tests, cost model, caches, CI size/perf signals — and the 4 code-simplification checks — Zero-Base Necessity + Deletion Dominance, Structural Diff of feature/backend branches & consecutive loops, No Round-Trip Property Plumbing + above/below early-return audit, and cross-language Prototype-Scar Elimination — plus `veteran-review` lens mode for Skia C++/text/font diffs and the WebParagraph M1–M9 gate for `lib/web_ui/`), strict `S*` / `B*` / Grill output sections, mandatory `Checked Sites` enumeration before `None`, and mandatory `COMPACTION: YES` / `COMPACTION: NO` final footer. Use `scout` (`Model="flash"`) for `<= 3`-turn external `file:line` caller lookups. If **any** Stage A shard reports `COMPACTION: YES` (meaning `<CONTEXT_SUMMARY>` appeared in its context), discard that shard's output, re-shard its assigned files into smaller slices (`<= 2–3` files per shard), and re-run before Phase 2.5.

---

### Phase 2.5: The Skeptic Refutation Pass (`keeper_skeptic`, `Hunt then Refute` + `XFAMILY_REFUTE`)

Whenever Phase 2 (across all Hunter shards) produces any candidate defects in **Section 2 (`B1, B2...`)** OR any `BLOCKING_SCOPE` / non-trivial code removal in **Section 1 (`S1, S2...`)** (skip if Phase 2 returned `None` or only `OPTIONAL_GOLFING` `nit:` / trivial comment cleanup), spawn **one batched `keeper_skeptic` subagent** using the Phase 2.5 prompt in [`references/hunter_prompt.md`](references/hunter_prompt.md) and following the `XFAMILY_REFUTE` routing (`Model="flash"` default, `Model="inherit"` for concurrency/BiDi/metrics/dropped guards/cross-backend), Quota Exhaustion Fallback Ladder (`inherit` → `flash` → Phase 3 Trapsmith TDD → user escalation), Three Iron Verification Principles, and mandatory `COMPACTION: YES` / `COMPACTION: NO` footer in `plugins/keeper/agents/keeper_skeptic.md` (re-sharding into `<= 5`-candidate batches if `COMPACTION: YES` is reported).

---

### Phase 3: Pruning & The Trapsmith TDD Filter

1. **Apply Confirmed Scope & Size Reductions (`BLOCKING_SCOPE` vs `OPTIONAL_GOLFING`)**:
   - Apply confirmed `BLOCKING_SCOPE` reductions whose safety was **not** refuted by `keeper_skeptic`; drop any reduction marked `REFUTED (removal unsafe: guard needed at path:line)`.
   - Enforce **Deletion Dominance**: when a `BLOCKING_SCOPE` deletion of a file, class, helper, field, or branch is confirmed, drop all overlapping internal `#include` or `import`, naming, comment, micro-style, or local defect items inside that deleted span instead of polishing code being deleted.
   - `OPTIONAL_GOLFING` (`nit:`) suggestions never block upload or trigger an extra rebuild cycle on their own: fold them in only when safe and a rebuild is already running for a `BLOCKING` fix, or skip them when the diff is otherwise ready to ship.
   - Ensure all remaining comments strictly obey `code-comments` rules (explain timeless *why*, no final period in C++).
2. **Filter Surviving Behavioral Defects (`CONFIRMED` / `UNPROVEN`) & Persist Refutations**:
   - **Record Skeptic Refutations in the Persistent Finding Ledger**: For every candidate (`B1...`, `S1...`) marked **`REFUTED`** (and whenever the user in chat dismisses a finding as intentional design or out-of-scope for this CL), persist it immediately:
     ```bash
     python3 ~/.gemini/config/scripts/finding_ledger.py add \
       --repo . --file <path> --symbol "<func_or_class>" \
       --claim "<concise summary of suspected defect or unsafe removal>" \
       --reason "<exact path:line guard, caller SkASSERT, or user rationale>" \
       --scope repo  # use --scope cl for trade-offs specific to the current CL/branch
     ```
   - **Validate Surviving `CONFIRMED` / `UNPROVEN` Defects via Reproducer Test (No Silent `"Not covered:"` Demotion)**:
     - **Never demote a Stage A `B*` defect or a diff-induced regression vs. `HEAD`/`main` into `"Not covered:"` / `"known limitation"`** without running `keeper_skeptic` and Trap Test validation (see `INCIDENTS.md`, Gerrit 1418376 PS2–PS6). `"Not covered:"` is valid strictly for pre-existing, untouched behavior outside the modified code path; if fixing a real `B*` defect requires a genuine scope/design trade-off, escalate to the user via `ask_question` rather than deciding inside `<thinking>`.
     - Discard any `B*` defect marked **`REFUTED`** with a verified `file:line` guard or contract citation. For each surviving **`CONFIRMED`** or **`UNPROVEN`** defect, write the minimal reproducer Trap Test (`SkParagraphTest.cpp` / `web_paragraph_test.dart`) and compile/run once:
       - **Test FAILS / CRASHES**: Finding is `[VALIDATED]`. Implement the root-cause fix (no symptomatic `if` branches) and keep the test.
       - **Test PASSES**: Finding is `[FALSE POSITIVE: Refuted by Execution]`. Revert the test (unless it adds genuine boundary value), do **not** touch production code, and record via `finding_ledger.py add --category false-positive-exec ...`.
3. **Resolve Pre-Review Grill Questions**:
   - If a Section 3 question has a known technical answer from the codebase or debugging session, **silently address it** in a 1-line "why" code comment or in the commit message.
   - Surface a question to the user in the final summary **only** if it represents a genuine contract/product ambiguity that requires the user's decision.

---

### Phase 4: Single Verification Pass, Tree-Pinned Rebase & Summary

0. **Test Liveness Gate (Mandatory Before Any "Tests Pass" Claim, Enforced by `TEST-LIVENESS GUARD`)**:
   Read [`references/gates.md`](references/gates.md) §0 and run for every new or modified test in the diff (including ones a previous chat or state file reports as done; read the test body yourself):
   - **Mutation check** (`scripts/mutation_check.py`, never by hand; requires green → red with mutation → restored byte-for-byte in `finally` → green, writing `$(git rev-parse --git-path jetski/mutation_attestations)/<Name>.json`).
   - **Whole-diff liveness & warning audit** (`scripts/verify_test_liveness.py --repo . --diff <base rev> --log /tmp/run.log`: checks build list membership, named execution in log, non-empty collection asserts before loops, no absent host `/system/...` paths, no unguarded font-backend metric pinning per `skia_headers_and_font_tests.md`, zero unexplained warnings—root-cause before `--explained-warning REGEX`, never `grep -v`—and `PASS` mutation attestation).
   - **Benchmarks & Differential Equivalence**: Prove benchmarks load non-zero input data before quoting numbers; for fast paths, lookup tables, or caches, verify byte-for-byte / element-for-element equivalence across a generated boundary corpus (e.g. all `0x110000` Unicode code points, lone surrogates, mixed scripts) and record the corpus size in the commit message.

1. **Run the Relevant Test Suite Once (Executable Code Only, Enforced by `CORPUS-CHECK GUARD` & `PERF-CHECK GUARD` Before Upload/Push)**:
   Read [`references/gates.md`](references/gates.md) §1–6 and run every required gate for the modified paths (`dm --match SkParagraph`, `flutter-test-paragraph-engine`, Flutter web CanvasKit `felt test --suite chrome-dart2js-canvaskit-canvaskit`, `skia_patch_ab.py` perf gate, `google3-test-paragraph` A/B corpus, `webparagraph-corpus`, Chromium OOP-R/Fontations/PoC gate, and `skia-canary-advisor` with mandatory automatic ASAN bot trigger and `NativeFonts_Fontations` selection).

2. **Tree-Pinned 3-Way Rebase & Attestation Invalidation (Pre-Upload Gate)**:
   - **Commit at the Verified Base SHA**: Never move changes across branches, worktrees, or checkouts via raw `git diff | git apply`, `--recount`, or `git stash pop` onto a drifted `HEAD`. Always commit the verified diff at the exact `base_commit_sha` where tests passed, then transfer or update using true 3-way Git mechanics (`git rebase` or `git cherry-pick`).
   - **Invalidate Attestation on Upstream Overlap**: If the branch is rebased onto a newer `origin/main` after verification, check `git diff --name-only ORIG_HEAD..HEAD`. If any upstream change overlaps the modified subsystem or its dependencies, treat the prior test run as invalidated and re-run the test target once on the rebased tree before `git cl upload` (always wrap `git cl upload` and `git cl try` in `luci-auth context -- git cl ...` so `depot_tools` uses 7-day `LOAS2` instead of 20-hour `CorpSSO`; note that `chromium/src` still requires 20-hour `RAPT` via `git-credential-luci reauth` on `git cl upload`, whereas `skia` does not).
   - **Final Commit Message & PR Description Audit**: Read [`references/commit_message.md`](references/commit_message.md) for Tier 1 / Tier 2 structure, the explicit pre-fix test failure statement, the complete human-readable `Tested:` block (`canary_advisor.py triage` must print `TESTED BLOCK: OK`), the 4-bullet `Reviewer & AI Audit Context` block for `WebParagraph` / `flutter/flutter` PRs, and the single `Assisted-by:` trailer. Before `git cl upload` or `gh pr create`, run `audit_comments.py --commit-msg <(git log -1 --format=%B HEAD)` (or `scripts/precommit_razor.py --check-commit-msg`) to confirm zero internal leaks, a valid 40-hex `Change-Id`, and **exactly one** `Assisted-by:` trailer.

3. **Report Minimization & Audit Telemetry in Chat**:
   Output a concise summary directly in chat (no extra artifact files):
   - **Diff Size**: `Before: <N> lines (+A / -D) -> After: <M> lines (+A' / -D') (<X>% of original)`
   - **Pruned Scope/Bloat**: Brief list of reverted drive-bys or simplified blocks.
   - **Critic Findings**: Count of `[VALIDATED]` fixes vs. `[REFUTED by Skeptic (model)]` vs. `[FALSE POSITIVE by Execution]`.
   - **Ready to Commit/Upload**: Proposed English commit message with the `Assisted-by:` trailer.
