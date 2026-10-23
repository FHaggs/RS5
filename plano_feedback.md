# Plano: fuzzer com fontes de feedback ligáveis e experimentos iniciais

> **Atualização 2026-10-22.** A infraestrutura das etapas E1 e E2 (sondas, modelos das fontes,
> fontes ligáveis, relatório) está implementada no branch `cov-tests`, e os experimentos X0 a X5
> foram rodados com um gerador simples. Resultados e decisões em [relatorio_fontes.md](relatorio_fontes.md):
> a fonte 0 não satura em 8000 casos, a fonte 2 satura em ~250, mutar é pior que gerar do zero com
> o gerador atual, as sondas custam 5% e a cobertura do Verilator 3×. A seção 10 do relatório lista
> o que muda neste plano.

Complementa `pesquisa.md` (seções 6 e 7), `fontes.md`, `bins.md` e `report.md`. Este documento diz
**o que construir, em que ordem, por quê, e quais experimentos rodar antes de gastar tempo na ablação**.

Decisões já tomadas e que este plano assume:

- **Infra:** o fuzzer do branch `cov-tests` (`riscof/fuzzer/`: `fuzz.py`, `runner.py`, `generator.py`,
  `program.py`, `coverage.py`, `toolchain.py`) e o `riscof_tb.sv`. Sem Encarsia e sem Yosys.
- **Bugs para avaliação:** mutantes manuais no fonte do RS5 e fixes revertidos do histórico. Um bug por
  build.
- **Mecanismo de feedback:** mapa segmentado estilo AFL (uma região por fonte, buckets, virgin bits),
  com três ajustes: arestas com duração, energia por raridade e modo dirigido para bins em zero.
- **Fontes neste plano:** 0 (pipeline), 1 (CSR com contexto), 2 (Verilator sem artefatos), 6 (bins).
  As fontes 1 e 2 entram como **linhas de base** (o Encarsia mostra que não ajudam; queremos ver se o
  resultado se repete). As fontes 4 e 5 (HPM e traps lidos pelo programa) ficam fora do escopo por ora.
- **Configuração fixa:** `RV32IMU_Zicsr_Zihpm`, `BRANCHPRED=1`, `FORWARDING=1`, `IQUEUE_SIZE=2`,
  `DELAY_CYCLES=0`.

---

## 1. Hipótese e o que o plano precisa responder

> Feedback **funcional de microarquitetura** (hazards, forwarding, hold, rollback, traps no contexto do
> pipeline) encontra mais bugs, ou os encontra antes, do que feedback estrutural (linha do Verilator,
> transições de CSR) e do que nenhum feedback, com o mesmo gerador e o mesmo orçamento.

Para responder isso de forma honesta, o fuzzer precisa:

1. gravar **todas** as fontes em todo caso, mas decidir (guardar no corpus, dar energia) com um
   **subconjunto escolhido por linha de comando**;
2. ter um modo **sem feedback** que seja o mesmo fuzzer com a decisão desligada, não outro programa;
3. medir o custo de cada fonte, porque o orçamento é em tempo de relógio.

---

## 2. Arquitetura do feedback

### 2.1 Um caso, do início ao fim

```
generator.generate() ─▶ .S ─▶ gcc/objcopy ─▶ Vriscof_tb +SIG_* +FUZZ_COV=0,1,6 ─▶ dut.sig, fuzz_cov.txt[, coverage.dat]
                                           └▶ sail ──────────────────────────▶ ref.sig
runner: compara assinaturas (oráculo) ─▶ Result{status, hits{(fonte, idx): count}, stats{nome: valor}}
fuzz.py: CoverageMap.merge(hits) por região ─▶ novidade/raridade só nas fontes de --guide ─▶ corpus, energia
```

Três momentos, como na seção 6.2 do `pesquisa.md`: **registrar** (cada fonte no seu ritmo), **capturar**
(arrays no RTL, `coverage.dat`), **consumir** (só no fim do caso, no Python).

### 2.2 Regiões do mapa

