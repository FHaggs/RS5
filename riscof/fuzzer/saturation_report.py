#!/usr/bin/env python3
"""Saturação das fontes em rodadas longas.

    saturation_report.py <work-dir> [<work-dir> ...] [-o <saída>] [--sources 0,1,3,6]

Para cada rodada e fonte:
  * curva de posições acumuladas (SVG, x linear e log) e casos até 50/90/99% do total
  * taxa de novidade nas janelas de 1k/10k casos (início, meio, fim)
  * estimador Chao1 do total alcançável: S + f1^2 / (2 f2), com f1/f2 = posições vistas em 1 e 2 casos
  * Good-Turing: fração da "massa" nas posições vistas uma vez (probabilidade aproximada de o próximo
    acerto ser inédito) = f1 / soma(freq)
  * entre rodadas do mesmo grupo (prefixo até o "_s"): união, interseção, Jaccard e exclusivas por semente
Lê só os campos `new`, `positions` e `status` de cases.jsonl.gz (funciona com --no-log-hits).
"""
import argparse
import gzip
import json
import os
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import feedback as fb  # noqa: E402
from feedback_report import md_table, svg_lines  # noqa: E402


class Run:
    def __init__(self, path, sources):
        self.path = path
        self.name = os.path.basename(os.path.normpath(path))
        self.group = self.name.rsplit('_s', 1)[0] if '_s' in self.name else self.name
        self.run = json.load(open(os.path.join(path, 'run.json')))
        self.map = json.load(open(os.path.join(path, 'feedback_map.json')))
        self.curves = {s: [] for s in sources}
        self.status = Counter()
        self.n = 0
        tot = {s: 0 for s in sources}
        with gzip.open(os.path.join(path, 'cases.jsonl.gz'), 'rt') as f:
            for line in f:
                c = json.loads(line)
                self.n += 1
                self.status[c['status']] += 1
                for s in sources:
                    tot[s] += c['new'].get(str(s), [0, 0])[0]
                    self.curves[s].append(tot[s])

    def positions(self, src):
        return {r['idx']: r['freq'] for r in self.map['regions'].get(str(src), {'rows': []})['rows']}


def quantile_case(curve, frac):
    target = curve[-1] * frac
    for i, v in enumerate(curve):
        if v >= target:
            return i + 1
    return len(curve)


def window_rates(curve, window):
    out = []
    for i in range(0, len(curve), window):
        a = curve[i - 1] if i else 0
        b = curve[min(i + window, len(curve)) - 1]
        out.append(b - a)
    return out


def chao1(freq):
    f = Counter(freq.values())
    s, f1, f2 = len(freq), f[1], f[2]
    if f2 == 0:
        return s + f1 * (f1 - 1) / 2.0, f1, f2        # forma corrigida para f2 = 0
    return s + f1 * f1 / (2.0 * f2), f1, f2


def report(runs, out_dir, sources):
    os.makedirs(out_dir, exist_ok=True)
    md = []
    w = md.append
    w('# Saturação das fontes\n')
    w('Rodadas: ' + ', '.join('{} ({} casos, {})'.format(r.name, r.n, ' '.join('{}={}'.format(k, v) for k, v in sorted(r.status.items()))) for r in runs) + '\n')
    for src in sources:
        name = fb.SOURCES[src].name
        rows = []
        series = []
        for r in runs:
            curve = r.curves[src]
            if not curve or curve[-1] == 0:
                continue
            freq = r.positions(src)
            est, f1, f2 = chao1(freq)
            mass = sum(freq.values())
            w1k = window_rates(curve, 1000)
            w10k = window_rates(curve, 10000) if len(curve) >= 20000 else None
            rows.append([r.name, curve[-1], quantile_case(curve, .5), quantile_case(curve, .9), quantile_case(curve, .99),
                         w1k[0], w1k[len(w1k) // 2], w1k[-1],
                         (w10k[-1] if w10k else '-'),
                         '{:.0f}'.format(est), '{:.0f}%'.format(100.0 * curve[-1] / est) if est else '-',
                         f1, f2, '{:.4f}'.format(f1 / mass) if mass else '-'])
            series.append((r.name, curve))
        if not rows:
            continue
        w('## Fonte {} ({})\n'.format(src, name))
        w(md_table(['rodada', 'posições', '50%', '90%', '99%', 'novas 1º milhar', 'milhar do meio', 'último milhar',
                    'últimos 10k', 'Chao1', 'vistas/Chao1', 'f1 (1 caso)', 'f2 (2 casos)', 'Good-Turing f1/massa'], rows))
        w('')
        for logx, suffix in ((False, ''), (True, '_log')):
            fn = 'sat_src{}{}.svg'.format(src, suffix)
            with open(os.path.join(out_dir, fn), 'w') as f:
                f.write(svg_lines(series, 'Fonte {}: posições acumuladas{}'.format(src, ' (x em log)' if logx else ''),
                                  'casos' + (' (log)' if logx else ''), 'posições', logx=logx))
            w('![]({})'.format(fn))
        w('')
        # grupos de sementes
        groups = defaultdict(list)
        for r in runs:
            if r.curves[src] and r.curves[src][-1]:
                groups[r.group].append(r)
        grows = []
        for g, rs in groups.items():
            if len(rs) < 2:
                continue
            sets = [set(r.positions(src)) for r in rs]
            union, inter = set.union(*sets), set.intersection(*sets)
            excl = [len(s - set.union(*[t for t in sets if t is not s])) for s in sets]
            jac = [len(a & b) / len(a | b) for i, a in enumerate(sets) for b in sets[i + 1:]]
            grows.append([g, len(rs), ' / '.join(str(len(s)) for s in sets), len(union), len(inter),
                          '{:.2f}'.format(statistics.mean(jac)), ' / '.join(map(str, excl)),
                          '{:.1f}%'.format(100.0 * statistics.mean(excl) / statistics.mean(len(s) for s in sets))])
        if grows:
            w('Entre sementes do mesmo grupo:\n')
            w(md_table(['grupo', 'sementes', 'posições por semente', 'união', 'interseção', 'Jaccard médio',
                        'exclusivas por semente', '% exclusivas'], grows))
            w('')
        # curva da taxa de novidade (novas por 1000 casos), em log-log
        rate_series = []
        for r in runs:
            curve = r.curves[src]
            if curve and curve[-1] and len(curve) >= 5000:
                rate_series.append((r.name, window_rates(curve, 1000)))
        if rate_series:
            fn = 'rate_src{}.svg'.format(src)
            with open(os.path.join(out_dir, fn), 'w') as f:
                f.write(svg_lines(rate_series, 'Fonte {}: posições novas por milhar de casos (log-log)'.format(src),
                                  'milhar de casos (log)', 'novas por 1000 casos (log)', logx=True, logy=True))
            w('![]({})\n'.format(fn))
    path = os.path.join(out_dir, 'saturation.md')
    with open(path, 'w') as f:
        f.write('\n'.join(md) + '\n')
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('work_dirs', nargs='+')
    p.add_argument('-o', '--out', default=None)
    p.add_argument('--sources', default='0,1,3,6')
    args = p.parse_args()
    sources = [int(x) for x in args.sources.split(',')]
    runs = [Run(d, sources) for d in args.work_dirs]
    out = args.out or os.path.join(args.work_dirs[0], 'saturation')
    print(report(runs, out, sources))


if __name__ == '__main__':
    main()
