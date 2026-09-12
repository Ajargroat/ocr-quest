/* ═══════════ shared helpers ═══════════ */
const $ = id => document.getElementById(id);
const FA_DIGITS = '۰۱۲۳۴۵۶۷۸۹';
const faDigits = v => String(v ?? '').replace(/\d/g, d => FA_DIGITS[d]);
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

/* ── inline icon set · Lucide-style standard glyphs (ISC), stroke-based, no text labels ──
   Buttons carry only these glyphs; `title` supplies the name on hover. */
const _sv = p => '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round">' + p + '</svg>';
const IC = {
  play:   _sv('<path d="M6 3l14 9-14 9V3Z"/>'),
  stop:   _sv('<rect width="14" height="14" x="5" y="5" rx="2"/>'),
  seal:   _sv('<path d="M3.85 8.62a4 4 0 0 1 4.78-4.77 4 4 0 0 1 6.74 0 4 4 0 0 1 4.78 4.78 4 4 0 0 1 0 6.74 4 4 0 0 1-4.77 4.78 4 4 0 0 1-6.75 0 4 4 0 0 1-4.78-4.77 4 4 0 0 1 0-6.76Z"/><path d="m9 12 2 2 4-4"/>'),
  xspark: _sv('<path d="M18 6 6 18"/><path d="m6 6 12 12"/>'),
  reopen: _sv('<path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/>'),
  ghost:  _sv('<path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/><path d="M10 11v6"/><path d="M14 11v6"/>'),
  warn:   _sv('<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3"/><path d="M12 9v4"/><path d="M12 17h.01"/>'),
  pencil: _sv('<path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/><path d="M18.375 2.625a2.121 2.121 0 1 1 3 3L12 15l-4 1 1-4Z"/>'),
  dup:    _sv('<rect width="14" height="14" x="8" y="8" rx="2"/><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>'),
  undo:   _sv('<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>'),
  chevL:  _sv('<path d="m15 18-6-6 6-6"/>'),
  chevR:  _sv('<path d="m9 18 6-6-6-6"/>'),
  scroll: _sv('<path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z"/><path d="M14 2v4a2 2 0 0 0 2 2h4"/><path d="M16 13H8"/><path d="M16 17H8"/><path d="M10 9H8"/>'),
  backup: _sv('<path d="M22 12H2"/><path d="M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z"/><path d="M6 16h.01"/><path d="M10 16h.01"/>'),
};
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
function setConn(ok){
  $('conn').className = 'conn ' + (ok ? 'on' : 'off');
  $('connText').textContent = ok ? 'live' : 'reconnecting…';
}
function tween(el, to){
  const from = parseInt(el.dataset.v || '0');
  if (from === to){ el.textContent = to; el.dataset.v = to; return; }
  const t0 = performance.now(), dur = 500;
  (function step(t){
    const p = Math.min(1, (t - t0) / dur);
    el.textContent = Math.round(from + (to - from) * (1 - Math.pow(1 - p, 3)));
    if (p < 1) requestAnimationFrame(step); else el.dataset.v = to;
  })(t0);
}
function applyStats(s){
  tween($('stTotal'), s.total || 0);
  tween($('stProcessed'), s.processed || 0);
  tween($('stQuestions'), s.questions || 0);
  tween($('stAnswers'), s.answers || 0);
  tween($('stErrors'), s.errors || 0);
  const done = (s.processed || 0) + (s.errors || 0), total = s.total || 0;
  $('barFill').style.width = total ? (done / total * 100) + '%' : '0%';
  $('barLabel').textContent = total ? done + ' / ' + total : 'idle';
}
const STATUS_LABEL = {queued:'Queued', uploading:'Uploading', ocr:'OCR',
                      saving:'Saving', cached:'Cached', done:'Done', failed:'Failed'};
function fileCard(ev){
  const wrap = $('files');
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
  counts: null, last: null, cur: null,
  running: false, total: 0, processed: 0, errors: 0,
};
const railBox   = $('stages');
const railStages= railBox.querySelectorAll('.stage');
const railWires = railBox.querySelectorAll('.wire');
function railReset(){ RAIL.counts = {queued:0, uploading:0, ocr:0, saving:0}; RAIL.last = {}; RAIL.cur = null; }
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
      if (ev.name || ev.kind || ev.detail)
        RAIL.cur = {
          name: ev.name || (RAIL.cur && RAIL.cur.name) || '',
          kind: ev.kind || (RAIL.cur && RAIL.cur.kind) || '',
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
      });
      const f = files[ids[ids.length - 1]];          // insertion order → most recent activity
      if (f) RAIL.cur = {name: f.name || '', kind: f.kind || '', detail: f.detail || '', status: f.status};
      break;
    }
  }
  renderRail();
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
  let st = 'idle', nm = 'idle', dt = 'press “Start Scan” to feed the pipeline';
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
  b.innerHTML = PIPE_RUNNING ? IC.stop : IC.play;
  b.title = PIPE_RUNNING
    ? (PIPE_STOPPING ? 'Stopping after the current file…'
                     : 'Stop — finishes the current file, imports parked writes')
    : 'Start scan — click again mid-run to stop it';
}
function handle(ev){
  if (ev.rev){ revLogLine(ev); return; }   // revision lives in its own timeline
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
      revSeedLogs(ev.rev_log || [], true);
      if (ev.running) setRunning(true);
      break;
    case 'log': logLine(ev); break;
    case 'file': fileCard(ev); break;
    case 'error': diagLine(ev); break;
    case 'stats': applyStats(ev.stats || {}); break;
    case 'run_started': PIPE_STOPPING = false; setRunning(true); DIAG.counts = {}; updateDiagCount(); break;
    case 'run_finished': PIPE_STOPPING = false; setRunning(false); break;
    case 'route':
      CredState.events.push({ ts: ev.ts || '', message: ev.text });
      if ($('viewCredentials').classList.contains('active')) credRenderSide();
      break;
  }
}
function connect(){
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(proto + '://' + location.host + '/ws');
  ws.onopen = () => setConn(true);
  ws.onclose = () => { setConn(false); setTimeout(connect, 1500); };
  ws.onmessage = m => handle(JSON.parse(m.data));
}
connect();
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
  const r = await fetch('/api/run', {method: 'POST'});
  const j = await r.json();
  if (!j.started) logLine({type:'log', level:'warn', ts:'',
    message:'A run is already in progress.'});
};