| Fonte | Conteúdo | Chave | Índices | Registra em |
|---|---|---|---|---|
| 0 | arestas do pipeline **com duração** | `mix(A) >> 1 ^ mix(B) ^ bucket(ciclos em A) << 12` | 64K | `bind` em `RS5` |
| 1 | transições de trap/CSR com contexto | `hash(antes, depois, evento, contexto)` | 4K | `bind` em `CSRBank`/`execute` |
| 2 | linha/branch/cover do Verilator, filtrado pelas exclusões do `report.md` | número do ponto | ~900 | Verilator |
| 6 | bins nomeados (`bins.md`) | índice fixo | 256 | `bind` |
| 7 | **escalares** para o modo dirigido (não é bucket; guarda o máximo/mínimo) | índice fixo | 32 | `bind` |

A fonte 0 muda em relação ao `fontes.md` 1.4: a aresta só é registrada **quando o snapshot muda**, e
leva no XOR o bucket de quantos ciclos o pipeline ficou no snapshot anterior. Por quê: com um snapshot
por ciclo, `hold → hold` por 32 ciclos vira 32 incrementos na mesma posição e domina todos os buckets; com
duração, vira uma aresta `hold(32–127) → X`, que é o que interessa ("hold longo seguido de trap" e
"hold curto seguido de trap" são arestas diferentes).

A fonte 7 não é cobertura. É um vetor de "melhor valor visto no caso" (ocupação máxima da fila,
maior sequência de hazards, menor distância entre `interrupt_ack` e `mret`...). Serve ao modo dirigido
(2.4).

### 2.3 Lado RTL: um módulo por região, ligado por `bind`

Arquivo novo: `riscof/fuzz_cov.sv`. O RTL do RS5 não é editado.

```systemverilog
// Contadores de uma região. Uma instância por fonte.
module fuzz_cov_part #(parameter int SRC = 0, parameter int BITS = 16)
    (input logic clk, input logic en, input logic [63:0] key);
    byte unsigned counts [2**BITS];
    logic enabled;                                   // +FUZZ_COV=0,1,6 (padrão: todas)
    wire [BITS-1:0] idx = fold(key);                 // mistura (ex.: xorshift-multiply) + dobra
    always_ff @(posedge clk)
        if (enabled && en && counts[idx] != 8'hFF) counts[idx] <= counts[idx] + 1;
    final if (enabled) dump(SRC, counts);            // "SRC idx count" só para count != 0
endmodule

// Sonda do pipeline (fonte 0 + bins + escalares)
module pipe_probe import RS5_pkg::*; (input logic clk, reset_n, /* sinais da tabela 1.4 de fontes.md */);
    logic [33:0] snap, snap_prev;  logic [15:0] run;      // run = ciclos no snapshot atual
    assign snap = { ... };
    always_ff @(posedge clk) begin
        if (snap != snap_prev) begin run <= 1; snap_prev <= snap; end
        else if (run != '1)     run <= run + 1;
    end
    wire edge_now = reset_n && (snap != snap_prev);
    fuzz_cov_part #(.SRC(0)) p0 (.clk, .en(edge_now),
        .key((mix(snap_prev) >> 1) ^ mix(snap) ^ (64'(bucket(run)) << 12)));
    // bins (fonte 6) e escalares (fonte 7) no mesmo módulo: veem os mesmos fios
endmodule

bind RS5 pipe_probe probe (.clk(clk), .reset_n(reset_n), .decode_ctrl, .ctrl_execute, ...);
```

Sinais internos de submódulos (`decoder1.hazard_rs1`, `execute1.hold_div`, `CSRBank1.mstatus_mie`)
entram por referência hierárquica. **Risco:** o Verilator pode recusar referência hierárquica para dentro
de irmãos a partir de um módulo `bind`. Alternativas, em ordem: caminho absoluto
(`riscof_tb.dut.decoder1.hazard_rs1`), ou `/*verilator public*/` nos sinais, ou `--public-flat-rw`.
Isso é o item 1 do experimento X0.

Ligar e desligar **a gravação** no RTL: plusarg `+FUZZ_COV=0,1,6`. Serve para medir o custo de cada
sonda (X2) e para a ablação rodar só com a instrumentação que a variante usa. O Verilator (fonte 2)
é um build à parte (`RS5_COVERAGE=1`), então variantes com e sem fonte 2 usam `obj_dir` diferentes.

### 2.4 Lado Python: `--record`, `--guide`, escalonamento

Mudanças em `coverage.py` e `fuzz.py`:

