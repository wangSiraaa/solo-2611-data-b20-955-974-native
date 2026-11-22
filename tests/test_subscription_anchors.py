"""Origin anchoring: successful parcels only, frozen facts, point-in-time boxes."""
from __future__ import annotations

from address_delivery.subscription.errors import AnchorError, CalendarError
from address_delivery.subscription.rationals import Q

from support_subscription import SubscriptionCase


class OriginAnchorTests(SubscriptionCase):
    def test_root_anchors_on_a_real_successful_parcel(self):
        self.anchor_s3_root()
        anchor = self.ledger.get_anchor("R1")
        self.assertEqual(anchor.parcel_id, "NP1")
        self.assertEqual(anchor.street_id, "S3")
        self.assertEqual(anchor.house_no, 2)
        self.assertEqual(anchor.box_id, "BX0")
        self.assertEqual(anchor.initial_share, Q(1))
        self.assertEqual(anchor.parcel_outcome, "submitted")
        # Evidence carries the frozen identity and the original sender text.
        self.assertEqual(anchor.evidence["original_text"]["street_text"], "学园路")
        self.assertEqual(anchor.evidence["box_registration"]["box_label"], "老合订门户箱")

    def test_failed_or_unknown_parcel_cannot_anchor(self):
        # P-style ambiguous parcel never produces a job at r1.
        result = self.submit_parcel("AMB", "东", "东路", 9)
        self.assertEqual(result.outcome, "failed_station_ambiguous")
        self.ledger.register_box("BX0", "S1", 9, "歧义门牌箱", 1)
        with self.assertRaises(AnchorError):
            self.ledger.anchor_root("R1", "OLD", "AMB", "BX0")
        with self.assertRaises(AnchorError):
            self.ledger.anchor_root("R2", "OLD", "NEVER_SENT", "BX0")

    def test_box_must_match_the_frozen_street_and_house(self):
        self.submit_parcel("NP1", "东", "学园路", 2)
        self.ledger.register_box("BX0", "S3", 2, "老合订门户箱", 1)
        self.ledger.register_box("OTHER", "S1", 9, "别处的箱", 1)
        with self.assertRaises(AnchorError):
            self.ledger.anchor_root("R1", "OLD", "NP1", "OTHER")

    def test_anchor_uses_the_registration_current_at_freeze_time_not_today(self):
        # The parcel freezes S3#2 at catalog version 1.
        self.submit_parcel("NP1", "东", "学园路", 2)
        self.ledger.register_box("BX0", "S3", 2, "期初具名门箱", 1)
        self.ledger.anchor_root("R1", "OLD", "NP1", "BX0")
        anchor = self.ledger.get_anchor("R1")
        self.assertEqual(anchor.evidence["box_registration"]["box_label"], "期初具名门箱")

        # A later re-registration (e.g. the box is renamed next year) must not
        # rewrite the historical anchor's evidence.
        self.ledger.register_box("BX0", "S3", 2, "明年改名门箱", 2)
        again = self.ledger.get_anchor("R1")
        self.assertEqual(
            again.evidence["box_registration"]["box_label"], "期初具名门箱"
        )
        # Point-in-time lookup: the new label does not exist at version 1, the
        # historical label is gone at version 2.
        self.assertEqual(self.ledger.box_at("BX0", 1).box_label, "期初具名门箱")
        self.assertEqual(self.ledger.box_at("BX0", 2).box_label, "明年改名门箱")
        self.assertEqual(
            self.ledger.box_at("BX0", 1).effective_to_version, 1
        )

    def test_anchor_does_not_follow_todays_same_name_street_coverage(self):
        """The trap: S1/S2 share the text 东路; coverage must not re-derive origin.

        The parcel commits through S1 at r2 (after K3 removal).  Even though S2
        also displays the street text 东路 and later gets deactivated, the
        anchor keeps the frozen stable street S1 and never consults current
        coverage to re-guess the household.
        """
        self.apply_r2()
        result = self.submit_parcel("NP9", "东", "东路", 9)
        self.assertEqual(result.outcome, "submitted")
        self.assertEqual(result.job["street_id"], "S1")
        frozen_version = result.job["catalog_version"]
        self.ledger.register_box("BX9", "S1", 9, "东路九号箱", frozen_version)
        self.ledger.anchor_root("R9", "H9", "NP9", "BX9")
        anchor = self.ledger.get_anchor("R9")
        self.assertEqual(anchor.street_id, "S1")
        self.assertEqual(anchor.catalog_version, frozen_version)
        # Replaying the frozen parcel still returns S1 although the same-name
        # S2 is now inactive; the anchor never re-resolves through coverage.
        replay = self.svc.replay_submitted("NP9")
        self.assertEqual(replay.job["street_id"], "S1")
        self.assertEqual(replay.outcome, "replayed")

    def test_box_without_registration_at_freeze_version_is_rejected(self):
        self.submit_parcel("NP1", "东", "学园路", 2)  # freezes at version 1
        self.ledger.register_box("LATE", "S3", 2, "事后才登记的箱", 2)
        with self.assertRaises(CalendarError):
            self.ledger.anchor_root("R1", "OLD", "NP1", "LATE")


if __name__ == "__main__":
    unittest.main()
