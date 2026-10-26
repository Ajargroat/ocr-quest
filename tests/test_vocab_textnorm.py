"""Offline tests for pipeline.vocab and pipeline.textnorm (salvaged from Studio).

The vectors are the real OCR strings seen in the live rows: a text layer with
no ZWNJ at all, institute spellings that differ only by ZWNJ/Arabic letters,
and `answers.year` in both digit systems.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import textnorm, vocab  # noqa: E402

ZWNJ = "\u200c"


class ZwnjTests(unittest.TestCase):
    def test_prefix_half_space_is_restored(self):
        bed = "می" + "شود"
        self.assertEqual(textnorm.restore_zwnj(bed), "می" + ZWNJ + "شود")
        self.assertTrue(textnorm.missing_zwnj(bed))

    def test_negated_prefix(self):
        self.assertEqual(textnorm.restore_zwnj("نمی" + "کند"),
                         "نمی" + ZWNJ + "کند")

    def test_plural_suffix(self):
        self.assertEqual(textnorm.restore_zwnj("کتاب" + "های"),
                         "کتاب" + ZWNJ + "های")

    def test_a_real_word_that_starts_with_mi_is_kept(self):
        for word in ("میانگین", "میدان", "میزان"):
            self.assertEqual(textnorm.restore_zwnj(word), word)

    def test_words_that_keep_their_tail_are_kept(self):
        for word in ("تنها", "رها", "نگاه"):
            self.assertEqual(textnorm.restore_zwnj(word), word)

    def test_already_correct_text_is_untouched(self):
        text = "می" + ZWNJ + "شود و زیست" + ZWNJ + "شناسی"
        self.assertEqual(textnorm.restore_zwnj(text), text)
        self.assertFalse(textnorm.missing_zwnj(text))

    def test_nothing_but_zwnj_is_ever_added(self):
        source = "می" + "شود در" + "یاخته" + "های گیاه"
        out = textnorm.restore_zwnj(source)
        self.assertEqual(len(out) - len(source), out.count(ZWNJ))
        self.assertEqual(out.replace(ZWNJ, ""), source)


class YearTests(unittest.TestCase):
    def test_persian_digits(self):
        self.assertEqual(textnorm.canonical_year("۱۴۰۲-۱۴۰۳"), "1402-1403")

    def test_latin_digits(self):
        self.assertEqual(textnorm.canonical_year("1403-1404"), "1403-1404")

    def test_underscore_separator(self):
        self.assertEqual(textnorm.canonical_year("1403_1404"), "1403-1404")

    def test_mixed_digit_systems_collapse_to_one_canonical(self):
        self.assertEqual(textnorm.canonical_year("۱۴۰۳-1404"), "1403-1404")

    def test_spaces_and_dashes(self):
        self.assertEqual(textnorm.canonical_year("۱۴۰۲ – ۱۴۰۳"), "1402-1403")

    def test_single_year_and_missing(self):
        self.assertEqual(textnorm.canonical_year("۱۴۰۴"), "1404")
        self.assertEqual(textnorm.canonical_year(None), "")
        self.assertEqual(textnorm.canonical_year(""), "")

    def test_latin_digits_leaves_the_rest_alone(self):
        self.assertEqual(textnorm.latin_digits("سال ۱۴۰۳ (a)"), "سال 1403 (a)")


class MathDelimiterTests(unittest.TestCase):
    def test_canonical_is_idempotent(self):
        for text in (r"$\frac{6}{3}$", r"$$\frac{6}{3}$$", r"\frac{6}{3}",
                     r"\(x^{2}\)", r"\[x^{2}\]", "متن " + r"\frac{1}{2}" + " است"):
            once = textnorm.canonical(text)
            self.assertEqual(textnorm.canonical(once), once, text)

    def test_bare_tex_gets_fenced(self):
        self.assertEqual(textnorm.canonical(r"\frac{6}{3}"), r"$\frac{6}{3}$")

    def test_tabular_becomes_array(self):
        out = textnorm.canonical(r"$\begin{tabular}{cc}a & b\end{tabular}$")
        self.assertIn("array", out)
        self.assertNotIn("tabular", out)


class VocabularyTests(unittest.TestCase):
    def setUp(self):
        self.tags = vocab.load()

    def test_the_seed_loads(self):
        self.assertTrue(self.tags["subjects"])
        self.assertTrue(self.tags["corps"])
        self.assertEqual(vocab.validate(self.tags), [])

    def test_spelling_variants_are_known(self):
        # the canonical tag is the ZWNJ spelling; the bare one is a variant
        tag = [e["tag"] for e in self.tags["corps"] if ZWNJ in e["tag"]][0]
        self.assertEqual(vocab.match_corp(tag.replace(ZWNJ, ""), self.tags), tag)
        self.assertIsNotNone(vocab.match_corp("قلم چی", self.tags))

    def test_arabic_letter_forms_fold(self):
        self.assertEqual(vocab.fold("كتاب"), vocab.fold("کتاب"))

    def test_unknown_institute_is_not_guessed(self):
        self.assertIsNone(vocab.match_corp("موسسه ناشناخته", self.tags))
        self.assertFalse(vocab.is_known_corp("", self.tags))

    def test_accept_alias_is_the_only_way_in(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "seed.json")
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(vocab.normalize(self.tags), fh, ensure_ascii=False)
            tag = self.tags["corps"][0]["tag"]
            self.assertIsNone(vocab.match_corp("امیرکبیر", vocab.load(path)))
            vocab.accept_alias("امیرکبیر", tag, path=path)
            self.assertEqual(vocab.match_corp("امیرکبیر", vocab.load(path)), tag)

    def test_accept_alias_refuses_an_unknown_tag(self):
        with self.assertRaises(ValueError):
            vocab.accept_alias("x", "موسسه ناشناخته",
                               path=vocab.SEED_PATH)

    def test_the_shipped_seed_is_not_modified_by_the_tests(self):
        before = open(vocab.SEED_PATH, encoding="utf-8").read()
        vocab.load()
        after = open(vocab.SEED_PATH, encoding="utf-8").read()
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
