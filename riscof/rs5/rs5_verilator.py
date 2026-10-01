"""Verilator command for riscof_tb.sv, shared by the RISCOF plugin and the fuzzer.

Keep this module free of third-party imports: the fuzzer imports it without riscof.
"""
import os

RS5_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

# ISA substring -> riscof_tb parameter enabled by it
EXTENSION_PARAMS = [
    ("M",      "MEnable"),
    ("A",      "AEnable"),
    ("C",      "COMPRESSED"),
    ("Zicond", "ZICONDEnable"),
    ("Zihpm",  "HPMCOUNTEREnable"),
    ("Zkne",   "ZKNEEnable"),
    ("Zcb",    "ZCBEnable"),
    ("Zbkb",   "ZBKBEnable"),
    ("Zknh",   "ZKNHEnable"),
]

REQUIRED_EXTENSIONS = ["I", "U", "Zicsr"]

# Microarchitecture knobs taken from the environment (defaults match riscof/Makefile)
ENV_PARAMS = [
    # (env var, parameter, default, is_bit)
    ("BRANCHPRED",   "BRANCHPRED",   "1", True),
    ("FORWARDING",   "FORWARDING",   "1", True),
    ("DUALPORT_MEM", "DUALPORT_MEM", "1", True),
    ("IQUEUE_SIZE",  "IQUEUE_SIZE",  "2", False),
    ("DELAY_CYCLES", "DELAY_CYCLES", "0", False),
]


def coverage_args_from_env():
    """RS5_COVERAGE: unset/0 = off, 1 = line,user, or an explicit list such as line,toggle,user."""
    value = os.environ.get('RS5_COVERAGE', '0').strip()
    if value in ('', '0'):
        return []
    kinds = ['line', 'user'] if value == '1' else [k.strip() for k in value.split(',') if k.strip()]
    return ['--coverage-' + k for k in kinds]


def check_isa(isa):
    for ext in REQUIRED_EXTENSIONS:
        if ext not in isa:
            raise SystemExit("ISA should contain " + ext + ".")


def verilator_cmd(isa, obj_dir, extra_args=()):
    """Return the verilator argv that builds <obj_dir>/Vriscof_tb for the given ISA string."""
    check_isa(isa)

    cmd = [
        'verilator', '--cc', '--exe', '--binary', '--timescale', '1ns/1ns', '-j', '0',
        '--Mdir', obj_dir,
        '-I' + RS5_ROOT + '/RingBuffer/rtl/',
        '-I' + RS5_ROOT + '/rtl/',
        '-I' + RS5_ROOT + '/sim/',
        '-I' + RS5_ROOT + '/rtl/aes',
        RS5_ROOT + '/riscof/riscof_tb.sv',
    ]

    for ext, param in EXTENSION_PARAMS:
        if ext in isa:
            cmd.append("-G" + param + "=1'b1")

    for var, param, default, is_bit in ENV_PARAMS:
        value = os.environ.get(var, default)
        cmd.append("-G" + param + "=" + ("1'b" + value if is_bit else value))

    return cmd + list(extra_args)
