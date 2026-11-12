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
python3 $S try --cl <change> -b <builder>  # git cl try + Buildbucket check, ends with TRIGGER:
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
   - **Trigger only through `try`, report only `TRIGGER: VERIFIED`**: run the `canary_advisor.py
     try --cl <CL> -b <builder> ...` command printed under `AGENT ACTION`, never a bare
     `git cl try`. It wraps `git cl try` in `luci-auth context` (7-day `LOAS2`) and falls back to
     plain `git cl` where `luci-auth` is not logged in (the Mac), then polls Buildbucket and ends
     with `TRIGGER: VERIFIED` or `TRIGGER: FAILED (...)`. Tell the user a bot is triggered or
     running only after `TRIGGER: VERIFIED`; on `FAILED`, report the printed error. Incident
     (Gerrit 1407718 PS18, 2026-11-07): `git cl try` exited 1 with `Unable to determine default
     branch` from a worktree branch without upstream, the agent read only the depot_tools warning
     tail, reported ASAN as running, and the watcher waited an hour for a build that did not exist.
   - **ASAN**: If an ASAN bot is recommended, YOU (the agent) MUST execute the `try` command
     provided under `AGENT ACTION` immediately, without asking the author or the user.
   - **SkParagraph / SkUnicode on Linux Cloudtop (`LOCAL google3-test-paragraph`)**: When `/google/src/cloud` is mounted, `canary_advisor.py` automatically replaces `Canary-G3` for `modules/skparagraph/` and `modules/skunicode/` paths with `LOCAL google3-test-paragraph` (~10–13 min, 1,139 fast targets including `skia_build_test`, cached `G3_HEAD` baseline, zero Piper CLs, and zero Gerrit submit hold). Run it before `CR+1` / upload instead of triggering `Canary-G3`. Lightweight `Canary-G3` remains active only for non-paragraph Skia paths (`modules/skshaper/`, `src/ports/`, core `src/`/`include/`, root `BUILD.bazel`) or when off Cloudtop.
     - **Auto-Slow-Tier Policy**: When you run `google3-test-paragraph` automatically as the final check before granting `CR+1` (or before `amnesia-precommit` upload), you MUST append `--tier all` to cover the 13 slow tests. During intermediate/WIP checks, use the default `--tier fast`.
   - **SkParagraph speed (`LOCAL flutter-engine-perf-compare`)**: For executable C/C++ under
     `modules/{skparagraph,skunicode,skshaper}/{src,include}/` or `Sk*` files under
     `include/core/`, `include/private/base/`, `src/base/`, `src/core/`, the advisor prints a
     `txt_benchmarks A/B` `AGENT ACTION` (`skia_patch_ab.py`; for a CL, run it on a detached
     worktree of the patchset with `--base HEAD~1`). It applies to reviews too: the
     `[PERF-CHECK GUARD]` hook sees only our own edits. `REGRESSION` blocks `CR+1`.
   - **External Authors** (owner is not `@google.com`): YOU (the agent) MUST execute the `try`
     command provided under `AGENT ACTION` immediately, without asking.
     - **Canary-G3 restriction (`g3_canary.go:134`)**: For non-paragraph Skia CLs that still need `Canary-G3`, `Canary-G3` requires a Googler author OR a Googler `Code-Review+1` approval. If the external CL has no Googler `CR+1` yet, `canary_advisor.py` excludes `Canary-G3` from `AGENT ACTION` and prints the manual command to run if needed after `CR+1` (do not place unresolved submit-hold comments).
     - **Stacked CLs**: the google3 try service (`chrome/skia/autoroller/try_service.py`)
       cherry-picks every unmerged parent of the CL too, and each one needs its own Googler
       `CR+1` (or a Googler owner and author). A `CR+1` on the reviewed CL alone gives
       `MISSING_APPROVAL` after about a minute; `triage` lists the parents that block it. Never
       vote `CR+1` on unreviewed parents just to unblock `Canary-G3`.
     - Other canaries (`Canary-Chromium`, `Canary-Android`, `Canary-Flutter`) have no such
       restriction and are triggered immediately. For `skparagraph`/`skunicode`/`skshaper`
       paths the full local Flutter corpus runs alongside `Canary-Flutter` (see below).
   - **Internal Authors / Own CLs**: Show the printed `Canary Command` for canaries and run it
     ONLY after the user explicitly approves. (For ASAN, you still run it automatically).
