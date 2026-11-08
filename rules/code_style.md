---
trigger: always_on
description: "When editing source code or tests, writing code comments, commit messages, CL/PR descriptions, or code review replies."
---

# Code Style

## Comments
- English, 1–4 lines. No trailing period at the end of a comment outside Dart; in `.dart` files
  every `//` and `///` comment ends with a period (Effective Dart)
- Every non-trivial change, even one line, gets a why-comment. Why, not what: state invariants,
  contracts, API quirks, or why something must not happen here.
  Never restate the next line, never write `// Removed...` / `// Fixed...` diff-style notes
- Every new or changed function, method or template, private ones too, gets a contract comment
  above it (a docstring in Python): what it does, inputs, outputs, side effects. A function that
  does several things names each of them. Focused comments go above non-trivial branches, never
  one umbrella block at the top of a method or `switch`
- Every new test (`DEF_TEST`, `UNIX_ONLY_TEST`, `TEST`, `test(...)`) gets a 1–2 line header:
  which contract it checks and why that edge matters
- No numbered step comments, no markdown emphasis in comments
- Rewrite or delete only comments you added; ask before touching pre-existing ones

## Code
- Strict YAGNI: no helpers, overloads, iterators or wrappers without a caller in this change.
  Private state is a plain struct with `fMember` fields
- Keep each function and loop body at one level of abstraction: extract a self-contained domain
  step with its own local state (e.g. building a font cascade, feature list, or variation map) into
  a helper that returns a value; never slice an algorithm arbitrarily or add trivial wrappers
- Platform or backend code is picked by the build, as in `skunicode` and `skshaper`: its own
  source list in gni/Bazel, `sources +=` under the gn flag. Never wrap a whole `.h`/`.cpp` in
  `#if`; `#if` stays only at call sites in shared code that choose a backend
- Callbacks are a plain function parameter (`std::function` or a function pointer) by default.
  A template parameter needs a why-comment with a measured cost or a precedent
  (`SkUnicode::forEachCodepoint`); same for any other non-obvious language construct
- `SkASSERT` invariants at boundaries; no defensive `if (!ptr) return;` along internal call chains
- Root-cause fixes only: no special-case branch for the failing input; keep layout, hit-testing,
  caret, selection and rendering consistent
- Tests: no narrative comment before every assert; test domain edges, not C++ mechanics
- Tests check behavior through the public API (`Paragraph`, `TextLine` visitors); internal state
  (`runs()`, `clusters()`, run counts) only with a why-comment when that structure is the
  contract, never as a precondition for reading glyphs
- New features start from a contract catalog (`rules/contract_first.md`)
- Hard limit of 100 columns for every line (`clang-format -style=file`)

## Commit messages, CL/PR descriptions, review replies
- English only; plain declarative sentences, present tense, plain words (`keep`, `get`, `run`,
  `same as before`), no `utilize`, `facilitate`,
  `subsequently`, `consequently`, `mitigate`, `invocations`, `redundantly`, `leverage`,
  `comprehensive`, `seamlessly`
- No bulleted lists of imperative verbs that restate the diff
- Format (Tier 1 / Tier 2, `Tested:` block, google3 corpus wording): read
  `skills/amnesia-precommit/references/commit_message.md` before writing or amending a description
- `[SkoreText] ` subject prefix for SkoreText CLs and CLs from the macOS `/Users/jlavrova/Sources`
  workspace (the hook checks only the project name); never on other projects' CLs or on
  companion review CLs on external authors' changes
- When a reviewer suggests an alternative, measure all four combinations (baseline, A, B, A+B)
  and put the numbers in the reply
- Gerrit drafts: `scripts/gerrit_draft.py save` dedups in place by `(path, line, in_reply_to)`;
  `"Done"` drafts on her own addressed threads come from `finalize_review.py stage-thread-drafts`
- Canary-G3 triage: see `skills/skia-canary-advisor/SKILL.md`
