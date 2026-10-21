---
name: amnesia-precommit
description: >-
  Token-efficient pre-commit pipeline for Skia, SkParagraph, WebParagraph, and Flutter changes.
  Combines (1) deterministic mechanical pruning (comment & anti-slop rules), (2) a clean-context
  Hunter subagent pass (keeper_inquisitor) to minimize diff size, verify dropped guards, and check
  pattern completeness, (3) a clean-context Skeptic Refutation pass (keeper_skeptic, Hunt then Refute
  with XFAMILY_REFUTE auto-routing) to eliminate false-positive defects and unsafe code removals,
  and (4) a hostile TDD filter to validate surviving bugs before a single rebuild. Trigger when the
  user asks to "подготовь к коммиту", "минимизируй дифф", "проверь перед пушем",
  "сделай амнезия-ревью", or "run amnesia precommit".
---

# Amnesia Pre-Commit & Diff Minimizer (`amnesia-precommit`)

Execute a strict pre-commit audit and diff minimization workflow before uploading a CL or pushing a PR. This skill prevents chat-history confirmation bias, strips drive-by changes, and filters out AI reviewer hallucinations using a two-stage **Hunt then Refute** clean-context pipeline and at most one rebuild cycle.

---

## Core Invariants

1. **Zero-Token First Pass**: Always run deterministic scripts (`audit_comments.py` and `ruthless_audit.py`) before invoking any subagent.
2. **Two-Stage "Hunt then Refute" Clean-Context Pipeline**:
   - **Stage A (Hunter)**: Spawn one isolated subagent (`keeper_inquisitor` or `self`) with zero conversational history—only the raw `git diff` and the intended task summary.
   - **Stage B (Batched Verification Skeptic)**: Whenever the Hunter proposes `HIGH`/`CRITICAL` behavioral defects or non-trivial code/branch removals (`Scope & Size Reductions`), spawn **one batched Skeptic subagent** (`keeper_skeptic`) in a fresh context whose sole goal is to **REFUTE** each candidate using real code, callers (`git grep`, `code_search`), and tests.
3. **Hostile TDD Validation (`Trapsmith` Filter)**: Never alter production logic based solely on an LLM critique. A behavioral bug that survives the Skeptic (`CONFIRMED`) is fixed **only** if a newly written reproducer unit test fails against the current code.
4. **Single Rebuild Budget**: Batch all confirmed diff-shrinking edits and validated bug fixes into a single compilation and test pass (`dm`, `felt`, or `flutter test`). Do not enter multi-iteration review-compile loops.

---

## Workflow

### Phase 1: Deterministic Mechanical Razor (0 Subagents)

Before spawning an agent, clean up mechanical violations locally in the working tree:

1. **Inspect Working Tree & Diff Stats**:
   ```bash
   git status --short
   git diff HEAD --stat
   ```
   Record the initial changed line count (`added + deleted`) as the baseline for minimization.

2. **Run Comment, Anti-Slop, Public CL Hygiene, & Repo Presubmit Audits**:
   ```bash
   python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo . --check-cl-hygiene
   python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py . --repo . --diff HEAD
   # If the repository has a PRESUBMIT.py (e.g., Skia), always run git cl presubmit:
   if [ -f PRESUBMIT.py ]; then SKIP_GCE_AUTH_FOR_GIT=1 git cl presubmit --force; fi
   ```

3. **Apply Mechanical Fixes**:
   - Fix trailing periods, missing "why" comments on non-trivial 1-line changes, and line length violations (`<= 100` cols).
   - Strip leaked internal corporate markers (`go/`, `goto.google.com`, `cl/`, `cr/`, depot/CitC paths, machine-local home paths, `sponge`, `critique`), code-comment `b/<id>` references (replace with public tracker IDs such as `crbug.com/` or `skia:`), and malformed Gerrit `Change-Id` lines (must be `I` + 40 lowercase hex chars).
   - Strip leftover debug prints, anonymous `TODO`s without `KEEPER-DEBT`, and uncalled helpers.
   - Format modified lines only: `git clang-format` (or `dart format` for Dart files).

