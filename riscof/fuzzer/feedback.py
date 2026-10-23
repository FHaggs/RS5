"""Modelos das fontes de feedback e o mapa segmentado estilo AFL.

Uma *fonte* é uma região do mapa: um conjunto de posições (índices) com um contador cada,
preenchido durante um caso. No fim do caso o fuzzer aplica, região por região, a regra do
AFL: bucket de contagem novo numa posição = novidade.

Fontes (ver FONTES.md para o detalhe de cada campo):

    0  arestas do pipeline com duração           RTL (fuzz_pipe_probe)   hash
    1  transições de trap/CSR com contexto       RTL (fuzz_csr_probe)    hash
    2  cobertura de linha/user do Verilator      Verilator (coverage.dat) id do ponto
    3  arestas grossas do pipeline               derivada da fonte 0 em Python (derive_coarse)
    6  bins nomeados                             RTL (fuzz_pipe_probe)   índice fixo
    7  escalares (máximo por caso)               RTL (fuzz_pipe_probe)   índice fixo, sem bucket

As tabelas SNAPSHOT_FIELDS, CSR_STATE_FIELDS, BINS e SCALARS espelham riscof/fuzz_cov.sv.
Se o .sv mudar, elas mudam junto.
"""
import json
import os
from collections import Counter
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Buckets do AFL: 1, 2, 3, 4-7, 8-15, 16-31, 32-127, 128+
# ---------------------------------------------------------------------------

BUCKETS = (1, 2, 3, 4, 8, 16, 32, 128)
BUCKET_NAMES = ('1', '2', '3', '4-7', '8-15', '16-31', '32-127', '128+')


def bucket(count):
    b = 0
    for i, limit in enumerate(BUCKETS):
        if count >= limit:
            b = i
    return b


def bucket_bit(count):
    return 1 << bucket(count)


# ---------------------------------------------------------------------------
# Fonte 0: snapshot do pipeline (40 bits, MSB primeiro, igual ao `assign snap` do .sv)
# ---------------------------------------------------------------------------

SNAPSHOT_FIELDS = (
    # (nome, bits)
    ('fq',          2),   # nível da fila do fetch: 0 vazia, 1 com algo, 2 quase cheia, 3 cheia
    ('dec_valid',   1),   # fetch entregou instrução válida ao decode
    ('dec_jumping', 1),   # fetch no meio de um redirecionamento
    ('bp_take',     1),   # previsor mandou o fetch saltar
    ('hz_rs1',      1),   # bolha por rs1 trancado por load
    ('hz_rs2',      1),   # bolha por rs2 trancado por load
    ('hz_mem',      1),   # bolha por load logo após store
    ('fwd1',        2),   # origem de rs1: 0 banco, 1 execute, 2 memória, 3 retire
    ('fwd2',        2),   # origem de rs2
    ('xclass',      4),   # classe da instrução em execute (XCLASS)
    ('x_hazard',    1),   # instrução em execute é bolha de hazard
    ('x_killed',    1),   # instrução em execute foi descartada
    ('x_bp_taken',  1),   # foi prevista como tomada
    ('x_illegal',   1),   # decode marcou como ilegal
    ('mul',         3),   # estado do multiplicador: 0 idle, 1 ALBL, 2 ALBH, 3 AHBL, 4 AHBH
    ('div',         2),   # estado do divisor: 0 idle, 1 init, 2 calc, 3 sign
    ('jump',        1),   # salto confirmado não previsto
    ('rollback',    1),   # previsão desfeita
    ('exc',         1),   # exceção neste ciclo
    ('mret',        1),   # mret neste ciclo
    ('irq_ack',     1),   # interrupção aceita
    ('mem',         2),   # classe em mem_access: 0 nada, 1 resultado, 2 load, 3 lr/sc
    ('ret',         2),   # classe em retire
    ('stall',       1),   # memória de dados pediu espera (DELAY_CYCLES)
    ('irq_pend',    1),   # interrupção pendente
    ('priv',        2),   # privilégio: 0 U, 3 M
    ('mie',         1),   # mstatus.MIE
    ('_pad',        1),
)
SNAPSHOT_BITS = sum(b for _, b in SNAPSHOT_FIELDS)
assert SNAPSHOT_BITS == 40

