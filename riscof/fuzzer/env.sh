# Ferramentas usadas pelo fuzzer nesta máquina:  source riscof/fuzzer/env.sh
# (funciona em shell interativo e em scripts bash/zsh)
if ! command -v module >/dev/null 2>&1; then
    [ -f /usr/share/Modules/init/bash ] && source /usr/share/Modules/init/bash
fi
module load verilator/5.052 riscv64-elf/14.2.0 sail-riscv/0.7 >/dev/null 2>&1
source /opt/rh/gcc-toolset-15/enable      # verilator 5.052 precisa de g++ 15
export TRIPLET=riscv64-elf
