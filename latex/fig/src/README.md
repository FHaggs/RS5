Fontes das figuras do capítulo `previous.tex`.

- `extract.py <saída.json>`: lê os `cases.jsonl.gz` das oito rodadas de 200 000 programas
  (ajustar `D`) e grava as curvas acumuladas por fonte, amostradas a cada 500 programas.
- `curves.json`: resultado do passo acima para as rodadas de 2026-10-29.
- `figs.py curves.json <dir>`: gera `saturacao_arestas.svg` e `razao_guiado.svg`.
- `flow.py <arquivo.svg>`: gera o diagrama `fluxo_fuzzer.svg`.
- Conversão: `rsvg-convert -f pdf -o ../<nome>.pdf <nome>.svg`.
