"""Synthetic readers: confusion groups, proofs, calibration, decode constraints."""
from __future__ import annotations

import unittest

from address_delivery.readers import (
    Observation,
    Proof,
    Reader,
    reader_from_json,
)
from address_delivery.errors import ReaderContradiction, ReaderError


def reader(paper, output, proofs=()):
    return Reader("R", "v1", paper, output, list(proofs))


class ConfusionGroupTests(unittest.TestCase):
    def test_group_must_be_equal_sized(self):
        with self.assertRaises(ReaderError):
            reader(["甲", "乙"], ["子"])

    def test_duplicates_rejected(self):
        with self.assertRaises(ReaderError):
            reader(["甲", "甲"], ["子", "丑"])

    def test_glyphs_must_be_single_code_points(self):
        with self.assertRaises(ReaderError):
            reader(["甲乙"], ["子"])

    def test_unconstrained_group_has_every_bijection(self):
        r = reader(["甲", "乙", "丙"], ["子", "丑", "寅"])
        self.assertEqual(len(r.all_tables()), 6)
        self.assertTrue(r.calibrated)

    def test_characters_outside_group_read_literally(self):
        r = reader(["甲"], ["子"])
        for table in r.all_tables():
            self.assertEqual(r.decode("甲乙路", table), "子乙路")


class ProofCalibrationTests(unittest.TestCase):
    def test_exact_observations_pin_table(self):
        r = reader(
            ["东", "西"], ["东", "西"],
            [Proof("东西", (Observation.exact("西"), Observation.exact("东")))],
        )
        self.assertEqual(r.all_tables(), ({"东": "西", "西": "东"},))

    def test_choices_and_missing_keep_searching(self):
        r = reader(
            ["甲", "乙"], ["子", "丑"],
            [Proof("甲", (Observation.choices("子", "丑"),))],
        )
        self.assertEqual(len(r.all_tables()), 2)

    def test_missing_observation_is_permissive(self):
        r = reader(
            ["甲", "乙"], ["子", "丑"],
            [Proof("甲乙", (Observation.missing(), Observation.exact("子")))],
        )
        # 乙 -> 子 forces 甲 -> 丑.
        self.assertEqual(r.all_tables(), ({"甲": "丑", "乙": "子"},))

    def test_all_proofs_share_one_table(self):
        # Two proofs that together force a single permutation.
        r = reader(
            ["甲", "乙"], ["子", "丑"],
            [
                Proof("甲", (Observation.exact("丑"),)),
                Proof("甲乙", (Observation.exact("丑"), Observation.exact("子"))),
            ],
        )
        self.assertEqual(r.all_tables(), ({"甲": "丑", "乙": "子"},))

    def test_contradiction_withdraws_credential(self):
        r = reader(
            ["东", "西"], ["东", "西"],
            [
                Proof("东西", (Observation.exact("东"), Observation.exact("东"))),
                Proof("东西", (Observation.exact("东"), Observation.exact("西"))),
            ],
        )
        self.assertEqual(r.all_tables(), ())
        self.assertFalse(r.calibrated)
        self.assertEqual(r.status, "contradiction")
        self.assertFalse(r.credential()["credential_issued"])
        with self.assertRaises(ReaderContradiction):
            r.require_calibrated()

    def test_adding_a_proof_narrows_tables(self):
        r = reader(["东", "西"], ["东", "西"])
        self.assertEqual(len(r.all_tables()), 2)
        r.add_proof(Proof("西", (Observation.exact("西"),)))
        self.assertEqual(r.all_tables(), ({"东": "东", "西": "西"},))

    def test_correcting_a_proof_reissues_credential(self):
        r = reader(
            ["东", "西"], ["东", "西"],
            [Proof("东西", (Observation.exact("东"), Observation.exact("东")))],
        )
        self.assertFalse(r.calibrated)
        r.replace_proof(0, Proof("东西", (Observation.exact("东"), Observation.exact("西"))))
        self.assertTrue(r.calibrated)
        self.assertEqual(r.all_tables(), ({"东": "东", "西": "西"},))


class DecodeConsistencyTests(unittest.TestCase):
    def test_header_and_street_share_one_table(self):
        r = reader(["甲", "乙"], ["子", "丑"])
        combos = r.consistent_decodings([("甲", ()), ("乙", ())])
        # Never ("子", "子") or ("丑", "丑"): one bijection cannot map both
        # paper glyphs to the same output glyph.
        self.assertEqual(set(combos), {("子", "丑"), ("丑", "子")})

    def test_pair_is_possible_rejects_double_reading(self):
        r = reader(["甲", "乙"], ["子", "丑"])
        self.assertTrue(r.pair_is_possible("甲", (), "乙", (), "子", "丑"))
        self.assertFalse(r.pair_is_possible("甲", (), "乙", (), "子", "子"))
        self.assertFalse(r.pair_is_possible("甲", (), "乙", (), "丑", "丑"))

    def test_bright_cell_reads_literally(self):
        r = reader(["东", "西"], ["东", "西"])
        # With no brightness both readings exist.
        self.assertEqual(r.possible_decodings("东西"), ["东西", "西东"])
        # Brightening the first cell keeps the paper 东 regardless of table.
        self.assertEqual(r.possible_decodings("东西", bright=[0]), ["东东", "东西"])

    def test_possible_decodings_is_sorted_unique(self):
        r = reader(["甲"], ["乙"])
        self.assertEqual(r.possible_decodings("甲"), ["乙"])

    def test_indistinguishable_pairs(self):
        r = reader(["东", "西"], ["东", "西"])
        report = r.indistinguishable_pairs("东西")
        self.assertEqual(report["readings"], ["东西", "西东"])
        self.assertEqual(report["pairs"], [["东西", "西东"]])
        # After a pinning proof no pair remains.
        r.add_proof(Proof("东", (Observation.exact("东"),)))
        self.assertEqual(r.indistinguishable_pairs("东西")["pairs"], [])


class ReaderJsonTests(unittest.TestCase):
    def test_roundtrip_json(self):
        obj = {
            "reader_id": "R2",
            "version": "e2",
            "confusion": {"paper": ["东", "西"], "output": ["东", "西"]},
            "proofs": [{"paper": "西", "observed": [{"exact": "西"}]}],
        }
        r = reader_from_json(obj)
        self.assertTrue(r.calibrated)
        self.assertEqual(r.all_tables(), ({"东": "东", "西": "西"},))

    def test_missing_observation_json(self):
        obj = {
            "reader_id": "R",
            "version": "v",
            "confusion": {"paper": ["甲", "乙"], "output": ["子", "丑"]},
            "proofs": [{"paper": "甲乙", "observed": [{"missing": True}, {"choices": ["子", "丑"]}]}],
        }
        r = reader_from_json(obj)
        self.assertEqual(len(r.all_tables()), 2)


if __name__ == "__main__":
    unittest.main()
