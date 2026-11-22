"""票面/询证全流程 CLI 端到端演示（从实际编译产物重开票版）。

复现题面最后一个例：A=B=C=t、D=u、E=t+u，纸票 (4,4,7)、合计取 C、容错 2。
演示：
  * 票版从冻结快照的编译产物发行，独立验证健全/完备；
  * 辨错给出可能合计 4/7，必须再核；
  * 经费 3、最多两馆时 J（总台，费 3）与 D+E（两馆，费 2）都能签，只问 E 不行；
  * 带原单号的回签三重核对，D=3,E=7 记 4、J=21 记 7；
  * 2 元且一馆签不了，给同答不同款的两张有效票；
  * 重开数据目录后订单与待回签状态原样保留。

运行：python -m tests.demo_ticket_cli
"""

import json
import shutil
import subprocess
import sys
import tempfile


def run_cli(data_root, *args, stdin_payload=None, expect_fail=False):
    cmd = [sys.executable, "-m", "budget_solver.cli", "--data-root", data_root, *args]
    payload = json.dumps(stdin_payload, ensure_ascii=False) if stdin_payload is not None else None
    proc = subprocess.run(
        cmd, input=payload, capture_output=True, text=True, check=False)
    if expect_fail:
        assert proc.returncode != 0, f"本应失败却成功：{args}"
        return json.loads(proc.stderr)
    assert proc.returncode == 0, f"命令失败 {args}\nSTDERR={proc.stderr}"
    return json.loads(proc.stdout)


MODEL = {
    "model_id": "M_TICKET",
    "name": "印务票版模型",
    "cells": {
        "t": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},
        "u": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "u"}]},
        "A": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},
        "B": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},
        "C": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},
        "D": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "u"}]},
        "E": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"},
                        {"coef": "1", "cell_id": "u"}]},
    },
    "outputs": ["A", "B", "C"],
}
FACES = [f"M_TICKET::{x}" for x in "ABCDE"]
PAPER = {"M_TICKET::A": "4", "M_TICKET::B": "4", "M_TICKET::C": "7"}
TOTAL = {"constant": "0", "terms": {"M_TICKET::C": "1"}}
CATALOG = [
    {"item_id": "Q1", "name": "总台三栏和J", "branch": "总台", "fee": "3",
     "form": {"constant": "0",
              "terms": {"M_TICKET::A": "1", "M_TICKET::B": "1",
                        "M_TICKET::C": "1"}}},
    {"item_id": "Q2", "name": "核D栏", "branch": "甲分馆", "fee": "1",
     "form": {"constant": "0", "terms": {"M_TICKET::D": "1"}}},
    {"item_id": "Q3", "name": "核E栏", "branch": "乙分馆", "fee": "1",
     "form": {"constant": "0", "terms": {"M_TICKET::E": "1"}}},
]


