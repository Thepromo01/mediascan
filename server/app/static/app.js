'use strict';
/* ============================== helpers ============================== */
const $ = s => document.querySelector(s);
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const nf = n => Number(n||0).toLocaleString(LOC);
const GB = b => (b/1e9).toLocaleString(LOC,{maximumFractionDigits:b>=1e11?0:1});
const size = b => b==null?'–':(b>=1e12?(b/1e12).toLocaleString(LOC,{maximumFractionDigits:2})+' TB':GB(b)+' GB');
const pct = (a,b) => b?Math.round(100*a/b):0;
const RTF = (() => { try { return new Intl.RelativeTimeFormat(LOC, {numeric:'always', style:'long'}); } catch { return null; } })();
const ago = t => { if(!t) return '–'; const s=Date.now()/1000-t; if(s<60) return 'just now';
  const [n, u] = s<3600 ? [Math.floor(s/60), 'minute'] : s<86400 ? [Math.floor(s/3600), 'hour'] : [Math.floor(s/86400), 'day'];
  if (RTF) return RTF.format(-n, u);
  return u==='minute' ? n+' min ago' : u==='hour' ? n+' h ago' : n===1 ? '1 day ago' : n+' days ago'; };
const when = t => { if (!t) return '–'; const d = new Date(t*1000); return d.toLocaleString(LOC,{day:'numeric',month:'short',...(d.getFullYear()!==new Date().getFullYear()?{year:'numeric'}:{}),hour:'2-digit',minute:'2-digit'}); };
const dur = s => { if(s==null) return '–'; const d=Math.floor(s/86400), h=Math.floor(s%86400/3600); return d? `${d} d ${h} h` : `${h} h ${Math.floor(s%3600/60)} m`; };
const base = r => (r||'').split('/').pop();
const folder = r => (r||'').split('/').slice(0,-1).join(' › ');
const res = (w,h) => w>=3200||h>=1700?'4K':w>=1700||h>=950?'1080p':w>=1100||h>=650?'720p':'SD';
const H = {'Content-Type':'application/json','X-MS':'1'};
// show a number from the server safely (partly comes from workers): only real numbers, otherwise a dash
const num = v => (v==null || v==='' || !Number.isFinite(+v)) ? '–' : String(+v);
// text safely as a JS string inside an onclick attribute
/* event delegation instead of inline on*="..." attributes (keeps the CSP at script-src 'self'):
   elements carry data-click / data-change / data-input = JSON [action, ...args]. An action is a function in ACT
   (called with the element, the event and the args) or a plain global function (called with the args only). */
