---
name: skia-canary-advisor
description: >-
  Recommend and trigger Skia Canary try jobs and ASAN bots for a Skia CL based on the diff.
  Trigger from `amnesia-precommit` (own CLs) and `review-triage` (others' CLs).
  Agent ALWAYS triggers the ASAN bot. Agent triggers canaries automatically for external authors
  (Canary-G3 requires Googler author or Googler CR+1).
---

# Skia Canary & ASAN Advisor

The script checks client-visible diff surface and selects appropriate ASAN and Canary bots.

## Run

```bash
S=~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py
python3 $S advise --cl <change>      # Gerrit CL, own or under review
python3 $S advise --repo <skia>      # local branch vs origin/main
python3 $S triage --cl <change>      # triage tryjobs (ASAN + Canaries + font trybots)
python3 $S watch --cl <change> --save-draft  # watch tryjobs + triage on completion
python3 $S health                    # OK/DEAD per canary (real verdicts in last 45 days)
python3 $S stats                     # RAN/skipped per logged recommendation + follow rate
```

## Agent steps

1. Run `advise` once on the final patchset (before CR+1 or before LGTM).
2. Execute every `AGENT TODO` line with `code_search`. A hit outside `skparagraph`/`skunicode` test rdeps makes `Canary-G3` STRONG.
3. Present the result: STRONG items as "run", CONSIDER items as "only if rendering may change".
4. **Execution Policy (Agent Actions)**:
   - **7-Day `LOAS2` Auth Wrapper (`luci-auth context`)**: Always invoke `git cl try` via
     `luci-auth context -- git cl try ...` (printed directly by `canary_advisor.py`) so
     `depot_tools` selects `LuciContextAuthenticator` (talking to `luci-auth context` backed by
     `/usr/bin/sso-cred-helper`, 7-day `LOAS2`) instead of `SSOAuthenticator` (`git-remote-sso`,
     20-hour `CorpSSO`), and check for exit code `0` (never append `|| true`).
   - **ASAN**: If an ASAN bot is recommended, YOU (the agent) MUST execute the
     `luci-auth context -- git cl try` command provided under `AGENT ACTION` immediately, without
     asking the author or the user.
   - **SkParagraph / SkUnicode on Linux Cloudtop (`LOCAL google3-test-paragraph`)**: When `/google/src/cloud` is mounted, `canary_advisor.py` automatically replaces `Canary-G3` for `modules/skparagraph/` and `modules/skunicode/` paths with `LOCAL google3-test-paragraph` (~10–13 min, 1,139 fast targets including `skia_build_test`, cached `G3_HEAD` baseline, zero Piper CLs, and zero Gerrit submit hold). Run it before `CR+1` / upload instead of triggering `Canary-G3`. Lightweight `Canary-G3` remains active only for non-paragraph Skia paths (`modules/skshaper/`, `src/ports/`, core `src/`/`include/`, root `BUILD.bazel`) or when off Cloudtop.
     - **Auto-Slow-Tier Policy**: When you run `google3-test-paragraph` automatically as the final check before granting `CR+1` (or before `amnesia-precommit` upload), you MUST append `--tier all` to cover the 13 slow tests. During intermediate/WIP checks, use the default `--tier fast`.
   - **External Authors** (owner is not `@google.com`): YOU (the agent) MUST execute the
     `luci-auth context -- git cl try` command provided under `AGENT ACTION` immediately, without
     asking.
     - **Canary-G3 restriction (`g3_canary.go:134`)**: For non-paragraph Skia CLs that still need `Canary-G3`, `Canary-G3` requires a Googler author OR a Googler `Code-Review+1` approval. If the external CL has no Googler `CR+1` yet, `canary_advisor.py` excludes `Canary-G3` from `AGENT ACTION` and prints the manual command to run if needed after `CR+1` (do not place unresolved submit-hold comments).
     - **Stacked CLs**: the google3 try service (`chrome/skia/autoroller/try_service.py`)
       cherry-picks every unmerged parent of the CL too, and each one needs its own Googler
       `CR+1` (or a Googler owner and author). A `CR+1` on the reviewed CL alone gives
       `MISSING_APPROVAL` after about a minute; `triage` lists the parents that block it. Never
       vote `CR+1` on unreviewed parents just to unblock `Canary-G3`.
     - Other canaries (`Canary-Chromium`, `Canary-Android`) have no such restriction and are
       triggered immediately. `Canary-Flutter` is disabled (see below): run the full local
       Flutter corpus instead.
   - **Internal Authors / Own CLs**: Show the printed `Canary Command` for canaries and run it
     ONLY after the user explicitly approves. (For ASAN, you still run it automatically).
5. **Background Monitoring & Wakeup Protocol**:
   - Immediately after triggering `luci-auth context -- git cl try`, launch
     `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py watch --cl <CL> --save-draft`
     as a background command (`WaitMsBeforeAsync=500`, `NotificationTimeoutSeconds=5400`).
   - **On Red Canary (`UNRELATED_HEAD_BREAKAGE`)**: `watch --save-draft` automatically verifies
     `CheckTestsSkia` + `blaze query somepath` for all failing targets and saves a strictly
     sanitized English `/PATCHSET_LEVEL` draft on Gerrit (never leaking internal `google3` target
     paths or `cl/N` numbers to public Gerrit). When woken up, the agent shows the internal triage
     table in chat and confirms the draft is saved. (No Gerrit draft is created when Canary is
     green).
   - **Approval gate (`TRYJOBS:` line)**: `triage` (and `watch` on wakeup) ends with
     `TRYJOBS: ALL GREEN` or `TRYJOBS: NOT GREEN (<builders>)`, covering ASAN, canaries, and
     other trybots such as Mac `NativeFonts_Fontations` (with the dm `Failures:` block). Never
     give LGTM / `CR+1` / a CLEAN verdict before `ALL GREEN`; if the watcher was killed, run
     `triage` by hand. `stop_guard.py` (`[TRYJOB GATE GUARD]`) enforces this.
   - **On Red Canary (`CL_SUSPECTED_REGRESSION`)**: Report the failing target, `somepath` chain,
     and stack trace to the user and proceed to fix.
   - **On Red Canary / ASAN (`NOT_MERGEABLE`)**: Build failed at Task Scheduler startup because the
     CL has merge conflicts against `main`; ask the author to rebase (do not re-trigger on the
     conflicting patchset).
   - **On Red ASAN (`COMPILE_FAILURE` or `SANITIZER_OR_TEST_FAILURE`)**:
     - **External / Others' CLs (`review-triage`)**: Never modify the author's CL directly.
       Create or update the `%wip` **companion CL** per `companion-cl-review` (`PS1` = failing
       reproducer test if needed, `PS2` = fix passing ASAN), update the `/PATCHSET_LEVEL` draft
       on the parent CL (`unresolved: true`) with the ASAN trace and companion CL link, and
       notify the user.
     - **Own CLs (`amnesia-precommit` / Self-Review)**: Reproduce and fix in an isolated worktree,
       verify with a local ASAN build, upload the fix (or ask first if reviewers have already
       voted `CR+1`), re-trigger ASAN + `watch`, and report to the user.
6. A red canary is not automatically the CL's fault. Follow the **Canary-G3 Runtime & Triage
   Procedure** below before blaming the CL.

## Canary-G3 Runtime & Triage Procedure

- **Runtime**: `Canary-G3` can run for ~55 minutes. Do not assume timeout after 8–10 minutes.
- **Triage on Red Canary**:
  1. Extract the canary roll CL number from the swarming task stdout (`Canary roll [ http://cl/N ]`) or the Swarming UI task step.
  2. Run the automated google3 triage command:
     ```bash
     cd ~/.gemini/jetski/scratch/skia_gardener && go run . -triage-google3 -cl <g3 canary CL>
     ```
  3. Perform 3-step per-target verification for every failing target:
     - **(a) Transitive Dependency Path Check**: Never conclude a target does not use Skia/SkParagraph based on local directory searches (dependencies are often transitive, e.g. `map3d -> impress canvas -> skparagraph`). From `/google/src/head/depot/google3`, query the exact dependency path:
       ```bash
       blaze query "somepath(<target>, //third_party/skia/HEAD/<path to changed file>)"
       ```
     - **(b) Flakiness & HEAD Status Check**: Verify if the target is already broken at HEAD:
       ```bash
       /google/bin/releases/gemini-agents-auto-repair-cli/autorepair target-status <target>
       ```
     - **(c) Stack Trace Inspection**: Inspect the crash logs to check if Skia frames appear in the crash stack:
       ```bash
       autorepair readonly investigate --sponge_id=<id> --test_target=<target>
       ```

## ASAN Sanitizer & Font-Backend Trybot Selection

The script automatically selects one ASAN bot based on the diff:
- Text/Fonts C++ -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload`
- GPU (Graphite/Vulkan) -> `Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan`
- GPU (Metal) -> `Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal`
- General CPU -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN`
(Does not trigger for docs/Dart/TS/markdown-only CLs).

In addition, when a diff adds or modifies C++ tests using `TestFontMgr()`, `DefaultFontMgr()`,
`makeFromData()`, `MakeFromFile()`, `CreateTestTypeface()`, or font metrics, the script also selects
`Test-Mac14-Clang-MacMini8.1-CPU-AVX2-x86_64-Debug-All-NativeFonts_Fontations` (`[ALWAYS_RUN]`).
This ensures `--nativeFonts` (CoreText, where `SkScalerContext_Mac::generateMetrics` outsets bounds
by 1) and `--fontations` are exercised before submit, preventing font-backend-specific test
breakages on Mac trybots (`cpp_systems_invariants.md` #10; Gerrit 1404196 -> 1411716).

### Android `NativeFonts` Trybot Caveat (`--paragraph_fonts extra_fonts` Omitted)

In Skia's task generator (`infra/bots/gen_tasks_logic/dm_flags.go:1811-1817` and
`infra/bots/gen_tasks_logic/gen_tasks_logic.go:1737-1739`), `NativeFonts` bots mount the
`extra_fonts` CIPD package (`needsFontsForParagraphTests()`) and pass `--paragraph_fonts extra_fonts`
only when `!b.MatchOs("Android")`. On Android `NativeFonts` trybots (e.g.,
`Test-Android-Clang-GalaxyS24-CPU-Adreno750-arm64-Release-All-Android_NativeFonts_AndroidNDKFonts`),
`FLAGS_paragraph_fonts` is empty, so `SkParagraph` tests backed by `ResourceFontCollection` and
guarded by `SKIP_IF_FONTS_NOT_FOUND` (`modules/skparagraph/tests/SkParagraphTest.cpp:126-128`,
`288-294`) silently return early without executing (incident: `flutter#192891`, Gerrit 1416501 PS1).
When `canary_advisor.py triage` inspects an Android `NativeFonts` build on a CL touching
`modules/skparagraph/`, it prints an explicit `Note:` warning that `SKIP_IF_FONTS_NOT_FOUND` tests
were skipped on that bot. Do not treat a green Android `NativeFonts` build as proof that a
`ResourceFontCollection` test ran on Android; see `bug-triage` Phase 3 for target-OS font
verification options (`GetResourceAsData` from `resources/fonts/` or a one-off `DO NOT SUBMIT`
WIP CL pair on `GalaxyS24`).

## Local Chromium Verification (Fast Pre-Canary Check)

When a Skia diff touches `include/private/chromium/`, `src/text/gpu/SkChromeRemoteGlyphCache*`,
`Fontations` (`src/ports/*fontations*`, `rust/`), `src/ports/SkFontMgr_fontconfig*`, or fixes a
`crbug` with an HTML/font reproducer (`poc.html`), verify locally before or alongside
`Canary-Chromium` using the component build in
`/usr/local/google/home/jlavrova/chromium/src/out/Default` (`is_component_build = true`):
1. Apply the Skia diff to `/usr/local/google/home/jlavrova/chromium/src/third_party/skia`.
2. Rebuild `libskia.so` in seconds and run the relevant target:
   ```bash
   autoninja -C /usr/local/google/home/jlavrova/chromium/src/out/Default skia
   # Run blink_platform_unittests, cc_unittests, or headless/live chrome on poc.html
   ```
3. Restore the Chromium Skia checkout cleanly after verification:
   ```bash
   git -C /usr/local/google/home/jlavrova/chromium/src/third_party/skia checkout -- .
   ```

## Local Google3 Paragraph A/B Corpus (Replaces `Canary-G3` on Cloudtop)

`Canary-G3` runs a fixed TAP project list (`third_party/skia/METADATA`, ~55 min) that covers only
157 of the 1,150 transitive runnable `google3` tests depending on `skparagraph` / `skunicode`
(leaving 993 runnable tests uncovered). On Linux Cloudtop, when a CL modifies executable code or
`BUILD.bazel` under `modules/skparagraph/` or `modules/skunicode/`, `canary_advisor.py` replaces
`Canary-G3` with `google3-test-paragraph` fast tier (1,139 targets including `skia_build_test`,
~10–13 min per uncached leg with cached `G3_HEAD` baseline, zero Piper CLs, and zero submit hold):
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py \
  --out /tmp/g3_paragraph_tests \
  --create-citc \
  baseline=G3_HEAD \
  cand=cl:<change>/<ps>
```

## Canary-Flutter is disabled

Never trigger `Canary-Flutter` (R38: it runs but gives no verdict, every run ends in
`INFRA_FAILURE` at the 3h timeout). `canary_advisor.py` lists it in `DISABLED_CANARIES` and
recommends the full local Flutter corpus instead:
`python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py --full`
(see `flutter-test-paragraph-engine`). Remove the entry only after the bot gives real
SUCCESS/FAILURE results again.
