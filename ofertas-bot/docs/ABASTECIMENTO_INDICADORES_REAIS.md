# Abastecimento do ranking por dados oficiais preservados

O extrato anterior do contrato preservava tipos e omitia descrições. O arquivo
`CONTRATO_INDICADORES_SHOPEE_20261021.json` recupera as descrições do Explorer
oficial já salvo localmente, sem HTTP. SHA256 do HTML de origem:
`c48bed7c7caef93e815903e92f446caf81b51544a9e1b09fe293dd0c69054de4`.

## O que pode ser utilizado

| Campo | Evidência local | Uso automático |
|---|---|---|
| preço/faixa | Preço do produto em moeda local, mínimo/máximo | Cotação anunciada, nunca confirmação de preço final. Faixa ambígua não pontua. |
| `ratingStar` | Descrição identifica avaliação do produto | PARTIAL; escala e quantidade de avaliações não documentadas, score null. Zero reportado não é convertido em nota ruim nem em ausência de avaliações. |
| `sales` | Contagem de vendas do produto | PARTIAL; período não documentado, score null. Não é automaticamente venda acumulada, demanda recente ou tendência. |
| `shopType` | Tipo da loja do produto | Preservado, sem ponto de reputação. |
| comissão/desconto/período | Já normalizados, mas perdidos na comparação | Preservados em `provider_observation`, sem virar desconto histórico ou estoque. |
| `offerLink` | Documentado no esquema, fora da seleção assinada atual | Se fornecido numa resposta, preservado como candidato; titularidade e verificação operacional permanecem falsas. Nenhum link é gerado. |

Os dados de produto antes agrupados sob `seller` continuam disponíveis por
compatibilidade e agora também aparecem em `product_indicators`, com presença de
campos, origem, horário, identidade e fingerprint. Zero e lista vazia não são
confundidos com campo ausente. Os payloads assinados não foram alterados.

Cobertura documental e cobertura pontuável são separadas. Pela regra existente de
metade do peso para PARTIAL, avaliação e vendas reconhecidas representam 17,5%
documentais. Sem escala/amostra/período não há pontos nessas dimensões. Isso não
significa 17,5 pontos nem elegibilidade. Observações antigas deixam de contribuir
para a cobertura vigente, mas permanecem no relatório com seus dados originais.

## Importação operacional sem cadastro por produto

Novas rotas administrativas autenticadas:

- POST `/api/v1/shopee-discovery/ranking-signals/preview`: valida sem gravar.
- POST `/api/v1/shopee-discovery/ranking-signals/import`: documento imutável e
  idempotente, condicionado às confirmações explícitas de origem, semântica e importação.

CLI com cálculo interno do hash, sem exibir chave administrativa:

```powershell
.venv/Scripts/python.exe -m app.import_ranking_signals --csv exportacao.csv --manifest semantica.json
.venv/Scripts/python.exe -m app.import_ranking_signals --csv exportacao.csv --manifest semantica.json --persist
```

O manifesto informa `columns`, `source_url`, `source_reference`,
`column_semantics_reference`, `actor`, `exported_at` e as confirmações já revisadas.
Não contém CSV ou hash. `confirmed_official_document`, `confirmed_column_semantics`
e, para gravar, `confirmed_import` exigem true explícito; o comando não os preenche.

Colunas canônicas: `shop_id`, `item_id`, `observed_at`, `product_rating`,
`product_review_count`, `sales`, `seller_rating`, `seller_review_count`.
Somente campos presentes são mapeados. `rating_scale_max=5` exige escala documentada;
sem isso a avaliação do produto permanece parcial. `sales_semantics` aceita
UNSPECIFIED_PERIOD, LIFETIME ou WINDOW; WINDOW exige `sales_period_seconds`.
`decimal_separator` e `delimiter` são explícitos. Não há inferência de período.

O lote aceita até 200 observações e não cria produtos, ofertas, históricos ou links.
As datas originais são preservadas, inclusive em séries históricas. Pontos atuais
continuam sujeitos à validade do ranking. Documentos contraditórios no mesmo horário
não vencem por ordem de chegada. O worker lê novos documentos por identidade a cada
tick, mesmo depois de esgotar o orçamento de consultas. A API e o radar usam a mesma fonte.

## Operações adicionais e publicação

O cache também documenta `shopOfferV2`, `listItemFeeds` e `getItemFeedData`, mas não
comprova permissão atual da conta. `shopOfferV2` não documenta estoque, quantidade de
avaliações ou semântica de reputação para seu `ratingStar`. Não resolve sozinho a
elegibilidade. O dicionário das colunas dos feeds permanece desconhecido.

O importador aceita dados já obtidos legalmente dessas operações por mapeamento
explícito, com `origin_operation` e `source_authorization_reference`. Não faz HTTP
nem reutiliza a tentativa referenciada. Documentos somente de loja podem omitir
itemId: métricas documentadas ficam ligadas a `shop:<id>` e alcançam candidatos dessa
loja, sem inventar produtos. Não são deduzidas do nome, selo ou campo sem descrição.

O caminho comercial permanece a importação oficial existente de preço público e
disponibilidade vigentes, identidade/opções documentadas e link próprio verificado.
`offerLink` ou `generateShortLink` exigem prova de vínculo à conta/anúncio; a mutação
de geração exige autorização própria. Nenhuma dessas operações é executada aqui.
