"""Split batches: one-level opening-snapshot distribution, merges, provenance."""
from __future__ import annotations

from address_delivery.subscription import SplitEntry
from address_delivery.subscription.errors import SplitError
from address_delivery.subscription.rationals import Q, frac_to_text

from support_subscription import SubscriptionCase


class SplitBatchTests(SubscriptionCase):
    def test_two_to_one_split_records_exact_shares_and_root_flow(self):
        self.canonical_split(Q(2, 3))
        allocations = {a.household_id: a for a in self.ledger.allocations(1)}
        self.assertEqual(allocations["A"].share, Q(2, 3))
        self.assertEqual(allocations["B"].share, Q(1, 3))
        self.assertEqual(allocations["A"].provenance_dict, {"R1": Q(2, 3)})
        self.assertEqual(allocations["B"].provenance_dict, {"R1": Q(1, 3)})
        self.assertEqual(allocations["A"].box_id, "BXA")
        self.assertEqual(allocations["B"].box_id, "BXB")

    def test_ratios_must_be_non_negative_and_sum_to_one(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)
        with self.assertRaises(SplitError):
            self.ledger.apply_split_batch(
                "BAD", 1,
                [SplitEntry("OLD", "A", Q(1, 3), "BXA"),
                 SplitEntry("OLD", "B", Q(1, 3), "BXB")],
            )
        with self.assertRaises(SplitError):
            self.ledger.apply_split_batch(
                "BAD2", 1,
                [SplitEntry("OLD", "A", Q(-1, 2), "BXA"),
                 SplitEntry("OLD", "B", Q(3, 2), "BXB")],
            )

    def test_unlisted_old_household_carries_forward_unchanged(self):
        # Two independent roots/households; only OLD is split, OTHER stays.
        self.anchor_s3_root("R1", "OLD")
        result = self.submit_parcel("NP2", "东", "学园路", 2)
        self.assertEqual(result.outcome, "submitted")
        self.ledger.register_box("BX2", "S3", 2, "另一老户箱", 1)
        self.ledger.anchor_root("R2", "OTHER", "NP2", "BX2", initial_share=Q(1))
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
             SplitEntry("OLD", "B", Q(1, 2), "BXB")],
        )
        allocations = {a.household_id: a for a in self.ledger.allocations(1)}
        self.assertEqual(allocations["OTHER"].share, Q(1))
        self.assertEqual(allocations["OTHER"].provenance_dict, {"R2": Q(1)})
        # Total share is conserved across the batch.
        self.assertEqual(
            sum((a.share for a in allocations.values()), Q(0)), Q(2)
        )

    def test_two_old_households_merge_into_one_new_household(self):
        self.anchor_s3_root("R1", "OLD1")
        result = self.submit_parcel("NP2", "东", "学园路", 2)
        self.ledger.register_box("BX2", "S3", 2, "老二户箱", 1)
        self.ledger.anchor_root("R2", "OLD2", "NP2", "BX2", initial_share=Q(1))
        self.ledger.register_box("BXM", "S3", 2, "合并户箱", 1)
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD1", "M", Q(1), "BXM"),
             SplitEntry("OLD2", "M", Q(1), "BXM")],
        )
        merged = next(iter(self.ledger.allocations(1)))
        self.assertEqual(merged.household_id, "M")
        self.assertEqual(merged.share, Q(2))
        self.assertEqual(
            merged.provenance_dict, {"R1": Q(1), "R2": Q(1)}
        )

    def test_new_household_may_keep_an_old_identifier(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        # OLD keeps its own id while spinning B out; same number is fine.
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "OLD", Q(2, 3), "BX0"),
             SplitEntry("OLD", "B", Q(1, 3), "BXA")],
        )
        allocations = {a.household_id: a for a in self.ledger.allocations(1)}
        self.assertEqual(allocations["OLD"].share, Q(2, 3))
        self.assertEqual(allocations["B"].share, Q(1, 3))

    def test_same_batch_proceeds_cannot_be_split_again(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)
        self.ledger.register_box("BXC", "S3", 2, "孙户箱", 1)
        # Serial nesting: B receives from OLD in this batch and is then split.
        with self.assertRaises(SplitError) as ctx:
            self.ledger.apply_split_batch(
                "CHAIN", 1,
                [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
                 SplitEntry("OLD", "B", Q(1, 2), "BXB"),
                 SplitEntry("B", "B", Q(1, 2), "BXB"),
                 SplitEntry("B", "C", Q(1, 2), "BXC")],
            )
        self.assertIn("B", str(ctx.exception))
        # Nothing was materialised: the failed batch leaves no period rows.
        self.assertEqual(self.ledger.allocated_periods(), [])

    def test_cross_entry_chain_is_rejected_even_when_the_household_exists(self):
        # Two genuine period-opening holders anchored on separate parcels.
        self.anchor_s3_root("R1", "OLD1")
        result = self.submit_parcel("NP2", "东", "学园路", 2)
        self.assertEqual(result.outcome, "submitted")
        self.ledger.register_box("BX2", "S3", 2, "老二户箱", 1)
        self.ledger.anchor_root("R2", "OLD2", "NP2", "BX2", initial_share=Q(1))
        for box in ("BXA", "BXB", "BXC"):
            self.ledger.register_box(box, "S3", 2, box, 1)
        # OLD2 really exists at the opening, yet it also receives OLD1's
        # proceeds in this same batch and is then re-split: a cross-entry chain.
        with self.assertRaises(SplitError) as ctx:
            self.ledger.apply_split_batch(
                "CHAIN", 1,
                [SplitEntry("OLD1", "OLD2", Q(1), "BX2"),
                 SplitEntry("OLD2", "OLD2", Q(1, 2), "BX2"),
                 SplitEntry("OLD2", "C", Q(1, 2), "BXC")],
            )
        self.assertIn("OLD2", str(ctx.exception))
        self.assertEqual(self.ledger.allocated_periods(), [])

    def test_self_retention_is_allowed_but_cross_receiving_then_splitting_is_not(self):
        # OLD keeping its own id (old == new) is retention and stays legal.
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.apply_split_batch(
            "KEEP", 1,
            [SplitEntry("OLD", "OLD", Q(3, 4), "BX0"),
             SplitEntry("OLD", "A", Q(1, 4), "BXA")],
        )
        kept = {a.household_id: a.share for a in self.ledger.allocations(1)}
        self.assertEqual(kept, {"OLD": Q(3, 4), "A": Q(1, 4)})

    def test_two_level_split_needs_two_period_batches_and_compounds_ratios(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)
        self.ledger.register_box("BXC", "S3", 2, "孙户箱", 1)
        # Period 1: OLD -> A,B half each.
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
             SplitEntry("OLD", "B", Q(1, 2), "BXB")],
        )
        # Period 2 opening: split B's period-1 holding (the snapshot, not new
        # same-batch proceeds) into B and C.
        self.ledger.apply_split_batch(
            "B2", 2,
            [SplitEntry("B", "B", Q(1, 2), "BXB"),
             SplitEntry("B", "C", Q(1, 2), "BXC")],
        )
        period2 = {a.household_id: a for a in self.ledger.allocations(2)}
        self.assertEqual(period2["A"].share, Q(1, 2))
        self.assertEqual(period2["B"].share, Q(1, 4))
        self.assertEqual(period2["C"].share, Q(1, 4))
        self.assertEqual(period2["C"].provenance_dict, {"R1": Q(1, 4)})
        # Period 1 history is untouched by the later batch.
        period1 = {a.household_id: a for a in self.ledger.allocations(1)}
        self.assertEqual(set(period1), {"A", "B"})
        self.assertEqual(period1["B"].share, Q(1, 2))

    def test_two_batches_cannot_share_an_effective_period(self):
        self.canonical_split()
        with self.assertRaises(SplitError):
            self.ledger.apply_split_batch(
                "B2", 1, [SplitEntry("A", "A2", Q(1), "BXA")]
            )

    def test_splitting_a_household_without_opening_share_is_rejected(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        with self.assertRaises(SplitError):
            self.ledger.apply_split_batch(
                "GHOST", 1, [SplitEntry("GHOST", "A", Q(1), "BXA")]
            )

    def test_entitlement_table_carries_unchanged_state_forward(self):
        self.canonical_split()
        table = self.ledger.entitlement_table([1, 2, 3])
        by_period = {p: [a for a in table if a.period == p] for p in (1, 2, 3)}
        for period in (1, 2, 3):
            shares = {a.household_id: a.share for a in by_period[period]}
            self.assertEqual(shares, {"A": Q(2, 3), "B": Q(1, 3)})

    def test_share_text_round_trips_as_exact_fraction(self):
        self.canonical_split()
        row = self.svc.conn.execute(
            "SELECT share_text FROM sub_allocations WHERE household_id='A'"
        ).fetchone()
        self.assertEqual(row["share_text"], frac_to_text(Q(2, 3)))


if __name__ == "__main__":
    unittest.main()
