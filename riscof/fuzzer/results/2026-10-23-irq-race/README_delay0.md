# Reprodução: interrupção aceita no ciclo em que uma instrução de CSR termina

Core: `BRANCHPRED=1 FORWARDING=1 DUALPORT_MEM=1 IQUEUE_SIZE=2 DELAY_CYCLES=0`. Gerado por `riscof/fuzzer/repro_irq_race.py`. Cada subdiretório tem o `test.S` (teste RISCOF completo), `dut.sig` (RS5), `ref.sig` (Sail) e, com `--trace`, os traces de instrução.

| variante | status | traps RS5 / Sail | mstatus&0x88 RS5 / Sail | palavras diferentes |
|---|---|---|---|---|
| 0_nada | mismatch | 1 / 1 | 0x00 / 0x80 | 1 |
| 1_addi | mismatch | 1 / 1 | 0x88 / 0x80 | 1 |
| 1_lw | mismatch | 1 / 1 | 0x88 / 0x80 | 1 |
| 1_sw | mismatch | 1 / 1 | 0x88 / 0x80 | 1 |
| 1_csrr | mismatch | 1 / 1 | 0x88 / 0x80 | 1 |
| 2_addi | pass | 1 / 1 | 0x80 / 0x80 | 0 |
| 3_addi | pass | 1 / 1 | 0x80 / 0x80 | 0 |
| 1_div | pass | 1 / 1 | 0x80 / 0x80 | 0 |
