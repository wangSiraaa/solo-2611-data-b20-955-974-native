"""Subscription ledger: anchoring, point-in-time box registry, split batches.

The ledger owns the subscription-side tables only; it reads frozen parcel
facts from :class:`~address_delivery.delivery.DeliveryStore` and never writes
the ``jobs``/``attempts`` tables.  Every quantity is an exact
:class:`~fractions.Fraction`.

Period convention
-----------------
Periods are 1-based.  A split batch *effective from period* ``p`` is applied to
the period-``p`` opening balances **before** the period-``p`` paper is
dispatched.  At most one batch may be effective at a given period, and a batch
is a single, simultaneous distribution over the opening snapshot: proceeds
created inside the batch cannot be split again inside the same batch.

Immutability
------------
Anchors, box registrations, split batches/entries and the per-period
allocations they produce are append-only facts.  A later batch never rewrites
an earlier period's entitlement or a departed household's history - new facts
take effect from their own period.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping, Sequence

from ..db import transaction
from ..delivery import DeliveryStore, SUCCESS_OUTCOMES
from .errors import AnchorError, CalendarError, SchedulingError, SplitError
from .rationals import Q, frac_from_text, frac_to_text

#: Problem bound: at most four stable household identifiers per plan horizon.
MAX_HOUSEHOLDS = 4
#: Problem bound: at most thirty issues scheduled in one plan.
MAX_PERIODS = 30

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sub_box_registry (
    box_id        TEXT NOT NULL,
    street_id     TEXT NOT NULL,
    house_no      INTEGER NOT NULL,
    box_label     TEXT NOT NULL,
    effective_from_version INTEGER NOT NULL,
    effective_to_version   INTEGER,
    note          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (box_id, effective_from_version)
);
CREATE TABLE IF NOT EXISTS sub_roots (
    root_id       TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL,
    parcel_id     TEXT NOT NULL,
    street_id     TEXT NOT NULL,
    house_no      INTEGER NOT NULL,
    box_id        TEXT NOT NULL,
    initial_share TEXT NOT NULL,
    catalog_version        INTEGER NOT NULL,
    parcel_outcome         TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    anchored_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_split_batches (
    batch_id          TEXT PRIMARY KEY,
    effective_period  INTEGER NOT NULL UNIQUE,
    note              TEXT NOT NULL DEFAULT '',
    applied_at        TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sub_split_entries (
    entry_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL REFERENCES sub_split_batches(batch_id),
    old_household TEXT NOT NULL,
    new_household TEXT NOT NULL,
    ratio_text    TEXT NOT NULL,
    box_id        TEXT
);
CREATE TABLE IF NOT EXISTS sub_allocations (
    period        INTEGER NOT NULL,
    household_id  TEXT NOT NULL,
    box_id        TEXT NOT NULL,
    share_text    TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    batch_id      TEXT,
    PRIMARY KEY (period, household_id)
);
CREATE TABLE IF NOT EXISTS sub_box_closures (
    period        INTEGER NOT NULL,
    box_id        TEXT NOT NULL,
    note          TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (period, box_id)
);
"""


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BoxRegistration:
    """A named mailbox registration as it stood at a catalog version.

    ``effective_to_version`` is ``None`` while the registration is current.
    The same physical box may be re-registered under a new label at a later
    version; the row that was current at anchoring time is the one used.
    """

    box_id: str
    street_id: str
    house_no: int
    box_label: str
    effective_from_version: int
    effective_to_version: int | None = None
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "box_id": self.box_id,
            "street_id": self.street_id,
            "house_no": self.house_no,
            "box_label": self.box_label,
            "effective_from_version": self.effective_from_version,
            "effective_to_version": self.effective_to_version,
            "note": self.note,
        }


