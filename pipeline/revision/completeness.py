"""Stage 1: definitive completeness audit.

Every required column that is NULL (or empty-equivalent) on a question or an
answer is reported with severity 'error' so the row lands in the human queue.
"""

REQUIRED_COLUMNS = {
    "question": (
        "id", "source_id", "question_number", "question_text", "options",
        "subject", "topic", "difficulty", "raw_ocr_text", "grade", "corp", "year",
    ),
    "answer": (
        "id", "source_id", "question_number", "answer_explanation",
        "correct_option_label", "corp", "year", "difficulty", "subject",
        "topic", "grade", "raw_ocr_text",
    ),
}


def is_missing(value) -> bool:
    """A column counts as NULL when it is None, blank, or an empty JSON list/dict."""
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        return stripped in ("", "[]", "{}", "null")
    if isinstance(value, (list, dict, tuple)):
        return len(value) == 0
    return False


def find_nulls(row: dict, entity: str):
    """Names of required columns still missing on this row."""
    fields = REQUIRED_COLUMNS.get(entity, ())
    return [f for f in fields if is_missing(row.get(f))]


def null_reports(row: dict, entity: str):
    """Report dicts (unstamped) for every missing required column."""
    return [{"entity": entity, "entity_id": row.get("id"), "field": f,
             "issue": "null_required_column", "severity": "error",
             "source": "code", "before_value": None, "after_value": None}
            for f in find_nulls(row, entity)]
