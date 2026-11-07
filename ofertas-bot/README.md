# Ofertas Bot — fase 4 validada, envios desativados

Backend local para cadastrar produtos, guardar links de afiliado e acompanhar preços simulados ou APIs oficiais explicitamente habilitadas, com comparação histórica, pontuação e fila dry-run. A aplicação usa Python 3.12+, FastAPI, SQLAlchemy, PostgreSQL, Alembic e APScheduler 3.11 em um monólito modular.

A fase 3 preserva as fases anteriores e adiciona conectores independentes Mercado Livre/Shopee, proveniência, variantes e evidências de cupom. **Estado atual:** consulta automática Mercado Livre suspensa após HTTP 403; OAuth e tokens protegidos preservados; leitura Shopee `productOfferV2` confirmada em consulta pontual (HTTP 200), mantendo a integração desativada; a categoria zero foi corrigida e validada offline posteriormente. A fase 4 acrescenta cadastro, prévia e aprovação administrativa de ofertas manuais, com envios desativados. A fila simulada anterior mantém seus controles, inclusive quando seu estado se chama `PUBLISHED`; isso não representa uma mensagem externa. A [matriz oficial de capacidades](docs/INTEGRACOES_FASE_3.md) distingue documentação, configuração e acesso comprovado. As seções de validação abaixo preservam os resultados históricos das fases anteriores.

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

Aguarde o serviço `postgres` aparecer como `healthy` antes da migração. `alembic current` deve indicar `0008_shopee_collection (head)`. O seed pode ser repetido sem duplicar categorias ou substituir preferências existentes. O Compose publica o banco somente em `127.0.0.1:5432` e mantém os dados no volume `postgres_data`. A API não cria tabelas automaticamente ao iniciar; a alteração do esquema é responsabilidade das migrações.

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
| Shopee, links | `SHOPEE_SHORT_LINKS_ENABLED` | `generateShortLink` somente mediante chamada administrativa explícita; sem retry da mutation, sem substituir link existente. Identidade coerente é separada da atribuição à conta: reconhecer o caminho não define `account_verified=true`; associação automática permanece bloqueada sem comprovação independente. |

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

## OAuth local do Mercado Livre

O [guia OAuth do Mercado Livre](docs/OAUTH_MERCADOLIVRE.md) prepara uma autorização oficial pelo navegador com PKCE, sem expor a API administrativa. `MERCADOLIVRE_OAUTH_ENABLED=false` é o padrão; Client ID e Client Secret devem ser preenchidos apenas no `.env` local, após configurar sua própria aplicação no DevCenter. Não envie credenciais pelo chat.

O script `scripts/start_oauth_tunnel.ps1` prepara um Cloudflare Quick Tunnel para um gateway separado em `127.0.0.1:8787`, grava a URI exata em `MERCADOLIVRE_REDIRECT_URI` e mantém o controle dos processos em `.runtime/oauth-tunnel.json`. Cadastre essa mesma URI HTTPS no DevCenter. A API principal permanece em `127.0.0.1:8000`; o gateway expõe somente o callback, sem Swagger/OpenAPI. Um túnel gerenciado ainda ativo é reutilizado; iniciar outro após encerrá-lo gera um hostname novo, que precisa ser recadastrado.

`POST /api/v1/oauth/mercadolivre/authorize` exige administração local por `X-API-Key` e devolve uma `launch_url` descartável para abrir no navegador. Não imprima ou compartilhe essa URL. O navegador recebe um cookie seguro e segue para a autorização oficial; o retorno precisa corresponder ao cookie e ao `state`, dentro de 600 segundos por padrão. `GET /api/v1/oauth/mercadolivre/status` informa apenas configuração, presença e validade do token. As requisições OAuth releem o `.env`, permitindo preencher as credenciais depois de preparar o túnel sem reiniciá-lo.

O armazenamento OAuth é separado do PostgreSQL: `%LOCALAPPDATA%\VianaOfertasChatBot\oauth\mercadolivre.sqlite3`, com tokens e verificador protegidos por DPAPI do usuário Windows atual e ACL restrita a esse usuário e SYSTEM. **No checkpoint OAuth abaixo**, o token ainda não era usado pelo provedor de preços, e renovação/rotação de refresh token não haviam sido implementadas nem testadas. A evolução desse ponto está registrada na seção da Fase 3.2. As flags comerciais Mercado Livre/Shopee continuam independentes e desativadas por padrão; autorizar OAuth não consulta produtos nem publica mensagens. Ao terminar, execute `scripts/stop_oauth_tunnel.ps1`.

**OAuth real concluído em 18/10/2026 às 17:28:59 America/Sao_Paulo (20:28:59 UTC).** Foram confirmados vínculo com o navegador, `state`, PKCE S256, troca no endpoint oficial, consumo do lançamento/estado e armazenamento DPAPI CurrentUser com ACL de usuário + SYSTEM. A inspeção confirmou expiração válida, fingerprint compatível com o Client ID e ausência dos tokens em texto puro nos blobs cifrados. A flag OAuth foi restaurada para `false`, `MERCADO_LIVRE_ENABLED=false`, `SHOPEE_ENABLED=false` e `PRICE_PROVIDER=simulated`; nenhuma consulta comercial ou publicação foi executada. A suíte completa posterior aprovou **699 testes em 64,61 segundos**, incluindo quatro testes PostgreSQL reais em schemas isolados de `ofertas_bot_test`, com um `StarletteDeprecationWarning`. Ruff e formatação aprovaram os 88 arquivos Python. O [guia OAuth](docs/OAUTH_MERCADOLIVRE.md) preserva as evidências e os limites dessa validação.

A proteção dos processos do túnel também passou em 19 verificações PowerShell isoladas: propriedade da porta, filho Python legítimo, rejeição de serviço alheio e encerramento aguardado da árvore gerenciada. Execute `./tests/test_oauth_tunnel_processes.ps1` para repetir essa verificação sem criar túnel nem carregar credenciais.

## Fase 3.2 — retomada autorizada

A retomada foi autorizada em 18/10/2026 para conectar o armazenamento OAuth ao conector Mercado Livre, implementar renovação com rotação segura e concorrência e validar uma consulta pontual do item `MLB3810190843`, com o link manual `https://meli.la/1JQSATy`. O escopo inclui persistência PostgreSQL, categorização, histórico e fila simulada. Shopee, mineração automática e publicações continuam fora desta execução; a Fase 4 não foi iniciada.

O gerenciador de tokens e sua integração ao conector Mercado Livre estão implementados e cobertos por testes de refresh e concorrência. Os resultados anteriores de OAuth e da pausa permanecem históricos. O [relatório da Fase 3.2](docs/VALIDACAO_FASE_3_2.md) distingue respostas reais de fixtures sintéticas e registra o bloqueio comercial sem inventar preço, cupom, estoque ou condição pública.

