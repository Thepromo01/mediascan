# Changelog

## 1.0.1

**Simpler worker setup**
- A package downloaded from the dashboard (Workers › Add worker) already contains the dashboard
  address and the worker key, so the installer has nothing to ask about the connection.
- Windows installer: finds the media share by itself (the dashboard tells it which folders the
  library starts with), can map a `\\NAS\Media` path to a drive letter, keeps existing settings,
  asks at most one question (subtitles yes/no), works when started from the MediaScan folder itself,
  and opens the MediaScan app when done. Linux and macOS installers use the shipped address and key
  as defaults.
- MediaScan app (Windows): a **Settings** form for `worker.ini` with a connection test, a share
  finder and drive mapping, the maximum resolution, and the subtitle settings (OpenSubtitles account,
  Whisper model, Ollama address and translation model, with the installed Ollama models listed).
  Saving restarts the worker. The app opens in your default browser as its own window (Brave,
  Chrome, Edge and other Chromium browsers), and opens on the Settings form while the setup is not
  finished.
- The app removes files of older versions from the MediaScan folder, and the worker clears temp
  folders a crash left behind.

**Dashboard**
- The queue pages (In progress, Encoding › Queue) load in milliseconds on large libraries; the queue
  position was computed with a window function that SQLite re-evaluated per row.
- Compare detail: previous/next as one compact control in the page header.
- README: screenshots side by side.

**Build**
- GitHub Actions on Node 24 (checkout v7, setup-python v7, setup-node v7, Docker actions v4/v6/v7).

## 1.0.0 — first public release

**Upgrading from a pre-1.0 install**
- The compose file no longer reads `.env`. Move any values you had there (`MEDIA_PATH`, `PUID`,
  `PGID`, `WORKER_TOKEN`, `ADMIN_PASSWORD`, `PLEX_*`) into `docker-compose.yml`: the media path
  under `volumes:` and the rest under `environment:`. A changed `WORKER_TOKEN` would lock out your
  workers.
- Windows workers: run `Install-Worker.bat` from the new package once. It replaces the old
  PowerShell service with the new MediaScan app. Later updates are automatic again.

**Dashboard**
- Info page: hardware per worker with ratings (Low to Ultra), the server, tools in use with versions,
  and a plain-language knowledge base (English and Dutch).
- Every list sorts by column and filters by folder, resolution and codec; the activity log is paged.
- Phone layout with a bottom tab bar and a slide-up menu; confirmations in the dashboard's own style;
  a connection bar while the server is away.
- Strict Content Security Policy (no inline scripts); static files are versioned by content.
- New logo, sharp at every size, also in the Windows tray and taskbar.
- Compare: tabs (Comparisons, Can be made, All replaced films), clear status chips with the days
  left in quarantine, a thumbnail per comparison, bulk "Make comparison", previous/next with the arrow
  keys, a Flip mode and an Enlarge view for the frames, and a note on which scene each frame shows.
- In progress explains why nothing runs (no worker, paused, empty queue) with the matching button;
  running jobs show the step and an estimated time left; waiting for Plex is its own status.
- Buttons show a spinner while an action runs; toasts are green or red; Skip can be undone; every
  status chip explains itself on hover; queued films show their position and expected start.
- Failed tab: a plain-language explanation and next step for every known error.
- File drawer: actions in a fixed bar, the chosen quality step marked, why the planner chose this
  plan, and a timeline (queued, started, finished, replaced).
- Activity: readable messages with the film name, filters for warnings and errors, search, repeated
  lines grouped. Quarantine shows when each original expires. Workers show their capabilities and a
  pending update. Settings warn before leaving with unsaved changes and check number ranges inline.
- Keyboard: / searches, ? lists the shortcuts; each page remembers its tab, search and filters.
- Overview with two headline numbers (Saved, Time left) and a compact strip for the rest; a pause banner
  with Resume; folder rows open the library filtered on that folder.
- Every list has a helpful empty state, a loading indicator when the server is slow, rows per page
  (50/100/200), a compact mode, and Quarantine and Compare are paged and sorted on the server.
- On a phone, lists are cards (name on top, details below) with sort chips instead of sideways tables.
- Settings start with a Processing card (pause switches, workers); Plex shows a status chip with one
  Connect/Update button. The setup wizard has clickable steps, a live scan line and Plex validation.