---

### Phase 2: The Amnesiac Shrinker & Inquisitor (Stage A Hunter Subagent)

Invoke a single clean-context subagent that has **zero knowledge** of the current conversation's debugging history or compromises.

```python
invoke_subagent(
    TypeName="keeper_inquisitor",
    Role="Amnesiac Diff Minimizer & Auditor",
    Workspace="inherit",
    Prompt="""
You are evaluating an uncommitted/pending diff in a clean context with zero prior chat history.

Intended Task Goal:
<1-2 sentence summary of what the change is supposed to achieve>

Instructions:
1. Run `git diff HEAD` (or inspect the target commit) in `<repo_path>`.
2. Evaluate the diff across six strict axes:
   - **Scope (Drive-By Pruning)**: Identify any hunk, file, formatting change, or refactor that is not strictly required to achieve the Intended Task Goal.
   - **Size (Diff Golfing)**: Identify overly verbose logic, redundant local variables, speculative helpers (YAGNI), or duplicate branches that can be expressed in fewer lines while preserving exact behavior and ABI.
   - **Dropped Guards & Historical Regressions (`-` Lines)**: For every deleted or simplified condition, branch, early return, or fallback (`-` lines in `git diff`, or any reduction you propose under Diff Golfing), run `git log -S "<symbol_or_expr>" -p -n 3` or `git log -L <start>,<end>:<file>` to verify the removed guard was not previously introduced to fix a specific bug or edge case.
   - **Pattern Completeness ("What is Missing from the Diff?")**: Inspect full modified files and run `git grep` or `code_search` on modified functions, structs, enums, and flags to check all callers, paired backends, and symmetrical code paths (e.g., `SkParagraph` vs. `WebParagraph`, ICU vs. Apple/Client `SkUnicode`, `layout` vs. `paint` / `getRectsForRange`, serialization read vs. write, header declarations vs. all implementations). Flag any sibling site that was forgotten.
   - **Correctness & KEEPER Invariants**: Check against C++ Systems Invariants (`cpp_systems_invariants.md`), Text Domain Invariants (`text_domain_invariants.md`), and Code Style (`code_style.md`) — specifically zero-allocation hot paths, loop guards, fail-fast `SkASSERT` over defensive `if (!ptr) return;`, and cross-layer consistency.
   - **Pre-Review Grill (Skeptical Reviewer's First Question)**: Identify if any hunk introduces a non-obvious contract change, subtle tradeoff, or "Why didn't you do X instead?" question that a skeptical Gerrit/PR reviewer will immediately ask.

Output Format (Strict):
### 1. Candidate Scope & Size Reductions (S1, S2...)
For each reduction (`S1`, `S2`...), provide `File:Lines`, why it is unnecessary/verbose, and the exact replacement or `git checkout` action.

### 2. Behavioral, Dropped-Guard & Pattern-Completeness Defects (Requires Refutation & Test Validation)
For each suspected bug:
- `ID`: B1, B2...
- `File:Lines`: <path and range>
- `Defect`: <exact breaking input or state>
- `Proposed Reproducer Test`: <exact test input and assertion that should fail if this bug is real>

### 3. Pre-Review Grill (Unanswered Reviewer / Contract Questions)
At most 1–2 sharp questions a skeptical reviewer will ask about this diff (either missing rationale for a non-obvious choice or an unconfirmed behavioral/contract change). Write `None` if the diff is self-evident.

If nothing is found in a section, write `None`.
"""
)
```

---

### Phase 2.5: The Skeptic Refutation Pass (Stage B Skeptic Subagent, `Hunt then Refute` + `XFAMILY_REFUTE`)

Aggressive Hunter subagents suffer from confirmation bias: they frequently invent hypothetical null/bounds bugs already prevented by upstream callers, or misclassify a load-bearing guard as "redundant bloat" under Section 1 (`Scope & Size Reductions`).

