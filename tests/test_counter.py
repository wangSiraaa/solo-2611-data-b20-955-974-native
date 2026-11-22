"""Tests for segmentable counter operations, seals and live pickup slips."""
from __future__ import annotations

import unittest

from address_delivery import CounterSession, POLICY_CYCLE, POLICY_STEP, ReturnCabinet
from address_delivery.counter import EVENT_MOVE_BEGUN, EVENT_MOVE_DONE, EVENT_PICKUP_PENDING
from address_delivery.renumbering import Door

CELLS = ["1", "2", "3", "T"]
CHANNELS = [(a, b) for a in CELLS for b in CELLS if a != b]
PLACEMENT = {"甲": "1", "乙": "2", "丙": "3"}
# After the 1->2, 2->3, 3->1 renumbering the cabinet targets are:
TARGETS = {"甲": "2", "乙": "3", "丙": "1"}
FINAL_DOORS = {"甲": ("S", 2), "乙": ("S", 3), "丙": ("S", 1)}


class StubLedger:
    """Minimal ledger implementing the only method the counter calls."""

    def __init__(self, issued):
        self._issued = dict(issued)

    def verify_claim(self, parcel_id, street_id, house_no):
        door = self._issued.get(parcel_id)
        base = {"parcel_id": parcel_id,
                "claimed": {"street_id": street_id, "house_no": house_no},
                "issued": (None if door is None
                           else {"parcel_id": parcel_id, "street_id": door[0],
                                 "house_no": door[1]})}
        if door == (street_id, house_no):
            return {**base, "valid": True, "reason": "matches_issued_door"}
        return {**base, "valid": False,
                "reason": "door_mismatch" if door else "no_issued_door"}


def make_session(*, policy=POLICY_STEP, max_pending=None,
                 placement=PLACEMENT, targets=TARGETS, cells=CELLS,
                 channels=CHANNELS, issued=FINAL_DOORS):
    cabinet = ReturnCabinet(cells, channels, dict(placement), dict(targets))
    return CounterSession(cabinet, StubLedger(issued),
                          sealing_policy=policy, max_pending=max_pending)


class UninterruptedMoveTests(unittest.TestCase):
    def test_step_policy_finishes_the_ring_in_four_moves(self):
        session = make_session()
        result = session.run_moves()
        self.assertTrue(session.cabinet.is_complete())
        self.assertEqual(session.completed_moves, 4)
        self.assertEqual(result["plan"]["total_moves"], 0)
        kinds = [e["type"] for e in session.events]
        self.assertEqual(kinds.count(EVENT_MOVE_BEGUN), 4)
        self.assertEqual(kinds.count(EVENT_MOVE_DONE), 4)

    def test_moves_are_segmentable_and_reflect_in_hand_state(self):
        session = make_session()
        begun = session.begin_move("丙", "T")
        self.assertEqual(session.letter_state("丙"), "in_hand")
        self.assertEqual(session.letter_state("甲"), "in_cell")
        self.assertEqual(begun["state"], "in_hand")
        # A second carry cannot start while a letter is in hand.
        with self.assertRaises(Exception):
            session.begin_move("乙", "3")
        session.complete_move()
        self.assertIsNone(session.in_hand)
        self.assertEqual(session.cabinet.cell_of("丙"), "T")

    def test_old_pickup_label_cannot_hand_over_a_moved_letter_twice(self):
        session = make_session()
        session.begin_move("甲", "T")
        session.complete_move()
        # The old door number (1) is no longer a valid claim for 甲.
        rejected = session.request_pickup("甲", "S", 1)
        self.assertFalse(rejected["valid"])
        self.assertEqual(rejected["reason"], "door_mismatch")
        # ... and even a valid claim, once served, cannot be replayed.
        served = session.request_pickup("甲", "S", 2)
        self.assertEqual(served["action"], "served")
        again = session.request_pickup("甲", "S", 2)
        self.assertEqual(again["reason"], "already_taken")
        self.assertEqual(session.letter_state("甲"), "taken")


