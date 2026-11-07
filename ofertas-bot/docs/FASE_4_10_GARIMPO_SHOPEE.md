# Fase 4.10 - descoberta Shopee exclusivamente offline

> Atualizacao: o adendo tornou os parametros operacionais preferencias dinamicas em AppSetting. Os numeros desta entrega inicial descrevem os valores iniciais/historicos; preferencias persistidas prevalecem. Veja o [relatorio consolidado e as rotas de configuracao](RELATORIO_FASE_4_10_E_ADENDO_CONFIGURACOES.md).

O motor reutiliza ShopeeConnector/ConnectorRegistry, CategoryService, filtros, coleta 4.8, historico, comparador, OfferEngine/OfferScoringService, fila simulada, templates, leases e o APScheduler existentes. Nao ha novo conector, publicador ou autorizacao comercial.

## Arquitetura e isolamento

`ShopeeDiscoveryService` separa DISCOVER, MONITOR e EVALUATE. A configuracao usa as categorias existentes; execucoes, achados e produtos selecionados possuem tabelas proprias na migracao aditiva `0009_shopee_discovery`. Achados registram pagina, identidade loja:item/variante, classificacao, motivos, pontuacao e referencias ao historico/candidata da coleta 4.8. Cada avaliacao registra tambem history_id, candidate_id, score e evaluated_at; a politica vigente e revalidada sem reaprovar estados anteriores. CONFIGURE registra alteracoes/ator; RECOVER registra recuperacao, sem repetir I/O.

Execucao exige as cinco flags globais false, banco isolado e ConnectorRegistry fornecido explicitamente, contendo ShopeeConnector com httpx.MockTransport no endpoint oficial. PostgreSQL exige exatamente ofertas_bot_test e schema UUID proprio; public e banco principal sao recusados. SQLite exige contexto de testes ou arquivo dedicado .simulation.db. A API principal nao recebe esse registro mockado e responde 409 a pedidos de execucao. Nao existe rota para ativar coleta real, autoaprovacao comercial ou envio. As tabelas novas de producao ficam vazias.

Dados com formato da API oficial em um replay conservam seu contrato/proveniencia no historico isolado; isso nao significa nova observacao real. Execucoes sao sempre OFFLINE, os exemplos sao ficticios e a fila utilizada e exclusivamente SIMULATED/PENDING. Publicacoes tecnicas e comerciais reais anteriores nao sao alteradas.

## Nove categorias e configuracao

GET /api/v1/shopee-discovery/status retorna IDs e nomes efetivos do banco, estado e configuracao opcional. Nao pressupor numeros de IDs nem associar categoria local a catId Shopee.

| Slug existente | Nome existente | Exemplo ficticio de termo / intencao |
|---|---|---|
| casa-decoracao | Casa e Decoracao | organizador / generic |
| materiais-ferramentas | Materiais e Ferramentas | furadeira / tool |
| eletrodomesticos | Eletrodomesticos | geladeira / generic |
| moda | Moda | camiseta / generic |
| infantil | Infantil | brinquedo / generic |
| tecnologia | Tecnologia | smartphone iPhone 15 / phone |
| games | Games | Console PlayStation 5 / console |
| beleza | Beleza | shampoo / generic |
| gerais | Gerais | objeto XYZ / generic |

Os nomes acentuados e IDs persistidos originais sao preservados; a tabela acima apresenta exemplos de configuracao, nunca novos registros de producao. A demonstracao cria essas mesmas nove categorias somente no banco SQLite novo.

PUT /api/v1/shopee-discovery/categories/{id}/policy recebe actor e policy. Exemplo exclusivamente offline:

```json
{"actor":"administrador laboratorio","policy":{"enabled":true,"searches":[{"query":"Console PlayStation 5","intent":"console","priority":2},{"query":"console","intent":"console","priority":1}],"exclusions":["recondicionado"],"category_priority":2,"page_size":2,"max_pages":2,"cooldown_seconds":900,"requests_per_hour":4,"destinations":[],"approval":{"min_score":"100","max_per_category":2,"max_per_destination":1}}}
```

