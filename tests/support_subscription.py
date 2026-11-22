"""Fixtures for the subscription-ledger tests.

A fresh in-memory service is loaded with the bundled fictional r1 catalog.
Tests submit real parcels through the normal delivery pipeline so subscription
roots always anchor on genuinely frozen jobs - never on hand-written rows.

Street/house layout (all synthetic):

* S3 学园路 2  -> station A, stable id S3 (newspapers' original compound)
* S1 东路 9    -> ambiguous at r1 (A/X), unique A at r2 after K3 is removed
* S2 东路 2    -> deactivated at r2 (same street text as S1, different district)
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import MATERIALS

from address_delivery.service import AddressService
from address_delivery.subscription import SubscriptionLedger, SplitEntry
from address_delivery.subscription.rationals import Q


class SubscriptionCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.svc = AddressService(Path(self.tmp.name) / "subscription.db")
        self.svc.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
        self.ledger = SubscriptionLedger(self.svc.conn, self.svc.delivery)

    # -- parcel helpers ------------------------------------------------------

    def submit_parcel(self, parcel_id: str, district: str, street: str, house: int):
        return self.svc.submit_new(parcel_id, "C1", district, street, house)

    def apply_r2(self) -> int:
        import json

        revision = json.loads((MATERIALS / "revision.json").read_text("utf-8"))
        return self.svc.apply_revision(revision)

    # -- box helpers ---------------------------------------------------------

    def register_s3_boxes(self) -> None:
        """Original compound box plus the two split households' own boxes."""
        self.ledger.register_box("BX0", "S3", 2, "老合订门户箱", 1)
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)

    def anchor_s3_root(self, root_id: str = "R1", household: str = "OLD") -> None:
        result = self.submit_parcel("NP1", "东", "学园路", 2)
        self.assertEqual(result.outcome, "submitted")
        self.ledger.register_box("BX0", "S3", 2, "老合订门户箱", 1)
        self.ledger.anchor_root(root_id, household, "NP1", "BX0", initial_share=Q(1))

    def canonical_split(self, ratio_a=Q(2, 3), batch_id: str = "B1") -> None:
        """The statement's opening example: one root split A:B before issue 1."""
        self.anchor_s3_root()
        self.ledger.register_box("BXA", "S3", 2, "甲户分箱", 1)
        self.ledger.register_box("BXB", "S3", 2, "乙户分箱", 1)
        self.ledger.apply_split_batch(
            batch_id,
            1,
            [
                SplitEntry("OLD", "A", ratio_a, "BXA"),
                SplitEntry("OLD", "B", Q(1) - ratio_a, "BXB"),
            ],
        )
