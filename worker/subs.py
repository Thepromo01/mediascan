"""
Subtitle pipeline of the MediaScan worker (loaded by worker.py).
- Searches OpenSubtitles for missing subtitles (first by file hash, then by title)
- Checks the timing (against an embedded English subtitle or the speech) and syncs with ffsubsync
- Fallback: Whisper (English from the audio, or translated from another language) and translation with a local Ollama model
- Writes <video>.<lang>.srt (UTF-8) next to the video. Never modifies the video itself.
"""
import os, sys, re, glob, json, time, struct, shutil, subprocess, tempfile, wave, datetime
import urllib.request, urllib.error, urllib.parse

IS_WIN = os.name == 'nt'
IS_MAC = sys.platform == 'darwin'
if os.environ.get('MEDIASCAN_HOME'):
    LOCAL = os.environ['MEDIASCAN_HOME']
elif IS_WIN:
    LOCAL = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'MediaScan')
elif IS_MAC:
    LOCAL = os.path.expanduser('~/Library/Application Support/MediaScan')
else:
    LOCAL = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')), 'mediascan')
if IS_WIN:
    for d in glob.glob(os.path.join(sys.prefix, 'Lib', 'site-packages', 'nvidia', '*', 'bin')):
        try: os.add_dll_directory(d); os.environ['PATH'] = d + os.pathsep + os.environ['PATH']
        except OSError: pass

import srt
from charset_normalizer import from_bytes
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from langs import EMB, SIDE, NAME_EN as LANG_NAME

DEFAULT_MODEL = 'gemma4:12b'           # translation model when worker.ini sets none (ollama pull gemma4:12b)


def _tr(cfg, key, fallback=None):
    """Reads worker.ini [translation]; [vertaling] is the old (Dutch) name of that section and still works."""
    return cfg.get('translation', key, fallback=cfg.get('vertaling', key, fallback=fallback))
TEXT_SUB_CODECS = {'subrip', 'ass', 'ssa', 'mov_text', 'webvtt', 'text'}


def log(msg):
    print(datetime.datetime.now().strftime('%H:%M:%S') + '  ' + msg, flush=True)


def find_tool(name):
    p = shutil.which(name)
    if p: return p
    roots = [LOCAL] + ([os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages')] if IS_WIN else [])
    for root in roots:
        hits = glob.glob(os.path.join(root, '**', name), recursive=True)
        if hits: return hits[0]
    raise SystemExit(f'{name} not found')


EXE = '.exe' if IS_WIN else ''
FFMPEG = find_tool('ffmpeg' + EXE); FFPROBE = os.path.join(os.path.dirname(FFMPEG), 'ffprobe' + EXE)
os.environ['PATH'] = os.path.dirname(FFMPEG) + os.pathsep + os.environ['PATH']


def run(args, **kw):
    """ffmpeg/ffprobe/ffsubsync with a time limit: a dropped share must not hang the subtitle slot forever."""
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=kw.pop('timeout', 3600), **kw)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'{os.path.basename(str(args[0]))} took longer than an hour - is the media share still reachable?') from None


# ------------------------------------------------------------------ OpenSubtitles
class OpenSubs:
    UA = 'MediaScan v4'

    def __init__(self, cfg):
        self.key = cfg.get('opensubtitles', 'api_key', fallback='').strip()
        self.user = cfg.get('opensubtitles', 'username', fallback='').strip()
        self.pw = cfg.get('opensubtitles', 'password', fallback='').strip()
        self.base = 'https://api.opensubtitles.com/api/v1'
        self.token = None
        self.remaining = None
        self.exhausted = False
        self.disabled = False          # no credentials / cannot sign in: skip searching, keep Whisper + translation

    def _req(self, method, path, params=None, body=None, base=None):
        url = (base or self.base) + path
        if params:
            q = '&'.join(f'{k}={urllib.parse.quote(str(v).lower())}' for k, v in sorted(params.items()))
            url += '?' + q
        h = {'Api-Key': self.key, 'User-Agent': self.UA, 'Accept': 'application/json', 'Content-Type': 'application/json'}
        if self.token: h['Authorization'] = 'Bearer ' + self.token
        data = json.dumps(body).encode() if body is not None else None
        for attempt in range(4):
            try:
                time.sleep(0.3)
                with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=30) as r:
                    return json.loads(r.read().decode('utf-8'))
            except urllib.error.HTTPError as e:
                txt = e.read().decode('utf-8', 'replace')[:300]
                if e.code == 429: time.sleep(3 + attempt * 3); continue
                if e.code == 401 and path != '/login' and self.user and not getattr(self, '_relogin', False):
                    # the session token expires after a while (or when you log in elsewhere): sign in again once
                    self._relogin = True
                    try:
                        self.login()
                    finally:
                        self._relogin = False
                    h['Authorization'] = 'Bearer ' + self.token
                    url = (self.base if base is None else base) + path + (url[url.index('?'):] if '?' in url else '')
                    continue
                if e.code == 406 and 'download' in path.lower():
                    self.exhausted = True
                raise RuntimeError(f'HTTP {e.code} {path}: {txt}')
            except urllib.error.URLError:
                time.sleep(3); continue
        raise RuntimeError(f'OpenSubtitles unreachable: {path}')

    def login(self):
        r = self._req('POST', '/login', body={'username': self.user, 'password': self.pw})
        self.token = r['token']
        if r.get('base_url'): self.base = 'https://' + r['base_url'] + '/api/v1'
        u = r.get('user', {})
        log(f"OpenSubtitles signed in: VIP={u.get('vip')}, downloads/day={u.get('allowed_downloads')}")

    @staticmethod
    def moviehash(path):
        size = os.path.getsize(path)
        if size < 131072: return None
        h = size
        with open(path, 'rb') as f:
            for pos in (0, size - 65536):
                f.seek(pos); buf = f.read(65536)
                for (x,) in struct.iter_unpack('<Q', buf): h = (h + x) & 0xFFFFFFFFFFFFFFFF
        return '%016x' % h

    def search(self, langs, mhash=None, query=None, year=None, season=None, episode=None):
        p = {'languages': ','.join(sorted(langs))}
        if mhash: p['moviehash'] = mhash
        if query: p['query'] = query
        if year: p['year'] = year
        if season is not None: p['season_number'] = season; p['episode_number'] = episode; p['type'] = 'episode'
        elif query: p['type'] = 'movie'
        return self._req('GET', '/subtitles', params=p).get('data', [])

    def download(self, file_id):
        r = self._req('POST', '/download', body={'file_id': file_id})
        self.remaining = r.get('remaining')
        if self.remaining is not None and self.remaining <= 0: self.exhausted = True
        with urllib.request.urlopen(urllib.request.Request(r['link'], headers={'User-Agent': self.UA}), timeout=60) as f:
            data = f.read(5 * 1024 * 1024 + 1)
        if len(data) > 5 * 1024 * 1024: raise RuntimeError('subtitle file unexpectedly large')
        return data


