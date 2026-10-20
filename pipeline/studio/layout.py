"""Stage 1 — deterministic text → block layout (no AI, no network).

Both extraction lanes converge here: the text-PDF lane feeds pdfium's text
layer, the image lane feeds the vision model's raw OCR *text*. Splitting is
code's job — question numbering, option runs, metadata lines, page
attribution — so a model only ever sees what code cannot decide.

A block is one question-shaped unit:

    {"kind": "question" | "prose" | "answer_key_entry",
     "number": int | None,
     "text": "…", "options": [{"label","text"}],
     "metadata_line": "(داخل۱۴۰۱)" | None,
     "ranges": [(start, end), …]}     # char offsets into the page text

``ranges`` lets the text-PDF lane union the character boxes of exactly the
chars that formed the block (the exact-bbox proof from the plan), while the
vision lane simply ignores them.

Splitting is a heuristic — a numbered stem line, then a contiguous option run
whose labels climb 1,2,3,4 (or الف،ب،ج،د). Anything ambiguous stays in the
stem text, so a wrong split is visible in the bulk instead of silently losing
content. Enrich (Stage 2) is where a model re-sorts what this pass doubts.
"""
import re

from pipeline.utils import to_english_digits

# A numeric marker: 1-3 digits (years are 4, so never a marker), a separator
# character, then real content (not another digit — that is a decimal).
_MARK_RE = re.compile(
    r"^[\(\[\{]?\s*(\d{1,3})\s*[\)\.\-\u2013\u2014\u0640:]\s*(?!\d)(.*)$")
# Latin / Persian option letters (الف، ب، ج، د plus latin forms).
_LETTER_RE = re.compile(
    r"^[\(\[\{]?\s*([A-Da-d]|الف|[أإآ]|[بجده])\s*[\)\.\-\u2013\u2014\u0640:]\s*(.*)$")
_LETTER_ALIASES = {"الف": 0, "أ": 0, "إ": 0, "آ": 0, "ا": 0,
                   "ب": 1, "ج": 2, "د": 3}
# Leading bidi controls — stripped for *classification* only, never from text.
_BIDI = " \t\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"

_FOOTER_RE = re.compile(r"^[\s\-\u2013\u2014\d\u06F0-\u06F9\u0660-\u0669]{1,12}$")
_META_ONLY_RE = re.compile(r"^[\(\[]\s*(.{3,80}?)\s*[\)\]]$")
_META_TAIL_RE = re.compile(r"[\(\[]([^\(\)\[\]]{3,80})[\)\]]\s*$")
# A candidate metadata body that still carries a bracket is a caption, not meta.
_UNBALANCED_PAREN = re.compile(r"[\(\)\[\]]")
_YEAR_RE = re.compile(r"(1[34]\d{2})(?:\s*[-–]\s*(1[34]\d{2}))?")
_DIFFICULTY = ("متوسط", "سخت", "آسان", "دشوار", "ساده", "چالشی", "آسون")


def _stripped(line):
    return line.lstrip(_BIDI).strip()


def _letter_index(label):
    low = (label or "").lower()
    if len(low) == 1 and low in "abcd":
        return "abcd".index(low)
    return _LETTER_ALIASES.get(label)


def _classify(line):
    """→ ("num", int) | ("letter", label) | None for one line."""
    if not line:
        return None
    if _FOOTER_RE.match(line):          # page footers/headers, never markers
        return None
    match = _MARK_RE.match(line)
    if match:
        return ("num", int(to_english_digits(match.group(1))))
    match = _LETTER_RE.match(line)
    if match:
        return ("letter", match.group(1))
    return None


def _option_marker(line):
    """The line read as an option start (label 1-4 / الف..د), or None."""
    kind = _classify(line)
    if kind is None:
        return None
    if kind[0] == "num" and kind[1] <= 4:
        match = _MARK_RE.match(line)
        return {"label": match.group(1), "text": match.group(2).strip()}
    if kind[0] == "letter":
        match = _LETTER_RE.match(line)
        return {"label": match.group(1), "text": match.group(2).strip()}
    return None


def _lines_with_offsets(text):
    """[(line, start, end)] — end is exclusive of the newline."""
    out, pos = [], 0
    for raw in text.split("\n"):
        out.append((raw, pos, pos + len(raw)))
        pos += len(raw) + 1
    return out


