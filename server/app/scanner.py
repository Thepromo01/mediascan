"""Scan the library (read-only) and plan which jobs are needed."""
import os, re, json, time, subprocess, threading
from . import db
from .langs import EMB, SIDE

MEDIA = os.environ.get('MEDIA_ROOT', '/media')
VIDEO_EXT = {'.mkv', '.mp4', '.m4v', '.avi', '.mov', '.wmv', '.ts', '.m2ts', '.mts', '.mpg', '.mpeg', '.vob', '.webm', '.flv', '.divx', '.ogm', '.rmvb', '.3gp'}
SUB_EXT = ('.srt', '.ass', '.ssa', '.vtt', '.sub', '.idx', '.sup')
BATCH = 300

state = {'running': False, 'phase': '', 'done': 0, 'total': 0, 'new': 0, 'changed': 0, 'gone': 0, 'started': None, 'finished': None}
_scan_lock = threading.Lock()
plan_lock = threading.Lock()         # plan_jobs and manual queueing never run at the same time (no duplicate jobs)

# Minimum video bitrate of the new file: min_vbr_1080 (Mbps for 1080p); the other resolutions scale
# proportionally, except 4K when min_vbr_2160 is set. HDR slightly higher, animation (flat colours) may go lower.
RES_FACTOR = {'SD': 0.3, '720p': 0.55, '1080p': 1.0, '2160p': 2.5}
HDR_FACTOR, ANIM_FACTOR = 1.15, 0.6


_VF = {'t': 0, 'v': (0, 0)}


def _vfloor_settings():
    """The bitrate / saving settings, re-read at most once a second (planning runs call this for every file)."""
    if time.time() - _VF['t'] > 1:
        _VF.update(t=time.time(), v=(db.setting('min_vbr_1080', float) or 0, db.setting('min_vbr_2160', float) or 0,
                                     db.setting('min_saving_pct', float) or 0))
    return _VF['v']


def vfloor(width, height, hdr, category):
    """Minimum video bitrate (bit/s) for a film with this resolution/HDR/folder."""
    rc = rescls(width or 0, height or 0)
    fhd, uhd, _ = _vfloor_settings()                              # own 4K value (0 = 2.5 x the 1080p value)
    base = uhd * 1e6 if rc == '2160p' and uhd > 0 else fhd * 1e6 * RES_FACTOR[rc]
    return base * (HDR_FACTOR if hdr else 1) * \
        (ANIM_FACTOR if category in _anim_dirs() else 1)
FILE_COLS = ('category', 'size', 'mtime', 'codec', 'width', 'height', 'hdr', 'dv', 'duration', 'vbr', 'audio',
             'subs_have', 'ours', 'plan', 'est_save', 'gone', 'last_seen', 'probe_err')


def rescls(w, h):
    if w >= 3200 or h >= 1700: return '2160p'
    if w >= 1700 or h >= 950: return '1080p'
    if w >= 1100 or h >= 650: return '720p'
    return 'SD'


def _num(v):
    try: return float(v)
    except (TypeError, ValueError): return None


def summarize(probe, size):
    """Relevant fields from ffprobe JSON."""
    streams = probe.get('streams', [])
    vs = [s for s in streams if s.get('codec_type') == 'video' and not (s.get('disposition') or {}).get('attached_pic')]
    if not vs: return None
    v = vs[0]; fmt = probe.get('format', {})
    dur = _num(fmt.get('duration')) or 0
    dv = 0
    for sd in v.get('side_data_list') or []:
        if 'DOVI' in sd.get('side_data_type', ''): dv = int(sd.get('dv_profile') or 1)
    tags = v.get('tags') or {}
    ours = 1 if any(k.lower().startswith('mediascan') for k in (fmt.get('tags') or {})) else 0
    # files encoded by MediaScan before 1.0 carry the source's BPS tag (copied by ffmpeg): do not trust it
    br = None if ours else (_num(tags.get('BPS')) or _num(tags.get('BPS-eng')))
    br = br or _num(v.get('bit_rate'))
    if br is None:
        tot = _num(fmt.get('bit_rate')) or (size * 8 / dur if dur else 0)
        audio = 0
        for s in streams:
            if s.get('codec_type') == 'audio':
                t = s.get('tags') or {}
                audio += _num(s.get('bit_rate')) or _num(t.get('BPS')) or _num(t.get('BPS-eng')) or 640000
        br = max(tot - audio, tot * 0.5)
    subs = {EMB[l] for l in (((s.get('tags') or {}).get('language') or '').lower() for s in streams if s.get('codec_type') == 'subtitle') if l in EMB}
    audio = [((s.get('tags') or {}).get('language') or '') + ':' + (s.get('codec_name') or '') for s in streams if s.get('codec_type') == 'audio']
    return dict(codec=v.get('codec_name'), width=v.get('width') or 0, height=v.get('height') or 0,
                hdr=1 if v.get('color_transfer') in ('smpte2084', 'arib-std-b67') else 0, dv=dv,
                duration=dur, vbr=br, audio=json.dumps(audio), subs_emb=subs, ours=ours)


