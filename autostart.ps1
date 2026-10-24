# Start Mission Control at logon, hidden (Windows Task Scheduler).
#   powershell -ExecutionPolicy Bypass -File autostart.ps1           install / update
#   powershell -ExecutionPolicy Bypass -File autostart.ps1 -Remove   uninstall
param([switch]$Remove)
$name = "Mission Control"
Unregister-ScheduledTask -TaskName $name -Confirm:$false -ErrorAction SilentlyContinue
if ($Remove) { Write-Host "Автозапуск убран / Autostart removed"; exit 0 }

$run = Join-Path $PSScriptRoot "run.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" -WorkingDirectory $PSScriptRoot `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$run`""
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
# Restart a crashed server every minute, up to a day of retries; no time limit on the task.
$settings = New-ScheduledTaskSettingsSet -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 1440 `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $name
Write-Host "Автозапуск включён / Autostart enabled. Убрать / Remove: autostart.ps1 -Remove"
