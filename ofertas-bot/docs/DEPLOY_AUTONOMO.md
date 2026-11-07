# Execução autônoma: API de controle, worker Python e PostgreSQL

Os processos não dependem de IA, Codex, MCP ou prompts. O worker usa os mesmos
serviços Shopee, comparador, elegibilidade comercial, fila, outbox e Telegram.
Nenhum deploy, build com downloads ou worker real foi iniciado nesta preparação.

## Operação e recuperação

- API: `app.deployment:create_control_app`. Scheduler legado e flags globais de
  integrações permanecem desligados. Subir a API não aprova nem inicia um piloto.
- Worker: `python -m app.auto_pilot_worker`. PREPARED/APPROVED/PAUSED ficam em espera.
  Somente ACTIVE_REAL, com escopo e aprovação válidos, executa ciclos. A ativação é
  administrativa e independente da IA; não há aprovação individual por mensagem.
- Ticks a cada 30 segundos, com intervalo mínimo persistido entre réplicas. O lote
  pode enviar até cinco mensagens consecutivas, respeitando a janela deslizante.
- Uma lease exclusiva de execução coordena processos e chamadas administrativas.
  Ela é renovada e conferida imediatamente antes do transporte externo. A lease
  por destino e as reservas/outbox existentes continuam em uso.
- Após queda abrupta, a lease abandonada vence em até 180 segundos. Reservas órfãs
  continuam consumidas; mensagens/outbox em andamento passam a UNCERTAIN. Consultas
  reservadas interrompidas não repetem o payload. O resultado nunca é inventado.
- SIGINT/Ctrl+C ou SIGTERM encerra o processo depois do ciclo limitado em andamento,
  sem revogar a autorização persistida. Reiniciar o processo retoma ACTIVE_REAL,
  dentro da mesma janela, sem zerar orçamento/cooldown. Para suspender a autorização
  use `/pause`; para encerrá-la definitivamente use `/stop`, antes da manutenção.
- Pausa restaura o estado anterior do destino. Retomada exige o mesmo hash,
  aprovação íntegra e janela vigente. STOPPED/COMPLETED não reabrem. Prazo, orçamento
  ou parada administrativa encerram o piloto; o worker sai normalmente.
- Dados comerciais continuam obrigatórios. Descoberta ou importação não inventam
  disponibilidade, preço público, SKU ou titularidade. Sem oferta válida: zero envios.

## Comandos locais — PowerShell, na raiz do repositório

Os comandos abaixo são um procedimento futuro. O plano atual continua PREPARED,
com janela 03/11/2026 09h–11h Brasília (12h–14h UTC), sem autorização de ativação.

Configuração não sensível do processo, preservando a identidade do plano:

```powershell
$env:AUTO_PILOT_ID = 'd80879af-27e3-47fc-b47b-01e3499a3006'
$env:AUTO_PILOT_SCOPE_SHA256 = 'fc942c3b9f2e0a28ea63f75762aadbe8f6580d2700bc50977e5944d0bbe6b387'
```

As credenciais existentes e DATABASE_URL são lidas pelo Settings do ambiente/`.env`.
Não foram alteradas. A API assistida atual ocupa a porta 8000; na implantação futura,
pare seu processo antes de substituí-lo pela API de controle. Em dois terminais:

```powershell
.venv/Scripts/python.exe -m uvicorn app.deployment:create_control_app --factory --host 127.0.0.1 --port 8000 --no-access-log --no-proxy-headers
```

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_worker
```

O segundo comando apenas espera enquanto o plano não estiver ativo. Ele não foi
executado no operacional nesta tarefa. Para consultar:

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_admin report
.venv/Scripts/python.exe -m app.auto_pilot_admin worker
.venv/Scripts/python.exe -m app.auto_pilot_worker --healthcheck
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Somente após autorização específica do responsável para este escopo, e confirmação
das condições comerciais, a aprovação única pode ser registrada:

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_admin approve --actor eduardo.viana.3 --confirm-scope --acknowledge-unconfirmed-quotas
```

`--acknowledge-unconfirmed-quotas` registra ciência da limitação, não comprova quotas
ou permissões. Dentro da janela, uma ativação explícita inicia os ciclos:

```powershell
.venv/Scripts/python.exe -m app.auto_pilot_admin start
.venv/Scripts/python.exe -m app.auto_pilot_admin pause
.venv/Scripts/python.exe -m app.auto_pilot_admin resume
.venv/Scripts/python.exe -m app.auto_pilot_admin stop
```

Cada linha é uma ação separada, a ser executada quando pretendida. Os comandos usam
a chave administrativa existente sem imprimi-la. Nenhuma dessas ações de ativação
foi executada nesta preparação.

## Endpoints administrativos

Base autenticada por `X-API-Key`: `/api/v1/auto-pilots-real/{uuid}`.

| Método/caminho | Função |
|---|---|
| GET base | Escopo, estado e contadores sanitizados |
| GET `/worker` | Heartbeat persistido e saúde do processo |
| POST `/approve` | Confirmação única do escopo, ator e ciência das permissões |
| POST `/start` | Ativa plano aprovado, com hash exato, dentro da janela |
| POST `/pause` | Suspende chamadas novas e restaura o destino |
| POST `/resume` | Retoma com o mesmo hash e janela |
| POST `/stop` | Encerra definitivamente |
| POST `/tick` | Ciclo explícito, com as mesmas leases e cadência global |

Uma requisição já entregue ao provedor pode terminar após a pausa/parada. Não é
repetida. Reservas e tentativas incertas permanecem consumidas.

## Docker Compose

