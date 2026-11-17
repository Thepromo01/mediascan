"""MediaScan Manager for Windows.

Keeps the worker running (restarts it after a crash or an update), and serves a small local
app on http://127.0.0.1:<port> to see what the worker is doing and to start/stop it.

Standard library only. Runs with pythonw.exe (no console window) from the Windows scheduled task
"MediaScan Manager" (called "MediaScan Beheer" in older installs). Security: listens on 127.0.0.1 only, checks the Host header (against DNS
rebinding), needs a random per-start key to open (handed to the tray icon via a file in the user's
own profile) that the page keeps in sessionStorage and sends as a header on every API call (no
cookies, so other services on 127.0.0.1 never see it), and a JSON body on every action.

Usage:  pythonw manager.py            run (normally started by the scheduled task)
        pythonw manager.py --open     open the app (starts the manager first if it is not running)
"""
import base64, configparser, ctypes, ctypes.wintypes as wt, hashlib, hmac, http.server, json, os, re, secrets, socket, subprocess, sys, threading, time, urllib.error, urllib.parse, urllib.request

VERSION = '1.0.0'
BASE = os.environ.get('MEDIASCAN_HOME') or os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'MediaScan')
LOGS, STATUS, CTL = (os.path.join(BASE, d) for d in ('logs', 'status', 'control'))
CTL_DONE = os.path.join(CTL, 'done')
for d in (LOGS, STATUS, CTL, CTL_DONE): os.makedirs(d, exist_ok=True)
MGR_FILE = os.path.join(STATUS, 'manager.json')      # port + key for the tray icon / --open
STATE_FILE = os.path.join(STATUS, 'state.json')      # desired state per job (survives a restart)
STATUS_FILE = os.path.join(STATUS, 'status.json')    # heartbeat for the tray icon
WORKER_STATUS = os.path.join(STATUS, 'worker.json')  # written by worker.py
PORTS = range(8790, 8800)
LOG_MAX = 20 * 1024 * 1024
NO_WINDOW = 0x08000000
ME = os.path.abspath(__file__)
ME_MTIME = os.path.getmtime(ME)
TRAY = os.path.join(BASE, 'tray.ps1')
TRAY_MTIME = os.path.getmtime(TRAY) if os.path.exists(TRAY) else 0


MLOG = os.path.join(LOGS, 'manager.log')
LEGACY_LOGS = ('beheer.log', 'beheer-herstart.log', 'tray-herstart.log', 'opruimen.log')     # Dutch names of older versions
# Files that older versions put in the MediaScan folder and that nothing uses any more: the PowerShell service and its
# helper scripts, the separate subtitle installer, the 1.0 migration script and the old job/control files.
LEGACY_FILES = ('supervisor.ps1', 'herstart-beheer.ps1', 'opruimen.ps1', 'install-beheer.ps1', 'Install-Beheer.bat',
                'install-subs.ps1', 'Install-Subs.bat', 'Test-Subs.bat', 'migrate-1.0.ps1', 'control.json', 'jobs.json')


def migrate_logs():
    """beheer.log keeps its history under the new name manager.log; other old log files are removed."""
    try:
        old = os.path.join(LOGS, 'beheer.log')
        if os.path.exists(old):
            with open(old, 'rb') as a, open(MLOG, 'ab') as b: b.write(a.read())
            os.remove(old)
        for n in LEGACY_LOGS[1:]:
            try: os.remove(os.path.join(LOGS, n))
            except OSError: pass
    except OSError:
        pass


def remove_legacy_files():
    """Remove files of older versions (see LEGACY_FILES) and status files a crashed worker left half-written."""
    gone = []
    for n in LEGACY_FILES:
        p = os.path.join(BASE, n)
        if os.path.exists(p):
            try: os.remove(p); gone.append(n)
            except OSError: pass
    try:
        for n in os.listdir(STATUS):
            if n.endswith('.tmp'):
                p = os.path.join(STATUS, n)
                if time.time() - os.path.getmtime(p) > 300:
                    try: os.remove(p); gone.append('status/' + n)
                    except OSError: pass
    except OSError:
        pass
    if gone: mlog('removed files of an older version: ' + ', '.join(gone))


def mlog(msg):
    try:
        with open(MLOG, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%dT%H:%M:%S') + '  ' + msg + '\n')
    except OSError:
        pass


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8-sig') as f: return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f: json.dump(obj, f, indent=1)
    os.replace(tmp, path)


def tail(path, n=200, max_bytes=256 * 1024):
    try:
        with open(path, 'rb') as f:
            f.seek(0, 2); size = f.tell(); f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.decode('utf-8', 'replace').replace('\r', '\n').split('\n')
    return [l for l in lines if l.strip()][-n:]


# ------------------------------------------------------------------ Windows job objects
# Every started job gets its own job object, so "stop" ends the whole process tree (ffmpeg, ollama
# clients ...) and nothing keeps running if the manager itself disappears.
k32 = ctypes.WinDLL('kernel32', use_last_error=True)


class _BASIC(ctypes.Structure):
    _fields_ = [('PerProcessUserTimeLimit', ctypes.c_int64), ('PerJobUserTimeLimit', ctypes.c_int64), ('LimitFlags', wt.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t), ('MaximumWorkingSetSize', ctypes.c_size_t), ('ActiveProcessLimit', wt.DWORD),
                ('Affinity', ctypes.c_size_t), ('PriorityClass', wt.DWORD), ('SchedulingClass', wt.DWORD)]


class _IO(ctypes.Structure):
    _fields_ = [(n, ctypes.c_uint64) for n in ('r', 'w', 'o', 'rb', 'wb', 'ob')]


class _EXT(ctypes.Structure):
    _fields_ = [('Basic', _BASIC), ('Io', _IO), ('ProcessMemoryLimit', ctypes.c_size_t), ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t), ('PeakJobMemoryUsed', ctypes.c_size_t)]


k32.CreateJobObjectW.restype = wt.HANDLE
k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
k32.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
k32.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
k32.TerminateJobObject.argtypes = [wt.HANDLE, wt.UINT]
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CreateMutexW.restype = wt.HANDLE
k32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]


def new_job_object():
    h = k32.CreateJobObjectW(None, None)
    if not h: return None
    info = _EXT(); info.Basic.LimitFlags = 0x2000          # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
        mlog(f'SetInformationJobObject failed ({ctypes.get_last_error()})')
    return h


k32.OpenMutexW.restype = wt.HANDLE
k32.OpenMutexW.argtypes = [wt.DWORD, wt.BOOL, wt.LPCWSTR]


def old_service_running():
    """The PowerShell service of MediaScan < 1.0 (supervisor.ps1) still runs: it owns the worker until the next sign-in."""
    h = k32.OpenMutexW(0x00100000, False, 'Local\\MediaScanBeheer')     # SYNCHRONIZE
    if h: k32.CloseHandle(h); return True
    return False


def single_instance():
    h = k32.CreateMutexW(None, False, 'Local\\MediaScanManager')
    return h if h and ctypes.get_last_error() != 183 else None      # 183 = ERROR_ALREADY_EXISTS


# ------------------------------------------------------------------ jobs
def job_defs():
    """Only the worker. jobs.json of older installs may hold other one-off entries; those are ignored."""
    py = os.path.join(BASE, 'venv', 'Scripts', 'python.exe')
    return {'worker': {'cmd': f'"{py}" -u "{os.path.join(BASE, "worker.py")}"', 'autorestart': True}}


class Job:
    def __init__(self, name, spec, wanted):
        self.name, self.spec, self.wanted = name, spec, wanted
        self.proc = self.handle = None
        self.started = self.ended = self.exitcode = None
        self.restarts = 0; self.recent = []; self.gave_up = False; self.next_start = 0.0; self.note = ''
        self.last_restart = None          # {'at': ts, 'why': 'crash' | 'update' | 'manual'}: the restarts counter only counts crashes

    @property
    def running(self): return self.proc is not None and self.proc.poll() is None

    def start(self):
        if self.running: return 'already running'
        log = os.path.join(LOGS, f'{self.name}.log')
        try:
            if os.path.getsize(log) > LOG_MAX: os.replace(log, log + '.1')
        except OSError:
            pass
        out = open(log, 'ab')
        out.write(('\r\n===== START ' + time.strftime('%Y-%m-%dT%H:%M:%S') + ' =====\r\n').encode())
        out.flush()
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUNBUFFERED='1')
        cmd = os.path.expandvars(self.spec['cmd'])
        try:
            self.proc = subprocess.Popen(cmd, cwd=BASE, stdout=out, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                         env=env, creationflags=NO_WINDOW | 0x00000200)   # + CREATE_NEW_PROCESS_GROUP
        except OSError as e:
            out.close(); self.note = str(e); mlog(f'start {self.name} failed: {e}'); return 'start failed'
        out.close()
        self.handle = new_job_object()
        ph = getattr(self.proc, '_handle', None)
        if self.handle and ph and not k32.AssignProcessToJobObject(self.handle, wt.HANDLE(int(ph))):
            mlog(f'AssignProcessToJobObject failed ({ctypes.get_last_error()}) - stop falls back to taskkill')
        self.started, self.ended, self.exitcode, self.note = time.time(), None, None, ''
        mlog(f'start {self.name} (pid {self.proc.pid})')
        return 'started'

    def stop(self):
        if not self.running: self._close(); return 'not running'
        pid = self.proc.pid
        if self.handle: k32.TerminateJobObject(self.handle, 1)
        try:
            self.proc.wait(15)
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True, creationflags=NO_WINDOW)
            try: self.proc.wait(10)
            except subprocess.TimeoutExpired: pass
        ok = self.proc.poll() is not None
        self.exitcode = self.proc.poll(); self.ended = time.time()
        if ok: self._close()
        mlog(f'stop {self.name}: {"ok" if ok else "FAILED"}')
        return 'stopped' if ok else 'stop failed'

    def _close(self):
        if self.handle: k32.CloseHandle(self.handle)
        self.handle = None
        if self.proc is not None and self.proc.poll() is not None: self.proc = None


class Manager:
    def __init__(self):
        self.lock = threading.RLock()
        saved = read_json(STATE_FILE, {}) or {}
        self.jobs = {n: Job(n, s, saved.get(n, 'running') if saved.get(n) in ('running', 'stopped') else 'running')
                     for n, s in job_defs().items()}
        self.started = time.time(); self.quit = threading.Event(); self.port = None; self.respawn_after = False
        self.key = secrets.token_urlsafe(32)

    def save_state(self):
        try: write_json(STATE_FILE, {n: j.wanted for n, j in self.jobs.items()})
        except OSError: pass

    def act(self, action, name='worker'):
        with self.lock:
            if action == 'quit':
                for j in self.jobs.values(): j.stop()
                self.quit.set(); self.write_status(); return 'quitting'
            j = self.jobs.get(name)
            if not j: return f'unknown job {name!r}'
            if action == 'start':
                j.wanted = 'running'; j.gave_up = False; j.recent = []; r = j.start()
            elif action == 'stop':
                j.wanted = 'stopped'; r = j.stop()
            elif action == 'restart':
                r1 = j.stop(); time.sleep(1); j.wanted = 'running'; j.gave_up = False; r = f'{r1}, {j.start()}'
                j.last_restart = {'at': time.time(), 'why': 'manual'}
            else:
                return f'unknown action {action!r}'
            self.save_state(); self.write_status()
            return r

    def control_files(self):
        """Commands dropped in control\\*.json: {"action":"start|stop|restart|quit","job":"worker"} (tray icon, scripts)."""
        try: files = sorted((f for f in os.scandir(CTL) if f.is_file() and f.name.endswith('.json')), key=lambda f: f.stat().st_mtime)
        except OSError: return
        for f in files:
            try: age = time.time() - f.stat().st_mtime
            except OSError: continue
            c = read_json(f.path, {}) or {}
            if age > 120:                                  # left over from before this start (e.g. a quit while we were down)
                res = 'ignored (too old)'
            else:
                    res = self.act(str(c.get('action', '')), str(c.get('job') or 'worker')) if isinstance(c, dict) else 'invalid'
            mlog(f'command {f.name}: {res}')
            try:
                with open(os.path.join(CTL_DONE, time.strftime('%Y%m%d-%H%M%S_') + f.name[:-5] + '.txt'), 'w', encoding='utf-8') as o: o.write(res)
                os.remove(f.path)
            except OSError:
                pass

    def watch(self):
        now = time.time()
        with self.lock:
            for j in self.jobs.values():
                if j.proc is not None and j.proc.poll() is not None:
                    j.exitcode = j.proc.returncode; j.ended = now; j._close()
                    mlog(f'{j.name} exited with code {j.exitcode}')
                    if j.exitcode == 3:                       # worker updated itself
                        j.last_restart = {'at': now, 'why': 'update'}
                        if os.path.getmtime(ME) != ME_MTIME or (os.path.exists(TRAY) and os.path.getmtime(TRAY) != TRAY_MTIME):  # ... and the manager/tray too: restart the manager first
                            self.respawn(); return
                        j.next_start = now + 2
                    elif j.wanted == 'running' and j.spec.get('autorestart', True):
                        j.recent = [t for t in j.recent if now - t < 3600]
                        if len(j.recent) >= 5:
                            j.gave_up = True; mlog(f'{j.name} keeps crashing - automatic restart paused')
                        else:
                            j.recent.append(now); j.restarts += 1; j.next_start = now + 10; j.last_restart = {'at': now, 'why': 'crash'}
                if j.wanted == 'running' and not j.running and not j.gave_up and now >= j.next_start:
                    if j.start() == 'start failed': j.next_start = now + 30

    def respawn(self):
        mlog('manager updated - restarting the manager')
        for j in self.jobs.values(): j.stop()
        self.respawn_after = True; self.quit.set()

    def snapshot(self):
        with self.lock:
            jobs = {n: {'running': j.running, 'pid': j.proc.pid if j.running else None, 'wanted': j.wanted,
                        'started': j.started, 'ended': j.ended, 'exitcode': j.exitcode, 'restarts': j.restarts,
                        'last_restart': j.last_restart, 'gave_up': j.gave_up, 'note': j.note} for n, j in self.jobs.items()}
        w = read_json(WORKER_STATUS, None); wj = self.jobs.get('worker')
        if w and not (wj and wj.running and time.time() - w.get('at', 0) < 90 and w.get('at', 0) >= (wj.started or 0) - 5): w = None   # stale file
        return {'version': VERSION, 'host': socket.gethostname(), 'started': self.started, 'now': time.time(),
                'jobs': jobs, 'worker': w}

    def write_status(self):
        s = self.snapshot()
        o = {'heartbeat': time.strftime('%Y-%m-%dT%H:%M:%S'), 'host': s['host'], 'manager': VERSION, 'port': self.port,
             'quit': self.quit.is_set() and not self.respawn_after, 'worker': s['worker'], 'jobs': {}}
        for n, j in s['jobs'].items():
            o['jobs'][n] = dict(j, started=_iso(j['started']), ended=_iso(j['ended']), tail=tail(os.path.join(LOGS, f'{n}.log'), 8))
        try: write_json(STATUS_FILE, o)
        except OSError: pass

    def loop(self):
        last = 0
        while not self.quit.is_set():
            try:
                self.control_files(); self.watch()
                if time.time() - last >= 3: last = time.time(); self.write_status()
            except Exception as e:                      # one bad round must never stop the manager
                mlog(f'error in manager loop: {e!r}')
            self.quit.wait(1)


def _iso(t): return time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(t)) if t else None


def cleanup_leftovers():
    """A worker left behind by an older manager (or the old PowerShell service) would run twice; an old tray icon
    holds the tray mutex, so it is replaced by a fresh one (start_tray runs right after this)."""
    ps = ("Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe' OR Name='powershell.exe'\" | "
          "Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and ($_.CommandLine -like '*MediaScan*worker.py*' -or $_.CommandLine -like '*MediaScan*supervisor.ps1*' -or $_.CommandLine -like '*MediaScan*tray.ps1*') } | "
          "ForEach-Object { $_.ProcessId }")
    try:
        r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
        for pid in r.stdout.split():
            if pid.isdigit() and int(pid) != os.getpid():
                subprocess.run(['taskkill', '/PID', pid, '/T', '/F'], capture_output=True, creationflags=NO_WINDOW)
                mlog(f'removed leftover process {pid}')
    except Exception as e:
        mlog(f'cleanup failed: {e!r}')


# ------------------------------------------------------------------ local app (HTTP)
ICON_DIR = os.path.join(BASE, 'icons')


def write_icons():
    """Write the icon files (mediascan.ico for the Start menu shortcut, tray-*.ico for the tray) and point an older
    shortcut (shell32 icon) at mediascan.ico. Runs at every start; only changed files are written."""
    try:
        os.makedirs(ICON_DIR, exist_ok=True)
        for name, data in ICONS.items():
            p = os.path.join(ICON_DIR, name); raw = base64.b64decode(data)
            try:
                with open(p, 'rb') as f:
                    if f.read() == raw: continue
            except OSError:
                pass
            with open(p + '.new', 'wb') as f: f.write(raw)
            os.replace(p + '.new', p)
        # shortcuts point at a copy named after the icon version: Explorer caches icons by path, so a changed
        # icon under the same name would keep showing the old picture
        vico = os.path.join(ICON_DIR, f'mediascan-{ICON_V}.ico')
        if not os.path.exists(vico):
            with open(vico, 'wb') as f: f.write(base64.b64decode(ICONS['mediascan.ico']))
        for f in os.listdir(ICON_DIR):
            if f.startswith('mediascan-') and f.endswith('.ico') and f != os.path.basename(vico):
                try: os.remove(os.path.join(ICON_DIR, f))
                except OSError: pass
        mark = os.path.join(ICON_DIR, f'.shortcuts-{ICON_V}')
        if os.path.exists(mark): return
        appdata = os.environ.get('APPDATA', '')
        lnks = [os.path.join(appdata, r'Microsoft\Windows\Start Menu\Programs\MediaScan.lnk')]
        pinned = os.path.join(appdata, r'Microsoft\Internet Explorer\Quick Launch\User Pinned\TaskBar')
        if os.path.isdir(pinned): lnks += [os.path.join(pinned, f) for f in os.listdir(pinned) if f.lower().endswith('.lnk')]
        ico = vico
        for lnk in lnks:
            if not os.path.exists(lnk): continue
            # only shortcuts that start this manager; a pinned item of another app is left alone
            q = lambda v: v.replace("'", "''")          # PowerShell single-quoted string
            ps = (f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{q(lnk)}'); "
                  f"if ($s.Arguments -like '*manager.py*' -and $s.IconLocation -ne '{q(ico)},0') {{ $s.IconLocation='{q(ico)},0'; $s.Save(); 'changed' }}")
            r = subprocess.run(['powershell', '-NoProfile', '-NonInteractive', '-Command', ps], capture_output=True, text=True, timeout=60, creationflags=NO_WINDOW)
            if 'changed' in (r.stdout or ''): mlog(f'icon of shortcut {os.path.basename(lnk)} updated')
        open(mark, 'w').close()
    except Exception as e:
        mlog(f'icons: {e!r}')


# ------------------------------------------------------------------ settings (worker.ini)
INI = os.path.join(BASE, 'worker.ini')
CFG_FILES = [os.path.join(BASE, f) for f in ('config.ini', 'worker.ini', 'overrides.ini')]     # same order as the worker
URL_RE = re.compile(r'^https?://[A-Za-z0-9.\-\[\]:]+$')


def _cfg():
    c = configparser.ConfigParser(interpolation=None); c.read(CFG_FILES, encoding='utf-8'); return c


def configured():
    c = _cfg(); return bool(c.get('server', 'url', fallback='') and c.get('server', 'worker_token', fallback=''))


def ollama_models(url):
    try:
        with urllib.request.urlopen(url.rstrip('/') + '/api/tags', timeout=2) as r:
            return sorted(m.get('name', '') for m in json.load(r).get('models', []) if m.get('name'))
    except Exception:
        return []


def read_settings():
    c = _cfg(); g = lambda sec, key, d='': c.get(sec, key, fallback=d)
    media = g('worker', 'media_root') or 'M:\\'
    ourl = g('translation', 'ollama_url') or 'http://localhost:11434'
    return {'url': g('server', 'url'), 'token': g('server', 'worker_token'), 'name': g('worker', 'name') or socket.gethostname(),
            'media': media, 'media_ok': os.path.isdir(media), 'max_height': g('worker', 'max_height') or '0',
            'subs': g('worker', 'subs', 'true').strip().lower() == 'true',
            'os_key': g('opensubtitles', 'api_key'), 'os_user': g('opensubtitles', 'username'), 'os_pw_set': bool(g('opensubtitles', 'password')),
            'whisper': g('whisper', 'model') or 'large-v3', 'ollama_url': ourl,
            'ollama_model': g('translation', 'ollama_model') or g('vertaling', 'ollama_model') or 'gemma3:4b',   # [vertaling] = old section name
            'ollama_models': ollama_models(ourl), 'configured': bool(g('server', 'url') and g('server', 'worker_token'))}


def save_settings(d):
    """Write the fields of the app's Settings form to worker.ini (other keys in the file are kept)."""
    url = str(d.get('url', '')).strip().rstrip('/')
    if url and not URL_RE.match(url): return 'invalid dashboard address'
    media = str(d.get('media', '')).strip()
    if re.fullmatch(r'[A-Za-z]:', media): media += '\\'
    name = re.sub(r'[^A-Za-z0-9._ -]', '-', str(d.get('name', ''))).strip(' ._-')[:64]
    try: maxh = max(0, int(d.get('max_height') or 0))
    except (TypeError, ValueError): return 'invalid resolution'
    c = configparser.ConfigParser(interpolation=None); c.read(INI, encoding='utf-8')
    def put(sec, key, val):
        if not c.has_section(sec): c.add_section(sec)
        val = '' if val is None else str(val).strip()
        if val: c.set(sec, key, val)
        elif c.has_option(sec, key): c.remove_option(sec, key)
    put('server', 'url', url); put('server', 'worker_token', str(d.get('token', '')).strip())
    put('worker', 'name', name); put('worker', 'media_root', media); put('worker', 'max_height', maxh)
    put('worker', 'subs', 'true' if d.get('subs') else 'false')
    put('opensubtitles', 'api_key', d.get('os_key')); put('opensubtitles', 'username', d.get('os_user'))
    if str(d.get('os_pw', '')).strip(): put('opensubtitles', 'password', d.get('os_pw'))      # empty = keep the current password
    put('whisper', 'model', d.get('whisper') or 'large-v3')
    put('translation', 'ollama_url', d.get('ollama_url') or 'http://localhost:11434'); put('translation', 'ollama_model', d.get('ollama_model') or 'gemma3:4b')
    if c.has_section('vertaling'): c.remove_section('vertaling')
    new = not os.path.exists(INI)
    with open(INI, 'w', encoding='utf-8') as f: c.write(f)
    if new:     # only this user may read the file (keys and password)
        subprocess.run(['icacls', INI, '/inheritance:r', '/grant:r', f"{os.environ.get('USERNAME', '')}:(R,W)"], capture_output=True, creationflags=NO_WINDOW)
    return 'ok'


