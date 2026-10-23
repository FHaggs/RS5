#!/usr/bin/env python3
"""Reproduz a divergência RS5 x Sail na aceitação de interrupção durante uma instrução de CSR.

    repro_irq_race.py [--work-dir DIR] [--model Vriscof_tb] [--out DIR] [--trace] [--delay-cycles N]

Para cada variante do trecho

    li   x1, 0x80
    csrs mie, x1          # habilita a interrupção de timer (MTIP=1 desde o reset)
    csrsi mstatus, 8      # MIE=1: a interrupção fica pendente
    <n instruções>
    csrci mstatus, 8      # MIE=0
    csrr x3, mstatus
    andi x3, x3, 0x88     # x3 = {MIE, MPIE} no fim

o script monta um teste RISCOF com o tratador de trap do fuzzer (program.py), roda no RS5
(Verilator) e no Sail, e imprime lado a lado cada palavra da assinatura que difere, com o nome da
região (registrador, memória, log de traps). Com --trace, grava os traces de instrução dos dois
modelos e mostra a primeira linha em que divergem (normalização do riscof/lockstep.py).

Resultado esperado (2026-10-23, commit cov-tests): com 1 instrução entre csrsi e csrci o RS5
aceita a interrupção no ciclo em que o csrci termina e perde a escrita do CSR (MIE continua 1);
com 0 instruções o RS5 aceita a interrupção na instrução seguinte ao csrci, com MIE já em 0
(MPIE fica 0 em vez de 1); com 2+ instruções ou um div, concordam.

Os arquivos de cada variante (test.S reproduzível com RISCOF, ELF, assinaturas, traces) ficam em
--out (padrão <work-dir>/irq_race), com um README.md resumindo a tabela.
"""
import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..'))

import toolchain  # noqa: E402
from program import FUZZ_MEM_WORDS, MAX_TRAPS, Program  # noqa: E402
from runner import RunConfig, init_worker, run_case  # noqa: E402

CORE_DEFAULTS = {'BRANCHPRED': '1', 'FORWARDING': '1', 'DUALPORT_MEM': '1', 'IQUEUE_SIZE': '2', 'DELAY_CYCLES': '2'}
ISA = 'RV32IMUZicsr_Zihpm'

# No Sail, mip.MTIP só sobe no primeiro tique do CLINT (a cada 100 instruções, contando as do
# vetor de reset); no RS5 o mtimecmp reseta em 0 e MTIP está em 1 desde o reset. O preenchimento
# garante que a interrupção já esteja pendente nos dois modelos quando MIE é ligado, como acontece
# nos programas do fuzzer (o prólogo tem mais de 100 instruções).
PAD = "    .rept 128\n    nop\n    .endr\n"
PRE = PAD + "    li x1, 0x80\n    csrs mie, x1\n    csrsi mstatus, 8\n"
POST = "    csrci mstatus, 8\n    csrr x3, mstatus\n    andi x3, x3, 0x88\n    nop"

VARIANTS = [
    ('0_nada',    PRE + POST),
    ('1_addi',    PRE + "    addi x2, x2, 1\n" + POST),
    ('1_lw',      PRE + "    lw x6, 0(x31)\n" + POST),
    ('1_sw',      PRE + "    sw x6, 0(x31)\n" + POST),
    ('1_csrr',    PRE + "    csrr x7, mscratch\n" + POST),
    ('2_addi',    PRE + "    addi x2, x2, 1\n    addi x2, x2, 1\n" + POST),
    ('3_addi',    PRE + "    addi x2, x2, 1\n" * 3 + POST),
    ('1_div',     PRE + "    li x6, 1000\n    li x7, 7\n    div x8, x6, x7\n" + POST),
]

EXC_NAMES = {0: 'fetch desalinhado', 1: 'fetch fault', 2: 'instrução ilegal', 3: 'breakpoint',
             4: 'load desalinhado', 5: 'load fault', 6: 'store desalinhado', 7: 'store fault',
             8: 'ecall de U', 11: 'ecall de M', 0x80000007: 'interrupção de timer',
             0x80000003: 'interrupção de software', 0x8000000b: 'interrupção externa'}


def symbols(elf):
    out = subprocess.run([toolchain.TRIPLET + '-nm', elf], capture_output=True, text=True).stdout
    return {p[2]: int(p[0], 16) for p in (l.split() for l in out.splitlines()) if len(p) == 3}


