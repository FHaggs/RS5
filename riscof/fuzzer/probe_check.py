#!/usr/bin/env python3
"""Sanidade das sondas (X0 do plano): roda trechos escritos à mão e mostra as arestas da fonte 0,
as transições da fonte 1, os bins e os escalares que cada um produz.

    probe_check.py --work-dir <dir com obj_dir já compilado>   [--only nome]

Cada trecho vem com a lista do que se espera ver (bins e pedaços de descrição de aresta); o script
marca OK/FALTA. Serve para validar a semântica da sonda depois de mudar o fuzz_cov.sv.
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--work-dir', required=True, help='directory with obj_dir/Vriscof_tb (from a fuzz.py run)')
    p.add_argument('--only', default=None)
    p.add_argument('--all-edges', action='store_true', help='print every edge, not only the matching ones')
    args = p.parse_args()

    model = os.path.join(args.work_dir, 'obj_dir', 'Vriscof_tb')
    if not os.path.exists(model):
        raise SystemExit('model not found: ' + model)
    scratch = os.path.join(args.work_dir, 'probe_check')
    os.makedirs(scratch, exist_ok=True)
    cfg = RunConfig(isa='RV32IMUZicsr_Zihpm', model=model, scratch=scratch, keep=True)
    init_worker(cfg)

    failures = 0
    for i, (name, body, expect) in enumerate(CHECKS):
        if args.only and args.only != name:
            continue
        prog = Program(name='chk-' + name, body=body, march='rv32im_zicsr', rvtest_isa='RV32IM', spec={})
        res = run_case(i, prog)
        print('==== {} [{}] cycles={}'.format(name, res.status, res.meta.get('cycles')))
        if res.status not in ('pass',):
            print('   ', res.detail)
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
