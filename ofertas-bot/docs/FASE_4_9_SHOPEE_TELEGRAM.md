# Fase 4.9 — consulta real e preparação comercial

**Consulta SUCCEEDED; preparação comercial BLOCKED.** Autorização explícita do responsável para uma única `productOfferV2`, termo `PlayStation 5`, página 1, limite 1. Executada em 21/10/2026 20:14:25 (America/Sao_Paulo).

## Resultado e persistência

- HTTP **200**, **1 requisição**, zero erros GraphQL, retries, fallback ou páginas adicionais.
- Produto: **Cabo HDMI 2.1 8K PS5 Gamer 1 Metro Melhor Qualidade Disponivel! Ponta de Ouro**. É um acessório cabo HDMI, não um console.
- Identidade Shopee: loja `1018042820`, item `22293101052`; produto local **id 1**.
- Preço informado/intervalo: **R$ 59,00**, mínimo e máximo R$ 59,00. Valores persistidos em Decimal; não se calculou desconto.
- Categoria local **Games** pela classificação existente. O filtro de produto específico PlayStation 5 recusou o acessório (`PRODUCT_FILTER_OR_AMBIGUOUS_INTENT`), candidata `FILTERED`.
- Histórico real **id 1**, fonte `shopee:official_api`, tipo `OFFICIAL_API`, `is_simulated=false`, observado em `2026-10-21T23:14:25.115255+00:00`.
- Comparação histórica **INITIAL**: é a primeira observação persistida, sem preço anterior ou percentual de queda. O status FILTERED da candidata não transforma a referência inicial em queda.
- Imagem retornada e registrada na evidência do histórico. Não gerada uma prévia comercial bloqueada.

## Auditoria e autorização

Serviço `ShopeeCollectionService.execute` e conector oficial existentes, com opt-in exclusivamente por execução. Reserva idempotente **0d19d8fa-f466-47d1-8ddb-6bbebb3751d5**, ator `responsavel-projeto-via-chat`, referência da autorização explícita, uma tentativa reservada, resultado SUCCEEDED e candidata vinculada ao produto/histórico. Evento HTTP adicional de auditoria **id 5**. Manifesto local conserva o UUID e impede repetir esta autorização; nenhuma credencial foi registrada.

O Docker Engine estava desligado. Iniciados o Docker Desktop existente pelo comando oficial e somente o container PostgreSQL já existente, sem reinstalar, recriar volumes ou alterar configurações. Conexão, revisão 0008, nove categorias e bloqueios foram conferidos antes do HTTP.

## Bloqueios comerciais

1. Resultado não corresponde à intenção de console; não foi substituído por outro produto.
2. Disponibilidade **UNKNOWN**. A resposta consultada não comprova estoque.
3. Acesso público ao preço **UNKNOWN**. Intervalo igual não comprova preço público ou condições para todos os compradores.
4. **Nenhum link de afiliado próprio cadastrado/verificado**. Geração/fornecimento de link próprio exige ação específica para o produto adequado; nenhuma mutation foi autorizada ou executada.

Não se presumiram cupom, preço antigo, desconto, parcelamento ou variante. Comissão não foi usada como desconto. A consulta autorizada não comportava uma segunda chamada para obter evidências adicionais; nenhuma foi realizada. Não existe oferta comercial aprovada, prévia, hash aprovado ou despacho desta fase. Não foi usado o envio técnico.

## Estado final

- `.env` e credenciais inalterados; cinco flags permanentes `false`.
- Viana Ofertas `-5309308689` permanece pausado; monitoramento principal desativado.
- Histórico: **+1** observação real; ofertas: **+0**; outbox comercial: **+0**; publicações técnicas: **+0**.
- **Zero chamadas de publicação Telegram**, zero mutations de links, zero polling comercial.
- Código, testes e migrações preservados; nenhuma suíte extensa repetida nesta consulta. Última regressão da Fase 4.8: 1.183 aprovados, incluindo 38 PostgreSQL reais.
- Nenhum commit ou avanço às Fases 5/6. A autorização desta consulta única está consumida e não permite reenvio/consulta adicional.

[Evidência sanitizada](VALIDACAO_FASE_4_9.json). Próximo passo: definir novo escopo de busca para um console adequado e obter autorização específica antes de qualquer outra consulta; comprovar condições e link próprio antes de preparar uma prévia para aprovação. Nenhum despacho é autorizado por este relatório.
