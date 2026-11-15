# Installs the MediaScan manager (manager.py) as a Windows scheduled task that starts at sign-in.
# The manager keeps the worker running and starts the tray icon; the Start menu item "MediaScan" opens its app.
$Base = Join-Path $env:LOCALAPPDATA 'MediaScan'
$user = "$env:USERDOMAIN\$env:USERNAME"
$pyw  = Join-Path $Base 'venv\Scripts\pythonw.exe'
$mgr  = Join-Path $Base 'manager.py'
$py   = Join-Path $Base 'venv\Scripts\python.exe'
if (-not (Test-Path $pyw)) { throw "pythonw.exe not found in $Base\venv - run the worker installer first" }

$set  = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$prin = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$trig = New-ScheduledTaskTrigger -AtLogOn -User $user
$act  = New-ScheduledTaskAction -Execute $pyw -Argument "`"$mgr`"" -WorkingDirectory $Base
# the task was called 'MediaScan Beheer' (Dutch) before; remove it so only 'MediaScan Manager' remains
if (Get-ScheduledTask -TaskName 'MediaScan Beheer' -ErrorAction SilentlyContinue) { Unregister-ScheduledTask -TaskName 'MediaScan Beheer' -Confirm:$false -ErrorAction SilentlyContinue }
Register-ScheduledTask -TaskName 'MediaScan Manager' -Action $act -Trigger $trig -Settings $set -Principal $prin -Force | Out-Null
# older versions had a separate task for the tray icon; the manager starts it now
Unregister-ScheduledTask -TaskName 'MediaScan Tray' -Confirm:$false -ErrorAction SilentlyContinue

# icon files (Start menu shortcut, tray), then the Start menu item: "MediaScan" opens the app (and starts the manager if needed)
if (Test-Path $py) { Start-Process -FilePath $py -ArgumentList @("`"$mgr`"", '--icons') -Wait -WindowStyle Hidden }
$lnk = Join-Path ([Environment]::GetFolderPath('Programs')) 'MediaScan.lnk'
$ws = New-Object -ComObject WScript.Shell; $s = $ws.CreateShortcut($lnk)
$s.TargetPath = $pyw; $s.Arguments = "`"$mgr`" --open"; $s.WorkingDirectory = $Base; $s.Description = 'MediaScan'
$s.IconLocation = (Join-Path $Base 'icons\mediascan.ico') + ',0'; $s.Save()

# Switch over now, unless the worker is busy (that would abort a running encode): then it happens at the next sign-in.
$busy = @(Get-ChildItem -LiteralPath (Join-Path $Base 'work') -ErrorAction SilentlyContinue).Count -gt 0
$old = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" | Where-Object { $_.CommandLine -match 'MediaScan\\supervisor\.ps1' })
if ($busy) {
  # don't abort a running encode. An already running manager restarts itself after the worker has updated
  # (the worker sees its new files, finishes the job and exits); the old PowerShell service (< 1.0) keeps
  # running until the next sign-in - manager.py does not start while it runs.
  Write-Host 'The worker is busy; the new version takes over after the current job (or at your next sign-in).' -ForegroundColor Yellow
  return
}
$old | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pythonw.exe'" |
  Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -match 'MediaScan\\(tray\.ps1|manager\.py)' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Remove-Item -LiteralPath (Join-Path $Base 'status\manager.json') -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Start-ScheduledTask -TaskName 'MediaScan Manager'
Write-Host 'Waiting for the manager...'
$ok = $false
for ($i = 0; $i -lt 20 -and -not $ok; $i++) { Start-Sleep -Seconds 2; $ok = Test-Path (Join-Path $Base 'status\manager.json') }
if ($ok) { Write-Host 'MediaScan is running. The MediaScan icon is in the system tray (maybe under the ^ arrow); click it to open the app.' -ForegroundColor Green }
else { Write-Host 'Task created, but the manager did not start yet. See %LOCALAPPDATA%\MediaScan\logs\manager.log.' -ForegroundColor Yellow }
