$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot
$researchPython = Get-Command python -ErrorAction Stop
$researchStdout = Join-Path $PSScriptRoot "sports_research_shadow.out.log"
$researchStderr = Join-Path $PSScriptRoot "sports_research_shadow.err.log"
# Resume only: the launcher never registers/reconstructs a missing experiment.
while ($true) {
    $researchReport = Join-Path $PSScriptRoot "sports_research_shadow_report.json"
    if (Test-Path -LiteralPath $researchReport) {
        try {
            if ((Get-Content -LiteralPath $researchReport -Raw | ConvertFrom-Json).status -eq "complete") { exit 0 }
        } catch { }
    }
    $researchExit = 1
    try {
        & $researchPython.Source -B (Join-Path $PSScriptRoot "sports_research_worker.py") 1>> $researchStdout 2>> $researchStderr
        $researchExit = $LASTEXITCODE
    } catch {
        Add-Content -LiteralPath $researchStderr -Value $_.Exception.Message
    }
    if ($researchExit -eq 0) { exit 0 }
    Start-Sleep -Seconds 30
}
