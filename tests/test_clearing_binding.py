"""清算包/跨日调节表/日结装册单元测试。

覆盖：
- 事实层：跨票归属、凭证重复、缺侧、最终未平、同额不同包不得凑平、三态查询；
- 装册层：末序不减/越界/末日固定、表生命周期（开/滚/销同号）、零差额仍开表、
  两套择优口径、配额卡点诊断、表号不复用、重开沿凭证号追溯、独立账重排不改原账。
"""

import unittest

from group_consol import (
    ClearingFact,
    Voucher,
    DaySpec,
    PlanBuilder,
    ArchiveRegistry,
    ValidationError,
    ConflictError,
)


def fact_ab() -> ClearingFact:
    return ClearingFact(
        {"PKG-A": ("A", 10), "PKG-B": ("B", 10)},
        [
            Voucher("P", 1, "p-a1", "PKG-A", "A", 4),
            Voucher("P", 2, "p-a2", "PKG-A", "A", 6),
            Voucher("P", 3, "p-b1", "PKG-B", "B", 10),
            Voucher("S", 1, "s-b1", "PKG-B", "B", 10),
            Voucher("S", 2, "s-a1", "PKG-A", "A", 10),
        ],
    )


def specs_ab() -> list[DaySpec]:
    return [
        DaySpec(1, old={"P": 2, "S": 1}, allowed={"P": (1, 3), "S": (1, 2)}),
        DaySpec(2, old={"P": 3, "S": 2}, allowed={"P": (3, 3), "S": (2, 2)}),
    ]


class ClearingFactTests(unittest.TestCase):
    def test_signable_fact(self):
        self.assertTrue(fact_ab().signable())

    def test_cross_invoice_rejected(self):
        # 凭证前缀 A 却归入专属 B 的包
        bad = [Voucher("P", 1, "p-x", "PKG-B", "A", 10),
               Voucher("S", 1, "s-b", "PKG-B", "B", 10)]
        with self.assertRaises(ValidationError) as cm:
            ClearingFact({"PKG-B": ("B", 10)}, bad)
        self.assertEqual(cm.exception.details["code"], "cross_invoice")

    def test_duplicate_voucher_rejected(self):
        bad = [
            Voucher("P", 1, "dup", "PKG-A", "A", 5),
            Voucher("P", 2, "dup", "PKG-A", "A", 5),
            Voucher("S", 1, "s", "PKG-A", "A", 10),
        ]
        with self.assertRaises(ValidationError) as cm:
            ClearingFact({"PKG-A": ("A", 10)}, bad)
        self.assertEqual(cm.exception.details["code"], "duplicate_voucher")

    def test_missing_side_and_not_balanced(self):
        # 只建 P 侧 => 缺 S；金额不足 => 未平
        fact = ClearingFact(
            {"PKG-A": ("A", 10)},
            [Voucher("P", 1, "p", "PKG-A", "A", 7)])
        self.assertFalse(fact.signable())
        codes = {i["code"] for i in fact.issues()}
        self.assertIn("missing_side", codes)
        self.assertIn("not_balanced", codes)
        with self.assertRaises(ValidationError):
            fact.assert_signable()

    def test_equal_totals_different_packages_not_offset(self):
        # 两包各单边 10，总额相等但都缺侧，不能互相凑平
        fact = ClearingFact(
            {"PKG-A": ("A", 10), "PKG-B": ("B", 10)},
            [Voucher("P", 1, "p-a", "PKG-A", "A", 10),
             Voucher("S", 1, "s-b", "PKG-B", "B", 10)])
        self.assertEqual(len(fact.issues()), 2)
        self.assertTrue(all(i["code"] == "missing_side" for i in fact.issues()))

    def test_desk_query_three_states(self):
        fact = ClearingFact(
            {"PKG-A": ("A", 10), "PKG-B": ("B", 10)},
            [Voucher("P", 1, "p-a", "PKG-A", "A", 10),
             Voucher("S", 1, "s-a", "PKG-A", "A", 10),
             Voucher("P", 2, "p-b", "PKG-B", "B", 4)])
        q_a, q_b = fact.desk_query("A"), fact.desk_query("B")
        self.assertEqual([x["package_id"] for x in q_a["both_complete"]], ["PKG-A"])
        self.assertEqual([x["package_id"] for x in q_b["started_incomplete"]], ["PKG-B"])
        empty = ClearingFact({"PKG-X": ("X", 10)}, [])
        self.assertEqual([x["package_id"] for x in empty.desk_query("X")["all_not_entered"]],
                         ["PKG-X"])


