"""Building registry: recognise the *building* of a successfully submitted job.

Two parcels may share an exact street and house number yet belong to different
buildings (different station/delivery identities).  The building is therefore
never guessed from the address text alone.  It is recognised from the stable
facts frozen on a successfully submitted :mod:`~address_delivery.delivery` job:

* the stable street id,
* the integer house number,
* the frozen station (delivery identity).

A building claims a house-number segment on one street.  A segment may be
restricted to odd or even numbers; ``any`` covers every integer in range and
the endpoints are inclusive.  Recognition outcomes:

================  ===========================================================
``unique``        exactly one building matches the frozen street/number.
``two_buildings`` two buildings match the street/number; the concierge must
                  re-check manually (a parcel is never auto-assigned here).
``unmatched``     no building's segment covers the frozen number.
``no_job``        the parcel never produced a successful, frozen job.
================  ===========================================================

A building may additionally pin a station.  When a building pins a station,
only jobs frozen to that station can be recognised as it, which is what keeps
same-address/different-station parcels in separate buildings.

Like the catalog and the job store, this is an independent SQLite component:
it owns only its own table and never writes the catalog or job tables.  The
existing counter ("柜面") surface is untouched; this module is purely additive.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from .errors import BuildingError

PARITIES = ("any", "odd", "even")

# Recognition outcome vocabulary ------------------------------------------------
BUILDING_UNIQUE = "unique"
BUILDING_TWO = "two_buildings"
BUILDING_UNMATCHED = "unmatched"
BUILDING_NO_JOB = "no_job"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS buildings (
    building_id  TEXT PRIMARY KEY,
    street_id    TEXT NOT NULL,
    lo           INTEGER NOT NULL,
    hi           INTEGER NOT NULL,
    parity       TEXT NOT NULL CHECK (parity IN ('any','odd','even')),
    station_id   TEXT,
    name         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_buildings_street ON buildings(street_id);
"""


@dataclass(frozen=True)
class Building:
    """One registered building segment."""

    building_id: str
    street_id: str
    lo: int
    hi: int
    parity: str
    station_id: str | None
    name: str

    def covers(self, house_no: int) -> bool:
        if not (self.lo <= house_no <= self.hi):
            return False
        if self.parity == "odd" and house_no % 2 == 0:
            return False
        if self.parity == "even" and house_no % 2 == 1:
            return False
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "building_id": self.building_id,
            "street_id": self.street_id,
            "lo": self.lo,
            "hi": self.hi,
            "parity": self.parity,
            "station_id": self.station_id,
            "name": self.name,
        }


@dataclass(frozen=True)
class BuildingRecognition:
    """Result of recognising one parcel's building from its frozen job."""

    parcel_id: str
    outcome: str
    buildings: tuple[Building, ...]
    job: dict[str, Any] | None

    @property
    def building_id(self) -> str | None:
        """The recognised building id, or ``None`` unless exactly one matched."""
        return self.buildings[0].building_id if self.outcome == BUILDING_UNIQUE else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "parcel_id": self.parcel_id,
            "outcome": self.outcome,
            "building_id": self.building_id,
            "buildings": [b.as_dict() for b in self.buildings],
            "job": self.job,
        }


