# Bins de cobertura para o fuzzer do RS5

Complementa a seção 6 do `pesquisa.md` (feedback) e a seção 10 do `report.md`. Propõe uma **fonte 6**: bins nomeados, com índice fixo.

## 1. O que é um bin

Um **bin** é uma "caixinha" com nome que responde a uma pergunta do tipo *"isto aconteceu?"* (ou *"quantas vezes?"*). Cada bin tem:

- uma **condição** (um predicado sobre sinais do RTL ou sobre o programa);
- um **contador** (quantas vezes a condição foi verdadeira num caso);
- um **índice fixo** no mapa de feedback.

Exemplo: o bin `fetch_fila_cheia` conta os ciclos em que a fila de instruções do fetch estava cheia.

### Diferença para as arestas da fonte 0

| | Aresta (fonte 0) | Bin (fonte 6) |
|---|---|---|
| Pergunta | "o pipeline passou do estado A para o B?" | "esta situação específica ocorreu?" |
| Índice | `hash(A) >> 1 ^ hash(B)`; surge sozinho | fixo, escolhido por quem escreve o bin |
| Quem decide o que importa | o hash; qualquer estado novo vale | uma pessoa, por conhecer o design |
| Volume típico | centenas por caso | dezenas, quase todas em zero |
| Parecido com | arestas do AFL | `covergroup`/`coverpoint` do SystemVerilog |

Os bins são as **esquinas conhecidas**. As arestas exploram o desconhecido. Um bin que nunca foi acertado é, ao mesmo tempo, um relatório ("ninguém cobre isto") e um alvo para o fuzzer.

### Bin simples e bin cruzado

- **Simples:** uma condição. Ex.: `interrupt_ack`.
- **Cruzado (cross):** o produto de duas ou mais dimensões, com um bin por combinação. Ex.: `(classe da instrução em execute) × (interrupção aceita)`. Quase todo o valor está nos cruzamentos: o evento sozinho costuma ser acertado por qualquer programa, e a combinação é que é rara.

## 2. Como colocar

### 2.1 Princípios

1. **Fonte própria, sem hash.** Região 6 do mapa lógico, com um índice por bin, como a fonte 2. Os bins não disputam posição com as arestas da fonte 0.
2. **Qualificar com evento real.** O `report.md` (seção 4) mostrou que 26,5% dos pontos do Verilator eram artefatos de avaliação combinacional. Todo bin precisa de uma condição de qualificação, por exemplo `valid && !stall && !killed`. Sem isso, o bin conta instruções especulativas, anuladas ou paradas.
3. **Contar por ciclo qualificado, não por ciclo de clock.** Os buckets do AFL (1, 2, 3, 4–7, 8–15…) continuam valendo. Para bins de evento, o bucket "1" já é a informação principal.
4. **Registrar tudo, decidir com um subconjunto** (6.1 do `pesquisa.md`): `--guide 0,1,6` escolhe quais fontes decidem a entrada no corpus. Bins nunca acertados entram no relatório de qualquer jeito.
5. **Peso alto para bin inédito.** Um bin raro novo vale mais que uma aresta nova na hora de escolher onde gastar mutações (6.7).

### 2.2 Onde calcular: RTL ou Python

| Tipo de bin | Onde | Por quê |
|---|---|---|
| Microarquitetural (fila cheia, hazard, rollback, hold) | RTL, módulo `bind` | só o RTL enxerga esses sinais |
| ISA (CSR × privilégio, `mret` em U, rs1=x0) | Python, a partir do trace do Sail ou do gerador | é propriedade do fluxo de instruções; não precisa de simulador |
| Interrupção/exceção × estado do pipeline | RTL, `bind` no CSRBank/execute | precisa do contexto do pipeline no mesmo ciclo |

Quando um bin ISA aparece nos dois lados (Sail diz que a instrução existe; o RTL diz que ela retirou), compará-los é um teste a mais: divergência entre "o programa tem `csrrs x1, mstatus, x0`" e "o RTL viu um `csrrs` com rs1=x0 retirar" já indica problema de fetch/decode.

### 2.3 Esqueleto do `bind` (RTL)

