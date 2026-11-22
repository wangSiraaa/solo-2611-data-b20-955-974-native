"""海损理算与码头提货的单元测试（合成口径）。

覆盖：
- 件段运算（合并/差集/子集）；
- 交接单按 (time, seq) 生效且对到档顺序不敏感；重叠或部分无权整单退回；
- 发票双侧核对：缺侧/异文待核；
- 共同海损：认可/不认可、交接变化、船值变化三态；
- 提货最高优先分、同分多方案、现金上限与联保函总额；
- 两户共用一张函、两份草案不可相加；跨草案结算被拒；
- 重复放货、跨货主箱、含已损箱被拒；
- 重开数据库后状态与已发布结算仍在；
- 百万件规模只用连续段，不逐件展开。
"""

import os
import tempfile
import unittest

from group_consol.ledger import EntityLedger
from group_consol.general_average import VoyageDesk, ReleaseError, MAX_DRAFTS
from group_consol.ga_seed import seed_invoice
from group_consol.segs import normalize, subtract, intersect, is_subset, count


INV = dict(invoice_id="i1", seller="P", buyer="S", sku="X", q=4, T=80, C=40)


def two_boxes():
    return [
        {"box_id": "b1", "segments": [[1, 1]], "priority": 1, "net_value": 5},
        {"box_id": "b2", "segments": [[2, 2]], "priority": 1, "net_value": 5},
    ]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.tmp.close()
        self.path = self.tmp.name
        self.ledger = EntityLedger(self.path)
        self.desk = VoyageDesk(self.ledger)

    def tearDown(self):
        self.ledger.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def seed(self, inv=None, p="s-i1", s="b-i1"):
        return seed_invoice(self.ledger, inv or INV, p_id=p, s_id=s)


class SegTests(unittest.TestCase):
    def test_ops(self):
        self.assertEqual(normalize([(3, 4), (1, 2)]), [(1, 4)])
        self.assertEqual(subtract([(1, 5)], [(2, 3)]), [(1, 1), (4, 5)])
        self.assertEqual(intersect([(1, 5)], [(3, 9)]), [(3, 5)])
        self.assertTrue(is_subset([(2, 3)], [(1, 5)]))
        self.assertFalse(is_subset([(2, 6)], [(1, 5)]))
        self.assertEqual(count([(1, 1000000)]), 1000000)


class InvoiceCheckTests(Base):
    def test_missing_side_pending(self):
        self.ledger.post(
            "s-only", "P",
            [["receivable", 80, 0], ["revenue", 0, 80], ["cost", 40, 0], ["inventory", 0, 40]],
            {"type": "internal_sale", "side": "seller", "invoice_id": "i1",
             "seller": "P", "buyer": "S", "sku": "X", "q": 4, "T": 80, "C": 40},
        )
        from group_consol import PendingError
        with self.assertRaises(PendingError) as ctx:
            self.desk.register_invoice({"P": 1, "S": 0})
        types = [i["issue_type"] for i in ctx.exception.details["issues"]]
        self.assertIn("missing_side", types)

    def test_mismatch_pending(self):
        self.ledger.post(
            "s1", "P",
            [["receivable", 80, 0], ["revenue", 0, 80], ["cost", 40, 0], ["inventory", 0, 40]],
            {"type": "internal_sale", "side": "seller", "invoice_id": "i1",
             "seller": "P", "buyer": "S", "sku": "X", "q": 4, "T": 80, "C": 40},
        )
        # S 镜像 sku 不同
        self.ledger.post(
            "b1", "S", [["inventory", 80, 0], ["payable", 0, 80]],
            {"type": "internal_sale", "side": "buyer", "invoice_id": "i1",
             "seller": "P", "buyer": "S", "sku": "Y", "q": 4, "T": 80, "C": 40},
        )
        from group_consol import PendingError
        with self.assertRaises(PendingError) as ctx:
            self.desk.register_invoice({"P": 1, "S": 1})
        issues = ctx.exception.details["issues"]
        self.assertTrue(any("sku" in i["details"].get("fields", {}) for i in issues))


