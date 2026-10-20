---
name: amnesia-precommit
description: >-
  Token-efficient pre-commit pipeline for Skia, SkParagraph, WebParagraph, and Flutter changes.
  Combines (1) deterministic mechanical pruning (comment & anti-slop rules), (2) a single
  clean-context subagent pass to minimize diff size (revert drive-bys, golf logic) and check
  KEEPER invariants without chat history bias, and (3) a hostile TDD filter to validate
  reported bugs before rebuilding. Trigger when the user asks to "подготовь к коммиту",
  "минимизируй дифф", "проверь перед пушем", "сделай амнезия-ревью", or "run amnesia precommit".
---

# Amnesia Pre-Commit & Diff Minimizer (`amnesia-precommit`)

Execute a strict, single-pass pre-commit audit and diff minimization workflow before uploading a CL or pushing a PR. This skill prevents chat-history confirmation bias, strips drive-by changes, and filters out AI reviewer hallucinations using a single subagent and at most one rebuild cycle.

---

## Core Invariants

1. **Zero-Token First Pass**: Always run deterministic scripts (`audit_comments.py` and `ruthless_audit.py`) before invoking any subagent.
2. **Single Amnesiac Subagent**: Spawn **at most one** isolated subagent (`keeper_inquisitor` or `self`) to evaluate the diff. Never pass conversational history—only the raw `git diff` and the intended task summary.
3. **Hostile TDD Validation (`Trapsmith` Filter)**: Never alter production logic based solely on the subagent's behavioral critique. A reported bug is valid **only** if a newly written reproducer unit test fails against the current code.
4. **Single Rebuild Budget**: Batch all diff-shrinking edits and validated bug fixes into a single compilation and test pass (`dm`, `felt`, or `flutter test`). Do not enter multi-iteration review-compile loops.

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

2. **Run Comment, Anti-Slop, & Repo Presubmit Audits**:
   ```bash
   python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py --diff HEAD --repo .
   python3 ~/.gemini/config/skills/ruthless-code-audit/scripts/ruthless_audit.py . --repo . --diff HEAD
   # If the repository has a PRESUBMIT.py (e.g., Skia), always run git cl presubmit:
   if [ -f PRESUBMIT.py ]; then SKIP_GCE_AUTH_FOR_GIT=1 git cl presubmit --force; fi
   ```

3. **Apply Mechanical Fixes**:
   - Fix trailing periods, missing "why" comments on non-trivial 1-line changes, and line length violations (`<= 100` cols).
   - Strip leftover debug prints, anonymous `TODO`s without `KEEPER-DEBT`, and uncalled helpers.
   - Format modified lines only: `git clang-format` (or `dart format` for Dart files).

---

### Phase 2: The Amnesiac Shrinker & Inquisitor (1 Subagent)

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
2. Evaluate the diff across three strict axes:
   - **Scope (Drive-By Pruning)**: Identify any hunk, file, formatting change, or refactor that is not strictly required to achieve the Intended Task Goal.
   - **Size (Diff Golfing)**: Identify overly verbose logic, redundant local variables, speculative helpers (YAGNI), or duplicate branches that can be expressed in fewer lines while preserving exact behavior and ABI.
   - **Correctness & KEEPER Invariants**: Check against C++ Systems Invariants (`cpp_systems_invariants.md`), Text Domain Invariants (`text_domain_invariants.md`), and Code Style (`code_style.md`) — specifically zero-allocation hot paths, loop guards, fail-fast `SkASSERT` over defensive `if (!ptr) return;`, and cross-layer consistency.
   - **Pre-Review Grill (Skeptical Reviewer's First Question)**: Identify if any hunk introduces an non-obvious contract change, subtle tradeoff, or "Why didn't you do X instead?" question that a skeptical Gerrit/PR reviewer will immediately ask.

Output Format (Strict):
### 1. Scope & Size Reductions (Safe to Apply Immediately)
For each reduction, provide `File:Lines`, why it is unnecessary/verbose, and the exact replacement or `git checkout` action.

### 2. Behavioral / Invariant Defects (Requires Test Validation)
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

### Phase 3: Pruning & The Trapsmith TDD Filter

When the subagent returns its report, process the sections as follows:

#### 1. Apply Scope & Size Reductions
- Immediately revert flagged drive-by changes and apply safe code simplifications that preserve behavior.
- Ensure all remaining comments strictly obey `code-comments` rules (explain timeless *why*, no final period in C++).

#### 2. Filter Behavioral Defects via Reproducer Test
Do **not** blindly fix issues listed in Section 2 (`B1, B2...`). AI critics frequently hallucinate null dereferences or edge cases already guaranteed by upstream callers.

For each candidate defect:
1. **Static Contract Check**: Verify the caller contract first. If an upstream `SkASSERT` or API boundary already forbids the state, discard the finding as `[FALSE POSITIVE: Contract Guaranteed]`.
2. **Write the Trap Test**: If the state is reachable, add the minimal reproducer unit test (e.g., in `ParagraphTest.cpp` or `web_paragraph_test.dart`).
3. **Compile & Run Once**:
   - **Test FAILS / CRASHES**: Finding is `[VALIDATED]`. Implement the root-cause fix (no symptomatic `if` branches) and keep the test.
   - **Test PASSES**: Finding is `[FALSE POSITIVE]`. Revert the test (unless it adds genuine boundary value) and do **not** touch production code.

#### 3. Resolve Pre-Review Grill Questions
- If a question in Section 3 has a known technical answer from the codebase or debugging session (e.g., why approach A was chosen over B), **silently address it** in a 1-line "why" code comment or in the commit message.
- Surface a question to the user in the final summary **only** if it represents a genuine contract/product ambiguity (e.g., an unintended behavior change on an edge case) that requires the user's decision.

---

### Phase 4: Single Verification Pass & Summary

1. **Run the Relevant Test Suite Once**:
   - **Skia (`SkParagraph`)**: Build and run `dm --match Paragraph` (or use `skia-test-paragraph`).
   - **Flutter Engine / WebParagraph**: Run the targeted `felt` or `flutter test` target (or use `flutter-test-paragraph-engine` / `webparagraph-corpus`).

2. **Report Minimization & Audit Telemetry in Chat**:
   Output a concise summary directly in chat (no extra artifact files):
   - **Diff Size**: `Before: <N> lines (+A / -D) -> After: <M> lines (+A' / -D') (<X>% of original)`
   - **Pruned Scope/Bloat**: Brief list of reverted drive-bys or simplified blocks.
   - **Critic Findings**: Count of `[VALIDATED]` fixes vs. `[FALSE POSITIVE]` rejections.
   - **Ready to Commit/Upload**: Proposed English commit message with the `Assisted-by:` trailer.
