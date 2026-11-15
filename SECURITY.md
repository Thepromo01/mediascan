# Security

## Reporting a vulnerability

Please **do not open a public issue** for security problems. Use GitHub's private
*Report a vulnerability* button on the **Security** tab of this repository, and include steps to
reproduce. You can expect a first reply within a week.

## Security model

MediaScan is designed to run on a **trusted home network**. It is not designed to be exposed to
the internet. If you want remote access, use a VPN (Tailscale, WireGuard, or your NAS's VPN server).

**Dashboard**
- Logging in needs a password (stored as a scrypt hash) plus a TOTP code. Recovery codes are stored
  hashed as well.
- Login attempts are limited: after 8 failures from one address there is a 15-minute pause, with a
  global limit on top.
- Sessions are HttpOnly, SameSite=Strict cookies. They get the `Secure` flag automatically behind
  HTTPS, and they expire after 72 hours of inactivity.
- Every state-changing request needs a custom header and a JSON body, which blocks cross-site
  request forgery.
- A Content Security Policy allows no external scripts, styles, fonts or connections. Other headers
  include `X-Frame-Options: DENY`, `nosniff` and HSTS when served over HTTPS.
- The first-run setup needs a one-time code that is only printed in the container log.

**Workers**
- Workers authenticate with a bearer key and can only use the worker API (fetch jobs, report
  progress and results). They cannot read settings or log in to the dashboard.
- Paths from the server are confined to the media share, and data from workers is sanitised and
  size-limited by the server.
- Workers never overwrite originals. They write to `_MediaScan/staging`, and the server re-checks
  the file before swapping it.
- Self-updates are verified against SHA-256 hashes the server provides. Downloaded tools are
  verified: dovi_tool and MKVToolNix against pinned SHA-256 checksums, the Linux FFmpeg build against
  the publisher's checksum file, and Windows packages by winget.
- The Windows app listens on `127.0.0.1` only. It checks the `Host` header (against DNS
  rebinding), needs a random key that changes at every start, and requires a custom header on every
  action.

**Container**
- The container runs as `PUID`/`PGID` (not root) with `no-new-privileges` and a minimal set of
  capabilities.
- The base image and all Python dependencies are pinned to exact versions. Rebuild regularly to
  pick up security updates (see below).

## Keeping it up to date

- The server image is rebuilt for every release. Run `docker compose pull && docker compose up -d`
  to update.
- Workers update themselves from the server.
- For maintainers: before a release, check `server/requirements.txt` and the base image in the
  `Dockerfile` for new versions and known CVEs (for example with `pip-audit -r
  server/requirements.txt` and `docker scout cves`).
