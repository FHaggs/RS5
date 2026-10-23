#!/usr/bin/env python3
"""Sanidade das sondas (X0 do plano): roda trechos escritos à mão e mostra as arestas da fonte 0,
as transições da fonte 1, os bins e os escalares que cada um produz.

    probe_check.py --work-dir <dir com obj_dir já compilado>   [--only nome] [--traps] [--irq-race]

Cada trecho vem com a lista do que se espera ver (bins e pedaços de descrição de aresta); o script
marca OK/FALTA. Serve para validar a semântica da sonda depois de mudar o fuzz_cov.sv.
`--traps` roda os trechos com o tratador de trap (exceções em M e U, interrupção de timer) e
confere também a contagem de traps contra o Sail. `--irq-race` roda a corrida entre a aceitação
da interrupção e a instrução de CSR que desliga MIE (divergência conhecida do RS5, 2026-10-23).
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feedback as fb  # noqa: E402
import toolchain  # noqa: E402
from program import Program  # noqa: E402
from runner import RunConfig, init_worker, run_case  # noqa: E402

CHECKS = [
    ('load_use', """\
    li x1, 0x12345678
    sw x1, 0(x31)
    lw x5, 0(x31)
    add x6, x5, x1
    add x7, x6, x6
    nop
    nop""", {'bins': ['hazard_x_op', 'stall_and_hazard'], 'edges': ['x=bubble hz', 'fwd=ret']}),
    ('store_then_load', """\
    li x1, 7
    sw x1, 8(x31)
    lw x5, 12(x31)
    nop
    nop""", {'bins': ['hazard_mem'], 'edges': ['dec_hz=mem']}),
    ('div_then_use', """\
    li x1, 1000
    li x2, 7
    div x5, x1, x2
    add x6, x5, x5
    rem x7, x1, x0
    nop""", {'bins': ['div_by_zero'], 'edges': ['div=calc', 'div=sign', '--3']}),
    ('div_div', """\
    li x1, 1000
    li x2, 7
    div x5, x1, x2
    divu x6, x1, x2
    add x7, x6, x5
    nop""", {'bins': ['hold_after_hold'], 'edges': ['div=sign', 'div=init']}),
    ('mul_chain', """\
    li x1, 1000
    li x2, 7
    mul x5, x1, x2
    mulh x6, x5, x2
    add x7, x6, x5
    nop""", {'bins': [], 'edges': ['mul=ALBL', 'mul=AHBH', 'x=mul']}),
    ('loop', """\
    li x5, 3
    li x6, 0
1:  addi x6, x6, 1
    addi x5, x5, -1
    bnez x5, 1b
    nop
    nop""", {'bins': ['bp_confirmed', 'bp_rollback'], 'edges': ['rollback', 'bpt']}),
    ('forward_branch_taken', """\
    li x1, 1
    li x2, 2
    blt x1, x2, 1f
    addi x3, x3, 1
    addi x3, x3, 1
1:  nop
    nop""", {'bins': ['branch_taken_unpredicted'], 'edges': ['x=branch jump', 'killed']}),
    ('jalr', """\
    la x5, 1f
    jalr x6, 0(x5)
    addi x3, x3, 1
1:  nop
    nop""", {'bins': ['jalr_taken'], 'edges': ['x=jalr jump']}),
    ('csr', """\
    li x1, 0x55
    csrrw x5, mscratch, x1
    csrrs x6, mscratch, x0
    csrr x7, mcycle
    xor x7, x7, x7
    csrrs x8, mie, x1
    li x9, 0x888
    and x8, x8, x9
    nop""", {'bins': ['csrw_mscratch', 'csr_read_counter', 'csrw_mie'], 'edges': ['x=csr'],
             'csr': ['csr_w mscratch', 'csr_r mcycle', 'csr_w mie']}),
]


CHECKS_TRAPS = [
    ('exc_m', """\
    li x1, 5
    ecall
    ebreak
    .word 0x00000000
    csrrw x0, cycle, x1
    lw x2, 2(x31)
    sw x1, 6(x31)
    la x29, 1f
    addi x29, x29, 2
    jalr x0, 0(x29)
    addi x5, x5, 1
