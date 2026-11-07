# Fontes comerciais automatizáveis Shopee — proposta de 26/10/2026

**Conclusão:** a menor evolução é monitorar anúncios conhecidos por
`productOfferV2`, inicialmente como **cotações informativas**, reutilizando o
conector, reservas, histórico e comparador. O contrato disponível não comprova
preço público aplicável, variante ou estoque. Portanto, permite planejar coleta
de preços sem capturas, mas ainda não a aprovação comercial automática.

Esta entrega é documental: nenhuma implementação, chamada autenticada, consulta
ao banco operacional ou ativação. Produto **2** e observações **2/3** intocados.

## Base e alcance da evidência

- Fonte: [Explorer oficial brasileiro](https://open-api.affiliate.shopee.com.br/explorer/v2),
  cópia pública local `.runtime/shopee-official-explorer.html`.
- Data do cache conforme o relatório existente: **21/10/2026**. SHA256 do HTML:
  `c48bed7c7caef93e815903e92f446caf81b51544a9e1b09fe293dd0c69054de4`.
- [Extrato verificável das três operações, tipos e enums](CONTRATO_FONTES_SHOPEE_20261026.json),
  obtido lendo o objeto `SCHEMA` do HTML, sem executar seu JavaScript.
- O leitor web não conseguiu abrir a documentação pública nesta execução.
  Não houve introspecção autenticada nem reconfirmação do contrato vigente.
- O [teste anterior](VALIDACAO_SHOPEE_2026_10_21.md) comprovou acesso pontual a
  `productOfferV2`. Não comprova autorização para feeds, todas as seleções de
  campos ou recorrência. Quotas e permissões atuais por conta permanecem pendentes.

## Matriz de capacidade

| Necessidade | Campo/operação documentado no cache | Uso seguro e limite |
|---|---|---|
| Descobrir anúncios | `productOfferV2(keyword, page, limit, productCatId, ...)` | Busca e identificação; palavra-chave não comprova tipo de produto nem condições comerciais. Preservar categorias e filtros existentes. |
| Reconsultar anúncio conhecido | `productOfferV2(shopId, itemId, page:1, limit:1)` | Melhor ponto inicial para monitoramento. Validar identidade retornada; resultado vazio não comprova falta de estoque. |
| Preço anunciado | `price: String!` | Valor em moeda local segundo o contrato; converter estritamente para Decimal. Não documenta Pix, cupom, público elegível ou variante. Não é atestado de preço efetivamente pagável. |
| Faixa de preço | `priceMin`, `priceMax`: `String!` | Mínimo/máximo do produto. Faixa diferente ou incompleta permanece sem preço utilizável conforme o normalizador atual. Faixa igual não prova variante ou estoque. |
| Desconto anunciado | `priceDiscountRate: Int!`, documentado de 0 a 100 | Pode ser guardado como percentual **anunciado pela API**, separado de queda histórica, cupom e comissão. Ainda não solicitado/normalizado pelo conector. |
| Período da oferta | `periodStartTime`, `periodEndTime`: `Int64!` | Descritos como início/fim da oferta. Unidade temporal e relação com preço, cupom ou campanha de afiliados não estão esclarecidas no cache. Não prometer validade comercial com esses campos. |
| Nome, imagem e categoria | `productName`, `imageUrl`, `productCatIds`, `shopId`, `itemId` | Já suportados pelo conector. Categoria externa não substitui classificação local; imagem não identifica variante selecionada. |
| Comissão | `commissionRate`, `commission`, demais taxas de comissão | Remuneração do afiliado, separada de preço/desconto do consumidor. Não alimentar o percentual de queda. |
| Descobrir feeds | `listItemFeeds(feedMode: FULL ou DELTA)` | Metadados `datafeedId`, `referenceId`, `datafeedName`, `description`, `totalCount`, `date`, `feedMode`. Não retorna preços de produtos nessa operação. Sem argumento de limite documentado. |
| Ler dados de feed | `getItemFeedData(datafeedId, offset, limit)` | Retorna `rows { columns updateType }` e `pageInfo { offset limit totalCount hasMore }`. `columns` é uma **String com JSON**, sem dicionário de colunas no contrato local. Não há campos de preço/variante/estoque tipados nessa operação. |
| Mudanças em feeds | `FeedMode: FULL/DELTA`; `updateType: UNKNOWN/NEW/UPDATE/DELETE` | Eventos do conjunto de dados. DELETE ou ausência no feed não significam estoque zero; nunca apagar histórico por esse motivo. |
| Variante/modelo selecionado | Não documentado em `ProductOfferV2`; desconhecido dentro de `columns` | Não fabricar modelId/variantId. IDs de modelo existentes em tipos de relatórios de conversão não constituem catálogo de variantes. |
| Disponibilidade/estoque | Não documentado em `ProductOfferV2`; desconhecido dentro de `columns` | Preservar `availability=UNKNOWN`. Presença no catálogo, vendas acumuladas e campanha ativa não comprovam estoque. |
| Pagamento, cupom aplicável, frete e preço público | Nenhum campo suficiente nas operações examinadas | Preservar acesso UNKNOWN/CONDITIONAL conforme evidência. Obter fonte oficial adicional e condições explícitas antes de elegibilidade comercial. |

O cache mostra `limit` padrão **20** para `productOfferV2`, sem máximo oficial
documentado. Para `getItemFeedData`, padrão e máximo documentados são **500**;
offset inicial **0**. Esses valores são paginação, **não quotas de requisições**.
Os limites conservadores locais já existentes não substituem limites oficiais.

### Como consumir o desconto anunciado

Proposta aditiva: guardar `priceDiscountRate` como metadado
`advertised_discount_percent`, aceitando somente inteiro estrito entre 0 e 100.
Ausência não vira zero; booleanos, valores negativos e acima de 100 são inválidos.
Não calcular preço anterior pela divisão inversa do percentual, inclusive em 100%.
Não preencher `previous_price`, desconto histórico ou cupom a partir desse campo.
Uma primeira cotação continua **INITIAL**, mesmo com percentual anunciado alto.

O filtro atual de desconto mínimo exige queda histórica verificada. Mantê-lo;
qualquer filtro futuro por desconto anunciado deve ter nome e finalidade próprios,
sem promover candidatura ou comprovar condições comerciais automaticamente.

## Três operações diferentes no pipeline

1. **Descoberta:** localizar candidatos por termos/categorias, registrar identidade
   e relevância. Preservar a rejeição de acessórios quando o termo procura console.
2. **Monitoramento:** reconsultar identidades selecionadas e anexar observações
   comparáveis ao longo do tempo. Variação de cotação é informação, não autorização.
3. **Comprovação comercial:** verificar preço aplicável, variante, disponibilidade,
   condições e evidência de vínculo do link segundo política aprovada. Ausência de
   prova mantém a oferta BLOCKED, independentemente de pontuação ou desconto anunciado.

## Integração mínima proposta, ainda não implementada

| Componente existente | Ajuste futuro restrito |
|---|---|
| `ShopeeConnector.fetch_product` / `discover_offers` | Acrescentar seleção/validação dos três campos documentados de desconto/período. Manter contrato monetário, identidade e UNKNOWN para fatos não comprovados. Não criar outro conector. |
| `ShopeeCollectionService` | Reutilizar reserva anterior ao HTTP, auditoria, lease e limite de chamadas. O request administrativo atual é por palavra-chave: acrescentar modo tipado por identidade para chamar `fetch_product`, sob autorização nova e delimitada. |
| `record_price` / `ProductPriceHistory` | Acrescentar à allowlist apenas metadados públicos tipados necessários; manter a transação e idempotência existentes. Não armazenar resposta bruta ou cabeçalhos. |
| `PriceComparisonService` | Persistir uma base de contexto estável e validada para a comparação existente. Não misturar API, documento manual ou feed nem alterar observações anteriores. |
| `ShopeeDiscoveryService` / `MonitoringRuntime` | Futuramente admitir um adaptador real específico **observacional**, separado do laboratório mockado. Reutilizar DISCOVER/MONITOR, reservas, fencing e APScheduler; não retirar a vedação atual a I/O real apenas ligando uma flag. |
| `DiscoveryOperations` / `AppSetting` / políticas por destino | Reutilizar janelas, fuso, frequências, prioridades e orçamentos editáveis. Acrescentar quotas oficiais confirmadas e escopo de autorização independente das preferências. |
| `OfferEngine` / evidências / fila | Continuar bloqueando dados insuficientes. A etapa observacional não chama aprovação, geração de links nem publisher; a ponte comercial depende de autorização e evidências próprias. |

Na recorrência futura, cada ciclo deve consumir reserva própria dentro do escopo
autorizado, contando também tentativas com falha nas quotas. Preservar leases,
fencing e bloqueio de sobreposição; erro de permissão pausa para revisão, e timeout
não reaproveita a mesma autorização como tentativa não ocorrida. Frequência/backoff
continuam dinâmicos e limitados pela política oficial, sem retry imediato.

`ingest_discovery` respeita `monitoring_enabled`: não usá-lo para alterar
silenciosamente o produto 2, cujo monitoramento está desativado. O piloto de leitura
pode primeiro devolver diagnóstico sem persistência; eventual inclusão de novas
observações oficiais e seleção do produto exige autorização específica posterior.

**Feeds são opcionais, posteriores à consulta por identidade.** Antes de implementar
um adaptador, obter dicionário oficial das `columns`, tipos, unidade monetária,
identificadores, vigência e semântica de atualização. Uma amostra não substitui esse
contrato. Reutilizar transporte/assinatura e reservas; o mecanismo de coleta atual
autoriza `productOfferV2`, não feeds. Não inserir uma nova operação por fora dele.

## Armazenamento e comparação ao longo do tempo

- Reutilizar produto único por plataforma, `shopId:itemId` e variante confiável.
  Sem ID oficial de variante, manter escopo do anúncio não resolvido; não criar
  históricos separados para azul/vermelho por inferência.
- Manter `current_price` em Numeric/Decimal, moeda BRL vinculada à fonte brasileira
  confirmada, `source_type=OFFICIAL_API` e fonte estável específica da operação.
  **Definir explicitamente** acesso e disponibilidade UNKNOWN quando faltarem provas;
  não depender do default PUBLIC do modelo.
- API e os dois contextos documentais existentes não compartilham baseline.
  A primeira observação oficial tem referência própria e começa em INITIAL.
- Guardar preço/faixa anunciados sem escolher arbitrariamente variante, menor valor
  ou preço Pix. Quando não houver preço utilizável, preservar faixa informativa e
  manter a observação/candidata inelegível conforme o fluxo existente.
- Registrar momento UTC da resposta em `observed_at`, identificando-o como momento
  da coleta, não como verificação de compra. Guardar horário efetivo do dado somente
  se informado/documentado pela fonte; conservar precisão e origem.
- Propor `price_basis` versionado, derivado de contexto comercial validado:
  operação, escopo da variante, base de preço/faixa, pagamento, cupom/eligibilidade
  e região quando documentados. UNKNOWN não equivale a ausência de restrição.
  Mudança de contexto inicia outra baseline, sem queda entre contextos.
  Não incluir preço observado, percentual, comissão ou horário nessa identidade de
  contexto: sua mudança não pode impedir a detecção de uma variação legítima.
- Ajuste necessário: `comparable_context` já compara `price_range` e `price_basis`,
  mas `_safe_evidence` ainda **descarta `price_basis`**. Permitir/validar esse campo
  antes da recorrência e testar que nenhuma condição nova é ignorada. Os campos
  atuais `price_contexts`/`request_context` isoladamente não fazem essa comparação.
- Desconto anunciado e períodos permanecem metadados separados. Preservar os
  Int64 de período sem conversão temporal até confirmar unidade, semântica e sentinelas.
- Idempotência: reutilizar UUID/reserva da execução e unicidade por produto/fonte/
  fingerprint/horário. Reprocessar a mesma resposta mantém horário original e não
  cria outra linha. Nova coleta autorizada em outro instante pode registrar preço
  igual (UNCHANGED); não usar um novo horário para transformar replay em observação.
- Se feeds forem autorizados: guardar identidade/data/modo do feed, offset e versão
  da linha como proveniência; manter data do conjunto separada da coleta e do eventual
  horário do item. `date=YYYYMMDD` não informa atualização por minuto ou fuso. Reler
  a mesma versão não renova sua validade comercial. DELETE não apaga observações.
- `source_reference` e a allowlist de evidências hoje não aceitam referências de
  feed arbitrárias. Estender somente com formato validado e contrato confirmado;
  não contornar validações ou copiar `columns` desconhecidas para metadados.

## Confirmações oficiais ainda necessárias

Solicitar à Shopee, pelo suporte/documentação da conta: autorização e quotas por
operação/conta (frequência, burst, custo e política de atualização/retenção), acesso
a anúncios de terceiros, habilitação dos feeds, dicionário e versões das colunas,
unidades dos períodos e significado comercial de preço/percentual anunciado.

Perguntar explicitamente se há fonte oficial autorizável para **preço por modelo,
saldo/disponibilidade, pagamento, cupom e validade das condições**. Não especificar
endpoint de vendedor nem assumir acesso de afiliado sem contrato e consentimento
correspondentes. Se a resposta não fornecer esses dados, automatizar apenas
cotações; comprovação comercial continua pendente. Não substituir por scraping.

## Plano mínimo de testes reais futuros — nenhuma execução autorizada agora

Cada linha abaixo depende de autorização nova, com escopo, limite e permissão de
persistência definidos. Nenhuma inclui mutation, retry, fallback ou publicação.

| Etapa | Chamada e campos | Limite proposto | Evidência/condição para avançar |
|---|---|---|---|
| A — amostra do Cockpit | `productOfferV2(shopId:1904573791,itemId:23199869203,page:1,limit:1)`; `itemId shopId productName productLink imageUrl productCatIds price priceMin priceMax priceDiscountRate periodStartTime periodEndTime`, `pageInfo { page limit hasNextPage }` | **1 POST**, no máximo 1 nó, sem próxima página | Confirmar permissão vigente e campos; comparar IDs; Decimal/faixa/contexto; ausências continuam UNKNOWN. Resultado vazio/erro encerra, sem substituir produto. Primeira leitura sem escrita se essa for a autorização. |
| B — observação posterior | Mesma operação, seleção e identidade da etapa A | **1 POST adicional**, em outro instante e intervalo oficialmente permitido | Nova autorização pontual ou autorização recorrente delimitada; persistência explicitamente autorizada. Comparação informativa só no mesmo contexto; INITIAL/UNCHANGED/DROP/RISE conforme os dados. |
| C — metadados de feed, opcional | `listItemFeeds { feeds { datafeedId referenceId datafeedName description totalCount date feedMode } }` | **1 POST**, sem leitura de dados; o contrato não permite limitar a quantidade de metadados retornados | Confirmar habilitação da conta e quotas. Registrar IDs realmente retornados; não fabricar feed nem presumir que contém o Cockpit. |
| D — amostra de feed, opcional | `getItemFeedData(datafeedId:<ID retornado em C>,offset:0,limit:1) { rows { columns updateType } pageInfo { offset limit totalCount hasMore } }` | **1 POST**, até 1 linha, sem paginação | Autorização para o feed exato. Conferir chaves/tipos contra dicionário oficial; não ingestir campos sem contrato nem associar linha de outro anúncio ao produto 2. |

O piloto mínimo é **A**; uma série exige também **B**. C/D só se justificam para
avaliar lacunas que a documentação dos feeds possa resolver. Uma amostra FULL não
valida DELTA: teste desse modo exige um ID DELTA retornado e outra autorização
delimitada. A ausência de feeds ou erro de permissão encerra a etapa, sem fallback.

Antes de código real futuro: regressões offline de desconto 0/100/inválido,
períodos/unidades desconhecidas, Decimal, faixa/variante não resolvida, identidade,
preço ausente e contexto alterado; feeds JSON inválido/sem dicionário/UNKNOWN/DELETE,
replay e dados antigos. PostgreSQL isolado deve cobrir idempotência, concorrência,
contextos e reservas consumidas. Revalidar zero I/O real nos testes e bloqueio de
aprovação/publicação. Suíte, Ruff e Alembic serão necessários na implementação;
não foram repetidos nesta entrega documental.

## Estado desta entrega e próximo passo

Documentação e extrato local preparados; código, `.env`, credenciais, produto 2,
históricos 2/3, primeira mutation e políticas preservados. Nenhum acesso ao banco
operacional, chamada autenticada, mutation, scheduler ou publicação. Flags e grupo
não foram alterados nem reconferidos no banco; não se declara uma nova validação
operacional. Sem commit/push ou Fases 5/6.

**Próximo passo mínimo:** obter as confirmações oficiais de escopo/quota/semântica;
depois autorizar a pequena implementação observacional e **uma leitura por identidade
(A)**. A futura coleta recorrente exige autorização própria, limites documentados,
backoff/concorrência existentes e nenhuma liberação implícita de publicação.