def _server_call(url, token, path, data=None):
    req = urllib.request.Request(url.rstrip('/') + path, data=data, method='POST' if data is not None else 'GET',
                                 headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r: return json.load(r)


def test_server(url, token):
    url = str(url).strip().rstrip('/'); token = str(token).strip()
    if not URL_RE.match(url): return {'ok': False, 'error': 'invalid address'}
    try:
        r = _server_call(url, token, '/api/worker/update', b'{}')
        return {'ok': True, 'version': r.get('version')}
    except urllib.error.HTTPError as e:
        return {'ok': False, 'error': 'key not accepted' if e.code == 401 else f'HTTP {e.code}'}
    except Exception as e:
        return {'ok': False, 'error': str(getattr(e, 'reason', e))[:120]}


def find_media(url, token):
    """The drive that holds the library's top folders (the dashboard says which), e.g. 'M:\\'."""
    try: roots = [r for r in _server_call(str(url).strip(), str(token).strip(), '/api/worker/roots').get('roots', []) if r][:50]
    except Exception: roots = []
    if not roots: return {'path': '', 'roots': roots}
    mask = ctypes.windll.kernel32.GetLogicalDrives(); best, best_n = '', 0
    for i in range(26):
        if not mask & (1 << i): continue
        root = f'{chr(65 + i)}:\\'
        try: n = sum(os.path.isdir(os.path.join(root, r)) for r in roots)
        except OSError: n = 0
        if n > best_n: best, best_n = root, n
    return {'path': best if best_n >= max(1, -(-len(roots) // 2)) else '', 'roots': roots}


def map_share(path, letter):
    path = str(path).strip(); letter = str(letter).strip().rstrip(':').upper()
    if not re.fullmatch(r'\\\\[^\\/:*?"<>|]+\\[^\\/:*?"<>|]+(\\[^\\/:*?"<>|]+)*', path): return {'ok': False, 'error': 'not a \\\\server\\share path'}
    if not re.fullmatch(r'[D-Z]', letter): return {'ok': False, 'error': 'drive letter must be D to Z'}
    if os.path.exists(f'{letter}:\\'): return {'ok': False, 'error': f'{letter}: is already in use'}
    try:
        r = subprocess.run(['net', 'use', f'{letter}:', path, '/persistent:yes'], capture_output=True, text=True, errors='replace', timeout=60, creationflags=NO_WINDOW)
    except Exception as e:
        return {'ok': False, 'error': str(e)[:120]}
    out = ' '.join((r.stdout + ' ' + r.stderr).split())[:200]
    return {'ok': r.returncode == 0, 'path': f'{letter}:\\', 'error': '' if r.returncode == 0 else (out or f'net use failed ({r.returncode})')}


def _browser():
    """The default browser when it can show a page as its own window (Chromium family: Brave, Chrome, Edge, ...), else Edge."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows\Shell\Associations\UrlAssociations\http\UserChoice') as k:
            prog = winreg.QueryValueEx(k, 'ProgId')[0]
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, prog + r'\shell\open\command') as k:
            cmd = winreg.QueryValueEx(k, None)[0]
        m = re.match(r'^"([^"]+)"|^(\S+)', cmd); exe = (m.group(1) or m.group(2)) if m else ''
        if exe and os.path.exists(exe) and re.search(r'(chrome|brave|msedge|vivaldi|opera|chromium)\.exe$', exe, re.I): return exe
    except Exception:
        pass
    for exe in (os.path.expandvars(r'%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe'),
                os.path.expandvars(r'%ProgramFiles%\Microsoft\Edge\Application\msedge.exe')):
        if os.path.exists(exe): return exe
    return None


def open_app(port, key):
    url = f'http://127.0.0.1:{port}/#k={urllib.parse.quote(key)}'
    exe = _browser()
    if exe: subprocess.Popen([exe, f'--app={url}', '--window-size=880,820'], creationflags=NO_WINDOW); return
    os.startfile(url)


def start_tray():
    tray = os.path.join(BASE, 'tray.ps1')
    if not os.path.exists(tray): return
    conhost = os.path.join(os.environ.get('SystemRoot', r'C:\Windows'), 'System32', 'conhost.exe')
    subprocess.Popen([conhost, '--headless', 'powershell.exe', '-NoProfile', '-STA', '-ExecutionPolicy', 'Bypass',
                      '-WindowStyle', 'Hidden', '-File', tray], creationflags=NO_WINDOW)


def make_handler(mgr):
    allowed_hosts = {f'127.0.0.1:{mgr.port}', f'localhost:{mgr.port}'}
    allowed_origins = {'http://' + h for h in allowed_hosts}

    class H(http.server.BaseHTTPRequestHandler):
        server_version = 'MediaScan'; sys_version = ''

        def log_message(self, *a): pass

        def send(self, code, body=b'', ctype='text/plain; charset=utf-8', extra=None):
            if isinstance(body, str): body = body.encode()
            self.send_response(code)
            self.send_header('Content-Type', ctype); self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store'); self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('X-Frame-Options', 'DENY'); self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                                        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            for k, v in (extra or {}).items(): self.send_header(k, v)
            self.end_headers()
            if self.command != 'HEAD': self.wfile.write(body)

        def host_ok(self):
            if self.headers.get('Host', '') not in allowed_hosts: self.send(421, 'wrong host'); return False
            return True

        def authed(self):
            return hmac.compare_digest(self.headers.get('X-Key') or '', mgr.key)

        def do_GET(self):
            if not self.host_ok(): return
            u = urllib.parse.urlsplit(self.path); q = urllib.parse.parse_qs(u.query)
            if u.path == '/': return self.send(200, PAGE.replace('__ICON_V__', ICON_V), 'text/html; charset=utf-8')      # the shell holds no data
            if u.path == '/app.css': return self.send(200, CSS, 'text/css; charset=utf-8')
            if u.path == '/app.js': return self.send(200, JS, 'text/javascript; charset=utf-8')
            if u.path in ('/icon.svg', '/logo.svg'): return self.send(200, ICON, 'image/svg+xml')
            if u.path in ('/icon-256.png', '/logo-256.png'): return self.send(200, ICON_PNG, 'image/png')
            if not self.authed(): return self.send(401, 'login required')
            if u.path == '/api/state': return self.send(200, json.dumps(mgr.snapshot()), 'application/json')
            if u.path == '/api/settings': return self.send(200, json.dumps(read_settings()), 'application/json')
            if u.path == '/api/log':
                which = 'manager' if q.get('which', [''])[0] == 'manager' else 'worker'
                n = max(20, min(1000, int(q.get('n', ['300'])[0]) if q.get('n', [''])[0].isdigit() else 300))
                return self.send(200, json.dumps({'lines': tail(os.path.join(LOGS, which + '.log'), n)}), 'application/json')
            self.send(404, 'not found')

        do_HEAD = do_GET

        def do_POST(self):
            if not self.host_ok(): return
            origin = self.headers.get('Origin')
            if (origin and origin not in allowed_origins) or not self.authed() \
                    or not (self.headers.get('Content-Type') or '').startswith('application/json'):
                return self.send(403, 'forbidden')
            try:
                n = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(min(n, 16384)) or b'{}')
            except Exception:
                return self.send(400, 'bad request')
            if self.path != '/api/action' or not isinstance(body, dict): return self.send(404, 'not found')
            a = str(body.get('action', ''))
            if a in ('start', 'stop', 'restart', 'quit'):
                res = mgr.act(a, 'worker')
            elif a == 'open_folder':
                os.startfile(BASE); res = 'ok'
            elif a == 'open_log':
                os.startfile(os.path.join(LOGS, 'manager.log' if body.get('which') == 'manager' else 'worker.log')); res = 'ok'
            elif a == 'save_settings':
                res = save_settings(body)
                if res == 'ok' and body.get('restart') and mgr.jobs['worker'].running(): mgr.act('restart', 'worker')
            elif a == 'test_server':
                res = test_server(body.get('url', ''), body.get('token', ''))
            elif a == 'find_media':
                res = find_media(body.get('url', ''), body.get('token', ''))
            elif a == 'map_share':
                res = map_share(body.get('path', ''), body.get('letter', 'M'))
            else:
                return self.send(400, 'unknown action')
            self.send(200, json.dumps({'result': res}), 'application/json')

    return H


class LocalServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = False          # on Windows SO_REUSEADDR would let two servers share a port
    daemon_threads = True

    def server_bind(self):
        if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve(mgr):
    for p in PORTS:
        try:
            mgr.port = p
            srv = LocalServer(('127.0.0.1', p), make_handler(mgr))
            return srv
        except OSError:
            continue
    raise SystemExit('no free port for the local app')


def main():
    if '--icons' in sys.argv: write_icons(); return        # used by the installer before it creates the shortcut
    want_open = '--open' in sys.argv
    mutex = single_instance()
    if not mutex:                                   # already running: just open the app
        if want_open:
            m = read_json(MGR_FILE, {}) or {}
            if m.get('port') and m.get('key'): open_app(m['port'], m['key'])
        return
    if old_service_running():
        mlog('old MediaScan service (supervisor.ps1) is still running - the manager takes over at the next sign-in')
        k32.CloseHandle(mutex); return
    migrate_logs()
    mlog(f'manager {VERSION} started')
    remove_legacy_files()
    write_icons()
    cleanup_leftovers()
    mgr = Manager()
    srv = serve(mgr)
    old = os.umask(0o077)
    try: write_json(MGR_FILE, {'port': mgr.port, 'key': mgr.key, 'pid': os.getpid(), 'version': VERSION})
    finally: os.umask(old)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    start_tray()
    if want_open or not configured(): open_app(mgr.port, mgr.key)      # unfinished setup: show the Settings form right away
    mgr.loop()
    mgr.write_status()
    srv.shutdown(); srv.server_close()
    try: os.remove(MGR_FILE)
    except OSError: pass
    mlog('manager stopped')
    if mgr.respawn_after:
        k32.CloseHandle(mutex)
        subprocess.Popen([sys.executable, ME], creationflags=NO_WINDOW, close_fds=True)


# ------------------------------------------------------------------ the app (same look as the dashboard)
ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32"><rect width="32" height="32" rx="8" fill="#ff8a1f"/><path d="M5 12.6h22v10.9a2.5 2.5 0 0 1-2.5 2.5h-17A2.5 2.5 0 0 1 5 23.5z" fill="#141414"/><g transform="rotate(-12 6.4 12.4)"><clipPath id="c12"><rect x="5.2" y="6.999999999999999" width="21.8" height="5.2" rx="1.3"/></clipPath><rect x="5.2" y="6.999999999999999" width="21.8" height="5.2" rx="1.3" fill="#141414"/><g clip-path="url(#c12)"><path d="M8.98 12.20L11.18 7.00h2.13L11.12 12.20z" fill="#ff8a1f"/><path d="M14.06 12.20L16.26 7.00h2.13L16.19 12.20z" fill="#ff8a1f"/><path d="M19.13 12.20L21.33 7.00h2.13L21.27 12.20z" fill="#ff8a1f"/><path d="M24.21 12.20L26.41 7.00h2.13L26.34 12.20z" fill="#ff8a1f"/></g></g><circle cx="6.7" cy="13.9" r="1" fill="#ff8a1f"/><path d="M9.5 15.9l3.2 3.6-3.2 3.6M22.5 15.9l-3.2 3.6 3.2 3.6" fill="none" stroke="#ff8a1f" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/><rect x="15.1" y="15.7" width="1.8" height="7.6" rx=".9" fill="#ff8a1f"/></svg>'
ICON_PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAQAAAAEACAYAAABccqhmAAAQAElEQVR4nOydCZxUxbX/f3W7e1aQdWCAYYZFWUTWARRRATEJUVRcUGJiXOPfqH8Toyzqi2Ly'
    'VBbjM774fCYvL6J5CaBIJInicwETMEZAEBUUZZkFGBj2ZdbuW6/O7R5mGGaYme6+t6t6zvfz6enumZ6Ze6vq/Krq1KlTfiQh8sncDhAYpF4OhCUGQmKAet8R'
    'Umao5wz1PgMQ9LodGEbisPpSptpDmXqtngU9H1Dvv4AtN8PCJoSwSTxYeBBJhkASIOf2mAD4xqmKu1BV3CB1V13BMPFGYo9qW5+rjuTvsOyVYnrxShiOcQIg'
    'Z/dKQ6Y8X1XCOHX56oHz1CMVDOM9lerxoWqLK0kQcNz/oZi9owIGYYQAyNlqEJaZezFsXKeu+Cp12Z3BMNoh96kvr6mRwmKUF64Qs1WL1RxtBcAx+oycC9XQ'
    'fqp6e416ZINhzKFEPZYA9iKUFa3WVQy0FAA5L3eyepqlLm8sGMZ0pFwFaT8hZhW/Cc3QRgDkYviwPW+aejVTOfMGg2GSj41qRDAHvYoWi+vUuoIGaCEAck7P'
    'b8GynlEvB4Bhkh+1vGj/WMwqegsJJqECIOd27w8RIMOfBIZpfSxXFniXmF6wHQkiIQIg53ZuC2T8q3p5lxru+8EwrZdK5SN4Gih7UszcdxQe46kAOJ799Lxb'
    '1MvHOViHYepAQUawH0R50QIvVww8EwD5eJeuCKQvUi/HgWGYxngfIXmVV2HHngiAcvJdpJx8ZPy8ls8wTSFRCJ99rXigaA1cxoKLSAkh5+Y+CEu8BzZ+hmke'
    'ArkIWavkvLy74TKujQDC6/q5zysn3w/AMEx0SPkb9C78oVtxA64IgHL2pSAjd4n685PBMEyMyL+grPAa5RysQpyJuwDIOR3aQZyxVP3lCWAYJj5IrEB59WQx'
    'e1cZ4khcBUA+kZ0FX8q7HMrLMC4g5UcIVU0WD5WUIk7ETQDk3B45gG+FMv4zwTCMO0j5NRCaIGbuLEYciIsAyKdzOqLaWqOMvw8YhnEZuRV+e7T4SfEBxEjM'
    'y4Dyuaw2CFrL2fgZxitEX9XhvunYXozEJADOUt/xjD+rCxoFhmG8Q4jRyvZed2wwBqIWAArywY68l9XL8WAYJhFcjB25Lzq2GCXRjwDm5T6qvn4HDMMkEPG9'
    'iC1G99uIAjkvZxyk9Z4ahrgaSswwbnOgzMbGPTaKjtjYr1bYz+pkoZ960LMxSGnDsidGk6a8xQIg53ftAjttI2/nZUyjvNrGhhIb60sk1u0KOY+djezAz8oA'
    '7sgP4LYRAbRNNSB5Nm0ntiqGiOl79rbk11p0Z85cY34ebewZD4bRnE17lZHvDjk9/LpdEp/ubfk2+4wA8MykFFw1MADtoWjBGQUThVCvmknLBGBu7hw17J8J'
    'htGMXWoI//Fu2zH49SXhnv54HCPnf3VpCq4/xwQRkI+JmYWzm/vxZguAnJc3Qn39SP1KTMsODBMrx6vI2KV6hIfxZPh7jsN1nrwkBbeP0F0EZEiJwHliZtHa'
    '5ny6WQIQPqQjl4w/HwzjISFb4vO94d7dMXj1/NV+GxKJmZevujUd/Tvr7iCUa1BWeF5zUos1LyFneu69bPyMFxQeigzjneF82ENfEaz/qcQ55X6zrgpPfSsN'
    'eiNGIb3nHUDRfzb5yaY+EInzL1Bz/5jDDhmmLkcrJdZGvPE1nvn95dCadNVlfn53hgErA3IfgqEB4qGd+0/3qaZHANXW02z8TDxYu5MMnQw+3LN/daDZzmpt'
    'KFejkZc3BnHXKN19AaIzfL5fqBc3n/ZTp/uhE/AD30owTAvZeiAUmbfbTs/+mZrHV9vGnUbfIBfmWnhtWjqMQGCimF7wXmM/bmIE4JsLhmmCg+U21uwMnfDM'
    'k9Efrqz/qeQwfoKWGY1B4gn19bzGftxorcj5OeMhfSvAMHWoDEp8UhL2xtc46goPmzeUj5UPbks3J1xYhCY0Fibc+AjAtmYlkWgzUSClxJb9tcN4ctR9vjeE'
    'kOSGsV6NdIwRALJlYGVDP2qwJuVTuflqBXENbToG02ooPU5D+dr19g2qpz9WxU2gIW4b7secb6TCDJSSSzm6oeCghkcANqaz8Sc3ZVXKwPfIE5F09Lz7WP1P'
    'cRNoDBJJc1C2LPCAejHtlJ/U/4by/J8FaW3iU3uTB9uW2LzPjhh72FH3pRra2zyUjxq/Gv0X3peBgM+QMpQyCH9okLh/55a63z7VyKX1XTZ+syk+XDuMJ0fd'
    'J2rNvay6/qfY+GMhqBYCaGlzeDdDtsaQTQd9N6hXs+t+uwFDF9PAGMOxSumEztZ11O093vq88omAytwYAXBwbHv2Sd+p+0bO6zFMacJ6MFoSDCkvfKntRNKF'
    'e/gQvnai6bg3TwTXDfLhuct03xdQn+BwMWPnhpp39UYAvpvBaMP2g7Ux8tTbfKqG8pWn+J7Y+BMFTa/Mw3ez+vLjmncnBEDOVq+luIHbU2I4UiGxZldt+Cz1'
    '8Ac03xjT2qG9DOXVEukBg4xGOtOAUwUAmTkXQCILjOtUBaWzGaauo277IZ63m8haNR27MM8gP4BAV4ryrYkMrBUA2xrPvX/8oWi6rQdOdtTRPD6YJBtjWjsk'
    '4kYJAEG2HokMrBUAIcaDiRlKM71mV62jjjaOHEnijTGtHbMCgiLUsXVHANT8PwWn2THENEyFmv/R/vZaR10IxUfAtCI+NtERKHGufBap4l5UhkcAmTnnq2+a'
    'EticECia7sv99klOuk2lHE3X2ik5Buwrk+icYVA7EEhDZc4YoHhlWABsaxKPSk9m99HaYfzHjaaZ5kJj4ORC+PZZhgXPRvwAkasWk9CKoTTT63fXOOrCKau8'
    'SDPNJAfUZowTgIgfwO+k/BbynNbSm9WkmSbnXE2AzZYEpplmzMdMP4AcS0eL+5GanZvMh30UHAobOm2IoTXbj3Y2VFls/Ez0UAdiHLQ5aGt2Tz/8/l5IkhiU'
    'mjTT4bx04W2v+8rAMK5y6rkFhqBs3w9bDDCxA6wOSWc7Zs0wnhx1Ww+QEnNvzngLtUUjkUIJgFACYAD7y2y8tz3soNvgJKVsqNDZ+BnvMTZHoiMAQC9ozOrC'
    'EF76pBp/2RJEVYgNnNGPs7MMbZcCA5QAiGxoyJ5jNqYursDmfTU9PRs/oyffG2JqAi2R7VcOwDTdbOtgucTlf6zA9oO8Q47RmxS1fnb1QN2PCWuUNJIurVKa'
    'HKuSmLKwnI2fMYIr+/uQmWKqDwDtSQDaQyPuf6sSm0rZ+Bn9yWsn8Oj4FBiL0GwEQMksX//CwO2VTKuje1uB17+Thq5tDDkdqCEkCYDQRwBe2lAFU5dUmdZD'
    'l0yBZcr4e5xhsPETIjwF0GIbsC0l/nu9qSFVTGugXyeBm4b68Z3BAbRNTYpVqVRt1i9K1fC/lMN2Gc0Ymm1hVHcLk/v5MTY3+bbMaCMAAcNHU4z59GovMKKb'
    '5Tzyu/kwpKuFFH9yx59oIwApppyxxiQFHZTnyzH27j6MUL38SNXLt09vfb2QPiOApN2QzCSaVNW2zukikE/G3o0eAr07cIMjtBGAVDXU6pQO7OfDMJiYkDiz'
    'o3AMPd8xdksZvwU/jzAbRKsg5nw1DPvfrSYet8QkiqyMyFCeDL57eO7eJjk89J6glQBQRbIAMI2R7g975es66nLasfc4FjQTAJqXcSwAQzEqEv07h418hOrZ'
    'yVF3dpYFy+LePZ5oJQAju7NjprXSrQ1ODOPpeXi2QEYK9+5uo5UAUHRVnw4C23gnYFKTGZAYHnHQUQ8/qoeFrEw29kSgXSaD4Wqot+0gbwhKFnxCqqF7eOmt'
    'pofv18mCEDyU1wH9BED1Cks2swCYSm672mg6egzL9jlLvIyeaCcAYUdgNRj9aZcaFuwaRx3FzHdohdF0JqOdAAxTUwDqL9gLoBcBSzoBNXUddX07srGbjnYC'
    'EPAJDO5qYeMejgdIJOSMrbveTsaf7BtjWiNapjOlJaCNe8B4BIVg18TIj3C88r5k2e/ONIGWAkCNcMEn7Ah0A0oCN1j15uFhfHgon9eeh/KtFT1HAN24QcYD'
    'iqY7q1PtMH646uHP6eKDj6PpmAhaCgCFgFJPZeyhiwmiaxs1hM+uWW8PB9oYm7Ka8QQtBcASYQfUB0XsCGyMzBRgaNeTN8Z0Nz1JJeM52p5pRBGBLABhLCEx'
    'sLMVcdSFDX5AZ94Yw8SOtgIwohX7AXq0rbsxxnJiI9I5aSLjAhoLQOvYGdg25eSNMZSbrjNvjGE8QlsBoEQPyZYizG9JDOriO8lRR+mreGMMkyi0PteY8rAv'
    '+9LceIC8yMYYGsqTT2N0jqnHSDPJitYt8vtD/cYIAKeZZkxEawEY18uP3u2rsP2QXluDUnzSiaarGcZT6HKfjpzNiDEP7cekt4/w4+H3Erk9WKJvh9qc8vmc'
    'ZppJIrQXgJuHB/C3AhtvbfVmKkBppk/sce9mOXkKOc00k6wIOS9P+633QVvi5qWVcReB2jTTtTvhenKaaaYVYYQAELGKQENppgdmWbwxhmnVGCMAhJQSb34V'
    'wrP/rMK63ae/7Ow2ODGMr3HUcZpphjkZowSgLh/vCuGjnSFs2S/VKoEN8skNzQ7P2WkJjtNMM0zTGCsADMPEDneTDNOK0X4ZcHUhpwZjzIXC2XVGewGYsrAC'
    'DGMqpTMyoTM8BWCYVkzSbE8blCUc7z+t9fdub6HkmFQrBDY27glhdRH7ORmmIYwXgFQ1xZp5QQB3jQo0GtTzztYg7nmjMqlyCzBMPDB6CkDhuytvTsf/Pzfl'
    'tBF9l/T1Y9Wt6bi8H+/YY5i6GC0AM8em4MxOzbsFSrP103EBZATAMEwEYwXgpqE+XNynZTOY3h18+NmEFDAME8ZYAbhzZHSGfOMQv7MLkGEYQwWgc0b49Npo'
    'oFz6tBuQYRhDBWBI19gOxcjnswcZxsHIwTCl5Erk7zNMsmCkJbzxVWynhlJOAYZhDBWArQckyqqiPzfw070sAAxDGCkAEgL/9XF0o4B/Fgfx9QEwDAODlwHn'
    'rKrCtgMt68mPq1HDPW9UgWGYMMYKQLUtcO+bVQjZzd/o8+iKKuzQ7JARhkkkRofE/HOnjbG/LcNzl6U5iT8bo/CQjXuXV2J1Ycv9BrTZaFh2+Jju0T18aJsK'
    'rNkZwt/V3/qgKHo/BBM/xuZauKCnhVGqfo5WwskVuW63rZ65fppC+5yAWfOON/kZSvl958gAvtnX72wJTgsIZ2SwcY+NlTtCeObDapS18HAhMvy7Rwfwo3P9'
    'jWYTLq+2sXRzCA+9V6WmF2A8hI5Vn3NJKi7v70N6oOH6IUfxv6m6f+bD2FaNYkH3hCBJIQB1qTmCe8s+G+VR1vu3+lpO48pp5iEhNMK4bVkFNpTw9MIL8rsp'
    'J/AVaS2qn1nvVOHtbd6v/nBGII8JKt/AJyXRG/+5PSz8/pr0ZjcuIre9hbe/n4Gbh/F2Y7ehsyKX35jR4vr5w7VpzuiQORkukTrQHoPfTUlDtMz/Zhqu6atp'
    'FgAAEABJREFU6M8i4BbXnu3Dk2pkFi0LrkpzjnFnamEBiEA7C16+Og1ZmbEdFfbC5am4IJeLNd5Qmf7q0uiNn+ii6pYEng+Dq4VbaoRxvcKnCsWK3xL4vRKS'
    'AZ25mcUL2rtBZRqPcxwpTff5PbluamABiDAmJ35D98wUgSXXp6NHW25osUJluOT6NKdM48X5uZwQogYuiQhjesZXC2m4uXRaGi55qRxHKqEVlBaNetWzs8JG'
    'talU4rO9douXSt2G5utUhh3T4yuk5/Xgfq8GFoAIg7Li77zr3cHC4qlpzuEmFYlbiq5DOF7ikXEpCPhONqqqoMSjK6ui3mMRb9JUy1yoPPdUhvFmcFcWgBq4'
    'JCKUlrkTNUYRii9OSU2446md8p+RGP384tRTjJ9I8QvHw/7bK1KQGUhsPANN9anMRnR3Z0VlfxmYCCwAEQoPu9foJ/bx498mJTYZ6U9Vrz+hd9MDvisGBPDY'
    'hNi87bFC3v6JfdwbnO44xCHCNbAARHBTAIjvDgngJ2MSk5N8vFrhuGlY8/83fXZi78Q0jQfOD2DqIHdnpgWHOWKzBhaACK9/4f7c98ELU5xgFq/54ciWC88d'
    '+d6LFZXNzAvcHykt2aSHn0MHWAAi0O4+OkLMbWh462WgEG2UGprd8v9HjjL6Xa+4uLcv5kCf5vDW10Gs2cVTgBpYAOrws/erYEt3G70vEijkVWLSvh0FOmW0'
    '/H9lZVrO73oBlcWCq1LjEuhzOqhuH1nB2zbrwgJQh837JH7/ifuL4RTU8srUVPQ8A66TGYjeqDJT3G8eVAavXpeqlv3cF5uXNgSx7SDP/+vCAlCPh9+rxtpd'
    '7m8bpbMKKVqwNW9OoXtfdkN6VCOUlkJ1+lPu/U+BBaAeFLAz7ZUK1VO4P0+kIBcKc01thRsIKRqR7j3nDPebINUl1akewVh6wQLQAIcrgWsWVWB/mfvDxcFd'
    'fc7812pF2wboXskPQvfuNlSHVJeHNQvH1gUWgEYoPiIxVfUa5dXuiwAFvXjhAdcFutcL89w3fqo7qkOqS6ZhWABOw6d7bNzyemWLMg9HCwW/TB+bmEAhL5k5'
    '1u96oA9BdUZ1R3XINA4LQBO8uy2EH73pjfNoxtjEBAp5xQ2D/XhgrDcjHaqzd7fxCVBNwQLQDBZ9HsTTH3gjAjQ8pqCYZIPuyav9EL/4oNKpM6ZpWACayZOr'
    'qvGnze43KgqGIadgMp1gPDzbijg63fd0LvysGnNWsfE3FxaAFvDDv1biH0XuDyspKIaWyHq1N39pgO7hlevSPAn0eX+Hd9O1ZIEFoAUElT/phiUV2FTqvmOJ'
    'suBQNhyTA4WyMsIZfdqluW/8VCffX1oBmx3+LYIFoIUcUx3MtYvKsfuo+yJAQTI0EsgwcHEgHOiT7kmgD9UF1YluKc1MgAUgCkrLgKsXVeBopTeBQhQ0Y1Kg'
    'kC8S6DMwy/3mdahCOnVRyll+ooIFIEq+PiCd6UB1yH0RoKAZkwKF6GwELwJ9qlTZU4gv1QUTHSwAMfBhse04BqX0JlCIgmh05+ELA7hygPvXSWX+g2WVzinA'
    'TPSwAMTI61+E8NhKbzzPFESjc6AQBfr8eIw3a/2PrqjCG19xoE+ssADEgefWBPFf67wRgecn63nI5Sh1Tb/8tjfTlF+vrcLza3mtPx6wAMSJh96t9iSlGPGL'
    'b+m1e5Ccfs965KP465agk7OBiQ8sAHGCvAC0+cSLZCJnK+/6nSP1WRu8a5QfZ3b0JqkHzfuZ+MECEEe8TCZy8zB9fAG3DndfjGqSelSzzy+usADEGa+SifTu'
    '4EPbxJ414tAxHchp524z2nuck3q4BQuAC3iVTCRfA2dgfjd3RyLHq6QT5cdJPdyBBcAlKBHFra+722UNy0589Q3r5u413PKnCidbM+MOLAAuQsEqh8rdm7Sm'
    '+BK/FOAT7hqnaEW5EhMBC4BL0Mk6v5uShvbp7hWxF7sSm2JzqbsC8N9XpmFgZ1YBt2ABcIGcM+jgjzSkB9xtuDoIgNvXQIeovOrsKmQRcAMWgDjTLjWc775T'
    'hrsNljYh6XDKDW3EqXJ5Q1SXzHCClHatJ3GyZ7AAxJE0P7BQ9fx9OrhfrL9ep0c0HJn+gg3uXwuVKZVtgFtsXOHijBPU3//uylSM7O5+gM72gzYe/5s+4bA/'
    'e78aRYfdn45Q2f7mCh4GxBMWgDjx+MQALunrzXbdu/9aqVVEHEVA0jV5wWX9/Hj84uQ/P8ErWADiwN2j/PhBvjdheT96s1LL8+3/UWw71+YFd4xMwQ9H6p8b'
    'wQRYAGLkygE+PDreG+N/anUl/vCpvttg6dq8Oj/hsQkpuPSsVniqapxhAYiB83IsPH9ZKoQH0SqvfB7E3NX674Gn8xNe8eBQDipz8gfkd+MmHAtcelFyZkeB'
    'P1yjvNIeROP9vSCEe94wZycMXStds9tQJCStDFBdMNHBAhAFlO/+NbUu3TbV/Yb36Z4QvveaWfnu6Vrpmuna3aZ9mnDqguqEaTksAC2kjZruU2Rat7buF13x'
    'EdvZBmtivnu6Zrp2uge3obqgOjHx/IREwwLQAvyqtGjYf7YH+e4PlEtctbACBytgLHTtdA90L25DdfLSVWadn6ADLAAtgBx+Y3q673muCIYTYOw4ZP42WLoH'
    'uhe6J7cZ18uHX35bgywpBsEC0Ewo3/2Uge6vPYfUBPqmpZX4bG/y5L6ie6F7CnngyJh2TgAzDDg/QRdYAJrBjUO9y3dPHvT3tidfvnu6J69WMqaPTcX1g1gE'
    'mgMLQBNM7OPDU9/0xvjnrqrCq5uS97ALurf5q70RAZoKXJTHzbspuIROg5PU40rKwe9NoM9THyR/vvt5q4OeBAr5lDeQnIJUh0zjcOk0gldJPYh3twWNCvSJ'
    'FbpXume3oWQiVIecTKRxWAAagFJde5HUg6BgGXKQmRToEyt0r3TPXgQKUR1yMpHGYQGoByX1eMWjpB60r5+WyCpb4RmXdM9079s9OESFk4k0DhdJPeZckoIh'
    '2e6v9R8st3HtYrMDfWKF7n0qlUG5N8lE5n6DQwXrwwJQBzrh9rtD3G8kdNjF1YsqUXiY890XHA6XBZWJ29w4NEWLsxR0gksjguXRCbcUDEMbZbwK9Nm4x1bG'
    '1fL/VV5tY2OJN3MTKgsqEy8ChZ6ZxM6AurAARLh9hDcn3JIHfFWhd1F+EgJfRHGyzqZS6fyuV1CZeLESMqiLhVs0Olg10bAARLiyv/uN4vG/VSYk0OeFtS2P'
    'L/jVR97HJFDZUDCU20wZyL6AGlgAInRMd7co/mdjNZ75MDEZfZZ+EcLCT5tv0JTm+y9bErM0QcFQbgcKtedZwAlYACK0T4NrUNDLfcu9yZXXGDPfqVJLbk0b'
    'dcEhG4+sSOy1uh0o1CGdA4NqYAGI4NZBm2t2hgN9Eu3vpwQdo39ToZbdyvFJPeceHWJK36Ofjfx1ecITkNQECn28y51RSBrvEzoBF0WE9coAxvWKb3FQkMu0'
    'V/UK9Fm5w1aPCue4rYt7h/X/7a0h7C+HVlCZUdm9dWM6esc5KGtDSfJstY4VHgFEWFUYXyvdezyc0eeIpiH+dH0LPws5D92Mvwa3Mgp5kbDUFLQXgEyPErys'
    'juPSHAW1XLOoHDuPcqBPrFAZUshwPAOF4i32jZFpQHIi7QWga6Y3Dhs6bSceDSMYCfSJZu2daZh4Bgqt2B5U0z1v6ia7jf7ORu0FoEumd4V4658qsPtobCOB'
    '//dnbwN9WgvxCBSiur19mXdzMhaAONDVQwGgOectf4p+d86styux7EueX7oFBQo9/G70BnzTUm99Ml623WghAdA6E8VQjzdvrNtNSTnLcaCs+b34fvXZK/9Y'
    'jt+u1//oLtP59bogpvyxzCnz5kJ1eeNr5Z4N/WsYqn82okpLLVAfgsZ8s6/3cdtvfGWrNfNyvLa5aYP+SK3zX/S7CnxQxMN+r1hdJJ0y/3h306OtVz+vdupy'
    '+dfe18/k/pqvsivbF3Je3g71Mg8ak/9CWcK2zg7PFhiilLxXewv9OltI9wvllAphnXIaflZqY+sBdvYlkn6dwvUzuIt6dPWhrFriq/02th+ynZ2QG0oSUz99'
    'Owp8eLv255UV+JUKVEDzqcplZ/nw/NrEDK9p2LjeiZzjub2ObNkv1SMU2WSlT1LVSX0N2HGobN9Sxq/1FIC4c1TAOZaLYUwgVdn+3aMNCAJQtq98AFJ7Aeje'
    '1sId+Ry1zJgBtdUsA1YAyPapXy2BAdx/fgra8rFvjObQ6dH3jTGmoZaQAOyAAZyRKjxJ2cUwsfDcZalom2rMduMdygcgd8AQJvfz465RnM2F0ZN7z/Xj0rMM'
    'mqpK+YXyAdhfwCAeHR/AeTnsEWT04sJcCw9fZNgc1bKVAKQEjBIAOqfv5atSWQQYbRij2uKLU9I8OUMyrgQCOyxx345Daj3wMAyifbqFpdPS8J1zeGWASSxT'
    'z/bhNdUWz0gzzPgl9pDth7tRgQ0wDL8Vdgo+OTGglJej8Rhv8ak2R6dI/cfkNKctGoeAM/IPC4CUK2Eot+en4B+3pWPaOT5VESwEjLtQG7thsA8fqDZ32wiD'
    'HdIRm3ekS87PGQ/pWwHD2XXExgvrgliyOYg9x1gMmPiR3YaG+37croy++xlJ4H8SoQlievHKsAA8i1SU5x1ScuBicmxv+ag4iD9vCWHtLts5fJL2gZeWgWGa'
    'JCsDaJdGqeItjO7hwxX9fcjvnkSnCdH+n/SC9uJeVJ6YvMh5eSvV0zgwDJPsvC9mFIynF7VjGYP9AAzDtIA6tl4rAML+EAzDJD+WvfLEyxPfLCt+RynDMTAM'
    'k7xIlOJ48aqatycEQMwGZdxYAoZhkhchX4vYusPJ6xmW/SIYhklehL3wpLf1fy7n5m6FEH3AMEyysV15/0+y7YYiGl4GwzDJh5Qv1f/WqQJgiQX0STAMk0Qo'
    'm7arXqz/3VMEQEwv2K6eFoNhmGRisXiwZEf9bzYc1GxhPo8CGCZZULYcEnMb+kmDAiAeKFyn1gv/FwzDJAPLxIMF6xv6QePbmix7DhiGMR8ZatSWGxUA2iqo'
    'nv4JhmFM5n0xc2ejYf6n39gs5L+AYRhzseT9p/3x6X4ophe+o8YPi8AwjHlIucDx552GplOb+ILTlUPwKBiGMQey2VDo/qY+1qQAiPt3FampwBNgGMYg5CPi'
    'oZ37m/pU85KblRU+rf7gJjAMoz8SX6K88NnmfLRZAiBmowqwmxxOMAyjAVboTmWzdrM+imYiZhQvV9LyAhiG0Rj5y8gSfrNoWX7jssJ71D9YA4ZhNESuUzb6'
    'QEt+o0UC4GQSsaqnqpeHwDCMThyCHby6braf5tDiEw7EA7sLIO2bwDCMRsgbxaxdhWghUR1xImYWLaO5BhiG0QA1759R+BdEQfRnHPUqVKsCMqp/yjBMvFA2'
    '6NhidEQtAOI6hJBWeC07BRkmYawkG3RsMUpiOuWQzhaD354EKT8HwzAeojrezLLLHRuMgZiPOTfz4tMAAAKCSURBVBU/KT6AFPENJQLbwDCM+5CtqY5X3F0a'
    '80E+cTnnWNxXsBuWuERd2C4wDOMeZGPK1pyONw7E7aBzJ5loqGqYusCPwDBM/JFyLXzBoZHEvXEhbgJAiIdKSlEenACJFWAYJn5IvImAfZF4YNc+xBEBF5Cz'
    'kYL0vAXqr08DwzAxIl9BWeG05m7waQmuCAAhpfrb83Jnqa8/V//GB4ZhWogMQYpHMaPgCSHgSpp+1wSgBjmn50WwLEorlg2GYZpLCWz7ejGr6G9wkbj6ABrC'
    'uYHq8mHq5ftgGKZpaL6vbMZt4ydcHwHUIBfDh+25P1f/cZb6t579X4YxB/eH/PXx3BDl/LwxykHwK/WvR4BhmAjyY9Uv3qOW+P4BD0lIT6xWCSxk5N6t/v2/'
    'qrdngGFaL0cogafy8v+7G17+pkjoUFzO66Ucg/Yz6jKuB8O0KpzDdxcoN9yDYsaOEiQILebi8sm84WpM8Li6mm+DYZKf5QjhocYO7PQSrZxxcm6P8wD/IywE'
    'TJKyHDL42OnO6vMaLb3xcn7OuZA+OtF0PBjGfFZChGaJ6cXaHbar9XKcnJczDvDNBgsBYyYrgdBsMaNY2xgYI9bjnb0FGXmXq5XR76m3l6qrTgHD6IZEBYT8'
    's3r8D44XvRk+UEdvjAvIkc9ltcGxtEvUmukkdfnqgTwwTOIoUK3yLQh7OTIq345Hkg4vMT4iTz7VfQBCfhICEoRx6o7SwDBuQb08JA3pl6v29paYWbAZBpN0'
    'IblyTvdcWL48NULIg60eQvZSldZTvW+vll4z1B1nqPcZ6tbpdTswjMRh9aVMtYcy9Vo9C/UsD6n3RaqdFKjX4Yc/WOCci5FE/B8AAAD//1S6KAoAAAAGSURB'
    'VAMAwfsjcsGgGU0AAAAASUVORK5CYII=')
# icon files for the Start menu shortcut and the tray (16-256 px; the small sizes as plain bitmaps, which every Windows
# shell path decodes): written to BASE\icons at start, see write_icons()
ICONS = {
    'mediascan.ico': (
    'AAABAAgAEBAAAAEAIABoBAAAhgAAABQUAAABACAAuAYAAO4EAAAYGAAAAQAgAIgJAACmCwAAICAAAAEAIACoEAAALhUAACgoAAABACAAaBoAANYlAAAwMAAA'
    'AQAgAKglAAA+QAAAQEAAAAEAIAAoQgAA5mUAAAAAAAABACAAUSQAAA6oAAAoAAAAEAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6K'
    '/0weiv/EH4r/7x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/7x6K/8Qeiv9MAAAAAB2J/1Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x2J/1Afif/HH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fif/HH4n/8B+K//8bbcf/Exoh/xUlNv8TFRf/FBQU/xQUFP8UFBT/FBQU/xMXGv8VJDT/FBsj/xtuyP8fiv//H4n/8B+K//8fiv//GE+L/xQUFP8bbcf/'
    'GmGu/xMUFf8WMU7/FjFO/xMUFf8aYrH/HGvC/xQUFP8YT4v/H4r//x+K//8fiv//H4r//xlPif8UFBT/FSc6/x2E9P8ZVpj/GEl+/xlJfv8ZV5r/HoT0/xQm'
    'OP8UFBT/GU+J/x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xMUFP8bZ7v/HHjd/xhJfv8ZSX7/HXzk/xxtxf8TFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//'
    'GU+J/xQUFP8ZUI3/HoPy/xUpP/8aXaf/Gl2n/xYqQf8eg/P/GE+L/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//xlQjP8XQ3P/GU+K/xYuSf8UFBT/FBcb/xQX'
    'G/8UFBT/Fi9L/xlOiP8UFBT/GU+J/x+K//8fiv//H4r//x+K//8ZXaf/GEuC/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xpdpv8fiv//'
    'H4r//x+K//8fiv//GEyE/xQaH/8XSHz/GVaY/xtlt/8cgO3/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYxTv8UFBT/Fzxl/xph'
    'rv8TFBX/HX7o/xQkNv8YTYf/GFmg/xtsxf8dhff/H4r//x+K//8fiv//H4n/8B+K//8aYa//Fjhd/xQsRP8dd9v/FBQU/xtovf8WMk//Fzxl/xphrv8TFBX/'
    'HYDs/x2C8f8fiv//H4n/8B+J/8cfiv//H4r//x+K//8fiv//HH/q/xtktf8YVpj/F0V2/xQrQf8dd9v/FBQU/xttx/8fiv//H4r//x+J/8cdif9QH4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif7/G3TV/xpcpf8bar//H4r//x+K//8dif9QAAAAAB6K/0weiv/EH4r/7x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r/7x6K/8Qeiv9MAAAAAIAB//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//4AB'
    '//8oAAAAFAAAACgAAAABACAAAAAAAJAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAejP8qH4n/nB+K/+Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r/4B+J/5wejP8qAAAAAAAAAAAAAAAAHon/iB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8eif+AAAAAAB6L/0Qeif/9H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x6J//0bif9BHon/uB+K//8fiv//Hob5/xxwzf8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HHHO/x6H+v8fiv//H4r//x6J'
    '/7gfif/oH4r//x+K//8XPWb/FBQU/xQgLf8VHyr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSAr/xQgLf8UFBT/Fz1n/x+K//8fiv//H4n/6B+K//8fiv//'
    'H4r//xUjM/8UFBT/G2zF/x164P8UHij/FBQU/xYyT/8WMk//FBQU/xUfKv8deuH/HGvC/xQUFP8VIzP/H4r//x+K//8fiv//H4r//x+K//8fiv//FCIw/xQU'
    'FP8VLUf/Hob5/xx01f8UGR7/GVWX/xlVl/8UGR7/HHTV/x6G+f8VLUf/FBQU/xQiMP8fiv//H4r//x+K//8fiv//H4r//x+K//8UIjD/FBQU/xQUFP8WOV7/'
    'Hon+/xpYnP8ZVZf/GVWX/xpZn/8fiv//Fz5o/xQUFP8UFBT/FCIw/x+K//8fiv//H4r//x+K//8fiv//H4r//xQiMP8UFBT/ExYY/xtqwf8eif7/FzZY/xlV'
    'l/8ZVZf/FzZZ/x6J/v8basD/ExYY/xQUFP8UIjD/H4r//x+K//8fiv//H4r//x+K//8fiv//FCIw/xQUFP8aWJz/H4r//xdDcv8UFBT/G2i9/xtovf8UFBT/'
    'F0Ny/x+K//8ZVpn/FBQU/xQiMP8fiv//H4r//x+K//8fiv//H4r//x+K//8UKkD/FSk+/xphr/8ZTIX/FBQU/xQUFP8TGiD/Exog/xQUFP8UFBT/GU2G/xtg'
    'rP8UFBT/FCIw/x+K//8fiv//H4r//x+K//8fiv//H4r//xdAbP8aY7L/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UIjD/'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//GU+J/xpYnf8bctD/Hoj9/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x6D8v8UFBT/FBQU/xQUFP8UGB3/FjBM/xhJfv8aYrD/HXvi/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'G27I/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB4o/xc3Wv8ZUY7/G2m+/x6D8/8fiv//H4r//x+K//8fiv//H4n/6B+K//8eg/L/GU2G/xUx'
    'T/8UGB3/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xtgrP8fiv//H4r//x+J/+geif+4H4r//x+K//8fiv//H4r//x6J/v8cddf/'
    'Glyl/xdDcv8WKkH/ExUX/xQUFP8UFBT/FBQU/xQUFP8UFBT/G2zF/x+K//8fiv//Hon/uBuJ/0Efiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8ehff/G23G/xlUlf8XOV//FCIw/xQhLv8eg/P/H4r//x6J//0ei/9EAKr/Ax6J/4Yeiv/+H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8eiv/+HYr/gwCq/wMAAAAAAAAAAByI/0keiv/NH4r/7x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv/vHor/zRyI/0kAAAAAAAAAAMAAP/+AAB//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//'
    'AAAP/wAAD//AAD//KAAAABgAAAAwAAAAAQAgAAAAAABgCQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6J/10fif+tHon/5R+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+Ufif+tHon/XQAAAAAAAAAAAAAAAAAAAAAajP8oH4r/+B+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J//Mbif8cAAAAAAAAAAAeif+4H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+wAAAAAB2J/1Afiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYn/UB6J/7gfiv//H4r//x+K'
    '//8ca8P/GVGP/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlRj/8cbMT/H4r//x+K//8fiv//Hon/uB+J/+gfiv//'
    'H4r//x2E9f8TFhn/FBQU/xQeKP8VJjn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VJzr/FB0n/xQUFP8TFhn/HYX2/x+K//8fiv//H4n/6B+K'
    '//8fiv//H4r//xttxv8UFBT/FBQU/xtqwP8eiP3/FjNT/xQUFP8UFBT/FStD/xYrQv8UFBT/FBQU/xY0Vf8eiP3/G2i8/xQUFP8UFBT/G23G/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xc8Zf8fiv//HoT0/xUpPv8UFBT/GmKx/xphrv8UFBT/FSk+/x2E9P8fiv//Fzti/xQUFP8UFBT/HGzE/x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xQUFP8ZTYb/H4r//x1/6v8VHyr/GmOy/xpisf8VHyr/HX/q/x+K//8YS4L/FBQU/xQUFP8UFBT/'
    'HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xQUFP8UGB3/HX7p/x+K//8XP2v/GmOy/xpisf8YQW//H4r//x1+6P8UGBz/FBQU/xQU'
    'FP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xMUFf8bZ7r/H4r//xxvy/8TFhj/GmOy/xpisf8TFhn/HG/L/x+K//8bZ7r/'
    'ExQV/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xlUlP8fiv//HXfb/xQbI/8UFBT/G2W2/xphrv8UFBT/FBwk/xx4'
    '3P8fiv//GVKQ/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UHCT/FBwk/xlWmP8ced7/FSMy/xQUFP8UFBT/FBsi/xQbIv8UFBT/'
    'FBQU/xUjM/8ced//GVSU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8aYa7/GmGu/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//x164f8XR3v/GEyF/xlPif8ZT4n/GU+J/xlPif8ZT4n/'
    'GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/HXrh/x+K//8fiv//H4r//x+K//8fiv//H4r//xtqwP8UFBT/FBQU/xQdJ/8WN1v/GVKR/xxr'
    'wv8dhPT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xhMhP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8TFBX/FSc7/xdAbf8aWqD/HHLR/x6I/P8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xY4XP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQXG/8WLkn/GUl+/xpjsv8ehvn/H4r//x+K//8fiv//H4r//x+J/+gfiv//H4r//xtmuP8VJjn/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8YRnj/H4r//x+K//8fiv//H4n/6B6J/7gfiv//H4r//x+K'
    '//8fiv//HoPy/xtovf8YT4v/FzZZ/xQdJ/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8YRXf/H4r//x+K//8fiv//Hon/uB2J/2Efiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8deuD/GmGu/xhIfP8WL0r/FBgc/xQUFP8UFBT/FBQU/xQUFP8aXaf/H4r//x+K//8fiv//HYn/YQAA'
    'AAAeiv/EH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hoj8/xty0P8aWZ//F0Bs/xc5X/8eg/P/H4r//x+K//8eif/D'
    'AAAAAAAAAAAZjP8fHor/uR+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K'
    '/7kZjP8fAAAAAAAAAAAAAAAAAAAAAB6J/10fif+tHon/5R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+Ufif+t'
    'Hon/XQAAAAAAAAAAAAAAAOAAB/+AAAH/gAAB/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAA'
    'AP8AAAD/AAAA/4AAAf+AAAH/4AAH/ygAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG4n/OB6K/5geiv/C'
    'Hon/7B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif/sHor/wh6K/5kbif84AAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAIID/CB6J/4geiv/8H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K//weiv+JIID/CAAAAAAAAAAAAAAAAACq/wMfif/OH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/PAKr/AwAAAAAAAAAA'
    'Hor/eB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif9wAAAAAByK/yUfif/5H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J//Mch/8kHon/iB+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x6J/4geif+4H4r//x+K//8fiv//H4r//xtpvv8WL0r/ExYY/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFhn/Fi9L/xtqwP8fiv//H4r//x+K//8fiv//Hon/uB+J/+gfiv//H4r//x+K//8fiv//FSk+/xQUFP8UFBT/'
    'Exca/xc+aP8UGR7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMaIP8XPmj/Exca/xQUFP8UFBT/FipB/x+K//8fiv//H4r//x+K'
    '//8fif/oH4r//x+K//8fiv//H4r//x+K//8TFRf/FBQU/xQUFP8aXaf/H4r//xxz0/8UGR7/FBQU/xQUFP8UFBT/FSk+/xUpPv8UFBT/FBQU/xQUFP8UGh//'
    'HHTV/x+K//8aW6P/FBQU/xQUFP8TFRf/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xlQjf8fiv//H4r//xtn'
    'uf8TFBX/FBQU/xQUFP8ceN3/HHjd/xQUFP8UFBT/ExQV/xtnu/8fiv//H4r//xhPiv8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//FBQU/xQUFP8UFBT/ExUX/xxsxP8fiv//H4r//xlWmP8UFBT/FBQU/x1+6P8dfef/FBQU/xQUFP8ZV5v/H4r//x+K//8cbMT/ExUX/xQU'
    'FP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBgd/xxxzv8fiv//H4r//xhCcf8UFBT/'
    'HX7o/x195/8UFBT/GEJx/x+K//8fiv//HHDN/xQYHf8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBwl/x132/8fiv//HXrg/xQUFP8dfuj/HX3n/xQUFP8dfun/H4r//x132/8UGyL/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/FBQU/xQZHv8cctD/H4r//x+K//8YQnH/FBQU/x1+6P8dfef/FBQU/xdD'
    'c/8fiv//H4r//xxy0P8UGR7/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8TFRf/'
    'HGzE/x+K//8fiv//GVWW/xQUFP8UFBT/HX7o/x195/8UFBT/FBQU/xlXmv8fiv//H4r//xxsxP8TFRf/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xlQjf8fiv//H4r//xtmuP8TFBX/FBQU/xQUFP8dd9v/HXfb/xQUFP8UFBT/ExQV/xtnuv8fiv//'
    'H4r//xlPiv8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/Gl6o/x+K//8cdNT/FBke/xQU'
    'FP8UFBT/FBQU/xQeKP8UHij/FBQU/xQUFP8UFBT/FBke/xx01P8fiv//Glyk/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8VJDT/HX/r/xhIff8TFxr/F0Bs/xQZHv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Exkf/xdAbP8TFxr/FBQU/xQU'
    'FP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xUkNP8df+v/GEh9/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//GEqA/xdB'
    'bf8YTIT/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6G+P8UHij/FBQU/xMUFP8WKkH/F0R0/xlbov8cc9P/Hoj9/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HGvD/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UGh//FjJP/xhKgP8bY7P/HXzl/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8ZUpD/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB8r/xY4Xf8ZUpD/HGvC/x2E9P8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xhDcv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUoPf8XQGz/Glme/x174/8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/wH4r//x+K//8fiv//'
    'GmKw/xQXG/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExYZ/x17'
    '4/8fiv//H4r//x+K//8fiv//H4n/6B+K/8gfiv//H4r//x+K//8fiv//HYX2/xtsxf8ZU5P/Fzph/xQhLv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HHbY/x+K//8fiv//H4r//x+K//8fiv/AHor/mB+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x2A7f8bZbf/GUyF/xYzUv8TGiH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMWGP8eiP3/H4r//x+K//8fiv//'
    'H4r//x6K/5gfiP86Hon//R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/v8dd9r/Gl6o/xdEdf8VK0P/ExYY/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FSxE/x+K//8fiv//H4r//x+K//8eif/9G4j/LwAAAAAeif+IH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ehvn/HG/K/xlWmP8XO2L/FSMz/xMWGf8aXab/H4r//x+K//8fiv//H4r//x6J/4gAAAAAAAAAABuI'
    '/y8fif/YH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fif/QG4j/LwAAAAAAAAAAAAAAACCA/wgeiv+JHor//B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv/8Hon/iACZ/wUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbif84'
    'Hor/mR6K/8Ieif/sH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+weiv/CHor/mBuJ'
    '/zgAAAAAAAAAAAAAAAAAAAAA8AAAD8AAAAOAAAABgAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAGAAAABwAAAA/AAAA8oAAAAKAAAAFAAAAABACAAAAAAAEAaAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG43/Jh+K/2Qeif+iH4r/4B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4n/4R6K/6MeiP9lGon/JwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAZjP8fH4r/vx+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv+/H4//GQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAei/9nHon/8x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K/+8eiv9eAAAAAAAAAAAAAAAAAAAAAAAAAAAeif9wH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x6J/3AAAAAAAAAAAAAAAAAdi/8sH4n/+R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/5Hoz/KgAAAAAagP8K'
    'Hon/0h+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/9Qcjv8JHYj/WB+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYj/WB6J/4gfiv//H4r//x+K//8fiv//H4r//x+K//8cdtj/GU+J/xlPif8ZT4n/'
    'GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/HHjc/x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x6J/4geif+4H4r//x+K//8fiv//H4r//x+K//8ced7/FBke/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQaH/8ced//H4r//x+K//8fiv//H4r//x+K'
    '//8eif+4H4n/6B+K//8fiv//H4r//x+K//8fiv//F0R1/xQUFP8UFBT/FBQU/xMUFP8WOF3/F0Fu/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GEFv/xY4XP8TFBT/FBQU/xQUFP8UFBT/GEZ4/x+K//8fiv//H4r//x+K//8fiv//H4n/6B+K//8fiv//H4r//x+K'
    '//8fiv//H4r//xYzUv8UFBT/FBQU/xQUFP8ZTIX/H4r//x+K//8ZTIX/FBQU/xQUFP8UFBT/FBQU/xQUFP8TGiD/Exog/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'GU2G/x+K//8fiv//GEl//xQUFP8UFBT/FBQU/xYzU/8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8WMU7/FBQU/xQU'
    'FP8UFBT/Glmf/x+K//8fiv//H4r//xc/a/8UFBT/FBQU/xQUFP8TFhj/HXzk/x195v8TFhj/FBQU/xQUFP8UFBT/Fz9r/x+K//8fiv//H4r//xpYnP8UFBT/'
    'FBQU/xQUFP8WMU7/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FjFO/xQUFP8UFBT/FBQU/xQiMf8dge7/H4r//x+K'
    '//8eiP3/FjRV/xQUFP8UFBT/FCIx/x+K//8fiv//FCIx/xQUFP8UFBT/FjRV/x6I/f8fiv//H4r//x2A7f8UIjH/FBQU/xQUFP8UFBT/FjFO/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYxTv8UFBT/FBQU/xQUFP8UFBT/FS1H/x2F9v8fiv//H4r//x2E9P8WK0L/FBQU/xQi'
    'Mf8fiv//H4r//xQiMf8UFBT/FitC/x2E9P8fiv//H4r//x2F9v8WK0L/FBQU/xQUFP8UFBT/FBQU/xYxTv8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8WMU7/FBQU/xQUFP8UFBT/FBQU/xQUFP8XN1r/Hon+/x+K//8fiv//HYDt/xQbIv8UIjH/H4r//x+K//8UIjH/FBsj/x2A'
    '7f8fiv//H4r//x6J/v8XN1r/FBQU/xQUFP8UFBT/FBQU/xQUFP8WMU7/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'FjFO/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xhPiv8fiv//H4r//x+K//8XPmn/FCIx/x+K//8fiv//FCIx/xdBbv8fiv//H4r//x+K//8ZT4r/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FjFO/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYxTv8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xUkNf8egvD/H4r//x+K//8eiP3/FCY4/xQiMf8fiv//H4r//xQiMf8VJjn/Hoj9/x+K//8fiv//HYDt/xUkNf8UFBT/FBQU/xQUFP8UFBT/FBQU/xYx'
    'Tv8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8WMU7/FBQU/xQUFP8UFBT/FBQU/xQdJv8de+L/H4r//x+K//8fiv//'
    'Fz9r/xQUFP8UIjH/H4r//x+K//8UIjH/FBQU/xc/a/8fiv//H4r//x+K//8de+L/FB0m/xQUFP8UFBT/FBQU/xQUFP8WMU7/H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FjFO/xQUFP8UFBT/FBQU/xQZHv8cctH/H4r//x+K//8fiv//GEyE/xQUFP8UFBT/FCIx/x+K//8fiv//'
    'FCIx/xQUFP8UFBT/GUyF/x+K//8fiv//H4r//xty0P8UGR7/FBQU/xQUFP8UFBT/FjFO/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//xYxTv8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//Gluh/xQUFP8UFBT/FBQU/xQZHv8dhPT/Hof6/xQaH/8UFBT/FBQU/xQUFP8aW6H/'
    'H4r//x+K//8fiv//GEyE/xQUFP8UFBT/FBQU/xYxTv8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8WMU7/FBQU/xQU'
    'FP8UFBT/Gluh/x+K//8fiv//G2W3/xMUFf8UFBT/FBQU/xQUFP8UFBT/FB8r/xQgLf8UFBT/FBQU/xQUFP8UFBT/ExQV/xtmuP8fiv//H4r//xpYnf8UFBT/'
    'FBQU/xQUFP8WMU7/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FjFO/xc9Zv8bZbb/FS1G/xQbIv8ZV5r/GU2H/xQY'
    'HP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UGBz/GU2H/xlVl/8TGiH/FBQU/xQUFP8UFBT/FjFO/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYxTv8dd9v/H4r//xphrv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xYxTv8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8WMU7/Fz1m/xtltv8VLUb/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8WMU7/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'G3HO/xlYnf8aXKb/G2vD/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HGzE/xxs'
    'xP8cbMT/HGzE/xxsxP8cbMT/HHPS/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xdAbf8UFBT/FBQU/xQbIv8WM1P/'
    'GUyF/xtmuP8dgO3/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6I/f8UGBz/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQiMP8XO2L/'
    'GlWV/xttxv8dhfb/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8cddb/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExUW/xUpP/8YQ3L/'
    'Gluj/xx11v8eiP3/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//Glyl/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBof/xYyT/8YSoD/'
    'G2Oz/x185f8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB0n/xY4Xf8ZUY//'
    'HHLQ/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4n/6B+K//8fiv//H4r//x+K//8ca8P/FBcb/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZU5L/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r/4B6K/7Afiv//H4r//x+K//8fiv//H4r//x174/8aWJ3/F0Fu/xUqQP8TFBX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FjFO/x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/6geif9w'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hob4/xxvy/8aWJz/F0Bs/xUoPP8TFRb/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xdAbP8fiv//H4r//x+K//8fiv//H4r//x+K//8eif9wG4n/OB+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYX2/xtuyP8ZVpn/Fz5p/xUmOf8TFBX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8aW6H/H4r//x+K//8fiv//H4r//x+K//8fiv//G4n/OAD//wEeif/FH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYT0/xtsxf8ZVJX/Fz1m/xUmOf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HHbY/x+K'
    '//8fiv//H4r//x+K//8fiv//Hor/xAD//wEAAAAAHof/Ih6K//Qfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HoPz/xxsxP8ZVJT/Fzxk/xUkNP8UFBT/FSg9/x+K//8fiv//H4r//x+K//8fiv//Hor/9B6H'
    '/yIAAAAAAAAAAAAAAAAdif9oH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HHnf/x2E9P8fiv//H4r//x+K//8fiv//H4r//x2J/2gAAAAAAAAAAAAAAAAAAAAAAAAAAB+K'
    '/2Qeif/zH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hon/8x2K/2IAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAH4v/IR6J/8Ufiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//Hon/xR+L/yEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAHor/TB2K/4Mfiv+1H4r/5x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hon/5h6J/7Qdif+CHor/TAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAPwAAAA/////8AAAAA/////gAAAAB////8AAAAAD////gAAAAAH///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAAAP///wAA'
    'AAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAA'
    'AP///wAAAAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD///8AAAAAAP///wAAAAAA////AAAAAAD/'
    '//8AAAAAAP///wAAAAAA////AAAAAAD///+AAAAAAf///8AAAAAD////4AAAAAf////wAAAAD/////wAAAA/////KAAAADAAAABgAAAAAQAgAAAAAACAJQAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdiP8+Hov/fx+J/7Yfif/YHon/8x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J//Mfif/ZH4n/th6J/4AdiP8+'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG4n/HB6K/7Mfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//Hor/sxuJ/xwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAdif9OH4r/8R+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//EfiP9LAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6J/4Afiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hon/gAAAAAAAAAAAAAAAAAAAAAAAAAAAHYr/YB+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/14AAAAAAAAAAAAAAAAaif8nH4r/+B+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//gcjv8bAAAAAAAA'
    'AAAeif+4H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8eiv+wAAAAABqM/ygfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//GIf/IB6J/3Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYn/aB6K/7Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HX3n/xc7Y/8UICz/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UIC3/Fz1m/x1+6P8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hor/sB+K/+Afiv//H4r//x+K//8fiv//H4r//x+K//8eg/L/FSEu/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUjM/8ehPT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4n/2B+K//gfiv//H4r//x+K//8fiv//H4r//x+K//8aYK3/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8XPWf/G2Gt/xUkNP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FSU2/xpgrf8XPWb/FBQU/xQUFP8UFBT/FBQU/xQUFP8aYrD/H4r//x+K//8fiv//H4r//x+K//8fiv//H4n/8B+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8ZUpH/FBQU/xQUFP8UFBT/FBQU/xc2WP8fiv//H4r//x2B7/8VJDX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8VJDX/HoPy/x+K//8fiv//FjJR/xQUFP8UFBT/FBQU/xQUFP8ZU5P/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xpZnv8fiv//H4r//x+K//8deuD/FB0m/xQUFP8UFBT/FBQU/xQUFP8UGh//HG3F/xxt'
    'xf8UGh//FBQU/xQUFP8UFBT/FBQU/xQdJ/8deuH/H4r//x+K//8fiv//GVea/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xUlNv8ehff/H4r//x+K//8fiv//HHDN/xQYHP8UFBT/FBQU/xQU'
    'FP8XPWb/H4r//x+K//8XPWb/FBQU/xQUFP8UFBT/FBgc/xxxz/8fiv//H4r//x+K//8dhPX/FSQ0/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQUFP8XNlj/Hoj9/x+K//8fiv//H4r//xtn'
    'uv8TFBX/FBQU/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/xQUFP8TFBX/G2e6/x+K//8fiv//H4r//x6I/P8WM1L/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GENy/x+K'
    '//8fiv//H4r//x+K//8aW6H/FBQU/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/xQUFP8aW6H/H4r//x+K//8fiv//H4r//xc/a/8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xlSkf8fiv//H4r//x+K//8fiv//GEJw/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/xhCcf8fiv//H4r//x+K//8fiv//GVGO/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMUFP8aYa7/H4r//x+K//8fiv//HHjd/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/x174/8fiv//H4r//x+K//8bZrj/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQmOP8eg/L/H4r//x+K//8fiv//GmKw/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/xtjs/8fiv//'
    'H4r//x+K//8dge//FSQ1/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB4p/x195v8fiv//H4r//x+K//8dfun/FCAs/xQUFP8XPWb/H4r//x+K//8XPWb/'
    'FBQU/xQgLP8dfun/H4r//x+K//8fiv//HXvi/xQdJv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xQUFP8UGBz/HHTV/x+K//8fiv//H4r//x2E9P8VKD3/FBQU/xQUFP8XPWb/'
    'H4r//x+K//8XPWb/FBQU/xQUFP8VKD3/HYT0/x+K//8fiv//H4r//xty0P8UGBz/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xMUFf8bZ7n/H4r//x+K//8fiv//Hoj9/xY0VP8UFBT/'
    'FBQU/xQUFP8XPWb/H4r//x+K//8XPWb/FBQU/xQUFP8UFBT/FjVX/x6I/f8fiv//H4r//x+K//8bZ7n/ExQV/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xhBb/8fiv//H4r//x+K//8fiv//'
    'Fz9r/xQUFP8UFBT/FBQU/xQUFP8VLUf/H4r//x+K//8VKkD/FBQU/xQUFP8UFBT/FBQU/xc/a/8fiv//H4r//x+K//8fiv//F0Bt/xQUFP8UFBT/FBQU/xQU'
    'FP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xQUFP8UFBT/FBQU/xlVlv8fiv//'
    'H4r//x+K//8ZUI3/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFBX/F0Bs/xdAbP8TFBX/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZUY7/H4r//x+K//8fiv//GVOT/xQU'
    'FP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xUjMv8UJjj/'
    'FBQU/xQbIv8ddtn/H4r//xlXmv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Glme/x+K'
    '//8cdtj/FBsj/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZT4n/'
    'FS1H/x+K//8fiv//Fzxl/xQUFP8TFhj/FSY3/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xUmN/8TFRf/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8ZT4n/F0Bs/x+K//8fiv//GEd7/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8ZT4n/FBcb/xhPiv8ZVZf/Exoh/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZT4n/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HXrh/xtsxf8dge//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8baLz/FBQU/xQUFP8UFBT/FSQ0/xc9Zv8ZVpj/HG/K/x6G+f8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8XOmH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8VLET/F0R0/xpdpv8cdNX/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UHSb/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Exoh/xYzUv8YTIT/G2W3/x1/6/8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x185P8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UIS//Fzti/xlUlP8bbcb/HYX2/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xpi'
    'sP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'ExUW/xUpP/8YQnH/Gluj/xx01f8eiP3/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//xlbov8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UGR7/FjBN/xhKgf8bZLX/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//gfiv//H4r//x+K//8fiv//H4r//xx11v8UHij/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FCIw/x195/8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r/+B+J//Afiv//H4r//x+K//8fiv//H4r//x+K//8dhfb/GmKw/xhHe/8WL0v/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xtnu/8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4n/8B+K/8Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hoj8/xty0P8aWZ//F0Bs/xUnOv8TFBX/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xxt'
    'xf8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//Hon/uB2J/2gfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8ehPT/HGvC/xlRj/8WOF3/FSAr/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQV/x6F9v8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYn/aBeL/xYeiv/+H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x185f8aY7L/GEqA/xYxTv8UGh//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FSk//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv/+IIr/GAAAAAAeiv+wH4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HHTU/xlbov8XRHT/FipB/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/F0R1/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+wAAAAAAAAAAAbif9BHon/+x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x2F9v8bbMX/GVSU/xc6Yf8UIS//FBQU/xQUFP8UFBT/HXXW/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//oei/9EAAAAAAAAAAAAAAAA'
    'H4n/Wx+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HX/r/xpeqf8bctD/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x2K/2AAAAAA'
    'AAAAAAAAAAAAAAAAAP//AR6J/4Aeiv/+H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv/+'
    'H4r/fAD//wEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAfiP9LH4r/8R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//Edif9OAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFoX/Fx6J/5Efif/3H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fif/3Hor/kBaF/xcAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAARiP8PHor/Xh6K/7Afif/YH4n/8h+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+J//Ifiv/XH4n/rx6J/10RiP8PAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAP4AAAAAf///+AAAAAAf///wAAAAAA///+AAAAAAB///wAAAAAAD'
    '//+AAAAAAAH//4AAAAAAAf//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//'
    'AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAA'
    'AAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAA'
    'AAD//wAAAAAAAP//AAAAAAAA//+AAAAAAAH//4AAAAAAAf//wAAAAAAD///AAAAAAAP///AAAAAAD///+AAAAAAf///+AAAAAH///ygAAABAAAAAgAAAAAEA'
    'IAAAAAAAAEIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6L/00eiv+jHor/zR+J/+Eeif/1'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv/2Hor/4h+J/84eiv+jHYn/TgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAUif8NHon/iB6J//Mfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/wHon/iBSJ/w0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAACA/wIfif9jH4n/6B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/pHoj/ZQCA/wIAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAByO/xIeif/FH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+7HI7/EgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAByI/y0fiv/xH4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x6K/+sdiv8jAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAByO/xseif/jH4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//Hon/2xuG/xMAAAAAAAAAAAAAAAAAAAAAAAAAABSJ/w0fiv/RH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fif/HIID/CAAAAAAAAAAAAAAAAAAAAAAeiv+QH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/4gAAAAA'
    'AAAAAAAAAAAWhf8XH4n/9x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/3GIb/FQAAAAAAAAAAHor/mB+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/5gAAAAAEpL/Dh+K//gfiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/5AJn/BR2J/1Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xyK/0geiv+YH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8dd9v/'
    'GEJx/xQhLv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSMy/xhCcf8dd9v/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8eiv+YH4r/yB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZVJX/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlVl/8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r/yB+K/+Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8casD/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HGvB/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K/+Afiv/4'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FjNS/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQiMP8ZV5v/Gl2n/xQiMf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSMz/xpeqf8ZV5v/'
    'FCIw/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xY0Vf8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/4H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//xQeKP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQYHf8dfuj/H4r//x+K//8dgOz/FCY4/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSY5/x2B7v8fiv//H4r//x1+6P8UFxv/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UHin/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8YTIT/H4r//x+K//8fiv//H4r//x1+6P8UIS7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMW'
    'Gf8XOmH/Fzph/xMWGf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FCIw/x1/6v8fiv//H4r//x+K//8fiv//GUl+/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/Fjle/x+K//8fiv//H4r//x+K//8fiv//HXjc/xQcJf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZUpH/H4r//x+K//8ZT4r/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBwl/x153v8fiv//H4r//x+K//8fiv//H4r//xY3Wv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8cctH/H4r//x+K//8fiv//H4r//x+K//8ccc7/FBgd/xQUFP8UFBT/FBQU/xQUFP8UFBT/HHLQ/x+K//8fiv//HHHP/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBke/xxy0P8fiv//H4r//x+K//8fiv//H4r//xxxz/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB4p/x19'
    '5v8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFRf/FBQU/xQUFP8UFBT/FBQU/xxy0P8fiv//H4r//xxxz/8UFBT/FBQU/xQUFP8UFBT/FBUX/xxrwv8fiv//'
    'H4r//x+K//8fiv//H4r//x195v8UHin/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VJjj/HoPy/x+K//8fiv//'
    'H4r//x+K//8fiv//G2Oy/xQUFP8UFBT/FBQU/xQUFP8cctD/H4r//x+K//8ccc//FBQU/xQUFP8UFBT/FBQV/xtisP8fiv//H4r//x+K//8fiv//H4r//x6C'
    '7/8VJjj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xYwTP8eh/r/H4r//x+K//8fiv//H4r//x+K'
    '//8aWp//FBQU/xQUFP8UFBT/HHLQ/x+K//8fiv//HHHP/xQUFP8UFBT/FBQU/xpcpP8fiv//H4r//x+K//8fiv//H4r//x6H+f8WLUf/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Fzti/x+K//8fiv//H4r//x+K//8fiv//H4r//xc9Zv8UFBT/'
    'FBQU/xxy0P8fiv//H4r//xxxz/8UFBT/FBQU/xc9Zv8fiv//H4r//x+K//8fiv//H4r//x+K//8XO2L/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8YRXf/H4r//x+K//8fiv//H4r//x+K//8ccMz/FBQU/xQUFP8cctD/H4r//x+K'
    '//8ccc//FBQU/xQUFP8cctH/H4r//x+K//8fiv//H4r//x+K//8XRHX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GEqA/x+K//8fiv//H4r//x+K//8fiv//HHHO/xQUFP8UFBT/HHLQ/x+K//8fiv//HHHP/xQUFP8UFBT/'
    'HHPS/x+K//8fiv//H4r//x+K//8fiv//GEh9/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/Fz1m/x+K//8fiv//H4r//x+K//8fiv//H4r//xc9Zv8UFBT/FBQU/xxy0P8fiv//H4r//xxxz/8UFBT/FBQU/xc9Z/8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8XOmH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FjBM/x6H'
    '+v8fiv//H4r//x+K//8fiv//H4r//xpYnP8UFBT/FBQU/xQUFP8cctD/H4r//x+K//8ccc//FBQU/xQUFP8UFBT/Gl2m/x+K//8fiv//H4r//x+K//8fiv//'
    'Hof5/xYwTP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSY4/x6D8v8fiv//H4r//x+K//8fiv//'
    'H4r//xtisP8UFBT/FBQU/xQUFP8UFBT/HHLQ/x+K//8fiv//HHHP/xQUFP8UFBT/FBQU/xQUFf8bY7L/H4r//x+K//8fiv//H4r//x+K//8eg/L/FSY4/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB4p/x195v8fiv//H4r//x+K//8fiv//H4r//xxrwv8UFRf/FBQU/xQU'
    'FP8UFBT/FBQU/xxy0P8fiv//H4r//xxxz/8UFBT/FBQU/xQUFP8UFBT/FBUX/xxsxP8fiv//H4r//x+K//8fiv//H4r//x195v8UHin/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xxz0/8fiv//H4r//x+K//8fiv//H4r//xxxzv8UGR7/FBQU/xQUFP8UFBT/FBQU/xQUFP8dd9v/'
    'H4r//x+K//8dd9v/FBQU/xQUFP8UFBT/FBQU/xQUFP8UGh//HHLQ/x+K//8fiv//H4r//x+K//8fiv//HHLR/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xc6Yf8fiv//H4r//x+K//8fiv//H4r//x142/8UHCX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GVKR/x+K//8fiv//GVKR/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQcJf8det//H4r//x+K//8fiv//H4r//x+K//8WOFz/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8ZTIX/H4r//x+K//8fiv//H4r//x195v8UIC3/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMWGf8XOmH/Fzph/xMWGf8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FCAt/x1+6P8fiv//H4r//x+K//8fiv//GEqB/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBgd/x1+6P8fiv//'
    'H4r//x2A7P8VJzr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8VJzv/HYHu/x+K//8fiv//HX3n/xQZHv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UGB3/GEqA/xpjsv8XPWb/FBQU/xQUFP8UIjD/GVeb/xpeqP8VIzP/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQiMf8aXqn/'
    'GVeb/xQgLf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/Fzxl/x+K//8fiv//H4r//xQmOP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//FBQU/xlSkf8fiv//H4r//x+K//8WOF3/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQU'
    'FP8VKT//HoLx/x+K//8deNz/ExYZ/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8WMU7/FjFO/xUzUf8XQGz/'
    'FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYx'
    'Tv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xxxz/8aXab/HXrg/x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlSkP8UFBT/FBQU/xQUFP8UGyP/FjVW/xlNhv8bZ7n/HX/q/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x2B7v8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UICz/Fjhc/xlQjf8bab//HoLx/x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8aYa//FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VJDX/Fzxl/xlUlf8bbcb/'
    'HYT1/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'GEh9/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMVFv8VKDz/F0Bt/xpY'
    'nf8ccc7/Hob5/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYvSv8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMWGf8VK0P/'
    'F0R0/xpcpP8cdNX/Hoj9/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/f8UFhj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQY'
    'Hf8WL0v/GEh8/xthrf8ceNz/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8cctH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xMaIf8WMlH/GEuC/xtltv8de+P/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//G2W2/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQeKP8XNln/GVKR/x2B7/8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/4H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x185f8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8VICv/HX7o/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/4H4r/4B+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//HXfb/xc/av8UHCT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlQ'
    'jf8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/4B+K/8gfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x164f8aYa7/GEl+/xYwTP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZUY7/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K/8geiv+YH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//Hoj8/xxz0v8aW6H/F0Fu/xUoPP8TFBX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/G2W2/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8eiv+YHYn/UB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//HYT0/xxsxP8ZUpH/Fzlf/xQgLP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x6A7P8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'HYn/UBKS/w4fif/5H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//HX/q/xtktP8YS4L/FTFP/xMZH/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQhL/8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/+BKS/w4AAAAAHor/mB+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8eif7/HHXX/xpcpP8XQ3L/FSpA/xMVF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8XO2P/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/5gAAAAAAAAAABqM/xQfif/yH4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8ehff/G23G/xlUlf8XOV//FCIw/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'GVaY/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//EYhv8VAAAAAAAAAAAAAAAAHor/eB+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8dgO3/G2e5/xlNhv8WNFT/FBsi/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBYZ/x6C7/8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv94AAAAAAAAAAAAAAAAAAAAAACA/wQfiv/GH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8dd9v/Gl6o/xhGef8VKT//FCEv/xpZnv8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8eiv/EAID/BAAAAAAAAAAAAAAAAAAAAAAAAAAAG4b/Ex6K/9ofiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/ZGoz/FAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbjf8mH4n/2B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/XGon/JwAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAABSJ/w0fiv+9H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+wFIn/DQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAID/Bh2I/3Ieif/uH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x6J/+4div9xAID/BgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'Hof/ER2K/4seif/zH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4n/8B2K/4seh/8RAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB2J'
    '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'
    '5leVmB2ulv4X3V4jZfdQO7AAAAAASUVORK5CYII='),
    'tray-busy.ico': (
    'AAABAAUAEBAAAAEAIABoBAAAVgAAABQUAAABACAAuAYAAL4EAAAYGAAAAQAgAIgJAAB2CwAAICAAAAEAIACoEAAA/hQAADAwAAABACAAqCUAAKYlAAAoAAAA'
    'EAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB6K/0weiv/EH4r/7x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/7x6K'
    '/8Qeiv9MAAAAAB2J/1Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x2J/1Afif/HH4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/HH4n/8B+K//8bbcf/Exoh/xUlNv8TFRf/FBQU/xQUFP8UFBT/FBQU/xMX'
    'Gv8VJDT/FBsj/xtuyP8fiv//H4n/8B+K//8fiv//GE+L/xQUFP8bbcf/GmGu/xMUFf8WMU7/FjFO/xMUFf8aYrH/HGvC/xQUFP8YT4v/H4r//x+K//8fiv//'
    'H4r//xlPif8UFBT/FSc6/x2E9P8ZVpj/GEl+/xlJfv8ZV5r/HoT0/xQmOP8UFBT/GU+J/x+K//8fiv//H4r//x+K//8ZT4n/FBQU/xMUFP8bZ7v/HHjd/xhJ'
    'fv8ZSX7/HXzk/xxtxf8TFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//GU+J/xQUFP8ZUI3/HoPy/xUpP/8aXaf/Gl2n/xYqQf8eg/P/GE+L/xQUFP8ZT4n/'
    'H4r//x+K//8fiv//H4r//xlQjP8XQ3P/GU+K/xYuSf8UFBT/FBcb/xQXG/8UFBT/Fi9L/xlOiP8UFBT/GU+J/x+K//8fiv//H4r//x+K//8ZXaf/GEuC/xYx'
    'Tv8WMU7/FjFO/xYxTv8WMU7/FjFO/xYxTv8WMU7/FjFO/xpdpv8fiv//H4r//x+K//8fiv//GEyE/xQaH/8WM1L/GU2G/xtlt/8dgO3/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xYxTv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQhL/8XO2L/GVOT/xtsxf8ehff/H4r//x+K//8fiv//H4n/8B+K'
    '//8aYa//Fjhd/xQgLP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBgc/x6C8f8fiv//H4n/8B+J/8cfiv//H4r//x+K//8fiv//HX/q/xtktf8YS4P/'
    'FjJQ/xMaIP8UFBT/FBQU/xQYHP8fiv//H4r//x+J/8cdif9QH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif7/HHTU/xpcpf8bZrj/H4r//x+K'
    '//8dif9QAAAAAB6K/0weiv/EH4r/7x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/7x6K/8Qeiv9MAAAAAIAB//8AAP//AAD//wAA//8AAP//'
    'AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//4AB//8oAAAAFAAAACgAAAABACAAAAAAAJAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAejP8qH4n/nB+K/+Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/4B+J/5wejP8qAAAAAAAAAAAAAAAAHon/iB+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif+AAAAAAB6L/0Qeif/9H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J//0bif9BHon/uB+K//8fiv//Hob5/xxwzf8cbMT/HGzE/xxsxP8cbMT/'
    'HGzE/xxsxP8cbMT/HGzE/xxsxP8cbMT/HHHO/x6H+v8fiv//H4r//x6J/7gfif/oH4r//x+K//8XPWb/FBQU/xQgLf8VHyr/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FSAr/xQgLf8UFBT/Fz1n/x+K//8fiv//H4n/6B+K//8fiv//H4r//xUjM/8UFBT/G2zF/x164P8UHij/FBQU/xYyT/8WMk//FBQU/xUfKv8deuH/'
    'HGvC/xQUFP8VIzP/H4r//x+K//8fiv//H4r//x+K//8fiv//FCIw/xQUFP8VLUf/Hob5/xx01f8UGR7/GVWX/xlVl/8UGR7/HHTV/x6G+f8VLUf/FBQU/xQi'
    'MP8fiv//H4r//x+K//8fiv//H4r//x+K//8UIjD/FBQU/xQUFP8WOV7/Hon+/xpYnP8ZVZf/GVWX/xpZn/8fiv//Fz5o/xQUFP8UFBT/FCIw/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//xQiMP8UFBT/ExYY/xtqwf8eif7/FzZY/xlVl/8ZVZf/FzZZ/x6J/v8basD/ExYY/xQUFP8UIjD/H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//FCIw/xQUFP8aWJz/H4r//xdDcv8UFBT/G2i9/xtovf8UFBT/F0Ny/x+K//8ZVpn/FBQU/xQiMP8fiv//H4r//x+K//8fiv//H4r//x+K//8UKkD/'
    'FSk+/xphr/8ZTIX/FBQU/xQUFP8TGiD/Exog/xQUFP8UFBT/GU2G/xtgrP8UFBT/FCIw/x+K//8fiv//H4r//x+K//8fiv//H4r//xdAbP8aY7L/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UIjD/H4r//x+K//8fiv//H4r//x+K//8fiv//GU+J/xpYnf8bctD/Hoj9/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6D8v8UFBT/FBQU/xQUFP8UGB3/FjBM/xhJfv8aYrD/HXvi/x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//G27I/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FB4o/xc3Wv8ZUY7/'
    'G2m+/x6D8/8fiv//H4r//x+K//8fiv//H4n/6B+K//8eg/L/GU2G/xUxT/8UGB3/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xtg'
    'rP8fiv//H4r//x+J/+geif+4H4r//x+K//8fiv//H4r//x6J/v8cddf/Glyl/xdDcv8WKkH/ExUX/xQUFP8UFBT/FBQU/xQUFP8UFBT/G2zF/x+K//8fiv//'
    'Hon/uBuJ/0Efiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ehff/G23G/xlUlf8XOV//FCIw/xQhLv8eg/P/H4r//x6J//0ei/9EAKr/Ax6J'
    '/4Yeiv/+H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv/+HYr/gwCq/wMAAAAAAAAAAByI/0keiv/N'
    'H4r/7x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/vHor/zRyI/0kAAAAAAAAAAMAAP/+AAB//AAAP/wAAD/8AAA//AAAP/wAA'
    'D/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD//AAD//KAAAABgAAAAwAAAAAQAgAAAAAABgCQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAB6J/10fif+tHon/5R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+Ufif+tHon/XQAA'
    'AAAAAAAAAAAAAAAAAAAajP8oH4r/+B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x6J//Mbif8cAAAAAAAAAAAeif+4H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8eiv+wAAAAAB2J/1Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//HYn/UB6J/7gfiv//H4r//x+K//8ca8P/GVGP/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlP'
    'if8ZT4n/GU+J/xlRj/8cbMT/H4r//x+K//8fiv//Hon/uB+J/+gfiv//H4r//x2E9f8TFhn/FBQU/xQeKP8VJjn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8VJzr/FB0n/xQUFP8TFhn/HYX2/x+K//8fiv//H4n/6B+K//8fiv//H4r//xttxv8UFBT/FBQU/xtqwP8eiP3/FjNT/xQUFP8UFBT/FStD/xYr'
    'Qv8UFBT/FBQU/xY0Vf8eiP3/G2i8/xQUFP8UFBT/G23G/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xc8Zf8fiv//HoT0/xUpPv8UFBT/'
    'GmKx/xphrv8UFBT/FSk+/x2E9P8fiv//Fzti/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xQUFP8ZTYb/H4r//x1/'
    '6v8VHyr/GmOy/xpisf8VHyr/HX/q/x+K//8YS4L/FBQU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xQUFP8UGB3/'
    'HX7p/x+K//8XP2v/GmOy/xpisf8YQW//H4r//x1+6P8UGBz/FBQU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/FBQU/xMU'
    'Ff8bZ7r/H4r//xxvy/8TFhj/GmOy/xpisf8TFhn/HG/L/x+K//8bZ7r/ExQV/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxsxP8UFBT/'
    'FBQU/xlUlP8fiv//HXfb/xQbI/8UFBT/G2W2/xphrv8UFBT/FBwk/xx43P8fiv//GVKQ/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//H4r//xxs'
    'xP8UHCT/FBwk/xlWmP8ced7/FSMy/xQUFP8UFBT/FBsi/xQbIv8UFBT/FBQU/xUjM/8ced//GVSU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//xxsxP8aYa7/GmGu/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HGzE/x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x164f8XR3v/GEyF/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/HXrh/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//xtqwP8UFBT/FBQU/xQdJ/8WN1v/GVKR/xxrwv8dhPT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//xhMhP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFBX/FSc7/xdAbf8aWqD/HHLR/x6I/P8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xY4XP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQXG/8WLkn/GUl+/xpj'
    'sv8ehvn/H4r//x+K//8fiv//H4r//x+J/+gfiv//H4r//xtmuP8VJjn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8YRnj/H4r//x+K//8fiv//H4n/6B6J/7gfiv//H4r//x+K//8fiv//HoPy/xtovf8YT4v/FzZZ/xQdJ/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8YRXf/H4r//x+K//8fiv//Hon/uB2J/2Efiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8deuD/GmGu/xhIfP8WL0r/'
    'FBgc/xQUFP8UFBT/FBQU/xQUFP8aXaf/H4r//x+K//8fiv//HYn/YQAAAAAeiv/EH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//Hoj8/xty0P8aWZ//F0Bs/xc5X/8eg/P/H4r//x+K//8eif/DAAAAAAAAAAAZjP8fHor/uR+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/7kZjP8fAAAAAAAAAAAAAAAAAAAAAB6J/10fif+tHon/5R+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+Ufif+tHon/XQAAAAAAAAAAAAAAAOAAB/+AAAH/gAAB/wAAAP8AAAD/AAAA/wAAAP8AAAD/'
    'AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/4AAAf+AAAH/4AAH/ygAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAG4n/OB6K/5geiv/CHon/7B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8eif/sHor/wh6K/5kbif84AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIID/CB6J/4geiv/8H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K//weiv+JIID/CAAAAAAAAAAA'
    'AAAAAACq/wMfif/OH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/PAKr/AwAAAAAAAAAAHor/eB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif9wAAAAAByK/yUfif/5H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x6J//Mch/8kHon/iB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/4geif+4H4r//x+K//8fiv//H4r//xtpvv8WL0r/'
    'ExYY/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFhn/Fi9L/xtqwP8fiv//H4r//x+K'
    '//8fiv//Hon/uB+J/+gfiv//H4r//x+K//8fiv//FSk+/xQUFP8UFBT/Exca/xc+aP8UGR7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xMaIP8XPmj/Exca/xQUFP8UFBT/FipB/x+K//8fiv//H4r//x+K//8fif/oH4r//x+K//8fiv//H4r//x+K//8TFRf/FBQU/xQUFP8aXaf/H4r//xxz'
    '0/8UGR7/FBQU/xQUFP8UFBT/FSk+/xUpPv8UFBT/FBQU/xQUFP8UGh//HHTV/x+K//8aW6P/FBQU/xQUFP8TFRf/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xlQjf8fiv//H4r//xtnuf8TFBX/FBQU/xQUFP8ceN3/HHjd/xQUFP8UFBT/ExQV/xtnu/8fiv//H4r//xhP'
    'iv8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/ExUX/xxsxP8fiv//H4r//xlWmP8UFBT/'
    'FBQU/x1+6P8dfef/FBQU/xQUFP8ZV5v/H4r//x+K//8cbMT/ExUX/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8UFBT/FBQU/xQUFP8UFBT/FBgd/xxxzv8fiv//H4r//xhCcf8UFBT/HX7o/x195/8UFBT/GEJx/x+K//8fiv//HHDN/xQYHf8UFBT/FBQU/xQUFP8UFBT/'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xQUFP8UFBT/FBwl/x132/8fiv//HXrg/xQUFP8dfuj/HX3n/xQU'
    'FP8dfun/H4r//x132/8UGyL/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//FBQU/xQUFP8UFBT/'
    'FBQU/xQZHv8cctD/H4r//x+K//8YQnH/FBQU/x1+6P8dfef/FBQU/xdDc/8fiv//H4r//xxy0P8UGR7/FBQU/xQUFP8UFBT/FBQU/x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8UFBT/FBQU/xQUFP8TFRf/HGzE/x+K//8fiv//GVWW/xQUFP8UFBT/HX7o/x195/8UFBT/FBQU/xlXmv8fiv//'
    'H4r//xxsxP8TFRf/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xQUFP8UFBT/FBQU/xlQjf8fiv//H4r//xtm'
    'uP8TFBX/FBQU/xQUFP8dd9v/HXfb/xQUFP8UFBT/ExQV/xtnuv8fiv//H4r//xlPiv8UFBT/FBQU/xQUFP8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//FBQU/xQUFP8UFBT/Gl6o/x+K//8cdNT/FBke/xQUFP8UFBT/FBQU/xQeKP8UHij/FBQU/xQUFP8UFBT/FBke/xx01P8fiv//Glyk/xQU'
    'FP8UFBT/FBQU/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8VJDT/HX/r/xhIff8TFxr/F0Bs/xQZHv8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Exkf/xdAbP8TFxr/FBQU/xQUFP8UFBT/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xUk'
    'NP8df+v/GEh9/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//GEqA/xdBbf8YTIT/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlP'
    'if8ZT4n/GU+J/xlPif8ZT4n/GU+J/xlPif8ZT4n/GU+J/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6G+P8UHij/FBQU/xMUFP8WKkH/'
    'F0R0/xlbov8cc9P/Hoj9/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//HGvD/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UGh//FjJP/xhKgP8bY7P/HXzl/x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ZUpD/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FB8r/xY4Xf8ZUpD/HGvC/x2E9P8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//xhDcv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUoPf8XQGz/Glme/x17'
    '4/8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/wH4r//x+K//8fiv//GmKw/xQXG/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExYZ/x174/8fiv//H4r//x+K//8fiv//H4n/6B+K/8gfiv//H4r//x+K//8fiv//HYX2/xts'
    'xf8ZU5P/Fzph/xQhLv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HHbY/x+K//8fiv//'
    'H4r//x+K//8fiv/AHor/mB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x2A7f8bZbf/GUyF/xYzUv8TGiH/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xMWGP8eiP3/H4r//x+K//8fiv//H4r//x6K/5gfiP86Hon//R+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x6J/v8dd9r/Gl6o/xdEdf8VK0P/ExYY/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSxE/x+K//8fiv//H4r//x+K//8eif/9G4j/LwAA'
    'AAAeif+IH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8ehvn/HG/K/xlWmP8XO2L/'
    'FSMz/xMWGf8aXab/H4r//x+K//8fiv//H4r//x6J/4gAAAAAAAAAABuI/y8fif/YH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/QG4j/LwAAAAAAAAAAAAAAACCA/wgeiv+J'
    'Hor//B+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8eiv/8Hon/iACZ/wUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbif84Hor/mR6K/8Ieif/sH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6J/+weiv/CHor/mBuJ/zgAAAAAAAAAAAAAAAAAAAAA8AAAD8AAAAOAAAABgAAAAQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAGAAAAB'
    'wAAAA/AAAA8oAAAAMAAAAGAAAAABACAAAAAAAIAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB2I/z4ei/9/H4n/th+J'
    '/9geif/zH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//Hon/8x+J/9kfif+2Hon/gB2I/z4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAbif8cHor/sx+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+zG4n/HAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB2J'
    '/04fiv/xH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/8R+I/0sAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAHon/gB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif+AAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAdiv9gH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//Hor/XgAAAAAAAAAAAAAAABqJ/ycfiv/4H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r/+ByO/xsAAAAAAAAAAB6J/7gfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K/7AAAAAAGoz/KB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8Yh/8gHon/cB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8dif9oHor/sB+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8dfef/Fztj/xQgLP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQgLf8XPWb/HX7o/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eiv+wH4r/4B+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x6D8v8VIS7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSMz/x6E9P8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/Y'
    'H4r/+B+K//8fiv//H4r//x+K//8fiv//H4r//xpgrf8UFBT/FBQU/xQUFP8UFBT/FBQU/xc9Z/8bYa3/FSQ0/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VJTb/GmCt/xc9Zv8UFBT/FBQU/xQUFP8UFBT/FBQU/xpisP8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fif/wH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlSkf8UFBT/FBQU/xQUFP8UFBT/FzZY/x+K//8fiv//HYHv/xUkNf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUkNf8eg/L/H4r//x+K//8WMlH/FBQU/xQUFP8UFBT/FBQU/xlTk/8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/Glme/x+K//8fiv//H4r//x16'
    '4P8UHSb/FBQU/xQUFP8UFBT/FBQU/xQaH/8cbcX/HG3F/xQaH/8UFBT/FBQU/xQUFP8UFBT/FB0n/x164f8fiv//H4r//x+K//8ZV5r/FBQU/xQUFP8UFBT/'
    'FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FSU2/x6F'
    '9/8fiv//H4r//x+K//8ccM3/FBgc/xQUFP8UFBT/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FBQU/xQUFP8UGBz/HHHP/x+K//8fiv//H4r//x2E9f8VJDT/'
    'FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xc2WP8eiP3/H4r//x+K//8fiv//G2e6/xMUFf8UFBT/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FBQU/xMUFf8bZ7r/H4r//x+K//8fiv//'
    'Hoj8/xYzUv8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlP'
    'if8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8YQ3L/H4r//x+K//8fiv//H4r//xpbof8UFBT/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FBQU/xpbof8fiv//'
    'H4r//x+K//8fiv//Fz9r/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GVKR/x+K//8fiv//H4r//x+K//8YQnD/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/'
    'GEJx/x+K//8fiv//H4r//x+K//8ZUY7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExQU/xphrv8fiv//H4r//x+K//8ceN3/FBQU/xc9Zv8fiv//'
    'H4r//xc9Zv8UFBT/HXvj/x+K//8fiv//H4r//xtmuP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FCY4/x6D8v8fiv//H4r//x+K//8aYrD/'
    'FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/G2Oz/x+K//8fiv//H4r//x2B7/8VJDX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UHin/HX3m/x+K//8fiv//'
    'H4r//x1+6f8UICz/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FCAs/x1+6f8fiv//H4r//x+K//8de+L/FB0m/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlP'
    'if8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/FBQU/xQYHP8cdNX/'
    'H4r//x+K//8fiv//HYT0/xUoPf8UFBT/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FBQU/xUoPf8dhPT/H4r//x+K//8fiv//G3LQ/xQYHP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/'
    'ExQV/xtnuf8fiv//H4r//x+K//8eiP3/FjRU/xQUFP8UFBT/FBQU/xc9Zv8fiv//H4r//xc9Zv8UFBT/FBQU/xQUFP8WNVf/Hoj9/x+K//8fiv//H4r//xtn'
    'uf8TFBX/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFBT/'
    'FBQU/xQUFP8UFBT/GEFv/x+K//8fiv//H4r//x+K//8XP2v/FBQU/xQUFP8UFBT/FBQU/xUtR/8fiv//H4r//xUqQP8UFBT/FBQU/xQUFP8UFBT/Fz9r/x+K'
    '//8fiv//H4r//x+K//8XQG3/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//xlPif8UFBT/FBQU/xQUFP8UFBT/GVWW/x+K//8fiv//H4r//xlQjf8UFBT/FBQU/xQUFP8UFBT/FBQU/xMUFf8XQGz/F0Bs/xMUFf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xlRjv8fiv//H4r//x+K//8ZU5P/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//xlPif8UFBT/FSMy/xQmOP8UFBT/FBsi/x122f8fiv//GVea/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8aWZ7/H4r//xx22P8UGyP/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8VLUf/H4r//x+K//8XPGX/FBQU/xMWGP8VJjf/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FSY3/xMVF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8XQGz/H4r//x+K//8YR3v/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xlPif8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xlPif8UFxv/GE+K/xlVl/8TGiH/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xlPif8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8deuH/G2zF/x2B7/8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xtovP8UFBT/FBQU/xQU'
    'FP8VJDT/Fz1m/xlWmP8cb8r/Hob5/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//xc6'
    'Yf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUsRP8XRHT/Gl2m/xx01f8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//xQdJv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TGiH/FjNS/xhMhP8bZbf/HX/r/x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//HXzk/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQhL/8XO2L/GVSU/xttxv8dhfb/H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//GmKw/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFRb/FSk//xhCcf8aW6P/HHTV/x6I/f8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//GVui/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQZHv8WME3/GEqB/xtktf8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/+B+K//8fiv//H4r//x+K//8fiv//HHXW/xQeKP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UIjD/HX3n/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv/4H4n/8B+K//8fiv//H4r//x+K//8fiv//H4r//x2F9v8aYrD/GEd7/xYvS/8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/G2e7/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fif/wH4r/wB+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8eiPz/G3LQ/xpZn/8XQGz/FSc6/xMUFf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HG3F/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8eif+4HYn/aB+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6E9P8ca8L/GVGP/xY4Xf8VICv/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBX/HoX2/x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8dif9oF4v/Fh6K//4fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HXzl/xpjsv8YSoD/FjFO/xQa'
    'H/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VKT//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K//4giv8Y'
    'AAAAAB6K/7Afiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8cdNT/GVui/xdEdP8WKkH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XRHX/H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x6K/7AAAAAAAAAAABuJ/0Eeif/7H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//HYX2/xtsxf8ZVJT/Fzph/xQhL/8UFBT/FBQU/xQUFP8dddb/H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r/+h6L/0QAAAAAAAAAAAAAAAAfif9bH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8df+v/Gl6p/xty0P8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//HYr/YAAAAAAAAAAAAAAAAAAAAAAA//8BHon/gB6K//4fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x6K//4fiv98AP//AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB+I/0sfiv/xH4r//x+K//8fiv//H4r//x+K'
    '//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r/8R2J/04AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAWhf8XHon/kR+J'
    '//cfiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+J//ceiv+QFoX/FwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAABGI/w8eiv9eHor/sB+J/9gfif/yH4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//'
    'H4r//x+K//8fiv//H4r//x+K//8fiv//H4r//x+K//8fiv//H4n/8h+K/9cfif+vHon/XRGI/w8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAAAAB/'
    '///4AAAAAB////AAAAAAD///4AAAAAAH///AAAAAAAP//4AAAAAAAf//gAAAAAAB//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//'
    'AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAA'
    'AAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAA'
    'AAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//4AAAAAAAf//gAAAAAAB///AAAAAAAP//8AAAAAAA///8AAAAAAP'
    '///4AAAAAB////4AAAAAf///'),
    'tray-idle.ico': (
    'AAABAAUAEBAAAAEAIABoBAAAVgAAABQUAAABACAAuAYAAL4EAAAYGAAAAQAgAIgJAAB2CwAAICAAAAEAIACoEAAA/hQAADAwAAABACAAqCUAAKYlAAAoAAAA'
    'EAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFG5PExPuT7EULk/71C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk/70+5'
    'PsRRuTxMAAAAAFC5PVBQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5PVBPuT/HULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuT/HULk/8FC5P/9BkTT/Fx0V/xwsGv8UFhT/FBQU/xQUFP8UFBT/FBQU/xUY'
    'FP8cKxn/Fx8W/0GSNP9QuT//ULk/8FC5P/9QuT//MWcp/xQUFP9BkTT/OoAv/xMVE/8jPR7/Iz0e/xMVE/87gjD/QI4z/xQUFP8xZyn/ULk//1C5P/9QuT//'
    'ULk//zJmKf8UFBT/HS8a/0yxPP81cCv/Ll4m/y9eJ/81ciz/TbE9/xwtGv8UFBT/MmYp/1C5P/9QuT//ULk//1C5P/8yZin/FBQU/xMUE/8+iTL/RqA4/y5e'
    'Jv8vXif/SKY5/0GQNP8TFBP/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//MmYp/xQUFP8yaSn/TK88/x8yG/85ey7/OXsu/x80HP9MsDz/MWcp/xQUFP8yZin/'
    'ULk//1C5P/9QuT//ULk//zJoKf8rViT/Mmcp/yE5Hf8UFBT/FRkV/xUZFf8UFBT/Ijse/zFlKf8UFBT/MmYp/1C5P/9QuT//ULk//1C5P/85ey7/MGEn/yM9'
    'Hv8jPR7/Iz0e/yM9Hv8jPR7/Iz0e/yM9Hv8jPR7/Iz0e/zl6Lv9QuT//ULk//1C5P/9QuT//MGIo/xccFv8jPx7/MWQo/z2GMf9LrDv/ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//yM9Hv8UFBT/FBQU/xQUFP8UFBT/FBQU/xonGP8nSyL/NG0r/0CQM/9Nsz3/ULk//1C5P/9QuT//ULk/8FC5'
    'P/87gC//Jkcg/xklF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FhoV/0yvPP9QuT//ULk/8E+5P8dQuT//ULk//1C5P/9QuT//Sqo6/z2FMf8wYij/'
    'Ij4e/xYcFf8UFBT/FBQU/xYaFf9QuT//ULk//0+5P8dQuT1QULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuD7/RJo2/zh5Lv89hzL/ULk//1C5'
    'P/9QuT1QAAAAAFG5PExPuT7EULk/71C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk/70+5PsRRuTxMAAAAAIAB//8AAP//AAD//wAA//8AAP//'
    'AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//4AB//8oAAAAFAAAACgAAAABACAAAAAAAJAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AABPvD0qULk+nFC4P+BQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULg/4FC5PpxPvD0qAAAAAAAAAAAAAAAAT7o+iFC5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT6AAAAAAE+4PERQuD79ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C4Pv1OuD9BT7g+uFC5P/9QuT//TrQ9/0OVNf9BjzT/QY80/0GPNP9BjzT/'
    'QY80/0GPNP9BjzT/QY80/0GPNP9BjzT/Q5Y1/061Pf9QuT//ULk//0+4PrhPuT/oULk//1C5P/8pTiP/FBQU/xolGP8ZJBj/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/GiQY/xolGP8UFBT/KE4i/1C5P/9QuT//T7k/6FC5P/9QuT//ULk//xsqGf8UFBT/QJAz/0ejOP8ZIhf/FBQU/yM9Hv8jPR7/FBQU/xkkGP9Iozn/'
    'QI4z/xQUFP8bKhn/ULk//1C5P/9QuT//ULk//1C5P/9QuT//GygY/xQUFP8gOB3/TrQ9/0SbNv8WGxX/NXAr/zVwK/8WGxX/RJs2/060Pf8gOB3/FBQU/xso'
    'GP9QuT//ULk//1C5P/9QuT//ULk//1C5P/8bKBj/FBQU/xQUFP8mSCH/T7g+/zZzLP81cCv/NXAr/zd1Lf9QuT//KU8i/xQUFP8UFBT/GygY/1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//xsoGP8UFBT/FBcU/0CNM/9PuD7/JUQg/zVwK/81cCv/JUQg/0+4Pv8/jDP/FBcU/xQUFP8bKBj/ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//GygY/xQUFP82cyz/ULk//ytWJP8UFBT/Pooy/z6KMv8UFBT/K1Yk/1C5P/81cSv/FBQU/xsoGP9QuT//ULk//1C5P/9QuT//ULk//1C5P/8eMxv/'
    'HjEb/zuAL/8wYyj/FBQU/xQUFP8WHBX/FhwV/xQUFP8UFBT/MWQo/zp+L/8UFBT/GygY/1C5P/9QuT//ULk//1C5P/9QuT//ULk//ypSI/88gzD/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8bKBj/ULk//1C5P/9QuT//ULk//1C5P/9QuT//MmYp/zZ0LP9DlzX/T7c+/1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0yvPP8UFBT/FBQU/xQUFP8WGhX/Ijse/y5eJv87gTD/SKQ5/1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//QZI0/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GSIX/yZFIP8yaSn/'
    'P4sy/0ywPP9QuT//ULk//1C5P/9QuT//T7k/6FC5P/9Mrzz/MWQo/yI9Hv8WGhX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zp+'
    'L/9QuT//ULk//0+5P+hPuD64ULk//1C5P/9QuT//ULk//0+4Pv9FnDf/OHku/ytWJP8fNBz/FBYU/xQUFP8UFBT/FBQU/xQUFP8UFBT/QJAz/1C5P/9QuT//'
    'T7g+uE64P0FQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9Nsz3/QJAz/zRuKv8mSCH/GygY/xomGP9MsDz/ULk//1C4Pv1PuDxEVaoAA1C5'
    'P4ZPuT7+ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuT7+ULk+g1WqAAMAAAAAAAAAAFC5P0lQuD7N'
    'ULk/71C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT/vULg+zVC5P0kAAAAAAAAAAMAAP/+AAB//AAAP/wAAD/8AAA//AAAP/wAA'
    'D/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD//AAD//KAAAABgAAAAwAAAAAQAgAAAAAABgCQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAFC4P11QuD6tT7k+5VC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+5PuVQuD6tULg/XQAA'
    'AAAAAAAAAAAAAAAAAABNuUAoT7g/+FC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/NStkAcAAAAAAAAAABPuD64ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuD6wAAAAAFC5PVBQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk9UE+4PrhQuT//ULk//1C5P/9AjzT/M2oq/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJm'
    'Kf8yZin/MmYp/zNqKv9BjzT/ULk//1C5P/9QuT//T7g+uE+5P+hQuT//ULk//0yxPP8VGBT/FBQU/xkiF/8dLhr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8dLxr/GCEX/xQUFP8VGBT/TbI8/1C5P/9QuT//T7k/6FC5P/9QuT//ULk//0CQM/8UFBT/FBQU/z+MM/9Ptz7/I0Af/xQUFP8UFBT/HzUc/x80'
    'HP8UFBT/FBQU/yRCH/9Ptz7/Poky/xQUFP8UFBT/QJAz/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GPNP8UFBT/FBQU/yhNIv9QuT//TbE9/x4xG/8UFBT/'
    'O4Iw/zqAL/8UFBT/HjEb/0yxPP9QuT//J0si/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GPNP8UFBT/FBQU/xQUFP8xZCj/ULk//0qq'
    'Ov8ZJBj/PIMw/zuCMP8ZJBj/Sqo6/1C5P/8vYSf/FBQU/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GPNP8UFBT/FBQU/xQUFP8WGhX/'
    'Sak6/1C5P/8qUSP/PIMw/zuCMP8rVCT/ULk//0moOv8WGhX/FBQU/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GPNP8UFBT/FBQU/xMV'
    'E/89iDH/ULk//0KUNf8UFxT/PIMw/zuCMP8VGBT/QpQ1/1C5P/89iDH/ExUT/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GPNP8UFBT/'
    'FBQU/zRuK/9QuT//Rp84/xcfFv8UFBT/PYUx/zqAL/8UFBT/Fx8W/0agN/9QuT//M2sq/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//ULk//0GP'
    'NP8XHxb/Fx8W/zVwK/9HoTj/GykZ/xQUFP8UFBT/Fx4W/xceFv8UFBT/FBQU/xsqGf9Hojj/NG4r/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//0GPNP86gC//OoAv/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/QY80/1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//0ijOf8sXCT/L2In/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/SKM5/1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//z+MM/8UFBT/FBQU/xghF/8lRiD/M2wq/0COM/9MsTz/ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//zBiKP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8TFRP/HS8a/ypSJP83di3/Q5g1/0+2Pv9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//yZGIP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUZFf8hOR3/L14n/zyD'
    'MP9OtD3/ULk//1C5P/9QuT//ULk//0+5P+hQuT//ULk//z2HMv8dLhr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8tWib/ULk//1C5P/9QuT//T7k/6E+4PrhQuT//ULk//1C5P/9QuT//TK88/z6KMv8xZyn/JUQg/xghF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8tWSX/ULk//1C5P/9QuT//T7g+uE+4P2FQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9Hozj/OoAv/y5dJ/8hOh3/'
    'FhoV/xQUFP8UFBT/FBQU/xQUFP85ey7/ULk//1C5P/9QuT//T7g/YQAAAABPuT7EULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//T7Y+/0OXNf83dS3/KlIj/yZIIf9MsDz/ULk//1C5P/9QuD/DAAAAAAAAAABSvTofULk+uVC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5PrlSvTofAAAAAAAAAAAAAAAAAAAAAFC4P11QuD6tT7k+5VC5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+5PuVQuD6tULg/XQAAAAAAAAAAAAAAAOAAB/+AAAH/gAAB/wAAAP8AAAD/AAAA/wAAAP8AAAD/'
    'AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/4AAAf+AAAH/4AAH/ygAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAATbtAOE+5PphPuD7CULk/7FC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT/sT7g+wlC5PplNu0A4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAQL9ACE+6PohQuD/8ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C4P/xQuD2JQL9ACAAAAAAAAAAA'
    'AAAAAFWqAANPuD7OULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT/PVaoAAwAAAAAAAAAAT7k+eFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuD1wAAAAAEy6PiVQuD75ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/NOuEAkT7o+iFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+6PohPuD64ULk//1C5P/9QuT//ULk//z+LMv8hOh3/'
    'FBcU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VGBT/Ijse/z+MM/9QuT//ULk//1C5'
    'P/9QuT//T7g+uE+5P+hQuT//ULk//1C5P/9QuT//HjEb/xQUFP8UFBT/FRgU/ylPIv8WGxX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xYcFf8pTyL/FRgU/xQUFP8UFBT/HzQc/1C5P/9QuT//ULk//1C5P/9PuT/oULk//1C5P/9QuT//ULk//1C5P/8UFhT/FBQU/xQUFP85ey7/ULk//0Sa'
    'Nv8WGxX/FBQU/xQUFP8UFBT/HjEb/x4xG/8UFBT/FBQU/xQUFP8XHBb/RJs2/1C5P/84eC3/FBQU/xQUFP8UFhT/ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//xQUFP8UFBT/FBQU/zJpKf9QuT//ULk//z6IMv8TFRP/FBQU/xQUFP9GoDj/RqA4/xQUFP8UFBT/ExUT/z6JMv9QuT//ULk//zFm'
    'KP8UFBT/FBQU/xQUFP9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//FBQU/xQUFP8UFBT/FBYU/0GPNP9QuT//ULk//zVwK/8UFBT/'
    'FBQU/0qoOv9JqDr/FBQU/xQUFP82ciz/ULk//1C5P/9BjzT/FBYU/xQUFP8UFBT/FBQU/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/8UFBT/FBQU/xQUFP8UFBT/FhoV/0OWNf9QuT//ULk//ytVJf8UFBT/Sqg6/0moOv8UFBT/K1Ul/1C5P/9QuT//Q5U1/xYaFf8UFBT/FBQU/xQUFP8UFBT/'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//xQUFP8UFBT/FBQU/xQUFP8UFBT/GCAW/0afOP9QuT//R6M4/xQUFP9KqDr/Sag6/xQU'
    'FP9JqTr/ULk//0afOP8XHhb/FBQU/xQUFP8UFBT/FBQU/xQUFP9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//FBQU/xQUFP8UFBT/'
    'FBQU/xYbFf9EmDb/ULk//1C5P/8rVSX/FBQU/0qoOv9JqDr/FBQU/ytWJP9QuT//ULk//0SYNv8WGxX/FBQU/xQUFP8UFBT/FBQU/1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/8UFBT/FBQU/xQUFP8UFhT/QY80/1C5P/9QuT//NG8r/xQUFP8UFBT/Sqg6/0moOv8UFBT/FBQU/zVyLP9QuT//'
    'ULk//0GPNP8UFhT/FBQU/xQUFP8UFBT/ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//xQUFP8UFBT/FBQU/zJpKf9QuT//ULk//z2H'
    'Mv8TFRP/FBQU/xQUFP9Gnzj/Rp84/xQUFP8UFBT/ExUT/z2IMf9QuT//ULk//zJnKf8UFBT/FBQU/xQUFP9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//FBQU/xQUFP8UFBT/OXwu/1C5P/9Emjb/FhsV/xQUFP8UFBT/FBQU/xkiF/8ZIhf/FBQU/xQUFP8UFBT/FhsV/0SaNv9QuT//OHkt/xQU'
    'FP8UFBT/FBQU/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/8cKxn/Sqo6/y5eJ/8VGBT/KlIj/xYbFf8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FhwV/ypSI/8VGBT/FBQU/xQUFP8UFBT/ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//xwr'
    'Gf9Kqjr/Ll4n/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//L18n/ypSI/8wYif/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/zJm'
    'Kf8yZin/MmYp/zJmKf8yZin/MmYp/zJmKf8yZin/MmYp/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//02zPf8ZIhf/FBQU/xMUE/8fNBz/'
    'LFcl/zd3Lf9Emjb/T7c+/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//QI80/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XHBb/Iz0e/y9fJ/88gzD/SaY6/1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/8zayr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/GSQX/yZHIP8zayr/QI4z/0yxPP9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//yxWJf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x4xG/8qUiP/N3Ut/0il'
    'Of9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT/wULk//1C5P/9QuT//O4Ew/xUZFf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FRgU/0ilOf9QuT//ULk//1C5P/9QuT//T7k/6E+5PshQuT//ULk//1C5P/9QuT//TbI8/0CQ'
    'M/80bSv/J0oh/xomGP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/RZ03/1C5P/9QuT//'
    'ULk//1C5P/9QuT7AT7k+mFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0usO/89hjH/MGMo/yM/Hv8XHRX/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQXFP9Ptz7/ULk//1C5P/9QuT//ULk//0+5PphPuT46ULg+/VC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//0+4Pv9Gnzj/OXwu/yxYJf8fNRz/FBcU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HzYc/1C5P/9QuT//ULk//1C5P/9QuD79Ubg8LwAA'
    'AABPuj6IULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9OtD3/QpM0/zVwK/8nSyL/'
    'GyoZ/xUYFP85ei7/ULk//1C5P/9QuT//ULk//0+6PogAAAAAAAAAAFG4PC9PuD/YULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT/QUbg8LwAAAAAAAAAAAAAAAEC/QAhQuD2J'
    'ULg//FC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuD/8T7o+iDPMMwUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABNu0A4ULk+mU+4PsJQuT/sULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P+xPuD7CT7k+mE27QDgAAAAAAAAAAAAAAAAAAAAA8AAAD8AAAAOAAAABgAAAAQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAGAAAAB'
    'wAAAA/AAAA8oAAAAMAAAAGAAAAABACAAAAAAAIAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE65Pj5QuT5/ULk/tk+4'
    'P9hQuT/zULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk/81C4PtlQuT+2ULk+gE65Pj4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABStkAcULk/s1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT+zUrZAHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE66'
    'Pk5PuD7xULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//T7g+8U64PUsAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAULk+gFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT6AAAAAAAAA'
    'AAAAAAAAAAAAAAAAAABQuj1gULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//T7g+XgAAAAAAAAAAAAAAAE63OydPuD/4ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//T7g/+Ey9ORsAAAAAAAAAAE+4PrhQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C4PrAAAAAATblAKFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9Qt0AgULg9cFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9OuD1oULg+sFC5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9JqDr/KEsi/xklF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xolGP8pTiP/Sag6/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuD6wULg/4FC5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//0yvPP8aJhj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GykZ/02xPf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuD/Y'
    'T7g/+FC5P/9QuT//ULk//1C5P/9QuT//ULk//zp/L/8UFBT/FBQU/xQUFP8UFBT/FBQU/yhOIv87fzD/HCsZ/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8cLBr/On8v/ylOI/8UFBT/FBQU/xQUFP8UFBT/FBQU/zuBMP9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT/wULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zNsKv8UFBT/FBQU/xQUFP8UFBT/JUQg/1C5P/9QuT//S607/xwrGv8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xwrGv9Mrzz/ULk//1C5P/8jPx7/FBQU/xQUFP8UFBT/FBQU/zRtK/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/N3Ut/1C5P/9QuT//ULk//0ej'
    'OP8YIRf/FBQU/xQUFP8UFBT/FBQU/xccFv9BkDT/QZA0/xccFv8UFBT/FBQU/xQUFP8UFBT/GCEX/0ijOf9QuT//ULk//1C5P/81ciz/FBQU/xQUFP8UFBT/'
    'FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/HCwa/02z'
    'Pf9QuT//ULk//1C5P/9DlTX/FhoV/xQUFP8UFBT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/FBQU/xQUFP8WGhX/Q5c2/1C5P/9QuT//ULk//0yxPP8cKxn/'
    'FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/yVEIP9Ptz7/ULk//1C5P/9QuT//PYgx/xMVE/8UFBT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/FBQU/xMVE/89iDH/ULk//1C5P/9QuT//'
    'T7Y+/yM/Hv8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJm'
    'Kf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8sViX/ULk//1C5P/9QuT//ULk//zh3Lf8UFBT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/FBQU/zh3Lf9QuT//'
    'ULk//1C5P/9QuT//KlEj/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/M2wq/1C5P/9QuT//ULk//1C5P/8rVCT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/'
    'K1Ul/1C5P/9QuT//ULk//1C5P/8yaSn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExQT/zqAL/9QuT//ULk//1C5P/9GoDj/FBQU/yhNIv9QuT//'
    'ULk//ylOI/8UFBT/SKU5/1C5P/9QuT//ULk//z2HMv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HC0a/0yvPP9QuT//ULk//1C5P/87gTD/'
    'FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/PIMw/1C5P/9QuT//ULk//0utO/8cKxr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8ZIxf/Sac6/1C5P/9QuT//'
    'ULk//0mpOv8ZJRf/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/GSUX/0mpOv9QuT//ULk//1C5P/9IpDn/GCEX/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJm'
    'Kf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xYaFf9Emzb/'
    'ULk//1C5P/9QuT//TLE8/x4xG/8UFBT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/FBQU/x4xG/9MsTz/ULk//1C5P/9QuT//Q5c1/xYaFf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/'
    'ExUT/z6IMv9QuT//ULk//1C5P/9Ptz7/JEEf/xQUFP8UFBT/FBQU/yhNIv9QuT//ULk//ylOI/8UFBT/FBQU/xQUFP8lQyD/T7c+/1C5P/9QuT//ULk//z6I'
    'Mv8TFRP/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8UFBT/'
    'FBQU/xQUFP8UFBT/K1Qk/1C5P/9QuT//ULk//1C5P/8qUSP/FBQU/xQUFP8UFBT/FBQU/yA4Hf9QuT//ULk//x8zHP8UFBT/FBQU/xQUFP8UFBT/KlEj/1C5'
    'P/9QuT//ULk//1C5P/8qUiT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//zJmKf8UFBT/FBQU/xQUFP8UFBT/NG8r/1C5P/9QuT//ULk//zJpKf8UFBT/FBQU/xQUFP8UFBT/FBQU/xMVE/8qUiP/KlIj/xMVE/8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/zJpKf9QuT//ULk//1C5P/80bSv/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//zJmKf8UFBT/GykZ/xwtGv8UFBT/Fx4W/0aeN/9QuT//NXIs/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP83dS3/ULk//0WdN/8XHxb/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8gOB3/ULk//1C5P/8oTSL/FBQU/xQXFP8dLRr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HS0a/xQWFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8qUiP/ULk//1C5P/8uXCb/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zJmKf9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//zJmKf8VGRX/MWYo/zVwK/8XHRX/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/zJmKf9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9Iozn/QJAz/0utO/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//z6JMv8UFBT/FBQU/xQU'
    'FP8cKxn/KU4j/zVwK/9CkzT/TrQ9/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//ydK'
    'If8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x82HP8sVyX/OXou/0SbNv9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//xggF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XHRX/Iz8e/zBiKP89hjH/Sqo6/1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//SKY5/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xonGP8nSyL/NG4r/0CQM/9Nsjz/ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//O4Ew/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFRP/HzIb/ytVJf84eC3/RJs2/0+3Pv9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//N3ct/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xYbFf8iPB7/L2An/z2FMf9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//T7g/+FC5P/9QuT//ULk//1C5P/9QuT//RZw3/xkiF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8bKBj/Sag6/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuD/4ULk/8FC5P/9QuT//ULk//1C5P/9QuT//ULk//02yPP87gTD/Llwm/yI7Hv8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/Poky/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT/wULk+wFC5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9Ptj7/Q5c1/zd1Lf8qUiP/HS8a/xMVE/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/QZA0/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9PuD64Trg9aFC5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//02xPf9AjjP/M2oq/yZHIP8aJBj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFRT/TbM9/1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9OuD1oUbk6Fk+5Pv5QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//SaY6/zyDMP8vXyf/Iz0e/xcc'
    'Fv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8fMhv/ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+5Pv5Kv0AY'
    'AAAAAFC4PrBQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9Emjb/N3ct/yxXJf8fNBz/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8sWCX/ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C4PrAAAAAAAAAAAE64P0FPuT/7ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//TbI8/0CQM/80biv/J0oh/xonGP8UFBT/FBQU/xQUFP9FnDf/ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk++k+4PEQAAAAAAAAAAAAAAABOuT5bULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9Kqjr/OXwv/0OXNf9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULo9YAAAAAAAAAAAAAAAAAAAAAAA/wABULk+gE+5Pv5QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+5Pv5QuT58AP8AAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE64PUtPuD7xULk//1C5P/9QuT//ULk//1C5'
    'P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//T7g+8U66Pk4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABOvDcXT7k+kU+5'
    'P/dQuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//0+5P/dQuD6QTrw3FwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAFW7Mw9PuD5eULg+sE+4P9hPuD7yULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//'
    'ULk//1C5P/9QuT//ULk//1C5P/9QuT//ULk//1C5P/9QuT//T7g+8k+5P9dQuT+vULg/XVW7Mw8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAAAAB/'
    '///4AAAAAB////AAAAAAD///4AAAAAAH///AAAAAAAP//4AAAAAAAf//gAAAAAAB//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//'
    'AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAA'
    'AAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAA'
    'AAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//4AAAAAAAf//gAAAAAAB///AAAAAAAP//8AAAAAAA///8AAAAAAP'
    '///4AAAAAB////4AAAAAf///'),
    'tray-off.ico': (
    'AAABAAUAEBAAAAEAIABoBAAAVgAAABQUAAABACAAuAYAAL4EAAAYGAAAAQAgAIgJAAB2CwAAICAAAAEAIACoEAAA/hQAADAwAAABACAAqCUAAKYlAAAoAAAA'
    'EAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJeNjUyWi4vElYyM75aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM75aL'
    'i8SXjY1MAAAAAJaMjFCWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjFCWjIzHloyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIzHlouL8JaMjP92b2//Gxoa/yclJf8VFRX/FBQU/xQUFP8UFBT/FBQU/xcX'
    'F/8mJCT/HBsb/3dvb/+WjIz/louL8JaMjP+WjIz/VVBQ/xQUFP92b2//aWJi/xQUFP80MjL/NDIy/xQUFP9qZGT/dGxs/xQUFP9VUFD/loyM/5aMjP+WjIz/'
    'loyM/1VQUP8UFBT/KScn/4+Fhf9cV1f/TklJ/05KSv9eWFj/j4aG/ygmJv8UFBT/VVBQ/5aMjP+WjIz/loyM/5aMjP9VUFD/FBQU/xQUFP9waWn/gnp6/05J'
    'Sf9OSkr/h35+/3Zubv8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/VVBQ/xQUFP9WUVH/joWF/ywqKv9lX1//ZV9f/y0rK/+PhYX/VVBQ/xQUFP9VUFD/'
    'loyM/5aMjP+WjIz/loyM/1dRUf9IRET/VVBQ/zEvL/8UFBT/GBcX/xgXF/8UFBT/MjAw/1RPT/8UFBT/VVBQ/5aMjP+WjIz/loyM/5aMjP9kXl7/UU1N/zQy'
    'Mv80MjL/NDIy/zQyMv80MjL/NDIy/zQyMv80MjL/NDIy/2ReXv+WjIz/loyM/5aMjP+WjIz/Uk1N/xoaGv82MzP/U05O/25nZ/+LgoL/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/zQyMv8UFBT/FBQU/xQUFP8UFBT/FBQU/yMhIf8/PDz/WlVV/3Vubv+Rh4f/loyM/5aMjP+WjIz/louL8JaM'
    'jP9pYmL/PDk5/yEgIP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GBgY/46EhP+WjIz/louL8JaMjMeWjIz/loyM/5aMjP+WjIz/ioCA/21mZv9RTEz/'
    'NTIy/xoaGv8UFBT/FBQU/xgYGP+WjIz/loyM/5aMjMeWjIxQloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Vi4v/fnV1/2RdXf9uZ2f/loyM/5aM'
    'jP+WjIxQAAAAAJeNjUyWi4vElYyM75aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM75aLi8SXjY1MAAAAAIAB//8AAP//AAD//wAA//8AAP//'
    'AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//4AB//8oAAAAFAAAACgAAAABACAAAAAAAJAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AACYjIwqlYuLnJWMjOCWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM4JWLi5yYjIwqAAAAAAAAAAAAAAAAlouLiJaMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Vi4uAAAAAAJaLi0SVi4v9loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi/2VjY1BloyMuJaMjP+WjIz/koiI/3pycv91bm7/dW5u/3Vubv91bm7/'
    'dW5u/3Vubv91bm7/dW5u/3Vubv91bm7/enNz/5OJif+WjIz/loyM/5aMjLiVjIzoloyM/5aMjP9BPj7/FBQU/yEgIP8gHx//FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/ISAg/yEgIP8UFBT/QT4+/5aMjP+WjIz/lYyM6JaMjP+WjIz/loyM/yUkJP8UFBT/dW5u/4R7e/8fHh7/FBQU/zUyMv81MjL/FBQU/yAfH/+FfHz/'
    'dGxs/xQUFP8lJCT/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/IyIi/xQUFP8wLi7/koiI/352dv8aGRn/XFZW/1xWVv8aGRn/fnZ2/5KIiP8wLi7/FBQU/yMi'
    'Iv+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP8jIiL/FBQU/xQUFP89OTn/lYuL/19ZWf9cVlb/XFZW/2FbW/+WjIz/Qj4+/xQUFP8UFBT/IyIi/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/yMiIv8UFBT/FhYW/3NsbP+Vi4v/OTc3/1xWVv9cVlb/Ojc3/5WLi/9za2v/FhYW/xQUFP8jIiL/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/IyIi/xQUFP9fWVn/loyM/0dDQ/8UFBT/cWpq/3Fqav8UFBT/R0ND/5aMjP9dV1f/FBQU/yMiIv+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP8sKir/'
    'Kykp/2liYv9STU3/FBQU/xQUFP8aGhr/Ghoa/xQUFP8UFBT/U05O/2hhYf8UFBT/IyIi/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/0NBQf9rZGT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8jIiL/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/VVBQ/19aWv97c3P/lIqK/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/46Fhf8UFBT/FBQU/xQUFP8ZGRn/MzAw/05JSf9qY2P/hX19/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/d29v/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Hx4e/zs4OP9XUlL/'
    'cWpq/4+Fhf+WjIz/loyM/5aMjP+WjIz/lYyM6JaMjP+OhYX/U05O/zQyMv8ZGRn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/2hh'
    'Yf+WjIz/loyM/5WMjOiWjIy4loyM/5aMjP+WjIz/loyM/5WLi/9/d3f/ZF1d/0dDQ/8tKyv/FRUV/xQUFP8UFBT/FBQU/xQUFP8UFBT/dW5u/5aMjP+WjIz/'
    'loyMuJWNjUGWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Rh4f/dm5u/1tVVf89Ojr/IyIi/yIhIf+PhYX/loyM/5WLi/2Wi4tEqqqqA5aL'
    'i4aWjIz+loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz+loyMg6qqqgMAAAAAAAAAAJaMjEmVi4vN'
    'lYyM75aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+VjIzvlYuLzZaMjEkAAAAAAAAAAMAAP/+AAB//AAAP/wAAD/8AAA//AAAP/wAA'
    'D/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD//AAD//KAAAABgAAAAwAAAAAQAgAAAAAABgCQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAJeMjF2VjIytlYuL5ZaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi+WVjIytl4yMXQAA'
    'AAAAAAAAAAAAAAAAAACZjIwolYyM+JaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5WMjPOSiYkcAAAAAAAAAACWjIy4loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+Vi4uwAAAAAJaMjFCWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyMUJaMjLiWjIz/loyM/5aMjP90bW3/WFJS/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQ'
    'UP9VUFD/VVBQ/1hSUv91bm7/loyM/5aMjP+WjIz/loyMuJWMjOiWjIz/loyM/5CGhv8XFhb/FBQU/x8eHv8oJib/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8pJyf/Hh4e/xQUFP8XFhb/kIeH/5aMjP+WjIz/lYyM6JaMjP+WjIz/loyM/3Zubv8UFBT/FBQU/3Nra/+Uior/NzQ0/xQUFP8UFBT/Lisr/y0r'
    'K/8UFBT/FBQU/zg1Nf+Uior/cGlp/xQUFP8UFBT/dm5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vubv8UFBT/FBQU/0E9Pf+WjIz/j4aG/yspKf8UFBT/'
    'amRk/2liYv8UFBT/Kykp/4+Fhf+WjIz/Pzw8/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vubv8UFBT/FBQU/xQUFP9TTk7/loyM/4qA'
    'gP8gHx//a2Rk/2pkZP8gHx//ioCA/5aMjP9QTEz/FBQU/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vubv8UFBT/FBQU/xQUFP8ZGRn/'
    'iYCA/5aMjP9EQED/a2Rk/2pkZP9GQkL/loyM/4h/f/8YGBj/FBQU/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vubv8UFBT/FBQU/xQU'
    'FP9vaGj/loyM/3lxcf8WFhb/a2Rk/2pkZP8XFhb/eXFx/5aMjP9vaGj/FBQU/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vubv8UFBT/'
    'FBQU/1tVVf+WjIz/gnl5/xwbG/8UFBT/bWZm/2liYv8UFBT/HRwc/4J5ef+WjIz/WFNT/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Vu'
    'bv8dHBz/HRwc/1xXV/+Denr/JCMj/xQUFP8UFBT/Gxsb/xsbG/8UFBT/FBQU/yUkJP+Ee3v/W1VV/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/3Vubv9pYmL/aWJi/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/dW5u/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/4V8fP9NSEj/Uk1N/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/hXx8/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/3Nra/8UFBT/FBQU/x4eHv87ODj/WVNT/3RsbP+PhYX/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/1JNTf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/KSgo/0VBQf9hW1v/fHR0/5SKiv+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/zs4OP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xgXF/8xLy//TkpK/2tk'
    'ZP+SiIj/loyM/5aMjP+WjIz/loyM/5WMjOiWjIz/loyM/25nZ/8oJib/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP9LR0f/loyM/5aMjP+WjIz/lYyM6JaMjLiWjIz/loyM/5aMjP+WjIz/joWF/3Fqav9VUFD/Ojc3/x4eHv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP9KRkb/loyM/5aMjP+WjIz/loyMuJaLi2GWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Ee3v/aWJi/01JSf8yLy//'
    'GBgY/xQUFP8UFBT/FBQU/xQUFP9lX1//loyM/5aMjP+WjIz/louLYQAAAACWi4vEloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/lIqK/3tzc/9hW1v/REFB/z06Ov+PhYX/loyM/5aMjP+VjIzDAAAAAAAAAACUjIwflYuLuZaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi7mUjIwfAAAAAAAAAAAAAAAAAAAAAJeMjF2VjIytlYuL5ZaMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi+WVjIytl4yMXQAAAAAAAAAAAAAAAOAAB/+AAAH/gAAB/wAAAP8AAAD/AAAA/wAAAP8AAAD/'
    'AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/4AAAf+AAAH/4AAH/ygAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAlo2NOJWLi5iWi4vClYuL7JaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+Vi4vslouLwpaMjJmWjY04AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAn4CACJaLi4iWjIz8loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjPyVjIyJn4CACAAAAAAAAAAA'
    'AAAAAKqqqgOWjIzOloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Vi4vPqqqqAwAAAAAAAAAAlYyMeJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Wi4twAAAAAJiKiiWWi4v5loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5WMjPOVjo4klouLiJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aLi4iWjIy4loyM/5aMjP+WjIz/loyM/3Fqav8yLy//'
    'FhYW/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XFhb/MjAw/3Nra/+WjIz/loyM/5aM'
    'jP+WjIz/loyMuJWMjOiWjIz/loyM/5aMjP+WjIz/Kykp/xQUFP8UFBT/FxcX/0I+Pv8aGRn/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xoaGv9CPj7/FxcX/xQUFP8UFBT/LSsr/5aMjP+WjIz/loyM/5aMjP+VjIzoloyM/5aMjP+WjIz/loyM/5aMjP8VFRX/FBQU/xQUFP9lX1//loyM/311'
    'df8aGRn/FBQU/xQUFP8UFBT/Kykp/yspKf8UFBT/FBQU/xQUFP8aGhr/fnZ2/5aMjP9iXFz/FBQU/xQUFP8VFRX/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/xQUFP8UFBT/FBQU/1ZRUf+WjIz/loyM/29oaP8UFBT/FBQU/xQUFP+Cenr/gnp6/xQUFP8UFBT/FBQU/3Bpaf+WjIz/loyM/1VQ'
    'UP8UFBT/FBQU/xQUFP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/FBQU/xQUFP8UFBT/FRUV/3Vubv+WjIz/loyM/1xXV/8UFBT/'
    'FBQU/4mAgP+If3//FBQU/xQUFP9eWFj/loyM/5aMjP91bm7/FRUV/xQUFP8UFBT/FBQU/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP8UFBT/FBQU/xQUFP8UFBT/GRkZ/3pzc/+WjIz/loyM/0dDQ/8UFBT/iYCA/4h/f/8UFBT/R0ND/5aMjP+WjIz/enJy/xkZGf8UFBT/FBQU/xQUFP8UFBT/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/xQUFP8UFBT/FBQU/xQUFP8UFBT/HRwc/4J5ef+WjIz/hHt7/xQUFP+JgID/iH9//xQU'
    'FP+JgID/loyM/4J5ef8bGxv/FBQU/xQUFP8UFBT/FBQU/xQUFP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/FBQU/xQUFP8UFBT/'
    'FBQU/xoZGf98dHT/loyM/5aMjP9HQ0P/FBQU/4mAgP+If3//FBQU/0hERP+WjIz/loyM/3x0dP8aGRn/FBQU/xQUFP8UFBT/FBQU/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP8UFBT/FBQU/xQUFP8VFRX/dW5u/5aMjP+WjIz/W1ZW/xQUFP8UFBT/iYCA/4h/f/8UFBT/FBQU/15YWP+WjIz/'
    'loyM/3Vubv8VFRX/FBQU/xQUFP8UFBT/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/xQUFP8UFBT/FBQU/1ZRUf+WjIz/loyM/25n'
    'Z/8UFBT/FBQU/xQUFP+CeXn/gnl5/xQUFP8UFBT/FBQU/29oaP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/FBQU/xQUFP8UFBT/ZV9f/5aMjP9+dXX/GhkZ/xQUFP8UFBT/FBQU/x8eHv8fHh7/FBQU/xQUFP8UFBT/GhkZ/351df+WjIz/Y11d/xQU'
    'FP8UFBT/FBQU/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP8mJCT/ioGB/05JSf8XFxf/REFB/xoZGf8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/GhkZ/0RBQf8XFxf/FBQU/xQUFP8UFBT/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/yYk'
    'JP+KgYH/TklJ/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/UEtL/0VBQf9STU3/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQ'
    'UP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/1VQUP9VUFD/VVBQ/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5GIiP8fHh7/FBQU/xQUFP8tKyv/'
    'SURE/2JcXP99dXX/lIqK/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/dG1t/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8aGhr/NTIy/09LS/9rZWX/h35+/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP9YU1P/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/IB8f/zw5Of9YU1P/dGxs/4+Fhf+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/0hERP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/yopKf9EQUH/YFpa/4Z9'
    'ff+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Wi4vwloyM/5aMjP+WjIz/amNj/xgXF/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FxYW/4Z9ff+WjIz/loyM/5aMjP+WjIz/lYyM6JWLi8iWjIz/loyM/5aMjP+WjIz/kIeH/3Vu'
    'bv9aVVX/Pjs7/yIhIf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/gHh4/5aMjP+WjIz/'
    'loyM/5aMjP+Wi4vAlYuLmJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/4uCgv9uZ2f/Uk1N/zYzM/8bGhr/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xYWFv+Uior/loyM/5aMjP+WjIz/loyM/5WLi5iVjY06lYuL/ZaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5WLi/+BeXn/ZV9f/0lFRf8uKyv/FhYW/xQUFP8UFBT/FBQU/xQUFP8UFBT/Liws/5aMjP+WjIz/loyM/5aMjP+Vi4v9mI2NLwAA'
    'AACWi4uIloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+SiIj/eHBw/1xXV/8/PDz/'
    'JSQk/xcWFv9kXl7/loyM/5aMjP+WjIz/loyM/5aLi4gAAAAAAAAAAJiNjS+Wi4vYloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIzQmI2NLwAAAAAAAAAAAAAAAJ+AgAiVjIyJ'
    'loyM/JaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz8louLiJmZmQUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACWjY04loyMmZaLi8KVi4vsloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi+yWi4vClYuLmJaNjTgAAAAAAAAAAAAAAAAAAAAA8AAAD8AAAAOAAAABgAAAAQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAGAAAAB'
    'wAAAA/AAAA8oAAAAMAAAAGAAAAABACAAAAAAAIAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJSMjD6XjY1/loyMtpaL'
    'i9iVjIzzloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/lYyM85WMjNmWjIy2lYuLgJSMjD4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACSiYkcloyMs5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIyzkomJHAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJaN'
    'jU6VjIzxloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM8ZaLi0sAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAlYuLgJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Vi4uAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAACVjY1gloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/lYqKXgAAAAAAAAAAAAAAAJaJiSeVjIz4loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/lYyM+JeOjhsAAAAAAAAAAJaMjLiWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5WLi7AAAAAAmYyMKJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Xj48glouLcJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIxolYuLsJaMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+If3//Pzw8/yEgIP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/yEgIP9BPj7/iH9//5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Vi4uwlYyM4JaMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/46Fhf8iISH/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/JSMj/5CGhv+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Wi4vY'
    'lYyM+JaMjP+WjIz/loyM/5aMjP+WjIz/loyM/2hhYf8UFBT/FBQU/xQUFP8UFBT/FBQU/0E+Pv9oYmL/JiQk/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8nJSX/aGFh/0E+Pv8UFBT/FBQU/xQUFP8UFBT/FBQU/2pjY/+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+Wi4vwloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1lTU/8UFBT/FBQU/xQUFP8UFBT/OTc3/5aMjP+WjIz/jYOD/yYlJf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/yYlJf+OhYX/loyM/5aMjP81MzP/FBQU/xQUFP8UFBT/FBQU/1pVVf+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/YFpa/5aMjP+WjIz/loyM/4R7'
    'e/8eHR3/FBQU/xQUFP8UFBT/FBQU/xoaGv92bm7/dm5u/xoaGv8UFBT/FBQU/xQUFP8UFBT/Hh4e/4V8fP+WjIz/loyM/5aMjP9eWFj/FBQU/xQUFP8UFBT/'
    'FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/JyUl/5GH'
    'h/+WjIz/loyM/5aMjP96cnL/GBgY/xQUFP8UFBT/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/FBQU/xQUFP8YGBj/e3Nz/5aMjP+WjIz/loyM/5CGhv8mJCT/'
    'FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/zk3N/+Uior/loyM/5aMjP+WjIz/b2ho/xQUFP8UFBT/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/FBQU/xQUFP9vaGj/loyM/5aMjP+WjIz/'
    'lIqK/zYzM/8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQ'
    'UP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9IRET/loyM/5aMjP+WjIz/loyM/2JcXP8UFBT/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/FBQU/2JcXP+WjIz/'
    'loyM/5aMjP+WjIz/REBA/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/WVNT/5aMjP+WjIz/loyM/5aMjP9HQ0P/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/'
    'R0ND/5aMjP+WjIz/loyM/5aMjP9XUlL/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/2liYv+WjIz/loyM/5aMjP+Cenr/FBQU/0E9Pf+WjIz/'
    'loyM/0E+Pv8UFBT/hn19/5aMjP+WjIz/loyM/25nZ/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/KCYm/46Fhf+WjIz/loyM/5aMjP9qY2P/'
    'FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/a2Vl/5aMjP+WjIz/loyM/42Dg/8mJSX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8gHx//iH9//5aMjP+WjIz/'
    'loyM/4mAgP8hICD/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/ISAg/4mAgP+WjIz/loyM/5aMjP+FfX3/Hh0d/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQ'
    'UP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/FBQU/xgYGP9+dnb/'
    'loyM/5aMjP+WjIz/j4WF/yopKf8UFBT/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/FBQU/yopKf+PhYX/loyM/5aMjP+WjIz/e3Nz/xgYGP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/29oaP+WjIz/loyM/5aMjP+Uior/NzQ0/xQUFP8UFBT/FBQU/0E9Pf+WjIz/loyM/0E+Pv8UFBT/FBQU/xQUFP85Njb/lIqK/5aMjP+WjIz/loyM/29o'
    'aP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/'
    'FBQU/xQUFP8UFBT/RkJC/5aMjP+WjIz/loyM/5aMjP9EQED/FBQU/xQUFP8UFBT/FBQU/zAuLv+WjIz/loyM/ywqKv8UFBT/FBQU/xQUFP8UFBT/REBA/5aM'
    'jP+WjIz/loyM/5aMjP9FQUH/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/1VQUP8UFBT/FBQU/xQUFP8UFBT/W1ZW/5aMjP+WjIz/loyM/1ZRUf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9EQUH/REFB/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/1dSUv+WjIz/loyM/5aMjP9aVVX/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/1VQUP8UFBT/JCMj/ygmJv8UFBT/Gxsb/4B4eP+WjIz/XlhY/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9gWlr/loyM/4B4eP8cGxv/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8wLi7/loyM/5aMjP9BPT3/FBQU/xYWFv8nJib/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/JyYm/xUVFf8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP9EQUH/loyM/5aMjP9NSEj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/1VQUP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/1VQUP8YFxf/VVBQ/1xWVv8bGhr/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/1VQUP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+FfHz/dW5u/42Dg/+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/3Bpaf8UFBT/FBQU/xQU'
    'FP8mJCT/QT4+/1xXV/94cHD/koiI/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/z47'
    'O/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/y4sLP9JRET/ZF5e/352dv+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/x4dHf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8bGhr/NjMz/1JNTf9uZ2f/ioGB/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/h35+/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/yMhIf8/PDz/W1VV/3Zubv+Qh4f/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/amNj/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VFRX/LCoq/0dDQ/9iXFz/fnZ2/5SKiv+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/Ylxc/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xoZGf8zMTH/UEtL/21mZv+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM+JaMjP+WjIz/loyM/5aMjP+WjIz/f3Z2/x8eHv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8jIiL/iH9//5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+VjIz4louL8JaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5CHh/9qY2P/TUhI/zIwMP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/cGlp/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+Wi4vwlouLwJaMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+Uior/e3Nz/2FbW/9EQUH/KScn/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/dm5u/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIy4loyMaJaMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/4+Ghv90bGz/WFJS/zw5Of8hICD/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VFBT/kYeH/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIxol4uLFpaMjP6WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/h35+/2tkZP9PS0v/NDIy/xoa'
    'Gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8sKir/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP6ViooY'
    'AAAAAJWLi7CWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP9+dXX/Ylxc/0lERP8tKyv/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9JRUX/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5WLi7AAAAAAAAAAAJWNjUGVi4v7loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/kIeH/3Vubv9bVVX/Pjs7/yMhIf8UFBT/FBQU/xQUFP9/d3f/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM+paLi0QAAAAAAAAAAAAAAACVjIxbloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+KgYH/ZmBg/3tzc/+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lY2NYAAAAAAAAAAAAAAAAAAAAAD///8BlYuLgJaMjP6WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP6WjIx8////AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAJaLi0uVjIzxloyM/5aMjP+WjIz/loyM/5aM'
    'jP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/lYyM8ZaNjU4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAACbkJAXlYuLkZaL'
    'i/eWjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aLi/eVjIyQm5CQFwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAJmIiA+ViopelYuLsJaLi9iWi4vyloyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/'
    'loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/loyM/5aMjP+WjIz/louL8pWMjNeWjIyvl4yMXZmIiA8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAAAAB/'
    '///4AAAAAB////AAAAAAD///4AAAAAAH///AAAAAAAP//4AAAAAAAf//gAAAAAAB//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//'
    'AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAA'
    'AAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAA'
    'AAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//4AAAAAAAf//gAAAAAAB///AAAAAAAP//8AAAAAAA///8AAAAAAP'
    '///4AAAAAB////4AAAAAf///'),
    'tray-err.ico': (
    'AAABAAUAEBAAAAEAIABoBAAAVgAAABQUAAABACAAuAYAAL4EAAAYGAAAAQAgAIgJAAB2CwAAICAAAAEAIACoEAAA/hQAADAwAAABACAAqCUAAKYlAAAoAAAA'
    'EAAAACAAAAABACAAAAAAAEAEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFte8kxaX+/EWmDw71pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw71pf'
    '78RbXvJMAAAAAFlg71BaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lg71BaYPDHWmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPDHWWDv8Fpg8P9ITbv/Fxgh/x4fNP8UFBf/FBQU/xQUFP8UFBT/FBQU/xUW'
    'Gv8dHjL/GBgi/0lNvP9aYPD/WWDv8Fpg8P9aYPD/NzqD/xQUFP9ITbv/QUWk/xQUFf8lJ0v/JSdL/xQUFf9CRqf/R0y3/xQUFP83OoP/WmDw/1pg8P9aYPD/'
    'WmDw/zc6gv8UFBT/HyA4/1Zb5f86Po//MzV3/zM2d/87P5H/Vlzl/x4fNv8UFBT/NzqC/1pg8P9aYPD/WmDw/1pg8P83OoL/FBQU/xMTFP9FSbD/T1TQ/zM1'
    'd/8zNnf/UVfW/0hNuf8TExT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/NzqC/xQUFP83OoX/Vlvj/yAiPf8/Q53/P0Od/yEiPv9WXOT/NzqD/xQUFP83OoL/'
    'WmDw/1pg8P9aYPD/WmDw/zg7hf8vMm3/NzqC/yMlRv8UFBT/FhYb/xYWG/8UFBT/JCVI/zY5gf8UFBT/NzqC/1pg8P9aYPD/WmDw/1pg8P8/Qp7/NTd8/yUn'
    'S/8lJ0v/JSdL/yUnS/8lJ0v/JSdL/yUnS/8lJ0v/JSdL/z9Dnf9aYPD/WmDw/1pg8P9aYPD/NTh9/xcXH/8mJ07/NTh//0RIrP9UWd//WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/yUnS/8UFBT/FBQU/xQUFP8UFBT/FBQU/xscLv8rLV3/OT2L/0hMuf9XXej/WmDw/1pg8P9aYPD/WWDv8Fpg'
    '8P9BRaX/KStY/xobK/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FhYc/1Vb4v9aYPD/WWDv8Fpg8MdaYPD/WmDw/1pg8P9aYPD/U1jc/0NIq/80N3z/'
    'JSdM/xcXIP8UFBT/FBQU/xYWHP9aYPD/WmDw/1pg8MdZYO9QWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZX+//TFHH/z5CnP9ESa3/WmDw/1pg'
    '8P9ZYO9QAAAAAFte8kxaX+/EWmDw71pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw71pf78RbXvJMAAAAAIAB//8AAP//AAD//wAA//8AAP//'
    'AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//wAA//8AAP//AAD//4AB//8oAAAAFAAAACgAAAABACAAAAAAAJAGAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AABbYfMqWl/wnFpg7+BaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDv4Fpf8JxbYfMqAAAAAAAAAAAAAAAAWmDwiFpg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYO+AAAAAAFpe8ERaYPD9WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P1aXu9BWmDwuFpg8P9aYPD/V13q/0tPwf9ITbn/SE25/0hNuf9ITbn/'
    'SE25/0hNuf9ITbn/SE25/0hNuf9ITbn/S1DC/1he6/9aYPD/WmDw/1pg8LhZYPDoWmDw/1pg8P8sLmH/FBQU/xsbLP8aGyn/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/Gxsq/xsbLP8UFBT/LC5i/1pg8P9aYPD/WWDw6Fpg8P9aYPD/WmDw/x0eMf8UFBT/SEy5/1BV0/8aGif/FBQU/yUnS/8lJ0v/FBQU/xobKf9QVtT/'
    'R0y3/xQUFP8dHjH/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/HB0v/xQUFP8jJET/V13q/01SyP8XFx7/Oj6P/zo+j/8XFx7/TVLI/1dd6v8jJET/FBQU/xwd'
    'L/9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P8cHS//FBQU/xQUFP8pK1n/WV/v/zw/k/86Po//Oj6P/z1Blv9aYPD/LC5j/xQUFP8UFBT/HB0v/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/xwdL/8UFBT/FRUY/0dLtv9ZX+//KCpU/zo+j/86Po//KCpV/1lf7/9HS7X/FRUY/xQUFP8cHS//WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/HB0v/xQUFP88P5P/WmDw/y8ybP8UFBT/Rkqy/0ZKsv8UFBT/LzJs/1pg8P87PpD/FBQU/xwdL/9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P8gIT7/'
    'ICE8/0FFpf81OH7/FBQU/xQUFP8XFyD/Fxcg/xQUFP8UFBT/NTh//0FFov8UFBT/HB0v/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/y0wZ/9CRqj/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8cHS//WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/NzqC/zxAlP9LUMT/WV/u/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1Zb4/8UFBT/FBQU/xQUFP8WFx3/JCZJ/zM1d/9CRqb/UVbU/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/SU28/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Ghon/ykqVv84O4b/'
    'Rkqz/1Zc5P9aYPD/WmDw/1pg8P9aYPD/WWDw6Fpg8P9WW+P/NTh//yUmS/8WFx3/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/0FF'
    'ov9aYPD/WmDw/1lg8OhaYPC4WmDw/1pg8P9aYPD/WmDw/1lf7/9NUsr/PkKc/y8ybP8hIj7/FBQX/xQUFP8UFBT/FBQU/xQUFP8UFBT/SEy5/1pg8P9aYPD/'
    'WmDwuFpe70FaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9XXej/SE26/zk9jf8qLFr/HB0v/xscLf9WXOT/WmDw/1pg8P1aXvBEVVX/A1lf'
    '8IZZX/D+WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZX/D+Wl/vg1VV/wMAAAAAAAAAAFte8UlaYPDN'
    'WmDw71pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPDvWmDwzVte8UkAAAAAAAAAAMAAP/+AAB//AAAP/wAAD/8AAA//AAAP/wAA'
    'D/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD/8AAA//AAAP/wAAD//AAD//KAAAABgAAAAwAAAAAQAgAAAAAABgCQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAFpg8V1aYPCtWWDv5Vpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lg7+VaYPCtWmDxXQAA'
    'AAAAAAAAAAAAAAAAAABZYPIoWWDw+Fpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1lf7/NbW/YcAAAAAAAAAABaYPC4WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYO+wAAAAAFlg71BaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WWDvUFpg8LhaYPD/WmDw/1pg8P9ITLj/ODuH/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6'
    'gv83OoL/NzqC/zg7h/9ITbn/WmDw/1pg8P9aYPD/WmDwuFlg8OhaYPD/WmDw/1Zc5v8VFRn/FBQU/xoaJ/8eHzf/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8fIDj/GRom/xQUFP8VFRn/Vlzn/1pg8P9aYPD/WWDw6Fpg8P9aYPD/WmDw/0hNuv8UFBT/FBQU/0dLtf9ZX+7/JihP/xQUFP8UFBT/ISJA/yEj'
    'P/8UFBT/FBQU/ycpUf9ZX+7/RUqx/xQUFP8UFBT/SE26/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hNuf8UFBT/FBQU/ywuYP9aYPD/Vlzl/yAhPP8UFBT/'
    'Qkan/0FFpP8UFBT/ICE8/1Zb5f9aYPD/Ky1d/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hNuf8UFBT/FBQU/xQUFP81OH//WmDw/1NY'
    '3P8aGyn/Qkao/0JGp/8aGyn/U1jc/1pg8P80N3v/FBQU/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hNuf8UFBT/FBQU/xQUFP8WFx3/'
    'Uljb/1pg8P8tMGb/Qkao/0JGp/8vMWn/WmDw/1JY2v8WFhz/FBQU/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hNuf8UFBT/FBQU/xQU'
    'Ff9ESa//WmDw/0pPv/8VFRj/Qkao/0JGp/8VFRn/Sk+//1pg8P9ESa//FBQV/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hNuf8UFBT/'
    'FBQU/zo9jP9aYPD/T1TO/xgYIv8UFBT/REir/0FFpP8UFBT/GBkj/05Uz/9aYPD/ODyI/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0hN'
    'uf8YGSP/GBkj/zo+j/9PVNH/HB0w/xQUFP8UFBT/Fxgh/xcYIf8UFBT/FBQU/x0eMf9QVdL/Oj2M/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/0hNuf9BRaT/QUWk/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1BW1P8xNHT/NDh9/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/UFbU/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/0dLtf8UFBT/FBQU/xkaJv8oKlf/OTyJ/0dMt/9WW+X/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zU4ff8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBX/HyA5/y4wZ/89QZf/S1DF/1he7f9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/ykrWP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xYWG/8jJUb/MzZ3/0JG'
    'qP9XXer/WmDw/1pg8P9aYPD/WmDw/1lg8OhaYPD/WmDw/0RJrf8eHzf/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8xNHL/WmDw/1pg8P9aYPD/WWDw6Fpg8LhaYPD/WmDw/1pg8P9aYPD/Vlvj/0ZKsv83OoP/KCpV/xkaJv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8xM3H/WmDw/1pg8P9aYPD/WmDwuFlf72FaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9QVdP/QUWk/zM1df8kJUf/'
    'FhYc/xQUFP8UFBT/FBQU/xQUFP8/Q53/WmDw/1pg8P9aYPD/WV/vYQAAAABaX+/EWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WF7t/0tQxP89QZb/LjBm/yosWv9WXOT/WmDw/1pg8P9ZX+/DAAAAAAAAAABaY/cfWl/wuVpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pf8LlaY/cfAAAAAAAAAAAAAAAAAAAAAFpg8V1aYPCtWWDv5Vpg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lg7+VaYPCtWmDxXQAAAAAAAAAAAAAAAOAAB/+AAAH/gAAB/wAAAP8AAAD/AAAA/wAAAP8AAAD/'
    'AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/wAAAP8AAAD/AAAA/4AAAf+AAAH/4AAH/ygAAAAgAAAAQAAAAAEAIAAAAAAAgBAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAW2DxOFlg8JhZYO/CWl/w7Fpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aX/DsWWDvwlpf8JlbYPE4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAYGD/CFpg8IhZX/D8WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lf8PxZX/CJYGD/CAAAAAAAAAAA'
    'AAAAAFVV/wNZX/DOWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPDPVVX/AwAAAAAAAAAAWWDweFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZYO9wAAAAAFpg8SVZX/D5WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1lf7/NcXPEkWmDwiFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8IhaYPC4WmDw/1pg8P9aYPD/WmDw/0ZKs/8kJUf/'
    'FRUY/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8VFRn/JCVI/0dLtf9aYPD/WmDw/1pg'
    '8P9aYPD/WmDwuFlg8OhaYPD/WmDw/1pg8P9aYPD/ICE8/xQUFP8UFBT/FRYa/ywuY/8XFx7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xcXIP8sLmP/FRYa/xQUFP8UFBT/ISI+/1pg8P9aYPD/WmDw/1pg8P9ZYPDoWmDw/1pg8P9aYPD/WmDw/1pg8P8UFBf/FBQU/xQUFP8/Q53/WmDw/0xR'
    'xv8XFx7/FBQU/xQUFP8UFBT/ICE8/yAhPP8UFBT/FBQU/xQUFP8XFx//TVLI/1pg8P8+QZr/FBQU/xQUFP8UFBf/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/xQUFP8UFBT/FBQU/zc6hf9aYPD/WmDw/0VJrv8UFBX/FBQU/xQUFP9PVND/T1TQ/xQUFP8UFBT/FBQV/0VJsP9aYPD/WmDw/zY5'
    'gv8UFBT/FBQU/xQUFP9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/FBQU/xQUFP8UFBT/FBQX/0hNuf9aYPD/WmDw/zo+j/8UFBT/'
    'FBQU/1NY2v9SWNn/FBQU/xQUFP88P5L/WmDw/1pg8P9ITbn/FBQX/xQUFP8UFBT/FBQU/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P8UFBT/FBQU/xQUFP8UFBT/Fhcd/0tQwv9aYPD/WmDw/y8ya/8UFBT/U1ja/1JY2f8UFBT/LzJr/1pg8P9aYPD/S0/B/xYXHf8UFBT/FBQU/xQUFP8UFBT/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/xQUFP8UFBT/FBQU/xQUFP8UFBT/GRkk/09Uzv9aYPD/UFXT/xQUFP9TWNr/UljZ/xQU'
    'FP9SWNv/WmDw/09Uzv8XGCH/FBQU/xQUFP8UFBT/FBQU/xQUFP9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/FBQU/xQUFP8UFBT/'
    'FBQU/xcXHv9MUMT/WmDw/1pg8P8vMmv/FBQU/1NY2v9SWNn/FBQU/y8ybf9aYPD/WmDw/0xQxP8XFx7/FBQU/xQUFP8UFBT/FBQU/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P8UFBT/FBQU/xQUFP8UFBf/SE25/1pg8P9aYPD/Oj2O/xQUFP8UFBT/U1ja/1JY2f8UFBT/FBQU/zs/kf9aYPD/'
    'WmDw/0hNuf8UFBf/FBQU/xQUFP8UFBT/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/xQUFP8UFBT/FBQU/zc6hf9aYPD/WmDw/0RJ'
    'rf8UFBX/FBQU/xQUFP9PVM7/T1TO/xQUFP8UFBT/FBQV/0RJr/9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/FBQU/xQUFP8UFBT/P0Oe/1pg8P9MUcf/Fxce/xQUFP8UFBT/FBQU/xoaJ/8aGif/FBQU/xQUFP8UFBT/Fxce/0xRx/9aYPD/PkKb/xQU'
    'FP8UFBT/FBQU/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P8dHjL/U1nd/zM2dv8VFhr/LjBm/xcXHv8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Fhcf/y4wZv8VFhr/FBQU/xQUFP8UFBT/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/x0e'
    'Mv9TWd3/MzZ2/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/NDZ5/y4waP81N33/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6'
    'gv83OoL/NzqC/zc6gv83OoL/NzqC/zc6gv83OoL/NzqC/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1dd6f8aGif/FBQU/xMTFP8hIj7/'
    'MDJu/z1Bmf9MUcb/WV/u/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/SEy4/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XFx//JSdL/zM2ef9DR6n/UlfX/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P84PIj/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/Ghsq/ykrWP84PIj/R0y3/1Zb5f9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/zAybP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/yAhO/8uMGb/PUCV/1FW'
    '1f9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZYO/wWmDw/1pg8P9aYPD/Qkam/xYWG/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FRUZ/1FW1f9aYPD/WmDw/1pg8P9aYPD/WWDw6Flg8MhaYPD/WmDw/1pg8P9aYPD/Vlzn/0hM'
    'uf85PYv/Kixc/xscLf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/TlPL/1pg8P9aYPD/'
    'WmDw/1pg8P9ZYO/AWWDwmFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1RZ3/9ESKz/NTh+/yYnTv8XGCH/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xUVGP9ZX+7/WmDw/1pg8P9aYPD/WmDw/1lg8JhYYfI6WmDw/Vpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1lf7/9OVM3/P0Oe/zAzb/8hIkD/FRUY/xQUFP8UFBT/FBQU/xQUFP8UFBT/ISNB/1pg8P9aYPD/WmDw/1pg8P9aYPD9V2L0LwAA'
    'AABaYPCIWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9XXer/SU6+/zo+j/8rLV3/'
    'HR4x/xUVGf8/Q53/WmDw/1pg8P9aYPD/WmDw/1pg8IgAAAAAAAAAAFdi9C9aYPDYWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZYO/QV2L0LwAAAAAAAAAAAAAAAGBg/whZX/CJ'
    'WV/w/Fpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9ZX/D8WmDwiGZm/wUAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbYPE4Wl/wmVlg78JaX/DsWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pf8OxZYO/CWWDwmFtg8TgAAAAAAAAAAAAAAAAAAAAA8AAAD8AAAAOAAAABgAAAAQAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAAAGAAAAB'
    'wAAAA/AAAA8oAAAAMAAAAGAAAAABACAAAAAAAIAlAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFpf8z5aYPF/Wl/wtlpg'
    '8NhZX+/zWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WV/v81lf8NlaX/C2WmDvgFpf8z4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABbW/YcWl/vs1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aX++zW1v2HAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFhf'
    '8k5aX+/xWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/Wl/v8Vhf8UsAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAWmDvgFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYO+AAAAAAAAA'
    'AAAAAAAAAAAAAAAAAABaYO9gWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/Wl/xXgAAAAAAAAAAAAAAAFxi8idZYPD4WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WWDw+FVe9hsAAAAAAAAAAFpg8LhaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg77AAAAAAWWDyKFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9YYO8gWWDvcFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9YYPBoWmDvsFpg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9SWNn/Ky1e/xobK/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xsbLP8sLmH/Ulja/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYO+wWmDv4Fpg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1Zb4/8bHCz/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/HR4x/1Zc5v9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPDY'
    'WWDw+Fpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0FFo/8UFBT/FBQU/xQUFP8UFBT/FBQU/ywuYv9BRaP/HR4y/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8eHzT/QUWj/ywuYf8UFBT/FBQU/xQUFP8UFBT/FBQU/0JGpv9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9ZYO/wWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zk8if8UFBT/FBQU/xQUFP8UFBT/KCpU/1pg8P9aYPD/VVrh/x4eM/8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/x4eM/9WW+P/WmDw/1pg8P8lJ03/FBQU/xQUFP8UFBT/FBQU/zk9i/9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/PUCV/1pg8P9aYPD/WmDw/1BV'
    '0/8ZGSX/FBQU/xQUFP8UFBT/FBQU/xcXH/9ITbn/SE25/xcXH/8UFBT/FBQU/xQUFP8UFBT/GRom/1BW1P9aYPD/WmDw/1pg8P87P5H/FBQU/xQUFP8UFBT/'
    'FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/Hh80/1dd'
    '6P9aYPD/WmDw/1pg8P9LT8H/FhYc/xQUFP8UFBT/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/FBQU/xQUFP8WFhz/S1DD/1pg8P9aYPD/WmDw/1Zc5v8dHjL/'
    'FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/ygqVP9ZX+7/WmDw/1pg8P9aYPD/REmv/xQUFf8UFBT/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/FBQU/xQUFf9ESa//WmDw/1pg8P9aYPD/'
    'WF7t/yYnTv8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6'
    'gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8wMmz/WmDw/1pg8P9aYPD/WmDw/z5BmP8UFBT/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/FBQU/z5BmP9aYPD/'
    'WmDw/1pg8P9aYPD/LTBm/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/OTyJ/1pg8P9aYPD/WmDw/1pg8P8vMWr/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/'
    'LzJr/1pg8P9aYPD/WmDw/1pg8P84O4b/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/ExMU/0FFpP9aYPD/WmDw/1pg8P9PVND/FBQU/ysuYf9aYPD/'
    'WmDw/ywuYf8UFBT/UVbV/1pg8P9aYPD/WmDw/0RJrf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Hh82/1Zb4/9aYPD/WmDw/1pg8P9CRqb/'
    'FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/Q0ep/1pg8P9aYPD/WmDw/1Va4f8eHjP/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8aGyj/UlfY/1pg8P9aYPD/'
    'WmDw/1JY2/8aGyv/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/Ghsr/1JY2/9aYPD/WmDw/1pg8P9RVtT/GRkl/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6'
    'gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/FBQU/xYWHP9NUsj/'
    'WmDw/1pg8P9aYPD/Vlvl/yAhO/8UFBT/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/FBQU/yAhO/9WW+X/WmDw/1pg8P9aYPD/S1DE/xYWHP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/'
    'FBQV/0VJrv9aYPD/WmDw/1pg8P9ZX+7/JihQ/xQUFP8UFBT/FBQU/ysuYf9aYPD/WmDw/ywuYf8UFBT/FBQU/xQUFP8oKVP/WV/u/1pg8P9aYPD/WmDw/0VJ'
    'rv8UFBX/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/'
    'FBQU/xQUFP8UFBT/LzFp/1pg8P9aYPD/WmDw/1pg8P8tMGb/FBQU/xQUFP8UFBT/FBQU/yMkRP9aYPD/WmDw/yEiPf8UFBT/FBQU/xQUFP8UFBT/LTBm/1pg'
    '8P9aYPD/WmDw/1pg8P8uMGf/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/zc6gv8UFBT/FBQU/xQUFP8UFBT/Oj2O/1pg8P9aYPD/WmDw/zc6hf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFf8uMGb/LjBm/xQUFf8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/zg7hv9aYPD/WmDw/1pg8P85PYv/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/zc6gv8UFBT/HB0w/x4fNv8UFBT/Fxgh/05TzP9aYPD/Oz+R/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP89QJX/WmDw/05Ty/8YGCL/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8jJET/WmDw/1pg8P8sLmD/FBQU/xUVGP8eHzX/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/Hh81/xQUF/8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8uMGb/WmDw/1pg8P8yNXT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/zc6gv9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/zc6gv8WFhv/NjmC/zo+j/8XGCH/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/zc6gv9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9QVtT/SEy5/1Va4f9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/0VKsf8UFBT/FBQU/xQU'
    'FP8dHjL/LC5h/zo+j/9JTr7/V13q/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/yos'
    'XP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/yEjQf8wMm7/P0Od/01SyP9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/xkZJf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8XGCH/JidO/zU4ff9ESKz/U1nd/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/UVfW/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xscLv8rLV3/Oj2M/0hNuv9WXOf/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/Qkam/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBb/ICI9/y8ya/8+QZr/TVLI/1lf7v9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/PUGZ/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xcXHv8lJkr/NDd6/0NIq/9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WWDw+Fpg8P9aYPD/WmDw/1pg8P9aYPD/TVLJ/xoaJ/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8cHS//UljZ/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZYPD4WWDv8Fpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1Zc5/9CRqb/MjV0/yQlSP8UFBT/'
    'FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/RUmw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9ZYO/wWWDvwFpg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9YXu3/S1DE/z1Blv8uMGb/HyA4/xQUFf8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/SE25/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPC4WGDwaFpg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1Zc5f9HTLf/ODuH/ykrWP8bGyr/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQU'
    'FP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBX/V13o/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9YYPBoXV3zFllf8P5aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/UlfX/0JGqP8zNnn/JSdL/xcX'
    'H/8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8gIj3/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lf8P5VYPQY'
    'AAAAAFpg77BaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9MUcf/PUGZ/zAybv8hIj7/FBQU/xQUFP8UFBT/FBQU/xQUFP8UFBT/FBQU/xQUFP8wM2//WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg77AAAAAAAAAAAFpe70FZX/D7WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/Vlzn/0hMuf86PYz/Kixc/xscLv8UFBT/FBQU/xQUFP9NUsr/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw+lpe8EQAAAAAAAAAAAAAAABaX/FbWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9TWd3/QESf/0tQxP9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDvYAAAAAAAAAAAAAAAAAAAAAAAAP8BWmDvgFlf8P5aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1lf8P5YX/F8AAD/AQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAFhf8UtaX+/xWmDw/1pg8P9aYPD/WmDw/1pg'
    '8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/Wl/v8Vhf8k4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABZZPQXWl/vkVpg'
    '8PdaYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8PdZYO+QWWT0FwAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAFVm/w9aX/FeWmDvsFpg8NhaYO/yWmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/'
    'WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDw/1pg8P9aYPD/WmDv8llg8NdZYPCvWmDxXVVm/w8AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/gAAAAB/'
    '///4AAAAAB////AAAAAAD///4AAAAAAH///AAAAAAAP//4AAAAAAAf//gAAAAAAB//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//'
    'AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAA'
    'AAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAA'
    'AAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//wAAAAAAAP//AAAAAAAA//8AAAAAAAD//4AAAAAAAf//gAAAAAAB///AAAAAAAP//8AAAAAAA///8AAAAAAP'
    '///4AAAAAB////4AAAAAf///'),
}
ICON_V = hashlib.sha256(ICON_PNG + ICONS['mediascan.ico'].encode()).hexdigest()[:8]      # cache-buster for the browser

PAGE = '''<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MediaScan</title><link rel="icon" href="/logo.svg?v=__ICON_V__" type="image/svg+xml"><link rel="icon" href="/logo-256.png?v=__ICON_V__" type="image/png" sizes="256x256"><link rel="stylesheet" href="/app.css"><script src="/app.js" defer></script></head>
<body><div id="app"><header><div class="brand"><img src="/logo.svg?v=__ICON_V__" alt=""><div><b>MediaScan</b><span id="host" class="muted"></span></div></div>
<div class="hdr-r"><span id="pill" class="pill">…</span><a id="dash" class="btn" target="_blank" rel="noopener" hidden></a><button id="b-settings" class="ghost"></button></div></header>
<div id="banner" class="banner" hidden></div>
<main>
<section class="card" id="c-settings" hidden><form id="settings" autocomplete="off">
<div class="row"><h2 data-t="s_title"></h2><button id="b-close" type="button" class="ghost" data-t="close"></button></div>
<p class="muted" data-t="s_intro"></p>
<div class="sgrid">
<fieldset><legend data-t="s_dash"></legend>
<label><span data-t="s_url"></span><input id="f-url" placeholder="http://192.168.1.10:8780" spellcheck="false"></label>
<label><span data-t="s_key"></span><div class="inl"><input id="f-token" type="password" spellcheck="false"><button id="b-show" type="button" class="ghost sm" data-t="show"></button></div></label>
<div class="inl"><button id="b-test" type="button" class="sm" data-t="test"></button><span id="t-res" class="small"></span></div>
</fieldset>
<fieldset><legend data-t="s_this"></legend>
<label><span data-t="s_name"></span><input id="f-name" maxlength="64"></label>
<label><span data-t="s_media"></span><div class="inl"><input id="f-media" placeholder="M:\\ or \\\\NAS\\Media" spellcheck="false"><button id="b-find" type="button" class="sm" data-t="find"></button><button id="b-map" type="button" class="sm" data-t="map" hidden></button></div><span id="m-res" class="small"></span></label>
<label><span data-t="s_maxh"></span><select id="f-maxh"><option value="0">4K</option><option value="1440">1440p</option><option value="1080">1080p</option><option value="720">720p</option></select></label>
</fieldset>
<fieldset><legend data-t="s_subs"></legend>
<label class="chk"><input id="f-subs" type="checkbox"><span data-t="s_subs_on"></span></label>
<p class="muted small" data-t="s_subs_hint"></p>
<div id="subs-more">
<label><span data-t="s_os_key"></span><input id="f-oskey" spellcheck="false"></label>
<label><span data-t="s_os_user"></span><input id="f-osuser" spellcheck="false"></label>
<label><span data-t="s_os_pw"></span><input id="f-ospw" type="password" autocomplete="new-password"></label>
<p class="muted small" data-t="s_os_hint"></p>
<label><span data-t="s_whisper"></span><select id="f-whisper"></select></label>
<label><span data-t="s_ourl"></span><input id="f-ourl" spellcheck="false"></label>
<label><span data-t="s_omodel"></span><input id="f-omodel" list="omodels" spellcheck="false"><datalist id="omodels"></datalist><span id="o-res" class="muted small"></span></label>
</div>
</fieldset>
</div>
<div class="btns end"><button id="b-save" type="submit" class="primary" data-t="save"></button></div>
</form></section>
<section class="card" id="c-worker"><div class="row"><div><h2 id="w-title">…</h2><p id="w-sub" class="muted"></p></div>
<div class="btns"><button id="b-start" class="primary"></button><button id="b-restart"></button><button id="b-stop" class="danger"></button></div></div>
<dl id="w-facts" class="facts"></dl></section>
<section class="card" id="c-jobs"><h3 id="t-now"></h3><div id="jobs"></div></section>
<section class="card"><div class="row"><h3 id="t-log"></h3><div class="tabs" id="logtabs"><button data-w="worker" class="on"></button><button data-w="manager"></button></div></div>
<pre id="log" tabindex="0"></pre><div class="row foot"><div class="btns"><button id="b-folder" class="ghost"></button><button id="b-openlog" class="ghost"></button></div>
<button id="b-quit" class="danger ghost"></button></div></section>
</main><footer id="ver" class="muted"></footer></div>
<div id="modal" hidden><div class="dlg"><h3 id="m-title"></h3><p id="m-text"></p><div class="btns end"><button id="m-no"></button><button id="m-yes" class="primary"></button></div></div></div>
<div id="toast" hidden></div></body></html>'''

CSS = r''':root{--bg:#0b0b0d;--panel:#131316;--panel2:#1a1a1e;--hover:#1f1f24;--line:#24242a;--line2:#303038;--text:#ececef;--muted:#9a9aa4;--faint:#6a6a74;
--accent:#ff8a1f;--accent-h:#ffa04a;--accent-s:rgba(255,138,31,.12);--accent-line:rgba(255,138,31,.45);--on-accent:#111;--ok:#3fb950;--ok-s:rgba(63,185,80,.12);
--bad:#f0605a;--bad-s:rgba(240,96,90,.12);--r:12px;--r-sm:8px;--mono:ui-monospace,"Cascadia Mono",Consolas,monospace;color-scheme:dark}
@media (prefers-color-scheme:light){:root{--bg:#e8e8ec;--panel:#f4f4f6;--panel2:#ececf0;--hover:#e3e3e8;--line:#dadade;--line2:#cbcbd2;--text:#1d1d1f;--muted:#5d5d65;--faint:#8b8b93;
--accent:#e2700b;--accent-h:#ef7f1a;--accent-s:rgba(226,112,11,.11);--accent-line:rgba(226,112,11,.42);--on-accent:#fff;--ok:#2a8a43;--ok-s:rgba(42,138,67,.11);--bad:#cc3a35;--bad-s:rgba(204,58,53,.10);color-scheme:light}}
[hidden]{display:none!important}
*{box-sizing:border-box}html,body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI Variable Text","Segoe UI",Inter,system-ui,sans-serif}
#app{max-width:900px;margin:0 auto;padding:18px 16px 24px}
header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:14px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:10px}.brand img{width:30px;height:30px}.brand b{font-size:16px;display:block;line-height:1.2}.brand span{font-size:12.5px}
.hdr-r{display:flex;gap:8px;align-items:center}
.muted{color:var(--muted)}
button,.btn{font:inherit;color:inherit;cursor:pointer;border:1px solid var(--line2);background:transparent;border-radius:var(--r-sm);padding:7px 14px;transition:border-color .15s,background .15s,opacity .15s;text-decoration:none;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
button:hover,.btn:hover{border-color:var(--accent-line)}button:disabled{opacity:.45;cursor:default}
button.primary{background:var(--accent);border-color:var(--accent);color:var(--on-accent);font-weight:600}button.primary:hover:not(:disabled){background:var(--accent-h)}
button.danger{color:var(--bad)}button.danger:hover:not(:disabled){border-color:var(--bad)}
button.ghost{border-color:transparent;color:var(--muted)}button.ghost:hover:not(:disabled){color:var(--text);background:var(--hover)}
button.ghost.danger{color:var(--bad)}
button.busy{position:relative;color:transparent!important}button.busy::after{content:"";position:absolute;inset:0;margin:auto;width:14px;height:14px;border-radius:50%;border:2px solid var(--muted);border-top-color:transparent;animation:spin .7s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--r);padding:18px;margin-bottom:12px}
h2{font-size:18px;margin:0 0 2px;display:flex;align-items:center;gap:10px}h3{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);margin:0 0 10px}
.row{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}.row h3{margin:6px 0 10px}
.btns{display:flex;gap:8px;flex-wrap:wrap}.btns.end{justify-content:flex-end}
.dot{width:10px;height:10px;border-radius:50%;background:var(--faint);flex:none}.dot.ok{background:var(--ok);box-shadow:0 0 0 4px var(--ok-s)}.dot.run{background:var(--accent);box-shadow:0 0 0 4px var(--accent-s)}.dot.bad{background:var(--bad);box-shadow:0 0 0 4px var(--bad-s)}
.pill{font-size:12.5px;padding:3px 10px;border-radius:999px;border:1px solid var(--line2);color:var(--muted)}.pill.ok{color:var(--ok);border-color:var(--ok)}.pill.run{color:var(--accent);border-color:var(--accent-line)}.pill.bad{color:var(--bad);border-color:var(--bad)}
.facts{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0 0}.facts div{background:var(--panel2);border-radius:var(--r-sm);padding:9px 12px;min-width:0}
.facts dt{font-size:11.5px;color:var(--faint);text-transform:uppercase;letter-spacing:.05em}.facts dd{margin:0;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.job{padding:12px 0;border-top:1px solid var(--line)}.job:first-child{border-top:0;padding-top:0}
.job .k{font-size:11.5px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:.05em}.job .fn{font-weight:600;overflow-wrap:anywhere}.job .st{color:var(--muted);font-size:13px}
.bar{height:6px;background:var(--panel2);border-radius:99px;overflow:hidden;margin-top:8px}.bar i{display:block;height:100%;background:var(--accent);border-radius:99px;transition:width .4s}
.empty{color:var(--muted);padding:4px 0}
.tabs{display:inline-flex;background:var(--bg);border:1px solid var(--line2);border-radius:var(--r-sm);padding:2px}.tabs button{border:0;padding:4px 12px;font-size:13px;color:var(--muted)}.tabs button.on{background:var(--panel2);color:var(--text)}
pre{background:var(--bg);border:1px solid var(--line);border-radius:var(--r-sm);padding:10px 12px;margin:0;height:300px;overflow:auto;font:12px/1.55 var(--mono);white-space:pre-wrap;overflow-wrap:anywhere;color:var(--muted)}
pre .e{color:var(--bad)}pre .s{color:var(--text)}
.foot{margin-top:10px;align-items:center}
.banner{border:1px solid var(--bad);background:var(--bad-s);color:var(--bad);padding:10px 14px;border-radius:var(--r-sm);margin-bottom:12px}
footer{font-size:12px;text-align:center;margin-top:8px}
.sgrid{display:grid;gap:14px;margin-top:10px}fieldset{border:1px solid var(--line);border-radius:var(--r-sm);padding:12px 14px 4px;margin:0;min-width:0}
legend{font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--faint);padding:0 6px}
label{display:grid;gap:4px;margin-bottom:10px;font-size:13px;color:var(--muted)}label.chk{grid-template-columns:auto 1fr;align-items:center;gap:8px;color:var(--text)}
input,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line2);border-radius:var(--r-sm);padding:7px 10px;width:100%;min-width:0}
input:focus,select:focus{outline:none;border-color:var(--accent-line)}input[type=checkbox]{width:16px;height:16px;padding:0;accent-color:var(--accent)}
.inl{display:flex;gap:8px;align-items:center}.inl input{flex:1}.small{font-size:12.5px}.small.ok{color:var(--ok)}.small.bad{color:var(--bad)}
button.sm{padding:4px 10px;font-size:12.5px}
.banner.info{border-color:var(--accent-line);background:var(--accent-s);color:var(--text)}
#subs-more.off{opacity:.45;pointer-events:none}
.gone{margin-top:20vh;text-align:center}
#modal{position:fixed;inset:0;background:rgba(0,0,0,.55);display:grid;place-items:center;padding:16px;z-index:10}#modal[hidden]{display:none}
.dlg{background:var(--panel);border:1px solid var(--line2);border-radius:var(--r);padding:20px;max-width:440px;width:100%}.dlg h3{font-size:16px;text-transform:none;letter-spacing:0;color:var(--text)}.dlg p{color:var(--muted);margin:0 0 16px}
#toast{position:fixed;bottom:18px;left:50%;transform:translateX(-50%);background:var(--panel2);border:1px solid var(--line2);padding:9px 14px;border-radius:var(--r-sm);box-shadow:0 10px 30px rgba(0,0,0,.4);z-index:20;max-width:calc(100vw - 32px)}
@media (max-width:560px){.row>.btns{width:100%}.row>.btns button{flex:1}h2{font-size:16px}}
'''

JS = r''''use strict';
const NL = (navigator.language||'').toLowerCase().startsWith('nl');
const T = NL ? {
  start:'Starten', stop:'Stoppen', restart:'Herstarten', now:'Nu bezig', log:'Logboek', wlog:'Worker', mlog:'Beheer', folder:'Map openen', openlog:'In Kladblok openen',
  quit:'Afsluiten (alles stoppen)', dash:'Dashboard openen', running:'Actief', stopped:'Gestopt', starting:'Start op…', crashed:'Gestopt na fouten', manager:'Beheer',
  w_busy:'Worker is bezig', w_idle:'Worker wacht op werk', w_off:'Worker staat uit', w_start:'Worker start op…', w_crash:'Worker crasht steeds', w_drain:'Worker werkt zichzelf bij na de lopende job', w_media:'Worker wacht op de media-share', w_conn:'Worker maakt verbinding met de server…',
  s_media:'De mediamap is niet bereikbaar. Controleer of de share gekoppeld is (media_root in worker.ini).',
  s_off:'Start de worker om weer werk op te pakken.', s_idle:'Nieuwe jobs komen vanzelf binnen vanuit het dashboard.', s_crash:'Na 6 fouten binnen een uur is automatisch herstarten gepauzeerd. Bekijk het logboek en klik op Starten.',
  s_server_bad:'Kan de server niet bereiken: ', ver:'Versie', up:'Actief sinds', restarts:'Crashes', lastr:'Laatste herstart', why:{crash:'crash', update:'update', manual:'handmatig'}, server:'Server', noj:'Geen job op dit moment.',
  encode:'Encoden', cpu:'CPU-encoden', subs:'Ondertitels', since:'bezig sinds ', q_title:'MediaScan afsluiten?', q_text:'De worker en het beheer stoppen. Een lopende job gaat terug in de wachtrij. Bij de volgende keer inloggen start alles weer.',
  q_stop:'Worker stoppen?', q_stop_t:'Een lopende job gaat terug in de wachtrij. De worker blijft uit, ook na een herstart van de pc, tot je hem weer start.', yes_quit:'Afsluiten', yes_stop:'Stoppen', cancel:'Annuleren',
  locked:'Open MediaScan via het MediaScan-icoon in het systeemvak of via Start > MediaScan.', lost:'Het beheer reageert niet. Herstart via het M-icoon of het Startmenu.', gone:'MediaScan is afgesloten. Je kunt dit venster sluiten.', done:'Klaar', failed:'Mislukt', enc_off:'encoden uit', subs_off:'ondertitels uit',
  settings:'Instellingen', close:'Sluiten', s_title:'Instellingen', s_intro:'Dit staat in worker.ini. Na opslaan start de worker opnieuw, zodat de nieuwe instellingen gelden.',
  s_dash:'Dashboard', s_url:'Adres van het dashboard', s_key:'Worker-key (dashboard > Workers > Add worker)', show:'Tonen', hide:'Verbergen', test:'Verbinding testen', t_ok:'Verbonden · dashboard levert worker ', t_bad:'Mislukt: ',
  s_this:'Deze worker', s_name:'Naam', s_media:'Media-share', find:'Zoeken', map:'Koppelen aan letter', m_found:'Gevonden: de bibliotheek staat op ', m_none:'Niet gevonden. Koppel de share eerst (bijv. net use M: \\\\NAS\\Media) of vul het pad in.', m_ok:'Bereikbaar', m_bad:'Niet bereikbaar op dit moment',
  map_letter:'Welke stationsletter?', map_ok:'Gekoppeld aan ', map_bad:'Koppelen mislukt: ', s_maxh:'Maximale resolutie om te encoden',
  s_subs:'Ondertitels', s_subs_on:'Ondertitels maken op deze pc', s_subs_hint:'Zet je dit later aan, draai dan Install-Worker.bat nog een keer: die installeert Whisper en Ollama.',
  s_os_key:'OpenSubtitles API-key (optioneel)', s_os_user:'OpenSubtitles gebruikersnaam', s_os_pw:'OpenSubtitles wachtwoord', unchanged:'(ongewijzigd)', s_os_hint:'Gratis account op opensubtitles.com; API-key onder Profile > API consumers. Leeg = alleen Whisper en vertalen.',
  s_whisper:'Whisper-model', s_ourl:'Ollama-adres', s_omodel:'Vertaalmodel (Ollama)', o_list:'Geïnstalleerd: ', o_none:'Ollama niet bereikbaar of nog geen model gedownload.',
  save:'Opslaan', saved:'Opgeslagen', q_apply:'Worker nu herstarten?', q_apply_t:'De nieuwe instellingen gelden pas na een herstart van de worker. Een lopende job gaat terug in de wachtrij.', yes_apply:'Herstarten', later:'Later',
  setup:'Maak de installatie af: vul het adres van het dashboard en de worker-key in en klik op Opslaan.'
} : {
  start:'Start', stop:'Stop', restart:'Restart', now:'Working on', log:'Log', wlog:'Worker', mlog:'Manager', folder:'Open folder', openlog:'Open in Notepad',
  quit:'Quit (stop everything)', dash:'Open dashboard', running:'Running', stopped:'Stopped', starting:'Starting…', crashed:'Stopped after errors', manager:'Manager',
  w_busy:'Worker is busy', w_idle:'Worker is waiting for work', w_off:'Worker is stopped', w_start:'Worker is starting…', w_crash:'Worker keeps crashing', w_drain:'Worker will update itself after the current job', w_media:'Worker is waiting for the media share', w_conn:'Worker is connecting to the server…',
  s_media:'The media folder cannot be reached. Check that the share is mounted (media_root in worker.ini).',
  s_off:'Start the worker to pick up work again.', s_idle:'New jobs arrive automatically from the dashboard.', s_crash:'After 6 errors within an hour, automatic restarts are paused. Check the log and click Start.',
  s_server_bad:'Cannot reach the server: ', ver:'Version', up:'Running since', restarts:'Crashes', lastr:'Last restart', why:{crash:'crash', update:'update', manual:'manual'}, server:'Server', noj:'No job right now.',
  encode:'Encoding', cpu:'CPU encoding', subs:'Subtitles', since:'running for ', q_title:'Quit MediaScan?', q_text:'This stops the worker and the manager. A running job goes back to the queue. Everything starts again at your next sign-in.',
  q_stop:'Stop the worker?', q_stop_t:'A running job goes back to the queue. The worker stays off, also after a reboot, until you start it again.', yes_quit:'Quit', yes_stop:'Stop', cancel:'Cancel',
  locked:'Open MediaScan with the MediaScan icon in the system tray, or Start > MediaScan.', lost:'The manager is not responding. Restart it via the M icon or the Start menu.', gone:'MediaScan has been closed. You can close this window.', done:'Done', failed:'Failed', enc_off:'encoding off', subs_off:'subtitles off',
  settings:'Settings', close:'Close', s_title:'Settings', s_intro:'These live in worker.ini. After saving, the worker restarts so the new settings take effect.',
  s_dash:'Dashboard', s_url:'Dashboard address', s_key:'Worker key (dashboard > Workers > Add worker)', show:'Show', hide:'Hide', test:'Test connection', t_ok:'Connected · the dashboard serves worker ', t_bad:'Failed: ',
  s_this:'This worker', s_name:'Name', s_media:'Media share', find:'Find', map:'Map to drive letter', m_found:'Found: the library is on ', m_none:'Not found. Map the share first (e.g. net use M: \\\\NAS\\Media) or enter the path.', m_ok:'Reachable', m_bad:'Not reachable right now',
  map_letter:'Which drive letter?', map_ok:'Mapped to ', map_bad:'Mapping failed: ', s_maxh:'Maximum resolution to encode',
  s_subs:'Subtitles', s_subs_on:'Make subtitles on this PC', s_subs_hint:'If you turn this on later, run Install-Worker.bat once more: it installs Whisper and Ollama.',
  s_os_key:'OpenSubtitles API key (optional)', s_os_user:'OpenSubtitles username', s_os_pw:'OpenSubtitles password', unchanged:'(unchanged)', s_os_hint:'Free account at opensubtitles.com; the API key is under Profile > API consumers. Empty = Whisper and translation only.',
  s_whisper:'Whisper model', s_ourl:'Ollama address', s_omodel:'Translation model (Ollama)', o_list:'Installed: ', o_none:'Ollama not reachable, or no model downloaded yet.',
  save:'Save', saved:'Saved', q_apply:'Restart the worker now?', q_apply_t:'The new settings only apply after the worker restarts. A running job goes back to the queue.', yes_apply:'Restart', later:'Later',
  setup:'Finish the setup: enter the dashboard address and the worker key, then click Save.'
};
document.documentElement.lang = NL ? 'nl' : 'en';
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dur = s => { s = Math.max(0, Math.floor(s)); const d = Math.floor(s/86400), h = Math.floor(s%86400/3600), m = Math.floor(s%3600/60);
  return d ? `${d}d ${h}h` : h ? `${h}h ${m}m` : `${m}m`; };
const base = p => String(p||'').split(/[\\/]/).pop();
let ST = null, which = 'worker', busy = false, fails = 0, quitting = false, setupBanner = false;
// The key arrives in the URL fragment (never sent to a server), is kept for this window only and sent as a header.
let KEY = ''; try { const m = location.hash.match(/k=([\w-]+)/); if (m) { sessionStorage.setItem('k', m[1]); history.replaceState(null, '', '/'); } KEY = sessionStorage.getItem('k') || ''; } catch {}
const api = (url, opt) => fetch(url, Object.assign({cache:'no-store'}, opt, {headers: Object.assign({'X-Key': KEY}, (opt||{}).headers)}));

function toast(msg){ const t = $('#toast'); t.textContent = msg; t.hidden = false; clearTimeout(t._t); t._t = setTimeout(() => t.hidden = true, 3500); }
function ask(title, text, yes){ return new Promise(res => { $('#m-title').textContent = title; $('#m-text').textContent = text; $('#m-yes').textContent = yes; $('#m-no').textContent = T.cancel;
  const m = $('#modal'); m.hidden = false; $('#m-no').focus();
  const done = v => { m.hidden = true; $('#m-yes').onclick = $('#m-no').onclick = null; res(v); };
  $('#m-yes').onclick = () => done(true); $('#m-no').onclick = () => done(false); m.onclick = e => { if (e.target === m) done(false); }; }); }

async function act(action, btn, extra){
  if (busy) return; busy = true; if (btn) { btn.classList.add('busy'); btn.disabled = true; }
  try {
    const r = await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.assign({action}, extra||{}))});
    if (!r.ok) throw new Error(r.status);
    const d = await r.json();
    if (action === 'quit') { quitting = true; document.body.innerHTML = `<div id="app"><div class="card gone">${esc(T.gone)}</div></div>`; setTimeout(() => window.close(), 1500); return; }
    if (!['open_folder','open_log'].includes(action)) toast(T.done + ': ' + d.result);
  } catch (e) { toast(T.failed + ' (' + e.message + ')'); }
  finally { busy = false; if (btn) { btn.classList.remove('busy'); btn.disabled = false; } refresh(); }
}

function render(){
  const s = ST, j = s.jobs.worker || {}, w = s.worker, now = s.now;
  $('#host').textContent = T.manager + ' · ' + s.host;
  const jobs = w ? Object.entries(w.jobs||{}) : [];
  let cls = '', title, sub = '', pill;
  if (j.running && !w) { cls = 'run'; title = T.w_start; pill = T.starting; }
  else if (j.running && w.phase && w.phase !== 'ready') { cls = w.phase === 'waiting_media' ? 'bad' : 'run'; pill = T.running;
    title = w.phase === 'waiting_media' ? T.w_media : T.w_conn; sub = w.phase === 'waiting_media' ? T.s_media : (w.server || ''); }
  else if (j.running) { cls = jobs.length ? 'run' : 'ok'; title = w.draining ? T.w_drain : jobs.length ? T.w_busy : T.w_idle; pill = T.running;
    sub = w.server_ok === false ? T.s_server_bad + (w.server||'') : jobs.length ? '' : T.s_idle;
    const off = [w.encode ? '' : T.enc_off, w.subs ? '' : T.subs_off].filter(Boolean); if (off.length) sub += (sub?' · ':'') + off.join(' · '); }
  else if (j.gave_up) { cls = 'bad'; title = T.w_crash; sub = T.s_crash; pill = T.crashed; }
  else if (j.wanted === 'running') { cls = 'run'; title = T.w_start; pill = T.starting; }
  else { title = T.w_off; sub = T.s_off; pill = T.stopped; }
  if (w && w.server_ok === false) cls = 'bad';
  $('#w-title').innerHTML = `<span class="dot ${cls}"></span>${esc(title)}`; $('#w-sub').textContent = sub;
  const p = $('#pill'); p.textContent = pill; p.className = 'pill ' + cls;
  $('#b-start').hidden = j.running; $('#b-stop').hidden = !j.running && j.wanted !== 'running'; $('#b-restart').hidden = !j.running;
  const lr = j.last_restart;
  const facts = [[T.ver, w ? w.version : '–'], [T.up, j.running && j.started ? dur(now - j.started) : '–'], [T.restarts, j.restarts ?? 0],
    [T.lastr, lr ? `${T.why[lr.why] || lr.why} · ${dur(now - lr.at)}` : '–'], [T.server, w ? w.server : '–']];
  $('#w-facts').innerHTML = facts.map(([k,v]) => `<div><dt>${esc(k)}</dt><dd title="${esc(v)}">${esc(v)}</dd></div>`).join('');
  $('#jobs').innerHTML = jobs.length ? jobs.map(([k, x]) => `<div class="job"><div class="k">${esc(T[k]||k)}</div><div class="fn">${esc(base(x.rel))}</div>
     <div class="st">${esc(x.stage||'')} · ${Math.round(x.progress||0)}% · ${esc(T.since + dur(now - x.since))}</div><div class="bar"><i data-w="${Math.min(100, +x.progress||0)}"></i></div></div>`).join('')
     : `<div class="empty">${esc(T.noj)}</div>`;
  document.querySelectorAll('.bar i').forEach(i => i.style.width = i.dataset.w + '%');
  const d = $('#dash'); if (w && /^https?:\/\//.test(w.server||'')) { d.href = w.server; d.textContent = T.dash; d.hidden = false; } else d.hidden = true;
  $('#ver').textContent = 'MediaScan Manager ' + s.version;
}

async function post(action, extra){
  const r = await api('/api/action', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.assign({action}, extra||{}))});
  if (!r.ok) throw new Error(r.status);
  return (await r.json()).result;
}
const F = id => $('#f-' + id);
let SET = null;
function showSettings(on){ $('#c-settings').hidden = !on; $('#c-worker').hidden = on; $('#c-jobs').hidden = on; if (on) { loadSettings(); setTimeout(() => F('url').focus(), 50); } }
function note(el, text, cls){ el.textContent = text; el.className = 'small' + (cls ? ' ' + cls : ''); }
async function loadSettings(){
  try { const r = await api('/api/settings'); if (!r.ok) throw 0; SET = await r.json(); } catch { return; }
  F('url').value = SET.url; F('token').value = SET.token; F('name').value = SET.name; F('media').value = SET.media;
  F('maxh').value = String(SET.max_height); if (F('maxh').value !== String(SET.max_height)) F('maxh').value = '0';
  F('subs').checked = SET.subs; $('#subs-more').classList.toggle('off', !SET.subs);
  F('oskey').value = SET.os_key; F('osuser').value = SET.os_user; F('ospw').value = ''; F('ospw').placeholder = SET.os_pw_set ? T.unchanged : '';
  const wm = F('whisper'); wm.innerHTML = [...new Set([SET.whisper, 'large-v3', 'large-v3-turbo', 'medium', 'small', 'base'])].map(m => `<option value="${esc(m)}">${esc(m)}</option>`).join(''); wm.value = SET.whisper;
  F('ourl').value = SET.ollama_url; F('omodel').value = SET.ollama_model;
  $('#omodels').innerHTML = SET.ollama_models.map(m => `<option value="${esc(m)}">`).join('');
  note($('#o-res'), SET.ollama_models.length ? T.o_list + SET.ollama_models.join(', ') : T.o_none, SET.ollama_models.length ? '' : 'bad');
  note($('#m-res'), SET.media ? (SET.media_ok ? T.m_ok : T.m_bad) : '', SET.media_ok ? 'ok' : 'bad'); note($('#t-res'), '');
  $('#b-map').hidden = !/^\\\\/.test(F('media').value);
}
function collect(){
  return {url: F('url').value, token: F('token').value, name: F('name').value, media: F('media').value, max_height: F('maxh').value, subs: F('subs').checked,
    os_key: F('oskey').value, os_user: F('osuser').value, os_pw: F('ospw').value, whisper: F('whisper').value, ollama_url: F('ourl').value, ollama_model: F('omodel').value};
}
function wireSettings(){
  document.querySelectorAll('[data-t]').forEach(el => el.textContent = T[el.dataset.t] || '');
  $('#b-settings').textContent = T.settings;
  $('#b-settings').onclick = () => showSettings($('#c-settings').hidden);
  $('#b-close').onclick = () => showSettings(false);
  $('#b-show').onclick = () => { const t = F('token'); t.type = t.type === 'password' ? 'text' : 'password'; $('#b-show').textContent = t.type === 'password' ? T.show : T.hide; };
  F('subs').onchange = () => $('#subs-more').classList.toggle('off', !F('subs').checked);
  F('media').oninput = () => { $('#b-map').hidden = !/^\\\\/.test(F('media').value); };
  F('url').oninput = F('token').oninput = () => note($('#t-res'), '');
  const run = async (btn, fn) => { btn.classList.add('busy'); btn.disabled = true; try { await fn(); } catch (e) { toast(T.failed + ' (' + e.message + ')'); } finally { btn.classList.remove('busy'); btn.disabled = false; } };
  $('#b-test').onclick = e => run(e.currentTarget, async () => { const r = await post('test_server', {url: F('url').value, token: F('token').value});
    note($('#t-res'), r.ok ? T.t_ok + (r.version || '?') : T.t_bad + r.error, r.ok ? 'ok' : 'bad'); });
  $('#b-find').onclick = e => run(e.currentTarget, async () => { const r = await post('find_media', {url: F('url').value, token: F('token').value});
    if (r.path) { F('media').value = r.path; note($('#m-res'), T.m_found + r.path, 'ok'); } else note($('#m-res'), T.m_none, 'bad'); });
  $('#b-map').onclick = e => { const letter = prompt(T.map_letter, 'M'); if (!letter) return; run(e.currentTarget, async () => { const r = await post('map_share', {path: F('media').value, letter});
    if (r.ok) { F('media').value = r.path; $('#b-map').hidden = true; note($('#m-res'), T.map_ok + r.path, 'ok'); } else note($('#m-res'), T.map_bad + r.error, 'bad'); }); };
  $('#settings').onsubmit = e => { e.preventDefault(); run($('#b-save'), async () => {
    const r = await post('save_settings', collect()); if (r !== 'ok') { toast(T.failed + ': ' + r); return; }
    toast(T.saved); $('#banner').hidden = true; setupBanner = false;
    const j = ST && ST.jobs.worker || {};
    if (j.running && await ask(T.q_apply, T.q_apply_t, T.yes_apply)) await post('restart'); else if (!j.running && j.wanted !== 'running') await post('start');
    showSettings(false); refresh(); }); };
}

function locked(){ quitting = true; document.body.innerHTML = `<div id="app"><div class="card gone">${esc(T.locked)}</div></div>`; }
async function refresh(){
  if (quitting) return;
  try {
    const r = await api('/api/state'); if (r.status === 401) return locked(); if (!r.ok) throw 0;
    ST = await r.json(); fails = 0; if (!setupBanner) $('#banner').hidden = true; render();
  } catch { if (++fails >= 3) { const b = $('#banner'); b.textContent = T.lost; b.className = 'banner'; b.hidden = false; setupBanner = false; } }
}
async function loadLog(){
  if (quitting) return;
  try {
    const r = await api('/api/log?which=' + which + '&n=300'); if (!r.ok) return;
    const {lines} = await r.json(); const el = $('#log'); const atEnd = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    el.innerHTML = lines.map(l => { const c = /FOUT|ERROR|Traceback|mislukt|failed/i.test(l) ? 'e' : /=====|start:|klaar|done/i.test(l) ? 's' : ''; return c ? `<span class="${c}">${esc(l)}</span>` : esc(l); }).join('\n');
    if (atEnd) el.scrollTop = el.scrollHeight;
  } catch {}
}

document.addEventListener('DOMContentLoaded', () => {
  for (const [id, k] of [['#b-start','start'],['#b-stop','stop'],['#b-restart','restart'],['#t-now','now'],['#t-log','log'],['#b-folder','folder'],['#b-openlog','openlog'],['#b-quit','quit']]) $(id).textContent = T[k];
  const tabs = document.querySelectorAll('#logtabs button'); tabs[0].textContent = T.wlog; tabs[1].textContent = T.mlog;
  tabs.forEach(b => b.onclick = () => { which = b.dataset.w; tabs.forEach(x => x.classList.toggle('on', x === b)); $('#log').innerHTML = ''; loadLog(); });
  $('#b-start').onclick = e => act('start', e.currentTarget);
  $('#b-restart').onclick = e => act('restart', e.currentTarget);
  $('#b-stop').onclick = async e => { const b = e.currentTarget; if (await ask(T.q_stop, T.q_stop_t, T.yes_stop)) act('stop', b); };
  $('#b-quit').onclick = async e => { const b = e.currentTarget; if (await ask(T.q_title, T.q_text, T.yes_quit)) act('quit', b); };
  $('#b-folder').onclick = e => act('open_folder', e.currentTarget);
  $('#b-openlog').onclick = e => act('open_log', e.currentTarget, {which});
  document.addEventListener('keydown', e => { if (e.key === 'Escape') $('#m-no').click(); });
  wireSettings();
  api('/api/settings').then(r => r.ok ? r.json() : null).then(d => { if (d && !d.configured) { setupBanner = true; const b = $('#banner'); b.textContent = T.setup; b.className = 'banner info'; b.hidden = false; showSettings(true); } }).catch(() => {});
  refresh(); loadLog(); setInterval(refresh, 1500); setInterval(loadLog, 3000);
});
'''

if __name__ == '__main__':
    main()