class BuildingRegistry:
    """Building definitions and frozen-job recognition over a SQLite connection."""

    def __init__(self, conn, *, init_schema: bool = True) -> None:
        self.conn = conn
        if init_schema:
            self.conn.executescript(_SCHEMA)

    # -- construction --------------------------------------------------------

    def register_building(
        self,
        building_id: str,
        street_id: str,
        lo: int,
        hi: int,
        *,
        parity: str = "any",
        station_id: str | None = None,
        name: str | None = None,
    ) -> Building:
        """Register one building segment.

        Overlapping segments on the same street are allowed on purpose: an
        address that lands in two of them yields ``two_buildings`` and goes to
        the concierge for manual re-check rather than being auto-assigned.
        """
        if not isinstance(building_id, str) or not building_id:
            raise BuildingError("building_id must be a non-empty string")
        if not isinstance(street_id, str) or not street_id:
            raise BuildingError("street_id must be a non-empty string")
        if isinstance(lo, bool) or isinstance(hi, bool) or not (
            isinstance(lo, int) and isinstance(hi, int)
        ):
            raise BuildingError("lo/hi must be integers")
        if not (1 <= lo <= hi):
            raise BuildingError(f"building {building_id}: require 1 <= lo <= hi")
        if parity not in PARITIES:
            raise BuildingError(f"building {building_id}: parity must be one of {PARITIES}")
        if self.conn.execute(
            "SELECT 1 FROM buildings WHERE building_id=?", (building_id,)
        ).fetchone():
            raise BuildingError(f"building {building_id} already registered")
        self.conn.execute(
            "INSERT INTO buildings(building_id,street_id,lo,hi,parity,station_id,name) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                building_id,
                street_id,
                lo,
                hi,
                parity,
                station_id,
                name if name is not None else building_id,
            ),
        )
        return self.get_building(building_id)  # type: ignore[return-value]

    def register_many(self, definitions: Sequence[dict[str, Any]]) -> list[Building]:
        created: list[Building] = []
        for definition in definitions:
            created.append(
                self.register_building(
                    definition["building_id"],
                    definition["street_id"],
                    int(definition["lo"]),
                    int(definition["hi"]),
                    parity=definition.get("parity", "any"),
                    station_id=definition.get("station_id"),
                    name=definition.get("name"),
                )
            )
        return created

    # -- queries -------------------------------------------------------------

    def get_building(self, building_id: str) -> Building | None:
        row = self.conn.execute(
            "SELECT * FROM buildings WHERE building_id=?", (building_id,)
        ).fetchone()
        return None if row is None else _building_row(row)

    def list_buildings(self, street_id: str | None = None) -> list[Building]:
        if street_id is None:
            rows = self.conn.execute(
                "SELECT * FROM buildings ORDER BY street_id, building_id"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM buildings WHERE street_id=? ORDER BY building_id",
                (street_id,),
            ).fetchall()
        return [_building_row(r) for r in rows]

    def candidates_for(self, street_id: str, house_no: int) -> list[Building]:
        """All buildings whose segment covers *house_no* on *street_id*."""
        return [
            b
            for b in self.list_buildings(street_id)
            if b.covers(house_no)
        ]

    # -- recognition ---------------------------------------------------------

    def recognize_job(self, job: dict[str, Any]) -> BuildingRecognition:
        """Recognise the building of an already-frozen job dict."""
        matches = self._match(job["street_id"], job["house_no"], job["station_id"])
        outcome = self._outcome(matches)
        return BuildingRecognition(
            parcel_id=job["parcel_id"],
            outcome=outcome,
            buildings=tuple(matches),
            job=_job_summary(job),
        )

    def recognize_parcel(self, delivery, parcel_id: str) -> BuildingRecognition:
        """Recognise a parcel's building through its successful frozen job.

        A parcel without a committed job is ``no_job``: unsuccessful work may
        not be supplemented into a building, so no building is ever inferred
        from a failed or merely attempted address.
        """
        job = delivery.get_job(parcel_id)
        if job is None:
            return BuildingRecognition(
                parcel_id=parcel_id,
                outcome=BUILDING_NO_JOB,
                buildings=(),
                job=None,
            )
        return self.recognize_job(job)

    def recognize_parcels(self, delivery, parcel_ids: Sequence[str]) -> list[BuildingRecognition]:
        return [self.recognize_parcel(delivery, pid) for pid in parcel_ids]

    # -- internals -----------------------------------------------------------

    def _match(self, street_id: str, house_no: int, station_id: str) -> list[Building]:
        matches: list[Building] = []
        for building in self.candidates_for(street_id, house_no):
            # A station-pinned building only accepts jobs frozen to that
            # station; this is what separates same-address/different-station
            # parcels into different buildings.
            if building.station_id is not None and building.station_id != station_id:
                continue
            matches.append(building)
        matches.sort(key=lambda b: b.building_id)
        return matches

    @staticmethod
    def _outcome(matches: Sequence[Building]) -> str:
        if len(matches) == 1:
            return BUILDING_UNIQUE
        if len(matches) >= 2:
            return BUILDING_TWO
        return BUILDING_UNMATCHED


def _building_row(row) -> Building:
    return Building(
        building_id=row["building_id"],
        street_id=row["street_id"],
        lo=row["lo"],
        hi=row["hi"],
        parity=row["parity"],
        station_id=row["station_id"],
        name=row["name"],
    )


def _job_summary(job: dict[str, Any]) -> dict[str, Any]:
    """Only the stable, frozen facts that justify a building recognition."""
    return {
        "parcel_id": job["parcel_id"],
        "street_id": job["street_id"],
        "district_id": job["district_id"],
        "city_id": job["city_id"],
        "house_no": job["house_no"],
        "station_id": job["station_id"],
        "catalog_version": job["catalog_version"],
        "identity": job["identity_key"],
    }
