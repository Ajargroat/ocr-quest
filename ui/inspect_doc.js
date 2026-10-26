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

  /* `$$...$$`, `$...$` and `\(..\)` -> chips, everything else -> escaped text
     with its newlines kept. */
  function inlineHtml(text){
    const src = String(text == null ? '' : text);
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

  /* ── the live editor ───────────────────────────────────────────── */
  let editor = null;
  let typesetTimer = null;
  let onInput = null;

  const selectionIn = () => {
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount) return null;
    const range = sel.getRangeAt(0);
    return editor && editor.contains(range.commonAncestorContainer) ? range : null;
  };

  /* MathJax once the page settles: a chip re-typesets the moment its LaTeX
     changes, and a burst of typing must not queue one pass per keystroke. */
  function scheduleTypeset(){
    clearTimeout(typesetTimer);
    typesetTimer = setTimeout(() => {
      if (!editor) return;
      /* re-wrap a chip whose content was replaced: typing inside a chip is
         turned back into a chip on the next pass */
      editor.querySelectorAll('.ins-math[data-tex]').forEach(chip => {
        const tex = chip.getAttribute('data-tex') || '';
        const want = chip.classList.contains('display')
          ? `\\[${tex}\\]` : `\\(${tex}\\)`;
        if (chip.textContent !== want) chip.textContent = want;
      });
      if (window.typesetMath) typesetMath(editor);
    }, 260);
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
    const chip = document.createElement('span');
    chip.className = 'ins-math' + (display ? ' display' : '');
    chip.setAttribute('contenteditable', 'false');
    chip.setAttribute('data-tex', tex);
    chip.textContent = display ? `\\[${tex}\\]` : `\\(${tex}\\)`;
    return chip;
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
     exactly the rule the dashboard's own renderer applies. */
  function tidyTex(){
    const run = /\\[A-Za-z]+(?:\s*\{[^{}]*\})*/g;
    const walker = document.createTreeWalker(editor, NodeFilter.SHOW_TEXT);
    const targets = [];
    while (walker.nextNode()){
      const node = walker.currentNode;
      if (node.parentElement && node.parentElement.closest('.ins-math')) continue;
      if (/\\[A-Za-z]+/.test(node.nodeValue)) targets.push(node);
    }
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
    if (targets.length) scheduleTypeset();
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
  };
})();
window.InspDoc = InspDoc;
