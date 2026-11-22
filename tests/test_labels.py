"""Label pages, bright cells, semantic validation and the planner.

The worked example from the brief is reproduced end to end:

* stable S street #2 in the eastern district (names 东/新城); western T street
  also named 杉;
* reader A is calibrated; reader B may read 东/西 literally or swapped;
* the short paper 东 杉/2 is wrong for B unless the 东 cell is brightened
  (4 cells + 1 impression); the long paper 新城 杉/2 is safe with 0 impressions
  (5 cells);
* one extra B proof pinning 西 collapses B to the literal table, so the short
  form now works with 0 impressions at 4 cells; if all heads are lent out the
  long form still costs 5 cells.

A physical page is shared between counters, so a page must be safe for *every*
recipient's reader/catalog, not just one.
"""
from __future__ import annotations

import unittest

from support import fresh_planner_catalog

from address_delivery.labels import (
    PlanJob,
    Recipient,
    StableAddress,
    diagnose,
    plan_deck,
    plan_min_impressions,
    resolve_row,
)
from address_delivery.readers import Observation, Proof, Reader


def literal_reader(reader_id="A"):
    return Reader(
        reader_id, "e-" + reader_id, ["东", "西"], ["东", "西"],
        [Proof("东西", (Observation.exact("东"), Observation.exact("西")))],
    )


def swap_reader(reader_id="B", pinned=False):
    proofs = []
    if pinned:
        proofs = [Proof("西", (Observation.exact("西"),))]
    return Reader(reader_id, "e-" + reader_id, ["东", "西"], ["东", "西"], proofs)


def jobs_for(*addresses, max_rows=5, max_cells=40, separator="/"):
    return [
        PlanJob(address=a, max_rows=max_rows, max_cells=max_cells, separator=separator)
        for a in addresses
    ]


S2_EAST = StableAddress("P1", "C1", "DE", "SE", 2)
S2_WEST = StableAddress("P2", "C1", "DW", "SW", 2)


class WorkedExampleTests(unittest.TestCase):
    def setUp(self):
        self.cat = fresh_planner_catalog()

    def recipients(self, *readers):
        return [Recipient(r, self.cat) for r in readers]

    def test_calibrated_reader_short_form_is_four_cells_zero_impressions(self):
        rec = self.recipients(literal_reader("A"))
        sol = plan_deck(jobs_for(S2_EAST), self.cat, rec, max_impressions=10)
        self.assertEqual(sol.cells, 4)
        self.assertEqual(sol.impressions, 0)
        page = sol.pages[0]
        self.assertEqual(page.header.text, "东")
        self.assertEqual(page.rows[0].paper(), "杉/2")

    def test_uncalibrated_short_form_needs_one_impression(self):
        rec = self.recipients(swap_reader("B"))
        sol = plan_deck(jobs_for(S2_EAST), self.cat, rec, max_impressions=10)
        self.assertEqual(sol.cells, 4)
        self.assertEqual(sol.impressions, 1)
        page = sol.pages[0]
        self.assertEqual(page.header.text, "东")
        self.assertEqual(page.header_bright(), [0])

    def test_uncalibrated_long_form_zero_impressions_five_cells(self):
        rec = self.recipients(swap_reader("B"))
        sol = plan_deck(jobs_for(S2_EAST), self.cat, rec, max_impressions=0)
        self.assertEqual(sol.cells, 5)
        self.assertEqual(sol.impressions, 0)
        self.assertEqual(sol.pages[0].header.text, "新城")

    def test_extra_proof_pins_table_short_form_free(self):
        reader = swap_reader("B", pinned=True)
        self.assertEqual(reader.all_tables(), ({"东": "东", "西": "西"},))
        rec = self.recipients(reader)
        sol = plan_deck(jobs_for(S2_EAST), self.cat, rec, max_impressions=0)
        self.assertEqual(sol.cells, 4)
        self.assertEqual(sol.impressions, 0)
        self.assertEqual(sol.pages[0].header.text, "东")

    def test_fewest_borrowed_for_paid_four_cells(self):
        rec = self.recipients(swap_reader("B"))
        # A 4-cell allowance fits the short 东 form only if it is brightened,
        # so one borrowed impression is required.
        sol, imps = plan_min_impressions(jobs_for(S2_EAST), self.cat, rec, target_cells=4)
        self.assertIsNotNone(sol)
        self.assertLessEqual(sol.cells, 4)
        self.assertEqual(imps, 1)

    def test_fewest_borrowed_for_paid_five_cells_is_zero(self):
        rec = self.recipients(swap_reader("B"))
        sol, imps = plan_min_impressions(jobs_for(S2_EAST), self.cat, rec, target_cells=5)
        self.assertEqual(imps, 0)
        self.assertLessEqual(sol.cells, 5)

    def test_paid_three_cells_is_infeasible(self):
        rec = self.recipients(swap_reader("B"))
        sol, imps = plan_min_impressions(jobs_for(S2_EAST), self.cat, rec, target_cells=3)
        self.assertIsNone(sol)
        self.assertEqual(imps, -1)


