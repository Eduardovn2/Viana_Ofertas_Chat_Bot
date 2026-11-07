# Prioridade textual e revisão documental

A análise inclui `review_queue`, ordenada por um índice textual de 0 a 100 com
componentes, penalidades e versão. O índice usa sobreposição com a consulta e
menções de marca/modelo/quantidade/conteúdo/edição/variante. Acessórios, ambiguidades
e conflitos recebem penalidades. Preço, nota e vendas não influenciam esse índice.
As faixas HIGH/MEDIUM/LOW são prioridades de revisão, não probabilidades estatísticas
nem prova de equivalência. O ranking comprovado continua usando seus próprios fatos
documentados e preços anunciados; a pontuação não o habilita ou modifica.

## Fontes verificadas no contrato local

`GET /api/v1/shopee-discovery/evidence-sources` retorna o mapa documental local,
sem HTTP externo ou concessão de permissão:

- `productOfferV2`: identidade do anúncio, título, preço anunciado e dados selecionados
  das lojas. Não há atributos estruturados suficientes para comprovar equivalência.
- `listItemFeeds`/`getItemFeedData`: operações documentadas. `ItemFeedDataRow.columns`
  é JSON, mas nomes e semântica das colunas não estão documentados localmente.
  Acesso da conta, quotas e presença de marca/GTIN/modelo/variante são desconhecidos.
  Não há coletor de feeds implementado ou autorização automática para consultá-los.
- Exportação oficial do portal de afiliados: pode ser apresentada pelo responsável
  se estiver legalmente disponível na conta. Só valem as colunas efetivamente presentes.
- Documento oficial do fabricante: pode documentar especificações do produto exato,
  mas não comprova quantidade vendida, condição da unidade ou condições de preço da loja.
- Observação manual de anúncio/opção acessível: pode completar fatos documentados com
  vínculo explícito à loja, item, variante e momento da observação gravada. Sem scraping.

Não se presume acesso a API de vendedor, estoque ou variantes. Frete, preço final,
disponibilidade e cupom aplicável continuam desconhecidos. Nenhuma origem é buscada
automaticamente ou autenticada externamente por este fluxo.

## Revisão administrativa utilizável

Todas as rotas exigem a autenticação administrativa existente. A busca de origem
precisa estar SUCCEEDED, com observação sanitizada disponível. Uma autorização
consumida pode ser referenciada para leitura/revisão, mas nunca é executada novamente.

```text
GET  /api/v1/shopee-discovery/multistore-searches/{UUID}/reviews/{shopId}/{itemId}
PUT  /api/v1/shopee-discovery/multistore-searches/{UUID}/reviews/{shopId}/{itemId}
POST /api/v1/shopee-discovery/multistore-searches/{UUID}/reviews/{shopId}/{itemId}/confirm
POST /api/v1/shopee-discovery/multistore-searches/{UUID}/analyze
```

O GET fornece `current_observation_fingerprint`, versão atual e pendências. O PUT
recebe, por exemplo, um documento parcial do fabricante:

```json
{
  "actor": "responsavel",
  "expected_revision": 0,
  "observation_fingerprint": "COPIAR_O_FINGERPRINT_ATUAL_DO_GET",
  "origin": "OFFICIAL_MANUFACTURER_DOCUMENT",
  "reference": "Referência do documento efetivamente apresentado",
  "source_observed_at": "2026-11-01T02:00:00Z",
  "source_excerpt": "Trecho documental efetivamente observado pelo revisor.",
  "binding_note": "Como o documento corresponde ao anúncio e à opção selecionada.",
  "facts": {"brand": "Marca documentada", "model": "Modelo documentado"}
}
```

A data deve representar uma observação real e não pode estar no futuro. `facts`
aceita patches parciais: campos ausentes ficam desconhecidos; `null` explícito retira
um campo do rascunho. Novas edições preservam a origem de campos não alterados.
Quantidade, condição e condições da loja exigem outra origem, como
`MANUAL_LISTING_OBSERVATION` ou `AFFILIATE_PORTAL_EXPORT` efetivamente disponível.

Cada campo mantém origem, referência, data, responsável, trecho e SHA-256 calculado
do trecho fornecido. Esse hash não é apresentado como hash do documento original
baixado: nenhum documento foi buscado. É necessária confirmação explícita do vínculo
documental, sem significar aprovação de uma nova consulta ou publicação:

```json
{"actor":"responsavel","expected_revision":1,"confirmed_listing_binding":true}
```

