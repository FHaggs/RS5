# Pesquisa: fuzzer para o RS5

Notas de pesquisa do TCC. Resume as ideias discutidas para evoluir o fuzzer em `riscof/fuzzer/`.
Nada aqui está implementado ainda, exceto o que aparece em "Estado atual".

## 1. Ponto de partida

### Estado atual do fuzzer

- Fuzzer diferencial: cada programa roda no RS5 (Verilator com cobertura) e no Sail, e as assinaturas são comparadas.
- Feedback: cobertura de linha e `cover property` do Verilator, com buckets de hit count no estilo AFL (`coverage.py`).
- **O gerador é um placeholder**: 4 corpos fixos com valores iniciais aleatórios, e `feedback()` não faz nada.
- **O oráculo é fraco**: compara só o estado final (x1..x30 + `fuzz_mem`). Bugs com efeito sobrescrito passam. Não há trap handler, então qualquer exceção vira `hang` ou `dut_timeout`.

### Trabalhos relacionados e o que tiramos deles

| Trabalho | Ideia | O que aproveitamos | Crítica |
|---|---|---|---|
| **SimFuzz** (preprint) | corpus de bugs históricos, mutação por bloco sem tocar nas CTIs, filtro por similaridade sintática; *open-loop* | a importância de um corpus bom | ganhos pequenos sobre o PathFuzz; a hipótese de que similaridade sintática aproxima similaridade de estado nunca é validada; avalia com cobertura, embora critique cobertura; CTIs nunca mutadas |
| **Cascade** | programas longos montados a partir de blocos básicos | composição de blocos | é o mais próximo da nossa ideia de blocos; diferenciar explicitamente |
| **ProcessorFuzz** | feedback por transições de CSR, calculadas num simulador de ISA | transições de CSR como fonte de feedback | no RS5 o espaço de CSR é pequeno e satura; o valor está no cruzamento com o pipeline |
| **DifuzzRTL** | cobertura por registradores de controle do RTL | arestas de estado de controle | — |
| **MorFuzz**, SiliFuzz | morphing de instruções; fuzzing em silício sem cobertura | — | posicionar em relação a eles |

Corpus de bugs de outros cores (Rocket, BOOM, XiangShan) tem **pouco retorno** para o RS5: a maioria envolve RV64, FPU, Sv39 ou execução fora de ordem. O corpus mais valioso é o **histórico do próprio RS5** (fixes do Xkyber, `#56 bugfix`), que também serve para a avaliação.

## 2. Proposta em uma frase

> Gerar programas pela **composição de blocos semânticos** (map, reduce, crc, aes...) e por **transformações que preservam a semântica**, guiado por um **feedback unificado estilo AFL** que combina arestas do pipeline, transições de trap/CSR e contadores de desempenho (HPM), e avaliado por **bugs reencontrados e injetados**, não por cobertura.

Diferença em relação ao SimFuzz: em vez de manter o mutante sintaticamente perto da seed, geramos variantes **semanticamente equivalentes mas microarquiteturalmente diferentes**, com feedback em malha fechada e um oráculo que funciona mesmo onde não há modelo de referência.

## 3. Blocos semânticos

### 3.1 Contrato de um bloco

```yaml
name: vec_sum
tag: reduce                       # map | reduce | filter | scan | sort | crypto | atomic | csr | trap ...
isa: [rv32i]
variant: O2
inputs:
  a0: {type: ptr, size: "4*a1"}   # ponteiro para uma sub-região do fuzz_mem
  a1: {type: len, range: [1, 16]}
outputs:
  a0: {type: word}
clobbers: [t0, t1, t2]
mem: {reads: "a0[0:4*a1]", writes: none}
cti: {kind: loop, bound: a1}      # garante terminação
body: |
    mv    t0, zero
1:  lw    t1, 0(a0)
    add   t0, t0, t1
    addi  a0, a0, 4
    addi  a1, a1, -1
    bnez  a1, 1b
    mv    a0, t0
```

Regras:

