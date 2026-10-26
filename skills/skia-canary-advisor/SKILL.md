---
name: skia-canary-advisor
description: >-
  Recommend (never trigger) Skia Canary try jobs — Canary-Flutter, Canary-G3, Canary-Chromium,
  Canary-Android — for an own or reviewed Skia CL, based on the client-visible surface of the diff
  (text modules, public API, removed SK_* guards, build files, font ports) and live canary health
  from Buildbucket. Trigger from `amnesia-precommit` (own CLs) and `review-triage` (others' CLs),
  or when asked "нужен ли канареечный билд", "какие канарейки запустить", "canary for this CL".
---

# Skia Canary Advisor

Recommendation-only mode: the user decides. The ledger records every recommendation so that
`stats` can show the follow rate; if it stays near 100%, switch own CLs to auto-trigger.

## Run

```bash
S=~/.gemini/config/skills/skia-canary-advisor/scripts/canary_advisor.py
python3 $S advise --cl <change>      # Gerrit CL, own or under review
python3 $S advise --repo <skia>      # local branch vs origin/main
python3 $S health                    # OK/DEAD per canary (real verdicts in last 45 days)
python3 $S stats                     # RAN/skipped per logged recommendation + follow rate
```

## Agent steps

1. Run `advise` once, on the final patchset (before CR+1 or before your LGTM), not on every
   patchset: median canary runtime is ~8 min (G3), ~40 min (Chromium), ~75 min (Android),
   and each run uses target CQ capacity.
2. Execute every `AGENT TODO` line with `code_search`. A hit makes Canary-G3 STRONG; zero hits for
   all symbols lets you downgrade a G3 recommendation that came only from API changes.
3. Present the result: STRONG items as "run", CONSIDER items as "only if rendering may change".
4. Own CL: show the printed `git cl try ...` command and run it only after the user says yes.
   Reviewed CL: never trigger; draft a review comment for the author naming the canaries and the
   reason (symbol + usage count), for the user to publish.
5. A red canary is not automatically the CL's fault: Canary-Chromium and Canary-G3 fail often on
   their own. Compare failing targets with the target's tip before blaming the CL.

## Health fallback

If `Canary-Flutter` has no SUCCESS/FAILURE in 45 days, the script replaces it with local
`flutter-test-paragraph-engine` + Canary-G3. It switches back automatically once Canary-Flutter
produces verdicts again.
