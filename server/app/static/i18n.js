'use strict';
/* MediaScan - interface language (38 languages) and theme (auto/light/dark).
   The UI is written in English (the source language); for any other language a MutationObserver translates visible text
   using static/locales/<code>.json:  { name, s:{english:translation}, rx:[[pattern,flags,template]], w:[[pattern,flags,replacement]] }
   Templates: $1..$9 = group, {t:$n} = translate group as text, {w:$n} = apply w-rules to group ($0 = whole string). */
const LANG_UI = {en:'English',nl:'Nederlands',de:'Deutsch',fr:'Français',es:'Español',it:'Italiano',pt:'Português',tr:'Türkçe',
  ar:'العربية',pl:'Polski',ru:'Русский',uk:'Українська',ro:'Română',cs:'Čeština',hu:'Magyar',el:'Ελληνικά',sv:'Svenska',
  da:'Dansk',no:'Norsk',fi:'Suomi',hr:'Hrvatski',sr:'Srpski',bs:'Bosanski',bg:'Български',he:'עברית',fa:'فارسی',hi:'हिन्दी',
  ur:'اردو',id:'Bahasa Indonesia',ms:'Bahasa Melayu',th:'ไทย',vi:'Tiếng Việt',zh:'中文',ja:'日本語',ko:'한국어',ku:'Kurdî',
  so:'Soomaali',ti:'ትግርኛ'};
const RTL = new Set(['ar','he','fa','ur']);
const PREF = (() => { const g = k => { try { return localStorage.getItem(k); } catch { return null; } };
  let lang = g('ms_lang');
  if (!LANG_UI[lang]) { const nav = (navigator.languages || [navigator.language || '']).map(x => String(x).slice(0,2).toLowerCase()).map(x => x === 'nb' || x === 'nn' ? 'no' : x).find(x => LANG_UI[x]); lang = nav || 'en'; }
  return { lang, theme: g('ms_theme') || 'auto', rows: g('ms_rows'), dense: !!g('ms_dense') }; })();
function savePref(k, v){ try { localStorage.setItem(k, v); } catch {} }
const LOC = PREF.lang === 'nl' ? 'nl-NL' : PREF.lang === 'en' ? 'en-GB' : PREF.lang === 'no' ? 'nb' : PREF.lang;
document.documentElement.lang = PREF.lang;
if (RTL.has(PREF.lang)) document.documentElement.dir = 'rtl';
function applyTheme(t){ PREF.theme = t; if (t === 'auto') document.documentElement.removeAttribute('data-theme'); else document.documentElement.setAttribute('data-theme', t); savePref('ms_theme', t); }
applyTheme(PREF.theme);
function setLang(l){ if (l === PREF.lang || !LANG_UI[l]) return; savePref('ms_lang', l); location.reload(); }
/* Language names in the UI language (for the subtitle-language lists) */
function langName(code, fallback){ try { return new Intl.DisplayNames([LOC], {type:'language'}).of(code) || fallback; } catch { return fallback; } }

let TR = null;                                      // null = English (no translation)
/* If a locale cannot be loaded, simply show the English source text. */
function i18nFallback(){ document.documentElement.removeAttribute('dir'); document.documentElement.lang = 'en'; }
const I18N_READY = PREF.lang === 'en' ? Promise.resolve() :
  fetch(`/static/locales/${PREF.lang}.json?v=${document.documentElement.dataset.v||''}`, {credentials:'same-origin'}).then(r => r.ok ? r.json() : null).then(d => {
    if (!d) return i18nFallback();
    const mk = ([p, f, t]) => { try { return [new RegExp(p, f || ''), t]; } catch { return null; } };
    TR = { s: d.s || {}, rx: (d.rx || []).map(mk).filter(Boolean), w: (d.w || []).map(mk).filter(Boolean) };
    start18n();
  }).catch(i18nFallback);

function trW(s){ if (!TR || !s) return s; let o = String(s); for (const [rx, r] of TR.w) o = o.replace(rx, r); return o; }
function fill(tpl, m){
  return tpl.replace(/\{([tw]):\$(\d)\}|\$(\d)/g, (_, k, g, g2) => {
    if (g2 !== undefined) return m[+g2] ?? '';
    const v = m[+g] ?? ''; return k === 't' ? trText(v) : trW(v); });
}
function trText(s){
  if (!TR || !s) return s;
  const t = s.trim(); if (!t) return s;
  let out = TR.s[t];
  if (out === undefined) for (const [rx, tpl] of TR.rx) { rx.lastIndex = 0; const m = t.match(rx); if (m) { if (tpl.includes('$0')) { const a = [t, ...m.slice(1)]; out = fill(tpl, a); } else if (m[0] === t) { out = fill(tpl, m); } else out = t.replace(rx, (...a) => fill(tpl, a)); break; } }
  return out === undefined ? s : s.replace(t, out);
}
function trNode(root){
  if (!TR || !root) return;
  if (root.nodeType === 3) { const v = trText(root.nodeValue); if (v !== root.nodeValue) root.nodeValue = v; return; }
  if (root.nodeType !== 1) return;
  const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT); let n;
  while ((n = w.nextNode())) { if (n.parentNode && /^(SCRIPT|STYLE|CODE)$/.test(n.parentNode.nodeName)) continue; const v = trText(n.nodeValue); if (v !== n.nodeValue) n.nodeValue = v; }
  const els = root.querySelectorAll ? [root, ...root.querySelectorAll('[placeholder],[title],[aria-label]')] : [];
  for (const el of els) for (const a of ['placeholder','title','aria-label']) { const v = el.getAttribute && el.getAttribute(a); if (v) { const t = trText(v); if (t !== v) el.setAttribute(a, t); } }
}
function start18n(){
  new MutationObserver(ms => { for (const m of ms) { if (m.type === 'characterData') { const v = trText(m.target.nodeValue); if (v !== m.target.nodeValue) m.target.nodeValue = v; } else for (const n of m.addedNodes) trNode(n); } })
    .observe(document.documentElement, {childList:true, subtree:true});
  if (document.body) trNode(document.body); else document.addEventListener('DOMContentLoaded', () => trNode(document.body));
}