class SharedPageRecipientTests(unittest.TestCase):
    """One physical page is read by several counters with different readers."""

    def setUp(self):
        self.cat = fresh_planner_catalog()

    def test_shared_page_needs_impression_for_the_weakest_reader(self):
        a = literal_reader("A")
        b = swap_reader("B")
        recips = [Recipient(a, self.cat), Recipient(b, self.cat)]
        # A alone would be free at 4 cells, but B forces one bright cell.
        sol = plan_deck(jobs_for(S2_EAST), self.cat, recips, max_impressions=10)
        self.assertEqual(sol.cells, 4)
        self.assertEqual(sol.impressions, 1)
        page = sol.pages[0]
        self.assertTrue(page.valid_for(Recipient(a, self.cat)))
        self.assertTrue(page.valid_for(Recipient(b, self.cat)))

    def test_shared_page_without_impressions_uses_long_header(self):
        recips = [Recipient(literal_reader("A"), self.cat), Recipient(swap_reader("B"), self.cat)]
        sol = plan_deck(jobs_for(S2_EAST), self.cat, recips, max_impressions=0)
        self.assertEqual(sol.cells, 5)
        self.assertEqual(sol.pages[0].header.text, "新城")
        self.assertEqual(sol.impressions, 0)

    def test_every_counter_receipt_passes(self):
        recips = [Recipient(literal_reader("A"), self.cat), Recipient(swap_reader("B"), self.cat)]
        sol = plan_deck(jobs_for(S2_EAST), self.cat, recips, max_impressions=10)
        payload = sol.as_dict(recips)
        for reader_id, receipts in payload["receipts"].items():
            self.assertTrue(receipts[0]["ok"], msg=reader_id)
            self.assertGreaterEqual(receipts[0]["tables"], 1)

    def test_contradictory_counter_makes_page_infeasible(self):
        bad = Reader("BAD", "e", ["东", "西"], ["东", "西"], [
            Proof("东", (Observation.exact("西"),)),
            Proof("东", (Observation.exact("东"),)),
        ])
        recips = [Recipient(bad, self.cat)]
        sol = plan_deck(jobs_for(S2_EAST), self.cat, recips, max_impressions=10)
        self.assertIsNone(sol)


class TwoRowSharedHeaderTests(unittest.TestCase):
    def _catalog_with_second_east_street(self):
        cat = fresh_planner_catalog()
        cat.conn.execute(
            "INSERT INTO streets(street_id,district_id,name,active,created_version)"
            " VALUES('SN','DE','楠',1,1)"
        )
        cat.conn.execute(
            "INSERT INTO street_names(street_id,text,is_primary) VALUES('SN','楠',1)"
        )
        cat.conn.execute(
            "INSERT INTO coverages(coverage_id,street_id,station_id,lo,hi,parity)"
            " VALUES('KN','SN','A',1,100,'any')"
        )
        return cat

    def test_two_rows_share_one_header_and_one_impression(self):
        cat = self._catalog_with_second_east_street()
        recips = [Recipient(swap_reader("B"), cat)]
        east_nan = StableAddress("P3", "C1", "DE", "SN", 4)
        sol = plan_deck(jobs_for(S2_EAST, east_nan), cat, recips, max_impressions=1)
        self.assertEqual(len(sol.pages), 1)
        page = sol.pages[0]
        self.assertEqual(len(page.rows), 2)
        # One shared header (东, one bright cell) + two rows of 3 cells each.
        self.assertEqual(page.header.text, "东")
        self.assertEqual(page.header_bright(), [0])
        self.assertEqual(sol.impressions, 1)
        self.assertEqual(sol.cells, 1 + 3 + 3)

    def test_two_rows_without_impressions_use_long_header(self):
        cat = self._catalog_with_second_east_street()
        recips = [Recipient(swap_reader("B"), cat)]
        east_nan = StableAddress("P3", "C1", "DE", "SN", 4)
        sol = plan_deck(jobs_for(S2_EAST, east_nan), cat, recips, max_impressions=0)
        self.assertEqual(len(sol.pages), 1)
        self.assertEqual(sol.pages[0].header.text, "新城")
        self.assertEqual(sol.impressions, 0)