XCLASS = ('bubble', 'alu', 'load', 'store', 'branch', 'jal', 'jalr', 'mul', 'div',
          'csr', 'sys', 'amo', 'other', '?13', '?14', '?15')
FWD = ('rf', 'ex', 'mem', 'ret')
MULST = ('idle', 'ALBL', 'ALBH', 'AHBL', 'AHBH', '?5', '?6', '?7')
DIVST = ('idle', 'init', 'calc', 'sign')
WBCLASS = ('-', 'res', 'load', 'lrsc')


def decode_fields(value, fields):
    """Int -> {nome: valor}, campos MSB primeiro."""
    out = {}
    total = sum(b for _, b in fields)
    pos = total
    for name, bits in fields:
        pos -= bits
        out[name] = (value >> pos) & ((1 << bits) - 1)
    return out


def describe_snapshot(value):
    """Texto curto com o que não é "neutro" no snapshot."""
    f = decode_fields(value, SNAPSHOT_FIELDS)
    parts = ['x=' + XCLASS[f['xclass']]]
    if f['x_hazard']:   parts.append('hz')
    if f['x_killed']:   parts.append('killed')
    if f['x_bp_taken']: parts.append('bpt')
    if f['x_illegal']:  parts.append('ill')
    if f['fwd1'] or f['fwd2']:
        parts.append('fwd={}/{}'.format(FWD[f['fwd1']], FWD[f['fwd2']]))
    hz = ''.join(k[3:] for k in ('hz_rs1', 'hz_rs2', 'hz_mem') if f[k])
    if hz:              parts.append('dec_hz=' + hz)
    if not f['dec_valid']: parts.append('dec_invalid')
    if f['dec_jumping']: parts.append('jumping')
    if f['bp_take']:    parts.append('bp_take')
    if f['mul']:        parts.append('mul=' + MULST[f['mul']])
    if f['div']:        parts.append('div=' + DIVST[f['div']])
    for k in ('jump', 'rollback', 'exc', 'mret', 'irq_ack', 'stall', 'irq_pend'):
        if f[k]:
            parts.append(k)
    parts.append('fq={}'.format(f['fq']))
    parts.append('m/r={}/{}'.format(WBCLASS[f['mem']], WBCLASS[f['ret']]))
    if f['priv'] != 3:  parts.append('U')
    if f['mie']:        parts.append('mie')
    return ' '.join(parts)


def describe_edge(raw):
    """raw de 96 bits: {snap_prev[39:0], snap[39:0], run[15:0]}"""
    run = raw & 0xffff
    cur = (raw >> 16) & ((1 << 40) - 1)
    prev = (raw >> 56) & ((1 << 40) - 1)
    return '[{}] --{}c({})--> [{}]'.format(describe_snapshot(prev), run, BUCKET_NAMES[bucket(run)],
                                           describe_snapshot(cur))


# ---------------------------------------------------------------------------
# Fonte 3: arestas grossas, derivadas da fonte 0 (sem RTL)
# ---------------------------------------------------------------------------

COARSE_FIELDS = (
    ('hz',         1),   # qualquer hazard no decode
    ('fwd',        1),   # algum operando veio por forwarding
    ('xclass',     4),
    ('x_hazard',   1), ('x_killed', 1), ('x_bp_taken', 1), ('x_illegal', 1),
    ('hold',       1),   # multiplicador ou divisor ocupado
    ('jump',       1), ('rollback', 1), ('exc', 1), ('mret', 1), ('irq_ack', 1),
    ('stall',      1), ('irq_pend', 1),
    ('umode',      1),   # privilégio U
    ('mie',        1),
)
COARSE_BITS = sum(b for _, b in COARSE_FIELDS)


def coarse_snapshot(snap):
    """Projeta o snapshot de 40 bits nos 20 bits de evento (sem fila, forwarding detalhado,
    sub-estados de mul/div, classes de mem/retire, dec_valid/jumping/bp_take)."""
    f = decode_fields(snap, SNAPSHOT_FIELDS)
    vals = [f['hz_rs1'] | f['hz_rs2'] | f['hz_mem'],
            1 if (f['fwd1'] or f['fwd2']) else 0,
            f['xclass'],
            f['x_hazard'], f['x_killed'], f['x_bp_taken'], f['x_illegal'],
            1 if (f['mul'] or f['div']) else 0,
            f['jump'], f['rollback'], f['exc'], f['mret'], f['irq_ack'],
            f['stall'], f['irq_pend'],
            1 if f['priv'] != 3 else 0,
            f['mie']]
    out = 0
    for (name, bits), v in zip(COARSE_FIELDS, vals):
        out = (out << bits) | (v & ((1 << bits) - 1))
    return out


