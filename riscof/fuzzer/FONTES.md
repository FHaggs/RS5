# Fontes de feedback do fuzzer do RS5

Este documento descreve **o que cada fonte observa, como vira posição no mapa e onde está
implementada**. O código é a referência final: `riscof/fuzz_cov.sv` (captura no RTL, via `bind`)
e `riscof/fuzzer/feedback.py` (modelos em Python, mapa e regra de novidade). As tabelas dos dois
lados são espelhadas; mudar uma exige mudar a outra.

Configuração assumida: `RV32IMU_Zicsr_Zihpm`, `BRANCHPRED=1`, `FORWARDING=1`, `IQUEUE_SIZE=2`,
`DUALPORT_MEM=1`, **`DELAY_CYCLES=2`** (padrão do `fuzz.py`; a memória de dados responde com 2
ciclos de `stall`, o que expõe os caminhos de stall do pipeline).

---

## 1. Mapa, buckets e novidade (comum a todas as fontes)

O mapa é segmentado: **uma região por fonte**. Cada região é um conjunto de posições (índices) e
cada posição tem um contador de 8 bits preenchido durante o caso. No fim do caso o Python aplica a
regra do AFL por região:

| Contagem | 1 | 2 | 3 | 4–7 | 8–15 | 16–31 | 32–127 | 128+ |
|---|---|---|---|---|---|---|---|---|
| Bucket | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |

- **Posição nova**: índice nunca visto na região (vale 2 pontos).
- **Bucket novo**: índice já visto, mas com um bucket de contagem inédito (vale 1 ponto).
- `virgin[idx]` guarda o OR dos bits de bucket já vistos (virgin map do AFL).
- `freq[idx]` conta em quantos casos a posição apareceu: é a base da **energia por raridade**
  (`Σ peso/freq` sobre as posições do caso), usada para sortear pais no gerador.

Só as fontes de `--guide` decidem o `score` (entrada no corpus) e a energia; todas as de
`--record` são gravadas e salvas em `cases.jsonl.gz`.

Momentos: **registrar** (cada fonte no seu ritmo, durante a simulação), **capturar** (arrays no
RTL, despejados em `fuzz_cov_<src>.txt` no `final`), **consumir** (fim do caso, no Python).

Plusargs do modelo: `+FUZZ_COV=<prefixo>` liga a gravação; `+FUZZ_SRC=<máscara>` escolhe as fontes
(bit i = fonte i). O `runner.py` passa os dois a partir de `--record`.

Formato de `fuzz_cov_<src>.txt`: `"<índice> <contagem> <raw em hex> <colidiu>"`, onde `raw` é a
chave original (antes do hash) da primeira vez que o índice foi atingido e `colidiu` vale 1 se, no
mesmo caso, o índice recebeu uma chave diferente da primeira (colisão de hash observada).

---

## 2. Fonte 0: arestas do pipeline com duração

**Pergunta que responde:** "o pipeline passou do estado A para o estado B depois de ficar N ciclos
em A?"

**Onde:** `fuzz_pipe_probe` em `fuzz_cov.sv`, `bind RS5`. Python: `SNAPSHOT_FIELDS`,
`describe_edge`.

### 2.1 Snapshot (40 bits, um por ciclo)

Não é um hash de todos os sinais. Primeiro, sinais escolhidos são **comprimidos em campos com
significado**; nada de dados (PC, valores, números de registrador). Depois, a palavra inteira é
misturada por `mix64`.

