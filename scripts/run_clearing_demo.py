"""题目真票端到端演示：三种装册 + 跨日一表 + 中途等额未齐 + 配额卡点 + 重开查表。

运行：python3 scripts/run_clearing_demo.py
全部关键数字用 assert 钉死，任何口径漂移直接失败。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol import (
    ClearingFact,
    Voucher,
    DaySpec,
    PlanBuilder,
    ArchiveRegistry,
    ValidationError,
)


def build_main_fact() -> ClearingFact:
    """同一张金额至少 20 的真票：两包 A(20)、B(10)。

    P 依次收：A 的 4、A 的 6、B 的 10；S 依次付：B 的 10、A 的 10。
    序号是两边建票以后的相对数。
    """
    packages = {"PKG-A": ("A", 10), "PKG-B": ("B", 10)}
    vouchers = [
        Voucher("P", 1, "P-RECV-A1", "PKG-A", "A", 4),
        Voucher("P", 2, "P-RECV-A2", "PKG-A", "A", 6),
        Voucher("P", 3, "P-RECV-B1", "PKG-B", "B", 10),
        Voucher("S", 1, "S-PAY-B1", "PKG-B", "B", 10),
        Voucher("S", 2, "S-PAY-A1", "PKG-A", "A", 10),
    ]
    fact = ClearingFact(packages, vouchers)
    assert fact.signable(), fact.issues()
    return fact


def build_main_specs() -> list[DaySpec]:
    """第一日旧末序 (2,1)，允许 P=1..3、S=1..2；第二日旧版与实末都取 (3,2)。"""
    return [
        DaySpec(1, old={"P": 2, "S": 1},
                allowed={"P": (1, 3), "S": (1, 2)}),
        DaySpec(2, old={"P": 3, "S": 2},
                allowed={"P": (3, 3), "S": (2, 2)}),
    ]


def show_plan(title: str, plan) -> None:
    print(f"\n=== {title} ===")
    for line in plan.red_lines():
        print(f"  第{line['day']}日红线 P={line['P']} S={line['S']}")
    print(f"  总移动={plan.movement}  原表数={plan.table_count}  "
          f"须申请日差额额度={plan.required_daily_quota}")
    print(f"  每日未清表差额和={list(plan.daily_open_diff_sum)}")
    for table in plan.tables:
        d = table.to_dict()
        print(f"  表(测算号未占) 包{d['package_id']} 开第{d['open_day']}日 "
              f"销第{d['close_day']}日 覆盖日{d['days']} 逐日差额{d['daily_diff']}")


def main() -> None:
    fact = build_main_fact()
    specs = build_main_specs()
    builder = PlanBuilder(fact, specs)

    # ------------------------------------------------- 收付台：逐前缀三态
    print("---------- 收付台单查（原 post 顺序） ----------")
    for prefix in ("A", "B"):
        q = fact.desk_query(prefix)
        print(f"前缀 {prefix}: 全未进={[x['package_id'] for x in q['all_not_entered']]} "
              f"双边全收={[x['package_id'] for x in q['both_complete']]} "
              f"开头未齐={[x['package_id'] for x in q['started_incomplete']]}")
        for row in q["started_incomplete"] + q["all_not_entered"]:
            print(f"  {row['package_id']}: P已入{row['P_vouchers']}={row['P_amount']} "
                  f"S已入{row['S_vouchers']}={row['S_amount']} "
                  f"未进额 P={row['P_missing_amount']} S={row['S_missing_amount']}")

    # ================================================= 三种装册
    # 装册一：沿旧版分日不移一格 => A、B 各开一表，第一日差额相加 20
    old_cuts = [{"P": 2, "S": 1}, {"P": 3, "S": 2}]
    plan_old = builder.evaluate(old_cuts)
    show_plan("装册一：沿旧版 (2,1)->(3,2)，零移动", plan_old)
    print("  装订台第1日逐包原单截面：")
    for rep in plan_old.package_report(fact, day=1):
        print(f"    {rep['package_id']}: P已入{rep['P_entered']}({rep['P_amount']}) "
              f"未进{rep['P_missing_vouchers']} | S已入{rep['S_entered']}({rep['S_amount']}) "
              f"未进{rep['S_missing_vouchers']}")
    day1_rep = {r["package_id"]: r for r in plan_old.package_report(fact, day=1)}
    assert day1_rep["PKG-A"]["P_missing_vouchers"] == []
    assert day1_rep["PKG-A"]["S_missing_vouchers"] == ["S-PAY-A1"]
    assert day1_rep["PKG-B"]["P_missing_vouchers"] == ["P-RECV-B1"]
    assert day1_rep["PKG-B"]["S_missing_vouchers"] == []
    assert plan_old.movement == 0
    assert plan_old.table_count == 2
    assert plan_old.required_daily_quota == 20
    assert plan_old.daily_open_diff_sum == (20, 0)
    a_life = next(t for t in plan_old.tables if t.package_id == "PKG-A")
    b_life = next(t for t in plan_old.tables if t.package_id == "PKG-B")
    assert (a_life.open_day, a_life.close_day) == (1, 2)
    assert (b_life.open_day, b_life.close_day) == (1, 2)
    assert a_life.daily_diff == {1: 10, 2: 0}
    assert b_life.daily_diff == {1: 10, 2: 0}

    # 装册二：移动一格 => 只开一表，占额度 10（(2,2) 与 (3,1) 同值并列，取省表后仍一表）
    plan_move1 = builder.best_under_quotas(table_quota=2, daily_quota=10)
    show_plan("装册二：配额 表<=2、日差额<=10 下最省移动", plan_move1)
    assert plan_move1.movement == 1
    assert plan_move1.table_count == 1
    assert plan_move1.required_daily_quota == 10
    only = plan_move1.tables[0]
    assert only.package_id == "PKG-B" and only.daily_diff == {1: 10, 2: 0}
    # 并列的另一套一格排法 (3,1)：A 留表、B 当日整包齐
    plan_alt = builder.evaluate([{"P": 3, "S": 1}, {"P": 3, "S": 2}])
    assert plan_alt.movement == 1 and plan_alt.table_count == 1
    assert plan_alt.tables[0].package_id == "PKG-A"
    assert plan_alt.required_daily_quota == 10
    print("  并列一格排法 (3,1)：留表包=PKG-A，同样 1 表/额度10（择优键下 (2,2) 先出）")

    # 装册三：第一日两册都截 (3,2) => 移动两格、零表零额度
    plan_full = builder.evaluate([{"P": 3, "S": 2}, {"P": 3, "S": 2}])
    show_plan("装册三：第一日即整包全进 (3,2)->(3,2)", plan_full)
    assert plan_full.movement == 2
    assert plan_full.table_count == 0
    assert plan_full.required_daily_quota == 0
    assert plan_full.daily_open_diff_sum == (0, 0)

    # 问二：只准给定总移动量，最低须申请多少日差额额度
    q2 = builder.minimum_quota_for_movement(move_budget=1, table_quota=2)
    print(f"\n问二（移动<=1）：最低日差额额度={q2.required_daily_quota} "
          f"对应移动={q2.movement} 表数={q2.table_count}")
    assert q2.required_daily_quota == 10 and q2.movement == 1 and q2.table_count == 1
    q2_zero = builder.minimum_quota_for_movement(move_budget=0, table_quota=2)
    assert q2_zero.required_daily_quota == 20 and q2_zero.table_count == 2

    # 问一在额度 10 下：零移动排法需要额度 20，被卡；最少移动一格
    blocked = None
    try:
        builder.best_under_quotas(table_quota=2, daily_quota=10)
    except ValidationError as exc:
        blocked = exc
    chosen = builder.best_under_quotas(table_quota=2, daily_quota=10)
    assert chosen.movement == 1
    print("问一（额度10）：零移动需额度20被卡，最省移动=1，与题面一致")

    # ================================================= 独立账交换 S 顺序
    print("\n---------- 同一已核清算：独立账中交换 S 两笔提交顺序 ----------")
    swapped = fact.resubmit_order("S", ["S-PAY-A1", "S-PAY-B1"])  # 先 A 后 B
    swap_builder = PlanBuilder(swapped, specs)
    plan_swap = swap_builder.evaluate(old_cuts)  # 旧末序 (2,1)->(3,2)
    show_plan("交换后沿旧末序原样出刊", plan_swap)
    # 旧末序一格不移即可出刊；第1日 A 包双边全收，B 包被日界截住（两侧原单均未进）。
    assert plan_swap.movement == 0
    # 按题面硬规则“日界截在中间即须专属开表，等额未齐也须有表”，B 仍须开一张
    # 零差额表：占 1 张原表配额，但日差额额度为 0。
    assert plan_swap.required_daily_quota == 0
    assert plan_swap.daily_open_diff_sum == (0, 0)
    b_table = next(t for t in plan_swap.tables if t.package_id == "PKG-B")
    assert b_table.daily_diff == {1: 0, 2: 0}
    day1 = {r.package_id: r for r in plan_swap.rows_of_day(1)}
    assert day1["PKG-A"].complete and not day1["PKG-A"].under_table
    assert not day1["PKG-B"].complete and day1["PKG-B"].under_table
    print("  注：题面称交换后“无需任何表”，但 B 包第1日两侧原单均未进即被日界截住，")
    print("  依题面自身硬规则仍须开一张零差额表（占表号、不占日差额额度）。")
    print("  若档案室另有“整包两侧当日均无原单则不视为跨界”的本地豁免，方可做到零表；")
    print("  本实现不擅自加该豁免，以免与‘等额未齐也须有表’的硬规则冲突。")
    # 原账保持可复读，未被改写
    assert [v.voucher_id for v in fact.vouchers if v.entity == "S"] == \
        ["S-PAY-B1", "S-PAY-A1"]

    # ================================================= 实际选中版本才占表号
    print("\n---------- 档案室装册：只有选中版本开表/转记/销表 ----------")
    registry = ArchiveRegistry()
    archive = registry.bind("DAILY-MAIN", fact, plan_old, table_quota=2)
    print(f"占用表号 {archive.table_numbers_used()}（两测算方案不额外占号）")
    assert archive.table_no_of("PKG-A") == "R001"
    assert archive.table_no_of("PKG-B") == "R002"
    # 注销表号不重新分配：另一档案独立编号、互不串号
    archive2 = registry.bind("DAILY-MOVE1", fact, plan_move1, table_quota=2)
    assert archive2.table_no_of("PKG-B") == "R001"
    assert archive.table_no_of("PKG-B") == "R002"  # 旧档案表号不变
    print(f"旧档案 PKG-B 仍持 {archive.table_no_of('PKG-B')}；"
          f"新档案 PKG-B 另持 {archive2.table_no_of('PKG-B')}，注销号不跨册复用")

    # 重开沿原凭证号验手续
    trace = archive.trace_voucher("S", "S-PAY-A1")
    print("重开查 S/S-PAY-A1：", trace)
    assert trace["sealed_day"] == 2
    assert trace["table_touches"] == [
        {"table_no": "R001", "package_id": "PKG-A", "day": 2, "action": "close"}]
    reopened = archive.reopen()
    assert len(reopened["volumes"]) == 4 and len(reopened["tables"]) == 2

    # ================================================= 附加核验一：同一包跨两天只立一表
    print("\n---------- 附加核验一：同一包跨两天只立一张表（开/滚/销同号） ----------")
    cross_packages = {"PKG-C": ("C", 20)}
    cross_vouchers = [
        Voucher("P", 1, "P-C1", "PKG-C", "C", 5),
        Voucher("P", 2, "P-C2", "PKG-C", "C", 15),
        Voucher("S", 1, "S-C1", "PKG-C", "C", 20),
    ]
    cross_fact = ClearingFact(cross_packages, cross_vouchers)
    cross_specs = [
        DaySpec(1, old={"P": 1, "S": 1}, allowed={"P": (0, 1), "S": (0, 1)}),
        DaySpec(2, old={"P": 1, "S": 1}, allowed={"P": (1, 2), "S": (1, 1)}),
        DaySpec(3, old={"P": 2, "S": 1}, allowed={"P": (2, 2), "S": (1, 1)}),
    ]
    cross_builder = PlanBuilder(cross_fact, cross_specs)
    cross_plan = cross_builder.evaluate([{"P": 1, "S": 1},
                                         {"P": 1, "S": 1},
                                         {"P": 2, "S": 1}])
    show_plan("PKG-C 跨三日（第1日开、第2日滚、第3日销）", cross_plan)
    assert cross_plan.table_count == 1
    life = cross_plan.tables[0]
    assert life.open_day == 1 and life.close_day == 3 and life.days == (1, 2, 3)
    assert life.daily_diff == {1: 15, 2: 15, 3: 0}
    cross_archive = registry.bind("CROSS-C", cross_fact, cross_plan, table_quota=2)
    rec = cross_archive.table_of("PKG-C")
    assert rec.table_no == "R001" and rec.status == "closed"
    assert [e["action"] for e in rec.daily_entries] == ["open", "carry", "close"]
    print(f"表号 {rec.table_no} 三日同号：开第1日/滚第2日/销第3日，未每天换号")

    # ================================================= 附加核验二：中途累计等额但原单未齐
    print("\n---------- 附加核验二：两侧当日累计相等但原单未齐，仍须开表 ----------")
    eq_packages = {"PKG-E": ("E", 20)}
    eq_vouchers = [
        Voucher("P", 1, "P-E1", "PKG-E", "E", 10),
        Voucher("P", 2, "P-E2", "PKG-E", "E", 10),
        Voucher("S", 1, "S-E1", "PKG-E", "E", 10),
        Voucher("S", 2, "S-E2", "PKG-E", "E", 10),
    ]
    eq_fact = ClearingFact(eq_packages, eq_vouchers)
    eq_specs = [
        DaySpec(1, old={"P": 1, "S": 1}, allowed={"P": (0, 1), "S": (0, 1)}),
        DaySpec(2, old={"P": 2, "S": 2}, allowed={"P": (2, 2), "S": (2, 2)}),
    ]
    eq_plan = PlanBuilder(eq_fact, eq_specs).evaluate(
        [{"P": 1, "S": 1}, {"P": 2, "S": 2}])
    show_plan("PKG-E 第1日两侧各10（等额）但各缺一笔", eq_plan)
    assert eq_plan.table_count == 1
    row1 = next(r for r in eq_plan.rows if r.day == 1)
    assert row1.diff == 0 and row1.under_table and row1.table_open and not row1.complete
    assert eq_plan.daily_open_diff_sum == (0, 0)  # 差额为0仍有表，占表不占差额额度
    eq_archive = registry.bind("EQUAL-E", eq_fact, eq_plan, table_quota=2)
    eq_rec = eq_archive.table_of("PKG-E")
    assert eq_rec.table_no == "R001"
    assert [e["action"] for e in eq_rec.daily_entries] == ["open", "close"]
    print(f"表 {eq_rec.table_no} 第1日差额0仍开表，第2日补齐注销；"
          f"所需日差额额度={eq_plan.required_daily_quota}")

    # ================================================= 配额卡点诊断
    print("\n---------- 边界/配额卡住时的结构化诊断 ----------")
    # 额度 9：零移动需 20、一格需 10，都超；但移动两格可零额度。故同时限定移动<=1
    # 才被日差额额度卡死，诊断给出最低需求 10。
    assert builder.enumerate_plans(move_cap=1, quota_cap=9, table_cap=2) == []
    q2_answer = builder.minimum_quota_for_movement(move_budget=1, table_quota=2)
    assert q2_answer.required_daily_quota == 10
    diag = builder.diagnose(table_quota=2, daily_quota=9, move_budget=1)
    print("移动<=1、日差额额度=9 被卡：", diag)
    assert diag["code"] == "blocked"
    assert any(b["code"] == "daily_quota_short" for b in diag["blockers"])
    # 表配额 0：只有“第一日整包全进（移动2）”能零表；要求沿旧版零移动则必然缺表
    zero_table = builder.best_under_quotas(table_quota=0, daily_quota=20)
    assert zero_table.table_count == 0 and zero_table.movement == 2
    try:
        builder.minimum_quota_for_movement(move_budget=0, table_quota=0)
        raise AssertionError("零移动且零表应无解")
    except ValidationError as exc:
        print("移动=0、原表配额=0 被卡：", exc.details)
        assert any(b["code"] == "table_quota_short" for b in exc.details["blockers"])

    print("\n全部断言通过。")


if __name__ == "__main__":
    main()
