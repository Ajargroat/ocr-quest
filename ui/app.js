/* ═══════════ shared helpers ═══════════ */
const $ = id => document.getElementById(id);
const FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹';
const faDigits = v => String(v ?? '').replace(/\d/g, d => FA_DIGITS[d]);
/* Font Awesome <i> helper — one source of truth for icon markup. */
const fi = n => '<i class="fa fa-' + n + '" aria-hidden="true"></i>';
/* ISO-ish server timestamp → Persian (Jalali) date, e.g. ۲ شهریور ۱۴۰۴ */
const faDate = iso => {
  const d = new Date(iso);
  if (isNaN(d)) return '';
  return d.toLocaleDateString('fa-IR', { year: 'numeric', month: 'long', day: 'numeric' });
};

/* Database codes → pretty Persian display labels (values stay untouched). */
const SUBJECT_FA = {
  zist: 'زیست', shimi: 'شیمی', fizik: 'فیزیک', physic: 'فیزیک', physics: 'فیزیک',
  zamin: 'زمین‌شناسی', zeminscnasi: 'زمین‌شناسی', riazi: 'ریاضی', math: 'ریاضی'
};
const subjectFa = s => SUBJECT_FA[String(s || '').toLowerCase()] || s;
const chapterFa = t => {
  const m = /^(?:chapter|chap|fasl|part)\s*_?\s*(\d+)/i.exec(String(t || ''));
  return m ? 'فصل ' + faDigits(m[1]) : t;
};

/* Icons: Font Awesome via the CDN <link> in index.html — use fi('name'). */
let PIPE_RUNNING = false, PIPE_STOPPING = false;

/* One shared floating tooltip for [data-tip] elements (donut slices, cells…). */
const floatTip = document.createElement('div');
floatTip.className = 'float-tip';
document.body.appendChild(floatTip);
document.addEventListener('pointerover', e => {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (t){ floatTip.textContent = t.dataset.tip; floatTip.classList.add('show'); }
});
document.addEventListener('pointerout', e => {
  const t = e.target.closest && e.target.closest('[data-tip]');
  if (t && !(e.relatedTarget && t.contains(e.relatedTarget))) floatTip.classList.remove('show');
});
document.addEventListener('pointermove', e => {
  if (!floatTip.classList.contains('show')) return;
  const pad = 14, w = floatTip.offsetWidth, h = floatTip.offsetHeight;
  let x = e.clientX + pad, y = e.clientY + pad;
  if (x + w > innerWidth - 8) x = e.clientX - w - pad;
  if (y + h > innerHeight - 8) y = e.clientY - h - pad;
  floatTip.style.transform = 'translate(' + x + 'px,' + y + 'px)';
});

function toast(msg, ms){
  const el = document.createElement('div');
  el.textContent = msg;
  toastEl(el, ms || 2600);
}

/* One shared top-right stack: entries fade in, sit for `ms`, fade out —
   several pile up downward as they arrive. */
function toastEl(el, ms){
  const box = $('toasts');
  el.classList.add('toast');
  box.appendChild(el);
  setTimeout(() => {
    el.classList.add('out');
    setTimeout(() => el.remove(), 300);
  }, ms);
}

/* Unwraps n8n's double-encoded options until a real array is found. */
function parseOptions(raw){
  let val = raw;
  for (let i = 0; i < 3; i++){
    if (Array.isArray(val)) return val;
    if (typeof val === 'string'){
      try { val = JSON.parse(val); } catch(e){ return []; }
    } else {
      return [];
    }
  }
  return Array.isArray(val) ? val : [];
}

/* Typesets math once MathJax has finished loading from the CDN. */
function typesetMath(el){
  let tries = 0;
  const run = () => {
    if (window.MathJax && MathJax.typesetPromise){
      MathJax.typesetPromise(el ? [el] : undefined).catch(()=>{});
    } else if (tries++ < 25){
      setTimeout(run, 200);
    }
  };
  run();
}

/* ═══════════ PIPELINE (unchanged logic) ═══════════ */
function applyStats(s){
  const done = (s.processed || 0) + (s.errors || 0), total = s.total || 0;
  $('barFill').style.width = total ? (done / total * 100) + '%' : '0%';
  $('barLabel').textContent = total ? done + ' / ' + total : 'idle';
}
const STATUS_LABEL = {queued:'Queued', uploading:'Uploading', ocr:'OCR',
                      saving:'Saving', cached:'Cached', done:'Done', failed:'Failed'};
function fileCard(ev){
  const wrap = $('files');
  // Completed files leave the queue (rework item 15): a finished source has
  // moved to done/ server-side, so keeping its card only clutters the list.
  if (ev.status === 'done'){
    const done = document.getElementById('f-' + ev.id);
    if (done) done.remove();
    $('fileCount').textContent = wrap.children.length + ' file(s)';
    return;
  }
  const empty = wrap.querySelector('.empty');
  if (empty) empty.remove();
  let el = document.getElementById('f-' + ev.id);
  if (!el){
    el = document.createElement('div');
    el.id = 'f-' + ev.id;
    el.innerHTML = '<div class="file-top"><span class="fname"></span>' +
                   '<span class="kind"></span><span class="chip"></span></div>' +
                   '<div class="fdetail"></div>';
    wrap.prepend(el);
  }
  if (ev.name){
    el.querySelector('.fname').textContent = ev.name;
    el.querySelector('.fname').title = ev.name;
  }
  if (ev.kind) el.querySelector('.kind').textContent = ev.kind;
  const chip = el.querySelector('.chip');
  chip.textContent = STATUS_LABEL[ev.status] || ev.status;
  chip.className = 'chip s-' + ev.status;
  el.className = 'file s-' + ev.status;
  if (ev.detail) el.querySelector('.fdetail').textContent = ev.detail;
  $('fileCount').textContent = wrap.children.length + ' file(s)';
}
function logLine(ev){
  const box = $('log');
  const ph = box.querySelector('.empty');
  if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = 'line lv-' + (ev.level || 'info');
  line.innerHTML = '<span class="t"></span><span class="m"></span>';
  line.querySelector('.t').textContent = ev.ts || '';
  if (ev.err) line.insertBefore(faultChip(ev.err), line.querySelector('.m'));
  line.querySelector('.m').textContent = ev.message;
  box.appendChild(line);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  $('logCount').textContent = box.querySelectorAll('.line').length + ' events';
}

/* ═══════════ FAULT DIAGNOSTICS · classified errors from the runner ═══════════
   pipeline/faults.py names every failure (tunnel blip vs dead key vs quota…);
   the runner emits typed `error` events and tags log lines with `ev.err`. */
const FAULT_GROUP = {tunnel_down:'net', send_blocked:'net', recv_dropped:'net',
                     rate_limit:'bill', quota:'bill', bad_key:'auth', geo_block:'auth',
                     overload:'srv', model_empty:'model', model_garbage:'model',
                     db_down:'store', storage_fail:'store', local_io:'local', unknown:'misc'};
function faultChip(kind, text){
  const chip = document.createElement('span');
  chip.className = 'ek ' + (FAULT_GROUP[kind] || 'misc');
  chip.textContent = text || String(kind || 'fault').replace(/_/g, ' ');
  return chip;
}
const DIAG = {counts: {}};
function updateDiagCount(pop){
  const el = $('diagCount');
  const total = Object.values(DIAG.counts).reduce((a, b) => a + b, 0);
  if (!total){ el.hidden = true; return; }
  el.textContent = '⚠ ' + total + ' fault' + (total > 1 ? 's' : '');
  el.title = Object.entries(DIAG.counts).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => k.replace(/_/g, ' ') + ' ×' + n).join('  ·  ');
  el.hidden = false;
  if (pop){ el.classList.remove('pop'); void el.offsetWidth; el.classList.add('pop'); }
}
function diagLine(ev, replay){
  const box = $('log');
  const ph = box.querySelector('.empty');
  if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = 'line diag ' + (FAULT_GROUP[ev.kind] || 'misc');
  const t = document.createElement('span'); t.className = 't'; t.textContent = ev.ts || '';
  const chip = faultChip(ev.kind, (ev.emoji || '❔') + ' ' + (ev.label || ev.kind));
  chip.title = 'where: ' + (ev.where || '?');
  const m = document.createElement('span'); m.className = 'm';
  const head = document.createElement('b');
  head.textContent = (ev.file ? ev.file + ' — ' : '') + (ev.hint || '');
  m.appendChild(head);
  if (ev.where){
    const w = document.createElement('span'); w.className = 'where';
    w.textContent = ' @ ' + ev.where; m.appendChild(w);
  }
  if (ev.raw){
    const r = document.createElement('span'); r.className = 'raw';
    r.textContent = ev.raw; m.appendChild(r);
  }
  line.append(t, chip, m);
  box.appendChild(line);
  while (box.children.length > 400) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  $('logCount').textContent = box.querySelectorAll('.line').length + ' events';
  if (!replay){
    DIAG.counts[ev.kind] = (DIAG.counts[ev.kind] || 0) + 1;
    updateDiagCount(true);
  }
}

/* ── log wipe ── */
function wipeLog(){
  $('log').innerHTML = '';
  $('logCount').textContent = '';
}
$('logCopyBtn').onclick = async () => {
  const txt = ($('log').innerText || '').trim();
  if (!txt){ toast('Log is empty'); return; }
  try { await navigator.clipboard.writeText(txt); toast('Log copied'); }
  catch(e){ toast('Copy failed — ' + e); }
};
$('logClearBtn').onclick = () => {
  const box = $('log'), btn = $('logClearBtn');
  if (!box.querySelector('.line')){ toast('Log is already empty'); return; }
  const n = box.querySelectorAll('.line').length;
  btn.classList.add('sweeping');
  box.classList.add('wiping');
  setTimeout(() => {
    box.classList.remove('wiping');
    btn.classList.remove('sweeping');
    wipeLog();
    const ph = document.createElement('div');
    ph.className = 'empty';
    ph.textContent = 'log wiped — ' + n + ' event(s) swept away; new ones stream right in.';
    box.appendChild(ph);
    DIAG.counts = {}; updateDiagCount();
  }, 420);
};

/* ═══════════ STAGE RAIL · live pipeline phase readout ═══════════
   Derived entirely from the event stream the runner already emits:
   file statuses walk Scan → Upload → OCR → Save → Archive, stats
   carry totals, and run_started/finished bracket the whole ride. */
const RAIL = {
  names: ['Scan','Upload','Gemini OCR','Save','Archive'],
  statuses: ['queued','uploading','ocr','saving'],          // stage 0..3
  stageOf: {queued:0, uploading:1, ocr:2, saving:3},
  counts: null, last: null, cur: null, by_id: {},
  running: false, total: 0, processed: 0, errors: 0,
  wait: null,                            // {until,total,attempt,tries,where} · Q8
};
const railBox   = $('stages');
const railStages= railBox.querySelectorAll('.stage');
const railWires = railBox.querySelectorAll('.wire');
function railReset(){ RAIL.counts = {queued:0, uploading:0, ocr:0, saving:0}; RAIL.last = {}; RAIL.cur = null; RAIL.by_id = {}; }
railReset();
function railFlash(idx, cls, ms){
  const el = railStages[idx];
  el.classList.remove(cls); void el.offsetWidth; el.classList.add(cls);
  clearTimeout(el._ft); el._ft = setTimeout(() => el.classList.remove(cls), ms);
}
function stageFeed(ev){
  switch (ev.type){
    case 'run_started':
      RAIL.running = true; RAIL.total = 0; RAIL.processed = 0; RAIL.errors = 0;
      railReset();
      break;
    case 'run_finished':
      RAIL.running = false;
      break;
    case 'stats': {
      const s = ev.stats || {};
      if (s.total !== undefined) RAIL.total = s.total;
      if (s.processed !== undefined) RAIL.processed = s.processed;
      if (s.errors !== undefined) RAIL.errors = s.errors;
      break;
    }
    case 'file': {
      const prev = RAIL.last[ev.id];
      if (prev && prev !== ev.status && RAIL.counts[prev] !== undefined) RAIL.counts[prev]--;
      RAIL.last[ev.id] = ev.status;
      if (prev !== ev.status && RAIL.counts[ev.status] !== undefined) RAIL.counts[ev.status]++;
      if (ev.name || ev.kind) RAIL.by_id[ev.id] = {name: ev.name || '', kind: ev.kind || ''};
      const meta = RAIL.by_id[ev.id] || {};
      RAIL.cur = {
        name: ev.name || meta.name || '',
        kind: ev.kind || meta.kind || '',
        detail: ev.detail || (RAIL.cur && RAIL.cur.detail) || '',
        status: ev.status,
      };
      if (ev.status === 'failed') railFlash(RAIL.stageOf[prev] !== undefined ? RAIL.stageOf[prev] : 1, 'err', 1400);
      if (ev.status === 'done') railFlash(4, 'passed', 900);
      break;
    }
    case 'snapshot': {
      RAIL.running = !!ev.running;
      const s = ev.stats || {};
      RAIL.total = s.total || 0; RAIL.processed = s.processed || 0; RAIL.errors = s.errors || 0;
      railReset();
      const files = ev.files || {};
      const ids = Object.keys(files);
      ids.forEach(id => {
        const f = files[id];
        if (RAIL.counts[f.status] !== undefined) RAIL.counts[f.status]++;
        RAIL.last[id] = f.status;
        if (f.name) RAIL.by_id[id] = {name: f.name, kind: f.kind || ''};
      });
      const f = files[ids[ids.length - 1]];          // insertion order → most recent activity
      if (f) RAIL.cur = {name: f.name || '', kind: f.kind || '', detail: f.detail || '', status: f.status};
      break;
    }
  }
  renderRail();
  statsPaint();
}
function renderRail(){
  const doneN = RAIL.processed + RAIL.errors;
  const complete = !RAIL.running && RAIL.total > 0 && doneN >= RAIL.total;
  // most advanced file currently parked in a working stage (the runner is sequential → usually one)
  let active = -1;
  [1, 2, 3].forEach(i => { if (RAIL.counts[RAIL.statuses[i]] > 0) active = Math.max(active, i); });
  if (active < 0){
    if (RAIL.running && RAIL.total === 0) active = 0;                                    // preflight + scan
    else if (RAIL.running && RAIL.cur && RAIL.cur.status === 'done' && doneN < RAIL.total) active = 4; // archive gap between files
    else if (RAIL.running && RAIL.counts.queued > 0) active = 0;                          // everything still queued
  }
  if (complete) active = 4;

  railStages.forEach((el, i) => {
    el.classList.toggle('active', i === active && !complete);
    el.classList.toggle('warm', complete ? true : (i === 0 ? RAIL.total > 0 && active !== 0 : active > i));
    const bubble = el.querySelector('.bubble');
    const n = i < 4 ? RAIL.counts[RAIL.statuses[i]] : RAIL.processed;
    bubble.textContent = n;
    el.classList.toggle('counting', n > 0);
  });
  railWires.forEach((w, i) => {
    w.classList.toggle('live', !complete && active === i + 1);
    w.classList.toggle('warm', complete || active > i + 1);
  });
  railBox.classList.toggle('live', RAIL.running && !complete);

  // caption line
  const now = $('stageNow'), name = $('stageNowName'), detail = $('stageNowDetail');
  let st = 'idle', nm = 'idle', dt = '';
  if (complete){
    st = 'done'; nm = 'complete';
    dt = RAIL.processed + ' archived · ' + RAIL.errors + ' failed · run finished';
  } else if (RAIL.running){
    st = 'live';
    const cur = RAIL.cur;
    if (active >= 0){
      nm = RAIL.names[active];
      dt = (cur && cur.name ? cur.name + ' · ' : '') +
           ((cur && cur.detail) || (active === 0 && RAIL.total > 0
             ? RAIL.counts.queued + ' file(s) queued'
             : railStages[active].dataset.desc));
    } else {
      nm = 'Pipeline'; dt = 'waiting for the next file…';
    }
    if (RAIL.wait){
      const w = RAIL.wait;
      const left = Math.max(0, Math.ceil((w.until * 1000 - Date.now()) / 1000));
      if (left > 0){
        st = 'wait';
        nm = w.attempt && w.tries ? `Retry ${w.attempt}/${w.tries}` : 'Backing off';
        dt = (w.where ? w.where + ' · ' : '') + `${left}s left of ${Math.round(w.total)}s`;
      } else RAIL.wait = null;
    }
    if (cur && cur.status === 'failed'){ st = 'err'; nm = 'Failed'; dt = cur.name + (cur.detail ? ' · ' + cur.detail : ''); }
  } else if (RAIL.cur && RAIL.cur.status === 'failed' && doneN >= RAIL.total && RAIL.total > 0){
    st = 'err'; nm = 'Failed'; dt = RAIL.cur.name + (RAIL.cur.detail ? ' · ' + RAIL.cur.detail : '');
  }
  now.dataset.state = st;
  if (name.textContent !== nm) name.textContent = nm;
  if (detail.textContent !== dt){
    detail.textContent = dt;
    detail.style.animation = 'none'; void detail.offsetWidth; detail.style.animation = '';
  }
}
renderRail();
function setRunning(r){
  PIPE_RUNNING = !!r;
  const b = $('runBtn');
  b.classList.toggle('running', PIPE_RUNNING);
  b.classList.toggle('stopping', PIPE_RUNNING && PIPE_STOPPING);
  b.innerHTML = PIPE_RUNNING
    ? fi('stop') + '<span>Stop</span>'
    : fi('power-off') + '<span>Run</span>';
  b.title = PIPE_RUNNING
    ? (PIPE_STOPPING ? 'Stopping after the current file…'
                     : 'Stop — finishes the current file, imports parked writes')
    : 'Start scan — click again mid-run to stop it';
}
function handle(ev){
  stageFeed(ev);
  switch (ev.type){
    case 'snapshot':
      applyStats(ev.stats || {});
      DIAG.counts = Object.assign({}, ev.error_kinds || {});
      updateDiagCount();
      wipeLog();                       // replay below — never duplicate lines
      (ev.log || []).forEach(e => {
        if (e.type === 'log') logLine(e);
        else if (e.type === 'file') fileCard(e);
        else if (e.type === 'error') diagLine(e, true);
      });
      if (ev.lanes) LANES.states = Object.assign({}, ev.lanes);
      if (ev.running) setRunning(true);
      break;
    case 'log': logLine(ev); break;
    case 'file': fileCard(ev); break;
    case 'lane': laneFeed(ev); break;
    case 'round': LANES.round = ev.no || 0; laneNowPaint(); break;
    case 'error': diagLine(ev); break;
    case 'stats': applyStats(ev.stats || {}); break;
    case 'run_started': PIPE_STOPPING = false; setRunning(true); DIAG.counts = {}; updateDiagCount(); LANES.states = {}; LANES.round = 0;
      if (LANE_TICK){ clearInterval(LANE_TICK); LANE_TICK = null; } break;
    case 'run_finished': PIPE_STOPPING = false; setRunning(false);
      LANES.states = {}; LANES.round = 0; lanesRender(); laneNowPaint(); break;
    case 'wait': waitCountdown(ev); break;
    case 'upload': upApplyEvent(ev); break;
    case 'studio': studioFeed(ev); break;
    case 'usage': credUsageLive(ev); break;
  }
}

/* ═══════════ RETRY COUNTDOWN (Q8) ═══════════
   The runner sends the exact seconds it is about to sleep, so the number
   shown here always equals the wait actually being taken. */
let WAIT_TICK = null;
function waitCountdown(ev){
  RAIL.wait = { until: ev.until || 0, total: Math.max(0, ev.total || 0),
                attempt: ev.attempt || 0, tries: ev.tries || 0,
                where: ev.where || '' };
  clearInterval(WAIT_TICK);
  renderRail();
  WAIT_TICK = setInterval(() => {
    if (!RAIL.wait){ clearInterval(WAIT_TICK); WAIT_TICK = null; return; }
    if (RAIL.wait.until * 1000 <= Date.now()){
      RAIL.wait = null; clearInterval(WAIT_TICK); WAIT_TICK = null;
    }
    renderRail();
  }, 500);
}
function connect(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onclose = () => { setTimeout(connect, 1500); };
  ws.onmessage = m => handle(JSON.parse(m.data));
}
connect();

/* Run mode — page-level, remembered per-browser (localStorage; non-secret
   UI state, item 1.2/1b) so a refresh keeps Parallel selected.
   'one' = the serial loop, 'parallel' = round-based key-pinned lanes. */
const PIPE_MODE_KEY = 'konkour.pipeMode';
const PipeRun = { mode: (() => {
  try{ return localStorage.getItem(PIPE_MODE_KEY) || 'one'; }catch(e){ return 'one'; }
})() };
const pipeModeEl = $('pipeMode');
if (pipeModeEl){
  pipeModeEl.value = PipeRun.mode;
  pipeModeEl.onchange = () => {
    PipeRun.mode = pipeModeEl.value;
    try{ localStorage.setItem(PIPE_MODE_KEY, PipeRun.mode); }catch(e){}
    pipeModePaint();
  };
}

/* ═══════════ PARALLEL LANES ═══════════
   One row per Gemini key — Parallel mode's counterpart of the 5-stage rail.
   Defined here, never CALLED at top level before CredState (2069) / credEsc
   (2077) are initialised: every call site is a click, a WS message or
   credRender(), all of which run after the script has parsed. */
const LANES = { states: {}, round: 0, list: null };

function laneRows(){
  // Same filter activeKeyMask() uses: saved, not removed.
  return (CredState.rows || []).map((r, i) => ({r, i}))
    .filter(({r}) => r.keep !== null && r.keep !== undefined && !r.removed);
}
/* the one-by-one rail's five steps, shrunk to one icon per lane — the lane's
   .hub icon IS the current stage (item 1.3), not a text chip. */
const LANE_STEPS = [
  ['scan', 'magnifying-glass'], ['upload', 'cloud-arrow-up'],
  ['ocr', 'eye'], ['save', 'database'], ['archive', 'box-archive'],
];
const LANE_HUB_ICON = LANE_STEPS.map(([_k, icon]) => icon);
/* map the server's phase/state onto a stage index; -1 = nothing running */
function laneStepIndex(state, s){
  if (s.phase === 'imported') return 4;
  if (state === 'done' || s.phase === 'importing') return 3;
  if (s.phase === 'uploading' || state === 'start') return 1;
  if (state === 'route' || state === 'wait' || s.phase === 'ocr') return 2;
  return -1;
}
/* the hub glyph: key when idle/off, else the current stage's icon. A live run
   that has not picked a file up yet shows the scan glyph (item 1.3). */
function laneHubIcon(state, s, on){
  if (!on || state === 'off') return 'key';
  const i = laneStepIndex(state, s);
  if (i >= 0) return LANE_HUB_ICON[i];
  return RAIL.running ? 'magnifying-glass' : 'key';
}
/* per-key on/off is remembered per-browser (item 1.2) — non-secret UI state
   only, keyed by the row's saved index; the key VALUE never leaves .env. */
const LANE_OFF_KEY = 'konkour.laneOff';
function laneOffSet(){
  try { return new Set(JSON.parse(localStorage.getItem(LANE_OFF_KEY) || '[]')); }
  catch(e){ return new Set(); }
}
function laneOffSave(set){
  try { localStorage.setItem(LANE_OFF_KEY, JSON.stringify([...set])); } catch(e){}
}
/* the retry ring's progress: 0 → 1 over the exact slept seconds the runner
   sent (`until`/`total`), ending when the retry time is up (item 1.4). */
let LANE_TICK = null;
function laneTickStart(){
  if (!LANE_TICK) LANE_TICK = setInterval(laneTick, 250);
}
function laneTick(){
  const box = LANES.list;
  let waiting = false;
  if (box) box.querySelectorAll('.lane[data-state="wait"] .hub .ring').forEach(ring => {
    waiting = true;
    const lane = ring.closest('.lane');
    const s = LANES.states[+lane.dataset.key] || {};
    const total = s.total || 0, until = s.until || 0;
    const p = (!total || !until) ? 0
            : Math.max(0, Math.min(1, 1 - (until - Date.now() / 1000) / total));
    ring.style.setProperty('--p', p.toFixed(3));
  });
  if (!waiting){ clearInterval(LANE_TICK); LANE_TICK = null; }
}
function lanesRender(){
  const box = LANES.list || (LANES.list = $('laneList'));
  if (!box) return;
  const off = laneOffSet();
  box.innerHTML = laneRows().map(({r, i}) => {
    if (r.on === undefined) r.on = !off.has(r.keep);   // seed from localStorage
    const s = LANES.states[r.keep] || {};
    const on = r.on !== false;
    const st = on ? (s.state || 'idle') : 'off';
    const where = s.file ? '#' + (s.index || '?') + ' ' + s.file : '';
    return `<div class="lane" data-key="${r.keep}" data-state="${st}">` +
      `<div class="hub">${fi(laneHubIcon(st, s, on))}` +
        `<i class="ring" aria-hidden="true"></i></div>` +
      `<label class="lane-name">${credEsc(r.name || ('Key ' + (r.keep + 1)))}` +
        `<em>${credEsc(r.masked)}</em></label>` +
      `<em class="lane-file">${credEsc(where)}</em>` +
      `<label class="sw lane-sw" title="${on ? 'ON - this key can take a parallel lane'
                                             : 'OFF - kept in .env, no lane in a parallel run'}">` +
        `<input type="checkbox" data-keyon="${i}"${on ? ' checked' : ''}` +
        ` aria-label="${credEsc(r.name || ('Key ' + (i + 1)))} — parallel lane">` +
        `<span class="tr" aria-hidden="true"></span>` +
      `</label>` +
      `</div>`;
  }).join('');
  box.querySelectorAll('input[data-keyon]').forEach(box => {
    box.onchange = () => {
      const r = CredState.rows[+box.dataset.keyon];
      if (!r) return;
      r.on = box.checked;                 // session state (never .env)
      const set = laneOffSet();
      box.checked ? set.delete(r.keep) : set.add(r.keep);
      laneOffSave(set);                   // remembered per-browser (item 1.2)
      lanesRender();
    };
  });
  laneNowPaint();
  if (Object.values(LANES.states).some(s => s.state === 'wait')) laneTickStart();
}
function laneNowPaint(){
  const now = $('laneNow');
  if (!now) return;
  const vals = Object.values(LANES.states);
  const busy = vals.filter(s => s.state === 'start' || s.state === 'route' ||
                                s.state === 'wait').length;
  const done = vals.filter(s => s.state === 'done').length;   // importing or imported
  const imported = vals.filter(s => s.phase === 'imported').length;
  const bad  = vals.filter(s => s.state === 'failed').length;
  now.dataset.state = bad ? 'err' : (busy ? 'live' : (done ? 'done' : 'idle'));
  $('laneNowName').textContent = busy ? ('Round ' + LANES.round) : 'lanes';
  $('laneNowDetail').textContent = busy
    ? busy + ' lane(s) working · ' + imported + ' imported'
    : imported + ' imported · ' + bad + ' failed · ' + laneRows().length + ' lane(s)';
}
function laneFeed(ev){
  if (ev.key === null || ev.key === undefined) return;
  const s = {state: ev.state};
  ['index', 'file', 'model', 'attempt', 'reason', 'phase',
   'until', 'total'].forEach(f => {
    if (ev[f] !== undefined) s[f] = ev[f];
  });
  LANES.states[ev.key] = Object.assign({}, LANES.states[ev.key] || {}, s);
  lanesRender();
}
function pipeModePaint(){
  const par = PipeRun.mode === 'parallel';
  $('stages').hidden = par;
  $('lanes').hidden = !par;
  if (par && !CredState.loaded) loadCredentials();   // the rows ARE the lane list
  lanesRender();
}

