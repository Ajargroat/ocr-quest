"""One scan-chunk of the revision pipeline: stages 1 → 2 → 3.

Runs entirely on a batch of question rows plus candidate answer rows that the
runner already fetched. Nothing here talks to the network except the narrow AI
audit; if that call fails, its sub-chunk is dropped unchanged (rows stay
'pending' and the next run retries them).
"""
import json
from datetime import datetime, timezone

from .audit import build_audit_prompt, call_router, merge_audit, parse_ai_json
from .claims import analyze as analyze_claims
from .completeness import is_missing, null_reports
from .pairing import parse_options_field, pair_batch
from .polish import delimiters_balanced, polish_answer_row, polish_row


def stamp_reports(reports, run_id):
    """Assign globally-unique ids: run_id:entity:entity_id:r<n> (per-row counter)."""
    counters = {}
    stamped = []
    for rep in reports:
        key = (str(rep.get("entity")), str(rep.get("entity_id")))
        idx = counters.get(key, 0)
        counters[key] = idx + 1
        stamped.append({**rep, "run_id": run_id,
                        "id": f"{run_id}:{key[0]}:{key[1]}:r{idx}"})
    return stamped


def _repair_report(rep):
    return {"field": rep.get("field"), "issue": rep.get("issue"),
            "severity": rep.get("severity", "info"),
            "source": rep.get("source", "code"),
            "before_value": rep.get("before_value"),
            "after_value": rep.get("after_value")}


def _row_needs_ai(q, a, verify_label):
    """What the AI is still required for (3a verification, 3b typos, 3c label)."""
    reasons = []
    if q.get("question_text"):
        reasons.append("typos")
    for val in (q.get("question_text"), q.get("options"),
                (a or {}).get("answer_explanation")):
        if val and not delimiters_balanced(val):
            reasons.append("digits")
            break
    if verify_label:
        reasons.append("label")
    return reasons


def _finalize(row, reports_for_row, conflicts=False, forced=False):
    """Deterministic status: any error report, conflict or forced flag ->
    needs_human, otherwise the row is 'revised'."""
    has_error = any(r.get("severity") == "error" for r in reports_for_row)
    flagged = has_error or conflicts or forced
    row["revision_status"] = "needs_human" if flagged else "revised"
    row["needs_review"] = bool(flagged)
    row["revision_notes"] = json.dumps({"deterministic": True}, ensure_ascii=False)
    row["revised_at"] = datetime.now(timezone.utc).isoformat()


def _fill_from_options(target, entity, label, opts):
    """Set label on target and derive its option text; returns report dicts."""
    reports = [{"entity": entity, "entity_id": target.get("id"),
                "field": "correct_option_label",
                "issue": "filled from explanation review section",
                "severity": "info", "source": "code",
                "before_value": None, "after_value": label}]
    if is_missing(target.get("correct_option_text")):
        hit = next((o for o in opts if o["label"] == label), None)
        if hit:
            target["correct_option_text"] = hit["text"]
            reports.append({"entity": entity, "entity_id": target.get("id"),
                            "field": "correct_option_text",
                            "issue": "filled from options by label",
                            "severity": "info", "source": "code",
                            "before_value": None, "after_value": hit["text"][:150]})
    target["correct_option_label"] = label
    return reports


