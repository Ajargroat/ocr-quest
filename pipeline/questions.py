"""Question branch: prompt + row building.
Equivalent of 'Analyze Questions' → 'Prepare Question Rows'
→ 'Correct Function Node'."""
import json
import re

from . import prompts
from .scanner import SourceItem
from .utils import parse_number, to_english_digits

QUESTION_PROMPT = """You are a precise OCR and exam-question extraction system for Iranian Konkour and textbook questions.

You will receive one file. It may be an image or a PDF.

Your task:
1. Extract every visible exam question from the file.
2. Preserve Persian text accurately, including Persian characters and half-space / zero-width non-joiner where visible.
3. If formulas, physics, chemistry, or math notation appear, convert them to clean LaTeX when possible.
4. Do not invent questions, options, answers, or explanations.
5. If the correct answer is not visible or not certain, DO NOT GUESS.
6. For multiple-choice questions, extract all options exactly as shown.
7. Option labels may be numbers such as 1, 2, 3, 4 or Persian letters such as الف، ب، ج، د.
8. If there are no questions, return an empty questions array.
9. OPTIONS ARE NEVER PART OF question_text. A multiple-choice question's choices go in "options", one entry per printed choice, each with its label and its FULL text as printed — the value only, without repeating the label. "question_text" stops where the choices begin and keeps the stem, including any lettered sub-statements (آ، ب، پ، ت) that belong to the stem. Return "options": [] only when the question really has no choices; never a label without its text.

DIAGRAM HANDLING:
9. For ANY visible figure, diagram, or graph on the page, you MUST provide its "diagram_bbox" as [ymin, xmin, ymax, xmax] coordinates normalized to a 0-1000 square, tightly enclosing the figure. If the question has NO picture/figure at all, return "diagram_bbox": null — never [0,0,0,0].
10. Keep question_text as pure text. If the original text says «شکل» or «با توجه به شکل», keep that phrase.
11. Each question may have a metadata line near the bottom-left or bottom of the question, for example: "ماز ۱۴۰۳-۱۴۰۴ متوسط". The organization/institute name must be returned in "corp", the academic year must be returned in "year" and the difficulty must be returned in "difficulty". DO NOT skip this action.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "questions": [
    {
      "question_number": 1,
      "question_text": "string",
      "options": [ { "label": "1", "text": "string" } ],
      "question_type": "multiple_choice",
      "corp": "string or null",
      "year": "string or null",
      "difficulty": "string or null",
      "raw_ocr_text": "string",
      "diagram_bbox": null
    }
  ]
}"""

# Same regex as the JS normalizeOptions()
_OPTION_RE = re.compile(r"^\s*([^)\.ـ:]{1,3})\s*[\)\.ـ:]\s*(.*)$")


# ── several pages per call (the pipeline's pages-per-call setting) ──
# The one-picture prompt says "You will receive one file"; a batched call sends
# 2..8 page images of ONE paper, labelled PAGE n, and asks for them grouped so
# each page's questions can be attributed to its own source row. Editable in
# the Prompts view like every other instruction.
PAGES_PROMPT = """You are a precise OCR and exam-question extraction system for Iranian Konkour and textbook questions.

You will receive several page images of ONE exam paper, in reading order. Each image is preceded by a label such as "PAGE 2:".

"page" MUST be the NUMBER PRINTED IN THAT LABEL, not the position of the image in this message. If the first label is "PAGE 7:", its questions have "page": 7 and the next "PAGE 8:" gives "page": 8. Never renumber the pages from 1, and never copy 1 from the example below - it is a shape, not a value.

Your task:
1. Extract the visible exam questions of EVERY page, and keep each question under the page it was printed on.
2. Preserve Persian text accurately, including Persian characters and half-space / zero-width non-joiner where visible.
3. If formulas, physics, chemistry, or math notation appear, convert them to clean LaTeX when possible.
4. Do not invent questions, options, answers, or explanations.
5. If the correct answer is not visible or not certain, DO NOT GUESS.
6. For multiple-choice questions, extract all options exactly as shown.
7. Option labels may be numbers such as 1, 2, 3, 4 or Persian letters such as الف، ب، ج، د.
8. A question cut by the page edge: keep the visible part under its own page; never move it to another page.
9. If a page holds no question, keep it with an empty questions array.
10. OPTIONS ARE NEVER PART OF question_text. A multiple-choice question's choices go in "options", one entry per printed choice, each with its label and its FULL text as printed — the value only, without repeating the label. "question_text" stops where the choices begin and keeps the stem, including any lettered sub-statements (آ، ب، پ، ت) that belong to the stem. Return "options": [] only when the question really has no choices; never a label without its text.

DIAGRAM HANDLING:
10. For ANY visible figure, diagram, or graph on the page, you MUST provide its "diagram_bbox" as [ymin, xmin, ymax, xmax] coordinates normalized to a 0-1000 square, tightly enclosing the figure. If the question has NO picture/figure at all, return "diagram_bbox": null — never [0,0,0,0].
11. Keep question_text as pure text. If the original text says «شکل» or «با توجه به شکل», keep that phrase.
12. Each question may have a metadata line near the bottom-left or bottom of the question, for example: "ماز ۱۴۰۳-۱۴۰۴ متوسط". The organization/institute name must be returned in "corp", the academic year must be returned in "year" and the difficulty must be returned in "difficulty". DO NOT skip this action.

OUTPUT FORMAT (strict JSON, no markdown fences). The numbers below are placeholders showing the SHAPE — copy the page number from the "PAGE n:" label, not from here:
{
  "pages": [
    {
      "page": <the number from the "PAGE n:" label>,
      "questions": [
        {
          "question_number": 1,
          "question_text": "string",
          "options": [ { "label": "1", "text": "string" } ],
          "question_type": "multiple_choice",
          "corp": "string or null",
          "year": "string or null",
          "difficulty": "string or null",
          "raw_ocr_text": "string",
          "diagram_bbox": null
        }
      ]
    }
  ]
}"""