5. **Background Monitoring & Wakeup Protocol**:
   - Immediately after `TRIGGER: VERIFIED`, launch
     `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py watch --cl <CL> --save-draft`
     as a background command (`WaitMsBeforeAsync=500`, `NotificationTimeoutSeconds=5400`). The
     watcher exits with `WAKEUP_REASON: NO_TRYJOBS` when the patchset still has no build after
     `--no-builds-min` (10) minutes; treat that as a failed trigger, not as a slow bot.
   - **On Red Canary (`UNRELATED_HEAD_BREAKAGE` across All Canaries)**: `watch --save-draft` and
     `triage --save-draft` verify the downstream roll CL for **all** Skia canaries (`Canary-G3`
     via `skia_gardener` `CheckTestsSkia` + `blaze query somepath`, `Canary-Chromium` via
     Buildbucket CQ trybots on `https://crrev.com/c/<roll_cl>/<ps>`, `Canary-Android` via
     TreeHugger messages + AnTS build/test logs, and `Canary-Flutter` via the GitHub checks of
     the `flutter/flutter` roll PR) before returning `UNRELATED_HEAD_BREAKAGE`, and
     save a strictly sanitized English `/PATCHSET_LEVEL` draft on the CL's **current** patchset
     (enforcing `PUBLIC_DRAFT_LEAK_RE` so zero `cl/`, `ag/`, `cr/`, `b/`, `go/`, `googleplex`,
     `//...` paths, or AnTS `I<15+>` IDs reach public Gerrit). When woken up, show the internal
     triage summary in chat and confirm the draft is saved. (No Gerrit draft is created when a
     canary is green.)
   - **Approval gate (`TRYJOBS:` line)**: `triage` (and `watch` on wakeup) aggregates tryjobs
     across all same-tree patchsets (`code_patchset..ps`) and ends with `TRYJOBS: ALL GREEN` or
     `TRYJOBS: NOT GREEN (<builders>)`, covering ASAN, canaries, and other trybots such as Mac
     `NativeFonts_Fontations` (with the dm `Failures:` block). Never give LGTM / `CR+1` / a CLEAN
     verdict before `ALL GREEN`; if the watcher was killed, run `triage` by hand. `stop_guard.py`
     (`[TRYJOB GATE GUARD]`) enforces this.
   - **Description & Red-Canary Draft gates (`TESTED BLOCK:` and `CANARY DRAFT:` lines)**: on
     `ALL GREEN` for an open CL, `triage` checks the current patchset's commit message and prints
     `TESTED BLOCK: OK` or `TESTED BLOCK: STALE (missing: ...)`, plus `CANARY DRAFT: OK` or
     `CANARY DRAFT: MISSING on PS <draft_ps>` whenever any canary failed on unrelated/infra
     targets. On `STALE`, update the `Tested:` block from the latest local runs and trybots
     (`skills/amnesia-precommit/references/commit_message.md`) and re-upload with
     `--commit-description=+` before the final summary; if any canary was red (`UNRELATED_HEAD_BREAKAGE`),
     re-run `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py triage --cl <CL> --save-draft`
     right after the `NO_CODE_CHANGE` upload so the `/PATCHSET_LEVEL` draft lands on the new
     latest patchset instead of staying stale on an older patchset. Do not re-trigger ASAN or
     canaries for a `NO_CODE_CHANGE` patchset.
   - **On Red Canary (`CL_SUSPECTED_REGRESSION` or `CANNOT_TRIAGE`)**: Inspect the failing target,
     dependency chain, or roll logs before blaming the CL. If the failure is caused by the CL,
     report the target and stack trace to the user and proceed to fix. If manual stack-trace or
     roll-CL inspection proves our part passed and the failure is unrelated, you MUST save a
     sanitized English `/PATCHSET_LEVEL` Gerrit draft on the final current patchset via
     `python3 ~/.gemini/config/scripts/gerrit_draft.py save --cl <CL> --resolved --message "..."`
     stating specifically what passed for our part and why the canary failed.
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
6. A red canary is not automatically the CL's fault, nor is it automatically unrelated. Follow the
   **Canary Triage Procedures (`Canary-G3`, `Canary-Chromium`, `Canary-Android`)** below to prove
   our part passed before saving any Gerrit draft.

## Canary Triage Procedures (`Canary-G3`, `Canary-Chromium`, `Canary-Android`)

### `Canary-G3`
- **Runtime**: `Canary-G3` can run for ~55 minutes (or hit a 3h Swarming wrapper timeout after checks finish). Do not assume timeout after 8–10 minutes.
- **Triage on Red Canary**:
  1. Extract the canary roll CL number from the Swarming task stdout (`Canary roll [ http://cl/N ]`) or the Swarming UI task step.
  2. Run the automated google3 triage command:
     ```bash
     cd ~/.gemini/jetski/scratch/skia_gardener && go run . -triage-google3 -cl <g3 canary CL>
     ```
     Confirm `CheckTestsSkia` actually `PASSED` (`failed == 0`, no `setup_error`).
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