A sondagem real de **18/10/2026 às 21:27:01 America/Sao_Paulo (19/10/2026 00:27:01 UTC)** confirmou token ainda válido e `/users/me` HTTP 200, com identidade compatível e site MLB. O runner do conector foi executado às **21:32:48.703151 America/Sao_Paulo** e retornou `FAILED / AUTHORIZATION_DENIED / HTTP 403` em `/items/MLB3810190843`, sem causa específica comprovada. `/sale_price` e `/prices` não foram chamados. O PostgreSQL guardou o evento de falha e o link informado como configuração manual; não foram criados produto, preço, oferta ou fila com dados presumidos. A Fase 3.2 permanece **não operacional para essa consulta comercial**.

Após comprovar no DevCenter o acesso funcional e as condições do programa aplicáveis, ou obter orientação do suporte oficial para o HTTP 403, a retomada pontual usa um único comando:

```powershell
.\.venv\Scripts\python.exe scripts/validate_mercadolivre.py --item MLB3810190843 --affiliate-url 'https://meli.la/1JQSATy'
```

O runner habilita Mercado Livre apenas nas configurações em memória dessa execução e persiste o scheduler em pausa. As flags comerciais no `.env` permanecem desativadas e `PRICE_PROVIDER=simulated` é preservado. As consultas reutilizaram o token válido armazenado; refresh real não foi exercitado. Se uma consulta futura retornar um snapshot válido, o runner usa o fluxo existente de categorização, histórico, ofertas e fila simulada, com a janela de três horas.

O reteste de **18/10/2026 às 21:35:43 America/Sao_Paulo**, após revisão de permissões pelo proprietário, também retornou HTTP 403. O proprietário cadastrou a nova URI e concluiu outro consentimento OAuth aproximadamente às **21:40–21:41**, atualizando os tokens protegidos por DPAPI e consumindo os registros de `state`/PKCE. A retentativa com o **novo token**, às **21:41:28.303381 America/Sao_Paulo (19/10/2026 00:41:28 UTC)**, continuou `FAILED / AUTHORIZATION_DENIED / HTTP 403`, sem consultar `/sale_price` ou `/prices`. Houve novo Authorization Code; **o grant de refresh real não foi exercitado**.

O novo consentimento usou uma API atualizada temporária em `127.0.0.1:8001`, com scheduler desativado, encerrada ao terminar. A API preexistente em `8000`, que não oferecia as rotas OAuth nessa instância, foi preservada. Gateway em `8787` e novo túnel permanecem ativos; a URI real está apenas no `.env` e no manifesto local. A flag OAuth foi restaurada para `false`; Mercado Livre/Shopee seguem desativados e o provedor simulado foi preservado. O próximo passo é esclarecer o acesso funcional/programa ou a recusa com o suporte oficial, sem repetir consentimentos sem uma mudança de acesso identificada. A Fase 4 continua fora do escopo.

A verificação final com o novo token confirmou `/users/me` HTTP 200 e identidade compatível, ausência de access/refresh token em texto puro no SQLite e nenhuma tentativa OAuth pendente. O PostgreSQL confirmou o evento recusado, a configuração manual preservada e zero produtos/históricos desse item. A implementação atual da API ficará disponível na instância usual após seu reinício; a instância legada não foi encerrada durante esta validação.

A suíte final desta retomada aprovou **755 testes em 136,34 segundos, sem falhas ou skips**, incluindo seis casos PostgreSQL reais em schemas isolados de `ofertas_bot_test`. Concorrência com oito threads, três processos e falha de processo foi verificada em cenários controlados com mocks/dados temporários; a rotação real continua não exercitada. Houve um `StarletteDeprecationWarning`. Ruff e formatação aprovaram 94 arquivos Python; Alembic `current/check` confirmou a revisão 0004 sem divergências no PostgreSQL real.

## Fase 4 — ofertas manuais, destinos e outbox Telegram

A retomada de 20/10/2026 preserva a branch `codex/fase-3` e os componentes anteriores do Telegram. O fluxo manual está integrado à outbox e ao publisher, com despacho administrativo pontual bloqueado por padrão e sem publicação automática. Mantenha `TELEGRAM_PUBLISHER_ENABLED=false` e `TELEGRAM_REAL_SENDS_AUTHORIZED=false`. Não é necessário fornecer token ou reautorizar OAuth para testar o fluxo com mocks. Veja o [guia Telegram da Fase 4](docs/TELEGRAM_FASE_4.md).

Primeiro cadastre o produto pelas rotas existentes e seu link próprio gerado pelo Link Builder oficial. No `PUT /api/v1/products/{id}/affiliate-link`, informe `operationally_verified=true` somente após conferir a própria conta e a associação ao produto. Links sem essa confirmação são recusados no fluxo manual real. O monitoramento do produto pode continuar desativado.

| Rota administrativa | Comportamento |
|---|---|
| `POST /api/v1/manual-offers` | Registra preço e evidência manuais, sem consultar lojas. |
| `GET /api/v1/manual-offers` | Lista com `product_id`, `limit` e `offset` opcionais. |
| `GET /api/v1/manual-offers/{id}` | Consulta o registro preservado, inclusive após vencer. |
| `POST /api/v1/manual-offers/{id}/preview` | Recebe `destination_id` e `include_image` opcional; devolve mensagem exata, estado e `preview_hash`. |
| `GET /api/v1/telegram/publications` | Lista prévias/aprovações com `destination_id`, `limit` e `offset`. |
| `POST /api/v1/telegram/publications/{id}/approve` | Recebe `preview_hash`, `actor` e `confirmed=true`; revalida antes de aprovar. |

Todas exigem administração local por `X-API-Key`. Cadastro exige `product_id`, `current_price`, `actor`, `verified=true`, `availability="AVAILABLE"` e `price_access="PUBLIC"`. Informe valores efetivamente conferidos pelo administrador; preços são `Decimal`, positivos, em BRL, com precisão de centavos. `previous_price`, `coupon_id`, `installment_text`, `image_url`, `verified_at` e `expires_at` são opcionais. Datas informadas exigem fuso; verificação futura/vencida é recusada. O padrão de frescor é `PRICE_MAX_AGE_SECONDS=600`; a validade efetiva também respeita o cupom e qualquer vencimento anterior informado.

O preço anterior anunciado fica separado do histórico próprio, que utiliza somente observações manuais comparáveis. Sem referência não há desconto presumido. Cupom informado precisa ter confirmação pública, vínculo ao produto, condições satisfeitas e validade/verificação recentes; seu cadastro continua nas rotas existentes de evidência de cupom. Sem cupom a oferta continua válida. Imagem deve ser URL HTTPS pública válida; o cadastro rejeita uma URL insegura. Parcelamento e demais textos são escapados na mensagem, que inclui identificação publicitária.

A prévia exige destino Telegram cadastrado, habilitado para avaliação local e aceito pelos filtros existentes (`all` ou `selected`, com categoria ativa). `unconfigured`, destino pausado ou categoria incompatível bloqueiam a prévia. As novas rotas `POST/GET /api/v1/telegram/destinations` e `GET/PATCH /api/v1/telegram/destinations/{id}` administram cadastro, pausa, filtros e limites, sempre com aprovação obrigatória. O Chat ID é imutável e duplicatas são recusadas. `Ofertas de Casa` não é ativado automaticamente.

