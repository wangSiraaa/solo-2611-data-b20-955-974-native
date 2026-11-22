"""Night-shift walking routes and physical-key signing desk.

The carrier leaves the station node and must return there before closing time,
having delivered every parcel.  The street graph is directed; every edge has a
positive integer length in minutes.  A building entrance sits on a graph node,
accepts declared parcel categories during half-open integer windows, may
demand a specific physical key (a side door / 便门), and parcels may impose an
ordering (one parcel handed over only after another).  Handing over at an
eligible entrance, borrowing and returning keys, and waiting in place all cost
zero minutes; several parcels can be handed over at the same moment in order.

Keys are borrowed at the counter node recognised by a frozen cutoff sheet and
must be returned to the *same* counter.  Each key is exclusive: an occupancy is
the integer half-open interval ``[out, back)`` of minutes the key physically
spends out of its cabinet, and it must not overlap another shift's reservations
or another of this carrier's own intervals.

This module has two layers:

* :class:`NightPlanner` - a time-indexed label-setting search that keeps the
  Pareto set over (walk minutes, key minutes).  With at most six parcels, ten
  nodes, three keys and a 24-minute horizon it answers the director's three
  questions exactly: shortest walk, smallest total key minutes (then the other
  metric), and shortest walk under a key-minute budget.
* :class:`NightDesk` - the persistence side.  A generated plan occupies nothing
  until it is signed; the desk rejects a new signature that clashes with an
  already signed interval, re-signing the same plan never issues a second key
  interval, and a failed signature occupies no key at all.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import PlanningError
from .keys import KeyLedger, _overlaps

# Step kind vocabulary ---------------------------------------------------------
STEP_WALK = "walk"
STEP_WAIT = "wait"
STEP_DELIVER = "deliver"
STEP_BORROW = "borrow"
STEP_RETURN = "return"

# Signature outcomes -----------------------------------------------------------
SIGNED = "signed"
REJECTED_CONFLICT = "rejected_conflict"
REJECTED_BORROWABLE = "rejected_not_borrowable"
REJECTED_INFEASIBLE = "rejected_infeasible"
REPLAYED_SIGNATURE = "replayed"


# ---------------------------------------------------------------------------
# Scenario value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    src: str
    dst: str
    minutes: int

    def as_dict(self) -> dict[str, Any]:
        return {"src": self.src, "dst": self.dst, "minutes": self.minutes}


class StreetGraph:
    """A directed graph whose edges carry positive integer minutes."""

    def __init__(self, edges: Sequence[Edge | dict[str, Any]]):
        self._edges: list[Edge] = []
        self._adj: dict[str, list[Edge]] = {}
        self._nodes: set[str] = set()
        for raw in edges:
            edge = raw if isinstance(raw, Edge) else Edge(
                str(raw["src"]), str(raw["dst"]), int(raw["minutes"])
            )
            if isinstance(edge.minutes, bool) or edge.minutes <= 0:
                raise PlanningError("edge minutes must be positive integers")
            if edge.src == edge.dst:
                raise PlanningError("self loops are not allowed")
            self._edges.append(edge)
            self._nodes.add(edge.src)
            self._nodes.add(edge.dst)
            self._adj.setdefault(edge.src, []).append(edge)
        for adj in self._adj.values():
            adj.sort(key=lambda e: (e.dst, e.minutes))

    @property
    def nodes(self) -> tuple[str, ...]:
        return tuple(sorted(self._nodes))

    def edges(self) -> tuple[Edge, ...]:
        return tuple(sorted(self._edges, key=lambda e: (e.src, e.dst)))

    def neighbors(self, node: str) -> tuple[Edge, ...]:
        return tuple(self._adj.get(node, ()))

    def has_node(self, node: str) -> bool:
        return node in self._nodes

    def as_dict(self) -> dict[str, Any]:
        return {"nodes": list(self.nodes), "edges": [e.as_dict() for e in self.edges()]}


@dataclass(frozen=True)
class Entrance:
    """A building entrance on a graph node.

    * ``kind`` ``"staffed"`` is an attended entrance (值守门); ``"side"`` is a
      side door that demands ``requires_key``.
    * ``windows`` are half-open integer intervals ``[lo, hi)`` during which the
      entrance accepts parcels.  ``None`` means open all night; an empty tuple
      means closed all night.
    * ``categories`` restricts which parcel categories may be handed over here;
      an empty tuple/``None`` accepts every category.
    """

    entrance_id: str
    building_id: str
    node: str
    kind: str
    requires_key: str | None = None
    windows: tuple[tuple[int, int], ...] | None = None
    categories: tuple[str, ...] = ()

    def accepts_category(self, category: str | None) -> bool:
        return not self.categories or category in self.categories

    def open_at(self, minute: int) -> bool:
        if self.windows is None:
            return True
        return any(lo <= minute < hi for lo, hi in self.windows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entrance_id": self.entrance_id,
            "building_id": self.building_id,
            "node": self.node,
            "kind": self.kind,
            "requires_key": self.requires_key,
            "windows": None if self.windows is None else [list(w) for w in self.windows],
            "categories": list(self.categories),
        }


@dataclass(frozen=True)
class NightParcel:
    parcel_id: str
    building_id: str
    category: str | None = None
    after: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "building_id": self.building_id,
            "category": self.category,
            "after": list(self.after),
        }


@dataclass(frozen=True)
class Step:
    kind: str
    minute: int
    detail: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        payload = {"kind": self.kind, "minute": self.minute}
        payload.update(self.detail)
        return payload


@dataclass(frozen=True)
class RoutePlan:
    plan_id: str
    steps: tuple[Step, ...]
    delivered: tuple[str, ...]
    key_intervals: dict[str, tuple[tuple[int, int], ...]]
    walk_minutes: int
    key_minutes_total: int
    finish_minute: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "steps": [s.as_dict() for s in self.steps],
            "delivered": list(self.delivered),
            "key_intervals": {
                key: [list(iv) for iv in intervals]
                for key, intervals in sorted(self.key_intervals.items())
            },
            "walk_minutes": self.walk_minutes,
            "key_minutes_total": self.key_minutes_total,
            "finish_minute": self.finish_minute,
        }


@dataclass(frozen=True)
class NightScenario:
    graph: StreetGraph
    station: str
    entrances: tuple[Entrance, ...]
    parcels: tuple[NightParcel, ...]
    closing_minute: int
    # key -> graph node of the counter that holds it (borrow/return location).
    counter_nodes: dict[str, str] = field(default_factory=dict)
    # key -> ledger counter id that holds it (custody identity).
    counter_holders: dict[str, str] = field(default_factory=dict)
    # ledger counter id -> keys quietly held there at the frozen cutoff.
    borrowable: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # key -> half-open [lo, hi) intervals already occupied by other shifts.
    external_reservations: dict[str, tuple[tuple[int, int], ...]] = field(default_factory=dict)

    def entrance(self, entrance_id: str) -> Entrance | None:
        return next((e for e in self.entrances if e.entrance_id == entrance_id), None)

    def parcel(self, parcel_id: str) -> NightParcel | None:
        return next((p for p in self.parcels if p.parcel_id == parcel_id), None)


# ---------------------------------------------------------------------------
# Pure exhaustive planner
# ---------------------------------------------------------------------------


class NightPlanner:
    """Enumerate every feasible route for one night scenario."""

    def __init__(self, scenario: NightScenario, *, max_loans_per_key: int | None = None):
        self.scenario = scenario
        self._validate()
        self._entrances_by_building: dict[str, list[Entrance]] = {}
        for entrance in scenario.entrances:
            self._entrances_by_building.setdefault(entrance.building_id, []).append(entrance)
        for entrances in self._entrances_by_building.values():
            entrances.sort(key=lambda e: e.entrance_id)
        self._plans_by_id: dict[str, RoutePlan] = {}
        # A loan interval that contains no key-required delivery is pure waste:
        # merging it away can only reduce key minutes without adding walking.
        # In any non-dominated plan each interval therefore contains at least
        # one delivery, so the parcel count is a safe bound on useful intervals
        # and keeps the exhaustive search finite at the problem's scale (<=6).
        if max_loans_per_key is None:
            max_loans_per_key = max(1, len(scenario.parcels))
        if max_loans_per_key < 1:
            raise PlanningError("max_loans_per_key must be >= 1")
        self.max_loans_per_key = max_loans_per_key

    # -- validation -----------------------------------------------------------

    def _validate(self) -> None:
        s = self.scenario
        if not s.graph.has_node(s.station):
            raise PlanningError(f"station {s.station!r} is not a graph node")
        if isinstance(s.closing_minute, bool) or not isinstance(s.closing_minute, int):
            raise PlanningError("closing_minute must be an integer")
        if s.closing_minute <= 0:
            raise PlanningError("closing_minute must be positive")
        if len(s.graph.nodes) > 10:
            raise PlanningError("at most ten street-graph nodes are supported")
        if len(s.parcels) > 6:
            raise PlanningError("at most six parcels per night are supported")
        parcel_ids = set()
        building_ids = set()
        for parcel in s.parcels:
            if parcel.parcel_id in parcel_ids:
                raise PlanningError(f"duplicate parcel {parcel.parcel_id}")
            parcel_ids.add(parcel.parcel_id)
            building_ids.add(parcel.building_id)
        # Ordering must form a DAG over known parcels.
        self._topological_order()
        entrance_ids = set()
        for entrance in s.entrances:
            if entrance.entrance_id in entrance_ids:
                raise PlanningError(f"duplicate entrance {entrance.entrance_id}")
            entrance_ids.add(entrance.entrance_id)
            if not s.graph.has_node(entrance.node):
                raise PlanningError(
                    f"entrance {entrance.entrance_id} on unknown node {entrance.node!r}"
                )
            if entrance.kind == "side" and not entrance.requires_key:
                raise PlanningError(
                    f"side entrance {entrance.entrance_id} must declare requires_key"
                )
            if entrance.kind == "staffed" and entrance.requires_key:
                raise PlanningError(
                    f"staffed entrance {entrance.entrance_id} must not require a key"
                )
            if entrance.requires_key and entrance.requires_key not in s.counter_nodes:
                raise PlanningError(
                    f"key {entrance.requires_key} has no counter node in this scenario"
                )
            counter_node = s.counter_nodes.get(entrance.requires_key)
            if counter_node is not None and not s.graph.has_node(counter_node):
                raise PlanningError(
                    f"counter node {counter_node!r} for key {entrance.requires_key} is unknown"
                )
            if entrance.windows is not None:
                for lo, hi in entrance.windows:
                    if not (0 <= lo < hi):
                        raise PlanningError("entrance windows must satisfy 0 <= lo < hi")
        for parcel in s.parcels:
            if parcel.building_id not in self._buildings_with_entrances():
                raise PlanningError(
                    f"parcel {parcel.parcel_id} targets building "
                    f"{parcel.building_id} with no entrance"
                )

    def _buildings_with_entrances(self) -> set[str]:
        return {e.building_id for e in self.scenario.entrances}

    def _topological_order(self) -> list[str]:
        parcels = {p.parcel_id: p for p in self.scenario.parcels}
        visited: dict[str, int] = {}
        order: list[str] = []

        def visit(pid: str, stack: set[str]) -> None:
            state = visited.get(pid, 0)
            if state == 1:
                raise PlanningError("parcel 'after' ordering contains a cycle")
            if state == 2:
                return
            visited[pid] = 1
            for prereq in parcels[pid].after:
                if prereq not in parcels:
                    raise PlanningError(f"parcel {pid} waits on unknown parcel {prereq}")
                visit(prereq, stack)
            visited[pid] = 2
            order.append(pid)

        for pid in sorted(parcels):
            visit(pid, set())
        return order

    # -- search ---------------------------------------------------------------

    def all_plans(self) -> list[RoutePlan]:
        """Return the distinct feasible plans worth keeping (the Pareto set).

        Time-indexed label-setting dynamic programming.  States are bucketed by
        minute and processed in chronological order; within one minute the
        zero-minute actions (deliver, borrow, return) are closed to a fixed
        point, after which only non-dominated ``(walk_minutes, key_minutes)``
        labels survive and each is expanded exactly once.  Walking and waiting
        always advance the clock, so processing minute buckets in order is
        sufficient: once a minute is settled no later transition can feed it,
        there is no re-expansion, and the search stays bounded at the problem's
        scale (<=6 parcels, 10 nodes, 3 keys, a 24-minute horizon).
        """
        if self._plans_by_id:
            return list(self._plans_by_id.values())
        scenario = self.scenario
        all_parcel_ids = frozenset(p.parcel_id for p in scenario.parcels)
        order = self._topological_order()
        horizon = scenario.closing_minute

        def signature(state: dict[str, Any]) -> tuple:
            # The minute is implicit because labels live in per-minute buckets.
            # Neither exact borrow start minutes nor closed interval tuples are
            # part of the signature: their cost is carried by the scalar
            # ``walk``/``keys`` Pareto coordinates, so routes that differ only
            # in when an equal-cost loan was taken collapse together.  The
            # surviving labels still retain their own starts/intervals for
            # reconstructing concrete plans.
            cached = state.get("_sig")
            if cached is not None:
                return cached
            sig = (
                state["node"],
                state["delivered"],
                state["holding"],
            )
            state["_sig"] = sig
            return sig

        def dominates(a: dict[str, Any], b: dict[str, Any]) -> bool:
            return (
                a["walk"] <= b["walk"]
                and a["keys"] <= b["keys"]
                and (a["walk"] < b["walk"] or a["keys"] < b["keys"])
            )

        initial = {
            "node": scenario.station,
            "minute": 0,
            "delivered": frozenset(),
            "holding": frozenset(),
            "borrow_starts": {},
            "closed_intervals": {},
            "steps": (),
            "walk": 0,
            "keys": 0,
        }
        # minute -> signature -> list of mutually non-dominated labels.
        buckets: dict[int, dict[tuple, list[dict[str, Any]]]] = {
            0: {signature(initial): [initial]}
        }
        terminals: list[dict[str, Any]] = []

        def is_terminal(state: dict[str, Any]) -> bool:
            return (
                state["node"] == scenario.station
                and not state["holding"]
                and frozenset(state["delivered"]) == all_parcel_ids
            )

        def place(state: dict[str, Any], target: dict[tuple, list[dict[str, Any]]]) -> bool:
            """Insert into one minute's bucket unless an equal/better label is there."""
            sig = signature(state)
            kept = target.setdefault(sig, [])
            if any(
                label["walk"] <= state["walk"] and label["keys"] <= state["keys"]
                for label in kept
            ):
                return False
            kept[:] = [label for label in kept if not dominates(state, label)]
            kept.append(state)
            return True

        for minute in range(horizon + 1):
            bucket = buckets.get(minute)
            if not bucket:
                continue

            # Close zero-minute actions inside this minute.  Every accepted
            # label is expanded exactly once via a work queue (rather than
            # re-scanning the whole frontier until quiescence).
            zero_frontier: dict[tuple, list[dict[str, Any]]] = {}
            work: list[dict[str, Any]] = []
            for labels in bucket.values():
                for label in labels:
                    if place(label, zero_frontier):
                        work.append(label)
            while work:
                label = work.pop()
                for nxt in self._zero_transitions(label, order):
                    if place(nxt, zero_frontier):
                        work.append(nxt)

            # Expand the settled frontier: collect terminal routes and forward
            # the clock-advancing transitions into later minute buckets.
            for labels in zero_frontier.values():
                for state in labels:
                    if is_terminal(state):
                        terminals.append(state)
                        continue
                    for nxt in self._walk_transitions(state):
                        buckets.setdefault(nxt["minute"], {})
                        place(nxt, buckets[nxt["minute"]])
                    for nxt in self._wait_transitions(state):
                        buckets.setdefault(nxt["minute"], {})
                        place(nxt, buckets[nxt["minute"]])

        plans: dict[str, RoutePlan] = {}
        for state in terminals:
            plan = self._build_plan(state, state["keys"])
            plans[plan.plan_id] = plan
        ordered = sorted(
            plans.values(),
            key=lambda p: (
                p.walk_minutes,
                p.key_minutes_total,
                p.finish_minute,
                self._signature(p),
            ),
        )
        for plan in ordered:
            self._plans_by_id[plan.plan_id] = plan
        return ordered

    def _zero_transitions(
        self, state: dict[str, Any], order: Sequence[str]
    ) -> list[dict[str, Any]]:
        """Zero-minute actions: a delivery, then a key borrow or return."""
        scenario = self.scenario
        minute = state["minute"]
        node = state["node"]
        out: list[dict[str, Any]] = []

        # At most one delivery transition per parcel at this node/minute.
        for parcel_id in order:
            if parcel_id in state["delivered"]:
                continue
            parcel = scenario.parcel(parcel_id)
            if not all(a in state["delivered"] for a in parcel.after):
                continue
            for entrance in self._entrances_by_building.get(parcel.building_id, ()):
                if entrance.node != node:
                    continue
                if not entrance.accepts_category(parcel.category):
                    continue
                if not entrance.open_at(minute):
                    continue
                if entrance.requires_key and entrance.requires_key not in state["holding"]:
                    continue
                nxt = self._clone(state)
                nxt["delivered"] = state["delivered"] | {parcel_id}
                nxt["steps"] = state["steps"] + (
                    Step(
                        STEP_DELIVER,
                        minute,
                        {
                            "parcel_id": parcel_id,
                            "entrance_id": entrance.entrance_id,
                            "building_id": parcel.building_id,
                            "key": entrance.requires_key,
                        },
                    ),
                )
                out.append(nxt)
                break

        for key_code in sorted(scenario.counter_nodes):
            if scenario.counter_nodes[key_code] != node:
                continue
            holding = state["holding"]
            starts = state["borrow_starts"]
            closed = state["closed_intervals"]

            if key_code not in holding and key_code not in starts:
                if len(closed.get(key_code, ())) >= self.max_loans_per_key:
                    continue
                if key_code not in self._available_keys(node):
                    continue
                nxt = self._clone(state)
                nxt["holding"] = holding | {key_code}
                nxt["borrow_starts"] = {**starts, key_code: minute}
                nxt["steps"] = state["steps"] + (
                    Step(STEP_BORROW, minute,
                         {"key": key_code, "counter": self._holder_of(key_code)}),
                )
                out.append(nxt)

            if key_code in holding:
                start = starts[key_code]
                end = minute
                if end <= start:
                    continue
                if self._external_blocked(key_code, start, end):
                    continue
                nxt = self._clone(state)
                nxt["holding"] = holding - {key_code}
                new_starts = dict(starts)
                del new_starts[key_code]
                nxt["borrow_starts"] = new_starts
                prior = closed.get(key_code, ())
                nxt["closed_intervals"] = {
                    **closed, key_code: tuple(sorted(prior + ((start, end),)))
                }
                nxt["keys"] = key_cost_static(nxt["closed_intervals"])
                nxt["steps"] = state["steps"] + (
                    Step(STEP_RETURN, minute, {
                        "key": key_code, "counter": self._holder_of(key_code),
                        "out": start, "back": end, "minutes": end - start,
                    }),
                )
                out.append(nxt)
        return out

    def _walk_transitions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for edge in self.scenario.graph.neighbors(state["node"]):
            arrival = state["minute"] + edge.minutes
            if arrival > self.scenario.closing_minute:
                continue
            nxt = self._clone(state)
            nxt["node"] = edge.dst
            nxt["minute"] = arrival
            nxt["walk"] += edge.minutes
            nxt["steps"] = state["steps"] + (
                Step(STEP_WALK, arrival,
                     {"from": state["node"], "to": edge.dst, "minutes": edge.minutes}),
            )
            out.append(nxt)
        return out

    def _wait_transitions(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        """Wait in place for one minute.

        Waiting costs no walk but advances the clock (and therefore any open
        key loan).  It is only generated when an entrance at this node opens
        later, so a wait can lead to real progress; Pareto label pruning then
        discards every wait that is dominated by walking around instead.
        """
        if state["minute"] >= self.scenario.closing_minute:
            return []
        if not self._waiting_useful(state):
            return []
        nxt = self._clone(state)
        nxt["minute"] = state["minute"] + 1
        nxt["steps"] = state["steps"] + (
            Step(STEP_WAIT, nxt["minute"], {"at": state["node"]}),
        )
        return [nxt]

    # -- plan construction ----------------------------------------------------

    def _build_plan(self, state: dict[str, Any], key_minutes: int) -> RoutePlan:
        steps = tuple(self._compress_waits(state["steps"]))
        delivered = tuple(sorted(state["delivered"]))
        intervals = {
            key: tuple(sorted(values))
            for key, values in sorted(state["closed_intervals"].items())
        }
        signature = self._steps_signature(steps, intervals)
        return RoutePlan(
            plan_id=self._plan_id(signature),
            steps=steps,
            delivered=delivered,
            key_intervals=intervals,
            walk_minutes=state["walk"],
            key_minutes_total=key_minutes,
            finish_minute=state["minute"],
        )

    @staticmethod
    def _plan_id(signature: str) -> str:
        # Deterministic, collision-resistant short id for one concrete route.
        digest = hashlib.blake2b(signature.encode("utf-8"), digest_size=10).hexdigest()
        return f"plan-{digest}"

    @staticmethod
    def _compress_waits(steps: list[Step]) -> list[Step]:
        """Merge consecutive one-minute waits into one wait step per pause."""
        result: list[Step] = []
        for step in steps:
            if step.kind == STEP_WAIT and result and result[-1].kind == STEP_WAIT:
                prev = result[-1]
                result[-1] = Step(
                    STEP_WAIT,
                    step.minute,
                    {"at": step.detail["at"], "from": prev.detail.get("from", prev.minute - 1)},
                )
            elif step.kind == STEP_WAIT:
                result.append(Step(STEP_WAIT, step.minute, {"at": step.detail["at"], "from": step.minute - 1}))
            else:
                result.append(step)
        return result

    @staticmethod
    def _steps_signature(steps: Sequence[Step], intervals: dict[str, tuple]) -> str:
        parts = [s.kind + ":" + str(s.minute) + ":" + json_dumps(s.detail) for s in steps]
        for key, values in sorted(intervals.items()):
            parts.append(f"iv:{key}:{values}")
        return "|".join(parts)

    def _signature(self, plan: RoutePlan) -> str:
        return self._steps_signature(plan.steps, plan.key_intervals)

    # -- key helpers ----------------------------------------------------------

    def _holder_of(self, key_code: str) -> str:
        """Ledger counter id that custody-wise holds *key_code*."""
        return self.scenario.counter_holders.get(key_code, key_code)

    def _available_keys(self, node: str) -> set[str]:
        result: set[str] = set()
        for key_code, counter_node in self.scenario.counter_nodes.items():
            if counter_node != node:
                continue
            holders = self.scenario.borrowable.get(self._holder_of(key_code), ())
            if key_code in holders:
                result.add(key_code)
        return result

    def _external_blocked(self, key_code: str, start: int, end: int) -> bool:
        for lo, hi in self.scenario.external_reservations.get(key_code, ()):
            if _overlaps(start, end, lo, hi):
                return True
        return False

    def _waiting_useful(self, state: dict[str, Any]) -> bool:
        """True only if waiting here can lead to a future delivery/key action."""
        node = state["node"]
        minute = state["minute"]
        # A not-yet-delivered, order-ready parcel whose entrance sits here and
        # is merely closed now but opens later makes waiting worthwhile.
        for parcel in self.scenario.parcels:
            if parcel.parcel_id in state["delivered"]:
                continue
            if not all(a in state["delivered"] for a in parcel.after):
                continue
            for entrance in self._entrances_by_building.get(parcel.building_id, ()):
                if entrance.node != node:
                    continue
                if not entrance.accepts_category(parcel.category):
                    continue
                if entrance.requires_key and entrance.requires_key not in state["holding"]:
                    continue
                if entrance.windows is None:
                    continue
                if any(lo > minute and lo <= self.scenario.closing_minute for lo, _ in entrance.windows):
                    return True
        return False

    @staticmethod
    def _clone(state: dict[str, Any]) -> dict[str, Any]:
        # Shallow-copy the immutable canonical containers; only the mutating
        # transition replaces the fields it changes (and drops the cached sig).
        return {
            "node": state["node"],
            "minute": state["minute"],
            "delivered": state["delivered"],            # frozenset
            "holding": state["holding"],                # frozenset
            "borrow_starts": dict(state["borrow_starts"]),
            "closed_intervals": dict(state["closed_intervals"]),  # key -> tuple
            "steps": state["steps"],                    # replaced, never mutated
            "walk": state["walk"],
            "keys": state["keys"],
        }

    # -- director questions ---------------------------------------------------

    def shortest_walk(self) -> RoutePlan | None:
        plans = self.all_plans()
        return plans[0] if plans else None

    def least_key_minutes(self) -> RoutePlan | None:
        plans = self.all_plans()
        if not plans:
            return None
        return min(
            plans,
            key=lambda p: (p.key_minutes_total, p.walk_minutes, p.finish_minute, self._signature(p)),
        )

    def shortest_walk_with_budget(self, key_minute_budget: int) -> RoutePlan | None:
        plans = [p for p in self.all_plans() if p.key_minutes_total <= key_minute_budget]
        return plans[0] if plans else None

    def feasible(self) -> bool:
        return bool(self.all_plans())

    def frontier(self) -> list[RoutePlan]:
        """The Pareto frontier over (walk minutes, key minutes).

        These are the undominated plans worth putting in front of the director:
        the shortest-walk plan and the least-key-minutes plan are always among
        them, along with any trade-off plans where neither metric can be
        improved without worsening the other.
        """
        plans = self.all_plans()
        result: list[RoutePlan] = []
        for plan in plans:
            dominated = any(
                other is not plan
                and other.walk_minutes <= plan.walk_minutes
                and other.key_minutes_total <= plan.key_minutes_total
                and (
                    other.walk_minutes < plan.walk_minutes
                    or other.key_minutes_total < plan.key_minutes_total
                )
                for other in plans
            )
            if not dominated:
                result.append(plan)
        result.sort(
            key=lambda p: (p.walk_minutes, p.key_minutes_total, self._signature(p))
        )
        return result

    def director_report(self, key_minute_budget: int | None = None) -> dict[str, Any]:
        """The three things the director asks for, in one document."""
        shortest = self.shortest_walk()
        least_keys = self.least_key_minutes()
        return {
            "feasible": self.feasible(),
            "shortest_walk": None if shortest is None else shortest.as_dict(),
            "least_key_minutes": None if least_keys is None else least_keys.as_dict(),
            "pareto_frontier": [p.as_dict() for p in self.frontier()],
            "key_minute_budget": key_minute_budget,
            "shortest_walk_under_budget": (
                None
                if key_minute_budget is None
                else (
                    None
                    if self.shortest_walk_with_budget(key_minute_budget) is None
                    else self.shortest_walk_with_budget(key_minute_budget).as_dict()
                )
            ),
        }


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def key_cost_static(closed_intervals: dict[str, list[tuple[int, int]]]) -> int:
    """Total key-minutes already fixed by closed loan intervals."""
    return sum(
        hi - lo for values in closed_intervals.values() for lo, hi in values
    )


# ---------------------------------------------------------------------------
# Scenario builder from a frozen cutoff sheet
# ---------------------------------------------------------------------------


def scenario_from_cutoff(
    ledger: KeyLedger,
    snapshot_id: str,
    *,
    graph: StreetGraph,
    station: str,
    entrances: Sequence[Entrance],
    parcels: Sequence[NightParcel],
    closing_minute: int,
    counter_nodes: dict[str, str],
    counter_holders: dict[str, str] | None = None,
) -> NightScenario:
    """Build a planner scenario starting from one frozen cutoff inventory.

    Only keys the sheet freezes as quietly held at a counter are borrowable;
    in-transit and disputed keys are absent.  Other shifts' reservations already
    recorded on the ledger are loaded as half-open blocked intervals.
    """
    sheet = ledger.get_cutoff(snapshot_id)
    borrowable: dict[str, list[str]] = {}
    cutoff_holder: dict[str, str] = {}
    for entry in sheet["keys"]:
        # The cutoff sheet is the frozen source of truth for who holds each key
        # at this night's start, overriding the key's original initial holder.
        cutoff_holder[entry["key_code"]] = entry["holder"]
        if entry["borrowable"]:
            borrowable.setdefault(entry["holder"], []).append(entry["key_code"])
    external: dict[str, tuple[tuple[int, int], ...]] = {}
    for key in ledger.list_keys():
        reservations = ledger.reservations_for(key.key_code)
        external[key.key_code] = tuple(
            (r.start_min, r.end_min) for r in reservations
        )
    holders = dict(counter_holders or {})
    for key_code, holder in cutoff_holder.items():
        holders.setdefault(key_code, holder)
    return NightScenario(
        graph=graph,
        station=station,
        entrances=tuple(entrances),
        parcels=tuple(parcels),
        closing_minute=closing_minute,
        counter_nodes=dict(counter_nodes),
        counter_holders=holders,
        borrowable={holder: tuple(sorted(keys)) for holder, keys in borrowable.items()},
        external_reservations=external,
    )


# ---------------------------------------------------------------------------
# Signing desk: persistence side of a planned night
# ---------------------------------------------------------------------------


class NightDesk:
    """Signs plans against a :class:`KeyLedger`, occupying real key intervals."""

    def __init__(self, ledger: KeyLedger):
        self.ledger = ledger
        self._ensure_table()

    def _ensure_table(self) -> None:
        self.ledger.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS signed_plans (
                plan_id        TEXT PRIMARY KEY,
                outcome        TEXT NOT NULL,
                detail_json    TEXT NOT NULL,
                signed_at      TEXT NOT NULL
            );
            """
        )

    def sign(self, plan: RoutePlan) -> dict[str, Any]:
        """Try to occupy every key interval of *plan*.

        The whole signature is one transaction: a clash on any key rejects the
        plan and occupies nothing.  Re-signing an existing plan idempotently
        returns its stored outcome and never issues a second interval.
        """
        existing = self.ledger.conn.execute(
            "SELECT * FROM signed_plans WHERE plan_id=?", (plan.plan_id,)
        ).fetchone()
        if existing is not None:
            return {
                "plan_id": plan.plan_id,
                "outcome": REPLAYED_SIGNATURE,
                "detail": json_loads(existing["detail_json"]),
            }

        # Every demanded key must exist and be quietly held at its counter at
        # the cutoff (the moment this night's planning starts from).
        known = {key.key_code: key for key in self.ledger.list_keys()}
        for key_code in sorted(plan.key_intervals):
            if key_code not in known:
                return self._reject(plan, REJECTED_INFEASIBLE, f"unknown key {key_code}")
            ownership = self.ledger.ownership_at(key_code, 0)
            if not ownership.borrowable:
                return self._reject(
                    plan,
                    REJECTED_BORROWABLE,
                    f"key {key_code} is {ownership.status} at cutoff and cannot be lent",
                )

        # No interval may overlap an existing reservation (other shifts or an
        # already signed plan).
        for key_code, intervals in sorted(plan.key_intervals.items()):
            for lo, hi in intervals:
                if self.ledger.has_overlap(key_code, lo, hi):
                    return self._reject(
                        plan,
                        REJECTED_CONFLICT,
                        f"key {key_code} interval [{lo},{hi}) clashes with a signed occupancy",
                    )

        from .db import transaction

        with transaction(self.ledger.conn):
            index = 0
            for key_code, intervals in sorted(plan.key_intervals.items()):
                for lo, hi in intervals:
                    index += 1
                    self.ledger.add_external_reservation(
                        reservation_id=f"{plan.plan_id}:{key_code}:{index}",
                        key_code=key_code,
                        start_min=lo,
                        end_min=hi,
                        source="night_signature",
                    )
                    self.ledger.conn.execute(
                        "UPDATE external_key_reservations SET plan_id=? "
                        "WHERE reservation_id=?",
                        (plan.plan_id, f"{plan.plan_id}:{key_code}:{index}"),
                    )
            detail = plan.as_dict()
            self.ledger.conn.execute(
                "INSERT INTO signed_plans(plan_id,outcome,detail_json,signed_at) "
                "VALUES(?,?,?,?)",
                (plan.plan_id, SIGNED, json_dumps(detail), _desk_now()),
            )
        return {"plan_id": plan.plan_id, "outcome": SIGNED, "detail": plan.as_dict()}

    def _reject(self, plan: RoutePlan, outcome: str, message: str) -> dict[str, Any]:
        # A failed signature occupies no key; keep the rejection for audit only.
        self.ledger.conn.execute(
            "INSERT INTO signed_plans(plan_id,outcome,detail_json,signed_at) "
            "VALUES(?,?,?,?) ON CONFLICT(plan_id) DO NOTHING",
            (
                plan.plan_id,
                outcome,
                json_dumps({"message": message, "plan": plan.as_dict()}),
                _desk_now(),
            ),
        )
        return {
            "plan_id": plan.plan_id,
            "outcome": outcome,
            "detail": {"message": message, "plan": plan.as_dict()},
        }

    def signed_plan_ids(self) -> list[str]:
        return [
            r["plan_id"]
            for r in self.ledger.conn.execute(
                "SELECT plan_id FROM signed_plans WHERE outcome=? ORDER BY plan_id",
                (SIGNED,),
            ).fetchall()
        ]


def json_loads(value: str) -> Any:
    import json

    return json.loads(value)


def _desk_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()
