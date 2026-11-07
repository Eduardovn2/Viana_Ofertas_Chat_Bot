# Gate de viabilidade — Fase 3

Consulta: **18/10/2026**. Documento produzido antes da implementação dos conectores. Nenhuma chamada comercial foi realizada: não há credenciais nem flags de integração configuradas no ambiente consultado. Estado inicial: **FASE 3 PARCIAL / BLOQUEADA POR ACESSO**.

## Fontes oficiais e evidência

- Mercado Livre: [Preços de produtos](https://developers.mercadolivre.com.br/devcenter/api-de-precos), conteúdo oficial indexado, atualizado em 26/02/2026. As páginas fornecidas na especificação retornaram HTTP 403 no acesso direto; o conteúdo indexado foi recuperado sem contornar autenticação.
- Mercado Livre: [Itens e buscas](https://developers.mercadolivre.com.br/pt_br/itens-e-buscas), [Cupons do vendedor](https://developers.mercadolivre.com.br/pt_br/api-docs-pt-br/cupons-do-vendedor) e [Link Builder](https://www.mercadolivre.com.br/l/afiliados-gere-seus-links): evidência oficial indexada; acesso direto parcialmente bloqueado.
- Mercado Livre: [Boas práticas](https://developers.mercadolivre.com.br/boas-praticas-para-usar-a-plataforma): reduzir e distribuir requisições ao receber 429; nenhuma quota numérica por conta foi comprovada.
- Shopee: [Explorer BR](https://open-api.affiliate.shopee.com.br/explorer) e [Explorer V2](https://open-api.affiliate.shopee.com.br/explorer/v2). O HTML público do V2 retornou 200 e inclui o esquema oficial e a implementação do gerador de requisições. A leitura dessa documentação não autentica a conta do proprietário.
- Shopee: [Ajuda de links](https://help.shopee.com.br/portal/10/article/128461-Como-gerar-links-de-produtos-ou-lojas). Disponibilidade operacional depende do programa e da conta.

## Mercado Livre

| Capacidade | Estado inicial | Contrato e limite |
| --- | --- | --- |
| Metadados de item | REQUIRES_CREDENTIALS | GET `/items/{id}`, Bearer; somente campos comerciais não sensíveis. |
| Preço atual | REQUIRES_CREDENTIALS | GET `/items/{id}/sale_price?context=channel_marketplace`; `amount`, `regular_amount`, `currency_id`, `price_id`. |
| Condições de preço | REQUIRES_CREDENTIALS | GET `/items/{id}/prices`; conferir contexto e correspondência do preço. Condições ocultas permanecem desconhecidas. |
| Busca automática para afiliado | UNVERIFIED | A documentação de buscas não comprova acesso efetivo desta conta. Desativada até validação específica. |
| Cupons públicos de terceiros | UNVERIFIED | APIs de vendedor não comprovam pesquisa/aplicabilidade geral. Nenhuma campanha será criada ou alterada. |
| Link de afiliado manual | SUPPORTED | Link Builder oficial; origem manual registrada, sem gerar parâmetros ou automatizar interfaces. |
| Conversão automática de link | UNVERIFIED | Não implementada como operação de rede. |
| Preço específico de variante | UNVERIFIED | Não foi confirmado parâmetro de variante para a API atual de preços; não usar preços legados de variantes. |

Os campos antigos de preço de `/items` não serão usados. A ausência de metadados privados para tokens de outros vendedores não prova permissão para esta conta de afiliado. Preços VIP, B2B, por quantidade e condições insuficientes não serão classificados como públicos. A disponibilidade da conta só poderá ser confirmada mediante requisição autorizada com credenciais próprias.

## Shopee

| Capacidade | Estado inicial | Contrato e limite |
| --- | --- | --- |
| Consulta/descoberta de produtos | REQUIRES_CREDENTIALS | Query oficial `productOfferV2`; referências por `shopId`/`itemId`, paginação e filtros documentados. |
| Ofertas de campanha/loja | REQUIRES_CREDENTIALS | `shopeeOfferV2` está no esquema; não converter campanha genérica em preço de produto ou cupom confirmado. |
| Conversão de link | REQUIRES_CREDENTIALS | `generateShortLink(input: ShortLinkInput!)`, apenas se explicitamente habilitada e autorizada. |
| Cupom e checkout | UNVERIFIED | Esquema examinado não comprova endpoint de validação de aplicação de cupom. Cadastro manual conservador. |
| Estoque, variante e preço antigo observado | UNVERIFIED | Campos não presentes no tipo de produto consultado. Preservar desconhecidos; não inverter desconto percentual. |

Endpoint BR publicado pelo Explorer: `https://open-api.affiliate.shopee.com.br/graphql`. O corpo JSON exato contém a query GraphQL; a assinatura SHA256 hexadecimal concatena AppId, timestamp, corpo serializado e Secret. O código publicado usa timestamp arredondado para cima em segundos. Valores de AppId/Secret e cabeçalhos nunca entram em logs.

O esquema fornece preços como strings, IDs, nome, links, imagem, categorias e dados comerciais opcionais. Intervalos de preço não comprovam preço de uma variante. A existência de uma oferta na API não comprova estoque. Sem confirmação suficiente, disponibilidade/acesso permanecem desconhecidos e não produzem elegibilidade de publicação. Não há quota numérica confirmada para esta conta.

## Regras de habilitação e resultado real

- Flags independentes desligadas por padrão; startup sem rede mesmo sem credenciais.
- Credenciais só no ambiente local; `.env` existente não será alterado.
- Configuração não equivale a autorização: 401/403 encerram a tentativa, sem contorno; 429/5xx têm limites, espera e cache conservadores.
- Apenas APIs oficiais documentadas; nenhuma compra, modificação de vendedor ou publicação.
- Todos os exemplos de teste serão identificados como **sintéticos**, sem alegar captura real.
- Um teste comercial supervisionado por plataforma só poderá ocorrer com credenciais autorizadas e flag explícita. **Nenhum produto real foi consultado nesta etapa do gate.**

Este gate poderá receber evidências adicionais mantendo a distinção entre documentação, configuração e acesso efetivamente comprovado.

## Resultado da implementação

Os dois adaptadores estão implementados com flags desligadas por padrão. Mercado Livre usa somente metadados, `/sale_price` e `/prices`; descoberta ampla, cupons de terceiros e conversão automática seguem `UnsupportedCapability`. Shopee usa `productOfferV2` e, por ação administrativa e flag própria, `generateShortLink`; `shopeeOfferV2` continua apenas documentada como campanha/coleção, sem endpoint administrativo de produto fictício.

Identidade, preços e condições são normalizados de modo conservador; variantes não resolvidas não recebem preço genérico. O acesso público/estoque não demonstrados pela Shopee continuam `UNKNOWN`. Um link curto não comprova propriedade por domínio: geração só fica verificada com resposta autenticada coerente com a identidade canônica; formatos ambíguos requerem confirmação manual.

Erros externos ficam em tabela própria. Rate limits e `Retry-After` são compartilhados no PostgreSQL por escopo de credencial com hash; 401/403 persistem bloqueio até consulta administrativa bem-sucedida. A fila continua dry-run e o destino Ofertas de Casa desativado. Os testes usam dados **sintéticos**, não respostas comerciais capturadas. Consulte [a validação técnica](VALIDACAO_FASE_3.md).

**Resultado comercial:** não executado. `MERCADO_LIVRE_ACCESS_TOKEN`, `SHOPEE_APP_ID` e `SHOPEE_SECRET` não estavam configurados. Não existe preço, cupom, estoque ou comissão real verificado nesta entrega. A fase permanece **PARCIAL / BLOQUEADA POR ACESSO**, sem avanço à Fase 4.
