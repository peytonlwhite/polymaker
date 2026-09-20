$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# This is the persistent worker launcher. One-off scans call Python directly.
$env:CRYPTO_RUN_LOOP = "true"

$sizingScript = Join-Path $PSScriptRoot "start_crypto_shadow_sizing.ps1"
if (Test-Path -LiteralPath $sizingScript) {
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-File", ('"' + $sizingScript + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}

# Independent research worker owns an OS lock, so repeated launches are harmless.
# It reads quotes and scanner reports without calling the trading scan loop.
$researchScript = Join-Path $PSScriptRoot "start_crypto_shadow_research.ps1"
if (Test-Path -LiteralPath $researchScript) {
    Start-Process -FilePath "powershell.exe" -ArgumentList @("-NoProfile", "-File", ('"' + $researchScript + '"')) -WorkingDirectory $PSScriptRoot -WindowStyle Hidden
}

function Set-BotProcessStatus {
    param([bool]$Running)
    $mutex = [System.Threading.Mutex]::new($false, "Local\PolymakerBotProcessStatus")
    $locked = $false
    try {
        $locked = $mutex.WaitOne(10000)
        if (-not $locked) { throw "Timed out waiting for bot process status lock" }
        $path = Join-Path $PSScriptRoot "bot_processes.json"
        if (Test-Path -LiteralPath $path) {
            try { $data = Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } catch { $data = [pscustomobject]@{} }
        } else { $data = [pscustomobject]@{} }
        if (-not ($data.PSObject.Properties.Name -contains "crypto")) {
            $data | Add-Member -NotePropertyName "crypto" -NotePropertyValue ([pscustomobject]@{})
        }
        $data.crypto = [pscustomobject]@{
            pid = $PID
            running = $Running
            script = "start_crypto_paper.ps1"
            started_at = if ($Running) { (Get-Date).ToString("s") } else { $data.crypto.started_at }
            stopped_at = if ($Running) { $null } else { (Get-Date).ToString("s") }
        }
        $data | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $path -Encoding UTF8
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}

$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($python) {
    Set-BotProcessStatus $true
    & $python.Source crypto_paper_bettor.py
    $exitCode = $LASTEXITCODE
    Set-BotProcessStatus $false
    exit $exitCode
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    Set-BotProcessStatus $true
    & $py.Source crypto_paper_bettor.py
    $exitCode = $LASTEXITCODE
    Set-BotProcessStatus $false
    exit $exitCode
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path -LiteralPath $bundledPython) {
    Set-BotProcessStatus $true
    & $bundledPython crypto_paper_bettor.py
    $exitCode = $LASTEXITCODE
    Set-BotProcessStatus $false
    exit $exitCode
}

throw "Could not find Python. Install Python 3, then rerun this script."
