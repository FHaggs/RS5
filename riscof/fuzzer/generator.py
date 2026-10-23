"""Geradores de programas.

SimpleGenerator: programa = lista de blocos (kind, seed). Cada bloco é renderizado por um
`random.Random(seed)` próprio, então mutar um bloco não muda os outros. Mantém um corpus
com os programas que trouxeram novidade e, quando há corpus, gera a maioria dos casos por
mutação de um pai sorteado pela energia (raridade) que o fuzzer calculou.

Regras do corpo (ver program.py): x31 é reservado (base de fuzz_mem), nada de traps
(não há tratador: exceção vira hang), acessos sempre alinhados, laços sempre finitos.

Interface:
    generate(case_id) -> Program
    feedback(program, result, novelty)   # chamado no processo principal
"""
import random

from program import FUZZ_MEM_WORDS, Program

_INTERESTING = (0, 1, 2, 0xffffffff, 0x80000000, 0x7fffffff, 0x0000ffff, 0xffff0000, 0xfffffffe)

_R_OPS = ('add', 'sub', 'xor', 'or', 'and', 'sll', 'srl', 'sra', 'slt', 'sltu')
_I_OPS = ('addi', 'xori', 'ori', 'andi')
_SH_OPS = ('slli', 'srli', 'srai')
_MUL_OPS = ('mul', 'mulh', 'mulhsu', 'mulhu')
_DIV_OPS = ('div', 'divu', 'rem', 'remu')
_BR_OPS = ('beq', 'bne', 'blt', 'bge', 'bltu', 'bgeu')
_LOADS = (('lw', 4), ('lh', 2), ('lhu', 2), ('lb', 1), ('lbu', 1))
_STORES = (('sw', 4), ('sh', 2), ('sb', 1))

# CSRs seguros de ler para um registrador que vai para a assinatura (valor arquitetural)
_CSR_ARCH = ('mscratch', 'mepc', 'mcause', 'mtval', 'mtvec')
# mstatus e mie: o RS5 e o Sail divergem em bits WARL/reset (MPP no reset, bits de S-mode),
# então toda leitura é mascarada antes de chegar à assinatura (MIE|MPIE; MSIE|MTIE|MEIE)
_CSR_MASKED = {'mstatus': 0x88, 'mie': 0x888}
# contadores: o valor nunca pode chegar à assinatura (difere do Sail)
_CSR_COUNTERS = ('mcycle', 'minstret', 'mcycleh', 'minstreth', 'mhpmcounter3', 'mhpmcounter4',
                 'mhpmcounter5', 'mhpmcounter6', 'mhpmcounter7', 'mhpmcounter8', 'mhpmcounter9',
                 'cycle', 'instret', 'hpmcounter3')

BLOCK_KINDS = ('alu', 'mem', 'muldiv', 'branch', 'loop', 'jump', 'csr', 'mix')


class _Ctx:
    """Estado compartilhado ao renderizar um programa."""

    def __init__(self, has_m, has_csr):
        self.has_m = has_m
        self.has_csr = has_csr
        self.nlabel = 0
        self.recent = []        # últimos registradores escritos (para forçar forwarding)
        self.reserved = set()   # registradores em uso por laços

    def label(self):
        self.nlabel += 1
        return 'Lb{}'.format(self.nlabel)

    def wrote(self, r):
        if r:
            self.recent = ([r] + [x for x in self.recent if x != r])[:4]