```systemverilog
module rs5_cov_bins (
    input logic        clk, reset_n,
    // sinais amarrados por bind a fetch/decode/execute/CSRBank
    input logic        valid_retire,       // valid && !stall && !killed
    input logic        buffer_not_full,
    input logic        jump_rollback,
    input logic        bp_taken_r,
    input logic        valid_fetch,
    input logic        hazard_rs1, use_rs1,
    input logic        locked_rs1,
    input logic [3:0]  cls_next,           // classe da instrução seguinte
    input logic        interrupt_ack
    // ...
);
    localparam int NBINS = 64;
    logic [7:0] cnt [NBINS];               // saturam em 255

    task automatic hit(input int id);
        if (cnt[id] != 8'hFF) cnt[id] <= cnt[id] + 1;
    endtask

    always_ff @(posedge clk) if (reset_n) begin
        if (!buffer_not_full)                         hit(0);   // fetch_fila_cheia
        if (jump_rollback && !valid_fetch)            hit(1);   // rollback_sem_valido
        if (locked_rs1 && !use_rs1)                   hit(2);   // lock_sem_uso_rs1
        if (interrupt_ack)                            hit(3 + cls_next);  // cross
    end

    final $fwrite(fd, "bin %0d %0d\n", ...);    // escreve em fuzz_cov.txt
endmodule
```

O formato de saída é o mesmo de `fuzz_cov.txt` da fonte 0/1, com uma região a mais.

### 2.4 Escolher e validar um bin

Antes de incluir, meça a taxa de acerto no corpus atual (os 2000 casos):

| Taxa de acerto | Veredito |
|---|---|
| > 90% | inútil: todo programa acerta; não discrimina |
| 1% a 90% | bom sinal de feedback |
| 0% e alcançável | precisa de operador/gerador dirigido; é a lista de trabalho |
| 0% e inalcançável | vai para o arquivo de exclusões |

As 67 posições "alcançáveis e não cobertas" do `report.md` (seção 7) são uma fonte natural de candidatos.

**Cuidado com circularidade:** escolher bins olhando os bugs que depois se diz ter achado enviesa a avaliação. Escolha pelo critério acima e avalie com mutações ou com os desvios da seção 8 do `pesquisa.md`.

## 3. Bins interessantes

Os trechos citados foram conferidos em `rtl/` neste checkout. As linhas podem mudar.

### 3.1 Microarquitetura (RTL, `bind`)

| Bin | Condição (qualificada) | Por que importa | Onde |
|---|---|---|---|
| `fetch_fila_cheia` | `!buffer_not_full` | backpressure; recuperação do fetch | `fetch.sv:219` |
| `fetch_fila_cheia × jump` | fila cheia no mesmo ciclo de `jumped` ou `jump_rollback_i` | desvio com a fila no limite | `fetch.sv:217-219` |
| `rollback_sem_valido` | `jump_rollback_i && bp_taken_r && !valid_fetch` | corrida rara entre previsão desfeita e fetch sem dado | `fetch.sv:240, 362` |
| `lock_sem_uso` | `locked_rs1 && !use_rs1` (e o análogo para rs2) | exercita o `&& use_rs*`; um `use_rs*` errado esconde ou cria hazard | `decode.sv:656` |
| `hazard × classe` | `hazard_rs1` cruzado com a classe da instrução que o sofre (ALU, branch, load, store, CSR, mul, div, jalr) | cada classe que lê rs1 precisa ter visto o hazard disparar | `decode.sv:654-660` |
| `hazard_mem` | `locked_memory && is_load` | load logo após load/store à mesma região | `decode.sv:650` |
| `fwd × hold` | origem do forwarding de rs1/rs2 enquanto `hold_mul/div` | forwarding com o operando ainda em cálculo | `decode.sv:686-708`, `execute.sv:350` |
| `hold_dois_seguidos` | `hold_mul` terminando e `hold_div` iniciando (e o inverso) | transição entre unidades multiciclo | `execute.sv:350-357` |
| `branch_em_hold` | branch/jump em execute com `hold_*` | decisão de desvio adiada | `execute.sv` |

### 3.2 Traps e interrupções (RTL, no CSRBank/execute)

| Bin | Condição | Por que importa |
|---|---|---|
| `irq_ack × classe_execute` | `interrupt_ack_o` cruzado com a classe da instrução em execute (load, store, branch, mul, div, CSR, `mret`) | interrupção sobre cada tipo de instrução; salvar o PC certo (`mepc`) |
| `irq_ack × hold` | `interrupt_ack` com `hold_o = 0`, e o caso bloqueado por hold | a condição `!hold_o` de `execute.sv:1018` |
| `irq_ack × csrw_mstatus` | aceitação de IRQ no ciclo de escrita em `mstatus` (o `@todo` em `CSRBank.sv:148`) | risco já marcado no próprio código |
| `exc × privilégio` | cada `exceptionCode_e` (`execute.sv:1025-1042`) em M e em U | uma exceção só vista em M não valida o caminho de U |
| `exc_prioridade` | duas causas simultâneas (ex.: `exc_load_access_fault_i` junto com ilegal) | a cadeia `if/else` define a prioridade |
| `ecall × privilégio` | `ECALL_FROM_UMODE` vs `ECALL_FROM_MMODE` | seleção de causa por privilégio |
| `mret_em_M`, `mret_em_U` | `is_mret` com `privilege_i == 2'b11` vs. outro | `machine_return` só vale em M (`execute.sv:1011`); em U deve ser ilegal |
| `mpp_valor` | `mstatus.MPP` escrito com 00, 01, 10, 11, depois `mret` | a ausência de legalização do MPP (seção 8, item 1) |
| `mip_escrita` | escrita em `mip` com bits MSIP/MTIP/MEIP | desvio da spec (seção 8, item 2) |

