# MediaScan worker on macOS

This guide sets up a MediaScan worker on a Mac. The worker encodes media from your share and can optionally create
subtitles. You control it from the dashboard.

## Requirements

- **Minimum:** Apple Silicon (M1 or newer), macOS 13 Ventura, 16 GB of memory, 150 GB free disk space,
  wired gigabit network, Homebrew.
- **Recommended:** M2 Pro / M3 or newer, 24–32 GB of memory if it also creates subtitles, 250 GB free for 4K.

Good to know:

- Encoding uses Apple VideoToolbox (HEVC 10-bit). The VMAF check guards quality just like on the other workers,
  although files end up slightly larger than with NVENC or x265.
- Subtitles use mlx-whisper on the Apple GPU and Ollama for translation.
- Intel Mac: works, but only with the CPU (x265). Set it to "subtitles only" or "up to 720p" in the dashboard.
- The Mac stays awake while the worker is running (caffeinate). The display may turn off.

## 1. Install Homebrew

Install Homebrew from https://brew.sh if you do not have it yet. The installer uses it for ffmpeg, Python 3.12
and Ollama.

## 2. Connect the media share

In Finder choose **Go > Connect to Server** and enter `smb://<nas>/Media`. Tick "Remember this password in my
keychain". Then add the share under **System Settings > General > Login Items** so it reconnects after a restart.

The share normally appears under `/Volumes/Media`. If it is not connected when the worker starts, the worker simply
waits until it becomes available.

## 3. Run the installer

1. In the dashboard, go to **Workers > Add worker**. Download the macOS package there and copy the worker key.
2. Extract the package, open Terminal in that folder and run:

       bash install.sh

3. Answer the questions: the dashboard address (e.g. `http://192.168.1.10:8780`), the worker key, the path to the
   media share (default `/Volumes/Media`), a name for this worker, the maximum resolution and whether it should also
   create subtitles.

The installer installs ffmpeg with libvmaf, sets up a Python environment, tests the connection to the dashboard
and registers a launchd agent (`nl.mediascan.worker`) that starts the worker now and after every restart.
If you enable subtitles it also installs mlx-whisper, ffsubsync and Ollama and downloads the translation model.
ffsubsync needs the Xcode Command Line Tools (`xcode-select --install`).

If macOS asks whether Python may access "Network Volumes", choose **Allow**.

Within a minute the worker appears in the dashboard under **Workers**. Choose there what this Mac does
(encoding, subtitles, maximum resolution).

## 4. Check status and logs

    tail -f ~/Library/Application\ Support/MediaScan/worker.log          # follow the log

The log is rotated to `worker.log.1` when it is over 20 MB at the moment the worker starts.
    launchctl print gui/$(id -u)/nl.mediascan.worker | head               # is it loaded/running?

Stop and start the worker:

    launchctl bootout gui/$(id -u)/nl.mediascan.worker
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/nl.mediascan.worker.plist

## 5. Updating

The worker fetches new versions from the dashboard by itself. To turn that off, set `auto_update = false` in
`worker.ini`.

You can also run `bash install.sh` again from a newer package at any time. It keeps your existing settings unless
you choose to enter them again.

## 6. Changing settings

Settings are stored in `~/Library/Application Support/MediaScan/worker.ini` (readable only by you).

- Run `bash install.sh` again and answer **y** when asked "Settings already exist. Enter them again?", or
- edit `worker.ini` directly and then stop and start the worker with the two `launchctl` commands above.

Note: keep the section and key names in `worker.ini` exactly as they are (for example `[translation]` and
`ollama_model`); the worker reads them by those names. The old section name `[vertaling]` still works.

## 7. Uninstalling

    launchctl bootout gui/$(id -u)/nl.mediascan.worker
    rm ~/Library/LaunchAgents/nl.mediascan.worker.plist
    rm -rf ~/Library/Application\ Support/MediaScan

If you no longer need Ollama: `brew services stop ollama` and `brew uninstall ollama`.
