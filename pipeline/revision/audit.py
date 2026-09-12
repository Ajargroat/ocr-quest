"""Stage 3: the narrow AI audit.

The AI only runs on question/answer PAIRS that deterministic code could not
settle by itself, and its power is deliberately tiny:

* it may NOT rewrite question_text / answer_explanation / options / difficulty;
* its one writable field is correct_option_label, and only for rows where the
  deterministic «بررسی سایر گزینه‌ها» extraction was inconclusive;
* everything else it produces is a flag (report row) for humans.
"""
import json
import re

import requests

from ..utils import to_english_digits
from .pairing import parse_options_field

PROMPT_HEADER = (
    "You are a strict QA auditor for a Persian Konkour question bank.\n"
    "Each input row is a question/answer PAIR extracted by OCR. The fields\n"
    "raw_ocr_text (and answer_raw_ocr_text) are the VERBATIM ground truth.\n\n"
    "HARD RULES:\n"
    "1. Never invent content. Never rewrite question_text, answer_explanation or options.\n"
    "2. Your ONLY writable field is set.correct_option_label, and ONLY on rows where\n"
    "   needs.label is true. Decide it from answer_explanation: the «بررسی سایر گزینه‌ها»\n"
    "   section reviews every WRONG option, so the option it never reviews is the correct\n"
    "   one. The value must exactly equal one of the labels in \"options\". Set null otherwise.\n"
    "3. You report problems via \"flags\" (field, issue, severity warn|error, optional\n"
    "   suggestion). Suggestions are INFORMATIONAL ONLY and are never applied automatically.\n"
    "   Flag exactly these three checks:\n"
    "   a) digits: every digit inside math spans ($...$, \\(...\\), \\[...\\], $$...$$) must be\n"
    "      English (0-9); digits in Persian prose must be Persian (۰-۹). Flag violations in\n"
    "      question_text, options or answer_explanation.\n"
    "   b) typos/grammar in question_text: flag with a short corrected \"suggestion\".\n"
    "   c) correct_option_label plausibility (only when needs.label): flag severity error when\n"
    "      the explanation contradicts the stored label and you cannot resolve it confidently.\n"
    "4. needs_review = true when any severity-error flag exists or you are unsure.\n"
    "5. Output ONLY strict JSON, no markdown.\n\n"
)

OUTPUT_FORMAT = (
    "\n\nOUTPUT FORMAT:\n"
    '{ "revisions":[{ "id": "<question id>", "set":{ "correct_option_label": null }, '
    '"flags":[{ "field": "...", "issue": "...", "severity": "warn", "suggestion": null }], '
    '"needs_review": false, "confidence": 0.9}]}'
)

SET_FIELDS = ("correct_option_label",)


def build_audit_prompt(pairs) -> str:
    """One slim row per question/answer pair. `pairs` items: {q, a, verify_label}."""
    slim = []
    for p in pairs:
        q = p.get("q") or {}
        a = p.get("a") or {}
        slim.append({
            "id": q.get("id"),
            "aid": a.get("id"),
            "question_number": q.get("question_number"),
            "question_text": q.get("question_text"),
            "options": parse_options_field(q.get("options")),
            "answer_explanation": a.get("answer_explanation"),
            "correct_option_label": a.get("correct_option_label")
                or q.get("correct_option_label"),
            "needs": {"digits": bool(p.get("needs_digits")),
                      "label": bool(p.get("verify_label"))},
            "raw_ocr_text": (q.get("raw_ocr_text") or "")[:3000],
            "answer_raw_ocr_text": (a.get("raw_ocr_text") or "")[:1500],
        })
    return PROMPT_HEADER + "INPUT PAIRS:\n" + json.dumps(slim, ensure_ascii=False) + OUTPUT_FORMAT


def call_router(cfg, prompt: str, retries: int = 2) -> str:
    """OpenAI-compatible chat completion against your local OCR-Quest router."""
    url = cfg.router_base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": cfg.router_model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": 8192,
    }
    headers = {"Content-Type": "application/json"}
    if cfg.router_api_key:
        headers["Authorization"] = "Bearer " + cfg.router_api_key

    last_error = ""
    for _attempt in range(1, retries + 1):
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=(10, 300))
            if resp.status_code == 200:
                body = resp.json()
                try:
                    return body["choices"][0]["message"]["content"] or ""
                except (KeyError, IndexError, TypeError):
                    last_error = "Unexpected router response shape."
            else:
                last_error = f"HTTP {resp.status_code}: {resp.text[:300]}"
        except requests.RequestException as exc:
            last_error = str(exc)
    raise RuntimeError(f"Router failed: {last_error}")