`enabled` nesta politica permite apenas a selecao em execucoes offline seladas, nunca ativa integracoes. `automatic_approval` aceita somente false. IDs/booleanos/tamanhos/termos duplicados sao validados. `product_ids` OU frases `terms` preservam o filtro existente e sao combinados por E com categorias, exclusoes, max_price e min_discount verificado. A pontuacao considera relevancia, prioridades de categoria/termo, prioridade do produto e os criterios comprovados do OfferScoringService, incluindo cupons confirmados existentes. Comissao nao pontua como desconto.

## Descoberta, reconsulta e relevancia

DISCOVER escolhe uma categoria ativa e vencida por execucao. Categorias nunca visitadas e a ultima visita mais antiga precedem a prioridade, evitando monopolio. Termos giram em ordem de prioridade. Descoberta ampla nao substitui a reconsulta por identidade de MONITOR; esta usa produtos SELECTED vencidos, com prioridade e lease por produto.

Nomes/termos sao normalizados por palavras, sem inferir equivalencia aproximada. Intencoes console/phone/tool excluem conservadoramente cabo, capa, controle, pelicula ou broca avulsa conforme o caso. Titulos incompletos ou intencao nao comprovada ficam REVIEW. Intencao accessory e explicitamente separada; nao significa console. Correcoes manuais de categorias continuam soberanas. PATCH /api/v1/shopee-discovery/selections/{id}/decision registra ACCEPT/REJECT e ator; aceitacao humana pode resolver ambiguidade de identidade, mas nao elimina exclusoes, acessorios incompativeis nem comprovacao comercial. Rejeicao de novo preco/titulo na categoria responsavel pausa a selecao anterior, evitando aproveitar preco antigo. Uma rejeicao de outra categoria nao pausa a selecao valida; quando outra politica seleciona efetivamente o produto, sua categoria/termo tornam-se o contexto auditado da selecao.

## Limites, falhas e recuperacao

- Uma categoria por descoberta, lote de 1-5 produtos, 1-2 paginas. Somente keyword/page/limit documentados pelo conector; nunca catId local, cursor opaco, ordenacao ou filtros presumidos.
- Pagina seguinte depende de hasNextPage; next_page e term_index sao persistidos apos commit. Ao atingir max_pages, o termo avanca e a truncagem permanece registrada. Repeticao por loja:item/variante na mesma execucao nao ganha outro historico.
- Limite local global de 12 reservas/hora, 1-10 por categoria/hora, ate 2 por execucao e intervalo minimo local 900 segundos. Estes sao controles conservadores locais, NAO quotas oficiais da conta.
- Reserva duravel antes do POST, UUID idempotente, leases compartilhados com coleta 4.8 e fencing antes de persistir. Nao ha retry/fallback HTTP. Alterar configuracao nao apaga quota nem backoff.
- Rate limit/falhas aplicam backoff exponencial limitado a 24h. Autorizacao/erros GraphQL bloqueiam a categoria para revisao. Resposta GraphQL parcial nao e ingerida. Paginas anteriores ja confirmadas sao preservadas se a proxima falhar.
- Timeout/perda de lease ficam UNCERTAIN; repeticao do mesmo UUID devolve auditoria sem I/O. RECOVER marca reservas abandonadas como incertas apos 120s e posse dos leases; nao retoma nem reenvia.

Primeira observacao e INITIAL. Historico Decimal comparavel distingue DROP/RISE/UNCHANGED; variacao informativa nao comprova desconto publicavel. Faixa de preco/variante nao resolvida permanece REVIEW com preco utilizavel ausente. Cupom, estoque, preco publico, frete, parcelamento e disponibilidade desconhecidos nao sao inventados.

## Avaliacao e preparacao dry-run

EVALUATE revalida fatos atuais, termos/exclusoes, correcao de categoria, limiares e destino. Politica hipotetica exige queda comparavel fresca, estoque/preco publico verificados, link proprio verificado, relevancia sem ambiguidade, pontuacao minima e limites por categoria/destino/hora. Reutiliza deduplicacao, janela minima de tres horas e nova observacao da fila. Limites contam tambem previsoes anteriormente auditadas, nao apenas o lote atual.