# ------------------------------------------------------------------ helpers
def parse_name(rel):
    """Title/year/episode from the path relative to the media share, e.g. 'Series/Show (2010)/Season 1/Show S01E02.mkv'."""
    parts = [p for p in rel.replace('\\', '/').split('/') if p]
    fname = os.path.splitext(parts[-1])[0]
    m = re.search(r'[Ss](\d{1,2})[Ee](\d{1,3})', fname)
    if m:
        above = [p for p in parts[:-1] if not re.match(r'(?i)^(season|seizoen|staffel|saison|series|specials?)\b|^s\d{1,2}$', p.strip())]
        show = above[-1] if len(parts) > 2 and above else re.split(r'\s+-\s+|\s*[Ss]\d{1,2}[Ee]\d', fname)[0]
        show = re.sub(r'\s*\(\d{4}\)\s*$', '', show)
        return {'kind': 'episode', 'query': show, 'season': int(m.group(1)), 'episode': int(m.group(2)), 'year': None}
    folder = parts[-2] if len(parts) > 1 else ''
    m = re.match(r'^(.*?)\s*\((\d{4})\)', folder) or re.match(r'^(.*?)\s*\((\d{4})\)', fname)
    if m: return {'kind': 'movie', 'query': m.group(1).replace('_', ' ').strip(' -'), 'year': int(m.group(2)), 'season': None, 'episode': None}
    q = re.sub(r'[._]', ' ', fname); q = re.split(r'\b(19|20)\d{2}\b|1080p|2160p|720p', q)[0].strip(' -')
    return {'kind': 'movie', 'query': q, 'year': None, 'season': None, 'episode': None}


def probe(path):
    r = run([FFPROBE, '-v', 'error', '-print_format', 'json', '-show_streams', '-show_format', path])
    if r.returncode or not r.stdout.strip().startswith('{'):
        raise RuntimeError('ffprobe: ' + ((r.stderr or '').strip()[-300:] or 'cannot read the file'))
    return json.loads(r.stdout)


def existing_langs(video, info):
    have, emb_text = set(), {}
    sidx = 0
    for s in info.get('streams', []):
        if s.get('codec_type') != 'subtitle': continue
        lg = EMB.get((s.get('tags') or {}).get('language', '').lower())
        if lg:
            have.add(lg)
            if s.get('codec_name') in TEXT_SUB_CODECS and lg not in emb_text and not (s.get('disposition') or {}).get('forced'):
                emb_text[lg] = sidx
        sidx += 1
    d = os.path.dirname(video); stem = os.path.splitext(os.path.basename(video))[0].lower()
    side = {}
    for f in os.listdir(d):
        fl = f.lower()
        if not fl.endswith(('.srt', '.ass', '.ssa', '.vtt')) or not fl.startswith(stem): continue
        toks = re.split(r'[^a-z]+', fl[len(stem):])
        lg = next((SIDE[t] for t in toks if t in SIDE), None)
        if lg is None and fl.endswith('.srt'): lg = 'en'
        if lg: have.add(lg); side.setdefault(lg, os.path.join(d, f))
    return have, emb_text, side


def decode_sub(raw):
    best = from_bytes(raw).best()
    txt = str(best) if best else raw.decode('utf-8', 'replace')
    return txt.replace('\r\n', '\n').lstrip('﻿')


def load_srt(text):
    subs = list(srt.parse(text, ignore_errors=True))
    return [s for s in subs if s.content.strip()]


def extract_audio(video, out_wav, stream_idx):
    r = run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', video, '-map', f'0:{stream_idx}', '-vn',
             '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', out_wav])
    if r.returncode != 0: raise RuntimeError('audio extraction failed: ' + r.stderr[-300:])


