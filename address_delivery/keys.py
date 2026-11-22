"""Physical-key ledger: custody, handovers, receipts, disputes, cutoff inventory.

Each physical key has a unique engraving (``key_code``) and an initial holder.
A move between two counters is a *transfer* identified by one shared
``transfer_id`` and is evidenced by two separately recorded signatures:

* a **handover** ("交出") signed by the original holder;
* a **receipt** ("签收") signed by the actual receiving party.

The same ``transfer_id`` must agree on engraving, both parties and the ordering
of moments (handover before receipt).  Re-sending an identical record is the
same entry and is accepted idempotently.

Custody rules, evaluated per key independently:

* After the handover and before the receipt the key is **in transit**: neither
  side may lend or hand it over again; only the receiver can make it lendable by
  signing the receipt.
* A handover by a party that does not hold the key (including while it is in
  transit), a receipt without a matching handover, or two contradictory signed
  records under one transfer id, are all *disputes*.  The key then stops lending
  from the dispute moment on.  Evidence is append-only, so a dispute can never
  be healed by deleting one side's record.
* A handover with no receipt yet is simply still in transit, not a dispute.

The ledger is an event-sourced store: :meth:`KeyLedger.ownership_at` and the
cutoff inventory are both derived by replaying the immutable event log.  A
cutoff inventory (the night's "营业截点" frozen sheet) copies each key's state at
one moment; later transfer history is never appended back into that frozen
sheet, and the sheet can be rebuilt from one-sided old records (an in-transit
key is simply recorded as in transit and is not borrowable).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from .errors import KeyLedgerError

HANDOVER = "handover"
RECEIPT = "receipt"

# Dispute reason vocabulary ----------------------------------------------------
DISPUTE_CONTRADICTION = "contradictory_signed_records"
DISPUTE_UNAUTHORIZED = "handover_from_non_holder"
DISPUTE_RECEIPT_NO_HANDOVER = "receipt_without_handover"
DISPUTE_CROSS_KEY = "transfer_id_bound_to_other_key"

# Custody status vocabulary ----------------------------------------------------
STATUS_HELD = "held"
STATUS_IN_TRANSIT = "in_transit"
STATUS_DISPUTED = "disputed"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS key_counters (
    counter_id TEXT PRIMARY KEY,
    name       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS physical_keys (
    key_code       TEXT PRIMARY KEY,
    initial_holder TEXT NOT NULL,
    name           TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS key_transfers (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    transfer_id TEXT NOT NULL,
    key_code    TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('handover','receipt')),
    from_party  TEXT NOT NULL,
    to_party    TEXT NOT NULL,
    moment      INTEGER NOT NULL,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_transfers_key ON key_transfers(key_code);
CREATE INDEX IF NOT EXISTS idx_transfers_transfer ON key_transfers(transfer_id);
CREATE TABLE IF NOT EXISTS key_disputes (
    key_code     TEXT PRIMARY KEY,
    start_moment INTEGER NOT NULL,
    reason       TEXT NOT NULL,
    transfer_id  TEXT,
    evidence_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cutoff_snapshots (
    snapshot_id   TEXT PRIMARY KEY,
    cutoff_moment INTEGER NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cutoff_snapshot_keys (
    snapshot_id  TEXT NOT NULL,
    key_code     TEXT NOT NULL,
    holder       TEXT NOT NULL,
    status       TEXT NOT NULL,
    open_transfer TEXT,
    disputed     INTEGER NOT NULL,
    borrowable   INTEGER NOT NULL,
    basis_json   TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, key_code)
);
CREATE TABLE IF NOT EXISTS external_key_reservations (
    reservation_id TEXT PRIMARY KEY,
    key_code       TEXT NOT NULL,
    start_min      INTEGER NOT NULL,
    end_min        INTEGER NOT NULL,
    source         TEXT NOT NULL,
    plan_id        TEXT,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reservations_key ON external_key_reservations(key_code);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counter:
    counter_id: str
    name: str

    def as_dict(self) -> dict[str, Any]:
        return {"counter_id": self.counter_id, "name": self.name}


@dataclass(frozen=True)
class PhysicalKey:
    key_code: str
    initial_holder: str
    name: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_code": self.key_code,
            "initial_holder": self.initial_holder,
            "name": self.name,
        }


@dataclass(frozen=True)
class TransferRecord:
    seq: int
    transfer_id: str
    key_code: str
    kind: str
    from_party: str
    to_party: str
    moment: int
    note: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "transfer_id": self.transfer_id,
            "key_code": self.key_code,
            "kind": self.kind,
            "from_party": self.from_party,
            "to_party": self.to_party,
            "moment": self.moment,
            "note": self.note,
        }


@dataclass
class _Dispute:
    start_moment: int
    reason: str
    transfer_id: str | None
    evidence: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_moment": self.start_moment,
            "reason": self.reason,
            "transfer_id": self.transfer_id,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Ownership:
    """Physical custody of one key at one moment, with the evidence basis."""

    key_code: str
    moment: int
    holder: str
    status: str
    open_transfer: str | None
    disputed: bool
    dispute: dict[str, Any] | None
    basis: list[dict[str, Any]]

    @property
    def borrowable(self) -> bool:
        """A key can leave a counter only when quietly held there (not in
        transit, not disputed)."""
        return self.status == STATUS_HELD and not self.disputed

    def as_dict(self) -> dict[str, Any]:
        return {
            "key_code": self.key_code,
            "moment": self.moment,
            "holder": self.holder,
            "status": self.status,
            "open_transfer": self.open_transfer,
            "disputed": self.disputed,
            "dispute": self.dispute,
            "borrowable": self.borrowable,
            "basis": self.basis,
        }


@dataclass(frozen=True)
class Reservation:
    reservation_id: str
    key_code: str
    start_min: int
    end_min: int
    source: str
    plan_id: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "key_code": self.key_code,
            "start_min": self.start_min,
            "end_min": self.end_min,
            "source": self.source,
            "plan_id": self.plan_id,
        }


# ---------------------------------------------------------------------------
# Pure replay: the single source of truth for custody
# ---------------------------------------------------------------------------


def _replay(rows: Sequence[Any], initial_holder: str) -> dict[str, Any]:
    """Fold the immutable event log of one key into its custody state.

    Returns the final holder, any open (un-receipted) transfer, the earliest
    dispute and the ordered basis of applied signatures.  Events at or after a
    dispute start stay in the log as evidence but no longer move custody.
    """
    holder = initial_holder
    open_transfer: dict[str, Any] | None = None
    handovers: dict[str, Any] = {}
    receipts: dict[str, list[Any]] = {}
    dispute: _Dispute | None = None
    basis: list[dict[str, Any]] = []
    all_events: list[dict[str, Any]] = []

    def frozen(row) -> bool:
        return dispute is not None and row["moment"] >= dispute.start_moment

    for row in rows:
        event = {
            "seq": row["seq"],
            "transfer_id": row["transfer_id"],
            "kind": row["kind"],
            "from_party": row["from_party"],
            "to_party": row["to_party"],
            "moment": row["moment"],
            "applied": False,
        }
        all_events.append(event)
        tid = row["transfer_id"]

        if row["kind"] == HANDOVER:
            prior = handovers.get(tid)
            if prior is not None:
                if _same_handover(prior, row):
                    # Identical re-send: the same entry, no state change.
                    continue
                if not frozen(row):
                    dispute = _Dispute(
                        row["moment"], DISPUTE_CONTRADICTION, tid,
                        [dict(prior), dict(event)],
                    )
                continue
            if not frozen(row):
                if open_transfer is not None:
                    # Possession has already left; neither side holds it.
                    dispute = _Dispute(
                        row["moment"], DISPUTE_UNAUTHORIZED, tid,
                        [dict(open_transfer), dict(event)],
                    )
                elif holder != row["from_party"]:
                    dispute = _Dispute(
                        row["moment"], DISPUTE_UNAUTHORIZED, tid,
                        [{"holder": holder}, dict(event)],
                    )
                else:
                    open_transfer = dict(event)
                    event["applied"] = True
                    basis.append(dict(event))
            handovers[tid] = row
            continue

        # RECEIPT -----------------------------------------------------------
        prior_receipts = receipts.setdefault(tid, [])
        if any(_same_receipt(r, row) for r in prior_receipts):
            # Identical re-send of an already recorded receipt.
            continue
        prior_receipts.append(row)
        handover = handovers.get(tid)
        if handover is None:
            if not frozen(row):
                dispute = _Dispute(
                    row["moment"], DISPUTE_RECEIPT_NO_HANDOVER, tid, [dict(event)]
                )
            continue
        if not _agrees(handover, row):
            if not frozen(row):
                dispute = _Dispute(
                    row["moment"], DISPUTE_CONTRADICTION, tid,
                    [dict(_event_of(handover)), dict(event)],
                )
            continue
        if not frozen(row):
            if open_transfer is not None and open_transfer["transfer_id"] == tid:
                holder = row["to_party"]
                open_transfer = None
                event["applied"] = True
                basis.append(dict(event))
            # A receipt for an already-closed transfer is a contradictory extra
            # signature only if it differs; identical ones were skipped above.

    return {
        "holder": holder,
        "open_transfer": open_transfer,
        "dispute": dispute,
        "basis": basis,
        "events": all_events,
    }


def _same_handover(a: Any, b: Any) -> bool:
    return (
        a["transfer_id"] == b["transfer_id"]
        and a["key_code"] == b["key_code"]
        and a["from_party"] == b["from_party"]
        and a["to_party"] == b["to_party"]
        and a["moment"] == b["moment"]
    )


def _same_receipt(a: Any, b: Any) -> bool:
    return (
        a["transfer_id"] == b["transfer_id"]
        and a["key_code"] == b["key_code"]
        and a["from_party"] == b["from_party"]
        and a["to_party"] == b["to_party"]
        and a["moment"] == b["moment"]
    )


def _agrees(handover: Any, receipt: Any) -> bool:
    """The receipt under one transfer id must match the handover."""
    return (
        handover["key_code"] == receipt["key_code"]
        and handover["from_party"] == receipt["from_party"]
        and handover["to_party"] == receipt["to_party"]
        and receipt["moment"] >= handover["moment"]
    )


def _event_of(row: Any) -> dict[str, Any]:
    return {
        "seq": row["seq"],
        "transfer_id": row["transfer_id"],
        "kind": row["kind"],
        "from_party": row["from_party"],
        "to_party": row["to_party"],
        "moment": row["moment"],
    }


# ---------------------------------------------------------------------------
# Ledger store
# ---------------------------------------------------------------------------


class KeyLedger:
    """Append-only evidence log and derived custody for physical keys."""

    def __init__(self, conn, *, init_schema: bool = True) -> None:
        self.conn = conn
        if init_schema:
            self.conn.executescript(_SCHEMA)

    # -- setup ----------------------------------------------------------------

    def register_counter(self, counter_id: str, name: str | None = None) -> Counter:
        self._require_text(counter_id, "counter_id")
        self.conn.execute(
            "INSERT INTO key_counters(counter_id,name) VALUES(?,?) "
            "ON CONFLICT(counter_id) DO UPDATE SET name=excluded.name",
            (counter_id, name if name is not None else counter_id),
        )
        return Counter(counter_id, name if name is not None else counter_id)

    def register_key(
        self, key_code: str, initial_holder: str, *, name: str | None = None
    ) -> PhysicalKey:
        self._require_text(key_code, "key_code")
        self._require_text(initial_holder, "initial_holder")
        self._assert_counter(initial_holder)
        if self.conn.execute(
            "SELECT 1 FROM physical_keys WHERE key_code=?", (key_code,)
        ).fetchone():
            raise KeyLedgerError(f"key {key_code} already registered")
        self.conn.execute(
            "INSERT INTO physical_keys(key_code,initial_holder,name) VALUES(?,?,?)",
            (key_code, initial_holder, name if name is not None else key_code),
        )
        return PhysicalKey(key_code, initial_holder, name if name is not None else key_code)

    def list_keys(self) -> list[PhysicalKey]:
        rows = self.conn.execute(
            "SELECT * FROM physical_keys ORDER BY key_code"
        ).fetchall()
        return [PhysicalKey(r["key_code"], r["initial_holder"], r["name"]) for r in rows]

    def list_counters(self) -> list[Counter]:
        rows = self.conn.execute(
            "SELECT * FROM key_counters ORDER BY counter_id"
        ).fetchall()
        return [Counter(r["counter_id"], r["name"]) for r in rows]

    # -- event recording ------------------------------------------------------

    def record_handover(
        self,
        transfer_id: str,
        key_code: str,
        from_party: str,
        to_party: str,
        moment: int,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record the original holder's 交出 signature."""
        return self._record(
            HANDOVER, transfer_id, key_code, from_party, to_party, moment, note
        )

    def record_receipt(
        self,
        transfer_id: str,
        key_code: str,
        from_party: str,
        to_party: str,
        moment: int,
        *,
        note: str | None = None,
    ) -> dict[str, Any]:
        """Record the receiving party's 签收 signature."""
        return self._record(
            RECEIPT, transfer_id, key_code, from_party, to_party, moment, note
        )

    def _record(
        self,
        kind: str,
        transfer_id: str,
        key_code: str,
        from_party: str,
        to_party: str,
        moment: int,
        note: str | None,
    ) -> dict[str, Any]:
        self._require_text(transfer_id, "transfer_id")
        self._require_text(from_party, "from_party")
        self._require_text(to_party, "to_party")
        if isinstance(moment, bool) or not isinstance(moment, int):
            raise KeyLedgerError("moment must be an integer minute")
        key = self._assert_key(key_code)
        self._assert_counter(from_party)
        self._assert_counter(to_party)
        if from_party == to_party:
            raise KeyLedgerError("handover parties must differ")
        if kind == RECEIPT:
            handover = self._find_event(HANDOVER, transfer_id, key_code)
            if handover is not None and moment < handover["moment"]:
                raise KeyLedgerError("receipt moment precedes its handover moment")

        # Idempotent re-send of an identical record.
        existing = self._find_identical(
            kind, transfer_id, key_code, from_party, to_party, moment
        )
        if existing is not None:
            return {
                "record": _record_row(existing).as_dict(),
                "idempotent": True,
                "state": self.ownership_at(key_code, moment).as_dict(),
            }

        # A key already in dispute at or before this moment has stopped lending.
        current = self._replay_key(key_code)
        dispute = current["dispute"]
        if dispute is not None and moment >= dispute.start_moment:
            raise KeyLedgerError(
                f"key {key_code} is in dispute since minute {dispute.start_moment}; "
                "lending is stopped"
            )

        cur = self.conn.execute(
            "INSERT INTO key_transfers(transfer_id,key_code,kind,from_party,"
            "to_party,moment,note) VALUES(?,?,?,?,?,?,?)",
            (transfer_id, key_code, kind, from_party, to_party, moment, note),
        )
        seq = int(cur.lastrowid)
        self._persist_dispute_if_any(key_code)
        row = self.conn.execute(
            "SELECT * FROM key_transfers WHERE seq=?", (seq,)
        ).fetchone()
        state = self.ownership_at(key_code, moment)
        return {
            "record": _record_row(row).as_dict(),
            "idempotent": False,
            "state": state.as_dict(),
        }

    def list_records(self, key_code: str | None = None) -> list[TransferRecord]:
        if key_code is None:
            rows = self.conn.execute(
                "SELECT * FROM key_transfers ORDER BY key_code, moment, seq"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM key_transfers WHERE key_code=? ORDER BY moment, seq",
                (key_code,),
            ).fetchall()
        return [_record_row(r) for r in rows]

    # -- custody queries ------------------------------------------------------

    def ownership_at(self, key_code: str, moment: int) -> Ownership:
        """Physical custody of one key at *moment*, with its evidence basis."""
        if isinstance(moment, bool) or not isinstance(moment, int):
            raise KeyLedgerError("moment must be an integer minute")
        key = self._assert_key(key_code)
        rows = self.conn.execute(
            "SELECT * FROM key_transfers WHERE key_code=? AND moment<=? "
            "ORDER BY moment, seq",
            (key_code, moment),
        ).fetchall()
        result = _replay(list(rows), key.initial_holder)
        dispute = result["dispute"]
        open_transfer = result["open_transfer"]
        if dispute is not None:
            status = STATUS_DISPUTED
        elif open_transfer is not None:
            status = STATUS_IN_TRANSIT
        else:
            status = STATUS_HELD
        return Ownership(
            key_code=key_code,
            moment=moment,
            holder=result["holder"],
            status=status,
            open_transfer=open_transfer["transfer_id"] if open_transfer else None,
            disputed=dispute is not None,
            dispute=dispute.as_dict() if dispute is not None else None,
            basis=result["basis"],
        )

    def dispute(self, key_code: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM key_disputes WHERE key_code=?", (key_code,)
        ).fetchone()
        if row is None:
            return None
        return {
            "key_code": row["key_code"],
            "start_moment": row["start_moment"],
            "reason": row["reason"],
            "transfer_id": row["transfer_id"],
            "evidence": json.loads(row["evidence_json"]),
        }

    # -- cutoff inventory -----------------------------------------------------

    def freeze_cutoff(self, snapshot_id: str, cutoff_moment: int) -> dict[str, Any]:
        """Freeze the per-key inventory at one cutoff moment.

        Re-freezing the same ``snapshot_id`` returns the stored sheet verbatim:
        transfer history arriving afterwards never mutates a frozen sheet.  The
        sheet can be built from one-sided records - an in-transit key is frozen
        as in transit and is not borrowable.
        """
        if isinstance(cutoff_moment, bool) or not isinstance(cutoff_moment, int):
            raise KeyLedgerError("cutoff_moment must be an integer")
        existing = self.conn.execute(
            "SELECT 1 FROM cutoff_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if existing is not None:
            return self.get_cutoff(snapshot_id)
        with self._transaction():
            self.conn.execute(
                "INSERT INTO cutoff_snapshots(snapshot_id,cutoff_moment,created_at) "
                "VALUES(?,?,?)",
                (snapshot_id, cutoff_moment, _now()),
            )
            for key in self.list_keys():
                ownership = self.ownership_at(key.key_code, cutoff_moment)
                self.conn.execute(
                    "INSERT INTO cutoff_snapshot_keys(snapshot_id,key_code,holder,"
                    "status,open_transfer,disputed,borrowable,basis_json) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        snapshot_id,
                        key.key_code,
                        ownership.holder,
                        ownership.status,
                        ownership.open_transfer,
                        1 if ownership.disputed else 0,
                        1 if ownership.borrowable else 0,
                        json.dumps(ownership.basis, ensure_ascii=False),
                    ),
                )
        return self.get_cutoff(snapshot_id)

    def get_cutoff(self, snapshot_id: str) -> dict[str, Any]:
        header = self.conn.execute(
            "SELECT * FROM cutoff_snapshots WHERE snapshot_id=?", (snapshot_id,)
        ).fetchone()
        if header is None:
            raise KeyLedgerError(f"no cutoff snapshot {snapshot_id!r}")
        rows = self.conn.execute(
            "SELECT * FROM cutoff_snapshot_keys WHERE snapshot_id=? ORDER BY key_code",
            (snapshot_id,),
        ).fetchall()
        keys = [
            {
                "key_code": r["key_code"],
                "holder": r["holder"],
                "status": r["status"],
                "open_transfer": r["open_transfer"],
                "disputed": bool(r["disputed"]),
                "borrowable": bool(r["borrowable"]),
                "basis": json.loads(r["basis_json"]),
            }
            for r in rows
        ]
        return {
            "snapshot_id": snapshot_id,
            "cutoff_moment": header["cutoff_moment"],
            "created_at": header["created_at"],
            "keys": keys,
        }

    def list_cutoffs(self) -> list[dict[str, Any]]:
        return [
            self.get_cutoff(r["snapshot_id"])
            for r in self.conn.execute(
                "SELECT snapshot_id FROM cutoff_snapshots ORDER BY cutoff_moment, snapshot_id"
            ).fetchall()
        ]

    def borrowable_keys(self, snapshot_id: str) -> dict[str, list[str]]:
        """Counter -> keys quietly held there at the frozen cutoff."""
        sheet = self.get_cutoff(snapshot_id)
        result: dict[str, list[str]] = {}
        for entry in sheet["keys"]:
            if entry["borrowable"]:
                result.setdefault(entry["holder"], []).append(entry["key_code"])
        for held in result.values():
            held.sort()
        return result

    # -- external reservations (other shifts) ---------------------------------

    def add_external_reservation(
        self,
        reservation_id: str,
        key_code: str,
        start_min: int,
        end_min: int,
        *,
        source: str = "other_shift",
    ) -> Reservation:
        """Record a half-open ``[start_min, end_min)`` occupancy by another shift.

        Re-sending an identical reservation id is idempotent.
        """
        self._require_text(reservation_id, "reservation_id")
        self._assert_key(key_code)
        self._check_window(start_min, end_min)
        existing = self.conn.execute(
            "SELECT * FROM external_key_reservations WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["key_code"] == key_code
                and existing["start_min"] == start_min
                and existing["end_min"] == end_min
            ):
                return _reservation_row(existing)
            raise KeyLedgerError(
                f"reservation {reservation_id!r} already exists with different terms"
            )
        self.conn.execute(
            "INSERT INTO external_key_reservations(reservation_id,key_code,"
            "start_min,end_min,source,plan_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (reservation_id, key_code, start_min, end_min, source, None, _now()),
        )
        row = self.conn.execute(
            "SELECT * FROM external_key_reservations WHERE reservation_id=?",
            (reservation_id,),
        ).fetchone()
        return _reservation_row(row)

    def reservations_for(self, key_code: str) -> list[Reservation]:
        rows = self.conn.execute(
            "SELECT * FROM external_key_reservations WHERE key_code=? "
            "ORDER BY start_min, reservation_id",
            (key_code,),
        ).fetchall()
        return [_reservation_row(r) for r in rows]

    def has_overlap(self, key_code: str, start_min: int, end_min: int) -> bool:
        """Whether half-open ``[start_min, end_min)`` clashes with any occupancy."""
        self._check_window(start_min, end_min)
        rows = self.conn.execute(
            "SELECT start_min, end_min FROM external_key_reservations WHERE key_code=?",
            (key_code,),
        ).fetchall()
        return any(_overlaps(start_min, end_min, r["start_min"], r["end_min"]) for r in rows)

    # -- internals ------------------------------------------------------------

    def _replay_key(self, key_code: str) -> dict[str, Any]:
        key = self._assert_key(key_code)
        rows = self.conn.execute(
            "SELECT * FROM key_transfers WHERE key_code=? ORDER BY moment, seq",
            (key_code,),
        ).fetchall()
        return _replay(list(rows), key.initial_holder)

    def _persist_dispute_if_any(self, key_code: str) -> None:
        result = self._replay_key(key_code)
        dispute = result["dispute"]
        if dispute is None:
            return
        existing = self.conn.execute(
            "SELECT start_moment FROM key_disputes WHERE key_code=?", (key_code,)
        ).fetchone()
        if existing is not None and existing["start_moment"] <= dispute.start_moment:
            return
        self.conn.execute(
            "INSERT INTO key_disputes(key_code,start_moment,reason,transfer_id,evidence_json) "
            "VALUES(?,?,?,?,?) ON CONFLICT(key_code) DO UPDATE SET "
            "start_moment=excluded.start_moment,reason=excluded.reason,"
            "transfer_id=excluded.transfer_id,evidence_json=excluded.evidence_json",
            (
                key_code,
                dispute.start_moment,
                dispute.reason,
                dispute.transfer_id,
                json.dumps(dispute.evidence, ensure_ascii=False),
            ),
        )

    def _find_event(self, kind: str, transfer_id: str, key_code: str):
        return self.conn.execute(
            "SELECT * FROM key_transfers WHERE kind=? AND transfer_id=? AND key_code=? "
            "ORDER BY seq LIMIT 1",
            (kind, transfer_id, key_code),
        ).fetchone()

    def _find_identical(
        self, kind, transfer_id, key_code, from_party, to_party, moment
    ):
        return self.conn.execute(
            "SELECT * FROM key_transfers WHERE kind=? AND transfer_id=? AND key_code=? "
            "AND from_party=? AND to_party=? AND moment=? ORDER BY seq LIMIT 1",
            (kind, transfer_id, key_code, from_party, to_party, moment),
        ).fetchone()

    def _assert_key(self, key_code: str) -> PhysicalKey:
        row = self.conn.execute(
            "SELECT * FROM physical_keys WHERE key_code=?", (key_code,)
        ).fetchone()
        if row is None:
            raise KeyLedgerError(f"unknown key {key_code!r}")
        return PhysicalKey(row["key_code"], row["initial_holder"], row["name"])

    def _assert_counter(self, counter_id: str) -> Counter:
        row = self.conn.execute(
            "SELECT * FROM key_counters WHERE counter_id=?", (counter_id,)
        ).fetchone()
        if row is None:
            raise KeyLedgerError(f"unknown counter {counter_id!r}")
        return Counter(row["counter_id"], row["name"])

    @staticmethod
    def _require_text(value: str, label: str) -> None:
        if not isinstance(value, str) or not value:
            raise KeyLedgerError(f"{label} must be a non-empty string")

    @staticmethod
    def _check_window(start_min: int, end_min: int) -> None:
        if isinstance(start_min, bool) or isinstance(end_min, bool) or not (
            isinstance(start_min, int) and isinstance(end_min, int)
        ):
            raise KeyLedgerError("reservation minutes must be integers")
        if not (0 <= start_min < end_min):
            raise KeyLedgerError("require 0 <= start_min < end_min")

    def _transaction(self):
        from .db import transaction

        return transaction(self.conn)


def _overlaps(b1: int, r1: int, b2: int, r2: int) -> bool:
    return not (r1 <= b2 or b1 >= r2)


def _record_row(row) -> TransferRecord:
    return TransferRecord(
        seq=row["seq"],
        transfer_id=row["transfer_id"],
        key_code=row["key_code"],
        kind=row["kind"],
        from_party=row["from_party"],
        to_party=row["to_party"],
        moment=row["moment"],
        note=row["note"],
    )


def _reservation_row(row) -> Reservation:
    return Reservation(
        reservation_id=row["reservation_id"],
        key_code=row["key_code"],
        start_min=row["start_min"],
        end_min=row["end_min"],
        source=row["source"],
        plan_id=row["plan_id"],
    )
