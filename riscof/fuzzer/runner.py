"""Worker side: compile one program, run it on RS5 (Verilator) and Sail, compare, collect coverage.

run_case() executes inside a pool process; everything it returns must be picklable and small.
"""
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field

import toolchain
from coverage import parse_coverage_dat


@dataclass
class RunConfig:
    isa: str
    model: str                # path to the coverage-enabled Vriscof_tb
    scratch: str              # per-case directories are created here (ideally on tmpfs)
    max_cycles: int = 200000  # RS5 timeout
    inst_limit: int = 100000  # Sail instruction limit
    proc_timeout: int = 120   # wall-clock limit for any subprocess (seconds)
    keep: bool = False        # keep passing case directories


@dataclass
class Result:
    case_id: int
    name: str
    status: str               # pass | mismatch | dut_timeout | hang | compile_error | error
    detail: str = ''
    case_dir: str = ''        # kept for anything that is not a pass
    meta: dict = field(default_factory=dict)    # written by riscof_tb (+META_PATH)
    hits: dict = field(default_factory=dict)    # coverage point id -> count
    names: dict = None        # point id -> description, sent once per worker process
    times: dict = field(default_factory=dict)   # stage -> seconds


_cfg = None
_names_sent = False
_skeletons = {}

_BODY_MARK = '__FUZZ_BODY__'
_MEM_MARK  = '__FUZZ_MEM__'


def init_worker(cfg):
    global _cfg
    _cfg = cfg


def _run(cmd, cwd, stdout=subprocess.DEVNULL):
    return subprocess.run(cmd, cwd=cwd, stdout=stdout, stderr=subprocess.PIPE,
                          text=True, timeout=_cfg.proc_timeout)


def _symbols(elf, cwd):
    out = _run([toolchain.TRIPLET + '-nm', elf], cwd, stdout=subprocess.PIPE).stdout
    syms = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3:
            syms[parts[2]] = parts[0]
    return syms


def _skeleton(program):
    """Preprocessed template (arch_test.h expanded) with markers for body and memory.

    Running cpp over arch_test.h is ~40% of the compile time and does not depend on the
    body, so each worker does it once per (march, RVTEST_ISA) and reuses the result.
    """
    key = program.skeleton_key()
    if key not in _skeletons:
        d = os.path.join(_cfg.scratch, 'skeleton-{}-{}'.format(os.getpid(), len(_skeletons)))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'skel.S'), 'w') as f:
            f.write(program.render(body=_BODY_MARK, mem_init=_MEM_MARK))
        p = _run(toolchain.gcc_cmd(program.march, 'skel.S', 'skel.s', preprocess_only=True), d)
        if p.returncode != 0:
            raise RuntimeError('preprocessing the template failed:\n' + p.stderr[-2000:])
        with open(os.path.join(d, 'skel.s')) as f:
            text = f.read()
        shutil.rmtree(d, ignore_errors=True)
        if _BODY_MARK not in text or _MEM_MARK not in text:
            raise RuntimeError('markers lost while preprocessing the template')
        _skeletons[key] = text
    return _skeletons[key]


def _read_words(path):
    try:
        with open(path) as f:
            return [line.strip().lower() for line in f if line.strip()]
    except FileNotFoundError:
        return None


def run_case(case_id, program):
    global _names_sent
    cfg = _cfg
    res = Result(case_id=case_id, name=program.name, status='error')
    case_dir = os.path.join(cfg.scratch, program.name)
    os.makedirs(case_dir, exist_ok=True)
    res.case_dir = case_dir

    try:
        t = time.perf_counter()
        # test.S is the RISCOF-compatible source (kept for failures); test.s is what gets built
        with open(os.path.join(case_dir, 'test.S'), 'w') as f:
            f.write(program.render())
        asm = _skeleton(program).replace(_BODY_MARK, program.body).replace(_MEM_MARK, program.mem_asm())
        with open(os.path.join(case_dir, 'test.s'), 'w') as f:
            f.write(asm)
        p = _run(toolchain.gcc_cmd(program.march, 'test.s', 'test.elf'), case_dir)
        if p.returncode != 0:
            res.status, res.detail = 'compile_error', p.stderr[-2000:]
            return res
        syms = _symbols('test.elf', case_dir)
        _run([toolchain.TRIPLET + '-objcopy', 'test.elf', 'test.bin', '-O', 'binary'], case_dir)
        res.times['compile'] = time.perf_counter() - t

        # RS5: coverage.dat is written in the current directory (case_dir)
        t = time.perf_counter()
        p = _run([cfg.model,
                  '+SIG_START=' + syms['begin_signature'],
                  '+SIG_END=' + syms['end_signature'],
                  '+TOHOST_ADDR=' + syms['tohost'],
                  '+SIG_PATH=dut.sig',
                  '+META_PATH=meta.json',
                  '+MAX_CYCLES=' + str(cfg.max_cycles)], case_dir)
        res.times['dut'] = time.perf_counter() - t
        if p.returncode != 0:
            res.detail = 'RS5 simulation failed:\n' + p.stderr[-2000:]
            return res
        with open(os.path.join(case_dir, 'meta.json')) as f:
            res.meta = json.load(f)

        t = time.perf_counter()
        p = _run([toolchain.SAIL_EXE] + toolchain.sail_args(cfg.isa) +
                 ['-V', '--inst-limit', str(cfg.inst_limit), '--test-signature=ref.sig', 'test.elf'],
                 case_dir, stdout=subprocess.PIPE)
        res.times['sail'] = time.perf_counter() - t
        sail_finished = 'SUCCESS' in p.stdout

        t = time.perf_counter()
        res.hits, names = parse_coverage_dat(os.path.join(case_dir, 'coverage.dat'),
                                             with_names=not _names_sent)
        if names is not None:
            res.names, _names_sent = names, True
        res.times['coverage'] = time.perf_counter() - t

        dut_sig = _read_words(os.path.join(case_dir, 'dut.sig'))
        ref_sig = _read_words(os.path.join(case_dir, 'ref.sig'))
        dut_finished = res.meta.get('exit') == 'tohost'

        if not dut_finished and not sail_finished:
            res.status = 'hang'          # program loops forever on both: generator problem
        elif not dut_finished:
            res.status = 'dut_timeout'   # Sail reached tohost but RS5 did not
        elif ref_sig is None:
            res.detail = 'Sail produced no signature:\n' + p.stderr[-2000:]
        elif dut_sig != ref_sig:
            res.status = 'mismatch'
            diffs = [i for i, (a, b) in enumerate(zip(dut_sig, ref_sig)) if a != b]
            res.detail = 'first differing words: {} (dut {} / ref {} words)'.format(
                diffs[:8], len(dut_sig), len(ref_sig))
        else:
            res.status = 'pass'
        return res

    except subprocess.TimeoutExpired as e:
        res.detail = 'timeout: ' + ' '.join(e.cmd)
        return res
    except Exception as e:  # never kill the pool because of one case
        res.detail = '{}: {}'.format(type(e).__name__, e)
        return res
    finally:
        if res.status == 'pass' and not cfg.keep:
            shutil.rmtree(case_dir, ignore_errors=True)
            res.case_dir = ''
