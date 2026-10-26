"""Canonical math delimiters, written ONCE when a block is first stored.

The dashboard's ``fenceBareTex`` (``ui/app.js``) repairs bare OCR TeX at render
time for every consumer. That repair is still the safety net for legacy rows,
but the stored text should already be canonical: a block keeps one form of
delimiters, a reviewer sees the same form in Inspect and in the edit box, and
a re-render changes nothing.

What counts as canonical: the OCR delimiter zoo collapses to ``$`` for inline
math and ``$$`` for display math, and a formula that lost its delimiters
entirely (``\\frac{6}{3}``, ``x^{2}``) gets ``$...$``.

The rules mirror the renderer's, step for step, so a normalized text renders
exactly like the raw text did:

1. ``\\begin{table}`` unwraps to its body, ``tabular`` becomes ``array`` and
   ``\\centering`` disappears — MathJax has no float environments.
2. A bare ``\\begin{env}...\\end{env}`` becomes display math.
3. Inside a ``$$`` block a nested ``$`` is dropped (the delimiters are already
   there); an escaped ``\\$`` survives.
4. ``\\(...\\)`` becomes ``$...$`` and ``\\[...\\]`` becomes ``$$...$$``.
5. What is left, outside every fenced region, is scanned for strong TeX
   (a command, ``_{...}``/``^{...}`` or ``_x``/``^x``) and the run around it is
   fenced.

Pure text in, pure text out; no I/O, no model, no database.
"""
import re

_PERSIAN = re.compile(u"[\u0600-\u06FF\u0750-\u077F]")
_WORD = re.compile(r"[A-Za-z0-9]")
_CMD = re.compile(r"\\[A-Za-z]+$")
_CMD_AHEAD = re.compile(r"^\\[A-Za-z]+")
# One nested brace level, like the renderer's ``strong`` pattern.
_STRONG = re.compile(r"\\[A-Za-z]+|[_^]\{(?:[^{}]|\{[^{}]*\})*\}|[_^][A-Za-z0-9]")
_PUNCT = "+-*/().,;:=[]~%'!?_^"
# Delimiter normalization, applied in this order so a conversion never
# produces a region the next step would fence again.
_PAREN = re.compile(r"\\\((.+?)\\\)", re.S)
_BRACK = re.compile(r"\\\[(.+?)\\\]", re.S)
_ENV = re.compile(r"(\\begin\{[a-zA-Z*]+\}[\s\S]*?\\end\{[a-zA-Z*]+\})")
_DISPLAY = re.compile(r"\$\$([\s\S]*?)\$\$")
_PROTECTED = re.compile(
    r"\$\$[\s\S]*?\$\$|\$[^$\n]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")


def _grow_left(text, end):
    """Start of the TeX run that ends at ``end`` (spaces skipped on the way)."""
    pos = end
    while True:
        j = pos
        while j > 0 and text[j - 1] in " \t":
            j -= 1
        if j == 0:
            break
        ch = text[j - 1]
        if ch == "}":
            depth = 0
            k = j - 1
            while k >= 0:
                if text[k] == "}":
                    depth += 1
                elif text[k] == "{":
                    depth -= 1
                    if not depth:
                        break
                k -= 1
            if k < 0:
                break
            start = k
            cmd = _CMD.search(text[:k])
            if cmd:
                start = k - len(cmd.group(0))
            pos = start
            continue
        if ch == "\\":
            cmd = _CMD.search(text[:j])
            if not cmd:
                break
            pos = j - len(cmd.group(0))
            continue
        if _WORD.match(ch):
            k = j
            while k > 0 and _WORD.match(text[k - 1]):
                k -= 1
            pos = k
            continue
        if ch in _PUNCT:
            prev = text[j - 2] if j >= 2 else ""
            # A Persian full stop/colon is prose punctuation, not TeX.
            if ch in ".,;:" and prev and _PERSIAN.match(prev):
                break
            pos = j - 1
            continue
        break
    return pos


