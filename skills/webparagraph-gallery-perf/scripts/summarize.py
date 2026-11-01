#!/usr/bin/env python3
# Human-readable Russian tables for the WebParagraph vs SkParagraph report, from one or more
# run_bench.py output dirs: main-thread table with verdicts, GPU range, 60 Hz table with long
# frames, optional A/B effect table (2% rule) and tracing overhead table. Numbers use format A:
# bare signed integer percents (+25 = 25% longer than the reference)
#
# Usage: summarize.py DIR [DIR...] --wp c1 --skp skp [--ab "c1/c0"] [--ab-labels "с ним,без него"]
#          [--ab-title "вклад FillTextRuns"] [--trace "wpt/wpn,skpt/skpn"] [--md out.md]
# For every config the first DIR that has both engines of a pair is used, so DPR 1 and DPR 2/3
# may come from different runs
import argparse
import json
import math
import os
import statistics
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import compare  # noqa: E402
import gallery_analysis as ga  # noqa: E402

CONFIG_ORDER = ['base', 'dpr2', 'dpr3', 'cpu2', 'cpu4']
CONFIG_TITLE = {'base': 'DPR 1', 'dpr2': 'DPR 2', 'dpr3': 'DPR 3', 'cpu2': 'CPU ×2',
                'cpu4': 'CPU ×4'}
# (column title, kind or phase selector)
SCENARIOS = [('`open`', ('kind', 'open')), ('`reopen`', ('kind', 'reopen')),
             ('`scroll`', ('kind', 'scroll')), ('`resize`', ('kind', 'resize')),
             ('`resize:home` (самая тяжёлая страница)', ('phase', 'resize:home'))]
RT_PHASES = [('Прокрутка, главная', 'rt-scroll:home'),
             ('Изменение ширины, главная', 'rt-resize:home'),
             ('Изменение ширины, `fortnightly`', 'rt-resize:fortnightly')]
# Verdict thresholds on the rounded worst percent vs the reference
GREEN_MAX, YELLOW_MAX = 5, 15
# A/B win rule: CI excludes 1.00 and the effect is at least this large
WIN_MIN = 0.02
FRAME_BUDGET_MS = 1000.0 / 60


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('dirs', nargs='+', help='run_bench.py --out dirs')
    ap.add_argument('--wp', required=True, help='WebParagraph engine name')
    ap.add_argument('--skp', required=True, help='SkParagraph engine name')
    ap.add_argument('--ab', help='NEW/OLD engine names for the A/B effect table')
    ap.add_argument('--ab-labels', default='A,B', help='winner words for NEW,OLD')
    ap.add_argument('--ab-title', default='A против B', help='section title, in words')
    ap.add_argument('--trace', help='WPtraced/WPuntraced,SKPtraced/SKPuntraced engine names')
    ap.add_argument('--md', help='write Markdown here (default: stdout)')
    return ap.parse_args()


def pct(ratio):
    """Ratio to a signed integer percent, rounding half away from zero"""
    v = (ratio - 1.0) * 100.0
    return int(math.copysign(math.floor(abs(v) + 0.5), v))


def fmt_pct(p):
    if p == 0:
        return '0'
    return f'+{p}' if p > 0 else f'−{-p}'


def plural(n, one, few, many):
    """Russian noun form for n: 1 запуске, 2 запусках, 5 запусках"""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_ms(x):
    return f'{x:.1f}'.replace('.', ',')


class Runs:
    """Loaded runs per (dir, config, engine), lazily"""

    def __init__(self, dirs):
        self.dirs = dirs
        self.cache = {}
        self.configs = {}
        for d in dirs:
            self.configs[d] = compare.discover_configs(d)

    def get_all(self, d, cfg, name):
        key = (d, cfg, name)
        if key not in self.cache:
            files = ga.run_files(os.path.join(d, cfg), name)
            self.cache[key] = {rep: ga.load_run(p) for rep, p in files.items()}
        return self.cache[key]

    def get(self, d, cfg, name):
        return {rep: x for rep, x in self.get_all(d, cfg, name).items() if x['done']}

    def source(self, cfg, names):
        """First dir whose config has complete runs of every engine in names"""
        for d in self.dirs:
            if cfg in self.configs[d] and all(self.get(d, cfg, n) for n in names):
                return d
        return None

    def all_configs(self):
        found = {c for cs in self.configs.values() for c in cs}
        return [c for c in CONFIG_ORDER if c in found]


