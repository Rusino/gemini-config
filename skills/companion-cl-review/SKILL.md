---
name: companion-cl-review
description: >-
  Proof-by-WIP-CL code review discipline for Gerrit CLs and GitHub PRs. Instead of relying solely
  on prose comments, checks out the author's exact patchset without rebasing, tests domain edge
  cases with a compiler and test runner, classifies the CL into 5 operational outcomes (1: Ideal CL,
  2: Near-Ideal / Minor Nits, 3: No Failing Tests / Coverage or Cleanup Only, 4: Fundamental Design
  Flaw / Failing Reproducer Tests Only, 5: Edge-Case Defects + Clean Fix in 2 Patchsets), uploads a
  silent stacked WIP companion CL (PS1 = failing tests with timeless comments, PS2 = passing fix)
  when warranted, and drafts a collaborative English review message giving the author full freedom
  to cherry-pick tests, fold in the fix, or split the CL. Trigger by default on code reviews (via
  `review-triage` or directly) or when the user asks for "ревью с компаньоном", "сделай WIP СЛ для
  ревью", "повесь дочерний СЛ с тестами и фиксом", "companion cl review", unless the user explicitly
  opts out ("без компаньона", "только текстом", "without companion CL").
---

# Proof-by-WIP-CL Code Review (`companion-cl-review`)

Prose code review discussions on subtle edge cases often degrade into unverified back-and-forth debates, or worse, suggest fixes that do not compile or break existing tests.

`companion-cl-review` replaces speculative prose critique with **compiler-verified proof**:
1. Attempt to break the author's patchset with targeted contract tests (`Gate A`).
2. Classify the outcome into one of **5 operational cases** so simple or clean CLs never generate noise.
3. When edge-case failures or substantive gaps exist, stack a **silent WIP companion CL** directly on top of the author's patchset (`PS1` = failing reproducer tests; `PS2` = verified fix) and draft a constructive English review note in the parent CL.

---

## Opt-Out Rule (Prose-Only Override)

This skill is the **default execution phase** for substantive code reviews after triage. However, if the user explicitly asks for a prose-only review (e.g., *"без компаньона"*, *"только текстом"*, *"просто посмотри без дочернего СЛя"*, *"text-only review"*), **skip companion CL creation** and deliver a standard inline/top-level text review.

---

## The 5-Case Decision Matrix

Never upload a companion WIP CL blindly. After auditing the patchset and testing edge cases locally, classify the review into exactly one of the five cases below:

| Case | Audit & Test Outcome | Companion WIP CL Action | Review Message in Author's CL |
| :--- | :--- | :--- | :--- |
| **Case 1: Ideal CL**<br>*(Zero defects, solid coverage)* | Edge cases (`BiDi`, fallback, `letterSpacing`, `maxLines`, empty input, zero width, concurrency) are already handled and covered by tests. | **None** (0 Gerrit/GitHub churn). | Concise `LGTM` in English naming the specific non-trivial invariants and edge cases verified against the patchset. |
| **Case 2: Near-Ideal CL**<br>*(Cosmetic, style, or tiny 1–3 line nits)* | Core logic and tests are sound; only minor issues remain (e.g., trailing periods in comments, unsorted forward declarations, tiny local cleanup). | **None** (forcing a cherry-pick for 3 lines of style nits is overkill). | Standard inline review comments (or `LGTM with nits`) at the exact lines. |
| **Case 3: No Failing Tests**<br>*(Logic holds, but missing edge-case test coverage or clean structural simplification)* | Newly written adversarial contract tests **all pass** on the author's code, OR you have a pure non-functional simplification / allocation removal (>15–20 lines) that is easier to inspect as a diff than in prose. | **1-Patchset WIP CL (`PS1` only)** containing the additional passing edge-case tests and/or structural cleanup (if <15 lines, use an inline code snippet instead). | Praise the implementation for surviving the edge-case suite; link the optional `PS1` companion CL so the author can cherry-pick the extra tests or cleanup if they wish. |
| **Case 4: Fundamental Flaw**<br>*(Broken architecture, wrong subsystem layer, or requires a ground-up rewrite)* | Contract tests fail on fundamental scenarios, or the CL mutates the wrong architectural layer where writing a "fix" would mean rewriting the entire feature from scratch. | **1-Patchset WIP CL (`PS1` = Failing Reproducer Tests ONLY)**. Do **not** write `PS2` on top of a broken foundation. | Constructive pushback explaining the architectural/design issue, linking `PS1` as a ready-made failing test harness the author can use while redesigning the approach. |
| **Case 5: Edge-Case Defects + Clean Fix**<br>*(Sound direction, breaks on 1–4 specific edge cases or admits a much cleaner invariant fix)* | Contract tests fail on specific edge cases (`Gate A` proven), and a clean, minimal production fix resolves all failures without regressions. | **2-Patchset WIP CL**:<br>• `PS1`: Failing contract tests **only**.<br>• `PS2`: Production fix + passing tests (`git commit --amend`). | Detailed English review message explaining each edge case, linking the companion WIP CL (`PS1` = failing tests, `PS2` = fix), and inviting the author to take the whole patch, cherry-pick parts, or split into a follow-up CL. |

