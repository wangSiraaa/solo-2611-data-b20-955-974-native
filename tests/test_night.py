"""Night-shift planner: exhaustive routes, the two reports, budget and windows."""
from __future__ import annotations

import unittest

from address_delivery.errors import PlanningError
from address_delivery.night import (
    Edge,
    Entrance,
    NightParcel,
    NightPlanner,
    NightScenario,
    STEP_BORROW,
    STEP_DELIVER,
    STEP_RETURN,
    STEP_WAIT,
    STEP_WALK,
    StreetGraph,
)


def small_street_graph() -> StreetGraph:
    # H is the station/value-counter; D the side door (1 min each way);
    # S the same building's staffed door (3 min each way).
    return StreetGraph(
        [
            Edge("H", "D", 1),
            Edge("D", "H", 1),
            Edge("H", "S", 3),
            Edge("S", "H", 3),
        ]
    )


def small_scenario(*, side_open: bool = True, staffed_open: bool = True,
                   reservations=None) -> NightScenario:
    side_windows = None if side_open else ()
    staffed_windows = None if staffed_open else ()
    entrances = (
        Entrance("eD", "B1", "D", "side", requires_key="K", windows=side_windows),
        Entrance("eS", "B1", "S", "staffed", windows=staffed_windows),
    )
    return NightScenario(
        graph=small_street_graph(),
        station="H",
        entrances=entrances,
        parcels=(NightParcel("M1", "B1"),),
        closing_minute=8,
        counter_nodes={"K": "H"},
        counter_holders={"K": "H"},
        borrowable={"H": ("K",)},
        external_reservations=reservations or {},
    )


class SmallStreetWorkedExampleTests(unittest.TestCase):
    def test_two_reports_short_walk_vs_zero_key(self):
        planner = NightPlanner(small_scenario())
        shortest = planner.shortest_walk()
        least = planner.least_key_minutes()
        # Side door: borrow K, walk 1, deliver, walk 1, return -> 2 walk, key [0,2).
        self.assertEqual(shortest.walk_minutes, 2)
        self.assertEqual(shortest.key_minutes_total, 2)
        self.assertEqual(shortest.key_intervals, {"K": ((0, 2),)})
        # Staffed door round trip: 6 walk minutes and no key.
        self.assertEqual(least.walk_minutes, 6)
        self.assertEqual(least.key_minutes_total, 0)
        self.assertEqual(least.key_intervals, {})

    def test_director_report_contains_both_optima_and_frontier(self):
        report = NightPlanner(small_scenario()).director_report()
        self.assertTrue(report["feasible"])
        self.assertEqual(report["shortest_walk"]["walk_minutes"], 2)
        self.assertEqual(report["shortest_walk"]["key_minutes_total"], 2)
        self.assertEqual(report["least_key_minutes"]["walk_minutes"], 6)
        self.assertEqual(report["least_key_minutes"]["key_minutes_total"], 0)
        frontier_pairs = {
            (p["walk_minutes"], p["key_minutes_total"]) for p in report["pareto_frontier"]
        }
        self.assertIn((2, 2), frontier_pairs)
        self.assertIn((6, 0), frontier_pairs)

    def test_key_minute_budget_selects_feasible_shortest(self):
        planner = NightPlanner(small_scenario())
        # Budget 0 forces the staffed-door route.
        plan = planner.shortest_walk_with_budget(0)
        self.assertEqual(plan.walk_minutes, 6)
        self.assertEqual(plan.key_minutes_total, 0)
        # Budget 2 admits the short side-door route.
        plan2 = planner.shortest_walk_with_budget(2)
        self.assertEqual(plan2.walk_minutes, 2)
        # An impossible budget yields no plan.
        self.assertIsNone(planner.shortest_walk_with_budget(-1))

    def test_every_plan_returns_to_station_and_all_keys_before_closing(self):
        planner = NightPlanner(small_scenario())
        for plan in planner.all_plans():
            self.assertEqual(plan.delivered, ("M1",))
            self.assertLessEqual(plan.finish_minute, 8)
            last = plan.steps[-1]
            self.assertIn(last.kind, (STEP_WALK, STEP_RETURN))
            for key, intervals in plan.key_intervals.items():
                for lo, hi in intervals:
                    self.assertLess(lo, hi)
                    self.assertLessEqual(hi, 8)

    def test_in_transit_key_removes_side_door_route(self):
        # K is simply absent from borrowable -> the side door cannot be used.
        scenario = small_scenario()
        object.__setattr__(scenario, "borrowable", {})
        planner = NightPlanner(scenario)
        self.assertTrue(planner.feasible())
        self.assertEqual(planner.shortest_walk().walk_minutes, 6)
        self.assertEqual(planner.shortest_walk().key_minutes_total, 0)

    def test_closed_staffed_door_forces_key_route(self):
        planner = NightPlanner(small_scenario(staffed_open=False))
        plan = planner.shortest_walk()
        self.assertEqual(plan.walk_minutes, 2)
        self.assertEqual(plan.key_minutes_total, 2)
        # And with the key unavailable too, nothing is feasible.
        scenario = small_scenario(staffed_open=False)
        object.__setattr__(scenario, "borrowable", {})
        self.assertFalse(NightPlanner(scenario).feasible())

    def test_external_reservation_blocks_conflicting_interval(self):
        # Another shift holds K over [0,3): the [0,2) side-door plan clashes.
        planner = NightPlanner(
            small_scenario(reservations={"K": ((0, 3),)})
        )
        plans = planner.all_plans()
        # Side door may still work by borrowing after minute 3, but the [0,2)
        # interval itself must never appear.
        for plan in plans:
            for lo, hi in plan.key_intervals.get("K", ()):
                self.assertFalse(lo < 3 and hi > 0)
        # The no-key staffed route remains available and is key-optimal.
        self.assertEqual(planner.least_key_minutes().key_minutes_total, 0)