def _mix64(x):
    x &= (1 << 64) - 1
    x ^= x >> 30
    x = (x * 0xbf58476d1ce4e5b9) & ((1 << 64) - 1)
    x ^= x >> 27
    x = (x * 0x94d049bb133111eb) & ((1 << 64) - 1)
    x ^= x >> 31
    return x


def derive_coarse(hits, raws, bits=16):
    """Fonte 3 a partir dos hits/raws da fonte 0 de um caso.

    Cada aresta fina (prev, cur, run) vira a aresta grossa (coarse(prev), coarse(cur), bucket(run));
    arestas cujo estado grosso não muda são descartadas. A contagem é a soma das finas (máx. 255).
    Devolve (hits3, raws3) com chaves (3, idx)."""
    hits3, raws3 = {}, {}
    for (src, idx), count in hits.items():
        if src != 0 or (0, idx) not in raws:
            continue
        raw = raws[(0, idx)]
        run = raw & 0xffff
        cur = coarse_snapshot((raw >> 16) & ((1 << 40) - 1))
        prev = coarse_snapshot((raw >> 56) & ((1 << 40) - 1))
        if cur == prev:
            continue
        key = (_mix64(prev) >> 1) ^ _mix64(cur) ^ (bucket(run) << 60)
        i = _mix64(key) & ((1 << bits) - 1)
        hits3[(3, i)] = min(255, hits3.get((3, i), 0) + count)
        raws3.setdefault((3, i), (prev << 36) | (cur << 16) | run)
    return hits3, raws3


def describe_coarse(value):
    f = decode_fields(value, COARSE_FIELDS)
    parts = ['x=' + XCLASS[f['xclass']]]
    for k in ('hz', 'fwd', 'x_hazard', 'x_killed', 'x_bp_taken', 'x_illegal', 'hold',
              'jump', 'rollback', 'exc', 'mret', 'irq_ack', 'stall', 'irq_pend', 'umode', 'mie'):
        if f[k]:
            parts.append(k)
    return ' '.join(parts)


def describe_coarse_edge(raw):
    run = raw & 0xffff
    cur = (raw >> 16) & ((1 << COARSE_BITS) - 1)
    prev = (raw >> 36) & ((1 << COARSE_BITS) - 1)
    return '[{}] --{}c({})--> [{}]'.format(describe_coarse(prev), run, BUCKET_NAMES[bucket(run)], describe_coarse(cur))


# ---------------------------------------------------------------------------
# Fonte 1: transições de trap/CSR
# ---------------------------------------------------------------------------

CSR_STATE_FIELDS = (
    ('priv', 2), ('mie', 1), ('mpie', 1), ('mpp', 2),
    ('msie', 1), ('mtie', 1), ('meie', 1),
    ('msip', 1), ('mtip', 1), ('meip', 1),
    ('inh_cy', 1), ('inh_ir', 1),
)
CSR_EVENT_KINDS = ('-', 'exc', 'irq', 'mret', 'csr_w', 'csr_r')
CSR_GROUPS = ('mstatus', 'misa', 'mie', 'mtvec', 'mcounteren', 'mcountinhibit', 'mscratch',
              'mepc', 'mcause', 'mtval', 'mip', 'mcycle/minstret', 'mhpmcounter', 'ucounter',
              'mhpmevent', 'other')
EXC_CODES = {0: 'misaligned-fetch', 1: 'fetch-fault', 2: 'illegal', 3: 'breakpoint',
             4: 'misaligned-load', 5: 'load-fault', 6: 'misaligned-store', 7: 'store-fault',
             8: 'ecall-U', 9: 'ecall-S', 11: 'ecall-M'}
CSR_CTX_FIELDS = (('xclass', 4), ('hold', 1), ('stall', 1), ('irq_pend', 1), ('dec_valid', 1))


