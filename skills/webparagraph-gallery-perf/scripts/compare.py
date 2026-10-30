#!/usr/bin/env python3
# Builds <out>/compare.md from run_bench.py output: Run Attestation, Integrity Check,
# per-round paired ratios A/B with bootstrap 95% CI per phase kind and notable phase,
# absolute medians, and the real-time input table
#
# Usage: compare.py <out> --pairs "WP/SkP=wp/skp,R12 effect=wpnew/wpold"
#        [--configs base,dpr2] [--phases resize:home,open:reply] [--md path]
import argparse
import json
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import gallery_analysis as ga  # noqa: E402

KINDS = ['scroll', 'resize', 'open', 'reopen']
DEFAULT_PHASES = 'resize:home,resize:fortnightly,open:reply,open:data-table'
METRICS = [
    ('main', 'renderer main busy'),
    ('gpu', 'GPU proc busy'),
    ('build', 'build (FrameTiming)'),
    ('cpuall', 'CPU all Chrome (noisy)'),
]
CONFIG_DPR = {'base': 1, 'dpr2': 2, 'dpr3': 3, 'cpu2': 1, 'cpu4': 1}
INVALID, WARNING, INFO = 'INVALID', 'WARNING', 'INFO'


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('out', help='run_bench.py --out dir (or one flat dir of runs)')
    ap.add_argument('--pairs', required=True, help='label=A/B,... (ratio is A/B)')
    ap.add_argument('--configs', help='default: every subdir with runs')
    ap.add_argument('--phases', default=DEFAULT_PHASES, help='notable phases, comma-separated')
    ap.add_argument('--md', help='default: <out>/compare.md')
    ap.add_argument('--load-warn', type=float, default=0.25,
                    help='warn when 1-min load average exceeds this fraction of CPU count')
    return ap.parse_args()


def has_runs(d):
    return any(f.endswith('.json') and f.rsplit('_', 1)[-1][:-5].isdigit()
               for f in os.listdir(d))


def discover_configs(out):
    if has_runs(out):
        return ['.']
    return sorted(c for c in os.listdir(out)
                  if os.path.isdir(os.path.join(out, c)) and has_runs(os.path.join(out, c)))


def total(vals, metric, kind=None, phase=None):
    """Sum of a metric over the phases of a kind (or one phase); None when unmeasured"""
    xs = [v[metric] for ph, v in vals.items()
          if (phase is not None and ph == phase)
          or (phase is None and ph.split(':')[0] == kind)]
    xs = [x for x in xs if x is not None]
    return sum(xs) if xs else None


def ratio_cell(a_runs, b_runs, sel):
    rs = []
    for rep in sorted(set(a_runs) & set(b_runs)):
        x, y = sel(a_runs[rep]), sel(b_runs[rep])
        if x is not None and y:
            rs.append(x / y)
    if not rs:
        return '—'
    if len(rs) == 1:
        return f'{rs[0]:.2f}× (n=1)'
    lo, hi = ga.paired_ci(rs)
    return (f'**{statistics.median(rs):.2f}×** [{lo:.2f}–{hi:.2f}] '
            f'({min(rs):.2f}–{max(rs):.2f}, n={len(rs)})')


