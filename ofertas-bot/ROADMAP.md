# Roadmap do Ofertas Bot

Fases 1, 2 e 2.1 aprovadas e preservadas. Conectores, OAuth e pipeline da Fase 3 implementados e preservados. **Estado atual: consulta automática de preços Mercado Livre temporariamente suspensa; operação manual autorizada; Shopee com leitura pontual autorizada `productOfferV2` confirmada (HTTP 200), ainda desativada; normalização de categoria zero corrigida e validada offline. Fases 4–4.8 implementadas; coleta Shopee validada offline, sem polling real; última regressão integral com APIs mockadas e PostgreSQL real: 1.183 testes aprovados.** Envios reais continuam sem autorização, com `telegram_publisher_enabled=false` e `telegram_real_sends_authorized=false`. Docker/Compose e PostgreSQL foram validados na continuação de 20/10/2026. Os registros abaixo conservam o histórico das decisões e validações anteriores.

## Fase 1 — Fundação

Concluída em **18/10/2026**, com 88 testes aprovados, 1 teste PostgreSQL pulado, validação em SQLite e compilação da migração para PostgreSQL. O Docker não estava disponível no `PATH` do ambiente de execução; o Compose e a integração com PostgreSQL real não foram executados. O teste PostgreSQL está preparado para execução opcional com `TEST_DATABASE_URL` em banco exclusivo com sufixo `_test`.

- [x] Estrutura Python 3.12+, FastAPI, SQLAlchemy e configurações por ambiente.
- [x] PostgreSQL local com Docker Compose e migração Alembic versionada.
- [x] Modelos iniciais para produtos, histórico, links, cupons, ofertas, destinos, publicações, monitoramento e configurações.
- [x] CRUD administrativo de produtos e links de afiliado manuais.
- [x] Ativação/desativação do monitoramento por produto.
- [x] Consulta manual ao provedor simulado e armazenamento do histórico.
- [x] API administrativa protegida por chave; servidor local por padrão.
- [x] Testes automatizados e instruções Windows/PowerShell.

O preço simulado não é uma oferta real. Nenhuma consulta comercial, descoberta automática, rotina agendada ou publicação é executada nesta fase.

## Fase 2 — Monitoramento, pontuação e fila simulada

**Concluída em 18/10/2026: 173 testes aprovados e 1 PostgreSQL real pulado.** O fluxo completo opera com dados simulados, incluindo pontuação, avaliação conservadora de cupons e fila local. Nenhuma consulta a loja ou publicação real foi implementada.

- APScheduler 3.11 integrado ao lifecycle, sem threads em testes, com pausa e retomada.
- Intervalos de 60/300 segundos para produtos prioritários/gerais, limite do provedor simulado, backoff e leases persistentes para concorrência.
- Configuração validada em `AppSetting` por API, incluindo frequência, validade dos dados, pesos e preços de teste.
- Comparação com histórico compatível, distinguindo queda, alta, estabilidade e preço anterior declarado pela loja.
- Exclusão de dados inválidos, indisponíveis, exclusivos, futuros ou desatualizados da avaliação de oportunidades.
- Pontuação configurável para queda comprovada, cupom confirmado, prioridade, desconto e disponibilidade.
- Cupons opcionais: cálculo somente com confirmação, produto elegível, validade, verificação recente e regras conhecidas suficientes.
- Ofertas elegíveis sem cupom; ausência de link manual mantém a oferta/fila pendente.
- Fila `PENDING`/`READY`/`PUBLISHED`/`FAILED`/`EXPIRED`, um item ativo por produto/destino, revalidação e janela mínima de três horas.
- Destino local `SIMULATED`; `/queue/{id}/simulate` registra o resultado sem enviar mensagens externas.
- Migração `0002_simulated_pipeline`, preservando a revisão 0001 e os dados existentes, com fila e leases novos.
- Testes unitários e de integração para o runtime, comparação, cupons, pontuação, fila e contratos HTTP; concorrência e relógio controlado sem iniciar o scheduler real nos testes.

Validação consolidada daquela fase: **173 testes aprovados, 1 pulado, Ruff e formatação aprovados**. Os 18 testes de migração incluem preservação dos registros da fase 1, zero divergências entre modelos e esquema, checks de fila e rollback SQLite após falha injetada no meio da migração. Alembic `upgrade head`, `current` e `check` passaram em SQLite isolado. O SQL PostgreSQL foi gerado offline. Naquela execução, o CLI Docker e a porta PostgreSQL local 5432 não estavam acessíveis ao processo; Compose e PostgreSQL real ficaram pendentes.

**Fluxo validado:** cadastro → observação base → queda simulada → pontuação → fila → simulação explícita, com testes e instruções locais revisados. **Critério de avanço:** aprovação explícita para a fase 3, além de credenciais e permissões verificadas das fontes reais.

## Fase 2.1 — Categorias e validação de infraestrutura

Parte de categorias concluída em **18/10/2026**, com **241 testes isolados aprovados e, em execução posterior, os 2 testes PostgreSQL reais aprovados**. Não há consultas a lojas, IA paga ou publicações reais.

- [x] Modelos `Category`, `ProductCategory` e `DestinationCategory`, origem/confiança/explicação e unicidades.
- [x] Migração incremental `0003_category_routing`, sem substituir revisões anteriores.
- [x] Seed idempotente de nove categorias e do destino `Ofertas de Casa`, local/desativado, em `selected` com oito categorias; `Gerais` não é selecionada.
- [x] Preservação das preferências existentes ao repetir o seed.
- [x] Classificação por correção manual, categoria confiável de loja já disponível, regras locais e fallback `Gerais`, nessa prioridade.
- [x] Regras JSON configuráveis, normalização de texto e reclassificação que preserva a correção manual.
- [x] API administrativa de categorias, associações, reclassificação e preferências dos destinos.
- [x] Modos `unconfigured`, `selected` e `all`, com padrão conservador também para destinos existentes.
- [x] Filtro por categoria ativa na fila e na simulação; múltiplas categorias aceitas não multiplicam entradas.
- [x] Expiração de itens ativos que deixam de atender ao destino, preservando auditoria e janela de três horas.
- [x] Teste PostgreSQL opcional com banco `*_test`, schema aleatório, migração 0003, constraints, seed, roteamento e `/health` sem threads.
- [x] Diagnóstico de infraestrutura reutilizável, sem imprimir saída sensível de `docker info` nem modificar o ambiente.
- [x] Suíte anterior preservada: 173 testes anteriores mais 68 novos, mantendo as verificações de deduplicação, concorrência e janela de três horas. Os destinos usados nos testes antigos foram configurados explicitamente em `all` para atender à nova política conservadora.
- [x] Ruff e formatação aprovados em 50 arquivos Python; 22 blocos PowerShell e o exemplo executável passaram no parser.
- [x] Alembic `upgrade head`, `current` e `check` aprovados em SQLite isolado na revisão 0003, sem divergências; seed executável repetido sem duplicações.
- [x] Executar os dois testes de integração no PostgreSQL real, incluindo migrações, categorias, seed, roteamento e `/health`, em schemas exclusivos do banco `ofertas_bot_test`.
- [ ] Verificar diretamente o CLI Docker/Compose neste contexto e registrar a inspeção do container de desenvolvimento.

**Estado do ambiente:** o usuário informou Docker Desktop instalado e engine **29.8.2** ativo via WSL 2 no seu PowerShell do Windows. Esse relato não substitui a execução dos comandos pelo agente. O processo Codex usa shell/caminho Windows; nele, os três comandos `docker --version`, `docker compose version` e `docker info` retornaram `The term 'docker' is not recognized`. O executável não foi localizado nos caminhos usuais consultados e o `.env` estava ausente. O bloqueio é acesso ao CLI nesse processo, sem evidência de falta de instalação do Desktop. Nenhum recurso Windows/WSL, credencial ou volume foi alterado.

**Validação real a concluir:** no PowerShell Windows em que `docker info` funciona, seguir o README para subir apenas `postgres`, aguardar `healthy`, executar Alembic até `0003_category_routing`, iniciar a API e confirmar `/health`. Em seguida, usar `ofertas_bot_test` e `TEST_DATABASE_URL` temporária para `pytest -m postgres`. O roteiro cria o banco somente se ausente e preserva o volume e o `.env` existentes.

**Nova tentativa final em 18/10/2026:** o `.env` passou a estar visível e foi carregado sem exibir segredos. O CLI Docker continuou indisponível no `PATH` e no caminho explícito solicitado. A conexão direta ao PostgreSQL configurado falhou com `OperationalError`; a verificação TCP no destino local confirmou conexão recusada. Por isso, Compose, migrações/seed reais, criação do banco de testes e Pytest PostgreSQL continuam pendentes. Não se repetiu a suíte isolada. Ruff e formatação passaram nos 50 arquivos Python, sem mudanças funcionais. O [relatório da tentativa](VALIDACAO_FASE_2_1.md) separa as evidências atuais dos 241 testes anteriores e não declara PostgreSQL validado.

`Gerais` é somente fallback, não uma permissão global. `all` ainda exige categoria ativa; `selected=[]` e `unconfigured` bloqueiam. `Ofertas de Casa` fica desativado até configuração explícita. A aprovação posterior da fase 3 está registrada na seção seguinte.

**Validação PostgreSQL concluída em execução posterior de 18/10/2026:** conexão real com PostgreSQL **17.11**, banco `ofertas_bot_test` já existente e identidade confirmada antes dos testes. `python -m pytest -q -m postgres` terminou com **2 aprovados, 0 falhas, 0 ignorados, 241 deselecionados**, em 8,01 segundos. A URL foi temporária no subprocesso, sem exposição de credenciais e sem operações de teste no banco principal. Os objetos preexistentes foram preservados. A suíte completa não foi repetida. O [relatório atualizado](VALIDACAO_FASE_2_1.md) registra a superação do bloqueio PostgreSQL anterior; Docker/Compose não foram reavaliados nesta rodada.

## Fase 3 — Fontes comerciais autorizadas

**Autorizada; implementação disponível, validação comercial bloqueada por acesso.**

- [x] Gate oficial anterior ao código em [docs/INTEGRACOES_FASE_3.md](docs/INTEGRACOES_FASE_3.md), com capacidades por operação e fontes/data.
- [x] Conectores independentes, flags opcionais, startup sem rede e provedor simulado preservado.
- [x] Mercado Livre: metadados + APIs atuais de preços/condições; ignorar preço legado e excluir VIP, condições ocultas e variantes não resolvidas.
- [x] Mercado Livre: Link Builder manual, identidade reconhecível validada e confirmação operacional explícita para dados reais.
- [x] Shopee: assinatura publicada no Explorer BR, `productOfferV2`, consulta e descoberta paginada; descoberta automática pequena e opt-in.
- [x] Shopee: `generateShortLink` com flag própria, sem retry de mutation, conferência de produto/conta e fallback manual para ambiguidade.
- [x] `Decimal`, preço anterior anunciado separado do histórico, origem/instante/fingerprint, metadados públicos limitados e variantes isoladas.
- [x] Evidência manual de cupom opcional, incluindo frete grátis/desconhecidos, sem prometer aplicabilidade ou preço final sem confirmação completa.
- [x] Classificação e preferências de destinos reutilizadas; correções manuais e `Ofertas de Casa` desativado preservados.
- [x] Monitoramento integrado ao score/fila dry-run; deduplicação e revalidação após três horas, sem publisher real.
- [x] Timeout, cache com instante original, retries limitados, jitter e `Retry-After` persistido por credencial; recusa de autorização persiste até check explícito bem-sucedido.
- [x] API administrativa local/autenticada para status, capacidades, consulta controlada, refresh real, descoberta, links e evidência manual.
- [x] Migração `0004_marketplace_provenance` preserva revisões/dados anteriores e recusa downgrade com perda de informações novas.
- [x] PostgreSQL de desenvolvimento migrado até 0004; `current/check` aprovados, seed idempotente de nove categorias e destino desativado confirmado.
- [x] Suíte completa com PostgreSQL real: **485 aprovados, 0 falhas, 0 ignorados**, incluindo quatro testes PostgreSQL em schemas exclusivos de `ofertas_bot_test`. Os 243 casos anteriores foram preservados; testes novos usam somente contratos sintéticos e HTTP mockado.
- [x] Ruff e verificação de formatação aprovados em 68 arquivos Python. Aviso conhecido de depreciação Starlette/httpx não impede os testes.
- [ ] Executar um teste comercial autorizado por plataforma e registrar identidade, timestamp e origem, sem publicação. Faltam `MERCADO_LIVRE_ACCESS_TOKEN`, `SHOPEE_APP_ID` e `SHOPEE_SECRET`, além das permissões oficiais e flags explícitas.
- [ ] Comprovar acesso apropriado para descoberta ampla Mercado Livre e cupons de terceiros; continuam não suportados/não verificados, sem endpoints inventados.