O perfil **Viana Ofertas — Chat ID -5309308689** pode ser preparado por `POST /api/v1/telegram/destinations/prepare-test-group` ou `python -m app.services.telegram_destinations prepare-test-group`. É idempotente e mantém o grupo pausado, sem modificar allowlist/token/flags. **Após iniciar Docker, o cadastro foi persistido no PostgreSQL como destino id 2, `enabled=false` e aprovação obrigatória.** O filtro continua `unconfigured`. Não se confirmou acesso do bot nem se chamou a API real do Telegram.

A aprovação pertence à oferta e ao destino e não autoriza envio. A oferta continua `PENDING_REVIEW`; o registro independente `TelegramPublication` passa de `PENDING` para `APPROVED`. Preço, link, cupom, categorias, destino ou conteúdo alterados exigem nova revisão. Repetir o pedido de aprovação preserva o auditor original. Repetir cadastro com nova verificação cria outra observação/oferta; a prévia da nova versão remove a aprovação anterior no mesmo slot. O histórico não é sobrescrito. Prévia e aprovação não escrevem logs de envio nem disparam a fila simulada anterior.

A janela mínima de três horas exige nova observação manual posterior ao sucesso e nova aprovação. `POST /api/v1/telegram/publications/{id}/dispatch` recebe `confirmed=true` e o hash aprovado, revalida as condições e exige ambas as flags, destino permitido e token configurado. Nesta etapa fica bloqueado. Não existe job de despacho automático. A prévia pode omitir a imagem com `include_image=false`; a mudança exige aprovação nova e não provoca fallback depois de uma tentativa incerta.

A outbox confirma `SENDING` e log `PENDING` antes do HTTP. Confirmação válida resulta em `SENT`; rejeição comprovada, em `FAILED`; timeout, transporte, HTTP 5xx ou resposta incompatível, em `UNCERTAIN`, com slot reservado. Não há retry automático. Nova tentativa após falha definitiva exige prévia/aprovação e limites respeitados. `GET /api/v1/telegram/publications/{id}/logs` consulta auditoria; `POST .../{id}/recover` marca tentativa abandonada como incerta, mesmo com flags desativadas; `POST .../{id}/resolve` exige confirmação, ator e evidência (`SENT` com identificador conferido ou `NOT_SENT`). Veja os contratos no guia.

Os testes desta retomada usam preços/atores/destinos sintéticos, SQLite temporário e HTTP mockado. A regressão pertinente aprovou **424 testes, com cinco casos PostgreSQL ignorados**. O conjunto final de serviço/API manual aprovou **57 testes**, incluindo o limite de validade do cupom e o frescor configurado no banco; há sobreposição entre as execuções. Ruff e formatação passaram nos 106 arquivos Python. Os 755 testes aprovados na fase anterior permanecem históricos; a suíte integral não foi repetida nesta retomada limitada.

Os casos PostgreSQL exigem `TEST_DATABASE_URL` exclusivamente para banco `_test` e usam schemas UUID descartáveis; nunca aponte essa variável para o banco principal. A primeira tentativa local retornou `OperationalError`; após iniciar Docker, **12 testes PostgreSQL reais passaram**, incluindo fluxo de API, prévias concorrentes, despacho único concorrente com mocks e preparação do grupo. Docker Engine 29.8.2 e Compose PostgreSQL saudável foram confirmados. Alembic `upgrade head/current/check` passou na revisão **0005_telegram_outbox**, sem drift e preservando os dados existentes.

O [`ROADMAP.md`](ROADMAP.md) registra os resultados separados por execução. Antes de iniciar Docker, a regressão pertinente aprovou 334 testes com sete PostgreSQL ignorados; o conjunto final Telegram/manual aprovou 205 testes, com sobreposição. A pendência PostgreSQL e o cadastro do grupo foram resolvidos. **A suíte integral final aprovou 982 testes em 268,29 s, sem falhas/skips, incluindo os 755 anteriores e os 12 PostgreSQL reais.** Ruff, formatação e `git diff --check` passaram. Houve 17 avisos de depreciação de dependências, sem falha funcional. Não somar os resultados de execuções sobrepostas.

O código da Fase 4 está validado com mocks e PostgreSQL real; nenhuma mensagem foi enviada e a outbox principal permanece vazia. O scheduler está pausado, nove categorias foram preservadas e `Ofertas de Casa` continua desativado. O grupo Viana Ofertas permanece pausado e `unconfigured`, para seleção explícita dos filtros. Verificação do acesso real do bot e qualquer envio exigem autorização posterior. Fases 5/6 não foram iniciadas e nenhum commit final foi criado nesta execução.

## Fase 4.6 — mensagens técnicas administrativas

Após a validação de leitura do bot e a configuração do grupo para aceitar as nove categorias, o primeiro teste técnico autorizado ficou `BLOCKED`: a outbox comercial exigia produto/oferta. A extensão atual adiciona `TelegramTechnicalPublication` e auditoria própria, sem dados comerciais fictícios ou alterações nas ofertas existentes. Migração aditiva **0006_telegram_technical**, já aplicada no PostgreSQL local; `upgrade head`, `current` e `check` aprovados e conteúdo das tabelas anteriores conferido e preservado.

O prefixo administrativo local `/api/v1/telegram/technical-publications` oferece `POST /preview`, listagem/consulta, `GET /{id}/audit`, `POST /{id}/approve`, `POST /{id}/dispatch` e recuperação sem HTTP em `POST /{id}/recover`. Exige `X-API-Key`. A prévia recebe `destination_id`, `text` e `actor`; aprovação exige `destination_id`, `preview_hash`, `actor` e `confirmed=true`. O despacho exige destino/hash e confirmação, aprovação persistida, destino habilitado, allowlist e ambas as flags. Prévia e aprovação não enviam mensagens e podem ser preparadas com o grupo pausado.

Texto técnico é identificado e escapado, sem imagem, campos comerciais ou vínculos com produtos/ofertas. A chave única por conteúdo/destino permanece após qualquer resultado. Reserva e auditoria são confirmadas antes da única chamada ao publisher oficial; não há segunda tentativa, retry, fallback ou agendamento, inclusive em erro definitivo. Timeout, resposta incompatível e falha ao persistir o resultado conservam a reserva como incerta. Alterações no destino exigem nova revisão/aprovação. O fluxo comercial anterior e a janela de três horas não foram modificados.

**Estado local final:** prévia técnica **id 1**, destino **id 2 / -5309308689**, `PENDING`, sem aprovador e com **zero tentativas**. O grupo Viana Ofertas está pausado, aprovação obrigatória, `category_mode=all`; os demais destinos continuam desativados. `TELEGRAM_PUBLISHER_ENABLED=false`, `TELEGRAM_REAL_SENDS_AUTHORIZED=false`, Mercado Livre e Shopee desativados e monitoramento pausado. `.env`, tokens e credenciais preservados. **Nenhuma chamada real ao Telegram foi realizada. A autorização anterior não foi reutilizada.**