1:  addi x6, x6, 1
    nop""", {'bins': ['exc_in_M', 'ecall_M', 'ebreak', 'illegal_other', 'misaligned_ldst', 'mret'],
             'edges': ['exc', 'mret'], 'csr': ['exc ecall-M', 'exc breakpoint', 'exc illegal', 'exc misaligned-load', 'exc misaligned-fetch', 'mret'],
             'traps': 7}),
    ('irq_timer', """\
    li x1, 0x80
    csrs mie, x1
    csrsi mstatus, 8
    nop
    nop
    csrci mstatus, 8
    csrs mie, x1
    li x3, 1000
    li x4, 7
    csrsi mstatus, 8
    div x5, x3, x4
    csrci mstatus, 8
    nop""", {'bins': ['irq_blocked_by_hold', 'irq_x_div', 'csrw_mie', 'csrw_mstatus'], 'edges': ['irq_ack'],
             'csr': ['irq(t)'], 'traps': 2}),
    ('umode', """\
    li x29, 0x88
    csrc mstatus, x29
    li x29, 0x80
    csrc mie, x29
    li x29, 0x1800
    csrc mstatus, x29
    la x29, 1f
    csrw mepc, x29
    mret
1:  addi x1, x1, 1
    lw x2, 0(x31)
    csrr x3, mscratch
    ecall
    nop""", {'bins': ['exc_in_U', 'illegal_csr', 'ecall_M', 'mret'], 'edges': ['U'], 'csr': ['exc illegal @{U', 'exc ecall-M'], 'traps': 2}),
    ('umode_ecall', """\
    li x29, 0x88
    csrc mstatus, x29
    li x29, 0x1800
    csrc mstatus, x29
    la x29, 1f
    csrw mepc, x29
    mret
