# AUTO_PILOT real — implementado, preparado, não aprovado ou iniciado

`app/services/auto_pilot_real.py` estende o controlador existente. Reutiliza fila,
aprovação automática por regras, outbox, cooldown, cadência e reservas. Não modifica
as guardas do laboratório: teste do adaptador real exige dois MockTransport explícitos
e banco isolado; produção exige `ofertas_bot/public`. O ambiente é vinculado ao escopo.

## Plano específico preparado

- UUID: **d80879af-27e3-47fc-b47b-01e3499a3006**.
- scope_sha256: **fc942c3b9f2e0a28ea63f75762aadbe8f6580d2700bc50977e5944d0bbe6b387**.
- Estado persistido: **PREPARED**, sem aprovação, reservas, HTTP ou worker.
- Janela proposta: **03/11/2026, 09h–11h Brasília**;
  `2026-11-03T12:00:00Z` até, exclusivamente, `2026-11-03T14:00:00Z`.
- Viana Ofertas, destino **2**, chat **-5309308689**.
- Até **20 tentativas de publicação**, **5/600 segundos**, **12 consultas Shopee** e
  **32 tentativas HTTP totais**. Ticks a cada 30 segundos, sem scheduler global.
- Termos: `Cockpit simulador Brave Bull`, `Creatina monohidratada 250g`,
  `Mouse Logitech G203`; separados das regras comerciais e sem caráter probatório.
- `productOfferV2`, página 1, limite 3. Sem retry, fallback, paginação adicional ou
  geração de links. Payload exato de cada termo vinculado ao seu SHA256 no escopo.

[Manifesto preparado e inventário](AUTO_PILOT_REAL_PREPARADO_20261102.json).
O plano de 01/11 foi preservado, sem execução; sua janela de 02/11 já expirou. Mudança
de janela, termos, credenciais, destino, regras ou orçamento exige novo escopo/UUID.

## Autorização e fronteira de HTTP

Plano e aprovação recebem assinaturas HMAC distintas, derivadas das credenciais já
existentes, sem criar/expor/alterar segredos. Escopo inclui fingerprints de credenciais,
URLs, regras, chat, orçamentos, janela e ambiente. Recalcular apenas o hash público
não valida um escopo alterado. Assinaturas não são expostas na resposta administrativa.

Antes de cada reserva e transporte: assinatura/estado/janela, orçamento e credenciais
atuais, banco correto, scheduler persistido desligado, destino e allowlist. No HTTP:
método, URL, corpo e hash exatos; assinatura/autenticação Shopee; chat, mensagem,
oferta, evidência e outbox Telegram correspondentes. A permissão de transporte é
consumida em transação antes de chamar o transporte. Redirect/retry ficam desativados.

O destino só é ativado temporariamente depois de aprovação válida e início na janela.
A posse exclusiva é persistida; o fim/parada restaura o estado anterior. Não são
habilitadas flags globais: conectores/publicador habilitados existem só no processo
do worker, e o transporte continua exigindo a autorização a cada chamada.

Timeout e claims interrompidos não repetem automaticamente a mesma publicação.
Consultas com falha/incerteza não repetem aquele payload no piloto. Recusa de permissão
Shopee ou Telegram encerra o piloto antes de novas tentativas. Prazo, orçamento ou
parada também encerram. Uma requisição já iniciada pode terminar. Em queda abrupta do
processo, usar a parada administrativa para reconciliar/restaurar o destino; não
reiniciar publicações incertas. O relatório identifica reservas não finalizadas.

## Procedimento de aprovação única e início futuro

O backend assistido em `127.0.0.1:8000` permite preparar, ler, aprovar e parar planos,
mas bloqueia `/start` e `/tick` reais. Assim, iniciar o backend não inicia um piloto.
Todos os endpoints abaixo exigem autenticação administrativa local.

1. Conferir `GET /api/v1/auto-pilots-real/{uuid}` e o manifesto.
2. Somente após o responsável autorizar expressamente esse escopo e reconhecer as
   quotas/permissões não confirmadas, registrar em
   `POST /api/v1/auto-pilots-real/{uuid}/approve` o `scope_sha256` exato, ator real,
   `confirmed_pilot_scope=true` e
   `acknowledge_unconfirmed_quotas_and_permissions=true`. Isso aprova o piloto e suas
   regras, não dados comerciais incompletos. Não foi chamado nesta preparação.
