# Novo piloto de seis horas — somente PREPARED

UUID: `de36e12d-5581-41ea-9137-0a4d54556fb7`

scope_sha256: `0b29f0a310b0909211c596bf9e656540a8ec38cc9b7d63f788899e7d9416336c`

Estado persistido: PREPARED, sem aprovação, used=0, active_cycle=null.
Auditoria de preparação: ID 22. Nenhuma consulta externa foi executada.

## Escopo vinculado

Produto local 2; shopId 1904573791/itemId 23199869203; uma conta local vinculada
por fingerprint, URL e payload por hash. Exclusivamente productOfferV2, page=1,
limit=1. Até dois POSTs no total, intervalo mínimo 21600 segundos após conclusão
da primeira leitura. Sem retry, fallback ou paginação extra.

Janela exata: **2026-11-01T12:00:00Z até 2026-11-02T12:00:00Z**.
Em Brasília: **01/11/2026 às 09h até 02/11/2026 às 09h**.
Nenhuma data poderá ser deslocada automaticamente.

[Plano JSON e vínculos](PLANO_PILOTO_6H_SHOPEE_20261101.json).

## Autorizações anteriores

Consultadas somente por leitura e preservadas:

- `1cfbb654-f4ab-4146-b185-e9f48e702540`: estado persistido PREPARED, estado efetivo EXPIRED, used=0.
- `59c8346b-6767-48c3-8f70-5de398b624ca`: estado persistido APPROVED, estado efetivo EXPIRED, used=0.
- `0538bd35-9fd2-40bf-b25d-85713fa92bd5`: SUCCEEDED, consumed=True.

O plano 59c8346b-6767-48c3-8f70-5de398b624ca não foi renovado, alterado ou reutilizado.
As autorizações pontuais consumidas permanecem consumidas.

## Limitações e segurança

Nenhum bloqueio local da conta foi encontrado na configuração atual. Quotas oficiais
e permissão para recorrência permanecem não confirmadas. Os sucessos anteriores
não comprovam autorização irrestrita. A aprovação exige ciência explícita dessas
incertezas; o contrato local é vinculado por hash e não identificou proibição
periódica nos materiais examinados. Não houve verificação externa nesta preparação.

Reutiliza ShopeeRecurringMonitor, reservas/claims por ciclo, leases, auditoria e
PriceComparisonService. Futuras cotações só poderão ser informativas OFFICIAL_API,
com UNKNOWN para acesso/disponibilidade, commercial_verified=false e
observation_only=true. Histórico ID 4 permanece referência oficial quando houver
contexto comparável; documentos MANUAL 2/3 não fornecem baseline oficial.
403, 429, timeout, UNCERTAIN, identidade divergente ou outra falha pausam o piloto,
sem tentativa automática ou devolução da reserva.

Somente um AppSetting e a auditoria 22 foram inseridos. Todas as linhas anteriores
permaneceram idênticas por hash, incluindo históricos 2/3/4 e autorizações anteriores.
.env e credenciais preservados. Flags false, scheduler desativado e grupo pausado.
Sem oferta, link, mutation, outbox ou publicação. Nenhum código ou migração alterado;
validações locais de identidade/escopo/persistência, sem repetir suíte de testes.
Sem commit ou push.

## Aprovação futura — não executada

Após autorização específica do responsável deste UUID/hash/janela, usar a API
administrativa autenticada existente, com X-API-Key obtida localmente e não exposta:

1. GET `/api/v1/shopee-collection/recurring-pilots/de36e12d-5581-41ea-9137-0a4d54556fb7` para conferir
   PREPARED, used=0, conta, escopo e ausência de aprovação.
2. POST `/api/v1/shopee-collection/recurring-pilots/de36e12d-5581-41ea-9137-0a4d54556fb7/approve` com:

```json
{
  "scope_sha256": "0b29f0a310b0909211c596bf9e656540a8ec38cc9b7d63f788899e7d9416336c",
  "actor": "eduardo.viana.3",
  "confirmed_experimental_pilot": true,
  "acknowledge_unknown_quotas": true
}
```

3. Somente na janela vigente, despacho por
   POST `/api/v1/shopee-collection/recurring-pilots/de36e12d-5581-41ea-9137-0a4d54556fb7/cycles/{NOVO_UUID}/execute`
   com o mesmo payload. Reserva/claim precedem no máximo um POST externo por ciclo.
4. Apenas primeiro ciclo SUCCEEDED, orçamento disponível, ausência de bloqueios e
   next_due decorrido permitem um segundo UUID, no mínimo seis horas após conclusão
   do primeiro. Não instalar job ou habilitar scheduler global implicitamente.
5. Encerrar após duas reservas, falha ou expiração, sem outra autorização automática.

Esta preparação não constitui aprovação nem autorização para executar consultas.
