# Correções pós-auditoria Shopee — 25/10/2026

Escopo: desenvolvimento e validação offline. Nenhuma autorização comercial nova consumida; nenhuma chamada externa, consulta, mutation, resolução de link ou publicação autorizada por este ajuste.

## Correções

1. **Identidade unificada**: `shopee_identity` é o parser comum ao conector e ao cadastro manual. URLs convencionais e `/universal-link/` precisam provar shopId/itemId compatíveis. Caminhos desconhecidos, domínios falsos, credenciais, protocolo/porta indevidos, travessias, escapes estruturais/recursivos e variantes incompatíveis são rejeitados. Links válidos continuam armazenados integralmente, sem normalização dos parâmetros de rastreamento. Erros não contêm o link. Links curtos opacos continuam dependendo de confirmação administrativa; não são resolvidos por rede nem comprovam identidade/propriedade.
2. **Entrada administrativa real bloqueada**: `POST /api/v1/products/{id}/affiliate-link/generate` retorna `409 REAL_GENERATION_BLOCKED` antes do conector. Ainda não existe adaptador real com autorização durável integrada; flags ou repetição da requisição não liberam esse caminho. A autorização histórica permanece consumida. O laboratório continua exclusivamente em MockTransport, com reserva AppSetting, claim antes do HTTP mockado, lease/fencing e IntegrationEvent. `account_verified=false` consome a tentativa. Timeout, 5xx e persistência ambígua não repetem; interrupção é registrada UNCERTAIN antes de propagada. Não foi implementada nova modalidade de geração real.
3. **Evidências e política proposta**: documentos imutáveis/versionados em AppSetting e referências de auditoria em IntegrationEvent distinguem geração autenticada, identidade, proveniência da credencial/conta, vínculo atestado, conversão e comissão. O cadastro administrativo só registra atestados manuais, com responsável, referência documental, horário e hash do link. Proveniência exige referências administrativas não secretas da conta e da credencial; vínculo, conversão e comissão precisam corresponder à mesma conta documentada. Não pode declarar uma mutation como fato autenticado. Trocar link/origem/identidade invalida a correspondência das evidências. Registros do laboratório trazem `mode=MOCK_ONLY`; não comprovam atribuição real.
4. **Condições comerciais**: atestado independente referencia observação real persistida, preço Decimal exato, variante, disponibilidade, acesso público, condições, horário e origem/tipo/fingerprint da observação. Evidência incompleta ou vencida não basta. O histórico automático não é alterado, convertido em MANUAL ou promovido a preço público/estoque confirmado. O diagnóstico não aprova ofertas nem fornece uma ponte para o publisher.
5. **Comparabilidade**: comparações e reaproveitamento de queda exigem mesma origem, source_type, moeda e caráter simulado/real, além de disponibilidade/acesso público. Contextos de faixa de preço e base comercial distintos não se misturam. Uma cotação sem comprovação comercial permanece informativa. Histórico anterior não foi reescrito.

## APIs documentais (administrador local obrigatório)

Prefixo `/api/v1/products/{id}/shopee`:

- `POST /link-evidence`: tipo de evidência, `link_sha256`, responsável, referência e data; nunca recebe autorização de publicação ou `account_verified`.
- `POST /commercial-evidence`: atestado independente vinculado a `history_id`; não altera a observação.
- `POST /trust-proposals`: critérios configuráveis; cria somente `PROPOSED`.
- `GET /evidence-readiness?policy_id=...`: lacunas e suficiência documental; sempre `usable=false`, `account_verified=false`, `publication_authorized=false` nesta etapa.

Referências usam `document:`, `report:` ou `audit:` e identificadores internos sem URLs/segredos. O tipo AUTHENTICATED_GENERATION está reservado a uma futura importação/adaptador interno protegido: o cadastro manual não pode produzi-lo, e o mock não é evidência real. Não existe endpoint de ativação/aprovação da política nesta entrega. A fonte legada dos links manuais aprovados foi preservada; novos documentos não alteram automaticamente essa fonte.

## Política de confiança proposta — sujeita à aprovação

Exigir identidade/variante correspondente, proveniência documentada da credencial/conta e vínculo atestado pelo responsável. Para links de API, acrescentar evidência autenticada de geração vinculada à reserva consumida; para links manuais oficiais, uma política separada pode dispensar geração por API mediante decisão explícita e evidência equivalente. Conversão e comissão são provas independentes, opcionais por política; **venda anterior não é condição universal**. Geração e parsing, isoladamente, nunca promovem `account_verified`.

Uso comercial continua exigindo evidência recente de preço aplicável, variante, disponibilidade pública e condições, além de prévia, aprovação exata por conteúdo/destino, allowlist, deduplicação e revalidação. Preferências e documentos não autorizam consultas/publicações reais.

