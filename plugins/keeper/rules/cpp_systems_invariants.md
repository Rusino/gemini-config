---
trigger: always_on
description: "C++ systems invariants for all Skia and engine code."
---

# C++ Systems Invariants

1. **Type bifurcation:** keep types for physical representation apart from logic or temporary types
2. **Fail-fast dimensional honesty:** validate dimensions and bounds at the earliest point; fail
   fast instead of corrupting state
3. **Zero-allocation hot paths:** no `new`, `malloc`, `std::string` copies or growing vectors in
   layout and rendering loops
4. **Loop guards:** every loop provably terminates
5. **No warning suppression:** never `#pragma ... diagnostic ignored`; fix the source
6. **`// KEEPER-DEBT:`** marks intentional temporary debt, with the reason and removal condition
7. **Frozen ABI:** never change existing public struct layouts, vtable order or exported symbols
   in ways that break backward compatibility
8. **Contract Honesty:** never invert, negate or loosen asserts to make tests pass against unfixed
   code; tests assert the required contract, not accidental behavior
9. Public headers and font-backed tests: see `skia_headers_and_font_tests.md`