---

## Step-by-Step Execution Protocol

### Phase 1: Isolated Checkout Without Rebasing (Strict Parent Invariant)

1. **Fetch the exact reviewed patchset into an isolated worktree (or a clean checkout)**:
   - Check `git status` first; if the primary working copy has local modifications or another active branch, create a separate `git worktree` (never switch branches inside a dirty checkout):
   ```bash
   git fetch https://skia.googlesource.com/skia refs/changes/<NN>/<CL>/<PS>
   git worktree add -B companion-<CL>-ps<PS> ../companion-<CL>-ps<PS> FETCH_HEAD
   ```
   *(If stored credentials fail on public Gerrit repos, use `git -c credential.helper= -c http.cookiefile=/dev/null fetch ...`. For GitHub PRs, fetch `pull/<PR>/head`.)*
2. **NEVER rebase onto `main`**:
   - The companion CL's parent commit (`parents[0]`) **must** be the exact SHA of the author's current patchset (`FETCH_HEAD`).
   - Rebasing onto `origin/main` before pushing a child CL either rewrites the author's parent CL in Gerrit or breaks the clean single-parent diff between the author's patchset and your companion CL.
3. **Never push a new patchset directly to the author's CL or edit the author's commit message in Gerrit**:
   - Uploading to the author's own CL number makes you the patchset uploader, which disqualifies your `Code-Review+1` from counting toward `OWNERS` approval in Gerrit and forces the author to re-upload an identical patchset just to restore ownership. Always use a separate `Change-Id`.

---

### Phase 2: Adversarial Contract Testing (`Gate A`) & Classification

1. **Design targeted edge-case tests**:
   - Based on static audit of the changed hunks, write unit tests targeting the exact boundary conditions the CL touches (e.g., `BiDi` / RTL transitions, fallback fonts, `letterSpacing != 0`, `maxLines` remainder loops, `layout(0)`, empty/whitespace-only inputs, cache eviction/concurrency).
   - If the host pipeline supplies confirmed findings from a domain review lens (subsystem checklists, historical reviewer precedents), turn each confirmed finding into at least one contract test first.
2. **Timeless Test Comments Invariant (No `"Fails on cl/..."` Annotations)**:
   - Write comments inside the test file as **timeless domain invariants** (describing *why* the contract must hold in `main`, without trailing periods in C++).
   - **Never** write `"// Fails on cl/123456 because..."` or `"// Reproducer for PS3 bug"` in test comments — when the author folds your patchset into their CL, temporary review-time phrasing pollutes their commit and requires another round of review cleanup.
3. **Compile and run the test binary**:
   - If no defects exist and no new tests are needed → **Case 1** or **Case 2** (stop here and draft prose review).
   - If the new tests **pass** on the author's unmodified code → **Case 3**.
   - If the new tests **fail** (`Gate A` confirmed), record the exact failing test names and assertion count (`N failing assertions across K tests`), then decide between **Case 4** (reproducer tests only) and **Case 5** (tests in `PS1` + fix in `PS2`).

---

### Phase 3: Uploading `Patchset 1` (Tests Only, Silent WIP)

For **Case 3**, **Case 4**, and **Case 5**, commit **only the test file changes** first:

1. **Stage only test files**:
   ```bash
   git add <path/to/test_file>
   ```
   Verify with `git diff --cached --name-only` that zero production files are staged in `PS1`.
2. **Commit with a fresh `Change-Id` and a clear test-only title**:
   - Title format: `[<module>] Add failing <topic> tests` (for Cases 4 & 5) or `[<module>] Add <topic> edge-case tests` (for Case 3).
   - Include the `Assisted-by:` trailer (`python3 ~/.gemini/config/hooks/chat_lifecycle.py model --trailer` if available, else `Assisted-by: AI`).
