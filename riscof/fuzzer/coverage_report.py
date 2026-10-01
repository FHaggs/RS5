#!/usr/bin/env python3
"""Coverage and metrics report for a RISCOF work dir run with RS5_COVERAGE=1.

Reads <work>/**/dut/{coverage.dat,meta.json} with the same parser the fuzzer uses and
writes <work>/coverage_report/{tests.csv,modules.csv,uncovered.txt,summary.json}.
"""
import argparse
import csv
import glob
import json
import os
import shutil
import subprocess
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from coverage import CoverageMap, parse_coverage_dat  # noqa: E402


def read_lines(path):
    try:
        with open(path) as f:
            return [line.strip().lower() for line in f if line.strip()]
    except FileNotFoundError:
        return None


def module_of(desc):
    """'v_line riscof_tb.dut.execute1.gen_div_on.div1 ...' -> 'execute1'"""
    hier = desc.split()[1].split('.')
    return hier[2] if len(hier) > 2 else '<top>'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('work_dir')
    p.add_argument('--annotate', action='store_true',
                   help='also merge with verilator_coverage and annotate the RTL sources')
    args = p.parse_args()

    dut_dirs = sorted(os.path.dirname(m) for m in
                      glob.glob(os.path.join(args.work_dir, '**', 'dut', 'coverage.dat'), recursive=True))
    if not dut_dirs:
        raise SystemExit('No dut/coverage.dat found: was riscof run with RS5_COVERAGE=1?')

    out_dir = os.path.join(args.work_dir, 'coverage_report')
    os.makedirs(out_dir, exist_ok=True)

    cov = CoverageMap()
    per_test_hits = {}
    rows = []
    for d in dut_dirs:
        test = os.path.relpath(os.path.dirname(d), args.work_dir)
        hits, names = parse_coverage_dat(os.path.join(d, 'coverage.dat'), with_names=not cov.names)
        cov.learn_names(names)
        new_points, new_buckets = cov.merge(hits)
        per_test_hits[test] = hits

        with open(os.path.join(d, 'meta.json')) as f:
            meta = json.load(f)
        dut_sig = read_lines(os.path.join(d, 'DUT-RS5.signature'))
        ref_sig = read_lines(os.path.join(os.path.dirname(d), 'ref', 'Reference-sail_c_simulator.signature'))
        status = 'pass' if dut_sig is not None and dut_sig == ref_sig else 'FAIL'

        rows.append({
            'test': test, 'status': status, 'exit': meta['exit'],
            'cycles': meta['cycles'], 'retired': meta['retired'],
            'cpi': round(meta['cycles'] / meta['retired'], 3) if meta['retired'] else '',
            'hits': len(hits), 'new': len(new_points), 'new_bucket': len(new_buckets),
            'cumulative': cov.covered,
        })

    # Points hit by exactly one test: that test is the only one exercising them
    owners = Counter(pid for hits in per_test_hits.values() for pid in hits)
    for row in rows:
        row['unique'] = sum(1 for pid in per_test_hits[row['test']] if owners[pid] == 1)

    with open(os.path.join(out_dir, 'tests.csv'), 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    kinds, modules = Counter(), {}
    for pid, desc in cov.names.items():
        kind, mod = desc.split()[0], module_of(desc)
        hit = pid in cov.max_bucket
        kinds[(kind, hit)] += 1
        m = modules.setdefault(mod, [0, 0])
        m[0] += hit
        m[1] += 1

    with open(os.path.join(out_dir, 'modules.csv'), 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['module', 'covered', 'total', 'percent'])
        for mod, (c, t) in sorted(modules.items(), key=lambda kv: kv[1][0] / kv[1][1]):
            w.writerow([mod, c, t, round(100.0 * c / t, 1)])

    with open(os.path.join(out_dir, 'uncovered.txt'), 'w') as f:
        f.write('\n'.join(cov.uncovered()) + '\n')

    summary = {
        'tests': len(rows),
        'failed': sum(r['status'] != 'pass' for r in rows),
        'timeouts': sum(r['exit'] != 'tohost' for r in rows),
        'covered': cov.covered, 'total': cov.total,
        'by_kind': {k: {'covered': kinds[(k, True)], 'total': kinds[(k, True)] + kinds[(k, False)]}
                    for k in sorted({k for k, _ in kinds})},
        'cycles': sum(r['cycles'] for r in rows),
        'retired': sum(r['retired'] for r in rows),
    }
    with open(os.path.join(out_dir, 'summary.json'), 'w') as f:
        json.dump(summary, f, indent=2)

    # ---- console ----
    print('{:<45} {:<6} {:<7} {:>8} {:>8} {:>6} {:>5} {:>5} {:>6} {:>6}'.format(
        'test', 'status', 'exit', 'cycles', 'retired', 'cpi', 'hits', 'new', 'unique', 'total'))
    for r in rows:
        print('{:<45} {:<6} {:<7} {:>8} {:>8} {:>6} {:>5} {:>5} {:>6} {:>6}'.format(
            r['test'][-45:], r['status'], r['exit'], r['cycles'], r['retired'], r['cpi'],
            r['hits'], r['new'], r['unique'], r['cumulative']))

    print('\ntests: {tests}  failed: {failed}  timeouts: {timeouts}'.format(**summary))
    print('coverage: {}/{} ({:.1f}%)'.format(cov.covered, cov.total, 100.0 * cov.covered / cov.total))
    for k, v in summary['by_kind'].items():
        print('  {:<9} {:>5}/{:<5} ({:.1f}%)'.format(k, v['covered'], v['total'], 100.0 * v['covered'] / v['total']))
    print('per module (least covered first):')
    for mod, (c, t) in sorted(modules.items(), key=lambda kv: kv[1][0] / kv[1][1]):
        print('  {:<20} {:>5}/{:<5} ({:.1f}%)'.format(mod, c, t, 100.0 * c / t))
    print('\nreport: ' + out_dir)

    if args.annotate:
        if shutil.which('verilator_coverage') is None:
            raise SystemExit('verilator_coverage not found in PATH')
        dats = [os.path.join(d, 'coverage.dat') for d in dut_dirs]
        merged = os.path.join(out_dir, 'merged.dat')
        subprocess.run(['verilator_coverage', '--write', merged] + dats, check=True)
        subprocess.run(['verilator_coverage', '--annotate', os.path.join(out_dir, 'annotated'), merged],
                       check=True)
        print('annotated sources: ' + os.path.join(out_dir, 'annotated'))


if __name__ == '__main__':
    main()
