"""Orchestrates a revision run: a chunked scan (default 50 rows per chunk).

Each chunk flows through the three stages in order —
  1. completeness (NULL required-column reports)
  2. deterministic polish + QA pairing + definite backfills
  3. a narrow AI audit ONLY for rows deterministic code could not settle
— then every checked row is marked 'revised' (or 'needs_human') and the scan
advances to the next chunk. Chunks whose AI call failed stay 'pending' and are
retried by the next run.

All of this run's log lines carry the `rev` flag, so the Hub files them in a
separate revision timeline instead of the pipeline's Live log. A run can be
interrupted with request_stop(): the scan finishes the current chunk, skips
the remaining AI sub-chunks (their rows stay pending) and imports what it
already computed.
"""
import threading
import time

from .batch import process_batch
from .pairing import norm_scalar

_state = {"running": False, "last_run": None, "last_summary": None, "progress": None,
          "stop_requested": False}
_lock = threading.Lock()
_stop = threading.Event()


def _set_progress(**kw):
    """Live scan progress for the UI stepper + progress bar (None = idle)."""
    with _lock:
        _state["progress"] = kw if kw else None


def get_state():
    with _lock:
        return dict(_state)


def request_stop() -> bool:
    """Ask the running scan to halt after its current chunk."""
    with _lock:
        if not _state["running"]:
            return False
    _stop.set()
    with _lock:
        _state["stop_requested"] = True
    return True


def start(hub, cfg, db) -> bool:
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["stop_requested"] = False
    _stop.clear()
    threading.Thread(target=_run, args=(hub, cfg, db), daemon=True).start()
    return True


def _chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


def _run(hub, cfg, db):
    def log(level, message):
        hub.emit({"type": "log", "level": level, "message": message, "rev": True})

    # Live context for the UI rail; prog() merges into the progress snapshot
    # so a stage flip never clobbers chunk/phase counters (and vice versa).
    ctx = {"phase": "questions", "part": 1, "parts": 2, "chunk": 0,
           "chunks_total": 0, "stage": "completeness", "summary": {}}

    def prog(**kw):
        ctx.update(kw)
        _set_progress(**ctx)

    def on_stage(stage):
        prog(stage=stage)

    run_id = str(int(time.time()))
    summary = {"questions_scanned": 0, "answers_scanned": 0, "revised": 0,
               "needs_human": 0, "links": 0, "backfills": 0, "null_reports": 0,
               "conflicts": 0, "ai_flags": 0, "errors": 0, "stopped": False}
    try:
        q_ids = db.fetch_pending_question_ids()
        summary["questions_scanned"] = len(q_ids)
        chunks_q = -(-len(q_ids) // max(1, cfg.revision_scan_chunk))
        log("info", f"{len(q_ids)} pending question(s); scan chunk = "
                    f"{cfg.revision_scan_chunk}.")
        prog(phase="questions", part=1, parts=2, chunk=0,
             chunks_total=chunks_q, stage="completeness", summary=dict(summary))
        touched_answers = set()
        stopped = False
        for ci, chunk_ids in enumerate(_chunks(q_ids, cfg.revision_scan_chunk)):
            if _stop.is_set():
                left = len(q_ids) - ci * cfg.revision_scan_chunk
                log("warn", f"Stop requested — the remaining {left} question(s) "
                            "stay pending for the next scan.")
                stopped = True
                break
            q_rows = db.fetch_questions_by_ids(chunk_ids)
            numbers = [n for n in (norm_scalar(r.get("question_number"))
                                   for r in q_rows) if n]
            a_rows = db.fetch_answers_for_linking(numbers, chunk_ids)
            touched_answers.update(a["id"] for a in a_rows)
            log("info", f"Scanning chunk {ci + 1} ({len(q_rows)} questions, "
                        f"{len(a_rows)} candidate answers)…")
            process_batch(db, cfg, log, run_id, q_rows, a_rows,
                          status_questions=True, summary=summary,
                          on_stage=on_stage, should_stop=_stop.is_set)
            log("success", f"Chunk {ci + 1} done.")
            prog(phase="questions", part=1, parts=2, chunk=ci + 1,
                 chunks_total=chunks_q, summary=dict(summary))

        if not stopped:
            a_ids = [i for i in db.fetch_pending_answer_ids()
                     if i not in touched_answers]
            summary["answers_scanned"] = len(a_ids)
            if a_ids:
                chunks_a = -(-len(a_ids) // max(1, cfg.revision_scan_chunk))
                log("info", f"{len(a_ids)} answer(s) without a pending question — "
                            "final sweep.")
                for ci, chunk_ids in enumerate(_chunks(a_ids, cfg.revision_scan_chunk)):
                    if _stop.is_set():
                        left = len(a_ids) - ci * cfg.revision_scan_chunk
                        log("warn", f"Stop requested — the remaining {left} "
                                    "answer(s) stay pending for the next scan.")
                        stopped = True
                        break
                    a_rows = db.fetch_answers_by_ids(chunk_ids)
                    qid_set = sorted({a.get("question_id") for a in a_rows
                                      if a.get("question_id")})
                    numbers = [n for n in (norm_scalar(a.get("question_number"))
                                           for a in a_rows) if n]
                    ctxq = db.fetch_questions_by_ids(qid_set)
                    ctxq += db.fetch_questions_matching_numbers(
                        numbers, exclude_ids={q["id"] for q in ctxq})
                    log("info", f"Sweeping chunk {ci + 1} ({len(a_rows)} answers)…")
                    process_batch(db, cfg, log, run_id, ctxq, a_rows,
                                  status_questions=False, summary=summary,
                                  on_stage=on_stage, should_stop=_stop.is_set)
                    prog(phase="answers", part=2, parts=2, chunk=ci + 1,
                         chunks_total=chunks_a, summary=dict(summary))
        if stopped:
            summary["stopped"] = True

        if stopped:
            log("warn", f"Revision run stopped on request: {summary['revised']} revised, "
                        f"{summary['needs_human']} need human review, "
                        f"{summary['links']} QA links created so far.")
        else:
            log("success",
                f"Revision run finished: {summary['revised']} revised, "
                f"{summary['needs_human']} need human review, "
                f"{summary['links']} QA links created.")
    except Exception as exc:
        log("error", f"Revision run aborted: {exc}")
    finally:
        with _lock:
            _state["running"] = False
            _state["stop_requested"] = False
            _state["last_run"] = run_id
            _state["last_summary"] = summary
            _state["progress"] = None
