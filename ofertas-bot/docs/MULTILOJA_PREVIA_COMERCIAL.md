# Prévia comercial multiloja

`POST /api/v1/shopee-discovery/multistore-searches/{uuid}/candidates/{shopId}/{itemId}/commercial-preview`

Endpoint administrativo autenticado. Corpo: `{"target": null, "offer_id": null}`.
Reanalisa somente a resposta salva e suas revisões documentais atuais. Não consulta a
Shopee, não grava ofertas, observações, categorias ou outbox e não gera links.
Mostra também candidatos não comprovados, com seus impedimentos.

A resposta contém produto, loja, identidade, preço anunciado, atributos documentados,
inferências separadas, fontes, pendências e `next_step.multistore_context`.
O hash da prévia identifica o contexto; **não concede aprovação nem autorização**.
Empates entre lojas preservam as duas opções. Somente os menores preços de grupos
comprovados são selecionados pelo `commercial_handoff` existente.

## Entrada no fluxo existente

Para cadastrar uma oferta, o responsável usa o já existente `POST /api/v1/manual-offers`,
com os campos comerciais normais e o `multistore_context` retornado pela prévia.
Informar esse vínculo juntamente com `verified=true` confirma que a verificação
comercial se aplica à variante cadastrada e às condições documentadas do candidato.
Não copiar automaticamente `announced_price` para um preço confirmado ou enviar
`verified=true` por padrão. Disponibilidade, preço público aplicável, prazo de validade
e link próprio já verificado continuam obrigatórios pelas regras existentes.

Esse caminho rejeita candidato inconclusivo, acessório, identidade ou preço divergente,
contexto alterado e evidência antiga para a verificação comercial. Não aceita
`previous_price`: a diferença entre lojas não é desconto histórico. Uma nova
observação manual é criada somente pelo cadastro explicitamente verificado; consultar
a prévia nunca cria histórico. Fontes simuladas só podem atravessar o adaptador no
ambiente de testes.

O vínculo fica nos metadados da nova observação manual. `read_manual_offer` o revalida
antes da prévia de publicação, aprovação e envio existentes. Mudanças na observação,
nos documentos, no alvo, na seleção ou na variante invalidam o reaproveitamento.
Não há migração de dados nem alteração de ofertas/autorização anteriores.

Uma única oferta válida vinculada é reutilizada automaticamente; `offer_id` permite
escolha explícita quando houver alternativas. Ofertas sem vínculo não são promovidas
a evidência multiloja pela coincidência de nomes ou preços.

Depois do cadastro, usar o já existente
`POST /api/v1/manual-offers/{offer_id}/preview` com `destination_id`. Esse passo grava
a prévia da publicação e verifica categoria, destino e cooldown. A aprovação humana
continua em `POST /api/v1/telegram/publications/{id}/approve`, com hash atual, ator e
`confirmed=true`. Nenhuma dessas ações é executada pela prévia multiloja.

Frete e preço total entregue permanecem desconhecidos. O preço ajustado por cupom
de uma oferta válida é mostrado separadamente e não representa total com frete.
Avaliação, vendas e tipo da loja mantêm a semântica do conector e não comprovam
reputação. Os bloqueios e autorizações de publicação existentes permanecem ativos.

## Validação local

Testes: `tests/test_shopee_commercial_handoff.py` e
`tests/test_postgres_shopee_commercial_handoff.py`. Os testes PostgreSQL exigem
`TEST_DATABASE_URL` para banco dedicado com sufixo `_test` e usam schema UUID isolado.
Nenhum teste permite HTTP real ou envia mensagens.
