# Contributing

Thanks for helping! Bug reports, translations and pull requests are welcome. MediaScan is
licensed under the [PolyForm Noncommercial License](LICENSE.md). By contributing, you agree that
your contribution is shared under the same terms.

## Project layout

```
Dockerfile, docker-compose.yml, docker-entrypoint.sh   the server container
server/app/main.py          FastAPI server: API, auth, job planning, file swaps
server/app/scanner.py       library scan (ffprobe) and planning
server/app/db.py            SQLite schema, defaults and migrations
server/app/langs.py         the 38 subtitle languages (shared with the worker)
server/app/static/          the dashboard (plain JavaScript, no build step)
server/app/static/locales/  interface translations
worker/worker.py            the worker: encoding, VMAF, disc remux, self-update
worker/subs.py              subtitles: OpenSubtitles, sync, Whisper, Ollama translation
worker/windows/             Windows installer, manager.py (local app) and tray icon
worker/macos, worker/linux  installers
tools/build_packages.py     builds the worker packages (also runs inside the Docker build)
tools/check_locales.py      checks that every translation is complete
```

## Running the server locally

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -r server/requirements.txt
cd server
MEDIA_ROOT=/path/to/test-media DB_PATH=/tmp/ms.db uvicorn app.main:app --port 8780
```

## Translations

The dashboard is written in English. Every other language is a file in `server/app/static/locales/`:

```json
{
  "name": "Nederlands",
  "s":  { "<English text>": "<translation>" },
  "rx": [["<regex on English text>", "<flags>", "<template>"]],
  "w":  [["<regex on worker/server messages>", "<flags>", "<replacement>"]]
}
```

- `s` holds exact texts.
- `rx` holds texts with variable parts. In a template, `$1` inserts a captured group, `{t:$1}`
  inserts the group translated as text, and `{w:$1}` inserts it after the `w` rules.
- `w` holds substring replacements for status messages that come from workers and the server,
  applied in order.

`en.json` is the reference: it lists every key, with English values. To fix or improve a
translation, edit the file and run `python3 tools/check_locales.py`. It compares every locale with
`en.json` (keys, rule count and placeholders).

To add a new UI text, write it in English in `app.js`, add it to `en.json`, and add a translation
to every other locale. The check fails until every locale has it.

## Pull requests

- Keep changes focused, and describe what you tested.
- No new runtime dependencies without a good reason. The Windows manager and the worker use only
  the Python standard library, apart from the subtitle packages.
- Run the checks: `python -m py_compile server/app/*.py worker/*.py` and
  `python3 tools/check_locales.py`.
