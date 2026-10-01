"""Paths and commands shared by the fuzzer: toolchain, Verilator model build and Sail."""
import os
import re
import shutil
import subprocess
import sys

RISCOF_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, os.path.join(RISCOF_DIR, 'rs5'))

from rs5_verilator import RS5_ROOT, verilator_cmd  # noqa: E402

ARCHTEST_ENV = os.path.join(RISCOF_DIR, 'riscv-arch-test', 'riscv-test-suite', 'env')
MODEL_ENV    = os.path.join(RISCOF_DIR, 'rs5', 'env')
LINKER       = os.path.join(MODEL_ENV, 'link.ld')

TRIPLET  = os.environ.get('TRIPLET', 'riscv64-elf')
SAIL_EXE = 'riscv_sim_rv32d'

# Coverage types instrumented by Verilator. Toggle coverage is much larger/slower, opt-in.
DEFAULT_COVERAGE = ('line', 'user')


def read_isa(isa_yaml):
    """Extract the ISA string from a riscv-config yaml without depending on PyYAML."""
    with open(isa_yaml) as f:
        m = re.search(r'^\s*ISA:\s*(\S+)', f.read(), re.M)
    if not m:
        raise SystemExit('No ISA field found in ' + isa_yaml)
    return m.group(1)


def check_tools():
    missing = [t for t in (TRIPLET + '-gcc', TRIPLET + '-objcopy', TRIPLET + '-nm', SAIL_EXE, 'verilator')
               if shutil.which(t) is None]
    if missing:
        raise SystemExit('Missing tools in PATH: ' + ', '.join(missing))
    if not os.path.isfile(os.path.join(ARCHTEST_ENV, 'arch_test.h')):
        raise SystemExit('arch_test.h not found; run "make arch-test-update" in ' + RISCOF_DIR)

    version = subprocess.run(['verilator', '--version'], capture_output=True, text=True).stdout
    m = re.search(r'Verilator (\d+)\.', version)
    if not m or int(m.group(1)) < 5:
        raise SystemExit('Verilator 5.x is required (--binary and timing); found: ' + version.strip())


def _rtl_sources():
    for d in ('rtl', 'rtl/aes', 'sim', 'RingBuffer/rtl', 'riscof'):
        path = os.path.join(RS5_ROOT, d)
        for name in os.listdir(path):
            if name.endswith(('.sv', '.svh', '.v')):
                yield os.path.join(path, name)


def build_model(isa, work_dir, coverage=DEFAULT_COVERAGE):
    """Verilate riscof_tb with coverage into <work_dir>/obj_dir, skipping it when up to date."""
    obj_dir = os.path.join(work_dir, 'obj_dir')
    binary  = os.path.join(obj_dir, 'Vriscof_tb')
    stamp   = os.path.join(obj_dir, 'fuzz_build_cmd')

    cmd = verilator_cmd(isa, obj_dir, ['--coverage-' + c for c in coverage])
    cmd_str = ' '.join(cmd)

    if os.path.exists(binary) and os.path.exists(stamp):
        with open(stamp) as f:
            same_cmd = f.read() == cmd_str
        newest_src = max(os.path.getmtime(p) for p in _rtl_sources())
        if same_cmd and os.path.getmtime(binary) > newest_src:
            print('[build] model up to date: ' + binary)
            return binary

    print('[build] ' + cmd_str)
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    with open(stamp, 'w') as f:
        f.write(cmd_str)
    return binary


def gcc_cmd(march, src, out, preprocess_only=False):
    """Same flags as the RISCOF plugins. A .S src is preprocessed, a .s src is only assembled."""
    return [TRIPLET + '-gcc', '-march=' + march, '-mabi=ilp32',
            '-static', '-mcmodel=medany', '-fvisibility=hidden', '-nostdlib', '-nostartfiles', '-g',
            '-T', LINKER, '-I', MODEL_ENV, '-I', ARCHTEST_ENV,
            '-DTEST_CASE_1=True', '-DXLEN=32'] + (['-E'] if preprocess_only else []) + [src, '-o', out]


def sail_args(isa):
    """Keep in sync with sail_cSim/riscof_sail_cSim.py (build)."""
    args = ['--disable-writable-misa', '--disable-vector-ext', '--pmp-count', '0']
    if 'C' not in isa:
        args.append('--disable-compressed')
    if 'F' not in isa and 'D' not in isa:
        args.append('--disable-fdext')
    if 'Zcb' in isa:
        args.append('--enable-zcb')
    return args