def process_batch(db, cfg, log, run_id, q_rows, a_rows, status_questions, summary,
                  on_stage=None, should_stop=None):
    """Process one chunk. Only rows that receive an update are written; every
    other row (AI failure, missing verdict) keeps its previous status.

    `on_stage(name)` reports the live stage ('completeness' | 'pairing' | 'ai')
    for the UI rail; `should_stop()` is checked between AI sub-chunks so a
    manual stop leaves their rows pending instead of dropping them."""
    def stage(name):
        if on_stage:
            on_stage(name)

    for k in ("revised", "needs_human", "links", "backfills", "null_reports",
              "conflicts", "ai_flags", "errors"):
        summary.setdefault(k, 0)
    rmap = {}  # (entity, str(row_id)) -> [deterministic report dicts]

    def R(entity, row, rep):
        rmap.setdefault((entity, str(row.get("id"))), []).append(
            {"entity": entity, "entity_id": row.get("id"), **rep})

    # ── Stage 1: NULL reporting rides on the deterministic passes ──
    stage("completeness")
    # ── Stage 2a: deterministic polish ─────────────────────────
    stage("pairing")
    q_polished = [polish_row(r, run_id) for r in q_rows]
    a_polished = [polish_answer_row(r, run_id) for r in a_rows]
    for q in q_polished:
        for rep in q.pop("_repairs", []):
            R("question", q, _repair_report(rep))
    for a in a_polished:
        for rep in a.pop("_repairs", []):
            R("answer", a, _repair_report(rep))

    # ── Stage 2b: pairing + definite backfills ─────────────────────
    result = pair_batch(q_polished, a_polished)
    pairs = list(result["pairs"])
    for p in pairs:
        q, a = p["q"], p["a"]
        p["verify_label"] = False

        for c in p["conflicts"]:
            for ent, row in (("question", q), ("answer", a)):
                R(ent, row, {"field": c["field"], "issue": "qa_field_conflict",
                             "severity": "warn", "source": "code",
                             "before_value": str(c["question_value"])[:150],
                             "after_value": str(c["answer_value"])[:150]})

        for ent, eid, rep in p["reports"]:
            rmap.setdefault((ent, str(eid)), []).append({"entity": ent,
                                                         "entity_id": eid,
                                                         **_repair_report(rep)})

        # claims: definitive correct-option derivation from the explanation
        opts = parse_options_field(q.get("options"))
        cl = analyze_claims(a, opts)
        if cl["status"] == "fill":
            for rep in _fill_from_options(a, "answer", cl["derived"], opts):
                rmap.setdefault((rep["entity"], str(rep["entity_id"])), []).append(rep)
            if is_missing(q.get("correct_option_label")):
                for rep in _fill_from_options(q, "question", cl["derived"], opts):
                    rmap.setdefault((rep["entity"], str(rep["entity_id"])), []).append(rep)
        elif cl["status"] == "mismatch":
            p["verify_label"] = True
            for ent, row in (("question", q), ("answer", a)):
                R(ent, row, {"field": "correct_option_label",
                             "issue": "qa_label_mismatch: explanation reviews the "
                                      "stored option",
                             "severity": "warn", "source": "code",
                             "before_value": None,
                             "after_value": str(cl["derived"])[:150]})
        elif (cl["status"] == "inconclusive" and a.get("answer_explanation")
              and not is_missing(a.get("correct_option_label"))):
            p["verify_label"] = True

        # ── Stage 1 (reporting half): NULL columns AFTER definite fills ──
        for rep in null_reports(q, "question"):
            R("question", q, _repair_report(rep))
        for rep in null_reports(a, "answer"):
            R("answer", a, _repair_report(rep))

    for um in result["unmatched"]:
        row, ent = um["row"], um["entity"]
        R(ent, row, {"field": "question_id" if ent == "answer" else None,
                     "issue": um["reason"], "severity": "warn", "source": "code",
                     "before_value": None, "after_value": None})
        for rep in null_reports(row, "question" if ent == "question" else "answer"):
            R(ent, row, _repair_report(rep))

    written_q, written_a = [], []
    final_reports = []
    emitted = set()

    def emit(key):
        if key not in emitted:
            emitted.add(key)
            final_reports.extend(rmap.get(key, []))

    # ── Split: rows the code settled vs rows that still need the AI ─
    ai_pairs = []
    for p in pairs:
        reasons = _row_needs_ai(p["q"], p["a"], p["verify_label"])
        p["needs_digits"] = "digits" in reasons
        p["_needs_ai"] = bool(reasons)
        if p["_needs_ai"]:
            ai_pairs.append(p)
            continue
        if status_questions:
            _finalize(p["q"], rmap.get(("question", str(p["q"]["id"])), []),
                      conflicts=bool(p["conflicts"]))
            written_q.append(p["q"])
            emit(("question", str(p["q"]["id"])))
        _finalize(p["a"], rmap.get(("answer", str(p["a"]["id"])), []),
                  conflicts=bool(p["conflicts"]))
        written_a.append(p["a"])
        emit(("answer", str(p["a"]["id"])))

    # Unmatched questions: AI (typos) if we own their status, else done.
    for um in result["unmatched"]:
        if um["entity"] != "question":
            continue
        if not status_questions:
            continue
        row = um["row"]
        reasons = _row_needs_ai(row, None, False)
        if reasons:
            ai_pairs.append({"q": row, "a": None, "verify_label": False,
                             "needs_digits": "digits" in reasons,
                             "_needs_ai": True})
            continue
        _finalize(row, rmap.get(("question", str(row.get("id"))), []))
        written_q.append(row)
        emit(("question", str(row.get("id"))))

    # Orphan/ambiguous answers: report + human queue (a human decides why no
    # question matched). Their pairing reports are kept, nulls included.
    for um in result["unmatched"]:
        if um["entity"] != "answer":
            continue
        row = um["row"]
        _finalize(row, rmap.get(("answer", str(row.get("id"))), []), forced=True)
        written_a.append(row)
        emit(("answer", str(row.get("id"))))

    # ── Stage 3: narrow AI audit, sub-chunked for prompt size ──────
    stage("ai")
    sub_size = max(1, cfg.revision_chunk_size)
    for start_i in range(0, len(ai_pairs), sub_size):
        if should_stop and should_stop():
            left = len(ai_pairs) - start_i
            summary["stopped"] = True
            log("warn", f"Stop requested — {left} pair(s) skip the AI audit "
                        "and stay pending for the next scan.")
            break
        sub = ai_pairs[start_i:start_i + sub_size]
        log("info", f"AI audit sub-chunk {start_i // sub_size + 1} "
                    f"({len(sub)} pair(s))…")
        try:
            ai_text = call_router(cfg, build_audit_prompt(sub))
            ai = parse_ai_json(ai_text)
        except Exception as exc:
            summary["errors"] += len(sub)
            log("error", f"AI audit failed: {exc} — these rows stay pending.")
            continue
        q_upd, a_upd, ai_reports = merge_audit(sub, ai, run_id)
        now_iso = datetime.now(timezone.utc).isoformat()

        def demote(rows, entity):
            # Deterministic errors (e.g. NULL required columns) outrank the
            # AI verdict: never hand back a 'revised' row that is incomplete.
            for u in rows:
                rs = rmap.get((entity, str(u.get("id"))), [])
                if any(r.get("severity") == "error" for r in rs):
                    u["revision_status"] = "needs_human"
                    u["needs_review"] = True

        demote(q_upd, "question")
        demote(a_upd, "answer")
        for u in q_upd:
            u["revised_at"] = now_iso
            written_q.append(u)
            emit(("question", str(u.get("id"))))
        for u in a_upd:
            u["revised_at"] = now_iso
            written_a.append(u)
            emit(("answer", str(u.get("id"))))
        # AI reports only land on rows whose verdict actually arrived.
        for rep in ai_reports:
            if (rep["entity"], str(rep["entity_id"])) in emitted:
                final_reports.append(rep)

    # ── Writes ─────────────────────────────────────────────────────
    try:
        if written_q:
            db.apply_question_updates(written_q)
        if written_a:
            db.apply_answer_updates(written_a)
        stamped = stamp_reports(final_reports, run_id)
        if stamped:
            db.insert_report_rows(stamped)
    except Exception as exc:
        summary["errors"] += len(written_q) + len(written_a)
        log("error", f"Database write failed: {exc}")
        return

    for rep in stamped:
        if rep["issue"] == "null_required_column":
            summary["null_reports"] += 1
        elif rep["source"] == "ai":
            summary["ai_flags"] += 1
        elif rep["field"] == "question_id" and rep["issue"] == "linked to matched question":
            summary["links"] += 1
        elif rep["source"] == "code" and rep["severity"] == "info" \
                and rep["field"] != "question_id":
            summary["backfills"] += 1
        elif rep["issue"] in ("qa_field_conflict",
                              ("qa_label_mismatch: explanation reviews the stored option")):
            summary["conflicts"] += 1
    for rows in (written_q, written_a):
        for u in rows:
            if u.get("revision_status") == "needs_human":
                summary["needs_human"] += 1
            else:
                summary["revised"] += 1
