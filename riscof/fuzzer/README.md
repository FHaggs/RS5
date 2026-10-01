# RS5 fuzzer

Coverage-guided differential fuzzer: each generated program runs on RS5 (Verilator, with
coverage instrumentation) and on Sail, the signatures are compared and the RS5 coverage is
fed back to the generator. It reuses the RISCOF pieces (`rs5/env`, `arch_test.h`, `riscof_tb.sv`,
the Verilator/Sail flags), so any saved case is a regular RISCOF test.

The current generator is a placeholder (hardcoded bodies with random initial values).

## Running

Same prerequisites as RISCOF (toolchain, Sail, Verilator 5.x with a C++20 compiler, `riscv-arch-test`).

```sh
make fuzz                                         # baseline ISA, 200 cases, all cores
make fuzz FUZZ_CONFIG=extensions FUZZ_ARGS="-n 0" # extensions ISA, run until Ctrl-C
make fuzz-riscof                                  # replay the saved cases with RISCOF
python3 fuzzer/fuzz.py --help                     # all options
```

Microarchitecture knobs (`BRANCHPRED`, `DELAY_CYCLES`, ...) are the same Makefile variables used by RISCOF.

## Outputs (`fuzz_work/`)

| Path | Content |
|---|---|
| `obj_dir/Vriscof_tb` | coverage-instrumented model, rebuilt only when RTL or flags change |
| `suite/rv32i_m/fuzz/src/*.S` | cases that found new coverage or failed (RISCOF suite) |
| `failures/<case>/` | `test.S`, ELF, `dut.sig`, `ref.sig`, `meta.json`, `coverage.dat`, `result.json` |
| `uncovered.txt` | RS5 coverage points never hit |

Case status: `pass`, `mismatch` (signatures differ), `dut_timeout` (Sail reached `tohost`, RS5 did not),
`hang` (neither finished: generator problem), `compile_error`, `error`.

## Architecture

```
main process                                   pool worker (one case, end to end)
------------                                   -----------------------------------
generator.generate() -> Program  ──submit──▶   render .s from cached preprocessed skeleton
                                               gcc → test.elf → nm (symbols) → objcopy → test.bin
                                               Vriscof_tb +SIG_*/+META_PATH/+MAX_CYCLES → dut.sig, meta.json, coverage.dat
                                               riscv_sim_rv32d --inst-limit → ref.sig
CoverageMap.merge(hits)          ◀──Result──   compare signatures, parse coverage.dat (DUT scope only)
generator.feedback(...)
save to suite / failures
```

| File | Role |
|---|---|
| `fuzz.py` | CLI, process pool, coverage merge, reporting, saving cases |
| `runner.py` | worker: build, run RS5 and Sail, compare, collect coverage/metadata |
| `generator.py` | test generation (placeholder) — `generate()` / `feedback()` |
| `program.py` | `Program` and the RISCOF-compatible `.S` template |
| `coverage.py` | `coverage.dat` parser and the global `CoverageMap` |
| `toolchain.py` | tool paths, model build, gcc/Sail flags |
| `../rs5/rs5_verilator.py` | Verilator command shared with the RISCOF plugin |

### Feedback available to the generator

* **Coverage** (`--coverage-line --coverage-user`, `--toggle` adds toggle coverage): hit count per
  point, merged into AFL-style buckets. `feedback()` receives the new points and the points that
  reached a new count bucket.
* **Metadata** from `riscof_tb` (`+META_PATH`): exit reason, cycles and retired instructions (CPI).
* **Per-stage times**, to spot where throughput is going.

Functional coverage can be added as SystemVerilog `cover property` statements; they show up as
`v_user` points without changes to the fuzzer.

### Parallelism

* One case = one worker, end to end. Every step is a short single-threaded process
  (a Verilator model this small gains nothing from `--threads`), so parallelism comes from
  running many cases at once, one per core.
* The pool is kept saturated (`2 × jobs` cases in flight) and feedback is merged as each case
  finishes, instead of in synchronous batches: no core waits for the slowest case of a batch.
* Workers return only small data (hashed coverage ids and counts); coverage point names are
  sent once per worker.
* Case directories live in `/dev/shm` (tmpfs) and are deleted when the case passes.
* The expensive part of compiling (the C preprocessor expanding `arch_test.h`) is done once per
  worker and reused (~40% less compile time).

The compile step (gcc/as/ld + nm + objcopy) is still the largest cost per case, followed by the process
startup of RS5 and Sail. Next steps, in order of expected gain:

1. Emit machine code directly from Python and patch it into a pre-linked image (no gcc per case).
2. Persistent RS5 simulator: a custom Verilator `main` that runs many programs per process
   (reset between them) and resets coverage with `VerilatedCov::zero()`, fed through a pipe.
3. Several programs per Sail invocation, or Sail as a library.
