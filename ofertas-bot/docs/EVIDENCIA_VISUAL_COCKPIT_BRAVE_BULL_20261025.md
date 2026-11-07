# Evidência visual — Cockpit Brave Bull — 25/10/2026, 21h11

**Diagnóstico: BLOCKED.** Registro documental da primeira captura comercial fornecida pelo responsável, sem inserção de observação no histórico operacional, sem validação de condições efetivas e sem aprovação de oferta.

[Captura original preservada](evidencias/cockpit_brave_bull/2026-10-25_2111_brt.png) · [Registro estruturado](EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.json).

## Origem e integridade

- Fonte: captura da página Shopee fornecida pelo responsável, inspecionada visualmente, sem abrir o site.
- Produto informado: **Cockpit simulador de corrida Brave Bull**, shopId **1904573791**, itemId **23199869203**. Nome e IDs visíveis na captura são consistentes com a referência fornecida; nenhuma consulta ao servidor foi realizada.
- Data/hora visíveis no relógio Windows: **25/10/2026, 21h11**. Fuso Brasília/America/Sao_Paulo informado pelo responsável, precisão de minuto. A imagem não autentica o relógio, o fuso ou o instante de captura; esse horário não foi registrado como verified_at.
- Arquivo PNG original copiado sem edição; **1919 × 1079**, **575.360 bytes**. SHA256: `c2f854f2140211c0fa04380675ac0df98e52a3d1fe7738d3cd6df992556a2247`.
- O hash comprova igualdade dos bytes recebidos/preservados, sem atestar autenticidade da página ou condições comerciais atuais. Nenhum parâmetro de rastreamento da barra de endereço foi transcrito para este relatório.

## Campos exibidos, condicionais e pendentes

| Campo | O que a captura documenta | Limite da evidência |
|---|---|---|
| Preço no Pix | **R$ 956,13**, texto “no Pix com cupom” | Preço exibido e condicionado; aplicação/eligibilidade do cupom e preço final não validados |
| Outros pagamentos | **R$ 1.099,00**, texto “sem cupom em outros métodos de pagamento” | Não comprova métodos específicos, parcelamento ou preço efetivamente aplicável à variante |
| Cupom | Anúncio **R$ 40 OFF** | Sem código, validade, mínimo, regras ou confirmação de aplicação; não cadastrado como cupom confirmado |
| Frete | Exibição **R$ 0,00**, texto “Frete grátis com cupom” | Condicionado a cupom; não comprova custo aplicável, CEP ou gratuidade universal |
| Localidade | Responsável informou São Paulo/SP; tela mostra São Paulo, São Paulo sob “Enviado De” | Esse rótulo não comprova o endereço de entrega. CEP/destino e cálculo final pendentes |
| Cores | Opções **vermelho** e **azul** visíveis; igualdade do preço exibido declarada separadamente pelo responsável | Nenhuma seleção de variante/SKU ou disponibilidade confirmada; imagem azul não comprova seleção |
| Disponibilidade | Não comprovada | availability=UNKNOWN; quantidade/botões visíveis não comprovam estoque |
| Histórico | Uma evidência visual documental, sem série temporal comparável | Nenhuma queda, desconto histórico ou preço anterior comprovado |
| Link/conta/comissão | A captura não fornece prova | Não altera a evidência histórica do link, a proposta de confiança ou account_verified |

Os valores monetários foram registrados como textos decimais exatos (`956.13`, `1099.00`, `40.00`, `0.00`), sem aritmética binária. Os dois preços pertencem a contextos distintos de pagamento/cupom e não foram comparados como histórico. Não se subtraiu o cupom dos preços, nem se calculou desconto/percentual ou comissão. Não se presumiu que o cupom anunciado produz exatamente o preço Pix exibido.

## Continuidade — declaração do responsável sobre as cores

**Origem: DECLARACAO_DO_RESPONSAVEL**, independente da captura e de qualquer resposta de API. Declaração recebida nesta continuidade: “As cores azul e vermelha do Cockpit Brave Bull apresentam o mesmo preço exibido.” Referência no JSON: `declaracao-responsavel-mesmo-preco-exibido-azul-vermelho`.

A igualdade do preço exibido entre as cores está documentada segundo o responsável e não precisa ser perguntada novamente. Os valores **R$ 956,13 no Pix com cupom** e **R$ 1.099,00 sem cupom em outros métodos** e suas condições permanecem inalterados. A declaração não é confirmação de preço final, aplicação de cupom, seleção de cor, ID/SKU de variante, estoque, titularidade ou comissão. Não foi atribuído a ela o horário da captura; o JSON distingue o horário de registro da declaração de um horário de verificação não fornecido.

