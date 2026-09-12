"""Question branch: prompt + row building.
Equivalent of 'Analyze Questions' → 'Prepare Question Rows'
→ 'Correct Function Node'."""
import json
import re

from .scanner import SourceItem
from .utils import parse_number

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

DIAGRAM HANDLING:
9. For ANY visible figure, diagram, or graph on the page, you MUST provide its "diagram_bbox" as [ymin, xmin, ymax, xmax] coordinates normalized to a 0-1000 square, tightly enclosing the figure.
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
      "diagram_bbox": [0, 0, 0, 0]
    }
  ]
}"""

# Same regex as the JS normalizeOptions()
_OPTION_RE = re.compile(r"^\s*([^)\.ـ:]{1,3})\s*[\)\.ـ:]\s*(.*)$")


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


def normalize_bbox(raw):
    """Gemini may return [y,x,y,x] or the string "[y,x,y,x]"."""
    if isinstance(raw, list) and len(raw) == 4:
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) == 4:
                return parsed
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
