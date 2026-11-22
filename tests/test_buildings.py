"""Building registry: recognise a frozen job's building, parity, two-buildings."""
from __future__ import annotations

import unittest

from support import fresh_synthetic_service

from address_delivery.buildings import (
    BUILDING_NO_JOB,
    BUILDING_TWO,
    BUILDING_UNIQUE,
    BUILDING_UNMATCHED,
    BuildingError,
)
from address_delivery.models import AddressInput


class BuildingRecognitionTests(unittest.TestCase):
    def setUp(self) -> None:
        # S4 公园路: even 2-10 -> station P, odd 1-9 -> station Q.
        self.svc = fresh_synthetic_service()
        self.svc.buildings.register_building(
            "B-EVEN", "S4", 2, 10, parity="even", station_id="P", name="偶数楼"
        )
        self.svc.buildings.register_building(
            "B-ODD", "S4", 1, 9, parity="odd", station_id="Q", name="奇数楼"
        )

    def _submit(self, parcel_id: str, house_no: int):
        result = self.svc.delivery.attempt(
            AddressInput(parcel_id, "C1", "北区", "公园路", house_no), self.svc.catalog
        )
        self.assertTrue(result.ok)
        return result

    def test_parity_segments_recognise_distinct_buildings(self):
        self._submit("E1", 6)
        self._submit("O1", 3)
        self.assertEqual(self.svc.recognize_building("E1")["building_id"], "B-EVEN")
        self.assertEqual(self.svc.recognize_building("O1")["building_id"], "B-ODD")

    def test_endpoints_are_inclusive_and_parity_filtered(self):
        even = self.svc.buildings.get_building("B-EVEN")
        self.assertTrue(even.covers(2))
        self.assertTrue(even.covers(10))
        self.assertFalse(even.covers(9))  # right endpoint but wrong parity
        odd = self.svc.buildings.get_building("B-ODD")
        self.assertTrue(odd.covers(1))
        self.assertFalse(odd.covers(2))

    def test_same_house_two_buildings_goes_to_concierge(self):
        # Two station-P buildings both cover even 2-4.
        self.svc.buildings.register_building(
            "B-DUP", "S4", 2, 4, parity="even", station_id="P"
        )
        self._submit("E2", 2)
        recognition = self.svc.recognize_building("E2")
        self.assertEqual(recognition["outcome"], BUILDING_TWO)
        self.assertIsNone(recognition["building_id"])
        self.assertEqual(
            {b["building_id"] for b in recognition["buildings"]}, {"B-EVEN", "B-DUP"}
        )

    def test_station_pin_keeps_same_address_in_separate_buildings(self):
        # A building pinned to Q must not claim a job frozen to station P.
        self.svc.buildings.register_building(
            "B-Q-PIN", "S4", 6, 6, parity="even", station_id="Q"
        )
        self._submit("E6", 6)
        recognition = self.svc.recognize_building("E6")
        self.assertEqual(recognition["outcome"], BUILDING_UNIQUE)
        self.assertEqual(recognition["building_id"], "B-EVEN")

    def test_unmatched_house_number(self):
        # House 9 matches both B-ODD and an overlapping B-NINE -> two buildings.
        self.svc.buildings.register_building(
            "B-NINE", "S4", 9, 9, parity="odd", station_id="Q"
        )
        self._submit("O9", 9)
        self.assertEqual(self.svc.recognize_building("O9")["outcome"], BUILDING_TWO)

        # A clean registry where the only segment stops at house 4: a submitted
        # house-8 job matches no building and is reported unmatched.
        from address_delivery.buildings import BuildingRegistry
        from address_delivery.db import connect

        clean = connect(":memory:")
        try:
            registry = BuildingRegistry(clean)
            registry.register_building("B-LOW", "S4", 1, 4, parity="even", station_id="P")
            self._submit("E8", 8)
            recognition = registry.recognize_parcel(self.svc.delivery, "E8")
            self.assertEqual(recognition.outcome, BUILDING_UNMATCHED)
        finally:
            clean.close()

    def test_unsuccessful_parcel_cannot_be_supplemented(self):
        # An ambiguous address never creates a job -> no building inference.
        self.svc.delivery.attempt(
            AddressInput("BAD", "C0", "同名", "同名路", 4), self.svc.catalog
        )
        recognition = self.svc.recognize_building("BAD")
        self.assertEqual(recognition["outcome"], BUILDING_NO_JOB)
        self.assertEqual(recognition["buildings"], [])
        self.assertIsNone(recognition["job"])

    def test_recognition_uses_frozen_not_current_catalog(self):
        self._submit("E1", 6)
        first = self.svc.recognize_building("E1")
        # Re-sending identical content replays the frozen job; building stays.
        replay = self.svc.delivery.attempt(
            AddressInput("E1", "C1", "北区", "公园路", 6), self.svc.catalog
        )
        self.assertEqual(replay.outcome, "replayed")
        self.assertEqual(
            self.svc.recognize_building("E1")["building_id"], first["building_id"]
        )


class BuildingDefinitionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = fresh_synthetic_service()

    def test_duplicate_id_rejected(self):
        self.svc.buildings.register_building("B", "S4", 1, 2)
        with self.assertRaises(BuildingError):
            self.svc.buildings.register_building("B", "S4", 3, 4)

    def test_bad_range_and_parity_rejected(self):
        with self.assertRaises(BuildingError):
            self.svc.buildings.register_building("B1", "S4", 5, 2)
        with self.assertRaises(BuildingError):
            self.svc.buildings.register_building("B2", "S4", 0, 2)
        with self.assertRaises(BuildingError):
            self.svc.buildings.register_building("B3", "S4", 1, 2, parity="weekly")

    def test_overlapping_segments_are_allowed_on_purpose(self):
        self.svc.buildings.register_building("B1", "S4", 1, 5)
        self.svc.buildings.register_building("B2", "S4", 3, 8)
        self.assertEqual(
            {b.building_id for b in self.svc.buildings.candidates_for("S4", 4)},
            {"B1", "B2"},
        )


if __name__ == "__main__":
    unittest.main()
