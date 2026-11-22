"""Worked newspaper-subscription cases on synthetic addresses.

Run::

    python3 examples/newspaper_cases.py

Every subscription root is anchored on a parcel that really succeeds through
the normal catalog/delivery pipeline (no hand-written frozen facts), all
shares are exact rationals, and every plan is the exact optimum of the
min-max cumulative deviation objective.

Cases:
  1. one root split 2:1 before issue 1, three open issues      -> peak 1/3
  2. the split credentials corrected to 1:1                    -> peak 1/2
  3. 2:1 retained, but B's box is closed for issue 2           -> peak 2/3
  4. two households sharing one physical box, separate shares
  5. a same-batch serial split (identity case) is rejected,
     with the two legal ways to express multi-level splits
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from address_delivery.service import AddressService  # noqa: E402
from address_delivery.subscription import (  # noqa: E402
    Scheduler,
    SplitEntry,
    SubscriptionLedger,
)
from address_delivery.subscription.errors import SplitError  # noqa: E402
from address_delivery.subscription.rationals import Q, frac_to_text  # noqa: E402

MATERIALS = ROOT / "materials" / "707"
STREET = "S3"
HOUSE = 2


def _service() -> AddressService:
    return AddressService(Path(tempfile.mkdtemp()) / "paper.db")


def _anchored_ledger(service: AddressService) -> SubscriptionLedger:
    """Submit a real successful parcel at S3#2 and anchor the original root."""
    result = service.submit_new("NP1", "C1", "东", "学园路", HOUSE)
    assert result.outcome == "submitted", result.outcome
    ledger = SubscriptionLedger(service.conn, service.delivery)
    ledger.register_box("BX0", STREET, HOUSE, "老合订门户箱", 1)
    ledger.anchor_root("R1", "OLD", "NP1", "BX0", initial_share=Q(1))
    return ledger


def _print_plan(title: str, ledger: SubscriptionLedger, periods: list[int]) -> None:
    plan = Scheduler(ledger).schedule(periods)
    print(f"\n=== {title} ===")
    print("各户逐期应得（精确分数 + 源流）:")
    seen = set()
    for allocation in ledger.entitlement_table(periods):
        key = (allocation.household_id, allocation.period)
        if key in seen:
            continue
        seen.add(key)
        flow = ", ".join(
            f"{root}={frac_to_text(amount)}" for root, amount in allocation.provenance
        )
        print(
            f"  期{allocation.period} 户{allocation.household_id} "
            f"应得 {frac_to_text(allocation.share):>4} 箱 {allocation.box_id} "
            f"源流[{flow}]"
        )
    print("逐期实报排投与逐户欠份（实得整份 - 累计应得）:")
    for issue in plan.issues:
        balances = " ".join(
            f"{hh}={frac_to_text(value)}" for hh, value in sorted(issue.balances.items())
        )
        if issue.schedulable:
            print(
                f"  期{issue.period} 领户 {issue.recipient} "
                f"实投门箱 {issue.box_id} | 期末欠份 {balances}"
            )
        else:
            print(
                f"  期{issue.period} 不能排（闭箱 {list(issue.closed_boxes)}，"
                f"无人可领）| 期末欠份 {balances}"
            )
    print(f"最优最大绝对差 = {frac_to_text(plan.max_deviation)}")


def case_one() -> None:
    service = _service()
    service.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger = _anchored_ledger(service)
    ledger.register_box("BXA", STREET, HOUSE, "甲户分箱", 1)
    ledger.register_box("BXB", STREET, HOUSE, "乙户分箱", 1)
    ledger.apply_split_batch(
        "B1", 1,
        [SplitEntry("OLD", "A", Q(2, 3), "BXA"),
         SplitEntry("OLD", "B", Q(1, 3), "BXB")],
    )
    _print_plan("案1  开刊前 2:1 分户，三期全开放（期望 1/3）", ledger, [1, 2, 3])


def case_two() -> None:
    service = _service()
    service.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger = _anchored_ledger(service)
    ledger.register_box("BXA", STREET, HOUSE, "甲户分箱", 1)
    ledger.register_box("BXB", STREET, HOUSE, "乙户分箱", 1)
    # Credentials corrected to an equal split.
    ledger.apply_split_batch(
        "B1", 1,
        [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
         SplitEntry("OLD", "B", Q(1, 2), "BXB")],
    )
    _print_plan("案2  分户凭据更正为平分（期望 1/2）", ledger, [1, 2, 3])


