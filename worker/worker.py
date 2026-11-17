"""
MediaScan worker. Runs on Windows, Linux and macOS (Apple Silicon).
Fetches jobs from the MediaScan container on the NAS, does the heavy work locally
and reports progress back. The worker NEVER modifies originals:
the new version goes to _MediaScan/staging, and the NAS replaces it after checking.
"""
import os, re, sys, json, time, glob, shutil, socket, hashlib, threading, subprocess, configparser, traceback, platform
import urllib.request, urllib.error
from fractions import Fraction

WORKER_VERSION = '1.0.1'

IS_MAC = sys.platform == 'darwin'
OS_NAME = 'Windows' if os.name == 'nt' else ('macOS' if IS_MAC else 'Linux')
if os.environ.get('MEDIASCAN_HOME'):
    LOCAL = os.environ['MEDIASCAN_HOME']
elif os.name == 'nt':
    LOCAL = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'MediaScan')
elif IS_MAC:
    LOCAL = os.path.expanduser('~/Library/Application Support/MediaScan')
else:
    LOCAL = os.path.join(os.environ.get('XDG_DATA_HOME', os.path.expanduser('~/.local/share')), 'mediascan')
# worker.ini = all settings (including OpenSubtitles and translation). config.ini (older installs) and
# overrides.ini (individual tweaks without secrets) are read too; later files take precedence.
CFG_FILES = [os.path.join(LOCAL, f) for f in ('config.ini', 'worker.ini', 'overrides.ini')]
CFG = configparser.ConfigParser(interpolation=None); CFG.read(CFG_FILES, encoding='utf-8')
SERVER = CFG.get('server', 'url', fallback='').strip().rstrip('/')
if SERVER and not re.match(r'^https?://', SERVER): SERVER = 'http://' + SERVER      # '10.0.0.5:8780' without a scheme
TOKEN = CFG.get('server', 'worker_token', fallback='')
NAME = re.sub(r'[^A-Za-z0-9._ -]', '-', CFG.get('worker', 'name', fallback=socket.gethostname())).strip(' ._-')[:64] or 'worker'
MEDIA = CFG.get('worker', 'media_root', fallback={'Windows': 'M:\\', 'macOS': '/Volumes/Media'}.get(OS_NAME, '/mnt/media'))
if os.name == 'nt' and re.fullmatch(r'[A-Za-z]:', MEDIA): MEDIA += '\\'       # 'M:' alone would mean 'current folder on M:'
WORK = os.path.join(LOCAL, 'work')
ENCODER_CFG = CFG.get('worker', 'encoder', fallback='auto').strip().lower()   # auto | nvenc | qsv | amf | vaapi | videotoolbox | x265
VAAPI_DEVICE = CFG.get('worker', 'vaapi_device', fallback='/dev/dri/renderD128')
ENCODE_ENABLED = CFG.getboolean('worker', 'encode', fallback=True)
MAX_HEIGHT = CFG.getint('worker', 'max_height', fallback=0)          # 0 = no limit, e.g. 1080 for a weak machine
SUBS_ENABLED = CFG.getboolean('worker', 'subs', fallback=True)
AUTO_UPDATE = CFG.getboolean('worker', 'auto_update', fallback=True)   # take over a new version from the server
CPU_ENABLED = CFG.getboolean('worker', 'cpu_encode', fallback=True)     # offer a CPU (x265) slot next to the GPU one (dashboard decides per worker)
CPU_THREADS = CFG.getint('worker', 'cpu_threads', fallback=0)          # threads for x265; 0 = all cores but two (the GPU job keeps those)
os.makedirs(WORK, exist_ok=True)

for d in (glob.glob(os.path.join(sys.prefix, 'Lib', 'site-packages', 'nvidia', '*', 'bin')) if os.name == 'nt' else []):
    try: os.add_dll_directory(d)
    except Exception: pass
    os.environ['PATH'] = d + os.pathsep + os.environ['PATH']


def log(msg):
    print(time.strftime('%H:%M:%S') + '  ' + msg, flush=True)


