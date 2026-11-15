# =====================================================================
#  MediaScan tray icon
#  Shows the worker state in the system tray and opens the MediaScan app (manager.py).
#  Everything else (start/stop, log, status) lives in that app.
#  Reads status\status.json and status\manager.json; commands go to control\*.json.
# =====================================================================
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()
$ErrorActionPreference = 'Continue'
$Base  = Join-Path $env:LOCALAPPDATA 'MediaScan'
$StatF = Join-Path $Base 'status\status.json'
$MgrF  = Join-Path $Base 'status\manager.json'
$Ctl   = Join-Path $Base 'control'
$Utf8  = New-Object System.Text.UTF8Encoding($false)
$NL    = (Get-UICulture).TwoLetterISOLanguageName -eq 'nl'
function L([string]$nl, [string]$en) { if ($NL) { $nl } else { $en } }

$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\MediaScanTray', [ref]$created)
if (-not $created) { exit 0 }

function New-Icon([System.Drawing.Color]$c) {
  $bmp = New-Object System.Drawing.Bitmap 32, 32
  $g = [System.Drawing.Graphics]::FromImage($bmp)
  $g.SmoothingMode = 'AntiAlias'; $g.TextRenderingHint = 'AntiAliasGridFit'; $g.Clear([System.Drawing.Color]::Transparent)
  $path = New-Object System.Drawing.Drawing2D.GraphicsPath
  $r = 9; $path.AddArc(1, 1, $r * 2, $r * 2, 180, 90); $path.AddArc(31 - $r * 2, 1, $r * 2, $r * 2, 270, 90)
  $path.AddArc(31 - $r * 2, 31 - $r * 2, $r * 2, $r * 2, 0, 90); $path.AddArc(1, 31 - $r * 2, $r * 2, $r * 2, 90, 90); $path.CloseFigure()
  $g.FillPath((New-Object System.Drawing.SolidBrush $c), $path)
  $font = New-Object System.Drawing.Font('Segoe UI', 18, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
  $sf = New-Object System.Drawing.StringFormat; $sf.Alignment = 'Center'; $sf.LineAlignment = 'Center'
  $g.DrawString('M', $font, (New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(17, 17, 17))), (New-Object System.Drawing.RectangleF(0, 1, 32, 32)), $sf)
  $g.Dispose()
  return [System.Drawing.Icon]::FromHandle($bmp.GetHicon())
}
$Icons = @{
  busy = New-Icon ([System.Drawing.Color]::FromArgb(255, 138, 31))   # orange: working
  idle = New-Icon ([System.Drawing.Color]::FromArgb(63, 185, 80))    # green: waiting for work
  off  = New-Icon ([System.Drawing.Color]::FromArgb(140, 140, 150))  # grey: stopped
  err  = New-Icon ([System.Drawing.Color]::FromArgb(240, 96, 90))    # red: problem
}
# the manager writes proper multi-size icons (16-48 px) to icons\; use them when present (sharp at any scaling)
$IconDir = Join-Path $Base 'icons'; $IconSize = [System.Windows.Forms.SystemInformation]::SmallIconSize
foreach ($k in @('busy', 'idle', 'off', 'err')) {
  $f = Join-Path $IconDir "tray-$k.ico"
  if (Test-Path $f) { try { $Icons[$k] = New-Object System.Drawing.Icon($f, $IconSize) } catch {} }
}

function Read-Json([string]$p) { try { return ([IO.File]::ReadAllText($p, $Utf8) | ConvertFrom-Json) } catch { return $null } }
function Send-Cmd([string]$action) {
  New-Item -ItemType Directory -Force -Path $Ctl | Out-Null
  # write under a temporary name, then rename: the manager only reads complete *.json files
  $name = (Get-Date -Format 'yyyyMMdd-HHmmss-fff') + '_' + [guid]::NewGuid().ToString('N').Substring(0, 6) + "_tray_$action"
  $tmp = Join-Path $Ctl "$name.tmp"
  [IO.File]::WriteAllText($tmp, (@{ action = $action; job = 'worker' } | ConvertTo-Json -Compress), $Utf8)
  Move-Item -LiteralPath $tmp -Destination (Join-Path $Ctl "$name.json") -Force
}
function Manager-Exe {
  $pyw = Join-Path $Base 'venv\Scripts\pythonw.exe'
  return @($pyw, ('"' + (Join-Path $Base 'manager.py') + '"'))
}
function Open-App {
  # manager.py --open: opens the app window, and starts the manager first if it is not running
  $m = Manager-Exe
  Start-Process -FilePath $m[0] -ArgumentList @($m[1], '--open') -WindowStyle Hidden
}

