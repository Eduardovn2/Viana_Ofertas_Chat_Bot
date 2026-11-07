# Cotação histórica e próximo monitoramento — 30/10/2026

## Integridade e ingestão preparada

A resposta protegida de ee52b076-4084-4dc3-abd3-4a9e994db463 foi verificada por
leitura do PostgreSQL: autorização consumida SUCCEEDED, reserva REAL de uma
tentativa, aprovação do mesmo escopo, quatro eventos de auditoria na ordem correta,
identidade do produto 2 e horário entre claim e conclusão. O SHA256 do objeto quote
confere com AppSetting e ShopeeCollectionRun. O hash do arquivo result.json inteiro
foi registrado separadamente: a auditoria original atesta o objeto quote, não todos
os campos do envelope local.

[Preparação e hashes](PREPARACAO_INGESTAO_COTACAO_SHOPEE_20261030.json).

`app/services/shopee_quote_ingestion.py` reutiliza PriceSnapshot, PriceContext,
record_price e ProductPriceHistory. A preparação é somente leitura. O método de
ingestão aceita exclusivamente modo offline e banco isolado; não há endpoint nem
liberação operacional nesta entrega. O chamador é responsável pela transação.

Observação preparada: OFFICIAL_API, BRL Decimal 1099.00, coletada originalmente em
2026-10-30T09:29:44.200225+00:00. availability e price_access UNKNOWN,
commercial_verified=false, observation_only=true, preço anterior ausente. Percentual
anunciado 31 guardado separadamente; períodos permanecem Int64 sem conversão.
Sem variante inventada, cupom, estoque, preço público/final ou queda histórica.

price_basis identifica contexto oficial do anúncio, operação, escopo de variante e
condições desconhecidas, sem preço/horário/comissão/percentual em sua identidade.
MANUAL não fornece baseline para OFFICIAL_API. O banco operacional contém somente
os históricos 2/3 desse produto, ambos preservados: a primeira observação oficial
será INITIAL informativo. OfferEngine continua excluindo o modo observacional.

Idempotência: origem estável shopee:official_api, horário original, fingerprint da
identidade da leitura + hash do resultado, referência productOfferV2:shop:item e
metadados price_id/source_name que apontam para autorização e digest. Reexecução
retorna a mesma linha. Lock do produto e constraint existente protegem concorrência;
não é criada outra arquitetura ou tabela. Nenhuma autorização histórica é alterada.

## Proposta mínima de monitoramento — não aplicada

Começar com **um anúncio**, o Cockpit; ampliar até três somente após seleção e
autorização específicas. Proposta inicial editável em DiscoveryOperations/AppSetting:

| Configuração proposta | Valor inicial, sujeito à confirmação oficial |
|---|---|
| monitor_interval_seconds | 21600 (6 horas) |
| quota_period_seconds | 86400 (24 horas) |
| max_queries_per_period | 12, global compartilhado |
| Consultas por anúncio/execução | 1 POST, page 1, limit 1 |
| Anúncios selecionados por ciclo | 1–3 identidades explicitamente autorizadas |
| Execuções simultâneas | 1, com leases/fencing existentes |
| Aprovação comercial/publicação | Sempre bloqueadas neste modo |

Com três anúncios, quatro ciclos completos/dia consomem o teto proposto de 12.
Esses números são limites locais conservadores, **não quotas oficiais comprovadas**;
se o limite oficial for menor, ele prevalece. Todos os contadores devem considerar
reservas consumidas e falhas. Frequência, orçamento, janelas e prioridades serão
persistidos dinamicamente somente depois de autorização; nenhum valor foi alterado.

O futuro job reutilizará o scheduler existente, mantendo descoberta ampla separada
de monitoramento por identidade. Exigirá autorização recorrente própria, limitada
por produtos, período, orçamento e finalidade. A autorização pontual já consumida
nunca será reciclada. Cada leitura terá UUID/claim únicos; reservar antes do HTTP,
deduplicar por ciclo/produto/contexto e registrar o horário real de cada coleta.
Comparar somente OFFICIAL_API com fonte/contexto compatíveis, informativamente.

- **403/erro de permissão:** interromper o monitoramento da conta e exigir revisão
  administrativa; não trocar conta, endpoint ou renovar autorização automaticamente.