**Limites comprovados:** esquema Shopee consultado não comprova estoque, preço público, variante nem preço antigo. Informações permanecem desconhecidas e não geram elegibilidade por suposição. `shopeeOfferV2` foi documentado como campanha/coleção, sem convertê-lo em produto/cupom; o conector implementa `productOfferV2`. Nenhuma consulta comercial real foi feita nesta entrega. `.env` e credenciais existentes não foram modificados. Execução local Windows/PowerShell; nenhum ajuste global, reinstalação Docker ou publicação.

**Critério de avanço:** consulta demonstrada com acesso autorizado; nenhum endpoint privado ou contorno de proteção.

Evidências e limites detalhados em [docs/VALIDACAO_FASE_3.md](docs/VALIDACAO_FASE_3.md). A implementação está na branch local `codex/fase-3`, sem push.

### OAuth Mercado Livre — desenvolvimento autorizado

- [x] Callback `GET /oauth/mercadolivre/callback` e gateway isolado, com HTTPS real via Cloudflare Quick Tunnel gratuito. A URI exata fica no `.env` e no manifesto local, sem hostname fictício.
- [x] Authorization Code com `state` criptograficamente aleatório, uso único/expiração, vínculo com navegador e PKCE S256 quando habilitado.
- [x] Client ID/Secret somente no ambiente local; troca form em endpoint oficial, sem retry automático ou logs de códigos/tokens.
- [x] Tokens/verificador protegidos por DPAPI CurrentUser em SQLite privado fora do PostgreSQL/Git; DACL exata para usuário atual + SYSTEM.
- [x] Rotas administrativas autenticadas para criar o lançamento e consultar status sem segredos. API principal permanece local; túnel expõe só o callback.
- [x] Scripts de início, reuso, atualização apenas do gateway e parada por identidade do processo, sem alterar configurações globais.
- [x] Testes offline, proteção nativa Windows e isolamento público demonstrados; resultados em [docs/OAUTH_MERCADOLIVRE.md](docs/OAUTH_MERCADOLIVRE.md).
- [x] Consentimento real do proprietário, troca oficial e armazenamento concluídos em **18/10/2026 às 17:28:59 America/Sao_Paulo (20:28:59 UTC)**. Vínculo com navegador, `state`, PKCE S256 e consumo de lançamento/estado confirmados.
- [x] Inspeção real confirmou DPAPI CurrentUser, ACL restrita ao usuário atual + SYSTEM, expiração válida, fingerprint de Client ID compatível e ausência dos tokens em texto puro nos blobs cifrados.
- [x] Flag OAuth restaurada para `false`; comerciais Mercado Livre/Shopee `false` e `PRICE_PROVIDER=simulated` preservados. Nenhuma consulta comercial ou publicação executada.
- [x] Suíte completa posterior: **699 testes aprovados em 64,61 segundos**, incluindo quatro PostgreSQL reais em schemas isolados de `ofertas_bot_test`, com um `StarletteDeprecationWarning`.
- [x] Ruff e formatação finais aprovados em **88 arquivos Python**; `git diff --check` aprovado.
- [x] Proteção adicional dos scripts: gateway e proprietário do listener verificados antes do túnel, encerramento aguardado de launcher/filhos e rejeição de processos alheios. **19 verificações PowerShell isoladas aprovadas**, sem recriar o túnel real.
- [ ] Conectar os tokens OAuth ao provedor de preços e demonstrar uma consulta comercial autorizada; o token armazenado ainda não é usado por esse provedor.
- [ ] Implementar e testar renovação com rotação de refresh token antes de assumir operação contínua autenticada.

A URI temporária permanece somente no `.env` e no manifesto `.runtime/oauth-tunnel.json`; não é fixada neste roadmap. A autorização real acrescenta evidência ao fluxo OAuth e preserva o bloqueio de avanço comercial/publicação descrito acima. Detalhes em [docs/OAUTH_MERCADOLIVRE.md](docs/OAUTH_MERCADOLIVRE.md).

### Fase 3.2 — ponto de retomada após pausa

**Desenvolvimento pausado por solicitação do responsável em 18/10/2026, para economizar o limite de uso.** Não retomar automaticamente nem iniciar a Fase 4. Base funcional salva na branch local `codex/fase-3`, commit `da5c8d6`; este checkpoint acrescenta somente documentação.

**Concluído:** conectores Mercado Livre/Shopee, fluxo OAuth real Mercado Livre com `state`, vínculo ao navegador e PKCE S256, troca oficial e tokens protegidos por DPAPI CurrentUser/ACL. Migrações e funcionalidades anteriores preservadas, PostgreSQL na revisão `0004_marketplace_provenance`. Última validação: 699 testes aprovados (quatro PostgreSQL reais), 19 verificações PowerShell aprovadas, Ruff/formatação em 88 arquivos Python; um aviso de depreciação de terceiros. Esses resultados são da execução anterior; nenhuma suíte foi repetida durante a pausa.

**Encerramento:** gateway OAuth e Quick Tunnel gerenciados foram encerrados; manifesto removido e porta 8787 sem listener confirmados. Credenciais, tokens protegidos e dados PostgreSQL foram preservados. Nenhuma configuração global ou serviço Docker/PostgreSQL foi alterado. OAuth e integrações comerciais permanecem desativados; provedor simulado, sem garimpo real ou publicações. Não há teste ou implementação desta entrega em andamento.

**Pendências para retomar, nesta ordem:**

1. Conferir configuração e expiração do token por status/metadados seguros, sem imprimir segredos. A autorização real anterior já foi comprovada e não precisa ser refeita sem necessidade.
2. Conectar o armazenamento OAuth ao provedor Mercado Livre, mantendo flags comerciais desativadas por padrão.
3. Implementar e testar renovação com rotação segura do refresh token, incluindo concorrência, falhas e persistência protegida.
4. Validar consultas comerciais pontuais autorizadas Mercado Livre/Shopee com as permissões e credenciais necessárias, sem monitoramento automático ou publicação. As limitações oficiais das operações permanecem as registradas nesta fase.
5. Executar testes apropriados às mudanças e atualizar as evidências. Acesso comercial continua pendente; não considerar a Fase 3.2 operacional somente pela conclusão do OAuth.

Se for necessário novo consentimento, iniciar outro Quick Tunnel, usar a nova URI HTTPS real no `.env` e cadastrar exatamente essa URI no DevCenter antes do fluxo. A URL anterior deixou de ser utilizável após o encerramento; não reutilizá-la nem inventar endereço. Tokens permanecem no armazenamento Windows privado fora do Git; `.env`, `.runtime` e `.tools` continuam ignorados. O briefing preexistente `FASE_3.md` foi preservado sem alterações e permanece não rastreado.

### Fase 3.2 — retomada autorizada

**Retomada explicitamente autorizada em 18/10/2026.** O checkpoint de pausa acima permanece como registro do encerramento anterior. Esta autorização abrange integração OAuth → conector Mercado Livre, renovação segura e concorrente e validação pontual do item `MLB3810190843`, associado ao link manual informado pelo proprietário. Não autoriza Shopee, mineração automática ou publicação real.

- [x] Revisar fontes oficiais de refresh, identidade, metadados e preços, distinguindo documentação de permissão efetiva da conta.
- [x] Sondagem real em **18/10/2026 às 21:27:01 America/Sao_Paulo**: token válido, `/users/me` HTTP 200 com identidade/site MLB compatíveis; `/items/MLB3810190843` HTTP 403 sem causa específica comprovada, sem metadados/preços obtidos.
- [x] Conectar o armazenamento OAuth protegido ao conector Mercado Livre sem copiar tokens para código, `.env`, logs ou respostas administrativas; gerenciador e conector implementados e testados.
- [x] Testar refresh de uso único e rotação protegida, concorrência e falhas em cenários controlados: oito threads, três processos e falha de processo. Nenhum refresh real foi forçado; o token utilizado estava válido.
- [x] Runner pontual executado em **18/10/2026 às 21:32:48.703151 America/Sao_Paulo**: `FAILED / AUTHORIZATION_DENIED / HTTP 403` em `/items/MLB3810190843`; `/sale_price` e `/prices` não foram chamados. Causa específica não comprovada.
- [x] Evento `manual_commercial_check` com estado `FAILED` e configuração manual do link persistidos no PostgreSQL, sem criar produto, preço, oferta ou fila presumidos. Seis testes unitários do runner e dois novos casos PostgreSQL aprovados na suíte completa.
- [x] Reteste em **18/10/2026 às 21:35:43 America/Sao_Paulo**, após revisão de permissões pelo proprietário: HTTP 403 persistiu, sem preços obtidos.
- [ ] Obter acesso apropriado no DevCenter e demonstrar metadados/preços reais do único item autorizado, sem presumir que a autenticação já concede essa permissão.
- [ ] Verificar a persistência PostgreSQL, categorização, histórico e resultado da fila dry-run, respeitando elegibilidade e deduplicação existentes.
- [x] Suíte final: **755 testes aprovados, sem falhas/skips, em 136,34 segundos**, com um `StarletteDeprecationWarning`. Seis testes PostgreSQL reais em schemas exclusivos de `ofertas_bot_test`: quatro anteriores e dois novos do runner, usando contratos sintéticos para preços/produtos.
- [x] Ruff e formatação aprovados em 94 arquivos Python; Alembic `current/check` confirmou PostgreSQL real na revisão 0004 sem divergências.
- [x] Novo consentimento Authorization Code concluído aproximadamente às **21:40–21:41 America/Sao_Paulo** após cadastro da URI e revisão de permissões pelo proprietário; tokens DPAPI atualizados e registros de `state`/PKCE consumidos. Isso não foi uma execução do grant de refresh.
- [x] Reteste com o novo token em **18/10/2026 às 21:41:28.303381 America/Sao_Paulo**: `FAILED / AUTHORIZATION_DENIED / HTTP 403` persistiu no item; sem chamadas de preços, preço legado ou scraping.
- [x] Prova final: `/users/me` HTTP 200 com novo token/identidade compatível, tokens ausentes em texto puro no SQLite, zero tentativas OAuth pendentes e refresh em estado pronto. PostgreSQL confirmou evento recusado, configuração manual preservada e zero produto/histórico do item.
- [x] Registrar configuração final e próximos passos no [relatório da Fase 3.2](docs/VALIDACAO_FASE_3_2.md), preservando as evidências anteriores.
- [x] Parser final aprovou 31 blocos PowerShell de README, guia OAuth e relatório da Fase 3.2.
- [ ] Comprovar permissão funcional e condições do programa Mercado Livre, ou esclarecer a recusa via suporte oficial, antes de outra tentativa comercial; não repetir OAuth sem mudança de acesso identificada.