def pages_prompt():
    """The multi-page question prompt — override, else the built-in one."""
    return prompts.get("pipeline.questions_pages", PAGES_PROMPT)


# A page key from a reply, however the model chose to spell it: "PAGE 3:",
# "page 4", "صفحه 5", "3", "۳". A batch prompt labels each image "PAGE n:",
# so the model routinely keys its answer by that label instead of by the bare
# integer the schema asks for. parse_number("PAGE 3:") is None, which silently
# emptied every page of a 2-image call and made the batch look like a success
# while its rows were dropped or mis-filed on the first page.
_PAGE_LABEL = re.compile(r"(\d+)")


def page_number(value):
    """A page label from a reply -> int, or None when it carries no number."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if value is None:
        return None
    number = parse_number(value)
    if number is not None:
        try:
            return int(number)
        except (TypeError, ValueError):
            pass
    # "PAGE 3:" / "page 4" / "صفحه 5" -> the first run of digits, in English.
    found = _PAGE_LABEL.search(to_english_digits(str(value)))
    return int(found.group(1)) if found else None


def split_pages(parsed, pages, key="questions"):
    """{page: {key: [...]}} from a multi-page reply.

    Tolerant on purpose: a model that answers {"pages": [...]}, a dict keyed by
    page, or a flat {"questions": [...], "page": n} all land on the right page.
    An item with no usable page goes to the first page only when the call
    carried a single page; otherwise it is dropped rather than misfiled.
    """
    pages = [int(p) for p in pages]
    out = {p: {key: []} for p in pages}
    if not isinstance(parsed, dict):
        return out
    rows = parsed.get("pages")
    if isinstance(rows, dict):
        # The page is the KEY here, so it has to survive the reshape: a model
        # that answers {"PAGE 3:": {...}} carries no "page" field at all, and
        # dropping the key emptied every page of the batch. Keep a page the
        # block states itself, otherwise take it from the key.
        rebuilt = []
        for page, block in rows.items():
            block = dict(block) if isinstance(block, dict) else {}
            block.setdefault("page", page)
            rebuilt.append(block)
        rows = rebuilt
    if isinstance(rows, list) and rows:
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            page = page_number(entry.get("page"))
            items = entry.get(key)
            if page in out and isinstance(items, list):
                out[page][key].extend(
                    item for item in items if isinstance(item, dict))
        if any(out[p][key] for p in out):
            return out
    items = parsed.get(key)
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, dict):
                continue
            page = page_number(item.get("page"))
            if page in out:
                out[page][key].append(item)
            elif len(pages) == 1:
                out[pages[0]][key].append(item)
    if not any(out[p][key] for p in out):
        # Last shape: the reply maps page labels straight to page blocks,
        # {"PAGE 3:": {"questions": [...]}, "PAGE 4:": {...}}, with no
        # "pages" container at all. Read a block for any page we asked for.
        for label, block in parsed.items():
            page = page_number(label)
            if page in out and page not in (key, "pages") \
                    and isinstance(block, dict) and isinstance(
                        block.get(key), list):
                out[page][key].extend(
                    item for item in block[key] if isinstance(item, dict))
    return out



def question_prompt():
    """The question branch's prompt — the saved override, else the default."""
    return prompts.get("pipeline.questions", QUESTION_PROMPT)