def word_name(i, regs_off, mem_off, traps_off):
    """Nome da palavra i da assinatura."""
    if regs_off <= i < regs_off + 32:
        r = i - regs_off + 1
        return 'x%d' % r if r <= 30 else 'fuzz_regs[%d]' % (r - 1)
    if mem_off <= i < mem_off + FUZZ_MEM_WORDS:
        return 'fuzz_mem[%d]' % (i - mem_off)
    if traps_off <= i < traps_off + 2 * (MAX_TRAPS + 1):
        k = i - traps_off
        if k == 0:
            return 'traps.count'
        if k == 1:
            return 'traps.(reservado)'
        return 'traps[%d].%s' % (k // 2, 'cause' if k % 2 == 0 else 'mepc')
    return 'palavra %d' % i


def cause_name(v):
    return EXC_NAMES.get(v, 'cause 0x%08x' % v)


def read_sig(path):
    return [int(w, 16) for w in open(path).read().split()]


def show_variant(name, res, out_dir, trace):
    d = res.case_dir
    print('=' * 78)
    print('{}  [{}]  ciclos RS5: {}'.format(name, res.status, res.meta.get('cycles')))
    if res.status in ('compile_error', 'error'):
        print(res.detail)
        return None
    sy = symbols(os.path.join(d, 'test.elf'))
    base = sy['begin_signature']
    regs_off, mem_off, traps_off = [(sy[k] - base) // 4 for k in ('fuzz_regs', 'fuzz_mem', 'fuzz_traps')]
    dut, ref = read_sig(os.path.join(d, 'dut.sig')), read_sig(os.path.join(d, 'ref.sig'))

    n_dut, n_ref = dut[traps_off], ref[traps_off]
    print('  traps registrados: RS5 {}  Sail {}'.format(n_dut, n_ref))
    for k in range(1, min(max(n_dut, n_ref), MAX_TRAPS) + 1):
        a = (dut[traps_off + 2 * k], dut[traps_off + 2 * k + 1])
        b = (ref[traps_off + 2 * k], ref[traps_off + 2 * k + 1])
        mark = '' if a == b else '   <-- difere'
        print('    #{}: RS5 {} mepc=0x{:08x} | Sail {} mepc=0x{:08x}{}'.format(
            k, cause_name(a[0]), a[1], cause_name(b[0]), b[1], mark))
    x3d, x3r = dut[regs_off + 2], ref[regs_off + 2]
    print('  mstatus & 0x88 no fim (x3): RS5 0x{:02x} (MIE={} MPIE={})  Sail 0x{:02x} (MIE={} MPIE={})'.format(
        x3d, (x3d >> 3) & 1, (x3d >> 7) & 1, x3r, (x3r >> 3) & 1, (x3r >> 7) & 1))

    diffs = [(i, dut[i], ref[i]) for i in range(min(len(dut), len(ref))) if dut[i] != ref[i]]
    if diffs:
        print('  palavras da assinatura que diferem ({}):'.format(len(diffs)))
        print('    {:<6} {:<20} {:<10} {}'.format('#', 'nome', 'RS5', 'Sail'))
        for i, a, b in diffs:
            print('    {:<6} {:<20} {:08x}   {:08x}'.format(i, word_name(i, regs_off, mem_off, traps_off), a, b))
    else:
        print('  assinaturas idênticas')

    # arquivos para o relato
    vdir = os.path.join(out_dir, name)
    os.makedirs(vdir, exist_ok=True)
    for f in ('test.S', 'test.elf', 'dut.sig', 'ref.sig', 'meta.json'):
        if os.path.exists(os.path.join(d, f)):
            shutil.copy(os.path.join(d, f), vdir)

    first_div = None
    if trace:
        first_div = show_trace(d, vdir, res.status == 'pass')
    return {'name': name, 'status': res.status, 'n_dut': n_dut, 'n_ref': n_ref, 'x3d': x3d, 'x3r': x3r,
            'diffs': len(diffs), 'first_div': first_div}


def show_trace(case_dir, vdir, passed=False):
    """Grava os traces (RS5Debug via +TRACE já está em case_dir; Sail roda de novo com -v) e
    mostra a primeira divergência depois da normalização do lockstep.py."""
    import lockstep
    subprocess.run([toolchain.SAIL_EXE] + toolchain.sail_args(ISA) +
                   ['-v', '--trace-output=sail_trace.log', '--inst-limit', '200000', 'test.elf'],
                   cwd=case_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
    dut_log = os.path.join(case_dir, lockstep.DUT_LOG)
    ref_log = os.path.join(case_dir, 'sail_trace.log')
    if not (os.path.exists(dut_log) and os.path.exists(ref_log)):
        print('  (trace indisponível)')
        return None
    shutil.copy(dut_log, os.path.join(vdir, 'rs5_trace.log'))
    shutil.copy(ref_log, os.path.join(vdir, 'sail_trace.log'))
    dut, n_dut = lockstep.normalize(dut_log)
    ref, _ = lockstep.normalize(ref_log, limit=n_dut)

    import re
    value = re.compile(r'(?:<-|->) 0x([0-9A-Fa-f]+)')

    def masked(a, b):
        ma, mb = value.search(a), value.search(b)
        if not (ma and mb):
            return a == b
        return (int(ma.group(1), 16) & ~0x1800) == (int(mb.group(1), 16) & ~0x1800)

    def same(a, b, prev):
        # Diferença conhecida e inofensiva: o RS5 reseta mstatus.MPP=11 e o Sail MPP=00, e o RS5Debug
        # imprime o valor escrito antes da legalização. Escritas e leituras de mstatus (e o registrador
        # que recebe a leitura) são comparados sem os bits MPP.
        if a.startswith(('CSR mstatus <- ', 'CSR mstatus -> ')) and b.startswith(('CSR mstatus <- ', 'CSR mstatus -> ')):
            return masked(a, b)
        if prev.startswith('CSR mstatus -> ') and a.startswith('x') and b.startswith('x') and a.split()[0] == b.split()[0]:
            return masked(a, b)
        return a == b

    for i in range(min(len(dut), len(ref))):
        if not same(dut[i], ref[i], dut[i - 1] if i else ''):
            lo, hi = max(0, i - 8), i + 6
            print('  primeira divergência nos traces (linha {} da normalização):'.format(i))
            if passed:
                print('  (as assinaturas são iguais: a diferença abaixo é só o momento em que cada modelo'
                      ' atende a interrupção, o que a spec permite; o estado arquitetural converge)')
            print('    {:<60} | {}'.format('RS5', 'Sail'))
            for j in range(lo, min(hi, max(len(dut), len(ref)))):
                a = dut[j] if j < len(dut) else ''
                b = ref[j] if j < len(ref) else ''
                print('  {} {:<60} | {}'.format('>' if j == i else ' ', a[:60], b[:60]))
            return i
    print('  traces idênticos no trecho comparado ({} passos)'.format(n_dut))
    return None


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--work-dir', default=os.path.join(toolchain.RISCOF_DIR, 'bug_repro_work'))
    p.add_argument('--model', default=None, help='Vriscof_tb já compilado (senão compila em <work-dir>/obj_dir, sem cobertura)')
    p.add_argument('--out', default=None, help='onde guardar test.S, assinaturas e traces (padrão <work-dir>/irq_race)')
    p.add_argument('--trace', action='store_true', help='grava os traces de instrução e mostra a primeira divergência')
    p.add_argument('--only', default=None, help='só esta variante')
    for var, default in CORE_DEFAULTS.items():
        p.add_argument('--' + var.lower().replace('_', '-'), default=os.environ.get(var, default))
    args = p.parse_args()

    for var in CORE_DEFAULTS:
        os.environ[var] = getattr(args, var.lower())
    toolchain.check_tools()
    os.makedirs(args.work_dir, exist_ok=True)
    model = args.model or toolchain.build_model(ISA, args.work_dir, coverage=())
    out_dir = args.out or os.path.join(args.work_dir, 'irq_race')
    scratch = os.path.join(args.work_dir, 'scratch')
    init_worker(RunConfig(isa=ISA, model=model, scratch=scratch, keep=True, record=frozenset(), trace=args.trace))
    core = ' '.join('{}={}'.format(v, os.environ[v]) for v in CORE_DEFAULTS)
    print('modelo: {}\ncore: {}\nsaída: {}'.format(model, core, out_dir))

    rows = []
    for i, (name, body) in enumerate(VARIANTS):
        if args.only and args.only != name:
            continue
        prog = Program(name='irq-race-' + name, body=body, march='rv32im_zicsr', rvtest_isa='RV32IM', spec={}, traps=True)
        r = show_variant(name, run_case(i, prog), out_dir, args.trace)
        if r:
            rows.append(r)

    print('=' * 78)
    print('{:<10} {:<9} {:<16} {:<22} {}'.format('variante', 'status', 'traps RS5/Sail', 'MIE,MPIE RS5 / Sail', 'palavras diferentes'))
    lines = ['# Reprodução: interrupção aceita no ciclo em que uma instrução de CSR termina', '',
             'Core: `{}`. Gerado por `riscof/fuzzer/repro_irq_race.py`. Cada subdiretório tem o `test.S` (teste RISCOF '
             'completo), `dut.sig` (RS5), `ref.sig` (Sail) e, com `--trace`, os traces de instrução.'.format(core), '',
             '| variante | status | traps RS5 / Sail | mstatus&0x88 RS5 / Sail | palavras diferentes |', '|---|---|---|---|---|']
    for r in rows:
        mie = '{:02x} / {:02x}'.format(r['x3d'], r['x3r'])
        print('{:<10} {:<9} {:<16} {:<22} {}'.format(r['name'], r['status'], '{} / {}'.format(r['n_dut'], r['n_ref']), mie, r['diffs']))
        lines.append('| {} | {} | {} / {} | 0x{} | {} |'.format(r['name'], r['status'], r['n_dut'], r['n_ref'], mie.replace(' / ', ' / 0x'), r['diffs']))
    with open(os.path.join(out_dir, 'README.md'), 'w') as f:
        f.write('\n'.join(lines) + '\n')
    print('arquivos em', out_dir)


if __name__ == '__main__':
    main()
