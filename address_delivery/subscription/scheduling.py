"""Exact scheduler for the single physical paper issued each period.

One paper exists per issue.  A household may receive it only when it has a
**positive** period entitlement **and** its period box is open; households
sharing one box still hold their shares separately and are separate
candidates.  If nobody is eligible the issue is explicitly *unschedulable*
rather than delivered somewhere invalid.

The objective is lexicographic on the exact rationals:

    minimise   max over period t and household h of | G_h(t) - E_h(t) |

where ``G_h(t)`` is the integer number of papers h has actually received
through t and ``E_h(t)`` is its accumulated fractional entitlement through t.
A household with no entitlement in a period contributes zero and may receive
early to balance later ("可先多拿以后平衡").

The solver is an exact dynamic program over cumulative balance vectors.  With
at most four stable households and thirty periods the reachable state set is
small; states are Pareto-pruned on the balance tuple and ties broken
deterministically (smallest lexicographic recipient sequence), so the optimum
is reproducible even when several plans tie.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from .errors import CalendarError, SchedulingError
from .ledger import MAX_PERIODS, SubscriptionLedger
from .rationals import Q, frac_to_text


@dataclass(frozen=True)
class PlannedIssue:
    """One issue's dispatch decision and the balances it leaves behind."""

    period: int
    recipient: str | None  # None only when the issue was unschedulable
    box_id: str | None
    closed_boxes: tuple[str, ...]
    eligible: tuple[str, ...]
    # Cumulative received minus cumulative entitlement, per household, after t.
    balances: dict[str, Q]
    period_peak: Q
    note: str = ""

    @property
    def schedulable(self) -> bool:
        return self.recipient is not None

    def as_dict(self) -> dict:
        return {
            "period": self.period,
            "recipient": self.recipient,
            "box_id": self.box_id,
            "closed_boxes": list(self.closed_boxes),
            "eligible": list(self.eligible),
            "schedulable": self.schedulable,
            "balances": {h: frac_to_text(v) for h, v in sorted(self.balances.items())},
            "period_peak": frac_to_text(self.period_peak),
            "note": self.note,
        }


@dataclass(frozen=True)
class IssuePlan:
    periods: tuple[int, ...]
    households: tuple[str, ...]
    issues: tuple[PlannedIssue, ...]
    max_deviation: Q
    blocked_periods: tuple[int, ...]
    issued_through: int

    def as_dict(self) -> dict:
        return {
            "periods": list(self.periods),
            "households": list(self.households),
            "max_deviation": frac_to_text(self.max_deviation),
            "blocked_periods": list(self.blocked_periods),
            "issued_through": self.issued_through,
            "issues": [i.as_dict() for i in self.issues],
        }


def make_box_calendar(
    ledger: SubscriptionLedger, periods: Sequence[int]
) -> dict[int, set[str]]:
    """Build the ``period -> closed box set`` calendar from ledger closures."""
    return {period: set(ledger.closed_boxes(period)) for period in periods}


@dataclass
class _Node:
    """A DP frontier node: cumulative balance vector and its peak so far.

    Two nodes are merged only when their signed balance tuples are identical.
    We deliberately do NOT prune distinct tuples against each other: the
    objective is on absolute values, so two tuples with the same component
    magnitudes but opposite signs (e.g. ``(1/2,-1/2)`` vs ``(-1/2,1/2)``) react
    differently to the next issue and are both needed for exact optimality.
    The reachable set stays small - at most the weak-composition count of
    papers across four households over thirty periods.
    """

    balances: tuple[Q, ...]
    peak: Q
    recipients: tuple[str | None, ...]


