# Installing workers

A worker does the heavy lifting: encoding, VMAF measurements and subtitles. It runs on a PC or Mac
that can reach the dashboard and has the media share mounted. You can run several workers. Each
one picks the next job as soon as it's free.

Packages for every system are on the dashboard under **Workers › Add worker**. The same page shows
the **dashboard address** and the **worker key** the installer asks for.

## What the installers do

Each installer checks the machine, installs what is missing, asks a few questions, tests the
connection to the dashboard, and starts the worker as a background service. You can run it again
at any time to update or change settings.

| | Windows 10/11 | macOS 13+ (Apple Silicon) | Linux (x86-64) |
|---|---|---|---|
| Run | `Install-Worker.bat` | `bash install.sh` | `bash install.sh` |
| Installs | Python 3.12 and FFmpeg via winget, a Python venv | Homebrew packages (FFmpeg with libvmaf, Python) | static FFmpeg build (BtbN, checksum-verified), a Python venv |
| Runs as | scheduled task *MediaScan Manager* (at sign-in) | launchd agent | systemd user service `mediascan-worker` |
| Lives in | `%LOCALAPPDATA%\MediaScan` | `~/Library/Application Support/MediaScan` | `~/.local/share/mediascan` |
| Subtitles (optional) | faster-whisper (CUDA), ffsubsync, Ollama | faster-whisper, ffsubsync, Ollama | faster-whisper (CUDA if NVIDIA), ffsubsync, Ollama |

The subtitle part adds about 20 GB: the Whisper model plus a translation model for Ollama. The
installer picks a model that fits your video memory (`gemma4:12b` from 12 GB, smaller models below
that).

## Mount the media share first

The worker reads originals from the share and writes new files back to it, so it needs read and
write access.

- **Windows:** map a drive letter, for example `net use M: \\NAS\Media /persistent:yes`, or enter
  the UNC path `\\NAS\Media` when the installer asks.
- **macOS:** Finder › Go › Connect to Server › `smb://nas/Media`. To mount it at login, add it under
  System Settings › General › Login Items.
- **Linux:** add the share to `/etc/fstab` (CIFS or NFS). See `README-linux.md` in the package
  for an example.

Use a user with write access to the share. The worker only writes into `_MediaScan/staging`, and
the server does the actual swap after its own checks.

## Windows: the MediaScan app

After installing, the orange MediaScan icon appears in the system tray. Click it, or open **Start ›
MediaScan**, to open the MediaScan app:

- what the worker is doing right now, with progress
- **Start**, **Stop** and **Restart** for the worker
- the live worker log and the manager log
- **Open dashboard** and **Quit (stop everything)**

The icon color shows the state at a glance: **orange** means working, **green** means waiting for
work, **grey** means stopped, and **red** means a problem (crashing, or the server can't be reached).

The app runs locally on `127.0.0.1`. Only you can open it, through a key that changes at every
start.

If the worker crashes, it restarts automatically. After 6 crashes within an hour, automatic
restarts pause so it doesn't loop, and the app tells you so. **Stop** keeps the worker off, even
after a reboot, until you start it again. **Quit** stops everything until your next sign-in.

## Worker settings

The installer writes `worker.ini`. You can change it by running the installer again, or edit it
and restart the worker. To add settings without touching secrets, put them in `overrides.ini` in
the same folder; it wins over `worker.ini`.

```ini
[server]
url = http://192.168.1.10:8780       ; dashboard address
worker_token = …                     ; from Workers › Add worker

[worker]
name = living-room-pc                ; shown in the dashboard
media_root = M:\                     ; where the media share is mounted on this machine
encoder = auto                       ; auto | nvenc | qsv | amf | vaapi | videotoolbox | x265
max_height = 0                       ; 0 = any resolution, 1080 = skip 4K (for slower machines)
encode = true                        ; take encode jobs
subs = true                          ; take subtitle jobs
auto_update = true                   ; update automatically from the dashboard
cpu_encode = true                    ; offer a CPU (x265) slot next to the GPU one; the dashboard decides per worker
cpu_threads = 0                      ; threads for x265; 0 = all cores but two (the GPU job keeps those)

[opensubtitles]                      ; optional, free account at opensubtitles.com
api_key = …
username = …
password = …

[whisper]
model = large-v3

[translation]                        ; translation with Ollama
ollama_url = http://localhost:11434
ollama_model = gemma4:12b
```

The translation section used to be called `[vertaling]`; that name still works, so an existing `worker.ini`
needs no change.

You can also switch encoding and subtitles per worker, and set its maximum resolution, from the
dashboard (Workers page). Those switches take effect without a restart.

## Updates

Workers update themselves. When the server has a newer worker version, the worker finishes its
current job, downloads the new files (checked against SHA-256 hashes from the server) and
restarts. To turn this off, set `auto_update = false`.

## Status and logs

| | Status | Log |
|---|---|---|
| Windows | MediaScan app (tray icon) | `%LOCALAPPDATA%\MediaScan\logs\worker.log` (the manager's own log is `manager.log` next to it) |
| macOS | `launchctl print gui/$(id -u)/nl.mediascan.worker` | `~/Library/Application Support/MediaScan/worker.log` (rotated to `worker.log.1` at 20 MB) |
| Linux | `systemctl --user status mediascan-worker` | `journalctl --user -u mediascan-worker -f` |

## Uninstalling

- **Windows:** Quit MediaScan from the tray icon. Delete the scheduled task *MediaScan Manager* (Task
  Scheduler), the Start menu item *MediaScan* and the folder `%LOCALAPPDATA%\MediaScan`.
- **macOS:** `launchctl bootout gui/$(id -u)/nl.mediascan.worker`, then delete the plist in
  `~/Library/LaunchAgents` and the MediaScan folder.
- **Linux:** `systemctl --user disable --now mediascan-worker`, then delete
  `~/.config/systemd/user/mediascan-worker.service` and `~/.local/share/mediascan`.

Then remove the worker from the dashboard (Workers page, once it is offline).
