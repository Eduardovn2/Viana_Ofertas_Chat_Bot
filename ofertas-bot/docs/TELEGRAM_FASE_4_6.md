# Fase 4.6 — publicação técnica administrativa

Extensão independente da outbox comercial. `telegram_technical_publications` e `telegram_technical_audit` não possuem produto, oferta, preço, cupom, imagem ou link comercial. A migração aditiva `0006_telegram_technical` preserva o esquema/dados anteriores e recusa downgrade com registros técnicos ou de auditoria.

## Controles

- Somente rotas administrativas locais, protegidas por `X-API-Key`. Não há ligação com scheduler, monitoramento, classificação comercial ou filas de ofertas.
- Texto administrativo simples, identificado com `🧪 SIMULAÇÃO — ` ou `AVISO TÉCNICO — `. URLs usuais, valores monetários BRL, percentuais promocionais, padrões de credenciais, controles e limites inválidos são recusados. O tipo técnico não substitui a revisão humana de conteúdo; não use para ofertas comerciais. HTML é escapado, sem formatação ou links ativos, sem imagem e sem prévia de links.
- `content_hash` é SHA-256 do texto UTF-8 exato. A chave única permanente `(destination_id, content_hash)` impede uma segunda tentativa do mesmo conteúdo nesse destino, mesmo após falha, timeout, três horas ou reinício. Esta regra técnica é mais restritiva que a republicação comercial, que continua intacta.
- `preview_hash` vincula texto, mensagem renderizada, tipo técnico e configuração do destino. Aprovação exige `confirmed=true`, ator, destino e esse hash exato. A autorização histórica do primeiro teste bloqueado **não aprova** os novos registros.
- Prévia e aprovação podem ser preparadas com destino pausado e flags `false`; não enviam. Despacho exige destino habilitado, aprovação persistida, hash/destino atuais, allowlist, ambas as flags e o publisher oficial com token válido. A configuração não é habilitada automaticamente.
- Alterar a configuração do destino expira revisões técnicas pendentes/aprovadas e preserva os eventos anteriores. Depois de qualquer alteração, gere nova prévia e aprovação. Não altera nem libera tentativas `SENDING/UNCERTAIN`.
- Bloqueio PostgreSQL na ordem destino → registro, mais atualização condicional de `APPROVED/attempt_number=0`, garantem uma reserva. `SENDING`, tentativa `1` e evento `ATTEMPT_RESERVED` são confirmados no banco **antes** da única chamada ao publisher.
- O publisher oficial continua responsável por HTTPS, allowlist, flags, validação de resposta e redação de tokens nos logs HTTP. Não há retry, fallback ou segunda tentativa, inclusive em `FAILED`/HTTP 429. Limites do destino consideram tentativas técnicas e logs comerciais antes do despacho técnico.
- `SENT` exige `message_id` positivo e Chat ID correspondente. Rejeição comprovada termina `FAILED`. Timeout, transporte, HTTP 5xx ou resposta inválida terminam `UNCERTAIN`. Outra mensagem técnica para um destino com `SENDING/UNCERTAIN` também fica bloqueada.
- Falha ao persistir o resultado tenta somente registrar `UNCERTAIN`, nunca enviar novamente. Se o banco permanecer indisponível, a reserva durável continua consumida em `SENDING` (ou em estado já confirmado); a API informa incerteza. Após restabelecer o banco, recuperação administrativa de `SENDING` abandonado há 60 segundos marca `UNCERTAIN`, mesmo com flags desativadas. Não há rota para zerar tentativas ou liberar a chave única.

Auditoria separada registra criação/atualização de prévia, aprovação com ator, invalidação, reserva e resultado com códigos sanitizados. Não armazena token ou resposta HTTP bruta. Os registros não podem ser editados/excluídos pelas APIs técnicas. `content_hash` e o conteúdo exato são preservados; reaprovar sem alteração mantém o aprovador original.

## Rotas

Prefixo: `/api/v1/telegram/technical-publications`.

| Método/rota | Dados e resultado |
|---|---|
| `POST /preview` | `destination_id`, `text`, `actor`; persiste/devolve prévia, hashes e estado. Sem publicação. |
| `GET` | Listagem por `destination_id`, `limit`, `offset`. |
| `GET /{id}` | Consulta texto, Chat ID, hashes, aprovação, tentativa e resultado. |
| `GET /{id}/audit` | Eventos ordenados, atores e códigos de erro sanitizados. |
| `POST /{id}/approve` | `destination_id`, `preview_hash`, `actor`, `confirmed=true`. |
| `POST /{id}/dispatch` | `destination_id`, `preview_hash`, `confirmed=true`; despacho administrativo único, protegido pelas flags. |
| `POST /{id}/recover` | Somente recuperação local de reserva abandonada, sem HTTP ou liberação. |

