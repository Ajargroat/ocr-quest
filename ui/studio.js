/* ═══════════ STUDIO · any-file OCR subsystem ═══════════
   The whole chain is live: upload → probe → profile → extract (layer1) →
   enrich (layer2) → locate crops → human review → import. Bulk reads the
   merged view, Crops is the review desk (bbox + fields + approve/reject),
   Import writes whatever is ready to the chosen target.

   Loaded after app.js, so it reuses dbApi / dbEsc / toast / viewLoading and
   the WS dispatcher's `studioFeed` hook. */
const StudioState = { loaded:false, wired:false, jobs:[], job:null, detail:null,
                      tags:null, tagDraft:null, models:null, bulk:null, block:null,
                      tab:'jobs', cropPage:null, drawerKey:null,
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
}

/* stage tabs · exactly one pane is visible; #studio/<tab> keeps the deep link
   working (app.js panelFromHash) */
const ST_TABS = ['jobs', 'profile', 'bulk', 'crops', 'tags', 'models', 'import'];

function studioPanel(name, silent){
  if (!ST_TABS.includes(name)) name = 'jobs';
  StudioState.tab = name;
  document.querySelectorAll('#stTabs .rail-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.sttab === name));
  document.querySelectorAll('.st-pane').forEach(p =>
    p.hidden = p.dataset.sttab !== name);
  if (name === 'tags') studioRenderTags();
  if (name === 'models') studioRenderModels();
  if (name === 'bulk' || name === 'crops' || name === 'import') studioLoadBulk();
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
    const chips = ['profile','layer1','layer2','geometry','review','imports']
      .map(k => '<span class="st-art ' + (art[k] ? 'on' : 'off') + '">' + k + '</span>')
      .join('');
    const active = StudioState.job === j.id ? ' active' : '';
    return '<tr class="st-row' + active + '" data-job="' + stEsc(j.id) + '">'
      + '<td><b>' + stEsc(j.name) + '</b><span class="st-sub">' + stEsc(j.id) + '</span></td>'
      + '<td>' + (j.kind ? '<span class="chip s-ocr">' + stEsc(j.kind) + '</span>' : '—') + '</td>'
      + '<td>' + stEsc(j.subject || '—') + '</td>'
      + '<td>' + stEsc((j.compilation || '—').replace(/_/g, ' ')) + '</td>'
      + '<td class="st-art-cell">' + chips + '</td>'
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
  studioLoadBulk();
  stSetRail(d.job);
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

function studioRenderModels(){
  const body = $('stModelsBody');
  const m = StudioState.models;
  if (!m || m.error || !m.providers){
    body.innerHTML = m && m.error ? '<div class="db-empty db-err">' + stEsc(m.error) + '</div>' : '';
    return;
  }
  const rows = (m.providers || []).map((p, i) =>
    '<div class="st-tagrow"><b>' + stEsc(p.label || '') + '</b>'
    + '<span class="st-art ' + (p.api_key_set ? 'on' : 'off') + '">'
    + (p.api_key_set ? 'key set' : 'no key') + '</span>'
    + '<input type="password" data-mkey="' + i + '" value="" aria-label="api key" autocomplete="off">'
    + '<input type="text" dir="ltr" data-mmodel="' + i + '" value="' + stEsc(p.model || '') + '" aria-label="model">'
    + '<input type="text" dir="ltr" data-mmodels="' + i + '" value="' + stEsc((p.models || []).join(', ')) + '" aria-label="models">'
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
    + pick(labels, m.enrich_active, 'none') + '</select></label>'
    + '<label class="st-field"><span>locate</span><select data-mvision>'
    + pick(labels, m.vision_active, 'Gemini pool') + '</select></label></div>'
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
      enrich_active: sel('data-menrich'), vision_active: sel('data-mvision'),
      proxy: sel('data-mproxy') });
    StudioState.models = d;
    studioRenderModels();
    toast('Studio models saved.');
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
    studioRenderBulk(); studioRenderCrops(); studioRenderImport();
    return;
  }
  try {
    StudioState.bulk = await studioApi('/api/studio/jobs/'
      + encodeURIComponent(id) + '/bulk');
  } catch (e){ StudioState.bulk = { error: e.message }; }
  studioRenderBulk(); studioRenderCrops(); studioRenderImport();
}