/* The session on/off selection sent with Run: pool INDICES of the rows that
   are saved, not removed and still ON (row i ↔ gemini_key_pool[i] — the same
   contract /api/credentials uses). null = every key is on (Q4=A). */
function activeKeyMask(){
  const rows = (CredState.rows || []).filter(
    r => r.keep !== null && !r.removed && r.on !== false);
  const idx = rows.map(r => r.keep);
  return idx.length ? idx : null;
}

$('runBtn').onclick = async () => {
  if (PIPE_RUNNING){
    if (PIPE_STOPPING) return;                  // already asked — one wish is enough
    PIPE_STOPPING = true; setRunning(true);
    try{
      const j = await (await fetch('/api/stop', {method:'POST'})).json();
      if (!j.stopping){ PIPE_STOPPING = false; setRunning(PIPE_RUNNING); toast('No pipeline run in progress'); }
      else toast('Stop requested — the current file finishes first, then parked writes are imported.', 4200);
    }catch(e){ PIPE_STOPPING = false; setRunning(true); }
    return;
  }
  PIPE_STOPPING = false;
  const r = await fetch('/api/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({mode: PipeRun.mode, enabled_keys: activeKeyMask()}),
  });
  const j = await r.json();
  if (!j.started) logLine({type:'log', level:'warn', ts:'',
    message:'A run is already in progress.'});
};

/* ═══════════ TAB SWITCHING ═══════════ */
function showView(target){
  document.querySelectorAll('#side .side-btn').forEach(t =>
    t.classList.toggle('active', t.dataset.view === target));
  $('viewPipeline').classList.toggle('active', target === 'pipeline');
  $('viewReview').classList.toggle('active', target === 'review');
  $('viewStudio').classList.toggle('active', target === 'studio');
  $('viewDatabase').classList.toggle('active', target === 'database');
  $('viewUsage').classList.toggle('active', target === 'usage');
  $('viewPrompts').classList.toggle('active', target === 'prompts');
  $('viewCredentials').classList.toggle('active', target === 'credentials');
  /* the usage tab is LIVE data: every visit re-reads the call history, so a
     studio run (or a pipeline run) that happened while another tab was open
     is there when you look — a load-once tab showed stale numbers. */
  if (target === 'usage'){
    const first = !UsageState.loaded;
    UsageState.loaded = true;
    UsageState.dirty = false;
    if (first) viewLoading('viewUsage', true);
    usageRefresh(first ? () => viewLoading('viewUsage', false) : null);
  }
  if (target === 'prompts' && !PromptState.loaded){
    viewLoading('viewPrompts', true);
    promptsLoad().finally(() => viewLoading('viewPrompts', false));
  }
  if (target === 'review' && !RState.loaded){
    RState.loaded = true;
    viewLoading('viewReview', true);
    loadReviewMeta().then(loadReviewRows).finally(() => viewLoading('viewReview', false));
  }
  if (target === 'studio' && !StudioState.loaded){
    StudioState.loaded = true;
    viewLoading('viewStudio', true);
    studioInit().then(() => {
      const p = panelFromHash();
      if (p) studioPanel(p, true);
    }).finally(() => viewLoading('viewStudio', false));
  }
  if (target === 'database' && !DbState.loaded){
    DbState.loaded = true;
    viewLoading('viewDatabase', true);
    dbInit().finally(() => viewLoading('viewDatabase', false));
  }
  if (target === 'credentials' && !CredState.loaded){
    CredState.loaded = true;
    viewLoading('viewCredentials', true);
    loadCredentials().finally(() => viewLoading('viewCredentials', false));
  }
  if (target === 'pipeline' && !PipeState.loaded){
    viewLoading('viewPipeline', true);
    loadPipeProfiles().finally(() => viewLoading('viewPipeline', false));
  }
}

document.querySelectorAll('#side .side-btn[data-view]').forEach(tab => {
  tab.onclick = () => showView(tab.dataset.view);
});

/* Deep links (#studio, #database, …) open that view on load and on hash change,
   so a view can be shared/bookmarked and a headless screenshot can target one. */
function viewFromHash(){
  const h = (location.hash || '').replace('#', '').split('/')[0];
  return ['pipeline','review','studio','database','usage','prompts','credentials'].includes(h) ? h : null;
}
function panelFromHash(){
  const parts = (location.hash || '').replace('#', '').split('/');
  return parts[0] === 'studio' && parts[1] ? parts[1] : null;
}
window.addEventListener('hashchange', () => {
  const v = viewFromHash(); if (v) showView(v);
  if (v === 'studio' && window.StudioState && StudioState.loaded){
    const p = panelFromHash(); if (p) studioPanel(p, true);
  }
});

/* one loading treatment for every data-fetching view (item 13) */
function viewLoading(viewId, on){
  const v = document.getElementById(viewId);
  if (!v) return;
  let sk = v.querySelector('.load-shell');
  if (!on){ if (sk) sk.remove(); return; }
  if (sk) return;
  sk = document.createElement('div');
  sk.className = 'load-shell';
  sk.innerHTML = '<div class="load-wave"><i></i><i></i><i></i></div>' +
                 '<p class="load-label">Loading…</p>';
  v.appendChild(sk);
}

/* ═══════════ QBANK ═══════════ */
const STATUS_FA = {pending:'در انتظار', approved:'تأیید شده', rejected:'رد شده'};
const RState = { subject:[], status:[], grade:[], mode:'both', loaded:false,
                 chapter:[], corp:[], difficulty:[], from:'', to:'',
                 hasAnswer:'', hasPicture:'' };

/* ── themed custom dropdown (native <select> menus render white) ── */
const DDS = {};
function closeAllDD(){
  document.querySelectorAll('.dd.open').forEach(d => d.classList.remove('open'));
}
document.addEventListener('click', closeAllDD);

/* Q3(b): only the up-form dropdowns trade their caption for the selection;
   the revision rv* hosts keep their static Persian captions. */
const UPFORM_DDS = ['upSubject', 'upGrade', 'upTopic', 'upType'];
function createDropdown(hostId, caption, multi = false){
  const host = $(hostId);
  host.innerHTML =
    '<button class="dd-btn" type="button" aria-expanded="false">' +
      '<span class="dd-cap">' + caption + '</span>' +
      '<i class="dd-pip"></i>' +
    '</button><div class="dd-menu" role="listbox"></div>';
  const btn   = host.querySelector('.dd-btn');
  const pip   = host.querySelector('.dd-pip');
  const cap   = host.querySelector('.dd-cap');
  const menu  = host.querySelector('.dd-menu');
  const has   = v => multi
    ? dd.value.some(x => String(x) === String(v))
    : String(dd.value) === String(v);
  const dd = {
    host, multi, value: multi ? [] : '', options: [], onChange: () => {},
    setOptions(pairs, keep){
      dd.options = pairs;
      if (keep !== undefined) dd.value = multi ? (keep || []).slice() : keep;
      if (multi) dd.value = dd.value.filter(v => pairs.some(p => String(p[0]) === String(v)));
      else if (!pairs.some(p => String(p[0]) === String(dd.value))) dd.value = '';
      menu.innerHTML = '';
      pairs.forEach(([v, label]) => {
        const o = document.createElement('button');
        o.type = 'button';
        o.className = 'dd-opt' + (has(v) ? ' sel' : '');
        o.dataset.v = v;
        if (multi){
          const ck = document.createElement('i'); ck.className = 'ck';
          const tx = document.createElement('span'); tx.textContent = label;
          o.append(ck, tx);
        } else o.textContent = label;
        o.onclick = e => { e.stopPropagation(); multi ? dd.toggleVal(v) : dd.pick(v); };
        menu.appendChild(o);
      });
      paint();
    },
    /* multi mode: flip one value and keep the menu open for stacking */
    toggleVal(v){
      const i = dd.value.findIndex(x => String(x) === String(v));
      if (i >= 0) dd.value.splice(i, 1); else dd.value.push(v);
      menu.querySelectorAll('.dd-opt').forEach(o =>
        o.classList.toggle('sel', has(o.dataset.v)));
      paint();
      dd.onChange(dd.value.slice());
    },
    pick(v){
      dd.value = v;
      menu.querySelectorAll('.dd-opt').forEach(o =>
        o.classList.toggle('sel', o.dataset.v === String(v)));
      paint();
      host.classList.remove('open');
      btn.setAttribute('aria-expanded', 'false');
      dd.onChange(dd.value);
    },
    reset(){
      dd.value = multi ? [] : '';
      menu.querySelectorAll('.dd-opt').forEach(o => o.classList.remove('sel'));
      paint();
    }
  };
  function paint(){
    let labels;
    if (multi){
      labels = dd.value.map(v => {
        const h = dd.options.find(p => String(p[0]) === String(v));
        return h ? h[1] : v;
      });
    } else {
      const hit = dd.options.find(p => String(p[0]) === String(dd.value));
      labels = hit && dd.value ? [hit[1]] : [];
    }
    const active = !!labels.length;
    pip.textContent = multi && labels.length > 1 ? faDigits(labels.length) : '';
    pip.classList.toggle('on', active);
    btn.title = active ? caption + ': ' + labels.join('، ') : caption + ': همه';
    host.classList.toggle('has-value', active);
    if (cap && UPFORM_DDS.indexOf(hostId) !== -1)
      cap.textContent = active ? labels.join('، ') : caption;
  }
  btn.onclick = e => {
    e.stopPropagation();
    const wasOpen = host.classList.contains('open');
    closeAllDD();
    if (!wasOpen){
      host.classList.add('open');
      btn.setAttribute('aria-expanded', 'true');
    }
  };
  DDS[hostId] = dd;
  return dd;
}

/* ── floating stats panel: pale donut charts with hover tooltips ── */
const DOT = {
  approved:'#34d399', pending:'#fbbf24', rejected:'#fb7185',
  answer:'#34d399', noanswer:'#64748b', picture:'#22d3ee', nopicture:'#64748b'
};
const SUBJ_COLORS = ['#34d399', '#22d3ee', '#a78bfa', '#fbbf24', '#fb7185', '#f472b6', '#4ade80', '#38bdf8'];
const alpha = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + (n >> 16 & 255) + ',' + (n >> 8 & 255) + ',' + (n & 255) + ',' + a + ')';
};
const pale = c => alpha(c, .5);          /* tag-like washed fill */
const pctOf = (n, t) => t ? Math.round(n / t * 100) : 0;

function statusSegs(c){
  c = c || {};
  return [
    { label:'تأیید',     n:c.approved || 0, dot:DOT.approved },
    { label:'در انتظار', n:c.pending  || 0, dot:DOT.pending  },
    { label:'رد',        n:c.rejected || 0, dot:DOT.rejected }
  ].map(s => ({ ...s, color: pale(s.dot) }));
}

function donutSVG(segs, opt){
  opt = opt || {};
  const size = opt.size || 86, sw = opt.stroke || 5.4;
  const total = segs.reduce((a, s) => a + (s.n || 0), 0);
  let acc = 0;
  const arcs = !total ? '' : segs.filter(s => s.n > 0).map(s => {
    const p = s.n / total * 100;
    const len = Math.max(p - .9, .9);
    const el = '<circle class="dseg" data-tip="' + s.label + ': ' + faDigits(s.n) +
      ' (' + faDigits(Math.round(p)) + '٪)" r="15.915" cx="21" cy="21" fill="none" ' +
      'stroke="' + s.color + '" stroke-width="' + sw + '" pathLength="100" ' +
      'stroke-dasharray="' + len + ' ' + (100 - len) + '" stroke-dashoffset="' + (25 - acc) + '"/>';
    acc += p;
    return el;
  }).join('');
  return '<svg class="donut" viewBox="0 0 42 42" width="' + size + '" height="' + size + '" aria-hidden="true">' +
    '<circle r="15.915" cx="21" cy="21" fill="none" stroke="rgba(255,255,255,.06)" stroke-width="' + sw + '"/>' +
    arcs +
    '<text x="21" y="' + (opt.sub ? 20.6 : 23) + '" class="dn-t">' +
      (opt.center != null ? opt.center : faDigits(total)) + '</text>' +
    (opt.sub ? '<text x="21" y="26.6" class="dn-s">' + opt.sub + '</text>' : '') +
    '</svg>';
}

function legendHTML(segs, total){
  return '<div class="sleg">' + segs.map(s =>
    '<div><i style="background:' + s.dot + '"></i>' + s.label +
    '<b>' + faDigits(s.n) + '</b><span>' + faDigits(pctOf(s.n, total)) + '٪</span></div>'
  ).join('') + '</div>';
}

const statCard = (title, body) =>
  '<div class="stat-card"><div class="stat-title">' + title + '</div>' + body + '</div>';

function renderCountsBoard(m){
  const body = $('statsBody');
  if (!body) return;
  const total = (m.counts || {}).total || 0;
  let html = '';

  /* 1 · overall review status */
  const segs = statusSegs(m.counts);
  html += statCard('وضعیت بررسی', '<div class="stat-flex">' +
    donutSVG(segs) + legendHTML(segs, total) + '</div>');

  /* 2 · each subject's share of the bank */
  const subs = m.by_subject || [];
  if (subs.length){
    const share = subs.map((s, i) => ({
      label: subjectFa(s.subject), n: s.total,
      dot: SUBJ_COLORS[i % SUBJ_COLORS.length],
      color: alpha(SUBJ_COLORS[i % SUBJ_COLORS.length], .5)
    }));
    html += statCard('سهم هر درس از کل', '<div class="stat-flex">' +
      donutSVG(share, { center: faDigits(subs.length), sub: 'درس' }) +
      legendHTML(share, total) + '</div>');

    /* 3 · per-subject status donuts (hover a row for the full split) */
    html += statCard('وضعیت هر درس', '<div class="subj-list">' + subs.map(s => {
      return '<div class="subj-row" data-tip="' + subjectFa(s.subject) +
        ' — تأیید: ' + faDigits(s.approved) + ' · در انتظار: ' + faDigits(s.pending) +
        ' · رد: ' + faDigits(s.rejected) + '">' +
        donutSVG(statusSegs(s), { size: 38, stroke: 6.6, center: '' }) +
        '<div class="subj-meta"><b>' + subjectFa(s.subject) + '</b><span>' +
          faDigits(s.total) + ' سؤال · ' + faDigits(pctOf(s.total, total)) + '٪ کل</span></div>' +
        '<div class="subj-dots">' +
          '<i style="background:' + DOT.approved + '"></i>' + faDigits(s.approved) +
          '<i style="background:' + DOT.pending + '"></i>' + faDigits(s.pending) +
          '<i style="background:' + DOT.rejected + '"></i>' + faDigits(s.rejected) +
        '</div></div>';
    }).join('') + '</div>');
  }

  /* 4 · answer / picture facets */
  const a = m.answers || {}, p = m.pictures || {};
  const aSegs = [
    { label:'دارای پاسخ', n:a.with    || 0, dot:DOT.answer,   color:pale(DOT.answer)   },
    { label:'بدون پاسخ',  n:a.without || 0, dot:DOT.noanswer, color:pale(DOT.noanswer) }
  ];
  html += statCard('پاسخ‌داری', '<div class="stat-flex">' +
    donutSVG(aSegs, { center: faDigits(pctOf(a.with || 0, total)) + '٪', sub:'پاسخ‌دار' }) +
    legendHTML(aSegs, total) + '</div>');
  const pSegs = [
    { label:'دارای تصویر', n:p.with    || 0, dot:DOT.picture,   color:pale(DOT.picture)   },
    { label:'بدون تصویر',  n:p.without || 0, dot:DOT.nopicture, color:pale(DOT.nopicture) }
  ];
  html += statCard('تصویرداری', '<div class="stat-flex">' +
    donutSVG(pSegs, { center: faDigits(pctOf(p.with || 0, total)) + '٪', sub:'تصویردار' }) +
    legendHTML(pSegs, total) + '</div>');

  body.innerHTML = html;
}

async function loadReviewMeta(){
  try{
    const m = await (await fetch('/api/review/meta')).json();
    DDS.rvSubject.setOptions((m.subjects||[]).map(s => [s, subjectFa(s)]), RState.subject);
    DDS.rvGrade.setOptions((m.grades||[]).map(g => {
        const gs = String(g);
        const disp = /^\d+(\.0)?$/.test(gs) ? 'پایه ' + faDigits(gs.replace(/\.0$/,'')) : gs;
        return [gs, disp];
      }), RState.grade);
    DDS.rvChapter.setOptions((m.chapters||[]).map(s => [s, chapterFa(s)]), RState.chapter);
    DDS.rvCorp.setOptions((m.corps||[]).map(s => [s, s]), RState.corp);
    DDS.rvDifficulty.setOptions((m.difficulties||[]).map(s => [s, s]), RState.difficulty);
    renderCountsBoard(m);
  }catch(e){ toast('خطا در ارتباط با سرور'); }
}

async function loadReviewRows(){
  const wrap = $('cards');
  wrap.innerHTML = '<div class="loading">در حال بارگذاری…</div>';
  const p = new URLSearchParams();
  if (RState.subject.length)    p.set('subject',    RState.subject.join(','));
  if (RState.status.length)     p.set('status',     RState.status.join(','));
  if (RState.grade.length)      p.set('grade',      RState.grade.join(','));
  if (RState.chapter.length)    p.set('topic',      RState.chapter.join(','));
  if (RState.corp.length)       p.set('corp',       RState.corp.join(','));
  if (RState.difficulty.length) p.set('difficulty', RState.difficulty.join(','));
  if (RState.from) p.set('date_from', RState.from);
  if (RState.to) p.set('date_to', RState.to);
  if (RState.hasAnswer) p.set('has_answer', RState.hasAnswer);
  if (RState.hasPicture) p.set('has_picture', RState.hasPicture);
  p.set('mode', RState.mode);
  p.set('limit', '120');
  try{
    const res = await fetch('/api/review/rows?' + p);
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Server error');

    wrap.innerHTML = '';
    if (!data.rows || !data.rows.length){
      wrap.innerHTML = '<div class="empty-card">موردی یافت نشد.</div>';
      return;
    }
    data.rows.forEach(r => wrap.appendChild(renderCard(r)));
    mountDiagrams();
    typesetMath(wrap);
  }catch(e){
    console.error('QA Load Error:', e);
    wrap.innerHTML = '<div class="empty-card" style="color:var(--red);white-space:pre-wrap;text-align:left;direction:ltr;">Database Error:\n' + e.message + '</div>';
  }
}

/* The one media_images entry a card shows (first array element). The review
   payload carries the JSON array; an older cached payload may still carry a
   JSON string, so both shapes are accepted. */
function mediaEntry(raw){
  let list = raw;
  if (typeof list === 'string'){ try { list = JSON.parse(list); } catch (e) { return null; } }
  if (!Array.isArray(list) || !list.length) return null;
  const first = list[0] || {};
  return first.object ? first : null;
}
function mediaUrl(object){
  return '/api/media?object=' + encodeURIComponent(object || '');
}

