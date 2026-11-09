#!/bin/bash
# remote_build_push.sh: build txt_benchmarks (+ txt_ext_benchmarks) on the Cloudtop with the
# Flutter checkout's Skia at one sha in one LTO mode, stage the binaries as one point and push
# it to the measuring host through perf_remote.py (ControlMaster only, atomic .complete).
#
# Usage: remote_build_push.sh --checkout <flutter monorepo root> --sha <skia sha>
#          --mode nolto|thinlto|lto [--point <name>] [--flutter-sha <sha>] [--ext-src <file>]
#          [--skia-mirror <skia repo>] [--stage <dir>] [--jobs <n>] [--no-push]
# --point defaults to <sha10>_<mode>, the name shared by every agent; a point whose remote
# bin/<point>/.complete exists is not built again. --flutter-sha runs gclient sync first.
# --ext-src adds a local-only txt_ext_benchmarks target built from that .cc file.
# Env: PERF_HOST, PERF_REMOTE_DIR, PERF_CM (see perf_remote.py), NINJA (default ninja),
# BUILD_CPUS (taskset cpu list for gn/ninja/gclient, default all).
# Exit: 0 pushed or already complete (or staged with --no-push), 1 build error, 2 bad
# arguments, 3 no live ControlMaster (perf_remote.py prints the command to open it).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
REMOTE="python3 $HERE/perf_remote.py"
CO="" SHA="" MODE="" POINT="" FLUTTER_SHA="" EXT_SRC="" MIRROR="" STAGE="" JOBS=24 PUSH=1

# Prints usage to stderr and exits 2
usage() { sed -n '2,15p' "$0" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case $1 in
    --checkout) CO=$2; shift 2 ;;
    --sha) SHA=$2; shift 2 ;;
    --mode) MODE=$2; shift 2 ;;
    --point) POINT=$2; shift 2 ;;
    --flutter-sha) FLUTTER_SHA=$2; shift 2 ;;
    --ext-src) EXT_SRC=$2; shift 2 ;;
    --skia-mirror) MIRROR=$2; shift 2 ;;
    --stage) STAGE=$2; shift 2 ;;
    --jobs) JOBS=$2; shift 2 ;;
    --no-push) PUSH=0; shift ;;
    *) usage ;;
  esac
done
[ -n "$CO" ] && [ -n "$SHA" ] || usage
case $MODE in nolto|thinlto|lto) ;; *) usage ;; esac
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || { echo "--sha must be a full 40-hex Skia sha" >&2; exit 2; }
CO=$(cd "$CO" && pwd) || exit 2
POINT=${POINT:-${SHA:0:10}_$MODE}
STAGE=${STAGE:-$CO/perf_stage}
E=$CO/engine/src
[ -d "$E/flutter" ] || { echo "$CO is not a Flutter monorepo checkout" >&2; exit 2; }
[ -z "$EXT_SRC" ] || [ -f "$EXT_SRC" ] || { echo "no such --ext-src $EXT_SRC" >&2; exit 2; }
RUN="nice -n 19"
[ -n "${BUILD_CPUS:-}" ] && RUN="$RUN taskset -c $BUILD_CPUS"
NINJA=${NINJA:-ninja}
export DEPOT_TOOLS_UPDATE=0
# Prints a progress tag with a timestamp, for the run_command notification log
log() { echo "$* $(date -Is)"; }

if [ $PUSH = 1 ]; then
  # Fail before a long build if the push could not happen anyway
  $REMOTE check || exit $?
  if $REMOTE has "$POINT"; then log "ALREADY_COMPLETE $POINT"; exit 0; fi
fi

# Prints the Skia sha recorded in a staged point's META
staged_sha() { sed -n 's/^skia_sha=//p' "$STAGE/$1/META" 2>/dev/null; }

if [ -f "$STAGE/$POINT/SHA256SUMS" ] && [ "$(staged_sha "$POINT")" = "$SHA" ]; then
  log "REUSE_STAGE $POINT"
