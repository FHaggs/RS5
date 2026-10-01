"""Test generators.

PlaceholderGenerator only picks one of a few hardcoded bodies and randomizes the initial
register/memory values. A real generator should implement the same interface:

    generate(case_id) -> Program
    feedback(program, result, new_points, new_buckets)  # called in the main process
"""
import random

from program import FUZZ_MEM_WORDS, Program

_ALU = """\
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
    add   x20, x19, x18"""

_MEM = """\
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
    lw    x12, 16(x31)"""

_BRANCH = """\
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
5:"""

_MULDIV = """\
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
    add    x15, x5, x9"""

_INTERESTING = (0, 1, 2, 0xffffffff, 0x80000000, 0x7fffffff, 0x0000ffff, 0xffff0000)


class PlaceholderGenerator:
    def __init__(self, isa, seed=None):
        self.rng = random.Random(seed)
        # (name, body, march, RVTEST_ISA)
        self.templates = [
            ('alu',    _ALU,    'rv32i_zicsr', 'RV32I'),
            ('mem',    _MEM,    'rv32i_zicsr', 'RV32I'),
            ('branch', _BRANCH, 'rv32i_zicsr', 'RV32I'),
        ]
        if 'M' in isa:
            self.templates.append(('muldiv', _MULDIV, 'rv32im_zicsr', 'RV32IM'))

    def _value(self):
        if self.rng.random() < 0.3:
            return self.rng.choice(_INTERESTING)
        return self.rng.getrandbits(32)

    def generate(self, case_id):
        name, body, march, rvtest_isa = self.rng.choice(self.templates)
        init = '\n'.join('    li    x{}, 0x{:08x}'.format(r, self._value()) for r in range(1, 31))
        return Program(name='{}-{:06d}'.format(name, case_id),
                       body=init + '\n' + body,
                       march=march,
                       rvtest_isa=rvtest_isa,
                       mem_init=tuple(self._value() for _ in range(FUZZ_MEM_WORDS)))

    def feedback(self, program, result, new_points, new_buckets):
        # Placeholder: a real generator would keep `program` in its corpus when it found
        # new coverage and bias future mutations towards it.
        pass
