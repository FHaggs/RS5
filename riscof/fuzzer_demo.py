#!/usr/bin/env python3

import argparse
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


RISCOF_DIR = Path(__file__).resolve().parent
DEFAULT_SUITE_DIR = RISCOF_DIR / "fuzzer_demo_suite"
DEFAULT_WORK_DIR = RISCOF_DIR / "fuzzer_demo_work"
DEFAULT_CONFIG = RISCOF_DIR / "baseline.ini"


@dataclass(frozen=True)
class AssemblyTest:
    name: str
    code: str


TESTS = (
    AssemblyTest(
        name="demo-add",
        code="""\
  li t0, 20
  li t1, 22
  add t2, t0, t1
  li t3, -7
  addi t4, t3, 12

  la t5, signature
  sw t2, 0(t5)
  sw t4, 4(t5)
""",
    ),
    AssemblyTest(
        name="demo-branch-memory",
        code="""\
  la t0, scratch
  li t1, 0x13579bdf
  sw t1, 0(t0)
  lw t2, 0(t0)
  bne t1, t2, mismatch
  li t3, 1
  j store_results
mismatch:
  li t3, 0
store_results:
  la t4, signature
  sw t2, 0(t4)
  sw t3, 4(t4)
""",
    ),
)


ARCH_TEST_HEADER = """\
#ifndef FUZZER_DEMO_ARCH_TEST_H
#define FUZZER_DEMO_ARCH_TEST_H

#define RVTEST_ISA(_ISA)
#define RVTEST_CASE(_CASE, _CONDITION, ...)
#define RVTEST_CODE_BEGIN
#define RVTEST_CODE_END
#define RVTEST_DATA_BEGIN
#define RVTEST_DATA_END

#endif
"""


def render_test(test: AssemblyTest) -> str:
    return f"""\
#include "model_test.h"
#include "arch_test.h"

RVTEST_ISA("RV32I")

.section .text.init
.globl rvtest_entry_point
rvtest_entry_point:
RVMODEL_BOOT
RVTEST_CODE_BEGIN

#ifdef TEST_CASE_1
RVTEST_CASE(0,"//check ISA:=regex(.*32.*);check ISA:=regex(.*I.*);def TEST_CASE_1=True;",{test.name.replace("-", "_")})

{test.code.rstrip()}

RVTEST_CODE_END
RVMODEL_HALT
#endif

RVMODEL_DATA_BEGIN
.align 4
signature:
  .fill 2, 4, 0xdeadbeef
scratch:
  .word 0
RVMODEL_DATA_END
"""


def generate_suite(suite_dir: Path) -> list[Path]:
    source_dir = suite_dir / "rv32i_m" / "I" / "src"
    env_dir = suite_dir / "env"
    source_dir.mkdir(parents=True, exist_ok=True)
    env_dir.mkdir(parents=True, exist_ok=True)

    generated = []
    header = env_dir / "arch_test.h"
    header.write_text(ARCH_TEST_HEADER, encoding="ascii")
    generated.append(header)

    expected_sources = set()
    for test in TESTS:
        source = source_dir / f"{test.name}.S"
        source.write_text(render_test(test), encoding="ascii")
        generated.append(source)
        expected_sources.add(source.name)

    for source in source_dir.glob("*.S"):
        if source.name not in expected_sources:
            source.unlink()

    return generated


def required_tools() -> list[str]:
    triplet = os.environ.get("TRIPLET", "riscv64-elf")
    return [
        "riscof",
        f"{triplet}-gcc",
        f"{triplet}-objcopy",
        f"{triplet}-readelf",
        "riscv_sim_rv32d",
        "verilator",
        "make",
    ]


def check_prerequisites() -> None:
    missing = [tool for tool in required_tools() if shutil.which(tool) is None]
    if missing:
        joined = ", ".join(missing)
        raise RuntimeError(
            f"missing required executable(s): {joined}. "
            "See riscof/README.md for installation instructions."
        )


def build_riscof_command(
    suite_dir: Path, work_dir: Path, config: Path
) -> list[str]:
    return [
        "riscof",
        "run",
        f"--suite={suite_dir.resolve()}",
        f"--env={(suite_dir / 'env').resolve()}",
        f"--work-dir={work_dir.resolve()}",
        "--no-browser",
        f"--config={config.resolve()}",
    ]


def run_demo(suite_dir: Path, work_dir: Path, config: Path) -> None:
    check_prerequisites()
    env = os.environ.copy()
    env.setdefault("TRIPLET", "riscv64-elf")
    env.setdefault("BRANCHPRED", "1")
    env.setdefault("FORWARDING", "1")
    env.setdefault("DUALPORT_MEM", "1")
    env.setdefault("IQUEUE_SIZE", "2")
    env.setdefault("DELAY_CYCLES", "0")
    subprocess.run(
        build_riscof_command(suite_dir, work_dir, config),
        cwd=RISCOF_DIR,
        env=env,
        check=True,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate two assembly tests and compare RS5 against Sail with RISCOF."
    )
    parser.add_argument(
        "--generate-only",
        action="store_true",
        help="generate the suite without invoking RISCOF",
    )
    parser.add_argument("--suite-dir", type=Path, default=DEFAULT_SUITE_DIR)
    parser.add_argument("--work-dir", type=Path, default=DEFAULT_WORK_DIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    generated = generate_suite(args.suite_dir)
    print("Generated:")
    for path in generated:
        print(f"  {path}")

    if args.generate_only:
        return 0

    try:
        run_demo(args.suite_dir, args.work_dir, args.config)
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Fuzzer demo failed: {error}")
        return 1

    print(f"RISCOF comparison passed. Report: {args.work_dir / 'report.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