$ni = New-Object System.Windows.Forms.NotifyIcon
$ni.Icon = $Icons.off; $ni.Text = 'MediaScan'; $ni.Visible = $true
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$miOpen = $menu.Items.Add((L 'MediaScan openen' 'Open MediaScan'))
$miOpen.Font = New-Object System.Drawing.Font($miOpen.Font, [System.Drawing.FontStyle]::Bold)
$miDash = $menu.Items.Add((L 'Dashboard openen' 'Open dashboard'))
[void]$menu.Items.Add('-')
$miRestart = $menu.Items.Add((L 'Worker herstarten' 'Restart worker'))
$miQuit = $menu.Items.Add((L 'Afsluiten (alles stoppen)' 'Quit (stop everything)'))
$ni.ContextMenuStrip = $menu

$miOpen.add_Click({ Open-App })
$ni.add_MouseClick({ param($s, $e) if ($e.Button -eq 'Left') { Open-App } })
$miDash.add_Click({
  $st = Read-Json $StatF
  if ($st -and $st.worker -and $st.worker.server -match '^https?://') { Start-Process $st.worker.server } else { Open-App }
})
$miRestart.add_Click({ Send-Cmd 'restart'; $ni.ShowBalloonTip(3000, 'MediaScan', (L 'Worker wordt herstart.' 'Restarting the worker.'), 'Info') })
$miQuit.add_Click({
  $q = L "MediaScan afsluiten?`r`n`r`nDe worker en het beheer stoppen. Een lopende job gaat terug in de wachtrij. Bij de volgende keer inloggen start alles weer." `
         "Quit MediaScan?`r`n`r`nThis stops the worker and the manager. A running job goes back to the queue. Everything starts again at your next sign-in."
  if ([System.Windows.Forms.MessageBox]::Show($q, 'MediaScan', 'YesNo', 'Question') -eq 'Yes') {
    Send-Cmd 'quit'; $ni.Visible = $false; [System.Windows.Forms.Application]::Exit()
  }
})

# state -> icon + tooltip; restart the manager if it stopped responding
$script:lastFix = [DateTime]::MinValue
$timer = New-Object System.Windows.Forms.Timer; $timer.Interval = 3000
$timer.add_Tick({
  try {
    $st = Read-Json $StatF
    $age = 9999
    if ($st) { try { $age = ((Get-Date) - [DateTime]::Parse($st.heartbeat)).TotalSeconds } catch {} }
    if ($st -and $st.quit) { $ni.Visible = $false; [System.Windows.Forms.Application]::Exit(); return }
    if ($age -gt 60) {
      $ni.Icon = $Icons.err; $ni.Text = 'MediaScan - ' + (L 'beheer reageert niet' 'manager not responding')
      if (((Get-Date) - $script:lastFix).TotalMinutes -ge 3) {
        $script:lastFix = Get-Date
        try { Start-ScheduledTask -TaskName 'MediaScan Manager' -ErrorAction Stop }
        catch { try { Start-ScheduledTask -TaskName 'MediaScan Beheer' -ErrorAction Stop }   # task name of older installs
               catch { $m = Manager-Exe; Start-Process -FilePath $m[0] -ArgumentList $m[1] -WindowStyle Hidden } }
      }
      return
    }
    $j = $st.jobs.worker; $w = $st.worker
    if (-not $j.running) {
      if ($j.gave_up) { $ni.Icon = $Icons.err; $t = L 'worker crasht steeds' 'worker keeps crashing' }
      else { $ni.Icon = $Icons.off; $t = L 'worker staat uit' 'worker stopped' }
    } elseif (-not $w) { $ni.Icon = $Icons.busy; $t = L 'worker start op' 'worker starting' }
    elseif ($w.phase -eq 'waiting_media') { $ni.Icon = $Icons.err; $t = L 'wacht op de media-share' 'waiting for the media share' }
    elseif ($w.server_ok -eq $false) { $ni.Icon = $Icons.err; $t = L 'server niet bereikbaar' 'server unreachable' }
    elseif ($w.phase -and $w.phase -ne 'ready') { $ni.Icon = $Icons.busy; $t = L 'verbinden met de server' 'connecting to the server' }
    elseif ($w.jobs -and @($w.jobs.PSObject.Properties).Count) {
      $ni.Icon = $Icons.busy
      $p = @($w.jobs.PSObject.Properties)[0].Value
      $t = [IO.Path]::GetFileNameWithoutExtension([string]$p.rel) + ' ' + [int]$p.progress + '%'
    } else { $ni.Icon = $Icons.idle; $t = L 'wacht op werk' 'waiting for work' }
    $tip = 'MediaScan - ' + $t
    $ni.Text = $tip.Substring(0, [Math]::Min(63, $tip.Length))       # Windows limit
  } catch {}
})
$timer.Start()
[System.Windows.Forms.Application]::Run()
$ni.Visible = $false; $ni.Dispose()
