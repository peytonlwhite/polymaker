param(
    [switch]$NoDashboard
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$logPath = Join-Path $PSScriptRoot "bot_watchdog.log"

function Write-WatchdogLog {
    param([string]$Message)
    $line = "[{0}] {1}" -f (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
}

function Test-WorkerPidFile {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    try {
        $payload = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        $process = Get-Process -Id ([int]$payload.pid) -ErrorAction Stop
        return $process.ProcessName -match "^python"
    } catch {
        return $false
    }
}

function Test-PythonCommandLine {
    param([string]$Pattern)
    try {
        return [bool](Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" |
            Where-Object { $_.CommandLine -match $Pattern } |
            Select-Object -First 1)
    } catch {
        return $false
    }
}

function Start-DetachedScript {
    param(
        [string]$Label,
        [string]$Script,
        [string]$Stdout,
        [string]$Stderr
    )
    $arguments = "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$Script`""
    $process = Start-Process `
        -FilePath "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $PSScriptRoot `
        -WindowStyle Hidden `
        -RedirectStandardOutput $Stdout `
        -RedirectStandardError $Stderr `
        -PassThru
    Write-WatchdogLog "$Label launcher started pid=$($process.Id)"
}

function Stop-StaleSportsProcess {
    $matches = @(
        Get-CimInstance Win32_Process -Filter "Name LIKE 'python%.exe'" |
            Where-Object { $_.CommandLine -match 'sports_paper_bettor\.py' }
    )
    if ($matches.Count -gt 1) {
        throw "Refusing sports restart because multiple sports Python processes were found"
    }
    if ($matches.Count -eq 0) {
        return
    }
    $worker = $matches[0]
    $launcherPid = [int]$worker.ParentProcessId
    $launcher = Get-CimInstance Win32_Process -Filter "ProcessId=$launcherPid" -ErrorAction SilentlyContinue
    if ($launcher -and ($launcher.Name -notmatch '^powershell(?:\.exe)?$' -or $launcher.CommandLine -notmatch 'start_sports_loop\.ps1')) {
        throw "Refusing sports restart because parent PID $launcherPid is not the expected launcher"
    }
    Stop-Process -Id ([int]$worker.ProcessId) -Force
    $deadline = (Get-Date).AddSeconds(10)
    do {
        Start-Sleep -Milliseconds 250
        $remainingLauncher = Get-Process -Id $launcherPid -ErrorAction SilentlyContinue
    } while ($remainingLauncher -and (Get-Date) -lt $deadline)
    if ($remainingLauncher) {
        Stop-Process -Id $launcherPid -Force
    }
    Write-WatchdogLog "sports stale worker stopped pid=$($worker.ProcessId) launcher=$launcherPid"
}

function Wait-SportsWorker {
    param([int]$Seconds = 20)
    $deadline = (Get-Date).AddSeconds($Seconds)
    do {
        if (Test-WorkerPidFile (Join-Path $PSScriptRoot ".sports_bot.pid")) {
            return $true
        }
        Start-Sleep -Milliseconds 500
    } while ((Get-Date) -lt $deadline)
    return $false
}

try {
    $maintenancePython = Get-Command python -ErrorAction SilentlyContinue
    if ($maintenancePython) {
        & $maintenancePython.Source (Join-Path $PSScriptRoot "storage_maintenance.py") --scheduled | Out-Null
    } else {
        $maintenancePy = Get-Command py -ErrorAction SilentlyContinue
        $bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
        if ($maintenancePy) {
            & $maintenancePy.Source (Join-Path $PSScriptRoot "storage_maintenance.py") --scheduled | Out-Null
        } elseif (Test-Path -LiteralPath $bundledPython) {
            & $bundledPython (Join-Path $PSScriptRoot "storage_maintenance.py") --scheduled | Out-Null
        }
    }

    $cryptoRunning = Test-WorkerPidFile (Join-Path $PSScriptRoot ".crypto_bot.pid")
    if (-not $cryptoRunning) {
        Start-DetachedScript `
            -Label "crypto" `
            -Script (Join-Path $PSScriptRoot "start_crypto_paper.ps1") `
            -Stdout (Join-Path $PSScriptRoot "crypto_loop_stdout.log") `
            -Stderr (Join-Path $PSScriptRoot "crypto_loop_stderr.log")
        Start-Sleep -Seconds 2
        $cryptoRunning = Test-WorkerPidFile (Join-Path $PSScriptRoot ".crypto_bot.pid")
    }

    $guardianPython = $null
    if ($maintenancePython) {
        $guardianPython = $maintenancePython.Source
    } elseif ($maintenancePy) {
        $guardianPython = $maintenancePy.Source
    } elseif (Test-Path -LiteralPath $bundledPython) {
        $guardianPython = $bundledPython
    }
    $guardian = $null
    if ($guardianPython) {
        try {
            $guardianRaw = & $guardianPython (Join-Path $PSScriptRoot "sports_operations_guardian.py")
            $guardian = $guardianRaw | ConvertFrom-Json
            Write-WatchdogLog (
                "sports guardian healthy={0} action={1} report_age={2}m warnings={3}" -f `
                $guardian.healthy, $guardian.action, $guardian.report.age_minutes, `
                (($guardian.warnings | ForEach-Object { [string]$_ }) -join ",")
            )
        } catch {
            Write-WatchdogLog "sports guardian check failed: $($_.Exception.Message)"
        }
    }
    if ($guardian -and $guardian.action -eq "restart") {
        Stop-StaleSportsProcess
        Start-DetachedScript `
            -Label "sports guardian restart" `
            -Script (Join-Path $PSScriptRoot "start_sports_loop.ps1") `
            -Stdout (Join-Path $PSScriptRoot "sports_launcher.out.log") `
            -Stderr (Join-Path $PSScriptRoot "sports_launcher.err.log")
        if (Wait-SportsWorker) {
            & $guardianPython (Join-Path $PSScriptRoot "sports_operations_guardian.py") --record-restart | Out-Null
            Write-WatchdogLog "sports guardian restart verified"
        } else {
            Write-WatchdogLog "sports guardian restart did not produce a worker within 20 seconds"
        }
    }
    $sportsRunning = Test-WorkerPidFile (Join-Path $PSScriptRoot ".sports_bot.pid")
    if (-not $sportsRunning -and (-not $guardian -or $guardian.action -ne "alert")) {
        Start-DetachedScript `
            -Label "sports" `
            -Script (Join-Path $PSScriptRoot "start_sports_loop.ps1") `
            -Stdout (Join-Path $PSScriptRoot "sports_launcher.out.log") `
            -Stderr (Join-Path $PSScriptRoot "sports_launcher.err.log")
        $sportsRunning = Wait-SportsWorker
    }

    # BTC companion is public-data/shadow only; resume only an existing cohort.
    if ((Test-Path -LiteralPath (Join-Path $PSScriptRoot "crypto_btc_random_shadow.json")) -and
        -not (Test-PythonCommandLine 'crypto_btc_random_worker\.py')) {
        Start-DetachedScript `
            -Label "BTC random shadow" `
            -Script (Join-Path $PSScriptRoot "start_crypto_btc_random_shadow.ps1") `
            -Stdout (Join-Path $PSScriptRoot "crypto_btc_random_shadow.out.log") `
            -Stderr (Join-Path $PSScriptRoot "crypto_btc_random_shadow.err.log")
    }

    # BTC value experiments resume a frozen cohort, including unresolved fills.
    $btcValuePath = Join-Path $PSScriptRoot "crypto_btc_value_shadow.json"
    if (Test-Path -LiteralPath $btcValuePath) {
        try {
            $btcValue = Get-Content -LiteralPath $btcValuePath -Raw | ConvertFrom-Json
            $btcValueOpen = @($btcValue.records | Where-Object { $_.status -eq "filled" }).Count
            $btcValueNeeded = ([DateTimeOffset]::Parse([string]$btcValue.ends_at) -gt [DateTimeOffset]::Now) -or ($btcValueOpen -gt 0)
            if ($btcValue.mode -eq "shadow_only" -and $btcValueNeeded -and -not (Test-WorkerPidFile (Join-Path $PSScriptRoot ".crypto_btc_value_bot.pid"))) {
                Start-DetachedScript -Label "BTC value shadow" -Script (Join-Path $PSScriptRoot "start_crypto_btc_value_shadow.ps1") -Stdout (Join-Path $PSScriptRoot "crypto_btc_value_shadow.out.log") -Stderr (Join-Path $PSScriptRoot "crypto_btc_value_shadow.err.log")
            }
        } catch {
            Write-WatchdogLog "BTC value state needs attention; preserved without reset: $($_.Exception.Message)"
        }
    }

    # Resume the registered price-quality cohort; never register a new week.
    $itfQualityPath = Join-Path $PSScriptRoot "sports_itf_price_quality_state.json"
    if (Test-Path -LiteralPath $itfQualityPath) {
        try {
            $itfQuality = Get-Content -LiteralPath $itfQualityPath -Raw | ConvertFrom-Json
            $itfQualityOpen = @($itfQuality.positions.PSObject.Properties | Where-Object { $_.Value.status -eq "open" }).Count
            $itfQualityNeeded = ([DateTimeOffset]::Parse([string]$itfQuality.ends_at) -gt [DateTimeOffset]::Now) -or ($itfQualityOpen -gt 0)
            if ($itfQuality.mode -eq "shadow" -and $itfQualityNeeded -and -not (Test-WorkerPidFile (Join-Path $PSScriptRoot ".sports_itf_price_quality_bot.pid"))) {
                Start-DetachedScript -Label "ITF price-quality shadow" -Script (Join-Path $PSScriptRoot "start_sports_itf_price_quality.ps1") -Stdout (Join-Path $PSScriptRoot "sports_itf_price_quality.out.log") -Stderr (Join-Path $PSScriptRoot "sports_itf_price_quality.err.log")
            }
        } catch {
            Write-WatchdogLog "ITF price-quality state needs attention; preserved without reset: $($_.Exception.Message)"
        }
    }

    # Resume only registered shadow follow-ups; never create or reset a cohort.
    $itfFollowupPath = Join-Path $PSScriptRoot "sports_itf_followups_state.json"
    if (Test-Path -LiteralPath $itfFollowupPath) {
        try {
            $itfFollowup = Get-Content -LiteralPath $itfFollowupPath -Raw | ConvertFrom-Json
            $itfFollowupOpen = @($itfFollowup.positions.PSObject.Properties | Where-Object { $_.Value.row.status -eq "open" }).Count
            $itfFollowupEnd = [DateTimeOffset]::Parse([string]$itfFollowup.ends_at)
            $itfFollowupCaughtUp = $itfFollowup.source_last_scan_at -and ([DateTimeOffset]::Parse([string]$itfFollowup.source_last_scan_at) -ge $itfFollowupEnd)
            $itfFollowupNeeded = ($itfFollowupEnd -gt [DateTimeOffset]::Now) -or ($itfFollowupOpen -gt 0) -or -not $itfFollowupCaughtUp
            if ($itfFollowup.mode -eq "shadow" -and $itfFollowupNeeded -and -not (Test-WorkerPidFile (Join-Path $PSScriptRoot ".sports_itf_followups_bot.pid"))) {
                Start-DetachedScript -Label "ITF shadow follow-ups" -Script (Join-Path $PSScriptRoot "start_sports_itf_followups.ps1") -Stdout (Join-Path $PSScriptRoot "sports_itf_followups.out.log") -Stderr (Join-Path $PSScriptRoot "sports_itf_followups.err.log")
            }
        } catch {
            Write-WatchdogLog "ITF follow-up state needs attention; preserved without reset: $($_.Exception.Message)"
        }
    }

    # This independent worker has no credentials or execution routes. Starting it
    # requires an existing experiment ledger; a completed week is never reset.
    $itfStatePath = Join-Path $PSScriptRoot "sports_itf_shadow_state.json"
    if (Test-Path -LiteralPath $itfStatePath) {
        try {
            $itfState = Get-Content -LiteralPath $itfStatePath -Raw | ConvertFrom-Json
            $itfOpen = @($itfState.positions.PSObject.Properties | Where-Object { $_.Value.status -eq "open" }).Count
            $itfEnd = [DateTimeOffset]::Parse([string]$itfState.ends_at)
            $itfNeeded = ($itfEnd -gt [DateTimeOffset]::Now) -or ($itfOpen -gt 0)
            if ($itfState.mode -eq "shadow" -and $itfNeeded -and -not (Test-WorkerPidFile (Join-Path $PSScriptRoot ".sports_itf_shadow_bot.pid"))) {
                Start-DetachedScript `
                    -Label "ITF shadow research" `
                    -Script (Join-Path $PSScriptRoot "start_sports_itf_shadow.ps1") `
                    -Stdout (Join-Path $PSScriptRoot "sports_itf_shadow.out.log") `
                    -Stderr (Join-Path $PSScriptRoot "sports_itf_shadow.err.log")
            }
        } catch {
            Write-WatchdogLog "ITF shadow state needs attention; preserved without reset: $($_.Exception.Message)"
        }
    }

    if (-not $NoDashboard) {
        $dashboardRunning = Test-PythonCommandLine "dashboard\.py"
        if (-not $dashboardRunning) {
            Start-DetachedScript `
                -Label "dashboard" `
                -Script (Join-Path $PSScriptRoot "start_dashboard.ps1") `
                -Stdout (Join-Path $PSScriptRoot "dashboard_runtime.out.log") `
                -Stderr (Join-Path $PSScriptRoot "dashboard_runtime.err.log")
        }
    }

    $automationControl = Join-Path $PSScriptRoot "crypto_automation_control.py"
    if ($cryptoRunning -and (Test-Path -LiteralPath $automationControl)) {
        $healthPython = Get-Command python -ErrorAction SilentlyContinue
        if ($healthPython) {
            & $healthPython.Source $automationControl health | Out-Null
            $healthPath = Join-Path $PSScriptRoot "crypto_automation_health.json"
            if (Test-Path -LiteralPath $healthPath) {
                $cryptoHealth = Get-Content -LiteralPath $healthPath -Raw | ConvertFrom-Json
                Write-WatchdogLog "crypto deep health status=$($cryptoHealth.status) critical=$(@($cryptoHealth.critical_failures).Count) warnings=$(@($cryptoHealth.warnings).Count)"
            }
        }
    }

    if ($cryptoRunning -and $sportsRunning -and ($NoDashboard -or $dashboardRunning)) {
        Write-WatchdogLog "health check OK; all requested processes already running"
    }
} catch {
    Write-WatchdogLog "watchdog error: $($_.Exception.Message)"
    throw
}