def select(vals, metric, sel):
    how, key = sel
    return compare.total(vals, metric, kind=key if how == 'kind' else None,
                         phase=key if how == 'phase' else None)


def ratios(a_runs, b_runs, metric, sel):
    rs = []
    for rep in sorted(set(a_runs) & set(b_runs)):
        x = select(a_runs[rep]['vals'], metric, sel)
        y = select(b_runs[rep]['vals'], metric, sel)
        if x is not None and y:
            rs.append(x / y)
    return rs


def cell(a_runs, b_runs, metric, sel):
    rs = ratios(a_runs, b_runs, metric, sel)
    if len(rs) < 2:
        return None
    lo, hi = ga.paired_ci(rs)
    return {'med': statistics.median(rs), 'lo': lo, 'hi': hi, 'n': len(rs)}


def med_ms(runs, metric, sel):
    xs = [select(x['vals'], metric, sel) for x in runs.values()]
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def color(p):
    return '🟢' if p <= GREEN_MAX else '🟡' if p <= YELLOW_MAX else '🔴'


VERDICT_WORD = {'🟢': 'наравне', '🟡': 'немного тяжелее', '🔴': 'тяжелее'}


def main_table(p, runs, wp, skp):
    p('### Главный поток: насколько дольше, чем у SkParagraph\n')
    p('Числа — проценты от времени SkParagraph: +25 значит на 25% дольше, −1 значит на 1% '
      'быстрее.\n')
    p('| DPR | ' + ' | '.join(t for t, _ in SCENARIOS) + ' |')
    p('| :--- |' + ' :---: |' * len(SCENARIOS))
    worst = [None] * len(SCENARIOS)
    gpu_pcts, missing = [], []
    for cfg in runs.all_configs():
        d = runs.source(cfg, [wp, skp])
        if d is None:
            missing.append(cfg)
            continue
        a, b = runs.get(d, cfg, wp), runs.get(d, cfg, skp)
        row = []
        for i, (_, sel) in enumerate(SCENARIOS):
            m = cell(a, b, 'main', sel)
            g = cell(a, b, 'gpu', sel)
            if m is None:
                row.append('—')
                continue
            pm = pct(m['med'])
            row.append(f'{color(pm)} {fmt_pct(pm)}')
            vals = [pm] + ([pct(g['med'])] if g else [])
            if g:
                gpu_pcts.append(pct(g['med']))
            worst[i] = max(vals) if worst[i] is None else max(worst[i], *vals)
        p(f'| **{CONFIG_TITLE[cfg]}** | ' + ' | '.join(row) + ' |')
    verdict = []
    for w in worst:
        verdict.append('—' if w is None else f'{color(w)} **{VERDICT_WORD[color(w)]}**')
    p('| **Вердикт** | ' + ' | '.join(verdict) + ' |')
    p()
    p(f'🟢 — не больше +{GREEN_MAX}, 🟡 — от +{GREEN_MAX + 1} до +{YELLOW_MAX}, 🔴 — больше '
      f'+{YELLOW_MAX}. Вердикт — худшее значение по всем DPR и обеим метрикам (главный поток '
      'и GPU-процесс).\n')
    if gpu_pcts:
        p(f'**GPU-процесс** во всех {len(gpu_pcts)} ячейках в пределах от '
          f'{fmt_pct(min(gpu_pcts))} до {fmt_pct(max(gpu_pcts))}.\n')
    if missing:
        p(f'Нет пары WebParagraph и SkParagraph для: {", ".join(missing)}.\n')


