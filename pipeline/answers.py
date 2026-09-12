"""Answer branch: prompt building ('Build Answer Prompt'),
'Prepare Answer Rows' and 'Finalize Answer Rows'."""
import json

from .scanner import SourceItem
from .utils import norm_text, parse_number, sha1_hex

# __CANDIDATES__ is replaced at runtime (avoids brace-escaping issues).
ANSWER_TEMPLATE = """You are a precise OCR and answer-extraction system for Iranian Konkour and textbook answer keys.
You will receive one file (image) containing answer explanations for one or more questions.

Your task:
1. Extract every answer block. A block starts at its printed question number (e.g. "۴") and continues until the next question number or end of page.
2. Preserve Persian text exactly, including half-spaces (ZWNJ) and punctuation.
3. For each block output "question_number" (integer, Western digits), "answer_explanation" (full explanation in reading order, do not summarize), and "raw_ocr_text" (verbatim OCR of the block).
4. ASSETS: for EVERY visible table, figure, chemical structure or graph inside the block, output one entry in "assets" in reading order, with:
   - "asset_type": "table" | "diagram" | "chemical_structure" | "graph"
   - "bbox": [ymin, xmin, ymax, xmax] normalized to 0-1000, tightly enclosing the asset
   - "caption": the exact sentence introducing/referring to the asset (usually containing «جدول»، «شکل»، «نمودار»، «ساختار»); null if none
   - "order": 1-based reading-order index inside the block
5. CORRECT OPTION: CANDIDATE QUESTIONS from the database are given below. Using ONLY the explanation content, identify which candidate option the explanation supports. If certain, set "correct_option_label" exactly as in the candidates. If the page explicitly states the correct option, use that. DO NOT put this field empty or null.
6. Metadata line near the bottom (e.g. "(ماز ۱۴۰۳-۱۴۰۴ - متوسط)") maps to "corp", "year", "difficulty".
7. If no answers exist, return {"answers": []}.

CANDIDATE QUESTIONS (JSON):
__CANDIDATES__

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "answers": [
    {
      "question_number": 4,
      "answer_explanation": "string",
      "correct_option_label": "string",
      "correct_option_text": "string or null",
      "corp": "string or null",
      "year": "string or null",
      "difficulty": "string or null",
      "raw_ocr_text": "string",
      "assets": [
        { "asset_type": "table", "order": 1, "bbox": [0,0,0,0], "caption": "string or null" }
      ]
    }
  ]
}"""


def build_answer_prompt(candidates) -> str:
    slim = []
    for q in candidates:
        options = q.get("options")
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except Exception:
                options = []
        slim.append({
            "question_number": q.get("question_number"),
            "question_text": q.get("question_text"),
            "options": options,
            "corp": q.get("corp"),
            "year": q.get("year"),
        })
    return ANSWER_TEMPLATE.replace(
        "__CANDIDATES__", json.dumps(slim, ensure_ascii=False))


def prepare_answer_rows(gemini_result: dict):
    """Same sanitisation as the 'Prepare Answer Rows' node."""
    answers = gemini_result.get("answers") if isinstance(gemini_result, dict) else None
    if not isinstance(answers, list):
        return []

    prepared = []
    for index, a in enumerate(answers):
        if not isinstance(a, dict):
            continue
        assets = []
        raw_assets = a.get("assets")
        if isinstance(raw_assets, list):
            for j, s in enumerate(raw_assets):
                if not isinstance(s, dict):
                    continue
                bbox = s.get("bbox")
                assets.append({
                    "asset_type": s.get("asset_type") or "diagram",
                    "order": s.get("order") or (j + 1),
                    "bbox": bbox if (isinstance(bbox, list) and len(bbox) == 4) else None,
                    "caption": s.get("caption"),
                })
        prepared.append({
            "answer_number": index + 1,
            "question_number": a.get("question_number"),
            "answer_explanation": a.get("answer_explanation"),
            "correct_option_label": a.get("correct_option_label"),
            "correct_option_text": a.get("correct_option_text"),
            "corp": a.get("corp"),
            "year": a.get("year"),
            "difficulty": a.get("difficulty"),
            "raw_ocr_text": a.get("raw_ocr_text"),
            "assets": assets,
        })
    return prepared


def finalize_answer_rows(item: SourceItem, answers, candidates):
    """Same linking rules as 'Finalize Answer Rows':
    match by question number (with corp/year tolerance), then a unique
    option-text substring match inside the explanation as fallback."""

    def find_question(number, corp, year):
        target = parse_number(number)
        if target is None:
            return None
        for q in candidates:  # strict pass
            if parse_number(q.get("question_number")) != target:
                continue
            if corp and q.get("corp") and q["corp"] != corp:
                continue
            if year and q.get("year") and q["year"] != year:
                continue
            return q
        for q in candidates:  # tolerant pass
            if parse_number(q.get("question_number")) == target:
                return q
        return None

    rows = []
    for index, a in enumerate(answers):
        q = find_question(a.get("question_number"), a.get("corp"), a.get("year"))

        label = a.get("correct_option_label") or None
        text = a.get("correct_option_text") or None

        # Fallback: exactly one option text appears inside the explanation.
        if not label and q and q.get("options"):
            options = q["options"]
            if isinstance(options, str):
                try:
                    options = json.loads(options)
                except Exception:
                    options = []
            explanation = norm_text(a.get("answer_explanation"))
            hits = [
                o for o in options
                if isinstance(o, dict) and o.get("text")
                and len(norm_text(o["text"])) > 10
                and norm_text(o["text"]) in explanation
            ]
            if len(hits) == 1:
                label, text = hits[0].get("label"), hits[0].get("text")

        number = q.get("question_number") if q else parse_number(a.get("question_number"))
        suffix = number if number is not None else (index + 1)

        rows.append({
            "id": f"{item.source_id}-a{suffix}",
            "source_id": item.source_id,
            "question_id": q.get("id") if q else None,
            "question_number": parse_number(number) if number is not None else None,
            "answer_explanation": a.get("answer_explanation"),
            "correct_option_label": label,
            "correct_option_text": text,
            "assets": json.dumps(a.get("assets") or [], ensure_ascii=False),
            "subject": item.subject,
            "topic": item.topic,
            "grade": item.grade,
            "corp": a.get("corp"),
            "year": a.get("year"),
            "difficulty": a.get("difficulty"),
            "review_status": "pending" if q else "unlinked",
            "raw_ocr_text": a.get("raw_ocr_text"),
            "tags": "[]",
            "content_hash": sha1_hex(norm_text(a.get("answer_explanation"))),
        })
    return rows
