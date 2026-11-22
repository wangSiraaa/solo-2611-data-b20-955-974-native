"""代拨用途变更 CLI：场次 → 去向册 → 点名册 → 账簿/拨款单/验证。

与既有 ``budget_solver.cli`` 并列、互不影响；旧预算公式与已发报告继续可读。
"""

import argparse
import json
import sys

from ..errors import BudgetError
from .service import AllocationService


def _read_payload(arg):
    if arg == "-":
        return json.load(sys.stdin)
    with open(arg, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _summary_ledger(ledger):
    return {
        "ledger_id": ledger.get("ledger_id"),
        "session_id": ledger["session_id"],
        "settled": ledger["settled"],
        "trap_components": ledger["trap_components"],
        "trapped_sources": [
            {"funder": t["funder"], "trapped_accounts": t["trapped_accounts"]}
            for t in ledger["trapped_sources"]
        ],
        "zero_retention_accounts": ledger["zero_retention_accounts"],
        "retained_by_source": ledger["retained_by_source"],
        "total_retained": ledger["total"]["retained"],
        "conservation": ledger["conservation"],
        "ledger_fingerprint": ledger["ledger_fingerprint"],
    }


def _summary_rollcall(rollcall):
    return {
        "roll_id": rollcall.get("roll_id"),
        "session_id": rollcall["session_id"],
        "agenda_order": rollcall["agenda_order"],
        "approved_cases": rollcall["approved_cases"],
        "rejected_cases": rollcall["rejected_cases"],
        "cases": [
            {
                "case_id": c["case_id"],
                "account": c["account"],
                "approved": c["approved"],
                "total_retained": c["total_retained"],
                "yes_amount": c["yes_amount"],
                "no_amount": c["no_amount"],
                "yes_share": c["yes_share"],
                "threshold": c["threshold"],
                "records": [
                    {k: r[k] for k in ("funder", "mode", "signer", "vote",
                                       "interest", "signed", "abstained", "reason")
                     if k in r}
                    for r in c["records"]
                ],
            }
            for c in rollcall["cases"]
        ],
        "credential_audit": rollcall["credential_audit"],
    }


def build_parser():
    parser = argparse.ArgumentParser(
        prog="allocation", description="代拨去向册与用途变更拨款服务（精确有理数）")
    parser.add_argument("--data-root", default="./budget-data", help="持久化数据目录（可重开）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("register-session", help="登记/校验一场代拨事实")
    p.add_argument("payload")

    sub.add_parser("list-sessions")

    p = sub.add_parser("build-ledger", help="编译等式核并建代拨去向册")
    p.add_argument("session_id")
    sub.add_parser("list-ledgers")
    p = sub.add_parser("get-ledger")
    p.add_argument("ledger_id")
    p = sub.add_parser("verify-ledger")
    p.add_argument("ledger_id")

    p = sub.add_parser("build-rollcall", help="按议程点名、委托证扣额、核门槛")
    p.add_argument("ledger_id")
    sub.add_parser("list-rollcalls")
    p = sub.add_parser("get-rollcall")
    p.add_argument("roll_id")
    p = sub.add_parser("verify-rollcall")
    p.add_argument("ledger_id")
    p.add_argument("roll_id")

    p = sub.add_parser("open-book", help="从同一开场账建拨款账簿")
    p.add_argument("ledger_id")
    p.add_argument("roll_id")
    p.add_argument("--label")
    sub.add_parser("list-books")
    p = sub.add_parser("get-book")
    p.add_argument("book_id")
    p = sub.add_parser("book-state", help="重开核余额：原资助/新用途/自有款余数")
    p.add_argument("book_id")
    p = sub.add_parser("check-feasible", help="不占款测算能否同时足额兑现")
    p.add_argument("book_id")
    p = sub.add_parser("issue-order", help="下正式拨款单（--dry-run 只测算不占款）")
    p.add_argument("book_id")
    p.add_argument("payload", help="拨款明细 JSON 文件，- 表示 stdin")
    p.add_argument("--order-id", help="显式稳定单号（默认顺序发号）")
    p.add_argument("--dry-run", action="store_true")
    sub.add_parser("list-orders")
    p = sub.add_parser("get-order")
    p.add_argument("order_id")
    p = sub.add_parser("verify-book")
    p.add_argument("book_id")
    p = sub.add_parser("close-book", help="批签收口：获批案必须全部足额到款")
    p.add_argument("book_id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    service = AllocationService(args.data_root)
    try:
        cmd = args.command
        if cmd == "register-session":
            session = service.register_session(_read_payload(args.payload))
            _emit({"session_id": session["session_id"],
                   "accounts": session["accounts"], "funders": session["funders"],
                   "agenda": [c["case_id"] for c in session["agenda"]]})
        elif cmd == "list-sessions":
            _emit(service.list_sessions())
        elif cmd == "build-ledger":
            _emit(_summary_ledger(service.build_ledger(args.session_id)))
        elif cmd == "list-ledgers":
            _emit(service.list_ledgers())
        elif cmd == "get-ledger":
            _emit(_summary_ledger(service.get_ledger(args.ledger_id)))
        elif cmd == "verify-ledger":
            _emit(service.verify_ledger(args.ledger_id))
        elif cmd == "build-rollcall":
            _emit(_summary_rollcall(service.build_rollcall(args.ledger_id)))
        elif cmd == "list-rollcalls":
            _emit(service.list_rollcalls())
        elif cmd == "get-rollcall":
            _emit(_summary_rollcall(service.get_rollcall(args.roll_id)))
        elif cmd == "verify-rollcall":
            _emit(service.verify_rollcall(args.ledger_id, args.roll_id))
        elif cmd == "open-book":
            book = service.open_book(args.ledger_id, args.roll_id, label=args.label)
            _emit({"book_id": book["book_id"], "label": book["label"],
                   "opening_original": book["opening_original"],
                   "opening_own_funds": book["opening_own_funds"]})
        elif cmd == "list-books":
            _emit(service.list_books())
        elif cmd == "get-book":
            _emit(service.get_book(args.book_id))
        elif cmd == "book-state":
            _emit(service.book_state(args.book_id))
        elif cmd == "check-feasible":
            _emit(service.check_feasible(args.book_id))
        elif cmd == "issue-order":
            payload = _read_payload(args.payload)
            _emit(service.issue_order(
                args.book_id, payload["items"],
                order_id=args.order_id, dry_run=args.dry_run))
        elif cmd == "list-orders":
            _emit(service.list_orders())
        elif cmd == "get-order":
            _emit(service.get_order(args.order_id))
        elif cmd == "verify-book":
            _emit(service.verify_book(args.book_id))
        elif cmd == "close-book":
            _emit(service.close_book(args.book_id))
        else:  # pragma: no cover
            raise SystemExit(2)
    except BudgetError as exc:
        payload = {"error": type(exc).__name__, "message": str(exc)}
        if getattr(exc, "errors", None):
            payload["details"] = exc.errors
        json.dump(payload, sys.stderr, ensure_ascii=False, indent=2)
        sys.stderr.write("\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
