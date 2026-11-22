"""Tests for the house-number renumbering ledger (max three door books)."""
from __future__ import annotations

import unittest

from support import fresh_synthetic_service

from address_delivery import RenumberingError
from address_delivery.renumbering import Door, RenumberingStore, RenumberStrip, apply_publication


def strip(strip_id, old, lo, hi, new_first, step, *, old_street="S1",
          new_street="S1", parity="any"):
    return {
        "strip_id": strip_id, "old_street_id": old_street, "old_lo": lo,
        "old_hi": hi, "parity": parity, "new_street_id": new_street,
        "new_first": new_first, "new_step": step,
    }


class RenumberingLedgerTests(unittest.TestCase):
    def setUp(self):
        self.svc = fresh_synthetic_service()  # streets S1..S4 exist
        self.ledger = self.svc.renumbering

    def test_initial_door_list_is_stored_and_listed_sorted(self):
        self.ledger.register_initial_era([("S1", 3), ("S1", 1), ("S4", 2)])
        doors = self.ledger.registered_doors(0)
        self.assertEqual([(d.street_id, d.house_no) for d in doors],
                         [("S1", 1), ("S1", 3), ("S4", 2)])

    def test_duplicate_initial_doors_are_rejected(self):
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.register_initial_era([Door("S1", 1), Door("S1", 1)])
        self.assertEqual(ctx.exception.code, "duplicate_door")

    def test_unknown_street_cannot_register_or_publish(self):
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.register_initial_era([Door("NOPE", 1)])
        self.assertEqual(ctx.exception.code, "unknown_street")
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 2)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([strip("X", "S1", 1, 2, 9, 1, new_street="GHOST")])
        self.assertEqual(ctx.exception.code, "unknown_street")
        # A rejected publication creates no era.
        self.assertEqual(self.ledger.current_era_seq(), 0)

    def test_three_cycle_publication_maps_one_to_one_and_others_carry_over(self):
        self.ledger.register_initial_era([Door("S1", n) for n in (1, 2, 3, 9)])
        self.ledger.publish([strip("R", "S1", 1, 3, 2, 1)])
        doors = {(d.street_id, d.house_no) for d in self.ledger.registered_doors(1)}
        self.assertEqual(doors, {("S1", 2), ("S1", 3), ("S1", 4), ("S1", 9)})

    def test_parity_matches_only_the_requested_numbers(self):
        self.ledger.register_initial_era([Door("S1", n) for n in range(1, 6)])
        self.ledger.publish([strip("O", "S1", 1, 5, 101, 10, parity="odd")])
        doors = {(d.street_id, d.house_no) for d in self.ledger.registered_doors(1)}
        # 1,3,5 -> 101,111,121; evens 2,4 carry over
        self.assertEqual(doors, {("S1", 101), ("S1", 111), ("S1", 121),
                                 ("S1", 2), ("S1", 4)})

    def test_negative_step_is_allowed_but_targets_must_stay_positive(self):
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 2), Door("S1", 3)])
        self.ledger.publish([strip("R", "S1", 1, 3, 3, -1)])
        self.assertEqual({(d.street_id, d.house_no) for d in self.ledger.registered_doors(1)},
                         {("S1", 3), ("S1", 2), ("S1", 1)})
        # Sequence 2,1,0 reaches zero: rejected even though the step is fine.
        with self.assertRaises(RenumberingError) as ctx:
            RenumberStrip("bad", "S1", 1, 3, "any", "S1", 2, -1)
        self.assertEqual(ctx.exception.code, "target_not_positive")
        with self.assertRaises(RenumberingError) as ctx:
            RenumberStrip("zero", "S1", 1, 1, "any", "S1", 1, 0)
        self.assertEqual(ctx.exception.code, "zero_step")

    def test_zero_step_is_rejected(self):
        self.ledger.register_initial_era([Door("S1", 1)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([strip("Z", "S1", 1, 1, 5, 0)])
        self.assertEqual(ctx.exception.code, "zero_step")

    def test_overlapping_old_coverage_rejects_and_names_the_door(self):
        self.ledger.register_initial_era([Door("S1", n) for n in range(1, 6)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([
                strip("A", "S1", 1, 3, 10, 1),
                strip("B", "S1", 3, 5, 20, 1),
            ])
        self.assertEqual(ctx.exception.code, "overlapping_old_coverage")
        self.assertEqual(ctx.exception.conflicts[0]["door"], {"street_id": "S1", "house_no": 3})
        self.assertEqual(self.ledger.current_era_seq(), 0)

    def test_two_old_doors_on_one_new_door_reject(self):
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 2)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([
                strip("A", "S1", 1, 1, 7, 1),
                strip("B", "S1", 2, 2, 7, 1),
            ])
        self.assertEqual(ctx.exception.code, "duplicate_new_door")
        self.assertEqual(ctx.exception.conflicts[0]["new_door"],
                         {"street_id": "S1", "house_no": 7})

    def test_carry_over_collision_is_also_a_duplicate_new_door(self):
        # Door 5 carries over unchanged while strip A sends door 1 to 5.
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 5)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([strip("A", "S1", 1, 1, 5, 1)])
        self.assertEqual(ctx.exception.code, "duplicate_new_door")

    def test_three_eras_chain_without_reapplying_outputs_in_one_publication(self):
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 2)])
        self.ledger.publish([strip("e1", "S1", 1, 2, 10, 1)])
        # Era-1 numbers 10/11 are not valid *old* inputs for a strip in the
        # same publication, so this second publication must target them.
        self.ledger.publish([strip("e2", "S1", 10, 11, 100, 1)])
        trace = self.ledger.trace_door("S1", 1)
        self.assertEqual([(e.era_seq, e.house_no, e.applied_strip) for e in trace.eras],
                         [(0, 1, None), (1, 10, "e1"), (2, 100, "e2")])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([strip("e3", "S1", 1, 2, 9, 1)])
        self.assertEqual(ctx.exception.code, "too_many_eras")

    def test_trace_unknown_door_fails_reviewably(self):
        self.ledger.register_initial_era([Door("S1", 1)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.trace_door("S1", 404)
        self.assertEqual(ctx.exception.code, "door_not_registered")

    def test_issue_and_verify_claims_against_parcel_bound_final_doors(self):
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 2), Door("S1", 3)])
        self.ledger.publish([strip("R", "S1", 1, 3, 2, 1)])
        issued = self.ledger.issue_door("甲", "S1", 2)
        self.assertEqual(issued.house_no, 2)
        # Idempotent re-issue with the same door.
        self.assertEqual(self.ledger.issue_door("甲", "S1", 2).house_no, 2)
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.issue_door("甲", "S1", 3)
        self.assertEqual(ctx.exception.code, "issue_conflict")
        self.assertTrue(self.ledger.verify_claim("甲", "S1", 2)["valid"])
        old = self.ledger.verify_claim("甲", "S1", 1)
        self.assertFalse(old["valid"])
        self.assertEqual(old["reason"], "door_mismatch")
        self.assertEqual(self.ledger.verify_claim("鬼", "S1", 2)["reason"], "no_issued_door")
        with self.assertRaises(RenumberingError):
            self.ledger.issue_door("乙", "S1", 99)

    def test_issue_parcel_starts_from_the_frozen_job_only(self):
        self.ledger.register_initial_era([Door("S4", 2), Door("S4", 4)])
        self.ledger.publish([strip("R", "S4", 2, 4, 20, 2,
                                   old_street="S4", new_street="S4", parity="even")])
        # A real committed parcel through the delivery store.
        result = self.svc.submit_new("P9", "C1", "北区", "公园路", 2)
        self.assertEqual(result.outcome, "submitted")
        self.assertEqual(result.job["street_id"], "S4")
        issued = self.svc.issue_final_door("P9")
        self.assertEqual((issued.street_id, issued.house_no), ("S4", 20))
        with self.assertRaises(RenumberingError) as ctx:
            self.svc.issue_final_door("NOT-A-PARCEL")
        self.assertEqual(ctx.exception.code, "parcel_not_committed")

    def test_sequence_positions_are_kept_when_interval_has_unregistered_numbers(self):
        # Only door 4 is registered inside the even interval 2..4; the new
        # sequence still pairs by position, so 4 -> 22 (not 20).
        self.ledger.register_initial_era([Door("S4", 4), Door("S4", 9)])
        self.ledger.publish([strip("R", "S4", 2, 4, 20, 2,
                                   old_street="S4", new_street="S4", parity="even")])
        doors = {(d.street_id, d.house_no) for d in self.ledger.registered_doors(1)}
        self.assertEqual(doors, {("S4", 22), ("S4", 9)})
        self.assertEqual(self.ledger.trace_door("S4", 4).final.house_no, 22)

    def test_renumber_target_colliding_with_a_carried_over_door_is_rejected(self):
        self.ledger.register_initial_era([Door("S1", 1), Door("S1", 5)])
        with self.assertRaises(RenumberingError) as ctx:
            self.ledger.publish([strip("A", "S1", 1, 1, 5, 1)])
        self.assertEqual(ctx.exception.code, "duplicate_new_door")
        self.assertEqual(self.ledger.current_era_seq(), 0)

    def test_pure_apply_publication_reports_the_used_strip_per_door(self):
        old = {Door("S1", 1), Door("S1", 2)}
        new_doors, owner, mapping = apply_publication(
            old, [RenumberStrip("R", "S1", 1, 2, "any", "S1", 8, 1)])
        self.assertEqual(owner, {Door("S1", 1): "R", Door("S1", 2): "R"})
        self.assertEqual(mapping[Door("S1", 1)], Door("S1", 8))
        self.assertEqual(new_doors, {Door("S1", 8), Door("S1", 9)})

    def test_ledger_works_without_a_catalog_for_pure_door_rules(self):
        from address_delivery.db import connect

        conn = connect(":memory:")
        store = RenumberingStore(conn, catalog=None)
        store.register_initial_era([Door("X", 1)])
        store.publish([strip("R", "X", 1, 1, 2, 1, old_street="X", new_street="X")])
        self.assertEqual(store.trace_door("X", 1).final.house_no, 2)

if __name__ == "__main__":
    unittest.main()
