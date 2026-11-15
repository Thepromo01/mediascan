# Troubleshooting and FAQ

## The dashboard does not open

- Check that the container is running: `docker ps`, or Container Manager › Container.
- Look at the container log (`docker logs mediascan`). Errors at startup show up there.
- Port 8780 might already be taken. Change the left side of `"8780:8780"` in the compose file,
  for example to `"8781:8780"`.

## Where is the setup code?

In the container log: `docker logs mediascan`, or Container Manager › Container › mediascan ›
Log. Look for `SETUP CODE`. A new code is printed at every start until a password is set.

## I forgot my password

1. Stop the container.
2. Delete the stored password:
   `sqlite3 data/mediascan.db "delete from auth where key='pw_hash'"`
   (or use any SQLite tool on `data/mediascan.db`).
3. Start the container again and choose a new password with the setup code from the log.

## I lost my phone with the authenticator app

Log in with one of your **recovery codes** and set up two-factor authentication again under
Settings › Account & security.

No recovery codes? Add `MFA_RESET: "1"` to the environment in the compose file, restart, log in
with your password, set up two-factor authentication again, and **then remove `MFA_RESET`** and
restart once more.

## The worker does not show up

- On the worker machine, check the log (see [install-worker.md](install-worker.md#status-and-logs)).
- *"server unreachable"*: the dashboard address in `worker.ini` must be the NAS's IP or name, not
  `localhost`, and port 8780 must be reachable from that machine.
- *"401"* or *"key"*: the worker key changed. Run the installer again and paste the key from
  Workers › Add worker.
- Make sure the media share is mounted. The worker waits until `media_root` exists.

## Encoding fails or every title lands in *No gain*

- *No gain* is normal for files that are already small or already HEVC. MediaScan only replaces a
  file when it saves at least the minimum saving at your quality target.
- Look at the reason in the *Failed* tab. Encoder errors usually mean the GPU driver is too old:
  update the NVIDIA, Intel or AMD driver, or set `encoder = x265` in `worker.ini`.
- Check that the worker has enough free disk space. It works on a local copy (`work/` folder).

## Subtitles fail

- *"OpenSubtitles daily limit reached"*: these jobs continue automatically the next day.
- *Translation fails*: check that Ollama is running (`ollama list`) and that the model in
  `worker.ini` is downloaded (`ollama pull <model>`).
- *Whisper is very slow*: without an NVIDIA GPU, Whisper runs on the CPU. Consider leaving
  subtitles to a machine with a GPU (Workers page › Subtitles switch).

## The quality of an encode looks worse than I'd like

Raise the **Minimum bitrate (1080p)** (for example from 5 to 6–7 Mbps) or pick the quality level
*Maximum*. Then open **Quarantine**, select the titles and choose **Re-encode (current rules)**.

## FAQ

**Does MediaScan delete anything?** Originals go to quarantine first and are deleted only after
the retention period, or when the quarantine exceeds its maximum size. Until then you can
restore them.

**Does it touch my audio?** No. Audio, subtitles and chapters are copied unchanged. Only the video
is re-encoded.

**What about HDR10 and HDR10+?** HDR metadata is kept. Dolby Vision files are skipped (see
[usage.md](usage.md#dolby-vision)).

**Can I run the worker on the NAS itself?** Technically yes (Linux worker), but NAS CPUs are far
too slow for this. A PC or Mac with a GPU is many times faster.

**Can several workers run at the same time?** Yes. Each worker takes its own jobs. You can also
split the work: one worker only encodes and another only makes subtitles.

**Is my data sent anywhere?** No. Everything runs on your own machines. The only outside services
are OpenSubtitles (if you add an account) and the download sources the installers use.