def find_tool(name):
    p = shutil.which(name)
    if p: return p
    roots = [LOCAL] + ([os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Microsoft', 'WinGet', 'Packages')] if os.name == 'nt' else [])
    roots += ['/opt/homebrew/bin', '/usr/local/bin'] if IS_MAC else []
    for root in roots:
        hits = glob.glob(os.path.join(root, '**', name), recursive=True)
        if hits: return hits[0]
    raise SystemExit(f'{name} not found')


EXE = '.exe' if os.name == 'nt' else ''
FFMPEG = find_tool('ffmpeg' + EXE); FFPROBE = os.path.join(os.path.dirname(FFMPEG), 'ffprobe' + EXE)
os.environ['PATH'] = os.path.dirname(FFMPEG) + os.pathsep + os.environ['PATH']
THREADS = os.cpu_count() or 8
# the CPU slot: x265 gets a fixed thread pool so it never fights the GPU job's decoder/muxer/VMAF for every core,
# and runs below normal priority so the machine stays responsive
CPU = {'preset': 'medium', 'pools': CPU_THREADS or max(2, THREADS - 2), 'busy': False}
_HERE = os.path.dirname(os.path.abspath(__file__))
RESERVED = {'bytes': 0, 'lock': threading.Lock()}     # work space the running encode jobs still need (two slots share one disk)
NICE_FLAGS = 0x00004000 if os.name == 'nt' else 0          # BELOW_NORMAL_PRIORITY_CLASS


# ------------------------------------------------------------------ server
FINAL_TRIES = 2880      # a finished job is reported until the server answers (up to ~2 days): hours of work must not be lost


def call(path, body=None, method='POST', tries=30):
    data = json.dumps(body or {}).encode()
    for i in range(tries):
        try:
            req = urllib.request.Request(SERVER + path, data=data, method=method,
                                         headers={'Authorization': 'Bearer ' + TOKEN, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500: raise          # error in the request itself: retrying is pointless
            time.sleep(min(60, 5 * (i + 1)))
        except Exception:
            time.sleep(min(60, 5 * (i + 1)))
    raise RuntimeError('server unreachable: ' + path)


class Cancelled(Exception): pass
class FinalError(RuntimeError): pass     # retrying the same job would give the same result


class JobCtx:
    def __init__(self, job):
        self.job = job; self.last = 0; self.stage = ''; self.progress = 0.0; self.t0 = time.time(); self.quiet_until = 0
    def report(self, progress, stage=None, message=None, force=False):
        if stage: self.stage = stage
        self.progress = progress
        now = time.time()
        if not force and now - self.last < 5: return
        self.last = now
        local_status()
        if now < self.quiet_until: return
        try:     # few tries and no waiting: the work goes on while the server is briefly away (e.g. a NAS reboot)
            r = call('/api/worker/progress', {'name': NAME, 'job_id': self.job['id'], 'progress': round(progress, 1),
                                              'stage': self.stage, 'message': message}, tries=1)
        except Exception:
            self.quiet_until = time.time() + 60; return
        if r is not None and not r.get('continue', True): raise Cancelled()


current = {'encode': None, 'subs': None, 'cpu': None}
LINK = {'ok': None, 'at': 0}           # last contact with the server (for the local status)
PHASE = {'v': 'starting'}              # starting | waiting_media | connecting | ready
_ST_LOCK = threading.Lock()


def local_status():
    """status/worker.json: what the worker is doing now, for the manager app on this machine (no secrets)."""
    try:
        jobs = {k: {'rel': v.job.get('rel'), 'stage': v.stage, 'progress': round(v.progress, 1), 'since': int(v.t0)}
                for k, v in current.items() if v}
        st = {'version': WORKER_VERSION, 'name': NAME, 'server': SERVER, 'pid': os.getpid(), 'at': int(time.time()), 'phase': PHASE['v'],
              'server_ok': LINK['ok'], 'server_at': LINK['at'], 'draining': DRAIN['on'],
              'encode': ENCODE_ENABLED and CAPS.get('encode', False), 'subs': SUBS_ENABLED and CAPS.get('subs', False),
              'cpu': CAPS.get('cpu', False), 'jobs': jobs}
        d = os.path.join(LOCAL, 'status'); os.makedirs(d, exist_ok=True)
        with _ST_LOCK:
            tmp = os.path.join(d, f'worker.json.{threading.get_ident()}.tmp')
            with open(tmp, 'w', encoding='utf-8') as f: json.dump(st, f)
            os.replace(tmp, os.path.join(d, 'worker.json'))
    except Exception:
        pass


def clean_leftovers():
    """Temp folders of subtitle jobs (subs_*) and half-written status files that a killed worker left behind."""
    for n in os.listdir(LOCAL):
        p = os.path.join(LOCAL, n)
        if n.startswith('subs_') and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    d = os.path.join(LOCAL, 'status')
    if os.path.isdir(d):
        for n in os.listdir(d):
            if n.endswith('.tmp'):
                try: os.remove(os.path.join(d, n))
                except OSError: pass


def gpu_name():
    try:
        n = subprocess.run(['nvidia-smi', '--query-gpu=name', '--format=csv,noheader'], capture_output=True, text=True, timeout=10).stdout.strip()
        if n: return n
    except Exception:
        pass
    try:
        if IS_MAC:
            r = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True, timeout=10)
            return r.stdout.strip()
        if os.name == 'nt':
            r = subprocess.run(['powershell', '-NoProfile', '-Command', '(Get-CimInstance Win32_VideoController | Where-Object {$_.Name -notmatch "Basic|Virtual"} | Select-Object -First 1).Name'],
                               capture_output=True, text=True, timeout=15)
            return r.stdout.strip()
        r = subprocess.run(['sh', '-c', "lspci | grep -iE 'vga|3d|display' | head -1 | cut -d: -f3"], capture_output=True, text=True, timeout=10)
        return r.stdout.strip()
    except Exception:
        return ''


def _sh(cmd, timeout=15):
    try: return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)).stdout.strip()
    except Exception: return ''


def sysinfo():
    """Hardware and software facts for the dashboard's Info page (no secrets, no paths). Gathered once at start."""
    hw = {'threads': THREADS}
    try:          # just the version number; the OS name is reported separately
        if os.name == 'nt':
            v = platform.win32_ver(); b = int((v[1] or '0').split('.')[-1] or 0)
            hw['os_version'] = '11' if b >= 22000 else v[0]
        elif IS_MAC: hw['os_version'] = platform.mac_ver()[0]
        else: hw['os_version'] = platform.release()
    except Exception:
        pass
    try:
        if os.name == 'nt':
            r = _sh(['powershell', '-NoProfile', '-Command', '$c=Get-CimInstance Win32_Processor | Select-Object -First 1; '
                     '$m=(Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory; "$($c.Name)|$($c.NumberOfCores)|$m"'])
            cpu, cores, mem = (r.split('|') + ['', '', ''])[:3]
            hw.update(cpu=cpu.strip(), cores=int(cores or 0) or None, ram_gb=round(int(mem) / 2**30) if mem.isdigit() else None)
        elif IS_MAC:
            hw.update(cpu=_sh(['sysctl', '-n', 'machdep.cpu.brand_string']), cores=int(_sh(['sysctl', '-n', 'hw.physicalcpu']) or 0) or None,
                      ram_gb=round(int(_sh(['sysctl', '-n', 'hw.memsize']) or 0) / 2**30) or None)
        else:
            txt = open('/proc/cpuinfo', encoding='utf-8', errors='replace').read()
            m = re.search(r'^model name\s*:\s*(.+)$', txt, re.M); hw['cpu'] = m.group(1).strip() if m else platform.processor()
            ids = set(re.findall(r'^core id\s*:\s*(\d+)', txt, re.M)); hw['cores'] = len(ids) or None
            m = re.search(r'^MemTotal:\s*(\d+)', open('/proc/meminfo', encoding='utf-8').read(), re.M)
            hw['ram_gb'] = round(int(m.group(1)) / 2**20) if m else None
    except Exception:
        pass
    r = (_sh(['nvidia-smi', '--query-gpu=memory.total,driver_version', '--format=csv,noheader,nounits']).splitlines() or [''])[0]   # first GPU
    if r and ',' in r:
        v, d = r.split(',', 1)
        try: hw['vram_gb'] = round(float(v.strip()) / 1024, 1)
        except ValueError: pass
        hw['gpu_driver'] = d.strip()
    elif IS_MAC and hw.get('ram_gb'): hw['vram_gb'] = hw['ram_gb']      # unified memory
    ver = {}
    m = re.match(r'ffmpeg version (\S+)', _sh([FFMPEG, '-version']) or '')
    if m: ver['ffmpeg'] = m.group(1)[:40]
    if os.name == 'nt': ver['mkvtoolnix'] = re.search(r'/releases/([\d.]+)/', MKVTN_URL[0]).group(1)       # the versions the worker downloads itself
    if os.name == 'nt' or sys.platform == 'linux': ver['dovi_tool'] = re.search(r'/download/([\d.]+)/', DOVI_URL['nt'][0]).group(1)
    ollama = CFG.get('translation', 'ollama_model', fallback=CFG.get('vertaling', 'ollama_model', fallback='gemma4:12b'))   # [vertaling] = old section name
    models = {'whisper': CFG.get('whisper', 'model', fallback='large-v3')[:40],
              'ollama': ollama[:60]}      # same default as subs.py
    for k in ('cpu', 'os_version', 'gpu_driver'):
        if isinstance(hw.get(k), str): hw[k] = hw[k][:80]
    return {'hw': hw, 'versions': ver, 'models': models}


def status_loop():
    """Local status every 5 s, independent of the server (which can take minutes when the NAS is off)."""
    while True:
        local_status(); time.sleep(5)


def files_sha():
    """sha256 of the worker's own files, so the server can tell whether this worker is up to date."""
    out = {}
    for name in UPDATABLE:
        try:
            with open(os.path.join(_HERE, name), 'rb') as f: out[name] = hashlib.sha256(f.read()).hexdigest()
        except OSError:
            pass
    return out


def heartbeat_loop():
    gpu = gpu_name()
    try: extra = sysinfo()
    except Exception as e: extra = {}; log(f'system info not available: {e}')
    try: extra['files_sha'] = files_sha()
    except Exception: pass
    extra['version'] = WORKER_VERSION
    while True:
        try:
            free = shutil.disk_usage(WORK).free
            info = {'gpu': gpu, 'free_local': free, 'encoder': ENCODERS.get(ENC['key'] or '', {}).get('name'), 'enc_key': ENC['key'], **extra,
                    'caps': CAPS, 'roles': [r for r, on in CAPS.items() if on], 'max_height': MAX_HEIGHT,
                    'os': OS_NAME,
                    'jobs': {k: (v.job['id'] if v else None) for k, v in current.items()}}
            call('/api/worker/heartbeat', {'name': NAME, 'info': info}, tries=3)
            LINK.update(ok=True, at=int(time.time()))
        except Exception as e:
            LINK['ok'] = False
            log(f'heartbeat failed: {e}')
        time.sleep(30)


# ------------------------------------------------------------------ helpers
def nas(rel):
    """Path on the media share. Never outside the share (not even if the server sends something odd)."""
    parts = [p for p in str(rel).replace('\\', '/').split('/') if p not in ('', '.')]
    if any(p == '..' or ':' in p for p in parts): raise RuntimeError(f'invalid path from the server: {rel!r}')
    return os.path.join(MEDIA, *parts)


def copy_progress(src, dst, ctx, p0, p1, label):
    total = os.path.getsize(src); done = 0; t0 = time.time()
    try:
        with open(src, 'rb') as fi, open(dst + '.part', 'wb') as fo:
            while True:
                b = fi.read(16 * 1024 * 1024)
                if not b: break
                fo.write(b); done += len(b)
                mbps = done / 1e6 / max(time.time() - t0, 0.1)
                ctx.report(p0 + (p1 - p0) * done / total, label, f'{done/1e9:.1f}/{total/1e9:.1f} GB · {mbps:.0f} MB/s')
    except BaseException:                 # cancelled or the share dropped: leave no half file behind
        try: os.remove(dst + '.part')
        except OSError: pass
        raise
    for attempt in range(6):          # Windows: a virus scanner or a leftover process may briefly hold the file
        try:
            if os.path.exists(dst): os.remove(dst)
            os.replace(dst + '.part', dst); return
        except PermissionError:
            if attempt == 5: raise
            time.sleep(5)


def kill_strays():
    """Stop ffmpeg processes still running in our work folder (from a previous, aborted worker)."""
    try:
        if os.name == 'nt':
            wq = WORK.replace("'", "''")
            ps = ("Get-CimInstance Win32_Process -Filter \"Name='ffmpeg.exe' OR Name='ffprobe.exe'\" | "
                  f"Where-Object {{ $_.CommandLine -and $_.CommandLine.Contains('{wq}') }} | "
                  "ForEach-Object { Stop-Process -Id $_.ProcessId -Force; $_.ProcessId }")
            r = subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True, text=True, timeout=60,
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            killed = [x for x in r.stdout.split() if x.isdigit()]
        else:
            r = subprocess.run(['pkill', '-f', 'ff(mpeg|probe).*' + re.escape(WORK)], capture_output=True, timeout=30)
            killed = ['?'] if r.returncode == 0 else []
        if killed:
            log(f'stopped {len(killed)} leftover ffmpeg process(es)'); time.sleep(3)
    except Exception as e:
        log(f'cleaning up old processes failed: {e}')


def _ffprobe_json(args):
    r = subprocess.run([FFPROBE, '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams', '-show_chapters'] + args,
                       capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=600,
                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if r.returncode or not r.stdout.strip().startswith('{'): raise RuntimeError('ffprobe: ' + ((r.stderr or '').strip()[-300:] or 'failed'))
    return json.loads(r.stdout)


def probe(path): return _ffprobe_json([path])


def ts(x): return f'{x:.3f}'


STALL_SEC = 30 * 60        # ffmpeg without any progress for this long (share dropped, NAS rebooting): give up on the step


def run_ff(args, ctx=None, dur=None, p0=0, p1=0, label=None, cwd=None, nice=False):
    """ffmpeg with progress via -progress. A run that makes no progress for STALL_SEC is killed (a stalled SMB read
    otherwise blocks the slot forever while the heartbeat keeps saying everything is fine)."""
    cmd = [FFMPEG, '-hide_banner', '-nostats', '-loglevel', 'error', '-y', '-progress', 'pipe:1'] + args
    if nice and os.name != 'nt': cmd = ['nice', '-n', '10'] + cmd
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', cwd=cwd,
                         creationflags=NICE_FLAGS if nice else 0)
    err_lines = []; last = {'t': time.time(), 'pos': None}
    def drain():
        for l in p.stderr: err_lines.append(l)
    def watchdog():
        while p.poll() is None:
            time.sleep(30)
            if time.time() - last['t'] > STALL_SEC:
                last['stalled'] = True; p.kill(); return
    threading.Thread(target=drain, daemon=True).start()
    threading.Thread(target=watchdog, daemon=True).start()
    try:
        speed = ''
        for line in p.stdout:
            k, _, v = line.strip().partition('=')
            if k == 'speed': speed = v
            if k == 'out_time_us' and v.isdigit():
                if v != last['pos']: last.update(t=time.time(), pos=v)
                if ctx and dur:
                    f = min(1.0, int(v) / 1e6 / dur)
                    ctx.report(p0 + (p1 - p0) * f, label, f'{f*100:.0f}% · speed {speed}')
        p.wait()
    except Cancelled:
        p.kill(); p.wait(timeout=15); raise
    if last.get('stalled'):
        raise RuntimeError(f'ffmpeg made no progress for {STALL_SEC // 60} minutes (share unreachable?) - stopped')
    if p.returncode != 0:
        raise RuntimeError('ffmpeg: ' + ''.join(err_lines)[-600:])


# ------------------------------------------------------------------ encoders
# Every encoder: name, search range for the quality parameter, and the ffmpeg arguments.
# 'pre' goes before the input (device), 'out' after the input (encoder settings).
ENCODERS = {
    'nvenc': {'name': 'NVIDIA NVENC', 'range': (18, 40)},
    'qsv':   {'name': 'Intel Quick Sync (QSV)', 'range': (16, 40)},
    'amf':   {'name': 'AMD AMF', 'range': (16, 40)},
    'vaapi': {'name': 'VA-API (Linux, Intel/AMD)', 'range': (16, 40)},
    # VideoToolbox: -q:v runs 1-100 and higher = better; internally we search on 100-q so 'higher = smaller' still holds
    'videotoolbox': {'name': 'Apple VideoToolbox', 'range': (25, 75)},
    'x265':  {'name': 'CPU (x265)', 'range': (14, 32)},
    'x265-grain': {'name': 'CPU (x265, tune grain)', 'range': (14, 30)},   # the CPU slot for grainy films
}
ENC = {'key': None, 'feat': set()}      # chosen encoder + supported extras


def enc_args(key, q, feat=None):
    feat = ENC['feat'] if feat is None else feat
    q = str(q)
    if key == 'nvenc':
        out = ['-c:v:0', 'hevc_nvenc', '-profile:v:0', 'main10', '-preset', 'p7', '-tune', 'hq', '-rc', 'vbr', '-cq', q, '-b:v', '0',
               '-multipass', 'fullres', '-spatial-aq', '1', '-temporal-aq', '1', '-rc-lookahead', '32']
        if 'bref' in feat: out += ['-bf', '4', '-b_ref_mode', 'middle']        # Turing and newer
        return [], out + ['-pix_fmt:v:0', 'p010le']
    if key == 'qsv':
        out = ['-c:v:0', 'hevc_qsv', '-profile:v:0', 'main10', '-preset', 'veryslow', '-global_quality', q]
        if 'la' in feat: out += ['-look_ahead_depth', '40', '-extbrc', '1']
        return [], out + ['-pix_fmt:v:0', 'p010le']
    if key == 'amf':
        return [], ['-c:v:0', 'hevc_amf', '-profile:v:0', 'main10', '-quality', 'quality', '-rc', 'cqp',
                    '-qp_i', q, '-qp_p', q, '-qp_b', q, '-pix_fmt:v:0', 'p010le']
    if key == 'vaapi':
        return ['-vaapi_device', VAAPI_DEVICE], ['-filter:v:0', 'format=p010,hwupload', '-c:v:0', 'hevc_vaapi', '-profile:v:0', 'main10',
                                                 '-rc_mode', 'CQP', '-qp', q]
    if key == 'videotoolbox':
        out = ['-c:v:0', 'hevc_videotoolbox', '-profile:v:0', 'main10', '-q:v:0', str(100 - int(q)), '-allow_sw', '0', '-realtime', '0']
        if 'prio' in feat: out += ['-prio_speed', '0']
        return [], out + ['-pix_fmt:v:0', 'p010le']
    if key == 'x265-grain':
        return [], ['-c:v:0', 'libx265', '-preset', CPU['preset'], '-tune', 'grain', '-crf', q,
                    '-x265-params', f"log-level=error:pools={CPU['pools']}", '-pix_fmt:v:0', 'yuv420p10le']
    return [], ['-c:v:0', 'libx265', '-preset', 'slow', '-crf', q, '-x265-params', 'log-level=error', '-pix_fmt:v:0', 'yuv420p10le']


STAT_TAGS = ('BPS', 'NUMBER_OF_FRAMES', 'NUMBER_OF_BYTES', '_STATISTICS_TAGS', '_STATISTICS_WRITING_APP',
             '_STATISTICS_WRITING_DATE_UTC')


def stat_tags_off(vi):
    out = []
    for t in STAT_TAGS:
        for k in (t, t + '-eng'): out += [f'-metadata:s:v:{vi}', f'{k}=']
    return out


def color_args(v):
    a = []
    for k, opt in (('color_primaries', '-color_primaries:v:0'), ('color_transfer', '-color_trc:v:0'), ('color_space', '-colorspace:v:0'), ('color_range', '-color_range:v:0')):
        if v.get(k) and v.get(k) != 'unknown': a += [opt, v[k]]
    return a


def venc(cq, v, deint=None, vi=0, key=None):
    key = key or ENC['key']
    pre, out = enc_args(key, cq, ENC['feat'] if key == ENC['key'] else set())
    if key == 'nvenc':
        # NVENC picks the lowest HEVC level that fits the resolution (1080p: level 4.0) and then silently caps
        # VBR at that level's maximum (12 Mbps). Grainy films never reach the quality target under that cap.
        # Level 4.1 (20 Mbps) / 5.1 (40 Mbps) are what 1080p/4K players support anyway.
        uhd = (v.get('width') or 0) > 2048 or (v.get('height') or 0) > 1152
        out += ['-level:v:0', '5.1' if uhd else '4.1', '-maxrate:v:0', '40M' if uhd else '20M', '-bufsize:v:0', '80M' if uhd else '40M']
    if deint:                                   # interlaced source (DVD): deinterlace properly first
        if '-filter:v:0' in out:
            i = out.index('-filter:v:0'); out = out[:i + 1] + [deint + ',' + out[i + 1]] + out[i + 2:]
        else:
            out = ['-filter:v:0', deint] + out
    out = out + color_args(v)
    if vi:                                      # cover art listed before the film: aim every option at the film
        out = [a.replace(':v:0', f':v:{vi}') if a.startswith('-') else a for a in out]
    return pre, out


def _try_encode(key, feat):
    pre, out = enc_args(key, 26, feat)
    cmd = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-y'] + pre + ['-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=24', '-t', '1',
          '-pix_fmt', 'yuv420p10le'] + out + ['-f', 'null', '-']
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
        lines = [l for l in (r.stderr or '').splitlines() if l.strip()]
        return r.returncode == 0, (lines[0] if lines else '')[:160]
    except Exception as e:
        return False, str(e)


