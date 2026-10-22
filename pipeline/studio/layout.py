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
# The booklet writes an option as "1) text" and as "1( text" - on that row
# the open paren is the label's own closing mark.
_MARK_RE = re.compile(
    r"^[\(\[\{]?\s*(\d{1,3})\s*[\(\)\.\-\u2013\u2014\u0640:]\s*(?!\d)(.*)$")
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


def _looks_like_sentence(text):
    """True for a line that reads as prose rather than an option fragment.

    An option value is a short token ("200 , NaNO3") or a formula; five or
    more words is running text, whatever the line happens to end with - the
    OCR also closes such a line with a stray ")2".
    """
    body = (text or "").strip()
    if not body:
        return False
    return len(body.split()) >= 5


def _option_marker(line):
    """The line read as an option start (label 1-4 / الف..د), or None.

    Two shapes are accepted: the usual "1) text", and the booklet's reversed
    "200 , NaNO3 )1", where the label is drawn after the value it belongs to.
    A sentence that merely ends in ")2" is not an option: ``_tail_label``
    refuses anything ending in its own punctuation.
    """
    kind = _classify(line)
    if kind is not None and kind[0] == "num" and kind[1] <= 4:
        match = _MARK_RE.match(line)
        return {"label": match.group(1), "text": match.group(2).strip()}
    if kind is not None and kind[0] == "letter":
        match = _LETTER_RE.match(line)
        return {"label": match.group(1), "text": match.group(2).strip()}
    tail = _tail_label(line)
    if tail:
        return {"label": tail[1], "text": tail[0]}
    return None


# Combining marks the producer draws as their own glyph.  A line that
# starts with one (127 lines of the 32-page exam) no longer matches a
# question marker, so 22 questions lost their number and merged into prose.
_LEAD_MARKS = "\u0654\u064b\u064c\u064d\u064e\u064f\u0650\u0651\u0652\u0670"


# A value followed by an option label: "200 , NaNO3 )1". The label comes last
# in the stream because the producer draws the number after the value it
# belongs to, so the pair is read back to front.
_TAIL_LABEL_RE = re.compile(
    r"^(?P<value>.*?\S)\s*\)\s*(?P<label>[1-4\u06F1-\u06F4\u0661-\u0664])\s*$")

# A row of four value/label pairs on one line, drawn right to left:
# "120 )4 100 )3 90 )2 80 )1". The labels are the anchors.
_PAIR_RE = re.compile(
    r"(?P<value>.*?)\s*\)\s*(?P<label>[1-4\u06F1-\u06F4\u0661-\u0664])(?=\s|$)")
# A lone dash opens a numberless question (the booklet prints no number).
_DASH_STEM_RE = re.compile(r"^[\-–—ـ]")


# Anything that marks a line as a value rather than a phrase: a digit, a
# fraction or decimal separator, a unit, or a formula glyph.
_VALUE_RE = re.compile(r"[0-9\u06F0-\u06F9\u0660-\u0669/.,=%\u00D7\u00B7^_()]"
                      r"|\w{1,3}O\d|mol|kJ|J/g")


def _tail_label(text):
    """("200 , NaNO3", "1") when the line is a value with a trailing label."""
    match = _TAIL_LABEL_RE.match(text or "")
    if not match:
        return None
    value = match.group("value").strip()
    if not value or not re.search(r"[0-9\u06F0-\u06F9]|\w", value):
        return None
    # An option row carries a value: a number, a symbol or a formula part
    # ("200 , NaNO3", "9 - 157/5").  Prose that merely happens to end in
    # ")2" ("a sentence )2") has none of those and is not an option.
    if value[-1:] in "?!." or not _VALUE_RE.search(value):
        return None
    return value, _english_digit(match.group("label"))


def _english_digit(ch):
    code = ord(ch)
    if 0x06F0 <= code <= 0x06F9:
        return str(code - 0x06F0)
    if 0x0660 <= code <= 0x0669:
        return str(code - 0x0660)
    return ch