class Scheduler:
    """Computes optimal issue plans from a :class:`SubscriptionLedger`."""

    def __init__(self, ledger: SubscriptionLedger) -> None:
        self.ledger = ledger

    # -- public API ----------------------------------------------------------

    def schedule(
        self,
        periods: Sequence[int],
        *,
        box_calendar: Mapping[int, set[str]] | None = None,
        issued_through: int = 0,
        forced_recipients: Mapping[int, str] | None = None,
    ) -> IssuePlan:
        """Return an optimal plan over *periods* (consecutive 1-based issues).

        Parameters
        ----------
        periods:
            Consecutive issue numbers, at most :data:`MAX_PERIODS`.
        box_calendar:
            Explicit ``period -> closed box ids``; defaults to the closures
            registered on the ledger.  Closed boxes never erase entitlements.
        issued_through:
            Issues up to and including this period are already printed sample
            issues.  Their dispatch is frozen and replanning may only change
            later periods.
        forced_recipients:
            The actual recipients used on the frozen prefix; required for every
            frozen period and validated against that period's eligibility.
        """
        periods = self._validate_periods(periods)
        if issued_through < 0 or issued_through > periods[-1]:
            raise SchedulingError("issued_through must lie inside the plan horizon")
        forced = dict(forced_recipients or {})
        if issued_through and set(forced) != {p for p in periods if p <= issued_through}:
            raise SchedulingError(
                "every already-issued period needs its frozen recipient in forced_recipients"
            )
        households = tuple(self.ledger.household_universe(periods))
        entitlements = self._entitlement_matrix(periods, households)
        boxes = self._box_matrix(periods, households)
        calendar = self._normalise_calendar(periods, box_calendar)

        zero = tuple(Q(0) for _ in households)
        # frontier maps an exact signed balance tuple to its best node.  Distinct
        # tuples are never merged; only identical tuples collapse, keeping the
        # lexicographically smallest recipient sequence for reproducible ties.
        frontier: dict[tuple[Q, ...], _Node] = {
            zero: _Node(zero, Q(0), ())
        }
        issue_facts: list[tuple[int, tuple[str, ...], tuple[str, ...]]] = []
        blocked: list[int] = []

        for period in periods:
            closed = calendar.get(period, set())
            eligible = tuple(
                hh
                for idx, hh in enumerate(households)
                if entitlements[period][idx] > 0 and boxes[period][idx] not in closed
            )
            choices = self._choices_for(period, eligible, issued_through, forced)
            issue_facts.append((period, tuple(sorted(closed)), eligible))
            if not choices:
                # Nobody can receive the unique paper: the issue cannot be排.
                # No paper moves, so every frontier node simply accrues its
                # (negative) entitlement drift; the block is reported separately.
                blocked.append(period)
                drifted: dict[tuple[Q, ...], _Node] = {}
                for node in frontier.values():
                    balances = tuple(
                        node.balances[j] - entitlements[period][j]
                        for j in range(len(households))
                    )
                    peak = max(node.peak, max(abs(v) for v in balances))
                    self._offer(
                        drifted, _Node(balances, peak, node.recipients + (None,))
                    )
                frontier = drifted
                continue

            new_frontier: dict[tuple[Q, ...], _Node] = {}
            for node in frontier.values():
                for recipient in choices:
                    idx = households.index(recipient)
                    balances = tuple(
                        node.balances[j]
                        + (Q(1) if j == idx else Q(0))
                        - entitlements[period][j]
                        for j in range(len(households))
                    )
                    peak = max(node.peak, max(abs(v) for v in balances))
                    candidate = _Node(
                        balances, peak, node.recipients + (recipient,)
                    )
                    self._offer(new_frontier, candidate)
            frontier = new_frontier

        best = self._best_node(frontier)
        issues: list[PlannedIssue] = []
        # Replay the winning recipient sequence with running balances so the
        # reported per-issue balances match the optimum exactly, including
        # across unschedulable issues.
        running = list(zero)
        for (period, closed, eligible), recipient in zip(issue_facts, best.recipients):
            if recipient is None:
                running = [
                    running[j] - entitlements[period][j]
                    for j in range(len(households))
                ]
                box_id = None
                note = "no eligible household: single paper cannot be assigned"
            else:
                idx = households.index(recipient)
                running = [
                    running[j]
                    + (Q(1) if j == idx else Q(0))
                    - entitlements[period][j]
                    for j in range(len(households))
                ]
                box_id = boxes[period][idx]
                note = ""
            peak_here = max(abs(v) for v in running)
            issues.append(
                PlannedIssue(
                    period, recipient, box_id, closed, eligible,
                    self._balance_dict(households, tuple(running)),
                    peak_here, note,
                )
            )

        return IssuePlan(
            tuple(periods),
            households,
            tuple(issues),
            best.peak,
            tuple(blocked),
            issued_through,
        )

    def replan(
        self,
        periods: Sequence[int],
        issued_through: int,
        forced_recipients: Mapping[int, str],
        *,
        box_calendar: Mapping[int, set[str]] | None = None,
    ) -> IssuePlan:
        """Re-optimise unissued periods while frozen sample issues stay verbatim."""
        return self.schedule(
            periods,
            box_calendar=box_calendar,
            issued_through=issued_through,
            forced_recipients=forced_recipients,
        )

    # -- internals -----------------------------------------------------------

    def _validate_periods(self, periods: Sequence[int]) -> list[int]:
        periods = list(periods)
        if not periods:
            raise SchedulingError("at least one period is required")
        if len(periods) > MAX_PERIODS:
            raise SchedulingError(f"a plan covers at most {MAX_PERIODS} periods")
        if periods != list(range(periods[0], periods[0] + len(periods))):
            raise SchedulingError("periods must be consecutive starting at period 1")
        if periods[0] != 1:
            raise SchedulingError("a plan horizon starts at period 1")
        return periods

    def _entitlement_matrix(
        self, periods: Sequence[int], households: Sequence[str]
    ) -> dict[int, tuple[Q, ...]]:
        table = self.ledger.entitlement_table(periods)
        matrix: dict[int, dict[str, Q]] = {
            period: {hh: Q(0) for hh in households} for period in periods
        }
        for allocation in table:
            matrix[allocation.period][allocation.household_id] = allocation.share
        return {
            period: tuple(matrix[period][hh] for hh in households)
            for period in periods
        }

    def _box_matrix(
        self, periods: Sequence[int], households: Sequence[str]
    ) -> dict[int, tuple[str | None, ...]]:
        table = self.ledger.entitlement_table(periods)
        matrix: dict[int, dict[str, str]] = {
            period: {} for period in periods
        }
        for allocation in table:
            matrix[allocation.period][allocation.household_id] = allocation.box_id
        return {
            period: tuple(matrix[period].get(hh) for hh in households)
            for period in periods
        }

    def _normalise_calendar(
        self, periods: Sequence[int], calendar: Mapping[int, set[str]] | None
    ) -> dict[int, set[str]]:
        if calendar is None:
            return make_box_calendar(self.ledger, periods)
        unknown_periods = sorted(set(calendar) - set(periods))
        if unknown_periods:
            raise CalendarError(f"closure calendar mentions unknown periods {unknown_periods}")
        return {period: set(calendar.get(period, set())) for period in periods}

    def _choices_for(
        self,
        period: int,
        eligible: Sequence[str],
        issued_through: int,
        forced: Mapping[int, str],
    ) -> tuple[str, ...]:
        if period > issued_through:
            return tuple(eligible)
        frozen = forced.get(period)
        if frozen not in eligible:
            raise SchedulingError(
                f"frozen recipient {frozen!r} for issued period {period} is not "
                f"eligible now (eligible={list(eligible)}); printed sample data is "
                "immutable and cannot be re-derived"
            )
        return (frozen,)

    @staticmethod
    def _offer(frontier: dict[tuple[Q, ...], "_Node"], candidate: "_Node") -> None:
        existing = frontier.get(candidate.balances)
        if existing is None or (
            candidate.peak,
            candidate.recipients,
        ) < (existing.peak, existing.recipients):
            frontier[candidate.balances] = candidate

    @staticmethod
    def _best_node(frontier: dict[tuple[Q, ...], "_Node"]) -> "_Node":
        return min(
            frontier.values(),
            key=lambda node: (node.peak, node.recipients, node.balances),
        )

    @staticmethod
    def _balance_dict(households: Sequence[str], balances: Sequence[Q]) -> dict[str, Q]:
        return {hh: value for hh, value in zip(households, balances)}