# A second option marker inside an already-open option line. OCR happily
# renders "۱) ۱۰۰۰  ۲) ۲۰۰۰  ۳) ۱۵۰" as one line, so the marker has to be
# found mid-line, never only at the start.
_INLINE_MARK = re.compile(
    r"(?<=\s)[\(\[]?\s*([1-4\u06F1-\u06F4\u0661-\u0664])\s*[\)]\s*(?=\S)")


def _split_inline_rows(rows):
    """One row per option marker: "۱) a  ۲) b" -> ["۱) a", "۲) b"].

    Only a line that already *starts* with an option marker is split: prose
    that mentions "(۱)" and "(۲)" mid-sentence must stay one line.
    """
    out = []
    for line, start, end in rows:
        if _classify(_stripped(line)) is None:
            out.append((line, start, end))
            continue
        cuts = [match.start() for match in _INLINE_MARK.finditer(line)]
        if not cuts:
            out.append((line, start, end))
            continue
        last = 0
        for cut in cuts + [len(line)]:
            piece = line[last:cut]
            if piece.strip():
                out.append((piece, start + last, start + cut))
            last = cut
    return out


def metadata_fields(line):
    """Best-effort parse of a metadata line — corp / year / difficulty.

    Never guesses: a piece that is not there stays None. The enrich stage
    (and the tag folding) refines whatever this returns.
    """
    raw = str(line or "").strip().strip("()[]").strip()
    if not raw:
        return {"corp": None, "year": None, "difficulty": None}
    # Persian digits scan as their ASCII twins; the spans are identical, so
    # the original spelling is what gets stored.
    scanned = to_english_digits(raw)
    year = None
    match = _YEAR_RE.search(scanned)
    if match:
        year = match.group(1) + ("-" + match.group(2) if match.group(2) else "")
    difficulty = next((w for w in _DIFFICULTY if w in raw), None)
    corp = raw
    if match:
        corp = raw[:match.start()] + " " + raw[match.end():]
    for piece in filter(None, (difficulty, "داخل", "خارج", "کلاس", "کلاسی")):
        corp = corp.replace(piece, " ")
    corp = re.sub(r"[\s\-–_،,]+", " ", corp).strip()
    return {"corp": corp or None, "year": year, "difficulty": difficulty}


def _starts_option_run(rows, index):
    """True when the next marker after `index` is option #2 of a fresh run.

    A stem that lists its own items as "۱) … ۲) …" restarts the numbering where
    the real options begin, so the second run — not the first — is the options.
    The lookahead is what separates that restart from a genuinely new question
    numbered 1: a real question continues with its stem prose, not with "۲)".
    """
    for line, _start, _end in rows[index:]:
        kind = _classify(_stripped(line))
        if kind is None:
            continue
        if kind[0] == "num":
            return kind[1] == 2
        return _letter_index(kind[1]) == 1
    return False


def _same_marker_family(left, right):
    """Numeric and letter labels never mix inside one option run."""
    return str(left or "").strip().isdigit() == str(right or "").strip().isdigit()


