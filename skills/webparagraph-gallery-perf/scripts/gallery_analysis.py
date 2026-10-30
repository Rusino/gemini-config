#!/usr/bin/env python3
# Shared analysis of drive_gallery.py runs: trace busy time, /proc CPU deltas,
# GALLERY_PHASE frame stats, attestation lines, and the paired bootstrap CI
import collections
import glob
import gzip
import json
import os
import random
import statistics

# main_perf.dart emits performance.mark('R31:start:<phase>') / 'R31:end:<phase>'
MARK_PREFIX = 'R31:'
SOFTWARE_GL = ('swiftshader', 'llvmpipe', 'software', 'none', 'masked')


def union_ms(intervals, lo, hi):
    tot, cur_s, cur_e = 0.0, None, None
    for s, e in sorted(intervals):
        s, e = max(s, lo), min(e, hi)
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                tot += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        tot += cur_e - cur_s
    return tot / 1000.0


def analyze_trace(path):
    """Per phase: busy ms of renderer main, other renderer threads, GPU process, browser"""
    with gzip.open(path, 'rt') as f:
        data = json.load(f)
    ev = data['traceEvents'] if isinstance(data, dict) else data
    pname, tname = {}, {}
    for e in ev:
        if e.get('ph') == 'M':
            if e['name'] == 'process_name':
                pname[e['pid']] = e['args']['name']
            elif e['name'] == 'thread_name':
                tname[(e['pid'], e['tid'])] = e['args']['name']
    marks = {}
    for e in ev:
        n = e.get('name', '')
        if n.startswith(MARK_PREFIX) and e.get('ph') in ('R', 'I', 'n', 'i'):
            marks.setdefault(n, e['ts'])
    tasks = collections.defaultdict(list)
    for e in ev:
        if e.get('ph') == 'X' and e.get('cat') == 'toplevel' and 'dur' in e:
            tasks[(e['pid'], e['tid'])].append((e['ts'], e['ts'] + e['dur']))
    # The renderer that emitted our marks is the page renderer
    mark_pids = {e['pid'] for e in ev if e.get('name', '').startswith(MARK_PREFIX)}
    start = MARK_PREFIX + 'start:'
    out = {}
    for n, ts in marks.items():
        if not n.startswith(start):
            continue
        phase = n[len(start):]
        end = marks.get(MARK_PREFIX + 'end:' + phase)
        if end is None:
            continue
        r = {'rendererMain': 0.0, 'rendererOther': 0.0, 'gpu': 0.0, 'browser': 0.0}
        for (pid, tid), iv in tasks.items():
            pn = pname.get(pid, '')
            tn = tname.get((pid, tid), '')
            ms = union_ms(iv, ts, end)
            if pid in mark_pids:
                r['rendererMain' if tn == 'CrRendererMain' else 'rendererOther'] += ms
            elif 'GPU' in pn:
                r['gpu'] += ms
            elif pn == 'Browser':
                r['browser'] += ms
        out[phase] = r
    return out


def analyze_cpu(run):
    """Per phase: CPU ms per Chrome process type from /proc samples (empty off Linux)"""
    marks = {}
    for t, what in run['marks']:
        marks[what] = t
    samples = run['cpu']
    out = {}

    def at(t):
        best = min(samples, key=lambda s: abs(s[0] - t))
        return best[1]

    for what, t in marks.items():
        if not what.startswith('start:'):
            continue
        phase = what[6:]
        te = marks.get('end:' + phase)
        if te is None or not samples:
            continue
        a, b = at(t), at(te)
        out[phase] = {k: b.get(k, 0.0) - a.get(k, 0.0) for k in set(a) | set(b)}
    return out


def phases_from_lines(run):
    out = {}
    for line in run['lines']:
        if line.startswith('GALLERY_PHASE '):
            j = json.loads(line.split(' ', 1)[1])
            out[j['phase']] = j
    return out


def attestation(run):
    for line in run['lines']:
        if line.startswith('GALLERY_ATTESTATION '):
            return json.loads(line.split(' ', 1)[1])
    return None


def is_done(run):
    return any(line.startswith('GALLERY_DONE') for line in run['lines'])


def is_software_gl(renderer):
    r = str(renderer or 'none').lower()
    return any(s in r for s in SOFTWARE_GL)


def run_files(d, name):
    """{round: path} of <d>/<name>_<round>.json"""
    out = {}
    for p in glob.glob(os.path.join(d, f'{name}_*.json')):
        tail = os.path.basename(p)[len(name) + 1:-len('.json')]
        if tail.isdigit():
            out[int(tail)] = p
    return out


def load_run(path):
    """Raw run plus per-phase values {phase: {main, gpu, cpuall, build, ...}}"""
    with open(path) as f:
        run = json.load(f)
    tr = path[:-len('.json')] + '.trace.json.gz'
    trace = analyze_trace(tr) if os.path.exists(tr) else {}
    cpu = analyze_cpu(run)
    frames = phases_from_lines(run)
    vals = {}
    for ph, fr in frames.items():
        t = trace.get(ph, {})
        vals[ph] = {
            'main': t.get('rendererMain'),
            'gpu': t.get('gpu'),
            'cpuall': sum(cpu[ph].values()) if ph in cpu else None,
            'build': fr['buildSum'],
            'frames': fr['frames'],
            'jank16': fr.get('jank16'),
            'totalP90': fr.get('totalP90'),
            'intervalMax': fr.get('intervalMax'),
            'steps': fr.get('steps'),
        }
    return {'run': run, 'vals': vals, 'done': is_done(run), 'att': attestation(run),
            'hasTrace': bool(trace), 'hasCpu': bool(cpu)}


def paired_ci(ratios, n=4000, seed=1):
    """Bootstrap 95% CI of the median of per-round ratios (resampling rounds, not runs)"""
    rnd = random.Random(seed)
    meds = sorted(statistics.median(rnd.choices(ratios, k=len(ratios))) for _ in range(n))
    return meds[int(0.025 * n)], meds[int(0.975 * n) - 1]
