"""Offline tests for pipeline.mathnorm (salvaged from Studio's row builder).

Vectors are the real OCR notations from the chemistry pages: bare element
subscripts, Persian digits/decimal slashes inside maths, and key pages whose
option labels are printed as letters on either side of the value.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import mathnorm  # noqa: E402


class BraceSubscriptTests(unittest.TestCase):
    def test_bare_subscripts_inside_mathrm_are_braced(self):
        self.assertEqual(mathnorm.brace_subscripts(r"\mathrm{C_nH_{2n+2}}"),
                         r"\mathrm{C_{n}H_{2n+2}}")
        self.assertEqual(mathnorm.brace_subscripts(r"\mathrm{C_6H_{10}}"),
                         r"\mathrm{C_{6}H_{10}}")

    def test_braced_subscripts_and_prose_are_untouched(self):
        self.assertEqual(mathnorm.brace_subscripts(r"\mathrm{kJ.mol^{-1}}"),
                         r"\mathrm{kJ.mol^{-1}}")
        self.assertEqual(mathnorm.brace_subscripts("بدون ریاضی"), "بدون ریاضی")
        self.assertIsNone(mathnorm.brace_subscripts(None))


class NormaliseMathTests(unittest.TestCase):
    def test_chemical_formulas_become_upright(self):
        self.assertEqual(mathnorm.normalise_math(r"$C_2H_6$"),
                         r"$\mathrm{C}_{2}\mathrm{H}_{6}$")
        self.assertEqual(
            mathnorm.normalise_math(r"$2N_2(g) + 5O_2(g)$"),
            r"$2\mathrm{N}_{2}\mathrm{(g)} + 5\mathrm{O}_{2}\mathrm{(g)}$")

    def test_persian_digits_and_decimal_slash(self):
        self.assertEqual(mathnorm.normalise_math(r"$۲/۵۶\text{ g.L}^{-1}$"),
                         r"$2.56\text{ g.L}^{-1}$")
        self.assertEqual(mathnorm.normalise_math(r"$۱۳/۵\text{ kJ}$"),
                         r"$13.5\text{ kJ}$")
        self.assertEqual(mathnorm.normalise_math(r"$\Delta H = ۱۱۴\text{kJ}$"),
                         r"$\Delta H = 114\text{kJ}$")

    def test_fractions_keep_their_slash(self):
        self.assertEqual(mathnorm.normalise_math(r"$1/2$"), r"$1/2$")

    def test_bonds_are_upright(self):
        self.assertEqual(mathnorm.normalise_math(r"$C-C$"), r"$\mathrm{C-C}$")
        self.assertEqual(mathnorm.normalise_math(r"$O=O$"), r"$\mathrm{O=O}$")

    def test_upright_formulas_and_bare_variables_are_left_alone(self):
        for untouched in (r"$\mathrm{NaHCO_{3}(s)}$", r"$\mathrm{H_{2}O}$",
                          r"$\mathrm{C_{6}H_{10}}$", r"$\Delta H = ?$",
                          r"$X$", "بدون ریاضی"):
            self.assertEqual(mathnorm.normalise_math(untouched), untouched)

    def test_it_is_idempotent(self):
        for text in (r"$C_2H_6$", r"$۲/۵۶\text{ g.L}^{-1}$",
                     r"$\mathrm{C_{6}H_{10}}$"):
            once = mathnorm.normalise_math(text)
            self.assertEqual(mathnorm.normalise_math(once), once)


class OptionTextTests(unittest.TestCase):
    OPTIONS = [{"label": "1", "text": "A"}, {"label": "2", "text": "B"},
               {"label": "3", "text": "C"}, {"label": "4", "text": "D"}]

    def test_label_match_wins(self):
        self.assertEqual(mathnorm.option_text(self.OPTIONS, "۲"), "B")
        self.assertEqual(mathnorm.option_text(self.OPTIONS, "ب"), "B")
        self.assertEqual(mathnorm.option_text(self.OPTIONS, "(3)"), "C")

    def test_position_is_the_fallback(self):
        bare = [{"text": "x"}, {"text": "y"}]
        self.assertEqual(mathnorm.option_text(bare, "ب"), "y")

    def test_an_unknown_label_is_none(self):
        self.assertIsNone(mathnorm.option_text(self.OPTIONS, "ز"))
        self.assertIsNone(mathnorm.option_text([], "1"))
        self.assertIsNone(mathnorm.option_text(None, "1"))


if __name__ == "__main__":
    unittest.main()
