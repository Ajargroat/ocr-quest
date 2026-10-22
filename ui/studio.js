/* ═══════════ STUDIO · any-file OCR subsystem ═══════════
   The whole chain is live: upload → probe → profile → extract (layer1) →
   enrich (layer2) → locate crops → human review → import. Bulk reads the
   merged view, Crops is the review desk (bbox + fields + approve/reject),
   Import writes whatever is ready to the chosen target.

   Loaded after app.js, so it reuses dbApi / dbEsc / toast / viewLoading and
   the WS dispatcher's `studioFeed` hook. */
const StudioState = { loaded:false, wired:false, jobs:[], job:null, detail:null,
                      tags:null, tagDraft:null, models:null, bulk:null, block:null,
                      tab:'jobs', cropPage:null, drawerKey:null, organize:'auto',
                      tagScope:'corps', tagSubject:'', tagGrade:'' };
const ST_KEEP = '__KEEP__';   // same sentinel the credentials tab sends (main.py KEEP)

const studioApi = dbApi;                 // same fetch+json contract
const studioJson = (url, body) => studioApi(url, {
  method:'POST', headers:{'content-type':'application/json'},
  body: JSON.stringify(body),
});
const stEsc = dbEsc;

/* Studio numbers are English (UI rule): counters, pages, limits and dates render
   with Latin digits. Only OCR'd question/answer text keeps its own numerals. */
const stNum = v => String(v == null ? '' : v);

const ST_COMPILATION = [
  'unknown','single_institute_multi_year','multi_institute_single_year',
  'multi_institute_multi_year','single_subject_multi_chapter','multi_subject',
];
const ST_KIND = ['unknown','text_pdf','image_pdf','mixed','images'];
const ST_ANSWER = ['unknown','inline','separate_section','key_pages_at_end','none'];
const ST_FIELDS = ['subject','corp','year','grade','topic','difficulty'];

function stFmt(iso){
  if (!iso) return '—';
  const d = new Date(iso);
  return isNaN(d) ? String(iso) : d.toLocaleString('en-US',
    { year:'numeric', month:'short', day:'2-digit', hour:'2-digit', minute:'2-digit' });
}

async function studioInit(){
  studioWireOnce();
  try {
    await Promise.all([studioLoadJobs(), studioLoadTags(), studioLoadModels()]);
    studioRenderTags();
    studioRenderModels();
  } catch (e){
    $('stJobs').innerHTML = '<div class="db-empty db-err">Studio unreachable — '
      + stEsc(e.message) + '</div>';
  }
  StudioState.loaded = true;     // the live feed and #studio/<tab> now apply
}

function studioWireOnce(){
  if (StudioState.wired) return;
  StudioState.wired = true;

  document.querySelectorAll('#stTabs .rail-btn').forEach(b =>
    b.onclick = () => studioPanel(b.dataset.sttab));
  $('stCropDrawerClose').onclick = () => stCropDrawerClose();
  $('stJobModalClose').onclick = () => stJobClose();
  $('stJobModal').onclick = e => { if (e.target === $('stJobModal')) stJobClose(); };
  $('stJobTabs').querySelectorAll('.rail-btn').forEach(b => b.onclick = () => {
    StJob.tab = b.dataset.stjob;
    stJobRender();
  });
  $('stJobSave').onclick = () => stJobSave();
  stEditWireOnce();
  $('stRefresh').onclick = () => {
    studioLoadJobs();
    studioLoadBulk();
    studioRenderTags();
    studioRenderModels();
  };
  $('stFile').onchange = e => {
    const f = e.target.files && e.target.files[0];
    if (f) studioUpload(f);
    e.target.value = '';
  };
  stLogWire();
}

/* stage tabs · exactly one pane is visible; #studio/<tab> keeps the deep link
   working (app.js panelFromHash) */
const ST_TABS = ['jobs', 'profile', 'bulk', 'crops', 'inspect', 'tags',
                 'models', 'import'];

