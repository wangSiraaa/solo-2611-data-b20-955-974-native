"""Tests for the return-mail cabinet and its minimum-move BFS planner."""
from __future__ import annotations

import unittest

from address_delivery import CabinetError, ReturnCabinet

CELLS = ["1", "2", "3", "T"]
FULL_CHANNELS = [(a, b) for a in CELLS for b in CELLS if a != b]
CYCLE_PLACEMENT = {"甲": "1", "乙": "2", "丙": "3"}
CYCLE_TARGETS = {"甲": "2", "乙": "3", "丙": "1"}


def cycle_cabinet(*, cells=CELLS, channels=FULL_CHANNELS,
                  placement=CYCLE_PLACEMENT, targets=CYCLE_TARGETS):
    return ReturnCabinet(cells, channels, dict(placement), dict(targets))


class CabinetConstructionTests(unittest.TestCase):
    def test_duplicate_target_cells_are_rejected(self):
        with self.assertRaises(CabinetError):
            ReturnCabinet(CELLS, FULL_CHANNELS, CYCLE_PLACEMENT,
                          {"甲": "2", "乙": "2", "丙": "1"})

    def test_placement_and_targets_must_cover_the_same_letters(self):
        with self.assertRaises(CabinetError):
            ReturnCabinet(CELLS, FULL_CHANNELS, {"甲": "1"}, {"甲": "1", "乙": "2"})

    def test_scale_caps_are_enforced(self):
        nine = [f"C{i}" for i in range(9)]
        with self.assertRaises(CabinetError):
            ReturnCabinet(nine, [], {}, {})
        with self.assertRaises(CabinetError):
            ReturnCabinet([f"C{i}" for i in range(7)], [],
                          {p: f"C{i}" for i, p in enumerate("甲乙丙丁戊己庚")},
                          {p: f"C{i}" for i, p in enumerate("甲乙丙丁戊己庚")})

    def test_unknown_cells_in_channels_placement_and_targets_are_rejected(self):
        with self.assertRaises(CabinetError):
            ReturnCabinet(CELLS, [("1", "Z")], CYCLE_PLACEMENT, CYCLE_TARGETS)
        with self.assertRaises(CabinetError):
            ReturnCabinet(CELLS, FULL_CHANNELS, {"甲": "Z"}, {"甲": "T"})
        with self.assertRaises(CabinetError):
            ReturnCabinet(CELLS, FULL_CHANNELS, {"甲": "1"}, {"甲": "Z"})


