# Comparação por marca, modelo e quantidade

O serviço de descoberta aceita um `ProductTarget` com marca, modelo e quantidade
obrigatórios. Condição, embalagem, conteúdo do kit, variante e condições comerciais
são opcionais no pedido. Campos omitidos permanecem desconhecidos; não são copiados
para os fatos de um anúncio.

## Uso

Rota administrativa autenticada, exclusivamente para ambiente isolado com MockTransport:

```text
POST /api/v1/shopee-discovery/offline-product-search
```

```json
{
  "product": {"brand": "Orion", "model": "RX-500", "quantity": 1},
  "limit": 20
}
```

A busca é construída com marca e modelo; quantidade é validada pela evidência de
cada anúncio. A rota reutiliza `ShopeeConnector.discover_offers`, o perfil multiloja,
`DiscoveryOperations` e os filtros existentes. O limite de 1 a 100 deve caber nas
preferências locais. É consultada uma única página mockada, sem retry ou paginação.
O limite configurável é exclusivo do laboratório: autorizações reais continuam
vinculadas ao payload original, inclusive página 1 e limite 3.

`ShopeeDiscoveryService.review_defined_product(page, product, evidence=...)` permite
revisar a página capturada sem outro HTTP. As evidências documentadas são vinculadas
ao fingerprint retornado em `quote_fingerprint`. Uma divergência de título ou preço
invalida a evidência correspondente. Evidência incompleta não é preenchida pelo alvo.

## Regras de comparação

`ProductFacts` continua exigindo todos os atributos: marca, modelo, quantidade,
condição, embalagem, kit, variante e condições de preço. Os títulos estabelecem
relevância; não comprovam equivalência. Caracteres como `+` permanecem significativos
no modelo. Conflitos documentados com o alvo são apresentados em
`mismatched_attributes`, inclusive quando o título está incompleto.

Somente conjuntos completos e iguais de fatos formam um grupo. Quando o alvo não
especifica kit ou variante, conjuntos documentados diferentes formam grupos
separados. Produtos incompatíveis e casos inconclusivos têm listas próprias.

O ranking retém o menor preço comparável por `shopId`, preserva empates e calcula
diferença absoluta e percentual sobre o menor preço anunciado. A amplitude de preços
é transversal entre lojas; não é desconto histórico. Um grupo com apenas uma loja
não declara vencedor. Faixas ambíguas, moedas diferentes de BRL, preços inválidos
e identidades duplicadas não são admitidos no ranking.

`candidate_price_order` é apenas ordenação informativa: pode conter produtos ainda
sem equivalência comprovada e não fundamenta uma recomendação de compra.

`shopName`, `ratingStar`, `sales` e `shopType` mantêm a validação do conector.
Avaliação ausente permanece ausente; nota, vendas e códigos de loja não são
transformados em reputação, estoque ou selo de qualidade. Preço final, frete e
disponibilidade não são confirmados pelo ranking.

## Demonstração reproduzível

```text
python -m app.demo_shopee_multistore
python -m app.demo_shopee_multistore --output caminho/resultado-novo.json
```

O catálogo `examples/multistore_defined_product.json` contém somente dados
fictícios e documentos sintéticos explícitos. A demonstração usa credenciais
fictícias, SQLite temporário e um POST em MockTransport; não lê `.env` e recusa
transporte HTTP real. O arquivo de saída, quando solicitado, precisa ser novo.

Entre os 12 anúncios, o grupo equivalente contém três lojas:

| Posição | Loja fictícia | Preço anunciado | Diferença do menor |
|---|---|---|---|
| 1 | 102 | R$ 1.099,90 | R$ 0,00 |
| 1 | 103 | R$ 1.099,90 | R$ 0,00 |
| 3 | 101 | R$ 1.299,90 | R$ 200,00 (18,18%) |

Seis anúncios são rejeitados e dois ficam inconclusivos; um anúncio adicional da
loja 102 é deduplicado no ranking. Nenhuma diferença é registrada como queda
histórica. Nenhum produto, histórico, autorização, link ou publicação é criado.

## Limites para dados reais

A consulta `productOfferV2` disponível não fornece atributos estruturados suficientes
para provar modelo, kit, quantidade e variante automaticamente. Uma resposta real
continua candidata até existir evidência adequada. Este desenvolvimento não executa
consultas reais nem altera o escopo, hash, janela ou estado das autorizações existentes.
Uma nova pesquisa real exige autorização específica vigente; publicação e links de
afiliado seguem seus próprios controles.

## Conteúdo por unidade, versões e classificação automática

O alvo e a evidência aceitam `net_content`, por exemplo
`{"amount":"250","unit":"g"}`, e `edition`, por exemplo `standard` ou `premium`.
O conteúdo é por unidade comercial, separado da contagem inteira `quantity`.
Conversões exatas normalizam mg/g/kg em gramas e ml/l em mililitros. Não há
conversão entre massa e volume, nem multiplicação automática de kits para declarar
equivalência: duas unidades de 250 g não viram uma unidade de 500 g.

As pistas textuais agora reconhecem medidas, contagens de multipack, dose, edição
e contextos de acessórios. Dose de 3 g não é tratada como conteúdo de um pote.
Ausência da palavra premium não comprova versão comum. Divergências documentadas
de conteúdo/edição separam produtos; diferenças apenas nos títulos continuam
inconclusivas. Conflitos entre título e documento suspendem o agrupamento para
revisão. Creatina sem conteúdo e edição documentados não recebe equivalência comprovada,
mesmo quando os demais campos coincidem.

O resultado inclui:

- `proven_comparisons` e `lowest_price_highlights`: grupos comprovados com duas ou
  mais lojas, únicos que recebem destaque de menor preço anunciado.
- `proven_incompatible_products`: incompatibilidades documentadas em relação ao alvo.
- `unproven_candidates`: seção inconclusiva, sem posição ou diferença para o menor.
- `filtered_candidates`: filtros de relevância/acessório, separados de comprovação.
- `stores`: todos os anúncios e alternativas de cada `shopId`; nomes de loja
  divergentes são preservados em vez de eleger uma identidade comercial.
- `pairwise_assessments`: comparação entre lojas com classificações
  `PROVEN_EQUIVALENT`, `PROVEN_INCOMPATIBLE` ou `INCONCLUSIVE`. Diferenças de título
  têm nível de evidência próprio. A saída é limitada a 500 pares, com indicador
  explícito de truncamento, sem perder anúncios ou alternativas por loja.

As chaves antigas de candidatos e filtros continuam disponíveis por compatibilidade;
não devem ser usadas como destaque de melhor compra. Incompatibilidade comercial
(por exemplo, condições de preço diferentes) é identificada com escopo próprio.

Uma demonstração adicional usa somente creatinas fictícias:

```text
python -m app.demo_shopee_multistore --catalog creatine
```

O catálogo `examples/multistore_creatine.json` testa 250 g, 0,25 kg, 300 g, comum,
premium, dosador, dose e kit. Seus preços e documentos são sintéticos; não representam
ofertas reais das marcas mencionadas. Os dados reais sanitizados anteriores continuam
sem grupo equivalente comprovado e sem destaque de menor preço.