```python
@dataclass
class FeedbackConfig:
    record:   set[int]            # fontes gravadas e salvas no result.json (padrão: todas)
    guide:    set[int]            # fontes que decidem corpus e energia ({} = sem feedback)
    weight:   dict[int, float]    # peso por fonte na energia (padrão 1)
    schedule: str                 # 'novelty' | 'rarity' | 'rarity+directed'
    targets:  list[str]           # bins/escalares alvo do modo dirigido
    map_bits: dict[int, int]      # tamanho de cada região
```

- `CoverageMap`: chave `(fonte, idx)`, **bitmap de buckets por posição** (virgin map do AFL), e um
  contador de **quantos casos do corpus** atingiram cada posição (para raridade). O `merge` atual guarda
  só o bucket máximo e precisa mudar.
- **Novidade** (admissão no corpus): posição ou bucket inédito em qualquer fonte de `guide`.
- **Raridade** (energia): `energia(caso) = Σ peso[f] / freq[(f, idx)]` sobre as posições que o caso
  atinge. Pais são sorteados com probabilidade proporcional à energia. É o power schedule de arestas
  raras do AFL++ e continua funcionando depois que a novidade satura.
- **Dirigido**: quando a novidade fica plana (N casos sem novidade), o fuzzer escolhe um alvo da lista
  (bin em zero ou escalar) e passa a admitir no corpus casos que **melhoram o escalar** associado, com
  energia máxima para o melhor. Troca de alvo depois de M casos sem melhora ou quando o bin acerta.
- **Calibração**: todo caso admitido roda duas vezes; posições que mudam entre as execuções são marcadas
  instáveis naquela região e ignoradas (importa para a fonte 0 se houver qualquer não determinismo).
- **Sem feedback** (`--guide ""`): tudo é gravado, nada decide; o gerador roda com a mesma semente e a
  mesma distribuição de operadores. O corpus não cresce, logo não há pais nem energia.

Linha de comando (exemplos):

```sh
# controle: grava tudo, não guia
python3 fuzzer/fuzz.py --guide "" --seed 1 -n 0 --budget 6h --work-dir $PWD/abl_none_s1
# só arestas de pipeline, raridade
python3 fuzzer/fuzz.py --guide 0 --schedule rarity --seed 1 --budget 6h ...
# pipeline + bins, dirigido para bins em zero
python3 fuzzer/fuzz.py --guide 0,6 --schedule rarity+directed --targets fuzzer/targets.txt ...
# estilo ProcessorFuzz (linha de base)
python3 fuzzer/fuzz.py --guide 1 --schedule novelty ...
```

Cada variante da ablação é um arquivo em `riscof/fuzzer/experiments/*.ini` com esses campos, para que o
script de ablação (seção 5) não dependa de ninguém lembrar as flags.

### 2.5 O que **não** muda entre variantes

- `generator.py`: mesma gramática, operadores e distribuição. Qualquer mudança no gerador invalida a
  comparação (Insight 3 do Encarsia).
- Oráculo: comparação de assinatura com o Sail, mais `mcause`/`mepc` dos traps de exceção (ver 3.1).
- Testbench, configuração do core, toolchain, `--max-cycles`.

---

## 3. Etapas de construção

### E0. Pré-requisitos do gerador (antes de qualquer feedback)

Sem isto, as fontes 1 e 6 medem zero e o experimento não tem o que comparar:

1. **Tratador de trap** no template (`program.py`): grava `mcause`, `mepc`, `mstatus` num `trap_buf`
   dentro da assinatura (só exceções; interrupções fora, pelo timing), pula a instrução (`mepc += 4`)
   e retorna com `mret`. Registradores reservados: `x28`, `x29`.
2. **Operadores mínimos** no gerador: `csr` (matriz de `bins.md` 3.3), `trap` (ilegal, `ecall`,
   `ebreak`, desalinhado), `umode` (entrar em U via `mret` e voltar por `ecall`), `div`/`mul` com
   bordas, `branch` com padrões tomado/não tomado, `irq` (programar o timer).
3. **Congelar** o gerador. A partir daqui, só correção de bugs no gerador, nunca ampliação, até a ablação
   terminar.

Entregável: 200 casos do gerador novo sem `hang`, e as 67 posições alcançáveis do `report.md` seção 7
reduzidas (medida: quantas continuam em zero).

### E1. Sondas e mapa

