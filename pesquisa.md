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

Princípio do AFL: um mapa de contadores, buckets de hit count, novidade = posição nova ou bucket novo. Como o AFL funciona em detalhe (mapa, arestas `hash(prev) >> 1 ^ hash(cur)`, buckets e uso do feedback): **Apêndice A**.

A organização em três frases:

1. **Um mapa lógico, dividido em uma região por fonte** de feedback (6.1).
2. **Cada fonte registra no seu próprio ritmo** (todo ciclo, a cada evento, a cada bloco…), mas **o fuzzer lê todas de uma vez, no fim de cada caso** (6.2).
3. No fim do caso, a mesma regra do AFL (buckets + bits nunca vistos) é aplicada a cada região, e o resultado decide se o caso entra no corpus (6.7).

### Fontes

| ID | Fonte | Passo (quando registra) | Chave | Onde o mapa é montado | Precisa de simulador? |
|---|---|---|---|---|---|
| 0 | **arestas do pipeline** | cada ciclo de clock | `hash(snapshot anterior) >> 1 ^ hash(snapshot)` (6.3) | no RTL (`bind`) | sim |
| 1 | **transições de trap/CSR com contexto de pipeline** | cada evento de trap/CSR | `hash(antes, depois, evento, contexto)` (6.4) | no RTL (`bind` no CSRBank) | sim |
| 2 | Verilator (linha, `cover property`) | cada avaliação (contagem interna do Verilator) | número do ponto (6.5) | no Python, a partir do `coverage.dat` | sim |
| 3 | ISA pelo trace do Sail (opcional) | cada instrução retirada no Sail | `(classe anterior, classe atual, distância de dependência)` (6.5) | no Python | não (só o Sail) |
| 4 | **HPM por bloco** | cada fronteira de bloco semântico | `hash(S anterior) >> 1 ^ hash(S)` (6.6) | no Python, a partir do buffer gravado pelo programa | **não** |
| 5 | **traps registrados pelo handler** | cada trap tratado | `hash(mcause, MPP, MPIE, bloco, posição)` (6.6) | no Python | **não** |

### 6.1 Um mapa lógico, uma região por fonte

Na prática, é **um mapa lógico dividido em regiões**, uma por fonte. Do lado do fuzzer, cada posição é
identificada pelo par `(fonte, índice)`:

```
fonte 0  arestas do pipeline        índices 0 … 65535   (64K: muitas arestas distintas)
fonte 1  transições de trap/CSR     índices 0 … 4095    (4K: poucas transições possíveis)
fonte 2  Verilator                  índices 0 … 1409    (um por ponto instrumentado, sem hash)
fonte 3  ISA pelo Sail              índices 0 … 16383
fonte 4  HPM por bloco              índices 0 … 16383
fonte 5  traps do handler           índices 0 … 4095
```

Cada região tem o tamanho adequado ao número de chaves distintas que a fonte produz. Os tamanhos acima
são um ponto de partida, a ajustar medindo colisões.

O AFL usa um mapa único e não se importa com a origem de cada posição. Com todas as regiões ligadas e
pesos iguais, a semântica é exatamente a dele: qualquer bucket inédito em qualquer região é novidade.
Separar em regiões serve para quatro coisas que um mapa misturado não permite:

1. **Ablação:** ligar e desligar fontes e responder "qual fonte acha qual bug" (seção 7).
2. **Relatório por fonte:** quantas posições cada fonte cobriu e quais casos trouxeram novidade em cada uma.
3. **Pesos diferentes:** a fonte 0 produz centenas de arestas por caso; a fonte 1, duas ou três. Na hora de
   escolher em quais entradas gastar mutações (6.7), uma transição de trap inédita deve valer mais que uma
   aresta de pipeline inédita.
4. **Sem colisão entre fontes:** a fonte mais movimentada não ocupa posições de uma fonte esparsa.