def describe_csr_state(v):
    f = decode_fields(v, CSR_STATE_FIELDS)
    s = 'M' if f['priv'] == 3 else 'U'
    s += ' mie=%d mpie=%d mpp=%d' % (f['mie'], f['mpie'], f['mpp'])
    en = ''.join(k[1] for k in ('msie', 'mtie', 'meie') if f[k])
    pend = ''.join(k[1] for k in ('msip', 'mtip', 'meip') if f[k])
    if en:   s += ' ie=' + en
    if pend: s += ' ip=' + pend
    if f['inh_cy'] or f['inh_ir']: s += ' inh'
    return s


def describe_csr_transition(raw):
    """raw de 44 bits: {before[13:0], after[13:0], kind[2:0], code[4:0], ctx[7:0]}"""
    ctx = raw & 0xff
    code = (raw >> 8) & 0x1f
    kind = (raw >> 13) & 0x7
    after = (raw >> 16) & 0x3fff
    before = (raw >> 30) & 0x3fff
    k = CSR_EVENT_KINDS[kind] if kind < len(CSR_EVENT_KINDS) else '?'
    if kind == 1:
        what = EXC_CODES.get(code, 'exc%d' % code)
    elif kind == 2:
        what = 'irq(' + ''.join(n for n, b in (('s', 1), ('t', 2), ('e', 4)) if code & b) + ')'
    elif kind in (4, 5):
        what = CSR_GROUPS[code & 0xf]
    else:
        what = ''
    c = decode_fields(ctx, CSR_CTX_FIELDS)
    cs = 'x=' + XCLASS[c['xclass']] + ''.join(' ' + n for n in ('hold', 'stall', 'irq_pend') if c[n])
    change = '' if before == after else ' => {' + describe_csr_state(after) + '}'
    return '{} {} @{{{}}}{} [{}]'.format(k, what, describe_csr_state(before), change, cs)


# ---------------------------------------------------------------------------
# Fonte 6: bins (índice fixo). Espelha bin_hit[i] em fuzz_pipe_probe.
# ---------------------------------------------------------------------------

BINS = (
    'fq_full', 'fq_full_jump', 'rollback_no_valid_fetch', 'fetch_starved',
    'lock_rs1_unused', 'lock_rs2_unused', 'hazard_mem',
    'hazard_x_load', 'hazard_x_store', 'hazard_x_branch', 'hazard_x_jalr', 'hazard_x_op',
    'hazard_x_opimm', 'hazard_x_system', 'hazard_x_muldiv',
    'fwd_exec_in_hold', 'fwd_memret_in_hold', 'fwd_both_retire', 'fwd_mixed_sources',
    'hold_after_hold', 'div_by_zero', 'div_overflow', 'hazard_bubble_in_hold', 'killed_in_hold',
    'irq_x_bubble', 'irq_x_alu', 'irq_x_load', 'irq_x_store', 'irq_x_branch', 'irq_x_jal',
    'irq_x_jalr', 'irq_x_mul', 'irq_x_div', 'irq_x_csr', 'irq_x_sys', 'irq_x_other',
    'irq_blocked_by_hold', 'irq_pending_in_stall',
    'stall_in_hold', 'stall_and_jump', 'stall_and_hazard', 'stall_and_fwd', 'stall_and_irq_ack',
    'exc_in_U', 'exc_in_M', 'mret', 'ecall_U', 'ecall_M', 'illegal_csr', 'illegal_mret',
    'illegal_other', 'misaligned_ldst', 'ebreak', 'exc_after_hold', 'csrw_mstatus_irq_pending',
    'bp_rollback', 'bp_confirmed', 'branch_taken_unpredicted', 'jalr_taken', 'jal_taken_unacked',
    'csr_read_counter', 'csrw_mscratch', 'csrw_mie', 'csrw_mstatus',
)
assert len(BINS) == 64

# Bins que, por construção, não devem acertar nesta configuração (servem de sanidade da sonda)
BINS_IMPOSSIBLE = {'killed_in_hold', 'stall_and_irq_ack'}

# ---------------------------------------------------------------------------
# Fonte 7: escalares (máximo no caso)
# ---------------------------------------------------------------------------

SCALARS = ('fq_level_max', 'hold_run_max', 'hazard_run_max', 'stall_run_max',
           'killed_run_max', 'irq_wait_max', 'fq_full_run_max', 'snapshot_run_max')