async function studioRunStage(stage, label){
  const id = StudioState.job;
  if (!id) return;
  stBusy(true, label + ' running…');
  try {
    const d = await studioApi('/api/studio/jobs/' + encodeURIComponent(id)
      + '/' + stage, { method:'POST' });
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
    + '<button class="ghost-btn neutral" id="stRunEnrich" type="button">Enrich</button>'
    + '<button class="ghost-btn neutral" id="stRunGeometry" type="button">Locate</button>'
    + '</div></div>';
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
  body.innerHTML =
    stStats([['page', stNum(cur.page) + ' / ' + stNum(pages[pages.length - 1].page), ''],
             ['rows', stNum(cur.blocks.length), ''],
             ['crops', stNum(approved) + ' / ' + stNum(crops.length),
              crops.length && approved === crops.length ? 'ok' : (crops.length ? 'warn' : 'plain')]])
    + '<div class="st-crop-list">' + cur.blocks.map(stCropRowHtml).join('') + '</div>'
    + stCropPagebarHtml(pages, cur.page);
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
function studioRenderImport(){
  const body = $('stImportBody');
  const d = StudioState.bulk;
  if (!d || d.error){ body.innerHTML = ''; return; }
  const view = d.view || {};
  const blocks = view.blocks || [];
  const reasons = {};
  blocks.forEach(b => {
    const crop = b.crop || {};
    let why = null;
    if (b.kind !== 'question' && b.kind !== 'answer_key_entry') why = 'not a question';
    else if (b.status === 'error') why = 'flagged';
    else if (crop.needed && !crop.approved) why = 'crop awaiting approval';
    if (why) reasons[why] = (reasons[why] || 0) + 1;
  });
  const queuedPairs = Object.keys(reasons)
    .map(k => [k, stNum(reasons[k]), 'warn']);
  const queued = queuedPairs.length
    ? stStats([['queued', stNum(Object.values(reasons).reduce((a, b) => a + b, 0)), '']]
        .concat(queuedPairs)) : '';
  const targets = (d.targets || []).map(t => '<option value="' + stEsc(t.id) + '">'
    + stEsc(t.label) + '</option>').join('');
  const runs = (d.runs || []).slice().reverse().map(r =>
    '<tr><td>' + stFmt(r.at) + '</td><td>' + stEsc(r.target || '') + '</td>'
    + '<td>' + stNum(r.questions || 0) + '</td>'
    + '<td>' + stNum(r.answers || 0) + '</td>'
    + '<td>' + stNum((r.skipped || []).length) + '</td>'
    + '<td>' + (r.dry_run ? 'dry' : '—') + '</td></tr>').join('');
  body.innerHTML =
    '<div class="st-col">'
    + '<div class="st-profile-grid">'
    + '<label class="st-field"><span>target</span><select id="stImportTarget">'
    + targets + '</select></label>'
    + '<label class="sw"><input type="checkbox" id="stImportDry">'
    + '<span class="tr" aria-hidden="true"></span><em>dry run</em></label>'
    + '</div>'
    + queued
    + (runs
      ? '<div class="st-table-wrap"><table class="st-table"><thead><tr><th>When</th><th>Target</th><th>Q</th>'
        + '<th>A</th><th>Queued</th><th></th></tr></thead><tbody>' + runs
        + '</tbody></table></div>'
      : '')
    + '<div class="st-profile-foot">'
    + '<span class="st-warn" id="stImportErr" hidden></span>'
    + '<button class="ghost-btn neutral" id="stImportRun" type="button">Import</button>'
    + '</div></div>';
  $('stImportRun').onclick = () => studioImportRun();
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
  if (ev.job && ev.level === 'success'){
    studioLoadJobs().catch(() => {});
    if (StudioState.job === ev.job){
      studioLoadBulk().catch(() => {});
      studioSelectJob(ev.job, { keepPanel:true }).catch(() => {});
    }
  }
}