class TimedWindowAndWaitTests(unittest.TestCase):
    def test_wait_until_entrance_opens(self):
        # D is a dead-end: the carrier reaches the side door at minute 1, but it
        # only opens at minute 3, so the only feasible route waits in place.
        graph = StreetGraph([Edge("H", "D", 1), Edge("D", "H", 1)])
        entrances = (
            Entrance("eD", "B1", "D", "side", requires_key="K", windows=((3, 9),)),
        )
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances,
            parcels=(NightParcel("M1", "B1"),), closing_minute=9,
            counter_nodes={"K": "H"}, counter_holders={"K": "H"},
            borrowable={"H": ("K",)}, external_reservations={},
        )
        planner = NightPlanner(scenario)
        plans_with_wait = [
            p for p in planner.all_plans()
            if any(s.kind == STEP_WAIT for s in p.steps)
        ]
        self.assertTrue(plans_with_wait)
        plan = min(plans_with_wait, key=lambda p: p.finish_minute)
        deliver = next(s for s in plan.steps if s.kind == STEP_DELIVER)
        self.assertGreaterEqual(deliver.minute, 3)
        self.assertLessEqual(plan.finish_minute, 9)

    def test_window_that_never_opens_makes_infeasible(self):
        graph = StreetGraph([Edge("H", "D", 1), Edge("D", "H", 1)])
        entrances = (
            Entrance("eD", "B1", "D", "side", requires_key="K", windows=((9, 12),)),
        )
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances,
            parcels=(NightParcel("M1", "B1"),), closing_minute=9,
            counter_nodes={"K": "H"}, counter_holders={"K": "H"},
            borrowable={"H": ("K",)}, external_reservations={},
        )
        self.assertFalse(NightPlanner(scenario).feasible())

    def test_category_restricted_entrance(self):
        graph = StreetGraph([Edge("H", "D", 1), Edge("D", "H", 1)])
        entrances = (
            Entrance("eD", "B1", "D", "side", requires_key="K", categories=("letter",)),
        )
        # A parcel of another category cannot use this entrance.
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances,
            parcels=(NightParcel("M1", "B1", category="parcel"),),
            closing_minute=8, counter_nodes={"K": "H"}, counter_holders={"K": "H"},
            borrowable={"H": ("K",)}, external_reservations={},
        )
        self.assertFalse(NightPlanner(scenario).feasible())


