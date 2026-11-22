"""Label printing: pages, shared district headers, bright cells and planning.

A *page* has one shared district header and one or more rows.  Each row is a
street name, a separator and a decimal house number.

The physical page is shared between counters, so a page is *valid* only when
**every** receiving counter, reading with **every** table still consistent with
its proofs, uniquely recovers each row's target street.  A counter is modelled
by a :class:`Recipient` = (its own reader, its own frozen catalog).

Semantics
---------
* Paper characters are counted per NFC code point.  The header counts once per
  page; each row is billed with its own cells.  No generous spacing is counted.
* Every printed name must be a registered canonical name or alias in the
  *designated* reader edition (the edition the print run is keyed to).
* Decoding uses one table per (counter, line).  The shared header and the street
  name are decoded together, so one paper glyph can never read as two different
  glyphs within one line.
* A *bright* cell (明字印头) is read literally by every reader, ignoring that
  reader's confusion table.  Each bright location costs one impression and is
  local to that cell.
* Recognition follows city -> district -> street with the *same* decoded header
  and street.  Unlike the delivery business layer, a printed row carries a fixed
  house number, so recognition **uses a valid station to exclude same-named
  streets**: a candidate street that serves no unique station for that number
  cannot be the target ("依赖有效站点来排除同名街").  Ambiguity remains only
  when two deliverable same-named streets survive.

The planner paginates rows in their existing house-number order: a page is a
contiguous run of same-district rows.  For each header/variant/bright choice it
computes a per-row frontier and combines rows with a small impressions-bounded
knapsack; bundles are then chosen with a shortest-path DP over row prefixes.
"""
from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .catalog import Catalog
from .errors import LayoutError
from .readers import Reader

ROLE_HEADER = "header"
ROLE_STREET = "street"


def _nfc(value: str) -> str:
    return unicodedata.normalize("NFC", value)


# ---------------------------------------------------------------------------
# Recipients, stable addresses and name variants
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Recipient:
    """One receiving counter: its reader and its frozen catalog."""

    reader: Reader
    catalog: Catalog

    @property
    def edition(self) -> str:
        return self.reader.version

    def reading_pairs(self) -> list[tuple[Reader, dict[str, str], Catalog]]:
        return [(self.reader, table, self.catalog) for table in self.reader.all_tables()]


@dataclass(frozen=True)
class StableAddress:
    parcel_id: str
    city_id: str
    district_id: str
    street_id: str
    house_no: int


@dataclass(frozen=True)
class DistrictVariant:
    text: str
    index: int
    primary: bool

    @property
    def cells(self) -> int:
        return len(_nfc(self.text))


@dataclass(frozen=True)
class StreetVariant:
    text: str
    index: int
    primary: bool


def _ordered_names(catalog: Catalog, table: str, key: str, value: str) -> list[tuple[str, bool]]:
    rows = catalog.conn.execute(
        f"SELECT text, is_primary FROM {table} WHERE {key}=? "
        "ORDER BY is_primary DESC, text",
        (value,),
    ).fetchall()
    return [(row["text"], bool(row["is_primary"])) for row in rows]


def district_variants(catalog: Catalog, district_id: str) -> list[DistrictVariant]:
    if catalog.get_district(district_id) is None:
        raise LayoutError(f"unknown district {district_id}")
    return [
        DistrictVariant(text=text, index=i, primary=primary)
        for i, (text, primary) in enumerate(
            _ordered_names(catalog, "district_names", "district_id", district_id)
        )
    ]


def street_variants(catalog: Catalog, street_id: str) -> list[StreetVariant]:
    if catalog.get_street(street_id) is None:
        raise LayoutError(f"unknown street {street_id}")
    return [
        StreetVariant(text=text, index=i, primary=primary)
        for i, (text, primary) in enumerate(
            _ordered_names(catalog, "street_names", "street_id", street_id)
        )
    ]


