# Validação da Fase 3.2 — retomada de 18/10/2026

**FASE 3.2 NÃO OPERACIONAL para a consulta comercial: HTTP 403 em `/items/MLB3810190843`, inclusive após novo consentimento e reteste com novo token.** O gerenciador de tokens e a integração OAuth/conector estão implementados e testados. O runner preservou a falha auditável; nenhum preço ou metadado do produto foi obtido. A causa exata da recusa não foi comprovada. Implementação da retomada registrada no commit `a3ff17e`.

## Escopo e evidência anterior

O alvo desta execução é o item público `MLB3810190843` e seu link de afiliado fornecido para associação manual, `https://meli.la/1JQSATy`. O link não será gerado automaticamente. A persistência PostgreSQL, categorização, histórico e fila dry-run permanecem no escopo; uma entrada `READY` depende das verificações de elegibilidade existentes, não apenas da presença de um token.

Shopee, mineração automática e publicações reais permanecem fora desta execução. `Ofertas de Casa` deve continuar desativado. Não há autorização para iniciar a Fase 4.

A autorização OAuth real anterior, em **18/10/2026 às 17:28:59 America/Sao_Paulo (20:28:59 UTC)**, está registrada em [OAUTH_MERCADOLIVRE.md](OAUTH_MERCADOLIVRE.md). A execução anterior aprovou 699 testes, quatro deles em PostgreSQL real, além das verificações de proteção de processos e lint registradas no [roadmap](../ROADMAP.md). Esses números não são apresentados como resultado da Fase 3.2. O checkpoint de pausa foi preservado; gateway/túnel foram encerrados naquela ocasião, mantendo tokens protegidos e dados.

## Pesquisa primária consultada

Fontes oficiais consultadas em **18/10/2026**, sem credenciais nem chamadas comerciais. A ferramenta de pesquisa obteve o conteúdo indexado das páginas oficiais; a abertura direta das páginas de autenticação, preços e identificadores retornou HTTP 403. Esse bloqueio de documentação não é um resultado de consulta comercial à API.

