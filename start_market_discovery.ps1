$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue)
if ($python) {
    & $python.Source market_discovery.py
    exit $LASTEXITCODE
}

$py = (Get-Command py -ErrorAction SilentlyContinue)
if ($py) {
    & $py.Source market_discovery.py
    exit $LASTEXITCODE
}

throw "Could not find Python. Install Python 3, then rerun this script."
