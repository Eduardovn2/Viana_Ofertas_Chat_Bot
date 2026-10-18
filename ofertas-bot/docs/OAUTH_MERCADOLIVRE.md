# OAuth do Mercado Livre no Windows

Este guia prepara uma autorização oficial da própria conta pelo navegador. O callback HTTPS passa por um Cloudflare Quick Tunnel temporário até um gateway local exclusivo. A API administrativa continua acessível somente em `127.0.0.1:8000`, com `X-API-Key`.

O OAuth é opcional e começa desativado. Client ID e Client Secret precisam ser configurados **localmente**, a partir de uma aplicação própria no DevCenter. Não compartilhe esses valores, tokens, códigos, cookies ou URLs de lançamento no chat, nos logs ou em capturas de tela. A autorização não comprova permissão para consultar preços de terceiros. O token armazenado ainda não é usado pelo provedor de preços, e não há publicação real.

## Configuração e limites

| Variável | Padrão do exemplo | Uso |
| --- | --- | --- |
| `MERCADOLIVRE_OAUTH_ENABLED` | `false` | Permite iniciar a autorização somente após configuração e decisão local do administrador. |
| `MERCADOLIVRE_CLIENT_ID` | vazio | App ID da aplicação própria no DevCenter. |
| `MERCADOLIVRE_CLIENT_SECRET` | vazio | Secret da mesma aplicação; permanece no servidor local. |
| `MERCADOLIVRE_REDIRECT_URI` | vazio | URI HTTPS exata do callback; preparada pelo script do túnel. |
| `MERCADOLIVRE_PKCE_ENABLED` | `true` | Usa PKCE com `S256`; mantenha o recurso habilitado também no DevCenter. |
| `MERCADOLIVRE_OAUTH_STATE_TTL_SECONDS` | `600` | Prazo da tentativa de autorização; o padrão corresponde a dez minutos. |

Os nomes `MERCADOLIVRE_*` deste fluxo são independentes de `MERCADO_LIVRE_ENABLED` e `MERCADO_LIVRE_ACCESS_TOKEN`, usados pelo conector comercial anterior. Preserve os valores existentes. `PRICE_PROVIDER=simulated`, Mercado Livre/Shopee desativados e o destino `Ofertas de Casa` desativado continuam sendo a configuração conservadora.

As operações OAuth releem as configurações locais a cada requisição. Preencher Client ID, Secret e a flag OAuth depois de preparar o callback não exige reiniciar API, gateway ou túnel. Isso não recarrega as flags comerciais do registry: mudanças de configuração comercial seguem o procedimento de reinício descrito no README.

## 1. Preparar o callback temporário

