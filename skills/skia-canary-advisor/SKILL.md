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
python3 $S health                    # OK/DEAD per canary (real verdicts in last 45 days)
python3 $S stats                     # RAN/skipped per logged recommendation + follow rate
```

## Agent steps

1. Run `advise` once on the final patchset (before CR+1 or before LGTM).
2. Execute every `AGENT TODO` line with `code_search`. A hit makes Canary-G3 STRONG.
3. Present the result: STRONG items as "run", CONSIDER items as "only if rendering may change".
4. **Execution Policy (Agent Actions)**:
   - **ASAN**: If an ASAN bot is recommended, YOU (the agent) MUST execute the `git cl try` command provided under `AGENT ACTION` immediately, without asking the author or the user.
   - **External Authors** (owner is not `@google.com`): YOU (the agent) MUST execute the `git cl try` command provided under `AGENT ACTION` immediately, without asking.
     - **Canary-G3 restriction (`g3_canary.go:134`)**: Canary-G3 requires a Googler author OR a Googler `Code-Review+1` approval. If the external CL has no Googler CR+1 yet, `canary_advisor.py` automatically excludes Canary-G3 from `AGENT ACTION` and prints a deferred command.
     - **External Author Submit-Hold Protocol**: Follow the submit-hold protocol below: prepare a `/PATCHSET_LEVEL` draft with `unresolved: true` for the user to publish with `Code-Review+1`. Run Canary-G3 immediately after the user publishes the approval and hold comment.
     - Other canaries (`Canary-Flutter`, `Canary-Chromium`, `Canary-Android`) have no such restriction and are triggered immediately.
   - **Internal Authors / Own CLs**: Show the printed `Canary Command` for canaries and run it ONLY after the user explicitly approves. (For ASAN, you still run it automatically).
5. A red canary is not automatically the CL's fault. Follow the **Canary-G3 Runtime & Triage Procedure** below before blaming the CL.

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
   - The user reviews and publishes `Code-Review+1` with the draft hold comment.
   - Once published, the agent immediately triggers `Canary-G3`:
     ```bash
     git cl try -i <CL> -B skia/skia.primary -b Canary-G3
     ```
   - When `Canary-G3` turns green, the user resolves the comment thread, unblocking submit.

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

## ASAN Sanitizer Selection

The script automatically selects one ASAN bot based on the diff:
- Text/Fonts C++ -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload`
- GPU (Graphite/Vulkan) -> `Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan`
- GPU (Metal) -> `Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal`
- General CPU -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN`
(Does not trigger for docs/Dart/TS/markdown-only CLs).

## Health fallback

If `Canary-Flutter` has no SUCCESS/FAILURE in 45 days, the script replaces it with local `flutter-test-paragraph-engine` + Canary-G3.