| Bits | Campo | Valores | Sinal de origem |
|---|---|---|---|
| 39:38 | `fq` | 0 vazia, 1 com algo, 2 quase cheia, 3 cheia | `fetch1.buffer_tx`, `almost_full`, `!buffer_not_full` |
| 37 | `dec_valid` | fetch entregou instrução válida | `decode_ctrl.valid` |
| 36 | `dec_jumping` | fetch no meio de um redirecionamento | `decode_ctrl.jumping` |
| 35 | `bp_take` | previsor mandou o fetch saltar | `bp_take_fetch` |
| 34 | `hz_rs1` | bolha: rs1 trancado por load | `decoder1.hazard_rs1` |
| 33 | `hz_rs2` | bolha: rs2 trancado por load | `decoder1.hazard_rs2` |
| 32 | `hz_mem` | bolha: load logo após store | `decoder1.hazard_mem` |
| 31:30 | `fwd1` | origem de rs1: 0 banco, 1 execute, 2 memória, 3 retire | recalculado (ver 2.2) |
| 29:28 | `fwd2` | origem de rs2 | recalculado |
| 27:24 | `xclass` | classe da instrução em execute (tabela 2.3) | `ctrl_execute.is_*` |
| 23 | `x_hazard` | execute tem bolha de hazard | `ctrl_execute.hazard` |
| 22 | `x_killed` | execute tem instrução descartada | `ctrl_execute.killed` |
| 21 | `x_bp_taken` | instrução foi prevista como tomada | `ctrl_execute.bp_taken` |
| 20 | `x_illegal` | decode marcou ilegal | `ctrl_execute.exc_ilegal_inst` |
| 19:17 | `mul` | 0 idle, 1 ALBL, 2 ALBH, 3 AHBL, 4 AHBH | `execute1.gen_zmmul_on.mul1.mul_state` |
| 16:15 | `div` | 0 idle, 1 init, 2 calc, 3 sign | `execute1.gen_div_on.div1.state` |
| 14 | `jump` | salto confirmado não previsto | `jump` |
| 13 | `rollback` | previsão desfeita | `jump_rollback` |
| 12 | `exc` | exceção neste ciclo | `RAISE_EXCEPTION` |
| 11 | `mret` | mret neste ciclo | `MACHINE_RETURN` |
| 10 | `irq_ack` | interrupção aceita | `interrupt_ack_o` |
| 9:8 | `mem` | em mem_access: 0 nada, 1 resultado, 2 load, 3 lr/sc | `ctrl_mem_access` |
| 7:6 | `ret` | em retire, idem | `ctrl_retire` |
| 5 | `stall` | memória de dados pediu espera | `stall` |
| 4 | `irq_pend` | interrupção pendente | `interrupt_pending` |
| 3:2 | `priv` | 0 U, 3 M | `privilege` |
| 1 | `mie` | `mstatus.MIE` | `CSRBank1.mstatus_mie` |
| 0 | reservado | | |

O que fica **de fora** e por quê:
- `hold`: é o OU de `mul`/`div` ocupados (já no snapshot).
- os ~90 bits de `exec_ctrl_t`: `is_xor` e `is_or` se comportam igual; só a classe entra.
- o contador do divisor (32 passos): entraria como 32 estados sem informação nova; a **duração**
  da aresta já diz quanto tempo o hold durou.
- qualquer dado: faria `add x5` e `add x6` serem estados diferentes.

### 2.2 Forwarding recalculado

O decode não guarda num fio de onde veio o operando. A sonda recalcula com a mesma prioridade de
`decode.sv` (`gen_forwarding_on`), usando os fios do `RS5.sv`:

```
se !use_rsN || rsN == x0 || !decode_ctrl.valid     -> 0 (banco / não usa)
se rsN == rd_execute    && write_enable_exec       -> 1 (execute)
se rsN == rd_mem_access && ctrl_mem_access.rd_we   -> 2 (memória)
se rsN == rd_retire     && ctrl_retire.rd_we       -> 3 (retire)
senão                                              -> 0 (banco)
```

### 2.3 Classe em execute (`xclass`)

