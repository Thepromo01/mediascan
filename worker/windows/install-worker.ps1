# =====================================================================
#  MediaScan worker - installer for Windows 10/11
#  - checks the PC and installs what is needed (Python, ffmpeg with VMAF)
#  - asks for the settings and immediately tests the connection to the dashboard
#  - optional: subtitles (Whisper on the NVIDIA GPU, ffsubsync, Ollama + translation model)
#  - sets up the management service + tray icon that keep the worker running and up to date
#  Everything goes into %LOCALAPPDATA%\MediaScan. Safe to run again at any time (to update or change settings).
# =====================================================================
$ErrorActionPreference = 'Stop'
$Src  = $PSScriptRoot
$Base = Join-Path $env:LOCALAPPDATA 'MediaScan'
$Utf8 = New-Object System.Text.UTF8Encoding($false)
New-Item -ItemType Directory -Force -Path $Base | Out-Null
Start-Transcript -Path (Join-Path $Base 'install-log.txt') -Force | Out-Null
function Step([string]$m) { Write-Host ''; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Warn([string]$m) { Write-Host "  WARNING: $m" -ForegroundColor Yellow }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User') }
function Ask([string]$q, [string]$def) { $a = Read-Host ($(if ($def) { "$q [$def]" } else { $q })); if ($a) { $a.Trim() } else { $def } }
function Yes([string]$q, [bool]$def) { $a = Read-Host ($q + $(if ($def) { ' (Y/n)' } else { ' (y/N)' })); if (-not $a) { $def } else { $a -match '^[yYjJ]' } }
function Ini-Get([string]$file, [string]$sec, [string]$key) {
  # reads config.ini (older installs), worker.ini and overrides.ini; the last value wins, just like in the worker
  $val = ''
  foreach ($f in @((Join-Path (Split-Path $file) 'config.ini'), $file, (Join-Path (Split-Path $file) 'overrides.ini'))) {
    if (-not (Test-Path $f)) { continue }
    $cur = ''
    foreach ($l in [IO.File]::ReadAllLines($f)) {
      if ($l -match '^\s*\[(.+)\]\s*$') { $cur = $Matches[1]; continue }
      if ($cur -eq $sec -and $l -match "^\s*$key\s*=\s*(.*)$" -and $Matches[1].Trim()) { $val = $Matches[1].Trim() }
    }
  }
  return $val
}
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
  throw 'winget is missing. Install "App Installer" from the Microsoft Store and run this script again.'
}

Step '1/6 System check'
$cpu  = Get-CimInstance Win32_Processor | Select-Object -First 1
$ram  = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB)
$gpus = @(Get-CimInstance Win32_VideoController | Where-Object { $_.Name -notmatch 'Basic|Virtual|Remote' } | ForEach-Object { $_.Name })
$freeC = [math]::Round((Get-PSDrive -Name $env:LOCALAPPDATA.Substring(0,1)).Free / 1GB)
$nvidia = $gpus -match 'NVIDIA'
Write-Host ("CPU       : {0} ({1} cores)" -f $cpu.Name.Trim(), $cpu.NumberOfCores)
Write-Host ("Memory    : {0} GB" -f $ram)
Write-Host ("GPU       : {0}" -f ($gpus -join ', '))
Write-Host ("Free      : {0} GB on {1}:" -f $freeC, $env:LOCALAPPDATA.Substring(0,1))
if ($cpu.NumberOfCores -lt 4) { Warn 'fewer than 4 CPU cores (quality measurement will be slow)' }
if ($ram -lt 8) { Warn 'less than 8 GB of memory' }
if ($freeC -lt 100) { Warn 'less than 100 GB free: large movies may not fit in the work folder' }
if (-not ($gpus -match 'NVIDIA|Arc|Intel|Radeon|AMD')) { Warn 'no suitable GPU found: encoding will use the CPU (slow)' }

Step '2/6 Python and ffmpeg'
$py = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
if (-not (Test-Path $py)) {
  winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements | Out-Host
  Refresh-Path
}
if (-not (Test-Path $py)) { $c = Get-Command python.exe -ErrorAction SilentlyContinue; if ($c -and $c.Path -notlike '*WindowsApps*') { $py = $c.Path } }
if (-not (Test-Path $py)) { throw 'Python 3.12 not found after installation - run this script again' }
$venv = Join-Path $Base 'venv'; $vpy = Join-Path $venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) { & $py -m venv $venv }
& $vpy --version
$ff = Get-Command ffmpeg.exe -ErrorAction SilentlyContinue
$hasVmaf = $false
if ($ff) { $hasVmaf = ((& $ff.Path -hide_banner -filters 2>$null) -join "`n") -match 'libvmaf' }
if (-not $hasVmaf) {
  winget install -e --id Gyan.FFmpeg --accept-package-agreements --accept-source-agreements | Out-Host
  Refresh-Path
}

