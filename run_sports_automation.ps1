param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("governor", "weekly", "monthly")]
    [string]$Task
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$bundledPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (Test-Path -LiteralPath $bundledPython) {
    $python = $bundledPython
} else {
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw "Could not find Python for the sports automation"
    }
    $python = $pythonCommand.Source
}

$logPath = Join-Path $PSScriptRoot "sports_automation.log"
$startedAt = Get-Date
try {
    if ($Task -eq "governor") {
        $output = & $python (Join-Path $PSScriptRoot "sports_strategy_governor_runner.py") 2>&1
    } elseif ($Task -eq "weekly") {
        $output = & $python (Join-Path $PSScriptRoot "sports_model_audit.py") --scope weekly --run 2>&1
    } else {
        $output = & $python (Join-Path $PSScriptRoot "sports_model_audit.py") --scope monthly --run 2>&1
    }
    $exitCode = $LASTEXITCODE
    $line = "[{0}] task={1} exit={2} duration_seconds={3:N1} output={4}" -f `
        (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Task, $exitCode, `
        ((Get-Date) - $startedAt).TotalSeconds, (($output | Out-String).Trim() -replace "`r?`n", " ")
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    exit $exitCode
} catch {
    $line = "[{0}] task={1} failed duration_seconds={2:N1} error={3}" -f `
        (Get-Date).ToString("yyyy-MM-dd HH:mm:ss"), $Task, `
        ((Get-Date) - $startedAt).TotalSeconds, $_.Exception.Message
    Add-Content -LiteralPath $logPath -Value $line -Encoding UTF8
    throw
}

