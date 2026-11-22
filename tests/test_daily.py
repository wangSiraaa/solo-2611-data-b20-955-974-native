"""分日回款 + 现金柜端到端测试。

覆盖题目要求的三种情况，并额外验证：
* 同一天的循环与隔日滞后共存（跨日项不并成当天自环）；
* 自由参数跨日保留同一参数、只有真正唯一的回款才签定数；
* 不相关矛盾表不阻断选定回款格；
* 补录早期入账后重出一版，旧账本仍能复读；
* 现金柜不可行时指出从哪一天起没有可行资金安排。

运行：python -m tests.test_daily。
"""

import shutil
import tempfile

from budget_solver.service import BudgetService

from . import scenarios

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh_service():
    tmp = tempfile.mkdtemp(prefix="budget-daily-")
    return BudgetService(tmp), tmp


def run_model(service, payload, label=None):
    service.put_model(payload)
    snap = service.snapshot(payload["model_id"], label=label)
    artifact, _ = service.compile(snap["snap_id"], label=label)
    report = service.solve(artifact)
    return artifact, report, snap


# --------------------------------------------------- 情形一：基准 8 / 4 / 2
def test_case_base_b_only():
    service, tmp = fresh_service()
    try:
        artifact, report, snap = run_model(service, scenarios.daily_collection_model())
        # 当天循环不存在；跨日滞后单独成边
        check(artifact["artifact_kind"] == "compiled_daily_equations",
              "声明 horizon 的快照走分日编译")
        check(artifact["temporal_edges"] == [
            {"from": "M_COLLECT::x@2", "to": "M_COLLECT::x@1", "lag": 1},
            {"from": "M_COLLECT::x@3", "to": "M_COLLECT::x@2", "lag": 1},
        ], "跨日项被单列为相邻两日的滞后边，而不是当天自环")
        extracted = service.daily_collections(report)
        check(extracted["status"] == "ok", "各日回款均为唯一确定值")
        check(extracted["collections"] == {"1": "8", "2": "4", "3": "2"},
              "基准回款手算 8、4、2")
        check(service.verify_solve(report["solve_id"])["ok"],
              "分日报告独立验证通过")
        check(service.verify_compile(artifact)["ok"],
              "分日编译产物独立验证通过")

        service.put_cashier(scenarios.cashier_two_deposits())
        ledger = service.execute_cashier("CASH_MAIN", report["solve_id"])
        plan = ledger["plan"]
        check(plan["feasible"], "基准下现金柜可行")
        check(plan["selected_deposit_ids"] == ["B"], "基准只买 B（A 买不起）")
        check(plan["total_profit"] == "1", "基准收益为 1")
        check([d["closing_cash"] for d in plan["days"]] == ["3", "7", "9"],
              "逐日余额 3、7、9 手算一致")
        check(plan["min_closing_balance"] == "3", "最低日末余额为 3")
        # 账本重开可复读
        reopened = BudgetService(ledger["_root"] if "_root" in ledger else service.data_root)
        check(reopened.get_ledger(ledger["ledger_id"])["ledger_hash"]
              == ledger["ledger_hash"], "账本重开后哈希一致")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------------- 情形二：滞后系数改为 1
def test_case_lag_one_buys_a():
    service, tmp = fresh_service()
    try:
        _, report, _ = run_model(
            service, scenarios.daily_collection_model(late_ratio="1"),
            label="lag=1",
        )
        extracted = service.daily_collections(report)
        check(extracted["collections"] == {"1": "8", "2": "8", "3": "8"},
              "滞后系数为 1 时回款 8、8、8")
        service.put_cashier(scenarios.cashier_two_deposits())
        ledger = service.execute_cashier("CASH_MAIN", report["solve_id"])
        plan = ledger["plan"]
        check(plan["selected_deposit_ids"] == ["A"], "回款变 8、8、8 后可买 A")
        check(plan["total_profit"] == "2", "买 A 收益为 2")
        check([d["closing_cash"] for d in plan["days"]] == ["0", "2", "20"],
              "买 A 的逐日余额 0、2、20（日 1 全额买入 A）")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 情形三：回款不变，B 兑付额 9
