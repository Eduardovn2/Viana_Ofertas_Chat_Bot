# Primeira publicação técnica real — 21/10/2026

**Resultado: SENT. Publicação técnica id 1. Uma única chamada real de publicação.**

- Bot confirmado: **@VianinhaOfertasBot**.
- Grupo confirmado: **Viana Ofertas**, Chat ID **-5309308689**, destino id **2**.
- `getMe`, `getChat` e `getChatMember` retornaram HTTP 200; bot administrador do grupo.
- Telegram `message_id` confirmado e persistido: **12**.
- Conteúdo exato: [PREVIA_TESTE_TELEGRAM.txt](PREVIA_TESTE_TELEGRAM.txt), sem quebra final.
- SHA-256 do conteúdo: `2aa7bceb17fe76ccfcb202ce9106d0d44c76a9b12d785dda0cdb990d22b88963`.
- Hash aprovado após preparação temporária do destino: `5566b708f961f36e4d12680fd898bef51d9fd99f0fb33f32354dcdac087db0c3`.

## Aprovação e execução

A nova autorização explícita do responsável no chat em **21/10/2026** foi utilizada exclusivamente para este conteúdo e destino. Antes da preparação, a prévia era `PENDING`, sem aprovação, reserva ou tentativa anterior; conteúdo e hashes foram comparados e validados. A autorização antiga do teste bloqueado não foi reutilizada.

Ator registrado: **Responsável pelo projeto — autorização explícita no chat de 21/10/2026**. Aprovação persistida em **2026-10-21T03:05:34.483665+00:00**. A habilitação temporária do destino invalidou a revisão pausada; a rota de prévia atualizou o mesmo registro id 1, com conteúdo idêntico, antes da aprovação do novo hash.

Executadas as rotas administrativas autenticadas do próprio FastAPI em processo local, sem iniciar seu lifespan/scheduler: configuração do destino, prévia, aprovação e **uma única invocação de dispatch**. A outbox técnica e o `TelegramPublisher` oficial realizaram reserva, auditoria, validação e envio. Não houve emissor alternativo ou chamada direta de publicação fora do publisher.

As flags foram liberadas somente na configuração em memória desse processo; a allowlist temporária foi restrita ao único grupo. Uma verificação adicional no cliente HTTP do publisher conferiu método, origem, corpo exato, aprovação e reserva persistida. Antes de liberar o POST, restaurou e confirmou a pausa no PostgreSQL e fechou as flags em memória. Se essa restauração falhasse, a chamada não seria liberada. A limpeza em `finally` reconfirmou os bloqueios. Nenhum arquivo de código ou credencial foi alterado.

O publisher executou somente `sendMessage`, com o texto autorizado, sem imagem e com prévia de links desativada. Telegram retornou confirmação válida com `message_id=12` e o Chat ID correto. Não houve retry, fallback ou segundo despacho.

## Auditoria persistida

Tabela `telegram_technical_audit`, publicação id **1**:

| ID | Evento | Resultado |
|---|---|---|
| 1 | PREVIEW_CREATED | Prévia original preservada. |
| 2 | REVIEW_INVALIDATED | Preparação temporária alterou a configuração do destino. |
| 3 | PREVIEW_UPDATED | Mesmo texto e destino, hash atualizado. |
| 4 | APPROVED | Aprovação explícita pelo ator acima, vinculada ao hash atual. |
| 5 | ATTEMPT_RESERVED | Reserva `SENDING`, tentativa `1`, confirmada antes do HTTP. |
| 6 | SENT | Sucesso confirmado e persistido; sem código de erro. |

Consulta administrativa: `GET /api/v1/telegram/technical-publications/1/audit`. O registro permanece `SENT`, com `attempt_number=1`; o limite está consumido e não autoriza nova tentativa.

## Estado final confirmado

- `TELEGRAM_PUBLISHER_ENABLED=false`.
- `TELEGRAM_REAL_SENDS_AUTHORIZED=false`.
- Destino Viana Ofertas: `enabled=false`, aprovação obrigatória preservada.
- Todos os demais destinos desativados; seus registros foram comparados e preservados.
- As nove categorias foram comparadas antes/depois e preservadas.
- Mercado Livre e Shopee desativados; monitoramento automático pausado.
- `.env` íntegro, comparado por hash sem divulgar seu conteúdo; token e credenciais preservados.
- Somente o Chat ID **-5309308689** foi utilizado na única chamada real de publicação; nenhum outro grupo recebeu mensagens nesta operação.
- Sem mudanças de código, migrações, commits, testes extensos ou avanço às Fases 5/6. A validação anterior de 1.026 testes não foi reexecutada nesta operação pontual.
