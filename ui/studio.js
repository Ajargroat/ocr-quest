/* ═══════════ STUDIO · any-file OCR subsystem ═══════════
   Phase 1 wires Stage 0 end to end: upload a file → store + probe → an
   editable profile that pins the fields we already know. The later stage
   panels (Bulk/Crops/Models/Import) render placeholders until their phase
   lands; the rail and job list are already real.

   Loaded after app.js, so it reuses dbApi / dbEsc / toast / viewLoading and
   the WS dispatcher's `studioFeed` hook. */
const StudioState = { loaded:false, wired:false, jobs:[], job:null, detail:null, tags:null, tagDraft:null, models:null };
const ST_KEEP = '__KEEP__';   // same sentinel the credentials tab sends (main.py KEEP)

const studioApi = dbApi;                 // same fetch+json contract
const studioJson = (url, body) => studioApi(url, {
  method:'POST', headers:{'content-type':'application/json'},
  body: JSON.stringify(body),
});
const stEsc = dbEsc;

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
  return isNaN(d) ? String(iso) : d.toLocaleString(undefined,
    { year:'numeric', month:'short', day:'2-digit', hour:'2-digit', minute:'2-digit' });
}

async function studioInit(){
  studioWireOnce();
  try {
    await Promise.all([studioLoadJobs(), studioLoadTags(), studioLoadModels()]);
  } catch (e){
    $('stJobs').innerHTML = '<div class="db-empty db-err">Studio unreachable — '
      + stEsc(e.message) + '</div>';
  }
}

