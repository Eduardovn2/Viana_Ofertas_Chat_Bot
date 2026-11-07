# Preparação do monitoramento recorrente — 30/10/2026

## Escopo e estado

Implementação local e testes com MockTransport, sem chamadas reais. Nenhuma
autorização recorrente foi criada no banco operacional. As autorizações pontuais
anteriores permanecem consumidas e não podem ser recicladas.

`ShopeeRecurringMonitor` compõe o `ShopeeSingleReadAdapter` sobre o
`ShopeeCollectionService`, sem outro conector, histórico ou scheduler. Reutiliza
DiscoveryOperations para quotas locais/leases, AppSetting para autorização e ciclos,
IntegrationEvent para auditoria, record_price/ProductPriceHistory para observações,
PriceContext e PriceComparisonService para comparação informativa. A normalização
do contexto foi extraída do serviço de ingestão histórica, preservando o price_basis
compatível com a observação oficial ID 4.

Não há migração, endpoint real ou registro em startup. O serviço rejeita execução
operacional incondicionalmente. O laboratório exige banco isolado e o adaptador
filho exige MockTransport. Alterar flags não constitui autorização.

## Autorização e ciclo

- PREPARED contém produto/identidade, conta por fingerprint da configuração,
  URL/payload por hash, validade UTC, orçamento total e intervalo mínimo autorizado.
- APPROVED exige responsável, hash exato, confirmação estrita e referência de
  confirmação de quotas. Nos testes essa referência é explicitamente sintética;
  não é comprovação oficial. Não se aceita aprovação expirada nem renovação implícita.
- Intervalo é preferência editável persistida, podendo diminuir somente até o piso
  autorizado. Alterar frequência não muda validade, conta, orçamento ou aprovação.
- Cada ciclo tem UUID e leitura filha determinística, reserva própria e consumo
  durável do orçamento antes do HTTP. Lock e compare-and-swap impedem ciclos
  simultâneos da mesma autorização; a leitura filha reutiliza o lease global.
- A leitura filha faz claim/auditoria antes de um POST, page 1/limit 1, sem retry,
  fallback, paginação adicional ou mutation. Quotas locais compartilhadas continuam
  aplicáveis, sem serem apresentadas como quotas oficiais.
- Apenas SUCCEEDED íntegro permite observação e conclusão do ciclo na mesma
  transação. Replay devolve o mesmo resultado sem outro HTTP ou histórico.
- 403, 429, resposta vazia/divergente e demais falhas pausam a autorização. Timeout,
  interrupção, falha de persistência ou claim abandonado são incertos e bloqueiam
  novos ciclos. Não há reembolso automático, reaprovação ou retomada automática.
  Auditoria indisponível deixa a reserva consumida; nunca reconstruir a requisição.

O scheduler existente recebe somente um job preparado e pausado, explicitamente
em um runtime de laboratório, max_instances=1/coalesce. O observador existente
recarrega o intervalo sem ativar o job. Nada foi instalado no runtime operacional.

## Histórico e bloqueios comerciais

Cada resposta usa o horário efetivo da coleta, Decimal, origem OFFICIAL_API e
identidade da leitura/digest. Price_basis não inclui preço, horário, comissão ou
percentual. As comparações informativas aceitam apenas fonte, tipo, moeda e contexto
compatíveis; MANUAL não fornece baseline. A primeira cotação é INITIAL.

Availability e price_access permanecem UNKNOWN; commercial_verified=false,
observation_only=true e previous_price NULL. Percentual anunciado e períodos brutos
são informativos, sem inferir preço antigo, estoque, variante, cupom ou promoção.
OfferEngine continua excluindo essas observações. Nenhum link, oferta, aprovação
comercial, outbox ou publicação é criado por este serviço.

## Proposta para o primeiro piloto — não aplicada

| Parâmetro | Proposta local |
|---|---|
| Produto | 2 — Cockpit Brave Bull, 1904573791:23199869203 |
| Operação | productOfferV2, page 1, limit 1 |
| Intervalo mínimo | 21600 segundos (6 horas), editável sem ampliar autorização |
| Janela piloto | 24 horas, início e fim UTC ainda a definir |
| Orçamento total | No máximo 4 POSTs, incluindo reservas e falhas |
| Concorrência | Um ciclo por autorização; lease compartilhado existente |
| Finalidade | Cotação observacional e histórico informativo |
| Publicação/links | Bloqueados |

Se o limite oficial confirmado for mais restritivo, ele prevalece e o plano deve
ser refeito antes da aprovação. A janela de 24h e o intervalo de 6h não são limites
oficiais. A validação local aceita no máximo sete dias/100 reservas por autorização;
esses são tetos defensivos do software, não permissões da Shopee.

## Pendências e autorização futura

Ainda é necessário obter fonte oficial para: permissão de consultas recorrentes
pela conta, quotas por período/conta/operação, regras de rate limit e Retry-After.
Uma leitura bem-sucedida não demonstra permissão de recorrência ilimitada.

Após essa comprovação, será necessária liberação técnica operacional separada,
mantendo os mesmos controles e validando banco/conta reais. Não basta mudar
`offline`, flags ou preferências: a implementação atual bloqueia isso expressamente.
Também será necessária aprovação de escopo novo, com UUID próprio, hash da conta,
payload e período preenchidos. A autorização de persistência histórica não serve.

Texto a preencher e confirmar pelo responsável, **ainda não concedido**:

> Com base na evidência oficial [REFERÊNCIA E QUOTAS], autorizo exclusivamente o
> monitoramento observacional do produto 2, shopId 1904573791/itemId 23199869203,
> pela conta local identificada por [FINGERPRINT], no escopo [UUID E HASH]. Validade
> [INÍCIO UTC] até [FIM UTC, 24h], no máximo quatro POSTs totais productOfferV2,
> page 1/limit 1, intervalo mínimo 21600 segundos e limites oficiais menores
> prevalecendo. Autorizo somente essas novas cotações OFFICIAL_API e sua auditoria,
> com reserva/claim por ciclo e comparação informativa; falhas/incerteza pausam a
> operação sem retry. Não autorizo mutations, links, ofertas ou Telegram. A ativação
> exige liberação operacional validada; não permite habilitar outros jobs/destinos.

Não iniciar enquanto houver placeholders, quota desconhecida ou liberação técnica
pendente. Não há qualquer autorização implícita nesta documentação.

## Validação

Testes direcionados offline e PostgreSQL usam apenas fixtures sintéticas e schemas
UUID de ofertas_bot_test. A fixture aplica Alembic head; o teste específico executa
current/check no schema isolado. Resultados efetivamente executados estão em
`VALIDACAO_MONITORAMENTO_RECORRENTE_SHOPEE_20261030.json`.

Resultado: 182 testes distintos aprovados (143 offline, 39 PostgreSQL), sem ignorados
ou falhas finais. Rodadas: 142 regressões offline, 38 PostgreSQL; após os ajustes
finais, os 21 testes offline e 22 PostgreSQL do monitor foram repetidos e passaram.
A suíte completa não foi repetida. Ruff e format check passaram (187 arquivos).
Alembic current/check passou no schema isolado, revisão 0009_shopee_discovery.
Persiste apenas o aviso preexistente de depreciação Starlette/httpx.

Todas as tabelas operacionais e o .env permaneceram idênticos por comparação de
hashes antes/depois. Não há autorização recorrente no banco operacional. As seis
flags verificadas continuam false, scheduler desativado e grupo -5309308689 pausado.
As quatro rodadas tiveram transporte HTTP real proibido, sem tentativas bloqueadas.
Zero chamadas externas, mutations, ofertas, outbox ou publicações.
