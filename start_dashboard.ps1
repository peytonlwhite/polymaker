$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

if (-not $env:DASHBOARD_PORT) {
    $env:DASHBOARD_PORT = "8765"
}

function Set-DashboardProcessStatus {
    param([bool]$Running)
    $mutex = [System.Threading.Mutex]::new($false, "Local\PolymakerBotProcessStatus")
    $locked = $false
    try {
        $locked = $mutex.WaitOne(10000)
        if (-not $locked) { throw "Timed out waiting for bot process status lock" }
        $path = Join-Path $PSScriptRoot "bot_processes.json"
        try {
            $data = if (Test-Path -LiteralPath $path) { Get-Content -LiteralPath $path -Raw | ConvertFrom-Json } else { [pscustomobject]@{} }
        } catch { $data = [pscustomobject]@{} }
        if (-not ($data.PSObject.Properties.Name -contains "dashboard")) {
            $data | Add-Member -NotePropertyName "dashboard" -NotePropertyValue ([pscustomobject]@{})
        }
        $data.dashboard = [pscustomobject]@{
            pid = $PID
            running = $Running
            script = "dashboard.py"
            started_at = if ($Running) { (Get-Date).ToString("s") } else { $data.dashboard.started_at }
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
    Set-DashboardProcessStatus $true
    & $python.Source dashboard.py
    $exitCode = $LASTEXITCODE
    Set-DashboardProcessStatus $false
    exit $exitCode
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    Set-DashboardProcessStatus $true
    & $py.Source dashboard.py
    $exitCode = $LASTEXITCODE
    Set-DashboardProcessStatus $false
    exit $exitCode
}

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path -LiteralPath $bundledPython) {
    Set-DashboardProcessStatus $true
    & $bundledPython dashboard.py
    $exitCode = $LASTEXITCODE
    Set-DashboardProcessStatus $false
    exit $exitCode
}

throw "Could not find Python. Install Python 3, then rerun this script."
