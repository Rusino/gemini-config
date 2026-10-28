---
name: skia-canary-advisor
description: >-
  Recommend and trigger Skia Canary try jobs and ASAN bots for a Skia CL based on the diff.
  Trigger from `amnesia-precommit` (own CLs) and `review-triage` (others' CLs).
  Agent ALWAYS triggers the ASAN bot. Agent triggers canaries automatically for external authors.
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
   - **External Authors** (owner is not `@google.com`): YOU (the agent) MUST execute the `git cl try` command for ALL recommended canaries AND the ASAN bot immediately, without asking.
   - **Internal Authors / Own CLs**: Show the printed `Canary Command` for canaries and run it ONLY after the user explicitly approves. (For ASAN, you still run it automatically).
5. A red canary is not automatically the CL's fault. Compare failing targets with the target's tip before blaming the CL.

## ASAN Sanitizer Selection

The script automatically selects one ASAN bot based on the diff:
- Text/Fonts C++ -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-NativeFonts_FreeType_ASAN_Upload`
- GPU (Graphite/Vulkan) -> `Test-Ubuntu24.04-Clang-NUC11TZi5-GPU-IntelIrisXe-x86_64-Debug-All-DDL3_ASAN_Vulkan`
- GPU (Metal) -> `Test-Mac15-Clang-MacMini16.10-GPU-AppleM4-arm64-Debug-All-ASAN_Graphite_Native_Metal`
- General CPU -> `Test-Ubuntu24.04-Clang-GCE-CPU-AVX2-x86_64-Debug-All-ASAN`
(Does not trigger for docs/Dart/TS/markdown-only CLs).

## Health fallback

If `Canary-Flutter` has no SUCCESS/FAILURE in 45 days, the script replaces it with local `flutter-test-paragraph-engine` + Canary-G3.