def detect_encoder():
    """Pick the best working encoder on this machine and test which extra features it has."""
    if ENCODER_CFG in ENCODERS: order = [ENCODER_CFG]
    elif IS_MAC: order = ['videotoolbox', 'x265']
    elif os.name == 'nt': order = ['nvenc', 'qsv', 'amf', 'x265']
    else: order = ['nvenc', 'qsv', 'amf', 'vaapi', 'x265']
    for key in order:
        full = {'nvenc': {'bref'}, 'qsv': {'la'}, 'videotoolbox': {'prio'}}.get(key, set())
        ok, err = _try_encode(key, full)
        if ok:
            ENC.update(key=key, feat=full); break
        if full:
            ok2, err = _try_encode(key, set())
            if ok2:
                ENC.update(key=key, feat=set()); break
        log(f'  encoder {key}: not usable ({err[:120]})')
    if not ENC['key']:
        raise SystemExit('No HEVC 10-bit encoder works on this machine (not even x265) - check ffmpeg')
    log(f"Encoder: {ENCODERS[ENC['key']]['name']}" + (f" (+{', '.join(sorted(ENC['feat']))})" if ENC['feat'] else ''))


def subs_capable():
    """Subtitles are only possible when subs.py and Whisper are present (faster-whisper, or mlx-whisper on a Mac)."""
    if not SUBS_ENABLED or not os.path.exists(os.path.join(_HERE, 'subs.py')): return False
    try:
        import importlib.util
        has = lambda m: importlib.util.find_spec(m) is not None
        return (has('faster_whisper') or (IS_MAC and has('mlx_whisper'))) and has('srt')
    except Exception:
        return False


CAPS = {'encode': False, 'subs': False, 'cpu': False}


def check_vmaf():
    r = subprocess.run([FFMPEG, '-hide_banner', '-filters'], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=60)
    if 'libvmaf' not in (r.stdout or ''):
        raise SystemExit('This ffmpeg has no libvmaf. Use a "full"/"gpl" build (Windows: winget install Gyan.FFmpeg, '
                         'Linux: BtbN ffmpeg-master-latest-linux64-gpl, '
                         'macOS: brew install ffmpeg or the build from the installer).')


def vmaf_vals(dist, ref, model, sub=1, hw=False, ctx=None, dur=None, p0=0, p1=0, label=None, vi=0, pair='pts', fps=None,
              refvf=None, frames=False, threads=None, nice=False):
    """Per-frame VMAF scores (list; with frames=True a list of (frame number, score)). The log is written relative to
    WORK (no colon in the filter path). vi = which video stream (skip cover art). How frames are paired:
      pts   - by timestamp (libvmaf frame sync). Breaks on sources with irregular timestamps.
      index - frame n of one file with frame n of the other, timestamps ignored. Exact, but only valid when both files
              have the same frames (checked by the caller with frame_count).
      fps   - both converted to a fixed frame rate (fps) first, then paired in order."""
    log_name = f'vmaf_{os.getpid()}_{threading.get_ident()}.json'
    if pair == 'index': sync = 'setpts=N/24/TB,'           # any fixed rate will do: only the order counts
    elif pair == 'fps' and fps: sync = f'fps={fps},setpts=N/FRAME_RATE/TB,'
    else: sync = 'setpts=PTS-STARTPTS,'
    rv = (refvf + ',') if refvf else ''
    lf = (f'[0:v:{vi}]{sync}format=yuv420p10le[d];[1:v:{vi}]{rv}{sync}format=yuv420p10le[r];'
          f'[d][r]libvmaf=model=version={model}:n_threads={threads or THREADS}:n_subsample={sub}:log_fmt=json:log_path={log_name}')
    hwa = hwdec() if hw else []
    run_ff(hwa + ['-i', dist] + hwa + ['-i', ref, '-lavfi', lf, '-f', 'null', '-'], ctx, dur, p0, p1, label, cwd=WORK, nice=nice)
    lp = os.path.join(WORK, log_name)
    with open(lp, encoding='utf-8') as f: d = json.load(f)
    os.remove(lp)
    if frames:
        return [(fr.get('frameNum', i * sub), fr['metrics']['vmaf']) for i, fr in enumerate(d['frames'])]
    return [fr['metrics']['vmaf'] for fr in d['frames']]


