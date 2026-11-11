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
- Keep each function and loop body readable on one screen at one level of abstraction: extract
  a cohesive domain phase (e.g. building a font cascade, resolving a run's font, computing
  advances, or populating an output run) into a file-local function or private method with a
  narrow signature (one caller in a long function or loop satisfies Strict YAGNI), and group
  loop-carried state into a small struct instead of named multi-line closures (e.g. C++ `[&]`
  lambdas); never slice an algorithm into tiny pass-through wrappers with long parameter lists
- Prefer early `return` or `continue` for preconditions, degenerate cases, and short fast paths
  when nothing follows the `if`/`else`, keeping the main body unindented; keep `if`/`else` for
  short symmetric peer branches (e.g. LTR vs RTL) or when shared work runs after both branches
- Platform or backend code is picked by the build, as in `skunicode` and `skshaper`: its own
  source list in gni/Bazel, `sources +=` under the gn flag. Never wrap a whole `.h`/`.cpp` in
  `#if`; `#if` stays only at call sites in shared code that choose a backend
- Callbacks are a plain function parameter (`std::function` or a function pointer) by default.
  A template parameter needs a why-comment with a measured cost or a precedent
  (`SkUnicode::forEachCodepoint`); same for any other non-obvious language construct
- Assert invariants at boundaries (`SkASSERT` in Skia, `CHECK`/`DCHECK` in Chromium, `assert` in
  Dart/TS), with a why-comment when the guarantee is non-obvious; no defensive `if (!ptr) return;`
  along internal call chains or branches for container/field states that no constructor or builder
  produces (check the producer and sibling path first)
- Root-cause fixes only: no special-case branch for the failing input; keep layout, hit-testing,
  caret, selection and rendering consistent
- Lookup tables (`ES.28`, `SL.con.1`): in C++, build fixed lookup tables as `std::array<T, N>`:
  `constexpr` when compile-time computable, otherwise a function-local `static const`
  initialized once by a value-returning lambda (`static const auto kTable = [] {
  std::array<T, N> a{}; ... return a; }();`, using `SkNoDestructor` only when `T` is not
  trivially destructible) — never C arrays, `const T(*)[N]`, or mutable `static` init flags.
  Do not mark owning locals `const` when returned or moved, or put top-level `const` on
  by-value parameter declarations
- Buffers and slices (`I.13`): across new internal C++ interfaces, pass contiguous buffers
  and string slices by value as `SkSpan<const T>` (`SkSpan<T>` when mutated) and
  `std::string_view` instead of `(ptr, count)` pairs, decaying C arrays, or
  `const std::string&`; keep `(ptr, count)` only at C, CoreFoundation, CoreText, ICU, and
  HarfBuzz call sites and in existing public or virtual signatures (`Frozen ABI`)
- Resource handles (`R.1`): bind every owned C, CoreFoundation (`Create`/`Copy`), ICU,
  FreeType, and HarfBuzz handle to its RAII owner on the line that acquires it (`sk_sp<T>`,
  `SkUniqueCFRef<CFRef>` or `sk_cfp<CFRef>` for CF pointer typedefs, or
  `std::unique_ptr<Pointee, SkFunctionObject<DestroyFn>>` where `Pointee` is the underlying
  struct, not a pointer typedef); never pair manual release or destroy calls across early
  returns
- Values and flags (`F.16`, `F.20`, `I.4`, `Enum.3`): pass small trivially-copyable values
  (`SkSpan`, `std::string_view`, scalars, ranges) by value, and return single values, small
  structs, or `std::optional<T>` by value from new internal C++ functions; use `T*`
  out-parameters (never non-const `T&`) only for reused hot-path buffers or existing Skia
  APIs. Replace adjacent `bool` or raw `int` flags with `enum class` or named structs
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
