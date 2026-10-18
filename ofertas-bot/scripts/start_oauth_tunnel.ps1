param(
    [string]$CloudflaredPath = '',
    [ValidateRange(1024, 65535)][int]$Port = 8787,
    [switch]$RestartGateway
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeDir = Join-Path $projectRoot '.runtime'
$manifestPath = Join-Path $runtimeDir 'oauth-tunnel.json'
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw 'Python local .venv nao encontrado.' }
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null

. (Join-Path $PSScriptRoot 'oauth_tunnel_processes.ps1')

function Start-CallbackGateway([int]$GatewayPort) {
    $gateway = Start-Process -FilePath $pythonExe -WindowStyle Hidden -PassThru -WorkingDirectory $projectRoot -ArgumentList @(
        '-m', 'uvicorn', 'app.oauth_gateway:app', '--host', '127.0.0.1', '--port', [string]$GatewayPort, '--no-access-log', '--log-level', 'warning', '--no-server-header'
    ) -RedirectStandardOutput (Join-Path $runtimeDir 'oauth-gateway-out.log') -RedirectStandardError (Join-Path $runtimeDir 'oauth-gateway-err.log')
    try {
        Wait-CallbackGateway $gateway $GatewayPort
        return $gateway
    } catch {
        Stop-CallbackGateway $gateway $GatewayPort
        throw
    }
}

if (Test-Path -LiteralPath $manifestPath) {
    $existing = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ((Test-ManagedProcess $existing.tunnel_pid $existing.tunnel_started_at $existing.tunnel_executable) -and
        (Test-ManagedProcess $existing.gateway_pid $existing.gateway_started_at $existing.gateway_executable)) {
        if ($RestartGateway) {
            $restarted = $null
            try {
                $oldGateway = Get-ManagedProcess $existing.gateway_pid $existing.gateway_started_at $existing.gateway_executable
                if ($null -eq $oldGateway) { throw 'Identidade do gateway mudou antes do reinicio.' }
                Stop-CallbackGateway $oldGateway $existing.port
                $restarted = Start-CallbackGateway $existing.port
                $existing.gateway_pid = $restarted.Id
                $existing.gateway_started_at = $restarted.StartTime.ToUniversalTime().Ticks.ToString()
                $existing.gateway_executable = $restarted.Path
                $existing | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8
                Write-Output 'Gateway atualizado; o tunel e sua URL foram preservados.'
            } catch {
                try {
                    $failedTunnel = Get-ManagedProcess $existing.tunnel_pid $existing.tunnel_started_at $existing.tunnel_executable
                    if ($null -ne $failedTunnel) {
                        $failedTunnel.Kill()
                        $null = $failedTunnel.WaitForExit(5000)
                    }
                } finally {
                    if ($null -ne $restarted) { Stop-CallbackGateway $restarted $existing.port }
                }
                throw
            }
        }
        Write-Output ('Tunel existente: ' + $existing.redirect_uri)
        Write-Output 'Mantenha essa URI identica no DevCenter e no .env. Nenhuma integracao foi ativada.'
        exit 0
    }
    throw 'Manifesto anterior encontrado. Execute stop_oauth_tunnel.ps1 antes de criar outro tunel.'
}

if ([string]::IsNullOrWhiteSpace($CloudflaredPath)) {
    $available = Get-Command cloudflared -ErrorAction SilentlyContinue
    if ($null -ne $available) { $CloudflaredPath = $available.Source }
    else {
        $toolsDir = Join-Path $projectRoot '.tools'
        New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
        $CloudflaredPath = Join-Path $toolsDir 'cloudflared-2026.10.1.exe'
        if (-not (Test-Path -LiteralPath $CloudflaredPath)) {
            Invoke-WebRequest -Uri 'https://github.com/cloudflare/cloudflared/releases/download/2026.10.1/cloudflared-windows-amd64.exe' -OutFile $CloudflaredPath
        }
        if ((Get-FileHash -LiteralPath $CloudflaredPath -Algorithm SHA256).Hash.ToLowerInvariant() -ne
            '4d2490e74082c5d48dbfedaf92576fa5bd1702379ab88eb7faa0cfbe8dc891b6') {
            throw 'Checksum do cloudflared divergiu da release oficial. Nada foi executado.'
        }
    }
}
$CloudflaredPath = [IO.Path]::GetFullPath($CloudflaredPath)
if (-not (Test-Path -LiteralPath $CloudflaredPath -PathType Leaf)) { throw 'cloudflared nao encontrado.' }