def _grow_right(text, start):
    """End of the TeX run that starts at ``start``."""
    pos = start
    while True:
        j = pos
        while j < len(text) and text[j] in " \t":
            j += 1
        if j >= len(text):
            break
        ch = text[j]
        if ch == "{":
            depth = 0
            k = j
            while k < len(text):
                if text[k] == "{":
                    depth += 1
                elif text[k] == "}":
                    depth -= 1
                    if not depth:
                        break
                k += 1
            if k >= len(text):
                break
            pos = k + 1
            continue
        if ch == "\\":
            cmd = _CMD_AHEAD.match(text[j:])
            if not cmd:
                break
            pos = j + len(cmd.group(0))
            continue
        if _WORD.match(ch):
            k = j
            while k < len(text) and _WORD.match(text[k]):
                k += 1
            pos = k
            continue
        if ch in _PUNCT:
            nxt = text[j + 1] if j + 1 < len(text) else ""
            if ch in ".,;:" and nxt and _PERSIAN.match(nxt):
                break
            pos = j + 1
            continue
        break
    return pos


def _fence_plain(text):
    """Fence every strong TeX run in one unfenced stretch of text."""
    if "\\" not in text and "_" not in text and "^" not in text:
        return text
    out, cursor = [], 0
    for match in _STRONG.finditer(text):
        if match.start() < cursor:
            continue
        start = _grow_left(text, match.start())
        end = _grow_right(text, match.end())
        if start < cursor:
            start = cursor
        if end <= start:
            continue
        out.append(text[cursor:start] + "$" + text[start:end] + "$")
        cursor = end
    out.append(text[cursor:])
    return "".join(out)


def fence_bare(text):
    """Fence bare TeX outside the math that already has delimiters."""
    if not text:
        return text
    out, last = [], 0
    for match in _PROTECTED.finditer(text):
        out.append(_fence_plain(text[last:match.start()]))
        out.append(match.group(0))
        last = match.end()
    out.append(_fence_plain(text[last:]))
    return "".join(out)


def _drop_inner_dollars(match):
    """``$$ ... $ ... $$`` -> one display block; the inner ``$`` pairs go.

    Two fences never share the same ``$$``: the greedy walk would otherwise
    steal the other block's closing delimiter and emit ``$$$`` (a MathJax
    error stitched onto a broken neighbour). A body with no inner ``$`` is
    returned untouched.
    """
    inner = match.group(1)
    if "$" not in inner.replace("\\$", ""):
        return "$$" + inner + "$$"
    return "$$" + re.sub(r"(^|[^\\])\$", r"\1", inner) + "$$"


def canonical(text):
    """One stored text -> the same text with canonical math delimiters.

    Idempotent: ``canonical(canonical(x)) == canonical(x)``.
    """
    s = text or ""
    if not s:
        return s
    s = re.sub(r"\\begin\{table\}(?:\[[^\]]*\])?", "", s)
    s = s.replace("\\end{table}", "")
    s = re.sub(r"\\begin\{tabular\}(?:\[[^\]]*\])?", "\\\\begin{array}", s)
    s = s.replace("\\end{tabular}", "\\end{array}")
    s = re.sub(r"\\centering\b\s*", "", s)
    if "\\begin{" in s and "$$" not in s and "\\[" not in s:
        s = _ENV.sub(lambda m: "$$" + m.group(1) + "$$", s)
    s = _DISPLAY.sub(_drop_inner_dollars, s)
    # Leftover ``$$`` (an unpaired fence the greedy walk above skipped) would
    # make every later delimiter pair up wrong: they become a plain ``$``.
    s = re.sub(r"\$\$(?![\s\S]*\$\$)", "$", s) if s.count("$$") % 2 else s
    s = _BRACK.sub(lambda m: "$$" + m.group(1) + "$$", s)
    s = _PAREN.sub(lambda m: "$" + m.group(1) + "$", s)
    s = fence_bare(s)
    return _collapse_empty(s)


def _collapse_empty(s):
    """``$$$$`` (a display that lost its body) is nothing, not a MathJax error."""
    return re.sub(r"\$\$\s*\$\$", "", s)

# ─── Persian half-spaces (ZWNJ) ────────────────────────────────────────────
# The scanned pages' text layers carry no ZWNJ at all: ``میشود`` arrives as
# ``میشود``.  Persian reads (and shapes in a browser) differently without the
# half-space, so the ones *required* by orthography are restored — from the
# closed set of productive affixes below, never on a guess.  A missed half-space
# leaves a readable word; a wrong one invents a word that was never printed.

