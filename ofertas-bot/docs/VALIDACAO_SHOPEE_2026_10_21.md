# Shopee Affiliate Open API — consulta controlada de 21/10/2026

**Resultado da consulta original: acesso real de leitura confirmado; normalização então bloqueada.** A correção offline posterior está registrada ao final deste documento. Uma única consulta `productOfferV2` retornou HTTP **200**, sem erros GraphQL, e **um produto**. Não houve retry, paginação adicional, geração de links, persistência comercial ou publicação.

## Contrato e autorização

- Endpoint oficial: `POST https://open-api.affiliate.shopee.com.br/graphql`.
- [Explorer oficial BR V2](https://open-api.affiliate.shopee.com.br/explorer/v2): HTML público consultado com HTTP 200; esquema e gerador de assinatura conferidos novamente nesta execução.
- Autenticação: `Authorization: SHA256 Credential=<AppId>, Timestamp=<segundos Unix>, Signature=<assinatura>`. A assinatura é SHA256 hexadecimal da concatenação **AppId + timestamp + corpo JSON exato + Secret**. O gerador oficial usa `Math.ceil` no timestamp. Cabeçalho real e credenciais não foram registrados.
- É necessário acesso da conta à Affiliate Open API, com App ID/Secret e autorização para a query de ofertas. A [ajuda oficial](https://help.shopee.sg/portal/10/article/191702-API-Access) inclui leitura de listas de produtos nas capacidades do programa; a habilitação regional/por conta não é demonstrada apenas pela documentação.
- Credenciais locais presentes. A resposta autenticada comprova acesso a **esta leitura `productOfferV2` neste momento**; não comprova todas as permissões, quotas, geração de links ou disponibilidade futura. Não foi consultada uma lista independente de permissões concedidas.
- O conector existente foi habilitado exclusivamente na instância em memória desta consulta, com `max_retries=0`, transporte sem redirects/retries e limite adicional de uma requisição. A configuração permanente `SHOPEE_ENABLED=false` foi preservada.

## Consulta e dados efetivos

Query: `productOfferV2(keyword:"PlayStation 5",page:1,limit:1)`. Campos solicitados: `itemId shopId productName productLink imageUrl price priceMin priceMax productCatIds commissionRate commission` e paginação. Nenhuma mutation foi executada; `offerLink` não foi solicitado.

| Campo | Valor efetivamente retornado |
|---|---|
| `productName` | Cabo HDMI 2.1 8K PS5 Gamer 1 Metro Melhor Qualidade Disponivel! Ponta de Ouro |
| `itemId` / `shopId` | `22293101052` / `1018042820` |
| `price` / `priceMin` / `priceMax` | `"59"` / `"59"` / `"59"` |
| `commissionRate` / `commission` | `"0.38"` / `"22.42"` |
| `productCatIds` | `[100535, 100583, 0]` |
| `productLink` | URL canônica de produto retornada; não é comprovação de link de afiliado próprio |
| `imageUrl` | Presente, no domínio oficial de imagens Shopee |

O resultado é **um acessório**, não um console PlayStation 5. Busca por palavra-chave não garante elegibilidade pelos filtros existentes. Comissão não é desconto. Não foram obtidos preço anterior, histórico comparável, cupom, estoque, condições de parcelamento, variante ou comprovação de acesso público ao preço. Nenhum desses dados foi inferido.

## Limitação local confirmada

A API respondeu corretamente, mas `ShopeeConnector._snapshot` rejeitou a categoria **0**: a validação atual aceita somente identificadores positivos. Código local **`INVALID_PAYLOAD`**, sem erro de autorização externo. O mesmo erro foi reproduzido offline com os dados retornados; uma cópia diagnóstica removendo apenas o zero normalizou o preço como Decimal `59.00`, mantendo disponibilidade e acesso como `UNKNOWN`.

Essa cópia foi usada somente em memória para identificar a causa; a resposta original, o conector e as regras de categorias não foram alterados. Antes de conectar o resultado ao motor, é necessário tratar conservadoramente IDs de categoria sem classificação, preservar os IDs válidos e adicionar regressão. O acesso real à API está demonstrado; o fluxo de ingestão completo ainda não está operacional.

IDs, nome, URL canônica, imagem, categorias válidas e preços efetivamente retornados poderão alimentar cadastro/histórico após essa correção. Preço/intervalo e comissão devem permanecer evidências distintas. Elegibilidade continua dependendo de histórico comparável e condições comerciais verificadas; a consulta não autorizou publicação ou operação automática.

## Segurança e validação

- **1 tentativa HTTP**, **1 resposta HTTP 200**, **1 produto**, **0 erros GraphQL**.
- **0 retries**, **0 consultas de próxima página**, **0 mutations de link**, **0 chamadas Telegram**, **0 gravações no banco**.
- `.env` confirmado inalterado antes/depois. Shopee e Mercado Livre desativados; ambas as flags reais Telegram `false`; simulação automática desativada. Nenhum scheduler foi iniciado nesta execução. Não se reconfirmou aqui a pausa persistida do monitoramento/grupo, pois este diagnóstico não consultou o banco.
- Nove categorias, filtros, dados, tokens, migrações e alterações locais preservados. Nenhum código de produção alterado e nenhum commit criado.
- Testes efetivamente executados: `python -m pytest -q tests/test_shopee.py` — **86 aprovados**, todos offline/mockados. Suíte completa e PostgreSQL não foram repetidos neste diagnóstico.
- [Evidência sanitizada JSON](VALIDACAO_SHOPEE_2026_10_21.json). Reserva local da tentativa impede repetição acidental deste ensaio; não houve armazenamento de corpo bruto, assinatura ou credenciais.

Próximo passo: corrigir a normalização de categoria zero com teste de regressão, sem nova consulta real por padrão. Ativação comercial/automática ou publicação continuam dependentes de autorização específica; Mercado Livre segue suspenso. Fases 5/6 não iniciadas.


## Correção posterior — normalização offline

Sem repetir a consulta oficial, a normalização foi ajustada apenas para omitir o **inteiro zero** em `productCatIds`. IDs positivos e ordem permanecem; somente zeros produz lista vazia e `category_external_id=None`. Booleanos, floats, string `"0"`, negativos, estruturas inválidas e limites anteriores continuam sendo rejeitados. Nenhuma categoria externa foi inventada.

O nó observado foi preservado em fixture sanitizada dedicada. Regressão mockada confirma preço Decimal `59.00`, IDs `100535` e `100583`, comissão separada, disponibilidade/acesso `UNKNOWN` e preço anterior ausente. O resultado da busca não passa a ser console: o nome real de cabo HDMI é preservado; Games é uma categoria ampla e o filtro específico PlayStation 5 o exclui da outbox.

**179 testes direcionados aprovados**. **1.129 testes aprovados na suíte integral**, incluindo **30 PostgreSQL reais** em schemas UUID isolados de `ofertas_bot_test`, **zero falhas e zero testes ignorados**. Os 179 direcionados são um subconjunto (120 Shopee e 59 simulação), não uma soma adicional. Há 17 avisos de depreciação de dependências, sem falhas. Ruff passou e 132 arquivos estão formatados; Alembic `upgrade head/current/check` passou na revisão `0007_simulated_automation`, sem drift. A conferência final confirmou preservação dos dados principais por fingerprints, `.env` inalterado, nove categorias, grupo pausado e monitoramento desativado. O JSON da consulta original permanece inalterado como evidência histórica do bloqueio anterior. Esta correção não comprova estoque, preço público, cupom, desconto, variante ou elegibilidade automática e não habilita nenhuma integração.