| Valor | Classe | Condição em `exec_ctrl_t` |
|---|---|---|
| 0 | bubble | `is_nop` (bolha de hazard, descarte, ou `FENCE`) |
| 1 | alu | add/sub/xor/or/and/sll/srl/sra/slt/sltu/lui/auipc |
| 2 | load | `is_load` |
| 3 | store | `is_store` |
| 4 | branch | beq/bne/blt/bltu/bge/bgeu |
| 5 | jal | `is_jal_jalr && !is_jalr` |
| 6 | jalr | `is_jalr` |
| 7 | mul | `is_mul` |
| 8 | div | `is_div || is_rem` |
| 9 | csr | `is_csr` |
| 10 | sys | ecall/ebreak/mret/wfi/sret |
| 11 | amo | só com A |
| 12 | other | aes, zbkb, zicond, vetor |

### 2.4 Aresta e chave

Uma aresta é registrada **só quando o snapshot muda**, e leva a duração do estado anterior:

```
run       = ciclos que o pipeline ficou no snapshot anterior
chave     = (mix64(prev) >> 1) XOR mix64(cur) XOR (bucket(run) << 60)
índice    = mix64(chave)[15:0]            (65 536 posições)
raw       = {prev[39:0], cur[39:0], run[15:0]}   (96 bits, para o relatório)
```

Por que duração: sem ela, `hold -> hold` por 32 ciclos vira 32 incrementos da mesma posição e
domina os buckets; com ela, "hold longo seguido de trap" e "hold curto seguido de trap" são arestas
diferentes e um `div` inteiro vira ~4 arestas.

Por que `mix64` (finalizador do splitmix64) antes do XOR: snapshots consecutivos diferem em 1 ou 2
bits; o XOR cru daria chaves pequenas e amontoadas.

**Exemplo** (`lw x5` seguido de `add x6, x5, x2`, depois um `div`):

```
ciclo  snapshot (resumido)                       aresta registrada
 10    x=load fq=2
 11    x=bubble hz, dec_hz=rs1                   [load] --1c--> [bubble hz]
 12    x=alu fwd=ret/rf                          [bubble hz] --1c--> [alu fwd=ret/rf]
 13    x=div div=init                            [alu fwd=ret/rf] --1c--> [div init]
 14    x=div div=calc                            [div init] --1c--> [div calc]
 15–44 (sem mudança, run = 31)
 45    x=div div=sign                            [div calc] --31c(16-31)--> [div sign]
 46    x=alu                                     [div sign] --1c--> [alu]
```

### 2.5 Limites

- **Colisões**: 65 536 posições com hash. A coluna `colidiu` do dump mede colisões dentro de um
  caso; o relatório agrega.
- **Saturação**: o espaço de arestas alcançáveis é finito. A curva "posições novas por caso" do
  relatório diz quando a novidade para de guiar (daí a energia por raridade e os bins).
- Qualquer mudança nos campos muda **todos** os índices; mapas de execuções diferentes só se
  comparam com o mesmo `fuzz_cov.sv`.

---

## 3. Fonte 1: transições de trap/CSR com contexto

**Pergunta:** "um evento de trap/CSR levou o estado de controle dos CSRs de A para B, com o
pipeline nesta situação?" É a métrica do ProcessorFuzz, mas calculada no RTL, por evento, com
contexto de pipeline (o Encarsia mostrou que a versão original não ajuda; esta fonte é a **linha de
base** para comparar).

**Onde:** `fuzz_csr_probe` em `fuzz_cov.sv`, `bind RS5`. Python: `CSR_STATE_FIELDS`,
`describe_csr_transition`.

### 3.1 Estado de controle (14 bits)

| Bits | Campo | Sinal |
|---|---|---|
| 13:12 | `priv` | `privilege` |
| 11 | `mie` | `mstatus.MIE` |
| 10 | `mpie` | `mstatus.MPIE` |
| 9:8 | `mpp` | `mstatus.MPP` |
| 7:5 | `msie`, `mtie`, `meie` | `mie` |
| 4:2 | `msip`, `mtip`, `meip` | `mip` |
| 1:0 | `inh_cy`, `inh_ir` | `mcountinhibit[0]`, `[2]` |

