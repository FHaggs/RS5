#!/usr/bin/env python3
"""Coverage-guided differential fuzzer for RS5 (Verilator) against Sail.

The main process owns the generator and the global coverage map. Each test case runs
end-to-end (gcc -> RS5 -> Sail -> compare -> coverage) inside one pool worker, and the
pool is kept saturated: as soon as a case finishes, its feedback is merged and a new case
is submitted, without waiting for a whole batch.

Interesting cases (new coverage) and failures are written as a RISCOF suite under
<work-dir>/suite, so they can be replayed with `make fuzz-riscof`.
"""
import argparse
import getpass
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolchain  # noqa: E402
from coverage import CoverageMap  # noqa: E402
from generator import PlaceholderGenerator  # noqa: E402
from runner import RunConfig, init_worker, run_case  # noqa: E402


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--isa-yaml', default=os.path.join(toolchain.RISCOF_DIR, 'rs5', 'baseline.yaml'))
    p.add_argument('--work-dir', default=os.path.join(toolchain.RISCOF_DIR, 'fuzz_work'))
    p.add_argument('-n', '--iterations', type=int, default=200, help='number of test cases (0 = forever)')
    p.add_argument('-j', '--jobs', type=int, default=os.cpu_count())
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--toggle', action='store_true', help='also instrument toggle coverage (slower)')
    p.add_argument('--max-cycles', type=int, default=200000)
    p.add_argument('--keep', action='store_true', help='keep the directories of passing cases')
    p.add_argument('--show-new', type=int, default=5, help='new coverage points printed per case')
    return p.parse_args()


def scratch_dir(work_dir):
    # Many small files per case: use tmpfs when available
    base = '/dev/shm' if os.path.isdir('/dev/shm') else work_dir
    path = os.path.join(base, 'rs5fuzz-{}-{}'.format(getpass.getuser(), os.getpid()))
    os.makedirs(path, exist_ok=True)
    return path


class Fuzzer:
    def __init__(self, args, cfg, generator):
        self.args = args
        self.cfg = cfg
        self.gen = generator
        self.cov = CoverageMap()
        self.status = Counter()
        self.stage_time = defaultdict(float)
        self.done = 0
        self.suite_dir = os.path.join(args.work_dir, 'suite', 'rv32i_m', 'fuzz', 'src')
        self.fail_dir = os.path.join(args.work_dir, 'failures')
        os.makedirs(self.suite_dir, exist_ok=True)
        os.makedirs(self.fail_dir, exist_ok=True)

    def save_to_suite(self, program):
        with open(os.path.join(self.suite_dir, program.name + '.S'), 'w') as f:
            f.write(program.render())

    def handle(self, program, res):
        self.done += 1
        self.status[res.status] += 1
        for stage, t in res.times.items():
            self.stage_time[stage] += t

        self.cov.learn_names(res.names)
        new_points, new_buckets = self.cov.merge(res.hits)
        self.gen.feedback(program, res, new_points, new_buckets)

        meta = res.meta
        cpi = meta['cycles'] / meta['retired'] if meta.get('retired') else float('nan')
        print('[{:>6}] {:<18} {:<13} cycles={:<7} retired={:<7} cpi={:5.2f} hits={:<5} '
              'new={:<4} +bucket={:<4} total={}/{} ({:.1f}%)'.format(
                  self.done, program.name, res.status, meta.get('cycles', '-'), meta.get('retired', '-'),
                  cpi, len(res.hits), len(new_points), len(new_buckets),
                  self.cov.covered, self.cov.total, 100.0 * self.cov.covered / max(self.cov.total, 1)))
        for pid in new_points[:self.args.show_new]:
            print('           + ' + self.cov.describe(pid))
        if 0 < self.args.show_new < len(new_points):
            print('           + ... {} more'.format(len(new_points) - self.args.show_new))

        if res.status != 'pass':
            print('           ! ' + (res.detail.strip().replace('\n', '\n             ') or res.status))
            if res.case_dir and os.path.isdir(res.case_dir):
                dest = os.path.join(self.fail_dir, program.name)
                shutil.rmtree(dest, ignore_errors=True)
                shutil.move(res.case_dir, dest)
                with open(os.path.join(dest, 'result.json'), 'w') as f:
                    json.dump({'status': res.status, 'detail': res.detail, 'meta': meta}, f, indent=2)

        if new_points or res.status in ('mismatch', 'dut_timeout'):
            self.save_to_suite(program)

    def run(self):
        n = self.args.iterations
        max_inflight = 2 * self.args.jobs   # keeps workers busy while the main process merges
        next_id = 0
        inflight = {}
        start = time.time()

        with ProcessPoolExecutor(self.args.jobs, initializer=init_worker, initargs=(self.cfg,)) as pool:
            try:
                while True:
                    while len(inflight) < max_inflight and (n == 0 or next_id < n):
                        program = self.gen.generate(next_id)
                        inflight[pool.submit(run_case, next_id, program)] = program
                        next_id += 1
                    if not inflight:
                        break
                    finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in finished:
                        self.handle(inflight.pop(fut), fut.result())
            except KeyboardInterrupt:
                print('\nInterrupted, cancelling pending cases...')
                for fut in inflight:
                    fut.cancel()

        self.summary(time.time() - start)

    def summary(self, elapsed):
        print('\n==== summary ====')
        print('cases: {}  in {:.1f}s  ({:.1f} cases/s with {} jobs)'.format(
            self.done, elapsed, self.done / max(elapsed, 1e-9), self.args.jobs))
        print('status: ' + ', '.join('{}={}'.format(k, v) for k, v in sorted(self.status.items())))
        print('avg time per case: ' + ', '.join(
            '{}={:.1f}ms'.format(k, 1000 * v / max(self.done, 1)) for k, v in self.stage_time.items()))
        print('coverage: {}/{} points ({:.1f}%)'.format(
            self.cov.covered, self.cov.total, 100.0 * self.cov.covered / max(self.cov.total, 1)))

        uncovered = os.path.join(self.args.work_dir, 'uncovered.txt')
        with open(uncovered, 'w') as f:
            f.write('\n'.join(self.cov.uncovered()) + '\n')
        print('uncovered points: ' + uncovered)
        print('RISCOF suite (new coverage / failures): ' + os.path.dirname(os.path.dirname(os.path.dirname(self.suite_dir))))
        if self.status['mismatch'] or self.status['dut_timeout']:
            print('failures: ' + self.fail_dir)


def main():
    args = parse_args()
    os.makedirs(args.work_dir, exist_ok=True)

    toolchain.check_tools()
    isa = toolchain.read_isa(args.isa_yaml)
    coverage = toolchain.DEFAULT_COVERAGE + (('toggle',) if args.toggle else ())
    model = toolchain.build_model(isa, args.work_dir, coverage)

    scratch = scratch_dir(args.work_dir)
    cfg = RunConfig(isa=isa, model=model, scratch=scratch, max_cycles=args.max_cycles, keep=args.keep)
    print('[fuzz] ISA {}  jobs {}  scratch {}'.format(isa, args.jobs, scratch))

    try:
        Fuzzer(args, cfg, PlaceholderGenerator(isa, args.seed)).run()
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    main()