`compose.deploy.yml` complementa o Compose existente; preserva o serviço PostgreSQL
e seu volume. API e worker compartilham a imagem; o worker tem perfil explícito
`pilot-worker`. O worker não depende da API para executar ciclos depois da ativação;
ambos dependem do mesmo PostgreSQL. O serviço `migrate` só roda por comando explícito.

Preparar um arquivo privado de ambiente para deploy, fora do contexto da imagem:

- Credenciais e ADMIN_API_KEY existentes, sem trocar valores nesta etapa.
- POSTGRES_DB/USER/PASSWORD existentes; banco esperado pelo adaptador: `ofertas_bot`.
- `DEPLOY_DATABASE_URL`: URL psycopg com host `postgres`, porta 5432 e os mesmos
  usuário/senha/banco; codificar caracteres reservados na URL. Isso altera somente
  o endereço de conexão dos containers. Não sobrescrever a URL local do ambiente atual.
- `AUTO_PILOT_ID` e `AUTO_PILOT_SCOPE_SHA256` exatos acima.
- `DEPLOY_ENV_FILE`: caminho do arquivo privado, se diferente de `.env`.

Definir `DEPLOY_ENV_FILE` no ambiente do Compose ou dentro desse próprio arquivo,
apontando para o mesmo caminho passado a `--env-file`, para que os containers
recebam as credenciais corretas. A senha não deve aparecer no comando ou na imagem.

As flags globais são fixadas em false no Compose. A imagem executa como UID 10001,
com sistema de arquivos somente de leitura, `/tmp` temporário, logs com rotação,
sem privilégios adicionais. `.env`, volumes, `.runtime` e dados operacionais não
entram na imagem. Dependências estão fixadas e verificadas por hash, exportadas
offline de `uv.lock` para `requirements.deploy.txt`.

Comandos futuros (build baixa a imagem base/dependências e não foi executado):

```bash
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml config --quiet
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml build api worker
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml up -d postgres
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml --profile tools run --rm migrate
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml up -d api
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml --profile pilot-worker up -d worker
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml ps
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml logs --tail 100 api worker
```

No banco já existente, conferir a revisão antes de executar migrações. Nenhuma
migração foi adicionada nesta etapa; testes migraram apenas schemas isolados.
`/ready` exige revisão atual, scheduler persistido em false e chave administrativa.
No destino VPS, preservar o banco integral, incluindo autorizações, outbox e leases;
não criar um banco vazio esperando encontrar nele o plano preparado.

A API permanece publicada somente em 127.0.0.1:8000. Chamadas de administração podem
ser feitas dentro do container, evitando expor a chave na linha de comando:

```bash
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml exec api python -m app.auto_pilot_admin report --pilot-id d80879af-27e3-47fc-b47b-01e3499a3006
```

Para approve/start/resume, informar também `--scope-sha256` exato; approve exige os
três argumentos de confirmação mostrados nos comandos locais. Para parar o piloto,
usar o mesmo comando com `stop` no lugar de `report`. Depois, parar os processos:

```bash
docker compose --env-file /caminho/privado/deploy.env -f docker-compose.yml -f compose.deploy.yml stop worker api
```

Isso preserva PostgreSQL e volume. Para acesso HTTP pelo host através da bridge
Docker, a aplicação precisa permitir explicitamente o IP privado individual do
gateway em `ADMIN_ALLOWED_NETWORKS` (JSON, por exemplo `["172.18.0.1/32"]` **somente
se esse for o gateway real**, obtido por `docker network inspect`). O padrão vazio
mantém o bloqueio remoto. Outros containers/IPs e cabeçalhos X-Forwarded-For não
ganham confiança. `X-API-Key` continua obrigatório. Acesso administrativo remoto
na VPS deve usar túnel SSH ou `docker compose exec`; não publicar a porta na internet.

## VPS e bloqueios atuais

Na VPS, instalar Docker/Compose, manter relógio UTC sincronizado, restaurar backup
autorizado do banco em local privado e fornecer as mesmas credenciais por ambiente.
Conferir `/ready`, estado do plano, destino e orçamento antes da ativação. Executar
uma única autoridade de banco: leases não coordenam cópias independentes de um backup.
Na migração, encerrar o worker da origem antes de operar o destino. Nunca executar
dois bancos clonados com a mesma autorização ativa.

O plano permanece PREPARED para 03/11/2026 09h–11h Brasília. Infraestrutura autônoma
não resolve a ausência de dados: continuam zero ofertas elegíveis. São necessários
link próprio verificado e evidências comerciais atuais, pelas fontes autorizadas
descritas em `ABASTECIMENTO_AUTO_PILOT.md`. Quotas/permissões não foram verificadas
externamente. A autorização de ativação real e a alimentação comercial continuam
pendentes. Não há renovação automática de janela, orçamento ou evidência.

## Validação desta entrega

113 casos distintos offline e 28 PostgreSQL isolados aprovados: regressão de 112/27
mais um caso adicional de encerramento exatamente no prazo em cada banco. Cobertura
inclui espera sem aprovação, pausa/retomada, dois controladores concorrentes,
cadência global após reinício, interrupção antes/depois do HTTP, última tentativa do
orçamento, deduplicação, saúde, autenticação, isolamento e bloqueio do scheduler.
Ruff e validação estrutural do Compose (`config --no-interpolate --quiet`) aprovados.
Imagem Linux não construída nem executada: exigiria downloads não autorizados nesta
etapa. Worker e API de deploy não foram iniciados no operacional. Backend assistido
existente e PostgreSQL preservados; zero chamadas externas nesta tarefa.