# ---------------------------------------------------------------------------
# Page model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RowPlan:
    address: StableAddress
    street: StreetVariant
    separator: str = "/"

    def digits(self) -> str:
        return str(self.address.house_no)

    def paper(self) -> str:
        return f"{self.street.text}{self.separator}{self.digits()}"

    def cells(self) -> int:
        return len(_nfc(self.paper()))

    def street_end(self) -> int:
        return len(_nfc(self.street.text))


@dataclass
class PagePlan:
    header: DistrictVariant
    rows: list[RowPlan]
    edition: str
    bright: set[tuple[str, int, int]] = field(default_factory=set)
    frontier: tuple[tuple[int, int], ...] = ()

    # -- counting ------------------------------------------------------------

    def header_cells(self) -> int:
        return self.header.cells

    def row_cells(self, row: RowPlan) -> int:
        return row.cells()

    def cells(self) -> int:
        return self.header_cells() + sum(self.row_cells(r) for r in self.rows)

    def impressions(self) -> int:
        return len(self.bright)

    def row_count(self) -> int:
        return len(self.rows)

    # -- brightness ----------------------------------------------------------

    def header_bright(self) -> list[int]:
        return sorted(i for role, _ri, i in self.bright if role == ROLE_HEADER)

    def row_bright(self, index: int) -> list[int]:
        return sorted(i for role, ri, i in self.bright if role == ROLE_STREET and ri == index)

    def _page_offset(self, role: str, ri: int, cell: int) -> int:
        if role == ROLE_HEADER:
            return cell
        base = self.header_cells()
        for earlier in range(ri):
            base += self.row_cells(self.rows[earlier])
        return base + cell

    def bright_positions(self) -> list[dict[str, Any]]:
        positions = []
        for role, ri, cell in sorted(self.bright):
            paper = self.header.text if role == ROLE_HEADER else self.rows[ri].paper()
            positions.append(
                {
                    "role": role,
                    "row": ri if role == ROLE_STREET else None,
                    "cell": cell,
                    "paper_glyph": _nfc(paper)[cell],
                    "page_offset": self._page_offset(role, ri, cell),
                }
            )
        return positions

    # -- decoding ------------------------------------------------------------

    def decode_header(self, table: dict[str, str]) -> str:
        bright = set(self.header_bright())
        return "".join(
            g if i in bright else table.get(g, g)
            for i, g in enumerate(_nfc(self.header.text))
        )

    def decode_row(self, table: dict[str, str], index: int) -> dict[str, Any]:
        row = self.rows[index]
        paper = _nfc(row.paper())
        bright = set(self.row_bright(index))
        chars = [g if i in bright else table.get(g, g) for i, g in enumerate(paper)]
        end = row.street_end()
        return {
            "header": self.decode_header(table),
            "street": "".join(chars[:end]),
            "separator": chars[end],
            "digits": "".join(chars[end + 1 :]),
        }

    # -- validation ----------------------------------------------------------

    def row_failure(
        self, catalog: Catalog, table: dict[str, str], index: int
    ) -> dict[str, Any] | None:
        row = self.rows[index]
        decoded = self.decode_row(table, index)
        house = _decimal(decoded["digits"])
        if decoded["separator"] != row.separator:
            return {"row": index, "reason": "bad_separator", "target": row.address.street_id}
        if house is None or house != row.address.house_no:
            return {"row": index, "reason": "bad_house", "target": row.address.street_id}
        result = resolve_row(
            catalog, row.address.city_id, decoded["header"], decoded["street"], house
        )
        # Success requires both the *right* stable street AND reason "ok":
        # an inactive/no-station/ambiguous decode that happens to name the
        # target street is still a failure.
        if result["reason"] != "ok" or result["street_id"] != row.address.street_id:
            return {
                "row": index,
                "reason": result["reason"],
                "target": row.address.street_id,
                "decoded_header": decoded["header"],
                "decoded_street": decoded["street"],
                "candidates": result["candidates"],
            }
        return None

    def valid_for(self, recipient: Recipient) -> bool:
        if not recipient.reader.all_tables():
            return False
        return all(
            self.row_failure(catalog, table, i) is None
            for _r, table, catalog in recipient.reading_pairs()
            for i in range(len(self.rows))
        )

    def valid_for_all(self, recipients: Iterable[Recipient]) -> bool:
        return all(self.valid_for(r) for r in recipients)

    # -- receipts ------------------------------------------------------------

    def affirm_receipt(self, recipient: Recipient) -> dict[str, Any]:
        """Re-read this physical page on a counter; report every table."""
        tables = recipient.reader.all_tables()
        readings = []
        ok = bool(tables)
        for table in tables:
            fails = [
                f
                for i in range(len(self.rows))
                if (f := self.row_failure(recipient.catalog, table, i)) is not None
            ]
            decoded = [self.decode_row(table, i) for i in range(len(self.rows))]
            readings.append({"decoded": decoded, "ok": not fails, "failures": fails})
            if fails:
                ok = False
        return {
            "reader_id": recipient.reader.reader_id,
            "edition": recipient.edition,
            "page_cells": self.cells(),
            "impressions": self.impressions(),
            "ok": ok,
            "tables": len(tables),
            "readings": readings,
        }