class SameTableStringTests(unittest.TestCase):
    """A pair that looks possible cell-by-cell but is impossible for one table."""

    def test_impossible_two_readings_from_one_bijection(self):
        reader = Reader("X", "e", ["甲", "乙"], ["子", "丑"])
        # (甲->子, 乙->子) would map two paper glyphs to the same output; no
        # bijection allows it, even though each cell alone looks possible.
        self.assertFalse(reader.pair_is_possible("甲", (), "乙", (), "子", "子"))
        self.assertTrue(reader.pair_is_possible("甲", (), "乙", (), "子", "丑"))
        combos = reader.consistent_decodings([("甲乙", ()), ("乙甲", ())])
        for first, second in combos:
            self.assertEqual(sorted(first), sorted(second))
            self.assertNotEqual(first, second)


class DiagnosisTests(unittest.TestCase):
    def setUp(self):
        self.cat = fresh_planner_catalog()

    def test_ambiguous_when_table_unpinned(self):
        report = diagnose(swap_reader("B"), self.cat, "C1", "东", "杉", 2)
        self.assertEqual(report["status"], "ambiguous")
        self.assertEqual(report["possible_streets"], ["SE", "SW"])

    def test_unique_after_pinning(self):
        report = diagnose(swap_reader("B", pinned=True), self.cat, "C1", "东", "杉", 2)
        self.assertEqual(report["status"], "unique")
        self.assertEqual(report["possible_streets"], ["SE"])

    def test_unknown_street_credential(self):
        report = diagnose(literal_reader("A"), self.cat, "C1", "东", "不存在", 2)
        self.assertEqual(report["status"], "none")
        self.assertEqual(report["failures"][0]["reason"], "unknown_street")

    def test_inactive_failure(self):
        cat = fresh_planner_catalog()
        cat.apply_revision(
            {"revision": "off", "operations": [{"op": "deactivate_street", "street_id": "SE"}]}
        )
        report = diagnose(literal_reader("A"), cat, "C1", "东", "杉", 2)
        self.assertEqual(report["failures"][0]["reason"], "inactive")

    def test_contradiction_withdraws_credential(self):
        bad = Reader("BAD", "e", ["东"], ["东"], [
            Proof("东", (Observation.exact("西"),)),
        ])
        report = diagnose(bad, self.cat, "C1", "东", "杉", 2)
        self.assertFalse(report["credential_issued"])
        self.assertEqual(report["status"], "contradiction")


class ResolveRowTests(unittest.TestCase):
    def setUp(self):
        self.cat = fresh_planner_catalog()

    def test_full_parent_chain(self):
        self.assertEqual(resolve_row(self.cat, "C1", "东", "杉", 2)["street_id"], "SE")
        self.assertEqual(resolve_row(self.cat, "C1", "西", "杉", 2)["street_id"], "SW")

    def test_unknown_paths(self):
        self.assertEqual(resolve_row(self.cat, "C9", "东", "杉", 2)["reason"], "unknown_city")
        self.assertEqual(resolve_row(self.cat, "C1", "无", "杉", 2)["reason"], "unknown_district")
        self.assertEqual(resolve_row(self.cat, "C1", "东", "无", 2)["reason"], "unknown_street")