class LivePickupTests(unittest.TestCase):
    def test_pickup_after_first_step_is_served_from_T_and_replans_to_two(self):
        session = make_session()
        session.begin_move("甲", "T")
        session.complete_move()
        served = session.request_pickup("甲", "S", 2)
        self.assertEqual(served["action"], "served")
        self.assertEqual(served["from_cell"], "T")
        self.assertEqual(served["completed_moves_on_ledger"], 1)
        remaining = session.remaining_plan()
        self.assertEqual(remaining.total_moves, 2)
        self.assertEqual({(m.parcel_id, m.from_cell, m.to_cell) for m in remaining.moves},
                         {("丙", "3", "1"), ("乙", "2", "3")})
        session.run_moves()
        self.assertEqual(session.completed_moves, 3)
        self.assertTrue(session.cabinet.is_complete())

    def test_cap_one_never_holds_more_than_one_sealed_letter(self):
        session = make_session(max_pending=1)
        session.begin_move("甲", "T")
        seals = session.face()["active_seals"]
        self.assertEqual(len(seals), 1)
        self.assertEqual(seals[0]["parcels"], ["甲"])
        session.complete_move()
        # The per-step seal opened and closed around the single carry.
        self.assertEqual(session.face()["active_seals"], [])

    def test_cycle_seal_queues_the_claim_and_shows_the_waiting_cost(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=3)
        session.begin_move("丙", "T")  # opens the ring seal
        pending = session.request_pickup("甲", "S", 2)
        self.assertEqual(pending["action"], "pending")
        self.assertEqual(pending["seal_id"], "cycle-seal-1")
        self.assertTrue(session.is_sealed("甲"))
        session.complete_move()
        session.run_moves()
        summary = session.pickup_summary()
        self.assertEqual(summary["served_count"], 1)
        served = summary["served"][0]
        # Delivered at unseal from the real final cell 2, after four carries.
        self.assertEqual(served["from_cell"], "2")
        self.assertEqual(served["waited_moves"], 4)
        self.assertEqual(summary["total_waiting_moves"], 4)
        self.assertEqual(session.completed_moves, 4)

    def test_cycle_policy_falls_back_to_step_seals_under_cap_one(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=1)
        session.begin_move("丙", "T")
        self.assertEqual(session.face()["active_seals"][0]["kind"], "step")
        session.complete_move()
        served = session.request_pickup("甲", "S", 2)
        self.assertEqual(served["action"], "served")  # not queued
        session.run_moves()
        self.assertEqual(session.completed_moves, 3)

    def test_manual_cycle_closure_validates_the_ring(self):
        session = make_session()
        # Not a cycle: cell 2 is not part of the group.
        with self.assertRaises(Exception):
            session.open_cycle_closure(["甲"])
        seal = session.open_cycle_closure(["甲", "乙", "丙"])
        self.assertEqual(set(seal["parcels"]), {"甲", "乙", "丙"})
        with self.assertRaises(Exception):
            session.close_cycle_closure(seal["seal_id"])  # ring unfinished
        session.run_moves()
        self.assertNotIn(seal["seal_id"], session.face()["active_seals"])

    def test_valid_claim_on_the_in_hand_letter_is_served_from_the_hand(self):
        session = make_session()
        session.begin_move("甲", "T")
        self.assertEqual(session.letter_state("甲"), "in_hand")
        served = session.request_pickup("甲", "S", 2)
        self.assertEqual(served["action"], "served")
        self.assertTrue(served["in_hand"])
        self.assertIsNone(session.in_hand)
        self.assertEqual(session.letter_state("甲"), "taken")
        # The interrupted carry did not complete; the two remaining letters
        # settle in two carries and the cancelled step is not counted.
        self.assertEqual(session.remaining_plan().total_moves, 2)
        session.run_moves()
        self.assertEqual(session.completed_moves, 2)
        self.assertTrue(session.cabinet.is_complete())

    def test_unrelated_open_cell_letter_is_served_while_another_is_in_hand(self):
        cells = CELLS + ["X"]
        channels = CHANNELS + [("X", c) for c in CELLS] + [(c, "X") for c in CELLS]
        placement = {**PLACEMENT, "丁": "X"}
        targets = {**TARGETS, "丁": "X"}
        issued = {**FINAL_DOORS, "丁": ("S", 9)}
        session = make_session(cells=cells, channels=channels,
                               placement=placement, targets=targets, issued=issued)
        session.begin_move("甲", "T")
        served = session.request_pickup("丁", "S", 9)
        self.assertEqual(served["action"], "served")
        self.assertEqual(served["from_cell"], "X")
        self.assertEqual(session.in_hand, "甲")  # the carry continues
        session.complete_move()

    def test_pending_request_is_delivered_from_the_actual_cell_not_a_guess(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=3)
        session.begin_move("丙", "T")
        session.request_pickup("乙", "S", 3)
        session.complete_move()
        session.run_moves()
        served = next(s for s in session.pickup_summary()["served"]
                      if s["parcel_id"] == "乙")
        self.assertEqual(served["from_cell"], "3")
        self.assertFalse(served["immediate"])
        self.assertGreater(served["waited_moves"], 0)

    def test_replan_after_pickup_is_booked_with_completed_steps_kept(self):
        session = make_session()
        plan0 = session.remaining_plan()
        self.assertEqual(plan0.total_moves, 4)
        session.begin_move("甲", "T")
        session.complete_move()
        session.request_pickup("甲", "S", 2)  # removes one letter from the job
        replans = [e for e in session.events if e["type"] == "replanned"]
        self.assertEqual(len(replans), 1)
        self.assertEqual(replans[0]["detail"]["remaining_moves_after"], 2)
        # Steps already done stay on the ledger honestly.
        self.assertEqual(session.completed_moves, 1)
        session.run_moves()
        self.assertEqual(session.completed_moves, 3)
        self.assertEqual(session.pickup_summary()["replan_count"], 1)

    def test_pickup_for_unknown_or_absent_parcel_is_rejected_without_side_effects(self):
        session = make_session()
        self.assertEqual(session.request_pickup("鬼", "S", 1)["reason"], "no_issued_door")
        before = session.completed_moves
        session.request_pickup("鬼", "S", 1)
        self.assertEqual(session.completed_moves, before)

    def test_duplicate_slip_while_sealed_is_rejected_not_queued_twice(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=3)
        session.begin_move("丙", "T")
        first = session.request_pickup("甲", "S", 2)
        second = session.request_pickup("甲", "S", 2)
        self.assertEqual(first["action"], "pending")
        self.assertEqual(second["action"], "rejected")
        self.assertEqual(second["reason"], "pickup_already_pending")
        self.assertEqual(len(session.face()["pending_pickups"]), 1)
        session.complete_move()
        session.run_moves()
        self.assertEqual(session.pickup_summary()["served_count"], 1)

    def test_cycle_larger_than_the_cap_is_refused_and_step_policy_used(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=2)
        session.begin_move("丙", "T")  # ring of 3 does not fit the cap
        self.assertEqual(session.face()["active_seals"][0]["kind"], "step")
        manual = make_session(max_pending=2)
        with self.assertRaises(Exception):
            manual.open_cycle_closure(["甲", "乙", "丙"])

    def test_letter_outside_the_sealed_ring_stays_deliverable_on_the_spot(self):
        cells = CELLS + ["X"]
        channels = CHANNELS + [("X", c) for c in CELLS] + [(c, "X") for c in CELLS]
        placement = {**PLACEMENT, "丁": "X"}
        targets = {**TARGETS, "丁": "X"}
        issued = {**FINAL_DOORS, "丁": ("S", 9)}
        session = make_session(policy=POLICY_CYCLE, max_pending=3, cells=cells,
                               channels=channels, placement=placement,
                               targets=targets, issued=issued)
        session.begin_move("丙", "T")  # ring of 甲乙丙 sealed; 丁 is outside
        self.assertTrue(session.is_sealed("甲"))
        served = session.request_pickup("丁", "S", 9)
        self.assertEqual(served["action"], "served")
        self.assertEqual(served["from_cell"], "X")
        session.complete_move()
        session.run_moves()
        self.assertTrue(session.cabinet.is_complete())

    def test_snapshot_restore_keeps_letter_states_pending_and_ledger(self):
        session = make_session(policy=POLICY_CYCLE, max_pending=3)
        session.begin_move("丙", "T")
        session.request_pickup("甲", "S", 2)
        session.complete_move()
        data = session.snapshot()
        restored = CounterSession.restore(data, StubLedger(FINAL_DOORS))
        self.assertEqual(restored.completed_moves, 1)
        self.assertEqual(restored.letter_state("甲"), "in_cell")
        self.assertEqual(restored.face()["pending_pickups"][0]["parcel_id"], "甲")
        restored.run_moves()
        self.assertEqual(restored.pickup_summary()["served"][0]["from_cell"], "2")