- **Registradores virtuais** (nomes de ABI), renomeados na composição. `x31` continua reservado.
- **Memória só por ponteiro de entrada**, limitado por máscara (`andi a1, a1, 15`). O `fuzz_mem` precisa crescer dos 64 words atuais.
- **Só blocos folha**; labels locais numéricos ou com prefixo único.
- **Pilha opcional** (blocos `-O0`), com `sp` apontando para dentro da região de assinatura.
- **Nada não determinístico na assinatura**: `mcycle`, `time` e HPM nunca entram.

### 3.2 Fontes de blocos

| Fonte | Conteúdo |
|---|---|
| **kernels em C escritos à mão** (principal) | map, reduce, filter, scan, memcpy, strlen, crc32, popcount, matmul, insertion sort, busca binária, borboleta da NTT do Kyber |
| `riscv-tests/benchmarks` | vvadd, median, multiply, qsort, rsort, towers, spmv |
| macros de `riscv-tests/isa` | `TEST_RR_OP` (instrução + valor esperado); `*_BYPASS` como **templates de distância de hazard** |
| blocos de extensão | AES, SHA-256, Zbkb, Zicond, atômicos, Xkyber |
| blocos à mão | CSR, traps (ecall, ilegal, desalinhado), `fence`, `wfi` |

Atenção: código `-O2` é escalonado pelo compilador para **evitar** hazards. Programas reais sozinhos exercitam pouco o pipeline; por isso os operadores de composição e as transformações são essenciais.

### 3.3 Compilação com as extensões

ISA alvo (`riscof/rs5/extensions.yaml`): `RV32IMACUZicond_Zicntr_Zicsr_Zihpm_Zca_Zcb_Zbkb_Zkne_Zknh`.

Pipeline offline (uma vez, sem custo por caso): `kernel.c → gcc -S (por variante) → kernel.s → extrator → bloco.yaml`.

```sh
riscv64-elf-gcc -S -march=rv32imac_zicsr_zicond_zcb_zbkb_zkne_zknh -mabi=ilp32 \
    -O2 -ffreestanding -fno-pic -fno-builtin -fno-asynchronous-unwind-tables kernel.c
```

| Extensão | GCC gera sozinho? | Como forçar |
|---|---|---|
| M | sim | — |
| A | só com builtins | `__atomic_fetch_add` → `amoadd.w`; `__atomic_compare_exchange` → `lr.w`/`sc.w` |
| C / Zca / Zcb | sim | variantes com e sem C |
| Zbkb | em parte | padrões de rotação, `__builtin_bswap32` |
| Zicond | sim (GCC 14) | `r = c ? a : 0;` com `-O2` |
| Zkne / Zknh | não | intrínsecos (`riscv_crypto.h`, `__builtin_riscv_aes32esmi`) ou `asm volatile` |
| **Xkyber** | não (custom) | `.insn r CUSTOM_1, f3, f7, rd, rs1, rs2` |

- Zicond e Zcb exigem GCC 14 / binutils 2.41; Zbkb/Zkne/Zknh, GCC 12 / binutils 2.38. **Conferir a versão da toolchain.**
- Xkyber: opcode `[6:2]=01010` (CUSTOM_1, 0x2B), operação pelo funct7 (`decode.sv:240-245`): `0000000` ADD, `0000001` SUB, `0000010` MUL, `0000011` COMPRESS, `0000100` CBD (rs2=3 → CBD3). Falta conferir se o funct3 importa.
- **O Sail não conhece o Xkyber**: esses blocos ficam fora do diferencial e usam só o oráculo metamórfico. O modelo do fuzzer precisa de `XKYBEREnable=1`.

**Variantes que saem de graça** (já são transformações equivalentes): `-O0/-O2/-Os/-funroll-loops`, `-fschedule-insns` vs `-fno-schedule-insns` (muda distâncias de hazard), `-mtune` diferentes, com e sem C/Zbkb/Zicond.

## 4. Mix-and-match: o programa é uma árvore

O corpus guarda **árvores de composição**; o `.s` é só a renderização.

```
seq
├── init(regs, mem)
├── interleave
│   ├── vec_map[O2]   (a0=buf0, a1=8)
│   └── crc32[Os+zbkb](a0=buf1, a1=4)
├── call(vec_sum[O0]) (a0=buf0, a1=8)     # encadeado com a saída do map
├── branch(r_sum < r_crc, aes_round, sha_sig0)
└── loop(3, atomic_inc[A])
```

