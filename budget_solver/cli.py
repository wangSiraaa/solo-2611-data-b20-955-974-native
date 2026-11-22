"""命令行入口：put-model / snapshot / compile / solve / query / explain / verify。

JSON 输入从文件或 stdin 读取，结果以 JSON 写到 stdout；业务错误写到 stderr 并
返回非零退出码，便于脚本化与合成案例复跑。
"""

import argparse
import json
import sys

from .errors import BudgetError
from .service import BudgetService


def _read_payload(args):
    if args.payload == "-":
        return json.load(sys.stdin)
    with open(args.payload, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _emit(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def build_parser():
    parser = argparse.ArgumentParser(prog="budget", description="精确预算循环引用解算服务")
    parser.add_argument("--data-root", default="./budget-data", help="持久化数据目录（可重开）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("put-model", help="创建或修订模型（非法引用整次拒绝）")
    p.add_argument("payload", help="模型 JSON 文件，- 表示 stdin")

    p = sub.add_parser("snapshot", help="为模型 HEAD 修订创建固定快照")
    p.add_argument("model_id")
    p.add_argument("--label")

    p = sub.add_parser("list-models", help="独立查询目录版本")
    p = sub.add_parser("list-revisions")
    p.add_argument("model_id")
    p = sub.add_parser("list-snapshots")
    p.add_argument("model_id")
    p = sub.add_parser("get-snapshot")
    p.add_argument("snap_id")

    p = sub.add_parser("compile", help="按快照编译引用/方程/SCC 产物")
    p.add_argument("snap_id")
    p.add_argument("--label")

    p = sub.add_parser("solve", help="消费编译产物，按输出闭包精确求解并发布报告")
    p.add_argument("artifact", help="编译产物路径或内容哈希")
    p.add_argument("--no-cache", action="store_true")

    p = sub.add_parser("query", help="查询输出格的值与实际依赖")
    p.add_argument("solve_id")
    p.add_argument("variable")

    p = sub.add_parser("explain", help="输出格的方程、SCC、参数或矛盾来源")
    p.add_argument("solve_id")
    p.add_argument("variable")

    p = sub.add_parser("verify-compile", help="独立从快照重建并核对编译产物")
    p.add_argument("artifact", help="编译产物路径或内容哈希")

    p = sub.add_parser("verify-solve", help="独立代回/证书核对解算报告")
    p.add_argument("solve_id")

    p = sub.add_parser("list-reports")
    p = sub.add_parser("get-report")
    p.add_argument("solve_id")

    # ------------------------------------------------------- 票面发行/校版
    p = sub.add_parser("issue-ticket", help="从编译产物发行票面仿射校验关系")
    p.add_argument("artifact", help="编译产物路径或内容哈希")
    p.add_argument("ticket_id")
    p.add_argument("--face", action="append", required=True,
                   help="票面永久格变量（model::cell），可重复，至多 6 个")
    p.add_argument("--label")

    p = sub.add_parser("list-tickets")
    p = sub.add_parser("get-ticket")
    p.add_argument("ticket_id")

    p = sub.add_parser("check-face", help="联合核查一组票面值并拿内页补写/冲突依据")
    p.add_argument("ticket_id")
    p.add_argument("payload", help="JSON：{票面变量: 有理值}，- 表示 stdin")

    p = sub.add_parser("verify-ticket")
    p.add_argument("ticket_id")

    # ------------------------------------------------------- 收单/询证/订单
    p = sub.add_parser("analyze-paper", help="纸票容错辨错与合计固定性判断")
    p.add_argument("ticket_id")
    p.add_argument("payload", help="JSON：{paper, total_form, k}，- 表示 stdin")

    p = sub.add_parser("plan-inquiries", help="在经费/分馆上限内规划询证套餐")
    p.add_argument("ticket_id")
    p.add_argument("payload",
                   help="JSON：{paper, total_form, k, catalog, budget, max_branches}")

    p = sub.add_parser("open-order")
    p.add_argument("ticket_id")
    p.add_argument("payload",
                   help="JSON：{paper, total_form, k, catalog, budget, max_branches}")

    p = sub.add_parser("dispatch-inquiries", help="一次确定套餐并发单（只计费一次）")
    p.add_argument("order_id")
    p.add_argument("payload", help="JSON：{item_ids: [...]}，- 表示 stdin")

    p = sub.add_parser("receive-reply", help="收回签（票号/发行版/问项号三重核对）")
    p.add_argument("order_id")
    p.add_argument("payload",
                   help="JSON：{ticket_id, issue_hash, item_id, value}，- 表示 stdin")

    p = sub.add_parser("list-orders")
    p = sub.add_parser("get-order")
    p.add_argument("order_id")
    p = sub.add_parser("verify-booking")
    p.add_argument("order_id")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    service = BudgetService(args.data_root)
    try:
        cmd = args.command
        if cmd == "put-model":
            _emit(service.put_model(_read_payload(args)))
        elif cmd == "snapshot":
            _emit(
                {
                    "snap_id": (snap := service.snapshot(args.model_id, args.label))[
                        "snap_id"
                    ],
                    "model_id": snap["model_id"],
                    "rev_id": snap["rev_id"],
                    "external_refs": snap["external_refs"],
                    "content_hash": snap["content_hash"],
                }
            )
        elif cmd == "list-models":
            _emit(service.list_models())
        elif cmd == "list-revisions":
            _emit(
                [
                    {
                        "rev_id": r["rev_id"],
                        "revision_number": r["revision_number"],
                        "name": r["name"],
                        "outputs": r["outputs"],
                        "content_hash": r["content_hash"],
                    }
                    for r in service.list_revisions(args.model_id)
                ]
            )
        elif cmd == "list-snapshots":
            _emit(
                [
                    {
                        "snap_id": s["snap_id"],
                        "rev_id": s["rev_id"],
                        "name": s["name"],
                        "label": s.get("label"),
                    }
                    for s in service.list_snapshots(args.model_id)
                ]
            )
        elif cmd == "get-snapshot":
            _emit(service.get_snapshot(args.snap_id))
        elif cmd == "compile":
            artifact, path = service.compile(args.snap_id, args.label)
            _emit(
                {
                    "artifact_path": path,
                    "artifact_hash": artifact["artifact_hash"],
                    "root_model_id": artifact["root_model_id"],
                    "scope_snapshots": artifact["scope_snapshots"],
                    "variable_count": len(artifact["variables"]),
                    "equation_count": len(artifact["equations"]),
                    "components": [
                        {
                            "scc_id": c["scc_id"],
                            "cyclic": c["cyclic"],
                            "size": len(c["variables"]),
                            "variables": c["variables"],
                        }
                        for c in artifact["components"]
                    ],
                    "root_outputs": artifact["root_outputs"],
                }
            )
        elif cmd == "solve":
            artifact = service.load_artifact(args.artifact)
            config = None
            if args.no_cache:
                config = {"mode": "closure_rref_exact", "use_cache": False}
            report = service.solve(artifact, config)
            _emit(_report_summary(report))
        elif cmd == "query":
            _emit(service.query(args.solve_id, args.variable))
        elif cmd == "explain":
            _emit(service.explain(args.solve_id, args.variable))
        elif cmd == "verify-compile":
            artifact = service.load_artifact(args.artifact)
            _emit(service.verify_compile(artifact))
        elif cmd == "verify-solve":
            _emit(service.verify_solve(args.solve_id))
        elif cmd == "list-reports":
            _emit(service.list_reports())
        elif cmd == "get-report":
            _emit(service.get_report(args.solve_id))
        elif cmd == "issue-ticket":
            artifact = service.load_artifact(args.artifact)
            _emit(service.issue_ticket(
                artifact, args.face, args.ticket_id, label=args.label))
        elif cmd == "list-tickets":
            _emit(service.list_tickets())
        elif cmd == "get-ticket":
            _emit(service.get_ticket(args.ticket_id))
        elif cmd == "check-face":
            _emit(service.check_face(args.ticket_id, _read_payload(args)))
        elif cmd == "verify-ticket":
            _emit(service.verify_ticket(args.ticket_id))
        elif cmd == "analyze-paper":
            body = _read_payload(args)
            _emit(service.analyze_paper(
                args.ticket_id, body["paper"], body["total_form"], body["k"]))
        elif cmd == "plan-inquiries":
            body = _read_payload(args)
            _emit(service.plan_inquiries(
                args.ticket_id, body["paper"], body["total_form"], body["k"],
                body["catalog"], body["budget"], body["max_branches"]))
        elif cmd == "open-order":
            body = _read_payload(args)
            _emit(service.open_order(
                args.ticket_id, body["paper"], body["total_form"], body["k"],
                body["catalog"], body["budget"], body["max_branches"]))
        elif cmd == "dispatch-inquiries":
            body = _read_payload(args)
            _emit(service.dispatch_inquiries(args.order_id, body["item_ids"]))
        elif cmd == "receive-reply":
            body = _read_payload(args)
            _emit(service.receive_reply(
                args.order_id, body["ticket_id"], body["issue_hash"],
                body["item_id"], body["value"]))
        elif cmd == "list-orders":
            _emit(service.list_orders())
        elif cmd == "get-order":
            _emit(service.get_order(args.order_id))
        elif cmd == "verify-booking":
            _emit(service.verify_booking(args.order_id))
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


def _report_summary(report):
    return {
        "solve_id": report["solve_id"],
        "report_hash": report["report_hash"],
        "artifact_hash": report["artifact_hash"],
        "catalog_snapshot": report["catalog_snapshot"],
        "solve_config": report["solve_config"],
        "cache": report["cache"],
        "outputs": report["outputs"],
        "sections": report["sections"],
    }


if __name__ == "__main__":
    raise SystemExit(main())