3. O worker independente agora espera pela ativação administrativa. Execute o
   perfil `app.deployment:create_control_app` e, dentro da janela e somente após
   aprovação, use `POST /api/v1/auto-pilots-real/{uuid}/start` com o hash exato.
   O comando abaixo mantém o processo do worker, sem aprovar ou iniciar por conta própria:

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_worker --pilot-id d80879af-27e3-47fc-b47b-01e3499a3006 --scope-sha256 fc942c3b9f2e0a28ea63f75762aadbe8f6580d2700bc50977e5944d0bbe6b387
```

O endpoint start ativa o destino no escopo autorizado. O worker executa ticks e
revalida cada oferta sem aprovação individual. PREPARED/APPROVED/PAUSED aguardam
comando administrativo; o processo não reabre autorização encerrada. SIGTERM encerra
o processo, preservando o grant para reinício seguro. Para encerrar o piloto, use
stop. Procedimento completo em `DEPLOY_AUTONOMO.md`.

4. Acompanhar `GET /api/v1/auto-pilots-real/{uuid}`. Parada imediata de novas chamadas:
   `POST /api/v1/auto-pilots-real/{uuid}/stop`. A parada funciona sem exigir credencial
   comercial ainda válida e restaura o destino que pertencia ao piloto.

## Inventário real e diagnóstico objetivo

Consulta de **02/11/2026, 17h Brasília**: **zero ofertas Shopee cadastradas, zero
comercialmente elegíveis**. Nenhuma oferta foi fabricada. Produto 2 e históricos foram
preservados. O link existente não foi associado, nem a declaração de titularidade
promovida a verificação operacional. Mesmo autorizado, o piloto poderá publicar zero.

Fontes ainda necessárias para alimentar a fila elegível:

- Identidade, variante, quantidade e kit documentados com origem/hash e vínculo
  explícito. A seleção azul declarada do produto 2 não é SKU/prova documental.
- Preço público aplicável, pagamento/elegibilidade e disponibilidade atuais, com
  verificação comercial válida por no máximo 600 segundos. Descoberta não cria
  atestados nem atualiza uma verificação antiga automaticamente.
- Link próprio previamente verificado, com referência de titularidade/conta e vínculo
  ao anúncio/variante. Sem nova geração de links.

O contrato atual de afiliado não comprova sozinho esses fatos, preço total/frete,
estoque de variantes ou reputação da loja. Documentos oficiais fornecidos pelo
responsável podem completar a evidência; documento de fabricante comprova características,
não disponibilidade/preço de uma loja. Não se presume acesso a APIs de vendedor ou feeds.

Quotas oficiais atuais e permissões da conta/bot **não foram consultadas nem confirmadas**.
Sucessos históricos não provam capacidade atual de 12 consultas/20 mensagens. O plano é
um teto local; não uma quota concedida pelos provedores. Não houve chamada externa para
validar essas permissões. Erros 401/403 encerram; 429 não provoca retry automático.

## Validação desta implementação

Testes do adaptador real com MockTransport e PostgreSQL isolado: assinatura adulterada,
payload/URL fora do escopo, autorização ausente, cadência/cap de 20, prazo na fronteira
de HTTP, parada, restauração, timeout, autenticação administrativa e isolamento.
Laboratório e startup assistido também foram revalidados. Ruff aprovado.
Resultados: **20 testes específicos do adaptador real**, **13 PostgreSQL isolados**
e **44 testes na regressão conjunta** com laboratório/startup assistido, todos aprovados.
As contagens se sobrepõem. Guardas de HTTP real: zero tentativas externas.
Backend e plano foram conferidos por HTTP somente em loopback. Zero chamadas externas,
destino ativado, worker iniciado ou aprovação real. `.env`, credenciais, históricos e
autorizações anteriores preservados. Apenas o novo plano foi registrado como PREPARED.