- Compare: previous/next between films; dates show the year when it differs; relative times in your language.
- Compare has tabs (Made, Possible, All) with a thumbnail per film and the date the held original expires,
  comparisons can be requested in bulk, and they can be made automatically after every replace with a
  size cap on the stored frames (Settings).
- The activity log names the film instead of a job number, and can be filtered by level (warnings, errors)
  and searched.
- Queued jobs show their position in the queue and how much has to be processed before them; the
  Overview reports the processing speed.
- A file's page explains why the planner chose its plan (bitrate against the floor, estimated saving,
  Dolby Vision, disc folder).
- Workers show an "update available" chip when they run older files than the server ships.
- Light theme: darker orange and grey text for readable contrast.
- Bulk actions on "all results" respect the folder, resolution and codec filters.
- Keyboard and screen-reader support: switches, sortable headers, the file drawer, the pager and
  notifications are reachable and announced; a destructive confirmation no longer runs on a stray Enter.
- One name per action (Retry, Re-encode from original) and clearer labels (Time left, Queued,
  Keep originals for, Quarantine size limit); the CPU-encoding texts are translated in every language.
- A new, simpler layout: *Overview* and *In progress*, then *Processing* (Encoding, Subtitles,
  DVD & Blu-ray), *Library* (Library, Compare, Quarantine) and *System* (Workers, Activity,
  Settings).
- A setup wizard for the first run.
- 38 interface languages, including right-to-left layouts for Arabic, Hebrew, Persian and Urdu.
- Settings regrouped into General, Quality, Subtitles, Library, Storage & safety, Plex and
  Account & security.
- *Add worker* moved to the Workers page.
- Quarantine can re-encode originals with the current rules.

**Quality**
- Whole-film VMAF checks pair frames by number and re-measure a result that does not fit the
  samples, so a measuring error is never reported as a quality verdict.
- A CPU fallback for grainy films: a second worker slot runs x265 with grain tuning next to the GPU
  work (by hand per film, in bulk, or automatically), with a fixed thread pool and lower priority.
- The planner skips files that cannot reach the minimum saving, and the search stops as soon as a
  film would need more bits than its source.
- An absolute minimum bitrate per resolution (default 5 Mbps for 1080p, scaled for SD, 720p, 4K,
  HDR and animation) replaces the relative floor.
- Smarter retries: a title is encoded again only if it fell just short of the target, and only
  while that still helps.

**Workers**
- Windows: a new MediaScan app (local, in the dashboard's style) shows what the worker is doing,
  its log, and start/stop/restart controls. It replaces the old PowerShell service and status
  window.
- Workers report their status locally, and the Windows manager is updated together with the worker.
- A finished job is reported until the server answers, so hours of encoding are never lost while the
  server is away; cancelling a subtitle job from the dashboard stops it at the next step.
- A media root written as a bare drive letter (`M:`) works; an interrupted copy leaves no half file.
- Translation copes with a shaky Ollama: a cut-off answer is kept up to the last complete line and only the
  rest is asked again, server errors are waited out instead of splitting the block ever smaller, and a
  missing model or a stopped Ollama fails at once with a clear message.
- English installers for Windows, macOS and Linux.
- `worker.ini` section `[translation]` (the old name `[vertaling]` still works); the Windows scheduled task is
  called 'MediaScan Manager', its log is `manager.log` (an old `beheer.log` is carried over) and the installer
  script is `install-manager.ps1`; macOS log rotation.
- Comparison frames are chosen by content: the busiest and the quietest moment of the film (measured from the
  video packet sizes) plus the quarter and three-quarter marks, each shot labelled with its reason.
- Workers report the checksums of their files, so the dashboard can tell when one is out of date.

**Server and deployment**
- One simple `docker-compose.yml` using the published image `ghcr.io/thepromo01/mediascan`, with
  multi-arch images (amd64 and arm64).
- English documentation.
- Clean installs start correctly (the first start was missing a column), the tool's own `_MediaScan`
  folder is never scanned, and worker requests run off the event loop so a long scan cannot freeze
  the dashboard. Replaced and restored files get their real codec and bitrate stored.
- The quarantine folder is `_MediaScan/quarantine` (an existing `quarantaine` folder is renamed at the
  first start); disc folders have a stable id; dependencies updated.
- Settings `cmp_auto` and `cmp_max_mb`; the server queues the comparison itself after a replace and cleans up
  the oldest ones when the cap is exceeded. Quarantine rows carry their expiry time.
- Licence: PolyForm Noncommercial 1.0.0.