Whenever Phase 2 produces any candidate defects in **Section 2 (`B1, B2...`)** OR any non-trivial code/branch removal in **Section 1 (`S1, S2...`)** (skip if Phase 2 returned `None` or only trivial whitespace/comment cleanup), spawn **one batched `keeper_skeptic` subagent** in a clean context to challenge all candidates at once.

#### 1. Automatic Model Routing (`XFAMILY_REFUTE`)
Select the `Model` argument for `invoke_subagent` automatically based on the hardest candidate in the packet:
- **`Model="flash"` (Default Fast Skeptic)**: Use when all candidates are local single-function invariants, dead-code/scope reductions, or straightforward caller checks.
- **`Model="pro"` (Auto-Escalation for Deep Invariants)**: Automatically escalate to `Model="pro"` if **any** candidate in the batch involves:
  1. Concurrency, locks, thread safety, or asynchronous object lifetimes;
  2. BiDi level resolution, UTF-8 / UTF-16 / grapheme cluster index math, or `SkParagraph` line/run metrics;
  3. Dropped historical guards (`-` lines requiring `git log -S` / `git blame` root-cause tracing);
  4. Cross-backend symmetry (`SkParagraph` ↔ `WebParagraph`, ICU ↔ Apple/Client `SkUnicode`);
  5. Re-verification of a blocking candidate that a `flash` Skeptic marked `UNPROVEN`.

#### 2. Batched Skeptic Invocation
```python
invoke_subagent(
    TypeName="keeper_skeptic",
    Role="Batched Verification Skeptic",
    Model="<flash or pro per routing rules above>",
    Workspace="inherit",
    Prompt="""
You are the Verification Skeptic for a pre-commit audit in `<repo_path>`.
Your sole job is to REFUTE each candidate below using real code, callers, comments, git history, and tests. A refutation you cannot prove with an exact `path:line` citation or caller search is a confirmation, not a dismissal.

Candidates Under Test (Batch V001):
<paste all non-trivial Section 1 Scope/Size removals (S1, S2...) and all Section 2 Behavioral/Completeness defects (B1, B2...)>

Instructions:
1. Apply the Three Iron Verification Principles:
   - **Refute with Code (`file:line`), Never Memory**: Never write "looks handled" or "the caller probably checks". Cite the exact `path:line` guard, caller `SkASSERT`, or `git grep` / `code_search` output.
   - **Documented Intent Overrides Syntactic Omissions**: Adjacent comments, header docstrings, and unit tests that explicitly codify the behavior refute claims of accidental omission.
   - **Burden of Proof Requires Reachable Harm**: A missing defensive `if (!ptr)` check is REFUTED if callers already guarantee the contract via `SkASSERT` or API boundary. For a proposed code removal (`S1...`), REFUTE the removal if `git log -S` or a caller trace shows the branch guards a real edge case.
2. Output exactly one verdict (`REFUTED`, `CONFIRMED`, or `UNPROVEN`) per candidate ID with its mandatory evidence fields.
"""
)
```

#### 3. Quota Exhaustion Fallback Ladder
If the `keeper_skeptic` call fails due to model quota exhaustion (`429` / resource exhausted) or timeout:
1. **Step 1 — Auto-Downgrade (`pro` → `flash` → `flash_lite`)**: Immediately retry the exact same Skeptic batch on `Model="flash"` (or `flash_lite`) and tag the resulting verdicts `[Verified via Flash fallback (Pro quota exhausted)]`.
2. **Step 2 — Deterministic Trapsmith TDD Fallback (0 LLM Quota)**: In `amnesia-precommit`, if subagent quota is exhausted across models, bypass the LLM Skeptic and validate each candidate directly in Phase 3 by writing the minimal unit test and running the native test binary (`dm`, `felt`, or `flutter test`).
3. **Step 3 — User Escalation**: If all models are exhausted and no local test binary can settle the candidate, never silently drop or blindly apply it—mark it `[UNVERIFIED: Quota Exhausted]` in the final report and ask the user whether to switch the chat model (`python3 ~/.gemini/config/hooks/chat_lifecycle.py model`) or pause.