def _decimal(text: str) -> int | None:
    if not text or not text.isascii() or not text.isdecimal():
        return None
    return int(text)


def resolve_row(
    catalog: Catalog,
    city_id: str,
    district_text: str,
    street_text: str,
    house_no: int,
) -> dict[str, Any]:
    """Recognise the target stable street through the complete parent chain.

    Unlike the *delivery business* layer (where coverage never collapses an
    address tie), label recognition is explicitly allowed - and required - to
    **use a valid station to exclude same-named streets**: a printed row has a
    fixed house number, so a candidate street that serves no unique station for
    that number cannot be the target.

    The chain is city -> district -> street, always with the *same* decoded
    header and street.  Reasons reported:

    * ``unknown_city`` / ``unknown_district`` / ``unknown_street`` - no hit;
    * ``ambiguous`` - more than one *deliverable* street remains after station
      exclusion;
    * ``inactive`` - the only candidate(s) are deactivated;
    * ``no_station`` / ``ambiguous_station`` - a candidate serves no unique
      station for this house number.

    ``street_id`` is set only on ``ok`` (or on the terminal ``inactive`` street
    so callers can explain it).
    """
    resolution = catalog.resolve(city_id, district_text, street_text, house_no)
    raw = [{"street_id": c.street_id, "active": c.active} for c in resolution.candidates]
    if not resolution.city_found:
        return {"street_id": None, "reason": "unknown_city", "candidates": []}
    if not resolution.district_ids:
        return {"street_id": None, "reason": "unknown_district", "candidates": []}
    if not resolution.candidates:
        return {"street_id": None, "reason": "unknown_street", "candidates": []}

    deliverable: list[str] = []
    inactive: list[str] = []
    no_station: list[str] = []
    ambiguous_station: list[str] = []
    for candidate in resolution.candidates:
        if not candidate.active:
            inactive.append(candidate.street_id)
            continue
        station, hits, _stations = catalog.station_for(candidate.street_id, house_no)
        if not hits:
            no_station.append(candidate.street_id)
        elif station is None:
            ambiguous_station.append(candidate.street_id)
        else:
            deliverable.append(candidate.street_id)

    if len(deliverable) == 1:
        return {"street_id": deliverable[0], "reason": "ok", "candidates": raw}
    if len(deliverable) > 1:
        return {"street_id": None, "reason": "ambiguous", "candidates": raw}
    # Nothing deliverable: explain the dominant reason.
    if ambiguous_station:
        return {
            "street_id": ambiguous_station[0],
            "reason": "ambiguous_station",
            "candidates": raw,
        }
    if no_station:
        return {"street_id": no_station[0], "reason": "no_station", "candidates": raw}
    return {"street_id": inactive[0], "reason": "inactive", "candidates": raw}


