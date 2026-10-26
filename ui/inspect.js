/* ═══════════ INSPECT · staged OCR results, reviewed before any import ═══════════
   Nothing on this page has touched Postgres or the bucket. A run writes the
   local staging store (/api/staging/*), a human corrects the rows here, marks
   them ready, and the Import tab is the only thing that writes the live bank.

   Three stages, one rail: Sources -> Review -> Import. Review lists the rows a
   source produced; a row opens in a window of its own with the page picture on
   one side and a single document editor on the other (text, formulas and
   tables in the same surface, see inspect_doc.js). The bbox editor draws the
   stored box over the page in the OCR's own 0-1000 frame, so a re-cut is a
   pure local operation. */
window.InspectState = {
  loaded: false,
  stage: 'sources',
  counts: {},
  sources: [],
  source: null,          // the open source (questions + answers)
  record: null,          // the open record
  page: 1,
  filter: '',            // '' | status
  onlyBox: false,        // show only rows carrying a box
  vocab: {corps: [], subjects: [], difficulty: [], topics: []},
  drag: null,
  zoom: 1,               // page magnification, 1 = fit the pane
  dirty: false,          // the editor holds unsaved keystrokes
  openedAt: null,        // a click that opened the window never closes it
};

const INS_STATUS = {
  empty:    {label: 'empty',    colour: 'var(--amber)'},
  staged:   {label: 'staged',   colour: 'var(--muted)'},
  edited:   {label: 'edited',   colour: 'var(--cyan)'},
  ready:    {label: 'ready',    colour: 'var(--green)'},
  imported: {label: 'imported', colour: 'var(--green)'},
  skipped:  {label: 'skipped',  colour: 'var(--red)'},
};

const ins = id => document.getElementById(id);
const insDigits = n => String(n ?? 0).replace(/[۰-۹]/g, d => '۰۱۲۳۴۵۶۷۸۹'.indexOf(d));

async function insApi(path, options){
  const r = await fetch('/api/staging' + path, options);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  if (!r.ok || (body && body.ok === false)){
    const message = (body && body.error) || ('HTTP ' + r.status);
    throw new Error(message);
  }
  return body;
}

function insPost(path, payload){
  return insApi(path, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload || {}),
  });
}

function insStatus(message){
  const box = ins('insState');
  if (box) box.textContent = message || '';
}

/* The four digits the store keeps are ASCII; a row typed as `۱۲` updates the
   same key. */
const insToAscii = value => String(value ?? '').replace(/[۰-۹]/g,
  d => String('۰۱۲۳۴۵۶۷۸۹'.indexOf(d)));

/* ── stage rail ─────────────────────────────────────────────── */
function inspectStage(stage, silent){
  if (!['sources', 'review', 'import'].includes(stage)) stage = 'sources';
  InspectState.stage = stage;
  document.querySelectorAll('#insRail .rail-btn').forEach(b =>
    b.classList.toggle('active', b.dataset.ins === stage));
  document.querySelectorAll('#viewInspect .ins-pane').forEach(pane =>
    pane.hidden = pane.dataset.ins !== stage);
  if (silent) return;
  if (stage === 'sources') insLoadSources();
  if (stage === 'review') insRenderRecords();
  if (stage === 'import') insLoadImport();
}

/* ── sources ────────────────────────────────────────────────── */
async function insLoadCounts(){
  const body = await insApi('/counts');
  InspectState.counts = body.counts || {};
  const row = box => {
    const total = Object.values(box || {}).reduce((a, b) => a + b, 0);
    const ready = (box || {}).ready || 0;
    const empty = (box || {}).empty || 0;
    return {total, ready, empty};
  };
  const src = row(InspectState.counts.st_sources);
  const q = row(InspectState.counts.st_questions);
  const a = row(InspectState.counts.st_answers);
  /* One card per kind, in the order the rail reads. A note only appears when
     it says something ("0 ready" is noise dressed as information). */
  ins('insCounts').innerHTML = [
    ['Sources', src.total, src.ready],
    ['Questions', q.total, q.ready],
    ['Answers', a.total, a.ready],
    ['Empty', src.empty + q.empty + a.empty, 0],
  ].map(([label, value, ready]) =>
    `<div class="ins-count"><span class="ins-count-label">${label}</span>` +
    `<span class="ins-count-value">${insDigits(value)}</span>` +
    (ready ? `<span class="ins-count-note">${insDigits(ready)} ready</span>` : '') +
    `</div>`).join('');
}

function insSourceUrl(sourceId){
  return '/sources/' + encodeURIComponent(sourceId);
}

async function insLoadSources(quiet){
  if (!quiet) insStatus('Reading the staging store…');
  try {
    const [list] = await Promise.all([insApi('/sources?limit=500'), insLoadCounts()]);
    InspectState.sources = list.sources || [];
    ins('insSources').setAttribute('dir', 'ltr');
    const tbody = ins('insSources').querySelector('tbody');
    if (!InspectState.sources.length){
      tbody.innerHTML = '<tr><td colspan="9" class="ins-none">—</td></tr>';
    } else {
      tbody.innerHTML = InspectState.sources.map(s => {
        const st = INS_STATUS[s.status] || INS_STATUS.staged;
        return `<tr class="ins-src-row" data-sid="${s.source_id}">
          <td class="mono ins-path-cell" title="${insEscape(s.rel_path || s.source_id)}">${insEscape(s.rel_path || '')}</td>
          <td>${insEscape(s.subject || '')}</td>
          <td>${insEscape(s.year || '')}</td>
          <td>${insEscape(s.corp || '')}</td>
          <td>${insDigits(s.page_count || 0)}</td>
          <td>${insDigits(s.question_count || 0)}</td>
          <td>${insDigits(s.answer_count || 0)}</td>
          <td><span class="chip ins-chip" style="color:${st.colour}">${st.label}</span></td>
          <td><button class="icon-btn sm" data-open="${s.source_id}" title="Open">
            <i class="fa fa-arrow-up-right-from-square" aria-hidden="true"></i></button></td>
        </tr>`;
      }).join('');
    }
    insStatus(InspectState.sources.length + ' source(s) staged');
  } catch (err){
    insStatus(err.message);
  }
}

