# Roadmap do Ofertas Bot

Fases 1, 2 e 2.1 aprovadas e preservadas. Conectores, OAuth e pipeline da Fase 3 implementados e preservados. **Estado atual: consulta automática de preços Mercado Livre temporariamente suspensa; operação manual autorizada; Shopee aguardando liberação da Open API. Fase 4 implementada e validada com Telegram mockado e PostgreSQL real: 982 testes aprovados.** Envios reais continuam sem autorização, com `telegram_publisher_enabled=false` e `telegram_real_sends_authorized=false`. Docker/Compose e PostgreSQL foram validados na continuação de 20/10/2026. Os registros abaixo conservam o histórico das decisões e validações anteriores.

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
