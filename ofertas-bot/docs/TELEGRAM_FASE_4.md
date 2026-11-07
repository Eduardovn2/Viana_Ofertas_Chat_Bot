# Telegram — operação manual e outbox

Estado em 20/10/2026: código integrado e testes com mocks; nenhuma publicação real autorizada ou executada. As duas flags Telegram e os conectores comerciais continuam desativados. Não há job de publicação automática.

## Destino informado

Perfil local: **Viana Ofertas**, Chat ID **-5309308689**. O identificador foi fornecido pelo responsável; não foi consultado ou confirmado no Telegram.

Preparação explícita, idempotente, sem chamadas Telegram:

```powershell
.\.venv\Scripts\python.exe -m app.services.telegram_destinations prepare-test-group
```

Também disponível em `POST /api/v1/telegram/destinations/prepare-test-group`, com `X-API-Key` local. Cria o destino pausado, aprovação obrigatória, filtro `unconfigured`, intervalo de 60 segundos e limite de 20 tentativas/hora. Reexecutar mantém o grupo pausado e preserva filtros/limites já configurados. Não altera token, `.env`, allowlist ou outros destinos.

**A tentativa inicial retornou `OperationalError`; após o responsável iniciar Docker, o cadastro foi persistido como destino id 2, pausado e com aprovação obrigatória.** Docker Engine 29.8.2, Compose PostgreSQL saudável e `alembic upgrade head/current/check` foram validados na revisão 0005, com dados existentes preservados. Nenhum banco alternativo foi criado para mascarar o bloqueio anterior. O banco de testes já existia. Não execute downgrade no banco principal.

## Administração local

Todas as rotas abaixo usam `/api/v1/telegram`, autenticação administrativa existente e restrição de acesso local.

| Método e rota | Uso |
|---|---|
| `GET /status` | Flags e presença do token, sem revelar o valor. |
| `POST /destinations` | Cadastro com nome e `external_id` numérico negativo; pausado por padrão. |
| `GET /destinations` | Listagem paginada, somente destinos Telegram. |
| `GET/PATCH /destinations/{id}` | Consulta, pausa, nome, limites e filtros. Chat ID é imutável. |
| `GET /publications/{id}` | Prévia, aprovação, tentativa, resultado e resolução. |
| `GET /publications/{id}/logs` | Registro da tentativa correspondente, sem respostas privadas da API. |
| `POST /publications/{id}/dispatch` | Despacho pontual condicionado às duas flags, allowlist e aprovação. Atualmente bloqueado. |
| `POST /publications/{id}/recover` | Converte tentativa abandonada há pelo menos 60 s em resultado incerto; não envia nada. |
| `POST /publications/{id}/resolve` | Reconciliação administrativa explícita de resultado incerto, com evidência. |

PATCH aceita `name`, `enabled`, `min_interval_seconds` (1–86400), `max_messages_per_hour` (1–1000), `category_mode` e `category_ids`. `manual_approval_required=false` é recusado também pela rota antiga `/api/v1/destinations/{id}`. Categorias precisam existir, ser únicas e usar `selected` quando houver uma lista; `all` considera somente categorias ativas e `unconfigured` bloqueia o fluxo. A troca de configuração expira prévias/aprovações pendentes, preservando envios e resultados incertos. A rota anterior de preferências por destino também invalida essas aprovações.

`enabled` controla a avaliação local do destino. Não substitui autorização global, aprovação ou allowlist. `Ofertas de Casa` e destinos existentes não são habilitados pela preparação do grupo.

## Prévia, aprovação e despacho

O cadastro e a aprovação existentes foram preservados. `POST /api/v1/manual-offers/{id}/preview` aceita `destination_id` e, opcionalmente, `include_image=false`. Essa escolha gera uma prévia somente texto antes de qualquer envio; exige nova aprovação se substituir a versão com imagem. URL insegura continua sendo recusada no cadastro. Não se faz download ou consulta para testar a imagem.

O despacho recebe `confirmed=true` e o `preview_hash` exato. Confere novamente a aprovação, preço manual recente, cupom, link próprio, categorias, destino e conteúdo. Alterações exigem nova prévia/aprovação. Prévias legadas com formato anterior também podem exigir revisão; nenhuma aprovação é migrada silenciosamente para outro conteúdo.