| Recurso | Contrato documentado e limite | Fonte oficial |
| --- | --- | --- |
| Renovação OAuth | POST form em `/oauth/token`, com `grant_type=refresh_token`, `client_id`, `client_secret` e `refresh_token`. Somente o último refresh token é válido, de uso único e associado ao Client ID; renovar quando o access token perder validade. | [Autenticação e autorização](https://developers.mercadolivre.com.br/pt_br/mensagens-post-venda/autenticacao-e-autorizacao) |
| Identidade | GET `/users/me` usa Bearer no cabeçalho. Confirma a identidade autorizada; seu sucesso não comprova acesso a preços de outro vendedor. | [Autenticação](https://developers.mercadolivre.com.br/autenticacao-e-autorizacao) |
| Metadados | GET `/items/{id}` identifica o anúncio e seus atributos. Preços legados dessa resposta não são a fonte de preço atual adotada pelo conector. | [Identificadores de produtos](https://developers.mercadolivre.com.br/pt_br/identificadores-de-produtos/identificadores-de-produtos) e [gate anterior](INTEGRACOES_FASE_3.md) |
| Preço de venda | GET `/items/{id}/sale_price?context=channel_marketplace` informa valor, moeda, preço regular e identificador de preço. O contexto pode representar canal ou nível do comprador; não assumir condição pública sem evidência. | [Preços de produtos](https://developers.mercadolivre.com.br/devcenter/api-de-precos) |
| Condições de preço | GET `/items/{id}/prices` distingue preços e suas restrições/contexto/vigência. Informações confidenciais podem ser ocultadas quando o token não pertence ao dono do item. Ausência dessas informações não comprova ausência de restrições. | [Consulta de preços](https://developers.mercadolivre.com.br/pt_br/servicos-gerenciamento-de-contatos/api-de-precos) |

A documentação OAuth enumera `read`, `write` e `offline_access` entre os escopos aceitos. A aplicação deve operar com as permissões efetivamente concedidas; o consentimento anterior não amplia direitos sobre recursos de terceiros. [Referência oficial](https://developers.mercadolivre.com.br/autenticacao-e-autorizacao).

Como consequência do refresh de uso único, a implementação precisa serializar a renovação por credencial e persistir o par novo de forma protegida antes de permitir outro uso. Essa é uma exigência de consistência do projeto derivada do contrato, não uma promessa de transação atômica entre a API remota e o armazenamento local. Timeout ou falha ambígua não justificam repetir indiscriminadamente o mesmo refresh token.

## Comando único para consulta pontual

O runner usa a configuração local existente e exige PostgreSQL. Depois de comprovar as permissões funcionais/condições do programa aplicáveis no DevCenter, ou obter orientação do suporte oficial sobre o HTTP 403, o comando de retomada é:

```powershell
.\.venv\Scripts\python.exe scripts/validate_mercadolivre.py --item MLB3810190843 --affiliate-url 'https://meli.la/1JQSATy'
```

Mercado Livre é habilitado apenas em uma cópia de `Settings` em memória para essa execução explícita. As flags comerciais do `.env` não são ativadas e `PRICE_PROVIDER=simulated` é preservado. O runner persiste o scheduler em pausa e não o retoma automaticamente; não inicia mineração ou envio. A saída é um relatório sanitizado. As primeiras consultas reutilizaram o token válido anterior; a última usou o token obtido pelo novo consentimento descrito abaixo. Não se força refresh real para demonstrar o recurso.

Quando houver um snapshot válido, o runner aplica os metadados, usa `record_price`, categorização e `OfferEngine`, seguindo a fila dry-run e a janela de deduplicação de três horas já existentes. Um erro antes de obter o snapshot registra o diagnóstico sem criar uma observação comercial fictícia. O link informado é configuração do responsável; salvá-lo após uma recusa da API não comprova verificação comercial pela plataforma.

## Implementação e testes controlados

| Verificação | Resultado desta retomada |
| --- | --- |
| Uso do OAuth protegido pelo conector, sem impressão/cópia dos tokens | Gerenciador e integração implementados; consulta real chegou ao upstream e recebeu HTTP 403 no item |
| Token válido reutilizado e refresh somente quando necessário | Token válido reutilizado na execução real; refresh real não exercitado |
| Exclusão mútua entre processos/sessões e rotação persistida | Aprovado em cenários controlados com oito threads e três processos, sem refresh comercial real |
| Falhas, expiração, recusa de autorização e respostas sanitizadas | Aprovado na suíte controlada, incluindo falha de processo; mocks/dados temporários |
| Runner e persistência conservadora sob falha | Seis testes unitários aprovados |
| Suíte automatizada e regressões anteriores | **755 aprovados, sem falhas/skips, em 136,34 segundos**, com um `StarletteDeprecationWarning` |
| Integração PostgreSQL em schemas exclusivos de `ofertas_bot_test` | Seis casos reais aprovados: quatro anteriores e dois novos do runner; payloads comerciais sintéticos |
| Ruff e formatação | Aprovados em **94 arquivos Python** |
| Alembic no PostgreSQL real | `current` na revisão `0004_marketplace_provenance (head)`; `check` sem novas operações |
| Parser final dos exemplos PowerShell | 31 blocos aprovados em README, guia OAuth e neste relatório |

Fixtures de tokens, itens e preços usadas em testes devem ser sintéticas e identificadas como tal. HTTP mockado não comprova acesso comercial; PostgreSQL real com payload sintético comprova persistência e regras de banco, não preço de mercado. Não registrar valores de credenciais, corpos OAuth, cookies, tickets ou conteúdo do armazenamento privado.

## Consultas reais e persistência

| Etapa autorizada | Resultado/horário sanitizado |
| --- | --- |
| GET `/users/me` | HTTP 200 em 18/10/2026 21:27:01 America/Sao_Paulo, identidade compatível e site MLB, sem expor dados pessoais |
| GET `/items/MLB3810190843` | HTTP 403 na sondagem e nos testes do runner, inclusive com o novo token às 21:41:28.303381; `FAILED / AUTHORIZATION_DENIED`, sem causa específica comprovada |
| GET `/items/MLB3810190843/sale_price?context=channel_marketplace` | Não chamado: consulta interrompida após recusa em `/items` |
| GET `/items/MLB3810190843/prices` | Não chamado: consulta interrompida após recusa em `/items` |
| Novo consentimento Authorization Code | Concluído aproximadamente às 21:40–21:41 America/Sao_Paulo, com atualização dos tokens DPAPI e consumo dos registros de `state`/PKCE |
| Grant de refresh real | Não exercitado: os tokens usados estavam válidos; o novo Authorization Code não constitui uma execução de refresh |
| Conexão PostgreSQL | Conectado, revisão `0004_marketplace_provenance` |
| Configuração manual no PostgreSQL | `AppSetting` de chave `manual-link:mercadolivre:MLB3810190843` conserva o link fornecido; configuração manual, sem confirmação da API |
| Diagnóstico PostgreSQL | Evento `manual_commercial_check`, estado `FAILED`, código `AUTHORIZATION_DENIED` persistido |
| Produto, categoria e histórico | Nenhum produto ou observação criado para o item sem snapshot; nenhum metadado, categoria ou preço inventado |
| Oferta/fila dry-run | Nenhuma oferta ou entrada criada a partir da consulta recusada; nenhum envio externo |

Preço anterior anunciado, histórico anterior do projeto e preço atual têm significados distintos. Uma primeira observação real não comprova queda histórica. HTTP 401/403, anúncio inexistente, preço exclusivo ou condições insuficientes devem preservar o diagnóstico e impedir uma oferta pública presumida. A fila simulada pode legitimamente permanecer vazia.

A sondagem ocorreu em **18/10/2026 às 21:27:01 America/Sao_Paulo (19/10/2026 00:27:01 UTC)**. A inspeção dos escopos não identificou uma permissão funcional específica de leitura de itens; havia escopos genéricos e de outros recursos. Isso é uma limitação observada e não estabelece, isoladamente, a causa do HTTP 403. Não foi identificado um código upstream que permita atribuir motivo mais preciso. O sucesso de `/users/me` demonstra autenticação, sem demonstrar autorização para consultar esse anúncio.

A execução do conector pelo runner ocorreu em **18/10/2026 às 21:32:48.703151 America/Sao_Paulo (19/10/2026 00:32:48 UTC)**. O resultado sanitizado foi `FAILED`, `AUTHORIZATION_DENIED`, HTTP 403. O evento e a configuração manual foram persistidos no PostgreSQL real, sem transformar a associação manual do link em verificação da API. As rotas de preços não foram chamadas após essa recusa.

Após o proprietário revisar permissões, houve novo teste em **18/10/2026 às 21:35:43 America/Sao_Paulo**. O item continuou retornando HTTP 403, com token válido e sem preços obtidos. Em seguida, um novo Quick Tunnel foi preparado, alterando somente `MERCADOLIVRE_REDIRECT_URI` no `.env`, sem fixar o hostname neste relatório.

O proprietário cadastrou a nova URI/permissões e autorizou novo consentimento, concluído aproximadamente em **18/10/2026 às 21:40–21:41 America/Sao_Paulo**. A versão do armazenamento mudou, os tokens DPAPI foram atualizados e os registros de `state`/PKCE foram consumidos pelo gateway. Isso comprova uma nova troca Authorization Code, **não uma chamada com `grant_type=refresh_token`**.

Com o novo token, a consulta foi repetida em **18/10/2026 às 21:41:28.303381 America/Sao_Paulo (19/10/2026 00:41:28 UTC)**. O resultado permaneceu `FAILED / AUTHORIZATION_DENIED / HTTP 403` em `/items/MLB3810190843`. `/sale_price` e `/prices` não foram chamados; não houve fallback para preços legados ou scraping. A causa específica continua não comprovada.

Para esse consentimento, a instância preexistente da API em `127.0.0.1:8000` não oferecia as rotas OAuth, retornando HTTP 404. Ela foi preservada. A autorização administrativa usou uma instância atualizada **temporária em `127.0.0.1:8001`**, com scheduler desativado, encerrada após o uso. Isso é uma evidência da execução local, não uma mudança da porta padrão da aplicação.

As verificações finais, sem expor valores privados, confirmaram:

- `/users/me` HTTP 200 com o novo token e identidade compatível com o armazenamento.
- Access token e refresh token ausentes em texto puro nos bytes do SQLite; zero registros de tentativas OAuth e estado de refresh pronto, sem renovação em andamento.
- Último evento PostgreSQL `FAILED / AUTHORIZATION_DENIED` em **19/10/2026 00:41:28.882834 UTC**, zero produtos e históricos do item, configuração manual presente e scheduler desativado.

O artefato local `.runtime/phase-3-2-result.json` registra somente booleanos, estados e contagens sanitizados e permanece ignorado pelo Git. Nenhum identificador privado é reproduzido neste relatório.

## Encerramento, lacunas e próximos passos

O scheduler permanece persistido em pausa para manter o garimpo desativado. Após o consentimento, `MERCADOLIVRE_OAUTH_ENABLED` foi restaurada para `false`; as flags comerciais Mercado Livre/Shopee continuam `false` e `PRICE_PROVIDER=simulated` foi preservado. A API temporária `8001` foi encerrada e a instância anterior `8000` preservada. O novo gateway em `8787` e o Quick Tunnel permanecem ativos; a URI real fica somente no `.env` e no manifesto `.runtime/oauth-tunnel.json`. Não se presume ativação comercial permanente a partir dessa preparação. Nenhum hostname temporário ou identificador privado é fixado neste relatório.

A instância usual em `8000` carregará a implementação atual no próximo reinício normal da API. A verificação desta entrega não encerrou nem substituiu essa instância legada.

O próximo passo é comprovar a permissão funcional e as condições do programa Mercado Livre para aplicação/conta, ou esclarecer a recusa com o suporte oficial usando somente diagnóstico sanitizado. O novo consentimento já foi realizado e não resolveu o HTTP 403; não há evidência que justifique repeti-lo sem uma mudança de acesso identificada.

Refresh/rotação comercial real não foram exercitados porque os tokens usados estavam válidos; sua implementação foi verificada com mocks e dados temporários. Categorização/histórico/fila com resposta real de produto não puderam ser demonstrados devido ao HTTP 403. Shopee segue desativada, sem credenciais; acesso amplo, cupons de terceiros, mineração automática e publicação não são demonstrados por esse ensaio. A Fase 4 continua dependente de novo aceite explícito.
