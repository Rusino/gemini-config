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
python3 $S triage --cl <change>      # triage tryjobs (ASAN + Canaries)
python3 $S watch --cl <change> --save-draft  # watch + auto-resolve own Canary-G3 hold on pass
python3 $S resolve-hold --cl <change> [--dry-run]  # resolve own Canary-G3 hold on verified stack
python3 $S health                    # OK/DEAD per canary (real verdicts in last 45 days)
python3 $S stats                     # RAN/skipped per logged recommendation + follow rate
```

## Agent steps

1. Run `advise` once on the final patchset (before CR+1 or before LGTM).
2. Execute every `AGENT TODO` line with `code_search`. A hit makes Canary-G3 STRONG.
3. Present the result: STRONG items as "run", CONSIDER items as "only if rendering may change".
4. **Execution Policy (Agent Actions)**:
   - **ASAN**: If an ASAN bot is recommended, YOU (the agent) MUST execute the `git cl try`
     command provided under `AGENT ACTION` immediately, without asking the author or the user.
   - **External Authors** (owner is not `@google.com`): YOU (the agent) MUST execute the
     `git cl try` command provided under `AGENT ACTION` immediately, without asking.
     - **Canary-G3 restriction (`g3_canary.go:134`)**: Canary-G3 requires a Googler author OR a
       Googler `Code-Review+1` approval. If the external CL has no Googler CR+1 yet,
       `canary_advisor.py` automatically excludes Canary-G3 from `AGENT ACTION` and prints a
       deferred command.
     - **Stacked CLs**: the google3 try service (`chrome/skia/autoroller/try_service.py`)
       cherry-picks every unmerged parent of the CL too, and each one needs its own Googler
       CR+1 (or a Googler owner and author). A CR+1 on the reviewed CL alone gives
       `MISSING_APPROVAL` after about a minute. `watch` checks the chain first and does not
       trigger Canary-G3 while a parent lacks approval; `triage` lists the parents that block
       it. Never vote CR+1 on unreviewed parents just to unblock Canary-G3.
     - **External Author Submit-Hold Protocol**: Follow the submit-hold protocol below: prepare
       a `/PATCHSET_LEVEL` draft with `unresolved: true` for the user to publish with
       `Code-Review+1`. Never ask the user to tell you when they publish `CR+1`: both
       `canary_advisor.py watch` and the Flight Deck `cl_watcher.py` daemon
       (`sync_external_canary_g3`) automatically detect the published `CR+1` + submit-hold
       comment, trigger `Canary-G3` once via `trigger_canary_g3_once`
       (`~/.gemini/config/ledgers/canary_g3_claims.json`), and wake the CL chat on completion.
     - Other canaries (`Canary-Chromium`, `Canary-Android`) have no such restriction and are
       triggered immediately. `Canary-Flutter` is disabled (see below): run the full local
       Flutter corpus instead.
   - **Internal Authors / Own CLs**: Show the printed `Canary Command` for canaries and run it
     ONLY after the user explicitly approves. (For ASAN, you still run it automatically).
5. **Background Monitoring & Wakeup Protocol**:
   - Immediately after triggering `git cl try`, launch
     `python3 ~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py watch --cl <CL> --save-draft`
     as a background command (`WaitMsBeforeAsync=500`, `NotificationTimeoutSeconds=5400`).
   - For external authors needing `Canary-G3`, `watch` automatically polls Gerrit for the Googler
     `Code-Review+1` vote, triggers `Canary-G3` the moment `CR+1` is published, and watches
     `Canary-G3` to completion.
   - **On Green `Canary-G3` (`SUCCESS`)**: `watch` automatically resolves **only**
     `jlavrova@google.com`'s own `/PATCHSET_LEVEL` `Canary-G3` submit-hold thread (matching
     `"hold the submit"` + `"Canary-G3"`, with no third-party replies and active `Code-Review+1` on
     the tested `current_revision`) across the tested `(CL, PS)` and verified unmerged stack
     ancestors, posting the reply on each hold comment's own revision (`revisions/{leaf_ps}/review`
     with `drafts: KEEP`). Never resolves substantive comments or third-party threads.
   - **On Red Canary (`UNRELATED_HEAD_BREAKAGE`)**: `watch --save-draft` automatically verifies
     `CheckTestsSkia` + `blaze query somepath` for all failing targets and saves a strictly
     sanitized English `/PATCHSET_LEVEL` draft on Gerrit (never leaking internal `google3` target
     paths or `cl/N` numbers to public Gerrit). When woken up, the agent shows the internal triage
     table in chat and confirms the draft is saved. (No Gerrit draft is created when Canary is
     green).
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

## External Author Canary-G3 Submit-Hold Protocol

When reviewing an external author's Skia CL that requires `Canary-G3`:
1. **The Hold Mechanism**:
   - `g3_canary.go:134` forbids running Canary-G3 for external authors until a Googler votes `Code-Review+1`.
   - Skia Gerrit `Review-Enforcement` requires 2 approvals. A bare `Code-Review+1` vote allows a second reviewer to submit the CL while Canary-G3 is still running.
   - An unresolved comment leaves Gerrit submit requirement `No-Unresolved-Comments` in `UNSATISFIED` state, preventing premature submission.
2. **Drafting the Hold Comment**:
   - The agent NEVER publishes review comments or votes automatically.
   - The agent drafts a `/PATCHSET_LEVEL` comment (`unresolved: true`) on the current patchset:
     > The change looks good to me (Code-Review+1). I leave this comment unresolved on purpose: it blocks the submit until the Canary-G3 try job passes. Canary-G3 builds and tests the google3 clients of Skia with this change. Skia starts it for an external author only after a Googler votes Code-Review+1, so I vote first and hold the submit with this comment. I will resolve it when Canary-G3 is green, or report the failure here. Please do not resolve or override it before that.
3. **Execution Steps**:
   - The user reviews and publishes `Code-Review+1` with the draft hold comment (never ask the user
     to notify you after publishing `CR+1`).
   - Once published, `canary_advisor.py watch` and the Flight Deck `cl_watcher.py` daemon
     (`sync_external_canary_g3`) automatically detect `CR+1` + the unresolved submit-hold comment
     and invoke `trigger_canary_g3_once` (`~/.gemini/config/ledgers/canary_g3_claims.json`) to run
     `Canary-G3` exactly once per `(CL, PS)`:
     ```bash
     git cl try -i <CL> -B skia/skia.primary -b Canary-G3
     ```
   - When `Canary-G3` finishes `SUCCESS` (with no failing ASAN/Canary builds on that patchset),
     `canary_advisor.py` (`watch` or `resolve-hold --cl <CL>`) and `cl_watcher.py` automatically
     resolve `jlavrova@google.com`'s own `/PATCHSET_LEVEL` submit-hold thread on `<CL>` and any
     unmerged stack ancestors whose `current_revision` matches the cherry-picked patchset and keeps
     `jlavrova@google.com`'s `Code-Review+1`, unblocking submit and waking the CL chat. If
     `Canary-G3` fails, the hold stays unresolved and the CL chat is woken with the triage verdict.

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

## Local Google3 Paragraph A/B Corpus (Fast Pre-Canary Check)

`Canary-G3` runs a fixed TAP project list (`third_party/skia/METADATA`, ~55 min) that covers only
157 of the 1,150 runnable transitive `google3` tests depending on `skparagraph` / `skunicode`
(leaving 993 runnable tests uncovered). On Linux Cloudtop, when a CL modifies executable code or
`BUILD.bazel` under `modules/skparagraph/` or `modules/skunicode/`, run `google3-test-paragraph`
fast tier (~10–13 min per leg, no Piper CL created) before or alongside `Canary-G3`:
```bash
python3 ~/.gemini/config/skills/google3-test-paragraph/scripts/run_g3_paragraph_tests.py \
  --out /tmp/g3_paragraph_tests \
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
