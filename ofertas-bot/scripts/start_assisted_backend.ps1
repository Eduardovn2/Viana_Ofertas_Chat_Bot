param([ValidateRange(1024, 65535)][int]$Port = 8000)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$taskPython = Join-Path $taskRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Ambiente Python local ausente.' }
if (Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue) {
    throw "Porta $Port ocupada; nenhum processo foi interrompido."
}
$taskRuntime = Join-Path $taskRoot '.runtime'
New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
$taskProcess = Start-Process -FilePath $taskPython -ArgumentList @(
    '-m', 'uvicorn', 'app.assisted:create_assisted_app', '--factory',
    '--host', '127.0.0.1', '--port', "$Port", '--no-access-log'
) -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru `
  -RedirectStandardOutput (Join-Path $taskRuntime 'assisted-backend.stdout.log') `
  -RedirectStandardError (Join-Path $taskRuntime 'assisted-backend.stderr.log')
$taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskRuntime 'assisted-backend.pid')
Write-Output "Backend assistido iniciado: PID $($taskProcess.Id), somente 127.0.0.1:$Port."
