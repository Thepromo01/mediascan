# Installing the server

The server is a single Docker container. It runs the dashboard and the API, scans your library,
hands out jobs to workers, and checks and swaps files. It needs very little: 1 CPU core and 1 GB RAM.

## Before you start

- **A media folder the container can write to.** MediaScan replaces files and moves originals to
  quarantine, so it needs write access to the folder that holds your movies and series.
- **A user that owns that folder.** The container runs as that user (`PUID`/`PGID`) instead of
  root. On Synology, connect over SSH and run `id <your-user>`. You'll usually get `uid=1026`
  and `gid=100` (users).
- **A folder for MediaScan itself**, outside your shared media folders. For example
  `/volume1/docker/mediascan`. The database (`data/`) is stored there, so keep it readable by
  administrators only.

MediaScan creates one working folder inside your media folder: `_MediaScan/`. It holds `staging`
(new files waiting for the final check) and `quarantine` (replaced originals). Library scans
skip this folder. Installations from before 1.0 have a `quarantaine` folder; it is renamed to
`quarantine` automatically at the first start, together with the stored paths.

## Docker Compose (any Linux host, TrueNAS, Portainer)

1. Save [`docker-compose.yml`](../docker-compose.yml) in your MediaScan folder.
2. Edit the marked lines:
   ```yaml
   volumes:
     - /volume1/Media:/media   # <- your movies and series
   environment:
     PUID: "1026"              # <- owner of that folder
     PGID: "100"
     TZ: "Europe/Amsterdam"
   ```
3. Start it: `docker compose up -d`
4. Read the setup code: `docker logs mediascan` (look for `SETUP CODE`).
5. Open `http://<server-ip>:8780`, enter the setup code, choose a password (at least 10
   characters), and scan the QR code with an authenticator app (Google Authenticator,
   Microsoft Authenticator, 1Password, Bitwarden, Aegis and so on). **Save the recovery codes.**
6. Follow the setup wizard.

In Portainer, use *Stacks › Add stack* and paste the compose file.

## Synology (DSM 7.2+, Container Manager)

1. In **File Station**, create `docker/mediascan` and upload `docker-compose.yml` to it.
2. Edit the media path and `PUID`/`PGID` in that file (Text Editor package, or edit it before
   uploading). Synology paths start with `/volume1/…`.
3. **Container Manager › Project › Create**
   - Project name: `mediascan`
   - Path: `docker/mediascan`
   - Source: *Use existing docker-compose.yml*
   - Click through and choose **Done**. The image is downloaded and the container starts.
4. **Container Manager › Container › mediascan › Details › Log** shows the setup code.
5. Open `http://<nas-ip>:8780` and continue with step 5 above.

If the container cannot write to your media folder, check the permissions of that shared folder
for the `PUID` user in **Control Panel › Shared Folder › Edit › Permissions**.

## Unraid

Use the *Docker Compose Manager* plugin with the compose file above, or add a container by hand:

| Field | Value |
|---|---|
| Repository | `ghcr.io/thepromo01/mediascan:latest` |
| Port | `8780` → `8780` |
| Path `/media` | your media share, for example `/mnt/user/Media` (read/write) |
| Path `/data` | `/mnt/user/appdata/mediascan` |
| Variables | `PUID=99`, `PGID=100`, `TZ=Europe/Amsterdam` |
| Extra parameters | `--security-opt no-new-privileges:true` |

## Building the image yourself

If you'd rather not use the published image, clone the repository and build it:

```sh
git clone https://github.com/Thepromo01/mediascan.git
cd mediascan
# in docker-compose.yml: comment out the "image:" line and uncomment "build: ."
docker compose up -d --build
```

The worker install packages are built into the image during the build, so the dashboard always
serves packages that match the server version.

## HTTPS with a reverse proxy (optional)

MediaScan runs on your own network, so plain HTTP is fine. If you put it behind a reverse proxy
with a certificate, cookies get the `Secure` flag automatically and HSTS is switched on.

- **Synology:** Control Panel › Login Portal › Advanced › Reverse Proxy › Create. Set the source
  to `https://mediascan.your.lan:443` and the destination to `http://localhost:8780`.
- **Nginx Proxy Manager, Caddy or Traefik:** forward to `http://<container>:8780` and pass
  `X-Forwarded-Proto`.

The container trusts `X-Forwarded-*` headers only from the host itself and from Docker networks
(`127.0.0.1` and `172.16.0.0/12`). Set `TRUST_PROXY=0` to ignore them completely.

Workers should then use the `https://` address too.

## Updating

- **Published image:** `docker compose pull && docker compose up -d`. In Synology, open
  Container Manager › Project › mediascan › Action › Build, or use the Images page to update.
- **Self-built image:** `git pull && docker compose up -d --build`.

The database is migrated automatically. Workers notice the new version, finish their current
job, update themselves and restart.

## Backup

Back up the `data/` folder (the database with your settings, history and the list of
quarantined originals). The quarantine itself lives in `<media>/_MediaScan/quarantine`.

## Uninstalling

1. In the dashboard, restore any originals you want back (Quarantine page).
2. Stop and remove the container: `docker compose down`.
3. Delete the MediaScan folder and `<media>/_MediaScan`.