class NarrativeScenarioTests(unittest.TestCase):
    def test_identity_renumber_rejects_new_number_claim_even_with_letter_present(self):
        # When the renumbering keeps the original numbers, 甲 stays at door 1;
        # a slip naming the would-be new number 2 must be refused.
        identity_targets = {"甲": "1", "乙": "2", "丙": "3"}
        identity_doors = {"甲": ("S", 1), "乙": ("S", 2), "丙": ("S", 3)}
        session = make_session(targets=identity_targets, issued=identity_doors)
        self.assertEqual(session.remaining_plan().total_moves, 0)
        verdict = session.request_pickup("甲", "S", 2)
        self.assertFalse(verdict["valid"])
        self.assertEqual(verdict["reason"], "door_mismatch")
        good = session.request_pickup("甲", "S", 1)
        self.assertEqual(good["action"], "served")

    def test_full_interrupt_story_matches_the_spec_step_account(self):
        session = make_session(max_pending=1)
        # First carry: original door-1 letter 甲 goes into the empty T cell.
        session.begin_move("甲", "T")
        session.complete_move()
        # New-2 claim is valid at this moment; old-1 claim is not.
        self.assertEqual(session.request_pickup("甲", "S", 2)["action"], "served")
        self.assertFalse(session.request_pickup("甲", "S", 1)["valid"])
        # Two remaining letters settle in two more carries; total is three.
        session.run_moves()
        self.assertEqual(session.completed_moves, 3)
        self.assertTrue(session.cabinet.is_complete())


if __name__ == "__main__":
    unittest.main()