### Operadores

| Operador | O que faz | Estressa |
|---|---|---|
| `seq(A, B)` | B após A, podendo ligar saída de A na entrada de B (encadeamento **tipado**: ptr, len, word) | dependência entre blocos |
| `interleave(A, B)` | intercala instruções mantendo a ordem interna; exige registradores e memória disjuntos | **hazards e forwarding no meio dos blocos** |
| `loop(k, A)` | laço com contador | branch predictor |
| `call(A)` | `jal`/`jalr` | `jalr`, `ra`, predição |
| `branch(c, A, B)` | escolha por valor calculado | mispredict, `jump_rollback` |
| `trap(A, t)` | ecall, ilegal ou desalinhado no meio de A; handler retorna | `ctx_switch`, exceção, `mret` |

**Pressão de registradores** é um parâmetro de mutação: poucos registradores → muitas dependências; muitos → paralelismo.

### Mutações

1. Trocar bloco por outro de mesma tag e contrato.
2. Trocar a variante de compilação.
3. Inserir, remover ou duplicar subárvore.
4. Trocar operador (`seq` ↔ `interleave`, envolver em `loop`/`call`).
5. Religar entradas e saídas, mudar `len` e ponteiros.
6. Mudar a pressão de registradores.
7. Mutar o estado inicial (registradores, memória, CSRs, interrupções).

Bônus: **minimização por delta debugging na árvore** gera reprodutores pequenos para triagem.

## 5. Oráculos

| Oráculo | Cobre | Observação |
|---|---|---|
| **Diferencial com o Sail** (atual) | ISA padrão | melhorar: trap handler com `mcause`/`mepc` na assinatura; depois, comparação do **trace de commit** (PC, rd, valor) com o `--trace` do Sail |
| **Metamórfico**: P e T(P) devem ter a mesma assinatura | tudo, inclusive onde o Sail não ajuda | saídas dos blocos em slots fixos da assinatura |
| `assert property` no RTL | invariantes internas, bugs mascarados | precisa de `--assert` no Verilator e de um novo status `assert_fail`; não pega erro de interpretação da spec |

### Transformações equivalentes

| Nível | Transformação |
|---|---|
| compilador | trocar a variante do bloco |
| instrução | `mul x, 2^k` ↔ `slli`; `sub` ↔ `neg`+`add`; `rol` ↔ `sll`/`srl`/`or`; `czero` ↔ branch; `aes32esmi` ↔ tabela |
| escalonamento | reordenar independentes; intercalar com bloco neutro |
| registrador | renomeação |
| layout | nops, alinhamento, comprimida ↔ não comprimida |
| ambiente | **interrupção de timer em ciclo aleatório** (handler preserva contexto; contador fora da assinatura) |
| extensão custom | **Xkyber ↔ implementação em RV32IM** |

Para Xkyber e interrupções, o metamórfico é o **único** oráculo disponível. Ele também funciona **na FPGA, sem o Sail**.

## 6. Feedback: interface unificada estilo AFL

Princípio do AFL: um mapa de contadores, buckets de hit count, novidade = posição nova ou bucket novo. Concessão: **mapa particionado por fonte**, para ablação e relatório por fonte.

### Fontes

| ID | Fonte | Quando registra | Chave | Precisa de simulador? |
|---|---|---|---|---|
| 0 | **arestas de pipeline** (`bind`) | todo ciclo | `hash(prev) >> 1 ^ hash(cur)` do snapshot | sim |
| 1 | **transições de trap/CSR com contexto de pipeline** (`bind` no CSRBank) | por evento | ver 6.2 | sim |
| 2 | Verilator (linha, `cover property`) | fim do caso | hash do ponto | sim |
| 3 | ISA pelo trace do Sail (opcional) | por instrução | (classe anterior, classe atual, distância de dependência) | não |
| 4 | **HPM por bloco** (lidos pelo programa) | fronteira de bloco | ver 6.3 | **não** |
| 5 | **traps registrados pelo handler** | por trap | ver 6.3 | **não** |