# ---------------------------------------------------------------------------
# Descrição das fontes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Source:
    id: int
    name: str
    bits: int                 # tamanho da região (2**bits) quando é um array no RTL; 0 = dict
    hashed: bool              # índice vem de hash (colisões possíveis)
    bucketed: bool            # entra na regra de buckets (escalares não)

    def describe(self, idx, raw=None, names=None):
        if self.id == 0 and raw is not None:
            return describe_edge(raw)
        if self.id == 1 and raw is not None:
            return describe_csr_transition(raw)
        if self.id == 3 and raw is not None:
            return describe_coarse_edge(raw)
        if self.id == 6:
            return BINS[idx] if idx < len(BINS) else 'bin%d' % idx
        if self.id == 7:
            return SCALARS[idx] if idx < len(SCALARS) else 'scalar%d' % idx
        if self.id == 2 and names:
            return names.get(idx, '<ponto %016x>' % idx)
        return '%d:%d' % (self.id, idx)


SOURCES = {
    0: Source(0, 'pipe_edges', 16, True, True),
    1: Source(1, 'csr_trans', 12, True, True),
    2: Source(2, 'verilator', 0, False, True),
    3: Source(3, 'pipe_coarse', 16, True, True),
    6: Source(6, 'bins', 6, False, True),
    7: Source(7, 'scalars', 0, False, False),
}
RTL_SOURCES = (0, 1, 6, 7)          # gravadas pelo fuzz_cov.sv (+FUZZ_COV/+FUZZ_SRC)
DERIVED_SOURCES = {3: 0}            # fonte derivada -> fonte de que depende


def src_mask(sources):
    return sum(1 << s for s in sources if s in RTL_SOURCES)


# ---------------------------------------------------------------------------
# Leitura dos arquivos escritos pelo fuzz_cov.sv
# ---------------------------------------------------------------------------

def parse_fuzz_cov(prefix, sources=RTL_SOURCES):
    """Lê <prefix>_<src>.txt.

    Devolve (hits {(src, idx): count}, raws {(src, idx): raw}, scalars {idx: v}, collided {(src, idx)}).
    `collided` são as posições que, dentro do caso, receberam uma chave diferente da primeira
    (colisão de hash observada no próprio RTL).
    """
    hits, raws, scalars, collided = {}, {}, {}, set()
    for src in sources:
        path = '{}_{}.txt'.format(prefix, src)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            for line in f:
                parts = line.split()
                if not parts:
                    continue
                idx = int(parts[0])
                if src == 7:
                    scalars[idx] = int(parts[1])
                    continue
                count = int(parts[1])
                hits[(src, idx)] = count
                if len(parts) > 2:
                    raws[(src, idx)] = int(parts[2], 16)
                if len(parts) > 3 and parts[3] == '1':
                    collided.add((src, idx))
    return hits, raws, scalars, collided


# ---------------------------------------------------------------------------
# Configuração: o que gravar, o que decide
# ---------------------------------------------------------------------------

@dataclass
class FeedbackConfig:
    record: frozenset = frozenset(SOURCES)          # fontes gravadas (e salvas no log)
    guide: frozenset = frozenset()                  # fontes que decidem corpus/energia ({} = sem feedback)
    weight: dict = field(default_factory=dict)      # peso por fonte na energia (padrão 1.0)
    schedule: str = 'rarity'                        # novelty | rarity | uniform
    verilator_cov: bool = True                      # modelo instrumentado (fonte 2 disponível)
    score: str = 'buckets'                          # 'buckets': posição nova = 2, bucket novo = 1; 'positions': só posições

    @staticmethod
    def parse_sources(text):
        text = (text or '').strip()
        if text in ('', 'none'):
            return frozenset()
        if text == 'all':
            return frozenset(SOURCES)
        return frozenset(int(x) for x in text.split(','))

    def describe(self):
        return 'record={} guide={} schedule={} score={}'.format(
            ','.join(map(str, sorted(self.record))), ','.join(map(str, sorted(self.guide))) or 'none',
            self.schedule, self.score)


# ---------------------------------------------------------------------------
# O mapa segmentado
# ---------------------------------------------------------------------------

