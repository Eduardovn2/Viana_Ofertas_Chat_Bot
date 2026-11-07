# Relatorio - Fase 4.10 e adendo de configuracoes dinamicas

Projeto: Viana Ofertas / Viana_Ofertas_Chat_Bot. Validacao local no Windows, com PostgreSQL do Docker e APIs exclusivamente mockadas. Nenhuma autorizacao operacional real foi concedida ou reutilizada nestas duas entregas.

## 1. Fase 4.10 - resultado da entrega anterior

**Concluida e validada offline.** Foi implementado o motor de descoberta/garimpo Shopee aproveitando as Fases 1-4.9:

- Descoberta distribuida entre as nove categorias ja existentes, com multiplos termos, prioridades e exclusoes; sem mapear categorias locais para IDs Shopee.
- Relevancia deterministica por intencao: console, telefone, ferramenta e acessorio. Cabos, capas, controles, peliculas e brocas incompativeis nao sao considerados o produto principal. Ambiguidades seguem para revisao; correcoes manuais e identidade loja:item/variante sao preservadas.
- Lotes/paginas limitados, rotacao conservadora, quotas locais, UUID idempotente, leases/fencing, tratamento de timeout/rate limit/GraphQL parcial e recuperacao sem replay de tentativa incerta.
- Reuso da coleta/historico Decimal, comparador, pontuacao, classificacao, filtros, templates e fila simulada. Primeira observacao INITIAL; variacao informativa separada de desconto comercial verificado. Comissao nao e desconto.
- Separacao entre descoberta, reconsulta de selecionados e avaliacao de candidatas, no APScheduler existente. Jobs comerciais preparados pausados e com callbacks BLOCKED.
- Preparacao dry-run somente em banco isolado e MockTransport. Uma hipotese elegivel pode produzir MOCK_WOULD_APPROVE e fila PENDING, sem aprovacao/envio real ou uso da outbox tecnica.
- Migracao aditiva 0009_shopee_discovery, com quatro tabelas e downgrade protegido contra perda de dados. APIs administrativas autenticadas para configuracao e consulta de auditoria.

### Demonstracao executada

| Medida | Resultado |
|---|---:|
| Categorias existentes reutilizadas | 9 |
| Produtos ficticios distintos descobertos | 27 |
| Produtos selecionados | 18 |
| Registros filtrados na descoberta | 9 |
| Registros duplicados identificados | 9 |
| Observacoes/candidatas, incluindo reconsultas | 36 |
| Requisicoes Shopee mockadas | 36 |
| Preparacoes publicaveis | 0 |
| Consultas comerciais reais / envios Telegram reais | 0 / 0 |

Precos inteiramente ficticios: 100.00 e 90.00. Nenhuma candidata foi considerada publicavel, pois disponibilidade/acesso publico continuaram UNKNOWN e nao havia link proprio comprovado. Os testes separados com evidencias sinteticas verificadas exercitaram preparacao, template, hash e PENDING; nao comprovam que a API real fornece essas condicoes.

### Testes e preservacao anteriores

**1.267 testes aprovados**, incluindo **49 PostgreSQL reais**, sem falhas/skips. Os 1.183 anteriores foram preservados e 84 adicionados. Ruff e formatacao passaram em 150 arquivos; Alembic upgrade/current/check passou em 0009, sem drift. Fingerprints de 27 tabelas anteriores e .env foram preservados; quatro tabelas novas de producao ficaram vazias. Nove categorias ativas, grupo pausado, monitoramento desativado e cinco flags false. Nenhum commit/push ou Fase 5/6.

Evidencias historicas: [validacao 4.10](VALIDACAO_FASE_4_10.json), [demonstracao](DEMONSTRACAO_FASE_4_10.json) e [guia original](FASE_4_10_GARIMPO_SHOPEE.md). Essas contagens pertencem a entrega anterior e nao devem ser somadas as desta rodada.

## 2. Adendo - configuracoes dinamicas

**Implementacao e validacao concluidas, exclusivamente offline.** As preferencias operacionais usam AppSetting, sem tabelas ou migracao novas, e mantem as politicas/filtros de destinos existentes.

### Preferencias editaveis