class _Block:
    def __init__(self, kind, seed):
        self.kind = kind
        self.seed = seed

    def render(self, ctx):
        rng = random.Random(self.seed)
        lines = []
        kind = self.kind
        if kind == 'mix':
            kind = rng.choice(('alu', 'mem', 'muldiv', 'branch', 'jump', 'csr'))
        if kind == 'muldiv' and not ctx.has_m:
            kind = 'alu'
        if kind == 'csr' and not ctx.has_csr:
            kind = 'alu'
        getattr(self, '_' + kind)(rng, ctx, lines)
        return lines

    # ---- registradores -------------------------------------------------------

    @staticmethod
    def _pool(ctx):
        return [r for r in range(1, 31) if r not in ctx.reserved]

    def _src(self, rng, ctx):
        """Operando: metade das vezes um registrador escrito há pouco (forwarding/hazard)."""
        pool = self._pool(ctx)
        recent = [r for r in ctx.recent if r in pool]
        if recent and rng.random() < 0.5:
            return rng.choice(recent)
        return rng.choice(pool)

    def _dst(self, rng, ctx):
        return rng.choice(self._pool(ctx))

    def _emit(self, lines, ctx, text, rd=None):
        lines.append('    ' + text)
        ctx.wrote(rd)

    # ---- blocos --------------------------------------------------------------

    def _alu_one(self, rng, ctx, lines):
        p = rng.random()
        rd = self._dst(rng, ctx)
        if p < 0.45:
            self._emit(lines, ctx, '{} x{}, x{}, x{}'.format(rng.choice(_R_OPS), rd, self._src(rng, ctx), self._src(rng, ctx)), rd)
        elif p < 0.75:
            self._emit(lines, ctx, '{} x{}, x{}, {}'.format(rng.choice(_I_OPS), rd, self._src(rng, ctx), rng.randint(-2048, 2047)), rd)
        elif p < 0.88:
            self._emit(lines, ctx, '{} x{}, x{}, {}'.format(rng.choice(_SH_OPS), rd, self._src(rng, ctx), rng.randint(0, 31)), rd)
        elif p < 0.96:
            self._emit(lines, ctx, 'lui x{}, 0x{:05x}'.format(rd, rng.getrandbits(20)), rd)
        else:
            self._emit(lines, ctx, 'auipc x{}, 0x{:05x}'.format(rd, rng.getrandbits(20)), rd)

    def _alu(self, rng, ctx, lines):
        for _ in range(rng.randint(4, 14)):
            self._alu_one(rng, ctx, lines)

    def _mem_one(self, rng, ctx, lines):
        p = rng.random()
        if p < 0.45:                                   # load (e talvez uso imediato)
            op, size = rng.choice(_LOADS)
            rd = self._dst(rng, ctx)
            off = rng.randrange(0, FUZZ_MEM_WORDS * 4, size)
            self._emit(lines, ctx, '{} x{}, {}(x31)'.format(op, rd, off), rd)
            if rng.random() < 0.6:                     # load-use: hazard de rs1/rs2
                r2 = self._dst(rng, ctx)
                self._emit(lines, ctx, '{} x{}, x{}, x{}'.format(rng.choice(_R_OPS), r2, rd, self._src(rng, ctx)), r2)
        elif p < 0.8:                                  # store (e talvez load logo depois)
            op, size = rng.choice(_STORES)
            off = rng.randrange(0, FUZZ_MEM_WORDS * 4, size)
            self._emit(lines, ctx, '{} x{}, {}(x31)'.format(op, self._src(rng, ctx), off))
            if rng.random() < 0.5:                     # load após store: hazard_mem
                lop, lsize = rng.choice(_LOADS)
                rd = self._dst(rng, ctx)
                loff = off if rng.random() < 0.5 and lsize <= size else rng.randrange(0, FUZZ_MEM_WORDS * 4, lsize)
                loff -= loff % lsize
                self._emit(lines, ctx, '{} x{}, {}(x31)'.format(lop, rd, loff), rd)
        else:                                          # dois loads seguidos
            for _ in range(2):
                op, size = rng.choice(_LOADS)
                rd = self._dst(rng, ctx)
                self._emit(lines, ctx, '{} x{}, {}(x31)'.format(op, rd, rng.randrange(0, FUZZ_MEM_WORDS * 4, size)), rd)

    def _mem(self, rng, ctx, lines):
        for _ in range(rng.randint(3, 8)):
            self._mem_one(rng, ctx, lines)

    def _muldiv_one(self, rng, ctx, lines):
        rs1, rs2 = self._src(rng, ctx), self._src(rng, ctx)
        if rng.random() < 0.3:                         # operando de borda (0, -1, INT_MIN...)
            r = self._dst(rng, ctx)
            self._emit(lines, ctx, 'li x{}, 0x{:08x}'.format(r, rng.choice(_INTERESTING)), r)
            rs2 = r
        rd = self._dst(rng, ctx)
        op = rng.choice(_DIV_OPS if rng.random() < 0.5 else _MUL_OPS)
        self._emit(lines, ctx, '{} x{}, x{}, x{}'.format(op, rd, rs1, rs2), rd)
        if rng.random() < 0.5:                         # uso imediato do resultado (forwarding após hold)
            r2 = self._dst(rng, ctx)
            self._emit(lines, ctx, '{} x{}, x{}, x{}'.format(rng.choice(_R_OPS), r2, rd, self._src(rng, ctx)), r2)

    def _muldiv(self, rng, ctx, lines):
        for _ in range(rng.randint(2, 6)):
            self._muldiv_one(rng, ctx, lines)

    def _branch(self, rng, ctx, lines):
        """Desvio para a frente (não previsto) sobre 1-4 instruções; tomado ou não depende dos dados."""
        for _ in range(rng.randint(1, 3)):
            lab = self.__class__._lab(ctx)
            self._emit(lines, ctx, '{} x{}, x{}, {}'.format(rng.choice(_BR_OPS), self._src(rng, ctx), self._src(rng, ctx), lab))
            for _ in range(rng.randint(1, 4)):
                self._alu_one(rng, ctx, lines)
            lines.append(lab + ':')

    def _loop(self, rng, ctx, lines):
        """Laço com contador: desvio para trás, previsto como tomado; a última iteração dá rollback."""
        cnt = self._dst(rng, ctx)
        ctx.reserved.add(cnt)
        lab = self.__class__._lab(ctx)
        self._emit(lines, ctx, 'li x{}, {}'.format(cnt, rng.randint(1, 5)), cnt)
        lines.append(lab + ':')
        for _ in range(rng.randint(2, 6)):
            k = rng.random()
            if k < 0.5:
                self._alu_one(rng, ctx, lines)
            elif k < 0.8:
                self._mem_one(rng, ctx, lines)
            elif ctx.has_m:
                self._muldiv_one(rng, ctx, lines)
        self._emit(lines, ctx, 'addi x{0}, x{0}, -1'.format(cnt), cnt)
        self._emit(lines, ctx, 'bnez x{}, {}'.format(cnt, lab))
        ctx.reserved.discard(cnt)

    def _jump(self, rng, ctx, lines):
        for _ in range(rng.randint(1, 3)):
            lab = self.__class__._lab(ctx)
            rd = self._dst(rng, ctx)
            if rng.random() < 0.5:                     # JAL: previsto como tomado no decode
                self._emit(lines, ctx, 'jal x{}, {}'.format(rd, lab), rd)
            else:                                      # JALR: nunca previsto, redireciona no execute
                rt = self._dst(rng, ctx)
                self._emit(lines, ctx, 'la x{}, {}'.format(rt, lab), rt)
                if rng.random() < 0.3:
                    rd = rt                            # rs1 == rd
                self._emit(lines, ctx, 'jalr x{}, 0(x{})'.format(rd, rt), rd)
            for _ in range(rng.randint(1, 3)):         # instruções puladas (descartadas pelo fetch)
                self._alu_one(rng, ctx, lines)
            lines.append(lab + ':')

    def _csr(self, rng, ctx, lines):
        for _ in range(rng.randint(2, 6)):
            p = rng.random()
            rd = self._dst(rng, ctx)
            if p < 0.3:                                # contador para um registrador, depois apagado
                self._emit(lines, ctx, 'csrr x{}, {}'.format(rd, rng.choice(_CSR_COUNTERS)), rd)
                self._emit(lines, ctx, 'xor x{0}, x{0}, x{0}'.format(rd), rd)
            elif p < 0.45:                             # contador sem destino (rd = x0)
                self._emit(lines, ctx, 'csrrs x0, {}, x0'.format(rng.choice(_CSR_COUNTERS)))
            elif p < 0.65:                             # mscratch: leitura/escrita/set/clear
                op = rng.choice(('csrrw', 'csrrs', 'csrrc'))
                self._emit(lines, ctx, '{} x{}, mscratch, x{}'.format(op, rd, self._src(rng, ctx)), rd)
            elif p < 0.75:                             # imediato
                op = rng.choice(('csrrwi', 'csrrsi', 'csrrci'))
                self._emit(lines, ctx, '{} x{}, mscratch, {}'.format(op, rd, rng.randint(0, 31)), rd)
            elif p < 0.82:                             # leitura arquitetural
                self._emit(lines, ctx, 'csrr x{}, {}'.format(rd, rng.choice(_CSR_ARCH)), rd)
            elif p < 0.88:                             # leitura mascarada de mstatus/mie
                csr = rng.choice(tuple(_CSR_MASKED))
                self._emit(lines, ctx, 'csrr x{}, {}'.format(rd, csr), rd)
                self._mask(lines, ctx, rd, _CSR_MASKED[csr])
            elif p < 0.94:                             # habilita fontes em mie (MIE global fica 0: sem IRQ)
                self._emit(lines, ctx, 'csrrs x{}, mie, x{}'.format(rd, self._src(rng, ctx)), rd)
                self._mask(lines, ctx, rd, _CSR_MASKED['mie'])
            else:                                      # mstatus sem MIE: só MPIE/MPP mudam
                r = self._dst(rng, ctx)
                self._emit(lines, ctx, 'li x{}, 0x{:x}'.format(r, rng.choice((0x80, 0x1800, 0x1880, 0x0, 0x800))), r)
                self._emit(lines, ctx, 'csrrw x{}, mstatus, x{}'.format(rd, r), rd)
                self._mask(lines, ctx, rd, _CSR_MASKED['mstatus'])

    def _mask(self, lines, ctx, rd, mask):
        """rd &= mask, via registrador (andi só aceita imediatos de 12 bits com sinal)."""
        cands = [r for r in self._pool(ctx) if r != rd]
        rm = cands[(rd * 7 + mask) % len(cands)]
        self._emit(lines, ctx, 'li x{}, 0x{:x}'.format(rm, mask), rm)
        self._emit(lines, ctx, 'and x{0}, x{0}, x{1}'.format(rd, rm), rd)

    @staticmethod
    def _lab(ctx):
        return ctx.label()


