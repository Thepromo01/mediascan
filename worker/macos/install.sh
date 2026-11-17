#!/usr/bin/env bash
# MediaScan worker - installer for macOS (Apple Silicon)
# Usage: open Terminal in this folder and run:  bash install.sh
set -euo pipefail
cd "$(dirname "$0")"
SRC="$(pwd)"
HOME_DIR="$HOME/Library/Application Support/MediaScan"
LABEL="nl.mediascan.worker"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
echo "== Installing MediaScan worker in $HOME_DIR"

# 0) check: Apple Silicon and macOS 13+
ARCH="$(uname -m)"; VER="$(sw_vers -productVersion)"; MAJOR="${VER%%.*}"
if [ "$ARCH" != "arm64" ]; then
  echo "!! This is an Intel Mac. Encoding can only use the CPU (x265, slow)."
  echo "   Tip: set this worker to 'subtitles only' or 'up to 720p' in the dashboard."
fi
if [ "$MAJOR" -lt 13 ]; then echo "!! macOS $VER is too old - macOS 13 Ventura or newer is required."; exit 1; fi

# 1) Homebrew
if ! command -v brew >/dev/null; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do [ -x "$b" ] && eval "$("$b" shellenv)"; done
fi
if ! command -v brew >/dev/null; then
  echo "!! Homebrew is missing. Install it first from https://brew.sh, then run this script again."; exit 1
fi
BREW_PREFIX="$(brew --prefix)"

# 2) ffmpeg with libvmaf
brew list ffmpeg >/dev/null 2>&1 || brew list homebrew-ffmpeg/ffmpeg/ffmpeg >/dev/null 2>&1 || brew install ffmpeg
if ! ffmpeg -hide_banner -filters 2>/dev/null | grep -q libvmaf; then
  echo "== This ffmpeg has no VMAF; installing a build with libvmaf (homebrew-ffmpeg)"
  brew uninstall --ignore-dependencies ffmpeg 2>/dev/null || true
  brew tap homebrew-ffmpeg/ffmpeg
  brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-libvmaf
fi
ffmpeg -hide_banner -filters 2>/dev/null | grep -q libvmaf && echo "   libvmaf: ok" || { echo "!! ffmpeg without libvmaf - encoding is not possible"; }
ffmpeg -hide_banner -encoders 2>/dev/null | grep -q hevc_videotoolbox && echo "   Apple VideoToolbox HEVC: ok"

# 3) Python environment
PY="$(command -v python3.12 || true)"
[ -n "$PY" ] || { brew install python@3.12; PY="$BREW_PREFIX/bin/python3.12"; }
mkdir -p "$HOME_DIR"
cp "$SRC/worker.py" "$SRC/subs.py" "$SRC/langs.py" "$HOME_DIR/"
[ -d "$HOME_DIR/venv" ] || "$PY" -m venv "$HOME_DIR/venv"
VPY="$HOME_DIR/venv/bin/python"
"$VPY" -m pip install -q --upgrade pip