- **429:** encerrar a tentativa; respeitar Retry-After quando disponível e backoff
  configurável. Eventual ciclo futuro exige orçamento e autorização recorrente
  vigentes. Não repetir o UUID/tentativa consumidos.
- **Timeout, interrupção, claim abandonado ou UNCERTAIN:** suspender o anúncio para
  revisão, conservar a reserva e não reenviar automaticamente a mesma leitura.
- **Payload vazio/divergente/inválido:** preservar a última observação, registrar
  diagnóstico e não substituir o anúncio nem fabricar preço/disponibilidade.

O adaptador atual continua pontual e manual. A extensão para autorização recorrente
e jobs reais ainda precisa de desenvolvimento e aprovação; não basta ativar flags.
Monitoramento e Telegram permanecem desativados. Não executar chamada de teste para
descobrir quotas: obter documentação/suporte oficial e autorização antes de novo I/O.

## Validação desta etapa

85 testes offline e 17 PostgreSQL reais aprovados, zero falhas/ignorados, em schemas
UUID de ofertas_bot_test. Incluem ingestão idempotente/concorrente, rejeição de
adulteração de preço/horário/identidade/modo, separação MANUAL/OFFICIAL_API e bloqueio
comercial/operacional. As fixtures são sintéticas e não carregam a cotação privada.
A suíte completa anterior não foi repetida. Nenhuma chamada externa foi permitida.

Ruff, formatação e preservação estão registrados no arquivo de validação desta
entrega. Não há migração nova ou alteração de esquema necessária. Banco operacional
acessado somente em READ ONLY; sem nova observação, oferta, link, fila, Telegram,
credencial/flag alterada, scheduler iniciado, commit ou push.

## Autorização posterior necessária

Texto sugerido, **ainda não concedido**:

> Autorizo gravar no produto 2 uma única observação OFFICIAL_API baseada na resposta
> autenticada ee52b076-4084-4dc3-abd3-4a9e994db463, hash quote
> a567eb6f9d8acf963ee73e54874fbff1b9abb5be747f8f77173a0deece2ab829,
> BRL 1099.00, coletada em 2026-10-30T09:29:44.200225+00:00, sem atualizar esse horário.
> Manter UNKNOWN/UNKNOWN, commercial_verified=false e percentual anunciado 31
> separado, sem comparação com MANUAL, preservando os históricos 2/3. Permito somente
> a pequena liberação técnica necessária, transação idempotente e auditoria desta
> gravação; zero consultas externas, links, ofertas, scheduler ou publicações.

A autorização acima não permitirá coleta recorrente. Esta precisará de outro escopo
explícito e confirmação das permissões/quotas oficiais. Variante, disponibilidade,
preço aplicável e condições comerciais seguem sem fonte suficiente para oferta.

## Execução posterior expressamente autorizada — 30/10/2026

A autorização acima foi posteriormente concedida pelo responsável para a cotação
exata. Após a interrupção, a consulta local confirmou que ainda não havia observação
oficial. A transação foi então concluída: histórico ID 4, auditoria ID 14, produto 2,
BRL 1099.00, horário original 2026-10-30T09:29:44.200225+00:00, comparação INITIAL.
O hash foi validado contra a leitura consumida e a auditoria original. A chamada
repetida dentro da transação reutilizou o mesmo ID. Todos os registros anteriores,
incluindo os históricos 2/3, permaneceram idênticos; somente uma observação e um
evento de auditoria foram acrescentados. Houve confirmação após o commit.

Disponibilidade e acesso ao preço permanecem UNKNOWN, commercial_verified=false,
observation_only=true. Percentual anunciado 31 e períodos brutos são informativos.
Nenhuma chamada externa, mutation, oferta ou publicação foi realizada. Flags false,
grupo pausado e configurações preservadas. O fluxo genérico continua bloqueado.

Validação nesta retomada: 85 regressões offline aprovadas; Ruff check e format check
aprovados. Os 17 testes PostgreSQL anteriores não foram repetidos. Relatório local
protegido: `.runtime/authorized-identity-read-ee52b076-4084-4dc3-abd3-4a9e994db463/INGESTAO_AUTORIZADA.json`.