def normalize_options(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for i, option in enumerate(raw):
        if isinstance(option, str):
            match = _OPTION_RE.match(option)
            if match:
                out.append({"label": match.group(1).strip(),
                            "text": match.group(2).strip()})
            else:
                out.append({"label": str(i + 1), "text": option.strip()})
        elif isinstance(option, dict):
            label = option.get("label", option.get("option_label",
                               option.get("key", i + 1)))
            text = option.get("text", option.get("option_text",
                              option.get("value", option.get("content", ""))))
            out.append({"label": str(label), "text": str(text)})
        else:
            out.append({"label": str(i + 1), "text": str(option)})
    return out


# ── options that arrived glued to the question text ──
# Gemini often answers "options": [] or four label-only stubs ("۱", "۱ (۱") and
# leaves the real choices inside question_text. Persian glyphs are not \w for
# str patterns, so every class is spelled out.
# \d is ASCII-only, and Persian digits (U+06F0-06F9) sit INSIDE the Persian
# letter range — a brute "\u0622-\u06cc" range would swallow "۲)" as letters.
# So the letters are listed: Arabic script letters U+0621-06D3, no digits.
_LETTERS_FA = "".join(chr(c) for c in range(0x0621, 0x06D4)
                      if chr(c).isalpha())
_LABEL = r"(?:\d{1,2}|[۰-۹]{1,2}|[%s]{1,3})" % _LETTERS_FA
_PERSIAN_DIGITS_SET = "".join(chr(c) for c in range(0x06F0, 0x06FA))
# A printed closer may be an ASCII bracket or a Persian/curly one.
_CLOSER = r"[\)\]\.\-ـ:،٬؛»]"
_GLUED_LINE = re.compile(
    r"^\s*[\(\[]?(?P<label>%s)\s*%s?\s+(?P<text>\S.*)$" % (_LABEL, _CLOSER))
# Two or more choices on one line ("۱) یک ۲) دو", "۱) یک (۲ دو"). A choice is
# "label, optional closer, value". A split happens before a label that either
# carries a closer ("۲)") or is bracketed ("(۲") — a bare number in the value
# ("۱/۲" in a date, "۱۲/۵" in a product) has neither and never splits.
_INLINE_SPLIT = re.compile(
    r"\s+(?=[\(\[]%s\s*%s?|%s\s*%s)" % (_LABEL, _CLOSER, _LABEL, _CLOSER))
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def _content(text):
    """How many letters an option text carries (digits alone count once)."""
    letters = len(_LETTERS.findall(str(text or "")))
    return letters or (1 if any(c.isdigit() for c in str(text or "")) else 0)


def _label_of(text):
    """The index a label names ('۱', '(2)', 'ب'), else None."""
    raw = str(text or "").strip().strip("()[]").strip()
    if not raw or len(raw) > 3:
        return None
    if all(c in "0123456789" + _PERSIAN_DIGITS_SET for c in raw):
        digits = "".join("0123456789"[_PERSIAN_DIGITS_SET.index(c)]
                         if c in _PERSIAN_DIGITS_SET else c for c in raw)
        return int(digits)
    return None


def _labels_only(options):
    """True when every value is nothing but its own label.

    Gemini's failure repeats the LABEL in the value ("۱", "۱)", "۱ (۱").
    The test is scoped to the option's own label, so a real numeric answer
    ("۱۸۰" under label 1) is never mistaken for a stub.
    """
    if not options:
        return False
    for option in options:
        text = str(option.get("text") or "").strip()
        if not text:
            continue
        if not _is_own_label(text, option.get("label"), options):
            return False
    return True


def _is_own_label(text, label, options):
    """True for "۱", "۱)", "(۱)", "۱ (۱" when the value repeats its label.

    Gemini's stub copies the label into the value. A real classification
    answer ("۴" under label 1) is a DIFFERENT number, so it never matches.
    """
    numbers = {_label_of(text)}
    for inner in re.findall(r"%s" % _LABEL, text):
        numbers.add(_label_of(inner))
    numbers.discard(None)
    if not numbers:
        return False
    # The value must consist of nothing but label tokens and punctuation.
    scrubbed = re.sub(r"%s" % _LABEL, "", text)
    if _LETTERS.findall(scrubbed) or any(c in "0123456789" for c in scrubbed):
        return False
    index = _label_of(str(label))
    if index is None:
        return True
    # Only the option's OWN label, repeated once ("۱ (۱") or alone ("۱").
    return numbers == {index}


def _line_choices(line):
    """``[(label, text)]`` for one printed line, or [] if it is not choices."""
    text = str(line or "")
    parts = _INLINE_SPLIT.split(text)
    out = []
    for piece in parts:
        hit = _GLUED_LINE.match(piece)
        if hit and _label_of(hit.group("label")) is not None:
            out.append((hit.group("label"), hit.group("text").strip()))
        elif out:
            # A split that was not a real choice boundary: glue it back.
            out[-1] = (out[-1][0], (out[-1][1] + " " + piece).strip())
    if len(out) > 1:
        return out
    hit = _GLUED_LINE.match(text)
    if not hit or _label_of(hit.group("label")) is None:
        return []
    return [(hit.group("label"), hit.group("text").strip())]


def split_options(text):
    """Pull the choices out of a question's text.

    Returns ``(stem, options)``: the stem is what precedes the first printed
    choice, the options are ``[{"label","text"}]`` renumbered 1..N. Fewer than
    two readable choices means the text holds no choices (a stem's own
    lettered sub-statements are not choices), so the text comes back whole.
    """
    body = str(text or "")
    if not body.strip():
        return body, []
    lines = body.split("\n")
    first = None
    for index, line in enumerate(lines):
        found = _line_choices(line)
        # The first line normally heads each choice with its number and
        # carries no text of its own ("۱)" then the choice on the next line),
        # so any recognised label opens the choice block.
        if found:
            first = index
            break
    if first is None:
        return body, []
    stem = "\n".join(lines[:first]).strip()
    options = []
    for line in lines[first:]:
        for _label, value in _line_choices(line):
            options.append({"label": str(len(options) + 1), "text": value})
    if len(options) < 2:
        return body, []          # one numbered line is a sub-statement
    return stem, options


def clean_options(raw, question_text=""):
    """``(stem, options)`` — the one place an OCR reply's choices are read.

    Three shapes arrive, and the text decides between them:

    * the text carries its own choice lines -> those are the options, and any
      list beside them is Gemini's stub ("۱", "۲"…). This is the common case
      and it also fixes a stem that holds only sub-statements.
    * no choice lines, and the list is *just labels* ("۱ (۱") -> a stub with
      nothing to show; the question has no readable choices.
    * no choice lines, and the list holds values -> the list stands. A short
      answer list ("۳", "۲", "۱") is a real classification answer and must
      survive.
    """
    text = str(question_text or "")
    options = normalize_options(raw)
    stem, glued = split_options(text)
    if glued:
        return stem, glued
    if options and _labels_only(options):
        return text, []
    return text, options


def is_real_bbox(bbox):
    """A bbox is real only if it encloses something — [0,0,0,0] is Gemini's
    'no figure here' answer and must not attach the page image."""
    return (isinstance(bbox, list) and len(bbox) == 4
            and any(float(v) != 0.0 for v in bbox))


def normalize_bbox(raw):
    """Gemini may return [y,x,y,x] or the string "[y,x,y,x]".
    [0,0,0,0] means "no figure" -> None."""
    if isinstance(raw, list) and len(raw) == 4:
        return raw if is_real_bbox(raw) else None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) == 4:
                return parsed if is_real_bbox(parsed) else None
        except Exception:
            pass
    return None


