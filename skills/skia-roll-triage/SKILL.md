---
name: skia-roll-triage
description: >-
  Triage the google3 Skia autoroll in one command: find the newest pending
  "Roll Skia" CL, pull its TAP/Sponge diagnostics via autorepair, classify every
  failing target by its HEAD status (Skia regression / broken at HEAD by someone
  else / infra / flaky), extract the first compiler error and the pending CLs
  that already fix it. Use when the user asks why the google3 Skia roll is
  broken or stuck ("почему ролл сломан", "что с роллом в гугл3", "ролл стоит",
  "is the g3 roll stuck", "triage the skia roll"), or before deciding whether
  to re-roll with SKIA_DEPENDENTS_ALREADY_FAILING=true.
---

# Skia google3 roll triage

## Run

From inside any google3 CitC workspace (g4 needs the cwd):

```bash
python3 ~/.gemini/config/skills/skia-roll-triage/scripts/triage.py            # newest pending roll
python3 ~/.gemini/config/skills/skia-roll-triage/scripts/triage.py --cl N     # a specific roll CL
python3 ~/.gemini/config/skills/skia-roll-triage/scripts/triage.py --json     # for automation
```

Takes 10–30 s (one `autorepair investigate` plus one `target-status` per
failing target). Output is Markdown: verdict line, one row per TAP run mapped
to its `third_party/skia/METADATA` check, one row per failing target, and for
each non-flaky target the first log line and pending CLs touching the failing
file or package.

## Reading the verdict

| Verdict | Meaning | What to do |
|---|---|---|
| `SKIA REGRESSION` | A `//third_party/skia/...` or `//chrome/skia/...` target fails | Our problem: bisect the roll range (`https://skia.googlesource.com/skia/+log/A..B`) |
| `DEPENDENT FAILURE NOT BROKEN AT HEAD` | A dependent target fails in the latest run but passes at HEAD and is not an infra abort | Open its Sponge log; if the error names Skia symbols it is a real regression, otherwise likely a flake |
| `BLOCKED BY DEPENDENTS BROKEN AT HEAD` | Every blocking target is already red at postsubmit | Not Skia's fault. Nudge the listed fix CLs, or re-roll with `SKIA_DEPENDENTS_ALREADY_FAILING=true` (sanctioned by METADATA) |
| `INFRA ONLY` | Only `ABORTED_BY_TOOL` / distributor timeouts, HEAD green | Wait for the next attempt |
| `IN PROGRESS` / `GREEN` | Nothing to do yet | — |

`Pending identical attempts: N` is the number of pending twin CLs the roller
has created for the same range; it grows by one every ~20 minutes while stuck.

## How the three checks differ

`third_party/skia/METADATA` runs three TAP groups on each roll:

- `CheckTestsSkia` — Skia's own targets. Red here is always ours.
- `CheckSkiaDependents` — build-only targets of dependents (~5.5k).
- `CheckTestsSkiaDependents` — test projects of dependents (~50 projects,
  `failure_status: WARNING`, `ignore_existing_failures`). Red here is usually
  someone else's HEAD breakage or a flake; the autoroller is supposed to detect
  that and retry with `SKIA_DEPENDENTS_ALREADY_FAILING=true`
  (`//chrome/skia/autoroller/autoroller_lib.py`, `_DependentsAlreadyFailing`),
  but a single flake or a superseded TAP run vetoes the retry.

## Caveats

- `BROKEN_AT_HEAD` relies on `autorepair target-status`; a postsubmit that is
  still `PENDING` is reported as `UNKNOWN_HEAD`, not as broken.
- Pending-CL discovery is a `g4 changes` on the failing file / package from the
  last 10 days; it lists everything touching it, so some rows are unrelated
  (e.g. clrobot cleanups). Read the titles.
- Titles from `g4 changes` are truncated to ~31 characters by g4 itself.
- The script never submits, tags, or comments. Re-rolling with the tag is a
  deliberate human action.