def parse_ai_json(text: str) -> dict:
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else {"revisions": []}
    except json.JSONDecodeError:
        s, e = t.find("{"), t.rfind("}")
        if s == -1 or e <= s:
            return {"revisions": []}
        try:
            data = json.loads(t[s:e + 1])
            return data if isinstance(data, dict) else {"revisions": []}
        except json.JSONDecodeError:
            return {"revisions": []}


def merge_audit(pairs, ai: dict, run_id: str):
    """Apply the AI's one writable field, decide statuses, build report dicts.

    Returns (q_updates, a_updates, reports); report dicts are unstamped (the
    runner assigns ids). Rows the AI was asked about but never answered are
    held as pending (no update returned) — the next run retries them.
    """
    by_id = {}
    revisions = ai.get("revisions") if isinstance(ai, dict) else None
    if isinstance(revisions, list):
        for rv in revisions:
            if isinstance(rv, dict) and rv.get("id"):
                by_id[str(rv["id"])] = rv

    q_updates, a_updates, reports = [], [], []
    for p in pairs:
        q, a = p.get("q"), p.get("a")
        if not isinstance(q, dict):
            continue
        ai_required = bool(p.get("_needs_ai"))
        rv = by_id.get(str(q.get("id")))
        if ai_required and rv is None:
            reports.append({"entity": "question", "entity_id": q.get("id"),
                            "field": None, "issue": "ai_no_verdict",
                            "severity": "warn", "source": "ai",
                            "before_value": None, "after_value": None})
            continue  # leave pending: the next run retries the AI call

        a_dict = rv if isinstance(rv, dict) else {}
        raw_set = a_dict.get("set")
        set_map = raw_set if isinstance(raw_set, dict) else {}
        raw_flags = a_dict.get("flags")
        flags = raw_flags if isinstance(raw_flags, list) else []

        q_upd = dict(q)
        a_upd = dict(a) if isinstance(a, dict) else None

        # The single writable field, validated against the real option labels.
        # Only rows whose deterministic derivation failed may have it written.
        lab = set_map.get("correct_option_label")
        if p.get("verify_label") and isinstance(lab, str):
            lab = to_english_digits(lab).strip()
            if lab:
                opts = parse_options_field(q.get("options"))
                hit = next((o for o in opts if o["label"] == lab), None)
                if hit is None:
                    reports.append({"entity": "question", "entity_id": q.get("id"),
                                    "field": "correct_option_label",
                                    "issue": f"ai suggested unknown label '{lab}'",
                                    "severity": "warn", "source": "ai",
                                    "before_value": None, "after_value": None})
                else:
                    cur = to_english_digits(str(q.get("correct_option_label") or "")).strip()
                    if lab != cur:
                        for upd, entity in ((q_upd, "question"), (a_upd, "answer")):
                            if not isinstance(upd, dict):
                                continue
                            before = str(upd.get("correct_option_label") or "")[:150]
                            upd["correct_option_label"] = lab
                            upd["correct_option_text"] = hit["text"]
                            reports.append({"entity": entity, "entity_id": upd.get("id"),
                                            "field": "correct_option_label",
                                            "issue": "ai corrected option label from explanation",
                                            "severity": "info", "source": "ai",
                                            "before_value": before, "after_value": lab})
                            reports.append({"entity": entity, "entity_id": upd.get("id"),
                                            "field": "correct_option_text",
                                            "issue": "filled from options by label",
                                            "severity": "info", "source": "ai",
                                            "before_value": None,
                                            "after_value": hit["text"][:150]})
                    if a_upd is not None:
                        a_upd["correct_option_label"] = a_upd.get("correct_option_label") or lab

        has_error = any(isinstance(fl, dict) and fl.get("severity") == "error"
                        for fl in flags)
        needs_human = has_error or (a_dict.get("needs_review") is True)
        status = "needs_human" if needs_human else "revised"
        notes = json.dumps({"ai_flags": flags,
                           "ai_confidence": a_dict.get("confidence")},
                          ensure_ascii=False)
        for upd in (q_upd, a_upd):
            if not isinstance(upd, dict):
                continue
            upd["revision_status"] = status
            upd["needs_review"] = bool(needs_human)
            upd["revision_notes"] = notes
            if upd is q_upd:
                q_updates.append(upd)
            else:
                a_updates.append(upd)

        for fl in flags:
            if not isinstance(fl, dict):
                continue
            sev = "error" if fl.get("severity") == "error" else "warn"
            base = {"field": fl.get("field"), "issue": fl.get("issue") or "unspecified",
                    "severity": sev, "source": "ai",
                    "before_value": None,
                    "after_value": (str(fl["suggestion"])[:280]
                                    if fl.get("suggestion") is not None else None)}
            reports.append({"entity": "question", "entity_id": q.get("id"), **base})
            if isinstance(a, dict) and base["field"] in ("answer_explanation",
                                                         "correct_option_label"):
                reports.append({"entity": "answer", "entity_id": a.get("id"), **base})

    return q_updates, a_updates, reports