function studioWireOnce(){
  if (StudioState.wired) return;
  StudioState.wired = true;

  $('stPanels').querySelectorAll('button').forEach(b => b.onclick = () => {
    $('stPanels').querySelectorAll('button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    studioPanel(b.dataset.spanel);
  });
  $('stRefresh').onclick = () => studioLoadJobs();
  $('stFile').onchange = e => {
    const f = e.target.files && e.target.files[0];
    if (f) studioUpload(f);
    e.target.value = '';
  };
}

function studioPanel(name, silent){
  ['jobs','profile','bulk','crops','tags','models','import'].forEach(p => {
    const el = $('stPanel' + p.charAt(0).toUpperCase() + p.slice(1));
    if (el) el.hidden = (p !== name);
  });
  $('stPanels').querySelectorAll('button').forEach(x =>
    x.classList.toggle('active', x.dataset.spanel === name));
  if (name === 'tags') studioRenderTags();
  if (name === 'models') studioRenderModels();
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

function studioRenderJobs(){
  const wrap = $('stJobs');
  const jobs = StudioState.jobs;
  if (!jobs.length){
    wrap.innerHTML = '';
    return;
  }
  const rows = jobs.map(j => {
    const art = j.artifacts || {};
    const chips = ['profile','layer1','layer2','review']
      .map(k => '<span class="st-art ' + (art[k] ? 'on' : 'off') + '">' + k + '</span>')
      .join('');
    const active = StudioState.job === j.id ? ' active' : '';
    return '<tr class="st-row' + active + '" data-job="' + stEsc(j.id) + '">'
      + '<td><b>' + stEsc(j.name) + '</b><span class="st-sub">' + stEsc(j.id) + '</span></td>'
      + '<td>' + (j.kind ? '<span class="chip s-ocr">' + stEsc(j.kind) + '</span>' : '—') + '</td>'
      + '<td>' + stEsc(j.subject || '—') + '</td>'
      + '<td>' + stEsc((j.compilation || '—').replace(/_/g, ' ')) + '</td>'
      + '<td class="st-art-cell">' + chips + '</td>'
      + '<td>' + (j.blocks != null ? faDigits(j.blocks) : '—') + '</td>'
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
  wrap.querySelectorAll('.st-row').forEach(tr => tr.onclick = () => {
    studioSelectJob(tr.dataset.job);
  });
}

async function studioSelectJob(id, opts){
  StudioState.job = id;
  studioRenderJobs();
  const d = await studioApi('/api/studio/jobs/' + encodeURIComponent(id));
  StudioState.detail = d;
  $('stCrumb').textContent = d.job ? d.job.name : id;
  studioRenderProfile();
  if (!(opts && opts.keepPanel)){
    studioPanel('profile');
    $('stPanels').querySelectorAll('button').forEach(x =>
      x.classList.toggle('active', x.dataset.spanel === 'profile'));
  }
  stSetRail(d.job);
}

function stSetRail(job){
  if (!job) return;
  const art = job.artifacts || {};
  const map = { intake: true, probe: !!art.profile, extract: !!art.layer1,
                enrich: !!art.layer2, review: !!art.review };
  Object.entries(map).forEach(([stage, done]) => {
    const el = document.querySelector('#stStages .stage[data-stage="' + stage + '"]');
    if (el) el.classList.toggle('done', done);
  });
  $('stNowStage').textContent = (job.kind || 'IDLE').toUpperCase();
  $('stNowMsg').textContent = job.name + ' — ' + (job.compilation || 'unknown')
    + (job.blocks != null ? ' · ' + faDigits(job.blocks) + ' blocks' : '');
  $('stNow').dataset.state = job.kind ? 'done' : 'idle';
}

/* ── Profile panel ── */
function studioRenderProfile(){
  const d = StudioState.detail;
  const body = $('stProfileBody');
  if (!d || !d.profile){
    body.innerHTML = '';
    return;
  }
  const p = d.profile, st = p.static || {}, pr = d.probe || {};
  const missing = d.required_missing || [];

  const opt = (list, cur) => list.map(v =>
    '<option value="' + stEsc(v) + '"' + (v === cur ? ' selected' : '') + '>'
    + stEsc(v.replace(/_/g,' ')) + '</option>').join('');

  const fieldRow = f => {
    const pinned = st[f] != null && st[f] !== '';
    return '<label class="st-field' + (pinned ? ' pinned' : '') + '">'
      + '<span>' + f + (pinned ? ' · pinned' : '') + '</span>'
      + '<input type="text" data-field="' + f + '" value="' + stEsc(st[f] || '') + '"'
      + ' placeholder="' + (pinned ? '' : 'extract per question') + '">'
      + '</label>';
  };

  const probeLine = pr && pr.ok
    ? (pr.page_count + ' pages · text ' + (pr.text_pages||0) + ' / image ' + (pr.image_pages||0)
       + ' · answer-key pages ' + ((pr.answer_key_pages||[]).length)
       + ' · figures on ' + ((pr.figure_pages||[]).length) + ' page(s)')
    : 'probe not run';

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
    +   '<p class="hint">' + stEsc(probeLine) + '</p>'
    + '</div>'
    + '<div class="st-col">'
    +   '<h3>Static fields (known — never guessed)</h3>'
    +   ST_FIELDS.map(fieldRow).join('')
    + '</div></div>'
    + '<div class="st-profile-foot">'
    +   (missing.length
        ? '<span class="st-warn">will be extracted: ' + stEsc(missing.join(', ')) + '</span>'
        : '<span class="hint">every field is pinned — extraction has nothing to guess.</span>')
    +   '<button class="ghost-btn neutral" id="stReProbe" type="button">re-probe</button>'
    +   '<button class="ghost-btn neutral" id="stSaveProfile" type="button">Save profile</button>'
    + '</div>';

  $('stSaveProfile').onclick = () => studioSaveProfile();
  $('stReProbe').onclick = () => studioReprobe();
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
    await studioLoadJobs();
    toast('Job deleted.');
  } catch (e){ toast('Delete failed: ' + e.message); }
}

/* ── Tags panel (global editor — one shared list for every job) ── */
async function studioLoadTags(){
  try {
    const d = await studioApi('/api/studio/tags');
    StudioState.tags = d;
    StudioState.tagDraft = d.tags ? JSON.parse(JSON.stringify(d.tags)) : null;
  } catch (e){ StudioState.tags = { error: e.message }; StudioState.tagDraft = null; }
}

function studioRenderTags(){
  const body = $('stTagsBody');
  const draft = StudioState.tagDraft;
  if (!draft){
    const err = (StudioState.tags && StudioState.tags.error) || '';
    body.innerHTML = err ? '<div class="db-empty db-err">' + stEsc(err) + '</div>' : '';
    return;
  }
  const subjRows = (draft.subjects || []).map((s, i) =>
    '<div class="st-tagrow"><input type="text" data-subj="' + i + '" value="' + stEsc(s) + '" aria-label="subject">'
    + '<button class="ghost-btn neutral sm" data-del-subj="' + i + '" type="button">remove</button></div>').join('');
  const corpRows = (draft.corps || []).map((c, i) =>
    '<div class="st-tagrow"><input type="text" data-ctag="' + i + '" value="' + stEsc(c.tag || '') + '" aria-label="canonical">'
    + '<input type="text" data-cvar="' + i + '" value="' + stEsc((c.variants || []).join(', ')) + '" aria-label="variants">'
    + '<button class="ghost-btn neutral sm" data-del-corp="' + i + '" type="button">remove</button></div>').join('');
  const chRows = (draft.chapters || []).map((c, i) =>
    '<div class="st-tagrow col"><div class="st-tagrow">'
    + '<input type="text" data-hsubj="' + i + '" value="' + stEsc(c.subject || '') + '" aria-label="subject">'
    + '<input type="text" data-hgrade="' + i + '" value="' + stEsc(c.grade || '') + '" aria-label="grade">'
    + '<input type="text" data-hnum="' + i + '" value="' + stEsc(c.chapter == null ? '' : String(c.chapter)) + '" aria-label="chapter">'
    + '<button class="ghost-btn neutral sm" data-del-ch="' + i + '" type="button">remove</button></div>'
    + '<textarea data-htop="' + i + '" rows="2">' + stEsc((c.topics || []).join('\n')) + '</textarea></div>').join('');
  body.innerHTML =
    '<div class="st-col"><h3>Subjects</h3><div>' + subjRows + '</div>'
    + '<button class="ghost-btn neutral sm" data-add-subj type="button">add</button></div>'
    + '<div class="st-col"><h3>Institutes</h3><div>' + corpRows + '</div>'
    + '<button class="ghost-btn neutral sm" data-add-corp type="button">add</button></div>'
    + '<div class="st-col"><h3>Chapters · topics</h3><div>' + chRows + '</div>'
    + '<button class="ghost-btn neutral sm" data-add-ch type="button">add</button></div>'
    + '<div class="st-profile-foot"><span class="st-warn" data-tags-err hidden></span>'
    + '<label class="st-upload"><i class="fa fa-file-arrow-up" aria-hidden="true"></i><span>Upload file</span>'
    + '<input type="file" data-tags-file accept=".json,application/json"></label>'
    + '<button class="ghost-btn neutral" data-tags-save type="button">Save tags</button></div>';
  body.onclick = e => {
    const b = e.target.closest('button');
    if (!b || !body.contains(b)) return;
    if (b.hasAttribute('data-add-subj')){ (draft.subjects = draft.subjects || []).push(''); studioRenderTags(); }
    else if (b.hasAttribute('data-del-subj')){ draft.subjects.splice(+b.dataset.delSubj, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-add-corp')){ (draft.corps = draft.corps || []).push({ tag: '', variants: [] }); studioRenderTags(); }
    else if (b.hasAttribute('data-del-corp')){ draft.corps.splice(+b.dataset.delCorp, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-add-ch')){ (draft.chapters = draft.chapters || []).push({ subject: '', grade: '', chapter: '', topics: [] }); studioRenderTags(); }
    else if (b.hasAttribute('data-del-ch')){ draft.chapters.splice(+b.dataset.delCh, 1); studioRenderTags(); }
    else if (b.hasAttribute('data-tags-save')){ studioSaveTags(); }
  };
  const file = body.querySelector('[data-tags-file]');
  if (file) file.onchange = e => {
    const f = e.target.files && e.target.files[0];
    if (f) studioUploadTags(f);
    e.target.value = '';
  };
}

function studioReadTagsDraft(){
  const body = $('stTagsBody');
  const draft = StudioState.tagDraft;
  draft.subjects = Array.from(body.querySelectorAll('[data-subj]'))
    .map(el => el.value.trim()).filter(Boolean);
  draft.corps = Array.from(body.querySelectorAll('[data-ctag]')).map(el => {
    const v = body.querySelector('[data-cvar="' + el.dataset.ctag + '"]');
    return { tag: el.value.trim(),
             variants: (v ? v.value : '').split(',').map(s => s.trim()).filter(Boolean) };
  }).filter(c => c.tag);
  draft.chapters = Array.from(body.querySelectorAll('[data-hsubj]')).map(el => {
    const i = el.dataset.hsubj;
    const val = name => { const n = body.querySelector('[data-' + name + '="' + i + '"]'); return n ? n.value.trim() : ''; };
    const top = body.querySelector('[data-htop="' + i + '"]');
    return { subject: el.value.trim(), grade: val('hgrade'), chapter: val('hnum'),
             topics: (top ? top.value : '').split('\n').map(s => s.trim()).filter(Boolean) };
  }).filter(c => c.subject || c.topics.length);
}

async function studioSaveTags(){
  studioReadTagsDraft();
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
    toast('File loaded — press Save tags to apply.');
  } catch (e){ fail('Upload failed: ' + e.message); }
}

/* ── Models panel (studio-local providers + per-stage picks + proxy) ── */
async function studioLoadModels(){
  try {
    const d = await studioApi('/api/studio/models');
    StudioState.models = d;
  } catch (e){ StudioState.models = { error: e.message }; }
}

function studioRenderModels(){
  const body = $('stModelsBody');
  const m = StudioState.models;
  if (!m || m.error || !m.providers){
    body.innerHTML = m && m.error ? '<div class="db-empty db-err">' + stEsc(m.error) + '</div>' : '';
    return;
  }
  const rows = (m.providers || []).map((p, i) =>
    '<div class="st-tagrow"><b>' + stEsc(p.label || '') + '</b>'
    + '<input type="password" data-mkey="' + i + '" value="" placeholder="'
    + (p.api_key_set ? '••••••' : 'API key') + '" aria-label="api key" autocomplete="off">'
    + '<input type="text" data-mmodel="' + i + '" value="' + stEsc(p.model || '') + '" placeholder="model" aria-label="model">'
    + '<input type="text" data-mmodels="' + i + '" value="' + stEsc((p.models || []).join(', ')) + '" placeholder="models" aria-label="models">'
    + '<button class="ghost-btn neutral sm" data-del-prov="' + i + '" type="button">remove</button></div>').join('');
  const pick = (labels, cur, emptyLabel) =>
    '<option value="">' + stEsc(emptyLabel) + '</option>' + labels.map(l =>
      '<option value="' + stEsc(l) + '"' + (l === cur ? ' selected' : '') + '>' + stEsc(l) + '</option>').join('');
  const labels = (m.providers || []).map(p => p.label).filter(Boolean);
  body.innerHTML =
    '<div class="st-col"><h3>Providers</h3><div>' + rows + '</div>'
    + '<button class="ghost-btn neutral sm" data-add-prov type="button">add 9router</button></div>'
    + '<div class="st-col"><h3>Stages</h3>'
    + '<label class="st-field"><span>extract</span><select data-mextract>'
    + pick(labels, m.extract_active, 'Gemini pool') + '</select></label>'
    + '<label class="st-field"><span>enrich</span><select data-menrich>'
    + pick(labels, m.enrich_active, 'none') + '</select></label></div>'
    + '<div class="st-col"><h3>Proxy</h3>'
    + '<label class="st-field"><span>proxy</span><select data-mproxy>'
    + [''].concat(m.proxy_profiles || []).map(p =>
        '<option value="' + stEsc(p) + '"' + (p === (m.proxy || '') ? ' selected' : '') + '>'
        + stEsc(p || 'Direct') + '</option>').join('') + '</select></label></div>'
    + '<div class="st-profile-foot"><span class="st-warn" data-models-err hidden></span>'
    + '<button class="ghost-btn neutral" data-models-save type="button">Save models</button></div>';
  body.onclick = e => {
    const b = e.target.closest('button');
    if (!b || !body.contains(b)) return;
    if (b.hasAttribute('data-add-prov')){
      const cur = StudioState.models.providers || [];
      if (!cur.some(p => p.label === '9router'))
        cur.push({ label: '9router', base_url: 'http://127.0.0.1:20128/v1',
                   api_key_set: false, model: '', models: [] });
      studioRenderModels();
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
             models: at('data-mmodels', i).split(',').map(s => s.trim()).filter(Boolean) };
  });
  const sel = attr => { const el = body.querySelector('[' + attr + ']'); return el ? el.value : ''; };
  const err = body.querySelector('[data-models-err]');
  try {
    const d = await studioJson('/api/studio/models', {
      providers, extract_active: sel('data-mextract'),
      enrich_active: sel('data-menrich'), proxy: sel('data-mproxy') });
    StudioState.models = d;
    studioRenderModels();
    toast('Studio models saved.');
  } catch (e){
    if (err){ err.hidden = false; err.textContent = e.message; }
    else toast('Save failed: ' + e.message);
  }
}

/* ── live events (wired from app.js `handle`) ── */
function studioFeed(ev){
  if (!StudioState.loaded) return;
  if (ev.job && StudioState.job === ev.job){
    $('stNowStage').textContent = (ev.stage || '').toUpperCase();
    $('stNowMsg').textContent = ev.message || '';
    $('stNow').dataset.state = ev.level === 'error' ? 'err' : 'live';
  }
  if (ev.stage === 'probe' && ev.job){
    studioLoadJobs().catch(() => {});
  }
}
