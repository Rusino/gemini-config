---
trigger: always_on
description: "Skia-style C++ minimalism and suppression of LLM coding reflexes."
---

# Code Style & Anti-Slop Rules

## 1. Comments (No Trailing Periods, No Noise)
- **Punctuation & length**: Keep comments to 1–4 lines in English; periods (`.`) are allowed between sentences inside a multi-line block, but **never** at the end of a single-line comment or the end of a comment block
- **Never restate syntax**: Do not write comments that describe *what* the next line of code does (e.g., `// Populate the cache`, `// Check for null`, `// Increment counter`)
- **No tutorial markers**: Never use numbered step comments (`// 1. Setup`, `// 2. Execute`) or markdown emphasis (`*word*`, `--`) in code comments
- **Why over What (Timeless Invariants)**: Write comments only to state non-obvious domain invariants, contracts, hardware/API quirks, or why a check/call must not happen here (never diff-style `// Removed...` or `// Fixed...`). Even a 1-line non-trivial addition, modification, or deletion must get a comment explaining why
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

## 6. Human Commit Messages
- Write concise, direct prose (1–3 sentences explaining the problem and the fix)
- Never generate LLM-style bulleted lists of imperative verbs (`- Add...`, `- Refactor...`, `- Update...`) in commit descriptions unless explicitly asked
