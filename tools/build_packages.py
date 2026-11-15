#!/usr/bin/env python3
"""Build the worker packages and the worker files the server hands out.

Usage:   python3 tools/build_packages.py [target]      (default: server/app/dist)
Makes:   <target>/worker/{worker.py,subs.py,langs.py,manager.py,tray.ps1}  -> for the workers' self-update
         <target>/downloads/*.zip|*.tar.gz             -> install packages (Windows, Linux, macOS)
Also run by the Dockerfile, so every image ships the matching packages.
"""
import io, os, sys, shutil, tarfile, zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
W = os.path.join(ROOT, 'worker')
CORE = {'worker.py': os.path.join(W, 'worker.py'), 'subs.py': os.path.join(W, 'subs.py'),
        'langs.py': os.path.join(ROOT, 'server', 'app', 'langs.py')}
UPDATES = dict(CORE, **{'manager.py': os.path.join(W, 'windows', 'manager.py'),
                        'tray.ps1': os.path.join(W, 'windows', 'tray.ps1')})   # files workers fetch when they update
PACKAGES = {
    'mediascan-worker-windows.zip': ('MediaScan-Worker', 'windows',
                                     ['Install-Worker.bat', 'install-worker.ps1', 'install-manager.ps1', 'manager.py', 'tray.ps1', 'README.txt']),
    'mediascan-worker-linux.tar.gz': ('mediascan-worker', 'linux', ['install.sh', 'README-linux.md']),
    'mediascan-worker-macos.tar.gz': ('mediascan-worker-macos', 'macos', ['install.sh', 'README-macos.md']),
}
FIXED_TIME = (2026, 1, 1, 0, 0, 0)        # fixed time: the same source gives the same package


def files_for(sub, extra):
    out = {n: os.path.join(W, sub, n) for n in extra}
    out.update(CORE)
    return out


def build(dest):
    os.makedirs(os.path.join(dest, 'worker'), exist_ok=True)
    os.makedirs(os.path.join(dest, 'downloads'), exist_ok=True)
    for n, src in UPDATES.items():
        shutil.copyfile(src, os.path.join(dest, 'worker', n))
    for pkg, (top, sub, extra) in PACKAGES.items():
        out = os.path.join(dest, 'downloads', pkg)
        fs = files_for(sub, extra)
        if pkg.endswith('.zip'):
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
                for n, src in sorted(fs.items()):
                    data = open(src, 'rb').read()
                    if n.endswith(('.bat', '.ps1', '.txt')):           # Windows line endings
                        data = data.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')
                    if n.endswith('.ps1') and not data.startswith(b'\xef\xbb\xbf'):
                        data = b'\xef\xbb\xbf' + data                 # PowerShell 5.1 reads UTF-8 correctly only with a BOM
                    z.writestr(zipfile.ZipInfo(f'{top}/{n}', FIXED_TIME), data, zipfile.ZIP_DEFLATED)
        else:
            with tarfile.open(out, 'w:gz') as t:
                for n, src in sorted(fs.items()):
                    data = open(src, 'rb').read()
                    ti = tarfile.TarInfo(f'{top}/{n}'); ti.size = len(data); ti.mtime = 1767225600
                    ti.mode = 0o755 if n.endswith('.sh') else 0o644
                    t.addfile(ti, io.BytesIO(data))
        print(f'{pkg}: {os.path.getsize(out)//1024} KB')


if __name__ == '__main__':
    build(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, 'server', 'app', 'dist'))