def frame_count(path, vi):
    """Number of frames (packets) in video stream vi, without decoding. None if it cannot be determined."""
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', f'v:{vi}', '-count_packets',
                            '-show_entries', 'stream=nb_read_packets', '-of', 'csv=p=0', path],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=1800,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        n = int(r.stdout.strip().splitlines()[0].strip(', '))
        return n if n > 0 else None
    except Exception:
        return None


def low_spots(fv, fps, below=60, limit=5):
    """Where the scores drop below `below`: 'h:mm:ss-h:mm:ss' ranges (for the log, to find the cause)."""
    try: rate = float(Fraction(str(fps))) if fps else 24.0
    except (ValueError, ZeroDivisionError): rate = 24.0
    rate = rate if rate and rate > 0 else 24.0
    clock = lambda s: f'{int(s // 3600)}:{int(s % 3600 // 60):02d}:{int(s % 60):02d}'
    spans = []
    for n, val in fv:
        t = n / rate
        if val >= below: continue
        if spans and t - spans[-1][1] <= 5: spans[-1][1] = t
        else: spans.append([t, t])
    return [f'{clock(a)}-{clock(b)}' for a, b in spans[:limit]] + (['...'] if len(spans) > limit else [])


def hwdec():
    """Hardware decoding for the big steps (encoding the whole film and the check)."""
    return {'nvenc': ['-hwaccel', 'cuda'], 'videotoolbox': ['-hwaccel', 'videotoolbox']}.get(ENC['key'], [])


def stats(vals):
    v = sorted(vals)
    return round(sum(v) / len(v), 2), round(v[int(0.05 * (len(v) - 1))], 2)


def video_bytes(pr, size, v=None):
    v = v or next(s for s in pr['streams'] if s.get('codec_type') == 'video')
    t = v.get('tags') or {}
    for k in ('NUMBER_OF_BYTES', 'NUMBER_OF_BYTES-eng'):
        if k in t:
            try: return int(t[k])
            except ValueError: pass
    other = 0; dur = float(pr['format'].get('duration') or 0)
    for s in pr['streams']:
        if s.get('codec_type') == 'audio':
            tt = s.get('tags') or {}
            b = s.get('bit_rate') or tt.get('BPS') or tt.get('BPS-eng') or 640000
            try: other += float(b) * dur / 8
            except ValueError: other += 640000 * dur / 8
    return max(size - other, size * 0.5)


# ------------------------------------------------------------------ encode-job
def _suspect(cm, cp, m, p5):
    """A whole-film score that does not fit the sample scores: almost always frames paired wrongly in the measurement
    (a one-frame offset gives VMAF ~85 / p5 ~40 while the picture is fine), not a bad encode."""
    return cm < 80 or cp < 70 or cm < m - 4 or cp < p5 - 15


def whole_film_check(out, src, model, ctx, dur, vi, fps, deint, m, p5, threads=None, nice=False, n_src=None):
    """VMAF of the whole film (every 10th frame). A pairing error can only LOWER the score, never raise it, so when a
    result looks wrong it is measured again with another pairing method and the best result counts.
    Order: by frame number (when both files have exactly the same frames), by timestamp, at a fixed frame rate."""
    n_src, n_out = (frame_count(src, vi) if n_src is None else n_src), frame_count(out, vi)
    same = bool(n_src) and n_src == n_out
    if not same:
        log(f'  frame count differs (source {n_src}, new {n_out}): pairing frames by timestamp')
    methods = (['index'] if same else []) + ['pts'] + (['fps'] if fps else [])
    best = None
    for i, pair in enumerate(methods):
        label = 'checking VMAF whole film' if i == 0 else f'checking VMAF (measurement {i + 1})'
        fv = vmaf_vals(out, src, model, sub=10, hw=(i == 0), ctx=ctx, dur=dur, p0=70, p1=90, label=label, vi=vi,
                       pair=pair, fps=fps, refvf=deint, frames=True, threads=threads, nice=nice)
        cm, cp = stats([x for _, x in fv])
        log(f'  whole-film check by {pair}: VMAF {cm} (p5 {cp})')
        if best is None or (cp, cm) > (best[1], best[0]): best = (cm, cp, pair, fv)
        if not _suspect(best[0], best[1], m, p5): break
        if i + 1 < len(methods):
            log(f'  {best[0]}/{best[1]} does not fit the samples ({m}/{p5}) - measuring again with other frame pairing')
    cm, cp, pair, fv = best
    if cm < 80 or cp < 70:
        spots = ', '.join(low_spots(fv, fps)) or '-'
        raise FinalError(f'whole-film VMAF {cm}/{cp} not plausible with every pairing method (samples {m}/{p5}); '
                         f'low at {spots}. Original kept; check the source there')
    return cm, cp, pair


