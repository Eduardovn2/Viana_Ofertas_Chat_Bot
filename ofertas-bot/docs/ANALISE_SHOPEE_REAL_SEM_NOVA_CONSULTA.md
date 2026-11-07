# Ranking de uma resposta real sem nova consulta

A reanálise reutiliza os filtros, a ordenação por loja e o resultado do comparador
multiloja. Aceita a estrutura sanitizada gravada anteriormente e não reconstrói
uma resposta bruta da Shopee nem preenche atributos de produto ausentes.

## Contrato e níveis de evidência

O contrato local do Explorer, salvo em `CONTRATO_FONTES_SHOPEE_20261026.json`, fornece
`shopId`, `itemId`, `productName`, preços anunciados e dados como `shopName`,
`ratingStar`, `sales` e `shopType`. Não fornece marca, modelo, quantidade, kit,
variante, embalagem, condição ou condições comerciais em campos estruturados
suficientes para provar equivalência. O esquema autenticado não foi consultado
novamente nesta etapa.

- `reported_fields`: valores observados, já normalizados pelo conector. Identidade,
  preço anunciado e nome da loja não são confirmação de características do produto.
- `attributes` e `attribute_evidence_level`: fatos completos continuam exigindo
  evidência documentada vinculada à cotação no comparador existente.
- `inferred_attributes`: menções em `productName`, acompanhadas de trecho, regra,
  campo de origem e `TITLE_INFERENCE_NOT_PROOF`.
- `unknown_attributes`: permanecem desconhecidos para fins de equivalência, mesmo
  quando existe uma hipótese pelo título.

A identificação de marca usa um vocabulário limitado de nomes do domínio de
simuladores e, opcionalmente, o nome solicitado pelo usuário. Uma marca mencionada
em “compatível com” não é promovida a fabricante. Tokens alfanuméricos são candidatos
a modelo; `RX-500` e `RX-500+` permanecem distintos. Contagens exigem menção explícita,
sem assumir uma unidade. Cores/capacidades são menções de opção ou especificação,
sem confirmar a variante selecionada. “Volante”, “pedais” e “banco” mencionados no
título não comprovam conteúdo incluído no kit.

`sales` foi corrigido para o intervalo não negativo de `Int64`, conforme o contrato;
bool, float, string e overflow continuam recusados. `ratingStar` mantém sua semântica
não confirmada e não se transforma em reputação da loja. Zero não significa
automaticamente ausência de avaliações. `shopType` não atribui selo de qualidade.

## Ordenação informativa

`candidate_price_order` ordena preços anunciados válidos, deduplica por `shopId`,
preserva empates entre lojas e informa `same_shop_listing_count` e
`same_price_identities`. `all_listing_price_order` preserva os anúncios individuais.
Nenhuma diferença monetária entre candidatos inconclusivos vira desconto histórico
ou recomendação de compra.

O menor preço inconclusivo recebe aviso próprio, inclusive quando empata com um
anúncio documentado. Usos diferentes mencionados nos títulos também geram aviso
de revisão. Esses avisos não são provas de incompatibilidade.

## Resultado do registro anterior

Sem atributos preenchidos manualmente, os três anúncios gravados produziram:

| Ordem por preço | Loja | shopId | Preço anunciado | Anúncios da loja |
|---|---|---|---|---|
| 1 | 3L LOJA——Brave Bull | 1595990092 | R$ 639,00 | 1 |
| 2 | Brave Bull Oficial | 1754100018 | R$ 1.399,00 | 2, empatados |

O resultado continua `INSUFFICIENT_EQUIVALENCE`: não há grupo equivalente comprovado.
O título de R$ 639,00 menciona simulador de voo; outro menciona corrida. Marca é
apenas indício textual, e modelo/quantidade/variante permanecem sem comprovação.

A cotação normalizada foi validada pelo conector na consulta original. O relatório
sanitizado não preservou `priceMin`, `priceMax` ou o fingerprint bruto dessa primeira
coleta. A reanálise não inventa esses campos: declara
`raw_price_range_revalidated=false`, utiliza a avaliação de preço anteriormente
gravada e não produz nova evidência de equivalência. Campos de preço sem avaliação
válida registrada não participam do ranking.

## Uso

Somente arquivo local, sem credenciais, banco ou HTTP:

```text
python -m app.analyze_shopee_search --input docs/RESULTADO_BUSCA_MULTILOJA_20261031_2210.json --output caminho/novo-relatorio.json
```

O arquivo de saída deve ser novo. O registro de entrada permanece intacto.

Para resultado já persistido, a rota administrativa autenticada é:

```text
POST /api/v1/shopee-discovery/multistore-searches/{authorization_id}/analyze
{}
```

Ela lê apenas uma consulta `SUCCEEDED` com resultado sanitizado disponível. Pode
reanalisar uma autorização consumida, sem aprová-la, reservá-la ou executá-la de
novo. Um alvo opcional orienta as menções reconhecidas, sem preencher fatos ausentes.

Em ambos os caminhos, publicação permanece bloqueada, histórico temporal não é
escrito, e preço final, estoque, frete, elegibilidade e reputação continuam sem
confirmação comercial.
