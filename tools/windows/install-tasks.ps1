# One-time setup for the campaign scripts. Run elevated. Read it first: it changes your system.
# Creates:
#   - task RlapScewinAgent (on demand, highest privileges): scewin-agent.ps1
#   - task RlapHelper (on demand, highest privileges): helper-agent.ps1
#   - task "RaptorLakeAutopilot Failsafe" (SYSTEM, at startup): ..\failsafe.ps1 with -FailsafeDir
#   - bcdedit: recoveryenabled No, bootstatuspolicy IgnoreAllFailures (failed boots retry instead of stopping in
#     Windows Recovery). Undo with -Remove.
# Folder rights: the scripts the tasks run must be writable by administrators only, or anyone who can edit them
# owns the machine. The password file must be readable by administrators only.
param(
  [Parameter(Mandatory = $true)][string]$Scewin,
  [Parameter(Mandatory = $true)][string]$Stage,
  [Parameter(Mandatory = $true)][string]$HelperDir,
  [Parameter(Mandatory = $true)][string]$FailsafeDir,
  [string]$PasswordFile = '',
  [switch]$Remove
)
$here = $PSScriptRoot
$fsTask = 'RaptorLakeAutopilot Failsafe'
if ($Remove) {
  foreach ($t in 'RlapScewinAgent', 'RlapHelper', $fsTask) { Unregister-ScheduledTask -TaskName $t -Confirm:$false -ErrorAction SilentlyContinue }
  bcdedit /set '{current}' recoveryenabled Yes | Out-Null
  bcdedit /deletevalue '{current}' bootstatuspolicy | Out-Null
  'removed'; exit
}
New-Item -ItemType Directory -Force -Path $Stage, $HelperDir, $FailsafeDir | Out-Null
if ($PasswordFile) { icacls $PasswordFile /inheritance:r /grant:r 'Administrators:F' 'SYSTEM:F' | Out-Null }
@{ scewin = $Scewin; password_file = $PasswordFile } | ConvertTo-Json | Out-File (Join-Path $FailsafeDir 'failsafe.json') -Encoding utf8
$user = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\scewin-agent.ps1`" -Scewin `"$Scewin`" -Stage `"$Stage`" -PasswordFile `"$PasswordFile`"")
Register-ScheduledTask -TaskName 'RlapScewinAgent' -Action $a -Principal $user -Settings $set -Force | Out-Null
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$here\helper-agent.ps1`" -Dir `"$HelperDir`"")
Register-ScheduledTask -TaskName 'RlapHelper' -Action $a -Principal $user -Settings $set -Force | Out-Null
$a = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ("-NoProfile -ExecutionPolicy Bypass -File `"$here\..\failsafe.ps1`" -Dir `"$FailsafeDir`"")
$sys = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$fsSet = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5) -Priority 0
Register-ScheduledTask -TaskName $fsTask -Action $a -Trigger (New-ScheduledTaskTrigger -AtStartup) -Principal $sys -Settings $fsSet -Force | Out-Null
bcdedit /set '{current}' recoveryenabled No | Out-Null
bcdedit /set '{current}' bootstatuspolicy IgnoreAllFailures | Out-Null
'tasks: ' + ((Get-ScheduledTask -TaskName 'RlapScewinAgent', 'RlapHelper', $fsTask | % { $_.TaskName + '=' + $_.State }) -join ', ')