DISC_DIRS = ('/video_ts/', '/bdmv/', '/audio_ts/')


def is_disc(rel):
    return any(d in '/' + rel.lower() for d in DISC_DIRS)


_anim = {'t': 0, 'v': set()}


def _anim_dirs():
    """Folders with animation (lower minimum bitrate). Setting 'anim_dirs', re-read once a minute."""
    if time.time() - _anim['t'] > 60:
        _anim.update(t=time.time(), v={x.strip() for x in (db.setting('anim_dirs') or '').split(',') if x.strip()})
    return _anim['v']


def plan(s, category, rel=''):
    """Encode candidate? The worker later determines the real quality/gain per film with VMAF."""
    if is_disc(rel): return 'disc', 0
    if s['ours']: return 'done', 0
    if s['dv']: return 'dolby-vision', 0
    if not s['duration'] or not s['vbr']: return 'unknown', 0
    floor = max(vfloor(s['width'], s['height'], s['hdr'], category), 0.8e6)
    br, c = s['vbr'], s['codec']
    if br < floor * (1.15 if c in ('hevc', 'av1', 'vp9') else 1.1): return 'efficient', 0
    # estimate of the target bitrate (the worker measures the real value with VMAF)
    target = min(br, max(floor * 1.1, br * (0.6 if c in ('hevc', 'vp9', 'av1') else 0.45)))
    save = int(max(0, (br - target) * s['duration'] / 8))
    # below the minimum saving the worker would reject it anyway: do not spend hours of searching on it
    too_small = save < 100e6 or (br - target) / br * 100 < _vfloor_settings()[2]
    return ('small-gain' if too_small else 'encode'), save


def plan_reason(s, category, rel='', plan=None, err=None):
    """One short sentence that explains the plan for this file, from the same numbers plan() uses (so it never
    contradicts it). s = the summarize() fields (codec, width, height, hdr, dv, duration, vbr, ours)."""
    if plan == 'error': return f'Could not be probed: {err}' if err else 'Could not be probed'
    if plan == 'disc' or is_disc(rel): return 'Part of a DVD/Blu-ray folder'
    if in_skipped(rel, skip_set()): return 'Skipped folder'
    if plan == 'done' or s.get('ours'): return 'Already encoded by MediaScan'
    if plan == 'dolby-vision' or s.get('dv'): return 'Dolby Vision: re-encoding would drop the DV layer'
    if plan == 'no-gain': return 'Tried: the new version could not meet the quality target with a smaller file'
    if plan == 'restored': return 'Original restored from quarantine'
    if not s.get('duration') or not s.get('vbr'): return 'Duration or bitrate unknown: the saving cannot be estimated'
    floor = max(vfloor(s.get('width'), s.get('height'), s.get('hdr'), category), 0.8e6)
    br, c = s['vbr'], (s.get('codec') or 'unknown').upper()
    mb = lambda v: f'{v / 1e6:.1f} Mbps'
    if br < floor * (1.15 if c in ('HEVC', 'AV1', 'VP9') else 1.1):
        return f'Already {c} at {mb(br)}, close to the {mb(floor)} floor: too little to gain' if c in ('HEVC', 'AV1', 'VP9') else \
            f'{c} at {mb(br)} is already close to the {mb(floor)} floor: too little to gain'
    target = min(br, max(floor * 1.1, br * (0.6 if c in ('HEVC', 'VP9', 'AV1') else 0.45)))
    save = max(0, (br - target) * s['duration'] / 8); pct = (br - target) / br * 100
    min_pct = _vfloor_settings()[2]
    if save < 100e6: return f'{c} at {mb(br)}: less than 100 MB to save'
    if pct < min_pct: return f'{c} at {mb(br)}: about {pct:.0f}% to save, below the {min_pct:g}% minimum saving'
    return f'{c} at {mb(br)}: about {save / 1e9:.1f} GB to save' if save >= 1e9 else f'{c} at {mb(br)}: about {save / 1e6:.0f} MB to save'