def main():
    data_root = tempfile.mkdtemp(prefix="ticket-cli-demo-")
    try:
        # 1. 模型 → 冻结快照 → 编译产物 → 从产物发行票版
        run_cli(data_root, "put-model", "-", stdin_payload=MODEL)
        snap = run_cli(data_root, "snapshot", "M_TICKET", "--label", "票面冻结版")
        compiled = run_cli(data_root, "compile", snap["snap_id"])
        ticket = run_cli(
            data_root, "issue-ticket", compiled["artifact_hash"], "TK-DEMO",
            *[x for face in FACES for x in ("--face", face)], "--label", "五栏票版")
        print("1. 票版 TK-DEMO 从编译产物", compiled["artifact_hash"][:19],
              "发行；票面等式", len(ticket["face_equations"]), "条：")
        for eq in ticket["face_equations"]:
            print("   ", eq["coeffs"], "=", eq["rhs"])
        vt = run_cli(data_root, "verify-ticket", "TK-DEMO")
        print("   独立验证：", vt["check_count"], "项检查通过（健全且完备）")

        # 2. 校版员独查：相容拿内页补写，矛盾拿 λ 依据
        ok = run_cli(data_root, "check-face", "TK-DEMO", "-",
                     stdin_payload={"M_TICKET::A": "4", "M_TICKET::B": "4",
                                    "M_TICKET::C": "4", "M_TICKET::D": "3",
                                    "M_TICKET::E": "7"})
        print("2. 校版相容：内页补写 t =",
              ok["inner_completion"]["point"]["M_TICKET::t"],
              "u =", ok["inner_completion"]["point"]["M_TICKET::u"])
        bad = run_cli(data_root, "check-face", "TK-DEMO", "-",
                      stdin_payload={"M_TICKET::A": "4", "M_TICKET::B": "5"})
        print("   校版冲突：", bad["status"], "残差", bad["residual_rhs"],
              "（λ 组合冲突依据", len(bad["certificate"]), "条）")

        # 3. 纸票辨错：容错 2，两个复原、合计 4 或 7
        analysis = run_cli(
            data_root, "analyze-paper", "TK-DEMO", "-",
            stdin_payload={"paper": PAPER, "total_form": TOTAL, "k": 2})
        print("3. 辨错：", analysis["status"],
              "可能合计", analysis["possible_total_values"],
              "可能错栏", [v.split("::")[1] for v in analysis["possibly_error_filled"]])

        # 4. 套餐规划：经费 3 / 两馆
        plan_payload = {"paper": PAPER, "total_form": TOTAL, "k": 2,
                        "catalog": CATALOG, "budget": "3", "max_branches": 2}
        planning = run_cli(data_root, "plan-inquiries", "TK-DEMO", "-",
                           stdin_payload=plan_payload)["planning"]
        print("4. 可签套餐（按少馆→少费排序）：")
        for p in planning["plans"]:
            print("   ", p["item_ids"], "馆数", p["branch_count"],
                  "费用", p["total_fee"])

        # 5. 订单一：D+E 两馆，回签带原单号，D=3,E=7 记 4
        order1 = run_cli(data_root, "open-order", "TK-DEMO", "-",
                         stdin_payload=plan_payload)
        run_cli(data_root, "dispatch-inquiries", order1["order_id"], "-",
                stdin_payload={"item_ids": ["Q2", "Q3"]})
        run_cli(data_root, "receive-reply", order1["order_id"], "-",
                stdin_payload={"ticket_id": "TK-DEMO",
                               "issue_hash": ticket["issue_hash"],
                               "item_id": "Q2", "value": "3"})
        run_cli(data_root, "receive-reply", order1["order_id"], "-",
                stdin_payload={"ticket_id": "TK-DEMO",
                               "issue_hash": ticket["issue_hash"],
                               "item_id": "Q3", "value": "7"})
        booked1 = run_cli(data_root, "get-order", order1["order_id"])
        vb1 = run_cli(data_root, "verify-booking", order1["order_id"])
        print("5. 订单", booked1["order_id"], "D=3,E=7 → 记账",
              booked1["booking"]["booked_total"],
              "；回执", vb1["check_count"], "项独立核对通过")

        # 6. 订单二：总台 J，J=21 记 7（D/E 自由栏永远未定也能签）
        order2 = run_cli(data_root, "open-order", "TK-DEMO", "-",
                         stdin_payload=plan_payload)
        run_cli(data_root, "dispatch-inquiries", order2["order_id"], "-",
                stdin_payload={"item_ids": ["Q1"]})
        run_cli(data_root, "receive-reply", order2["order_id"], "-",
                stdin_payload={"ticket_id": "TK-DEMO",
                               "issue_hash": ticket["issue_hash"],
                               "item_id": "Q1", "value": "21"})
        booked2 = run_cli(data_root, "get-order", order2["order_id"])
        print("6. 订单", booked2["order_id"], "J=21 → 记账",
              booked2["booking"]["booked_total"],
              "；复原", booked2["booking"]["face_assignment"])

        # 7. 2 元且一馆：签不了，给同答不同款反例
        blocked_payload = dict(plan_payload, budget="2", max_branches=1)
        blocked = run_cli(data_root, "plan-inquiries", "TK-DEMO", "-",
                          stdin_payload=blocked_payload)["planning"]
        print("7. 经费 2/一馆：", blocked["status"])
        for ce in blocked["counterexamples"][:2]:
            print("    反例问项", ce["answers"], "两张票合计",
                  ce["total_a"], "vs", ce["total_b"])

        # 8. 重开：待回签订单与票版原样保留
        order3 = run_cli(data_root, "open-order", "TK-DEMO", "-",
                         stdin_payload=plan_payload)
        run_cli(data_root, "dispatch-inquiries", order3["order_id"], "-",
                stdin_payload={"item_ids": ["Q1"]})
        reopened_orders = run_cli(data_root, "list-orders")
        reopened_tickets = run_cli(data_root, "list-tickets")
        pending = run_cli(data_root, "get-order", order3["order_id"])
        print("8. 重开目录：票版", reopened_tickets, "订单", reopened_orders,
              "；未回齐订单状态", pending["status"])

        print("\n票面/询证 CLI 端到端演示全部通过。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()
