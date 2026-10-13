# Fuzzing do RS5 com um mapa de feedback segmentado

Esta apresentação conta a parte do plano que trata do **feedback**: como o fuzzer percebe que um
programa fez algo novo dentro do processador. De onde vêm os programas (o gerador) e como um bug é
detectado (o oráculo) ficam para outra parte; aqui basta saber que o fuzzer gera programas, roda cada
um no RS5 e precisa decidir quais guardar.

---

## 1. O problema

Um fuzzer gera milhares de programas. A maioria faz praticamente a mesma coisa dentro do processador:
soma, carrega, desvia, termina. Se o fuzzer tratar todos iguais, ele gasta quase todo o tempo repetindo
o que já testou.

O que ele precisa é de um sinal que diga: **"este programa fez o processador passar por um caminho que
nenhum outro tinha feito"**. Programas assim são guardados e viram ponto de partida para novas
mutações. Os outros são descartados.

O AFL, o fuzzer de software mais conhecido, resolve isso com uma ideia simples: um **mapa**.

---

## 2. O mapa do AFL

### Um quadro de contadores

Imagine um quadro com 65.536 contadores, todos zerados. O programa testado foi compilado de um jeito
especial: cada trecho de código (um **bloco básico**) recebeu um número aleatório, como uma etiqueta.
Toda vez que a execução passa de um bloco **A** para um bloco **B**, um contador do quadro é
incrementado. A posição desse contador é calculada a partir dos dois números:

```
posição = (A >> 1) XOR B
```

No fim da execução, o quadro mostra **quais passagens** aconteceram e **quantas vezes**.

### Por que passagens, e não lugares?

Saber que você visitou as cidades A, B, C, D e E não diz em que ordem. Os roteiros
`A → B → C → D → E` e `A → B → D → C → E` passam pelos mesmos lugares, mas são viagens diferentes. Um
mapa de lugares (que é o que a cobertura de linha mede) não distingue os dois; um mapa de **passagens**
distingue. Muitos bugs só aparecem numa ordem específica.

### Por que o `>> 1`?

Sem o deslocamento, ir de A para B e voltar de B para A cairiam na mesma posição (o XOR é simétrico), e
toda passagem de um bloco para ele mesmo cairia na posição 0 (`A XOR A = 0`). O deslocamento preserva a
direção da passagem e separa os laços.

### Buckets: contar sem se afogar em detalhes

O quadro guarda quantas vezes cada passagem aconteceu, mas o AFL não compara o número exato. Ele olha a
**faixa** em que o número cai:

```
1 │ 2 │ 3 │ 4–7 │ 8–15 │ 16–31 │ 32–127 │ 128+
```

Um laço que roda 5 ou 6 vezes é, na prática, o mesmo comportamento: os dois caem em `4–7`. Um laço que
roda uma vez e outro que roda cinquenta são comportamentos diferentes: caem em `1` e `32–127`. Sem as
faixas, cada contagem diferente pareceria novidade, e o fuzzer guardaria milhares de programas quase
iguais.

### A regra de novidade

O AFL lembra, para cada posição do quadro, quais faixas já apareceram em algum programa. Um programa
novo é guardado se produzir **uma passagem nunca vista** ou **uma faixa nunca vista** numa passagem
conhecida. Esse é o laço inteiro:

```
escolhe um programa guardado → muta → executa → olha o quadro → algo novo? → guarda
        ▲                                                                       │
        └───────────────────────────────────────────────────────────────────────┘
```

Cada programa guardado é um degrau. Mutações acumuladas sobre esses degraus chegam a comportamentos
que um programa aleatório nunca alcançaria.

---

## 3. A chave: o que é um "lugar" dentro de um processador?

No software, o "lugar" é óbvio: o bloco de código que está executando. Num processador, **nós escolhemos
o que é um lugar**. Pode ser o estado do pipeline num ciclo, o estado dos registradores de controle
depois de um trap, o perfil de um trecho do programa. A fórmula continua a mesma, só muda o que entra
nela:

```
chave = hash(estado anterior) >> 1  XOR  hash(estado atual)
```

O `hash` é necessário porque, ao contrário das etiquetas aleatórias do AFL, nossos estados têm bits com
significado, e estados parecidos colidiriam se fossem combinados diretamente.

Há uma regra que vale para todas as escolhas: **o estado só pode ter sinais de controle, nunca dados**.
Se o valor de um registrador entrasse no estado, todo programa pareceria novo, e o mapa deixaria de
separar o que importa do que não importa.

