# Ofertas Bot — MVP pessoal, fase 3 parcial

Backend local para cadastrar produtos, guardar links de afiliado e acompanhar preços simulados ou APIs oficiais explicitamente habilitadas, com comparação histórica, pontuação e fila dry-run. A aplicação usa Python 3.12+, FastAPI, SQLAlchemy, PostgreSQL, Alembic e APScheduler 3.11 em um monólito modular.

A fase 3 preserva as fases anteriores e adiciona conectores independentes Mercado Livre/Shopee, proveniência, variantes e evidências de cupom. Ambos ficam desativados por padrão. **FASE 3 PARCIAL / BLOQUEADA POR ACESSO:** não há credenciais comerciais configuradas e nenhuma consulta real a produto foi executada. A fila registra uma **simulação**, inclusive quando seu estado se chama `PUBLISHED`; ela não envia mensagens externas. A [matriz oficial de capacidades](docs/INTEGRACOES_FASE_3.md) distingue documentação, configuração e acesso comprovado.

## O que já pode ser usado

- Verificação de saúde da API e da conexão com o banco.
- Cadastro, consulta, alteração e exclusão de produtos.
- Ativação e desativação do monitoramento de cada produto.
- Cadastro, consulta, alteração e exclusão de um link de afiliado manual por produto.
- Consulta manual ao provedor simulado e leitura do histórico persistido.
- Monitoramento agendado, pausa/retomada, intervalos persistentes e controle de concorrência.
- Comparação de queda, alta e estabilidade, separada do preço anterior informado pela loja.
- Pontuação configurável, avaliação conservadora de cupons já cadastrados e fila simulada.
- Consulta de ofertas, destinos simulados e simulação explícita de publicação.
- Catálogo de categorias, classificação local e associações manuais com origem e explicação.
- Preferências conservadoras de categorias por destino, reavaliadas na fila e na simulação.
- Proteção das rotas administrativas por `X-API-Key`.
- Migrações das entidades iniciais, fila e leases de monitoramento.

