# Ofertas Bot — fundação do MVP

Backend local para cadastrar produtos, guardar links de afiliado informados manualmente e registrar um histórico de preços **simulados**. A aplicação usa Python 3.12+, FastAPI, SQLAlchemy, PostgreSQL e Alembic em um monólito modular.

Esta é somente a primeira entrega. Não há consulta real a lojas, garimpo automático, agendador ativo, cálculo de cupons, pontuação, fila ou envio de mensagens. Mercado Livre e Shopee ficam desativados como fontes reais. Nenhuma oferta é publicada.

## O que já pode ser usado

- Verificação de saúde da API e da conexão com o banco.
- Cadastro, consulta, alteração e exclusão de produtos.
- Ativação e desativação do monitoramento de cada produto.
- Cadastro, consulta, alteração e exclusão de um link de afiliado manual por produto.
- Consulta manual ao provedor simulado e leitura do histórico persistido.
- Proteção das rotas administrativas por `X-API-Key`.
- Modelos iniciais e migração para as nove entidades previstas no briefing.

Um produto sem link de afiliado retorna `affiliate_pending=true`. O registro de preços funciona independentemente desse link. O preço anterior não é inventado: o provedor simulado informa apenas o preço atual configurado, disponibilidade pública simulada e sua origem.

## Requisitos no Windows

- Python 3.12 ou mais recente, com o launcher `py` disponível.
- Docker Desktop com suporte a containers Linux e Docker Compose v2.
- PowerShell, um terminal na pasta `ofertas-bot` e a porta local `5432` disponível.

O Docker executa somente o PostgreSQL. A API roda no ambiente virtual do Windows. Os comandos seguintes partem da pasta do projeto:

```powershell
Set-Location 'C:\Users\eduardo.viana.3\Documents\ChatGPT\Chat_BOT\ofertas-bot'
py -3.12 --version
docker compose version
```

Se estiver usando outra versão compatível, ajuste `-3.12` no comando de criação do ambiente virtual.

## 1. Criar o ambiente Python

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -e '.[dev]'
```

Caso a política local do PowerShell impeça a ativação, use diretamente o executável do ambiente virtual. Não é necessário mudar a política da máquina:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
```

Nesse caso, substitua `python` por `.\.venv\Scripts\python.exe` nos demais comandos deste guia.

O arquivo `uv.lock` registra versões das dependências para instalação reproduzível. Se já usa `uv`, pode substituir a instalação com `pip` por:

```powershell
uv sync --frozen --extra dev
.\.venv\Scripts\Activate.ps1
```

## 2. Configurar o ambiente local

```powershell
Copy-Item .env.example .env
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
notepad .env
```

Use os dois valores gerados para segredos diferentes:

1. Preencha `POSTGRES_PASSWORD` com um deles.
2. Substitua `SUBSTITUA_SENHA` em `DATABASE_URL` pela mesma senha do PostgreSQL.
3. Preencha `ADMIN_API_KEY` com o outro valor. A chave deve ter pelo menos 32 caracteres.
4. Mantenha `POSTGRES_DB=ofertas_bot`, `POSTGRES_USER=ofertas_bot` e `PRICE_PROVIDER=simulated` para seguir os exemplos.

`token_urlsafe` gera valores adequados para inserir na URL. Se escolher uma senha com caracteres como `@`, `:`, `/` ou `#`, faça a codificação de URL da senha no `DATABASE_URL`.

| Variável | Finalidade |
| --- | --- |
| `APP_ENV` | Ambiente da aplicação; `development` neste guia. |
| `LOG_LEVEL` | Nível dos logs; padrão do exemplo: `INFO`. |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | Configuração usada pelo container PostgreSQL. |
| `DATABASE_URL` | URL SQLAlchemy com driver `postgresql+psycopg`, usada pela API e pelo Alembic. |
| `ADMIN_API_KEY` | Chave das rotas administrativas. Vazia mantém essas rotas bloqueadas. |
| `PRICE_PROVIDER` | Provedor de preços; somente `simulated` está disponível nesta entrega. |
| `MOCK_CURRENT_PRICE` | Preço atual de desenvolvimento; `99.90` significa R$ 99,90. |

Os valores monetários usam precisão decimal. `99.90` é um dado de teste e não foi consultado em nenhuma loja. O arquivo `.env` não deve ser versionado ou compartilhado. Não inclua chaves, senhas ou tokens em código, capturas de tela e logs.

## 3. Iniciar o PostgreSQL e aplicar a migração

```powershell
docker compose up -d postgres
docker compose ps
python -m alembic upgrade head
python -m alembic current
```

