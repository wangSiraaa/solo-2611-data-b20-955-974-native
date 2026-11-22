"""Physical-key ledger: handover/receipt, in-transit, disputes, cutoff sheets."""
from __future__ import annotations

import unittest

from address_delivery.db import connect
from address_delivery.errors import KeyLedgerError
from address_delivery.keys import (
    DISPUTE_CONTRADICTION,
    DISPUTE_RECEIPT_NO_HANDOVER,
    DISPUTE_UNAUTHORIZED,
    KeyLedger,
    STATUS_DISPUTED,
    STATUS_HELD,
    STATUS_IN_TRANSIT,
)


class LedgerCase(unittest.TestCase):
    def setUp(self) -> None:
        self.conn = connect(":memory:")
        self.led = KeyLedger(self.conn)
        for counter in ("总柜", "H", "G"):
            self.led.register_counter(counter)
        self.led.register_key("K", "总柜")

    def tearDown(self) -> None:
        self.conn.close()

    def handover_and_receipt(self) -> None:
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        self.led.record_receipt("T1", "K", "总柜", "H", -4)


class CustodyFlowTests(LedgerCase):
    def test_handover_is_in_transit_until_receipt(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        mid = self.led.ownership_at("K", -4)
        self.assertEqual(mid.status, STATUS_IN_TRANSIT)
        self.assertEqual(mid.holder, "总柜")  # possession has not moved yet
        self.assertEqual(mid.open_transfer, "T1")
        self.assertFalse(mid.borrowable)

    def test_receipt_moves_custody_and_makes_lendable(self):
        self.handover_and_receipt()
        owner = self.led.ownership_at("K", 0)
        self.assertEqual(owner.status, STATUS_HELD)
        self.assertEqual(owner.holder, "H")
        self.assertTrue(owner.borrowable)
        # The basis is the ordered pair of applied signatures.
        kinds = [b["kind"] for b in owner.basis]
        self.assertEqual(kinds, ["handover", "receipt"])

    def test_identical_resend_is_one_idempotent_entry(self):
        first = self.led.record_handover("T1", "K", "总柜", "H", -5)
        second = self.led.record_handover("T1", "K", "总柜", "H", -5)
        self.assertFalse(first["idempotent"])
        self.assertTrue(second["idempotent"])
        records = self.led.list_records("K")
        self.assertEqual(len(records), 1)

    def test_each_key_counted_independently(self):
        self.led.register_key("K2", "总柜")
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        # K in transit does not affect K2, which stays quietly held.
        self.assertEqual(self.led.ownership_at("K", 0).status, STATUS_IN_TRANSIT)
        self.assertEqual(self.led.ownership_at("K2", 0).status, STATUS_HELD)
        self.assertTrue(self.led.ownership_at("K2", 0).borrowable)


class InTransitRulesTests(LedgerCase):
    def test_neither_side_can_lend_while_in_transit(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        # The would-be receiver H is not the holder yet and hands over -> dispute.
        self.led.record_handover("T2", "K", "H", "G", -3)
        dispute = self.led.dispute("K")
        self.assertEqual(dispute["reason"], DISPUTE_UNAUTHORIZED)
        self.assertEqual(dispute["start_moment"], -3)
        self.assertEqual(self.led.ownership_at("K", 0).status, STATUS_DISPUTED)

    def test_handover_without_receipt_is_merely_in_transit(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        self.assertIsNone(self.led.dispute("K"))
        self.assertEqual(self.led.ownership_at("K", 0).status, STATUS_IN_TRANSIT)

    def test_unknown_party_and_unknown_key_are_hard_errors(self):
        with self.assertRaises(KeyLedgerError):
            self.led.record_handover("T", "K", "总柜", "NOPE", -5)
        with self.assertRaises(KeyLedgerError):
            self.led.record_handover("T", "NOPE", "总柜", "H", -5)

    def test_receipt_before_handover_moment_rejected(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        with self.assertRaises(KeyLedgerError):
            self.led.record_receipt("T1", "K", "总柜", "H", -6)


class DisputeTests(LedgerCase):
    def test_receipt_without_handover_disputes(self):
        self.led.record_receipt("R1", "K", "总柜", "H", -2)
        dispute = self.led.dispute("K")
        self.assertEqual(dispute["reason"], DISPUTE_RECEIPT_NO_HANDOVER)
        self.assertFalse(self.led.ownership_at("K", 0).borrowable)

    def test_contradictory_handover_under_one_transfer_id(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        # Same transfer id, different receiver -> contradictory signed records.
        self.led.record_handover("T1", "K", "总柜", "G", -4)
        dispute = self.led.dispute("K")
        self.assertEqual(dispute["reason"], DISPUTE_CONTRADICTION)
        self.assertEqual(len(dispute["evidence"]), 2)

    def test_receipt_party_mismatch_is_contradiction(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        self.led.record_receipt("T1", "K", "总柜", "G", -4)  # receiver differs
        self.assertEqual(self.led.dispute("K")["reason"], DISPUTE_CONTRADICTION)

    def test_dispute_stops_lending_and_cannot_be_healed(self):
        self.led.record_receipt("R1", "K", "总柜", "H", -2)
        with self.assertRaises(KeyLedgerError):
            self.led.record_handover("T9", "K", "总柜", "H", 1)
        # Even a perfectly-formed later transfer is refused; the evidence log
        # keeps the original disputed record rather than deleting it.
        self.assertEqual(self.led.dispute("K")["reason"], DISPUTE_RECEIPT_NO_HANDOVER)
        self.assertTrue(any(r.transfer_id == "R1" for r in self.led.list_records("K")))

    def test_ownership_query_reports_basis_and_dispute(self):
        self.led.record_receipt("R1", "K", "总柜", "H", -2)
        owner = self.led.ownership_at("K", 10)
        self.assertTrue(owner.disputed)
        self.assertIsNotNone(owner.dispute)
        self.assertFalse(owner.borrowable)


class CutoffSnapshotTests(LedgerCase):
    def test_snapshot_freezes_per_key_state(self):
        self.handover_and_receipt()
        sheet = self.led.freeze_cutoff("cut0", 0)
        self.assertEqual(sheet["cutoff_moment"], 0)
        entry = sheet["keys"][0]
        self.assertEqual(entry["key_code"], "K")
        self.assertEqual(entry["holder"], "H")
        self.assertTrue(entry["borrowable"])

    def test_in_transit_sheet_built_from_one_sided_record(self):
        self.led.record_handover("T1", "K", "总柜", "H", -5)
        sheet = self.led.freeze_cutoff("cut0", 0)
        entry = sheet["keys"][0]
        self.assertEqual(entry["status"], STATUS_IN_TRANSIT)
        self.assertFalse(entry["borrowable"])
        self.assertEqual(entry["open_transfer"], "T1")
        self.assertEqual(self.led.borrowable_keys("cut0"), {})

    def test_later_history_is_never_appended_to_frozen_sheet(self):
        self.handover_and_receipt()
        self.led.freeze_cutoff("cut0", 0)
        self.led.record_handover("T2", "K", "H", "G", 2)
        self.led.record_receipt("T2", "K", "H", "G", 3)
        # Frozen sheet still says H; live custody has moved on to G.
        self.assertEqual(self.led.get_cutoff("cut0")["keys"][0]["holder"], "H")
        self.assertEqual(self.led.ownership_at("K", 5).holder, "G")
        # Re-freezing the same id returns the stored sheet verbatim.
        again = self.led.freeze_cutoff("cut0", 0)
        self.assertEqual(again["keys"][0]["holder"], "H")

    def test_borrowable_keys_groups_by_counter(self):
        self.led.register_key("K2", "H")
        self.handover_and_receipt()  # K now held at H
        sheet = self.led.freeze_cutoff("cut0", 0)
        grouped = self.led.borrowable_keys("cut0")
        self.assertEqual(grouped, {"H": ["K", "K2"]})


class ReservationTests(LedgerCase):
    def test_half_open_interval_overlap(self):
        self.led.add_external_reservation("r1", "K", 2, 5)
        # Adjacent intervals do not overlap (half-open).
        self.assertFalse(self.led.has_overlap("K", 0, 2))
        self.assertFalse(self.led.has_overlap("K", 5, 7))
        # Any interior touch overlaps.
        self.assertTrue(self.led.has_overlap("K", 4, 6))
        self.assertTrue(self.led.has_overlap("K", 3, 4))

    def test_reservation_idempotent_and_terms_locked(self):
        self.led.add_external_reservation("r1", "K", 2, 5)
        again = self.led.add_external_reservation("r1", "K", 2, 5)
        self.assertEqual(again.reservation_id, "r1")
        with self.assertRaises(KeyLedgerError):
            self.led.add_external_reservation("r1", "K", 6, 8)


if __name__ == "__main__":
    unittest.main()
