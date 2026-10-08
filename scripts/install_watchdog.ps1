param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$ConfigPath = 'config.http-api.yaml',
    [string]$TaskName = 'WangWechatTwin-Backend'
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$taskPython = Join-Path $taskRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $taskPython)) { throw 'Create the project venv first.' }
$taskConfig = Join-Path $taskRoot $ConfigPath
if (-not (Test-Path -LiteralPath $taskConfig)) { throw 'The selected config does not exist.' }
$action = New-ScheduledTaskAction -Execute $taskPython -Argument ('-u -m app.supervisor --config "' + $taskConfig + '"') -WorkingDirectory $taskRoot
$logon = New-ScheduledTaskTrigger -AtLogOn
$retry = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger @($logon, $retry) -Settings $settings -Principal $principal -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Output 'Installed the independent Windows backend task.'