### 6.1 Snapshot do pipeline (fonte 0)

Cerca de 32 bits, só controle (nunca dados, senão o corpus explode):

| Campo | Sinal no RS5 | Bits |
|---|---|---|
| classe da instrução em decode, execute, retire (~12 classes) | `instruction_operation`, `ctrl_execute.is_*` | 3 × 4 |
| hazard rs1, rs2, mem | `hazard_rs1/rs2/mem` (`decode.sv:621-662`) | 3 |
| origem do forwarding de rs1 e rs2 | recalculada de `rs*`/`rd_*`/`*_we` (`decode.sv:686-708`) | 4 |
| hold mul, div, xkyber | `hold_mul/div/xkyber` (`execute.sv:350-357`) | 3 |
| stall de memória | `stall`, `busy_i` | 2 |
| controle de fluxo | `jump`, `bp_take_fetch`, `jump_rollback` | 3 |
| traps | `ctx_switch`, `RAISE_EXCEPTION`, `MACHINE_RETURN`, `interrupt_pending` | 4 |
| anulada | `ctrl_execute.killed` | 1 |
| contexto privilegiado | `privilege`, `mstatus.MIE`, `mie & mip != 0` | ~4 |

Os buckets distinguem "div em hold por 34 ciclos" de "por 3 ciclos". As arestas dependem de `DELAY_CYCLES` e da configuração de memória: fixar por experimento.

### 6.2 Transições de trap/CSR (fonte 1)

Registradas **por evento**, não por ciclo:

```
chave = hash(estado_antes[~16], estado_depois[~16], evento + atributo[~7], ctx_pipeline[~8])
```

- **Estado** (só controle): `privilege`, `mstatus.MIE/MPIE/MPP`, `mie` (MSIE/MTIE/MEIE), `mip` (MSIP/MTIP/MEIP), `interrupt_pending_o`, `mcountinhibit` CY/IR, `mvmctl_en`.
- **Eventos**: exceção (`exception_code_i`), interrupção aceita (`irq_code`), `mret`, escrita de CSR (grupo do endereço + W/S/C).
- **Contexto do pipeline**: classe em execute, `hold`, `stall`, `ctrl_i.hazard`, `ctrl_i.killed`, `ctrl_i.compressed`, load/store em mem.
- **Fora**: `mepc`, `mtval`, `mscratch`, base do `mtvec`, contadores, valor de `mcause`.

No RS5 o estado de CSR sozinho é pequeno (M/U, sem FPU, sem S-mode) e satura rápido. O valor está no **cruzamento com o pipeline**: exceção com div em hold, interrupção junto com `csrw mstatus` (o próprio código marca esse risco: `@todo What if an interrupt occurs during mstatus write?`, `CSRBank.sv:148`).

O CSRBank já recebe `ctrl_i`, `hold` e `stall`: um `bind` nele enxerga CSR e execute. Só hazards e forwarding precisam de um segundo `bind` no decode.

### 6.3 Feedback só com HPM + traps (fontes 4 e 5): sem simulador

O programa lê os contadores com `csrr` na fronteira de cada bloco. Roda igual em Verilator e em **FPGA**, sem instrumentar o RTL.

```
[ler HPM] ─▶ bloco 1 ─▶ [ler HPM] ─▶ bloco 2 ─▶ ...      Δᵢ = HPM depois − HPM antes
```

- **Vetor do bloco**: `Δmcycle − Δminstret`, `killed`, `hazard`, `stall`, `nop`, `context`, `exception`, `irq`, `misaligned` (os contadores por classe são quase estáticos; servem só de verificação).
- **Estado**: `S_i = hash(id do bloco, buckets(Δ_i))`. A identidade do bloco é obrigatória.
- **Aresta**: `hash(S_{i-1}) >> 1 ^ hash(S_i)`.
- **Traps**: o handler grava `(mcause, MPP, MPIE, bloco, posição no bloco em bucket)`. Dentro do handler, MPP e MPIE já são o estado **antes** do trap.

Necessário:

1. **Isolar a medição**: congelar com `csrw mcountinhibit, -1` antes da leitura e liberar depois; sequência de leitura sempre igual.
2. **Leituras fora da assinatura**; conferir se o Sail aceita `csrr mhpmcounterN` (se ele gerar trap, o diferencial quebra).
3. **`HPMCOUNTEREnable=1`** no modelo e no bitstream.
4. **Eventos novos de HPM** para o que hoje é invisível: `fwd_exec`, `fwd_mem`, `fwd_retire`, `bp_mispredict`, `hazard_rs1/rs2/mem`, hold por unidade. Forwarding bem-sucedido *evita* o hazard, então hoje nenhum contador o vê, e é justamente a parte difícil do RS5. Argumento: **"contadores de desempenho pensados para verificação"**.
5. Na FPGA: loader residente (UART) que recebe o programa, executa e despeja assinatura e contadores.

Limitações: os contadores dão **totais por trecho**, não ordem dentro do bloco; a leitura separa os blocos com instruções de CSR e pode esconder hazards de junção; o **sensor faz parte do DUT** (validar os contadores contra o `bind` em simulação); os eventos de HPM são **específicos do core** (o método é portável, o conjunto de sinais não).

### 6.4 Implementação

SystemVerilog, uma partição por fonte (sem multi-driver):

```systemverilog
module fuzz_cov_part #(parameter int SRC = 0, parameter int BITS = 14)
    (input logic clk, input logic en, input logic [63:0] key);
    byte unsigned counts [2**BITS];
    wire [BITS-1:0] idx = fold(key, SRC);
    always_ff @(posedge clk)
        if (en && counts[idx] != 8'hFF) counts[idx] <= counts[idx] + 1;
    final dump(SRC, counts);   // "SRC idx count" para count != 0
endmodule
```

- Instanciado via `bind` (RTL intacto); sem DPI por ciclo.
- `+FUZZ_COV=0,1,...` liga e desliga fontes sem recompilar.
- Python: `CoverageMap` passa a usar chave `(fonte, índice)`, mesma lógica de `merge` e buckets; **peso por fonte** no corpus; relatório por fonte; opcionalmente guardar a chave original na primeira ocorrência para relatórios legíveis.

## 7. Avaliação

**Métrica: bugs encontrados e tempo até detecção**, não cobertura (evita o argumento circular do SimFuzz).

- **Bugs reais**: reverter fixes do histórico do RS5.
- **Bugs injetados**: mutantes manuais em `decode.sv`/`execute.sv`/`CSRBank.sv` (forwarding errado, hazard faltando, kill esquecido) e/ou MCY; cuidado com mutantes equivalentes.
- **Baselines**: gerador aleatório atual, riscv-dv, Cascade.

### Ablação de feedback

| Variante | Roda em |
|---|---|
| sem feedback (estilo SimFuzz) | sim + FPGA |
| linha do Verilator (atual) | sim |
| transições de CSR na ISA (estilo ProcessorFuzz) | sim |
| arestas de pipeline (`bind`, limite superior) | sim |
| arestas de pipeline + contexto privilegiado | sim |
| **HPM + traps, contadores atuais** | sim + FPGA |
| **HPM + traps, eventos de verificação novos** | sim + FPGA |

Perguntas que a ablação responde:

1. Qual fonte de feedback encontra quais classes de bug (hazard vs trap/interrupção)?
2. Quanto da capacidade do sinal ideal (RTL) o sinal barato (HPM) preserva?
3. Quais eventos de HPM fazem diferença? (recomendação de contadores para um core verificável)
4. Com o mesmo tempo de relógio, FPGA + sinal grosso ganha de simulação + sinal fino? (trabalho futuro se faltar tempo)

## 8. Possíveis desvios da spec encontrados lendo o código (a confirmar)

Testar à mão contra o Sail antes de tudo. Se confirmados, entram no corpus de bugs da avaliação.

