# Shopee generateShortLink: resultado da tentativa autorizada

- Status: **SUCCEEDED**; HTTP **200**; sem erros GraphQL.
- POSTs reais: **1**. Sem retry, redirect, fallback ou consultas adicionais.
- Produto: Cockpit simulador de corrida Brave Bull.
- shopId: `1904573791`; itemId: `23199869203`.
- shortLink: https://s.shopee.com.br/1qczeLcPMR

## longLink retornado (sanitizado)

```text
https://shopee.com.br/universal-link/Cockpit-simulador-de-corrida-Brave-Bull-estrutura-em-a%C3%A7o-resistente.-Volante-pedais-e-encosto-ajust%C3%A1veis.-i.1904573791.23199869203?[tracking parameters redacted]
```

Os valores dos parâmetros de rastreamento foram omitidos. Esta versão sanitizada não reproduz integralmente a URL utilizável retornada.

O caminho corresponde ao autorizado acrescido de `/universal-link`; os IDs conferem por comparação local, sem nova rede. A verificação operacional do conector permanece pendente porque o parser conservador não reconhece esse prefixo (`account_verified=false`). Comissão e atribuição de conversões não foram comprovadas. Nenhum produto, associação ou oferta comercial foi criado ou aprovado.

## Auditoria

- Autorização consumida: `50f837480acf995acb3a4eabd3901a113407042b16177d9fdf1a80442e95d9f9`.
- SHA256 do corpo exato: `60c07179bdf9fc4f83be86ec5bb3cde4de1e500aae30469d92149fca03214174`.
- AppSetting: `shopee_shortlink_once:50f837480acf995acb3a4eabd3901a113407042b16177d9fdf1a80442e95d9f9`.
- IntegrationEvent: **6 RESERVED**, **7 DISPATCH_CLAIMED**, **8 SUCCEEDED**, **9 IDENTITY_PATH_MATCH**.
- Reserva exclusiva em arquivo durável antes do HTTP: `.runtime/shortlink-once-50f837480acf995acb3a4eabd3901a113407042b16177d9fdf1a80442e95d9f9.jsonl`.
- Resultado sanitizado: `RESULTADO_GENERATE_SHORTLINK_SHOPEE.json`.

## Estado final

```ini
SHOPEE_ENABLED=false
MERCADO_LIVRE_ENABLED=false
TELEGRAM_PUBLISHER_ENABLED=false
TELEGRAM_REAL_SENDS_AUTHORIZED=false
SIMULATION_AUTOMATION_ENABLED=false
```

Grupo Viana Ofertas (`-5309308689`): `enabled=false`. Monitoramento pausado; geração global de links desativada. Zero publicações Telegram. `.env` inalterado e registros anteriores preservados. Nenhum commit ou push.

A autorização foi consumida: não repetir. Sem alterações de código ou repetição da suíte nesta operação; os 21 testes offline registrados na preparação pertencem à validação anterior.
