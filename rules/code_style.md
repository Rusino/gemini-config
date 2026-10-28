---
trigger: always_on
description: "Skia-style C++ minimalism and suppression of LLM coding reflexes."
---

# Code Style & Anti-Slop Rules

## 1. Comments (No Trailing Periods outside Dart, No Noise)
- **Punctuation & length**: Keep comments to 1–4 lines in English; periods (`.`) are allowed between sentences inside a multi-line block, but **never** at the end of a single-line comment or the end of a comment block
- **Dart exception**: In `.dart` files (Flutter framework, engine `web_ui`, tooling, tests) every `//` and `///` comment **must** end with a period (Effective Dart, enforced by flutter/flutter review bots). This overrides the no-trailing-period rule above for Dart only; all other comment rules still apply
- **Never restate syntax**: Do not write comments that describe *what* the next line of code does (e.g., `// Populate the cache`, `// Check for null`, `// Increment counter`)
- **No tutorial markers**: Never use numbered step comments (`// 1. Setup`, `// 2. Execute`) or markdown emphasis (`*word*`, `--`) in code comments
- **Why over What (Timeless Invariants)**: Write comments only to state non-obvious domain invariants, contracts, hardware/API quirks, or why a check/call must not happen here (never diff-style `// Removed...` or `// Fixed...`). Even a 1-line non-trivial addition, modification, or deletion must get a comment explaining why
- **Method Header vs. Targeted Branch Placement (No Umbrella Blocks)**: Above a method signature, write a concise comment describing the method's contract/purpose without implementation details; inside the body, place short, focused comments directly above each non-trivial branch, condition, loop, or field. Never use a multi-sentence summary block at the top of a method, hunk, or `switch` as a substitute for commenting the branches inside
- **Comment ownership**: Only rewrite or delete comments that you (the AI) added and explicitly remember adding; always ask before modifying or removing any pre-existing or user-written comment

## 2. Zero Speculative API Surface (Strict YAGNI)
- **No uncalled methods**: Never add helper methods, const/non-const overloads, `begin()`/`end()` iterators, `empty()`/`size()` wrappers, or `std::ranges` `static_assert` checks unless they have a direct caller in the current change
- **Data is just data**: For private/internal state in Skia style, use plain structs with `fMember` fields rather than classes with trivial getters and setters

## 3. Contracts (`SkASSERT`) Over Defensive Paranoia
- **Assert invariants, don't mask bugs**: Validate preconditions at API boundaries or via `SkASSERT`
- **No redundant null/bounds checks**: Never scatter defensive `if (!ptr) return;` checks across internal call chains where the invariant is already guaranteed by the caller or data model

## 4. Root-Cause Fixes Only (No Symptomatic Branching)
- **No test-fitting `if`s**: When fixing a bug, never append a special-case branch tailored to the failing input
- **Cross-pipeline consistency**: Trace the bug to the broken invariant in the data model so all consumers (layout, hit-testing, caret geometry, selection, rendering) remain consistent

## 5. Test Minimalism
- **No narrative headers**: Do not put full-sentence comments before every `REPORTER_ASSERT` block in unit tests
- **Test domain edges, not C++ mechanics**: Do not write exhaustive boilerplate tests for trivial language mechanics (e.g., testing move-construction + move-assignment with repeated `NOLINT` comments); focus tests on domain invariants and boundary conditions

## 6. Human Commit Messages & Code Review Surface
- **English-only review surface**: Everything that enters code review (commit messages, CL/PR descriptions, top comments, inline comments, review replies) must strictly be written in English
- **AI-assisted CL attribution**: Any CL, PR, or commit prepared with AI assistance (with user direction/participation) must include an `Assisted-by:` trailer in the commit summary footer, separated from the body by an empty line and placed right before `Change-Id:`:
  ```gitcommit
  Problem description and fix explanation.

  Assisted-by: <Model Name>
  Change-Id: I...
  ```
  Run `python3 ~/.gemini/config/hooks/chat_lifecycle.py model --trailer` to obtain the trailer (e.g. `Assisted-by: Gemini 3.8 Flash (High)` or `Assisted-by: Gemini 3.8 Pro`). If the model cannot be determined, use `Assisted-by: AI`
- **Two-Tier Commit / PR Description Scaling**:
  - **Tier 1 (Simple / localized bug fixes & small refactors)**: Write concise, direct prose (1–3 sentences explaining the problem and the fix), plus 1 explicit line stating how the new test fails without the fix (exact assertion, call count, or crash)
  - **Tier 2 (Algorithmic, caching, concurrency, or performance CLs/PRs)**: Write a structured engineering specification containing:
    1. **Problem & per-call cost**: Concrete mechanics and cost of the current code path
    2. **Equivalence proof**: Why the new path produces the exact same result as the old path (and the input generator corpus size tested)
    3. **`Behavior notes` / `Assumptions` / `Thread safety`**: Edge-case invariants, leaf-lock or lock-order proof, and bounded-cache memory math (`bytes/entry × capacity = max KB`)
    4. **Empirical tables**: Compact `Before | After` benchmark tables and capacity/threshold selection sweep tables (`no cache`, `cap1`, `cap2`, `cap3`, `no limit`)
    5. **Explicit pre-fix test failure & honest scope disclosures**: Exact failure of the new test before the fix (e.g., `Without the lock, the test crashes in 28 of 30 runs`) and explicit `Not tested:` / `Not profiled:` lines whenever a platform or scenario was not measured (never speculate)
