# Fase 4.7 — automação exclusivamente simulada

## Fluxo e componentes reaproveitados

`SimulationMonitoringRuntime` estende o `MonitoringRuntime`: usa o mesmo APScheduler, intervalos de prioridade/geral, coalescência, limite de uma instância por tick, leases, slots de provedor, backoff e persistência de jobs. O provedor é obrigatoriamente o `SimulatedPriceProvider`; opções de consulta real são recusadas.

O runtime insere observações pelo coletor existente, chama o `OfferEngine`, a comparação histórica, pontuação e classificação existentes e aproveita a fila `PublicationQueueItem`. A ponte `SimulatedAutomation` aplica os filtros adicionais, revalida com `SimulatedQueueService`, gera a mensagem com os templates existentes e reserva uma outbox separada antes do despacho mockado.

O `MockTelegramDelivery` instancia o **mesmo TelegramPublisher oficial**, exclusivamente com `httpx.MockTransport`, identidade/token sintéticos constantes e chat fictício. Nenhum token/allowlist real é reutilizado. Os controles do publisher real não foram modificados. A troca por transporte HTTP real é recusada antes do envio; não há scraping, consulta de loja ou rede Telegram nesta fase.

## Isolamento e bloqueios

- Por padrão `SIMULATION_AUTOMATION_ENABLED=false`. A operação principal mantém ambas as flags reais Telegram `false`, grupo Viana Ofertas pausado e conectores/monitoramento desativados.
- PostgreSQL exige o banco **ofertas_bot_test** e schema UUID próprio `ofertas_bot_*_test_<32 hex>`. `public` e o banco principal são recusados, mesmo com a flag de simulação habilitada.
- SQLite é permitido nos testes ou em arquivo dedicado com sufixo `.simulation.db`.
- Produtos precisam de inscrição explícita em `simulation_products`, nome iniciado por `[SIMULATED] ` e metadado `simulation_only=true`.
- Políticas são aceitas somente para destinos `SIMULATED`. Não configuram o grupo real nem seus filtros; as nove categorias e suas regras permanecem intactas.
- Observações devem ser públicas, comparáveis, atuais, de fonte `simulated` e `is_simulated=true`. A ponte exige queda respaldada por histórico próprio. Preço anterior anunciado isolado e cupom não fabricam oportunidades neste fluxo; esta extensão não usa cupons.
- Nenhuma observação fictícia entra na outbox real: o serviço manual anterior continua recusando histórico simulado, mesmo com detalhes manuais inseridos indevidamente.
- A outbox técnica da Fase 4.6 e suas autorizações não participam deste pipeline.

O módulo `simulation_safety` valida o contexto antes de ler/configurar/despachar. É possível preparar os novos modelos no banco principal por migração; isso não autoriza executar a simulação ali nem inserir produtos fictícios nessa base.

## Filtros e aprovação

`SimulationPolicy` complementa o filtro de categorias existente:

- `product_ids`: produtos fictícios específicos inscritos.
- `terms`: frases normalizadas sem distinção de maiúsculas/acentos, com limites de palavras; por exemplo `playstation 5`, `iphone 15` ou `ferramentas`. Não é regex executável.
- Um produto corresponde se estiver na lista **ou** corresponder a um termo. Listas vazias não restringem além das categorias. A condição continua combinada com categorias e demais validações por **E**.
- `max_price` e `min_discount`: campos opcionais já validados com Decimal e aplicados conservadoramente, preparados para futuros usos autorizados.
- `automatic_approval=false` e `enabled=false` por padrão. A aprovação automática, quando explicitamente configurada no ambiente isolado, registra `SIMULATED_AUTO_APPROVAL` e evento `AUTO_APPROVED_SIMULATED` do hash atual. Não modifica a aprovação obrigatória do publisher real.

Toda alteração de produto, preço, link, destino ou política muda o hash. O dispatcher compara a prévia e as condições atuais antes de reservar. Se mudaram, interrompe o despacho e exige revisão/revalidação; o próximo ciclo simulado só aprova automaticamente se a política isolada continuar autorizando.

## Outbox, concorrência e falhas

