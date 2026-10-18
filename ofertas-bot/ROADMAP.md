# Roadmap do Ofertas Bot

Fases 1, 2 e 2.1 aprovadas e preservadas. A Fase 3 foi autorizada em 18/10/2026; conectores oficiais e pipeline dry-run implementados, com validação comercial pendente de credenciais/permissões. Estado: **FASE 3 PARCIAL / BLOQUEADA POR ACESSO**. A verificação do CLI Docker/Compose permanece separada do PostgreSQL real validado. Não iniciar a Fase 4 sem novo aceite.

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

## Fase 4 — Publicação Telegram

- Implementar `MessageTemplateService`, omitindo campos ausentes e usando identificação publicitária aplicável.
- Implementar Telegram pela API oficial com token no ambiente, destinos e modo de teste.
- Bloquear publicação sem link de afiliado próprio, preço válido ou informação atualizada.
- Adicionar aprovação manual, registro de envio, erros e limites por destino.
- Publicar mensagens ou imagens somente após configuração e autorização do responsável.

**Critério de avanço:** conteúdo revisado no modo de teste e aprovação explícita antes do primeiro envio real.

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
