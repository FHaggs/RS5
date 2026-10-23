#!/usr/bin/env python3
"""Fuzzer diferencial do RS5 (Verilator) contra o Sail, com feedback por fontes ligáveis.

O processo principal tem o gerador e o mapa de feedback (feedback.py). Cada caso roda
de ponta a ponta (gcc -> RS5 -> Sail -> comparação -> leitura das fontes) num worker do
pool, que é mantido cheio: assim que um caso termina, o feedback é aplicado e outro caso
é submetido.

Fontes de feedback (ver FONTES.md): todas as de --record são gravadas e vão para o log;
só as de --guide decidem o que entra no corpus do gerador e quanta energia recebe.
--guide "" é o controle sem feedback (o gerador só gera programas novos).

Saídas em <work-dir>:
    cases.jsonl.gz      uma linha por caso: status, tempos, spec, novidade e posições por fonte
    feedback_map.json   o mapa final (posições, buckets, frequência, descrição)
    suite/              casos com novidade ou falha como suíte RISCOF (replay: make fuzz-riscof)
    failures/<case>/    tudo de um caso que não passou
"""
import argparse
import getpass
import gzip
import json
import os
import shutil
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import toolchain  # noqa: E402
from feedback import SOURCES, FeedbackConfig, FeedbackMap  # noqa: E402
from generator import PlaceholderGenerator, SimpleGenerator  # noqa: E402
from runner import RunConfig, init_worker, run_case  # noqa: E402

# Configuração "interessante" do core para o estudo das fontes: a memória de dados demora
# DELAY_CYCLES ciclos (stall ativo), o que expõe os caminhos de stall do pipeline.
CORE_DEFAULTS = {'BRANCHPRED': '1', 'FORWARDING': '1', 'DUALPORT_MEM': '1',
                 'IQUEUE_SIZE': '2', 'DELAY_CYCLES': '2'}


def parse_args():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--isa-yaml', default=os.path.join(toolchain.RISCOF_DIR, 'rs5', 'baseline.yaml'))
    p.add_argument('--work-dir', default=os.path.join(toolchain.RISCOF_DIR, 'fuzz_work'))
    p.add_argument('-n', '--iterations', type=int, default=200, help='number of test cases (0 = forever)')
    p.add_argument('-j', '--jobs', type=int, default=os.cpu_count())
    p.add_argument('--seed', type=int, default=None)
    p.add_argument('--max-cycles', type=int, default=200000)
    p.add_argument('--keep', action='store_true', help='keep the directories of passing cases')
    p.add_argument('--show-new', type=int, default=3, help='new positions printed per case and source')
    p.add_argument('--log-hits', action='store_true', default=True,
                   help='store the per-case hits in cases.jsonl.gz (default on; needed by feedback_report.py)')
    p.add_argument('--no-log-hits', dest='log_hits', action='store_false')
    p.add_argument('--calibrate', type=int, default=0, metavar='K',
                   help='run every K-th case twice on RS5 and log the positions that differ (stability)')
    # feedback
    p.add_argument('--record', default='all', help='sources to record: all | none | 0,1,2,6,7')
    p.add_argument('--guide', default='', help='sources that decide corpus/energy ("" = no feedback)')
    p.add_argument('--weights', default='', help='energy weight per source, e.g. 0=1,6=3')
    p.add_argument('--schedule', choices=('novelty', 'rarity', 'uniform'), default='rarity',
                   help='parent energy: 1 for cases with novelty, sum(weight/freq) over hit positions, or 1 for all')
    p.add_argument('--score', choices=('buckets', 'positions'), default='buckets',
                   help='what counts as novelty for corpus admission: new positions and new buckets, or positions only')
    p.add_argument('--corpus', choices=('novelty', 'all'), default='novelty',
                   help='corpus admission: cases with novelty in a guiding source, or every passing case (random-corpus control)')
    p.add_argument('--no-verilator-cov', dest='verilator_cov', action='store_false',
                   help='build without --coverage (source 2 unavailable, faster)')
    p.add_argument('--trace', action='store_true', help='write the RS5Debug trace per case (+TRACE, slow)')
    # generator
    p.add_argument('--generator', choices=('simple', 'placeholder'), default='simple')
    p.add_argument('--blocks', default='3,8', help='min,max blocks per program')
    p.add_argument('--mutate-p', type=float, default=0.8, help='fraction of cases mutated from the corpus')
    p.add_argument('--traps', action='store_true', help='trap handler + trap/irq/umode blocks (reserves x28..x30)')
    p.add_argument('--kinds', default='', help='block kinds to generate (default: all for the generator mode)')
    # core configuration (same variables as riscof/Makefile; defaults in CORE_DEFAULTS)
    for var, default in CORE_DEFAULTS.items():
        p.add_argument('--' + var.lower().replace('_', '-'), default=os.environ.get(var, default))
    return p.parse_args()