**Registrar tudo, decidir com um subconjunto.** Em todo experimento, todas as fontes disponíveis ficam
ligadas e gravando; uma opção (por exemplo `--guide 0,1`) escolhe quais contam para decidir se um caso
entra no corpus. As outras são só medidas. Assim dá para responder perguntas cruzadas na ablação, como
"quantas transições de CSR um fuzzer guiado só pelo pipeline alcança".

### 6.2 Três momentos: registrar, capturar, consumir

É importante separar três momentos, que acontecem em lugares diferentes:

1. **Registrar** (o "passo" da fonte): o momento em que um contador da fonte é incrementado. É o
   equivalente, no AFL, a executar um bloco básico. **Cada fonte tem o seu ritmo.**
2. **Capturar:** onde o dado bruto fica guardado durante a execução (um array no RTL, os contadores
   internos do Verilator, um buffer na memória do programa, o log do Sail).
3. **Consumir:** o momento em que o fuzzer lê o mapa, aplica buckets e novidade e decide. **É um só,
   igual para todas as fontes: o fim do caso.** O fuzzer nunca lê nada durante a simulação.

| Fonte | 1. Registrar (passo) | 2. Capturar (dado bruto) | 3. Consumir |
|---|---|---|---|
| 0 | cada ciclo de clock | array de contadores no módulo `bind` | fim do caso, via `fuzz_cov.txt` |
| 1 | cada exceção, interrupção aceita, `mret` ou escrita de CSR | array de contadores no `bind` do CSRBank | fim do caso, via `fuzz_cov.txt` |
| 2 | cada avaliação de linha/branch/cover | contadores do Verilator → `coverage.dat` no `$finish` | fim do caso |
| 3 | cada instrução retirada no Sail | trace do Sail | fim do caso (o Python percorre o trace) |
| 4 | cada fronteira de bloco: o programa lê os HPM com `csrr` | buffer na memória, fora da assinatura | fim do caso (o testbench ou a FPGA despeja o buffer; o Python monta o mapa) |
| 5 | cada trap: o handler grava os dados | buffer na memória, fora da assinatura | fim do caso, idem |

Linha do tempo de um caso:

```
ciclo:    0    1    2   …   57   58  …   120  …   250 ($finish)
fonte 0:  +    +    +   …   +    +   …   +    …   +           um incremento por ciclo
fonte 1:                    +             +                    trap no ciclo 57, mret no 120
fonte 4:  |── bloco 1 ──|[lê HPM]|──── bloco 2 ────|[lê HPM]|── bloco 3 ──|
fonte 5:                    (handler grava mcause, MPP…)
fonte 2:  (o Verilator conta por dentro)
                                                     │
                                                     ▼
      dump: fuzz_cov.txt (0, 1) + coverage.dat (2) + buffer HPM/traps (4, 5) + trace do Sail (3)
                                                     │
                                                     ▼
      Python: monta as chaves das fontes 2–5 → buckets → novidade por região → feedback()
```

Por que consumir só no fim:

- É o que o AFL faz: o mapa é lido depois que a execução termina.
- Ler durante a simulação exigiria comunicação com o Python a cada ciclo (DPI ou pipe), o que é caro.
- A decisão é por caso de qualquer jeito: guardar ou não guardar o programa inteiro.

### 6.3 Fonte 0: arestas do pipeline (todo ciclo)

**Estado:** um snapshot de cerca de 32 bits, só com sinais de controle (nunca dados, senão todo caso
seria "novo" e o corpus explodiria):

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

**Chave:** `hash(snapshot do ciclo anterior) >> 1 ^ hash(snapshot deste ciclo)`. O `hash()` é
obrigatório: diferente dos IDs aleatórios do AFL, os snapshots têm bits com significado, e o XOR direto
de dois snapshots parecidos colidiria muito (Apêndice A.6).

**Volume:** um incremento por ciclo. Num caso de 250 ciclos, são 250 incrementos espalhados por algumas
dezenas a centenas de arestas distintas.

**Exemplo:** um load seguido de uma instrução que usa o resultado:

```
ciclo t:    decode=ADD(usa x5)  execute=LOAD(escreve x5)  hazard_rs1=1    ← detecta o hazard
ciclo t+1:  decode=ADD          execute=bolha              fwd_rs1=retire
ciclo t+2:  decode=…            execute=ADD                fwd_rs1=—
```

As transições t → t+1 e t+1 → t+2 são duas arestas. "Hazard de load resolvido por forwarding a partir do
retire" vira uma posição do mapa, e o primeiro programa que a produzir é guardado.

**O que o bucket mede:** a contagem de uma aresta é **somada no caso inteiro**, como no AFL (a aresta de
um laço conta todas as iterações de todas as visitas). Se o div fica em hold duas vezes por 34 ciclos, a
transição `hold → hold` conta 66 e cai em `32–127`. O bucket mede **quanto tempo no total** o pipeline
passou naquela transição, não a duração de cada ocorrência. Para distinguir "um hold longo" de "dois
holds médios", dá para incluir no snapshot um campo "há quantos ciclos estou neste estado", já em bucket.
É opcional; dá para começar sem.

As arestas dependem de `DELAY_CYCLES` e da configuração de memória: fixar por experimento.

### 6.4 Fonte 1: transições de trap/CSR (a cada evento)

**Quando registra:** só nos ciclos em que `raise_exception_i`, `interrupt_ack_i`, `machine_return_i` ou
`write_enable_i` estão ativos no CSRBank.

**Chave:** aqui a transição é explícita (estado antes e depois do evento), então não se usa o `>> 1`:

```
chave = hash(estado_antes[~16], estado_depois[~16], evento + atributo[~7], ctx_pipeline[~8])
```

Como o "depois" só aparece nos registradores no ciclo seguinte, o módulo guarda o evento num ciclo e
monta a chave no próximo.

- **Estado** (só controle): `privilege`, `mstatus.MIE/MPIE/MPP`, `mie` (MSIE/MTIE/MEIE), `mip` (MSIP/MTIP/MEIP), `interrupt_pending_o`, `mcountinhibit` CY/IR, `mvmctl_en`.
- **Eventos**: exceção (`exception_code_i`), interrupção aceita (`irq_code`), `mret`, escrita de CSR (grupo do endereço + W/S/C).
- **Contexto do pipeline**: classe em execute, `hold`, `stall`, `ctrl_i.hazard`, `ctrl_i.killed`, `ctrl_i.compressed`, load/store em mem.
- **Fora**: `mepc`, `mtval`, `mscratch`, base do `mtvec`, contadores, valor de `mcause`.

**Volume:** de zero a algumas dezenas de eventos por caso.

**O que o bucket mede:** quantas vezes **a mesma transição** aconteceu no caso. Um único `ecall` de U para
M é diferente de vinte.

No RS5 o estado de CSR sozinho é pequeno (M/U, sem FPU, sem S-mode) e satura rápido. O valor está no **cruzamento com o pipeline**: exceção com div em hold, interrupção junto com `csrw mstatus` (o próprio código marca esse risco: `@todo What if an interrupt occurs during mstatus write?`, `CSRBank.sv:148`).

O CSRBank já recebe `ctrl_i`, `hold` e `stall`: um `bind` nele enxerga CSR e execute. Só hazards e forwarding precisam de um segundo `bind` no decode.

### 6.5 Fontes 2 e 3: Verilator e ISA pelo Sail

**Fonte 2 (Verilator).** É o feedback que o fuzzer usa hoje. O Verilator conta internamente cada linha,
branch e `cover property` e grava tudo no `coverage.dat` no `$finish`. Como a lista de pontos é fixa
(1410 no total, 1240 no DUT), não precisa de hash: o número do ponto é o índice. O `report.md` mostrou
que 26,5% desses pontos são **artefatos** (lógica combinacional avaliada para toda instrução), então essa
fonte entra na ablação como **comparação**, de preferência sem as estruturas de artefato. As
`cover property` escritas à mão continuam úteis aqui, porque podem ser condicionadas ao que de fato
aconteceu (por exemplo, `read_enable_i`).

