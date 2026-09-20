$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pythonCommand) { throw "Python is required for the Crypto research companion." }
while ($true) {
    & $pythonCommand.Source -B (Join-Path $PSScriptRoot "frozen_crypto_shadow_runner.py")
    if ($LASTEXITCODE -eq 0) { exit 0 }
    if ($LASTEXITCODE -eq 42) { exit 42 }
    Start-Sleep -Seconds 10
}