Validação final: **1.026 testes aprovados**, zero falhas/skips, preservando os 982 anteriores e acrescentando 44 casos. Execução PostgreSQL separada: **20 aprovados**, incluídos na suíte integral, em schemas UUID exclusivos de `ofertas_bot_test`, com Telegram mockado. Ruff e formatação dos 119 arquivos Python e `git diff --check` aprovados. Os 17 avisos da suíte são de depreciação de dependências. O uso inicial de `pytest.main` via entrada padrão falhou nos testes antigos de `multiprocessing` Windows; a execução final usou `python -m pytest`, sem alterar esses testes ou o OAuth. Os testes não usam o banco principal.

A [prévia exata](docs/PREVIA_TESTE_TELEGRAM.txt), os contratos e o [procedimento de nova autorização pontual](docs/TELEGRAM_FASE_4_6.md) estão documentados. Reinicie a API local para carregar as novas rotas, se houver uma instância antiga em execução. Qualquer envio futuro depende de **nova autorização explícita**, revalidação de identidade/permissões e aprovação do hash atual após preparar temporariamente o destino, com restauração dos bloqueios ao terminar. Fases 5/6 não iniciadas; nenhum commit ou push criado nesta extensão.

## Fase 4.7 — automação com despachos exclusivamente simulados

O pipeline **scheduler → provedor simulado → histórico → oportunidades/pontuação → categorias e filtros específicos → revalidação → outbox simulada → publisher oficial com MockTransport → auditoria** está integrado aos componentes anteriores. O envio técnico real da Fase 4.6 permanece preservado como `SENT/message_id=12`; nenhuma autorização daquele envio é reutilizada nesta fase.

`SimulationMonitoringRuntime` reaproveita o APScheduler e os leases/backoff do monitoramento; `SimulatedAutomation` aproveita comparação, pontuação, categorias, fila, revalidação e templates. O destino real não é habilitado. O transporte mockado usa somente token/chat sintéticos, sem ler/reutilizar a credencial Telegram real. `TelegramSimulation` registra sucesso como **SIMULATED**, separado de ofertas reais e publicações técnicas. A fila mantém deduplicação por produto/destino, três horas e observação nova; timeout/falha de persistência preservam reserva incerta, sem retry/fallback do mesmo registro.

Filtros `product_ids` ou `terms` se somam às categorias por E; produto específico ou frase normalizada correspondem entre si por OU. `max_price` e `min_discount` opcionais usam Decimal. Políticas e aprovação automática começam desativadas e só operam em banco/schema isolado com produtos inscritos e explicitamente fictícios. Lote padrão 3 (máximo 5), no máximo 5 tentativas/hora por destino e intervalo configurado preservado. Aprovação automática registra ator e hash simulados e não altera a aprovação obrigatória real. O resultado é por destino, sem invalidar a mesma oferta aprovada para outros destinos.

Rotas administrativas locais, todas com `X-API-Key`, sob `/api/v1/simulated-automation`: `GET /status`, `PUT /products/{id}`, `PUT /destinations/{id}/policy`, `GET /publications`, `GET /publications/{id}/audit`, `POST /publications/{id}/approve`, `POST /run`. O endpoint `/run` processa apenas a fila isolada pendente; não inicia consultas comerciais ou scheduler principal. O modo automático é opt-in por `SIMULATION_AUTOMATION_ENABLED=true` **somente no processo isolado**. No PostgreSQL, exige schema UUID próprio em `ofertas_bot_test`; recusa `public` e o banco principal. SQLite exige testes ou arquivo dedicado `.simulation.db`. Veja o [guia da Fase 4.7](docs/FASE_4_7_SIMULACAO.md).

Demonstração automática limitada, sem alterar o `.env` ou a configuração principal:

```powershell
.\.venv\Scripts\python.exe -m app.services.simulation_demo
```

O comando cria e preserva um schema UUID novo de `ofertas_bot_test`, aplica as migrações e usa o APScheduler real fora dos testes, com encerramento em `finally`. Na [evidência executada](docs/DEMONSTRACAO_FASE_4_7.json), PlayStation 5 fictício passou de R$ 3.000,00 para R$ 2.700,00; smartphone iPhone 15 de R$ 4.000,00 para R$ 3.600,00; furadeira de R$ 500,00 para R$ 450,00. Outros produtos fictícios exercitaram exclusão por termo, categoria e preço igual. **5 oportunidades detectadas, 2 produtos filtrados, 3 enfileirados e 3 simulados; 3 chamadas mockadas e zero reais.** Aprovações automáticas estão auditadas; repetir o despacho concluído manteve 3 chamadas, sem chamada extra. As contagens de oportunidades/filtros são de produtos distintos; revalidações de observações novas podem gerar mais eventos de aprovação que despachos.

Migração aditiva **0007_simulated_automation** aplicada no PostgreSQL principal; `upgrade head/current/check` passaram sem drift e os fingerprints do conteúdo anterior foram preservados. Não há produtos inscritos/outbox simulada no banco principal. **TELEGRAM_PUBLISHER_ENABLED=false, TELEGRAM_REAL_SENDS_AUTHORIZED=false, grupo Viana Ofertas enabled=false, Mercado Livre/Shopee desativados, monitoramento principal pausado e SIMULATION_AUTOMATION_ENABLED=false.** Nove categorias, histórico técnico e credenciais anteriores preservados. A pedido do responsável, os campos locais `SHOPEE_APP_ID` e `SHOPEE_SECRET` foram preenchidos no `.env`, ignorado pelo Git, mantendo `SHOPEE_ENABLED=false`; nenhuma API Shopee foi chamada. O mecanismo compartilhado de leases recebeu correção comprovada no psycopg: usa `RETURNING owner` porque `rowcount=-1` não comprova aquisição. A regressão anterior permanece obrigatória.

Validação anterior confirmada: **1.093 testes aprovados na suíte completa**, incluindo 29 PostgreSQL reais; depois do último ajuste de serialização da política, **mais um teste PostgreSQL novo passou isoladamente**. Ruff passou e **131 arquivos** estão formatados. A tentativa de revalidação integral final em 21/10 foi interrompida após erros de conexão: o ambiente Windows isolado atual não enxerga o Docker no PATH/caminho padrão nem o PostgreSQL em `127.0.0.1:5432`. Portanto, **a suíte completa final de 1.094 testes e os 30 PostgreSQL juntos permanecem pendentes de ambiente**, sem declaração de sucesso dessa nova execução. A validação Alembic 0007 sem drift e a preservação dos dados foram comprovadas na execução anterior; não foram reconfirmadas nesta tentativa. Acesso comercial do Mercado Livre segue suspenso pelo 403; Shopee aguarda liberação oficial. Não basta substituir o provedor para publicar dados reais: isso exige autorização/permissões próprias e nova decisão sobre operação comercial. Sem avanço às Fases 5/6 e sem commit final.


## Validação inicial Shopee — 21/10/2026

