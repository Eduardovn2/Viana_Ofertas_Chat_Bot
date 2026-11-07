# Primeiro cadastro documental autorizado — Cockpit Brave Bull

**Cadastro operacional concluído; avaliação comercial BLOCKED.** Autorização limitada à criação/reutilização do produto e às duas observações documentais; sem consulta externa, link, oferta, aprovação ou publicação. Registro efetuado em **26/10/2026 00:08:51 -0300**, America/Sao_Paulo.

[Resultado estruturado](CADASTRO_DOCUMENTAL_COCKPIT_SHOPEE.json).

## Produto e operação

- PostgreSQL operacional **ofertas_bot**, conexão e identidade do banco confirmadas antes da gravação; revisão existente 0009, sem migration.
- Produto **ID 2**, criado: Cockpit simulador de corrida Brave Bull, plataforma SHOPEE, external_id **1904573791:23199869203** (shopId **1904573791**, itemId **23199869203**).
- Um único produto, variant_id **vazio** pelo padrão nativo do modelo: escopo não confirmado, nenhum modelId/variantId fabricado. Opções azul/vermelho e declaração de mesmo preço exibido preservadas como **DECLARACAO_DO_RESPONSAVEL** nos metadados documentais.
- **monitoring_enabled=false**, priority=false. Nenhuma categoria, destino ou política foi alterada. Não foram criados registros por cor.
- Única transação de gravação, com lock transacional por escopo e validações de entrada/identidade/preço existentes. SQL de escrita restrito por guarda a **um INSERT em products e dois INSERTs em product_price_history**, sem UPDATE/DELETE/DDL ou outras tabelas.

## Observações persistidas

| ID | Preço Decimal BRL | Contexto | price_access | availability | Comparação documental |
|---|---|---|---|---|---|
| 2 | R$ 1099.00 | no_coupon_other_methods | UNKNOWN | UNKNOWN | INITIAL |
| 3 | R$ 956.13 | pix_with_coupon | CONDITIONAL | UNKNOWN | INITIAL |

A observação **2 / R$ 1.099,00** é a referência documental inicial do contexto sem cupom em outros métodos. A **3 / R$ 956,13** registra o preço exibido no Pix com cupom, cuja aplicação não está comprovada. Não se subtraiu o cupom, não se calcularam porcentagens, não há previous_price nem queda histórica. Ambas têm **previous_price=NULL**, is_simulated=false e source_type **MANUAL**, significando entrada documental humana, sem atestado de preço efetivamente verificado.

As fontes são distintas: `manual:visual_document:shopee:no_coupon_other_methods` e `manual:visual_document:shopee:pix_with_coupon`. A comparação informativa existente retornou INITIAL para cada contexto, sem cruzá-los. Isso não torna os valores comercialmente comparáveis ou elegíveis; availability continua UNKNOWN.

## Proveniência, data e integridade

- Captura fornecida pelo responsável; data exibida **25/10/2026, 21h11 Brasília**, armazenada como observed_at **2026-10-26T00:11:00+00:00**. Precisão de minuto: segundos zero são normalização do armazenamento, não um segundo exato informado. Relógio/fuso da captura não foram autenticados; não se inventou verified_at ou verificação comercial atual.
- Ambas as linhas preservam o SHA256 da imagem em **raw_fingerprint**: `c2f854f2140211c0fa04380675ac0df98e52a3d1fe7738d3cd6df992556a2247`.
- Documentos preservados integralmente: [JSON visual](EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.json) e [relatório visual](EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.md). Os caminhos e hashes de ambos estão persistidos em `Product.classification_metadata.documentary_registration.document_references`.
- Cada observação identifica o documento JSON em `evidence_metadata.source_name`, contém os contextos comerciais/restrições e declara `conditions_known=false`, variante não resolvida e `price_validation=DOCUMENTARY_DISPLAY_ONLY_NOT_PUBLICLY_VERIFIED`.
- `source_reference` é NULL: o serviço existente restringe esse campo a referências oficiais, portanto não foi forçada uma URL/localização documental indevida. As referências locais foram preservadas nos metadados existentes, sem afrouxar validações.
- As referências, a imagem, `.env` e artefatos da primeira mutation mantêm os hashes anteriores à operação. Nenhum histórico de outro produto foi consultado para servir como referência ou alterado.

## Preço público e estoque não comprovados

Não existe coluna física `public_price_verified` no modelo. **public_price_verified=false** está explícito nos metadados documentais do produto e de cada contexto, e representado nas observações pelos enums **UNKNOWN/CONDITIONAL**, pelo motivo `PUBLIC_PRICE_NOT_VERIFIED` e pela validação documental não confirmada. Nenhuma linha foi marcada PUBLIC ou AVAILABLE.

Não há estoque, preço final, cupom aplicado, regra de elegibilidade universal, frete ou parcelamento confirmados. A observação é documental, sem promoção a `manual:administrator`, ManualOfferDetails ou evidência comercial completa. A declaração de equivalência entre cores não comprova seleção, SKU, disponibilidade ou titularidade.

## Idempotência e segurança

- Após o commit confirmado, a mesma lógica foi reexecutada com **todo DML proibido**: reutilizou produto 2 e observações 2/3, **zero produtos/linhas adicionais**. A identificação usa origem, contexto, fingerprint e data; conflitos seriam recusados, sem sobrescrever histórico.
- Unicidade nativa do produto e das observações, lock por escopo e verificação integral dos registros existentes preservados.
- Dependências do produto verificadas antes/depois e na reexecução: **0 links associados, 0 ofertas, 0 itens de fila, 0 outbox Telegram, 0 novos eventos de integração**. Sem oferta/outbox, não houve aprovação comercial criada.
- Viana Ofertas permanece **enabled=false**, monitoramento persistido **false**, e as seis flags Shopee/Mercado Livre/Telegram/simulação/geração de links **false**, sem alterações.
- **Zero chamadas Shopee, mutations, scraping, publicações Telegram ou ativação de scheduler.** Nenhuma associação automática de afiliado, uso da autorização de mutation antiga, alteração de credenciais, migration operacional, commit ou push.
- Conferência prévia isolada em SQLite: modelos, validações, precisão Decimal, idempotência e ausência de comparação entre contextos aprovados, sem dados persistentes de teste. Não houve nova suíte extensa; os 80 PostgreSQL anteriores não foram contabilizados como testes desta operação.

## Pendências para avaliação comercial futura

Comprovar preço efetivamente aplicável e condições, disponibilidade e escopo de variante, inclusive aplicação/regras do cupom se for usado no preço Pix. Frete/parcelamento só poderão ser alegados com evidência suficiente. Classificação/elegibilidade poderão ser avaliadas posteriormente sem ativar monitoramento.

Documentar conta/beneficiário e vínculo do shortLink e aprovar explicitamente a política de confiança antes de associar/aprovar seu uso comercial; o longLink histórico continua parcialmente sanitizado. Não há autorização de publicação, nova consulta ou nova mutation. A primeira referência documental não comprova desconto histórico.