**Estado desta retomada:** integração OAuth/conector implementada e testada; consulta comercial **não operacional por HTTP 403**, inclusive com novo token, sem causa exata atribuída. PostgreSQL conectado na revisão 0004, scheduler persistido em pausa, OAuth e flags comerciais Mercado Livre/Shopee desativados e provedor simulado preservado. O runner ativa Mercado Livre somente em memória para a consulta explícita. Os tokens usados estavam válidos; refresh real não foi exercitado. Novo consentimento concluído por uma API temporária atualizada em `8001`, já encerrada; a instância preexistente `8000` foi preservada. Novo gateway `8787`/Quick Tunnel seguem ativos, com URI somente no `.env` e no manifesto local. Shopee permanece sem credenciais. Os 699 testes e o OAuth real descritos acima pertencem à execução anterior; a suíte nova de 755 testes está registrada nesta seção. A Fase 4 não foi iniciada.

### Fase 3.2 — diagnóstico de acesso em 18/10/2026

- [x] Conferir permissões efetivas por leituras oficiais de aplicativo e grant: ambos HTTP 200, coerentes com o token, incluindo `urn:ml:all:publish-sync:/read-only` e `urn:ml:mktp:publish-sync:/read-only`. Aplicação ativa, `sandbox_mode=false`, sem scopes `urn:mp:`.
- [x] Confirmar token válido e identidade MLB por `/users/me` HTTP 200, sem renovar tokens ou repetir OAuth.
- [x] Consultar anúncios do próprio usuário com `limit=1`: HTTP 200, sem itens retornados. Comparação com anúncio próprio indisponível; nenhum anúncio foi criado para o diagnóstico.
- [x] Identificar a recusa do alvo: `/items/MLB3810190843` HTTP 403, `error=access_denied`, em 18/10/2026 às 21:56:25 America/Sao_Paulo. Request ID sanitizado registrado no relatório; não foi identificado o código `PA_UNAUTHORIZED_RESULT_FROM_POLICIES`.
- [x] Preparar [relatório técnico para suporte](docs/DIAGNOSTICO_403_MERCADOLIVRE.md), sem enviar mensagens externamente. Consultas restritas a GET, sem scraping ou tentativas de contorno.
- [ ] Obter do Mercado Livre a política específica que nega o recurso. A permissão funcional de leitura foi confirmada; propriedade do anúncio e regras de acesso de afiliados continuam sem comprovação conclusiva. Não há fonte oficial localizada que estabeleça proibição ou autorização geral para afiliados consultarem terceiros.

Nenhuma funcionalidade, migração, configuração ou teste aprovado foi alterado nesta rodada. Os **755 testes anteriores** permanecem como última validação da implementação; não foram repetidos no diagnóstico documental. Garimpo, integrações comerciais e publicações continuam desativados. Fase 4 não iniciada.

### Decisão operacional — suspensão comercial temporária em 18/10/2026

- [x] Registrar a suspensão temporária da consulta automática de preços Mercado Livre: HTTP 403 persistente e autorização não confirmada para ler anúncios de terceiros. Não confundir a leitura funcional `publish-sync` concedida com autorização demonstrada para esse recurso.
- [x] Preservar integralmente conector, OAuth, armazenamento protegido e renovação de tokens, migrações, testes, diagnósticos e histórico de implementação. Nenhuma funcionalidade será excluída para aplicar esta decisão.
- [x] Definir Mercado Livre em modo manual: cadastro de produtos, preços informados pelo responsável, cupons opcionais e links próprios gerados no Link Builder oficial. Não gerar links por métodos não autorizados nem completar campos ausentes por suposição.
- [x] Manter a Shopee desativada e aguardando liberação da Open API e credenciais oficiais; não executar descoberta, consultas comerciais ou geração de links automaticamente.
- [x] Permitir o planejamento da Fase 4 com ofertas manuais, independentemente da liberação da integração comercial automática. O critério antigo de avanço da Fase 3 aplica-se à retomada comercial automática, não bloqueia esse planejamento manual.
- [ ] Retomar a consulta automática Mercado Livre somente depois de comprovar o acesso autorizado, esclarecer o HTTP 403 e receber nova autorização operacional do responsável. Não repetir OAuth ou consultas de diagnóstico sem necessidade.

As pendências comerciais anteriores permanecem em espera, sem declarar a Fase 3.2 comercial concluída. O scheduler continua em pausa e as integrações comerciais desativadas. Esta atualização altera somente o planejamento; não modifica credenciais, tokens, dados ou configurações de execução. A última suíte aprovada permanece em **755 testes**, incluindo seis PostgreSQL reais; nenhuma suíte foi repetida nesta atualização documental.

## Fase 4 — Publicação no Telegram com ofertas manuais

**Implementação autorizada, com destinos administrativos, fluxo manual, outbox e publisher integrados. Envios reais bloqueados até autorização explícita.** Os componentes anteriores foram preservados; os checkpoints abaixo distinguem a primeira retomada limitada de cadastro/prévia/aprovação da continuação do publisher e da validação PostgreSQL. A primeira publicação futura deve usar somente o grupo de testes definido pelo responsável. Implementar ou aprovar uma oferta não autoriza seu envio.

### 4.1 — Entrada manual e preparação da oferta

- [x] Reutilizar produtos, histórico, cupons, categorias e links manuais existentes. Serviço `create_manual_offer` registra origem `MANUAL`, valor `Decimal` e responsável/data da verificação; não converte preços simulados em reais nem consulta provedores.
- [x] Exigir nome, preço atual válido e link de afiliado próprio operacionalmente verificado. Disponibilidade e acesso público exigem confirmação explícita; verificação recente e preço anterior anunciado permanecem separados do histórico próprio.
- [x] Manter cupom, preço anterior, imagem e parcelamento opcionais. Ausências não geram condições inventadas. Cupom informado precisa estar confirmado, público, elegível e vigente; preços VIP/exclusivos/condicionais/desconhecidos são recusados. Reverificação acrescenta nova oferta/observação, preservando os registros anteriores.

### 4.2 — Templates dinâmicos e prévia

- [x] Integrar o renderizador determinístico existente com nome, preço atual, link próprio e identificação publicitária; incluir somente os campos opcionais disponíveis e válidos.
- [x] Omitir integralmente blocos de cupom e preço anterior quando ausentes; desconto anunciado usa somente os valores informados e histórico próprio usa observações manuais comparáveis.
- [x] Preparar variantes com texto e imagem do produto. URL insegura é recusada no cadastro; imagem ausente usa texto. A opção explícita `include_image=false` permite omitir uma imagem antes do envio e exige nova aprovação se o conteúdo mudar; não há fallback após resultado incerto.
- [x] Escapar conteúdo dinâmico, validar URLs e tratar limites de texto/legenda. Prévia persistida com hash por destino; aprovação exige esse hash e confirmação explícita. Mudança válida do conteúdo exige nova revisão; evidência vencida/inválida ou filtro incompatível expira a entrada ao revalidar. Repetir a aprovação não sobrescreve o responsável original.

### 4.3 — Grupo de testes e publisher Telegram

- [x] Integrar publisher pela API oficial, desativado por padrão; token somente na configuração local `SecretStr`, sem exposição em logs, respostas ou versionamento. Testes com `httpx.MockTransport`, sem broadcast pago.
- [x] Cadastrar explicitamente **Viana Ofertas, Chat ID -5309308689**, pausado e com aprovação obrigatória. Preparação idempotente por API/CLI, filtros conservadores e limites de 60 s/20 tentativas por hora. Não altera allowlist, credenciais, outros destinos nem cria grupos no Telegram.
- [x] Separar simulação, prévia e despacho efetivo. A rota pontual `/dispatch` exige confirmação, hash aprovado, duas flags globais, allowlist, token e revalidação; não há publicação automática. Flags reais continuam `false`.
- [x] Suportar foto/texto e diagnósticos sanitizados; timeout, erro de transporte, HTTP 5xx ou resposta incompatível mantêm `UNCERTAIN` e slot reservado, sem retry/fallback automático.

### 4.4 — Categorias, filtros e fila

- [x] Reutilizar categorias inteligentes, correções manuais e preferências por destino, com categorias ativas e modos conservadores. `Ofertas de Casa` não é ativado. Rotas novas e antigas invalidam aprovações ao alterar o destino/filtros.
- [x] Revalidar categoria, filtros, preço, link, frescor, cupom e aprovação antes do despacho. Preservar fila simulada e usar a outbox Telegram independente já modelada na revisão 0005.
- [x] Preservar deduplicação por produto (identidade de plataforma/variante) e destino, inclusive em concorrência SQLite e PostgreSQL. Republicar exige **três horas**, observação manual posterior ao sucesso e aprovação nova. Tentativas e limites são persistidos antes do HTTP.
- [x] Nenhuma etapa consulta lojas: Mercado Livre automático suspenso, Shopee e conectores comerciais desativados. Scheduler não aciona publisher.

### 4.5 — Auditoria e testes

- [x] Registrar mensagem aprovada, ator, destino, hash, número/horário da tentativa, resultado e ID confirmado, sem credenciais. `recover` preserva claims abandonados; `resolve` exige ator/evidência/resultado explícito e conserva o aprovador original.
- [x] Testar templates, opcionais, escape, URLs, limites, escolha explícita de texto e alterações posteriores à aprovação.
- [x] Testar categorias/filtros, gates, aprovação por destino, concorrência, janela de três horas, expiração, HTTP 400/401/403/429, timeout, resposta incerta, falha de processo/persistência e recuperação sem enviar.
- [x] Mockar a API Telegram, sem mensagens reais ou chamadas comerciais externas. Preservar os 755 testes anteriores; suíte integral atual com **982 aprovados**, incluindo **12 PostgreSQL reais**, zero falhas/skips. Ruff e formatação aprovados; detalhes e avisos no fechamento abaixo.

**Critério de entrega:** fluxo manual → prévia → aprovação → fila → registro demonstrado em testes/simulação; envio pontual no grupo de testes somente após autorização explícita do responsável. Expansão para outros destinos ou operação sem aprovação inicial exige decisão posterior. A integração comercial automática suspensa não é requisito para essa entrega manual.

### Checkpoint da retomada — 20/10/2026

Rotas locais protegidas por `X-API-Key`: `POST/GET /api/v1/manual-offers`, `GET /api/v1/manual-offers/{id}`, `POST /api/v1/manual-offers/{id}/preview`, `GET /api/v1/telegram/publications` e `POST /api/v1/telegram/publications/{id}/approve`. Listagens possuem paginação e filtros por produto/destino. Oferta permanece `PENDING_REVIEW`; a aprovação é registrada separadamente em `TelegramPublication.APPROVED`, por destino. Prévia reutiliza um único slot ativo e não cria logs de envio ou itens na fila simulada anterior.

Nesta retomada não há endpoint de envio, alteração de credenciais, ativação do scheduler ou alteração dos conectores suspensos. O `.env` foi lido sem exposição de segredos e confirmou ambas as flags Telegram em `false`. Nenhum destino real foi cadastrado/habilitado e nenhuma mensagem foi enviada. A revisão 0005 existente é exercitada somente em bancos temporários; o banco principal não recebeu migrações nesta execução.