_PREFIXES = ("نمی", "می")
# The comparative "t-ar" is deliberately absent: "بیشتر" is one word.
_SUFFIXES = ("هایی", "های", "ها", "ترین")

_FA = "\u0621-\u064A\u066E-\u06D3\u06D5\u06FA-\u06FF\u200C\u200D"
_FA_WORD = re.compile(r"[%s]+" % _FA)

# Stems that begin with "می" as part of the word ("میان" must not split).
_KEEP_MIY = frozenset("""
میان میانا میانجی میانجیگری میانگین میدان میدانی میدانگاه میانه
میر میرت میراث میرزا میرا میزان میز میزبان میزگرد میسل میگرن
میل میلاد میلی میلیارد میلیون میله میم میمند میهمان میوه
میگو میگویی میروم میرم میری میبینم میکنم میدهم میخورم
""".split())

# Stems that end in "ها"/"تر" as part of the word, not as a suffix.
_KEEP_HA = frozenset("""
تنها رها بها گها نگاه شهادت گواهی همراه ماه راه گناه گیاه
""".split())


def _restore_prefix(word):
    if "\u200c" in word or word in _KEEP_MIY:
        return word
    for prefix in _PREFIXES:
        if not word.startswith(prefix):
            continue
        rest = word[len(prefix):]
        if len(rest) < 2 or rest in _KEEP_MIY:
            return word
        return prefix + "\u200c" + rest
    return word


def _restore_suffix(word):
    if "\u200c" in word or word in _KEEP_HA:
        return word
    for suffix in _SUFFIXES:
        if not word.endswith(suffix):
            continue
        stem = word[:-len(suffix)]
        if len(stem) < 2 or stem in _KEEP_HA:
            return word
        return stem + "\u200c" + suffix
    return word


def restore_zwnj(text):
    """Put the orthographically required Persian half-spaces back.

    Only U+200C is ever added, so a repaired string is the original plus
    half-spaces - readable and comparable, never re-worded.
    """
    if not text or "\u200c" not in text and not _FA_WORD.search(text):
        return text
    out, last = [], 0
    for match in _FA_WORD.finditer(text):
        start, end = match.span()
        out.append(text[last:start])
        word = text[start:end]
        if len(word) < 3 or not any("\u0600" <= ch <= "\u06FF" for ch in word):
            out.append(word)
        else:
            out.append(_restore_suffix(_restore_prefix(word)))
        last = end
    out.append(text[last:])
    return "".join(out)


def missing_zwnj(text):
    """True when ``restore_zwnj`` would change this text."""
    return bool(text) and restore_zwnj(text) != text


# ─── canonical years and institutes ───────────────────────────────────────
# `answers.year` in the live DB is free text in two digit systems
# (``۱۴۰۲-۱۴۰۳`` 1,458 rows, ``1403-1404`` 852, ``1403_1404`` 1, ...), so a
# match that compares the raw column can only ever fail.  A canonical year is
# ASCII digits joined by one "-", the first four-digit year first.

_PERSIAN_DIGITS = {chr(0x06F0 + i): str(i) for i in range(10)}   # ۰-۹
_ARABIC_DIGITS = {chr(0x0660 + i): str(i) for i in range(10)}    # ٠-٩
_DIGIT_MAP = str.maketrans({**_PERSIAN_DIGITS, **_ARABIC_DIGITS})
_SEP = re.compile(r"[\s_\u2010-\u2015\u2212/\\.,؛،]*-[\s_\u2010-\u2015\u2212/\\.,؛،]*")
_YEAR = re.compile(r"\d{4}")


def latin_digits(text):
    """Persian and Arabic-Indic digits to ASCII; everything else untouched."""
    return (text or "").translate(_DIGIT_MAP)


def canonical_year(raw):
    """``۱۴۰۲-۱۴۰۳`` / ``1403_1404`` / ``۱۴۰۲ ۱۴۰۳`` -> ``"1402-1403"``.

    Returns "" when no year can be read, so a caller can treat a missing year
    as missing instead of matching on an accident.
    """
    text = latin_digits(str(raw or "")).strip()
    if not text:
        return ""
    years = sorted({y for y in _YEAR.findall(text)})
    if len(years) >= 2:
        return years[0] + "-" + years[1]
    if years:
        return years[0]
    digits = "".join(ch for ch in text if ch.isdigit())
    return digits