### `Canary-Chromium`
- `canary_advisor.py triage` extracts `https://chromium-review.googlesource.com/c/.../<roll_cl>` from Swarming stdout and queries Buildbucket `SearchBuilds` + `GetBuild` on `chromium/src` across roll patchsets `(3, 2, 1)` newest-first:
  - Requires `> 0` green compiling Chromium CQ trybots (`passed_compile`, excluding `presubmit`/`tricium`), fails closed on `SCHEDULED`/`STARTED`/`CANCELED` builders, clears bots that passed on retry, and classifies remaining failures (`bot_update` GoB lag patch failure, early shard abort paired with an independent infra peer, Buildbucket `INFRA_FAILURE`, non-Skia compile error where `compile (without patch)` also failed, pure ADB device disconnect without crash/Skia tokens, or pre-existing Chromium test flake reproduced on sibling non-autoroll CLs).
  - Any Skia/caller compile error where trunk compiled cleanly or unproven test failure fails closed (`CL_SUSPECTED_REGRESSION`). Public Gerrit drafts cite `https://crrev.com/c/<roll_cl>/<ps>` (never `cr/<N>`).

### `Canary-Android`
- `canary_advisor.py triage` extracts the `googleplex-android-review` roll CL from Swarming stdout, fetches `?o=MESSAGES&o=LABELS` via `sso_client`, and inspects paginated AnTS build/test logs via `androidbuildinternal.googleapis.com` (`luci-auth token -scopes https://www.googleapis.com/auth/androidbuild.internal`):
  - Evaluates the newest latest-revision TreeHugger message (failing closed on unparsed `FAIL:` lines) and marks `UNRELATED_HEAD_BREAKAGE` only when TreeHugger passed on automatic retry (`Verify-Pass` / `Presubmit-Verified+1`), when `build_error.log` fails strictly outside Skia/HWUI/consumers, or when failing test runs are TradeFed `*INFRA_ERROR` / AnTS failures with `0` Skia, HWUI, RenderEngine, Minikin, CtsText, PDFium, SurfaceFlinger, or touched-stem frames.
  - Public Gerrit drafts must never contain `ag/<N>`, `googleplex`, `*.corp.google.com` / `*.corp.goog`, internal device/lunch target names, or AnTS `I<15+>` invocation IDs.

### `Canary-Flutter`
- `canary_advisor.py triage` takes the newest `https://github.com/flutter/flutter/pull/<N>` from
  Swarming stdout, reads the PR head SHA, every page of `check-runs` and the combined commit
  `status` (`gh api`, anonymous REST fallback). `Presubmit` and `Dashboard Checks`
  (`flutter-dashboard`) stand for the ~150 `flutter/try` LUCI builds.
  - `UNRELATED_HEAD_BREAKAGE` only when every check run is `completed` with `success` /
    `skipped` / `neutral`, every commit status except `tree-status` is `success`, and
    `Dashboard Checks`, `Presubmit` (if present) and `flutter-gold` are `success`. Dry-run rolls
    wait for no named check (`autoroll/go/codereview/roll.go`), so a closed tree
    (`tree-status: failure`) alone turns the canary red.
  - A red check (including `action_required`) gives `CL_SUSPECTED_REGRESSION`; running checks or
    a missing gate give `CANNOT_TRIAGE`; `Canary roll could not be created` or no PR URL gives
    `INFRA_NO_ROLL_CL`.

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
breakages on Mac trybots (`skia_headers_and_font_tests.md`; Gerrit 1404196 -> 1411716).

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

## Canary-Flutter and the local Flutter corpus

`Canary-Flutter` was disabled from 2026-08 to 2026-11-12 (R38): every run ended in
`INFRA_FAILURE` at the 3h timeout because the autoroller treated GitHub `skipped` checks as still
running. [buildbot CL 1409959](https://skia-review.googlesource.com/c/buildbot/+/1409959) fixed
that and was deployed on 2026-11-03; runs give SUCCESS/FAILURE verdicts again (R58).

`canary_advisor.py` recommends it like any other canary. For
`modules/(skparagraph|skunicode|skshaper)/(src|include)/` it also prints the full local Flutter
corpus as a LOCAL gate, because the canary does not run the ~2400 A/B text tests:
`python3 ~/.gemini/config/skills/flutter-test-paragraph-engine/scripts/run_flutter_paragraph_tests.py --full`
(see `flutter-test-paragraph-engine`). If `canary_health` finds no SUCCESS/FAILURE verdict in 45
days, or a canary is listed in `DISABLED_CANARIES`, the advisor drops `Canary-Flutter` and keeps
only the local corpus.