**Validação concluída deste escopo:** regressão pertinente com **424 testes aprovados e cinco casos PostgreSQL ignorados** (203,71 s), cobrindo APIs anteriores, domínio, categorias/filtros, histórico/ofertas, fila simulada, migrações e templates/publisher com mocks. Após os ajustes finais de validade do cupom e configuração persistida de frescor, os **57 testes de ofertas manuais/API passaram** (65,03 s); esse conjunto sobrepõe a regressão e os números não devem ser somados. Os avisos são de depreciação de dependências (Starlette/httpx e adaptador datetime SQLite). Ruff e formatação passaram nos 106 arquivos Python; `git diff --check` passou. Nenhum teste existente foi excluído.

A primeira tentativa isolada de Pytest encontrou bloqueio de criação de temporários no sandbox; a execução autorizada fora dele, com temporários na pasta `.pytest_cache` do projeto, passou. A conexão exclusiva a `ofertas_bot_test` falhou com `OperationalError`, antes de iniciar os casos reais; os cinco skips não representam validação PostgreSQL. Foram preparados dois testes novos para serviço/API e concorrência de prévias no PostgreSQL, além dos contratos da revisão 0005 já existentes. Os **755 testes anteriores** permanecem como evidência histórica; a suíte integral não foi repetida neste escopo limitado.

**Pendências da Fase 4:** concluir configuração administrativa dos destinos Telegram e validar o publisher/outbox completo (envio confirmado, falhas, resultado incerto e recuperação), além da regressão integral e PostgreSQL real. Os componentes existentes de envio foram preservados; ainda não constituem uma entrega operacional completa. Não criar commit final da Fase 4 com essas pendências. Fases 5 e 6 não iniciadas.

### Continuação com Docker iniciado — 20/10/2026

Código anterior preservado, sem recriar cadastro/prévia/aprovação. Rotas de destinos, status, despacho pontual, logs, recuperação e resolução integradas ao publisher. Configuração do destino invalida revisões pendentes e nunca libera `SENDING/UNCERTAIN`; bloqueios seguem destino → produto → outbox. Falha de persistência após o HTTP conserva a reserva para resolução manual.

**Infraestrutura efetivamente validada após o responsável iniciar o Docker:** Docker Engine **29.8.2**; Compose `postgres` **running/healthy**; PostgreSQL principal conectado. Alembic `upgrade head`, `current` e `check` passaram na revisão **0005_telegram_outbox**, sem drift. Contagens dos dados existentes conferidas antes/depois da migração e preservadas. Banco `ofertas_bot_test` já existente confirmado, sem excluir bancos/volumes/tabelas existentes.

**Grupo persistido:** preparação CLI retornou destino **id 2**, nome **Viana Ofertas**, Chat ID **-5309308689**, `enabled=false`, `manual_approval_required=true`. O grupo permanece sem filtro habilitado e sem permissão global de envio. Nenhum contato com a API Telegram real ocorreu. `.env` e credenciais não foram modificados; as duas flags Telegram, Mercado Livre e Shopee foram confirmadas em `false`.

**Testes desta continuação, sem somar execuções sobrepostas:** regressão pertinente **334 aprovados, sete PostgreSQL ignorados** antes da disponibilidade do banco; conjunto final Telegram/manual **205 aprovados**. Após iniciar Docker, **12 testes PostgreSQL reais aprovados**, zero falhas/skips, incluindo migração, metadados/constraints, cadastro pausado, prévias concorrentes e um único despacho concorrente com publisher oficial mockado. Cada caso usou schemas temporários em `ofertas_bot_test`. As antigas pendências de conexão/migração/preparação foram resolvidas.

**Fechamento técnico da Fase 4, código e mocks:** a suíte integral `pytest -q --tb=short` aprovou **982 testes em 268,29 s**, **zero falhas e zero skips**, incluindo os 755 anteriores e os 12 PostgreSQL reais. `TEST_DATABASE_URL` existiu somente no subprocesso e apontou exclusivamente para `ofertas_bot_test`. Os 17 avisos são de depreciação de dependências (Starlette/httpx e adaptador datetime SQLite), sem falhas funcionais. Ruff, formatação e `git diff --check` aprovados. O banco principal preserva nove categorias, `Ofertas de Casa` desativado, scheduler pausado e outbox Telegram vazia; somente o destino Viana Ofertas foi preparado, sem ativação.

Não há autorização para mensagens reais, inclusive no grupo de testes. Acesso real do bot, token/allowlist quando necessários, seleção de filtros e envio pontual permanecem para uma operação futura explicitamente autorizada. Isso não foi presumido pela validação com mocks. Nenhum avanço às Fases 5/6 e nenhum commit final criado nesta execução. Contratos e recuperação em [docs/TELEGRAM_FASE_4.md](docs/TELEGRAM_FASE_4.md).

**Salvamento Git — 20/10/2026:** após a validação integral, o responsável solicitou um commit local para salvar o progresso na branch `codex/fase-3`. Inclui implementação, migração, testes e documentação, preservando os diagnósticos anteriores. Credenciais, `.env`, bancos e arquivos de runtime continuam fora do versionamento. Não há autorização para push ou publicação real; as flags e pausas permanecem preservadas.

### Conexão Telegram validada, sem envio — 20/10/2026

Token local presente e preservado. Somente `getMe`, `getChat` e `getChatMember` foram consultados na API oficial: **HTTP 200 nos três métodos**, identidade **@VianinhaOfertasBot**, grupo **Viana Ofertas / -5309308689**, bot com status **administrator**. Não se executou `sendMessage` ou `sendPhoto` nem se testou uma publicação real.

Corrigido apenas o formato não secreto de `TELEGRAM_ALLOWED_CHAT_IDS` no `.env`, de escalar para lista JSON do mesmo Chat ID. No PostgreSQL, destino id 2 passou a `category_mode=all`, aceitando as nove categorias ativas, com aprovação obrigatória e `enabled=false`. Catálogo e demais destinos/associações preservados; nenhum registro de publicação ou oferta fictícia acrescentado. As duas flags Telegram e os conectores comerciais continuam `false`.

[Prévia exata da simulação](docs/PREVIA_TESTE_TELEGRAM.txt) preparada localmente, sem produto/preço/link inventado e sem inserção na fila de ofertas reais. Falta autorização explícita do conteúdo/grupo para um único envio, com registro da tentativa e restauração dos bloqueios após a operação. A base de 982 testes foi preservada; não se repetiu a suíte integral nesta validação sem alterações funcionais. Evidências sanitizadas em [docs/VALIDACAO_CONEXAO_TELEGRAM.md](docs/VALIDACAO_CONEXAO_TELEGRAM.md). Nenhum avanço às Fases 5/6.

### Tentativa técnica autorizada, bloqueada antes do envio — 20/10/2026

Recebida autorização explícita para uma única tentativa da prévia de simulação no grupo **Viana Ofertas / -5309308689**. **Status: BLOCKED; zero chamadas de publicação.** A outbox comercial exige oferta/produto e não suporta mensagem técnica independente. Aplicado o item 4 da autorização: nenhum produto/oferta fictício, envio direto ou adaptação automática foi realizado.

Consulta de leitura ao PostgreSQL confirmou ausência de tentativa anterior do conteúdo e outbox vazia no destino. Allowlist correta, destino pausado, aprovação obrigatória, demais destinos desativados e ambas as flags Telegram `false`, sem alteração do `.env`. Mercado Livre e Shopee permanecem desativados. Nenhuma mensagem enviada e nenhuma chamada ao Telegram nesta execução.

Pendência proposta: publicação técnica persistida com conteúdo/hash, aprovação, reserva, limite de uma tentativa e auditoria, reutilizando os controles do publisher oficial e preservando as constraints das ofertas comerciais. A adaptação não foi implementada nem autoriza envio alternativo. Evidência em [docs/AUDITORIA_TESTE_TELEGRAM_BLOQUEADO.md](docs/AUDITORIA_TESTE_TELEGRAM_BLOQUEADO.md). Base anterior de 982 testes preservada; sem reexecução neste diagnóstico documental. Nenhum avanço às Fases 5/6.

### Fase 4.6 — publicação técnica controlada — 20/10/2026

Implementada outbox técnica administrativa separada, sem produtos/ofertas fictícios nem campos comerciais. Migração aditiva `0006_telegram_technical` cria publicações e auditoria técnicas; preserva tabelas, migrações e constraints anteriores e recusa downgrade com dados. A publicação comercial e sua janela de três horas permanecem preservadas.

Rotas locais autenticadas de prévia, listagem, consulta, auditoria, aprovação do hash/destino exatos, despacho explícito e recuperação. Reutilizados destino, allowlist, validação da mensagem, controles globais e publisher oficial. Configuração alterada exige nova revisão; ator original e eventos históricos preservados. Chave permanente por conteúdo/destino, reserva transacional antes do HTTP, locks PostgreSQL e atualização condicional limitam a uma tentativa. Timeout/resposta ambígua/falha de persistência mantêm reserva; não há retry/fallback nem job automático. Mensagem técnica nunca entra na fila comercial ou no monitoramento.

**Não há autorização de envio nesta implementação.** A autorização anterior do teste bloqueado não foi reutilizada. Ambas as flags Telegram permanecem `false` e o grupo pausado; Mercado Livre/Shopee e monitoramento não foram ativados. [Prévia e procedimento de nova autorização](docs/TELEGRAM_FASE_4_6.md). Fases 5/6 não iniciadas.

**Validação concluída:** suíte completa pela CLI `python -m pytest`: **1.026 aprovados em 206,51 s**, zero falhas/skips, incluindo os 982 anteriores e 44 novos. Execução separada `-m postgres`: **20 aprovados em 35,02 s**, 1.006 não selecionados, exclusivamente em schemas UUID de `ofertas_bot_test`. Os resultados se sobrepõem e não devem ser somados. Telegram exclusivamente mockado. Ruff, formatação dos 119 arquivos Python e `git diff --check` aprovados; 17 avisos de depreciação na suíte integral. A tentativa inicial via `pytest.main` na entrada padrão falhou nos dois testes antigos de processos Windows; a execução final pela CLI corrigiu o ambiente, sem mudar o OAuth nem remover testes. Execuções do sandbox com problemas de temporário foram substituídas por execução local Windows.

**PostgreSQL principal:** `upgrade head/current/check` aprovados em **0006_telegram_technical**, sem drift; fingerprints do conteúdo de todas as tabelas anteriores preservados. Gravada somente a prévia técnica **id 1**, destino **id 2 / -5309308689**, `PENDING`, sem aprovador, tentativa `0`, auditada como `PREVIEW_CREATED`. Nenhuma aprovação ou chamada Telegram real. Destino pausado, aprovação obrigatória, nove categorias aceitas; demais destinos desativados, monitoramento pausado, ambas as flags Telegram e conectores comerciais `false`. `.env` e credenciais não alterados. Nenhum commit ou push criado. Próximo passo: nova autorização humana para uma única tentativa do conteúdo exato, com revalidação e aprovação do hash/destino atuais; a autorização antiga não se aplica.

### Primeira publicação técnica real autorizada — 21/10/2026

**SENT**, publicação técnica **id 1**, bot **@VianinhaOfertasBot**, grupo **Viana Ofertas / -5309308689**, Telegram **message_id 12**. Nova autorização explícita do responsável, conteúdo exato e hashes conferidos, nenhuma aprovação/tentativa anterior. `getMe/getChat/getChatMember` HTTP 200, bot administrador. Rotas administrativas autenticadas, outbox técnica e publisher oficial; **uma única chamada real de publicação**, sem retry/fallback, produtos ou ofertas fictícias.

