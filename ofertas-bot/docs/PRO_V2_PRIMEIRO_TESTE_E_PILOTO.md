# PRO_V2: primeiro teste e piloto de 30 minutos

Estado: PREPARED, sem aprovações, HTTP ou envios. O HDMI 1018042820:22293101052 não participa.
Janela: 05/11/2026 00:12:12–02:12:12 Brasília (03:12:12Z–05:12:12Z).
O primeiro teste tem duração máxima de 600 s e até uma mensagem. O piloto dura 1800 s desde a ativação persistida; precisa iniciar até 01:42:12 Brasília.

| Escopo | UUID | SHA-256 | Orçamento |
|---|---|---|---|
| Primeiro envio | 7ef42585-3d5d-4ea3-8a2d-856b1939a8cf | 5ea8d00569783e81f85063b56c446f593f127cf1dc8fb4f47d9c55f14d19f2a2 | 1 productOfferV2 + 1 Telegram |
| Link primeiro anúncio | e1efbf51-6233-42d9-885e-8928e984a285 | e2e9b9c5ad510f82edf6000f30459c091762367dfb226defb2f0463bcad0406e | 1 generateShortLink |
| Piloto 30 minutos | 16c52aca-1426-48e2-bcb6-387dd5c6922b | da011802f42ebce420130815faf925d0af5e00695f396a91f30582858a03dcc8 | 3 productOfferV2 + 3 Telegram |
| Links piloto | fab4d580-3db3-4140-875c-f2fa2f56297a | 6a30a7e57f25ab7bf2c4075642fa5379f593777b6c6076b8b4917ee96833093e | 3 generateShortLink |

Primeiro anúncio: 1595990092:58202217973 — Suporte Cockpit Para Volante Pedais Para Simulador De Voo Brave Bull.
Os R$ 639,00 da captura de 01/11 são somente referência histórica. Não entram como preço vigente.
Prévia ainda não definitiva: .runtime/previews/first-pro-v2-preparation/preview.html. Preço, link e imagem dependem das operações autorizadas.

## Operação após confirmação específica
1. Aprovar o lote de links com seu próprio hash em POST /api/v1/integrations/shopee/link-batches/{UUID}/approve; executar uma vez em /execute.
2. Só prosseguir com recibos confirmados de conta, credencial e identidade. UNCERTAIN não é repetido. Falha não autoriza outro lote nem fallback.
3. Aprovar o piloto correspondente com seu hash em POST /api/v1/auto-pilots-real/{UUID}/approve e iniciar em /start.
4. Executar o worker independente abaixo. O primeiro teste encerra ao consumir a única tentativa ou ao atingir prazo; o piloto de 30 minutos encerra por prazo, orçamento de mensagens ou parada administrativa.

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_worker --pilot-id 16c52aca-1426-48e2-bcb6-387dd5c6922b --scope-sha256 da011802f42ebce420130815faf925d0af5e00695f396a91f30582858a03dcc8
```

O lote de três links é uma preparação separada, antes do worker: não amplia os seis POSTs autorizados no piloto. Total conjunto do piloto e lote: no máximo nove POSTs. Primeiro teste e lote: no máximo três. Todas as etapas combinadas: no máximo doze.
O piloto considera exclusivamente 1904573791:23199869203, 1754100018:22799528968 e 1754100018:58219916237.
O ranking de 100 pontos existente define prioridade de fila. Cobertura parcial permanece parcial, não vira aprovação comercial. Não existe promessa de três envios: um anúncio ausente, cotação ambígua, link inválido ou cooldown resulta em bloqueio.
Cadência: até três tentativas em janela deslizante de 600 segundos; cooldown por produto/destino de 10800 segundos. Consultas distribuídas a cada 600 segundos, sem retry ou paginação adicional.
Autorização administrativa única por escopo; não se pede aprovação por produto dentro do lote/piloto confirmado.
Preço anunciado precisa ter menos de 600 segundos no despacho. Template PRO_V2 e imagem oficial seguem o hash do escopo e a outbox revalida a mensagem antes do HTTP. Não há alegação de estoque, preço final ou composição.
Permissão efetiva e quotas de generateShortLink ainda não foram reconfirmadas com a credencial atual; uma falha é registrada e não autoriza insistência.
Não foram alterados pilotos anteriores, mensagens publicadas, ofertas ou históricos. Foram cadastrados apenas três produtos em preparação, com origem real preservada e monitoramento desligado.