def encode_job(job, cpu=False):
    """cpu=True: the x265 (tune grain) slot for films the GPU encoder cannot shrink while keeping the quality."""
    ctx = current.get('cpu' if cpu else 'encode') or JobCtx(job); st = job['settings']     # same ctx as the slot: local status shows progress
    key = 'x265-grain' if cpu else ENC['key']
    # VMAF threads: the CPU job uses its own pool; the GPU job steps back while a CPU encode runs, so the two don't
    # oversubscribe every core (100% busy but barely progressing)
    vt = lambda: CPU['pools'] if cpu else (max(2, THREADS // 4) if CPU['busy'] else THREADS)
    rel = job['rel']; src_nas = nas(rel)
    min_mean, min_p5, min_pct = st['min_mean'], st['min_p5'], st['min_saving_pct']
    jdir = os.path.join(WORK, f'job{job["id"]}'); need = 0
    try:
        os.makedirs(jdir, exist_ok=True)
        size = os.path.getsize(src_nas); want = size * 2.2
        waited = 0
        while True:          # source copy + output + headroom, minus what the other slot's job still needs
            with RESERVED['lock']:
                if shutil.disk_usage(WORK).free - RESERVED['bytes'] >= want + 20e9:
                    RESERVED['bytes'] += want; need = want; break      # 'need' is only what this job really added
            if waited >= 3600:
                raise RuntimeError(f'not enough free space on the PC for this film ({(want + 20e9) / 1e9:.0f} GB needed in {WORK})')
            ctx.report(0, 'waiting for free space on the PC', force=True); time.sleep(120); waited += 120
        if cpu:
            CPU['preset'] = st.get('cpu_preset') if st.get('cpu_preset') in ('medium', 'slow') else 'medium'; CPU['busy'] = True
            log(f"  CPU encode: x265 {CPU['preset']}, tune grain, {CPU['pools']} threads")
        src = os.path.join(jdir, 'source' + os.path.splitext(rel)[1])
        copy_progress(src_nas, src, ctx, 0, 8, 'copying NAS → PC')
        pr = probe(src)
        vids = [s for s in pr['streams'] if s.get('codec_type') == 'video']
        v = next((s for s in vids if not (s.get('disposition') or {}).get('attached_pic')), None)
        if v is None: raise FinalError('no video stream (only cover art?)')
        vi = vids.index(v)                                  # same order in the new file (-map 0)
        if any('DOVI' in (sd.get('side_data_type') or '') for sd in v.get('side_data_list') or []):
            raise FinalError('Dolby Vision: not re-encoded (the DV layer would be lost)')
        # MP4 text subtitles (mov_text) can't go into .mkv as they are: convert them to SRT, keeping every track
        sub_fix = []
        for i, s_ in enumerate(x for x in pr['streams'] if x.get('codec_type') == 'subtitle'):
            if s_.get('codec_name') == 'mov_text': sub_fix += [f'-c:s:{i}', 'srt']
        try:     # can every stream go into .mkv? Find out now, not after hours of searching and encoding
            run_ff(['-t', '2', '-i', src, '-map', '0', '-c', 'copy'] + sub_fix + ['-f', 'matroska', os.path.join(jdir, 'trial.mkv')])
        except RuntimeError as e:
            raise FinalError(f'a stream cannot be copied into .mkv: {str(e)[-300:]}')
        fps = v.get('avg_frame_rate') if v.get('avg_frame_rate') not in (None, '0/0') else v.get('r_frame_rate')
        deint = 'bwdif=mode=send_frame' if v.get('field_order') in ('tt', 'bb', 'tb', 'bt') else None
        if deint: log('  source is interlaced: deinterlacing (bwdif)')
        dur = float(pr['format']['duration'])
        # floor: never less than this share of the original video bitrate (against grain/flat dark scenes
        # that VMAF rates too favourably)
        src_vbps = video_bytes(pr, size, v) * 8 / dur if dur else 0
        rel_bps = src_vbps * float(st.get('min_keep_pct') or 0) / 100
        abs_bps = float(st.get('min_vbr_bps') or 0)
        floor_bps = min(max(rel_bps, abs_bps), src_vbps * 0.9)      # never higher than (almost) the source itself
        if floor_bps: log(f'  bitrate floor {floor_bps/1e6:.1f} Mbps (minimum {abs_bps/1e6:.1f} Mbps, '
                          f'{st.get("min_keep_pct")}% of {src_vbps/1e6:.1f} Mbps = {rel_bps/1e6:.1f})')
        # can the film still reach the minimum saving with that floor? If not: stop right away (saves the whole search)
        if floor_bps:
            best_case = 100 * (1 - (size - video_bytes(pr, size, v) + floor_bps * dur / 8) / size)
            if best_case < min_pct:
                return 'nogain', {'reason': 'too little gain', 'predicted_pct': round(best_case, 1), 'src_size': size,
                                  'floor_kbps': round(floor_bps / 1000)}, f'with the minimum bitrate at most {best_case:.0f}% smaller'
        is4k = (v.get('width') or 0) >= 3200
        model = 'vmaf_4k_v0.6.1' if is4k else 'vmaf_v0.6.1'
        seg = 8.0 if is4k else 10.0
        # reference segments (lossless) -> exact frame comparison
        refs = []
        for k in range(8):
            t = dur * (0.08 + 0.11 * k)
            ref = os.path.join(jdir, f'ref{k}.mkv')
            ctx.report(8 + k * 0.25, 'making references', f'{k+1}/8')
            run_ff(['-ss', ts(t), '-t', ts(seg), '-i', src, '-map', f'0:v:{vi}', '-an', '-sn', '-dn'] + (['-vf', deint] if deint else []) +
                   ['-c:v', 'ffv1', '-level', '3', '-slices', '24', '-g', '1', ref])
            refs.append(ref)
        # self-test: reference against itself. Should be high; with animation or heavy noise VMAF naturally
        # does not reach 100 (97-99 is normal). A misaligned measurement gives scores around 30-85, caught here.
        mean, _ = stats(vmaf_vals(refs[0], refs[0], model, threads=vt(), nice=cpu))
        if mean < 95: raise RuntimeError(f'self-test measurement unreliable (VMAF {mean})')
        log(f'  self-test VMAF {mean}')

        def measure(cq):
            allv = []; bytes_ = 0
            for k, ref in enumerate(refs):
                ctx.report(10 + min(20, len(tries) * 3.5), f'searching quality (cq {cq})', f'segment {k+1}/8')
                smp = os.path.join(jdir, f'smp{k}.mkv')
                pre, out = venc(cq, v, key=key)
                run_ff(pre + ['-i', ref, '-map', '0:v:0'] + out + [smp], nice=cpu)
                bytes_ += os.path.getsize(smp)
                allv += vmaf_vals(smp, ref, model, threads=vt(), nice=cpu)
                os.remove(smp)
            m, p5 = stats(allv)   # same method as the test: all frames together
            return m, p5, bytes_ * 8 / (len(refs) * seg)

        tries = []; best = None
        lo, hi = ENCODERS[key]['range']
        while lo <= hi:
            mid = (lo + hi) // 2
            m, p5, bps = measure(mid)
            ok = m >= min_mean and p5 >= min_p5 and bps >= floor_bps
            tries.append({'cq': mid, 'mean': m, 'p5': p5, 'kbps': round(bps / 1000)})
            why = 'OK' if ok else ('below the bitrate floor' if m >= min_mean and p5 >= min_p5 else 'too low')
            log(f'  cq {mid}: VMAF {m} (p5 {p5}) ~{bps/1e6:.1f} Mbps {why}')
            if ok: best = (mid, m, p5, bps); lo = mid + 1
            else:
                if (m < min_mean or p5 < min_p5) and src_vbps and bps >= src_vbps:
                    # already more bits than the source and still not good enough: a lower cq only makes it bigger
                    return 'nogain', {'reason': 'needs more bitrate than the source', 'search': tries, 'src_size': size}, \
                        'keeping the quality would need more bitrate than the source has'
                hi = mid - 1
        if not best:
            return 'nogain', {'reason': 'no setting meets the quality target', 'search': tries, 'src_size': size}, 'quality target not reachable'
        cq, m, p5, bps = best
        vb = video_bytes(pr, size, v)
        predicted = size - vb + bps * dur / 8
        pred_pct = 100 * (1 - predicted / size)
        if pred_pct < min_pct:
            return 'nogain', {'reason': 'too little gain', 'predicted_pct': round(pred_pct, 1), 'cq': cq, 'search': tries, 'src_size': size}, \
                (f'expected only {pred_pct:.0f}% smaller' if pred_pct >= 0 else f'would become {-pred_pct:.0f}% larger')
        out = os.path.join(jdir, 'out.mkv')
        lo = ENCODERS[key]['range'][0]
        prev_p5 = None
        for step in range(3):                       # target just missed over the whole film: at most 2x one step better
            t0 = time.time()
            pre, out_args = venc(cq, v, deint, vi, key)
            p0, p1 = 30 + step * 3, 70
            run_ff(pre + hwdec() + ['-i', src, '-map', '0', '-map_metadata', '0', '-map_chapters', '0', '-c', 'copy'] + sub_fix + out_args +
                   ['-max_muxing_queue_size', '4096', '-metadata', f'MEDIASCAN=hevc cq{cq} vmaf{m}'] + stat_tags_off(vi) + ['-y', out], ctx, dur, p0, p1, f'encoding (cq {cq})', nice=cpu)
            enc_min = round((time.time() - t0) / 60, 1)
            po = probe(out)
            if abs(float(po['format']['duration']) - dur) > 1.5 or len(po['streams']) != len(pr['streams']):
                raise RuntimeError('check: duration or number of streams differs')
            chk_m, chk_p5, check_pairing = whole_film_check(out, src, model, ctx, dur, vi, fps, deint, m, p5, threads=vt(), nice=cpu)
            log(f'  whole-film check cq {cq}: VMAF {chk_m} (p5 {chk_p5})')
            if chk_m >= min_mean and chk_p5 >= min_p5:
                break
            if step == 2 or cq <= lo:
                break
            # only retry if the gap is small and one step better really helps (otherwise it costs hours for nothing)
            short_m, short_p5 = min_mean - chk_m, min_p5 - chk_p5
            if short_m > 0.4 or short_p5 > 0.7:
                log(f'  whole film too far below target ({chk_m}/{chk_p5}) - not retrying'); break
            if prev_p5 is not None and chk_p5 - prev_p5 < 0.15:
                log('  one step better barely helped - not retrying again'); break
            prev_p5 = chk_p5
            cq -= 1
            log(f'  whole film below target ({min_mean}/{min_p5}) - retrying with cq {cq}')
            ctx.report(30, f're-encoding (cq {cq})', 'whole film just below target', force=True)
        out_size = os.path.getsize(out)
        res = {'cq': cq, 'vmaf_mean': chk_m, 'vmaf_p5': chk_p5, 'check_pairing': check_pairing, 'sample_mean': m, 'sample_p5': p5, 'src_size': size, 'out_size': out_size,
               'encode_min': enc_min, 'search': tries, 'encoder': key, 'preset': CPU['preset'] if cpu else None}
        if chk_m < min_mean or chk_p5 < min_p5:
            return 'nogain', {**res, 'reason': 'whole-film check below target'}, f'check VMAF {chk_m}/{chk_p5} too low'
        if 100 * (1 - out_size / size) < min_pct:
            return 'nogain', {**res, 'reason': 'too little gain'}, f'only {100*(1-out_size/size):.0f}% smaller'
        staged_rel = f'_MediaScan/staging/{job["id"]}.mkv'
        os.makedirs(os.path.dirname(nas(staged_rel)), exist_ok=True)
        copy_progress(out, nas(staged_rel), ctx, 90, 100, 'copying PC → NAS')
        res['staged_rel'] = staged_rel
        return 'staged', res, None
    finally:
        if cpu: CPU['busy'] = False
        with RESERVED['lock']: RESERVED['bytes'] = max(0, RESERVED['bytes'] - need)
        rm_tree(jdir)


def rm_tree(path):
    """Remove a job folder; on Windows a just-killed ffmpeg can hold a file for a moment."""
    for attempt in range(6):
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path): return
        time.sleep(5)
    log(f'  could not remove {path} completely (file in use?) - cleaned up at the next start')


# ------------------------------------------------------------------ subtitle job
_subs = {'mod': None, 'osub': None, 'cfg': None}


def subs_job(job):
    ctx = current.get('subs') or JobCtx(job)
    if _subs['mod'] is None:
        sys.path.insert(0, _HERE)
        import subs as S
        orig_log = S.log
        def fwd(msg):
            orig_log(msg)
            c = current.get('subs')
            if c:
                try: c.report(50, 'subtitles', msg.strip()[:200])
                except Cancelled: c.cancelled = True        # raised at the next step boundary (see check)
                except Exception: pass
        def check():
            c = current.get('subs')
            if not c: return
            if getattr(c, 'cancelled', False): raise Cancelled()
            if time.time() - c.last > 30: c.report(c.progress, force=True)     # raises Cancelled when the dashboard cancelled it
        S.log = fwd; S.CHECK = check
        _subs.update(mod=S, cfg=CFG)
    S = _subs['mod']
    if _subs['osub'] is None or _subs['osub'].exhausted:
        o = S.OpenSubs(_subs['cfg'])
        if o.key and o.user and o.pw:
            try: o.login()
            except Exception as e:                 # site down or wrong details: Whisper/translation still work
                log(f'  OpenSubtitles sign-in failed ({e}) - searching skipped for now'); o.disabled = True
        else:
            if _subs['osub'] is None: log('  no OpenSubtitles details in worker.ini - subtitles are made locally only')
            o.disabled = True
        _subs['osub'] = o
    ctx.report(5, 'subtitles', 'start', force=True)
    want = [l.strip() for l in job['settings']['langs'].split(',') if l.strip()]
    report = []
    S.process(nas(job['rel']), _subs['cfg'], _subs['osub'], None, want, report, rel=job['rel'])
    made = [{'lang': r['lang'], 'source': r['source'], 'score': r['score']} for r in report]
    if _subs['osub'].exhausted:
        return 'limit', {'made': made}
    return 'done', {'made': made}


# ------------------------------------------------------------------ comparison (screenshots original/new)
TONEMAP = ('zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,'
           'zscale=t=bt709:m=bt709:r=tv,format=yuv420p')


def _main_video(path):
    pr = probe(path)
    vids = [x for x in pr['streams'] if x.get('codec_type') == 'video']
    v = next(x for x in vids if not (x.get('disposition') or {}).get('attached_pic'))
    return pr, vids.index(v), v


def _grab(path, vi, t, out, hdr):
    base_vf = "scale='min(1920,iw)':-2"
    for vf in ([TONEMAP + ',' + base_vf] if hdr else []) + [base_vf]:
        r = subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-ss', ts(t), '-i', path, '-map', f'0:v:{vi}',
                            '-frames:v', '1', '-vf', vf, '-q:v', '3', out], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=300,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 0:
            return vf != base_vf
    raise RuntimeError(f'cannot grab a frame at {t:.0f}s: {(r.stderr or "").strip()[-200:]}')