Escolher o que é "estado" é a principal decisão de projeto, e não sabemos de antemão qual escolha
encontra mais bugs. Por isso o plano é **medir várias escolhas ao mesmo tempo**. Cada escolha é uma
**fonte** de feedback.

---

## 4. As fontes

### Fonte 0 · O pipeline, ciclo a ciclo

**Por quê.** O RS5 é um processador pequeno, em ordem. A parte difícil dele não está nas instruções em si,
mas na interação entre elas dentro do pipeline: uma instrução que precisa de um resultado ainda não
pronto (hazard), o atalho que entrega esse resultado antes da hora (forwarding), o divisor que segura o
pipeline por dezenas de ciclos, um desvio que descarta o que já tinha sido buscado.

**Como.** Um módulo de observação é "grudado" no RTL sem alterá-lo (`bind` do SystemVerilog). A cada
ciclo, ele monta uma fotografia de uns 32 bits: que tipo de instrução está em cada estágio, se há hazard,
de onde veio o forwarding, se alguma unidade está segurando o pipeline, se houve desvio ou trap. A chave
é a passagem de uma fotografia para a seguinte.

**Quando.** Todo ciclo de clock.

**Exemplo.** Um `load` em execução enquanto a instrução seguinte já quer usar o valor carregado: no
ciclo *t* o pipeline detecta o hazard; no ciclo *t+1* insere uma bolha e resolve com forwarding. Essas
duas passagens viram duas posições do mapa. O primeiro programa que as produzir é guardado.

```angelo

Fetch, decode, execute, mem_acess, retire. Olhar sinais com ctrl_t (definidos no arquivo RS5_pkg.sv)

```

### Fonte 1 · Traps e registradores de controle, a cada evento

**Por quê.** Inspirada no ProcessorFuzz, que usa as mudanças nos registradores de controle (CSRs) como
sinal. No RS5, esse estado sozinho é pequeno: dois modos de privilégio, poucas interrupções. O que
interessa é o **cruzamento** com o pipeline: uma exceção que chega enquanto o divisor está ocupado, uma
interrupção no mesmo ciclo de uma escrita no `mstatus` (o próprio código do RS5 tem um comentário
perguntando o que acontece nesse caso). É também nessa região que lendo o código encontramos dois
possíveis desvios da especificação, ainda a confirmar.

**Como.** Outro módulo de observação, no banco de CSRs. A chave junta o estado de controle **antes** e
**depois** do evento, o tipo de evento e uma fotografia resumida do pipeline naquele momento.

**Quando.** Só quando algo acontece: uma exceção, uma interrupção aceita, um retorno de trap (`mret`) ou
uma escrita em CSR. São poucos eventos por programa, mas cada um diz muito.

### Fonte 2 · A cobertura de linha do Verilator (a referência)

**Por quê.** É o feedback que o fuzzer usa hoje, e serve de **comparação**. Um teste mostrou por que ela
não basta. Na cobertura bruta, o fuzzer atual parecia melhor que a suíte oficial de testes (78% contra
67%). Mas todo esse excedente vinha de lógica que o processador calcula para qualquer instrução e depois
descarta: os números aleatórios do fuzzer "acertavam" linhas do leitor de CSRs sem que nenhum CSR fosse
lido. Descontado isso, a suíte oficial cobre 91,5% do que é alcançável, e o fuzzer, 85%. A cobertura de
linha mede "esta linha foi calculada", não "isto aconteceu".

**Como e quando.** O Verilator conta sozinho durante a simulação e grava um arquivo no final.

### Fonte 3 · Diversidade de instruções, pelo modelo de referência (opcional)

**Por quê.** Mede a variedade **do programa** em si, sem olhar para o RS5: que tipo de instrução vem
depois de qual, e a que distância está a instrução da qual ela depende. Por isso vale para qualquer
processador.

**Como e quando.** A cada instrução executada no Sail (o modelo de referência da especificação), lida do
registro de execução dele depois que o programa termina.

### Fontes 4 e 5 · O que o próprio programa consegue ver

**Por quê.** As fontes 0 e 1 exigem simulador e acesso ao RTL. Mas o RS5 tem **contadores de desempenho**
(HPM) que o software pode ler: quantas instruções foram descartadas, quantos ciclos se perderam com
hazards, quantos com espera de memória. Se o próprio programa lê esses contadores, o feedback funciona
igual na simulação e numa **FPGA**, sem instrumentar o Verilog.