function renderCard(r){
  const card = document.createElement('div');
  const status = r.review_status || 'pending';
  card.className = 'qcard st-' + status;
  card.dataset.table = 'questions';
  card.dataset.id = r.id;

  /* header */
  const head = document.createElement('div');
  head.className = 'qhead';
  const num = document.createElement('span');
  num.className = 'qnum';
  num.textContent = 'سؤال ' + faDigits(r.question_number ?? '—');
  const chip = document.createElement('span');
  chip.className = 'status-chip sc-' + status;
  chip.textContent = STATUS_FA[status] || STATUS_FA.pending;
  head.append(num, chip);
  card.appendChild(head);

  /* metadata chips */
  const chips = document.createElement('div');
  chips.className = 'chips';
  [r.subject ? subjectFa(r.subject) : null, r.topic ? chapterFa(r.topic) : null,
   (r.grade != null && r.grade !== '') ? 'پایه ' + faDigits(r.grade) : null,
   r.corp, r.year, r.difficulty,
   r.created_at ? '🗓 ' + faDate(r.created_at) : null].forEach(v => {
    if (!v) return;
    const c = document.createElement('span');
    c.className = 'mchip';
    c.textContent = v;
    chips.appendChild(c);
  });
  /* read-only provenance chips (C3c) - no controls, no coaching text */
  [['duplicate', 'تکراری', 'fa-copy'],
   ['unreadable', 'ناخوانا', 'fa-triangle-exclamation'],
   ['split', 'ادامه‌دار', 'fa-scissors']].forEach(([field, label, icon]) => {
    const v = r[field];
    if (v === null || v === undefined || v === false || v === '') return;
    const c = document.createElement('span');
    c.className = 'qflag f-' + field;
    c.innerHTML = fi(icon) + '<span>' + label +
      (field === 'split' ? ' · ' + dbEsc(String(v)) : '') + '</span>';
    c.title = field + ': ' + String(v);
    chips.appendChild(c);
  });
  card.appendChild(chips);

  /* question body (hidden in answers-only mode) */
  if (RState.mode !== 'answers'){
    const qt = document.createElement('div');
    qt.className = 'qtext';
    // Tables -> real <table>, bare LaTeX -> $$ fences, then MathJax sees them
    const rawText = r.question_text || '';
    qt.innerHTML = mdHtml(rawText);
    card.appendChild(qt);

    const options = parseOptions(r.options);
    if (options.length){
      const ul = document.createElement('ul');
      ul.className = 'opts';
      options.forEach(o => {
        const li = document.createElement('li');
        if (r.correct_option_label && String(o.label) === String(r.correct_option_label))
          li.classList.add('correct');
        const lab = document.createElement('span');
        lab.className = 'opt-label';
        lab.textContent = o.label;
        const txt = document.createElement('span');
        txt.className = 'opt-text';
        txt.innerHTML = mdHtml(o.text);
        li.append(lab, txt);
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }

    const media = mediaEntry(r.media_images);
    if (media && r.source_id){
      const box = document.createElement('div');
      box.className = 'diag';
      box.dataset.object = media.object;
      box.dataset.sourceId = r.source_id;
      box.dataset.questionId = r.id;
      box.dataset.index = '0';
      box.dataset.bbox = media.bbox ? JSON.stringify(media.bbox) : '';
      card.appendChild(box);
    }
  }

  /* answer block — rendered in both/answers modes */
  if (RState.mode !== 'questions'){
    const ab = document.createElement('div');
    ab.className = 'answer-box';
    if (r.answer_id || r.answer_explanation){
      const key = document.createElement('div');
      key.className = 'akey';
      key.textContent = 'گزینهٔ صحیح: ' + (r.correct_option_label || '—') +
        (r.correct_option_text ? ' — ' + r.correct_option_text : '');
      ab.appendChild(key);
      if (r.answer_explanation){
        const expl = document.createElement('div');
        expl.className = 'aexpl';
        expl.innerHTML = mdHtml(r.answer_explanation);
        ab.appendChild(expl);
      }
    } else {
      ab.classList.add('no-answer');
      ab.textContent = 'پاسخی برای این سؤال ثبت نشده است.';
    }
    card.appendChild(ab);
  }

  /* raw OCR toggle */
  if (r.raw_ocr_text){
    const rawWrap = document.createElement('div');
    rawWrap.className = 'raw-wrap';
    const rawBtn = document.createElement('button');
    rawBtn.className = 'raw-toggle';
    rawBtn.type = 'button';
    rawBtn.title = 'نمایش متن خام OCR';
    rawBtn.innerHTML = fi('scroll');
    const rawBox = document.createElement('pre');
    rawBox.className = 'raw-box no-mathjax';
    rawBox.textContent = r.raw_ocr_text;
    rawBox.style.display = 'none';
    rawBtn.addEventListener('click', () => {
      const hidden = rawBox.style.display === 'none';
      rawBox.style.display = hidden ? 'block' : 'none';
      rawBtn.title = hidden ? 'پنهان کردن متن خام' : 'نمایش متن خام OCR';
      rawBtn.classList.toggle('open', hidden);
    });
    rawWrap.append(rawBtn, rawBox);
    card.appendChild(rawWrap);
  }

  /* actions · icon-only, names on hover */
  const foot = document.createElement('div');
  foot.className = 'qfoot';
  const ok = document.createElement('button');
  ok.className = 'act approve';
  ok.type = 'button';
  ok.title = 'تأیید نهایی این ردیف';
  ok.innerHTML = fi('circle-check');
  ok.addEventListener('click', () => setReviewStatus(card, 'approved'));
  const no = document.createElement('button');
  no.className = 'act reject';
  no.type = 'button';
  no.title = 'رد این ردیف';
  no.innerHTML = fi('xmark');
  no.addEventListener('click', () => setReviewStatus(card, 'rejected'));
  foot.append(ok, no);
  card.appendChild(foot);

  return card;
}

/* Robust bbox crop using the image's natural dimensions.
   bbox = [ymin, xmin, ymax, xmax] normalized to a 0-1000 square. */
/* ── bbox helpers ─────────────────────────────────────────── */
function parseBbox(raw){
  let bbox = null;
  try { bbox = JSON.parse(raw || 'null'); } catch(e){}
  if (typeof bbox === 'string'){
    try { bbox = JSON.parse(bbox); } catch(e){ bbox = null; }
  }
  return bbox;
}
function hasValidBbox(bbox){
  if (!Array.isArray(bbox) || bbox.length !== 4) return false;
  if (!bbox.every(Number.isFinite)) return false;
  if (bbox.every(v => Number(v) === 0)) return false;
  const [ymin, xmin, ymax, xmax] = bbox;
  return (ymax > ymin) || (xmax > xmin);
}
/* [ymin, xmin, ymax, xmax] normalized to 0-1000 → pixels.
   X scales with image width, Y scales with image height. */
function bboxToPixels(bbox, nw, nh){
  const [ymin, xmin, ymax, xmax] = bbox.map(Number);
  return {
    x: (xmin/1000) * nw,
    y: (ymin/1000) * nh,
    w: ((xmax - xmin)/1000) * nw,
    h: ((ymax - ymin)/1000) * nh
  };
}

/* ── diagram renderer (canvas crop + full-image overlay) ──── */
/* refit hooks for all mounted diagrams; each drops itself when detached */
const diagRefit = new Set();
let diagResizeT = null;
window.addEventListener('resize', () => {
  clearTimeout(diagResizeT);
  diagResizeT = setTimeout(() => diagRefit.forEach(fn => fn()), 150);
});

function mountDiagrams(){
  document.querySelectorAll('.diag[data-source-id]').forEach(box => {
    if (box.dataset.mounted) return;
    box.dataset.mounted = '1';

    const sourceId = box.dataset.sourceId;
    const object = box.dataset.object || sourceId;
    const questionId = box.dataset.questionId || '';
    let bbox = parseBbox(box.dataset.bbox);
    /* A `crops/...` object is already the finished picture, so the box is
       never re-applied to it: the canvas draws the crop whole. Its editor
       still works - the backdrop becomes the page the crop came from
       (/api/qbank/page) and saving re-cuts that page region, re-uploads it
       over the same object id, and rewrites the stored bbox. Legacy page
       objects keep the old crop-on-canvas behaviour. */
    const preCropped = /^crops\//.test(object);
    const pageUrl = preCropped && questionId
      ? '/api/qbank/page?question=' + encodeURIComponent(questionId) : '';

    /* wrapper: image area + collapsible editor */
    const wrap = document.createElement('div');
    wrap.className = 'diag-wrap';
    box.parentNode.insertBefore(wrap, box);
    wrap.appendChild(box);

    /* The canvas fits whatever is currently rendered (crop or full image),
       but while a slider is being dragged the frame stays put so the
       editor doesn't jump around — it snaps to the new fit on release. */
    const MAX_W = 560, MAX_H = 460;
    const DPR = window.devicePixelRatio || 1;

    /* the stored picture, and - for a crop - the page it was cut from */
    const img = new Image();
    const pageImg = new Image();

    const canvas = document.createElement('canvas');
    canvas.style.width = '100%';
    canvas.style.maxWidth = MAX_W + 'px';
    canvas.style.aspectRatio = '4 / 3';
    canvas.style.display = 'block';
    canvas.style.background = 'var(--surface-2)';
    canvas.width = MAX_W * DPR;
    canvas.height = MAX_W * 0.75 * DPR;
    box.appendChild(canvas);

    function fitTo(cw, ch){
      if (!cw || !ch) return;
      const avail = Math.max(200, (box.clientWidth || MAX_W) - 2);
      let w = Math.min(avail, MAX_W);
      let h = w * ch / cw;
      if (h > MAX_H){ h = MAX_H; w = h * cw / ch; }
      canvas.style.aspectRatio = 'auto';
      canvas.style.width = Math.round(w) + 'px';
      canvas.style.height = Math.round(h) + 'px';
      canvas.width = Math.round(w * DPR);
      canvas.height = Math.round(h * DPR);
    }

    /* ── "نمایش بیشتر" rounded toggle switch under the canvas ── */
    const viewSw = document.createElement('label');
    viewSw.className = 'sw diag-sw';
    const swInput = document.createElement('input');
    swInput.type = 'checkbox';
    const knob = document.createElement('i');
    knob.className = 'tr';
    const cap = document.createElement('em');
    cap.textContent = 'نمایش بیشتر';
    viewSw.append(swInput, knob, cap);
    box.appendChild(viewSw);

    /* ── collapsible crop editor ── */
    const editor = document.createElement('div');
    editor.className = 'crop-editor';
    const head = document.createElement('button');
    head.className = 'crop-toggle';
    head.type = 'button';
    head.innerHTML = '<span>✂ ویرایش برش تصویر</span><span class="chev">▼</span>';
    const panel = document.createElement('div');
    panel.className = 'crop-panel';
    editor.append(head, panel);
    wrap.appendChild(editor);

    head.onclick = () => {
      editor.classList.toggle('open');
      if (editor.classList.contains('open') && mode !== 'full'){
        /* a box is only meaningful over the whole page */
        setMode('full');
        draw(true);
      }
    };

    const AXES = [['ymin','axis-y'], ['xmin','axis-x'],
                  ['ymax','axis-y'], ['xmax','axis-x']];
    const inputs = {}, chips = {};
    AXES.forEach(([key, cls]) => {
      const row = document.createElement('div');
      row.className = 'slider-row ' + cls;
      const lab = document.createElement('label');
      lab.textContent = key;
      const rng = document.createElement('input');
      rng.type = 'range'; rng.min = '0'; rng.max = '1000';
      rng.step = '1'; rng.className = 'cool';
      const chip = document.createElement('span');
      chip.className = 'val';
      row.append(lab, rng, chip);
      panel.appendChild(row);
      inputs[key] = rng; chips[key] = chip;
      /* 'input' = live drag (frame frozen); 'change' = release (snap-fit) */
      rng.addEventListener('input', () => { enforceGap(); draw(false); });
      rng.addEventListener('change', () => { enforceGap(); draw(true); });
    });

    const actions = document.createElement('div');
    actions.className = 'crop-actions';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'crop-save';
    saveBtn.type = 'button';
    saveBtn.textContent = '✓ تأیید برش جدید';
    const resetBtn = document.createElement('button');
    resetBtn.className = 'crop-reset';
    resetBtn.type = 'button';
    resetBtn.textContent = 'بازنشانی';
    actions.append(saveBtn, resetBtn);
    panel.appendChild(actions);

    function vals(){
      return [Number(inputs.ymin.value), Number(inputs.xmin.value),
              Number(inputs.ymax.value), Number(inputs.xmax.value)];
    }
    function setVals(ymin, xmin, ymax, xmax){
      inputs.ymin.value = ymin; inputs.xmin.value = xmin;
      inputs.ymax.value = ymax; inputs.xmax.value = xmax;
      chips.ymin.textContent = ymin; chips.xmin.textContent = xmin;
      chips.ymax.textContent = ymax; chips.xmax.textContent = xmax;
    }
    function initVals(){
      if (hasValidBbox(bbox)) setVals(...bbox.map(v => Math.round(Number(v))));
      else setVals(0, 0, 1000, 1000);
    }
    /* keeps a minimum 10-unit span so the crop never collapses */
    function enforceGap(){
      const MIN = 10;
      let [ymin, xmin, ymax, xmax] = vals();
      if (ymax - ymin < MIN){
        if (ymin + MIN <= 1000) ymax = ymin + MIN;
        else ymin = Math.max(0, ymax - MIN);
      }
      if (xmax - xmin < MIN){
        if (xmin + MIN <= 1000) xmax = xmin + MIN;
        else xmin = Math.max(0, xmax - MIN);
      }
      setVals(ymin, xmin, ymax, xmax);
    }

    let mode = 'crop';
    let imgLoaded = false, pageLoaded = false, pageFailed = false;

    /* full view = the page itself when the stored object is already a crop */
    function activeImg(){
      return (mode === 'full' && preCropped) ? pageImg : img;
    }

    function draw(fit){
      const im = activeImg();
      if (!im || !im.naturalWidth) return;
      const nw = im.naturalWidth, nh = im.naturalHeight;
      const cur = vals();
      let src;

      if (mode === 'crop' && !preCropped){
        src = bboxToPixels(cur, nw, nh);
        if (src.w < 2 || src.h < 2) src = { x:0, y:0, w:nw, h:nh };
      } else {
        src = { x:0, y:0, w:nw, h:nh };
      }

      /* resize the frame to hug the picture being rendered */
      if (fit !== false) fitTo(src.w, src.h);

      const ctx = canvas.getContext('2d');
      const W = canvas.width / DPR, H = canvas.height / DPR;
      ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      ctx.clearRect(0, 0, W, H);

      /* center the content inside the frame (tiny letterbox while dragging) */
      const scale = Math.min(W / src.w, H / src.h);
      const dw = src.w * scale, dh = src.h * scale;
      const ox = (W - dw) / 2, oy = (H - dh) / 2;
      ctx.drawImage(im, src.x, src.y, src.w, src.h, ox, oy, dw, dh);

      if (mode === 'full'){
        /* saved bbox — solid red */
        if (hasValidBbox(bbox)){
          const r = bboxToPixels(bbox, nw, nh);
          ctx.strokeStyle = '#fb7185';
          ctx.lineWidth = 3;
          ctx.setLineDash([]);
          ctx.strokeRect(ox + r.x * scale, oy + r.y * scale, r.w * scale, r.h * scale);
        }
        /* live slider bbox — dashed cyan */
        if (hasValidBbox(cur)){
          const r2 = bboxToPixels(cur, nw, nh);
          ctx.strokeStyle = '#22d3ee';
          ctx.lineWidth = 2;
          ctx.setLineDash([8, 5]);
          ctx.strokeRect(ox + r2.x * scale, oy + r2.y * scale, r2.w * scale, r2.h * scale);
          ctx.setLineDash([]);
        }
      }
    }

    function setMode(next){
      mode = next;
      swInput.checked = next === 'full';
      cap.textContent = next === 'full' ? 'نمایش برش‌خورده' : 'نمایش بیشتر';
      if (next === 'full') ensurePage();
    }

    swInput.onchange = () => {
      setMode(swInput.checked ? 'full' : 'crop');
      draw(true);
    };
    resetBtn.onclick = () => { initVals(); draw(true); };

    saveBtn.onclick = async () => {
      const cur = vals();
      if (cur[2] <= cur[0] || cur[3] <= cur[1]){
        toast('مقادیر برش نامعتبر است');
        return;
      }
      saveBtn.disabled = true;
      saveBtn.textContent = 'در حال ذخیره…';
      try{
        const res = await fetch(
          preCropped ? '/api/qbank/recrop' : '/api/review/bbox', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: questionId, bbox: cur,
                               index: Number(box.dataset.index || 0)})
        });
        const j = await res.json();
        if (!j.ok) throw new Error(j.error || '');
        bbox = cur.slice();
        box.dataset.bbox = JSON.stringify(bbox);
        /* the crop was replaced under its own object id: refetch it */
        if (preCropped && j.url) img.src = j.url;
        saveBtn.textContent = '✓ ذخیره شد';
        toast('برش تصویر ذخیره شد ✓');
        draw(true);
      }catch(e){
        toast('خطا در ذخیرهٔ برش');
        saveBtn.textContent = '✓ تأیید برش جدید';
      }finally{
        saveBtn.disabled = false;
        setTimeout(() => {
          if (saveBtn.textContent === '✓ ذخیره شد')
            saveBtn.textContent = '✓ تأیید برش جدید';
        }, 1400);
      }
    };

    function ensurePage(){
      if (!pageUrl || pageLoaded || pageFailed) return;
      if (!pageImg.src) pageImg.src = pageUrl;
    }

    img.onload = () => {
      imgLoaded = true;
      initVals();
      draw(true);
    };
    img.onerror = () => {
      box.classList.add('diag-error');
      box.textContent = 'تصویر بارگذاری نشد';
      editor.style.display = 'none';
    };
    img.src = mediaUrl(object);

    /* the page a crop was cut from - fetched only when the box is edited */
    pageImg.onload = () => {
      pageLoaded = true;
      if (mode === 'full') draw(true);
    };
    pageImg.onerror = () => {
      pageFailed = true;
      setMode('crop');
      viewSw.style.display = 'none';
      editor.classList.remove('open');
      editor.style.display = 'none';
      toast('تصویر بارگذاری نشد');
      draw(true);
    };

    /* keep the fitted size honest when the window/container changes */
    const refit = () => {
      if (!document.contains(box)){ diagRefit.delete(refit); return; }
      if (imgLoaded) draw(true);
    };
    diagRefit.add(refit);
  });
}

async function setReviewStatus(card, status){
  try{
    const res = await fetch('/api/review/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({table: card.dataset.table, id: card.dataset.id, status})
    });
    const j = await res.json();
    if (!j.ok) throw new Error();
    card.classList.remove('st-approved', 'st-rejected', 'st-pending');
    card.classList.add('st-' + status);
    const chip = card.querySelector('.status-chip');
    chip.className = 'status-chip sc-' + status;
    chip.textContent = STATUS_FA[status];
    toast(status === 'approved' ? 'تأیید شد ✓' : 'رد شد ✗');
    loadReviewMeta(); /* keep the counts board live after each decision */
  }catch(e){ toast('خطا در ذخیرهٔ وضعیت'); }
}

/* toolbar + filter panel wiring */
function activeFilterCount(){
  return [RState.subject, RState.status, RState.grade, RState.chapter,
          RState.corp, RState.difficulty, RState.from, RState.to,
          RState.hasAnswer, RState.hasPicture]
    .filter(v => Array.isArray(v) ? v.length : v).length;
}
function updateFilterBadge(){
  const n = activeFilterCount();
  const badge = $('rvFilterBadge');
  badge.hidden = !n;
  badge.textContent = faDigits(n);
  $('rvFilterBtn').classList.toggle('on', !!n || $('rvFilters').classList.contains('open'));
}
function applyFilters(){ updateFilterBadge(); loadReviewRows(); }

function initFilterUI(){
  DDS.rvSubject.onChange   = v => { RState.subject    = v; applyFilters(); };
  DDS.rvGrade.onChange     = v => { RState.grade      = v; applyFilters(); };
  DDS.rvChapter.onChange   = v => { RState.chapter    = v; applyFilters(); };
  DDS.rvCorp.onChange      = v => { RState.corp       = v; applyFilters(); };
  DDS.rvDifficulty.onChange= v => { RState.difficulty = v; applyFilters(); }
  createDropdown('rvStatus', 'وضعیت', true);
  DDS.rvStatus.setOptions([
    ['pending', 'در انتظار'], ['approved', 'تأیید شده'], ['rejected', 'رد شده']
  ]);
  DDS.rvStatus.onChange = v => { RState.status = v; applyFilters(); };

  /* tri-state binary facets as plain themed dropdowns */
  createDropdown('rvAnswer', 'پاسخ');
  DDS.rvAnswer.setOptions([['', 'همه'], ['1', 'دارای پاسخ'], ['0', 'بدون پاسخ']]);
  DDS.rvAnswer.onChange = v => { RState.hasAnswer = v; applyFilters(); };
  createDropdown('rvPicture', 'تصویر');
  DDS.rvPicture.setOptions([['', 'همه'], ['1', 'دارای تصویر'], ['0', 'بدون تصویر']]);
  DDS.rvPicture.onChange = v => { RState.hasPicture = v; applyFilters(); };

  $('rvFrom').onchange = e => { RState.from = e.target.value; applyFilters(); };
  $('rvTo').onchange   = e => { RState.to   = e.target.value; applyFilters(); };

  /* date pills: keep the empty field clean (no mm/dd/yyyy ghost) and open
     the native picker on click anywhere in the pill, not just the icon */
  const paintDate = el => el.classList.toggle('filled', !!el.value);
  ['rvFrom','rvTo'].forEach(id => {
    const el = $(id);
    paintDate(el);
    el.addEventListener('input', () => paintDate(el));
    el.addEventListener('click', () => { try { el.showPicker(); } catch(_){} });
  });

  function setFilterOpen(open){
    $('rvFilters').classList.toggle('open', open);
    $('rvFilterBtn').setAttribute('aria-expanded', String(open));
    $('rvFilterBtn').classList.toggle('on', open || !!activeFilterCount());
  }
  $('rvFilterBtn').onclick = e => {
    e.stopPropagation();
    setFilterOpen(!$('rvFilters').classList.contains('open'));
    closeAllDD();
  };
  document.addEventListener('click', e => {
    if (!$('rvFilters').classList.contains('open')) return;
    if (!e.target.closest('#rvFilters') && !e.target.closest('#rvFilterBtn')) setFilterOpen(false);
  });
  document.addEventListener('keydown', e => { if (e.key === 'Escape') setFilterOpen(false); });

  $('rvClearFilters').onclick = () => {
    ['subject','status','grade','chapter','corp','difficulty']
      .forEach(k => RState[k] = []);
    RState.from = RState.to = '';
    RState.hasAnswer = RState.hasPicture = '';
    Object.values(DDS).forEach(dd => dd.reset());
    $('rvFrom').value = ''; $('rvTo').value = '';
    ['rvFrom','rvTo'].forEach(id => paintDate($(id)));
    applyFilters();
  };

  const rvRefreshBtn = $('rvRefresh');
  // the icon's one-shot rotation ending is our cue to drop 'spin' (it bubbles from the svg)
  rvRefreshBtn.addEventListener('animationend', () => rvRefreshBtn.classList.remove('spin'));
  rvRefreshBtn.onclick = () => {
    rvRefreshBtn.classList.remove('spin'); void rvRefreshBtn.offsetWidth; rvRefreshBtn.classList.add('spin');
    loadReviewMeta().then(loadReviewRows);
  };

  /* آمار بانک سؤال toggle — slides the whole stats panel down under the toolbar */
  $('rvStatsBtn').onclick = () => {
    const panel = $('rvStatsPanel');
    const open = panel.hidden;
    panel.hidden = !open;
    $('rvStatsBtn').classList.toggle('on', open);
    $('rvStatsBtn').setAttribute('aria-expanded', String(open));
  };

  document.querySelectorAll('#rvMode button').forEach(b => {
    b.onclick = () => {
      document.querySelectorAll('#rvMode button').forEach(x => x.classList.remove('active'));
      b.classList.add('active');
      RState.mode = b.dataset.mode;
      loadReviewRows();
    };
  });

}

/* create the custom dropdowns before the first meta fetch fills them */
createDropdown('rvSubject', 'درس', true);
createDropdown('rvGrade', 'پایه', true);
createDropdown('rvChapter', 'فصل', true);
createDropdown('rvCorp', 'نشر', true);
createDropdown('rvDifficulty', 'سطح', true);
initFilterUI();