Um produto sem link de afiliado retorna `affiliate_pending=true`; seus preços continuam sendo monitorados. Uma oportunidade elegível fica `PENDING` sem o link manual e pode ficar `READY` com ele. O preço anterior não é inventado: `mock_previous_price` só representa um preço anterior declarado no ensaio, enquanto o preço histórico vem das observações persistidas.

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
if (-not (Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
python -c "import secrets; print(secrets.token_urlsafe(32))"
python -c "import secrets; print(secrets.token_urlsafe(32))"
notepad .env
```

Em uma instalação nova, use os dois valores gerados para segredos diferentes. Se o `.env` já estiver configurado, preserve suas credenciais; não as substitua para executar este guia:

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
| `PRICE_PROVIDER` | `simulated` por padrão; `marketplaces` seleciona os conectores habilitados, sem fallback que invente preço real. |
| `MOCK_CURRENT_PRICE` | Preço atual de desenvolvimento; `99.90` significa R$ 99,90. |
| `SCHEDULER_ENABLED` | Habilita o agendador por padrão; `true` inicia o ciclo com a aplicação. |
| `PRIORITY_INTERVAL_SECONDS` / `GENERAL_INTERVAL_SECONDS` | Intervalos iniciais dos produtos prioritários/gerais: 60/300 segundos. |
| `PROVIDER_MIN_INTERVAL_SECONDS` | Espaçamento mínimo do provedor simulado: 1 segundo. |
| `PRICE_MAX_AGE_SECONDS` | Idade máxima da informação usada em ofertas: 600 segundos. |

Os valores monetários usam precisão decimal. `99.90` é um dado de teste e não foi consultado em nenhuma loja. O arquivo `.env` não deve ser versionado ou compartilhado. Não inclua chaves, senhas ou tokens em código, capturas de tela e logs.

## 3. Iniciar o PostgreSQL e aplicar a migração

Use o **PowerShell do Windows** em que o Docker Desktop já está acessível, com a pasta do projeto aberta. Confira o CLI, o Compose e o engine:

```powershell
docker --version
docker compose version
docker info
```

O diagnóstico opcional abaixo faz somente verificações de leitura. Ele descarta a saída completa de `docker info`, que pode conter detalhes de proxy, e não modifica Windows, WSL, credenciais ou volumes:

```powershell
python scripts/validate_infrastructure.py
```

Com o `.env` já configurado, suba somente o PostgreSQL:

```powershell
docker compose up -d postgres
docker compose ps postgres
python -m alembic upgrade head
python -m alembic current
python -m app.services.categories seed
```

Aguarde o serviço `postgres` aparecer como `healthy` antes da migração. `alembic current` deve indicar `0004_marketplace_provenance (head)`. O seed pode ser repetido sem duplicar categorias ou substituir preferências existentes. O Compose publica o banco somente em `127.0.0.1:5432` e mantém os dados no volume `postgres_data`. A API não cria tabelas automaticamente ao iniciar; a alteração do esquema é responsabilidade das migrações.

O usuário e a senha do container são aplicados na primeira inicialização do volume. Alterar apenas o `.env` depois disso não altera a senha de um banco já criado. Preserve o volume e faça a alteração da credencial no PostgreSQL antes de atualizar a configuração da aplicação.

Para atualizar uma instalação da fase 1, pare a API, atualize as dependências com `python -m pip install -e '.[dev]'` e execute `python -m alembic upgrade head`. A revisão atual é `0002_simulated_pipeline`: ela adiciona a fila e os leases, amplia estados e garante uma oferta por observação. A revisão `0001_initial_foundation` permanece intacta. Produtos, links e histórico existentes são preservados. Se um banco já tiver ofertas duplicadas para a mesma observação, a migração recusa a atualização antes de alterar o esquema; resolva os registros explicitamente.

Um downgrade da fase 2 recusa ofertas em `PENDING`/`FAILED` ou destinos `SIMULATED`, pois esses estados não existem na fase 1. Ele não converte nem exclui esses registros silenciosamente. As tabelas novas são removidas por um downgrade válido; faça backup antes de reverter uma instalação com dados.

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

O ciclo do APScheduler inicia no lifecycle da API e termina no shutdown. Com `APP_ENV=test`, ou durante pytest, não são iniciadas threads de agendamento. Em desenvolvimento, use os controles `/monitoring/pause` e `/monitoring/resume` para pausar e retomar o ciclo. Os produtos prioritários usam 60 segundos e os demais 300 segundos por padrão. Intervalos de agendamento aceitam no mínimo 1 segundo.

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

O código MLB é identificado na URL e normalizado para `MLB3810190843`. Também é possível fornecer `external_id`. O provedor simulado mantém o nome cadastrado; consultas oficiais autorizadas atualizam o nome e os metadados públicos, preservando correções manuais de categoria.

Associe o link gerado manualmente no Link Builder oficial:

```powershell
$linkBody = @{ url = 'https://meli.la/1JQSATy' } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri "$baseUrl/products/$productId/affiliate-link" -Headers $headers -ContentType 'application/json' -Body $linkBody
Invoke-RestMethod -Uri "$baseUrl/products/$productId/affiliate-link" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/products/$productId" -Headers $headers
```

O projeto guarda o endereço informado e deixa de marcar o produto como pendente. Ele não gera parâmetros de afiliado nem comprova que um link informado pertence ao administrador.

Configure um destino **local de ensaio**, separado de `Ofertas de Casa`, e escolha a categoria do exemplo. Um destino sem preferências não recebe novas entradas de fila:

```powershell
$null = Invoke-RestMethod -Method Post -Uri "$baseUrl/categories/seed" -Headers $headers
$categories = @(Invoke-RestMethod -Uri "$baseUrl/categories?limit=100" -Headers $headers)
$casa = $categories | Where-Object { $_.slug -eq 'casa-decoracao' }
$selectionBody = @{ category_ids = @($casa.id) } | ConvertTo-Json
$null = Invoke-RestMethod -Method Put -Uri "$baseUrl/products/$productId/categories" -Headers $headers -ContentType 'application/json' -Body $selectionBody

$destinationBody = @{
    name = 'Ensaio local de categorias'
    external_id = "demo-categories-$productId"
    manual_approval_required = $false
    category_mode = 'selected'
} | ConvertTo-Json
$demoDestination = Invoke-RestMethod -Method Post -Uri "$baseUrl/destinations" -Headers $headers -ContentType 'application/json' -Body $destinationBody
$preferencesBody = @{ mode = 'selected'; category_ids = @($casa.id) } | ConvertTo-Json
$null = Invoke-RestMethod -Method Put -Uri "$baseUrl/destinations/$($demoDestination.id)/categories" -Headers $headers -ContentType 'application/json' -Body $preferencesBody
```

Para reproduzir uma queda de R$ 100,00 para R$ 80,00, pause o agendador e altere a configuração simulada pela API. O roteiro salva e restaura a configuração anterior, inclusive seu estado de pausa, ao terminar:

```powershell
$configBefore = Invoke-RestMethod -Uri "$baseUrl/monitoring/config" -Headers $headers
try {
    $null = Invoke-RestMethod -Method Post -Uri "$baseUrl/monitoring/pause" -Headers $headers
    $baselineBody = @{
        provider_min_interval_seconds = 0
        mock_current_price = 100.00
        mock_previous_price = $null
        mock_availability = 'AVAILABLE'
        mock_price_access = 'PUBLIC'
    } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Patch -Uri "$baseUrl/monitoring/config" -Headers $headers -ContentType 'application/json' -Body $baselineBody
    $null = Invoke-RestMethod -Method Post -Uri "$baseUrl/products/$productId/monitor" -Headers $headers

    $dropBody = @{ mock_current_price = 80.00 } | ConvertTo-Json
    $null = Invoke-RestMethod -Method Patch -Uri "$baseUrl/monitoring/config" -Headers $headers -ContentType 'application/json' -Body $dropBody
    $null = Invoke-RestMethod -Method Post -Uri "$baseUrl/products/$productId/monitor" -Headers $headers

    Invoke-RestMethod -Uri "$baseUrl/products/$productId/price-history" -Headers $headers
    Invoke-RestMethod -Uri "$baseUrl/products/$productId/variations" -Headers $headers
    Invoke-RestMethod -Uri "$baseUrl/offers" -Headers $headers
    $queue = @(Invoke-RestMethod -Uri "$baseUrl/queue?state=READY" -Headers $headers)
    $readyItem = $queue | Where-Object { $_.product_id -eq $productId } | Select-Object -First 1
    if ($null -ne $readyItem) {
        Invoke-RestMethod -Method Post -Uri "$baseUrl/queue/$($readyItem.id)/simulate" -Headers $headers
    }
}
finally {
    $restoreBody = $configBefore | ConvertTo-Json
    $null = Invoke-RestMethod -Method Patch -Uri "$baseUrl/monitoring/config" -Headers $headers -ContentType 'application/json' -Body $restoreBody
}
```

O zero acima desativa apenas o espaçamento do provedor **simulado**, para permitir as duas chamadas imediatas do ensaio. O agendamento continua exigindo intervalos maiores ou iguais a 1 segundo. Com o padrão de 1 segundo, chamadas próximas de `/monitor` podem receber HTTP 429; espere antes de repetir. Uma consulta concorrente ao mesmo produto pode retornar HTTP 409.

A variação é calculada como `(atual - histórico) / histórico`: neste exemplo, `amount=-20.00`, `percentage=-20.00` e direção `DROP`. O histórico comparável usa a mesma origem, moeda e indicação de simulação. `store_previous_price` é independente desse histórico. A pontuação e a porcentagem de desconto da oferta usam desconto positivo; não confunda isso com a variação histórica assinada.

O motor distribui oportunidades aos destinos `SIMULATED` ativos cujas preferências aceitam uma categoria ativa do produto. Destinos antigos e novos sem configuração de categorias não qualificam novas entradas. Cada destino tem sua própria fila e janela de republicação. O endpoint `/simulate` revalida dados, categorias e link manual, registra o resultado no banco e pode marcar `PUBLISHED`; nenhum envio de rede acontece. Uma nova simulação do mesmo produto/destino exige pelo menos três horas, uma observação posterior à última simulação e dados ainda atuais. Ao repetir o roteiro antes disso, pode não existir novo item `READY`. Um destino com aprovação manual mantém o item `PENDING` até a chamada explícita a `/simulate`.

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

# Excluir um produto sem ofertas, fila ou logs associados.
# Invoke-RestMethod -Method Delete -Uri "$baseUrl/products/$productId" -Headers $headers
Remove-Variable headers
```

Há também o roteiro de cadastro da fase 1 em [`examples/first_run.ps1`](examples/first_run.ps1). Ele solicita a chave, cadastra o exemplo, associa o link, grava duas observações e mostra o histórico. Para esse ensaio, pause antes o agendador com `/monitoring/pause` e retome com `/monitoring/resume` depois. Execute uma vez em um banco sem esse produto ou reutilize o cadastro anterior:

```powershell
.\examples\first_run.ps1
```

Use `-Cleanup` se quiser que esse roteiro exclua ao final o produto que acabou de criar:

```powershell
.\examples\first_run.ps1 -Cleanup
```

A exclusão remove link e histórico quando o produto não possui referências de ofertas, fila ou publicação. Com referências de auditoria, a operação pode ser recusada por integridade; preserve o cadastro usado no roteiro de fase 2.

## Configuração, elegibilidade e fila simulada

`GET /api/v1/monitoring/config` mostra a configuração efetiva. `PATCH` altera somente os campos enviados e persiste o conjunto em `AppSetting`, sem guardar credenciais. Depois desse primeiro ajuste, os valores persistidos prevalecem sobre os valores iniciais do ambiente e sobrevivem ao reinício da API.

| Campo da configuração | Padrão | Uso |
| --- | --- | --- |
| `scheduler_enabled` | `true` | Ciclo agendado habilitado. |
| `priority_interval_seconds` | `60` | Intervalo dos produtos com `priority=true`. |
| `general_interval_seconds` | `300` | Intervalo dos outros produtos. |
| `provider_min_interval_seconds` | `1` | Espaçamento global das consultas simuladas; zero permitido para ensaios. |
| `price_max_age_seconds` | `600` | Validade da observação atual e da verificação do cupom. |
| `score_price_drop` | `40` | Peso de queda histórica comprovada. |
| `score_confirmed_coupon` | `30` | Peso de cupom confirmado e aplicável. |
| `score_priority` | `20` | Peso de produto prioritário. |
| `score_discount_percent` | `1` | Peso de cada ponto percentual de desconto. |
| `score_availability` | `5` | Peso de disponibilidade. |
| `mock_current_price` | `99.90` | Preço atual do provedor simulado. |
| `mock_previous_price` | `null` | Preço anterior declarado no ensaio; campo opcional. |
| `mock_availability` | `AVAILABLE` | Também aceita `UNAVAILABLE` e `UNKNOWN`. |
| `mock_price_access` | `PUBLIC` | `EXCLUSIVE` representa preço restrito e é excluído. |

O motor reconhece oportunidade por queda histórica, desconto declarado pela loja quando o preço anterior é válido e maior que o atual, ou cupom público confirmado e aplicável. Um produto sem cupom continua elegível. Preço ausente/inválido, indisponibilidade, acesso exclusivo e informação vencida impedem a oferta. Alterar `priority` pelo CRUD afeta o intervalo e a pontuação das próximas avaliações.

A avaliação de cupom exige confirmação, validade e verificação recente, plataforma compatível, produto explicitamente elegível, compra mínima atendida e ausência de restrições textuais ainda sem interpretação. O cálculo respeita teto e valor do desconto. Cupons `UNVERIFIED` e `INVALID` não produzem uma promessa de preço final. Esta fase não busca cupons reais nem oferece CRUD HTTP de cupons; os serviços são exercitados com dados controlados nos testes.

A fila usa `PENDING`, `READY`, `PUBLISHED`, `FAILED` e `EXPIRED`. Há no máximo um item ativo por produto/destino; o histórico terminal permanece armazenado. A simulação revalida preço, cupom, disponibilidade, atualidade e link manual antes de concluir. Os estados de ofertas legados continuam válidos no banco; a API de ofertas pode apresentar esses estados ao atualizar uma base antiga.

## Categorias e seleção por destino

O seed idempotente cria Casa e Decoração (`casa-decoracao`), Materiais e Ferramentas (`materiais-ferramentas`), Eletrodomésticos (`eletrodomesticos`), Moda (`moda`), Infantil (`infantil`), Tecnologia (`tecnologia`), Games (`games`), Beleza (`beleza`) e Gerais (`gerais`). Ele também cria `Ofertas de Casa` como destino `SIMULATED`, **desativado**, com identificador local `local:ofertas-de-casa` e modo `selected`: as oito primeiras categorias estão selecionadas e `Gerais` fica de fora. Esse identificador é interno; não representa um grupo real. Repetir o seed preserva nomes, ativações e preferências já alteradas pelo administrador.

A classificação usa, nesta ordem, **correção manual → categorias confiáveis da loja já presentes nos metadados → regras locais → Gerais**. Nome, descrição e campos públicos permitidos de `classification_metadata` são normalizados sem distinção de acentos/caixa e comparados por palavras ou expressões completas. Metadados de credenciais e campos desconhecidos não entram nas regras. Não há consulta a lojas nem serviço de IA.

As regras ficam em `app/classification_rules.json`, com slug, palavras-chave e confiança entre 0 e 1. É possível apontar `CATEGORY_RULES_PATH` para outro JSON local antes de iniciar a API. O código registra `source` (`manual`, `store` ou `rule`), confiança e uma explicação; confiança de regra é uma heurística local. Categoria confiável de loja exige `store_category_trusted=true` e `store_category_slugs` conhecidos nos metadados, sem presumir informações ausentes.

Para suspender uma categoria inicial, desative-a: a classificação assegura que os nove slugs básicos existam, portanto excluir um deles sem vínculos permite sua recriação. A reclassificação não reativa categorias desativadas nem substitui correções manuais. Uma seleção manual vazia continua vazia até remover explicitamente a correção manual.

A API classifica automaticamente ao cadastrar ou alterar nome, descrição e metadados relevantes, e o motor reavalia antes de formar ofertas/fila. `POST /products/{id}/reclassify` reaplica as regras sem substituir escolhas manuais. `PUT /products/{id}/categories` substitui a seleção manual; uma lista vazia é uma decisão explícita de deixar o produto sem categorias. `DELETE /products/{id}/categories/manual-override` remove a correção e permite classificar automaticamente de novo.

| Modo do destino | Seleção para novas entradas de fila |
| --- | --- |
| `unconfigured` (padrão) | Bloqueia até configuração explícita, mesmo se o destino estiver ativado. |
| `selected` | Exige ao menos uma categoria ativa do produto entre os IDs escolhidos. Lista vazia bloqueia. |
| `all` | Aceita qualquer categoria ativa já associada ao produto; não dispensa classificação. Use `category_ids=[]`. |

`Gerais` é uma categoria de fallback comum: selecioná-la não aceita produtos de outras categorias. Um produto associado apenas a categorias desativadas, ou sem categoria por correção manual, não qualifica nem em `all`. Múltiplas categorias aceitas continuam gerando no máximo um item ativo por produto/destino. Mudanças que tornem o item inelegível expiram a entrada ativa; preservam registros anteriores e a janela de três horas. Categorias associadas não são apagadas silenciosamente: para interromper seu uso, desative-as com `PATCH {"active":false}`.

Com `$baseUrl`, `$headers` e `$productId` já definidos, consulte a explicação ou teste reclassificação:

```powershell
Invoke-RestMethod -Uri "$baseUrl/products/$productId/categories" -Headers $headers
Invoke-RestMethod -Method Post -Uri "$baseUrl/products/$productId/reclassify" -Headers $headers
Invoke-RestMethod -Uri "$baseUrl/destinations/$($demoDestination.id)/categories" -Headers $headers
```

Uma ativação posterior de `Ofertas de Casa` pela API continua sendo apenas local e simulada. A configuração de um canal real e os envios externos pertencem a fases futuras.

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
| `GET` | `/api/v1/products/{id}/variations` | Comparar observações e separar histórico do preço anterior da loja. |
| `GET` | `/api/v1/monitoring/status` | Consultar estado do ciclo e configuração efetiva. |
| `GET`, `PATCH` | `/api/v1/monitoring/config` | Consultar/alterar configuração persistida. |
| `POST` | `/api/v1/monitoring/pause` | Pausar o ciclo agendado. |
| `POST` | `/api/v1/monitoring/resume` | Retomar o ciclo agendado. |
| `GET` | `/api/v1/monitoring/jobs` | Consultar jobs e falhas de monitoramento. |
| `GET` | `/api/v1/offers` | Listar ofertas por pontuação. |
| `GET` | `/api/v1/queue?state=READY` | Listar fila; filtro de estado é opcional. |
| `POST` | `/api/v1/queue/{id}/simulate` | Revalidar e registrar uma simulação, sem enviar mensagens. |
| `GET`, `POST` | `/api/v1/destinations` | Consultar/cadastrar destinos exclusivamente simulados. |
| `PATCH` | `/api/v1/destinations/{id}` | Alterar dados do destino simulado, incluindo ativação. |
| `POST` | `/api/v1/categories/seed` | Criar catálogo e destino local ausentes sem sobrescrever preferências. |
| `GET`, `POST` | `/api/v1/categories` | Listar ou cadastrar categorias; `active` é filtro opcional da listagem. |
| `GET`, `PATCH`, `DELETE` | `/api/v1/categories/{id}` | Consultar, alterar/ativar ou excluir categoria sem vínculos. |
| `GET`, `PUT` | `/api/v1/products/{id}/categories` | Consultar origem/explicação ou definir `{"category_ids":[1,2]}` manualmente. |
| `POST` | `/api/v1/products/{id}/reclassify` | Reclassificar preservando correção manual. |
| `DELETE` | `/api/v1/products/{id}/categories/manual-override` | Remover correção manual e voltar à classificação automática. |
| `GET`, `PUT` | `/api/v1/destinations/{id}/categories` | Consultar/definir `{"mode":"selected","category_ids":[1,2]}`. |

Listagens de produtos, histórico, ofertas e fila aceitam `limit` de 1 a 100 (padrão 50) e `offset` a partir de zero. O histórico é ordenado da observação mais recente para a mais antiga; ofertas e fila priorizam pontuação. `/monitor` retorna HTTP 201 quando registra uma observação, 429 ao atingir o espaçamento do provedor e 409 em conflito de monitoramento. Produtos inexistentes, URLs inválidas, duplicidades e estados incompatíveis também retornam erros HTTP. Consulte o OpenAPI local para os schemas e respostas completos.

## Testes e verificações

Em um terminal com o ambiente virtual ativado e a pasta do projeto aberta, execute os testes abaixo. Eles usam SQLite isolado e as migrações Alembic para verificar regras e a integração básica da API sem depender de Docker ou credenciais comerciais. PostgreSQL continua sendo o banco de execução local.

```powershell
python -m pytest -q
python -m ruff check .
python -m ruff format --check app tests alembic scripts
```

Os testes opcionais de integração PostgreSQL exigem `TEST_DATABASE_URL` e um banco cujo nome termine em `_test`. Cada um usa um schema aleatório isolado, removido ao final. Sem a variável, são pulados. Cobrem as revisões 0001–0004, constraints, preservação de dados, seed, roteamento, fila, contratos sintéticos dos conectores e `/health` sem threads. Testes comerciais nunca usam a internet. A validação SQLite/offline não substitui PostgreSQL real.

Crie um banco **exclusivo e descartável para testes**, separado de `ofertas_bot`. O exemplo abaixo usa o usuário padrão deste guia e solicita sua senha sem colocá-la no histórico do PowerShell:

```powershell
$testDbExists = docker compose exec -T postgres psql -U ofertas_bot -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname = 'ofertas_bot_test'"
if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel verificar o banco de testes.' }
if (($testDbExists -join '').Trim() -ne '1') {
    docker compose exec -T postgres psql -U ofertas_bot -d postgres -v ON_ERROR_STOP=1 -c 'CREATE DATABASE ofertas_bot_test;'
    if ($LASTEXITCODE -ne 0) { throw 'Nao foi possivel criar o banco de testes.' }
}
$secureDbPassword = Read-Host 'Informe POSTGRES_PASSWORD do seu .env' -AsSecureString
$dbPassword = [System.Net.NetworkCredential]::new('', $secureDbPassword).Password
$encodedDbPassword = [Uri]::EscapeDataString($dbPassword)
$previousTestDatabaseUrl = $env:TEST_DATABASE_URL
$env:TEST_DATABASE_URL = "postgresql+psycopg://ofertas_bot:${encodedDbPassword}@127.0.0.1:5432/ofertas_bot_test"
Remove-Variable secureDbPassword, dbPassword, encodedDbPassword

try {
    python -m pytest -q -m postgres
}
finally {
    if ($null -eq $previousTestDatabaseUrl) {
        Remove-Item Env:\TEST_DATABASE_URL -ErrorAction SilentlyContinue
    }
    else { $env:TEST_DATABASE_URL = $previousTestDatabaseUrl }
    Remove-Variable previousTestDatabaseUrl
}
```

A criação do banco é necessária apenas uma vez. Não aponte `TEST_DATABASE_URL` para o banco de uso cotidiano.

Baseline da fase 2 em **18/10/2026**: **173 testes aprovados, 1 pulado e nenhuma falha**. O teste pulado exige PostgreSQL real via `TEST_DATABASE_URL`. A suíte cobre concorrência entre sessões, fencing de workers expirados, intervalos e limites, comparação Decimal, cupons, pontuação, múltiplos destinos, republicação com relógio controlado e integração HTTP completa. Nenhuma thread real do APScheduler é iniciada nos testes; o registro e o ciclo de vida usam um scheduler controlado.

Os **18 testes de migração** verificaram upgrade/downgrade, preservação dos registros da fase 1, ausência de divergências de tipos/defaults, integridade da fila e SQL PostgreSQL offline. Uma falha injetada na reconstrução SQLite demonstrou rollback do esquema, dos dados e da revisão. Os comandos Alembic `upgrade head`, `current` e `check` também passaram em SQLite isolado, na revisão `0002_simulated_pipeline`, sem novas operações pendentes.

Validação consolidada da **fase 2.1 em 18/10/2026: 241 testes aprovados, nenhuma falha e 2 testes PostgreSQL ignorados** por ausência de `TEST_DATABASE_URL`. São os 173 testes anteriores e 68 novos: 44 de categorização/roteamento, 14 da API administrativa e 10 adicionais de migração. As fixtures dos cenários antigos de fila passaram a declarar `category_mode=all` explicitamente; suas verificações de preço, concorrência, deduplicação e três horas foram preservadas. A alteração é necessária porque destinos sem configuração agora bloqueiam o roteamento.

Na entrega da fase 2.1, a suíte incluía **28 testes de migração aprovados**. Os comandos Alembic `upgrade head`, `current` e `check` passaram em SQLite isolado na revisão `0003_category_routing`, sem divergências. O seed executado duas vezes criou nove categorias e um destino na primeira execução e zero registros na segunda. O SQL PostgreSQL foi verificado offline; a validação PostgreSQL posterior está registrada abaixo.

Ruff e formatação passaram nos **50 arquivos Python**. Os **22 blocos PowerShell** do guia e `examples/first_run.ps1` passaram no parser. Há um aviso de depreciação de terceiros no TestClient do Starlette sobre `httpx`; ele não impediu os testes. Caso o ambiente restrinja o diretório temporário padrão do pytest, acrescente `--basetemp=.pytest_cache/testes-locais` a uma execução em um diretório reservado para esses testes.

Na fase 2.1, o usuário confirmou **Docker Desktop instalado e engine 29.8.2 ativo via WSL 2 no seu PowerShell do Windows**. Isso é um relato do usuário, separado do que este processo verificou. O processo de execução do Codex está no Windows, com caminho e shell Windows; nele, `docker --version`, `docker compose version` e `docker info` falharam com `The term 'docker' is not recognized`. O CLI também não foi encontrado nos caminhos usuais consultados, e o `.env` local estava ausente. Isso não demonstra falta de instalação nem uma falha no Docker do usuário.

Por esse bloqueio de acesso ao CLI neste ambiente de execução, **Compose, migração no PostgreSQL real e `/health` conectado ao PostgreSQL real permanecem pendentes de verificação**. Nenhum recurso do Windows/WSL, credencial ou volume foi alterado. Execute os comandos da seção 3, inicie a API e consulte `/health` no PowerShell em que o Docker já funciona; depois rode `python -m pytest -q -m postgres` com a variável temporária acima. Não é necessário reinstalar o Docker para executar este roteiro.

Na tentativa final posterior, ainda em 18/10/2026, o `.env` já estava acessível e foi carregado com sucesso, mas o CLI Docker continuou indisponível e a conexão direta ao PostgreSQL local foi recusada. Não foram repetidos os testes isolados. Ruff e formatação passaram. Os resultados e as etapas não executadas estão no [relatório de validação real](VALIDACAO_FASE_2_1.md).

**Atualização posterior em 18/10/2026:** o acesso direto ao PostgreSQL **17.11** passou a funcionar. O banco `ofertas_bot_test` já existia e os dois testes antes ignorados foram executados com `TEST_DATABASE_URL` temporária: **2 aprovados, 0 falhas, 0 ignorados e 241 deselecionados**, em 8,01 segundos. Nenhuma operação de teste foi executada no banco principal e nenhum objeto preexistente foi excluído. A suíte completa e o diagnóstico Docker/Compose não foram repetidos nessa rodada.

## Estrutura e decisões técnicas

```text
ofertas-bot/
  app/
    main.py         criação da aplicação FastAPI
    db.py           engine e sessões SQLAlchemy
    api/            rotas e contratos HTTP
    core/           configurações e segurança
    models/         entidades SQLAlchemy
    services/       categorias, roteamento, comparação, cupons, scoring e fila simulada
    classification_rules.json  regras locais de categorias
    integrations/   contrato de provedor e integrações desativadas
    publishers/     contrato abstrato para WhatsApp futuro
    jobs/           runtime APScheduler, limites e leases de monitoramento
  alembic/          migrações versionadas
  tests/            regras e integração básica
  examples/         roteiro PowerShell para a API local
  scripts/          diagnóstico de infraestrutura sem modificar o ambiente
  docker-compose.yml
  pyproject.toml
  .env.example
  README.md
  ROADMAP.md
```

As entidades iniciais são `Product`, `ProductPriceHistory`, `AffiliateLink`, `Coupon`, `Offer`, `PublicationDestination`, `PublicationLog`, `MonitoringJob` e `AppSetting`, com associação `coupon_products`. A fase 2 acrescenta `PublicationQueueItem` e `MonitoringLease`, configurações persistentes e os endpoints descritos acima. A fila tem uma restrição de banco que exige `is_simulated=true`; não existe implementação de envio real nesta versão.

A fase 2.1 acrescenta `Category`, `ProductCategory` e `DestinationCategory`, preferências por destino e os campos opcionais de descrição/metadados do produto. A revisão `0003_category_routing` segue o histórico anterior e adota `unconfigured` para destinos existentes; configure as preferências explicitamente antes de esperar novas entradas de fila.

Valores monetários são decimais; os modelos preservam os campos opcionais de preço. As migrações controlam o esquema e o histórico de preços é independente do cadastro de afiliado. A API usa logs estruturados sem registrar a chave administrativa ou a URL de conexão. O contrato abstrato `WhatsAppPublisher` não tem implementação e não realiza automação.

## Parar e retomar

Encerre a API com `Ctrl+C`. Pare os containers preservando os dados:

```powershell
docker compose down
```

Para retomar, ative o ambiente virtual, execute `docker compose up -d postgres`, confira o estado saudável, aplique `python -m alembic upgrade head` e inicie o Uvicorn. Não remova o volume se quiser preservar o cadastro e o histórico.

## Problemas comuns

- **`py` não encontrado:** instale/configure um Python 3.12+ e reabra o terminal.
- **Comando Docker não encontrado:** use o PowerShell em que `docker info` já funciona e confira `(Get-Command docker).Source`; não confunda ausência do CLI neste processo com falta de instalação do Desktop.
- **Porta 5432 ocupada:** pare o serviço local que ocupa a porta ou ajuste o bind no Compose e a porta do `DATABASE_URL` de forma correspondente.
- **Banco ainda não responde:** aguarde `healthy` em `docker compose ps`; confira se o banco foi criado com a senha que consta no `.env`.
- **Tabela inexistente:** execute `python -m alembic upgrade head` com o mesmo `DATABASE_URL` usado pela API.
- **Administração bloqueada:** configure `ADMIN_API_KEY` com pelo menos 32 caracteres, reinicie a API e use o mesmo valor no cabeçalho `X-API-Key`.
- **Produto duplicado ao repetir o exemplo:** reutilize o identificador já cadastrado ou exclua o produto do ensaio anterior antes de cadastrar novamente.

## Integrações da fase 3

Use a [documentação de acesso](docs/INTEGRACOES_FASE_3.md) antes de habilitar qualquer loja. A instalação existente mantém `.env` e credenciais intactos. Obtenha acesso em sua própria conta oficial e configure **localmente**, sem compartilhar segredos:

| Loja | Variáveis opcionais | Operação implementada e limite |
| --- | --- | --- |
| Mercado Livre | `MERCADO_LIVRE_ENABLED`, `MERCADO_LIVRE_ACCESS_TOKEN` | Metadados e preços em `/sale_price` + `/prices`; Bearer autorizado. Campos legados de preço de `/items` são ignorados. Busca ampla, cupons de terceiros e geração automática de links não foram comprovados. |
| Shopee | `SHOPEE_ENABLED`, `SHOPEE_APP_ID`, `SHOPEE_SECRET` | `productOfferV2` para consulta/descoberta; assinatura SHA256 do corpo exato. Estoque, acesso público e preço antigo ficam desconhecidos quando o esquema não os comprova; tais observações não geram oferta elegível. |
| Shopee, links | `SHOPEE_SHORT_LINKS_ENABLED` | `generateShortLink` somente mediante chamada administrativa explícita; sem retry da mutation, sem substituir link existente. Associação exige resposta coerente com o produto; formatos ambíguos exigem verificação manual. |

Com `PRICE_PROVIDER=simulated`, a automação existente continua simulada; `refresh-real`/`check` permitem consultas pontuais se a loja estiver habilitada. Com `PRICE_PROVIDER=marketplaces`, o scheduler usa as lojas habilitadas e ignora as desativadas, sem substituí-las pelo simulado. Reinicie a API após mudar flags/credenciais locais. Startup e testes não fazem consultas comerciais involuntárias.

`INTEGRATION_*` define timeout, tentativas, intervalo e cache (valores no `.env.example`). O intervalo local de 1 segundo é conservador/configurável, **não uma quota oficial garantida**. Adeque-o à permissão da conta. Locks de produto e limites por credencial são compartilhados no banco; `Retry-After` também persiste entre processos. 401/403 bloqueiam essa credencial até `check` explícito bem-sucedido. Falhas mantêm o último histórico válido e registram `IntegrationEvent` separado. O cache conserva o instante original e não simula uma revalidação.

Descoberta Shopee manual é paginada. Automática exige também `SHOPEE_DISCOVERY_ENABLED=true`, `SHOPEE_DISCOVERY_QUERY` e `PRICE_PROVIDER=marketplaces`; consulta até 10 itens da primeira página por intervalo geral, sem varrer backlog no reinício. Não há descoberta automática Mercado Livre sem comprovação de acesso apropriado.

Todas as rotas abaixo exigem `X-API-Key` e acesso local:

| Rota | Uso |
| --- | --- |
| `GET /api/v1/integrations/status` | Flags, presença de credenciais, bloqueios, última consulta e capacidades; nenhum segredo. |
| `GET /api/v1/integrations/{platform}/capabilities` | Estados por operação. Use `MERCADO_LIVRE` ou `SHOPEE`. |
| `POST /api/v1/integrations/{platform}/check` | Corpo `{"product_id": 1}`; limpa cache, consulta um produto cadastrado e só libera bloqueio após sucesso. Persiste histórico/diagnóstico, sem publicação. |
| `POST /api/v1/products/{id}/refresh-real` | Consulta oficial pontual usando as mesmas reservas e validações do scheduler. |
| `POST /api/v1/integrations/{platform}/discover` | Corpo `{"query":"ferramentas","page":1,"limit":20}`; ingestão somente se a operação for autorizada. |
| `POST /api/v1/products/{id}/affiliate-link/generate` | Shopee com flag específica; usa a URL canônica cadastrada. Mercado Livre retorna capacidade não suportada. |
| `POST/GET /api/v1/products/{id}/coupon-evidence` | Cadastro manual conservador e consulta paginada de evidências; ver `/docs` para campos. |

Produtos Shopee usam `external_id="shopId:itemId"` e URL `/product/shopId/itemId` ou slug `-i.shopId.itemId`. `variant_id` é opcional no cadastro, imutável e faz parte da unicidade: cada variante tem histórico/fila próprios. Se a API não resolve seu preço, fica `None/UNKNOWN`, sem empréstimo do menor preço do anúncio. Moeda é BRL e valores monetários são `Decimal`.

O link Mercado Livre é cadastrado por `PUT /products/{id}/affiliate-link`, com origem `MANUAL_LINKBUILDER`; Shopee aceita o fluxo manual. Use `operationally_verified=true` **apenas após conferir pertencimento à própria conta e associação ao produto**. O sistema valida formato/identidades reconhecíveis, sem resolver encurtadores ou usar cookies. No armazenamento, fontes terminadas em `_VERIFIED` registram essa confirmação manual; links legados continuam válidos no ensaio simulado. Dados reais exigem confirmação operacional ou geração Shopee comprovada para chegar a `READY`. `affiliate_pending` no cadastro indica ausência de link; a lista de ofertas informa a pendência efetiva de elegibilidade.

Cupom começa `UNVERIFIED/UNKNOWN`. Confirmação manual requer produto elegível, condições completas, referência oficial, verificação com fuso, validade e acesso público. Apenas código/desconto percentual ou fixo completos são projetados no avaliador anterior; mínimo, teto, frescor e expiração continuam obrigatórios. Frete grátis é evidência separada e não vira desconto inventado no produto. Campanha anunciada e cupom expirado não aumentam score.

Uma nova confirmação do mesmo cupom manual, produto e condições renova `last_verified_at` e acrescenta evidência auditável. Condições/origem/produto diferentes são recusados para preservar o registro anterior.

Histórico expõe `source_type`, `source_reference`, fingerprint e evidência pública limitada, sem respostas privadas. `previous_price` é preço anterior anunciado; `historical_price` continua vindo do histórico comparável. Metadados de loja sem mapeamento confiável não ganham categoria local inventada. Imagens são apenas referências públicas permitidas, sem download/publicação. A fila mostra origem, momento, bloqueio e próxima elegibilidade; `is_simulated=true` refere-se à ação dry-run, `source_is_simulated` distingue a origem dos dados. A janela de três horas exige uma nova observação e as categorias continuam sendo revalidadas. `Ofertas de Casa` permanece desativado.

`0004_marketplace_provenance` acrescenta variantes, proveniência, evidências e diagnósticos, sem reescrever 0001–0003. O downgrade recusa dados novos que seriam perdidos; revise-os e faça backup antes de qualquer reversão. Nunca execute downgrade ou testes destrutivos no banco de desenvolvimento.

Validação da Fase 3 em 18/10/2026: **485 testes aprovados, sem falhas/ignorados**, incluindo quatro testes PostgreSQL em `ofertas_bot_test`. Ruff e formatação aprovados em 68 arquivos Python; `alembic upgrade head/current/check` aprovados no PostgreSQL real, revisão 0004. Seed idempotente confirmou nove categorias e `Ofertas de Casa` desativado. Detalhes e a distinção entre PostgreSQL real e dados comerciais sintéticos estão no [relatório de validação](docs/VALIDACAO_FASE_3.md).

## Próximo avanço

O [`ROADMAP.md`](ROADMAP.md) registra a aprovação da fase 3 e a pendência de acesso comercial. Configure as credenciais opcionais obtidas oficialmente, confirme permissões com um produto por loja e registre horário/origem/preço sem publicar. A fase continua **parcial/bloqueada por acesso** até essa validação. A Fase 4 não foi iniciada e depende de novo aceite explícito antes de qualquer publicação real.
