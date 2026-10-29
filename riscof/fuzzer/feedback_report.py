#!/usr/bin/env python3
"""Relatório do comportamento do mapa e das fontes de feedback em uma ou mais execuções do fuzz.py.

    feedback_report.py <work-dir> [<work-dir> ...] [-o <saída>] [--window N]

Lê cases.jsonl.gz, feedback_map.json e run.json de cada diretório e escreve <saída>/report.md
mais os gráficos em SVG (sem dependências além da biblioteca padrão).

O que calcula, por execução e por fonte:
  * curva de saturação: posições distintas acumuladas x casos; casos até 50/90/99% do total;
    último caso com posição nova; posições novas por janela de N casos (taxa de novidade)
  * distribuição de posições por caso, buckets, colisões de hash, instabilidade (--calibrate)
  * bins: taxa de acerto por bin e classificação (>90%, 1-90%, 0%)
  * escalares: máximo e média dos máximos por caso
  * arestas (fonte 0): estados distintos, distribuição das durações, posições mais e menos comuns
  * sobreposição: casos com novidade em uma fonte que também trazem novidade em outra
  * gerador: corpus, aleatório x mutado, status, tempos por etapa
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

SRC_ORDER = (0, 1, 2, 3, 6)


# ---------------------------------------------------------------------------
# leitura
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(os.path.normpath(path))
        with open(os.path.join(path, 'run.json')) as f:
            self.run = json.load(f)
        with open(os.path.join(path, 'feedback_map.json')) as f:
            self.map = json.load(f)
        self.cases = []
        self.bin_cases = Counter()      # bin idx -> casos que o atingiram
        self.has_hits = False
        with gzip.open(os.path.join(path, 'cases.jsonl.gz'), 'rt') as f:
            for line in f:
                c = json.loads(line)
                hits = c.pop('hits', None)      # grande: só os agregados ficam em memória
                if hits is not None:
                    self.has_hits = True
                    for idx in hits.get('6', {}):
                        self.bin_cases[int(idx)] += 1
                self.cases.append(c)
        self.cases.sort(key=lambda c: c['id'])
        self.n = len(self.cases)
        self.guide = self.run['feedback']['guide']
        self.record = self.run['feedback']['record']

    def label(self):
        g = ','.join(map(str, self.guide)) or 'none'
        return '{} (guide={}, seed={})'.format(self.name, g, self.run['args'].get('seed'))

    def curve(self, src):
        """posições acumuladas após cada caso (na ordem de conclusão registrada no log)."""
        key = str(src)
        out, total = [], 0
        for c in self.cases:
            total += c['new'].get(key, [0, 0])[0]
            out.append(total)
        return out


# ---------------------------------------------------------------------------
# svg
# ---------------------------------------------------------------------------

def svg_lines(series, title, xlabel, ylabel, width=720, height=360, logx=False, logy=False):
    """series: [(label, [y...])] com x = 1..len. Devolve o texto SVG."""
    import math
    L, R, T, B = 60, 20, 30, 45
    pw, ph = width - L - R, height - T - B
    xmax = max(len(ys) for _, ys in series) or 1
    ymax = max((max(ys) if ys else 0) for _, ys in series) or 1
    colors = ('#1f77b4', '#d62728', '#2ca02c', '#ff7f0e', '#9467bd', '#8c564b', '#17becf', '#7f7f7f')
    ylog_max = math.log10(max(ymax, 10))

    def X(x):
        if logx:
            return L + pw * (math.log10(max(x, 1)) / math.log10(max(xmax, 10)))
        return L + pw * (x / xmax)

    def Y(y):
        if logy:
            return T + ph - ph * (math.log10(max(y, 1)) / ylog_max)
        return T + ph - ph * (y / ymax)

    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="sans-serif" font-size="11">'.format(width, height),
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="{}" y="18" text-anchor="middle" font-size="13">{}</text>'.format(width / 2, title)]
    # grades
    if logy:
        for k in range(0, int(ylog_max) + 1):
            y = Y(10 ** k)
            out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#ddd"/>'.format(L, y, L + pw, y))
            out.append('<text x="{}" y="{:.1f}" text-anchor="end" dominant-baseline="middle">{}</text>'.format(L - 6, y, 10 ** k))
    else:
        for i in range(5):
            y = T + ph * i / 4
            val = ymax * (1 - i / 4)
            out.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="#ddd"/>'.format(L, y, L + pw, y))
            out.append('<text x="{}" y="{:.1f}" text-anchor="end" dominant-baseline="middle">{:.0f}</text>'.format(L - 6, y, val))
    if logx:
        ticks = [10 ** k for k in range(0, int(math.log10(max(xmax, 10))) + 1)]
    else:
        ticks = [round(xmax * i / 5) for i in range(6)]
    for t in ticks:
        x = X(t)
        out.append('<line x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}" stroke="#ddd"/>'.format(x, T, x, T + ph))
        out.append('<text x="{:.1f}" y="{}" text-anchor="middle">{}</text>'.format(x, T + ph + 14, t))
    out.append('<text x="{}" y="{}" text-anchor="middle">{}</text>'.format(L + pw / 2, height - 6, xlabel))
    out.append('<text transform="translate(14,{}) rotate(-90)" text-anchor="middle">{}</text>'.format(T + ph / 2, ylabel))
    for i, (label, ys) in enumerate(series):
        if not ys:
            continue
        step = max(1, len(ys) // 2000)
        pts = ' '.join('{:.1f},{:.1f}'.format(X(x + 1), Y(y)) for x, y in enumerate(ys) if x % step == 0 or x == len(ys) - 1)
        c = colors[i % len(colors)]
        out.append('<polyline fill="none" stroke="{}" stroke-width="1.6" points="{}"/>'.format(c, pts))
        out.append('<rect x="{}" y="{}" width="10" height="10" fill="{}"/>'.format(L + 8, T + 8 + 14 * i, c))
        out.append('<text x="{}" y="{}">{}</text>'.format(L + 22, T + 17 + 14 * i, label))
    out.append('</svg>')
    return '\n'.join(out)


def svg_bars(items, title, width=720, bar=14):
    """items: [(label, value, color)] horizontal."""
    L = 230
    height = 30 + bar * len(items) + 10
    vmax = max((v for _, v, _ in items), default=1) or 1
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="sans-serif" font-size="10">'.format(width, height),
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="{}" y="16" text-anchor="middle" font-size="13">{}</text>'.format(width / 2, title)]
    for i, (label, v, color) in enumerate(items):
        y = 28 + bar * i
        w = (width - L - 60) * v / vmax
        out.append('<text x="{}" y="{}" text-anchor="end" dominant-baseline="middle">{}</text>'.format(L - 6, y + bar / 2, label))
        out.append('<rect x="{}" y="{}" width="{:.1f}" height="{}" fill="{}"/>'.format(L, y + 2, w, bar - 4, color))
        out.append('<text x="{:.1f}" y="{}" dominant-baseline="middle">{:.3g}</text>'.format(L + w + 4, y + bar / 2, v))
    out.append('</svg>')
    return '\n'.join(out)


def svg_stacked(items, title, legend, width=720, bar=18):
    """items: [(label, [v1, v2, ...])] barras horizontais empilhadas; legend: [(nome, cor)]."""
    L = 230
    height = 44 + bar * len(items) + 10
    vmax = max((sum(v) for _, v in items), default=1) or 1
    out = ['<svg xmlns="http://www.w3.org/2000/svg" width="{}" height="{}" font-family="sans-serif" font-size="10">'.format(width, height),
           '<rect width="100%" height="100%" fill="white"/>',
           '<text x="{}" y="16" text-anchor="middle" font-size="13">{}</text>'.format(width / 2, title)]
    x = L
    for name, color in legend:
        out.append('<rect x="{}" y="22" width="10" height="10" fill="{}"/>'.format(x, color))
        out.append('<text x="{}" y="31">{}</text>'.format(x + 13, name))
        x += 13 + 7 * len(name) + 14
    for i, (label, vals) in enumerate(items):
        y = 42 + bar * i
        out.append('<text x="{}" y="{}" text-anchor="end" dominant-baseline="middle">{}</text>'.format(L - 6, y + bar / 2, label))
        x = L
        for v, (_, color) in zip(vals, legend):
            w = (width - L - 70) * v / vmax
            out.append('<rect x="{:.1f}" y="{}" width="{:.1f}" height="{}" fill="{}"/>'.format(x, y + 2, w, bar - 4, color))
            if w > 24:
                out.append('<text x="{:.1f}" y="{}" fill="#222" dominant-baseline="middle">{}</text>'.format(x + 3, y + bar / 2, v))
            x += w
        out.append('<text x="{:.1f}" y="{}" dominant-baseline="middle">{}</text>'.format(x + 4, y + bar / 2, sum(vals)))
    out.append('</svg>')
    return '\n'.join(out)


# ---------------------------------------------------------------------------
# métricas
# ---------------------------------------------------------------------------

def pct(a, b):
    return 100.0 * a / b if b else 0.0


def quantile_case(curve, frac):
    target = curve[-1] * frac
    for i, v in enumerate(curve):
        if v >= target:
            return i + 1
    return len(curve)


def last_new(run, src):
    key = str(src)
    last = 0
    for c in run.cases:
        if c['new'].get(key, [0, 0])[0]:
            last = c['id'] + 1
    return last


def window_rate(curve, window):
    """posições novas por janela de `window` casos."""
    out = []
    for i in range(0, len(curve), window):
        a = curve[i - 1] if i else 0
        b = curve[min(i + window, len(curve)) - 1]
        out.append(b - a)
    return out


def md_table(header, rows):
    lines = ['| ' + ' | '.join(header) + ' |', '|' + '|'.join('---' for _ in header) + '|']
    for r in rows:
        lines.append('| ' + ' | '.join(str(x) for x in r) + ' |')
    return '\n'.join(lines)


def fmt(x, nd=1):
    if isinstance(x, float):
        return '{:.{}f}'.format(x, nd)
    return str(x)


# ---------------------------------------------------------------------------
# relatório
# ---------------------------------------------------------------------------

def report(runs, out_dir, window, baseline=()):
    os.makedirs(out_dir, exist_ok=True)
    md = []
    w = md.append
    w('# Relatório das fontes de feedback\n')
    w('Gerado por `feedback_report.py` a partir de {} execução(ões). Fontes: ver `FONTES.md`.\n'.format(len(runs)))

    # ---- 1. execuções ----
    w('## 1. Execuções\n')
    rows = []
    for r in runs:
        st = Counter(c['status'] for c in r.cases)
        core = r.run['core']
        dur = r.cases[-1]['t'] if r.cases else 0
        rows.append([r.name, ','.join(map(str, r.guide)) or 'none', ','.join(map(str, r.record)),
                     r.run['feedback']['schedule'], r.run['args'].get('seed'), r.n,
                     ' '.join('{}={}'.format(k, v) for k, v in sorted(st.items())),
                     fmt(dur), fmt(r.n / dur if dur else 0, 2),
                     'DELAY={} IQ={}'.format(core['DELAY_CYCLES'], core['IQUEUE_SIZE'])])
    w(md_table(['execução', 'guide', 'record', 'schedule', 'seed', 'casos', 'status', 'tempo (s)', 'casos/s', 'core'], rows))
    w('')
    rows = []
    for r in runs:
        times = defaultdict(list)
        for c in r.cases:
            for k, v in c['times'].items():
                times[k].append(v * 1000)
        cyc = [c['cycles'] for c in r.cases if c.get('cycles')]
        ret = [c['retired'] for c in r.cases if c.get('retired')]
        rows.append([r.name] + ['{:.1f} / {:.1f}'.format(statistics.mean(v), statistics.median(v)) if v else '-'
                                for k in ('compile', 'dut', 'sail', 'coverage') for v in [times.get(k, [])]]
                    + [fmt(statistics.mean(cyc)) if cyc else '-', fmt(statistics.mean(ret)) if ret else '-',
                       fmt(statistics.mean(c / i for c, i in zip(cyc, ret)) if ret else 0, 2)])
    w(md_table(['execução', 'compile ms (média/mediana)', 'dut ms', 'sail ms', 'leitura ms', 'ciclos/caso', 'instr/caso', 'CPI'], rows))
    w('')

    # ---- 2. posições finais por fonte ----
    w('## 2. Posições por fonte no fim da execução\n')
    rows = []
    for r in runs:
        row = [r.name]
        for src in SRC_ORDER:
            reg = r.map['regions'].get(str(src))
            if not reg:
                row.append('-')
                continue
            n = len(reg['rows'])
            bk = sum(bin(x['buckets']).count('1') for x in reg['rows'])
            row.append('{} pos / {} buckets'.format(n, bk))
        rows.append(row)
    w(md_table(['execução'] + ['fonte {} ({})'.format(s, fb.SOURCES[s].name) for s in SRC_ORDER], rows))
    w('')
    w('Posições atingidas **por caso** (média / mín / máx), ou seja, quantas posições um programa típico toca:\n')
    rows = []
    for r in runs:
        row = [r.name]
        for src in SRC_ORDER:
            v = [c['positions'].get(str(src), 0) for c in r.cases]
            row.append('{:.0f} / {} / {}'.format(statistics.mean(v), min(v), max(v)) if v and max(v) else '-')
        rows.append(row)
    w(md_table(['execução'] + ['fonte {}'.format(s) for s in SRC_ORDER], rows))
    w('')

    # ---- 3. saturação ----
    w('## 3. Saturação: posições novas ao longo dos casos\n')
    w('Casos necessários para atingir 50%, 90% e 99% das posições finais, último caso que trouxe posição nova, '
      'e taxa de novidade (posições novas por janela de {} casos) no início, no meio e no fim.\n'.format(window))
    for src in SRC_ORDER:
        rows = []
        series = []
        for r in runs:
            if str(src) not in r.map['regions']:
                continue
            curve = r.curve(src)
            if not curve or curve[-1] == 0:
                continue
            rate = window_rate(curve, window)
            series.append((r.label(), curve))
            rows.append([r.name, curve[-1], quantile_case(curve, .5), quantile_case(curve, .9), quantile_case(curve, .99),
                         last_new(r, src), rate[0], rate[len(rate) // 2], rate[-1],
                         '{:.2f}'.format(100.0 * (curve[-1] - curve[int(len(curve) * 0.8)]) / curve[-1]) + '%'])
        if not rows:
            continue
        w('### Fonte {} ({})\n'.format(src, fb.SOURCES[src].name))
        w(md_table(['execução', 'total', '50%', '90%', '99%', 'última nova', 'novas/janela início', 'meio', 'fim', '% achado nos últimos 20% dos casos'], rows))
        w('')
        name = 'sat_src{}.svg'.format(src)
        with open(os.path.join(out_dir, name), 'w') as f:
            f.write(svg_lines(series, 'Fonte {}: posições acumuladas'.format(src), 'casos', 'posições'))
        namelog = 'sat_src{}_log.svg'.format(src)
        with open(os.path.join(out_dir, namelog), 'w') as f:
            f.write(svg_lines(series, 'Fonte {}: posições acumuladas (x em log)'.format(src), 'casos (log)', 'posições', logx=True))
        w('![]({})\n![]({})\n'.format(name, namelog))

    # ---- 4. colisões e estabilidade ----
    w('## 4. Colisões de hash e estabilidade\n')
    rows = []
    for r in runs:
        coll = Counter()
        for c in r.cases:
            for s, n in c.get('collisions', {}).items():
                coll[s] += n
        unstable_cases = [c for c in r.cases if c.get('unstable') is not None]
        n_unst = sum(1 for c in unstable_cases if c['unstable'])
        rows.append([r.name,
                     '{} ({:.2f}% das posições)'.format(coll['0'], pct(coll['0'], len(r.map['regions'].get('0', {'rows': []})['rows']))),
                     '{}'.format(coll['1']),
                     '{} casos calibrados, {} instáveis'.format(len(unstable_cases), n_unst) if unstable_cases else 'sem calibração'])
    w(md_table(['execução', 'colisões fonte 0 (posições com 2+ chaves)', 'colisões fonte 1', 'estabilidade'], rows))
    w('')
    w('Colisão = uma posição do mapa que, dentro de um mesmo caso, recebeu duas chaves originais diferentes '
      '(medido na sonda, coluna `colidiu`). Instável = posição cuja contagem mudou entre duas execuções do mesmo binário.\n')

    # ---- 5. fonte 0 em detalhe ----
    w('## 5. Fonte 0 em detalhe: estados, durações e raridade\n')
    for r in runs:
        reg = r.map['regions'].get('0')
        if not reg:
            continue
        states = set()
        dur = Counter()
        freq = []
        for row in reg['rows']:
            raw = int(row['raw'], 16) if row['raw'] else 0
            run_len = raw & 0xffff
            cur = (raw >> 16) & ((1 << 40) - 1)
            prev = (raw >> 56) & ((1 << 40) - 1)
            states.add(cur); states.add(prev)
            dur[fb.BUCKET_NAMES[fb.bucket(run_len)]] += 1
            freq.append(row['freq'])
        w('### {}\n'.format(r.name))
        w('- arestas (posições): **{}**; snapshots distintos: **{}**; arestas por snapshot: {:.1f}'.format(
            len(reg['rows']), len(states), len(reg['rows']) / max(len(states), 1)))
        fc = Counter(freq)
        w('- raridade: {} arestas vistas em 1 caso só ({:.1f}%), {} em 2–9 casos, {} em 10+ casos'.format(
            fc[1], pct(fc[1], len(freq)), sum(v for k, v in fc.items() if 2 <= k <= 9), sum(v for k, v in fc.items() if k >= 10)))
        chao = len(freq) + (fc[1] ** 2 / (2.0 * fc[2]) if fc[2] else float('inf'))
        w('- estimativa Chao1 do total alcançável por este gerador: **{:.0f}** arestas (observadas {}, {:.0f}%)'.format(
            chao, len(freq), pct(len(freq), chao)))
        w('- distribuição da duração do estado de origem: ' + ', '.join(
            '{}: {}'.format(b, dur[b]) for b in fb.BUCKET_NAMES if dur[b]))
        xcls = Counter()
        for st in states:
            xcls[fb.XCLASS[fb.decode_fields(st, fb.SNAPSHOT_FIELDS)['xclass']]] += 1
        w('- snapshots por classe em execute: ' + ', '.join('{}: {}'.format(k, v) for k, v in xcls.most_common()))
        top = sorted(reg['rows'], key=lambda x: -x['freq'])[:6]
        w('\nArestas mais comuns (freq = casos em que apareceu):\n')
        w(md_table(['freq', 'aresta'], [[x['freq'], '`' + x['desc'] + '`'] for x in top]))
        rare = [x for x in reg['rows'] if x['freq'] == 1]
        rare.sort(key=lambda x: x['first_case'] if x['first_case'] is not None else 0)
        w('\nExemplos de arestas vistas uma única vez (caso que as descobriu):\n')
        w(md_table(['caso', 'aresta'], [[x['first_case'], '`' + x['desc'] + '`'] for x in rare[-8:]]))
        w('')

    # ---- 6. fonte 1 ----
    w('## 6. Fonte 1: transições de trap/CSR\n')
    for r in runs:
        reg = r.map['regions'].get('1')
        if not reg or not reg['rows']:
            w('- {}: nenhuma transição registrada'.format(r.name))
            continue
        kinds = Counter()
        for row in reg['rows']:
            raw = int(row['raw'], 16)
            kinds[fb.CSR_EVENT_KINDS[(raw >> 13) & 7]] += 1
        w('### {}\n'.format(r.name))
        w('- transições: {}; por evento: {}'.format(len(reg['rows']), ', '.join('{}: {}'.format(k, v) for k, v in kinds.most_common())))
        top = sorted(reg['rows'], key=lambda x: -x['freq'])[:10]
        w(md_table(['freq', 'transição'], [[x['freq'], '`' + x['desc'] + '`'] for x in top]))
        w('')

    # ---- 7. bins ----
    w('## 7. Fonte 6: taxa de acerto dos bins\n')
    w('Fração dos casos em que cada bin foi atingido. Classificação: `>90%` não discrimina; `1–90%` serve de feedback; '
      '`0%` é alvo (se alcançável) ou exclusão. Bins marcados `(impossível)` não devem acertar nesta configuração.\n')
    for r in runs:
        hit_cases = r.bin_cases
        if not r.has_hits:
            w('- {}: sem hits de bins no log (--no-log-hits?)'.format(r.name))
            continue
        rows = []
        items = []
        cls = Counter()
        for i, name in enumerate(fb.BINS):
            p = pct(hit_cases[i], r.n)
            tag = 'impossível' if name in fb.BINS_IMPOSSIBLE else ('>90%' if p > 90 else ('1-90%' if p >= 1 else ('<1%' if p > 0 else '0%')))
            cls[tag] += 1
            rows.append([i, name, '{:.1f}%'.format(p), tag])
            items.append((name, p, '#d62728' if p == 0 else ('#ff7f0e' if p > 90 else '#1f77b4')))
        w('### {}\n'.format(r.name))
        w('Resumo: ' + ', '.join('{}: {}'.format(k, v) for k, v in sorted(cls.items())) + '\n')
        w(md_table(['#', 'bin', 'acerto', 'classe'], rows))
        name = 'bins_{}.svg'.format(r.name)
        with open(os.path.join(out_dir, name), 'w') as f:
            f.write(svg_bars(items, 'Taxa de acerto dos bins (%) - ' + r.name))
        w('\n![]({})\n'.format(name))

    # ---- 8. escalares ----
    w('## 8. Fonte 7: escalares\n')
    rows = []
    for r in runs:
        per = defaultdict(list)
        for c in r.cases:
            for k, v in c.get('scalars', {}).items():
                per[int(k)].append(v)
        for i, name in enumerate(fb.SCALARS):
            v = per.get(i, [])
            if v:
                rows.append([r.name, name, max(v), fmt(statistics.mean(v)), fmt(statistics.median(v)), sum(1 for x in v if x == 0)])
    w(md_table(['execução', 'escalar', 'máx', 'média dos máx por caso', 'mediana', 'casos com 0'], rows))
    w('')

    # ---- 9. sobreposição entre fontes ----
    w('## 9. Sobreposição: novidade em uma fonte x novidade em outra\n')
    w('Para cada par (A, B): dos casos que trouxeram posição nova em A, quantos também trouxeram em B. '
      'Linhas próximas de 100% em todas as colunas = A não acrescenta sinal próprio; baixas = A vê coisas que B não vê.\n')
    for r in runs:
        srcs = [s for s in SRC_ORDER if str(s) in r.map['regions']]
        novel = {s: set(c['id'] for c in r.cases if c['new'].get(str(s), [0, 0])[0]) for s in srcs}
        rows = []
        for a in srcs:
            row = ['fonte {} ({} casos c/ novidade)'.format(a, len(novel[a]))]
            for b in srcs:
                row.append('-' if a == b else '{:.0f}%'.format(pct(len(novel[a] & novel[b]), len(novel[a]))))
            rows.append(row)
        w('### {}\n'.format(r.name))
        w(md_table(['A \\ B'] + ['fonte {}'.format(b) for b in srcs], rows))
        w('')

    # ---- 9b. sobreposição entre execuções ----
    if len(runs) > 1:
        w('## 9b. Sobreposição entre execuções (mesmo índice = mesma posição)\n')
        w('Para cada fonte: tamanho da união das posições de todas as execuções, posições comuns a todas, '
          'e, por execução, quantas posições só ela encontrou. Índices são comparáveis porque o `fuzz_cov.sv` é o mesmo.\n')
        for src in SRC_ORDER:
            sets = {r.name: set(x['idx'] for x in r.map['regions'].get(str(src), {'rows': []})['rows']) for r in runs}
            sets = {k: v for k, v in sets.items() if v}
            if len(sets) < 2:
                continue
            union = set.union(*sets.values())
            inter = set.intersection(*sets.values())
            rows = []
            for name, st in sets.items():
                others = set.union(*(v for k, v in sets.items() if k != name))
                rows.append([name, len(st), len(st - others), '{:.1f}%'.format(pct(len(st), len(union)))])
            w('### Fonte {} ({}): união {}, comum a todas {} ({:.1f}% da união)\n'.format(
                src, fb.SOURCES[src].name, len(union), len(inter), pct(len(inter), len(union))))
            w(md_table(['execução', 'posições', 'só nesta execução', '% da união'], rows))
            w('')

    # ---- 9c. arestas exclusivas em relação a uma referência ----
    if baseline:
        w('## 9c. Posições exclusivas em relação à referência\n')
        base_runs = [r for r in runs if r.name in baseline]
        w('Referência: a união das posições de {} ({} casos). Para cada execução, quantas das suas posições '
          'nenhum programa da referência atingiu. Uma execução que faz parte da referência é comparada com a '
          'união das **outras** execuções de referência.\n'.format(
              ', '.join(r.name for r in base_runs), sum(r.n for r in base_runs)))
        for src in SRC_ORDER:
            sets = {r.name: set(x['idx'] for x in r.map['regions'].get(str(src), {'rows': []})['rows']) for r in runs}
            if not any(sets.values()):
                continue
            rows, items = [], []
            for r in runs:
                if str(src) not in r.map['regions']:
                    continue
                ref = set().union(*[sets[b.name] for b in base_runs if b.name != r.name]) if base_runs else set()
                mine = sets[r.name]
                excl = mine - ref
                freq = {x['idx']: x['freq'] for x in r.map['regions'][str(src)]['rows']}
                once = sum(1 for i in excl if freq.get(i) == 1)
                tag = ' (ref.)' if r.name in baseline else ''
                rows.append([r.name + tag, len(mine), len(mine & ref), len(excl), '{:.1f}%'.format(pct(len(excl), len(mine))), once])
                items.append((r.name + tag, [len(mine & ref), len(excl)]))
            w('### Fonte {} ({})\n'.format(src, fb.SOURCES[src].name))
            w(md_table(['execução', 'posições', 'também na referência', 'exclusivas', '% exclusivas', 'exclusivas vistas 1x'], rows))
            name = 'exclusive_src{}.svg'.format(src)
            with open(os.path.join(out_dir, name), 'w') as f:
                f.write(svg_stacked(items, 'Fonte {}: posições compartilhadas com a referência e exclusivas'.format(src),
                                    [('na referência', '#9ecae1'), ('exclusivas', '#fc8d59')]))
            w('\n![]({})\n'.format(name))

    # ---- 10. gerador ----
    w('## 10. Gerador e corpus\n')
    rows = []
    for r in runs:
        ops = Counter((c['spec'] or {}).get('op', 'random') for c in r.cases)
        kept = sum(1 for c in r.cases if c['score'] > 0 and c['status'] == 'pass')
        mut = [c for c in r.cases if (c['spec'] or {}).get('op')]
        rnd = [c for c in r.cases if not (c['spec'] or {}).get('op')]
        def newsum(cs, src):
            return sum(c['new'].get(str(src), [0, 0])[0] for c in cs)
        g = r.run.get('generator', {})
        rows.append([r.name + (' traps' if g.get('traps') else ''), r.cases[-1]['corpus'] if r.cases else 0, kept, len(rnd), len(mut),
                     ' '.join('{}={}'.format(k, v) for k, v in ops.most_common()),
                     '{:.2f} / {:.2f}'.format(newsum(rnd, 0) / max(len(rnd), 1), newsum(mut, 0) / max(len(mut), 1)),
                     '{:.2f} / {:.2f}'.format(newsum(rnd, 6) / max(len(rnd), 1), newsum(mut, 6) / max(len(mut), 1))])
    w(md_table(['execução', 'corpus final', 'casos com score>0', 'aleatórios', 'mutados', 'operadores',
                'novas pos. fonte 0 por caso (aleat / mut)', 'idem fonte 6'], rows))
    w('')
    blocks = Counter()
    for r in runs[:1]:
        for c in r.cases:
            for kind, _ in (c['spec'] or {}).get('blocks', []):
                blocks[kind] += 1
    if blocks:
        w('Blocos gerados ({}): {}'.format(runs[0].name, ', '.join('{}={}'.format(k, v) for k, v in blocks.most_common())))
        w('')

    # ---- 11. falhas ----
    w('## 11. Casos que não passaram\n')
    for r in runs:
        bad = [c for c in r.cases if c['status'] != 'pass']
        if not bad:
            w('- {}: todos os casos passaram'.format(r.name))
            continue
        w('- {}: {}'.format(r.name, ', '.join('{}={}'.format(k, v) for k, v in Counter(c['status'] for c in bad).items())))
        for c in bad[:10]:
            w('  - `{}` {} (ciclos {})'.format(c['name'], c['status'], c.get('cycles')))
    w('')

    path = os.path.join(out_dir, 'report.md')
    with open(path, 'w') as f:
        f.write('\n'.join(md) + '\n')
    return path


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('work_dirs', nargs='+')
    p.add_argument('-o', '--out', default=None, help='output directory (default: <first work dir>/report)')
    p.add_argument('--window', type=int, default=100)
    p.add_argument('--baseline', default='', help='comma-separated run names used as the reference for exclusive positions')
    args = p.parse_args()
    runs = [Run(d) for d in args.work_dirs]
    out = args.out or os.path.join(args.work_dirs[0], 'report')
    print(report(runs, out, args.window, set(x for x in args.baseline.split(',') if x)))


if __name__ == '__main__':
    main()
