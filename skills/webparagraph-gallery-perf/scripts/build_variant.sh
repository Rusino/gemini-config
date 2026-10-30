#!/bin/bash
# Builds Flutter Gallery (assets/main_perf.dart entry point) from one commit in its own
# detached worktree and copies build/web to <out>/build_<name> with ATTESTATION.json.
# The worktree reuses gclient deps of --base-checkout through symlinks, so only
# `ninja flutter/web_sdk` and `flutter build web` run (~30 min from scratch)
#
# Usage: build_variant.sh <name> <commit> [--out DIR] [--mode release|profile]
#          [--base-checkout PATH] [--worktree PATH] [--engine-out wasm_release]
#          [--allow-dirty] [--dry-run]
set -euo pipefail

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 2 ] || usage
NAME=$1
COMMIT=$2
shift 2
SKILL=$(cd "$(dirname "$0")/.." && pwd)
SCR=$HOME/.gemini/jetski/scratch
OUT=${WP_PERF_OUT:-$SCR/wp_gallery_perf}
MODE=release
BASE=$SCR/flutter_perf_method
WT=
ENGINE_OUT=wasm_release
ALLOW_DIRTY=0
DRY=0
while [ $# -gt 0 ]; do
  case $1 in
    --out) OUT=$2; shift 2 ;;
    --mode) MODE=$2; shift 2 ;;
    --base-checkout) BASE=$2; shift 2 ;;
    --worktree) WT=$2; shift 2 ;;
    --engine-out) ENGINE_OUT=$2; shift 2 ;;
    --allow-dirty) ALLOW_DIRTY=1; shift ;;
    --dry-run) DRY=1; shift ;;
    *) echo "unknown flag $1"; usage ;;
  esac
done
case $MODE in
  # flutter_tools web/compiler_config.dart: -O4 for both, --minify only in release
  release) DART2JS="-O4 --minify (dart.vm.product=true)" ;;
  profile) DART2JS="-O4 --no-minify (dart.vm.profile=true, Timeline -> performance.mark)" ;;
  *) echo "--mode must be release or profile"; exit 2 ;;
esac
WT=${WT:-$(dirname "$BASE")/flutter_perf_$NAME}
mkdir -p "$OUT/logs"
OUT=$(cd "$OUT" && pwd)
DEST=$OUT/build_$NAME

run() {
  if [ $DRY = 1 ]; then echo "+ $*"; else "$@"; fi
}
# Runs "$@" with stdout+stderr to the log file given first
run_log() {
  local log=$1; shift
  if [ $DRY = 1 ]; then echo "+ $* > $log 2>&1"; else "$@" > "$log" 2>&1; fi
}
link() {
  [ -e "$2" ] || run ln -s "$(readlink -f "$1")" "$2"
}

[ -d "$BASE/engine/src/third_party" ] || { echo "no gclient deps in $BASE"; exit 1; }
[ -d "$BASE/engine/src/out/$ENGINE_OUT" ] || [ -d "$WT/engine/src/out/$ENGINE_OUT" ] ||
  { echo "no engine/src/out/$ENGINE_OUT in $BASE or $WT"; exit 1; }
if [ ! -d "$WT" ]; then
  run git -C "$BASE" worktree add -q --detach "$WT" "$COMMIT"
fi

# Dirty = tracked changes or untracked files other than what this script creates
dirty_files() {
  git -C "$WT" status --porcelain | grep -vE \
    -e '^\?\? (dev/integration_tests/new_gallery/(lib/main_perf\.dart|web/))' \
    -e '^\?\? engine/src/(third_party|flutter/prebuilts|flutter/buildtools|out)/?$' \
    -e '^\?\? engine/src/flutter/third_party/[^/]+/?$' \
    -e '^\?\? engine/src/build/linux/debian_[^/]+/?$' \
    -e '^\?\? third_party/ninja/?$' \
    -e '^ D third_party/ninja/' || true
}
SHA=COMMIT
if [ -d "$WT" ]; then
  SHA=$(git -C "$WT" rev-parse HEAD)
  WANT=$(git -C "$WT" rev-parse "$COMMIT^{commit}")
  if [ "$SHA" != "$WANT" ]; then
    echo "worktree $WT is at $SHA, not $COMMIT ($WANT); remove it or pass --worktree"
    exit 1
  fi
  DIRTY=$(dirty_files)
  if [ -n "$DIRTY" ]; then
    echo "worktree $WT is dirty:"; echo "$DIRTY"
    [ $ALLOW_DIRTY = 1 ] || { echo "refusing; pass --allow-dirty to build anyway"; exit 1; }
  fi
