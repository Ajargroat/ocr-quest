"""Pure-logic tests for the OCR pipeline (no network, no database).

Run them either way:
    python tests/test_pipeline_logic.py
    pytest tests/test_pipeline_logic.py
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.answers import (build_answer_prompt, finalize_answer_rows,      # noqa: E402
                              prepare_answer_rows)
from pipeline.gemini import extract_json                                     # noqa: E402
from pipeline.questions import build_question_rows, normalize_bbox, normalize_options
from pipeline.revision.audit import build_audit_prompt, merge_audit, parse_ai_json   # noqa: E402
from pipeline.revision.claims import analyze as analyze_claims                        # noqa: E402
from pipeline.revision.completeness import REQUIRED_COLUMNS, find_nulls              # noqa: E402
from pipeline.revision.pairing import check_conflicts, identity_key, pair_batch, norm_scalar  # noqa: E402
from pipeline.revision.polish import (apply_digit_policy, delimiters_balanced,          # noqa: E402
                                      fix_coeffs, polish_answer_row, polish_row,
                                      sanitize_math, wrap_bare)
from pipeline.scanner import SourceItem                                      # noqa: E402
from pipeline.utils import norm_text, parse_number, sha1_hex, to_english_digits


def source_item(**overrides):
    base = dict(source_id="abc", rel_path="riazi/amar/question/q.jpg", subject="riazi",
                grade=12, topic="amar", type="سوال", file_name="q.jpg",
                file_path="C:/tmp/q.jpg", file_size=1024, mime_type="image/jpeg")
    base.update(overrides)
    return SourceItem(**base)


class TestUtils(unittest.TestCase):
    def test_persian_and_arabic_digits(self):
        self.assertEqual(to_english_digits("۱۴۰۳"), "1403")
        self.assertEqual(to_english_digits("٤٢"), "42")
        self.assertEqual(parse_number("۱۲"), 12)
        self.assertEqual(parse_number("۱۲.۵"), 12.5)
        self.assertIsNone(parse_number("نامعلوم"))

    def test_norm_text_collapses_whitespace(self):
        self.assertEqual(norm_text("  a\n\t b  "), "a b")
        self.assertEqual(norm_text(None), "")


class TestGeminiJsonExtraction(unittest.TestCase):
    def test_clean_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json('```json\n{"a": 1}\n```'), {"a": 1})

    def test_json_buried_in_prose(self):
        self.assertEqual(extract_json('Here you go: {"a": {"b": 2}} thanks'),
                         {"a": {"b": 2}})

    def test_no_json_raises(self):
        with self.assertRaises(ValueError):
            extract_json("sorry, no data")


class TestQuestionRows(unittest.TestCase):
    def test_rows_are_built_from_model_output_plus_folder_metadata(self):
        parsed = {"questions": [
            {"question_number": "۲", "question_text": "متن سؤال",
             "options": ["۱) گزینه اول", "متن بدون برچسب"],
             "corp": "ماز", "year": "۱۴۰۳", "difficulty": "متوسط",
             "raw_ocr_text": "خام", "diagram_bbox": "[0, 10, 20, 30]"},
            {"question_number": None, "question_text": "دوم", "options": None,
             "diagram_bbox": [1, 2, 3, 4]},
        ]}
        rows = build_question_rows(source_item(), parsed, "http://u/abc")

        self.assertEqual(len(rows), 2)
        first, second = rows
        self.assertEqual(first["subject"], "riazi")          # folder, not model
        self.assertEqual(first["grade"], 12)
        self.assertEqual(first["topic"], "amar")
        self.assertEqual(first["question_number"], 2)        # model, digits normalised
        self.assertEqual(first["corp"], "ماز")
        self.assertEqual(first["review_status"], "pending")
        self.assertEqual(first["tags"], '["riazi", "amar"]')
        self.assertEqual(json.loads(first["options"]),
                         [{"label": "۱", "text": "گزینه اول"},
                          {"label": "2", "text": "متن بدون برچسب"}])
        self.assertEqual(first["diagram_bbox"], "[0, 10, 20, 30]")
        self.assertEqual(first["diagram_url"], "http://u/abc")

        # Missing numbers fall back to the 1-based index, and absent options
        # become an empty list rather than NULL.
        self.assertEqual(second["question_number"], 2)
        self.assertEqual(second["options"], "[]")
        self.assertEqual(second["diagram_bbox"], "[1, 2, 3, 4]")

    def test_id_uses_the_unparsed_question_number(self):
        """Pinned behaviour: the row id keeps whatever the model returned, so a
        Persian digit can leak into the primary key. Flagged for review."""
        parsed = {"questions": [{"question_number": "۲", "question_text": "ت"}]}
        self.assertEqual(build_question_rows(source_item(), parsed, "u")[0]["id"], "abc-q۲")

    def test_unusable_model_output_yields_no_rows(self):
        self.assertEqual(build_question_rows(source_item(), {}, "u"), [])
        self.assertEqual(build_question_rows(source_item(), {"questions": "nope"}, "u"), [])
        self.assertEqual(build_question_rows(source_item(), {"questions": ["x"]}, "u"), [])

    def test_normalize_options_handles_every_shape(self):
        self.assertEqual(normalize_options("not a list"), [])
        self.assertEqual(normalize_options([{"label": "الف", "text": "متن"}]),
                         [{"label": "الف", "text": "متن"}])
        self.assertEqual(normalize_options([{"key": 3, "value": "v"}]),
                         [{"label": "3", "text": "v"}])
        self.assertEqual(normalize_options(["5"]), [{"label": "1", "text": "5"}])

    def test_normalize_bbox_accepts_lists_and_json_strings(self):
        self.assertEqual(normalize_bbox([0, 1, 2, 3]), [0, 1, 2, 3])
        self.assertEqual(normalize_bbox("[0, 1, 2, 3]"), [0, 1, 2, 3])
        self.assertIsNone(normalize_bbox("[0, 1, 2]"))
        self.assertIsNone(normalize_bbox("garbage"))
        self.assertIsNone(normalize_bbox(None))


class TestAnswerLinking(unittest.TestCase):
    def setUp(self):
        self.options = json.dumps(
            [{"label": "1", "text": "گزینه یک طولانی"},
             {"label": "2", "text": "گزینه دو طولانی"}], ensure_ascii=False)
        self.candidates = [{"id": "abc-q1", "question_number": 1,
                            "question_text": "سؤال یک", "options": self.options,
                            "corp": "ماز", "year": "1403"}]

    def test_prompt_injects_candidates(self):
        prompt = build_answer_prompt(self.candidates)
        self.assertNotIn("__CANDIDATES__", prompt)
        self.assertIn('"question_number": 1', prompt)
        self.assertIn("گزینه یک طولانی", prompt)

    def test_linking_rules(self):
        parsed = {"answers": [
            {"question_number": "۱", "answer_explanation": "توضیح کامل",
             "correct_option_label": "۲",
             "assets": [{"asset_type": "جدول", "bbox": [0, 0, 10, 10], "caption": "جدول ۱"},
                        {"asset_type": "نمودار", "bbox": "bad"}]},
            {"question_number": "۹", "answer_explanation": "بی ربط"},
            {"question_number": "۱", "answer_explanation": "پاسخ: گزینه یک طولانی درست است"},
        ]}
        prepared = prepare_answer_rows(parsed)
        self.assertEqual([p["answer_number"] for p in prepared], [1, 2, 3])
        # A malformed bbox degrades to None instead of dropping the asset.
        self.assertIsNone(prepared[0]["assets"][1]["bbox"])
        self.assertEqual(prepared[0]["assets"][0]["bbox"], [0, 0, 10, 10])

        rows = finalize_answer_rows(source_item(type="پاسخ"), prepared, self.candidates)
        self.assertEqual(len(rows), 3)

        linked, orphan, inferred = rows
        self.assertEqual(linked["question_id"], "abc-q1")
        self.assertEqual(linked["review_status"], "pending")
        self.assertEqual(linked["id"], "abc-a1")
        self.assertEqual(linked["correct_option_label"], "۲")   # raw until polish
        self.assertEqual(json.loads(linked["assets"])[0]["asset_type"], "جدول")

        self.assertIsNone(orphan["question_id"])
        self.assertEqual(orphan["review_status"], "unlinked")
        self.assertEqual(orphan["id"], "abc-a9")
        self.assertEqual(orphan["assets"], "[]")

        # No label from the model: exactly one long option text appears in the
        # explanation, so it is used to recover the label.
        self.assertEqual(inferred["correct_option_label"], "1")
        self.assertEqual(inferred["correct_option_text"], "گزینه یک طولانی")
        self.assertEqual(inferred["review_status"], "pending")

    def test_content_hash_is_normalised_explanation(self):
        prepared = prepare_answer_rows({"answers": [
            {"question_number": 1, "answer_explanation": "توضیح  کامل"}]})
        row = finalize_answer_rows(source_item(), prepared, [])[0]
        self.assertIsNone(row["question_id"])
        self.assertEqual(row["content_hash"], sha1_hex(norm_text("توضیح کامل")))


class TestMathSanitizing(unittest.TestCase):
    def test_bare_commands_get_wrapped(self):
        self.assertEqual(wrap_bare("\\frac{1}{2}"), "$\\frac{1}{2}$")

    def test_multi_argument_commands_stay_whole(self):
        """Regression: only the first argument was consumed, so the rest leaked
        out of the math span as literal braces ($\\frac{1}${2})."""
        self.assertEqual(wrap_bare("\\sqrt[3]{x}"), "$\\sqrt[3]{x}$")
        self.assertEqual(wrap_bare("x = \\frac{a+b}{c-d} است"), "x = $\\frac{a+b}{c-d}$ است")
        self.assertEqual(wrap_bare("\\binom{n}{k}"), "$\\binom{n}{k}$")
        self.assertEqual(wrap_bare("\\overline{AB} = \\frac{1}{2}"),
                         "$\\overline{AB}$ = $\\frac{1}{2}$")

    def test_prose_after_a_command_is_not_swallowed(self):
        self.assertEqual(wrap_bare("\\sqrt{2} text"), "$\\sqrt{2}$ text")
        self.assertEqual(wrap_bare("\\alpha alone"), "\\alpha alone")

    def test_coefficient_underscore_is_removed(self):
        self.assertEqual(fix_coeffs("a_1 + _5x"), "a_1 + 5x")

    def test_existing_math_spans_are_left_alone(self):
        self.assertEqual(sanitize_math("$a_1 + _5x$"), "$a_1 + 5x$")
        self.assertEqual(sanitize_math("$$\\sum_{i=1}^n$$"), "$$\\sum_{i=1}^n$$")

    def test_empty_input_passes_through(self):
        self.assertIsNone(sanitize_math(None))
        self.assertEqual(sanitize_math(""), "")


class TestPolishRow(unittest.TestCase):
    def test_deterministic_repairs(self):
        row = {
            "id": "abc-q2", "question_number": "۲", "grade": "۱۲",
            "year": "۱۴۰۳-۱۴۰۴",
            "question_text": "حل: \\frac{1}{2} و ۵",
            "answer_explanation": "$a_1 + _5x$",
            "correct_option_text": None,
            "options": json.dumps(["۱) الف", "۲) ب"], ensure_ascii=False),
            "correct_option_label": "۲",
            "subject": "riazi", "topic": "amar", "difficulty": "متوسط", "corp": "ماز",
            "tags": "[]",
            "diagram_bbox": "[0, 10, 20, 30]", "diagram_url": None,
            "source_storage_url": "http://u/abc",
        }
        polished = polish_row(row, "42")

        self.assertEqual(polished["run_id"], "42")
        self.assertEqual(polished["question_number"], 2)
        self.assertEqual(polished["grade"], 12)
        self.assertEqual(polished["year"], "1403-1404")
        self.assertIn("$\\frac{1}{2}$", polished["question_text"])
        # Digit policy: English inside math, Persian in prose (۵ stays).
        self.assertIn("و ۵", polished["question_text"])
        self.assertEqual(polished["answer_explanation"], "$a_1 + 5x$")
        self.assertEqual(json.loads(polished["options"]),
                         [{"label": "1", "text": "الف"}, {"label": "2", "text": "ب"}])
        # Label <-> text are cross-filled from the option list.
        self.assertEqual(polished["correct_option_label"], "2")
        self.assertEqual(polished["correct_option_text"], "ب")
        self.assertEqual(json.loads(polished["tags"]), ["riazi", "amar", "متوسط", "ماز"])
        self.assertTrue(polished["has_diagram"])
        self.assertEqual(polished["diagram_url"], "http://u/abc")
        self.assertEqual(polished["diagram_bbox"], "[0, 10, 20, 30]")

        fields = {r["field"] for r in polished["_repairs"]}
        self.assertTrue({"year", "question_text", "options", "correct_option_text",
                         "tags", "diagram_url"} <= fields)
        self.assertTrue(all(r["severity"] == "info" for r in polished["_repairs"]))

    def test_input_row_is_not_mutated(self):
        row = {"id": "x", "question_text": "۵", "options": "[]"}
        polish_row(row, "42")
        self.assertEqual(row["question_text"], "۵")
        self.assertNotIn("run_id", row)

    def test_missing_diagram_stays_missing(self):
        polished = polish_row({"id": "x", "diagram_bbox": "not json", "options": "[]"}, "42")
        self.assertFalse(polished["has_diagram"])
        self.assertIsNone(polished["diagram_bbox"])

    def test_text_only_options_and_whitespace(self):
        polished = polish_row({"id": "x", "options": '["الف", "", "ب"]'}, "42")
        self.assertEqual(json.loads(polished["options"]),
                         [{"label": "1", "text": "الف"}, {"label": "3", "text": "ب"}])


class TestRevisionAudit(unittest.TestCase):
    """v2 contract: AI audits PAIRS and may only write a validated label."""

    def _pair(self, **over):
        q = {"id": "r1", "question_number": 1, "question_text": "سوال؟",
             "options": json.dumps([{"label": "1", "text": "الف"},
                                    {"label": "2", "text": "ب"}], ensure_ascii=False),
             "correct_option_label": "1", "correct_option_text": "الف",
             "raw_ocr_text": "ز" * 4000}
        a = {"id": "a1", "answer_explanation": "توضیح",
             "correct_option_label": "1", "correct_option_text": "الف",
             "raw_ocr_text": "ی" * 3000}
        p = {"q": q, "a": a, "verify_label": True, "needs_digits": False,
             "_needs_ai": True}
        p.update(over)
        return p

    def test_audit_prompt_carries_pairs_and_caps_ocr_text(self):
        prompt = build_audit_prompt([self._pair()])
        self.assertIn("QA auditor", prompt)
        self.assertIn('"needs"', prompt)
        self.assertIn("ز" * 3000, prompt)
        self.assertNotIn("ز" * 3001, prompt)
        self.assertIn("ی" * 1500, prompt)
        self.assertNotIn("ی" * 1501, prompt)

    def test_parse_ai_json_is_forgiving(self):
        self.assertEqual(parse_ai_json("```json\n{\"revisions\": []}\n```"),
                         {"revisions": []})
        self.assertEqual(parse_ai_json("noise {\"revisions\": []} noise"),
                         {"revisions": []})
        self.assertEqual(parse_ai_json("not json at all"), {"revisions": []})
        self.assertEqual(parse_ai_json("[1, 2]"), {"revisions": []})

    def test_merge_applies_only_the_validated_label(self):
        p = self._pair()
        ai = {"revisions": [{
            "id": "r1",
            "set": {"correct_option_label": "2", "question_text": "hack"},
            "flags": [{"field": "question_text", "issue": "typo",
                       "severity": "warn", "suggestion": "صواب"}],
            "needs_review": False, "confidence": 0.8}]}
        q_upd, a_upd, reports = merge_audit([p], ai, "42")
        self.assertEqual(q_upd[0]["correct_option_label"], "2")
        self.assertEqual(q_upd[0]["correct_option_text"], "ب")
        self.assertEqual(q_upd[0]["question_text"], "سوال؟")    # never rewritten
        self.assertEqual(a_upd[0]["correct_option_label"], "2")
        self.assertEqual(q_upd[0]["revision_status"], "revised")
        self.assertFalse(q_upd[0]["needs_review"])
        label_reps = [r for r in reports if r["field"] == "correct_option_label"
                      and r["source"] == "ai"]
        self.assertEqual({r["entity"] for r in label_reps}, {"question", "answer"})
        typo = [r for r in reports if r["issue"] == "typo"]
        self.assertEqual(typo[0]["entity"], "question")
        self.assertEqual(typo[0]["after_value"], "صواب")

    def test_merge_rejects_unknown_label_and_flags_errors(self):
        p = self._pair()
        ai = {"revisions": [{
            "id": "r1", "set": {"correct_option_label": "7"},
            "flags": [{"field": "answer_explanation", "issue": "unrelated",
                       "severity": "error"}],
            "needs_review": True, "confidence": 0.3}]}
        q_upd, a_upd, reports = merge_audit([p], ai, "42")
        self.assertEqual(q_upd[0]["correct_option_label"], "1")   # '7' rejected
        self.assertEqual(q_upd[0]["revision_status"], "needs_human")
        self.assertTrue(a_upd[0]["needs_review"])
        self.assertTrue(any("unknown label" in (r["issue"] or "") for r in reports))
        self.assertEqual({r["entity"] for r in reports if r["issue"] == "unrelated"},
                         {"question", "answer"})

    def test_merge_skips_rows_without_verdict(self):
        p = self._pair()
        q_upd, a_upd, reports = merge_audit([p], {"revisions": []}, "42")
        self.assertEqual((q_upd, a_upd), ([], []))
        self.assertEqual(reports[0]["issue"], "ai_no_verdict")

    def test_merge_ignores_label_write_when_not_requested(self):
        p = self._pair(verify_label=False)
        ai = {"revisions": [{"id": "r1", "set": {"correct_option_label": "2"},
                             "flags": [], "needs_review": False, "confidence": 0.9}]}
        q_upd, a_upd, reports = merge_audit([p], ai, "42")
        self.assertEqual(q_upd[0]["correct_option_label"], "1")
        self.assertEqual(q_upd[0]["revision_status"], "revised")


class TestDigitPolicy(unittest.TestCase):
    def test_math_english_prose_persian(self):
        s = apply_digit_policy("احتمال $\\frac{3}{4}$ برابر ۵ و 7 است")
        self.assertIn("$\\frac{3}{4}$", s)
        self.assertIn("برابر ۵ و ۷ است", s)

    def test_delimiters_balanced(self):
        self.assertTrue(delimiters_balanced("a $x$ \\[y\\] b"))
        self.assertFalse(delimiters_balanced("a $x b"))
        self.assertFalse(delimiters_balanced("a \\[y b"))


class TestCompleteness(unittest.TestCase):
    def test_question_contract_reports_nulls(self):
        row = {c: "x" for c in REQUIRED_COLUMNS["question"]}
        row["options"] = "[]"
        row["corp"] = None
        self.assertEqual(find_nulls(row, "question"), ["options", "corp"])

    def test_answer_contract_matches_spec(self):
        self.assertEqual(list(REQUIRED_COLUMNS["answer"]),
                         ["id", "source_id", "question_number",
                          "answer_explanation", "correct_option_label", "corp",
                          "year", "difficulty", "subject", "topic", "grade",
                          "raw_ocr_text"])


class TestPairing(unittest.TestCase):
    def _q(self, **over):
        q = {"id": "q1", "question_number": 5, "subject": "shimi",
             "topic": "avarez", "grade": 12, "corp": "ماز", "year": "1403",
             "difficulty": "متوسط",
             "options": json.dumps([{"label": "1", "text": "الف"},
                                    {"label": "2", "text": "ب"}],
                                   ensure_ascii=False),
             "correct_option_label": None, "correct_option_text": None}
        q.update(over)
        return q

    def _a(self, **over):
        a = {"id": "a1", "question_id": None, "question_number": 5,
             "subject": "shimi", "topic": "avarez", "grade": "۱۲",
             "corp": "ماز", "year": "1403", "difficulty": None,
             "answer_explanation": "ت", "correct_option_label": "2",
             "correct_option_text": None}
        a.update(over)
        return a

    def test_identity_link_and_fills(self):
        res = pair_batch([self._q()], [self._a()])
        self.assertEqual(len(res["pairs"]), 1)
        p = res["pairs"][0]
        q, a = p["q"], p["a"]
        self.assertEqual(a["question_id"], "q1")            # the missing link
        self.assertEqual(a["difficulty"], "متوسط")          # filled from question
        self.assertEqual(q["correct_option_label"], "2")    # mirrored from answer
        self.assertEqual(a["correct_option_text"], "ب")     # from options by label
        self.assertEqual(p["conflicts"], [])
        self.assertEqual({r[2]["field"] for r in p["reports"]},
                         {"question_id", "difficulty", "correct_option_label",
                          "correct_option_text"})

    def test_conflict_is_reported_not_resolved(self):
        res = pair_batch([self._q()], [self._a(year="1402")])
        p = res["pairs"][0]
        self.assertEqual([c["field"] for c in p["conflicts"]], ["year"])
        self.assertEqual(p["a"]["year"], "1402")            # untouched
        self.assertEqual(p["q"]["year"], "1403")

    def test_grade_forms_compare_numerically(self):
        self.assertEqual(identity_key(self._q(grade="۱۲")),
                         identity_key(self._a(grade=12.0)))
        self.assertEqual(norm_scalar("۱۲.۰"), "12")

    def test_ambiguous_and_orphans(self):
        res = pair_batch([self._q()], [self._a(), self._a(id="a2")])
        self.assertEqual(res["pairs"], [])
        reasons = {(u["entity"], u["row"]["id"], u["reason"]) for u in res["unmatched"]}
        self.assertIn(("answer", "a1", "qa_ambiguous_pair"), reasons)
        self.assertIn(("question", "q1", "qa_ambiguous_pair"), reasons)
        res2 = pair_batch([self._q()], [])
        self.assertEqual(res2["unmatched"][0]["reason"], "qa_unmatched_answer")
        res3 = pair_batch([], [self._a()])
        self.assertEqual(res3["unmatched"][0]["reason"], "qa_unmatched_question")

    def test_existing_link_wins_and_rechecks_identity(self):
        res = pair_batch([self._q()],
                         [self._a(question_id="q1", topic="cheshni")])
        p = res["pairs"][0]
        self.assertIn("topic", [c["field"] for c in p["conflicts"]])

    def test_check_conflicts_ignores_nulls(self):
        self.assertEqual(check_conflicts({"corp": "ماز"}, {"corp": None}), [])


class TestClaims(unittest.TestCase):
    OPTS = [{"label": "1", "text": "الف"}, {"label": "2", "text": "ب"},
            {"label": "3", "text": "ج"}, {"label": "4", "text": "د"}]
    EX = ("حل: ۲>۱. بررسی سایر گزینه‌ها: گزینه ۱ نادرست است؛ گزینه ۲ نیز "
          "غلط است؛ گزینه ۴ هم نیست.")

    def test_review_section_fills_missing_label(self):
        r = analyze_claims({"answer_explanation": self.EX}, self.OPTS)
        self.assertEqual((r["status"], r["derived"]), ("fill", "3"))

    def test_review_section_confirms_and_detects_mismatch(self):
        ok = analyze_claims({"answer_explanation": self.EX,
                             "correct_option_label": "3"}, self.OPTS)
        self.assertEqual(ok["status"], "ok")
        bad = analyze_claims({"answer_explanation": self.EX,
                              "correct_option_label": "1"}, self.OPTS)
        self.assertEqual(bad["status"], "mismatch")

    def test_explicit_correct_assertion_without_section(self):
        r = analyze_claims({"answer_explanation": "پاسخ گزینه ۳ صحیح است.",
                            "correct_option_label": "3"}, self.OPTS)
        self.assertEqual((r["status"], r["derived"]), ("ok", "3"))
        w = analyze_claims({"answer_explanation": "گزینه ۲ نادرست است.",
                            "correct_option_label": "2"}, self.OPTS)
        self.assertEqual(w["status"], "inconclusive")

    def test_no_useful_structure_is_inconclusive(self):
        r = analyze_claims({"answer_explanation": "توضیح کامل و بی‌عیب.",
                            "correct_option_label": "2"}, self.OPTS)
        self.assertEqual(r["status"], "inconclusive")
        r2 = analyze_claims({"answer_explanation": "", "correct_option_label": "2"},
                            self.OPTS)
        self.assertEqual(r2["status"], "inconclusive")

    def test_naderest_does_not_count_as_sahih(self):
        # «نادرست» must never be read as a correctness marker.
        r = analyze_claims({"answer_explanation":
                            "بررسی سایر گزینه‌ها: گزینه ۱ نادرست، گزینه ۲ نادرست، "
                            "گزینه ۴ نادرست.", "correct_option_label": "3"}, self.OPTS)
        self.assertEqual(r["status"], "ok")


class TestPolishAnswerRow(unittest.TestCase):
    def test_answer_fields_polished(self):
        a = {"id": "a1", "question_number": "۵", "grade": "۱۲",
             "year": "۱۴۰۳",
             "answer_explanation": "نرخ 5 برابر $x_1$ و 7",
             "correct_option_label": "۲", "tags": "[]",
             "subject": "shimi", "topic": "t", "difficulty": "hard", "corp": "ماز"}
        polished = polish_answer_row(a, "7")
        self.assertEqual(polished["question_number"], 5)
        self.assertEqual(polished["grade"], 12)
        self.assertEqual(polished["year"], "1403")
        self.assertEqual(polished["correct_option_label"], "2")
        self.assertIn("نرخ ۵", polished["answer_explanation"])
        self.assertIn("$x_1$", polished["answer_explanation"])
        self.assertIn("و ۷", polished["answer_explanation"])
        self.assertEqual(json.loads(polished["tags"]),
                         ["shimi", "t", "hard", "ماز"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
