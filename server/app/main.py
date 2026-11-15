"""MediaScan server: dashboard API, worker API and background tasks (scanning, replacing, quarantine)."""
import os, re, json, time, shutil, secrets, threading, asyncio, base64, hmac, hashlib, struct, contextlib, platform, urllib.request
from fastapi import FastAPI, Request, HTTPException, Response, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.concurrency import run_in_threadpool
from . import db, scanner
from .langs import NAME_NL, NAME_EN

MEDIA = scanner.MEDIA
WORK = os.path.join(MEDIA, '_MediaScan')
STAGING = os.path.join(WORK, 'staging')
QUAR = os.path.join(WORK, 'quarantine')
QUAR_OLD = os.path.join(WORK, 'quarantaine')                 # name before 1.0: renamed once at start (_migrate_quarantine_dir)
ENV_PASSWORD = os.environ.get('ADMIN_PASSWORD', '')          # optional; without it: choose a password the first time
ENV_WORKER_TOKEN = os.environ.get('WORKER_TOKEN', '')       # optional; without it: the server creates one itself
SETUP_CODE = secrets.token_hex(4).upper()                   # only needed while there is no password yet (printed in the log)
STALE_SEC = 600
SESSION_DAYS = 30                                            # longest session lifetime
SESSION_IDLE = float(os.environ.get('SESSION_IDLE_HOURS', '72')) * 3600   # idle this long: sign in again
TRUST_PROXY = os.environ.get('TRUST_PROXY', '1') == '1'      # trust X-Forwarded-Proto from a reverse proxy
HERE = os.path.dirname(__file__)

@contextlib.asynccontextmanager
async def lifespan(_app):
    startup(); yield


APP_VERSION = '1.0.0'
app = FastAPI(title='MediaScan', version=APP_VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1000)


def _https(request):
    """Is the dashboard served over https (e.g. the NAS reverse proxy)? Then cookies get the Secure flag."""
    return request.url.scheme == 'https' or (TRUST_PROXY and request.headers.get('x-forwarded-proto', '') == 'https')


def _set_session(resp, request, tok):
    resp.set_cookie('ms_session', tok, httponly=True, samesite='strict', secure=_https(request),
                    max_age=SESSION_DAYS * 86400, path='/')


@app.middleware('http')
async def security(request: Request, call_next):
    # CSRF: state-changing browser requests must send our own header and JSON (another site cannot)
    path = request.scope.get('path', '')
    if request.method not in ('GET', 'HEAD') and path.startswith('/api/') and not path.startswith('/api/worker/'):
        if request.headers.get('x-ms') != '1' or not request.headers.get('content-type', '').startswith('application/json'):
            return JSONResponse({'detail': 'request refused'}, status_code=403)
    resp = await call_next(request)
    if _https(request): resp.headers['Strict-Transport-Security'] = 'max-age=31536000'
    resp.headers['X-Frame-Options'] = 'DENY'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    if '/img/' not in path:                        # comparison frames may stay cached in the browser
        resp.headers['Cache-Control'] = 'no-store' if path.startswith('/api/') else ('public, max-age=86400' if path.startswith('/static/') else 'no-cache')
    resp.headers['Content-Security-Policy'] = (
        "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; font-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'")
    return resp