O rascunho só passa a DOCUMENTED quando os fatos exigidos estão completos e não há
conflito de título. Conteúdo e edição documentados continuam necessários para creatinas.
A análise reaproveita o mesmo gate do comparador, incluindo filtros de relevância,
condições equivalentes e validade do preço anunciado.

Os documentos ficam vinculados ao fingerprint da observação sanitizada e ao hash
do resultado de origem. Não se fabrica fingerprint da resposta bruta nem faixa de
preço ausente. Mudança na origem invalida o vínculo; versão desatualizada é recusada.
Editar uma revisão DOCUMENTED volta a DRAFT e suspende sua prova ativa, preservando
a documentação anterior para rastreabilidade.

O armazenamento usa registros próprios de revisão e auditoria existentes; não altera
autorizações, produtos ou histórico temporal. Mesmo grupos documentados não confirmam
estoque, frete, cupom ou preço final e não autorizam links ou Telegram.

## Próxima pesquisa real

Novos planos de `productOfferV2` podem receber uma palavra-chave específica pelo
campo `keyword` ou pela opção `--keyword` do comando de preparação existente.
Continuam fixos página 1, limite 3, um POST, sem retry/fallback/paginação. O plano
requer UUID novo e janela futura; a credencial, URL, palavra-chave, payload e prazo
continuam selados por hash. A parametrização não altera registros anteriores.

O caminho mínimo é definir o produto exato, preparar um plano novo, obter aprovação
expressa para esse UUID/hash e executar uma vez dentro da janela. Depois, a fila
textual orienta a revisão de fontes documentais efetivamente disponíveis. Investigar
feeds exige autorização própria e verificação prévia de acesso/colunas; não é fallback
automático da pesquisa. Nenhum plano real foi preparado, aprovado ou executado nesta
etapa de desenvolvimento.

## Visão consolidada e reutilização automática

A descoberta e a reanálise agora retornam `opportunity_view`, com anúncios por loja,
grupos comprovados, candidatos inconclusivos, decisão por anúncio e lacunas de
informação. O ciclo offline existente também inclui `multistore_opportunities`
para cada página já coletada, sem outra requisição. Páginas com observações distintas
permanecem em lotes separados, sem criar uma comparação temporal artificial.

```text
POST /api/v1/shopee-discovery/multistore-searches/{UUID}/opportunities
{"selection_limit":2}
```

A seleção usa prioridade textual e cobre lojas diferentes antes de alternativas
da mesma loja. `selection_limit` limita tarefas de revisão, não consultas à Shopee.
Não altera quotas, escopos ou preferências globais. O valor zero permite adiar a
revisão humana sem bloquear grupos que já podem ser comparados automaticamente.

As decisões distinguem:

- `AUTO_COMPARED`: documentação completa e válida, com vendedor equivalente.
- `AWAITING_COMPARABLE_SELLER`: documentação suficiente; falta outro vendedor.
  Não exige reconfirmação documental.
- `REVIEW_INSUFFICIENT_EVIDENCE` e `REVIEW_STALE_OR_CONFLICTING_EVIDENCE`: precisam
  de revisão, com atributos faltantes e conflitos identificados.
- `AUTHORIZED_PRICE_REVALIDATION_REQUIRED`: atributos documentados, mas cotação
  não resolvida. Não dispara consulta; depende de autorização própria.
- Anúncios incompatíveis documentados e filtros de relevância saem da fila humana.

Fontes DOCUMENTED e vinculadas ao mesmo fingerprint são reaproveitadas sem nova
confirmação. Campos de rascunhos aparecem como `PENDING_MANUAL_CONFIRMATION`, sem
preencher fatos comprovados. Revisões desatualizadas não são reaproveitadas.
Candidatos incompletos não impedem comparações válidas de outros anúncios.

`commercial_handoff` é uma prévia versionada para um adaptador futuro de aprovação
assistida. Contém apenas menores preços anunciados de grupos comprovados, preservando
empates, e mantém `can_create_offer=false`, `execution_allowed=false` e
`publication_authorized=false`. Não cria Offer, fila, link ou publicação.

O adaptador ainda precisa validar preço público aplicável, disponibilidade, atualidade,
condições comerciais, vínculo afiliado existente, categoria/destino e aprovação.
O OfferEngine atual exige queda histórica, referência da própria loja ou cupom;
uma diferença entre vendedores não pode ser inserida como desconto histórico.
Integração futura deve tratar `CROSS_STORE_ANNOUNCED_PRICE` como origem distinta,
sem relaxar os gates existentes. Cupom não é requisito da comparação multiloja.
