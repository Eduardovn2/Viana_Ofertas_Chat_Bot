# Comparação multiloja Shopee — implementação offline, 31/10/2026

## Implementação

ShopeeConnector.discover_offers ganhou profile="multistore", que usa a consulta
existente por keyword e seleciona adicionalmente shopName/ratingStar/sales/shopType.
O perfil standard e product_query permanecem idênticos; o escopo aprovado de amanhã
foi recalculado e conferiu com o hash original. Nenhuma autorização anterior mudou.
O perfil multistore desativa cache e retry; não percorre páginas automaticamente.

ShopeeMultistoreService.discover_offline exige banco isolado e MockTransport,
reutiliza as preferências DiscoveryOperations e consulta page=1/limit=3. A comparação
pura compare_offers reutiliza relevance/SearchTerm, valida Decimal BRL e não escreve
histórico. ProductFacts/EquivalenceEvidence são estruturas em memória, sem nova
migração, tabela de produtos ou pipeline histórico.

## Evidência de equivalência

Cada evidência precisa informar explicitamente marca, modelo, quantidade, condição,
conteúdo do kit, variante e condições de preço, além de documento, responsável,
horário e fingerprint da cotação. A origem aceita é MANUAL_DOCUMENTED. Não se
converte observação da API em atestado humano automaticamente.

A API selecionada não fornece atributos estruturados suficientes para comprovar
marca/modelo/kit/variante. O título estabelece relevância, não equivalência.
Os atributos são identificados nas evidências documentadas fornecidas; não são
inventados ou promovidos a fatos por heurística de título. Campos desconhecidos ou
evidência ausente/incompleta mantêm o item como candidato, sem vencedor.

Exclusões configuráveis reutilizam o filtro existente para acessórios/peças;
atributos documentados incompatíveis com o alvo rejeitam marca/modelo/kit/quantidade,
condição/variante diferentes. Título incompleto fica candidato para revisão.
Cotação de outro fingerprint ou evidência anterior à coleta não comprova o item.

Grupos exigem atributos e condições equivalentes. O ranking deduplica por shopId
retendo o anúncio mais barato comparável de cada loja, preserva empates e calcula
diferença absoluta em centavos. Um grupo precisa de ao menos duas lojas para indicar
os menores preços anunciados. Sem isso, o resultado é insuficiente.

A comparação transversal exige o mesmo horário de coleta. PriceComparisonService
não foi relaxado: histórico continua por anúncio/contexto e preço de outra loja
não se torna queda histórica. Produtos sem cupom participam normalmente.

Preço anunciado não é preço final confirmado. Acessibilidade pública, estoque,
frete, cupom aplicável e condição comercial não ganham confirmação por ranking;
nenhuma oferta, aprovação ou outbox é criada.

## Dados das lojas

| Campo selecionado | Validação e interpretação |
|---|---|
| shopId | Identificador de loja extraído da identidade validada; não prova identidade jurídica do vendedor |
| shopName | Texto opcional, tamanho limitado e sem controles; ausência continua None |
| ratingStar | String numérica finita não negativa; valor bruto preservado; escala/semântica não confirmadas |
| sales | Inteiro não negativo, sem aceitar bool; período e significado não confirmados |
| shopType | Lista limitada de códigos inteiros não negativos; sem atribuir rótulos não documentados |

ratingStar não é convertido em reputação da loja. sales não é convertido em nota,
estoque ou vendas em um período inventado. Códigos de shopType não dão selo ou
qualidade presumidos. A documentação usada é o contrato local do Explorer; não
houve nova verificação externa. Dados ausentes permanecem desconhecidos.

## Exemplo estritamente fictício

Mesma marca/modelo/quantidade/kit/condição/variante e condições de preço foram
atestados apenas na fixture sintética. Nenhum desses valores é cotação real.

| Posição | Loja fictícia | Preço anunciado | Diferença do menor |
|---|---|---|---|
| 1 | 102 | R$ 1.000,00 | R$ 0,00 |
| 2 | 101 | R$ 1.100,00 | R$ 100,00 |

A loja 103 anunciou uma capa fictícia a R$ 500,00; o filtro a rejeitou.
Não se declara melhor compra real ou desconto histórico.
[Demo completa](DEMO_MULTILOJA_SHOPEE_20261031.json).

## Testes executados

216 testes distintos aprovados: 205 offline e 11 PostgreSQL isolados em schemas UUID
de ofertas_bot_test. Rodadas: 204 regressões offline, 26 testes finais multiloja,
10 PostgreSQL iniciais, 11 finais e uma regressão adicional de título incompleto.
Não somar repetições como testes distintos. A suíte completa não foi executada.

Cobertura: dois vendedores equivalentes, acessório incompatível, deduplicação por
loja, empate, marca/modelo/kit/quantidade/condição/variante/condições diferentes,
variante desconhecida, avaliações ausentes/inválidas, preço inválido/faixa ambígua,
evidência ligada a outra cotação, separação temporal, perfil legado preservado,
idempotência PREPARED e bloqueio de despacho real.
Ruff e formatação passaram (198 arquivos); Alembic current/check passou no schema
isolado em 0009_shopee_discovery. Aviso preexistente de depreciação Starlette/httpx.
[Validação](VALIDACAO_MULTILOJA_SHOPEE_20261031.json).

## Primeira busca real — apenas PREPARED

- UUID: `bce2459b-a77a-4e30-86fa-30d8069cf4bf`.
- scope_sha256: `f64562375158a13c35fd8364d0f1d8035ccca8540a4a9b7a3c42bdd05302dd0a`.
- Auditoria de preparação: 25; aprovação ausente, consumed=false.
- productOfferV2 keyword="Cockpit simulador Brave Bull", page=1, limit=3,
  profile=multistore; máximo um POST, sem retry, fallback ou paginação adicional.
- Conta, endpoint e payload vinculados por hash. Sem histórico, links ou publicação.
- Quotas e permissões atuais permanecem não confirmadas.

[Plano e payload exatos](PLANO_BUSCA_MULTILOJA_SHOPEE_20261031.json).

A preparação não concede autorização nem inclui um dispatcher real novo. execute_real
permanece bloqueado. Após autorização específica do responsável, será necessária
uma entrada administrativa com validade curta, reserva e claim duráveis antes de
um HTTP, contador de uma tentativa e auditoria do resultado. Reutilizar os mecanismos
existentes de leitura/coleta; não chamar o conector diretamente fora dessas proteções.
Esse caminho real não foi implementado nem acionado nesta etapa offline.

Não utilizar identity-reads ou o piloto de seis horas para executar esta busca.
Se houver menos de duas lojas ou evidência insuficiente, apresentar os candidatos e
INSUFFICIENT_EQUIVALENCE, sem buscar novamente, fabricar atributos ou trocar produto.
Mesmo uma busca bem-sucedida não comprova automaticamente equivalência: a primeira
resposta provavelmente ainda exigirá evidências documentadas dos atributos.

## Preservação

Todas as tabelas operacionais e .env permaneceram idênticos durante os testes. A
preparação acrescentou apenas um AppSetting e a auditoria 25, preservando por hash
todas as linhas anteriores. Históricos 2/3/4, autorizações e credenciais preservados.
Piloto de amanhã segue APPROVED, used=0, mesma janela/hash/payload. Flags false,
scheduler desativado e grupo pausado. Zero chamadas externas, mutations, links,
ofertas ou publicações. Sem commit ou push.