O campo `kind=ADMINISTRATIVE_TECHNICAL` distingue respostas técnicas. Listagens comerciais não incluem estes registros. Campos comerciais extras no cadastro técnico são recusados. Não confunda `content_hash` com `preview_hash`: apenas o segundo é aceito para aprovação/despacho.

## Prévia solicitada

Texto exato em [PREVIA_TESTE_TELEGRAM.txt](PREVIA_TESTE_TELEGRAM.txt), sem a quebra final do arquivo:

```text
🧪 SIMULAÇÃO — VIANA OFERTAS

Teste de conexão do @VianinhaOfertasBot.

Esta é uma mensagem de teste técnico, sem oferta real.
Não contém produtos, preços, cupons, descontos ou links de compra.

Grupo: Viana Ofertas
```

Destino pretendido: **Viana Ofertas / -5309308689**. `content_hash`: `2aa7bceb17fe76ccfcb202ce9106d0d44c76a9b12d785dda0cdb990d22b88963`. A prévia possui 215 unidades UTF-16, sem foto. A autorização anterior resultou em operação **BLOCKED**, com zero chamadas; não é reutilizada.

## Como autorizar uma futura tentativa única

1. Revisar o texto acima e autorizar **novamente**, expressamente, uma única tentativa pelo **@VianinhaOfertasBot**, exclusivamente no grupo **-5309308689**, sem retry/fallback nem ofertas comerciais. Esta implementação não realiza esse envio.
2. Na operação futura autorizada, revalidar a identidade via `getMe`, o grupo via `getChat` e permissões via `getChatMember`, conferir allowlist e ausência de tentativa anterior. Essas leituras não substituem a aprovação.
3. Habilitar somente os controles indispensáveis em contexto temporário, sem alteração permanente do `.env` e mantendo demais destinos/conectores/monitoramento desativados. Como habilitar o destino altera sua configuração, gerar a prévia atual **depois** dessa mudança e registrar nova aprovação explícita do destino e `preview_hash` atual.
4. Despachar uma única vez pela outbox técnica e publisher oficial. Consultar resultado/auditoria sem reenviar. Timeout ou persistência ambígua exigem interrupção e conferência humana.
5. Em `finally`, restaurar ambas as flags para `false` e destino pausado, mesmo se ocorrer erro. Conferir e relatar estado final, número de chamadas, registro da auditoria e `message_id` somente quando confirmado.

Até essa nova autorização, manter `TELEGRAM_PUBLISHER_ENABLED=false`, `TELEGRAM_REAL_SENDS_AUTHORIZED=false` e o grupo pausado. Não editar token, OAuth, conectores comerciais ou iniciar Fases 5/6.

## Validação

Os testes usam apenas tokens/destinos sintéticos e `httpx.MockTransport`; o transporte HTTP real é proibido nos novos módulos. PostgreSQL usa exclusivamente schemas UUID criados pelo fixture dentro de `ofertas_bot_test`. Incluem aprovação/hash/destino, segurança, mutação, concorrência, falhas, timeout, commit ambíguo, recuperação, auditoria, constraints, migração e API. Resultados finais desta execução ficam no README e ROADMAP.

Resultado final: **1.026 testes aprovados**, zero falhas/skips, incluindo os 982 anteriores e 44 novos; **20 PostgreSQL reais aprovados** também na execução separada (contagens sobrepostas). Ruff, formatação e `git diff --check` passaram. Migração principal aplicada até `0006_telegram_technical`, sem drift e preservando conteúdo das tabelas anteriores.

Prévia local persistida: **id 1**, destino **id 2 / -5309308689**, `PENDING`, sem aprovador, tentativa `0`, auditoria `PREVIEW_CREATED`. `preview_hash` na configuração pausada atual: `8a3e2b611697306268beedf08cc7bfdd57e980ac110ba718ffa05f17cf779a79`; este hash mudará após habilitar/configurar o destino e deve ser consultado novamente. Flags globais `false`, todos os destinos pausados, monitoramento/conectores comerciais desativados. Nenhum envio ou consulta real ao Telegram nesta implementação; nenhuma autorização anterior reutilizada.