Os valores dos contadores (HPM) **não** entram: são o objeto das fontes 4/5 de `pesquisa.md`,
fora do escopo aqui.

### 3.2 Evento (3 + 5 bits) e contexto (8 bits)

| `kind` | Evento | `code` |
|---|---|---|
| 1 | exceção (`RAISE_EXCEPTION`) | `Exception_Code` |
| 2 | interrupção aceita | bits `{meip, mtip, msip}` |
| 3 | `mret` | – |
| 4 | escrita de CSR (`csr_write_enable`) | grupo do CSR |
| 5 | leitura de CSR (`csr_read_enable`) | grupo do CSR |

Prioridade: exceção > irq > mret > escrita > leitura (um evento por ciclo). Grupos de CSR:
mstatus, misa, mie, mtvec, mcounteren, mcountinhibit, mscratch, mepc, mcause, mtval, mip,
mcycle/minstret, mhpmcounterN, contadores RO de usuário (0xC00+), mhpmeventN, outro.

Contexto: `{xclass[3:0], hold, stall, irq_pending, dec_valid}` no ciclo do evento.

### 3.3 Chave

```
raw   = {antes[13:0], depois[13:0], kind[2:0], code[4:0], ctx[7:0]}   (44 bits)
índice = mix64(raw)[11:0]                                              (4 096 posições)
```
`antes` é o estado no ciclo do evento; `depois`, no ciclo seguinte. Como o evento é explícito,
não há `>> 1`. Eventos durante `stall` são ignorados (o execute os mascara).

---

## 4. Fonte 2: cobertura de linha do Verilator

**Pergunta:** "esta linha/branch/cover do RTL foi avaliada?" Métrica estrutural, linha de base
(`coverage.py`, inalterada). Índice = hash do nome do ponto; escopo `riscof_tb.dut`.

Conhecido do `report.md`: ~26% dos pontos são artefatos de avaliação combinacional (mux de leitura
de CSR, decodificação de funct7/funct3) e ~12% são inalcançáveis nesta configuração. O filtro por
exclusões ainda não está aplicado; o relatório mostra os números brutos. Só existe quando o modelo
é compilado com `--coverage` (`--no-verilator-cov` desliga e acelera).

---

## 5. Fonte 6: bins nomeados

**Pergunta:** "esta situação específica, escolhida por quem conhece o design, ocorreu?" Índice
fixo, 64 posições, contagem por ciclo qualificado (`act = armed && !stall`, exceto os bins de
stall). A lista está em `BINS` (`feedback.py`) e em `bin_hit[i]` (`fuzz_cov.sv`):

| # | Bin | Condição |
|---|---|---|
| 0 | `fq_full` | fila do fetch cheia |
| 1 | `fq_full_jump` | fila cheia no ciclo de `jump`/`rollback` |
| 2 | `rollback_no_valid_fetch` | rollback sem `valid_fetch` |
| 3 | `fetch_starved` | fila vazia, decode sem instrução, sem hold |
| 4–5 | `lock_rs1_unused`, `lock_rs2_unused` | registrador trancado mas não usado (exercita `&& use_rs*`) |
| 6 | `hazard_mem` | load logo após store |
| 7–14 | `hazard_x_<classe>` | hazard de rs1/rs2 cruzado com a classe da instrução no decode |
| 15–16 | `fwd_exec_in_hold`, `fwd_memret_in_hold` | forwarding enquanto o pipeline está em hold |
| 17 | `fwd_both_retire` | os dois operandos vindos do retire |
| 18 | `fwd_mixed_sources` | operandos de origens diferentes |
| 19 | `hold_after_hold` | hold que começa no ciclo seguinte ao fim de outro |
| 20–21 | `div_by_zero`, `div_overflow` | casos de borda da divisão |
| 22 | `hazard_bubble_in_hold` | bolha de hazard congelada por hold |
| 23 | `killed_in_hold` | não deve ocorrer (sanidade) |
| 24–35 | `irq_x_<classe>` | interrupção aceita sobre cada classe em execute |
| 36 | `irq_blocked_by_hold` | IRQ pendente e habilitada, barrada por hold |
| 37 | `irq_pending_in_stall` | IRQ pendente durante stall |
| 38–42 | `stall_*` | stall junto com hold, jump, hazard, forwarding, irq_ack (o último não deve ocorrer) |
| 43–53 | traps | exceção em U/M, `mret`, `ecall` U/M, ilegal (CSR, mret, outro), desalinhado, `ebreak`, exceção logo após hold |
| 54 | `csrw_mstatus_irq_pending` | escrita em mstatus com IRQ pendente (o `@todo` do CSRBank) |
| 55–59 | previsão | rollback, previsão confirmada, branch tomado sem previsão, jalr, JAL redirecionado pelo execute (previsão não reconhecida pelo fetch) |
| 60–63 | CSR | leitura de contador, escrita em mscratch/mie/mstatus |

