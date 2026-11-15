"""Subtitle languages: ISO 639-1 code -> (ISO 639-2 codes, English name, Dutch name, extra names used in file names).
Shared by the server (scanning) and the worker (searching and translating); keep both copies identical."""

LANGS = {
    'en': (('eng',), 'English', 'Engels', ('english',)),
    'nl': (('dut', 'nld'), 'Dutch', 'Nederlands', ('dutch', 'nederlands', 'vlaams', 'flemish')),
    'de': (('ger', 'deu'), 'German', 'Duits', ('german', 'deutsch')),
    'fr': (('fre', 'fra'), 'French', 'Frans', ('french', 'francais')),
    'es': (('spa',), 'Spanish', 'Spaans', ('spanish', 'espanol', 'castellano')),
    'it': (('ita',), 'Italian', 'Italiaans', ('italian', 'italiano')),
    'pt': (('por',), 'Portuguese', 'Portugees', ('portuguese', 'portugues')),
    'tr': (('tur',), 'Turkish', 'Turks', ('turkish', 'turkce')),
    'ar': (('ara',), 'Arabic', 'Arabisch', ('arabic',)),
    'pl': (('pol',), 'Polish', 'Pools', ('polish', 'polski')),
    'ru': (('rus',), 'Russian', 'Russisch', ('russian',)),
    'uk': (('ukr',), 'Ukrainian', 'Oekraïens', ('ukrainian',)),
    'ro': (('rum', 'ron'), 'Romanian', 'Roemeens', ('romanian',)),
    'cs': (('cze', 'ces'), 'Czech', 'Tsjechisch', ('czech',)),
    'hu': (('hun',), 'Hungarian', 'Hongaars', ('hungarian',)),
    'el': (('gre', 'ell'), 'Greek', 'Grieks', ('greek',)),
    'sv': (('swe',), 'Swedish', 'Zweeds', ('swedish', 'svenska')),
    'da': (('dan',), 'Danish', 'Deens', ('danish', 'dansk')),
    'no': (('nor', 'nob', 'nno'), 'Norwegian', 'Noors', ('norwegian', 'norsk')),
    'fi': (('fin',), 'Finnish', 'Fins', ('finnish', 'suomi')),
    'hr': (('hrv',), 'Croatian', 'Kroatisch', ('croatian',)),
    'sr': (('srp',), 'Serbian', 'Servisch', ('serbian',)),
    'bs': (('bos',), 'Bosnian', 'Bosnisch', ('bosnian',)),
    'bg': (('bul',), 'Bulgarian', 'Bulgaars', ('bulgarian',)),
    'he': (('heb',), 'Hebrew', 'Hebreeuws', ('hebrew',)),
    'fa': (('per', 'fas'), 'Persian', 'Perzisch', ('persian', 'farsi')),
    'hi': (('hin',), 'Hindi', 'Hindi', ('hindi',)),
    'ur': (('urd',), 'Urdu', 'Urdu', ('urdu',)),
    'id': (('ind',), 'Indonesian', 'Indonesisch', ('indonesian',)),
    'ms': (('may', 'msa'), 'Malay', 'Maleis', ('malay',)),
    'th': (('tha',), 'Thai', 'Thai', ('thai',)),
    'vi': (('vie',), 'Vietnamese', 'Vietnamees', ('vietnamese',)),
    'zh': (('chi', 'zho'), 'Chinese', 'Chinees', ('chinese',)),
    'ja': (('jpn',), 'Japanese', 'Japans', ('japanese',)),
    'ko': (('kor',), 'Korean', 'Koreaans', ('korean',)),
    'ku': (('kur',), 'Kurdish', 'Koerdisch', ('kurdish',)),
    'so': (('som',), 'Somali', 'Somalisch', ('somali',)),
    'ti': (('tir',), 'Tigrinya', 'Tigrinya', ('tigrinya',)),
}

# embedded subtitles: language code from the container (eng, dut, nl, ...) -> ISO 639-1
EMB = {c: c for c in LANGS}
for _c, (_iso2, _en, _nl, _names) in LANGS.items():
    for _x in _iso2: EMB[_x] = _c

# external subtitle files: movie.nl.srt, movie.dutch.srt, movie.Nederlands.srt ...
SIDE = dict(EMB)
for _c, (_iso2, _en, _nl, _names) in LANGS.items():
    for _x in _names + (_en.lower(),): SIDE[_x] = _c

NAME_EN = {c: v[1] for c, v in LANGS.items()}
NAME_NL = {c: v[2] for c, v in LANGS.items()}