# ---------------------------------------------------------------------------
# Bright search
# ---------------------------------------------------------------------------


def _confusable_positions(reader: Reader, text: str) -> list[int]:
    confusing = reader.confusing_chars()
    return [i for i, g in enumerate(_nfc(text)) if g in confusing]


def _pair_frontier(options: list[tuple[int, int, Any]]) -> list[tuple[int, int, Any]]:
    """Pareto frontier on (cells, impressions), stable order."""
    options = sorted(options, key=lambda p: (p[0], p[1], repr(p[2])))
    out: list[tuple[int, int, Any]] = []
    best_imps = None
    for option in options:
        if best_imps is not None and option[1] >= best_imps:
            continue
        out.append(option)
        best_imps = option[1] if best_imps is None else min(best_imps, option[1])
    return out


def _header_options(
    recipients: Sequence[Recipient], header_text: str
) -> list[tuple[int, frozenset[int], tuple[str, ...], tuple[ConsumerPair, ...]]]:
    """Distinct header decodings reachable by brightening, cheapest bright set.

    Returns ``(impressions, bright_cells, decodes_by_pair, pairs)``.  Pairs are
    the flattened ``(reader, table, catalog)`` reading combinations; the decode
    vector is aligned to it so the same table is used for header and street.
    """
    pairs = [p for r in recipients for p in r.reading_pairs()]
    chars = _nfc(header_text)
    masks = _distinct_masks(chars, [r.reader for r in recipients])
    best: dict[tuple[str, ...], tuple[int, frozenset[int]]] = {}
    for bright in masks:
        decodes = tuple(
            "".join(chars[i] if i in bright else table.get(chars[i], chars[i]) for i in range(len(chars)))
            for _reader, table, _catalog in pairs
        )
        if decodes not in best or len(bright) < best[decodes][0]:
            best[decodes] = (len(bright), bright)
    return [
        (cost, bright, decodes, tuple(pairs))
        for decodes, (cost, bright) in best.items()
    ]


ConsumerPair = tuple[Reader, dict[str, str], Catalog]


def _distinct_masks(chars: str, readers) -> list[frozenset[int]]:
    """All bright masks over the cells that any reader might confuse.

    Only positions whose glyph is non-literal in some table need brightening,
    and a cell that no table confuses never benefits from an impression.
    """
    confusing: set[str] = set()
    for reader in readers:
        confusing |= reader.confusing_chars()
    positions = [i for i, g in enumerate(chars) if g in confusing]
    return [
        frozenset(positions[i] for i in range(len(positions)) if mask >> i & 1)
        for mask in range(1 << len(positions))
    ]


def _row_frontier(
    pairs: Sequence[ConsumerPair],
    header_decodes: tuple[str, ...],
    row: RowPlan,
) -> list[tuple[int, int, frozenset[int]]]:
    """Valid (cells, impressions, bright) options for one row on one page.

    Validity is required for every pair, each pair using its own table (and the
    matching header decode at the same index) - so a glyph is never read two
    ways within a line.
    """
    paper = _nfc(row.paper())
    extra_readers = [reader for reader, _t, _c in pairs]
    bright_masks = _distinct_masks(paper, extra_readers)
    end = row.street_end()
    out = []
    for bright in bright_masks:
        good = True
        for (reader, table, catalog), header_decode in zip(pairs, header_decodes):
            chars = [paper[j] if j in bright else table.get(paper[j], paper[j]) for j in range(len(paper))]
            street = "".join(chars[:end])
            sep = chars[end] if end < len(chars) else ""
            digits = "".join(chars[end + 1 :])
            if sep != row.separator:
                good = False
                break
            house = _decimal(digits)
            if house is None or house != row.address.house_no:
                good = False
                break
            result = resolve_row(catalog, row.address.city_id, header_decode, street, house)
            if result["reason"] != "ok" or result["street_id"] != row.address.street_id:
                good = False
                break
        if good:
            out.append((row.cells(), len(bright), bright))
    return _pair_frontier(out)


