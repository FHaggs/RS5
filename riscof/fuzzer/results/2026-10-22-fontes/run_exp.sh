#!/bin/bash
# Experimentos iniciais das fontes (X1 saturação, X2 custo, X3 estabilidade, X4 bins, X5 colisões)
set -u
cd /sim/pedro.fam/RS5/riscof
source fuzzer/env.sh
EXP=<scratch>/exp
mkdir -p $EXP
N=8000
# modelos (um com cobertura do Verilator, outro sem), compartilhados por symlink
python3 fuzzer/fuzz.py --work-dir $EXP/model -n 2 -j 2 --seed 0 > $EXP/model.log 2>&1
python3 fuzzer/fuzz.py --work-dir $EXP/model_nocov --no-verilator-cov -n 2 -j 2 --seed 0 > $EXP/model_nocov.log 2>&1
run() { # nome, modelo, args...
  local name=$1 model=$2; shift 2
  mkdir -p $EXP/$name && ln -sfn $EXP/$model/obj_dir $EXP/$name/obj_dir
  python3 fuzzer/fuzz.py --work-dir $EXP/$name "$@" > $EXP/$name.log 2>&1
  echo "done $name $(date)" >> $EXP/progress.txt
}
echo "start $(date)" > $EXP/progress.txt
run none_s1 model -n $N -j 12 --seed 1 --guide "" --calibrate 50 &
run none_s2 model -n $N -j 12 --seed 2 --guide "" &
run none_s3 model -n $N -j 12 --seed 3 --guide "" &
run pipe_s1 model -n $N -j 12 --seed 1 --guide 0 &
run bins_s1 model -n $N -j 12 --seed 1 --guide 0,6 --weights 6=3 &
run csr_s1  model -n $N -j 12 --seed 1 --guide 1 &
run vl_s1   model -n $N -j 12 --seed 1 --guide 2 &
wait
# custo por fonte (mesmos programas: seed 9, sem guia), um de cada vez
run cost_cov_none  model       -n 600 -j 8 --seed 9 --guide "" --record none
run cost_cov_rtl   model       -n 600 -j 8 --seed 9 --guide "" --record 0,1,6,7
run cost_cov_vl    model       -n 600 -j 8 --seed 9 --guide "" --record 2
run cost_cov_all   model       -n 600 -j 8 --seed 9 --guide "" --record all
run cost_nocov_none model_nocov -n 600 -j 8 --seed 9 --guide "" --record none --no-verilator-cov
run cost_nocov_rtl  model_nocov -n 600 -j 8 --seed 9 --guide "" --record 0,1,6,7 --no-verilator-cov
echo "all done $(date)" >> $EXP/progress.txt
