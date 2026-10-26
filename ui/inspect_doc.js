/* ═══════════ INSPECT DOC · one editing surface: text, formulas, tables ═══════════
   The reviewer edits a staged row the way a document editor works — one page,
   click into it, type. There is no second box: a formula is a chip inside the
   text (double-click edits the LaTeX), a table is a real table in the text,
   and the choices are rows at the bottom of the same page.

   The stored value stays the OCR's own Markdown (`$...$` inline, `$$...$$` for
   display, one `| a | b |` line per table row), so nothing downstream has to
   learn a new format: `docToText` writes it, `textToDoc` reads it. */
const InspDoc = (() => {

  /* ── HTML helpers ───────────────────────────────────────────────── */
  const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, ch =>
    ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));

  const cellText = el => (el ? el.textContent : '').replace(/[\r\n]+/g, ' ').trim();

  /* A cell written as `| x |` breaks the line format: `|` is escaped. */
  const mdCell = s => cellText(s).replace(/\|/g, '\\|');

  const tableToMd = table => {
    const rows = [...table.querySelectorAll('tr')].map(tr =>
      [...tr.children].map(td => mdCell(td)));
    if (!rows.length) return '';
    const width = Math.max(...rows.map(r => r.length));
    const line = cells => {
      const padded = cells.concat(Array(Math.max(0, width - cells.length)).fill(''));
      return '| ' + padded.join(' | ') + ' |';
    };
    return [line(rows[0]), line(Array(width).fill('---'))]
      .concat(rows.slice(1).map(line)).join('\n');
  };

  /* a pipe-table cell may carry math; the same inline pass renders it */
  const cellHtml = text => (text ? inlineHtml(text) : '<br>');

  /* The OCR drops the `$` around a formula as often as it keeps it: a choice
     arrives as bare `\frac{1}{۳}`. A command ("\frac") or a script ("q_3")
     marks the start of maths; the run grows over its Latin/digits/operators
     and brace groups until prose (Persian letters) starts again — the same
     rule the dashboard's renderer applies (ui/app.js: fenceBareTex). */
  const fenceBareTex = s => {
    if (!s) return s;
    /* delimited maths is already a chip: leave it for inlineHtml */
    const mine = /\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]/g;
    const parts = [];
    let last = 0, m;
    while ((m = mine.exec(s))){
      parts.push({text: s.slice(last, m.index), fenced: false},
                 {text: m[0], fenced: true});
      last = m.index + m[0].length;
    }
    parts.push({text: s.slice(last), fenced: false});

    const persian = /[\u0600-\u06FF\u0750-\u077F]/;
    const word = /[A-Za-z0-9\u06F0-\u06F9\u0660-\u0669]/;
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
    const plain = text => {
      if (text.indexOf('\\') === -1 && text.indexOf('_') === -1
          && text.indexOf('^') === -1) return text;
      const strong = /\\[A-Za-z]+|[_^]\{(?:[^{}]|\{[^{}]*\})*\}|[_^][A-Za-z0-9]/g;
      let out = '', cursor = 0, hit;
      while ((hit = strong.exec(text))){
        if (hit.index < cursor) continue;
        let start = growLeft(text, hit.index);
        const end = growRight(text, hit.index + hit[0].length);
        if (start < cursor) start = cursor;
        if (end <= start) continue;
        out += text.slice(cursor, start) + '$' + text.slice(start, end) + '$';
        cursor = end;
        strong.lastIndex = end;
      }
      return out + text.slice(cursor);
    };
    return parts.map(p => p.fenced ? p.text : plain(p.text)).join('');
  };

  /* `$$...$$`, `$...$` and `\(..\)` -> chips, everything else -> escaped text
     with its newlines kept. */
  function inlineHtml(text){
    const src = fenceBareTex(String(text == null ? '' : text));
    const math = /\$\$([\s\S]*?)\$\$|\$([^$\n]+?)\$|\\\(([\s\S]*?)\\\)/g;
    let out = '', last = 0, m;
    while ((m = math.exec(src))){
      out += esc(src.slice(last, m.index)).replace(/\n/g, '<br>');
      const body = (m[1] ?? m[2] ?? m[3] ?? '').trim();
      const display = m[1] != null ? ' display' : '';
      out += `<span class="ins-math${display}" contenteditable="false"` +
             ` data-tex="${esc(body)}">\\(${esc(body)}\\)</span>`;
      last = m.index + m[0].length;
    }
    return out + esc(src.slice(last)).replace(/\n/g, '<br>');
  }

  /* the same chip from inside the editor (a typed `$..$` becomes one) */
  const chipSpan = (tex, display) => {
    const span = document.createElement('span');
    span.className = 'ins-math' + (display ? ' display' : '');
    span.setAttribute('contenteditable', 'false');
    span.setAttribute('data-tex', tex);
    span.textContent = display ? `\\[${tex}\\]` : `\\(${tex}\\)`;
    return span;
  };

  function tableHtml(rows){
    const body = rows.map(cells =>
      '<tr>' + cells.map(c => '<td>' + cellHtml(c) + '</td>').join('') + '</tr>')
      .join('');
    return `<table class="ins-tbl"><tbody>${body}</tbody></table>`;
  }

  /* ── Markdown -> HTML ───────────────────────────────────────────── */
  function toHtml(md){
    const lines = String(md == null ? '' : md).replace(/\r\n?/g, '\n')
      .split('\n');
    const out = [];
    let i = 0;
    /* leading display math becomes its own block, so `$$x$$` on its own line
       reads as a centred formula instead of an inline chip */
    const blockMath = line => {
      const m = line.trim().match(/^\$\$([\s\S]+?)\$\$$/) ||
                  line.trim().match(/^\\\[([\s\S]+?)\\\]$/);
      return m ? m[1].trim() : null;
    };
    while (i < lines.length){
      const display = blockMath(lines[i]);
      if (display != null){
        out.push(`<div class="ins-math display" contenteditable="false"` +
                 ` data-tex="${esc(display)}">\\[${esc(display)}\\]</div>`);
        i++;
        continue;
      }
      const head = lines[i];
      const rule = lines[i + 1] || '';
      if (/^\s*\|.*\|\s*$/.test(head) && /^\s*\|[\s:|-]+\|\s*$/.test(rule)){
        const split = line => line.trim().replace(/^\||\|$/g, '').split('|')
          .map(c => c.trim());
        const rows = [split(head)];
        i += 2;
        for (; i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i]); i++)
          rows.push(split(lines[i]));
        out.push(tableHtml(rows));
        continue;
      }
      out.push(inlineHtml(head));
      i++;
    }
    /* consecutive prose lines are separate lines, never one glued paragraph */
    return out.join('<br>');
  }

  /* ── HTML -> Markdown ───────────────────────────────────────────── */
  const BLOCK = new Set(['DIV', 'P', 'LI', 'UL', 'OL', 'TABLE', 'TR', 'H1',
                         'H2', 'H3', 'H4', 'H5', 'H6', 'SECTION', 'BLOCKQUOTE']);

  function serialize(node){
    let out = '';
    node.childNodes.forEach(child => {
      if (child.nodeType === Node.TEXT_NODE){
        out += child.nodeValue.replace(/\u00a0/g, ' ');
        return;
      }
      if (child.nodeType !== Node.ELEMENT_NODE) return;
      const tag = child.tagName;
      if (child.classList && child.classList.contains('ins-math')){
        const tex = child.getAttribute('data-tex') || '';
        out += child.classList.contains('display') ? `$$${tex}$$` : `$${tex}$`;
        return;
      }
      if (tag === 'BR'){ out += '\n'; return; }
      if (tag === 'TABLE'){
        out += '\n' + tableToMd(child) + '\n';
        return;
      }
      if (BLOCK.has(tag)){
        const inner = serialize(child).replace(/^\n+|\n+$/g, '');
        if (inner || tag === 'LI') out += '\n' + inner + '\n';
        return;
      }
      out += serialize(child);
    });
    return out;
  }

  function toText(root){
    if (!root) return '';
    return serialize(root)
      .replace(/\u200b/g, '')
      .split('\n').map(line => line.replace(/[ \t]+$/g, '')).join('\n')
      .replace(/\n{3,}/g, '\n\n')
      .trim();
  }

  /* ── the live editor state ─────────────────────────────────────── */
  let editor = null;
  let typesetTimer = null;
  let onInput = null;
  /* choice cells currently rendered in the page, so a tidy pass covers them */
  let choiceCells = [];

  /* ── choices ────────────────────────────────────────────────────
     A choice is prose plus formulas, so its text reads exactly like the stem:
     the same Markdown -> chip pass, typed into a contenteditable cell that
     writes its Markdown back on every edit. One input event per row. */
  function choiceHtml(MD){
    const md = String(MD == null ? '' : MD);
    if (!md.trim()) return '<p><br></p>';
    return toHtml(md).split('<br>').map(line => `<p>${line || '<br>'}</p>`).join('');
  }

  function choiceText(cell){
    if (!cell) return '';
    return toText(cell)
      .split('\n').map(line => line.trim()).filter(Boolean).join(' ');
  }

  function choiceInit(cell, onEdit){
    if (cell && choiceCells.indexOf(cell) === -1) choiceCells.push(cell);
    cell.addEventListener('input', () => {
      markDirty();
      if (typeof onEdit === 'function') onEdit(choiceText(cell));
      scheduleTypeset(cell);
    });
    cell.addEventListener('paste', event => {
      const data = event.clipboardData || window.clipboardData;
      if (!data) return;
      event.preventDefault();
      const html = data.getData('text/html');
      let md = data.getData('text/plain') || '';
      if (html){
        const box = document.createElement('div');
        box.innerHTML = html;
        md = toText(box) || md;
      }
      document.execCommand('insertHTML', false,
        choiceHtml(md).replace(/^<p[^>]*>|<\/p>$/g, ''));
      markDirty();
      if (typeof onEdit === 'function') onEdit(choiceText(cell));
      scheduleTypeset(cell);
    });
  }

  /* ── the live editor ───────────────────────────────────────────── */
  /* (a chip keeps its LaTeX in `data-tex`; refreshChip below re-<wbr>wraps it) */

  const selectionIn = () => {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    return editor && editor.contains(range.commonAncestorContainer) ? range : null;
  };

  /* MathJax once the page settles: a chip re-typesets the moment its LaTeX
     changes, and a burst of typing must not queue one pass per keystroke.

     Two passes, because MathJax's own startup sweep SKIPS anything inside a
     `contenteditable` region (it treats it as text being typed), and so does a
     second incantation on a container it already mapped. So:
       1. at once: give every chip a fresh `math/tex` script MathJax has not
          seen (`\\(tex\\)` / `\\[tex\\]`), and
       2. ask for a typeset on that chip element itself — a fresh node is not
          skipped — retrying until the CDN library is up. */
  function scheduleTypeset(target){
    clearTimeout(typesetTimer);
    typesetTimer = setTimeout(() => {
      const scopes = target ? [target] : [editor].concat(choiceCells);
      scopes.forEach(scope => {
        if (!scope || !scope.querySelectorAll) return;
        scope.querySelectorAll('.ins-math[data-tex]').forEach(chip => {
          refreshChip(chip);
          typesetChip(chip);
        });
      });
    }, 120);
  }

  const chipScript = chip =>
    chip.querySelector('script[type^="math/tex"]');

  /* MathJax's output can outlive a plain `textContent` write (it also keeps an
     `mjx-assistive-mml` sibling), so a redraw empties the chip explicitly. */
  function clearChip(chip){
    while (chip.firstChild) chip.removeChild(chip.firstChild);
    chip._drawnTex = null;
  }

  /* A chip is `data-tex` + a TeX body in the delimiters MathJax reads. Whichever
     state it is in, the body must match `data-tex`, and `_drawnTex` records what
     MathJax actually drew:
       - not drawn yet -> body is `\(tex\)` (or `\[tex\]` when display);
       - drawn and unchanged -> left alone;
       - drawn but `data-tex` moved on -> emptied so the new source goes in. */
  function refreshChip(chip){
    const tex = chip.getAttribute('data-tex') || '';
    const display = chip.classList.contains('display');
    const want = display ? `\\[${tex}\\]` : `\\(${tex}\\)`;
    if (!!chip.querySelector('mjx-container')){
      if (chip._drawnTex === tex) return;
      clearChip(chip);
    }
    if (chip.textContent === want && chipScript(chip)) return;
    if (chip.querySelector('mjx-container')) clearChip(chip);
    chip.textContent = want;
  }

  /* one chip at a time, and only until it is typeset */
  function typesetChip(chip, tries){
    if (!chip || chip.isConnected === false) return;
    if (chip.querySelector('mjx-container')) return;
    const tex = chip.getAttribute('data-tex') || '';
    if (!tex.trim()) return;
    const n = tries || 0;
    if (window.MathJax && window.MathJax.typesetPromise){
      window.MathJax.typesetPromise([chip]).then(
        () => { if (chip) chip._drawnTex = tex; },
        () => {});
      return;
    }
    if (n < 40) setTimeout(() => typesetChip(chip, n + 1), 150);
  }

  function markDirty(){
    if (typeof onInput === 'function') onInput();
  }

  function placeCaret(node, atEnd){
    const range = document.createRange();
    range.selectNodeContents(node);
    range.collapse(!atEnd);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }
  function insertNode(node, after){
    const range = selectionIn();
    if (range){
      range.deleteContents();
      range.insertNode(node);
      if (after){
        const next = document.createElement('p');
        next.innerHTML = '<br>';
        node.parentNode.insertBefore(next, node.nextSibling);
        placeCaret(next, false);
      } else {
        /* keep the caret in the paragraph that follows the table */
        let tail = node.nextSibling;
        if (!tail){
          tail = document.createElement('p');
          tail.innerHTML = '<br>';
          node.parentNode.appendChild(tail);
        }
        placeCaret(tail, false);
      }
    } else {
      editor.appendChild(node);
    }
    markDirty();
    scheduleTypeset();
  }

  function makeMath(tex, display){
    return chipSpan(tex, display);
  }

  /* Insert a formula. With a selection, the selection becomes the formula's
     body — the reviewer highlights `a_1` and presses the formula key. */
  function insertMath(){
    const range = selectionIn();
    let tex = '';
    if (range && !range.collapsed) tex = range.toString().trim();
    let display = false;
    if (!tex && range){
      /* a display formula on a line of its own is what a whole-line body wants */
      const block = range.startContainer.nodeType === Node.ELEMENT_NODE
        ? range.startContainer : range.startContainer.parentElement;
      display = !!(block && block !== editor && block.tagName === 'DIV');
    }
    const chip = makeMath(tex || 'x', display);
    if (range){
      range.deleteContents();
      range.insertNode(chip);
      chip.after(document.createTextNode(' '));
      const after = document.createTextNode('');
      chip.nextSibling.after(after);
      placeCaret(after, false);
    }
    markDirty();
    scheduleTypeset();
    editMath(chip);
  }

  /* Double-click / edit: the LaTeX lives in one small input over the chip. */
  function editMath(chip){
    if (!chip || !chip.isConnected) return;
    const box = document.createElement('input');
    box.className = 'ins-math-input open';
    box.value = chip.getAttribute('data-tex') || '';
    box.spellcheck = false;
    box.dir = 'ltr';
    const rect = chip.getBoundingClientRect();
    box.style.left = Math.max(8, rect.left) + 'px';
    box.style.top = (rect.bottom + 6) + 'px';
    box.style.minWidth = Math.max(180, rect.width) + 'px';
    document.body.appendChild(box);
    box.focus();
    box.select();
    const head = box.value.slice(0, 2);
    if (head === '$$' || head === '\\(') box.setSelectionRange(2, box.value.length - 2);
    const done = save => {
      const open = box.classList.contains('open');
      box.classList.remove('open');
      if (!open) return;
      if (save){
        const tex = box.value.replace(/^\$+|\$+$/g, '')
          .replace(/^\\\(|\\\)$/g, '').replace(/^\\\[|\\\]$/g, '').trim();
        if (tex) chip.setAttribute('data-tex', tex);
        else chip.remove();
        markDirty();
      }
      box.remove();
      if (chip.isConnected && chip.textContent) chip.textContent = '';
      scheduleTypeset();
    };
    box.addEventListener('keydown', ev => {
      if (ev.key === 'Enter'){ ev.preventDefault(); done(true); }
      if (ev.key === 'Escape'){ ev.preventDefault(); done(false); }
    });
    /* Enter already closed it; a later blur must not write the box a second
       time (or twice, from two paths) */
    box.addEventListener('blur', () => done(true));
  }

  function insertTable(tpl){
    const node = tpl && tpl.content
      ? tpl.content.firstElementChild.cloneNode(true) : null;
    if (node) insertNode(node, true);
  }

  function caretTable(){
    const range = selectionIn();
    if (!range) return null;
    const node = range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer : range.startContainer.parentElement;
    return node ? node.closest('table.ins-tbl') : null;
  }

  function addRow(){
    const table = caretTable();
    if (!table) return insertTable(document.getElementById('insDocTableTpl'));
    const rows = table.rows;
    const last = rows[rows.length - 1];
    const row = last.cloneNode(true);
    [...row.children].forEach(td => { td.innerHTML = '<br>'; });
    last.after(row);
    placeCaret(row.children[0], false);
    markDirty();
  }

  function addCol(){
    const table = caretTable();
    if (!table){ addRow(); return; }
    const range = selectionIn();
    const cell = range && range.startContainer.nodeType === Node.ELEMENT_NODE
      ? range.startContainer.closest('td')
      : (range ? range.startContainer.parentElement.closest('td') : null);
    const index = cell && cell.parentElement ? [...cell.parentElement.children].indexOf(cell) : -1;
    [...table.rows].forEach(tr => {
      const td = document.createElement('td');
      td.innerHTML = '<br>';
      const at = index >= 0 ? tr.children[index] : tr.children[tr.children.length - 1];
      if (at) at.after(td); else tr.appendChild(td);
    });
    markDirty();
  }

  /* `\frac` typed by hand comes out as `x\frac{a}{b}`: the run is fenced,
     exactly the rule the dashboard's own renderer applies. Covers the page
     and every choice cell. */
  function tidyTex(){
    const lists = [editor].concat(choiceCells);
    tidyIn(lists);
  }

  function tidyIn(scope){
    const run = /\\[A-Za-z]+(?:\s*\{[^{}]*\})*/g;
    const targets = [];
    scope.forEach(root => {
      if (!root) return;
      const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()){
        const node = walker.currentNode;
        if (node.parentElement && node.parentElement.closest('.ins-math')) continue;
        if (/\\[A-Za-z]+/.test(node.nodeValue)) targets.push(node);
      }
    });
    targets.forEach(node => {
      const frag = document.createDocumentFragment();
      let last = 0;
      const text = node.nodeValue;
      run.lastIndex = 0;
      let m, changed = false;
      while ((m = run.exec(text))){
        changed = true;
        frag.appendChild(document.createTextNode(text.slice(last, m.index)));
        frag.appendChild(makeMath(m[0].trim(), false));
        last = m.index + m[0].length;
      }
      if (!changed) return;
      frag.appendChild(document.createTextNode(text.slice(last)));
      node.parentNode.replaceChild(frag, node);
    });
    if (targets.length) scope.forEach(root => { if (root) scheduleTypeset(root); });
  }

  function init(options){
    editor = options.element;
    onInput = options.onInput || null;
    editor.addEventListener('input', () => { markDirty(); clearTimeout(typesetTimer); });
    editor.addEventListener('paste', event => {
      const data = event.clipboardData || window.clipboardData;
      if (!data) return;
      const html = data.getData('text/html');
      event.preventDefault();
      if (html){
        /* paste as a document, but only the text (and its math, re-chipped) */
        const box = document.createElement('div');
        box.innerHTML = html;
        const frag = document.createElement('div');
        frag.innerHTML = toHtml(toText(box) || data.getData('text/plain'));
        document.execCommand('insertHTML', false, frag.innerHTML);
      } else {
        const text = data.getData('text/plain');        // plain paste uses our own conversion
        document.execCommand('insertHTML', false, toHtml(text));
      }
      markDirty();
      scheduleTypeset();
    });
    editor.addEventListener('keydown', event => {
      /* Enter at the end of a heading-ish line makes a new line, never a clone
         of the chip it sits beside */
      if (event.key === 'Enter' && !event.shiftKey){
        const range = selectionIn();
        const chip = range && range.startContainer.parentElement
          && range.startContainer.parentElement.closest('.ins-math');
        if (chip && chip.classList.contains('display')){
          event.preventDefault();
          const line = document.createElement('p');
          line.innerHTML = '<br>';
          chip.after(line);
          placeCaret(line, false);
        }
      }
    });
    editor.addEventListener('dblclick', event => {
      const chip = event.target.closest('.ins-math');
      if (chip) { event.preventDefault(); editMath(chip); }
    });
    scheduleTypeset();
  }

  function run(cmd){
    if (!editor) return;
    if (cmd === 'math') return insertMath();
    if (cmd === 'table') return insertTable(document.getElementById('insDocTableTpl'));
    if (cmd === 'row') return addRow();
    if (cmd === 'col') return addCol();
    editor.focus();
    document.execCommand(cmd, false, null);
    markDirty();
  }

  function setText(md){
    if (!editor) return;
    editor.innerHTML = toHtml(md) || '<br>';
    scheduleTypeset();
  }

  function focus(){
    if (!editor) return;
    editor.focus();
    if (!editor.firstChild) editor.innerHTML = '<br>';
    const range = document.createRange();
    range.selectNodeContents(editor);
    range.collapse(true);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
  }

  return {
    toHtml, toText, init, run, setText, focus, editMath, scheduleTypeset, tidyTex,
    choiceHtml, choiceText, choiceInit,
  };
})();
window.InspDoc = InspDoc;