class OrderingAndMultiParcelTests(unittest.TestCase):
    def test_parcel_after_ordering(self):
        graph = StreetGraph(
            [Edge("H", "A", 1), Edge("A", "H", 1), Edge("H", "B", 2), Edge("B", "H", 2)]
        )
        entrances = (
            Entrance("eA", "BA", "A", "staffed"),
            Entrance("eB", "BB", "B", "staffed"),
        )
        parcels = (
            NightParcel("P1", "BA"),
            NightParcel("P2", "BB", after=("P1",)),
        )
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances, parcels=parcels,
            closing_minute=12, counter_nodes={}, counter_holders={},
            borrowable={}, external_reservations={},
        )
        planner = NightPlanner(scenario)
        self.assertTrue(planner.feasible())
        for plan in planner.all_plans():
            minutes = {
                s.detail["parcel_id"]: s.minute
                for s in plan.steps if s.kind == STEP_DELIVER
            }
            self.assertLessEqual(minutes["P1"], minutes["P2"])

    def test_cyclic_ordering_rejected(self):
        graph = StreetGraph([Edge("H", "A", 1), Edge("A", "H", 1)])
        entrances = (Entrance("eA", "BA", "A", "staffed"),)
        parcels = (
            NightParcel("P1", "BA", after=("P2",)),
            NightParcel("P2", "BA", after=("P1",)),
        )
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances, parcels=parcels,
            closing_minute=8,
        )
        with self.assertRaises(PlanningError):
            NightPlanner(scenario)


class SameKeyMultiBuildingTests(unittest.TestCase):
    def test_one_key_interval_serves_two_side_doors(self):
        # One key opens both side doors D1 and D2; a single continuous loan can
        # cover both deliveries.  The line H-D1-D2 (and back) forces an order.
        graph = StreetGraph(
            [
                Edge("H", "D1", 1), Edge("D1", "H", 1),
                Edge("D1", "D2", 1), Edge("D2", "D1", 1),
            ]
        )
        entrances = (
            Entrance("e1", "B1", "D1", "side", requires_key="K"),
            Entrance("e2", "B2", "D2", "side", requires_key="K"),
        )
        parcels = (NightParcel("P1", "B1"), NightParcel("P2", "B2"))
        scenario = NightScenario(
            graph=graph, station="H", entrances=entrances, parcels=parcels,
            closing_minute=20, counter_nodes={"K": "H"}, counter_holders={"K": "H"},
            borrowable={"H": ("K",)}, external_reservations={},
        )
        planner = NightPlanner(scenario)
        self.assertTrue(planner.feasible())
        least = planner.least_key_minutes()
        # The key-optimal plan uses exactly one continuous interval.
        self.assertEqual(len(least.key_intervals["K"]), 1)
        self.assertEqual(set(least.delivered), {"P1", "P2"})
        shortest = planner.shortest_walk()
        # H->D1->D2->D1->H is four edges of one minute.
        self.assertEqual(shortest.walk_minutes, 4)
        self.assertEqual(set(shortest.delivered), {"P1", "P2"})


class GraphValidationTests(unittest.TestCase):
    def test_positive_edges_and_known_station(self):
        with self.assertRaises(PlanningError):
            StreetGraph([Edge("H", "D", 0)])
        with self.assertRaises(PlanningError):
            StreetGraph([Edge("H", "H", 1)])
        scenario = NightScenario(
            graph=StreetGraph([Edge("A", "B", 1)]), station="Z",
            entrances=(), parcels=(), closing_minute=5,
        )
        with self.assertRaises(PlanningError):
            NightPlanner(scenario)

    def test_side_door_must_declare_key(self):
        scenario = NightScenario(
            graph=StreetGraph([Edge("H", "D", 1)]), station="H",
            entrances=(Entrance("eD", "B1", "D", "side"),),
            parcels=(NightParcel("P1", "B1"),), closing_minute=5,
        )
        with self.assertRaises(PlanningError):
            NightPlanner(scenario)


