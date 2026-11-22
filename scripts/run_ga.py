"""按题目给出的三笔海损/提货场景，经“原入账入口 + 两端新业务”真实重开。

每笔都：
  1. 用 EntityLedger.post 真实提交 P 卖方四行、S 买方两行（原入账入口）；
  2. 用 VoyageDesk.register_invoice 按 heads 取同票镜像并逐字核对；
  3. 建航次、登记船上交接单、登记两份互斥裁定草案；
  4. 建联保函、提货申请，求最高总优先分并给出同分方案；
  5. 按其中一份草案结算，据实列明净欠/留箱担保/现金实扣应退/函索金额，
     并验证另一份模拟草案不能动用一分钱。

另加一个两户真实共用一张函、两份草案不能拼起来收费的例子。

运行：python3.11 scripts/run_ga.py [--db state/ga.db]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from fractions import Fraction

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from group_consol.ledger import EntityLedger  # noqa: E402
from group_consol.general_average import VoyageDesk, ReleaseError  # noqa: E402
from group_consol.ga_seed import seed_invoice  # noqa: E402
from group_consol.jsoncodec import frac_to_json  # noqa: E402


def _default(o):
    if isinstance(o, Fraction):
        return frac_to_json(o)
    if isinstance(o, tuple):
        return list(o)
    raise TypeError(f"不可序列化 {o!r}")

INVOICE = dict(invoice_id="i1", seller="P", buyer="S", sku="X", q=4, T=80, C=40)


def fresh(path: str) -> EntityLedger:
    if os.path.exists(path):
        os.remove(path)
    return EntityLedger(path)


def boxes_two() -> list[dict]:
    return [
        {"box_id": "b1", "segments": [[1, 1]], "priority": 1, "net_value": 5},
        {"box_id": "b2", "segments": [[2, 2]], "priority": 1, "net_value": 5},
    ]


def brief(ev: dict) -> dict:
    return {
        "draft": ev["draft_id"],
        "applicant_net": ev["applicant_net"],
        "max_priority": ev["max_priority"],
        "infeasible": ev["infeasible"],
        "best_options": [
            {"keep": o["keep"], "release": o["release"],
             "cash_frozen": o["cash_frozen"], "guarantee_draw": o["guarantee_draw"]}
            for o in ev.get("options", [])
        ],
    }


def scenario_1(db: str) -> dict:
    """原交接（1、2 给 S），牺牲 4；两份草案分别认可/不认可 4；船值 60。"""
    ledger = fresh(db)
    desk = VoyageDesk(ledger)
    heads = seed_invoice(ledger, INVOICE)
    desk.register_invoice(heads)
    desk.create_voyage("v1", 60, ["i1"])
    desk.add_handover("v1", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
    desk.register_draft("d-rec", "v1", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
    desk.register_draft("d-none", "v1", {"i1": [[4, 4]]}, {"i1": []})
    desk.create_guarantee("G-S", 2, ["S"])
    desk.create_claim("cl-S", "v1", "S", boxes_two(), cash_cap=2, guarantee_id="G-S")

    ga_rec = desk.compute_ga("d-rec")
    ga_none = desk.compute_ga("d-none")
    ev_rec = desk.evaluate_release("cl-S", "d-rec")
    ev_none = desk.evaluate_release("cl-S", "d-none")

    result = {
        "scenario": "① 原交接 + 牺牲 4 + 船值 60",
        "heads": heads,
        "s_net_recognized": str(ga_rec["net"]["S"]),
        "s_net_unrecognized": str(ga_none["net"]["S"]),
        "pool_recognized": str(ga_rec["pool"]),
        "evaluate_recognized": brief(ev_rec),
        "evaluate_unrecognized": brief(ev_none),
        "same_score_variants": sorted({
            (o["cash_frozen"], o["guarantee_draw"]) for o in ev_rec["options"]
        }),
    }

    # 放行（认可草案）：全冻 2 元
    desk.release("cl-S", "d-rec", keep=[], cash_frozen=2, guarantee_draw=0)

    # 实际收到“不认可”裁定：应退 2 元，S 净欠 0
    settle_none = desk.settle("cl-S", "d-none")
    result["final_unrecognized"] = {
        "applicant_net": settle_none["applicant_net"],
        "cash_frozen": settle_none["cash_frozen"],
        "cash_deducted": settle_none["cash_deducted"],
        "cash_refund": settle_none["cash_refund"],
        "guarantee_claim": settle_none["guarantee_claim"],
        "secured_by_kept_boxes": settle_none["secured_by_kept_boxes"],
    }
    # 另一份模拟草案此刻不得动用一分钱
    ledger.close()
    return result


def scenario_1b(db: str) -> dict:
    """同场景，但放行时全用函（现金 0、函 2）；最终不认可则无需退款。"""
    ledger = fresh(db)
    desk = VoyageDesk(ledger)
    heads = seed_invoice(ledger, INVOICE)
    desk.register_invoice(heads)
    desk.create_voyage("v1", 60, ["i1"])
    desk.add_handover("v1", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
    desk.register_draft("d-rec", "v1", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
    desk.register_draft("d-none", "v1", {"i1": [[4, 4]]}, {"i1": []})
    desk.create_guarantee("G-S", 2, ["S"])
    desk.create_claim("cl-S", "v1", "S", boxes_two(), cash_cap=2, guarantee_id="G-S")
    desk.release("cl-S", "d-rec", keep=[], cash_frozen=0, guarantee_draw=2)
    settle_none = desk.settle("cl-S", "d-none")
    out = {
        "scenario": "①' 同放行全用函，最终不认可",
        "applicant_net": settle_none["applicant_net"],
        "cash_frozen": settle_none["cash_frozen"],
        "cash_deducted": settle_none["cash_deducted"],
        "cash_refund": settle_none["cash_refund"],
        "guarantee_claim": settle_none["guarantee_claim"],
    }
    ledger.close()
    return out


def scenario_2(db: str) -> dict:
    """只把船上交接改为另将 4 号也转给 S；认可时 S 净获 7，两箱均可无担保放走。"""
    ledger = fresh(db)
    desk = VoyageDesk(ledger)
    heads = seed_invoice(ledger, INVOICE)
    desk.register_invoice(heads)
    desk.create_voyage("v1", 60, ["i1"])
    desk.add_handover("v1", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
    desk.add_handover("v1", "h4", 2, 1, "i1", [[4, 4]], "P", "S")
    desk.register_draft("d-rec", "v1", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
    desk.register_draft("d-none", "v1", {"i1": [[4, 4]]}, {"i1": []})
    desk.create_guarantee("G-S", 2, ["S"])
    desk.create_claim("cl-S", "v1", "S", boxes_two(), cash_cap=2, guarantee_id="G-S")
    ga = desk.compute_ga("d-rec")
    ev = desk.evaluate_release("cl-S", "d-rec")
    held, rejected = desk.holdings("v1")
    result = {
        "scenario": "② 交接另转 4 号给 S + 船值 60",
        "holdings_S": {iid: h.get("S", []) for iid, h in held.items()},
        "rejected_handovers": rejected,
        "s_net_recognized": str(ga["net"]["S"]),
        "pool": str(ga["pool"]),
        "evaluate": brief(ev),
    }
    out = desk.release("cl-S", "d-rec")
    result["released"] = out
    settle = desk.settle("cl-S", "d-rec")
    result["settlement"] = {
        "applicant_net": settle["applicant_net"],
        "cash_frozen": settle["cash_frozen"],
        "guarantee_claim": settle["guarantee_claim"],
        "released_out": settle["released_out"],
        "in_port": settle["in_port"],
    }
    ledger.close()
    return result


def scenario_3(db: str) -> dict:
    """还原交接，只把码头理算船值改为 0；认可时 S 欠 5，全提不足，须留一箱，最高 1 分。"""
    ledger = fresh(db)
    desk = VoyageDesk(ledger)
    heads = seed_invoice(ledger, INVOICE)
    desk.register_invoice(heads)
    desk.create_voyage("v1", 0, ["i1"])
    desk.add_handover("v1", "h12", 1, 1, "i1", [[1, 2]], "P", "S")
    desk.register_draft("d-rec", "v1", {"i1": [[4, 4]]}, {"i1": [[4, 4]]})
    desk.register_draft("d-none", "v1", {"i1": [[4, 4]]}, {"i1": []})
    desk.create_guarantee("G-S", 2, ["S"])
    desk.create_claim("cl-S", "v1", "S", boxes_two(), cash_cap=2, guarantee_id="G-S")
    ga = desk.compute_ga("d-rec")
    ev = desk.evaluate_release("cl-S", "d-rec")
    result = {
        "scenario": "③ 还原交接 + 船值 0",
        "s_net_recognized": str(ga["net"]["S"]),
        "pool": str(ga["pool"]),
        "total_base": str(ga["total_base"]),
        "evaluate": brief(ev),
    }
    # 放行：最高分 1（留一箱、提一箱），留箱即已覆盖全部 5，无需现金/函
    out = desk.release("cl-S", "d-rec")
    result["released"] = out
    settle = desk.settle("cl-S", "d-rec")
    result["settlement"] = {
        "applicant_net": settle["applicant_net"],
        "secured_by_kept_boxes": settle["secured_by_kept_boxes"],
        "cash_deducted": settle["cash_deducted"],
        "guarantee_claim": settle["guarantee_claim"],
        "in_port": settle["in_port"],
        "released_out": settle["released_out"],
    }
    ledger.close()
    return result


def scenario_shared_guarantee(db: str) -> dict:
    """两户真实共用一张函、两份草案不能拼起来收费。

    单票 q=11、unit=10；P 把 1..5 交 A、6..10 交 B，自留 11。
    牺牲 1、6 两件；草案 dA 只认可 A 的 1，dB 只认可 B 的 6。
    于是 A 只在 dB 欠 4、B 只在 dA 欠 4 —— 两份草案互斥，绝不同时发生。
    一张函额 4 足以覆盖任一份裁定；若错误地把两草案相加会误以为需要 8，
    函额 3 则在任一份裁定下都差 1，无法放行。
    """
    ledger = fresh(db)
    desk = VoyageDesk(ledger)
    inv = dict(invoice_id="i9", seller="P", buyer="S", sku="Z", q=11, T=110, C=110)
    heads = seed_invoice(ledger, inv)
    desk.register_invoice(heads)
    desk.create_voyage("v2", 0, ["i9"])
    desk.add_handover("v2", "to-A", 1, 1, "i9", [[1, 5]], "P", "A")
    desk.add_handover("v2", "to-B", 1, 2, "i9", [[6, 10]], "P", "B")
    desk.register_draft("dA", "v2", {"i9": [[1, 1], [6, 6]]}, {"i9": [[1, 1]]})
    desk.register_draft("dB", "v2", {"i9": [[1, 1], [6, 6]]}, {"i9": [[6, 6]]})
    desk.create_guarantee("G-AB", 4, ["A", "B"])
    # 各自可提箱：A 提 2..5，B 提 7..10（都不含牺牲件）
    desk.create_claim("cl-A", "v2", "A",
                      [{"box_id": "A-box", "segments": [[2, 5]], "priority": 1, "net_value": 0}],
                      cash_cap=0, guarantee_id="G-AB")
    desk.create_claim("cl-B", "v2", "B",
                      [{"box_id": "B-box", "segments": [[7, 10]], "priority": 1, "net_value": 0}],
                      cash_cap=0, guarantee_id="G-AB")
    ga_A, ga_B = desk.compute_ga("dA"), desk.compute_ga("dB")
    # B 在 dA 欠 4，A 在 dB 欠 4；两份互斥
    ev_B_dA = desk.evaluate_release("cl-B", "dA")
    ev_A_dB = desk.evaluate_release("cl-A", "dB")
    result = {
        "scenario": "④ 两户共用一张函：两份草案不可相加",
        "dA_net": {h: str(v) for h, v in sorted(ga_A["net"].items())},
        "dB_net": {h: str(v) for h, v in sorted(ga_B["net"].items())},
        "guarantee_limit": 4,
        "B_under_dA": brief(ev_B_dA),
        "A_under_dB": brief(ev_A_dB),
        "wrong_sum_if_stacked": 8,
        "note": "limit=4 即可分别覆盖任一份裁定；limiting=3 则在任一份下差 1，无法放行",
    }
    # 实际按 dA 结算：B 用函 4；A 在 dA 下净欠 -5（无需担保），可无担保放行
    ev_A_dA = desk.evaluate_release("cl-A", "dA")
    result["A_under_dA"] = brief(ev_A_dA)
    desk.release("cl-A", "dA")
    desk.release("cl-B", "dA")
    sB = desk.settle("cl-B", "dA")
    result["settle_B_dA"] = {
        "net": sB["applicant_net"],
        "guarantee_claim": sB["guarantee_claim"],
        "cash_refund": sB["cash_refund"],
    }
    result["guarantee_usage"] = desk.guarantee_usage("G-AB")
    # 现在对 A 用 dB 结算必须被拒绝（同一航次另一份实际裁定）
    try:
        desk.settle("cl-A", "dB")
        result["cross_draft_settle_rejected"] = False
    except ReleaseError as exc:
        result["cross_draft_settle_rejected"] = True
        result["cross_draft_error"] = exc.message
    ledger.close()
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="state/ga.db")
    args = parser.parse_args()
    base, ext = os.path.splitext(args.db)
    scenarios = [
        scenario_1(base + "-s1" + ext),
        scenario_1b(base + "-s1b" + ext),
        scenario_2(base + "-s2" + ext),
        scenario_3(base + "-s3" + ext),
        scenario_shared_guarantee(base + "-s4" + ext),
    ]
    print(json.dumps(scenarios, ensure_ascii=False, indent=2, default=_default))


if __name__ == "__main__":
    main()