def case_three() -> None:
    service = _service()
    service.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger = _anchored_ledger(service)
    ledger.register_box("BXA", STREET, HOUSE, "甲户分箱", 1)
    ledger.register_box("BXB", STREET, HOUSE, "乙户分箱", 1)
    ledger.apply_split_batch(
        "B1", 1,
        [SplitEntry("OLD", "A", Q(2, 3), "BXA"),
         SplitEntry("OLD", "B", Q(1, 3), "BXB")],
    )
    ledger.register_closure(2, "BXB", note="报房第二期封乙箱")
    _print_plan("案3  仍按 2:1，B 箱仅第二期停投（期望 2/3）", ledger, [1, 2, 3])


def case_four_shared_box() -> None:
    service = _service()
    service.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger = _anchored_ledger(service)
    # Two households moving into the same named box; shares stay separate.
    ledger.register_box("BXS", STREET, HOUSE, "合一门箱", 1)
    ledger.apply_split_batch(
        "B1", 1,
        [SplitEntry("OLD", "A", Q(2, 3), "BXS"),
         SplitEntry("OLD", "B", Q(1, 3), "BXS")],
    )
    _print_plan("案4  两家迁入同一门箱，按户分别持份", ledger, [1, 2, 3])


def case_five_identity() -> None:
    service = _service()
    service.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger = _anchored_ledger(service)
    for box_id, label in (("BXA", "甲箱"), ("BXB", "乙箱"), ("BXC", "孙箱")):
        ledger.register_box(box_id, STREET, HOUSE, label, 1)

    print("\n=== 案5  同一批内「先分给又被列作旧户」的身份例 ===")
    serial = [
        SplitEntry("OLD", "A", Q(1, 2), "BXA"),
        SplitEntry("OLD", "B", Q(1, 2), "BXB"),
        SplitEntry("B", "B", Q(1, 2), "BXB"),
        SplitEntry("B", "C", Q(1, 2), "BXC"),
    ]
    try:
        ledger.apply_split_batch("CHAIN", 1, serial)
    except SplitError as exc:
        print(f"串行套单被整批拒绝：{exc}")
    print("拒绝后期初分配未产生任何记录：", ledger.allocated_periods())

    # Legal route A: compose the ratios in one level inside a single batch.
    ledger.apply_split_batch(
        "FLAT", 1,
        [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
         SplitEntry("OLD", "B", Q(1, 4), "BXB"),
         SplitEntry("OLD", "C", Q(1, 4), "BXC")],
    )
    flat = {a.household_id: frac_to_text(a.share) for a in ledger.allocations(1)}
    print("合法做法一（同批一次合成，OLD 直接三分）：", flat)

    # Legal route B: a later-period batch splits B's real opening snapshot.
    service2 = _service()
    service2.load_initial(MATERIALS / "catalog.json", MATERIALS / "coverage.json")
    ledger2 = _anchored_ledger(service2)
    for box_id in ("BXA", "BXB", "BXC"):
        ledger2.register_box(box_id, STREET, HOUSE, box_id, 1)
    ledger2.apply_split_batch(
        "B1", 1,
        [SplitEntry("OLD", "A", Q(1, 2), "BXA"),
         SplitEntry("OLD", "B", Q(1, 2), "BXB")],
    )
    ledger2.apply_split_batch(
        "B2", 2,
        [SplitEntry("B", "B", Q(1, 2), "BXB"),
         SplitEntry("B", "C", Q(1, 2), "BXC")],
    )
    second = {a.household_id: frac_to_text(a.share) for a in ledger2.allocations(2)}
    print("合法做法二（第二期期初再拆 B 的真实份额）：", second)
    print(
        "为什么不能串行套单：批次只按期初快照分配一次。B 在期 1 期初并不存在，"
        "它收到的 1/2 是本批产物；若再当旧户拆，等于把同一笔本金在批内二次分配，"
        "C 会凭同一根订份重复持份并破坏份额守恒。自留存（新旧同号）允许，"
        "跨条目的收益再分不允许。"
    )


def main() -> None:
    case_one()
    case_two()
    case_three()
    case_four_shared_box()
    case_five_identity()


if __name__ == "__main__":
    main()