# ---------------------------------------------------------------------------
# Page layouts
# ---------------------------------------------------------------------------


@dataclass
class PageOption:
    page: PagePlan

    @property
    def cells(self) -> int:
        return self.page.cells()

    @property
    def impressions(self) -> int:
        return self.page.impressions()


def page_layouts(
    jobs: Sequence[Any],
    designated: Catalog,
    recipients: Sequence[Recipient],
    *,
    max_cells: int,
) -> list[PageOption]:
    """Pareto-optimal layouts for a contiguous run of same-district rows."""
    if not jobs:
        return []
    district_id = jobs[0].address.district_id
    # A counter whose proofs admit no table has withdrawn its credential; a
    # page cannot be certified readable for it, so no page is feasible.
    if any(not r.reader.all_tables() for r in recipients):
        return []
    all_pairs = [p for r in recipients for p in r.reading_pairs()]
    if not all_pairs:
        return []
    pages: list[PagePlan] = []
    for header in district_variants(designated, district_id):
        for header_imps, header_bright, header_decodes, pairs in _header_options(recipients, header.text):
            row_frontiers = []
            for job in jobs:
                per_variant = []
                for street in street_variants(designated, job.address.street_id):
                    row = RowPlan(address=job.address, street=street, separator=job.separator)
                    options = _row_frontier(pairs, header_decodes, row)
                    for cells, imps, bright in options:
                        per_variant.append((cells, imps, (row, bright)))
                if not per_variant:
                    row_frontiers = None
                    break
                row_frontiers.append(_pair_frontier(per_variant))
            if row_frontiers is None:
                continue
            # Combine rows: min cells per impression count.
            combined = {header_imps: (0, [])}
            for frontier in row_frontiers:
                nxt: dict[int, tuple[int, list]] = {}
                for imps_so_far, (cells_so_far, chosen) in combined.items():
                    for rc, ri, payload in frontier:
                        imp = imps_so_far + ri
                        cand = (cells_so_far + rc, chosen + [payload])
                        if imp not in nxt or cand[0] < nxt[imp][0]:
                            nxt[imp] = cand
                combined = nxt
            page_frontier = []
            for imp, (cells, chosen) in combined.items():
                total = header.cells + cells
                page_frontier.append((total, imp, chosen))
            page_frontier = _pair_frontier(page_frontier)
            frontier_shape = tuple((c, i) for c, i, _ in page_frontier)
            for total, imp, chosen in page_frontier:
                if total > max_cells:
                    continue
                page = PagePlan(
                    header=header,
                    rows=[rp for rp, _b in chosen],
                    edition=recipients[0].edition,
                    frontier=frontier_shape,
                )
                bright: set[tuple[str, int, int]] = {
                    (ROLE_HEADER, 0, c) for c in header_bright
                }
                for ri, (_rp, rb) in enumerate(chosen):
                    for c in rb:
                        bright.add((ROLE_STREET, ri, c))
                page.bright = bright
                if not page.valid_for_all(recipients):
                    continue
                pages.append(page)
    return _pareto_pages(pages)


def _pareto_pages(pages: list[PagePlan]) -> list[PageOption]:
    seen = set()
    options: list[PageOption] = []
    for page in sorted(pages, key=lambda p: (p.cells(), p.impressions(), p.header.text,
                                             tuple(r.street.text for r in p.rows))):
        key = (page.header.index, tuple(r.street.index for r in page.rows),
               tuple(r.address.parcel_id for r in page.rows),
               frozenset(page.bright))
        if key in seen:
            continue
        seen.add(key)
        options.append(PageOption(page))
    options.sort(key=lambda o: (o.cells, o.impressions))
    out: list[PageOption] = []
    best_imps = None
    for option in options:
        if best_imps is not None and option.impressions >= best_imps:
            continue
        out.append(option)
        best_imps = option.impressions if best_imps is None else min(best_imps, option.impressions)
    return out


