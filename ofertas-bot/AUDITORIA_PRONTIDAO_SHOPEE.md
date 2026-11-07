# VIANA OFERTAS — CORREÇÕES PÓS-AUDITORIA SHOPEE

## OBJETIVO

Corrigir as lacunas técnicas identificadas na AUDITORIA_PRONTIDAO_SHOPEE.md de 25/10/2026, preparando o projeto para um futuro piloto real controlado.

Não ativar automações nem realizar chamadas reais nesta etapa.

## ETAPA 1 — UNIFICAR A VALIDAÇÃO DE IDENTIDADE

Revisar:

- app/integrations/shopee.py
- app/services/affiliate_links.py
- app/api/products.py

Extrair ou reutilizar um validador estrito comum para URLs Shopee convencionais e /universal-link/.

Requisitos:

1. Comparar shopId e itemId com os identificadores esperados.
2. Rejeitar URLs com IDs divergentes.
3. Preservar integralmente parâmetros de rastreamento dos links válidos.
4. Rejeitar domínios falsos, caminhos ambíguos, protocolo indevido, credenciais embutidas, escapes perigosos e variantes incompatíveis quando houver identificação confiável.
5. Não considerar a identidade do produto como prova de titularidade do link.
6. Garantir que o cadastro manual não aceite automaticamente um universal-link de outro produto.

Criar regressões para os casos de aceitação indevida identificados na auditoria.

## ETAPA 2 — PROTEGER A ROTA ADMINISTRATIVA DE GERAÇÃO

Revisar:

- app/api/integrations.py
- app/services/affiliate_pipeline.py
- serviços de reservas e auditoria existentes

Centralizar no mecanismo de reserva durável todas as entradas que possam solicitar generateShortLink real.

Requisitos:

1. Reserva antes da requisição externa.
2. No máximo uma tentativa por autorização e escopo.
3. Sem retry ou fallback automático.
4. Timeout ou interrupção com resultado incerto deve permanecer UNCERTAIN.
5. Concorrência não pode provocar mutation duplicada.
6. Resposta account_verified=false não permite repetir a mutation como se ela não tivesse ocorrido.
7. Não reutilizar a autorização real já consumida.
8. Manter separação entre laboratório mockado e eventual operação real.

Se algum caminho não puder ser protegido integralmente, mantê-lo bloqueado.

## ETAPA 3 — EVIDÊNCIA DE ATRIBUIÇÃO E CONDIÇÕES COMERCIAIS

Preparar estruturas auditáveis, reutilizando os modelos existentes sempre que possível.

Distinguir explicitamente:

- Link gerado por API autenticada.
- Identidade do anúncio comprovada.
- Proveniência da credencial/conta.
- Titularidade ou vínculo atestado.
- Conversão registrada.
- Comissão validada.

Não exigir uma venda anterior como condição universal para admitir um link como utilizável. A política de confiança deve ser configurada por decisão administrativa explícita e evidências suficientes, sem promover account_verified automaticamente.

Para dados comerciais, permitir evidências rastreáveis de preço aplicável, variante, disponibilidade, condições e horário da verificação, preservando a origem real de cada informação.

Não inventar dados indisponíveis na API.
Não converter uma observação automática em atestado manual.
Não aprovar ofertas sem evidência comercial suficiente.

## ETAPA 4 — REVISÃO DE COMPARABILIDADE

Revisar PriceComparisonService para impedir a mistura indevida de observações de origens ou source_type incompatíveis.

Preservar históricos existentes e criar regressões para a distinção entre cotação observada, preço público comprovado e preço histórico comparável.

## ETAPA 5 — TESTES E SEGURANÇA

Executar:

- Testes direcionados para as quatro correções.
- Suíte offline completa.
- Testes PostgreSQL em banco isolado, se disponível.
- Ruff check e format check.
- Alembic current/check, se aplicável.

O PostgreSQL estava indisponível na última auditoria. Se continuar indisponível, registrar o impedimento sem apresentar testes não executados como aprovados.

Não modificar banco operacional, .env, credenciais, histórico ou registros da primeira mutation.

Não executar chamadas Shopee, Telegram, scraping, scheduler, commit ou push.

## ENTREGA

Apresentar:

1. Problemas corrigidos.
2. Arquivos alterados.
3. Testes aprovados e não executados.
4. Política de confiança proposta, ainda sujeita a aprovação.
5. Bloqueios comerciais restantes.
6. Próximo piloto mínimo recomendado.
7. Confirmação de zero chamadas reais e zero publicações.

Esta tarefa autoriza exclusivamente desenvolvimento e validação offline.