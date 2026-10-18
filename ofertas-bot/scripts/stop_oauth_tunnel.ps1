Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$manifestPath = Join-Path $projectRoot '.runtime\oauth-tunnel.json'
. (Join-Path $PSScriptRoot 'oauth_tunnel_processes.ps1')
if (-not (Test-Path -LiteralPath $manifestPath)) { Write-Output 'Nenhum tunel gerenciado registrado.'; exit 0 }
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$gateway = $null
$tunnel = $null
# Validate both identities before changing either process.
foreach ($entry in @(
    @{ Role = 'gateway'; ProcessId = $manifest.gateway_pid; StartedAt = $manifest.gateway_started_at; Executable = $manifest.gateway_executable },
    @{ Role = 'tunnel'; ProcessId = $manifest.tunnel_pid; StartedAt = $manifest.tunnel_started_at; Executable = $manifest.tunnel_executable }
)) {
    $process = Get-Process -Id $entry.ProcessId -ErrorAction SilentlyContinue
    if ($null -ne $process) {
        $null = $process.SafeHandle
        if ($process.Path -ne $entry.Executable -or $process.StartTime.ToUniversalTime().Ticks.ToString() -ne $entry.StartedAt) {
            throw 'Identidade do processo divergiu. Nenhum processo desconhecido sera encerrado.'
        }
        if ($entry.Role -eq 'gateway') { $gateway = $process } else { $tunnel = $process }
    }
}
try {
    if ($null -ne $tunnel) {
        $tunnel.Kill()
        if (-not $tunnel.WaitForExit(5000)) { throw 'Tunel ainda esta encerrando.' }
    }
} finally {
    if ($null -ne $gateway) { Stop-CallbackGateway $gateway $manifest.port }
}
if (@(Get-CallbackListeners $manifest.port).Count -gt 0) {
    throw 'A porta ainda possui listener. Manifesto preservado; nenhum processo desconhecido foi encerrado.'
}
# Only this known manifest is removed; credentials, logs and tokens are preserved.
Remove-Item -LiteralPath $manifestPath
Write-Output 'Gateway e Quick Tunnel encerrados. Um novo inicio gerara outra URL; atualize o DevCenter.'