Aguarde o serviço `postgres` aparecer como `healthy` antes da migração. O Compose publica o banco somente em `127.0.0.1:5432` e mantém os dados no volume `postgres_data`. A API não cria tabelas automaticamente ao iniciar; a alteração do esquema é responsabilidade das migrações.

O usuário e a senha do container são aplicados na primeira inicialização do volume. Alterar apenas o `.env` depois disso não altera a senha de um banco já criado. Preserve o volume e faça a alteração da credencial no PostgreSQL antes de atualizar a configuração da aplicação.

## 4. Iniciar a API

```powershell
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Deixe esse terminal aberto. Em um segundo PowerShell, verifique:

```powershell
Invoke-RestMethod -Uri 'http://127.0.0.1:8000/health'
```

A resposta saudável é `{"status":"ok","database":"connected","price_provider":"simulated"}`. Em falha de conexão, a API responde HTTP 503 com `{"status":"degraded","database":"unavailable","price_provider":"simulated"}`. A verificação executa uma consulta simples de conexão; ela não confirma que as migrações foram aplicadas. Confira a revisão com `python -m alembic current`.

`/health` é público e não expõe a URL de conexão nem segredos.

A documentação local está em [Swagger UI](http://127.0.0.1:8000/docs), com contrato em [OpenAPI](http://127.0.0.1:8000/openapi.json). As operações administrativas exigem o cabeçalho `X-API-Key`, inclusive quando chamadas pela documentação.

Mantenha o bind em `127.0.0.1` durante esta fase. A aplicação também restringe o host e a origem da conexão ao acesso local. Exposição em rede exige uma etapa própria de implantação e proteção de acesso.

## 5. Experimentar o cadastro e o histórico

O produto e o link abaixo vêm do briefing, para demonstrar cadastro manual. **Não foram verificados como oferta real, preço atual ou link válido pelo projeto.** Nenhum dos comandos visita a loja ou publica mensagens.

No segundo terminal, prepare as chamadas sem colocar a chave diretamente no histórico do PowerShell:

```powershell
$baseUrl = 'http://127.0.0.1:8000/api/v1'
$secureApiKey = Read-Host 'Informe ADMIN_API_KEY do seu .env' -AsSecureString
$headers = @{ 'X-API-Key' = [System.Net.NetworkCredential]::new('', $secureApiKey).Password }
Remove-Variable secureApiKey
```

Cadastre o produto:

```powershell
$body = @{
    platform = 'MERCADO_LIVRE'
    name = 'Kit 7 pecas cabeceira modular — exemplo do briefing'
    original_url = 'https://produto.mercadolivre.com.br/MLB-3810190843-kit-7-pecas-cabeceira-de-cama-modular-para-cama-box-de-casal-_JM'
    monitoring_enabled = $true
    priority = $false
} | ConvertTo-Json