# ---------------------------------------------------------------------------
# Deck planning
# ---------------------------------------------------------------------------


@dataclass
class PlanJob:
    address: StableAddress
    max_rows: int
    max_cells: int
    separator: str = "/"


@dataclass
class DeckSolution:
    pages: list[PagePlan]

    @property
    def cells(self) -> int:
        return sum(p.cells() for p in self.pages)

    @property
    def impressions(self) -> int:
        return sum(p.impressions() for p in self.pages)

    def as_dict(self, recipients: Sequence[Recipient] = ()) -> dict[str, Any]:
        return grouped_serialize(self.pages, recipients)


def _bundle_pool(
    jobs: Sequence[PlanJob],
    designated: Catalog,
    recipients: Sequence[Recipient],
) -> dict[int, list[tuple[int, list[PageOption]]]]:
    """start -> list of (end_exclusive, page options) for every contiguous run."""
    pool: dict[int, list[tuple[int, list[PageOption]]]] = {}
    n = len(jobs)
    for start in range(n):
        proto = jobs[start]
        limit = min(n, start + proto.max_rows)
        for end in range(start + 1, limit + 1):
            group = jobs[start:end]
            if any(j.address.district_id != proto.address.district_id for j in group):
                break
            if any(j.max_rows != proto.max_rows or j.max_cells != proto.max_cells for j in group):
                break
            layouts = page_layouts(group, designated, recipients, max_cells=proto.max_cells)
            if layouts:
                pool.setdefault(start, []).append((end, layouts))
    return pool


def plan_deck(
    jobs: Sequence[PlanJob],
    designated: Catalog,
    recipients: Sequence[Recipient],
    *,
    max_impressions: int | None = None,
) -> DeckSolution | None:
    """Minimise total cells subject to ``max_impressions`` (then impressions).

    *designated* is the reader edition whose registered names may be printed;
    *recipients* are every counter that will read the physical page and whose
    live catalog and reader tables must all agree on the destination.
    """
    if not jobs:
        return DeckSolution(pages=[])
    pool = _bundle_pool(jobs, designated, recipients)
    cap = max_impressions if max_impressions is not None else 10**9
    n = len(jobs)
    # state[i][imps] = (min_cells, prev_imps, page)
    state: dict[int, dict[int, tuple[int, int | None, PagePlan | None]]] = {0: {0: (0, None, None)}}
    for i in range(n):
        if i not in state:
            continue
        for end, layouts in pool.get(i, []):
            for option in layouts:
                for imps_used, (cells_used, _pi, _pp) in list(state[i].items()):
                    new_imps = imps_used + option.impressions
                    if new_imps > cap:
                        continue
                    new_cells = cells_used + option.cells
                    slot = state.setdefault(end, {})
                    if new_imps not in slot or new_cells < slot[new_imps][0]:
                        slot[new_imps] = (new_cells, imps_used, option.page)
    finals = state.get(n)
    if not finals:
        return None
    best_imps = min(finals, key=lambda im: (finals[im][0], im))
    return _walk_back(state, n, best_imps)


def _walk_back(state, n: int, imps: int) -> DeckSolution:
    pages: list[PagePlan] = []
    i = n
    while i > 0:
        _cells, prev_imps, page = state[i][imps]
        pages.append(page)
        i -= page.row_count()
        imps = prev_imps if prev_imps is not None else 0
    pages.reverse()
    return DeckSolution(pages=pages)


