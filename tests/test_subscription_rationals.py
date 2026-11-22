"""Exact rational literal parsing and canonical text form."""
from __future__ import annotations

import unittest
from fractions import Fraction

from address_delivery.subscription.rationals import Q, frac_from_text, frac_to_text


class RationalTests(unittest.TestCase):
    def test_canonical_text_is_lowest_terms(self):
        self.assertEqual(frac_to_text(Q(2, 3)), "2/3")
        self.assertEqual(frac_to_text(Q(4, 6)), "2/3")
        self.assertEqual(frac_to_text(Q(1)), "1")
        self.assertEqual(frac_to_text(Q(0)), "0")

    def test_parse_fraction_decimal_and_integer_literals(self):
        self.assertEqual(frac_from_text("2/3"), Fraction(2, 3))
        self.assertEqual(frac_from_text(" 4/8 "), Fraction(1, 2))
        self.assertEqual(frac_from_text("0.5"), Fraction(1, 2))
        self.assertEqual(frac_from_text("0.25"), Fraction(1, 4))
        self.assertEqual(frac_from_text("3"), Fraction(3))
        self.assertEqual(frac_from_text(Fraction(1, 7)), Fraction(1, 7))
        self.assertEqual(frac_from_text(2), Fraction(2))

    def test_arithmetic_stays_exact(self):
        share = frac_from_text("2/3")
        # A 2:1 split of one unit, merged and accumulated, never turns into a float.
        self.assertEqual(share + frac_from_text("1/3"), Q(1))
        self.assertEqual(share * frac_from_text("1/2"), Q(1, 3))

    def test_bad_literals_are_rejected(self):
        for bad in ("", " ", "1/0", "a/b", "1e-3", "1.2.3", "1/", "/2", None):
            with self.assertRaises(ValueError):
                frac_from_text(bad)


if __name__ == "__main__":
    unittest.main()