def long_frames(runs_by_rep):
    """(runs with a frame over budget in rt-* phases, frames per such run, max frame ms)"""
    hit, per_run, worst = 0, [], 0.0
    for x in runs_by_rep.values():
        n = 0
        for ph, fr in ga.phases_from_lines(x['run']).items():
            if ph.startswith('rt-'):
                n += fr.get('jank16') or 0
                if fr.get('jank16'):
                    worst = max(worst, fr.get('totalMax') or 0.0)
        if n:
            hit += 1
            per_run.append(n)
    return hit, per_run, worst


def rt_table(p, runs, wp, skp):
    p('### Плавность при вводе 60 Гц\n')
    p(f'Время кадра, 90-й перцентиль, в миллисекундах. Бюджет кадра при 60 Гц — '
      f'{fmt_ms(FRAME_BUDGET_MS)} мс.\n')
    p('| DPR | ' + ' | '.join(t for t, _ in RT_PHASES) + ' |')
    p('| :--- |' + ' :---: |' * len(RT_PHASES))
    notes = []
    for cfg in runs.all_configs():
        d = runs.source(cfg, [wp, skp])
        if d is None:
            continue
        a, b = runs.get(d, cfg, wp), runs.get(d, cfg, skp)
        row = []
        for _, ph in RT_PHASES:
            ma = compare.med([x['vals'][ph]['totalP90'] for x in a.values() if ph in x['vals']])
            mb = compare.med([x['vals'][ph]['totalP90'] for x in b.values() if ph in x['vals']])
            row.append('—' if ma is None or mb is None else
                       f'{fmt_ms(ma)} против {fmt_ms(mb)}')
        p(f'| **{CONFIG_TITLE[cfg]}** | ' + ' | '.join(row) + ' |')
        for label, rr in (('WebParagraph', a), ('SkParagraph', b)):
            hit, per_run, worst = long_frames(rr)
            if hit:
                lo, hi = min(per_run), max(per_run)
                span = f'{lo}' if lo == hi else f'{lo}–{hi}'
                notes.append(f'{label}, {CONFIG_TITLE[cfg]}: в {hit} '
                             f'{plural(hit, "запуске", "запусках", "запусках")} из {len(rr)}, '
                             f'по {span} {plural(hi, "кадру", "кадра", "кадров")}, самый долгий '
                             f'{fmt_ms(worst)} мс')
    p()
    p('Первое число — WebParagraph, второе — SkParagraph.\n')
    if notes:
        p('Кадры дольше бюджета: ' + '; '.join(notes) + '.\n')
    else:
        p('Кадров дольше бюджета нет ни в одном запуске.\n')


def winner(c):
    """'A', 'B' or '=' for one metric cell under the win rule"""
    if c is None:
        return '='
    if (c['hi'] < 1.0 or c['lo'] > 1.0) and abs(c['med'] - 1.0) >= WIN_MIN:
        return 'A' if c['med'] < 1.0 else 'B'
    return '='


def ab_table(p, runs, new, old, labels, title):
    lab_new, lab_old = labels
    p(f'### Детали: {title}\n')
    p(f'🟩 — {lab_new} быстрее, 🟥 — {lab_old} быстрее, ⬜ — ничья. Победа, если интервал не '
      f'включает 1,00 и эффект не меньше {WIN_MIN:.0%}; если главный поток и GPU-процесс '
      'спорят, решает сумма их медиан в миллисекундах. В скобках: проценты на главном потоке '
      'и на GPU-процессе.\n')
    p('| DPR | ' + ' | '.join(t for t, _ in SCENARIOS) + ' | Счёт |')
    p('| :--- |' + ' :---: |' * (len(SCENARIOS) + 1))
    for cfg in runs.all_configs():
        d = runs.source(cfg, [new, old])
        if d is None:
            continue
        a, b = runs.get(d, cfg, new), runs.get(d, cfg, old)
        row, score = [], {'A': 0, 'B': 0, '=': 0}
        for _, sel in SCENARIOS:
            m, g = cell(a, b, 'main', sel), cell(a, b, 'gpu', sel)
            wm, wg = winner(m), winner(g)
            if wm == wg or wg == '=':
                w = wm
            elif wm == '=':
                w = wg
            else:
                sa = sum(med_ms(a, k, sel) or 0.0 for k in ('main', 'gpu'))
                sb = sum(med_ms(b, k, sel) or 0.0 for k in ('main', 'gpu'))
                w = 'A' if sa < sb else 'B'
            score[w] += 1
            nums = (f'({fmt_pct(pct(m["med"])) if m else "—"} / '
                    f'{fmt_pct(pct(g["med"])) if g else "—"})')
            row.append({'A': f'🟩 **{lab_new}** {nums}', 'B': f'🟥 **{lab_old}** {nums}',
                        '=': f'⬜ 🤝 {nums}'}[w])
        lead = '🟩' if score['A'] > score['B'] else '🟥' if score['B'] > score['A'] else '⬜'
        row.append(f'{lead} **{score["A"]} : {score["B"]}**, ничьих: {score["="]}')
        p(f'| **{CONFIG_TITLE[cfg]}** | ' + ' | '.join(row) + ' |')
    p()