1. **`mstatus.MPP` não é legalizado** (`CSRBank.sv:189-191`): `mstatus_mpp <= wr_data[12:11]` sem máscara. MPP é WARL e só deveria aceitar M (11) e U (00). `csrw mstatus` com MPP=01 + `mret` leva o `privilege` para S, que não existe (`CSRBank.sv:1343-1344`).
2. **`mip` aceita escrita em MSIP/MTIP/MEIP** (`CSRBank.sv:246-282`): pela spec são só leitura em M-mode. MTIP/MEIP voltam no ciclo seguinte, mas MSIP fica gravado e pode gerar interrupção de software que o Sail não geraria.
3. A verificar: se `minstret` (arquitetural, deveria bater com o Sail) conta corretamente instruções NOP arquiteturais, dado que incrementa com `!(hold || ctrl_i.is_nop)` (`CSRBank.sv:419`).

## 9. Roteiro

1. Testes manuais dos achados da seção 8 contra o Sail; conferir o Sail com `csrr mhpmcounterN`.
2. Formato de bloco, extrator e ~10 kernels em C com variantes (só `seq`).
3. Gerador por árvore com mutações e `feedback()` mantendo o corpus (substitui o placeholder).
4. Trap handler no template + operadores `trap`, `interleave`, `call`, `branch`.
5. Interface unificada: `fuzz_cov_part` com fontes 0 e 1; migrar o Verilator (fonte 2).
6. Fontes 4 e 5 (HPM + traps lidos pelo programa), em simulação.
7. Transformações equivalentes e oráculo metamórfico (Xkyber, interrupções).
8. Experimento de ablação com bugs revertidos e injetados.
9. (Extensão) eventos de HPM para verificação; FPGA com loader residente.

Os passos 1–5 já formam um TCC fechado; 6–8 o fortalecem; 9 é trabalho futuro se faltar tempo.

## 10. Busca bibliográfica pendente

Antes de afirmar novidade, procurar: *hardware performance counter guided fuzzing*, *PMU feedback CPU fuzzing*, *post-silicon processor fuzzing*, *FPGA-accelerated processor fuzzing*, *metamorphic testing processors*, *CSR transition coverage*. Posicionar explicitamente em relação a Cascade, ProcessorFuzz, DifuzzRTL, MorFuzz, SiliFuzz e TheHuzz.

## Apêndice: CSRs do RS5 em uma página

- **CSR** = *Control and Status Register*, acessado por `csrrw/csrrs/csrrc` (Zicsr). Modos: **M** (11) e **U** (00); o RS5 não tem **S** (01).
- **Trap** = desvio forçado: **exceção** (síncrona) ou **interrupção** (assíncrona).
- Entrada no trap: `mepc` ← PC, `mcause` ← causa, `mtval` ← info extra, `MPIE` ← `MIE`, `MIE` ← 0, `MPP` ← modo, modo ← M, PC ← `mtvec`. **`mret`**: `MIE` ← `MPIE`, modo ← `MPP`, PC ← `mepc`.

| CSR | Função |
|---|---|
| `mtvec` | endereço do tratador; MODE fixo em DIRECT no RS5 |
| `mepc`, `mcause`, `mtval` | onde, por que e detalhe do trap |
| `mscratch` | livre para o software |
| `mstatus` | MIE (chave geral de interrupções), MPIE e MPP (estado antes do trap) |
| `mie` / `mip` | interrupções habilitadas / pendentes, por fonte: MSI (software), MTI (timer, `tip_i` do RTC), MEI (externa, `eip_i` do PLIC). Aceita se `MIE && (mie & mip)` |
| `mcycle`, `minstret` | ciclos (dependente de timing) e instruções retiradas (arquitetural) |
| `mcountinhibit`, `mcounteren` | congelar contadores; liberar leitura em U-mode |
| `mhpmcounter3..31` / `mhpmevent3..31` | **HPM** (*Hardware Performance Monitor*): contadores de eventos definidos pela implementação. No RS5 o evento é fixo por contador (`CSRBank.sv:438-470`): killed, context, exception, irq, hazard, stall, nop, classes de instrução, misaligned, mul, div, vetor |
| `misa`, `mvendorid`, `marchid`, `mimpid`, `mhartid` | identificação |
| `mvm*` | custom do RS5 (XOSVM): MMU simples por regiões |
| `vl`, `vtype`, `vlenb`, `vstart` | estado da extensão vetorial |