/* ════════════ network-mesh background ════════════ */
(function meshBackground(){
  const cvs = document.getElementById('meshBg');
  if (!cvs) return;
  const ctx = cvs.getContext('2d');
  const DPR = Math.min(window.devicePixelRatio || 1, 2);
  let W = 0, H = 0, nodes = [];
  const LINK = 148;        // max distance for a connecting line ("enough space")
  const SPEED = 0.22;      // gentle drift
  const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;

  function build(){
    W = cvs.clientWidth; H = cvs.clientHeight;
    cvs.width = Math.floor(W * DPR); cvs.height = Math.floor(H * DPR);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    const density = Math.round((W * H) / 26000);
    const count = Math.max(38, Math.min(120, density));
    nodes = Array.from({ length: count }, () => ({
      x: Math.random() * W,
      y: Math.random() * H,
      vx: (Math.random() - 0.5) * SPEED,
      vy: (Math.random() - 0.5) * SPEED
    }));
  }

  function step(){
    ctx.clearRect(0, 0, W, H);
    for (const n of nodes){
      if (!reduce){
        n.x += n.vx; n.y += n.vy;
        if (n.x < 0 || n.x > W) n.vx *= -1;
        if (n.y < 0 || n.y > H) n.vy *= -1;
      }
    }
    /* links */
    for (let i = 0; i < nodes.length; i++){
      for (let j = i + 1; j < nodes.length; j++){
        const a = nodes[i], b = nodes[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const d2 = dx*dx + dy*dy;
        if (d2 < LINK*LINK){
          const d = Math.sqrt(d2);
          const alpha = (1 - d / LINK) * 0.18;
          const dark = document.documentElement.dataset.theme !== 'light';
          ctx.strokeStyle = 'rgba(' + (dark ? '255,255,255' : '16,24,48') + ',' + alpha.toFixed(3) + ')';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    /* nodes */
    const dark = document.documentElement.dataset.theme !== 'light';
    ctx.fillStyle = 'rgba(' + (dark ? '255,255,255' : '16,24,48') + ',.5)';
    for (const n of nodes){
      ctx.beginPath();
      ctx.arc(n.x, n.y, 1.3, 0, Math.PI * 2);
      ctx.fill();
    }
    if (!reduce) requestAnimationFrame(step);
  }

  build();
  step();
  let rt = null;
  window.addEventListener('resize', () => {
    clearTimeout(rt);
    rt = setTimeout(() => { build(); if (reduce) step(); }, 180);
  });
})();

/* ═══════════ CREDENTIALS · Gemini key pool & routing ═══════════ */
const CRED_KEEP = '__KEEP__';
const CRED_CLEAR = '__CLEAR__';
const CredState = { loaded:false, rows:[], ladder:[], modelEnabled:null, events:[], supaKeySet:false,
                    gateway:null, usageCap:20, modelLimits:{}, dragIdx:null,
                    extraction:{ engine:'gemini', active:'', providers:[] },
                    revision:{ active:'', providers:[] },
                    providerOk:null, dirty:false, busy:false,
                    enabled:{ gemini:true, ninerouter:true, revision:true, supabase:true },
                    dbConnections:[] };

const credEsc = s => String(s ?? '').replace(/[<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// 'gemini-3.5-flash' → '3.5' (bars would be unreadable otherwise)
function credModelTag(m){
  const s = m.replace(/^gemini[-_]?/i, '');
  // flash-lite keeps a tag of its own: it is the 500/day rung, and two bars
  // both reading "3.5-flash" would hide which cap is which
  if (/lite$/i.test(s)) return s.replace(/[-_]?(flash[-_]?)?lite$/i, '-lite') || m;
  return s.replace(/[-_]?(flash|pro)$/i, '') || m;
}

function credRowEl(r, i){
  const wrap = document.createElement('div');
  const state = r.removed ? 'removed' : (r.checking ? 'checking'
    : (r.state || (r.keep === null ? 'new' : 'unknown')));
  wrap.className = 'cred-key s-' + state;
  if (r.removed){
    wrap.innerHTML = `<i class="dot"></i>` +
      `<span class="cred-rmv">${credEsc(r.masked)} · removed on save</span>` +
      `<button class="cred-btn undo" type="button" title="Undo removal — keep this key">${fi('undo')}</button>`;
    wrap.querySelector('.undo').onclick = () => { r.removed = false; credMarkDirty(); credRender(); };
    return wrap;
  }
  const bars = CredState.ladder.map(m => {
    const um = (r.usage && r.usage.models) || {};
    const lim = (r.usage && r.usage.limits && r.usage.limits[m])
      || CredState.modelLimits[m] || (r.usage && r.usage.limit) || CredState.usageCap;
    const used = um[m] || 0;
    const frac = Math.max(0, Math.min(1, used / (lim || 20)));   // green→red as the cap fills
    const hue = Math.round(130 * (1 - frac));       // green → red as it fills
    let cls = 'untested', why = 'not checked yet';
    if (r.keep === null){ cls = 'pending'; why = 'save this key first'; }
    else if (r.models[m] === 'ok'){ cls = 'ok';   why = 'available for this key'; }
    else if (r.models[m] === 'no_generate'){ cls = 'warn'; why = 'listed but no generateContent'; }
    else if (r.models[m] === 'missing'){ cls = 'dead'; why = 'invisible to this key (404)'; }
    else if ((r.blocked || []).includes(m)){ cls = 'dead'; why = 'failed before — cooling down'; }
    const title = `${m} · ${used}/${lim} sends today · ${why}`;
    return `<b class="mbar m-${cls}" style="--f:${(frac * 100).toFixed(1)}%;--h:${hue}" ` +
      `title="${credEsc(title)}"><i></i>` +
      `<span>${credEsc(credModelTag(m))}<em>${used}/${lim}</em></span></b>`;
  }).join('');
  const keyField = (r.keep !== null && !r.replaced)
    ? `<code class="cred-mask" title="stored in .env — replace to change">${credEsc(r.masked)}</code>` +
      `<button class="cred-btn rep" type="button" title="Replace this key">${fi('pen')}</button>`
    : `<input class="cred-in cred-keyin" type="text" spellcheck="false" autocomplete="off"
        value="${credEsc(r.value || '')}" aria-label="New API key">`;
  wrap.innerHTML =
    `<div class="cred-key-main">` +
    `<button class="cred-btn cred-grip" type="button" title="Hold and drag to reorder — the router tries keys top to bottom">${fi('up-down')}</button>` +
    `<input class="cred-in cred-name" type="text" spellcheck="false" autocomplete="off"
       value="${credEsc(r.name || '')}" aria-label="Key name">` +
    `<span class="cred-date" title="${credEsc(r.added ? 'added ' + r.added : 'no saved date yet')}">` +
    `${credEsc(r.added ? r.added.slice(0, 10) : '—')}</span>` +
    keyField +
    (r.keep !== null
      ? `<button class="cred-btn chk" type="button" title="Re-check this key — one free googleapis listing call, no model used">${fi('flask')}</button>`
      : '') +
    `<div class="cred-models">${bars}</div>` +
    `<button class="cred-btn rm" type="button" title="Remove on save">${fi('trash')}</button>` +
    `</div>`;
  const nameIn = wrap.querySelector('.cred-name');
  nameIn.oninput = () => { r.name = nameIn.value; credMarkDirty(); };
  const keyIn = wrap.querySelector('.cred-keyin');
  if (keyIn) keyIn.oninput = () => { r.value = keyIn.value.trim(); credMarkDirty(); };
  const rep = wrap.querySelector('.rep');
  if (rep) rep.onclick = () => { r.replaced = true; credMarkDirty(); credRender(); };
  const chk = wrap.querySelector('.chk');
  if (chk) chk.onclick = () => credCheckOne(r.keep);
  wrap.querySelector('.rm').onclick = () => {
    if (r.keep === null) CredState.rows.splice(i, 1);
    else r.removed = true;
    credMarkDirty();
    credRender();
  };
  // drag-to-reorder: the grip arms the row, dropping moves it. Order IS
  // routing priority — the saved pool keeps this exact sequence.
  const grip = wrap.querySelector('.cred-grip');
  grip.addEventListener('pointerdown', () => { wrap.draggable = true; });
  wrap.addEventListener('dragstart', e => {
    CredState.dragIdx = i;
    wrap.classList.add('dragging');
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', String(i)); } catch (_){ }
  });
  wrap.addEventListener('dragover', e => {
    if (CredState.dragIdx === null) return;
    e.preventDefault();
    const rect = wrap.getBoundingClientRect();
    const after = e.clientY > rect.top + rect.height / 2;
    wrap.classList.toggle('drop-after', after);
    wrap.classList.toggle('drop-before', !after);
  });
  wrap.addEventListener('dragleave', () => wrap.classList.remove('drop-before', 'drop-after'));
  wrap.addEventListener('drop', e => { e.preventDefault(); credMoveRow(i, e); });
  wrap.addEventListener('dragend', () => {
    wrap.draggable = false;
    CredState.dragIdx = null;
    credRender();
  });
  return wrap;
}

function credMoveRow(target, e){
  const from = CredState.dragIdx;
  if (from === null || from === undefined || from === target) return;
  const rows = CredState.rows;
  const rect = e.currentTarget.getBoundingClientRect();
  let to = target + (e.clientY > rect.top + rect.height / 2 ? 1 : 0);
  const [moved] = rows.splice(from, 1);
  if (from < to) to -= 1;
  rows.splice(to, 0, moved);
  CredState.dragIdx = null;
  credMarkDirty();
  credRender();
}

function credRender(){
  const box = $('credKeys');
  box.innerHTML = '';
  CredState.rows.forEach((r, i) => box.appendChild(credRowEl(r, i)));
  cred9rRow();
  credRenderModelToggles();
  credRender9rModelToggles();
  lanesRender();
}

/* the 9router credential, inline under its section header (no usage, no ring) */
function cred9rRow(){
  const box = $('credKeys9r');
  if (!box) return;
  const ex = CredState.extraction;
  const p = ex.providers.find(x => x.label === ex.active) || ex.providers[0] || null;
  const on = p && p.api_key_set;
  box.innerHTML =
    `<div class="cred-key s-${on ? 'ok' : 'unknown'}"><div class="cred-key-main">` +
    `<i class="dot" title="${on ? 'key set' : 'key not set'}"></i>` +
    `<b class="cred-name-ro">${credEsc(p ? p.label : '9router')}</b>` +
    `<code class="cred-mask">${credEsc((p && p.api_key_masked) || '— not set —')}</code>` +
    `<span class="hint">${credEsc(p && p.model ? p.model : '—')}</span>` +
    `</div></div>`;
}

/* Q2 — extra connections are a presentation list beside Supabase */
function credDbConnRow(c, i){
  const wrap = document.createElement('div');
  wrap.className = 'cred-key s-' + (c.enabled ? 'ok' : 'removed');
  wrap.innerHTML = `<div class="cred-key-main">` +
    `<i class="dot"></i>` +
    `<b class="cred-name-ro">${credEsc(c.label)}</b>` +
    `<span class="hint">${credEsc(c.host || '—')}:${credEsc(c.port || '5432')}/${credEsc(c.db || '—')}</span>` +
    `<span class="hint">${credEsc(c.user || '—')}</span>` +
    `<span class="hint">${credEsc(c.password_set ? 'password set' : '— not set —')}</span>` +
    `<label class="sw"><input type="checkbox" ${c.enabled ? 'checked' : ''} data-conn="${i}">` +
    `<span class="tr" aria-hidden="true"></span><em>Enabled</em></label>` +
    `<button class="cred-btn rm" type="button" data-rm="${i}" title="Remove on save">${fi('trash')}</button></div>`;
  const sw = wrap.querySelector('input[data-conn]');
  if (sw) sw.onchange = () => {
    CredState.dbConnections[i].enabled = sw.checked;
    wrap.className = 'cred-key s-' + (sw.checked ? 'ok' : 'removed');
    credMarkDirty();
  };
  const rm = wrap.querySelector('.rm');
  if (rm) rm.onclick = () => {
    CredState.dbConnections.splice(i, 1);
    credRenderDbConns();
    credMarkDirty();
  };
  return wrap;
}

function credRenderDbConns(){
  const box = $('dbConns');
  if (!box) return;
  box.innerHTML = '';
  (CredState.dbConnections || []).forEach((c, i) => box.appendChild(credDbConnRow(c, i)));
}

/* ── per-model ON/OFF (round .sw, same theme as every switch here) ──
   Gemini: CredState.modelEnabled drives GEMINI_MODELS_ENABLED; null = all rungs on.
   9router: each provider's models_enabled array (null = all on), saved with the
   provider via extraction_providers — the pipeline lane and studio both honour it. */
function modelToggleRow(m, on, section){
  const row = document.createElement('div');
  row.className = 'cred-key cred-model' + (on ? '' : ' s-removed');
  row.innerHTML = `<div class="cred-key-main">` +
    `<i class="dot"></i>` +
    `<b class="cred-name-ro">${credEsc(m)}</b>` +
    `<label class="sw"><input type="checkbox" ${on ? 'checked' : ''} data-mt="${credEsc(m)}">` +
    `<span class="tr" aria-hidden="true"></span><em>Enabled</em></label></div>`;
  row.querySelector('input[data-mt]').onchange = e => {
    const on2 = e.target.checked;
    if (section === 'gemini'){
      // materialise the "all on" (null) state into an explicit list
      const list = CredState.modelEnabled === null
        ? (CredState.ladder || []).slice() : (CredState.modelEnabled || []).slice();
      const i = list.indexOf(m);
      if (on2 && i === -1) list.push(m);
      if (!on2 && i > -1) list.splice(i, 1);
      if (!list.length){ e.target.checked = true; toast('Enable at least one model'); return; }
      CredState.modelEnabled = list;
      credRenderModelToggles();
    } else {
      const p = rtProvider();
      if (!p) return;
      const all = rtModels();
      const list = Array.isArray(p.models_enabled) ? p.models_enabled.slice()
        : all.slice();                       // null = all on → materialise
      const i = list.indexOf(m);
      if (on2 && i === -1) list.push(m);
      if (!on2 && i > -1) list.splice(i, 1);
      if (!list.length){ e.target.checked = true; toast('Enable at least one model'); return; }
      p.models_enabled = (list.length === all.length) ? null : list;  // all on → null
      credRender9rModelToggles();
    }
    credMarkDirty();
  };
  return row;
}

function credRenderModelToggles(){
  const box = $('credModelToggles');
  if (!box) return;
  box.innerHTML = '';
  (CredState.ladder || []).forEach(m => {
    const on = CredState.modelEnabled === null
      || (CredState.modelEnabled || []).includes(m);
    box.appendChild(modelToggleRow(m, on, 'gemini'));
  });
}

function credRender9rModelToggles(){
  const box = $('cred9rModelToggles');
  if (!box) return;
  box.innerHTML = '';
  const p = rtProvider();
  if (!p) return;
  const on = m => Array.isArray(p.models_enabled) ? p.models_enabled.includes(m) : true;
  rtModels().forEach(m => box.appendChild(modelToggleRow(m, on(m), '9router')));
}

/* section enable switches → .env flags; a run with the active provider off is blocked */
const CRED_FLAGS = { swGemini: 'gemini', sw9router: 'ninerouter',
                     swRevision: 'revision', swSupabase: 'supabase' };
Object.keys(CRED_FLAGS).forEach(id => {
  const el = $(id);
  if (el) el.addEventListener('change', () => {
    CredState.enabled[CRED_FLAGS[id]] = el.checked;
    credMarkDirty();
  });
});

function credOcrFields(){
  const off = CredState.extraction.engine === 'gemini';
  ['ocrLabel', 'ocrSel', 'ocrRouterUrl', 'ocrRouterKey', 'ocrRouterKeyClear',
   'ocrRouterModel'].forEach(id => {
    const el = $(id);
    if (el) el.disabled = off;
  });
  // Ladder editor and idle banner: the ladder belongs to the Gemini
  // pool, so it is editable only while that engine is selected.
  const lad = $('credLadder');
  if (lad) lad.disabled = !off;
  const idle = $('credIdleNote');
  if (idle) idle.hidden = off;
  // The live test exists for BOTH modes (empty base_url = test the pool).
  if (off){
    const st = $('ocrProviderStatus');
    if (st){ st.hidden = true; st.textContent = ''; }
  }else{
    credProvStatus(CredState.providerOk);
  }
}

/* Verdict chip under the provider segment: ok = live test passed (shows
   the reply snippet + latency), bad = the last fault, nothing = not tested. */
function credProvStatus(verdict){
  const el = $('ocrProviderStatus');
  if (!el) return;
  const host = CredState.ocrUrlMasked || '';
  if (!verdict){
    if (!host){ el.hidden = true; el.textContent = ''; return; }
    el.hidden = false;
    el.className = 'cred-prov-status';
    el.textContent = 'endpoint: ' + host + ' · not tested yet';
    return;
  }
  el.hidden = false;
  if (verdict.ok){
    el.className = 'cred-prov-status ok';
    el.textContent = '✓ ' + (verdict.detail || (host ? host + ' · reachable' : 'reachable'));
  }else{
    const f = verdict.fault || {};
    el.className = 'cred-prov-status bad';
    el.textContent = '✗ ' + (f.label || verdict.detail || 'unreachable');
  }
}

function credMarkDirty(){
  if (CredState.busy) return;
  CredState.dirty = true;
  credButtons();
}

function credButtons(){
  const busy = CredState.busy || !CredState.loaded;
  const ready = CredState.loaded && CredState.dirty;
  const save = $('credSaveBtn');
  if (save){
    save.disabled = busy || !ready;
    save.title = busy ? 'Loading or busy — wait for the current operation'
      : CredState.dirty ? 'Save everything to .env'
      : 'Nothing to save yet — edit a field or add a provider';
  }
  const r = $('credReloadBtn');
  if (r) r.disabled = busy;
  const st = $('credState');
  if (st) st.textContent = CredState.dirty ? 'unsaved changes' : '';
  const check = $('credCheckBtn');
  if (check) check.disabled = busy;
}

async function loadCredentials(){
  CredState.busy = true;
  credButtons();
  try{
    const res = await fetch('/api/credentials');
    const d = await res.json();
    CredState.loaded = true;
    CredState.ladder = d.models || [];
    // GEMINI_MODELS_ENABLED: null → every rung on (absent key = all on)
    CredState.modelEnabled = Array.isArray(d.model_enabled)
      ? d.model_enabled.slice() : null;
    CredState.gateway = d.gateway || null;
    CredState.usageCap = (d.keys && d.keys[0] && d.keys[0].usage && d.keys[0].usage.limit) || 20;
  CredState.modelLimits = d.model_limits || {};
    CredState.rows = (d.keys || []).map(k => ({
      keep:k.index, masked:k.masked, name:k.name || '', value:'', replaced:false, removed:false,
      added:k.added || '',
      okModel:k.ok_model || '',
      models:{}, blocked:k.blocked_models || [], usage:k.usage || null, calls:k.calls || 0,
      checking:false, latency:k.latency_ms || 0, penalized:!!k.penalized,
      state: k.serving ? 'ok' : k.cooling ? 'cooldown'
             : (k.last_fault === 'bad_key' ? 'dead' : 'unknown'),
    }));
    const lad = $('credLadder');
    if (lad) lad.value = CredState.ladder.join(', ');
    $('credSupaUrl').value = d.supabase_url || '';
    $('credSupaBucket').value = d.supabase_bucket || '';
    $('credSupaKey').value = '';
    $('credSupaKey').placeholder = d.supabase_key_masked || '— not set —';
    $('credSupaKeyClear').checked = false;
    // Per-section saved providers + active selector (TASK 2/3) — two
    // independent objects; loading one never touches the other.
    CredState.extraction = {
      engine: d.extraction_active ? 'custom' : 'gemini',
      active: d.extraction_active || '',
      providers: d.extraction_providers || [],
    };
    CredState.revision = {
      active: d.revision_active || '',
      providers: d.revision_providers || [],
    };
    CredState.providerOk = d.provider_ok || null;
    // Local/other Postgres, beside Supabase (TASK 6).
    $('credPgHost').value = d.postgres_host || '';
    $('credPgPort').value = d.postgres_port || '';
    $('credPgDb').value = d.postgres_db || '';
    $('credPgUser').value = d.postgres_user || '';
    $('credPgPass').value = '';
    $('credPgPass').placeholder = d.postgres_password_masked || '— not set —';
    $('credPgSsl').value = d.postgres_sslmode || '';
    credRenderProviders('extraction');
    credRenderProviders('revision');
    // Section enable flags (Q1) + the presentation-only connection list (Q2).
    CredState.enabled = Object.assign(
      { gemini:true, ninerouter:true, revision:true, supabase:true }, d.enabled || {});
    Object.keys(CRED_FLAGS).forEach(id => {
      const el = $(id);
      if (el) el.checked = !!CredState.enabled[CRED_FLAGS[id]];
    });
    CredState.dbConnections = (d.db_connections || []).map(c => Object.assign({}, c));
    if (!CredState.dbConnections.length && (d.postgres_host || d.postgres_user)){
      CredState.dbConnections = [{
        label:'Local / other', host:d.postgres_host || '', port:d.postgres_port || '',
        db:d.postgres_db || '', user:d.postgres_user || '', sslmode:d.postgres_sslmode || '',
        password_set:!!d.postgres_password_set, enabled:true }];
    }
    credRenderSummaries();
    document.querySelectorAll('#ocrProvider button').forEach(b =>
      b.classList.toggle('active', b.dataset.ocr === CredState.extraction.engine));
    credOcrFields();
    $('credLegacyNote').hidden = !d.legacy_pool;
    CredState.events = d.recent || [];
    CredState.dirty = false;
    CredState.busy = false;
    credRender();
    credButtons();
  }catch(e){
    toast('Credentials: could not load — ' + e);
    CredState.busy = false;
    credButtons();
  }
}

async function credCheck(){
  const btn = $('credCheckBtn');
  const span = btn.querySelector('span');
  if (CredState.busy){ toast('Busy — wait for the current operation'); return; }
  const stored = CredState.rows.filter(r => !r.removed && r.keep !== null);
  if (!stored.length){ toast('Save at least one key before checking'); return; }
  CredState.busy = true;
  credButtons();
  try{
    // one by one, top to bottom — every verdict lands live as it is made
    for (let n = 0; n < stored.length; n++){
      span.textContent = `Checking ${n + 1}/${stored.length}…`;
      await credCheckOne(stored[n].keep);
    }
    const bad = stored.filter(r => r.state === 'dead' || r.state === 'blocked').length;
    const net = stored.filter(r => r.state === 'unreachable').length;
    CredState.events.push({ ts:'', message:'♥ sweep finished — free googleapis calls only, no model was used' });
    if (bad) toast(`Health check: ${bad} key(s) unusable${net ? ` · ${net} unreachable` : ''} — reasons flashed top-right`);
    else if (net) toast(`Health check: ${net} key(s) unreachable — that is the tunnel/region, not the keys`);
    else toast('Health check done — every key reachable');
  }finally{
    CredState.busy = false;
    credButtons();
    span.textContent = 'Check all keys';
  }
}

async function credCheckOne(index){
  const r = CredState.rows.find(x => x.keep === index);
  if (!r) return;
  r.checking = true;
  credRender();
  try{
    const res = await fetch('/api/credentials/check/' + index, { method:'POST' });
    const row = await res.json();
    if (row.error) throw new Error(row.error);
    credApply(row);
  }catch(e){
    r.checking = false;
    credFaultToast(r, {cause:'other', label:'CHECK FAILED', hint:String(e), emoji:'❔'});
    credRender();
  }
}

/* A failed check flashes for 5 s at the top-right (stacking with any others)
   — the row's dot and chip states carry the verdict afterwards. */
function credFaultToast(r, f){
  if (!f) return;
  const el = document.createElement('div');
  el.className = 'toast-err f-' + credEsc(f.cause || 'other');
  el.innerHTML =
    `<b>${f.emoji || '❔'} ${credEsc(f.label || f.kind || 'FAULT')}</b>` +
    `<span>${credEsc(((r && (r.label || r.name)) ? (r.label || r.name) + ' · ' : '') +
      ((r && r.masked) || ''))}</span>` +
    `<i>${credEsc(String(f.hint || '').split('—')[0].split('.')[0].trim().slice(0, 130))}</i>`;
  toastEl(el, 5000);
}

function credApply(row){
  const r = CredState.rows.find(x => x.keep === row.index);
  if (!r) return;
  r.checking = false;
  r.state = row.status;              // ok | alive_unlisted | dead | blocked | unreachable
  r.latency = row.latency_ms || 0;
  r.penalized = !!row.penalized;
  r.models = row.models || {};
  r.blocked = Object.keys(r.models)
    .filter(m => r.models[m] === 'missing' || r.models[m] === 'no_generate');
  credFaultToast(r, row.gateway_only ? null : row.fault);
  r.usage = row.usage || null;
  r.calls = row.calls || 0;
  if (row.usage && row.usage.limit) CredState.usageCap = row.usage.limit;
  if (row.gateway) CredState.gateway = row.gateway;
  CredState.events.push({ ts:'', message:`♥ ${row.label || r.masked} → ${row.status.replace('_', ' ')}` +
    (row.fault ? ` · ${row.fault.emoji || ''} ${row.fault.label}` : '') });
  credRender();
}

/* One model per line or comma-separated → clean ladder array. */
function credParseLadder(text){
  return String(text || '').split(/[,\n]/)
    .map(m => m.trim())
    .filter(Boolean);
}

async function credSave(){
  // Fold the visible four-field form into the active provider rows first,
  // so edits saved without clicking "Add provider" still land (TASK 2/3).
  const fold = (sec, pre) => {
    const st = CredState[sec];
    const p = st.providers.find(x => x.label === st.active);
    if (!p) return;
    p.base_url = $(pre + 'RouterUrl').value.trim() || p.base_url;
    p.model = $(pre + 'RouterModel').value.trim() || p.model;
    const box = $(pre + 'RouterKeyClear');
    const keyIn = $(pre + 'RouterKey');
    if (box && box.checked) p.api_key = '';
    else if (keyIn && keyIn.value.trim()) p.api_key = keyIn.value.trim();
  };
  fold('extraction', 'ocr');
  fold('revision', 'cred');
  const keys = [], names = [];
  let bad = null;
  const ladder = credParseLadder($('credLadder').value);
  if (CredState.extraction.engine === 'gemini' && !ladder.length){
    toast('Credentials: the model ladder cannot be empty — nothing was saved');
    return;
  }
  CredState.rows.forEach(r => {
    if (r.removed) return;
    if (r.keep !== null && !r.replaced){
      keys.push(CRED_KEEP + ':' + r.keep); names.push((r.name || '').trim());
      return;
    }
    const v = (r.value || '').replace(/\s+/g, '');
    if (!v) return;                                  // abandoned half-typed row
    if (v.length < 20){ bad = bad || 'a key looks too short to be a Google API key'; return; }
    keys.push(v); names.push((r.name || '').trim());
  });
  if (bad){ toast('Credentials: ' + bad + ' — nothing was saved'); return; }
  CredState.busy = true;
  credButtons();
  try{
    const res = await fetch('/api/credentials', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({
        gemini_keys: keys, gemini_names: names,
        model_ladder: ladder,             // edited above; validated server-side
        // per-model toggles: null → keep stored (nothing was touched); a list
        // must be a non-empty subset of `ladder` — intersect defensively so an
        // edited ladder can't 400 on a stale toggle (empty → null = keep stored).
        model_enabled: CredState.modelEnabled === null ? null
          : (() => {
              const keep = CredState.modelEnabled.filter(m => ladder.includes(m));
              return keep.length ? keep : null;
            })(),
        supabase_url: $('credSupaUrl').value.trim(),
        supabase_bucket: $('credSupaBucket').value.trim(),
        supabase_key: $('credSupaKeyClear').checked ? CRED_CLEAR
          : ($('credSupaKey').value.trim() || CRED_KEEP),
        postgres_host: $('credPgHost').value.trim() || CRED_KEEP,
        postgres_port: $('credPgPort').value.trim() || CRED_KEEP,
        postgres_db: $('credPgDb').value.trim() || CRED_KEEP,
        postgres_user: $('credPgUser').value.trim() || CRED_KEEP,
        postgres_password: $('credPgPass').value.trim() || CRED_KEEP,
        postgres_sslmode: $('credPgSsl').value.trim() || CRED_KEEP,
        extraction_providers: CredState.extraction.providers,
        extraction_active: CredState.extraction.engine === 'gemini'
          ? '' : (CredState.extraction.active || ''),
        revision_providers: CredState.revision.providers,
        revision_active: CredState.revision.active || '',
        enabled: CredState.enabled,
        db_connections: (CredState.dbConnections || []).map(c => ({
          label: c.label, host: c.host, port: c.port, db: c.db, user: c.user,
          sslmode: c.sslmode, enabled: !!c.enabled,
          password: c.password_set ? (c.password !== undefined ? c.password : CRED_KEEP) : '' })),
      }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || d.error || res.status);
    await loadCredentials();
    toast(`Saved to .env ✓ ${d.key_count} Gemini key(s) routed`);
  }catch(e){ toast('Save failed — ' + e); }
  finally {
    CredState.busy = false;
    credButtons();
  }
}

$('credAddBtn').onclick = () => {
  CredState.rows.push({ keep:null, name:'', value:'', masked:'', replaced:false,
                        removed:false, state:'new', models:{}, blocked:[],
                        usage:null, checking:false });
  credMarkDirty();
  credRender();
};
/* One saved-provider <select> per section — the lists are independent (TASK 3).
   The four form fields below the selector always edit the ACTIVE entry. */
function credRenderProviders(section){
  const st = CredState[section];
  const pre = section === 'extraction' ? 'ocr' : 'cred';
  const sel = $(pre + 'Sel');
  if (!sel) return;
  sel.innerHTML = '';
  if (section === 'revision'){
    const none = document.createElement('option');
    none.value = '';
    none.textContent = st.providers.length ? '— none —' : 'no saved providers yet';
    sel.appendChild(none);
  }
  st.providers.forEach(p => {
    const o = document.createElement('option');
    o.value = p.label; o.textContent = p.label + (p.model ? ' · ' + p.model : '');
    sel.appendChild(o);
  });
  sel.value = st.active || '';
  const p = st.providers.find(x => x.label === st.active);
  if (section === 'extraction') CredState.ocrUrlMasked = p ? p.base_url : '';
  $(pre + 'Label').value = p ? p.label : '';
  $(pre + 'RouterUrl').value = p ? p.base_url : '';
  $(pre + 'RouterModel').value = p ? p.model : '';
  $(pre + 'RouterKey').value = '';
  $(pre + 'RouterKey').placeholder = (p && p.api_key_set)
    ? (p.api_key_masked || '— not set —') : '— not set —';
  const box = $(pre + 'RouterKeyClear');
  if (box) box.checked = false;
}

document.querySelectorAll('#ocrProvider button').forEach(b => {
  b.onclick = () => {
    if (CredState.busy) return;
    document.querySelectorAll('#ocrProvider button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    CredState.extraction.engine = b.dataset.ocr;
    if (b.dataset.ocr === 'gemini'){
      CredState.extraction.active = '';
    } else if (!CredState.extraction.active && CredState.extraction.providers.length){
      CredState.extraction.active = CredState.extraction.providers[0].label;
    }
    credRenderProviders('extraction');
    credOcrFields();
    credMarkDirty();
  };
});
$('ocrSel').onchange = () => {
  CredState.extraction.active = $('ocrSel').value;
  if (CredState.extraction.active) CredState.extraction.engine = 'custom';
  document.querySelectorAll('#ocrProvider button').forEach(b =>
    b.classList.toggle('active', b.dataset.ocr === CredState.extraction.engine));
  credRenderProviders('extraction');
  credOcrFields();
  credMarkDirty();
};
$('credSel').onchange = () => {
  CredState.revision.active = $('credSel').value;
  credRenderProviders('revision');
  credMarkDirty();
};
$('ocrAddProvider').onclick = () => credAddProvider('extraction');
$('credAddProvider').onclick = () => credAddProvider('revision');
['credRouterUrl', 'credRouterKey', 'credRouterModel', 'credRouterKeyClear',
 'ocrRouterUrl', 'ocrRouterKey', 'ocrRouterModel', 'ocrRouterKeyClear',
 'ocrLabel', 'credLabel', 'credLadder',
 'credPgHost', 'credPgPort', 'credPgDb', 'credPgUser', 'credPgPass', 'credPgSsl',
 'credSupaUrl', 'credSupaBucket', 'credSupaKey', 'credSupaKeyClear'].forEach(id => {
  const el = $(id);
  if (el) el.addEventListener('input', credMarkDirty);
});
/* Stage the four visible fields as (or into) the section's provider list.
   A blank key on an existing entry keeps the stored secret — the backend
   resolves the KEEP sentinel. */
function credAddProvider(section){
  const pre = section === 'extraction' ? 'ocr' : 'cred';
  const label = $(pre + 'Label').value.trim();
  const base_url = $(pre + 'RouterUrl').value.trim().replace(/\/+$/, '');
  const model = $(pre + 'RouterModel').value.trim();
  if (!label || !base_url || !model){
    toast('Credentials: label, base URL and model are all needed to add a provider');
    return;
  }
  const st = CredState[section];
  const keyIn = $(pre + 'RouterKey');
  const box = $(pre + 'RouterKeyClear');
  const row = { label, base_url, model };
  if (box && box.checked) row.api_key = '';
  else if (keyIn && keyIn.value.trim()) row.api_key = keyIn.value.trim();
  // else: api_key omitted → the backend keeps the stored key at this position
  const existing = st.providers.find(p => p.label === label);
  if (existing) Object.assign(existing, row);
  else st.providers.push(row);
  st.active = label;
  if (section === 'extraction'){
    st.engine = 'custom';
    document.querySelectorAll('#ocrProvider button').forEach(b =>
      b.classList.toggle('active', b.dataset.ocr === 'custom'));
  }
  credRenderProviders(section);
  credOcrFields();
  credMarkDirty();
  toast('Provider “' + label + '” staged — save to write it to .env');
}

/* ── compact credentials page: summary cards + modal forms ──
   One reusable modal (#credModal) carries whichever form the card
   opened. The form's ids are the SAME ids the inline page used, so
   loadCredentials()/credSave()/credRenderProviders()/the dirty
   listeners all keep working unchanged — only the location moved.
   Modal markup is static (in index.html), never rebuilt, so the
   listeners bound once at load never go stale. */
const CredModal = { form:null, addConn:false };

function credOpenModal(form, title, hint){
  CredModal.form = form;
  CredModal.addConn = false;
  $('credModalTitle').textContent = title;
  $('credModalHint').textContent = hint;
  // exactly one form is visible per open; ids are unique document-wide
  document.querySelectorAll('#credModalBody .cred-form')
    .forEach(el => { el.hidden = el.dataset.form !== form; });
  $('credModal').hidden = false;
  const first = $('credModalBody')
    .querySelector(`[data-form="${form}"] input, [data-form="${form}"] select`);
  if (first) first.focus();
}

function credCloseModal(){ $('credModal').hidden = true; }

/* Compact card body: masked values + a status chip, no inputs.
   Reads only what loadCredentials() already fills: CredState for the
   two provider sections, the (now modal-resident) inputs for the DB
   sections — .value and .placeholder are readable while hidden. No new
   CredState fields are needed: loadCredentials() writes the masked
   secret into each password input's placeholder. */
function credSummary(el, rows){
  el.innerHTML = rows.map(r =>
    `<div class="cred-sum"><span>${credEsc(r[0])}</span>` +
    `<b class="${r[2] || ''}">${credEsc(r[1])}</b></div>`).join('');
}

function credRenderSummaries(){
  const rv = CredState.revision;
  const rp = rv.providers.find(x => x.label === rv.active);
  credSummary($('credSummary'), [
    ['active', rp ? rp.label : '— none —'],
    ['base URL', rp ? (rp.base_url || '—') : '—'],
    ['model', rp ? (rp.model || '—') : '—'],
    ['API key', (rp && rp.api_key_masked) || (rp && rp.api_key_set ? 'set' : '— not set —')],
  ]);
  credSummary($('supaSummary'), [
    ['Project URL', $('credSupaUrl').value || '— not set —'],
    ['Bucket', $('credSupaBucket').value || '— not set —'],
    ['Service key', $('credSupaKey').placeholder || '— not set —'],
  ]);
  credRenderDbConns();
}

/* ONE real model call against the endpoint — reply snippet + latency
   (TASK item 4). Gemini pool mode posts an empty base_url and the
   backend replies ping with the first ladder model; custom mode chats over
   /chat/completions. */
async function credTestChat(section){
  if (CredState.busy){ toast('Busy — wait for the current operation'); return; }
  const st = CredState[section];
  const pre = section === 'extraction' ? 'ocr' : 'cred';
  const poolTest = section === 'extraction' && st.engine === 'gemini';
  const payload = {
    section,
    base_url: poolTest ? '' : $(pre + 'RouterUrl').value.trim(),
    api_key: $(pre + 'RouterKey').value.trim(),
    model: $(pre + 'RouterModel').value.trim(),
  };
  CredState.busy = true;
  credButtons();
  try{
    const res = await fetch('/api/credentials/test', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await res.json();
    const ok = res.ok && d.ok;
    const detail = ok
      ? (d.snippet || 'ping') + ' · ' + d.latency_ms + ' ms'
      : (d.fault || d.error || ('HTTP ' + res.status));
    if (section === 'extraction'){
      CredState.providerOk = { ok, detail, models: [],
                                fault: d.fault ? { label: d.fault } : null };
      credProvStatus(CredState.providerOk);
    }
    CredState.events.push({ ts:'', message: (ok ? '✓ live test — ' : '✗ live test — ') + detail });
    credRouterSummary();
    toast(ok ? 'Test OK — ' + detail : 'Test failed — ' + detail);
  }catch(e){
    toast('Test failed — ' + e);
  }finally{
    CredState.busy = false;
    credButtons();
  }
}

$('credCheckBtn').onclick = credCheck;
$('credSaveBtn').onclick = credSave;
$('ocrProviderTest').onclick = () => credTestChat('extraction');
$('credProviderTest').onclick = () => credTestChat('revision');
$('credReloadBtn').onclick = () => { if (!CredState.busy) loadCredentials(); };
$('ocrEdit').onclick    = () => credOpenModal('extraction',
  'Main OCR provider', 'Four fields: label · base URL · API key · model. Blank key input keeps the stored secret.');
$('credEdit').onclick   = () => credOpenModal('revision',
  'Revision AI provider', 'Separate endpoint — selecting it never changes the extraction engine.');
$('supaEdit').onclick   = () => credOpenModal('supabase',
  'Supabase', 'Project URL, bucket and the service key. Blank key input keeps the stored secret.');
$('pgEdit').onclick     = () => credOpenModal('postgres',
  'Local / other database', 'All six fields are needed to connect; leave SSL mode blank for the driver default.');
$('dbConnAdd').onclick  = () => {
  credOpenModal('postgres', 'Add database connection',
    'Appends a new connection card under Supabase — presentation only, the pipeline keeps its one active connection.');
  CredModal.addConn = true;
};
$('credModalClose').onclick = credCloseModal;
$('credModalSave').onclick  = () => {
  if (CredModal.form === 'postgres' && CredModal.addConn){
    const label = ($('credPgDb').value.trim() || $('credPgHost').value.trim() || 'connection')
      + ' @ ' + ($('credPgHost').value.trim() || 'host');
    CredState.dbConnections.push({
      label, host:$('credPgHost').value.trim(), port:$('credPgPort').value.trim(),
      db:$('credPgDb').value.trim(), user:$('credPgUser').value.trim(),
      sslmode:$('credPgSsl').value.trim(), enabled:true,
      password_set:!!$('credPgPass').value.trim(),
      password:$('credPgPass').value.trim() });
    credRenderDbConns();
  }
  credCloseModal();
  credMarkDirty();
};
$('credModal').addEventListener('click',
  e => { if (e.target === $('credModal')) credCloseModal(); });
// top-level, NOT inside dbWireOnce() — that Escape handler is behind
// DbState.wireOnce and would only arm after the Database tab is opened.
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('credModal').hidden) credCloseModal();
});

/* ═══════════ PIPELINE · proxy profiles (the only proxy surface) ═══════════
   Named profiles live in a git-ignored local JSON store (.proxy-profiles.json,
   edited from the Credentials › Proxy pane); this selector switches the active
   one — "" is the explicit Direct entry (Q4). The GET view carries masked
   passwords only, so every re-save sends the KEEP sentinel back and the server
   keeps the stored secret. */
const PipeState = { loaded:false, profiles:[], active:'', editing:null, results:{} };
const PKEEP = '__KEEP__';
const pipeRows = () => PipeState.profiles.map(p => ({
  name: p.name, host: p.host, port: p.port, scheme: p.scheme, user: p.user,
  enabled: p.enabled !== false,          // round .sw in the Proxy pane (default on)
  password: p.password === undefined ? PKEEP : p.password,
}));

async function loadPipeProfiles(){
  try{
    const d = await (await fetch('/api/profiles')).json();
    PipeState.profiles = d.profiles || [];
    PipeState.active = d.active || '';
    PipeState.loaded = true;
    const sel = $('pipeProxySel');
    sel.innerHTML = '';
    const direct = document.createElement('option');
    direct.value = ''; direct.textContent = 'Direct (no proxy)';
    sel.appendChild(direct);
    PipeState.profiles.forEach(p => {
      if (p.enabled === false) return;    // toggled off — not a selectable lane
      const o = document.createElement('option');
      o.value = p.name; o.textContent = p.name;
      sel.appendChild(o);
    });
    sel.value = PipeState.active;
  }catch(e){ /* first paint before the server is up */ }
  credRenderProfiles();
  pipeGemLoad();
  pipePagesLoad();
}

/* ── pages per call · how many page images of ONE paper ride in a single
   OCR call (the one-by-one engine reads it as OCR_PAGES_PER_CALL). 1 is the
   classic one-picture-at-a-time lane; the value is saved to .env. */
async function pipePagesLoad(){
  const sel = $('pipePagesPerCall');
  if (!sel) return;
  try{
    const d = await (await fetch('/api/pipeline/settings')).json();
    sel.value = String(Math.max(1, Math.min(8, Number(d.pages_per_call) || 1)));
  }catch(e){ /* first paint before the server is up */ }
}

$('pipePagesPerCall').onchange = async () => {
  const value = Number($('pipePagesPerCall').value) || 1;
  try{
    const res = await fetch('/api/pipeline/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages_per_call: value }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    $('pipePagesPerCall').value = String(d.pages_per_call);
    toast('Pages per call: ' + d.pages_per_call);
  }catch(e){ toast('Pages-per-call save failed — ' + e.message); pipePagesLoad(); }
};

/* ── the Gemini model picker · All (round-robin) or ONE pinned model ──
   The options are the credential section's ENABLED ladder models, so a
   single model can be exercised on its own without touching the ladder. */
async function pipeGemLoad(){
  const sel = $('pipeGemSel');
  if (!sel) return;
  try{
    const d = await (await fetch('/api/gemini/pin')).json();
    sel.innerHTML = '';
    const all = document.createElement('option');
    all.value = ''; all.textContent = 'All models';
    sel.appendChild(all);
    (d.models || []).forEach(m => {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      sel.appendChild(o);
    });
    sel.value = d.pin || '';
  }catch(e){ /* first paint before the server is up */ }
}

$('pipeGemSel').onchange = async () => {
  const pin = $('pipeGemSel').value;
  try{
    const res = await fetch('/api/gemini/pin', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pin: pin, surface: 'pipeline' }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    toast(pin ? ('Gemini model pinned to ' + pin) : 'Gemini model: all (round-robin)');
  }catch(e){ toast('Model pin failed — ' + e.message); pipeGemLoad(); }
};

/* the Credentials › Proxy pane lists each saved profile as a row with a
   test (flask), edit (pen) and delete (trash) control at the RIGHT (item 6) */
function credRenderProfiles(){
  const list = $('credProxyList');
  if (!list) return;
  const profs = (typeof PipeState !== 'undefined' && PipeState.profiles) || [];
  if (!profs.length){
    list.innerHTML = '<span class="hint">No profiles saved.</span>';
    return;
  }
  list.innerHTML = profs.map(p => {
    const res = (PipeState.results || {})[p.name];
    const cls = res === true ? ' t-ok' : res === false ? ' t-bad' : '';
    const off = p.enabled === false ? ' s-removed' : '';
    const addr = p.host ? credEsc(p.host) + ':' + credEsc(p.port || '') : 'Direct';
    return `<div class="cred-key cred-prof${cls}${off}" data-name="${credEsc(p.name)}">` +
      `<div class="cred-key-main">` +
        `<span class="cred-prof-name">${credEsc(p.name)}</span>` +
        `<code class="cred-mask" title="${addr}">${addr}</code>` +
        `<label class="sw"><input type="checkbox" ${off ? '' : 'checked'} data-prof-en>` +
        `<span class="tr" aria-hidden="true"></span><em>Enabled</em></label>` +
        `<button class="cred-btn prof-test" type="button" title="Test this proxy — one keyless googleapis probe, no model used">${fi('flask')}</button>` +
        `<button class="cred-btn prof-edit" type="button" title="Edit this profile">${fi('pen')}</button>` +
        `<button class="cred-btn prof-rm" type="button" title="Delete this profile">${fi('trash')}</button>` +
      `</div></div>`;
  }).join('');
  list.querySelectorAll('.cred-prof').forEach(row => {
    const name = row.dataset.name;
    row.querySelector('.prof-test').onclick = () => profTest(name);
    row.querySelector('.prof-edit').onclick = () => proxyOpen('edit', name);
    row.querySelector('.prof-rm').onclick = () => profDelete(name);
    const en = row.querySelector('input[data-prof-en]');
    if (en) en.onchange = () => {
      const p = PipeState.profiles.find(x => x.name === name);
      if (!p) return;
      p.enabled = en.checked;
      row.classList.toggle('s-removed', !en.checked);
      pipePost().catch(e => toast('Proxy: ' + e));   // persisted immediately, like pipeProxySel
    };
  });
}

async function pipePost(msg){
  const res = await fetch('/api/profiles', {
    method:'POST', headers:{ 'Content-Type':'application/json' },
    body: JSON.stringify({ profiles: pipeRows(), active: PipeState.active })
  });
  if (!res.ok){
    const d = await res.json().catch(() => ({}));
    throw new Error(d.error || ('HTTP ' + res.status));
  }
  const d = await res.json();
  PipeState.profiles = d.profiles || [];
  PipeState.active = d.active || '';
  await loadPipeProfiles();
  if (msg) toast(msg);
}

/* switch the active profile from the pipeline-tab selector */
$('pipeProxySel').onchange = async () => {
  PipeState.active = $('pipeProxySel').value;
  try{ await pipePost('Proxy profile saved'); }
  catch(e){ toast('Proxy save failed — ' + e); loadPipeProfiles(); }
};

/* one keyless gateway probe for EVERY enabled profile - no model, no tokens */
$('pipeProxyCheck').onclick = async () => {
  const btn = $('pipeProxyCheck');
  btn.disabled = true;
  try{
    const res = await fetch('/api/proxy/check');
    const d = await res.json();
    if (!res.ok || !d.ok) throw new Error(d.error || ('HTTP ' + res.status));
    const rows = d.results || [];
    if (!rows.length){ toast('No enabled proxy profile'); return; }
    PipeState.results = PipeState.results || {};
    rows.forEach(r => {
      const ok = !!r.ok;
      PipeState.results[r.name] = ok;
      const opt = [...$('pipeProxySel').options].find(o => o.value === r.name);
      if (opt) opt.title = ok ? ('ok - ' + (r.detail || ''))
                             : ('failed - ' + (r.detail || r.kind));
    });
    const line = rows.map(r => r.name + ': '
      + (r.ok ? 'ok' : ('failed ' + (r.detail || r.kind)))).join(' \u00b7 ');
    toast('Proxy check \u2014 ' + line);
  }catch(e){
    toast('Proxy check failed \u2014 ' + e);
  }finally{
    btn.disabled = false;
  }
};

/* one keyless googleapis probe through this profile — no model, no tokens */
async function profTest(name){
  const p = PipeState.profiles.find(x => x.name === name);
  if (!p) return;
  toast('Testing proxy “' + name + '”…');
  try{
    const res = await fetch('/api/profiles/test', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({ name, host:p.host, port:p.port, scheme:p.scheme,
                             user:p.user, password:PKEEP })
    });
    const d = await res.json();
    const ok = res.ok && d.ok;
    PipeState.results = PipeState.results || {};
    PipeState.results[name] = ok;
    credRenderProfiles();
    toast(ok ? 'Proxy “' + name + '” OK — ' + (d.detail || 'reachable')
             : 'Proxy “' + name + '” failed — ' + (d.detail || d.error || res.status));
  }catch(e){
    PipeState.results[name] = false;
    credRenderProfiles();
    toast('Proxy test failed — ' + e);
  }
}

/* the profile pop-up doubles as the ADD form (new) and the edit form (edit) */
function proxyOpen(mode, name){
  PipeState.editing = mode === 'edit' ? name : null;
  const p = mode === 'edit'
    ? (PipeState.profiles.find(x => x.name === name) || {}) : {};
  $('proxyModalTitle').textContent = mode === 'edit'
    ? 'Edit proxy profile' : 'New proxy profile';
  $('pxName').value = p.name || '';
  $('pxHost').value = p.host || '';
  $('pxPort').value = p.port || '';
  $('pxScheme').value = p.scheme || 'http';
  $('pxUser').value = p.user || '';
  $('pxPass').value = '';                     // blank keeps the stored secret
  $('pxName').disabled = mode === 'edit';     // the name is the row key
  $('proxyModalErr').textContent = '';
  $('proxyModal').hidden = false;
  $('pxName').focus();
}
$('pipeProxyNew').onclick = () => proxyOpen('new');   // ADD (criterion 9)
function proxyClose(){ $('proxyModal').hidden = true; }
$('proxyModalClose').onclick = proxyClose;
$('proxyModalCancel').onclick = proxyClose;
$('proxyModal').addEventListener('click', e => { if (e.target === $('proxyModal')) proxyClose(); });
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && !$('proxyModal').hidden) proxyClose();
});

$('proxyModalSave').onclick = async () => {
  const name = $('pxName').value.trim();
  const host = $('pxHost').value.trim();
  const port = $('pxPort').value.trim();
  const err = $('proxyModalErr');
  if (!name || !host || !port){
    err.textContent = 'Name, host and port are required.';
    return;
  }
  const editing = PipeState.editing;
  if (!editing && PipeState.profiles.some(p => p.name === name)){
    err.textContent = 'A profile with that name already exists.';
    return;
  }
  const scheme = ($('pxScheme').value || 'http').toLowerCase();
  const passIn = $('pxPass').value;
  const row = { name, host, port, scheme, user: $('pxUser').value.trim(),
                password: editing ? (passIn || PKEEP) : passIn };
  const before = PipeState.profiles.slice();
  if (editing){
    const i = PipeState.profiles.findIndex(p => p.name === editing);
    PipeState.profiles[i] = Object.assign({}, PipeState.profiles[i], row);
  } else {
    PipeState.profiles.push(row);
  }
  try{
    await pipePost('Profile “' + name + '” saved');
    proxyClose();
  }catch(e){
    PipeState.profiles = before;
    err.textContent = 'Could not save the profile — ' + e;
  }
};

async function profDelete(name){
  if (!window.confirm('Delete the proxy profile “' + name + '”?')) return;
  const before = PipeState.profiles.slice();
  const beforeActive = PipeState.active;
  PipeState.profiles = PipeState.profiles.filter(p => p.name !== name);
  if (PipeState.active === name) PipeState.active = '';
  if (PipeState.results) delete PipeState.results[name];
  try{
    await pipePost('Profile “' + name + '” deleted');
  }catch(e){
    PipeState.profiles = before;
    PipeState.active = beforeActive;
    toast('Delete failed — ' + e);
  }
}

loadPipeProfiles();   // the pipeline tab is the default view — no lazy slot fires

/* ═══════════ PIPELINE · extraction provider selector ═══════════
   Extraction only (user decision Q1) — Revision stays switchable
   from the Credentials tab. ONE source of truth with #ocrSel: the
   persisted EXTRACTION_ACTIVE, read from the same /api/credentials
   view loadCredentials() uses. The payload is rebuilt from that
   SERVER view (not from client state that may never have loaded),
   and every stored key goes back as a KEEP sentinel — because
   CredentialSave.gemini_keys defaults to [] and a body without the
   keys blanks GEMINI_API_KEYS (main.py:614). The pipeline tab is
   the default view, so this loads at startup without ever opening
   the Credentials tab. */
const ProvState = { loaded:false, providers:[], active:'', keys:[], ladder:[] };

async function loadPipeProviders(){
  try{
    const d = await (await fetch('/api/credentials')).json();
    ProvState.providers = d.extraction_providers || [];
    ProvState.active = d.extraction_active || '';
    ProvState.keys = d.keys || [];          // {index, name, masked, …}
    ProvState.ladder = d.models || [];
    ProvState.loaded = true;
    const sel = $('pipeProvSel');
    sel.innerHTML = '';
    const pool = document.createElement('option');
    pool.value = '';
    pool.textContent = 'Gemini pool';
    sel.appendChild(pool);
    ProvState.providers.forEach(p => {
      const o = document.createElement('option');
      o.value = p.label;
      o.textContent = p.label + (p.model ? ' · ' + p.model : '');
      sel.appendChild(o);
    });
    sel.value = ProvState.active;
    // Parallel mode needs the Gemini key pool: a custom (9router) provider has
    // no key to pin a lane to, so disable Parallel and fall back to one-by-one.
    const pm = $('pipeMode');
    if (pm){
      const gemini = !ProvState.active;      // "" == the Gemini pool
      const parOpt = pm.querySelector('option[value="parallel"]');
      if (parOpt) parOpt.disabled = !gemini;
      // Only fall back when parallel is genuinely unavailable (a custom
      // provider is active) — otherwise the stored mode survives refresh.
      if (!gemini && PipeRun.mode === 'parallel'){
        PipeRun.mode = 'one';
        pm.value = 'one';
        try{ localStorage.setItem(PIPE_MODE_KEY, 'one'); }catch(e){}
      }
      pipeModePaint();   // C3-1: repaint the surface after the (possibly forced) mode
    }
  }catch(e){ /* first paint before the server is up */ }
}

$('pipeProvSel').onchange = async () => {
  const label = $('pipeProvSel').value;
  try{
    if (!ProvState.loaded) await loadPipeProviders();
    const res = await fetch('/api/credentials', {
      method:'POST', headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({
        gemini_keys:  ProvState.keys.map(k => CRED_KEEP + ':' + k.index),
        gemini_names: ProvState.keys.map(k => k.name || ''),
        model_ladder: ProvState.ladder,
        extraction_active: label,        // "" = the Gemini pool
      }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.error || d.detail || ('HTTP ' + res.status));
    toast('OCR provider saved to .env');
    await loadPipeProviders();           // read the persisted value back
    // keep the Credentials tab honest without disturbing its edits:
    // refresh only if it is already loaded AND has nothing unsaved.
    if (CredState.loaded && !CredState.dirty) await loadCredentials();
    else if (CredState.loaded){
      CredState.extraction.active = label;
      CredState.extraction.engine = label ? 'custom' : 'gemini';
      credRenderProviders('extraction');
      credOcrFields();
    }
  }catch(e){
    toast('Provider save failed — ' + e);
    loadPipeProviders();                 // put the selector back in sync
  }
};

loadPipeProviders();

/* ═══════════ DATABASE · data console ═══════════
   One UI over the five Supabase tables the pipeline writes:
   grid of the latest uploads, guarded row actions, SQL editor,
   and the weekly backup manager. Backed by /api/db/* (see
   pipeline/dataconsole.py + pipeline/backups.py). */
const DbState = {
  loaded: false, panel: 'grid', tables: [], byName: {},
  table: 'questions', cols: [], pk: [], rows: [], total: 0,
  offset: 0, limit: 100, q: '', sort: '', order: 'desc',
  rowActions: false, lastSqlResult: null, wireOnce: false,
};

const dbEsc = credEsc;
const dbBigType = t => /text|json|bytea/.test(t || '');

/* MathJax only typesets delimited math. The OCR sometimes drops the $ around
   an expression ("\rho = ... \frac{...}{...}"), which then reads as raw LaTeX.
   A command ("\frac") or a script ("q_3") is a reliable marker; the run grows
   over its Latin/digits/operators and brace groups until prose (Persian
   letters) starts again. Delimited math is copied untouched. */
function fenceBareTex(s){
  if (!s) return s;
  const persian = /[\u0600-\u06FF\u0750-\u077F]/;
  const word = /[A-Za-z0-9]/;
  const punct = '+-*/().,;:=[]~%\'!?_^';
  const growLeft = (text, end) => {
    let pos = end;
    for (;;){
      let j = pos;
      while (j > 0 && (text[j - 1] === ' ' || text[j - 1] === '\t')) j--;
      if (j === 0) break;
      const ch = text[j - 1];
      if (ch === '}'){
        let depth = 0, k = j - 1;
        for (; k >= 0; k--){
          if (text[k] === '}') depth++;
          else if (text[k] === '{'){ depth--; if (!depth) break; }
        }
        if (k < 0) break;
        let start = k;
        const cmd = text.slice(0, k).match(/\\[A-Za-z]+$/);
        if (cmd) start = k - cmd[0].length;
        pos = start;
        continue;
      }
      if (ch === '\\'){
        const cmd = text.slice(0, j).match(/\\[A-Za-z]+$/);
        if (!cmd) break;
        pos = j - cmd[0].length;
        continue;
      }
      if (word.test(ch)){
        let k = j;
        while (k > 0 && word.test(text[k - 1])) k--;
        pos = k;
        continue;
      }
      if (punct.indexOf(ch) !== -1){
        const prev = text[j - 2];
        if ((ch === '.' || ch === ',' || ch === ';' || ch === ':')
            && prev && persian.test(prev)) break;
        pos = j - 1;
        continue;
      }
      break;
    }
    return pos;
  };
  const growRight = (text, start) => {
    let pos = start;
    for (;;){
      let j = pos;
      while (j < text.length && (text[j] === ' ' || text[j] === '\t')) j++;
      if (j >= text.length) break;
      const ch = text[j];
      if (ch === '{'){
        let depth = 0, k = j;
        for (; k < text.length; k++){
          if (text[k] === '{') depth++;
          else if (text[k] === '}'){ depth--; if (!depth) break; }
        }
        if (k >= text.length) break;
        pos = k + 1;
        continue;
      }
      if (ch === '\\'){
        const cmd = text.slice(j).match(/^\\[A-Za-z]+/);
        if (!cmd) break;
        pos = j + cmd[0].length;
        continue;
      }
      if (word.test(ch)){
        let k = j;
        while (k < text.length && word.test(text[k])) k++;
        pos = k;
        continue;
      }
      if (punct.indexOf(ch) !== -1){
        const next = text[j + 1];
        if ((ch === '.' || ch === ',' || ch === ';' || ch === ':')
            && next && persian.test(next)) break;
        pos = j + 1;
        continue;
      }
      break;
    }
    return pos;
  };
  const fencePlain = text => {
    if (text.indexOf('\\') === -1 && text.indexOf('_') === -1
        && text.indexOf('^') === -1) return text;
    const strong = /\\[A-Za-z]+|[_^]\{(?:[^{}]|\{[^{}]*\})*\}|[_^][A-Za-z0-9]/g;
    let out = '', cursor = 0, m;
    while ((m = strong.exec(text))){
      if (m.index < cursor) continue;
      let start = growLeft(text, m.index);
      const end = growRight(text, m.index + m[0].length);
      if (start < cursor) start = cursor;
      if (end <= start) continue;
      out += text.slice(cursor, start) + '$' + text.slice(start, end) + '$';
      cursor = end;
      strong.lastIndex = end;
    }
    return out + text.slice(cursor);
  };
  const protectedMath = /\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]/g;
  let out = '', last = 0, m;
  while ((m = protectedMath.exec(s))){
    out += fencePlain(s.slice(last, m.index)) + m[0];
    last = m.index + m[0].length;
  }
  return out + fencePlain(s.slice(last));
}
/* QBank / DB text -> HTML: fence bare LaTeX environments, render pipe tables.
   Escapes first, so every '<' the OCR produced stays literal. */
function mdHtml(raw){
  let s = dbEsc(String(raw == null ? '' : raw)).replace(/\r\n?/g, '\n');
  if (!s) return '';
  /* 0. MathJax has no LaTeX float/table environments and the OCR emits them
        ("\begin{table}[] \centering \begin{tabular}{|c|c|}"). Left alone
        they draw a red "Unknown environment" where the table belongs. The
        `table` float is unwrapped (its body is the maths), `tabular` becomes
        `array` (same column spec), and \centering is a no-op here. */
  s = s.replace(/\\begin\{table\}(?:\[[^\]]*\])?/g, '')
       .replace(/\\end\{table\}/g, '')
       .replace(/\\begin\{tabular\}(?:\[[^\]]*\])?/g, '\\begin{array}')
       .replace(/\\end\{tabular\}/g, '\\end{array}')
       .replace(/\\centering\b\s*/g, '');
  /* 1. each bare LaTeX environment becomes display math; prose stays prose.
        The replacement MUST be a function: in a *string* replacement a '$$'
        sequence is an escape, so the fence would silently vanish. */
  if (s.indexOf('\\begin{') !== -1 && s.indexOf('$$') === -1 && s.indexOf('\\[') === -1)
    s = s.replace(/(\\begin\{[a-zA-Z*]+\}[\s\S]*?\\end\{[a-zA-Z*]+\})/g,
                  m => '$$' + m + '$$');
  /* 1b. A display block that still nests inline math ("$^6\text{A}$" inside
        an OCR'd table) dies on the misplaced `$`: inside $$ the delimiters are
        dropped and the TeX runs inline. Escaped \$ survives. */
  s = s.replace(/\$\$([\s\S]*?)\$\$/g,
                (m, inner) => '$$' + inner.replace(/(^|[^\\])\$/g, '$1') + '$$');
  /* 1c. a formula that lost its delimiters reads as raw LaTeX: fence the run here. */
  s = fenceBareTex(s);
  /* 2. pipe tables -> real <table>, so MathJax inside cells still works */
  const lines = s.split('\n'), out = [];
  for (let i = 0; i < lines.length; i++){
    const next = lines[i + 1] || '';
    if (/^\s*\|.*\|\s*$/.test(lines[i]) && /^\s*\|[\s:|-]+\|\s*$/.test(next)){
      const rows = [];
      const cells = line => line.trim().replace(/^\||\|$/g, '').split('|')
        .map(c => '<td>' + c.trim() + '</td>').join('');
      rows.push('<tr>' + cells(lines[i]) + '</tr>');
      i += 2;
      for (; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++)
        rows.push('<tr>' + cells(lines[i]) + '</tr>');
      i--;
      out.push('<table class="md-table"><tbody>' + rows.join('') + '</tbody></table>');
      continue;
    }
    out.push(lines[i]);
  }
  /* 3. everything that is not a table keeps its line breaks */
  return out.join('<br>').replace(/<br>(<table)/g, '$1')
            .replace(/(<\/table>)<br>/g, '$1');
}

async function dbApi(url, opts){
  const res = await fetch(url, opts);
  const d = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(d.error || res.status + ' ' + res.statusText);
  return d;
}
const dbJson = (url, body) => dbApi(url, {
  method: 'POST', headers: { 'content-type': 'application/json' },
  body: JSON.stringify(body),
});

function dbFmt(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString(undefined,
    { year:'numeric', month:'short', day:'2-digit',
      hour:'2-digit', minute:'2-digit' });
}

/* ── init + table cards ── */
async function dbInit(){
  dbWireOnce();
  try {
    const ov = await dbApi('/api/db/overview');
    DbState.tables = ov.tables;
    DbState.byName = Object.fromEntries(ov.tables.map(t => [t.name, t]));
    dbRenderStrip();
    dbSelect(DbState.tables.some(t => t.name === DbState.table)
             ? DbState.table : ov.tables[0].name);
    dbBkLoad();
  } catch (e) {
    $('dbStrip').innerHTML =
      '<div class="db-empty db-err">Database unreachable — ' + dbEsc(e.message) + '</div>';
  }
}

function dbRenderStrip(){
  $('dbStrip').innerHTML = DbState.tables.map(t => `
    <button type="button" class="db-card${t.name === DbState.table ? ' active' : ''}"
            data-t="${t.name}" title="${dbEsc(t.name)} · ${t.columns.length} columns">
      <b>${dbEsc(t.name)}</b>
      <span class="db-card-n">${t.rows.toLocaleString()}</span>
      <label>rows · newest ${dbEsc(dbFmt(t.latest))}</label>
    </button>`).join('');
  $('dbStrip').querySelectorAll('.db-card').forEach(b =>
    b.onclick = () => dbSelect(b.dataset.t));
}

function dbSelect(table){
  const meta = DbState.byName[table];
  if (!meta) return;
  DbState.table = table;
  DbState.cols = meta.columns;
  DbState.pk = meta.pk;
  DbState.rowActions = meta.row_actions;
  DbState.sort = meta.ts_column || '';
  DbState.order = 'desc';
  DbState.offset = 0;
  $('dbSearch').value = DbState.q = '';
  $('dbAddBtn').disabled = !meta.row_actions;
  $('dbCrumb').textContent =
    meta.rows.toLocaleString() + ' rows · ' + meta.columns.length + ' columns · '
    + 'showing latest ' + Math.min(DbState.limit, meta.rows);
  dbRenderStrip();
  dbLoadRows();
}

/* ── data grid ── */
async function dbLoadRows(){
  const g = $('dbGrid');
  g.innerHTML = '<div class="db-empty">Loading <b>' + dbEsc(DbState.table) + '</b>…</div>';
  const p = new URLSearchParams({
    table: DbState.table, limit: DbState.limit, offset: DbState.offset,
    q: DbState.q, sort: DbState.sort, order: DbState.order,
  });
  try {
    const d = await dbApi('/api/db/rows?' + p);
    DbState.rows = d.rows; DbState.total = d.total; DbState.cols = d.columns; DbState.pk = d.pk;
    dbRenderGrid();
  } catch (e) {
    g.innerHTML = '<div class="db-empty db-err">' + dbEsc(e.message) + '</div>';
  }
}

/* A cell that holds a table or a formula gets a formatted preview; the title
   keeps the raw value so nothing is hidden from a curious eye. */
function dbRichHtml(s){
  const html = mdHtml(s);
  const rich = html.indexOf('<table') !== -1 || html.indexOf('$$') !== -1;
  if (!rich) return null;
  return '<span class="db-rich">' + html + '</span>';
}

function dbCellHtml(v){
  if (v === null || v === undefined) return '<i class="db-null">NULL</i>';
  if (v === true || v === false)
    return '<span class="db-bool ' + (v ? 'yes' : 'no') + '">' + v + '</span>';
  const s = String(v);
  const rich = dbRichHtml(s);
  if (rich) return '<div class="db-rich-wrap" title="' + dbEsc(s.slice(0, 900)) + '">'
    + rich + '</div>';
  return '<span class="db-v" title="' + dbEsc(s.length > 900 ? s.slice(0, 900) + '…' : s)
         + '">' + dbEsc(s.length > 240 ? s.slice(0, 240) + '…' : s) + '</span>';
}

function dbRenderGrid(){
  const g = $('dbGrid');
  if (!DbState.rows.length){
    g.innerHTML = '<div class="db-empty">No rows' +
      (DbState.q ? ' match “' + dbEsc(DbState.q) + '”' : '') + '.</div>';
    dbRenderPagebar(); return;
  }
  const names = DbState.cols.map(c => c.name);
  const head = DbState.cols.map(c => {
    const on = DbState.sort === c.name;
    return `<th data-c="${c.name}" title="${dbEsc(c.type)}">${dbEsc(c.name)}
      <em>${dbEsc(c.type.replace(/ with time zone/, 'tz'))}</em>
      <s class="arr">${on ? (DbState.order === 'asc' ? '▲' : '▼') : '⇅'}</s></th>`;
  }).join('') + '<th class="db-act-h">row</th>';
  const body = DbState.rows.map((r, i) => '<tr data-i="' + i + '">' +
    names.map(n => '<td class="' + (n === DbState.pk[0] ? 'db-pk ' : '')
      + (n.endsWith('_status') ? 'db-st ' : '') + '">' + dbCellHtml(r[n]) + '</td>').join('') +
    '<td class="db-act">' + (DbState.rowActions ? `
      <button type="button" class="db-ib" data-a="edit"   title="Edit row">${fi('pen')}</button>
      <button type="button" class="db-ib" data-a="dup"    title="Duplicate row">${fi('copy')}</button>
      <button type="button" class="db-ib del" data-a="del" title="Delete this row (saved to graveyard)">${fi('ghost')}</button>`
      : '') + '</td></tr>').join('');
  g.innerHTML =
    `<table class="db-grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  typesetMath(g);

  g.querySelectorAll('th[data-c]').forEach(th => th.onclick = () => {
    const c = th.dataset.c;
    if (DbState.sort === c) DbState.order = DbState.order === 'asc' ? 'desc' : 'asc';
    else { DbState.sort = c; DbState.order = 'asc'; }
    DbState.offset = 0; dbLoadRows();
  });
  g.querySelectorAll('tbody tr').forEach(tr => {
    const r = DbState.rows[+tr.dataset.i];
    tr.querySelectorAll('.db-ib').forEach(b => b.onclick = ev => {
      ev.stopPropagation();
      const a = b.dataset.a, id = r[DbState.pk[0]];
      if (a === 'edit') dbOpenModal('edit', r);
      if (a === 'dup')  dbOpenModal('dup',  r);
      if (a === 'del')  dbDeleteFlow(b, id);
    });
    tr.ondblclick = () => { if (DbState.rowActions) dbOpenModal('edit', r); };
  });
  dbRenderPagebar();
}

function dbRenderPagebar(){
  const bar = $('dbPagebar');
  const from = DbState.total ? DbState.offset + 1 : 0;
  const to = Math.min(DbState.offset + DbState.limit, DbState.total);
  bar.innerHTML = `
    <button class="icon-btn sm" id="dbPrev" type="button" title="Previous page" ${DbState.offset <= 0 ? 'disabled' : ''}>${fi('chevron-left')}</button>
    <span class="count-line">${from}–${to} of ${DbState.total.toLocaleString()}
      ${DbState.q ? ' · search: “' + dbEsc(DbState.q) + '”' : ''}</span>
    <button class="icon-btn sm" id="dbNext" type="button" title="Next page" ${to >= DbState.total ? 'disabled' : ''}>${fi('chevron-right')}</button>`;
  $('dbPrev').onclick = () => { DbState.offset = Math.max(0, DbState.offset - DbState.limit); dbLoadRows(); };
  $('dbNext').onclick = () => { DbState.offset += DbState.limit; dbLoadRows(); };
}

/* two-step delete: first click arms the button, second confirms */
function dbDeleteFlow(btn, id){
  if (btn.dataset.armed){
    btn.disabled = true;
    dbJson('/api/db/delete', { table: DbState.table, id })
      .then(() => { toast('Row deleted · kept in backups/row_graveyard.jsonl'); dbLoadRows(); })
      .catch(e => { btn.disabled = false; toast('Delete failed — ' + e.message); });
    return;
  }
  btn.dataset.armed = '1';
  btn.classList.add('armed');
  btn.innerHTML = fi('triangle-exclamation');
  btn.title = 'Click again to delete for good';
  setTimeout(() => {
    delete btn.dataset.armed; btn.classList.remove('armed');
    btn.innerHTML = fi('ghost'); btn.title = 'Delete this row (saved to graveyard)';
  }, 2600);
}

/* ── row modal (edit · duplicate · insert) ── */
let dbModalMode = 'edit', dbModalRow = null;

function dbOpenModal(mode, row){
  dbModalMode = mode; dbModalRow = row || {};
  const isWrite = mode !== 'edit';
  const pkCol = DbState.pk[0];
  $('dbModalTitle').textContent = mode === 'edit'
    ? 'Edit ' + DbState.table + ' · ' + String(dbModalRow[pkCol] ?? '').slice(0, 8)
    : mode === 'dup' ? 'Duplicate ' + DbState.table + ' row'
                     : 'New ' + DbState.table + ' row';
  const body = $('dbModalBody');
  body.innerHTML = DbState.cols.map(c => {
    const n = c.name;
    if (!isWrite && n === pkCol) return `
      <label class="db-f"><span>${dbEsc(n)} <em>${dbEsc(c.type)}</em></span>
        <input class="db-in mono" value="${dbEsc(String(dbModalRow[n] ?? ''))}" readonly></label>`;
    if (n === 'created_at' || n === 'last_at'){
      return isWrite ? '' : `
      <label class="db-f"><span>${dbEsc(n)} <em>${dbEsc(c.type)}</em></span>
        <input class="db-in mono" value="${dbEsc(String(dbModalRow[n] ?? ''))}" readonly></label>`;
    }
    let v;
    if (mode === 'add')      v = n === pkCol ? crypto.randomUUID() : '';
    else if (mode === 'dup') v = n === pkCol ? crypto.randomUUID() : String(dbModalRow[n] ?? '');
    else                     v = dbModalRow[n] === null || dbModalRow[n] === undefined ? '' : String(dbModalRow[n]);
    const field = dbBigType(c.type) || v.length > 60
      ? `<textarea class="db-in mono tall" data-f="${n}" spellcheck="false">${dbEsc(v)}</textarea>`
      : `<input class="db-in mono" data-f="${n}" value="${dbEsc(v)}" spellcheck="false">`;
    return `<label class="db-f"><span>${dbEsc(n)}${c.null ? '' : ' <b>*</b>'}
      <em>${dbEsc(c.type)}</em>${c.default ? '<i class="df">def: ' + dbEsc(c.default) + '</i>' : ''}</span>
      ${field}</label>`;
  }).join('');
  $('dbModal').hidden = false;
  const first = body.querySelector('[data-f]');
  if (first) first.focus();
}

function dbCloseModal(){ $('dbModal').hidden = true; }

async function dbSaveModal(){
  const btn = $('dbModalSave');
  const vals = {};
  $('dbModalBody').querySelectorAll('[data-f]').forEach(el => {
    const v = el.value;
    vals[el.dataset.f] = v === '' ? null : v;
  });
  btn.disabled = true;
  try {
    if (dbModalMode === 'edit'){
      const id = dbModalRow[DbState.pk[0]];
      let changed = 0;
      for (const [col, v] of Object.entries(vals)){
        const before = dbModalRow[col] === null ? '' : String(dbModalRow[col] ?? '');
        if ((v === null ? '' : String(v)) !== before){
          await dbJson('/api/db/cell', { table: DbState.table, id, column: col, value: v });
          changed++;
        }
      }
      toast(changed ? changed + ' field' + (changed > 1 ? 's' : '') + ' saved ✓'
                    : 'Nothing changed');
    } else {
      await dbJson('/api/db/insert', { table: DbState.table, values: vals });
      toast('Row inserted ✓');
    }
    dbCloseModal();
    dbRefreshAll();
  } catch (e) {
    toast('Save failed — ' + e.message);
  } finally { btn.disabled = false; }
}

function dbRefreshAll(){
  dbLoadRows();
  dbApi('/api/db/overview').then(ov => {
    DbState.tables = ov.tables;
    DbState.byName = Object.fromEntries(ov.tables.map(t => [t.name, t]));
    dbRenderStrip();
  }).catch(() => {});
}

/* ── CSV export (current page, client-side) ── */
function dbToCsv(cols, rows){
  const q = s => '"' + String(s ?? '').replace(/"/g, '""') + '"';
  const text = '\uFEFF' + cols.map(q).join(',') + '\n' +
    rows.map(r => cols.map(c => q(r[c])).join(',')).join('\n');
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([text], { type: 'text/csv;charset=utf-8' }));
  a.download = (DbState.panel === 'sql' ? 'query_result' : DbState.table + '_rows') + '.csv';
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* ── SQL editor ── */
const SQL_SNIPPETS = [
  ['Row counts per table',
    "SELECT 'sources' AS tbl, COUNT(*) AS n FROM sources\nUNION ALL SELECT 'questions', COUNT(*) FROM questions\nUNION ALL SELECT 'answers', COUNT(*) FROM answers\nUNION ALL SELECT 'revision_reports', COUNT(*) FROM revision_reports\nUNION ALL SELECT 'revision_summary', COUNT(*) FROM revision_summary;"],
  ['Latest uploads with question counts',
    "SELECT s.file_name, s.subject, s.created_at,\n       COUNT(q.id) AS questions\nFROM sources s LEFT JOIN questions q ON q.source_id = s.id\nGROUP BY s.id, s.file_name, s.subject, s.created_at\nORDER BY s.created_at DESC LIMIT 20;"],
  ['Questions by subject × status',
    "SELECT subject,\n       COUNT(*) AS total,\n       SUM((review_status = 'approved')::int) AS approved,\n       SUM((needs_review)::int) AS needs_review\nFROM questions GROUP BY subject ORDER BY total DESC;"],
  ['Newest revision flags',
    "SELECT run_id, entity, field, severity, issue, created_at\nFROM revision_reports\nORDER BY created_at DESC LIMIT 50;"],
  ['Column metadata (read-only peek)',
    "SELECT table_name, column_name, data_type\nFROM information_schema.columns\nWHERE table_schema = 'public' AND table_name = 'answers'\nORDER BY ordinal_position;"],
  ['Fix one field (template)',
    "UPDATE questions SET review_status = 'pending'\nWHERE id = 'paste-a-row-uuid-here';"],
];

async function sqlRun(){
  const sql = $('sqlInput').value.trim();
  if (!sql){ toast('Type a statement first'); return; }
  const btn = $('sqlRunBtn');
  btn.disabled = true; btn.classList.add('stopping');
  const out = $('sqlResult');
  try {
    const d = await dbJson('/api/db/sql', { sql });
    if (!d.ok){
      out.innerHTML = '<div class="db-sql-msg err">⛔ ' + dbEsc(d.error) + '</div>';
      DbState.lastSqlResult = null;
      if (d.affected !== undefined) dbRefreshAll();
      return;
    }
    const took = '<span class="db-sql-msg ok">✓ ' + d.elapsed_ms + ' ms · ' +
      (d.kind === 'write'
        ? d.affected + ' row' + (d.affected === 1 ? '' : 's') + ' affected'
        : d.rows.length + ' row' + (d.rows.length === 1 ? '' : 's')) +
      (d.truncated ? ' (truncated at 1000)' : '') + '</span>';
    if (d.kind === 'write'){
      out.innerHTML = took;
      DbState.lastSqlResult = null;
      dbRefreshAll();
      return;
    }
    DbState.lastSqlResult = d;
    out.innerHTML = took + (d.rows.length
      ? '<table class="db-grid sql"><thead><tr>' +
        d.columns.map(c => '<th>' + dbEsc(c) + '</th>').join('') + '</tr></thead><tbody>' +
        d.rows.map(r => '<tr>' + d.columns.map(c => '<td>' + dbCellHtml(r[c]) + '</td>').join('')
                 + '</tr>').join('') + '</tbody></table>'
      : '<div class="db-empty">Statement returned no rows.</div>');
  } catch (e) {
    out.innerHTML = '<div class="db-sql-msg err">⛔ ' + dbEsc(e.message) + '</div>';
  } finally {
    btn.disabled = false; btn.classList.remove('stopping');
    btn.innerHTML = fi('power-off');
  }
}

/* ── backups panel ── */
async function dbBkLoad(){
  const list = $('dbBackupList');
  try {
    const d = await dbApi('/api/db/backups');
    if (!d.backups.length){
      list.innerHTML = '<div class="db-empty">No snapshots yet — press “Back up now”.</div>';
      return;
    }
    list.innerHTML = d.backups.map(b => `
      <details class="db-bk-set" ${b === d.backups[0] ? 'open' : ''}>
        <summary>
          <b>${dbEsc(b.created_utc.replace('T', ' ').slice(0, 16))} UTC</b>
          <span class="db-bk-chip ${b.trigger === 'weekly' ? '' : 'man'}">${dbEsc(b.trigger)}</span>
          <em>${b.total_rows.toLocaleString()} rows · ${(b.total_bytes / 1048576).toFixed(2)} MB · ${b.duration_s}s</em>
        </summary>
        <div class="db-bk-tables">${Object.entries(b.tables).map(([t, info]) => `
          <span class="db-bk-t"><b>${dbEsc(t)}</b> ${info.rows.toLocaleString()} rows
            <a href="/api/db/backup/download?set=${encodeURIComponent(b.dir)}&table=${t}&fmt=sql" download>SQL</a>
            <a href="/api/db/backup/download?set=${encodeURIComponent(b.dir)}&table=${t}&fmt=json" download>JSON</a>
          </span>`).join('')}</div>
      </details>`).join('');
  } catch (e) {
    list.innerHTML = '<div class="db-empty db-err">' + dbEsc(e.message) + '</div>';
  }
}

async function dbBackupNow(){
  const btn = $('dbBackupNow');
  btn.disabled = true; btn.classList.add('stopping');
  try {
    const d = await dbJson('/api/db/backup/run', {});
    toast('Snapshot ' + d.manifest.name + ' · ' + d.manifest.total_rows + ' rows ✓');
    dbBkLoad();
  } catch (e) {
    toast('Backup failed — ' + e.message);
  } finally {
    btn.disabled = false; btn.classList.remove('stopping');
    btn.innerHTML = fi('box-archive');
  }
}

/* ── panel switching + wiring ── */
function dbShowPanel(name){
  DbState.panel = name;
  $('dbPanels').querySelectorAll('button').forEach(b =>
    b.classList.toggle('active', b.dataset.dpanel === name));
  $('dbPanelGrid').hidden = name !== 'grid';
  $('dbPanelSql').hidden = name !== 'sql';
  $('dbPanelBackups').hidden = name !== 'backups';
  $('dbPanelBucket').hidden = name !== 'bucket';
  if (name === 'backups') dbBkLoad();
  if (name === 'bucket') dbBucketLoad();
}

/* ── bucket panel · read-only browser (item 3.4) ── */
async function dbBucketLoad(){
  const list = $('dbBucketList');
  if (!list) return;
  list.innerHTML = '<div class="db-empty">Loading…</div>';
  try{
    const d = await dbApi('/api/db/bucket?limit=200');
    if (!d.ok) throw new Error(d.error || 'bucket listing failed');
    if ($('dbBucketName')) $('dbBucketName').textContent =
      d.bucket ? 'bucket · ' + d.bucket : '';
    const objs = d.objects || [];
    if (!objs.length){ list.innerHTML = '<div class="db-empty">No objects in the bucket.</div>'; return; }
    list.innerHTML = objs.map(o => {
      const name = o.name || o.id || '';
      return `<button class="db-bucket-row" type="button" data-name="${dbEsc(name)}" title="${dbEsc(name)}">` +
        `<i class="fa fa-image" aria-hidden="true"></i>` +
        `<span class="mono">${dbEsc(name)}</span></button>`;
    }).join('');
    list.querySelectorAll('.db-bucket-row').forEach(b => {
      b.onclick = () => dbBucketOpen(b.dataset.name);
    });
  }catch(e){
    list.innerHTML = '<div class="db-empty db-err">' + dbEsc(e.message || e) + '</div>';
  }
}
function dbBucketOpen(name){
  if (!name) return;
  const drawer = $('bucketDrawer');
  if (!drawer) return;
  $('bucketDrawerName').textContent = name;
  $('bucketDrawerImg').src = mediaUrl(name);
  const src = $('bucketDrawerSrc');
  const links = $('bucketDrawerLinks');
  src.hidden = true; src.textContent = '';
  links.innerHTML = '<div class="db-empty">Loading…</div>';
  drawer.hidden = false;
  dbApi('/api/db/source?source_id=' + encodeURIComponent(name)).then(d => {
    const s = d.source;
    const folder = s && s.folder_id;
    $('bucketDrawerName').textContent = name + (folder ? ' · ' + folder : '');
    if (s && s.storage_url){
      src.href = s.storage_url; src.textContent = s.storage_url; src.hidden = false;
    } else {
      src.href = '#'; src.textContent = 'no source row in the database';
      src.hidden = false;
    }
    const recs = (d.questions || []).map(q =>
        { return { tag:'question', cls:'q', num:q.question_number, text:q.question_text }; })
      .concat((d.answers || []).map(a =>
        { return { tag:'answer', cls:'a', num:a.question_number, text:a.answer_explanation }; }));
    links.innerHTML = recs.length
      ? recs.map(r =>
          `<div class="bucket-link ${r.cls}">` +
            `<span class="tag">${r.tag}</span>` +
            `<span class="txt">#${dbEsc(r.num ?? '—')} · ${dbEsc(String(r.text || '').slice(0, 240))}</span>` +
          `</div>`).join('')
      : '<div class="db-empty">No questions or answers linked to this source.</div>';
  }).catch(e => {
    links.innerHTML = '<div class="db-empty db-err">' + dbEsc(e.message || e) + '</div>';
  });
}
$('bucketDrawerClose').onclick = () => { $('bucketDrawer').hidden = true; };