def scratch_dir(work_dir):
    base = '/dev/shm' if os.path.isdir('/dev/shm') else work_dir
    path = os.path.join(base, 'rs5fuzz-{}-{}'.format(getpass.getuser(), os.getpid()))
    os.makedirs(path, exist_ok=True)
    return path


class Fuzzer:
    def __init__(self, args, cfg, fb_cfg, generator):
        self.args = args
        self.cfg = cfg
        self.fb_cfg = fb_cfg
        self.gen = generator
        self.map = FeedbackMap(fb_cfg)
        self.status = Counter()
        self.stage_time = defaultdict(float)
        self.done = 0
        self.suite_dir = os.path.join(args.work_dir, 'suite', 'rv32i_m', 'fuzz', 'src')
        self.fail_dir = os.path.join(args.work_dir, 'failures')
        os.makedirs(self.suite_dir, exist_ok=True)
        os.makedirs(self.fail_dir, exist_ok=True)
        self.log = gzip.open(os.path.join(args.work_dir, 'cases.jsonl.gz'), 'wt')
        self.t0 = time.time()

    def save_to_suite(self, program):
        with open(os.path.join(self.suite_dir, program.name + '.S'), 'w') as f:
            f.write(program.render())

    def handle(self, program, res):
        self.done += 1
        self.status[res.status] += 1
        for stage, t in res.times.items():
            self.stage_time[stage] += t

        self.map.learn_names(res.names)
        nov = self.map.merge(res.case_id, res.hits, res.raws, res.scalars)
        self.gen.feedback(program, res, nov)

        meta = res.meta
        per_src = {}
        for src in sorted(SOURCES):
            n = sum(1 for (s, _) in res.hits if s == src)
            if src == 7:
                n = len(res.scalars)
            if n or src in self.fb_cfg.record:
                new_p, new_b = nov.count(src)
                per_src[src] = (n, new_p, new_b)
        cpi = meta['cycles'] / meta['retired'] if meta.get('retired') else float('nan')
        print('[{:>6}] {:<10} {:<11} cyc={:<6} cpi={:4.2f} {} score={:<4.0f} E={:<6.2f} corpus={}'.format(
            self.done, program.name, res.status, meta.get('cycles', '-'), cpi,
            ' '.join('s{}={}+{}/{}'.format(s, v[1], v[2], v[0]) for s, v in per_src.items()),
            nov.score, nov.energy, len(getattr(self.gen, 'corpus', ()))))
        for src in sorted(nov.new_positions):
            if src not in self.fb_cfg.guide and self.fb_cfg.guide:
                continue
            for idx in nov.new_positions[src][:self.args.show_new]:
                print('           +{} {}'.format(src, self.map.describe(src, idx)))

        if res.unstable:
            print('           ~ unstable positions: {}'.format(res.unstable[:8]))
        if res.status != 'pass':
            print('           ! ' + (res.detail.strip().replace('\n', '\n             ') or res.status))
            if res.case_dir and os.path.isdir(res.case_dir):
                dest = os.path.join(self.fail_dir, program.name)
                shutil.rmtree(dest, ignore_errors=True)
                shutil.move(res.case_dir, dest)
                with open(os.path.join(dest, 'result.json'), 'w') as f:
                    json.dump({'status': res.status, 'detail': res.detail, 'meta': meta, 'spec': program.spec}, f, indent=2)

        if nov.score > 0 or res.status in ('mismatch', 'dut_timeout'):
            self.save_to_suite(program)

        entry = {
            'id': res.case_id, 'name': program.name, 'status': res.status, 't': round(time.time() - self.t0, 3),
            'cycles': meta.get('cycles'), 'retired': meta.get('retired'),
            'times': {k: round(v, 4) for k, v in res.times.items()},
            'spec': program.spec,
            'score': nov.score, 'energy': round(nov.energy, 4), 'corpus': len(getattr(self.gen, 'corpus', ())),
            'new': {str(s): [len(v), len(nov.new_buckets.get(s, ()))] for s, v in nov.new_positions.items()},
            'new_buckets_only': {str(s): len(v) for s, v in nov.new_buckets.items() if s not in nov.new_positions},
            'positions': {str(s): sum(1 for (x, _) in res.hits if x == s) for s in SOURCES if s != 7},
            'scalars': res.scalars,
            'collisions': dict(Counter(str(s) for s, _ in res.collided)),
            'unstable': res.unstable,
            'total_positions': {str(s): r.positions for s, r in self.map.regions.items()},
        }
        if self.args.log_hits:
            hits = defaultdict(dict)
            for (s, idx), c in res.hits.items():
                hits[str(s)][str(idx)] = c
            entry['hits'] = hits
        self.log.write(json.dumps(entry) + '\n')

    def run(self):
        n = self.args.iterations
        max_inflight = 2 * self.args.jobs
        next_id = 0
        inflight = {}
        start = time.time()

        with ProcessPoolExecutor(self.args.jobs, initializer=init_worker, initargs=(self.cfg,)) as pool:
            try:
                while True:
                    while len(inflight) < max_inflight and (n == 0 or next_id < n):
                        program = self.gen.generate(next_id)
                        twice = self.args.calibrate > 0 and next_id % self.args.calibrate == 0
                        inflight[pool.submit(run_case, next_id, program, twice)] = program
                        next_id += 1
                    if not inflight:
                        break
                    finished, _ = wait(inflight, return_when=FIRST_COMPLETED)
                    for fut in sorted(finished, key=lambda f: inflight[f].name):
                        self.handle(inflight.pop(fut), fut.result())
            except KeyboardInterrupt:
                print('\nInterrupted, cancelling pending cases...')
                for fut in inflight:
                    fut.cancel()

        self.log.close()
        self.summary(time.time() - start)

    def summary(self, elapsed):
        print('\n==== summary ====')
        print('cases: {}  in {:.1f}s  ({:.2f} cases/s with {} jobs)'.format(
            self.done, elapsed, self.done / max(elapsed, 1e-9), self.args.jobs))
        print('status: ' + ', '.join('{}={}'.format(k, v) for k, v in sorted(self.status.items())))
        print('avg time per case: ' + ', '.join(
            '{}={:.1f}ms'.format(k, 1000 * v / max(self.done, 1)) for k, v in self.stage_time.items()))
        print('feedback: ' + self.fb_cfg.describe())
        for src, info in self.map.summary().items():
            print('  source {} {:<10} positions={:<6} buckets={}'.format(src, info['name'], info['positions'], info['buckets']))
        for name, (v, cid) in sorted(self.map.scalar_best.items()):
            from feedback import SCALARS
            print('  scalar {:<18} max={:<8} (case {})'.format(SCALARS[name] if name < len(SCALARS) else name, v, cid))
        print('generator: ' + ', '.join('{}={}'.format(k, v) for k, v in self.gen.stats.items()))
        path = os.path.join(self.args.work_dir, 'feedback_map.json')
        self.map.save(path)
        print('map: ' + path)
        print('log: ' + os.path.join(self.args.work_dir, 'cases.jsonl.gz'))
        if self.status['mismatch'] or self.status['dut_timeout']:
            print('failures: ' + self.fail_dir)