/* ── one source ─────────────────────────────────────────────── */
async function insOpenSource(sourceId){
  try {
    const body = await insApi(insSourceUrl(sourceId));
    InspectState.source = body;
    InspectState.page = 1;
    ins('insPath').textContent = body.rel_path || sourceId;
    ins('insPath').title = body.rel_path || sourceId;
    inspectStage('review', true);
    insRenderPages();
    insRenderRecords();
    insStatus(body.rel_path || sourceId);
  } catch (err){
    insStatus(err.message);
  }
}

function insRenderPages(){
  const source = InspectState.source;
  if (!source) return;
  const total = source.page_count || 1;
  let html = '';
  for (let p = 1; p <= total; p++){
    html += `<button class="ins-page-btn" data-page="${p}" type="button">${insDigits(p)}</button>`;
  }
  ins('insPages').innerHTML = total > 1 ? html : '';
  /* the strip is a sequence of numbers, not prose: keep 1 on the left */
  ins('insPages').setAttribute('dir', 'ltr');
  ins('insPages').hidden = total <= 1;
  insShowPage(InspectState.page);
  ins('insPageNo').textContent = insDigits(InspectState.page) + ' / ' + insDigits(total);
}

function insShowPage(page){
  const source = InspectState.source;
  if (!source) return;
  const total = source.page_count || 1;
  page = Math.min(Math.max(1, Number(page) || 1), total);
  InspectState.page = page;
  const img = ins('insPageImg');
  img.hidden = false;
  ins('insPageNone').hidden = true;
  img.onerror = () => { img.hidden = true; ins('insPageNone').hidden = false; };
  img.onload = () => { ins('insPageNone').hidden = true; insSyncStage(); };
  /* A re-selected page keeps its cached bitmap, so `onload` never fires and
     the box would stay stale - draw now as well as on load. */
  insSyncStage();
  img.src = '/api/staging/page/' + encodeURIComponent(source.source_id) + '/' + page;
  document.querySelectorAll('#insPages .ins-page-btn').forEach(b =>
    b.classList.toggle('active', Number(b.dataset.page) === page));
}

/* ── the row list ───────────────────────────────────────────────
   Every staged row of the source, questions first, each carrying three facts
   at a glance: its number, its status, and whether a box (the picture) was
   assigned. The list is the index into the editor window. */
function insRows(){
  const source = InspectState.source;
  const rows = [];
  if (!source) return rows;
  (source.questions || []).forEach(q => rows.push({kind: 'question', row: q}));
  (source.answers || []).forEach(a => rows.push({kind: 'answer', row: a}));
  const filter = InspectState.filter;
  return rows.filter(({row}) => {
    if (filter && row.status !== filter) return false;
    if (InspectState.onlyBox && !insHasBox(row)) return false;
    return true;
  });
}

function insHasBox(row){
  if (!row) return false;
  if (row.bbox) return true;
  const fields = row.fields || {};
  return Array.isArray(fields.bbox) && fields.bbox.length === 4;
}

/* the box a row carries, wherever the OCR wrote it */
function insRecordBox(row){
  if (!row) return null;
  if (Array.isArray(row.bbox) && row.bbox.length === 4) return row.bbox.slice();
  const fields = row.fields || {};
  if (Array.isArray(fields.bbox) && fields.bbox.length === 4) return fields.bbox.slice();
  return null;
}

async function insRenderRecords(quiet){
  if (quiet && InspectState.source){
    // Re-read the OPEN source so a background refresh shows new rows in the
    // Review pane instead of a snapshot from when it was opened.
    try {
      const body = await insApi(insSourceUrl(InspectState.source.source_id));
      if (body.source) InspectState.source = body.source;
    } catch (err){ /* keep what is on screen */ }
  }
  if (quiet) return;                // a live refresh never re-renders the list
  const source = InspectState.source;
  const box = ins('insRecords');
  if (!source){ box.innerHTML = '<div class="ins-none">—</div>'; return; }
  const rows = insRows();
  if (!rows.length){ box.innerHTML = '<div class="ins-none">—</div>'; return; }
  box.innerHTML = rows.map(({kind, row}) => {
    const st = INS_STATUS[row.status] || INS_STATUS.staged;
  const text = kind === 'question' ? row.question_text : row.answer_explanation;
  /* a list row is a glance, not a formula sheet: the stored Markdown is shown
     as its words (a `$...$` fence would read as raw LaTeX in a 140-char line) */
  const plain = String(text || '').replace(/\$\$?/g, ' ').replace(/\\[()[\]]/g, ' ')
    .replace(/\s+/g, ' ').trim();
    const boxed = insHasBox(row);
    const link = kind === 'answer' && row.match_state
      ? `<span class="ins-match">${insEscape(row.match_state)}</span>` : '';
    const no = insDigits(row.question_number ?? '');
    return `<button class="ins-row${boxed ? ' has-box' : ''}" data-kind="${kind}" data-id="${row.id}" type="button">
      <span class="ins-row-kind">${kind === 'question' ? 'Q' : 'A'}</span>
      <span class="ins-row-no">${no}</span>
      <span class="ins-row-text" dir="auto">${insEscape(plain.slice(0, 140))}</span>
      ${link}
      <span class="ins-row-tools">
        <span class="ins-box-dot" title="picture assigned"></span>
        <span class="chip ins-chip" style="color:${st.colour}">${st.label}</span>
        <i class="fa fa-up-right-and-down-left-from-center" aria-hidden="true"></i>
      </span>
    </button>`;
  }).join('');
}