# ------------------------------------------------------------------ authentication: password + TOTP
def totp_code(secret, t=None, step=30):
    key = base64.b32decode(secret + '=' * (-len(secret) % 8))
    h = hmac.new(key, struct.pack('>Q', int((t or time.time()) // step)), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    return '%06d' % ((struct.unpack('>I', h[o:o + 4])[0] & 0x7FFFFFFF) % 1000000)


def totp_ok(secret, code):
    code = re.sub(r'\D', '', str(code))
    if len(code) != 6 or not secret: return False
    now = time.time(); last = db.auth_get('totp_last') or ''
    for d in (-30, 0, 30):
        if secrets.compare_digest(totp_code(secret, now + d), code):
            slot = str(int((now + d) // 30))
            if slot == last: return False            # never accept the same code twice
            db.auth_set('totp_last', slot); return True
    return False


def _rnorm(code): return re.sub(r'[^0-9a-f]', '', str(code).lower())


def _rhash(code):
    """Store a recovery code as a scrypt hash with its own salt (slow to crack if the database ever leaks)."""
    salt = secrets.token_bytes(8)
    return 's$' + salt.hex() + '$' + hashlib.scrypt(_rnorm(code).encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=16).hex()


def _rmatch(code, h):
    if h.startswith('s$'):
        _, salt, v = h.split('$')
        return secrets.compare_digest(hashlib.scrypt(_rnorm(code).encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=16).hex(), v)
    return secrets.compare_digest(hashlib.sha256(('ms:' + _rnorm(code)).encode()).hexdigest(), h)     # older codes


def use_recovery(code):
    if len(_rnorm(code)) != 12: return False
    codes = json.loads(db.auth_get('recovery') or '[]')
    for h in codes:
        if _rmatch(code, h):
            codes.remove(h); db.auth_set('recovery', json.dumps(codes))
            db.event(f'Recovery code used to sign in ({len(codes)} left)', 'warn'); return True
    return False


def mfa_enabled(): return db.auth_get('totp_enabled') == '1'


def _pw_hash(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return 'scrypt$' + base64.b64encode(salt).decode() + '$' + base64.b64encode(h).decode()


def password_set(): return bool(db.auth_get('pw_hash') or ENV_PASSWORD)


def password_ok(pw):
    pw = str(pw or '')
    stored = db.auth_get('pw_hash')
    if stored:
        try:
            _, salt, _h = stored.split('$')
            return secrets.compare_digest(_pw_hash(pw, base64.b64decode(salt)), stored)
        except ValueError:
            return False
    return bool(ENV_PASSWORD) and secrets.compare_digest(pw.encode(), ENV_PASSWORD.encode())


def worker_token():
    if ENV_WORKER_TOKEN: return ENV_WORKER_TOKEN
    t = db.auth_get('worker_token')
    if not t:
        t = secrets.token_urlsafe(32); db.auth_set('worker_token', t)
    return t


def plex_cfg():
    if db.setting('plex_off', bool): return '', ''
    url = (db.setting('plex_url') or os.environ.get('PLEX_URL', '')).rstrip('/')
    return url, db.auth_get('plex_token') or os.environ.get('PLEX_TOKEN', '')


def session(request):
    tok = request.cookies.get('ms_session')
    if not tok: return None
    now = time.time()
    s = db.q('SELECT * FROM sessions WHERE token=? AND created>? AND COALESCE(seen, created)>?',
             (db.token_hash(tok), now - SESSION_DAYS * 86400, now - SESSION_IDLE), one=True)
    if s and now - (s['seen'] or 0) > 300: db.x('UPDATE sessions SET seen=? WHERE token=?', (now, s['token']))
    return s


def require_user(request: Request):
    s = session(request)
    if not s: raise HTTPException(401, 'not signed in')
    if not s['mfa']: raise HTTPException(403, 'mfa_setup' if not mfa_enabled() else 'mfa_required')


def _clean(v, depth=0):
    """Clean data from a worker before it is stored or shown: no HTML characters or control characters,
    limited length and depth. Numbers stay numbers."""
    if isinstance(v, bool) or v is None: return v
    if isinstance(v, (int, float)): return v if v == v and abs(v) < 1e18 else 0
    if isinstance(v, str): return re.sub(r'[<>\x00-\x08\x0b-\x1f\x7f]', '', v)[:1000]
    if depth >= 5: return None
    if isinstance(v, list): return [_clean(x, depth + 1) for x in v[:300]]
    if isinstance(v, dict): return {str(k)[:40]: _clean(x, depth + 1) for k, x in list(v.items())[:100] if re.match(r'^[\w.-]+$', str(k))}
    return None


async def jbody(request: Request):
    """Body of a dashboard request: a JSON object (400 instead of 500 when it is not)."""
    try: b = await request.json()
    except ValueError: raise HTTPException(400, 'not JSON') from None
    if not isinstance(b, dict): raise HTTPException(400, 'expected a JSON object')
    return b


async def wbody(request: Request):
    """Body of a worker request: a JSON object, cleaned, with a valid name if one is sent."""
    try: b = await request.json()
    except ValueError: raise HTTPException(400, 'not JSON')
    if not isinstance(b, dict): raise HTTPException(400, 'expected a JSON object')
    b = _clean(b)
    if b.get('name') not in (None, ''):          # same rules as the worker: odd characters become a dash
        b['name'] = re.sub(r'[^A-Za-z0-9._ -]', '-', str(b['name'])).strip(' ._-')[:64] or 'worker'
    return b


def require_worker(request: Request):
    if not secrets.compare_digest(request.headers.get('authorization', '').encode(), ('Bearer ' + worker_token()).encode()):
        raise HTTPException(401, 'invalid worker token')


def new_session(mfa):
    tok = secrets.token_urlsafe(32)
    db.x('INSERT INTO sessions(token,created,mfa,seen) VALUES(?,?,?,?)', (db.token_hash(tok), time.time(), mfa, time.time()))
    db.x('DELETE FROM sessions WHERE created<?', (time.time() - SESSION_DAYS * 86400,))
    return tok


_login_lock = asyncio.Lock()          # attempts one at a time: guessing in parallel does not help


def _limited(ip):
    db.x('DELETE FROM login_fail WHERE ts<?', (time.time() - 900,))
    if db.q('SELECT COUNT(*) n FROM login_fail WHERE ip=?', (ip,), one=True)['n'] >= 8 or \
            db.q('SELECT COUNT(*) n FROM login_fail', one=True)['n'] >= 200:
        raise HTTPException(429, 'Too many attempts - try again in 15 minutes')


@app.post('/api/login')
async def login(request: Request):
    async with _login_lock:
        return await _login(request)


async def _login(request):
    ip = request.client.host if request.client else '?'
    _limited(ip)
    body = await request.json()
    async def fail(msg):
        db.x('INSERT INTO login_fail(ip,ts) VALUES(?,?)', (ip, time.time()))
        await asyncio.sleep(1)                         # slows down guessing without blocking the server
        raise HTTPException(401, msg)
    if not password_set(): raise HTTPException(409, 'setup')
    if not await run_in_threadpool(password_ok, body.get('password')):
        await fail('Incorrect password')
    mfa = 0
    if mfa_enabled():
        code = str(body.get('code') or '').strip()
        if not code: return {'need_code': True}
        if not (totp_ok(db.auth_get('totp_secret'), code) or use_recovery(code)):
            await fail('Incorrect code')
        mfa = 1
    db.x('DELETE FROM login_fail WHERE ip=?', (ip,))
    r = JSONResponse({'ok': True, 'mfa_setup': not mfa_enabled()})
    _set_session(r, request, new_session(mfa))
    return r


@app.get('/api/setup')
def setup_state():
    """First run: is there a password yet? (can be asked without signing in; reveals nothing else)"""
    return {'setup': not password_set()}


@app.post('/api/setup')
async def setup_password(request: Request):
    """Choose the first password. Only possible while there is none, and with the code from the container log."""
    ip = request.client.host if request.client else '?'
    _limited(ip)
    if password_set(): raise HTTPException(409, 'A password has already been set')
    b = await request.json(); pw = str(b.get('password') or '')
    if not secrets.compare_digest(re.sub(r'[^0-9A-F]', '', str(b.get('code', '')).upper()).encode(), SETUP_CODE.encode()):
        db.x('INSERT INTO login_fail(ip,ts) VALUES(?,?)', (ip, time.time())); await asyncio.sleep(1)
        raise HTTPException(400, 'Setup code is wrong - check the container log')
    if len(pw) < 10: raise HTTPException(400, 'Choose a password of at least 10 characters')
    db.auth_set('pw_hash', _pw_hash(pw)); db.event('Password set (first time)', 'warn')
    r = JSONResponse({'ok': True, 'mfa_setup': True})
    _set_session(r, request, new_session(0))
    return r


@app.get('/api/mfa/setup')
def mfa_setup(request: Request):
    if not session(request): raise HTTPException(401)
    if mfa_enabled(): raise HTTPException(400, 'MFA is already enabled')
    sec = db.auth_get('totp_pending')
    if not sec:
        sec = base64.b32encode(secrets.token_bytes(20)).decode().rstrip('=')
        db.auth_set('totp_pending', sec)
    return {'secret': sec, 'uri': f'otpauth://totp/MediaScan:admin?secret={sec}&issuer=MediaScan&digits=6&period=30'}


@app.post('/api/mfa/enable')
async def mfa_enable(request: Request):
    s = session(request)
    if not s: raise HTTPException(401)
    if mfa_enabled(): raise HTTPException(400, 'MFA is already enabled')
    sec = db.auth_get('totp_pending'); body = await request.json()
    if not sec or not totp_ok(sec, body.get('code', '')):
        raise HTTPException(400, 'Code is incorrect - check the time on your phone')
    codes = ['-'.join(secrets.token_hex(2) for _ in range(3)) for _ in range(8)]
    db.auth_set('totp_secret', sec); db.auth_set('totp_pending', None); db.auth_set('totp_enabled', '1')
    db.auth_set('recovery', json.dumps([_rhash(c) for c in codes]))
    db.x('DELETE FROM sessions WHERE token!=?', (s['token'],))
    db.x('UPDATE sessions SET mfa=1 WHERE token=?', (s['token'],))
    db.event('MFA enabled', 'warn')
    return {'ok': True, 'recovery': codes}


@app.post('/api/logout')
def logout(request: Request):
    db.x('DELETE FROM sessions WHERE token=?', (db.token_hash(request.cookies.get('ms_session', '')),))
    r = JSONResponse({'ok': True}); r.delete_cookie('ms_session', path='/'); return r


@app.get('/api/account', dependencies=[Depends(require_user)])
def account():
    return {'mfa': mfa_enabled(), 'recovery_left': len(json.loads(db.auth_get('recovery') or '[]')),
            'sessions': db.q('SELECT COUNT(*) n FROM sessions WHERE created>?', (time.time() - SESSION_DAYS * 86400,), one=True)['n']}


async def _verify_pw_code(request):
    ip = request.client.host if request.client else '?'
    async with _login_lock:
        _limited(ip)
        b = await request.json()
        err = 'Incorrect password' if not await run_in_threadpool(password_ok, b.get('password')) else \
            'Incorrect code from your authenticator app' if not totp_ok(db.auth_get('totp_secret'), b.get('code', '')) else None
        if err:
            db.x('INSERT INTO login_fail(ip,ts) VALUES(?,?)', (ip, time.time())); await asyncio.sleep(1)
            raise HTTPException(400, err)
    return b


@app.post('/api/account/mfa-reset', dependencies=[Depends(require_user)])
async def account_mfa_reset(request: Request):
    await _verify_pw_code(request)
    for k in ('totp_enabled', 'totp_secret', 'totp_pending', 'recovery'): db.auth_set(k, None)
    tok = db.token_hash(request.cookies.get('ms_session', ''))
    db.x('DELETE FROM sessions WHERE token!=?', (tok,))
    db.x('UPDATE sessions SET mfa=0 WHERE token=?', (tok,))          # this session may only set up MFA
    db.event('MFA reset from settings - set it up again', 'warn')
    return {'ok': True}


@app.post('/api/account/recovery', dependencies=[Depends(require_user)])
async def account_recovery(request: Request):
    await _verify_pw_code(request)
    codes = ['-'.join(secrets.token_hex(2) for _ in range(3)) for _ in range(8)]
    db.auth_set('recovery', json.dumps([_rhash(c) for c in codes]))
    db.event('New recovery codes created', 'warn')
    return {'ok': True, 'recovery': codes}


@app.post('/api/account/password', dependencies=[Depends(require_user)])
async def account_password(request: Request):
    b = await _verify_pw_code(request); new = str(b.get('new') or '')
    if len(new) < 10: raise HTTPException(400, 'Choose a password of at least 10 characters')
    db.auth_set('pw_hash', _pw_hash(new))
    tok = db.token_hash(request.cookies.get('ms_session', ''))
    db.x('DELETE FROM sessions WHERE token!=?', (tok,))
    db.event('Password changed; other sessions signed out', 'warn')
    return {'ok': True}


@app.post('/api/account/worker-token', dependencies=[Depends(require_user)])
async def account_worker_token(request: Request):
    """New worker key: all workers then need to be given the new key."""
    await _verify_pw_code(request)
    if ENV_WORKER_TOKEN: raise HTTPException(400, 'The key is fixed in WORKER_TOKEN (container environment)')
    db.auth_set('worker_token', secrets.token_urlsafe(32)); db.event('New worker key created', 'warn')
    return {'ok': True}


@app.post('/api/account/logout-others', dependencies=[Depends(require_user)])
def account_logout_others(request: Request):
    tok = db.token_hash(request.cookies.get('ms_session', ''))
    n = db.q('SELECT COUNT(*) n FROM sessions WHERE token!=?', (tok,), one=True)['n']
    db.x('DELETE FROM sessions WHERE token!=?', (tok,))
    db.event(f'Signed out of {n} other session(s)', 'warn')
    return {'ok': True, 'n': n}


@app.get('/api/worker-setup', dependencies=[Depends(require_user)])
def worker_setup():
    """Details for connecting an extra worker (only visible after signing in with MFA)."""
    return {'token': worker_token(), 'token_fixed': bool(ENV_WORKER_TOKEN), 'version': _worker_version(),
            'downloads': [{'name': n, 'file': f'/api/download/{f}'} for n, f in DOWNLOADS.items()
                          if os.path.exists(os.path.join(DIST, 'downloads', f))]}


# ------------------------------------------------------------------ worker packages and automatic updates
DIST = os.environ.get('DIST_DIR', os.path.join(HERE, 'dist'))          # filled at build time (tools/build_packages.py)
DOWNLOADS = {'Windows': 'mediascan-worker-windows.zip', 'Linux': 'mediascan-worker-linux.tar.gz', 'macOS': 'mediascan-worker-macos.tar.gz'}
WORKER_FILES = ('worker.py', 'subs.py', 'langs.py', 'manager.py', 'tray.ps1')
_wv = {'t': 0, 'v': None, 'files': {}}


def _worker_version():
    """Version and sha256 of the worker files shipped with this server (re-checked once a minute)."""
    if time.time() - _wv['t'] > 60:
        files, ver = {}, None
        for f in WORKER_FILES:
            p = os.path.join(DIST, 'worker', f)
            if os.path.exists(p):
                data = open(p, 'rb').read(); files[f] = hashlib.sha256(data).hexdigest()
                if f == 'worker.py':
                    m = re.search(rb"^WORKER_VERSION = '([^']+)'", data, re.M); ver = m.group(1).decode() if m else None
        _wv.update(t=time.time(), v=ver, files=files)
    return _wv['v']


@app.get('/api/download/{name}', dependencies=[Depends(require_user)])
def download(name: str):
    if name not in DOWNLOADS.values(): raise HTTPException(404)
    p = os.path.join(DIST, 'downloads', name)
    if not os.path.exists(p): raise HTTPException(404, 'package missing from this installation')
    return FileResponse(p, filename=name)


@app.post('/api/worker/update', dependencies=[Depends(require_worker)])
async def worker_update(request: Request):
    """Which worker version belongs to this server, with the sha256 of every file."""
    ver = _worker_version()
    return {'version': ver, 'files': _wv['files']} if ver else {'version': None}


@app.get('/api/worker/file/{name}', dependencies=[Depends(require_worker)])
def worker_file(name: str):
    if name not in WORKER_FILES: raise HTTPException(404)
    p = os.path.join(DIST, 'worker', name)
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type='text/plain' if name.endswith('.ps1') else 'text/x-python')


# ------------------------------------------------------------------ helpers
def row(r):
    d = dict(r)
    for k in ('result', 'subs_have'):
        if d.get(k):
            try: d[k] = json.loads(d[k])
            except ValueError: pass
    return d


def disk_free():
    try:
        u = shutil.disk_usage(MEDIA); return u.free, u.total
    except OSError:
        return 0, 0


def plex_get(path, timeout=10, cfg=None):
    url, tok = cfg or plex_cfg()
    req = urllib.request.Request(url + path, headers={'X-Plex-Token': tok, 'Accept': 'application/json'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else {}


def plex_playing():
    """File names currently playing in Plex. Empty = nothing / no Plex configured; None = Plex unreachable."""
    if not all(plex_cfg()): return set()
    try:
        names = set()
        for m in plex_get('/status/sessions').get('MediaContainer', {}).get('Metadata', []) or []:
            for md in m.get('Media', []) or []:
                for p in md.get('Part', []) or []:
                    if p.get('file'): names.add(os.path.basename(p['file'].replace('\\', '/')).lower())
        return names
    except Exception:
        return None


def plex_refresh():
    if not all(plex_cfg()): return
    try:
        for sec in plex_get('/library/sections').get('MediaContainer', {}).get('Directory', []) or []:
            plex_get(f"/library/sections/{sec['key']}/refresh")
    except Exception:
        pass


# ------------------------------------------------------------------ dashboard API
_ov_cache = {'t': 0, 'v': None}
_ov_lock = threading.Lock()


# ------------------------------------------------------------------ selection ('Process only these')
ACTIVE = ('queued', 'running', 'replacing')
SEL_KEEP = 30 * 60          # how long a finished film stays visible in the selection list


def _sel_ids(kind):
    r = db.q('SELECT value FROM settings WHERE key=?', (f'sel_{kind}',), one=True)
    try: return [int(i) for i in json.loads(r['value'])] if r else []
    except (ValueError, TypeError): return []


def _sel_active(ids):
    if not ids: return []
    act = {r['id'] for r in db.q('SELECT id FROM jobs WHERE id IN (%s) AND status IN (%s)' % (','.join('?' * len(ids)), ','.join('?' * len(ACTIVE))),
                                 list(ids) + list(ACTIVE))}
    return [i for i in ids if i in act]


def _sel_add(kind, new_ids):
    """New selection: finished films from a previous pick drop off, anything still running or waiting stays."""
    keep = _sel_active(_sel_ids(kind))
    ids = keep + [i for i in new_ids if i not in set(keep)]
    db.set_setting(f'sel_{kind}', json.dumps(ids[:5000]))


def _only_done(kind):
    db.set_setting(f'only_{kind}', 0)
    db.set_setting('paused' if kind == 'encode' else 'subs_paused', 1)
    db.event(f"Selection handed out - {'encoding' if kind == 'encode' else 'subtitles'} paused automatically")


def _sel_waiting(kind):
    if not db.setting(f'only_{kind}', bool): return 0
    ids = _sel_ids(kind)
    if not ids: return 0
    return db.q("SELECT COUNT(*) n FROM jobs WHERE status='queued' AND id IN (%s)" % ','.join('?' * len(ids)), ids, one=True)['n']


def _selection():
    out = {}
    for kind in ('encode', 'subs'):
        ids = _sel_ids(kind)
        if not ids: continue
        ids = ids + [r['id'] for r in db.q("SELECT id FROM jobs WHERE kind=? AND priority>0 AND status IN ('running','replacing')", (kind,)) if r['id'] not in ids]
        rows = db.q('SELECT j.id, j.file_id, j.status, j.progress, j.stage, j.message, j.result, j.src_size, j.out_size, j.priority, j.finished, f.rel '
                    'FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.id IN (%s) ORDER BY j.priority DESC, j.id' % ','.join('?' * len(ids)), ids)
        items = []
        for r in rows:
            d = row(r); res = d.pop('result', None) or {}
            if isinstance(res, dict):
                d['vmaf'] = res.get('vmaf_mean'); d['reason'] = res.get('reason')
                d['made'] = [m.get('lang') for m in res.get('made', [])] if kind == 'subs' else None
            items.append(d)
        # everything done and nothing has happened for half an hour: close the list automatically
        if items and all(i['status'] not in ACTIVE for i in items) and max(i['finished'] or 0 for i in items) < time.time() - SEL_KEEP:
            db.set_setting(f'sel_{kind}', '[]'); continue
        out[kind] = items
    return out


@app.post('/api/selection/clear', dependencies=[Depends(require_user)])
async def selection_clear(request: Request):
    """Close the list (display only; jobs and priority stay as they are). {kind: 'encode'|'subs'} or both."""
    try: kind = (await request.json()).get('kind')
    except Exception: kind = None
    for k in ([kind] if kind in ('encode', 'subs') else ['encode', 'subs']):
        if not _sel_waiting(k) and not db.q("SELECT 1 FROM jobs WHERE kind=? AND priority>0 AND status IN ('running','replacing') LIMIT 1", (k,), one=True):
            db.set_setting(f'sel_{k}', '[]')
        else:
            raise HTTPException(400, 'selection still running - pause or wait until it is done')
    _ov_cache['t'] = 0
    return {'ok': True}


@app.get('/api/overview', dependencies=[Depends(require_user)])
def overview():
    with _ov_lock:                                   # several tabs: at most 1 calculation per 2 s
        if time.time() - _ov_cache['t'] < 2 and _ov_cache['v']:
            return _ov_cache['v']
        v = _overview(); _ov_cache.update(t=time.time(), v=v); return v


def _overview():
    lib = db.q('SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM files WHERE gone=0', one=True)
    counts = {}
    for r in db.q('SELECT kind, status, COUNT(*) n FROM jobs GROUP BY kind, status'):
        counts.setdefault(r['kind'], {})[r['status']] = r['n']
    sv = db.q("SELECT COALESCE(SUM(src_size-out_size),0) saved, COALESCE(SUM(src_size),0) src FROM jobs WHERE kind='encode' AND status='done'", one=True)
    rem = db.q("SELECT COALESCE(SUM(f.size),0) s, COALESCE(SUM(f.est_save),0) e FROM jobs j JOIN files f ON f.id=j.file_id "
               "WHERE j.kind='encode' AND j.status='queued'", one=True)
    thr = db.q("SELECT COALESCE(SUM(src_size),0) s FROM jobs WHERE kind='encode' AND status IN ('done','no-gain') AND finished>?",
               (time.time() - 86400,), one=True)['s']
    free, total = disk_free()
    workers = []
    for w in db.q('SELECT name, last_seen, info FROM workers ORDER BY name'):
        c = worker_cfg(w['name'])
        info = json.loads(w['info'] or '{}')
        workers.append({'name': w['name'], 'last_seen': w['last_seen'], 'online': time.time() - w['last_seen'] < 120, 'info': info,
                        'cfg': {'encode': c['encode'], 'subs': c['subs'], 'cpu': c['cpu'], 'max_height': c['max_height']},
                        'update': _worker_update(w['name'], info)})
    quar = db.q("SELECT COUNT(*) n, COALESCE(SUM(size),0) s FROM quarantine WHERE state='held'", one=True)
    return {
        'library': {'files': lib['n'], 'size': lib['s'],
                    'categories': [dict(r) for r in db.q('SELECT category, COUNT(*) n, SUM(size) s FROM files WHERE gone=0 GROUP BY category ORDER BY s DESC')],
                    'codecs': [r['codec'] for r in db.q("SELECT codec FROM files WHERE gone=0 AND codec IS NOT NULL AND codec!='' GROUP BY codec ORDER BY COUNT(*) DESC")]},
        'jobs': counts, 'saved': sv['saved'], 'done_src': sv['src'], 'remaining_src': rem['s'], 'remaining_est_save': rem['e'],
        'throughput_24h': thr, 'throughput_bps': thr / 86400,            # source bytes processed per day / per second (last 24 h)
        'eta_sec': (rem['s'] / thr * 86400) if thr else None, 'disk': {'free': free, 'total': total},
        'paused': db.setting('paused', bool), 'subs_paused': db.setting('subs_paused', bool), 'scan': dict(scanner.state),
        'workers': workers, 'quarantine': {'n': quar['n'], 'size': quar['s']},
        'running': [row(r) for r in db.q("SELECT j.id, j.file_id, j.kind, j.cpu, j.status, j.stage, j.message, j.progress, j.worker, f.rel FROM jobs j JOIN files f ON f.id=j.file_id "
                                          "WHERE j.status IN ('running','replacing') ORDER BY j.started")],
        'events': [dict(r) for r in db.q('SELECT ts, level, msg FROM events ORDER BY id DESC LIMIT 40')],
        'newest': [dict(r) for r in db.q('SELECT rel, first_seen FROM files WHERE gone=0 ORDER BY first_seen DESC LIMIT 8')],
        'auto_paused_disk': bool(free) and free < db.setting('min_free_gb', float) * 1e9,
        'langs': db.setting('langs'), 'setup_done': db.setting('setup_done', bool), 'version': APP_VERSION,
        'sel_waiting': _sel_waiting('encode') + _sel_waiting('subs'),
        'selection': _selection(),
        'norm': {'min_mean': db.setting('min_mean', float), 'min_p5': db.setting('min_p5', float)},
        'discs': {r['status']: r['n'] for r in db.q('SELECT status, COUNT(*) n FROM discs GROUP BY status')},
        'cpu_waiting': db.q("SELECT COUNT(*) n FROM jobs WHERE kind='encode' AND status='queued' AND cpu=1", one=True)['n'],
        'cpu_worker': _cpu_worker_available(),
        'discs_running': _disc_running(),
        'daily': [dict(r) for r in db.q("SELECT date(finished,'unixepoch','localtime') d, SUM(src_size-out_size) saved, COUNT(*) n FROM jobs "
                                        "WHERE kind='encode' AND status='done' AND finished>? GROUP BY d ORDER BY d", (time.time() - 14 * 86400,))],
    }


FINISHED = {'done', 'no-gain', 'failed', 'not-found', 'restored', 'skipped', 'cancelled'}
STATUSES = FINISHED | {'queued', 'running', 'replacing'}


def _statuses(status):
    """?status=a,b -> known status codes."""
    return [c for c in str(status).split(',') if c in STATUSES]


@app.get('/api/events', dependencies=[Depends(require_user)])
def events(before: int = 0, limit: int = 100, level: str = '', q: str = ''):
    """The activity log, newest first; before=<id> gives the page before that entry. level=warn shows warnings and
    errors, level=error only errors; q = text to search for (case-insensitive)."""
    limit = max(1, min(limit, 500))
    where, args = [], []
    if before > 0: where.append('id<?'); args.append(before)
    if level == 'warn': where.append("level IN ('warn','error')")
    elif level == 'error': where.append("level='error'")
    if q.strip(): where.append("instr(lower(msg), lower(?))>0"); args.append(q.strip()[:200])
    w = ('WHERE ' + ' AND '.join(where)) if where else ''
    rows = [dict(r) for r in db.q(f'SELECT id, ts, level, msg FROM events {w} ORDER BY id DESC LIMIT ?', (*args, limit + 1))]
    return {'rows': rows[:limit], 'more': len(rows) > limit}


@app.get('/api/jobs', dependencies=[Depends(require_user)])
def jobs(kind: str = 'encode', status: str = '', q: str = '', limit: int = 100, offset: int = 0, sort: str = '', dir: str = 'desc',
         cat: str = '', res: str = '', codec: str = ''):
    if kind not in ('encode', 'subs'): raise HTTPException(400)
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    w, args = _job_where(kind, status, q, cat, res, codec)
    st = _statuses(status)
    default = 'j.finished DESC' if st and set(st) <= FINISHED else 'j.priority DESC, f.est_save DESC, j.id'
    order = _order(sort, dir, {'name': 'f.rel', 'size': 'f.size', 'vbr': 'f.vbr', 'status': 'j.status', 'finished': 'j.finished',
                               'result': 'COALESCE(j.src_size-j.out_size, f.est_save)', 'saving': 'f.est_save', 'attempts': 'j.attempts'}, default)
    total = db.q(f'SELECT COUNT(*) n FROM jobs j JOIN files f ON f.id=j.file_id WHERE {w}', args, one=True)['n']
    cols = 'j.*, f.rel, f.size, f.codec, f.width, f.height, f.hdr, f.dv, f.vbr, f.est_save, f.subs_have'
    rows = [row(r) for r in db.q(f'SELECT {cols} FROM jobs j JOIN files f ON f.id=j.file_id WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?', (*args, limit, offset))]
    if st == ['queued'] and rows:
        # position in the real hand-out order (GPU and CPU queues separately) and the bytes of the jobs before it; one
        # walk over the queue in Python, a window function joined back per row made SQLite rescan the whole queue per row
        want = {r['id']: r for r in rows}; pos = {}; ahead = {}
        for jid, cpu, size in db.q("SELECT j.id, COALESCE(j.cpu,0), COALESCE(f.size,0) FROM jobs j JOIN files f ON f.id=j.file_id "
                                   "WHERE j.kind=? AND j.status='queued' AND f.gone=0 ORDER BY j.priority DESC, f.est_save DESC, j.id", (kind,)):
            pos[cpu] = pos.get(cpu, 0) + 1
            if jid in want: want[jid].update(queue_pos=pos[cpu], ahead_bytes=ahead.get(cpu, 0))
            ahead[cpu] = ahead.get(cpu, 0) + size
    return {'total': total, 'rows': rows}


@app.get('/api/file/{fid}', dependencies=[Depends(require_user)])
def file_detail(fid: int):
    f = db.q('SELECT * FROM files WHERE id=?', (fid,), one=True)
    if not f: raise HTTPException(404)
    d = row(f)
    try: d['audio'] = json.loads(d.get('audio') or '[]')
    except ValueError: pass
    d['jobs'] = [row(r) for r in db.q('SELECT id, kind, cpu, status, stage, message, progress, attempts, worker, created, started, finished, result, '
                                         'src_size, out_size FROM jobs WHERE file_id=? ORDER BY id DESC', (fid,))]
    d['quarantine'] = [dict(r) for r in db.q("SELECT id, created, state, size FROM quarantine WHERE file_id=? ORDER BY id DESC", (fid,))]
    sm = {k: f[k] for k in ('codec', 'width', 'height', 'hdr', 'dv', 'duration', 'vbr', 'ours')}
    try: d['plan_reason'] = scanner.plan_reason(sm, f['category'], f['rel'] or '', f['plan'], f['probe_err'])
    except Exception: d['plan_reason'] = None
    return d


@app.post('/api/jobs/{jid}/{action}', dependencies=[Depends(require_user)])
def job_action(jid: int, action: str):
    j = db.q('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
    if not j: raise HTTPException(404)
    retry_from = "('failed','no-gain','skipped','not-found','cancelled','restored')"
    if action == 'retry' and j['status'] in ('failed', 'no-gain', 'skipped', 'not-found', 'cancelled', 'restored'):
        n = db.xn(f"UPDATE jobs SET status='queued', cpu=0, message=NULL, progress=0, not_before=0, attempts=0, finished=NULL WHERE id=? AND status IN {retry_from}", (jid,))
        if n and j['kind'] == 'encode': db.x("UPDATE files SET plan='encode' WHERE id=? AND plan IN ('restored','no-gain')", (j['file_id'],))
    elif action == 'retry-cpu' and j['kind'] == 'encode' and j['status'] in ('failed', 'no-gain', 'skipped', 'cancelled', 'restored'):
        if not _cpu_worker_available(): raise HTTPException(409, 'No worker has CPU encodes switched on (Workers page)')
        n = db.xn("UPDATE jobs SET status='queued', cpu=1, message=NULL, progress=0, not_before=0, attempts=0, finished=NULL, "
                  f"priority=(SELECT COALESCE(MAX(priority),0)+1 FROM jobs) WHERE id=? AND status IN {retry_from}", (jid,))
        if n: db.x("UPDATE files SET plan='encode' WHERE id=? AND plan IN ('restored','no-gain')", (j['file_id'],))
    elif action == 'prioritize' and j['status'] == 'queued':
        n = db.xn("UPDATE jobs SET priority=(SELECT COALESCE(MAX(priority),0)+1 FROM jobs) WHERE id=? AND status='queued'", (jid,))
    elif action == 'skip' and j['status'] == 'queued':
        n = db.xn("UPDATE jobs SET status='skipped', finished=? WHERE id=? AND status='queued'", (time.time(), jid))
    else:
        raise HTTPException(400, 'action not possible in this status')
    if not n: raise HTTPException(409, 'the job changed in the meantime')     # a worker picked it up a moment ago
    f = db.q('SELECT rel FROM files WHERE id=?', (j['file_id'],), one=True)
    db.event(f"{ACTION_LABEL[action]}: {_base(f['rel']) if f else jid}"); _ov_cache['t'] = 0
    return {'ok': True}


ACTION_LABEL = {'retry': 'Retry', 'retry-cpu': 'Retry on CPU', 'prioritize': 'Prioritise', 'skip': 'Skip', 'only': 'Process only these'}


def _base(rel):
    """File name without its folders, for the activity log (the folder is in the file's own page)."""
    return os.path.basename(str(rel or '').rstrip('/')) or str(rel or '')


BULK = {  # action -> (allowed current statuses, SQL update)
    'prioritize': ({'queued'}, None),
    'only': ({'queued'}, None),     # priority + process only these, then pause automatically
    'skip': ({'queued'}, "status='skipped', finished=:now"),
    'retry': ({'failed', 'no-gain', 'skipped', 'not-found', 'cancelled', 'restored'},   # restored: encode again from the original
              "status='queued', cpu=0, message=NULL, progress=0, not_before=0, attempts=0, finished=NULL"),
    'retry-cpu': ({'failed', 'no-gain', 'skipped', 'cancelled', 'restored'},            # once more, on a worker CPU with x265
                  "status='queued', cpu=1, message=NULL, progress=0, not_before=0, attempts=0, finished=NULL"),
}


RES_WHERE = {'4K': '({p}width>=3200 OR {p}height>=1700)',
             '1080p': '(({p}width>=1700 OR {p}height>=950) AND NOT ({p}width>=3200 OR {p}height>=1700))',
             '720p': '(({p}width>=1100 OR {p}height>=650) AND NOT ({p}width>=1700 OR {p}height>=950))',
             'SD': '({p}width<1100 AND {p}height<650)'}     # same classes as scanner.rescls


def _file_filters(cat, res, codec, p=''):
    """Extra WHERE parts for the folder / resolution / codec filters of the lists (p = table alias prefix)."""
    where, args = [], []
    if cat: where.append(f'{p}category=?'); args.append(cat[:200])
    if res in RES_WHERE: where.append(RES_WHERE[res].format(p=p))
    if codec: where.append(f'{p}codec=?'); args.append(codec[:20].lower())
    return where, args


def _order(sort, direction, keys, default):
    """ORDER BY for a list: sort = column key, direction = asc|desc (keys: key -> SQL expression)."""
    expr = keys.get(sort)
    if not expr: return default
    d = 'ASC' if direction == 'asc' else 'DESC'
    return f'{expr} {d} NULLS LAST, {default}' if d == 'DESC' else f'{expr} {d}, {default}'


def _job_where(kind, status, q, cat='', res='', codec=''):
    where, args = ['j.kind=?'], [kind]
    st = _statuses(status)
    if st: where.append('j.status IN (%s)' % ','.join('?' * len(st))); args += st
    if q: w, a = _q_like('f.rel', q); where.append(w); args += a
    w2, a2 = _file_filters(cat, res, codec, 'f.'); where += w2; args += a2
    return ' AND '.join(where), args


def _q_like(col, q):
    """Search term -> SQL. Separate several terms with | (e.g. 'Iron_Man.mkv|Spirited Away') = any of these."""
    terms = [t.strip() for t in q.split('|') if t.strip()][:50] or [q]
    like = lambda t: '%' + re.sub(r'([%_\\])', r'\\\1', t) + '%'
    return '(' + ' OR '.join(f"{col} LIKE ? ESCAPE '\\'" for _ in terms) + ')', [like(t) for t in terms]


@app.post('/api/jobs/bulk', dependencies=[Depends(require_user)])
def jobs_bulk(b: dict = Depends(jbody)):
    """Action on several jobs: {action, ids:[...]} or {action, all:true, kind, status, q, cat, res, codec} (all results of a filter)."""
    action = b.get('action')
    if action not in BULK: raise HTTPException(400, 'unknown action')
    if action == 'retry-cpu' and not _cpu_worker_available(): raise HTTPException(409, 'No worker has CPU encodes switched on (Workers page)')
    allowed, upd = BULK[action]
    if b.get('all'):
        if b.get('kind') not in ('encode', 'subs') or (action == 'retry-cpu' and b['kind'] != 'encode'): raise HTTPException(400)
        w, args = _job_where(b['kind'], str(b.get('status', '')), str(b.get('q', '')), str(b.get('cat', '')), str(b.get('res', '')), str(b.get('codec', '')))
        rows = db.q(f'SELECT j.id, j.status FROM jobs j JOIN files f ON f.id=j.file_id WHERE {w} ORDER BY j.priority DESC, f.est_save DESC, j.id', args)
    else:
        ids = [int(i) for i in (b.get('ids') or [])][:20000]
        if not ids: return {'ok': True, 'n': 0}
        found = {r['id']: r for r in db.q('SELECT id, status FROM jobs WHERE id IN (%s)' % ','.join('?' * len(ids)) + (" AND kind='encode'" if action == 'retry-cpu' else ''), ids)}
        rows = [found[i] for i in ids if i in found]
    sel = [r['id'] for r in rows if r['status'] in allowed]
    if sel:
        now = time.time()
        with db.tx() as c:
            if action in ('prioritize', 'only'):   # keep the relative order: the first selected gets the highest priority
                top = c.execute('SELECT COALESCE(MAX(priority),0) FROM jobs').fetchone()[0] + len(sel)
                c.executemany('UPDATE jobs SET priority=? WHERE id=?', [(top - i, jid) for i, jid in enumerate(sel)])
            else:
                c.executemany(f'UPDATE jobs SET {upd.replace(":now", "?")} WHERE id=?',
                              [((now, jid) if ':now' in upd else (jid,)) for jid in sel])
        if action in ('retry', 'retry-cpu'):         # the file shows up as planned work again
            db.x("UPDATE files SET plan='encode' WHERE plan IN ('restored','no-gain') AND id IN "
                 "(SELECT file_id FROM jobs WHERE kind='encode' AND id IN (%s))" % ','.join('?' * len(sel)), sel)
        if action == 'only':
            kind = db.q('SELECT kind FROM jobs WHERE id=?', (sel[0],), one=True)['kind']
            _sel_add(kind, sel)                      # several searches in a row are fine: the list grows, duplicates do not count
            if kind == 'encode':                     # a selection also gets a picture comparison right away (after replacing)
                fids = [r['file_id'] for r in db.q('SELECT file_id FROM jobs WHERE id IN (%s)' % ','.join('?' * len(sel)), sel)]
                db.xmany("INSERT INTO compares(file_id,status,requested) VALUES(?,'planned',?) ON CONFLICT(file_id) DO UPDATE SET "
                         "status='planned', requested=excluded.requested WHERE compares.status NOT IN ('done','queued','running')",
                         [(f, now) for f in fids])
            db.set_setting(f'only_{kind}', 1)        # only hand out jobs from the selection, then pause
            db.set_setting('paused' if kind == 'encode' else 'subs_paused', 0)
        db.event(f'{ACTION_LABEL[action]}: {len(sel)} job(s)' + (f' (all {len(rows)})' if b.get('all') else '')); _ov_cache['t'] = 0
    return {'ok': True, 'n': len(sel), 'skipped': len(rows) - len(sel)}


@app.post('/api/files/queue', dependencies=[Depends(require_user)])
def files_queue(b: dict = Depends(jbody)):
    """Queue files manually (even if the planner had skipped them)."""
    kinds = [k for k in (b.get('kinds') or []) if k in ('encode', 'subs')]
    ids = [int(i) for i in (b.get('ids') or [])][:20000]
    if not kinds or not ids: raise HTTPException(400)
    files = db.q('SELECT id, rel, plan FROM files WHERE gone=0 AND id IN (%s)' % ','.join('?' * len(ids)), ids)
    with scanner.plan_lock:              # not at the same time as the planner: no duplicate jobs
        busy = {(r['file_id'], r['kind']) for r in db.q("SELECT file_id, kind FROM jobs WHERE status IN ('queued','running','replacing') AND file_id IN (%s)"
                                                         % ','.join('?' * len(ids)), ids)}
        top = db.q('SELECT COALESCE(MAX(priority),0) p FROM jobs', one=True)['p'] + 1
        new = [(f['id'], k, 'queued', top, time.time()) for f in files for k in kinds
               if (f['id'], k) not in busy and f['plan'] not in ('disc',)
               and not (k == 'encode' and f['plan'] in ('done', 'error', 'dolby-vision'))]     # re-encoding would drop the DV layer
        db.xmany('INSERT INTO jobs(file_id,kind,status,priority,created) VALUES(?,?,?,?,?)', new)
    if new: db.event(f'{len(new)} job(s) queued manually'); _ov_cache['t'] = 0
    return {'ok': True, 'n': len(new), 'skipped': len(files) * len(kinds) - len(new)}


@app.post('/api/quarantine/restore', dependencies=[Depends(require_user)])
def restore_many(b: dict = Depends(jbody)):
    """Restore originals. With redo: re-encode right after with the current rules (with priority)."""
    return _restore_many(b)


def _restore_many(b):
    ok = 0; errors = []; redo = []
    for qid in [int(i) for i in (b.get('ids') or [])][:500]:
        fid = (db.q('SELECT file_id FROM quarantine WHERE id=?', (qid,), one=True) or {'file_id': None})['file_id']
        try: restore(qid); ok += 1
        except HTTPException as e: errors.append(f'{qid}: {e.detail}'); continue
        if b.get('redo') and fid: redo.append(fid)
    if redo:
        top = db.q('SELECT COALESCE(MAX(priority),0) p FROM jobs', one=True)['p'] + 1
        db.xmany("INSERT INTO jobs(file_id,kind,status,priority,created) VALUES(?,'encode','queued',?,?)",
                 [(f, top, time.time()) for f in redo])
        db.event(f'{len(redo)} original(s) restored and queued again (current rules)')
    return {'ok': True, 'n': ok, 'errors': errors, 'queued': len(redo)}


@app.get('/api/files', dependencies=[Depends(require_user)])
def files(plan: str = '', q: str = '', new_days: float = 0, limit: int = 100, offset: int = 0, sort: str = '', dir: str = 'desc',
          cat: str = '', res: str = '', codec: str = ''):
    limit = max(1, min(limit, 500)); offset = max(0, offset)
    where, args = ['gone=0'], []
    if plan: where.append('plan=?'); args.append(plan)
    if q: w, a = _q_like('rel', q); where.append(w); args += a
    if new_days > 0: where.append('first_seen>?'); args.append(time.time() - new_days * 86400)
    w2, a2 = _file_filters(cat, res, codec); where += w2; args += a2
    w = ' AND '.join(where)
    order = _order(sort, dir, {'name': 'rel', 'size': 'size', 'vbr': 'vbr', 'plan': 'plan', 'added': 'first_seen', 'subs': 'subs_have'},
                   'first_seen DESC' if new_days > 0 else 'size DESC')
    total = db.q(f'SELECT COUNT(*) n FROM files WHERE {w}', args, one=True)['n']
    rows = db.q(f"SELECT id, rel, size, codec, width, height, hdr, dv, vbr, plan, subs_have, first_seen, probe_err FROM files WHERE {w} "
                f"ORDER BY {order} LIMIT ? OFFSET ?", (*args, limit, offset))
    return {'total': total, 'rows': [row(r) for r in rows]}


# ------------------------------------------------------------------ compare (original next to the new version)
CMP = os.path.join(WORK, 'compare')
CMP_NAME = re.compile(r'^shot\d_(orig|new)\.jpg$')


def _rel(p):
    """Absolute path in the container -> path relative to the media folder (for the worker)."""
    return os.path.relpath(p, MEDIA).replace(os.sep, '/')


# which replaced films a Compare tab shows: made = a comparison exists (or is being made), possible = none yet and the
# original is still held in quarantine (so one can still be made)
CMP_FROM = 'jobs j JOIN files f ON f.id=j.file_id LEFT JOIN compares c ON c.file_id=j.file_id'
CMP_MADE = "c.status IN ('queued','running','done','failed')"
CMP_POSSIBLE = ("COALESCE(c.status,'') NOT IN ('queued','running','done','failed') AND "
                "EXISTS (SELECT 1 FROM quarantine qq WHERE qq.job_id=j.id AND qq.state='held')")
CMP_VIEW = {'made': CMP_MADE, 'possible': CMP_POSSIBLE, 'all': '1=1'}


def _cmp_where(view, q):
    where, args = ["j.kind='encode' AND j.status='done'", CMP_VIEW.get(view) or '1=1'], []
    if q: w, a = _q_like('f.rel', q); where.append(w); args += a
    return ' AND '.join(where), args


def _cmp_total_kb():
    return db.q("SELECT COALESCE(SUM(json_extract(x.value, '$.orig_kb') + json_extract(x.value, '$.new_kb')), 0) kb FROM compares c, "
                "json_each(c.result, '$.shots') x WHERE c.status='done'", one=True)['kb']


@app.get('/api/compare', dependencies=[Depends(require_user)])
def compare_list(q: str = '', limit: int = 100, offset: int = 0, sort: str = '', dir: str = 'desc', view: str = 'all'):
    """Replaced films with their numbers (paged, sortable); whether there is a comparison and whether the original still exists.
    view = made | possible | all (see CMP_VIEW); counts gives the size of every view for the whole library."""
    w, args = _cmp_where(view, q)
    order = _order(sort, dir, {'name': 'f.rel', 'size': 'j.src_size', 'gain': '(j.src_size - j.out_size) * 1.0 / NULLIF(j.src_size, 0)',
                               'vmaf': "json_extract(j.result, '$.vmaf_mean')", 'finished': 'j.finished'}, 'j.finished DESC')
    total = db.q(f"SELECT COUNT(*) n FROM {CMP_FROM} WHERE {w}", args, one=True)['n']
    rows = db.q('SELECT j.id job_id, j.file_id, j.finished, j.result, j.src_size, j.out_size, f.rel, f.width, f.height, f.hdr, '
                "(SELECT state FROM quarantine qq WHERE qq.job_id=j.id ORDER BY CASE qq.state WHEN 'held' THEN 0 ELSE 1 END LIMIT 1) qstate, "
                "(SELECT created FROM quarantine qq WHERE qq.job_id=j.id AND qq.state='held') qcreated, "
                "c.status cstatus, c.finished cfinished, c.message cmessage, c.result cresult "
                f"FROM {CMP_FROM} WHERE {w} ORDER BY {order} LIMIT ? OFFSET ?", args + [max(1, min(limit, 1000)), max(0, offset)])
    qdays = db.setting('quarantine_days', float) or 0
    out = []
    for r in rows:
        d = row(r); res = d.pop('result', None) or {}
        d.update(cq=res.get('cq'), vmaf=res.get('vmaf_mean'), p5=res.get('vmaf_p5'))
        try: cr = json.loads(d.pop('cresult') or '{}')
        except ValueError: cr = {}
        shots = cr.get('shots') or []
        d['cmp_kb'] = sum(x.get('orig_kb', 0) + x.get('new_kb', 0) for x in shots)
        d['thumb'] = (shots[0].get('new') if shots and d['cstatus'] == 'done' and isinstance(shots[0], dict) else None) or None
        qc = d.pop('qcreated', None)
        d['q_expires'] = (qc + qdays * 86400) if qc else None
        out.append(d)
    c = db.q(f"SELECT SUM({CMP_MADE}) made, SUM({CMP_POSSIBLE}) possible, COUNT(*) total FROM {CMP_FROM} WHERE j.kind='encode' AND j.status='done'", one=True)
    return {'rows': out, 'total': total, 'cmp_kb': _cmp_total_kb() if offset == 0 else 0,
            'counts': {'made': c['made'] or 0, 'possible': c['possible'] or 0, 'all': c['total'] or 0}}


@app.get('/api/compare/{file_id}', dependencies=[Depends(require_user)])
def compare_detail(file_id: int):
    f = db.q('SELECT id, rel, size, width, height, hdr, codec, duration, subs_have FROM files WHERE id=?', (file_id,), one=True)
    if not f: raise HTTPException(404)
    enc = db.q("SELECT id, result, src_size, out_size, started, finished FROM jobs WHERE file_id=? AND kind='encode' AND status='done' "
               'ORDER BY finished DESC LIMIT 1', (file_id,), one=True)
    sub = db.q("SELECT result, finished FROM jobs WHERE file_id=? AND kind='subs' AND status='done' ORDER BY finished DESC LIMIT 1",
               (file_id,), one=True)
    c = db.q('SELECT status, finished, result, message FROM compares WHERE file_id=?', (file_id,), one=True)
    qrow = db.q("SELECT state FROM quarantine WHERE job_id=?", (enc['id'],), one=True) if enc else None
    return {'file': row(f), 'encode': row(enc) if enc else None, 'subs': row(sub) if sub else None,
            'compare': row(c) if c else None, 'original': (qrow['state'] if qrow else None)}


def _cmp_check(file_id):
    """Can a comparison be made for this film? Returns None, or the reason why not (the original must still be held)."""
    j = db.q("SELECT id FROM jobs WHERE file_id=? AND kind='encode' AND status='done' ORDER BY finished DESC LIMIT 1", (file_id,), one=True)
    if not j: return 'this film has not been replaced yet'
    qr = db.q("SELECT qpath FROM quarantine WHERE job_id=? AND state='held'", (j['id'],), one=True)
    if not qr or not os.path.exists(qr['qpath']):
        return 'the original has already been removed from quarantine - comparing is no longer possible'
    return None


def _cmp_queue(file_id, c=None):
    """Queue a comparison: a new row, or a failed/planned one queued again (callers check queued/running/done first)."""
    sql = ('INSERT INTO compares(file_id,status,requested) VALUES(?,?,?) ON CONFLICT(file_id) DO UPDATE SET '
           "status='queued', requested=excluded.requested, worker=NULL, finished=NULL, message=NULL")
    if c is not None: c.execute(sql, (file_id, 'queued', time.time()))
    else: db.x(sql, (file_id, 'queued', time.time()))


@app.post('/api/compare/{file_id}/make', dependencies=[Depends(require_user)])
def compare_make(file_id: int):
    err = _cmp_check(file_id)
    if err: raise HTTPException(400, err)
    _cmp_queue(file_id)
    _ov_cache['t'] = 0
    return {'ok': True}


@app.post('/api/compare/bulk-make', dependencies=[Depends(require_user)])
def compare_bulk_make(b: dict = Depends(jbody)):
    """Queue comparisons for several films: {ids:[file_id...]} or {all:true, q} (every film that still can be compared)."""
    if b.get('all'):
        w, args = _cmp_where('possible', str(b.get('q', '')))
        ids = [r['file_id'] for r in db.q(f'SELECT j.file_id FROM {CMP_FROM} WHERE {w} ORDER BY j.finished DESC LIMIT 5000', args)]
    else:
        ids = [int(i) for i in (b.get('ids') or [])][:5000]
    busy = {r['file_id'] for r in db.q("SELECT file_id FROM compares WHERE status IN ('queued','running','done')")}
    n = 0
    for fid in ids:
        if fid in busy or _cmp_check(fid): continue
        _cmp_queue(fid); n += 1
    if n: db.event(f'{n} comparison(s) requested'); _ov_cache['t'] = 0
    return {'ok': True, 'n': n, 'skipped': len(ids) - n}


@app.post('/api/compare/{file_id}/delete', dependencies=[Depends(require_user)])
def compare_delete(file_id: int):
    shutil.rmtree(os.path.join(CMP, str(file_id)), ignore_errors=True)
    db.x('DELETE FROM compares WHERE file_id=?', (file_id,))
    return {'ok': True}


@app.post('/api/compare/bulk-delete', dependencies=[Depends(require_user)])
async def compare_bulk_delete(request: Request):
    b = await request.json()
    return await run_in_threadpool(_compare_bulk_delete, b)


def _compare_bulk_delete(b):
    ids = [int(i) for i in (b.get('ids') or [])][:5000]
    for fid in ids:
        shutil.rmtree(os.path.join(CMP, str(fid)), ignore_errors=True)
    if ids: db.x('DELETE FROM compares WHERE file_id IN (%s)' % ','.join('?' * len(ids)), ids)
    db.event(f'{len(ids)} comparison(s) deleted')
    return {'ok': True, 'n': len(ids)}


@app.get('/api/compare/{file_id}/img/{name}', dependencies=[Depends(require_user)])
def compare_img(file_id: int, name: str):
    if not CMP_NAME.match(name): raise HTTPException(404)
    p = os.path.join(CMP, str(file_id), name)
    if not os.path.exists(p): raise HTTPException(404)
    return FileResponse(p, media_type='image/jpeg', headers={'Cache-Control': 'private, max-age=86400'})


@app.post('/api/worker/compare-next', dependencies=[Depends(require_worker)])
def compare_next(b: dict = Depends(wbody)):
    name = str(b.get('name', ''))[:64]
    with _assign_lock:
        c = db.q("SELECT file_id FROM compares WHERE status='queued' ORDER BY requested LIMIT 1", one=True)
        if not c: return Response(status_code=204)
        j = db.q("SELECT id FROM jobs WHERE file_id=? AND kind='encode' AND status='done' ORDER BY finished DESC LIMIT 1", (c['file_id'],), one=True)
        qr = db.q("SELECT qpath FROM quarantine WHERE job_id=? AND state='held'", (j['id'],), one=True) if j else None
        f = db.q('SELECT rel, hdr FROM files WHERE id=?', (c['file_id'],), one=True)
        if not qr or not f or not os.path.exists(qr['qpath']):
            db.x("UPDATE compares SET status='failed', message='original no longer available', finished=? WHERE file_id=?", (time.time(), c['file_id']))
            return Response(status_code=204)
        db.x("UPDATE compares SET status='running', worker=? WHERE file_id=?", (name, c['file_id']))
    os.makedirs(os.path.join(CMP, str(c['file_id'])), exist_ok=True)
    return {'file_id': c['file_id'], 'orig_rel': _rel(qr['qpath']), 'new_rel': f['rel'], 'out_rel': _rel(os.path.join(CMP, str(c['file_id']))),
            'hdr': bool(f['hdr'])}


@app.post('/api/worker/compare-done', dependencies=[Depends(require_worker)])
def compare_done(b: dict = Depends(wbody)):
    fid = int(b.get('file_id', 0))
    if b.get('ok'):
        db.x("UPDATE compares SET status='done', finished=?, result=?, message=NULL WHERE file_id=?", (time.time(), json.dumps(b.get('result') or {}), fid))
    else:
        db.x("UPDATE compares SET status='failed', finished=?, message=? WHERE file_id=?", (time.time(), str(b.get('message', ''))[:500], fid))
    _ov_cache['t'] = 0
    return {'ok': True}


# ------------------------------------------------------------------ dvd / blu-ray -> mkv
DISC_PARTS = {'dvd': ('VIDEO_TS', 'AUDIO_TS'), 'bd': ('BDMV', 'CERTIFICATE')}


# Progress of running conversions (in memory only; after a restart the worker starts the disc again)
_disc_prog = {}


def _disc_running():
    out = []
    for r in db.q("SELECT id, folder, kind, worker FROM discs WHERE status='running' ORDER BY requested"):
        p = _disc_prog.get(r['id']) or {}
        out.append({'id': r['id'], 'folder': r['folder'], 'kind': r['kind'], 'worker': r['worker'], 'step': p.get('step', 0),
                    'progress': p.get('progress', 0), 'stage': p.get('stage') or 'waiting for the worker', 'message': p.get('message'),
                    'eta': p.get('eta'), 'started': p.get('started'), 'steps': p.get('steps') or {}, 'seen': p.get('t')})
    return out


@app.get('/api/discs', dependencies=[Depends(require_user)])
def discs_list():
    out = []
    for r in db.q('SELECT * FROM discs ORDER BY folder'):
        d = dict(r)
        try: d['result'] = json.loads(d['result'] or '{}')
        except ValueError: d['result'] = {}
        out.append(d)
    return {'rows': out, 'running': _disc_running()}


@app.post('/api/worker/remux-progress', dependencies=[Depends(require_worker)])
def remux_progress(b: dict = Depends(wbody)):
    rid = int(b.get('id', 0))
    r = db.q('SELECT status FROM discs WHERE id=?', (rid,), one=True)
    if not r or r['status'] != 'running': return {'continue': False}
    p = _disc_prog.setdefault(rid, {'started': time.time(), 'steps': {}})
    step = int(b.get('step', p.get('step', 0)))
    p.update(step=max(0, min(step, 4)), progress=max(0.0, min(100.0, float(b.get('progress') or 0))), stage=str(b.get('stage') or '')[:80],
             message=str(b.get('message') or '')[:200], eta=b['eta'] if isinstance(b.get('eta'), (int, float)) else None, t=time.time())
    if b.get('note'): p['steps'][str(step)] = str(b['note'])[:120]
    return {'continue': True}


@app.post('/api/discs/action', dependencies=[Depends(require_user)])
def discs_action(b: dict = Depends(jbody)):
    ids = [int(i) for i in (b.get('ids') or [])][:500]; act = b.get('action')
    if not ids or act not in ('convert', 'skip', 'reset'): raise HTTPException(400)
    ph = ','.join('?' * len(ids))
    if act == 'convert':
        db.x(f"UPDATE discs SET status='queued', requested=?, message=NULL WHERE id IN ({ph}) AND status IN ('found','failed','skipped')",
             [time.time()] + ids)
    elif act == 'skip':
        db.x(f"UPDATE discs SET status='skipped', message='skipped by you' WHERE id IN ({ph}) AND status IN ('found','queued','failed')", ids)
    else:
        db.x(f"UPDATE discs SET status='found', message=NULL WHERE id IN ({ph}) AND status IN ('skipped','failed')", ids)
    _ov_cache['t'] = 0
    return {'ok': True, 'n': len(ids)}


@app.post('/api/worker/remux-next', dependencies=[Depends(require_worker)])
def remux_next(b: dict = Depends(wbody)):
    name = str(b.get('name', ''))[:64]
    with _assign_lock:
        r = db.q("SELECT id, folder, kind FROM discs WHERE status='queued' ORDER BY requested LIMIT 1", one=True)
        if not r: return Response(status_code=204)
        db.x("UPDATE discs SET status='running', worker=? WHERE id=?", (name, r['id']))
    _disc_prog[r['id']] = {'started': time.time(), 'steps': {}, 'step': 0, 'progress': 0, 'stage': 'finding the film', 't': time.time()}
    os.makedirs(STAGING, exist_ok=True)
    src = r['folder'] + '/VIDEO_TS' if r['kind'] == 'dvd' else r['folder']
    return {'id': r['id'], 'kind': r['kind'], 'folder': r['folder'], 'src_rel': src, 'out_rel': f"_MediaScan/staging/disc{r['id']}.mkv"}


@app.post('/api/worker/remux-done', dependencies=[Depends(require_worker)])
def remux_done(b: dict = Depends(wbody)):
    return _remux_done(b)


def _remux_done(b):
    rid = int(b.get('id', 0))
    r = db.q('SELECT * FROM discs WHERE id=?', (rid,), one=True)
    if not r: raise HTTPException(404)
    if not b.get('ok'):
        st = 'skipped' if b.get('skip') else 'failed'
        db.x('UPDATE discs SET status=?, finished=?, message=?, worker=NULL WHERE id=?', (st, time.time(), str(b.get('message', ''))[:500], rid))
        db.event(f"DVD/Blu-ray {r['folder']}: {b.get('message', '')}", 'warn'); _ov_cache['t'] = 0
        _disc_prog.pop(rid, None)
        return {'ok': True}
    p = _disc_prog.get(rid) or {}
    p.update(step=4, progress=99, stage='old folder to quarantine', message='', eta=None)
    try:
        res = _disc_replace(r, b.get('result') or {})
        db.x("UPDATE discs SET status='done', finished=?, result=?, message=NULL, worker=NULL WHERE id=?", (time.time(), json.dumps(res), rid))
    except Exception as e:
        db.x("UPDATE discs SET status='failed', finished=?, message=?, worker=NULL WHERE id=?", (time.time(), f'replace failed: {e}'[:500], rid))
        db.event(f"DVD/Blu-ray {r['folder']}: {e}", 'error')
    _disc_prog.pop(rid, None)
    _ov_cache['t'] = 0
    return {'ok': True}


def _disc_replace(r, res):
    """Put the new .mkv next to the disc folders; move VIDEO_TS/BDMV etc. to quarantine (can be restored)."""
    folder = os.path.join(MEDIA, r['folder']); staged = os.path.join(STAGING, f"disc{r['id']}.mkv")
    if not os.path.exists(staged): raise RuntimeError('new .mkv missing from staging')
    name = os.path.basename(r['folder'].rstrip('/')) or 'film'
    target = os.path.join(folder, name + '.mkv')
    if os.path.exists(target): target = os.path.join(folder, name + ' (dvd).mkv' if r['kind'] == 'dvd' else name + ' (blu-ray).mkv')
    if os.path.exists(target): raise RuntimeError('a .mkv with that name already exists')
    now = time.time(); qbase = os.path.join(QUAR, time.strftime('%Y-%m-%d'), r['folder'])
    os.makedirs(qbase, exist_ok=True)
    moved = []
    try:
        for part in DISC_PARTS[r['kind']]:
            src = os.path.join(folder, part)
            if not os.path.isdir(src):
                alt = [d for d in os.listdir(folder) if d.lower() == part.lower()]
                if not alt: continue
                src = os.path.join(folder, alt[0])
            dst = os.path.join(qbase, os.path.basename(src))
            if os.path.exists(dst): dst += f".{r['id']}"
            size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(src) for f in fs)
            os.rename(src, dst); moved.append((src, dst, size))
        os.rename(staged, target)
    except Exception:
        for src, dst, _ in reversed(moved):
            try: os.rename(dst, src)
            except OSError: pass
        raise
    new_rel = os.path.relpath(target, MEDIA).replace(os.sep, '/')
    with db.tx() as c:
        for src, dst, size in moved:
            c.execute('INSERT INTO quarantine(file_id,job_id,orig_rel,new_rel,qpath,size,created) VALUES(?,?,?,?,?,?,?)',
                      (None, None, os.path.relpath(src, MEDIA).replace(os.sep, '/'), new_rel, dst, size, now))
        under = re.sub(r'([%_\\])', r'\\\1', r['folder']) + '/%'       # _ and % in a folder name are not wildcards
        c.execute("UPDATE files SET gone=1 WHERE plan='disc' AND rel LIKE ? ESCAPE '\\'", (under,))
        c.execute("UPDATE jobs SET status='cancelled', message='dvd/blu-ray converted to mkv' WHERE status='queued' AND file_id IN "
                  "(SELECT id FROM files WHERE plan='disc' AND rel LIKE ? ESCAPE '\\')", (under,))
    res['new_rel'] = new_rel; res['new_size'] = os.path.getsize(target)
    db.event(f"DVD/Blu-ray converted: {new_rel} ({res['new_size']/1e9:.1f} GB); the disc folder is in quarantine")
    threading.Thread(target=scanner.scan, daemon=True).start()          # read in and plan the new .mkv right away
    threading.Thread(target=plex_refresh, daemon=True).start()
    return res


@app.get('/api/quarantine', dependencies=[Depends(require_user)])
def quarantine(q: str = '', limit: int = 100, offset: int = 0, sort: str = '', dir: str = 'desc'):
    where, args = ["state='held'"], []
    if q: w, a = _q_like('orig_rel', q); where.append(w); args += a
    order = _order(sort, dir, {'name': 'orig_rel', 'size': 'size', 'created': 'created'}, 'created DESC')
    total = db.q(f"SELECT COUNT(*) n FROM quarantine WHERE {' AND '.join(where)}", args, one=True)['n']
    rows = db.q(f"SELECT id, orig_rel, size, created FROM quarantine WHERE {' AND '.join(where)} ORDER BY {order} LIMIT ? OFFSET ?",
                args + [max(1, min(limit, 1000)), max(0, offset)])
    keep = (db.setting('quarantine_days', float) or 0) * 86400
    return {'rows': [dict(r, expires=(r['created'] or 0) + keep) for r in rows], 'total': total}


def restore(qid: int):
    r = db.q("SELECT * FROM quarantine WHERE id=? AND state='held'", (qid,), one=True)
    if not r: raise HTTPException(404)
    orig = os.path.join(MEDIA, r['orig_rel']); new = os.path.join(MEDIA, r['new_rel'])
    if not os.path.exists(r['qpath']): raise HTTPException(410, 'original is no longer in quarantine')
    if os.path.exists(orig) and orig != new: raise HTTPException(409, 'a file already exists at the original location')
    aside = None
    if os.path.isfile(new):                         # the new version: set aside until the original is back
        aside = new + '.restoring'
        os.replace(new, aside)
    os.makedirs(os.path.dirname(orig), exist_ok=True)
    try:
        os.rename(r['qpath'], orig)
    except OSError:
        if aside: os.replace(aside, new)
        raise
    if aside:
        try: os.remove(aside)
        except OSError: pass
    st = os.stat(orig)
    with db.tx() as c:
        c.execute("UPDATE quarantine SET state='restored' WHERE id=?", (qid,))
        c.execute("UPDATE files SET rel=rel||'#gone'||id WHERE rel=? AND gone=1 AND id<>?", (r['orig_rel'], r['file_id']))
        c.execute("UPDATE files SET rel=?, size=?, mtime=?, ours=0, plan='restored' WHERE id=?", (r['orig_rel'], st.st_size, st.st_mtime, r['file_id']))
        c.execute("UPDATE jobs SET status='restored' WHERE id=?", (r['job_id'],))
    _refresh_probe(r['file_id'], orig, st.st_size)
    db.event(f"Original restored: {_base(r['orig_rel'])}", 'warn'); _ov_cache['t'] = 0
    threading.Thread(target=plex_refresh, daemon=True).start()
    return {'ok': True}


@app.post('/api/scan', dependencies=[Depends(require_user)])
def scan_now():
    if scanner.state['running']: return {'ok': False, 'msg': 'scan already running'}
    threading.Thread(target=scanner.scan, daemon=True).start()
    return {'ok': True}


@app.get('/api/settings', dependencies=[Depends(require_user)])
def get_settings():
    s = {r['key']: r['value'] for r in db.q('SELECT key, value FROM settings')}
    url, tok = plex_cfg()
    s['plex_url'] = url; s['plex_token_set'] = bool(tok); s['lang_list'] = NAME_NL; s['lang_list_en'] = NAME_EN
    for k in [k for k in s if k.startswith(('sel_', 'wver_', 'mig_'))]: del s[k]     # internal, not for display
    return s


@app.post('/api/plex', dependencies=[Depends(require_user)])
async def plex_set(request: Request):
    """Connect Plex: {url, token} (leave token empty = keep the current one) or {clear: true}. Tests the connection right away."""
    b = await request.json()
    return await run_in_threadpool(_plex_set, b)


def _plex_set(b):
    if b.get('clear'):
        db.set_setting('plex_url', ''); db.auth_set('plex_token', None); db.set_setting('plex_off', 1)
        db.event('Plex disconnected'); return {'ok': True}
    url = str(b.get('url') or '').strip().rstrip('/')
    if not re.match(r'^https?://[A-Za-z0-9.\-\[\]:]+(/[^\s]*)?$', url): raise HTTPException(400, 'Invalid address, e.g. http://192.168.1.10:32400')
    db.set_setting('plex_off', 0)
    tok = str(b.get('token') or '').strip() or plex_cfg()[1]
    if not tok: raise HTTPException(400, 'Enter a Plex token')
    try:
        mc = plex_get('/identity', timeout=6, cfg=(url, tok)).get('MediaContainer', {})
        plex_get('/status/sessions', timeout=6, cfg=(url, tok))
    except Exception as e:
        raise HTTPException(400, f'Plex is not responding properly: {str(e)[:120]}')
    db.set_setting('plex_url', url); db.auth_set('plex_token', tok)
    db.event('Plex connected'); return {'ok': True, 'version': mc.get('version')}


# allowed values per setting: (type, min, max) or regex
VALID = {
    'min_mean': (float, 80, 100), 'min_p5': (float, 60, 100), 'min_saving_pct': (float, 0, 90), 'min_keep_pct': (float, 0, 80), 'min_vbr_1080': (float, 0, 40), 'min_vbr_2160': (float, 0, 100),
    'quarantine_days': (float, 0, 90), 'quarantine_max_gb': (float, 0, 20000), 'min_free_gb': (float, 0, 50000),
    'scan_interval_min': (float, 0, 10080), 'paused': (int, 0, 1), 'subs_paused': (int, 0, 1), 'setup_done': (int, 0, 1),
    'langs': r'^(%s)(,(%s)){0,9}$' % ('|'.join(NAME_NL), '|'.join(NAME_NL)), 'skip_dirs': r'^[^/\\]{0,500}$', 'anim_dirs': r'^[^/\\]{0,500}$',
    'cpu_fallback': (int, 0, 1), 'cpu_preset': r'^(medium|slow)$',
    'cmp_auto': (int, 0, 1), 'cmp_max_mb': (float, 0, 100000),       # automatic comparisons; cap on the stored frames (MB, 0 = none)
}


@app.post('/api/settings', dependencies=[Depends(require_user)])
def post_settings(b: dict = Depends(jbody)):
    body = b; changed = []
    for k, v in body.items():
        rule = VALID.get(k)
        if rule is None: continue
        v = str(v).strip()
        if isinstance(rule, str):
            v = v.replace(' ', '') if k == 'langs' else v
            if not re.fullmatch(rule, v): raise HTTPException(400, f'invalid value for {k}')
        else:
            typ, lo, hi = rule
            try: n = typ(float(v))
            except ValueError: raise HTTPException(400, f'invalid value for {k}')
            if not lo <= n <= hi: raise HTTPException(400, f'{k} must be between {lo} and {hi}')
            v = str(n)
        db.set_setting(k, v)
        if k != 'setup_done': changed.append(f'{k}={v}')
        if k in ('paused', 'subs_paused'):        # switched on/off by hand: 'only these' no longer applies
            db.set_setting('only_encode' if k == 'paused' else 'only_subs', 0)
    if changed: db.event('Settings changed: ' + ', '.join(changed))
    if any(k in body for k in ('min_vbr_1080', 'min_vbr_2160', 'anim_dirs', 'min_saving_pct')):   # recalculate the planning and the queue
        scanner._VF['t'] = scanner._anim['t'] = 0
        threading.Thread(target=lambda: (scanner.replan(), scanner.plan_jobs()), daemon=True).start()
    elif 'langs' in body:
        threading.Thread(target=scanner.plan_jobs, daemon=True).start()
    _ov_cache['t'] = 0
    return {'ok': True}


# ------------------------------------------------------------------ worker API
def worker_caps(info):
    """What the worker can do. Old workers send no 'caps': then everything it reports as a role (or everything)."""
    caps = info.get('caps')
    if isinstance(caps, dict): return {'encode': bool(caps.get('encode')), 'subs': bool(caps.get('subs')), 'cpu': bool(caps.get('cpu'))}
    roles = info.get('roles') or ['encode', 'subs']
    return {'encode': 'encode' in roles, 'subs': 'subs' in roles, 'cpu': False}


def worker_cfg(name):
    """What the worker may do (set in the dashboard), limited by what it can do."""
    w = db.q('SELECT info, cfg FROM workers WHERE name=?', (name,), one=True)
    info = json.loads(w['info'] or '{}') if w else {}
    caps = worker_caps(info)
    cfg = json.loads(w['cfg']) if w and w['cfg'] else {'encode': caps['encode'], 'subs': caps['subs'], 'max_height': int(info.get('max_height') or 0)}
    return {'encode': bool(cfg.get('encode')) and caps['encode'], 'subs': bool(cfg.get('subs')) and caps['subs'],
            'cpu': bool(cfg.get('cpu')) and caps['cpu'],          # CPU encodes are opt-in: hours per film
            'max_height': int(cfg.get('max_height') or 0), 'caps': caps, 'raw': cfg}


def _worker_update(name, info):
    """'available' when this worker runs other files than the server hands out (version or sha256 of a worker file),
    None when it is up to date or does not say (old workers send no file hashes)."""
    ver = _worker_version()
    if not ver: return None
    w = db.q('SELECT value FROM settings WHERE key=?', (f'wver_{name}',), one=True)
    wver = (w['value'] if w else None) or info.get('version')
    if wver and wver != ver: return 'available'
    shas = info.get('files_sha')
    if isinstance(shas, dict) and any(_wv['files'].get(f) and _wv['files'][f] != h for f, h in shas.items()): return 'available'
    return None


def _cpu_worker_available():
    """Is there a worker that may run x265 jobs (switched on in the dashboard, seen in the last 10 minutes)?"""
    return any(worker_cfg(w['name'])['cpu'] for w in db.q('SELECT name FROM workers WHERE last_seen>?', (time.time() - STALE_SEC,)))


def _server_facts():
    """What the server runs on (for the Info page). Everything best-effort: a NAS container may hide some of it."""
    f = {'version': APP_VERSION, 'python': platform.python_version(), 'uptime': round(time.time() - STARTED), 'threads': os.cpu_count()}
    try:
        txt = open('/proc/cpuinfo', encoding='utf-8', errors='replace').read()
        m = re.search(r'^model name\s*:\s*(.+)$', txt, re.M); f['cpu'] = m.group(1).strip() if m else platform.processor()
        f['cores'] = len(set(re.findall(r'^core id\s*:\s*(\d+)', txt, re.M))) or None
        m = re.search(r'^MemTotal:\s*(\d+)', open('/proc/meminfo', encoding='utf-8').read(), re.M)
        f['ram_gb'] = round(int(m.group(1)) / 2**20, 1) if m else None
        m = re.search(r'^VmRSS:\s*(\d+)', open('/proc/self/status', encoding='utf-8').read(), re.M)
        f['rss_mb'] = round(int(m.group(1)) / 1024) if m else None
    except OSError:
        pass
    f['disk_free'], f['disk_total'] = disk_free()
    try: f['db_bytes'] = sum(os.path.getsize(db.DB_PATH + x) for x in ('', '-wal') if os.path.exists(db.DB_PATH + x))
    except OSError: f['db_bytes'] = None
    f['files'] = db.q('SELECT COUNT(*) n FROM files WHERE gone=0', one=True)['n']
    return f


@app.get('/api/info', dependencies=[Depends(require_user)])
def system_info():
    """Hardware, versions and roles of the server and every worker (the Info page rates them in the browser)."""
    ws = []
    for w in db.q('SELECT name, last_seen, info FROM workers ORDER BY name'):
        info = json.loads(w['info'] or '{}'); c = worker_cfg(w['name'])
        ver = db.q('SELECT value FROM settings WHERE key=?', (f"wver_{w['name']}",), one=True)
        ws.append({'name': w['name'], 'online': time.time() - w['last_seen'] < 120, 'last_seen': w['last_seen'],
                   'version': (ver['value'] if ver else None) or info.get('version'), 'os': info.get('os'), 'gpu': info.get('gpu'),
                   'encoder': info.get('encoder'), 'enc_key': info.get('enc_key'), 'hw': info.get('hw') or {}, 'versions': info.get('versions') or {},
                   'models': info.get('models') or {}, 'caps': c['caps'], 'cfg': {'encode': c['encode'], 'subs': c['subs'], 'cpu': c['cpu']}})
    return {'server': _server_facts(), 'workers': ws, 'worker_version': _worker_version(),
            'settings': {'min_free_gb': db.setting('min_free_gb', float), 'cpu_preset': db.setting('cpu_preset'), 'langs': db.setting('langs'),
                         'min_mean': db.setting('min_mean', float), 'min_p5': db.setting('min_p5', float)}}


@app.get('/api/workers', dependencies=[Depends(require_user)])
def workers_list():
    out = []
    for w in db.q('SELECT name, last_seen, info FROM workers ORDER BY name'):
        c = worker_cfg(w['name'])
        job = db.q("SELECT j.kind, j.cpu, j.stage, j.progress, f.rel FROM jobs j JOIN files f ON f.id=j.file_id WHERE j.worker=? AND j.status='running'", (w['name'],))
        info = json.loads(w['info'] or '{}')
        out.append({'name': w['name'], 'last_seen': w['last_seen'], 'online': time.time() - w['last_seen'] < 120, 'info': info,
                    'cfg': c['raw'], 'caps': c['caps'], 'jobs': [dict(j) for j in job], 'update': _worker_update(w['name'], info)})
    return out


@app.post('/api/workers/{name}', dependencies=[Depends(require_user)])
def workers_set(name: str, b: dict = Depends(jbody)):
    if not db.q('SELECT 1 FROM workers WHERE name=?', (name,), one=True): raise HTTPException(404)
    cur = worker_cfg(name)['raw']
    for k in ('encode', 'subs', 'cpu'):
        if k in b: cur[k] = bool(b[k])
    if 'max_height' in b:
        mh = int(b['max_height'] or 0)
        if mh not in (0, 480, 720, 1080, 1440, 2160): raise HTTPException(400, 'invalid resolution')
        cur['max_height'] = mh
    db.x('UPDATE workers SET cfg=? WHERE name=?', (json.dumps(cur), name))
    db.event(f"Worker {name}: encoding {'on' if cur.get('encode') else 'off'}, subtitles {'on' if cur.get('subs') else 'off'}, "
             f"CPU encodes {'on' if cur.get('cpu') else 'off'}" + (f", up to {cur['max_height']}p" if cur.get('max_height') else ''))
    _ov_cache['t'] = 0
    return {'ok': True}


@app.post('/api/workers/{name}/forget', dependencies=[Depends(require_user)])
def workers_forget(name: str):
    w = db.q('SELECT last_seen FROM workers WHERE name=?', (name,), one=True)
    if not w: raise HTTPException(404)
    if time.time() - w['last_seen'] < 120: raise HTTPException(409, 'worker is still online')
    n = db.q("SELECT COUNT(*) n FROM jobs WHERE worker=? AND status='running'", (name,), one=True)['n']
    if n: db.x("UPDATE jobs SET status='queued', worker=NULL, progress=0, message='worker removed - rescheduled', attempts=MAX(attempts-1,0) "
               "WHERE worker=? AND status='running'", (name,))
    db.x("UPDATE compares SET status='queued', worker=NULL WHERE worker=? AND status='running'", (name,))
    db.x("UPDATE discs SET status='queued', worker=NULL WHERE worker=? AND status='running'", (name,))
    db.x('DELETE FROM workers WHERE name=?', (name,)); db.x('DELETE FROM settings WHERE key=?', (f'wver_{name}',)); db.event(f'Worker {name} removed' + (f': {n} job(s) back in the queue' if n else '')); _ov_cache['t'] = 0
    return {'ok': True}


@app.post('/api/worker/heartbeat', dependencies=[Depends(require_worker)])
def heartbeat(b: dict = Depends(wbody)):
    name = str(b.get('name', ''))[:64]
    if not name: raise HTTPException(400)
    info = b.get('info') if isinstance(b.get('info'), dict) else {}
    raw = json.dumps(info)
    if len(raw) > 8000: raw = json.dumps({k: info[k] for k in ('gpu', 'encoder', 'enc_key', 'caps', 'roles', 'max_height', 'os', 'version', 'hw', 'versions', 'models', 'files_sha') if k in info})
    if len(raw) > 8000: raw = json.dumps({k: info[k] for k in ('gpu', 'encoder', 'enc_key', 'caps', 'roles', 'max_height', 'os', 'version') if k in info})
    db.x('INSERT INTO workers(name,last_seen,info) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET last_seen=excluded.last_seen, info=excluded.info',
         (name, time.time(), raw))
    return {'paused': db.setting('paused', bool), 'subs_paused': db.setting('subs_paused', bool)}


@app.post('/api/worker/hello', dependencies=[Depends(require_worker)])
def hello(b: dict = Depends(wbody)):
    """Worker (re)start: jobs still assigned to it were interrupted -> back in the queue."""
    name = str(b.get('name', ''))[:64]
    n = db.q("SELECT COUNT(*) n FROM jobs WHERE worker=? AND status='running'", (name,), one=True)['n']
    if n:
        db.x("UPDATE jobs SET status='queued', worker=NULL, progress=0, message='worker restarted - rescheduled', "
             "attempts=MAX(attempts-1,0) WHERE worker=? AND status='running'", (name,))
        db.event(f'{name} restarted: {n} interrupted job(s) back in the queue', 'warn'); _ov_cache['t'] = 0
    db.x("UPDATE compares SET status='queued', worker=NULL WHERE worker=? AND status='running'", (name,))
    db.x("UPDATE discs SET status='queued', worker=NULL WHERE worker=? AND status='running'", (name,))
    # new worker version: recently failed jobs get another chance (the update may have fixed the error)
    ver = str(b.get('version', ''))[:20]; key = f'wver_{name}'
    old = db.q('SELECT value FROM settings WHERE key=?', (key,), one=True)
    retried = 0
    if ver and (not old or old['value'] != ver):
        retried = db.q("SELECT COUNT(*) n FROM jobs WHERE status='failed' AND finished>?", (time.time() - 3 * 86400,), one=True)['n']
        if retried:
            db.x("UPDATE jobs SET status='queued', worker=NULL, progress=0, attempts=0, not_before=0, finished=NULL, "
                 "message='retry after worker update' WHERE status='failed' AND finished>?", (time.time() - 3 * 86400,))
            db.event(f'{name} updated to {ver}: {retried} failed job(s) back in the queue'); _ov_cache['t'] = 0
    if ver: db.set_setting(key, ver)
    return {'ok': True, 'requeued': n, 'retried': retried}


_assign_lock = threading.Lock()


@app.post('/api/worker/next', dependencies=[Depends(require_worker)])
def next_job(b: dict = Depends(wbody)):
    kind = b.get('kind'); name = str(b.get('name', ''))[:64]
    if kind not in ('encode', 'subs') or not name: raise HTTPException(400)
    cpu = 1 if (kind == 'encode' and b.get('cpu')) else 0
    cfg = worker_cfg(name)
    if not cfg['cpu' if cpu else kind]: return Response(status_code=204)      # this role is switched off for this worker
    try: maxh = max(0, int(b.get('max_height') or 0))
    except (TypeError, ValueError): maxh = 0
    if cfg['max_height']: maxh = cfg['max_height'] if not maxh else min(maxh, cfg['max_height'])
    wlim = int(maxh * 16 / 9 * 1.1) if maxh else 0          # width limit (also for cinema aspect ratios such as 3840x1608)
    if kind == 'encode':
        if db.setting('paused', bool): return Response(status_code=204)
        free, _ = disk_free()
        if free and free < db.setting('min_free_gb', float) * 1e9: return Response(status_code=204)
    elif db.setting('subs_paused', bool):
        return Response(status_code=204)
    with _assign_lock:
        only = db.setting(f'only_{kind}', bool)
        ids = _sel_ids(kind) if only else []
        extra, eargs = ('', [])
        if only:
            if not ids: ids = [-1]
            extra, eargs = ' AND j.id IN (%s)' % ','.join('?' * len(ids)), ids
        r = db.q("SELECT j.id, j.file_id, j.kind, f.rel, f.size, f.width, f.height, f.hdr, f.category FROM jobs j JOIN files f ON f.id=j.file_id "
                 "WHERE j.kind=? AND j.status='queued' AND f.gone=0 AND j.not_before<? AND (?=0 OR COALESCE(f.width,0)<=?) AND COALESCE(j.cpu,0)=?" + extra +
                 " ORDER BY j.priority DESC, f.est_save DESC, j.id LIMIT 1",
                 [kind, time.time(), wlim, wlim, cpu] + eargs, one=True)
        if not r:      # nothing for this worker; pause only if nothing from the selection is waiting any more
            if only and not db.q("SELECT 1 FROM jobs WHERE status='queued' AND id IN (%s) LIMIT 1" % ','.join('?' * len(ids)), ids, one=True):
                _only_done(kind)
            return Response(status_code=204)
        db.x("UPDATE jobs SET status='running', worker=?, started=?, progress=0, stage='start', message=NULL, attempts=attempts+1 WHERE id=?",
             (name, time.time(), r['id']))
        if only and not db.q("SELECT 1 FROM jobs WHERE status='queued' AND id IN (%s) LIMIT 1" % ','.join('?' * len(ids)), ids, one=True):
            _only_done(kind)                          # the last one from the selection was handed out: pause afterwards
    d = dict(r)
    vf = scanner.vfloor(d.pop('width'), d.pop('height'), d.pop('hdr'), d.pop('category'))
    d['settings'] = {'min_vbr_bps': round(vf),'min_mean': db.setting('min_mean', float), 'min_p5': db.setting('min_p5', float),
                     'langs': db.setting('langs'), 'min_saving_pct': db.setting('min_saving_pct', float),
                     'min_keep_pct': db.setting('min_keep_pct', float), 'cpu_preset': db.setting('cpu_preset') or 'medium'}
    d['cpu'] = cpu
    db.event(f"{name} started {'CPU encoding' if cpu else 'encoding' if kind == 'encode' else 'subtitles'}: {_base(r['rel'])}"); _ov_cache['t'] = 0
    return d


def _jid(b):
    try: return int(b['job_id'])
    except (KeyError, TypeError, ValueError): raise HTTPException(400, 'job_id missing') from None


def _own_job(jid, name=None):
    """Fetch a job; a worker may only report on its own job (old workers send no name)."""
    j = db.q('SELECT * FROM jobs WHERE id=?', (jid,), one=True)
    if not j: raise HTTPException(404)
    if name and j['worker'] and j['worker'] != name: raise HTTPException(409, 'job belongs to another worker')
    return j


@app.post('/api/worker/progress', dependencies=[Depends(require_worker)])
def progress(b: dict = Depends(wbody)):
    try: prog = max(0.0, min(100.0, float(b.get('progress') or 0)))
    except (TypeError, ValueError): prog = 0.0
    nm = b.get('name')
    if nm: db.x('UPDATE workers SET last_seen=? WHERE name=?', (time.time(), nm))
    db.x("UPDATE jobs SET progress=?, stage=?, message=? WHERE id=? AND status='running'" + (" AND worker=?" if nm else ''),
         (prog, str(b.get('stage') or '')[:120], str(b.get('message') or '')[:300] or None, _jid(b)) + ((nm,) if nm else ()))
    j = db.q('SELECT status, worker FROM jobs WHERE id=?', (_jid(b),), one=True)
    return {'continue': bool(j and j['status'] == 'running' and (not nm or j['worker'] == nm))}


@app.post('/api/worker/done', dependencies=[Depends(require_worker)])
def done(b: dict = Depends(wbody)):
    jid = _jid(b); res = b.get('result') or {}; outcome = b.get('outcome')
    if not isinstance(res, dict): res = {}
    j = _own_job(jid, b.get('name')); now = time.time()
    if j['status'] != 'running':
        repeat = (outcome == 'staged' and j['status'] in ('replacing', 'done')) or (outcome != 'staged' and j['status'] == 'no-gain')
        if repeat and j['worker'] == b.get('name'): return {'ok': True, 'repeat': True}     # our earlier reply got lost
        return {'ok': False, 'msg': 'job is no longer active'}
    msg = str(b.get('message') or '')[:500] or None
    if j['kind'] == 'encode':
        src = int(res.get('src_size') or 0); out = int(res.get('out_size') or 0)
        if outcome == 'staged':
            res['staged_rel'] = f'_MediaScan/staging/{jid}.mkv'     # the server decides the path, not the worker
            db.x("UPDATE jobs SET status='replacing', stage='waiting to replace', progress=100, message=NULL, result=?, src_size=?, out_size=? WHERE id=?",
                 (json.dumps(res), src, out, jid))
        else:
            db.x("UPDATE jobs SET status='no-gain', stage=NULL, finished=?, result=?, message=?, src_size=?, out_size=? WHERE id=?",
                 (now, json.dumps(res), msg, src or None, out or None, jid))
            db.x("UPDATE files SET plan='no-gain' WHERE id=?", (j['file_id'],))
            # grainy film: the GPU encoder needs more bits than the source to keep the quality. With the CPU fallback on,
            # try once more with x265 (tune grain), which keeps grain far more efficiently.
            if (not j['cpu'] and db.setting('cpu_fallback', bool) and _cpu_worker_available()
                    and res.get('reason') in ('needs more bitrate than the source', 'no setting meets the quality target', 'whole-film check below target')):
                db.x("UPDATE jobs SET status='queued', cpu=1, message='retrying on the CPU (x265)', progress=0, attempts=0, finished=NULL, not_before=0 WHERE id=?", (jid,))
                db.x("UPDATE files SET plan='encode' WHERE id=?", (j['file_id'],))
                f = db.q('SELECT rel FROM files WHERE id=?', (j['file_id'],), one=True)
                db.event(f"{_base(f['rel']) if f else jid}: GPU could not keep the quality - queued for the CPU encoder (x265)")
            db.x("DELETE FROM compares WHERE file_id=? AND status='planned'", (j['file_id'],))   # nothing replaced: nothing to compare
    else:
        made = [m for m in res.get('made', []) if isinstance(m, dict) and re.match(r'^[a-z]{2}$', str(m.get('lang', '')))]
        f = db.q('SELECT subs_have FROM files WHERE id=?', (j['file_id'],), one=True)
        have = set(json.loads(f['subs_have'] or '[]')) | {m['lang'] for m in made}
        db.x('UPDATE files SET subs_have=? WHERE id=?', (json.dumps(sorted(have)), j['file_id']))
        db.x('UPDATE jobs SET status=?, finished=?, progress=100, result=?, message=? WHERE id=?',
             ('done' if made else 'not-found', now, json.dumps({'made': made}), msg, jid))
    _ov_cache['t'] = 0
    return {'ok': True}


@app.post('/api/worker/fail', dependencies=[Depends(require_worker)])
def fail(b: dict = Depends(wbody)):
    jid = _jid(b); j = _own_job(jid, b.get('name'))
    if j['status'] != 'running': return {'ok': False}
    msg = str(b.get('message', ''))[:1000]
    try: delay = float(b.get('retry_after') or 0)
    except (TypeError, ValueError): delay = 0
    if delay > 0:                     # e.g. OpenSubtitles daily limit: retry later, does not count as an attempt
        db.x("UPDATE jobs SET status='queued', worker=NULL, not_before=?, message=?, attempts=MAX(attempts-1,0) WHERE id=?",
             (time.time() + min(delay, 86400), msg, jid))
    elif j['attempts'] < 3 and not b.get('final'):     # final: the worker knows a retry gives the same result
        db.x("UPDATE jobs SET status='queued', worker=NULL, not_before=?, message=? WHERE id=?", (time.time() + 600, msg, jid))
    else:
        db.x("UPDATE jobs SET status='failed', stage=NULL, finished=?, message=? WHERE id=?", (time.time(), msg, jid))
    f = db.q('SELECT rel FROM files WHERE id=?', (j['file_id'],), one=True)
    db.event(f"Error in {_base(f['rel']) if f else jid}: {msg[:200]}", 'error'); _ov_cache['t'] = 0
    return {'ok': True}


# ------------------------------------------------------------------ background: replacing, quarantine, offline workers
def _refresh_probe(fid, path, size):
    """Store codec, bitrate, size and audio of the file that is now at this path (after a restore)."""
    try:
        sm = scanner.summarize(scanner.ffprobe(path), size)
        if sm: db.x('UPDATE files SET codec=?, vbr=?, width=?, height=?, hdr=?, audio=? WHERE id=?',
                    (sm['codec'], sm['vbr'], sm['width'], sm['height'], sm['hdr'], sm['audio'], fid))
    except Exception:
        pass            # the next scan probes it again (size and mtime changed)


def set_job(jid, **kw):
    db.x('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in kw) + ' WHERE id=?', (*kw.values(), jid))


def do_replace(j, playing=None):
    res = json.loads(j['result'] or '{}')
    staged = os.path.join(STAGING, f"{j['id']}.mkv")
    orig = os.path.join(MEDIA, j['rel'])
    now = time.time()
    if not os.path.exists(staged):
        return set_job(j['id'], status='failed', message='new version missing from staging', finished=now)
    try:
        st0 = os.stat(orig)
    except OSError:
        os.remove(staged); return set_job(j['id'], status='cancelled', message='original disappeared', finished=now)
    if j['src_size'] and st0.st_size != j['src_size']:
        os.remove(staged); return set_job(j['id'], status='cancelled', message='original has changed in the meantime', finished=now)
    if playing is None: playing = plex_playing()
    if playing is None: return set_job(j['id'], stage='waiting: Plex unreachable')
    if os.path.basename(orig).lower() in playing: return set_job(j['id'], stage='waiting: being watched in Plex')
    try:                                              # final check on the NAS itself
        p, po = scanner.ffprobe(staged), scanner.ffprobe(orig)
        ok = abs(float(p['format'].get('duration') or 0) - float(po['format'].get('duration') or 0)) < 1.5 and len(p['streams']) == len(po['streams'])
    except Exception:
        ok = False
    if not ok:
        os.remove(staged); return set_job(j['id'], status='failed', message='check on the NAS rejected (duration/tracks)', finished=now)
    new_rel = os.path.splitext(j['rel'])[0] + '.mkv'
    new = os.path.join(MEDIA, new_rel)
    if new != orig and os.path.exists(new):
        os.remove(staged)                             # a retry encodes again; do not leave tens of GB behind
        return set_job(j['id'], status='failed', message='target file already exists', finished=now)
    qdir = os.path.join(QUAR, time.strftime('%Y-%m-%d'), os.path.dirname(j['rel']))
    os.makedirs(qdir, exist_ok=True)
    qpath = os.path.join(qdir, os.path.basename(orig))
    if os.path.exists(qpath):                         # do not overwrite
        b, e = os.path.splitext(qpath); qpath = f"{b}.{j['id']}{e}"
    os.rename(orig, qpath)                            # same volume: instant, bypassing the recycle bin
    try:
        os.rename(staged, new)
    except OSError:
        os.rename(qpath, orig); raise
    try:
        st = os.stat(new)
        res['out_size'] = st.st_size
        with db.tx() as c:
            # a file that vanished from the library earlier may still hold the new name (files.rel is unique)
            c.execute("UPDATE files SET rel=rel||'#gone'||id WHERE rel=? AND gone=1 AND id<>?", (new_rel, j['file_id']))
            c.execute('INSERT INTO quarantine(file_id,job_id,orig_rel,new_rel,qpath,size,created) VALUES(?,?,?,?,?,?,?)',
                      (j['file_id'], j['id'], j['rel'], new_rel, qpath, st0.st_size, now))
            c.execute("UPDATE files SET rel=?, size=?, mtime=?, ours=1, plan='done', codec='hevc', est_save=0 WHERE id=?",
                      (new_rel, st.st_size, st.st_mtime, j['file_id']))
            sm = scanner.summarize(p, st.st_size) if ok else None
            if sm: c.execute('UPDATE files SET codec=?, vbr=?, width=?, height=?, hdr=?, audio=? WHERE id=?',
                             (sm['codec'], sm['vbr'], sm['width'], sm['height'], sm['hdr'], sm['audio'], j['file_id']))
            c.execute("UPDATE jobs SET status='done', stage='replacing', finished=?, result=?, out_size=? WHERE id=?",
                      (now, json.dumps(res), st.st_size, j['id']))
    except Exception:
        # nothing recorded: put both files back, so the original is never left untracked in quarantine
        try: os.rename(new, staged)
        except OSError: pass
        os.rename(qpath, orig)
        raise
    db.event(f"Replaced: {_base(new_rel)} ({st0.st_size/1e9:.1f} -> {st.st_size/1e9:.1f} GB, VMAF {res.get('vmaf_mean')})")
    if db.setting('cmp_auto', bool): _cmp_queue(j['file_id'])            # picture comparison for every replaced film
    else: db.x("UPDATE compares SET status='queued', requested=? WHERE file_id=? AND status='planned'", (time.time(), j['file_id']))
    _ov_cache['t'] = 0
    threading.Thread(target=plex_refresh, daemon=True).start()


def purge_quarantine():
    days = db.setting('quarantine_days', float); cap = db.setting('quarantine_max_gb', float) * 1e9
    held = db.q("SELECT id, qpath, size, created FROM quarantine WHERE state='held' ORDER BY created")
    total = sum(r['size'] for r in held)
    for r in held:
        if time.time() - r['created'] <= days * 86400 and total <= cap: continue
        try:
            if os.path.isdir(r['qpath']): shutil.rmtree(r['qpath'])          # DVD/Blu-ray folder
            elif os.path.exists(r['qpath']): os.remove(r['qpath'])
        except OSError as e:
            db.event(f'Quarantine cleanup failed: {e}', 'error'); continue
        total -= r['size']
        db.x("UPDATE quarantine SET state='deleted' WHERE id=?", (r['id'],))


_cmp_purge = {'t': 0}


def purge_compares():
    """Keep the comparison frames under cmp_max_mb: the oldest finished comparisons go first (files and rows)."""
    if time.time() - _cmp_purge['t'] < 300: return
    _cmp_purge['t'] = time.time()
    cap = db.setting('cmp_max_mb', float) or 0
    if cap <= 0: return
    total = _cmp_total_kb()
    if total <= cap * 1024: return
    over = total / 1024; removed = 0
    for r in db.q("SELECT c.file_id, COALESCE(SUM(json_extract(x.value, '$.orig_kb') + json_extract(x.value, '$.new_kb')), 0) kb "
                  "FROM compares c, json_each(c.result, '$.shots') x WHERE c.status='done' GROUP BY c.file_id ORDER BY c.finished"):
        if total <= cap * 1024: break
        shutil.rmtree(os.path.join(CMP, str(r['file_id'])), ignore_errors=True)
        db.x('DELETE FROM compares WHERE file_id=?', (r['file_id'],))
        total -= r['kb']; removed += 1
    if removed: db.event(f'Comparisons cleaned up: {removed} removed (over {over:.0f} MB)'); _ov_cache['t'] = 0


STARTED = time.time()


def sweep_staging():
    """Delete leftovers in staging (cancelled copies, rejected versions) that no job is waiting for, after a day."""
    if not os.path.isdir(STAGING): return
    live = {str(r['id']) for r in db.q("SELECT id FROM jobs WHERE kind='encode' AND status IN ('running','replacing')")}
    for n in os.listdir(STAGING):
        p = os.path.join(STAGING, n)
        if n.split('.')[0] in live or not os.path.isfile(p) or time.time() - os.path.getmtime(p) < 86400: continue
        try: os.remove(p); db.event(f'Staging cleanup: removed leftover {n}')
        except OSError: pass


def requeue_stale():
    if time.time() - STARTED < STALE_SEC: return      # just restarted: workers have not had a chance to check in yet
    for w in db.q('SELECT name FROM workers WHERE last_seen<?', (time.time() - STALE_SEC,)):
        n = db.q("SELECT COUNT(*) n FROM jobs WHERE worker=? AND status='running'", (w['name'],), one=True)['n']
        if n:
            db.x("UPDATE jobs SET status='queued', worker=NULL, progress=0, message='worker offline - rescheduled', "
                 "attempts=MAX(attempts-1,0) WHERE worker=? AND status='running'", (w['name'],))
            db.event(f"Worker {w['name']} offline: {n} job(s) back in the queue", 'warn')


_sweep = {'t': 0}


def background_loop():
    while True:
        try:
            todo = db.q("SELECT j.id, j.file_id, j.result, j.src_size, f.rel FROM jobs j JOIN files f ON f.id=j.file_id "
                        "WHERE j.kind='encode' AND j.status='replacing'")
            playing = plex_playing() if todo else set()
            for j in todo:
                try: do_replace(j, playing)
                except Exception as e:
                    set_job(j['id'], status='failed', message=f'replace failed: {e}', finished=time.time())
                    db.event(f'Replace failed ({_base(j["rel"])}): {e}', 'error')
            purge_quarantine()
            purge_compares()
            requeue_stale()
            if time.time() - _sweep['t'] > 6 * 3600:
                _sweep['t'] = time.time(); sweep_staging()
        except Exception as e:
            try: db.event(f'Background task failed: {e}', 'error')
            except Exception: pass
        time.sleep(15)


def scan_scheduler():
    time.sleep(3)
    try:
        scanner.sync_discs()
        scanner.replan()               # planning rules may have changed
        n_enc, n_sub = scanner.plan_jobs()   # also on every start: apply new rules (e.g. DVD folders) right away
        if n_enc or n_sub: db.event(f'Planning updated: {n_enc} encode and {n_sub} subtitle jobs added')
    except Exception as e:
        db.event(f'Planning at startup failed: {e}', 'error')
    last = time.time() if db.q('SELECT COUNT(*) n FROM files', one=True)['n'] else 0
    while True:
        try:
            iv = db.setting('scan_interval_min', float) * 60
            if iv > 0 and time.time() - last > iv:
                last = time.time(); scanner.scan()
        except Exception:
            pass                                   # e.g. database locked: try again next minute, never let the thread die
        time.sleep(60)


def _merge_dir(src, dst):
    """Move everything in src into dst (recursively for folders both have); returns True when src is empty afterwards."""
    clean = True
    for name in os.listdir(src):
        s, d = os.path.join(src, name), os.path.join(dst, name)
        if not os.path.lexists(d):
            os.replace(s, d)
        elif os.path.isdir(s) and os.path.isdir(d) and not os.path.islink(s):
            if _merge_dir(s, d): os.rmdir(s)
            else: clean = False
        else:
            db.event(f'Quarantine: {d} already exists, {s} was left where it is', 'warn'); clean = False
    return clean


def _migrate_quarantine_dir():
    """One-off: the quarantine folder used to be called 'quarantaine'. Rename it and fix the stored paths. Safe when the
    media share is not mounted yet (then there is nothing to rename; the next start tries again)."""
    try:
        if not os.path.isdir(QUAR_OLD): return
        if not os.path.lexists(QUAR):
            os.rename(QUAR_OLD, QUAR)
        elif _merge_dir(QUAR_OLD, QUAR):
            os.rmdir(QUAR_OLD)
        old, new = QUAR_OLD + os.sep, QUAR + os.sep
        n = db.xn('UPDATE quarantine SET qpath = REPLACE(qpath, ?, ?) WHERE substr(qpath, 1, ?) = ?', (old, new, len(old), old))
        if os.path.exists(QUAR_OLD): db.event(f'Quarantine folder renamed to quarantine; {n} record(s) updated, but {QUAR_OLD} is not empty yet', 'warn')
        else: db.event(f'Quarantine folder renamed to quarantine ({n} record(s) updated)')
    except Exception as e:
        db.event(f'Renaming the quarantine folder failed (is the media share mounted?): {e}', 'warn')


def startup():
    db.conn()
    if not password_set():
        print(f'\n  MediaScan first-time setup: open the dashboard and enter SETUP CODE {SETUP_CODE}\n', flush=True)
    elif ENV_PASSWORD and not db.auth_get('pw_hash') and len(ENV_PASSWORD) < 10:
        db.event('Note: ADMIN_PASSWORD is shorter than 10 characters - choose a stronger password in Settings', 'error')
    worker_token()
    db.x('UPDATE sessions SET seen=? WHERE seen IS NULL', (time.time(),))     # do not sign out sessions from before the update
    if not db.q("SELECT 1 FROM settings WHERE key='setup_done'", one=True):    # existing installation: skip the welcome tour
        db.set_setting('setup_done', 1 if db.q("SELECT 1 FROM jobs WHERE status='done' LIMIT 1", one=True) else 0)
    if not db.q("SELECT 1 FROM settings WHERE key='mig_vbr_abs'", one=True):  # one-off: fixed minimum bitrate instead of 25%
        if db.setting('min_keep_pct', float) == 25: db.set_setting('min_keep_pct', 0)
        db.set_setting('mig_vbr_abs', 1)
    if os.environ.get('MFA_RESET') == '1':            # once per time the variable is set
        if db.auth_get('mfa_reset_done') != '1':
            for k in ('totp_enabled', 'totp_secret', 'totp_pending', 'recovery'): db.auth_set(k, None)
            db.x('DELETE FROM sessions'); db.auth_set('mfa_reset_done', '1')
            db.event('MFA reset via MFA_RESET - remove MFA_RESET from the container settings again', 'warn')
    else:
        db.auth_set('mfa_reset_done', None)
    _migrate_quarantine_dir()
    os.makedirs(STAGING, exist_ok=True); os.makedirs(QUAR, exist_ok=True)
    threading.Thread(target=background_loop, daemon=True).start()
    threading.Thread(target=scan_scheduler, daemon=True).start()
    db.event('MediaScan started')


@app.get('/api/health')
def health():
    return {'ok': True, 'version': APP_VERSION}


app.mount('/static', StaticFiles(directory=os.path.join(HERE, 'static')), name='static')


_index = {'v': None, 'html': None}


@app.get('/')
def index():
    """index.html with the static files' version filled in ({{v}}): browsers may cache the files for a day, and this
    makes sure a new release is picked up at once."""
    if _index['html'] is None:
        h = hashlib.sha256()
        sd = os.path.join(HERE, 'static')
        for f in ('style.css', 'i18n.js', 'app.js', 'index.html', 'logo.svg', 'logo-256.png'):
            h.update(open(os.path.join(sd, f), 'rb').read())
        for sub in ('locales', 'kb'):
            for f in sorted(os.listdir(os.path.join(sd, sub))):
                h.update(open(os.path.join(sd, sub, f), 'rb').read())
        _index['v'] = h.hexdigest()[:10]
        _index['html'] = open(os.path.join(sd, 'index.html'), encoding='utf-8').read().replace('{{v}}', _index['v'])
    return Response(_index['html'], media_type='text/html')