def split_text(text):
    """Split one page's text into blocks (see module docstring).

    Deterministic and total: every line lands in some block, so nothing an
    OCR pass produced is ever silently dropped.
    """
    raw_blocks, lead, current = [], [], None
    expected_option = None            # next numeric option value we'd consume
    letter_next = None                # next letter-option index we'd consume
    run_count = 0                     # options accepted in the open option run
    run_start = None                  # char offset where that run began
    numeric_seen = False              # the block's options are numbered, not lettered
    rows = _split_inline_rows(list(_lines_with_offsets(text or "")))
    for row_index, (line, start, end) in enumerate(rows):
        stripped = _stripped(line)
        kind = _classify(stripped)

        if kind is not None and kind[0] == "num":
            value = kind[1]
            if current is not None and expected_option == value:
                if value == 1:
                    run_count = 0
                    run_start = start
                current["lines"].append((line, start, end))
                run_count += 1
                numeric_seen = True
                expected_option = value + 1 if value < 4 else None
                continue
            if (current is not None and value == 1 and run_count >= 2
                    and current["number"] != 1
                    and _starts_option_run(rows, row_index + 1)):
                # A stem that lists its own items as "۱) … ۲) …" (reaction
                # equations, sub-parts) restarts the numbering where the REAL
                # options begin. The earlier run is stem text, so the block
                # stays open and only this later run becomes its options.
                run_start = start
                current["lines"].append((line, start, end))
                run_count, expected_option, letter_next = 1, 2, 0
                numeric_seen = True
                continue
            if current is not None:
                current["run_start"] = run_start
                raw_blocks.append(current)
            current = {"number": value, "lines": [(line, start, end)],
                       "head": _MARK_RE.match(stripped).group(2).strip(),
                       "run_start": None}
            run_count, run_start = 0, None
            numeric_seen = False
            expected_option = 1
            letter_next = 0
            continue

        if kind is not None and kind[0] == "letter":
            idx = _letter_index(kind[1])
            # A numbered question keeps its text: "(الف) (ب)" under a caption
            # is a figure label, not option 1 of a letter run.
            lettered = not numeric_seen or (letter_next or 0) > 0
            if (current is not None and idx >= 0 and letter_next is not None
                    and idx == letter_next and lettered):
                if idx == 0:

                    run_count = 0
                    run_start = start
                current["lines"].append((line, start, end))
                run_count += 1
                letter_next = idx + 1 if idx < 3 else None
                expected_option = None
                continue

        if current is not None:
            current["lines"].append((line, start, end))
        elif stripped:
            lead.append((line, start, end))
    if current is not None:
        current["run_start"] = run_start
        raw_blocks.append(current)

    out = []
    if lead:
        body = "\n".join(t for t in (_stripped(l) for l, _, _ in lead) if t)
        if body:
            out.append({"kind": "prose", "number": None, "text": body,
                        "options": [], "metadata_line": None,
                        "ranges": [(s, e) for _, s, e in lead]})

    for raw in raw_blocks:
        # The marker line carried the number and, usually, the first words of
        # the stem — keep that head, drop the bare marker.
        stem, opts, meta = [], [], None
        head = raw.get("head") or ""
        if head and not _FOOTER_RE.match(head):
            stem.append(head)
        in_options = False
        run_start = raw.get("run_start")
        lines = raw["lines"][1:]
        tail_from = max(0, len(lines) - 3)   # metadata lives at a block's end
        for index, (line, start, end) in enumerate(lines):
            stripped = _stripped(line)
            if not stripped or _FOOTER_RE.match(stripped):
                continue
            if meta is None and index >= tail_from:
                only = _META_ONLY_RE.match(stripped)
                # A figure caption like "(الف) (ب)" is not a metadata line.
                if only and not _UNBALANCED_PAREN.search(only.group(1)):
                    meta = only.group(1).strip()
                    continue
            # Lines before the option run's start are stem content even when
            # they look like markers (a stem's own "۱) … ۴)" list).
            early = run_start is not None and start < run_start
            marker = None if early else _option_marker(stripped)
            if marker is not None and opts and not _same_marker_family(
                    opts[-1]["label"], marker["label"]):
                marker = None          # "(الف) (ب)" inside a numbered run
            if marker is not None:
                in_options = True
                opts.append(marker)
                continue
            if in_options and opts:
                # Splitter only places non-markers here while the option run
                # is open — a wrapped option tail.
                opts[-1]["text"] = (opts[-1]["text"] + " " + stripped).strip()
                continue
            stem.append(stripped)
        if meta is None:
            for i in range(len(stem) - 1, -1, -1):
                match = _META_TAIL_RE.search(stem[i])
                if match and not _UNBALANCED_PAREN.search(match.group(1)):
                    meta = match.group(1).strip()
                    stem[i] = stem[i][:match.start()].rstrip()
                    if not stem[i]:
                        stem.pop(i)
                    break
        out.append({
            "kind": "question" if raw["number"] is not None else "prose",
            "number": raw["number"],
            "text": "\n".join(stem),
            "options": opts,
            "metadata_line": meta,
            "ranges": [(s, e) for _, s, e in raw["lines"]],
        })
    return _absorb_option_orphans(out)


def _absorb_option_orphans(blocks):
    """Fold a numbered stem-list leftover back into the question it belongs to.

    A stem can spell its own items "۱) … ۳)" and then start the real options at
    "۱)" again; the splitter closes the question there, so the options land in
    a 1..4 block right after a question that still has none. Such a block is a
    leftover, not a question — append its text to the stem and hand over its
    options.
    """
    out = []
    for block in blocks:
        prev = out[-1] if out else None
        if (prev is not None and block["kind"] == "question"
                and block["number"] in (1, 2, 3, 4)
                and prev["kind"] == "question" and (prev["number"] or 0) > 4
                and len(prev["options"]) < 2):
            if block["text"]:
                prev["text"] = (prev["text"] + "\n" + block["text"]).strip()
            if block["options"]:
                prev["options"] = block["options"]
                prev["metadata_line"] = (prev["metadata_line"]
                                         or block["metadata_line"])
            prev["ranges"] = prev["ranges"] + block["ranges"]
            continue
        out.append(block)
    return out


