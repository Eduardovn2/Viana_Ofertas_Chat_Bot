# Validação da conexão Telegram — 20/10/2026

Execução autorizada somente para consultas de leitura e configuração local. Base preservada: commit `b920cd5`, Fase 4 com 982 testes aprovados, incluindo 12 PostgreSQL reais. Nenhum código de aplicação, conector comercial ou teste foi alterado nesta validação.

## Resultados reais

- Token local presente, sem exibição, gravação em relatório ou alteração do valor.
- `getMe`: HTTP 200; `is_bot=true`, identidade confirmada como **@VianinhaOfertasBot**.
- `getChat`: HTTP 200; ID **-5309308689**, título **Viana Ofertas**, tipo `group`.
- `getChatMember`, consultando o próprio ID retornado por `getMe`: HTTP 200; bot integrante com status **administrator**.
- Direitos administrativos retornados incluem gerenciar o chat, excluir mensagens, convidar/restringir membros e fixar mensagens. Promover membros não é permitido. O papel de administrador foi confirmado; capacidade efetiva de publicar não foi exercitada com um envio.
- Métodos utilizados exclusivamente: `getMe`, `getChat`, `getChatMember`. Nenhum `sendMessage` ou `sendPhoto`; zero mensagens enviadas.

As consultas usam a [API oficial](https://core.telegram.org/bots/api#getme), em HTTPS, sem redirects ou retries e sem registrar URLs autenticadas ou corpos privados. As informações de associação seguem o contrato de [getChatMember](https://core.telegram.org/bots/api#getchatmember). Credenciais e respostas completas não foram incluídas nos arquivos versionados.

## Configuração local

O `.env` tinha `TELEGRAM_ALLOWED_CHAT_IDS` como escalar numérico, causando erro de validação. Somente esse campo foi corrigido para uma lista JSON com o mesmo grupo: `["-5309308689"]`. O token e demais credenciais foram preservados. Allowlist não autoriza envio por si só.

No PostgreSQL, apenas o destino **id 2** foi configurado para `category_mode=all`, aceitando as **nove categorias ativas**: Casa e Decoração, Materiais e Ferramentas, Eletrodomésticos, Moda, Infantil, Tecnologia, Games, Beleza e Gerais. O catálogo de categorias, campos e associações dos demais destinos foram comparados antes/depois e preservados.

O destino permanece `enabled=false` e `manual_approval_required=true`. `TELEGRAM_PUBLISHER_ENABLED=false`, `TELEGRAM_REAL_SENDS_AUTHORIZED=false`, `MERCADO_LIVRE_ENABLED=false` e `SHOPEE_ENABLED=false` foram confirmados. Não foram acrescentados registros de publicação ou ofertas fictícias. Não houve consultas comerciais.

## Prévia e autorização futura

A [mensagem exata de simulação](PREVIA_TESTE_TELEGRAM.txt) está preparada somente como artefato local, em texto, sem imagem, produto, preço ou link comercial. Não foi inserida como uma oferta real nem despachada pela outbox. A leitura remove apenas a quebra de linha final do arquivo antes de formar o conteúdo da mensagem.

Conteúdo validado pelo verificador de mensagem existente: 215 unidades UTF-16, `parse_mode=HTML`, sem foto. SHA-256 do texto UTF-8 sem quebra final: `2aa7bceb17fe76ccfcb202ce9106d0d44c76a9b12d785dda0cdb990d22b88963`. Isso identifica a prévia; não é uma aprovação de envio.

Para autorizar uma única publicação futura, o responsável deverá aprovar explicitamente esse conteúdo, o grupo **-5309308689** e o limite de **um envio**. A execução precisará registrar essa autorização/tentativa, revalidar bot/grupo, abrir os bloqueios somente para a operação pontual e restaurar as flags e a pausa ao terminar. Resultado incerto não permite repetição automática. A autorização ainda não foi concedida nesta execução.

Os 982 testes anteriores permanecem como validação da base; a suíte integral não foi repetida para estas consultas/configuração. Fases 5 e 6 não foram iniciadas; nenhum commit ou push foi criado nesta validação.