function insFindRecord(kind, id){
  const source = InspectState.source;
  if (!source) return null;
  const list = kind === 'question' ? source.questions : source.answers;
  return (list || []).find(r => r.id === id) || null;
}

/* ── the editor window ──────────────────────────────────────── */
function insOpenRecord(kind, id){
  const raw = insFindRecord(kind, id);
  if (!raw) return;
  const record = {...raw};
  record.kind = kind;
  const fields = record.fields || {};
  record.subject = fields.subject || record.subject || '';
  record.topic = fields.topic || '';
  record.corp = fields.corp || '';
  record.year = fields.year || '';
  record.grade = fields.grade ?? '';
  record.tags = Array.isArray(record.tags) && record.tags.length
    ? record.tags.slice()
    : (Array.isArray(fields.tags) ? fields.tags.slice() : []);
  record.bbox = insRecordBox(record);
  InspectState.record = record;
  InspectState.dirty = false;

  ins('insRecKind').textContent = kind === 'question' ? 'Question' : 'Answer';
  ins('insRecId').textContent = record.id;
  insPaintStatus(record);

  /* one document: the stem, or the explanation */
  InspDoc.setText(kind === 'question'
    ? (record.question_text || '')
    : (record.answer_explanation || ''));
  insRenderOptions(record.options || []);
  ins('insDocOpts').hidden = kind !== 'question';

  ins('insSubject').value = record.subject;
  ins('insTopic').value = record.topic;
  ins('insCorp').value = record.corp;
  ins('insYear').value = record.year;
  ins('insGrade').value = record.grade === null || record.grade === undefined
    ? '' : insDigits(record.grade);
  ins('insDifficulty').value = fields.difficulty || '';
  insRenderTags(record.tags);
  insRenderCrop(record);

  const linkField = ins('insLinkField');
  if (kind === 'answer'){
    linkField.hidden = false;
    insFillLink(record);
  } else {
    linkField.hidden = true;
  }

  insShowPage(record.page || InspectState.page || 1);
  insDrawBbox();
  insSyncBboxInputs();
  insCenterBox();
  insValidateVocab();

  ins('insEdit').hidden = false;
  InspectState.openedAt = Date.now();
  document.body.classList.add('ins-modal');
  document.querySelectorAll('#insRecords .ins-row').forEach(b =>
    b.classList.toggle('active',
      b.dataset.kind === kind && b.dataset.id === id));
  const active = document.querySelector('#insRecords .ins-row.active');
  if (active && active.scrollIntoView) active.scrollIntoView({block: 'nearest'});
  if (window.InspDoc) InspDoc.scheduleTypeset();
}

function insPaintStatus(record){
  const st = INS_STATUS[record.status] || INS_STATUS.staged;
  const chip = ins('insRecStatus');
  chip.hidden = false;
  chip.textContent = st.label;
  chip.style.color = st.colour;
  const flag = ins('insBoxFlag');
  flag.hidden = !record.bbox;
}

function insCloseRecord(force){
  if (!InspectState.record) return;
  if (!force && InspectState.dirty && !window.confirm('Discard the unsaved edit?')) return;
  InspectState.dirty = false;
  InspectState.record = null;
  ins('insEdit').hidden = true;
  document.body.classList.remove('ins-modal');
  document.querySelectorAll('#insRecords .ins-row.active').forEach(b =>
    b.classList.remove('active'));
  insStatus('');
}

function insStep(dir){
  const record = InspectState.record;
  if (!record) return;
  const rows = insRows();
  const index = rows.findIndex(r => r.row.id === record.id);
  const next = rows[index + dir];
  if (next){
    if (InspectState.dirty) insSaveRecord().then(() => insOpenRecord(next.kind, next.row.id));
    else insOpenRecord(next.kind, next.row.id);
  }
}

function insFillLink(record){
  const candidates = record.candidates || [];
  const options = ['<option value="">—</option>'];
  candidates.forEach(c => {
    const label = `#${insDigits(c.question_number ?? '?')} · ${c.corp || '—'} · ${c.year || '—'}`;
    const selected = String(c.id) === String(record.link_id) ? ' selected' : '';
    options.push(`<option value="${c.id}"${selected}>${insEscape(label)}</option>`);
  });
  ins('insLink').innerHTML = options.join('');
  ins('insLink').value = record.link_id == null ? '' : String(record.link_id);
}

/* ── tags ────────────────────────────────────────────────────────
   A tag is a word the vocabulary knows; anything else is accepted on the spot
   (the vocabulary is a helper, never a gate). Value and label are the same
   word, so a tag reads as plain text inside its chip. */
