"""Persian half-spaces (ZWNJ) for text that arrives without them.

This exam PDF's text layer carries **no ZWNJ characters at all** in body text:
``می‌شود`` is stored as ``میشود`` and ``زیست‌شناسی`` is stored as ``زیستشناسی``
(only a few title strings survive).  Persian is read, and shaped in a browser,
quite differently when a half-space is missing, so the text must be put back
before it is stored.

The repair is deliberately narrow: a half-space is restored only where Persian
orthography *requires* it, using the closed set of productive affixes and
compounds below.  A missed half-space leaves a readable word; a wrongly added
one invents a word that is not there, so nothing is repaired on a guess.

Two families are handled:

* **prefix + stem** - ``می``, ``نمی``, ``ها`` and the verb negation that ends
  one: ``میشود`` -> ``می‌شود``, ``نمیکند`` -> ``نمی‌کند``;
* **stem + suffix** - the plural and object suffixes that never join:
  ``ها``, ``های``, ``هایی``, ``تر``, ``ترین``.

Words that genuinely join (``همیشه``, ``کتابها`` as printed without plural
marks, ``آموزش``) are protected: the stem list is checked before the prefix
rule, so ``میان`` never becomes ``می‌ان``.
"""
import re

# Prefixes written with a half-space before the stem.
_PREFIXES = ("نمی", "می")
# Suffixes written with a half-space after the stem.  The comparative "t-ar"
# is deliberately absent: "بیشتر" (more) is one word, and
# "بیش‌تر" is not how it is printed.
_SUFFIXES = ("هایی", "های", "ها", "ترین")

# A word is Persian when it is made of Persian letters (and the marks that
# ride on them).
_FA = "\u0621-\u064A\u066E-\u06D3\u06D5\u06FA-\u06FF\u200C\u200D"
_WORD_RE = re.compile(r"[%s]+" % _FA)

# Stems that begin with "می" as part of the word itself.  Repairing these
# would invent a word ("میان" -> "می‌ان"), so they are skipped outright.
_KEEP_MIY = frozenset("""
میان میانا میانجی میانجیگری میانگین میدان میدانی میدانگاه میانه
میر میرت میراث میرزا میرا میزان میز میزبان میزگرد میسل میگرن
میل میلاد میلی میلیارد میلیون میله میم میمند میمند میهمان میوه
میگو میگویی میروم میرم میری میبینم میکنم میدهم میخورم
""".split())

# Stems that end in "ها"/"تر" as part of the word, not as a suffix.
_KEEP_HA = frozenset("""
تنها رها بها گها نگاه شهادت گواهی همراه ماه راه گناه گیاه
""".split())


def _bounded(text, index):
    """True when ``index`` starts a word (a non-letter sits before it)."""
    return index == 0 or not (text[index - 1].isalpha() and
                              _WORD_RE.match(text[index - 1]))


def _restore_prefix(word):
    """``میشود`` -> ``می‌شود``; a stem that merely starts with ``می`` stays."""
    if "\u200c" in word or word in _KEEP_MIY:
        return word
    for prefix in _PREFIXES:
        if not word.startswith(prefix):
            continue
        rest = word[len(prefix):]
        # A stem of at least two letters, or the word is too short to split.
        if len(rest) < 2 or rest in _KEEP_MIY:
            return word
        return prefix + "\u200c" + rest
    return word


def _restore_suffix(word):
    """``کتابهای`` -> ``کتاب‌های``; ``تنها`` and ``رها`` stay whole."""
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


def restore(text):
    """Return ``text`` with the Persian half-spaces put back.

    Characters are never added or dropped elsewhere, so the string only ever
    gains U+200C; offsets into the repaired text are therefore offsets into the
    original plus the half-spaces already inserted before them.
    """
    if not text or "\u200c" not in text and not _WORD_RE.search(text):
        return text
    out = []
    last = 0
    for match in _WORD_RE.finditer(text):
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


def missing(text):
    """True when ``text`` carries a Persian word this module would repair."""
    if not text:
        return False
    return restore(text) != text