Uma única leitura oficial `productOfferV2` (página 1, limite 1) respondeu HTTP **200**, sem erros GraphQL, com um cabo HDMI relacionado a PS5 e preço informado `59`. Credenciais aceitas para essa leitura; demais permissões/quotas não foram verificadas. O conector rejeitou `productCatIds=[100535,100583,0]` por exigir IDs positivos: acesso à API comprovado, ingestão ainda bloqueada por normalização local, reproduzida offline. Nenhuma nova tentativa foi feita.

**86 testes existentes do conector passaram**, todos mockados. Sem alteração de código, credenciais, flags permanentes, categorias ou banco; zero publicações e nenhum monitoramento iniciado. Detalhes, campos reais e pendências no [relatório técnico](docs/VALIDACAO_SHOPEE_2026_10_21.md) e na [evidência sanitizada](docs/VALIDACAO_SHOPEE_2026_10_21.json). `SHOPEE_ENABLED=false` permanece obrigatório; esta verificação não autoriza operação automática nem geração de links.


## Correção pontual da normalização Shopee — 21/10/2026

`productCatIds` ignora exclusivamente o **inteiro 0** como não classificável, mantendo os IDs positivos válidos em sua ordem. Lista vazia/somente zeros resulta em nenhuma categoria externa. Booleanos, floats, negativos, string `"0"`, IDs fora do limite, estruturas inválidas e listas acima do limite continuam rejeitados pelo contrato anterior. Preço, produto, variante, links e condições comerciais não foram alterados.

A resposta sanitizada da consulta anterior está em `tests/fixtures/shopee_product_offer_observed.json`; replays mockados confirmam preço Decimal `59.00`, categorias `100535`/`100583`, comissão separada e disponibilidade/acesso `UNKNOWN`. Não houve nova consulta real. O cabo HDMI pode receber a categoria ampla Games pela palavra Gamer, mas o filtro específico `PlayStation 5` o rejeita e não cria outbox. Nenhuma categoria Console foi criada.

Validação direcionada: **179 testes aprovados** (120 Shopee e 59 de automação simulada). Ruff e formatação passaram; Alembic `upgrade head/current/check` passou em `0007_simulated_automation`, sem drift, com dados anteriores preservados. **1.129 testes aprovados na suíte integral**, incluindo **30 PostgreSQL reais** em schemas UUID isolados de `ofertas_bot_test`, **zero falhas e zero testes ignorados**. Os 179 direcionados são um subconjunto (120 Shopee e 59 simulação), não uma soma adicional. Há 17 avisos de depreciação de dependências, sem falhas. Ruff passou e 132 arquivos estão formatados; Alembic `upgrade head/current/check` passou na revisão `0007_simulated_automation`, sem drift. A pendência anterior de revalidação da Fase 4.7 está encerrada nesta execução, com Docker/PostgreSQL disponíveis.

Cupons, estoque, variante, preço anterior, parcelamento, desconto e acesso público ao preço permanecem sem comprovação. Correção de normalização não habilita elegibilidade, operação automática ou publicação. `.env`, credenciais, nove categorias, filtros e flags desativadas preservados; sem commit.


## Fase 4.8 — Shopee: coleta controlada e histórico

Pipeline integrado e auditado: **autorização administrativa por execução → reserva/lease → ShopeeConnector → ingestão/histórico → nove categorias e filtros → variação informativa/comparação verificada → candidatas dry-run**. Reutiliza o motor existente, sem outbox automática, links ou publicação. A migração aditiva **0008_shopee_collection** acrescenta execuções e candidatas e recusa downgrade com dados.

`reported_change` descreve o preço informado em observações comparáveis; `verified_change` mantém os critérios comerciais conservadores já aprovados. A primeira coleta não é queda. Estoque/acesso desconhecidos da API continuam desconhecidos; comissão não é desconto. Mesmo numa hipótese futura verificada, a oferta fica `PENDING`, sem enqueue e sem aprovação automática. Títulos ambíguos/acessórios não passam automaticamente por um filtro de console PlayStation 5.

API local autenticada sob `/api/v1/shopee-collection`: `POST /runs`, `GET /runs`, `GET /candidates`, `POST /candidates/{id}/revalidate` e `POST /schedule/prepare`. A leitura real exige UUID idempotente, responsável, autorização registrada e opt-in específico; nesta entrega **nenhuma leitura comercial adicional foi autorizada/executada**. Limites: uma página, até cinco produtos, um POST, sem retries; intervalo local conservador de 900 segundos e controle de quota/bloqueio por credencial. O agendamento é preparado pausado no APScheduler existente; não inicia polling, e sua função permanece bloqueada para consulta comercial contínua.

A [demonstração offline](docs/DEMONSTRACAO_FASE_4_8.json) reutiliza o nó sanitizado anterior e identifica separadamente um cenário hipotético fictício: **1 produto, 2 observações, 3 avaliações, nenhuma oferta/outbox, 3 chamadas mockadas, zero chamadas reais**. Replay/idempotência, falhas, filtros, variantes, histórico e concorrência são cobertos em testes. Não há ingestão de dados fictícios no banco principal.

Ver [contratos, arquitetura, API, reprodução offline e pendências](docs/FASE_4_8_SHOPEE.md). As cinco flags permanentes do briefing continuam `false`; grupo pausado, monitoramento principal desativado, `.env`/credenciais intactos, sem avanço às Fases 5/6 ou commit. **1.183 testes aprovados**, incluindo **38 PostgreSQL reais** em schemas UUID isolados de `ofertas_bot_test`; zero falhas e zero ignorados. Os 1.129 anteriores foram preservados e 54 novos acrescentados (46 offline e oito PostgreSQL). Há 17 avisos de depreciação de dependências, sem falhas. Ruff e formatação de 140 arquivos passaram; Alembic `upgrade head/current/check` passou em `0008_shopee_collection`, sem drift. Dados anteriores preservados por fingerprints; novas tabelas principais vazias. [Evidência da validação](docs/VALIDACAO_FASE_4_8.json).


## Fase 4.9 — leitura real autorizada e preparação bloqueada

Em 21/10/2026, uma única `productOfferV2` para PlayStation 5 (página 1, limite 1) respondeu **HTTP 200**, sem retries/fallback. Retornou cabo HDMI de R$ 59,00, não console: classificação Games, mas filtro específico recusado. Produto id 1 e histórico oficial id 1 persistidos como referência **INITIAL**, sem queda/percentual inventados. Estoque e acesso público ao preço permanecem UNKNOWN e não existe link próprio cadastrado; **nenhuma oferta ou prévia comercial criada/aprovada**, zero chamadas Telegram e zero mutations.

Reserva/auditoria existentes utilizadas; flags permanentes continuam false, grupo pausado, monitoramento desativado e `.env` intacto. A autorização de leitura está consumida. [Relatório completo](docs/FASE_4_9_SHOPEE_TELEGRAM.md) e [evidência sanitizada](docs/VALIDACAO_FASE_4_9.json). Nenhum novo commit ou avanço às Fases 5/6.


## Fase 4.10 - motor de descoberta Shopee offline