function dbWireOnce(){
  if (DbState.wireOnce) return;
  DbState.wireOnce = true;

  let deb = null;
  $('dbSearch').addEventListener('input', e => {
    clearTimeout(deb);
    deb = setTimeout(() => {
      DbState.q = e.target.value.trim(); DbState.offset = 0; dbLoadRows();
    }, 380);
  });
  $('dbLimit').onchange = e => {
    DbState.limit = +e.target.value; DbState.offset = 0; dbLoadRows();
    const m = DbState.byName[DbState.table];
    if (m) $('dbCrumb').textContent = m.rows.toLocaleString() + ' rows · '
      + m.columns.length + ' columns · showing latest ' + Math.min(DbState.limit, m.rows);
  };
  $('dbRefresh').onclick  = () => dbRefreshAll();
  $('dbExportBtn').onclick = () => {
    if (!DbState.rows.length){ toast('Nothing to export'); return; }
    dbToCsv(DbState.cols.map(c => c.name), DbState.rows);
  };
  $('dbAddBtn').onclick = () => dbOpenModal('add', null);

  $('dbPanels').querySelectorAll('button').forEach(b =>
    b.onclick = () => dbShowPanel(b.dataset.dpanel));

  $('sqlRunBtn').onclick = sqlRun;
  $('sqlInput').addEventListener('keydown', e => {
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter'){ e.preventDefault(); sqlRun(); }
  });
  const snip = $('sqlSnippets');
  SQL_SNIPPETS.forEach((s, i) => {
    const o = document.createElement('option');
    o.value = i; o.textContent = s[0]; snip.appendChild(o);
  });
  snip.onchange = () => {
    if (snip.value === '') return;
    $('sqlInput').value = SQL_SNIPPETS[+snip.value][1];
    snip.value = '';
    $('sqlInput').focus();
  };

  $('dbBackupNow').onclick = dbBackupNow;
  $('dbBucketReload').onclick = dbBucketLoad;

  $('dbModalClose').onclick = dbCloseModal;
  $('dbModalSave').onclick = dbSaveModal;
  $('dbModal').addEventListener('click', e => { if (e.target === $('dbModal')) dbCloseModal(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('dbModal').hidden) dbCloseModal();
  });
}

