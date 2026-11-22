"""分日回款 / 现金柜现场演示：走真实编译与落盘入口展示题目三种情况。

运行：python -m tests.demo_daily

全部数值为精确分数；每一步都经过目录快照、编译器、解算器、独立验证器，
账本与报告都真正落盘并可重开复读。
"""

import json
import shutil
import tempfile

from budget_solver.service import BudgetService

from . import scenarios


def show(title):
    print(f"\n=== {title} ===")


def publish(service, payload, label):
    service.put_model(payload)
    snap = service.snapshot(payload["model_id"], label=label)
    artifact, path = service.compile(snap["snap_id"], label=label)
    report = service.solve(artifact)
    return snap, artifact, path, report


def main():
    data_root = tempfile.mkdtemp(prefix="budget-daily-demo-")
    try:
        service = BudgetService(data_root)

        show("情形一：回款 8 / 4 / 2，只买 B，收益 1")
        snap, art, path, rep = publish(
            service, scenarios.daily_collection_model(), "基准"
        )
        print("编译产物:", art["artifact_kind"],
              "｜跨日滞后边:", json.dumps(art["temporal_edges"], ensure_ascii=False))
        print("落盘产物:", path.split("/")[-1],
              "｜独立验证编译:", service.verify_compile(art)["check_count"], "项")
        ext = service.daily_collections(rep)
        print("各日回款（唯一确定）:", ext["collections"],
              "｜回款格:", ext["cell_id"])
        service.put_cashier(scenarios.cashier_two_deposits())
        ledger1 = service.execute_cashier("CASH_MAIN", rep["solve_id"])
        print("选中存单:", ledger1["plan"]["selected_deposit_ids"],
              "｜收益:", ledger1["plan"]["total_profit"],
              "｜最低日末余额:", ledger1["plan"]["min_closing_balance"])
        for day in ledger1["plan"]["days"]:
            print(f"  日{day['day']}: 收 {day['collections']} "
                  f"+ 到期 {[m['payout'] for m in day['maturities']]} "
                  f"- 必付 {day['mandatory_payment']} "
                  f"- 购 {[p['cost'] for p in day['purchases']]} "
                  f"= 余额 {day['closing_cash']}")

        show("情形二：滞后系数改为 1，回款 8 / 8 / 8，改买 A，收益 2")
        _, art2, path2, rep2 = publish(
            service, scenarios.daily_collection_model(late_ratio="1"), "滞后=1"
        )
        print("各日回款:", service.daily_collections(rep2)["collections"])
        ledger2 = service.execute_cashier("CASH_MAIN", rep2["solve_id"])
        print("选中存单:", ledger2["plan"]["selected_deposit_ids"],
              "｜收益:", ledger2["plan"]["total_profit"],
              "｜逐日余额:",
              [d["closing_cash"] for d in ledger2["plan"]["days"]])

        show("情形三：恢复原回款，仅把 B 的兑付额改为 9，买 B 收益 4")
        _, art3, _, rep3 = publish(
            service, scenarios.daily_collection_model(), "原回款"
        )
        service.put_cashier(scenarios.cashier_two_deposits(b_payout="9"))
        ledger3 = service.execute_cashier("CASH_MAIN", rep3["solve_id"])
        print("各日回款仍为:", service.daily_collections(rep3)["collections"])
        print("选中存单:", ledger3["plan"]["selected_deposit_ids"],
              "｜收益:", ledger3["plan"]["total_profit"],
              "｜逐日余额:",
              [d["closing_cash"] for d in ledger3["plan"]["days"]])

        show("补充验证一：当天循环与隔日返回共存")
        upstream = {
            "model_id": "UP_COLLECT", "name": "上游回款引擎",
            "horizon": 3, "day0_seed": {"p": "0"},
            "cells": {
                "in": {"kind": "input", "value": "0",
                       "daily": {"1": "6", "2": "2", "3": "0"}},
                "p": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "cell_id": "in"},
                    {"coef": "1/2", "cell_id": "q"},
                    {"coef": "1/5", "cell_id": "p", "lag": 1}]},
                "q": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "cell_id": "p"},
                    {"coef": "-1/4", "cell_id": "q"}]},
            },
            "outputs": ["p"],
        }
        downstream = {
            "model_id": "DOWN_BUDGET", "name": "下游预算",
            "horizon": 3, "day0_seed": {"UP_COLLECT::p": "2"},
            "cells": {
                "y": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "model_id": "UP_COLLECT",
                     "cell_id": "p", "lag": 1},
                    {"coef": "1", "cell_id": "a"}]},
                "a": {"kind": "input", "value": "1",
                      "daily": {"1": "1", "2": "1", "3": "1"}},
            },
            "outputs": ["y"],
        }
        service.put_model(upstream)
        service.put_model(downstream)
        snap_mix = service.snapshot("DOWN_BUDGET", label="混合")
        art_mix, _ = service.compile(snap_mix["snap_id"])
        cyclic = [c["variables"] for c in art_mix["components"] if c["cyclic"]]
        print("每天各自的 p/q 循环 SCC:", cyclic)
        print("跨日滞后边:",
              [(e["from"], e["to"]) for e in art_mix["temporal_edges"]])
        rep_mix = service.solve(art_mix)
        print("上游 p:", [rep_mix["outputs"][f"UP_COLLECT::p@{d}"]["value"]
                          for d in (1, 2, 3)])
        print("下游 y:", [rep_mix["outputs"][f"DOWN_BUDGET::y@{d}"]["value"]
                          for d in (1, 2, 3)])
        print("独立验证解算:", service.verify_solve(rep_mix["solve_id"])["check_count"],
              "项")

        show("补充验证二：翻查补款以前的旧账")
        reopened = BudgetService(data_root)
        old = reopened.get_ledger(ledger1["ledger_id"])
        print("重开数据目录后旧账本:", old["ledger_id"],
              "哈希", old["ledger_hash"][:18] + "...")
        print("旧账本收益:", old["plan"]["total_profit"],
              "｜选中:", old["plan"]["selected_deposit_ids"],
              "｜绑定报告:", old["solve_id"])
        print("旧解算报告仍可复读:",
              reopened.get_report(old["solve_id"])["outputs"]["M_COLLECT::x@2"]["value"])

        show("补充验证三：外部入账 +1 的影响查询")
        impact = service.impact(rep3, "M_COLLECT", "in", 1, "1")
        print("日 1 入账 +1 影响的回款日:", impact["affected_days"])
        print("逐日变化:", [(r["day"], r["delta"]) for r in impact["per_day"]])
        print("派生报告独立验证:",
              service.verify_solve(impact["perturbed_report_id"])["check_count"], "项")

        print("\n演示完成。数据目录：", data_root)
    finally:
        shutil.rmtree(data_root)


if __name__ == "__main__":
    main()