const on = (type, name, ...args) => `data-${type}="${esc(JSON.stringify([name, ...args]))}"`;
const ACT = {};
function dispatch(e){
  const attr = 'data-' + e.type; const el = e.target.closest ? e.target.closest(`[${attr}]`) : null; if (!el) return;
  let spec; try { spec = JSON.parse(el.getAttribute(attr)); } catch { return; }
  const [name, ...args] = spec; const fn = ACT[name] || window[name];
  if (typeof fn !== 'function') return;
  const r = ACT[name] ? fn(el, e, ...args) : fn(...args);
  if (r && typeof r.then === 'function' && el.tagName === 'BUTTON' && !el.disabled) {     // a running action: no double clicks, show a spinner
    el.disabled = true; el.classList.add('busy');
    r.finally(() => { el.disabled = false; el.classList.remove('busy'); });
  }
}
for (const t of ['click', 'change', 'input']) document.addEventListener(t, dispatch);
document.addEventListener('keydown', e => {          // keyboard users: Enter/Space on a focusable data-click element
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const el = e.target.closest && e.target.closest('[data-click]');
  if (!el || /^(BUTTON|A|INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
  e.preventDefault(); el.click();
});

const ICON = {
  home:'<path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/>',
  queue:'<path d="M4 6h16M4 12h16M4 18h10"/>',
  play:'<circle cx="12" cy="12" r="9"/><path d="M10 8.5l5 3.5-5 3.5z"/>',
  info:'<circle cx="12" cy="12" r="9"/><path d="M12 11v5M12 8v.01"/>',
  more:'<circle cx="5" cy="12" r="1.7"/><circle cx="12" cy="12" r="1.7"/><circle cx="19" cy="12" r="1.7"/>',
  dense:'<path d="M4 7h16M4 12h16M4 17h16"/>',
  cc:'<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="M10 10.5a2 2 0 1 0 0 3M17 10.5a2 2 0 1 0 0 3"/>',
  spark:'<path d="M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5L18 18M6 18l2.5-2.5M15.5 8.5L18 6"/>',
  lib:'<path d="M4 4h4v16H4zM10 4h4v16h-4z"/><path d="M16.5 4.5l3.8 1-3.6 15-3.8-1z"/>',
  shield:'<path d="M12 3l8 3v6c0 4.5-3.4 8-8 9-4.6-1-8-4.5-8-9V6z"/>',
  log:'<path d="M4 4h16v16H4z"/><path d="M8 9h8M8 13h8M8 17h5"/>',
  gear:'<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  cpu:'<rect x="6" y="6" width="12" height="12" rx="2"/><path d="M9 2v4M15 2v4M9 18v4M15 18v4M2 9h4M2 15h4M18 9h4M18 15h4"/>',
  search:'<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  disc:'<circle cx="12" cy="12" r="9"/><circle cx="12" cy="12" r="2.5"/>',
  compare:'<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M12 3v18"/><path d="M6 15l2.5-3 2 2.5M14.5 15l2-2.5 2 2.5"/>',
  scan:'<path d="M4 8V5a1 1 0 0 1 1-1h3M16 4h3a1 1 0 0 1 1 1v3M20 16v3a1 1 0 0 1-1 1h-3M8 20H5a1 1 0 0 1-1-1v-3M4 12h16"/>',
};
const icon = k => `<svg class="i" viewBox="0 0 24 24">${ICON[k]||''}</svg>`;

let OV = null, view = 'overview', page = 0, filter = '', sort = '', sortDir = '', timer = null, cmpId = null;
let pageSize = [50, 100, 200].includes(+PREF.rows) ? +PREF.rows : 100;      // rows per page (remembered in this browser)
if (PREF.dense) document.documentElement.classList.add('dense');
const flt = {cat: '', res: '', codec: ''};          // list filters: folder, resolution, codec
let hashTab = null;
function parseHash(){
  let h = location.hash.slice(1) || 'overview'; const m = h.match(/^compare-(\d+)$/);
  cmpId = m ? +m[1] : null; if (m) h = 'compare';
  const [v, t] = h.split('/');
  view = PAGES[v] ? v : 'overview'; hashTab = t || null;
}

async function api(path, opts={}) {
  const ctl = new AbortController(); const t = setTimeout(()=>ctl.abort(), 20000);
  let r;
  try { r = await fetch(path, {headers:H, credentials:'same-origin', signal:ctl.signal, ...opts}); }
  catch(e){ if (!opts.silent) toast('Server not responding', null, 'bad'); throw e; } finally { clearTimeout(t); }
  if (opts.silent) setOffline(false);
  if (r.status === 401) { showLogin(); throw new Error('login'); }
  if (r.status === 403) { const t = await r.text(); if (t.includes('mfa_setup')) showMfaSetup(); else showLogin(); throw new Error('mfa'); }
  if (!r.ok) { let m = await r.text(); try { m = JSON.parse(m).detail || m; } catch{} toast(m, null, 'bad'); throw new Error(m); }
  return r.status === 204 ? null : r.json();
}
const post = (path, body={}) => api(path, {method:'POST', body:JSON.stringify(body)});
function toast(m, link, kind){
  const t=$('#toast'); t.textContent=trText(String(m)); t.className = kind || '';
  if (link) { const a=document.createElement('a'); a.href=link.href || '#'; a.className='acc'; a.style.marginLeft='12px'; a.style.fontWeight='600';
    a.textContent=trText(link.label); a.onclick=(ev)=>{ if (link.action) { ev.preventDefault(); link.action(); } t.style.display='none'; }; t.appendChild(a); }
  t.style.display='block'; clearTimeout(t._h); t._h=setTimeout(()=>t.style.display='none', link?7000:3800);
}

/* ============================== login & MFA ============================== */
function panel(id){ ['f-setup','f-login','mfasetup','mfacodes'].forEach(x=>$('#'+x).style.display = x===id?'':'none'); $('#loginerr').textContent=''; }
function loginPrefs(){ const el=$('#loginprefs'); if(el){ el.innerHTML = prefSwitch(); el.querySelectorAll('button').forEach(b=>b.addEventListener('click',()=>setTimeout(loginPrefs,0))); } }
async function showLogin(){
  loginPrefs(); $('#app').style.display='none'; document.body.classList.remove('in-app'); $('#login').style.display='grid'; clearInterval(timer);
  fetch('/api/health').then(r=>r.json()).then(d=>{ if(d.version) $('#lver').textContent='v'+d.version; }).catch(()=>{});
  let setup = false; try { setup = (await (await fetch('/api/setup')).json()).setup; } catch {}
  if (setup) { panel('f-setup'); setTimeout(()=>$('#s-code').focus(),50); return; }
  panel('f-login'); $('#pwrow').style.display=''; $('#coderow').style.display='none'; $('#code').value=''; setTimeout(()=>$('#pw').focus(),50);
}
async function doSetup(e){
  e.preventDefault(); $('#loginerr').textContent='';
  if ($('#s-pw').value !== $('#s-pw2').value) { $('#loginerr').textContent = trText('The passwords do not match'); return; }
  const r = await fetch('/api/setup',{method:'POST',headers:H,body:JSON.stringify({code:$('#s-code').value, password:$('#s-pw').value})});
  const d = await r.json().catch(()=>({}));
  if (!r.ok) { $('#loginerr').textContent = trText(d.detail || 'Failed'); return; }
  showMfaSetup();
}
async function doLogin(e){
  e.preventDefault(); $('#loginerr').textContent='';
  const r = await fetch('/api/login',{method:'POST',headers:H,body:JSON.stringify({password:$('#pw').value, code:$('#code').value})});
  const d = await r.json().catch(()=>({}));
  if (r.status === 409) return showLogin();
  if (!r.ok) { $('#loginerr').textContent = trText(d.detail || 'Sign-in failed'); return; }
  if (d.need_code) { $('#pwrow').style.display='none'; $('#coderow').style.display=''; $('#code').focus(); return; }
  if (d.mfa_setup) return showMfaSetup();
  start();
}
async function showMfaSetup(){
  $('#app').style.display='none'; $('#login').style.display='grid'; clearInterval(timer); panel('mfasetup');
  const d = await (await fetch('/api/mfa/setup', {headers:H})).json();
  $('#secret').textContent = d.secret.match(/.{1,4}/g).join(' ');
  $('#qr').innerHTML=''; new QRCode($('#qr'), {text:d.uri, width:180, height:180, correctLevel: QRCode.CorrectLevel.M});
  setTimeout(()=>$('#setupcode').focus(),50);
}
async function enableMfa(){
  const r = await fetch('/api/mfa/enable',{method:'POST',headers:H,body:JSON.stringify({code:$('#setupcode').value})});
  const d = await r.json().catch(()=>({}));
  if (!r.ok) { $('#loginerr').textContent = d.detail || 'Code is incorrect'; return; }
  panel('mfacodes'); $('#codes').innerHTML = d.recovery.map(c=>`<span>${c}</span>`).join('');
}

/* ============================== navigation & polling ============================== */
const PAGES = {
  welcome:     {t:'Welcome', d:'Ready to use in a few steps.', i:'spark', hidden:true},
  overview:    {t:'Overview', d:'Status of the entire library.', i:'home'},
  running:     {t:'In progress', d:'What the workers are doing now and what is planned next.', i:'play', cnt:()=>(OV.jobs.encode?.running||0)+(OV.jobs.encode?.replacing||0)+(OV.jobs.subs?.running||0)+(OV.discs?.running||0)},
  encode:      {t:'Encoding', d:'Make files smaller without visible quality loss. The worker works through the queue from top to bottom.', i:'queue', cnt:()=>OV.jobs.encode?.queued},
  subtitles:   {t:'Subtitles', d:'Find, sync or generate missing subtitles.', i:'cc', cnt:()=>OV.jobs.subs?.queued},
  discs:       {t:'DVD & Blu-ray', d:'Folders with VIDEO_TS or BDMV. Converting turns them into one .mkv with all video, audio, subtitles and chapters.', i:'disc', cnt:()=>(OV.discs?.found||0)+(OV.discs?.queued||0)+(OV.discs?.running||0)},
  library:     {t:'Library', d:'All videos with their current plan. You can also queue items manually here.', i:'lib'},
  compare:     {t:'Compare', d:'Original next to the new version: size, quality, picture and subtitles. You choose which films.', i:'compare'},
  quarantine:  {t:'Quarantine', d:'Replaced originals, kept for a while so you can restore them or have them re-encoded.', i:'shield', cnt:()=>OV.quarantine.n},
  workers:     {t:'Workers', d:'The machines doing the work. Choose what each worker is allowed to do.', i:'cpu', cnt:()=>OV.workers.filter(w=>w.online).length},
  activity:    {t:'Activity', d:'Log of everything that happens.', i:'log'},
  info:        {t:'Info', d:'Your hardware, how well it fits the work, the tools in use, and how the settings work.', i:'info'},
  settings:    {t:'Settings', d:'How MediaScan works: quality, subtitles, storage and security.', i:'gear'},
};
const NAVGROUPS = [[null,['overview','running']],['Processing',['encode','subtitles','discs']],['Library',['library','compare','quarantine']],['System',['workers','activity','info','settings']]];

function renderNav(){
  const w = OV ? OV.workers.find(w=>w.online) : null;
  const html = `<div class="brand"><img class="logo" src="/static/logo.svg?v=${document.documentElement.dataset.v||''}" alt=""><span class="bt">MediaScan</span></div>` +
    NAVGROUPS.map(([lbl,keys])=>(lbl?`<div class="navlbl">${lbl}</div>`:'')+keys.filter(k=>k!=='discs'||(OV&&Object.keys(OV.discs||{}).length)).map(k=>{
      const p=PAGES[k]; const c=OV&&p.cnt?p.cnt():0;
      return `<a href="#${k}" data-v="${k}" class="${k===view?'active':''}">${icon(p.i)}<span>${p.t}</span>${c?`<span class="cnt">${nf(c)}</span>`:''}</a>`;}).join('')).join('') +
    `<div class="navfoot">
      <div class="sw ${OV&&!OV.paused?'on':''}" role="switch" aria-checked="${!!(OV&&!OV.paused)}" tabindex="0" data-click=\'["togglePause","paused"]\'><span>Encoding</span><span class="t"></span></div>
      <div class="sw ${OV&&!OV.subs_paused?'on':''}" role="switch" aria-checked="${!!(OV&&!OV.subs_paused)}" tabindex="0" data-click=\'["togglePause","subs_paused"]\'><span>Subtitles</span><span class="t"></span></div>
      <div class="wstat"><span class="dot ${w?'ok':(OV&&OV.workers.length?'bad':'')}"></span>${w?`worker ${esc(w.name)} online`:(OV&&OV.workers.length?'worker offline':'no worker')}</div>
      ${OV&&OV.sel_waiting?`<div class="wstat"><span class="dot acc"></span>${nf(OV.sel_waiting)} more to start, then pause</div>`:''}
      <div class="prefs">${prefSwitch()}</div>${OV&&OV.version?`<div class="ver">MediaScan ${esc(OV.version)}</div>`:""}
    </div>`;
  const nav = $('#nav'); const key = html + PREF.theme; if (nav._h !== key) { nav.innerHTML = html; nav._h = key; }
  const TAB = ['overview', 'running', 'encode', 'library']; const inMore = !TAB.includes(view);
  const th = TAB.map(k => { const p = PAGES[k]; const c = OV && p.cnt ? p.cnt() : 0;
    return `<a href="#${k}" class="${k === view ? 'on' : ''}">${icon(p.i)}<span>${p.t}</span>${c ? `<span class="cnt">${nf(c)}</span>` : ''}</a>`; }).join('') +
    `<button class="${inMore ? 'on' : ''}" data-click=\'["menu"]\' aria-label="More" aria-controls="nav" aria-expanded="${document.body.classList.contains('menu-open')}">${icon('more')}<span>More</span></button>`;
  const tb = $('#tabbar'); if (tb && tb._h !== th) { tb.innerHTML = th; tb._h = th; }
}
function menu(on){ const open = document.body.classList.toggle('menu-open', on === undefined ? !document.body.classList.contains('menu-open') : !!on); const b = document.querySelector('#tabbar [aria-controls="nav"]'); if (b) b.setAttribute('aria-expanded', open); }

function langSelect(cls=''){
  return `<select class="${cls}" aria-label="Language" data-change=\'["langSel"]\'>${Object.entries(LANG_UI).sort((a,b)=>a[1].localeCompare(b[1])).map(([k,n])=>`<option value="${k}" ${PREF.lang===k?'selected':''}>${esc(n)}</option>`).join('')}</select>`;
}
function prefSwitch(){
  const th = [['auto','A','Auto'],['light','☀','Light'],['dark','☾','Dark']];
  return `<div class="tabs mini">${th.map(([k,i,l])=>`<button class="${PREF.theme===k?'on':''}" title="${l}" ${on('click','theme',k)}>${i}</button>`).join('')}</div>${langSelect('mini')}`;
}

async function start(pre){
  $('#login').style.display='none'; $('#app').style.display='grid'; document.body.classList.add('in-app');
  parseHash(); tab = hashTab;
  if (pre) { OV = pre; renderNav(); } else await refresh();      // boot() already fetched the overview
  clearInterval(timer); timer = setInterval(tick, 5000);
  if (!OV.setup_done && view !== 'welcome') { location.hash = '#welcome'; return; }
  render();
}
const MEM = {};                     // per page: tab, search, filters, sort and page, so coming back shows the same list
window.addEventListener('hashchange', async () => {
  if (view === 'settings' && settingsDirty && !location.hash.startsWith('#settings')) { const to = location.hash; history.replaceState(null, '', '#settings');
    if (!await ask('Leave without saving? Your changes to the settings will be lost.', 'Leave', true)) return; S = {...S0}; settingsDirty = false; location.hash = to; return; }
  const prev = view + (cmpId ? '-detail' : '');
  if (!cmpId) MEM[view] = {tab, filter, flt:{...flt}, libPlan, sort, sortDir, page};
  parseHash();
  const m = (view !== prev && !cmpId) ? MEM[view] : null;
  page=m?m.page:0; filter=m?m.filter:''; sort=m?m.sort:''; sortDir=m?m.sortDir:''; Object.assign(flt, m?m.flt:{cat:'',res:'',codec:''}); libPlan=m?m.libPlan:'';
  tab = hashTab || (m ? m.tab : null); lastHtml=''; actRows=[]; resetSel(); bulk=null; closeDrawer(); menu(false); renderNav(); render();
});
document.addEventListener('keydown', e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable;
  if (e.key === '/' && !typing) { const i = document.querySelector('.toolbar input'); if (i) { e.preventDefault(); i.focus(); i.select(); } }
  else if (e.key === 'Escape' && typing && e.target.matches('.toolbar input') && e.target.value) { e.target.value = ''; onSearch(e.target); }
  else if (e.key === '?' && !typing) { e.preventDefault(); showShortcuts(); }
  else if ((e.key === 'ArrowLeft' || e.key === 'ArrowRight') && !typing && view === 'compare' && cmpId && cmpIds.length > 1) {
    const i = cmpIds.indexOf(cmpId), j = i + (e.key === 'ArrowLeft' ? -1 : 1); if (j >= 0 && j < cmpIds.length) location.hash = '#compare-' + cmpIds[j]; }
});
function showShortcuts(){
  $('#modal-card').innerHTML = `<h2>${esc(trText('Keyboard shortcuts'))}</h2><dl class="kv keys"><dt><kbd>/</kbd></dt><dd>${esc(trText('Search in the current list'))}</dd><dt><kbd>Esc</kbd></dt><dd>${esc(trText('Clear the search, close a drawer or dialog'))}</dd>
    <dt><kbd>←</kbd> <kbd>→</kbd></dt><dd>${esc(trText('Previous or next film on a comparison'))}</dd><dt><kbd>Enter</kbd> <kbd>Space</kbd></dt><dd>${esc(trText('Open or switch the focused row, switch or header'))}</dd><dt><kbd>?</kbd></dt><dd>${esc(trText('This list'))}</dd></dl>
    <div class="btns" style="margin-top:14px"><button class="primary" data-click='["closeModal"]'>OK</button></div>`;
  $('#modal').classList.add('on');
}
document.addEventListener('visibilitychange', ()=>{ if(!document.hidden) tick(); });
let ticking = false;
async function tick(){
  if (document.hidden || ticking || $('#app').style.display==='none') return;
  ticking = true;
  try { await refresh(true); if(['overview','running'].includes(view) || (view==='compare' && cmpBusy) || (view==='discs' && (OV.discs?.running||OV.discs?.queued))) render(true);
    else if (view==='activity' && actRows.length) { const d = await api('/api/events?limit=50', {silent:true}); const fresh = d.rows.filter(r => r.id > actRows[0].id); if (fresh.length) { actRows = fresh.concat(actRows); render(true); } }
    else if (view==='welcome' && wz===1) { const el = $('#wz-scan'); if (el) el.textContent = OV.scan?.running ? `Scanning: ${OV.scan.phase} · ${nf(OV.scan.done)} / ${nf(OV.scan.total)}` : (OV.library?.files ? `${nf(OV.library.files)} videos found (${size(OV.library.size)})` : ''); }
  } catch(e){} finally { ticking = false; }
}
async function refresh(silent){
  try { OV = await api('/api/overview', silent ? {silent:true} : {}); }
  catch(e){ if (silent && e.name !== 'Error') setOffline(true); throw e; }     // network error while polling: show it once, no toast every 5 s
  renderNav();
}
function setOffline(on){ const el = $('#offline'); if (!el || el.hidden === !on) return; el.hidden = !on; if (on) el.textContent = trText('No connection to the server - retrying…'); }
async function togglePause(k){ await post('/api/settings', {[k]: OV[k]?'0':'1'}); await refresh(); toast(OV[k]?'Paused: a running job will finish first':'Resumed'); }
async function scanNow(){ const r=await post('/api/scan'); toast(r.ok?'Scan started':r.msg); await refresh(); }

/* ============================== page layout ============================== */
let renderSeq = 0, lastHtml = '', tab = null;
function pageHead(actions=''){ const p=PAGES[view]; return `<div class="ph"><div class="ti"><h1>${p.t}</h1><p>${p.d}</p></div>${actions}</div>`; }
async function render(soft){
  const v = $('#view'); const seq = ++renderSeq;
  if (!soft && !['overview','running'].includes(view)) v.style.opacity = '.55';
  const pill = soft ? null : setTimeout(() => { if (seq === renderSeq) $('#loadpill').hidden = false; }, 300);    // only when the server is slow
  try { await (R[view]||R.overview)(v, soft, seq); } catch(e){ console.error(e); if (!soft) { lastHtml=''; setView(v, `<div class="card empty">Could not load this page. <button class="link" ${on('click','reload')}>Retry</button></div>`); } } finally { clearTimeout(pill); if (seq === renderSeq) { v.style.opacity = ''; $('#loadpill').hidden = true; } }
}
const stale = seq => seq !== renderSeq;
function setView(v, html){ if (html === lastHtml) return; const ev=v.querySelector('.events'); const top=ev?ev.scrollTop:0; lastHtml = html; v.innerHTML = html; const e2=v.querySelector('.events'); if(e2) e2.scrollTop=top; }

function emptyHtml(e){
  if (filter || flt.cat || flt.res || flt.codec) return `<div class="et">No results for this search or filter.</div><div class="ea"><button class="sm" ${on('click','clearFilters')}>Clear search and filters</button></div>`;
  if (!e) return 'Nothing to show';
  return `<div class="et">${e.text}</div>${e.actions?`<div class="ea">${e.actions}</div>`:''}`;
}
const goBtn = (page, label, cls='sm') => `<button class="${cls}" ${on('click','go',page)}>${label}</button>`;
function listCard({tabs='', extra='', body, total, pager=true, cols, empty}){
  return `<div class="card flush">
    <div class="toolbar"><div class="search">${icon('search')}<input type="search" placeholder="Search by name or folder…" title="Several titles at once: separate them with |" value="${esc(filter)}" data-input=\'["search"]\'></div>${tabs}${extra}<button class="sm ghost dense-btn" title="Compact rows" aria-pressed="${!!PREF.dense}" ${on('click','dense')}>${icon('dense')}</button></div>
    ${bulkBar()}
    <div class="tscroll"><table>${body || `<tbody><tr class="none"><td colspan="${cols||5}" class="empty">${emptyHtml(empty)}</td></tr></tbody>`}</table></div>
    ${pager?pagerHtml(total):''}</div>`;
}
/* sortable column headers: click = descending for numbers (ascending for text), again = the other way, again = default order */
const SORT_DESC_FIRST = new Set(['size', 'vbr', 'result', 'saving', 'finished', 'added', 'attempts', 'gain', 'vmaf', 'created']);
function th(label, key, cls=''){
  const cur = key && sort === key;
  return `<th class="${cls}${key ? ' sortable' : ''}${cur ? ' on' : ''}" ${key ? `tabindex="0" aria-sort="${cur ? (sortDir === 'asc' ? 'ascending' : 'descending') : 'none'}" ` + on('click', 'sortBy', key) : ''}>${label}${cur ? `<span class="arrow">${sortDir === 'asc' ? '▲' : '▼'}</span>` : ''}</th>`;
}
const sortQS = () => sort ? `&sort=${encodeURIComponent(sort)}&dir=${sortDir}` : '';
const fltQS = () => `&cat=${encodeURIComponent(flt.cat)}&res=${encodeURIComponent(flt.res)}&codec=${encodeURIComponent(flt.codec)}`;
function filterBar(){
  const cats = (OV.library?.categories || []).map(c => c.category).filter(Boolean), codecs = OV.library?.codecs || [];
  const sel = (k, first, opts, label = o => o) => `<select ${on('change', 'flt', k)} class="${flt[k] ? 'on' : ''}"><option value="">${first}</option>${opts.map(o => `<option value="${esc(o)}" ${flt[k] === o ? 'selected' : ''}>${esc(label(o))}</option>`).join('')}</select>`;
  return sel('cat', 'Folder: all', cats) + sel('res', 'Resolution: all', ['4K', '1080p', '720p', 'SD']) + sel('codec', 'Codec: all', codecs, o => o.toUpperCase());
}
function clientSort(rows, keys){
  const f = sort && keys[sort]; if (!f) return rows;
  const d = sortDir === 'asc' ? 1 : -1;
  return [...rows].sort((a, b) => { const x = f(a), y = f(b); return (x == null) - (y == null) || (typeof x === 'string' ? x.localeCompare(y, LOC, {numeric: true, sensitivity: 'base'}) : (x > y) - (x < y)) * d; });
}
function tabsHtml(list, cur, fn){ return `<div class="tabs">${list.map(([k,l,n])=>`<button class="${k===cur?'on':''}" ${on('click','tab',fn,k)}>${l}${n!=null?`<span class="n">${nf(n)}</span>`:''}</button>`).join('')}</div>`; }
function setTab(k){ tab=k; page=0; resetSel(); actRows=[]; try { history.replaceState(null, '', '#'+view+(k?'/'+k:'')); } catch {} render(); }
function pagerHtml(total){ const pages=Math.max(1,Math.ceil(total/pageSize)); return `<div class="pager"><select class="sm" aria-label="Rows per page" ${on('change','rows')}>${[50,100,200].map(n=>`<option value="${n}" ${n===pageSize?'selected':''}>${n} per page</option>`).join('')}</select><span class="grow"></span>${nf(total)} items · page ${page+1} of ${pages} <button class="sm" ${page<=0?'disabled':''} data-click=\'["pagePrev"]\' aria-label="Previous page">‹</button><button class="sm" ${page>=pages-1?'disabled':''} data-click=\'["pageNext"]\' aria-label="Next page">›</button></div>`; }
let searchT = null;
// client-side search with the same rule as the server: several terms separated by | = any of these
function matches(text, q){ const terms = String(q||'').split('|').map(t=>t.trim().toLowerCase()).filter(Boolean); return !terms.length || terms.some(t=>String(text||'').toLowerCase().includes(t)); }
function onSearch(el){ clearTimeout(searchT); searchT = setTimeout(async()=>{ filter = el.value; page = 0; resetSel(); actRows = []; const pos = el.selectionStart; await render(); const n = document.querySelector('.toolbar input'); if(n){ n.focus(); n.setSelectionRange(pos,pos); } }, 300); }

// display labels for status/plan codes (the keys are machine codes from the server)
const LABEL = {queued:'queued', running:'running', replacing:'replacing', done:'done', failed:'failed', 'no-gain':'no gain',
  skipped:'skipped', cancelled:'cancelled', restored:'restored', 'not-found':'nothing found', subtitles:'subtitles',
  encode:'to encode', efficient:'already efficient', 'small-gain':'small gain', 'dolby-vision':'Dolby Vision', disc:'DVD/Blu-ray',
  unknown:'unknown', error:'read error', waiting:'waiting for Plex'};
const HELP = {queued:'Waiting for a worker; the queue runs from top to bottom', running:'A worker is busy with this file', replacing:'The new version is on the NAS and is swapped in as soon as nobody is watching',
  done:'Replaced after the quality check; the original is in quarantine for a while', failed:'Stopped with an error; see the message and try again', 'no-gain':'The new file would save less than the minimum saving, so the original was kept',
  skipped:'Skipped by you; Retry puts it back in the queue', cancelled:'Stopped because the file changed or disappeared', restored:'The original was put back from quarantine', 'not-found':'No subtitles found online and none could be made',
  subtitles:'A worker is searching or making subtitles', encode:'Planned for encoding', efficient:'Already efficient: encoding would hardly save anything', 'small-gain':'Would save less than the minimum saving', 'dolby-vision':'Dolby Vision: not re-encoded, the DV layer would be lost',
  disc:'Part of a DVD or Blu-ray folder; convert it on the DVD & Blu-ray page', error:'The file could not be read; see the drawer', waiting:'Waiting for Plex: someone is watching this film'};
function chip(st){ const cls = {running:'running',done:'done',failed:'failed',replacing:'replacing',subtitles:'running',waiting:'replacing'}[st]||''; return `<span class="chip ${cls}" ${HELP[st]?`title="${esc(HELP[st])}"`:''}>${esc(LABEL[st]||st)}</span>`; }
const cpuTag = r => r && r.cpu ? ' <span class="chip cpu" title="Encoded on the CPU with x265 (tune grain)">CPU</span>' : '';
function langChips(have, made){ return (OV?.langs||'en,nl,tr').split(',').map(l=> `<span class="lang ${made?.includes(l)?'made':have?.includes(l)?'have':'miss'}">${l.toUpperCase()}</span>`).join(''); }
function fileInfo(r){ const p=[]; if(r.codec) p.push(r.codec.toUpperCase()); if(r.width) p.push(res(r.width,r.height)); if(r.hdr) p.push('HDR'); if(r.dv) p.push('DV'); if(r.vbr) p.push((r.vbr/1e6).toFixed(1)+' Mbps'); return p.join(' · '); }
function nameCell(rel){ return `<td><div class="fn">${esc(base(rel))}</div><div class="sub">${esc(folder(rel))}</div></td>`; }

/* ============================== selection & bulk ============================== */
let sel = new Set(), selAll = false, pageIds = [], lastClick = null, bulk = null;
function resetSel(){ sel = new Set(); selAll = false; lastClick = null; }
const ck = id => `<td class="ck" data-click=\'["stop"]\'><input type="checkbox" ${selAll||sel.has(id)?'checked':''} ${on('click','toggleSel',id)} aria-label="select"></td>`;
const ckHead = () => `<th class="ck"><input type="checkbox" ${selAll||(pageIds.length&&pageIds.every(i=>sel.has(i)))?'checked':''} data-click=\'["togglePage"]\' aria-label="select page"></th>`;
function toggleSel(e, id){
  if (selAll) { selAll = false; sel = new Set(pageIds); }
  const on = e.target.checked;
  if (e.shiftKey && lastClick !== null) { const a = pageIds.indexOf(lastClick), b = pageIds.indexOf(id); if (a>-1 && b>-1) pageIds.slice(Math.min(a,b), Math.max(a,b)+1).forEach(i => on?sel.add(i):sel.delete(i)); }
  on ? sel.add(id) : sel.delete(id); lastClick = id; syncSel();
}
function togglePage(on){ selAll = false; pageIds.forEach(i => on?sel.add(i):sel.delete(i)); syncSel(); }
function syncSel(){
  document.querySelectorAll('td.ck input').forEach((c,i) => { c.checked = selAll || sel.has(pageIds[i]); c.closest('tr').classList.toggle('sel', c.checked); });
  const h = document.querySelector('th.ck input'); if (h) h.checked = selAll || (pageIds.length && pageIds.every(i=>sel.has(i)));
  const el = document.getElementById('bulk'); if (el) el.outerHTML = bulkBar();
}
function bulkBar(){
  const n = selAll ? bulk?.total : sel.size;
  if (!bulk || !n) return '<div id="bulk"></div>';
  const allLink = bulk.allowAll && !selAll && pageIds.length && pageIds.every(i=>sel.has(i)) && bulk.total > pageIds.length
    ? `<button class="link" data-click=\'["selAllResults"]\'>Select all ${nf(bulk.total)} results</button>` : '';
  return `<div id="bulk" class="bulk"><b>${nf(n)} selected</b>${allLink}<span class="grow"></span>
    ${bulk.actions.map(([l,a,c]) => `<button class="sm ${c||''}" ${on('click','doBulk',a)}>${l}</button>`).join('')}
    <button class="sm ghost" data-click=\'["clearSelAll"]\'>Clear</button></div>`;
}
async function doBulk(action){
  const n = selAll ? bulk.total : sel.size;
  const q = {only:`Process only these ${n}? They get priority, processing resumes and pauses again automatically afterwards.`, cmpmake:`Make ${n} comparison(s)? About 1–2 MB each.`, skip:`Skip ${n} job(s)?`, restore:`Restore ${n} original(s)? The new versions will be deleted.`, redo:`Re-encode ${n} film(s) with the current rules? The originals are restored and the films get priority in the queue.`, cmpdel:`Delete ${n} comparison(s)? The films themselves stay as they are.`}[action];
  if (q && !await ask(q, {skip:'Skip', restore:'Restore', redo:'Re-encode', cmpdel:'Delete', cmpmake:'Make'}[action]||'OK', ['restore','cmpdel'].includes(action))) return;
  let r;
  if (bulk.type === 'jobs') r = await post('/api/jobs/bulk', selAll ? {action, all:true, kind:bulk.kind, status:bulk.status, q:filter, ...flt} : {action, ids:[...sel]});
  else if (bulk.type === 'files') r = await post('/api/files/queue', {ids:[...sel], kinds:{enc:['encode'],subs:['subs'],both:['encode','subs']}[action]});
  else if (bulk.type === 'cmp') r = await post('/api/compare/bulk-delete', {ids:[...sel]});
  else if (bulk.type === 'cmpmake') r = await post('/api/compare/bulk-make', selAll ? {all:true, q:filter} : {ids:[...sel]});
  else if (bulk.type === 'disc') r = await post('/api/discs/action', {action, ids:[...sel]});
  else r = await post('/api/quarantine/restore', {ids:[...sel], redo: action==='redo'});
  resetSel(); await refresh(); lastHtml=''; render();
  if (action==='only' && r.n) toast(`${nf(r.n)} added to the selection · the worker starts within a few seconds`, {href:'#running', label:'View'});
  else if (bulk.type==='disc') toast(action==='convert' ? `${nf(r.n)} folder(s) will be converted · the worker starts within half a minute` : `${nf(r.n)} updated`);
  else if (action==='cmpdel') toast(`${nf(r.n)} comparison(s) deleted`, null, 'ok');
  else if (action==='cmpmake') toast(`${nf(r.n)} comparison(s) requested · ready within a few minutes`, null, 'ok');
  else if (action==='redo') toast(`${nf(r.queued||0)} film(s) back in the queue` + (r.errors?.length ? ` · ${r.errors.length} error(s)` : ''));
  else toast(`${nf(r.n)} updated` + (r.skipped ? ` · ${nf(r.skipped)} not applicable` : '') + (r.errors?.length ? ` · ${r.errors.length} error(s)` : ''));
}
async function act(id, a){
  if(a==='skip' && !await ask('Skip this job?', 'Skip')) return;
  await post(`/api/jobs/${id}/${a}`);
  if (a==='skip') toast('Skipped', {label:'Undo', action: () => act(id, 'retry')}, 'ok');
  else toast({retry:'Back in the queue', 'retry-cpu':'Queued for the CPU encoder', prioritize:'Moved to the front'}[a] || 'Done', null, 'ok');
  lastHtml=''; render(); refresh();
}

/* ============================== pages ============================== */
const R = {};

R.overview = (v) => {
  const e=OV.jobs.encode||{}, s=OV.jobs.subs||{};
  const doneN=(e.done||0)+(e['no-gain']||0)+(e.skipped||0), allN=doneN+(e.queued||0)+(e.running||0)+(e.replacing||0)+(e.failed||0);
  const subsDone=(s.done||0)+(s['not-found']||0), subsAll=subsDone+(s.queued||0)+(s.running||0)+(s.failed||0);
  const days=[]; for(let i=13;i>=0;i--){ const d=new Date(Date.now()-i*864e5); days.push(d.toLocaleDateString('sv-SE')); }
  const dm=Object.fromEntries((OV.daily||[]).map(x=>[x.d,x.saved])); const mx=Math.max(1,...days.map(d=>dm[d]||0));
  const chart = OV.daily?.length ? `<div class="chart">${days.map(d=>`<div class="b" title="${d}: ${size(dm[d]||0)}"><i style="height:${Math.round(80*(dm[d]||0)/mx)}px"></i><span>${d.slice(8)}</span></div>`).join('')}</div>` : '<div class="empty" style="padding:24px">No processed files yet</div>';
  setView(v, `${pageHead(`<button data-click=\'["scanNow"]\'>${icon('scan')} Scan now</button>`)}
  ${OV.auto_paused_disk?`<div class="banner row"><span>Free space on the NAS is below the safety brake. Encoding was paused automatically.</span>${goBtn('settings','Open settings','sm')}</div>`:''}
  ${OV.paused&&OV.subs_paused?`<div class="banner row"><span>Encoding and subtitles are paused.</span><span class="btns"><button class="sm" ${on('click','resume','paused')}>Resume encoding</button><button class="sm" ${on('click','resume','subs_paused')}>Resume subtitles</button></span></div>`
    : OV.paused?`<div class="banner row"><span>Encoding is paused: no new encode jobs are handed out.</span><button class="sm" ${on('click','resume','paused')}>Resume</button></div>`
    : OV.subs_paused?`<div class="banner row"><span>Subtitles are paused: no new subtitle jobs are handed out.</span><button class="sm" ${on('click','resume','subs_paused')}>Resume</button></div>`:''}
  ${scanBanner()}
  <div class="hero">
    <div class="card kpi acc"><div class="l">Saved</div><div class="v">${size(OV.saved)}</div><div class="s">${OV.done_src?pct(OV.saved,OV.done_src)+'% smaller on processed files':'nothing processed yet'}</div></div>
    <div class="card kpi"><div class="l">Time left</div><div class="v">${OV.eta_sec?dur(OV.eta_sec):'–'}</div><div class="s">${OV.throughput_24h?size(OV.throughput_24h)+' per 24 h':'no measurement yet'}</div></div>
  </div>
  <div class="strip">
    <div class="stat" ${on('click','go','library')}><span class="l">Library</span><b>${size(OV.library.size)}</b><span class="s">${OV.library.files===1?'1 video':nf(OV.library.files)+' videos'}</span></div>
    <div class="stat" ${on('click','go','encode')}><span class="l">Queued</span><b>${nf(e.queued)}</b><span class="s">${size(OV.remaining_src)} · ~${size(OV.remaining_est_save)} gain</span></div>
    <div class="stat" ${on('click','go','quarantine')}><span class="l">Free on NAS</span><b>${size(OV.disk.free)}</b><span class="s">quarantine ${size(OV.quarantine.size)}</span></div>
  </div>
  <div class="grid two" style="margin-top:14px">
    <div class="grid">
      <div class="card"><h3>Now running</h3>${runningHtml()}${selSummary()}</div>
      <div class="card"><h3>Progress</h3>
        <div class="row1" style="border:0;padding-top:0"><span class="t">Encoding</span><span class="w">${nf(doneN)} / ${nf(allN)}</span></div>
        <div class="bar" style="margin-bottom:14px"><i style="width:${pct(doneN,allN)}%"></i></div>
        <div class="row1" style="border:0"><span class="t">Subtitles</span><span class="w">${nf(subsDone)} / ${nf(subsAll)}</span></div>
        <div class="bar soft"><i style="width:${pct(subsDone,subsAll)}%"></i></div>
      </div>
      <div class="card"><h3>Saved per day <span class="r sub">last 14 days</span></h3>${chart}</div>
      <div class="card"><h3>Per folder</h3><table class="mini">${OV.library.categories.map(c=>`<tr class="lnk" ${on('click','goFolder',c.category)}><td>${esc(c.category)}</td><td class="num sub">${c.n===1?'1 video':nf(c.n)+' videos'}</td><td class="num" style="width:90px">${size(c.s)}</td></tr>`).join('')}</table></div>
    </div>
    <div class="grid">
      <div class="card"><h3>Workers</h3>${OV.workers.length?OV.workers.map(workerRow).join(''):'<div class="empty" style="padding:16px">No worker connected yet</div>'}</div>
      <div class="card"><h3>Recently added <a class="r" href="#library/new">all</a></h3>${OV.newest.map(n=>`<div class="row1" title="${esc(n.rel)}"><span class="t">${esc(base(n.rel))}</span><span class="w">${ago(n.first_seen)}</span></div>`).join('')||'<div class="empty" style="padding:16px">–</div>'}</div>
      <div class="card"><h3>Activity <a class="r" href="#activity">all</a></h3><div class="events">${eventsHtml(OV.events)}</div></div>
    </div>
  </div>`);
};
function workerRow(w){
  const i = w.info||{}; const jobs = OV.running.filter(j=>j.worker===w.name);
  const c = w.cfg||{}; const roles = [c.encode&&'encoding', c.subs&&'subtitles'].filter(Boolean).join(' + ') || 'disabled';
  return `<div style="padding:9px 0;border-bottom:1px solid var(--line);cursor:pointer" data-click=\'["go","workers"]\'>
    <div class="row1" style="border:0;padding:0"><span class="t"><span class="dot ${w.online?'ok':'bad'}" style="display:inline-block;margin-right:8px"></span><b>${esc(w.name)}</b></span><span class="w">${w.online?'online':'seen '+ago(w.last_seen)}</span></div>
    <div class="sub" style="margin-left:16px">${esc(i.gpu||'')}${i.encoder?' · '+esc(i.encoder):''}${i.os?' · '+esc(i.os):''}</div>
    <div class="sub" style="margin-left:16px">${roles}${c.max_height?` · up to ${num(c.max_height)}p`:''}${jobs.length?` · working on ${jobs.map(j=>`<span class="acc">${esc(base(j.rel))}</span>`).join(', ')}`:''}</div></div>`;
}
function scanBanner(){
  const sc = OV.scan || {}; if (!sc.running) return '';
  const p = sc.total ? Math.min(100, Math.round(100 * sc.done / sc.total)) : 0;
  const el = sc.started ? Date.now()/1000 - sc.started : 0, eta = sc.total && sc.done > 20 && el > 5 ? el * (sc.total - sc.done) / sc.done : null;
  return `<div class="banner scan"><div class="row1" style="border:0;padding:0"><span>Scanning: ${esc(trW(sc.phase||''))}</span><span class="sub">${nf(sc.done)} / ${nf(sc.total)}${eta!=null?' · ~'+mins(eta)+' left':''}</span></div><div class="bar"><i style="width:${p}%"></i></div></div>`;
}
function goFolder(cat){ location.hash = '#library'; setTimeout(() => { flt.cat = cat; page = 0; render(); }, 0); }
function runningHtml(){
  const dr = (OV.discs_running||[]).map(d=>`<div class="job" data-click=\'["go","#discs"]\'>
    <div class="row1" style="border:0;padding:0"><span class="t n">${esc(base(d.folder))}</span><span class="chip running">${d.kind==='bd'?'Blu-ray':'DVD'} → MKV</span></div>
    <div class="m"><span>${esc(trW(d.stage))}${d.message?' · '+esc(trW(d.message)):''}${d.eta!=null?' · ~'+mins(d.eta)+' left':''}</span><span>${Math.round(d.progress||0)}%</span></div>
    <div class="bar"><i style="width:${d.progress||0}%"></i></div></div>`).join('');
  return dr + runningHtml0();
}
function idleReason(){
  const online = (OV.workers||[]).filter(w=>w.online), e = OV.jobs.encode||{}, su = OV.jobs.subs||{};
  const queued = (e.queued||0) + (su.queued||0);
  if (!online.length) return {text: (OV.workers||[]).length ? 'No worker is online, so nothing can run.' : 'There is no worker yet. Add one to get started.', btn: goBtn('workers', (OV.workers||[]).length ? 'Open Workers' : 'Add worker')};
  if (OV.paused && OV.subs_paused) return {text:'Encoding and subtitles are paused.', btn:`<button class="sm" ${on('click','resume','paused')}>Resume encoding</button><button class="sm" ${on('click','resume','subs_paused')}>Resume subtitles</button>`};
  if (OV.paused && e.queued && !su.queued) return {text:'Encoding is paused.', btn:`<button class="sm" ${on('click','resume','paused')}>Resume</button>`};
  if (OV.subs_paused && su.queued && !e.queued) return {text:'Subtitles are paused.', btn:`<button class="sm" ${on('click','resume','subs_paused')}>Resume</button>`};
  if (OV.auto_paused_disk) return {text:'Paused automatically: the NAS has less free space than the safety brake.', btn:goBtn('settings','Open settings')};
  if (!queued) return {text:'The queue is empty. New files are queued after a scan.', btn:`<button class="sm" data-click='["scanNow"]'>Scan now</button>`};
  return {text:'A worker picks up the next job within a few seconds.', btn:''};
}
function jobEta(j){ const p = +j.progress || 0; if (!j.started || p < 5 || p >= 100) return ''; const left = (Date.now()/1000 - j.started) * (100 - p) / p; return left > 30 ? ' · ~' + mins(left) + ' left' : ''; }
function stepTxt(j){ const m = String(j.message||'').match(/\b(\d+)\/(\d+)\b/); return m ? ` · step ${m[1]} of ${m[2]}` : ''; }
function runningHtml0(){
  if(!OV.running.length) { if ((OV.discs_running||[]).length) return ''; const r = idleReason(); return `<div class="empty idle"><div>${r.text}</div>${r.btn?`<div class="ea">${r.btn}</div>`:''}</div>`; }
  return OV.running.map(j=>{ const waiting = j.status==='replacing' && /watched|Plex/i.test(j.stage||''); return `<div class="job" ${on('click','openFile',j.file_id)}>
    <div class="row1" style="border:0;padding:0"><span class="t n">${esc(base(j.rel))}</span><span>${chip(j.kind==='subs'?'subtitles':waiting?'waiting':j.status==='replacing'?'replacing':'running')}${cpuTag(j)}</span></div>
    <div class="m"><span>${esc(trW(j.stage||''))}${j.message&&!stepTxt(j)?' · '+esc(trW(j.message)):''}${stepTxt(j)}${jobEta(j)}</span><span>${Math.round(j.progress||0)}%</span></div>
    <div class="bar ${j.kind==='subs'?'soft':''}"><i style="width:${j.progress||0}%"></i></div></div>`; }).join('');
}
const eventsHtml = ev => ev.map(e=>`<div class="ev ${e.level}"><time>${new Date(e.ts*1000).toLocaleString(LOC,{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})}</time><span>${esc(e.msg)}${e.n>1?` <span class="chip" title="Repeated ${e.n} times">×${e.n}</span>`:''}</span></div>`).join('')||'<div class="empty" style="padding:16px">–</div>';

/* selection ('Process only these'): show what was chosen and how far along it is */
const SEL_DONE = ['done','no-gain','not-found','skipped','failed','cancelled'];
function selCount(items){ const done=items.filter(i=>SEL_DONE.includes(i.status)).length, busy=items.filter(i=>['running','replacing'].includes(i.status)).length; return {done, busy, wait:items.length-done-busy, all:items.length}; }
function selPos(c){
  if (c.busy) return `working on ${c.done+1}${c.busy>1?'–'+(c.done+c.busy):''} of ${c.all}`;
  if (c.wait) return `${c.done} of ${c.all} done, waiting for a worker`;
  return `done (${c.all} of ${c.all})`;
}
function selStatus(i, kind){
  if (i.status==='queued') return '<span class="chip">waiting</span>';
  if (i.status==='running') return chip(kind==='subs'?'subtitles':'running');
  if (i.status==='replacing') return chip('replacing');
  if (i.status==='done') return '<span class="chip done">done</span>';
  if (i.status==='failed') return '<span class="chip failed">failed</span>';
  return `<span class="chip">${esc(LABEL[i.status]||i.status)}</span>`;
}
function selDetail(i, kind){
  if (['running','replacing'].includes(i.status)) return `${esc(trW(i.stage||''))}${i.message?' · '+esc(trW(i.message)):''} · ${Math.round(i.progress||0)}%`;
  if (kind==='encode' && i.status==='done' && i.src_size && i.out_size) return `${size(i.src_size)} → ${size(i.out_size)} · ${pct(i.src_size-i.out_size,i.src_size)}% smaller${i.vmaf?' · VMAF '+num(i.vmaf):''} · <a href="#compare-${i.file_id}" class="acc" data-click=\'["stop"]\'>comparison</a>`;
  if (kind==='subs' && i.status==='done') return i.made&&i.made.length? 'made: '+i.made.map(l=>l.toUpperCase()).join(', ') : 'nothing needed';
  if (i.status==='no-gain') return esc(trW(i.reason||i.message||'original kept'));
  if (i.status==='failed') return esc(trW(i.message||''));
  return '';
}
const SEL_KEEP_S = 30*60; const selShowAll = {};
function selectionHtml(){
  const S = OV.selection||{};
  return ['encode','subs'].filter(k=>(S[k]||[]).length).map(k=>{
    const rank=i=>['running','replacing'].includes(i.status)?0:i.status==='queued'?1:2;
    const all=[...S[k]].sort((a,b)=>rank(a)-rank(b)), c=selCount(all), finished=!c.busy&&!c.wait;
    const old=i=>SEL_DONE.includes(i.status) && i.finished && Date.now()/1000-i.finished > SEL_KEEP_S;   // hidden after 30 min
    const hidden=all.filter(old).length, it=selShowAll[k]?all:all.filter(i=>!old(i));
    return `<div class="card"><h3 style="display:flex;justify-content:space-between;align-items:center;gap:10px">${k==='encode'?'Selection · encoding':'Selection · subtitles'}${finished?`<button class="sm" ${on('click','clearSel',k)}>Close list</button>`:''}</h3>
      <div class="row1" style="border:0;padding:0 0 6px"><span class="t sub">${selPos(c)}${c.busy&&c.done?` · ${c.done} done`:''}${c.busy&&c.wait?` · ${c.wait} waiting`:''}</span><span class="w">${pct(c.done,c.all)}%</span></div>
      <div class="bar ${k==='subs'?'soft':''}" style="margin-bottom:8px"><i style="width:${pct(c.done,c.all)}%"></i></div>
      ${it.map(i=>`<div class="row1" style="cursor:pointer" ${on('click','openFile',i.file_id)}><span class="t">${esc(base(i.rel))}<span class="sub" style="display:block">${selDetail(i,k)}</span></span>${selStatus(i,k)}</div>`).join('')}
      ${hidden?`<div class="sub" style="padding-top:8px"><button class="link" ${on('click','selShowAll',k)}>${selShowAll[k]?'Hide finished items':`${hidden} finished earlier · show`}</button></div>`:''}
    </div>`; }).join('');
}
async function clearSel(kind){ try { await post('/api/selection/clear', {kind}); } catch(e){} await refresh(); render(true); }
function selPlace(kind, id){   // 'next', '#2', ... in the order the worker picks them up
  const w=(OV.selection?.[kind]||[]).filter(i=>i.status==='queued').sort((a,b)=>(b.priority||0)-(a.priority||0));
  const n=w.findIndex(i=>i.id===id); return n<0 ? '' : n===0 ? 'next' : `#${n+1}`;
}
function selBanner(kind){
  const it=(OV.selection?.[kind]||[]); const c=selCount(it);
  if (!it.length || (!c.busy && !c.wait)) return '';
  const run=OV.running.filter(j=>j.kind===kind);
  const now = run.length ? run.map(j=>`<b>${esc(base(j.rel))}</b> · ${esc(trW(j.stage||''))} ${Math.round(j.progress||0)}%`).join(' · ') : 'waiting for worker';
  return `<div class="banner" style="display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap"><span style="min-width:0">Now: ${now}${c.wait?` <span style="opacity:.8">· then ${c.wait} more from the selection</span>`:''}</span><a href="#running" class="acc" style="font-weight:600">View</a></div>`;
}
function selSummary(){
  const S = OV.selection||{}; const parts = ['encode','subs'].filter(k=>(S[k]||[]).length).map(k=>`${k==='encode'?'encoding':'subtitles'}: ${selPos(selCount(S[k]))}`);
  return parts.length ? `<div class="sub" style="margin-top:10px"><a href="#running" class="acc">Selection · ${parts.join(' · ')}</a></div>` : '';
}
R.running = async (v, soft, seq) => {
  const nxt = OV.paused ? {rows: [], total: 0} : await api('/api/jobs?kind=encode&status=queued&limit=6'); if (stale(seq)) return;
  const up = nxt.rows.length ? `<div class="card"><h3>Next up${nxt.total>nxt.rows.length?`<span class="r muted"><span>${nf(nxt.total)}</span> <span>in the queue</span> · <a href="#encode" class="acc">all</a></span>`:''}</h3>
    ${nxt.rows.map((r,i)=>`<div class="row1" style="cursor:pointer" ${on('click','openFile',r.file_id)}><span class="sub" style="width:18px">${i+1}</span><span class="t n" style="flex:1">${esc(base(r.rel))}<div class="sub">${esc(folder(r.rel))}</div></span><span class="sub">${size(r.size)}${r.est_save?` · ~${GB(r.est_save)} GB gain`:''}</span></div>`).join('')}</div>` : '';
  setView(v, `${pageHead()}<div style="display:grid;gap:16px;align-content:start"><div class="card">${runningHtml()}</div>${up}<div class="selgrid">${selectionHtml()}</div></div>`);
};

function resultHtml(r){
  const R2=r.result||{};
  if (r.status==='queued') { const bps = OV.throughput_bps || (OV.throughput_24h ? OV.throughput_24h / 86400 : 0); const eta = r.queue_pos && bps && r.ahead_bytes != null ? r.ahead_bytes / bps : null;
    return `<span class="muted">~${GB(r.est_save)} GB gain</span><div class="sub">${r.queue_pos?`#${nf(r.queue_pos)}${eta!=null?' · starts in ~'+mins(eta):''} · `:''}estimate${r.message?' · '+esc(trW(r.message)):''}</div>`; }
  if (R2.out_size && R2.src_size) return `${GB(R2.src_size)} → <b>${GB(R2.out_size)} GB</b> <span class="good">−${pct(R2.src_size-R2.out_size,R2.src_size)}%</span><div class="sub">VMAF ${num(R2.vmaf_mean)} · worst 5% ${num(R2.vmaf_p5)}</div>`;
  return r.message ? `<span class="muted">${esc(trW(r.message))}</span>` : '';
}

async function jobsPage(v, seq, {kind, status, tabs, actionsFor}){
  const d = await api(`/api/jobs?kind=${kind}&status=${status}&q=${encodeURIComponent(filter)}&offset=${page*pageSize}&limit=${pageSize}${sortQS()}${fltQS()}`);
  if (stale(seq)) return;
  const actions = actionsFor(status);
  bulk = actions ? {type:'jobs', kind, status, total:d.total, allowAll:true, actions} : null;
  pageIds = actions ? d.rows.map(r=>r.id) : [];
  const body = d.rows.length ? `<thead><tr>${actions?ckHead():''}${th('File','name')}${th('Source','size')}${th('Status', status==='queued'?'':'finished')}${kind==='subs'?th('Languages',''):th('Result','result')}</tr></thead><tbody>` + d.rows.map(r=>`<tr class="${sel.has(r.id)||selAll?'sel':''}" ${on('click','openFile',r.file_id)}>
      ${actions?ck(r.id):''}${nameCell(r.rel)}
      <td><div>${fileInfo(r)}</div><div class="sub">${GB(r.size)} GB</div></td>
      <td>${selPlace(kind, r.id) ? `<span class="chip running" title="In the selection">selection · ${selPlace(kind, r.id)}</span>` : chip(r.status)}${kind==='encode'?cpuTag(r):''}${r.finished&&r.status!=='queued'?`<div class="sub">${when(r.finished)}</div>`:''}</td>
      <td>${kind==='subs'?langChips(r.subs_have,(r.result?.made||[]).map(m=>m.lang)):resultHtml(r)}</td></tr>`).join('') + '</tbody>' : '';
  const empty = kind==='encode' ? {queued:{text:'Nothing is queued for encoding. New files are queued after a scan.', actions:`<button class="sm" data-click='["scanNow"]'>Scan now</button>${goBtn('library','Open the library')}`},
      done:{text:'No files have been replaced yet. Finished encodes appear here after the quality check.'}, 'no-gain':{text:'No files were kept as they are. Films that would save too little end up here.'}}[status]
    : {queued:{text:'No subtitles are waiting. Files missing a language are queued after a scan.', actions:`<button class="sm" data-click='["scanNow"]'>Scan now</button>${goBtn('settings','Subtitle languages')}`},
      running:{text:'No subtitle job is running right now.'}, done:{text:'No subtitles have been made yet.'}, 'not-found':{text:'Nothing missing: every file got its subtitles so far.'}}[status];
  const cpuNote = kind==='encode'&&OV.cpu_waiting&&!OV.cpu_worker ? `<div class="banner"><b>${nf(OV.cpu_waiting)}</b> <span>film(s) wait for the CPU encoder, but no worker has CPU encode switched on (Workers page).</span></div>` : '';
  setView(v, pageHead() + cpuNote + selBanner(kind) + listCard({tabs, extra:filterBar(), body, total:d.total, cols:5, empty}));
}

// tab key (English, in the URL) -> job status codes sent to the server
const ENC_TABS = {queue:'queued', done:'done,replacing', 'no-gain':'no-gain,skipped,restored,cancelled', failed:'failed'};
R.encode = (v,s,seq) => {
  const st = ENC_TABS[tab] ? tab : 'queue'; const c = OV.jobs.encode||{};
  const tabs = tabsHtml([['queue','Queue',c.queued],['done','Done',(c.done||0)+(c.replacing||0)],
    ['no-gain','No gain',(c['no-gain']||0)+(c.skipped||0)+(c.restored||0)+(c.cancelled||0)],['failed','Failed',c.failed]], st, 'setTab');
  if (st === 'failed') return failedPage(v, seq, 'encode', tabs);
  return jobsPage(v, seq, {kind:'encode', status:ENC_TABS[st], tabs,
    actionsFor:()=> st==='queue' ? [['Process only these','only','primary'],['Prioritise','prioritize'],['Skip','skip']]
      : st==='no-gain' ? [['Retry','retry','primary'], ...(OV.workers.some(w=>w.cfg&&w.cfg.cpu)?[['Retry on CPU (x265)','retry-cpu']]:[])] : null});
};
const SUB_TABS = {queue:'queued', running:'running', done:'done', 'not-found':'not-found', failed:'failed'};
R.subtitles = (v,s,seq) => {
  const st = SUB_TABS[tab] ? tab : 'queue'; const c = OV.jobs.subs||{};
  const tabs = tabsHtml([['queue','Queue',c.queued],['running','In progress',c.running],['done','Done',c.done],['not-found','Nothing found',c['not-found']],['failed','Failed',c.failed]], st, 'setTab');
  if (st === 'failed') return failedPage(v, seq, 'subs', tabs);
  return jobsPage(v, seq, {kind:'subs', status:SUB_TABS[st], tabs, actionsFor:x=> x==='queued'?[['Process only these','only','primary'],['Prioritise','prioritize'],['Skip','skip']] : ['not-found','failed'].includes(x)?[['Retry','retry','primary']]:null});
};
// plain-language explanation and next step for a known error (texts live in the knowledge base files)
const ERR_KEYS = [[/missing from staging/i,'staging'],[/cannot be copied into \.mkv/i,'mux'],[/not enough (free )?space/i,'space'],[/share unreachable|took longer than an hour|made no progress/i,'share'],
  [/Ollama/i,'ollama'],[/translation failed/i,'translation'],[/OpenSubtitles/i,'opensubtitles'],[/Whisper|no audio stream/i,'whisper'],[/Dolby Vision/i,'dv'],[/VMAF .*not plausible|measurement unreliable/i,'vmaf'],
  [/ffprobe|cannot read the file|no video stream/i,'probe'],[/original (disappeared|has changed|no longer)/i,'original'],[/check on the NAS rejected|duration or number of streams/i,'check'],[/worker (offline|removed|restarted|restarting)/i,'worker'],[/ffmpeg/i,'ffmpeg']];
function errorHelp(msg, kb){ const k = (ERR_KEYS.find(([rx]) => rx.test(msg||''))||[])[1]; const t = k && kb?.errors?.[k]; return t ? `<div class="errhelp">${kbText(t)}</div>` : ''; }
async function failedPage(v, seq, k, tabs){
  const d = await api(`/api/jobs?kind=${k}&status=failed&q=${encodeURIComponent(filter)}&offset=${page*pageSize}&limit=${pageSize}${sortQS()}${fltQS()}`);
  if (stale(seq)) return;
  bulk = {type:'jobs', kind:k, status:'failed', total:d.total, allowAll:true, actions:[['Retry','retry','primary']]};
  pageIds = d.rows.map(r=>r.id);
  const kb = await loadKB(); if (stale(seq)) return;
  const body = d.rows.length ? `<thead><tr>${ckHead()}${th('File','name')}${th('Error','')}${th('When','finished')}</tr></thead><tbody>` + d.rows.map(r=>`<tr class="${sel.has(r.id)?'sel':''}" ${on('click','openFile',r.file_id)}>${ck(r.id)}${nameCell(r.rel)}<td style="max-width:520px">${errorHelp(r.message, kb)}<span class="sub" style="font-family:var(--mono)">${esc(trW(r.message||''))}</span></td><td class="sub">${when(r.finished)}<br>${r.attempts}× tried</td></tr>`).join('') + '</tbody>' : '';
  setView(v, pageHead() + listCard({tabs, extra:filterBar(), body, total:d.total, cols:4, empty:{text:k==='encode'?'No failed encodes.':'No failed subtitle jobs.'}}));
}

const PLANS = [['','All'],['encode','Encoding'],['done','Done'],['efficient','Already efficient'],['small-gain','Small gain'],['no-gain','No gain'],['dolby-vision','Dolby Vision'],['disc','DVD/Blu-ray'],['restored','Restored'],['error','Error']];
let libPlan = '';
async function filesPage(v, seq, newDays, tabs=''){
  const plan = newDays ? '' : libPlan;
  const qs = new URLSearchParams({q:filter, offset:page*pageSize, limit:pageSize, ...(newDays?{new_days:newDays}:{}), ...(plan?{plan}:{})});
  const d = await api('/api/files?'+qs+sortQS()+fltQS());
  if (stale(seq)) return;
  bulk = {type:'files', total:d.total, allowAll:false, actions:[['Encoding','enc','primary'],['Subtitles','subs'],['Both','both']]};
  pageIds = d.rows.map(r=>r.id);
  const extra = (newDays ? '' : `<select data-change=\'["planSel"]\' class="${plan?'on':''}">${PLANS.map(([k,l])=>`<option value="${k}" ${k===plan?'selected':''}>Plan: ${l}</option>`).join('')}</select>`) + filterBar();
  const body = d.rows.length ? `<thead><tr>${ckHead()}${th('File','name')}${th('Source','size')}${th('Plan','plan')}${th('Subtitles','subs')}${newDays?th('Added','added','num'):''}</tr></thead><tbody>` + d.rows.map(r=>`<tr class="${sel.has(r.id)?'sel':''}" ${on('click','openFile',r.id)}>${ck(r.id)}${nameCell(r.rel)}<td>${fileInfo(r)}<div class="sub">${GB(r.size)} GB</div></td><td>${chip(r.plan)}</td><td>${langChips(r.subs_have)}</td>${newDays?`<td class="num sub">${ago(r.first_seen)}</td>`:''}</tr>`).join('') + '</tbody>' : '';
  const empty = newDays ? {text:'Nothing was added in the last 14 days.'} : plan ? {text:'No videos with this plan.'} : {text:OV.scan?.running?'The library is being scanned; videos appear as they are found.':'No videos found yet. Check the media folder and run a scan.', actions:`<button class="sm" data-click='["scanNow"]'>Scan now</button>`};
  setView(v, pageHead() + listCard({tabs, extra, body, total:d.total, cols:5, empty}));
}
R.library = (v,s,seq) => { const t = tab==='new' ? 'new' : 'all';
  return filesPage(v, seq, t==='new' ? 14 : 0, tabsHtml([['all','All'],['new','New (14 days)']], t, 'setTab')); };

function qExp(r, st){ const t = r.expires || (r.created + (+st.quarantine_days||7)*86400); const d = (t - Date.now()/1000)/86400; return `<div class="${d<1?'bad':'sub'}">${d<0?'expires today':d<1?'expires within a day':`expires in ${Math.ceil(d)} days`}</div>`; }
R.quarantine = async (v,s,seq) => {
  const [d, st] = await Promise.all([api(`/api/quarantine?q=${encodeURIComponent(filter)}&offset=${page*pageSize}&limit=${pageSize}${sortQS()}`), api('/api/settings')]);
  if (stale(seq)) return;
  const rows = d.rows;
  bulk = {type:'quar', total:d.total, allowAll:false, actions:[['Re-encode from original','redo','primary'],['Restore originals','restore']]};
  pageIds = rows.map(r=>r.id);
  const body = rows.length ? `<thead><tr>${ckHead()}${th('Original','name')}${th('Size','size','num')}${th('Replaced · expires','created','num')}</tr></thead><tbody>` + rows.map(r=>`<tr class="${sel.has(r.id)?'sel':''}" data-click=\'["rowCheck"]\'>${ck(r.id)}${nameCell(r.orig_rel)}<td class="num">${GB(r.size)} GB</td><td class="num sub">${when(r.created)}${qExp(r, st)}</td></tr>`).join('') + '</tbody>' : '';
  setView(v, pageHead() + `<div class="banner" style="color:var(--muted);border-color:var(--line2);background:var(--panel)">Originals are kept for at most ${st.quarantine_days.replace('.0','')} days and together never more than ${size(st.quarantine_max_gb*1e9)}. The oldest go first. Restoring deletes the new version.</div>` + listCard({body, total:d.total, cols:4, empty:{text:'Nothing in quarantine. Replaced originals are kept here until the keep time is over.'}}));
};

/* ============================== compare ============================== */
let cmpBusy = false;
async function cmpMake(id){ try { await post(`/api/compare/${id}/make`); toast('Comparison requested · ready within a minute'); } catch(e){ toast(e.message||'Not possible'); } lastHtml=''; render(true); }
async function cmpDelete(id, stay){ if (!await ask('Delete this comparison? The film itself stays as it is.', 'Delete', true)) return; await post(`/api/compare/${id}/delete`); toast('Comparison deleted');
  if (stay) { lastHtml=''; render(true); } else location.hash='compare/' + (CMP_TABS[MEM.compare?.tab] ? MEM.compare.tab : 'made'); }
const tc = t => { t=Math.round(t||0); const h=Math.floor(t/3600), m=Math.floor(t%3600/60), x=t%60; return `${h}:${String(m).padStart(2,'0')}:${String(x).padStart(2,'0')}`; };
function wipeHtml(id, sh, i){
  const u = n => `/api/compare/${num(id)}/img/${encodeURIComponent(String(n||''))}`;
  const why = {motion:'lots of motion', dark:'dark scene', quarter:'a quarter in', 'three-quarters':'three quarters in'}[sh.why] || '';
  return `<figure class="cmp"><div class="wipe" style="--p:50%"><img src="${u(sh.orig)}" alt="Original" loading="lazy"><img class="top" src="${u(sh.new)}" alt="New" loading="lazy">
    <span class="tag l">Original</span><span class="tag r">New</span><div class="wbar"></div>
    <input type="range" min="0" max="100" value="50" aria-label="Slide between original and new" data-input=\'["wipe"]\' data-click=\'["wipeToggle"]\'></div>
    <figcaption><span class="t">Frame ${i+1} · at ${tc(sh.t)}${why?' · '+why:''}</span><span class="btns"><button class="sm ghost" title="Show the original and the new version in turn: the best way to spot grain and detail" ${on('click','wipeFlip')}>Flip</button><button class="sm ghost" ${on('click','wipeHalf')} title="Slider back to the middle">50%</button><button class="sm ghost" ${on('click','wipeZoom')} title="Full width">Enlarge</button><span>${num(sh.orig_kb)} kB → ${num(sh.new_kb)} kB as jpg</span></span></figcaption></figure>`;
}
function daysLeft(t){ if (!t) return ''; const d = (t - Date.now()/1000) / 86400; return d < 1 ? 'less than a day left' : `${Math.ceil(d)} days left`; }
function cmpState(r){
  const made = r.cstatus==='done' ? `<span class="chip done" title="Four frames of the original and the new version are stored">Made${r.cmp_kb?' · '+(r.cmp_kb/1024).toLocaleString(LOC,{maximumFractionDigits:1})+' MB':''}</span> ${goBtn('compare-'+r.file_id,'View','sm primary')}` : '';
  if (made) return made;
  if (r.cstatus==='queued' || r.cstatus==='running') return '<span class="chip running" title="A worker is grabbing the frames">Being made…</span>';
  if (r.qstate==='held' || r.q_expires) return `<span class="chip" title="The original is still in quarantine, so the frames can still be grabbed">Can be made${r.q_expires?' · '+daysLeft(r.q_expires):''}</span> <button class="sm" ${on('click','cmpMakeStop',r.file_id)}>Make comparison</button>${r.cstatus==='failed'?`<div class="sub bad">previous attempt failed</div>`:''}`;
  return '<span class="chip muted" title="The original has left quarantine, so the frames can no longer be grabbed">No longer possible</span>';
}
const CMP_TABS = {made:'made', possible:'possible', all:'all'};
R.compare = async (v,s,seq) => {
  if (cmpId) return cmpDetail(v, seq, cmpId);
  const view0 = CMP_TABS[tab] ? tab : 'made';
  const d = await api(`/api/compare?view=${view0}&q=${encodeURIComponent(filter)}&offset=${page*pageSize}&limit=${pageSize}${sortQS()}`); if (stale(seq)) return;
  const rows = d.rows, c = d.counts || {};
  cmpBusy = rows.some(r=>r.cstatus==='queued'||r.cstatus==='running');
  const has = rows.filter(r=>r.cstatus && !['queued','running'].includes(r.cstatus)), can = rows.filter(r=>!r.cstatus && (r.qstate==='held' || r.q_expires));
  cmpIds = rows.filter(r=>r.cstatus==='done').map(r=>r.file_id);
  bulk = view0==='possible' && can.length ? {type:'cmpmake', total:d.total, allowAll:true, actions:[['Make comparison','cmpmake','primary']]}
       : has.length ? {type:'cmp', total:has.length, allowAll:false, actions:[['Delete comparison','cmpdel','danger']]} : null;
  pageIds = (view0==='possible' ? can : has).map(r=>r.file_id);
  const kb = d.cmp_kb || 0;
  const tabs = tabsHtml([['made','Comparisons',c.made],['possible','Can be made',c.possible],['all','All replaced films',c.all]], view0, 'setTab');
  const thumb = r => r.thumb ? `<img class="thumb" alt="" loading="lazy" src="/api/compare/${r.file_id}/img/${encodeURIComponent(r.thumb)}">` : '';
  const body = rows.length ? `<thead><tr>${bulk?ckHead():''}${th('Movie','name')}${th('Original → new','gain','num')}${th('Quality (VMAF)','vmaf','num')}${th('Replaced','finished')}${th('Comparison','')}</tr></thead><tbody>` + rows.map(r=>`<tr ${r.cstatus==='done'?`${on('click','go','compare-'+r.file_id)}`:'style="cursor:default"'}>
      ${bulk?(pageIds.includes(r.file_id)?ck(r.file_id):'<td class="ck"></td>'):''}<td><div class="fnrow">${thumb(r)}<div><div class="fn">${esc(base(r.rel))}</div><div class="sub">${esc(folder(r.rel))}</div></div></div></td>
      <td class="num">${GB(r.src_size)} → <b>${GB(r.out_size)} GB</b> <span class="good">−${pct(r.src_size-r.out_size,r.src_size)}%</span></td>
      <td class="num">${num(r.vmaf)}<div class="sub">worst 5% ${num(r.p5)}</div></td>
      <td class="sub">${when(r.finished)}</td><td><div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">${cmpState(r)}${pageIds.includes(r.file_id)?`<button class="sm ghost" title="Delete comparison" ${on('click','cmpDeleteStop',r.file_id)}>Remove</button>`:''}</div></td></tr>`).join('') + '</tbody>' : '';
  const empty = view0==='made' ? {text:'No comparisons yet. Make one while the original is still in quarantine, or switch on automatic comparisons in Settings.', actions:`${c.possible?`<button class="sm" ${on('click','tab','setTab','possible')}>Show films that can be compared</button>`:''}${goBtn('settings','Open settings')}`}
    : view0==='possible' ? {text:'No film can be compared right now: the originals have left quarantine, or nothing has been replaced yet.'} : {text:'Replaced films appear here after their first encode.'};
  setView(v, pageHead() + `<div class="banner" style="color:var(--muted);border-color:var(--line2);background:var(--panel)">A comparison takes 4 frames from the original and the new version (about 1–2 MB in total). It can be made while the original is still in quarantine, and stays until you delete it, even after the original is gone.${kb?` <span>In use now:</span> <b>${(kb/1024).toLocaleString(LOC,{maximumFractionDigits:1})} MB</b>`:''}</div>` + listCard({tabs, body, total:d.total, cols:6, empty}));
};
async function cmpDetail(v, seq, id){
  if (!cmpIds.length) { try { const l = await api('/api/compare?limit=1000'); cmpIds = (l.rows||l).filter(r=>r.cstatus==='done').map(r=>r.file_id); } catch {} }
  const d = await api(`/api/compare/${id}`); if (stale(seq)) return;
  const f=d.file, e=d.encode||{}, R2=e.result||{}, c=d.compare, sr=(d.subs?.result?.made)||[];
  cmpBusy = c && ['queued','running'].includes(c.status);
  const tile=(k,val,sub,cls='')=>`<div class="card kpi ${cls}"><div class="l">${k}</div><div class="v">${val}</div><div class="s">${sub}</div></div>`;
  const okN = R2.vmaf_mean>=OV_NORM().m && R2.vmaf_p5>=OV_NORM().p;
  const shots = c?.status==='done' ? (c.result?.shots||[]) : [];
  const steps = (R2.search||[]).map(t=>`<tr><td class="n">cq ${num(t.cq)}</td><td class="num">${num(t.mean)}</td><td class="num">${num(t.p5)}</td><td class="num">${(+t.kbps/1000||0).toLocaleString(LOC,{maximumFractionDigits:1})} Mbps</td><td>${t.cq===R2.cq?'<span class="chip done">chosen</span>':''}</td></tr>`).join('');
  setView(v, `<div class="ph"><div class="ti"><h1>${esc(base(f.rel))}</h1><p>${esc(folder(f.rel))} · ${res(f.width,f.height)}${f.hdr?' · HDR':''}</p></div>
      <div class="cmpnav">${goBtn('compare','Back','ghost sm back')}${cmpIds.length>1&&cmpIds.includes(id)?`<span class="pill" title="Previous / next film (arrow keys)"><button ${cmpIds.indexOf(id)<=0?'disabled':''} ${on('click','go','compare-'+cmpIds[cmpIds.indexOf(id)-1])} aria-label="Previous film">‹</button><span>${cmpIds.indexOf(id)+1} / ${cmpIds.length}</span><button ${cmpIds.indexOf(id)>=cmpIds.length-1?'disabled':''} ${on('click','go','compare-'+cmpIds[cmpIds.indexOf(id)+1])} aria-label="Next film">›</button></span>`:''}${d.original==='held'?`<button class="sm" ${on('click','cmpMake',id)}>Make again</button>`:''}<button class="sm danger" ${on('click','cmpDelete',id)}>Remove</button></div></div>
    <div class="grid" style="gap:16px"><div class="grid kpis">${tile('Original', size(e.src_size), R2.encoder?esc(R2.encoder):'')}${tile('New', size(e.out_size), `HEVC · cq ${num(R2.cq)}`)}${tile('Saved', pct(e.src_size-e.out_size,e.src_size)+'%', size(e.src_size-e.out_size), 'acc')}${tile('VMAF whole film', num(R2.vmaf_mean), `worst 5% ${num(R2.vmaf_p5)} · norm ${OV_NORM().m} / ${OV_NORM().p}`, okN?'good':'')}</div>
    ${cmpBusy?`<div class="banner" style="margin:0">Making the frames… this usually takes less than a minute.</div>`:''}
    ${c?.status==='failed'?`<div class="banner" style="margin:0;color:var(--bad);border-color:var(--bad);background:var(--bad-s)">Making frames failed: ${esc(trW(c.message||''))}</div>`:''}
    ${!c && d.original==='held'?`<div class="banner" style="margin:0;display:flex;justify-content:space-between;align-items:center;gap:12px"><span>There is no picture comparison for this film yet.</span><button class="sm primary" ${on('click','cmpMake',id)}>Make comparison</button></div>`:''}
    ${shots.length?`<div class="card"><h3>Picture · drag across the image</h3>${c.result?.tonemapped?'<p class="sub" style="margin:0 0 10px">HDR has been converted to SDR for this view; your TV shows it in HDR.</p>':''}<div class="shots">${shots.map((sh,i)=>wipeHtml(id,sh,i)).join('')}</div></div>`:''}
    <div class="grid2">
      <div class="card"><h3>How the setting was chosen</h3>${steps?`<table class="mini"><thead><tr><th>Step</th><th class="num">VMAF</th><th class="num">Weakest 5%</th><th class="num">Bitrate</th><th></th></tr></thead><tbody>${steps}</tbody></table>`:'<p class="sub">No data.</p>'}
        <p class="sub" style="margin-top:10px">Encode time ${num(R2.encode_min)} min · measured on 8 fragments, then checked over the whole film.</p></div>
      <div class="card"><h3>Subtitles</h3>${sr.length?sr.map(m=>`<div class="row1"><span class="t"><b>${esc(String(m.lang||'').toUpperCase())}</b> <span class="sub">${esc(m.source)}</span></span><span class="w">timing ${Math.round((m.score||0)*100)}%</span></div>`).join(''):`<p class="sub">${d.subs?'Nothing needed, all languages were present.':'Not processed yet.'}</p>`}
        <p class="sub" style="margin-top:10px">Present now: ${(f.subs_have||[]).map(x=>x.toUpperCase()).join(', ')||'–'}</p></div>
    </div></div>`);
}
function OV_NORM(){ return {m: OV?.norm?.min_mean ?? 96.5, p: OV?.norm?.min_p5 ?? 93}; }

/* ============================== DVD / Blu-ray ============================== */
const DISC_ST = {found:['chip','found'], queued:['chip running','queued'], running:['chip running','converting…'], done:['chip done','converted'],
  failed:['chip failed','failed'], skipped:['chip','skipped']};
const DISC_STEPS = ['Finding the film on the disc','Converting to one .mkv','Checking (duration, tracks)','Copying to the NAS','Old folder to quarantine'];
const mins = s => s==null ? '' : s < 90 ? `${Math.max(1,Math.round(s))} s` : s < 5400 ? `${Math.round(s/60)} min` : `${(s/3600).toFixed(1)} h`;
function discRunHtml(d){
  const stale = d.seen && Date.now()/1000 - d.seen > 90;
  const steps = DISC_STEPS.map((t,i)=>{
    const st = i < d.step ? 'done' : i===d.step ? 'now' : 'todo';
    const extra = i < d.step ? (d.steps?.[i]||'') : i===d.step ? [d.message, d.eta!=null?'~'+mins(d.eta)+' left':''].filter(Boolean).join(' · ') : '';
    return `<div class="dstep ${st}"><span class="dm">${st==='done'?'✓':st==='now'?'<i></i>':''}</span><span class="dt">${t}</span><span class="sub">${esc(trW(extra))}</span></div>`; }).join('');
  return `<div class="job" style="cursor:default">
    <div class="row1" style="border:0;padding:0"><span class="t n">${esc(base(d.folder))} <span class="sub">· ${d.kind==='bd'?'Blu-ray':'DVD'} · ${esc(d.worker||'')}</span></span><span class="chip running">${esc(trW(d.stage))}</span></div>
    <div class="m"><span>${d.started?'running for '+mins(Date.now()/1000-d.started):''}${stale?' · <span class="bad">worker has not reported for a while</span>':''}</span><span>${Math.round(d.progress||0)}%</span></div>
    <div class="bar"><i style="width:${d.progress||0}%"></i></div>
    <div class="dsteps">${steps}</div></div>`;
}
R.discs = async (v,s,seq) => {
  const D = await api('/api/discs'); if (stale(seq)) return;
  const rows = clientSort(filter ? D.rows.filter(r=>matches(r.folder, filter)) : D.rows, {name: r=>r.folder, type: r=>r.kind, size: r=>r.size, status: r=>r.status}), running = D.running;
  bulk = rows.length ? {type:'disc', total:rows.length, allowAll:false, actions:[['Convert to MKV','convert','primary'],['Skip','skip'],['Undo skip','reset']]} : null;
  pageIds = rows.map(r=>r.id);
  const body = rows.length ? `<thead><tr>${ckHead()}${th('Folder','name')}${th('Type','type')}${th('Size','size','num')}${th('Status','status')}${th('Result','')}</tr></thead><tbody>` + rows.map(r=>{
      const st = DISC_ST[r.status]||['chip',esc(r.status)]; const R2 = r.result||{};
      const res = r.status==='done' ? `<div>${esc(base(R2.new_rel||''))}</div><div class="sub">${size(R2.new_size)} · ${(R2.streams||[]).filter(x=>x.type==='audio').length} audio · ${(R2.streams||[]).filter(x=>x.type==='subtitle').length} subtitles</div>`
        : r.status==='running' ? (()=>{ const d = running.find(x=>x.id===r.id)||{}; return `<div class="bar" style="width:140px"><i style="width:${d.progress||0}%"></i></div><div class="sub" style="margin-top:4px"><span>${esc(trW(d.stage||''))}</span> · ${Math.round(d.progress||0)}%</div>`; })()
        : `<span class="sub">${esc(trW(r.message||''))}</span>`;
      return `<tr data-click=\'["rowCheck"]\'>${ck(r.id)}<td><div class="fn">${esc(base(r.folder))}</div><div class="sub">${esc(folder(r.folder))}</div></td>
        <td>${r.kind==='bd'?'Blu-ray':'DVD'}</td><td class="num">${size(r.size)}</td><td><span class="${st[0]}">${st[1]}</span></td><td>${res}</td></tr>`; }).join('') + '</tbody>' : '';
  const now = running.length ? `<div class="card" style="margin-bottom:16px"><h3 style="margin:0 0 10px">Now running</h3>${running.map(discRunHtml).join('')}</div>` : '';
  setView(v, pageHead() + now + `<div class="banner" style="color:var(--muted);border-color:var(--line2);background:var(--panel)">Converting copies everything 1:1 into one .mkv: video (HDR too), all audio tracks, subtitles and chapters. For a DVD it picks the longest title (the film). The old folder goes to quarantine, so you can restore it. The .mkv then goes through normal encoding and subtitles. Dolby Vision Blu-rays become Dolby Vision profile 8.1 (base layer 1:1, DV metadata added): Plex and your TV play it as Dolby Vision, other players as HDR10. Such a film is not re-encoded afterwards, since that would lose Dolby Vision.</div>` + listCard({body, total:rows.length, pager:false, cols:6, empty:{text:'No DVD or Blu-ray folders (VIDEO_TS, BDMV) were found in the library.'}}));
};

const RESOPTS = [[0,'All'],[2160,'up to 4K'],[1440,'up to 1440p'],[1080,'up to 1080p'],[720,'up to 720p'],[480,'up to SD']];
let showAdd = false;
function workerAddHtml(){
  return `<div class="card"><h3>Add worker</h3>
    <ol class="steps">
      <li><b>Connect the media share on the new machine.</b><span class="hint">Windows: drive letter or \\\\NAS\\Media · Mac: Finder › Connect to Server · Linux: see the instructions in the package.</span></li>
      <li><b>Download the package for that system and unpack it.</b><span class="btns">${(WS?.downloads||[]).map(d=>`<a href="${esc(d.file)}" download><button class="sm">${esc(d.name)}</button></a>`).join(' ')}</span></li>
      <li><b>Run the installer.</b><span class="hint">Windows: Install-Worker.bat · Mac/Linux: bash install.sh</span><span class="hint">The installer asks for the details below and tests the connection.</span></li>
    </ol>
    <div class="srow"><div><div class="lbl">Dashboard address</div><div class="hint">Use the IP address of the NAS, not localhost.</div></div><code>${esc(location.origin)}</code></div>
    <div class="srow"><div><div class="lbl">Worker key</div><div class="hint">Keep secret: with this key a worker can fetch jobs.</div></div>
      <div class="btns"><code id="wtok">••••••••••••</code><button class="sm" data-click=\'["showTok"]\'>Show</button><button class="sm" data-click=\'["copyTok"]\'>Copy</button>${WS.token_fixed?'':'<button class="sm ghost" data-click=\'["secModal","token"]\'>Renew</button>'}</div></div>
    <div class="srow"><div><div class="lbl">Minimum requirements</div><div class="hint"><b>Windows/Linux:</b> NVIDIA GTX 1660 Super/RTX 2060 or newer, Intel Arc or 11th-gen+ iGPU, AMD RX 6000+ · 4 cores · 8 GB · 100 GB free · wired network. Subtitles: NVIDIA with 8 GB+.<br><b>macOS:</b> Apple Silicon (M1+) · macOS 13+ · 16 GB (subtitles: 24 GB+) · 150 GB free · Homebrew.</div></div><span></span></div>
    <div class="srow"><div><div class="lbl">Worker version</div><div class="hint">Workers update themselves when this dashboard has a new version.</div></div><code>${esc(WS?.version||'–')}</code></div>
  </div>`;
}
function capChips(w){ const c = w.caps||{}, i = w.info||{}; const enc = i.encoder ? ({nvenc:'NVENC', qsv:'Quick Sync', amf:'AMF', vaapi:'VAAPI', videotoolbox:'VideoToolbox', x265:'x265'}[String(i.encoder).split(' ')[0]] || i.encoder) : '';
  return [c.encode && enc ? `<span class="chip" title="GPU encoder found">${esc(enc)}</span>` : '', c.cpu ? '<span class="chip" title="Can encode on the CPU with x265">x265</span>' : '', c.subs ? '<span class="chip" title="Whisper installed: can make subtitles">Whisper</span>' : '', i.ollama || (i.models&&i.models.ollama) ? '<span class="chip" title="Ollama found: can translate subtitles">Ollama</span>' : ''].filter(Boolean).join(' ') || '<span class="sub">no capabilities reported</span>'; }
R.workers = async (v,s,seq) => {
  const [ws, setup] = await Promise.all([api('/api/workers'), api('/api/worker-setup')]); if (stale(seq)) return; WS = setup;
  const sw = (w, k) => { const can = w.caps[k]; const isOn = can && w.cfg[k];
    return can ? `<div class="sw ${isOn?'on':''}" role="switch" aria-checked="${isOn}" aria-label="${esc(k)}" tabindex="0" ${on('click','workerFlag',w.name,k,!isOn)}><span class="t"></span></div>` : `<span class="sub" title="This machine lacks the hardware or software for this">not supported here</span>`; };
  const body = ws.length ? `<thead><tr><th>Worker</th><th>Hardware</th><th>Encoding</th><th>CPU encoding</th><th>Subtitles</th><th>Resolution</th><th>Working on</th><th></th></tr></thead><tbody>` + ws.map(w=>{
    const i = w.info||{};
    return `<tr style="cursor:default">
      <td><div class="fn"><span class="dot ${w.online?'ok':'bad'}" style="display:inline-block;margin-right:8px"></span>${esc(w.name)}${w.update==='available'?' <span class="chip running" title="The server hands out newer worker files; the worker installs them after its current job">update pending</span>':''}</div><div class="sub" title="${esc(when(w.last_seen))}">${w.online?'online':'seen '+ago(w.last_seen)}${i.os?' · '+esc(i.os):''}${w.version?' · '+esc(w.version):''}</div></td>
      <td><div>${esc(i.gpu||'–')}</div><div class="caps">${capChips(w)}</div></td>
      <td data-l="Encoding">${sw(w,'encode')}</td><td data-l="CPU encoding">${sw(w,'cpu')}</td><td data-l="Subtitles">${sw(w,'subs')}</td>
      <td data-l="Resolution">${w.caps.encode?`<select ${on('change','workerHeight',w.name)}>${RESOPTS.map(([k,l])=>`<option value="${k}" ${+(w.cfg.max_height||0)===k?'selected':''}>${l}</option>`).join('')}</select>`:'<span class="sub">–</span>'}</td>
      <td data-l="Working on">${w.jobs.length?w.jobs.map(j=>`<div class="sub"><span class="acc">${j.kind==='subs'?'subtitles':j.cpu?'CPU encoding':'encode'}</span> ${esc(base(j.rel))} · ${Math.round(j.progress||0)}%</div>`).join(''):'<span class="sub">nothing</span>'}</td>
      <td class="act">${w.online?'':`<button class="sm ghost" ${on('click','forgetWorker',w.name)}>Remove</button>`}</td></tr>`;}).join('') + '</tbody>' : '';
  const add = !ws.length || showAdd;
  setView(v, pageHead(ws.length ? `<button data-click=\'["showAdd"]\'>${showAdd?'Close':'Add worker'}</button>` : '') +
    (add ? workerAddHtml() : '') +
    (ws.length ? `<div class="card flush" style="margin-top:${add?16:0}px"><div class="tscroll"><table>${body}</table></div></div>
    <p class="sub" style="margin-top:12px">Each worker reports what it can do (encoder found, Whisper installed). Here you decide what it may do. Changes apply from the next job; a running job is finished first.</p>` : ''));
};
async function setWorker(name, patch){ await post('/api/workers/'+encodeURIComponent(name), patch); toast('Settings saved'); lastHtml=''; render(); refresh(); }
async function forgetWorker(name){ if (!await ask(`Remove worker ${name} from the list? If it comes back online it reappears automatically.`, 'Remove')) return; await post('/api/workers/'+encodeURIComponent(name)+'/forget'); lastHtml=''; render(); refresh(); }

let actRows = [], actMore = false, cmpIds = [];
const ACT_TABS = {all:'', warn:'warn', error:'error'};
const actQS = () => `&level=${ACT_TABS[tab]||''}&q=${encodeURIComponent(filter)}`;
R.activity = async (v,s,seq) => {
  const lv = ACT_TABS[tab] !== undefined ? tab : 'all';
  if (!actRows.length) { const d = await api(`/api/events?limit=100${actQS()}`); if (stale(seq)) return; actRows = d.rows; actMore = d.more; }
  const grouped = []; for (const e of actRows) { const p = grouped[grouped.length-1]; if (p && p.msg === e.msg && p.level === e.level && p.ts - e.ts < 6*3600) p.n = (p.n||1) + 1; else grouped.push({...e}); }
  setView(v, pageHead() + `<div class="card flush"><div class="toolbar"><div class="search">${icon('search')}<input type="search" placeholder="Search the log…" value="${esc(filter)}" data-input=\'["search"]\'></div>${tabsHtml([['all','All'],['warn','Warnings'],['error','Errors']], lv, 'setTab')}</div>
    <div class="events" style="max-height:none;padding:6px 16px">${actRows.length?eventsHtml(grouped):`<div class="empty">${filter||lv!=='all'?'Nothing in the log matches this.':'Nothing has happened yet.'}</div>`}</div>
    ${actMore?`<div style="text-align:center;padding:14px"><button class="sm" data-click=\'["loadMore"]\'>Load older entries</button></div>`:''}</div>`);
};
async function actLoadMore(btn){ const d = await api(`/api/events?limit=200&before=${actRows[actRows.length-1].id}${actQS()}`); actRows = actRows.concat(d.rows); actMore = d.more; lastHtml=''; render(); }

/* ============================== info: hardware, ratings, tools, knowledge base ============================== */
let KBC = {lang: null, data: null};
const KB_LANGS = ['en', 'nl'];           // languages with their own knowledge base; the rest read the English one
async function loadKB(){
  const lang = KB_LANGS.includes(PREF.lang) ? PREF.lang : 'en'; if (KBC.lang === lang && KBC.data) return KBC.data;
  const v = document.documentElement.dataset.v || ''; let d = null;
  for (const l of [...new Set([lang, 'en'])]) { try { const r = await fetch(`/static/kb/${l}.json?v=${v}`, {credentials: 'same-origin'}); if (r.ok) { d = await r.json(); break; } } catch {} }
  KBC = {lang, data: d}; return d || {rate: {}, tools: {}, groups: []};
}
// ratings: a plain verdict per role, from what the worker reports (the sentences live in the knowledge base files)
function rateGpu(w){ const g = (w.gpu || '').toLowerCase(), k = w.enc_key || '';
  if (!w.caps.encode || !k || k.startsWith('x265')) return 'low';
  if (k === 'nvenc') return /quadro/.test(g) ? 'high' : /rtx ?(40|50)\d\d|rtx (6000|5000|4500|4000) ada|blackwell/.test(g) ? 'ultra' : /rtx|titan|\ba\d{4}\b/.test(g) ? 'high' : 'medium';
  if (k === 'qsv') return /\barc\b/.test(g) ? 'high' : 'medium';
  if (k === 'amf') return /rx ?[79]\d{3}/.test(g) ? 'high' : 'medium';
  if (k === 'videotoolbox') return /\b(max|ultra)\b/.test(g) ? 'ultra' : 'high';
  return 'medium'; }
// 'unknown' = the worker has not reported this yet (older worker, or no nvidia-smi for the VRAM): no verdict rather than a wrong one
function rateCpu(w){ const t = w.hw.threads; return !w.caps.cpu ? 'off' : !t ? 'unknown' : t >= 24 ? 'ultra' : t >= 16 ? 'high' : t >= 8 ? 'medium' : 'low'; }
function rateSubs(w){ const v = w.hw.vram_gb; return !w.caps.subs ? 'off' : v == null ? 'unknown' : v >= 12 ? 'ultra' : v >= 8 ? 'high' : v >= 6 ? 'medium' : 'low'; }
function rateLlm(w){ const m = w.models.ollama || ''; if (!w.caps.subs || !m) return 'none';
  const b = parseFloat((m.match(/(\d+(?:\.\d+)?)b\b/i) || [])[1]) || 8, v = w.hw.vram_gb;     // ~0.75 GB per billion parameters (4-bit) + Whisper
  return v == null ? 'unknown' : v >= 24 ? 'ultra' : v >= b * 0.75 + 5 ? 'high' : v >= b * 0.75 ? 'medium' : 'low'; }
const ratePill = r => r === 'none' ? '' : `<span class="rate ${r}">${{low: 'Low', medium: 'Medium', high: 'High', ultra: 'Ultra', unknown: 'Unknown', off: 'Off'}[r]}</span>`;
const kbText = t => esc(t).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>');
function hwCell(k, v, s){ return `<div><div class="k">${k}</div><div class="v">${v || '–'}</div>${s ? `<div class="s">${s}</div>` : ''}</div>`; }
function rateBox(label, r, kb, key){ return `<div><div class="t"><span>${label}</span>${ratePill(r)}</div><div class="why">${esc(kb.rate[`${key}.${r}`] || '')}</div></div>`; }
function workerInfoHtml(w, kb){
  const h = w.hw || {}, m = w.models || {}, vr = w.versions || {};
  const gpuSub = [h.vram_gb ? `${num(h.vram_gb)} GB <span>${w.os === 'macOS' ? 'unified memory' : 'VRAM'}</span>` : '', h.gpu_driver ? `<span>driver</span> ${esc(h.gpu_driver)}` : ''].filter(Boolean).join(' · ');
  const sw = [vr.ffmpeg ? `ffmpeg ${esc(vr.ffmpeg)}` : '', w.encoder ? esc(w.encoder) : ''].filter(Boolean).join(' · ');
  const sw2 = [w.caps.subs ? `Whisper ${esc(m.whisper || '')}` : '<span>no Whisper</span>', m.ollama ? `Ollama ${esc(m.ollama)}` : ''].filter(Boolean).join(' · ');
  const llm = rateLlm(w);
  return `<div class="card"><h3>${esc(w.name)} <span class="r"><span class="dot ${w.online ? 'ok' : 'bad'}" style="display:inline-block;margin-right:6px"></span><span>${w.online ? 'online' : 'seen ' + ago(w.last_seen)}</span>${(h.os_version&&String(h.os_version).startsWith(w.os||'')?h.os_version:[w.os, h.os_version].filter(Boolean).join(' '))?' · '+esc(h.os_version&&String(h.os_version).startsWith(w.os||'')?h.os_version:[w.os, h.os_version].filter(Boolean).join(' ')):''} · <span>worker</span> ${esc(w.version || '?')}</span></h3>
    ${h.cpu || w.gpu ? `<div class="hw">
      ${hwCell('CPU', esc(h.cpu || ''), h.threads ? `${h.cores ? num(h.cores) + ' <span>cores</span> · ' : ''}${num(h.threads)} <span>threads</span>` : '')}
      ${hwCell('GPU', esc(w.gpu || ''), gpuSub)}
      ${hwCell('Memory', h.ram_gb ? `${num(h.ram_gb)} GB` : '', '')}
      ${hwCell('Software', sw, sw2)}
    </div>` : `<div class="sub">Hardware details appear after this worker has updated itself.</div>`}
    <div class="rates">
      ${rateBox('GPU encoding', rateGpu(w), kb, 'gpu')}${rateBox('CPU encoding (x265)', rateCpu(w), kb, 'cpu')}
      ${rateBox('Subtitles (Whisper)', rateSubs(w), kb, 'subs')}${rateBox('Translation (Ollama)', llm, kb, 'llm')}
    </div></div>`;
}
function toolsHtml(d, kb){
  const ws = d.workers, first = k => (ws.map(w => (w.versions || {})[k]).find(Boolean)) || '';
  const encs = [...new Set(ws.map(w => w.enc_key).filter(k => k && !k.startsWith('x265')))];
  const rows = [['FFmpeg', first('ffmpeg'), 'ffmpeg'], ['libvmaf', '', 'libvmaf'],
    ...encs.map(k => [{nvenc: 'NVIDIA NVENC', qsv: 'Intel Quick Sync', amf: 'AMD AMF', vaapi: 'VA-API', videotoolbox: 'Apple VideoToolbox'}[k] || k, '', k]),
    ...(ws.some(w => w.caps.cpu || w.enc_key === 'x265') ? [['x265', '', 'x265']] : []),
    ...(ws.some(w => w.caps.subs) ? [['faster-whisper', [...new Set(ws.map(w => (w.models || {}).whisper).filter(Boolean))].join(', '), 'whisper'],
      ['Ollama', [...new Set(ws.map(w => (w.models || {}).ollama).filter(Boolean))].join(', '), 'ollama'], ['OpenSubtitles', '', 'opensubtitles'], ['ffsubsync', '', 'ffsubsync']] : []),
    ['MKVToolNix', first('mkvtoolnix'), 'mkvtoolnix'], ['dovi_tool', first('dovi_tool'), 'dovi_tool']];
  return `<div class="tools">${rows.map(([n, v, k]) => `<div><b>${esc(n)}</b><span>${[v ? esc(v) : '', esc(kb.tools[k] || '')].filter(Boolean).join(' · ')}</span></div>`).join('')}</div>`;
}
R.info = async (v, s, seq) => {
  const [d, kb] = await Promise.all([api('/api/info'), loadKB()]); if (stale(seq)) return;
  const sv = d.server, online = d.workers.filter(w => w.online).length;
  const srvRate = sv.disk_free && d.settings.min_free_gb && sv.disk_free < d.settings.min_free_gb * 1e9 ? 'disk' : 'high';
  const kpi = (l, val, sub) => `<div class="kpi"><div class="l">${l}</div><div class="v">${val}</div><div class="s">${sub}</div></div>`;
  const html = pageHead() + `<div class="kpis4">
      ${kpi('Server', esc(sv.version), `Docker · <span>up</span> ${dur(sv.uptime)}`)}
      ${kpi('Worker', esc(d.worker_version || '–'), `${nf(online)} / ${nf(d.workers.length)} <span>online</span>`)}
      ${kpi('Database', `${nf(sv.files)} <span>files</span>`, sv.db_bytes ? `${(sv.db_bytes / 1e6).toLocaleString(LOC, {maximumFractionDigits: sv.db_bytes < 1e7 ? 1 : 0})} MB` : '')}
      ${kpi('Free on NAS', size(sv.disk_free), sv.disk_total ? `<span>of</span> ${size(sv.disk_total)}` : '')}
    </div>
    ${d.workers.map(w => workerInfoHtml(w, kb)).join('')}
    <div class="card"><h3>Server <span class="r">${sv.cpu ? '' : 'Docker'}</span></h3><div class="hw">
      ${hwCell('CPU', esc(sv.cpu || ''), sv.threads ? `${sv.cores ? num(sv.cores) + ' <span>cores</span> · ' : ''}${num(sv.threads)} <span>threads</span>` : '')}
      ${hwCell('Memory', sv.ram_gb ? `${num(sv.ram_gb)} GB` : '', sv.rss_mb ? `MediaScan <span>uses</span> ${num(sv.rss_mb)} MB` : '')}
      ${hwCell('Media share', size(sv.disk_total), `${size(sv.disk_free)} <span>free</span>`)}
      ${hwCell('Rating', ratePill(srvRate === 'disk' ? 'medium' : 'high'), esc(kb.rate['srv.' + srvRate] || ''))}
    </div></div>
    <div class="card"><h3>Tools in use</h3>${toolsHtml(d, kb)}</div>
    <div class="card"><h3>Knowledge base</h3>${kb.groups.map(g => `<div class="kbcat"><h4>${esc(g.t)}</h4>${g.q.map((x, i) => `<details class="kb"><summary>${esc(x.q)}</summary><div class="a">${kbText(x.a)}</div></details>`).join('')}</div>`).join('')}</div>`;
  setView(v, html);
};

/* ============================== detail drawer ============================== */
function closeDrawer(){ const d = $('#drawer'); if (!d.classList.contains('on')) return; d.classList.remove('on'); d.setAttribute('aria-hidden', 'true'); $('#scrim').classList.remove('on'); if (closeDrawer._back && document.contains(closeDrawer._back)) closeDrawer._back.focus(); closeDrawer._back = null; }
function timelineHtml(j, q){
  const steps = [['Queued', j.created], ['Started', j.started], [j.status==='done'||j.status==='replacing'?'Encoded':j.status==='failed'?'Failed':'Finished', j.finished], ['Replaced', q?.created]].filter(([,t])=>t);
  if (!steps.length) return '';
  return `<div class="tl">${steps.map(([l,t],i)=>`<div><span class="dot"></span><b>${l}</b><span class="sub">${when(t)}${i?` · ${dur(Math.max(0, t - steps[i-1][1]))} ${i===1?'waiting':'later'}`:''}</span></div>`).join('')}</div>`;
}
async function openFile(id){
  if (!id) return;
  const f = await api('/api/file/'+id);
  $('#dr-sub').textContent = folder(f.rel); $('#dr-title').textContent = base(f.rel);
  const enc = f.jobs.find(j=>j.kind==='encode'), sub = f.jobs.find(j=>j.kind==='subs');
  const R2 = enc?.result || {};
  const search = (R2.search||[]).map(t=>`<tr class="${t.cq===R2.cq?'chosen':''}"><td>cq ${num(t.cq)}${t.cq===R2.cq?' <span class="chip done">chosen</span>':''}</td><td class="num">${num(t.mean)}</td><td class="num">${num(t.p5)}</td><td class="num">${(+t.kbps/1000||0).toFixed(1)} Mbps</td></tr>`).join('');
  const made = (sub?.result?.made||[]).map(m=>`<div class="row1"><span class="t"><span class="lang made">${esc(String(m.lang||'').toUpperCase())}</span> ${esc(m.source)}</span><span class="w">${m.score!=null?'timing '+num(m.score):''}</span></div>`).join('');
  const q = f.quarantine.find(x=>x.state==='held');
  $('#dr-body').innerHTML = `
    <div class="sec"><h4>File</h4><dl class="kv">
      <dt>Plan</dt><dd>${chip(f.plan)}${f.plan_reason?`<div class="sub" style="margin-top:4px">${esc(trW(f.plan_reason))}</div>`:''}</dd>
      <dt>Video</dt><dd>${fileInfo(f)||'–'}</dd>
      <dt>Size</dt><dd>${size(f.size)}</dd>
      <dt>Duration</dt><dd>${f.duration?Math.round(f.duration/60)+' min':'–'}</dd>
      <dt>Audio</dt><dd>${(Array.isArray(f.audio)?f.audio:[]).map(a=>esc(String(a).replace(':',' · '))).join('<br>')||'–'}</dd>
      <dt>Subtitles</dt><dd>${langChips(f.subs_have)}</dd>
      <dt>Added</dt><dd>${when(f.first_seen)}</dd>
      ${f.probe_err?`<dt>Error</dt><dd class="bad">${esc(f.probe_err)}</dd>`:''}
    </dl></div>
    ${enc?`<div class="sec"><h4>Encode</h4><dl class="kv">
      <dt>Status</dt><dd>${chip(enc.status)}${cpuTag(enc)}${enc.stage&&['running','replacing'].includes(enc.status)?' <span class="sub">'+esc(trW(enc.stage))+(enc.message?' · '+esc(trW(enc.message)):'')+'</span>':''}</dd>
      ${R2.out_size?`<dt>Result</dt><dd>${GB(R2.src_size)} → <b>${GB(R2.out_size)} GB</b> <span class="good">−${pct(R2.src_size-R2.out_size,R2.src_size)}%</span></dd>
      <dt>Quality</dt><dd>VMAF ${num(R2.vmaf_mean)} · worst 5% ${num(R2.vmaf_p5)} · cq ${num(R2.cq)}${R2.encoder==='x265-grain'?` · x265 ${esc(R2.preset||'')}`:''}</dd>
      <dt>Encode time</dt><dd>${num(R2.encode_min)} min</dd>`:''}
      ${enc.message?`<dt>Message</dt><dd>${esc(trW(enc.message))}</dd>`:''}
    </dl>${timelineHtml(enc, q || f.quarantine[0])}${search?`<table class="mini" style="margin-top:10px"><tr><td class="sub">Step</td><td class="num sub">VMAF</td><td class="num sub">weakest 5%</td><td class="num sub">video</td></tr>${search}</table>`:''}</div>`:''}
    ${sub?`<div class="sec"><h4>Subtitles</h4><dl class="kv"><dt>Status</dt><dd>${chip(sub.status)}</dd>${sub.message?`<dt>Message</dt><dd>${esc(trW(sub.message))}</dd>`:''}</dl>${made?`<div style="margin-top:8px">${made}</div>`:''}</div>`:''}
    <div class="sec actions">
      ${enc&&enc.status==='queued'?`<button class="primary sm" ${on('click','actClose',enc.id,'prioritize')}>Prioritise</button><button class="sm" ${on('click','actClose',enc.id,'skip')}>Skip</button>`:''}
      ${enc&&['failed','no-gain','skipped','cancelled'].includes(enc.status)?`<button class="sm" ${on('click','actClose',enc.id,'retry')}>Retry</button>`:''}
      ${enc&&['failed','no-gain','skipped','cancelled'].includes(enc.status)&&OV.workers.some(w=>w.cfg&&w.cfg.cpu)?`<button class="sm" title="x265 with grain tuning on the worker's CPU: keeps grain far more efficiently, but takes hours per film" ${on('click','actClose',enc.id,'retry-cpu')}>Retry on CPU (x265)</button>`:''}
      ${!enc&&!['done','error','disc'].includes(f.plan)?`<button class="sm" ${on('click','queueOne',f.id,'encode')}>Encoding</button>`:''}
      ${sub&&['not-found','failed'].includes(sub.status)?`<button class="sm" ${on('click','actClose',sub.id,'retry')}>Retry</button>`:''}
      ${!sub&&f.plan!=='disc'?`<button class="sm" ${on('click','queueOne',f.id,'subs')}>Find subtitles</button>`:''}
      ${q?`<button class="sm" ${on('click','redoOne',q.id)}>Re-encode from original</button><button class="sm danger" ${on('click','restoreOne',q.id)}>Restore original</button>`:''}
    </div>`;
  closeDrawer._back = closeDrawer._back || document.activeElement; const d = $('#drawer'); d.classList.add('on'); d.setAttribute('aria-hidden', 'false'); $('#scrim').classList.add('on'); $('#dr-close').focus();
}
async function queueOne(id, kind){ const r = await post('/api/files/queue', {ids:[id], kinds:[kind]}); toast(r.n?'Added to the queue':'Not applicable'); closeDrawer(); lastHtml=''; render(); refresh(); }
async function redoOne(qid){ if (!await ask('Re-encode with the current rules? The original is restored and the film gets priority.', 'Re-encode')) return; const r = await post('/api/quarantine/restore', {ids:[qid], redo:true}); toast(r.queued?'Back in the queue':'Failed'); closeDrawer(); lastHtml=''; render(); refresh(); }
async function restoreOne(qid){ if (!await ask('Restore the original? The new version will be deleted.', 'Restore', true)) return; await post('/api/quarantine/restore', {ids:[qid]}); toast('Restored'); closeDrawer(); lastHtml=''; render(); refresh(); }

/* ============================== welcome wizard (first run) ============================== */
let wz = 0;
const WZ = ['Welcome','Library','Subtitles','Quality','Plex','Worker'];
R.welcome = async (v,s,seq) => {
  if (s) return;                                    // do not refresh while the user is typing
  const [st, setup] = await Promise.all([api('/api/settings'), api('/api/worker-setup')]); if (stale(seq)) return;
  S0 = {...st}; S = {...st}; WS = setup; drawWizard(v);
};
function wzSave(keys){ const b = {}; keys.forEach(k=>{ if (S[k]!==S0[k]) b[k]=S[k]; }); return Object.keys(b).length ? post('/api/settings', b).then(()=>{ Object.assign(S0, b); }) : Promise.resolve(); }
async function wzGo(d){
  await wzSave(['skip_dirs','anim_dirs','langs','min_mean','min_p5','min_vbr_1080']);
  if (wz===4 && d>0) {
    const u = $('#plex-url')?.value.trim(), t = $('#plex-tok')?.value.trim();
    if ((u && !t && !S.plex_token_set) || (!u && t)) { toast('Enter both the Plex address and the token, or leave both empty to skip.'); return; }
    if (u && (t || S.plex_token_set)) { try { await plexSave(); } catch { return; } }
  }
  wz = Math.max(0, Math.min(WZ.length-1, wz+d)); drawWizard($('#view'));
}
async function wzJump(i){ await wzSave(['skip_dirs','anim_dirs','langs','min_mean','min_p5','min_vbr_1080']); wz = Math.max(0, Math.min(WZ.length-1, i)); drawWizard($('#view')); }
async function wzDone(){ await wzSave(['skip_dirs','anim_dirs','langs','min_mean','min_p5','min_vbr_1080']); await post('/api/settings', {setup_done:1}); await refresh(); toast('Done! MediaScan is set up.'); location.hash = '#overview'; }
function drawWizard(v){
  const L = OV.library||{}; const langs = S.langs.split(',').filter(Boolean); const pr = presetOf(S.min_mean, S.min_p5);
  wzMax = Math.max(wzMax, wz);
  const steps = `<div class="wzsteps">${WZ.map((t,i)=>`<span class="${i===wz?'on':i<wz?'done':''}${i<=wzMax&&i!==wz?' lnk':''}" ${i<=wzMax&&i!==wz?`role="button" tabindex="0" ${on('click','wzJump',i)}`:''}>${i+1}. ${t}</span>`).join('')}</div>`;
  const body = [
    `<h2>Welcome to MediaScan</h2>
     <p>MediaScan makes your movies and shows smaller without a visible difference, and fills in missing subtitles. Originals are only replaced after a quality check, and are first kept in quarantine for a while.</p>
     <p class="muted">Six short steps and you're set up. You can change everything later in Settings.</p>
     <div class="srow"><div><div class="lbl">Language</div><div class="hint">Dashboard language.</div></div>${langSelect()}</div>
     <div class="srow"><div><div class="lbl">Theme</div><div class="hint">Auto follows your system setting.</div></div>${tabsHtml([['auto','Auto'],['light','Light'],['dark','Dark']], PREF.theme, 'wzTheme')}</div>`,
    `<h2>Your library</h2>
     <p>${L.files ? `MediaScan found ${nf(L.files)} video${L.files===1?'':'s'} (${size(L.size)}).` : 'MediaScan is now looking for videos in your media folder. With a large library the first scan takes a while; you can simply carry on.'}
       <span class="muted" id="wz-scan">${OV.scan?.running ? `Scanning: ${esc(OV.scan.phase)} · ${nf(OV.scan.done)} / ${nf(OV.scan.total)}` : ''}</span></p>
     <div class="srow"><div><div class="lbl">Skip folders</div><div class="hint">Folders MediaScan never touches, separated by commas (e.g. Music, Downloads).</div></div><input style="width:min(360px,100%)" value="${esc(S.skip_dirs)}" data-change=\'["sField","skip_dirs"]\'></div>
     <div class="srow"><div><div class="lbl">Animation folders</div><div class="hint">Top-level folders with animation: these may use a lower bitrate.</div></div><input style="width:min(360px,100%)" value="${esc(S.anim_dirs||'')}" data-change=\'["sField","anim_dirs"]\'></div>
     <div style="margin-top:12px"><button class="sm" data-click=\'["scanNow"]\'>Scan again</button></div>`,
    `<h2>Subtitles</h2>
     <p>Choose the languages every file should have. MediaScan looks for missing ones on OpenSubtitles. If that fails, the worker makes the subtitle itself (speech recognition and translation on your own PC).</p>
     <div class="srow"><div><div class="lbl">Languages</div><div class="hint">Click a language to remove it.</div></div>
       <div class="btns"><div class="togglechips">${langs.map(l=>`<button class="on" title="${esc(LN()[l]||l)} - click to remove" ${on('click','wzLang',l)}>${esc(l.toUpperCase())}</button>`).join('')}</div>
       <select data-change=\'["wzLangSel"]\'><option value="">+ Add language</option>${Object.entries(LN()).filter(([k])=>!langs.includes(k)).sort((a,b)=>a[1].localeCompare(b[1])).map(([k,n])=>`<option value="${esc(k)}">${esc(n)} (${esc(k.toUpperCase())})</option>`).join('')}</select></div></div>`,
    `<h2>Quality</h2>
     <p>Every movie is measured with VMAF, a score for how the picture looks to the human eye. "High" is indistinguishable from the original for almost everyone.</p>
     <div class="srow"><div><div class="lbl">Quality level</div><div class="hint">Higher = better picture, less space saved.</div></div>${tabsHtml([...PRESETS.map(([k,l,a,b])=>[k,`${l} <span class="n">${a.toLocaleString(LOC)}/${b}</span>`]), ...(pr==='custom'?[['custom',`Custom <span class="n">${(+S.min_mean).toLocaleString(LOC)}/${+S.min_p5}</span>`]]:[])], pr, 'wzPreset')}</div>
     <div class="srow"><div><div class="lbl">Minimum bitrate (1080p)</div><div class="hint">A safety net for grain and dark scenes. 5 Mbps is a good default; 4K and other resolutions scale accordingly.</div></div><div><input type="number" step="0.5" value="${esc(String(S.min_vbr_1080).replace(/\.0$/,''))}" data-change=\'["sField","min_vbr_1080"]\'><span class="unit">Mbps</span></div></div>`,
    `<h2>Plex (optional)</h2>
     <p>With Plex connected, MediaScan waits to replace a movie while someone is watching it, and refreshes Plex after every change. No Plex? Just skip this step.</p>
     <div class="srow"><div><div class="lbl">Plex address and token</div><div class="hint">${S.plex_token_set?'<span class="good">Connected</span> · ':''}<a class="acc" href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noopener">Where do I find the token?</a></div></div>
       <div class="btns"><input id="plex-url" placeholder="http://192.168.1.10:32400" value="${esc(S.plex_url||'')}" style="width:220px"><input id="plex-tok" type="password" placeholder="${S.plex_token_set?'token (unchanged)':'Plex token'}" style="width:160px" autocomplete="off"></div></div>`,
    `<h2>Add a worker</h2>
     <p>The heavy lifting happens on a PC or Mac with a good graphics card: the worker. Install it now, or later via Workers. As long as there is no worker, nothing happens to your files.</p>
     ${workerAddHtml().replace('<div class="card"><h3>Add worker</h3>','<div>')}
     ${(OV.workers||[]).some(w=>w.online)?`<p class="good">✓ A worker is connected.</p>`:''}`,
  ][wz];
  const nav = `<div class="wznav">${wz?'<button data-click=\'["wzGo",-1]\'>Previous</button>':'<span></span>'}
    ${wz<WZ.length-1?`<button class="primary" data-click=\'["wzGo",1]\'>${wz===4?'Next (or skip)':'Next'}</button>`:'<button class="primary" data-click=\'["wzDone"]\'>Done, go to the overview</button>'}</div>`;
  const html = `<div class="wizard card">${steps}<div class="wzbody">${body}</div>${nav}</div>`;
  v.innerHTML = html; lastHtml = html;
}
function wzTheme(k){ applyTheme(k); renderNav(); drawWizard($('#view')); }
function wzPreset(k){ const p = PRESETS.find(x=>x[0]===k); if (p) { S.min_mean = String(p[2]); S.min_p5 = String(p[3]); } drawWizard($('#view')); }
function wzLang(l){ const a = S.langs.split(',').filter(Boolean); const i = a.indexOf(l); if (i>-1) { if (a.length===1) return toast('At least one language'); a.splice(i,1); } else a.push(l); S.langs = a.join(','); drawWizard($('#view')); }

/* ============================== settings ============================== */
const PRESETS = [['std','Standard',95,90],['high','High',96.5,93],['max','Maximum',97.5,95]];
let S0 = null, S = null, ACC = null, WS = null, customQ = false, wzMax = 0, settingsDirty = false;
const LIM = {min_mean:[80,100], min_p5:[70,100], min_vbr_1080:[0,100], min_vbr_2160:[0,200], min_saving_pct:[0,90], min_keep_pct:[0,100], quarantine_days:[0,365], quarantine_max_gb:[0,100000], min_free_gb:[0,100000], cmp_max_mb:[10,100000]};   // sensible ranges for the number fields
R.settings = async (v,s,seq) => {
  const [st, acc] = await Promise.all([api('/api/settings'), api('/api/account')]);
  if (stale(seq)) return;
  S0 = {...st}; S = {...st}; ACC = acc; customQ = presetOf(st.min_mean, st.min_p5)==='custom'; lastHtml = ''; drawSettings(v);
};
function presetOf(m,p){ const x = PRESETS.find(([,, a, b]) => +m===a && +p===b); return x ? x[0] : 'custom'; }
function setS(k, val){ S[k] = String(val); drawSettings($('#view')); }
function drawSettings(v){
  const pr = customQ ? 'custom' : presetOf(S.min_mean, S.min_p5); const langs = S.langs.split(',').filter(Boolean);
  const dirty = Object.keys(S).some(k => S[k] !== S0[k]); settingsDirty = dirty;
  const invalid = Object.keys(LIM).some(k => S[k] != null && (isNaN(+S[k]) || +S[k] < LIM[k][0] || +S[k] > LIM[k][1]));
  const num = (k, unit, step=1) => { const [lo, hi] = LIM[k]||[]; const bad = LIM[k] && (isNaN(+S[k]) || +S[k] < lo || +S[k] > hi); return `<input type="number" step="${step}" ${LIM[k]?`min="${lo}" max="${hi}"`:''} class="${bad?'bad':''}" value="${esc(String(S[k]).replace(/\.0$/,''))}" ${on('change','setSVal',k)}><span class="unit">${unit}</span>${bad?`<div class="sub bad">between ${lo} and ${hi}</div>`:''}`; };
  const sw = (k, inv) => { const isOn = inv ? S[k]==='0' : S[k]==='1'; return `<div class="sw ${isOn?'on':''}" role="switch" aria-checked="${isOn}" aria-label="${esc(k)}" tabindex="0" ${on('click','setSToggle',k)}><span class="t"></span></div>`; };
  const scanOpts = [['0','Off'],['60','Every hour'],['360','Every 6 hours'],['720','Every 12 hours'],['1440','Daily']];
  const sv = String(+S.scan_interval_min);
  const html = pageHead() + `<div class="set">
    <div class="card"><h3>Processing</h3>
      <div class="srow"><div><div class="lbl">Encoding</div><div class="hint">Hand out new encode jobs to the workers.</div></div>${sw('paused', true)}</div>
      <div class="srow"><div><div class="lbl">Subtitles</div><div class="hint">Hand out new subtitle jobs to the workers.</div></div>${sw('subs_paused', true)}</div>
      <div class="srow"><div><div class="lbl">Workers</div><div class="hint">${OV.workers.filter(w=>w.online).length===1?'1 worker online':nf(OV.workers.filter(w=>w.online).length)+' workers online'} · adding a worker, the worker key and the downloads are on the Workers page.</div></div>${goBtn('workers','Go to Workers')}</div>
    </div>
    <div class="card"><h3>General</h3>
      <div class="srow"><div><div class="lbl">Language</div><div class="hint">Dashboard language.</div></div>${langSelect()}</div>
      <div class="srow"><div><div class="lbl">Theme</div><div class="hint">Auto follows your system setting.</div></div>${tabsHtml([['auto','Auto'],['light','Light'],['dark','Dark']], PREF.theme, 'pickTheme')}</div>
    </div>
    <div class="card"><h3>Quality</h3>
      <div class="srow"><div><div class="lbl">Quality level</div><div class="hint">How strict the VMAF measurement is. Higher = better picture, less space saved.</div></div>
        ${tabsHtml([...PRESETS.map(([k,l,a,b])=>[k,`${l} <span class="n">${a.toLocaleString(LOC)}/${b}</span>`]),['custom','Custom']], pr, 'pickPreset')}</div>
      ${pr==='custom'?`<div class="srow"><div><div class="lbl">Custom target</div><div class="hint">Average VMAF and the weakest 5% of frames.</div></div><div>${num('min_mean','avg.',0.1)} &nbsp; ${num('min_p5','weakest 5%',0.5)}</div></div>`:''}
      <div class="srow"><div><div class="lbl">Minimum bitrate (1080p)</div><div class="hint">The new file never goes lower, even if VMAF is fine with it. Other resolutions scale: 4K ×2.5 · 720p ×0.55 · SD ×0.3 · HDR +15% · animation ×0.6.</div></div><div>${num('min_vbr_1080','Mbps',0.5)}</div></div>
      <div class="srow"><div><div class="lbl">Minimum bitrate (4K)</div><div class="hint">Own floor for 4K, instead of 2.5 × the 1080p value. 0 = automatic. HDR +15% and animation ×0.6 apply here too.</div></div><div>${num('min_vbr_2160','Mbps',0.5)}</div></div>
      <div class="srow"><div><div class="lbl">Minimum saving</div><div class="hint">If a file saves less, the original is kept.</div></div><div>${num('min_saving_pct','%')}</div></div>
      <div class="srow"><div><div class="lbl">CPU fallback for grainy films</div><div class="hint">When the GPU cannot keep the quality without exceeding the source bitrate (film grain), the film is encoded once more on a worker's CPU with x265, which keeps grain far more efficiently. Takes hours per film; switch on CPU encode for a worker on the Workers page.</div></div>${sw('cpu_fallback')}</div>
      ${S.cpu_fallback==='1'?`<div class="srow"><div><div class="lbl">CPU preset</div><div class="hint">Medium: about 2 hours for a 2-hour 1080p film on 8 cores. Slow: 2-3 times longer for roughly 5-10% smaller files.</div></div><select ${on('change','setSVal','cpu_preset')}><option value="medium" ${S.cpu_preset!=='slow'?'selected':''}>Medium (faster)</option><option value="slow" ${S.cpu_preset==='slow'?'selected':''}>Slow (smaller)</option></select></div>`:''}
      <details class="adv"><summary>Advanced</summary>
        <div class="srow"><div><div class="lbl">Extra floor (share of the source)</div><div class="hint">Optional: also keep at least this share of the original video bitrate. Only useful for sources with a very high bitrate. 0 = off.</div></div><div>${num('min_keep_pct','%')}</div></div>
      </details>
    </div>
    <div class="card"><h3>Subtitles</h3>
      <div class="srow"><div><div class="lbl">Languages</div><div class="hint">Which subtitles every file should have. Missing ones are searched for, or else generated.</div></div>
        <div class="btns"><div class="togglechips">${langs.map(l=>`<button class="on" title="${esc(LN()[l]||l)} - click to remove" ${on('click','toggleLang',l)}>${esc(l.toUpperCase())}</button>`).join('')}</div>
          <select data-change=\'["langAddSel"]\'><option value="">+ Add language</option>${Object.entries(LN()).filter(([k])=>!langs.includes(k)).sort((a,b)=>a[1].localeCompare(b[1])).map(([k,n])=>`<option value="${esc(k)}">${esc(n)} (${esc(k.toUpperCase())})</option>`).join('')}</select></div></div>
    </div>
    <div class="card"><h3>Library</h3>
      <div class="srow"><div><div class="lbl">Automatic scan</div><div class="hint">New files are queued automatically.</div></div>
        <select data-change=\'["setSVal","scan_interval_min"]\'>${scanOpts.map(([k,l])=>`<option value="${k}" ${k===sv?'selected':''}>${l}</option>`).join('')}${scanOpts.some(([k])=>k===sv)?'':`<option selected value="${sv}">${sv} min</option>`}</select></div>
      <div class="srow"><div><div class="lbl">Skip folders</div><div class="hint">Comma-separated folder names.</div></div><input style="width:min(360px,100%)" value="${esc(S.skip_dirs)}" data-change=\'["setSVal","skip_dirs"]\'></div>
      <div class="srow"><div><div class="lbl">Animation folders</div><div class="hint">Top-level folders with animation: these may use a lower bitrate.</div></div><input style="width:min(360px,100%)" value="${esc(S.anim_dirs||'')}" data-change=\'["setSVal","anim_dirs"]\'></div>
    </div>
    <div class="card"><h3>Comparisons</h3>
      <div class="srow"><div><div class="lbl">Automatic comparisons</div><div class="hint">Grab four frames of the original and the new version for every replaced film, so you can always check the picture on the Compare page. About 1–2 MB per film.</div></div>${sw('cmp_auto')}</div>
      ${S.cmp_auto==='1'?`<div class="srow"><div><div class="lbl">Comparison storage limit</div><div class="hint">Above this, the oldest comparisons are deleted automatically.</div></div><div>${num('cmp_max_mb','MB',50)}</div></div>`:''}
    </div>
    <div class="card"><h3>Storage & safety</h3>
      <div class="srow"><div><div class="lbl">Keep originals for</div><div class="hint">How long replaced originals can be restored.</div></div><div>${num('quarantine_days','days')}</div></div>
      <div class="srow"><div><div class="lbl">Quarantine size limit</div><div class="hint">Above this, the oldest originals are deleted permanently.</div></div><div>${num('quarantine_max_gb','GB',50)}</div></div>
      <div class="srow"><div><div class="lbl">Safety brake</div><div class="hint">Encoding pauses when the NAS has less free space than this.</div></div><div>${num('min_free_gb','GB',50)}</div></div>
    </div>
    <div class="card"><h3>Plex</h3>
      <div class="srow"><div><div class="lbl">Connection ${S.plex_token_set?'<span class="chip done">Connected</span>':'<span class="chip">Not connected</span>'}</div><div class="hint">Replacing waits while a film is being watched, and Plex refreshes after every change.
          <a class="acc" href="https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/" target="_blank" rel="noopener">Where do I find the token?</a></div></div>
        <div class="btns"><input id="plex-url" placeholder="http://192.168.1.10:32400" value="${esc(S.plex_url||'')}" style="width:220px" data-input=\'["plexDirty"]\'>
          <input id="plex-tok" type="password" placeholder="${S.plex_token_set?'token (unchanged)':'Plex token'}" style="width:160px" autocomplete="off" data-input=\'["plexDirty"]\'>
          <button class="sm" id="plex-btn" data-click=\'["plexSave"]\'>${S.plex_token_set?'Update':'Connect'}</button>${S.plex_token_set?'<button class="sm ghost" data-click=\'["plexClear"]\'>Disconnect</button>':''}</div></div>
    </div>
    <div class="card"><h3>Account & security</h3>
      <div class="srow"><div><div class="lbl">Two-factor authentication</div><div class="hint">${ACC.mfa?`Active · ${ACC.recovery_left} recovery code(s) left`:'Not active'}</div></div>
        <div class="btns"><button class="sm" data-click=\'["secModal","recovery"]\'>New recovery codes</button><button class="sm danger" data-click=\'["secModal","mfa"]\'>Reset MFA</button></div></div>
      <div class="srow"><div><div class="lbl">Password</div><div class="hint">Other sessions are signed out after a change.</div></div><button class="sm" data-click=\'["secModal","password"]\'>Change password</button></div>
      <div class="srow"><div><div class="lbl">Sessions</div><div class="hint">${ACC.sessions} active session(s), including this one.</div></div>
        <div class="btns"><button class="sm" data-click=\'["logoutOthers"]\'>Sign out everywhere else</button><button class="sm" data-click=\'["logout"]\'>Sign out</button></div></div>
    </div>
    ${dirty?`<div class="savebar"><span>${invalid?'Some values are out of range.':'You have unsaved changes.'}</span><button class="ghost" data-click=\'["resetS"]\'>Cancel</button><button class="primary" ${invalid?'disabled':''} data-click=\'["saveSettings"]\'>Save</button></div>`:''}
  </div>`;
  v.innerHTML = html; lastHtml = html;
}
function pickTheme(k){ applyTheme(k); renderNav(); drawSettings($('#view')); }
function pickPreset(k){ const p = PRESETS.find(x=>x[0]===k); customQ = !p; if (p) { S.min_mean = String(p[2]); S.min_p5 = String(p[3]); } drawSettings($('#view')); if(!p) setTimeout(()=>document.querySelector('.srow input[type=number]')?.focus(),0); }
const LN = () => { const b = (PREF.lang==='nl' ? S.lang_list : S.lang_list_en) || S.lang_list_en || S.lang_list || {}; if (PREF.lang==='nl'||PREF.lang==='en') return b; const o = {}; for (const k in b) o[k] = langName(k, b[k]); return o; };
function toggleLang(l){ const a = S.langs.split(',').filter(Boolean); const i = a.indexOf(l); if (i>-1) { if (a.length===1) return toast('At least one language'); a.splice(i,1); } else a.push(l); setS('langs', a.join(',')); }
async function saveSettings(){
  const keys=['min_mean','min_p5','min_saving_pct','min_keep_pct','min_vbr_1080','min_vbr_2160','langs','quarantine_days','quarantine_max_gb','min_free_gb','scan_interval_min','skip_dirs','anim_dirs','paused','subs_paused','cpu_fallback','cpu_preset','cmp_auto','cmp_max_mb'];
  const body={}; keys.forEach(k=>{ if (S[k]!==S0[k]) body[k]=S[k]; });
  await post('/api/settings', body); toast('Settings saved'); await refresh(); lastHtml=''; render();
}
function plexDirty(){ const b = $('#plex-btn'); if (b && b.classList.contains('linked')) { b.classList.remove('linked'); b.classList.add('primary'); b.textContent = 'Connect'; } }
async function plexSave(){ const r = await post('/api/plex', {url:$('#plex-url').value, token:$('#plex-tok').value}); toast(r.version?`Plex connected (version ${r.version})`:'Plex connected'); lastHtml=''; render(); }
async function plexClear(){ if (!await ask('Disconnect Plex?', 'Disconnect', true)) return; await post('/api/plex', {clear:true}); toast('Plex disconnected'); lastHtml=''; render(); }
async function logoutOthers(){ const r = await post('/api/account/logout-others'); toast(`${r.n} other session(s) signed out`); lastHtml=''; render(); }
function secModal(kind){
  const t = {mfa:['Reset MFA','Your current authenticator link and recovery codes will stop working. You will set up MFA again right away.','Reset'],
    recovery:['New recovery codes','Your old recovery codes will stop working.','Create new codes'],
    password:['Change password','Other sessions will be signed out.','Change'],
    token:['New worker key','All workers stop until you enter the new key (run the installer again or edit worker.ini).','Create new key']}[kind];
  const url = {mfa:'/api/account/mfa-reset', recovery:'/api/account/recovery', password:'/api/account/password', token:'/api/account/worker-token'}[kind];
  $('#modal-card').innerHTML = `<h2>${t[0]}</h2><p class="muted" style="margin:0"><span>${t[1]}</span> <span>Confirm with your password and a code from your app.</span></p>
    <div class="field"><label>Password</label><input id="m-pw" type="password" autocomplete="current-password"></div>
    <div class="field"><label>Code from your authenticator app</label><input id="m-code" inputmode="numeric" style="font-family:var(--mono)"></div>
    ${kind==='password'?`<div class="field"><label>New password (at least 10 characters)</label><input id="m-new" type="password" autocomplete="new-password"></div>
      <div class="field"><label>Repeat new password</label><input id="m-new2" type="password" autocomplete="new-password"></div>`:''}
    <div id="m-out"></div><div id="m-err" class="bad" style="margin-top:8px"></div>
    <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px"><button class="ghost" data-click=\'["closeModal"]\'>Cancel</button><button class="primary" id="m-go">${t[2]}</button></div>`;
  $('#modal').classList.add('on'); setTimeout(()=>$('#m-pw').focus(),30);
  $('#m-go').onclick = async () => {
    if (kind==='password' && $('#m-new').value !== $('#m-new2').value) { $('#m-err').textContent = trText('The passwords do not match'); return; }
    const r = await fetch(url, {method:'POST', headers:H, body:JSON.stringify({password:$('#m-pw').value, code:$('#m-code').value, new: kind==='password'?$('#m-new').value:undefined})});
    const d = await r.json().catch(()=>({}));
    if (!r.ok) { $('#m-err').textContent = trText(d.detail || 'Failed'); return; }
    if (kind==='mfa') { closeModal(); showMfaSetup(); return; }
    if (kind==='password' || kind==='token') { closeModal(); toast(kind==='password'?'Password changed':'New worker key created'); lastHtml=''; render(); return; }
    $('#m-out').innerHTML = `<div class="codes">${d.recovery.map(c=>`<span>${c}</span>`).join('')}</div><div class="sub">Save them now, they are shown only once.</div>`;
    $('#m-go').textContent='Close'; $('#m-go').onclick = () => { closeModal(); lastHtml=''; render(); };
  };
}
function closeModal(){ $('#modal').classList.remove('on'); if (closeModal._no) { const f = closeModal._no; closeModal._no = null; f(); } }
// themed confirmation in place of window.confirm: resolves true/false; Escape, the scrim and Cancel all resolve false
function ask(msg, okLabel='OK', danger=false){
  return new Promise(res => {
    $('#modal-card').innerHTML = `<h2>${esc(trText('Are you sure?'))}</h2><p class="muted" style="margin:0 0 16px">${esc(trText(msg))}</p>
      <div class="btns"><button class="ghost" data-click=\'["closeModal"]\'>${esc(trText('Cancel'))}</button><button class="${danger?'danger':'primary'}" id="m-yes">${esc(trText(okLabel))}</button></div>`;
    closeModal._no = () => res(false);
    $('#m-yes').onclick = () => { closeModal._no = null; $('#modal').classList.remove('on'); res(true); };
    $('#modal').classList.add('on'); (danger ? $('#modal-card .ghost') : $('#m-yes')).focus();
  });
}

/* ============================== start ============================== */
document.addEventListener('DOMContentLoaded', () => {
  $('#f-login').addEventListener('submit', doLogin);
  $('#f-setup').addEventListener('submit', doSetup);
  $('#btn-enable').addEventListener('click', enableMfa);
  $('#btn-codes-ok').addEventListener('click', start);
  $('#scrim').addEventListener('click', closeDrawer);
  $('#dr-close').addEventListener('click', closeDrawer);
  $('#modal').addEventListener('click', e => { if (e.target.id==='modal') closeModal(); });
  document.addEventListener('keydown', e => { if (e.key==='Escape') { closeDrawer(); closeModal(); menu(false); } });
  const boot = () => Promise.all([api('/api/overview', {silent:true}), I18N_READY]).then(([d]) => start(d))
    .catch(e => { if (!['login','mfa'].includes(e.message)) { setOffline(true); setTimeout(boot, 5000); } });   // server still starting: keep trying
  boot();
});

/* compound actions for the data-* dispatcher (see `on` near the top) */
Object.assign(ACT, {
  reload: () => { lastHtml = ''; render(); },
  wipeFlip: (el) => { const w = el.closest('figure').querySelector('.wipe'); w.classList.toggle('flip'); w.classList.toggle('show-new', w.classList.contains('flip')); el.classList.toggle('on', w.classList.contains('flip'));
    if (w.classList.contains('flip')) w.querySelector('input').setAttribute('aria-label', 'Tap to switch between original and new'); },
  wipeToggle: (el) => { const w = el.closest('.wipe'); if (w.classList.contains('flip')) w.classList.toggle('show-new'); },
  wipeHalf: (el) => { const w = el.closest('figure').querySelector('.wipe'); w.style.setProperty('--p', '50%'); w.querySelector('input').value = 50; },
  wipeZoom: (el) => { const f = el.closest('figure'); f.classList.toggle('zoom'); el.textContent = trText(f.classList.contains('zoom') ? 'Smaller' : 'Enlarge'); f.scrollIntoView({block:'nearest'}); },
  resume: (el, e, k) => togglePause(k),
  rows: el => { pageSize = +el.value; PREF.rows = pageSize; savePref('ms_rows', pageSize); page = 0; resetSel(); render(); },
  dense: () => { PREF.dense = !PREF.dense; savePref('ms_dense', PREF.dense ? '1' : ''); document.documentElement.classList.toggle('dense', !!PREF.dense); lastHtml = ''; render(); },
  clearFilters: () => { filter = ''; flt.cat = flt.res = flt.codec = ''; page = 0; render(); },
  stop: () => {},                              // swallow a click inside a clickable row (the dispatcher takes the nearest data-click)
  rowCheck: el => el.querySelector('input')?.click(),
  toggleSel: (el, e, id) => toggleSel(e, id),
  togglePage: el => togglePage(el.checked),
  pagePrev: () => { page--; resetSel(); render(); },
  pageNext: () => { page++; resetSel(); render(); },
  sortBy: (el, e, key) => { const first = SORT_DESC_FIRST.has(key) ? 'desc' : 'asc';
    if (sort !== key) { sort = key; sortDir = first; } else if (sortDir === first) sortDir = first === 'desc' ? 'asc' : 'desc'; else { sort = ''; sortDir = ''; }
    page = 0; resetSel(); render(); },
  flt: (el, e, k) => { flt[k] = el.value; page = 0; resetSel(); render(); },
  planSel: el => { libPlan = el.value; page = 0; resetSel(); render(); },
  search: el => onSearch(el),
  wipe: el => el.parentElement.style.setProperty('--p', el.value + '%'),
  wzLangSel: el => { if (el.value) wzLang(el.value); },
  langAddSel: el => { if (el.value) toggleLang(el.value); },
  langSel: el => setLang(el.value),
  setSVal: (el, e, k) => setS(k, el.value),
  setSToggle: (el, e, k) => setS(k, S[k] === '1' ? '0' : '1'),
  sField: (el, e, k) => { S[k] = el.value; },
  resetS: () => { S = {...S0}; customQ = presetOf(S.min_mean, S.min_p5) === 'custom'; drawSettings($('#view')); },
  theme: (el, e, k) => { applyTheme(k); renderNav(); if (view === 'settings') { lastHtml = ''; drawSettings($('#view')); } },
  tab: (el, e, fn, k) => ({setTab, pickTheme, wzTheme, pickPreset, wzPreset})[fn](k),
  logout: () => post('/api/logout').finally(() => location.reload()),
  showTok: () => { $('#wtok').textContent = WS.token; },
  copyTok: () => navigator.clipboard.writeText(WS.token).then(() => toast('Copied')),
  go: (el, e, h) => { location.hash = h; },
  selAllResults: () => { selAll = true; syncSel(); },
  clearSelAll: () => { resetSel(); syncSel(); },
  showAdd: () => { showAdd = !showAdd; lastHtml = ''; render(); },
  selShowAll: (el, e, k) => { selShowAll[k] = !selShowAll[k]; lastHtml = ''; render(true); },
  actClose: (el, e, id, a) => { act(id, a); closeDrawer(); },
  cmpMakeStop: (el, e, id) => cmpMake(id),
  cmpDeleteStop: (el, e, id) => cmpDelete(id, true),
  workerFlag: (el, e, name, k, val) => setWorker(name, {[k]: val}),
  workerHeight: (el, e, name) => setWorker(name, {max_height: +el.value}),
  loadMore: el => actLoadMore(el),
  menu: (el, e, on) => menu(on),
});