Descoberta por termos nas nove categorias existentes, rotacao conservadora, relevancia por intencao, exclusoes, lotes/paginas limitados, selecao priorizada para reconsulta e avaliacao de candidatas. Reutiliza conector/registry, coleta 4.8, historico Decimal, classificacao, OfferEngine, leases, fila simulada/templates e APScheduler. Migracao aditiva **0009_shopee_discovery**; configuracao e auditoria administrativas em `/api/v1/shopee-discovery`, com autenticacao existente.

Execucao exige banco isolado e `httpx.MockTransport`; a API normal nao tem transporte mockado e bloqueia execucoes. Tres jobs preparados ficam pausados e seus callbacks comerciais retornam BLOCKED. A previsao `MOCK_WOULD_APPROVE` so existe no laboratorio; nao aprova ofertas reais nem publica. Estoque/acesso publico/variante/link proprio nao comprovados bloqueiam a preparacao.

Demonstracao automatica limitada: `.\.venv\Scripts\python.exe -m app.demo_shopee_discovery`. Resultado: **27 produtos ficticios distintos, 18 selecionados, 36 observacoes/candidatas, 9 filtrados e 9 duplicados; 36 requisicoes mockadas; zero consultas comerciais e zero publicacoes reais**. Precos ficticios 100.00/90.00; nenhuma candidata aprovada/publicavel por falta de comprovacao comercial. As cinco flags reais continuam false e o grupo Viana Ofertas permanece pausado. `.env`, dados e aprovacoes anteriores preservados; sem commit ou Fases 5/6.

[Arquitetura, configuracao, API, limites e pendencias](docs/FASE_4_10_GARIMPO_SHOPEE.md); [demonstracao executada](docs/DEMONSTRACAO_FASE_4_10.json). **1.267 testes aprovados**, incluindo **49 PostgreSQL reais**, zero falhas e zero ignorados; os 1.183 anteriores foram preservados e 84 novos acrescentados (73 offline e 11 PostgreSQL). Revalidacao dos 84 novos testes apos o ajuste final de contexto de categoria: todos aprovados. Contagens sobrepostas, nao somar. Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao passaram nos 150 arquivos Python; Alembic upgrade/current/check em 0009_shopee_discovery, sem drift. Fingerprints das 27 tabelas anteriores e .env identicos apos os testes; nove categorias ativas, grupo pausado, monitoramento desativado, cinco flags false e quatro tabelas novas de producao vazias. Zero novas consultas comerciais, mutations de afiliado ou publicacoes reais. Sem commit/push ou Fases 5/6. [Validacao sanitizada](docs/VALIDACAO_FASE_4_10.json).


## Adendo 4.10 - preferencias operacionais dinamicas

AppSetting agora persiste preferencias globais e overrides por destino, com revisao/ator/auditoria e recarga em cada operacao. Dias/janelas/fuso IANA, frequencias de descoberta/reconsulta/avaliacao, limiares normais/excepcionais, modo caca-bugs, categorias/produtos prioritarios e limites sao editaveis sem alterar codigo ou reiniciar o backend. Valores iniciais em app/discovery_operation_defaults.json; filtros de destinos anteriores continuam aplicaveis. Frequencias pertencem ao coletor compartilhado; janelas, limiares e limites de preparacao possuem overrides independentes por destino.

GET/PATCH `/api/v1/shopee-discovery/preferences` e `/api/v1/shopee-discovery/destinations/{id}/preferences`, com autenticacao administrativa. PATCH exige actor, expected_revision e changes. Revisao obsoleta ou ciclo em andamento: 409. `null` em override restaura heranca. Jobs preparados atualizam intervalos preservando pausa e callbacks bloqueados; outros processos recarregam pelo heartbeat existente. Nenhuma preferencia concede autorizacao comercial ou publica ofertas reais. Cinco flags false e grupo pausado; sem migracao nova.

[Relatorio completo da Fase 4.10 e do adendo](docs/RELATORIO_FASE_4_10_E_ADENDO_CONFIGURACOES.md). **1.341 testes aprovados**, incluindo **58 PostgreSQL reais**, zero falhas/skips; os 1.267 anteriores foram preservados e 74 acrescentados (65 offline e nove PostgreSQL). Ha 17 avisos de depreciacao de dependencias. Ruff e formatacao aprovados nos 154 arquivos Python; Alembic current/check em 0009_shopee_discovery, sem drift e sem migracao nova. Fingerprints das 31 tabelas anteriores e .env identicos apos a suite; nove categorias ativas, grupo pausado, monitoramento desativado e cinco flags false. Zero consultas comerciais, mutations de afiliado ou publicacoes reais. Nenhum commit/push ou Fases 5/6. [Evidencia sanitizada](docs/VALIDACAO_ADENDO_FASE_4_10.json).


### Ajuste offline do validador de links Shopee

O parser do conector reconhece `/product/SHOP_ID/ITEM_ID`, o slug convencional
`/NOME-i.SHOP_ID.ITEM_ID` e especificamente `/universal-link/NOME-i.SHOP_ID.ITEM_ID`.
A identidade é comparada com a URL original; domínio permitido, IDs positivos,
caminho completo e ausência de ambiguidades continuam obrigatórios. Formatos
profundos desconhecidos, fragmentos e IDs divergentes são rejeitados sem resolver links.

`AffiliateLinkResult.product_identity_verified` informa apenas a correspondência
do produto/variante. `account_verified` permanece `false`: identidade e sucesso da
mutation não comprovam propriedade, comissão ou atribuição de conversões.
A rota de geração mantém o bloqueio de associação automática sem essa comprovação.
O `long_url` interno preserva exatamente o valor retornado, incluindo ordem,
repetições e codificação dos parâmetros; fica excluído do `repr` do resultado.
O cadastro manual existente também preserva a URL integral. Não incluir esses
parâmetros em logs, relatórios ou mensagens de erro.

O ajuste não reexecuta a mutation, não reprocessa sua autorização consumida,
não altera a auditoria histórica e não ativa integrações, scheduler ou publicações.
Veja [a validação offline e as pendências](docs/AJUSTE_VALIDADOR_LINKS_SHOPEE.md).


### Geracao de links integrada ao laboratorio de candidatas

`ShopeeDiscoveryService` aceita injecao explicita de `AffiliatePipelineService`.
O fluxo reutiliza a avaliacao existente antes da geracao e repete a revalidacao
antes da preparacao da mensagem: descoberta/historico -> filtros/queda comparavel ->
reserva duravel -> generateShortLink **mockado** -> identidade/atribuicao ->
revalidacao -> fila simulada PENDING e previa com hash.

`affiliate_links_simulation_enabled=false` e
`max_affiliate_links_per_evaluation=1` sao preferencias versionadas em AppSetting,
com overrides por destino nas rotas administrativas de preferencias ja existentes.
O limite aceita inteiros de 0 a 10; 0 impede geracao. O bootstrap operacional nao
injeta gerador algum. Mudar essas preferencias nunca habilita rede, scheduler,
conectores reais ou publisher. O adaptador fornecido exige `httpx.MockTransport`,
banco de laboratorio e todas as flags reais desligadas.

