# Using MediaScan

## The first run

After you log in for the first time, a short wizard walks you through six steps: language and
theme, your library (folders to skip, animation folders), subtitle languages, quality, Plex, and
adding a worker. You can change all of it later in **Settings**.

Encoding and subtitles start **off**. That way you can look around first, try a few titles, and
then switch both on with the switches at the bottom of the sidebar.

## The dashboard

| Page | What it is for |
|---|---|
| **Overview** | Savings so far, free space, what is running, recent activity |
| **In progress** | Every running job with its live progress and step |
| **Encoding** | The queue, plus tabs for *Done*, *No gain* and *Failed* |
| **Subtitles** | Files that are missing subtitles, with tabs for *In progress*, *Done*, *Nothing found* and *Failed* |
| **DVD & Blu-ray** | Disc folders (`VIDEO_TS`, `BDMV`) that can be turned into one `.mkv`; shown only when there are any |
| **Library** | Every video with its codec, resolution, bitrate and plan; *New* shows recent additions |
| **Compare** | Frames of the original and the new version side by side, with a Flip mode; tabs for made comparisons, films that can still be compared, and all replaced films |
| **Quarantine** | Replaced originals: restore them, or encode them again |
| **Workers** | Connected workers and their switches, and **Add worker** |
| **Activity** | The event log |
| **Info** | Your hardware with ratings (Low to Ultra), the tools in use with versions, and a plain-language knowledge base |
| **Settings** | Everything below |

Every list can be searched (press `/` to jump to the search box, `?` shows all keyboard shortcuts),
filtered by folder, resolution and codec, and sorted by clicking a column
header. Each page remembers its tab, search and filters while you move around. The pager lets you pick 50, 100 or 200 rows per page, and the ≡ button switches to compact
rows. On a phone, lists are shown as cards with sort chips above them.

### Choosing what to process

By default the queue runs from top to bottom, ordered by expected saving. You can steer it:

- **Process only these:** select titles in the Encoding (or Subtitles) queue and choose *Process
  only these*. Workers do your selection first. If encoding is switched off, they stop after it, which
  makes this the best way to try MediaScan on a few films.
- **Skip:** a title is never touched.
- **Skip folders** (Settings › Library): top-level folders MediaScan ignores completely, such as
  `Music` or `Downloads`.

## Quality

Each title goes through these steps:

1. **Plan.** From the scan, MediaScan estimates whether encoding is worth it. A file that is already
   efficient (HEVC/AV1 near the bitrate floor) or would save less than 100 MB is not queued.
2. **Search.** The worker encodes 8 short samples at a few quality settings and measures each with
   VMAF. It picks the most efficient setting that meets your target.
3. **Encode** the whole film with that setting. The video becomes HEVC 10-bit, and audio, subtitles
   and chapters are copied as they are.
4. **Verify.** VMAF is measured over the whole film. If it falls just short, the worker makes one
   more attempt at a higher quality. A title that still fails, or doesn't save enough, keeps its
   original and lands in *No gain*.
5. **Replace.** The server checks the new file again (duration, audio and subtitle tracks). It waits
   if the film is being watched in Plex, then swaps the file and moves the original to quarantine.

### Settings › Quality

| Setting | Default | Meaning |
|---|---|---|
| **Quality level** | High | VMAF targets for the average and the worst 5% of frames. *Standard* is 95 / 90, *High* is 96.5 / 93 and *Maximum* is 97.5 / 95. *High* is indistinguishable from the original for almost everyone. |
| **Minimum bitrate (1080p)** | 5 Mbps | A floor that protects grain and dark scenes. The other resolutions scale from it: SD ×0.3, 720p ×0.55, 4K ×2.5. HDR adds ×1.15 and animation folders use ×0.6. The floor is never higher than 90% of the original's bitrate. |
| **Minimum bitrate (4K)** | 0 (automatic) | An own floor for 4K, for example 10 Mbps, instead of 2.5 × the 1080p value. HDR and animation factors still apply. |
| **Minimum saving** | 10% | Smaller savings aren't worth replacing a file. |
| **CPU fallback for grainy films** | off | Some films (heavy film grain) cannot be shrunk by a GPU encoder without losing the quality target: it would need more bits than the source has. With this on, such a film is encoded once more on a worker's CPU with x265 and grain tuning, which keeps grain far more efficiently. A 2-hour 1080p film takes about 2 hours on 8 cores (*Medium*) or 2-3 times longer with *Slow*. Switch on **CPU encode** for a worker on the Workers page first. |
| **CPU preset** | Medium | *Medium*: about 2 hours for a 2-hour 1080p film on 8 cores. *Slow*: 2-3 times longer for roughly 5-10% smaller files. |
| **Extra floor (share of the source)** *(Advanced)* | 0% | An optional extra floor as a percentage of the original bitrate. |

