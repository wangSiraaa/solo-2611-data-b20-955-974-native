"""House-number renumbering history (synthetic blocks only).

A *renumbering ledger* records up to three door books (eras) and the grouped
renumbering publications between adjacent eras.  Every renumbering strip maps
one closed old interval (optionally restricted to odd/even numbers) on one
stable street to an arithmetic sequence of *new* numbers on one stable target
street; matched old numbers are taken in ascending order and paired one-to-one
with the new numbers.  Doors not covered by any strip of a publication carry
over unchanged.

The same street text and house number in two eras may be different physical
entities: doors are always keyed by their **stable street id**, never by name.
A successfully delivered parcel therefore traces from its *frozen* street id
and house number - an alias correction that happens after delivery never makes
the history re-guess the street.

Validation rules for one publication (all strips share the same old map):

* old and target streets must exist in the catalog at the appropriate eras;
* every new number must be a positive integer and the step non-zero;
* two strips covering the same registered old door reject the publication;
* two distinct registered old doors landing on the same new
  ``(street, number)`` reject the publication.

A rejected publication leaves no new era behind (the whole publication is one
transaction).  The output of a publication only feeds the *next* era; it is
never re-applied inside the same publication.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence

from .db import transaction
from .errors import RenumberingError

MAX_ERAS = 3
PARITIES = ("any", "odd", "even")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS renumber_eras (
    era_seq   INTEGER PRIMARY KEY,
    label     TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS renumber_doors (
    era_seq    INTEGER NOT NULL REFERENCES renumber_eras(era_seq),
    street_id  TEXT NOT NULL,
    house_no   INTEGER NOT NULL,
    PRIMARY KEY (era_seq, street_id, house_no)
);
CREATE TABLE IF NOT EXISTS renumber_strips (
    strip_id        TEXT PRIMARY KEY,
    era_seq         INTEGER NOT NULL,        -- era the strip leads away from
    ordinal         INTEGER NOT NULL,
    old_street_id   TEXT NOT NULL,
    old_lo          INTEGER NOT NULL,
    old_hi          INTEGER NOT NULL,
    parity          TEXT NOT NULL CHECK (parity IN ('any','odd','even')),
    new_street_id   TEXT NOT NULL,
    new_first       INTEGER NOT NULL,
    new_step        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_strips_era ON renumber_strips(era_seq);
CREATE TABLE IF NOT EXISTS renumber_issued (
    parcel_id   TEXT PRIMARY KEY,
    street_id   TEXT NOT NULL,
    house_no    INTEGER NOT NULL,
    era_seq     INTEGER NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Door:
    """One registered door: stable street id plus positive house number."""

    street_id: str
    house_no: int

    def as_dict(self) -> dict[str, Any]:
        return {"street_id": self.street_id, "house_no": self.house_no}


@dataclass(frozen=True)
class RenumberStrip:
    """One grouped change-of-number entry between two adjacent eras.

    ``new_first`` plus a non-zero ``new_step`` define the arithmetic sequence
    paired with the matched old numbers in ascending order.
    """

    strip_id: str
    old_street_id: str
    old_lo: int
    old_hi: int
    parity: str
    new_street_id: str
    new_first: int
    new_step: int

    def __post_init__(self) -> None:
        if self.parity not in PARITIES:
            raise RenumberingError(f"strip {self.strip_id} has invalid parity {self.parity!r}")
        if self.old_lo <= 0 or self.old_hi < self.old_lo:
            raise RenumberingError(f"strip {self.strip_id} has an invalid old interval")
        if self.new_first <= 0 or any(n <= 0 for n in self.new_numbers()):
            raise RenumberingError(
                f"strip {self.strip_id} reaches a non-positive target number",
                code="target_not_positive",
            )
        if self.new_step == 0:
            raise RenumberingError(
                f"strip {self.strip_id} new_step must be non-zero",
                code="zero_step",
            )

    def matched_old_numbers(self) -> list[int]:
        if self.parity == "any":
            return [n for n in range(self.old_lo, self.old_hi + 1)]
        want = 1 if self.parity == "odd" else 0
        return [n for n in range(self.old_lo, self.old_hi + 1) if n % 2 == want]

    def new_numbers(self) -> list[int]:
        return [self.new_first + i * self.new_step for i in range(len(self.matched_old_numbers()))]

    def as_dict(self) -> dict[str, Any]:
        return {
            "strip_id": self.strip_id,
            "old_street_id": self.old_street_id,
            "old_lo": self.old_lo,
            "old_hi": self.old_hi,
            "parity": self.parity,
            "new_street_id": self.new_street_id,
            "new_first": self.new_first,
            "new_step": self.new_step,
        }


@dataclass(frozen=True)
class EraTrace:
    """Where one traced door sits in a particular era and which strip moved it."""

    era_seq: int
    label: str
    street_id: str
    house_no: int
    applied_strip: str | None
    carried_over: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "era_seq": self.era_seq,
            "label": self.label,
            "street_id": self.street_id,
            "house_no": self.house_no,
            "applied_strip": self.applied_strip,
            "carried_over": self.carried_over,
        }


@dataclass(frozen=True)
class DoorTrace:
    """Full era-by-era trace starting from a frozen door."""

    start_street_id: str
    start_house_no: int
    eras: tuple[EraTrace, ...]

    @property
    def final(self) -> EraTrace:
        return self.eras[-1]

    def as_dict(self) -> dict[str, Any]:
        return {
            "start_street_id": self.start_street_id,
            "start_house_no": self.start_house_no,
            "eras": [e.as_dict() for e in self.eras],
            "final": self.final.as_dict(),
            "used_strips": [e.applied_strip for e in self.eras if e.applied_strip is not None],
        }


@dataclass(frozen=True)
class IssuedDoor:
    """A final door bound to a real, committed parcel."""

    parcel_id: str
    street_id: str
    house_no: int
    era_seq: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "street_id": self.street_id,
            "house_no": self.house_no,
            "era_seq": self.era_seq,
        }


# ---------------------------------------------------------------------------
# Pure publication machinery (kept separate from SQLite for easy review)
# ---------------------------------------------------------------------------


def _strip_from_dict(data: dict[str, Any], ordinal: int) -> RenumberStrip:
    try:
        strip = RenumberStrip(
            strip_id=str(data["strip_id"]),
            old_street_id=str(data["old_street_id"]),
            old_lo=int(data["old_lo"]),
            old_hi=int(data["old_hi"]),
            parity=str(data.get("parity", "any")),
            new_street_id=str(data["new_street_id"]),
            new_first=int(data["new_first"]),
            new_step=int(data["new_step"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RenumberingError(f"strip at ordinal {ordinal} is malformed: {exc}") from exc
    if not strip.strip_id:
        raise RenumberingError(f"strip at ordinal {ordinal} needs a strip_id")
    if strip.parity not in PARITIES:
        raise RenumberingError(f"strip {strip.strip_id} has invalid parity {strip.parity!r}")
    if strip.old_lo <= 0 or strip.old_hi < strip.old_lo:
        raise RenumberingError(f"strip {strip.strip_id} has an invalid old interval")
    if strip.new_first <= 0:
        raise RenumberingError(
            f"strip {strip.strip_id} targets non-positive number {strip.new_first}",
            code="target_not_positive",
        )
    if strip.new_step == 0:
        raise RenumberingError(
            f"strip {strip.strip_id} new_step must be non-zero",
            code="zero_step",
        )
    if any(n <= 0 for n in strip.new_numbers()):
        raise RenumberingError(
            f"strip {strip.strip_id} reaches a non-positive target number",
            code="target_not_positive",
        )
    return strip


def apply_publication(
    old_doors: Iterable[Door], strips: Sequence[RenumberStrip]
) -> tuple[set[Door], dict[Door, str], dict[Door, Door]]:
    """Apply one publication to the registered door set of one era.

    Returns ``(new_doors, strip_by_old_door, old_to_new)``.  Overlap and
    collision conflicts raise :class:`RenumberingError` naming the doors; no
    partial mapping is returned in that case.
    """
    old_set = set(old_doors)
    owner: dict[Door, str] = {}
    mapping: dict[Door, Door] = {}
    # First pass: every strip claims its *registered* old doors.  A strip may
    # describe numbers that were never registered; those simply match nothing.
    for strip in strips:
        for old_no in strip.matched_old_numbers():
            old_door = Door(strip.old_street_id, old_no)
            if old_door not in old_set:
                continue
            if old_door in owner:
                raise RenumberingError(
                    f"old door {old_door.street_id}#{old_door.house_no} is covered by "
                    f"both {owner[old_door]} and {strip.strip_id}",
                    code="overlapping_old_coverage",
                    conflicts=[
                        {"door": old_door.as_dict(), "strips": [owner[old_door], strip.strip_id]}
                    ],
                )
            owner[old_door] = strip.strip_id
    # Second pass: build the mapping and check distinct old doors cannot share
    # a new door - including doors that merely carry over unchanged.
    for strip in strips:
        all_old = strip.matched_old_numbers()
        all_new = strip.new_numbers()
        for old_no, new_no in zip(all_old, all_new):
            old_door = Door(strip.old_street_id, old_no)
            if old_door not in old_set:
                # The strip still owns this sequence position; the new number
                # is simply not produced because that old door never existed.
                continue
            new_door = Door(strip.new_street_id, new_no)
            for previous_old, previous_new in mapping.items():
                if previous_new == new_door:
                    raise RenumberingError(
                        f"new door {new_door.street_id}#{new_door.house_no} is targeted by "
                        f"both {previous_old.street_id}#{previous_old.house_no} and "
                        f"{old_door.street_id}#{old_door.house_no}",
                        code="duplicate_new_door",
                        conflicts=[
                            {"new_door": new_door.as_dict(),
                             "old_doors": [previous_old.as_dict(), old_door.as_dict()]}
                        ],
                    )
            mapping[old_door] = new_door
    new_doors: set[Door] = set()
    for door in old_set:
        new_door = mapping.get(door, door)
        # A carried-over door may collide with another strip's target.
        if new_door in new_doors:
            raise RenumberingError(
                f"new door {new_door.street_id}#{new_door.house_no} is targeted by "
                f"both a carried-over door and a renumbering strip",
                code="duplicate_new_door",
                conflicts=[{"new_door": new_door.as_dict(),
                            "old_doors": [door.as_dict()]}],
            )
        new_doors.add(new_door)
    return new_doors, owner, mapping


# ---------------------------------------------------------------------------
# Persistent ledger
# ---------------------------------------------------------------------------


class RenumberingStore:
    """SQLite-backed renumbering ledger, independent of the delivery tables.

    A *catalog* with ``get_street(street_id)`` may be supplied so publications
    can verify that street ids exist; without one, only structural rules and
    door-level conflicts are checked.
    """

    def __init__(self, conn, *, catalog=None, init_schema: bool = True) -> None:
        self.conn = conn
        self.catalog = catalog
        if init_schema:
            self.conn.executescript(_SCHEMA)

    # -- era 0 ---------------------------------------------------------------

    def register_initial_era(
        self, doors: Iterable[Door | tuple[str, int]], *, label: str = "era-0"
    ) -> None:
        """Register the finite door list of era 0 (the starting door book)."""
        normalised = [d if isinstance(d, Door) else Door(str(d[0]), int(d[1])) for d in doors]
        self._validate_doors_positive(normalised)
        if len(set(normalised)) != len(normalised):
            raise RenumberingError("era 0 contains duplicate doors", code="duplicate_door")
        self._require_streets_exist(normalised, era_seq=0)
        with transaction(self.conn):
            if self.conn.execute("SELECT 1 FROM renumber_eras LIMIT 1").fetchone() is not None:
                raise RenumberingError("the initial era is already registered", code="era_exists")
            self.conn.execute(
                "INSERT INTO renumber_eras(era_seq,label) VALUES(0,?)", (label,)
            )
            self.conn.executemany(
                "INSERT INTO renumber_doors(era_seq,street_id,house_no) VALUES(0,?,?)",
                [(d.street_id, d.house_no) for d in normalised],
            )

    def _validate_doors_positive(self, doors: Sequence[Door]) -> None:
        for door in doors:
            if door.house_no <= 0:
                raise RenumberingError(
                    f"non-positive house number {door.house_no} on {door.street_id}",
                    code="target_not_positive",
                )

    def _require_streets_exist(self, doors: Sequence[Door], *, era_seq: int) -> None:
        if self.catalog is None:
            return
        missing = sorted({d.street_id for d in doors if self.catalog.get_street(d.street_id) is None})
        if missing:
            raise RenumberingError(
                f"streets not found in catalog for era {era_seq}: {','.join(missing)}",
                code="unknown_street",
                conflicts=[{"street_id": s} for s in missing],
            )

    # -- publications --------------------------------------------------------

    def publish(
        self,
        strips: Sequence[dict[str, Any] | RenumberStrip],
        *,
        label: str | None = None,
    ) -> int:
        """Apply one grouped publication; returns the new era sequence number.

        The publication is one transaction: any conflict or structural error
        rolls back and creates no era.
        """
        with transaction(self.conn):
            current_era = self.current_era_seq()
            if current_era is None:
                raise RenumberingError("register the initial era first", code="no_initial_era")
            next_era = current_era + 1
            if next_era >= MAX_ERAS:
                raise RenumberingError(
                    f"at most {MAX_ERAS} eras are supported", code="too_many_eras"
                )
            parsed = [
                s if isinstance(s, RenumberStrip) else _strip_from_dict(s, i)
                for i, s in enumerate(strips)
            ]
            ids = [s.strip_id for s in parsed]
            if len(set(ids)) != len(ids):
                raise RenumberingError("duplicate strip_id within one publication",
                                       code="duplicate_strip_id")
            old_doors = self._load_doors(current_era)
            self._require_publication_streets(parsed, current_era, next_era)
            new_doors, _owner, _mapping = apply_publication(old_doors, parsed)
            new_label = label if label is not None else f"era-{next_era}"
            self.conn.execute(
                "INSERT INTO renumber_eras(era_seq,label) VALUES(?,?)",
                (next_era, new_label),
            )
            for ordinal, strip in enumerate(parsed):
                self.conn.execute(
                    "INSERT INTO renumber_strips(strip_id,era_seq,ordinal,old_street_id,"
                    "old_lo,old_hi,parity,new_street_id,new_first,new_step) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        strip.strip_id, current_era, ordinal, strip.old_street_id,
                        strip.old_lo, strip.old_hi, strip.parity, strip.new_street_id,
                        strip.new_first, strip.new_step,
                    ),
                )
            self.conn.executemany(
                "INSERT INTO renumber_doors(era_seq,street_id,house_no) VALUES(?,?,?)",
                [(next_era, d.street_id, d.house_no) for d in sorted(new_doors, key=_door_key)],
            )
            return next_era

    def _require_publication_streets(
        self, strips: Sequence[RenumberStrip], old_era: int, new_era: int
    ) -> None:
        if self.catalog is None:
            return
        old_ids = {s.old_street_id for s in strips}
        new_ids = {s.new_street_id for s in strips}
        missing_old = sorted(s for s in old_ids if self.catalog.get_street(s) is None)
        missing_new = sorted(s for s in new_ids if self.catalog.get_street(s) is None)
        if missing_old or missing_new:
            raise RenumberingError(
                f"unknown streets in publication {old_era}->{new_era}: "
                f"old={missing_old} new={missing_new}",
                code="unknown_street",
                conflicts=[{"street_id": s} for s in missing_old + missing_new],
            )

    # -- queries -------------------------------------------------------------

    def current_era_seq(self) -> int | None:
        row = self.conn.execute("SELECT MAX(era_seq) AS m FROM renumber_eras").fetchone()
        return None if row is None or row["m"] is None else int(row["m"])

    def era_labels(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT era_seq,label FROM renumber_eras ORDER BY era_seq"
        ).fetchall()
        return [{"era_seq": r["era_seq"], "label": r["label"]} for r in rows]

    def _load_doors(self, era_seq: int) -> set[Door]:
        rows = self.conn.execute(
            "SELECT street_id,house_no FROM renumber_doors WHERE era_seq=? "
            "ORDER BY street_id,house_no",
            (era_seq,),
        ).fetchall()
        return {Door(r["street_id"], int(r["house_no"])) for r in rows}

    def registered_doors(self, era_seq: int | None = None) -> list[Door]:
        """The finite registered door list of one era (default: latest)."""
        if era_seq is None:
            era_seq = self.current_era_seq()
        if era_seq is None or not self._era_exists(era_seq):
            raise RenumberingError(f"unknown era {era_seq}", code="unknown_era")
        return sorted(self._load_doors(era_seq), key=_door_key)

    def _era_exists(self, era_seq: int) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM renumber_eras WHERE era_seq=?", (era_seq,)
        ).fetchone() is not None

    def strips(self, era_seq: int | None = None) -> list[RenumberStrip]:
        sql = ("SELECT * FROM renumber_strips"
               + (" WHERE era_seq=?" if era_seq is not None else "")
               + " ORDER BY era_seq,ordinal")
        params = (era_seq,) if era_seq is not None else ()
        rows = self.conn.execute(sql, params).fetchall()
        return [
            RenumberStrip(
                strip_id=r["strip_id"], old_street_id=r["old_street_id"],
                old_lo=r["old_lo"], old_hi=r["old_hi"], parity=r["parity"],
                new_street_id=r["new_street_id"], new_first=r["new_first"],
                new_step=r["new_step"],
            )
            for r in rows
        ]

    def trace_door(self, street_id: str, house_no: int) -> DoorTrace:
        """Trace one frozen door through every era, naming the strips used."""
        start_era = self.current_era_seq()
        if start_era is None:
            raise RenumberingError("no eras registered", code="no_initial_era")
        current = Door(street_id, house_no)
        if current not in self._load_doors(0):
            raise RenumberingError(
                f"door {street_id}#{house_no} is not registered in era 0",
                code="door_not_registered",
                conflicts=[current.as_dict()],
            )
        labels = {r["era_seq"]: r["label"] for r in self.era_labels()}
        traces = [EraTrace(0, labels[0], current.street_id, current.house_no, None, True)]
        for era_seq in range(start_era):
            strips = self.strips(era_seq)
            matched_strip = None
            for strip in strips:
                if strip.old_street_id != current.street_id:
                    continue
                if current.house_no in strip.matched_old_numbers():
                    matched_strip = strip
                    break
            if matched_strip is None:
                traces.append(EraTrace(era_seq + 1, labels[era_seq + 1],
                                       current.street_id, current.house_no, None, True))
                continue
            # Pair by position in the strip's full arithmetic sequence, so an
            # unregistered interval position still consumes its new number.
            index = matched_strip.matched_old_numbers().index(current.house_no)
            new_no = matched_strip.new_numbers()[index]
            current = Door(matched_strip.new_street_id, new_no)
            traces.append(EraTrace(era_seq + 1, labels[era_seq + 1],
                                   current.street_id, current.house_no,
                                   matched_strip.strip_id, False))
        return DoorTrace(street_id, house_no, tuple(traces))

    # -- issued final doors (bound to real parcels) --------------------------

    def issue_door(self, parcel_id: str, street_id: str, house_no: int) -> IssuedDoor:
        """Bind the ledger's final door of a parcel to that real parcel id.

        Re-issuing the same parcel is idempotent only when the door matches;
        the final door must be registered in the current era.
        """
        if not isinstance(parcel_id, str) or not parcel_id.strip():
            raise RenumberingError("parcel_id must be a non-empty string",
                                   code="bad_parcel_id")
        if house_no <= 0:
            raise RenumberingError("house_no must be positive", code="target_not_positive")
        era_seq = self.current_era_seq()
        if era_seq is None:
            raise RenumberingError("no eras registered", code="no_initial_era")
        wanted = Door(street_id, house_no)
        if wanted not in self._load_doors(era_seq):
            raise RenumberingError(
                f"final door {street_id}#{house_no} is not registered in era {era_seq}",
                code="door_not_registered",
                conflicts=[wanted.as_dict()],
            )
        with transaction(self.conn):
            existing = self.conn.execute(
                "SELECT * FROM renumber_issued WHERE parcel_id=?", (parcel_id,)
            ).fetchone()
            if existing is not None:
                if existing["street_id"] == street_id and existing["house_no"] == house_no:
                    return IssuedDoor(parcel_id, street_id, house_no, era_seq)
                raise RenumberingError(
                    f"parcel {parcel_id} is already bound to "
                    f"{existing['street_id']}#{existing['house_no']}",
                    code="issue_conflict",
                    conflicts=[
                        {"parcel_id": parcel_id,
                         "existing": {"street_id": existing["street_id"],
                                      "house_no": existing["house_no"]},
                         "requested": wanted.as_dict()},
                    ],
                )
            self.conn.execute(
                "INSERT INTO renumber_issued(parcel_id,street_id,house_no,era_seq) "
                "VALUES(?,?,?,?)",
                (parcel_id, street_id, house_no, era_seq),
            )
        return IssuedDoor(parcel_id, street_id, house_no, era_seq)

    def issue_parcel(self, parcel_id: str, verify=None) -> IssuedDoor:
        """Issue a parcel's final door from its *frozen* delivery identity.

        ``verify`` is an optional callable returning the committed job mapping
        ``{"street_id", "house_no"}`` (or ``None``); without it the caller must
        supply the frozen door via :meth:`issue_door`.
        """
        if verify is None:
            raise RenumberingError(
                "a job verifier is required to issue from a frozen parcel",
                code="no_verifier",
            )
        job = verify(parcel_id)
        if job is None:
            raise RenumberingError(
                f"parcel {parcel_id} has no committed delivery; cannot issue a door",
                code="parcel_not_committed",
                conflicts=[{"parcel_id": parcel_id}],
            )
        trace = self.trace_door(job["street_id"], int(job["house_no"]))
        final = trace.final
        return self.issue_door(parcel_id, final.street_id, final.house_no)

    def issued_door(self, parcel_id: str) -> IssuedDoor | None:
        row = self.conn.execute(
            "SELECT parcel_id,street_id,house_no,era_seq FROM renumber_issued "
            "WHERE parcel_id=?",
            (parcel_id,),
        ).fetchone()
        if row is None:
            return None
        return IssuedDoor(row["parcel_id"], row["street_id"], int(row["house_no"]),
                          int(row["era_seq"]))

    def list_issued(self) -> list[IssuedDoor]:
        rows = self.conn.execute(
            "SELECT parcel_id,street_id,house_no,era_seq FROM renumber_issued "
            "ORDER BY parcel_id"
        ).fetchall()
        return [IssuedDoor(r["parcel_id"], r["street_id"], int(r["house_no"]),
                           int(r["era_seq"])) for r in rows]

    def verify_claim(self, parcel_id: str, street_id: str, house_no: int) -> dict[str, Any]:
        """Check a pickup slip against the issued, parcel-bound final door.

        Returns a structured verdict; callers should reject whenever ``valid``
        is false.  A slip naming an old/alias door number never matches.
        """
        issued = self.issued_door(parcel_id)
        base = {"parcel_id": parcel_id, "claimed": {"street_id": street_id,
                                                     "house_no": house_no}}
        if issued is None:
            return {**base, "valid": False, "reason": "no_issued_door",
                    "issued": None}
        issued_dict = issued.as_dict()
        if issued.street_id == street_id and issued.house_no == house_no:
            return {**base, "valid": True, "reason": "matches_issued_door",
                    "issued": issued_dict}
        return {**base, "valid": False, "reason": "door_mismatch",
                "issued": issued_dict}

    def snapshot(self) -> dict[str, Any]:
        return {
            "max_eras": MAX_ERAS,
            "eras": self.era_labels(),
            "doors": {
                str(era["era_seq"]): [d.as_dict() for d in self.registered_doors(era["era_seq"])]
                for era in self.era_labels()
            },
            "strips": [
                {**s.as_dict(), "era_seq": r["era_seq"]}
                for r in self.era_labels()[:-1]
                for s in self.strips(r["era_seq"])
            ],
            "issued": [d.as_dict() for d in self.list_issued()],
        }


def _door_key(door: Door) -> tuple[str, int]:
    return door.street_id, door.house_no
