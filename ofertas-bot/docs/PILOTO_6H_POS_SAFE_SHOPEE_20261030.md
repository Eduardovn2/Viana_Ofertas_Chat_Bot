# Etapa de seis horas após SAFE — somente preparação

SAFE 0538bd35-9fd2-40bf-b25d-85713fa92bd5 verificado SUCCEEDED, consumido, uma
requisição HTTP 200, quote íntegro contra auditoria persistida e eventos 16–19 na
ordem PREPARED/APPROVED/CLAIMED/SUCCEEDED. Nenhuma autorização histórica reutilizada.

O plano anterior 1cfbb654-f4ab-4146-b185-e9f48e702540 continua integralmente preservado,
PREPARED, used=0, sem aprovação/ciclo ativo. Não aprovar nem executar esse plano.

## Novo escopo independente

- UUID: `59c8346b-6767-48c3-8f70-5de398b624ca`.
- scope_sha256: `69be36bbd90033e2da39159046a43a99eee4a85d07d98bf1d60724b033c6b88a`.
- Estado durável: PREPARED, sem aprovação, used=0, nenhum ciclo ativo.
- Auditoria de preparação: ID 20.
- Produto 2, shopId 1904573791/itemId 23199869203, conta vinculada por fingerprint.
- Janela: **2026-10-30T16:00:00Z até 2026-10-31T16:00:00Z**.
- Brasília: **30/10/2026 às 13h até 31/10/2026 às 13h**.
- Até dois POSTs productOfferV2, page=1/limit=1; nenhuma outra operação.
- Intervalo mínimo 21600 segundos após a conclusão da primeira leitura.
- Validade total 24h, sem renovação automática.

[Plano, vínculos e verificações](PLANO_PILOTO_6H_POS_SAFE_SHOPEE_20261030.json).

## Funcionamento autorizado somente numa etapa posterior

Reutiliza ShopeeRecurringMonitor/ShopeeSingleReadAdapter, reservas e leases existentes.
Reserva do ciclo/orçamento e claim da leitura filha persistidos antes do HTTP.
Idempotência impede repetir ciclo consumido. Sem retry, fallback ou paginação extra.
403, 429, timeout, UNCERTAIN, resposta vazia/divergente ou falha de persistência
pausam a autorização e impedem a segunda leitura; não liberar automaticamente.

Observações futuras serão OFFICIAL_API informativas, horário efetivo da coleta,
UNKNOWN para acesso/disponibilidade, commercial_verified=false e observation_only=true.
Somente contexto oficial compatível será comparado pelo PriceComparisonService.
Histórico oficial ID 4 preservado; seu price_basis foi conferido e é compatível com
ITEM_QUOTE de variante/condições desconhecidas. Se uma futura resposta apresentar
faixa/contexto incompatível, não inferir queda contra essa referência.
A resposta SAFE não foi inserida no histórico. Nenhuma observação foi criada agora.

## Limites da evidência oficial

O contrato oficial local documenta productOfferV2 por identidade e campos selecionados,
mas não confirma quotas de requisições nem permissão para recorrência. Não foi
identificada proibição periódica nos materiais locais examinados; isso não demonstra
permissão irrestrita. Os dois HTTP 200 anteriores comprovam apenas suas respectivas
leituras, não quotas, disponibilidade futura ou autorização ilimitada.
O piloto exige ciência expressa dessas incertezas. Bloqueios existentes da conta ou
alterações do contrato/payload/conta invalidam a execução até revisão.

## Aprovação e execução administrativas futuras

Nenhuma aprovação foi concedida nesta preparação. Com esta versão do backend carregada,
usar autenticação administrativa X-API-Key local, sem expor seu valor:

1. GET `/api/v1/shopee-collection/recurring-pilots/59c8346b-6767-48c3-8f70-5de398b624ca`: conferir PREPARED,
   used=0, hash, conta, janela e ausência de aprovação.
2. Somente após autorização explícita do responsável para este UUID/hash/janela,
   POST `/api/v1/shopee-collection/recurring-pilots/59c8346b-6767-48c3-8f70-5de398b624ca/approve`:

```json
{
  "scope_sha256": "69be36bbd90033e2da39159046a43a99eee4a85d07d98bf1d60724b033c6b88a",
  "actor": "eduardo.viana.3",
  "confirmed_experimental_pilot": true,
  "acknowledge_unknown_quotas": true
}
```

3. Na janela válida, POST
   `/api/v1/shopee-collection/recurring-pilots/59c8346b-6767-48c3-8f70-5de398b624ca/cycles/{NOVO_UUID}/execute`
   com o mesmo payload. Uma invocação faz no máximo um POST externo.
4. Consultar apenas o estado após a resposta. Somente primeiro ciclo SUCCEEDED,
   orçamento disponível, janela vigente e next_due decorrido admitem segundo UUID.
   Aguardar pelo menos seis horas após a conclusão do primeiro; nunca repetir ciclo
   consumido nem criar outra autorização para contornar erro/incerteza.
5. Encerrar após duas reservas, falha ou expiração. Não habilitar scheduler global,
   flags, Telegram, mutations, links ou ofertas. Nenhum job foi instalado agora.

Texto de autorização sugerido, **ainda não concedido**:

> Autorizo o piloto 59c8346b-6767-48c3-8f70-5de398b624ca, hash 69be36bbd90033e2da39159046a43a99eee4a85d07d98bf1d60724b033c6b88a,
> para o produto 2 e conta vinculada, de 2026-10-30T16:00:00Z a
> 2026-10-31T16:00:00Z. Autorizo até duas consultas productOfferV2, page=1/limit=1,
> separadas por no mínimo seis horas após conclusão da primeira, com registro
> observacional OFFICIAL_API e auditoria. Reconheço quotas e permissão recorrente
> não confirmadas; interromper diante de erro/incerteza, sem retry ou fallback.
> Não autorizo outros escopos, links, mutations, ofertas, Telegram ou scheduler global.

Se a janela expirar, não deslocar as datas nem criar outra autorização automaticamente.

## Verificação desta preparação

Somente um AppSetting novo e a auditoria 20 foram inseridos. Todas as linhas anteriores
permaneceram idênticas por hash, incluindo produto, históricos e autorizações. .env,
credenciais, flags e grupo preservados; flags false, scheduler desativado, grupo pausado.
Zero chamadas externas, mutations, links, ofertas, outbox ou publicações. Nenhuma
alteração de código/migração; sem repetir suíte de testes, commit ou push.
