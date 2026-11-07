# Integracao offline de links ao pipeline Shopee

## Diagnostico anterior as alteracoes

A descoberta e o monitoramento estao em `ShopeeDiscoveryService`, que reutiliza
`ShopeeCollectionService`, `PriceComparisonService`, `OfferEngine`, as politicas
versionadas de `DiscoveryOperations`, classificacao e filtros existentes.
A etapa EVALUATE ja revalida preco publico/estoque, queda comparavel, filtros,
limiares e limites por destino. `has_own_affiliate_link(require_verified=True)`
bloqueia candidatas sem vinculo previamente aprovado. A preparacao usa
`SimulatedQueueService` e `render_message`, sem aprovacao ou dispatch real.
A outbox comercial e a outbox tecnica permanecem separadas.

O conector oferece `generate_affiliate_link`, mas o pipeline de descoberta nao o
acionava. `product_identity_verified` e `account_verified` sao separados: reconhecer
a identidade ou receber HTTP 200 nao comprova atribuicao a conta.

## Arquitetura implementada

Injetar um servico de links apenas no laboratorio offline. Antes de qualquer
mutacao mockada, executar os filtros existentes e revalidar os fatos comerciais.
Reservar duravelmente uma chave por produto, variante, URL de origem, destino e
operacao. Usar o conector oficial com transporte MockTransport obrigatorio, sem
retry/redirect. Guardar links integrais em registro proprio; expor apenas metadados
sanitizados. Associacao automatica exige identidade e atribuicao comprovadas;
a resposta real atual do conector nao satisfaz essa ultima condicao.

Uma segunda passagem de revalidacao prepara a mensagem na fila simulada somente
se todas as verificacoes continuarem satisfeitas. Nenhum gerador e injetado no
backend operacional; preferencias nao concedem autorizacao para acesso real.

## Validacao

**Suite integral: 1.445 aprovados, zero falhas, zero ignorados**, em 521,40 segundos.
Inclui **72 PostgreSQL reais** (58 anteriores + 14 novos), sempre em schemas UUID
exclusivos de `ofertas_bot_test`, e 1.373 testes offline. Foram acrescentados
52 casos: 38 offline e 14 PostgreSQL. Os 17 avisos sao deprecacoes de dependencias
(Starlette/httpx e adaptador datetime SQLite), sem falhas.

- Baseline anterior a implementacao: 58 PostgreSQL aprovados.
- Validacao intermediaria: 163 direcionados aprovados; 31 casos novos offline e 14 novos PostgreSQL aprovados antes dos sete ultimos casos de preferencias. Contagens sobrepostas, nao somar a suite integral.
- Ruff check e format check aprovados em 162 arquivos Python.
- Alembic current/check aprovado em schema PostgreSQL isolado, revisao `0009_shopee_discovery`, sem drift. Regressao de migrations aprovada; sem migration nova ou upgrade no banco principal.
- Fingerprints de todas as 32 tabelas publicas operacionais (incluindo alembic_version) identicos antes/depois; `.env` identico. Nove categorias ativas, grupo Viana Ofertas `-5309308689` pausado, monitoramento principal false e zero reservas `shopee_link_mock:` no banco principal.
- Nenhuma chamada real Shopee, mutation de links ou publicacao Telegram. A autorizacao anterior nao foi reutilizada.

```ini
SHOPEE_ENABLED=false
MERCADO_LIVRE_ENABLED=false
TELEGRAM_PUBLISHER_ENABLED=false
TELEGRAM_REAL_SENDS_AUTHORIZED=false
SIMULATION_AUTOMATION_ENABLED=false
```

[Evidencia sanitizada](VALIDACAO_INTEGRACAO_LINKS_PIPELINE_SHOPEE.json). Nenhuma pendencia de testes desta entrega; atribuicao e ativacao real permanecem pendentes de autorizacao e comprovacao independentes.


## Arquivos desta entrega

