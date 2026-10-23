#!/bin/bash
# Experimentos 2: mutador x sinal (onda A, gerador sem traps) e gerador com traps (onda B)
set -u
cd /sim/pedro.fam/RS5/riscof
source fuzzer/env.sh
EXP=<scratch>/exp2
MODEL=<exp1>/model/obj_dir
mkdir -p $EXP
N=8000
run() { local name=$1; shift
  mkdir -p $EXP/$name && ln -sfn $MODEL $EXP/$name/obj_dir
  python3 fuzzer/fuzz.py --work-dir $EXP/$name -n $N -j 10 "$@" > $EXP/$name.log 2>&1
  echo "done $name $(date)" >> $EXP/progress.txt
}
echo "start A $(date)" > $EXP/progress.txt
run rand_s1       --seed 1 --guide "" --corpus all --schedule uniform &
run rand_s2       --seed 2 --guide "" --corpus all --schedule uniform &
run rand_s3       --seed 3 --guide "" --corpus all --schedule uniform &
run pipe_s2       --seed 2 --guide 0 &
run pipe_s3       --seed 3 --guide 0 &
run pipe_mp02_s1  --seed 1 --guide 0 --mutate-p 0.2 &
run pipe_mp05_s1  --seed 1 --guide 0 --mutate-p 0.5 &
run pipe_pos_s1   --seed 1 --guide 0 --score positions &
run coarse_s1     --seed 1 --guide 3 &
run coarse_pos_s1 --seed 1 --guide 3 --score positions &
wait
echo "start B $(date)" >> $EXP/progress.txt
run trap_none_s1  --seed 1 --traps --guide "" &
run trap_none_s2  --seed 2 --traps --guide "" &
run trap_none_s3  --seed 3 --traps --guide "" &
run trap_rand_s1  --seed 1 --traps --guide "" --corpus all --schedule uniform &
run trap_pipe_s1  --seed 1 --traps --guide 0 &
run trap_pipe_pos_s1 --seed 1 --traps --guide 0 --score positions &
run trap_coarse_s1 --seed 1 --traps --guide 3 &
run trap_csr_s1   --seed 1 --traps --guide 1 &
run trap_bins_s1  --seed 1 --traps --guide 0,6 --weights 6=3 &
run trap_vl_s1    --seed 1 --traps --guide 2 &
wait
echo "all done $(date)" >> $EXP/progress.txt
