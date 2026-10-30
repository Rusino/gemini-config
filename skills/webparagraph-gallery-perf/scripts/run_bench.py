#!/usr/bin/env python3
# Runs Gallery builds interleaved: per config and round every engine runs once, the
# engine order rotates per round against drift, each run gets a fresh Chrome profile
# and one retry. Writes <out>/<config>/<engine>_<round>.{json,trace.json.gz} and
# <out>/run_attestation.json (rewritten after every run, so partial runs stay usable)
#
# Usage: run_bench.py --engines skp:build_master:skp,wp:build_master:wp \
#          --configs base --rounds 6 --out results [--require-gpu] [--chrome PATH]
import argparse
import datetime
import json
import os
import platform
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gallery_analysis as ga  # noqa: E402

CONFIGS = {
    'base': ['--dpr', '1'],
    'dpr2': ['--dpr', '2'],
    'dpr3': ['--dpr', '3'],
    'cpu2': ['--dpr', '1', '--cpu-throttle', '2'],
    'cpu4': ['--dpr', '1', '--cpu-throttle', '4'],
}
DEFAULT_CHROME = {
    'Darwin': '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
    'Linux': '/usr/bin/google-chrome',
}
# Processes whose presence means the machine is busy building, not idle
BUSY_COMM = ('ninja', 'clang', 'clang++', 'cc1plus', 'ld.lld', 'dart', 'dart2js',
             'dartaotruntime', 'felt', 'gn')


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--engines', required=True,
                    help='name:build_dir:mode,... (mode wp or skp; build_dir is build/web)')
    ap.add_argument('--configs', default='base', help=','.join(CONFIGS))
    ap.add_argument('--rounds', type=int, default=6)
    ap.add_argument('--out', required=True)
    ap.add_argument('--chrome', default=os.environ.get('CHROME'))
    ap.add_argument('--require-gpu', action='store_true',
                    help='abort unless WebGL runs on a hardware GPU')
    ap.add_argument('--chrome-flag', action='append', default=[],
                    help='extra Chrome flag, e.g. --chrome-flag=--use-angle=gl-egl')
    ap.add_argument('--no-dbus', action='store_true',
                    help='hide the session DBus from Chrome (needed over SSH)')
    ap.add_argument('--headful', action='store_true')
    ap.add_argument('--timeout', type=int, default=900, help='per run, seconds')
    ap.add_argument('--sleep', type=float, default=3.0, help='pause between runs, seconds')
    return ap.parse_args()


def loadavg():
    return [round(x, 2) for x in os.getloadavg()] if hasattr(os, 'getloadavg') else None


def busy_processes():
    if not os.path.isdir('/proc'):
        return None
    out = {}
    for d in os.listdir('/proc'):
        if not d.isdigit():
            continue
        try:
            with open(f'/proc/{d}/comm') as f:
                comm = f.read().strip()
        except OSError:
            continue
        if comm in BUSY_COMM:
            out[comm] = out.get(comm, 0) + 1
    return out


def snapshot():
    return {'time': datetime.datetime.now().isoformat(timespec='seconds'),
            'loadavg': loadavg(), 'busyProcesses': busy_processes()}


def chrome_version(chrome):
    try:
        r = subprocess.run([chrome, '--version'], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=30)
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return f'unknown ({e!r})'