@dataclass(frozen=True)
class OriginAnchor:
    """Why a root subscription is credited to one original household.

    The evidence points at a *successful* parcel and the box registration that
    was current at the parcel's frozen catalog version.  No current-coverage
    lookup is performed, so a later same-name street re-coverage cannot change
    the anchor.
    """

    root_id: str
    household_id: str
    parcel_id: str
    street_id: str
    house_no: int
    box_id: str
    initial_share: Q
    catalog_version: int
    parcel_outcome: str
    evidence: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "root_id": self.root_id,
            "household_id": self.household_id,
            "parcel_id": self.parcel_id,
            "street_id": self.street_id,
            "house_no": self.house_no,
            "box_id": self.box_id,
            "initial_share": frac_to_text(self.initial_share),
            "catalog_version": self.catalog_version,
            "parcel_outcome": self.parcel_outcome,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class SplitEntry:
    """One line of a split batch: ``ratio`` of one old household's opening share."""

    old_household: str
    new_household: str
    ratio: Q
    box_id: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "old_household": self.old_household,
            "new_household": self.new_household,
            "ratio": frac_to_text(self.ratio),
            "box_id": self.box_id,
        }


@dataclass(frozen=True)
class SplitBatch:
    batch_id: str
    effective_period: int
    entries: tuple[SplitEntry, ...]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "effective_period": self.effective_period,
            "note": self.note,
            "entries": [e.as_dict() for e in self.entries],
        }