def main():
    args = parse_args()
    os.makedirs(args.work_dir, exist_ok=True)
    for var in CORE_DEFAULTS:
        os.environ[var] = getattr(args, var.lower())

    toolchain.check_tools()
    isa = toolchain.read_isa(args.isa_yaml)
    coverage = toolchain.DEFAULT_COVERAGE if args.verilator_cov else ()
    model = toolchain.build_model(isa, args.work_dir, coverage)

    record = FeedbackConfig.parse_sources(args.record)
    guide = FeedbackConfig.parse_sources(args.guide)
    if not args.verilator_cov:
        record = record - {2}
    if 3 in record and 0 not in record:
        raise SystemExit('source 3 is derived from source 0: add 0 to --record')
    if not guide <= record:
        raise SystemExit('--guide must be a subset of --record')
    weights = {}
    for item in args.weights.split(','):
        if item.strip():
            k, v = item.split('=')
            weights[int(k)] = float(v)
    fb_cfg = FeedbackConfig(record=record, guide=guide, weight=weights, schedule=args.schedule,
                            verilator_cov=args.verilator_cov, score=args.score)

    scratch = scratch_dir(args.work_dir)
    cfg = RunConfig(isa=isa, model=model, scratch=scratch, max_cycles=args.max_cycles, keep=args.keep,
                    record=record, verilator_cov=args.verilator_cov, trace=args.trace)
    core = ' '.join('{}={}'.format(v, os.environ[v]) for v in CORE_DEFAULTS)
    print('[fuzz] ISA {}  jobs {}  {}  corpus={} traps={}  core: {}'.format(
        isa, args.jobs, fb_cfg.describe(), args.corpus, args.traps, core))
    with open(os.path.join(args.work_dir, 'run.json'), 'w') as f:
        json.dump({'args': vars(args), 'isa': isa, 'core': {v: os.environ[v] for v in CORE_DEFAULTS},
                   'feedback': {'record': sorted(record), 'guide': sorted(guide), 'schedule': args.schedule,
                                'weights': weights, 'score': args.score, 'corpus': args.corpus},
                   'generator': {'kind': args.generator, 'traps': args.traps, 'kinds': args.kinds,
                                 'blocks': args.blocks, 'mutate_p': args.mutate_p}}, f, indent=2)

    lo, hi = (int(x) for x in args.blocks.split(','))
    if args.generator == 'simple':
        kinds = [k for k in args.kinds.split(',') if k] or None
        gen = SimpleGenerator(isa, args.seed, lo, hi, args.mutate_p, traps=args.traps, kinds=kinds, corpus=args.corpus)
    else:
        gen = PlaceholderGenerator(isa, args.seed)

    try:
        Fuzzer(args, cfg, fb_cfg, gen).run()
    finally:
        if not args.keep:
            shutil.rmtree(scratch, ignore_errors=True)


if __name__ == '__main__':
    main()