def read_build_attestation(build):
    p = os.path.join(build, 'ATTESTATION.json')
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def main():
    args = parse_args()
    chrome = args.chrome or DEFAULT_CHROME.get(platform.system())
    if not chrome or not os.path.exists(chrome):
        sys.exit(f'Chrome not found: {chrome!r}; pass --chrome')
    engines = []
    for spec in args.engines.split(','):
        name, build, mode = spec.split(':')
        if mode not in ('wp', 'skp'):
            sys.exit(f'bad mode {mode!r} in {spec!r}')
        build = os.path.abspath(build)
        if not os.path.exists(os.path.join(build, 'main.dart.js')):
            sys.exit(f'{build} has no main.dart.js; pass the build/web copy')
        engines.append((name, build, mode))
    configs = args.configs.split(',')
    for c in configs:
        if c not in CONFIGS:
            sys.exit(f'unknown config {c!r}; known: {",".join(CONFIGS)}')
    os.makedirs(args.out, exist_ok=True)

    att = {
        'host': socket.getfqdn(),
        'platform': platform.platform(),
        'cpuCount': os.cpu_count(),
        'python': sys.version.split()[0],
        'chrome': chrome,
        'chromeVersion': chrome_version(chrome),
        'args': vars(args),
        'engines': [{'name': n, 'build': b, 'mode': m, 'attestation': read_build_attestation(b)}
                    for n, b, m in engines],
        'configs': configs,
        'rounds': args.rounds,
        'start': snapshot(),
        'runs': [],
        'gpuRenderers': [],
        'aborted': None,
        'end': None,
    }
    att_path = os.path.join(args.out, 'run_attestation.json')

    def save():
        with open(att_path, 'w') as f:
            json.dump(att, f, indent=1)

    common = ['--chrome', chrome, '--timeout', str(args.timeout)]
    common += [f'--chrome-flag={f}' for f in args.chrome_flag]
    if args.require_gpu:
        common.append('--require-gpu')
    if args.no_dbus:
        common.append('--no-dbus')
    if args.headful:
        common.append('--headful')

    save()
    print(f'chrome: {att["chromeVersion"]}  load: {att["start"]["loadavg"]}  '
          f'busy: {att["start"]["busyProcesses"]}', flush=True)
    for cfg in configs:
        out = os.path.join(args.out, cfg)
        os.makedirs(out, exist_ok=True)
        print(f'=== {cfg} {time.strftime("%H:%M:%S")}', flush=True)
        for rep in range(1, args.rounds + 1):
            k = rep % len(engines)
            for name, build, mode in engines[k:] + engines[:k]:
                prefix = os.path.join(out, f'{name}_{rep}')
                cmd = [sys.executable, os.path.join(HERE, 'drive_gallery.py'), build, prefix,
                       mode, *common, *CONFIGS[cfg]]
                rec = {'config': cfg, 'round': rep, 'engine': name, 'mode': mode,
                       'before': snapshot(), 'attempts': []}
                for attempt in (1, 2):
                    t = time.monotonic()
                    r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True)
                    bad = [ln for ln in r.stdout.splitlines()
                           if ln.startswith(('ABORT', 'STALL', 'GALLERY_FAILED',
                                             'PAGE_EXCEPTION'))]
                    rec['attempts'].append({'exit': r.returncode, 'seconds':
                                            round(time.monotonic() - t, 1),
                                            'problems': [b[:300] for b in bad]})
                    print(f'{cfg} r{rep} {name} exit {r.returncode} attempt {attempt} '
                          f'{" ".join(bad)[:200]}', flush=True)
                    if any(ln.startswith('ABORT') for ln in bad):
                        rec['after'] = snapshot()
                        att['runs'].append(rec)
                        att['aborted'] = f'no hardware GPU ({cfg} r{rep} {name})'
                        att['end'] = snapshot()
                        save()
                        sys.exit('Aborted: no hardware GPU')
                    if r.returncode == 0:
                        break
                    time.sleep(5)
                rec['after'] = snapshot()
                rec['ok'] = rec['attempts'][-1]['exit'] == 0
                if os.path.exists(prefix + '.json'):
                    with open(prefix + '.json') as f:
                        a = ga.attestation(json.load(f))
                    if a:
                        rec['webgl2Renderer'] = a.get('webgl2Renderer')
                        if a.get('webgl2Renderer') not in att['gpuRenderers']:
                            att['gpuRenderers'].append(a.get('webgl2Renderer'))
                att['runs'].append(rec)
                save()
                time.sleep(args.sleep)
    att['end'] = snapshot()
    save()
    print(f'=== all done {time.strftime("%H:%M:%S")}  load: {att["end"]["loadavg"]}',
          flush=True)
    print(f'next: python3 {os.path.join(HERE, "compare.py")} {args.out} --pairs "A/B=a/b"')


if __name__ == '__main__':
    main()
