"""Math and letter normalisation (salvaged from the retired Studio row builder).

Two deterministic repairs that keep OCR maths printable, imported here so the
staging Inspect renderer and the importer share one implementation:

* ``brace_subscripts`` — MathJax 3 rejects a bare one-character subscript inside
  ``\\mathrm`` when another subscripted letter follows ("Double subscripts: use
  braces to clarify"), which the OCR hits as ``C_nH_{2n+2}``;
* ``normalise_math`` — the OCR mixes notations (``C_2H_6``, ``\\mathrm{H_2O}``,
  Persian digits inside maths), so every math span is brought to the house
  style: Latin digits, upright chemical formulas.

Both are pure text in / text out and idempotent.
"""
import re

from .textnorm import latin_digits as to_english_digits  # one digit rule

_MATH_RM = re.compile(r"\\mathrm\{((?:[^{}]|\{[^{}]*\})*)\}")
_BARE_SUB = re.compile(r"_(?!\{)([A-Za-z0-9])")


def brace_subscripts(text):
    """``\\mathrm{C_nH_2}`` -> ``\\mathrm{C_{n}H_2}``; everything else untouched."""
    if not text or "\\mathrm" not in text:
        return text
    return _MATH_RM.sub(
        lambda m: "\\mathrm{" + _BARE_SUB.sub(r"_{\1}", m.group(1)) + "}", text)


_MATH_SPAN = re.compile(
    r"\$\$[\s\S]*?\$\$|\$[^$]*\$|\\\([\s\S]*?\\\)|\\\[[\s\S]*?\\\]")
_ELEMENT = r"[A-Z][a-z]?(?:_\{?\d+\}?)?"
_STATE = r"(?:\((?:g|l|s|aq)\))?"
_CHEM_RUN = re.compile(
    # a run of >=2 element groups (C_2H_6, N_2O_5, SO_2) — an OCR coefficient
    # in front (2N_2) is left outside the match
    r"(?<![A-Za-z\\{])(?:" + _ELEMENT + r"){2,}" + _STATE +
    # or one element carrying a subscript (O_2, H_2), never a bare variable
    r"|(?<![A-Za-z\\{])(?:[A-Z][a-z]?)_\{?\d+\}?" + _STATE)
_ELEMENT_PARTS = re.compile(r"([A-Z][a-z]?)(?:_\{?(\d+)\}?)?")
_BOND = re.compile(
    r"(?<![A-Za-z\\{])(?:[A-Z][a-z]?(?:\s*[-=\u2261]\s*[A-Z][a-z]?)+)(?![A-Za-z0-9])")
_MACRO_START = re.compile(r"\\(?:mathrm|text)\{")
# Persian prints decimals with a slash (۲/۵۶ is 2.56) — inside maths a plain
# slash would read as a fraction, so only a Persian-typed slash becomes a dot.
_PERSIAN_DECIMAL = re.compile(r"([۰-۹]+)\s*[/٫]\s*([۰-۹]+)")


def _protected_spans(text):
    """(start, end) of every ``\\mathrm{...}`` / ``\\text{...}`` group."""
    spans, pos = [], 0
    while True:
        match = _MACRO_START.search(text, pos)
        if not match:
            return spans
        depth, index = 0, match.end() - 1          # the opening brace
        while index < len(text):
            char = text[index]
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    break
            index += 1
        if index >= len(text):                     # unbalanced source: leave it be
            return spans
        spans.append((match.start(), index + 1))
        pos = index + 1


def _upright(match):
    """``C_2H_6(g)`` -> ``\\mathrm{C}_{2}\\mathrm{H}_{6}\\mathrm{(g)}``."""
    token, state = match.group(0), ""
    trail = re.search(r"\((?:g|l|s|aq)\)$", token)
    if trail:
        state = "\\mathrm{%s}" % trail.group(0)
        token = token[:trail.start()]
    out, letters = [], ""
    for element, count in _ELEMENT_PARTS.findall(token):
        letters += element
        if count:
            out.append("\\mathrm{%s}_{%s}" % (letters, count))
            letters = ""
    if letters:
        out.append("\\mathrm{%s}" % letters)
    return "".join(out) + state


def _outside_macros(text, change):
    """Apply ``change`` only between ``\\mathrm{...}`` / ``\\text{...}`` groups."""
    out, pos = [], 0
    for start, end in _protected_spans(text):
        out.append(change(text[pos:start]))
        out.append(text[start:end])
        pos = end
    out.append(change(text[pos:]))
    return "".join(out)


def normalise_math(text):
    """Math spans: Latin digits, upright chemical formulas (book notation)."""
    if not text or ("$" not in text and "\\(" not in text and "\\[" not in text):
        return text

    def span(match):
        body = _PERSIAN_DECIMAL.sub(r"\1.\2", match.group(0))
        digits = to_english_digits(body)
        return _outside_macros(
            digits,
            lambda part: _BOND.sub(lambda b: "\\mathrm{%s}" % b.group(0),
                                   _CHEM_RUN.sub(_upright, part)))

    return _MATH_SPAN.sub(span, text)


# The printed option labels a key page can use, in option order.
_LABELS = (("1", "الف", "ا"), ("2", "ب"), ("3", "ج"), ("4", "د"),
           ("5", "ه"), ("6", "و"))


def _clean_label(value):
    return to_english_digits(str(value or "")).strip().strip("()[]-.").strip()


def _same_label(a, b):
    """"2" vs "۲", "ب" vs the same letter written differently: same option."""
    if a == b:
        return True
    return any(a in names and b in names for names in _LABELS)


def option_text(options, label):
    """The option text a key letter/number points at, or None.

    ``options`` is the normalized ``[{label, text}]`` list; a label match wins,
    position is the fallback for lists that carry no labels.
    """
    want = _clean_label(label)
    if not want or not options:
        return None
    for option in options:
        if isinstance(option, dict) \
                and _same_label(_clean_label(option.get("label")), want):
            return option.get("text")
    for index, names in enumerate(_LABELS):
        if want in names and index < len(options):
            option = options[index]
            return option.get("text") if isinstance(option, dict) else option
    return None