CMP_FIXED = ((0.12, 'fixed'), (0.35, 'fixed'), (0.58, 'fixed'), (0.81, 'fixed'))     # fallback when the packets cannot be read
CMP_PROBE_SEC = 2.0            # how much video is measured around every candidate


def _pick_times(path, vi, dur):
    """Choose 4 moments to compare: the busiest scene (largest video packets: motion/detail), the quietest (smallest:
    dark/static) among 9 candidates at 10%..90%, plus the quarter and three-quarter marks. One ffprobe call reads
    about 2 s of packets at every candidate; when that fails the old fixed fractions are used."""
    if dur < 60: return [(dur * f, why) for f, why in CMP_FIXED]
    cands = [dur * k / 10 for k in range(1, 10)]
    try:
        r = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', f'v:{vi}', '-show_entries', 'packet=pts_time,dts_time,size',
                            '-read_intervals', ','.join(f'{ts(t)}%+{CMP_PROBE_SEC:g}' for t in cands), '-of', 'json', path],
                           capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=12,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if r.returncode: raise RuntimeError((r.stderr or '').strip()[-200:])
        sizes = {t: [] for t in cands}
        for pk in json.loads(r.stdout).get('packets', []):
            try: pt = float(pk.get('pts_time') or pk.get('dts_time')); sz = int(pk.get('size') or 0)
            except (TypeError, ValueError): continue
            for t in cands:
                if t - 0.5 <= pt <= t + CMP_PROBE_SEC + 0.5: sizes[t].append(sz); break
        avg = {t: sum(v) / len(v) for t, v in sizes.items() if v}
        if len(avg) < 4: raise RuntimeError(f'packets read for only {len(avg)} of {len(cands)} moments')
    except Exception as e:
        log(f'  frame choice by packet size not possible ({str(e)[:120]}) - using fixed moments')
        return [(dur * f, why) for f, why in CMP_FIXED]
    picks = [(max(avg, key=avg.get), 'motion'), (min(avg, key=avg.get), 'dark'), (dur * 0.25, 'quarter'), (dur * 0.75, 'three-quarters')]
    out = []
    for t, why in picks + [(dur * f, why) for f, why in CMP_FIXED]:     # fixed moments fill up when picks coincide
        if len(out) >= 4: break
        if all(abs(t - u) > 5 for u, _ in out): out.append((t, why))
    return sorted(out)


def compare_job(c):
    orig, new, outdir = nas(c['orig_rel']), nas(c['new_rel']), nas(c['out_rel'])
    os.makedirs(outdir, exist_ok=True)
    po, vo, _ = _main_video(orig); pn, vn, v = _main_video(new)
    dur = min(float(po['format']['duration']), float(pn['format']['duration']))
    hdr = bool(c.get('hdr')) or v.get('color_transfer') in ('smpte2084', 'arib-std-b67')
    shots, tm = [], False
    for k, (t, why) in enumerate(_pick_times(orig, vo, dur)):
        a = os.path.join(outdir, f'shot{k}_orig.jpg'); b = os.path.join(outdir, f'shot{k}_new.jpg')
        tm = _grab(orig, vo, t, a, hdr) or tm
        _grab(new, vn, t, b, hdr)
        shots.append({'t': round(t, 1), 'why': why, 'orig': os.path.basename(a), 'new': os.path.basename(b),
                      'orig_kb': os.path.getsize(a) // 1024, 'new_kb': os.path.getsize(b) // 1024})
    return {'shots': shots, 'tonemapped': tm, 'orig_size': os.path.getsize(orig), 'new_size': os.path.getsize(new),
            'orig_codec': _main_video(orig)[2].get('codec_name'), 'width': v.get('width'), 'height': v.get('height')}


def compare_loop():
    """Light task alongside the regular work: only grab a few frames from both files."""
    while True:
        if DRAIN['on']: time.sleep(15); continue
        try:
            c = call('/api/worker/compare-next', {'name': NAME}, tries=3)
        except Exception:
            c = None
        if not c:
            time.sleep(15); continue
        log(f'making comparison: {c["new_rel"]}')
        _busy['n'] += 1
        try:
            res = compare_job(c)
            call('/api/worker/compare-done', {'file_id': c['file_id'], 'ok': True, 'result': res})
            log('  comparison done')
        except Exception as e:
            log(f'  comparison failed: {e}')
            try: call('/api/worker/compare-done', {'file_id': c['file_id'], 'ok': False, 'message': str(e)[:400]})
            except Exception: pass
        finally:
            _busy['n'] -= 1


# ------------------------------------------------------------------ DVD / Blu-ray -> one .mkv (everything copied 1:1)
class _NoCtx:
    def report(self, *a, **k): pass


# Steps when converting a DVD/Blu-ray, with their share of the total progress
DISC_STEPS = [(0, 2), (2, 88), (88, 89), (89, 99)]   # 0 find the film, 1 convert, 2 check, 3 to the NAS (4 = replace, server)


class DiscCtx:
    """Progress of a conversion to the dashboard; stops when the conversion was reset there."""
    def __init__(self, c):
        self.id = c['id']; self.step = 0; self.t0 = time.time(); self.last = 0
    def begin(self, step, stage, note=None):
        self.step = step; self.t0 = time.time()
        self._send(DISC_STEPS[step][0], stage, '', None, note)
    def note(self, text):            # short outcome of the current step (e.g. 'title 6 · 93 min')
        self._send(None, None, None, None, text)
    def report(self, progress, stage=None, message=None, force=False):
        now = time.time()
        if not force and now - self.last < 3: return
        p0, p1 = DISC_STEPS[min(self.step, 3)]
        f = (progress - p0) / (p1 - p0) if p1 > p0 else 0
        eta = round((now - self.t0) * (1 - f) / f) if 0.02 < f < 1 and now - self.t0 > 10 else None
        self._send(progress, stage, message, eta)
    def _send(self, progress, stage, message, eta, note=None):
        self.last = time.time()
        b = {'id': self.id, 'step': self.step, 'eta': eta}
        if progress is not None: b['progress'] = round(progress, 1)
        if stage is not None: b['stage'] = stage
        if message is not None: b['message'] = message
        if note: b['note'] = note
        try: r = call('/api/worker/remux-progress', b)
        except Exception: return
        if r is not None and not r.get('continue', True): raise Cancelled()


def _dvd_main_title(path, ctx=_NoCtx()):
    """Longest title on the DVD = the film (menus, trailers and extras are shorter)."""
    best, misses = (0, 0.0), 0
    for t in range(1, 100):
        try:
            d = float(_ffprobe_json(['-f', 'dvdvideo', '-title', str(t), path])['format'].get('duration') or 0)
            misses = 0
            if d > best[1]: best = (t, d)
            ctx.report(min(1.9, 0.1 * t), 'finding the film', f'title {t} checked ({d/60:.0f} min) · longest so far: title {best[0]}', force=True)
        except Exception:
            misses += 1
            if misses >= 2: break
    if not best[0]: raise RuntimeError('no titles found on the DVD')
    return best


# Dolby Vision Blu-ray: helper tools (dovi_tool + mkvmerge). On Windows/Linux the worker downloads them itself.
# (url, sha256): a download that does not match exactly is not used
DOVI_URL = {'nt': ('https://github.com/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-x86_64-pc-windows-msvc.zip',
                   '0d9733d311dfe49f9dca0c350f7e6d978182673d333c6f07f7365daf9e6c81eb'),
            'linux': ('https://github.com/quietvoid/dovi_tool/releases/download/2.3.4/dovi_tool-2.3.4-x86_64-unknown-linux-musl.tar.gz',
                      '1844258e13c26607b32224bf1fa82b595d3b35949f5467405fda560daad32b3f')}
MKVTN_URL = ('https://mkvtoolnix.download/windows/releases/99.0/mkvtoolnix-64-bit-99.0.zip',
             '9929554403e1ed920baa708a0f77967816f5f1c02de0b44ae320d6f8c367e876')
TOOLS = os.path.join(LOCAL, 'tools')


def _tool(name, ctx=None):
    exe = name + EXE
    p = shutil.which(name) or next(iter(glob.glob(os.path.join(TOOLS, '**', exe), recursive=True)), None)
    if not p and IS_MAC:
        p = next((x for x in (f'/opt/homebrew/bin/{name}', f'/usr/local/bin/{name}') if os.path.exists(x)), None)
    if p: return p
    src = DOVI_URL.get('nt' if os.name == 'nt' else sys.platform if sys.platform == 'linux' else '') if name == 'dovi_tool' \
        else (MKVTN_URL if os.name == 'nt' else None)
    if not src:
        raise RuntimeError(f'{name} is missing; install it (Mac: brew install {"mkvtoolnix" if name == "mkvmerge" else "dovi_tool"})')
    if ctx: ctx.report(ctx_p0(ctx), None, f'downloading {name} (one-time)', force=True)
    url, want = src
    log(f'  downloading {name}: {url}')
    os.makedirs(TOOLS, exist_ok=True); arc = os.path.join(TOOLS, os.path.basename(url))
    req = urllib.request.Request(url, headers={'User-Agent': 'MediaScan-worker'})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=300) as r, open(arc + '.part', 'wb') as f:
        while True:
            b = r.read(1 << 20)
            if not b: break
            h.update(b); f.write(b)
    if h.hexdigest() != want:
        os.remove(arc + '.part'); raise RuntimeError(f'{name}: download does not match (sha256) - not used')
    os.replace(arc + '.part', arc)
    dest = os.path.join(TOOLS, name)
    if arc.endswith('.zip'):
        import zipfile
        with zipfile.ZipFile(arc) as z:
            for m in z.namelist():
                if m.startswith(('/', '\\')) or '..' in m.replace('\\', '/').split('/'): raise RuntimeError(f'{name}: suspicious path in zip')
            z.extractall(dest)
    else:
        import tarfile
        with tarfile.open(arc) as t:
            for m in t.getmembers():
                if m.name.startswith('/') or '..' in m.name.split('/') or m.issym() or m.islnk():
                    raise RuntimeError(f'{name}: suspicious path in archive')
            t.extractall(dest)
    os.remove(arc)
    p = next(iter(glob.glob(os.path.join(dest, '**', exe), recursive=True)), None)
    if not p: raise RuntimeError(f'{name} not found after downloading')
    if os.name != 'nt': os.chmod(p, 0o755)
    return p


def ctx_p0(ctx): return DISC_STEPS[min(ctx.step, 3)][0]


def _run_tool(cmd, ctx=None, p0=0, p1=0, label=None):
    """Run dovi_tool / mkvmerge; mkvmerge reports progress as '#GUI#progress 45%'."""
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                         creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    tail = []
    try:
        for line in p.stdout:
            line = line.strip(); tail = (tail + [line])[-15:]
            if ctx and line.startswith('#GUI#progress'):
                try: f = int(line.split()[-1].rstrip('%')) / 100
                except ValueError: continue
                ctx.report(p0 + (p1 - p0) * f, label, f'{f*100:.0f}%')
        p.wait()
    except Cancelled:
        p.kill(); p.wait(timeout=15); raise
    if p.returncode not in (0, 1):            # mkvmerge: 1 = warnings only
        raise RuntimeError(f'{os.path.basename(cmd[0])}: ' + ' | '.join(tail)[-500:])


def _bd_dovi(src, c, ctx, pr, dur, vids, local):
    """UHD Blu-ray with Dolby Vision (profile 7: base layer + enhancement layer) -> one mkv with DV profile 8.1.
    The base layer (HDR10) stays 1:1; the DV metadata (RPU) moves from the enhancement layer to the base layer.
    That way Plex/TV plays it as Dolby Vision, and players without DV as regular HDR10."""
    dovi = _tool('dovi_tool', ctx); mkvmerge = _tool('mkvmerge', ctx)
    # base layer = highest resolution, enhancement layer = the other hevc stream
    hv = [x for x in vids if x.get('codec_name') == 'hevc']
    hv.sort(key=lambda x: -(x.get('width') or 0) * (x.get('height') or 0))
    bl = vids.index(hv[0]); el = vids.index(hv[1]) if len(hv) > 1 else None
    film = float(pr['format'].get('size') or 0) or float(pr['format'].get('bit_rate') or 0) / 8 * dur or \
        sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(src) for f in fs if f.lower().endswith('.m2ts'))
    free = shutil.disk_usage(local).free            # peak: base layer + copy with DV metadata + audio = ~2x the film
    if free < film * 2.1:
        raise RuntimeError(f'not enough space in the work folder: {free/1e9:.0f} GB free, ~{film*2.1/1e9:.0f} GB needed')
    fr = hv[0].get('r_frame_rate') or '24000/1001'
    blf, elf, rest = (os.path.join(local, n) for n in ('bl.hevc', 'el.hevc', 'rest.mkv'))
    # read the disc once: base layer, enhancement layer and the rest (audio, subtitles, chapters)
    args = ['-i', 'bluray:' + src, '-map', f'0:v:{bl}', '-c', 'copy', '-f', 'hevc', blf]
    if el is not None: args += ['-map', f'0:v:{el}', '-c', 'copy', '-f', 'hevc', elf]
    args += ['-map', '0', '-map', '-0:v', '-map_chapters', '0', '-c', 'copy', rest]
    run_ff(args, ctx, dur, 2, 60, 'reading the disc (DV)')
    rpu = os.path.join(local, 'rpu.bin'); bldv = os.path.join(local, 'bl_dv.hevc')
    ctx.report(60, 'converting Dolby Vision to profile 8.1', 'extracting metadata from the enhancement layer', force=True)
    _run_tool([dovi, '-m', '2', 'extract-rpu', elf if el is not None else blf, '-o', rpu])
    if el is not None: os.remove(elf)
    ctx.report(64, 'converting Dolby Vision to profile 8.1', 'injecting metadata into the base layer', force=True)
    _run_tool([dovi, 'inject-rpu', '-i', blf, '--rpu-in', rpu, '-o', bldv])
    os.remove(blf)
    out = os.path.join(local, 'film.mkv')
    _run_tool([mkvmerge, '--gui-mode', '-o', out, '--default-duration', f'0:{fr}p', bldv, rest], ctx, 72, 88, 'merging into mkv')
    os.remove(bldv); os.remove(rest)
    return out, {'playlist': 'longest', 'dolby_vision': '8.1 (from profile 7)' if el is not None else '8.1'}