Novas tabelas aditivas: `simulation_products`, `simulation_policies`, `telegram_simulations`, `simulation_audit`. Migração **0007_simulated_automation**, com downgrade recusado quando há dados. O PostgreSQL usa JSONB para filtros/payload; Alembic verifica tipos/defaults sem ignorar divergências.

Estados próprios: `PENDING`, `READY`, `DISPATCHING`, **`SIMULATED`**, `FAILED`, `UNCERTAIN`, `EXPIRED`. Nunca é gravado `SENT` real neste fluxo. `is_simulated=true` é obrigatório no banco; identificadores de resposta começam por `simulated:`. Os logs comerciais/técnicos anteriores não são usados para registrar sucesso simulado.

Reutilizados leases por produto, locks na ordem destino → produto → fila → outbox e claim condicional de `READY/attempt_number=0`. Alterar uma política também exige o lock do destino, serializando a mudança com a revalidação e o claim; teste concorrente PostgreSQL comprova que a edição aguarda essa fronteira. Claim e evento `MOCK_ATTEMPT_RESERVED` são confirmados antes da chamada ao transporte mockado. A fila/outbox preservam unicidade por produto/destino; repetir um despacho concluído registra deduplicação, sem nova chamada. Uma resposta após expiração da reserva termina incerta. Concluir um destino não modifica o estado global/hash da oferta compartilhada com outros destinos.

Somente o sucesso mockado atualiza a fila simulada existente para `PUBLISHED`, alimentando sua janela de **no mínimo três horas** e exigência de nova observação posterior à publicação anterior. Cada registro da outbox admite uma tentativa. Timeout/resposta inválida mantêm `UNCERTAIN` e a reserva; falha definitiva fica `FAILED`. Não há retry/fallback do mesmo registro. Claims abandonados após o prazo da lease são recuperados como incertos, sem envio. Se persistir o resultado falhar, tenta-se apenas registrar a incerteza; indisponibilidade continuada preserva o claim durável para recuperação posterior.

Limites: lote padrão de **3**, configurável de 1 a 5; no máximo **5 tentativas por hora/destino**, respeitando limite menor e intervalo configurados no destino. Destinos com envio/resultado incerto permanecem bloqueados para novos despachos simulados. Uma rejeição não retoma o mesmo registro; eventual nova oferta exige nova observação, revalidação e limites. Não há operação comercial automática.

Foi corrigida uma incompatibilidade observada no driver psycopg: `INSERT ... ON CONFLICT DO NOTHING` retornava `rowcount=-1`, inclusive sem aquisição da lease. O mecanismo compartilhado agora confirma a aquisição/tomada de reserva com `RETURNING owner`, em vez de interpretar contagem desconhecida como sucesso. Interfaces e semântica anteriores foram preservadas e incluídas na regressão.

## API administrativa no ambiente isolado

Prefixo `/api/v1/simulated-automation`, autenticação local `X-API-Key` existente:

| Rota | Uso |
|---|---|
| `GET /status` | Métricas e indicação do modo exclusivamente simulado. |
| `PUT /products/{id}` | Inscrição de produto fictício existente com `prices`, lista de preços Decimal ou indisponibilidade `null`. |
| `PUT /destinations/{id}/policy` | Configura filtros, habilitação e aprovação automática simulada. Não aceita destino Telegram real. |
| `GET /publications` | Até 100 registros da outbox simulada, sem misturar publicações reais/técnicas. |
| `GET /publications/{id}/audit` | Até 100 eventos do registro. |
| `POST /publications/{id}/approve` | Aprovação simulada manual com `preview_hash`, `actor`, `confirmed=true`. |
| `POST /run` | Processa explicitamente filas simuladas pendentes; não liga o scheduler principal nem consulta lojas. |

Com a configuração `SIMULATION_AUTOMATION_ENABLED=true` **somente no processo isolado**, a aplicação usa o runtime especializado e o APScheduler existente. Pausa/resumo e frequências continuam nas rotas administrativas de monitoramento existentes. Os testes mantêm a proteção que impede iniciar threads do scheduler sob Pytest.

## Demonstração reproduzível

Na raiz do projeto, com PostgreSQL ativo e `ofertas_bot_test` existente:

```powershell
.\.venv\Scripts\python.exe -m app.services.simulation_demo
```