Auditoria persistida: aprovação **id 4**, reserva **id 5**, sucesso **id 6**; tentativa única consumida. Antes da liberação do POST, a pausa do destino e o fechamento das flags em memória foram confirmados; `finally` reconfirmou o estado final. Ambas as flags Telegram `false`, destino pausado, demais destinos desativados e preservados. Nenhum outro grupo recebeu mensagens. `.env`/credenciais e nove categorias preservados; Mercado Livre/Shopee desativados e monitoramento pausado. Sem alterações de código/migrações, commits ou reexecução da suíte de 1.026 testes. Evidência sanitizada em [docs/PUBLICACAO_TECNICA_REAL_2026_10_21.md](docs/PUBLICACAO_TECNICA_REAL_2026_10_21.md).

Esta autorização pontual está encerrada e não permite nova tentativa, oferta comercial ou operação contínua. Nenhum avanço às Fases 5/6.

### Fase 4.7 — automação exclusivamente simulada — 21/10/2026

Implementado pipeline automático com o APScheduler/runtime existentes, provedor `SimulatedPriceProvider`, coletor/histórico/comparação, `OfferEngine`, classificação das nove categorias, filtros por produto/frase normalizada, limiares Decimal opcionais, revalidação, outbox Telegram simulada e publisher oficial selado em `httpx.MockTransport`. Perfis fictícios e políticas explícitas somente em contexto isolado; o banco principal e `public` são recusados. Flags de operação real não são habilitadas.

Migração aditiva **0007_simulated_automation** com perfis, políticas, outbox/auditoria próprias, constraints `is_simulated=true`, unicidade por produto/destino e downgrade sem perda de dados. Sucesso é **SIMULATED**, nunca `SENT` real. Claim e auditoria antes do mock, uma tentativa por registro, deduplicação, janela de três horas e nova observação; timeout/claims abandonados/resultados não persistidos conservam reserva incerta. Aprovação automática exclusiva da simulação, auditada por hash, lote padrão 3 e teto de 5 tentativas/hora/destino. Filtros por categorias continuam obrigatórios. A conclusão por destino preserva as prévias da mesma oferta para outros destinos.

Correção necessária no lease compartilhado: psycopg retornou `rowcount=-1` inclusive para `ON CONFLICT DO NOTHING`; aquisição e retomada agora comprovam propriedade por `RETURNING owner`. A regressão original do scheduler e os novos testes de concorrência PostgreSQL são preservados. JSONB mantém comparação de defaults/tipos do Alembic sem desativar validações.

**Demonstração efetivamente executada:** APScheduler real, limitado e encerrado, em schema UUID novo de `ofertas_bot_test`; produtos/preços explicitamente fictícios. **5 oportunidades, 2 produtos filtrados, 3 enfileirados e 3 simulados**, 3 chamadas mockadas, **zero chamadas reais ao Telegram**. Cinco eventos de aprovação automática na evidência final (incluindo revalidações); uma deduplicação explícita, repetição de despacho manteve chamadas em **3 → 3**. [Evidência JSON](docs/DEMONSTRACAO_FASE_4_7.json) e [guia/contratos](docs/FASE_4_7_SIMULACAO.md). Schemas de demonstração/diagnóstico foram preservados, sem apagar dados existentes.

**Operação principal conferida:** Alembic `upgrade head/current/check` passou em 0007 sem drift; fingerprints de todas as tabelas anteriores preservados, nove categorias e publicação técnica id 1 `SENT/message_id=12` intactos. Nenhum perfil/outbox fictício inserido na base principal. Ambas as flags reais Telegram e simulação automática `false`, grupo Viana Ofertas pausado, Mercado Livre/Shopee desativados e monitoramento principal pausado. Credenciais anteriores preservadas. Em 21/10, por solicitação explícita, `SHOPEE_APP_ID` e `SHOPEE_SECRET` foram configurados somente no `.env` local, sem expor valores, mantendo `SHOPEE_ENABLED=false` e sem chamadas à API. Nenhum commit criado; Fases 5/6 não iniciadas. Validação anterior confirmada: **1.093 testes aprovados na suíte completa**, incluindo 29 PostgreSQL reais; depois do último ajuste de serialização da política, **mais um teste PostgreSQL novo passou isoladamente**. Ruff passou e **131 arquivos** estão formatados. A tentativa de revalidação integral final em 21/10 foi interrompida após erros de conexão: o ambiente Windows isolado atual não enxerga o Docker no PATH/caminho padrão nem o PostgreSQL em `127.0.0.1:5432`. Portanto, **a suíte completa final de 1.094 testes e os 30 PostgreSQL juntos permanecem pendentes de ambiente**, sem declaração de sucesso dessa nova execução. A validação Alembic 0007 sem drift e a preservação dos dados foram comprovadas na execução anterior; não foram reconfirmadas nesta tentativa. A edição da política agora compartilha o lock do destino com o despacho, impedindo mudanças entre aprovação revalidada e reserva da tentativa.

## Checkpoint — validação inicial Shopee (21/10/2026)

- [x] Presença de App ID/Secret conferida sem exposição; endpoint, esquema e assinatura reconfirmados no Explorer oficial BR.
- [x] Uma única query de leitura `productOfferV2`, página 1/limite 1, HTTP **200**, **1 produto**, **0 erros GraphQL**. Resultado: acessório cabo HDMI relacionado a PS5, preço informado `59`; não confundir com console ou desconto.
- [x] Registrados IDs, nome, preço/intervalo, imagem, URL canônica, categorias e comissão efetivamente retornados. Sem geração de links, novas páginas, retries, consultas Mercado Livre, gravações no banco ou chamadas Telegram.
- [x] Diagnosticado offline `INVALID_PAYLOAD` local: categoria `0` em `[100535,100583,0]` é rejeitada pela validação de IDs positivos. Acesso autenticado de leitura confirmado; ingestão comercial completa ainda não operacional.
- [x] **86 testes existentes Shopee aprovados**, todos mockados. Código, filtros, nove categorias, migrações, tokens, alterações locais e `.env` preservados; flags reais permanecem `false`; nenhum monitoramento iniciado. Sem reconferência do estado persistido ou nova suíte PostgreSQL nesta consulta.
- [x] Corrigida posteriormente a normalização de categoria zero, com regressão, IDs válidos preservados e disponibilidade/acesso desconhecidos. Nenhuma nova consulta real executada; ver checkpoint abaixo.
- [x] Integração ao histórico/motor autorizada posteriormente na Fase 4.8 e validada offline; novas leituras reais, demais operações e operação automática continuam dependendo de autorização específica. Não presumir condições comerciais ou link próprio.

[Relatório técnico](docs/VALIDACAO_SHOPEE_2026_10_21.md) e [evidência sanitizada](docs/VALIDACAO_SHOPEE_2026_10_21.json). Shopee continua **desativada**, Mercado Livre suspenso, Telegram real desativado; nenhum commit e nenhum avanço às Fases 5/6.

## Checkpoint — correção pontual Shopee (21/10/2026)

- [x] Correção limitada a `ShopeeConnector._snapshot/productCatIds`: omitir somente inteiro zero, sem categoria inventada; preservar IDs positivos e ordem. Tipos inválidos, negativos, estruturas malformadas, tamanho e limites continuam rejeitados.
- [x] Resposta sanitizada anterior reproduzida offline; categorias mistas/somente zero e entradas inválidas cobertas. Comissão, preço e desconto permanecem distintos; nenhuma validação de preço, disponibilidade, variante, produto ou link relaxada.
- [x] Regressão do cabo HDMI: categoria ampla Games não implica Console; busca `PlayStation 5` não altera nome/tipo e filtro específico impede outbox.
- [x] **179 testes direcionados aprovados** (120 Shopee + 59 simulação). Ruff e formatação passaram (132 arquivos); Alembic `upgrade head/current/check` passou em `0007_simulated_automation`, sem drift.
- [x] Docker Engine 29.8.2, Compose/PostgreSQL saudável e conexão real conferidos. Nove categorias ativas, destino Viana Ofertas pausado e monitoramento principal desativado; fingerprints de todas as tabelas principais preservados após Alembic.
- [x] **1.129 testes aprovados na suíte integral**, incluindo **30 PostgreSQL reais** em schemas UUID isolados de `ofertas_bot_test`, **zero falhas e zero testes ignorados**. Os 179 direcionados são um subconjunto (120 Shopee e 59 simulação), não uma soma adicional. Há 17 avisos de depreciação de dependências, sem falhas. Ruff passou e 132 arquivos estão formatados; Alembic `upgrade head/current/check` passou na revisão `0007_simulated_automation`, sem drift. A indisponibilidade de ambiente registrada no checkpoint anterior foi resolvida para esta execução; revalidação integral da Fase 4.7 concluída.

Conferência final por fingerprints: conteúdo de todas as tabelas principais preservado; `.env` idêntico antes/depois; nove categorias ativas, grupo Viana Ofertas pausado e monitoramento principal desativado. Nenhuma consulta nova à Shopee, geração de links ou publicação. Credenciais, dados, migrações e alterações anteriores preservados. Mercado Livre/Shopee e flags Telegram continuam desativados. Sem commit ou avanço às Fases 5/6.

## Fase 4.8 — Shopee: coleta, histórico e garimpo preparado — 21/10/2026

Autorizada a implementação incremental pelo briefing, **sem novas consultas reais implícitas ou polling comercial contínuo**. Checkpoint Git anterior `a6eb20e` preservado; nenhum novo commit nesta entrega.

- [x] Reutilizar ShopeeConnector, ingestão/histórico, Decimal, classificação, filtros, comparador, OfferEngine, leases e APScheduler existentes.
- [x] Migração aditiva `0008_shopee_collection`: execuções idempotentes e candidatas com auditoria, referência ao produto/histórico, orçamento de uma tentativa e downgrade protegido contra perda de dados.
- [x] API administrativa autenticada para coleta pontual com opt-in por execução, consultas de auditoria/candidatas, revalidação local e preparação de agendamento pausado.
- [x] Preservar identidade loja:item/variante, nome, URL, imagem, categorias positivas, horário/fonte, preço e intervalo reais; categoria zero continua não classificável. Comissão separada de desconto.
- [x] Separar variação informativa do preço reportado de queda comercial verificada. Primeira observação não cria redução; estoque, preço público, variantes e demais dados não comprovados continuam desconhecidos.
- [x] Filtros conjuntos por categorias, produto/frase e limiares opcionais. Matching conservador exclui acessórios e títulos ambíguos quando a intenção é console; correções manuais de categorias preservadas.
- [x] Persistir candidatas dry-run; nenhuma outbox/autoaprovação real. Numa hipótese futura verificada, OfferEngine persiste apenas oferta PENDING, com enqueue desativado. Publicações técnicas não participam do fluxo.
- [x] Uma página/até cinco produtos/um POST, sem retry ou links, intervalo conservador local de 900 segundos, quota/bloqueio compartilhados do registry, reserva durável, lease e fencing. Job preparado no scheduler existente permanece pausado/bloqueado.
- [x] Testes direcionados finais: **136 aprovados**, incluindo 46 novos offline e regressões de automação/motor. Execução anterior de migrações/PostgreSQL: **77 aprovados**, incluindo oito novos PostgreSQL, sempre em schemas UUID de `ofertas_bot_test`. Contagens sobrepostas, não somar à suíte completa.
- [x] Alembic `upgrade head/current/check` no PostgreSQL principal aprovado na revisão `0008_shopee_collection`, sem drift. Ruff/formatação aprovados.
- [x] [Demonstração offline](docs/DEMONSTRACAO_FASE_4_8.json): 1 produto, 2 observações, 3 avaliações; INITIAL, UNVERIFIED e FILTERED; zero ofertas/outbox. Preço 59.00 é replay da resposta anterior; 55.00 é cenário hipotético explicitamente fictício, sem alegação de novo preço real.
- [x] **1.183 testes aprovados**, incluindo **38 PostgreSQL reais** em schemas UUID isolados de `ofertas_bot_test`; zero falhas e zero ignorados. Os 1.129 anteriores foram preservados e 54 novos acrescentados (46 offline e oito PostgreSQL). Há 17 avisos de depreciação de dependências, sem falhas. Ruff e formatação de 140 arquivos passaram; Alembic `upgrade head/current/check` passou em `0008_shopee_collection`, sem drift. Conferência final por fingerprints: todas as tabelas anteriores preservadas; `.env` idêntico, nove categorias ativas, grupo pausado, monitoramento desativado, cinco flags `false`, novas tabelas principais vazias. [Evidência sanitizada](docs/VALIDACAO_FASE_4_8.json).