/* ═══════════════════════════════════════════════════════════════════
   WORKSTREAMS A–D · the surfaces added by the 2026-10-06 task
   credentials rail (Q3) · model rings · usage reports (Q4/Q11)
   upload queue (Q9) · period stats (Q4/Q11) · proxy pop-up (criterion 9)
   ═══════════════════════════════════════════════════════════════ */

const NINEROUTER_URL = 'http://127.0.0.1:20128/v1';
const PERIODS = ['24h', '7d', '30d', 'all'];
const PERIOD_LABEL = { '24h':'24 hours', '7d':'7 days', '30d':'30 days', 'all':'all time' };

/* ── 1 · credentials rail: one press, one panel (Q3) ───────────── */
const CRED_SERVICES = ['ocr', 'revision', 'database', 'proxy'];

function credShow(service){
  CRED_SERVICES.forEach(s => {
    const panel = document.querySelector('.cred-card[data-cred="' + s + '"]');
    if (panel) panel.hidden = (s !== service);
    const btn = document.querySelector('#credRail .rail-btn[data-cred="' + s + '"]');
    if (btn) btn.classList.toggle('active', s === service);
  });
}
document.querySelectorAll('#credRail .rail-btn[data-cred]').forEach(btn => {
  btn.onclick = () => credShow(btn.dataset.cred);
});

