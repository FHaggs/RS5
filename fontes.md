# Fontes de feedback do RS5: o que observar e como capturar

Este documento detalha três fontes de feedback do fuzzer: **pipeline**, **CSRs** e **HPM**. Para cada uma,
diz quais sinais usar, o que cada sinal significa e como capturá-lo. A cobertura do Verilator fica de
fora por enquanto. A fonte de traps registrados pelo tratador está em `explicacao_fonte4e5.md`.

Correspondência com a numeração do `pesquisa.md`: pipeline = fonte 0, CSRs = fonte 1, HPM = fonte 4.

Tudo aqui vale para a configuração baseline (`RV32IMU_Zicsr_Zihpm`, `BRANCHPRED=1`, `FORWARDING=1`,
`DELAY_CYCLES=0`). Sinais de extensões desligadas (C, A, V, Xkyber, XOSVM) aparecem marcados.

---

## 0. Vocabulário mínimo de RTL

- **Sinal (ou fio):** um valor de 1 ou mais bits que existe a cada instante da simulação. Exemplo:
  `hold` vale 1 enquanto o divisor está trabalhando.
- **Registrador (flip-flop):** guarda um valor e só o atualiza na **borda do clock**, ou seja, uma vez
  por ciclo. Tudo o que é escrito num `always_ff` é registrador.
- **Lógica combinacional:** calculada continuamente a partir de outros sinais, sem esperar o clock
  (`assign`, `always_comb`).
- **Struct `packed`:** um pacote de fios com nome. `ctrl_execute.is_load` é um fio dentro do pacote
  `ctrl_execute`. O pacote inteiro também pode ser tratado como um número só.
- **Registrador de pipeline:** o registrador na saída de cada estágio. No fim de cada ciclo, ele captura
  o que o estágio produziu, e no ciclo seguinte isso vira a entrada do próximo estágio.
- **`bind`:** recurso do SystemVerilog que **insere um módulo nosso dentro de um módulo do RS5 sem
  editar os arquivos do RS5**. Esse módulo "espião" enxerga os fios internos pelo nome. Só existe na
  simulação.

---

## 1. Fonte pipeline

### 1.1 A sua hipótese: a saída de um estágio é a entrada do próximo