class BruteForceCrossValidationTests(unittest.TestCase):
    """The Pareto label search must match an unpruned exhaustive reference."""

    @staticmethod
    def _brute_force_optima(scenario: NightScenario):
        planner = NightPlanner(scenario)
        order = planner._topological_order()
        initial = {
            "node": scenario.station, "minute": 0,
            "delivered": frozenset(), "holding": frozenset(),
            "borrow_starts": {}, "closed_intervals": {},
            "steps": (), "walk": 0, "keys": 0,
        }
        target = frozenset(p.parcel_id for p in scenario.parcels)
        stack = [initial]
        seen = set()
        terminals = []
        while stack:
            state = stack.pop()
            dedupe = (
                state["node"], state["minute"], state["delivered"], state["holding"],
                tuple(sorted(state["borrow_starts"].items())),
                tuple(sorted(
                    (key, iv)
                    for key, intervals in state["closed_intervals"].items()
                    for iv in intervals
                )),
            )
            if dedupe in seen:
                continue
            seen.add(dedupe)
            if (
                state["node"] == scenario.station
                and not state["holding"]
                and state["delivered"] == target
            ):
                terminals.append(state)
                continue
            stack.extend(planner._zero_transitions(state, order))
            stack.extend(planner._walk_transitions(state))
            stack.extend(planner._wait_transitions(state))
        if not terminals:
            return None
        shortest = min(terminals, key=lambda s: (s["walk"], s["keys"]))
        least_keys = min(terminals, key=lambda s: (s["keys"], s["walk"]))
        return (
            (shortest["walk"], shortest["keys"]),
            (least_keys["keys"], least_keys["walk"]),
        )

    def test_matches_brute_force_on_random_small_scenarios(self):
        import random

        rng = random.Random(7)
        for _ in range(30):
            n_nodes = rng.randint(3, 6)
            nodes = ["H"] + [f"N{i}" for i in range(n_nodes - 1)]
            edges = []
            for i in range(len(nodes) - 1):
                edges.append(Edge(nodes[i], nodes[i + 1], rng.randint(1, 3)))
                edges.append(Edge(nodes[i + 1], nodes[i], rng.randint(1, 3)))
            if rng.random() < 0.5:
                a, b = rng.sample(nodes, 2)
                edges.append(Edge(a, b, rng.randint(1, 2)))
            graph = StreetGraph(edges)
            key_names = [f"K{i}" for i in range(rng.randint(0, 2))]
            entrances = []
            buildings = []
            for i in range(rng.randint(1, 3)):
                node = rng.choice([n for n in nodes if n != "H"])
                building = f"B{i}"
                buildings.append(building)
                if key_names and rng.random() < 0.6:
                    entrances.append(
                        Entrance(f"s{i}", building, node, "side",
                                 requires_key=rng.choice(key_names))
                    )
                else:
                    entrances.append(Entrance(f"t{i}", building, node, "staffed"))
            parcels = tuple(NightParcel(f"P{i}", b) for i, b in enumerate(buildings))
            scenario = NightScenario(
                graph=graph, station="H", entrances=tuple(entrances), parcels=parcels,
                closing_minute=rng.randint(8, 14),
                counter_nodes={k: "H" for k in key_names},
                counter_holders={k: "H" for k in key_names},
                borrowable={"H": tuple(key_names)},
                external_reservations={},
            )
            expected = self._brute_force_optima(scenario)
            planner = NightPlanner(scenario)
            if expected is None:
                self.assertFalse(planner.feasible())
                continue
            shortest = planner.shortest_walk()
            least = planner.least_key_minutes()
            self.assertEqual(
                (shortest.walk_minutes, shortest.key_minutes_total), expected[0]
            )
            self.assertEqual(
                (least.key_minutes_total, least.walk_minutes), expected[1]
            )


if __name__ == "__main__":
    unittest.main()
