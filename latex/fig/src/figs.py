"""Figuras do capítulo de trabalho prévio (SVG -> PDF com rsvg-convert).
Uso: figs.py curves.json <dir de saída>"""
import json, math, sys, os

data = json.load(open(sys.argv[1]))
out = sys.argv[2]
os.makedirs(out, exist_ok=True)

INK, INK2, MUTED, GRID, AXIS = '#0b0b0b', '#52514e', '#7a7974', '#e4e3df', '#b9b8b2'
BLUE, ORANGE, AQUA, YELLOW = '#2a78d6', '#eb6834', '#1baf7a', '#eda100'   # slots 1-4, ordem fixa
FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"

def fmt(n):
    return '{:,}'.format(int(round(n))).replace(',', '.')

def head(w, h):
    return ['<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" viewBox="0 0 {0} {1}" font-family="{2}" font-size="12">'.format(w, h, FONT)]

def text(x, y, s, anchor='start', fill=INK2, size=12, weight='normal', extra=''):
    return '<text x="{:.1f}" y="{:.1f}" text-anchor="{}" fill="{}" font-size="{}" font-weight="{}" {}>{}</text>'.format(x, y, anchor, fill, size, weight, extra, s)

def poly(pts, color, width=2, dash=None):
    d = ' '.join('{:.1f},{:.1f}'.format(x, y) for x, y in pts)
    return '<polyline fill="none" stroke="{}" stroke-width="{}" stroke-linejoin="round" stroke-linecap="round"{} points="{}"/>'.format(
        color, width, ' stroke-dasharray="{}"'.format(dash) if dash else '', d)

step = data['sat_none_s1']['step']
def curve(run, src='0'):
    ys = data[run]['series'][src]
    return [((i + 1) * step, y) for i, y in enumerate(ys)]

def mean_curve(runs, src='0'):
    cs = [data[r]['series'][src] for r in runs]
    return [((i + 1) * step, sum(c[i] for c in cs) / len(cs)) for i in range(len(cs[0]))]

NONE = ['sat_none_s1', 'sat_none_s2', 'sat_none_s3']
TRAP = ['sat_trap_none_s1', 'sat_trap_none_s2', 'sat_trap_none_s3']

# ------------------------------------------------------------------ figura 1
W, H = 720, 400
L, R, T, B = 66, 176, 14, 50
pw, ph = W - L - R, H - T - B
XMAX, YMAX = 200000, 35000
X = lambda v: L + pw * v / XMAX
Y = lambda v: T + ph - ph * v / YMAX
s = head(W, H)
for v in range(0, YMAX + 1, 5000):
    s.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="{}" stroke-width="1"/>'.format(L, Y(v), L + pw, Y(v), GRID))
    s.append(text(L - 8, Y(v) + 4, fmt(v), 'end', MUTED, 11))
for v in range(0, XMAX + 1, 50000):
    s.append('<line x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}" stroke="{}" stroke-width="1"/>'.format(X(v), T + ph, X(v), T + ph + 5, AXIS))
    s.append(text(X(v), T + ph + 19, fmt(v), 'middle', MUTED, 11))
s.append('<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="1"/>'.format(L, T + ph, L + pw, T + ph, AXIS))
s.append(text(L + pw / 2, H - 8, 'programas executados', 'middle', INK2, 12))
s.append(text(16, T + ph / 2, 'arestas distintas do pipeline (acumulado)', 'middle', INK2, 12,
              extra='transform="rotate(-90 16 {:.1f})"'.format(T + ph / 2)))

groups = [
    ('Aleatório, gerador sem traps (3 sementes)', 'aleatório, sem traps', NONE, BLUE, None),
    ('Aleatório, gerador com traps (3 sementes)', 'aleatório, com traps', TRAP, ORANGE, None),
    ('Guiado pelas arestas do pipeline (com traps)', 'guiado', ['sat_trap_pipe_s1'], AQUA, '10 4'),
    ('Mutação sem sinal, corpus aleatório (com traps)', 'mutação sem sinal', ['sat_trap_rand_s1'], YELLOW, '3 4'),
]
ends = []
for full, short, runs, color, dash in groups:
    for r in runs:
        pts = [(X(0), Y(0))] + [(X(x), Y(y)) for x, y in curve(r)]
        s.append(poly(pts, color, 2, dash))
    final = sum(data[r]['series']['0'][-1] for r in runs) / len(runs)
    ends.append((final, short, color))
for final, short, color in ends:
    y = Y(final)
    s.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="#ffffff" stroke-width="2"/>'.format(X(XMAX), y, color))
    s.append(text(X(XMAX) + 10, y + 4, fmt(final), 'start', INK, 12, 'bold'))
    s.append(text(X(XMAX) + 62, y + 4, short, 'start', INK2, 11))
# legenda (canto inferior direito da área de plotagem, livre de curvas)
lx, ly = X(72000), Y(11200)
for i, (full, short, runs, color, dash) in enumerate(groups[::-1] if False else [groups[1], groups[2], groups[3], groups[0]]):
    yy = ly + i * 19
    s.append('<line x1="{:.1f}" y1="{:.1f}" x2="{:.1f}" y2="{:.1f}" stroke="{}" stroke-width="2"{}/>'.format(
        lx, yy, lx + 26, yy, color, ' stroke-dasharray="{}"'.format(dash) if dash else ''))
    s.append(text(lx + 34, yy + 4, full, 'start', INK2, 11))