class SimpleGenerator:
    def __init__(self, isa, seed=None, min_blocks=3, max_blocks=8, mutate_p=0.8):
        self.rng = random.Random(seed)
        self.has_m = 'M' in isa
        self.has_csr = 'Zicsr' in isa
        self.march = 'rv32im_zicsr' if self.has_m else 'rv32i_zicsr'
        self.rvtest_isa = 'RV32IM' if self.has_m else 'RV32I'
        self.min_blocks, self.max_blocks = min_blocks, max_blocks
        self.mutate_p = mutate_p
        self.corpus = []            # [(spec, energy)]
        self.stats = {'random': 0, 'mutated': 0, 'kept': 0}

    # ---- specs -----------------------------------------------------------------

    def _random_spec(self):
        n = self.rng.randint(self.min_blocks, self.max_blocks)
        return {'blocks': [(self.rng.choice(BLOCK_KINDS), self.rng.getrandbits(32)) for _ in range(n)],
                'mem': self.rng.getrandbits(32), 'regs': self.rng.getrandbits(32)}

    def _mutate(self, spec, other=None):
        blocks = list(spec['blocks'])
        op = self.rng.choice(('point', 'point', 'insert', 'delete', 'swap', 'splice', 'data'))
        new = {'blocks': blocks, 'mem': spec['mem'], 'regs': spec['regs']}
        if op == 'point' or (op == 'splice' and other is None):
            i = self.rng.randrange(len(blocks))
            blocks[i] = (blocks[i][0] if self.rng.random() < 0.5 else self.rng.choice(BLOCK_KINDS),
                         self.rng.getrandbits(32))
        elif op == 'insert' and len(blocks) < 2 * self.max_blocks:
            blocks.insert(self.rng.randint(0, len(blocks)), (self.rng.choice(BLOCK_KINDS), self.rng.getrandbits(32)))
        elif op == 'delete' and len(blocks) > 1:
            del blocks[self.rng.randrange(len(blocks))]
        elif op == 'swap' and len(blocks) > 1:
            i, j = self.rng.sample(range(len(blocks)), 2)
            blocks[i], blocks[j] = blocks[j], blocks[i]
        elif op == 'splice':
            cut = self.rng.randint(1, len(blocks))
            cut2 = self.rng.randint(0, len(other['blocks']))
            new['blocks'] = blocks[:cut] + list(other['blocks'][cut2:])
            if not new['blocks']:
                new['blocks'] = blocks
        else:  # data
            if self.rng.random() < 0.5:
                new['mem'] = self.rng.getrandbits(32)
            else:
                new['regs'] = self.rng.getrandbits(32)
        new['op'] = op
        return new

    def _pick_parent(self):
        weights = [max(e, 1e-6) for _, e in self.corpus]
        return self.rng.choices(self.corpus, weights=weights)[0][0]

    # ---- render ----------------------------------------------------------------

    def _value(self, rng):
        if rng.random() < 0.3:
            return rng.choice(_INTERESTING)
        return rng.getrandbits(32)

    def _render(self, spec, case_id):
        ctx = _Ctx(self.has_m, self.has_csr)
        rreg = random.Random(spec['regs'])
        lines = ['    li x{}, 0x{:08x}'.format(r, self._value(rreg)) for r in range(1, 31)]
        for kind, seed in spec['blocks']:
            lines.append('    # block {} seed {:08x}'.format(kind, seed))
            lines.extend(_Block(kind, seed).render(ctx))
        rmem = random.Random(spec['mem'])
        return Program(name='fz-{:06d}'.format(case_id), body='\n'.join(lines),
                       march=self.march, rvtest_isa=self.rvtest_isa,
                       mem_init=tuple(self._value(rmem) for _ in range(FUZZ_MEM_WORDS)),
                       spec=spec)

    # ---- interface -------------------------------------------------------------

    def generate(self, case_id):
        if self.corpus and self.rng.random() < self.mutate_p:
            parent = self._pick_parent()
            other = self._pick_parent() if len(self.corpus) > 1 else None
            spec = self._mutate(parent, other)
            self.stats['mutated'] += 1
        else:
            spec = self._random_spec()
            self.stats['random'] += 1
        return self._render(spec, case_id)

    def feedback(self, program, result, novelty):
        """Guarda no corpus os programas que trouxeram novidade nas fontes de `guide`."""
        if novelty.score > 0 and result.status == 'pass':
            self.corpus.append((program.spec, novelty.energy))
            self.stats['kept'] += 1