1. `riscof/fuzz_cov.sv` com `fuzz_cov_part`, `pipe_probe` (fonte 0, 6, 7) e `csr_probe` (fonte 1).
2. Incluir o `bind` no comando do Verilator (`rs5_verilator.py`), despejo em `fuzz_cov.txt` no diretório
   do caso, plusarg `+FUZZ_COV`.
3. `runner.py`: ler `fuzz_cov.txt` e devolver `hits` e `stats` no `Result`.
4. `coverage.py`: `CoverageMap` com regiões, bitmap de buckets, contagem de frequência; relatório que
   traduz índice em chave original (a sonda grava, para cada índice, o primeiro snapshot que o produziu,
   num segundo arquivo `fuzz_cov_keys.txt`).
5. Fonte 2: aplicar o arquivo de exclusões (artefatos e inalcançáveis da seção 4 e 5 do `report.md`).

Entregável: X0, X2, X3, X5 rodados (seção 4).

### E2. Escalonamento e modos

1. `FeedbackConfig`, `--guide`, `--record`, `--schedule`, `--budget` (tempo de relógio).
2. Energia por raridade e sorteio de pais.
3. Modo dirigido com `targets.txt` (nome do bin ou escalar, e se é max ou min).
4. Calibração de instabilidade.
5. `experiments/*.ini` e `fuzzer/ablation.py` (seção 5).

Entregável: X1, X4, X6 rodados.

### E3. Corpus de bugs

1. `riscof/bugs/<nome>.patch` com um mutante cada, em `decode.sv`, `execute.sv`, `CSRBank.sv`,
   `fetch.sv`. Categorias, seguindo a taxonomia do Encarsia (troca de sinal, condicional quebrada):

   | Categoria | Exemplos de mutante |
   |---|---|
   | forwarding | prioridade trocada (memória antes de execute); `rd_we` ignorado numa fonte |
   | hazard | `use_rs1` removido da condição; `locked` por 1 ciclo em vez de 2; `hazard_mem` removido |
   | previsor/rollback | `jump_rollback` não limpa a fila em um caso; `bp_taken` propagado errado |
   | hold/multiciclo | `hold` não congela o decode num ciclo; resultado do `div` amostrado 1 ciclo cedo |
   | trap/CSR | `mepc` salvo com PC+4; `MPIE` não restaurado no `mret`; CSR RO aceita escrita; `csrrs` rs1=x0 escreve |
   | privilégio | `csr_address[9:8] > privilege` com `>=`; `mret` em U aceito |
   | fetch/fila | `almost_full` com limite errado; `valid` sem `!jumping` |

   Mais os fixes revertidos do histórico (`#56 bugfix`, Xkyber) e os desvios da seção 8 do
   `pesquisa.md`, se confirmados.
2. Para cada mutante, um teste dirigido escrito à mão que o **dispara e detecta** (prova que o bug é
   observável e que o oráculo o vê). Mutante sem teste que o detecte é equivalente ou invisível e sai
   do corpus. Isso substitui a prova formal do Encarsia.
3. `fuzzer/bugs.py`: aplica o patch numa cópia do RTL, builda um `obj_dir` por mutante (uma vez), e
   expõe `--rtl-dir` ao `fuzz.py`.

Entregável: 10 a 20 mutantes com teste de detecção, metade em cada categoria do Encarsia.

### E4. Ablação

Seção 5.

---

## 4. Experimentos iniciais

Cada um existe para tomar uma decisão específica. Rodar nesta ordem; os cinco primeiros cabem em
poucos dias de máquina.

### X0. A sonda funciona e mede o que diz medir

- **Como:** 5 a 8 programas escritos à mão, um por situação: load-uso com bolha, dependência resolvida
  por forwarding do execute/memória/retire, `div` em hold seguido de interrupção, branch previsto e
  desfeito, `ecall` em U, fila do fetch cheia. Rodar com `+FUZZ_COV=0,1,6` e olhar `fuzz_cov_keys.txt`.
- **Decide:** se as referências hierárquicas do `bind` compilam no Verilator (ou qual alternativa usar);
  se o forwarding recalculado bate com o que o `decode.sv` faz; se a aresta esperada aparece com o
  bucket de duração esperado.
- **Critério:** cada programa produz a aresta e o bin previstos, e nada mais que o esperado.

### X1. Curva de saturação por fonte (sem guia)