/* ═══════════ TAB SWITCHING ═══════════ */
document.querySelectorAll('.tab').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    tab.classList.add('active');
    const target = tab.dataset.view;
    $('viewPipeline').classList.toggle('active', target === 'pipeline');
    $('viewReview').classList.toggle('active', target === 'review');
    $('viewRevision').classList.toggle('active', target === 'revision');
    $('viewDatabase').classList.toggle('active', target === 'database');
    $('viewCredentials').classList.toggle('active', target === 'credentials');
    if (target === 'review' && !RState.loaded){
      RState.loaded = true;
      loadReviewMeta().then(loadReviewRows);
    }
    if (target === 'revision' && !RevState.loaded){
      RevState.loaded = true;
      loadRevisionSummary().then(loadRevisionItems);
    }
    if (target === 'database' && !DbState.loaded){
      DbState.loaded = true;
      dbInit();
    }
    if (target === 'credentials' && !CredState.loaded){
      CredState.loaded = true;
      loadCredentials();
    }
  };
});

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

function createDropdown(hostId, caption, multi = false){
  const host = $(hostId);
  host.innerHTML =
    '<button class="dd-btn" type="button" aria-expanded="false">' +
      '<span class="dd-cap">' + caption + '</span>' +
      '<i class="dd-pip"></i>' +
    '</button><div class="dd-menu" role="listbox"></div>';
  const btn   = host.querySelector('.dd-btn');
  const pip   = host.querySelector('.dd-pip');
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
  card.appendChild(chips);

  /* question body (hidden in answers-only mode) */
  if (RState.mode !== 'answers'){
    const qt = document.createElement('div');
    qt.className = 'qtext';
    // Preserve line breaks from OCR while keeping math renderable
    const rawText = r.question_text || '';
    const lines = rawText.split('\n');
    lines.forEach((line, i) => {
      if (i > 0) qt.appendChild(document.createElement('br'));
      qt.appendChild(document.createTextNode(line));
    });
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
        txt.textContent = o.text;
        li.append(lab, txt);
        ul.appendChild(li);
      });
      card.appendChild(ul);
    }

    if ((r.diagram_url || r.diagram_bbox) && r.source_id){
      const box = document.createElement('div');
      box.className = 'diag';
      box.dataset.sourceId = r.source_id;
      box.dataset.questionId = r.id;
      box.dataset.bbox = r.diagram_bbox || '';
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
        const explLines = r.answer_explanation.split('\n');
        explLines.forEach((line, i) => {
          if (i > 0) expl.appendChild(document.createElement('br'));
          expl.appendChild(document.createTextNode(line));
        });
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
    rawBtn.innerHTML = IC.scroll;
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
  ok.innerHTML = IC.seal;
  ok.addEventListener('click', () => setReviewStatus(card, 'approved'));
  const no = document.createElement('button');
  no.className = 'act reject';
  no.type = 'button';
  no.title = 'رد این ردیف';
  no.innerHTML = IC.xspark;
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
    const questionId = box.dataset.questionId || '';
    let bbox = parseBbox(box.dataset.bbox);

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

    const canvas = document.createElement('canvas');
    canvas.style.width = '100%';
    canvas.style.maxWidth = MAX_W + 'px';
    canvas.style.aspectRatio = '4 / 3';
    canvas.style.display = 'block';
    canvas.style.background = '#0a0c14';
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
      if (editor.classList.contains('open') && mode === 'full'){
        mode = 'crop';
        swInput.checked = false;
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
    let imgLoaded = false;

    function draw(fit){
      if (!imgLoaded) return;
      const nw = img.naturalWidth, nh = img.naturalHeight;
      const cur = vals();
      let src;

      if (mode === 'crop'){
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
      ctx.drawImage(img, src.x, src.y, src.w, src.h, ox, oy, dw, dh);

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

    swInput.onchange = () => {
      mode = swInput.checked ? 'full' : 'crop';
      cap.textContent = swInput.checked ? 'نمایش برش‌خورده' : 'نمایش بیشتر';
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
        const res = await fetch('/api/review/bbox', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({id: questionId, bbox: cur})
        });
        const j = await res.json();
        if (!j.ok) throw new Error(j.error || '');
        bbox = cur.slice();
        box.dataset.bbox = JSON.stringify(bbox);
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

    const img = new Image();
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
    img.src = '/api/proxy/image/' + encodeURIComponent(sourceId);

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

/* ═══════════ REVISION (three-stage audit console) ═══════════ */
const RevState = { view:'flagged', entity:'question', loaded:false, polling:null, items:[], selected:null,
                   repFilter:'all', running:false, stopping:false, ours:false, lastSeq:0 };

const REV_STATUS_FA  = { pending:'در انتظار', revised:'بازبینی‌شده', needs_human:'نیازمند بررسی انسانی', approved:'تأیید نهایی' };
const REV_STATUS_CLS = { pending:'pending', revised:'revised', needs_human:'rejected', approved:'approved' };
const REV_ISSUE_FA = {
  'null_required_column': 'ستون اجباری خالی است',
  'qa_unmatched_answer': 'پاسخی برای این سؤال جفت نشد',
  'qa_unmatched_question': 'سؤالی برای این پاسخ پیدا نشد',
  'qa_ambiguous_pair': 'جفت‌سازی مبهم — بیش از یک نامزد',
  'linked to matched question': 'اتصال قطعی پاسخ به سؤال',
  'ai_no_verdict': 'AI پاسخی برای این ردیف نداد',
  'derived from explanation review section': 'برچسب گزینه از بخش «بررسی سایر گزینه‌ها» استخراج شد',
  'ai corrected option label from explanation': 'AI برچسب گزینه را از پاسخ تشریحی اصلاح کرد',
  'filled from options by label': 'متن گزینه از روی برچسب در لیست گزینه‌ها پر شد',
  'filled from options by text': 'برچسب گزینه از روی متن در لیست گزینه‌ها پر شد',
  'text normalized + LaTeX sanitized + digit policy': 'یکسان‌سازی متن + پالایش LaTeX + قانون اعداد',
  'Persian digits converted to English': 'اعداد فارسی سال به لاتین تبدیل شد',
  'options normalized/rebuilt + LaTeX sanitized': 'لیست گزینه‌ها بازسازی و پالایش شد',
  'tags rebuilt from metadata': 'برچسب‌ها از فراداده بازسازی شد',
  'filled from source storage url': 'نشانی شکل از منبع پر شد',
};
function revIssueText(rep){
  const i = String(rep.issue || '');
  if (REV_ISSUE_FA[i]) return REV_ISSUE_FA[i];
  if (i.startsWith('filled from paired ')) return 'از ' + (i.includes('answer') ? 'پاسخ' : 'سؤال') + 'ِ جفت‌شده پر شد';
  if (i.startsWith('qa_label_mismatch')) return 'برچسب گزینه با «بررسی سایر گزینه‌ها» در تضاد است';
  if (i.startsWith('ai suggested unknown label')) return 'AI برچسب گزینه‌ای پیشنهاد داد که در لیست نیست';
  return i;
}

function revEl(cls, text){ const d = document.createElement('div'); if (cls) d.className = cls; if (text != null) d.textContent = text; return d; }
function revSpan(cls, text){ const s = document.createElement('span'); if (cls) s.className = cls; if (text != null) s.textContent = text; return s; }
function revChip(st){
  const s = st || 'pending';
  const chip = revSpan('status-chip sc-' + (REV_STATUS_CLS[s] || 'pending'), REV_STATUS_FA[s] || s);
  return chip;
}
function revLinkChip(kind, text){ return revSpan('rev-link' + (kind ? ' ' + kind : ''), text); }
function revTextBlock(cls, txt){
  const d = revEl(cls);
  String(txt || '').split('\n').forEach((ln,i)=>{
    if (i>0) d.appendChild(document.createElement('br'));
    d.appendChild(document.createTextNode(ln));
  });
  return d;
}
function revMetaChips(it){
  const row = revEl('rev-chiprow');
  [[ 'موضوع', it.subject && subjectFa(it.subject)], ['فصل', it.topic && chapterFa(it.topic)],
   ['پایه', it.grade != null && it.grade !== '' ? faDigits(it.grade) : null],
   ['ناشر', it.corp], ['سال', it.year && faDigits(it.year)], ['سطح', it.difficulty]]
    .forEach(([k,v]) => { if (v) row.appendChild(revSpan('rev-metachip', k + ': ' + v)); });
  return row;
}

/* ── summary stat cards + stage counters ── */
async function loadRevisionSummary(){
  try{
    const m = await (await fetch('/api/revision/summary')).json();
    renderRevStats(m);
    const c = m.counts || {};
    $('revCounts').textContent =
      'سؤال — نیازمند بررسی: ' + faDigits(c.needs_human || 0) +
      ' · بازبینی‌شده: ' + faDigits(c.revised || 0) +
      ' · در انتظار: ' + faDigits(c.pending || 0);
  }catch(e){}
}

function revStatCard(title, total, c, nr){
  const w = n => c && total ? (n/total*100) + '%' : '0%';
  const card = revEl('rev-stat-card');
  card.innerHTML =
    `<div class="rev-stat-head"><b>${title}</b><span>${faDigits(total||0)} مورد</span></div>` +
    `<div class="rev-segbar">` +
      `<span class="s-h" style="width:${w(c.needs_human||0)}"></span>` +
      `<span class="s-r" style="width:${w(c.revised||0)}"></span>` +
      `<span class="s-a" style="width:${w(c.approved||0)}"></span>` +
      `<span class="s-p" style="width:${w(c.pending||0)}"></span>` +
    `</div>` +
    `<div class="rev-legend">` +
      `<span><i style="background:var(--red)"></i>نیازمند بررسی ${faDigits(c.needs_human||0)}</span>` +
      `<span><i style="background:linear-gradient(90deg,var(--violet),var(--cyan))"></i>بازبینی ${faDigits(c.revised||0)}</span>` +
      `<span><i style="background:var(--green)"></i>تأیید ${faDigits(c.approved||0)}</span>` +
      `<span><i style="background:#565b70"></i>در انتظار ${faDigits(c.pending||0)}</span>` +
      (nr ? `<span style="color:var(--amber)">⚠ ${faDigits(nr)} نیازمند دید انسانی</span>` : '') +
    `</div>`;
  return card;
}

function renderRevStats(m){
  const box = $('revStats');
  if (!box) return;
  box.innerHTML = '';
  const q = m.counts || {}, a = m.answer_counts || {};
  const sum = c => (c.pending||0)+(c.revised||0)+(c.needs_human||0)+(c.approved||0);
  box.appendChild(revStatCard('سؤال‌ها', sum(q), q, m.needs_review));
  box.appendChild(revStatCard('پاسخ‌ها', sum(a), a, m.answer_needs_review));
  fetch('/api/revision/state').then(r=>r.json()).then(st=>{
    revApplyState(st);
    const s = st.last_summary;
    if (!s) return;
    const card = revEl('rev-stat-card');
    card.innerHTML =
      `<div class="rev-stat-head"><b>آخرین اسکن</b><span>${faDigits((s.questions_scanned||0)+(s.answers_scanned||0))} بررسی‌شده</span></div>` +
      `<div class="rev-legend" style="margin-top:8px">` +
        `<span><i style="background:var(--green)"></i>پیوند ساخته‌شده ${faDigits(s.links||0)}</span>` +
        `<span><i style="background:var(--cyan)"></i>تکمیل قطعی ${faDigits(s.backfills||0)}</span>` +
        `<span><i style="background:var(--red)"></i>ستون خالی ${faDigits(s.null_reports||0)}</span>` +
        `<span><i style="background:var(--amber)"></i>تعارض ${faDigits(s.conflicts||0)}</span>` +
        `<span><i style="background:#a5b4fc"></i>پرچم AI ${faDigits(s.ai_flags||0)}</span>` +
      `</div>`;
    box.appendChild(card);
  }).catch(()=>{});
}

/* ── the revision stage rail · same visual language as the pipeline's ── */
const RAILREV = { box:null, stages:[], wires:[] };
function revRailInit(){
  RAILREV.box = $('revStages'); if (!RAILREV.box) return;
  RAILREV.stages = [...RAILREV.box.querySelectorAll('.stage')];
  RAILREV.wires  = [...RAILREV.box.querySelectorAll('.wire')];
}
function revBubble(id, v){
  const el = $(id); if (!el) return;
  const n = Number(v || 0);
  const stg = el.closest ? el.closest('.stage') : null;
  if (stg) stg.classList.toggle('counting', n > 0);
  el.textContent = n ? String(n) : '';
}
const REV_STAGE_EN = { completeness:'Completeness', pairing:'Pairing & Fill', ai:'AI Audit' };
function revRail(st){
  if (!RAILREV.box) revRailInit();
  const running = !!(st && st.running);
  const pr = st && st.progress;
  const s = (pr && pr.summary) || (st && st.last_summary) || {};
  revBubble('revStageNull', s.null_reports);
  revBubble('revStagePair', (s.links || 0) + (s.backfills || 0));
  revBubble('revStageAi', s.ai_flags);
  const ORDER = ['completeness', 'pairing', 'ai'];
  const cur = running ? Math.max(0, ORDER.indexOf((pr && pr.stage) || 'completeness')) : -1;
  RAILREV.stages.forEach((el, i) => {
    el.classList.remove('active', 'warm', 'passed', 'err');
    if (running){
      if (i < cur) el.classList.add('warm');
      else if (i === cur) el.classList.add('active');
    } else if (st && st.last_summary) el.classList.add(st.last_summary.stopped ? 'warm' : 'passed');
  });
  RAILREV.wires.forEach((w, i) => {
    w.classList.remove('live', 'warm');
    if (running && cur === i + 1) w.classList.add('live');
    else if ((running && cur > i + 1) || (!running && st && st.last_summary && !st.last_summary.stopped)) w.classList.add('warm');
  });
  RAILREV.box.classList.toggle('live', running);
  const now = $('revNow'); if (!now) return;
  const name = $('revNowName'), det = $('revNowDetail');
  if (running && pr){
    now.dataset.state = 'live';
    name.textContent = REV_STAGE_EN[pr.stage] || REV_STAGE_EN.completeness;
    const pct = pr.chunks_total ? Math.max(3, Math.round(pr.chunk / pr.chunks_total * 100)) : 3;
    det.textContent = (pr.part > 1 ? 'answers pass' : 'questions pass')
      + ' — chunk ' + pr.chunk + ' of ' + pr.chunks_total
      + ' (' + pct + '%)' + (st.stop_requested ? ' · stopping…' : '');
  } else if (st && st.last_summary){
    const ls = st.last_summary;
    if (ls.stopped){ now.dataset.state = 'stopped'; name.textContent = 'stopped'; }
    else if (ls.errors > 0){ now.dataset.state = 'err'; name.textContent = 'done with errors'; }
    else { now.dataset.state = 'done'; name.textContent = 'ready'; }
    det.textContent = 'last scan: ' + ((ls.questions_scanned || 0) + (ls.answers_scanned || 0))
      + ' reviewed · ' + (ls.links || 0) + ' linked · ' + (ls.revised || 0)
      + ' revised · ' + (ls.needs_human || 0) + ' need review';
  } else {
    now.dataset.state = 'idle'; name.textContent = 'idle';
    det.textContent = 'press “Run Scan” — the three stages run over 50-item batches';
  }
}

/* ── revision live log · its own box, never mixed with the pipeline's ── */
function revLogLine(ev, replay){
  const box = $('revLog'); if (!box) return;
  if (ev.seq != null){
    if (!replay && ev.seq <= RevState.lastSeq) return;
    if (ev.seq > RevState.lastSeq) RevState.lastSeq = ev.seq;
  }
  const ph = box.querySelector('.empty'); if (ph) ph.remove();
  const line = document.createElement('div');
  line.className = 'line lv-' + (ev.level || 'info');
  line.innerHTML = '<span class="t"></span><span class="m"></span>';
  line.querySelector('.t').textContent = ev.ts || '';
  line.querySelector('.m').textContent = String(ev.message || '').replace(/^\[Revision\]\s*/, '');
  box.appendChild(line);
  while (box.children.length > 300) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  const c = $('revLogCount');
  if (c) c.textContent = box.querySelectorAll('.line').length + ' events';
}
function revSeedLogs(list, rebuild){
  const box = $('revLog');
  if (!box || !list || !list.length) return;
  if (rebuild){
    box.innerHTML = '';
    RevState.lastSeq = 0;
    list.forEach(e => revLogLine(e, true));
  } else {
    list.forEach(e => revLogLine(e, false));
  }
}
$('revLogClearBtn').onclick = () => {
  const box = $('revLog');
  if (!box.querySelector('.line')){ toast('لاگ اسکن همین حالا خالی است'); return; }
  box.innerHTML = '<div class="empty">لاگ پاک شد — رویدادهای تازه همین‌جا سرازیر می‌شوند.</div>';
  $('revLogCount').textContent = '';
};

/* ── item list ── */
async function loadRevisionItems(){
  const list = $('revList');
  list.innerHTML = '<div class="rev-empty">در حال بارگذاری…</div>';
  try{
    const url = '/api/revision/items?view=' + RevState.view + '&entity=' + RevState.entity;
    const data = await (await fetch(url)).json();
    RevState.items = data.items || [];
    list.innerHTML = '';
    if (!RevState.items.length){
      list.innerHTML = '<div class="rev-empty">موردی یافت نشد</div>';
      $('revDetail').innerHTML = '<div class="rev-empty">موردی برای نمایش وجود ندارد</div>';
      RevState.selected = null;
      return;
    }
    list.appendChild(revEl('rev-list-head', faDigits(RevState.items.length) + ' ' + (RevState.entity==='answer' ? 'پاسخ' : 'سؤال')));
    RevState.items.forEach(it => list.appendChild(renderRevListItem(it)));
    selectRevItem(RevState.items[0].id);
  }catch(e){
    list.innerHTML = '<div class="rev-empty" style="color:var(--red)">خطا در دریافت داده‌ها</div>';
  }
}

function revSeverityStats(it){
  const reps = it.reports || [];
  return {
    err:  reps.filter(r=>r.severity==='error').length,
    warn: reps.filter(r=>r.severity==='warn').length
  };
}

function renderRevListItem(it){
  const isA = RevState.entity === 'answer';
  const s = revSeverityStats(it);
  const row = revEl('rev-row');
  row.dataset.id = it.id;

  const top = revEl('rev-row-top');
  const title = revEl('rev-row-title');
  title.appendChild(document.createTextNode((isA ? 'پاسخ ' : 'سؤال ') + faDigits(it.question_number ?? '—')));
  title.appendChild(isA
    ? revLinkChip(it.question_id ? 'ok' : 'bad', it.question_id ? 'متصل' : 'بی‌سؤال')
    : revLinkChip(it.answer_id ? 'ok' : '',    it.answer_id ? 'جفت‌شده' : 'بی‌پاسخ'));
  const badges = revEl('rev-row-badges');
  if (s.err)  badges.appendChild(revSpan('rev-count-badge e', faDigits(s.err)));
  if (s.warn) badges.appendChild(revSpan('rev-count-badge w', faDigits(s.warn)));
  badges.appendChild(revSpan('sev-dot ' + (s.err ? 'e' : (s.warn ? 'w' : 'i'))));
  top.append(title, badges);

  const meta = revEl('rev-row-meta');
  const parts = [it.subject && subjectFa(it.subject), it.topic && chapterFa(it.topic), it.difficulty];
  if (isA && it.correct_option_label) parts.push('گزینه ' + faDigits(it.correct_option_label));
  meta.textContent = parts.filter(Boolean).join(' · ');

  row.append(top, meta);
  row.addEventListener('click', () => selectRevItem(it.id));
  return row;
}

function selectRevItem(id){
  RevState.selected = id;
  document.querySelectorAll('.rev-row').forEach(r => r.classList.toggle('active', r.dataset.id === id));
  const it = RevState.items.find(x => x.id === id);
  if (it) renderRevDetail(it);
}

/* ── detail pane: question ⇄ answer pair ── */
function revOptionList(it, correctLabel){
  const opts = parseOptions(it.options);
  if (!opts.length) return null;
  const ul = document.createElement('ul'); ul.className = 'opts';
  opts.forEach(o=>{
    const li = document.createElement('li');
    if (correctLabel && String(o.label) === String(correctLabel)) li.classList.add('correct');
    const lab = revSpan('opt-label', o.label);
    const txt = revSpan('opt-text', o.text);
    li.append(lab, txt); ul.appendChild(li);
  });
  return ul;
}

function revNoAnswerBox(msg){
  return revEl('answer-box no-answer', msg || 'پاسخی جفت نشده است.');
}

function renderRevDetail(it){
  const pane = $('revDetail');
  pane.innerHTML = '';
  const isA = RevState.entity === 'answer';

  // header
  const head = revEl('rev-detail-head');
  const left = document.createElement('div');
  const title = revEl('rev-detail-title', (isA ? 'پاسخ ' : 'سؤال ') + faDigits(it.question_number ?? '—'));
  const sub = revEl('rev-detail-sub');
  sub.textContent = [ it.grade != null && it.grade !== '' ? ('پایه ' + faDigits(it.grade)) : null,
                      it.corp, it.year && faDigits(it.year),
                      it.revised_at ? ('بازبینی: ' + faDate(it.revised_at)) : null ]
                      .filter(Boolean).join(' · ');
  left.append(title, sub);
  const side = revEl('rev-detail-side');
  side.appendChild(revChip(it.revision_status));
  const notes = it.revision_notes || {};
  if (notes.ai_confidence != null){
    side.appendChild(revEl('rev-conf', 'اطمینان مدل: ' + faDigits(Math.round(Number(notes.ai_confidence)*100)) + '٪'));
  }
  head.append(left, side);
  pane.appendChild(head);
  pane.appendChild(revMetaChips(it));

  // paired question/answer grid
  const grid = revEl('rev-pair-grid');
  const qc = revEl('rev-pair-col q-col');
  const qtag = revEl('rev-pair-tag');
  qtag.appendChild(revSpan('t-q', 'متن سؤال'));
  const correctLabel = isA ? (it.correct_option_label || it.q_correct_option_label) : (it.correct_option_label || it.answer_correct_option_label);
  if (correctLabel) qtag.appendChild(revSpan('rev-metachip', 'گزینه صحیح: ' + faDigits(correctLabel)));
  qc.appendChild(qtag);
  if (it.question_text) qc.appendChild(revTextBlock('rev-qtext', it.question_text));
  else qc.appendChild(revEl('answer-box no-answer', 'متن سؤال خالی است (ستون اجباری).'));
  const ol = revOptionList(it, correctLabel);
  if (ol) qc.appendChild(ol);

  const ac = revEl('rev-pair-col a-col');
  const atag = revEl('rev-pair-tag');
  atag.appendChild(revSpan('t-a', 'پاسخ تشریحی'));
  if (isA){
    atag.appendChild(revChip(it.revision_status));
  } else if (it.answer_id){
    atag.appendChild(revChip(it.answer_revision_status));
  }
  ac.appendChild(atag);
  if (isA ? !!it.answer_explanation : !!it.answer_id){
    ac.appendChild(revTextBlock('aexpl', it.answer_explanation || ''));
    if (isA && it.correct_option_label){
      ac.appendChild(revEl('rev-conf', 'برچسب گزینه: ' + faDigits(it.correct_option_label)));
    }
  } else {
    ac.appendChild(revNoAnswerBox(isA ? 'متن پاسخ تشریحی خالی است (ستون اجباری).'
                                      : 'پاسخی به این سؤال جفت نشده — جفت‌سازی بر پایهٔ شماره/درس/پایه/فصل نتیجه نداد.'));
  }

  if (isA) grid.append(ac, qc);
  else grid.append(qc, ac);
  pane.appendChild(grid);

  // audit findings (filterable)
  const reps = it.reports || [];
  pane.appendChild(revEl('rev-section-label', 'یافته‌های ممیزی (' + faDigits(reps.length) + ')'));
  const fbox = revEl('rev-repf');
  [['all','همه'],['error','خطا'],['warn','هشدار'],['fix','اصلاحات کد'],['ai','AI']].forEach(([k,label])=>{
    const b = document.createElement('button');
    b.textContent = label;
    if (RevState.repFilter === k) b.classList.add('on');
    b.onclick = () => { RevState.repFilter = k; renderRevDetail(it); };
    fbox.appendChild(b);
  });
  pane.appendChild(fbox);

  const order = { error:0, warn:1, info:2 };
  const shown = reps
    .filter(r => RevState.repFilter === 'all' ? true
                : RevState.repFilter === 'error' ? r.severity === 'error'
                : RevState.repFilter === 'warn' ? r.severity === 'warn'
                : RevState.repFilter === 'fix' ? (r.source === 'code' && r.severity === 'info')
                : r.source === 'ai')
    .sort((a,b)=>(order[a.severity]??3)-(order[b.severity]??3));
  if (!shown.length){
    const none = revEl('rev-report sev-info');
    const msg = revEl('rev-issue', reps.length ? 'موردی در این فیلتر نیست.' : 'هیچ ایرادی ثبت نشده است.');
    msg.style.direction = 'rtl'; msg.style.textAlign = 'right'; msg.style.fontFamily = 'Vazirmatn';
    none.appendChild(msg); pane.appendChild(none);
  } else {
    shown.forEach(rep => pane.appendChild(renderRevReport(rep)));
  }

  // per-entity actions
  const foot = revEl('rev-actions');
  function actRow(label, entityId, entityKind){
    const r = revEl('rev-action-row');
    const b = document.createElement('b'); b.textContent = label;
    const ok = document.createElement('button');
    ok.className = 'act approve'; ok.type = 'button';
    ok.title = 'تأیید و بستن — ' + label;
    ok.innerHTML = IC.seal;
    ok.onclick = () => resolveRevision(entityId, 'approve', entityKind);
    const again = document.createElement('button');
    again.className = 'act reopen'; again.type = 'button';
    again.title = 'بررسی مجدد — بازگشت به صف اسکن';
    again.innerHTML = IC.reopen;
    again.onclick = () => resolveRevision(entityId, 'reopen', entityKind);
    r.append(b, ok, again);
    return r;
  }
  if (!isA){
    foot.appendChild(actRow('سؤال', it.id, 'question'));
    if (it.answer_id) foot.appendChild(actRow('پاسخ', it.answer_id, 'answer'));
  } else {
    foot.appendChild(actRow('پاسخ', it.id, 'answer'));
  }
  pane.appendChild(foot);

  typesetMath(pane);
}

function renderRevReport(rep){
  const box = revEl('rev-report sev-' + (rep.severity || 'info'));

  const head = revEl('rev-report-head');
  const sev = revSpan('rev-sev ' + (rep.severity==='error' ? 'error' : (rep.severity==='warn' ? 'warn' : 'info')),
                       rep.severity==='error' ? 'خطا' : (rep.severity==='warn' ? 'هشدار' : 'اصلاح'));
  head.appendChild(sev);
  if (rep.entity){
    head.appendChild(revSpan('rev-ent' + (rep.entity==='question' ? ' q' : ''),
                             rep.entity==='question' ? 'سؤال' : 'پاسخ'));
  }
  if (rep.field){
    head.appendChild(revSpan('rev-field', rep.field));
  }
  head.appendChild(revSpan('rev-src', rep.source==='ai' ? 'AI' : 'کد'));
  box.appendChild(head);

  const issue = revEl('rev-issue');
  const txt = revIssueText(rep);
  if (/[\u0600-\u06FF]/.test(txt)){
    issue.style.direction = 'rtl'; issue.style.textAlign = 'right'; issue.style.fontFamily = 'Vazirmatn';
  }
  issue.textContent = txt;
  box.appendChild(issue);

  if (rep.before_value || rep.after_value){
    const diff = revEl('rev-diff');
    if (rep.before_value){
      diff.appendChild(revSpan('before', 'قبل: ' + rep.before_value));
    }
    if (rep.before_value && rep.after_value){
      diff.appendChild(revSpan('arrow', '←'));
    }
    if (rep.after_value){
      diff.appendChild(revSpan('after', 'بعد: ' + rep.after_value));
    }
    box.appendChild(diff);
  }
  return box;
}

async function resolveRevision(id, action, entity){
  try{
    const res = await fetch('/api/revision/resolve', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({id, action, entity: entity || 'question'})
    });
    const j = await res.json();
    if (!j.ok) throw new Error();
    toast(action==='approve' ? 'تأیید شد ✓' : 'برای بازبینی مجدد به صف بازگشت');
    loadRevisionSummary();
    loadRevisionItems();
  }catch(e){ toast('خطا در ذخیرهٔ وضعیت'); }
}

/* ── run / stop · the scan is interruptible after every chunk ── */
function revRunBtnState(){
  const b = $('revRunBtn'); if (!b) return;
  b.classList.toggle('running', RevState.running);
  b.classList.toggle('stopping', RevState.running && RevState.stopping);
  b.innerHTML = RevState.running ? IC.stop : IC.play;
  b.title = RevState.running
    ? (RevState.stopping ? 'در حال توقف — چکب جاری تمام می‌شود…'
                          : 'توقف اسکن — پس از چکب جاری؛ ردیف‌های هوش‌مصنوعی‌نشده در صف می‌مانند')
    : 'اجرای اسکن — سه مرحله روی چکب‌های ۵۰تایی';
}
function revApplyState(st){
  const was = RevState.running;
  RevState.running = !!st.running;
  RevState.stopping = RevState.running && !!st.stop_requested;
  revRail(st);
  revSeedLogs(st.logs, false);
  revRunBtnState();
  const pr = st.progress;
  if (RevState.running && pr){
    $('revProgress').hidden = false;
    const pct = pr.chunks_total ? Math.max(3, Math.round(pr.chunk / pr.chunks_total * 100)) : 3;
    $('revProgressFill').style.width = pct + '%';
    $('revProgressLabel').textContent = (pr.part > 1 ? 'روبش پاسخ‌ها' : 'اسکن سؤال‌ها')
      + ' — چکب ' + faDigits(pr.chunk) + ' از ' + faDigits(pr.chunks_total) + ' (' + faDigits(pct) + '٪)';
  } else if (!RevState.running){
    $('revProgress').hidden = true;
  }
  if (was && !RevState.running){
    // a run just ended (naturally, or on request) — refresh everything once
    if (RevState.polling){ clearInterval(RevState.polling); RevState.polling = null; }
    RevState.ours = false;
    loadRevisionSummary();
    if (RevState.loaded) loadRevisionItems();
    const s = st.last_summary;
    if (s){
      if (s.stopped)
        toast('اسکن متوقف شد — ' + faDigits(s.links || 0) + ' پیوند و ' +
              faDigits(s.revised || 0) + ' بازبینی تا این لحظه؛ بقیه در صف بعدی', 4600);
      else
        toast('اسکن تمام شد — ' + faDigits(s.revised || 0) + ' بازبینی، ' +
              faDigits(s.needs_human || 0) + ' نیازمند بررسی، ' +
              faDigits(s.links || 0) + ' پیوند ساخته‌شده', 4200);
    }
  }
}
function revPollOnce(){
  return fetch('/api/revision/state').then(r => r.json()).then(revApplyState).catch(() => {});
}
$('revRunBtn').onclick = async () => {
  if (RevState.running){
    if (RevState.stopping) return;                 // already asked to halt
    RevState.stopping = true; revRunBtnState();
    try{
      const j = await (await fetch('/api/revision/stop', {method:'POST'})).json();
      if (!j.stopping) RevState.stopping = false;
      else toast('توقف درخواست شد — چکب جاری کامل می‌شود، سپس اسکن می‌ایستد.', 4600);
    }catch(e){ RevState.stopping = false; }
    revRunBtnState();
    return;
  }
  $('revProgress').hidden = false;
  $('revProgressFill').style.width = '2%';
  $('revProgressLabel').textContent = 'آماده‌سازی اسکن…';
  try{
    const j = await (await fetch('/api/revision/run', {method:'POST'})).json();
    if (!j.started){ toast('یک اسکن همین حالا در حال اجراست'); }
    else RevState.ours = true;
  }catch(e){ RevState.ours = true; }
  RevState.running = true; RevState.stopping = false;
  revRunBtnState();
  if (RevState.polling) clearInterval(RevState.polling);
  RevState.polling = setInterval(revPollOnce, 1500);
};

$('revRefresh').onclick = () => { revPollOnce(); loadRevisionSummary().then(loadRevisionItems); };
document.querySelectorAll('#revView button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#revView button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    RevState.view = b.dataset.rview;
    loadRevisionItems();
  };
});
document.querySelectorAll('#revEntity button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#revEntity button').forEach(x=>x.classList.remove('active'));
    b.classList.add('active');
    RevState.entity = b.dataset.rentity;
    loadRevisionItems();
  };
});