def trace_table(p, runs, spec):
    (wpt, wpn), (skt, skn) = [x.split('/') for x in spec.split(',')]
    p('### Детали: влияние трейса\n')
    p('Метрика — время фазы build по FrameTiming (без трейса занятость главного потока и '
      'GPU-процесса недоступна). «Трейс» — насколько build с трейсом дольше, чем без него. '
      '«WP против SkP» — насколько build WebParagraph дольше, чем у SkParagraph, с трейсом / '
      'без трейса. Звёздочка — интервал не включает 1,00.\n')
    p('| DPR | Сценарий | Трейс у WebParagraph | Трейс у SkParagraph | '
      'WP против SkP: с трейсом / без |')
    p('| :--- | :--- | :---: | :---: | :---: |')
    for cfg in runs.all_configs():
        d = runs.source(cfg, [wpt, wpn, skt, skn])
        if d is None:
            continue
        r = {n: runs.get(d, cfg, n) for n in (wpt, wpn, skt, skn)}
        first = True
        for title, sel in SCENARIOS[:4]:
            cells = []
            for a, b in ((wpt, wpn), (skt, skn), (wpt, skt), (wpn, skn)):
                cells.append(cell(r[a], r[b], 'build', sel))

            def f(c):
                if c is None:
                    return '—'
                star = '*' if c['hi'] < 1.0 or c['lo'] > 1.0 else ''
                return fmt_pct(pct(c['med'])) + star

            head = f'**{CONFIG_TITLE[cfg]}**' if first else ''
            first = False
            p(f'| {head} | {title} | {f(cells[0])} | {f(cells[1])} | '
              f'{fmt_pct(pct(cells[2]["med"])) if cells[2] else "—"} / '
              f'{fmt_pct(pct(cells[3]["med"])) if cells[3] else "—"} |')
    p()


def sources(p, runs):
    p('### Источники\n')
    ns = argparse.Namespace(load_warn=0.25)
    for d in runs.dirs:
        att_path = os.path.join(d, 'run_attestation.json')
        att = json.load(open(att_path)) if os.path.exists(att_path) else None
        raw = {c: {e['name']: runs.get_all(d, c, e['name'])
                   for e in (att['engines'] if att else [])}
               for c in runs.configs[d]}
        verdict, _ = compare.integrity(att, raw, ns)
        when = (f"{att['start']['time']} – {(att.get('end') or {}).get('time', '—')}"
                if att else '—')
        p(f'- `{d}`: {", ".join(runs.configs[d])}; {when}; Integrity Check: **{verdict}**')
    p()


def main():
    args = parse_args()
    runs = Runs([os.path.abspath(d) for d in args.dirs])
    p = compare.Report()
    main_table(p, runs, args.wp, args.skp)
    rt_table(p, runs, args.wp, args.skp)
    if args.ab:
        new, old = args.ab.split('/')
        ab_table(p, runs, new, old, args.ab_labels.split(','), args.ab_title)
    if args.trace:
        trace_table(p, runs, args.trace)
    sources(p, runs)
    text = '\n'.join(p.lines)
    if args.md:
        with open(args.md, 'w') as f:
            f.write(text)
        print(f'wrote {args.md}')
    else:
        print(text)


if __name__ == '__main__':
    main()
