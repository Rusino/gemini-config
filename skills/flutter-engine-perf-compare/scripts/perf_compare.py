#!/usr/bin/env python3
import argparse
import subprocess
import json
import os
import sys

def run_cmd(cmd, cwd=None, check=True):
    print(f"Running: {cmd} in {cwd or '.'}")
    res = subprocess.run(cmd, shell=True, cwd=cwd, text=True, capture_output=True)
    if check and res.returncode != 0:
        print(f"Error running {cmd}")
        print("STDOUT:", res.stdout)
        print("STDERR:", res.stderr)
        sys.exit(res.returncode)
    return res.stdout.strip()

def resolve_commit(engine_dir, ref_or_date):
    flutter_dir = os.path.join(engine_dir, "flutter")
    # If it looks like a date (contains spaces or common date formats), try to find a commit before that date
    if " " in ref_or_date or "ago" in ref_or_date.lower() or "-" in ref_or_date:
        # Check if it's a valid git ref first
        res = subprocess.run(f"git rev-parse --verify {ref_or_date}", shell=True, cwd=flutter_dir, capture_output=True)
        if res.returncode == 0:
            return res.stdout.decode().strip()
        
        # Otherwise, treat as a date
        cmd = f"git rev-list -n 1 --before='{ref_or_date}' HEAD"
        sha = run_cmd(cmd, cwd=flutter_dir)
        if not sha:
            print(f"Could not find a commit for date/ref: {ref_or_date}")
            sys.exit(1)
        return sha
    else:
        return run_cmd(f"git rev-parse {ref_or_date}", cwd=flutter_dir)

def run_benchmark(engine_dir, build_dir, filter_str, out_file):
    cmd = f"./{build_dir}/txt_benchmarks --benchmark_format=json"
    if filter_str:
        cmd += f" --benchmark_filter={filter_str}"
    
    print(f"Running benchmarks -> {out_file}...")
    res = subprocess.run(cmd, shell=True, cwd=engine_dir, text=True, capture_output=True)
    if res.returncode != 0:
        print("Benchmark failed.")
        print(res.stderr)
    
    # Save the output
    with open(out_file, "w") as f:
        f.write(res.stdout)

def generate_report(baseline_file, target_file):
    try:
        with open(baseline_file) as f:
            base_data = json.load(f)
        with open(target_file) as f:
            target_data = json.load(f)
    except Exception as e:
        print(f"Error reading JSON output: {e}")
        return

    base_bench = {b["name"]: b for b in base_data.get("benchmarks", [])}
    target_bench = {b["name"]: b for b in target_data.get("benchmarks", [])}

    print("\n# Performance Comparison\n")
    print("| Benchmark | CPU Time (Base) | CPU Time (Target) | Delta % |")
    print("|---|---|---|---|")

    for name, t_bench in target_bench.items():
        if name not in base_bench:
            continue
        
        b_bench = base_bench[name]
        
        b_time = b_bench.get("cpu_time", 0)
        t_time = t_bench.get("cpu_time", 0)
        
        if b_time > 0:
            delta = ((t_time - b_time) / b_time) * 100
        else:
            delta = 0.0

        # Optional memory/counters logic if present
        # Google Benchmark exposes user counters in the dict.
        # e.g., b_bench.get("bytes_per_second") or custom counters like "PeakMemory".
        
        b_str = f"{b_time:.2f} {b_bench.get('time_unit', 'ns')}"
        t_str = f"{t_time:.2f} {t_bench.get('time_unit', 'ns')}"
        d_str = f"{delta:+.2f}%"
        
        print(f"| {name} | {b_str} | {t_str} | {d_str} |")

def main():
    parser = argparse.ArgumentParser(description="A/B test txt_benchmarks in Flutter Engine")
    parser.add_argument("--engine-dir", required=True, help="Path to engine/src directory")
    parser.add_argument("--baseline", required=True, help="Baseline commit, branch, or date (e.g. '2 weeks ago')")
    parser.add_argument("--target", required=True, help="Target commit, branch, or date")
    parser.add_argument("--filter", default="", help="Benchmark filter (e.g., Paragraph)")
    parser.add_argument("--build-dir", default="out/host_release", help="Ninja output directory")
    
    args = parser.parse_args()

    engine_dir = os.path.abspath(args.engine_dir)
    flutter_dir = os.path.join(engine_dir, "flutter")

    if not os.path.isdir(flutter_dir):
        print(f"Error: {flutter_dir} does not exist.")
        sys.exit(1)

    # Save current branch/HEAD
    orig_head = run_cmd("git rev-parse HEAD", cwd=flutter_dir)
    print(f"Original HEAD is {orig_head}")

    try:
        base_sha = resolve_commit(engine_dir, args.baseline)
        target_sha = resolve_commit(engine_dir, args.target)

        print(f"Baseline resolved to: {base_sha}")
        print(f"Target resolved to:   {target_sha}")

        # Baseline
        run_cmd(f"git checkout {base_sha}", cwd=flutter_dir)
        run_cmd("gclient sync -D", cwd=engine_dir)
        run_cmd(f"ninja -C {args.build_dir} txt_benchmarks", cwd=engine_dir)
        run_benchmark(engine_dir, args.build_dir, args.filter, "baseline.json")

        # Target
        run_cmd(f"git checkout {target_sha}", cwd=flutter_dir)
        run_cmd("gclient sync -D", cwd=engine_dir)
        run_cmd(f"ninja -C {args.build_dir} txt_benchmarks", cwd=engine_dir)
        run_benchmark(engine_dir, args.build_dir, args.filter, "target.json")

        # Report
        generate_report("baseline.json", "target.json")

    finally:
        # Cleanup
        print("Restoring original HEAD...")
        run_cmd(f"git checkout {orig_head}", cwd=flutter_dir, check=False)
        run_cmd("gclient sync -D", cwd=engine_dir, check=False)

if __name__ == "__main__":
    main()
