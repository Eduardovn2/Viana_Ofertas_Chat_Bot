param(
    [Parameter(Position=0)]
    [ValidateSet('validate','start','run','install','open','health','login','stop')]
    [string]$Action = 'health',
    [ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$PostgresService,
    [ValidatePattern('^[a-zA-Z0-9_.-]+$')][string]$PostgresContainer = 'ofertas-bot-postgres-1'
)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$python = Join-Path $project '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Ambiente .venv não encontrado.' }
$taskName = 'VianaOfertas - Operacao diaria'
$api = 'http://127.0.0.1:8000'
$runtime = Join-Path $project '.runtime'
Set-Location -LiteralPath $project

function Test-Database {
    & $python -m app.daily_admin database *> $null
    return $LASTEXITCODE -eq 0
}

function Start-ExistingDatabase {
    if (Test-Database) { return }
    if ($PostgresService) {
        Start-Service -Name $PostgresService
    } else {
        if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'Inicie o PostgreSQL existente ou informe -PostgresService.' }
        $desktop = Join-Path $env:LOCALAPPDATA 'Programs\DockerDesktop\Docker Desktop.exe'
        if ((Test-Path -LiteralPath $desktop) -and -not (Get-Process -Name 'Docker Desktop' -ErrorAction SilentlyContinue)) {
            Start-Process -FilePath $desktop -WindowStyle Hidden
        }
        for ($i=0; $i -lt 30; $i++) {
            docker inspect --format '{{.Id}}' $PostgresContainer *> $null
            if ($LASTEXITCODE -eq 0) { break }
            Start-Sleep -Seconds 2
        }
        if ($LASTEXITCODE -ne 0) { throw 'Contêiner PostgreSQL existente ou Docker Engine indisponível.' }
        docker start $PostgresContainer
        if ($LASTEXITCODE -ne 0) { throw 'Não foi possível iniciar o contêiner existente.' }
    }
    for ($i=0; $i -lt 30; $i++) {
        if (Test-Database) { return }
        Start-Sleep -Seconds 2
    }
    throw 'PostgreSQL não respondeu no prazo.'
}

function Start-LocalApi {
    $listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
    if ($listener -and ($listener.LocalAddress | Where-Object { $_ -ne '127.0.0.1' })) {
        throw 'A porta 8000 tem um listener fora de 127.0.0.1.'
    }
    if (-not $listener) {
        New-Item -ItemType Directory -Path $runtime -Force | Out-Null
        Start-Process -FilePath $python -ArgumentList @('-m','uvicorn','app.deployment:create_control_app','--factory','--host','127.0.0.1','--port','8000','--no-access-log','--no-proxy-headers') -WorkingDirectory $project -WindowStyle Hidden -RedirectStandardOutput (Join-Path $runtime 'daily-api.stdout.log') -RedirectStandardError (Join-Path $runtime 'daily-api.stderr.log')
    }
    for ($i=0; $i -lt 20; $i++) {
        try {
            $ready = Invoke-RestMethod -Uri "$api/ready" -TimeoutSec 2
            $panel = Invoke-WebRequest -Uri "$api/daily" -TimeoutSec 2 -UseBasicParsing
            if ($ready.status -eq 'ready' -and $panel.StatusCode -eq 200) { return }
        } catch {}
        Start-Sleep -Seconds 1
    }
    throw 'API indisponível ou versão anterior em execução. Confira a porta 8000 e os logs locais.'
}

switch ($Action) {
    'validate' {
        & $python -c 'import alembic, apscheduler, fastapi, httpx, psycopg, sqlalchemy, uvicorn; from app.deployment import create_control_app; from app.services.daily_operation import ZONE; print(ZONE.key)'
        exit $LASTEXITCODE
    }
    'start' {
        if (-not (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue)) { throw 'Execute install primeiro, ou use run no terminal.' }
        Start-ScheduledTask -TaskName $taskName
    }
    'run' {
        Start-ExistingDatabase
        Start-LocalApi
        & $python -m app.daily_worker
        exit $LASTEXITCODE
    }
    'install' {
        $shell = (Get-Command powershell.exe).Source
        $arguments = '-NoProfile -WindowStyle Hidden -File "' + $PSCommandPath + '" run'
        if ($PostgresService) { $arguments += ' -PostgresService "' + $PostgresService + '"' }
        else { $arguments += ' -PostgresContainer "' + $PostgresContainer + '"' }
        $taskAction = New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $project
        $triggers = @((New-ScheduledTaskTrigger -AtLogOn -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name)),(New-ScheduledTaskTrigger -Daily -At '08:00'))
        $settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -WakeToRun -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
        Register-ScheduledTask -TaskName $taskName -Action $taskAction -Trigger $triggers -Settings $settings -User ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) -RunLevel Limited -Description 'Vianinha local. Operação externa somente com autorização persistida.'
    }
    'open' { Start-Process "microsoft-edge:$api/daily" }
    'health' {
        & $python -m app.daily_admin database
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        Invoke-RestMethod -Uri "$api/ready" -TimeoutSec 5
    }
    'login' { & $python -m app.daily_admin login; exit $LASTEXITCODE }
    'stop' { & $python -m app.daily_admin stop; exit $LASTEXITCODE }
}
