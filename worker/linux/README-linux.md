# MediaScan worker on Linux

This guide sets up a MediaScan worker on a Linux machine (Ubuntu, Debian, Fedora, Arch, Bazzite and similar).
The worker encodes media from your share and can optionally create subtitles. You control it from the dashboard.

## Requirements

- **Minimum:** x86-64, 4+ CPU cores, 8 GB of memory, 100 GB free disk space (SSD), wired gigabit network, Python 3.10+.
- **GPU:** NVIDIA (NVENC), Intel Arc/iGPU 11th gen+ (QSV/VA-API) or AMD RX 6000+ (VA-API).
  Without a GPU the worker uses x265 on the CPU (slow).
- **Subtitles:** an NVIDIA GPU with 8+ GB of video memory is recommended (Whisper + translation model).

On Debian/Ubuntu, make sure `python3-venv` is installed: `sudo apt install python3-venv`.

## 1. Mount the media share

The worker needs access to the same media library as the dashboard. Mount the share permanently, for example
with this line in `/etc/fstab`:

    //NAS/Media  /mnt/media  cifs  credentials=/home/YOUR_USER/.smbcred,uid=1000,gid=1000,iocharset=utf8,_netdev  0 0

Put the lines `username=...` and `password=...` in `~/.smbcred` and give that file permissions 600
(`chmod 600 ~/.smbcred`). Then run `sudo mount -a`.

If the share is not mounted yet when the worker starts, it simply waits until it becomes available.

## 2. Run the installer

1. In the dashboard, go to **Workers > Add worker**. Download the Linux package there and copy the worker key.
2. Extract the package, open a terminal in that folder and run:

       bash install.sh

3. Answer the questions: the dashboard address (e.g. `http://192.168.1.10:8780`), the worker key, the path to the
   media share (e.g. `/mnt/media`), a name for this worker, the maximum resolution and whether it should also
   create subtitles.

The installer downloads ffmpeg (with VMAF), tests the connection to the dashboard and sets up a systemd user
service called `mediascan-worker`. If you enable subtitles it also installs faster-whisper, ffsubsync and Ollama
(this asks for your sudo password) and downloads the translation model.

If your machine has an Intel or AMD GPU, your user must be in the `render` group. The installer tells you when that
is not the case; fix it with `sudo usermod -aG render,video $USER` and log in again.

Within a minute the worker appears in the dashboard under **Workers**. That is where you choose what this machine does.

## 3. Check status and logs

    systemctl --user status mediascan-worker     # is it running?
    journalctl --user -u mediascan-worker -f     # follow the log
    systemctl --user restart mediascan-worker    # restart

The installer enables lingering (`loginctl enable-linger`), so the service keeps running when you are logged out
and starts after a reboot.

## 4. Updating

The worker fetches new versions from the dashboard by itself. To turn that off, set `auto_update = false` in
`worker.ini`.

You can also run `bash install.sh` again from a newer package at any time. It keeps your existing settings unless
you choose to enter them again.

## 5. Changing settings

Settings are stored in `~/.local/share/mediascan/worker.ini` (readable only by you).

- Run `bash install.sh` again and answer **y** when asked "Settings already exist. Enter them again?", or
- edit `worker.ini` directly and then run `systemctl --user restart mediascan-worker`.

Note: keep the section and key names in `worker.ini` exactly as they are (for example `[translation]` and
`ollama_model`); the worker reads them by those names. The old section name `[vertaling]` still works.

## 6. Uninstalling

    systemctl --user disable --now mediascan-worker
    rm ~/.config/systemd/user/mediascan-worker.service
    systemctl --user daemon-reload
    rm -rf ~/.local/share/mediascan

If the installer added Ollama and you no longer need it, remove it as described at https://ollama.com/download.
