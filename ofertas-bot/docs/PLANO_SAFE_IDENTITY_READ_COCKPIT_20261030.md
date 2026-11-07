# SAFE primeiro — plano independente preparado

- UUID: `0538bd35-9fd2-40bf-b25d-85713fa92bd5`.
- Hash de escopo para o backend: `8b115275bfe09b6094e1e00ed4dca92b5f0e794a5cee13426e6fd7f0efd72f18`.
- Hash documental SAFE, vinculando UUID, escopo e janela proposta: `1284f77ae20c6a0e95c662b3fd513e559f46b719251ef7a6eb7368d4250feb8a`.
- Estado persistido: PREPARED, sem aprovação, consumed=false, zero requests_reserved.
- Auditoria de preparação: ID 16. Preparação repetida com o mesmo UUID reutilizou o registro.
- Produto 2 / 1904573791:23199869203, conta local vinculada por fingerprint,
  URL/identidade/payload conferidos localmente; productOfferV2 page=1/limit=1.
- Máximo um POST; sem retry, fallback, paginação extra, mutations, links,
  scheduler, recorrência, histórico operacional ou Telegram.

## Janela proposta, ainda não autorizada

De `2026-10-30T14:42:39.340869+00:00` até `2026-10-30T15:02:39.340869+00:00`. Em Brasília: 30/10/2026,
11:42:39 até 12:02:39 (America/Sao_Paulo).
O fluxo existente aplica expires_at quando uma aprovação for concedida; PREPARED
não possui aprovação ou validade concedida. O início futuro é uma restrição do
procedimento administrativo, pois o adaptador existente não implementa not_before.
Não executar antes do início proposto. Se a janela expirar, solicitar revisão de
nova janela, sem consumir ou renovar silenciosamente a autorização.

O hash do escopo coincide com o da leitura histórica porque produto, conta, URL e
payload são idênticos: o contrato existente não inclui UUID nem validade nesse hash.
A autorização é independente pelo UUID novo; a histórica permanece consumida.
O hash documental acima distingue o plano SAFE com UUID/janela. Não substituir o
scope_sha256 do backend pelo hash documental.

## Procedimento administrativo após autorização explícita

1. GET `/api/v1/shopee-collection/identity-reads/0538bd35-9fd2-40bf-b25d-85713fa92bd5` para reconferir estado,
   hash, consumed=false e aprovação ausente. Usar a autenticação X-API-Key existente
   localmente, sem expor seu valor.
2. Somente após autorização do responsável para este UUID/escopo/janela, POST
   `/api/v1/shopee-collection/identity-reads/0538bd35-9fd2-40bf-b25d-85713fa92bd5/approve` com:

```json
{
  "scope_sha256": "8b115275bfe09b6094e1e00ed4dca92b5f0e794a5cee13426e6fd7f0efd72f18",
  "actor": "eduardo.viana.3",
  "confirmed_single_read": true,
  "expires_at": "2026-10-30T15:02:39.340869+00:00"
}
```

3. Dentro da janela, executar UMA VEZ POST
   `/api/v1/shopee-collection/identity-reads/0538bd35-9fd2-40bf-b25d-85713fa92bd5/execute`, com o mesmo payload
   removendo `expires_at`. Reserva/claim/auditoria precedem o HTTP no adaptador.
4. Capturar o retorno sanitizado, pois quote é efêmero. Não inserir histórico ou
   criar ofertas/outbox. Consultar somente o estado após erro ou resultado incerto;
   nunca criar outra autorização para repetir automaticamente.
5. Avaliar o resultado SAFE antes de sequer propor aprovação de outro piloto.

## Plano recorrente e bloqueios

O plano `1cfbb654-f4ab-4146-b185-e9f48e702540` continua PREPARED, used=0, sem aprovação
ou ciclo ativo, preservado integralmente. NÃO aprovar nem executar. Um eventual
piloto de seis horas exige avaliação satisfatória do SAFE e autorização separada.

A configuração local está presente e não há bloqueio local registrado para a conta.
Isso não comprova permissão atual da API: nenhuma chamada externa foi feita.
O contrato oficial local não informa quotas de requisições. Não foi identificada
restrição adicional nos materiais examinados que impeça preparar esta leitura;
quotas e permissões atuais continuam não confirmadas. Não interpretar ausência de
quota como autorização ilimitada.

Validações locais de identidade, URL, payload, conta, banco operacional e idempotência
passaram. Históricos 2/3/4, produto, autorizações anteriores e .env preservados.
Flags permanecem false, scheduler desativado e grupo pausado. Zero HTTP externo,
mutations, observações novas ou publicações. Nenhuma alteração de código,
reexecução de suíte extensa, commit ou push.
