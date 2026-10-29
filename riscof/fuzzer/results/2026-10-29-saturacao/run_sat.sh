#!/bin/bash
# Saturação: rodadas longas sem guia (3 sementes, com e sem traps) + guiada e corpus aleatório com traps
set -u
cd /sim/pedro.fam/RS5/riscof
source fuzzer/env.sh
EXP=<scratch>/sat
MODEL=<exp1>/model_nocov/obj_dir
mkdir -p $EXP
N=200000
run() { local name=$1; shift
  mkdir -p $EXP/$name && ln -sfn $MODEL $EXP/$name/obj_dir
  python3 fuzzer/fuzz.py --work-dir $EXP/$name -n $N -j 7 --no-verilator-cov --no-log-hits --no-suite --show-new 0 "$@" > $EXP/$name.log 2>&1
  echo "done $name $(date)" >> $EXP/progress.txt
}
echo "start $(date)" > $EXP/progress.txt
run sat_none_s1      --seed 1 --guide "" &
run sat_none_s2      --seed 2 --guide "" &
run sat_none_s3      --seed 3 --guide "" &
run sat_trap_none_s1 --seed 1 --traps --guide "" &
run sat_trap_none_s2 --seed 2 --traps --guide "" &
run sat_trap_none_s3 --seed 3 --traps --guide "" &
run sat_trap_pipe_s1 --seed 1 --traps --guide 0 &
run sat_trap_rand_s1 --seed 1 --traps --guide "" --corpus all --schedule uniform &
wait
echo "all done $(date)" >> $EXP/progress.txt
