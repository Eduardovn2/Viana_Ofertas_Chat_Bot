# Consulta pontual e proposta de comparação multiloja — 31/10/2026

## Funcionamento existente e validação local

ShopeeConnector.fetch_product usa productOfferV2(shopId,itemId,page:1,limit:1).
Identidade retornada e URL são validadas; preços usam Decimal e faixas/variantes
ambíguas não ganham preço final. Discovery por keyword e paginação delimitada já
existe em discover_offers; ShopeeCollectionService, DiscoveryOperations e os filtros
podem ser reutilizados. Não houve alteração de código ou payloads nesta etapa.

29 testes Pytest direcionados aprovados, 91 não selecionados; transporte real
bloqueado. Cobrem normalização, identidade/URL, busca, erros e timeout. Além disso,
uma consulta MockTransport do produto 2/1904573791:23199869203 extraiu nome fictício,
preço Decimal fictício e shopId corretamente, mantendo UNKNOWN/UNKNOWN e
commercial_verified=false. A simulação não usou credenciais reais nem gravou preço.
[Validação e fixture demonstrada](VALIDACAO_CONSULTA_COCKPIT_MULTILOJA_20261031.json).
Não foi executada suíte completa ou PostgreSQL isolado: não houve implementação
nova, migração ou mudança no esquema.

## Campos e limites da fonte oficial disponível localmente

Fonte: contrato do Explorer preservado em CONTRATO_FONTES_SHOPEE_20261026.json.
Nenhuma consulta externa foi realizada para atualizá-lo.

| Informação | Campo/estado |
|---|---|
| Identidade do anúncio | shopId + itemId; selecionados e validados |
| Nome | productName; selecionado |
| Preço anunciado/faixa | price, priceMin, priceMax; selecionados, sem provar preço final |
| Imagem/categorias | imageUrl/productCatIds; validação conservadora existente |
| Percentual anunciado | priceDiscountRate; separado de histórico, cupom e comissão |
| Períodos | periodStartTime/periodEndTime; preservados brutos, sem unidade confirmada |
| Identidade da loja | shopId disponível; seller_id não é preenchido como identidade de pessoa |
| Nome da loja | shopName documentado, não selecionado/normalizado atualmente |
| Avaliação | ratingStar documentado como String; não selecionado nem validado atualmente |
| Vendas | sales documentado; não selecionado; não equivale a reputação |
| Tipo da loja | shopType documentado como lista de inteiros; não selecionado; não é nota de reputação |
| Reputação do vendedor | Não comprovada por esta seleção/contrato local |
| Moeda | BRL é contexto do conector brasileiro, não campo currency retornado pela seleção |
| Estoque, variante, preço público/final, frete e cupom aplicável | Não comprovados pela resposta atual |

ratingStar não deve ser promovido a reputação do vendedor. Faltam confirmação de
semântica, seleção e validação dos campos, bem como limites/permissões atuais da
conta. Ausência de avaliação não pode ser transformada em nota zero ou avaliação
positiva. Campos não retornados continuam desconhecidos.

## Leitura real independente preparada para hoje

- UUID: `b08a1923-b111-4171-84bf-1b3fb34731e5`.
- scope_sha256: `8b115275bfe09b6094e1e00ed4dca92b5f0e794a5cee13426e6fd7f0efd72f18`.
- Estado PREPARED; consumed=false, aprovação ausente, requests_reserved=0.
- Auditoria: ID 24. Produto 2 e conta local vinculados por fingerprint; URL/payload
  preservados. Uma tentativa productOfferV2, page=1, limit=1; sem ingestão.
- Janela proposta: `2026-10-31T23:06:28.146857+00:00` a `2026-10-31T23:26:28.146857+00:00`.
- Brasília: **31/10/2026, 20:06:28 até 20:26:28**.

[Plano JSON](PLANO_LEITURA_PONTUAL_COCKPIT_20261031.json).
O hash do escopo pode coincidir com leituras anteriores de conteúdo idêntico; o UUID
novo identifica autorização independente. Não reutilizar autorizações consumidas.
A validade só será concedida quando houver aprovação explícita. O fluxo existente
aplica expires_at na aprovação; o início proposto será observado no procedimento
administrativo. Se a janela expirar, não renovar ou deslocar datas automaticamente.

Após autorização expressa desse UUID/hash/janela:

1. GET `/api/v1/shopee-collection/identity-reads/b08a1923-b111-4171-84bf-1b3fb34731e5` para conferir estado.
2. POST `/api/v1/shopee-collection/identity-reads/b08a1923-b111-4171-84bf-1b3fb34731e5/approve`, autenticado
   pela chave administrativa local X-API-Key, sem divulgar seu valor:

```json
{
  "scope_sha256": "8b115275bfe09b6094e1e00ed4dca92b5f0e794a5cee13426e6fd7f0efd72f18",
  "actor": "eduardo.viana.3",
  "confirmed_single_read": true,
  "expires_at": "2026-10-31T23:26:28.146857+00:00"
}
```

3. Somente dentro da janela, POST
   `/api/v1/shopee-collection/identity-reads/b08a1923-b111-4171-84bf-1b3fb34731e5/execute` com o mesmo payload,
   removendo expires_at. No máximo um POST Shopee, sem retry/fallback/paginação.
4. Guardar a primeira resposta sanitizada. Sem gravar histórico, links, ofertas
   ou Telegram nesta autorização pontual. Erro/incerteza encerra a tentativa.

Não há aprovação concedida nesta preparação. Nenhum bloqueio local encontrado;
quotas oficiais e permissões atuais continuam desconhecidas. O piloto de amanhã
`de36e12d-5581-41ea-9137-0a4d54556fb7` permanece APPROVED, used=0, hash e janela
inalterados. Todas as autorizações anteriores foram preservadas.

## Menor evolução para comparar lojas — proposta, não implementada

1. Usar discover_offers por nome, com consulta/página/limite e autorização próprios.
   Não transformar a autorização por identidade em autorização de busca.
2. Deduplicar por shopId:itemId e agrupar vendedores por shopId, sem inferir que
   várias contas correspondem ao mesmo vendedor jurídico ou logístico.
3. Reutilizar filtros e relevance para rejeitar acessórios, kits diferentes,
   peças avulsas e modelos incompatíveis. Similaridade de título/imagem é somente
   candidatura: não prova equivalência, nem mesmo com preço parecido.
4. Formar um grupo de equivalência apoiado em evidências de marca/modelo/configuração,
   conteúdo do kit, condição e variante; GTIN/MPN somente se disponíveis por fonte
   autorizada. Quando os campos faltarem, usar confirmação administrativa documentada
   ou classificar como NÃO COMPROVADO, excluindo da indicação de melhor compra.
5. Comparar cotações do mesmo grupo/contexto de pagamento/variante/região em BRL
   Decimal. Ordenar preços anunciados válidos; faixas ambíguas ficam em grupo separado,
   sem usar priceMin como preço final. Frete/cupom desconhecidos permanecem explícitos.
   Produtos sem cupom continuam candidatos.
6. Histórico permanece por anúncio. Não relaxar PriceComparisonService para usar
   preço de outra loja como queda histórica: comparação transversal será uma visão
   dos candidatos existentes, separada da variação temporal de cada anúncio.
7. ratingStar/shopName/sales/shopType podem ser selecionados e validados no perfil
   futuro de descoberta, com origem/horário. Não alterar _PRODUCT_FIELDS global
   agora: isso modificaria o payload e invalidaria o hash do piloto já aprovado.
8. Sem prova de equivalência e condições aplicáveis, apresentar apenas uma lista
   de cotações/candidatos; não declarar melhor oportunidade real nem aprovar oferta.

## Próximo teste multiloja mínimo — exige autorização nova

Primeiro validar offline um conjunto de dois vendedores equivalentes e um anúncio
incompatível; testar empate, Decimal, faixa, kit/variante diferente e notas ausentes
ou inválidas. Reutilizar modelos/evidências e filtros; não criar outro histórico.

Depois, propor autorização própria para **um POST productOfferV2 keyword="Cockpit
simulador Brave Bull", page=1, limit=3**, sem paginação extra, fallback ou geração
de links. O retorno pode não conter duas lojas ou produtos equivalentes; nesse caso
registrar insuficiência de resultados, sem buscar novamente automaticamente.
Quotas/permissões precisam ser explicitamente consideradas e aceitas no escopo.
Essa busca não está autorizada nem preparada como despacho nesta execução.

## Preservação

Produto e históricos anteriores preservados por hashes; somente uma autorização
PREPARED e a auditoria 24 foram acrescentadas. .env/credenciais preservados, flags
false, scheduler desativado e grupo pausado. Zero HTTP real, mutations, links,
ofertas, aprovações comerciais, outbox ou publicações. Sem commit ou push.