@dataclass(frozen=True)
class PeriodAllocation:
    """One household's exact period entitlement and where every unit came from."""

    period: int
    household_id: str
    box_id: str
    share: Q
    provenance: tuple[tuple[str, Q], ...]
    batch_id: str | None

    @property
    def provenance_dict(self) -> dict[str, Q]:
        return dict(self.provenance)

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period,
            "household_id": self.household_id,
            "box_id": self.box_id,
            "share": frac_to_text(self.share),
            "provenance": [
                {"root_id": root, "amount": frac_to_text(amount)}
                for root, amount in self.provenance
            ],
            "batch_id": self.batch_id,
        }


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class SubscriptionLedger:
    """Append-only subscription store sharing a connection with the job store."""

    def __init__(self, conn, delivery: DeliveryStore, *, init_schema: bool = True) -> None:
        self.conn = conn
        self.delivery = delivery
        if init_schema:
            self.conn.executescript(_SCHEMA)

    # -- box registry --------------------------------------------------------

    def register_box(
        self,
        box_id: str,
        street_id: str,
        house_no: int,
        box_label: str,
        effective_from_version: int,
        *,
        note: str = "",
    ) -> BoxRegistration:
        """Register (or re-register) a named box from a catalog version onward.

        Re-registering a ``box_id`` closes its previous row at
        ``effective_from_version - 1`` so point-in-time lookups remain unique.
        """
        if not isinstance(house_no, int) or isinstance(house_no, bool) or house_no <= 0:
            raise CalendarError("house_no must be a positive integer")
        if not isinstance(effective_from_version, int) or effective_from_version < 1:
            raise CalendarError("effective_from_version must be a positive integer")
        with transaction(self.conn):
            self.conn.execute(
                "UPDATE sub_box_registry SET effective_to_version=? "
                "WHERE box_id=? AND effective_to_version IS NULL",
                (effective_from_version - 1, box_id),
            )
            self.conn.execute(
                "INSERT INTO sub_box_registry(box_id,street_id,house_no,box_label,"
                "effective_from_version,effective_to_version,note) VALUES(?,?,?,?,?,NULL,?)",
                (box_id, street_id, house_no, box_label, effective_from_version, note),
            )
        return self.box_at(box_id, effective_from_version)

    def box_at(self, box_id: str, catalog_version: int) -> BoxRegistration:
        """Return the box registration current at *catalog_version*.

        This is the only registry lookup used when anchoring: it deliberately
        takes an explicit historical version rather than reading "today's" row.
        """
        row = self.conn.execute(
            "SELECT * FROM sub_box_registry WHERE box_id=? "
            "AND effective_from_version<=? "
            "AND (effective_to_version IS NULL OR effective_to_version>=?) "
            "ORDER BY effective_from_version DESC",
            (box_id, catalog_version, catalog_version),
        ).fetchone()
        if row is None:
            raise CalendarError(
                f"box {box_id!r} has no registration current at version {catalog_version}"
            )
        return BoxRegistration(
            row["box_id"],
            row["street_id"],
            row["house_no"],
            row["box_label"],
            row["effective_from_version"],
            row["effective_to_version"],
            row["note"],
        )

    def list_boxes(self) -> list[BoxRegistration]:
        rows = self.conn.execute(
            "SELECT * FROM sub_box_registry ORDER BY box_id, effective_from_version"
        ).fetchall()
        return [
            BoxRegistration(
                r["box_id"], r["street_id"], r["house_no"], r["box_label"],
                r["effective_from_version"], r["effective_to_version"], r["note"],
            )
            for r in rows
        ]

    # -- origin anchoring ----------------------------------------------------

    def anchor_root(
        self,
        root_id: str,
        household_id: str,
        parcel_id: str,
        box_id: str,
        *,
        initial_share: Q | int | str = Q(1),
        note: str = "",
    ) -> OriginAnchor:
        """Anchor a root subscription on a successful parcel's frozen fact.

        The parcel must have a committed job (``submitted``/``replayed``); the
        anchor copies the job's frozen stable street and house number and pairs
        it with the box registration current at the job's catalog version.
        Failures and never-submitted parcels cannot anchor a subscription.
        """
        share = _as_fraction(initial_share)
        if share < 0:
            raise AnchorError("initial_share must be non-negative")
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM sub_roots WHERE root_id=?", (root_id,)
            ).fetchone():
                raise AnchorError(f"root {root_id!r} already anchored")
            job = self.delivery.get_job(parcel_id)
            if job is None:
                raise AnchorError(
                    f"parcel {parcel_id!r} has no successful committed job; "
                    "a subscription root needs a real successful parcel"
                )
            # The frozen job is the authority.  We do NOT call the catalog here:
            # re-resolution today could follow a renamed/re-covered same-name
            # street and would contradict the frozen认门 fact.
            attempts = self.delivery.get_attempts(parcel_id)
            outcomes = [a["outcome"] for a in attempts]
            if not any(o in SUCCESS_OUTCOMES for o in outcomes):
                raise AnchorError(
                    f"parcel {parcel_id!r} never recorded a successful attempt"
                )
            version = int(job["catalog_version"])
            box = self.box_at(box_id, version)
            if box.street_id != job["street_id"] or box.house_no != job["house_no"]:
                raise AnchorError(
                    f"box {box_id!r} at version {version} names {box.street_id}#"
                    f"{box.house_no} but parcel {parcel_id!r} froze "
                    f"{job['street_id']}#{job['house_no']}"
                )
            evidence = {
                "note": note,
                "frozen_identity": job["identity_key"],
                "station_id_at_freeze": job["station_id"],
                "catalog_revision": job["catalog_revision"],
                "original_text": {
                    "city_id": job["city_id"],
                    "district_text": job["district_text_original"],
                    "street_text": job["street_text_original"],
                    "house_no": job["house_no"],
                },
                "box_registration": box.as_dict(),
                "attempt_outcomes": outcomes,
            }
            self.conn.execute(
                "INSERT INTO sub_roots(root_id,household_id,parcel_id,street_id,"
                "house_no,box_id,initial_share,catalog_version,parcel_outcome,"
                "evidence_json,anchored_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    root_id,
                    household_id,
                    parcel_id,
                    job["street_id"],
                    job["house_no"],
                    box_id,
                    frac_to_text(share),
                    version,
                    next(o for o in outcomes if o in SUCCESS_OUTCOMES),
                    json.dumps(evidence, ensure_ascii=False),
                    _now(),
                ),
            )
        return self.get_anchor(root_id)

    def get_anchor(self, root_id: str) -> OriginAnchor:
        row = self.conn.execute(
            "SELECT * FROM sub_roots WHERE root_id=?", (root_id,)
        ).fetchone()
        if row is None:
            raise AnchorError(f"unknown root {root_id!r}")
        return OriginAnchor(
            row["root_id"],
            row["household_id"],
            row["parcel_id"],
            row["street_id"],
            row["house_no"],
            row["box_id"],
            frac_from_text(row["initial_share"]),
            row["catalog_version"],
            row["parcel_outcome"],
            json.loads(row["evidence_json"]),
        )

    def list_anchors(self) -> list[OriginAnchor]:
        rows = self.conn.execute("SELECT * FROM sub_roots ORDER BY root_id").fetchall()
        return [
            OriginAnchor(
                r["root_id"], r["household_id"], r["parcel_id"], r["street_id"],
                r["house_no"], r["box_id"], frac_from_text(r["initial_share"]),
                r["catalog_version"], r["parcel_outcome"],
                json.loads(r["evidence_json"]),
            )
            for r in rows
        ]

    # -- split batches -------------------------------------------------------

    def apply_split_batch(
        self,
        batch_id: str,
        effective_period: int,
        entries: Sequence[SplitEntry | Mapping[str, Any]],
        *,
        note: str = "",
    ) -> SplitBatch:
        """Validate and atomically apply one batch effective at a period opening.

        Validation mirrors the problem statement:

        * every ratio is a non-negative rational and, per old household, the
          ratios sum to exactly one;
        * every old household listed must hold a positive opening share;
          households not listed carry forward unchanged;
        * the batch distributes the **opening snapshot once** - a household that
          receives shares in this batch cannot also be listed as an old
          household (no serial re-splitting of same-batch proceeds);
        * several old households may merge into one new household, and a new
          household may keep an old household's identifier.
        """
        if not isinstance(effective_period, int) or effective_period < 1:
            raise SplitError("effective_period must be a positive integer")
        parsed = tuple(_coerce_entry(e) for e in entries)
        self._validate_batch(batch_id, effective_period, parsed)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT INTO sub_split_batches(batch_id,effective_period,note,applied_at) "
                "VALUES(?,?,?,?)",
                (batch_id, effective_period, note, _now()),
            )
            for entry in parsed:
                self.conn.execute(
                    "INSERT INTO sub_split_entries(batch_id,old_household,new_household,"
                    "ratio_text,box_id) VALUES(?,?,?,?,?)",
                    (
                        batch_id,
                        entry.old_household,
                        entry.new_household,
                        frac_to_text(entry.ratio),
                        entry.box_id,
                    ),
                )
            self._materialize_period(effective_period, batch_id)
        return self.get_batch(batch_id)

    def _validate_batch(
        self, batch_id: str, effective_period: int, entries: tuple[SplitEntry, ...]
    ) -> None:
        if self.conn.execute(
            "SELECT 1 FROM sub_split_batches WHERE batch_id=?", (batch_id,)
        ).fetchone():
            raise SplitError(f"batch {batch_id!r} already applied")
        if self.conn.execute(
            "SELECT 1 FROM sub_split_batches WHERE effective_period=?",
            (effective_period,),
        ).fetchone():
            raise SplitError(
                f"a batch is already effective at period {effective_period}"
            )
        if not entries:
            raise SplitError("a split batch needs at least one entry")
        opening = self.opening_snapshot(effective_period)
        if not opening:
            raise SplitError(
                f"no opening shares exist at period {effective_period}; "
                "anchor a root subscription first"
            )
        old_households = [e.old_household for e in entries]
        new_households = {e.new_household for e in entries}
        # Ratios per old household must be non-negative and sum to exactly 1.
        grouped: dict[str, Q] = {}
        for entry in entries:
            if entry.ratio < 0:
                raise SplitError(
                    f"split ratio must be non-negative: {entry.as_dict()}"
                )
            grouped[entry.old_household] = grouped.get(entry.old_household, Q(0)) + entry.ratio
        for old, total in grouped.items():
            if total != 1:
                raise SplitError(
                    f"ratios for old household {old!r} sum to {frac_to_text(total)}, not 1"
                )
            holding = opening.get(old, {}).get("share", Q(0))
            if holding <= 0:
                raise SplitError(
                    f"old household {old!r} has no positive opening share at period "
                    f"{effective_period}; only period-opening holders may be split"
                )
        # One-level rule: same-batch proceeds may not be split again.  A
        # household may keep its own identifier (an entry whose old and new
        # ids are equal is a retention, not a re-split), so self-overlap is
        # allowed.  What is forbidden is a *cross-entry* chain: a household
        # that receives proceeds from another old household and is itself
        # listed as an old household.  Identifier-based checking means entry
        # order cannot hide the serial nesting.
        retention_pairs = {
            (e.old_household, e.new_household)
            for e in entries
            if e.old_household == e.new_household
        }
        cross_receivers = {
            e.new_household for e in entries
            if (e.old_household, e.new_household) not in retention_pairs
        }
        chained = sorted(set(old_households) & cross_receivers)
        if chained:
            raise SplitError(
                "household(s) " + ",".join(chained) + " receive proceeds from another "
                "old household and are also listed as old households in the same batch; "
                "the opening snapshot is distributed once - compose the ratios in one "
                "level, or move the second split to a later period's batch"
            )
        # Every receiving household must have a delivery box for the period.
        for entry in entries:
            if entry.box_id is None:
                raise SplitError(
                    f"new household {entry.new_household!r} needs a delivery box_id "
                    "for the period (even when sharing a box with another household)"
                )
            self._require_known_box(entry.box_id)

    def _require_known_box(self, box_id: str) -> None:
        if self.conn.execute(
            "SELECT 1 FROM sub_box_registry WHERE box_id=? LIMIT 1", (box_id,)
        ).fetchone() is None:
            raise CalendarError(f"unknown box_id {box_id!r}")

    def get_batch(self, batch_id: str) -> SplitBatch:
        row = self.conn.execute(
            "SELECT * FROM sub_split_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise SplitError(f"unknown batch {batch_id!r}")
        entry_rows = self.conn.execute(
            "SELECT * FROM sub_split_entries WHERE batch_id=? ORDER BY entry_id",
            (batch_id,),
        ).fetchall()
        entries = tuple(
            SplitEntry(
                r["old_household"],
                r["new_household"],
                frac_from_text(r["ratio_text"]),
                r["box_id"],
            )
            for r in entry_rows
        )
        return SplitBatch(row["batch_id"], row["effective_period"], entries, row["note"])

    def list_batches(self) -> list[SplitBatch]:
        rows = self.conn.execute(
            "SELECT batch_id FROM sub_split_batches ORDER BY effective_period"
        ).fetchall()
        return [self.get_batch(r["batch_id"]) for r in rows]

    # -- snapshots and materialisation --------------------------------------

    def opening_snapshot(self, period: int) -> dict[str, dict[str, Any]]:
        """Household holdings at the opening of *period*, before that period's batch.

        Returns ``household -> {"share": Fraction, "provenance": dict, "box_id": str}``.
        For period 1 this is the anchored roots; for later periods it is the
        previous period's allocated state carried forward.
        """
        if period <= 1:
            return self._root_snapshot()
        return {
            hh: {
                "share": frac_from_text(row["share_text"]),
                "provenance": _loads_provenance(row["provenance_json"]),
                "box_id": row["box_id"],
            }
            for hh, row in self._period_rows(period - 1).items()
        }

    def _root_snapshot(self) -> dict[str, dict[str, Any]]:
        snapshot: dict[str, dict[str, Any]] = {}
        for anchor in self.list_anchors():
            entry = snapshot.setdefault(
                anchor.household_id,
                {"share": Q(0), "provenance": {}, "box_id": anchor.box_id},
            )
            entry["share"] += anchor.initial_share
            entry["provenance"][anchor.root_id] = (
                entry["provenance"].get(anchor.root_id, Q(0)) + anchor.initial_share
            )
            # Multiple roots on one household keep the first anchored box as the
            # default; split entries can assign an explicit period box anyway.
        return snapshot

    def _period_rows(self, period: int) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute(
            "SELECT * FROM sub_allocations WHERE period=? ORDER BY household_id",
            (period,),
        ).fetchall()
        return {r["household_id"]: r for r in rows}

    def _materialize_period(self, period: int, batch_id: str) -> None:
        """Apply the batch to the opening snapshot and persist period allocations."""
        opening = self.opening_snapshot(period)
        entries = self.get_batch(batch_id).entries
        result: dict[str, dict[str, Any]] = {}

        def ensure(hh: str, box_id: str) -> dict[str, Any]:
            return result.setdefault(
                hh, {"share": Q(0), "provenance": {}, "box_id": box_id}
            )

        split_old = {e.old_household for e in entries}
        for old, state in opening.items():
            if old not in split_old:
                # Unlisted households carry forward unchanged (same box, share, roots).
                carried = ensure(old, state["box_id"])
                carried["share"] += state["share"]
                for root, amount in state["provenance"].items():
                    carried["provenance"][root] = (
                        carried["provenance"].get(root, Q(0)) + amount
                    )
        for entry in entries:
            source = opening[entry.old_household]
            target = ensure(entry.new_household, entry.box_id or source["box_id"])
            for root, amount in source["provenance"].items():
                target["provenance"][root] = (
                    target["provenance"].get(root, Q(0)) + amount * entry.ratio
                )
                target["share"] += amount * entry.ratio
        # Conservation: the batch redistributes but never creates/destroys share.
        before = sum((s["share"] for s in opening.values()), Q(0))
        after = sum((s["share"] for s in result.values()), Q(0))
        if before != after:
            raise SplitError(
                f"batch {batch_id!r} is not share-conserving "
                f"({frac_to_text(before)} -> {frac_to_text(after)})"
            )
        for household, state in result.items():
            self.conn.execute(
                "INSERT INTO sub_allocations(period,household_id,box_id,share_text,"
                "provenance_json,batch_id) VALUES(?,?,?,?,?,?)",
                (
                    period,
                    household,
                    state["box_id"],
                    frac_to_text(state["share"]),
                    _dumps_provenance(state["provenance"]),
                    batch_id,
                ),
            )

    def ensure_baseline_period(self) -> None:
        """Materialise period 1 from the roots when no batch is effective there.

        A horizon may start without any split batch; the anchored roots still
        need a period-1 allocation row so scheduling can read entitlements.
        """
        with transaction(self.conn):
            if self.conn.execute(
                "SELECT 1 FROM sub_allocations WHERE period=1 LIMIT 1"
            ).fetchone():
                return
            snapshot = self._root_snapshot()
            if not snapshot:
                raise SplitError("cannot materialise period 1: no anchored root")
            for household, state in snapshot.items():
                self.conn.execute(
                    "INSERT INTO sub_allocations(period,household_id,box_id,share_text,"
                    "provenance_json,batch_id) VALUES(?,?,?,?,?,NULL)",
                    (
                        1,
                        household,
                        state["box_id"],
                        frac_to_text(state["share"]),
                        _dumps_provenance(state["provenance"]),
                    ),
                )

    # -- box closures / calendar --------------------------------------------

    def register_closure(self, period: int, box_id: str, *, note: str = "") -> None:
        """Record a box that is closed for one issue.

        Closures are delivery-side facts only: the closed household's
        entitlement for the period is still recorded in full.
        """
        if not isinstance(period, int) or period < 1:
            raise CalendarError("period must be a positive integer")
        self._require_known_box(box_id)
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR IGNORE INTO sub_box_closures(period,box_id,note) VALUES(?,?,?)",
                (period, box_id, note),
            )

    def closed_boxes(self, period: int) -> set[str]:
        rows = self.conn.execute(
            "SELECT box_id FROM sub_box_closures WHERE period=?", (period,)
        ).fetchall()
        return {r["box_id"] for r in rows}

    # -- entitlement reads ---------------------------------------------------

    def allocated_periods(self) -> list[int]:
        rows = self.conn.execute(
            "SELECT DISTINCT period FROM sub_allocations ORDER BY period"
        ).fetchall()
        return [r["period"] for r in rows]

    def allocations(self, period: int) -> list[PeriodAllocation]:
        rows = self.conn.execute(
            "SELECT * FROM sub_allocations WHERE period=? ORDER BY household_id",
            (period,),
        ).fetchall()
        return [
            PeriodAllocation(
                r["period"],
                r["household_id"],
                r["box_id"],
                frac_from_text(r["share_text"]),
                tuple(sorted(_loads_provenance(r["provenance_json"]).items())),
                r["batch_id"],
            )
            for r in rows
        ]

    def entitlement_table(self, periods: Iterable[int]) -> list[PeriodAllocation]:
        """Exact per-period entitlements, carrying unchanged households forward.

        Periods without their own batch return the most recent allocated state,
        so callers can read every issue of a multi-issue horizon uniformly.
        """
        periods = list(periods)
        if not periods:
            return []
        known = self.allocated_periods()
        if not known:
            raise SplitError("no allocations exist; anchor roots and apply a batch")
        table: list[PeriodAllocation] = []
        for period in periods:
            source_period = max(p for p in known if p <= period)
            for allocation in self.allocations(source_period):
                table.append(
                    PeriodAllocation(
                        period,
                        allocation.household_id,
                        allocation.box_id,
                        allocation.share,
                        allocation.provenance,
                        allocation.batch_id,
                    )
                )
        return table

    def household_universe(self, periods: Sequence[int]) -> list[str]:
        """Stable household identifiers over the horizon (max four)."""
        universe = {
            a.household_id for a in self.entitlement_table(periods) if a.share > 0
        }
        if len(universe) > MAX_HOUSEHOLDS:
            raise SchedulingError(
                f"{len(universe)} positive-share households exceed the bound of "
                f"{MAX_HOUSEHOLDS}"
            )
        return sorted(universe)

    # -- audit views ---------------------------------------------------------

    def household_ledger_view(self, periods: Sequence[int]) -> dict[str, Any]:
        """Independent audit view: per-household exact entitlement by period."""
        table = self.entitlement_table(periods)
        view: dict[str, Any] = {}
        for allocation in table:
            household = view.setdefault(
                allocation.household_id,
                {"box_id": allocation.box_id, "periods": {}},
            )
            household["periods"][allocation.period] = {
                "share": frac_to_text(allocation.share),
                "provenance": [
                    {"root_id": root, "amount": frac_to_text(amount)}
                    for root, amount in allocation.provenance
                ],
                "batch_id": allocation.batch_id,
            }
        return view


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _as_fraction(value: Q | int | str) -> Q:
    if isinstance(value, Fraction):
        return value
    return frac_from_text(value)


def _coerce_entry(value: SplitEntry | Mapping[str, Any]) -> SplitEntry:
    if isinstance(value, SplitEntry):
        return value
    if not isinstance(value, Mapping):
        raise SplitError("each split entry must be a SplitEntry or a mapping")
    try:
        return SplitEntry(
            old_household=value["old_household"],
            new_household=value["new_household"],
            ratio=frac_from_text(value["ratio"]),
            box_id=value.get("box_id"),
        )
    except KeyError as exc:
        raise SplitError(f"split entry missing field {exc}") from exc


def _dumps_provenance(provenance: Mapping[str, Q]) -> str:
    return json.dumps(
        [
            {"root_id": root, "amount": frac_to_text(amount)}
            for root, amount in sorted(provenance.items())
            if amount != 0
        ],
        ensure_ascii=False,
    )


def _loads_provenance(text: str) -> dict[str, Q]:
    return {item["root_id"]: frac_from_text(item["amount"]) for item in json.loads(text)}
