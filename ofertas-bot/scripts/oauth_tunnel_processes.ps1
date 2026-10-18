# Process identity and listener ownership for the local OAuth gateway only.
function Get-ManagedProcess([int]$ProcessId, [string]$StartedAt, [string]$Executable) {
    $managed = Get-Process -Id $ProcessId -ErrorAction SilentlyContinue
    if ($null -eq $managed) { return $null }
    try {
        $null = $managed.SafeHandle
        if ($managed.Path -eq $Executable -and
            $managed.StartTime.ToUniversalTime().Ticks.ToString() -eq $StartedAt) { return $managed }
    } catch { return $null }
    return $null
}

function Test-ManagedProcess([int]$ProcessId, [string]$StartedAt, [string]$Executable) {
    return ($null -ne (Get-ManagedProcess $ProcessId $StartedAt $Executable))
}

function Get-CallbackListeners([int]$GatewayPort) {
    Get-NetTCPConnection -State Listen -LocalPort $GatewayPort -ErrorAction SilentlyContinue |
        Where-Object { $_.LocalAddress -in @('127.0.0.1', '0.0.0.0', '::') }
}

function Test-GatewayProcessOwner([int]$OwnerId, [Diagnostics.Process]$Gateway) {
    try {
        $Gateway.Refresh()
        if ($Gateway.HasExited) { return $false }
        $rootStart = $Gateway.StartTime.ToUniversalTime()
        $rootPath = $Gateway.Path
        $visited = [Collections.Generic.HashSet[int]]::new()
        $childStart = [DateTime]::MaxValue
        for ($depth = 0; $depth -lt 32; $depth++) {
            if (-not $visited.Add($OwnerId)) { return $false }
            $owner = Get-Process -Id $OwnerId -ErrorAction Stop
            $ownerStart = $owner.StartTime.ToUniversalTime()
            if ($ownerStart -lt $rootStart -or $ownerStart -gt $childStart) { return $false }
            if ($OwnerId -eq $Gateway.Id) {
                return ($ownerStart.Ticks -eq $rootStart.Ticks -and $owner.Path -eq $rootPath)
            }
            $details = Get-CimInstance Win32_Process -Filter "ProcessId = $OwnerId" -ErrorAction Stop
            if ($null -eq $details -or
                [Math]::Abs(($details.CreationDate.ToUniversalTime() - $ownerStart).TotalMilliseconds) -gt 1) {
                return $false
            }
            $childStart = $ownerStart
            $OwnerId = [int]$details.ParentProcessId
        }
    } catch { return $false }
    return $false
}

function Wait-CallbackGateway([Diagnostics.Process]$Gateway, [int]$GatewayPort,
                              [int]$TimeoutSeconds = 15) {
    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $Gateway.Refresh()
        if ($Gateway.HasExited) { throw 'Gateway OAuth terminou; consulte o log local sem publica-lo.' }
        $listeners = @(Get-CallbackListeners $GatewayPort)
        if ($listeners.Count -gt 0) {
            foreach ($connection in $listeners) {
                if ($connection.LocalAddress -ne '127.0.0.1' -or
                    -not (Test-GatewayProcessOwner ([int]$connection.OwningProcess) $Gateway)) {
                    throw 'Porta OAuth ocupada por outro processo; nenhum servico alheio sera exposto.'
                }
            }
            $Gateway.Refresh()
            if (-not $Gateway.HasExited) { return }
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    throw 'Gateway nao iniciou no prazo.'
}

function Stop-CallbackGateway([Diagnostics.Process]$Gateway, [int]$GatewayPort) {
    # Bind handles while the identities/ancestry are verified; never kill a reused PID.
    $owned = [Collections.Generic.List[Diagnostics.Process]]::new()
    $null = $Gateway.SafeHandle
    $owned.Add($Gateway)
    $parents = [Collections.Generic.Queue[int]]::new()
    $parents.Enqueue($Gateway.Id)
    $visited = [Collections.Generic.HashSet[int]]::new()
    while ($parents.Count -gt 0) {
        $parentId = $parents.Dequeue()
        if (-not $visited.Add($parentId)) { continue }
        if ($visited.Count -gt 256) { throw 'Arvore de processos OAuth inesperada.' }
        foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $parentId" -ErrorAction Stop)) {
            if (Test-GatewayProcessOwner ([int]$child.ProcessId) $Gateway) {
                $process = Get-Process -Id $child.ProcessId -ErrorAction SilentlyContinue
                if ($null -ne $process) {
                    $null = $process.SafeHandle
                    if ([Math]::Abs(($child.CreationDate.ToUniversalTime() -
                            $process.StartTime.ToUniversalTime()).TotalMilliseconds) -gt 1) { continue }
                    $owned.Add($process)
                    $parents.Enqueue($process.Id)
                }
            }
        }
    }
    $Gateway.Refresh()
    if (-not $Gateway.HasExited) { $Gateway.Kill() }
    for ($index = $owned.Count - 1; $index -gt 0; $index--) {
        $owned[$index].Refresh()
        if (-not $owned[$index].HasExited) { $owned[$index].Kill() }
    }
    foreach ($process in $owned) {
        if (-not $process.WaitForExit(5000)) { throw 'Gateway anterior ainda esta encerrando.' }
    }
}

