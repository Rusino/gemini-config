# Commit Message and CL/PR Description Format

Read before writing or amending a commit message, Gerrit CL description, or GitHub PR body.

## Tier 1: simple bug fixes and small refactors
- 1–3 sentences: the problem and the fix
- One line on how the new test fails without the fix (exact assertion, call count, or crash)
- A full `Tested:` block (below)

## Tier 2: algorithmic, caching, concurrency, or performance changes
A structured specification:
1. Problem and per-call cost of the current code path
2. Equivalence proof: why the new path gives the same result (and the generated input corpus size)
3. `Behavior notes` / `Assumptions` / `Thread safety`: edge cases, leaf-lock or lock-order proof,
   bounded-cache memory math (`bytes/entry × capacity = max KB`)
4. Compact `Before | After` tables and threshold sweeps (`no cache`, `cap1`, `cap2`, `no limit`)
5. Exact pre-fix failure of the new test (`Without the lock, the test crashes in 28 of 30 runs`),
   a full `Tested:` block, and `Not tested:` / `Not measured:` / `Not profiled:` lines for every
   platform or scenario not measured (never speculate)

## `Tested:` block (both tiers)
- List every executed unit suite, corpus (Flutter targeted/full with test counts, google3) and
  trybot (ASAN, Mac, Windows, Android) in terms an external reviewer understands; never local
  skill names
- Each entry names where it ran (OS/arch, build type, compiler, font/Unicode backend, or trybot)
  and the block ends with `Not tested:`
- google3 wording: `google3 SkParagraph clients (<N> test targets): no regressions`. Keep out
  only `//...` paths, `cl/` numbers, sponge links and target names; never write
  `Google-internal clients`
- Keep it current: corpora and trybots often finish after upload. Before the final summary,
  update from the latest `compare.md` / `attestation.json` and tryjob triage (`git commit --amend`,
  then `luci-auth context -- git cl upload -f --commit-description=+`; a message-only patchset is
  `NO_CODE_CHANGE` and keeps tryjob results). `canary_advisor.py triage` prints
  `TESTED BLOCK: STALE` until every green trybot is listed and must print `TESTED BLOCK: OK`
  before the final summary. The final chat summary carries the same per-suite environment

## `WebParagraph` / `flutter/flutter` PRs: `Reviewer & AI Audit Context`
Include a compact 4-bullet **`Reviewer & AI Audit Context`** block in the PR description:
- `Core Logic Hunks` with `prod vs test` line counts
- `C++ SkParagraph Parity Anchor` citing the exact `.cpp:line` formula/branch
- `Callers & Precondition Proofs` from `keeper_skeptic` / `finding_ledger.py` explaining omitted
  guards or unchanged sibling sites like `BoxHeightStyle.strut`
- `Cross-Phase Consumer Check` for `Placeholders` / `Backgrounds`

## Trailer & Final Mechanical Audit
Exactly one `Assisted-by:` line, the latest model, after an empty line and right before
`Change-Id:` (`python3 ~/.gemini/config/hooks/chat_lifecycle.py model --trailer`; `Assisted-by: AI`
if unknown). When amending, replace older lines and propagate the description to Gerrit
(`--commit-description=+`) or GitHub (`gh pr edit --body`).

Before `git cl upload` or `gh pr create`, verify the commit message with:
```bash
python3 ~/.gemini/config/skills/code-comments/scripts/audit_comments.py \
  --commit-msg <(git log -1 --format=%B HEAD)
```
to confirm zero internal leaks, a valid 40-hex `Change-Id`, and **exactly one** `Assisted-by:`
trailer (only the latest model, never multiple).
