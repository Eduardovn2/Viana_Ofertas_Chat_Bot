# Descoberta e análise local do AUTO_PILOT

Cada nova resposta fica em `queries[].capture`: origem, ambiente, horário de
registro, autorização, fingerprint e comparação normalizada. Os candidatos incluem
título, identidade, preço anunciado, faixa, horário original e URLs fornecidas.
Nenhuma captura cria uma linha no histórico temporal ou uma oferta operacional.

`opportunities.analysis_queue` recebe candidatos mesmo sem oferta cadastrada.
Apresenta prioridade textual com justificativas, atributos conhecidos/inferidos,
pendências de identidade e bloqueios comerciais. A pontuação não comprova identidade.
Evidências já aceitas na fila comercial permanecem identificadas separadamente.

`proven_comparisons[].price_statistics` contém mínimo, média e mediana em BRL.
A população é limitada ao mesmo grupo comprovado na mesma captura e usa somente
o menor anúncio válido de cada loja. O percentual abaixo da média não é desconto
histórico. Grupos com menos de duas lojas não produzem estatísticas comerciais.

Ao consumir o orçamento de consultas, o controlador termina a classificação local
e deixa de consultar. Novos planos reais usam a política por janela descrita em
[AUTO_PILOT_POR_HORARIO.md](AUTO_PILOT_POR_HORARIO.md): continuam processando a fila
e aguardando até o fim, com estado explícito de ausência de trabalho. Encerramento
antecipado sem trabalho exige `STOP_WHEN_DRAINED`. Autorizações legadas sem essa
política preservam a semântica originalmente assinada de `QUERY_BUDGET`.

Reanálise offline de uma captura anterior (não altera banco nem autorização):

```powershell
.venv/Scripts/python.exe -m app.analyze_pilot_captures --input docs/RESULTADO_BUSCA_MULTILOJA_20261031_2210.json --output .runtime/ranking-preservado.json
```

O comando recusa sobrescrever arquivos. Mantém a data original e sinaliza preço
desatualizado. Pilotos antigos que não preservaram títulos/preços são reportados
como dados indisponíveis; não são preenchidos com outra captura.

A publicação continua exigindo identidade documentada (individual não exige
equivalência multiloja), preço público aplicável vigente, disponibilidade e link
próprio verificado. Captura, análise, revisão e publicação são etapas distintas.
