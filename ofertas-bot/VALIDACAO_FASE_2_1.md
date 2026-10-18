# Tentativa de validação real — Fase 2.1

Data: **18/10/2026**. Resultado: **bloqueada pelo acesso à infraestrutura neste processo**, sem alteração das funcionalidades aprovadas.

O usuário informou Docker Desktop, PostgreSQL e `.env` funcionando no Windows. O diagnóstico abaixo descreve somente o que o processo PowerShell do Codex conseguiu verificar; não indica falha na instalação do usuário.

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

Para retomar, este processo precisa conseguir alcançar o Docker e o endereço PostgreSQL configurados no Windows. Depois disso, seguir o [README](README.md): verificar o Compose, aplicar Alembic, repetir o seed sem sobrescrever preferências, confirmar `/health` e executar testes em `ofertas_bot_test` com `TEST_DATABASE_URL` temporária. A Fase 3 continua não autorizada.