class StationExclusionTests(unittest.TestCase):
    """Same-named streets are excluded by a valid station for the house number."""

    def _catalog(self, second_covers=False):
        from address_delivery.catalog import Catalog
        from address_delivery.db import connect

        cat = Catalog(connect(":memory:"))
        coverages = [
            {"coverage_id": "K1", "street_id": "SE", "station_id": "A",
             "lo": 1, "hi": 10, "parity": "any"},
        ]
        if second_covers:
            coverages.append(
                {"coverage_id": "K2", "street_id": "SX", "station_id": "B",
                 "lo": 1, "hi": 10, "parity": "any"}
            )
        cat.load_initial(
            {
                "revision": "x",
                "cities": [{"city_id": "C1", "name": "x"}],
                "districts": [{"district_id": "DE", "city_id": "C1", "name": "东区", "aliases": ["东"]}],
                "streets": [
                    {"street_id": "SE", "district_id": "DE", "name": "杉", "aliases": [], "active": True},
                    {"street_id": "SX", "district_id": "DE", "name": "杉", "aliases": [], "active": True},
                ],
            },
            {"coverages": coverages},
        )
        return cat

    def test_station_excludes_non_serving_same_named_street(self):
        cat = self._catalog(second_covers=False)
        result = resolve_row(cat, "C1", "东", "杉", 2)
        self.assertEqual(result["reason"], "ok")
        self.assertEqual(result["street_id"], "SE")

    def test_two_deliverable_same_names_stay_ambiguous(self):
        cat = self._catalog(second_covers=True)
        result = resolve_row(cat, "C1", "东", "杉", 2)
        self.assertEqual(result["reason"], "ambiguous")
        self.assertIsNone(result["street_id"])

    def test_no_station_anywhere_fails(self):
        cat = self._catalog(second_covers=False)
        result = resolve_row(cat, "C1", "东", "杉", 99)
        self.assertEqual(result["reason"], "no_station")

    def test_inactive_target_still_fails_recognition(self):
        from address_delivery.catalog import Catalog
        from address_delivery.db import connect

        cat = Catalog(connect(":memory:"))
        cat.load_initial(
            {
                "revision": "x",
                "cities": [{"city_id": "C1", "name": "x"}],
                "districts": [{"district_id": "DE", "city_id": "C1", "name": "东区", "aliases": ["东"]}],
                "streets": [
                    {"street_id": "SE", "district_id": "DE", "name": "杉", "aliases": [], "active": False},
                ],
            },
            {"coverages": [
                {"coverage_id": "K1", "street_id": "SE", "station_id": "A",
                 "lo": 1, "hi": 10, "parity": "any"},
            ]},
        )
        result = resolve_row(cat, "C1", "东", "杉", 2)
        self.assertEqual(result["reason"], "inactive")
        # Even though street_id names the target, a page must not validate.
        reader = literal_reader("A")
        sol = plan_deck(
            jobs_for(StableAddress("P1", "C1", "DE", "SE", 2)), cat,
            [Recipient(reader, cat)], max_impressions=10,
        )
        self.assertIsNone(sol)

    def test_page_can_avoid_impression_when_station_disambiguates(self):
        cat = self._catalog(second_covers=False)
        reader = literal_reader("A")
        recips = [Recipient(reader, cat)]
        addr = StableAddress("P1", "C1", "DE", "SE", 2)
        sol = plan_deck(jobs_for(addr), cat, recips, max_impressions=0)
        self.assertIsNotNone(sol)
        self.assertEqual(sol.impressions, 0)
        self.assertEqual(sol.pages[0].header.text, "东")