def plan_min_impressions(
    jobs: Sequence[PlanJob],
    designated: Catalog,
    recipients: Sequence[Recipient],
    *,
    target_cells: int,
) -> tuple[DeckSolution | None, int]:
    """Fewest impressions within *target_cells* of paper (then fewest cells).

    *target_cells* is the cell allowance already paid for; a layout must use at
    most that many cells.  Returns the plan and the impressions it needs.
    ``(None, -1)`` if even the longest safe form exceeds the allowance.
    """
    if not jobs:
        return DeckSolution(pages=[]), 0
    pool = _bundle_pool(jobs, designated, recipients)
    n = len(jobs)
    # state[i][imps][cells] = (prev_imps, page)
    state: dict[int, dict[int, dict[int, tuple[int | None, PagePlan | None]]]] = {
        0: {0: {0: (None, None)}}
    }
    for i in range(n):
        if i not in state:
            continue
        for end, layouts in pool.get(i, []):
            for option in layouts:
                for imps_used, cellmap in list(state[i].items()):
                    new_imps = imps_used + option.impressions
                    for cells_used in list(cellmap.keys()):
                        new_cells = cells_used + option.cells
                        slot = state.setdefault(end, {}).setdefault(new_imps, {})
                        if new_cells not in slot:
                            slot[new_cells] = (imps_used, option.page)
    finals = state.get(n)
    if not finals:
        return None, -1
    candidates = [
        (imps, cells)
        for imps, cellmap in finals.items()
        for cells in cellmap
        if cells <= target_cells
    ]
    if not candidates:
        return None, -1
    best_imps, best_cells = min(candidates)
    pages: list[PagePlan] = []
    i, cur_imps, cur_cells = n, best_imps, best_cells
    while i > 0:
        prev_imps, page = state[i][cur_imps][cur_cells]
        pages.append(page)
        i -= page.row_count()
        cur_cells -= page.cells()
        cur_imps = prev_imps if prev_imps is not None else 0
    pages.reverse()
    return DeckSolution(pages=pages), best_imps


# ---------------------------------------------------------------------------
# Diagnosis of a raw paper reading
# ---------------------------------------------------------------------------


def diagnose(
    reader: Reader,
    catalog: Catalog,
    city_id: str,
    district_paper: str,
    street_paper: str,
    house_no: int,
    *,
    district_bright: Iterable[int] = (),
    street_bright: Iterable[int] = (),
) -> dict[str, Any]:
    """Walk the full parent chain for every surviving table of one counter.

    The header and street are decoded with the same table, so a glyph is never
    read two ways in one line.  Reports every possible stable street and a
    credential per failure (ambiguous / unknown / inactive / no station /
    ambiguous station).
    """
    tables = reader.all_tables()
    if not tables:
        return {
            "reader_id": reader.reader_id,
            "credential_issued": False,
            "status": "contradiction",
            "possible_streets": [],
            "failures": [],
            "readings": [],
        }
    drows, srows = set(district_bright), set(street_bright)
    readings = []
    possible: set[str] = set()
    failures = []
    for table in tables:
        header = "".join(
            g if i in drows else table.get(g, g) for i, g in enumerate(_nfc(district_paper))
        )
        street_text = "".join(
            g if i in srows else table.get(g, g) for i, g in enumerate(_nfc(street_paper))
        )
        result = resolve_row(catalog, city_id, header, street_text, house_no)
        readings.append({"decoded_district": header, "decoded_street": street_text, **result})
        if result["reason"] == "ok" and result["street_id"] is not None:
            possible.add(result["street_id"])
        else:
            failures.append(
                {"decoded_district": header, "decoded_street": street_text, "reason": result["reason"]}
            )
    return {
        "reader_id": reader.reader_id,
        "credential_issued": True,
        "status": "unique" if len(possible) == 1 else ("ambiguous" if possible else "none"),
        "possible_streets": sorted(possible),
        "failures": failures,
        "readings": readings,
    }


# ---------------------------------------------------------------------------
# Serialisation / reporting
# ---------------------------------------------------------------------------