Não houve promoção a atestado comercial completo: `availability=UNKNOWN`, IDs/seleção de variante desconhecidos, `account_verified=false` e `publication_authorized=false`. O resultado continua **BLOCKED**, sem gravação operacional.

## Estado e pendências

- **Preço exibido documentado**, preço público/efetivamente aplicável **não validado**.
- Variante/cor efetivamente selecionada, estoque e condições de pagamento/cupom permanecem **pendentes**.
- Cupom, frete, parcelamento e preço anterior não são obrigatórios se omitidos de uma futura mensagem; qualquer condição usada para justificar o preço anunciado precisa de confirmação suficiente.
- A primeira observação futura válida não poderá ser anunciada como queda histórica. Não foi criada uma observação INITIAL artificial para representar a captura.
- A política de confiança da conta/link continua apenas proposta; nenhuma confirmação humana adicional de titularidade foi inferida.
- **account_verified=false; publication_authorized=false; prévia, oferta e outbox não criadas.**

Próximo passo documental: obter confirmação explícita e referenciada da variante, disponibilidade e preço aplicável com suas condições reais, incluindo cupom/frete somente se forem utilizados. Não há autorização atual para gravar produto/observação/atestado no banco operacional. Antes de qualquer registro operacional, obter a autorização adicional solicitada pelo responsável; antes de qualquer uso do link, concluir sua política de confiança e comprovação de vínculo.

## Checklist mínimo para a primeira observação operacional

- [ ] **Responsável:** autorizar explicitamente o cadastro do produto/observação no banco operacional. Nome e shopId/itemId já estão documentados; não precisam ser fornecidos novamente.
- [ ] **Responsável:** definir o escopo da observação (ambas as cores ou uma cor), o preço/contexto a registrar e a data/hora da verificação. Igualdade do preço exibido já declarada não significa seleção de uma variante nem preço efetivamente validado.
- [ ] **Página/checkout oficial, com evidência fornecida pelo responsável:** comprovar o preço aplicável àquele escopo e suas condições de pagamento. Para usar o preço Pix com cupom, documentar regras, elegibilidade e aplicação; não calcular a aplicação a partir do anúncio R$ 40 OFF. Se não houver essa confirmação, manter o valor como exibido/condicional, sem promovê-lo a preço validado.
- [ ] **Página oficial, com confirmação documentada pelo responsável:** verificar disponibilidade das cores abrangidas. ID de variante/modelo só poderá ser informado se existir identificação oficial confiável e for exigido pelo fluxo; não criar IDs ou usar nome de cor como SKU presumido. Sem confirmação suficiente, manter a candidatura BLOCKED.
- [ ] **Checkout/regra oficial, somente se usado na oferta:** comprovar frete para o destino/CEP e cupom aplicável. Do contrário, conservar como desconhecido/condicional e não prometer frete grátis. Parcelamento, cupom e frete não são requisitos universais para uma observação sem essas alegações.

**Antes de avançar a oferta/prévia:** concluir a documentação da conta/beneficiário e a decisão administrativa da política de confiança do link. Essa é uma pendência de uso comercial do link, não uma exigência de venda anterior ou comissão para registrar uma observação de preço. Histórico comparável também não é requisito para a primeira observação: ela não comprova queda. Publicação permanece sem autorização.

As confirmações manuais podem ser fornecidas pelo responsável com referência e horário; as condições da loja, eventuais códigos/regras e identificadores devem vir da fonte oficial, sem exigir uma nova consulta da API pelo Codex nesta etapa. Nenhuma dessas pendências autoriza chamada externa ou alteração operacional automaticamente.

## Preservação e verificações

A captura foi preservada fora do banco, com SHA256 e registro estruturado. `.env`, credenciais, artefatos da mutation e os relatórios anteriores do piloto permaneceram com hashes idênticos. Nenhum banco foi acessado nesta execução; nenhum histórico de outro produto foi alterado. Não houve mudança de aplicação, migrations ou testes.

Verificações locais: hash da cópia versus fonte, leitura do JSON, valores Decimal, distinção de condições e invariantes BLOCKED/não aprovado. Suítes Pytest/PostgreSQL não foram repetidas para esta alteração documental; os 80 PostgreSQL aprovados pertencem à execução anterior do piloto.

**Zero chamadas Shopee, mutations ou publicações Telegram.** Nenhum scheduler, integração, envio, commit ou push ativado/executado. Sem uso da imagem como prova de titularidade, atribuição ou comissão.

Continuidade documental: apenas a declaração limitada de equivalência entre cores e o checklist foram acrescentados ao JSON/relatório. Captura, preços/condições, histórico e bloqueios foram preservados. Conferência local de JSON/invariantes; nenhuma auditoria completa, suíte de testes ou chamada externa repetida.