function insRenderTags(tags){
  const box = ins('insTags');
  const list = Array.isArray(tags) ? tags : [];
  box.innerHTML = list.map((tag, index) => `
    <span class="ins-tag">
      <span class="ins-tag-text">${insEscape(tag)}</span>
      <button class="ins-tag-x" type="button" data-tag="${index}" title="Remove">
        <i class="fa fa-xmark" aria-hidden="true"></i></button>
    </span>`).join('') +
    `<input class="ins-tag-in" id="insTagInput" dir="auto" spellcheck="false"
            aria-label="Tag">`;
  box.querySelectorAll('.ins-tag-x').forEach(button => {
    button.onclick = () => {
      const current = InspectState.record;
      if (!current) return;
      current.tags.splice(Number(button.dataset.tag), 1);
      insRenderTags(current.tags);
      InspectState.dirty = true;
      ins('insTagInput').focus();
    };
  });
  const input = ins('insTagInput');
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' || event.key === ',' || event.key === '،'){
      event.preventDefault();
      insAddTag(input.value);
    } else if (event.key === 'Backspace' && !input.value){
      const current = InspectState.record;
      if (current && current.tags.length){
        current.tags.pop();
        insRenderTags(current.tags);
        InspectState.dirty = true;
        ins('insTagInput').focus();
      }
    }
  });
  input.addEventListener('blur', () => { if (input.value.trim()) insAddTag(input.value); });
}

function insAddTag(value){
  const record = InspectState.record;
  const tag = String(value || '').trim().replace(/[,،]+$/, '').trim();
  if (!record || !tag) return;
  if (!record.tags.includes(tag)) record.tags.push(tag);
  InspectState.dirty = true;
  insRenderTags(record.tags);
  const input = ins('insTagInput');
  if (input) input.focus();
}

/* ── choices ─────────────────────────────────────────────────────
   The choices are their own rows INSIDE the document page, never part of the
   question text: an OCR reply that glued them to the stem, or that answered
   with label-only stubs, is repaired here by adding/removing/editing rows.
   A choice's text is typeset like the stem — formulas become chips, not raw
   LaTeX in an <input>. */
function insRenderOptions(options){
  const list = ins('insDocOpts');
  const rows = Array.isArray(options) ? options : [];
  list.classList.toggle('empty', !rows.length);
  list.innerHTML = rows.map((option, index) => `
    <div class="ins-opt" data-index="${index}">
      <input class="ins-opt-label" value="${insEscape(option.label ?? (index + 1))}"
             dir="ltr" aria-label="Label" spellcheck="false">
      <div class="ins-opt-text" contenteditable="true" dir="auto"
           aria-label="Choice text" spellcheck="false">${InspDoc.choiceHtml(option.text ?? '')}</div>
      <button class="icon-btn sm ins-opt-del" type="button" title="Remove this choice">
        <i class="fa fa-trash" aria-hidden="true"></i></button>
    </div>`).join('');
  list.querySelectorAll('.ins-opt').forEach(row => {
    const index = Number(row.dataset.index);
    const label = row.querySelector('.ins-opt-label');
    const text = row.querySelector('.ins-opt-text');
    label.addEventListener('input', () => insOptionChanged(index, row));
    InspDoc.choiceInit(text, value => {
      const opts = insOptions();
      if (!opts[index]) return;
      opts[index].text = value;
      InspectState.dirty = true;
    });
    /* Enter in the last choice adds the next one — the table-row reflex */
    text.addEventListener('keydown', event => {
      if (event.key !== 'Enter') return;
      event.preventDefault();
      const all = row.parentElement.querySelectorAll('.ins-opt');
      if (row === all[all.length - 1]) insAddOption();
      else all[index + 1].querySelector('.ins-opt-text').focus();
    });
    text.addEventListener('dblclick', event => {
      const chip = event.target.closest('.ins-math');
      if (chip) InspDoc.editMath(chip);
    });
    row.querySelector('.ins-opt-del').onclick = () => insRemoveOption(index);
  });
  if (rows.length && window.InspDoc) InspDoc.scheduleTypeset(list);
}

function insOptions(){
  const record = InspectState.record;
  if (!record) return [];
  if (!Array.isArray(record.options)) record.options = [];
  return record.options;
}

/* the label is a plain <input>; the choice text writes itself (InspDoc) */
function insOptionChanged(index, row){
  const options = insOptions();
  if (!options[index]) return;
  options[index].label = row.querySelector('.ins-opt-label').value;
  InspectState.dirty = true;
}function insRemoveOption(index){
  const options = insOptions();
  if (!options[index]) return;
  options.splice(index, 1);
  InspectState.dirty = true;
  insRenderOptions(options);
}

function insAddOption(){
  const options = insOptions();
  options.push({label: String(options.length + 1), text: ''});
  InspectState.dirty = true;
  insRenderOptions(options);
  const rows = ins('insDocOpts').querySelectorAll('.ins-opt-text');
  const last = rows[rows.length - 1];
  if (last) last.focus();
}

/* ── the row's picture ──────────────────────────────────────── */
function insRenderCrop(record){
  const img = ins('insCropImg');
  const none = ins('insCropNone');
  if (!img) return;
  if (record && record.crop_url){
    img.src = record.crop_url + '?v=' + (record.updated_at || Date.now());
    img.hidden = false;
    none.hidden = true;
  } else {
    img.removeAttribute('src');
    img.hidden = true;
    none.hidden = false;
  }
}

/* ── bbox editor ────────────────────────────────────────────────
   Gemini's own box is the starting point. It can be dragged from any edge or
   corner, and the four numbers under the page are the box itself. A box always
   normalises to [ymin,xmin,ymax,xmax] on the OCR's 0-1000 frame, so re-cutting
   is a pure local operation. */
function insBboxPx(box, width, height){
  const [ymin, xmin, ymax, xmax] = box;
  return {
    left: (xmin / 1000) * width,
    top: (ymin / 1000) * height,
    width: Math.max(2, ((xmax - xmin) / 1000) * width),
    height: Math.max(2, ((ymax - ymin) / 1000) * height),
  };
}

function insDrawBbox(){
  insLayoutBbox();
}

