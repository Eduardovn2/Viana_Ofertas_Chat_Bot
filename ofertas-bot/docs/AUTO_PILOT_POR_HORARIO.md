# AUTO_PILOT por janela operacional

Novos planos reais usam `operating_policy` assinada, por padrão:

```json
{
  "version": "WINDOWED_V1",
  "on_no_work": "WAIT_UNTIL_END"
}
```

Exemplo de configuração para uma nova autorização independente, não criada por
este documento:

```json
{
  "actor": "eduardo.viana.3",
  "destination_id": 2,
  "not_before": "2026-11-04T12:00:00Z",
  "expires_at": "2026-11-04T14:00:00Z",
  "terms": ["Cockpit simulador Brave Bull", "Creatina monohidratada 250g", "Mouse Logitech G203"],
  "max_queries": 12,
  "max_messages": 20,
  "messages_per_window": 5,
  "window_seconds": 600,
  "page": 1,
  "limit": 3,
  "operating_policy": {
    "query_interval_seconds": 600,
    "on_no_work": "WAIT_UNTIL_END"
  }
}
```

O endpoint autenticado de preparação continua `POST /api/v1/auto-pilots-real`.
Aprovar o novo UUID/hash e iniciar continuam ações explícitas, pelos endpoints
`/{uuid}/approve` e `/{uuid}/start`. O worker independente existente executa ticks
de 30 segundos. Nenhum scheduler legado é habilitado.

## Distribuição e encerramento

Sem frequência explícita, intervalo = duração autorizada / orçamento de consultas,
com mínimo de 30 segundos. Para duas horas e 12 consultas, as buscas ficam nos
minutos 0, 10, 20, ..., 110, se as condições e permissões continuarem válidas.
O worker nunca consulta antes do horário previsto; pode atrasar até um tick ou
mais por processamento/indisponibilidade. Reinício não repõe buscas nem dispara
um lote para recuperar horários perdidos.

`duration_seconds`, quando fornecido, continua delimitando a duração desde a
ativação, sem estender `expires_at`. Pausa/reinício não alteram esse prazo.

Esgotar consultas não encerra a operação nova. O worker continua análise local,
consumo de exportações oficiais confirmadas e processamento da fila. Sem trabalho,
mostra `WAITING_NO_WORK_QUERY_BUDGET` e aguarda o fim. A opção explícita
`STOP_WHEN_DRAINED` encerra com `NO_WORK_AFTER_QUERY_BUDGET`.

Condições terminais de segurança ficam declaradas no novo escopo: fim do prazo,
parada administrativa, orçamento de mensagens, recusa de permissão Shopee e
impossibilidade de continuar sem repetir consultas incertas. O destino é restaurado.
Escopos antigos, sem política operacional, mantêm o comportamento originalmente
assinado. Novas preparações reais não aceitam essa modalidade legada.

## Análise e evidência comercial

`queries[].capture` preserva origem, ambiente, horário, IDs, título, loja, imagem,
URLs e preços anunciados. Não grava histórico temporal. `opportunities.analysis_queue`
recebe anúncios sem oferta cadastrada e apresenta atributos, confiança textual e
pendências. Grupos comprovados da mesma captura e com lojas distintas produzem
mínimo/média/mediana. O ranking prioriza preços abaixo da média e mediana desses
grupos; candidatos sem prova continuam separados, sem benchmark inventado.

Descoberta, classificação, verificação comercial, fila e publicação têm estados
distintos. `query_schedule` informa próxima busca, intervalo e saldo; `counts`
separa anúncios, comparações, oportunidades baratas, fila publicável e publicações.

O contrato local de `productOfferV2` não comprova disponibilidade nem opção exata,
preço público aplicável ou vínculo próprio do link para todos os anúncios.
A integração adicional utilizável é a exportação oficial confirmada, via
`/api/v1/manual-offers/official-exports/preview` e `/import`. Seu consumo é automático
a cada tick, inclusive depois de esgotar consultas. Documentos válidos são reutilizados;
novos documentos exigem a confirmação já prevista. O serviço invalida evidências
superadas ou inconclusivas e respeita a validade comercial de 600 segundos.

O contrato menciona `listItemFeeds/getItemFeedData`, mas permissão da conta e
semântica das colunas seguem desconhecidas. Não há autorização para consultar esses
feeds nem evidência para interpretar campos de estoque/variantes. Não são utilizados
como fonte comercial fictícia. Para anunciar produtos novos sem intervenção por
item, ainda é necessária uma fonte autorizada que forneça as evidências obrigatórias
e vínculos próprios verificados. Sem isso, publicações permanecem bloqueadas.
