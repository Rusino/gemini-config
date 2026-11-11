---
trigger: always_on
description: "When editing source code or tests, writing code comments, commit messages, CL/PR descriptions, or code review replies."
---

# Code Style

## Comments
- English, 1–4 lines. No trailing period outside Dart; `.dart` `//` and `///`
  comments end with a period (Effective Dart)
- Comment non-obvious invariants, call-order constraints, negative invariants (why something must
  not happen here), and API quirks, even on 1-line edits; skip self-explanatory guards, renames,
  and phase extractions. Why, not what: never restate code or write `// Removed...` / `// Fixed...`
- Add a contract comment (Python docstring: behavior, inputs, outputs, side effects; name each
  responsibility if several) on new/changed `.h`/`.hpp` declarations, public Python functions, and
  helpers whose preconditions, ownership, index/unit domain, or side effects are not obvious from
  name and signature. Put focused comments above non-trivial branches, never an umbrella block
  atop a method or `switch`
- Every new test (`DEF_TEST`, `UNIX_ONLY_TEST`, `TEST`, `test(...)`) gets a 1–2 line header:
  which contract it checks and why that edge matters
- Rewrite or delete only comments you added; ask before touching pre-existing ones

## Code
- Every line <= 100 columns in every language (clang-format covers only C++)
- Strict YAGNI: no uncalled helpers, overloads, iterators or wrappers. State is a plain struct,
  not a class with accessors: `fMember` when it has methods, plain field names on methodless POD
  structs
- One screen per function or loop at one abstraction level: extract cohesive domain phases into
  narrow-signature helpers (one caller in a long loop satisfies YAGNI), and hold loop state in a
  struct instead of multi-line `[&]` lambdas. Pass the owning object or slice directly rather than
  a field-copying courier struct; no tiny pass-through wrappers
- Prefer early `return`/`continue` for preconditions, degenerate cases, and fast paths when
  nothing follows, placing guards above allocations/work they cancel and never repeating the
  inverted check below; keep `if`/`else` for short symmetric branches (LTR vs RTL) or shared
  trailing work
- Platform or backend code is picked by the build, as in `skunicode` and `skshaper`: its own
  source list in gni/Bazel, `sources +=` under the gn flag. Never wrap a whole `.h`/`.cpp` in
  `#if`; `#if` stays only at call sites in shared code that choose a backend
- Callbacks: for synchronous calls that do not outlive the call, prefer a function pointer
  (stateless) or a template callable (`SkUnicode::forEachCodepoint` style, no why-comment in
  `.cpp`; avoid `.h` templates that pull in heavy headers); reserve `std::function` for stored
  callbacks. Non-callable `.cpp` templates and other non-obvious constructs need a why-comment
- Assert invariants (`SkASSERT` in Skia, `CHECK`/`DCHECK` in Chromium, `assert` in Dart/TS), with
  a why-comment when non-obvious; no defensive `if (!ptr) return;` on internal paths or branches
  for states no constructor or builder produces (check the producer and sibling path first)
- Root-cause fixes only: no special-case branch for one failing input; when special cases
  accumulate, reshape the data structure or lookup table instead of chaining `if`/`else`, keeping
  layout, hit-testing, caret, selection and rendering consistent
- Lookup tables (`ES.28`, `SL.con.1`): in C++, use `std::array<T, N>`: `constexpr` when
  compile-time computable, else a function-local `static const auto kTable = [] {
  std::array<T, N> a{}; ... return a; }();` (`SkNoDestructor` only when `T` is not trivially
  destructible) — never C arrays, `const T(*)[N]`, or mutable `static` init flags. Never mark
  returned/moved owning locals `const` or put top-level `const` on by-value parameter declarations
- Buffers and slices (`I.13`): in new internal C++ APIs, pass `SkSpan<const T>` (`SkSpan<T>` when
  mutated) and `std::string_view` by value instead of `(ptr, count)`, decaying arrays, or
  `const std::string&`; keep `(ptr, count)` only at C/CF/CoreText/ICU/HarfBuzz calls and existing
  public/virtual ABI (`Frozen ABI`)
- Resource handles (`R.1`): bind every owned C, CoreFoundation (`Create`/`Copy`), ICU, FreeType,
  and HarfBuzz handle to its RAII owner on acquisition (`sk_sp<T>`, `SkUniqueCFRef<CFRef>`,
  `sk_cfp<CFRef>`, or `std::unique_ptr<Pointee, SkFunctionObject<DestroyFn>>` with `Pointee` =
  underlying struct, not pointer typedef); never pair manual release calls across early returns
- Values and flags (`F.16`, `F.20`, `I.4`, `Enum.3`): pass small trivially-copyable values
  (`SkSpan`, `std::string_view`, scalars, ranges) by value; return single values, small structs,
  or `std::optional<T>` by value from new internal C++ functions, using `T*` out-params (never
  non-const `T&`) only for reused hot-path buffers or existing Skia APIs. Use `enum class` or
  named structs instead of adjacent `bool`/`int` flags
- Tests: no narrative comment before every assert; test domain edges, not C++ mechanics
- Tests check behavior through the public API (`Paragraph`, `TextLine` visitors); internal state
  (`runs()`, `clusters()`, run counts) only with a why-comment when that structure is the
  contract, never as a precondition for reading glyphs

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