class HandoverTests(Base):
    def setUp(self):
        super().setUp()
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])

    def test_order_independent(self):
        self.desk.add_handover("v", "second", 2, 1, "i1", [[3, 4]], "P", "S")
        self.desk.add_handover("v", "first", 1, 1, "i1", [[1, 2]], "P", "S")
        held, rej = self.desk.holdings("v")
        self.assertEqual(rej, [])
        self.assertEqual(held["i1"]["S"], [(1, 4)])

    def test_overlap_rejected_whole(self):
        self.desk.add_handover("v", "ov", 1, 1, "i1", [[1, 3], [2, 4]], "P", "S")
        held, rej = self.desk.holdings("v")
        self.assertEqual(len(rej), 1)
        self.assertEqual(held["i1"]["P"], [(1, 4)])  # 整单退回，未转移

    def test_partial_authority_rejected(self):
        self.desk.add_handover("v", "hand", 1, 1, "i1", [[1, 2]], "P", "S")
        # S 只持 1..2，却想交出 2..3 -> 整单退回
        self.desk.add_handover("v", "bad", 2, 1, "i1", [[2, 3]], "S", "P")
        held, rej = self.desk.holdings("v")
        self.assertEqual(len(rej), 1)
        self.assertEqual(held["i1"]["S"], [(1, 2)])

    def test_multi_segment_single_transfer(self):
        self.desk.add_handover("v", "multi", 1, 1, "i1", [[1, 2], [4, 4]], "P", "S")
        held, rej = self.desk.holdings("v")
        self.assertEqual(rej, [])
        self.assertEqual(held["i1"]["S"], [(1, 2), (4, 4)])


