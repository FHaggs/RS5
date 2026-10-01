"""Parsing of Verilator coverage.dat and the global coverage map kept by the fuzzer."""
import hashlib
import os

# AFL-style hit-count buckets: a point reaching a new bucket is also considered novel
_BUCKETS = (1, 2, 3, 4, 8, 16, 32, 128)


def bucket(count):
    b = 0
    for i, limit in enumerate(_BUCKETS):
        if count >= limit:
            b = i
    return b


def _point_id(key):
    return int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8).digest(), 'little')


def _describe(key):
    """'\\x01f\\x02file\\x01l\\x02101...' -> 'v_line  riscof_tb.dut.fetch1  fetch.sv:101 if'"""
    fields = dict(item.split('\x02', 1) for item in key.split('\x01') if '\x02' in item)
    kind = fields.get('page', '?').split('/', 1)[0]
    where = os.path.basename(fields.get('f', '?')) + ':' + fields.get('l', '?')
    return '{:<9} {:<40} {} {}'.format(kind, fields.get('h', '?'), where, fields.get('o', ''))


# Only points inside the core count; testbench, RAM, etc. are ignored
DUT_SCOPE = '\x01h\x02riscof_tb.dut'


def parse_coverage_dat(path, with_names=False, scope=DUT_SCOPE):
    """Return ({point_id: count} for hit points, {point_id: description} or None).

    Point ids are stable hashes of the Verilator key, so results from different
    processes can be merged without shipping the long keys around.
    """
    hits = {}
    names = {} if with_names else None
    with open(path, encoding='latin-1') as f:
        for line in f:
            if not line.startswith("C '"):
                continue
            end = line.rindex("'")
            key = line[3:end]
            if scope not in key:
                continue
            count = int(line[end + 1:])
            pid = _point_id(key)
            if count:
                hits[pid] = count
            if with_names:
                names[pid] = _describe(key)
    return hits, names


class CoverageMap:
    def __init__(self):
        self.names = {}         # point id -> description (all instrumented points)
        self.max_bucket = {}    # point id -> highest bucket seen

    @property
    def total(self):
        return len(self.names)

    @property
    def covered(self):
        return len(self.max_bucket)

    def learn_names(self, names):
        if names:
            self.names.update(names)

    def merge(self, hits):
        """Merge one run; return (new points, points that reached a new count bucket)."""
        new_points, new_buckets = [], []
        for pid, count in hits.items():
            b = bucket(count)
            prev = self.max_bucket.get(pid)
            if prev is None:
                new_points.append(pid)
                self.max_bucket[pid] = b
            elif b > prev:
                new_buckets.append(pid)
                self.max_bucket[pid] = b
        return new_points, new_buckets

    def describe(self, pid):
        return self.names.get(pid, '<unknown point {:016x}>'.format(pid))

    def uncovered(self):
        return sorted(self.names[pid] for pid in self.names if pid not in self.max_bucket)