def med(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def fmt(x, nd=0):
    return '—' if x is None else f'{x:.{nd}f}'


class Report:
    def __init__(self):
        self.lines = []

    def __call__(self, s=''):
        self.lines.append(s)


def attestation_section(p, att, out):
    p('## Run Attestation\n')
    if not att:
        p(f'No `run_attestation.json` in `{out}` (runs not produced by run_bench.py).\n')
        return
    a = att['args']
    load0 = att['start'].get('loadavg')
    load1 = (att.get('end') or {}).get('loadavg')
    rows = [
        ('Host', f"`{att['host']}` ({att['cpuCount']} CPUs, {att['platform']})"),
        ('Chrome', f"{att['chromeVersion']} (`{att['chrome']}`)"),
        ('Chrome flags', ' '.join(f'`{f}`' for f in a.get('chrome_flag', [])) or '—'),
        ('Driver flags', ', '.join(f for f, on in (('--require-gpu', a.get('require_gpu')),
                                                    ('--no-dbus', a.get('no_dbus')),
                                                    ('--headful', a.get('headful'))) if on)
         or '—'),
        ('GPU renderer', '; '.join(f'`{r}`' for r in att.get('gpuRenderers', [])) or '—'),
        ('Configs × rounds', f"{','.join(att['configs'])} × {att['rounds']}"),
        ('Start / end', f"{att['start']['time']} / {(att.get('end') or {}).get('time', '—')}"),
        ('Load avg start / end', f'{load0} / {load1}'),
        ('Busy processes start / end', f"{att['start'].get('busyProcesses')} / "
                                       f"{(att.get('end') or {}).get('busyProcesses')}"),
    ]
    if att.get('aborted'):
        rows.append(('Aborted', att['aborted']))
    p('| field | value |')
    p('| :--- | :--- |')
    for k, v in rows:
        p(f'| {k} | {v} |')
    p()
    p('| engine | mode | build | commit | describe | build mode | dirty | built |')
    p('| :--- | :---: | :--- | :--- | :--- | :---: | :---: | :--- |')
    for e in att['engines']:
        b = e.get('attestation') or {}
        p(f"| `{e['name']}` | {e['mode']} | `{e['build']}` | `{b.get('commit', '—')[:12]}` | "
          f"`{b.get('describe', '—')}` | {b.get('buildMode', '—')} | "
          f"{b.get('dirty', '—')} | {b.get('timestamp', '—')} |")
    p()


def integrity(att, cfg_data, args):
    issues = []
    if not att:
        issues.append((WARNING, 'no run_attestation.json: load average, Chrome version and '
                                'build provenance are unknown'))
    renderers, require_gpu = {}, bool(att and att['args'].get('require_gpu'))
    for cfg, data in cfg_data.items():
        expected = att['rounds'] if att else None
        counts = {}
        for name, runs in data.items():
            done = {r: x for r, x in runs.items() if x['done']}
            counts[name] = len(done)
            for rep, x in sorted(runs.items()):
                tag = f'`{cfg}` `{name}` r{rep}'
                a = x['att'] or {}
                # Aborted runs still attest the renderer that caused the abort
                if x['att']:
                    renderers.setdefault(a.get('webgl2Renderer', 'none'), []).append(tag)
                if not x['done']:
                    issues.append((WARNING, f'{tag}: no GALLERY_DONE, run excluded'))
                    continue
                if any(ln.startswith('GALLERY_FAILED') for ln in x['run']['lines']):
                    issues.append((WARNING, f'{tag}: GALLERY_FAILED (scenario threw)'))
                if not x['hasTrace']:
                    issues.append((WARNING, f'{tag}: no trace, trace metrics empty'))
                want_wp = x['run'].get('mode') == 'wp'
                if a.get('webParagraph') is not want_wp:
                    issues.append((INVALID, f'{tag}: webParagraph={a.get("webParagraph")} '
                                            f'but mode={x["run"].get("mode")}'))
                if want_wp and not a.get('textClusters'):
                    issues.append((INVALID, f'{tag}: TextCluster missing, WebParagraph cannot '
                                            'run as intended'))
                if not a.get('crossOriginIsolated'):
                    issues.append((WARNING, f'{tag}: crossOriginIsolated=false'))
                dpr = x['run'].get('config', {}).get('dpr')
                if dpr is not None and a.get('devicePixelRatio') not in (None, dpr):
                    issues.append((INVALID, f'{tag}: devicePixelRatio '
                                            f'{a.get("devicePixelRatio")} != --dpr {dpr}'))
            if expected is not None and len(runs) < expected:
                issues.append((WARNING, f'`{cfg}` `{name}`: {len(runs)} of {expected} runs '
                                        'present'))
            if counts[name] == 0:
                issues.append((INVALID, f'`{cfg}` `{name}`: no complete run'))
        if len(set(counts.values())) > 1:
            issues.append((WARNING, f'`{cfg}`: unequal complete runs {counts}; pairs use '
                                    'common rounds only'))
    if len(renderers) > 1:
        issues.append((INVALID, 'different GPU renderers across runs: '
                       + '; '.join(f'`{r}` ({len(t)} runs)' for r, t in renderers.items())))
    soft = [r for r in renderers if ga.is_software_gl(r)]
    if soft:
        msg = (f'software GL (`{"`, `".join(soft)}`): absolute numbers and GPU proc busy do '
               'not represent users; use for smoke tests only, never for decisions')
        issues.append((INVALID if require_gpu else WARNING, msg))
    if att:
        cpus = att.get('cpuCount') or 1
        worst, busy = 0.0, set()
        for rec in att['runs']:
            for snap in (rec.get('before'), rec.get('after')):
                if snap and snap.get('loadavg'):
                    worst = max(worst, snap['loadavg'][0])
                if snap and snap.get('busyProcesses'):
                    busy.update(snap['busyProcesses'])
            if len(rec.get('attempts', [])) > 1:
                issues.append((INFO, f"`{rec['config']}` `{rec['engine']}` r{rec['round']}: "
                                     f"retried, exits {[x['exit'] for x in rec['attempts']]}"))
        if worst > args.load_warn * cpus:
            issues.append((WARNING, f'1-min load average reached {worst:.1f} on {cpus} CPUs '
                                    f'(> {args.load_warn:.0%}): ratios are suspect'))
        if busy:
            issues.append((WARNING, f'build processes seen during runs: {sorted(busy)}'))
        modes = set()
        for e in att['engines']:
            b = e.get('attestation')
            if not b:
                issues.append((WARNING, f"`{e['name']}`: no ATTESTATION.json in the build "
                                        '(not built by build_variant.sh), commit unknown'))
                continue
            modes.add(b.get('buildMode'))
            if b.get('dirty'):
                issues.append((WARNING, f"`{e['name']}`: built from a dirty worktree"))
        if len(modes) > 1:
            issues.append((WARNING, f'engines mix build modes {sorted(modes)}'))
        if att.get('aborted'):
            issues.append((INVALID, f"run aborted: {att['aborted']}"))
    done = [x for d in cfg_data.values() for runs in d.values() for x in runs.values()
            if x['done']]
    if done and not any(x['hasCpu'] for x in done):
        issues.append((INFO, 'no /proc CPU samples (not Linux): CPU all Chrome column empty'))
    verdict = ('INVALID' if any(l == INVALID for l, _ in issues) else
               'WARNING' if any(l == WARNING for l, _ in issues) else 'OK')
    return verdict, issues


def ratio_tables(p, title, data, pairs, sel_for):
    p(f'#### {title}\n')
    p('| pair | ' + ' | '.join(lbl for _, lbl in METRICS) + ' |')
    p('| :--- |' + ' :---: |' * len(METRICS))
    for label, a, b in pairs:
        if a not in data or b not in data:
            p(f'| {label} | ' + ' | '.join('—' for _ in METRICS) + ' |')
            continue
        cells = [ratio_cell(data[a], data[b], sel_for(m)) for m, _ in METRICS]
        p(f'| {label} | ' + ' | '.join(cells) + ' |')
    p()


def config_section(p, cfg, data, pairs, phases):
    p(f'## Config `{cfg}`\n')
    p('Complete runs: ' + ', '.join(f'`{n}`={len(r)}' for n, r in sorted(data.items())) + '\n')
    p('Median of per-round ratios A/B [bootstrap 95% CI] (min–max, n rounds); '
      '< 1 means A is faster. Renderer main busy is the primary metric (±3%); GPU proc busy is '
      'primary when WebParagraph paints on the GPU; CPU all Chrome is ±20% and Linux only.\n')
    p('### Phase kinds (sum of phases)\n')
    for kind in KINDS:
        ratio_tables(p, f'`{kind}`', data, pairs,
                     lambda m, k=kind: (lambda v: total(v, m, kind=k)))
    p('### Notable phases\n')
    for ph in phases:
        ratio_tables(p, f'`{ph}`', data, pairs,
                     lambda m, q=ph: (lambda v: total(v, m, phase=q)))
    names = sorted(data)
    p('### Absolute medians, ms\n')
    p('| metric | kind | ' + ' | '.join(f'`{n}`' for n in names) + ' |')
    p('| :--- | :--- |' + ' :---: |' * len(names))
    for m, lbl in METRICS:
        for kind in KINDS:
            row = [fmt(med([total(v, m, kind=kind) for v in data[n].values()])) for n in names]
            p(f'| {lbl} | `{kind}` | ' + ' | '.join(row) + ' |')
    p()
    rt = sorted({ph for n in names for v in data[n].values() for ph in v
                 if ph.startswith('rt-')})
    if rt:
        p('### Real-time 60 Hz input\n')
        p('Median over rounds: `p90 frame ms / max vsync gap ms / frames > 16.7 ms`. The '
          '"dropped ticks" count is not shown: it counts no-op ticks after the scroll extent '
          'and frames not caused by ticks, so it is unreliable.\n')
        p('| phase | ' + ' | '.join(f'`{n}`' for n in names) + ' |')
        p('| :--- |' + ' :---: |' * len(names))
        for ph in rt:
            cells = []
            for n in names:
                vs = [v[ph] for v in data[n].values() if ph in v]
                cells.append(f"{fmt(med([x['totalP90'] for x in vs]), 1)} / "
                             f"{fmt(med([x['intervalMax'] for x in vs]))} / "
                             f"{fmt(med([x['jank16'] for x in vs]))}")
            p(f'| `{ph}` | ' + ' | '.join(cells) + ' |')
        p()


def main():
    args = parse_args()
    pairs = []
    for spec in args.pairs.split(','):
        label, ab = spec.rsplit('=', 1)
        a, b = ab.split('/')
        pairs.append((label.strip(), a.strip(), b.strip()))
    att_path = os.path.join(args.out, 'run_attestation.json')
    att = json.load(open(att_path)) if os.path.exists(att_path) else None
    configs = args.configs.split(',') if args.configs else discover_configs(args.out)
    if not configs:
        sys.exit(f'no runs under {args.out}')
    names = ([e['name'] for e in att['engines']] if att else
             sorted({n for _, a, b in pairs for n in (a, b)}))
    raw = {}
    for cfg in configs:
        d = os.path.join(args.out, cfg)
        raw[cfg] = {n: {rep: ga.load_run(path) for rep, path in ga.run_files(d, n).items()}
                    for n in names}
    verdict, issues = integrity(att, raw, args)

    p = Report()
    p(f'# Gallery perf compare: `{os.path.abspath(args.out)}`\n')
    attestation_section(p, att, args.out)
    p('## Integrity Check\n')
    p(f'**Verdict: {verdict}**\n')
    order = {INVALID: 0, WARNING: 1, INFO: 2}
    for level, msg in sorted(issues, key=lambda i: order[i[0]]):
        p(f'- **{level}** {msg}')
    if not issues:
        p('- every run finished, one hardware GPU renderer, equal run counts, idle machine')
    p()
    phases = [x for x in args.phases.split(',') if x]
    for cfg in configs:
        data = {n: {rep: x['vals'] for rep, x in runs.items() if x['done']}
                for n, runs in raw[cfg].items()}
        config_section(p, cfg if cfg != '.' else os.path.basename(args.out), data, pairs,
                       phases)
    md = args.md or os.path.join(args.out, 'compare.md')
    with open(md, 'w') as f:
        f.write('\n'.join(p.lines))
    print(f'{md}: verdict {verdict}')


if __name__ == '__main__':
    main()