function insPointToNorm(event, img){
  const rect = img.getBoundingClientRect();
  const x = Math.min(Math.max(event.clientX - rect.left, 0), rect.width);
  const y = Math.min(Math.max(event.clientY - rect.top, 0), rect.height);
  return [Math.round((y / rect.height) * 1000), Math.round((x / rect.width) * 1000)];
}

/* The eight handles and the number fields all write through here. */
function insSetBbox(box){
  const record = InspectState.record;
  if (!record) return;
  const clean = insNormalizeBox(box);
  if (!clean) return;
  record.bbox = clean;
  InspectState.dirty = true;
  insDrawBbox();
  insSyncBboxInputs();
  insPaintStatus(record);
}

function insNormalizeBox(box){
  if (!Array.isArray(box) || box.length !== 4) return null;
  const nums = box.map(v => {
    const n = Number(v);
    return Number.isFinite(n) ? Math.min(1000, Math.max(0, Math.round(n))) : null;
  });
  if (nums.some(n => n === null)) return null;
  const [a, b, c, d] = nums;
  const ymin = Math.min(a, c), ymax = Math.max(a, c);
  const xmin = Math.min(b, d), xmax = Math.max(b, d);
  if (ymax - ymin < 4 || xmax - xmin < 4) return null;
  return [ymin, xmin, ymax, xmax];
}

const INS_BBOX_FIELDS = [
  ['insBboxYmin', 0],
  ['insBboxXmin', 1],
  ['insBboxYmax', 2],
  ['insBboxXmax', 3],
];

function insSyncBboxInputs(){
  const record = InspectState.record;
  const box = record && Array.isArray(record.bbox) ? record.bbox : null;
  INS_BBOX_FIELDS.forEach(([field, index]) => {
    const input = ins(field);
    const value = box ? box[index] : '';
    if (input && document.activeElement !== input) input.value = value;
  });
}

function insReadBboxInputs(){
  const box = INS_BBOX_FIELDS.map(([field]) => {
    const input = ins(field);
    return input ? Number(input.value) : 0;
  });
  return insNormalizeBox(box);
}