class PlaceholderGenerator(SimpleGenerator):
    """Gerador antigo: corpos fixos com valores iniciais aleatórios (mantido para comparação)."""

    _BODIES = {
        'alu': """\
    add   x5, x1, x2
    sub   x6, x3, x4
    xor   x7, x5, x6
    or    x8, x7, x1
    and   x9, x8, x2
    sll   x10, x1, x3
    srl   x11, x2, x4
    sra   x12, x3, x5
    slt   x13, x4, x6
    sltu  x14, x5, x7
    addi  x15, x14, -1
    xori  x16, x15, 0x7ff
    slli  x17, x16, 7
    srai  x18, x17, 3
    lui   x19, 0xabcde
    add   x20, x19, x18""",
        'mem': """\
    sw    x1, 0(x31)
    lw    x5, 0(x31)
    sh    x2, 6(x31)
    lh    x6, 6(x31)
    lhu   x7, 6(x31)
    sb    x3, 13(x31)
    lb    x8, 13(x31)
    lbu   x9, 13(x31)
    lw    x10, 32(x31)
    add   x11, x10, x5
    sw    x11, 16(x31)
    lw    x12, 16(x31)""",
        'branch': """\
    li    x5, 10
    li    x6, 0
1:  add   x6, x6, x1
    addi  x5, x5, -1
    bnez  x5, 1b
    beq   x1, x2, 2f
    addi  x7, x6, 1
2:  blt   x3, x4, 3f
    addi  x8, x6, 2
3:  jal   x9, 4f
    addi  x10, x0, 1
4:  bgeu  x1, x2, 5f
    xor   x11, x1, x2
5:""",
        'muldiv': """\
    mul    x5, x1, x2
    mulh   x6, x1, x2
    mulhsu x7, x1, x2
    mulhu  x8, x1, x2
    div    x9, x3, x4
    divu   x10, x3, x4
    rem    x11, x3, x4
    remu   x12, x3, x4
    div    x13, x3, x0
    rem    x14, x3, x0
    add    x15, x5, x9""",
    }

    def generate(self, case_id):
        kinds = ['alu', 'mem', 'branch'] + (['muldiv'] if self.has_m else [])
        kind = self.rng.choice(kinds)
        init = '\n'.join('    li    x{}, 0x{:08x}'.format(r, self._value(self.rng)) for r in range(1, 31))
        self.stats['random'] += 1
        return Program(name='{}-{:06d}'.format(kind, case_id), body=init + '\n' + self._BODIES[kind],
                       march=self.march, rvtest_isa=self.rvtest_isa,
                       mem_init=tuple(self._value(self.rng) for _ in range(FUZZ_MEM_WORDS)),
                       spec={'blocks': [(kind, 0)], 'mem': 0, 'regs': 0})

    def feedback(self, program, result, novelty):
        pass
