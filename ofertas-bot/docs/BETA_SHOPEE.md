# Modo Beta Shopee: ofertas automáticas no Telegram

Worker enxuto que, a cada ciclo, busca ofertas na API oficial de afiliados da Shopee
(`productOfferV2`), aplica filtros, evita repetição, confere o preço de novo logo
antes de postar e publica no grupo do Telegram com o link de afiliado (`offerLink`).

Política do beta (aprovada em 17/11/2026): os dados mostrados vêm da API oficial de
afiliados, sem inventar preço "de", estoque ou cupom. Toda mensagem traz o aviso
"Preço e estoque podem mudar a qualquer momento." e o rótulo de publicidade.
Os pilotos antigos (`auto_pilot_*`) continuam intactos e independentes.

## Travas de segurança

| Trava | Padrão | Efeito |
|---|---|---|
| `BETA_ENABLED` | `false` | Desligado: o ciclo não faz nada. |
| `BETA_DRY_RUN` | `true` | Simulação: grava no banco como `DRY_RUN`, nunca chama o Telegram. |
| `TELEGRAM_PUBLISHER_ENABLED`, `TELEGRAM_REAL_SENDS_AUTHORIZED`, `TELEGRAM_ALLOWED_CHAT_IDS` | desligados | Mesmas travas do publicador existente; sem elas o envio real é recusado. |
| Arquivo `.runtime/beta.pause` | ausente | `python -m app.beta_worker pause` pausa sem parar o processo. |
| Limites | 1 por ciclo, 2 por hora, 20 por dia | Nunca publica além disso. |
| Silêncio | 22h às 8h (Brasília) | Não busca nem publica nesse horário. |
| Repetição | 7 dias | Mesmo item só volta se o preço cair 10% ou mais. |
| Envio incerto | — | Se o Telegram não confirmar, o item conta como publicado (não duplica). |

Erros de permissão, credencial ou limite do Telegram interrompem o ciclo; o próximo
ciclo tenta de novo. Nenhum segredo é gravado em log.

## Filtros (todos no `.env`)

`BETA_MIN_DISCOUNT_PERCENT` (20), `BETA_MIN_RATING` (4.5), `BETA_MIN_SALES` (50),
`BETA_MIN_COMMISSION_RATE` (0 = desligado), `BETA_MIN_PRICE` (10) e
`BETA_MAX_PRICE` (2000). A pontuação prioriza desconto, depois avaliação, vendas e
comissão. `BETA_KEYWORDS` é uma lista JSON de termos; cada ciclo usa
`BETA_KEYWORDS_PER_CYCLE` termos, em rodízio.

Se essas buscas não renderem ofertas novas (tudo repetido, fora do tema ou abaixo dos
filtros), o ciclo **não desiste**: busca as próximas páginas dos mesmos termos (até
`BETA_MAX_PAGES_PER_KEYWORD`, padrão 3) e depois os próximos termos do rodízio, até
publicar `BETA_MAX_POSTS_PER_CYCLE` ofertas ou fazer `BETA_MAX_SEARCHES_PER_CYCLE`
buscas (padrão 15). Um termo que dá erro na Shopee (ex.: `UPSTREAM_GRAPHQL_ERROR`)
é pulado só naquele ciclo e a busca segue com os outros. O log `beta_cycle` mostra
em `searches` cada busca feita (`"biquíni p2"` = página 2) e o status `NO_OFFERS`
quando nada passou nos filtros.

## Rodando no PC (Windows/PowerShell), na pasta `ofertas-bot`

1. Banco no ar e migração nova (cria só a tabela `beta_posts`):

   ```powershell
   docker compose up -d postgres
   .venv/Scripts/python.exe -m alembic upgrade head
   ```

2. No `.env`, copie o bloco "Modo Beta Shopee" do `.env.example` e ajuste os termos.
   Para buscar na Shopee: `SHOPEE_ENABLED=true` com `SHOPEE_APP_ID`/`SHOPEE_SECRET`.

3. **Etapa 1: simulação.** `BETA_ENABLED=true`, `BETA_DRY_RUN=true`.

   ```powershell
   .venv/Scripts/python.exe -m app.beta_worker once      # um ciclo, mostra o resultado
   .venv/Scripts/python.exe -m app.beta_worker report    # o que teria sido postado
   .venv/Scripts/python.exe -m app.beta_worker run       # loop contínuo; Ctrl+C encerra
   ```

   Olhe o `report` e o texto das mensagens (coluna `text` da tabela `beta_posts`).
   Ajuste filtros/termos até gostar do que aparece.