3. **Upload `PS1` strictly as `WIP` with ZERO reviewers**:
   ```bash
   git push origin HEAD:refs/for/main%wip
   ```
   - **Never** add reviewers (not even the parent CL's author) to the Gerrit `Reviewers` field of the companion CL. Leaving `Reviewers` empty with `%wip` prevents notification spam to `OWNERS`/CC lists and signals that you are **not** claiming ownership of a competing CL — the companion CL exists purely as a sandbox for the author.

---

### Phase 4: Implementing & Verifying `Patchset 2` (Production Fix, Case 5 Only)

When **Case 5** applies:

1. **Implement the minimal root-cause fix**:
   - Prefer structural invariant fixes (e.g., eliminating redundant state or handling the boundary at its source) over symptomatic special-case branches.
   - Ensure all code comments follow project conventions (concise English rationale explaining *why*, 1–4 lines, no trailing periods in C++, `<= 100` columns).
2. **Verify all tests pass**:
   - Recompile and run both the new contract tests from `PS1` and the full module test suite to prove zero regressions.
3. **Run pre-commit verification (`amnesia-precommit` / Clean-Context Audit)**:
   - Run comment and code audits on `HEAD` (including `keeper_inquisitor` + `keeper_skeptic` if available in the environment) so the proposed fix is guaranteed free of secondary edge-case bugs before the author sees it.
4. **Amend into `PS2` and push with `%wip`**:
   ```bash
   git add -u
   git commit --amend
   git push origin HEAD:refs/for/main%wip
   ```
   - Update the commit subject and body in `PS2` to describe the complete fix + tests cleanly (so if the author copies the commit message or folds `PS2` directly into their CL, it is already review-ready).
   - Now Gerrit's patchset selector on the companion CL cleanly separates:
     - `Base -> PS1`: Reproducer unit tests only (fails on the parent CL).
     - `PS1 -> PS2`: Pure production fix delta (turns the tests green).
     - `Base -> PS2`: Complete combined diff ready to fold in.
   - *(GitHub PR equivalent: instead of `git commit --amend`, push two separate commits to a Draft PR or comparison branch — Commit 1 with the failing contract tests and Commit 2 with the production fix — so the commit list preserves the same 2-step separation.)*

---

### Phase 5: Drafting the Collaborative Review Message (Author Freedom)

Never auto-publish the review comment to Gerrit or GitHub unless the user explicitly asks you to post it. Present the draft in chat for the user to review and post on the parent CL.

Structure the English review comment using this proven template:

1. **Constructive Opening**:
   Acknowledge the goal and direction of the patchset (`"Thanks for putting this together! While testing PS<N> against a few additional scenarios, I found <K> edge cases where..."`).
2. **Numbered Root-Cause Breakdown**:
   For each edge case (`1.`, `2.`, ...):
   - Cite the exact `File.cpp:line` and symbol in the author's patchset.
   - Explain the concrete input scenario and why the current logic miscomputes or crashes.
   - Explain the invariant fix in 1–2 sentences (or with a 3-line inline snippet if helpful).
3. **Companion WIP CL Link & Patchset Guide**:
   Provide the direct link to the stacked companion WIP CL, explicitly explaining the patchset layout and giving the author complete autonomy:
   > I uploaded a companion WIP CL stacked on top of PS`<N>` so you can inspect the isolated diffs:
   > `<https://...-review.googlesource.com/c/.../+/<COMPANION_CL>>`
   > - **Patchset 1** adds the `<K>` unit tests (`<TestName1>`, `<TestName2>`, ...) that fail on PS`<N>`.
   > - **Patchset 2** has the corresponding fix (all `<Total>` `<Module>` tests pass).
   >
   > Feel free to fold the whole patchset into this CL, cherry-pick just the parts/tests you want, or keep the fix as a separate follow-up CL — whichever you prefer! (Once it's folded in or no longer needed, I'll abandon the WIP CL.)

---

## Self-Review Mode (Reviewing Your Own CL)

When the CL under review is owned by the user (Gerrit `owner`, not merely the uploader, matches the user's account; on GitHub, the PR author), there is nothing to fork: **skip the companion CL and upload the same patchset sequence directly into the user's own CL**.

1. **Same parent invariant**: work on top of the CL's current patchset without rebasing, so patchset-to-patchset diffs stay clean.
2. **Same patchset structure, in the CL itself**:
   - **Case 5**: `PS N+1` = failing contract tests only; `PS N+2` = fix + passing tests. The failing-tests patchset is intentional: the patchset history documents what broke and how it was fixed.
   - **Case 4**: `PS N+1` = failing reproducer tests only, then report the design problem to the user in chat before any redesign.
   - **Case 3**: one patchset with the extra tests or cleanup.
   - **Cases 1–2**: apply nits directly in one patchset.
3. **Before reviewers are involved** (no reviewers on the CL yet, the usual case for self-review): upload freely without asking; there are no votes to reset and nobody to notify.
4. **After reviewers are added**: prepare the patchsets locally and **ask the user before pushing**, because every upload resets copied `Code-Review` votes and notifies reviewers. Upload the intermediate failing-tests patchset with `%notify=NONE` to avoid an extra notification.
5. **Not uploaded yet** (local branch only): use two local commits (tests, then fix) instead of patchsets; squashing before upload is the user's choice.
6. **Never trigger CQ** on any patchset unless the user explicitly asks.

---

## Post-Adoption Cleanup

Once the author folds the tests/fix into a new patchset of their parent CL (or lands an alternative fix), verify the parent CL's new patchset and **abandon** the temporary companion WIP CL so no stale WIP changes remain open in Gerrit.