### 3.3 ISA (Python, Sail ou gerador)

**Matriz de CSR.** Um bin por combinação alcançável de:

```
grupo do CSR   {mstatus, mie, mip, mtvec, mepc, mcause, mscratch,
                contadores, contadores_ro (0xC00+), inexistente}
× privilégio   {M, U}
× operação     {csrrw, csrrs, csrrc, csrrwi, csrrsi, csrrci}
× rs1==0 (ou uimm==0)   {sim, não}
× rd==0                 {sim, não}
```

Cada célula tem um resultado esperado: sem efeito colateral, escrita, ou exceção. A lógica ilegal do RTL é:

```systemverilog
// execute.sv:340
exc_ilegal_csr_inst = (csr_write_enable && csr_address_i[11:10] == 2'b11)       // escreve em RO
                   || ((csr_read_enable || csr_write_enable) && csr_address_i[9:8] > privilege_i);  // CSR de nível maior
```

As combinações mais valiosas:

| Bin | O que testa |
|---|---|
| `csrrs/csrrc rs1=x0` em CSR read-only | não pode gerar exceção nem escrita (`csr_write_enable` com `csr_wr_uses_rs1`, `execute.sv:336`); é o tipo de bug do exemplo do Encarsia |
| `csrrw rd=x0` | não deve ler o CSR (`csr_read_enable`); efeito observável em CSRs com efeito colateral de leitura |
| `csrrsi/csrrci uimm=0` | mesma supressão de escrita nas variantes imediatas |
| `csrrw` em RO (`0xC00..`) | escrita em read-only → exceção ilegal |
| leitura de CSR de M a partir de U | `csr_address[9:8] > privilege` |
| `mret` em U | exceção de instrução ilegal |
| CSR inexistente em M | ilegal, mesmo com privilégio suficiente |

**Outras esquinas de ISA**, cruzadas com o que o RTL implementa:

| Bin | Observação |
|---|---|
| `load/store × desalinhamento × tamanho` (lh, lw, sh, sw, amo) | `execute.sv:243-258`; alinhado, desalinhado em 1 byte, em 2 bytes |
| `div/rem × (divisor 0, overflow INT_MIN/-1, sinais)` | casos de borda definidos pela spec |
| `branch × (tomado/não) × (previsto/não) × (para frente/para trás)` | exercita o preditor e o rollback |
| `jalr × (alvo desalinhado, rs1 == rd)` | `iaddr_misaligned` (`execute.sv:987`) e dependência rs1/rd |
| `instrução comprimida × (alinhada em 2 bytes, cruzando linha de fetch)` | só se `COMPRESSED` estiver ligado |
| `fence`, `fence.i`, `wfi` | classe frequentemente sem verificação |

## 4. Plano de implementação sugerido

1. **Semana 1:** escrever o `bind` com 6 a 10 bins microarquiteturais da seção 3.1 e medir a taxa de acerto no corpus de 2000 casos. Descartar os que acertam em mais de 90%.
2. **Semana 2:** implementar a matriz de CSR em Python (seção 3.3), a partir do trace do Sail. Gerar a lista de células alcançáveis e inalcançáveis.
3. **Semana 3:** cruzamento interrupção × classe (3.2). Conferir o pipeline do ciclo em que `interrupt_ack` dispara.
4. **Depois:** ligar a fonte 6 em `--guide`, rodar a ablação (6 ligada vs. desligada) e comparar bugs achados e tempo até o primeiro bug.

## 5. Limitações

- **Bin não é oráculo.** Acertar `csrrs rs1=x0` em CSR read-only diz que o programa chegou lá. Quem acha o bug é a comparação com o Sail, e ela precisa cobrir traps e `mcause`, não só a assinatura final.
- **Bins escritos à mão refletem o que você já sabe.** As arestas da fonte 0 existem para cobrir o resto.
- **Cruzamentos explodem.** Uma matriz com 6 dimensões tem milhares de células, a maioria inalcançável. Comece com 2 a 3 dimensões e exclua as impossíveis.
- **Dependem da configuração** (`COMPRESSED`, `DELAY_CYCLES`, extensões ligadas). Fixe-as por experimento, como já é feito para a fonte 0.
