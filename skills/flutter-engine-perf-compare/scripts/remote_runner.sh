#!/bin/bash
# remote_runner.sh: resumable benchmark queue on the measuring workstation.
# Lives in its root dir (PERF_ROOT, default: the script's dir) next to bin/, raw/, logs/, plans/,
# lanes.txt, txt_fonts/ and ext_fonts/; perf_remote.py enqueue uploads and starts it.
# lanes.txt lists plan files (relative to the root, whitespace-separated), highest priority
# first; it is reread every pass, so new lanes join a running queue. A later lane runs only
# while every earlier one is blocked. Plan lines: `<out> <point> <txt|ext> <reps> <filter|ALL>`
# or `BARRIER`.
# A line runs when raw/<out>.json and raw/<out>.failed are missing and bin/<point>/.complete
# exists. With STRICT=0 (default) a point that is not pushed yet is passed over until the next
# BARRIER; STRICT=1 keeps every line in order. Points listed in skip_points are ignored.
# Exits when every lane is done. Env: CPU, SIB (SMT sibling, empty = none), LOAD_MAX, BUSY_MAX
# (percent), WARMUP (seconds, 0 = none).
set -u
P=${PERF_ROOT:-$(cd "$(dirname "$0")" && pwd)}
RAW=$P/raw
LOG=$P/logs/runner.log
CPU=${CPU:-11}
SIB=${SIB-23}
LOAD_MAX=${LOAD_MAX:-3}
BUSY_MAX=${BUSY_MAX:-10}
WARMUP=${WARMUP:-3}
STRICT=${STRICT:-0}
mkdir -p "$RAW" "$P/logs"
cd "$P" || exit 1
# One runner per root: two runners on one core would corrupt each other's numbers
exec 9> "$P/.runner.lock"
flock -n 9 || { echo "ALREADY_RUNNING time=$(date -Is)" >> "$LOG"; exit 0; }

# Prints the 1-minute load average
load1() { cut -d' ' -f1 /proc/loadavg; }

# Succeeds when every cpufreq governor is `performance` (or the machine has no cpufreq)
govs_ok() {
  ! grep -qv '^performance$' /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor 2>/dev/null
}

# Prints the busy percent of CPU $1 over 1 s, from /proc/stat
busy() {
  local a b tot=0 idl i
  a=($(grep "^cpu$1 " /proc/stat)); sleep 1; b=($(grep "^cpu$1 " /proc/stat))
  for i in 1 2 3 4 5 6 7 8; do tot=$((tot + b[i] - a[i])); done
  idl=$(( (b[4] - a[4]) + (b[5] - a[5]) ))
  echo $(( tot > 0 ? 100 * (tot - idl) / tot : 0 ))
}

# Prints ok once load1 < LOAD_MAX and the core and its sibling are under BUSY_MAX percent busy,
# or timeout after 30 min; a timeout still runs the line, the log keeps quiet=timeout
wait_quiet() {
  local i
  for i in $(seq 1 180); do
    if awk -v l="$(load1)" -v m="$LOAD_MAX" 'BEGIN{exit !(l<m)}' && \
       { [ -z "$SIB" ] || [ "$(busy "$SIB")" -lt "$BUSY_MAX" ]; } && \
       [ "$(busy "$CPU")" -lt "$BUSY_MAX" ]; then
      echo ok; return
    fi
    sleep 9
  done
  echo timeout
}