class BindingPlanTests(unittest.TestCase):
    def setUp(self):
        self.fact = fact_ab()
        self.builder = PlanBuilder(self.fact, specs_ab())

    def test_three_bindings_match_spec_numbers(self):
        old = self.builder.evaluate([{"P": 2, "S": 1}, {"P": 3, "S": 2}])
        self.assertEqual((old.movement, old.table_count, old.required_daily_quota),
                         (0, 2, 20))
        one = self.builder.best_under_quotas(table_quota=2, daily_quota=10)
        self.assertEqual((one.movement, one.table_count, one.required_daily_quota),
                         (1, 1, 10))
        full = self.builder.evaluate([{"P": 3, "S": 2}, {"P": 3, "S": 2}])
        self.assertEqual((full.movement, full.table_count, full.required_daily_quota),
                         (2, 0, 0))

    def test_q2_minimum_quota_under_move_budget(self):
        ans = self.builder.minimum_quota_for_movement(move_budget=1, table_quota=2)
        self.assertEqual((ans.required_daily_quota, ans.movement, ans.table_count),
                         (10, 1, 1))
        zero = self.builder.minimum_quota_for_movement(move_budget=0, table_quota=2)
        self.assertEqual((zero.required_daily_quota, zero.table_count), (20, 2))

    def test_cut_must_be_monotone_and_in_range(self):
        with self.assertRaises(ValidationError):
            self.builder.evaluate([{"P": 3, "S": 2}, {"P": 2, "S": 2}])  # 回落
        with self.assertRaises(ValidationError):
            self.builder.evaluate([{"P": 0, "S": 1}, {"P": 3, "S": 2}])  # 越下界

    def test_final_day_must_reach_heads(self):
        bad_specs = [
            DaySpec(1, old={"P": 2, "S": 1}, allowed={"P": (1, 3), "S": (1, 2)}),
            DaySpec(2, old={"P": 3, "S": 2}, allowed={"P": (2, 3), "S": (2, 2)}),
        ]
        with self.assertRaises(ValidationError):
            PlanBuilder(self.fact, bad_specs)

    def test_quota_blockers_are_structured(self):
        # 联合移动预算后，额度 9 不够（移动<=1 的最低额度是 10），必须结构化报出
        diag = self.builder.diagnose(table_quota=2, daily_quota=9, move_budget=1)
        codes = {b["code"] for b in diag["blockers"]}
        self.assertIn("daily_quota_short", codes)
        # 零移动 + 零表无解：表配额短缺
        with self.assertRaises(ValidationError):
            self.builder.minimum_quota_for_movement(move_budget=0, table_quota=0)

    def test_resubmit_order_keeps_original_ledger(self):
        swapped = self.fact.resubmit_order("S", ["s-a1", "s-b1"])
        # 原账顺序不变
        self.assertEqual([v.voucher_id for v in self.fact.vouchers if v.entity == "S"],
                         ["s-b1", "s-a1"])
        plan = PlanBuilder(swapped, specs_ab()).evaluate(
            [{"P": 2, "S": 1}, {"P": 3, "S": 2}])
        self.assertEqual(plan.movement, 0)
        self.assertEqual(plan.required_daily_quota, 0)
        # B 包第1日两侧原单均未进，仍按硬规则开零差额表
        b = next(t for t in plan.tables if t.package_id == "PKG-B")
        self.assertEqual(b.daily_diff, {1: 0, 2: 0})

    def test_resubmit_rejects_adding_or_dropping(self):
        with self.assertRaises(ValidationError):
            self.fact.resubmit_order("S", ["s-a1"])  # 漏一笔


