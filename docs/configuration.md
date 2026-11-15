# Configuration

Most settings live in the dashboard (**Settings**) and are stored in the database. A few things are
set with environment variables on the container.

## Environment variables (server)

Set these under `environment:` in `docker-compose.yml`.

| Variable | Default | Meaning |
|---|---|---|
| `PUID`, `PGID` | *(root)* | User and group the server runs as. Use the owner of your media folder. |
| `TZ` | `UTC` | Time zone for the log and charts, for example `Europe/Amsterdam`. |
| `ADMIN_PASSWORD` | *(empty)* | Sets the password up front instead of choosing it with the setup code. Only used while no password has been set yet. |
| `WORKER_TOKEN` | *(generated)* | A fixed worker key. When it's empty, the server creates one, and you can renew it in the dashboard. |
| `SESSION_IDLE_HOURS` | `72` | You have to log in again after this many hours of inactivity. |
| `MFA_RESET` | *(empty)* | Set to `1` and restart to turn off two-factor authentication once (see Troubleshooting). Remove it afterwards. |
| `PLEX_URL`, `PLEX_TOKEN` | *(empty)* | Plex connection, as an alternative to entering it in Settings. |
| `TRUST_PROXY` | `1` | Trust `X-Forwarded-Proto` from a reverse proxy on the host or a Docker network (for Secure cookies and HSTS). Set to `0` to ignore it. |
| `MEDIA_ROOT` | `/media` | Media folder inside the container. Normally you don't need to change it. |
| `DB_PATH` | `/data/mediascan.db` | Database location inside the container. |

## Dashboard settings

| Group | Setting | Default |
|---|---|---|
| General | Language, theme, encoding on/off, subtitles on/off | follows your browser, auto (follows the system), off, off |
| Quality | Quality level (VMAF average / worst 5%) | High (96.5 / 93) |
| | Minimum bitrate (1080p) | 5 Mbps |
| | Minimum bitrate (4K) | 0 = 2.5 × the 1080p value |
| | Minimum saving | 10% |
| | CPU fallback for grainy films | off |
| | CPU preset | Medium |
| | Extra floor (share of the source), under Advanced | 0% |
| Subtitles | Languages (up to 10 of the 38) | English |
| Library | Automatic scan (interval) | every 6 hours |
| | Skip folders | `#recycle, @eaDir, #snapshot, _MediaScan, Music, Downloads` |
| | Animation folders | `Anime, Anime Movies, Animation` |
| Storage & safety | Keep originals for | 7 days |
| | Quarantine size limit | 500 GB |
| | Safety brake (minimum free space) | 100 GB |
| Compare | Automatic comparisons (`cmp_auto`) | off |
| | Comparison size limit (`cmp_max_mb`) | 500 MB (0 = no limit) |
| Plex | Address and token | not connected |
| Account & security | Password, two-factor authentication, recovery codes, sessions | — |

Folder names in *Skip folders* and *Animation folders* are top-level folders of your media folder,
separated by commas.

## Worker settings

See [install-worker.md](install-worker.md#worker-settings) for `worker.ini`.
