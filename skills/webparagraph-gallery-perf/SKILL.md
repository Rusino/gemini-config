---
name: webparagraph-gallery-perf
description: >-
  Measure Flutter Web WebParagraph vs SkParagraph (or two branches) on Flutter Gallery in Chrome
  with paired bootstrap 95% CI, Run Attestation, and a Russian report. Trigger when asked to
  "замерь производительность WebParagraph", "сравни производительность веток", "прогони Gallery
  бенчмарк", "сделай отчёт о производительности WebParagraph", "замерь на маке", "gallery perf",
  "WP vs SkP perf", or to compare profile vs release.
---

# WebParagraph Gallery Performance (`webparagraph-gallery-perf`)

End-to-end benchmark of text rendering in a real app: Flutter Gallery
(`dev/integration_tests/new_gallery`) driven by [`main_perf.dart`](assets/main_perf.dart)
through scroll, page open, app resize and page reopen phases, measured from Chrome traces.
Validated in R31 / R35 / R43 (30 + 36 runs on a Radeon WX 3200 host).

## Scripts

Set `S=~/.gemini/config/skills/webparagraph-gallery-perf/scripts`.

- [`build_variant.sh`](scripts/build_variant.sh): worktree at a commit, `ninja flutter/web_sdk`,
  `flutter build web`, copy of `build/web` with `ATTESTATION.json`
- [`run_bench.py`](scripts/run_bench.py): interleaved rotated rounds over engines × configs,
  `run_attestation.json`
- [`drive_gallery.py`](scripts/drive_gallery.py): one run (HTTP server with COOP/COEP, Chrome
  over CDP, trace, `/proc` CPU samples, console lines)
- [`gallery_analysis.py`](scripts/gallery_analysis.py): shared trace / CPU / phase analysis and
  the paired bootstrap CI
- [`compare.py`](scripts/compare.py): `compare.md` with attestation, integrity, ratio tables,
  absolute medians and the real-time table
- [`remote.sh`](scripts/remote.sh): `pack` / `start` / `status` / `fetch` on a remote GPU host
- [`summarize.py`](scripts/summarize.py): Russian tables for the human report (main thread with
  verdicts, GPU range, 60 Hz with long frames, A/B effect with the 2% rule, tracing overhead)

Assets copied into the Gallery by `build_variant.sh`: [`main_perf.dart`](assets/main_perf.dart),
[`flutter_bootstrap.js`](assets/flutter_bootstrap.js) (`?web_paragraph=true` →
`preferWebParagraph: true`, prints the `GALLERY_ATTESTATION` line with the WebGL renderer),
[`index.html`](assets/index.html).

## How to Run

### 1. Build the variants (~30 min each from scratch; run builds one at a time)

```bash
bash $S/build_variant.sh master origin/master --out ~/perf/r45
bash $S/build_variant.sh master_prof origin/master --out ~/perf/r45 --mode profile \
  --worktree ~/.gemini/jetski/scratch/flutter_perf_master   # reuse the release worktree
bash $S/build_variant.sh fill_runs 05cb0c401d5 --out ~/perf/r45
bash $S/build_variant.sh fill_runs 05cb0c401d5 --out ~/perf/r45 --dry-run   # print only
```

- The worktree goes to `$(dirname <base-checkout>)/flutter_perf_<name>` (or `--worktree`); the
  default `--base-checkout` is `~/.gemini/jetski/scratch/flutter_perf_method`, which must have
  gclient deps and `engine/src/out/wasm_release`. Deps are symlinked, `engine/src/out/wasm_release`
  is copied once.
- A dirty worktree (tracked changes or untracked files other than the harness files and dep
  symlinks the script creates) is refused unless `--allow-dirty`. An existing worktree at a
  different commit is refused.
- Output: `<out>/build_<name>/` (serve-ready `build/web`) with `ATTESTATION.json` (commit,
  `git describe`, subject, build mode, implied dart2js flags, engine out dir, timestamp, dirty
  files); logs in `<out>/logs/`.
- One engine build serves both `wp` and `skp` runs: the mode is the URL flag, not the build.
- Deps (Dart SDK, Skia, CanvasKit) come from the base checkout, not from the commit's `DEPS`.
  Build only commits whose `DEPS` match the base checkout; after a Dart or Skia roll, sync the
  base checkout first or the build mixes versions.