O comando cria um schema UUID **novo** no banco de testes, aplica migrações, cadastra dados explicitamente fictícios e executa o APScheduler em background por no máximo 30 segundos. Os intervalos acelerados de um segundo e preços abaixo são exclusivos dessa demonstração; não são gravados no `.env` nem na configuração principal. O scheduler é encerrado em `finally`, o schema fica preservado como evidência e variáveis de conexão são restauradas no processo. Nenhum schema/dado existente é apagado. Schemas anteriores de diagnóstico também foram preservados; a evidência final identifica o schema validado.

Resultado efetivamente observado em [DEMONSTRACAO_FASE_4_7.json](DEMONSTRACAO_FASE_4_7.json):

| Produto fictício `[SIMULATED]` | Histórico fictício | Observação fictícia | Resultado |
|---|---:|---:|---|
| PlayStation 5 | R$ 3.000,00 | R$ 2.700,00 | SIMULATED |
| Smartphone iPhone 15 | R$ 4.000,00 | R$ 3.600,00 | SIMULATED |
| Ferramentas furadeira | R$ 500,00 | R$ 450,00 | SIMULATED |
| PlayStation 4 | R$ 1.500,00 | R$ 1.300,00 | Filtrado por termo/produto |
| Sofá casa | R$ 1.000,00 | R$ 800,00 | Filtrado por categoria |
| Mouse sem queda | R$ 100,00 | R$ 100,00 | Sem oportunidade |

**5 oportunidades, 2 produtos filtrados, 3 enfileirados e 3 simulados.** Métricas contam produtos distintos com evidência na base da demonstração; eventos de aprovação podem ser mais numerosos quando uma observação nova exige revalidar uma prévia que aguardava o intervalo do destino. A evidência final registra cinco eventos de aprovação automática simulada e uma deduplicação. Repetir o despacho do primeiro registro manteve **3 → 3 chamadas mockadas**, sem chamada extra. **Zero chamadas reais ao Telegram nesta fase.**

O registro técnico real anterior `SENT/message_id=12` permanece preservado; ele não faz parte dessas contagens. No banco principal continuam nove categorias, grupo Viana Ofertas pausado, flags Telegram e conectores desativados, monitoramento pausado e nenhuma inscrição/outbox fictícia.

## Continuidade

Mercado Livre permanece suspenso pelo HTTP 403 e falta de autorização confirmada para consultar terceiros. Shopee aguarda liberação oficial da Open API. Integrar fontes comerciais ou operação real exigirá autorização específica, permissões válidas, revalidação das condições e decisão sobre aprovação/publicação; não basta trocar o provedor ou habilitar uma flag desta simulação. Credenciais anteriores preservadas. Por solicitação do responsável em 21/10, os campos Shopee foram configurados somente no `.env` local, ignorado pelo Git; `SHOPEE_ENABLED=false`, sem consulta à API ou liberação automática da integração. Fases 5/6 não iniciadas; nenhum commit final criado.


## Validação e pendência de ambiente — 21/10

Validação anterior confirmada: **1.093 testes aprovados na suíte completa**, incluindo 29 PostgreSQL reais; depois do último ajuste de serialização da política, **mais um teste PostgreSQL novo passou isoladamente**. Ruff passou e **131 arquivos** estão formatados. A tentativa de revalidação integral final em 21/10 foi interrompida após erros de conexão: o ambiente Windows isolado atual não enxerga o Docker no PATH/caminho padrão nem o PostgreSQL em `127.0.0.1:5432`. Portanto, **a suíte completa final de 1.094 testes e os 30 PostgreSQL juntos permanecem pendentes de ambiente**, sem declaração de sucesso dessa nova execução. A validação Alembic 0007 sem drift e a preservação dos dados foram comprovadas na execução anterior; não foram reconfirmadas nesta tentativa.

Para retomar sem retrabalho, disponibilizar PostgreSQL ao processo do Codex e executar a suíte completa com `TEST_DATABASE_URL` temporária apontando exclusivamente para `ofertas_bot_test`; os testes usam schemas UUID isolados. Reconfirmar `python -m alembic current` e `python -m alembic check` sem alterar credenciais. Não habilitar os conectores, as flags reais ou o destino real para esta validação.
