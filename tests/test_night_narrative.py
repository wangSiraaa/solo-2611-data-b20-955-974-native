"""End-to-end night-shift narrative: frozen job -> building -> keys -> routes.

This mirrors the worked example in the task brief: one building with a staffed
door (S, 3 minutes each way) and a key side door (D, 1 minute each way), key K
held at counter H, closing at minute 8.  It follows the whole chain from a
successfully submitted delivery record through building recognition, the key
ledger and its cutoff sheet, both director reports, and the signing desk.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from support import MATERIALS

from address_delivery import (
    AddressService,
    Edge,
    Entrance,
    NightDesk,
    NightParcel,
    NightPlanner,
    StreetGraph,
)
from address_delivery.keys import STATUS_DISPUTED, STATUS_HELD, STATUS_IN_TRANSIT
from address_delivery.night import STEP_DELIVER


def small_graph() -> StreetGraph:
    return StreetGraph(
        [
            Edge("H", "D", 1), Edge("D", "H", 1),
            Edge("H", "S", 3), Edge("S", "H", 3),
        ]
    )


def entrances(*, staffed_open: bool = True, side_open: bool = True):
    return (
        Entrance(
            "eD", "B1", "D", "side", requires_key="K",
            windows=None if side_open else (),
        ),
        Entrance("eS", "B1", "S", "staffed", windows=None if staffed_open else ()),
    )


class NightNarrativeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / "night.db"
        self.svc = AddressService(str(self.db))
        self.svc.load_initial(
            MATERIALS / "catalog.json", MATERIALS / "coverage.json"
        )
        # One successfully submitted, frozen delivery record (P2 -> S3 -> A).
        result = self.svc.submit_new("P2", "C1", "同区", "学园路", 2)
        self.assertTrue(result.ok)
        # The building registry recognises the frozen street S3.
        self.svc.register_building(
            {"building_id": "B1", "street_id": "S3", "lo": 1, "hi": 20,
             "parity": "any", "station_id": "A"}
        )
        # Key K is handed from the master counter to H and receipted there.
        self.svc.keys.register_counter("总柜")
        self.svc.keys.register_counter("H")
        self.svc.keys.register_key("K", "总柜")
        self.svc.keys.record_handover("T1", "K", "总柜", "H", -5)
        self.svc.keys.record_receipt("T1", "K", "总柜", "H", -4)
        self.svc.keys.freeze_cutoff("night0", 0)

    def tearDown(self) -> None:
        self.svc.close()

    def scenario(self, **kwargs):
        return self.svc.night_scenario(
            "night0",
            graph=small_graph(),
            station="H",
            entrances=entrances(**kwargs),
            parcels=(NightParcel("P2", "B1"),),
            closing_minute=8,
            counter_nodes={"K": "H"},
        )

    def test_frozen_record_recognises_building(self):
        recognition = self.svc.recognize_building("P2")
        self.assertEqual(recognition["outcome"], "unique")
        self.assertEqual(recognition["building_id"], "B1")
        self.assertEqual(recognition["job"]["station_id"], "A")

    def test_cutoff_sheet_makes_k_borrowable_at_h(self):
        sheet = self.svc.keys.get_cutoff("night0")
        (entry,) = sheet["keys"]
        self.assertEqual(entry["key_code"], "K")
        self.assertEqual(entry["status"], STATUS_HELD)
        self.assertEqual(entry["holder"], "H")
        self.assertTrue(entry["borrowable"])

    def test_both_director_reports(self):
        planner = NightPlanner(self.scenario())
        shortest = planner.shortest_walk()
        least_keys = planner.least_key_minutes()
        self.assertEqual((shortest.walk_minutes, shortest.key_minutes_total), (2, 2))
        self.assertEqual((least_keys.walk_minutes, least_keys.key_minutes_total), (6, 0))

    def test_removing_receipt_leaves_key_in_transit_only_six_minute_route(self):
        # A fresh ledger where the handover exists but the receipt is withdrawn:
        # the key is in transit and therefore not borrowable.
        svc = AddressService(":memory:")
        try:
            svc.keys.register_counter("总柜")
            svc.keys.register_counter("H")
            svc.keys.register_key("K", "总柜")
            svc.keys.record_handover("T1", "K", "总柜", "H", -5)  # no receipt
            svc.keys.freeze_cutoff("night0", 0)
            ownership = svc.keys.ownership_at("K", 0)
            self.assertEqual(ownership.status, STATUS_IN_TRANSIT)
            self.assertFalse(ownership.borrowable)
            scenario = svc.night_scenario(
                "night0", graph=small_graph(), station="H", entrances=entrances(),
                parcels=(NightParcel("P2", "B1"),), closing_minute=8,
                counter_nodes={"K": "H"},
            )
            planner = NightPlanner(scenario)
            # Only the six-minute staffed-door round trip remains.
            self.assertEqual(planner.shortest_walk().walk_minutes, 6)
            self.assertEqual(planner.shortest_walk().key_minutes_total, 0)
        finally:
            svc.close()

    def test_closed_staffed_door_forces_two_minute_key_route(self):
        planner = NightPlanner(self.scenario(staffed_open=False))
        plan = planner.shortest_walk()
        self.assertEqual(plan.walk_minutes, 2)
        self.assertEqual(plan.key_minutes_total, 2)
        deliver = next(s for s in plan.steps if s.kind == STEP_DELIVER)
        self.assertEqual(deliver.detail["entrance_id"], "eD")

    def test_sign_first_route_then_reject_conflicting_second_signature(self):
        planner = NightPlanner(self.scenario())
        first = planner.shortest_walk()  # K occupied [0, 2)
        desk = NightDesk(self.svc.keys)
        signed = desk.sign(first)
        self.assertEqual(signed["outcome"], "signed")

        # Re-signing the same plan is idempotent: no second key interval.
        again = desk.sign(first)
        self.assertEqual(again["outcome"], "replayed")
        intervals = [
            (r.start_min, r.end_min) for r in self.svc.keys.reservations_for("K")
        ]
        self.assertEqual(intervals, [(0, 2)])

        # A different plan whose interval overlaps [0,2) is rejected and
        # occupies nothing.
        from address_delivery import RoutePlan

        clashing = RoutePlan(
            plan_id="plan-clash",
            steps=(),
            delivered=("P2",),
            key_intervals={"K": ((1, 3),)},
            walk_minutes=2,
            key_minutes_total=2,
            finish_minute=3,
        )
        rejected = desk.sign(clashing)
        self.assertEqual(rejected["outcome"], "rejected_conflict")
        # The rejected plan added no occupancy.
        self.assertEqual(
            [(r.start_min, r.end_min) for r in self.svc.keys.reservations_for("K")],
            [(0, 2)],
        )
        self.assertEqual(desk.signed_plan_ids(), [first.plan_id])

    def test_disputed_key_is_rejected_at_signing(self):
        svc = AddressService(":memory:")
        try:
            svc.keys.register_counter("总柜")
            svc.keys.register_counter("H")
            svc.keys.register_key("K", "总柜")
            # A receipt with no matching handover is a dispute.
            svc.keys.record_receipt("R1", "K", "总柜", "H", -2)
            svc.keys.freeze_cutoff("night0", 0)
            self.assertEqual(svc.keys.ownership_at("K", 0).status, STATUS_DISPUTED)
            g = small_graph()
            # With the staffed door open the no-key route still works...
            both = svc.night_scenario(
                "night0", graph=g, station="H", entrances=entrances(),
                parcels=(NightParcel("P2", "B1"),), closing_minute=8,
                counter_nodes={"K": "H"},
            )
            self.assertEqual(
                NightPlanner(both).shortest_walk().key_minutes_total, 0
            )
            # ...but a side-door-only night is infeasible while K is disputed...
            side_only = svc.night_scenario(
                "night0", graph=g, station="H",
                entrances=(Entrance("eD", "B1", "D", "side", requires_key="K"),),
                parcels=(NightParcel("P2", "B1"),), closing_minute=8,
                counter_nodes={"K": "H"},
            )
            self.assertFalse(NightPlanner(side_only).feasible())
            # ...and the desk rejects any plan that would occupy the disputed key.
            from address_delivery import RoutePlan

            demanding = RoutePlan(
                plan_id="plan-needs-disputed-key",
                steps=(), delivered=("P2",),
                key_intervals={"K": ((0, 2),)},
                walk_minutes=2, key_minutes_total=2, finish_minute=2,
            )
            outcome = NightDesk(svc.keys).sign(demanding)["outcome"]
            self.assertEqual(outcome, "rejected_not_borrowable")
        finally:
            svc.close()


if __name__ == "__main__":
    unittest.main()