- Start builds detached (`setsid nohup bash -c '...' > log 2>&1 < /dev/null & disown`): a plain
  `nohup ... &` from an agent tool call dies with the tool session.

### 2. Run rounds

On a host with a hardware GPU (see [Remote GPU host](#remote-gpu-host)):

```bash
E=skp:builds/build_master:skp,wp:builds/build_master:wp,wpnew:builds/build_fill_runs:wp
python3 $S/run_bench.py --out results --rounds 6 --configs base --engines $E \
  --require-gpu --no-dbus --chrome /tmp/r31_cft/chrome-linux64/chrome \
  --chrome-flag=--use-angle=gl-egl
```

- `--engines name:build_dir:mode,...`; `mode` is `wp` or `skp`.
- `--configs`: `base` (DPR 1), `dpr2`, `dpr3`, `cpu2`, `cpu4` (CDP CPU throttling).
- One run takes ~1.5–2 min; 6 rounds × 3 engines ≈ 35 min per config.
- Results: `results/<config>/<engine>_<round>.{json,trace.json.gz}`,
  `results/run_attestation.json` (host, CPUs, Chrome version, flags, GPU renderers seen, load
  average and build processes before/after every run, retries, engines with their
  `ATTESTATION.json`). It is rewritten after every run, so a partial run can be compared.

### 3. Compare

```bash
python3 $S/compare.py results --pairs "WP/SkP=wp/skp,FillTextRuns effect=wpnew/wp"
```

Writes `results/compare.md`; prints the Integrity verdict. Options: `--configs`, `--phases`
(notable phases, default `resize:home,resize:fortnightly,open:reply,open:data-table`), `--md`.
`compare.py` also reads old flat directories of `<engine>_<round>.json` runs (no attestation).

> [!IMPORTANT]
> Paste the Run Attestation and Integrity Check sections verbatim when reporting. Do not draw
> conclusions from a run whose verdict is `INVALID`, and say so explicitly for `WARNING`.

In a human-readable report built from `compare.md` (not the verbatim attestation), call the
variants by what they change ("WebParagraph without FillTextRuns"), not by engine names (`c0`,
`c1`), commit hashes or roadmap numbers; keep those only in a reproducibility section, with words.
Prefix CL and issue numbers with their system name (`Chromium CL 8664078`).

### 4. Human-readable report

```bash
python3 $S/summarize.py results_dpr1 results_dpr23 results_fill --wp c1 --skp skp \
  --ab c1/c0 --ab-labels "с ним,без него" --ab-title "вклад FillTextRuns" --md tables.md
python3 $S/summarize.py results_notrace --wp wpt --skp skpt --trace wpt/wpn,skpt/skpn
```

For every config the first dir that has both engines of a pair is used, so DPR 1 and DPR 2/3
can come from different runs. `tables.md` ends with the sources and their Integrity verdicts.
Example of the finished report (Linux, Radeon WX 3200, Nov 2026):
[`report_example_linux.md`](assets/report_example_linux.md).

Report rules (agreed with the user, keep them):

- **Structure**: 1 Что сравниваем (each set of changes in words, the paint path, rejected
  variants in one short paragraph), 2 Как измеряли (scenarios without benchmark numbers, metrics,
  tools, brief overhead & rounds summary, machine, comparison, verdict rule, A/B rule), 3 Итоги
  (verdict per scenario, concise bullets, «Что не измерено»), 4 Результаты (main-thread table, GPU
  summary/table, 60 Hz table, then «Детали: …» sections; keep «Детали: влияние трейса» to one
  short paragraph with results and no table).
- **Numbers, format A**: bare signed integer percents, "проценты" said once in the caption
  (`+25`, `−1`, `0`); decimal comma for milliseconds.
- **Verdict per scenario**: the worst value over all DPRs and both metrics (main thread, GPU
  process) vs SkParagraph: `<= +5` 🟢 наравне, `+6..+15` 🟡 немного тяжелее, `> +15` 🔴 тяжелее.
- **A/B rule** (only for «Детали» of one change): a variant wins a metric if the 95% CI excludes
  1.00 and the effect is at least 2%, otherwise a tie; if the metrics disagree, the sum of the
  main-thread and GPU medians in ms decides. No undefined words such as «слабо» in cells.
- **Long frames**: give the count (`в 2 запусках из 12, по 1–3 кадра, самый долгий 21 мс`) and
  say whether a repeat or control run saw them.
- **Identifiers**: variants in words, no engine names (`c0`, `c1`), hashes or roadmap numbers
  outside a reproducibility note; CL and issue numbers with the system name
  (`Chromium CL 8664078`).
- **Delivery**: `report_vN.md` in the artifact dir root plus a redline from the previous
  version the user saw (`redline` skill), shown with `<agent-embed>`; a `file://` link to HTML in
  `scratch/` opens as raw source.

### Tracing overhead control

Add an engine spec suffix `:notrace` to run the same build without a Chrome trace
(`drive_gallery.py --no-trace`). Untraced runs have no renderer main / GPU proc busy, so compare
traced vs untraced on `build (FrameTiming)`, `CPU all Chrome` and the real-time 60 Hz table:

```bash
E=skpt:builds/build_c1:skp,skpn:builds/build_c1:skp:notrace
E=$E,wpt:builds/build_c1:wp,wpn:builds/build_c1:wp:notrace
python3 $S/run_bench.py --out results_notrace --rounds 6 --configs base,dpr3 --engines $E ...
python3 $S/compare.py results_notrace \
  --pairs "trace WP=wpt/wpn,trace SkP=skpt/skpn,WP/SkP traced=wpt/skpt,WP/SkP untraced=wpn/skpn"
```

If the WP/SkP ratios with and without the trace agree within their CIs, the trace does not bias
the comparison. The Integrity Check reports untraced runs as INFO.

### Smoke test on Cloudtop (no GPU)

```bash
B=<dir with build/web copy>
python3 $S/run_bench.py --engines skp:$B:skp,wp:$B:wp --configs base --rounds 1 --out /tmp/smoke
python3 $S/compare.py /tmp/smoke --pairs "WP/SkP=wp/skp"
```

Expect verdict `WARNING` with `software GL (... SwiftShader ...)`. This checks that the builds
load, WP is really on (`webParagraph=true`, `textClusters=true`) and the scenario finishes.

## Methodology and Why

- **Release build** (`flutter build web --release`: dart2js `-O4 --minify`,
  `dart.vm.product=true`) is what users get. **Profile** (`-O4 --no-minify`,
  `dart.vm.profile=true`) adds framework Timeline events as `performance.mark` calls; use it
  for DevTools diagnosis, and measure its effect on ratios before mixing it with release
  (roadmap R45). Devicelab web benchmarks use profile.
- **Not felt test flags**: `felt` compiles with `--disable-inlining --enable-asserts -O2`. That
  slows Dart-heavy WP layout and paint recording 1.6–2.3× vs `-O4` but SkP (WASM) only
  1.0–1.2× (R31 `ablation_summary.md`), so felt numbers distort WP vs SkP.
- **Real GPU required for decisions**: on Cloudtop Chrome falls back to SwiftShader (or no
  WebGL), CanvasKit rasterizes in software and GPU-process time is CPU time. Absolute numbers
  and GPU proc busy are meaningless there; Cloudtop is for smoke tests only.
- **Interleaving + rotation**: every round runs every engine once, and the order rotates per
  round (`k = round % engines`), so thermal and background drift hit all engines equally.
  Each run uses a fresh Chrome profile (no shader or HTTP cache carry-over); a failed run is
  retried once.
- **Paired ratios + bootstrap**: ratio A/B is computed per round (same drift), then the median
  of per-round ratios with a seeded bootstrap 95% CI over rounds (4000 resamples). Use ≥ 6
  rounds; 1 round gives a ratio with no CI.
- **Metrics** (ms per phase, summed per phase kind):
  - `renderer main busy`: union of `toplevel` trace tasks on `CrRendererMain` of the page
    renderer inside the phase marks. Primary metric, precise (±3% CI at 6 rounds).
  - `GPU proc busy`: `toplevel` tasks in the GPU process. Primary when the WP variant paints on
    the GPU (ImageBitmap path); on WP master text is CPU Canvas2D + `getImageData`.
  - `build (FrameTiming)`: Flutter build phase sum (layout + paint recording; WP rasterizes
    text here).
  - `CPU all Chrome`: `/proc` CPU time of all Chrome processes, Linux only, ±20% CI: noisy,
    use only as a sanity check.
- **Real-time phases** (`rt-scroll`, `rt-resize`): one input per 16.7 ms tick, not paced by
  frames. Use p90 frame time, max vsync gap and frames > 16.7 ms. The "dropped ticks" number
  is unreliable (no-op ticks after the scroll extent, negative under throttling); `compare.py`
  does not show it.

## Integrity Check (in `compare.md`)

| check | level |
| :--- | :--- |
| run without `GALLERY_DONE` (excluded), `GALLERY_FAILED`, missing trace | WARNING |
| engine with no complete run, run aborted | INVALID |
| different GPU renderers across runs | INVALID |
| software GL (SwiftShader / llvmpipe / none / masked) | INVALID if `--require-gpu`, else WARNING |
| `webParagraph` flag does not match the mode, WP without `TextCluster`, DPR mismatch | INVALID |
| unequal complete runs per engine, fewer runs than planned | WARNING |
| 1-min load average > 25% of CPUs (`--load-warn`), build processes seen | WARNING |
| no `ATTESTATION.json`, dirty build, mixed build modes | WARNING |
| retried runs, no `/proc` samples | INFO |

## Known Caveats

- Trace marks keep the historical `R31:` prefix (`R31:start:<phase>`); old traces and new ones
  parse the same way.
- `open:*` and `reopen:*` include fixed settle sleeps: compare busy time, never wall time.
- WP master paints text on the CPU (Canvas2D `willReadFrequently` + `getImageData`), so "real
  GPU" applies to CanvasKit compositing; GPU text paths (OffscreenCanvas + ImageBitmap) exist
  only on unmerged branches (R10 `70078f756ad`, R47 `r47_g0` / `r47_g1`).
- Do not run benchmarks while `ninja` / `flutter build` / `felt` run on the same machine: the
  Integrity Check warns, but the ratios are already biased.
- `drive_gallery.py` uses fixed ports (HTTP 8765+, CDP 9333): run one benchmark per host.

## Remote GPU host

Verified on `jlavrova.rdu.corp.google.com` (AMD Radeon PRO WX 3200, 24 cores), R35 / R43:

- The user must be in the `render` group (only `video` gives Mesa llvmpipe).
- Use Chrome for Testing, not system Chrome (policy-managed, ~10 forced extensions): unpack
  `chrome-linux64.zip` to e.g. `/tmp/r31_cft/chrome-linux64/chrome` (CfT 156 is there now;
  `/tmp` can be wiped on reboot).
- `--chrome-flag=--use-angle=gl-egl`: plain headless gives SwiftShader.
- `--no-dbus` (sets `DBUS_SESSION_BUS_ADDRESS=disabled:`): over SSH a session-DBus call stalls
  all navigation (zero HTTP requests).
- Page console is read over CDP `Runtime.consoleAPICalled`; it never reaches stderr there.
- Python 3.9+ with `websockets` (`python3 -m pip install --user websockets`).
- SSH needs a fresh `gcert` on the Cloudtop ("stuck at SSO" otherwise).

```bash
bash $S/remote.sh pack --builds ~/perf/r45/build_master,~/perf/r45/build_fill_runs
bash $S/remote.sh start --host jlavrova.rdu.corp.google.com --dir /tmp/wp_perf_r45 -- \
  --out results --rounds 6 --configs base --require-gpu --no-dbus \
  --chrome /tmp/r31_cft/chrome-linux64/chrome --chrome-flag=--use-angle=gl-egl \
  --engines skp:builds/build_master:skp,wp:builds/build_master:wp,wpnew:builds/build_fill_runs:wp
bash $S/remote.sh status --host jlavrova.rdu.corp.google.com --dir /tmp/wp_perf_r45
bash $S/remote.sh fetch --host jlavrova.rdu.corp.google.com --dir /tmp/wp_perf_r45 \
  --dest ~/perf/r45/results
python3 $S/compare.py ~/perf/r45/results --pairs "WP/SkP=wp/skp,FillTextRuns=wpnew/wp"
```

A smoke run on the host prints the attestation line, e.g.
`ANGLE (AMD, AMD Radeon Pro WX 3200 Series (radeonsi polaris12 ACO), OpenGL ES 3.2)`.

## Mac

Validated on Nov 1–2 2026: MacBook Pro, Apple M5 Max (18 cores, 64 GB), macOS 26.7.1, Chrome for
Testing 156.0.8078.73 (`base,dpr2 × 6`, 36/36 runs OK, ~1 h, Integrity Check `OK` in
`~/.gemini/jetski/scratch/wp_gallery_perf/mac/results_mac`; and `base,dpr2,dpr3 × 6` across 4
variants including `ImageBitmap` no-copy, 72/72 runs OK, ~2 h in `results_mac2`).

Builds are plain web assets (`build/web`), so do not rebuild on the Mac: pack the Linux builds
and the scripts on the Cloudtop and run locally on the Mac.

```bash
# Cloudtop
R=~/.gemini/jetski/scratch/wp_gallery_perf/r47
bash $S/remote.sh pack --builds $R/build_c0,$R/build_c1 --tgz /tmp/wp_mac.tgz
# Mac (copy /tmp/wp_mac.tgz over first)
mkdir -p ~/wp_perf && cd ~/wp_perf && tar -xzf ~/Downloads/wp_mac.tgz
python3 -m pip install --user websockets
CFT="$HOME/cft/chrome-mac-arm64/Google Chrome for Testing.app"
CFT="$CFT/Contents/MacOS/Google Chrome for Testing"
python3 scripts/run_bench.py --out results_mac --rounds 6 --configs base,dpr2 --require-gpu \
  --chrome "$CFT" --engines skp:builds/build_c0:skp,c0:builds/build_c0:wp,c1:builds/build_c1:wp
python3 scripts/compare.py results_mac --pairs "WP/SkP=c1/skp,FillTextRuns=c1/c0"
python3 scripts/summarize.py results_mac --wp c1 --skp skp --ab c1/c0 \
  --ab-labels "с ним,без него" --ab-title "вклад FillTextRuns" --md results_mac/tables.md
```

- Chrome for Testing (unpacked `chrome-mac-arm64.zip`) avoids managed-Chrome extensions; system
  Chrome is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`. Remove the
  quarantine flag if macOS blocks it: `xattr -dr com.apple.quarantine ~/cft`.
- `--require-gpu` aborts on SwiftShader. `--headless=new` gets the hardware Metal GPU without
  `--headful` (`ANGLE (Apple, ANGLE Metal Renderer: Apple M5 Max, ...)`); keep `--headful` only
  as a fallback if `GALLERY_ATTESTATION` shows SwiftShader.
- The Mac GPU process is lightly loaded (e.g. `resize` GPU busy ~85-145 ms vs ~465 ms on the
  Linux Radeon host), so WebParagraph GPU-process overhead that is invisible on Linux shows up
  here (+2..+80% vs SkParagraph across DPR 1–3; +10..+114% in the first DPR 1–2 run). Always report
  the GPU-process table and absolute ms for the
  Mac, and check one-off long frames in the trace: a 268.9 ms SkParagraph frame was a
  `CrGpuMain` task waiting 261 ms on an ANGLE worker task, not text work.
- No `/proc`: `CPU all Chrome` stays empty (INFO in the Integrity Check); trace metrics and
  FrameTiming work. `--no-dbus` is not needed.
- Retina is DPR 2 natively; `--force-device-scale-factor` still sets DPR 1 and 3. Close other
  apps, keep the Mac on power, and do not let it sleep (`caffeinate -dims` in another terminal).
- Fetch `results_mac/` back to the Cloudtop for the report; add the Mac as its own subsection
  next to the Linux results, do not mix Mac and Linux rounds in one table, and give each machine
  its own verdict (example: `assets/report_example_mac_linux.md`).
- Not checked: `build_variant.sh` on the Mac (it links Linux sysroots
  `engine/src/build/linux/debian_*`).