Use o PowerShell do Windows com o ambiente Python do projeto preparado. Tenha um executável Windows amd64 de `cloudflared` obtido e verificado a partir das fontes oficiais. O [catálogo de downloads](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/) aponta para os releases oficiais; o [release 2026.10.1](https://github.com/cloudflare/cloudflared/releases/tag/2026.10.1), consultado em 18/10/2026, publica o SHA256 por arquivo. Compare o arquivo baixado com o checksum de sua versão usando `Get-FileHash -Algorithm SHA256`. Não execute um arquivo cuja verificação falhe.

Na pasta do projeto:

```powershell
Set-Location 'C:\Users\eduardo.viana.3\Documents\ChatGPT\Chat_BOT\ofertas-bot'
.\scripts\start_oauth_tunnel.ps1
```

Se o executável não estiver no PATH, informe seu caminho real pelo parâmetro `-CloudflaredPath` do script. O caminho deve apontar para o arquivo já verificado; não use o caminho ilustrativo como se existisse no seu computador.

O script não exige credenciais OAuth nem a flag habilitada para preparar o callback. Ele inicia o gateway em segundo plano, com logs de acesso desabilitados, inicia o túnel e informa a **URI pública exata**. Também atualiza somente `MERCADOLIVRE_REDIRECT_URI` no `.env`, preservando as demais variáveis, e registra processos/URL em `.runtime/oauth-tunnel.json`. `.runtime` e `.tools` são artefatos locais ignorados pelo Git. Não copie o `.env` inteiro para verificar a URI.

Se um túnel gerenciado ainda estiver ativo, o script mostra a URI existente. Preserve esse processo durante cadastro e autorização. A [documentação dos Quick Tunnels](https://developers.cloudflare.com/tunnel/get-started/quick-tunnels/) informa que o hostname muda a cada novo túnel, a URL para de funcionar quando o processo termina e o serviço é destinado a desenvolvimento, sem garantia de disponibilidade. O [changelog oficial](https://developers.cloudflare.com/changelog/post/2026-03-19-wrangler-tunnel-commands/) confirma a modalidade gratuita e temporária sem conta Cloudflare.

## 2. Configurar a aplicação no DevCenter

1. Abra sua aplicação no DevCenter oficial do Mercado Livre, usando a própria conta.
2. Cadastre a URI HTTPS **exatamente** como informada pelo script e gravada no `.env`, terminando em `/oauth/mercadolivre/callback`. Não acrescente `launch`, `code`, `state`, query string ou fragmento ao cadastro.
3. Habilite PKCE na configuração da aplicação e mantenha `MERCADOLIVRE_PKCE_ENABLED=true`. Selecione apenas as permissões necessárias ao uso autorizado.
4. Edite localmente o `.env` para preencher `MERCADOLIVRE_CLIENT_ID` e `MERCADOLIVRE_CLIENT_SECRET`. Depois de decidir iniciar a autorização, altere `MERCADOLIVRE_OAUTH_ENABLED=true`.

```powershell
notepad .env
```

Não há hostname fictício para copiar neste guia: use a URI da execução atual. O [cadastro oficial de aplicações](https://developers.mercadolivre.com.br/en_us/category-prediction-resource/register-your-application) exige HTTPS. A [documentação OAuth](https://developers.mercadolivre.com.br/pt_br/mensagens-post-venda/autenticacao-e-autorizacao) exige correspondência exata entre URI cadastrada, autorização e troca do código.

## 3. Iniciar a API e autorizar pelo navegador

Inicie a API principal com o bind local habitual, se ela ainda não estiver ativa:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Em outro PowerShell, prepare a chave administrativa sem colocá-la no histórico dos comandos. O status pode ser consultado antes da autorização e mostra somente a presença da configuração e metadados do armazenamento:

```powershell
$oauthBase = 'http://127.0.0.1:8000/api/v1/oauth/mercadolivre'
$secureApiKey = Read-Host 'Informe ADMIN_API_KEY do seu .env' -AsSecureString
$oauthHeaders = @{ 'X-API-Key' = [System.Net.NetworkCredential]::new('', $secureApiKey).Password }
Remove-Variable secureApiKey
Invoke-RestMethod -Uri "$oauthBase/status" -Headers $oauthHeaders
```

Solicite uma tentativa e abra a URL devolvida diretamente no navegador. Não use `Format-List`, `Write-Output` ou impressão do objeto de autorização: a `launch_url` contém um ticket descartável.

```powershell
$authorization = Invoke-RestMethod -Method Post -Uri "$oauthBase/authorize" -Headers $oauthHeaders
Start-Process -FilePath $authorization.launch_url
Remove-Variable authorization
```

Com a flag desativada ou Client ID/Secret pendentes, `/authorize` responde HTTP 409; complete a configuração local antes de iniciar outra tentativa. A resposta de sucesso contém `launch_url` e `expires_in`, sem tokens. Conclua a autorização no mesmo navegador aberto por esse lançamento. Use o login e o consentimento oficiais; o projeto não recebe a senha da conta.

O lançamento acessa o próprio callback HTTPS com um ticket de uso único, instala um cookie `Secure`, `HttpOnly`, `SameSite=Lax` e redireciona ao Mercado Livre com `state` e PKCE `S256`. O retorno só é aceito com o estado e o cookie correspondentes, antes do vencimento. Alterar Client ID ou URI depois de iniciar uma tentativa invalida sua associação. Tentativas expiradas ou já consumidas exigem um novo `/authorize`; não tente repetir manualmente a URL do callback.

O servidor troca o código por tokens mediante POST form para `https://api.mercadolibre.com/oauth/token`, incluindo o verificador PKCE e a URI vinculada à tentativa. A troca não usa retry automático. Client Secret e tokens não são devolvidos ao navegador. O [fluxo oficial](https://developers.mercadolivre.com.br/pt_br/mensagens-post-venda/autenticacao-e-autorizacao) descreve os parâmetros, e a [documentação de autenticação](https://developers.mercadolivre.com.br/en_us/events/authentication-and-authorization) atribui ao aplicativo a validação de `state`.

## 4. Verificar armazenamento e encerrar o túnel

Depois da conclusão no navegador, consulte somente o endpoint administrativo de status:

```powershell
Invoke-RestMethod -Uri "$oauthBase/status" -Headers $oauthHeaders
Remove-Variable oauthHeaders, oauthBase
.\scripts\stop_oauth_tunnel.ps1
```

Confira a indicação de token armazenado e sua expiração. A API de status não retorna access token, refresh token, Secret, verificador, cookie ou ticket. Presença no armazenamento não prova acesso comercial nem garante que uma credencial não tenha sido revogada.

Os tokens e o verificador da tentativa são protegidos por **DPAPI do usuário Windows atual** no SQLite privado `%LOCALAPPDATA%\VianaOfertasChatBot\oauth\mercadolivre.sqlite3`, com ACL restrita ao usuário atual e SYSTEM. Esse arquivo fica fora do repositório e do PostgreSQL de produtos. Não exporte nem abra seu conteúdo para diagnóstico. Copiar o banco para outro usuário/computador não constitui uma migração de credenciais suportada. O token não é copiado para `.env` e ainda não é injetado no provedor de preços. Renovação e rotação de refresh token não foram implementadas nem testadas.

O script de parada verifica PIDs e o horário de início registrados no manifesto antes de encerrar os processos gerenciados; ele preserva os dados. Encerrar túnel/gateway não revoga tokens já concedidos no Mercado Livre. Se voltar a executar o script de início após a parada, atualize no DevCenter a nova URI antes de autorizar outra vez.

## Superfície exposta e diagnóstico

O túnel aponta somente para `127.0.0.1:8787`. O gateway aceita apenas o callback no host configurado, não oferece Swagger, ReDoc/OpenAPI ou acesso às rotas administrativas. A aplicação principal continua fechada em `127.0.0.1:8000`. O callback presente na aplicação principal não transforma a API inteira em um serviço público. A URL temporária é pública; não a trate como senha.

- **Configuração pendente:** consulte `/status`, complete os campos localmente e habilite apenas a flag OAuth desejada.
- **URI divergente:** compare a URI informada pelo script com o cadastro do DevCenter e o `.env`; não altere parâmetros para tentar compensar a diferença.
- **Tentativa inválida/expirada ou cookie ausente:** solicite outro `/authorize` e use o mesmo navegador desde o lançamento. Não reutilize um retorno anterior.
- **Falha no túnel:** confira seu estado com o script de início; um túnel novo implica um hostname novo. Não abra a porta 8000 para contornar o problema.
- **Falha na proteção local:** use o mesmo usuário Windows e confirme acesso à pasta privada. Não substitua DPAPI por armazenamento de tokens em texto puro.
- **Diagnóstico compartilhado:** informe somente a classe/código sanitizado do erro e a presença dos campos; nunca envie respostas brutas da troca OAuth, o banco privado ou URLs com `code`, `state` ou `launch`.

## Validação e estado desta entrega

O roteiro automatizado pode ser executado sem credenciais comerciais reais:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -k oauth
.\tests\test_oauth_tunnel_processes.ps1
```

Os testes controlados verificam configuração desativada, autenticação administrativa, validade/uso único das tentativas, associação com cookie e PKCE, respostas sanitizadas, proteção do armazenamento e isolamento do gateway. Eles não substituem consentimento no navegador nem comprovam permissões comerciais de uma conta real.

**Autorização real concluída em 18/10/2026 às 17:28:59 America/Sao_Paulo (20:28:59 UTC)**, mediante consentimento do proprietário no navegador. A execução confirmou:

- Lançamento autorizado pela API local, vínculo com o navegador, validação de `state` e PKCE S256.
- Troca do código no endpoint oficial e armazenamento dos tokens por DPAPI CurrentUser, com ACL restrita ao usuário atual e SYSTEM.
- Consumo dos registros de lançamento e estado, impedindo a reutilização da tentativa concluída.
- Expiração válida, fingerprint do Client ID compatível e ausência dos tokens em texto puro nos blobs cifrados inspecionados.

Após a validação, `MERCADOLIVRE_OAUTH_ENABLED` foi restaurada para `false`. `MERCADO_LIVRE_ENABLED=false`, `SHOPEE_ENABLED=false` e `PRICE_PROVIDER=simulated` foram preservados. A autorização não foi seguida por consulta comercial nem publicação. Os tokens permanecem fora do provedor de preços; o uso comercial autorizado e a renovação/rotação de refresh token continuam pendentes.

A suíte completa final aprovou **699 testes em 64,61 segundos**, incluindo quatro testes PostgreSQL reais em schemas isolados de `ofertas_bot_test`, com um `StarletteDeprecationWarning`. O aviso conhecido de terceiros não causou falhas. Ruff e formatação aprovaram os **88 arquivos Python**, e `git diff --check` passou. Esse resultado abrange a aplicação e o acréscimo OAuth, sem substituir o registro de consentimento real acima.

A revisão complementar aprovou **19 verificações nativas PowerShell** com processos sintéticos e portas dinâmicas. O script verifica que o listener pertence ao gateway antes de criar o túnel; aceita o filho Python legítimo da `.venv`, rejeita serviços alheios e encerra/aguarda somente a árvore de processos validada. O teste não inicia Quick Tunnel, não lê o `.env` e preserva o túnel real existente.

O manifesto `.runtime/oauth-tunnel.json` e a variável local `MERCADOLIVRE_REDIRECT_URI` são as referências da URI efetivamente preparada. O hostname temporário, códigos, tickets, tokens e identificadores privados não são fixados neste registro.
