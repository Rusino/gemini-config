# Phase 4 Test Liveness & Per-Repo Verification Gate Matrix (`amnesia-precommit`)

Read this file in Phase 4 Steps 0–1 to run the Test Liveness Gate and determine which test suites, corpora, benchmarks, and trybots are required for the modified paths before `luci-auth context -- git cl upload`, `gh pr create`, or `git push`.

## 0. Test Liveness Gate Details (`mutation_check.py` & `verify_test_liveness.py`)

Run for every new or modified test in the diff, including ones a previous chat or state file reports as done; read the test body yourself. This gates the run itself; the failing-contract-test line in `outgoing_text_verification.md` and `cpp_systems_invariants.md` #8 (Contract Honesty) still govern what the test asserts.

- **Fails under one mutation of the covered production code**: choosing the mutation is your judgement (break the exact behavior the test claims to check); executing it is mechanical and must go through the script, never by hand:
  ```bash
  python3 ~/.gemini/config/skills/amnesia-precommit/scripts/mutation_check.py \
    --file <prod file> --find '<exact line>' --replace '<broken line>' \
    --build '<build cmd>' --test '<runner cmd for <Name>>' \
    --test-file tests/<File>.cpp --test-name <Name>
  ```
  `PASS` requires: green before, red with the mutation, source restored byte-for-byte with an unchanged `git diff` (restore runs in `finally`, also on Ctrl-C/SIGTERM), green again after a rebuild. A mutation that does not compile, or a test that stays green, is `FAIL`. Each run writes an attestation (mutation diff, both log tails, file SHA-256s) to `$(git rev-parse --git-path jetski/mutation_attestations)/<Name>.json`.
- **Gate over the whole diff (built, run by name, non-vacuous, attested)**:
  ```bash
  <runner> --verbose > /tmp/run.log 2>&1   # Skia: out/<cfg>/dm --src tests --match <Names> --verbose
  python3 ~/.gemini/config/skills/amnesia-precommit/scripts/verify_test_liveness.py \
    --repo . --diff <base rev> --log /tmp/run.log
  ```
  `--diff` finds every test definition added or touched since the base. It fails when the file is in no build list (`gn/*.gni`, `BUILD*`, CMake), the log does not name the test (`N tests done` is no proof), a loop over a runtime collection has no preceding non-empty assert (e.g. `REPORTER_ASSERT(r, mgr->countFamilies() > 0)`), the body reads device paths (`/system/...`, `SkFontMgr_New_Android(nullptr, ...)`) absent on host, the body pins font-backend-specific glyph bounds/metrics/advances to hardcoded numbers without a backend guard (`skia_headers_and_font_tests.md`; Gerrit 1404196 -> 1411716), the log has warnings such as `could not be opened`, or the test has no `PASS` mutation attestation for its current contents. Root-cause each warning first; only then pass `--explained-warning REGEX`, never `grep -v` it away.
- **Benchmarks**: before quoting numbers, prove the benchmark loads real data (assert or log a non-zero input count); a timing of an empty environment is not a result.
- **Differential Equivalence Check (Fast Paths, Lookup Tables & Caches)**: When the diff replaces an algorithm with a fast path, table lookup, or cache, verify byte-for-byte / element-for-element equivalence between the old and new implementations across a generated boundary corpus (e.g. all `0x110000` Unicode code points, lone surrogates, mixed scripts) and record the corpus size in the commit message.

## 1–6. Per-Repo Verification Gates

1. **Skia (`SkParagraph` / `SkUnicode` / `SkShaper`, Enforced by `CORPUS-CHECK GUARD`)**:
   - Build and run native Skia `dm --match SkParagraph` (`skia-test-paragraph`).
   - Whenever **executable production code** (excluding comment-only edits, `.md` docs, summaries, or commit messages) under `modules/skparagraph/{src,include}/`, `modules/skunicode/{src,include}/`, or `modules/skshaper/{src,include}/` is modified, you **must** also run `flutter-test-paragraph-engine` (`~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py`) before `luci-auth context -- git cl upload`, `gh pr create`, or `git push` so Flutter framework layout, intrinsic width, and widget consumers (including `dropdown_menu_test.dart`) are verified with zero regressions.
   - For changes under `modules/skparagraph/` or `modules/skunicode/`, also run the Flutter web CanvasKit suite `felt test --suite chrome-dart2js-canvaskit-canvaskit` (`engine/src/flutter/lib/web_ui`, CanvasKit rebuilt from the patched Skia submodule) on baseline and patched Skia: `canvaskit_api_test.dart` pins SkParagraph metrics such as `LineMetrics.left` that the native and framework corpora never check (incident: Gerrit 1418376 broke the Flutter roll and was reverted in Gerrit 1420816).