def _pair_row(text):
    """[(label, value)] for "120 )4 100 )3 90 )2 80 )1", or None.

    Only a complete run of exactly the four options is accepted, each with a
    non-empty value, so a stray ")2" inside a sentence is never an option.
    """
    found = [(m.group("value").strip(), _english_digit(m.group("label")))
             for m in _PAIR_RE.finditer(text or "")]
    if len(found) != 4:
        return None
    pairs = {}
    for value, label in found:
        if not value or label in pairs:
            return None
        pairs[label] = value
    if set(pairs) != {"1", "2", "3", "4"}:
        return None
    return [(label, pairs[label]) for label in ("1", "2", "3", "4")]


def _lines_with_offsets(text):
    """[(line, start, end)] — end is exclusive of the line break.

    A leading run of combining marks and spaces is absorbed into the
    line's own start, so a mark the producer drew as its own glyph never
    sits in front of a question number: "\u0654 55- \u0628\u06cc\u0646" reads as
    "55- \u0628\u06cc\u0646".  Nothing is dropped - the mark still belongs to
    the text after it - and every offset still names its own character.
    """
    out, pos = [], 0
    for chunk in text.split("\n"):
        # the split consumed the "\n"; a CRLF stream leaves its "\r" here
        width = len(chunk)
        line = chunk[:-1] if chunk.endswith("\r") else chunk
        lead = 0
        while lead < len(line) and (line[lead] in _LEAD_MARKS
                                  or line[lead] in " \t"):
            lead += 1
        out.append((line[lead:], pos + lead, pos + len(line)))
        pos += width + 1
    return out


# A second option marker inside an already-open option line. OCR happily
# renders "۱) ۱۰۰۰  ۲) ۲۰۰۰  ۳) ۱۵۰" as one line, so the marker has to be
# found mid-line, never only at the start.
# An option label inside a line: "2) text" and the booklet's "2( text".
_INLINE_MARK = re.compile(
    r"(?<=\s)[\(\[]?\s*([1-4\u06F1-\u06F4\u0661-\u0664])\s*[\(\)]\s*(?=\S)")


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


# ---- text coherence: a print's line breaks must not break sentences ----
#
# The pdfium text stream of these booklets is not guaranteed visual order
# and often splits a sentence across lines for no reason; a block composed
# with hard "\n" joins reads broken. The reflow below joins two adjacent
# stem/lead pieces when the first does not end a sentence and the second is
# not a new structure. Geometry is untouched: these helpers run on composed
# text only, never on the char-span ranges.

# A line ending with one of these reads finished: the Persian sentence
# marks plus the closing brackets a formula line usually ends with.
_SENTENCE_END = "؟!.,؛،:)]"

# A following piece that starts a new structure must not be swallowed:
# an option-style marker row, a page/section marker, a header pipe.
_NEW_PIECE_RE = re.compile(r"^(?:\d{1,3}\s*[\)\(]|\||===|SPLIT)")


def _joins(left, right):
    """Should `left` flow into `right` inside one block's text?"""
    left, right = (left or "").rstrip(), (right or "").lstrip()
    if not left or not right:
        return False
    if left[-1] in _SENTENCE_END or "|" in left:
        return False
    if _NEW_PIECE_RE.match(right):
        return False
    return True


def reflow_lines(lines):
    """Join the pieces a print wrapped mid-sentence; keep structure lines."""
    out = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        if out and _joins(out[-1], line):
            out[-1] = out[-1] + " " + line
        else:
            out.append(line)
    return out


def mid_sentence_breaks(text):
    """True when reflow_lines would repair a hard wrap in this block text."""
    lines = [(line or "").strip() for line in (text or "").split("\n")]
    return any(_joins(lines[i], lines[i + 1])
               for i in range(len(lines) - 1))


