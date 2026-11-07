# Elegibilidade proporcional — 02/11/2026

O cadastro continua em `POST /api/v1/manual-offers`, com `documented_identity`.
Nenhum candidato é promovido automaticamente a uma oferta verificada.

- `identity_level=VARIANT` (padrão compatível): mantém identificador da opção e
  `ProductFacts` documentados. Não exige comparação entre lojas, cupom ou queda histórica.
- `identity_level=LISTING_ALL_OPTIONS`: permite `variant_id=""` e `facts=null`
  somente com documento de cobertura de **todas as opções selecionáveis**, preço
  público idêntico e disponibilidade de todas elas. Não afirma inexistência de variantes.
  Requer `confirmed_all_options_same_public_terms=true`,
  `confirmed_all_options_available=true`, `all_options_public_price` e
  `publication_name_sha256=fingerprint({"name": nome_cadastrado})`, além dos campos
  existentes de origem, referência, hash, responsável, data e confirmação do vínculo.
  Data documental deve coincidir com a verificação comercial e preço com a oferta.
  Cupom e preço anterior não são aceitos nessa modalidade conservadora.
- `documented_observation_names`: títulos alternativos explicitamente vinculados
  pela documentação. Coincidência textual não é prova; nomes não documentados ou
  conflitantes continuam bloqueados. Alterar o nome publicado invalida o documento
  da modalidade sem SKU.

Ambas exigem link próprio operacionalmente verificado, identidade/URL coerentes,
verificação comercial explícita, preço PUBLIC e disponibilidade AVAILABLE. No piloto,
a validade permanece em 600 segundos. Observações antigas não são atualizadas nem
usadas como atestado atual. Sem fatos físicos completos, deduplicação usa identidade
do anúncio; nunca cria equivalência entre vendedores com atributos desconhecidos.

O contrato local `CONTRATO_FONTES_SHOPEE_20261026.json` e o registro
`app/services/shopee_evidence_sources.py` sustentam preparação de identidade, título,
preço anunciado e campos selecionados do vendedor por `productOfferV2`. Não sustentam
sozinhos SKU, cobertura de opções, preço público aplicável, estoque ou titularidade.
Exportações oficiais e observações documentadas fornecidas pelo responsável podem
completar a evidência. Feeds constam do contrato, mas permissões e semântica das colunas
não foram confirmadas nem autorizadas para consulta. Nenhuma nova fonte foi acessada.

Resultado: 116 testes offline (MockTransport e regressões de ofertas/pilotos),
19 PostgreSQL isolados e Ruff aprovados. Os testes usam documentos fictícios apenas
em bancos isolados. A fila local possui cinco candidatos reais; zero ofertas e zero
links associados no operacional, portanto **zero elegíveis**, mesmo após os ajustes.

Plano `d80879af-27e3-47fc-b47b-01e3499a3006` e hash
`fc942c3b9f2e0a28ea63f75762aadbe8f6580d2700bc50977e5944d0bbe6b387`
preservados em PREPARED, janela 03/11/2026 09h–11h Brasília. Regras assinadas,
cadência, reservas, orçamento, prazo, timeout e controles de publicação não mudaram.
Pode operar tecnicamente após autorização específica e início manual na janela,
mas só enviará ofertas efetivamente verificadas e vigentes. Não há garantia comercial
de mensagens: sem evidências suficientes publicará zero. Nenhuma chamada externa,
geração de link, ativação de destino/scheduler ou gravação operacional nesta alteração.