/* ── 2 · 9router model helpers (the section summary reads these) ── */
function rtProvider(){
  return ((CredState.extraction && CredState.extraction.providers) || [])
    .find(p => p.label === '9router')
    || ((CredState.revision && CredState.revision.providers) || [])
         .find(p => p.label === '9router')
    || null;
}
function rtModels(){
  const p = rtProvider();
  if (!p) return [];
  const list = (p.models && p.models.length) ? p.models.slice() : [];
  if (p.model && !list.includes(p.model)) list.push(p.model);
  return list;
}
function rtActiveModel(){
  const p = rtProvider();
  return (p && p.model) || ACTIVE_MODEL.router || '';
}

/* live model from the per-call usage event pushed on every send (criterion 10) */
const ACTIVE_MODEL = { gemini: '', router: '' };
function credUsageLive(ev){
  if (!ev || !ev.model) return;
  ACTIVE_MODEL.gemini = ev.model;
  ACTIVE_MODEL.router = ev.model;
  /* a studio/pipeline call landed — the tab re-reads the history on its next
     visit, so a run made while another tab was open is never invisible */
  UsageState.dirty = true;
  if (!$('viewUsage').classList.contains('active')) return;
  usageRefresh();
}

/* ── 3 · usage tab: a plot + per-call table (criterion 6) ── */
const UsageState = { period: '24h', group: 'model', ymode: 'calls',
                     data: null, busy: false, loaded: false };
/* Prompts view state — declared here (before the deep-link boot) so
   showView('prompts') can read it on a #prompts URL. */
const PromptState = { loaded: false, rows: [], busy: false };

async function usageRefresh(after){
  if (UsageState.busy) return;
  UsageState.busy = true;
  try{
    const res = await fetch('/api/usage?period=' + encodeURIComponent(UsageState.period) +
      '&group=' + encodeURIComponent(UsageState.group) +
      '&y=' + encodeURIComponent(UsageState.ymode));
    UsageState.data = await res.json();
    UsageState.dirty = false;
  }catch(e){ UsageState.data = null; }
  UsageState.busy = false;
  usageRender(UsageState.data);
  if (typeof after === 'function') after();
}

/* the table's Time column follows the chart's day buckets: both are the
   RPD window (Pacific), not UTC (item 2, Q3). `sv-SE` formats ISO-like. */
function ptStamp(ts){
  if (!ts) return '';
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'America/Los_Angeles', hour12: false,
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  }).format(new Date(ts * 1000));
}

function usageRender(data){
  const d = data || UsageState.data;
  const table = $('usageTable');
  const plot = $('usagePlot');
  if (!d || !d.ok){
    if (plot) plot.innerHTML = '';
    if (table) table.innerHTML = '';
    const split = $('usageSplit');
    if (split){ split.hidden = true; split.innerHTML = ''; }
    usageWire();
    return;
  }
  const rows = (d.calls || []).slice(-40).reverse();
  const fmtSecs = ms => ((Number(ms) || 0) / 1000).toLocaleString(undefined, {maximumFractionDigits: 2});
  usagePlot(d.chart);
  const body = rows.length ? rows.map(r => `<tr class="${r.ok ? '' : 'bad'}">
      <td>${credEsc(ptStamp(r.ts))}</td>
      <td>${credEsc(r.source || 'pipeline')}</td>
      <td class="mono">${credEsc(r.model || '—')}</td>
      <td>${credEsc(r.key_name || '—')}</td>
      <td class="mono">${credEsc(r.key_masked || '—')}</td>
      <td class="num">${(r.tokens_in || 0).toLocaleString()} / ${(r.tokens_out || 0).toLocaleString()}</td>
      <td class="num">${fmtSecs(r.ms)}</td>
    </tr>`).join('')
    : '<tr><td colspan="7" class="hint">No calls recorded in this period.</td></tr>';
  /* per-surface tally (pipeline vs studio) — the answer to "did the studio
     run show up in the usage data?" without reading the table row by row */
  const splitBox = $('usageSplit');
  const bySource = (d.totals && d.totals.by_source) || {};
  const sources = Object.keys(bySource).sort();
  if (splitBox){
    splitBox.hidden = !sources.length;
    splitBox.innerHTML = sources.map(src =>
      '<span class="usage-legend-item"><b>' + credEsc(src) + '</b>' +
      dbEsc(Number(bySource[src].calls || 0).toLocaleString()) + ' calls · ' +
      dbEsc(Number(bySource[src].tokens || 0).toLocaleString()) + ' tokens</span>').join('');
  }
  if (table) table.innerHTML =
    `<thead><tr><th>Time (PT)</th><th>Source</th><th>Model</th><th>Key name</th><th>Key</th>
      <th class="num">Tokens in / out</th><th class="num">Delay (seconds)</th></tr></thead><tbody>${body}</tbody>`;
  usageWire();
}

/* grouped bars: one bar-group per day, one colour per series (no chart lib).
   Bars keep their rect identity so the CSS transition on the SVG geometry
   properties (y/height) animates them on filter change (item 11). */
let usageLast = {max: 0, cells: null};          // module-level, beside usagePlot
/* round a data max up to a readable axis level (59 → 100, 470922 → 500000) so
   the Y gridlines read as round numbers, not the exact data value (item 4.5). */
function niceMax(v){
  if (!(v > 0)) return 1;
  const pow = Math.pow(10, Math.floor(Math.log10(v)));
  for (const m of [1, 2, 2.5, 5, 10]) if (m * pow >= v) return m * pow;
  return 10 * pow;
}
/* compact an axis label so a big token count fits the gutter (rework item 6):
   470922 → "471k", 1.2e6 → "1.2M" — the hover tooltip keeps the exact value. */
function fmtAxis(v){
  if (v >= 1e6) return (v / 1e6).toFixed(v % 1e6 ? 1 : 0) + 'M';
  if (v >= 1e3) return Math.round(v / 1e3) + 'k';
  return String(Math.round(v));
}
function usagePlot(chart){
  const box = $('usagePlot');
  if (!box) return;
  const days = (chart && chart.days) || [];
  const series = (chart && chart.series) || [];
  const cells = (chart && chart.cells) || [];
  if (!days.length || !series.length){
    box.innerHTML = '<p class="hint">No calls in this period.</p>';
    usageLast = {max: 0, cells: null};
    return;
  }
  const W = 760, H = 220, PAD = 40, PADL = 64, GAP = 10;   // PADL: room for 470,922
  const plotW = W - PAD - PADL, plotH = H - PAD * 2;
  let max = 0;
  cells.forEach(row => row.forEach(v => { if (v > max) max = v; }));
  const axisMax = niceMax(max);                     // round Y levels (item 4.5)
  const ticks = 4;                                  // adaptive Y: 0 … axisMax
  let grid = '';
  for (let t = 0; t <= ticks; t++){
    const y = H - PAD - (plotH * t / ticks);
    grid += `<line class="usage-grid" x1="${PADL}" y1="${y.toFixed(1)}"` +
            ` x2="${W - PAD}" y2="${y.toFixed(1)}"/>` +
            `<text class="usage-tick" x="${PADL - 8}" y="${(y + 4).toFixed(1)}"` +
            ` text-anchor="end">${fmtAxis(axisMax * t / ticks)}</text>`;
  }
  const groupW = plotW / days.length;
  const barW = Math.max(3, (groupW - GAP - 2) / series.length);
  const labelEvery = Math.max(1, Math.ceil(days.length / 7));
  let bars = '', labels = '';
  days.forEach((day, di) => {
    const x0 = PADL + di * groupW + (groupW - barW * series.length) / 2;
    series.forEach((s, si) => {
      const v = cells[di][si] || 0;
      const h = (v / axisMax) * plotH;
      const prev = usageLast.cells && usageLast.cells[di]
        ? (usageLast.cells[di][si] || 0) / (usageLast.max || 1) * plotH : h;
      bars += `<rect class="s${si % 6}" x="${(x0 + si * barW).toFixed(1)}" ` +
              `y="${(H - PAD - prev).toFixed(1)}" height="${prev.toFixed(1)}" ` +
              `width="${(barW - 2).toFixed(1)}" rx="2" ` +
              `data-day="${credEsc(day)}" data-series="${credEsc(s)}" ` +
              `data-value="${v}"></rect>`;
    });
    if (di % labelEvery === 0){
      labels += `<text class="usage-tick" x="${(PADL + di * groupW + groupW / 2).toFixed(1)}"` +
                ` y="${H - PAD + 16}" text-anchor="middle">${day.slice(5)}</text>`;
    }
  });
  const unit = chart.y === 'tokens' ? 'tokens'
             : (chart.y === 'sec' || chart.y === 'avg_sec') ? 's' : 'calls';
  const legend = series.map((s, i) =>
    `<span class="usage-legend-item"><i class="usage-swatch s${i % 6}"></i>` +
    `${credEsc(s)}</span>`).join('');
  box.innerHTML =
    `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="usage per day">` +
    grid +
    `<line class="usage-axis" x1="${PADL}" y1="${H - PAD}" x2="${W - PAD}" y2="${H - PAD}"/>` +
    `<text class="usage-tick" x="${PADL}" y="${PAD - 12}">${unit}/day</text>` +
    bars + labels + `</svg>` +
    `<div class="usage-legend">${legend}</div>` +
    `<div class="usage-tip" id="usageTip" hidden></div>`;
  /* next frame: animate every bar from the previous height to the new one */
  requestAnimationFrame(() => {
    box.querySelectorAll('rect[data-day]').forEach(r => {
      const v = +r.dataset.value;
      const h = (v / axisMax) * plotH;
      r.setAttribute('height', h.toFixed(1));
      r.setAttribute('y', (H - PAD - h).toFixed(1));
    });
  });
  usageLast = {max: axisMax, cells: cells};
  usageHover(box, unit);
}
/* hover values — a delegated listener, so it survives the box.innerHTML rewrite */
function usageHover(box, unit){
  const tip = box.querySelector('#usageTip');
  if (!tip) return;
  box.onmouseover = e => {
    const r = e.target.closest('rect[data-day]');
    if (!r) return;
    tip.hidden = false;
    tip.textContent = r.dataset.day + ' · ' + r.dataset.series + ' · ' +
      (+r.dataset.value).toLocaleString() + ' ' + unit;
    const b = r.getBoundingClientRect(), p = box.getBoundingClientRect();
    tip.style.left = (b.left - p.left + b.width / 2) + 'px';
    tip.style.top = (b.top - p.top) + 'px';
    tip.classList.add('on');
  };
  box.onmouseout = e => {
    if (e.target.closest('rect[data-day]')){ tip.classList.remove('on'); tip.hidden = true; }
  };
}
function usageWire(){
  const per = $('usagePeriod');
  if (per){
    per.value = UsageState.period;
    per.onchange = () => { UsageState.period = per.value; usageRefresh(); };
  }
  const group = $('usageGroup');
  if (group){
    group.value = UsageState.group;
    group.onchange = () => { UsageState.group = group.value; usageRefresh(); };
  }
  const ym = $('usageYMode');
  if (ym){
    ym.value = UsageState.ymode;
    ym.onchange = () => { UsageState.ymode = ym.value; usageRefresh(); };
  }
}

/* the 9router panel's own summary line */
function credRouterSummary(){
  const el = $('routerSummary');
  if (!el) return;
  const p = rtProvider();
  credSummary(el, [
    ['Base URL', NINEROUTER_URL],
    ['API key', p && p.api_key_set ? (p.api_key_masked || '— not set —') : '— not set —'],
    ['Active model', rtActiveModel() || '—'],
    ['Model list', rtModels().join(' · ') || '— none yet —'],
    ['Used by', (CredState.extraction && CredState.extraction.active === '9router' ? 'Extraction' : '') +
                (CredState.extraction && CredState.extraction.active === '9router' &&
                 CredState.revision && CredState.revision.active === '9router' ? ' + ' : '') +
                (CredState.revision && CredState.revision.active === '9router' ? 'Revision' : '') ||
                'nothing right now'],
  ]);
}

/* ── 4 · upload queue (workstream B · Q9) ──────────────────────── */
const UpState = { items: [] };
const UP_FINAL = ['done', 'failed', 'cancelled'];

async function upLoad(){
  try{
    const d = await (await fetch('/api/uploads')).json();
    UpState.items = d.items || [];
  }catch(e){ return; }
  upRender();
}
function upApplyEvent(){ upLoad(); }   // the server owns the queue state

/* the destination folders are picked, never typed — the three subjects the app
   ships (shimi / zist / physic) x grade x chapter, straight from the watched tree */
const UP_TREE = {
  shimi:  { 10: 3, 11: 3, 12: 4 },
  zist:   { 10: 6, 11: 9, 12: 8 },
  physic: { 10: 4, 11: 3, 12: 4 },
};
const upDds = {};
(function wireUploadDds(){
  upDds.subject = createDropdown('upSubject', 'subject');
  upDds.grade   = createDropdown('upGrade', 'grade');
  upDds.topic   = createDropdown('upTopic', 'chapter');
  upDds.type    = createDropdown('upType', 'type');
  upDds.subject.setOptions(Object.keys(UP_TREE).map(s => [s, s]));
  upDds.grade.setOptions([]);
  upDds.topic.setOptions([]);
  upDds.type.setOptions([['question', 'question'], ['answer', 'answer']], 'question');
  upDds.subject.onChange = s => {
    upDds.grade.setOptions(Object.keys(UP_TREE[s] || {}).map(g => [g, g]));
    upDds.topic.setOptions([]);
  };
  upDds.grade.onChange = g => {
    const n = (UP_TREE[upDds.subject.value] || {})[g] || 0;
    upDds.topic.setOptions(Array.from({ length: n }, (_, i) => {
      const c = 'chapter' + (i + 1); return [c, c];
    }));
  };
})();

function upRender(){
  const list = $('upList');
  if (!list) return;
  // a finished file leaves the queue (item 2.3); failed/cancelled stay actionable
  const items = UpState.items.filter(it => (it.status || 'queued') !== 'done');
  const empty = $('upEmpty');
  if (empty) empty.hidden = items.length > 0;   // picker is an empty-state
  list.hidden = !items.length;
  if (!items.length){
    list.innerHTML = '';
    return;
  }
  list.innerHTML = items.map(it => {
    const st = it.status || 'queued';
    const canCancel = !UP_FINAL.includes(st) || st === 'failed' || st === 'cancelled';
    return `<div class="up-row" data-id="${credEsc(it.id)}">
      <i class="fa ${st === 'done' ? 'fa-circle-check ok' : st === 'failed' ? 'fa-circle-xmark bad'
          : st === 'extracting' ? 'fa-gears spin' : 'fa-hourglass-half'}" aria-hidden="true"></i>
      <div class="up-meta">
        <b>${credEsc(it.filename || '')}</b>
        <span class="hint">${credEsc(it.dest_rel || '')}${it.detail ? ' · ' + credEsc(it.detail) : ''}</span>
      </div>
      <span class="up-status" data-st="${credEsc(st)}">${credEsc(st)}</span>
      ${canCancel ? `<button class="ghost-btn neutral sm up-cancel" type="button" data-id="${credEsc(it.id)}"
        ${st === 'cancelled' ? 'disabled' : ''}>${st === 'failed' || st === 'cancelled' ? 'Remove' : 'Cancel'}</button>` : ''}
    </div>`;
  }).join('');
  list.querySelectorAll('button.up-cancel').forEach(b => {
    b.onclick = async () => {
      const res = await fetch('/api/uploads/' + encodeURIComponent(b.dataset.id) + '/cancel',
                              { method: 'POST' });
      const d = await res.json().catch(() => ({}));
      if (!d.ok) toast(d.error || 'Cannot cancel that upload');
      await upLoad();
    };
  });
}

$('upRefresh').onclick = upLoad;

async function upQueue(){
  const files = Array.from($('upFiles').files || []);
  if (!files.length){ toast('Pick at least one PDF first'); return; }
  const s = upDds.subject.value, g = upDds.grade.value, t = upDds.topic.value;
  if (!s || !g || !t){ toast('Subject, grade and chapter are all required'); return; }
  const dest = [s, g, t].join('/') + '/' + upDds.type.value;
  let ok = 0;
  for (const f of files){                    // one at a time — the server queue is sequential
    const fd = new FormData();
    fd.append('file', f);
    fd.append('dest', dest);
    try{
      const res = await fetch('/api/uploads', { method: 'POST', body: fd });
      const d = await res.json().catch(() => ({}));
      if (d.ok) ok++;
      else toast(f.name + ': ' + (d.error || 'upload failed'));
    }catch(e){ toast(f.name + ': ' + e); }
  }
  $('upFiles').value = '';
  $('upPickText').textContent = 'Add PDF files…';
  if (ok) toast(ok + ' file(s) queued for ' + dest);
  await upLoad();
}

/* choosing a file enqueues it straight away — selection is the add action */
$('upFiles').onchange = () => { if ($('upFiles').files.length) upQueue(); };
/* ADD opens the picker (item 2.2) — the enqueue happens on selection */
$('upAdd').onclick = () => $('upFiles').click();

/* ── 5 · period stats — all five cards are DB-sourced (item 2.1) ── */
const StatsState = { period: '24h' };

function statsPaint(){
  // no caption any more (rework item 14) — the chips + cards carry the state
}

async function statsLoad(period){
  StatsState.period = period || '24h';
  document.querySelectorAll('.ps-chip[data-period]').forEach(c =>
    c.classList.toggle('active', c.dataset.period === StatsState.period));
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('psError', ''); if ($('psError')) $('psError').hidden = true;
  try{
    const d = await (await fetch('/api/stats?period=' + encodeURIComponent(StatsState.period))).json();
    if (!d.ok) throw new Error(d.error || 'request failed');
    set('psTotal',     (d.total || 0).toLocaleString());
    set('psQFiles',    (d.question_files || 0).toLocaleString());
    set('psAFiles',    (d.answer_files || 0).toLocaleString());
    set('psQuestions', (d.questions || 0).toLocaleString());
    set('psAnswers',   (d.answers || 0).toLocaleString());
    statsPaint();                       // the caption only
  }catch(e){
    const el = $('psError');
    if (el){ el.hidden = false; el.textContent = String(e.message || e); }
  }
}
document.querySelectorAll('.ps-chip[data-period]').forEach(c => {
  c.onclick = () => statsLoad(c.dataset.period);
});

/* ── 6 · wire the new surfaces once the DOM is up ──────────────── */
function bootNewSurfaces(){
  if (!ProvState.loaded) loadPipeProviders();      // 9router shows up in the selector (bug d)
  upLoad();
  statsLoad('24h');
  if (CredState.loaded) credRouterSummary();
  pipeModePaint();   // C3-1: the restored PipeRun.mode drives BOTH the select and
                     // the #stages/#lanes surface, not just the select on refresh.
}
bootNewSurfaces();