O publisher oficial usa [sendMessage/sendPhoto](https://core.telegram.org/bots/api) por HTTPS, sem broadcast pago e sem retries automáticos. Uma mensagem só é confirmada com resposta válida, identificador de mensagem positivo e Chat ID correspondente. O [retry_after oficial](https://core.telegram.org/bots/api#responseparameters) é respeitado quando retornado. O token vem da configuração local `SecretStr`, não é recebido por rotas administrativas, e é removido dos diagnósticos HTTP.

## Estados e proteção contra duplicatas

1. Prévia → `PENDING`; aprovação explícita por oferta/destino → `APPROVED`.
2. Antes de chamar o publisher, a transação grava `SENDING` e um log `PENDING`, com número de tentativa e a mensagem aprovada, e faz commit.
3. Resposta confirmada → `SENT` e log `SENT`; rejeição comprovada → `FAILED`, com código sanitizado.
4. Timeout, erro de transporte, HTTP 5xx, resposta inválida ou resultado incompatível → `UNCERTAIN`; o log permanece `PENDING`, nunca sucesso presumido.

Um slot único por produto/destino impede duplicação ativa. `SENDING` e `UNCERTAIN` mantêm o slot reservado. Não há retry nem troca automática de foto por texto após resultado incerto. Repetir um despacho concluído não faz outro POST. Alterar ou pausar um destino não libera um resultado incerto.

Depois de falha definitiva, outra tentativa exige nova prévia/aprovação, com número de tentativa novo e respeito ao intervalo, limite horário e eventual `retry_after`. Os limites contam tentativas, incluindo falhas. Após sucesso, republicar exige **pelo menos três horas**, nova observação manual posterior ao sucesso e nova aprovação. O prazo sozinho não autoriza envio.

Bloqueios de banco seguem ordem consistente: destino → produto → outbox. A chamada HTTP ocorre fora da transação. Falha de processo ou de persistência após o HTTP mantém a tentativa reservada para reconciliação; não resulta em reenvio automático.

## Recuperação sem enviar

`recover` funciona com publicação desativada e apenas marca uma tentativa antiga `SENDING` como `UNCERTAIN`. Uma tentativa ainda recente permanece reservada.

`resolve` exige `confirmed=true`, `actor`, `evidence` e `outcome`:

- `SENT`: também exige `message_id` conferido pelo administrador; registra confirmação e começa uma janela conservadora de três horas no momento da resolução.
- `NOT_SENT`: registra `REJECTED` e libera o slot; eventual novo envio exige outra prévia/aprovação.

O sistema não consulta mensagens do grupo para inventar essa confirmação. O administrador precisa verificar o resultado antes de resolver. Ator original da aprovação, mensagem, tentativa e evidência da resolução são preservados.

## Validação e pendências

Os testes usam exclusivamente SQLite temporário, valores sintéticos e `httpx.MockTransport`. Flags habilitadas em cenários de envio existem somente nos objetos de teste com transporte mockado; o ambiente real permanece desativado.

A regressão pertinente antes de iniciar Docker aprovou 334 testes e ignorou sete casos PostgreSQL. O conjunto final Telegram/manual aprovou 205 testes; os resultados se sobrepõem. Depois de iniciar Docker, os 12 testes PostgreSQL reais passaram. A **suíte integral final aprovou 982 testes em 268,29 s, sem falhas ou skips**, incluindo os 755 anteriores e os 12 PostgreSQL. Ruff, formatação e `git diff --check` passaram. Os 17 avisos são de depreciação de dependências, sem falhas funcionais; não somar contagens sobrepostas.

PostgreSQL: conexão, persistência do grupo, migração real 0005 e testes reais foram confirmados após iniciar Docker. Os casos incluíram cadastro do grupo pausado, prévias concorrentes e um único despacho concorrente com publisher oficial mockado. `TEST_DATABASE_URL` foi configurada somente no subprocesso, exclusivamente para `ofertas_bot_test`, usando schemas UUID temporários. Nunca use o banco principal para os testes.

Nenhuma credencial foi alterada, nenhum conector comercial foi ativado e nenhum commit final da Fase 4 foi criado. Validação real de acesso do bot ao grupo e qualquer mensagem real dependem de autorização posterior.
