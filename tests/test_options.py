"""Choices must survive the OCR reply's two failure modes.

Gemini answers one of three ways, and all three reached the staging store on
2026-10-25 (26 real chemistry questions):

* ``"options": []`` with the choices printed inside ``question_text`` — the
  reviewer saw the choices glued to the stem;
* four label-only stubs (``"text": "۱"`` or ``"۱ (۱"``) — nothing readable,
  and the real choices were *still* glued to the stem;
* a real list of choices — the normal, working case.

``clean_options`` is the single reader: it returns the stem without the
choices and the choices as ``[{label, text}]``. These tests pin every shape
observed in that batch plus the false positives that guard against cutting a
stem's own lettered sub-statements.

Run: python tests/test_options.py
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline.questions import (  # noqa: E402
    clean_options, normalize_options, split_options)


def opts(*texts):
    return [{"label": str(i + 1), "text": t} for i, t in enumerate(texts)]


def texts(options):
    return [o["text"] for o in options]


class GluedLineTests(unittest.TestCase):
    """The choices are printed in question_text, one per line."""

    def test_persian_labelled_choices_are_split_out(self):
        stem, options = split_options(
            "با توجه به شکل، کدام مورد درست است؟\n"
            "۱) واکنش گرماده است.\n"
            "۲) فشار کمتر است.\n"
            "۳) ثابت تعادل کاهش می‌یابد.\n"
            "۴) گرما مصرف می‌شود.")
        self.assertEqual(stem, "با توجه به شکل، کدام مورد درست است؟")
        self.assertEqual(len(options), 4)
        self.assertEqual(texts(options)[0], "واکنش گرماده است.")
        self.assertEqual([o["label"] for o in options], ["1", "2", "3", "4"])

    def test_latin_labelled_choices_are_split_out(self):
        stem, options = split_options("Which one?\n1) alpha\n2) beta\n3) gamma\n4) delta")
        self.assertEqual(stem, "Which one?")
        self.assertEqual(texts(options), ["alpha", "beta", "gamma", "delta"])

    def test_two_choices_on_one_line_are_both_read(self):
        stem, options = split_options(
            "کدام درست است؟ ۱) یک  ۲) دو  ۳) سه  ۴) چهار")
        self.assertEqual(len(options), 4, texts(options))
        self.assertEqual(texts(options), ["یک", "دو", "سه", "چهار"])

    def test_choices_on_one_line_with_brackets(self):
        stem, options = split_options("کدام؟ ۱) یک (۲ دو (۳ سه (۴ چهار")
        self.assertEqual(len(options), 4, texts(options))

    def test_a_value_holding_spaces_survives(self):
        _, options = split_options("کدام؟ ۱) واکنش رفت و برگشت ۲) تعادل")
        self.assertEqual(texts(options), ["واکنش رفت و برگشت", "تعادل"])


class StemIsNotCutTests(unittest.TestCase):
    """A stem's own numbered or lettered sub-statements are not choices."""

    def test_lettered_sub_statements_stay_in_the_stem(self):
        text = ("چه تعداد از عبارت‌های زیر درست هستند؟\n"
                "آ: اتم نیتروژن در پروتئین‌ها یافت می‌شود.\n"
                "ب: سرعت واکنش افزایش می‌یابد.\n"
                "پ: آمونیاک تشکیل می‌شود.\n"
                "ت: دما کاهش می‌یابد.")
        stem, options = split_options(text)
        self.assertEqual(options, [])
        self.assertEqual(stem, text)

    def test_a_single_numbered_line_is_a_sub_statement(self):
        text = "در این واکنش ۱) چه اتفاقی می‌افتد؟"
        self.assertEqual(split_options(text), (text, []))

    def test_dates_and_products_are_not_choices(self):
        text = "مقدار ۱۲/۵ در تاریخ ۱/۲/۱۴۰۲ و در ساعت ۳:۳۰ محاسبه شد."
        self.assertEqual(split_options(text), (text, []))

    def test_plain_text_without_choices(self):
        text = "سؤال بدون هیچ گزینه‌ای"
        self.assertEqual(split_options(text), (text, []))


class StubListTests(unittest.TestCase):
    """A label-only list is no answer at all — but a short answer list is."""

    def test_a_stub_list_gives_way_to_the_choices_in_the_text(self):
        raw = opts("۱", "۲", "۳", "۴")
        stem, options = clean_options(raw, "چه کدام؟\n۱) یک\n۲) دو")
        self.assertEqual(stem, "چه کدام؟")
        self.assertEqual(texts(options), ["یک", "دو"])

    def test_a_stub_list_with_no_choices_in_the_text_yields_nothing(self):
        stem, options = clean_options(opts("۱ (۱", "۲ (۲", "۳ (۳", "۴ (۴"),
                                      "چه تعداد درست است؟")
        self.assertEqual(options, [])
        self.assertEqual(stem, "چه تعداد درست است؟")

    def test_a_classification_answer_list_is_kept(self):
        raw = opts("صفر", "۱", "۲", "۳")
        _, options = clean_options(raw, "چند مورد درست است؟")
        self.assertEqual(texts(options), ["صفر", "۱", "۲", "۳"])

    def test_a_numeric_answer_list_is_kept(self):
        raw = opts("180", "120", "60", "80")
        _, options = clean_options(raw, "چند کیلوژول؟")
        self.assertEqual(texts(options), ["180", "120", "60", "80"])

    def test_a_reversed_answer_list_is_kept(self):
        raw = opts("۴", "۳", "۲", "۱")
        _, options = clean_options(raw, "کدام ترتیب؟")
        self.assertEqual(texts(options), ["۴", "۳", "۲", "۱"])


class RealListTests(unittest.TestCase):
    """A list that holds real choice text is trusted untouched."""

    def test_a_real_option_list_is_kept_whole(self):
        raw = opts("$۷۴ - ۱/۸۰۶$", "$۱۱۱ - ۱/۸۰۶$")
        _, options = clean_options(raw, "کدام است؟")
        self.assertEqual(len(options), 2)
        self.assertEqual(texts(options), ["$۷۴ - ۱/۸۰۶$", "$۱۱۱ - ۱/۸۰۶$"])

    def test_the_text_wins_when_it_carries_choices_the_list_lacks(self):
        # The list is a two-entry stub while the paper printed four choices.
        raw = opts("۱", "۲")
        stem, options = clean_options(
            raw, "کدام است؟\n۱) الف\n۲) ب\n۳) ج\n۴) د")
        self.assertEqual(stem, "کدام است؟")
        self.assertEqual(texts(options), ["الف", "ب", "ج", "د"])

    def test_empty_list_and_empty_text(self):
        self.assertEqual(clean_options([], ""), ("", []))

    def test_normalize_options_accepts_the_known_aliases(self):
        out = normalize_options([{"option_label": "1", "option_text": "یک"}])
        self.assertEqual(out, [{"label": "1", "text": "یک"}])


if __name__ == "__main__":
    unittest.main(verbosity=2)
