Sim: nas duas fontes é o próprio programa RISC-V que escreve os dados numa região de memória, e essa região é extraída no fim, do mesmo jeito que a assinatura já é hoje. Vou montar a explicação em camadas: primeiro o pipeline e os hazards, depois os contadores, a fonte 4, os traps e a fonte 5.

# 1. O pipeline em um minuto

Pense numa lavanderia com quatro máquinas em fila: lavar, secar, passar, dobrar. Você não espera uma peça terminar as quatro etapas para começar a próxima: enquanto a primeira seca, a segunda já está lavando. Com a fila cheia, sai uma peça pronta por etapa de tempo.

O RS5 faz isso com instruções. Cada **ciclo de clock** é uma etapa de tempo, e cada instrução passa por estágios:

```
             ciclo 1   ciclo 2   ciclo 3   ciclo 4   ciclo 5
busca        instr A   instr B   instr C   instr D   instr E
decodifica             instr A   instr B   instr C   instr D
executa                          instr A   instr B   instr C
memória/fim                                instr A   instr B
```

O ideal é **uma instrução terminada por ciclo**. Quando o pipeline precisa parar ou jogar trabalho fora, perde ciclos. Esses "engasgos" são exatamente onde os bugs costumam morar, porque são as situações em que o hardware precisa tomar decisões especiais.

# 2. Os engasgos do RS5 (os hazards de que estamos falando)

Conferi no [decode.sv](rtl/decode.sv) o que o RS5 faz de fato (configuração com forwarding ligado):

**a) Dependência resolvida por atalho (forwarding): sem perda.**

```
add x5, x1, x2      # calcula x5
sub x6, x5, x3      # precisa de x5 logo em seguida
```

