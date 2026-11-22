"""Synthetic optical readers: confusion groups, proofs and calibration.

Each reader has a small *confusion group*: equal-sized sets of paper glyphs and
output glyphs.  The machine uses one fixed bijection (paper -> output) for the
whole machine; glyphs outside the group are read literally.  Nothing here does
image recognition - a "reading" is just a character substitution.

A *proof* pairs known paper text with transcribed observations.  Observations
are per aligned cell and may be an exact glyph, a set of candidate glyphs, or
missing.  All proofs on a reader constrain the same bijection.  If no bijection
explains every proof the reader is contradictory and its reading credential is
withdrawn (``Reader.tables() == ()``).

The central consistency rule for a printed line is that the district header and
the street name must be decoded with the *same* table: one paper glyph can
never be read as two different output glyphs inside one line.
"""
from __future__ import annotations

import itertools
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .errors import ReaderContradiction, ReaderError

MISSING = "missing"
EXACT = "exact"
CHOICES = "choices"


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _as_glyph(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReaderError(f"{where}: glyph must be a non-empty string")
    text = _nfc(value)
    if len(text) != 1:
        raise ReaderError(f"{where}: glyph must be a single NFC code point")
    return text


@dataclass(frozen=True)
class Observation:
    """One cell's transcribed output: exact, one of several, or missing."""

    kind: str
    values: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in (EXACT, CHOICES, MISSING):
            raise ReaderError(f"unknown observation kind {self.kind!r}")
        if self.kind == EXACT and len(self.values) != 1:
            raise ReaderError("exact observation needs exactly one value")
        if self.kind == CHOICES and not self.values:
            raise ReaderError("choices observation needs at least one value")
        if self.kind == MISSING and self.values:
            raise ReaderError("missing observation takes no values")

    def accepts(self, glyph: str) -> bool:
        if self.kind == MISSING:
            return True
        return glyph in self.values

    @classmethod
    def exact(cls, glyph: str) -> "Observation":
        return cls(EXACT, (_as_glyph(glyph, "exact"),))

    @classmethod
    def choices(cls, *glyphs: str) -> "Observation":
        return cls(CHOICES, tuple(_as_glyph(g, "choices") for g in glyphs))

    @classmethod
    def missing(cls) -> "Observation":
        return cls(MISSING, ())

    @classmethod
    def from_json(cls, obj: Any) -> "Observation":
        if isinstance(obj, str):
            return cls.exact(obj)
        if not isinstance(obj, dict):
            raise ReaderError("observation must be a string or object")
        if obj.get("missing"):
            return cls.missing()
        if "exact" in obj:
            return cls.exact(obj["exact"])
        if "choices" in obj:
            values = obj["choices"]
            if not isinstance(values, list) or not values:
                raise ReaderError("choices must be a non-empty list")
            return cls.choices(*values)
        raise ReaderError("observation must have exact/choices/missing")

    def as_dict(self) -> dict[str, Any]:
        if self.kind == MISSING:
            return {"missing": True}
        if self.kind == EXACT:
            return {"exact": self.values[0]}
        return {"choices": list(self.values)}


@dataclass(frozen=True)
class Proof:
    """Known paper text with one observation per aligned NFC code point."""

    paper: str
    observations: tuple[Observation, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.paper, str):
            raise ReaderError("proof paper must be a string")
        text = _nfc(self.paper)
        if len(text) != len(self.observations):
            raise ReaderError(
                f"proof {self.paper!r}: {len(text)} paper cells but "
                f"{len(self.observations)} observations"
            )

    @property
    def cells(self) -> str:
        return _nfc(self.paper)

    def check(self, table: dict[str, str]) -> bool:
        for glyph, observation in zip(self.cells, self.observations):
            if not observation.accepts(table.get(glyph, glyph)):
                return False
        return True

    @classmethod
    def from_json(cls, obj: Any) -> "Proof":
        if not isinstance(obj, dict) or not isinstance(obj.get("paper"), str):
            raise ReaderError("proof needs a paper string")
        raw = obj.get("observed", obj.get("observations"))
        if not isinstance(raw, list):
            raise ReaderError("proof needs an observed list")
        return cls(_nfc(obj["paper"]), tuple(Observation.from_json(o) for o in raw))

    def as_dict(self) -> dict[str, Any]:
        return {
            "paper": _nfc(self.paper),
            "observed": [o.as_dict() for o in self.observations],
        }


@dataclass
class Reader:
    """One synthetic reader with a shared confusion group and proofs."""

    reader_id: str
    version: str
    paper_alphabet: Sequence[str]
    output_alphabet: Sequence[str]
    proofs: list[Proof] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not isinstance(self.reader_id, str) or not self.reader_id:
            raise ReaderError("reader_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version:
            raise ReaderError("version must be a non-empty string")
        self.paper_alphabet = self._alphabet(self.paper_alphabet, "paper_alphabet")
        self.output_alphabet = self._alphabet(self.output_alphabet, "output_alphabet")
        if len(self.paper_alphabet) != len(self.output_alphabet):
            raise ReaderError("paper and output alphabets must be the same size")
        self.proofs = list(self.proofs)

    @staticmethod
    def _alphabet(values: Sequence[str], where: str) -> tuple[str, ...]:
        if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
            raise ReaderError(f"{where} must be a list of glyphs")
        glyphs = tuple(_as_glyph(v, where) for v in values)
        if len(set(glyphs)) != len(glyphs):
            raise ReaderError(f"{where} must not contain duplicates")
        return glyphs

    # -- calibration ---------------------------------------------------------

    def all_tables(self) -> tuple[dict[str, str], ...]:
        """Every bijection consistent with all proofs (deterministic order)."""
        tables: list[dict[str, str]] = []
        for permutation in itertools.permutations(self.output_alphabet):
            table = dict(zip(self.paper_alphabet, permutation))
            if all(proof.check(table) for proof in self.proofs):
                tables.append(table)
        return tuple(tables)

    def tables(self) -> tuple[dict[str, str], ...]:
        return self.all_tables()

    @property
    def calibrated(self) -> bool:
        return bool(self.all_tables())

    @property
    def status(self) -> str:
        return "calibrated" if self.calibrated else "contradiction"

    def require_calibrated(self) -> tuple[dict[str, str], ...]:
        tables = self.all_tables()
        if not tables:
            raise ReaderContradiction(
                f"reader {self.reader_id}: proofs contradict every table; "
                "reading credential withdrawn"
            )
        return tables

    def credential(self) -> dict[str, Any]:
        tables = self.all_tables()
        return {
            "reader_id": self.reader_id,
            "version": self.version,
            "status": "calibrated" if tables else "contradiction",
            "tables": [
                {p: t[p] for p in self.paper_alphabet} for t in tables
            ],
            "credential_issued": bool(tables),
        }

    # -- proof maintenance ---------------------------------------------------

    def add_proof(self, proof: Proof) -> "Reader":
        self.proofs.append(proof)
        return self

    def replace_proof(self, index: int, proof: Proof) -> "Reader":
        """Correct one proof and reissue the credential."""
        if not 0 <= index < len(self.proofs):
            raise ReaderError(f"no proof at index {index}")
        self.proofs[index] = proof
        return self

    def remove_proof(self, index: int) -> "Reader":
        if not 0 <= index < len(self.proofs):
            raise ReaderError(f"no proof at index {index}")
        del self.proofs[index]
        return self

    # -- decoding ------------------------------------------------------------

    def decode(
        self,
        paper: str,
        table: dict[str, str],
        bright: Iterable[int] = (),
    ) -> str:
        """Decode *paper* under *table*; ``bright`` cells stay literal."""
        bright_set = set(bright)
        out: list[str] = []
        for index, glyph in enumerate(_nfc(paper)):
            if index in bright_set:
                out.append(glyph)
            else:
                out.append(table.get(glyph, glyph))
        return "".join(out)

    def possible_decodings(
        self, paper: str, bright: Iterable[int] = ()
    ) -> list[str]:
        tables = self.all_tables()
        if not tables:
            raise ReaderContradiction(
                f"reader {self.reader_id}: no table can explain the proofs"
            )
        seen = {self.decode(paper, table, bright) for table in tables}
        return sorted(seen)

    def consistent_decodings(
        self, specs: Sequence[tuple[str, Iterable[int]]]
    ) -> list[tuple[str, ...]]:
        """Decode several paper strings with one shared table per outcome.

        This is where "one glyph, two readings" is ruled out: the district
        header and the street name are decoded by the *same* table, so a
        combination that would need a glyph to be read two ways never appears.
        """
        tables = self.require_calibrated()
        seen: set[tuple[str, ...]] = set()
        for table in tables:
            seen.add(
                tuple(self.decode(paper, table, bright) for paper, bright in specs)
            )
        return sorted(seen)

    def pair_is_possible(
        self,
        header_paper: str,
        header_bright: Iterable[int],
        street_paper: str,
        street_bright: Iterable[int],
        header_out: str,
        street_out: str,
    ) -> bool:
        return (
            header_out,
            street_out,
        ) in self.consistent_decodings(
            [
                (header_paper, header_bright),
                (street_paper, street_bright),
            ]
        )

    # -- technician query ----------------------------------------------------

    def indistinguishable_pairs(
        self, paper: str, bright: Iterable[int] = ()
    ) -> dict[str, Any]:
        """Complete readings still impossible to tell apart for *paper*."""
        if not self.all_tables():
            return {
                "reader_id": self.reader_id,
                "paper": _nfc(paper),
                "status": "contradiction",
                "readings": [],
                "pairs": [],
            }
        readings = self.possible_decodings(paper, bright)
        pairs = [
            [a, b] for a, b in itertools.combinations(readings, 2)
        ]
        return {
            "reader_id": self.reader_id,
            "paper": _nfc(paper),
            "status": "calibrated" if len(readings) == 1 else "ambiguous",
            "readings": readings,
            "pairs": pairs,
        }

    def confusing_chars(self) -> set[str]:
        """Paper glyphs that some surviving table does not read literally."""
        tables = self.all_tables()
        result: set[str] = set()
        for glyph in self.paper_alphabet:
            if any(table.get(glyph, glyph) != glyph for table in tables):
                result.add(glyph)
        return result


def reader_from_json(obj: dict[str, Any]) -> Reader:
    if not isinstance(obj, dict):
        raise ReaderError("reader must be an object")
    confusion = obj.get("confusion", {}) or {}
    paper = confusion.get("paper", obj.get("paper_alphabet", []))
    output = confusion.get("output", obj.get("output_alphabet", []))
    proofs = [Proof.from_json(p) for p in obj.get("proofs", [])]
    return Reader(
        reader_id=obj.get("reader_id", ""),
        version=obj.get("version", ""),
        paper_alphabet=paper,
        output_alphabet=output,
        proofs=proofs,
    )
