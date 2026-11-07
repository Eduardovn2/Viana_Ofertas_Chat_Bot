# Native, isolated regression tests. Does not load .env, run OAuth or start cloudflared.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonExe = Join-Path $projectRoot '.venv\Scripts\python.exe'
. (Join-Path $projectRoot 'scripts/oauth_tunnel_processes.ps1')

$checks = 0
function Assert-True([bool]$Condition, [string]$Message) {
    if (-not $Condition) { throw $Message }
    $script:checks++
}

$testDir = Join-Path ([IO.Path]::GetTempPath()) ('ofertas-oauth-processes-' + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $testDir | Out-Null
$serverScript = Join-Path $testDir 'synthetic_listener.py'
@'
"""SYNTHETIC stdlib-only process fixture; deliberately no application imports."""
import os
import pathlib
import socket
import subprocess
import sys
import time

mode, ready_path = sys.argv[1:3]
if mode == "launcher":
    subprocess.Popen([sys.executable, __file__, "listener", ready_path])
elif mode == "listener":
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(20)
    pathlib.Path(ready_path).write_text(f"{os.getpid()}:{listener.getsockname()[1]}")
elif mode == "idle":
    pathlib.Path(ready_path).write_text(str(os.getpid()))
else:
    raise SystemExit(2)
time.sleep(90)
'@ | Set-Content -LiteralPath $serverScript -Encoding utf8

$roots = [Collections.Generic.List[Diagnostics.Process]]::new()
$leafHandles = [Collections.Generic.List[Diagnostics.Process]]::new()
function Start-SyntheticProcess([string]$Mode) {
    $ready = Join-Path $testDir ([Guid]::NewGuid().ToString('N') + '.ready')
    $root = Start-Process -FilePath $pythonExe -WindowStyle Hidden -PassThru -ArgumentList @(
        '-u', ('"' + $serverScript + '"'), $Mode, ('"' + $ready + '"')
    )
    $null = $root.SafeHandle
    $roots.Add($root)
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    do {
        if ($root.HasExited) { throw 'Synthetic process exited before readiness.' }
        if (Test-Path -LiteralPath $ready) {
            $value = Get-Content -LiteralPath $ready -Raw
            if ($value -match '^(\d+)(?::(\d+))?$') {
                $leaf = Get-Process -Id ([int]$Matches[1])
                $null = $leaf.SafeHandle
                $leafHandles.Add($leaf)
                $assignedPort = if ($Mode -eq 'idle') { 0 } else { [int]$Matches[2] }
                return @{ Root = $root; Leaf = $leaf; Port = $assignedPort }
            }
        }
        Start-Sleep -Milliseconds 50
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Synthetic process readiness timed out.'
}

try {
    # Parse all changed scripts without executing their top-level launch/stop operations.
    foreach ($name in @('start_oauth_tunnel.ps1', 'stop_oauth_tunnel.ps1', 'oauth_tunnel_processes.ps1')) {
        $parseTokens = $null
        $parseErrors = $null
        $null = [Management.Automation.Language.Parser]::ParseFile(
            (Join-Path $projectRoot ('scripts/' + $name)), [ref]$parseTokens, [ref]$parseErrors
        )
        Assert-True ($parseErrors.Count -eq 0) ('PowerShell syntax: ' + $name)
    }

    $startText = [IO.File]::ReadAllText((Join-Path $projectRoot 'scripts/start_oauth_tunnel.ps1'))
    $gatewayPosition = $startText.IndexOf('$gatewayProcess = Start-CallbackGateway $Port')
    $tunnelPosition = $startText.IndexOf('$tunnelProcess = Start-Process')
    Assert-True ($gatewayPosition -ge 0 -and $gatewayPosition -lt $tunnelPosition) 'Gateway must be verified before public forwarding starts.'
    Assert-True ($startText.Contains('Stop-CallbackGateway $oldGateway $existing.port')) 'Restart must await the verified old gateway tree.'

    # The idle candidate and foreign listener are sibling trees, including real venv launchers.
    $candidate = Start-SyntheticProcess 'idle'
    $foreign = Start-SyntheticProcess 'listener'
    Assert-True (-not (Test-GatewayProcessOwner $foreign.Leaf.Id $candidate.Root)) 'A sibling listener is not owned by the candidate gateway.'
    $rejected = $false
    try { Wait-CallbackGateway $candidate.Root $foreign.Port 2 } catch {
        $rejected = $_.Exception.Message -like 'Porta OAuth ocupada por outro processo*'
    }
    Assert-True $rejected 'An unrelated listener must never satisfy readiness.'
    $foreign.Root.Refresh()
    $foreign.Leaf.Refresh()
    Assert-True (-not $foreign.Root.HasExited -and -not $foreign.Leaf.HasExited) 'Readiness rejection must preserve the foreign process.'

    # Force an additional real Python child so ancestry, not executable equality, is necessary.
    $legitimate = Start-SyntheticProcess 'launcher'
    Assert-True ($legitimate.Leaf.Id -ne $legitimate.Root.Id) 'Fixture must have a legitimate Python child.'
    Assert-True (Test-GatewayProcessOwner $legitimate.Leaf.Id $legitimate.Root) 'The actual Python child must be accepted through ancestry.'
    Wait-CallbackGateway $legitimate.Root $legitimate.Port 2
    $checks++
    Assert-True (Test-ManagedProcess $legitimate.Root.Id $legitimate.Root.StartTime.ToUniversalTime().Ticks.ToString() $legitimate.Root.Path) 'Recorded process identity must match.'
    Assert-True (-not (Test-ManagedProcess $legitimate.Root.Id '1' $legitimate.Root.Path)) 'A reused PID or changed start time must be rejected.'

    # Stop must remove launcher plus Python listener and must wait until both exit.
    Stop-CallbackGateway $legitimate.Root $legitimate.Port
    $legitimate.Root.Refresh()
    $legitimate.Leaf.Refresh()
    Assert-True ($legitimate.Root.HasExited -and $legitimate.Leaf.HasExited) 'Stopping must await launcher and listener child.'
    Assert-True (@(Get-CallbackListeners $legitimate.Port).Count -eq 0) 'Stopped child must release its dynamic port.'
    $foreign.Leaf.Refresh()
    Assert-True (-not $foreign.Leaf.HasExited) 'Stopping a managed tree must preserve the foreign listener.'

    # Failure cleanup also must not terminate a foreign owner of the requested port.
    Stop-CallbackGateway $candidate.Root $foreign.Port
    $candidate.Root.Refresh()
    $foreign.Root.Refresh()
    $foreign.Leaf.Refresh()
    Assert-True $candidate.Root.HasExited 'Failure cleanup must await the candidate launcher.'
    Assert-True (-not $foreign.Root.HasExited -and -not $foreign.Leaf.HasExited) 'Failure cleanup must never terminate a sibling tree.'
    $rejected = $false
    try { Wait-CallbackGateway $candidate.Root $foreign.Port 2 } catch {
        $rejected = $_.Exception.Message -like 'Gateway OAuth terminou*'
    }
    Assert-True $rejected 'An exited candidate cannot be made ready by a foreign listener.'
    Write-Output ('OAuth tunnel process regression checks: ' + $checks + ' passed.')
} finally {
    # Only handles created by this fixture are stopped. Existing gateways/tunnels are untouched.
    foreach ($root in $roots) {
        try {
            $root.Refresh()
            if (-not $root.HasExited) { Stop-CallbackGateway $root 0 }
        } catch { }
    }
    foreach ($leaf in $leafHandles) {
        $leaf.Refresh()
        if (-not $leaf.HasExited) { $leaf.Kill(); $null = $leaf.WaitForExit(5000) }
    }
    $resolvedTestDir = [IO.Path]::GetFullPath($testDir)
    $allowedPrefix = Join-Path ([IO.Path]::GetFullPath([IO.Path]::GetTempPath())) 'ofertas-oauth-processes-'
    if (-not $resolvedTestDir.StartsWith($allowedPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'Test cleanup target is outside its isolated directory.'
    }
    Remove-Item -LiteralPath $resolvedTestDir -Recurse -Force
}
