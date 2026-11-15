#!/usr/bin/env python3
"""Check the dashboard translations in server/app/static/locales/.

en.json is the reference: every other locale must have the same keys ("s", no extra ones), the same number
of rules ("rx", "w") with identical patterns, and the same placeholders ($1, {t:$1}, {w:$1}) in every
template. en.json itself may not contain duplicate patterns.
Run:  python3 tools/check_locales.py            (exit code 1 when something is wrong)
      python3 tools/check_locales.py --stale    also list en.json keys that occur in none of the source
                                               files (a warning: some worker texts are built at runtime)
"""
import json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIR = os.path.join(ROOT, 'server', 'app', 'static', 'locales')
SOURCES = ['server/app/static/app.js', 'server/app/static/index.html', 'server/app/main.py', 'server/app/scanner.py',
           'worker/worker.py', 'worker/subs.py']
PH = re.compile(r'\{[tw]:\$\d\}|\$\d')


def check_en(en):
    errs = []
    for part in ('rx', 'w'):
        seen = set()
        for r in en[part]:
            if r[0] in seen: errs.append(f'{part}: duplicate pattern {r[0]!r}')
            seen.add(r[0])
    return errs


def stale(en):
    src = ''.join(open(os.path.join(ROOT, f), encoding='utf-8').read() for f in SOURCES)
    return [k for k in en['s'] if k not in src]


def main():
    en = json.load(open(os.path.join(DIR, 'en.json'), encoding='utf-8'))
    errs = check_en(en); bad = bool(errs)
    print('en.json: ' + ('ok' if not errs else '; '.join(errs[:5])))
    for f in sorted(os.listdir(DIR)):
        if not f.endswith('.json') or f == 'en.json': continue
        d = json.load(open(os.path.join(DIR, f), encoding='utf-8')); errs = []
        if not d.get('name'): errs.append('missing "name"')
        miss = [k for k in en['s'] if not str(d.get('s', {}).get(k, '')).strip()]
        if miss: errs.append(f'{len(miss)} missing strings, e.g. {miss[0]!r}')
        extra = [k for k in d.get('s', {}) if k not in en['s']]
        if extra: errs.append(f'{len(extra)} keys not in en.json, e.g. {extra[0]!r}')
        for part in ('rx', 'w'):
            a, b = en[part], d.get(part, [])
            if len(a) != len(b): errs.append(f'{part}: {len(b)} rules, expected {len(a)}'); continue
            for i, (x, y) in enumerate(zip(a, b)):
                if x[:2] != y[:2]: errs.append(f'{part}[{i}]: pattern differs from en.json')
                elif sorted(PH.findall(x[2])) != sorted(PH.findall(y[2])): errs.append(f'{part}[{i}]: placeholders differ')
        print(f'{f}: ' + ('ok' if not errs else '; '.join(errs[:5])))
        bad += bool(errs)
    js = open(os.path.join(DIR, '..', 'i18n.js'), encoding='utf-8').read()
    codes = re.findall(r"(\w\w):'", js[js.index('const LANG_UI'):js.index('};', js.index('const LANG_UI'))])
    missing = [c for c in codes if c != 'nl' and not os.path.exists(os.path.join(DIR, c + '.json'))]
    if missing: print('no locale file for: ' + ', '.join(missing)); bad += 1
    if '--stale' in sys.argv:
        old = stale(en)
        print(f'\n{len(old)} en.json key(s) not found literally in the source files (check before removing):')
        for k in old: print('  ' + repr(k))
    sys.exit(1 if bad else 0)


if __name__ == '__main__':
    main()
