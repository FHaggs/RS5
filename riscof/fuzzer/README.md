# RS5 fuzzer

Differential fuzzer with switchable feedback sources: each generated program runs on RS5
(Verilator) and on Sail, the signatures are compared, and several *feedback sources* are
recorded for the case (pipeline edges, trap/CSR transitions, Verilator line coverage, named
bins, scalars). A chosen subset of the sources guides the generator (corpus + energy); the rest
is only recorded, so sources can be compared on the same programs. It reuses the RISCOF pieces
(`rs5/env`, `arch_test.h`, `riscof_tb.sv`, the Verilator/Sail flags), so any saved case is a
regular RISCOF test.

The feedback sources are documented in **[FONTES.md](FONTES.md)** (Portuguese): what each one
observes, how a key becomes a map position, file formats, how to switch them on and off.

## Running

Same prerequisites as RISCOF (toolchain, Sail, Verilator 5.x with a C++20 compiler, `riscv-arch-test`).
On the GAPH machines `source fuzzer/env.sh` loads the right modules.

```sh
make fuzz                                         # baseline ISA, 200 cases, all cores
make fuzz FUZZ_CONFIG=extensions FUZZ_ARGS="-n 0" # extensions ISA, run until Ctrl-C
make fuzz-riscof                                  # replay the saved cases with RISCOF
python3 fuzzer/fuzz.py --help                     # all options

# feedback study: same generator and seed, different guiding source
python3 fuzzer/fuzz.py --work-dir $PWD/w_none -n 2000 --seed 1 --guide ""     # control, no feedback
python3 fuzzer/fuzz.py --work-dir $PWD/w_pipe -n 2000 --seed 1 --guide 0      # pipeline edges
python3 fuzzer/fuzz.py --work-dir $PWD/w_bins -n 2000 --seed 1 --guide 0,6 --weights 6=3
python3 fuzzer/feedback_report.py $PWD/w_none $PWD/w_pipe $PWD/w_bins -o $PWD/report
```

Core knobs (`--delay-cycles`, `--iqueue-size`, `--branchpred`, `--forwarding`) are the same
variables used by RISCOF; the fuzzer defaults to `DELAY_CYCLES=2` (data memory stalls) because it
exposes more pipeline paths.

Main options:

| Option | Meaning |
|---|---|
| `--record all\|none\|0,1,2,6,7` | sources recorded and logged (RTL probes via `+FUZZ_COV`, Verilator via the build) |
| `--guide 0,6` | subset of `--record` that decides corpus admission and energy (`""` = no feedback) |
| `--weights 6=3` | energy weight per source |
| `--schedule novelty\|rarity` | energy = 1 for novelty, or Σ weight/freq (rare positions pay more) |
| `--calibrate K` | every K-th case runs twice on RS5; positions that differ are logged |
| `--no-verilator-cov` | build without `--coverage` (no source 2, faster) |
| `--generator simple\|placeholder`, `--blocks 3,8`, `--mutate-p 0.8` | generator |

## Outputs (`<work-dir>/`)

| Path | Content |
|---|---|
| `obj_dir/Vriscof_tb` | model (probes always compiled in; coverage-instrumented unless `--no-verilator-cov`) |
| `cases.jsonl.gz` | one line per case: status, times, spec, novelty and positions per source, scalars, collisions, instability |
| `feedback_map.json` | final map: positions, buckets, frequency, first case and description per position |
| `run.json` | arguments, core configuration, feedback configuration |
| `suite/rv32i_m/fuzz/src/*.S` | cases that found novelty in a guiding source or failed (RISCOF suite) |
| `failures/<case>/` | `test.S`, ELF, `dut.sig`, `ref.sig`, `meta.json`, `fuzz_cov_*.txt`, `coverage.dat`, `result.json` |
| `report/` (from `feedback_report.py`) | `report.md` + SVG charts: saturation, bins, collisions, overlap, generator |

Case status: `pass`, `mismatch` (signatures differ), `dut_timeout` (Sail reached `tohost`, RS5 did not),
`hang` (neither finished: generator problem), `compile_error`, `error`.

## Architecture

```
main process                                   pool worker (one case, end to end)
------------                                   -----------------------------------
generator.generate() -> Program  ──submit──▶   render .s from cached preprocessed skeleton
                                               gcc → test.elf → nm (symbols) → objcopy → test.bin
                                               Vriscof_tb +SIG_* +FUZZ_COV +FUZZ_SRC → dut.sig, meta.json,
                                                                                      fuzz_cov_<src>.txt, coverage.dat
                                               riscv_sim_rv32d --inst-limit → ref.sig
FeedbackMap.merge(hits)  -> Novelty ◀──Result──   compare signatures, parse probe files and coverage.dat
generator.feedback(program, result, novelty)
log to cases.jsonl.gz, save to suite / failures
```

| File | Role |
|---|---|
| `fuzz.py` | CLI, process pool, feedback merge, logging, saving cases |
| `runner.py` | worker: build, run RS5 and Sail, compare, read the probe files and coverage.dat |
| `feedback.py` | source models (field tables mirrored from `fuzz_cov.sv`), `FeedbackMap`, `FeedbackConfig`, novelty/energy |
| `feedback_report.py` | report of how the map and the sources behaved over one or more runs |
| `generator.py` | `SimpleGenerator` (blocks + corpus + mutation) and the old `PlaceholderGenerator` |
| `program.py` | `Program` and the RISCOF-compatible `.S` template |
| `coverage.py` | `coverage.dat` parser (source 2) |
| `toolchain.py` | tool paths, model build, gcc/Sail flags |
| `../fuzz_cov.sv` | the probes: `fuzz_pipe_probe` (sources 0, 6, 7), `fuzz_csr_probe` (source 1), bound into RS5 by `riscof_tb.sv` |
| `../rs5/rs5_verilator.py` | Verilator command shared with the RISCOF plugin |

### Feedback

Each source is a region of an AFL-style map (8-bit counters, hit-count buckets, virgin bits).
At the end of a case the main process merges every recorded source; novelty (new position or
new bucket) in a *guiding* source gives the case a score (it enters the corpus) and an energy
(rarity of the positions it hit), which the generator uses to pick parents. See FONTES.md.

The generator must not expose non-architectural values to the signature: `mstatus`/`mie` reads
are masked (RS5 and Sail differ in reset/WARL bits), counter reads go to `x0` or are cleared.

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