When you change the minimum bitrate or the animation folders, the queue is re-planned straight away.

A single film can also be sent to the CPU encoder by hand: open it and choose **Retry on CPU (x265)**, or select
several films on the *No gain* tab. The CPU encode runs next to the GPU work (the GPU keeps going through the
queue); the worker gives x265 all cores but two and runs it at a lower priority, so the machine stays responsive.

### Animation

Animation compresses much better than live action. Folders listed under **Settings › Library ›
Animation folders** (default: `Anime`, `Anime Movies`, `Animation`) use a lower bitrate floor.

### Dolby Vision

Files with Dolby Vision are **not** re-encoded, because the Dolby Vision layer would be lost.
They show the plan *Dolby Vision* and are left alone. Disc folders are different: see below.

## Quarantine and going back

Every replaced original is kept in `<media>/_MediaScan/quarantine`.

- **Keep quarantine** (default 7 days) and **Quarantine maximum** (default 500 GB): the oldest
  originals are deleted first.
- **Restore original:** this puts the original back and deletes the new version.
- **Re-encode from original** / **Re-encode (current rules)**: this restores the original and
  queues it again at the top, with your current settings. Use it after you change the quality or
  the minimum bitrate.
- **Safety brake** (default 100 GB): if free space on the media share drops below this,
  encoding pauses automatically.

## Subtitles

Under **Settings › Subtitles**, choose the languages every file should have (38 to pick from). For
each missing language, the worker:

1. searches **OpenSubtitles** (needs a free account in `worker.ini`), picks the best match and
   checks that it lines up with the audio. If needed, it synchronises the timing (ffsubsync).
2. if nothing is found, makes the subtitle itself: **Whisper** transcribes the audio (on the GPU),
   and **Ollama** translates it into the other languages with a local language model.

The subtitles are saved next to the video as `Movie (2020).nl.srt`, so Plex picks them up
automatically. Subtitles embedded in the video count as present.

OpenSubtitles has a daily download limit. When it's reached, the remaining jobs wait and continue
the next day.

## DVD and Blu-ray folders

Ripped discs (`VIDEO_TS` or `BDMV` folders) show up on the **DVD & Blu-ray** page. Select them and choose
**Convert to MKV**. This finds the main title and remuxes it with all its video, audio, subtitles and chapters.
It doesn't re-encode, so nothing is lost. Progress is shown step by step.

Dolby Vision profile 7 Blu-rays (dual-layer) are converted to **profile 8.1** using `dovi_tool`.
Plex, Apple TV, Shield and most TVs play that natively.

The disc folder goes to quarantine afterwards, like any other original.

## Compare

For finished encodes whose original is still in quarantine, **Compare** grabs 4 frames from both
versions: the busiest moment (most motion and detail), the quietest (a dark or static scene) and the
quarter and three-quarter marks of the film. You can drag a wipe across them to compare, and the
measured VMAF scores are shown alongside. *Flip* shows the two versions in turn, which is the best way
to spot grain and fine detail; *Enlarge* shows a frame at full width. The page has three tabs:
*Comparisons* (made or being made), *Can be made* (replaced films whose original is still in
quarantine, with the days left) and *All replaced films*. Comparisons can be requested for several
films at once, and ← → step through the compared films.

Comparisons stay saved after the original is gone, until you delete them (they are about 1–2 MB each).
Two settings control this:

- **Automatic comparisons** (`cmp_auto`, default off): make a comparison for every film right after it
  has been replaced, so you can check the result without requesting it by hand.
- **Comparison size limit** (`cmp_max_mb`, default 500 MB): when the stored frames grow past this, the
  oldest comparisons are deleted first (the activity log notes how many). Set it to 0 for no limit.

## Plex

Under **Settings › Plex**, enter your Plex address (for example `http://192.168.1.10:32400`) and a
token. MediaScan then waits to replace a film while someone is watching it, and refreshes the
library after each change.
[Finding your Plex token](https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/).

## Language and theme

The dashboard is available in 38 languages. Pick one in **Settings › General**, on the login page,
or with the selector at the bottom of the sidebar. The theme can be *Auto* (follows your system),
*Light* or *Dark*.
