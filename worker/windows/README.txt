MediaScan worker for Windows 10/11

Minimum: 4+ cores, 8 GB RAM, 100 GB free (SSD), wired gigabit network.
GPU: NVIDIA GTX 1660 Super / RTX 2060 or newer, Intel Arc or 11th-gen+ iGPU, AMD RX 6000+.
Subtitles: NVIDIA with 8+ GB of video memory recommended.

1. Map the media share to a drive letter, for example:   net use M: \\NAS\Media /persistent:yes
   (or enter \\NAS\Media directly when the installer asks)
2. Double-click Install-Worker.bat and answer the questions. Keep the dashboard open on
   Workers > Add worker: the dashboard address and the worker key are shown there.
3. The orange MediaScan icon appears in the system tray, and the worker shows up in the dashboard within a minute.
   Click the icon (or Start > MediaScan) to see what the worker is doing, read its log, or stop it.

Updates are automatic: the worker fetches new versions from the dashboard.
To change settings, run Install-Worker.bat again.
Everything lives in %LOCALAPPDATA%\MediaScan (log: logs\worker.log).
