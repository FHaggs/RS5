# Tempo do RISCOF antes e depois do PR #72 (modelo compilado uma única vez)

Medição de 2026-11-13 para o capítulo `latex/previous.tex` (tabela `tab:riscof-tempo`).

- Antes: `a54bb1c` (primeiro pai do merge). Depois: `ae986bf` (merge do PR #72,
  "Avoid re-Verilating for every RISCOF test").
- Suíte: `riscv-arch-test` 3.9.1, configuração `baseline.ini` (RV32IMU com Zicsr e Zihpm), 62 testes,
  todos passam nas duas versões.
- Máquina: 2 x AMD EPYC 7453 (112 núcleos lógicos), compartilhada, carga média de 67 a 110 durante
  as medidas. Execução restrita com `taskset` a 8 núcleos (2,3,5,6,21,22,23,25) ou a 56 (0-55).
- Verilator 5.052, g++ 15, `ccache` com diretório vazio em cada execução ("cold"); `before_warm` e
  `after_warm` repetem com o cache da execução anterior.

Como repetir: criar duas árvores com `git worktree add --detach <dir>/wt/before a54bb1c` e
`git worktree add --detach <dir>/wt/after ae986bf`, ligar `RingBuffer` e `riscof/riscv-arch-test`
da árvore principal em cada uma, copiar `time_riscof.sh` para `<dir>` e rodar
`time_riscof.sh <before|after> <rótulo> <dir do ccache> [núcleos]`.

Fases por execução (segundos; "etapa RS5" = compilação do modelo + testes no RS5):

| Execução | Núcleos | Preparo | Modelo | Testes no RS5 | Etapa RS5 | Sail | Total | CPU |
|---|---|---|---|---|---|---|---|---|
| before_8c | 8 | 35,1 | 0,7 | 92,1 | 92,8 | 6,8 | 134,9 | 728 |
| before_8c_2 | 8 | 31,8 | 0,5 | 98,2 | 98,7 | 6,8 | 137,5 | 775 |
| after_8c | 8 | 38,0 | 7,4 | 8,4 | 15,8 | 5,8 | 59,8 | 80 |
| after_8c_2 | 8 | 38,4 | 6,8 | 7,8 | 14,6 | 6,4 | 59,6 | 80 |
| before_cold | 56 | 34,4 | 0,5 | 23,8 | 24,3 | 4,8 | 63,8 | – |
| before_cold2 | 56 | 33,5 | 0,5 | 23,3 | 23,8 | 5,0 | 62,5 | – |
| before_56c_cpu | 56 | 35,1 | 0,4 | 22,6 | 23,0 | 5,5 | 63,8 | 910 |
| after_cold | 56 | 36,6 | 6,1 | 6,3 | 12,4 | 5,4 | 54,7 | – |
| after_cold2 | 56 | 33,6 | 6,2 | 5,8 | 12,0 | 5,5 | 51,2 | – |
| after_56c_cpu | 56 | 35,3 | 7,0 | 7,5 | 14,5 | 5,7 | 55,8 | 80 |
| before_warm (cache quente) | 56 | 35,8 | 0,6 | 11,9 | 12,5 | 5,2 | 53,7 | – |
| after_warm (cache quente) | 56 | 32,7 | 1,4 | 6,6 | 8,0 | 5,6 | 46,5 | – |

Com o `ccache` quente (repetição sem mudar o RTL) o fluxo antigo se aproxima do novo; a condição
"cold" é a que vale depois de qualquer alteração no RTL.