2. **SkParagraph Perf Gate (`txt_benchmarks` A/B, Enforced by `PERF-CHECK GUARD`)**:
   - Whenever **executable C/C++ code** under `modules/{skparagraph,skunicode,skshaper}/{src,include}/`, or `Sk*` files under `include/core/`, `include/private/base/`, `src/base/`, or `src/core/` changes, you **must** run `python3 ~/.gemini/config/skills/flutter-engine-perf-compare/scripts/skia_patch_ab.py --skia-repo <repo> --out /tmp/skia_patch_ab` before `luci-auth context -- git cl upload` or `git push` (`flutter-engine-perf-compare` skill).
   - `UNAFFECTED`, `NEUTRAL`, and `IMPROVEMENT` pass; `REGRESSION` (exit 2) blocks upload until fixed or the user explicitly accepts it.
   - Put the `report.md` table row(s) into the CL `Tested:` block. Incident: Skia `daf82dad48` (`SkASSERT_RELEASE` in the `SkSpan` ctor, `include/core/`) slowed `LongLayout` by 21.6% and no gate noticed.

3. **Google3 Paragraph/Unicode A/B Corpus (Linux Cloudtop Gate, Replaces `Canary-G3`)**:
   - On Linux Cloudtop (`/google/src/cloud/` available), whenever **executable production code** or `BUILD.bazel` under `modules/skparagraph/{src,include}/` or `modules/skunicode/{src,include}/` changes public headers, `FontCollection` / caching / lifetime contracts, `BUILD.bazel`, or layout/metrics behavior visible to `google3` callers, run `google3-test-paragraph` fast tier (`~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py --out /tmp/g3_paragraph_tests --create-citc baseline=G3_HEAD current=WORKTREE`, ~10–13 min per uncached leg with cached `G3_HEAD` baseline across 1,139 fast targets: 1,137 transitive `<=600s` tests + 2 pinned `skia_build_test*` gates, including 993 runnable tests `Canary-G3` never runs; run `--tier slow` separately only when broad rendering/timeout behavior is affected).

4. **Flutter Engine / WebParagraph**:
   - Whenever **executable production code** under `lib/web_ui/lib/` is modified, you **must** run `webparagraph-corpus` (`~/.gemini/config/skills/webparagraph-corpus/scripts/run_webparagraph_corpus.py`, both Part 1 `felt` and Part 2 `packages/flutter`) before `luci-auth context -- git cl upload`, `gh pr create`, or `git push`.

5. **Chromium OOP-R / Fontations / PoC Gate**:
   - Whenever a Skia diff modifies `include/private/chromium/`, `src/text/gpu/SkChromeRemoteGlyphCache*`, `Fontations` (`src/ports/*fontations*`, `rust/`), `src/ports/SkFontMgr_fontconfig*`, or fixes a Chromium bug (`crbug`) with an HTML/font reproducer, apply the diff to `/usr/local/google/home/jlavrova/chromium/src/third_party/skia`, rebuild `autoninja -C /usr/local/google/home/jlavrova/chromium/src/out/Default skia` (`libskia.so`), run the relevant Chromium test (`blink_platform_unittests`, `cc_unittests`, or `chrome` on the reproducer), and restore the checkout with `git -C /usr/local/google/home/jlavrova/chromium/src/third_party/skia checkout -- .`.

6. **Skia Canary, ASAN & NativeFonts Trybot Recommendation**:
   - For Skia diffs with executable changes, run `skia-canary-advisor`.
   - You MUST automatically trigger the ASAN bot.
   - In addition, when the diff adds or modifies C++ tests using `TestFontMgr()`, `makeFromData()`, or font metrics, `canary_advisor.py` automatically selects `Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations` (`[ALWAYS_RUN]`) so `--nativeFonts` (CoreText) + `--fontations` is verified before submit (`skia_headers_and_font_tests.md`; Gerrit 1404196 -> 1411716).
   - Present canaries to the user for approval.
