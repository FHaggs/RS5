#!/bin/bash
# Uso: time_riscof.sh <before|after> <rótulo> <ccache dir> [cores]
# Roda a suíte baseline do RISCOF na árvore indicada e grava a saída com o tempo decorrido por linha.
set -u
W=$1; LABEL=$2; CC=$3; CORES=${4:-0-55}
S=$(cd "$(dirname "$0")" && pwd)
source /sim/pedro.fam/RS5/riscof/fuzzer/env.sh
source ~/.local/riscof/bin/activate
cd $S/wt/$W/riscof
export TRIPLET=riscv64-elf BRANCHPRED=1 FORWARDING=1 DUALPORT_MEM=1 IQUEUE_SIZE=2 DELAY_CYCLES=0
export CCACHE_DIR=$CC
mkdir -p $CC $S/runs
OUT=$S/runs/$LABEL
rm -rf $OUT; mkdir -p $OUT
export START=$(date +%s.%N)
/usr/bin/time -f '%e %U %S' -o $OUT/time.txt taskset -c $CORES riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env --work-dir=$OUT/work --no-browser --config=baseline.ini 2>&1 | python3 -u -c '
import os, sys, time
t0 = float(os.environ["START"])
for line in sys.stdin:
    sys.stdout.write("%8.1f  %s" % (time.time() - t0, line))
' > $OUT/log.txt
END=$(date +%s.%N)
echo "$LABEL cores=$CORES wall/user/sys: $(cat $OUT/time.txt)" >> $S/runs/summary.txt
echo "$LABEL total $(python3 -c "print('%.1f' % ($END - $START))") s  load: $(cut -d' ' -f1-3 /proc/loadavg)" | tee -a $S/runs/summary.txt
