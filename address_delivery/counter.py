"""Counter operations during a return-cabinet relocation.

The counter keeps serving pickup slips *while* letters are being carried.  A
:class:`CounterSession` wraps a :class:`~address_delivery.cabinet.ReturnCabinet`
with:

* the three physical letter states - in a cell, in hand, or already taken;
* name checking against the renumbering ledger, so only a slip naming the
  parcel-bound *final* street id and house number is ever honoured;
* segmentable moves (:meth:`begin_move` / :meth:`complete_move`) so another
  window can do business while a letter is in hand;
* seal envelopes - a per-step seal is the minimum scope of every carry, and a
  cycle seal may hold one whole target-cycle plus the empty cell it needs;
* pickup handling: valid slips outside a seal are served on the spot, slips
  hitting a seal are queued and delivered from the real cell at unseal;
* minimum-move BFS plans re-computed from the live placement whenever a letter
  is taken away, with the steps already completed still counted honestly.

No threads or hardware are involved: every operation is an ordinary method
call and the whole session survives via :meth:`snapshot`/:meth:`restore`.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .cabinet import MovePlan, ReturnCabinet
from .errors import CabinetError

POLICY_STEP = "step"
POLICY_CYCLE = "cycle"

# Event / verdict vocabulary
EVENT_PLANNED = "planned"
EVENT_SEAL_OPEN = "seal_opened"
EVENT_MOVE_BEGUN = "move_begun"
EVENT_MOVE_DONE = "move_completed"
EVENT_SEAL_CLOSE = "seal_closed"
EVENT_PICKUP_SERVED = "pickup_served"
EVENT_PICKUP_PENDING = "pickup_pending"
EVENT_PICKUP_REJECTED = "pickup_rejected"
EVENT_REPLAN = "replanned"

SEAL_STEP = "step"
SEAL_CYCLE = "cycle"


@dataclass
class _Seal:
    kind: str
    seal_id: str
    parcels: set[str]
    cells: set[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "seal_id": self.seal_id,
            "parcels": sorted(self.parcels),
            "cells": sorted(self.cells),
        }


@dataclass
class PendingPickup:
    parcel_id: str
    street_id: str
    house_no: int
    queued_event_seq: int
    seal_id: str
    queued_at_moves: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "street_id": self.street_id,
            "house_no": self.house_no,
            "queued_event_seq": self.queued_event_seq,
            "seal_id": self.seal_id,
            "queued_at_moves": self.queued_at_moves,
        }


class CounterSession:
    """A segmentable teller session over one cabinet and one ledger."""

    def __init__(
        self,
        cabinet: ReturnCabinet,
        ledger,
        *,
        sealing_policy: str = POLICY_STEP,
        max_pending: int | None = None,
    ) -> None:
        if sealing_policy not in (POLICY_STEP, POLICY_CYCLE):
            raise CabinetError("sealing_policy must be 'step' or 'cycle'")
        self.cabinet = cabinet
        self.ledger = ledger
        self.sealing_policy = sealing_policy
        # The cabinet limit is the default cap on simultaneously held letters.
        self.max_pending = len(cabinet.cells) if max_pending is None else max_pending
        if self.max_pending < 1:
            raise CabinetError("max_pending must be at least 1")
        self.in_hand: str | None = None
        self.active_seals: dict[str, _Seal] = {}
        self.pending: list[PendingPickup] = []
        self.delivered: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.completed_moves = 0
        self.replan_count = 0
        self._seal_seq = 0
        initial = cabinet.plan_min_moves()
        self.current_plan = initial
        self._record(EVENT_PLANNED, detail=initial.as_dict())

    # -- general state -------------------------------------------------------

    def letter_state(self, parcel_id: str) -> str:
        """Return ``"in_cell"``, ``"in_hand"`` or ``"taken"`` (else unknown)."""
        if parcel_id in self.delivered:
            return "taken"
        if self.in_hand == parcel_id:
            return "in_hand"
        if parcel_id in self.cabinet.placement:
            return "in_cell"
        return "unknown"

    def sealed_parcels(self) -> set[str]:
        parcels: set[str] = set()
        for seal in self.active_seals.values():
            parcels.update(seal.parcels)
        return parcels

    def is_sealed(self, parcel_id: str) -> bool:
        return parcel_id in self.sealed_parcels()

    def face(self) -> dict[str, Any]:
        data = self.cabinet.face()
        data["in_hand"] = self.in_hand
        data["letter_states"] = {
            pid: self.letter_state(pid)
            for pid in sorted(set(self.cabinet.placement) | set(self.delivered) |
                              ({self.in_hand} if self.in_hand else set()))
        }
        data["active_seals"] = [s.as_dict() for s in self.active_seals.values()]
        data["pending_pickups"] = [p.as_dict() for p in self.pending]
        return data

    # -- seals ---------------------------------------------------------------

    def _new_seal_id(self, kind: str) -> str:
        self._seal_seq += 1
        return f"{kind}-seal-{self._seal_seq}"

    def open_cycle_closure(self, parcel_ids: Sequence[str]) -> dict[str, Any]:
        """Seal one target-cycle group plus the empty cell it needs.

        The parcels must form a real cycle: each one's target cell is currently
        occupied by another member of the group, and an empty cell reachable
        from the cycle must exist.  The group size is bounded by the cabinet's
        simultaneously-held letter cap.
        """
        group = list(dict.fromkeys(parcel_ids))
        if not group:
            raise CabinetError("a cycle seal needs at least one letter")
        if len(group) > self.max_pending:
            raise CabinetError(
                f"cycle of {len(group)} exceeds the held-letter cap {self.max_pending}",
            )
        for pid in group:
            if pid not in self.cabinet.placement:
                raise CabinetError(f"{pid} is not in a cabinet cell")
            if self.is_sealed(pid):
                raise CabinetError(f"{pid} is already inside another seal")
        if self.in_hand is not None:
            raise CabinetError("cannot open a cycle seal while a letter is in hand")
        placement = self.cabinet.placement
        targets = self.cabinet.targets
        group_set = set(group)
        # Every target cell must be currently held by another group member.
        for pid in group:
            holder = self.cabinet.letter_at(targets[pid])
            if holder is None or holder not in group_set:
                raise CabinetError(
                    f"{pid} does not form a closed target-cycle with the given letters"
                )
        empties = self.cabinet.empties()
        if not empties:
            raise CabinetError("no empty cell available for the cycle to rotate through")
        cells = {placement[p] for p in group} | {targets[p] for p in group} | set(empties)
        seal = _Seal(SEAL_CYCLE, self._new_seal_id(SEAL_CYCLE), group_set, cells)
        self.active_seals[seal.seal_id] = seal
        self._record(EVENT_SEAL_OPEN, detail=seal.as_dict())
        return seal.as_dict()

    def close_cycle_closure(self, seal_id: str) -> dict[str, Any]:
        seal = self.active_seals.get(seal_id)
        if seal is None or seal.kind != SEAL_CYCLE:
            raise CabinetError(f"no active cycle seal {seal_id}")
        if self.in_hand is not None:
            raise CabinetError("a letter is still in hand; finish the carry first")
        # A cycle seal may only end once every member has reached its target.
        unfinished = [
            p for p in seal.parcels
            if p in self.cabinet.placement and self.cabinet.placement[p] != self.cabinet.targets[p]
        ]
        if unfinished:
            raise CabinetError(f"cycle seal still has unfinished letters: {sorted(unfinished)}")
        return self._close_seal(seal)

    def _close_seal(self, seal: _Seal) -> dict[str, Any]:
        self.active_seals.pop(seal.seal_id)
        self._record(EVENT_SEAL_CLOSE, detail=seal.as_dict())
        served = self._serve_pending_for(seal.seal_id)
        return {"seal": seal.as_dict(), "served": served}

    # -- moves (segmentable) -------------------------------------------------

    def remaining_plan(self) -> MovePlan:
        """Minimum carries from the *current* physical placement."""
        return self.cabinet.plan_min_moves()

    def _next_cycle_group(self) -> list[str] | None:
        """Find an unfinished target-cycle for the cycle sealing policy.

        A group is a cycle when every member's target cell is currently held
        by another member, the chain returns to its start, and an empty cell
        exists to rotate the ring through.
        """
        targets = self.cabinet.targets
        for start in sorted(self.cabinet.placement):
            if self.cabinet.placement[start] == targets[start]:
                continue
            group: list[str] = []
            seen: set[str] = set()
            current = start
            while current not in seen:
                seen.add(current)
                group.append(current)
                holder = self.cabinet.letter_at(targets[current])
                if holder is None:
                    break
                current = holder
            else:
                if current == start and self.cabinet.empties():
                    return group
        return None

    def begin_move(self, parcel_id: str, to_cell: str) -> dict[str, Any]:
        """Take a named letter in hand, opening the minimum required seal."""
        if self.in_hand is not None:
            raise CabinetError(
                f"{self.in_hand} is still in hand; complete that carry first"
            )
        if parcel_id not in self.cabinet.placement:
            raise CabinetError(f"no letter {parcel_id} in a cabinet cell")
        source = self.cabinet.placement[parcel_id]
        # Validate legality before any seal is opened.
        if (source, to_cell) not in self.cabinet.channels:
            raise CabinetError(f"channel {source}->{to_cell} is not allowed")
        if to_cell not in self.cabinet.empties():
            raise CabinetError(f"cell {to_cell} is not empty; stacking is illegal")
        seal_info = None
        if not self.is_sealed(parcel_id):
            if self.sealing_policy == POLICY_CYCLE:
                group = self._next_cycle_group()
                if group is not None and parcel_id in group and len(group) <= self.max_pending:
                    seal_info = self.open_cycle_closure(group)
            if seal_info is None:
                seal = _Seal(
                    SEAL_STEP, self._new_seal_id(SEAL_STEP),
                    {parcel_id}, {source, to_cell},
                )
                self.active_seals[seal.seal_id] = seal
                seal_info = seal.as_dict()
                self._record(EVENT_SEAL_OPEN, detail=seal_info)
        self.in_hand = parcel_id
        self._record(EVENT_MOVE_BEGUN, detail={
            "parcel_id": parcel_id, "from_cell": source, "to_cell": to_cell,
            "seal": seal_info,
        })
        return {"parcel_id": parcel_id, "from_cell": source, "to_cell": to_cell,
                "state": "in_hand", "seal": seal_info}

    def complete_move(self) -> dict[str, Any]:
        """Drop the in-hand letter into the reserved empty target cell."""
        if self.in_hand is None:
            raise CabinetError("no letter is in hand")
        parcel_id = self.in_hand
        move = self.cabinet.apply_move(parcel_id, self._hand_destination(parcel_id))
        self.in_hand = None
        self.completed_moves += 1
        self._record(EVENT_MOVE_DONE, detail=move.as_dict())
        served: list[dict[str, Any]] = []
        # Close the per-step seal immediately; a cycle seal stays until done.
        step_seals = [s for s in self.active_seals.values() if s.kind == SEAL_STEP]
        for seal in step_seals:
            result = self._close_seal(seal)
            served.extend(result["served"])
        cycle_seals = [s for s in self.active_seals.values() if s.kind == SEAL_CYCLE]
        for seal in cycle_seals:
            unfinished = any(
                p in self.cabinet.placement
                and self.cabinet.placement[p] != self.cabinet.targets[p]
                for p in seal.parcels
            )
            if not unfinished:
                result = self._close_seal(seal)
                served.extend(result["served"])
        if self.cabinet.placement and not self.cabinet.is_complete():
            self.current_plan = self.cabinet.plan_min_moves()
        return {"move": move.as_dict(), "served_pickups": served,
                "completed_moves": self.completed_moves}

    def _hand_destination(self, parcel_id: str) -> str:
        # The destination was validated in begin_move; derive it from the
        # latest move_begun event so no second channel can be smuggled in.
        for event in reversed(self.events):
            if event["type"] == EVENT_MOVE_BEGUN and event["detail"]["parcel_id"] == parcel_id:
                return event["detail"]["to_cell"]
        raise CabinetError("no begun carry found")  # pragma: no cover

    # -- pickup slips --------------------------------------------------------

    def request_pickup(
        self, parcel_id: str, street_id: str, house_no: int
    ) -> dict[str, Any]:
        """Handle one counter pickup slip against the issued final doors."""
        verdict = self.ledger.verify_claim(parcel_id, street_id, house_no)
        if not verdict["valid"]:
            result = {**verdict, "action": "rejected"}
            self._record(EVENT_PICKUP_REJECTED, detail=result)
            return result
        state = self.letter_state(parcel_id)
        if state == "taken":
            result = {"parcel_id": parcel_id, "valid": False,
                      "reason": "already_taken", "action": "rejected",
                      "issued": verdict["issued"]}
            self._record(EVENT_PICKUP_REJECTED, detail=result)
            return result
        if state == "unknown":
            result = {"parcel_id": parcel_id, "valid": False,
                      "reason": "not_in_cabinet", "action": "rejected",
                      "issued": verdict["issued"]}
            self._record(EVENT_PICKUP_REJECTED, detail=result)
            return result
        if state == "in_hand":
            # The teller is physically holding this letter: a valid slip is
            # served from the hand and the still-open per-step seal is lifted.
            return self._deliver_in_hand(parcel_id, street_id, house_no)
        if self.is_sealed(parcel_id):
            # One parcel can only be handed over once: a duplicate slip while
            # the same letter is still sealed is refused rather than queued.
            if any(p.parcel_id == parcel_id for p in self.pending):
                result = {"parcel_id": parcel_id, "valid": False,
                          "reason": "pickup_already_pending", "action": "rejected",
                          "issued": verdict["issued"]}
                self._record(EVENT_PICKUP_REJECTED, detail=result)
                return result
            seal_id = next(s.seal_id for s in self.active_seals.values()
                           if parcel_id in s.parcels)
            held = len(self.sealed_parcels())
            if held > self.max_pending:
                raise CabinetError(
                    f"held-letter cap {self.max_pending} exceeded by seal {seal_id}"
                )
            queued = PendingPickup(parcel_id, street_id, house_no,
                                   len(self.events) + 1, seal_id,
                                   self.completed_moves)
            self.pending.append(queued)
            result = {"parcel_id": parcel_id, "valid": True, "action": "pending",
                      "seal_id": seal_id,
                      "queued_at_moves": self.completed_moves,
                      "issued": verdict["issued"]}
            self._record(EVENT_PICKUP_PENDING, detail=result)
            return result
        # Outside every seal: serve immediately, even while another letter is
        # in hand at a different window.
        return self._deliver(parcel_id, street_id, house_no, immediate=True)

    def _deliver(
        self, parcel_id: str, street_id: str, house_no: int, *, immediate: bool,
        waited_moves: int = 0, from_cell: str | None = None,
    ) -> dict[str, Any]:
        cell = self.cabinet.cell_of(parcel_id) if parcel_id in self.cabinet.placement else from_cell
        self.cabinet.remove_letter(parcel_id)
        record = {"parcel_id": parcel_id, "street_id": street_id, "house_no": house_no,
                  "from_cell": cell, "immediate": immediate,
                  "waited_moves": waited_moves}
        self.delivered[parcel_id] = record
        self._record(EVENT_PICKUP_SERVED, detail=record)
        if self.cabinet.placement and not self.cabinet.is_complete():
            before = self.cabinet.plan_min_moves().total_moves
            plan = self._replan_after_pickup()
            self._record(EVENT_REPLAN, detail={
                "parcel_id": parcel_id,
                "remaining_moves_before": before,
                "remaining_moves_after": plan.total_moves,
                "replan_seq": self.replan_count,
            })
        return {"parcel_id": parcel_id, "valid": True, "action": "served",
                "from_cell": cell, "immediate": immediate,
                "waited_moves": waited_moves,
                "completed_moves_on_ledger": self.completed_moves}

    def _replan_after_pickup(self) -> MovePlan:
        plan = self.cabinet.plan_min_moves()
        self.current_plan = plan
        self.replan_count += 1
        return plan

    def _deliver_in_hand(self, parcel_id: str, street_id: str, house_no: int) -> dict[str, Any]:
        """Hand a carried letter straight to the counter while it is in hand."""
        begun = next(
            (e["detail"] for e in reversed(self.events)
             if e["type"] == EVENT_MOVE_BEGUN and e["detail"]["parcel_id"] == parcel_id),
            None,
        )
        from_cell = begun["from_cell"] if begun else None
        # This carry ends at the counter instead of the reserved cell; no
        # placement move is completed, so close its per-step seal without it.
        step_seals = [s for s in self.active_seals.values() if s.kind == SEAL_STEP]
        for seal in step_seals:
            if parcel_id in seal.parcels:
                self._close_seal(seal)
        self.in_hand = None
        # The letter was removed from its cell when the carry began; drop its
        # target as well so the remaining job is re-planned without it.
        self.cabinet.remove_letter(parcel_id)
        self._record("move_cancelled", detail={
            "parcel_id": parcel_id, "from_cell": from_cell,
            "reason": "served_while_in_hand"})
        record = {"parcel_id": parcel_id, "street_id": street_id, "house_no": house_no,
                  "from_cell": from_cell, "immediate": True, "waited_moves": 0,
                  "in_hand": True}
        self.delivered[parcel_id] = record
        self._record(EVENT_PICKUP_SERVED, detail=record)
        if self.cabinet.placement and not self.cabinet.is_complete():
            plan = self._replan_after_pickup()
            self._record(EVENT_REPLAN, detail={
                "parcel_id": parcel_id,
                "remaining_moves_after": plan.total_moves,
                "replan_seq": self.replan_count,
            })
        return {"parcel_id": parcel_id, "valid": True, "action": "served",
                "from_cell": from_cell, "immediate": True, "in_hand": True,
                "waited_moves": 0,
                "completed_moves_on_ledger": self.completed_moves}

    def _serve_pending_for(self, seal_id: str) -> list[dict[str, Any]]:
        due = [p for p in self.pending if p.seal_id == seal_id]
        served: list[dict[str, Any]] = []
        for pickup in due:
            self.pending.remove(pickup)
            # The waiting cost is the number of carries performed while this
            # slip sat queued behind the seal.
            waited = self.completed_moves - pickup.queued_at_moves
            actual_cell = self.cabinet.cell_of(pickup.parcel_id)
            result = self._deliver(
                pickup.parcel_id, pickup.street_id, pickup.house_no,
                immediate=False, waited_moves=waited, from_cell=actual_cell,
            )
            served.append(result)
        return served

    # -- batch execution with interruptions ---------------------------------

    def run_moves(
        self,
        max_steps: int | None = None,
        *,
        interrupts: Sequence[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Execute planned carries, optionally inserting pickup slips.

        ``interrupts`` is a list of ``{"after_step": int, "pickup": (pid, sid, no)}``
        entries; ``after_step`` counts completed carries (0 = before the first
        carry).  The minimum plan is always re-derived from the live face.
        """
        interrupts = list(interrupts or [])
        handled: list[dict[str, Any]] = []
        steps_done = 0
        while not self.cabinet.is_complete() and self.cabinet.placement:
            due = [it for it in interrupts if it["after_step"] == steps_done]
            for it in due:
                pid, sid, no = it["pickup"]
                handled.append(self.request_pickup(pid, sid, no))
            interrupts = [it for it in interrupts if it not in due]
            if max_steps is not None and steps_done >= max_steps:
                break
            if not self.cabinet.placement or self.cabinet.is_complete():
                break
            plan = self.cabinet.plan_min_moves()
            if not plan.solvable:
                return {"completed_moves": self.completed_moves, "plan": plan.as_dict(),
                        "interrupt_results": handled, "pickups": self.pickup_summary()}
            first = plan.moves[0]
            self.begin_move(first.parcel_id, first.to_cell)
            self.complete_move()
            steps_done += 1
        # Any interrupt scheduled at the final step boundary.
        for it in [it for it in interrupts if it["after_step"] == steps_done]:
            pid, sid, no = it["pickup"]
            handled.append(self.request_pickup(pid, sid, no))
        return {"completed_moves": self.completed_moves,
                "plan": self.cabinet.plan_min_moves().as_dict(),
                "interrupt_results": handled,
                "pickups": self.pickup_summary()}

    # -- ledger --------------------------------------------------------------

    def _record(self, event_type: str, detail: dict[str, Any]) -> None:
        self.events.append({"seq": len(self.events) + 1, "type": event_type,
                            "detail": detail})

    def pickup_summary(self) -> dict[str, Any]:
        served = [e["detail"] for e in self.events if e["type"] == EVENT_PICKUP_SERVED]
        pending = [e["detail"] for e in self.events if e["type"] == EVENT_PICKUP_PENDING]
        rejected = [e["detail"] for e in self.events if e["type"] == EVENT_PICKUP_REJECTED]
        total_wait = sum(item.get("waited_moves", 0) for item in served)
        return {
            "served": served,
            "pending": pending,
            "rejected": rejected,
            "served_count": len(served),
            "pending_count": len(self.pending),
            "rejected_count": len(rejected),
            "total_waiting_moves": total_wait,
            "replan_count": self.replan_count,
            "completed_moves": self.completed_moves,
        }

    def ledger(self) -> dict[str, Any]:
        return {
            "sealing_policy": self.sealing_policy,
            "max_pending": self.max_pending,
            "events": self.events,
            "summary": self.pickup_summary(),
            "face": self.face(),
        }

    # -- persistence for segmented programs ---------------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "cabinet": self.cabinet.snapshot(),
            "sealing_policy": self.sealing_policy,
            "max_pending": self.max_pending,
            "in_hand": self.in_hand,
            "active_seals": [
                {"kind": s.kind, "seal_id": s.seal_id,
                 "parcels": sorted(s.parcels), "cells": sorted(s.cells)}
                for s in self.active_seals.values()
            ],
            "pending": [p.as_dict() for p in self.pending],
            "delivered": self.delivered,
            "events": self.events,
            "completed_moves": self.completed_moves,
            "replan_count": self.replan_count,
            "_seal_seq": self._seal_seq,
        }

    @classmethod
    def restore(cls, data: dict[str, Any], ledger) -> "CounterSession":
        cabinet = ReturnCabinet.restore(data["cabinet"])
        session = cls(cabinet, ledger,
                      sealing_policy=data["sealing_policy"],
                      max_pending=data["max_pending"])
        session.in_hand = data["in_hand"]
        session.active_seals = {
            s["seal_id"]: _Seal(s["kind"], s["seal_id"], set(s["parcels"]), set(s["cells"]))
            for s in data["active_seals"]
        }
        session.pending = [PendingPickup(**p) for p in data["pending"]]
        session.delivered = dict(data["delivered"])
        session.events = list(data["events"])
        session.completed_moves = data["completed_moves"]
        session.replan_count = data["replan_count"]
        session._seal_seq = data["_seal_seq"]
        session.current_plan = cabinet.plan_min_moves()
        return session