/* Honour a #view deep link after every script (incl. studio.js) has loaded —
   so a headless screenshot or a shared URL lands on the right tab. Deferred
   to DOMContentLoaded because showView('studio') needs StudioState, which is
   defined by studio.js loaded after this file. */
function bootDeepLink(){ const v = viewFromHash(); if (v) showView(v); }
if (document.readyState === 'loading')
  document.addEventListener('DOMContentLoaded', bootDeepLink);
else
  bootDeepLink();

/* keep the 9router section honest whenever credentials reload */
const _loadCredentials = loadCredentials;
loadCredentials = async function(){
  await _loadCredentials.apply(this, arguments);
  credRouterSummary();
};

/* Q2 — the built-in 9router is the only non-Gemini provider: the label and
   base URL are fixed, only the key and the model list stay editable. */
(function ninerouterForm(){
  const pin = () => {
    ['ocr', 'cred'].forEach(pre => {
      const u = $(pre + 'RouterUrl'), l = $(pre + 'Label');
      if (u){ u.value = NINEROUTER_URL; u.readOnly = true;
              u.placeholder = NINEROUTER_URL; }
      if (l && !l.value) l.value = '9router';
    });
  };
  ['ocrEdit', 'credEdit'].forEach(id => {
    const btn = $(id);
    if (!btn) return;
    const orig = btn.onclick;
    btn.onclick = () => { if (orig) orig(); pin(); };
  });
  const add = (section) => {
    credAddProvider(section);
    pin();
    toast('9router is built in — only the API key and model list can change.');
  };
  const oa = $('ocrAddProvider'), ca = $('credAddProvider');
  if (oa) oa.onclick = () => add('extraction');
  if (ca) ca.onclick = () => add('revision');
})();

/* ── Prompts view: every LLM instruction, editable ──
   The backend keeps a default per prompt id (pipeline/prompts.py) and an
   override store beside .gemini-usage.json; an override applies to the very
   next model call. LOCAL first: nothing here touches the DB. */

async function promptsLoad(){
  try{
    const d = await (await fetch('/api/prompts')).json();
    PromptState.rows = (d && d.prompts) || [];
    PromptState.loaded = true;
  }catch(e){ PromptState.rows = []; }
  promptsRender();
}

function promptsRender(){
  const box = $('promptList');
  if (!box) return;
  const rows = PromptState.rows || [];
  const count = $('prCount');
  if (count) count.textContent = rows.filter(r => r.overridden).length + ' / ' + rows.length + ' edited';
  if (!rows.length){
    box.innerHTML = '<div class="db-empty db-err">Prompts unreachable — is the server running?</div>';
    return;
  }
  let group = '';
  box.innerHTML = rows.map(r => {
    let head = '';
    if (r.group !== group){
      group = r.group;
      head = '<h3 class="prompt-group">' + credEsc(group) + '</h3>';
    }
    return head + '<article class="prompt-card" data-id="' + credEsc(r.id) + '">'
      + '<header class="prompt-head">'
      +   '<b>' + credEsc(r.label) + '</b>'
      +   '<span class="status-chip ' + (r.overridden ? 'sc-pending' : 'sc-approved') + '">'
      +     (r.overridden ? 'edited' : 'default') + '</span>'
      +   '<span class="st-gap"></span>'
      +   '<button class="ghost-btn neutral sm" data-prompt-save type="button">Save</button>'
      +   '<button class="ghost-btn neutral sm" data-prompt-reset type="button">Reset</button>'
      + '</header>'
      + '<textarea class="prompt-text" rows="12" spellcheck="false" dir="ltr">'
      +   credEsc(r.text) + '</textarea>'
      + '</article>';
  }).join('');
  box.querySelectorAll('.prompt-card').forEach(card => {
    const id = card.dataset.id;
    const area = card.querySelector('.prompt-text');
    const save = card.querySelector('[data-prompt-save]');
    const reset = card.querySelector('[data-prompt-reset]');
    if (save) save.onclick = () => promptSave(id, area.value);
    if (reset) reset.onclick = () => promptReset(id);
  });
}

async function promptSave(id, text){
  try{
    const res = await fetch('/api/prompts', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id, text: text }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    PromptState.rows = d.prompts || PromptState.rows;
    promptsRender();
    toast('Prompt saved');
  }catch(e){ toast('Prompt save failed — ' + e.message); }
}

async function promptReset(id){
  try{
    const res = await fetch('/api/prompts/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: id }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    PromptState.rows = d.prompts || PromptState.rows;
    promptsRender();
    toast('Prompt reset to default');
  }catch(e){ toast('Reset failed — ' + e.message); }
}

async function promptsResetAll(){
  try{
    const res = await fetch('/api/prompts/reset', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({}) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    PromptState.rows = d.prompts || PromptState.rows;
    promptsRender();
    toast('Every prompt is back to its default');
  }catch(e){ toast('Reset failed — ' + e.message); }
}

$('prReload').onclick = () => promptsLoad();
$('prResetAll').onclick = () => promptsResetAll();

/* ═══════════ ASSISTANT · the floating help chat ═══════════
   One bubble bottom-right opens a small chat panel. The endpoint/key/model are
   the assistant's OWN (.env ASSISTANT_*), never the OCR lanes. Replies stream
   in over /api/assistant/chat as our own SSE: "thinking" and "text" events,
   rendered as Markdown (tables, lists, code, $$math$$, mermaid diagrams).
   The composer has a Markdown preview of what the user is typing. */
const AsstState = { open: false, cfg: null, messages: [], busy: false,
                    preview: false, mermaid: 'idle' };

const asstEsc = s => String(s == null ? '' : s).replace(/[&<>"']/g,
  c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

/* inline markdown over ALREADY-ESCAPED text (so it can never inject HTML) */
function asstInline(s){
  return s
    .replace(/`([^`\n]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/(^|[\s(])\*([^*\n]+)\*/g, '$1<em>$2</em>')
    .replace(/\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
             '<a href="$2" target="_blank" rel="noopener">$1</a>');
}

/* line-based markdown renderer: fenced code (mermaid included), headings,
   lists, tables, blockquotes, rules, paragraphs. Escapes first. */
function asstMd(raw){
  const lines = String(raw == null ? '' : raw).replace(/\r\n?/g, '\n').split('\n');
  const out = [];
  let i = 0, list = null, para = [];
  const flushPara = () => {
    if (para.length){ out.push('<p>' + para.map(asstInline).join('<br>') + '</p>'); para = []; }
  };
  const closeList = () => { if (list){ out.push('</' + list + '>'); list = null; } };
  while (i < lines.length){
    const line = lines[i];
    const fence = line.match(/^\s*```([\w-]*)\s*$/);
    if (fence){
      flushPara(); closeList();
      const lang = (fence[1] || '').toLowerCase();
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```\s*$/.test(lines[i])) body.push(lines[i++]);
      i++;
      const text = body.join('\n');
      if (lang === 'mermaid')
        out.push('<div class="asst-mermaid" data-src="' + asstEsc(text) + '"></div>');
      else
        out.push('<pre class="asst-code"><code>' + asstEsc(text) + '</code></pre>');
      continue;
    }
    if (/^\s*$/.test(line)){ flushPara(); closeList(); i++; continue; }
    const h = line.match(/^(#{1,4})\s+(.*)$/);
    if (h){
      flushPara(); closeList();
      const n = h[1].length;
      out.push('<h' + n + '>' + asstInline(asstEsc(h[2])) + '</h' + n + '>');
      i++; continue;
    }
    if (/^\s*([-*+]|\d+[.)])\s+/.test(line)){
      flushPara();
      const ordered = /^\s*\d+[.)]\s+/.test(line);
      const want = ordered ? 'ol' : 'ul';
      if (list !== want){ closeList(); out.push('<' + want + '>'); list = want; }
      out.push('<li>' + asstInline(asstEsc(
        line.replace(/^\s*([-*+]|\d+[.)])\s+/, ''))) + '</li>');
      i++; continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1] || '')){
      flushPara(); closeList();
      const rows = [];
      const cells = l => l.trim().replace(/^\||\|$/g, '').split('|')
        .map(c => '<td>' + asstInline(asstEsc(c.trim())) + '</td>').join('');
      rows.push('<tr>' + cells(line) + '</tr>');
      i += 2;
      for (; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++)
        rows.push('<tr>' + cells(lines[i]) + '</tr>');
      out.push('<table class="md-table"><tbody>' + rows.join('') + '</tbody></table>');
      continue;
    }
    if (/^\s*>\s?/.test(line)){
      flushPara(); closeList();
      out.push('<blockquote>' + asstInline(asstEsc(line.replace(/^\s*>\s?/, ''))) + '</blockquote>');
      i++; continue;
    }
    if (/^\s*(---|\*\*\*)\s*$/.test(line)){
      flushPara(); closeList(); out.push('<hr>'); i++; continue;
    }
    para.push(asstEsc(line));
    i++;
  }
  flushPara(); closeList();
  return out.join('');
}

/* mermaid is fetched on first use (like MathJax, a CDN script, no build step);
   a failed load leaves the diagram source as a plain code block. */
function asstMermaid(scope){
  const blocks = scope.querySelectorAll('.asst-mermaid[data-src]');
  if (!blocks.length) return;
  const run = () => blocks.forEach(b => {
    if (b.dataset.done) return;
    b.dataset.done = '1';
    const src = b.dataset.src;
    const fallback = () => {
      b.innerHTML = '<pre class="asst-code"><code>' + asstEsc(src) + '</code></pre>';
    };
    if (!window.mermaid){ fallback(); return; }
    try{
      window.mermaid.render('asstm' + Math.random().toString(36).slice(2), src)
        .then(res => { b.innerHTML = res && res.svg ? res.svg : ''; })
        .catch(fallback);
    }catch(e){ fallback(); }
  });
  if (window.mermaid){ run(); return; }
  AsstState.pendingMermaid = run;
  if (AsstState.mermaid !== 'idle') return;
  AsstState.mermaid = 'loading';
  const s = document.createElement('script');
  s.src = 'https://cdn.jsdelivr.net/npm/mermaid@10.9.1/dist/mermaid.min.js';
  s.onload = () => {
    try{
      window.mermaid.initialize({ startOnLoad: false,
        theme: document.documentElement.dataset.theme === 'light' ? 'default' : 'dark' });
    }catch(e){ /* older mermaid: render still works */ }
    if (AsstState.pendingMermaid) AsstState.pendingMermaid();
  };
  s.onerror = () => {
    AsstState.mermaid = 'failed';
    if (AsstState.pendingMermaid) AsstState.pendingMermaid();
  };
  document.head.appendChild(s);
}

/* ── panel chrome ── */
function asstOpen(on){
  AsstState.open = on;
  $('asstPanel').hidden = !on;
  $('asstBubble').classList.toggle('on', on);
  if (on){
    if (!AsstState.cfg) asstCfgLoad();
    setTimeout(() => { const t = $('asstIn'); if (t) t.focus(); }, 30);
    asstScroll();
  }
}

function asstScroll(){
  const box = $('asstMsgs');
  if (box) box.scrollTop = box.scrollHeight;
}

async function asstCfgLoad(){
  try{
    const d = await (await fetch('/api/assistant/config')).json();
    AsstState.cfg = d;
    asstPickFill(d);
    const chip = $('asstChip');
    chip.hidden = false;
    chip.textContent = d.configured ? (d.provider || '') : 'not configured';
    chip.title = d.configured ? (d.model || '') : '';
    chip.className = 'status-chip ' + (d.configured ? 'sc-approved' : 'sc-rejected');
  }catch(e){ /* the server may be starting */ }
}

/* the header picker lists every model of every provider ("label :: model") */
function asstPickFill(d){
  const sel = $('asstPick');
  if (!sel) return;
  const providers = (d && d.providers) || [];
  const opts = [];
  providers.forEach(p => (p.models || []).forEach(m =>
    opts.push([p.label + '::' + m, p.label + ' · ' + m])));
  sel.innerHTML = opts.length ? opts.map(([v, l]) =>
      '<option value="' + asstEsc(v) + '"' + (v === (d.model_id || '') ? ' selected' : '') + '>'
      + asstEsc(l) + '</option>').join('')
    : '<option value="">no model</option>';
  sel.hidden = opts.length === 0;
}

async function asstPick(id){
  try{
    const res = await fetch('/api/assistant/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: id }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    AsstState.cfg = d;
    await asstCfgLoad();
  }catch(e){ toast('Model switch failed — ' + e.message); }
}

/* ── settings pop-up · providers, their models, one active pick ── */
const AsstEdit = { providers: [], active: '', effort: '' };

function asstModalOpen(on){
  $('asstModal').hidden = !on;
  if (!on) return;
  const d = AsstState.cfg || {};
  AsstEdit.providers = JSON.parse(JSON.stringify(d.providers || []));
  AsstEdit.active = d.model_id || '';
  AsstEdit.effort = d.effort || '';
  asstModalRender();
}

/* the picker lists provider + model; it is a change-triggered save, so the
   current pick is shown even when the model is not in the list yet */
/* set a select to a value that its options already carry */
function asstModalSelect(root, sel, value){
  const el = root.querySelector(sel);
  if (!el) return;
  el.value = value == null ? '' : String(value);
  if (el.value !== String(value || '') && value) el.value = '';
}

function asstSourceOptions(providers, cur){
  const opts = [];
  providers.forEach(p => (p.models || []).forEach(m =>
    opts.push([p.label + '::' + m, p.label + ' · ' + m])));
  if (cur && !opts.some(([v]) => v === cur)) opts.unshift([cur, cur]);
  /* the pick is applied by asstModalSelect after the markup is in place: an
     escaped attribute would reach the parser as selected&quot; and be dropped */
  return '<option value="">—</option>' + opts.map(([v, l]) =>
    '<option value="' + asstEsc(v) + '">' + asstEsc(l) + '</option>').join('');
}

function asstModalRender(){
  const body = $('asstModalBody');
  const efforts = [['', 'provider default'], ['minimal', 'minimal'], ['low', 'low'],
                   ['medium', 'medium'], ['high', 'high']];
  const cards = AsstEdit.providers.map((p, i) =>
    '<div class="asst-prov" data-prov="' + i + '">'
    + '<div class="asst-prov-head"><b>' + asstEsc(p.label || '') + '</b>'
    + '<span class="st-art ' + (p.api_key_set ? 'on' : 'off') + '">'
    + (p.api_key_set ? 'key set' : 'no key') + '</span>'
    + '<span class="asst-gap"></span>'
    + '<button class="ghost-btn neutral sm" type="button" data-asst-fetch="' + i + '">list models</button>'
    + '<button class="ghost-btn neutral sm" type="button" data-asst-del="' + i + '">remove</button></div>'
    + '<label class="st-field"><span>base url</span>'
    + '<input type="text" dir="ltr" data-ap-url="' + i + '" value="' + asstEsc(p.base_url || '') + '"></label>'
    + '<label class="st-field"><span>api key</span>'
    + '<input type="password" dir="ltr" autocomplete="new-password" data-ap-key="' + i + '" value=""'
    + (p.api_key_set ? ' placeholder="' + asstEsc(p.api_key_masked || 'set') + '"' : '') + '></label>'
    + '<label class="st-field"><span>models · one per line</span>'
    + '<textarea dir="ltr" rows="1" data-autofit data-ap-models="' + i + '">'
    + asstEsc((p.models || []).join('\n')) + '</textarea></label></div>').join('');
  body.innerHTML =
    '<div class="asst-set-grid">'
    + '<div class="asst-set-col"><div class="asst-prov-list">' + cards + '</div>'
    + '<button class="ghost-btn neutral sm" type="button" data-asst-add>add provider</button></div>'
    + '<div class="asst-set-col">'
    + '<label class="st-field"><span>active model</span><select data-asst-active>'
    + asstSourceOptions(AsstEdit.providers, AsstEdit.active) + '</select></label>'
    + '<label class="st-field"><span>thinking level</span><select data-asst-effort>'
    + efforts.map(([v, l]) => '<option value="' + v + '">'
        + asstEsc(l) + '</option>').join('') + '</select></label>'
    + '<div class="asst-opt"><span class="st-art '
    + (AsstEdit.providers.some(p => p.api_key_set) ? 'on' : 'off') + '">'
    + (AsstEdit.providers.some(p => p.api_key_set) ? 'keys saved' : 'no key')
    + '</span></div></div></div>';
  asstModalSelect(body, '[data-asst-active]', AsstEdit.active);
  asstModalSelect(body, '[data-asst-effort]', AsstEdit.effort);
  if (typeof stAutofit === 'function') stAutofit(body);
  body.querySelectorAll('textarea[data-autofit]').forEach(t => {
    t.oninput = () => { t.style.height = 'auto'; t.style.height = (t.scrollHeight + 2) + 'px'; };
  });
  body.onclick = async e => {
    const b = e.target.closest('button');
    if (!b) return;
    if (b.hasAttribute('data-asst-add')){
      const n = AsstEdit.providers.length + 1;
      AsstEdit.providers.push({ label: 'provider' + n, base_url: '',
        models: [], api_key_set: false });
      asstModalRender();
    } else if (b.hasAttribute('data-asst-del')){
      AsstEdit.providers.splice(+b.dataset.asstDel, 1);
      asstModalRender();
    } else if (b.hasAttribute('data-asst-fetch')){
      await asstFetchModels(+b.dataset.asstFetch, b);
    }
  };
}

function asstModalRead(){
  const body = $('asstModalBody');
  AsstEdit.providers = AsstEdit.providers.map((p, i) => {
    const val = attr => {
      const el = body.querySelector('[' + attr + '="' + i + '"]');
      return el ? el.value : '';
    };
    return { label: p.label,
             base_url: val('data-ap-url').trim(),
             api_key: val('data-ap-key').trim() || '__KEEP__',
             models: val('data-ap-models').split('\n').map(s => s.trim()).filter(Boolean),
             api_key_set: p.api_key_set, api_key_masked: p.api_key_masked };
  });
  const active = body.querySelector('[data-asst-active]');
  const effort = body.querySelector('[data-asst-effort]');
  AsstEdit.active = active ? active.value : '';
  AsstEdit.effort = effort ? effort.value : '';
}

async function asstFetchModels(i, btn){
  asstModalRead();
  const p = AsstEdit.providers[i];
  if (!p) return;
  btn.disabled = true;
  const err = $('asstModalErr');
  try{
    // the saved provider answers first (the key is masked in this form);
    // an unsaved row is listed straight from the server only after a save
    const res = await fetch('/api/assistant/models', { method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: p.label }) });
    const d = await res.json();
    if (!d.ok && !(d.models || []).length){
      throw new Error(d.detail || d.error || ('HTTP ' + res.status));
    }
    const merged = Array.from(new Set((p.models || []).concat(d.models || [])));
    AsstEdit.providers[i].models = merged;
    asstModalRender();
  }catch(e){
    if (err){ err.hidden = false; err.textContent = e.message; }
  }finally{ btn.disabled = false; }
}

async function asstCfgSave(){
  const err = $('asstModalErr');
  if (err) err.hidden = true;
  asstModalRead();
  try{
    const res = await fetch('/api/assistant/config', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ providers: AsstEdit.providers,
                             active: AsstEdit.active, effort: AsstEdit.effort }) });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    AsstState.cfg = d;
    await asstCfgLoad();
    asstModalOpen(false);
    toast('Assistant saved');
  }catch(e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Assistant save failed — ' + e.message);
  }
}

/* one message node; returns the live handles so the stream can paint it */
function asstAppend(role, text){
  const box = $('asstMsgs');
  const el = document.createElement('article');
  el.className = 'asst-msg ' + role;
  const body = document.createElement('div');
  body.className = 'asst-body';
  const think = document.createElement('details');
  think.className = 'asst-think';
  think.hidden = true;
  const sum = document.createElement('summary');
  sum.innerHTML = '<i class="fa fa-brain" aria-hidden="true"></i><span>thinking</span>';
  const thinkBody = document.createElement('div');
  thinkBody.className = 'asst-think-body';
  think.append(sum, thinkBody);
  el.append(body);
  if (role === 'assistant') el.insertBefore(think, body);
  box.appendChild(el);
  const node = { el: el, body: body, think: think, thinkBody: thinkBody,
                 text: text || '', thinking: '', dirty: false, timer: null,
                 started: performance.now(), usage: null };
  node.paint = () => {
    node.dirty = false;
    node.body.innerHTML = asstMd(node.text);
    node.el.classList.toggle('streaming', !!node.streaming);
    if (node.thinking){
      node.think.hidden = false;
      node.thinkBody.textContent = node.thinking;
    }
    if (node.usage) asstUsageLine(node);
    asstMermaid(node.body);
    typesetMath(node.body);
    asstScroll();
  };
  node.paint();
  return node;
}

/* the per-reply stat line: thinking, tokens in/out, speed */
function asstUsageLine(node){
  const u = node.usage || {};
  const secs = Math.max(0.001, (performance.now() - node.started) / 1000);
  const tps = u.tokens_per_second != null ? u.tokens_per_second
    : Math.round(((u.completion_tokens || 0) || (node.text.length / 4)) / secs * 10) / 10;
  let line = node.el.querySelector('.asst-usage');
  if (!line){
    line = document.createElement('div');
    line.className = 'asst-usage';
    node.el.appendChild(line);
  }
  const bits = [];
  if (node.thinking) bits.push('<i class="fa fa-brain" aria-hidden="true"></i> '
    + (u.reasoning_tokens ? stNumLocal(u.reasoning_tokens) + ' reasoning' : 'thinking'));
  if (u.prompt_tokens != null) bits.push(stNumLocal(u.prompt_tokens) + ' in');
  if (u.completion_tokens != null) bits.push(stNumLocal(u.completion_tokens) + ' out');
  bits.push(tps.toFixed(1) + ' tok/s');
  bits.push(stNumLocal(Math.max(0, secs).toFixed(1)) + 's');
  line.innerHTML = bits.join(' · ');
}

const stNumLocal = v => String(v == null ? '' : v);

function asstTouch(node){
  /* repaint at most every 90 ms — a token-by-token innerHTML would thrash */
  if (node.timer) return;
  node.timer = setTimeout(() => { node.timer = null; node.paint(); }, 90);
}

async function asstSend(){
  const input = $('asstIn');
  const text = (input.value || '').trim();
  if (!text || AsstState.busy) return;
  input.value = '';
  asstPreviewPaint();
  AsstState.messages.push({ role: 'user', content: text });
  asstAppend('user', text);
  const node = asstAppend('assistant', '');
  node.streaming = true;
  node.paint();
  AsstState.busy = true;
  $('asstSend').disabled = true;
  const size = $('asstSize');
  if (size) size.hidden = true;
  try{
    const res = await fetch('/api/assistant/chat', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ messages: AsstState.messages }) });
    if (!res.ok || !res.body) throw new Error('HTTP ' + res.status);
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    for (;;){
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let cut;
      while ((cut = buf.indexOf('\n\n')) >= 0){
        const chunk = buf.slice(0, cut);
        buf = buf.slice(cut + 2);
        chunk.split('\n').forEach(line => {
          if (!line.startsWith('data:')) return;
          let ev = null;
          try{ ev = JSON.parse(line.slice(5).trim()); }catch(e){ return; }
          if (!ev) return;
          if (ev.type === 'thinking'){ node.thinking += ev.text || ''; node.think.open = true; }
          else if (ev.type === 'text'){ node.text += ev.text || ''; }
          else if (ev.type === 'usage'){ node.usage = ev; }
          else if (ev.type === 'error'){
            node.text += (node.text ? '\n\n' : '') + '**' + (ev.message || 'Assistant error') + '**';
          }
          asstTouch(node);
        });
      }
    }
  }catch(e){
    node.text += (node.text ? '\n\n' : '') + '**Assistant unreachable — ' + e.message + '**';
  }
  node.streaming = false;
  node.paint();
  if (node.text.trim()) AsstState.messages.push({ role: 'assistant', content: node.text });
  AsstState.busy = false;
  $('asstSend').disabled = false;
  asstScroll();
}

/* ── the built-in Markdown preview: the message renders as you type ── */
function asstPreviewPaint(){
  const box = $('asstPreview');
  const area = $('asstIn');
  if (!box || !area) return;
  const text = area.value || '';
  box.classList.toggle('empty', !text.trim());
  if (!text.trim()){ box.innerHTML = ''; return; }
  box.innerHTML = asstMd(text);
  asstMermaid(box);
  typesetMath(box);
}

/* the composer grows with its text, up to a scroll-free cap */
function asstGrow(){
  const area = $('asstIn');
  if (!area) return;
  area.style.height = 'auto';
  const h = Math.min(area.scrollHeight + 2, 180);
  area.style.height = h + 'px';
  area.style.overflowY = area.scrollHeight + 2 > 180 ? 'auto' : 'hidden';
  asstPreviewPaint();
}

$('asstBubble').onclick = () => asstOpen(!AsstState.open);
$('asstClose').onclick = () => asstOpen(false);
$('asstGear').onclick = () => asstModalOpen($('asstModal').hidden);
$('asstModalClose').onclick = () => asstModalOpen(false);
$('asstModal').onclick = e => { if (e.target === $('asstModal')) asstModalOpen(false); };
$('asstSave').onclick = () => asstCfgSave();
$('asstPick').onchange = e => asstPick(e.target.value);
$('asstForm').onsubmit = e => { e.preventDefault(); asstSend(); };
$('asstIn').addEventListener('keydown', e => {
  if (e.key === 'Enter' && !e.shiftKey){ e.preventDefault(); asstSend(); }
});
$('asstIn').addEventListener('input', () => asstGrow());