| Configuracao | Comportamento |
|---|---|
| Dias e janelas | Lista de dias e intervalos; listas vazias fecham a operacao. Janelas noturnas pertencem ao dia de abertura. Inicio inclusivo e fim exclusivo. |
| Fuso | Nome IANA validado; valor inicial America/Sao_Paulo. Conversao local com ZoneInfo, sem mudar o Windows. |
| Frequencias | Descoberta, reconsulta e avaliacao, independentes. O coletor e compartilhado para evitar consultas duplicadas por destino. |
| Ofertas normais | Desconto percentual minimo e pontuacao minima, em Decimal. |
| Modo excepcional / caca-bugs | Ativacao independente, desconto e pontuacao minimos. O desconto excepcional nao pode ser inferior ao normal. Nao elimina evidencias, filtros, deduplicacao ou aprovacao real. |
| Categorias/produtos | IDs existentes do banco para categorias permitidas e prioridades de categoria/produto; nenhuma nova categoria de producao criada. |
| Consultas | Periodo de contabilizacao, limite global, por categoria e por execucao. Limite zero bloqueia consultas. |
| Preparacao para publicacao | Limites de previsoes dry-run por periodo, categoria e destino; limites zero bloqueiam. Limites nativos do destino continuam aplicaveis. |
| Falhas | Backoff inicial/maximo e prazo de lease configuraveis dentro dos limites tecnicos validados. Alterar frequencia nao apaga backoff ou reservas existentes. |

Os valores iniciais estao em app/discovery_operation_defaults.json. A operacao usa o estado persistido e validado; nao depende de editar codigo, .env, horario do Windows ou reiniciar o backend. As politicas anteriores por categoria continuam como compatibilidade para campos ainda nao substituidos explicitamente pela configuracao global.

### Escopo e independencia por destino

O coletor compartilha frequencias, limites de consultas, backoff e leases. Cada destino pode sobrescrever fuso, dias/janelas, limiares, modo excepcional, categorias, prioridades e limites de preparacao. Uma alteracao no destino A nao grava preferencias no B.

Herdar e explicito: campos nao sobrescritos seguem a configuracao global; enviar null em um override remove a sobrescrita e restaura a heranca. Uma alteracao global que invalidaria um override existente e recusada atomicamente.

As preferencias operacionais se somam aos filtros CategoryMode/DestinationCategory e SimulationPolicy ja existentes, incluindo produto/termo, preco maximo, desconto minimo e pausa da politica. A janela minima de tres horas, nova observacao e deduplicacao continuam obrigatorias. A outbox tecnica permanece separada.

### Aplicacao sem reinicio

Cada operacao carrega AppSetting novamente; nao ha cache de preferencias entre execucoes. Novas frequencias recalculam a elegibilidade de registros ja existentes, respeitando backoff/leases. Prioridades entram na ordenacao de reconsulta e na pontuacao efetiva, sem alterar manualmente Product.priority.

O backend que recebe a edicao atualiza os intervalos dos jobs ja preparados. Outros processos observam a mudanca pelo heartbeat existente do MonitoringRuntime. Atualizar um intervalo preserva jobs pausados; nao retoma scheduler nem troca callbacks BLOCKED por callbacks comerciais. Durante pytest nenhum scheduler inicia threads.

### Concorrencia e auditoria

Cada PATCH exige actor e expected_revision. A revisao global e a de cada destino sao independentes. Edicao obsoleta retorna HTTP 409; nenhuma sobrescrita silenciosa e feita. O lease do ciclo impede editar preferencias no meio de uma leitura/avaliacao em andamento; nesse caso a edicao e recusada para reapresentacao posterior.

Persistencia e auditoria CONFIGURE sao confirmadas na mesma transacao, com responsavel, destino, revisao e valores antes/depois. Falha de validacao/referencia/revisao nao grava configuracao ou auditoria parcial. Avaliacoes registram preferencias efetivas/revisoes, historico, pontuacao e classificacao NORMAL/EXCEPTIONAL.

### API administrativa

Todas as rotas exigem acesso local e X-API-Key ja existentes:

- GET /api/v1/shopee-discovery/preferences
- PATCH /api/v1/shopee-discovery/preferences
- GET /api/v1/shopee-discovery/destinations/{id}/preferences
- PATCH /api/v1/shopee-discovery/destinations/{id}/preferences

Exemplo de payload, apenas ilustrativo e nao aplicado ao banco principal:

```json
{"actor":"responsavel Viana Ofertas","expected_revision":0,"changes":{"timezone":"America/Sao_Paulo","working_days":[0,1,2,3,4],"windows":[{"start":"08:00","end":"22:00"}],"discovery_interval_seconds":120,"monitor_interval_seconds":60,"normal_min_discount":"5.00","exceptional_enabled":false,"exceptional_min_discount":"30.00","max_queries_per_period":8}}
```

Dias usam 0=segunda a 6=domingo. Primeiro consulte o GET e utilize a revisao atual. A resposta apresenta as preferencias efetivas, overrides, revisoes, audit_id e authorization_granted=false. Frequencias de coleta sao globais: nao sao aceitas como override por destino. IDs desconhecidos, limites negativos, tipos incorretos, fusos/janelas invalidos, percentuais nao finitos e campos de autorizacao sao recusados.

### Componentes alterados neste adendo

| Componente | Alteracao |
|---|---|
| app/discovery_operation_defaults.json | Valores iniciais fora da logica operacional. |
| app/services/discovery_operations.py | Schemas, janelas/fusos, heranca, revisoes e persistencia AppSetting. |
| app/services/shopee_discovery.py | Preferencias efetivas na descoberta, reconsulta, pontuacao e preparacao dry-run. |
| app/api/shopee_discovery.py | GET/PATCH global e por destino, autenticados. |
| app/jobs/runtime.py | Observacao de configuracao e atualizacao de intervalos preservando pausa. |
| tests/test_discovery_operations.py | Validacao, horarios, concorrencia, heranca, prioridades, limites e bloqueios. |
| tests/test_postgres_discovery_operations.py | Persistencia e concorrencia PostgreSQL em schemas isolados, com APIs mockadas. |
| README.md / ROADMAP.md | Estado atual e validacao; sem alterar evidencias historicas da fase anterior. |

## 3. Validacao desta rodada

- 73 testes de descoberta anteriores passaram apos a integracao inicial.
- 78 testes iniciais das preferencias/scheduler passaram.
- 164 testes direcionados passaram, incluindo nove novos PostgreSQL em schemas UUID de ofertas_bot_test; contagem sobreposta com a suite completa.
- Tres casos adicionais passaram para prioridade dinamica, teto por execucao e independencia dos limites por destino; tambem sobrepostos.
- Alembic current/check: 0009_shopee_discovery (head), sem drift; nenhuma migracao nova necessaria.
- **1.341 testes aprovados**, incluindo **58 PostgreSQL reais**, zero falhas/skips; os 1.267 anteriores foram preservados e 74 acrescentados (65 offline e nove PostgreSQL). Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao aprovados nos 154 arquivos Python; Alembic current/check em 0009_shopee_discovery, sem drift e sem migracao nova. Fingerprints das 31 tabelas anteriores e .env identicos apos a suite; nove categorias ativas, grupo pausado, monitoramento desativado e cinco flags false. Zero consultas comerciais, mutations de afiliado ou publicacoes reais. Nenhum commit/push ou Fases 5/6.

[Evidencia sanitizada desta rodada](VALIDACAO_ADENDO_FASE_4_10.json).

## 4. Seguranca e pendencias

As preferencias NAO concedem permissao para consultar APIs comerciais, gerar links, aprovar ofertas reais ou publicar mensagens. Campos que tentem habilitar integracoes/autorizacao sao rejeitados. O modo excepcional continua limitado a avaliacao offline. Execucao continua exigindo banco isolado, ConnectorRegistry/ShopeeConnector com MockTransport e controles de seguranca anteriores.

Permanecem false: SHOPEE_ENABLED, MERCADO_LIVRE_ENABLED, TELEGRAM_PUBLISHER_ENABLED, TELEGRAM_REAL_SENDS_AUTHORIZED e SIMULATION_AUTOMATION_ENABLED. Grupo Viana Ofertas (-5309308689) pausado; monitoramento principal desativado. Nenhuma consulta comercial, mutation de afiliado ou publicacao real nesta rodada. Credenciais e .env nao foram modificados. Nenhum commit ou push foi solicitado/executado; Fases 5/6 nao iniciadas.

Para operacao real futura ainda faltam autorizacao especifica, limites/permissoes oficiais da conta, disponibilidade/preco publico/variante comprovados, link proprio e aprovacao do conteudo/destino antes de tentativa real. Configuracoes dinamicas nao resolvem nem contornam essas pendencias.