class MinimumMoveTests(unittest.TestCase):
    def test_three_cycle_with_one_empty_cell_needs_exactly_four_moves(self):
        plan = cycle_cabinet().plan_min_moves()
        self.assertTrue(plan.solvable)
        self.assertEqual(plan.total_moves, 4)
        # Every move enters an empty cell along an allowed channel.
        cabinet = cycle_cabinet()
        for move in plan.moves:
            self.assertIn(move.to_cell, move.empties_before)
            self.assertIn((move.from_cell, move.to_cell), cabinet.channels)
            cabinet.apply_move(move.parcel_id, move.to_cell)
        self.assertTrue(cabinet.is_complete())
        # Empty-cell records are consistent before/after each step.
        first = plan.moves[0]
        self.assertEqual(first.empties_before, ("T",))
        self.assertEqual(first.empties_after, ("3",))

    def test_identity_targets_need_zero_moves(self):
        plan = cycle_cabinet(targets=dict(CYCLE_PLACEMENT)).plan_min_moves()
        self.assertTrue(plan.solvable)
        self.assertEqual(plan.total_moves, 0)
        self.assertEqual(plan.moves, ())

    def test_sealed_empty_cell_makes_the_first_move_impossible(self):
        # Only the three occupied cells remain usable: no empty cell at all.
        cells = ["1", "2", "3"]
        channels = [(a, b) for a in cells for b in cells if a != b]
        plan = cycle_cabinet(cells=cells, channels=channels).plan_min_moves()
        self.assertFalse(plan.solvable)
        self.assertEqual(plan.reason, "no_empty_cell")
        self.assertEqual(plan.detail["empties"], [])

    def test_directed_channels_can_make_a_target_unreachable(self):
        # 1->T->2 is one way; 乙 at 2 can never reach cell 1.
        cabinet = ReturnCabinet(
            ["1", "2", "T"], [("1", "T"), ("T", "2"), ("2", "T")],
            {"甲": "1", "乙": "2"}, {"甲": "2", "乙": "1"})
        plan = cabinet.plan_min_moves()
        self.assertFalse(plan.solvable)
        self.assertEqual(plan.reason, "unreachable")
        blocked = {b["parcel_id"] for b in plan.detail["blocked_letters"]}
        self.assertEqual(blocked, {"乙"})

    def test_exhausted_bfs_reports_states_explored(self):
        # Cell c is a one-way sink: the second letter can rotate a->c to free
        # a, but nothing can ever leave c again, so its own target (cell a) is
        # unreachable through any sequence of legal carries.
        cabinet = ReturnCabinet(
            ["a", "b", "c"],
            [("a", "b"), ("b", "a"), ("a", "c"), ("b", "c")],
            {"甲": "a", "乙": "b"}, {"甲": "b", "乙": "a"})
        plan = cabinet.plan_min_moves()
        self.assertFalse(plan.solvable)
        self.assertEqual(plan.reason, "unreachable")
        self.assertGreater(plan.detail["states_explored"], 1)
        self.assertEqual(plan.detail["blocked_letters"], [])

    def test_simple_shift_into_empty_cell_is_one_move(self):
        cabinet = ReturnCabinet(["a", "b"], [("a", "b"), ("b", "a")],
                                {"甲": "a"}, {"甲": "b"})
        plan = cabinet.plan_min_moves()
        self.assertEqual(plan.total_moves, 1)
        self.assertEqual((plan.moves[0].parcel_id, plan.moves[0].from_cell,
                          plan.moves[0].to_cell), ("甲", "a", "b"))

    def test_apply_move_rejects_stacking_swap_and_missing_channel(self):
        cabinet = cycle_cabinet()
        with self.assertRaises(CabinetError):
            cabinet.apply_move("甲", "2")          # occupied
        with self.assertRaises(CabinetError):
            cabinet.apply_move("甲", "T-x")        # unknown cell
        with self.assertRaises(CabinetError):
            cabinet.apply_move("鬼", "T")          # unknown letter
        narrow = ReturnCabinet(CELLS, [("1", "2")], CYCLE_PLACEMENT, CYCLE_TARGETS)
        with self.assertRaises(CabinetError):
            narrow.apply_move("甲", "T")           # channel absent

    def test_face_query_is_retained_and_shows_targets_and_empties(self):
        cabinet = cycle_cabinet()
        face = cabinet.face()
        self.assertEqual(face["empties"], ["T"])
        at_one = next(row for row in face["cells_face"] if row["cell"] == "1")
        self.assertEqual(at_one["parcel_id"], "甲")
        self.assertEqual(at_one["target_of"], "丙")
        self.assertTrue(all(row["parcel_id"] or row["cell"] == "T"
                            for row in face["cells_face"]))

    def test_snapshot_and_restore_round_trip(self):
        cabinet = cycle_cabinet()
        cabinet.apply_move("丙", "T")
        restored = ReturnCabinet.restore(cabinet.snapshot())
        self.assertEqual(restored.placement, cabinet.placement)
        self.assertEqual(restored.targets, cabinet.targets)
        self.assertEqual(restored.channels, cabinet.channels)
        self.assertEqual(restored.plan_min_moves().total_moves,
                         cabinet.plan_min_moves().total_moves)


if __name__ == "__main__":
    unittest.main()