**Fonte 3 (ISA pelo Sail, opcional).** O Python percorre o trace do Sail e, para cada instrução retirada,
monta `(classe anterior, classe atual, distância até a instrução de que depende, em bucket)`. Mede a
diversidade **do programa**, não do RS5, e por isso vale para qualquer core. Custo: o Sail precisa
gravar o trace, o que deixa cada execução mais lenta.

### 6.6 Fontes 4 e 5: HPM + traps lidos pelo programa (sem simulador)

Aqui quem captura o dado é o **próprio programa**, e não o RTL. Por isso roda igual em Verilator e em
**FPGA**, sem instrumentar o Verilog. O programa só **lê e guarda** valores brutos num buffer; quem calcula
estados, chaves e o mapa é o Python, depois do dump.

```
[ler HPM] ─▶ bloco 1 ─▶ [ler HPM] ─▶ bloco 2 ─▶ ...      Δᵢ = HPM depois − HPM antes
```

**Fonte 4 (HPM por bloco):**

- **Vetor do bloco**: `Δmcycle − Δminstret`, `killed`, `hazard`, `stall`, `nop`, `context`, `exception`, `irq`, `misaligned` (os contadores por classe de instrução são quase estáticos, porque o gerador já sabe o mix de cada bloco; servem só de verificação).
- **Estado do bloco**: `S_i = hash(id do bloco, buckets(Δ_i))`. A identidade do bloco é obrigatória: 5 ciclos de hazard num memcpy e num CRC são situações diferentes.
- **Aresta**: `hash(S_{i-1}) >> 1 ^ hash(S_i)`.

Nesta fonte os **buckets aparecem duas vezes**, com papéis diferentes:

1. **Dentro da chave**, para discretizar o estado. Cada componente do Δ vira bucket antes de entrar no
   hash, para que 7 e 8 hazards não sejam estados diferentes à toa. Exemplo: um bloco com Δ = (12 ciclos
   perdidos, 0 kills, 9 hazards) vira o estado `(id, 8–15, 0, 8–15)`.
2. **Na contagem**, como em todas as fontes. A aresta `S_{i-1} → S_i` é incrementada cada vez que a
   sequência acontece, e no fim essa contagem passa pelo bucket da regra de novidade. Mede quantas vezes
   aquela sequência de blocos, com aquele perfil, se repetiu no caso (num `loop(k, A)`, por exemplo).

**Fonte 5 (traps do handler):** o handler grava `(mcause, MPP, MPIE, id do bloco, posição no bloco em
bucket)` a cada trap, e o Python transforma isso em chave. Dentro do handler, MPP e MPIE já são o estado
**antes** do trap. É o equivalente sem simulador da fonte 1, mas não vê o contexto do pipeline, que só o
`bind` enxerga.

Necessário:

1. **Isolar a medição**: congelar com `csrw mcountinhibit, -1` antes da leitura e liberar depois; sequência de leitura sempre igual.
2. **Buffer fora da assinatura**; conferir se o Sail aceita `csrr mhpmcounterN` (se ele gerar trap, o diferencial quebra).
3. **`HPMCOUNTEREnable=1`** no modelo e no bitstream.
4. **Eventos novos de HPM** para o que hoje é invisível: `fwd_exec`, `fwd_mem`, `fwd_retire`, `bp_mispredict`, `hazard_rs1/rs2/mem`, hold por unidade. Forwarding bem-sucedido *evita* o hazard, então hoje nenhum contador o vê, e é justamente a parte difícil do RS5. Argumento: **"contadores de desempenho pensados para verificação"**.
5. Na FPGA: loader residente (UART) que recebe o programa, executa e despeja assinatura, contadores e buffer de traps.