def test_case_b_payout_nine():
    service, tmp = fresh_service()
    try:
        _, report, _ = run_model(service, scenarios.daily_collection_model())
        service.put_cashier(scenarios.cashier_two_deposits(b_payout="9"))
        ledger = service.execute_cashier("CASH_MAIN", report["solve_id"])
        plan = ledger["plan"]
        check(plan["selected_deposit_ids"] == ["B"],
              "恢复原回款后仍只买 B")
        check(plan["total_profit"] == "4", "B 兑付 9 时收益为 4")
        check([d["closing_cash"] for d in plan["days"]] == ["3", "10", "12"],
              "B 兑付 9 的逐日余额 3、10、12")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 当天循环与隔日返回共存（跨模型 + 跨日）
def test_same_day_cycle_with_lag():
    service, tmp = fresh_service()
    try:
        upstream = {
            "model_id": "UP_COLLECT",
            "name": "上游回款引擎",
            "horizon": 3,
            "day0_seed": {"p": "0"},
            "cells": {
                "in": {"kind": "input", "value": "0",
                       "daily": {"1": "6", "2": "2", "3": "0"}},
                "p": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "cell_id": "in"},
                    {"coef": "1/2", "cell_id": "q"},
                    {"coef": "1/5", "cell_id": "p", "lag": 1},
                ]},
                "q": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "cell_id": "p"},
                    {"coef": "-1/4", "cell_id": "q"},
                ]},
            },
            "outputs": ["p"],
        }
        downstream = {
            "model_id": "DOWN_BUDGET",
            "name": "下游预算",
            "horizon": 3,
            "day0_seed": {"UP_COLLECT::p": "2"},
            "cells": {
                "y": {"kind": "formula", "constant": "0", "terms": [
                    {"coef": "1", "model_id": "UP_COLLECT",
                     "cell_id": "p", "lag": 1},
                    {"coef": "1", "cell_id": "a"},
                ]},
                "a": {"kind": "input", "value": "1",
                      "daily": {"1": "1", "2": "1", "3": "1"}},
            },
            "outputs": ["y"],
        }
        service.put_model(upstream)
        service.put_model(downstream)
        snap = service.snapshot("DOWN_BUDGET")
        artifact, _ = service.compile(snap["snap_id"])
        cyclic = [c for c in artifact["components"] if c["cyclic"]]
        # p/q 的当天循环每日各一个 SCC，p 的跨日滞后不进 SCC
        check(
            sorted(sorted(c["variables"]) for c in cyclic) == [
                ["UP_COLLECT::p@1", "UP_COLLECT::q@1"],
                ["UP_COLLECT::p@2", "UP_COLLECT::q@2"],
                ["UP_COLLECT::p@3", "UP_COLLECT::q@3"],
            ],
            "每天各自的 p/q 循环独立成 SCC，跨日滞后没有并进当天自环",
        )
        report = service.solve(artifact)
        # 手算：q_d = 4/5 p_d；p_d = in_d + (1/2)(4/5)p_d + (1/5)p_{d-1}
        #       => (3/5)p_d = in_d + (1/5)p_{d-1} => p_d = (5 in_d + p_{d-1})/3
        check(
            [service.query(report["solve_id"],
                           f"UP_COLLECT::p@{d}")["value"] for d in (1, 2, 3)]
            == ["32/3", "62/9", "62/27"],
            "当天循环 + 隔日滞后手算 p = 32/3, 62/9, 62/27",
        )
        check(
            [service.query(report["solve_id"],
                           f"DOWN_BUDGET::y@{d}")["value"] for d in (1, 2, 3)]
            == ["3", "35/3", "71/9"],
            "下游跨模型滞后 y_d = p_{d-1} + a_d 手算 3, 35/3, 71/9",
        )
        check(service.verify_solve(report["solve_id"])["ok"],
              "同天循环 + 隔日返回共存的报告独立验证通过")
        check(service.verify_compile(artifact)["ok"],
              "同天循环 + 隔日返回共存的产物独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------- 内部入账 +1：各日回款变化与来源隔离
def test_impact_query():
    service, tmp = fresh_service()
    try:
        _, report, _ = run_model(service, scenarios.daily_collection_model())
        impact = service.impact(report, "M_COLLECT", "in", 1, "1")
        check(impact["affected_days"] == [1, 2, 3],
              "日 1 外部入账 +1 影响全部三天")
        deltas = {row["day"]: row["delta"] for row in impact["per_day"]}
        check(deltas == {1: "1", 2: "1/2", 3: "1/4"},
              "逐日变化 1、1/2、1/4 手算一致")
        impacted = service.impact(report, "M_COLLECT", "in", 2, "1")
        check(impacted["affected_days"] == [2, 3],
              "日 2 入账 +1 只影响日 2、3")
        check({r["day"]: r["delta"] for r in impacted["per_day"]}
              == {1: "0", 2: "1", 3: "1/2"}, "日 2 的逐日变化 0、1、1/2")
        untouched = service.daily_collections(
            service.get_report(report["solve_id"])
        )
        check(untouched["collections"] == {"1": "8", "2": "4", "3": "2"},
              "扰动不改原报告回款（不制造现金）")
        check(service.verify_solve(impact["perturbed_report_id"])["ok"],
              "扰动派生报告带来源标记，独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------- 自由参数跨日保留；自由/矛盾只按选定格阻断
def test_free_params_and_isolation():
    service, tmp = fresh_service()
    try:
        payload = scenarios.daily_collection_model()
        payload["cells"].update({
            # 跨日自由参数：a_d = b_d 自由，s_d = a_{d-1} 携带同一参数
            "a": {"kind": "formula", "constant": "0",
                  "terms": [{"coef": "1", "cell_id": "b"}]},
            "b": {"kind": "formula", "constant": "0",
                  "terms": [{"coef": "1", "cell_id": "a"}]},
            "s": {"kind": "formula", "constant": "0",
                  "terms": [{"coef": "1", "cell_id": "a", "lag": 1}]},
            "r": {"kind": "formula", "constant": "0", "terms": [
                {"coef": "1", "cell_id": "a", "lag": 1},
                {"coef": "-1", "cell_id": "b", "lag": 1},
            ]},
            # 不相关矛盾表
            "c1": {"kind": "formula", "constant": "1",
                   "terms": [{"coef": "1", "cell_id": "c2"}]},
            "c2": {"kind": "formula", "constant": "1",
                   "terms": [{"coef": "1", "cell_id": "c1"}]},
        })
        payload["day0_seed"] = {"x": "0", "a": "0", "b": "0"}
        payload["outputs"] = ["x", "a", "s", "r", "c1"]
        artifact, report, _ = run_model(service, payload)

        # 只有确实唯一的 r 能签为定数；a 自由但 s 携带同一自由参数
        signed = service.daily_collections(report, cell_id="r")
        check(signed["status"] == "ok" and signed["collections"] ==
              {"1": "0", "2": "0", "3": "0"},
              "r_d = a_{d-1} - b_{d-1} 恒为 0，可签为定数")
        free = service.daily_collections(report, cell_id="a")
        check(free["status"] == "undetermined",
              "a 有自由参数，不签为定数")
        carried = service.daily_collections(report, cell_id="s")
        check(carried["status"] == "undetermined",
              "s 跨日携带自由参数，不签为定数")
        a1_params = {
            t["parameter"]
            for t in service.query(report["solve_id"], "M_COLLECT::a@1")[
                "expression"]["param_terms"]
        }
        s2_params = {
            t["parameter"]
            for t in service.query(report["solve_id"], "M_COLLECT::s@2")[
                "expression"]["param_terms"]
        }
        check(s2_params == a1_params,
              "隔日项引用的是前一日同一自由参数，而不是复制成定数")
        # 不相关矛盾只出现在它自己的来源里，不阻断选定回款格
        x = service.daily_collections(report, cell_id="x")
        check(x["status"] == "ok" and x["collections"] ==
              {"1": "8", "2": "4", "3": "2"},
              "不相关矛盾表 c1/c2 不阻断选定回款格 x")
        check(any(i["cell_id"] == "c1" and i["status"] == "inconsistent"
                  for i in x["unrelated_issues"]),
              "矛盾来源被单独标明")
        bad = service.daily_collections(report, cell_id="c1")
        check(bad["status"] == "contradiction",
              "选到矛盾格时明确报告矛盾")
        check(service.verify_solve(report["solve_id"])["ok"],
              "自由/矛盾混合报告独立验证通过")

        # 现金柜拒收自由/矛盾回款
        service.put_cashier(scenarios.cashier_two_deposits())
        rejected = service.execute_cashier(
            "CASH_MAIN", report["solve_id"], cell_id="a"
        )
        check(rejected["feasible"] is False
              and rejected["status"] == "undetermined",
              "现金柜拒收含自由参数的回款")
    finally:
        shutil.rmtree(tmp)


# ----------------------- 补录早期入账重出一版；旧账本仍能复读；不可行定位
def test_backfill_and_infeasible():
    service, tmp = fresh_service()
    try:
        payload = scenarios.daily_collection_model()
        _, report_old, _ = run_model(service, payload, label="补录前")
        service.put_cashier(scenarios.cashier_two_deposits())
        ledger_old = service.execute_cashier("CASH_MAIN", report_old["solve_id"])
        check(ledger_old["plan"]["selected_deposit_ids"] == ["B"],
              "补录前买 B")

        # 补录日 1 额外入账 20，重出一版
        backfilled = scenarios.daily_collection_model()
        backfilled["cells"]["in"]["daily"]["1"] = "28"
        artifact_new, report_new, _ = run_model(
            service, backfilled, label="补录后"
        )
        new_collections = service.daily_collections(report_new)
        check(new_collections["collections"] == {"1": "28", "2": "14", "3": "7"},
              "补录后回款 28、14、7")
        ledger_new = service.execute_cashier("CASH_MAIN", report_new["solve_id"])
        check(ledger_new["plan"]["selected_deposit_ids"] == ["A", "B"],
              "补录后买得起 A 与 B")
        check(ledger_new["plan"]["total_profit"] == "3",
              "补录后收益 3")

        # 旧账本仍能原样复读
        reopened = BudgetService(service.data_root)
        old = reopened.get_ledger(ledger_old["ledger_id"])
        check(old["ledger_hash"] == ledger_old["ledger_hash"]
              and old["plan"]["total_profit"] == "1",
              "补录后旧账本哈希与收益原样可复读")
        check(reopened.get_report(report_old["solve_id"])["report_hash"]
              == report_old["report_hash"],
              "旧解算报告也原样保留")

        # 不可行：期初 0、日 1 必须付 5，且日 1 无回款
        hopeless = scenarios.cashier_two_deposits(cashier_id="CASH_TIGHT")
        hopeless["mandatory"] = {"1": "5"}
        hopeless["deposits"] = []
        service.put_cashier(hopeless)
        empty_model = scenarios.daily_collection_model(model_id="M_EMPTY")
        empty_model["cells"]["in"]["daily"] = {"1": "0", "2": "0", "3": "0"}
        _, report_empty, _ = run_model(service, empty_model, label="无回款")
        failed = service.execute_cashier("CASH_TIGHT", report_empty["solve_id"])
        check(failed["plan"]["feasible"] is False,
              "无回款且日 1 必付时不可行")
        check(failed["plan"]["first_unfundable_day"] == 1,
              "指出从日 1 起再也没有可行资金安排")
        check("日 1" in failed["plan"]["reason"], "不可行原因带具体日期")
    finally:
        shutil.rmtree(tmp)


# ----------------------- 恰好补够必付款 / 同日历多现金柜 / 不可行定位
def test_exact_cover_and_multiple_cashiers():
    service, tmp = fresh_service()
    try:
        # 回款只有日 1 = 5，日 2 必付 6；日 1 发行存单 5→日2收 6，恰好补够。
        model = scenarios.daily_collection_model(model_id="M_EXACT")
        model["cells"]["in"]["daily"] = {"1": "5", "2": "0", "3": "0"}
        model["cells"]["x"]["terms"] = [{"coef": "1", "cell_id": "in"}]
        _, report, _ = run_model(service, model, label="恰好补够")
        exact = {
            "cashier_id": "CASH_EXACT",
            "name": "恰好补够",
            "horizon": 3,
            "opening_cash": "0",
            "mandatory": {"2": "6"},
            "deposits": [{
                "id": "E1", "issue_day": 1, "maturity_day": 2,
                "principal": "5", "payout": "6",
            }],
        }
        service.put_cashier(exact)
        ledger = service.execute_cashier("CASH_EXACT", report["solve_id"])
        plan = ledger["plan"]
        check(plan["selected_deposit_ids"] == ["E1"],
              "到期收益恰好补够必付款的存单被选中")
        check([d["closing_cash"] for d in plan["days"]] == ["0", "0", "0"],
              "恰好补够时逐日余额 0、0、0")

        # 同一回款日历交给两个不同现金柜，账本互相独立
        service.put_cashier(scenarios.cashier_two_deposits(cashier_id="CASH_ONE"))
        service.put_cashier(scenarios.cashier_two_deposits(
            cashier_id="CASH_TWO", b_payout="9"))
        base_model = scenarios.daily_collection_model()
        _, rep2, _ = run_model(service, base_model, label="多柜")
        one = service.execute_cashier("CASH_ONE", rep2["solve_id"])
        two = service.execute_cashier("CASH_TWO", rep2["solve_id"])
        check(one["plan"]["total_profit"] == "1"
              and two["plan"]["total_profit"] == "4",
              "相同回款日历下不同现金柜收益各自计算")
        check(one["ledger_id"] != two["ledger_id"]
              and one["collections"] == two["collections"],
              "两账本独立落盘，回款来源相同")

        # 不可行且要精确定位：日 1 无回款、日 2 必付 6（无法靠存单筹集）
        hopeless = {
            "cashier_id": "CASH_HOPELESS",
            "name": "无从筹集",
            "horizon": 3,
            "opening_cash": "0",
            "mandatory": {"2": "6"},
            "deposits": [],
        }
        service.put_cashier(hopeless)
        empty = scenarios.daily_collection_model(model_id="M_ZERO")
        empty["cells"]["in"]["daily"] = {"1": "0", "2": "0", "3": "0"}
        empty["cells"]["x"]["terms"] = [{"coef": "1", "cell_id": "in"}]
        _, rep_zero, _ = run_model(service, empty, label="无回款")
        failed = service.execute_cashier("CASH_HOPELESS", rep_zero["solve_id"])
        check(failed["plan"]["feasible"] is False
              and failed["plan"]["first_unfundable_day"] == 2,
              "无回款且日 2 必付 6：日 1 尚可，日 2 起没有可行安排")
        check(failed["plan"]["last_survivable_day"] == 1,
              "明确标出最后可维持的日为日 1")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_case_base_b_only,
        test_case_lag_one_buys_a,
        test_case_b_payout_nine,
        test_same_day_cycle_with_lag,
        test_impact_query,
        test_free_params_and_isolation,
        test_backfill_and_infeasible,
        test_exact_cover_and_multiple_cashiers,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n分日回款 / 现金柜全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
