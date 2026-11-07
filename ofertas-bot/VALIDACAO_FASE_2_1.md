# Validação real — Fase 2.1

Data: **18/10/2026**. Resultado atual: **integração PostgreSQL real aprovada**, sem alteração das funcionalidades aprovadas.

## Execução mais recente

- Conexão real confirmada com PostgreSQL **17.11**, usando o `.env` existente sem exibir credenciais.
- Banco **`ofertas_bot_test`** já existente; reutilizado após confirmar `current_database()` antes de executar testes.
- `TEST_DATABASE_URL` foi definida somente no ambiente do subprocesso Pytest. O `.env` e o ambiente do processo principal permaneceram inalterados.
- Comando executado: `python -m pytest -q -m postgres`.
- Resultado: **2 aprovados, 0 falhas, 0 ignorados e 241 deselecionados**, em **8,01 segundos**; código de saída 0.
- Os dois testes antes ignorados validaram migrações, integridade, categorias, seed, roteamento e `/health` em schemas exclusivos criados dentro do banco de testes. A limpeza atingiu somente os objetos novos desses testes.
- Nenhum banco, tabela ou volume preexistente foi excluído; o banco principal não recebeu operações de teste.
- Um aviso de depreciação do TestClient/`httpx` permaneceu, sem impedir os testes.

A suíte completa não foi repetida nesta execução. Sua evidência anterior permanece 241 testes isolados aprovados. Docker/Compose não foram reavaliados nesta rodada; a conexão PostgreSQL direta foi suficiente para executar os testes solicitados. A Fase 3 permanece aguardando aprovação.

## Tentativa anterior — bloqueio de ambiente

O diagnóstico histórico abaixo descreve a tentativa anterior, que não conseguiu acessar a infraestrutura. Ele foi superado pela conexão e pelos testes PostgreSQL registrados acima.

| Verificação | Resultado desta execução |
| --- | --- |
| `.env` | Presente; configurações carregadas; PostgreSQL configurado; conteúdo não exibido. |
| Proteção do `.env` | Arquivo ignorado pelo Git; não alterado nem incluído em commit. |
| Docker no `PATH` | Não encontrado. |
| Caminho solicitado | `C:\Program Files\Docker\Docker\resources\bin\docker.exe` não acessível neste contexto. |
| `docker info` e `docker compose ps` | Não puderam ser executados porque o CLI não foi localizado. |
| PostgreSQL direto, usando a configuração existente | Tentativa real de conexão falhou com `OperationalError`, sem imprimir mensagem sensível ou URL. |
| TCP no destino configurado | Host local; conexão recusada (`connection_refused`). |
| Alembic `upgrade head`, `current`, `check` no PostgreSQL | Não executados: a conexão real não está disponível. Revisão real não confirmada. |
| Seed e persistência de categorias no PostgreSQL | Não executados nem confirmados nesta tentativa. |
| Banco `ofertas_bot_test` | Não criado ou alterado; acesso ao servidor indisponível. |
| Pytest PostgreSQL e suíte completa | Não executados nesta tentativa; os testes isolados não foram repetidos, conforme solicitado. |
| Ruff | Aprovado. |
| Formatação | Aprovada nos 50 arquivos Python. |

Não foram excluídos bancos, tabelas, schemas, volumes ou dados. Não houve reinstalação, alteração global do Windows, mudança de credenciais, publicação ou consulta a lojas. A execução usou PowerShell nativo Windows do Codex, sem usar WSL.

A evidência anterior permanece **241 testes aprovados e 2 testes PostgreSQL ignorados**; esses números não representam uma nova execução nem comprovam PostgreSQL real.

O roteiro operacional permanece no [README](README.md). A validação dos dois testes PostgreSQL antes pendentes está concluída; a verificação independente do CLI Docker/Compose neste contexto permanece separada.
