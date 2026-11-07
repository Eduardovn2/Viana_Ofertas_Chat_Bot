# Validação ponta a ponta — 01/11/2026

Resultado: **8 testes aprovados** em PostgreSQL de testes, com migrações e schema UUID
isolado por cenário. Testes em `tests/test_postgres_multistore_assisted_e2e.py`.
Ruff aprovado. Guard HTTP: `real_http_allowed=false`, `blocked_requests=0`.

## Fluxo demonstrado

1. Nova autorização fictícia e descoberta por `ShopeeConnector` com MockTransport:
   três anúncios, dois vendedores relevantes e um acessório filtrado. Nenhuma
   observação temporal é criada pela descoberta.
2. Comparação inicialmente inconclusiva; tentativa de cadastro comercial bloqueada.
3. Revisão documental pelos endpoints administrativos. Confirmação falsa é rejeitada;
   confirmação explícita permite comparar os dois anúncios documentados.
4. Prévia comercial do anúncio `102:203`, variante documental azul e SKU cadastrado
   fictício `synthetic-blue-sku`. Preço anunciado R$ 1.000,00; diferença entre lojas
   R$ 100,00. Preço final entregue e desconto histórico permanecem desconhecidos.
5. Cadastro pelo endpoint de ofertas manuais com vínculo à observação/documentos,
   verificação comercial explícita e link sintético já cadastrado. Sem cupom,
   preço anterior ou desconto histórico; estado `PENDING_REVIEW`.
6. Prévia de publicação idempotente e aprovação explícita pelos endpoints existentes.
   Tentativas sem aprovação e com `confirmed=false` são bloqueadas.
7. Serviço existente de publicação e `TelegramPublisher` com MockTransport:
   reserva `SENDING` e tentativa `PENDING` persistidas antes do transporte; conclusão
   `SENT`, um envio simulado e um log. Repetir o despacho não duplica o envio.

## Outros cenários aprovados

- Alterar observação, documento, variante ou preço depois da aprovação invalida a
  publicação antes do transporte, encerra a prévia e remove a aprovação.
- Concorrência: dois despachos resultam em uma única tentativa/mensagem simulada.
- Cooldown: bloqueio imediato e em 2h59m59s; após três horas exige observação
  comercial nova e nova aprovação. A descoberta não é repetida.
- Timeout simulado: estado `UNCERTAIN`, reserva mantida, sem retry ou nova publicação
  automática para o mesmo produto/destino.
- Flags da aplicação permanecem desativadas. Somente o publicador local da fixture
  recebe configuração habilitada, token fictício e transporte simulado explícito.

Falha encontrada: configuração da chave administrativa sintética como `str` por
`model_copy`, que não revalida tipos. Corrigida a fixture para usar `SecretStr`.
Não foi necessária correção no código de produção para os cenários exercitados.

## Bloqueios e procedimento para um teste real controlado

A resposta real salva continua sem prova suficiente de equivalência. É necessário
completar evidências de identidade/variante e confirmar preço público aplicável,
disponibilidade e condições comerciais recentes. Frete, estoque e preço total não
podem ser presumidos; avaliações não comprovam reputação da loja.

Com um anúncio comprovado e link próprio previamente verificado, gerar a prévia
comercial administrativa e cadastrar a oferta em `/api/v1/manual-offers` com o
`multistore_context` atual e verificação comercial explícita. Depois gerar a prévia
por destino em `/api/v1/manual-offers/{id}/preview` e obter aprovação humana do hash
atual em `/api/v1/telegram/publications/{id}/approve`. O despacho existente é
`POST /api/v1/telegram/publications/{id}/dispatch`, com o hash atual e confirmação
explícita, somente após autorização própria. O envio real exige autorização
própria, destino permitido e todos os controles atuais do publicador. Não executar
envio nem habilitar configuração de produção como parte desta validação.

Se forem necessários novos dados da Shopee, preparar uma nova autorização específica
com escopo, janela e orçamento limitados; não reutilizar autorizações consumidas.
Nada disso foi aprovado ou executado nesta etapa.

Zero chamadas reais à Shopee/Telegram, zero links reais gerados e nenhuma alteração
em credenciais, flags, autorizações ou dados operacionais. Sem scheduler, commit ou push.