def build_question_rows(item: SourceItem, gemini_result: dict, storage_url: str):
    questions = gemini_result.get("questions") if isinstance(gemini_result, dict) else None
    if not isinstance(questions, list):
        return []

    rows = []
    for index, q in enumerate(questions):
        if not isinstance(q, dict):
            continue
        number = q.get("question_number") or (index + 1)
        bbox = normalize_bbox(q.get("diagram_bbox"))
        options = normalize_options(q.get("options"))
        rows.append({
            "id": f"{item.source_id}-q{number}",
            "source_id": item.source_id,
            "question_number": parse_number(number),
            "question_text": q.get("question_text"),
            "options": json.dumps(options, ensure_ascii=False),
            # Metadata comes from the folder, never from Gemini.
            "subject": item.subject,
            "topic": item.topic,
            "grade": item.grade,
            # Extracted by Gemini from the image:
            "corp": q.get("corp"),
            "year": q.get("year"),
            "difficulty": q.get("difficulty"),
            "review_status": "pending",
            "raw_ocr_text": q.get("raw_ocr_text"),
            "diagram_bbox": json.dumps(bbox) if bbox else None,
            "diagram_url": storage_url if bbox else None,
            "tags": json.dumps([x for x in (item.subject, item.topic) if x],
                               ensure_ascii=False),
        })
    return rows
