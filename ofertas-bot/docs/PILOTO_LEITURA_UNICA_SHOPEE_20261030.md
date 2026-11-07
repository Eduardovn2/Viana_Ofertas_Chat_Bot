# Piloto Shopee de leitura única — preparação de 30/10/2026

**Implementação local, sem consulta real.** O adaptador está preparado para uma
leitura autorizada por identidade, sem registrar cotação em qualquer tabela
operacional. Nenhuma autorização real foi criada, aprovada ou consumida nesta etapa.

[Plano do Cockpit, payload e hash proposto](PLANO_LEITURA_UNICA_COCKPIT_SHOPEE_20261030.json).
O plano usa produto 2, shopId 1904573791 e itemId 23199869203. Seu UUID é apenas uma
referência proposta, ainda sem reserva no backend; não é uma autorização.

## Reutilização e limites conhecidos

`ShopeeSingleReadAdapter` compõe o `ShopeeCollectionService` existente. Reutiliza
`ShopeeConnector.fetch_product`/assinatura, validação de produto conhecido,
`DiscoveryOperations`, leases/fencing, AppSetting, IntegrationEvent e
ShopeeCollectionRun. Não há novo conector, pipeline de produtos, histórico ou
migração. O scheduler não recebe adaptador nem callback novos.

Contrato conferido contra o [extrato oficial local](CONTRATO_FONTES_SHOPEE_20261026.json),
proveniente do [Explorer BR](https://open-api.affiliate.shopee.com.br/explorer/v2),
cache de 21/10 revisado em 26/10. Não houve acesso à Shopee para reconfirmá-lo hoje.
`productOfferV2` documenta shopId/itemId, page e limit. O piloto fixa uma página e
um nó: `page:1,limit:1`, com os campos já normalizados pelo conector. O cache mostra
default de paginação 20, sem máximo oficial dessa operação. Nenhum desses números
comprova quota por segundo/dia, acesso atual da conta ou autorização para recorrência.

## Autorização e claim

1. **PLAN_ONLY:** leitura local do produto e cálculo do payload/hash, sem gravar
   reserva. Os IDs recebidos precisam corresponder ao produto e à URL já cadastrados.
2. **PREPARED:** após autorização posterior do responsável para o plano, registrar
   o escopo imutável em AppSetting, sob UUID novo. Nenhum HTTP nessa preparação.
3. **APPROVED:** aprovação administrativa explícita do hash, ator e validade futura.
   `confirmed_single_read` exige booleano true; a validade não pode superar o
   `quota_period_seconds` administrativo vigente. Essa janela não é uma quota oficial.
4. **CLAIMED:** lease compartilhado, revalidação de escopo/validade/orçamento, CAS
   atômico APPROVED→CLAIMED e reserva ShopeeCollectionRun com requests=1. Claim,
   auditoria e consumo da autorização são commitados **antes** do HTTP.
5. **SUCCEEDED / FAILED / UNCERTAIN:** persistir somente resultado técnico da
   tentativa. O mesmo UUID não autoriza outro POST, inclusive após erro conhecido.

O escopo vincula produto, shopId/itemId, variante vazia, operação, endpoint,
hash do corpo HTTP exato, hash da URL cadastrada, conta via hash interno da
credencial, modo, página/limite, máximo de uma chamada e ausência de persistência.
Mudança de URL, identidade, conta, payload ou modo invalida a aprovação anterior.
Uma autorização não admite outro request_id no despacho. UUID de coleta histórica
não pode ser reutilizado; o namespace não consome autorizações de mutations.

O guard imediatamente anterior ao HTTP verifica novamente claim/owner, reserva,
scope/hash, validade, flags e lease. Confere método POST, endpoint e hash dos bytes,
além do conteúdo da única query. Retries, cache e espera automática por retry ficam
desativados; redirects/fallbacks/paginação extra não são permitidos.

Flags globais não concedem autorização: precisam continuar false. Apenas a instância
efêmera do conector recebe habilitação para o despacho aprovado, sendo fechada ao
terminar. O transporte real não pode ser injetado pela API. Credenciais são lidas
da configuração local e não entram em respostas, auditoria ou logs.

## Resultado e ausência de persistência comercial

Somente a resposta da primeira execução bem-sucedida contém `quote`: identidade,
nome sanitizado, moeda, preço utilizável/faixa em strings Decimal, percentual
anunciado, períodos Int64 sem conversão e horário da coleta. Disponibilidade e
acesso continuam UNKNOWN e commercial_verified=false. Nenhum desconto histórico,
preço anterior, cupom, estoque ou validade comercial é inferido.

AppSetting e ShopeeCollectionRun guardam **somente status técnico, contagem HTTP,
código HTTP e hash do resultado**, sem preços, percentuais, períodos ou nome
observado. Não há gravação em ProductPriceHistory, produto, categorias, links,
ofertas, aprovações comerciais ou outbox. A aprovação desta leitura não é aprovação
de promoção ou publicação.

Replay/GET devolvem o estado e a auditoria; não recuperam a cotação efêmera e não
consultam a API novamente. Se o cliente perder a primeira resposta, não repetir o
despacho: consultar o estado. Obter outra cotação requer autorização nova.

Timeout, interrupção e falha de persistência após claim ficam UNCERTAIN, sem nova
tentativa. Se uma queda impedir até a gravação final, o claim durável permanece
consumido e GET o apresenta como UNCERTAIN. HTTP 5xx também fica conservadoramente
incerto; erros de autorização/GraphQL/payload encerram a tentativa. Nenhum desses
resultados provoca reenvio automático.

## Separação do laboratório

Testes exigem MockTransport e schema UUID isolado em ofertas_bot_test, ou SQLite
dedicado de testes. Autorizações MOCK_ONLY não podem ser convertidas em reais.
A operação real recusa app_env=test, bancos com sufixo _test e schemas não public.
A API não aceita `offline`, transporte, endpoint, flags ou retry como parâmetros.
Um harness unitário exercita o ramo de criação do registry real com MockTransport;
isso não registra autorizações no banco operacional nem contata a Shopee.

A rota anterior `/observations` permanece bloqueada para execução real. A nova
família `/identity-reads` é pontual, sem ingestão, e requer seu próprio registro e
aprovação; não libera descoberta, monitoramento ou publicação automática.

## Procedimento posterior exato — não executado

Primeiro obter confirmação explícita do responsável para **uma leitura deste
produto/payload**, permitindo somente reserva/auditoria e retorno efêmero, sem
histórico ou publicação. Confirmar também que o plano ainda corresponde ao código,
URL/conta e condições locais; hash divergente exige nova revisão, não ajuste silencioso.

Utilizar exclusivamente o backend administrativo local autenticado com
`X-API-Key` obtida localmente, sem imprimi-la. Base:
`http://127.0.0.1:8000/api/v1/shopee-collection`.

| Passo | Chamada local | Corpo / verificação |
|---|---|---|
| 1 | POST `/identity-reads/plan` | Objeto `request` do arquivo do plano; preencher `actor` com identificação real. Apenas lê o produto. Comparar o scope_sha256 e payload com o plano autorizado. |
| 2 | POST `/identity-reads` | Mesmo objeto request, UUID novo não consumido. Deve retornar PREPARED, consumed=false, requests_reserved=0. |
| 3 | POST `/identity-reads/{authorization_id}/approve` | JSON abaixo, com hash exato, responsável e expires_at UTC futuro dentro da janela administrativa. Deve retornar APPROVED, sem HTTP Shopee. |
| 4 | POST `/identity-reads/{authorization_id}/execute` | Mesmo hash/ator e confirmed_single_read=true, sem expires_at. Fazer essa chamada **uma única vez**, após autorização explícita. |
| 5 | GET `/identity-reads/{authorization_id}` | Consultar auditoria/estado final. Nunca repetir o passo 4 após falha, timeout ou resultado incerto. |

Corpo de aprovação (placeholders devem ser preenchidos, não são valores válidos):

```json
{
  "scope_sha256": "<hash exato do plano autorizado>",
  "actor": "<identificacao-real-do-responsavel>",
  "confirmed_single_read": true,
  "expires_at": "<ISO8601 UTC futuro com fuso>"
}
```

O corpo de execute contém somente os três primeiros campos. Não usar chamadas
diretas à Shopee, scripts independentes de envio nem autorizações antigas. Não
ligar flags globais, alterar .env, iniciar jobs ou usar o caminho de geração de links.

Se o serviço não estiver executando a versão atual, iniciar/recarregar o backend
local em janela posteriormente autorizada, conferindo antes o scheduler persistido
desativado. Esta entrega não iniciou servidor nem scheduler operacional.

## Validação e estado final

**437 testes distintos aprovados nesta etapa:** 328 offline e **109 PostgreSQL
reais**, zero falhas/ignorados. Os 37 casos novos do adaptador foram revalidados
depois dos ajustes finais, incluindo cotação efêmera; 16 casos PostgreSQL novos.
A primeira execução direcionada teve 325 aprovações, com 34 casos sobrepostos aos
37 finais; não somar as contagens brutas.

Ruff check e format check passaram nos **178 arquivos Python**. Alembic heads em
`0009_shopee_discovery`; current/check passou nos schemas UUID PostgreSQL de testes,
sem drift e sem migração operacional. Resta somente um aviso de depreciação da
dependência Starlette nas execuções selecionadas, sem falha.

[Resultados sanitizados e preservação](VALIDACAO_LEITURA_UNICA_SHOPEE_20261030.json).
Os testes direcionados preservam as regressões do conector, coleta, monitoramento
observacional, comparabilidade e bloqueios. PostgreSQL é exercitado em schemas
próprios de ofertas_bot_test; Alembic current/check usa esses schemas, sem migração
operacional. A suíte completa anterior de 1.593 não é apresentada como nova
execução integral nesta etapa.

Acesso operacional apenas READ ONLY para plano e conferência antes/depois de
produto 2, históricos 2/3, configurações e destinos. Sem registro/aprovação/consumo
de autorização real, cotação operacional, chamadas Shopee, mutations, Telegram,
alteração de .env/credenciais/flags ou commit/push.

Hashes confirmaram produto 2, históricos 2/3, destinos, configurações e sete arquivos
protegidos iguais. Dados públicos/schemas preexistentes de ofertas_bot_test
preservados; os schemas dos testes foram removidos por suas próprias fixtures.
As seis flags de integrações/links/publicação/simulação continuam false, Viana
Ofertas enabled=false e scheduler persistido false. Os guards HTTP registraram
zero tentativas de transporte real em todas as execuções.

## Pendências

Permissões atuais da conta, quotas oficiais, unidade/semântica dos períodos e
condições comerciais continuam não confirmadas. Uma resposta anterior HTTP 200 não
garante acesso atual nem funcionamento recorrente. O piloto posterior pode produzir
403/erro e deverá encerrar sem outra tentativa. Recorrência, persistência comercial,
vínculo de afiliado e publicação continuam exigindo autorização e evidências próprias.

Arquivos: `app/services/shopee_single_read.py`, adaptação somente de lock opcional
em `app/services/shopee_collection.py`, rotas em `app/api/shopee_collection.py`,
`tests/test_shopee_single_read.py`, `tests/test_postgres_shopee_single_read.py`,
README/ROADMAP e estes documentos/plano/validação. Não há dependências novas,
alterações de modelos ou migrações. Fases 5/6 não iniciadas.
