---
name: webparagraph-gallery-perf
description: >-
  Measure Flutter Web WebParagraph (WP) vs SkParagraph (SkP), or WP branch vs WP branch, on a
  release (or profile) build of Flutter Gallery in real Chrome: build variants from commits,
  interleaved rotated rounds with Chrome traces, paired per-round ratios with bootstrap 95% CI,
  and `compare.md` with Run Attestation and Integrity Check. Trigger when asked to
  "замерь производительность WebParagraph", "сравни производительность веток",
  "прогони Gallery бенчмарк", "gallery perf", "WP vs SkP perf", or to compare profile vs release.
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
  gclient deps and `engine/src/out/wasm_release`. Deps are symlinked, `out/wasm_release` is
  copied once.
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

## Mac (not yet validated)

- Chrome for Testing:
  `chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing`;
  system Chrome default is `/Applications/Google Chrome.app/Contents/MacOS/Google Chrome`.
- No `/proc`: `CPU all Chrome` stays empty (INFO in the Integrity Check); trace metrics and
  FrameTiming work. `--no-dbus` is not needed.
- Not checked: whether headless Chrome uses the Metal GPU without extra flags (look at the
  `GALLERY_ATTESTATION` renderer and use `--require-gpu`), and whether `build_variant.sh` works
  without the Linux sysroot links (`engine/src/build/linux/debian_*`).
