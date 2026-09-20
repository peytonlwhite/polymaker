$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pythonCommand) { throw "Python is required for ITF shadow price-quality tests." }
& $pythonCommand.Source -B (Join-Path $PSScriptRoot "sports_itf_price_quality_worker.py")
exit $LASTEXITCODE
