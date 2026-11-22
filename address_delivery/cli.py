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
from .service import AddressService
from .subscription import (
    Scheduler,
    SplitEntry,
    SubscriptionLedger,
)
from .subscription.errors import SubscriptionError
from .subscription.rationals import frac_from_text


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


# ---------------------------------------------------------------------------
# Subscription ledger + scheduling commands
# ---------------------------------------------------------------------------


def _open_ledger(service: AddressService) -> SubscriptionLedger:
    return SubscriptionLedger(service.conn, service.delivery)


def cmd_register_box(args) -> int:
    service = _open_service(args.db)
    try:
        ledger = _open_ledger(service)
        registration = ledger.register_box(
            args.box_id, args.street_id, args.house_no, args.label,
            args.version, note=args.note or "",
        )
        _dump(registration.as_dict())
    except SubscriptionError as exc:
        _dump({"error": "subscription", "message": str(exc)})
        return 6
    finally:
        service.close()
    return 0


def cmd_anchor(args) -> int:
    service = _open_service(args.db)
    try:
        ledger = _open_ledger(service)
        anchor = ledger.anchor_root(
            args.root_id, args.household, args.parcel, args.box,
            initial_share=frac_from_text(args.share), note=args.note or "",
        )
        _dump(anchor.as_dict())
    except SubscriptionError as exc:
        _dump({"error": "subscription", "message": str(exc)})
        return 6
    finally:
        service.close()
    return 0


def cmd_split(args) -> int:
    service = _open_service(args.db)
    try:
        ledger = _open_ledger(service)
        document = _load_json_arg(args.split_doc)
        entries = [
            SplitEntry(
                old_household=item["old_household"],
                new_household=item["new_household"],
                ratio=frac_from_text(item["ratio"]),
                box_id=item["box_id"],
            )
            for item in document["entries"]
        ]
        batch = ledger.apply_split_batch(
            document["batch_id"], int(document["effective_period"]), entries,
            note=document.get("note", ""),
        )
        _dump(batch.as_dict())
    except (SubscriptionError, KeyError, ValueError) as exc:
        _dump({"error": "subscription", "message": str(exc)})
        return 6
    finally:
        service.close()
    return 0


def cmd_entitlements(args) -> int:
    service = _open_service(args.db)
    try:
        ledger = _open_ledger(service)
        periods = list(range(1, args.periods + 1))
        _dump({
            "households": ledger.household_universe(periods),
            "allocations": [
                a.as_dict() for a in ledger.entitlement_table(periods)
            ],
        })
    except SubscriptionError as exc:
        _dump({"error": "subscription", "message": str(exc)})
        return 6
    finally:
        service.close()
    return 0


def cmd_schedule(args) -> int:
    service = _open_service(args.db)
    try:
        ledger = _open_ledger(service)
        periods = list(range(1, args.periods + 1))
        for closure in args.close_box or []:
            period_text, box_id = closure.split(":", 1)
            ledger.register_closure(int(period_text), box_id)
        plan = Scheduler(ledger).schedule(periods)
        _dump(plan.as_dict())
    except (SubscriptionError, ValueError) as exc:
        _dump({"error": "subscription", "message": str(exc)})
        return 6
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

    # -- subscription ledger / scheduling -----------------------------------
    p = sub.add_parser("register-box", help="register a named mailbox from a catalog version")
    p.add_argument("--db", required=True)
    p.add_argument("box_id")
    p.add_argument("street_id")
    p.add_argument("house_no", type=int)
    p.add_argument("label")
    p.add_argument("version", type=int)
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_register_box)

    p = sub.add_parser("anchor", help="anchor a root subscription on a successful parcel")
    p.add_argument("--db", required=True)
    p.add_argument("root_id")
    p.add_argument("household")
    p.add_argument("parcel")
    p.add_argument("box")
    p.add_argument("--share", default="1")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_anchor)

    p = sub.add_parser("split", help="apply one split batch from a JSON document")
    p.add_argument("--db", required=True)
    p.add_argument("split_doc")
    p.set_defaults(func=cmd_split)

    p = sub.add_parser("entitlements", help="exact per-period entitlements and root flow")
    p.add_argument("--db", required=True)
    p.add_argument("periods", type=int)
    p.set_defaults(func=cmd_entitlements)

    p = sub.add_parser("schedule", help="optimal per-issue dispatch plan")
    p.add_argument("--db", required=True)
    p.add_argument("periods", type=int)
    p.add_argument("--close-box", action="append", help="period:box_id closure, repeatable")
    p.set_defaults(func=cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