class ScenarioTests(Base):
    def _scenario1(self, ship=60, hand4=False):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", ship, ["i1"])
        self.desk.add_handover("v", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
        if hand4:
            self.desk.add_handover("v", "h4", 2, 1, "i1", [[4, 4]], "P", "S")
        self.desk.register_draft("d-rec", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        self.desk.register_draft("d-none", "v", {"i1": [[4, 4]]}, {"i1": []})
        self.desk.create_guarantee("G", 2, ["S"])
        self.desk.create_claim("c", "v", "S", two_boxes(), cash_cap=2, guarantee_id="G")

    def test_s1_nets_and_priority(self):
        self._scenario1()
        self.assertEqual(self.desk.compute_ga("d-rec")["net"]["S"], 2)
        self.assertEqual(self.desk.compute_ga("d-none")["net"]["S"], 0)
        ev = self.desk.evaluate_release("c", "d-rec")
        self.assertEqual(ev["max_priority"], 2)
        variants = {(o["cash_frozen"], o["guarantee_draw"]) for o in ev["options"]}
        self.assertIn((2, 0), variants)  # 全冻
        self.assertIn((0, 2), variants)  # 全用函

    def test_s1_settle_unrecognized_refunds_cash(self):
        self._scenario1()
        self.desk.release("c", "d-rec", keep=[], cash_frozen=2, guarantee_draw=0)
        st = self.desk.settle("c", "d-none")
        self.assertEqual(st["applicant_net"], 0)
        self.assertEqual(st["cash_deducted"], 0)
        self.assertEqual(st["cash_refund"], 2)

    def test_s1_settle_unrecognized_no_cash_when_guarantee_used(self):
        self._scenario1()
        self.desk.release("c", "d-rec", keep=[], cash_frozen=0, guarantee_draw=2)
        st = self.desk.settle("c", "d-none")
        self.assertEqual(st["cash_frozen"], 0)
        self.assertEqual(st["guarantee_claim"], 0)

    def test_s2_handover4_creditor_release_free(self):
        self._scenario1(hand4=True)
        self.assertEqual(self.desk.compute_ga("d-rec")["net"]["S"], -7)
        ev = self.desk.evaluate_release("c", "d-rec")
        self.assertEqual(ev["max_priority"], 2)
        top = ev["options"][0]
        self.assertEqual((top["cash_frozen"], top["guarantee_draw"]), (0, 0))

    def test_s3_ship_zero_must_keep_one(self):
        self._scenario1(ship=0)
        self.assertEqual(self.desk.compute_ga("d-rec")["net"]["S"], 5)
        ev = self.desk.evaluate_release("c", "d-rec")
        self.assertEqual(ev["max_priority"], 1)
        for o in ev["options"]:
            self.assertEqual(len(o["keep"]), 1)
            self.assertEqual(o["cash_frozen"], 0)
            self.assertEqual(o["guarantee_draw"], 0)

    def test_s3_without_keep_infeasible(self):
        self._scenario1(ship=0)
        # 不放留箱、只靠现金 2 + 函 2 = 4 < 5 -> 不可行
        ev = self.desk.evaluate_release("c", "d-rec")
        self.assertFalse(any(o["keep"] == [] for o in ev["all_options"]))


    def test_incremental_release_must_reunderwrite(self):
        # 船值 0：S 欠 5；先留一箱、放一箱；再想放走旧保箱时须重新核够，不足则不得放
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 0, ["i1"])
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 2]], "P", "S")
        self.desk.register_draft("d", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        self.desk.create_guarantee("G", 2, ["S"])
        self.desk.create_claim("c", "v", "S", two_boxes(), cash_cap=2, guarantee_id="G")
        first = self.desk.release("c", "d")
        self.assertEqual(first["priority"], 1)
        ev = self.desk.evaluate_release("c", "d")
        # 只剩一箱可动，且需 5 > 现金 2 + 函 2 = 4，放不掉，最高增量 0
        self.assertEqual(ev["max_priority"], 0)
        self.assertFalse(any(o["release"] for o in ev["options"]))


class TableTests(Base):
    def test_invoice_bad_good_pending(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])

    def test_box_cross_owner_rejected(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 2]], "P", "S")
        # S 提 2..3，其中 3 仍归 P -> 跨货主
        with self.assertRaises(ReleaseError):
            self.desk.create_claim("c", "v", "S",
                                   [{"box_id": "x", "segments": [[2, 3]], "priority": 1,
                                     "net_value": 5}], 0)

    def test_box_with_sacrificed_cargo_rejected(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 2]], "P", "S")
        self.desk.register_draft("d", "v", {"i1": [[1, 1]]}, {"i1": [[1, 1]]})
        with self.assertRaises(ReleaseError):
            self.desk.create_claim("c", "v", "S",
                                   [{"box_id": "x", "segments": [[1, 1]], "priority": 1,
                                     "net_value": 5}], 0)

    def test_duplicate_release_rejected_or_replayed(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 2]], "P", "S")
        self.desk.register_draft("d", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        self.desk.create_guarantee("G", 2, ["S"])
        self.desk.create_claim("c", "v", "S", two_boxes(), 2, "G")
        self.desk.release("c", "d", keep=[], cash_frozen=2)
        # 完全相同复读：按原单复读，不重复收费
        replay = self.desk.release("c", "d", keep=[], cash_frozen=2)
        self.assertEqual(replay["status"], "replayed")
        # 改条件再放：拒绝
        with self.assertRaises(ReleaseError):
            self.desk.release("c", "d", keep=[], cash_frozen=1, guarantee_draw=1)

    def test_millions_of_pieces_use_segments(self):
        big = dict(invoice_id="big", seller="P", buyer="S", sku="B",
                   q=1000000, T=1000000, C=1000000)
        self.desk.register_invoice(self.seed(big, p="s-big", s="b-big"))
        self.desk.create_voyage("v", 0, ["big"])
        self.desk.add_handover("v", "h", 1, 1, "big", [[1, 500000]], "P", "S")
        self.desk.register_draft("d", "v", {"big": [[1, 500000]]}, {"big": [[1, 500000]]})
        ga = self.desk.compute_ga("d")
        # 单位价值 = C/q = 1；S 持有的 500000 件全部牺牲且获认可，获救 0
        self.assertEqual(ga["owed"]["S"], 500000)
        self.assertEqual(ga["salvage"]["S"], 0)
        self.assertEqual(ga["salvage"]["P"], 500000)
        self.assertEqual(ga["pool"], 500000)


class SharedGuaranteeTests(Base):
    def _setup(self):
        inv = dict(invoice_id="i9", seller="P", buyer="S", sku="Z",
                   q=11, T=110, C=110)
        self.desk.register_invoice(self.seed(inv, p="s-i9", s="b-i9"))
        self.desk.create_voyage("v2", 0, ["i9"])
        self.desk.add_handover("v2", "to-A", 1, 1, "i9", [[1, 5]], "P", "A")
        self.desk.add_handover("v2", "to-B", 1, 2, "i9", [[6, 10]], "P", "B")
        self.desk.register_draft("dA", "v2", {"i9": [[1, 1], [6, 6]]}, {"i9": [[1, 1]]})
        self.desk.register_draft("dB", "v2", {"i9": [[1, 1], [6, 6]]}, {"i9": [[6, 6]]})

    def test_crossing_nets_and_one_guarantee_suffices(self):
        self._setup()
        self.assertEqual(self.desk.compute_ga("dA")["net"], {"A": -5, "B": 4, "P": 1})
        self.assertEqual(self.desk.compute_ga("dB")["net"], {"A": 4, "B": -5, "P": 1})
        self.desk.create_guarantee("G", 4, ["A", "B"])
        self.desk.create_claim("cB", "v2", "B",
                               [{"box_id": "Bb", "segments": [[7, 10]], "priority": 1,
                                 "net_value": 0}], 0, "G")
        ev = self.desk.evaluate_release("cB", "dA")
        self.assertFalse(ev["infeasible"])
        self.assertEqual(ev["options"][0]["guarantee_draw"], 4)

    def test_limit_three_infeasible_not_stacked(self):
        self._setup()
        self.desk.create_guarantee("G3", 3, ["A", "B"])
        self.desk.create_claim("cB", "v2", "B",
                               [{"box_id": "Bb", "segments": [[7, 10]], "priority": 1,
                                 "net_value": 0}], 0, "G3")
        self.assertTrue(self.desk.evaluate_release("cB", "dA")["infeasible"])

    def test_cross_draft_settle_rejected(self):
        self._setup()
        self.desk.create_guarantee("G", 4, ["A", "B"])
        self.desk.create_claim("cB", "v2", "B",
                               [{"box_id": "Bb", "segments": [[7, 10]], "priority": 1,
                                 "net_value": 0}], 0, "G")
        self.desk.create_claim("cA", "v2", "A",
                               [{"box_id": "Ab", "segments": [[2, 5]], "priority": 1,
                                 "net_value": 0}], 0, "G")
        self.desk.release("cB", "dA")
        self.desk.release("cA", "dA")
        self.desk.settle("cB", "dA")
        with self.assertRaises(ReleaseError):
            self.desk.settle("cA", "dB")


class PersistenceTests(Base):
    def test_reopen_restores_state(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        self.desk.add_handover("v", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
        self.desk.register_draft("d-rec", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        self.desk.create_guarantee("G", 2, ["S"])
        self.desk.create_claim("c", "v", "S", two_boxes(), 2, "G")
        self.desk.release("c", "d-rec", keep=[], cash_frozen=2)
        st = self.desk.settle("c", "d-rec")
        self.ledger.close()

        self.ledger = EntityLedger(self.path)
        self.desk = VoyageDesk(self.ledger)
        self.assertIn("i1", self.desk.invoices)
        self.assertEqual(self.desk.invoices["i1"]["q"], 4)
        self.assertIn("d-rec", self.desk.drafts)
        self.assertIn("c", self.desk.claims)
        self.assertEqual(self.desk.claims["c"]["status"], "settled")
        self.assertEqual(self.desk.settlements["c"]["applicant_net"], st["applicant_net"])
        self.assertEqual(self.desk.compute_ga("d-rec")["net"]["S"], 2)
        self.assertEqual(self.desk.rulings["v"], "d-rec")


class LiabilityRegisterTests(Base):
    def test_register_holder_and_provenance(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 2]], "P", "S")
        reg = self.desk.liability_register("v", "i1", [[1, 2]])
        self.assertEqual(reg["pieces"], 2)
        self.assertEqual(reg["recognized_value"], 20)
        self.assertEqual(reg["holders"][0]["holder"], "S")
        self.assertEqual(reg["provenance"][0]["to"], "S")


class JointUnderwritingTests(Base):
    """同一航次先后放货：每次须把尚未结清的全部申请人放在同一份裁定下合并核保。"""

    def _setup(self, limit):
        inv = dict(invoice_id="i1", seller="P", buyer="S", sku="X", q=4, T=40, C=40)
        self.desk.register_invoice(self.seed(inv, p="s1", s="b1"))
        self.desk.create_voyage("v", 10, ["i1"])
        self.desk.add_handover("v", "toA", 1, 1, "i1", [[1, 2]], "P", "A")
        self.desk.add_handover("v", "toB", 1, 2, "i1", [[3, 3]], "P", "B")
        # P 自留 4 号并牺牲它：A、B 同时为正净欠（应摊 > 应获偿）
        self.desk.register_draft("d", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        self.desk.create_guarantee("G", limit, ["A", "B"])
        self.desk.create_claim("cA", "v", "A",
                               [{"box_id": "Ab", "segments": [[1, 2]], "priority": 1,
                                 "net_value": 0}], 0, "G")
        self.desk.create_claim("cB", "v", "B",
                               [{"box_id": "Bb", "segments": [[3, 3]], "priority": 1,
                                 "net_value": 0}], 0, "G")

    def test_simultaneous_debtors(self):
        self._setup(6)
        self.assertEqual(self.desk.compute_ga("d")["net"], {"A": 4, "B": 2, "P": -8})

    def test_two_releases_share_one_limit(self):
        self._setup(6)
        self.assertEqual(self.desk.release("cA", "d")["guarantee_draw"], 4)
        self.assertEqual(self.desk.release("cB", "d")["guarantee_draw"], 2)
        self.assertEqual(self.desk.guarantee_usage("G")["per_draft"]["d"], 6)

    def test_second_needs_joint_check(self):
        # limit 5：A 单户只需 4，但 B 尚未结清、同裁定共需 6 > 5，故 A 也不可单独放行
        self._setup(5)
        self.assertTrue(self.desk.evaluate_release("cA", "d")["infeasible"])


class LimitsTests(Base):
    def test_max_drafts(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        for i in range(MAX_DRAFTS):
            self.desk.register_draft(f"d{i}", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
        from group_consol import ValidationError
        with self.assertRaises(ValidationError):
            self.desk.register_draft("over", "v", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})

    def test_max_boxes(self):
        self.desk.register_invoice(self.seed())
        self.desk.create_voyage("v", 60, ["i1"])
        boxes = [{"box_id": f"x{i}", "segments": [[i, i]], "priority": 1, "net_value": 0}
                 for i in range(13)]
        # 1..13 不全是 S 的，先转给 S
        self.desk.add_handover("v", "h", 1, 1, "i1", [[1, 4]], "P", "S")
        from group_consol import ValidationError
        with self.assertRaises(ValidationError):
            self.desk.create_claim("c", "v", "S", boxes, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
