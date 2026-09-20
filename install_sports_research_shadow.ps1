$ErrorActionPreference = "Stop"
$researchTaskName = "Polymaker Sports Shadow Research"
$researchLauncher = Join-Path $PSScriptRoot "start_sports_research_shadow.ps1"
$researchState = Join-Path $PSScriptRoot "sports_research_shadow_state.json"
if (-not (Test-Path -LiteralPath $researchState)) {
    throw "Register the prospective shadow cohort explicitly before installing its supervisor."
}
$researchPowerShell = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$researchArguments = "-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$researchLauncher`""
$researchExisting = Get-ScheduledTask -TaskName $researchTaskName -ErrorAction SilentlyContinue
if ($researchExisting) {
    if (@($researchExisting.Actions).Count -ne 1 -or $researchExisting.Actions[0].Arguments -ne $researchArguments) {
        throw "An unrelated task uses the research supervisor name; refusing to overwrite it."
    }
}
$researchUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$researchAction = New-ScheduledTaskAction -Execute $researchPowerShell -Argument $researchArguments -WorkingDirectory $PSScriptRoot
$researchTriggers = @(
    (New-ScheduledTaskTrigger -AtLogOn -User $researchUser),
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 45))
)
$researchPrincipal = New-ScheduledTaskPrincipal -UserId $researchUser -LogonType Interactive -RunLevel Limited
$researchSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $researchTaskName -Action $researchAction -Trigger $researchTriggers -Principal $researchPrincipal -Settings $researchSettings -Description "Public-data-only prospective sports shadow experiments. Never places live orders." -Force | Out-Null
Start-ScheduledTask -TaskName $researchTaskName
Get-ScheduledTask -TaskName $researchTaskName | Select-Object TaskName, State