**Como.** O programa é montado a partir de blocos (um laço de soma, um CRC, uma rodada de AES…). Na
fronteira de cada bloco, ele lê os contadores e guarda a diferença. Cada bloco vira um estado ("bloco CRC,
8–15 ciclos perdidos, nenhum descarte, 8–15 hazards"), e a chave é a passagem de um bloco para o
seguinte (fonte 4). Quando acontece um trap, o tratador anota a causa e o estado anterior (fonte 5).

**Quando.** Nas fronteiras de bloco e a cada trap. O programa só anota os números; quem monta o mapa é
o fuzzer, depois que a execução termina.

```
Fronteiras dos blocos é OK. Mas tem a ferramenta novo do angelo que pode ser usada
```

**Limite.** Contadores dão totais por trecho, não a ordem dentro dele. E alguns comportamentos não têm
contador: um forwarding bem-sucedido **evita** o hazard e não aparece em lugar nenhum. Medir quanto se
perde com isso, e quais contadores novos valeriam a pena acrescentar ao RS5, faz parte da pesquisa.

### As fontes lado a lado

| Fonte | O que é "estado" | Quando registra | Precisa de simulador? |
|---|---|---|---|
| 0 · pipeline | fotografia de controle do pipeline | todo ciclo | sim |
| 1 · traps e CSRs | estado de controle + evento + contexto do pipeline | a cada trap ou escrita de CSR | sim |
| 2 · Verilator | linha do RTL | contagem interna, gravada no fim | sim |
| 3 · ISA pelo Sail | par de instruções consecutivas | a cada instrução do Sail | não (só o Sail) |
| 4 · HPM por bloco | perfil de contadores de um bloco | a cada fronteira de bloco | **não** |
| 5 · traps do tratador | causa e estado anterior do trap | a cada trap | **não** |
### Tirar o 5
---

## 5. Um mapa, uma região por fonte

Todas as fontes escrevem no **mesmo mapa**, mas cada uma na sua **região**:

```
┌──────────────┬────────────┬──────────┬──────────┬──────────────┬──────────┐
│ 0 pipeline   │ 1 traps/CSR│ 2 linha  │ 3 ISA    │ 4 HPM/bloco  │ 5 traps  │
└──────────────┴────────────┴──────────┴──────────┴──────────────┴──────────┘
```

Cada fonte registra no seu ritmo (uma a cada ciclo, outra a cada evento, outra a cada bloco), mas o
fuzzer lê o mapa inteiro **uma única vez, no fim de cada programa**. A regra de novidade do AFL é
aplicada região por região.

A segmentação é o que transforma o fuzzer num **instrumento de pesquisa**:

- **Todas as fontes gravam sempre; só algumas decidem.** Num experimento, escolhemos quais regiões
  contam para guardar um programa. As outras continuam sendo medidas.
- Isso permite perguntas cruzadas: guiado só pelo pipeline, quantas transições de trap o fuzzer alcança?
  Guiado só pelos contadores que o programa enxerga, quanto do pipeline ele explora?
- E permite a pergunta principal: **quais fontes valem a pena manter?** Em particular, quanto o sinal
  barato (contadores, que rodam na FPGA) preserva do sinal caro (observação do RTL, que exige
  simulação).

**Como decidir se uma fonte vale a pena.** Não pela cobertura: medir uma fonte pela própria cobertura
seria circular, e o teste com a fonte 2 mostrou como a cobertura engana. A medida é **encontrar bugs**:
reintroduzir bugs reais que já foram corrigidos no histórico do RS5, injetar bugs artificiais no RTL
(um forwarding errado, um hazard não detectado) e medir quais combinações de fontes os encontram, e em
quanto tempo.

---

## 6. Score ponderado

Qualquer novidade numa fonte que está guiando já basta para guardar o programa. O que muda entre as
fontes é **quanto esforço** cada programa guardado recebe, ou seja, quantas mutações:

```
score = Σ peso(fonte) × novidades(fonte)
```

As fontes têm volumes muito diferentes. A fonte 0 gera centenas de passagens por programa; a fonte 1,
duas ou três. Uma transição de trap inédita é rara e valiosa e deve pesar mais que uma passagem de
pipeline inédita. Os pesos também são um botão experimental: ajustá-los é outra forma de descobrir quais
sinais levam aos bugs.

---

## Em resumo

1. O AFL decide quais programas guardar com um **mapa de passagens** contadas em **faixas**.
2. Num processador, nós escolhemos o que é "estado"; cada escolha é uma **fonte** de feedback.
3. Seis fontes, do pipeline ciclo a ciclo até os contadores que o próprio programa lê, cada uma
   registrando no seu momento.
4. Um **mapa segmentado** permite rodar todas juntas e descobrir, medindo **bugs encontrados**, quais
   valem a pena.
5. Um **score ponderado** distribui o esforço de mutação entre as novidades de cada fonte.