def remux_job(c):
    src = nas(c['src_rel']); local = os.path.join(WORK, f"disc{c['id']}"); os.makedirs(local, exist_ok=True)
    out = os.path.join(local, 'film.mkv'); ctx = DiscCtx(c)
    try:
        ctx.begin(0, 'finding the film on the disc')
        if c['kind'] == 'dvd':
            title, dur = _dvd_main_title(src, ctx)
            log(f'  dvd: title {title} is the movie ({dur/60:.0f} min)')
            ctx.note(f'title {title} · {dur/60:.0f} min')
            inp = ['-f', 'dvdvideo', '-title', str(title), '-i', src]
            info = {'title': title}
        else:
            inp = ['-i', 'bluray:' + src]                 # without -playlist libbluray picks the longest one (the film)
            pr = _ffprobe_json(['bluray:' + src]); dur = float(pr['format'].get('duration') or 0)
            vids = [x for x in pr['streams'] if x.get('codec_type') == 'video']
            dv = any(any('DOVI' in str(sd.get('side_data_type', '')).upper() or 'dolby vision' in str(sd).lower()
                         for sd in (x.get('side_data_list') or [])) for x in vids)
            dv = dv or sum(1 for x in vids if x.get('codec_name') == 'hevc') > 1
            info = {'playlist': 'longest'}
            ctx.note(f'longest playlist · {dur/60:.0f} min' + (' · Dolby Vision' if dv else ''))
        ctx.begin(1, 'converting to mkv')
        if c['kind'] != 'dvd' and dv:
            out, extra = _bd_dovi(src, c, ctx, pr, dur, vids, local); info.update(extra)
        else:
            run_ff(inp + ['-map', '0', '-map_chapters', '0', '-c', 'copy', '-y', out], ctx, dur, *DISC_STEPS[1], 'converting to mkv')
        ctx.note(f'{os.path.getsize(out)/1e9:.1f} GB · {(time.time()-ctx.t0)/60:.0f} min')
        ctx.begin(2, 'checking')
        po = _ffprobe_json([out]); odur = float(po['format'].get('duration') or 0)
        if dur and odur < dur * 0.97:
            raise RuntimeError(f'new mkv is too short ({odur/60:.0f} of {dur/60:.0f} min)')
        if info.get('dolby_vision'):
            vd = next((x for x in po['streams'] if x.get('codec_type') == 'video'), {})
            if not any('DOVI' in str(sd.get('side_data_type', '')).upper() and int(sd.get('dv_profile') or 0) == 8
                       for sd in (vd.get('side_data_list') or [])):
                raise RuntimeError('Dolby Vision is missing from the new mkv')
        info.update(duration=round(odur), streams=[{'type': x.get('codec_type'), 'codec': x.get('codec_name'),
                                                    'lang': (x.get('tags') or {}).get('language')} for x in po['streams']])
        na = sum(1 for x in po['streams'] if x.get('codec_type') == 'audio'); ns = sum(1 for x in po['streams'] if x.get('codec_type') == 'subtitle')
        ctx.note(f'{odur/60:.0f} min · {na} audio · {ns} subtitles · {len(po.get("chapters") or [])} chapters' if 'chapters' in po
                 else f'{odur/60:.0f} min · {na} audio · {ns} subtitles')
        ctx.begin(3, 'copying to the NAS')
        os.makedirs(os.path.dirname(nas(c['out_rel'])), exist_ok=True)
        copy_progress(out, nas(c['out_rel']), ctx, *DISC_STEPS[3], 'copying to the NAS')
        ctx.report(DISC_STEPS[3][1], 'done, waiting to replace', '', force=True)
        return info, None
    finally:
        shutil.rmtree(local, ignore_errors=True)


def remux_loop():
    while True:
        if DRAIN['on']: time.sleep(15); continue
        try:
            c = call('/api/worker/remux-next', {'name': NAME}, tries=3)
        except Exception:
            c = None
        if not c:
            time.sleep(20); continue
        log(f"converting dvd/blu-ray: {c['folder']}")
        _busy['n'] += 1
        try:
            info, skip = remux_job(c)
            if skip:
                call('/api/worker/remux-done', {'id': c['id'], 'ok': False, 'skip': True, 'message': skip}); log('  ' + skip)
            else:
                call('/api/worker/remux-done', {'id': c['id'], 'ok': True, 'result': info}); log('  converted to mkv')
        except Cancelled:
            log('  conversion stopped from the dashboard')
        except Exception as e:
            log(f'  conversion failed: {e}')
            try: call('/api/worker/remux-done', {'id': c['id'], 'ok': False, 'message': str(e)[:400]})
            except Exception: pass
        finally:
            _busy['n'] -= 1