- **Plain Technical English (UNIX / Bell Labs style)**: Write simple, declarative Subject-Verb-Object sentences in the present tense using plain words (`keep`, `give`, `get`, `make`, `run`, `call`, `has`, `same as before`, `Thus`, `Now`); never use pompous Latinate LLM vocabulary (`utilize`, `facilitate`, `subsequently`, `consequently`, `mitigate`, `invocations`, `redundantly`, `leverage`, `comprehensive`, `seamlessly`)
- **No LLM bulleted lists**: Never generate mechanical LLM-style bulleted lists of imperative verbs (`- Add...`, `- Refactor...`, `- Update...`) that restate the diff; structured `Behavior notes` / `Assumptions` / `Thread safety` bullets in Tier 2 specs are encouraged
- **2×2 Ablation Matrix in Code Review Replies**: When a reviewer suggests an alternative or combined optimization/design (`A` vs. `B`), measure all 4 combinations (`baseline`, `A alone`, `B alone`, `A + B`) and include the exact numbers in the draft review reply
- **Never publish review replies or resolve threads without asking**: Never publish review replies
  or comments to Gerrit or GitHub yourself, and never resolve/unresolve review threads (`resolveReviewThread`)
  unless the user explicitly asks you to. Only prepare / draft replies (either save as drafts on Gerrit or
  present them in chat for review); only the user publishes review submissions or resolves threads
- **External Author Canary-G3 Submit-Hold Protocol**: When approving an external author's Skia CL
  that requires Canary-G3 (which runs ~55 min and requires a Googler author or Googler
  `Code-Review+1` approval per `g3_canary.go:134`), do not vote CR+1 as a bare approval. Because
  Gerrit submit enforcement requires 2 approvals, another reviewer could submit the CL while
  Canary-G3 is still running. To prevent premature submit, the user publishes `Code-Review+1`
  together with an unresolved patchset-level comment (leaving `No-Unresolved-Comments`
  `UNSATISFIED`). The agent never votes or publishes comments itself: prepare a `/PATCHSET_LEVEL`
  draft (`unresolved: true`) for the user to publish:
  > The change looks good to me (Code-Review+1). I leave this comment unresolved on purpose:
  > it blocks the submit until the Canary-G3 try job passes. Canary-G3 builds and tests the
  > google3 clients of Skia with this change. Skia starts it for an external author only after a
  > Googler votes Code-Review+1, so I vote first and hold the submit with this comment. I will
  > resolve it when Canary-G3 is green, or report the failure here. Please do not resolve or
  > override it before that.
  Once the user publishes CR+1 with this hold comment, the agent immediately triggers `Canary-G3`.
  When `Canary-G3` passes, the user resolves the comment.
- **Canary-G3 Runtime & Triage Procedure**: Canary-G3 can take ~55 minutes. If it turns red,
  extract the roll CL number from swarming stdout (`Canary roll [ http://cl/N ]`) and triage:
  1. Run `cd ~/.gemini/jetski/scratch/skia_gardener && go run . -triage-google3 -cl <CL>`
  2. For each failing target:
     - Transitive Dependency Check: Never assume a target does not use Skia/SkParagraph based on
       local dir code search. Verify transitive paths from `/google/src/head/depot/google3`:
       `blaze query "somepath(<target>, //third_party/skia/HEAD/<path to changed file>)"`
     - Flakiness & HEAD Status: Check target status with
       `/google/bin/releases/gemini-agents-auto-repair-cli/autorepair target-status <target>`
     - Stack Trace Inspection: Inspect crash logs via
       `autorepair readonly investigate --sponge_id=<id> --test_target=<target>`
       to check whether Skia frames appear in the crash stack
- **No Auto-Submit or Automated CQ Votes**: Never vote `Auto-Submit+1`, `Commit-Queue+1` (dry run),
  or `Commit-Queue+2` on Gerrit CLs, and never auto-merge GitHub PRs, unless the user explicitly
  asks for it (Try jobs, such as ASAN or Canaries triggered via `git cl try`, are NOT CQ votes and CAN be triggered automatically when rules require it). Do not trigger full CI submission automatically

## 7. Strict Line Length (<= 100 Columns)
- **Hard 100-character limit**: Every source line, test line, and code comment must strictly stay
  within 100 characters (Skia column limit). Never commit or push any line exceeding 100
  characters; always reformat or wrap with `clang-format -style=file`
