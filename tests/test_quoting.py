"""数量报价曲线 + 采购额度核对的端到端断言。

运行：python -m tests.test_quoting（在仓库根目录）。覆盖：

* 题目手算主例的三个模型版本与两份保留的编译产物、重开复读；
* 曲线目录四分类（整域唯一 / 部分相容 / 多单价 / 完全矛盾）与负价区段；
* 预算核对的二次整数边界、紧邻未入段原因、新预算重算保留合同对应；
* 单点报价、十亿件大域不枚举、输入校验、独立验证路径。
"""

import shutil
import tempfile
import time
from fractions import Fraction

from budget_solver.errors import ValidationError
from budget_solver.quoting import (
    KIND_CONTRA,
    KIND_MULTI,
    KIND_PARTIAL,
    KIND_UNIQUE,
    MAX_DOMAIN_SIZE,
    MAX_REBATE_TIERS,
    ST_INCONSISTENT,
    ST_NEGATIVE,
    ST_NON_UNIQUE,
    ST_QUOTABLE,
    floor_frac,
)
from budget_solver.service import BudgetService

from . import quote_scenarios as qs

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh_service():
    tmp = tempfile.mkdtemp(prefix="budget-quote-")
    return BudgetService(tmp), tmp


def publish_compile(service, payload, label=None):
    service.put_model(payload)
    snap = service.snapshot(payload["model_id"], label=label)
    artifact, _ = service.compile(snap["snap_id"], label=label)
    return artifact, snap


