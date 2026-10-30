---
description: C++ systems programming invariants (RAII, zero-allocation hot paths, type bifurcation, loop guards, ABI stability).
trigger: always_on
---
# C++ Systems Invariants

1. **Type Bifurcation:** Ensure clear separation between types meant for physical representation versus those meant for logic or temporary structures.
2. **Fail-fast Dimensional Honesty:** Dimensions and boundaries must be strictly validated at the earliest possible point. Fail fast rather than silently corrupting state.
3. **Zero-allocation Hot Paths:** Hot paths (e.g., layout, rendering loops) must absolutely not perform dynamic memory allocations (no `new`, `malloc`, or implicit allocations like `std::string` copies or growing `std::vector`s). 
4. **Loop Guards:** All loops MUST have guaranteed termination. Use loop guards or static analysis invariants to prove termination.
5. **No Compiler Warnings Suppression:** Do not suppress compiler warnings (e.g. via `#pragma GCC diagnostic ignored`). Warnings are errors and must be fixed at the source.
6. **KEEPER-DEBT Markers:** Use explicit `// KEEPER-DEBT:` comments when introducing intentional temporary technical debt, specifying why and a condition for removal.
7. **Frozen ABI Stability:** For exposed APIs and structures, the Application Binary Interface (ABI) is frozen. Changes must not alter existing structure layouts, virtual table orders, or exported symbols in ways that break backwards compatibility.
8. **Contract Honesty:** Never invert, negate, or loosen test assertions to make tests pass against unfixed code. Tests must assert required contract behavior, never character-pin accidental failures.
9. **Public Headers Compile Clean Under Clang Header Warnings:** Skia compiles every public header (`include/`, `modules/*/include/`) via `tools/public_headers_warnings_check.cpp` with config `warnings_for_public_headers` (`gn/skia/BUILD.gn`): `-Wunused-parameter -Wsign-conversion -Wshorten-64-to-32` plus `-Werror`, clang only. Any inline body in a public header (virtual defaults, stubs, forwarding overloads), including code suggested in companion CLs and review comments, must leave unused parameters unnamed (`SkScalar /*firstLineIndent*/`, as in `include/core/SkStream.h`) and avoid implicit sign or 64->32 conversions. A local `out/Debug` with empty `args.gn` builds with GCC (`is_clang=false`) and skips these flags, so a green local `dm` proves nothing here. Before proposing or uploading a public-header change, run from the Skia checkout root: `printf '#include "<header>"\n' > /tmp/ph_check.cpp && ~/.gemini/jetski/scratch/clang_linux/bin/clang++ -std=c++20 -fsyntax-only -I. -Werror -Wunused-parameter -Wsign-conversion -Wshorten-64-to-32 /tmp/ph_check.cpp`. Incident: cl/1409697 PS4 broke `Build-Ubuntu24.04-Clang-x86_64-Debug-FreeType_ASAN` with `unused parameter 'firstLineIndent'` in `Paragraph.h`.