1:  addi x1, x1, 1
    mul x3, x1, x1
    ecall
    nop""", {'bins': ['ecall_U', 'exc_in_U'], 'edges': ['x=sys'], 'csr': ['exc ecall-U @{U'], 'traps': 1}),
]


def trap_count(res):
    """Contagem de traps no fuzz_traps do caso (dut, ref), lendo o offset no ELF."""
    import subprocess
    d = res.case_dir
    if not d or not os.path.exists(os.path.join(d, 'test.elf')):
        return None
    out = subprocess.run([toolchain.TRIPLET + '-nm', os.path.join(d, 'test.elf')], capture_output=True, text=True).stdout
    syms = {p[2]: int(p[0], 16) for p in (l.split() for l in out.splitlines()) if len(p) == 3}
    off = (syms['fuzz_traps'] - syms['begin_signature']) // 4
    dut = open(os.path.join(d, 'dut.sig')).read().split()
    ref = open(os.path.join(d, 'ref.sig')).read().split()
    return int(dut[off], 16), int(ref[off], 16)


def irq_race(cfg):
    """Corrida entre a aceitação da interrupção de timer e o `csrci mstatus, 8` que desliga MIE.
    Com 1 instrução entre ligar e desligar MIE, o RS5 aceita a interrupção no mesmo ciclo em que o
    csrci termina e perde a escrita do CSR (fica com MIE=1); o Sail fica com MIE=0. Com 0
    instruções, o RS5 aceita a interrupção e o Sail não. Com 2 ou mais, os dois concordam."""
    print('==== irq-race: csrs mie,MTIE; csrsi mstatus,8; <n instruções>; csrci mstatus,8')
    variants = []
    for n in range(0, 4):
        body = ('    li x1, 0x80\n    csrs mie, x1\n    csrsi mstatus, 8\n' + '    addi x2, x2, 1\n' * n +
                '    csrci mstatus, 8\n    csrr x3, mstatus\n    andi x3, x3, 0x88\n    nop')
        variants.append(('addi x%d' % n, body))
    variants.append(('lw x1', '    li x1, 0x80\n    csrs mie, x1\n    csrsi mstatus, 8\n    lw x6, 0(x31)\n    csrci mstatus, 8\n    csrr x3, mstatus\n    andi x3, x3, 0x88\n    nop'))
    variants.append(('div x1', '    li x1, 0x80\n    csrs mie, x1\n    csrsi mstatus, 8\n    li x6, 1000\n    li x7, 7\n    div x8, x6, x7\n    csrci mstatus, 8\n    csrr x3, mstatus\n    andi x3, x3, 0x88\n    nop'))
    for i, (name, body) in enumerate(variants):
        prog = Program(name='race-' + name.replace(' ', '_'), body=body, march='rv32im_zicsr', rvtest_isa='RV32IM', spec={}, traps=True)
        res = run_case(100 + i, prog)
        tc = trap_count(res)
        d = res.case_dir
        dut = open(os.path.join(d, 'dut.sig')).read().split(); ref = open(os.path.join(d, 'ref.sig')).read().split()
        print('  entre csrsi e csrci: {:<8} {:<9} traps dut={} ref={}  mstatus&0x88 dut={} ref={}'.format(
            name, res.status, tc[0], tc[1], dut[3], ref[3]))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--work-dir', required=True, help='directory with obj_dir/Vriscof_tb (from a fuzz.py run)')
    p.add_argument('--only', default=None)
    p.add_argument('--all-edges', action='store_true', help='print every edge, not only the matching ones')
    p.add_argument('--traps', action='store_true', help='run the trap-handler checks instead of the plain ones')
    p.add_argument('--irq-race', action='store_true', help='run the interrupt/CSR-write race (known RS5 divergence)')
    args = p.parse_args()

    model = os.path.join(args.work_dir, 'obj_dir', 'Vriscof_tb')
    if not os.path.exists(model):
        raise SystemExit('model not found: ' + model)
    scratch = os.path.join(args.work_dir, 'probe_check')
    os.makedirs(scratch, exist_ok=True)
    cfg = RunConfig(isa='RV32IMUZicsr_Zihpm', model=model, scratch=scratch, keep=True)
    init_worker(cfg)

    if args.irq_race:
        irq_race(cfg)
        return

    failures = 0
    for i, (name, body, expect) in enumerate(CHECKS_TRAPS if args.traps else CHECKS):
        if args.only and args.only != name:
            continue
        prog = Program(name='chk-' + name, body=body, march='rv32im_zicsr', rvtest_isa='RV32IM', spec={}, traps=args.traps)
        res = run_case(i, prog)
        print('==== {} [{}] cycles={}'.format(name, res.status, res.meta.get('cycles')))
        if res.status not in ('pass',):
            print('   ', res.detail)
        if args.traps and 'traps' in expect:
            tc = trap_count(res)
            ok = tc is not None and tc[0] == tc[1] == expect['traps']
            failures += not ok
            print('  {} traps dut/ref/esperado = {}/{}/{}'.format('OK   ' if ok else 'FALTA', tc and tc[0], tc and tc[1], expect['traps']))
        edges = [fb.describe_edge(res.raws[k]) for k in res.hits if k[0] == 0 and k in res.raws]
        trans = [fb.describe_csr_transition(res.raws[k]) for k in res.hits if k[0] == 1 and k in res.raws]
        bins = sorted(fb.BINS[k[1]] for k in res.hits if k[0] == 6)
        print('  edges: {}  transitions: {}  bins: {}'.format(len(edges), len(trans), ', '.join(bins)))
        print('  scalars: ' + ', '.join('{}={}'.format(fb.SCALARS[k], v) for k, v in sorted(res.scalars.items()) if v))
        for e in edges:
            if args.all_edges or any(x in e for x in expect.get('edges', [])):
                print('    ' + e)
        for t in trans:
            print('    csr: ' + t)
        for b in expect.get('bins', []):
            ok = b in bins
            failures += not ok
            print('  {} bin {}'.format('OK   ' if ok else 'FALTA', b))
        for x in expect.get('edges', []):
            ok = any(x in e for e in edges)
            failures += not ok
            print('  {} edge contendo "{}"'.format('OK   ' if ok else 'FALTA', x))
        for x in expect.get('csr', []):
            ok = any(x in t for t in trans)
            failures += not ok
            print('  {} transição contendo "{}"'.format('OK   ' if ok else 'FALTA', x))
    print('\n{} expectativa(s) não atendida(s)'.format(failures))


if __name__ == '__main__':
    main()
