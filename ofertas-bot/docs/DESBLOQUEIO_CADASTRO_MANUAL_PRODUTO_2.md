# Cadastro individual do produto 2 — estado conferido em 01/11/2026

**Conexão PostgreSQL restabelecida. Cadastro da oferta permanece bloqueado por
verificações comerciais, não por indisponibilidade do banco.**

## Recuperação local e leitura real

O Docker Desktop estava encerrado. A tentativa de início sem elevação encontrou
falha de acesso ao socket do Secrets Engine (`engine.sock`, reparse point). Não foram
alterados socket, permissões, credenciais ou configurações; nenhum reset foi realizado.
Após o responsável informar que iniciou o Docker, foi confirmado o container existente
`ofertas-bot-postgres-1`, `running/healthy`, porta local 5432 e volume existente
`ofertas-bot_postgres_data`. Nenhum container/banco/volume foi recriado.

Leitura às **07h23 Brasília** (10:23 UTC), banco `ofertas_bot`, schema `public`,
transação `REPEATABLE READ, READ ONLY`, com `transaction_read_only=on`.

- Produto **2**, SHOPEE, **1904573791:23199869203**: Cockpit simulador de corrida
  Brave Bull. Nome, identidade e URL original podem ser reaproveitados.
- `variant_id=""`: variante não resolvida; não significa produto sem variantes.
- Monitoramento e prioridade desligados; nenhuma categoria vinculada atualmente.
- Históricos reais 2/3: R$ 1.099,00 sem cupom e R$ 956,13 Pix com cupom, captura de
  25/10. Histórico **4**: API oficial, **30/10/2026 06h29 Brasília**, R$ 1.099,00.
  Disponibilidade desconhecida em todos; preço público não confirmado. Não são preços
  atuais, finais, evidências de estoque ou queda histórica. Nenhum possui preço anterior.
- **Zero ofertas, outbox comercial e logs de publicação** para o produto.
- **Nenhum AffiliateLink associado**. A reserva histórica de geração está `SUCCEEDED`,
  uma tentativa, e o link salvo corresponde exatamente ao artefato preservado, conferido
  localmente. Não foi acessado ou gerado outro link. Titularidade/conta e vínculo à
  variante não comprovados; nenhum registro de evidência/atestado/proposta de confiança
  do produto encontrado nos prefixos usados pelo serviço existente.
- Destino **2**, **Viana Ofertas**, **-5309308689**: `enabled=false`,
  `manual_approval_required=true`, categorias `all`, na allowlist local.
- Publicador/envios reais/Shopee/geração de links desligados; scheduler persistido
  `false`. O default de scheduler na configuração de ambiente é `true`, mas nenhuma
  aplicação/scheduler foi iniciada nesta operação e nenhum valor foi alterado.

## Formulário pronto para completar, não para enviar

[Payload pendente](FORMULARIO_OFERTA_MANUAL_PRODUTO_2_PENDENTE.json).
Método futuro: `POST /api/v1/manual-offers`; o objeto `body` usa apenas os campos
do endpoint existente. `current_price` e `actor` estão `null`, `verified=false`,
`availability=UNKNOWN`, `price_access=UNKNOWN`; horários e validade pendentes.
O envelope é um formulário local, não uma requisição aprovada. Foi validado que o
corpo incompleto é recusado pelo schema nos campos preço/responsável. Não houve POST.

### Confirmações exatas que faltam

1. Modelo, cor/variante selecionada, quantidade e conteúdo do kit; identificação
   oficial de SKU somente se disponível. Não converter nome de cor em SKU. A igualdade
   do preço exibido azul/vermelho já declarada permanece preservada e não precisa ser
   declarada novamente; ela não identifica a seleção nem confirma preço atual.
2. Preço público atual para esse escopo, pagamento/elegibilidade e disponibilidade,
   com referência e responsável. Caminho mínimo: sem cupom, sem alegação de frete grátis
   ou parcelamento. Se tais condições forem usadas, precisam de comprovação própria.
3. Data/hora real da verificação e validade. Limite vigente de frescor: **600 segundos**;
   a oferta não pode exceder verificação + 10 minutos, podendo expirar antes.
4. Titularidade/conta beneficiária do link existente e correspondência ao anúncio e
   variante. Depois de confirmação explícita, associar o mesmo link pelo endpoint
   existente de afiliado; não marcar verificação operacional por inferência.

O endpoint de produto não permite editar `variant_id` via PATCH. Não há alteração
automática proposta: se a confirmação exigir identidade de variante distinta, definir
o cadastro correto pelo fluxo existente antes de vincular a oferta, preservando o
produto e os históricos atuais. Não inventar um identificador para liberar a passagem.

## Próxima ação mínima

Receber as quatro confirmações acima, registrar o vínculo verificado do link existente
e completar o formulário com verificação comercial explícita. Revalidar esses dados
no momento do cadastro pelo serviço manual existente. Classificar/revisar a categoria
no fluxo normal; nenhuma categoria foi escrita agora. Manter preço anterior e cupom
ausentes se não houver evidências próprias.

Não há comparação multiloja comprovada para este produto: usar oferta individual,
sem `multistore_context`, desconto histórico inventado ou alegação de menor preço.
O cadastro não exige ativar Telegram. Uma futura prévia de publicação exigirá tratar
a pausa do destino com autorização própria e obter aprovação explícita do hash e
autorização de envio. Nada disso foi autorizado automaticamente nesta preparação.

Evidência estruturada de leitura: `.runtime/assisted-offer-product2-live-readonly.json`.
Conferência local do formulário e `.env` preservado; nenhuma nova suíte extensa.
Zero chamadas Shopee/Telegram, links criados, gravações operacionais, atestados,
ofertas ou publicações. Sem scheduler, alteração de flags, commit ou push.