Validação de um bin (ver `plano_feedback.md`, X4): acerto > 90% dos casos = inútil; 1–90% = bom
feedback; 0% e alcançável = alvo do modo dirigido; 0% e inalcançável = lista de exclusões.

---

## 6. Fonte 7: escalares

Não é cobertura: para cada escalar o mapa guarda o **máximo** visto no caso (`fuzz_cov_scalar`).
Serve ao modo dirigido (ainda não implementado) e ao relatório.

| # | Escalar | O que é |
|---|---|---|
| 0 | `fq_level_max` | maior nível da fila do fetch |
| 1 | `hold_run_max` | maior sequência de ciclos em hold |
| 2 | `hazard_run_max` | maior sequência de bolhas de hazard |
| 3 | `stall_run_max` | maior sequência de ciclos em stall |
| 4 | `killed_run_max` | maior sequência de ciclos com instrução descartada |
| 5 | `irq_wait_max` | maior espera entre IRQ pendente+habilitada e o ack |
| 6 | `fq_full_run_max` | maior sequência com a fila cheia |
| 7 | `snapshot_run_max` | maior tempo parado num mesmo snapshot |

---

## 7. Como ligar e desligar

```sh
source riscof/fuzzer/env.sh
cd riscof
python3 fuzzer/fuzz.py --work-dir $PWD/w_none  -n 2000 --seed 1 --guide ""          # controle
python3 fuzzer/fuzz.py --work-dir $PWD/w_pipe  -n 2000 --seed 1 --guide 0           # arestas
python3 fuzzer/fuzz.py --work-dir $PWD/w_bins  -n 2000 --seed 1 --guide 0,6 --weights 6=3
python3 fuzzer/fuzz.py --work-dir $PWD/w_csr   -n 2000 --seed 1 --guide 1           # estilo ProcessorFuzz
python3 fuzzer/fuzz.py --work-dir $PWD/w_vl    -n 2000 --seed 1 --guide 2           # linha do Verilator
python3 fuzzer/fuzz.py --work-dir $PWD/w_cost  -n 500  --record none                # custo sem sondas
python3 fuzzer/feedback_report.py w_none w_pipe ...                                  # relatório
```

- `--record`: o que é gravado (RTL via `+FUZZ_SRC`, Verilator via o build). `none` mede o custo.
- `--guide`: subconjunto de `--record` que decide corpus e energia. `""` = sem feedback.
- `--schedule novelty|rarity`: energia 1 para quem trouxe novidade, ou `Σ peso/freq`.
- `--calibrate K`: a cada K casos, roda o RS5 duas vezes e registra posições instáveis.
- `--delay-cycles`, `--iqueue-size`, `--branchpred`, `--forwarding`: configuração do core.

Custo: as sondas são compiladas sempre, mas só executam com `+FUZZ_COV`; `+FUZZ_SRC` liga cada
fonte. A fonte 2 exige o build instrumentado (`obj_dir` separado quando se alterna).
