# Diagnóstico do HTTP 403 — Mercado Livre

Diagnóstico somente leitura da Fase 3.2, em **18/10/2026, 21:56:19–21:56:25 America/Sao_Paulo** (19/10/2026, 00:56 UTC). Foram realizados cinco GETs com token válido, sem novo OAuth ou refresh. Nenhum dado pessoal, Client ID, segredo, token ou URI temporária é reproduzido aqui.

## Evidências reais

| Consulta | Resultado sanitizado |
| --- | --- |
| `/users/me` | HTTP 200; identidade compatível com o armazenamento e site MLB. |
| `/applications/{app}` | HTTP 200; aplicação correspondente, `active=true`, `sandbox_mode=false`, escopos configurados. |
| `/users/{authorized}/applications` | HTTP 200; grant correspondente e escopos iguais aos do token. |
| `/users/{authorized}/items/search?limit=1` | HTTP 200, `results=[]`; nenhum item próprio retornou nessa consulta. Não houve comparação com item próprio nem criação de anúncio. |
| `/items/MLB3810190843` | HTTP 403, `error=access_denied`. Código específico não identificado na saída sanitizada; `PA_UNAUTHORIZED_RESULT_FROM_POLICIES` não foi identificado. |

Os escopos incluem `read`, `write`, `offline_access`, `urn:ml:all:publish-sync:/read-only` e `urn:ml:mktp:publish-sync:/read-only`. Não foram encontrados escopos `urn:mp:`.

## Conclusão e limites

A ausência simples de permissão publish-sync e a expiração do token **não foram observadas**. A recusa de acesso ao recurso está confirmada; sua causa específica permanece aberta. Não se pode atribuí-la a ownership, condição de afiliado ou restrição do anúncio sem confirmação oficial. O retorno vazio da busca própria não permite comparar o comportamento com um anúncio do titular.

A documentação de [permissões funcionais](https://developers.mercadolivre.com.br/pt_br/produto-consulta-de-usuarios/permissoes-funcionais) associa items/prices à função Publicação e sincronização. [Gerencie seu aplicativo](https://developers.mercadolivre.com.br/devcenter/gerencie-seu-aplicativo) documenta inspeção da aplicação/grants e a separação de escopos Mercado Livre/Mercado Pago. O guia [Erro 403](https://developers.mercadolivre.com.br/pt_br/autenticacao-e-autorizacao/erro-403) lista várias causas possíveis, incluindo permissões, IP, estado cadastral e bloqueios; não diagnostica este caso individual. Fontes oficiais consultadas em 18/10/2026.

Não foi localizada regra oficial que estabeleça proibição ou autorização geral dessa leitura para afiliados brasileiros. A [documentação de preços](https://developers.mercadolivre.com.br/pt_br/servicos-gerenciamento-de-contatos/api-de-precos) contempla token que não pertence ao vendedor/item e limita metadados confidenciais. Isso não comprova autorização irrestrita para `/items/{id}` nem determina a política aplicada neste caso.

## Texto pronto para o suporte

Solicito identificar a política que recusou GET `/items/MLB3810190843` em **2026-10-19T00:56:25.111333+00:00** (18/10/2026, 21:56:25.111333 America/Sao_Paulo), HTTP 403, `access_denied`.

**x-request-id da falha:** `924d22d4-8766-40b8-a963-194a9100260d`.

Na mesma rodada, `/users/me`, inspeção da aplicação e grant retornaram 200. Aplicação ativa, fora de sandbox, token válido e escopos do grant iguais aos do token, incluindo os dois publish-sync de leitura acima; nenhum `urn:mp:`. A busca de itens próprios retornou vazia.

Podem confirmar qual política/código explica a recusa, se falta habilitação efetiva da aplicação e se há restrição de IP, cadastro ou do anúncio? Para uma conta participante do programa de afiliados brasileiro, qual acesso oficial permite ler metadados de anúncios de terceiros? Esse recurso exige autorização do vendedor ou outra aprovação? Solicito a documentação e o procedimento aplicáveis, sem presumir proibição ou acesso garantido.

Correlações adicionais dos HTTP 200: aplicação `dbc43192-137a-44f6-b11c-e7839b914c68`; grant `4c3db355-6692-422a-b2e7-4d9f36afeb7c`.

Este texto não foi enviado. Código e os 755 testes anteriores foram preservados; nenhuma suíte foi repetida nesta atualização documental.
