"""Persistence, auditability and immutability of subscription facts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from address_delivery.delivery import DeliveryStore
from address_delivery.service import AddressService
from address_delivery.subscription import (
    Scheduler,
    SplitEntry,
    SubscriptionLedger,
)
from address_delivery.subscription.rationals import Q

from support import MATERIALS


class PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "persist.db"

    def _build(self) -> AddressService:
        svc = AddressService(self.db)
        svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        return svc

    def _anchor_and_split(self, ledger: SubscriptionLedger, svc: AddressService) -> None:
        result = svc.submit_new("NP1", "C1", "东", "学园路", 2)
        self.assertEqual(result.outcome, "submitted")
        ledger.register_box("BX0", "S3", 2, "老户箱", 1)
        ledger.anchor_root("R1", "OLD", "NP1", "BX0")
        ledger.register_box("BXA", "S3", 2, "甲箱", 1)
        ledger.register_box("BXB", "S3", 2, "乙箱", 1)
        ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(2, 3), "BXA"),
             SplitEntry("OLD", "B", Q(1, 3), "BXB")],
        )

    def test_ledger_survives_reopen_and_keeps_historical_allocations(self):
        svc = self._build()
        ledger = SubscriptionLedger(svc.conn, svc.delivery)
        self._anchor_and_split(ledger, svc)
        plan_before = Scheduler(ledger).schedule([1, 2, 3])
        svc.close()

        svc = AddressService.open(self.db)
        try:
            ledger = SubscriptionLedger(svc.conn, svc.delivery)
            anchor = ledger.get_anchor("R1")
            self.assertEqual(anchor.parcel_id, "NP1")
            self.assertEqual(anchor.street_id, "S3")
            allocations = {a.household_id: a.share for a in ledger.allocations(1)}
            self.assertEqual(allocations, {"A": Q(2, 3), "B": Q(1, 3)})
            plan_after = Scheduler(ledger).schedule([1, 2, 3])
            self.assertEqual(
                [i.recipient for i in plan_after.issues],
                [i.recipient for i in plan_before.issues],
            )
            self.assertEqual(plan_after.max_deviation, plan_before.max_deviation)
        finally:
            svc.close()

    def test_later_batch_never_rewrites_earlier_period_or_departed_household(self):
        svc = self._build()
        ledger = SubscriptionLedger(svc.conn, svc.delivery)
        svc.submit_new("NP1", "C1", "东", "学园路", 2)
        ledger.register_box("BX0", "S3", 2, "老户箱", 1)
        ledger.anchor_root("R1", "OLD", "NP1", "BX0")
        ledger.register_box("BXA", "S3", 2, "甲箱", 1)
        ledger.register_box("BXB", "S3", 2, "乙箱", 1)
        ledger.apply_split_batch(
            "B1", 1,
            [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
             SplitEntry("OLD", "B", Q(1, 2), "BXB")],
        )
        # Period 2: A spins out C; period-1 rows must remain exactly as issued.
        ledger.register_box("BXC", "S3", 2, "丙箱", 1)
        ledger.apply_split_batch(
            "B2", 2,
            [SplitEntry("A", "A", Q(1, 2), "BXA"),
             SplitEntry("A", "C", Q(1, 2), "BXC")],
        )
        period1 = {a.household_id: a.share for a in ledger.allocations(1)}
        self.assertEqual(period1, {"A": Q(1, 2), "B": Q(1, 2)})
        period2 = {a.household_id: a.share for a in ledger.allocations(2)}
        self.assertEqual(period2, {"A": Q(1, 4), "B": Q(1, 2), "C": Q(1, 4)})
        svc.close()

    def test_ledger_tables_are_independent_of_the_catalog(self):
        # The subscription schema only needs a connection plus a delivery
        # store; no catalog table is touched by the ledger itself.
        from address_delivery.db import connect

        conn = connect(":memory:")
        try:
            store = DeliveryStore(conn)
            ledger = SubscriptionLedger(conn, store)
            ledger.register_box("BX0", "S1", 1, "裸连接箱", 1)
            self.assertEqual(ledger.box_at("BX0", 1).box_label, "裸连接箱")
        finally:
            conn.close()

    def test_provenance_is_independently_queryable_per_period(self):
        svc = self._build()
        ledger = SubscriptionLedger(svc.conn, svc.delivery)
        self._anchor_and_split(ledger, svc)
        for allocation in ledger.entitlement_table([1, 2]):
            if allocation.household_id != "A":
                continue
            self.assertEqual(allocation.provenance, (("R1", Q(2, 3)),))
            self.assertEqual(allocation.batch_id, "B1")
        svc.close()


if __name__ == "__main__":
    unittest.main()