Step '3/6 Files'
if ((Resolve-Path $Src).Path.TrimEnd('\') -ieq (Resolve-Path $Base).Path.TrimEnd('\')) {
  Write-Host "Running from $Base itself: the files are already in place"
} else {
  foreach ($f in 'worker.py','subs.py','langs.py','manager.py','tray.ps1','install-manager.ps1') {
    Copy-Item (Join-Path $Src $f) (Join-Path $Base $f) -Force
  }
  Write-Host "Installed in $Base"
}

Step '4/6 Settings'
Stop-Transcript | Out-Null          # keep keys and passwords out of the install log
$ini = Join-Path $Base 'worker.ini'
$pre = Join-Path $Src 'worker.ini'  # a package downloaded from the dashboard ships the address and key already filled in
function Test-Dashboard([string]$u, [string]$t) {
  try {
    $r = Invoke-RestMethod -Method Post -Uri "$u/api/worker/update" -Headers @{ Authorization = "Bearer $t" } -ContentType 'application/json' -Body '{}' -TimeoutSec 10
    Write-Host ("  Connection OK (dashboard serves worker {0})" -f $r.version) -ForegroundColor Green; return $true
  } catch { Warn "dashboard unreachable or key incorrect: $($_.Exception.Message)"; return $false }
}
function Find-Media([string]$u, [string]$t) {
  # The dashboard tells which folders the library starts with; the drive that has them is the media share.
  try { $roots = @((Invoke-RestMethod -Uri "$u/api/worker/roots" -Headers @{ Authorization = "Bearer $t" } -TimeoutSec 10).roots) } catch { return '' }
  if (-not $roots.Count) { return '' }
  $best = ''; $bestN = 0
  foreach ($d in Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match '^[A-Z]:\\$' }) {
    $n = @($roots | Where-Object { Test-Path -LiteralPath (Join-Path $d.Root $_) -PathType Container }).Count
    if ($n -gt $bestN) { $bestN = $n; $best = $d.Root }
  }
  if ($bestN -ge [math]::Max(1, [math]::Ceiling($roots.Count / 2))) { return $best }
  return ''
}
# connection: existing settings, else the values shipped in the package, else ask
$url = Ini-Get $ini 'server' 'url'; $token = Ini-Get $ini 'server' 'worker_token'
if (-not ($url -and $token) -and (Test-Path $pre)) { $url = Ini-Get $pre 'server' 'url'; $token = Ini-Get $pre 'server' 'worker_token' }
$url = $url.TrimEnd('/'); if ($url -and $url -notmatch '^https?://') { $url = "http://$url" }
$ok = ($url -and $token) -and (Test-Dashboard $url $token)
while (-not $ok) {
  $url   = (Ask 'Dashboard address, e.g. http://192.168.1.10:8780' $url).TrimEnd('/'); if ($url -and $url -notmatch '^https?://') { $url = "http://$url" }
  $token = Ask 'Worker key (dashboard > Workers > Add worker)' $token
  $ok = Test-Dashboard $url $token
  if (-not $ok -and -not (Yes 'Try again?' $true)) { break }
}
# media share: keep a working setting, else look for the library on the drives, else ask
$media = Ini-Get $ini 'worker' 'media_root'
if (-not ($media -and (Test-Path -LiteralPath $media))) {
  $found = if ($ok) { Find-Media $url $token } else { '' }
  if ($found) { $media = $found; Write-Host "  Media share found: $media" -ForegroundColor Green }
  else {
    $media = Ask 'Drive letter or path of the media share (e.g. M:\ or \\NAS\Media)' $(if ($media) { $media } else { 'M:\' })
    if ($media -match '^\\\\' -and (Yes "Map $media to a drive letter? (recommended)" $true)) {
      $free = @('M','N','P','Q','R','S','T','V','W','X','Y','Z') | Where-Object { -not (Test-Path "$($_):\") } | Select-Object -First 1
      $letter = Ask '  Drive letter' $free
      & net use "$($letter):" $media /persistent:yes | Out-Host
      if ($LASTEXITCODE -eq 0) { $media = "$($letter):\" } else { Warn "mapping failed (a username may be needed: net use $($letter): $media /user:NAME /persistent:yes)" }
    }
    if (-not (Test-Path -LiteralPath $media)) { Warn "$media is not reachable right now; the worker waits until it is available" }
  }
}
# the rest keeps its current value or a sensible default; change it later in the MediaScan app (Settings)
$name = Ini-Get $ini 'worker' 'name'; if (-not $name) { $name = $env:COMPUTERNAME }
$maxh = Ini-Get $ini 'worker' 'max_height'; if (-not $maxh) { $maxh = '0' }
$cur = Ini-Get $ini 'worker' 'subs'
if ($cur) { $subs = ($cur -eq 'true') }
else { $subs = Yes 'Also create subtitles on this machine? (recommended with an NVIDIA GPU of 8+ GB; ~20 GB extra disk space)' ([bool]$nvidia) }
$os_key = Ini-Get $ini 'opensubtitles' 'api_key'; $os_user = Ini-Get $ini 'opensubtitles' 'username'; $os_pw = Ini-Get $ini 'opensubtitles' 'password'
$whisper = Ini-Get $ini 'whisper' 'model'; if (-not $whisper) { $whisper = 'large-v3' }
$ourl = Ini-Get $ini 'translation' 'ollama_url'; if (-not $ourl) { $ourl = 'http://localhost:11434' }
$model = Ini-Get $ini 'translation' 'ollama_model'; if (-not $model) { $model = Ini-Get $ini 'vertaling' 'ollama_model' }   # [vertaling] = old section name
if (-not $model) {
  $vram = 0
  try { $vram = [int](& nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | Select-Object -First 1) } catch {}
  $model = if ($vram -ge 12000) { 'gemma4:12b' } elseif ($vram -ge 7000) { 'gemma3:4b' } else { 'gemma3:1b' }
}
$osl = (@(@('api_key', $os_key), @('username', $os_user), @('password', $os_pw)) | Where-Object { $_[1] } | ForEach-Object { "$($_[0]) = $($_[1])" }) -join "`r`n"
$txt = @"
[server]
url = $url
worker_token = $token

[worker]
name = $name
media_root = $media
encoder = auto
max_height = $maxh
subs = $($subs.ToString().ToLower())
auto_update = true

[opensubtitles]
$osl

[whisper]
model = $whisper

[translation]
ollama_url = $ourl
ollama_model = $model
"@
[IO.File]::WriteAllText($ini, $txt, $Utf8)
# only this user may read the file (keys and password)
& icacls $ini /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null
Write-Host '  Settings saved. Change them any time in the MediaScan app (tray icon > Settings).'
$subsOn = (Ini-Get $ini 'worker' 'subs') -eq 'true'
Start-Transcript -Path (Join-Path $Base 'install-log.txt') -Append | Out-Null

Step '5/6 Subtitles'
if ($subsOn) {
  & $vpy -m pip install --upgrade pip | Out-Host
  $pk = @('faster-whisper', 'ffsubsync', 'srt', 'charset-normalizer', 'numpy')
  if ($nvidia) { $pk += @('nvidia-cublas-cu12', 'nvidia-cudnn-cu12==9.*') }
  & $vpy -m pip install @pk | Out-Host
  if ($LASTEXITCODE -ne 0) { Warn 'package installation failed - see install-log.txt' }
  $ollama = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
  if (-not (Test-Path $ollama)) {
    winget install -e --id Ollama.Ollama --accept-package-agreements --accept-source-agreements | Out-Host
    Refresh-Path
  }
  if (-not (Test-Path $ollama)) { $c = Get-Command ollama.exe -ErrorAction SilentlyContinue; if ($c) { $ollama = $c.Path } }
  if (Test-Path $ollama) {
    $up = $false
    try { Invoke-RestMethod 'http://localhost:11434/api/tags' -TimeoutSec 3 | Out-Null; $up = $true } catch {}
    if (-not $up) { Start-Process $ollama -ArgumentList 'serve' -WindowStyle Hidden; Start-Sleep -Seconds 5 }
    $model = Ini-Get $ini 'translation' 'ollama_model'; if (-not $model) { $model = Ini-Get $ini 'vertaling' 'ollama_model' }
    Write-Host "Downloading translation model $model (one time, a few GB)..."
    & $ollama pull $model
    if ($LASTEXITCODE -ne 0) { Warn "downloading $model failed - retry later with: ollama pull $model" }
  } else { Warn 'Ollama not found: translation only works once Ollama is running' }
} else { Write-Host 'Skipped (encoding only). To enable later: run this installer again.' }

Step '6/6 Management service and tray icon'
Remove-Item -LiteralPath (Join-Path $Base 'supervisor.ps1') -Force -ErrorAction SilentlyContinue   # replaced by manager.py
& (Join-Path $Base 'install-manager.ps1')
Write-Host ''
Write-Host 'Done. The worker starts within a minute and appears in the dashboard under Workers.' -ForegroundColor Green
Write-Host 'Click the MediaScan icon in the system tray (or Start > MediaScan) to see what the worker is doing.'
Stop-Transcript | Out-Null