class GreedyTrapTests(unittest.TestCase):
    """Picking the shortest name per row independently can lose; the planner
    must choose the globally cheaper shared-header layout."""

    def setUp(self):
        self.cat = fresh_planner_catalog()
        self.cat.conn.execute(
            "INSERT INTO streets(street_id,district_id,name,active,created_version)"
            " VALUES('SN','DE','楠',1,1)"
        )
        self.cat.conn.execute(
            "INSERT INTO street_names(street_id,text,is_primary) VALUES('SN','楠',1)"
        )
        self.cat.conn.execute(
            "INSERT INTO coverages(coverage_id,street_id,station_id,lo,hi,parity)"
            " VALUES('KN','SN','A',1,100,'any')"
        )

    def test_short_names_split_but_shared_long_header_wins(self):
        recips = [Recipient(swap_reader("B"), self.cat)]
        east_nan = StableAddress("P2", "C1", "DE", "SN", 2)
        # Per-row shortest would use 东 + one impression on each row and, being
        # unable to share a header safely under a 1-cell budget, would need two
        # pages. Sharing 新城 once at 0 impressions fits both rows on one page.
        sol = plan_deck(
            jobs_for(S2_EAST, east_nan, max_rows=2, max_cells=8),
            self.cat, recips, max_impressions=0,
        )
        self.assertEqual(len(sol.pages), 1)
        self.assertEqual(sol.pages[0].header.text, "新城")
        self.assertEqual(len(sol.pages[0].rows), 2)
        self.assertEqual(sol.impressions, 0)

    def test_short_street_alias_loses_to_long_canonical_without_impressions(self):
        # Give SE a shorter but confusing alias that needs brightening, and a
        # longer canonical that is safe with no impressions.
        cat = fresh_planner_catalog()
        cat.apply_revision({
            "revision": "alias",
            "operations": [
                {"op": "add_street_alias", "street_id": "SE", "alias": "杉东"},
            ],
        })
        recips = [Recipient(swap_reader("B"), cat)]
        sol = plan_deck(jobs_for(S2_EAST), cat, recips, max_impressions=0)
        # Zero impressions: must use the safe long canonical 杉树路? There is no
        # such name, so the only street text 杉 (no confusing glyph) is used.
        self.assertEqual(sol.pages[0].rows[0].street.text, "杉")
        self.assertEqual(sol.impressions, 0)
        # With impressions available, the shorter 2-cell 杉东 can win on cells.
        sol2 = plan_deck(jobs_for(S2_EAST), cat, recips, max_impressions=10)
        self.assertLessEqual(sol2.cells, sol.cells)


class PaginationTests(unittest.TestCase):
    def setUp(self):
        self.cat = fresh_planner_catalog()
        self.cat.conn.execute(
            "INSERT INTO streets(street_id,district_id,name,active,created_version)"
            " VALUES('SN','DE','楠',1,1)"
        )
        self.cat.conn.execute(
            "INSERT INTO street_names(street_id,text,is_primary) VALUES('SN','楠',1)"
        )
        self.cat.conn.execute(
            "INSERT INTO coverages(coverage_id,street_id,station_id,lo,hi,parity)"
            " VALUES('KN','SN','A',1,100,'any')"
        )

    def addrs(self, n):
        return [
            StableAddress(
                f"P{i}", "C1", "DE", "SE" if i % 2 == 0 else "SN", i + 1
            )
            for i in range(n)
        ]

    def test_max_rows_forces_repeated_header_pressings(self):
        recips = [Recipient(swap_reader("B"), self.cat)]
        sol = plan_deck(
            [PlanJob(a, 2, 40, "/") for a in self.addrs(6)],
            self.cat, recips, max_impressions=10,
        )
        payload = sol.as_dict(recips)
        self.assertEqual(payload["page_count"], 3)
        # Header is re-pressed on every page (charged per page).
        self.assertEqual(payload["header_pressings"], 3)
        self.assertIn("东", payload["shared_headers"])

    def test_zero_impressions_uses_long_header_on_every_page(self):
        recips = [Recipient(swap_reader("B"), self.cat)]
        sol = plan_deck(
            [PlanJob(a, 2, 40, "/") for a in self.addrs(4)],
            self.cat, recips, max_impressions=0,
        )
        self.assertEqual(sol.impressions, 0)
        for page in sol.pages:
            self.assertEqual(page.header.text, "新城")

    def test_district_change_starts_new_page(self):
        recips = [Recipient(swap_reader("B"), self.cat)]
        addrs = [
            StableAddress("P1", "C1", "DE", "SE", 1),
            StableAddress("P2", "C1", "DW", "SW", 2),
        ]
        sol = plan_deck(
            [PlanJob(a, 5, 40, "/") for a in addrs],
            self.cat, recips, max_impressions=10,
        )
        self.assertEqual(len(sol.pages), 2)
        self.assertEqual([p.header.text for p in sol.pages], ["东", "西"])


if __name__ == "__main__":
    unittest.main()