def sidecar_langs(fname, siblings):
    stem = os.path.splitext(fname)[0].lower(); have = set()
    for f in siblings:
        fl = f.lower()
        if not fl.endswith(SUB_EXT) or not fl.startswith(stem) or fl[len(stem):len(stem) + 1] not in '.-_': continue
        lg = next((SIDE[t] for t in re.split(r'[^a-z]+', fl[len(stem):]) if t in SIDE), None)
        if lg is None and fl.endswith('.srt'): lg = 'en'
        if lg: have.add(lg)
    return have


def ffprobe(path):
    r = subprocess.run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', path],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    if r.returncode != 0 or not r.stdout.strip().startswith('{'):
        raise RuntimeError((r.stderr or 'ffprobe error').strip()[-300:])
    return json.loads(r.stdout)


def file_fields(rel, size, mtime, probe, sub_side, now, err=None):
    cat = rel.split('/')[0]
    s = summarize(probe, size) if probe is not None else None
    if s is None:
        return dict(category=cat, size=size, mtime=mtime, codec=None, width=None, height=None, hdr=None, dv=None, duration=None,
                    vbr=None, audio=None, subs_have=json.dumps(sorted(sub_side)), ours=0, plan='error', est_save=0, gone=0,
                    last_seen=now, probe_err=err or 'no video track')
    pl, save = plan(s, cat, rel)
    return dict(category=cat, size=size, mtime=mtime, codec=s['codec'], width=s['width'], height=s['height'], hdr=s['hdr'],
                dv=s['dv'], duration=s['duration'], vbr=s['vbr'], audio=s['audio'],
                subs_have=json.dumps(sorted(s['subs_emb'] | set(sub_side))), ours=s['ours'], plan=pl, est_save=save,
                gone=0, last_seen=now, probe_err=None)


def upsert(c, rel, fields, now):
    """Insert or update within an existing transaction. Returns True if it is new."""
    row = c.execute('SELECT id FROM files WHERE rel=?', (rel,)).fetchone()
    vals = [fields[k] for k in FILE_COLS]
    if row:
        c.execute('UPDATE files SET ' + ','.join(f'{k}=?' for k in FILE_COLS) + ' WHERE id=?', (*vals, row['id']))
        return False
    c.execute('INSERT INTO files(rel,first_seen,' + ','.join(FILE_COLS) + ') VALUES(?,?,' + ','.join('?' * len(FILE_COLS)) + ')',
              (rel, now, *vals))
    return True


def skip_set():
    return {x.strip() for x in db.setting('skip_dirs').split(',') if x.strip()}


def in_skipped(rel, skip):
    """Is this path inside a folder listed under 'Skip folders' (or a hidden/system folder)?"""
    return any(part in skip or part == '_MediaScan' or part.startswith(('.', '@', '#')) for part in rel.split('/')[:-1])


def scan():
    if not _scan_lock.acquire(blocking=False): return False
    try:
        skip = skip_set()
        state.update(running=True, phase='walking folders', done=0, total=0, new=0, changed=0, gone=0, started=time.time(), finished=None)
        now = time.time(); found = []
        if not os.path.isdir(MEDIA) or not os.listdir(MEDIA):
            raise RuntimeError('media folder is empty or not mounted - nothing changed')
        for root, dirs, files in os.walk(MEDIA):
            relroot = os.path.relpath(root, MEDIA).replace('\\', '/')
            dirs[:] = [d for d in dirs if d not in skip and d != '_MediaScan' and not d.startswith(('.', '@', '#'))]
            subs = [f for f in files if f.lower().endswith(SUB_EXT)]      # once per folder, not per video
            for fn in files:
                if os.path.splitext(fn)[1].lower() in VIDEO_EXT:
                    found.append((relroot, fn, subs))
            state['total'] = len(found)
        known = {r['rel']: r for r in db.q('SELECT id, rel, size, mtime, subs_have FROM files WHERE gone=0')}
        state['phase'] = 'analysing files'
        seen = set(); touch = []; pending = []

        def flush():
            with db.tx() as c:
                c.executemany('UPDATE files SET subs_have=? WHERE id=?', [(h, i) for _, h, i in touch])
                for rel, fields in pending:
                    if not os.path.exists(os.path.join(MEDIA, rel)): continue      # replaced or deleted while this batch was probed
                    if upsert(c, rel, fields, now):
                        state['new'] += 1
                        c.execute('INSERT INTO events(ts,level,msg) VALUES(?,?,?)', (now, 'info', f'New: {rel}'))
                    else:
                        state['changed'] += 1
            touch.clear(); pending.clear()

        for i, (relroot, fn, sib) in enumerate(found):
            rel = fn if relroot == '.' else f'{relroot}/{fn}'
            seen.add(rel)
            full = os.path.join(MEDIA, rel)
            try: st = os.stat(full)
            except OSError: continue
            k = known.get(rel); side = sidecar_langs(fn, sib)
            if k and k['size'] == st.st_size and abs((k['mtime'] or 0) - st.st_mtime) < 2:
                have = set(json.loads(k['subs_have'] or '[]')) | side
                touch.append((now, json.dumps(sorted(have)), k['id']))
            else:
                try: pr, err = ffprobe(full), None
                except Exception as e: pr, err = None, str(e)
                pending.append((rel, file_fields(rel, st.st_size, st.st_mtime, pr, side, now, err)))
            state['done'] = i + 1
            if len(touch) + len(pending) >= BATCH: flush()
        flush()
        missing = [r for r in known if r not in seen]
        if missing:
            mids = [known[r]['id'] for r in missing]; cur = {}
            for i in range(0, len(mids), 500):
                cur.update({r['id']: r['rel'] for r in db.q('SELECT id, rel FROM files WHERE id IN (%s)' % ','.join('?' * len(mids[i:i + 500])), mids[i:i + 500])})
            missing = [r for r in missing if not os.path.exists(os.path.join(MEDIA, cur.get(known[r]['id'], r)))]   # still there under its current name: not gone
        if known and len(missing) > max(20, len(known) // 2):
            raise RuntimeError(f'{len(missing)} of {len(known)} files are missing at once - share unreachable? Nothing marked gone')
        skipped = [known[r]['id'] for r in missing if in_skipped(r, skip)]     # deliberately skipped folder, not gone
        gone = [known[r]['id'] for r in missing if not in_skipped(r, skip)]
        with db.tx() as c:
            c.executemany('UPDATE files SET gone=1 WHERE id=?', [(g,) for g in gone + skipped])
            c.executemany("UPDATE jobs SET status='cancelled', message='file disappeared' WHERE file_id=? AND status='queued'", [(g,) for g in gone])
            c.executemany("UPDATE jobs SET status='skipped', message='folder is in Skip folders' WHERE file_id=? AND status='queued'",
                          [(g,) for g in skipped])
        state['gone'] = len(gone); state['phase'] = 'planning jobs'
        n_enc, n_sub = plan_jobs()
        sync_discs()
        state.update(running=False, phase='done', finished=time.time())
        if state['new'] > 100: db.x('DELETE FROM events WHERE id < (SELECT MAX(id) FROM events) - 5000')
        db.event(f"Scan done: {len(found)} videos, {state['new']} new, {state['changed']} changed, {len(gone)} gone"
                 + (f", {len(skipped)} in skipped folders" if skipped else '') + "; "
                 f"{n_enc} encode and {n_sub} subtitle jobs added")
        return True
    except Exception as e:
        state.update(running=False, phase='error: ' + str(e), finished=time.time())
        db.event(f'Scan failed: {e}', 'error')
        return False
    finally:
        _scan_lock.release()


def plan_jobs():
    with plan_lock: return _plan_jobs()


def _plan_jobs():
    now = time.time()
    langs = [l.strip() for l in db.setting('langs').split(',') if l.strip()]
    has_enc = {r['file_id'] for r in db.q("SELECT DISTINCT file_id FROM jobs WHERE kind='encode' AND status!='cancelled'")}
    has_sub = {r['file_id'] for r in db.q("SELECT DISTINCT file_id FROM jobs WHERE kind='subs' AND status!='cancelled'")}
    enc, sub = [], []
    # never process files from DVD/Blu-ray folders on their own (also clean up old queued jobs)
    with db.tx() as c:
        c.execute("UPDATE files SET plan='disc', est_save=0 WHERE gone=0 AND plan!='disc' AND "
                  "(lower(rel) LIKE '%/video_ts/%' OR lower(rel) LIKE '%/bdmv/%' OR lower(rel) LIKE '%/audio_ts/%')")
        c.execute("UPDATE jobs SET status='cancelled', message='part of a DVD/Blu-ray folder' WHERE status='queued' AND "
                  "file_id IN (SELECT id FROM files WHERE plan='disc')")
    for f in db.q("SELECT id, plan, subs_have FROM files WHERE gone=0"):
        if f['plan'] == 'encode' and f['id'] not in has_enc:
            enc.append((f['id'], 'encode', 'queued', 0, now))
        if f['plan'] not in ('error', 'disc') and f['id'] not in has_sub and any(l not in json.loads(f['subs_have'] or '[]') for l in langs):
            sub.append((f['id'], 'subs', 'queued', 0, now))
    db.xmany('INSERT INTO jobs(file_id,kind,status,priority,created) VALUES(?,?,?,?,?)', enc + sub)
    return len(enc), len(sub)


DISC_RE = re.compile(r'^(.*?)/(video_ts|bdmv)(/|$)', re.I)


def sync_discs():
    """Track DVD/Blu-ray folders (VIDEO_TS / BDMV) as a whole: they can be converted into one .mkv."""
    found = {}
    for f in db.q("SELECT rel, size FROM files WHERE gone=0 AND plan='disc'"):
        m = DISC_RE.match(f['rel'])
        if not m: continue
        k = found.setdefault(m.group(1), {'kind': 'bd' if m.group(2).lower() == 'bdmv' else 'dvd', 'size': 0})
        k['size'] += f['size'] or 0
    known = {r['folder']: r for r in db.q('SELECT folder, status FROM discs')}
    with db.tx() as c:
        for folder, d in found.items():
            if folder not in known:
                c.execute("INSERT INTO discs(folder,kind,size,status) VALUES(?,?,?,'found')", (folder, d['kind'], d['size']))
            else:
                c.execute('UPDATE discs SET size=? WHERE folder=?', (d['size'], folder))
        for folder, r in known.items():           # folder is gone (converted or deleted) and nothing is running any more
            if folder not in found and r['status'] in ('found', 'queued'):
                c.execute('DELETE FROM discs WHERE folder=?', (folder,))
    return len(found)


def replan():
    """Recalculate plans from the stored data (after a change to the planning rules; no ffprobe needed)."""
    rows = db.q("SELECT id, rel, category, codec, width, height, hdr, dv, duration, vbr, ours, plan FROM files "
                "WHERE gone=0 AND plan IN ('encode','efficient','small-gain','unknown')")
    upd = []
    for r in rows:
        s = dict(codec=r['codec'], width=r['width'] or 0, height=r['height'] or 0, hdr=r['hdr'] or 0, dv=r['dv'] or 0,
                 duration=r['duration'] or 0, vbr=r['vbr'] or 0, ours=r['ours'] or 0)
        pl, save = plan(s, r['category'], r['rel'])
        if pl != r['plan']: upd.append((pl, save, r['id']))
        elif pl == 'encode': upd.append((pl, save, r['id']))
    with db.tx() as c:
        c.executemany('UPDATE files SET plan=?, est_save=? WHERE id=?', upd)
        # queued encodes (not picked by hand) that now fall below the minimum bitrate: skip instead of trying
        c.execute("UPDATE jobs SET status='skipped', message='bitrate already near the minimum' WHERE kind='encode' AND "
                  "status='queued' AND priority=0 AND file_id IN (SELECT id FROM files WHERE plan IN ('efficient','small-gain'))")
        # and the other way round: the rule was relaxed again, so those files are worth encoding after all
        c.execute("UPDATE jobs SET status='queued', message=NULL WHERE kind='encode' AND status='skipped' AND "
                  "message='bitrate already near the minimum' AND file_id IN (SELECT id FROM files WHERE gone=0 AND plan='encode')")
    return sum(1 for u in upd if u[0] == 'encode')