def ffsubsync(wav, in_srt, out_srt):
    exe = os.path.join(sys.prefix, 'Scripts', 'ffsubsync.exe') if IS_WIN else os.path.join(sys.prefix, 'bin', 'ffsubsync')
    if not os.path.exists(exe): exe = shutil.which('ffsubsync') or exe
    r = run([exe, wav, '-i', in_srt, '-o', out_srt, '--output-encoding', 'utf-8'])
    return r.returncode == 0 and os.path.exists(out_srt)


def wrap(text, width=42):
    words = text.split(); lines, cur = [], ''
    for w in words:
        if cur and len(cur) + 1 + len(w) > width: lines.append(cur); cur = w
        else: cur = (cur + ' ' + w).strip()
    if cur: lines.append(cur)
    return lines


# ------------------------------------------------------------------ Whisper backends
# Windows/Linux with NVIDIA: faster-whisper on CUDA. Mac (Apple Silicon): mlx-whisper on the Apple GPU.
# Without NVIDIA and without a Mac: faster-whisper on the CPU (slow, but it works).
class _Seg:
    __slots__ = ('start', 'end', 'text')
    def __init__(self, a, b, t): self.start, self.end, self.text = a, b, t


def _backend():
    if IS_MAC:
        try:
            import mlx_whisper  # noqa: F401
            return 'mlx'
        except ImportError:
            pass
    return 'fw'


_fw_models = {}
def _fw(name):
    from faster_whisper import WhisperModel
    if name not in _fw_models:
        try:
            m = WhisperModel(name, device='cuda', compute_type='float16', download_root=os.path.join(LOCAL, 'models'))
            import numpy as np                     # CUDA libraries only load on the first call: try right away
            list(m.transcribe(np.zeros(16000, dtype=np.float32), beam_size=1)[0])
            _fw_models[name] = m
        except Exception as e:
            log(f'    Whisper: no CUDA ({str(e)[:80]}), falling back to the CPU')
            _fw_models[name] = WhisperModel(name, device='cpu', compute_type='int8', download_root=os.path.join(LOCAL, 'models'))
    return _fw_models[name]


MLX_REPO = {'large-v3': 'mlx-community/whisper-large-v3-mlx', 'large-v3-turbo': 'mlx-community/whisper-large-v3-turbo',
            'medium': 'mlx-community/whisper-medium-mlx', 'small': 'mlx-community/whisper-small-mlx'}


def detect_language(name, audio):
    """Spoken language (code, confidence)."""
    if _backend() == 'mlx':
        import mlx_whisper
        mid = len(audio) // 3      # 30 s from the first third: the start is often music or silence
        r = mlx_whisper.transcribe(audio[mid:mid + 16000 * 30], path_or_hf_repo=MLX_REPO.get(name, name), verbose=None)
        return r['language'], 0.0
    _, info = _fw(name).transcribe(audio, beam_size=1, vad_filter=True, language_detection_segments=6)
    return info.language, info.language_probability


def transcribe(name, audio, language=None, task='transcribe', beam_size=1):
    """Segments (.start/.end/.text). Without a language Whisper picks one itself."""
    if _backend() == 'mlx':
        import mlx_whisper
        if language is None: language, _ = detect_language(name, audio)
        r = mlx_whisper.transcribe(audio, path_or_hf_repo=MLX_REPO.get(name, name), language=language, task=task, verbose=None,
                                   condition_on_previous_text=False, word_timestamps=False)
        return [_Seg(s['start'], s['end'], s['text']) for s in r['segments']
                if not (s.get('no_speech_prob', 0) > 0.6 and s.get('avg_logprob', 0) < -1.0)]   # drop silences/hallucinations
    segs, _ = _fw(name).transcribe(audio, task=task, language=language, beam_size=beam_size, vad_filter=True,
                                   condition_on_previous_text=False, word_timestamps=False)
    return [_Seg(sg.start, sg.end, sg.text) for sg in segs]


# ------------------------------------------------------------------ timing reference (Whisper turbo) + check
RES = 0.1  # 100 ms grid

_audio_cache = {}
def load_audio(wav):
    """16 kHz mono wav -> float32 numpy (without PyAV, avoids version problems)."""
    import numpy as np
    if wav not in _audio_cache:
        _audio_cache.clear()
        with wave.open(wav, 'rb') as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        _audio_cache[wav] = a.astype(np.float32) / 32768.0
    return _audio_cache[wav]


def ref_segments(wav, video, cfg, tag_lang=None):
    """Speech moments according to Whisper (turbo). Cached per video."""
    cache_dir = os.path.join(LOCAL, 'refcache'); os.makedirs(cache_dir, exist_ok=True)
    key = re.sub(r'[^A-Za-z0-9]+', '_', video)[-150:] + '_%d' % os.path.getsize(video)
    cp = os.path.join(cache_dir, key + '.json')
    if os.path.exists(cp):
        return json.load(open(cp, encoding='utf-8'))
    lang = EMB.get((tag_lang or '').lower())
    t = time.time()
    segs = transcribe('large-v3-turbo', load_audio(wav), language=lang, beam_size=1)
    out = [(round(sg.start, 2), round(sg.end, 2)) for sg in segs]
    log(f'    timing reference (Whisper turbo): {len(out)} speech segments in {time.time()-t:.0f}s')
    json.dump(out, open(cp, 'w', encoding='utf-8'))
    return out