4. **Etapa 2: grupo de teste.** Coloque o bot como admin de um grupo de teste e use o
   ID dele em `BETA_CHAT_ID` e em `TELEGRAM_ALLOWED_CHAT_IDS`. Depois:
   `BETA_DRY_RUN=false`, `TELEGRAM_PUBLISHER_ENABLED=true`,
   `TELEGRAM_REAL_SENDS_AUTHORIZED=true`, e rode `once`.

5. **Etapa 3: grupo Viana Ofertas.** Troque `BETA_CHAT_ID` e a lista pelo ID do grupo
   real e rode `run`. Comece com os limites baixos do padrão.

6. **Conferir atribuição.** Clique num link publicado e confira, no painel de
   afiliados da Shopee, que o clique aparece na sua conta.

## Operação

- `pause` / `resume`: pausa e retoma sem reiniciar.
- `report`: contagens das últimas 24h e as 20 últimas ofertas.
- `health`: saída 0 se o worker registrou um ciclo recentemente
  (`.runtime/beta-worker-health.json`).
- Parar de vez: `Ctrl+C` no terminal do `run`, ou `BETA_ENABLED=false` e reiniciar.

## Filtro por nicho (ex.: moda feminina)

- `BETA_KEYWORDS`: termos buscados na Shopee, em rodízio.
- `BETA_REQUIRE_KEYWORD_IN_TITLE=true`: o título precisa conter as palavras do termo.
  Masculino/feminino e singular/plural contam como a mesma palavra
  ("blusa feminina" aceita "Blusas Femininas"; "vestido feminina" aceita "Vestido Feminino").
- `BETA_EXCLUDED_TERMS`: descarta títulos com algum desses termos
  (ex.: `["masculino","infantil"]`). Aparece no relatório como `EXCLUDED_TERM`.

## WhatsApp

O WhatsApp não tem API oficial para postar em grupos comuns, então o bot usa um
gateway local (`whatsapp-gateway/`, biblioteca Baileys) que se conecta a um número
como "aparelho conectado", do mesmo jeito que o WhatsApp Web.

**Risco:** é uma integração não oficial. O WhatsApp pode banir números que mandam
muitas mensagens com link. Use um número separado (chip só para o bot), poste em
um grupo/canal seu e comece com poucos envios por hora.

`BETA_CHANNELS` define onde cada oferta sai. Com `["telegram","whatsapp"]` a mesma
oferta vai para os dois; se um canal falhar, o outro segue normalmente. A coluna
`whatsapp_status` da tabela `beta_posts` guarda o resultado do WhatsApp.

### Ligando no PC (PowerShell, pasta `ofertas-bot`)

1. Requer Node.js 20+ (`node -v`). Instale as dependências do gateway uma vez:

   ```powershell
   cd whatsapp-gateway; npm install; cd ..
   .venv/Scripts/python.exe -m alembic upgrade head   # coluna whatsapp_status
   ```

2. Inicie o gateway num terminal separado e deixe aberto:

   ```powershell
   cd whatsapp-gateway; npm start
   ```

   Conecte o número: abra `http://127.0.0.1:3100/qr` e leia o QR no celular
   (WhatsApp > Aparelhos conectados > Conectar aparelho). Alternativa: defina
   `WHATSAPP_PAIRING_NUMBER` no `.env` e digite o código mostrado no terminal em
   "Conectar com número de telefone". A sessão fica salva em `.runtime/whatsapp-auth`.

3. Descubra o ID do grupo: `.venv/Scripts/python.exe -m app.beta_worker whatsapp`
   mostra o estado, e `http://127.0.0.1:3100/groups` lista os grupos do número.

4. No `.env`: `WHATSAPP_PUBLISHER_ENABLED=true`, `WHATSAPP_CHAT_ID=<id>@g.us` e
   `BETA_CHANNELS=["telegram","whatsapp"]`. Reinicie o `beta_worker run`.

Se o gateway estiver fechado ou desconectado, o ciclo registra
`WHATSAPP_GATEWAY_DOWN`/`WHATSAPP_NOT_CONNECTED` e continua postando no Telegram.
Para desconectar de vez: remova o aparelho no celular e apague `.runtime/whatsapp-auth`.