function insWireBbox(){
  const img = ins('insPageImg');
  const box = ins('insBbox');
  if (!img || !box) return;

  /* Drag from an edge/corner handle, or draw a fresh box on the page. */
  const start = event => {
    const record = InspectState.record;
    if (!record || img.hidden) return;
    const handle = event.target.closest('.ins-h');
    if (handle){
      InspectState.drag = {
        mode: 'resize', handle: handle.dataset.h,
        box: (record.bbox || [0, 0, 0, 0]).slice(),
      };
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (!event.target.closest('#insPageImg')) return;
    const [ymin, xmin] = insPointToNorm(event, img);
    InspectState.drag = {mode: 'draw', ymin, xmin, ymax: ymin, xmax: xmin};
    record.bbox = [ymin, xmin, ymin, xmin];
    box.hidden = false;
    event.preventDefault();
  };

  const move = event => {
    const drag = InspectState.drag;
    if (!drag) return;
    const [y, x] = insPointToNorm(event, img);
    if (drag.mode === 'draw'){
      drag.ymax = y; drag.xmax = x;
      insSetBbox([Math.min(drag.ymin, drag.ymax), Math.min(drag.xmin, drag.xmax),
                  Math.max(drag.ymin, drag.ymax), Math.max(drag.xmin, drag.xmax)]);
      return;
    }
    /* Resize: each handle moves only the edges it names. */
    let [ymin, xmin, ymax, xmax] = drag.box;
    const h = drag.handle;
    if (h.includes('n')) ymin = y;
    if (h.includes('s')) ymax = y;
    if (h.includes('w')) xmin = x;
    if (h.includes('e')) xmax = x;
    insSetBbox([ymin, xmin, ymax, xmax]);
  };

  const end = () => {
    if (!InspectState.drag) return;
    InspectState.drag = null;
  };
  img.addEventListener('mousedown', start);
  box.addEventListener('mousedown', event => {
    const handle = event.target.closest('.ins-h');
    if (!handle) return;
    start(event);
  });
  window.addEventListener('mousemove', move);
  window.addEventListener('mouseup', end);

  INS_BBOX_FIELDS.forEach(([field]) => {
    const input = ins(field);
    if (!input) return;
    input.addEventListener('input', () => {
      const next = insReadBboxInputs();
      if (next) insSetBbox(next);
    });
  });

  ins('insBboxClear').onclick = () => insClearBbox();
  ins('insBboxApply').onclick = insSaveBbox;
  ins('insShowBox').addEventListener('change', insDrawBbox);
}

/* Saving the box writes it on the record AND re-cuts the picture, because a
   box with a stale crop beside it is the state that confuses a reviewer. */
async function insSaveBbox(){
  const record = InspectState.record;
  if (!record) return;
  if (!record.bbox || record.bbox.length !== 4){
    await insClearBbox();
    return;
  }
  try {
    const body = await insPost('/recrop', {
      source_id: record.source_id, kind: record.kind, id: record.id,
      bbox: record.bbox, index: 0,
    });
    insMergeRecord(record.kind, body.record);
    insStatus('box saved · picture re-cut');
  } catch (err){
    /* A row with no stored page still keeps its box — the crop is the only
       thing that cannot be produced. */
    try {
      const body = await insPost(
        `/records/${record.kind}/${encodeURIComponent(record.id)}`,
        {bbox: record.bbox});
      insMergeRecord(record.kind, body.record);
      insStatus('box saved · ' + err.message);
    } catch (inner){
      insStatus(inner.message);
    }
  }
}

async function insClearBbox(){
  const record = InspectState.record;
  if (!record) return;
  try {
    const body = await insPost(
      `/records/${record.kind}/${encodeURIComponent(record.id)}`,
      {bbox: null});
    record.bbox = null;
    insMergeRecord(record.kind, body.record);
    insStatus('no box');
  } catch (err){
    insStatus(err.message);
  }
}

/* ── vocabulary ─────────────────────────────────────────────── */
function insValidateVocab(){
  const warn = ins('insVocabWarn');
  const corps = InspectState.vocab.corps || [];
  const subjects = InspectState.vocab.subjects || [];
  const problems = [];
  const corp = ins('insCorp').value.trim();
  const subject = ins('insSubject').value.trim();
  if (corp && corps.length && !corps.includes(corp)) problems.push('corp: ' + corp);
  if (subject && subjects.length && !subjects.includes(subject))
    problems.push('subject: ' + subject);
  if (!problems.length){ warn.hidden = true; warn.innerHTML = ''; return; }
  warn.hidden = false;
  warn.innerHTML = problems.map(p =>
    `<span class="ins-warn"><i class="fa fa-triangle-exclamation" aria-hidden="true"></i>` +
    `${insEscape(p)}</span>`).join('') +
    (corp && problems.some(p => p.startsWith('corp'))
      ? ` <button class="ghost-btn neutral sm" id="insAlias" type="button">Accept alias</button>`
      : '');
  if (ins('insAlias')) ins('insAlias').onclick = insAcceptAlias;
}

async function insAcceptAlias(){
  const alias = ins('insCorp').value.trim();
  const tag = (InspectState.vocab.corps || []).find(c => c === alias) || alias;
  try {
    await insPost('/vocab/alias', {alias, tag});
    ins('insVocabWarn').hidden = true;
    insStatus('alias accepted');
  } catch (err){
    insStatus(err.message);
  }
}

function insEscape(value){
  return String(value ?? '').replace(/[&<>"']/g, ch =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
}

/* ── writing a record ───────────────────────────────────────── */
function insPayload(){
  const record = InspectState.record;
  if (!record) return null;
  InspDoc.tidyTex();
  const text = InspDoc.toText(ins('insDocEditor'));
  const payload = {
    subject: ins('insSubject').value.trim() || null,
    topic: ins('insTopic').value.trim() || null,
    corp: ins('insCorp').value.trim() || null,
    year: ins('insYear').value.trim() || null,
    difficulty: ins('insDifficulty').value.trim() || null,
    tags: (record.tags || []).slice(),
  };
  const grade = Number(insToAscii(ins('insGrade').value.trim()));
  if (Number.isFinite(grade) && grade > 0) payload.grade = grade;
  if (record.kind === 'question'){
    payload.question_text = text;
    /* Choices travel with the row, cleaned of empty rows — an empty option is
       never written as a choice. */
    payload.options = insOptions()
      .map((o, i) => ({label: String(o.label ?? (i + 1)).trim() || String(i + 1),
                       text: String(o.text ?? '').trim()}))
      .filter(o => o.text);
  } else {
    payload.answer_explanation = text;
  }
  payload.bbox = record.bbox && record.bbox.length === 4 ? record.bbox : null;
  if (record.kind === 'answer'){
    payload.link_id = ins('insLink').value || null;
    payload.link_kind = 'staged';
    payload.match_state = 'manual_ok';
    payload.match_method = 'manual';
  }
  Object.keys(payload).forEach(k => { if (payload[k] === null && k !== 'bbox') delete payload[k]; });
  return payload;
}

async function insSaveRecord(){
  const record = InspectState.record;
  if (!record) return;
  const payload = insPayload();
  try {
    const body = await insPost(`/records/${record.kind}/${encodeURIComponent(record.id)}`,
                               payload);
    InspectState.dirty = false;
    insMergeRecord(record.kind, body.record);
    insStatus('saved');
  } catch (err){
    insStatus(err.message);
  }
}

async function insSetStatus(status){
  const record = InspectState.record;
  if (!record) return;
  try {
    /* a status press keeps whatever was typed: it saves first */
    if (InspectState.dirty) await insSaveRecord();
    const body = await insPost(
      `/records/${record.kind}/${encodeURIComponent(record.id)}/status?value=${status}`);
    InspectState.dirty = false;
    insMergeRecord(record.kind, body.record);
    insStatus(status);
  } catch (err){
    insStatus(err.message);
  }
}

function insMergeRecord(kind, updated){
  const source = InspectState.source;
  if (!source || !updated) return;
  const list = kind === 'question' ? source.questions : source.answers;
  const index = (list || []).findIndex(r => r.id === updated.id);
  if (index >= 0) list[index] = updated;
  insRenderRecords();
  insOpenRecord(kind, updated.id);
}

async function insReadyAll(){
  const source = InspectState.source;
  if (!source) return;
  try {
    const body = await insPost(insSourceUrl(source.source_id) + '/ready');
    insStatus(body.moved + ' row(s) marked ready');
    await insOpenSource(source.source_id);
  } catch (err){
    insStatus(err.message);
  }
}

/* ── import stage ───────────────────────────────────────────── */
async function insLoadImport(quiet){
  try {
    const body = await insApi('/counts');
    const c = body.counts || {};
    const readyQ = (c.st_questions || {}).ready || 0;
    const readyA = (c.st_answers || {}).ready || 0;
    const readyS = (c.st_sources || {}).ready || 0;
    ins('insImportBody').innerHTML =
      `<div class="ins-import-row"><span>Sources ready</span>` +
      `<span class="mono">${insDigits(readyS)}</span></div>` +
      `<div class="ins-import-row"><span>Questions ready</span>` +
      `<span class="mono">${insDigits(readyQ)}</span></div>` +
      `<div class="ins-import-row"><span>Answers ready</span>` +
      `<span class="mono">${insDigits(readyA)}</span></div>`;
    ins('insImportState').textContent = '';
  } catch (err){
    ins('insImportState').textContent = err.message;
  }
}

async function insRunImport(){
  const state = ins('insImportState');
  state.textContent = 'importing…';
  try {
    const body = await insPost('/import');
    state.textContent = `${body.imported || 0} imported, ${body.failed || 0} failed`;
    insLoadImport();
  } catch (err){
    state.textContent = err.message;
  }
}

let InspLiveTimer = null;

function inspLiveDebounce(){
  clearTimeout(InspLiveTimer);
  InspLiveTimer = setTimeout(() => { InspLiveTimer = null; inspectRefresh(true); }, 700);
}

/* Re-read whatever stage is showing. `quiet` skips the "reading…" line, so a
   background refresh never overwrites a message the reviewer is reading — and
   never rebuilds the list (or the open row) under the reviewer's hands. */
async function inspectRefresh(quiet){
  if (!quiet) insStatus('Reading the staging store…');
  if (InspectState.stage === 'sources') return insLoadSources(quiet);
  if (InspectState.stage === 'review') return insRenderRecords(quiet);
  if (InspectState.stage === 'import') return insLoadImport(quiet);
}
window.inspectRefresh = inspectRefresh;
window.inspLiveDebounce = inspLiveDebounce;

/* ── wiring ─────────────────────────────────────────────────── */
async function inspectInit(){
  try {
    const vocab = await insApi('/vocab');
    InspectState.vocab = {
      corps: vocab.corps || [], subjects: vocab.subjects || [],
      topics: vocab.topics || [], difficulty: ['ساده', 'متوسط', 'دشوار'],
    };
    ins('insVocabCorps').innerHTML = InspectState.vocab.corps
      .map(c => `<option value="${insEscape(c)}"></option>`).join('');
    ins('insVocabSubjects').innerHTML = InspectState.vocab.subjects
      .map(s => `<option value="${insEscape(s)}"></option>`).join('');
    ins('insVocabDifficulty').innerHTML = InspectState.vocab.difficulty
      .map(d => `<option value="${insEscape(d)}"></option>`).join('');
  } catch (err){
    /* the vocabulary is a helper, not a gate */
  }
  InspDoc.init({
    element: ins('insDocEditor'),
    onInput: () => { InspectState.dirty = true; },
  });
  document.querySelectorAll('.ins-tool[data-cmd]').forEach(b => {
    b.onmousedown = event => event.preventDefault();     // the caret stays put
    b.onclick = () => InspDoc.run(b.dataset.cmd);
  });
  document.querySelectorAll('.ins-tool[data-ins-cmd]').forEach(b => {
    b.onmousedown = event => event.preventDefault();
    b.onclick = () => InspDoc.run(b.dataset.insCmd);
  });
  InspectState.inited = true;
  insWireBbox();
  insWireZoom();
  document.querySelectorAll('#insRail .rail-btn').forEach(b =>
    b.onclick = () => inspectStage(b.dataset.ins));
  ins('insRefresh').onclick = () => inspectStage(InspectState.stage);
  ins('insBack').onclick = () => {
    insCloseRecord(true);
    inspectStage('sources');
  };
  ins('insSources').addEventListener('click', event => {
    const btn = event.target.closest('[data-open]');
    if (btn) { event.stopPropagation(); insOpenSource(btn.dataset.open); return; }
    const row = event.target.closest('tr[data-sid]');
    if (row) insOpenSource(row.dataset.sid);
  });
  ins('insRecords').addEventListener('click', event => {
    const btn = event.target.closest('.ins-row');
    if (btn) insOpenRecord(btn.dataset.kind, btn.dataset.id);
  });
  ins('insFilter').addEventListener('click', event => {
    const btn = event.target.closest('[data-filter]');
    if (!btn) return;
    InspectState.filter = btn.dataset.filter || '';
    ins('insFilter').querySelectorAll('.rail-btn').forEach(b =>
      b.classList.toggle('active', b === btn));
    insRenderRecords();
  });
  ins('insOnlyBox').addEventListener('change', event => {
    InspectState.onlyBox = event.target.checked;
    insRenderRecords();
  });
  ins('insPages').addEventListener('click', event => {
    const btn = event.target.closest('[data-page]');
    if (btn) insShowPage(Number(btn.dataset.page));
  });
  ins('insPagePrev').onclick = () => insShowPage(Math.max(1, InspectState.page - 1));
  ins('insPageNext').onclick = () =>
    insShowPage(Math.min(InspectState.source ? (InspectState.source.page_count || 1) : 1,
                         InspectState.page + 1));
  ins('insOptAdd').onclick = insAddOption;
  ins('insRecut').onclick = insSaveBbox;
  ins('insSave').onclick = insSaveRecord;
  ins('insMarkReady').onclick = () => insSetStatus('ready');
  ins('insSkip').onclick = () => insSetStatus('skipped');
  ins('insReadyAll').onclick = insReadyAll;
  ins('insImportRun').onclick = insRunImport;
  ins('insEditClose').onclick = () => insCloseRecord(false);
  /* a click on the dim ground outside the window closes it — but the very
     click that OPENED the window must not travel on and close it again */
  ins('insEdit').addEventListener('mousedown', event => {
    if (event.target !== ins('insEdit')) return;
    if (Date.now() - (InspectState.openedAt || 0) < 400) return;
    insCloseRecord(false);
  });
  document.addEventListener('keydown', event => {
    if (ins('insEdit').hidden) return;
    if (event.key === 'Escape'){
      if (document.querySelector('.ins-math-input')) return;   // the chip editor handles it
      if (event.target === ins('insDocEditor') && InspectState.dirty) return;
      event.preventDefault();
      insCloseRecord(false);
    }
    /* Ctrl+S saves the open row, like every document tool */
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's'){
      event.preventDefault();
      insSaveRecord();
    }
  });
  ins('insDocEditor').addEventListener('keydown', event => {
    if (event.key === 'Escape' && !InspectState.dirty) insCloseRecord(true);
  });
  ['insCorp', 'insSubject'].forEach(id =>
    ins(id).addEventListener('input', insValidateVocab));

  await insLoadSources();
}

/* ── page geometry ──────────────────────────────────────────────
   The image IS a box in the page: its on-screen rectangle is the 0-1000 frame
   every stored bbox is measured in, so the overlay is placed from the image's
   own rect and needs no DOM math of its own. At 1x the whole page fits its
   frame; a zoom gives the page a real height (from its aspect ratio) and lets
   the frame scroll. */
function insApplyZoom(){
  const wrap = ins('insPageWrap');
  const stage = ins('insPageStage');
  const img = ins('insPageImg');
  if (!wrap || !stage || !img) return;
  const zoom = InspectState.zoom;
  wrap.classList.toggle('zoomed', zoom > 1);
  if (zoom <= 1 || !img.naturalWidth){
    stage.style.height = '';
    stage.style.width = '';
  } else {
    const inner = insFitInner(wrap);
    const height = Math.round(inner.h * zoom);
    const width = Math.round(height * img.naturalWidth / img.naturalHeight);
    stage.style.height = height + 'px';
    stage.style.width = Math.max(width, Math.round(inner.w)) + 'px';
  }
  insLayoutBbox();
}

function insLayoutBbox(){
  const box = ins('insBbox');
  const img = ins('insPageImg');
  const record = InspectState.record;
  if (!box || !img) return;
  const show = ins('insShowBox').checked;
  const bar = ins('insBboxBar');
  const has = !!(record && record.bbox && record.bbox.length === 4);
  if (bar) bar.hidden = !has;
  if (!show || !has || img.hidden || !img.clientWidth){
    box.hidden = true;
    return;
  }
  const left = (record.bbox[1] / 1000) * img.clientWidth;
  const top = (record.bbox[0] / 1000) * img.clientHeight;
  const width = ((record.bbox[3] - record.bbox[1]) / 1000) * img.clientWidth;
  const height = ((record.bbox[2] - record.bbox[0]) / 1000) * img.clientHeight;
  box.style.left = img.offsetLeft + left + 'px';
  box.style.top = img.offsetTop + top + 'px';
  box.style.width = Math.max(2, width) + 'px';
  box.style.height = Math.max(2, height) + 'px';
  box.hidden = false;
}

/* A magnified page opens ON its box: the reviewer never lands on a corner of
   an unzoomed scan and has to hunt for the crop. */
function insCenterBox(){
  const wrap = ins('insPageWrap');
  const box = ins('insBbox');
  const record = InspectState.record;
  if (!wrap || !box || box.hidden || !record || !record.bbox) return;
  if (InspectState.zoom <= 1) return;
  wrap.scrollLeft = Math.max(0, box.offsetLeft + box.offsetWidth / 2 - wrap.clientWidth / 2);
  wrap.scrollTop = Math.max(0, box.offsetTop + box.offsetHeight / 2 - wrap.clientHeight / 2);
}

/* the frame's usable size, padding included */
function insFitInner(wrap){
  const style = getComputedStyle(wrap);
  const horizontal = parseFloat(style.paddingLeft || 0) + parseFloat(style.paddingRight || 0);
  const vertical = parseFloat(style.paddingTop || 0) + parseFloat(style.paddingBottom || 0);
  return {
    w: Math.max(160, wrap.clientWidth - horizontal),
    h: Math.max(240, wrap.clientHeight - vertical),
  };
}

/* The fitted stage is exactly the size of the fitted image; a frame narrower
   than its height times the page's aspect ratio limits the WIDTH instead. */
function insSyncStage(){
  const wrap = ins('insPageWrap');
  const stage = ins('insPageStage');
  const img = ins('insPageImg');
  if (!wrap || !stage || !img || !img.naturalWidth) return;
  if (InspectState.zoom > 1) return insApplyZoom();
  const inner = insFitInner(wrap);
  const ratio = img.naturalWidth / img.naturalHeight;
  let width = Math.min(inner.w, inner.h * ratio);
  let height = width / ratio;
  if (height > inner.h){ height = inner.h; width = height * ratio; }
  stage.style.width = Math.round(width) + 'px';
  stage.style.height = Math.round(height) + 'px';
  insLayoutBbox();
}

function insWireZoom(){
  const apply = insApplyZoom;
  ins('insZoomIn').onclick = () => {
    InspectState.zoom = Math.min(4, Math.round((InspectState.zoom + 0.25) * 100) / 100);
    apply();
  };
  ins('insZoomOut').onclick = () => {
    InspectState.zoom = Math.max(1, Math.round((InspectState.zoom - 0.25) * 100) / 100);
    apply();
  };
  ins('insZoomFit').onclick = () => { InspectState.zoom = 1; apply(); };
  /* the fitted page is a function of the frame's height: keep them in step */
  if (window.ResizeObserver){
    const observer = new ResizeObserver(() => insSyncStage());
    observer.observe(ins('insPageWrap'));
  }
  window.addEventListener('resize', insSyncStage);
  ins('insPageImg').addEventListener('load', insSyncStage);
  apply();
}

if (document.readyState === 'loading'){
  document.addEventListener('DOMContentLoaded', inspectInit);
} else {
  inspectInit();
}