class TableLifecycleTests(unittest.TestCase):
    def _cross(self):
        fact = ClearingFact(
            {"PKG-C": ("C", 20)},
            [Voucher("P", 1, "c1", "PKG-C", "C", 5),
             Voucher("P", 2, "c2", "PKG-C", "C", 15),
             Voucher("S", 1, "sc1", "PKG-C", "C", 20)])
        specs = [
            DaySpec(1, old={"P": 1, "S": 1}, allowed={"P": (0, 1), "S": (0, 1)}),
            DaySpec(2, old={"P": 1, "S": 1}, allowed={"P": (1, 2), "S": (1, 1)}),
            DaySpec(3, old={"P": 2, "S": 1}, allowed={"P": (2, 2), "S": (1, 1)}),
        ]
        return fact, specs

    def test_one_table_across_multiple_days_same_number(self):
        fact, specs = self._cross()
        plan = PlanBuilder(fact, specs).evaluate(
            [{"P": 1, "S": 1}, {"P": 1, "S": 1}, {"P": 2, "S": 1}])
        self.assertEqual(plan.table_count, 1)
        life = plan.tables[0]
        self.assertEqual((life.open_day, life.close_day, life.days),
                         (1, 3, (1, 2, 3)))
        archive = ArchiveRegistry().bind("ARC", fact, plan, table_quota=2)
        rec = archive.table_of("PKG-C")
        self.assertEqual(rec.table_no, "R001")
        self.assertEqual([e["action"] for e in rec.daily_entries],
                         ["open", "carry", "close"])

    def test_equal_cumulative_but_incomplete_still_opens_table(self):
        fact = ClearingFact(
            {"PKG-E": ("E", 20)},
            [Voucher("P", 1, "e1", "PKG-E", "E", 10),
             Voucher("P", 2, "e2", "PKG-E", "E", 10),
             Voucher("S", 1, "se1", "PKG-E", "E", 10),
             Voucher("S", 2, "se2", "PKG-E", "E", 10)])
        specs = [
            DaySpec(1, old={"P": 1, "S": 1}, allowed={"P": (0, 1), "S": (0, 1)}),
            DaySpec(2, old={"P": 2, "S": 2}, allowed={"P": (2, 2), "S": (2, 2)}),
        ]
        plan = PlanBuilder(fact, specs).evaluate([{"P": 1, "S": 1}, {"P": 2, "S": 2}])
        row1 = next(r for r in plan.rows if r.day == 1)
        self.assertEqual(row1.diff, 0)
        self.assertTrue(row1.table_open and not row1.complete)
        self.assertEqual(plan.required_daily_quota, 0)
        self.assertEqual(plan.table_count, 1)

    def test_table_numbers_not_reused_and_archive_reopen(self):
        fact, specs = self._cross()
        plan = PlanBuilder(fact, specs).evaluate(
            [{"P": 1, "S": 1}, {"P": 1, "S": 1}, {"P": 2, "S": 1}])
        registry = ArchiveRegistry()
        a1 = registry.bind("A1", fact, plan, table_quota=2)
        a2 = registry.bind("A2", fact, plan, table_quota=2)
        self.assertEqual(a1.table_no_of("PKG-C"), "R001")
        self.assertEqual(a2.table_no_of("PKG-C"), "R001")  # 各档案独立号簿
        # 同档案号异文拒绝
        with self.assertRaises(ConflictError):
            other = PlanBuilder(fact_ab(), specs_ab()).evaluate(
                [{"P": 2, "S": 1}, {"P": 3, "S": 2}])
            registry.bind("A1", fact_ab(), other, table_quota=2)
        reopened = a1.reopen()
        self.assertEqual(len(reopened["volumes"]), 6)  # 3日 x 2册
        trace = a1.trace_voucher("P", "c2")
        self.assertEqual(trace["sealed_day"], 3)
        self.assertEqual(trace["table_touches"][0]["action"], "close")

    def test_table_quota_exceeded_rejected(self):
        fact, specs = self._cross()
        plan = PlanBuilder(fact, specs).evaluate(
            [{"P": 1, "S": 1}, {"P": 1, "S": 1}, {"P": 2, "S": 1}])
        with self.assertRaises(ValidationError) as cm:
            ArchiveRegistry().bind("ARC", fact, plan, table_quota=0)
        self.assertEqual(cm.exception.details["code"], "table_quota_short")


if __name__ == "__main__":
    unittest.main()
