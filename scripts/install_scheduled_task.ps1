[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$taskName = 'WestBusanAccommodationDaily'
$repository = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$dailyScript = (Resolve-Path -LiteralPath (Join-Path $repository 'scripts\run_daily.ps1')).Path
$relativeScript = [IO.Path]::GetRelativePath($repository, $dailyScript)

if ([IO.Path]::IsPathRooted($relativeScript) -or $relativeScript.StartsWith('..')) {
    throw 'The resolved daily script is outside the repository.'
}
if ([TimeZoneInfo]::Local.Id -ne 'Korea Standard Time') {
    throw 'Set the Windows time zone to Korea Standard Time before registration.'
}

$powerShell = (Get-Command powershell.exe -ErrorAction Stop).Source
$arguments = '-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}"' -f $dailyScript
$action = New-ScheduledTaskAction -Execute $powerShell -Argument $arguments -WorkingDirectory $repository
$trigger = New-ScheduledTaskTrigger -Daily -At '04:30'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -Hidden

Register-ScheduledTask `
    -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Description 'West Busan accommodation evidence pipeline at 04:30 Asia/Seoul' `
    -Force | Out-Null

Write-Output "Registered exact task: $taskName"
