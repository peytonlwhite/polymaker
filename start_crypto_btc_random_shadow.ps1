$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pythonCommand) { throw "Python is required for BTC shadow cycles." }
while ($true) {
    & $pythonCommand.Source -B (Join-Path $PSScriptRoot "crypto_btc_random_worker.py")
    if ($LASTEXITCODE -eq 0) { exit 0 }
    Start-Sleep -Seconds 10
}