- **Como:** `--guide ""`, 2000 casos do gerador congelado, 3 sementes. Para cada fonte, plotar
  posições distintas acumuladas × número de casos, e novidades por caso em janelas de 100.
- **Por quê:** é a pergunta central do feedback. Se a fonte 0 satura antes de ~500 casos, a novidade não
  tem o que guiar e o valor migra para raridade e modo dirigido. Se ainda cresce em 2000, o mapa de
  novidade sozinho já faz trabalho.
- **Decide:** `--schedule` padrão (`novelty` vs `rarity` vs `rarity+directed`), o limiar N de "novidade
  plana" do modo dirigido, e quanto peso dar aos bins.
- **Também mostra:** quanto cada fonte "enxerga" do que a outra enxerga (posições da fonte 0 que
  aparecem só em casos que também trazem novidade na fonte 2, e vice-versa). Isso antecipa se as
  fontes são redundantes.

### X2. Custo das sondas

- **Como:** o mesmo conjunto de 200 casos, 4 builds: sem nada; `+FUZZ_COV=0`; `+FUZZ_COV=0,1,6`;
  `RS5_COVERAGE=1`. Tempo de simulação por caso, mediana e p95.
- **Por quê:** o orçamento da ablação é em tempo de relógio (Insight 4 do Encarsia: feedback que
  custa caro perde no tempo). Uma sonda que dobra o tempo de simulação precisa achar bugs 2x mais
  rápido só para empatar.
- **Decide:** se as sondas ficam ligadas em todas as variantes (custo desprezível, comparação mais
  simples) ou só na variante que as usa (custo real).

### X3. Estabilidade

- **Como:** 100 casos rodados 2 vezes cada; comparar `hits` por região.
- **Por quê:** a regra de novidade quebra se posições aparecem e somem sem o programa mudar.
- **Decide:** se a calibração é necessária e quais regiões precisam dela. Esperado: zero instabilidade
  com `DELAY_CYCLES=0`; se houver, é bug na sonda (por exemplo, registrar antes do reset).

### X4. Taxa de acerto dos bins no corpus

- **Como:** os 2000 casos de X1; para cada bin, fração de casos que o acerta.
- **Decide:** bins com acerto > 90% saem (não discriminam); 1% a 90% ficam como feedback; 0% e
  alcançáveis viram `targets.txt` do modo dirigido; 0% e inalcançáveis vão para o arquivo de exclusões.
  Para cada alvo, definir o escalar da fonte 7 que mede "distância" (ocupação máxima da fila, etc.).

### X5. Colisões e tamanho do mapa

- **Como:** nos 2000 casos, contar chaves distintas (`fuzz_cov_keys.txt`) e índices distintos por região.
- **Decide:** `map_bits` por região e se a função de mistura é boa o bastante. Meta: menos de 1% de
  colisões na fonte 0.

### X6. Oráculo e mutantes: alcançado, disparado, detectado

- **Como:** para 3 a 5 mutantes do E3, rodar os 2000 casos de X1 em cada build mutante. Para cada
  caso, classificar: **alcançou** (a sonda viu o estado do mutante: bin ou aresta associada ao local do
  mutante), **disparou** (o teste dirigido do mutante confirma que a condição ocorreu), **detectou**
  (`mismatch` contra o Sail).
- **Por quê:** separa falha do gerador (não alcança), falha do feedback (alcança mas não insiste) e
  falha do oráculo (dispara e a assinatura não mostra). Recomendação do SoK de 2026 citada no
  `pesquisa.md`.
- **Decide:** se o oráculo precisa de mais do que a assinatura final (por exemplo, `trap_buf` na
  assinatura, ou comparar CSRs no fim) antes da ablação. Bugs "disparados e não detectados" invalidam
  qualquer conclusão sobre feedback.

### X7. Pequena ablação piloto

- **Como:** 2 mutantes fáceis, 3 variantes (`none`, `0`, `2`), 3 sementes, 1 hora cada.
- **Por quê:** validar o `ablation.py`, o formato de resultados e a análise estatística com dados
  pequenos antes de gastar dias de máquina.

---

## 5. Ablação

### 5.1 Variantes

