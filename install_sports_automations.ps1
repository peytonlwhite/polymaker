$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$powershell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$runner = Join-Path $PSScriptRoot "run_sports_automation.ps1"
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

function Register-DailyAutomation {
    param(
        [string]$Name,
        [string]$TaskArgument,
        [datetime[]]$Times,
        [string]$Description,
        [timespan]$ExecutionLimit
    )
    $action = New-ScheduledTaskAction `
        -Execute $powershell `
        -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Task $TaskArgument" `
        -WorkingDirectory $PSScriptRoot
    $triggers = @($Times | ForEach-Object { New-ScheduledTaskTrigger -Daily -At $_ })
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit $ExecutionLimit
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $triggers `
        -Principal $principal `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null
}

function Register-WeeklyAutomation {
    param(
        [string]$Name,
        [string]$TaskArgument,
        [string]$Description
    )
    $action = New-ScheduledTaskAction `
        -Execute $powershell `
        -Argument "-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -Task $TaskArgument" `
        -WorkingDirectory $PSScriptRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek Sunday -At "04:00"
    $principal = New-ScheduledTaskPrincipal -UserId $identity -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes 90) `
        -RunOnlyIfNetworkAvailable
    Register-ScheduledTask `
        -TaskName $Name `
        -Action $action `
        -Trigger $trigger `
        -Principal $principal `
        -Settings $settings `
        -Description $Description `
        -Force | Out-Null
}

Register-DailyAutomation `
    -Name "Polymaker Sports Strategy Governor" `
    -TaskArgument "governor" `
    -Times @([datetime]"08:00", [datetime]"15:00", [datetime]"23:00") `
    -ExecutionLimit (New-TimeSpan -Minutes 10) `
    -Description "Three daily bounded sports strategy evaluations. Model review runs only when a segment changes state."

Register-WeeklyAutomation `
    -Name "Polymaker Sports Deep Strategy Audit" `
    -TaskArgument "weekly" `
    -Description "Weekly GPT-5.6 Sol high-reasoning sports strategy, pricing, execution, and code-health audit."

$escapedIdentity = [System.Security.SecurityElement]::Escape($identity)
$escapedPowerShell = [System.Security.SecurityElement]::Escape($powershell)
$escapedRunner = [System.Security.SecurityElement]::Escape($runner)
$escapedWorkspace = [System.Security.SecurityElement]::Escape($PSScriptRoot)
$nextMonth = (Get-Date -Day 1).Date.AddMonths(1).AddHours(5).ToString("yyyy-MM-ddTHH:mm:ss")
$monthlyXml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Monthly GPT-5.6 Sol xhigh research audit with official-source web research.</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>$nextMonth</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByMonth>
        <DaysOfMonth><Day>1</Day></DaysOfMonth>
        <Months>
          <January/><February/><March/><April/><May/><June/>
          <July/><August/><September/><October/><November/><December/>
        </Months>
      </ScheduleByMonth>
    </CalendarTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>$escapedIdentity</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>$escapedPowerShell</Command>
      <Arguments>-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File &quot;$escapedRunner&quot; -Task monthly</Arguments>
      <WorkingDirectory>$escapedWorkspace</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
Register-ScheduledTask -TaskName "Polymaker Sports Major Research Audit" -Xml $monthlyXml -Force | Out-Null

& (Join-Path $PSScriptRoot "install_bot_watchdog.ps1")

Write-Output "Registered sports automations:"
Get-ScheduledTask -TaskName "Polymaker Sports*", "Polymaker Bot Watchdog" |
    Select-Object TaskName, State, Description |
    Format-Table -AutoSize