def one_line(value):
    """A string that must never wrap: line breaks become single spaces."""
    text = str(value or "").replace("\r\n", " ").replace("\r", " ")
    return re.sub(r"[ \t]{2,}", " ", text.replace("\n", " ")).strip()


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

        if kind is None and _DASH_STEM_RE.match(stripped):
            # A lone dash opens a numberless question.  Inside an option run
            # that is already collecting options the same dash is content, so
            # only a line that starts a block (or follows one) opens a stem.
            # A dashed stem opens a block when nothing is open yet, or when
            # what is open is still pure prose (the page header).  A real
            # numbered question keeps its dashes as content.
            opens = (current is None
                     or (current.get("number") is None and run_count == 0))
            if opens:
                if current is not None:
                    current["run_start"] = run_start
                    raw_blocks.append(current)
                # A numberless stem keeps its own first line as the
                # head: there is no marker to strip from it.
                current = {"number": None, "lines": [(line, start, end)],
                           "head": _DASH_STEM_RE.sub("", stripped).strip(),
                           "run_start": None, "dash_stem": True}
                run_count, run_start = 0, None
                numeric_seen = False
                expected_option = 1
                letter_next = 0
                continue

        pair_row = _pair_row(stripped) if current is not None else None
        if pair_row:
            # "120 )4 100 )3 90 )2 80 )1" is this question's four options,
            # drawn right to left with each label after its own value.
            current["pair_options"] = pair_row
            current["lines"].append((line, start, end))
            run_count, expected_option = 4, None
            numeric_seen = True
            continue

        if kind is not None and kind[0] == "num":
            value = kind[1]
            head_match = _MARK_RE.match(stripped)
            if (current is None and value <= 4 and head_match
                    and "(" in head_match.group(0)[:head_match.start(2)]):
                # "1( only an option label" at the very start of a page is
                # not a question: an open paren is the option form, and an
                # option with nothing above it belongs to no question.
                lead.append((line, start, end))
                continue
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
            # _letter_index returns None for a letter it does not rank.
            if (current is not None and idx is not None and idx >= 0
                    and letter_next is not None
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
        body = "\n".join(reflow_lines(
            _stripped(l) for l, _, _ in lead))
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
                # A wrapped option tail continues the option above it, but
                # a finished sentence does not: a value that closes with
                # its own punctuation ends whatever was open.
                if _looks_like_sentence(stripped):
                    # Once the options have closed, running text is not
                    # part of the stem either - it is a stray page line,
                    # and the stem keeps only what preceded the options.
                    in_options = False
                    continue
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
        if raw.get("pair_options"):
            # A four-pair row is the question's whole option set; it wins
            # over whatever the per-line scan collected.
            opts = [{"label": label, "text": value}
                    for label, value in raw["pair_options"]]
        out.append({
            # A numberless dashed stem with its own option run is still a
            # question - the booklet simply printed no number for it.
            "kind": ("question" if raw["number"] is not None
                     or (raw.get("dash_stem") and len(opts) >= 2)
                     else "prose"),
            "number": raw["number"],
            "text": "\n".join(reflow_lines(stem)),
            "options": opts,
            "metadata_line": meta,
            "ranges": [(s, e) for _, s, e in raw["lines"]],
        })
    return _absorb_option_orphans(out)


# ── page organizer: question anchors ───────────────────────────────────────
#
# ``split_text`` is line-local: it opens a question only when a marker sits at
# the start of a LINE. The producer of a real exam PDF draws some numbers glued
# to a formula ("1657-", "2064-"), so the marker lands mid-line and the whole
# question merges into its neighbour. The organizer below reads the page, finds
# the question anchors by their printed sequence, and runs ``split_text`` on
# each anchored slice. It is deterministic: no model, no network, and the old
# splitter stays the fallback for a page with no anchors.

# A dash after a number, with spaces allowed but never a line break: an option
# row like "-9/6" belongs to the previous line and is not an anchor.
_ANCHOR_RE = re.compile(r"(\d{1,4})[ \t]*[-\u2013\u2014]")


def _anchor_candidates(text):
    """[(start, digits, strong)] for every "number-dash" run on the page.

    ``strong`` means the number opens its line (only bidi controls and the
    producer's own combining marks may precede it) - the one candidate that
    may seed a page without a known previous number.
    """
    out = []
    for match in _ANCHOR_RE.finditer(text or ""):
        digits = to_english_digits(match.group(1))
        if not digits.isdigit():
            continue
        line_start = (text or "").rfind("\n", 0, match.start()) + 1
        lead = (text or "")[line_start:match.start()].strip(
            " \t" + _BIDI + _LEAD_MARKS)
        out.append((match.start(), digits, lead == ""))
    return out


def question_anchors(text, start=None):
    """[(start_offset, number)] - where the questions start on one page.

    The accepted set follows the printed sequence: the next number, the tail
    of a glued digit run ("1657-" -> 57), a one-digit tail of the next number
    ("6-" after 65 -> 66), or the next-but-one number when the text layer lost
    one question entirely. Every anchor is a cut point; anything that cannot
    be placed in that sequence stays with the block before it.
    """
    candidates = _anchor_candidates(text)
    if start is not None:
        # The previous page ended with ``start``: every candidate on this page
        # is judged against that number, so a question glued to a formula at
        # the very top of the page ("−68-(") is still found.
        previous = int(start)
        out = []
        rest = candidates
    else:
        seed = next((i for i, (_, digits, strong) in enumerate(candidates)
                     if strong and len(digits) <= 3), None)
        if seed is None:
            return []
        at, digits, _ = candidates[seed]
        number = int(digits)
        if number <= 0 or number > 999:
            return []
        out = [(at, number)]
        previous = number
        rest = candidates[seed + 1:]
    for start, digits, _strong in rest:
        number = int(digits)
        if number <= 0:
            continue
        expected = previous + 1
        if number == expected:
            out.append((start, expected))
            previous = expected
        elif len(digits) >= 3 and int(digits[-2:]) in (expected,
                                                       expected + 1):
            # "1657-" / "2064-": the question number is the tail, the head
            # belongs to the formula that ended the previous line. The tail
            # may also close a one-question gap (55 then 1657 -> 57).
            tail = int(digits[-2:])
            out.append((start + len(digits) - 2, tail))
            previous = tail
        elif len(digits) == 1 and expected >= 10 and expected % 10 == number:
            out.append((start, expected))
            previous = expected
        elif number == expected + 1:
            # A text layer can lose one question; the next printed anchor is
            # still a question boundary and must not merge into the previous.
            out.append((start, number))
            previous = number
    return out


def _anchor_chunk(chunk):
    """Make a dash-glued anchor readable by ``split_text``'s marker rule.

    ``split_text`` refuses "70- 1" (the dash must not be followed by a
    digit), but an anchored slice already knows where the question starts.
    Rewriting that one separator to the option-style paren keeps the text
    and every offset intact and lets the stem begin with the number.
    """
    match = re.match(r"^(\d{1,4})[ \t]*[-\u2013\u2014](?=[ \t]*\d)",
                     chunk or "")
    if not match:
        return chunk
    dash = match.start() + len(match.group(1)) + 1
    return chunk[:dash] + ")" + chunk[dash + 1:]


def split_page(text, boxes=None, page_size=None, start=None):
    """One page's text -> blocks + a layout confidence.

    ``boxes``/``page_size`` are accepted for callers that already hold pdfium
    character geometry (the text lane); the anchor rule works on the text and
    the offsets, so a caller without geometry behaves identically. Returns
    {"blocks", "confidence", "anchors", "questions"}.
    """
    text = text or ""
    anchors = question_anchors(text, start=start)
    if not anchors:
        blocks = split_text(text)
        return {"blocks": blocks, "confidence": 0.0, "anchors": 0,
                "questions": sum(1 for b in blocks
                                 if b.get("kind") == "question"),
                "last": start}

    cuts = [start for start, _ in anchors]
    slices = []
    if cuts[0] > 0:
        slices.append((0, text[:cuts[0]], None))
    for index, start in enumerate(cuts):
        end = cuts[index + 1] if index + 1 < len(cuts) else len(text)
        slices.append((start, text[start:end], anchors[index][1]))

    blocks = []
    for offset, chunk, number in slices:
        chunk = _anchor_chunk(chunk)
        opened = 0
        for block in split_text(chunk):
            block["ranges"] = [(s + offset, e + offset)
                               for s, e in block.get("ranges") or []]
            if (number is not None and opened == 0
                    and block.get("kind") == "question"):
                # "6-" after 65 is really question 66 (the producer dropped a
                # digit). The sequence is the authority on the number; the
                # block keeps the text it landed.
                block["number"] = number
                opened += 1
            blocks.append(block)

    by_number = {block.get("number"): block for block in blocks
                 if block.get("kind") == "question"}
    good = 0
    for _start, number in anchors:
        block = by_number.get(number)
        if (block and (block.get("text") or "").strip()
                and len(block.get("options") or []) == 4):
            good += 1
    confidence = good / float(len(anchors))
    return {"blocks": blocks, "confidence": confidence,
            "anchors": len(anchors),
            "numbers": [number for _start, number in anchors],
            "questions": sum(1 for b in blocks if b.get("kind") == "question"),
            "last": anchors[-1][1]}


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