# ------------------------------------------------------------------ loops
def slot_loop(kind, fn):
    is_enc = kind in ('encode', 'cpu')
    while True:
        # draining for an update: take no new jobs - except that the GPU/subtitle slots keep going while a CPU encode
        # (hours) still blocks the restart anyway
        if DRAIN['on'] and (kind == 'cpu' or not current['cpu']): time.sleep(15); continue
        try:
            job = call('/api/worker/next', {'name': NAME, 'kind': 'encode' if is_enc else kind, 'max_height': MAX_HEIGHT, 'cpu': kind == 'cpu'})
        except Exception as e:
            log(f'{kind}: cannot fetch a job ({e})'); time.sleep(60); continue
        if not job:
            time.sleep(10); continue          # respond quickly when something is picked in the dashboard
        if DRAIN['on'] and (kind == 'cpu' or not current['cpu']):        # the update check won while we asked: hand it back
            try: call('/api/worker/fail', {'name': NAME, 'job_id': job['id'], 'message': 'worker restarting for an update', 'retry_after': 60}, tries=3)
            except Exception: pass
            continue
        log(f'{kind} start: {job.get("rel", "?")}')
        current[kind] = JobCtx(job); local_status()
        try:
            if is_enc:
                outcome, res, msg = fn(job)
                r = call('/api/worker/done', {'name': NAME, 'job_id': job['id'], 'outcome': outcome, 'result': res, 'message': msg}, tries=FINAL_TRIES)
                if outcome == 'staged' and r is not None and not r.get('ok', True):
                    try: os.remove(nas(res['staged_rel']))      # job cancelled in the meantime: leave nothing behind
                    except OSError: pass
                log(f'encode done ({outcome}): {job["rel"]} {msg or ""}')
            else:
                outcome, res = fn(job)
                if outcome == 'limit':
                    if res['made']: call('/api/worker/done', {'name': NAME, 'job_id': job['id'], 'result': res, 'message': 'partly; rest after download limit'}, tries=FINAL_TRIES)
                    else: call('/api/worker/fail', {'name': NAME, 'job_id': job['id'], 'message': 'OpenSubtitles daily limit reached', 'retry_after': 6 * 3600}, tries=FINAL_TRIES)
                else:
                    call('/api/worker/done', {'name': NAME, 'job_id': job['id'], 'result': res}, tries=FINAL_TRIES)
                log(f'subtitles done: {job["rel"]} -> {[m["lang"] for m in res["made"]]}')
        except Cancelled:
            log(f'{kind} cancelled from the dashboard: {job["rel"]}')
        except Exception as e:
            log(f'{kind} ERROR: {e}\n{traceback.format_exc()[-1500:]}')
            try: call('/api/worker/fail', {'name': NAME, 'job_id': job['id'], 'message': str(e)[:900], 'final': isinstance(e, FinalError)}, tries=FINAL_TRIES)
            except Exception: pass
        finally:
            current[kind] = None; local_status()


# ------------------------------------------------------------------ self-update
UPDATABLE = ('worker.py', 'subs.py', 'langs.py') + (('manager.py', 'tray.ps1') if os.name == 'nt' else ())


def update_info():
    """Version info from the server if it has a different worker version, otherwise None."""
    if not AUTO_UPDATE: return None
    try:
        info = call('/api/worker/update', {'version': WORKER_VERSION}, tries=2)
    except Exception as e:
        log(f'update: cannot reach the server ({e})'); return None
    if not info or not info.get('version'): return None
    if info['version'] != WORKER_VERSION: return info
    # same version number, but a fix shipped without a version bump: compare the files themselves
    for name, sha in (info.get('files') or {}).items():
        if name not in UPDATABLE: continue
        try:
            with open(os.path.join(_HERE, name), 'rb') as f:
                if hashlib.sha256(f.read()).hexdigest() != sha: return info
        except OSError:
            continue                                    # not installed on this machine: leave it
    return None


def self_update(info=None):
    """Does the server have a different worker version? Then fetch the files, verify them (sha256) and restart.
    The manager (Windows), systemd (Linux) or launchd (macOS) then starts the worker again automatically."""
    info = info or update_info()
    if not info: return False
    new = {}
    for name, sha in (info.get('files') or {}).items():
        if name not in UPDATABLE: continue
        req = urllib.request.Request(f'{SERVER}/api/worker/file/{name}', headers={'Authorization': 'Bearer ' + TOKEN})
        try:
            with urllib.request.urlopen(req, timeout=60) as r: data = r.read()
        except Exception as e:
            log(f'update postponed: cannot fetch {name} ({e})'); return False
        if hashlib.sha256(data).hexdigest() != sha:
            log(f'update aborted: {name} does not match its checksum'); return False
        new[name] = data
    if 'worker.py' not in new: return False
    for name, data in new.items():
        dst = os.path.join(os.path.dirname(os.path.abspath(__file__)), name)
        try:
            with open(dst, 'rb') as f:
                if f.read() == data: continue          # unchanged: do not touch (otherwise the manager app restarts for nothing)
        except OSError:
            pass
        with open(dst + '.new', 'wb') as f: f.write(data)
        os.replace(dst + '.new', dst)
    log(f"updated from {WORKER_VERSION} to {info['version']}" + (' (changed files)' if info['version'] == WORKER_VERSION else '') +
        ' - restarting the worker')
    return True


_MTIME = {f: os.path.getmtime(os.path.join(_HERE, f)) for f in UPDATABLE if os.path.exists(os.path.join(_HERE, f))}
DRAIN = {'on': False}     # new version ready: take no new jobs, restart after the running ones


def _changed_on_disk():
    return any(os.path.exists(os.path.join(_HERE, f)) and os.path.getmtime(os.path.join(_HERE, f)) != t for f, t in _MTIME.items())


def update_loop():
    """Every 10 minutes, check for a new version (from the server, or placed manually). If so: finish the running
    jobs, take no new ones, then update and restart."""
    info = None
    while True:
        time.sleep(600)
        try:
            if not DRAIN['on']:
                info = update_info()
                if info or _changed_on_disk():
                    DRAIN['on'] = True
                    log(f"new version ready ({info['version'] if info else 'files updated'}) - restarting after the running jobs")
                continue
            if any(current.values()) or _busy['n']: continue
            if info and not self_update(info):
                DRAIN['on'] = False; continue        # update failed: just keep working with this version
            log('restarting for the new version'); os._exit(3)
        except Exception as e:                       # never let this thread die: a dead update loop keeps the worker drained
            log(f'update check failed: {e}'); DRAIN['on'] = False


_busy = {'n': 0}          # comparisons/conversions running now


def main():
    if not SERVER or not TOKEN: raise SystemExit('No [server] url / worker_token in worker.ini - run the installer again')
    log(f'MediaScan worker {WORKER_VERSION} {NAME} -> {SERVER} (media {MEDIA}, {OS_NAME})')
    threading.Thread(target=status_loop, daemon=True).start()
    PHASE['v'] = 'connecting'
    try:
        if self_update(): sys.exit(3)
    except SystemExit:
        raise
    except Exception as e:
        log(f'update check at start failed ({e}) - continuing with this version')
    while not os.path.isdir(MEDIA):
        PHASE['v'] = 'waiting_media'                 # share not mounted (yet): wait instead of stopping
        log(f'media folder {MEDIA} not found - waiting 60 s (check media_root in worker.ini or mount the share)'); time.sleep(60)
    if ENCODE_ENABLED:
        try:
            check_vmaf(); detect_encoder(); CAPS['encode'] = True
        except SystemExit as e:
            log(f'encoding is not possible on this machine: {e}')
    if CPU_ENABLED and CAPS['encode'] and ENC['key'] not in ('x265', 'x265-grain'):
        CAPS['cpu'] = _try_encode('x265-grain', set())[0]        # a second, CPU-only encoder next to the GPU one
    CAPS['subs'] = subs_capable()
    log(f"Can do: {', '.join(k for k, v in CAPS.items() if v) or 'nothing'} (choose what it does in the dashboard)")
    if not any(CAPS.values()): raise SystemExit('This machine cannot run encode or subtitle jobs')
    PHASE['v'] = 'connecting'
    r = call('/api/worker/hello', {'name': NAME, 'version': WORKER_VERSION}, tries=1000)
    LINK.update(ok=True, at=int(time.time())); PHASE['v'] = 'ready'
    if r and r.get('retried'): log(f"{r['retried']} failed job(s) get another chance after this update")
    if r and r.get('requeued'): log(f"{r['requeued']} interrupted job(s) queued again")
    kill_strays()
    shutil.rmtree(WORK, ignore_errors=True); os.makedirs(WORK, exist_ok=True)     # clean up leftovers from a crash
    clean_leftovers()
    threading.Thread(target=heartbeat_loop, daemon=True).start()
    if CAPS['encode']:
        threading.Thread(target=slot_loop, args=('encode', encode_job), daemon=True).start()
    if CAPS['cpu']:
        threading.Thread(target=slot_loop, args=('cpu', lambda j: encode_job(j, cpu=True)), daemon=True).start()
    if CAPS['subs']:
        threading.Thread(target=slot_loop, args=('subs', subs_job), daemon=True).start()
    threading.Thread(target=compare_loop, daemon=True).start()
    if CAPS['encode']: threading.Thread(target=remux_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    while True: time.sleep(3600)


if __name__ == '__main__':
    try:
        main()
    except SystemExit as e:
        if isinstance(e.code, str): log(e.code); sys.exit(1)
        raise
