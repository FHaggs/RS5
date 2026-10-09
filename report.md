# Cobertura do RS5: fuzzer atual × riscv-arch-test

Teste do fuzzer atual (`riscof/fuzzer/`, gerador placeholder) contra a baseline completa do
riscv-arch-test rodada pelo RISCOF, ambos na configuração `baseline` (RV32IMU_Zicsr_Zihpm).
Nenhum código do projeto foi alterado para este relatório.

## Resumo

1. **O escopo está correto.** O filtro do fuzzer conta exatamente os 1240 pontos de `riscof_tb.dut.*`.
   Os 170 pontos do testbench, da RAM, do RTC e do PLIC ficam de fora.
2. **Os números brutos favorecem o fuzzer, mas enganam.** Fuzzer: 75,6% (200 casos) e 78,1% (2000 casos).
   Baseline: 67,1%. Porém **100% dos pontos que só o fuzzer cobre** estão em duas estruturas
   combinacionais avaliadas para *toda* instrução (o mux de leitura de CSR e a decodificação de
   `{funct7, funct3}`). Os imediatos aleatórios do fuzzer "acertam" esses braços sem que nenhuma
   instrução de CSR ou de extensão seja executada.
3. **Sem esses artefatos, a cobertura do fuzzer é um subconjunto estrito da baseline.** Nos pontos
   alcançáveis desta configuração: baseline **91,5%**, fuzzer **85,0%** (200) / **85,2%** (2000),
   união **91,5%**. O fuzzer não acrescenta nenhum ponto real.
4. **O fuzzer satura cedo.** O último ponto novo aparece por volta do caso 550 (de 2000).
5. **Nos buckets, a baseline domina.** Sem artefatos, ela leva 171 pontos a um bucket máximo mais alto
   que o fuzzer, e o fuzzer não supera a baseline em nenhum. Isso reflete em boa parte o tamanho dos
   programas: os testes da baseline têm em média 3946 ciclos, e os casos do fuzzer, 252.
6. **154 dos 1240 pontos (12,4%) são inalcançáveis nesta configuração** (64 por amarrações do
   testbench, 90 por parâmetros). Eles nunca podem ser cobertos, mas entram no denominador.
7. **Problemas encontrados no caminho** (seção 9): `--work-dir` relativo quebra o fuzzer (inclusive o
   `make fuzz` padrão), e toolchains com binutils ≥ 2.44 quebram a suíte do arch-test.

## 1. Configuração e método

| Item | Valor |
|---|---|
| Commit | `9086313` (branch `cov-tests`) |
| Configuração | `rs5/baseline.yaml`: `RV32IMUZicsr_Zihpm`; `BRANCHPRED=1 FORWARDING=1 DUALPORT_MEM=1 IQUEUE_SIZE=2 DELAY_CYCLES=0` |
| Verilator | 5.052, `--coverage-line --coverage-user` (o mesmo comando para os dois lados, via `rs5_verilator.py`) |
| Toolchain | `riscv64-elf` GCC 14.2.0 / binutils 2.43 (ver seção 9.2) |
| Sail | `sail-riscv/0.7` (`riscv_sim_rv32d`) |
| Baseline | riscv-arch-test 3.9.1 pelo RISCOF: **62 testes** (39 de `I`, 8 de `M`, 15 de `privilege`), **62/62 aprovados**, todos terminando em `tohost` |
| Fuzzer | `fuzz.py -n 200 --seed 1 --keep` (configuração padrão) e `-n 2000 --seed 1 --keep` (para ver a saturação); **todos os casos `pass`** |

"riscv-tests" aqui significa a suíte **riscv-arch-test** que o RISCOF já roda no projeto (alvo
`coverage-baseline`), e não o repositório `riscv-software-src/riscv-tests`.

**Cobertura agregada** = união: um ponto conta como coberto se *algum* teste o atinge. Se um teste cobre A
e outro cobre B, o conjunto cobre A e B.

**Buckets** são os do AFL, os mesmos de `coverage.py`: a contagem de um ponto *em um teste* cai em
`1, 2, 3, 4–7, 8–15, 16–31, 32–127, 128+`. Na agregação, uso duas visões:

- **pares (ponto, bucket)**: a semântica do AFL, em que qualquer bucket ainda não visto para aquele
  ponto é novidade;
- **bucket máximo por ponto**: o que o `CoverageMap` do fuzzer guarda hoje.

A análise relê todos os `coverage.dat` **sem** o filtro do fuzzer e reclassifica cada ponto.

## 2. Escopo: só o processador entra na conta?

Sim. Pontos instrumentados por instância de topo do `riscof_tb`:

| Instância | Pontos | Contada? | Cobertos pela baseline |
|---|---:|---|---:|
| `dut` (RS5) | 1240 | sim | 832 |
| `RAM_MEM` | 51 | não | — |
| `rtc` | 48 | não | — |
| `plic1` | 37 | não | — |
| `riscof_tb` (o próprio testbench) | 34 | não | — |
| **Total** | **1410** | | 127 dos 170 de fora |

- O filtro `DUT_SCOPE` de `coverage.py` aceita **exatamente** os pontos com hierarquia
  `riscof_tb.dut` ou `riscof_tb.dut.*` (verificado ponto a ponto; nenhuma diferença).
- Dentro do DUT há apenas partes do processador: `fetch1` (com a fila de instruções `ibuf`),
  `decoder1`, `execute1` (com `mul`/`div`), `mem_access1`, `retire1`, `CSRBank1` e `RegFileFF_blk`.
- Sem o filtro, a baseline marcaria 68,0% em vez de 67,1%. O efeito é pequeno porque a lógica de
  fora é simples e quase toda exercitada por qualquer programa.
- Observação: o filtro procura a substring `\x01h\x02riscof_tb.dut` na chave, então também
  aceitaria uma instância chamada, por exemplo, `dut_wrapper`. Hoje isso não acontece. Comparar a
  hierarquia com `== 'riscof_tb.dut' or startswith('riscof_tb.dut.')` deixaria o filtro exato.

O que o filtro **não** resolve é o denominador incluir pontos impossíveis nesta configuração.
Isso está na seção 5.

## 3. Cobertura agregada bruta

| Conjunto | Pontos cobertos | % de 1240 |
|---|---:|---:|
| Baseline (62 testes) | 832 | 67,1% |
| Fuzzer, 200 casos | 938 | 75,6% |
| Fuzzer, 2000 casos | 969 | 78,1% |
| União baseline + fuzzer 200 | 995 | 80,2% |
| União baseline + fuzzer 2000 | 1019 | 82,2% |

| Comparação | Fuzzer 200 | Fuzzer 2000 |
|---|---:|---:|
| Cobertos pelos dois | 775 | 782 |
| Só baseline | 57 | 50 |
| Só fuzzer | 163 | 187 |
| Nenhum dos dois | 245 | 221 |

Por tipo de ponto (fuzzer com 2000 casos):

| Tipo | Total | Baseline | Fuzzer | União |
|---|---:|---:|---:|---:|
| `v_line` | 834 | 540 | 685 | 719 |
| `v_branch` | 406 | 292 | 284 | 300 |

Por módulo (bruto; fuzzer 200 / 2000):

| Módulo | Total | Baseline | Fuzzer 200 | Fuzzer 2000 | Só baseline | Só fuzzer (2000) |
|---|---:|---:|---:|---:|---:|---:|
| `CSRBank1` | 539 | 278 | 393 | 423 | 28 | 173 |
| `decoder1` | 327 | 237 | 243 | 244 | 7 | 14 |
| `execute1` | 216 | 178 | 164 | 164 | 14 | 0 |
| `fetch1` | 124 | 108 | 107 | 107 | 1 | 0 |
| `retire1` | 17 | 17 | 17 | 17 | 0 | 0 |
| `mem_access1` | 12 | 9 | 9 | 9 | 0 | 0 |
| `RegFileFF_blk` | 5 | 5 | 5 | 5 | 0 | 0 |

### Saturação

Pontos acumulados na ordem de execução:

| Casos/testes | 1 | 5 | 10 | 20 | 50 | 100 | 200 | 500 | 1000 | 2000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Fuzzer | 637 | 738 | 752 | 781 | 829 | 890 | 938 | 968 | 969 | 969 |
| Baseline (ordem alfabética) | 626 | 639 | 679 | 712 | 825 | — | — | — | — | — |

O último ponto novo do fuzzer aparece no 549º caso concluído. Depois disso, ~1450 casos não acrescentam
nada.
Um único programa qualquer já cobre ~630 pontos (pipeline, fetch, ALU básica).

Custo: a baseline simula 244.666 ciclos (média de 3946 por teste, de 133 a 28.636). O fuzzer simula
50.466 ciclos em 200 casos (média de 252, de 201 a 362). Em tempo de parede, a baseline completa
leva ~1 min (dos quais ~35 s são o riscof montando a base de testes), e o fuzzer leva 1,1 s para 200
casos e 4,9 s para 2000 em 112 núcleos (sem contar o build do modelo).

## 4. Por que os números brutos enganam: artefatos de avaliação

Toda a vantagem do fuzzer vem de duas estruturas combinacionais que o RTL avalia para **qualquer**
instrução, mesmo quando o resultado é descartado.

**Mux de leitura de CSR** ([CSRBank.sv:1358-1584](rtl/CSRBank.sv#L1358-L1584)). O decoder faz
`csr_address = instruction_i[31:20]` para toda instrução ([decode.sv:593](rtl/decode.sv#L593)), e o
`case (address_i)` que monta `current_val` roda sempre. A saída só é usada quando `read_enable_i`
está ativo ([CSRBank.sv:1586](rtl/CSRBank.sv#L1586)). Assim, um `addi x1, x2, 0x300` "cobre" o braço
`MSTATUS` do mux sem ler CSR nenhum.

**Decodificação de `{funct7, funct3}`** ([decode.sv:173-235](rtl/decode.sv#L173-L235)). Os dois
`always_comb` que calculam `decode_op` e `decode_op_imm` casam os bits 31:25 e 14:12 de toda instrução,
sem olhar o opcode. Os imediatos aleatórios de `lui` (gerados pelos `li` do fuzzer) preenchem esses
bits com padrões de Zbkb, Zknh e Zicond, que estão desligadas.

| Estrutura | Pontos | % do total | Baseline | Fuzzer 200 | Fuzzer 2000 |
|---|---:|---:|---:|---:|---:|
| Mux de leitura de CSR | 216 | 17,4% | 42 | 185 | 215 |
| `decode_op` / `decode_op_imm` | 113 | 9,1% | 69 | 83 | 83 |
| **Total** | **329** | **26,5%** | 111 | 268 | 298 |

Verificação: dos pontos que só o fuzzer cobre, **163 de 163** (200 casos) e **187 de 187** (2000
casos) estão nessas duas estruturas. O gerador atual não contém nenhuma instrução de CSR.

Isso é uma limitação da métrica de linha/branch do Verilator sobre lógica combinacional, não do fuzzer.
A cobertura de linha conta "o bloco foi avaliado", não "o resultado foi usado". Qualquer fuzzer
guiado por essa métrica vai ser recompensado por variar imediatos, e não por exercitar CSRs.

## 5. Pontos inalcançáveis nesta configuração

Dos pontos que nenhum dos dois cobre, 154 não podem ser cobertos por nenhum programa no
`riscof_tb` com a configuração baseline. A classificação foi feita por regras sobre o código-fonte de
cada ponto, conferidas à mão.

| Categoria | Motivo | Pontos |
|---|---|---:|
| Testbench | `sys_reset_i` amarrado em `1'b0` no `riscof_tb` | 48 |
| Testbench | `stall` nunca está ativo na borda do clock com `DELAY_CYCLES=0` (inclui `should_stall` do `mul.sv`) | 16 |
| Parâmetro | braço "habilitado" de ternários de extensões desligadas (Zbkb, Zknh, Zkne, Zicond, V, Xkyber) | 29 |
| Parâmetro | operações de extensões desligadas que o decoder nunca gera (`zbkb_op`, `sha2_op`, `kyber_op`) | 27 |
| Parâmetro | `case` com parâmetro falso (`XKYBEREnable && …`, `AMOEXT inside … && …`) | 10 |
| Parâmetro | braço `INVALID` de MUL/DIV, que é impossível porque M está ligada | 8 |
| Parâmetro | A e V desligadas: o decoder nunca marca `is_amo`/`is_sc`/`is_vector` | 7 |
| Parâmetro | sem XOSVM: `load_access_fault` e `mmu_inst_fault` amarrados em 0 ([RS5.sv:444](rtl/RS5.sv#L444), [RS5.sv:165](rtl/RS5.sv#L165)) | 4 |
| Parâmetro | C desligada: `compressed = 1'b0` ([fetch.sv:456](rtl/fetch.sv#L456)) | 2 |
| Parâmetro | `IQUEUE_SIZE=2`: `BUFFER_SIZE > 1` é sempre verdadeiro | 2 |
| Parâmetro | `single_cycle_i` amarrado em `1'b0` ([execute.sv:438](rtl/execute.sv#L438)) | 1 |
| **Total** | 64 do testbench + 90 de parâmetro | **154** |

Os pontos dependentes de parâmetro aparecem no denominador porque o Verilator instrumenta os dois
braços de todo ternário e de todo `case`, mesmo quando a condição é uma constante de elaboração.
Blocos `generate` desligados, ao contrário, não aparecem (por isso a unidade vetorial não está nos
1240 pontos).

## 6. Cobertura ajustada

| Métrica | Denominador | Baseline | Fuzzer 200 | Fuzzer 2000 | União (B + F2000) |
|---|---:|---:|---:|---:|---:|
| Bruta | 1240 | 67,1% | 75,6% | 78,1% | 82,2% |
| Só pontos alcançáveis | 1086 | 76,6% | 86,4% | 89,2% | 93,8% |
| Sem os artefatos da seção 4 | 911 | 79,1% | 73,5% | 73,7% | 79,1% |
| **Alcançáveis e sem artefatos** | **788** | **91,5%** | **85,0%** | **85,2%** | **91,5%** |

Na última linha, o fuzzer cobre 670–671 pontos, **todos** também cobertos pela baseline (721).

Por módulo, alcançáveis e sem artefatos:

| Módulo | Total | Baseline | Fuzzer 2000 |
|---|---:|---:|---:|
| `CSRBank1` | 286 | 236 (82,5%) | 208 (72,7%) |
| `decoder1` | 181 | 168 (92,8%) | 161 (89,0%) |
| `execute1` | 181 | 178 (98,3%) | 164 (90,6%) |
| `fetch1` | 109 | 108 (99,1%) | 107 (98,2%) |
| `retire1` | 17 | 17 | 17 |
| `mem_access1` | 9 | 9 | 9 |
| `RegFileFF_blk` | 5 | 5 | 5 |

## 7. O que cada lado cobre que o outro não

### Só a baseline (50 pontos, contra o fuzzer de 2000 casos)

É exatamente o que o gerador placeholder não produz:

| Tema | Exemplos |
|---|---|
| **Exceções** | `raise_exception_i` atualizando `mstatus`, `mepc`, `mcause`, `mtval` e o privilégio ([CSRBank.sv:159](rtl/CSRBank.sv#L159), [1125](rtl/CSRBank.sv#L1125), [1187](rtl/CSRBank.sv#L1187), [1345](rtl/CSRBank.sv#L1345)); causas em [execute.sv:1029-1039](rtl/execute.sv#L1029-L1039): ilegal, salto desalinhado, `ecall`, `ebreak`, load/store desalinhado; `ctx_switch` no fetch ([fetch.sv:257](rtl/fetch.sv#L257)); contadores HPM de contexto e exceção |
| **`mret`** | [CSRBank.sv:172](rtl/CSRBank.sv#L172), [1343](rtl/CSRBank.sv#L1343); [execute.sv:947](rtl/execute.sv#L947); decode de `MRET` |
| **Instruções de CSR** | opcode `SYSTEM`; `CSRRW`/`CSRRS` no decode; `read_enable_i` real ([CSRBank.sv:1587](rtl/CSRBank.sv#L1587)); escrita em `mtvec`, `mscratch`, `mepc`; operação `SET` |
| **Fluxo de controle** | `JALR` (decode e alvo em [execute.sv:929](rtl/execute.sv#L929)); `bltu` e `bge` ([execute.sv:937-938](rtl/execute.sv#L937-L938)) |
| **Outros** | `FENCE` (`MISC-MEM`), `EBREAK`, overflow de divisão (`INT_MIN / -1`, [div.sv:169](rtl/div.sv#L169)) |

### Só o fuzzer

Nada além dos artefatos da seção 4.

### Nenhum dos dois, mas alcançável (67 pontos, contra o fuzzer de 2000 casos)

| Tema | Pontos |
|---|---:|
| Escrita de CSR em `mstatus`, `mie`, `mip`, `mcountinhibit`, contadores HPM, `mcycle`/`minstret`, `mcause`, `mtval`; operação `CLEAR` (`csrrc`) | 39 |
| Opcodes ilegais ou não suportados (V, A, Xkyber, `SRET`, `WFI`, `default`) | 12 |
| Interrupções (timer, externa, software; `interrupt_ack_i`) | 11 |
| `ecall` a partir de U-mode | 1 |
| Outros: `mcountinhibit.CY` ligado, `else if (jump)` no execute, `iaddr_hold` no fetch | 4 |

Com 200 casos aparecem mais 23 braços do mux de leitura de CSR não atingidos; com 2000 casos o fuzzer
atinge todos menos 1.

Os 39 pontos de escrita de CSR e os 11 de interrupção são o território dos possíveis desvios de spec
anotados em `pesquisa.md` (MPP sem legalização e escrita em `mip`). **Nenhuma das duas suítes exercita
essas linhas.**

## 8. Cobertura por buckets

### Todos os 1240 pontos

Pares (ponto, bucket) vistos em algum teste, e pontos cujo bucket máximo é ≥ *b*:

| Bucket | Pares baseline | Pares fuzzer 200 | Pares fuzzer 2000 | Só baseline | Só fuzzer 200 | Baseline ≥ b | Fuzzer 200 ≥ b | Fuzzer 2000 ≥ b |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 138 | 287 | 320 | 47 | 196 | 832 | 938 | 969 |
| 2 | 100 | 88 | 119 | 45 | 33 | 800 | 714 | 745 |
| 3 | 91 | 57 | 63 | 48 | 14 | 795 | 692 | 698 |
| 4–7 | 114 | 103 | 107 | 71 | 60 | 786 | 674 | 677 |
| 8–15 | 220 | 166 | 169 | 87 | 33 | 782 | 624 | 624 |
| 16–31 | 122 | 77 | 80 | 90 | 45 | 659 | 493 | 494 |
| 32–127 | 407 | 95 | 98 | 316 | 4 | 641 | 474 | 477 |
| 128+ | 605 | 398 | 399 | 207 | 0 | 605 | 398 | 399 |
| **Total de pares** | **1797** | **1271** | **1355** | | | | | |

União baseline + fuzzer 200: 2182 pares.

### Sem os artefatos da seção 4 (911 pontos)

| Bucket | Pares baseline | Pares fuzzer 200 | Pares fuzzer 2000 | Só baseline | Só fuzzer 200 | Baseline ≥ b | Fuzzer 200 ≥ b | Fuzzer 2000 ≥ b |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 73 | 41 | 42 | 37 | 5 | 721 | 670 | 671 |
| 2 | 67 | 48 | 48 | 33 | 14 | 707 | 652 | 652 |
| 3 | 59 | 37 | 37 | 36 | 14 | 702 | 646 | 646 |
| 4–7 | 89 | 81 | 85 | 56 | 48 | 697 | 634 | 637 |
| 8–15 | 180 | 159 | 159 | 53 | 32 | 695 | 598 | 598 |
| 16–31 | 95 | 73 | 76 | 66 | 44 | 589 | 471 | 472 |
| 32–127 | 352 | 77 | 80 | 279 | 4 | 580 | 453 | 456 |
| 128+ | 560 | 395 | 396 | 165 | 0 | 560 | 395 | 396 |
| **Total de pares** | **1475** | **911** | **923** | | | | | |

União baseline + fuzzer 200: 1636 pares.

### Leitura

- **Bucket máximo:** dos 670 pontos que os dois cobrem (sem artefatos), a baseline chega a um bucket
  maior em **171** e o fuzzer em **0**. Com os artefatos, é 256 contra 8.
- **Na semântica do AFL o fuzzer acrescenta algo:** 161 pares (ponto, bucket) que a baseline não tem
  (1636 − 1475). São pontos que os dois cobrem, mas com contagens intermediárias diferentes: os
  programas curtos do fuzzer atingem certas linhas de 2 a 31 vezes, enquanto os testes da baseline
  atingem 1 vez ou mais de 32. Nenhum desses pares eleva o bucket máximo.
- **O que a contagem significa:** num `always_ff`, a contagem é o número de ciclos; num `always_comb`
  ou `assign`, é o número de avaliações do bloco pelo Verilator. Nos dois casos ela cresce com o
  tamanho do programa. Como os testes da baseline são ~15× mais longos (3946 contra 252 ciclos em
  média), a vantagem nos buckets altos (32–127 e 128+) é em boa parte efeito de duração, não de
  comportamento diferente. Comparar buckets entre suítes com programas de tamanhos muito diferentes
  exige cuidado.
- **Diferença entre o `CoverageMap` e o AFL:** o `merge` do fuzzer só considera novidade quando o
  bucket de um ponto **sobe** acima do máximo já visto. O AFL marca qualquer bucket ainda não visto,
  inclusive um mais baixo. Nesta rodada, o fuzzer viu 241 pares (200 casos, sem artefatos) que estão
  abaixo do máximo do respectivo ponto: o AFL os teria contado como novidade, e o `CoverageMap`, não.
  Para a "interface unificada estilo AFL" planejada em `pesquisa.md`, vale guardar um bitmap de buckets
  por ponto em vez do máximo.

## 9. Problemas encontrados durante o teste

### 9.1 `--work-dir` relativo quebra o fuzzer

Com `--work-dir ./algum_dir`, o caminho do modelo fica relativo (`./algum_dir/obj_dir/Vriscof_tb`),
mas os workers executam o simulador com `cwd` no diretório de cada caso. Resultado: todos os casos
terminam em `error` com `FileNotFoundError`, e a cobertura fica 0/0.

O `make fuzz` padrão passa `--work-dir ./fuzz_work` (`WORK_DIR ?= .`), então é afetado. A causa está em
`toolchain.build_model`, que devolve `os.path.join(work_dir, 'obj_dir', 'Vriscof_tb')` sem `abspath`.
Para este relatório, usei caminhos absolutos. O código não foi corrigido.

### 9.2 Toolchains com binutils ≥ 2.44 quebram o riscv-arch-test

O `arch_test.h` envolve vários `.align` com `.option rvc` para permitir preenchimento com `c.nop`. Com
binutils ≥ 2.44, isso gera `c.nop` (16 bits) no código mesmo com `-march=rv32i`, e o resto do programa
fica desalinhado:

| Módulo | binutils | Instruções de 16 bits em `add-01.elf` |
|---|---|---:|
| `riscv64-elf/12.2.0` a `14.2.0` | 2.40–2.43 | 0 |
| `riscv64-elf/15.1.0`, `15.2.0`, `15.2.0-gaph`, `16.1.0-gaph` | 2.44–2.46 | 6 |

Com a 15.2.0-gaph, **47 dos 62 testes deram timeout no RS5**, e o Sail entrou em laço de trap infinito
(instrução ilegal → `mtvec = 0` fora da memória → falha de acesso → …), escrevendo ~1,9 GB de log por
teste em poucos minutos. O plugin `sail_cSim` roda o Sail **sem `--inst-limit`**, então nada interrompe
esse laço. A rodada foi abortada e refeita com a GCC 14.2.0.

No fuzzer, o `c.nop` também aparece, mas cai depois do laço final `j write_tohost` e nunca é executado.
Os resultados do fuzzer foram idênticos com as duas toolchains.

### 9.3 Ambiente

O shell não interativo não carrega o ambiente. O que funcionou:

```sh
source /usr/share/Modules/init/zsh
module load verilator/5.052 riscv64-elf/14.2.0 sail-riscv/0.7
export PATH=/opt/rh/gcc-toolset-14/root/usr/bin:$HOME/.local/riscof/bin:$PATH   # g++ 14 (C++20) e riscof
```

O `g++` do sistema é a versão 8.5, sem C++20. O `riscv_sim_rv32d` só existe no `sail-riscv/0.7`
(as versões 0.8 e 0.12 instalam `sail_riscv_sim`).

## 10. O que isso significa para o fuzzer

1. **A métrica atual recompensa o comportamento errado.** 26,5% dos pontos são artefatos de avaliação
   combinacional. Antes de usar essa cobertura como feedback (ou como resultado no TCC), é preciso
   excluir essas estruturas ou medir de outra forma: por exemplo, `cover property` condicionadas a
   `read_enable_i` ou ao opcode, ou a cobertura de arestas do pipeline descrita em `pesquisa.md`.
2. **O denominador precisa ser o alcançável.** Reportar 67,1% para a baseline esconde que ela cobre
   91,5% do que é alcançável e não é artefato. A lista da seção 5 pode virar um arquivo de exclusões.
3. **O gerador precisa produzir o que só a baseline cobre:** traps, `mret`, instruções de CSR, `JALR`,
   `FENCE`, `bltu`/`bge` e casos de borda de divisão. Para passar da baseline, precisa produzir o que
   nenhuma das duas cobre: escrita de CSR em `mstatus`/`mie`/`mip`/contadores, interrupções, opcodes
   ilegais e U-mode. São os mesmos operadores `trap`/`csr` e o trap handler do roteiro em `pesquisa.md`.
4. **Os buckets só são comparáveis entre programas de tamanho parecido.** E o `merge` deveria seguir a
   semântica de bitmap do AFL (seção 8).

## 11. Reprodução e dados

Com o ambiente da seção 9.3, a partir de `riscof/`:

```sh
export TRIPLET=riscv64-elf BRANCHPRED=1 FORWARDING=1 DUALPORT_MEM=1 IQUEUE_SIZE=2 DELAY_CYCLES=0

# baseline com cobertura (o mesmo que "make coverage-baseline", sem o "riscof arch-test --update")
RS5_COVERAGE=1 riscof run --suite=riscv-arch-test/riscv-test-suite --env=riscv-arch-test/riscv-test-suite/env \
    --work-dir=./report_baseline_cov_gcc14_work --no-browser --config=baseline.ini

# fuzzer (work-dir ABSOLUTO; --keep preserva o coverage.dat de cada caso em /dev/shm)
python3 fuzzer/fuzz.py --isa-yaml rs5/baseline.yaml --work-dir $PWD/report_fuzz200_gcc14_work  -n 200  --seed 1 --keep
python3 fuzzer/fuzz.py --isa-yaml rs5/baseline.yaml --work-dir $PWD/report_fuzz2000_gcc14_work -n 2000 --seed 1 --keep
```

| Dado | Local |
|---|---|
| Baseline (62 testes, `coverage.dat` por teste) | `riscof/report_baseline_cov_gcc14_work/` |
| Fuzzer: modelo e `uncovered.txt` | `riscof/report_fuzz200_gcc14_work/`, `riscof/report_fuzz2000_gcc14_work/` |
| Fuzzer: `coverage.dat` por caso | `/dev/shm/rs5fuzz-pedro.fam-1321350/` (200) e `/dev/shm/rs5fuzz-pedro.fam-1323994/` (2000); tmpfs, somem no reboot |

Os scripts de análise (`cov_compare.py`, que lê os `coverage.dat` sem filtro, e `classify.py`, com as
regras da seção 5) ficaram fora do repositório, no scratchpad da sessão. Podem ser adicionados em
`riscof/fuzzer/` quando for a hora de mexer no código.

Diretórios de uma primeira tentativa, **inválidos** (toolchain 15.2.0-gaph, seção 9.2) e que podem ser
apagados: `riscof/report_baseline_cov_work/`, `riscof/report_fuzz_work/`, `riscof/report_fuzz2000_work/`.