# ------------------------------------------------- 题目主例：循环费率报价曲线
def test_main_worked_example_three_versions():
    service, tmp = fresh_service()
    try:
        # ---- 版本 1：q 点值 0，x = 2 + q/4 + x/2，报价域 1..8 ----
        art1, snap1 = publish_compile(
            service, qs.cyclic_rate_quote_model(q_coef="1/4"), label="v1-四分之一"
        )
        check(art1["variables"]["M_QUOTE::q"]["kind"] == "input",
              "订量格是冻结作用域里的真实输入格")
        # 原 solve / query 固定订量路径照常：q=0 时 x=4
        report0 = service.solve(art1)
        q0 = service.query(report0["solve_id"], "M_QUOTE::x")
        check(q0["status"] == "determined" and q0["value"] == "4",
              "固定订量 q=0 的一次解仍为 4，原 solve/query 路径不受影响")

        curve1 = service.create_curve(
            art1, "M_QUOTE::q", "M_QUOTE::x", 1, 8, label="主例曲线"
        )
        check(curve1["classification"] == KIND_UNIQUE, "主例整域唯一可报价")
        check(curve1["price_relation"]["intercept"] == "4"
              and curve1["price_relation"]["q_coef"] == "1/2",
              "由实际方程推出精确关系 x(q)=4+q/2，而非沿用 q=0 的旧单价 4")
        check(len(curve1["segments"]) == 1
              and curve1["segments"][0]["status"] == ST_QUOTABLE,
              "1..8 单价全部非负，单一可报价段")
        check(curve1["frozen_q_value"] == "0", "曲线记录冻结点值 0")
        # 逐点手算单价
        for q, expect in ((1, "9/2"), (3, "11/2"), (4, "6"), (8, "8")):
            check(service.quote_at(curve1["curve_id"], q)["unit_price"] == expect,
                  f"单点报价 q={q} 手算 {expect}")

        # 预算：1–3 不返，4–8 返 15，预算 12
        budget1 = service.check_budget(
            curve1["curve_id"], qs.main_rebate_spec(), label="主例预算"
        )
        spans = [(s["lo"], s["hi"]) for s in budget1["segments"]]
        check(spans == [(1, 2), (4, 4)], "可买极大连续段恰为 [1,2] 与 [4,4]")
        seg12, seg44 = budget1["segments"]
        check(seg12["payment_at_lo"] == "9/2" and seg12["payment_at_hi"] == "10",
              "[1,2] 端点净付 9/2 与 10")
        check(seg44["payment_at_lo"] == "9" and seg44["payment_at_hi"] == "9",
              "第四件净付 9（毛利 24 减回扣 15）")
        # 第三件要 16.5 而第四件只需 9：紧邻原因必须是超预算而非不可报价
        rn = seg12["right_neighbor"]
        check(rn["q"] == 3 and rn["status"] == ST_QUOTABLE
              and rn["reason"] == "over_budget"
              and Fraction(rn["payment"]) == Fraction("33/2"),
              "第三件可报价但净付 33/2=16.5 超预算 12")
        ln = seg44["left_neighbor"]
        check(ln["q"] == 3 and Fraction(ln["payment"]) == Fraction("33/2"),
              "[4,4] 左紧邻同指第三件 16.5")
        rx = seg44["right_neighbor"]
        check(rx["q"] == 5 and rx["reason"] == "over_budget",
              "第五件净付 35/2=17.5 超预算，断点两侧原因齐备")
        check(budget1["payable_piece_count"] == 3, "可买件数合计 3")

        # ---- 版本 2：只把 q 的系数改为 1/2，原点单价仍为 4 ----
        art2, _ = publish_compile(
            service, qs.cyclic_rate_quote_model(q_coef="1/2"), label="v2-二分之一"
        )
        check(art2["artifact_hash"] != art1["artifact_hash"],
              "系数变更产生新的编译产物，两版产物均保留")
        report0b = service.solve(art2)
        check(service.query(report0b["solve_id"], "M_QUOTE::x")["value"] == "4",
              "q=0 原点单价仍为 4（截距不变，变的是 q 系数）")
        curve2 = service.create_curve(
            art2, "M_QUOTE::q", "M_QUOTE::x", 1, 8, label="半系数曲线"
        )
        check(curve2["price_relation"]["q_coef"] == "1"
              and curve2["price_relation"]["intercept"] == "4",
              "新关系 x(q)=4+q：斜率联动而非固定费率")
        budget2 = service.check_budget(curve2["curve_id"], qs.main_rebate_spec())
        check([(s["lo"], s["hi"]) for s in budget2["segments"]] == [(1, 2)],
              "半系数版本只剩 [1,2] 可买")
        check(budget2["segments"][0]["right_neighbor"]["q"] == 3
              and budget2["segments"][0]["right_neighbor"]["reason"] == "over_budget",
              "半系数版第三件 7 超预算")

        # ---- 版本 3：恢复原模型，仅把返 15 起点改为 3，沿用 curve1 ----
        budget3 = service.check_budget(
            curve1["curve_id"], qs.rebate_start_at_three_spec(), label="回扣前移"
        )
        check([(s["lo"], s["hi"]) for s in budget3["segments"]] == [(1, 4)],
              "回扣起点改为 3 后可买 [1,4]（第三件净付 24/2-15=1.5）")
        check(budget3["curve_content_hash"] == curve1["content_hash"]
              and budget3["curve_id"] == curve1["curve_id"],
              "新预算报告重算并保留原合同（同一条曲线）对应关系")
        # 旧报告原样保留
        old = service.get_budget_report(budget1["budget_id"])
        check([(s["lo"], s["hi"]) for s in old["segments"]] == [(1, 2), (4, 4)],
              "旧预算报告不被新预算重算覆盖")
        check(service.verify_curve(curve1["curve_id"])["ok"]
              and service.verify_curve(curve2["curve_id"])["ok"],
              "两版曲线均通过独立验证")
        check(service.verify_budget_report(budget1["budget_id"])["ok"]
              and service.verify_budget_report(budget2["budget_id"])["ok"]
              and service.verify_budget_report(budget3["budget_id"])["ok"],
              "三份预算报告均通过独立验证")
        check(service.verify_solve(report0["solve_id"])["ok"]
              and service.verify_solve(report0b["solve_id"])["ok"],
              "两版编译产物的固定订量解算报告独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 部分订量相容的真实循环例
def test_partial_consistency_cycle():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(service, qs.partial_consistency_cycle())
        curve = service.create_curve(art, "M_PART::q", "M_PART::c", 1, 5)
        check(curve["classification"] == KIND_PARTIAL, "目录识别为部分订量相容")
        check(curve["consistent_single_q"] == 2, "只有 q=2 满足循环块相容约束")
        statuses = [(s["lo"], s["hi"], s["status"]) for s in curve["segments"]]
        check(statuses == [(1, 1, ST_INCONSISTENT),
                           (2, 2, ST_QUOTABLE),
                           (3, 5, ST_INCONSISTENT)],
              "1 与 3..5 不相容，仅 q=2 可报价")
        check(curve["segments"][1]["price_at_lo"] == "9",
              "q=2 单价 c=a-b+6=3+6=9（自由格被消去，单价唯一）")
        cert = curve["q_constraints"][0]
        check(Fraction(cert["residual_constant"]) == 4
              and Fraction(cert["residual_q_coef"]) == -2
              and cert["required_q"] == "2",
              "约束证书 0=4-2q 给出可用订量 q=2 与来源 λ 组合")
        check(service.quote_at(curve["curve_id"], 2)["unit_price"] == "9",
              "单点查询 q=2 返回 9")
        out3 = service.quote_at(curve["curve_id"], 3)
        check(out3["status"] == ST_INCONSISTENT and out3["unit_price"] is None,
              "单点查询 q=3 明确不相容而非沿用旧价")
        # 预算核对：不相容订量绝不入段
        spec = {"domain": [1, 5], "budget": "100",
                "tiers": [{"lo": 1, "hi": 5, "rebate": "0"}]}
        report = service.check_budget(curve["curve_id"], spec)
        check([(s["lo"], s["hi"]) for s in report["segments"]] == [(2, 2)],
              "预算核对只接受唯一报价且能付款的 q=2")
        check(report["segments"][0]["left_neighbor"]["status"] == ST_INCONSISTENT
              and report["segments"][0]["right_neighbor"]["status"]
              == ST_INCONSISTENT,
              "紧邻未入段原因标注为曲线不相容")
        check(service.verify_curve(curve["curve_id"])["ok"]
              and service.verify_budget_report(report["budget_id"])["ok"],
              "部分相容曲线与预算报告独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 同一订量多个单价
def test_multi_price_cycle():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(service, qs.multi_price_cycle())
        curve = service.create_curve(art, "M_MULTI::q", "M_MULTI::a", 1, 8)
        check(curve["classification"] == KIND_MULTI, "目录识别为同订量多单价")
        check(all(s["status"] == ST_NON_UNIQUE for s in curve["segments"]),
              "整个域每个订量都有多个单价")
        check(set(curve["price_relation"]["free_params"]) == {"M_MULTI::b"},
              "自由格被明确列为单价表达式的自由参数（RREF 选 b 作自由列）")
        check(set(curve["free_variables"]) == {"M_MULTI::b"},
              "曲线 free_variables 交代闭包内的自由格")
        out = service.quote_at(curve["curve_id"], 4)
        check(out["status"] == ST_NON_UNIQUE and out["unit_price"] is None,
              "单点多单价不返回伪造数值")
        spec = {"domain": [1, 8], "budget": "999999",
                "tiers": [{"lo": 1, "hi": 8, "rebate": "0"}]}
        report = service.check_budget(curve["curve_id"], spec)
        check(report["segments"] == [] and report["empty_reason"],
              "多单价曲线即使预算巨大也无可买段，并给出空原因")
        check(service.verify_curve(curve["curve_id"])["ok"], "多单价曲线独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 完全矛盾
def test_fully_contradictory_cycle():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(service, qs.fully_contradictory_cycle())
        curve = service.create_curve(art, "M_CT::q", "M_CT::a", 1, 8)
        check(curve["classification"] == KIND_CONTRA, "目录识别为完全矛盾")
        check(all(s["status"] == ST_INCONSISTENT for s in curve["segments"]),
              "全部订量不相容")
        hard = curve["hard_contradictions"][0]
        check(Fraction(hard["residual_rhs"]) == 2 and hard["equation_combination"],
              "硬矛盾证书 0=2 附来源方程 λ 组合")
        check(service.quote_at(curve["curve_id"], 1)["status"] == ST_INCONSISTENT,
              "单点查询报不相容")
        spec = {"domain": [1, 8], "budget": "1",
                "tiers": [{"lo": 1, "hi": 8, "rebate": "0"}]}
        report = service.check_budget(curve["curve_id"], spec)
        check(report["segments"] == [], "完全矛盾曲线无任何可买段")
        check(service.verify_curve(curve["curve_id"])["ok"], "矛盾曲线独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 负价不可报价区段
def test_negative_price_segment():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(service, qs.negative_price_model())
        curve = service.create_curve(art, "M_NEG::q", "M_NEG::p", 1, 5)
        check(curve["classification"] == KIND_UNIQUE, "关系仍唯一，分类为整域唯一")
        spans = [(s["lo"], s["hi"], s["status"]) for s in curve["segments"]]
        check(spans == [(1, 2, ST_QUOTABLE), (3, 5, ST_NEGATIVE)],
              "q=1,2 非负可报价；q≥3 负价列为不可报价区段")
        check([(s["lo"], s["hi"]) for s in curve["quotable_segments"]] == [(1, 2)],
              "可报价段只含非负部分")
        check(service.quote_at(curve["curve_id"], 3)["status"] == ST_NEGATIVE,
              "负价单点状态为 negative_price")
        spec = {"domain": [1, 5], "budget": "1000",
                "tiers": [{"lo": 1, "hi": 5, "rebate": "0"}]}
        report = service.check_budget(curve["curve_id"], spec)
        check([(s["lo"], s["hi"]) for s in report["segments"]] == [(1, 2)],
              "预算再大也不买入负价区段")
        check(report["segments"][0]["right_neighbor"]["status"] == ST_NEGATIVE,
              "紧邻未入段原因是曲线负价而非超预算")
        check(service.verify_curve(curve["curve_id"])["ok"]
              and service.verify_budget_report(report["budget_id"])["ok"],
              "负价曲线与预算报告独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 重开：两版产物与曲线持久化
def test_reopen_persistence():
    tmp = tempfile.mkdtemp(prefix="budget-quote-reopen-")
    try:
        service = BudgetService(tmp)
        art1, _ = publish_compile(
            service, qs.cyclic_rate_quote_model(q_coef="1/4"), label="v1")
        c1 = service.create_curve(art1, "M_QUOTE::q", "M_QUOTE::x", 1, 8)
        b1 = service.check_budget(c1["curve_id"], qs.main_rebate_spec())
        art2, _ = publish_compile(
            service, qs.cyclic_rate_quote_model(q_coef="1/2"), label="v2")
        c2 = service.create_curve(art2, "M_QUOTE::q", "M_QUOTE::x", 1, 8)

        reopened = BudgetService(tmp)
        curves = reopened.list_curves()
        check(len(curves) == 2 and {c["curve_id"] for c in curves}
              == {c1["curve_id"], c2["curve_id"]}, "重开后两版曲线均可列出")
        rc1 = reopened.get_curve(c1["curve_id"])
        check(rc1["content_hash"] == c1["content_hash"]
              and rc1["price_relation"]["q_coef"] == "1/2",
              "重开后曲线哈希与关系不变")
        check(reopened.quote_at(c1["curve_id"], 4)["unit_price"] == "6",
              "重开后单点报价一致")
        rb1 = reopened.get_budget_report(b1["budget_id"])
        check(rb1["report_hash"] == b1["report_hash"]
              and rb1["curve_id"] == c1["curve_id"],
              "重开后预算报告哈希与合同对应关系不变")
        check(reopened.verify_curve(c1["curve_id"])["ok"]
              and reopened.verify_curve(c2["curve_id"])["ok"]
              and reopened.verify_budget_report(b1["budget_id"])["ok"],
              "重开后独立验证仍通过")
        # 新预算重算旧曲线
        b3 = reopened.check_budget(c1["curve_id"], qs.rebate_start_at_three_spec())
        check([(s["lo"], s["hi"]) for s in b3["segments"]] == [(1, 4)],
              "重开后旧曲线可用新预算重算")
        check(len(reopened.list_budget_reports()) == 2, "新旧预算报告并存")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 十亿件大域：不枚举、精确边界
def test_huge_domain_no_enumeration():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(service, {
            "model_id": "M_HUGE", "name": "十亿件",
            "cells": {
                "q": {"kind": "input", "value": "0"},
                "p": {"kind": "formula", "constant": "1/2",
                      "terms": [{"coef": "1/3", "cell_id": "q"}]},
            },
            "outputs": ["p"],
        })
        t0 = time.perf_counter()
        curve = service.create_curve(
            art, "M_HUGE::q", "M_HUGE::p", 1, MAX_DOMAIN_SIZE)
        tiers = []
        step = MAX_DOMAIN_SIZE // MAX_REBATE_TIERS
        for i in range(MAX_REBATE_TIERS):
            lo = 1 + i * step
            hi = MAX_DOMAIN_SIZE if i == MAX_REBATE_TIERS - 1 else lo + step - 1
            tiers.append({"lo": lo, "hi": hi, "rebate": str(i * 1000)})
        spec = {"domain": [1, MAX_DOMAIN_SIZE],
                "budget": "100000000000", "tiers": tiers}
        report = service.check_budget(curve["curve_id"], spec)
        elapsed = time.perf_counter() - t0
        check(elapsed < 2.0, f"十亿件 / 30 档在 2 秒内完成（实测 {elapsed:.3f}s）")
        # 边界精确性：每段右端点可付、右邻点不可付（除非撞上回扣断点或查询域末端）
        a, b = Fraction(1, 2), Fraction(1, 3)
        gross = lambda q: b * q * q + a * q
        for seg in report["segments"]:
            t = seg["hi"]
            check(Fraction(seg["payment_at_hi"]) <= Fraction(spec["budget"]),
                  f"段右端 {t} 净付不超预算")
            if t < MAX_DOMAIN_SIZE:
                nxt = seg["right_neighbor"]
                if nxt["reason"] == "over_budget":
                    check(gross(t + 1) - Fraction(nxt["rebate"])
                          > Fraction(spec["budget"]),
                          "右邻点独立复算确实超预算（精确整数边界，无浮点）")
        check(service.verify_budget_report(report["budget_id"])["ok"],
              "十亿件预算报告独立验证通过")
        # 分数地板/天花板小工具的手算
        check(floor_frac(Fraction(-7, 2)) == -4
              and floor_frac(Fraction(7, 2)) == 3, "floor_frac 负数地板除正确")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 输入校验与价格表约束
def test_validation_and_tier_guardrails():
    service, tmp = fresh_service()
    try:
        art, _ = publish_compile(
            service, qs.cyclic_rate_quote_model(), label="v1")
        for bad in ((0, 8), (5, 4), (1, MAX_DOMAIN_SIZE + 1)):
            try:
                service.create_curve(
                    art, "M_QUOTE::q", "M_QUOTE::x", bad[0], bad[1])
                raise AssertionError(f"非法报价域 {bad} 必须被拒绝")
            except ValidationError:
                pass
        # 订量格必须是输入格、单价必须是根输出
        try:
            service.create_curve(art, "M_QUOTE::x", "M_QUOTE::x", 1, 8)
            raise AssertionError("公式格作订量必须被拒绝")
        except ValidationError:
            pass
        try:
            service.create_curve(art, "M_QUOTE::q", "M_QUOTE::q", 1, 8)
            raise AssertionError("非根输出作单价必须被拒绝")
        except ValidationError:
            pass
        curve = service.create_curve(art, "M_QUOTE::q", "M_QUOTE::x", 1, 8)
        base = {"domain": [1, 8], "budget": "12",
                "tiers": [{"lo": 1, "hi": 3, "rebate": "0"},
                          {"lo": 4, "hi": 8, "rebate": "15"}]}
        # 不连续覆盖
        bad_gap = {"domain": [1, 8], "budget": "12",
                   "tiers": [{"lo": 1, "hi": 2, "rebate": "0"},
                             {"lo": 4, "hi": 8, "rebate": "15"}]}
        try:
            service.check_budget(curve["curve_id"], bad_gap)
            raise AssertionError("档表留缺口必须被拒绝")
        except ValidationError:
            pass
        # 越出报价域
        bad_domain = {"domain": [1, 9], "budget": "12",
                      "tiers": [{"lo": 1, "hi": 9, "rebate": "0"}]}
        try:
            service.check_budget(curve["curve_id"], bad_domain)
            raise AssertionError("查询域越出报价域必须被拒绝")
        except ValidationError:
            pass
        # 超过 30 档
        many = [{"lo": i, "hi": i, "rebate": "0"} for i in range(1, 32)]
        try:
            service.check_budget(curve["curve_id"],
                                 {"domain": [1, 31], "budget": "1", "tiers": many})
            raise AssertionError("超过 30 档必须被拒绝")
        except ValidationError:
            pass
        # 负预算 / 负回扣
        try:
            service.check_budget(curve["curve_id"],
                                 {"domain": [1, 8], "budget": "-1",
                                  "tiers": [{"lo": 1, "hi": 8, "rebate": "0"}]})
            raise AssertionError("负预算必须被拒绝")
        except ValidationError:
            pass
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_main_worked_example_three_versions,
        test_partial_consistency_cycle,
        test_multi_price_cycle,
        test_fully_contradictory_cycle,
        test_negative_price_segment,
        test_reopen_persistence,
        test_huge_domain_no_enumeration,
        test_validation_and_tier_guardrails,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n报价/预算全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