# 4) settings (everything in worker.ini, readable only by you)
cd "$HOME_DIR"
ask() { local a; read -rp "$1${2:+ [$2]}: " a; echo "${a:-$2}"; }
yes() { local a; read -rp "$1 ($([ "$2" = 1 ] && echo Y/n || echo y/N)): " a; [ -z "$a" ] && [ "$2" = 1 ] && return 0; [[ "$a" =~ ^[yYjJ] ]]; }
ini() { { cat config.ini worker.ini 2>/dev/null || true; } | awk -F' *= *' -v s="[$1]" -v k="$2" '$0==s{f=1;next} /^\[/{f=0} f&&$1==k{v=$2} END{print v}'; }
ini_tr() { local v; v="$(ini translation "$1")"; [ -n "$v" ] || v="$(ini vertaling "$1")"; echo "$v"; }   # [vertaling] = old name of [translation]
# a package downloaded from the dashboard ships a worker.ini with the address and key already filled in
pre() { [ -f "$SRC/worker.ini" ] && awk -F' *= *' -v s="[server]" -v k="$1" '$0==s{f=1;next} /^\[/{f=0} f&&$1==k{print $2;exit}' "$SRC/worker.ini" || true; }
if [ ! -f worker.ini ] || yes "Settings already exist. Enter them again?" 0; then
  while :; do
    DU="$(ini server url)"; [ -n "$DU" ] || DU="$(pre url)"; DT="$(ini server worker_token)"; [ -n "$DT" ] || DT="$(pre worker_token)"
    URL="$(ask 'Dashboard address, e.g. http://192.168.1.10:8780' "$DU")"; URL="${URL%/}"; [[ "$URL" =~ ^https?:// ]] || URL="http://$URL"
    TOKEN="$(ask "Worker key (dashboard > Workers > Add worker)" "$DT")"
    if V="$(curl -fsS -m 10 -X POST -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' -d '{}' "$URL/api/worker/update")"; then
      echo "   Connection OK ($V)"; break
    fi
    echo "!! Dashboard unreachable or key incorrect."; yes "Try again?" 1 || break
  done
  echo "Connect the media share in Finder first (Go > Connect to Server > smb://<nas>/<share>)."
  MEDIA="$(ask 'Path to the media share' "$(ini worker media_root)")"; MEDIA="${MEDIA:-/Volumes/Media}"
  DEFNAME="$(scutil --get ComputerName 2>/dev/null | tr ' ' '-' || hostname -s)"
  NAME="$(ask 'Name of this worker' "$(ini worker name)")"; NAME="${NAME:-$DEFNAME}"
  MAXH="$(ask 'Maximum resolution (0 = everything, 1080 = up to Full HD)' "$(ini worker max_height)")"; MAXH="${MAXH:-0}"
  MEM=$(( $(sysctl -n hw.memsize) / 1073741824 ))
  SUBS=false; OSKEY=""; OSUSER=""; OSPW=""; MODEL=""
  if yes "Also create subtitles? (recommended: 24 GB of memory or more, this Mac has $MEM GB)" "$([ "$MEM" -ge 24 ] && echo 1 || echo 0)"; then
    SUBS=true
    echo "  OpenSubtitles account (free at opensubtitles.com; API key under Profile > API consumers). Leave empty = Whisper/translation only."
    OSKEY="$(ask '  API key' "$(ini opensubtitles api_key)")"
    OSUSER="$(ask '  Username' "$(ini opensubtitles username)")"
    read -rsp "  Password (empty = keep current): " OSPW; echo; OSPW="${OSPW:-$(ini opensubtitles password)}"
    DEF=gemma3:4b; [ "$MEM" -ge 24 ] && DEF=gemma4:12b
    MODEL="$(ask '  Translation model (Ollama)' "$(ini_tr ollama_model)")"; MODEL="${MODEL:-$DEF}"
  fi
  umask 077
  cat > worker.ini <<INI
[server]
url = $URL
worker_token = $TOKEN

[worker]
name = $NAME
media_root = $MEDIA
encoder = auto
max_height = $MAXH
subs = $SUBS
auto_update = true

[opensubtitles]
api_key = $OSKEY
username = $OSUSER
password = $OSPW

[whisper]
model = large-v3

[translation]
ollama_url = http://localhost:11434
ollama_model = ${MODEL:-gemma4:12b}
INI
  chmod 600 worker.ini; umask 022
fi
[ -d "$(ini worker media_root)" ] || echo "!! The media share is not connected right now; the worker waits until it is available."

# 5) subtitles: mlx-whisper (Apple GPU), ffsubsync, Ollama
if grep -q '^subs *= *true' worker.ini; then
  "$VPY" -m pip install -q srt charset-normalizer numpy
  if [ "$ARCH" = "arm64" ]; then "$VPY" -m pip install -q mlx-whisper; else "$VPY" -m pip install -q faster-whisper; fi
  "$VPY" -m pip install -q ffsubsync || echo "!! ffsubsync not installed (requires Xcode Command Line Tools: xcode-select --install)"
  command -v ollama >/dev/null || brew install ollama
  brew services start ollama >/dev/null 2>&1 || true
  M="$(ini_tr ollama_model)"; sleep 3; ollama pull "$M" || echo "!! Ollama model download failed; retry later with: ollama pull $M"
fi

# 6) start automatically (launchd) and stay awake while the worker is running.
# launchd itself would append to worker.log forever, so a small wrapper rotates the log (20 MB -> worker.log.1)
# each time the worker starts and then runs it with its output appended to worker.log.
cat > "$HOME_DIR/run-worker.sh" <<SH
#!/bin/sh
# MediaScan worker launcher (written by install.sh): rotate the log, then run the worker
LOG="$HOME_DIR/worker.log"
if [ -f "\$LOG" ] && [ "\$(stat -f %z "\$LOG" 2>/dev/null || echo 0)" -gt 20971520 ]; then mv -f "\$LOG" "\$LOG.1"; fi
exec /usr/bin/caffeinate -i "$VPY" "$HOME_DIR/worker.py" >>"\$LOG" 2>&1
SH
chmod 755 "$HOME_DIR/run-worker.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>/bin/sh</string><string>$HOME_DIR/run-worker.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$HOME_DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$BREW_PREFIX/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>PYTHONUNBUFFERED</key><string>1</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>ProcessType</key><string>Background</string>
</dict></plist>
PL
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo
echo "== Done. The worker starts now and automatically after every restart."
echo "   Logs:      tail -f \"$HOME_DIR/worker.log\"   (rotated to worker.log.1 at 20 MB when the worker starts)"
echo "   Stop:      launchctl bootout gui/$(id -u)/$LABEL"
echo "   Start:     launchctl bootstrap gui/$(id -u) \"$PLIST\""
echo "   Note: if macOS asks whether Python may access 'Network Volumes', choose Allow."