def _onsets(iv, n):
    """Signal with peaks at the start and end of sentences (blurred, 0.3 s)."""
    import numpy as np
    m = np.zeros(n, dtype=np.float32)
    for a, b in iv:
        for t in (a, b):
            i = int(t / RES)
            if 0 <= i < n: m[i] += 1
    k = np.exp(-0.5 * (np.arange(-10, 11) * RES / 0.3) ** 2).astype(np.float32)
    return np.convolve(m, k, 'same')


def _mask(iv, n):
    import numpy as np
    m = np.zeros(n, dtype=np.float32)
    for a, b in iv:
        m[max(int(a / RES), 0):max(min(int(b / RES), n), 0)] = 1
    return m


def evaluate(subs, ref):
    """Returns (overlap, global offset, [(local offset, confidence), ...]).
    Offsets in seconds (+ = subtitle too late). Based on the start/end of sentences."""
    import numpy as np
    sv = [(s.start.total_seconds(), s.end.total_seconds()) for s in subs]
    end = max([b for _, b in ref] + [b for _, b in sv]) + 130
    n = int(end / RES)
    R = _onsets(ref, n); S = _onsets(sv, n)
    overlap = float((_mask(ref, n) * _mask(sv, n)).sum() / max(_mask(sv, n).sum(), 1))
    def best(lo, hi, maxs, step=1):
        offs = np.arange(-maxs, maxs + 1, step)
        vals = np.array([float((R[lo:hi] * np.roll(S, -o)[lo:hi]).sum()) for o in offs])
        if vals.max() <= 0: return 0.0, 0.0
        conf = vals.max() / (np.median(vals) + 1e-6)
        return float(offs[int(vals.argmax())]) * RES, float(conf)
    if not ref: return 0.0, 0.0, []            # no speech found (music only, silent film): nothing to compare with
    glob_off, _ = best(0, n, int(120 / RES), 2)
    dur = max(b for _, b in ref)
    win = int(min(600, max(180, dur / 6)) / RES)
    local = []
    for w0 in range(0, n - win // 2, win):
        hi = min(w0 + win, n)
        if R[w0:hi].sum() < 20 or S[w0:hi].sum() < 20: continue
        o, c = best(w0, hi, int(30 / RES))
        local.append((round(o, 1), round(c, 2)))
    return overlap, round(glob_off, 1), local


def timing_ok(subs, ref):
    ov, go, loc = evaluate(subs, ref)
    conf = [o for o, c in loc if c >= 1.35]          # only windows with a clear peak
    good = [o for o in conf if abs(o) <= 0.5]
    ok = ov >= 0.55 and len(conf) >= max(2, int(0.5 * len(loc))) and len(good) >= 0.85 * len(conf)
    return ok, ov, go, [o for o, _ in loc]


def shift(subs, sec):
    d = datetime.timedelta(seconds=sec)
    return [srt.Subtitle(s.index, max(s.start - d, datetime.timedelta(0)), max(s.end - d, datetime.timedelta(0)), s.content) for s in subs]


# ------------------------------------------------------------------ Whisper

def whisper_srt(wav, cfg, tag_lang=None):
    name = cfg.get('whisper', 'model', fallback='large-v3')
    lang = EMB.get((tag_lang or '').lower())
    if not lang:
        lang, prob = detect_language(name, load_audio(wav))
        log(f'    Whisper: detected spoken language: {lang}' + (f' ({prob:.2f})' if prob else ''))
    task = 'transcribe' if lang == 'en' else 'translate'
    log(f'    Whisper: language {lang} -> {task} to English')
    segs = transcribe(name, load_audio(wav), language=lang, task=task, beam_size=5)
    out = []
    for sg in segs:
        lines = wrap(sg.text.strip())
        if not lines: continue
        chunks = [lines[i:i + 2] for i in range(0, len(lines), 2)]
        total = sum(len(' '.join(c)) for c in chunks); t0 = sg.start; dur = sg.end - sg.start
        for c in chunks:
            frac = len(' '.join(c)) / total
            out.append(srt.Subtitle(len(out) + 1, datetime.timedelta(seconds=t0), datetime.timedelta(seconds=t0 + dur * frac), '\n'.join(c)))
            t0 += dur * frac
    return out, lang


def free_gpu():
    """Unload Whisper models from video memory so the translation model has room."""
    import gc
    _fw_models.clear(); gc.collect()
    if _backend() == 'mlx':
        try:
            import mlx.core as mx; mx.clear_cache()
        except Exception:
            pass


# ------------------------------------------------------------------ Ollama translation
_TITLE = ''         # title of the film being translated now (set in process)
CHECK = lambda: None    # set by the worker: raises when the job was cancelled in the dashboard
GUIDE = ('Use the surrounding lines to understand who is speaking, to whom, and what is meant. Translate idioms and fixed '
         'expressions by their meaning, never word for word (e.g. "I\'ll say." means "Absolutely / You are right"). For greetings '
         'and goodbyes pick the form that fits the situation. Keep the speaker\'s tone and level of formality.')
# extra hints per target language (from our own tests with native speakers)
HINTS = {'tr': 'Turkish goodbyes: the person leaving says "Hoşça kal", the person staying says "Güle güle".'}


def _guide(langs):
    return ' '.join([GUIDE] + [HINTS[l] for l in langs if l in HINTS])


def _ctx_text(context):
    """context = list of previous lines, or {'prev': [...], 'next': [...]}."""
    if isinstance(context, dict): prev, nxt = context.get('prev') or [], context.get('next') or []
    else: prev, nxt = context or [], []
    t = ''
    if _TITLE: t += f'Film/series: {_TITLE}\n\n'
    if prev: t += 'Previous lines (context only, do not translate):\n' + '\n'.join(prev[-10:]) + '\n\n'
    if nxt: t += 'Following lines (context only, do not translate):\n' + '\n'.join(nxt[:4]) + '\n\n'
    return t


_NO_THINK = set()   # models that do not support 'think'
_SERVER_WAIT = (15, 30, 60, 90, 120, 120)   # seconds to wait after each Ollama server error (about 7 minutes in total)


class OllamaServerError(RuntimeError):
    """Ollama answered 5xx: the model runner crashed or ran out of memory (e.g. while Whisper or an encode also uses the GPU).
    Ollama usually recovers by itself within minutes, so the same block is retried after a wait; splitting it does not help."""


def _http_error(e):
    """Turns an HTTPError from Ollama into a readable error (the body is {"error": "..."})."""
    try: msg = json.loads(e.read().decode('utf-8', 'replace')).get('error') or ''
    except Exception: msg = ''
    msg = (msg or e.reason or '').strip()[:300]
    if e.code >= 500: return OllamaServerError(f'Ollama HTTP {e.code}: {msg or "server error"}')
    hint = ' - pull the model (ollama pull <model>) or fix ollama_model in worker.ini' if e.code == 404 else ''
    return RuntimeError(f'Ollama HTTP {e.code}: {msg or e.reason}{hint}')


def _chat(url, body, timeout):
    """Ollama chat without 'thinking' (Gemma 4, Qwen 3 etc. otherwise think first: slow and empty JSON).
    If the model does not know the option, retry without it."""
    d = json.loads(body); model = d.get('model')
    if model not in _NO_THINK:
        d['think'] = False
        try:
            return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=json.dumps(d).encode(),
                              headers={'Content-Type': 'application/json'}), timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code != 400: raise _http_error(e) from None
            _NO_THINK.add(model)
    try:
        return json.loads(urllib.request.urlopen(urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'}), timeout=timeout).read())
    except urllib.error.HTTPError as e:
        raise _http_error(e) from None


def _json_lenient(text):
    """Parses the model's answer. A cut-off answer (the model stopped in the middle of a string) is salvaged up to the
    last complete entry, so only the missing lines are asked for again instead of redoing the whole block."""
    text = text.strip()
    try:
        return json.loads(text), False
    except ValueError as err:
        first = err
    i = text.find('{')
    if i < 0: raise first
    stack, instr, esc, cut = [], False, False, None
    for p in range(i, len(text)):
        ch = text[p]
        if instr:
            if esc: esc = False
            elif ch == '\\': esc = True
            elif ch == '"': instr = False
            continue
        if ch == '"': instr = True
        elif ch in '{[': stack.append(ch)
        elif ch in '}]':
            if not stack: break
            stack.pop()
            if not stack:                          # complete object (maybe inside a code fence): parse just that
                try: return json.loads(text[i:p + 1]), False
                except ValueError: raise first from None
        elif ch == ',': cut = (p, list(stack))     # a complete member ended here
    if cut is None: raise first
    p, st = cut
    try:
        return json.loads(text[i:p] + ''.join('}' if c == '{' else ']' for c in reversed(st))), True
    except ValueError:
        raise first from None


def _answer(url, model, sysmsg, user, timeout, want):
    """One model call; returns {line number: value}. Tolerates a cut-off answer and a few wrong shapes
    (keys like "1." or 1, the answer wrapped in an extra object, or languages outside instead of inside)."""
    body = json.dumps({'model': model, 'stream': False, 'format': 'json', 'keep_alive': '10m',
                       'options': {'temperature': 0.2, 'num_ctx': 8192, 'num_predict': -1},
                       'messages': [{'role': 'system', 'content': sysmsg}, {'role': 'user', 'content': user}]}).encode()
    r = _chat(url, body, timeout)
    d, cut = _json_lenient(r['message']['content'])
    if cut:
        log(f'    answer was cut off (done_reason={r.get("done_reason")}, {r.get("eval_count")} tokens): kept {len(d)} of {len(want)} lines')
    if not isinstance(d, dict): return {}
    num = lambda k: re.sub(r'\D', '', str(k))
    if not any(num(k) in want for k in d):
        inner = [v for v in d.values() if isinstance(v, dict)]
        if len(inner) == 1 and any(num(k) in want for k in inner[0]): d = inner[0]        # {"translations": {...}}
        elif inner and all(k in LANG_NAME for k in d) and any(num(k) in want for k in inner[0]):   # {"nl": {"1": ..}, "tr": {..}}
            flip = {}
            for l, m in d.items():
                for k, v in m.items(): flip.setdefault(num(k), {})[l] = v
            d = flip
    return {num(k): v for k, v in d.items() if num(k) in want}


_NOTEXT = re.compile(r'^[\W\d_♪♫]*$')     # only music symbols, dots, digits: nothing to translate


def _ollama_chunk(url, model, sysmsg, chunk, context):
    """Translates a block of lines. Missing lines are asked for separately instead of redoing the whole block."""
    src = {str(k + 1): s.content.replace('\n', ' ').strip() for k, s in enumerate(chunk)}
    done = {k: v for k, v in src.items() if _NOTEXT.match(v)}          # e.g. '♪' or '...' stays as it is
    for _ in range(3):
        todo = {k: v for k, v in src.items() if k not in done}
        if not todo: break
        user = _ctx_text(context) + 'Translate:\n' + json.dumps(todo, ensure_ascii=False)
        for k, v in _answer(url, model, sysmsg, user, 600, todo).items():
            if isinstance(v, str) and v.strip(): done[k] = v.strip()
    missing = [k for k in src if k not in done]
    if missing:
        raise ValueError(f'incomplete answer ({len(missing)} of {len(src)} lines missing)')
    return [done[str(k + 1)] for k in range(len(chunk))]


def _ollama_up(url):
    """Is Ollama reachable at all? One quick call, so a stopped Ollama fails in seconds instead of half an hour of retries."""
    try:
        urllib.request.urlopen(url.rsplit('/api/', 1)[0] + '/api/tags', timeout=10).read(); return True
    except Exception as e:
        raise RuntimeError(f'Ollama is not reachable at {url.rsplit("/api/", 1)[0]} ({e}) - start Ollama or check ollama_url')


def _call(fn, url, what):
    """Runs one translation call. Ollama server errors are waited out (same block, no splitting) and a connection that is
    gone fails at once. Content errors (cut-off or incomplete answer) are raised to the caller, which retries or splits."""
    for n, wait in enumerate(_SERVER_WAIT + (None,)):
        try:
            return fn()
        except OllamaServerError as e:
            if wait is None: raise RuntimeError(f'{e} - Ollama kept failing for {sum(_SERVER_WAIT) // 60} minutes')
            log(f'    {e} ({what}) - waiting {wait}s, retry {n + 1}/{len(_SERVER_WAIT)}')
            time.sleep(wait); _ollama_up(url); free_gpu()
        except urllib.error.URLError as e:
            raise RuntimeError(f'Ollama unreachable: {e}')      # connection gone: retrying or splitting won't help


def ollama_translate(subs, lang, cfg, batch=25):
    free_gpu()
    url = _tr(cfg, 'ollama_url', 'http://localhost:11434').rstrip('/') + '/api/chat'
    model = _tr(cfg, 'ollama_model', DEFAULT_MODEL)
    target = LANG_NAME[lang]
    out, context = [], []
    sysmsg = (f'You are a professional subtitle translator. Translate English movie/TV subtitles into natural, fluent, '
              f'colloquial {target} as a native {target} subtitler would. Keep the meaning, tone, names and humor. '
              f'Keep lines short. Translate each numbered line separately; do not merge or split lines. '
              f'Answer ONLY with a JSON object mapping each number (as string) to its {target} translation. ' + _guide([lang]))

    _ollama_up(url)

    def translate(chunk, depth=0):
        for attempt in range(2):
            try:
                return _call(lambda: _ollama_chunk(url, model, sysmsg, chunk, context), url, f'{len(chunk)} lines')
            except (ValueError, KeyError, TypeError, TimeoutError) as e:      # the model's answer was unusable or took too long
                log(f'    translation attempt {attempt + 1} failed ({len(chunk)} lines): {e!r}')
                time.sleep(2)
        if len(chunk) > 1 and depth < 4:   # split and retry
            h = len(chunk) // 2
            return translate(chunk[:h], depth + 1) + translate(chunk[h:], depth + 1)
        raise RuntimeError(f'translation failed: no usable answer for {len(chunk)} line(s) after retries')

    for i in range(0, len(subs), batch):
        CHECK()
        chunk = subs[i:i + batch]
        context = {'prev': context if isinstance(context, list) else [], 'next': [x.content.replace('\n', ' ') for x in subs[i + batch:i + batch + 4]]}
        texts = translate(chunk)
        for s, t in zip(chunk, texts):
            out.append(srt.Subtitle(s.index, s.start, s.end, '\n'.join(wrap(t))))
        context = [s.content.replace('\n', ' ') for s in chunk]
        if (i // batch) % 8 == 7 or i + batch >= len(subs):
            log(f'    translating to {target}: {min(i + batch, len(subs))}/{len(subs)}')
    return out


def _ollama_multi_chunk(url, model, langs, chunk, context):
    """One call for several languages: {"1": {"nl": "...", "tr": "..."}}. Lines that miss a language are asked for again."""
    names = ', '.join(f'"{l}" = {LANG_NAME[l]}' for l in langs)
    sysmsg = (f'You are a professional subtitle translator. Translate English movie/TV subtitles into natural, fluent, colloquial '
              f'subtitles in these languages: {names}. Keep the meaning, tone, names and humor. Keep lines short. '
              f'Translate each numbered line separately; do not merge or split lines. Answer ONLY with a JSON object that maps '
              f'each number (as string) to an object with one key per language code, e.g. {{"1": {{' +
              ', '.join(f'"{l}": "..."' for l in langs) + '}}. ' + _guide(langs))      # plain string: two braces close the example
    src = {str(k + 1): s.content.replace('\n', ' ').strip() for k, s in enumerate(chunk)}
    done = {k: {l: v for l in langs} for k, v in src.items() if _NOTEXT.match(v)}
    complete = lambda k: all(l in done.get(k, {}) for l in langs)
    for _ in range(3):
        todo = {k: v for k, v in src.items() if not complete(k)}
        if not todo: break
        user = _ctx_text(context) + 'Translate:\n' + json.dumps(todo, ensure_ascii=False)
        for k, v in _answer(url, model, sysmsg, user, 900, todo).items():
            if isinstance(v, dict):
                for l in langs:
                    if isinstance(v.get(l), str) and v[l].strip(): done.setdefault(k, {})[l] = v[l].strip()
    missing = [k for k in src if not complete(k)]
    if missing:
        raise ValueError(f'incomplete answer ({len(missing)} of {len(src)} lines missing)')
    return [done[str(k + 1)] for k in range(len(chunk))]


def ollama_translate_multi(subs, langs, cfg, batch=25):
    """Translates to all languages in one pass (saves half the calls). If a block fails, per language separately."""
    if len(langs) == 1:
        return {langs[0]: ollama_translate(subs, langs[0], cfg, batch)}
    free_gpu()
    url = _tr(cfg, 'ollama_url', 'http://localhost:11434').rstrip('/') + '/api/chat'
    model = _tr(cfg, 'ollama_model', DEFAULT_MODEL)
    _ollama_up(url)
    out = {l: [] for l in langs}; prev = []
    for i in range(0, len(subs), batch):
        CHECK()
        chunk = subs[i:i + batch]; texts = None
        context = {'prev': prev, 'next': [x.content.replace('\n', ' ') for x in subs[i + batch:i + batch + 4]]}
        for attempt in range(2):
            try:
                texts = _call(lambda: _ollama_multi_chunk(url, model, langs, chunk, context), url, f'{len(chunk)} lines, {"+".join(langs)}'); break
            except (ValueError, KeyError, TypeError, TimeoutError) as e:
                log(f'    translation attempt {attempt + 1} failed ({len(chunk)} lines, {"+".join(langs)}): {e!r}')
                time.sleep(2)
        if texts is None:                                   # fallback: per language, with splitting
            per = {l: ollama_translate(chunk, l, cfg, batch) for l in langs}
            texts = [{l: per[l][k].content.replace('\n', ' ') for l in langs} for k in range(len(chunk))]
        for s, t in zip(chunk, texts):
            for l in langs: out[l].append(srt.Subtitle(s.index, s.start, s.end, '\n'.join(wrap(t[l]))))
        prev = [s.content.replace('\n', ' ') for s in chunk]
        if (i // batch) % 8 == 7 or i + batch >= len(subs):
            log(f'    translating to {"+".join(LANG_NAME[l] for l in langs)}: {min(i + batch, len(subs))}/{len(subs)}')
    return out


# ------------------------------------------------------------------ main logic per video
def process(video, cfg, osub, out_dir, want, report, rel=None):
    global _TITLE
    log(f'=== {os.path.basename(video)}')
    rel = rel or os.path.basename(video)
    want = [l for l in want if l in LANG_NAME]
    try:
        meta = parse_name(rel)
        _TITLE = meta['query'] + (f" ({meta['year']})" if meta.get('year') else '') + \
            (f" S{int(meta['season']):02d}E{int(meta['episode']):02d}" if meta.get('season') and meta.get('episode') else '')
    except Exception:
        _TITLE = os.path.splitext(os.path.basename(video))[0]
    info = probe(video)
    have, emb_text, side = existing_langs(video, info)
    missing = [l for l in want if l not in have]
    log(f'  present: {sorted(have) or "-"}  missing: {missing or "-"}')
    if not missing: return
    stem = os.path.splitext(os.path.basename(video))[0]
    target_dir = out_dir or os.path.dirname(video)
    os.makedirs(target_dir, exist_ok=True)
    tmp = tempfile.mkdtemp(prefix='subs_', dir=LOCAL)
    try:
        auds = [s for s in info['streams'] if s.get('codec_type') == 'audio']
        aud = next((s for s in auds if (s.get('disposition') or {}).get('default')), auds[0] if auds else None)
        wav = os.path.join(tmp, 'audio.wav')

        ref_cache = {}
        def emb_en():
            """Embedded English text subtitle: belongs to exactly this video, so the timing is right by definition."""
            if 'emb' not in ref_cache:
                ref_cache['emb'] = None
                if 'en' in emb_text:
                    p = os.path.join(tmp, 'emb_en.srt')
                    r = run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-i', video, '-map', f'0:s:{emb_text["en"]}', '-c:s', 'srt', p])
                    if r.returncode == 0:
                        try:
                            subs = load_srt(open(p, encoding='utf-8').read())
                            if len(subs) >= 50: ref_cache['emb'] = (subs, p)
                        except Exception as e:
                            log(f'  embedded EN unreadable: {e}')
            return ref_cache['emb']
        def get_ref():
            if 'r' not in ref_cache:
                e = emb_en()
                if e:
                    log('  timing reference: embedded EN subtitle (no audio needed)')
                    ref_cache['r'] = [(s.start.total_seconds(), s.end.total_seconds()) for s in e[0]]
                else:
                    if not os.path.exists(wav):
                        log('  reading audio...'); extract_audio(video, wav, aud['index'])
                    ref_cache['r'] = ref_segments(wav, video, cfg, (aud.get('tags') or {}).get('language'))
            return ref_cache['r']
        def get_ref_srt():
            e = emb_en()
            if e: return e[1]                    # ffsubsync can also sync against a subtitle
            p = os.path.join(tmp, 'ref.srt')
            if not os.path.exists(p):
                cues = [srt.Subtitle(i + 1, datetime.timedelta(seconds=a), datetime.timedelta(seconds=b), '...') for i, (a, b) in enumerate(get_ref())]
                with open(p, 'w', encoding='utf-8') as f: f.write(srt.compose(cues))
            return p

        def write(lang, subs, source, score):
            p = os.path.join(target_dir, f'{stem}.{lang}.srt')
            with open(p, 'w', encoding='utf-8', newline='\n') as f: f.write(srt.compose(subs, reindex=True))
            log(f'  -> {lang.upper()} saved ({source}, timing score {score:.2f})')
            report.append({'video': video, 'lang': lang, 'source': source, 'score': round(score, 3), 'cues': len(subs)})

        # 1) OpenSubtitles
        CHECK(); got = {}
        if not osub.exhausted and not osub.disabled:
            meta = parse_name(rel); mh = OpenSubs.moviehash(video)
            cands = []
            try:
                if mh:
                    cands = osub.search(missing, mhash=mh)
                    cands = [c for c in cands if c['attributes'].get('moviehash_match')]
                cands += osub.search(missing, query=meta['query'], year=meta['year'], season=meta['season'], episode=meta['episode'])
            except Exception as e:
                log(f'  search failed: {e}')
            for lang in missing:
                lc = [c for c in cands if c['attributes'].get('language') == lang
                      and not c['attributes'].get('machine_translated') and not c['attributes'].get('ai_translated')]
                lc.sort(key=lambda c: (not c['attributes'].get('moviehash_match'), bool(c['attributes'].get('hearing_impaired')),
                                       -(c['attributes'].get('download_count') or 0)))
                seen = set()
                for c in lc[:4]:
                    fid = ((c['attributes'].get('files') or [{}])[0]).get('file_id')
                    if not fid or fid in seen: continue
                    seen.add(fid)
                    if osub.exhausted: log('  download limit reached - rest later'); break
                    try:
                        subs = load_srt(decode_sub(osub.download(fid)))
                    except Exception as e:
                        log(f'  download failed: {e}'); continue
                    if len(subs) < 20: continue
                    ref = get_ref()
                    how = 'OpenSubtitles' + (' (hash)' if c['attributes'].get('moviehash_match') else '')
                    ok, ov, go, loc = timing_ok(subs, ref)
                    log(f'  candidate {lang.upper()}: overlap {ov:.2f}, offset {go:+.1f}s, local {loc}')
                    best = subs
                    if not ok:
                        a = os.path.join(tmp, 'in.srt'); b = os.path.join(tmp, 'out.srt')
                        with open(a, 'w', encoding='utf-8') as f: f.write(srt.compose(subs))
                        if ffsubsync(get_ref_srt(), a, b):
                            with open(b, encoding='utf-8') as f: s2 = load_srt(f.read())
                            ok, ov, go, loc = timing_ok(s2, ref)
                            log(f'    after ffsubsync: overlap {ov:.2f}, offset {go:+.1f}s, local {loc}')
                            if ok: best, how = s2, how + ' + synced'
                        if not ok and loc and max(loc) - min(loc) <= 0.8 and abs(sorted(loc)[len(loc)//2]) > 0.6:
                            s3 = shift(subs, sorted(loc)[len(loc)//2])
                            ok, ov, go, loc = timing_ok(s3, ref)
                            log(f'    after shifting: overlap {ov:.2f}, local {loc}')
                            if ok: best, how = s3, how + ' + shifted'
                    if ok:
                        got[lang] = (best, how, ov); break
                    log('    timing does not match (different film version?) - next candidate')
            if osub.remaining is not None: log(f'  (OpenSubtitles downloads left today: {osub.remaining})')
        for lang, (subs, how, sc) in got.items(): write(lang, subs, how, sc)
        if osub.exhausted and any(l not in got for l in missing):
            log('  limit reached: missing languages will be searched again later (no machine translation now)')
            return

        # 2) determine the English source for translation
        still = [l for l in missing if l not in got]
        if not still: return
        en = None
        if 'en' in got: en = got['en'][0]
        elif emb_en(): en = emb_en()[0]
        elif 'en' in side and side['en'].lower().endswith('.srt'):
            en = load_srt(decode_sub(open(side['en'], 'rb').read()))
            ok, ov, go, loc = timing_ok(en, get_ref())
            log(f'  existing EN subtitle as source: overlap {ov:.2f}, local {loc}' + ('' if ok else ' -> timing does not match, Whisper becomes the source'))
            if not ok: en = None
        if 'en' in still or not en:
            CHECK()
            if aud is None: raise RuntimeError('no audio stream to transcribe')
            if not os.path.exists(wav):
                log('  reading audio...'); extract_audio(video, wav, aud['index'])
            log('  Whisper running on the GPU...')
            wsubs, spoken = whisper_srt(wav, cfg, (aud.get('tags') or {}).get('language'))
            if 'en' in still:
                write('en', wsubs, 'Whisper (' + ('transcription' if spoken == 'en' else f'translated from {spoken}') + ')', evaluate(wsubs, get_ref())[0])
            if not en: en = wsubs
        # 3) translate
        todo = [l for l in still if l != 'en']
        if todo:
            CHECK(); res = ollama_translate_multi(en, todo, cfg)
            for lang in todo:
                write(lang, res[lang], f'translated from English ({_tr(cfg, 'ollama_model', DEFAULT_MODEL)})', evaluate(res[lang], get_ref())[0])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