Somente num cenario sinteticamente comprovado e destino SIMULATED habilitado pode registrar MOCK_WOULD_APPROVE, renderizar template/hash e criar item PENDING com DISCOVERY_DRY_RUN_ONLY. Isso e previsao auditada, NAO aprovacao real, SENT ou SIMULATED de despacho. Nao chama publisher, nao usa outbox tecnica e preserva slots/aprovacoes existentes. Um destino Telegram, mesmo habilitado no banco isolado, permanece bloqueado neste fluxo. O grupo real continua pausado.

## API e scheduler

Rotas locais com X-API-Key, sob /api/v1/shopee-discovery:

- GET /status, /runs, /findings, /selections (listagens limitadas a 100).
- PUT /categories/{id}/policy; PATCH /selections/{id}/decision.
- POST /offline-exercises com request_id UUID, operation DISCOVER/MONITOR/EVALUATE/RECOVER e actor. Rejeitado na API normal, que nao possui registro mockado.
- POST /schedule/prepare registra os tres jobs pausados: shopee-discovery-prepared, shopee-selected-prepared e shopee-evaluation-prepared. Callbacks comerciais retornam BLOCKED, inclusive se alguem retomar um job manualmente. Nenhuma rota de ativacao.

O laboratorio pode fornecer explicitamente transporte mockado e preparar callbacks offline no mesmo MonitoringRuntime. max_instances=1, coalesce e leases continuam presentes. Durante pytest nenhum scheduler inicia threads.

## Demonstracao automatica offline

```powershell
.\.venv\Scripts\python.exe -m app.demo_shopee_discovery
```

Cria um novo .runtime/discovery-UUID.simulation.db, usa credenciais sinteticas, nao le .env e encerra os tres jobs em finally. O relogio virtual avanca para respeitar quotas locais; apenas os intervalos do laboratorio sao acelerados. Nunca reutiliza o banco principal ou o grupo real.

[Resultado executado](DEMONSTRACAO_FASE_4_10.json): 9 ciclos de descoberta, 18 reconsultas, 1 avaliacao, 36 requisicoes MockTransport, 27 produtos distintos, 18 selecionados, 36 observacoes/candidatas. Na descoberta: 18 selecionados, 9 filtrados, 9 duplicados; as 18 reconsultas somam outros 18 registros SELECTED. Precos inteiramente ficticios 100.00 e 90.00. Zero preparacoes publicaveis: estoque/preco publico UNKNOWN e link proprio ausente. Zero consultas comerciais reais, zero mutations de afiliado, zero publicacoes Telegram. Os testes de evidencias futuras sinteticas demonstram template/hash/PENDING e limites; nao alteram o resultado real desconhecido.

## Capacidades futuras e implantacao

productOfferV2 e operacao de produto; shopeeOfferV2 de campanhas nao e automaticamente produto ou cupom. O contrato de descoberta/aplicacao de cupons nao foi comprovado: fetch_coupons continua UNVERIFIED. Campanhas, ordenacoes e quotas da conta sao pendencias, sem fonte operacional ficticia ou promessa de desconto/frete gratis.

Apos autorizacao futura: confirmar oficialmente limites e permissoes; definir termos reais com o responsavel; autorizar leitura pontual delimitada; comprovar estoque/acesso publico/variante e link proprio; revisar candidatas; obter autorizacao individual de conteudo/hash/destino antes de uma tentativa comercial. Operacao continua e novas politicas de aprovacao dependem de outra decisao. Mercado Livre permanece suspenso e Fases 5/6 nao iniciadas.

## Validacao

**1.267 testes aprovados**, incluindo **49 PostgreSQL reais**, zero falhas e zero ignorados; os 1.183 anteriores foram preservados e 84 novos acrescentados (73 offline e 11 PostgreSQL). Revalidacao dos 84 novos testes apos o ajuste final de contexto de categoria: todos aprovados. Contagens sobrepostas, nao somar. Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao passaram nos 150 arquivos Python; Alembic upgrade/current/check em 0009_shopee_discovery, sem drift. Fingerprints das 27 tabelas anteriores e .env identicos apos os testes; nove categorias ativas, grupo pausado, monitoramento desativado, cinco flags false e quatro tabelas novas de producao vazias. Zero novas consultas comerciais, mutations de afiliado ou publicacoes reais. Sem commit/push ou Fases 5/6.

[Evidencia sanitizada](VALIDACAO_FASE_4_10.json).
