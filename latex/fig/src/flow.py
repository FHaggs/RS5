"""Diagrama do fluxo do fuzzer (SVG)."""
import sys
INK, INK2, MUTED, LINE, FILL = '#0b0b0b', '#52514e', '#7a7974', '#52514e', '#f4f3ef'
BLUE, BLUE_FILL = '#2a78d6', '#e8f1fc'
FONT = "DejaVu Sans, Helvetica, Arial, sans-serif"
W, H = 760, 318
s = ['<svg xmlns="http://www.w3.org/2000/svg" width="{0}" height="{1}" viewBox="0 0 {0} {1}" font-family="{2}" font-size="12">'.format(W, H, FONT),
     '<defs>'
     '<marker id="a" markerWidth="9" markerHeight="8" refX="8" refY="4" orient="auto"><path d="M0,0 L9,4 L0,8 z" fill="{}"/></marker>'
     '<marker id="b" markerWidth="9" markerHeight="8" refX="8" refY="4" orient="auto"><path d="M0,0 L9,4 L0,8 z" fill="{}"/></marker>'
     '</defs>'.format(LINE, BLUE)]

def box(x, y, w, h, lines, fb=False):
    s.append('<rect x="{}" y="{}" width="{}" height="{}" rx="6" fill="{}" stroke="{}" stroke-width="1.2"/>'.format(
        x, y, w, h, BLUE_FILL if fb else FILL, BLUE if fb else LINE))
    n = len(lines)
    y0 = y + h / 2 - (n - 1) * 8 + 4
    for i, (t, strong) in enumerate(lines):
        s.append('<text x="{}" y="{:.1f}" text-anchor="middle" fill="{}" font-size="{}" font-weight="{}">{}</text>'.format(
            x + w / 2, y0 + i * 16, INK if strong else INK2, 12 if strong else 11, 'bold' if strong else 'normal', t))

def arrow(pts, fb=False, dash=None):
    d = 'M' + ' L'.join('{},{}'.format(x, y) for x, y in pts)
    s.append('<path d="{}" fill="none" stroke="{}" stroke-width="1.6"{} marker-end="url(#{})"/>'.format(
        d, BLUE if fb else LINE, ' stroke-dasharray="{}"'.format(dash) if dash else '', 'b' if fb else 'a'))

def label(x, y, t, anchor='middle', fb=False):
    s.append('<text x="{}" y="{}" text-anchor="{}" fill="{}" font-size="10.5">{}</text>'.format(x, y, anchor, INK2, t))

# linha principal
box(10, 76, 126, 62, [('Gerador', True), ('blocos e mutação', False)])
box(166, 76, 126, 62, [('Programa de teste', True), ('formato do RISCOF', False)])
box(322, 76, 104, 62, [('Compilação', True), ('GCC', False)])
box(462, 18, 150, 54, [('Sail', True), ('modelo de referência', False)])
box(462, 142, 150, 54, [('RS5 no Verilator', True), ('com sondas via bind', False)])
box(646, 70, 106, 74, [('Oráculo', True), ('comparação de', False), ('assinaturas', False)])
arrow([(136, 107), (166, 107)])
arrow([(292, 107), (322, 107)])
arrow([(426, 96), (444, 96), (444, 45), (462, 45)])
arrow([(426, 118), (444, 118), (444, 169), (462, 169)])
arrow([(612, 45), (699, 45), (699, 70)])
arrow([(612, 169), (699, 169), (699, 144)])
label(655, 38, 'assinatura')
label(655, 185, 'assinatura')
# laço de realimentação
box(462, 244, 150, 58, [('Fontes de realimentação', True), ('contadores por programa', False)], fb=True)
box(166, 244, 260, 58, [('Mapa segmentado', True), ('buckets, novidade e raridade por fonte', False)], fb=True)
arrow([(537, 196), (537, 244)], fb=True)
arrow([(462, 273), (426, 273)], fb=True)
arrow([(166, 273), (73, 273), (73, 138)], fb=True)
label(80, 216, 'corpus e', 'start')
label(80, 229, 'energia', 'start')
label(545, 224, 'sinais internos', 'start')
s.append('</svg>')
open(sys.argv[1], 'w').write('\n'.join(s))