Reservas `shopee_link_mock:<sha256>` usam produto, identidade/variante, origem exata,
destino e operacao. AppSetting conserva links completos e o hash do payload;
IntegrationEvent registra RESERVED/CLAIMED e resultado sem URLs de rastreamento.
Timeout, HTTP 5xx e falha de persistencia ficam UNCERTAIN, sem retry, retomada da
mutation ou limpeza automatica da reserva. Falhas definitivas tambem nao repetem.
Consultas administrativas de execucoes recebem apenas metadados sanitizados.

Identidade valida sem atribuicao comprovada resulta em GENERATED sem associacao
a AffiliateLink nem preparacao publicavel. Links manuais nao sao sobrescritos.
Link previamente aprovado permite somente previa simulada; limites, aprovacao real,
janela minima de tres horas e observacao nova continuam obrigatorios. O modo
excepcional nao ignora a janela de funcionamento nem comprova erro de preco.

Nao ha nova migration: os registros e locks existentes foram reutilizados.
Detalhes, testes e condicoes para eventual ativacao futura estao no
[relatorio da integracao](docs/INTEGRACAO_LINKS_PIPELINE_SHOPEE.md).


### Auditoria de prontidao Shopee — 25/10/2026

[Auditoria tecnica/documental, matrizes e plano de ativacao](AUDITORIA_PRONTIDAO_SHOPEE.md).
O pipeline comercial automatico continua nao liberado: faltam evidencias de preco
publico/estoque/variante e vinculo da conta, confirmacao de quotas/escopo e adaptacao
aprovada da ponte para a outbox comercial, hoje baseada em ofertas manuais.
Foram registrados os gaps do validador manual universal-link e da reserva na rota
legada, sem alterar codigo. Nenhuma chamada autenticada Shopee ou publicacao foi
feita. A ultima suite registrada permanece 1.445/72 PostgreSQL; nao foi repetida
nesta auditoria. Flags locais false; PostgreSQL indisponivel para reconferencia
operacional nesta data. Nenhuma nova implementacao ou autorizacao decorrente da auditoria.


### Correções pós-auditoria Shopee — 25/10/2026

O parser estrito agora é compartilhado pelo conector e pelo cadastro manual,
incluindo `/universal-link/`, IDs e variantes compatíveis, sem reescrever tracking.
A rota legada de geração retorna `409 REAL_GENERATION_BLOCKED`: ainda não há
adaptador real com autorização durável integrada. O laboratório conserva suas
reservas consumíveis uma vez e transporte mockado; interrupções ficam UNCERTAIN.

Novas APIs administrativas em `/api/v1/products/{id}/shopee` registram
`link-evidence`, `commercial-evidence` e `trust-proposals`; `evidence-readiness`
consulta lacunas. Documentos em AppSetting e auditoria IntegrationEvent separam
identidade, geração, proveniência da conta, vínculo, conversão e comissão.
Políticas ficam PROPOSED e não ativam integrações, aprovam links/ofertas ou mudam
`account_verified`. Não é obrigatório comprovar uma venda anterior em toda política.
Atestados comerciais referenciam o histórico original, sem reclassificar cotações
automáticas como verificação manual. Comparações e queda histórica carregada
agora também distinguem source_type, faixa e base comercial.

[Correções, política proposta, APIs, arquivos e validações](docs/CORRECOES_POS_AUDITORIA_SHOPEE.md).


### Piloto documental do Cockpit Brave Bull — sem envio

Docker Linux e PostgreSQL restabelecidos no ambiente: **80 testes PostgreSQL
aprovados** em schemas UUID de ofertas_bot_test; Alembic em 0009 sem drift.
Banco operacional lido em modo READ ONLY, grupo pausado e bloqueios preservados.

A avaliação comercial permanece **BLOCKED**: o Cockpit existe como referência da
mutation histórica, sem cadastro/observações de preço no banco. Faltam condições
comerciais e comprovação de conta/vínculo com política aprovada. O shortLink oficial
está preservado; o longLink histórico possui um parâmetro sanitizado e não deve
ser reconstruído ou usado como URL integral de compra. Nenhuma mutation foi repetida.

[Diagnóstico, proposta de confiança e dados faltantes](docs/PILOTO_COMERCIAL_COCKPIT_SHOPEE.md).
Sem oferta/prévia/outbox criada, chamadas comerciais, publicação ou ativação.


### Evidência visual do Cockpit — somente documental

Captura fornecida pelo responsável em 25/10/2026, com relógio 21h11, preservada
com hash. Os preços R$ 956,13 (Pix com cupom) e R$ 1.099,00 (outros pagamentos
sem cupom) são valores exibidos, sem confirmação de aplicação. Variante,
disponibilidade e condições efetivas continuam pendentes; diagnóstico BLOCKED,
sem registro no banco, aprovação, consulta externa ou publicação.

[Captura preservada e diagnóstico comercial](docs/EVIDENCIA_VISUAL_COCKPIT_BRAVE_BULL_20261025.md).


### Primeiro cadastro documental do Cockpit — autorizado, sem oferta

Produto operacional ID **2**, com observações **2** (R$ 1.099,00 sem cupom/outros
pagamentos) e **3** (R$ 956,13 no Pix com cupom), em fontes/contextos distintos.
Captura de 25/10/2026, 21h11 Brasília, preservada por referências e hashes.
Disponibilidade UNKNOWN, acesso ao preço UNKNOWN/CONDITIONAL, sem comprovação
pública ou queda histórica. Um produto com escopo de variante não confirmado;
monitoramento desativado. Reexecução idempotente sem novas linhas.

[Cadastro, IDs, proveniência e verificações](docs/CADASTRO_DOCUMENTAL_COCKPIT_SHOPEE.md).
Nenhum link, oferta, aprovação ou outbox criado. Avaliação comercial BLOCKED;
integrações e publicações continuam desativadas.

### Fontes comerciais automatizáveis — proposta documental de 26/10/2026

O contrato local documenta preço/faixa, desconto anunciado e período em
`productOfferV2`, mas não variante, estoque ou preço público aplicável. Os feeds
retornam `columns` em JSON sem dicionário local; suas capacidades comerciais e
permissões ainda precisam de confirmação oficial. Proposta mínima: reconsulta por
identidade, histórico informativo com contexto preservado e bloqueios comerciais
mantidos. Nenhuma implementação ou ativação decorrente deste estudo.

[Matriz, ajustes mínimos e plano de leituras futuras autorizadas](docs/FONTES_COMERCIAIS_AUTOMATIZAVEIS_SHOPEE.md).
[Extrato do contrato público local e proveniência](docs/CONTRATO_FONTES_SHOPEE_20261026.json).
Zero chamadas autenticadas, acessos ao banco operacional ou publicações;
produto 2 e observações 2/3 preservados. Quotas/escopo e nova autorização são
pré-requisitos para coleta real, inclusive recorrente. Sem commit/push.

### Monitoramento observacional por identidade — 26/10/2026