---

### Phase 3: Pruning & The Trapsmith TDD Filter

When the Skeptic pass completes, process the surviving items as follows:

#### 1. Apply Confirmed Scope & Size Reductions
- Apply only those Section 1 reductions whose safety was **not** refuted by `keeper_skeptic`. Drop any reduction marked `REFUTED (removal unsafe: guard needed at path:line)`.
- Ensure all remaining comments strictly obey `code-comments` rules (explain timeless *why*, no final period in C++).

#### 2. Filter Surviving Behavioral Defects (`CONFIRMED` / `UNPROVEN`) via Reproducer Test
Discard any Section 2 defect (`B1, B2...`) that `keeper_skeptic` marked **`REFUTED`** with a verified `file:line` guard or contract citation. For each surviving **`CONFIRMED`** or **`UNPROVEN`** defect:
1. **Write the Trap Test**: Add the minimal reproducer unit test (e.g., in `SkParagraphTest.cpp` or `web_paragraph_test.dart`).
2. **Compile & Run Once**:
   - **Test FAILS / CRASHES**: Finding is `[VALIDATED]`. Implement the root-cause fix (no symptomatic `if` branches) and keep the test.
   - **Test PASSES**: Finding is `[FALSE POSITIVE: Refuted by Execution]`. Revert the test (unless it adds genuine boundary value) and do **not** touch production code.

#### 3. Resolve Pre-Review Grill Questions
- If a question in Section 3 has a known technical answer from the codebase or debugging session (e.g., why approach A was chosen over B), **silently address it** in a 1-line "why" code comment or in the commit message.
- Surface a question to the user in the final summary **only** if it represents a genuine contract/product ambiguity (e.g., an unintended behavior change on an edge case) that requires the user's decision.

---

### Phase 4: Single Verification Pass, Tree-Pinned Rebase & Summary

1. **Run the Relevant Test Suite Once**:
   - **Skia (`SkParagraph`)**: Build and run `dm --match Paragraph` (or use `skia-test-paragraph`).
   - **Flutter Engine / WebParagraph**: Run the targeted `felt` or `flutter test` target (or use `flutter-test-paragraph-engine` / `webparagraph-corpus`).

2. **Tree-Pinned 3-Way Rebase & Attestation Invalidation (Pre-Upload Gate)**:
   - **Commit at the Verified Base SHA**: Never move changes across branches, worktrees, or checkouts via raw `git diff | git apply`, `--recount`, or `git stash pop` onto a drifted `HEAD`. Always commit the verified diff at the exact `base_commit_sha` where tests passed, then transfer or update using true 3-way Git mechanics (`git rebase` or `git cherry-pick`).
   - **Invalidate Attestation on Upstream Overlap**: If the branch is rebased onto a newer `origin/main` after verification, check `git diff --name-only ORIG_HEAD..HEAD`. If any upstream change overlaps the modified subsystem or its dependencies, treat the prior test run as invalidated and re-run the test target once on the rebased tree before `git cl upload`.
   - **Final Commit Message Audit**: Before uploading, verify the commit message with `audit_comments.py --commit-msg <(git log -1 --format=%B HEAD)` to confirm zero internal leaks, a valid 40-hex `Change-Id`, and the `Assisted-by:` trailer.

3. **Report Minimization & Audit Telemetry in Chat**:
   Output a concise summary directly in chat (no extra artifact files):
   - **Diff Size**: `Before: <N> lines (+A / -D) -> After: <M> lines (+A' / -D') (<X>% of original)`
   - **Pruned Scope/Bloat**: Brief list of reverted drive-bys or simplified blocks.
   - **Critic Findings**: Count of `[VALIDATED]` fixes vs. `[REFUTED by Skeptic (model)]` vs. `[FALSE POSITIVE by Execution]`.
   - **Ready to Commit/Upload**: Proposed English commit message with the `Assisted-by:` trailer.
