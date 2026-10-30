#!/bin/bash
# Ships the benchmark to a remote (GPU) host and back. Bundle layout on the host:
#   <dir>/scripts/*.py, <dir>/builds/<basename of each build dir>/, <dir>/run.log
#
#   remote.sh pack   --builds DIR[,DIR...] [--tgz FILE]
#   remote.sh start  --host H [--dir /tmp/wp_gallery_perf] [--tgz FILE] -- <run_bench.py args>
#   remote.sh status --host H [--dir ...]
#   remote.sh fetch  --host H [--dir ...] [--results results] --dest LOCAL_DIR
#
# In start, refer to builds as builds/<name> and pass --out results (relative to <dir>)
set -euo pipefail

usage() { sed -n '2,10p' "$0" | sed 's/^# \{0,1\}//'; exit 2; }
[ $# -ge 1 ] || usage
CMD=$1
shift
SCRIPTS=$(cd "$(dirname "$0")" && pwd)
HOST=
DIR=/tmp/wp_gallery_perf
TGZ=/tmp/wp_gallery_perf_bundle.tgz
BUILDS=
RESULTS=results
DEST=
while [ $# -gt 0 ]; do
  case $1 in
    --host) HOST=$2; shift 2 ;;
    --dir) DIR=$2; shift 2 ;;
    --tgz) TGZ=$2; shift 2 ;;
    --builds) BUILDS=$2; shift 2 ;;
    --results) RESULTS=$2; shift 2 ;;
    --dest) DEST=$2; shift 2 ;;
    --) shift; break ;;
    *) echo "unknown flag $1"; usage ;;
  esac
done
need_host() { [ -n "$HOST" ] || { echo "--host is required"; exit 2; }; }

case $CMD in
  pack)
    [ -n "$BUILDS" ] || { echo "--builds is required"; exit 2; }
    STAGE=$(mktemp -d)
    trap 'rm -rf "$STAGE"' EXIT
    mkdir -p "$STAGE/bundle/scripts" "$STAGE/bundle/builds"
    cp "$SCRIPTS"/*.py "$STAGE/bundle/scripts/"
    IFS=, read -ra LIST <<< "$BUILDS"
    for b in "${LIST[@]}"; do
      [ -f "$b/main.dart.js" ] || { echo "$b is not a build/web copy"; exit 1; }
      cp -r "$b" "$STAGE/bundle/builds/$(basename "$b")"
    done
    tar -C "$STAGE/bundle" -czf "$TGZ" .
    echo "packed $(du -h "$TGZ" | cut -f1) -> $TGZ (builds: $(ls "$STAGE/bundle/builds"))"
    ;;
  start)
    need_host
    [ $# -gt 0 ] || { echo "pass run_bench.py args after --"; exit 2; }
    [ -f "$TGZ" ] || { echo "no $TGZ; run pack first"; exit 1; }
    ssh "$HOST" "mkdir -p '$DIR'"
    scp -q "$TGZ" "$HOST:$DIR/bundle.tgz"
    ARGS=$(printf ' %q' "$@")
    # setsid + nohup keep the run alive after the SSH session ends. Braces background only the
    # bench: a bare `a && b && c &` backgrounds the whole list in a subshell that holds the SSH
    # channel open until the run ends
    ssh "$HOST" "cd '$DIR' && tar -xzf bundle.tgz && \
      { setsid nohup python3 scripts/run_bench.py$ARGS > run.log 2>&1 < /dev/null & }"
    sleep 5
    ssh "$HOST" "tail -5 '$DIR/run.log'"
    echo "started on $HOST:$DIR; check with: $0 status --host $HOST --dir $DIR"
    ;;
  status)
    need_host
    # Brackets keep pgrep from matching the remote bash -c line that carries this pattern
    ssh "$HOST" "pgrep -af '[r]un_bench.py|[d]rive_gallery.py' || echo 'not running'; \
      uptime; tail -8 '$DIR/run.log'"
    ;;
  fetch)
    need_host
    [ -n "$DEST" ] || { echo "--dest is required"; exit 2; }
    mkdir -p "$DEST"
    scp -rq "$HOST:$DIR/$RESULTS/." "$DEST/"
    scp -q "$HOST:$DIR/run.log" "$DEST/run.log"
    echo "fetched $HOST:$DIR/$RESULTS -> $DEST"
    ;;
  *) usage ;;
esac
