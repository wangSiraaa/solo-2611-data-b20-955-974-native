"""Return-mail cabinet: parcel-to-cell placement and minimum move planning.

A cabinet has at most :data:`MAX_CELLS` cells and holds at most
:data:`MAX_LETTERS` letters.  Movement is restricted to the cabinet's
*directed* channels and one move may only carry one named letter along one
channel into a cell that is empty at that moment.  Stacking, swaps and
renumbering during a move are all impossible by construction.

Each letter has exactly one target cell and every target cell within one job
is unique.  :meth:`ReturnCabinet.plan_min_moves` performs a breadth-first
search over placements, so the returned plan is a provable minimum; every
step records the empty cells seen before and after the move.  When no plan
exists a structured, reviewable reason is returned instead.

The cabinet itself is deliberately unaware of renumbering history and pickup
slips: it only knows physical letters, cells and channels.  The counter
session in :mod:`address_delivery.counter` layers the business rules on top.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .errors import CabinetError

MAX_CELLS = 8
MAX_LETTERS = 6


@dataclass(frozen=True)
class Move:
    """One legal carry: *parcel_id* along one channel into a then-empty cell."""

    seq: int
    parcel_id: str
    from_cell: str
    to_cell: str
    empties_before: tuple[str, ...]
    empties_after: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "parcel_id": self.parcel_id,
            "from_cell": self.from_cell,
            "to_cell": self.to_cell,
            "empties_before": list(self.empties_before),
            "empties_after": list(self.empties_after),
        }


@dataclass(frozen=True)
class MovePlan:
    """A minimum-move plan, or a documented failure to find one."""

    solvable: bool
    moves: tuple[Move, ...] = ()
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def total_moves(self) -> int:
        return len(self.moves)

    def as_dict(self) -> dict[str, Any]:
        return {
            "solvable": self.solvable,
            "total_moves": self.total_moves,
            "moves": [m.as_dict() for m in self.moves],
            "reason": self.reason,
            "detail": self.detail,
        }


class ReturnCabinet:
    """An in-memory cabinet with directed channels and unique target cells."""

    def __init__(
        self,
        cells: Iterable[str],
        channels: Iterable[tuple[str, str]],
        placement: dict[str, str],
        targets: dict[str, str],
    ) -> None:
        self.cells = tuple(dict.fromkeys(cells))
        if not self.cells:
            raise CabinetError("a cabinet needs at least one cell")
        if len(self.cells) > MAX_CELLS:
            raise CabinetError(f"at most {MAX_CELLS} cells are supported")
        self._cell_set = set(self.cells)
        self.channels = self._normalise_channels(channels)
        self._validate_placement_and_targets(placement, targets)
        # parcel_id -> cell
        self.placement: dict[str, str] = dict(placement)
        # parcel_id -> its unique target cell
        self.targets: dict[str, str] = dict(targets)

    # -- construction / validation ------------------------------------------

    def _normalise_channels(self, channels: Iterable[tuple[str, str]]) -> set[tuple[str, str]]:
        normalised: set[tuple[str, str]] = set()
        for edge in channels:
            if not isinstance(edge, (tuple, list)) or len(edge) != 2:
                raise CabinetError(f"channel must be a (from,to) pair, got {edge!r}")
            a, b = str(edge[0]), str(edge[1])
            if a not in self._cell_set or b not in self._cell_set:
                raise CabinetError(f"channel {a}->{b} references an unknown cell")
            normalised.add((a, b))
        return normalised

    def _validate_placement_and_targets(
        self, placement: dict[str, str], targets: dict[str, str]
    ) -> None:
        if not isinstance(placement, dict) or not isinstance(targets, dict):
            raise CabinetError("placement and targets must be dicts")
        if len(placement) > MAX_LETTERS or len(targets) > MAX_LETTERS:
            raise CabinetError(f"at most {MAX_LETTERS} letters are supported")
        occupied: list[str] = []
        for parcel_id, cell in placement.items():
            if not isinstance(parcel_id, str) or not parcel_id:
                raise CabinetError("parcel ids must be non-empty strings")
            if cell not in self._cell_set:
                raise CabinetError(f"parcel {parcel_id} occupies unknown cell {cell}")
            occupied.append(cell)
        if len(set(occupied)) != len(occupied):
            dupes = sorted({c for c in occupied if occupied.count(c) > 1})
            raise CabinetError(f"two letters occupy the same cell: {dupes}")
        if set(placement) != set(targets):
            raise CabinetError("every present letter needs one target and vice versa")
        target_cells = list(targets.values())
        if len(set(target_cells)) != len(target_cells):
            dupes = sorted({c for c in target_cells if target_cells.count(c) > 1})
            raise CabinetError(f"target cells must be unique, duplicates: {dupes}")
        for parcel_id, cell in targets.items():
            if cell not in self._cell_set:
                raise CabinetError(f"parcel {parcel_id} targets unknown cell {cell}")

    # -- face queries (the old cabinet-face lookup is kept) ------------------

    def letter_at(self, cell: str) -> str | None:
        for parcel_id, occupied in self.placement.items():
            if occupied == cell:
                return parcel_id
        return None

    def cell_of(self, parcel_id: str) -> str | None:
        return self.placement.get(parcel_id)

    def empties(self) -> tuple[str, ...]:
        occupied = set(self.placement.values())
        return tuple(c for c in self.cells if c not in occupied)

    def face(self) -> dict[str, Any]:
        """The full current cabinet face: every cell and what it holds."""
        return {
            "cells": list(self.cells),
            "channels": [list(edge) for edge in sorted(self.channels)],
            "cells_face": [
                {"cell": cell, "parcel_id": self.letter_at(cell),
                 "target_of": self._target_owner(cell)}
                for cell in self.cells
            ],
            "empties": list(self.empties()),
            "letters": [
                {"parcel_id": pid, "cell": self.placement[pid],
                 "target": self.targets[pid], "in_place": self.placement[pid] == self.targets[pid]}
                for pid in sorted(self.placement)
            ],
        }

    def _target_owner(self, cell: str) -> str | None:
        for parcel_id, target in self.targets.items():
            if target == cell:
                return parcel_id
        return None

    # -- physical moves ------------------------------------------------------

    def legal_moves(self) -> list[tuple[str, str, str]]:
        """All currently legal ``(parcel_id, from_cell, to_cell)`` carries."""
        empty = set(self.empties())
        moves: list[tuple[str, str, str]] = []
        for parcel_id in sorted(self.placement):
            source = self.placement[parcel_id]
            for _a, dest in sorted(self.channels):
                if _a == source and dest in empty:
                    moves.append((parcel_id, source, dest))
        return moves

    def apply_move(self, parcel_id: str, to_cell: str) -> Move:
        """Validate and perform one move; returns the recorded :class:`Move`."""
        if parcel_id not in self.placement:
            raise CabinetError(f"no letter {parcel_id} in the cabinet")
        source = self.placement[parcel_id]
        if to_cell not in self._cell_set:
            raise CabinetError(f"unknown cell {to_cell}")
        if (source, to_cell) not in self.channels:
            raise CabinetError(
                f"channel {source}->{to_cell} is not allowed for {parcel_id}"
            )
        if to_cell not in self.empties():
            raise CabinetError(
                f"cell {to_cell} is occupied by {self.letter_at(to_cell)}; stacking is illegal"
            )
        before = self.empties()
        self.placement[parcel_id] = to_cell
        after = self.empties()
        return Move(0, parcel_id, source, to_cell, before, after)

    def remove_letter(self, parcel_id: str) -> str:
        """Take a letter out of the cabinet (a real counter pickup)."""
        if parcel_id not in self.placement:
            raise CabinetError(f"no letter {parcel_id} in the cabinet")
        cell = self.placement.pop(parcel_id)
        self.targets.pop(parcel_id)
        return cell

    def is_complete(self) -> bool:
        return all(self.placement[p] == self.targets[p] for p in self.placement)

    # -- planning ------------------------------------------------------------

    def plan_min_moves(self) -> MovePlan:
        """BFS for the minimum number of legal carries to place every letter."""
        ordered = tuple(sorted(self.placement))
        start = tuple(self.placement[p] for p in ordered)
        goal = tuple(self.targets[p] for p in ordered)
        if start == goal:
            return MovePlan(True, ())
        if not self.empties():
            return MovePlan(
                False, (), "no_empty_cell",
                {"message": "every cell is occupied; no legal first carry exists",
                 "empties": []},
            )
        # Static reachability ignoring occupancy gives a reviewable hint.
        reach = self._reachability()
        blocked = []
        for parcel_id in ordered:
            if self.targets[parcel_id] not in reach[self.placement[parcel_id]]:
                blocked.append({"parcel_id": parcel_id,
                                "from_cell": self.placement[parcel_id],
                                "target_cell": self.targets[parcel_id]})
        if blocked:
            return MovePlan(
                False, (), "unreachable",
                {"message": "target cannot be reached along the directed channels",
                 "blocked_letters": blocked, "states_explored": 0},
            )

        outgoing = self._outgoing_by_cell()
        frontier: deque[tuple[tuple[str, ...], int]] = deque([(start, 0)])
        parents: dict[tuple[str, ...], tuple[tuple[str, ...], str, str, str] | None] = {start: None}
        explored = 0
        while frontier:
            state, depth = frontier.popleft()
            explored += 1
            occupied = set(state)
            empty_cells = [c for c in self.cells if c not in occupied]
            for index, source in enumerate(state):
                for dest in outgoing.get(source, ()):  # directed channels only
                    if dest in occupied:
                        continue
                    new_state = list(state)
                    new_state[index] = dest
                    new_state = tuple(new_state)
                    if new_state in parents:
                        continue
                    parents[new_state] = (state, ordered[index], source, dest)
                    if new_state == goal:
                        return MovePlan(True, self._rebuild(parents, goal, ordered))
                    frontier.append((new_state, depth + 1))
        return MovePlan(
            False, (), "unreachable",
            {"message": "BFS exhausted every reachable placement without meeting the targets",
             "blocked_letters": [], "states_explored": explored},
        )

    def _outgoing_by_cell(self) -> dict[str, tuple[str, ...]]:
        out: dict[str, list[str]] = {c: [] for c in self.cells}
        for a, b in self.channels:
            out[a].append(b)
        return {c: tuple(sorted(v)) for c, v in out.items()}

    def _reachability(self) -> dict[str, set[str]]:
        out = self._outgoing_by_cell()
        reach: dict[str, set[str]] = {}
        for start in self.cells:
            seen = {start}
            queue = deque([start])
            while queue:
                cell = queue.popleft()
                for nxt in out.get(cell, ()):
                    if nxt not in seen:
                        seen.add(nxt)
                        queue.append(nxt)
            reach[start] = seen
        return reach

    def _rebuild(self, parents, goal, ordered) -> tuple[Move, ...]:
        reverse: list[tuple[str, str, str]] = []
        state = goal
        while parents[state] is not None:
            parent, parcel_id, source, dest = parents[state]
            reverse.append((parcel_id, source, dest))
            state = parent
        reverse.reverse()
        # Replay forward from the real start placement to capture the
        # empty-cell record before and after every carry.
        moves: list[Move] = []
        current = {p: self.placement[p] for p in ordered}
        for seq, (parcel_id, source, dest) in enumerate(reverse, start=1):
            before = tuple(c for c in self.cells if c not in current.values())
            assert current[parcel_id] == source
            current[parcel_id] = dest
            after = tuple(c for c in self.cells if c not in current.values())
            moves.append(Move(seq, parcel_id, source, dest, before, after))
        return tuple(moves)

    # -- serialisation for segmented teller programs ------------------------

    def snapshot(self) -> dict[str, Any]:
        return {
            "cells": list(self.cells),
            "channels": [list(edge) for edge in sorted(self.channels)],
            "placement": dict(self.placement),
            "targets": dict(self.targets),
        }

    @classmethod
    def restore(cls, data: dict[str, Any]) -> "ReturnCabinet":
        return cls(
            data["cells"],
            (tuple(edge) for edge in data["channels"]),
            dict(data["placement"]),
            dict(data["targets"]),
        )
