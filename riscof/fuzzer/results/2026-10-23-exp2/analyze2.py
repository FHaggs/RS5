"""Comparações do experimento 2. Uso: analyze2.py <dir_old_exp> <dir_exp2>"""
import gzip, json, os, sys, statistics
from collections import Counter
E0, E2 = sys.argv[1], sys.argv[2]

_cache = {}
def load(name):
    if name in _cache:
        return _cache[name]
    d = os.path.join(E2, name) if os.path.isdir(os.path.join(E2, name)) else os.path.join(E0, name)
    m = json.load(open(os.path.join(d, 'feedback_map.json')))
    cases = []
    for l in gzip.open(os.path.join(d, 'cases.jsonl.gz'), 'rt'):
        x = json.loads(l)
        x['hits'] = {'6': x.get('hits', {}).get('6', {})}      # só os bins ficam na memória
        cases.append(x)
    _cache[name] = (m, cases)
    return _cache[name]

def edges(m, src='0'):
    return {r['idx']: r for r in m['regions'].get(src, {'rows': []})['rows']}

def summarize(names, src='0'):
    tot = []
    for n in names:
        m, c = load(n)
        tot.append(len(edges(m, src)))
    return '{:.0f} ± {:.0f} (n={})'.format(statistics.mean(tot), statistics.pstdev(tot) if len(tot) > 1 else 0, len(tot)) if tot else '-'

def exclusive(name, refs, src='0'):
    m, _ = load(name)
    mine = set(edges(m, src))
    ref = set()
    for r in refs:
        if r == name: continue
        ref |= set(edges(load(r)[0], src))
    return len(mine - ref)

def status(name):
    _, c = load(name)
    return dict(Counter(x['status'] for x in c))

def per_op(name, src='0'):
    _, c = load(name)
    n = Counter(); new = Counter()
    for x in c:
        op = (x['spec'] or {}).get('op', 'random'); n[op] += 1; new[op] += x['new'].get(src, [0, 0])[0]
    return {op: (n[op], new[op] / n[op]) for op in n}

print('=== ONDA A: gerador sem traps (fonte 0 = arestas finas) ===')
groups = {
    'none (sem feedback)': ['none_s1', 'none_s2', 'none_s3'],
    'rand (corpus=todos, pais uniformes, mutate 0.8)': ['rand_s1', 'rand_s2', 'rand_s3'],
    'pipe (guide 0, raridade, mutate 0.8)': ['pipe_s1', 'pipe_s2', 'pipe_s3'],
    'pipe mutate 0.5': ['pipe_mp05_s1'],
    'pipe mutate 0.2': ['pipe_mp02_s1'],
    'pipe score=positions': ['pipe_pos_s1'],
    'coarse (guide 3)': ['coarse_s1'],
    'coarse score=positions': ['coarse_pos_s1'],
    'bins (guide 0,6)': ['bins_s1'], 'csr (guide 1)': ['csr_s1'], 'vl (guide 2)': ['vl_s1'],
}
refs = ['none_s1', 'none_s2', 'none_s3']
for g, names in groups.items():
    try:
        ex = [exclusive(n, refs) for n in names]
        f3 = summarize(names, '3')
        print('{:<50} f0={:<22} f3={:<22} exclusivas vs none: {}  status: {}'.format(g, summarize(names), f3, ex, [status(n) for n in names][0]))
    except FileNotFoundError as e:
        print(g, 'faltando', e)
print()
for n in ('pipe_s1', 'rand_s1', 'pipe_mp02_s1', 'coarse_s1', 'pipe_pos_s1'):
    try:
        po = per_op(n)
        print('{:<14} novas f0/caso por origem: '.format(n) + ' '.join('{}={:.2f}({})'.format(op, v[1], v[0]) for op, v in sorted(po.items(), key=lambda kv: -kv[1][1])))
    except FileNotFoundError:
        pass

print()
print('=== ONDA B: gerador com traps ===')
groupsB = {
    'trap_none': ['trap_none_s1', 'trap_none_s2', 'trap_none_s3'],
    'trap_rand': ['trap_rand_s1'], 'trap_pipe': ['trap_pipe_s1'], 'trap_pipe_pos': ['trap_pipe_pos_s1'],
    'trap_coarse': ['trap_coarse_s1'], 'trap_csr': ['trap_csr_s1'], 'trap_bins': ['trap_bins_s1'], 'trap_vl': ['trap_vl_s1'],
}
refsB = ['trap_none_s1', 'trap_none_s2', 'trap_none_s3']
for g, names in groupsB.items():
    try:
        row = []
        for src in ('0', '1', '3', '6'):
            row.append('f{}={}'.format(src, summarize(names, src)))
        ex0 = [exclusive(n, refsB, '0') for n in names]
        ex1 = [exclusive(n, refsB, '1') for n in names]
        print('{:<16} {}  excl f0: {} excl f1: {}  status: {}'.format(g, ' '.join(row), ex0, ex1, status(names[0])))
    except FileNotFoundError as e:
        print(g, 'faltando', e)
print()
# bins hit rates in trap_none_s1
try:
    sys.path.insert(0, '/sim/pedro.fam/RS5/riscof/fuzzer'); import feedback as fb
    _, c = load('trap_none_s1')
    hit = Counter()
    for x in c:
        for i in x['hits'].get('6', {}): hit[int(i)] += 1
    zero = [fb.BINS[i] for i in range(64) if hit[i] == 0]
    print('trap_none_s1 bins em zero ({}): {}'.format(len(zero), ', '.join(zero)))
    low = [(fb.BINS[i], hit[i]) for i in range(64) if 0 < hit[i] < 80]
    print('bins raros (<1%):', low)
    for n in ('trap_none_s1', 'trap_pipe_s1', 'trap_coarse_s1', 'trap_csr_s1'):
        po = per_op(n)
        print('{:<14} novas f0/caso por origem: '.format(n) + ' '.join('{}={:.2f}({})'.format(op, v[1], v[0]) for op, v in sorted(po.items(), key=lambda kv: -kv[1][1])))
except FileNotFoundError:
    pass