[Guia técnico e próximos requisitos](docs/FASE_4_8_SHOPEE.md). Nenhuma consulta comercial adicional, publicação, geração de links, ativação contínua ou avanço às Fases 5/6. As cinco flags permanecem desativadas e o grupo pausado. Leitura real futura exige apresentação do escopo e nova autorização específica; geração de links próprios e operação contínua também dependem de autorização e permissões oficiais.

## Fase 4.9 — Shopee → Telegram: consulta real e bloqueio comercial — 21/10/2026

- [x] Autorização explícita única executada por ShopeeCollectionService/reserva idempotente `0d19d8fa-f466-47d1-8ddb-6bbebb3751d5`; `productOfferV2`, PlayStation 5, página 1, limite 1. **HTTP 200**, uma requisição, zero retry/fallback/paginação adicional.
- [x] Cabo HDMI retornado a R$ 59,00, não console. Produto local id 1, histórico id 1, origem oficial e Decimal, primeira observação **INITIAL**, sem queda fictícia. Categoria Games; candidata FILTERED pelo matching específico.
- [x] Condições preservadas como desconhecidas: disponibilidade/estoque e acesso público ao preço. Nenhum preço antigo, desconto, cupom ou variante inventado.
- [x] Link próprio ausente; nenhuma geração/mutation executada. Sem substituição do produto.
- [x] Preparação comercial **BLOCKED**; nenhuma oferta, prévia ou aprovação comercial. **Zero chamadas Telegram**, zero publicações e nenhuma utilização da outbox técnica para produto.
- [x] Docker Desktop e container PostgreSQL existentes iniciados, sem reinstalação/alteração global ou recriação de volume. Cinco flags continuam false, grupo pausado, monitoramento desativado, `.env`/credenciais preservados.
- [ ] Definir escopo e obter nova autorização antes de outra consulta para console adequado; comprovar condições comerciais e link próprio antes de prévia.
- [ ] Caso uma prévia venha a ser elegível, apresentar conteúdo/destino/hash e obter autorização específica antes de uma tentativa real pela outbox comercial.

[Relatório e auditoria](docs/FASE_4_9_SHOPEE_TELEGRAM.md); [evidência sanitizada](docs/VALIDACAO_FASE_4_9.json). Código e testes preservados (última suíte: 1.183/38 PostgreSQL); sem reexecução extensa, commit ou Fases 5/6. A autorização pontual desta consulta está consumida.

## Fase 4.10 - descoberta e garimpo Shopee exclusivamente offline - 21/10/2026

- [x] Reutilizar componentes das Fases 1-4.9, nove categorias e alteracoes locais; sem commit/push.
- [x] Politicas por categoria com multiplos termos, intencoes, prioridades, exclusoes, produtos, preco maximo e desconto minimo comprovado; IDs locais nao viram IDs Shopee.
- [x] Relevancia deterministica conservadora, acessorios rejeitados, ambiguidades em REVIEW e correcoes manuais auditadas.
- [x] Lotes/paginas limitados, rotacao justa, cursor de pagina documentado, deduplicacao loja:item/variante, quota local, reservas idempotentes, leases/fencing e backoff.
- [x] Reusar coleta/historico 4.8, Decimal, INITIAL e comparacoes verificadas separadas de variacao informativa. Nenhuma condicao comercial inventada.
- [x] Reconsulta separada de selecionados, pontuacao, avaliacao e preparacao dry-run; hipotese MOCK_WOULD_APPROVE somente com mocks, fila SIMULATED/PENDING, template/hash e limites. Aprovacoes reais intactas.
- [x] Tres jobs no APScheduler existente, pausados e bloqueados para consultas reais. API administrativa autenticada sem ativacao comercial.
- [x] Migracao aditiva 0009_shopee_discovery aplicada; Alembic upgrade/current/check aprovados, sem drift.
- [x] Demonstracao APScheduler offline: 27 produtos distintos, 18 selecionados, 36 observacoes/candidatas, 9 filtrados, 9 duplicados, 36 requisicoes mockadas, 0 preparacoes publicaveis, 0 chamadas reais. Precos ficticios e fatos comerciais UNKNOWN.
- [x] Testes direcionados iniciais: 122 aprovados, incluindo 11 novos PostgreSQL reais em schemas UUID de ofertas_bot_test; contagem sobreposta com a suite integral.
- [x] **1.267 testes aprovados**, incluindo **49 PostgreSQL reais**, zero falhas e zero ignorados; os 1.183 anteriores foram preservados e 84 novos acrescentados (73 offline e 11 PostgreSQL). Revalidacao dos 84 novos testes apos o ajuste final de contexto de categoria: todos aprovados. Contagens sobrepostas, nao somar. Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao passaram nos 150 arquivos Python; Alembic upgrade/current/check em 0009_shopee_discovery, sem drift. Fingerprints das 27 tabelas anteriores e .env identicos apos os testes; nove categorias ativas, grupo pausado, monitoramento desativado, cinco flags false e quatro tabelas novas de producao vazias. Zero novas consultas comerciais, mutations de afiliado ou publicacoes reais. Sem commit/push ou Fases 5/6. [Evidencia](docs/VALIDACAO_FASE_4_10.json).
- [ ] Operacao real futura depende de autorizacao especifica, quotas/permissoes oficiais, estoque/acesso/variante/link proprio comprovados e revisao das candidatas. Cupons/campanhas/ordenacoes nao comprovados permanecem pendentes.

[Guia](docs/FASE_4_10_GARIMPO_SHOPEE.md) e [demonstracao](docs/DEMONSTRACAO_FASE_4_10.json). Cinco flags false; grupo real pausado; Mercado Livre suspenso. Nenhuma nova consulta Shopee, mutation de afiliado, publicacao Telegram ou Fase 5/6.

## Adendo 4.10 - configuracoes dinamicas

- [x] Preferencias em AppSetting com revisao otimista, auditoria transacional, validacao e independencia por destino; sem migracao nova.
- [x] Dias/janelas/fuso, frequencias, prioridades, limiares normais/excepcionais, modo caca-bugs e limites carregados dinamicamente; valores iniciais separados do codigo operacional.
- [x] Preservar filtros/politicas de destinos, deduplicacao de tres horas, novas observacoes, backoff, reservas e aprovacoes. Atualizar jobs existentes sem retomar a operacao ou conceder autorizacao real.
- [x] Testes direcionados: 164 aprovados, incluindo nove novos PostgreSQL; mais tres regressoes especificas aprovadas. Contagens sobrepostas.
- [x] Alembic current/check em 0009_shopee_discovery, sem drift; Ruff/formatacao aprovados.
- [x] **1.341 testes aprovados**, incluindo **58 PostgreSQL reais**, zero falhas/skips; os 1.267 anteriores foram preservados e 74 acrescentados (65 offline e nove PostgreSQL). Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao aprovados nos 154 arquivos Python; Alembic current/check em 0009_shopee_discovery, sem drift e sem migracao nova. Fingerprints das 31 tabelas anteriores e .env identicos apos a suite; nove categorias ativas, grupo pausado, monitoramento desativado e cinco flags false. Zero consultas comerciais, mutations de afiliado ou publicacoes reais. Nenhum commit/push ou Fases 5/6. [Evidencia sanitizada](docs/VALIDACAO_ADENDO_FASE_4_10.json).

[Relatorio da entrega anterior e deste adendo](docs/RELATORIO_FASE_4_10_E_ADENDO_CONFIGURACOES.md). Cinco flags false, grupo pausado, sem consultas/publicacoes reais, commit ou Fases 5/6.

### Ajuste pontual — identidade dos links Shopee

- [x] Reconhecer o formato oficial `/universal-link/NOME-i.SHOP_ID.ITEM_ID`, sem resolução de rede.
- [x] Comparar IDs com a origem, rejeitar caminhos/domínios inválidos e separar identidade de atribuição à conta (`account_verified=false`).
- [x] Preservar integralmente o rastreamento no retorno interno/cadastro manual, ocultando-o do `repr` e de erros.
- [x] Acrescentar regressões offline do parser, do bloqueio de associação administrativa e da persistência SQLite isolada.
- [x] Validação: **1.335 testes offline aprovados** (52 novos), **58 PostgreSQL excluídos**, zero falhas; Ruff e formatação aprovados em 157 arquivos. Os 17 avisos são depreciações de dependências. Nenhum acesso ao banco real neste ajuste.
- [ ] Confirmar propriedade/atribuição por evidência independente antes de qualquer ativação controlada; geração do link não comprova comissão/conversões.

Nenhuma mutation, consulta comercial ou publicação real neste ajuste. Autorização anterior consumida, `.env`, registros/auditoria e flags preservados. [Validação e pendências](docs/AJUSTE_VALIDADOR_LINKS_SHOPEE.md). Fases 5/6 não iniciadas.

### Integracao do gerador de links ao pipeline — exclusivamente offline

- [x] Diagnostico registrado antes da implementacao; reutilizar descoberta, historico, filtros, avaliacao, fila e templates existentes.
- [x] Gerador injetavel usando ShopeeConnector/assinatura, restrito a MockTransport e laboratorio isolado; nenhum adaptador real instalado no bootstrap operacional.
- [x] Reservas duraveis em AppSetting por produto/variante/origem/destino/operacao, hash do payload, leases/fencing e auditoria IntegrationEvent. Sem migration nova.
- [x] Conservar URLs integrais internamente e divulgar somente metadados sanitizados; identidade valida nao comprova atribuicao nem concede aprovacao.
- [x] Revalidar antes/depois da geracao mockada, preservar links existentes e impedir retries de resultados incertos ou recusados.
- [x] Preferencias dinamicas opt-in, limites globais/por destino e bloqueios reais independentes; reutilizar cooldown minimo de tres horas e nova observacao.
- [x] Casos A-H, concorrencia, recuperacao, flags, parametros estritos, persistencia e revalidacao implementados com dados ficticios.
- [x] Baseline anterior: 58 PostgreSQL aprovados; testes novos direcionados: 14 PostgreSQL aprovados, incluindo Alembic current/check em schema UUID de ofertas_bot_test, sem drift.
- [x] Suite integral: **1.445 aprovados**, incluindo **72 PostgreSQL reais**, zero falhas/ignorados; 52 novos casos (38 offline e 14 PostgreSQL). Ruff e formatacao aprovados em 162 arquivos; Alembic current/check em 0009_shopee_discovery sem drift/migration nova. Os 17 avisos sao deprecacoes de dependencias. Fingerprints das 32 tabelas publicas, incluindo alembic_version, e .env identicos; nove categorias ativas, grupo pausado, monitoramento false, cinco flags false e zero reservas de links mockados no principal. Zero chamadas reais Shopee/Telegram. [Evidencia](docs/VALIDACAO_INTEGRACAO_LINKS_PIPELINE_SHOPEE.json).
- [ ] Geracao real futura exige nova autorizacao, adaptador real aprovado, quotas/permissoes e comprovacao independente de atribuicao; a autorizacao anterior permanece consumida.
- [ ] Publicacao comercial automatica continua sem autorizacao: depende de condicoes comerciais, revalidacao, aprovacao por destino/conteudo e liberacao especifica dos controles reais.