# ── cross-page continuation (the split flag) ────────────────────────────────

# The vision prompt's continuation markers (rules 8/9 of OCR_PAGE_PROMPT).
_SPLIT_RE = re.compile(r"^\s*\**\s*SPLIT\s*:\s*(head|tail|both)\s*\**\s*$",
                       re.IGNORECASE)


def parse_split(text):
    """Strip the model's SPLIT marker lines -> (clean_text, flag | None)."""
    flags, kept = [], []
    for line in (text or "").splitlines():
        match = _SPLIT_RE.match(line)
        if match:
            flags.append(match.group(1).lower())
            continue
        kept.append(line)
    if not flags:
        return "\n".join(kept), None
    flag = "both" if len(set(flags)) > 1 or "both" in flags else flags[0]
    return "\n".join(kept), flag


def mark_continuations(blocks):
    """Flag a block a page break cut in two - the deterministic backstop.

    Only the unambiguous shape: the next page opens with an option line, which
    cannot be that page's own question. A vision marker already on a block is
    never overwritten.
    """
    by_page = {}
    for block in blocks or []:
        by_page.setdefault(block.get("page"), []).append(block)
    for page, group in by_page.items():
        following = by_page.get(page + 1)
        if not group or not following:
            continue
        tail, head = group[-1], following[0]
        if head.get("kind") == "question":
            continue
        first = (head.get("text") or "").splitlines()
        kind = _classify(_stripped(first[0])) if first else None
        if not kind or kind[0] != "letter":
            continue
        tail["split"] = "both" if tail.get("split") == "head" else (
            tail.get("split") or "tail")
        head["split"] = "both" if head.get("split") == "tail" else (
            head.get("split") or "head")
    return blocks


def join_continuations(blocks):
    """Join adjacent tail->head pairs in reading order -> (blocks, count).

    The head keeps its page, key and bbox; the tail contributes text and
    options and is dropped. The result records ``split_joined`` so the bulk and
    the importer can see that text changed hands.
    """
    out, joined, index = [], 0, 0
    while index < len(blocks):
        block = blocks[index]
        following = blocks[index + 1] if index + 1 < len(blocks) else None
        if (following is not None
                and block.get("split") in ("tail", "both")
                and following.get("split") in ("head", "both")):
            block["text"] = "\n".join(
                part for part in (block.get("text"), following.get("text"))
                if part)
            block["options"] = (list(block.get("options") or [])
                                + list(following.get("options") or []))
            block["page_end"] = (following.get("page_end")
                                 or following.get("page"))
            block["split"] = "tail" if following.get("split") == "both" else None
            block["split_joined"] = True
            block["issues"] = (list(block.get("issues") or [])
                               + ["joined across the page break on page %s"
                                  % following.get("page")])
            joined += 1
            out.append(block)
            index += 2
            continue
        out.append(block)
        index += 1
    return out, joined


# ── answer-key pages ────────────────────────────────────────────────────────

def key_entries(text):
    """Parse a bare answer-key page into [{number, label}].

    Those pages read as alternating ``number label`` runs (۱۲ ۳  45 ۱ …).
    Scanned as a token stream keeping only (1-3 digit number, 1-4 label)
    pairs, so a stray digit pair cannot shift the whole table.
    """
    tokens = re.findall(r"\d{1,3}", to_english_digits(text or ""))
    out, i = [], 0
    while i < len(tokens) - 1:
        number, label = int(tokens[i]), int(tokens[i + 1])
        if 1 <= label <= 4 and 1 <= number <= 999:
            out.append({"number": tokens[i], "label": tokens[i + 1]})
            i += 2
        else:
            i += 1
    return out


# ── geometry helpers (text lane) ────────────────────────────────────────────

def union_rects(rects):
    """Union of (left, bottom, right, top) boxes; None when empty."""
    rects = [r for r in rects if r]
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def pdf_rect_to_norm(rect, width, height):
    """(left, bottom, right, top) PDF points (y-up) → [ymin,xmin,ymax,xmax]
    normalized to 0-1000 with y measured from the page top — the frame the
    Sapienstech BboxParser and the old pipeline already use."""
    if not rect or not width or not height:
        return None
    left, bottom, right, top = rect
    return [int(round(1000 * (height - top) / height)),
            int(round(1000 * left / width)),
            int(round(1000 * (height - bottom) / height)),
            int(round(1000 * right / width))]


def norm_union(a, b):
    """Union of two normalized yxxy boxes (None-tolerant)."""
    if not a:
        return b
    if not b:
        return a
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