else
  # One build per checkout: the thinlto flag patch and the Skia checkout are shared state
  exec 9> "$CO/.perf_build.lock"
  flock -n 9 || { echo "another remote_build_push.sh is building in $CO" >&2; exit 1; }
  if [ -n "$FLUTTER_SHA" ] && [ "$(git -C "$CO" rev-parse HEAD)" != "$FLUTTER_SHA" ]; then
    log "SYNC $FLUTTER_SHA"
    (cd "$CO" && $RUN gclient sync -r ".@$FLUTTER_SHA" -D --force --no-history -j8) || exit 1
  fi
  cd "$E" || exit 1
  if [ -d flutter/txt/benchmarks ]; then TXT=flutter/txt; else TXT=flutter/third_party/txt; fi
  SK=flutter/third_party/skia
  PBS=$TXT/src/skia/paragraph_builder_skia.cc
  CFG=build/config/compiler/BUILD.gn
  SK_ORIG=$(git -C "$SK" rev-parse HEAD)
  EXT_DST=$TXT/benchmarks/skparagraph_ext_benchmarks.cc
  # restore() resets these files with git checkout, which would drop someone's local edits
  git -C "$E" diff --quiet -- "$PBS" "$TXT/BUILD.gn" "$CFG" || {
    echo "local edits in $PBS, $TXT/BUILD.gn or $CFG; commit or stash them" >&2; exit 1; }

  # Restores every file this script patches and the Skia checkout, also on failure or Ctrl-C
  restore() {
    local f
    for f in "$PBS" "$TXT/BUILD.gn" "$CFG"; do git -C "$E" checkout -q -- "$f" 2>/dev/null; done
    [ -n "$EXT_SRC" ] && rm -f "$E/$EXT_DST"
    git -C "$E/$SK" checkout -q -f "$SK_ORIG" 2>/dev/null
  }
  trap restore EXIT

  if ! git -C "$SK" cat-file -e "${SHA}^{commit}" 2>/dev/null; then
    { [ -n "$MIRROR" ] && git -C "$SK" fetch -q "$MIRROR" "$SHA"; } || \
      $RUN git -C "$SK" fetch -q --depth=1 origin "$SHA" || { log "FETCH_FAIL $SHA"; exit 1; }
  fi
  git -C "$SK" checkout -q -f "$SHA" || exit 1
  # paragraph_builder_skia.cc at this Flutter rev may call setters older Skia lacks
  for m in setRenderSoftHyphens setSoftHyphenString; do
    grep -q "$m" "$SK/modules/skparagraph/include/ParagraphStyle.h" || \
      sed -i "s/skia\.$m/\/\/ shim: no $m /" "$PBS"
  done

  TARGETS=("$TXT:txt_benchmarks")
  if [ -n "$EXT_SRC" ]; then
    cp "$EXT_SRC" "$EXT_DST"
    TARGETS+=("$TXT:txt_ext_benchmarks")
    utils=""
    grep -q 'skia_component("utils")' "$SK/modules/skparagraph/BUILD.gn" && \
      utils='      "//flutter/skia/modules/skparagraph:utils",'
    {
      echo ''
      echo '# Local-only A/B target (remote_build_push.sh), not part of upstream'
      echo 'if (enable_unittests) {'
      echo '  executable("txt_ext_benchmarks") {'
      echo '    testonly = true'
      echo '    sources = [ "benchmarks/skparagraph_ext_benchmarks.cc" ]'
      echo '    deps = ['
      echo '      ":txt",'
      echo '      "//flutter/fml",'
      echo '      "//flutter/skia/modules/skparagraph",'
      [ -n "$utils" ] && echo "$utils"
      echo '      "//flutter/third_party/benchmark",'
      echo '    ]'
      echo '  }'
      echo '}'
    } >> "$TXT/BUILD.gn"
  fi

  # Flutter's gn has only --lto/--no-lto; ThinLTO is the same -flto switch with =thin
  if [ "$MODE" = thinlto ]; then
    sed -i 's/"-flto"/"-flto=thin"/' "$CFG"
    grep -q '"-flto=thin"' "$CFG" || { log "THINLTO_PATCH_FAIL $CFG"; exit 1; }
  fi
  OD=out/perf_$MODE
  GNFLAG=""
  [ "$MODE" = nolto ] && GNFLAG=--no-lto

  # Generates OD if needed and builds TARGETS in it; returns ninja's status
  build() {
    if [ ! -f "$OD/build.ninja" ]; then
      $RUN ./flutter/tools/gn --runtime-mode release --prebuilt-dart-sdk --no-rbe --no-goma \
        $GNFLAG --target-dir "${OD#out/}" || return 1
    fi
    $RUN "$NINJA" -j"$JOBS" -C "$OD" "${TARGETS[@]}"
  }
  t0=$(date +%s)
  if ! build; then
    # Incremental builds across libc++ ABI changes leave stale LTO objects; retry clean once
    log "BUILD_FAIL_INCREMENTAL $POINT, retry clean"
    rm -rf "$OD"
    build || { log "BUILD_FAIL $POINT"; exit 1; }
  fi
  log "BUILT $POINT $(grep -E '^enable_lto' "$OD/args.gn" | tr -d ' ') $(( $(date +%s) - t0 ))s"

  d=$STAGE/$POINT
  rm -rf "$d.tmp" && mkdir -p "$d.tmp/assets"
  cp "$OD/txt_benchmarks" "$OD/icudtl.dat" "$d.tmp/"
  [ -n "$EXT_SRC" ] && cp "$OD/txt_ext_benchmarks" "$d.tmp/"
  fx=$(strings "$OD/txt_benchmarks" | grep -m1 -E '^/.*/gen/flutter/(third_party/)?txt/assets$')
  [ -n "$fx" ] && cp "$fx"/*.ttf "$d.tmp/assets/"
  {
    echo "name=$POINT"
    echo "mode=$MODE"
    echo "flutter_sha=$(git -C "$CO" rev-parse HEAD)"
    echo "skia_sha=$SHA"
    echo "fixtures_path=$fx"
    echo "clang=$(./flutter/buildtools/linux-x64/clang/bin/clang++ --version | head -1)"
  } > "$d.tmp/META"
  (cd "$d.tmp" && sha256sum txt_benchmarks txt_ext_benchmarks icudtl.dat assets/*.ttf \
    2>/dev/null) > "$d.tmp/SHA256SUMS"
  rm -rf "$d" && mv "$d.tmp" "$d"
  # Unstripped copies stay local for symbolizing profiles of this point
  if [ -f "$OD/exe.unstripped/txt_benchmarks" ]; then
    mkdir -p "$STAGE/unstripped/$POINT"
    cp "$OD"/exe.unstripped/txt*_benchmarks "$STAGE/unstripped/$POINT/"
  fi
  log "STAGED $POINT"
fi

[ $PUSH = 1 ] || exit 0
$REMOTE push "$STAGE/$POINT" "$POINT" || { log "PUSH_FAIL $POINT"; exit 1; }
log "PUSHED $POINT"