function studioPanel(name, silent){
  if (!ST_TABS.includes(name)) name = 'jobs';
  StudioState.tab = name;
  document.querySelectorAll('#stTabs .rail-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sttab === name));
  document.querySelectorAll('.st-pane').forEach(p =>
    p.hidden = p.dataset.sttab !== name);
  if (name === 'tags') studioRenderTags();
  if (name === 'models') studioRenderModels();
  if (name === 'bulk' || name === 'crops' || name === 'import'
      || name === 'inspect') studioLoadBulk();
  if (name === 'profile') studioRenderProfile();
  if (!silent) history.replaceState(null, '', '#studio/' + name);
}

/* ── Jobs panel ── */
async function studioLoadJobs(){
  const d = await studioApi('/api/studio/jobs');
  StudioState.jobs = d.jobs || [];
  studioRenderJobs();
  // First visit with jobs on disk: load the newest one's detail (rail + crumb)
  // but STAY on the Jobs panel — only an explicit open switches to Profile.
  if (!StudioState.job && StudioState.jobs.length)
    studioSelectJob(StudioState.jobs[0].id, { keepPanel: true });
}

/* Which artifacts a row shows as icons, and which pop-up tab each opens.
   Profile / Extract / Crops / Models — one file at a time, own model each. */
const ST_ART = [
  ['profile', 'fa-id-card', 'profile'],
  ['layer1', 'fa-layer-group', 'extract'],
  ['layer2', 'fa-list-check', 'extract'],
  ['geometry', 'fa-crop-simple', 'crops'],
  ['review', 'fa-eye', 'crops'],
  ['imports', 'fa-database', 'extract'],
];

function studioRenderJobs(){
  const wrap = $('stJobs');
  const jobs = StudioState.jobs;
  if (!jobs.length){
    wrap.innerHTML = '';
    return;
  }
  const rows = jobs.map(j => {
    const art = j.artifacts || {};
    const icons = ST_ART.map(([key, icon, tab]) =>
      '<button type="button" class="st-art-i' + (art[key] ? ' on' : '')
      + '" data-job="' + stEsc(j.id) + '" data-jobtab="' + tab + '"'
      + ' title="' + stEsc(key) + '" aria-label="' + stEsc(key) + '">'
      + '<i class="fa ' + icon + '" aria-hidden="true"></i></button>').join('');
    const active = StudioState.job === j.id ? ' active' : '';
    return '<tr class="st-row' + active + '" data-job="' + stEsc(j.id) + '">'
      + '<td><b>' + stEsc(j.name) + '</b><span class="st-sub">' + stEsc(j.id) + '</span></td>'
      + '<td>' + (j.kind ? '<span class="chip s-ocr">' + stEsc(j.kind) + '</span>' : '—') + '</td>'
      + '<td>' + stEsc(j.subject || '—') + '</td>'
      + '<td>' + stEsc((j.compilation || '—').replace(/_/g, ' ')) + '</td>'
      + '<td class="st-art-cell">' + icons + '</td>'
      + '<td>' + (j.blocks != null ? stNum(j.blocks) : '—') + '</td>'
      + '<td>' + stFmt(j.created_at) + '</td>'
      + '<td class="st-row-actions">'
      +   '<button class="ghost-btn neutral sm st-open" data-job="' + stEsc(j.id) + '" title="Open profile">open</button>'
      +   '<button class="ghost-btn neutral sm st-del" data-job="' + stEsc(j.id) + '" title="Delete job">delete</button>'
      + '</td></tr>';
  }).join('');
  wrap.innerHTML = '<table class="st-table"><thead><tr>'
    + '<th>Job</th><th>Kind</th><th>Subject</th><th>Compilation</th>'
    + '<th>Artifacts</th><th>Blocks</th><th>Created</th><th></th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table>';

  wrap.querySelectorAll('.st-open').forEach(b => b.onclick = e => {
    e.stopPropagation(); studioSelectJob(b.dataset.job);
  });
  wrap.querySelectorAll('.st-del').forEach(b => b.onclick = e => {
    e.stopPropagation(); studioDelete(b.dataset.job);
  });
  wrap.querySelectorAll('[data-jobtab]').forEach(b => b.onclick = e => {
    e.stopPropagation(); stJobOpen(b.dataset.job, b.dataset.jobtab);
  });
  wrap.querySelectorAll('.st-row').forEach(tr => tr.onclick = () => {
    studioSelectJob(tr.dataset.job);
  });
}

/* ── one file's own pop-up · the row's artifact icons ──
   profile · extract (layer1/layer2 + stage runs) · crops (review state) ·
   models (this FILE's provider + model, independent of the next file). */
const StJob = { job: null, tab: 'profile', data: null };

function stJobOpen(job, tab){
  StJob.job = job;
  StJob.tab = tab || 'profile';
  $('stJobModal').hidden = false;
  stJobLoad();
}

function stJobClose(){ $('stJobModal').hidden = true; StJob.job = null; }

async function stJobLoad(){
  const job = StJob.job;
  if (!job) return;
  try {
    StJob.data = await studioApi('/api/studio/jobs/' + encodeURIComponent(job) + '/lane');
  } catch (e){ StJob.data = { error: e.message }; }
  stJobRender();
}

function stJobPaintTabs(){
  document.querySelectorAll('#stJobTabs .rail-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.stjob === StJob.tab));
}

function stJobRender(){
  const d = StJob.data || {};
  const body = $('stJobModalBody');
  const err = $('stJobErr');
  if (err) err.hidden = true;
  stJobPaintTabs();
  const job = d.job || {};
  $('stJobModalTitle').textContent = job.name || StJob.job || '';
  $('stJobSave').hidden = StJob.tab !== 'models';
  if (d.error){
    body.innerHTML = '<div class="db-empty db-err">' + stEsc(d.error) + '</div>';
    return;
  }
  if (StJob.tab === 'profile') body.innerHTML = stJobProfileHtml(d);
  else if (StJob.tab === 'extract') body.innerHTML = stJobExtractHtml(d);
  else if (StJob.tab === 'crops') body.innerHTML = stJobCropsHtml(d);
  else body.innerHTML = stJobModelsHtml(d);
  stJobWire();
}

function stJobProfileHtml(d){
  const job = d.job || {};
  const stats = [['kind', job.kind], ['compilation', job.compilation],
    ['subject', job.subject], ['blocks', job.blocks], ['figures', job.figures],
    ['imported', job.imported], ['created', stFmt(job.created_at)]];
  const art = job.artifacts || {};
  return '<div class="st-job-grid">'
    + '<div class="st-col">' + (stStats(stats) || '') + '</div>'
    + '<div class="st-col"><div class="st-art-cell">' + ST_ART.map(([key, icon]) =>
        '<span class="st-art ' + (art[key] ? 'on' : 'off') + '">'
        + '<i class="fa ' + icon + '" aria-hidden="true"></i> ' + stEsc(key) + '</span>').join('')
    + '</div></div></div>'
    + '<div class="st-job-foot">'
    + '<button class="ghost-btn neutral sm" data-job-run="probe" type="button">re-probe</button>'
    + '<button class="ghost-btn neutral sm" data-job-open="profile" type="button">full profile</button>'
    + '</div>';
}

function stJobExtractHtml(d){
  const blocks = d.blocks || [];
  const stats = d.stats || {};
  const stages = d.stages || {};
  const lane = d.lane || {};
  const counts = [['blocks', blocks.length],
    ['questions', blocks.filter(b => b.kind === 'question').length],
    ['answers', blocks.filter(b => b.kind === 'answer_key_entry').length],
    ['importable', blocks.filter(b => b.importable).length],
    ['stat calls', stats.calls]];
  const stageRow = (stage, label) => {
    const s = stages[stage] || {};
    return '<tr><td>' + stEsc(label) + '</td><td>' + stEsc(s.label || '—') + '</td>'
      + '<td class="mono">' + stEsc(s.model || (s.provider ? '—' : 'all')) + '</td></tr>';
  };
  const rows = blocks.map(b =>
    '<tr><td class="mono">' + stEsc(b.key || '') + '</td>'
    + '<td>' + stEsc(b.kind || '') + '</td>'
    + '<td class="mono">' + stEsc(b.number != null ? b.number : '—') + '</td>'
    + '<td class="mono">' + stEsc(b.page != null ? b.page : '—') + '</td>'
    + '<td>' + stStatusChip(b.status) + '</td>'
    + '<td>' + (b.picture ? '<span class="st-art ' + (b.picture_ok ? 'on' : 'off') + '">picture</span>' : '—') + '</td>'
    + '<td>' + (b.importable ? '<span class="st-art on">ready</span>'
        : '<span class="st-art off" title="' + stEsc(b.why) + '">blocked</span>') + '</td></tr>').join('');
  return '<div class="st-col">' + (stStats(counts) || '')
    + '<div class="st-job-lane">' + '<b>' + stEsc(lane.provider_label || 'stage default') + '</b>'
    + '<span class="st-sub">this file' + (lane.prompt ? ' · own prompt' : '') + '</span></div>'
    + '<div class="st-table-wrap st-job-table"><table class="st-table"><thead><tr>'
    + '<th>Stage</th><th>source</th><th>model</th></tr></thead><tbody>'
    + stageRow('extract', 'extract') + stageRow('enrich', 'enrich') + stageRow('vision', 'locate')
    + '</tbody></table></div>'
    + (rows ? '<div class="st-table-wrap st-job-table"><table class="st-table"><thead><tr>'
      + '<th>Block</th><th>kind</th><th>#</th><th>page</th><th>status</th><th>picture</th><th>import</th>'
      + '</tr></thead><tbody>' + rows + '</tbody></table></div>' : '')
    + '<div class="st-job-foot">'
    + '<button class="ghost-btn neutral sm" data-job-run="extract" type="button">Extract</button>'
    + '<button class="ghost-btn neutral sm" data-job-run="enrich" type="button">Enrich</button>'
    + '<button class="ghost-btn neutral sm" data-job-run="geometry" type="button">Locate</button>'
    + '</div></div>';
}

function stJobCropsHtml(d){
  const blocks = (d.blocks || []).filter(b => b.picture);
  const counts = [['pictures', blocks.length],
    ['approved', blocks.filter(b => b.picture_ok).length],
    ['pending', blocks.filter(b => !b.picture_ok).length]];
  const rows = blocks.map(b =>
    '<tr><td class="mono">' + stEsc(b.key || '') + '</td>'
    + '<td class="mono">' + stEsc(b.number != null ? b.number : '—') + '</td>'
    + '<td class="mono">' + stEsc(b.page != null ? b.page : '—') + '</td>'
    + '<td>' + (b.picture_ok ? '<span class="st-art on">approved</span>'
        : '<span class="st-art off">review</span>') + '</td>'
    + '<td><button class="ghost-btn neutral sm" data-job-crop="' + stEsc(b.key) + '" type="button">'
    + 'picture</button></td></tr>').join('');
  return '<div class="st-col">' + (stStats(counts) || '')
    + (rows ? '<div class="st-table-wrap st-job-table"><table class="st-table"><thead><tr>'
      + '<th>Block</th><th>#</th><th>page</th><th>state</th><th></th></tr></thead><tbody>'
      + rows + '</tbody></table></div>' : '')
    + '<div class="st-job-foot">'
    + '<button class="ghost-btn neutral sm" data-job-panel="crops" type="button">crops desk</button>'
    + '<button class="ghost-btn neutral sm" data-job-panel="inspect" type="button">inspect</button>'
    + '</div></div>';
}

function stJobModelsHtml(d){
  const lane = d.lane || {};
  const stages = d.stages || {};
  const sources = d.sources || [];
  const pool = [['', 'Gemini pool']].concat(sources.map(p => [p.label, p.label]));
  const models = {};
  sources.forEach(p => {
    const stage = Object.values(stages).find(s => s.provider === p.label);
    models[p.label] = p.models.length ? p.models : (stage && stage.model ? [stage.model] : []);
  });
  const cur = lane.provider || '';
  const pick = models[cur] || [];
  const srcOpts = pool.map(([v, l]) => '<option value="' + stEsc(v) + '"'
    + (v === cur ? ' selected' : '') + '>' + stEsc(l) + '</option>').join('');
  const modelOpts = srcOpts ? (cur ? pick.map(m => '<option value="' + stEsc(m) + '"'
      + (m === (stages.extract || {}).model ? ' selected' : '') + '>' + stEsc(m) + '</option>').join('')
      : '<option value="">all</option>') : '';
  const stageRow = (stage, label) => {
    const s = stages[stage] || {};
    return '<label class="st-field"><span>' + stEsc(label) + '</span>'
      + '<input type="text" dir="ltr" readonly value="'
      + stEsc((s.label || 'Gemini pool') + (s.model ? ' · ' + s.model : '')) + '"></label>';
  };
  return '<div class="st-col">'
    + '<label class="st-field"><span>this file runs on</span>'
    + '<select id="stJobSource" data-job-source>' + srcOpts + '</select></label>'
    + '<label class="st-field"><span>model</span>'
    + '<select id="stJobModel" data-job-model>' + modelOpts + '</select></label>'
    + '<label class="st-field"><span>own prompt · spare</span>'
    + '<textarea data-autofit rows="2" dir="auto" data-job-prompt>'
    + stEsc(lane.prompt || '') + '</textarea></label>'
    + '</div>'
    + '<div class="st-col">'
    + stageRow('extract', 'extract') + stageRow('enrich', 'enrich') + stageRow('vision', 'locate')
    + '</div>';
}

function stJobWire(){
  const body = $('stJobModalBody');
  body.querySelectorAll('[data-job-run]').forEach(b => b.onclick = async () => {
    await studioSelectJob(StJob.job, { keepPanel: true });
    await studioRunStage(b.dataset.jobRun, b.dataset.jobRun);
    stJobLoad();
  });
  body.querySelectorAll('[data-job-open]').forEach(b => b.onclick = () => {
    const job = StJob.job;
    stJobClose();
    studioSelectJob(job).then(() => studioPanel(b.dataset.jobOpen));
  });
  body.querySelectorAll('[data-job-panel]').forEach(b => b.onclick = () => {
    const job = StJob.job, panel = b.dataset.jobPanel;
    stJobClose();
    studioSelectJob(job, { keepPanel: true }).then(() => studioPanel(panel));
  });
  body.querySelectorAll('[data-job-crop]').forEach(b => b.onclick = () => {
    stJobClose();
    stCropDrawerOpen(b.dataset.jobCrop);
  });
  const src = body.querySelector('[data-job-source]');
  if (src) src.onchange = () => stJobModelRefresh();
  stAutofit(body);
}

function stJobModelRefresh(){
  const body = $('stJobModalBody');
  const d = StJob.data || {};
  const src = body.querySelector('[data-job-source]');
  const sel = body.querySelector('[data-job-model]');
  if (!src || !sel) return;
  const label = src.value;
  const provider = (d.sources || []).find(p => p.label === label);
  const stage = Object.values(d.stages || {}).find(s => s.provider === label);
  const models = label ? (provider && provider.models.length ? provider.models
    : (stage && stage.model ? [stage.model] : [])) : [];
  sel.innerHTML = label
    ? (models.length ? models.map(m => '<option value="' + stEsc(m) + '">' + stEsc(m) + '</option>').join('')
                    : '<option value="">—</option>')
    : '<option value="">all</option>';
}

async function stJobSave(){
  const body = $('stJobModalBody');
  const err = $('stJobErr');
  const src = body.querySelector('[data-job-source]');
  const model = body.querySelector('[data-job-model]');
  const prompt = body.querySelector('[data-job-prompt]');
  if (!src) return;
  try {
    const d = await studioJson('/api/studio/jobs/' + encodeURIComponent(StJob.job) + '/lane', {
      provider: src.value,
      prompt: prompt ? prompt.value : '',
    });
    if (d.ok === false) throw new Error(d.error || 'save failed');
    if (src.value && model && model.value){
      StudioState.models = await studioJson('/api/studio/models/pick', {
        label: src.value, model: model.value });
    }
    StJob.data = d;
    stJobRender();
    toast('File lane saved.');
  } catch (e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Save failed: ' + e.message);
  }
}

async function studioSelectJob(id, opts){
  StudioState.job = id;
  studioRenderJobs();
  const d = await studioApi('/api/studio/jobs/' + encodeURIComponent(id));
  StudioState.detail = d;
  $('stCrumb').textContent = d.job ? d.job.name : id;
  studioRenderProfile();
  studioLoadBulk();
  stSetRail(d.job);
  if (ST_LOG.job !== id) stLogLoad(id);
  if (!(opts && opts.keepPanel)) studioPanel('profile');
}

function stSetRail(job){
  if (!job) return;
  const art = job.artifacts || {};
  const map = { intake: true, probe: !!art.profile, extract: !!art.layer1,
                enrich: !!art.layer2, geometry: !!art.geometry,
                review: !!art.review, import: !!art.imports };
  Object.entries(map).forEach(([stage, done]) => {
    const el = document.querySelector('#stStages .stage[data-stage="' + stage + '"]');
    if (el) el.classList.toggle('done', done);
  });
  $('stNowStage').textContent = (job.kind || 'IDLE').toUpperCase();
  $('stNowMsg').textContent = job.name + ' — ' + (job.compilation || 'unknown')
    + (job.blocks != null ? ' · ' + stNum(job.blocks) + ' blocks' : '');
  $('stNow').dataset.state = job.kind ? 'done' : 'idle';
}

/* ── Profile panel ── */
/* themed stat chips: pairs of [label, value, tone] — counts never render as
   bare plain text (tone: '' | 'ok' | 'warn' | 'err' | 'plain') */
function stStats(pairs){
  const chips = (pairs || []).filter(p => p && p[1] !== '' && p[1] != null)
    .map(p => '<span class="st-stat' + (p[2] ? ' ' + p[2] : '') + '">'
      + '<em>' + stEsc(p[0]) + '</em><b>' + stEsc(String(p[1])) + '</b></span>').join('');
  return chips ? '<span class="st-stats">' + chips + '</span>' : '';
}

function studioRenderProfile(){
  const d = StudioState.detail;
  const body = $('stProfileBody');
  if (!d || !d.profile){
    body.innerHTML = '';
    return;
  }
  const p = d.profile, st = p.static || {}, pr = d.probe || {};

  const opt = (list, cur) => list.map(v =>
    '<option value="' + stEsc(v) + '"' + (v === cur ? ' selected' : '') + '>'
    + stEsc(v.replace(/_/g,' ')) + '</option>').join('');

  const fieldRow = f => {
    const pinned = st[f] != null && st[f] !== '';
    return '<label class="st-field' + (pinned ? ' pinned' : '') + '">'
      + '<span>' + f + (pinned ? ' · pinned' : '') + '</span>'
      + '<input type="text" data-field="' + f + '" value="' + stEsc(st[f] || '') + '">'
      + '</label>';
  };

  const probeStats = pr && pr.ok
    ? stStats([['pages', pr.page_count],
               ['text', pr.text_pages || 0],
               ['image', pr.image_pages || 0],
               ['answer-key', (pr.answer_key_pages || []).length],
               ['figures', (pr.figure_pages || []).length]])
    : stStats([['probe', 'not run', 'plain']]);

  body.innerHTML =
    '<div class="st-profile-grid">'
    + '<div class="st-col">'
    +   '<h3>What the file is</h3>'
    +   '<label class="st-field"><span>source kind</span>'
    +     '<select id="stKind">' + opt(ST_KIND, p.source_kind) + '</select></label>'
    +   '<label class="st-field"><span>compilation</span>'
    +     '<select id="stComp">' + opt(ST_COMPILATION, p.compilation) + '</select></label>'
    +   '<label class="st-field"><span>answer layout</span>'
    +     '<select id="stAnswer">' + opt(ST_ANSWER, p.answer_layout) + '</select></label>'
    +   probeStats
    + '</div>'
    + '<div class="st-col">'
    +   '<h3>Static fields</h3>'
    +   ST_FIELDS.map(fieldRow).join('')
    + '</div></div>'
    + '<div class="st-profile-foot">'
    + '<button class="ghost-btn neutral" id="stReProbe" type="button">re-probe</button>'
    + '<button class="ghost-btn neutral" id="stSaveProfile" type="button">Save profile</button>'
    + '<button class="ghost-btn neutral" id="stExtract" type="button">Extract</button>'
    + '</div>';

  $('stSaveProfile').onclick = () => studioSaveProfile();
  $('stReProbe').onclick = () => studioReprobe();
  $('stExtract').onclick = () => studioRunStage('extract', 'Extract')
    .then(() => studioPanel('bulk'));
}

async function studioSaveProfile(){
  const id = StudioState.job;
  if (!id) return;
  const staticFields = {};
  document.querySelectorAll('#stProfileBody [data-field]').forEach(inp => {
    staticFields[inp.dataset.field] = inp.value.trim() || null;
  });
  const overrides = {
    source_kind: $('stKind').value,
    compilation: $('stComp').value,
    answer_layout: $('stAnswer').value,
    static: staticFields,
  };
  try {
    const d = await studioJson('/api/studio/jobs/' + encodeURIComponent(id) + '/profile', overrides);
    StudioState.detail.profile = d.profile;
    StudioState.detail.required_missing = d.required_missing;
    if (d.job) StudioState.jobs = StudioState.jobs.map(j => j.id === id ? d.job : j);
    studioRenderProfile();
    studioRenderJobs();
    toast('Profile saved.');
  } catch (e){ toast('Save failed: ' + e.message); }
}

async function studioReprobe(){
  const id = StudioState.job;
  if (!id) return;
  try {
    await studioApi('/api/studio/jobs/' + encodeURIComponent(id) + '/probe', { method:'POST' });
    await studioSelectJob(id);
    toast('Re-probed.');
  } catch (e){ toast('Re-probe failed: ' + e.message); }
}

/* ── upload ── */
async function studioUpload(file){
  $('stUploadLabel').textContent = 'Uploading ' + file.name + '…';
  const fd = new FormData();
  fd.append('file', file, file.name);
  try {
    const res = await fetch('/api/studio/jobs', { method:'POST', body: fd });
    const d = await res.json().catch(() => ({}));
    if (!res.ok || !d.ok) throw new Error(d.error || res.status + ' ' + res.statusText);
    toast('Job created: ' + (d.job ? d.job.name : 'ok'));
    await studioLoadJobs();
    if (d.job) studioSelectJob(d.job.id);
  } catch (e){
    stLogFail('upload failed: ' + e.message);
    toast('Upload failed: ' + e.message);
  } finally {
    $('stUploadLabel').textContent = 'Choose a PDF or image…';
  }
}

async function studioDelete(id){
  if (!confirm('Delete job ' + id + ' and all its artifacts?')) return;
  try {
    await fetch('/api/studio/jobs/' + encodeURIComponent(id), { method:'DELETE' });
    if (StudioState.job === id){ StudioState.job = null; StudioState.detail = null;
      $('stCrumb').textContent = ''; studioRenderProfile(); }
    if (ST_LOG.job === id) stLogReset(null);
    await studioLoadJobs();
    toast('Job deleted.');
  } catch (e){ toast('Delete failed: ' + e.message); }
}

/* ── Tags tab (global editor — one shared list for every job) ── */
async function studioLoadTags(){
  try {
    const d = await studioApi('/api/studio/tags');
    StudioState.tags = d;
    StudioState.tagDraft = d.tags ? JSON.parse(JSON.stringify(d.tags)) : null;
  } catch (e){ StudioState.tags = { error: e.message }; StudioState.tagDraft = null; }
}

const ST_TAG_SCOPES = [['corps', 'Institutes'], ['subjects', 'Subjects'], ['lessons', 'Lessons']];

function stTagSelectHtml(attr, pairs, value, aria){
  return '<select class="st-sel" ' + attr + ' aria-label="' + aria + '">'
    + pairs.map(p => '<option value="' + stEsc(p[0]) + '"'
        + (String(p[0]) === String(value) ? ' selected' : '') + '>' + stEsc(p[1]) + '</option>').join('')
    + '</select>';
}

function stTagRowShell(inner){
  return '<div class="st-tagrow">' + inner + '</div>';
}

function studioRenderTags(){
  const body = $('stTagsBody');
  const draft = StudioState.tagDraft;
  if (!draft){
    const err = (StudioState.tags && StudioState.tags.error) || '';
    body.innerHTML = err ? '<div class="db-empty db-err">' + stEsc(err) + '</div>' : '';
    return;
  }
  ['subjects', 'corps', 'chapters'].forEach(k => { if (!Array.isArray(draft[k])) draft[k] = []; });
  if (ST_TAG_SCOPES.every(p => p[0] !== StudioState.tagScope)) StudioState.tagScope = 'corps';

  const lessons = StudioState.tagScope === 'lessons';
  const subjects = Array.from(new Set(draft.subjects.filter(Boolean)));
  const grades = Array.from(new Set(draft.chapters.map(c => c && c.grade).filter(Boolean)));

  body.innerHTML =
    '<div class="st-tags-bar">'
    + stTagSelectHtml('data-tag-scope', ST_TAG_SCOPES, StudioState.tagScope, 'content')
    + (lessons ? stTagSelectHtml('data-tag-subject',
        [['', 'All subjects']].concat(subjects.map(s => [s, s])),
        StudioState.tagSubject, 'subject') : '')
    + (lessons ? stTagSelectHtml('data-tag-grade',
        [['', 'All grades']].concat(grades.map(g => [g, g])),
        StudioState.tagGrade, 'grade') : '')
    + '<div class="st-actions">'
    +   '<label class="st-upload"><i class="fa fa-file-arrow-up" aria-hidden="true"></i><span>Upload</span>'
    +     '<input type="file" data-tags-file accept=".json,application/json"></label>'
    +   '<button class="ghost-btn neutral" data-tags-save type="button">Save</button>'
    + '</div></div>'
    + '<div class="st-taglist" id="stTagList"></div>'
    + '<span class="st-warn" data-tags-err hidden></span>';

  /* the pickers are real <select>s, like the Models stage pickers: a change
     re-renders the rows below, nothing else is rebuilt */
  const picker = attr => body.querySelector('[' + attr + ']');
  const scope = picker('data-tag-scope');
  scope.onchange = () => {
    StudioState.tagScope = scope.value || 'corps';
    if (StudioState.tagScope !== 'lessons'){ StudioState.tagSubject = ''; StudioState.tagGrade = ''; }
    studioRenderTags();
  };
  const subjPick = picker('data-tag-subject');
  if (subjPick) subjPick.onchange = () => { StudioState.tagSubject = subjPick.value; studioRenderTags(); };
  const gradePick = picker('data-tag-grade');
  if (gradePick) gradePick.onchange = () => { StudioState.tagGrade = gradePick.value; studioRenderTags(); };

  const list = $('stTagList');
  if (lessons){
    list.innerHTML = stTagLessonsHtml(draft)
      + '<div><button class="ghost-btn neutral sm" data-tags-add="chapters" type="button">add</button></div>';
  } else if (StudioState.tagScope === 'subjects'){
    list.innerHTML = draft.subjects.map((s, i) =>
      stTagRowShell('<input class="st-rtl" type="text" dir="rtl" data-subj="' + i + '" value="'
        + stEsc(s || '') + '" aria-label="subject">'
        + '<button class="ghost-btn neutral sm" data-del-subj="' + i + '" type="button">remove</button>'))
      + '<div><button class="ghost-btn neutral sm" data-tags-add="subjects" type="button">add</button></div>';
  } else {
    list.innerHTML = draft.corps.map((c, i) =>
      stTagRowShell('<input class="st-rtl" type="text" dir="rtl" data-ctag="' + i + '" value="'
        + stEsc((c && c.tag) || '') + '" aria-label="canonical">'
        + '<input class="st-rtl" type="text" dir="rtl" data-cvar="' + i + '" value="'
        + stEsc(((c && c.variants) || []).join(', ')) + '" aria-label="variants">'
        + '<button class="ghost-btn neutral sm" data-del-corp="' + i + '" type="button">remove</button>'))
      + '<div><button class="ghost-btn neutral sm" data-tags-add="corps" type="button">add</button></div>';
  }

  list.oninput = e => stTagEdit(e.target, draft);
  list.onclick = e => {
    const b = e.target.closest('button');
    if (!b || !list.contains(b)) return;
    if (b.hasAttribute('data-del-subj')){ draft.subjects.splice(+b.dataset.delSubj, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-del-corp')){ draft.corps.splice(+b.dataset.delCorp, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-del-ch')){ draft.chapters.splice(+b.dataset.delCh, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-tags-add')){
      const what = b.dataset.tagsAdd;
      if (what === 'subjects') draft.subjects.push('');
      else if (what === 'corps') draft.corps.push({ tag: '', variants: [] });
      else draft.chapters.push({ subject: StudioState.tagSubject || '',
                                 grade: StudioState.tagGrade || '',
                                 chapter: null, topics: [] });
      studioRenderTags();
    }
  };
  body.onclick = e => {
    const b = e.target.closest('button');
    if (!b || !body.contains(b)) return;
    if (b.hasAttribute('data-tags-save')) studioSaveTags();
  };
  const file = body.querySelector('[data-tags-file]');
  if (file) file.onchange = e => {
    const f = e.target.files && e.target.files[0];
    if (f) studioUploadTags(f);
    e.target.value = '';
  };
}

/* lessons = the chapter/topic rows, narrowed by the two dropdowns beside the
   scope picker; every row keeps its original index so a hidden group is never
   rewritten by a save. */
function stTagLessonsHtml(draft){
  const want = c => c && (!StudioState.tagSubject || c.subject === StudioState.tagSubject)
    && (!StudioState.tagGrade || c.grade === StudioState.tagGrade);
  return draft.chapters.map((c, i) => !want(c) ? '' :
    '<div class="st-tagblock" data-ch="' + i + '">'
    + stTagRowShell('<input class="st-rtl" type="text" dir="rtl" data-hsubj="' + i + '" value="'
      + stEsc(c.subject || '') + '" aria-label="subject">'
      + '<input class="st-rtl" type="text" dir="rtl" data-hgrade="' + i + '" value="'
      + stEsc(c.grade || '') + '" aria-label="grade">'
      + '<input class="st-num" type="text" data-hnum="' + i + '" value="'
      + stEsc(c.chapter == null ? '' : String(c.chapter)) + '" aria-label="chapter">'
      + '<button class="ghost-btn neutral sm" data-del-ch="' + i + '" type="button">remove</button>')
    + '<div class="st-tagrow"><textarea class="st-rtl" dir="rtl" rows="2" data-htop="' + i
      + '" aria-label="topics">' + stEsc((c.topics || []).join('\n')) + '</textarea></div>'
    + '</div>').join('');
}

/* inputs write straight into the draft, so filtering the list can never drop
   values that a still-visible row did not carry. */
function stTagEdit(el, draft){
  const d = el.dataset;
  if (d.subj != null) draft.subjects[+d.subj] = el.value;
  else if (d.ctag != null) draft.corps[+d.ctag].tag = el.value;
  else if (d.cvar != null) draft.corps[+d.cvar].variants =
    el.value.split(',').map(s => s.trim()).filter(Boolean);
  else if (d.hsubj != null) draft.chapters[+d.hsubj].subject = el.value;
  else if (d.hgrade != null) draft.chapters[+d.hgrade].grade = el.value;
  else if (d.hnum != null){
    const v = stEnDigits(el.value).trim();
    draft.chapters[+d.hnum].chapter = v === '' ? null : Number(v);
  }
  else if (d.htop != null) draft.chapters[+d.htop].topics =
    el.value.split('\n').map(s => s.trim()).filter(Boolean);
}

async function studioSaveTags(){
  const err = document.querySelector('#stTagsBody [data-tags-err]');
  try {
    const d = await studioJson('/api/studio/tags', { tags: StudioState.tagDraft });
    StudioState.tags = d;
    StudioState.tagDraft = JSON.parse(JSON.stringify(d.tags));
    studioRenderTags();
    toast('Tag list saved.');
  } catch (e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Save failed: ' + e.message);
  }
}

async function studioUploadTags(file){
  const err = document.querySelector('#stTagsBody [data-tags-err]');
  const fail = msg => {
    if (err){ err.hidden = false; err.textContent = msg; }
    else toast(msg);
  };
  try {
    const doc = JSON.parse(await file.text());
    if (!doc || !Array.isArray(doc.subjects) || !Array.isArray(doc.corps))
      throw new Error('Not a tag list: needs subjects[] and corps[].');
    StudioState.tagDraft = doc;
    studioRenderTags();
    toast('File loaded — press Save to apply.');
  } catch (e){ fail('Upload failed: ' + e.message); }
}

/* ── Models panel (studio-local providers + per-stage picks + proxy) ── */
async function studioLoadModels(){
  try {
    const d = await studioApi('/api/studio/models');
    StudioState.models = d;
  } catch (e){ StudioState.models = { error: e.message }; }
}

/* each provider owns a MODEL LIST; the model select picks its default, and a
   single file can override that pick in its own row pop-up (Models tab) */
function studioRenderModels(){
  const body = $('stModelsBody');
  const m = StudioState.models;
  if (!m || m.error || !m.providers){
    body.innerHTML = m && m.error ? '<div class="db-empty db-err">' + stEsc(m.error) + '</div>' : '';
    return;
  }
  const rows = (m.providers || []).map((p, i) => {
    const models = (p.models || []).slice();
    if (p.model && !models.includes(p.model)) models.unshift(p.model);
    const opts = models.map(v => '<option value="' + stEsc(v) + '"'
      + (v === (p.model || '') ? ' selected' : '') + '>' + stEsc(v) + '</option>').join('');
    return '<div class="st-prov" data-prov="' + i + '">'
      + '<div class="st-prov-head"><b>' + stEsc(p.label || '') + '</b>'
      + '<span class="st-art ' + (p.api_key_set ? 'on' : 'off') + '">'
      + (p.api_key_set ? 'key set' : 'no key') + '</span>'
      + '<span class="st-gap"></span>'
      + '<button class="ghost-btn neutral sm" data-del-prov="' + i + '" type="button">remove</button></div>'
      + '<label class="st-field"><span>api key</span>'
      + '<input type="password" data-mkey="' + i + '" value="" autocomplete="off"></label>'
      + '<label class="st-field"><span>models · one per line</span>'
      + '<textarea data-autofit dir="ltr" rows="1" data-mmodels="' + i + '">'
      + stEsc(models.join('\n')) + '</textarea></label>'
      + '<label class="st-field"><span>default model</span><select data-mmodel="' + i + '">'
      + (opts || '<option value="">—</option>') + '</select></label></div>';
  }).join('');
  const pick = (labels, cur, emptyLabel) =>
    '<option value="">' + stEsc(emptyLabel) + '</option>' + labels.map(l =>
      '<option value="' + stEsc(l) + '"' + (l === cur ? ' selected' : '') + '>' + stEsc(l) + '</option>').join('');
  const labels = (m.providers || []).map(p => p.label).filter(Boolean);
  body.innerHTML =
    '<div class="st-col"><div class="st-models-grid">' + rows + '</div>'
    + '<button class="ghost-btn neutral sm" data-add-prov type="button">add provider</button></div>'
    + '<div class="st-col"><h3>Stages</h3>'
    + '<label class="st-field"><span>extract</span><select data-mextract>'
    + pick(labels, m.extract_active, 'Gemini pool') + '</select></label>'
    + '<label class="st-field"><span>enrich</span><select data-menrich>'
    + pick(labels, m.enrich_active, 'none') + '</select></label>'
    + '<label class="st-field"><span>locate</span><select data-mvision>'
    + pick(labels, m.vision_active, 'Gemini pool') + '</select></label>'
    + '<label class="st-field"><span>gemini model</span><select data-mgem>'
    + pick(m.gemini_models || [], m.gemini_pin, 'All models')
    + '</select></label></div>'
    + '<div class="st-col"><h3>Proxy</h3>'
    + '<label class="st-field"><span>proxy</span><select data-mproxy>'
    + [''].concat(m.proxy_profiles || []).map(p =>
        '<option value="' + stEsc(p) + '"' + (p === (m.proxy || '') ? ' selected' : '') + '>'
        + stEsc(p || 'Direct') + '</option>').join('') + '</select></label></div>'
    + '<div class="st-profile-foot"><span class="st-warn" data-models-err hidden></span>'
    + '<button class="ghost-btn neutral" data-models-save type="button">Save models</button></div>';
  stAutofit(body);
  body.onclick = e => {
    const b = e.target.closest('button');
    if (!b || !body.contains(b)) return;
    if (b.hasAttribute('data-add-prov')){
      studioSaveModels().then(() => {
        const cur = (StudioState.models && StudioState.models.providers) || [];
        if (!cur.some(p => p.label === '9router')){
          cur.push({ label: '9router', base_url: 'http://127.0.0.1:20128/v1',
                     api_key_set: false, model: '', models: [] });
          studioRenderModels();
        }
      });
    }
    else if (b.hasAttribute('data-del-prov')){
      StudioState.models.providers.splice(+b.dataset.delProv, 1);
      studioRenderModels();
    }
    else if (b.hasAttribute('data-models-save')){ studioSaveModels(); }
  };
}

async function studioSaveModels(){
  const body = $('stModelsBody');
  const m = StudioState.models;
  const at = (attr, i) => { const el = body.querySelector('[' + attr + '="' + i + '"]'); return el ? el.value : ''; };
  const providers = (m.providers || []).map((p, i) => {
    const typed = at('data-mkey', i);
    return { label: p.label, base_url: p.base_url || 'http://127.0.0.1:20128/v1',
             api_key: typed || (p.api_key_set ? ST_KEEP : ''),
             model: at('data-mmodel', i).trim(),
             models: at('data-mmodels', i).split('\n').map(s => s.trim()).filter(Boolean) };
  });
  const sel = attr => { const el = body.querySelector('[' + attr + ']'); return el ? el.value : ''; };
  const err = body.querySelector('[data-models-err]');
  try {
    const d = await studioJson('/api/studio/models', {
      providers, extract_active: sel('data-mextract'),
      enrich_active: sel('data-menrich'), vision_active: sel('data-mvision'),
      gemini_pin: sel('data-mgem'),
      proxy: sel('data-mproxy') });
    if (d.ok === false) throw new Error(d.error || 'save failed');
    StudioState.models = d;
    studioRenderModels();
    toast('Studio models saved.');
    return d;
  } catch (e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Save failed: ' + e.message);
  }
}

/* ── Bulk panel: layer2 + geometry + review, one table ── */
const stEnDigits = v => String(v == null ? '' : v).replace(/[۰-۹٠-٩]/g, d => {
  const fa = '۰۱۲۳۴۵۶۷۸۹'.indexOf(d);
  return String(fa >= 0 ? fa : '٠١٢٣٤٥٦٧٨٩'.indexOf(d));
});

function stBusy(on, msg){
  $('stNow').dataset.state = on ? 'live' : 'idle';
  if (msg) $('stNowMsg').textContent = msg;
}

function stStageNote(d){
  const s = (d && (d.report || d.stats)) || {};
  const parts = [];
  if (s.blocks != null) parts.push(stNum(s.blocks) + ' blocks');
  if (s.ready != null) parts.push('ready ' + stNum(s.ready) + ' · warn '
    + stNum(s.warn || 0) + ' · error ' + stNum(s.error || 0));
  if (s.crops != null) parts.push(stNum(s.crops) + ' crops');
  if (s.questions != null) parts.push(stNum(s.questions) + ' questions · '
    + stNum(s.answers || 0) + ' answers');
  return parts.join(' · ') || 'ok';
}

async function studioLoadBulk(){
  const id = StudioState.job;
  if (!id){
    StudioState.bulk = null;
    StImport.rows = null;
    studioRenderBulk(); studioRenderCrops(); studioRenderImport();
    studioRenderInspect();
    return;
  }
  try {
    StudioState.bulk = await studioApi('/api/studio/jobs/'
      + encodeURIComponent(id) + '/bulk');
  } catch (e){ StudioState.bulk = { error: e.message }; }
  // the Import table lists rows, not a count — they ride on the bulk payload
  await studioLoadImportRows();
  studioRenderBulk(); studioRenderCrops(); studioRenderImport();
  studioRenderInspect();
}

async function studioRunStage(stage, label){
  const id = StudioState.job;
  if (!id) return;
  stBusy(true, label + ' running…');
  try {
    const opts = { method:'POST' };
    if (stage === 'extract'){
      opts.headers = { 'content-type':'application/json' };
      opts.body = JSON.stringify({ organize: StudioState.organize || 'auto' });
    }
    const d = await studioApi('/api/studio/jobs/' + encodeURIComponent(id)
      + '/' + stage, opts);
    await studioLoadJobs();
    await studioSelectJob(id, { keepPanel:true });
    toast(label + ': ' + stStageNote(d));
    return d;
  } catch (e){
    toast(label + ' failed: ' + e.message);
  } finally { stBusy(false); }
}

function stStatusChip(status){
  const cls = status === 'error' ? 'sc-rejected'
    : (status === 'warn' ? 'sc-pending' : 'sc-approved');
  return '<span class="status-chip ' + cls + '">' + stEsc(status || 'ready') + '</span>';
}

function stSplitChip(b){
  if (!b) return '';
  if (b.split) return '<span class="status-chip sc-pending">split '
    + stEsc(b.split) + '</span> ';
  if (b.split_joined) return '<span class="status-chip sc-approved">joined</span> ';
  return '';
}

function stCropCell(crop){
  if (!crop || !crop.needed) return '—';
  return crop.approved ? '<span class="st-art on">cropped</span>'
                       : '<span class="st-art off">review</span>';
}

function stBatchSettings(){
  const m = StudioState.models || {};
  const s = (StudioState.bulk && StudioState.bulk.settings) || {};
  const pages = s.pages_per_call != null ? s.pages_per_call
    : (m.pages_per_call != null ? m.pages_per_call : 3);
  const bands = s.band_batch != null ? s.band_batch
    : (m.band_batch != null ? m.band_batch : 6);
  return '<div class="st-profile-grid">'
    + '<label class="st-field"><span>pages / call</span>'
    + '<input type="number" id="stPagesPerCall" min="1" max="8" value="'
    + stEsc(String(pages)) + '"></label>'
    + '<label class="st-field"><span>bands / call</span>'
    + '<input type="number" id="stBandBatch" min="1" max="12" value="'
    + stEsc(String(bands)) + '"></label>'
    + '</div>'
    + '<div class="st-profile-foot">'
    + '<button class="ghost-btn neutral" id="stSaveBatching" type="button">Save batching</button>'
    + '</div>';
}

async function studioSaveBatching(){
  const pages = parseInt($('stPagesPerCall').value, 10);
  const bands = parseInt($('stBandBatch').value, 10);
  try {
    const d = await studioJson('/api/studio/settings', {
      pages_per_call: isNaN(pages) ? null : pages,
      band_batch: isNaN(bands) ? null : bands,
    });
    if (StudioState.models){
      StudioState.models.pages_per_call = d.pages_per_call;
      StudioState.models.band_batch = d.band_batch;
    }
    if (StudioState.bulk && StudioState.bulk.settings){
      StudioState.bulk.settings.pages_per_call = d.pages_per_call;
      StudioState.bulk.settings.band_batch = d.band_batch;
    }
    studioRenderBulk();
    toast('Batching saved.');
  } catch (e){ toast('Batching save failed: ' + e.message); }
}

/* ── Inspect panel · the local-first editing desk ──
   Edit a question row or replace its picture, then approve the whole batch
   once. Nothing leaves the browser until approval: text/field edits sit in
   localStorage, pictures sit in the job's own workspace/drafts folder —
   never the bucket, never the database. Approval uploads each picture ALREADY
   CROPPED (the reviewer cuts it in their own editor, so there is no browser
   cropper to exploit) and imports every ready row in ONE pass. */
const ST_INSPECT_LIMIT = 400;
const StInspect = { job: null, edits: {}, crops: {}, timer: 0 };

const stInspectStoreKey = job => 'studio.inspect.' + String(job || '');

function stInspectLoad(){
  const job = StudioState.job || '';
  if (StInspect.job === job) return;
  StInspect.job = job;
  StInspect.edits = {};
  StInspect.crops = {};
  if (!job) return;
  try{
    const raw = localStorage.getItem(stInspectStoreKey(job));
    const d = raw ? JSON.parse(raw) : null;
    if (d && typeof d === 'object'){
      if (d.edits && typeof d.edits === 'object') StInspect.edits = d.edits;
      if (d.crops && typeof d.crops === 'object') StInspect.crops = d.crops;
    }
  }catch(e){ /* a corrupt draft is not worth breaking the panel for */ }
}

function stInspectSave(){
  const job = StInspect.job;
  if (!job) return;
  const flush = () => {
    StInspect.timer = 0;
    try{
      localStorage.setItem(stInspectStoreKey(job), JSON.stringify({
        edits: StInspect.edits, crops: StInspect.crops }));
    }catch(e){ toast('Local draft is full — approve the batch to free it'); }
  };
  if (StInspect.timer) clearTimeout(StInspect.timer);
  StInspect.timer = setTimeout(flush, 250);
}

function stInspectPatch(key){ return StInspect.edits[key] || {}; }

function stInspectValue(block, field){
  const patch = stInspectPatch(block.key);
  if (patch[field] !== undefined && patch[field] !== null) return patch[field];
  const value = block[field];
  return value == null ? '' : value;
}

function stInspectDraftCount(){
  return Object.keys(StInspect.edits).length + Object.keys(StInspect.crops).length;
}

function stInspectJobs(){
  /* StudioState.bulk is the whole /bulk reply, so the blocks hang off .view */
  const view = StudioState.bulk && StudioState.bulk.view;
  if (!view) return [];
  const blocks = (view.blocks || []).filter(b => b.kind === 'question');
  return blocks.slice(0, ST_INSPECT_LIMIT);
}

/* ── Inspect list · one row per question ──
   Every row carries its own edit and approve: Edit opens the focused pop-up
   (auto-fitting text boxes, the picture with box sliders), Approve commits
   that row's local draft now. "Approve all" stays for a whole batch. */
function stInspectRow(block){
  const key = block.key || '';
  const draft = (StInspect.edits[key] || {});
  const edited = Object.keys(draft).length > 0;
  const crop = block.crop || {};
  const prov = crop.source === 'model'
    ? '<span class="st-art plain">model \u00b7 review</span>'
    : (crop.source === 'asset' ? '<span class="st-art plain">asset</span>' : '');
  const pic = crop.needed
    ? ('<span class="st-art ' + (crop.approved || crop.external ? 'on' : 'off')
       + '">picture</span>' + prov)
    : '';
  const missing = (block.missing || []).length;
  const missingChip = missing
    ? '<span class="st-art off" title="' + stEsc((block.missing || []).join(' '))
      + '">' + stNum(missing) + '</span>'
    : '—';
  const topic = stInspectValue(block, 'topic');
  const text = stInspectValue(block, 'text');
  const opts = stInspectValue(block, 'options');
  const tr = document.createElement('tr');
  tr.className = 'st-insp-row' + (edited ? ' edited' : '');
  tr.dataset.key = key;
  tr.ondblclick = ev => {
    if (ev.target.closest('button, a, input, select, textarea')) return;
    stEditOpen(key);
  };
  tr.innerHTML =
    '<td class="mono">' + stEsc(block.number != null ? String(block.number) : '—') + '</td>'
    + '<td class="mono">' + stEsc(block.page != null ? String(block.page) : '—') + '</td>'
    + '<td>' + (block.status ? stStatusChip(block.status) : '—') + '</td>'
    + '<td class="st-insp-text" dir="auto">' + mdHtml(String(text || '')) + '</td>'
    + '<td class="st-insp-topic" dir="auto">' + stEsc(topic || '—') + '</td>'
    + '<td class="mono">' + stEsc(Array.isArray(opts) ? opts.length : 0) + '</td>'
    + '<td>' + missingChip + '</td>'
    + '<td>' + (pic || '—') + '</td>'
    + '<td><span class="st-art on" data-insp-edited' + (edited ? '' : ' hidden') + '>edited</span></td>'
    + '<td class="st-row-actions">'
    + '<button class="ghost-btn neutral sm" data-insp-edit="' + stEsc(key) + '" type="button">edit</button>'
    + '<button class="ghost-btn neutral sm" data-insp-ok="' + stEsc(key) + '" type="button">approve</button>'
    + '</td>';
  return tr;
}

function stInspectMark(){
  const wrap = $('stInspectBody');
  if (!wrap) return;
  wrap.querySelectorAll('.st-insp-row').forEach(tr => {
    const edited = !!StInspect.edits[tr.dataset.key];
    tr.classList.toggle('edited', edited);
    const chip = tr.querySelector('[data-insp-edited]');
    if (chip) chip.hidden = !edited;
  });
  const count = wrap.querySelector('[data-insp-count]');
  if (count) count.textContent = stNum(stInspectDraftCount());
  const stage = $('stEditHint');
  if (stage && StEdit.key) stage.textContent = stEditDirty() ? 'unsaved' : '';
}

async function stInspectReset(){
  const job = StInspect.job;
  if (!job) return;
  const keys = Object.keys(StInspect.crops);
  await Promise.all(keys.map(key => fetch('/api/studio/jobs/'
    + encodeURIComponent(job) + '/draft/crop/' + encodeURIComponent(key),
    { method: 'DELETE' }).catch(() => null)));
  StInspect.edits = {};
  StInspect.crops = {};
  try{ localStorage.removeItem(stInspectStoreKey(job)); }catch(e){}
  studioRenderInspect();
  toast('Local drafts cleared');
}

async function stInspectApprove(btn, keys){
  const job = StInspect.job || StudioState.job;
  if (!job) return;
  if (!Object.keys(StInspect.edits).length && !Object.keys(StInspect.crops).length){
    toast('No local drafts to approve');
    return;
  }
  if (btn) btn.disabled = true;
  try {
    const edits = {};
    Object.keys(StInspect.edits).forEach(k => {
      if (!keys || keys.includes(k)) edits[k] = StInspect.edits[k];
    });
    const d = await studioJson('/api/studio/jobs/' + encodeURIComponent(job)
      + '/commit', { edits: edits, crops: {}, target: 'supabase' });
    if (!d.ok) throw new Error(d.error || 'commit failed');
    if (keys) keys.forEach(k => { delete StInspect.edits[k]; });
    else StInspect.edits = {};
    StInspect.crops = {};
    stInspectSave();
    if (!Object.keys(StInspect.edits).length && !Object.keys(StInspect.crops).length){
      try{ localStorage.removeItem(stInspectStoreKey(job)); }catch(e){}
    }
    const report = d.report || {};
    toast('Imported ' + stNum(report.questions || 0) + ' question(s) · '
      + stNum(report.answers || 0) + ' answer(s) · '
      + stNum(d.uploads || 0) + ' picture(s)');
    await studioLoadBulk();
  }catch(e){ toast('Approve failed: ' + e.message); }
  finally { if (btn) btn.disabled = false; }
}

function stInspectScorecard(){
  const d = StudioState.bulk || {};
  const sc = d.scorecard || ((d.view || {}).scorecard);
  if (!sc) return document.createElement('div');
  const conf = sc.layout_confidence == null ? null
    : Number(sc.layout_confidence).toFixed(2);
  const meta = (sc.metadata_missing || []).join(' ');
  const chips = [
    ['blocks', stNum(sc.blocks), ''],
    ['prose', sc.prose ? stNum(sc.prose) : '', ''],
    ['ready', stNum(sc.ready), sc.ready ? 'ok' : 'plain'],
    ['warn', stNum(sc.warn), sc.warn ? 'warn' : 'plain'],
    ['4 opts', stNum(sc.four_options) + ' / ' + stNum(sc.questions),
     sc.questions && sc.four_options === sc.questions ? 'ok' : 'warn'],
    ['topics', stNum(sc.topics_known) + ' / ' + stNum(sc.questions),
     sc.questions && sc.topics_known === sc.questions ? 'ok' : 'warn'],
    ['crops', stNum(sc.crops_approved) + ' / ' + stNum(sc.crops),
     sc.crops && sc.crops_approved === sc.crops ? 'ok' : 'warn'],
    ['LLM', stNum(sc.organized), sc.organized ? '' : 'plain'],
  ];
  if (conf) chips.push(['layout', conf, Number(conf) >= 1 ? 'ok' : 'warn']);
  if (meta) chips.push(['metadata', meta, 'plain']);
  const strip = document.createElement('div');
  strip.className = 'st-tools st-scorecard';
  strip.innerHTML = stStats(chips);
  return strip;
}

function studioRenderInspect(){
  const wrap = $('stInspectBody');
  if (!wrap) return;
  stInspectLoad();
  const job = StudioState.job;
  if (!job || !StudioState.bulk){
    wrap.replaceChildren();
    return;
  }
  const staged = (StudioState.bulk && StudioState.bulk.drafts) || {};
  Object.keys(staged).forEach(key => {
    if (!StInspect.crops[key]) StInspect.crops[key] = staged[key].at || 1;
  });
  const blocks = stInspectJobs();
  if (!blocks.length){
    wrap.replaceChildren();
    return;
  }
  const bar = document.createElement('div');
  bar.className = 'st-inspect-bar';
  const count = stInspectDraftCount();
  bar.innerHTML = '<span class="st-stat' + (count ? ' warn' : '')
    + '"><em>drafts</em><b data-insp-count>' + stNum(count) + '</b></span>'
    + '<span class="st-gap"></span>'
    + '<button class="ghost-btn neutral sm" data-insp-clear type="button">reset</button>'
    + '<button class="ghost-btn neutral" data-insp-apply type="button">approve all</button>';
  const host = document.createElement('div');
  host.className = 'st-table-wrap st-insp-table';
  const table = document.createElement('table');
  table.className = 'st-table';
  table.innerHTML = '<thead><tr><th>#</th><th>page</th><th>status</th>'
    + '<th>question</th><th>topic</th><th>opts</th><th>missing</th>'
    + '<th>picture</th><th></th><th></th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  blocks.forEach(block => tbody.appendChild(stInspectRow(block)));
  host.appendChild(table);
  wrap.replaceChildren(stInspectScorecard(), bar, host);

  bar.querySelector('[data-insp-clear]').onclick = () => stInspectReset();
  bar.querySelector('[data-insp-apply]').onclick = e => stInspectApprove(e.currentTarget);
  tbody.querySelectorAll('[data-insp-edit]').forEach(b => b.onclick = () =>
    stEditOpen(b.dataset.inspEdit));
  tbody.querySelectorAll('[data-insp-ok]').forEach(b => b.onclick = () =>
    stInspectApprove(b, [b.dataset.inspOk]));
  typesetMath(host);
}

/* ── the question editor · auto-fitting boxes + picture box sliders ──
   Textareas grow with their text (no scrollbar, no resize handle), the
   picture shows the page with its current box, and four range pairs move the
   box edges in the page's own 0-1000 space. */
const StEdit = { key: null, snapshot: null, box: [0, 0, 1000, 1000],
                 touched: false, enabled: false, drag: null, ro: null,
                 previewTimer: 0 };
const ST_EDIT_FIELDS = [['number', 'number'], ['label', 'label'], ['subject', 'subject'],
  ['topic', 'topic'], ['corp', 'corp'], ['year', 'year'], ['grade', 'grade'],
  ['difficulty', 'difficulty']];
/* The crop square's own limits: 2 % of the page is the smallest cut worth
   making, and the arrow keys nudge by the values below. */
const ST_BOX_MIN = 20;
const stEditClamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

function stEditBlock(key){
  return stInspectJobs().find(b => b.key === key) || null;
}

function stEditCurrent(block, field){
  const el = document.querySelector('#stEditBody [data-efield="' + field + '"]');
  if (el) return el.value;
  return stInspectValue(block, field);
}

function stEditSnapshot(block){
  const out = {};
  ST_EDIT_FIELDS.concat([['text', 'text'], ['options', 'options']]).forEach(([f]) => {
    const el = document.querySelector('#stEditBody [data-efield="' + f + '"]');
    if (!el) return;
    out[f] = f === 'options'
      ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
      : el.value;
  });
  out.bbox = StEdit.box.map(v => Math.round(v));
  return out;
}

function stEditDirty(){
  if (!StEdit.key || !StEdit.snapshot) return false;
  const block = stEditBlock(StEdit.key);
  if (!block) return false;
  const now = stEditSnapshot(block);
  return JSON.stringify(now) !== JSON.stringify(StEdit.snapshot);
}

function stEditBoxes(){
  return ['y0', 'x0', 'y1', 'x1'].map(n =>
    document.querySelector('#stEditBody [data-box="' + n + '"]')).filter(Boolean);
}

/* ── the crop square ──
   The square lives in the page's own 0-1000 space, so what it carries IS the
   bbox the importer uses - there is no second conversion anywhere. The page
   is drawn with object-fit:contain, so the square is placed from the rendered
   image rect instead of the stage: it stays glued to the paper at any window
   size, and the scrim around it shows exactly what gets cut. */
function stEditBoxNorm(raw){
  let v = Array.isArray(raw) ? raw.map(Number) : [];
  if (v.length !== 4 || !v.every(Number.isFinite)) v = [0, 0, 1000, 1000];
  const y0 = stEditClamp(v[0], 0, 1000), y1 = stEditClamp(v[2], 0, 1000);
  const x0 = stEditClamp(v[1], 0, 1000), x1 = stEditClamp(v[3], 0, 1000);
  return [Math.min(y0, y1), Math.min(x0, x1), Math.max(y0, y1), Math.max(x0, x1)];
}

function stEditBoxFit(raw){
  const box = stEditBoxNorm(raw);
  [0, 1].forEach(lo => {
    const hi = lo + 2;
    if (box[hi] - box[lo] >= ST_BOX_MIN) return;
    box[hi] = Math.min(1000, box[lo] + ST_BOX_MIN);
    box[lo] = Math.max(0, box[hi] - ST_BOX_MIN);
  });
  return box;
}

/* where the page actually sits inside its stage (object-fit:contain) */
function stEditStageRect(){
  const stage = $('stEditStage'), img = $('stEditPage');
  if (!stage || !img) return null;
  const cw = stage.clientWidth, ch = stage.clientHeight;
  const nw = img.naturalWidth || 0, nh = img.naturalHeight || 0;
  if (!cw || !ch || !nw || !nh) return null;
  const scale = Math.min(cw / nw, ch / nh);
  const w = nw * scale, h = nh * scale;
  return { w: w, h: h, left: (cw - w) / 2, top: (ch - h) / 2 };
}

/* the one place the square's numbers are written: sliders, outputs and box
   position all follow StEdit.box, so they can never disagree */
function stEditBoxApply(raw){
  StEdit.box = stEditBoxFit(raw);
  stEditBoxes().forEach((r, i) => {
    r.value = String(Math.round(StEdit.box[i]));
    const out = document.querySelector('#stEditBody [data-box-out="' + r.dataset.box + '"]');
    if (out) out.textContent = String(Math.round(StEdit.box[i]));
  });
  stEditBoxPaint();
}

function stEditBoxPaint(){
  const box = document.getElementById('stEditBox');
  const rect = stEditStageRect();
  if (!box || !rect) return;
  const [y0, x0, y1, x1] = StEdit.box;
  box.style.left = (rect.left + x0 / 1000 * rect.w) + 'px';
  box.style.top = (rect.top + y0 / 1000 * rect.h) + 'px';
  box.style.width = ((x1 - x0) / 1000 * rect.w) + 'px';
  box.style.height = ((y1 - y0) / 1000 * rect.h) + 'px';
}

function stEditGrab(e){
  if (!StEdit.enabled || (e.button != null && e.button !== 0)) return;
  const rect = stEditStageRect();
  if (!rect) return;
  const handle = e.target && e.target.closest ? e.target.closest('[data-h]') : null;
  e.preventDefault();
  StEdit.drag = { mode: handle ? 'resize' : 'move',
                  corner: handle ? handle.dataset.h : '',
                  cx: e.clientX, cy: e.clientY,
                  box: StEdit.box.slice(), rect: rect };
  document.addEventListener('pointermove', stEditDragMove);
  document.addEventListener('pointerup', stEditDragEnd);
  document.addEventListener('pointercancel', stEditDragEnd);
}

function stEditDragMove(e){
  const g = StEdit.drag;
  if (!g) return;
  const dx = (e.clientX - g.cx) / g.rect.w * 1000;
  const dy = (e.clientY - g.cy) / g.rect.h * 1000;
  let y0 = g.box[0], x0 = g.box[1], y1 = g.box[2], x1 = g.box[3];
  if (g.mode === 'move'){
    const w = x1 - x0, h = y1 - y0;
    x0 = stEditClamp(x0 + dx, 0, 1000 - w); x1 = x0 + w;
    y0 = stEditClamp(y0 + dy, 0, 1000 - h); y1 = y0 + h;
  } else {
    if (g.corner.indexOf('n') !== -1) y0 = stEditClamp(y0 + dy, 0, y1 - ST_BOX_MIN);
    else y1 = stEditClamp(y1 + dy, y0 + ST_BOX_MIN, 1000);
    if (g.corner.indexOf('w') !== -1) x0 = stEditClamp(x0 + dx, 0, x1 - ST_BOX_MIN);
    else x1 = stEditClamp(x1 + dx, x0 + ST_BOX_MIN, 1000);
  }
  stEditTouch();
  stEditBoxApply([y0, x0, y1, x1]);
}

function stEditDragEnd(){
  if (!StEdit.drag) return;
  StEdit.drag = null;
  document.removeEventListener('pointermove', stEditDragMove);
  document.removeEventListener('pointerup', stEditDragEnd);
  document.removeEventListener('pointercancel', stEditDragEnd);
}

/* arrow keys nudge the square (Shift = coarse), so the box stays reachable
   without a pointer */
function stEditKey(e){
  if (!StEdit.enabled) return;
  const step = e.shiftKey ? 20 : 4;
  const nudge = { ArrowUp: [0, -step], ArrowDown: [0, step],
                  ArrowLeft: [-step, 0], ArrowRight: [step, 0] }[e.key];
  if (!nudge) return;
  e.preventDefault();
  const [y0, x0, y1, x1] = StEdit.box;
  const w = x1 - x0, h = y1 - y0;
  const ny = stEditClamp(y0 + nudge[1], 0, 1000 - h);
  const nx = stEditClamp(x0 + nudge[0], 0, 1000 - w);
  stEditTouch();
  stEditBoxApply([ny, nx, ny + h, nx + w]);
}

/* any hand on the square or a field means the draft is no longer saved */
function stEditTouch(){
  StEdit.touched = true;
  const hint = $('stEditHint');
  if (hint) hint.textContent = 'unsaved';
}

/* the picture the square cut, shown under the page */
function stEditCropPaint(url){
  const wrap = document.querySelector('#stEditBody .st-edit-croppic');
  const img = document.getElementById('stEditCrop');
  if (!wrap || !img) return;
  if (url) img.src = url;
  wrap.hidden = !img.getAttribute('src');
}

/* Text boxes size to their own text: one line of chrome minimum, the content
   height otherwise. A box inside .st-edit-long lets CSS fill the column
   instead, so nothing here fights it. */
function stAutofit(root){
  (root || document).querySelectorAll('textarea[data-autofit]').forEach(area => {
    if (area.closest('.st-edit-long')){ area.style.height = ''; return; }
    area.rows = 1;
    area.style.height = 'auto';
    area.style.height = Math.max(area.scrollHeight + 2, 34) + 'px';
  });
}

/* the editor's rendered pane: what the text / options actually look like once
   MathJax and the Markdown table pass have done their work. It repaints while
   the reviewer types, so the draft is judged by its output, not its source. */
function stEditPreviewHtml(field, raw){
  if (field === 'options'){
    const lines = String(raw || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (!lines.length) return '';
    return '<ul class="opts">' + lines.map((line, i) => {
      const m = line.match(/^\s*([^)\.ـ:]{1,3})\s*[\)\.ـ:]\s*(.*)$/);
      const label = m ? m[1] : String(i + 1);
      const text = m ? m[2] : line;
      return '<li><span class="opt-label">' + stEsc(label)
        + '</span><span class="opt-text">' + mdHtml(text) + '</span></li>';
    }).join('') + '</ul>';
  }
  return mdHtml(raw);
}

function stEditPreviewPaint(){
  const body = $('stEditBody');
  if (!body) return;
  body.querySelectorAll('[data-efield-preview]').forEach(box => {
    const field = box.dataset.efieldPreview;
    const area = body.querySelector('[data-efield="' + field + '"]');
    box.innerHTML = area ? stEditPreviewHtml(field, area.value) : '';
    typesetMath(box);
  });
}

function stEditPreviewSoon(){
  if (StEdit.previewTimer) clearTimeout(StEdit.previewTimer);
  StEdit.previewTimer = setTimeout(() => {
    StEdit.previewTimer = 0;
    stEditPreviewPaint();
  }, 260);
}

function stEditOpen(key){
  const block = stEditBlock(key);
  if (!block) return;
  const job = StInspect.job;
  const staged = StInspect.crops[key];
  const crop = block.crop || {};
  const page = crop.page || block.page;
  const draftUrl = '/api/studio/jobs/' + encodeURIComponent(job)
    + '/draft/crop/' + encodeURIComponent(key);
  const pic = staged ? draftUrl + '?v=' + encodeURIComponent(staged)
    : (crop.exists ? '/api/studio/jobs/' + encodeURIComponent(job)
      + '/crop/' + encodeURIComponent(key) : '');
  const preview = page ? '/api/studio/jobs/' + encodeURIComponent(job)
    + '/preview/' + encodeURIComponent(page) : '';
  StEdit.key = key;
  /* the square starts on the row's own box, so opening a crop shows the cut
     that is already stored, not a guess */
  StEdit.box = stEditBoxFit(stInspectPatch(key).bbox
    || (crop.needed ? crop.bbox : null) || block.bbox || [0, 0, 1000, 1000]);
  StEdit.enabled = !!crop.needed;
  StEdit.touched = false;
  StEdit.drag = null;
  const field = (f, label) => '<label class="st-field"><span>' + label + '</span>'
    + '<input type="text" dir="auto" data-efield="' + f + '" value="'
    + stEsc(String(stEditValue(block, f) == null ? '' : stEditValue(block, f))) + '"></label>';
  const area = (f, label) => {
    const raw = stInspectValue(block, f);
    const val = Array.isArray(raw) ? raw.join('\n') : (raw == null ? '' : String(raw));
    return '<div class="st-edit-pair">'
      + '<label class="st-field"><span>' + label + '</span>'
      + '<textarea data-autofit data-efield="' + f + '" rows="1" dir="auto">' + stEsc(val) + '</textarea></label>'
      + '<div class="st-field st-edit-preview"><span>rendered</span>'
      + '<div class="st-edit-render" data-efield-preview="' + f + '" dir="auto"></div></div>'
      + '</div>';
  };
  const slider = (idx, name, label) => '<label class="st-slider"><span>' + label + '</span>'
    + '<input type="range" min="0" max="1000" step="1" data-box="' + name + '" data-idx="' + idx
    + '" value="' + stEsc(String(StEdit.box[idx] != null ? StEdit.box[idx] : 0)) + '">'
    + '<b data-box-out="' + name + '">' + stEsc(String(StEdit.box[idx] != null ? StEdit.box[idx] : 0)) + '</b></label>';
  $('stEditTitle').textContent = 'Q' + (block.number != null ? block.number : '\u2014')
    + (page ? ' \u00b7 page ' + page : '');
  $('stEditBody').innerHTML =
    '<div class="st-edit-grid">'
    + '<div class="st-edit-left">'
    +   '<div class="st-edit-fields">'
    +     ST_EDIT_FIELDS.map(([f]) => field(f, f)).join('') + '</div>'
    +   '<div class="st-edit-long">'
    +     area('text', 'text') + area('options', 'options') + '</div>'
    + '</div>'
    + '<div class="st-edit-right">'
    +   (preview
        ? '<div class="st-edit-stage" id="stEditStage">'
          + '<img id="stEditPage" src="' + stEsc(preview) + '" alt="page">'
          + '<div class="st-edit-box" id="stEditBox" tabindex="0"'
          +   (crop.needed ? '' : ' hidden') + '>'
          +   '<i class="st-edit-handle" data-h="nw"></i>'
          +   '<i class="st-edit-handle" data-h="ne"></i>'
          +   '<i class="st-edit-handle" data-h="sw"></i>'
          +   '<i class="st-edit-handle" data-h="se"></i>'
          + '</div></div>'
        : '')
    +   '<div class="st-edit-boxes">'
    +     slider(1, 'x0', 'left') + slider(0, 'y0', 'top')
    +     slider(3, 'x1', 'right') + slider(2, 'y1', 'bottom')
    +   '</div>'
    +   (crop.needed ? '' : '<label class="sw"><input type="checkbox" data-edit-pic'
          + (pic || Object.keys(stInspectPatch(key)).includes('pic') ? ' checked' : '')
          + '><span class="tr" aria-hidden="true"></span><em>picture</em></label>')
    +   '<div class="st-edit-croppic"' + (pic ? '' : ' hidden') + '>'
    +     '<img class="st-edit-crop" id="stEditCrop" src="' + (pic ? stEsc(pic) : '')
          + '" alt="crop"></div>'
    +   '<label class="ghost-btn neutral sm st-inspect-pick">' + fi('image')
    +     '<span>swap picture</span><input type="file" accept="image/*" data-edit-file></label>'
    + '</div></div>';
  $('stEditHint').textContent = '';
  $('stEditModal').hidden = false;
  stEditWire(block);
  stAutofit($('stEditBody'));
  stEditBoxApply(StEdit.box);
  stEditPreviewPaint();
  StEdit.snapshot = stEditSnapshot(block);
}

function stEditValue(block, field){
  const v = stInspectValue(block, field);
  return Array.isArray(v) ? v.join('\n') : v;
}

function stEditWire(block){
  const body = $('stEditBody');
  body.querySelectorAll('[data-efield]').forEach(el => {
    el.oninput = () => {
      if (el.tagName === 'TEXTAREA') stAutofit(body);
      if (el.dataset.efield === 'text' || el.dataset.efield === 'options')
        stEditPreviewSoon();
      const hint = $('stEditHint');
      if (hint) hint.textContent = 'unsaved';
    };
  });
  body.querySelectorAll('[data-box]').forEach(r => {
    r.oninput = () => {
      stEditTouch();
      stEditBoxApply(stEditBoxes().map(x => parseInt(x.value, 10) || 0));
    };
  });
  const page = document.getElementById('stEditPage');
  if (page){
    page.onload = () => stEditBoxPaint();
    if (page.complete && page.naturalWidth) stEditBoxPaint();
  }
  const stage = $('stEditStage');
  if (stage && window.ResizeObserver){
    if (StEdit.ro) StEdit.ro.disconnect();
    StEdit.ro = new ResizeObserver(() => stEditBoxPaint());
    StEdit.ro.observe(stage);
  }
  const box = document.getElementById('stEditBox');
  if (box){
    box.onpointerdown = stEditGrab;
    box.onkeydown = stEditKey;
  }
  const toggle = body.querySelector('[data-edit-pic]');
  if (toggle){
    toggle.onchange = () => {
      StEdit.enabled = toggle.checked;
      StEdit.touched = toggle.checked;
      const hint = $('stEditHint');
      if (hint) hint.textContent = toggle.checked ? 'unsaved' : '';
      if (box) box.hidden = !toggle.checked;
      if (toggle.checked) stEditBoxApply(StEdit.box);
    };
  }
  const file = body.querySelector('[data-edit-file]');
  if (file) file.onchange = () => stEditSwapPicture(file, block);
}

async function stEditSwapPicture(input, block){
  const chosen = input.files && input.files[0];
  const key = block.key;
  if (!chosen || !key) return;
  input.value = '';
  try {
    const form = new FormData();
    form.append('file', chosen, chosen.name || 'crop.jpg');
    const res = await fetch('/api/studio/jobs/' + encodeURIComponent(StInspect.job)
      + '/draft/crop/' + encodeURIComponent(key), { method: 'POST', body: form });
    const d = await res.json();
    if (!res.ok || d.ok === false) throw new Error(d.error || ('HTTP ' + res.status));
    StInspect.crops[key] = d.at || Date.now();
    stInspectSave();
    stEditOpen(key);
    stInspectMark();
  }catch(e){ toast('Picture failed: ' + e.message); }
}

function stEditClose(){
  if (StEdit.previewTimer){ clearTimeout(StEdit.previewTimer); StEdit.previewTimer = 0; }
  stEditDragEnd();
  if (StEdit.ro){ StEdit.ro.disconnect(); StEdit.ro = null; }
  $('stEditModal').hidden = true;
  StEdit.key = null;
  StEdit.snapshot = null;
  StEdit.touched = false;
  StEdit.enabled = false;
}

async function stEditSave(){
  const key = StEdit.key;
  const block = key ? stEditBlock(key) : null;
  if (!block) { stEditClose(); return; }
  const btn = $('stEditSave');
  if (btn) btn.disabled = true;
  try {
    const patch = StInspect.edits[key] || (StInspect.edits[key] = {});
    ['text', 'options'].concat(ST_EDIT_FIELDS.map(([f]) => f)).forEach(f => {
      const el = document.querySelector('#stEditBody [data-efield="' + f + '"]');
      if (!el) return;
      if (f === 'options'){
        const list = el.value.split('\n').map(s => s.trim()).filter(Boolean);
        if (JSON.stringify(list) !== JSON.stringify((block.options || []).map(String)))
          patch.options = list;
        else delete patch.options;
      } else {
        const val = el.value.trim();
        const base = block[f] == null ? '' : String(block[f]);
        if (val !== base) patch[f] = val || null; else delete patch[f];
      }
    });
    /* the square's values ARE the bbox: rounded once, stored with the row's
       local draft, and cut from the page into the job's drafts folder */
    const bbox = StEdit.box.map(v => Math.round(v));
    const baseBox = (block.crop || {}).bbox;
    const moved = JSON.stringify(bbox) !== JSON.stringify(baseBox);
    if (moved) patch.bbox = bbox; else delete patch.bbox;
    if (!Object.keys(patch).length) delete StInspect.edits[key];
    stInspectSave();
    if (StEdit.enabled && StEdit.touched && document.getElementById('stEditBox')){
      const job = encodeURIComponent(StInspect.job);
      const d = await studioJson('/api/studio/jobs/' + job
        + '/draft/cut/' + encodeURIComponent(key), { bbox: bbox });
      if (!d.ok) throw new Error(d.error || 'cut failed');
      StInspect.crops[key] = d.at || Date.now();
      stInspectSave();
      stEditCropPaint(d.url || ('/api/studio/jobs/' + job
        + '/draft/crop/' + encodeURIComponent(key)
        + '?v=' + encodeURIComponent(StInspect.crops[key])));
      StEdit.touched = false;
    }
    StEdit.snapshot = stEditSnapshot(block);
    stInspectMark();
    $('stEditHint').textContent = 'saved';
  }catch(e){ toast('Save failed: ' + e.message); }
  finally { if (btn) btn.disabled = false; }
}

function stEditWireOnce(){
  $('stEditClose').onclick = () => stEditClose();
  $('stEditSave').onclick = () => stEditSave();
  $('stEditModal').onclick = e => {
    if (e.target === $('stEditModal')) stEditClose();
  };
  document.addEventListener('keydown', e => {
    if (e.key !== 'Escape') return;
    if (!$('stEditModal').hidden) stEditClose();
    else if (!$('stJobModal').hidden) stJobClose();
  });
}


function studioRenderBulk(){
  const body = $('stBulkBody');
  const d = StudioState.bulk;
  if (!d){
    body.innerHTML = stBatchSettings();
    $('stSaveBatching').onclick = () => studioSaveBatching();
    return;
  }
  if (d.error){
    body.innerHTML = '<div class="db-empty db-err">' + stEsc(d.error) + '</div>';
    return;
  }
  const view = d.view || {};
  const blocks = view.blocks || [];
  const s = view.stats || {};
  const results = d.results || {};
  const rows = blocks.map(b => {
    const mark = results[b.key] || {};
    const flag = ((b.issues || []).join('; ')
      || (mark.status === 'skipped' ? (mark.reason || '') : ''));
    const label = b.kind === 'answer_key_entry'
      ? ('key ' + (b.label || '?'))
      : (b.text || '').replace(/\s+/g, ' ').trim();
    const active = StudioState.block === b.key ? ' active' : '';
    return '<tr class="st-row' + active + '" data-block="' + stEsc(b.key) + '">'
      + '<td><b>' + stNum(b.number != null ? b.number : b.order) + '</b>'
      + '<span class="st-sub">' + stEsc(b.key) + '</span></td>'
      + '<td>' + stEsc((b.kind || '').replace(/_/g, ' ')) + '</td>'
      + '<td>' + stEsc(label.slice(0, 70) || '—') + '</td>'
      + '<td>' + stNum((b.options || []).length) + '</td>'
      + '<td>' + stStatusChip(b.status) + '</td>'
      + '<td>' + stSplitChip(b) + '<span class="st-sub">'
      + stEsc(flag.slice(0, 80)) + '</span></td>'
      + '<td>' + stCropCell(b.crop) + '</td>'
      + '<td>' + (mark.status === 'ok' ? '<span class="st-art on">imported</span>' : '—') + '</td>'
      + '</tr>';
  }).join('');
  body.innerHTML = stBatchSettings()
    + '<div class="st-col">'
    + (blocks.length
      ? '<div class="st-table-wrap"><table class="st-table"><thead><tr><th>#</th><th>Kind</th><th>Text</th>'
        + '<th>Opts</th><th>Status</th><th>Flags</th><th>Crop</th><th>Import</th>'
        + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
      : '')
    + '<div class="st-profile-foot">'
    + stStats([['ready', stNum(s.ready || 0), 'ok'],
               ['warn', stNum(s.warn || 0), 'warn'],
               ['error', stNum(s.error || 0), 'err']])
    + '<label class="st-field"><span>organize</span>'
    + '<select id="stExtractOrganize">'
    + ['auto','off','all'].map(v => '<option value="' + v + '"'
        + (v === (StudioState.organize || 'auto') ? ' selected' : '') + '>'
        + (v === 'all' ? 'all pages' : v) + '</option>').join('')
    + '</select></label>'
    + '<button class="ghost-btn neutral" id="stRunExtract" type="button">Extract</button>'
    + '<button class="ghost-btn neutral" id="stRunEnrich" type="button">Enrich</button>'
    + '<button class="ghost-btn neutral" id="stRunGeometry" type="button">Locate</button>'
    + '</div></div>';
  $('stExtractOrganize').onchange = e => {
    StudioState.organize = e.target.value;
  };
  $('stRunExtract').onclick = () => studioRunStage('extract', 'Extract');
  $('stRunEnrich').onclick = () => studioRunStage('enrich', 'Enrich');
  $('stRunGeometry').onclick = () => studioRunStage('geometry', 'Locate');
  $('stSaveBatching').onclick = () => studioSaveBatching();
  body.querySelectorAll('.st-row').forEach(tr => tr.onclick = () => {
    StudioState.block = tr.dataset.block;
    StudioState.drawerKey = tr.dataset.block;
    studioPanel('crops');
  });
}

/* ── Crops tab: one PDF page at a time; text rows here, the picture in the
   drawer (same slide-in surface the Database bucket uses). ── */
function stBlockPage(b){
  const raw = b.page != null ? b.page : ((b.crop || {}).page);
  const n = parseInt(stEnDigits(String(raw == null ? 1 : raw)).trim(), 10);
  return isNaN(n) ? 1 : n;
}

function stCropPages(){
  const blocks = ((StudioState.bulk || {}).view || {}).blocks || [];
  const byPage = {};
  blocks.forEach(b => { const p = stBlockPage(b); (byPage[p] = byPage[p] || []).push(b); });
  return Object.keys(byPage).map(Number).sort((a, b) => a - b)
    .map(p => ({ page: p, blocks: byPage[p] }));
}

/* provenance: where this block's text and its picture came from */
function stProvenanceChips(b){
  const crop = b.crop || {};
  const chips = [];
  if (crop.source) chips.push('src ' + crop.source);
  if (crop.figure) chips.push('fig ' + crop.figure);
  if (b.organized) chips.push('LLM');
  return chips.map(c => '<span class="st-art plain">'
    + stEsc(c) + '</span>').join('');
}

function stCropRowHtml(b){
  const crop = b.crop || {};
  const num = b.number != null ? b.number : b.order;
  const field = (name, value) => '<label class="st-field"><span>' + name + '</span>'
    + '<input type="text" data-edit="' + name + '" value="'
    + stEsc(value == null ? '' : String(value)) + '"></label>';
  return '<article class="st-crop-row" data-block="' + stEsc(b.key) + '">'
    + '<header class="st-crop-head">'
    +   '<b class="st-crop-id">#' + stNum(num) + '</b>'
    +   '<span class="st-sub">' + stEsc(b.key) + '</span>'
    +   stStatusChip(b.status)
    +   stSplitChip(b)
    +   stProvenanceChips(b)
    +   (crop.needed
        ? '<span class="st-art ' + (crop.approved ? 'on' : 'off') + '">'
          + (crop.approved ? 'cropped' : 'crop') + '</span>'
          + '<button class="ghost-btn neutral sm" data-crop-open="' + stEsc(b.key)
          + '" type="button">picture</button>'
        : '')
    +   ((b.split || b.split_joined)
        ? '<label class="sw"><input type="checkbox" data-split="' + stEsc(b.key) + '"'
          + (b.split ? '' : ' checked') + '><span class="tr" aria-hidden="true"></span>'
          + '<em>joined</em></label>'
        : '')
    +   '<button class="ghost-btn neutral sm st-gap" data-crop-save="' + stEsc(b.key)
    +   '" type="button">Save</button>'
    + '</header>'
    + '<div class="st-crop-grid">'
    +   field('subject', b.subject) + field('topic', b.topic) + field('corp', b.corp)
    +   field('year', b.year) + field('grade', b.grade) + field('difficulty', b.difficulty)
    +   field('number', b.number) + field('label', b.label)
    + '</div>'
    + '<label class="st-field"><span>text</span>'
    +   '<textarea rows="3" dir="auto" data-edit="text">' + stEsc(b.text || '') + '</textarea></label>'
    + '<label class="st-field"><span>options</span>'
    +   '<textarea rows="2" dir="auto" data-edit="options">'
    +   stEsc((b.options || []).join('\n')) + '</textarea></label>'
    + ((b.issues || []).length
        ? '<div class="st-stats">' + b.issues.map(x =>
            '<span class="st-stat warn"><em>issue</em><b>' + stEsc(x) + '</b></span>').join('') + '</div>'
        : '')
    + '</article>';
}

function stCropPagebarHtml(pages, page){
  const first = pages[0].page, last = pages[pages.length - 1].page;
  return '<div class="st-pagebar" id="stCropPagebar">'
    + '<button class="icon-btn sm" data-page="-1" type="button" title="Previous page"'
    +   (page <= first ? ' disabled' : '') + '>' + fi('chevron-left') + '</button>'
    + '<label class="st-pagebox"><input id="stCropPageIn" type="text" inputmode="numeric"'
    +   ' value="' + stNum(page) + '" aria-label="Page"><span>/ ' + stNum(last) + '</span></label>'
    + '<button class="icon-btn sm" data-page="1" type="button" title="Next page"'
    +   (page >= last ? ' disabled' : '') + '>' + fi('chevron-right') + '</button>'
    + '</div>';
}

function studioRenderCrops(){
  const body = $('stCropsBody');
  const d = StudioState.bulk;
  if (!d || d.error){ body.innerHTML = ''; return; }
  const pages = stCropPages();
  if (!pages.length){ body.innerHTML = ''; return; }
  if (!pages.some(p => p.page === StudioState.cropPage)) StudioState.cropPage = pages[0].page;
  const cur = pages.find(p => p.page === StudioState.cropPage);
  const crops = cur.blocks.filter(b => (b.crop || {}).needed);
  const approved = crops.filter(b => (b.crop || {}).approved).length;
  const every = (((StudioState.bulk || {}).view || {}).blocks || []);
  const pendingKeys = every.filter(b => (b.crop || {}).needed
    && (b.crop || {}).approved !== true).map(b => b.key);
  body.innerHTML =
    stStats([['page', stNum(cur.page) + ' / ' + stNum(pages[pages.length - 1].page), ''],
             ['rows', stNum(cur.blocks.length), ''],
             ['crops', stNum(approved) + ' / ' + stNum(crops.length),
              crops.length && approved === crops.length ? 'ok' : (crops.length ? 'warn' : 'plain')]])
    + ((pendingKeys.length)
      ? '<div class="st-tools"><button class="ghost-btn neutral"'
        + ' id="stCropsApproveAll" type="button">Approve all ('
        + stNum(pendingKeys.length) + ')</button></div>'
      : '')
    + '<div class="st-crop-list">' + cur.blocks.map(stCropRowHtml).join('') + '</div>'
    + stCropPagebarHtml(pages, cur.page);
  const approveAll = body.querySelector('#stCropsApproveAll');
  if (approveAll) approveAll.onclick = () => {
    const map = {};
    pendingKeys.forEach(k => { map[k] = { approved: true }; });
    studioSaveReview({ crops: map });
  };
  body.querySelectorAll('[data-crop-open]').forEach(b =>
    b.onclick = () => stCropDrawerOpen(b.dataset.cropOpen));
  body.querySelectorAll('[data-crop-save]').forEach(b =>
    b.onclick = () => stCropRowSave(b.closest('.st-crop-row')));
  body.querySelectorAll('[data-split]').forEach(el =>
    el.onchange = () => studioSaveReview(
      { crops: { [el.dataset.split]: { split_ok: !!el.checked } } }));
  stWireCropPagebar(body, pages);
  if (!$('stCropDrawer').hidden) stCropDrawerPaint();
}

function stWireCropPagebar(body, pages){
  const go = delta => {
    const i = pages.findIndex(p => p.page === StudioState.cropPage);
    const n = Math.max(0, Math.min(pages.length - 1, i + delta));
    if (n !== i) stCropSetPage(pages[n].page);
  };
  const prev = body.querySelector('[data-page="-1"]');
  const next = body.querySelector('[data-page="1"]');
  if (prev) prev.onclick = () => go(-1);
  if (next) next.onclick = () => go(1);
  const inp = $('stCropPageIn');
  if (inp){
    const apply = () => {
      const v = parseInt(stEnDigits(inp.value).trim(), 10);
      const hit = pages.find(p => p.page === v);
      if (hit && v !== StudioState.cropPage) stCropSetPage(v);
      else inp.value = stNum(StudioState.cropPage);
    };
    inp.onchange = apply;
    inp.onkeydown = e => { if (e.key === 'Enter'){ apply(); inp.blur(); } };
  }
}

function stCropSetPage(page){
  const open = !$('stCropDrawer').hidden;
  StudioState.cropPage = page;
  StudioState.drawerKey = null;       // the drawer follows the page
  studioRenderCrops();
  if (open) stCropDrawerPaint();
}

function stCropRowSave(row){
  if (!row) return;
  const key = row.dataset.block;
  const edits = {};
  row.querySelectorAll('[data-edit]').forEach(el => {
    edits[el.dataset.edit] = el.dataset.edit === 'options'
      ? el.value.split('\n').map(s => s.trim()).filter(Boolean)
      : (el.value.trim() || null);
  });
  studioSaveReview({ edits: { [key]: edits } });
}

/* human edits + crop verdicts land in review.json; the merged view and the
   job chips are reloaded because both show the result */
async function studioSaveReview(payload){
  const job = StudioState.job;
  if (!job) return;
  try {
    await studioJson('/api/studio/jobs/' + encodeURIComponent(job) + '/review', payload || {});
    await studioLoadBulk();
    await studioLoadJobs();
    toast('Saved');
  } catch (e){ toast('Save failed: ' + e.message); }
}

/* the bucket drawer, one crop at a time: picture, then its numbers under it */
function stCropDrawerOpen(key){
  StudioState.drawerKey = key;
  stCropDrawerPaint();
}

function stCropDrawerClose(){
  StudioState.drawerKey = null;
  $('stCropDrawer').hidden = true;
}

function stCropDrawerPaint(){
  const drawer = $('stCropDrawer');
  if (!drawer || !StudioState.bulk) return;
  const blocks = (StudioState.bulk.view || {}).blocks || [];
  let b = StudioState.drawerKey ? blocks.find(x => x.key === StudioState.drawerKey) : null;
  if (!b){
    /* a page change (or the first open) picks that page's first crop, so the
       drawer never shows another page's picture */
    b = blocks.find(x => stBlockPage(x) === StudioState.cropPage && (x.crop || {}).needed) || null;
    StudioState.drawerKey = b ? b.key : null;
  }
  if (!b){ drawer.hidden = true; return; }
  const crop = b.crop || {};
  const img = $('stCropDrawerImg');
  const meta = $('stCropDrawerMeta');
  $('stCropDrawerName').textContent = '#' + stNum(b.number != null ? b.number : b.order)
    + ' · page ' + stNum(stBlockPage(b));
  const src = '/api/studio/jobs/' + encodeURIComponent(StudioState.job)
    + '/crop/' + encodeURIComponent(b.key)
    + '?v=' + encodeURIComponent((crop.bbox || []).join(','));
  img.hidden = false;
  if (img.getAttribute('src') !== src) img.src = src;
  img.onerror = () => { img.hidden = true; };
  meta.innerHTML =
    stStats([['crop', crop.approved ? 'approved' : (crop.needed ? 'review' : 'none'),
              crop.approved ? 'ok' : (crop.needed ? 'warn' : 'plain')],
             ['page', stNum(stBlockPage(b)), '']])
    + '<div class="st-bbox-grid">'
    + [0, 1, 2, 3].map(i => '<label class="st-field"><span>bbox ' + i + '</span>'
        + '<input type="text" data-bbox="' + i + '" value="'
        + stEsc(crop.bbox && crop.bbox[i] != null ? String(crop.bbox[i]) : '')
        + '"></label>').join('')
    + '</div>'
    + '<div class="st-crop-actions">'
    + '<button class="ghost-btn neutral" data-crop-approve type="button">Approve</button>'
    + '<button class="ghost-btn neutral" data-crop-reject type="button">Reject</button>'
    + '<button class="ghost-btn neutral st-gap" data-crop-apply type="button">Re-crop</button>'
    + '</div>';
  drawer.hidden = false;
  meta.querySelector('[data-crop-approve]').onclick = () =>
    studioSaveReview({ crops: { [b.key]: { approved: true } } });
  meta.querySelector('[data-crop-reject]').onclick = () =>
    studioSaveReview({ crops: { [b.key]: { approved: false } } });
  meta.querySelector('[data-crop-apply]').onclick = () => stCropDrawerApply(b.key, meta);
}

async function stCropDrawerApply(key, host){
  const bbox = Array.from(host.querySelectorAll('[data-bbox]'))
    .map(el => parseInt(stEnDigits(el.value).trim(), 10))
    .map(v => (isNaN(v) ? null : v));
  try {
    const out = await studioJson('/api/studio/jobs/' + encodeURIComponent(StudioState.job)
      + '/crop/' + encodeURIComponent(key), { bbox: bbox.every(v => v != null) ? bbox : [] });
    toast('Crop uploaded: ' + String(out.object || ''));
    await studioLoadBulk();
  } catch (e){ toast('Crop upload failed: ' + e.message); }
}

/* ── Import panel: target, dry run, continuous import ── */
/* ── Import panel: the rows a run WILL write, as the database grid ──
   Target and scope on top, then one table per row (file · question · picture ·
   verdict) — the reviewer checks the data itself, not a count. */
const StImport = { rows: null, scope: 'job', busy: false };
/* column shares for the rows table: the question text takes the slack */
const ST_IMPORT_COLS = [12, 6, 4, 5, 22, 5, 10, 6, 25, 5];

/* "this file" comes free with the bulk view; "all files" is the only scope
   that needs its own request */
async function studioLoadImportRows(){
  const job = StudioState.job;
  const d = StudioState.bulk || {};
  if (!job){ StImport.rows = null; return; }
  if (StImport.scope !== 'all'){
    StImport.rows = { rows: d.rows || [], targets: d.targets || [],
                      totals: d.row_totals || { rows: 0 } };
    return;
  }
  try {
    StImport.rows = await studioApi('/api/studio/import/rows?job='
      + encodeURIComponent(job) + '&others=1');
  } catch (e){ StImport.rows = { error: e.message }; }
}

function studioRenderImport(){
  const body = $('stImportBody');
  const d = StudioState.bulk;
  if (!d || d.error){ body.innerHTML = ''; return; }
  const data = StImport.rows || {};
  const rows = data.rows || [];
  const targets = (d.targets || []).map(t => '<option value="' + stEsc(t.id) + '">'
    + stEsc(t.label) + '</option>').join('');
  const scopeOpt = (v, l) => '<option value="' + v + '"'
    + (v === StImport.scope ? ' selected' : '') + '>' + stEsc(l) + '</option>';
  const counts = [['rows', rows.length],
    ['ready', rows.filter(r => r.ok).length],
    ['blocked', rows.filter(r => !r.ok).length],
    ['with picture', rows.filter(r => r.has_picture).length]];
  const body_rows = rows.map((r, i) => {
    const pic = r.picture === 'none' ? '—'
      : '<span class="st-art ' + (r.picture === 'approved' ? 'on' : 'off') + '">'
        + stEsc(r.picture) + '</span>';
    return '<tr class="st-imp-row' + (r.ok ? '' : ' blocked') + '">'
      + '<td class="mono">' + stEsc(r.job_name || r.job) + '</td>'
      + '<td class="mono">' + stEsc(r.key || '') + '</td>'
      + '<td class="mono">' + stEsc(r.number != null ? String(r.number) : '—') + '</td>'
      + '<td class="mono">' + stEsc(r.page != null ? String(r.page) : '—') + '</td>'
      + '<td class="st-imp-text" dir="auto">'
      +   (r.kind === 'answer_key_entry'
           ? '<span class="st-art off">answer key</span> ' : '')
      +   stEsc(String(r.text || '').slice(0, 300)) + '</td>'
      + '<td>' + (r.options || []).length + '</td>'
      + '<td>' + pic + '</td>'
      + '<td>' + (r.answered ? '<span class="st-art on">answer</span>' : '—') + '</td>'
      + '<td>' + (r.ok ? '<span class="st-art on">ready</span>'
          : '<span class="st-art off">' + stEsc(r.why || 'blocked') + '</span>') + '</td>'
      + '<td>' + (r.result ? stStatusChip(r.result === 'ok' ? 'ready' : r.result) : '—') + '</td></tr>';
  }).join('');
  body.innerHTML =
    '<div class="st-col">'
    + '<div class="st-import-bar">'
    + '<label class="st-field"><span>target</span><select id="stImportTarget">'
    + targets + '</select></label>'
    + '<label class="st-field"><span>rows from</span><select id="stImportScope">'
    + scopeOpt('job', 'this file') + scopeOpt('all', 'all files') + '</select></label>'
    + '<label class="sw"><input type="checkbox" id="stImportDry">'
    + '<span class="tr" aria-hidden="true"></span><em>dry run</em></label>'
    + '<span class="st-gap"></span>' + (stStats(counts) || '')
    + '<button class="ghost-btn neutral sm" id="stImportRun" type="button">Import</button>'
    + '</div>'
    + '<div class="st-warn" id="stImportErr" hidden></div>'
    + '<div class="st-table-wrap st-import-table">'
    + '<table class="st-table"><colgroup>'
    + ST_IMPORT_COLS.map(w => '<col style="width:' + w + '%">').join('')
    + '</colgroup><thead><tr>'
    + '<th>File</th><th>Blk</th><th>#</th><th>pg</th><th>question</th>'
    + '<th>opts</th><th>picture</th><th>ans</th><th>verdict</th><th>run</th>'
    + '</tr></thead><tbody>' + (body_rows || '') + '</tbody></table></div></div>';
  $('stImportRun').onclick = () => studioImportRun();
  $('stImportScope').onchange = async e => {
    StImport.scope = e.target.value;
    await studioLoadImportRows();
    studioRenderImport();
  };
  typesetMath(body);
}

async function studioImportRun(){
  const id = StudioState.job;
  if (!id) return;
  const target = $('stImportTarget').value || 'supabase';
  const dry = $('stImportDry').checked;
  const err = $('stImportErr');
  stBusy(true, 'importing…');
  try {
    const d = await studioJson('/api/studio/jobs/' + encodeURIComponent(id) + '/import',
      { target: target, dry_run: dry });
    toast('Import: ' + stStageNote(d));
    await studioLoadJobs();
    await studioLoadBulk();
  } catch (e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Import failed: ' + e.message);
  } finally { stBusy(false); }
}

/* ── live events (wired from app.js `handle`) ── */
const ST_RAIL = ['intake','probe','extract','enrich','geometry','review','import'];

function stPages(ev){
  const a = ev.page_start, b = ev.page_end;
  if (a == null && b == null) return '';
  return 'page ' + stNum(a == null ? b : a)
    + (b != null && b !== a ? '–' + stNum(b) : '');
}

function stChunks(ev){
  if (ev.chunk == null || ev.chunks == null) return '';
  return 'chunk ' + stNum(ev.chunk) + '/' + stNum(ev.chunks);
}

function stRailActive(stage){
  ST_RAIL.forEach(s => {
    const el = document.querySelector('#stStages .stage[data-stage="' + s + '"]');
    if (el) el.classList.toggle('active', s === stage);
  });
}

function studioFeed(ev){
  if (!StudioState.loaded) return;
  if (ev.job && StudioState.job === ev.job){
    $('stNowStage').textContent = (ev.stage || '').toUpperCase();
    $('stNowMsg').textContent = ev.message || '';
    $('stNow').dataset.state = ev.level === 'error' ? 'err' : 'live';
    const page = stPages(ev), chunk = stChunks(ev);
    $('stNowPage').textContent = page;
    $('stNowPage').hidden = !page;
    $('stNowChunk').textContent = chunk;
    $('stNowChunk').hidden = !chunk;
    stRailActive(ev.stage);
  }
  if (ev.job && ST_LOG.job === ev.job) stLogLine(ev);
  if (ev.job && ev.level === 'success'){
    studioLoadJobs().catch(() => {});
    if (StudioState.job === ev.job){
      studioLoadBulk().catch(() => {});
      studioSelectJob(ev.job, { keepPanel:true }).catch(() => {});
    }
  }
}

/* ═══════════ LIVE LOG · the studio's own event stream ═══════════
   The pipeline tab wears a log panel; the studio gets the same surface.
   Live websocket events stream into it, the job's own log.jsonl rebuilds
   the history after a reload, and an error line always carries the fault
   the backend classified - a chip, the advice and the raw text - so a
   failure is never a bare red sentence. */
const ST_LOG = { job:null, faults:{}, max:400 };

function stLogNow(){
  const d = new Date(), p = n => String(n).padStart(2, '0');
  return p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}

function stLogTime(ts){
  const m = String(ts || '').match(/(\d{2}:\d{2}:\d{2})/);
  return m ? m[1] : '';
}

/* the fault chip - same .ek skin and colour groups as the pipeline log */
function stLogFaultChip(flt){
  const chip = document.createElement('span');
  chip.className = 'ek ' + (flt.group || 'misc');
  chip.textContent = (flt.emoji ? flt.emoji + ' ' : '')
    + (flt.label || flt.kind || 'fault');
  chip.title = flt.hint || '';
  return chip;
}

/* one log line: a plain row, or a diagnostic card when a fault is attached */
function stLogLine(row){
  const box = $('stLog');
  if (!box) return;
  const flt = row.fault || null;
  const line = document.createElement('div');
  const t = document.createElement('span');
  t.className = 't';
  t.textContent = stLogTime(row.ts) || stLogNow();
  if (flt){
    line.className = 'line diag ' + (flt.group || 'misc');
    const m = document.createElement('span');
    m.className = 'm';
    const head = document.createElement('b');
    head.textContent = row.message || flt.label || '';
    m.appendChild(head);
    const where = document.createElement('span');
    where.className = 'where';
    where.textContent = ' @ ' + (row.stage || 'studio');
    m.appendChild(where);
    if (flt.hint){
      const raw = document.createElement('span');
      raw.className = 'raw';
      raw.textContent = flt.hint;
      m.appendChild(raw);
    }
    line.append(t, stLogFaultChip(flt), m);
    if (flt.raw) line.title = flt.raw;
  } else {
    line.className = 'line lv-' + (row.level || 'info');
    const m = document.createElement('span');
    m.className = 'm';
    m.textContent = row.message || '';
    line.append(t, m);
  }
  box.appendChild(line);
  while (box.children.length > ST_LOG.max) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
  $('stLogCount').textContent = box.querySelectorAll('.line').length + ' events';
  if (flt) stLogFaultTally(flt);
}

/* the fault tally pill - the pipeline's, counted per kind */
function stLogFaultTally(flt){
  const kind = flt.kind || 'unknown';
  ST_LOG.faults[kind] = (ST_LOG.faults[kind] || 0) + 1;
  const el = $('stDiagCount');
  const total = Object.values(ST_LOG.faults).reduce((a, b) => a + b, 0);
  if (!total){ el.hidden = true; return; }
  el.textContent = '· ' + total + ' fault' + (total > 1 ? 's' : '');
  el.title = Object.entries(ST_LOG.faults).sort((a, b) => b[1] - a[1])
    .map(([k, n]) => k.replace(/_/g, ' ') + ' — ' + n).join('  ·  ');
  el.hidden = false;
}

/* a studio-side failure: the panel itself could not do its job */
function stLogFail(message, hint){
  stLogLine({ ts:stLogNow(), stage:'log', level:'error', message:message,
              fault:{ kind:'ui', label:'LOG ERROR', group:'misc',
                      hint:hint || '', raw:message } });
}

function stLogReset(jobId){
  ST_LOG.job = jobId || null;
  ST_LOG.faults = {};
  const box = $('stLog');
  if (box) box.innerHTML = '';
  $('stLogCount').textContent = '';
  $('stDiagCount').hidden = true;
}

/* the job's own history - log.jsonl, rebuilt after a reload */
async function stLogLoad(jobId){
  stLogReset(jobId);
  if (!jobId) return;
  try {
    const d = await studioApi('/api/studio/jobs/' + encodeURIComponent(jobId)
      + '/log?limit=300');
    (d.lines || []).forEach(row => stLogLine(row));
  } catch (e){
    stLogFail('log history unavailable: ' + e.message);
  }
}

/* the panel's own controls */
function stLogWire(){
  $('stLogCopyBtn').onclick = async () => {
    const txt = ($('stLog').innerText || '').trim();
    if (!txt){ toast('Log is empty'); return; }
    try { await navigator.clipboard.writeText(txt); toast('Log copied'); }
    catch (e){ toast('Copy failed — ' + e); }
  };
  $('stLogClearBtn').onclick = () => {
    const n = $('stLog').querySelectorAll('.line').length;
    stLogReset(ST_LOG.job);
    if (n) toast(n + ' event(s) cleared');
  };
  $('stLogToggle').onclick = () => {
    $('stLogPanel').classList.toggle('collapsed');
  };
}
