"""Command-line entry point.

Examples
--------
Initialise a database from the bundled materials and import the CSV::

    python -m address_delivery init --materials materials/707 --db /tmp/a.db
    python -m address_delivery import --db /tmp/a.db materials/707/addresses.csv

Accept the supplied client's JSON output (piped or from a file)::

    python3 materials/707/csv_client.py materials/707/addresses.csv \\
        | python -m address_delivery ingest --db /tmp/a.db -

Inspect::

    python -m address_delivery resolve --db /tmp/a.db C1 同区 东路 2
    python -m address_delivery explain --db /tmp/a.db P1
    python -m address_delivery job --db /tmp/a.db P2

Catalog-only commands work without any delivery tables being used.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .catalog import Catalog
from .db import connect
from .errors import CatalogError, InputFormatError
from .labels import (
    PlanJob,
    Recipient,
    StableAddress,
    diagnose,
    plan_deck,
    plan_min_impressions,
)
from .readers import Reader, reader_from_json
from .service import AddressService


def _load_json_arg(value: str):
    if value == "-":
        return json.load(sys.stdin)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _dump(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _open_service(db: str) -> AddressService:
    return AddressService.open(db)


def cmd_init(args) -> int:
    db = Path(args.db)
    if db.exists() and not args.force:
        print(f"refusing to overwrite existing {db} (use --force)", file=sys.stderr)
        return 2
    if db.exists():
        db.unlink()
    service = AddressService(str(db))
    try:
        service.load_initial(
            Path(args.materials) / "catalog.json",
            Path(args.materials) / "coverage.json",
        )
        _dump(service.catalog.snapshot())
    finally:
        service.close()
    return 0


def cmd_import(args) -> int:
    service = _open_service(args.db)
    try:
        batch = service.import_csv(args.csv)
        _dump(batch.as_dict())
        return 0 if batch.rows else 0
    except InputFormatError as exc:
        _dump({"error": "input_format", "message": str(exc)})
        return 3
    finally:
        service.close()


def cmd_ingest(args) -> int:
    document = _load_json_arg(args.input)
    service = _open_service(args.db)
    try:
        batch = service.import_address_document(document)
        _dump(batch.as_dict())
        return 0
    except InputFormatError as exc:
        _dump({"error": "input_format", "message": str(exc)})
        return 3
    finally:
        service.close()


def cmd_resolve(args) -> int:
    service = _open_service(args.db)
    try:
        _dump(service.resolve_address(args.city_id, args.district, args.street, args.house_no))
    finally:
        service.close()
    return 0


def cmd_explain(args) -> int:
    service = _open_service(args.db)
    try:
        result = service.explain(args.parcel_id)
        if result is None:
            _dump({"error": "unknown_parcel", "parcel_id": args.parcel_id})
            return 4
        _dump(result)
    finally:
        service.close()
    return 0


def cmd_job(args) -> int:
    service = _open_service(args.db)
    try:
        result = service.delivery.final_destination(args.parcel_id)
        if result is None:
            _dump({"error": "no_committed_job", "parcel_id": args.parcel_id})
            return 4
        _dump(result)
    finally:
        service.close()
    return 0


def cmd_apply_revision(args) -> int:
    revision = _load_json_arg(args.revision)
    service = _open_service(args.db)
    try:
        version = service.apply_revision(revision)
        _dump({"applied_revision": revision.get("revision"), "catalog_version": version})
    except CatalogError as exc:
        _dump({"error": "catalog", "message": str(exc)})
        return 5
    finally:
        service.close()
    return 0


def cmd_retry(args) -> int:
    service = _open_service(args.db)
    try:
        results = service.retry(args.parcel_ids, original=args.original)
        _dump([r.as_dict() for r in results])
    finally:
        service.close()
    return 0


def cmd_replay(args) -> int:
    service = _open_service(args.db)
    try:
        result = service.replay_submitted(args.parcel_id)
        if result is None:
            _dump({"error": "no_committed_job", "parcel_id": args.parcel_id})
            return 4
        _dump(result.as_dict())
    finally:
        service.close()
    return 0


def cmd_submit(args) -> int:
    service = _open_service(args.db)
    try:
        result = service.submit_new(
            args.parcel_id, args.city_id, args.district, args.street, args.house_no
        )
        _dump(result.as_dict())
    finally:
        service.close()
    return 0


def cmd_catalog(args) -> int:
    # Catalog-only access proves independence from the delivery store.
    conn = connect(args.db)
    try:
        catalog = Catalog(conn)
        if args.what == "snapshot":
            _dump(catalog.snapshot())
        elif args.what == "street":
            _dump(catalog.get_street(args.id))
        elif args.what == "candidates":
            _dump(catalog.lookup_street_by_name(args.id, args.district, args.street))
    finally:
        conn.close()
    return 0


def _reader_arg(value: str) -> Reader:
    return reader_from_json(_load_json_arg(value))


def cmd_reader_calibrate(args) -> int:
    reader = _reader_arg(args.reader)
    _dump(reader.credential())
    return 0


def cmd_reader_confusions(args) -> int:
    reader = _reader_arg(args.reader)
    _dump(reader.indistinguishable_pairs(args.paper))
    return 0


def _plan_brief_from_document(
    document: dict, service: AddressService
) -> tuple[list[PlanJob], list[Recipient], Catalog]:
    """Build jobs, recipients and the designated edition from a batch brief.

    A brief may either list stable rows explicitly, or point at the committed
    jobs already in the database via ``"from_committed": true``.  Every
    ``readers`` entry is a receiving counter bound to this catalog; the
    ``designated_reader_id`` selects whose registered names may be printed.
    """
    from .catalog import Catalog as _Catalog
    from .db import connect
    from .labels import Recipient

    readers = {raw["reader_id"]: reader_from_json(raw) for raw in document.get("readers", [])}

    # Each counter has its own frozen catalog.  The brief may give a
    # ``catalog_db`` per reader; otherwise everyone shares this service's
    # catalog.
    recipients: list[Recipient] = []
    extra_conns = []
    for raw in document.get("readers", []):
        reader = readers[raw["reader_id"]]
        if raw.get("catalog_db"):
            conn = connect(raw["catalog_db"])
            extra_conns.append(conn)
            recipients.append(Recipient(reader, _Catalog(conn)))
        else:
            recipients.append(Recipient(reader, service.catalog))

    designated_id = document.get("designated_reader_id")
    if designated_id:
        designated = next(r.catalog for r in recipients if r.reader.reader_id == designated_id)
    else:
        designated = recipients[0].catalog if recipients else service.catalog

    rows = document.get("rows")
    if rows is None and document.get("from_committed"):
        rows = service.delivery.stable_addresses()
    rows = rows or []
    jobs = [
        PlanJob(
            address=StableAddress(
                parcel_id=row["parcel_id"],
                city_id=row["city_id"],
                district_id=row["district_id"],
                street_id=row["street_id"],
                house_no=int(row["house_no"]),
            ),
            max_rows=int(row.get("max_rows", document.get("max_rows", 20))),
            max_cells=int(row.get("max_cells", document.get("max_cells", 40))),
            separator=row.get("separator", "/"),
        )
        for row in rows
    ]
    return jobs, recipients, designated


def cmd_plan(args) -> int:
    service = _open_service(args.db)
    try:
        document = _load_json_arg(args.brief)
        jobs, recipients, designated = _plan_brief_from_document(document, service)
        if args.borrow_for is not None:
            solution, imps = plan_min_impressions(
                jobs, designated, recipients, target_cells=args.borrow_for
            )
            if solution is None:
                _dump({"error": "infeasible", "target_cells": args.borrow_for})
                return 6
            payload = solution.as_dict(recipients)
            payload["impressions_needed"] = imps
            payload["already_owned"] = document.get("owned_impressions", 0)
            payload["borrowed"] = max(0, imps - int(document.get("owned_impressions", 0)))
            _dump(payload)
            return 0
        solution = plan_deck(
            jobs, designated, recipients, max_impressions=args.max_impressions
        )
        if solution is None:
            _dump({"error": "infeasible", "max_impressions": args.max_impressions})
            return 6
        _dump(solution.as_dict(recipients))
    finally:
        service.close()
    return 0


def cmd_diagnose(args) -> int:
    reader = _reader_arg(args.reader)
    service = _open_service(args.db)
    try:
        _dump(
            diagnose(
                reader,
                service.catalog,
                args.city_id,
                args.district,
                args.street,
                args.house_no,
            )
        )
    finally:
        service.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="address_delivery")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="initialise db with r1 catalog + coverage")
    p.add_argument("--materials", default="materials/707")
    p.add_argument("--db", required=True)
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("import", help="import and attempt a sender CSV")
    p.add_argument("--db", required=True)
    p.add_argument("csv")
    p.set_defaults(func=cmd_import)

    p = sub.add_parser("ingest", help="ingest address-input/v1 JSON (- for stdin)")
    p.add_argument("--db", required=True)
    p.add_argument("input")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("resolve", help="resolve one address to candidates/coverage")
    p.add_argument("--db", required=True)
    p.add_argument("city_id")
    p.add_argument("district")
    p.add_argument("street")
    p.add_argument("house_no", type=int)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("explain", help="audit one parcel")
    p.add_argument("--db", required=True)
    p.add_argument("parcel_id")
    p.set_defaults(func=cmd_explain)

    p = sub.add_parser("job", help="final committed destination")
    p.add_argument("--db", required=True)
    p.add_argument("parcel_id")
    p.set_defaults(func=cmd_job)

    p = sub.add_parser("apply-revision", help="apply a catalog revision JSON")
    p.add_argument("--db", required=True)
    p.add_argument("revision")
    p.set_defaults(func=cmd_apply_revision)

    p = sub.add_parser("retry", help="retry failed parcels against current catalog")
    p.add_argument("--db", required=True)
    p.add_argument("--original", choices=["original", "last_attempt"], default="original")
    p.add_argument("parcel_ids", nargs="+")
    p.set_defaults(func=cmd_retry)

    p = sub.add_parser("replay", help="replay a committed parcel")
    p.add_argument("--db", required=True)
    p.add_argument("parcel_id")
    p.set_defaults(func=cmd_replay)

    p = sub.add_parser("submit", help="submit a new parcel directly")
    p.add_argument("--db", required=True)
    p.add_argument("parcel_id")
    p.add_argument("city_id")
    p.add_argument("district")
    p.add_argument("street")
    p.add_argument("house_no", type=int)
    p.set_defaults(func=cmd_submit)

    p = sub.add_parser("catalog", help="catalog-only queries")
    p.add_argument("--db", required=True)
    p.add_argument("what", choices=["snapshot", "street", "candidates"])
    p.add_argument("--id")
    p.add_argument("--district")
    p.add_argument("--street")
    p.set_defaults(func=cmd_catalog)

    p = sub.add_parser("reader-calibrate", help="issue a reader's reading credential")
    p.add_argument("--reader", required=True, help="reader JSON (- for stdin)")
    p.set_defaults(func=cmd_reader_calibrate)

    p = sub.add_parser("reader-confusions", help="readings still indistinguishable")
    p.add_argument("--reader", required=True, help="reader JSON (- for stdin)")
    p.add_argument("paper")
    p.set_defaults(func=cmd_reader_confusions)

    p = sub.add_parser("diagnose", help="decode a raw paper header+street for a reader")
    p.add_argument("--db", required=True)
    p.add_argument("--reader", required=True, help="reader JSON (- for stdin)")
    p.add_argument("city_id")
    p.add_argument("district")
    p.add_argument("street")
    p.add_argument("house_no", type=int)
    p.set_defaults(func=cmd_diagnose)

    p = sub.add_parser("plan", help="plan pages/layouts for a batch brief")
    p.add_argument("--db", required=True)
    p.add_argument("brief", help="batch brief JSON (- for stdin)")
    p.add_argument("--max-impressions", type=int, default=None)
    p.add_argument("--borrow-for", type=int, default=None,
                   help="fewest borrowed impressions for this many paid cells")
    p.set_defaults(func=cmd_plan)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
