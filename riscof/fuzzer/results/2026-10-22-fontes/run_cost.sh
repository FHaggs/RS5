#!/bin/bash
set -u
cd /sim/pedro.fam/RS5/riscof
source fuzzer/env.sh
EXP=/tmp/claude-1695000639/-sim-pedro-fam-RS5/49b5b087-b9ae-4024-9c45-5f3148de84c7/scratchpad/exp
rm -rf $EXP/cost_* $EXP/report_cost
python3 fuzzer/fuzz.py --work-dir $EXP/model -n 2 -j 2 --seed 0 > $EXP/model2.log 2>&1
python3 fuzzer/fuzz.py --work-dir $EXP/model_nocov --no-verilator-cov -n 2 -j 2 --seed 0 > $EXP/model_nocov2.log 2>&1
run() { local name=$1 model=$2; shift 2
  mkdir -p $EXP/$name && ln -sfn $EXP/$model/obj_dir $EXP/$name/obj_dir
  python3 fuzzer/fuzz.py --work-dir $EXP/$name "$@" > $EXP/$name.log 2>&1
  echo "done $name $(date)" >> $EXP/progress_cost.txt; }
echo "start $(date)" > $EXP/progress_cost.txt
run cost_cov_none   model       -n 600 -j 8 --seed 9 --guide "" --record none
run cost_cov_rtl    model       -n 600 -j 8 --seed 9 --guide "" --record 0,1,6,7
run cost_cov_vl     model       -n 600 -j 8 --seed 9 --guide "" --record 2
run cost_cov_all    model       -n 600 -j 8 --seed 9 --guide "" --record all
run cost_nocov_none model_nocov -n 600 -j 8 --seed 9 --guide "" --record none --no-verilator-cov
run cost_nocov_rtl  model_nocov -n 600 -j 8 --seed 9 --guide "" --record 0,1,6,7 --no-verilator-cov
run cost_nocov_keep model_nocov -n 4 -j 2 --seed 9 --guide "" --record 0,1,6,7 --no-verilator-cov --keep
python3 fuzzer/feedback_report.py $EXP/cost_cov_none $EXP/cost_cov_rtl $EXP/cost_cov_vl $EXP/cost_cov_all $EXP/cost_nocov_none $EXP/cost_nocov_rtl -o $EXP/report_cost --window 100 >> $EXP/progress_cost.txt 2>&1
echo "all done $(date)" >> $EXP/progress_cost.txt