/* restore a mid-run scan after a page reload: rail, log replay and polling */
revRailInit();
revPollOnce().then(() => {
  if (RevState.running && !RevState.polling) RevState.polling = setInterval(revPollOnce, 1500);
});


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
          ctx.strokeStyle = 'rgba(255,255,255,' + alpha.toFixed(3) + ')';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(a.x, a.y);
          ctx.lineTo(b.x, b.y);
          ctx.stroke();
        }
      }
    }
    /* nodes */
    ctx.fillStyle = 'rgba(255,255,255,.5)';
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
const CRED_KEEP = '__KEEP__';           // server-side "leave stored value alone"
const CredState = { loaded:false, rows:[], ladder:[], events:[], supaKeySet:false,
                    gateway:null, usageCap:20, dragIdx:null, provider:'local' };

const credEsc = s => String(s ?? '').replace(/[<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// 'gemini-3.5-flash' → '3.5' (bars would be unreadable otherwise)
function credModelTag(m){
  return m.replace(/^gemini[-_]?/i, '').replace(/[-_]?(flash|pro|lite)$/i, '') || m;
}

function credRowEl(r, i){
  const wrap = document.createElement('div');
  const state = r.removed ? 'removed' : (r.checking ? 'checking'
    : (r.state || (r.keep === null ? 'new' : 'unknown')));
  wrap.className = 'cred-key s-' + state;
  if (r.removed){
    wrap.innerHTML = `<i class="dot"></i>` +
      `<span class="cred-rmv">${credEsc(r.masked)} · removed on save</span>` +
      `<button class="cred-btn undo" type="button" title="Undo removal — keep this key">${IC.undo}</button>`;
    wrap.querySelector('.undo').onclick = () => { r.removed = false; credRender(); };
    return wrap;
  }
  const bars = CredState.ladder.map(m => {
    const um = (r.usage && r.usage.models) || {};
    const lim = (r.usage && r.usage.limit) || CredState.usageCap;
    const used = um[m] || 0;
    const frac = Math.max(0, Math.min(1, used / lim));
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
      `<button class="cred-btn rep" type="button" title="Replace this key">${IC.pencil}</button>`
    : `<input class="cred-in cred-keyin" type="text" spellcheck="false" autocomplete="off"
        placeholder="${r.keep !== null ? 'paste new key · replaces ' + credEsc(r.masked) : 'AIza…'}"
        value="${credEsc(r.value || '')}">`;
  wrap.innerHTML =
    `<div class="cred-key-main">` +
    `<button class="cred-btn cred-grip" type="button" title="Hold and drag to reorder — the router tries keys top to bottom">⠿</button>` +
    `<i class="dot" title="${credEsc(state)}"></i>` +
    `<input class="cred-in cred-name" type="text" spellcheck="false" autocomplete="off"
       placeholder="${credEsc('Key ' + ((r.keep ?? i) + 1))}" value="${credEsc(r.name || '')}">` +
    keyField +
    (r.keep !== null
      ? `<button class="cred-btn chk" type="button" title="Re-check this key — one free googleapis listing call, no model used">♥</button>`
      : '') +
    `<div class="cred-models">${bars}</div>` +
    `<button class="cred-btn rm" type="button" title="Remove on save">${IC.xspark}</button>` +
    `</div>`;
  const nameIn = wrap.querySelector('.cred-name');
  nameIn.oninput = () => r.name = nameIn.value;
  const keyIn = wrap.querySelector('.cred-keyin');
  if (keyIn) keyIn.oninput = () => r.value = keyIn.value.trim();
  const rep = wrap.querySelector('.rep');
  if (rep) rep.onclick = () => { r.replaced = true; credRender(); };
  const chk = wrap.querySelector('.chk');
  if (chk) chk.onclick = () => credCheckOne(r.keep);
  wrap.querySelector('.rm').onclick = () => {
    if (r.keep === null) CredState.rows.splice(i, 1);
    else r.removed = true;
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
  credRender();
}

function credRender(){
  const box = $('credKeys');
  box.innerHTML = '';
  CredState.rows.forEach((r, i) => box.appendChild(credRowEl(r, i)));
  const stored = CredState.rows.filter(r => !r.removed && r.keep !== null);
  const live = stored.filter(r => r.state === 'ok' || r.state === 'alive_unlisted').length;
  $('credKeyHint').textContent =
    `${stored.length + CredState.rows.filter(r => r.keep === null && !r.removed).length} key(s) · ${live} verified`;
  credGatewayBar();
  credRenderSide();
}

function credGatewayBar(){
  const el = $('credGateway');
  const g = CredState.gateway;
  if (!g){ el.hidden = true; el.innerHTML = ''; return; }
  el.hidden = false;
  if (g.reachable && !g.google_err){
    el.className = 'cred-gw ok';
    el.innerHTML = `🛰 <b>googleapis.com reachable</b> — ${credEsc(g.detail || 'the path to Google is fine')}. ` +
      `Each key is then checked one by one with a free listing call — a model is never used here.`;
  } else if (g.google_err){
    el.className = 'cred-gw warn';
    el.innerHTML = `🌩 <b>Google is erroring right now</b> — ${credEsc(g.detail || 'HTTP 5xx')}. ` +
      `Your tunnel works; this is on Google's side. Wait a few minutes and re-check.`;
  } else {
    el.className = 'cred-gw bad';
    el.innerHTML = `🔌 <b>googleapis.com unreachable</b> — ${credEsc(g.detail || '')}. ` +
      `This is your tunnel / exit-IP / region — <u>not</u> your keys, so none of them were checked or blamed.`;
  }
}

function credRenderSide(){
  const stored = CredState.rows.filter(r => !r.removed && r.keep !== null);
  const live = stored.filter(r => r.state === 'ok' || r.state === 'alive_unlisted').length;
  $('credRoute').innerHTML =
    `<b>${live}/${stored.length}</b> keys verified · ladder ` +
    CredState.ladder.map((m, j) =>
      (j ? '<span class="arr">→</span>' : '') +
      `<em title="${credEsc(m)}">${credEsc(credModelTag(m))}</em>`).join(' ');
  const ev = $('credEvents');
  const list = CredState.events.slice(-14).reverse();
  ev.innerHTML = list.length
    ? list.map(e => `<div class="cev"><i>${credEsc(e.ts)}</i><span>${credEsc(e.message)}</span></div>`).join('')
    : '<div class="cev dim">no routing events yet — run a scan or a health check</div>';
}

async function loadCredentials(){
  try{
    const res = await fetch('/api/credentials');
    const d = await res.json();
    CredState.ladder = d.models || [];
    CredState.gateway = d.gateway || null;
    CredState.usageCap = (d.keys && d.keys[0] && d.keys[0].usage && d.keys[0].usage.limit) || 20;
    CredState.rows = (d.keys || []).map(k => ({
      keep:k.index, masked:k.masked, name:k.name || '', value:'', replaced:false, removed:false,
      models:{}, blocked:k.blocked_models || [], usage:k.usage || null, calls:k.calls || 0,
      checking:false,
      state: k.serving ? 'ok' : k.cooling ? 'cooldown'
             : (k.last_fault === 'bad_key' ? 'dead' : 'unknown'),
    }));
    const hint = $('credModelsHint');
    if (hint) hint.textContent = CredState.ladder.length
      ? 'Every key walks the same ladder: ' + CredState.ladder.join(' → ') : '';
    $('credSupaUrl').value = d.supabase_url || '';
    $('credSupaBucket').value = d.supabase_bucket || '';
    $('credSupaKey').value = '';
    $('credSupaKey').placeholder = d.supabase_key_masked || '— not set —';
    // AI router / provider box (values come from .env via the server)
    CredState.provider = d.router_provider === 'openai' ? 'openai' : 'local';
    $('credRouterUrl').value = d.router_url || '';
    $('credRouterModel').value = d.router_model || '';
    $('credRouterKey').value = '';
    $('credRouterKey').placeholder = d.router_key_masked || '— not set —';
    document.querySelectorAll('#credProvider button').forEach(b =>
      b.classList.toggle('active', b.dataset.provider === CredState.provider));
    $('credLegacyNote').hidden = !d.legacy_pool;
    CredState.events = d.recent || [];
    credRender();
  }catch(e){ toast('Credentials: could not load — ' + e); }
}

async function credCheck(){
  const btn = $('credCheckBtn');
  const span = btn.querySelector('span');
  const stored = CredState.rows.filter(r => !r.removed && r.keep !== null);
  if (!stored.length){ toast('Save at least one key before checking'); return; }
  btn.disabled = true;
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
    btn.disabled = false;
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

async function credSave(){
  const keys = [], names = [];
  let bad = null;
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
  const btn = $('credSaveBtn');
  btn.disabled = true;
  try{
    const res = await fetch('/api/credentials', {
      method:'POST',
      headers:{ 'Content-Type':'application/json' },
      body: JSON.stringify({
        gemini_keys: keys, gemini_names: names,
        model_ladder: CredState.ladder,   // fixed server-side; not user-chosen
        supabase_url: $('credSupaUrl').value.trim(),
        supabase_bucket: $('credSupaBucket').value.trim(),
        supabase_key: $('credSupaKey').value.trim() || CRED_KEEP,
        router_provider: CredState.provider,
        router_url: $('credRouterUrl').value.trim() || CRED_KEEP,
        router_key: $('credRouterKey').value.trim() || CRED_KEEP,
        router_model: $('credRouterModel').value.trim() || CRED_KEEP,
      }),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || d.error || res.status);
    await loadCredentials();
    toast(`Saved to .env ✓ ${d.key_count} Gemini key(s) routed`);
  }catch(e){ toast('Save failed — ' + e); }
  finally { btn.disabled = false; }
}

$('credAddKey').onclick = () => {
  CredState.rows.push({ keep:null, name:'', value:'', masked:'', replaced:false,
                        removed:false, state:'new', models:{}, blocked:[],
                        usage:null, checking:false });
  credRender();
};
document.querySelectorAll('#credProvider button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('#credProvider button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    CredState.provider = b.dataset.provider;
  };
});
$('credCheckBtn').onclick = credCheck;
$('credSaveBtn').onclick = credSave;

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
      ${t.row_actions ? '' : '<em class="db-ro" title="no primary key — read-only grid">read-only</em>'}
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

function dbCellHtml(v){
  if (v === null || v === undefined) return '<i class="db-null">NULL</i>';
  if (v === true || v === false)
    return '<span class="db-bool ' + (v ? 'yes' : 'no') + '">' + v + '</span>';
  const s = String(v);
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
      <button type="button" class="db-ib" data-a="edit"   title="Edit row">${IC.pencil}</button>
      <button type="button" class="db-ib" data-a="dup"    title="Duplicate row">${IC.dup}</button>
      <button type="button" class="db-ib del" data-a="del" title="Delete this row (saved to graveyard)">${IC.ghost}</button>`
      : '<span class="db-ro">—</span>') + '</td></tr>').join('');
  g.innerHTML =
    `<table class="db-grid"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;

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
    <button class="icon-btn sm" id="dbPrev" type="button" title="Previous page" ${DbState.offset <= 0 ? 'disabled' : ''}>${IC.chevL}</button>
    <span class="count-line">${from}–${to} of ${DbState.total.toLocaleString()}
      ${DbState.q ? ' · search: “' + dbEsc(DbState.q) + '”' : ''}</span>
    <button class="icon-btn sm" id="dbNext" type="button" title="Next page" ${to >= DbState.total ? 'disabled' : ''}>${IC.chevR}</button>`;
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
  btn.innerHTML = IC.warn;
  btn.title = 'Click again to delete for good';
  setTimeout(() => {
    delete btn.dataset.armed; btn.classList.remove('armed');
    btn.innerHTML = IC.ghost; btn.title = 'Delete this row (saved to graveyard)';
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
  $('dbModalHint').textContent = isWrite
    ? 'The new row lands in this table immediately — timestamps are set by the database.'
    : 'Only changed fields are written, one guarded UPDATE per field.';
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
      DbState.lastSqlResult = null; $('sqlCopyLast').hidden = true;
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
      DbState.lastSqlResult = null; $('sqlCopyLast').hidden = true;
      dbRefreshAll();
      return;
    }
    DbState.lastSqlResult = d;
    $('sqlCopyLast').hidden = !(d.columns && d.columns.length);
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
    btn.innerHTML = IC.play;
  }
}

/* ── backups panel ── */
async function dbBkLoad(){
  const list = $('dbBackupList');
  $('dbBkStatus').textContent = 'reading backup state…';
  try {
    const d = await dbApi('/api/db/backups');
    $('dbBkStatus').innerHTML =
      'Every <b>' + d.interval_days + ' days</b>, all five tables are dumped to ' +
      '<code class="mono">backups/&lt;stamp&gt;/</code> as JSON + replayable SQL · ' +
      'keeping the newest <b>' + d.keep + '</b> sets · next in <b>' + dbEsc(String(d.next_due)) + '</b>';
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
    btn.innerHTML = IC.backup;
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
  if (name === 'backups') dbBkLoad();
}

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
  $('sqlCopyLast').onclick = () => {
    const r = DbState.lastSqlResult;
    if (r && r.columns.length) dbToCsv(r.columns, r.rows);
  };

  $('dbBackupNow').onclick = dbBackupNow;

  $('dbModalClose').onclick = dbCloseModal;
  $('dbModalSave').onclick = dbSaveModal;
  $('dbModal').addEventListener('click', e => { if (e.target === $('dbModal')) dbCloseModal(); });
  document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && !$('dbModal').hidden) dbCloseModal();
  });
}
