"""分户订份册与逐期实报排投（虚构住址、有理数权益）。

本子系统建立在既有的 :mod:`address_delivery.catalog` 与
:mod:`address_delivery.delivery` 之上，自己只增加订份册/排投表，不改动
目录与作业库的任何既有表：

* :mod:`address_delivery.subscription.rationals` - 分数的精确表示与解析；
* :mod:`address_delivery.subscription.ledger` - 认门锚定、具名门箱时点登记、
  期初一次分配的分户批次、订份份额与源流账；
* :mod:`address_delivery.subscription.scheduling` - 逐期实报排投，最小化
  任一期末任一户「累计实得整份数 - 累计应得份额」的最大绝对值。

关键边界与既有工程一致：订份起点只能锚定**已成功（已冻结）的包裹**，
取该包裹作业里冻结的稳定街道/门牌与当时的门箱登记；不会按今天同名街的
覆盖站点倒推。
"""
from __future__ import annotations

from .ledger import (
    BoxRegistration,
    OriginAnchor,
    PeriodAllocation,
    SplitBatch,
    SplitEntry,
    SubscriptionLedger,
)
from .rationals import Q, frac_from_text, frac_to_text
from .scheduling import (
    IssuePlan,
    PlannedIssue,
    Scheduler,
    make_box_calendar,
)

__all__ = [
    "BoxRegistration",
    "IssuePlan",
    "OriginAnchor",
    "PeriodAllocation",
    "PlannedIssue",
    "Q",
    "Scheduler",
    "SplitBatch",
    "SplitEntry",
    "SubscriptionLedger",
    "frac_from_text",
    "frac_to_text",
    "make_box_calendar",
]
