# Publicação técnica autorizada e bloqueada — 20/10/2026

## Autorização recebida

O administrador autorizou explicitamente, no chat de desenvolvimento, uma única tentativa da mensagem de simulação pelo **@VianinhaOfertasBot**, exclusivamente para **Viana Ofertas**, Chat ID **-5309308689**. A autorização não abrange ofertas comerciais, outros destinos, publicação automática, retries ou outro mecanismo de envio.

Conteúdo autorizado: [prévia exata](PREVIA_TESTE_TELEGRAM.txt), removendo apenas a quebra final do arquivo. SHA-256 UTF-8: `2aa7bceb17fe76ccfcb202ce9106d0d44c76a9b12d785dda0cdb990d22b88963`.

## Resultado: BLOCKED

A implementação existente não suporta mensagem técnica independente de oferta comercial. `TelegramPublication` exige `offer_id` e `product_id` não nulos; `TelegramPublicationService._context` exige `read_manual_offer` e monta o conteúdo com produto, preço e link de afiliado verificados.

Aplicado o item 4 da autorização: interromper sem inventar produtos/ofertas e sem contornar outbox, aprovação, hash, reserva e auditoria. O publisher não foi chamado diretamente e nenhum mecanismo alternativo foi implementado ou autorizado.

## Verificações desta execução

- PostgreSQL acessível em consulta de leitura; destino id 2 corresponde a **-5309308689**.
- Token presente, sem exibir seu conteúdo; grupo presente na allowlist.
- Nenhuma tentativa anterior com o hash exato; outbox do destino vazia.
- Destino pausado (`enabled=false`), aprovação manual obrigatória e `category_mode=all`.
- Nenhum outro destino habilitado.
- `TELEGRAM_PUBLISHER_ENABLED=false` e `TELEGRAM_REAL_SENDS_AUTHORIZED=false` antes e depois da verificação, sem alterações temporárias ou permanentes.
- Mercado Livre e Shopee permanecem desativados; nenhum monitoramento ou consulta comercial foi acionado.
- Identidade e permissões possuem evidência de leitura da validação anterior em [VALIDACAO_CONEXAO_TELEGRAM.md](VALIDACAO_CONEXAO_TELEGRAM.md). Não foram reconsultadas nesta execução: o bloqueio estrutural foi identificado antes de qualquer acesso ao Telegram. Precisam de nova confirmação imediatamente antes de eventual envio futuro.

**Chamadas de publicação: 0. Conteúdo enviado: nenhum. Telegram message_id: inexistente. Nenhum grupo recebeu mensagem nesta execução.** Não houve timeout, tentativa HTTP nem resultado incerto a resolver. Nenhuma reserva comercial foi criada.

Este arquivo registra localmente a autorização recebida e o bloqueio; não representa aprovação persistida na outbox nem confirmação de envio. Nenhum registro fictício foi inserido no banco.

## Menor adaptação segura proposta, não implementada

Adicionar um tipo persistente de publicação técnica independente de produtos/ofertas, reutilizando o publisher oficial e os controles de destino, prévia/hash, aprovação explícita, reserva anterior ao HTTP, auditoria e estado `UNCERTAIN`. Uma chave única por destino/conteúdo e limite de uma tentativa devem impedir retries, concorrência e fallback. Preservar as constraints comerciais existentes e validar a adaptação com mocks e PostgreSQL antes de solicitar uma nova operação pontual.

Não houve alterações de aplicação, migrações, testes, credenciais ou dados nesta execução. Os 982 testes aprovados anteriormente não foram reexecutados neste diagnóstico documental. Fases 5 e 6 não iniciadas.
