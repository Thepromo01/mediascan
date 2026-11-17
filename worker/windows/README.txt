MediaScan worker for Windows 10/11

Minimum: 4+ cores, 8 GB RAM, 100 GB free (SSD), wired gigabit network.
GPU: NVIDIA GTX 1660 Super / RTX 2060 or newer, Intel Arc or 11th-gen+ iGPU, AMD RX 6000+.
Subtitles: NVIDIA with 8+ GB of video memory recommended.

1. Double-click Install-Worker.bat. A package downloaded from the dashboard already contains the
   dashboard address and the worker key; the installer finds the media share by itself and asks at
   most one question (subtitles yes/no).
   If the share cannot be found, it asks for the path and can map \\NAS\Media to a drive letter.
2. The MediaScan app opens, the orange icon appears in the system tray, and the worker shows up in the
   dashboard within a minute. Click the icon (or Start > MediaScan) to see what the worker is doing,
   read its log, stop it, or change its settings (Settings button).

Updates are automatic: the worker fetches new versions from the dashboard.
Everything lives in %LOCALAPPDATA%\MediaScan (log: logs\worker.log, settings: worker.ini).