Limitações: os contadores dão **totais por trecho**, não ordem dentro do bloco; a leitura separa os blocos com instruções de CSR e pode esconder hazards de junção; o **sensor faz parte do DUT** (validar os contadores contra o `bind` em simulação); os eventos de HPM são **específicos do core** (o método é portável, o conjunto de sinais não).

### 6.7 Junção no fuzzer (fim de cada caso)

Depois do dump, o Python junta todas as fontes num único dicionário e aplica a regra do AFL região por
região:

```python
hits = {(fonte, índice): contagem, ...}          # todas as fontes gravadas neste caso

novidade = Counter()
for (fonte, idx), c in hits.items():
    b = bit_do_bucket(c)                         # 1, 2, 3, 4–7, … → um bit (Apêndice A.3)
    if b & ~vistos[fonte][idx]:                  # algum bucket inédito nesta posição
        novidade[fonte] += 2 if vistos[fonte][idx] == 0 else 1   # posição nova vale mais
        vistos[fonte][idx] |= b

score   = sum(peso[f] * novidade[f] for f in fontes_que_guiam)   # --guide
guardar = score > 0
```

- **`vistos`** é o virgin map do AFL, um por região, com **um bit por bucket**. O `CoverageMap` atual
  guarda só o bucket máximo de cada ponto e precisa mudar para isso (Apêndice A.4).
- **Pesos por fonte:** não mudam *se* o caso é guardado (qualquer novidade numa fonte que guia já basta),
  mas mudam **quanta energia** ele recebe, ou seja, quantas mutações. Um ponto de partida: transições de
  trap/CSR e traps do handler com peso maior que arestas de pipeline, e o Verilator com peso menor.
- **Favoritas:** para cada posição `(fonte, índice)`, guarda-se a menor árvore de composição (ou a mais
  rápida) que a cobre. As árvores desse conjunto mínimo recebem a maior parte das mutações.
- **Calibração:** cada caso novo roda duas vezes. Posições que mudam entre as duas execuções são
  marcadas como instáveis naquela região e ignoradas. Isso importa sobretudo para as fontes 4 e 5 na FPGA
  e para a fonte 0 quando o timing de memória varia.
- **Relatório:** por região, posições cobertas, novidades por caso e a chave original da primeira
  ocorrência (para mostrar, por exemplo, `IRQ timer, MIE 1→0, U→M, div em hold` em vez de um índice).

### 6.8 Implementação

| Fonte | Onde | Como |
|---|---|---|
| 0 e 1 | SystemVerilog, módulo `fuzz_cov_part` instanciado via `bind` (RTL intacto) | um array por fonte; despejo esparso em `fuzz_cov.txt` no `final` |
| 2 | Verilator | `parse_coverage_dat` atual; número do ponto = índice |
| 3 | Python | leitura do trace do Sail |
| 4 e 5 | programa (leitura e gravação no buffer) + testbench/FPGA (despejo) + Python (mapa) | buffer em região de memória própria, despejado no fim como a assinatura |

Módulo das fontes 0 e 1, uma instância por fonte (sem multi-driver no mesmo array):

```systemverilog
module fuzz_cov_part #(parameter int SRC = 0, parameter int BITS = 14)
    (input logic clk, input logic en, input logic [63:0] key);
    byte unsigned counts [2**BITS];
    wire [BITS-1:0] idx = fold(key, SRC);        // hash de mistura + dobra para BITS bits
    always_ff @(posedge clk)
        if (en && counts[idx] != 8'hFF) counts[idx] <= counts[idx] + 1;   // satura em 255
    final dump(SRC, counts);   // "SRC idx count" só para count != 0
endmodule
```

- Fonte 0: `en` ativo todo ciclo depois do reset; `key` é a aresta do snapshot.
- Fonte 1: `en` ativo no ciclo seguinte a um evento; `key` é a chave da 6.4.
- Sem DPI por ciclo: tudo fica dentro do modelo C++ gerado pelo Verilator.
- `+FUZZ_COV=0,1` liga e desliga a gravação no RTL sem recompilar (por padrão, tudo ligado).

