# Primeira oferta assistida — preparação bloqueada — 01/11/2026

Selecionado para completar a documentação: **Cockpit simulador de corrida Brave Bull**,
produto local **2**, shopId **1904573791**, itemId **23199869203**.
Seleção por documentação e link existentes, sem recomendação de melhor preço.

## Evidências disponíveis

- Cadastro documental real e captura fornecida pelo responsável, de 25/10/2026,
  21h11 Brasília. Fingerprint da imagem preservada conferido localmente.
- R$ 1.099,00 exibidos sem cupom em outros métodos; R$ 956,13 exibidos no Pix com
  cupom. São contextos diferentes, antigos e não confirmados como preços aplicáveis.
- Igualdade do preço exibido entre azul/vermelho já declarada pelo responsável;
  não precisa ser declarada novamente. Seleção/SKU e disponibilidade continuam pendentes.
- Link curto existente no resultado oficial preservado, com os mesmos IDs de loja/item.
  Não foi gerado ou acessado outro link. Identidade do caminho não comprova titularidade
  da conta/beneficiário. Último cadastro documental: nenhum link associado ao produto.
- Busca multiloja salva: três anúncios de duas lojas, zero grupos comprovados.
  Os IDs desses anúncios são diferentes do produto 2; os dados não foram mesclados.

## Checklist específico

- [ ] Revalidar banco operacional e destino em leitura: PostgreSQL indisponível nesta
  execução e Docker Engine inativo. Nenhum serviço foi iniciado. Estado atual do banco
  não confirmado; os estados abaixo são os últimos documentados.
- [ ] Documentar modelo, quantidade, conteúdo do kit e variante efetivamente escolhida,
  com origem e data. Não inventar SKU nem tratar cor textual como identificador oficial.
- [ ] Responsável confirmar preço público atual aplicável à variante, disponibilidade,
  pagamento/elegibilidade, data/hora da verificação e prazo de validade.
  Preferir contexto sem cupom para reduzir pendências; não reutilizar os valores antigos
  como preço verificado. Frete/parcelamento/cupom somente se comprovados e anunciados.
- [ ] Comprovar titularidade e vínculo do link existente à conta e ao anúncio/variante.
  Após confirmação explícita, associar o mesmo link pelo serviço existente; não gerar
  outro. Não presumir `account_verified` nem aprovar política de confiança proposta.
- [ ] Autorizar cadastro operacional somente após a verificação comercial explícita.
  Nenhum `verified=true` ou atestado foi preenchido nesta preparação.
- [ ] Revisar categoria/destino, gerar prévia da publicação, aprovar explicitamente o
  hash atual e autorizar um envio comercial controlado, sem retry automático.

Destino documentado: **Viana Ofertas**, id **2**, chat **-5309308689**, pausado,
aprovação manual obrigatória, categorias `all`. Configuração local lida nesta execução:
grupo na allowlist, publicador desativado e envios reais não autorizados. Estado atual
da linha do destino não foi revalidado devido à indisponibilidade do banco.

## Possibilidade de prévia e caminho mínimo

**Sem prévia comercial/publicável neste estado.** Este documento é um checklist,
não uma prévia de oferta, autorização ou atestado. Faltam variante e condições comerciais,
link próprio operacionalmente verificado e acesso ao banco para conferir os vínculos.

Uma oferta individual pode seguir o cadastro manual existente após essas confirmações,
sem aguardar outra busca: `POST /api/v1/manual-offers`, sem `multistore_context`, sem
preço anterior artificial e sem alegar menor preço entre lojas. Esse caminho preserva
todas as exigências do serviço manual. Para usar o handoff multiloja, é necessário
primeiro documentar equivalência de anúncios de vendedores distintos.

Depois: `POST /api/v1/manual-offers/{id}/preview` para o destino aprovado; aprovação
do hash em `/api/v1/telegram/publications/{id}/approve`; despacho somente com autorização
própria em `/api/v1/telegram/publications/{id}/dispatch` e controles de envio válidos.
Nenhum desses passos foi executado.

Fontes: `CADASTRO_DOCUMENTAL_COCKPIT_SHOPEE.json`,
`EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.json`,
`RESULTADO_GENERATE_SHORTLINK_SHOPEE.json`, `AJUSTE_VALIDADOR_LINKS_SHOPEE.md`,
`RESULTADO_BUSCA_MULTILOJA_20261031_2210.json` e `VALIDACAO_CONEXAO_TELEGRAM.md`.
Resumo estruturado local: `.runtime/first-real-assisted-offer-preparation.json`.

Zero chamadas externas, links gerados, ofertas/atestados/publicações criados, alterações
de históricos, credenciais ou flags. Nenhuma autorização consumida foi executada
novamente. Sem testes sintéticos adicionais, commit ou push.
