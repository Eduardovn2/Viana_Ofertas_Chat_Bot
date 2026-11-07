# Monitoramento observacional Shopee — implementação local, 26/10/2026

Consulta por identidade preparada com **execução exclusivamente offline**. O
serviço reutiliza `ShopeeConnector`, `ShopeeCollectionService`, reservas, auditoria,
`ProductPriceHistory`, `PriceComparisonService` e preferências `DiscoveryOperations`.
Não há novo pipeline, migração ou adaptador comercial no scheduler.

## Entrada e funcionamento

`ObservationRequest` valida `request_id`, `actor`, `product_id`, `shop_id` e
`item_id`. IDs Shopee positivos são normalizados sem coerção de booleanos/floats e
limitados a Int64. O produto local deve existir, pertencer à Shopee e corresponder
à identidade solicitada. Não se cria outro produto nem se fabrica variante.

`ShopeeCollectionService.observe(..., offline=True, connector=...)` exige banco
isolado e `ShopeeConnector` com `httpx.MockTransport` no endpoint oficial. O serviço
chama `fetch_product`, com `productOfferV2(shopId,itemId,page:1,limit:1)`. Query e
seleção devem corresponder ao escopo reservado antes do HTTP. Resposta vazia,
divergente, parcial ou com erro não substitui o anúncio nem acrescenta histórico.

A descoberta por palavra-chave continua no caminho existente. A consulta
observacional pontual não altera nome, categorias, link ou `monitoring_enabled`
do produto conhecido. Isso não habilita monitoramento contínuo de um produto pausado.

`POST /api/v1/shopee-collection/observations`, protegido pela autenticação
administrativa, prepara a interface futura e valida seu corpo. Atualmente responde
**409** para execução real; nenhuma flag, texto de autorização ou credencial libera
esse caminho. Campos extras, inclusive `confirmed_real_read`, são recusados.
Não é exposta uma rota para instalar mocks na API operacional.

Exemplo de escopo administrativo futuro, **não executado**:

```json
{
  "operation": "OBSERVE",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "actor": "responsavel",
  "product_id": 2,
  "shop_id": "1904573791",
  "item_id": "23199869203"
}
```

O UUID acima é apenas ilustrativo; não é reserva ou autorização existente.

## Campos e limites comerciais

| Campo da API | Persistência/uso |
|---|---|
| `price`, `priceMin`, `priceMax` | Decimal estrito em BRL; valores ausentes continuam ausentes. Faixa não resolvida não vira preço de uma variante escolhida. |
| `priceDiscountRate` | `advertised_discount_percent`: inteiro estrito 0–100 ou ausente/null. Não calcula preço anterior, cupom ou desconto histórico. |
| `periodStartTime`, `periodEndTime` | `offer_period_start_raw`/`offer_period_end_raw`: Int64, aceitando representação decimal textual canônica sem perda. Sem conversão para datas. Unidade e semântica ficam UNCONFIRMED; zero/negativos não são interpretados como datas ou sentinelas conhecidas. |
| `commissionRate`, `commission` | Permanecem separados do preço/desconto do consumidor. |
| Variante, pagamento, cupom, elegibilidade, região | UNKNOWN quando não documentados. Extras não contratados de uma resposta não comprovam essas condições. |

Compatibilidade com respostas/fixtures anteriores: os novos campos ausentes não
geram valores fictícios. Valores presentes malformados rejeitam a observação sem
substituir histórico anterior.

Cada observação deste modo contém `observation_only=true`,
`commercial_verified=false`, `availability=UNKNOWN`, `price_access=UNKNOWN` e
`previous_price=null`. O guard de persistência recusa promoção desses fatos.
`PriceComparisonService.compare` exclui observações desse modo mesmo diante de
alteração indevida dos enums; OfferEngine continua inelegível. O serviço não chama
geração de links, aprovação, criação de ofertas ou outbox/publisher.

Os testes usam respostas sintéticas e credenciais fictícias em banco isolado.
`source_type=OFFICIAL_API` descreve o contrato reproduzido, **não uma consulta real
efetuada**: a execução durável é OFFLINE. Não transportar esses registros para a
base operacional como evidência real.

## Contexto, histórico e diagnóstico

`PriceContext` é um contrato estrito e imutável de origem, operação, identidade do
produto/variante, tipo da cotação e condições conhecidas de pagamento, cupom,
elegibilidade e região. Não aceita preço, horário, comissão ou percentual como
campos de contexto. `price_basis=v1:<sha256>` é estável quando só esses valores
comerciais/temporais mudam.

`_safe_evidence` preserva o contexto estruturado, valida o hash correspondente e
mantém apenas os novos metadados tipados permitidos. O comparador exige igualdade
da base e do contexto, além das separações já existentes de produto, origem,
source_type, moeda e simulação. Contexto novo começa em INITIAL; observações MANUAL
não formam a baseline de OFFICIAL_API. Históricos legados não são reescritos.

`reported_change` apresenta INITIAL, UNCHANGED, RISE, DROP ou UNAVAILABLE conforme
os dados, exclusivamente como variação informativa de cotação. `verified_change`
permanece EXCLUDED neste modo. Nada disso comprova promoção, preço Pix, cupom,
estoque ou elegibilidade comercial.

## Reserva, concorrência e falhas