## Validação desta execução

- Testes direcionados iniciais: **301 aprovados**, zero falhas.
- Módulo novo de correções: **47 aprovados**, zero falhas.
- Suíte offline completa com bloqueio de HTTP real: **1.421 aprovados**, **80 PostgreSQL excluídos**, zero falhas; 17 avisos de depreciação de dependências.
- Revalidação final de identidade/evidências/reservas: **139 aprovados**, incluindo um caso adicional de seletor de variante vazio; rota administrativa revalidada novamente com **2 aprovados**. Esses números se sobrepõem: há **1.422 casos offline distintos aprovados** no conjunto das execuções, não a soma dos totais de cada rodada.
- Seleção PostgreSQL: **80 ignorados, nenhum teste real executado** após a conexão direta terminar OperationalError. Inclui oito contratos novos de PostgreSQL, preparados para schemas UUID isolados.
- Ruff check e format check: aprovados; `git diff --check` sem erros.
- Alembic em SQLite isolado: upgrade/current/check aprovados em **0009_shopee_discovery**, sem drift.
- O lançador temporário de validação inicialmente provocou duas falhas de subprocessos Windows. O problema era a ausência de proteção `__main__` no lançador ignorado por Git; foi corrigido, os dois testes passaram isoladamente e na suíte completa subsequente. Não foi necessário modificar o OAuth nem eliminar testes.
- Bloqueio de HTTP real durante a suíte: zero tentativas alcançaram o transporte real. Os testes de subprocessos usam suas próprias respostas mockadas. Zero chamadas reais Shopee/Telegram e zero publicações.
- Hashes de `.env` e dos dois artefatos protegidos da primeira mutation idênticos; seis flags de integrações/publicação/simulação false e inalteradas. `scheduler_enabled` local já era true e foi preservado, sem iniciar backend/scheduler operacional. O estado persistido do grupo e do monitoramento não pôde ser reconfirmado, pois o banco está indisponível. Nenhuma escrita no banco operacional.


Docker localizado, mas Engine indisponível (`dockerDesktopLinuxEngine` ausente). Conexão direta ao banco exclusivo `ofertas_bot_test` terminou `OperationalError`. Nenhum schema PostgreSQL foi criado nesta execução. Testes PostgreSQL e Alembic PostgreSQL permanecem pendentes; resultados anteriores não foram contabilizados como execução atual.

O Alembic foi validado em SQLite isolado dentro de `.runtime`: `upgrade head`, `current` em `0009_shopee_discovery` e `check` sem novas operações. Nenhuma migration ou banco operacional foi alterado.

## Arquivos desta correção

- `app/services/shopee_identity.py` (novo): identidade estrita comum.
- `app/services/affiliate_links.py`: cadastro/revalidação manual usa parser comum.
- `app/integrations/shopee.py`: wrappers compatíveis usam parser comum.
- `app/api/integrations.py`: bloqueio da rota legada e APIs de evidências/propostas.
- `app/services/affiliate_pipeline.py`: bloqueio real, metadados de atribuição simulada e interrupções UNCERTAIN.
- `app/services/shopee_evidence.py` (novo): documentos, propostas e diagnóstico conservador.
- `app/services/comparison.py` e `app/services/offers.py`: comparabilidade e referência histórica.
- `tests/test_shopee_link_identity.py` e `tests/test_affiliate_pipeline.py`: regressões existentes preservadas/estendidas.
- `tests/test_shopee_post_audit.py` e `tests/test_postgres_shopee_post_audit.py` (novos): contratos offline e PostgreSQL opt-in.
- `README.md`, `ROADMAP.md` e este relatório.

`app/api/products.py` foi revisado: já usa `validate_link_for_product`, portanto recebeu a correção pelo serviço comum, sem recriar a rota. Alterações locais anteriores permanecem preservadas.

## Bloqueios e próximo piloto mínimo

Restabelecer PostgreSQL e executar os testes opt-in em schemas UUID exclusivos de `ofertas_bot_test`. Depois, aprovar a política e reunir evidências independentes da conta/link e das condições comerciais de **um** produto/variante. Somente com nova autorização específica, implementar/revisar o adaptador real com reserva por autorização+escopo e avaliar a ponte para prévia comercial. O próximo piloto pode começar só com cadastro/evidências e prévia, sem mutation adicional e sem envio. Publicação exigirá autorização separada do conteúdo e destino; não reutilizar a primeira autorização de geração.

Sem consulta Shopee/Telegram, scraping, monitoramento comercial, scheduler operacional, commit, push ou avanço às Fases 5/6. `.env`, credenciais e registros da primeira mutation são protegidos por comparação de hashes. Não foi feita reconferência do estado persistido do grupo, devido à indisponibilidade do banco; último estado documentado: pausado.