# Runs one plan line ($1 out, $2 point, $3 kind, $4 reps, $5 filter) pinned to CPU and writes
# raw/<out>.json; a crash writes raw/<out>.failed (never retried), a governor change only drops
# the result so the line runs again
run_line() {
  local out=$1 pt=$2 kind=$3 reps=$4 filt=$5 bin=txt_benchmarks q lb mhz t0 rc govend
  [ "$kind" = ext ] && bin=txt_ext_benchmarks
  local d=$P/bin/$pt
  if [ ! -x "$d/$bin" ]; then
    echo "point $pt is complete but has no $bin" > "$RAW/$out.failed"
    echo "out=$out point=$pt MISSING_BINARY $bin time=$(date -Is)" >> "$LOG"
    return
  fi
  q=$(wait_quiet)
  lb=$(load1)
  mhz=$(awk -v c="$CPU" '$1=="processor"{p=$3} $1=="cpu" && $2=="MHz" && p==c{print $4}' \
    /proc/cpuinfo)
  local args=(--benchmark_repetitions="$reps" --benchmark_out="$RAW/$out.json.tmp"
              --benchmark_out_format=json)
  [ "$filt" != ALL ] && args+=(--benchmark_filter="$filt")
  [ "$kind" = ext ] && args+=(--font-dir="$P/ext_fonts")
  t0=$(date +%s)
  # The first reps after idle are slow while the core clock ramps up; spin first (guarded,
  # because timeout 0 never stops)
  if [ "$WARMUP" -gt 0 ]; then
    taskset -c "$CPU" timeout "$WARMUP" sh -c 'while :; do :; done' < /dev/null
  fi
  # stdin is the plan file inside try_plan's read loop; keep the benchmark off it
  (cd "$d" && taskset -c "$CPU" "./$bin" "${args[@]}" < /dev/null > /dev/null 2> "$RAW/$out.err")
  rc=$?
  govend=$(govs_ok && echo performance || echo CHANGED)
  if [ $rc -eq 0 ] && [ "$govend" = performance ]; then
    mv "$RAW/$out.json.tmp" "$RAW/$out.json"
  else
    rm -f "$RAW/$out.json.tmp"
    [ $rc -ne 0 ] && touch "$RAW/$out.failed"
  fi
  echo "out=$out point=$pt kind=$kind rc=$rc quiet=$q gov=$govend mhz_before=$mhz" \
    "load_before=$lb load_after=$(load1) secs=$(( $(date +%s) - t0 )) time=$(date -Is)" >> "$LOG"
}

# Runs the first runnable line of plan $1; sets ran=1 if a line ran (or the governor check
# slept), open=1 if the plan still has lines waiting for a point
try_plan() {
  local out pt kind reps filt pending=0
  ran=0; open=0
  [ -f "$1" ] || { echo "MISSING_PLAN $1 time=$(date -Is)" >> "$LOG"; return; }
  while read -r out pt kind reps filt; do
    [ -z "${out:-}" ] && continue
    case $out in \#*) continue ;; esac
    if [ "$out" = BARRIER ]; then
      [ $pending -gt 0 ] && break
      continue
    fi
    { [ -f "$RAW/$out.json" ] || [ -f "$RAW/$out.failed" ]; } && continue
    grep -qx "$pt" "$P/skip_points" 2>/dev/null && continue
    # .complete is written last by perf_remote.py push; without it the point may be partial
    if [ ! -f "$P/bin/$pt/.complete" ]; then
      pending=$((pending + 1)); open=1
      [ "$STRICT" = 1 ] && break
      continue
    fi
    open=1
    if ! govs_ok; then
      echo "ABORT_GOVERNOR out=$out time=$(date -Is)" >> "$LOG"
      sleep 300; ran=1; return
    fi
    run_line "$out" "$pt" "$kind" "$reps" "$filt"
    ran=1; return
  done < "$1"
}

echo "START root=$P cpu=$CPU sib=$SIB strict=$STRICT time=$(date -Is)" >> "$LOG"
while :; do
  any=0; any_open=0
  # Word split on purpose: older lanes.txt files list all plans on one line
  for pl in $(cat "$P/lanes.txt" 2>/dev/null); do
    try_plan "$pl"
    [ $ran -eq 1 ] && { any=1; break; }
    [ $open -eq 1 ] && any_open=1
  done
  [ $any -eq 1 ] && continue
  if [ $any_open -eq 0 ]; then echo "DONE time=$(date -Is)" >> "$LOG"; exit 0; fi
  sleep 60
done