- UUID e conteúdo tipado são persistidos antes do I/O, com eventos
  `observation_reserved` e `observation_result` em `IntegrationEvent`.
- Uma reserva admite no máximo uma query; corpo diferente, mutation ou outro
  anúncio são recusados antes do transporte. Não há retry, fallback ou redirects.
- Reutilizam-se lease global da coleta e fencing antes de persistir. Duração do
  lease e orçamento global por período vêm de `DiscoveryOperations`; mudar essas
  preferências não concede autorização real. Não se criou agendamento.
- Replay do UUID devolve o resultado já reservado/concluído sem HTTP. Reprocessar
  a mesma cotação no mesmo instante reutiliza o histórico; uma nova cotação em
  outro instante pode registrar UNCHANGED. O fingerprint inclui a marca do modo,
  impedindo adotar/modificar uma observação legada não observacional.
- Timeout, perda de lease, interrupção capturada e falha após início da persistência
  ficam UNCERTAIN. A transação de dados falhada é revertida; a tentativa permanece
  consumida e auditada, sem reenvio. Morte abrupta que impeça qualquer gravação pode
  deixar RESERVED; replay também não reexecuta essa reserva. Recuperação operacional
  e seu procedimento precisam ser revisados antes de autorizar I/O real.
- Revalidação é local, mantém os bloqueios e expira cotações antigas; não realiza
  nova consulta ou promoção comercial.

## Payload sanitizado para leitura real futura

[JSON preparado sem credenciais](PAYLOAD_OBSERVACIONAL_COCKPIT_SHOPEE.json), gerado
pelo mesmo construtor de query do conector. **Não executado e ainda bloqueado.**
Requer autorização nova para uma leitura específica; persistência, recorrência e
publicação exigem decisões próprias. As autorizações antigas não foram reutilizadas.

## Validação e preservação

**1.593 testes aprovados**, zero falhas/ignorados: **1.500 offline** e **93 PostgreSQL
reais**. São 78 regressões offline e 13 PostgreSQL novas, preservando as anteriores.
Os 287 testes direcionados iniciais também passaram e estão sobrepostos às suítes;
não somar essa contagem. Há 17 avisos preexistentes de dependências na suíte offline.

Ruff check e format check passaram; **175 arquivos Python** formatados. Alembic
heads em `0009_shopee_discovery`; current/check passaram em schema UUID PostgreSQL
de testes, sem drift. Nenhuma migração operacional ou alteração de modelo/tabela
foi necessária.

[Resultados e evidências de preservação](VALIDACAO_MONITORAMENTO_OBSERVACIONAL_SHOPEE.json).
O banco principal foi acessado somente em transação READ ONLY para conferir
produto 2, históricos 2/3, configurações e destinos. Todos os testes PostgreSQL
utilizam exclusivamente schemas UUID próprios em `ofertas_bot_test`.

As conferências antes/depois preservaram os registros operacionais examinados,
destinos/configurações e dados/schemas preexistentes do banco de testes. Produto 2
e históricos 2/3 intocados; `.env` e evidências protegidas com hashes iguais.
As seis flags de integração/publicação/simulação e geração de links permanecem
false; scheduler persistido false; Viana Ofertas enabled=false. Guard HTTP ativo
nas três execuções, com zero tentativas de transporte real. Zero consultas Shopee
reais, mutations, Telegram ou scheduler operacional. Sem commit/push.

## Arquivos desta etapa

- `app/integrations/contracts.py`: contrato estável de contexto.
- `app/integrations/shopee.py`: seleção, validação informativa e construtor da query.
- `app/services/monitoring.py`: allowlist tipada, price_basis e guard observacional.
- `app/services/comparison.py`: contexto estruturado e exclusão comercial explícita.
- `app/services/shopee_collection.py`: escopo/execução observacional no serviço existente.
- `app/api/shopee_collection.py`: interface administrativa real bloqueada.
- `tests/test_shopee.py`: expectativas e vetor de assinatura sintético atualizados para a nova seleção, preservando as validações anteriores.
- `tests/test_shopee_observation.py`: novas regressões offline.
- `tests/test_postgres_shopee_observation.py`: integração PostgreSQL isolada e Alembic.
- `README.md`, `ROADMAP.md`, este relatório, `PAYLOAD_OBSERVACIONAL_COCKPIT_SHOPEE.json`
  e `VALIDACAO_MONITORAMENTO_OBSERVACIONAL_SHOPEE.json`: documentação e evidências.

Auxiliares e logs de validação em `.runtime/` são ignorados pelo Git; não fazem
parte da aplicação, não enviam mensagens e não executam consultas comerciais.
As alterações locais que já existiam foram preservadas; não há commit/push.

## Pendências para operação real

Confirmar quotas/permissões atuais por conta e semântica comercial dos campos;
autorizar um escopo novo de leitura e eventual persistência; revisar o adaptador
real observacional e recuperação antes de liberar a execução. Depois, decidir
separadamente sobre recorrência com limites oficiais e configurações dinâmicas.

Variante, preço efetivamente aplicável, disponibilidade e demais condições não
comprovadas continuam pendentes de fonte autorizada. Link/política de confiança e
ponte comercial/aprovação continuam requisitos separados de futura publicação.
Feeds não foram implementados nem consultados. Nenhum avanço às Fases 5/6.
