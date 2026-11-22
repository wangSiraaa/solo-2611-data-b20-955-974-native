"""Scheduler tests: optimal max-deviation, closures, shared boxes, replanning."""
from __future__ import annotations

from address_delivery.subscription import Scheduler, SplitEntry
from address_delivery.subscription.errors import SchedulingError
from address_delivery.subscription.rationals import Q

from support_subscription import SubscriptionCase


class CanonicalCasesTests(SubscriptionCase):
    """The three worked cases from the problem statement, run on real parcels."""

    def plan(self, ratio_a, closures=None):
        self.canonical_split(ratio_a)
        for period, box in (closures or {}).items():
            self.ledger.register_closure(period, box)
        return Scheduler(self.ledger).schedule([1, 2, 3])

    def test_two_to_one_open_boxes_peak_one_third(self):
        plan = self.plan(Q(2, 3))
        self.assertEqual([i.recipient for i in plan.issues], ["A", "B", "A"])
        self.assertEqual(plan.max_deviation, Q(1, 3))
        self.assertEqual(plan.blocked_periods, ())
        # Every issue dispatches exactly one paper to an open, positive-share box.
        for issue in plan.issues:
            self.assertIsNotNone(issue.box_id)
            self.assertNotIn(issue.box_id, issue.closed_boxes)

    def test_equal_split_peak_one_half(self):
        plan = self.plan(Q(1, 2))
        self.assertEqual(plan.max_deviation, Q(1, 2))
        self.assertEqual(
            [i.recipient for i in plan.issues], ["A", "B", "A"]
        )

    def test_two_to_one_with_b_closed_in_period_two_peak_two_thirds(self):
        plan = self.plan(Q(2, 3), closures={2: "BXB"})
        self.assertEqual(plan.max_deviation, Q(2, 3))
        # B is excluded only in period 2; its entitlement is still recorded.
        self.assertEqual(plan.issues[1].recipient, "A")
        self.assertNotIn("B", plan.issues[1].eligible)
        self.assertEqual(plan.issues[1].closed_boxes, ("BXB",))
        # Closed-box households keep full period entitlement on the ledger.
        entitlements = {
            a.household_id: a.share
            for a in self.ledger.entitlement_table([2]) if a.period == 2
        }
        self.assertEqual(entitlements["B"], Q(1, 3))

    def test_balances_are_cumulative_received_minus_entitlement(self):
        plan = self.plan(Q(2, 3))
        first = plan.issues[0].balances
        self.assertEqual(first["A"], Q(1, 3))   # 1 received - 2/3 due
        self.assertEqual(first["B"], -Q(1, 3))  # 0 received - 1/3 due
        last = plan.issues[-1].balances
        self.assertEqual(last, {"A": Q(0), "B": Q(0)})


class EligibilityTests(SubscriptionCase):
    def test_period_with_no_eligible_household_is_unschedulable(self):
        self.canonical_split()
        # Close both boxes in period 2: nobody can take the unique paper.
        self.ledger.register_closure(2, "BXA")
        self.ledger.register_closure(2, "BXB")
        plan = Scheduler(self.ledger).schedule([1, 2, 3])
        self.assertEqual(plan.blocked_periods, (2,))
        blocked = plan.issues[1]
        self.assertIsNone(blocked.recipient)
        self.assertFalse(blocked.schedulable)
        self.assertEqual(blocked.eligible, ())
        # The surrounding issues still dispatch normally.
        self.assertEqual(plan.issues[0].recipient, "A")
        self.assertIsNotNone(plan.issues[2].recipient)

    def test_zero_entitlement_household_cannot_receive(self):
        # Root stays on OLD (no split); A exists only as a registered box.
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "空份额箱", 1)
        self.ledger.ensure_baseline_period()
        plan = Scheduler(self.ledger).schedule([1, 2])
        self.assertEqual(plan.households, ("OLD",))
        self.assertTrue(all(i.recipient == "OLD" for i in plan.issues))


class SharedBoxTests(SubscriptionCase):
    def test_two_households_sharing_one_box_hold_shares_separately(self):
        self.anchor_s3_root()
        # Both split households physically use the same box id BXS.
        self.ledger.register_box("BXS", "S3", 2, "合一门箱", 1)
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(2, 3), "BXS"),
             SplitEntry("OLD", "B", Q(1, 3), "BXS")],
        )
        allocations = {a.household_id: a for a in self.ledger.allocations(1)}
        self.assertEqual(allocations["A"].box_id, "BXS")
        self.assertEqual(allocations["B"].box_id, "BXS")
        # Both are separate recipients even though the box id is identical.
        plan = Scheduler(self.ledger).schedule([1, 2, 3])
        self.assertEqual(set(plan.households), {"A", "B"})
        self.assertEqual(plan.max_deviation, Q(1, 3))

    def test_closing_the_shared_box_excludes_both_households(self):
        self.anchor_s3_root()
        self.ledger.register_box("BXS", "S3", 2, "合一门箱", 1)
        self.ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(1, 2), "BXS"),
             SplitEntry("OLD", "B", Q(1, 2), "BXS")],
        )
        self.ledger.register_closure(1, "BXS")
        plan = Scheduler(self.ledger).schedule([1, 2, 3])
        self.assertEqual(plan.blocked_periods, (1,))
        self.assertIsNone(plan.issues[0].recipient)
        # Entitlements for the closed issue are unaffected.
        first_shares = {
            a.household_id: a.share
            for a in self.ledger.entitlement_table([1]) if a.period == 1
        }
        self.assertEqual(first_shares, {"A": Q(1, 2), "B": Q(1, 2)})


class ReplanTests(SubscriptionCase):
    def test_issued_issues_are_frozen_while_future_is_reoptimised(self):
        self.canonical_split()
        scheduler = Scheduler(self.ledger)
        # Period 1 sample issue was actually given to B (suboptimal prefix).
        plan = scheduler.replan(
            [1, 2, 3], issued_through=1, forced_recipients={1: "B"}
        )
        self.assertEqual(plan.issues[0].recipient, "B")
        self.assertEqual(plan.issued_through, 1)
        # The remaining issues are chosen optimally given the frozen start.
        self.assertIsNotNone(plan.issues[1].recipient)
        self.assertIsNotNone(plan.issues[2].recipient)
        for issue in plan.issues[:1]:
            self.assertEqual(issue.balances["B"], Q(2, 3))

    def test_replan_rejects_a_forced_recipient_that_is_not_eligible(self):
        self.canonical_split()
        self.ledger.register_closure(1, "BXB")
        scheduler = Scheduler(self.ledger)
        with self.assertRaises(SchedulingError):
            scheduler.replan(
                [1, 2, 3], issued_through=1, forced_recipients={1: "B"}
            )

    def test_horizon_bounds_are_enforced(self):
        self.canonical_split()
        scheduler = Scheduler(self.ledger)
        with self.assertRaises(SchedulingError):
            scheduler.schedule(list(range(1, 32)))
        with self.assertRaises(SchedulingError):
            scheduler.schedule([2, 3])


if __name__ == "__main__":
    unittest.main()
