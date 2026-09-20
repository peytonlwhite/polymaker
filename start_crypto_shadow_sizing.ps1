$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$sizingPython = Get-Command python -ErrorAction SilentlyContinue
if (-not $sizingPython) { $sizingPython = Get-Command py -ErrorAction SilentlyContinue }
if (-not $sizingPython) { throw "Python is required for the Crypto shadow sizing lab." }
while ($true) {
    & $sizingPython.Source -B (Join-Path $PSScriptRoot "crypto_shadow_sizing_worker.py")
    if ($LASTEXITCODE -eq 0) { exit 0 }
    Start-Sleep -Seconds 10
}