class Region:
    """Uma fonte: virgin map (bits de bucket por posição) + frequência no corpus."""

    def __init__(self, src):
        self.src = src
        self.virgin = {}        # idx -> OR dos bits de bucket já vistos
        self.freq = Counter()   # idx -> número de casos (de todos os rodados) que atingiram
        self.first_raw = {}     # idx -> raw da primeira vez
        self.first_case = {}    # idx -> case_id que descobriu

    @property
    def positions(self):
        return len(self.virgin)

    def merge(self, idx, count, raw, case_id):
        """Devolve 2 (posição nova), 1 (bucket novo) ou 0."""
        self.freq[idx] += 1
        b = bucket_bit(count)
        seen = self.virgin.get(idx)
        if seen is None:
            self.virgin[idx] = b
            if raw is not None:
                self.first_raw[idx] = raw
            self.first_case[idx] = case_id
            return 2
        if b & ~seen:
            self.virgin[idx] |= b
            return 1
        return 0


@dataclass
class Novelty:
    new_positions: dict = field(default_factory=dict)   # src -> [idx]
    new_buckets: dict = field(default_factory=dict)     # src -> [idx]
    score: float = 0.0                                  # só fontes de `guide`
    energy: float = 0.0                                 # raridade das posições atingidas (fontes de guide)

    def count(self, src):
        return len(self.new_positions.get(src, ())), len(self.new_buckets.get(src, ()))


class FeedbackMap:
    def __init__(self, cfg):
        self.cfg = cfg
        self.regions = {s: Region(s) for s in SOURCES if SOURCES[s].bucketed}
        self.scalar_best = {}           # idx -> (valor, case_id)
        self.cases = 0
        self.verilator_names = {}

    def learn_names(self, names):
        if names:
            self.verilator_names.update(names)

    def merge(self, case_id, hits, raws, scalars):
        """Aplica o caso ao mapa. hits: {(src, idx): count}. Devolve Novelty."""
        self.cases += 1
        nov = Novelty()
        energy = 0.0
        for (src, idx), count in hits.items():
            region = self.regions.get(src)
            if region is None:
                continue
            r = region.merge(idx, count, raws.get((src, idx)), case_id)
            if r == 2:
                nov.new_positions.setdefault(src, []).append(idx)
            elif r == 1:
                nov.new_buckets.setdefault(src, []).append(idx)
            if src in self.cfg.guide:
                w = self.cfg.weight.get(src, 1.0)
                # raridade: 1/freq, com freq já incluindo este caso
                energy += w / region.freq[idx]
        for idx, v in scalars.items():
            best = self.scalar_best.get(idx)
            if best is None or v > best[0]:
                self.scalar_best[idx] = (v, case_id)
                nov.new_positions.setdefault(7, []).append(idx)
        for src in self.cfg.guide:
            w = self.cfg.weight.get(src, 1.0)
            n_pos, n_bkt = nov.count(src)
            nov.score += w * (2 * n_pos + (n_bkt if self.cfg.score == 'buckets' else 0))
        if self.cfg.schedule == 'rarity':
            nov.energy = energy
        elif self.cfg.schedule == 'uniform':
            nov.energy = 1.0
        else:
            nov.energy = 1.0 if nov.score > 0 else 0.0
        return nov

    def describe(self, src, idx):
        region = self.regions.get(src)
        raw = region.first_raw.get(idx) if region else None
        return SOURCES[src].describe(idx, raw, self.verilator_names)

    def summary(self):
        out = {}
        for src, region in self.regions.items():
            out[src] = {'name': SOURCES[src].name, 'positions': region.positions,
                        'buckets': sum(bin(v).count('1') for v in region.virgin.values())}
        return out

    def save(self, path):
        data = {
            'cases': self.cases,
            'config': {'record': sorted(self.cfg.record), 'guide': sorted(self.cfg.guide),
                       'schedule': self.cfg.schedule, 'weight': self.cfg.weight, 'score': self.cfg.score},
            'regions': {},
            'scalars': {SCALARS[i] if i < len(SCALARS) else str(i): v for i, v in self.scalar_best.items()},
        }
        for src, region in self.regions.items():
            rows = []
            for idx in sorted(region.virgin):
                rows.append({'idx': idx, 'buckets': region.virgin[idx], 'freq': region.freq[idx],
                             'first_case': region.first_case.get(idx),
                             'raw': ('%x' % region.first_raw[idx]) if idx in region.first_raw else None,
                             'desc': self.describe(src, idx)})
            data['regions'][str(src)] = {'name': SOURCES[src].name, 'rows': rows}
        with open(path, 'w') as f:
            json.dump(data, f)