[Arquitetura, arquivos e evidencias](docs/INTEGRACAO_LINKS_PIPELINE_SHOPEE.md). Sem consultas/mutations reais, Telegram, ativacao de scheduler, mudancas em .env, commit ou Fases 5/6.

### Auditoria de prontidao comercial Shopee — 25/10/2026

- [x] Auditoria documental e de codigo concluida, com matrizes de capacidades/dados, fontes oficiais, niveis de atribuicao e plano de ativacao independente. [Relatorio](AUDITORIA_PRONTIDAO_SHOPEE.md).
- [x] Nenhuma alteracao de producao/testes/migrations, consulta autenticada, mutation ou publicacao. Flags locais false. Ultima suite registrada 1.445/72 PostgreSQL preservada; nao reexecutada nesta auditoria.
- [ ] Confirmar oficialmente vinculo credencial/beneficiario, quotas e escopo de uso continuo; aprovar politica separando geracao de conversao/comissao.
- [ ] Definir evidencia autorizada de preco publico, estoque e variante; API usada nao os comprova.
- [ ] Mediante aprovacao futura, unificar identidade manual /universal-link/ e reserva da rota legada; formalizar atestados/proveniencia antes do adaptador real e da ponte comercial. Outbox atual exige oferta manual comprovada; nao fabricar detalhes manuais para anuncios automaticos.
- [ ] Restabelecer ambiente PostgreSQL para nova conferencia operacional antes do piloto: tentativa readonly desta auditoria terminou OperationalError, sem escrita; ultimo estado documentado do grupo: pausado, nao reconfirmado hoje.

A auditoria nao autoriza implementacao, novos testes reais ou Fases 5/6. Nao houve commit/push ou reutilizacao de autorizacao anterior.

### Correções pós-auditoria Shopee — 25/10/2026

- [x] Unificar parser de identidade convencional/universal-link no conector e cadastro manual, preservando tracking e distinguindo identidade de titularidade.
- [x] Bloquear entrada administrativa real sem adaptador de autorização/reserva durável; preservar autorização histórica consumida. Laboratório segue mockado, com uma tentativa e interrupções UNCERTAIN.
- [x] Preparar documentos auditáveis/versionados para atribuição e condições comerciais, sem alterar observações automáticas ou promover account_verified.
- [x] Preparar políticas PROPOSED e diagnóstico sem ativação; conversão/comissão opcionais, sem exigir universalmente venda anterior.
- [x] Corrigir comparabilidade por source/source_type e contexto de preço, inclusive reaproveitamento de referência de ofertas.
- [x] Regressões offline e contratos PostgreSQL opt-in adicionados; nenhuma migration nova.
- [x] Validação offline: suíte completa com 1.421 aprovados; revalidação de 139 e dois testes administrativos aprovados (sobrepostos; 1.422 casos distintos, incluindo uma regressão adicional). Zero falhas finais, 17 avisos de dependências. Ruff/formatação e Alembic SQLite current/check em 0009_shopee_discovery sem drift. .env/artefatos da mutation preservados por hashes; zero chamadas comerciais/publicações.
- [x] Pendência PostgreSQL resolvida na execução posterior do piloto documental: Docker Linux ativo, banco exclusivo existente, 80 testes reais aprovados e Alembic current/check em 0009 sem drift. A execução original acima havia sido bloqueada pelo ambiente.
- [ ] Aprovar política de confiança, reunir provas de conta/condições e revisar adaptador real/ponte comercial antes de novo piloto autorizado.

[Relatório, arquivos e próximo piloto mínimo](docs/CORRECOES_POS_AUDITORIA_SHOPEE.md).
Sem chamadas comerciais, publicações, ativação operacional, alteração de .env,
credenciais, histórico ou primeira mutation; sem commit/push ou Fases 5/6.

### Piloto comercial controlado do Cockpit — sem envio

- [x] Docker Engine 29.8.2 Linux e PostgreSQL 17.11 saudável; banco ofertas_bot_test existente, sem recriação/alteração de volumes ou credenciais.
- [x] 80 testes PostgreSQL aprovados, zero falhas/ignorados; schemas UUID isolados. Alembic current/check em 0009_shopee_discovery sem drift; saída explicitamente capturada em caso já incluído nos 80.
- [x] Conferência operacional READ ONLY: nove categorias, Viana Ofertas enabled=false, aprovação obrigatória, categorias all, monitoramento persistido false e seis flags reais false.
- [x] Hashes das 32 tabelas operacionais e dados/schemas públicos anteriores do banco de testes idênticos; .env e artefatos históricos preservados.
- [x] Reserva histórica consumida, uma tentativa/HTTP 200, IDs, hash do payload e eventos 6–9 íntegros. Nenhuma mutation repetida.
- [x] Lacuna de rastreamento registrada: um dos cinco valores de query do longLink histórico contém redação; shortLink oficial preservado. Não reconstruir valores nem tratar longLink sanitizado como URL utilizável.
- [x] Proposta documental de confiança e rascunho manual não confirmado preparados, sem ativação/persistência de atestado humano ou account_verified.
- [x] Diagnóstico de prévia BLOCKED: Cockpit sem cadastro e histórico comercial no principal; nenhum produto, preço, oferta, prévia ou outbox fabricado.
- [ ] Obter preço público aplicável, variante, disponibilidade, condições e documentos da conta/beneficiário; decidir sobre shortLink e aprovar política antes de reavaliar a prévia.

[Relatório e proposta sem ativação](docs/PILOTO_COMERCIAL_COCKPIT_SHOPEE.md).
Zero novas chamadas Shopee, mutations, publicações, scheduler operacional,
alterações de .env/credenciais ou commit/push. Sem avanço às Fases 5/6.

### Evidência visual documental do Cockpit — 25/10/2026, 21h11

- [x] Captura original fornecida pelo responsável preservada sem edição, com SHA256; registro exclusivamente documental, sem acesso/gravação operacional.
- [x] Preços exibidos R$ 956,13 no Pix com cupom e R$ 1.099,00 sem cupom em outros métodos separados de preço efetivamente validado; cupom anunciado R$ 40 OFF e frete R$ 0,00 condicionais, sem aplicação confirmada.
- [x] Cores vermelho/azul documentadas, sem seleção/SKU inferido; disponibilidade UNKNOWN; relógio visível com fuso informado, sem verified_at inventado.
- [x] Diagnóstico BLOCKED; nenhuma comparação/queda histórica, oferta, prévia, outbox, account_verified ou publication_authorized aprovada.
- [ ] Confirmar variante, disponibilidade e preço aplicável/condições por evidência explícita; obter autorização adicional antes de qualquer registro no banco operacional. Política/vínculo do link permanecem pendentes.

[Captura, campos condicionais e diagnóstico](docs/EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.md).
Sem chamadas externas, mutations, publicações, alteração de .env, histórico de outro
produto ou ativação. Validação documental local; testes da aplicação preservados.

### Primeiro cadastro documental autorizado do Cockpit — sem oferta

- [x] Produto operacional ID 2 criado em ofertas_bot, external_id 1904573791:23199869203, uma linha sem identificador de variante inventado; cores e declaração do responsável preservadas.
- [x] Observações 2 (1099.00 sem cupom/outros métodos) e 3 (956.13 Pix com cupom) persistidas em Decimal BRL, fontes separadas e mesma captura: 25/10/2026 21h11 Brasília. INITIAL documental em ambos os contextos, sem queda ou comparação cruzada.
- [x] availability UNKNOWN; price_access UNKNOWN/CONDITIONAL, preço público não verificado em metadados/enums reais. Referências/hashes preservados, sem contornar source_reference ou criar campos/migrations.
- [x] Transação restrita a INSERT do produto e duas observações, com lock/constraints. Reexecução com DML proibido reutilizou os mesmos IDs e adicionou zero linhas.
- [x] Zero associação de link, oferta, aprovação, fila/outbox, chamadas Shopee, mutations, Telegram, scheduler ou mudanças em políticas/.env. Monitoramento do produto false; grupo e flags reais preservados false.
- [ ] Comprovar preço aplicável/condições, variante e disponibilidade; concluir vínculo e política do link antes de futura avaliação comercial. Cadastro documental não autoriza operação real nem publicação.

[Registro operacional e verificações](docs/CADASTRO_DOCUMENTAL_COCKPIT_SHOPEE.md).
A autorização de cadastro desta execução supera somente a pendência documental
anterior de permissão para gravar; demais bloqueios comerciais continuam vigentes.
Sem commit/push ou avanço às Fases 5/6.

### Fontes comerciais automatizáveis — estudo documental de 26/10/2026

- [x] Extrair do Explorer local o contrato público de productOfferV2, listItemFeeds e getItemFeedData, com SHA256 e proveniência; leitura pública atual indisponível, sem nova comprovação de permissões.
- [x] Separar preço/faixa e desconto anunciado de queda histórica, estoque, variante e preço efetivamente aplicável; não presumir colunas dos feeds ou validade comercial dos períodos.
- [x] Propor reconsulta por identidade e reaproveitamento do pipeline, com persistência/contexto comparável, idempotência e etapas futuras de leitura delimitadas. Produto 2 e históricos 2/3 intocados.
- [ ] Confirmar oficialmente quotas, escopo por conta, dicionário dos feeds e fontes de condições comerciais; obter autorização nova antes da implementação e de qualquer consulta ou recorrência real.

[Matriz de capacidade e plano mínimo](docs/FONTES_COMERCIAIS_AUTOMATIZAVEIS_SHOPEE.md).
Entrega apenas documental, sem testes repetidos, alterações de código, acesso ao
banco operacional, chamadas autenticadas, mutations, scheduler ou Telegram.
Nenhuma flag, política ou credencial alterada; sem commit/push ou Fases 5/6.

### Monitoramento observacional Shopee — implementação offline, 26/10/2026