Do lado do Python:

- `CoverageMap` com chave `(fonte, índice)` e bitmap de buckets por posição.
- Opção `--guide` com as fontes que decidem e um peso por fonte.
- Relatório por região, com a chave original guardada na primeira ocorrência.

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

## Apêndice A: como o AFL funciona (mapa, arestas e buckets)

O AFL (*American Fuzzy Lop*) e seu sucessor AFL++ são fuzzers de software guiados por cobertura. A
ideia que vamos transpor para o RS5 tem três peças: um **mapa** de contadores, uma forma de transformar
**transições** em posições desse mapa e uma regra de **novidade** baseada em **buckets**.

### A.1 O laço do fuzzer

```
          ┌──────────────────────────────────────────────────────────────┐
          ▼                                                              │
   escolhe uma entrada da fila ─▶ muta ─▶ executa o alvo instrumentado ─▶ olha o mapa
                                                                          │
                                  novidade? ── sim ─▶ entra na fila ──────┘
                                      └─ não ─▶ descarta
```

1. A **fila** (*queue*) começa com as seeds.
2. O AFL pega uma entrada, aplica mutações (inverter bits, somar e subtrair, trocar por valores
   "interessantes" como 0, −1 e INT_MAX, empilhar várias mutações aleatórias, emendar duas entradas).
3. Executa o programa alvo, que foi compilado com instrumentação.
4. A instrumentação preenche um **mapa** durante a execução.
5. Se o mapa mostra algo que **nunca foi visto** antes, a entrada mutada entra na fila e vira ponto de
   partida para novas mutações. Se não, é descartada.

O efeito é **evolutivo**: cada entrada guardada é um degrau, e mutações acumuladas alcançam código
profundo que uma entrada aleatória nunca alcançaria.

### A.2 O mapa e as arestas

Na compilação, o AFL dá a cada **bloco básico** (trecho de código sem desvios no meio) um **ID
aleatório** e insere este código no início do bloco:

```c
cur_location = <ID aleatório deste bloco>;      // constante gerada na compilação
shared_mem[cur_location ^ prev_location]++;     // conta a aresta (prev → cur)
prev_location = cur_location >> 1;              // guarda o bloco atual, deslocado
```

- `shared_mem` é o **mapa**: 64 KiB de contadores de 8 bits, em memória compartilhada com o fuzzer.
- Cada posição representa uma **aresta**, ou seja, uma transição "do bloco A para o bloco B". Como
  `prev_location` já guarda `A >> 1`, o índice é `(A >> 1) ^ B`, que é exatamente o
  `hash(prev) >> 1 ^ hash(cur)` usado neste documento.

**Por que arestas e não blocos?** Dois caminhos podem passar pelos mesmos blocos em ordem diferente:

| Caminho | Blocos visitados | Arestas |
|---|---|---|
| A → B → C → D → E | A, B, C, D, E | AB, BC, CD, DE |
| A → B → D → C → E | A, B, C, D, E | AB, BD, DC, CE |

A cobertura de blocos (ou de linha, como a do Verilator) é **idêntica** nos dois. A cobertura de arestas
distingue os dois caminhos. Muitos bugs só aparecem numa ordem específica.

**Por que o XOR?** Ele combina os dois IDs num único índice de forma barata (uma instrução) e espalha
bem quando os IDs são aleatórios.

**Por que o `>> 1`?** Sem o deslocamento, o XOR é simétrico e anula valores iguais:

| Transição | Sem `>> 1`: `A ^ B` | Com `>> 1`: `(A >> 1) ^ B` |
|---|---|---|
| A → B (A=10, B=6) | 10 ^ 6 = **12** | 5 ^ 6 = **3** |
| B → A | 6 ^ 10 = **12** (igual: perdeu a direção) | 3 ^ 10 = **9** |
| A → A (laço) | 10 ^ 10 = **0** | 5 ^ 10 = **15** |
| B → B (laço) | 6 ^ 6 = **0** (todo laço cai no índice 0) | 3 ^ 6 = **5** |

