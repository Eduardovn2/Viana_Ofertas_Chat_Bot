# Validação técnica da Fase 3 — 18/10/2026

Estado: **FASE 3 PARCIAL / BLOQUEADA POR ACESSO comercial**. A fase foi autorizada pelo proprietário. O [gate de capacidades](INTEGRACOES_FASE_3.md) foi documentado e commitado antes da implementação. Nenhuma consulta comercial, publicação ou modificação de credenciais foi realizada.

## Ambiente e banco

Execução local Windows, PowerShell e Python 3.12 na `.venv`; não foi uma sessão WSL ou container. PostgreSQL real acessível pela configuração local já existente. O CLI Docker/Compose não foi reavaliado nem reinstalado; essa inspeção continua separada da evidência da conexão SQL.

No banco de desenvolvimento, conexão confirmada e executados com sucesso:

```powershell
python -m alembic upgrade head
python -m alembic current
python -m alembic check
python -m app.services.categories seed
```

Revisão final: **0004_marketplace_provenance (head)**. `check`: **No new upgrade operations detected**. O seed foi executado duas vezes pela mesma implementação, sem duplicações: **9 categorias**, destino `Ofertas de Casa` existente **desativado**. Migrações 0001–0003 foram preservadas; nenhum downgrade foi executado no banco de desenvolvimento.

`ofertas_bot_test` já existia e sua identidade foi confirmada antes dos testes. A URL foi derivada em memória do `.env` e passada como `TEST_DATABASE_URL` apenas ao subprocesso Pytest. Senhas/URLs/cabeçalhos não foram exibidos. Cada teste PostgreSQL criou um schema próprio, removendo somente os objetos criados por ele. Dados preexistentes e banco principal não receberam operações destrutivas de teste.

## Resultados

| Verificação | Resultado |
| --- | --- |
| Suíte completa `python -m pytest -q --tb=short`, com PostgreSQL real | **485 aprovados, 0 falhas, 0 ignorados**, 53,93 s |
| Quatro casos PostgreSQL incluídos | Migrações/preservação, categorias/roteamento/saúde e pipeline de cada conector com payloads sintéticos |
| Nova confirmação manual de cupom e regressão da API, após revisão | **17 aprovados**, sem rede externa |
| `python -m ruff check app tests scripts alembic` | Aprovado |
| `python -m ruff format --check app tests scripts alembic` | 68 arquivos aprovados |
| Parser PowerShell dos exemplos README | 22 blocos, sem erros de sintaxe |
| Alembic upgrade/current/check no PostgreSQL de desenvolvimento | Aprovado, revisão 0004, sem divergência |
| Seed idempotente | Nove categorias; Ofertas de Casa desativado |

Um aviso de depreciação Starlette/httpx já existente permanece. Não houve remoção de testes; os 243 casos das fases anteriores continuam presentes. Testes novos exercitam autenticação, autorização, timeout, 429/5xx, assinatura, contratos inesperados, valores inválidos, variantes, metadados, confirmação manual de cupom, propriedade de link, preservação de categorias, deduplicação e janela de três horas com relógio controlado. Registros com origem de contrato `OFFICIAL_API` dentro de testes são **sintéticos** e ficam apenas em bancos/schemas de teste.

## Limites e próxima validação

- Mercado Livre: token autorizado pendente. Cadastro de afiliado não comprova acesso de vendedor; descoberta ampla, cupons de terceiros e conversão automática permanecem não verificados.
- Shopee: AppId/Secret e autorização pendentes. O esquema público não demonstra estoque, acesso público, preço antigo ou variante; campos continuam desconhecidos.
- Nenhum exemplo comercial real foi validado. URLs/IDs usados nas fixtures não constituem evidência de oferta real.
- `.env` não foi alterado; as flags novas aparecem apenas em `.env.example`. Sem habilitação, não ocorre tráfego comercial em background.

Obter as credenciais oficialmente e configurá-las localmente, com as flags independentes. Depois, executar `POST /api/v1/integrations/{platform}/check` para um produto de cada loja e registrar horário, origem, identidade e campos efetivamente disponíveis. Manter a fila dry-run e Ofertas de Casa desativado. A Fase 4 exige novo aceite explícito.