- [x] Escopo tipado para anúncio já cadastrado, com consulta por shopId/itemId, página 1/limite 1, usando o conector e as reservas/auditorias existentes; nenhum anúncio substituto ou variante fabricada.
- [x] Desconto anunciado e períodos Int64 informativos integrados; preços continuam Decimal e sem cálculo fictício de preço anterior ou validade.
- [x] Contexto estruturado/price_basis validado persistido; separação por origem, produto, variante/condições, source_type e simulação. Nenhuma reescrita de histórico legado.
- [x] Modo observacional UNKNOWN/UNKNOWN/commercial_verified=false, sem geração de links, ofertas ou outbox. Interface administrativa real bloqueada com 409, independentemente de flags; laboratório exige MockTransport e banco isolado.
- [x] Reutilizar DiscoveryOperations para duração de lease/orçamento, idempotência, concorrência e fencing; timeout/interrupção/falha após início da persistência conservam tentativa e resultado UNCERTAIN.
- [x] 1.593 testes aprovados: 1.500 offline e 93 PostgreSQL reais em schemas UUID de ofertas_bot_test, zero falhas/ignorados; 78 testes offline e 13 PostgreSQL novos. Ruff/formatação nos 175 arquivos e Alembic current/check em 0009 sem drift; nenhuma migração operacional.
- [x] Conferência READ ONLY antes/depois: produto 2, históricos 2/3, destinos, configurações e dados/schemas preexistentes do banco de testes preservados. .env/evidências protegidas intactas; seis flags false, grupo pausado e scheduler persistido false; guard HTTP confirma zero transporte real.
- [ ] Confirmar quotas/escopo/semântica e fontes de condições comerciais; obter nova autorização e revisar adaptador/recuperação antes de qualquer leitura real ou recorrência. Publicação continua etapa separada e bloqueada.

[Funcionamento e validação](docs/MONITORAMENTO_OBSERVACIONAL_SHOPEE.md).
[Payload real futuro, somente preparado](docs/PAYLOAD_OBSERVACIONAL_COCKPIT_SHOPEE.json).
Nenhuma consulta comercial real, mutation, Telegram ou scheduler operacional iniciado;
produto 2, históricos 2/3, configurações e .env preservados. Sem commit/push ou
avanço às Fases 5/6.

### Piloto observacional de leitura única — preparação de 30/10/2026

- [x] Adaptador sobre o conector/coleta existentes, com plano read-only e escopo tipado de anúncio conhecido; não cria outro pipeline, histórico ou migração.
- [x] Reserva em AppSetting, aprovação explícita de hash/ator/validade, claim CAS e ShopeeCollectionRun commitados antes do HTTP; máximo de uma tentativa, sem retry/fallback/paginação ou liberação por flags.
- [x] Verificação de bytes/identidade/conta/modo/owner/lease antes do HTTP; concorrência e estados incertos impedem repetição. Namespace novo não consome autorizações históricas.
- [x] Cotação efêmera sanitizada, auditando somente metadados técnicos/hash. Zero gravações de preço em ProductPriceHistory ou JSON operacional, links, ofertas, outbox e Telegram.
- [x] Plano do Cockpit preparado somente por leitura do produto 2, com nonce/hash propostos; nenhuma autorização real registrada, aprovada ou consumida. Laboratório isolado exige mocks e não promove autorizações a reais.
- [x] 437 testes distintos aprovados: 328 offline e todos os 109 PostgreSQL reais, com 37 casos offline e 16 PostgreSQL novos. Ruff/formatação nos 178 arquivos e Alembic current/check em 0009 nos schemas isolados sem drift; zero transporte HTTP real e nenhuma migração operacional. Suíte offline anterior não repetida integralmente.
- [x] Conferência READ ONLY antes/depois preservou produto 2, históricos 2/3, destinos, configurações, arquivos protegidos e dados públicos/schemas preexistentes do banco de testes. Seis flags false, grupo pausado e scheduler persistido false.
- [ ] Confirmar acesso/quotas oficiais e obter autorização explícita posterior para este plano antes de registrar/aprovar/executar uma leitura real. Persistência, recorrência e publicação permanecem etapas separadas e bloqueadas.

[Detalhes, testes e procedimento exato](docs/PILOTO_LEITURA_UNICA_SHOPEE_20261030.md).
Somente desenvolvimento e validação local. Históricos 2/3, .env, credenciais,
flags, grupo e scheduler operacional preservados. Sem commit/push ou Fases 5/6.

### Cotação histórica e recorrência — preparação, 30/10/2026

- [x] Cotação protegida conferida com hash do objeto quote, reserva REAL consumida e cadeia de auditoria; hash do envelope completo registrado separadamente.
- [x] Preparação OFFICIAL_API com Decimal/horário originais, UNKNOWN/UNKNOWN, percentual anunciado independente, contexto e fingerprint por leitura/hash; sem misturar MANUAL ou alterar históricos 2/3.
- [x] 102 testes selecionados aprovados: 85 offline e 17 PostgreSQL isolados, incluindo idempotência, concorrência e bloqueios. Ruff/formatação passaram. Sem mudança de esquema ou migração operacional.
- [x] Plano não aplicado de 1–3 anúncios, intervalo inicial proposto 6h e teto local 12 leituras/24h, ajustáveis e sujeitos a quotas oficiais. Reserva por leitura e interrupção por permissão/incerteza; nenhuma recorrência habilitada.
- [x] Autorização específica recebida e executada em 30/10/2026: observação OFFICIAL_API ID 4 do produto 2, auditoria ID 14, BRL 1099.00 no horário original. Hash conferido; repetição na mesma transação reutilizou o ID sem duplicação. Históricos 2/3 e demais registros preservados.
- [ ] Confirmar permissões/quotas e aprovar separadamente qualquer recorrência. A entrada operacional genérica permanece bloqueada; a exceção aceita exclusivamente a cotação histórica autorizada.

[Plano e texto exato de autorização futura](docs/INGESTAO_HISTORICA_E_MONITORAMENTO_SHOPEE_20261030.md).
Zero consultas reais adicionais, observações operacionais, mutations, Telegram,
flags/credenciais/scheduler alterados, commit ou push. Fases 5/6 não iniciadas.

### Conclusão da ingestão histórica autorizada — 30/10/2026

Persistência confirmada após recuperação da interrupção: INITIAL, previous_price NULL,
percentual anunciado 31 separado, availability/price_access UNKNOWN e
commercial_verified=false. Os períodos brutos foram preservados. Não houve chamadas
externas, ofertas, outbox ou publicações. Flags permanecem false e grupo pausado.
85 regressões offline aprovadas; Ruff e formatação passaram nesta retomada.
Os 17 testes PostgreSQL da preparação não foram repetidos nesta retomada.
Relatório protegido em `.runtime/authorized-identity-read-ee52b076-4084-4dc3-abd3-4a9e994db463/INGESTAO_AUTORIZADA.json`.

### Preparação do monitoramento recorrente por identidade — 30/10/2026

- [x] Coordenador de laboratório reutilizando leitura única, reservas, DiscoveryOperations, histórico e comparação existentes; sem novo conector ou migração.
- [x] Autorização por conta/produto/payload, validade, orçamento e intervalo mínimo; aprovação explícita e referência de quotas. Cada ciclo tem reserva e claim próprios antes do HTTP, com idempotência e concorrência controladas.
- [x] Preferência de intervalo persistida sem ampliar autorização; integração somente preparada e pausada no scheduler de laboratório.
- [x] Comparação informativa OFFICIAL_API compatível; UNKNOWN/UNKNOWN e bloqueios comerciais preservados. Falhas/403/429/incerteza pausam, sem retry.
- [x] 182 testes distintos aprovados (143 offline e 39 PostgreSQL isolados), incluindo regressões e repetição final do novo monitor. Alembic current/check, Ruff e formatação passaram. Suíte completa não repetida; tabelas operacionais e .env preservados por hashes.
- [ ] Confirmar oficialmente permissões de recorrência e quotas. Aprovar novo escopo de piloto e validar uma liberação operacional posterior; execução real continua bloqueada por código.

Proposta não aplicada: produto 2, intervalo mínimo 6h, 24h de validade e até 4 POSTs.
[Plano e autorização futura](docs/MONITORAMENTO_RECORRENTE_SHOPEE_20261030.md).
Sem consultas externas, mudanças operacionais, publicações, commit ou push.

### Piloto experimental delimitado — 30/10/2026

- [x] Exceção separada para até duas leituras por identidade, intervalo mínimo 6h e validade máxima 24h; sem relaxar a autorização da recorrência geral.
- [x] Rotas administrativas autenticadas; hash/conta/payload/validade vinculados. Ciência explícita de quotas desconhecidas obrigatória; flags não substituem autorização.
- [x] Reserva/claim por ciclo, pausa em falhas/incerteza, sem retry; espaçamento mínimo contado após conclusão da leitura. Persistência somente observacional, sem vínculos/ofertas/outbox.
- [x] Escopo real PREPARED `1cfbb654-f4ab-4146-b185-e9f48e702540`, auditoria 15, zero reservas consumidas e zero HTTP. Demais dados/configurações preservados.
- [x] 111 testes distintos aprovados: 74 offline e 37 PostgreSQL isolados; Ruff, formatação e Alembic current/check passaram. Sem suíte completa ou migração operacional.
- [ ] Receber autorização específica do hash/janela final antes de aprovar ou despachar. Nenhuma chamada real executada nesta preparação; scheduler e Telegram desativados.

[Plano, limitações de permissão e procedimento](docs/PILOTO_EXPERIMENTAL_RECORRENCIA_SHOPEE_20261030.md).
Quotas oficiais e permissão para recorrência seguem não confirmadas. Sem commit/push.

### Comparação multiloja — implementação offline, 31/10/2026

- [x] Perfil específico de descoberta com shopName/ratingStar/sales/shopType; sem mudar payload de identidade ou perfil padrão.
- [x] Candidatos por relevância e grupos por evidências documentadas, sem inferir equivalência pelo título. Deduplicação por shopId, Decimal BRL, empates e diferenças; comparação transversal separada do histórico.
- [x] Campos ausentes/desconhecidos preservados; avaliações não promovidas a reputação, estoque, preço final ou aprovação comercial.
- [x] 216 testes distintos aprovados (205 offline e 11 PostgreSQL isolados); Ruff, formatação e Alembic current/check passaram. Suíte completa não repetida.
- [x] Busca independente PREPARED bce2459b-a77a-4e30-86fa-30d8069cf4bf, auditoria 25, sem aprovação/consumo. Keyword Cockpit simulador Brave Bull, page=1/limit=3, máximo um POST.
- [ ] Receber autorização específica e preparar entrada administrativa de despacho com claim para essa busca. execute_real permanece bloqueado; quotas e permissões não confirmadas.

[Implementação e demo sintética](docs/IMPLEMENTACAO_MULTILOJA_SHOPEE_20261031.md).
Zero chamadas externas, histórico operacional alterado, links ou publicações.
Piloto aprovado de amanhã preservado; sem scheduler, commit ou push. Fases 5/6 não iniciadas.

## Fase 5 — Ampliar cupons e operação da fila

- Expandir a avaliação conservadora de cupons da fase 2 com cadastro administrativo e fontes autorizadas de confirmação.
- Ajustar pesos e prioridades com evidência de uso, mantendo cupom opcional.
- Estender fila, tentativas e deduplicação simuladas aos publishers reais aprovados nas fases anteriores.
- Aplicar a janela mínima de três horas e a revalidação já demonstradas na simulação aos envios reais.
- Completar aprovação manual, limites por destino e conteúdo com imagens/templates.
- Ampliar cobertura de falhas de publishers, concorrência PostgreSQL e operação contínua.

**Critério de avanço:** nenhuma promessa de desconto sem verificação; ausência de cupom não impede oferta válida.

## Fase 6 — Operação contínua

- Definir implantação, HTTPS, acesso administrativo, backups e recuperação.
- Monitorar falhas operacionais e adicionar observabilidade sem expor segredos.
- Validar migrações e concorrência com PostgreSQL em integração contínua.
- Definir retenção do histórico e procedimentos de manutenção.
- Avaliar WhatsApp somente após validar uma solução autorizada para o destino pretendido.

## Fora do MVP pessoal

SaaS multiusuário, cadastro comercial de clientes, planos, cobrança, pagamentos e painel comercial não fazem parte das fases acima. Exigem escopo e aprovação próprios.