s.append('</svg>')
open(os.path.join(out, 'saturacao_arestas.svg'), 'w').write('\n'.join(s))

# ------------------------------------------------------------------ figura 2
ref = mean_curve(TRAP)
def ratio(run):
    c = curve(run)
    return [(x, 100.0 * y / ref[i][1]) for i, (x, y) in enumerate(c)]
rg, rr = ratio('sat_trap_pipe_s1'), ratio('sat_trap_rand_s1')
W, H = 720, 340
L, R, T, B = 60, 150, 16, 50
pw, ph = W - L - R, H - T - B
XMIN, XMAX = 500, 200000
YMIN, YMAX = 60, 105
X = lambda v: L + pw * (math.log10(v) - math.log10(XMIN)) / (math.log10(XMAX) - math.log10(XMIN))
Y = lambda v: T + ph - ph * (v - YMIN) / (YMAX - YMIN)
s = head(W, H)
for v in range(60, 101, 10):
    s.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="{}" stroke-width="1"/>'.format(L, Y(v), L + pw, Y(v), GRID))
    s.append(text(L - 8, Y(v) + 4, '{}%'.format(v), 'end', MUTED, 11))
for v in (500, 1000, 2000, 5000, 10000, 20000, 50000, 100000, 200000):
    s.append('<line x1="{:.1f}" y1="{}" x2="{:.1f}" y2="{}" stroke="{}" stroke-width="1"/>'.format(X(v), T + ph, X(v), T + ph + 5, AXIS))
    s.append(text(X(v), T + ph + 19, fmt(v), 'middle', MUTED, 11))
s.append('<line x1="{}" y1="{}" x2="{}" y2="{}" stroke="{}" stroke-width="1"/>'.format(L, T + ph, L + pw, T + ph, AXIS))
s.append(text(L + pw / 2, H - 8, 'programas executados (escala logarítmica)', 'middle', INK2, 12))
s.append(text(16, T + ph / 2, 'arestas em relação ao aleatório', 'middle', INK2, 12,
              extra='transform="rotate(-90 16 {:.1f})"'.format(T + ph / 2)))
# referência: aleatório = 100%
s.append('<line x1="{}" y1="{:.1f}" x2="{}" y2="{:.1f}" stroke="{}" stroke-width="2"/>'.format(L, Y(100), L + pw, Y(100), ORANGE))
s.append(text(L + pw + 10, Y(100) + 4, '100%', 'start', INK, 12, 'bold'))
s.append(text(L + pw + 50, Y(100) + 4, 'aleatório', 'start', INK2, 11))
for series, color, dash, short in ((rg, AQUA, '10 4', 'guiado'), (rr, YELLOW, '3 4', 'mutação sem sinal')):
    s.append(poly([(X(x), Y(v)) for x, v in series], color, 2, dash))
    xe, ve = series[-1]
    s.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="#ffffff" stroke-width="2"/>'.format(X(xe), Y(ve), color))
    s.append(text(L + pw + 10, Y(ve) + 4, '{:.0f}%'.format(ve), 'start', INK, 12, 'bold'))
    s.append(text(L + pw + 50, Y(ve) + 4, short, 'start', INK2, 11))
# marca em 8.000 programas na linha guiada
i8 = 8000 // step - 1
x8, v8 = rg[i8]
s.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="#ffffff" stroke-width="2"/>'.format(X(x8), Y(v8), AQUA))
s.append(text(X(x8) - 8, Y(v8) - 8, '{:.0f}% em 8.000'.format(v8), 'end', INK, 11))
x8, v8 = rr[i8]
s.append('<circle cx="{:.1f}" cy="{:.1f}" r="4" fill="{}" stroke="#ffffff" stroke-width="2"/>'.format(X(x8), Y(v8), YELLOW))
s.append(text(X(x8), Y(v8) + 20, '{:.0f}%'.format(v8), 'middle', INK, 11))
s.append('</svg>')
open(os.path.join(out, 'razao_guiado.svg'), 'w').write('\n'.join(s))

# números para o texto
for n in (1000, 8000, 50000, 100000, 200000):
    t = sum(data[r]['marks'][str(n)]['0'] for r in TRAP) / 3
    no = sum(data[r]['marks'][str(n)]['0'] for r in NONE) / 3
    g = data['sat_trap_pipe_s1']['marks'][str(n)]['0']; rc = data['sat_trap_rand_s1']['marks'][str(n)]['0']
    print(n, 'sem traps %.0f' % no, 'traps %.0f' % t, 'guiado', g, '%.1f%%' % (100 * g / t), 'corpus aleatório', rc, '%.1f%%' % (100 * rc / t), 'guiado/corpus %.1f%%' % (100.0 * g / rc - 100))
mn = min(rg, key=lambda p: p[1]); print('mínimo guiado', mn, 'primeiro ponto', rg[0], rr[0])