Quando o `sub` vai ler `x5`, o `add` ainda não gravou no banco de registradores. Em vez de esperar, o RS5 pega o resultado "no caminho", direto do estágio onde ele está. Há três atalhos possíveis (do execute, do acesso à memória e do retire) ([decode.sv:686-708](rtl/decode.sv#L686-L708)). Como nada para, **nenhum contador registra isso**. É uma lógica delicada (escolher o atalho errado dá resultado errado em silêncio) e invisível para as fontes 4 e 5.

**b) Load seguido de uso: uma bolha.**

```
lw  x5, 0(x1)       # x5 vem da memória
add x6, x5, x2      # precisa de x5, que ainda não chegou
```

O valor de um load só existe depois que a memória responde, então não há atalho a tempo. O RS5 "tranca" o registrador de destino do load por duas instruções ([decode.sv:555](rtl/decode.sv#L555)). Se uma das duas seguintes usar esse registrador, o pipeline insere uma **bolha**, um ciclo vazio, e espera.

**c) Load logo depois de store: uma bolha.**

```
sw  x7, 8(x1)
lw  x8, 0(x2)       # qualquer endereço
```

O RS5 sempre insere uma bolha nesse caso, mesmo com endereços diferentes ([decode.sv:651](rtl/decode.sv#L651)). O próprio código traz um comentário curioso: "não sei por que precisamos desse hazard, mas removê-lo quebra o processador". Para quem procura bugs, isso é um ótimo sinal de região frágil.

**d) Desvios: instruções descartadas.**

```
beq x1, x2, alvo    # se for tomado, as instruções logo abaixo
add ...             #   já tinham sido buscadas
sub ...             #   e precisam ser jogadas fora
```

O fetch não sabe para onde o desvio vai até ele ser executado, então continua buscando. O RS5 tem um **previsor de desvio** que tenta adivinhar. Quando erra, as instruções buscadas no caminho errado são **descartadas** (*killed*) e o fetch volta atrás (*rollback*).

**e) Unidades lentas: o pipeline congela.**

A divisão leva cerca de 32 ciclos, e a multiplicação alguns ciclos. Enquanto isso, o pipeline fica parado em **hold** ([execute.sv:888](rtl/execute.sv#L888)).

**f) Espera da memória (*stall*).**

Se a memória demora a responder, o pipeline espera. No nosso testbench, com `DELAY_CYCLES=0`, isso não acontece.

# 3. Os contadores de desempenho (HPM)

**HPM** (*Hardware Performance Monitor*) são registradores especiais, do tipo CSR, que o hardware incrementa sozinho quando certos eventos acontecem. O programa lê qualquer um deles com uma instrução:

```
csrr t0, mhpmcounter7      # t0 = quantos ciclos de hazard até agora
```

Os que interessam, ligados aos engasgos acima ([CSRBank.sv:438-470](rtl/CSRBank.sv#L438-L470)):

| Contador | O que conta | Engasgo |
|---|---|---|
| `mcycle` | ciclos desde o reset | tudo |
| `minstret` | instruções completadas | tudo |
| `mhpmcounter3` | instruções descartadas | (d) desvios, traps |
| `mhpmcounter7` | ciclos de bolha por hazard | (b) e (c) |
| `mhpmcounter8` | ciclos esperando a memória | (f) |
| `mhpmcounter9` | bolhas no execute | (b), (c), (d) |
| `mhpmcounter5`, `6` | exceções, interrupções | traps |
| `mhpmcounter22`, `23` | multiplicações, divisões | (e) |
| — | **nenhum conta forwarding** | (a) |

Um truque útil: se cada instrução levasse exatamente um ciclo, `mcycle` e `minstret` cresceriam juntos. Então **`Δmcycle − Δminstret` = ciclos perdidos** num trecho, somando todos os engasgos.

# 4. Fonte 4, passo a passo

### O programa ganha "pontos de medição"

O programa é montado por blocos (um laço de soma, um CRC, uma rodada de AES…). Entre um bloco e outro, o gerador insere um trechinho de medição. Reservamos dois registradores para isso, como já fazemos com o `x31`: `x30` aponta para o buffer e `x29` é temporário.

```asm
    # ---- fim do bloco 1 / medição ----
    li    x29, -1
    csrw  mcountinhibit, x29     # congela TODOS os contadores
    li    x29, 2                 # id do próximo bloco (o gerador sabe qual é)
    sw    x29,  0(x30)
    csrr  x29, mcycle
    sw    x29,  4(x30)
    csrr  x29, minstret
    sw    x29,  8(x30)
    csrr  x29, mhpmcounter3      # descartadas
    sw    x29, 12(x30)
    csrr  x29, mhpmcounter7      # hazards
    sw    x29, 16(x30)
    # … outros contadores …
    addi  x30, x30, 32           # avança para a próxima linha do buffer
    csrw  mcountinhibit, zero    # descongela
    # ---- início do bloco 2 ----
```

- **Por que congelar:** as próprias instruções de medição passam pelo pipeline e gerariam bolhas e ciclos. Congelando antes de ler, elas quase não afetam os números. O pouco que sobra é sempre igual, então não cria falsas novidades.
- **Por que gravar o id do bloco:** com laços e desvios, a ordem dos blocos executados só é conhecida em tempo de execução.

### Onde o buffer fica e por que fora da assinatura

O buffer é uma região de memória declarada no próprio programa (um label `hpm_buf` com espaço reservado). Ela fica **fora da assinatura** porque o Sail executa o mesmo programa sem pipeline: os contadores dele dão valores completamente diferentes. Se o buffer estivesse na assinatura, todo caso daria `mismatch`. Fica uma verificação pendente: confirmar se o Sail aceita ler `mhpmcounterN`. Se ele gerar um trap nessa instrução, os dois lados divergem.

### Como extraímos

Hoje, quando a simulação termina, o [riscof_tb.sv](riscof/riscof_tb.sv) já copia a região da assinatura da RAM para um arquivo (bloco `final`). O endereço vem do `nm`, que o [runner.py](riscof/fuzzer/runner.py) já usa para achar `begin_signature` e `tohost`. Para o buffer, seria o mesmo mecanismo com outro par de endereços (`+HPM_START`/`+HPM_END`). Na FPGA, o programa envia o buffer pela serial (UART) no fim.

### O que o Python faz com isso

Um exemplo com três linhas no buffer:

```
linha   bloco   mcycle   minstret   descartadas   hazards
  0       1       100        90          4            3
  1       2       160       130          4           12
  2       3       400       170          6           12
```

1. **Diferenças entre linhas** (o que aconteceu durante cada bloco):
   - bloco 2: 60 ciclos, 40 instruções → **20 ciclos perdidos**, 0 descartes, **9 hazards**
   - bloco 3: 240 ciclos, 40 instruções → **200 ciclos perdidos** (provavelmente divisões), 2 descartes, 0 hazards
2. **Buckets** em cada número, para formar o estado:
   - S₂ = (bloco 2, perdidos `16–31`, descartes `0`, hazards `8–15`)
   - S₃ = (bloco 3, perdidos `128+`, descartes `2`, hazards `0`)
3. **Chave da passagem:** `hash(S₂) >> 1 XOR hash(S₃)`, e incrementa essa posição na região 4 do mapa.
4. No fim, a regra de novidade de sempre: posição ou faixa nunca vista → o programa é guardado.

# 5. Traps, do zero

Um **trap** é quando o processador **interrompe o fluxo normal** do programa e pula para uma rotina especial, o **tratador** (*trap handler*). Há dois tipos:

- **Exceção:** causada pela própria instrução. Instrução ilegal (código que não existe), `ecall` (pedido ao sistema), `ebreak` (ponto de parada), load/store em endereço desalinhado, salto para endereço desalinhado.
- **Interrupção:** vem de fora, sem relação com a instrução atual. O timer disparou, um periférico pediu atenção.

Uma analogia: você está trabalhando e o telefone toca. Antes de atender, anota **onde parou** e **quem ligou**; depois de atender, volta para a anotação.

O hardware faz essa anotação sozinho, num único ciclo, usando CSRs:

| Passo | CSR | O que guarda |
|---|---|---|
| onde parou | `mepc` | endereço da instrução interrompida |
| por quê | `mcause` | código do motivo (ex.: 2 = instrução ilegal, 11 = `ecall`, timer…) |
| detalhe | `mtval` | informação extra (o endereço desalinhado, a instrução ilegal) |
| interrupções estavam ligadas? | `mstatus.MPIE` | cópia do `MIE` de antes; o `MIE` é desligado para o tratador não ser interrompido |
| em que modo estava | `mstatus.MPP` | modo de privilégio anterior (M = máquina, U = usuário) |
| para onde ir | `mtvec` | endereço do tratador |

O tratador é código comum que nós escrevemos. Ele lê essas informações, resolve a situação (por exemplo, pula a instrução problemática com `mepc += 4`) e termina com **`mret`**, que desfaz a anotação: restaura o `MIE`, volta ao modo do `MPP` e salta para o `mepc`.

**Hoje o fuzzer não tem tratador.** Se um programa causa um trap, o processador pula para um endereço sem código útil e o caso termina em `hang` ou `dut_timeout`. Por isso o gerador atual evita tudo que causa trap, e é por isso que o `report.md` mostrou o fuzzer sem cobrir nenhuma exceção. O tratador é necessário de qualquer jeito, para a fonte 5 e para o operador `trap` do gerador.

# 6. Fonte 5, passo a passo

O tratador, além de resolver o trap, **anota um registro** num segundo buffer (`trap_buf`, com `x28` como ponteiro):

```asm
trap_handler:
    csrr  x29, mcause
    sw    x29, 0(x28)            # o quê
    csrr  x29, mepc
    sw    x29, 4(x28)            # onde (o Python converte em bloco + posição)
    csrr  x29, mstatus
    sw    x29, 8(x28)            # MPP e MPIE = estado ANTES do trap
    addi  x28, x28, 12
    # resolve: pula a instrução que causou a exceção
    csrr  x29, mepc
    addi  x29, x29, 4
    csrw  mepc, x29
    mret                         # volta para o programa
```

Alguns detalhes:

- **O tratador não pode estragar os registradores do programa.** Ele usa só os reservados (`x28`, `x29`) ou salva e restaura os outros. Isso também é exigência do oráculo metamórfico, em que o programa com e sem interrupção deve terminar igual.
- **Por que o `mstatus` lido dentro do tratador mostra o estado anterior:** o hardware já copiou `MIE → MPIE` e o modo para `MPP` na entrada do trap.
- **Como provocar interrupções:** o programa programa o timer (o RTC, que no testbench fica mapeado na região de endereços `0x2…`) para disparar daqui a N ciclos. Quando dispara, no meio de algum bloco, o tratador registra onde caiu.
- **Extração:** igual à fonte 4, outro par de endereços no dump do testbench ou na UART.
- **Chave no Python:** `hash(mcause, MPP, MPIE, bloco, posição no bloco em bucket)`. O bucket da contagem diz quantas vezes o mesmo tipo de trap aconteceu no mesmo lugar.

**Um bônus:** para **exceções**, o conteúdo do `trap_buf` (`mcause`, `mepc`, `MPP`) é arquitetural, ou seja, o Sail produz exatamente os mesmos valores. Esse buffer pode ficar **dentro** da assinatura e virar mais um oráculo: se o RS5 anotar uma causa ou um endereço diferente do Sail, é bug. Para interrupções, não, porque o momento exato da chegada depende do timing, que o Sail não modela.

**O limite em relação à fonte 1:** a fonte 5 sabe **onde** no programa o trap aconteceu, mas não **em que situação estava o pipeline** naquele ciclo (div em hold, desvio sendo descartado…). Só o módulo `bind` no RTL enxerga isso.

# 7. Visão geral da memória do programa

```
┌──────────────────────────────┐
│ código: blocos + medições    │
│         + tratador de trap   │
├──────────────────────────────┤
│ fuzz_mem (dados dos blocos)  │  ← comparado com o Sail
├──────────────────────────────┤
│ assinatura (regs, fuzz_mem,  │  ← comparado com o Sail
│   trap_buf das exceções)     │
├──────────────────────────────┤
│ hpm_buf                      │  ← NÃO comparado; extraído só para o feedback
└──────────────────────────────┘
registradores reservados: x31 (fuzz_mem), x30 (hpm_buf), x29 (temporário), x28 (trap_buf)
```

Em resumo: nas fontes 4 e 5, o programa "se observa" e anota o que viu na própria memória. O testbench (ou a FPGA) só despeja essas anotações no fim, e todo o trabalho de transformar números em chaves do mapa acontece no Python.