O serviço de coleta agora aceita um escopo tipado de anúncio conhecido e reutiliza
`ShopeeConnector.fetch_product` com shopId/itemId, página 1 e limite 1. A execução
é exclusiva de MockTransport e banco isolado. A interface administrativa
`POST /api/v1/shopee-collection/observations` valida o corpo e retorna 409 para
execução real, mesmo com credenciais/flags; não há adaptador real ou novo scheduler.
A descoberta por palavra-chave permanece disponível no caminho anterior.

Preço/faixa em Decimal, desconto anunciado e períodos Int64 sem conversão temporal
são preservados com contexto estruturado e `price_basis` validado. O contexto não
inclui preço, horário, percentual ou comissão. Observações documentais e oficiais,
variantes/condições incompatíveis continuam separadas; cotação não comprova promoção.
O modo observacional mantém disponibilidade/acesso UNKNOWN, commercial_verified=false
e veda elegibilidade no OfferEngine, links, aprovação e outbox real.

[Funcionamento, arquivos, segurança e limitações](docs/MONITORAMENTO_OBSERVACIONAL_SHOPEE.md).
[Payload futuro do Cockpit, não executado](docs/PAYLOAD_OBSERVACIONAL_COCKPIT_SHOPEE.json).
Produto operacional 2 e históricos 2/3 preservados; permissões/quotas, fonte das
condições comerciais e nova autorização continuam pendentes. Sem commit/push.

Validação desta entrega: **1.593 testes aprovados** (1.500 offline + 93 PostgreSQL
reais em schemas UUID de ofertas_bot_test), sem falhas/ignorados. Ruff/formatação
passaram nos 175 arquivos Python; Alembic current/check em 0009 no PostgreSQL
isolado, sem drift. Dados operacionais examinados e bloqueios preservados;
zero chamadas reais. [Evidência dos resultados](docs/VALIDACAO_MONITORAMENTO_OBSERVACIONAL_SHOPEE.json).

### Adaptador Shopee de leitura única — preparação de 30/10/2026

Novas rotas administrativas em `/api/v1/shopee-collection/identity-reads` separam
plano, reserva PREPARED, aprovação explícita do hash/validade e execução única.
Reutilizam conector/assinatura, coleta, AppSetting, leases e auditoria, sem migração
ou scheduler novos. O claim durável é consumido antes do HTTP e não admite retry,
fallback, paginação extra ou reutilização por outra chamada/autorização.

O adaptador devolve a cotação somente na primeira resposta confirmada; armazena
apenas status/contagem/código HTTP/hash técnico, **sem gravar preços no histórico
ou em JSON de auditoria**. Flags não autorizam acesso; registros de laboratório
MOCK_ONLY não podem ser promovidos a reais. A rota anterior `/observations` continua
bloqueada para execução real.

[Implementação, limites e procedimento posterior](docs/PILOTO_LEITURA_UNICA_SHOPEE_20261030.md).
[Plano do Cockpit, ainda sem autorização/reserva](docs/PLANO_LEITURA_UNICA_COCKPIT_SHOPEE_20261030.json).
Esta entrega executa somente desenvolvimento/testes locais: nenhum HTTP Shopee,
mutation, publicação, ativação, alteração de credencial ou commit/push.

Validação: **437 testes distintos** (328 offline e 109 PostgreSQL reais), incluindo
53 novos; Ruff e formatação passaram nos 178 arquivos. Alembic current/check em
0009 nos schemas de testes, sem drift. A suíte offline anterior não foi repetida
integralmente; foram executadas as regressões relevantes e todos os PostgreSQL.
Produto 2, históricos 2/3, configurações, destino e .env preservados; nenhuma
autorização real registrada/consumida. [Resultados](docs/VALIDACAO_LEITURA_UNICA_SHOPEE_20261030.json).

### Ingestão da cotação histórica — preparação, 30/10/2026

O objeto quote da primeira leitura real confere com a auditoria persistida.
Preparação OFFICIAL_API de BRL 1099.00 conserva o horário original, UNKNOWN/UNKNOWN,
percentual anunciado 31 e contexto oficial independente dos documentos MANUAL.
Após autorização específica, a ingestão histórica operacional foi concluída:
observação oficial ID 4 e auditoria ID 14, preservando os históricos manuais 2/3.
O fluxo genérico de ingestão operacional permanece bloqueado.

102 testes selecionados aprovados (85 offline e 17 PostgreSQL), Ruff/formatação
passaram; nenhuma consulta real adicional, alteração de flags ou scheduler.
[Integridade, plano conservador e autorização necessária](docs/INGESTAO_HISTORICA_E_MONITORAMENTO_SHOPEE_20261030.md).
[Validação](docs/VALIDACAO_INGESTAO_HISTORICA_SHOPEE_20261030.json).

### Monitoramento recorrente — preparação offline

`ShopeeRecurringMonitor` reutiliza leitura única, reservas, histórico, comparação e
scheduler existentes. Autorização explícita por conta/produto, validade, orçamento
e intervalo; cada ciclo possui claim próprio e não admite retry. Falhas e incerteza
pausam os ciclos. Preferências são dinâmicas e não concedem autorização.

Somente laboratório isolado com MockTransport; nenhum job operacional instalado.
Proposta para o produto 2: 6h, piloto de 24h, no máximo 4 POSTs, sujeita a confirmação
oficial de permissões/quotas e nova autorização. Flags e grupo permanecem desativados.
[Controles, proposta e autorização necessária](docs/MONITORAMENTO_RECORRENTE_SHOPEE_20261030.md).

### Piloto experimental de duas leituras — somente preparado

Exceção operacional limitada preparada em PREPARED, sem aprovação ou HTTP:
UUID `1cfbb654-f4ab-4146-b185-e9f48e702540`, produto 2, até dois POSTs, intervalo
mínimo 6h e janela de 24h. Rotas administrativas de preparação/status/aprovação e
despacho por ciclo exigem autenticação, hash e ciência explícita das quotas ainda
desconhecidas. A recorrência geral permanece bloqueada. Nenhum job foi ativado.
111 testes distintos aprovados (74 offline, 37 PostgreSQL isolados), Ruff,
formatação e Alembic current/check passaram. Flags continuam false e grupo pausado.
[Escopo exato e aprovação futura](docs/PILOTO_EXPERIMENTAL_RECORRENCIA_SHOPEE_20261030.md).

### Comparação multiloja Shopee — offline

Perfil `multistore` de descoberta inclui dados opcionais de lojas, sem alterar o
payload padrão ou consultas por identidade já aprovadas. A comparação exige
equivalência documentada de marca/modelo/kit/quantidade/condição/variante e condições
de preço; sem prova, apenas candidatos. Deduplica lojas por shopId, ordena preços
anunciados Decimal e mostra diferenças/empates, sem confundir com queda histórica.
Não exige cupom e não declara preço final ou reputação do vendedor.

216 testes distintos aprovados (205 offline e 11 PostgreSQL isolados), Ruff,
formatação e Alembic current/check passaram. Busca real separada somente PREPARED,
sem aprovação ou dispatcher real habilitado. Flags, históricos e piloto de amanhã
preservados. [Implementação, exemplo e limitações](docs/IMPLEMENTACAO_MULTILOJA_SHOPEE_20261031.md).
