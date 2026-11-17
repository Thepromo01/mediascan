#!/usr/bin/env bash
# MediaScan worker - installer for Linux (Ubuntu/Debian/Fedora/Arch/Bazzite, x86-64)
# Usage: extract the package and run this inside that folder:  bash install.sh
# Safe to run again at any time (to update or change settings).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
HOME_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/mediascan"
mkdir -p "$HOME_DIR"
cd "$HOME_DIR"
ask() { local a; read -rp "$1${2:+ [$2]}: " a; echo "${a:-$2}"; }
yes() { local a; read -rp "$1 ($([ "$2" = 1 ] && echo Y/n || echo y/N)): " a; [ -z "$a" ] && [ "$2" = 1 ] && return 0; [[ "$a" =~ ^[yYjJ] ]]; }
ini() { [ -f worker.ini ] && awk -F' *= *' -v s="[$1]" -v k="$2" '$0==s{f=1;next} /^\[/{f=0} f&&$1==k{print $2;exit}' worker.ini || true; }
ini_tr() { local v; v="$(ini translation "$1")"; [ -n "$v" ] || v="$(ini vertaling "$1")"; echo "$v"; }   # [vertaling] = old name of [translation]
# a package downloaded from the dashboard ships a worker.ini with the address and key already filled in
pre() { [ -f "$SRC/worker.ini" ] && awk -F' *= *' -v s="[server]" -v k="$1" '$0==s{f=1;next} /^\[/{f=0} f&&$1==k{print $2;exit}' "$SRC/worker.ini" || true; }
echo "== Installing MediaScan worker in $HOME_DIR"
[ "$(uname -m)" = "x86_64" ] || { echo "!! Only x86-64 is supported."; exit 1; }
command -v python3 >/dev/null || { echo "!! python3 is missing - install it with your package manager"; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' || { echo "!! Python 3.10 or newer is required"; exit 1; }
python3 -m venv --help >/dev/null 2>&1 || { echo "!! python3-venv is missing (Debian/Ubuntu: sudo apt install python3-venv)"; exit 1; }

# 1) ffmpeg with libvmaf (static build from BtbN, verified against their checksums)
if [ ! -x "$HOME_DIR/ffmpeg/bin/ffmpeg" ]; then
  echo "== Downloading ffmpeg (BtbN, with libvmaf and hardware encoders)"
  REL=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest
  F=ffmpeg-master-latest-linux64-gpl.tar.xz
  curl -L --fail -o "$F" "$REL/$F"
  curl -L --fail -o checksums.sha256 "$REL/checksums.sha256"
  grep " $F\$" checksums.sha256 | sha256sum -c - || { echo "!! ffmpeg download failed verification (checksum)"; rm -f "$F"; exit 1; }
  mkdir -p ffmpeg && tar -xJf "$F" -C ffmpeg --strip-components=1 && rm -f "$F" checksums.sha256
fi
export PATH="$HOME_DIR/ffmpeg/bin:$PATH"
ffmpeg -hide_banner -filters 2>/dev/null | grep -q libvmaf && echo "   libvmaf: ok"

# 2) program files + Python environment
cp "$SRC/worker.py" "$SRC/subs.py" "$SRC/langs.py" "$HOME_DIR/"
[ -x venv/bin/python ] || python3 -m venv venv
VPY="$HOME_DIR/venv/bin/python"
"$VPY" -m pip install -q --upgrade pip

# 3) settings
NVIDIA=0; command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 && NVIDIA=1
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
  MEDIA="$(ask 'Path to the media share on this machine (e.g. /mnt/media)' "$(ini worker media_root)")"
  [ -d "$MEDIA" ] || echo "!! $MEDIA does not exist right now - mount the share (see README-linux.md); the worker waits until it is available."
  NAME="$(ask 'Name of this worker' "$(ini worker name || true)")"; NAME="${NAME:-$(hostname)}"
  MAXH="$(ask 'Maximum resolution (0 = everything, 1080 = up to Full HD)' "$(ini worker max_height)")"; MAXH="${MAXH:-0}"
  SUBS=false; OSKEY=""; OSUSER=""; OSPW=""; MODEL="gemma4:12b"
  if yes "Also create subtitles? (recommended: NVIDIA GPU with 8+ GB)" "$NVIDIA"; then
    SUBS=true
    echo "  OpenSubtitles account (free at opensubtitles.com; API key under Profile > API consumers). Leave empty = Whisper/translation only."
    OSKEY="$(ask '  API key' "$(ini opensubtitles api_key)")"
    OSUSER="$(ask '  Username' "$(ini opensubtitles username)")"
    read -rsp "  Password (empty = keep current): " OSPW; echo; OSPW="${OSPW:-$(ini opensubtitles password)}"
    VRAM=0; [ "$NVIDIA" = 1 ] && VRAM="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1 | tr -d ' ')"
    DEF=gemma3:1b; [ "${VRAM:-0}" -ge 7000 ] && DEF=gemma3:4b; [ "${VRAM:-0}" -ge 12000 ] && DEF=gemma4:12b
    MODEL="$(ask '  Translation model (Ollama)' "$(ini_tr ollama_model || true)")"; MODEL="${MODEL:-$DEF}"
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
ollama_model = $MODEL
INI
  chmod 600 worker.ini; umask 022
fi

# 4) subtitles: faster-whisper (CUDA if there is an NVIDIA GPU), ffsubsync, Ollama
if grep -q '^subs *= *true' worker.ini; then
  PK="faster-whisper ffsubsync srt charset-normalizer numpy"
  [ "$NVIDIA" = 1 ] && PK="$PK nvidia-cublas-cu12 nvidia-cudnn-cu12==9.*"
  "$VPY" -m pip install -q $PK || echo "!! Installing Python packages failed"
  if ! command -v ollama >/dev/null; then
    echo "== Installing Ollama (asks for your password for sudo)"
    curl -fsSL https://ollama.com/install.sh | sh || echo "!! Installing Ollama failed - see https://ollama.com/download"
  fi
  M="$(ini_tr ollama_model)"; ollama pull "$M" || echo "!! Model download failed; retry later with: ollama pull $M"
fi

# make the CUDA libraries from the Python environment findable for Whisper
LDP="$("$VPY" - <<'PY' 2>/dev/null || true
import os, glob, sys
print(':'.join(sorted(glob.glob(os.path.join(sys.prefix, 'lib', 'python3*', 'site-packages', 'nvidia', '*', 'lib')))))
PY
)"

# 5) GPU access (VA-API/QSV)
if [ -e /dev/dri/renderD128 ] && ! id -nG | grep -qwE "render|video"; then
  echo "!! Your user is not in the 'render' group. Run:  sudo usermod -aG render,video $USER  and log in again."
fi

# 6) systemd user service (starts automatically, restarts after a crash or update)
mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/mediascan-worker.service" <<UNIT
[Unit]
Description=MediaScan worker

[Service]
Environment=PATH=$HOME_DIR/ffmpeg/bin:$HOME_DIR/venv/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=LD_LIBRARY_PATH=$LDP
WorkingDirectory=$HOME_DIR
ExecStart=$VPY $HOME_DIR/worker.py
Restart=always
RestartSec=20

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
systemctl --user enable mediascan-worker.service
systemctl --user restart mediascan-worker.service
loginctl enable-linger "$USER" 2>/dev/null || true
echo
echo "== Done. Status:  systemctl --user status mediascan-worker"
echo "   Logs:           journalctl --user -u mediascan-worker -f"
