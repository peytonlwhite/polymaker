$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

. .\load_bot_settings.ps1

$env:SPORTS_RUN_LOOP = "true"

$python = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        $python = $command.Source
    }
}

if (-not $python -or -not (Test-Path -LiteralPath $python)) {
    throw "Could not find Python for sports loop."
}

& $python sports_paper_bettor.py
