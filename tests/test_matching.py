"""Offline tests for pipeline.matching — the answer-link ladder (P5).

The vectors are the live bank's real shapes: `year` written in both digit
systems and with an underscore, institute spellings that differ only by
ZWNJ, and question numbers printed in Persian digits. A wrong link is worse
than no link, so every ambiguous case must land on ``needs_manual``.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import matching, vocab  # noqa: E402


class YearCorpCanonTests(unittest.TestCase):
    def test_years_from_both_digit_systems_compare_equal(self):
        self.assertEqual(matching.canon({"year": "۱۴۰۲-۱۴۰۳"})["year"],
                         matching.canon({"year": "1402-1403"})["year"])

    def test_a_separator_typo_does_not_block_a_match(self):
        self.assertEqual(matching.canon({"year": "1403_1404"})["year"], "1403-1404")

    def test_an_institute_variant_folds_to_the_same_key(self):
        tags = vocab.load()
        right = [e["tag"] for e in tags["corps"] if "\u200c" in e["tag"]][0]
        left = matching.canon({"corp": right}, tags)["corp"]
        right_key = matching.canon({"corp": right.replace("\u200c", "")}, tags)["corp"]
        self.assertEqual(left, right_key)
        self.assertTrue(left)


def question(qid, number, corp="", year="", **extra):
    row = {"id": qid, "question_number": number, "corp": corp, "year": year,
           "question_text": "text " + qid, "origin": "staged"}
    row.update(extra)
    return row


class LadderTests(unittest.TestCase):
    def test_exactly_one_full_key_match_is_auto_ok(self):
        out = matching.decide({"question_number": 4, "corp": "قلمچی", "year": "1403-1404"},
                              [question("q4", 4, "قلم‌چی", "۱۴۰۳-۱۴۰۴"),
                               question("q5", 5, "قلم‌چی", "۱۴۰۳-۱۴۰۴")])
        self.assertEqual(out["match_state"], "auto_ok")
        self.assertEqual(out["link_id"], "q4")
        self.assertEqual(out["match_method"], "number+corp+year")

    def test_a_single_different_candidate_contradicts(self):
        out = matching.decide({"question_number": 4, "corp": "گاج", "year": "1403-1404"},
                              [question("q4", 4, "قلمچی", "1403-1404")])
        self.assertEqual(out["match_state"], "needs_manual")
        self.assertIsNone(out["link_id"])

    def test_two_full_matches_need_a_human(self):
        out = matching.decide({"question_number": 4, "corp": "قلمچی", "year": "1403-1404"},
                              [question("a", 4, "قلمچی", "1403-1404"),
                               question("b", 4, "قلمچی", "1403-1404")])
        self.assertEqual(out["match_state"], "needs_manual")

    def test_a_unique_number_without_contradiction_is_unlinked_ok(self):
        out = matching.decide({"question_number": 4}, [question("q4", 4, "گاج", "1403")])
        self.assertEqual(out["match_state"], "unlinked_ok")
        self.assertEqual(out["link_id"], "q4")

    def test_a_unique_number_takes_the_non_contradicting_candidate(self):
        # the answer carries an institute but no year: number-only evidence
        out = matching.decide({"question_number": 7, "corp": "گاج"},
                              [question("q7", 7, "گاج", "1403"),
                               question("other", 8, "گاج", "1403")])
        self.assertEqual(out["match_state"], "unlinked_ok")
        self.assertEqual(out["link_id"], "q7")

    def test_no_candidate_needs_a_human(self):
        out = matching.decide({"question_number": 99}, [question("q1", 1)])
        self.assertEqual(out["match_state"], "needs_manual")
        self.assertIsNone(out["link_id"])

    def test_a_persian_digit_number_still_matches(self):
        out = matching.decide({"question_number": "۴"}, [question("q4", 4)])
        self.assertEqual(out["link_id"], "q4")

    def test_candidates_are_returned_for_the_human(self):
        out = matching.decide({"question_number": 4},
                              [question("a", 4), question("b", 4)])
        self.assertEqual([c["id"] for c in out["candidates"]], ["a", "b"])
        self.assertTrue(all(len(c["text"]) <= 160 for c in out["candidates"]))

    def test_a_missing_candidate_id_is_dropped(self):
        out = matching.decide({"question_number": 4}, [{"question_number": 4}])
        self.assertIsNone(out["link_id"])

    def test_the_ladder_never_picks_the_first_of_several(self):
        pool = [question("q4", 4, "گاج", "1403"), question("q4b", 4, "گاج", "1403")]
        for _ in range(5):
            self.assertEqual(matching.decide({"question_number": 4, "corp": "گاج",
                                              "year": "1403"}, pool)["match_state"],
                             "needs_manual")


class PoolNarrowingTests(unittest.TestCase):
    def test_a_wrong_subject_candidate_is_excluded(self):
        pool = [question("q1", 1, subject="شیمی"), question("q2", 1, subject="زیست")]
        out = matching.link_candidates({"question_number": 1}, pool, subject="زیست")
        self.assertEqual(out["link_id"], "q2")

    def test_a_grade_mismatch_is_excluded(self):
        pool = [question("q1", 1, grade=10), question("q2", 1, grade=12)]
        out = matching.link_candidates({"question_number": 1}, pool, grade="12")
        self.assertEqual(out["link_id"], "q2")

    def test_a_staged_and_a_db_copy_count_as_one_side_only_when_identical(self):
        # the same question staged and live must not turn into "two matches"
        pool = [question("q1", 1, "گاج", "1403", origin="staged"),
                question("q1-live", 1, "گاج", "1403", origin="db")]
        out = matching.link_candidates({"question_number": 1, "corp": "گاج",
                                        "year": "1403"}, pool)
        self.assertEqual(out["match_state"], "needs_manual")


if __name__ == "__main__":
    unittest.main()