fi

if [ -d "$WT" ]; then cd "$WT/engine/src"; else echo "+ cd $WT/engine/src"; fi
link "$BASE/engine/src/third_party" third_party
for d in "$BASE"/engine/src/flutter/third_party/*; do
  link "$d" "flutter/third_party/$(basename "$d")"
done
for b in prebuilts buildtools; do
  link "$BASE/engine/src/flutter/$b" "flutter/$b"
done
for d in "$BASE"/engine/src/build/linux/debian_*; do
  link "$d" "build/linux/$(basename "$d")"
done
run mkdir -p "$WT/third_party" out
if [ ! -e "$WT/third_party/ninja/ninja" ]; then
  run rm -rf "$WT/third_party/ninja"
  run ln -s "$BASE/third_party/ninja" "$WT/third_party/ninja"
fi
# Copy through a temp dir: an interrupted copy must not pass for a complete out dir later
if [ ! -d "out/$ENGINE_OUT" ]; then
  run rm -rf "out/.partial_$ENGINE_OUT"
  run cp -a "$BASE/engine/src/out/$ENGINE_OUT" "out/.partial_$ENGINE_OUT"
  run mv "out/.partial_$ENGINE_OUT" "out/$ENGINE_OUT"
fi
echo "ninja flutter/web_sdk -> $OUT/logs/ninja_$NAME.log"
run_log "$OUT/logs/ninja_$NAME.log" timeout 1800 \
  "$WT/third_party/ninja/ninja" -C "$WT/engine/src/out/$ENGINE_OUT" flutter/web_sdk

G=$WT/dev/integration_tests/new_gallery
run mkdir -p "$G/web"
run cp "$SKILL/assets/main_perf.dart" "$G/lib/"
run cp "$SKILL/assets/flutter_bootstrap.js" "$SKILL/assets/index.html" "$G/web/"
if [ -d "$G" ]; then cd "$G"; else echo "+ cd $G"; fi
echo "flutter build web --$MODE -> $OUT/logs/gallery_build_$NAME.log"
run_log "$OUT/logs/gallery_build_$NAME.log" timeout 2400 \
  "$WT/bin/flutter" build web "--$MODE" --no-tree-shake-icons --no-web-resources-cdn \
  -t lib/main_perf.dart "--local-web-sdk=$ENGINE_OUT" "--local-engine-src-path=$WT/engine/src"
run rm -rf "$DEST"
run cp -r "$G/build/web" "$DEST"

if [ $DRY = 1 ]; then
  echo "+ write $DEST/ATTESTATION.json"
  exit 0
fi
DIRTY_NOW=$(dirty_files)
A_NAME=$NAME A_SHA=$SHA A_MODE=$MODE A_DART2JS=$DART2JS A_WT=$WT A_BASE=$BASE \
A_ENGINE_OUT=$WT/engine/src/out/$ENGINE_OUT A_DIRTY=$DIRTY_NOW \
A_DESCRIBE=$(git -C "$WT" describe --tags --always --dirty 2>/dev/null || echo unknown) \
A_SUBJECT=$(git -C "$WT" log -1 --format=%s) \
python3 - "$DEST/ATTESTATION.json" <<'EOF'
import datetime, json, os, sys
e = os.environ
dirty = e['A_DIRTY'].splitlines()
json.dump({
    'name': e['A_NAME'], 'commit': e['A_SHA'], 'describe': e['A_DESCRIBE'],
    'subject': e['A_SUBJECT'], 'buildMode': e['A_MODE'], 'dart2js': e['A_DART2JS'],
    'worktree': e['A_WT'], 'baseCheckout': e['A_BASE'], 'engineOut': e['A_ENGINE_OUT'],
    'timestamp': datetime.datetime.now().astimezone().isoformat(timespec='seconds'),
    'dirty': bool(dirty), 'dirtyFiles': dirty,
}, open(sys.argv[1], 'w'), indent=1)
EOF
echo "built $NAME ($MODE) at $(git -C "$WT" rev-parse --short HEAD) -> $DEST"
