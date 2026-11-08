---
trigger: model_decision
description: "When editing, reviewing, or suggesting code (including in companion CLs and review comments) for Skia public headers (include/, modules/*/include/) or tests that check font-derived values (glyph sizes, bounds, advances, metrics, glyph IDs)."
---

# Skia Public Headers and Font-Backed Tests

## Public headers compile clean under clang header warnings
Skia compiles every public header (`include/`, `modules/*/include/`) through
`tools/public_headers_warnings_check.cpp` with `-Wunused-parameter -Wsign-conversion
-Wshorten-64-to-32 -Werror` (clang only, config `warnings_for_public_headers` in
`gn/skia/BUILD.gn`). Inline bodies in public headers (virtual defaults, stubs, forwarding
overloads), including code suggested in reviews, leave unused parameters unnamed
(`SkScalar /*firstLineIndent*/`) and avoid implicit sign or 64→32 conversions. A local
`out/Debug` with empty `args.gn` builds with GCC and skips these flags. Before proposing or
uploading a header change, run from the Skia root:

```
printf '#include "<header>"\n' > /tmp/ph_check.cpp && \
~/.gemini/jetski/scratch/clang_linux/bin/clang++ -std=c++20 -fsyntax-only -I. -Werror \
  -Wunused-parameter -Wsign-conversion -Wshorten-64-to-32 /tmp/ph_check.cpp
```

## No backend-specific font metric pinning in cross-platform tests
Never compare font-derived outputs (`glyph.width()`, `getBounds()`, advances, `SkFontMetrics`,
mask sizes, system-font `SkGlyphID`s) with hardcoded non-zero numbers. Allowed: (a) backend-free
invariants or the exact predicate under test, (b) values computed by the same backend in the same
test, (c) explicit backend or platform guards with a comment. `--fontations` only switches
`ToolUtils::TestFontScanner()`; `--nativeFonts` makes `ToolUtils::TestFontMgr()` return the platform
manager (CoreText on Mac `NativeFonts_Fontations`, whose `generateMetrics` outsets bounds by 1).
Before uploading a font-backed test, trigger trybots for those backends, specifically Mac
`NativeFonts` and `NativeFonts_Fontations`.