- Novo `app/services/affiliate_pipeline.py`: contrato injetavel, adaptador mockado, reservas/auditoria e resultados sanitizados.
- `app/services/shopee_discovery.py`: injecao opcional, pre-avaliacao e revalidacao com os filtros existentes, orcamento por destino/global e preparacao simulada.
- `app/services/discovery_operations.py` e `app/discovery_operation_defaults.json`: novas preferencias estritas, versionadas e independentes de autorizacao real.
- `app/services/publication_queue.py`: extrair o cooldown existente em helper reutilizavel por candidata, preservando a mesma regra de tres horas/observacao nova.
- Novos `tests/test_affiliate_pipeline.py` e `tests/test_postgres_affiliate_pipeline.py`.
- README.md, ROADMAP.md e este relatorio; evidencia JSON final apos validacao.
- Apoio local de validacao em `.runtime/validate_affiliate_pipeline.py` e fingerprints/logs ignorados pelo Git, sem gravar credenciais.

Nenhum modelo, migration ou conector comercial foi alterado nesta entrega. As alteracoes preexistentes da branch foram preservadas. Nao houve commit/push.

## Evidencia dos cenarios

Dados exclusivamente ficticios: `[FICTICIO] Console PlayStation 5`, loja sintetica 7001,
item 1; precos hipoteticos 100.00 -> 90.00, em Decimal. Os fatos comerciais adicionais
sao injecoes sinteticas explicitas de teste, nao dados comprovados de productOfferV2.

| Cenario | Resultado exercitado |
| --- | --- |
| A: queda comparavel + link previamente aprovado | Previa com identificacao publicitaria, hash e fila SIMULATED/PENDING; zero geracoes e zero envios reais. |
| B: sem comparativo confiavel | BLOCKED; nenhuma geracao e nenhuma promocao comprovada. |
| C: identidade valida, conta nao comprovada | GENERATED, URLs completas na reserva; nenhum AffiliateLink associado ou fila criada. Relatorio sem query string. |
| D: timeout/HTTP 503 | UNCERTAIN, attempts=1, sem repeticao mesmo em nova avaliacao. 403/erro GraphQL ficam FAILED, tambem consumidos. |
| E: loja/item/variante divergente | Rejeicao; variante sem contrato comprovado nao aciona gerador. |
| F: cupom nao verificado | Nao anuncia cupom nem aplica desconto ficticio; preco da previa continua 90.00. |
| G: concorrencia | Uma reserva e uma mutation mockada; concorrente BUSY; nenhuma aprovacao/envio duplicado. |
| H: excepcional fora da janela | Identifica a oportunidade excepcional apenas pelo historico comparavel, mas BLOCKED por janela; nenhum bypass comercial. |

Uma injecao explicitamente ficticia de account_verified=true valida o ramo positivo:
associa somente no laboratorio isolado, reavalia e prepara PENDING; nao comprova a
atribuicao do link real gerado anteriormente. O conector real continua conservador.

Reservas abandonadas sao UNCERTAIN. Mudanca de destino durante o I/O impede associacao.
Mudancas de origem/destino geram escopos independentes; repeticao do mesmo escopo nao
repete a mutation. Cadastro manual nao verificado e preservado sem sobrescrita.

## Limites e proxima ativacao

- O unico adaptador fornecido exige MockTransport e banco isolado. Nao existe chave de configuracao capaz de torna-lo real.
- As preferencias de links nas rotas existentes podem ser editadas por administrador, mas nao instalam gerador nem concedem autorizacao.
- Antes de geracao real: novo escopo/autorizacao, contrato/permissoes/quotas oficiais e implementacao/revisao especifica do adaptador real com reserva e orcamento aprovados.
- Para associacao: identidade exata e comprovacao independente de atribuicao/conta. HTTP 200 e o caminho /universal-link/ nao bastam.
- Para publicacao comercial: disponibilidade/preco publico/variante/condicoes comprovados, revalidacao recente, link proprio aprovado, filtros/limites/deduplicacao, aprovacao explicita de conteudo/hash/destino e nova autorizacao de envio.
- A primeira autorizacao real permanece consumida. Nenhum teste desta entrega usa essa autorizacao ou seu ledger.
- Nao habilitar operacao continua, Telegram, Mercado Livre, Shopee ou Fases 5/6 nesta entrega.
