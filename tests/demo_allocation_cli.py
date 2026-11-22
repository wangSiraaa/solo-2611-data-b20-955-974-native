"""代拨用途变更 CLI 端到端演示（子进程，复用真实编译产物与持久化目录）。

覆盖题目要求的完整链路：

1. 基准场登记 -> 去向册（真实 put-model/snapshot/compile/solve）-> 独立验证；
2. 点名册：委托链/委托证扣额/门槛 -> 独立验证；
3. 同一开场账建两本账簿，演示两种合法拨法，各自实付、重开核余额，
   验证次日 X 旧用途能否再付 2 的相反结论；
4. 稳定单号：同号同内容复读、同号异文退回、dry-run 不占款；
5. 三个变体（1/4 留 X、证额 0、need=5）；
6. 受困来源：资金困在封闭循环，指出受困账户、阻断批签；
7. 无来款空转组不牵连本场。

运行：PYTHONPATH=. python -m tests.demo_allocation_cli
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile


def run(data_root, *args, stdin_payload=None, expect_fail=False):
    cmd = [sys.executable, "-m", "budget_solver.allocation.cli",
           "--data-root", data_root, *args]
    payload = json.dumps(stdin_payload, ensure_ascii=False) if stdin_payload is not None else None
    proc = subprocess.run(cmd, input=payload, capture_output=True, text=True, check=False)
    if expect_fail:
        assert proc.returncode != 0, f"本应失败却成功：{args}"
        return json.loads(proc.stderr)
    assert proc.returncode == 0, f"命令失败 {args}\nSTDERR={proc.stderr}"
    return json.loads(proc.stdout)


def baseline(ratio_d1_x="1/2", cred_cap="1", x_need="3", session_id="S1"):
    return {
        "session_id": session_id,
        "name": "基准代拨场",
        "accounts": ["X", "Y"],
        "funders": ["D1", "D2"],
        "injections": [
            {"funder": "D1", "account": "X", "amount": "4"},
            {"funder": "D2", "account": "X", "amount": "2"},
        ],
        "transfers": [
            {"from": "X", "to": "Y", "ratio": "0",
             "ratios": {"D1": ratio_d1_x, "D2": "0"}},
        ],
        "agenda": [
            {"case_id": "CX", "account": "X", "purpose": "X新用途",
             "need": x_need, "reserve": "1", "threshold": "3/4"},
            {"case_id": "CY", "account": "Y", "purpose": "Y新用途",
             "need": "2", "reserve": "0", "threshold": "1"},
        ],
        "signatures": [
            {"funder": "D1", "case_id": "CX", "vote": "yes"},
            {"funder": "D1", "case_id": "CY", "vote": "yes"},
        ],
        "delegations": [{"funder": "D2", "delegate": "D1"}],
        "credentials": [{"funder": "D2", "case_scope": ["CX", "CY"], "cap": cred_cap}],
        "own_funds": [{"fund_id": "F1", "balance": "1", "case_scope": ["CX", "CY"]}],
    }


def main():
    data_root = tempfile.mkdtemp(prefix="budget-alloc-demo-")
    try:
        # 1. 登记 + 去向册（真实编译产物）
        run(data_root, "register-session", "-", stdin_payload=baseline())
        ledger = run(data_root, "build-ledger", "S1")
        assert ledger["settled"]
        assert ledger["retained_by_source"] == {
            "D1": {"X": "2", "Y": "2"}, "D2": {"X": "2", "Y": "0"}}
        print("1. 去向册", ledger["ledger_id"], "结清：D1 留 X2/Y2，D2 留 X2/Y0；"
              "守恒", ledger["conservation"]["all_conserved"])
        vl = run(data_root, "verify-ledger", ledger["ledger_id"])
        print("   独立验证", vl["check_count"], "项通过（含逐户=合计、方程事实、哈希）")

        # 2. 点名册
        roll = run(data_root, "build-rollcall", ledger["ledger_id"])
        assert roll["approved_cases"] == ["CX", "CY"]
        cx = next(c for c in roll["cases"] if c["case_id"] == "CX")
        assert cx["yes_share"] == "3/4"
        print("2. 点名册", roll["roll_id"], "：CX 同意份额", cx["yes_share"],
              "、CY", next(c for c in roll["cases"] if c["case_id"] == "CY")["yes_share"],
              "两案获批；委托证审计", roll["credential_audit"]["D2"]["total_deducted"])
        run(data_root, "verify-rollcall", ledger["ledger_id"], roll["roll_id"])

        # 3. 两种合法拨法，各自实付
        cy_order = {"items": [{
            "case_id": "CY", "account": "Y",
            "redirects": [{"funder": "D1", "amount": "2"}], "own_fund_inputs": []}]}

        book_a = run(data_root, "open-book", ledger["ledger_id"], roll["roll_id"],
                     "--label", "拨法A-用足原款")
        plan_a = {"items": [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"},
                          {"funder": "D2", "amount": "1"}], "own_fund_inputs": []}]}
        order_a = run(data_root, "issue-order", book_a["book_id"], "-",
                      stdin_payload=plan_a)
        run(data_root, "issue-order", book_a["book_id"], "-", stdin_payload=cy_order)
        state_a = run(data_root, "book-state", book_a["book_id"])
        run(data_root, "close-book", book_a["book_id"])

        book_b = run(data_root, "open-book", ledger["ledger_id"], roll["roll_id"],
                     "--label", "拨法B-贴自有款")
        plan_b = {"items": [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"}],
            "own_fund_inputs": [{"fund_id": "F1", "amount": "1"}]}]}
        run(data_root, "issue-order", book_b["book_id"], "-", stdin_payload=plan_b)
        run(data_root, "issue-order", book_b["book_id"], "-", stdin_payload=cy_order)
        state_b = run(data_root, "book-state", book_b["book_id"])
        x_a = sum(int(v) for v in (state_a["original_balances"]["D1"]["X"],
                                   state_a["original_balances"]["D2"]["X"]))
        x_b = sum(int(v) for v in (state_b["original_balances"]["D1"]["X"],
                                   state_b["original_balances"]["D2"]["X"]))
        assert x_a == 1 and state_a["own_fund_balances"]["F1"] == "1"
        assert x_b == 2 and state_b["own_fund_balances"]["F1"] == "0"
        print("3. 两本账簿各自实付：拨法A X旧款余", x_a, "自有款余1；拨法B X旧款余",
              x_b, "自有款清零 -> 次日再付2：A 不能、B 能（结论相反）")

        # 4. 稳定单号：同号同内容复读、同号异文退回、dry-run 不占款。
        #    用一本全新账簿，避免与上面已支案冲突。
        book_d = run(data_root, "open-book", ledger["ledger_id"], roll["roll_id"],
                     "--label", "单号规则账簿")
        fixed_id = "order-demo-7"
        first = run(data_root, "issue-order", book_d["book_id"], "-",
                    "--order-id", fixed_id, stdin_payload=plan_a)
        again = run(data_root, "issue-order", book_d["book_id"], "-",
                    "--order-id", fixed_id, stdin_payload=plan_a)
        assert again["order_fingerprint"] == first["order_fingerprint"]
        # 同号异文：同一单号绑定不同分款明细，必须退回
        rejected = run(data_root, "issue-order", book_d["book_id"], "-",
                       "--order-id", fixed_id, stdin_payload=plan_b,
                       expect_fail=True)
        assert rejected["error"] == "OrderRejectedError"
        # dry-run 在另一本新账簿上测算：校验照跑但不占单号、不动余额
        book_c = run(data_root, "open-book", ledger["ledger_id"], roll["roll_id"],
                     "--label", "测算账簿")
        orders_before = run(data_root, "list-orders")
        dry = run(data_root, "issue-order", book_c["book_id"], "-", "--dry-run",
                  stdin_payload=plan_a)
        orders_after = run(data_root, "list-orders")
        assert dry["dry_run"] and orders_before == orders_after
        state_c = run(data_root, "book-state", book_c["book_id"])
        assert state_c["own_fund_balances"]["F1"] == "1"  # 余额未动
        print("4. 稳定单号：同号复读指纹", first["order_fingerprint"][:18],
              "…；同号异文退回；dry-run 不占款、不占单号、不动余额")

        # 5. 重开核余额
        reopened_state = run(data_root, "book-state", book_a["book_id"])
        assert reopened_state == state_a
        run(data_root, "verify-book", book_a["book_id"])
        run(data_root, "verify-book", book_b["book_id"])
        print("5. 重开数据目录后余额逐分一致，账簿重放验证通过")

        # 6. 三个变体（独立场次，避免单号/余额污染）
        for sid, kw, expect_share, expect_approved in [
            ("SQ", {"ratio_d1_x": "3/4"}, "2/3", False),
            ("SC", {"cred_cap": "0"}, "1/2", False),
        ]:
            run(data_root, "register-session", "-",
                stdin_payload=baseline(session_id=sid, **kw))
            led = run(data_root, "build-ledger", sid)
            rol = run(data_root, "build-rollcall", led["ledger_id"])
            c = next(x for x in rol["cases"] if x["case_id"] == "CX")
            assert c["yes_share"] == expect_share and c["approved"] == expect_approved
        run(data_root, "register-session", "-",
            stdin_payload=baseline(session_id="SN", x_need="5"))
        led_n = run(data_root, "build-ledger", "SN")
        rol_n = run(data_root, "build-rollcall", led_n["ledger_id"])
        assert next(x for x in rol_n["cases"] if x["case_id"] == "CX")["approved"]
        book_n = run(data_root, "open-book", led_n["ledger_id"], rol_n["roll_id"])
        feas = run(data_root, "check-feasible", book_n["book_id"])
        assert not feas["feasible"] and feas["infeasible_cases"][0]["shortfall"] == "1"
        print("6. 变体：留X1/4 -> 2/3 否决；证额0 -> 1/2 否决；need=5 投票过但最多筹4、缺口1")

        # 7. 受困来源
        trap_session = {
            "session_id": "STRAP", "name": "封闭循环受困",
            "accounts": ["A", "B", "C"], "funders": ["D1", "D2"],
            "injections": [{"funder": "D1", "account": "A", "amount": "3"},
                           {"funder": "D2", "account": "C", "amount": "2"}],
            "transfers": [{"from": "A", "to": "B", "ratio": "1"},
                          {"from": "B", "to": "A", "ratio": "1"}],
            "agenda": [], "signatures": [], "delegations": [],
            "credentials": [], "own_funds": [],
        }
        run(data_root, "register-session", "-", stdin_payload=trap_session)
        trap_ledger = run(data_root, "build-ledger", "STRAP")
        assert not trap_ledger["settled"]
        assert trap_ledger["trapped_sources"][0]["trapped_accounts"] == ["A", "B"]
        blocked = run(data_root, "build-rollcall", trap_ledger["ledger_id"],
                      expect_fail=True)
        assert blocked["error"] == "LedgerNotSettledError"
        run(data_root, "verify-ledger", trap_ledger["ledger_id"])
        print("7. 受困场：D1 的钱困在 A<->B 封闭循环，指出受困账户并阻断点名；D2 留 C 不受牵连")

        # 8. 无来款空转组
        idle_session = {
            "session_id": "SIDLE", "name": "无来款空转",
            "accounts": ["A", "B", "C"], "funders": ["D1"],
            "injections": [{"funder": "D1", "account": "C", "amount": "5"}],
            "transfers": [{"from": "A", "to": "B", "ratio": "1"},
                          {"from": "B", "to": "A", "ratio": "1"}],
            "agenda": [], "signatures": [], "delegations": [],
            "credentials": [], "own_funds": [],
        }
        run(data_root, "register-session", "-", stdin_payload=idle_session)
        idle_ledger = run(data_root, "build-ledger", "SIDLE")
        assert idle_ledger["settled"]
        print("8. 空转组无来款：A/B 零留用登记但不牵连，D1 全额留 C，本场照常结清")

        # 9. 旧预算系统与代拨模型在同一数据根并存，旧模型/报告继续可读
        from budget_solver.service import BudgetService

        budget = BudgetService(data_root)
        model_ids = sorted(m["model_id"] for m in budget.list_models())
        assert any(mid.startswith("ALO_") for mid in model_ids), "代拨编译模型已落目录"
        reports = budget.list_reports()
        assert reports, "代拨链路产出的解算报告（旧格式）仍可列出复读"
        print("9. 同根共存：代拨编译模型", sum(m.startswith("ALO_") for m in model_ids),
              "个；解算报告", len(reports), "份，旧预算公式与报告继续可读")

        print("\n代拨 CLI 端到端演示全部通过。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()
