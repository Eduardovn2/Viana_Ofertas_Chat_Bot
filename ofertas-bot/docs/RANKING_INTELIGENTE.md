# Ranking determinístico de oportunidades — OPPORTUNITY_100_V1

O ranking não altera `Offer.score`, verificações comerciais, permissões, limites
de pilotos ou aprovação de publicações. Não cria ofertas, links ou históricos.

## Fórmulas iniciais

Pesos configuráveis pelo modelo `RankingPolicy`: preço 35, avaliações 20,
popularidade 15, tendência 15, vendedor 15. A soma deve ser 100.

- Preço: `35 × clamp(0,5 + percentual_abaixo_da_referência / 40, 0, 1)`.
  Na mediana, 17,5 pontos; 20% abaixo, 35; 20% acima, zero. Diferença de pelo
  menos 50% abaixo exige revisão e retorna UNKNOWN, sem prêmio por possível erro.
  A referência transversal exige grupo comprovado, mesma captura e pelo menos
  duas lojas; o radar temporal exige o mesmo anúncio, variante/conteúdo documentados
  e contexto de preço. As referências permanecem identificadas separadamente.
- Avaliações: `20 × (nota − 1) / 4 × min(n / 100, 1)` para notas entre 1 e 5.
  Sem quantidade documentada, PARTIAL com score null. Amostras abaixo de 100
  permanecem PARTIAL; não há contagem inventada.
- Popularidade: `15 × min(vendas_documentadas / 1000, 1)`. O indicador distingue
  vendas acumuladas de vendas em janela; esta última exige período explícito.
  Também aceita posição em lista oficial: `15 × (1 − (posição − 1)/(tamanho − 1))`,
  com denominador mínimo 1 e tamanho da lista obrigatório. Não mede buscas recentes.
- Tendência: no mínimo três observações comparáveis em pelo menos 24 horas.
  Vendas acumuladas usam crescimento percentual da taxa no último intervalo
  contra a taxa no primeiro, corrigidas pelas durações. A pontuação é
  `15 × clamp(crescimento / 100, 0, 1)`. Contador reiniciado ou taxa inicial zero
  resulta em UNKNOWN. Rankings oficiais usam a melhora percentual da posição.
- Vendedor: mesma redução por nota/amostra das avaliações, com peso 15 e
  identidade da loja. Avaliação do produto não é reputação do vendedor.

Preço anunciado precisa estar vigente em até 600 segundos. Métricas documentais
usam, inicialmente, validade de 24 horas para a última observação. Parâmetros são
experimentais; não garantem qualidade, desconto ou retorno comercial.

## Evidências e cobertura

`MetricEvidence` exige sujeito, métrica, origem HTTPS oficial suportada, referência,
hash do documento, ator, data e confirmação explícita da semântica. Séries mantêm
identidade, fonte, definição e período consistentes. Não há consulta automática
para confirmar documentos, nem preenchimento de confirmações pelo coletor.

O contrato local completo confirma `ratingStar` como avaliação do produto e
`sales` como contagem de vendas, sem escala/amostra/período suficientes para pontuar.
Esses campos alimentam indicadores PARTIAL com score null; `shopType` não comprova
reputação. Ver [abastecimento por dados reais](ABASTECIMENTO_INDICADORES_REAIS.md).
O adaptador pode receber `documented_ranking_signals` de fontes documentadas;
campos desconhecidos ou sem vínculo resultam em UNKNOWN.

Cada dimensão informa score/null, máximo, VERIFIED/PARTIAL/UNKNOWN, evidência e
motivo. Pontos conhecidos são a soma dos scores disponíveis. Máximo possível soma
a esses pontos o restante das dimensões desconhecidas/parciais. A cobertura pondera
VERIFIED por 100%, PARTIAL por 50%, UNKNOWN por zero. Não é intervalo estatístico.

As faixas exigem cobertura de pelo menos 80%, quatro dimensões VERIFIED e preço
VERIFIED. Usam o limite inferior conhecido: 85 excepcional, 70 forte, 55 interessante,
abaixo disso monitoramento. Sem cobertura suficiente: INSUFFICIENT_DATA. Pontuação
definitiva só é exibida com cobertura completa.

## Integração e API

- AUTO_PILOT: `opportunities.analysis_queue[].intelligent_ranking`. A atualização
  local continua após o orçamento de consultas. Estados e bloqueios comerciais
  permanecem separados. Nenhum score torna o candidato automaticamente elegível.
- GET autenticado `/api/v1/auto-pilots-real/{uuid}/ranking?sort_by=score`.
  Outras ordens: `price`, `coverage`, `opportunity`. Empates usam identidade e captura.
- POST autenticado `/{uuid}/ranking/preview` recebe ator, confirmação explícita de
  fontes documentadas e `signals_by_identity`. É prévia sem persistência, sem
  confirmação comercial e sem publicação; não modifica pilotos antigos.
- Radar existente: `PriceComparisonService.rank_observation` e GET autenticado
  `/api/v1/products/{id}/ranking`. Leitura do histórico existente,
  sem substituir o preço atual por valores antigos ou gravar novos preços.

Próximo abastecimento real depende de fontes oficiais com semântica documentada e
de evidências vigentes de identidade, preço público, disponibilidade e vínculo
próprio de afiliado. Importações oficiais continuam pelo serviço já existente;
uma prévia de ranking não confirma essas condições nem autoriza novas consultas.
