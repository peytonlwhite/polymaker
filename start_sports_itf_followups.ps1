$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if (-not $pythonCommand) { $pythonCommand = Get-Command py -ErrorAction SilentlyContinue }
if (-not $pythonCommand) { throw "Python is required for the ITF shadow follow-up tests." }
& $pythonCommand.Source -B (Join-Path $PSScriptRoot "sports_itf_followups_worker.py")
exit $LASTEXITCODE