O deslocamento preserva a **direção** da aresta e evita que todo laço de um bloco para ele mesmo
colida no índice 0.

**Colisões.** Duas arestas diferentes podem cair no mesmo índice. Com IDs aleatórios e um mapa de 64K
posições, isso é tolerável enquanto o número de arestas do programa for bem menor que 64K. O AFL++ tem
modos de instrumentação sem colisão para programas grandes.

### A.3 Buckets: classificar quantas vezes cada aresta foi usada

Cada posição do mapa conta **quantas vezes** a aresta foi percorrida em uma execução. O AFL não compara
essa contagem exata, e sim a **faixa** em que ela cai, chamada **bucket**:

| Contagem na execução | 1 | 2 | 3 | 4–7 | 8–15 | 16–31 | 32–127 | 128+ |
|---|---|---|---|---|---|---|---|---|
| Bucket (um bit) | `0x01` | `0x02` | `0x04` | `0x08` | `0x10` | `0x20` | `0x40` | `0x80` |

Depois de cada execução, o AFL troca cada contador pelo bit do seu bucket (uma tabela de consulta faz
isso para o mapa inteiro de uma vez).

**Por que buckets?** Para separar comportamentos diferentes sem se afogar em variações irrelevantes:

- Um laço que roda 5 ou 6 vezes é, na prática, o **mesmo** comportamento. Os dois caem em `4–7`.
- Um laço que roda 1 vez e um que roda 50 vezes são **diferentes**: um não iterou, o outro iterou muito.
  Caem em `1` e `32–127`.
- Sem buckets, cada contagem exata seria "novidade", e a fila explodiria com variações de contador.
- As faixas crescem quase em escala logarítmica: muita resolução para contagens pequenas, onde a
  diferença costuma importar, e pouca para contagens grandes.

### A.4 A regra de novidade

O AFL mantém um mapa global, o **virgin map**, que começa com todos os bits em 1 ("nunca visto"). Depois
de cada execução, com o mapa já convertido em bits de bucket:

```c
if (trace[i] & virgin[i]) {        // algum bucket desta aresta nunca foi visto
    novidade = 1;                   // (2 se a aresta inteira era inédita)
    virgin[i] &= ~trace[i];         // marca esses buckets como vistos
}
```

Há dois níveis de novidade, e os dois fazem a entrada ser guardada:

1. **Aresta nova:** a posição nunca tinha sido tocada.
2. **Bucket novo:** a aresta já existia, mas com uma contagem numa faixa nunca vista.

Detalhe importante: o virgin map guarda **um bit por bucket**, não o máximo. Se uma aresta já foi vista
128+ vezes e uma nova entrada a percorre exatamente **1** vez, isso também é novidade. O `CoverageMap` do
fuzzer atual (`coverage.py`) só guarda o **bucket máximo** de cada ponto e só considera novidade quando o
bucket **sobe**. No teste do `report.md` (seção 8), isso descartou 241 pares (ponto, bucket) que o AFL
teria contado. A interface unificada deve usar o bitmap de buckets.

Exemplo com o laço do template `branch` do fuzzer atual (`li x5, 10` … `addi x5, x5, -1` /
`bnez x5, 1b`), supondo que uma mutação pudesse trocar o valor inicial de `x5`. Com `x5 = n`, o laço
roda `n` vezes e a aresta de volta do `bnez` é percorrida `n − 1` vezes (a última iteração sai do laço):

| Entrada | Aresta de volta percorrida | Bucket | Novidade? |
|---|---|---|---|
| seed: `x5 = 10` | 9 vezes | `8–15` | sim (aresta nova) |
| mutante 1: `x5 = 12` | 11 vezes | `8–15` | não |
| mutante 2: `x5 = 4` | 3 vezes | `3` | **sim** (bucket novo) |
| mutante 3: `x5 = 2` | 1 vez | `1` | **sim** (bucket novo, mesmo abaixo do máximo já visto) |

