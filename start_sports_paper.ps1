$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$env:SPORTS_EXECUTION_MODE = "paper"
$env:ALLOW_LIVE_TRADING = "false"

if (-not $env:SPORTS_STARTING_BALANCE) {
    $env:SPORTS_STARTING_BALANCE = "300"
}

$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($python) {
    & $python.Source sports_paper_bettor.py
    exit $LASTEXITCODE
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    & $py.Source sports_paper_bettor.py
    exit $LASTEXITCODE
}

throw "Could not find Python. Install Python 3, then rerun this script."