| Variante (`experiments/*.ini`) | `guide` | `schedule` | Build | Papel |
|---|---|---|---|---|
| `none` | `""` | — | sem sondas | controle (resultado do Encarsia) |
| `verilator` | `2` | novelty | `RS5_COVERAGE=1` | estrutural, feedback atual |
| `csr` | `1` | novelty | sondas | estilo ProcessorFuzz |
| `pipe` | `0` | novelty | sondas | funcional, só novidade |
| `pipe_rarity` | `0` | rarity | sondas | funcional + energia |
| `pipe_bins` | `0,6` | rarity+directed | sondas | funcional + alvos |
| `all` | `0,1,2,6` | rarity+directed | ambos | tudo ligado |

A variante estilo DifuzzRTL (registradores de controle por módulo) fica como opcional: exige análise
estática dos muxes; se entrar, é uma aproximação documentada (registradores escolhidos por nome).

### 5.2 Desenho

- **Unidade:** (variante, mutante, semente). Orçamento igual em **tempo de relógio** por unidade
  (ponto de partida: 2 h; ajustar após X2 e X7). Sementes: 5 por unidade no mínimo.
- **Sem interrupção ao detectar:** a campanha continua até o orçamento, e registra o tempo da primeira
  detecção e o número de casos que detectam (medida de dificuldade, como no Encarsia).
- **Falsos positivos:** todo `mismatch` é reexecutado no build sem mutante; se também falha lá, é bug
  natural ou do gerador e é descartado (como o filtro do Encarsia).
- **Métricas:** por unidade, tempo até a primeira detecção (censurado no orçamento); por variante,
  fração de mutantes detectados e curva de sobrevivência agregada. Teste: log-rank ou Mann-Whitney
  sobre tempos, com correção para múltiplas comparações; efeito reportado com intervalo, não só p.
- **Classificação por bug:** para cada mutante, qual variante o encontra, e se foi alcançado,
  disparado ou detectado (X6). Isso responde "qual fonte acha qual classe de bug".

### 5.3 `fuzzer/ablation.py`

```
ablation.py --variants experiments/ --bugs bugs/ --seeds 1..5 --budget 2h --jobs 32 --out abl/
```

- Prepara um `obj_dir` por (mutante, build) uma vez.
- Agenda as unidades num pool, cada uma com `fuzz.py` em subprocesso e `--work-dir` próprio.
- Salva `abl/<variante>/<mutante>/<semente>/summary.json` (primeira detecção, casos, novidades por
  fonte, tempos).
- `ablation_report.py` gera as tabelas e curvas.

---

## 6. Decisões em aberto e riscos

| Item | Risco | Quando decide | Mitigação |
|---|---|---|---|
| Referência hierárquica no `bind` | Verilator recusa | X0 | caminho absoluto, `public`, `--public-flat-rw` |
| Saturação precoce da fonte 0 | feedback sem efeito | X1 | raridade e dirigido; n-gramas de 3 snapshots só sobre bits de evento |
| Custo das sondas | perde no tempo de relógio | X2 | sondas só na variante que usa; contadores menores |
| Mutantes equivalentes ou invisíveis | corpus de bugs inflado | E3 | exigir teste dirigido que detecte |
| Oráculo fraco (estado final) | bug disparado e não visto | X6 | `trap_buf` na assinatura; comparar CSRs no fim |
| Gerador muda durante o estudo | comparação contaminada | E0 | congelar e versionar (`generator.py` com hash no `summary.json`) |
| Poucos mutantes | sem poder estatístico | E3 | mínimo 10; sementes compensam em parte |

---

## 7. Ordem e estimativa

| Semana | Etapa | Saída |
|---|---|---|
| 1–2 | E0 | gerador com tratador e operadores; congelado |
| 3 | E1 | sondas, `fuzz_cov.txt`, `CoverageMap` novo; X0, X3, X5 |
| 4 | X1, X2, X4 | curvas de saturação, custo, lista de alvos; decisão do `schedule` |
| 5 | E2 | `--guide`, raridade, dirigido, `experiments/*.ini` |
| 6 | E3 | 10 a 20 mutantes com teste de detecção; X6 |
| 7 | X7 | piloto; ajuste de orçamento e análise |
| 8–9 | E4 | ablação completa |
| 10 | relatório | tabelas por variante e por bug; resposta à hipótese |

Os resultados de X1 e X6 já são conteúdo do TCC mesmo que a ablação completa não caiba: "quanto cada
fonte enxerga" e "quantos bugs disparados o oráculo deixa passar" são achados por si.
