"""Read-only dry run of the answer matcher over the LIVE unlinked answers.

The 1 008 answers that carry no question today are the migration's real risk
surface, so they are shown, not guessed: this tool reads the live bank, runs
`pipeline.matching` over the candidate pool, and prints how many would link
automatically and how many need a human. It writes nothing - no UPDATE, no
INSERT, no bucket object - and it can be pointed at a JSON fixture instead of
the database, which is how the tests run it.

    .venv/Scripts/python tools/matching_report.py                # live, read-only
    .venv/Scripts/python tools/matching_report.py --json out.json
    .venv/Scripts/python tools/matching_report.py --answers a.json --questions q.json
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import matching  # noqa: E402

LINKED = """
SELECT id, question_number, question_id, subject, topic, grade, corp, year,
       answer_explanation
FROM public.answers
WHERE question_id IS NULL
ORDER BY source_id, question_number
"""

POOL = """
SELECT q.legacy_id AS id, q.question_number, q.subject, q.topic, q.grade,
       q.corp, q.year, q.prompt AS question_text
FROM public.questions q
WHERE q.subject = %s
"""


def load_live():
    """(answers, questions) from the live bank. Read-only by construction."""
    from pipeline.config import load_config
    from pipeline.db import Database
    db = Database(load_config())
    answers = [
        {"id": r[0], "question_number": r[1], "question_id": r[2],
         "subject": r[3], "topic": r[4], "grade": r[5], "corp": r[6],
         "year": r[7], "answer_explanation": r[8]}
        for r in (db._execute(LINKED, ("",), fetch=True) or [])
    ]
    subjects = {a["subject"] for a in answers if a["subject"]}
    questions = []
    for subject in sorted(subjects):
        for r in (db._execute(POOL, (subject,), fetch=True) or []):
            questions.append({"id": r[0], "question_number": r[1],
                              "subject": r[2], "topic": r[3], "grade": r[4],
                              "corp": r[5], "year": r[6],
                              "question_text": r[7]})
    return answers, questions


def load_files(answer_path, question_path):
    with open(answer_path, encoding="utf-8") as fh:
        answers = json.load(fh)
    with open(question_path, encoding="utf-8") as fh:
        questions = json.load(fh)
    return answers, questions


def report(answers, questions, limit=25):
    """One row per unlinked answer: its verdict and the pool it saw."""
    rows = []
    for answer in answers:
        pool = [q for q in questions
                if not answer.get("subject") or q.get("subject") == answer.get("subject")]
        verdict = matching.decide(answer, pool)
        rows.append({
            "id": answer.get("id"),
            "question_number": answer.get("question_number"),
            "subject": answer.get("subject"),
            "corp": answer.get("corp"),
            "year": answer.get("year"),
            "state": verdict["match_state"],
            "pick": verdict.get("link_id"),
            "reason": verdict.get("match_method"),
            "pool": len(pool),
        })
    counts = {}
    for row in rows:
        counts[row["state"]] = counts.get(row["state"], 0) + 1
    return {"total": len(rows), "counts": counts, "rows": rows,
            "sample": [r for r in rows if r["state"] != "auto_ok"][:limit]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--answers", help="JSON list of unlinked answers (skip the DB)")
    ap.add_argument("--questions", help="JSON list of candidate questions")
    ap.add_argument("--json", help="write the full report to this path")
    ap.add_argument("--limit", type=int, default=25,
                    help="how many non-auto rows to print")
    args = ap.parse_args(argv)

    if args.answers and args.questions:
        answers, questions = load_files(args.answers, args.questions)
    else:
        answers, questions = load_live()

    out = report(answers, questions, limit=args.limit)
    print("unlinked answers: %d" % out["total"])
    print("candidate questions: %d" % len(questions))
    for state in ("auto_ok", "unlinked_ok", "manual_ok", "needs_manual"):
        print("  %-12s %d" % (state, out["counts"].get(state, 0)))
    print("")
    print("%-14s %3s  %-9s %-8s %-6s %s" % ("answer", "no", "state", "corp", "pool", "reason"))
    for row in out["sample"]:
        print("%-14s %3s  %-9s %-8s %-6s %s" % (
            str(row["id"])[:14], row["question_number"], row["state"],
            str(row["corp"] or "-")[:8], row["pool"], (row["reason"] or "")[:44]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(out, fh, ensure_ascii=False, indent=2)
        print("\nfull report: %s" % args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
