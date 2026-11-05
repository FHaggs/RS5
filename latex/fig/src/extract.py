import gzip, json, re, sys, os
D = '/tmp/claude-1695000639/-sim-pedro-fam-RS5/6152cf82-880b-40ed-9bee-6df2d7afc914/scratchpad/sat'
runs = ['sat_none_s1', 'sat_none_s2', 'sat_none_s3', 'sat_trap_none_s1', 'sat_trap_none_s2',
        'sat_trap_none_s3', 'sat_trap_pipe_s1', 'sat_trap_rand_s1']
pat = re.compile(r'"total_positions": \{([^}]*)\}')
num = re.compile(r'"(\d+)": (\d+)')
out = {}
STEP = 500
for r in runs:
    series = {s: [] for s in ('0', '1', '3', '6')}
    marks = {}
    n = 0
    with gzip.open(os.path.join(D, r, 'cases.jsonl.gz'), 'rt') as f:
        for line in f:
            n += 1
            if n % STEP and n not in (1000, 8000, 50000, 100000, 200000):
                continue
            m = pat.search(line)
            tp = dict(num.findall(m.group(1)))
            if n % STEP == 0:
                for s in series:
                    series[s].append(int(tp.get(s, 0)))
            if n in (1000, 8000, 50000, 100000, 200000):
                marks[n] = {s: int(tp.get(s, 0)) for s in series}
    out[r] = {'n': n, 'step': STEP, 'series': series, 'marks': marks}
    print(r, n, marks.get(8000), marks.get(200000), file=sys.stderr)
json.dump(out, open(sys.argv[1], 'w'))
