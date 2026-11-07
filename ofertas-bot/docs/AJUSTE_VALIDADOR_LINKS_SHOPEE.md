# Ajuste offline do validador de links Shopee

## Alteracoes

- `app/integrations/shopee.py`, funcoes `_url_identity` e `_link_target_verified`: reconhece exclusivamente o slug oficial sob `/universal-link/`, alem dos formatos convencionais; compara shopId/itemId com a origem.
- Rejeita dominio falso, credenciais na URL, protocolo/porta indevidos, IDs divergentes ou invalidos, escapes malformados, separadores codificados, travessia de caminho, fragmentos e deep links sem identidade comprovavel. Nao resolve redirects.
- `app/integrations/contracts.py`: separa `product_identity_verified` de `account_verified`. O conector nao comprova atribuicao a conta apenas pela identidade e mantem `account_verified=false`.
- `AffiliateLinkResult.long_url` conserva a URL retornada integralmente, sem reconstruir o query string, e fica fora do `repr`. Erros nao incluem URL nem rastreamento.
- A API administrativa existente continua recusando associacao automatica sem comprovacao de conta. Cadastro manual preservado, com round-trip de URL integral demonstrado em SQLite isolado.
- `tests/test_shopee.py`: expectativas ajustadas a separacao de identidade/atribuicao. Casos desconhecidos/fragmentos continuam cobertos, agora como rejeicao explicita.
- `tests/test_shopee_link_identity.py`: novas regressoes de parser, transport mockado, logs, aprovacao independente e armazenamento isolado.
- README e ROADMAP atualizados; sem migracao nova.

## Seguranca e limites

Zero chamadas reais Shopee ou Telegram nesta tarefa. Nenhuma mutation, consulta, resolucao de URL, scheduler ou publicacao real. A autorizacao anterior permanece consumida; seus registros e os eventos de auditoria nao foram reprocessados. `.env`, credenciais, banco real e flags nao foram modificados. Sem commit/push.

O reconhecimento do caminho comprova somente a identidade identificavel no link, nao a propriedade da conta, comissao ou atribuicao de conversoes. A ativacao controlada continua dependendo de comprovacao independente e de autorizacao especifica; nao repetir a mutation anterior para validar este ajuste.

## Validacao

- Regressao offline completa: **1.335 aprovados**, **58 PostgreSQL excluidos**, zero falhas; 17 avisos de depreciacao de dependencias.
- 52 novos casos acrescentados; os casos anteriores de URLs ambiguas foram preservados e ajustados para rejeicao explicita quando nao comprovam identidade.
- Execucao: `.venv/Scripts/python.exe -m pytest -q -m "not postgres" --tb=short --basetemp=.runtime/pytest-link-validator-regression-01`.
- Ruff: `python -m ruff check .`, aprovado.
- Formatacao: `python -m ruff format --check .`, **157 arquivos** aprovados.
- PostgreSQL real nao executado: escopo exclusivamente offline. Sem migracoes aplicadas no banco real; migracoes SQLite foram cobertas pela regressao. A primeira execucao encontrou restricao do diretorio temporario da sandbox; os testes de API foram repetidos com temporario isolado no projeto e permissao de execucao local.
