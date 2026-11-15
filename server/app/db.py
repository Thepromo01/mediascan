"""SQLite storage. Every thread gets its own connection (WAL): reads never block on writes."""
import sqlite3, threading, time, os, hashlib
from contextlib import contextmanager

DB_PATH = os.environ.get('DB_PATH', '/data/mediascan.db')
_local = threading.local()
_init_lock = threading.Lock()
_initialized = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  id INTEGER PRIMARY KEY, rel TEXT UNIQUE, category TEXT, size INTEGER, mtime REAL,
  codec TEXT, width INTEGER, height INTEGER, hdr INTEGER, dv INTEGER, duration REAL, vbr REAL,
  audio TEXT, subs_have TEXT, ours INTEGER DEFAULT 0, plan TEXT, est_save INTEGER DEFAULT 0,
  first_seen REAL, last_seen REAL, gone INTEGER DEFAULT 0, probe_err TEXT);
CREATE INDEX IF NOT EXISTS ix_files_plan ON files(gone, plan);
CREATE INDEX IF NOT EXISTS ix_files_first ON files(gone, first_seen);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY, file_id INTEGER, kind TEXT, status TEXT, priority INTEGER DEFAULT 0,
  attempts INTEGER DEFAULT 0, worker TEXT, progress REAL DEFAULT 0, stage TEXT, message TEXT,
  created REAL, started REAL, finished REAL, not_before REAL DEFAULT 0, result TEXT,
  src_size INTEGER, out_size INTEGER, cpu INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs(kind, status);
CREATE INDEX IF NOT EXISTS ix_jobs_file ON jobs(file_id);
CREATE INDEX IF NOT EXISTS ix_jobs_finished ON jobs(kind, finished);
CREATE TABLE IF NOT EXISTS quarantine(
  id INTEGER PRIMARY KEY, file_id INTEGER, job_id INTEGER, orig_rel TEXT, new_rel TEXT, qpath TEXT,
  size INTEGER, created REAL, state TEXT DEFAULT 'held');
CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, ts REAL, level TEXT, msg TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS workers(name TEXT PRIMARY KEY, last_seen REAL, info TEXT, cfg TEXT);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, created REAL, mfa INTEGER DEFAULT 0, seen REAL);
CREATE TABLE IF NOT EXISTS auth(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS login_fail(ip TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS discs(id INTEGER PRIMARY KEY, folder TEXT UNIQUE, kind TEXT, size INTEGER, status TEXT, worker TEXT,
  requested REAL, finished REAL, message TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS compares(file_id INTEGER PRIMARY KEY, status TEXT, worker TEXT, requested REAL, finished REAL,
  result TEXT, message TEXT);
CREATE INDEX IF NOT EXISTS ix_quar_job ON quarantine(job_id);
CREATE INDEX IF NOT EXISTS ix_quar_state ON quarantine(state, created);
CREATE INDEX IF NOT EXISTS ix_cmp_status ON compares(status, requested);
CREATE INDEX IF NOT EXISTS ix_jobs_worker ON jobs(worker, status);
CREATE INDEX IF NOT EXISTS ix_fail_ip ON login_fail(ip, ts);
"""

DEFAULTS = {
    'min_mean': '96.5', 'min_p5': '93', 'langs': 'en', 'paused': '1', 'subs_paused': '1',
    'quarantine_days': '7', 'quarantine_max_gb': '500', 'min_free_gb': '100',
    'scan_interval_min': '360', 'min_saving_pct': '10', 'min_keep_pct': '0', 'min_vbr_1080': '5', 'min_vbr_2160': '0',
    'skip_dirs': '#recycle,@eaDir,#snapshot,_MediaScan,Music,Downloads',
    'anim_dirs': 'Anime,Anime Movies,Animation',
    'cpu_fallback': '0', 'cpu_preset': 'medium',
    'cmp_auto': '0', 'cmp_max_mb': '500',          # picture comparison after every replace; cap on the stored frames (0 = no cap)
}

# migrations for older databases
_MIGRATIONS = [
    'ALTER TABLE sessions ADD COLUMN mfa INTEGER DEFAULT 0',
    'ALTER TABLE jobs ADD COLUMN src_size INTEGER',
    'ALTER TABLE jobs ADD COLUMN out_size INTEGER',
    'ALTER TABLE workers ADD COLUMN cfg TEXT',
    'ALTER TABLE sessions ADD COLUMN seen REAL',
    'ALTER TABLE jobs ADD COLUMN cpu INTEGER DEFAULT 0',     # 1 = encode on a worker CPU with x265 (grainy films)
]

_DISC_COLS = 'folder, kind, size, status, worker, requested, finished, message, result'


def _migrate_discs_id(c):
    """discs used to have no INTEGER PRIMARY KEY: the implicit rowid was handed to workers and the dashboard, and rowids are
    not stable (VACUUM may renumber them). Rebuild the table once with an explicit id, keeping the current rowids."""
    if any(r[1] == 'id' for r in c.execute('PRAGMA table_info(discs)')): return
    c.execute('BEGIN')
    try:
        c.execute("""CREATE TABLE discs_new(id INTEGER PRIMARY KEY, folder TEXT UNIQUE, kind TEXT, size INTEGER, status TEXT,
                      worker TEXT, requested REAL, finished REAL, message TEXT, result TEXT)""")
        c.execute(f'INSERT INTO discs_new(id, {_DISC_COLS}) SELECT rowid, {_DISC_COLS} FROM discs')
        c.execute('DROP TABLE discs')
        c.execute('ALTER TABLE discs_new RENAME TO discs')
        c.execute('COMMIT')
    except Exception:
        c.execute('ROLLBACK'); raise


# MediaScan < 1.0 stored Dutch status codes and messages; 1.0 renamed them once (settings mig_codes_en / mig_msgs_en).
# That migration is gone; a database that still has them is reported at start so it is not silently misread.
_OLD_CODES = ('wachtrij', 'bezig', 'klaar', 'mislukt', 'vervangen', 'geen-winst', 'overgeslagen', 'geannuleerd', 'gepland',
              'teruggezet', 'gevonden', 'niets-gevonden', 'nieuw', 'disc-structuur', 'al-efficient', 'kleine-winst', 'onbekend')


def _check_old_codes(c):
    ph = ','.join('?' * len(_OLD_CODES))
    n = sum(c.execute(f'SELECT COUNT(*) FROM {t} WHERE {col} IN ({ph})', _OLD_CODES).fetchone()[0]
            for t, col in (('jobs', 'status'), ('files', 'plan'), ('discs', 'status'), ('compares', 'status'), ('quarantine', 'state')))
    if n:
        c.execute('INSERT INTO events(ts,level,msg) VALUES(?,?,?)',
                  (time.time(), 'error', f'{n} record(s) still use status codes from before 1.0 - start MediaScan 1.0.0 once to convert them'))
        c.commit()


def _open():
    os.makedirs(os.path.dirname(DB_PATH) or '.', exist_ok=True)
    c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA synchronous=NORMAL')
    c.execute('PRAGMA busy_timeout=30000')
    return c


def conn():
    global _initialized
    c = getattr(_local, 'c', None)
    if c is None:
        c = _local.c = _open()
        if not _initialized:
            with _init_lock:
                if not _initialized:
                    c.executescript(SCHEMA)                  # first the tables, then the columns older databases miss
                    for m in _MIGRATIONS:
                        try: c.execute(m)
                        except sqlite3.OperationalError: pass    # column already there
                    _migrate_discs_id(c)
                    for k, v in DEFAULTS.items():
                        c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))
                    c.commit()
                    _check_old_codes(c)
                    _initialized = True
    return c


def q(sql, args=(), one=False):
    rows = conn().execute(sql, args).fetchall()
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    c = conn()
    cur = c.execute(sql, args)
    c.commit()
    return cur.lastrowid


def xn(sql, args=()):
    """Like x(), but returns the number of changed rows."""
    c = conn()
    n = c.execute(sql, args).rowcount
    c.commit()
    return n


def xmany(sql, seq):
    c = conn()
    c.executemany(sql, seq)
    c.commit()


@contextmanager
def tx():
    """Several writes in one transaction (much faster than separate commits)."""
    c = conn()
    try:
        yield c
        c.commit()
    except Exception:
        c.rollback(); raise


def setting(key, cast=str):
    r = q('SELECT value FROM settings WHERE key=?', (key,), one=True)
    v = r['value'] if r else DEFAULTS.get(key)
    if cast is bool: return v in ('1', 'true', 'True')
    return cast(v) if v is not None else None


def set_setting(key, value):
    x('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


_ev_count = 0
def event(msg, level='info'):
    global _ev_count
    x('INSERT INTO events(ts,level,msg) VALUES(?,?,?)', (time.time(), level, msg))
    _ev_count += 1
    if _ev_count % 200 == 0:
        x('DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)')


def auth_get(key):
    r = q('SELECT value FROM auth WHERE key=?', (key,), one=True)
    return r['value'] if r else None


def auth_set(key, value):
    if value is None:
        x('DELETE FROM auth WHERE key=?', (key,))
    else:
        x('INSERT INTO auth(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(value)))


def token_hash(tok):
    return hashlib.sha256(('ms-session:' + (tok or '')).encode()).hexdigest()
