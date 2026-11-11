---
trigger: always_on
description: "C++ systems invariants for all Skia and engine code."
---

# C++ Systems Invariants

1. **Domain & representation separation:** separate stored representation from transient layout
   types; never mix index or coordinate domains (`UTF-8` bytes, `UTF-16` units, codepoints,
   `grapheme clusters`, glyph indices, logical vs visual BiDi, `advance` vs `ink bounds`) via `+ 1`
   or `auto`. Use signed `int` + `SkASSERT` for counts with `SkToInt` / `SkTFitsIn` at `size_t` /
   `float` / 64-bit boundaries
2. **Fail-fast dimensional honesty:** validate untrusted external inputs (public API, font/text
   data, C/ICU/CoreText responses) at the entry boundary and return cleanly on failure; enforce
   internal call-chain preconditions with `SkASSERT`, not defensive `if (!ptr) return;`
3. **Zero-allocation hot paths:** no `new`, `malloc`, `std::string` copies, growing vectors, or
   `std::map` / `std::unordered_map` in layout and rendering loops
4. **Loop guards:** every loop provably terminates; data-driven loops over clusters, runs, or
   iterators must enforce strictly positive forward progress each step
5. **No warning suppression:** never `#pragma ... diagnostic ignored`; fix the source
6. **`// KEEPER-DEBT:`** marks intentional temporary debt, with the reason and removal condition
7. **Frozen ABI:** never change existing public struct layouts, vtable order or exported symbols
   in ways that break backward compatibility
8. **Contract Honesty:** never invert, negate or loosen asserts to make tests pass against unfixed
   code; tests assert the required contract, not accidental behavior