# Confirm only this loopback port is free. Never stop an unrelated server.
$listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
try { $listener.Start() } finally { $listener.Stop() }

$tunnelProcess = $null
$gatewayProcess = $null
try {
    # Bind and verify the gateway before any public forwarding can be created.
    $gatewayProcess = Start-CallbackGateway $Port
    $gatewayStart = $gatewayProcess.StartTime.ToUniversalTime().Ticks.ToString()
    $tunnelOut = Join-Path $runtimeDir 'cloudflared-out.log'
    $tunnelErr = Join-Path $runtimeDir 'cloudflared-err.log'
    # Force an isolated empty configuration; no user's Cloudflare files are changed.
    $isolatedConfig = Join-Path $runtimeDir 'cloudflared-empty.yml'
    Set-Content -LiteralPath $isolatedConfig -Value '{}' -Encoding ascii
    $tunnelProcess = Start-Process -FilePath $CloudflaredPath -WindowStyle Hidden -PassThru -WorkingDirectory $projectRoot -ArgumentList @(
        'tunnel', '--config', ('"' + $isolatedConfig + '"'), '--no-autoupdate', '--protocol', 'http2', '--url', ('http://127.0.0.1:' + $Port), '--metrics', '127.0.0.1:0'
    ) -RedirectStandardOutput $tunnelOut -RedirectStandardError $tunnelErr
    $tunnelStart = $tunnelProcess.StartTime.ToUniversalTime().Ticks.ToString()
    $publicUrl = $null
    $deadline = [DateTime]::UtcNow.AddSeconds(45)
    do {
        $gatewayProcess.Refresh()
        if ($gatewayProcess.HasExited) { throw 'Gateway OAuth terminou antes do tunel ficar pronto.' }
        if ($tunnelProcess.HasExited) { throw 'cloudflared terminou; consulte o log local sem publica-lo.' }
        if (Test-Path -LiteralPath $tunnelErr) {
            $logText = Get-Content -LiteralPath $tunnelErr -Raw
            if (-not [string]::IsNullOrEmpty($logText)) {
                $match = [regex]::Match($logText, 'https://[a-z0-9-]+\.trycloudflare\.com')
                if ($match.Success) { $publicUrl = $match.Value }
            }
        }
        if (-not $publicUrl) { Start-Sleep -Milliseconds 250 }
    } while (-not $publicUrl -and [DateTime]::UtcNow -lt $deadline)
    if (-not $publicUrl) { throw 'Quick Tunnel nao forneceu uma URL HTTPS dentro do prazo.' }
    $redirectUri = $publicUrl + '/oauth/mercadolivre/callback'
    & $pythonExe (Join-Path $PSScriptRoot 'configure_oauth_redirect.py') --uri $redirectUri
    if ($LASTEXITCODE -ne 0) { throw 'Redirect URI nao foi configurada.' }
    Wait-CallbackGateway $gatewayProcess $Port
    @{
        public_url = $publicUrl; redirect_uri = $redirectUri; port = $Port
        tunnel_pid = $tunnelProcess.Id; tunnel_started_at = $tunnelStart; tunnel_executable = $tunnelProcess.Path
        gateway_pid = $gatewayProcess.Id; gateway_started_at = $gatewayStart; gateway_executable = $gatewayProcess.Path
        created_at = [DateTime]::UtcNow.ToString('o')
    } | ConvertTo-Json | Set-Content -LiteralPath $manifestPath -Encoding utf8
    Write-Output ('Redirect URI HTTPS real: ' + $redirectUri)
    Write-Output 'Cadastre exatamente essa URI no Mercado Livre DevCenter. Client ID/Secret devem ser preenchidos apenas no .env local.'
    Write-Output 'OAuth e integracoes comerciais permanecem desativados ate sua configuracao/autorizacao explicita.'
} catch {
    try {
        if ($null -ne $tunnelProcess -and -not $tunnelProcess.HasExited) {
            $tunnelProcess.Kill()
            $null = $tunnelProcess.WaitForExit(5000)
        }
    } finally {
        if ($null -ne $gatewayProcess) { Stop-CallbackGateway $gatewayProcess $Port }
    }
    throw
}