Está certa. O RS5 passa um **pacote de controle** de estágio em estágio, e o nome de todos os tipos
termina em `_ctrl_t` ([RS5_pkg.sv:264-367](rtl/RS5_pkg.sv#L264-L367)). Em [RS5.sv](rtl/RS5.sv), os fios
que ligam os estágios se chamam:

```
 fetch1 ──decode_ctrl───▶ decoder1 ──ctrl_execute───▶ execute1 ──ctrl_mem_access──▶ mem_access1 ──ctrl_retire──▶ retire1
        (decode_ctrl_t)            (exec_ctrl_t)        │        (wb_ctrl_t)                    (wb_ctrl_t)
                                                        └──────▶ CSRBank1 (recebe o mesmo ctrl_execute)
```

Como são registradores de pipeline, **num mesmo ciclo** cada pacote descreve a instrução que está
naquele estágio. Olhar os quatro pacotes juntos é tirar uma **fotografia da linha de montagem** inteira:

| Fio no `RS5.sv` | Tipo | Descreve a instrução que está em | Bits |
|---|---|---|---|
| `decode_ctrl` | `decode_ctrl_t` | decode | 4 |
| `ctrl_execute` | `exec_ctrl_t` | execute | ~90 |
| `ctrl_mem_access` | `wb_ctrl_t` | acesso à memória | 7 |
| `ctrl_retire` | `wb_ctrl_t` | retire (escrita no banco de registradores) | 7 |

### 1.2 O que **não** está nos pacotes

Os pacotes carregam informação **sobre a instrução** que está viajando. Os **eventos** do pipeline
(parar, saltar, descartar, desfazer uma previsão) são fios separados. Muitos andam "para trás", do
execute de volta para o fetch e o decode, e alguns são internos de um módulo. Eles também precisam
entrar:

| Sinal | Onde está | O que é |
|---|---|---|
| `hazard_rs1`, `hazard_rs2`, `hazard_mem` | interno ao `decoder1` | qual dependência causou a bolha (só o OU dos três vai para o pacote) |
| escolha do forwarding | calculada dentro do `decoder1`, não é guardada em fio nenhum | de onde veio o operando (execute, memória, retire ou banco) |
| `hold` (e `hold_mul`, `hold_div` internos ao `execute1`) | `RS5.sv` | pipeline congelado por uma unidade lenta |
| `jump`, `jump_rollback`, `ctx_switch` | `RS5.sv` (saídas do execute) | salto confirmado, previsão desfeita, entrada/saída de trap |
| `RAISE_EXCEPTION`, `MACHINE_RETURN`, `interrupt_ack_o` | `RS5.sv` (saídas do execute) | qual tipo de troca de contexto |
| `bp_take_fetch` | `RS5.sv` (saída do decode) | o previsor mandou o fetch saltar |
| `stall` | entrada do RS5 (vem do testbench) | a memória pediu para esperar |
| `interrupt_pending`, `privilege` | `RS5.sv` (saídas do CSRBank) | há interrupção para aceitar; modo atual (M ou U) |

### 1.3 Cada sinal: o que é e se entra

#### Fetch → decode (`decode_ctrl`, tipo `decode_ctrl_t`)

| Campo | O que significa | Entra? |
|---|---|---|
| `valid` | o fetch está entregando uma instrução de verdade (0 = esperando a memória ou recomeçando depois de um salto) | sim, 1 bit |
| `jumping` | o fetch está no meio de um redirecionamento (houve salto ou trap); o que ele entregar agora é do caminho antigo e será descartado | sim, 1 bit |
| `compressed` | a instrução tem 16 bits (extensão C) | só com C |
| `jump_misaligned` | salto para endereço terminado em 2 (só existe com C) | só com C |

Mais um sinal do decode para o fetch:

| Sinal | O que significa | Entra? |
|---|---|---|
| `bp_take_fetch` | o **previsor de desvio** mandou saltar antes de saber se o desvio é tomado. O previsor do RS5 é estático e fica no decode ([decode.sv:516-520](rtl/decode.sv#L516-L520)): `JAL` e desvios **para trás** (laços) são previstos como tomados; `JALR` nunca é previsto | sim, 1 bit |

#### Decode → execute (`ctrl_execute`, tipo `exec_ctrl_t`)

O maior pacote, com uns 90 bits ([RS5_pkg.sv:264-350](rtl/RS5_pkg.sv#L264-L350)). Divido em três grupos.

**Grupo A: "que instrução é".** São dezenas de bits do tipo `is_add`, `is_xor`, `is_load`, `is_beq`,
`is_mul`, `is_csr`, `is_ecall`… **Não entram crus.** Para o pipeline, um `xor` e um `or` se comportam
igual; o que muda o timing é a **classe**. Comprimimos em 4 bits:

| Classe | Bits do pacote que a definem | Por que é uma classe própria |
|---|---|---|
| bolha | `is_nop` | o decode transformou a instrução em NOP (hazard ou descarte); também `FENCE`, que o RS5 decodifica como NOP ([decode.sv:252](rtl/decode.sv#L252)) |
| ALU | `is_add`, `is_sub`, `is_xor`, `is_or`, `is_and`, `is_sll`, `is_srl`, `is_sra`, `is_slt`, `is_sltu`, `is_lui`, `is_auipc` | resultado em 1 ciclo, disponível para forwarding imediato |
| load | `is_load` | resultado só depois da memória: causa hazard de load |
| store | `is_store` | causa o hazard "load depois de store" |
| desvio | `is_beq` … `is_bgeu` | pode ser previsto, tomado ou desfeito |
| JAL | `is_jal_jalr` e não `is_jalr` | sempre previsto como tomado |
| JALR | `is_jalr` | nunca previsto: sempre redireciona no execute |
| MUL | `is_mul` | várias iterações (hold) |
| DIV/REM | `is_div`, `is_rem` | ~32 ciclos de hold |
| CSR | `is_csr` | lê/escreve o CSRBank |
| sistema | `is_ecall`, `is_ebreak`, `is_mret`, `is_wfi`, `is_sret` | troca de contexto |
| atômica | `is_amo`, `is_lr`, `is_sc` | só com A |
| cripto/bitmanip | `is_aes`, `is_sha2`, `is_zbkb`, `is_zicond` | só com as extensões |
| Xkyber | `is_kyber` | só com Xkyber |
| vetor | `is_vector` | só com V |

**Grupo B: o estado da instrução no pipeline.** Entram direto:

| Campo | O que significa |
|---|---|
| `hazard` | esta "instrução" é uma bolha inserida por hazard de dados |
| `killed` | esta "instrução" foi descartada (caminho errado de um salto, ou instrução que gerou exceção) |
| `bp_taken` | o previsor tinha mandado saltar quando ela estava no decode |
| `exc_ilegal_inst` | o decode marcou a instrução como ilegal (vai gerar exceção no execute) |
| `exc_inst_access_fault` | falha de acesso na busca; só com XOSVM, **sempre 0** na baseline: não entra |

Quando o decode insere uma bolha, ele zera o pacote, liga `is_nop` e marca `hazard` ou `killed`
([decode.sv:925-933](rtl/decode.sv#L925-L933)). Por isso a classe "bolha" junto com esses dois bits diz
**que tipo** de bolha é.

**Grupo C: detalhes que não mudam o timing.** `amo_op`, `sha2_op`, `kyber_op`, `zbkb_op`, `vector_op`,
`is_byte`/`is_half`/`is_unsigned`, `div_signed`, `aes_is_mix`, `zicond_is_eqz`, `csr_op`,
`csr_rd_uses_rs1`, `csr_wr_uses_rs1`, `rd_we`. **Não entram.** Uma exceção a considerar: `mul_low`, porque
`mul` e `mulh` levam números diferentes de ciclos no multiplicador. Pode virar uma classe MUL-baixo e
outra MUL-alto se quisermos.

#### Dentro do decode: hazards e forwarding (não estão em pacote nenhum)

**Hazards** ([decode.sv:621-662](rtl/decode.sv#L621-L662)). Com forwarding ligado, só dois casos geram bolha:

| Sinal | Situação | Exemplo |
|---|---|---|
| `hazard_rs1` / `hazard_rs2` | a instrução usa um registrador que um **load** recente ainda vai escrever (o registrador fica "trancado" por dois ciclos) | `lw x5, 0(x1)` seguido de `add x6, x5, x2` |
| `hazard_mem` | um **load** logo depois de um **store**, com qualquer endereço. O próprio código comenta: "não sei por que precisamos desse hazard, mas removê-lo quebra o processador" | `sw x7, 8(x1)` seguido de `lw x8, 0(x2)` |

Entram os três bits separados, porque o pacote só leva o OU deles.

**Forwarding** ([decode.sv:686-708](rtl/decode.sv#L686-L708)). Quando a instrução no decode lê `rs1` e
`rs2`, o RS5 escolhe de onde vem cada valor, nesta prioridade:

1. do **execute**, se a instrução lá vai escrever esse registrador (`rs1 == rd_execute && write_enable_exec`);
2. do **acesso à memória** (`rs1 == rd_mem_access && ctrl_mem_access.rd_we`);
3. do **retire** (`rs1 == rd_retire && ctrl_retire.rd_we`);
4. senão, do **banco de registradores**.

Essa escolha não fica guardada em fio nenhum, então o espião **recalcula** com a mesma prioridade, usando
os fios que existem no `RS5.sv` (`rs1`, `rs2`, `rd_execute`, `write_enable_exec`, `rd_mem_access`,
`rd_retire` e os pacotes). Ela só interessa quando a instrução realmente usa o operando (`use_rs1`,
`use_rs2`, internos ao decoder) e o registrador não é `x0`. Resultado: 2 bits por operando, 4 no total.
Esta é a parte que as fontes HPM não enxergam e que mais nos interessa.

#### Saídas do execute (eventos)

| Sinal | O que significa | Entra? |
|---|---|---|
| `hold_mul`, `hold_div` (internos ao `execute1`) | multiplicador ou divisor ocupados; o pipeline inteiro congela | sim, 2 bits (o `hold` geral é o OU deles, mais Xkyber/vetor/atômica, desligados) |
| `jump` | o execute confirmou um salto que **não** tinha sido previsto; o fetch precisa recomeçar em outro endereço ([execute.sv:956-978](rtl/execute.sv#L956-L978)) | sim |
| `jump_rollback` | o previsor mandou saltar, mas o desvio não foi tomado: desfaz | sim |
| `RAISE_EXCEPTION` | exceção neste ciclo | sim |
| `MACHINE_RETURN` | `mret` neste ciclo | sim |
| `interrupt_ack_o` | interrupção aceita neste ciclo | sim |
| `ctx_switch` | OU dos três anteriores | não (redundante) |
| `should_jump` | OU de `jump` e `ctx_switch` | não (redundante) |

#### Execute → memória → retire (`ctrl_mem_access` e `ctrl_retire`, tipo `wb_ctrl_t`)

Campos: `rd_we`, `is_load`, `is_lr`, `is_sc`, `is_byte`, `is_half`, `is_unsigned`. Para cada um dos dois
estágios, uma classe de 2 bits:

| Classe | Condição | Por que importa |
|---|---|---|
| nada | `rd_we = 0` | store, desvio ou bolha: não alimenta forwarding |
| resultado | `rd_we` e não load | fonte de forwarding |
| load | `is_load` | o dado chega da memória no retire |
| LR/SC | `is_lr` ou `is_sc` | só com A |

#### Sinais globais

| Sinal | O que significa | Entra? |
|---|---|---|
| `stall` | a memória pediu espera. Com `DELAY_CYCLES=0` nunca fica ativo na borda do clock | sim (para experimentos com espera) |
| `interrupt_pending` | há uma interrupção pronta para ser aceita | sim |
| `privilege` | modo atual: M (11) ou U (00) | sim, 2 bits |
| `mstatus_mie` (interno ao `CSRBank1`) | interrupções globalmente ligadas | sim |

### 1.4 Tudo junto vira uma entrada só

Sim, todos esses campos são concatenados numa única palavra por ciclo, o **snapshot**:

| Grupo | Campos | Bits |
|---|---|---|
| fetch → decode | `valid`, `jumping` (+ `compressed`, `jump_misaligned` com C) | 2 |
| previsão | `bp_take_fetch` | 1 |
| hazards no decode | `hazard_rs1`, `hazard_rs2`, `hazard_mem` | 3 |
| forwarding | origem de `rs1`, origem de `rs2` | 4 |
| instrução no execute | classe, `hazard`, `killed`, `bp_taken`, `exc_ilegal_inst` | 8 |
| eventos do execute | `hold_mul`, `hold_div`, `jump`, `jump_rollback`, `RAISE_EXCEPTION`, `MACHINE_RETURN`, `interrupt_ack_o` | 7 |
| memória e retire | classe de `ctrl_mem_access`, classe de `ctrl_retire` | 4 |
| global | `stall`, `interrupt_pending`, `privilege`, `mstatus_mie` | 5 |
| **Total** | | **~34** |

A chave do mapa compara o snapshot deste ciclo com o do anterior:

```
chave = hash(snapshot do ciclo anterior) >> 1  XOR  hash(snapshot deste ciclo)
```

Duas regras:

- **Nada de dados.** Nem PC, nem valores, nem números de registrador. A relação entre registradores
  aparece só na forma de bits ("houve hazard", "o forwarding veio do execute"). Um número de registrador
  no snapshot faria `add x5` e `add x6` serem estados diferentes, e o corpus explodiria.
- **Comprimir o que não muda o timing.** É por isso que os ~90 bits do `exec_ctrl_t` viram 8.

### 1.5 Como capturar

Um módulo espião ligado ao RS5 com `bind`. O `.*` conecta cada porta ao fio de mesmo nome que existe
dentro do RS5, sem editar o `RS5.sv`:

```systemverilog
module pipe_probe import RS5_pkg::*; (
    input logic         clk,
    input decode_ctrl_t decode_ctrl,
    input exec_ctrl_t   ctrl_execute,
    input wb_ctrl_t     ctrl_mem_access, ctrl_retire,
    input logic         jump, jump_rollback, RAISE_EXCEPTION, MACHINE_RETURN, interrupt_ack_o,
    input logic         bp_take_fetch, stall, interrupt_pending,
    input logic [1:0]   privilege,
    input logic [4:0]   rs1, rs2, rd_execute, rd_mem_access, rd_retire,
    input logic         write_enable_exec
);
    // Sinais internos de submódulos: referência hierárquica pelo caminho
    wire hz_rs1   = decoder1.hazard_rs1;     // idem hazard_rs2, hazard_mem, use_rs1, use_rs2
    wire hold_div = execute1.hold_div;       // idem hold_mul
    wire mie      = CSRBank1.mstatus_mie;

    // Forwarding recalculado com a mesma prioridade do decode
    function automatic logic [1:0] fwd(logic [4:0] rs);
        if      (rs == rd_execute    && write_enable_exec)     return 2'd1;   // execute
        else if (rs == rd_mem_access && ctrl_mem_access.rd_we) return 2'd2;   // memória
        else if (rs == rd_retire     && ctrl_retire.rd_we)     return 2'd3;   // retire
        else                                                   return 2'd0;   // banco
    endfunction

    logic [33:0] snap, snap_prev;
    assign snap = { /* concatenação dos campos da tabela 1.4 */ };
    always_ff @(posedge clk) snap_prev <= snap;

    fuzz_cov_part #(.SRC(0), .BITS(16)) part (
        .clk, .en(1'b1), .key((mix(snap_prev) >> 1) ^ mix(snap)));
endmodule

bind RS5 pipe_probe u_pipe_probe (.*);
```

Pontos a validar na implementação:

- **Referências hierárquicas** (`decoder1.hazard_rs1`): o nome é procurado subindo a hierarquia a partir
  do espião, que está dentro do RS5, onde existe o `decoder1`. Se o Verilator não resolver assim, a
  alternativa é o caminho absoluto `riscof_tb.dut.decoder1.hazard_rs1`.
- **`.*` dentro de `bind`:** confirmar o suporte na versão do Verilator. Se não funcionar, as conexões são
  escritas por extenso.
- **Sinais otimizados:** o Verilator pode eliminar fios internos que ele considera inúteis. Uma referência
  a eles normalmente os mantém, mas vale conferir que os valores não ficam constantes.

### 1.6 Exemplo: um load seguido de uso

```asm
lw   x5, 0(x10)
add  x6, x5, x1
```

Pela lógica do decode, a sequência esperada é esta (a confirmar na simulação):

| Ciclo | decode | execute | memória | retire | `hazard_rs1` | forwarding de rs1 |
|---|---|---|---|---|---|---|
| t | ADD | LOAD x5 | … | … | 1 | — |
| t+1 | ADD | bolha (`hazard`) | LOAD | … | 1 | — |
| t+2 | ADD | bolha (`hazard`) | … | LOAD | 0 | retire |
| t+3 | … | ADD | … | … | 0 | — |

Cada linha é um snapshot, e cada passagem de uma linha para a seguinte é uma posição no mapa. Um
programa que produza a passagem t+2 → t+3 pela primeira vez ("hazard de load resolvido por forwarding
do retire") é guardado.

---

## 2. Fonte CSRs

### 2.1 A ideia

Os **CSRs** (*Control and Status Registers*) guardam o estado de controle do processador: em que modo ele
está, se interrupções estão ligadas, o que aconteceu no último trap. Esta fonte **não registra todo
ciclo**. Ela registra só quando algo muda esse estado: uma exceção, uma interrupção, um `mret` ou uma
escrita em CSR. A chave junta o estado **antes**, o estado **depois**, o **evento** e uma fotografia
resumida do **pipeline** naquele momento.

Tudo está dentro do `CSRBank1` ([CSRBank.sv](rtl/CSRBank.sv)), que já recebe o `ctrl_execute` (como
`ctrl_i`), o `hold` e o `stall`. Um único espião ligado ao CSRBank enxerga tudo.

### 2.2 O estado: quais campos e o que é cada um

| Campo | Sinal no CSRBank | Bits | O que é | Por que interessa |
|---|---|---|---|---|
| privilégio | `privilege` | 2 | modo atual: M (máquina, 11) ou U (usuário, 00). Muda na entrada de um trap (vai para M) e no `mret` (volta para o modo do MPP) | erros de troca de modo; os 2 bits crus permitem ver valores que não deveriam existir (01 = S, que o RS5 não tem) |
| `mstatus.MIE` | `mstatus_mie` | 1 | chave geral das interrupções | o hardware desliga ao entrar num trap e restaura no `mret` |
| `mstatus.MPIE` | `mstatus_mpie` | 1 | cópia do MIE de antes do trap | é o que o `mret` restaura |
| `mstatus.MPP` | `mstatus_mpp` | 2 | modo de antes do trap; o `mret` volta para ele | **suspeita de bug:** o RS5 aceita qualquer valor escrito ([CSRBank.sv:189-191](rtl/CSRBank.sv#L189-L191)). Um MPP=01 aparece como estado inédito, e o fuzzer é recompensado por chegar lá |
| `mie.MSIE`, `MTIE`, `MEIE` | `mie_msie`, `mie_mtie`, `mie_meie` | 3 | chave de cada fonte de interrupção: software, timer, externa | combinações de interrupções habilitadas |
| `mip.MSIP`, `MTIP`, `MEIP` | `mip_msip`, `mip_mtip`, `mip_meip` | 3 | interrupções **pendentes** (chegando). MTIP vem do timer (RTC), MEIP do controlador de periféricos (PLIC), MSIP é escrita por software | **suspeita de bug:** o RS5 aceita escrita direta nos três ([CSRBank.sv:246-282](rtl/CSRBank.sv#L246-L282)) |
| interrupção pendente | `interrupt_pending_o` | 1 | vale 1 quando `MIE` está ligado e alguma fonte está habilitada e pendente; o execute aceita a interrupção no próximo momento possível | janela entre "pendente" e "aceita", onde moram bugs de timing |
| contadores congelados | `mcountinhibit[0]`, `mcountinhibit[2]` | 2 | `mcycle` (CY) e `minstret` (IR) parados | a fonte HPM depende disso |

Total: cerca de 15 bits. O `mvmctl_en` (MMU) só existe com XOSVM e fica fora na baseline.

### 2.3 Os eventos

| Evento | Sinal | Atributo | Detalhe |
|---|---|---|---|
| exceção | `raise_exception_i` | `exception_code_i` | códigos possíveis na baseline: instrução ilegal (2), salto desalinhado (0), `ebreak` (3), load desalinhado (4), store desalinhado (6), `ecall` de U (8), `ecall` de M (11). Falhas de acesso só com XOSVM |
| interrupção aceita | `interrupt_ack_i` | `irq_code` | externa, software ou timer, nesta prioridade ([CSRBank.sv:1622-1627](rtl/CSRBank.sv#L1622-L1627)) |
| retorno de trap | `machine_return_i` | — | `mret` |
| escrita de CSR | `write_enable_i` | **grupo** de `address_i` + `operation_i` | a operação é W (`csrrw`), S (`csrrs`, liga bits) ou C (`csrrc`, desliga bits) |

O endereço de CSR tem 12 bits, mas para o estado o que importa é **qual grupo** foi escrito:

| Grupo | CSRs |
|---|---|
| status | `mstatus`, `mstatush` |
| interrupções | `mie`, `mip` |
| vetor de trap | `mtvec` |
| dados de trap | `mepc`, `mcause`, `mtval`, `mscratch` |
| controle de contadores | `mcountinhibit`, `mcounteren` |
| contadores | `mcycle`, `minstret` e metades altas |
| HPM | `mhpmcounter3..31`, `mhpmevent3..31` e metades altas |
| somente leitura | `misa`, `mvendorid`, `marchid`, `mimpid`, `mhartid`, `cycle`/`time`/`instret` de usuário |
| outros / inexistentes | o resto |

O evento cabe em uns 7 bits: 2 para o tipo e 5 para o atributo (código da exceção, código da
interrupção, ou grupo + operação).

### 2.4 O contexto do pipeline no momento do evento

Vem do `ctrl_i` (o mesmo `ctrl_execute` da fonte pipeline), que o CSRBank já recebe:

| Campo | Bits |
|---|---|
| classe da instrução no execute (a mesma tabela da seção 1.3) | 4 |
| `ctrl_i.hazard`, `ctrl_i.killed` | 2 |
| `hold`, `stall` | 2 |

É isto que dá valor à fonte. Sozinho, o estado de CSR do RS5 é pequeno e satura rápido. Cruzado com o
pipeline, distingue "exceção com o divisor ocupado" de "exceção com o pipeline livre", ou "interrupção no
mesmo ciclo de uma escrita no `mstatus`", situação que o próprio código marca como dúvida
([CSRBank.sv:148](rtl/CSRBank.sv#L148)).

### 2.5 Como capturar: antes e depois

O evento acontece **num ciclo**, e os registradores de CSR só mudam **na borda do clock que encerra
esse ciclo**. Então:

- **no ciclo do evento**, o espião guarda o estado (ainda o "antes"), o evento e o contexto;
- **no ciclo seguinte**, lê o estado de novo (agora o "depois") e monta a chave.

```systemverilog
module csr_probe import RS5_pkg::*; ( /* portas com os nomes dos sinais do CSRBank */ );
    wire evento = raise_exception_i | interrupt_ack_i | machine_return_i | write_enable_i;
    wire [14:0] estado = {privilege, mstatus_mie, mstatus_mpie, mstatus_mpp,
                          mie_msie, mie_mtie, mie_meie, mip_msip, mip_mtip, mip_meip,
                          interrupt_pending_o, mcountinhibit[0], mcountinhibit[2]};

    logic        pend;
    logic [14:0] antes;
    logic [6:0]  ev;
    logic [7:0]  ctx;
    always_ff @(posedge clk) begin
        pend  <= evento;
        antes <= estado;
        ev    <= codifica_evento(...);
        ctx   <= {classe(ctrl_i), ctrl_i.hazard, ctrl_i.killed, hold, stall};
    end

    fuzz_cov_part #(.SRC(1), .BITS(12)) part (
        .clk, .en(pend), .key(mix({antes, estado /* = depois */, ev, ctx})));
endmodule

bind CSRBank csr_probe u_csr_probe (.*);
```

Aqui não se usa o `>> 1`: a transição já está explícita na chave, com "antes" e "depois" lado a lado.

### 2.6 O que fica de fora

`mepc`, `mtval`, `mscratch`, a base do `mtvec` e os contadores são **dados**: endereços e valores que
mudam a cada programa e explodiriam o número de estados. O valor do `mcause` também fica fora do estado,
porque já entra como atributo do evento.

### 2.7 Exemplo: um `ecall` de U para M, e o caminho até o bug suspeito

| Evento | Antes | Depois |
|---|---|---|
| `ecall` em U (exceção 8), pipeline livre | priv=U, MIE=1 | priv=M, MIE=0, MPIE=1, MPP=U |
| `mret` | priv=M, MIE=0, MPIE=1, MPP=U | priv=U, MIE=1 |
| `csrw mstatus` com MPP=01 (grupo status, W) | MPP=U | **MPP=01** ← estado nunca visto: o programa é guardado |
| `mret` | MPP=01 | **priv=01** ← outro estado nunca visto |

O mapa leva o fuzzer, passo a passo, exatamente para a situação suspeita.

---

## 3. Fonte HPM

### 3.1 O que são e como o programa lê

**HPM** (*Hardware Performance Monitor*) são CSRs que o hardware incrementa sozinho quando certos eventos
acontecem. O programa lê qualquer um com uma instrução:

```asm
csrr t0, mhpmcounter7      # t0 = ciclos de bolha por hazard desde o reset
```

Detalhes do RS5 ([CSRBank.sv:438-1085](rtl/CSRBank.sv#L438-L1085)), que existem com `HPMCOUNTEREnable=1`
(ligado na baseline por causa do `Zihpm`):

- **Os contadores HPM têm 32 bits.** Só `mcycle` e `minstret` têm 64. As "metades altas"
  `mhpmcounterNh` **não** são os bits altos do contador N, como diz a spec: em geral valem 0, e algumas
  são outros contadores (`mhpmcounter3h` é o `cntr_scalar`). Para programas curtos, 32 bits sobram.
- **Cada contador tem um bit no `mcountinhibit`** que o congela. O bit 3 congela o `cntr_killed` e o
  `cntr_scalar` juntos.
- **Os contadores podem ser escritos** (`csrw mhpmcounter7, zero`). Dá para zerá-los na fronteira de cada
  bloco em vez de calcular diferenças, mas ler e subtrair é mais simples e não altera o estado.
- **A captura é feita pelo próprio programa**, na fronteira de cada bloco, com os contadores congelados
  durante a leitura. O trecho de medição e a extração do buffer estão em `explicacao_fonte4e5.md`.

### 3.2 Todos os contadores da baseline

"Conta quando" é a condição que faz o contador somar 1 num ciclo. `ctrl_i` é o pacote da instrução que
está no **execute**.

| CSR | Nome no RTL | Conta quando | Significado | Tipo |
|---|---|---|---|---|
| `mcycle` | `mcycle` | todo ciclo | ciclos de clock | **dinâmico** |
| `minstret` | `minstret` | `!(hold \|\| ctrl_i.is_nop)` | instruções que passaram pelo execute (não conta bolhas nem `FENCE`; ver 3.4) | **dinâmico** |
| `mhpmcounter3` | `cntr_killed` | `ctrl_i.killed` | ciclos com instrução descartada no execute (salto, previsão errada, exceção) | **dinâmico** |
| `mhpmcounter3h` | `cntr_scalar` | `!(is_nop \|\| is_vector)` | ciclos com instrução escalar no execute. Sem a condição `!hold`, conta ciclos e não instruções | redundante |
| `mhpmcounter4` | `cntr_context` | exceção, `mret` ou interrupção | trocas de contexto | **dinâmico** |
| `mhpmcounter5` | `cntr_exception` | exceção | exceções | **dinâmico** |
| `mhpmcounter6` | `cntr_irq` | interrupção aceita | interrupções | **dinâmico** |
| `mhpmcounter7` | `cntr_hazard` | `ctrl_i.hazard && !hold` | ciclos de bolha por hazard de dados (load-uso, load depois de store) | **dinâmico** |
| `mhpmcounter8` | `cntr_stall` | `stall` | ciclos esperando a memória | dinâmico; **sempre 0** com `DELAY_CYCLES=0` |
| `mhpmcounter9` | `cntr_nop` | `!hold && ctrl_i.is_nop` | ciclos com NOP no execute: **todas** as bolhas (hazard e descarte) e `FENCE` | **dinâmico** |
| `mhpmcounter10` | `cntr_logic` | `!hold` e `xor`/`or`/`and` | instruções lógicas | estático |
| `mhpmcounter11` | `cntr_addsub` | `!hold` e `add`/`sub` | somas e subtrações | estático |
| `mhpmcounter12` | `cntr_shift` | `!hold` e deslocamentos | deslocamentos | estático |
| `mhpmcounter13` | `cntr_branch` | `!hold` e desvio condicional | desvios executados | estático |
| `mhpmcounter14` | `cntr_jump` | `!hold` e `jal`/`jalr` | saltos | estático |
| `mhpmcounter15` | `cntr_load` | `!hold` e load | loads | estático |
| `mhpmcounter16` | `cntr_store` | `!hold` e store | stores | estático |
| `mhpmcounter17` | `cntr_sys` | `!hold` e `mret`/`wfi`/`ecall`/`ebreak` | instruções de sistema | estático |
| `mhpmcounter18` | `cntr_csr` | `!hold` e instrução de CSR | acessos a CSR | estático |
| `mhpmcounter19` | `cntr_luislt` | `!hold` e `slt`/`sltu`/`lui` | comparações e `lui` | estático |
| `mhpmcounter20` | `cntr_compressed` | `!hold` e instrução de 16 bits | instruções comprimidas | só com C |
| `mhpmcounter21` | `cntr_misaligned` | salto para endereço terminado em 2 | saltos desalinhados | só com C |
| `mhpmcounter22` | `cntr_mul` | `!hold` e `mul*` | multiplicações | estático |
| `mhpmcounter23` | `cntr_div` | `!hold` e `div*`/`rem*` | divisões | estático |
| `mhpmcounter24..30` | `cntr_v*` | instruções vetoriais | — | só com V (lê 0) |
| `mhpmcounter31` | — | nunca | — | sempre 0 |

**Dinâmico** quer dizer que depende do que aconteceu no pipeline: o mesmo bloco pode dar valores
diferentes conforme os dados e o que veio antes. **Estático** quer dizer que depende só de quais
instruções o bloco tem (o gerador já sabe), com exceção de laços e desvios dependentes de dado.

### 3.3 Quais vamos usar

O vetor de cada bloco, calculado como diferença entre a leitura no fim e no começo do bloco:

| Componente | Cálculo | O que captura |
|---|---|---|
| ciclos perdidos | `Δmcycle − Δminstret` | todo atraso somado: holds de mul/div, bolhas, descartes |
| descartes | `ΔmHPM3` (`cntr_killed`) | saltos e previsões erradas, traps |
| bolhas de hazard | `ΔmHPM7` (`cntr_hazard`) | load-uso e load depois de store |
| NOPs no execute | `ΔmHPM9` (`cntr_nop`) | todas as bolhas; comparado com os dois anteriores, separa os tipos |
| exceções | `ΔmHPM5` | exceções no bloco |
| interrupções | `ΔmHPM6` | interrupções no bloco |
| retornos de trap | `ΔmHPM4 − ΔmHPM5 − ΔmHPM6` | `mret` executados |
| espera de memória | `ΔmHPM8` | só em experimentos com `DELAY_CYCLES > 0` |

Os contadores **estáticos** (10 a 23) não entram no estado, mas têm outro uso: o Python sabe quantos
loads, stores, desvios… cada bloco executou (pelo trace do Sail ou pela estrutura do bloco). Se o
contador do RS5 discordar, é um bug no próprio contador. Vira um oráculo de graça.

O que **nenhum** contador mostra: **qual forwarding** foi usado e **a ordem** dos eventos dentro do bloco.
Para ver forwarding sem simulador, seria preciso acrescentar contadores novos ao RS5 (`fwd_exec`,
`fwd_mem`, `fwd_retire`, `bp_mispredict`, hazards separados por tipo). Isso está como extensão no
`pesquisa.md`.

### 3.4 Observações sobre os contadores (a confirmar com o Sail)

- **`minstret` não conta `FENCE`.** O decode transforma `FENCE` em `NOP` ([decode.sv:252](rtl/decode.sv#L252)),
  e o `minstret` ignora NOPs ([CSRBank.sv:419](rtl/CSRBank.sv#L419)). Pela spec, `FENCE` é uma instrução
  completada e deveria contar. Um programa que leia o `minstret` depois de um `FENCE` deve divergir do
  Sail. Isso confirma o motivo do item 3 da seção 8 do `pesquisa.md`.
- **`minstret` e os contadores por classe não olham `stall`.** Durante uma espera de memória, a instrução
  fica parada no execute (o decode só avança com `!(stall || hold)`), e esses contadores continuam
  somando a cada ciclo. Com `DELAY_CYCLES = 0` isso nunca aparece; com espera, o `minstret` pode contar a
  mesma instrução várias vezes. O contador `retired` do testbench usa a mesma condição, então o CPI do
  `meta.json` também ficaria errado nesse caso.
- **Metades altas fora da spec.** `mhpmcounter3h` lê outro contador em vez dos bits altos do
  `mhpmcounter3`. É definido pela implementação e não afeta o fuzzer, mas é um desvio da spec.