def serialize_page(page: PagePlan, advice: dict[str, Any] | None = None) -> dict[str, Any]:
    rows = []
    for index, row in enumerate(page.rows):
        rows.append(
            {
                "parcel_id": row.address.parcel_id,
                "paper": row.paper(),
                "street_text": row.street.text,
                "street_is_primary": row.street.primary,
                "separator": row.separator,
                "house_no": row.address.house_no,
                "street_id": row.address.street_id,
                "district_id": row.address.district_id,
                "cells": page.row_cells(row),
                "bright_cells": page.row_bright(index),
            }
        )
    if advice is None:
        advice = _page_advice(page)
    return {
        "edition": page.edition,
        "header_text": page.header.text,
        "header_is_primary": page.header.primary,
        "header_cells": page.header_cells(),
        "header_bright_cells": page.header_bright(),
        "rows": rows,
        "row_count": page.row_count(),
        "cells": page.cells(),
        "impressions": page.impressions(),
        "bright_positions": page.bright_positions(),
        "advice": advice,
    }


def _page_advice(page: PagePlan) -> dict[str, Any]:
    """Whether the impressions on a page buy cells, within its own header."""
    no_imp = [c for c, i in page.frontier if i < page.impressions()]
    if not page.frontier:
        return {"impression_justified": None, "alternative_cells": None, "cells_saved": 0}
    if no_imp:
        alt = min(no_imp)
        return {
            "impression_justified": page.impressions() > 0 and alt > page.cells(),
            "alternative_cells": alt,
            "cells_saved": max(0, alt - page.cells()) if page.impressions() else 0,
        }
    return {
        "impression_justified": page.impressions() > 0,
        "alternative_cells": None,
        "cells_saved": 0,
    }


def _deck_page_advice(
    page: PagePlan, designated: Catalog, recipients: Sequence[Recipient]
) -> dict[str, Any]:
    """Deck-level: cheapest zero-impression layout for the *same* rows.

    This is what the print foreman needs: a page whose impressions only shave
    cells can be replaced by a longer shared name and its scarce impressions
    left for another page; a page where the no-impression variant is much
    larger (or impossible) genuinely deserves them.
    """
    if page.impressions() == 0:
        return {"impression_justified": False, "alternative_cells": page.cells(), "cells_saved": 0}
    budget = max(page.cells(), 1) + 64
    jobs = [
        PlanJob(address=row.address, max_rows=len(page.rows), max_cells=budget,
                separator=row.separator)
        for row in page.rows
    ]
    alternatives = page_layouts(jobs, designated, recipients, max_cells=budget)
    zero = [o.cells for o in alternatives if o.impressions == 0]
    if zero:
        alt = min(zero)
        return {
            "impression_justified": alt > page.cells(),
            "alternative_cells": alt,
            "cells_saved": max(0, alt - page.cells()),
        }
    return {"impression_justified": True, "alternative_cells": None, "cells_saved": 0}


def grouped_serialize(
    pages: Sequence[PagePlan], recipients: Sequence[Recipient] = ()
) -> dict[str, Any]:
    header_use: dict[str, list[int]] = {}
    for index, page in enumerate(pages, 1):
        header_use.setdefault(page.header.text, []).append(index)
    designated = recipients[0].catalog if recipients else None
    serialized = []
    for page in pages:
        advice = (
            _deck_page_advice(page, designated, recipients)
            if designated is not None
            else None
        )
        serialized.append(serialize_page(page, advice))
    receipts = {}
    for recipient in recipients:
        receipts[recipient.reader.reader_id] = [p.affirm_receipt(recipient) for p in pages]
    return {
        "total_cells": sum(p.cells() for p in pages),
        "total_impressions": sum(p.impressions() for p in pages),
        "page_count": len(pages),
        "pages": serialized,
        "shared_headers": {
            text: {"pages": idxs, "header_cells": len(_nfc(text))}
            for text, idxs in header_use.items()
            if len(idxs) > 1
        },
        "header_pressings": sum(len(idxs) for idxs in header_use.values()),
        "receipts": receipts,
    }