### A.5 Como o AFL aproveita o feedback

Guardar as entradas com novidade é só o começo. O AFL usa o mapa para decidir **onde gastar tempo**:

- **Favoritas** (*favored*): para cada posição do mapa, o AFL guarda a entrada "mais barata" que a cobre
  (menor tempo de execução × tamanho). As entradas que formam esse conjunto mínimo são favoritas e
  recebem a maior parte das mutações; as outras são puladas com alta probabilidade.
- **Energia** (*performance score*, *power schedules* no AFL++): cada entrada recebe um número de mutações
  proporcional ao quanto parece promissora: rápida, recente, que cobre arestas raras.
- **Calibração**: cada entrada nova é executada várias vezes. Posições do mapa que mudam entre execuções
  idênticas são marcadas como **instáveis** e ignoradas na regra de novidade (senão o ruído viraria
  "novidade" eterna).
- **Trim**: a entrada é encolhida enquanto o mapa continuar igual, para mutações mais baratas e eficazes.

### A.6 Tradução para o fuzzer do RS5

| AFL (software) | Fuzzer do RS5 |
|---|---|
| bloco básico com ID aleatório | **estado**: snapshot do pipeline (fonte 0), estado de CSR + evento (fonte 1), estado HPM de um bloco semântico (fonte 4) |
| um passo = executar um bloco básico | um **ciclo de clock** (fonte 0), um **evento** de trap/CSR (fonte 1), um **bloco semântico** (fonte 4) |
| `prev_location = cur >> 1` | snapshot do ciclo/evento/bloco anterior |
| aresta `(prev >> 1) ^ cur` | **transição de estado** `hash(prev) >> 1 ^ hash(cur)` |
| mapa de 64 KiB em memória compartilhada | um mapa lógico com uma região por fonte (seção 6.1): fontes 0 e 1 em arrays no RTL (`fuzz_cov_part`), fontes 2–5 montadas no Python; tudo lido uma vez, no fim do caso (seção 6.2) |
| buckets e virgin map | mesmos buckets de `coverage.py`; trocar o "máximo" do `CoverageMap` por um bitmap de buckets |
| fila e favoritas | corpus de árvores de composição; favorita = menor árvore (ou mais rápida) que cobre cada posição |
| calibração | rodar cada caso novo duas vezes e descartar posições instáveis (essencial na FPGA e com `DELAY_CYCLES` variável) |
| trim | delta debugging na árvore |

Diferenças que exigem cuidado na transposição:

- **O `hash()` é obrigatório.** No AFL, os IDs já são aleatórios e o XOR espalha bem. Nossos snapshots
  são estruturados (bits vizinhos com significado), e o XOR direto de dois snapshots parecidos colidiria
  muito. Primeiro se aplica um hash de mistura ao snapshot, depois o `>> 1` e o XOR.
- **Laços viram duração.** Quando o pipeline fica parado no mesmo estado (por exemplo, o div em hold por
  34 ciclos), a mesma transição `S → S` se repete a cada ciclo, e a contagem cai num bucket. Como a
  contagem é somada no caso inteiro, o bucket mede o **tempo total** naquela transição (seção 6.3), e o
  `>> 1` impede que todos esses estados estacionários colidam no índice 0.
- **O espaço de estados é maior que o de blocos.** Um programa tem um número fixo de blocos básicos, mas o
  snapshot do pipeline pode assumir muitos valores. Por isso o snapshot só leva sinais de **controle**
  (seção 6.3). Um bit de dado no snapshot tornaria todo caso "novo".
- **Avaliação combinacional não é execução.** A cobertura de linha conta lógica avaliada mesmo quando o
  resultado é descartado (`report.md`, seção 4). As fontes 0 e 1 usam sinais que refletem o que o
  pipeline **fez** (hazard, hold, kill, trap), o que evita esse artefato.

## Apêndice B: CSRs do RS5 em uma página

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
