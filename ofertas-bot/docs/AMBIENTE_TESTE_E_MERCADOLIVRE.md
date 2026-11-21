# Ambiente de teste e pesquisa Mercado Livre (21/11/2026)

## Ambiente de teste no PC

Separado da produção, que continua rodando em `Documents\ChatGPT\Chat_BOT\ofertas-bot`.

| Item | Produção | Teste |
|---|---|---|
| Pasta | `Chat_BOT\ofertas-bot` | `Chat_BOT_teste\ofertas-bot` (clone próprio, branch `claude/project-thread-jefsea`) |
| Python | `.venv` da produção | `.venv` próprio (`uv sync --extra dev`) |
| Banco | `ofertas_bot` | `ofertas_bot_sandbox` (mesmo Postgres do Docker, base separada) |
| Envio | Telegram + WhatsApp reais | `BETA_DRY_RUN=true`, `TELEGRAM_PUBLISHER_ENABLED=false`, `WHATSAPP_PUBLISHER_ENABLED=false`, gateway apontando para a porta 3199 (inexistente) |
| Pausa/saúde | `.runtime` da produção | `.runtime` próprio (pausar o teste não pausa a produção) |

O `.env` de teste é uma cópia do da produção com as travas acima no fim do arquivo.
Não está no Git. Para rodar um ciclo simulado:

```powershell
cd C:\Users\eduardo.viana.3\Documents\ChatGPT\Chat_BOT_teste\ofertas-bot
.venv\Scripts\python.exe -m app.beta_worker once
.venv\Scripts\python.exe -m app.beta_worker report
```

Validado: um ciclo gravou 1 post com status `DRY_RUN` em `ofertas_bot_sandbox`, sem envio.

Para testar envio real mais tarde, crie grupos de teste (Telegram e, se quiser, WhatsApp)
e troque só `BETA_CHAT_ID`/`TELEGRAM_ALLOWED_CHAT_IDS` no `.env` de teste. Um segundo
gateway WhatsApp precisaria de outra porta e outra sessão (outro número ou aparelho vinculado).

## O que a API oficial do Mercado Livre ainda entrega (testado do PC)

Token de aplicação via `grant_type=client_credentials` (HTTP 200). Não usa nem renova o
token OAuth do usuário. Script: `scripts/probe_mercadolivre.py "<termo>"`.

| Endpoint | Resultado |
|---|---|
| `/sites/MLB/search` (com e sem token) | 403 `forbidden` |
| `/items/{id}`, `/items?ids=`, `/items/{id}/prices`, `/items/{id}/sale_price` | 403 `access_denied` |
| `/sites/MLB/categories`, `/sites/MLB/domain_discovery/search?q=` | 200 (termo → categoria) |
| `/highlights/MLB/category/{cat}` (20 mais vendidos) | 200, mistura `ITEM`, `PRODUCT`, `USER_PRODUCT` |
| `/products/{id}` (catálogo `PRODUCT`) | 200 com nome e fotos |
| `/user-products/{id}`, `/products/{MLBU…}` | 403 |
| `/products/{id}/items` (`PRODUCT` e `USER_PRODUCT`) | 200 com `item_id`, `price`, `original_price` |
| `/products/search?site_id=MLB&q=` | 200 (catálogo; em moda os resultados são fracos) |

Na prática, em moda feminina: "Vestidos" teve 4 de 20 mais vendidos com preço;
"Camisetas e Regatas" teve 4 de 20, todos `USER_PRODUCT`, com preço e preço "de" mas
**sem título nem foto**. Os `ITEM` (a maioria) ficam totalmente bloqueados.

## Link de afiliado

Não existe API oficial pública para gerar link de afiliado do Mercado Livre. O caminho
documentado é o gerador do painel de afiliados (um link por vez). Ferramentas de terceiros
geram links reutilizando os cookies de sessão da conta logada em endpoints internos não
documentados. Isso pode quebrar a qualquer momento e pode violar os termos, com risco de
perder a conta de afiliado e as comissões.

## Conclusão

Com a API oficial dá para descobrir poucos produtos por categoria com preço e desconto,
mas sem título/foto na maioria e sem link de afiliado automático. Não dá para replicar
o beta da Shopee só com caminhos oficiais.