$produto = Invoke-RestMethod -Method Post -Uri "$baseUrl/products" -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([System.Text.Encoding]::UTF8.GetBytes($body))
$produto | Format-List
$productId = $produto.id
```

O código MLB é identificado na URL e normalizado para `MLB3810190843`. Também é possível fornecer `external_id` no cadastro. O nome permanece o informado pelo administrador. A aplicação não completa informações por consulta à loja.

Associe o link gerado manualmente no Link Builder oficial:

```powershell
$linkBody = @{ url = 'https://meli.la/1JQSATy' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$baseUrl/products/$productId/affiliate-link" -Headers $headers -ContentType 'application/json' -Body $linkBody
Invoke-RestMethod -Uri "$baseUrl/products/$productId/affiliate-link" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/products/$productId" -Headers $headers
```

O projeto guarda o endereço informado e deixa de marcar o produto como pendente. Ele não gera parâmetros de afiliado nem comprova que um link informado pertence ao administrador.

Registre duas observações simuladas e consulte o histórico:

```powershell
Invoke-RestMethod -Method Post -Uri "$baseUrl/products/$productId/monitor" -Headers $headers
Invoke-RestMethod -Method Post -Uri "$baseUrl/products/$productId/monitor" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/products/$productId/price-history" -Headers $headers
```

Cada chamada explícita de monitoramento cria uma observação. Com a configuração padrão, ambas recebem o mesmo preço simulado de R$ 99,90. Isso demonstra persistência, origem e horário; não comprova queda de preço. Para experimentar outra observação, altere `MOCK_CURRENT_PRICE`, reinicie a API e faça nova chamada. O histórico anterior permanece salvo.

Outras operações do CRUD:

```powershell
# Listar produtos.
Invoke-RestMethod -Uri "$baseUrl/products" -Headers $headers

# Alterar a prioridade.
$patchBody = @{ priority = $true } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$baseUrl/products/$productId" -Headers $headers -ContentType 'application/json' -Body $patchBody

# Pausar monitoramento; a chamada manual /monitor passa a ser recusada.
$monitoringBody = @{ enabled = $false } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$baseUrl/products/$productId/monitoring" -Headers $headers -ContentType 'application/json' -Body $monitoringBody

# Reativar monitoramento.
$monitoringBody = @{ enabled = $true } | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri "$baseUrl/products/$productId/monitoring" -Headers $headers -ContentType 'application/json' -Body $monitoringBody

# Remover o link manual; o produto volta a ficar pendente.
Invoke-RestMethod -Method Delete -Uri "$baseUrl/products/$productId/affiliate-link" -Headers $headers

# Excluir apenas o produto usado neste exemplo.
Invoke-RestMethod -Method Delete -Uri "$baseUrl/products/$productId" -Headers $headers
Remove-Variable headers
```

Há também um roteiro executável em [`examples/first_run.ps1`](examples/first_run.ps1). Ele solicita a chave, cadastra o exemplo, associa o link, grava duas observações e mostra o histórico. Execute uma vez em um banco sem esse produto ou exclua o cadastro anterior antes de repetir:

```powershell
.\examples\first_run.ps1
```

Use `-Cleanup` se quiser que esse roteiro exclua ao final o produto que acabou de criar:

```powershell
.\examples\first_run.ps1 -Cleanup
```

A exclusão de um produto remove também seu link de afiliado e seu histórico relacionados.

## API disponível

Todos os caminhos com prefixo `/api/v1` exigem a chave administrativa. Não há autenticação multiusuário nesta fase.

| Método | Caminho | Operação |
| --- | --- | --- |
| `GET` | `/health` | Saúde da aplicação e do banco. |
| `POST` | `/api/v1/products` | Cadastrar produto. |
| `GET` | `/api/v1/products` | Listar produtos. |
| `GET` | `/api/v1/products/{id}` | Consultar produto e pendência de afiliado. |
| `PATCH` | `/api/v1/products/{id}` | Alterar campos do produto. |
| `DELETE` | `/api/v1/products/{id}` | Excluir produto. |
| `PATCH` | `/api/v1/products/{id}/monitoring` | Alterar monitoramento com `{"enabled": true}` ou `false`. |
| `PUT` | `/api/v1/products/{id}/affiliate-link` | Cadastrar ou substituir o link manual com `{"url": "https://..."}`. |
| `GET` | `/api/v1/products/{id}/affiliate-link` | Consultar o link manual. |
| `DELETE` | `/api/v1/products/{id}/affiliate-link` | Remover o link manual. |
| `POST` | `/api/v1/products/{id}/monitor` | Consultar o provedor simulado; não recebe corpo. |
| `GET` | `/api/v1/products/{id}/price-history` | Consultar observações persistidas. |

Listagens de produtos e histórico aceitam `limit` de 1 a 100 (padrão 50) e `offset` a partir de zero. O histórico é ordenado da observação mais recente para a mais antiga. Produtos inexistentes, URLs inválidas, duplicidades e operações incompatíveis com o estado atual retornam erros HTTP. Consulte o OpenAPI local para os schemas e respostas completos.

## Testes e verificações

Em um terminal com o ambiente virtual ativado e a pasta do projeto aberta, execute os testes abaixo. Eles usam SQLite isolado e as migrações Alembic para verificar regras e a integração básica da API sem depender de Docker ou credenciais comerciais. PostgreSQL continua sendo o banco de execução local.

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check app tests alembic
```

O teste opcional de integração PostgreSQL exige `TEST_DATABASE_URL` e um banco cujo nome termine em `_test`. Ele usa um schema aleatório isolado, removido ao final. Sem a variável, o teste é pulado. A validação do SQLite e a compilação da migração para o dialeto PostgreSQL não substituem a execução em um servidor PostgreSQL real.

Crie um banco **exclusivo e descartável para testes**, separado de `ofertas_bot`. O exemplo abaixo usa o usuário padrão deste guia e solicita sua senha sem colocá-la no histórico do PowerShell:

```powershell
docker compose exec postgres psql -U ofertas_bot -d postgres -c 'CREATE DATABASE ofertas_bot_test;'
$secureDbPassword = Read-Host 'Informe POSTGRES_PASSWORD do seu .env' -AsSecureString
$dbPassword = [System.Net.NetworkCredential]::new('', $secureDbPassword).Password
$encodedDbPassword = [Uri]::EscapeDataString($dbPassword)
$env:TEST_DATABASE_URL = "postgresql+psycopg://ofertas_bot:${encodedDbPassword}@127.0.0.1:5432/ofertas_bot_test"
Remove-Variable secureDbPassword, dbPassword, encodedDbPassword

try {
    python -m pytest -q -m postgres
}
finally {
    Remove-Item Env:\TEST_DATABASE_URL
}
```

A criação do banco é necessária apenas uma vez. Não aponte `TEST_DATABASE_URL` para o banco de uso cotidiano.

Validação executada em **18/10/2026**:

- **88 testes aprovados e 1 pulado.** O teste pulado exige `TEST_DATABASE_URL` para PostgreSQL real.
- Os 7 testes de migração aprovados cobrem o esquema SQLite, alterações de revisão, restrições e geração de SQL PostgreSQL sem conexão com servidor. A integração da API também aplica as migrações Alembic no banco isolado.
- Ruff passou na verificação de código; a verificação de formatação passou para 28 arquivos.
- O roteiro `examples/first_run.ps1` e os blocos PowerShell deste README passaram pela verificação de sintaxe do parser PowerShell.
- Houve 1 aviso de depreciação de dependência no uso de `httpx` pelo `Starlette TestClient`, sem falha de teste.

O Docker não estava disponível no `PATH` do ambiente de execução. Por isso, **o Docker Compose e o PostgreSQL real não foram executados nesta validação**. Os comandos deste guia e o teste opcional permitem fazer essa verificação localmente quando o Docker estiver disponível.

## Estrutura e decisões técnicas

```text
ofertas-bot/
  app/
    main.py         criação da aplicação FastAPI
    db.py           engine e sessões SQLAlchemy
    api/            rotas e contratos HTTP
    core/           configurações e segurança
    models/         entidades SQLAlchemy
    services/       cadastro, links e monitoramento simulado
    integrations/   contrato de provedor e integrações desativadas
    publishers/     contrato abstrato para WhatsApp futuro
    jobs/           estrutura reservada, sem scheduler ativo
  alembic/          migrações versionadas
  tests/            regras e integração básica
  examples/         roteiro PowerShell para a API local
  docker-compose.yml
  pyproject.toml
  .env.example
  README.md
  ROADMAP.md
```

As entidades iniciais são `Product`, `ProductPriceHistory`, `AffiliateLink`, `Coupon`, `Offer`, `PublicationDestination`, `PublicationLog`, `MonitoringJob` e `AppSetting`. Cupons, ofertas, destinos, publicações, jobs e configurações persistentes ficam como base de esquema para as próximas fases, sem API funcional correspondente nesta entrega.

Valores monetários são decimais; os modelos preservam os campos opcionais de preço. As migrações controlam o esquema e o histórico de preços é independente do cadastro de afiliado. A API usa logs estruturados sem registrar a chave administrativa ou a URL de conexão. O contrato abstrato `WhatsAppPublisher` não tem implementação e não realiza automação.

## Parar e retomar

Encerre a API com `Ctrl+C`. Pare os containers preservando os dados:

```powershell
docker compose down
```

Para retomar, ative o ambiente virtual, execute `docker compose up -d postgres`, confira o estado saudável, aplique `python -m alembic upgrade head` e inicie o Uvicorn. Não remova o volume se quiser preservar o cadastro e o histórico.

## Problemas comuns

- **`py` não encontrado:** instale/configure um Python 3.12+ e reabra o terminal.
- **Docker indisponível:** inicie o Docker Desktop e confira `docker compose version`.
- **Porta 5432 ocupada:** pare o serviço local que ocupa a porta ou ajuste o bind no Compose e a porta do `DATABASE_URL` de forma correspondente.
- **Banco ainda não responde:** aguarde `healthy` em `docker compose ps`; confira se o banco foi criado com a senha que consta no `.env`.
- **Tabela inexistente:** execute `python -m alembic upgrade head` com o mesmo `DATABASE_URL` usado pela API.
- **Administração bloqueada:** configure `ADMIN_API_KEY` com pelo menos 32 caracteres, reinicie a API e use o mesmo valor no cabeçalho `X-API-Key`.
- **Produto duplicado ao repetir o exemplo:** reutilize o identificador já cadastrado ou exclua o produto do ensaio anterior antes de cadastrar novamente.

## Próximo avanço

O [`ROADMAP.md`](ROADMAP.md) detalha as fases. Para conectar fontes reais faltam credenciais, permissões e confirmação das operações disponíveis nas APIs autorizadas de cada plataforma, além de tratamento de limites, falhas e atualização de dados. A próxima fase depende de aprovação explícita; esta entrega não ativa integrações nem publicações reais.
