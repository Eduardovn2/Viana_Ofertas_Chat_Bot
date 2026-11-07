# Produto 2 — preparação estável para oferta individual assistida

Conferido em **01/11/2026**, com PostgreSQL disponível e leitura operacional somente
leitura. Produto **2**, **Cockpit simulador de corrida Brave Bull**, identidade
**1904573791:23199869203**. Cadastro e históricos preservados.

## Dados estáveis prontos

- IDs, nome e URL original conferem com o caminho documental do longLink da resposta
  oficial preservada. O shortLink coincide com o registro auditado `SUCCEEDED`.
  Conferência exclusivamente local; parâmetros sanitizados não foram usados como
  prova de titularidade ou reconstruídos.
- O link existente tem formato aceito. **Nenhum AffiliateLink está associado** ao
  produto. A reserva antiga foi apenas lida; não foi executada novamente.
- Destino **2**, **Viana Ofertas**, **-5309308689**, na allowlist, pausado e com aprovação
  manual obrigatória, categorias `all`. Publicador/envios reais permanecem desativados.
- Monitoramento persistido `false`; produto sem monitoramento. O início do runtime
  foi corrigido para ler a configuração persistida **antes de criar o scheduler**.
  Com pausa, falha de leitura ou configuração inválida, não cria a thread. Para sair
  dessa condição será necessário comando administrativo explícito após configuração
  autorizada. A aplicação e o scheduler não foram iniciados nesta etapa.
- Zero ofertas/outbox/logs comerciais para o produto. As observações existentes são
  antigas e não confirmam disponibilidade, preço público atual ou desconto histórico.

## Duas confirmações estáveis do responsável, antes da janela de preço

**Produto/variante:** fornecer uma referência documental, nome/modelo exatamente
exibido, cor/opção escolhida, quantidade, conteúdo do kit e itens não incluídos.
Se houver um identificador oficial da opção, fornecê-lo com a origem; se não houver,
declarar essa ausência e documentar a seleção por nome/imagem. Não usar “azul” como
SKU inventado. A igualdade do preço exibido azul/vermelho já declarada não precisa
ser declarada novamente, mas não resolve seleção, kit ou disponibilidade atual.

O produto atual tem `variant_id=""`, não uma variante comprovada. A oferta só poderá
prosseguir no escopo documental que o cadastro/link realmente sustentarem. Se a
variante exigir identidade própria não vinculável ao cadastro atual, resolver seu
cadastro pelo fluxo existente antes da janela, sem editar históricos ou fabricar SKU.

**Link próprio:** confirmar que o link preservado pertence à conta afiliada do
responsável/beneficiário e corresponde exatamente ao anúncio e ao escopo escolhido.
Fornecer referência/data da comprovação e referência administrativa não secreta da
conta e da proveniência da credencial, sem transmitir credenciais. Não é necessário
inventar venda ou comissão. O resultado antigo comprova a geração e a identidade do
caminho; não comprova sozinho titularidade, atribuição ou seleção de variante.

O endpoint administrativo existente `PUT /api/v1/products/2/affiliate-link` aceita
**o mesmo shortLink preservado**, sem HTTP externo. Somente depois da comprovação e
autorização explícitas poderá receber confirmação operacional. O rascunho mantém
`operationally_verified=false`; nenhum vínculo ou atestado foi gravado agora.

[Formulário estável e rascunho de associação](PREPARACAO_ESTAVEL_PRODUTO_2.json).

## Verificação comercial atual — fazer por último

Com as pendências estáveis resolvidas e a operação futura autorizada, o responsável
deve conferir na fonte oficial a opção/kit escolhidos e registrar:

- `current_price`: preço **público e efetivamente aplicável** àquela opção, sem copiar
  observações antigas; condições exatas de pagamento e elegibilidade documentadas.
- `availability` e `price_access`: confirmar disponibilidade e acesso público;
  enquanto desconhecidos, permanecem `UNKNOWN` e o cadastro fica bloqueado.
- `actor` e `verified_at`: responsável efetivo e horário real da verificação com fuso.
- `expires_at`: validade no máximo até **verified_at + 600 segundos**, ou menor.
  Se a janela vencer antes de concluir aprovação/envio, verificar novamente as
  condições, criar nova oferta/prévia pelo fluxo normal e obter nova aprovação.

Usar inicialmente contexto **sem cupom**, sem alegar frete grátis ou parcelamento.
Manter `previous_price`, `coupon_id`, `installment_text` e `multistore_context` ausentes.
Frete e total entregue permanecem desconhecidos quando não comprovados. Não há
equivalência multiloja comprovada para esse produto nem alegação de menor preço.
`verified` fica **false** até existir confirmação comercial explícita do responsável.

[Formulário comercial pendente](FORMULARIO_OFERTA_MANUAL_PRODUTO_2_PENDENTE.json).
O envelope local não deve ser enviado; seu `body` só será submetido depois de
completado e confirmado. Não contém preço atual nem verificação inventada.

## Sequência mínima futura, após autorização específica

1. Resolver produto/variante e titularidade; associar o link preservado explicitamente
   verificado pelo endpoint existente. Não abrir ainda a janela de preço.
2. Preparar o destino/publicador para **um envio controlado** somente com autorização
   própria, mantendo scheduler pausado. A pausa do destino impede a prévia de publicação;
   as flags atuais impedem despacho. Aprovar um hash não remove esses bloqueios.
3. Fazer a verificação comercial acima e completar `POST /api/v1/manual-offers`.
   O cadastro classifica o produto pelo serviço existente; revisar o resultado antes
   da publicação. Cadastro manual não exige ativar scheduler nem consulta Shopee.
4. `POST /api/v1/manual-offers/{offer_id}/preview` com `destination_id=2` e, para texto
   somente, `include_image=false`. Revisar produto, opção/escopo, preço e mensagem.
5. Responsável aprovar **o hash atual dessa prévia** em
   `POST /api/v1/telegram/publications/{id}/approve`, com ator real e confirmação
   explícita. Não é a aprovação do hash de um formulário ou documento local.
6. Após autorização específica de **um envio para -5309308689**, usar o despacho
   existente `/api/v1/telegram/publications/{id}/dispatch` uma vez com o hash atual e
   confirmação explícita. Consultar o resultado; se incerto, não repetir. Encerrar
   as permissões temporárias conforme o escopo autorizado e manter a pausa.

Nada dessa sequência foi executado. Não é preciso aprovar hashes de testes, escolher
UUID novo de consulta ou gerar outro link para concluir esta preparação.

Validação: **27 testes locais** de scheduler/API aprovados, incluindo pausa persistida,
configuração indisponível e inválida, sem threads reais; Ruff aprovado. Leitura real e
checagens locais de identidade/formulários concluídas. Zero HTTP externo, alterações
em credenciais, históricos ou autorizações, ofertas criadas, aprovações ou envios.
