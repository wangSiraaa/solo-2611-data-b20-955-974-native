"""实况册/计量发行 CLI：在原有 budget 命令之外新增的子命令入口。

用法示例：
  python3 -m budget_solver.livebook.cli --data-root ./data lb-register book.json
  python3 -m budget_solver.livebook.cli --data-root ./data lb-lines LB_A lines.json
  python3 -m budget_solver.livebook.cli --data-root ./data lb-photos LB_A photos.json
  python3 -m budget_solver.livebook.cli --data-root ./data lb-issue ps1
  python3 -m budget_solver.livebook.cli --data-root ./data lb-certify is1 JIA 7/4 2
  python3 -m budget_solver.livebook.cli --data-root ./data lb-explain is1 JIA 7/4
  python3 -m budget_solver.livebook.cli --data-root ./data lb-recheck is1 JIA '[1,2,4]' 2
  python3 -m budget_solver.livebook.cli --data-root ./data lb-common ps1 JIA
"""

import argparse
import json
import sys

from ..cli import _emit
from ..errors import BudgetError
from ..service import BudgetService
from .service import LiveBookService


def _read_payload(args, name):
    path = getattr(args, name)
    if path == "-":
        return json.load(sys.stdin)
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="budget-livebook", description="单向换挡实况册与精确计量发行")
    parser.add_argument("--data-root", default="./budget-data")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("lb-register", help="登记一册单向换挡实况册")
    p.add_argument("payload")

    sub.add_parser("lb-list", help="列出实况册")

    p = sub.add_parser("lb-plates", help="枚举一册的全部可达盘面")
    p.add_argument("book_ref")

    p = sub.add_parser("lb-next", help="查询盘面可继续开动的下一组")
    p.add_argument("book_ref")
    p.add_argument("plate_id")

    p = sub.add_parser("lb-inspect", help="独查盘面：各针状态、理想示数、原方程")
    p.add_argument("book_ref")
    p.add_argument("plate_id")

    p = sub.add_parser("lb-needle", help="独查一根针的依赖闭包与原方程")
    p.add_argument("book_ref")
    p.add_argument("plate_id")
    p.add_argument("variable")

    p = sub.add_parser("lb-lines", help="登记板卡挡位线（至多三条）")
    p.add_argument("book_ref")
    p.add_argument("payload")

    p = sub.add_parser("lb-photos", help="登记一套实抄照片")
    p.add_argument("book_ref")
    p.add_argument("payload")

    p = sub.add_parser("lb-issue", help="发行各挡位线可解释的全部增益区段")
    p.add_argument("photoset_ref")

    p = sub.add_parser("lb-certify", help="铭牌段整段签发/退签（带精确拒签增益）")
    p.add_argument("issuance_ref")
    p.add_argument("line_id")
    p.add_argument("lo")
    p.add_argument("hi")

    p = sub.add_parser("lb-explain", help="为具体有理增益指出动作与拍摄位置链")
    p.add_argument("issuance_ref")
    p.add_argument("line_id")
    p.add_argument("gain")

    p = sub.add_parser("lb-recheck", help="驻点复检：最少现场重查点与动作单")
    p.add_argument("issuance_ref")
    p.add_argument("line_id")
    p.add_argument("gains_json", help='JSON 数组，如 "[1,2,4]"')
    p.add_argument("sheet_budget", type=int)

    p = sub.add_parser("lb-common", help="照片集能否来自同一次单向切换")
    p.add_argument("photoset_ref")
    p.add_argument("line_id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    service = LiveBookService(BudgetService(args.data_root))
    try:
        cmd = args.command
        if cmd == "lb-register":
            _emit(service.register_book(_read_payload(args, "payload")))
        elif cmd == "lb-list":
            _emit(service.list_books())
        elif cmd == "lb-plates":
            _emit(service.plates(args.book_ref))
        elif cmd == "lb-next":
            _emit({"movable_next": service.movable_next(args.book_ref, args.plate_id)})
        elif cmd == "lb-inspect":
            _emit(service.inspect_plate(args.book_ref, args.plate_id))
        elif cmd == "lb-needle":
            _emit(service.inspect_needle(args.book_ref, args.plate_id, args.variable))
        elif cmd == "lb-lines":
            _emit(service.register_lines(args.book_ref, _read_payload(args, "payload")["lines"]))
        elif cmd == "lb-photos":
            _emit(service.register_photoset(
                args.book_ref, _read_payload(args, "payload")))
        elif cmd == "lb-issue":
            saved = service.issue(args.photoset_ref)
            _emit({k: v for k, v in saved.items() if k != "_working"})
        elif cmd == "lb-certify":
            _emit(service.certify(args.issuance_ref, args.line_id, args.lo, args.hi))
        elif cmd == "lb-explain":
            _emit(service.explain_gain(args.issuance_ref, args.line_id, args.gain))
        elif cmd == "lb-recheck":
            gains = json.loads(args.gains_json)
            _emit(service.station_recheck(
                args.issuance_ref, args.line_id, gains, args.sheet_budget))
        elif cmd == "lb-common":
            _emit(service.common_run_check(args.photoset_ref, args.line_id))
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
