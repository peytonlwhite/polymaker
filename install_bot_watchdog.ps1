$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$taskName = "Polymaker Bot Watchdog"
$watchdog = Join-Path $PSScriptRoot "bot_watchdog.ps1"
$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

$action = New-ScheduledTaskAction `
    -Execute $powershell `
    -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$watchdog`"" `
    -WorkingDirectory $PSScriptRoot

$recurringTrigger = New-ScheduledTaskTrigger `
    -Once `
    -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 5) `
    -RepetitionDuration (New-TimeSpan -Days 3650)

$logonTrigger = New-ScheduledTaskTrigger -AtLogOn -User $identity
$principal = New-ScheduledTaskPrincipal `
    -UserId $identity `
    -LogonType Interactive `
    -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 2)

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger @($recurringTrigger, $logonTrigger) `
    -Principal $principal `
    -Settings $settings `
    -Description "Five-minute Polymaker operations guardian with bounded sports health recovery and process checks." `
    -Force | Out-Null

Write-Output "Registered scheduled task: $taskName"
