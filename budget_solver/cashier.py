"""现金柜：请付日历 + 短存单报价的精确投资排程。

模型（与题面完全一致）：

* 第 d 日顺序：先收当日回款与当日到期存单的本金和收益，再付当日必付款；
  余额非负才可继续；随后可用剩余现金购买当日发行的任意几张存单。
* 每张存单只能整张购买一次、不能中途变现，到期日晚于发行日，且都在日历内；
* 不许向未来借钱（任何一步余额为负即不可行）；
* 目标：最大化最终实现收益总和（所有到期存单的 ``payout - principal``）。

存单数量最多 16 张，因此对“买哪些”做穷举（2^16）即可得到精确最优解；内部
先用各金额分母的最小公倍数放大成整数比较，选中最优组合后再用
:class:`fractions.Fraction` 重建逐日账本，避免浮点。
"""

import os
from fractions import Fraction
from math import lcm

from .errors import NotFoundError, ValidationError
from .rationals import parse_rational, qstr
from .util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic

MAX_DAYS = 20
MAX_DEPOSITS = 16
CASHIER_SCHEMA_VERSION = 1
LEDGER_SCHEMA_VERSION = 1

_ALLOWED_TOP = {
    "cashier_id",
    "name",
    "description",
    "horizon",
    "opening_cash",
    "mandatory",
    "deposits",
}
_ALLOWED_DEPOSIT = {"id", "issue_day", "maturity_day", "principal", "payout"}


def _day_int(value, field, *, minimum=1, maximum=MAX_DAYS):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} 必须是整数日", [field])
    if value < minimum or value > maximum:
        raise ValidationError(f"{field} 超出 {minimum}..{maximum}", [field])
    return value


def normalize_cashier(payload):
    """校验并规范化现金柜配置；非法时抛 ValidationError 且不落盘。"""
    if not isinstance(payload, dict):
        raise ValidationError("现金柜负载必须是 JSON 对象", ["payload"])
    unknown = set(payload) - _ALLOWED_TOP
    if unknown:
        raise ValidationError(f"现金柜含未知字段 {sorted(unknown)}", [])
    cashier_id = payload.get("cashier_id")
    if not isinstance(cashier_id, str) or not cashier_id:
        raise ValidationError("cashier_id 必须是非空字符串", ["cashier_id"])
    name = payload.get("name", cashier_id)
    if not isinstance(name, str) or not name:
        raise ValidationError("name 必须是非空字符串", ["name"])
    horizon = _day_int(payload.get("horizon"), "horizon")
    opening = parse_rational(
        payload.get("opening_cash", 0), field="opening_cash"
    )
    if opening < 0:
        raise ValidationError("opening_cash 不能为负", ["opening_cash"])
    mandatory_raw = payload.get("mandatory") or {}
    if not isinstance(mandatory_raw, dict):
        raise ValidationError("mandatory 必须是对象", ["mandatory"])
    mandatory = {}
    for day_key, value in mandatory_raw.items():
        try:
            day = int(day_key)
        except (TypeError, ValueError):
            raise ValidationError(
                "mandatory 的键必须是整数日", ["mandatory"]
            ) from None
        _day_int(day, f"mandatory[{day}]")
        if day > horizon:
            raise ValidationError(
                f"必付款日 {day} 超出所选日历 1..{horizon}",
                [f"mandatory[{day}]"],
            )
        amount = parse_rational(value, field=f"mandatory[{day}]")
        if amount < 0:
            raise ValidationError(f"mandatory[{day}] 不能为负", [f"mandatory[{day}]"])
        mandatory[str(day)] = qstr(amount)
    deposits_raw = payload.get("deposits") or []
    if not isinstance(deposits_raw, list):
        raise ValidationError("deposits 必须是数组", ["deposits"])
    if len(deposits_raw) > MAX_DEPOSITS:
        raise ValidationError(
            f"存单最多 {MAX_DEPOSITS} 张", ["deposits"]
        )
    deposits = []
    seen_ids = set()
    for index, raw in enumerate(deposits_raw):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {index} 张存单不是对象", ["deposits"])
        unknown_d = set(raw) - _ALLOWED_DEPOSIT
        if unknown_d:
            raise ValidationError(
                f"第 {index} 张存单含未知字段 {sorted(unknown_d)}", ["deposits"]
            )
        dep_id = raw.get("id")
        if not isinstance(dep_id, str) or not dep_id:
            raise ValidationError(f"第 {index} 张存单缺少 id", ["deposits"])
        if dep_id in seen_ids:
            raise ValidationError(f"存单 id {dep_id} 重复", ["deposits"])
        seen_ids.add(dep_id)
        issue = _day_int(raw.get("issue_day"), f"{dep_id}.issue_day")
        maturity = _day_int(raw.get("maturity_day"), f"{dep_id}.maturity_day")
        if issue > horizon:
            raise ValidationError(
                f"存单 {dep_id} 发行日超出所选日历 1..{horizon}",
                [f"deposits[{dep_id}]"],
            )
        if maturity <= issue:
            raise ValidationError(
                f"存单 {dep_id} 到期日必须晚于发行日", [f"deposits[{dep_id}]"]
            )
        if maturity > horizon:
            raise ValidationError(
                f"存单 {dep_id} 到期日超出所选日历 1..{horizon}",
                [f"deposits[{dep_id}]"],
            )
        principal = parse_rational(
            raw.get("principal"), field=f"{dep_id}.principal"
        )
        payout = parse_rational(raw.get("payout"), field=f"{dep_id}.payout")
        if principal < 0 or payout < 0:
            raise ValidationError(
                f"存单 {dep_id} 的本金与兑付额不能为负", [f"deposits[{dep_id}]"]
            )
        deposits.append(
            {
                "id": dep_id,
                "issue_day": issue,
                "maturity_day": maturity,
                "principal": qstr(principal),
                "payout": qstr(payout),
                "profit": qstr(payout - principal),
            }
        )
    norm = {
        "cashier_id": cashier_id,
        "name": name,
        "description": payload.get("description", ""),
        "horizon": horizon,
        "opening_cash": qstr(opening),
        "mandatory": {k: mandatory[k] for k in sorted(mandatory, key=int)},
        "deposits": deposits,
    }
    norm["content_hash"] = content_hash(norm)
    return norm


# --------------------------------------------------------------- 优化与账本
class _Scaled:
    """把全部金额按公共分母放大成整数，供穷举时的精确整数比较。"""

    def __init__(self, config, collections):
        amounts = [
            Fraction(config["opening_cash"]),
            *[Fraction(v) for v in config["mandatory"].values()],
            *[Fraction(d["principal"]) for d in config["deposits"]],
            *[Fraction(d["payout"]) for d in config["deposits"]],
            *collections.values(),
        ]
        denominator = 1
        for amount in amounts:
            denominator = lcm(denominator, amount.denominator)
        self.denominator = denominator

        def scale(value):
            return Fraction(value) * denominator

        self.opening = int(scale(config["opening_cash"]))
        self.mandatory = {
            int(day): int(scale(value)) for day, value in config["mandatory"].items()
        }
        self.collections = {
            day: int(scale(value)) for day, value in collections.items()
        }
        self.deposits = config["deposits"]
        self.principal = [int(scale(d["principal"])) for d in self.deposits]
        self.payout = [int(scale(d["payout"])) for d in self.deposits]


def validate_collections(collections, horizon):
    """回款日历：必须是 1..horizon 的逐日非负精确金额。"""
    if not isinstance(collections, dict):
        raise ValidationError("回款日历必须是对象", ["collections"])
    normalized = {}
    errors = []
    for day_key, value in collections.items():
        try:
            day = int(day_key)
        except (TypeError, ValueError):
            errors.append(f"非整数日 {day_key!r}")
            continue
        if day < 1 or day > horizon:
            errors.append(f"日 {day} 超出 1..{horizon}")
            continue
        amount = parse_rational(value, field=f"collections[{day}]")
        if amount < 0:
            errors.append(f"日 {day} 回款为负（现金柜只接收非负回款）")
        normalized[day] = amount
    missing = [d for d in range(1, horizon + 1) if d not in normalized]
    if missing:
        errors.append(f"缺少日 {missing} 的回款")
    if errors:
        raise ValidationError("回款日历非法", sorted(errors))
    return {d: normalized[d] for d in range(1, horizon + 1)}


def _simulate(config, scaled, mask, *, build_ledger):
    """按 mask 选中的存单模拟；可行返回 (True, ledger)，否则 (False, fail_day)。"""
    horizon = config["horizon"]
    selected = [i for i in range(len(scaled.deposits)) if mask >> i & 1]
    cash = scaled.opening
    ledger_days = []
    for day in range(1, horizon + 1):
        opening = cash
        collected = scaled.collections[day]
        maturing = [i for i in selected if scaled.deposits[i]["maturity_day"] == day]
        maturing_total = sum(scaled.payout[i] for i in maturing)
        cash += collected + maturing_total
        mandatory = scaled.mandatory.get(day, 0)
        cash -= mandatory
        if cash < 0:
            return False, day
        issuing = [i for i in selected if scaled.deposits[i]["issue_day"] == day]
        purchase_cost = sum(scaled.principal[i] for i in issuing)
        cash -= purchase_cost
        if cash < 0:
            return False, day
        if build_ledger:
            ledger_days.append(
                {
                    "day": day,
                    "opening_cash": opening,
                    "collections": collected,
                    "maturities": [
                        {
                            "id": scaled.deposits[i]["id"],
                            "principal": scaled.principal[i],
                            "profit": scaled.payout[i] - scaled.principal[i],
                            "payout": scaled.payout[i],
                        }
                        for i in maturing
                    ],
                    "mandatory_payment": mandatory,
                    "purchases": [
                        {
                            "id": scaled.deposits[i]["id"],
                            "cost": scaled.principal[i],
                        }
                        for i in issuing
                    ],
                    "closing_cash": cash,
                }
            )
    return True, ledger_days


def plan_cashier(config, collections):
    """在给定回款日历下求最优投资安排（穷举全部 2^n 购买组合）。

    非正收益存单（payout ≤ principal）只会在发行日占用现金、到期日最多
    归还本金，从不改善可行性或收益，因此在穷举前剔除；这样最多只需搜索
    2^16 且通常更少。
    """
    horizon = config["horizon"]
    normalized = validate_collections(collections, horizon)
    candidates = [
        (i, dep)
        for i, dep in enumerate(config["deposits"])
        if Fraction(dep["payout"]) > Fraction(dep["principal"])
    ]
    pruned = {**config, "deposits": [dep for _, dep in candidates]}
    scaled = _Scaled(pruned, normalized)
    n = len(pruned["deposits"])
    best_mask = None
    best_key = None
    best_survival = 0
    for mask in range(1 << n):
        feasible, payload = _simulate(
            pruned, scaled, mask, build_ledger=False
        )
        if not feasible:
            # 该组合在第 payload 日因余额为负而终止；最多能撑到前一天
            best_survival = max(best_survival, payload - 1)
            continue
        profit = sum(
            scaled.payout[i] - scaled.principal[i]
            for i in range(n)
            if mask >> i & 1
        )
        ids = tuple(
            sorted(pruned["deposits"][i]["id"] for i in range(n) if mask >> i & 1)
        )
        key = (-profit, len(ids), ids)
        if best_key is None or key < best_key:
            best_key = key
            best_mask = mask
    if best_mask is None:
        return {
            "feasible": False,
            "first_unfundable_day": best_survival + 1,
            "last_survivable_day": best_survival,
            "reason": (
                f"所有存单组合都无法付清；日 {best_survival + 1} 起再也没有"
                f"可行资金安排"
            ),
            "horizon": horizon,
            "deposits_considered": len(config["deposits"]),
        }

    ok, scaled_days = _simulate(pruned, scaled, best_mask, build_ledger=True)
    assert ok
    scale = scaled.denominator
    selected = [
        pruned["deposits"][i]
        for i in range(n)
        if best_mask >> i & 1
    ]
    days = []
    min_close = None
    min_day = None
    for row in scaled_days:
        reconstructed = {
            "day": row["day"],
            "opening_cash": qstr(Fraction(row["opening_cash"], scale)),
            "collections": qstr(Fraction(row["collections"], scale)),
            "maturities": [
                {
                    "id": m["id"],
                    "principal": qstr(Fraction(m["principal"], scale)),
                    "profit": qstr(Fraction(m["profit"], scale)),
                    "payout": qstr(Fraction(m["payout"], scale)),
                }
                for m in row["maturities"]
            ],
            "mandatory_payment": qstr(Fraction(row["mandatory_payment"], scale)),
            "purchases": [
                {
                    "id": p["id"],
                    "cost": qstr(Fraction(p["cost"], scale)),
                }
                for p in row["purchases"]
            ],
            "closing_cash": qstr(Fraction(row["closing_cash"], scale)),
        }
        closing = Fraction(row["closing_cash"], scale)
        if min_close is None or closing < min_close:
            min_close = closing
            min_day = row["day"]
        days.append(reconstructed)
    total_profit = sum((Fraction(d["payout"]) - Fraction(d["principal"]) for d in selected),
                       Fraction(0))
    return {
        "feasible": True,
        "selected_deposit_ids": sorted(d["id"] for d in selected),
        "selected_deposits": selected,
        "total_profit": qstr(total_profit),
        "min_closing_balance": qstr(min_close),
        "min_closing_day": min_day,
        "days": days,
        "horizon": horizon,
    }


# ----------------------------------------------------------------- 持久化
class CashierStore:
    """现金柜配置（请付日历 + 短存单报价）的版本化持久化。"""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        self._index_path = os.path.join(self.root, "index.json")
        ensure_dir(self.root)
        if os.path.exists(self._index_path):
            self._index = read_json(self._index_path)
        else:
            self._index = {"seq": 0, "cashiers": {}}
            self._flush()

    def _flush(self):
        write_json_atomic(self._index_path, self._index)

    def put(self, payload):
        norm = normalize_cashier(payload)
        cashier_id = norm["cashier_id"]
        entry = self._index["cashiers"].get(cashier_id, {"revs": []})
        self._index["seq"] += 1
        rev_id = f"c{self._index['seq']}"
        record = {
            **norm,
            "rev_id": rev_id,
            "revision_number": len(entry["revs"]) + 1,
            "created_at": utc_now_iso(),
        }
        record["record_hash"] = content_hash(
            {k: v for k, v in record.items() if k != "record_hash"}
        )
        path = os.path.join(self.root, "configs", cashier_id, f"{rev_id}.json")
        write_json_atomic(path, record)
        entry["revs"].append(rev_id)
        entry["head"] = rev_id
        entry["updated_at"] = record["created_at"]
        self._index["cashiers"][cashier_id] = entry
        self._flush()
        return record

    def get(self, cashier_id, rev_id=None):
        entry = self._index["cashiers"].get(cashier_id)
        if entry is None:
            raise NotFoundError(f"现金柜 {cashier_id} 不存在")
        chosen = rev_id or entry["head"]
        path = os.path.join(self.root, "configs", cashier_id, f"{chosen}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"现金柜 {cashier_id} 修订 {chosen} 不存在")
        return read_json(path)

    def list(self):
        return [
            {
                "cashier_id": cid,
                "head": entry["head"],
                "revision_count": len(entry["revs"]),
            }
            for cid, entry in sorted(self._index["cashiers"].items())
        ]


class LedgerStore:
    """投资安排账本：不可变、带内容哈希、可重开复读。"""

    def __init__(self, root):
        self.root = os.path.abspath(root)
        ensure_dir(self.root)

    def publish(self, ledger_body):
        seq_path = os.path.join(self.root, "seq.json")
        seq = read_json(seq_path) if os.path.exists(seq_path) else {"next": 1}
        ledger_id = f"ledger{seq['next']}"
        seq["next"] += 1
        write_json_atomic(seq_path, seq)
        record = {
            "ledger_kind": "cashier_ledger",
            "schema_version": LEDGER_SCHEMA_VERSION,
            "ledger_id": ledger_id,
            "created_at": utc_now_iso(),
            **ledger_body,
        }
        record["ledger_hash"] = content_hash(
            {k: v for k, v in record.items() if k != "ledger_hash"}
        )
        write_json_atomic(os.path.join(self.root, f"{ledger_id}.json"), record)
        return record

    def get(self, ledger_id):
        path = os.path.join(self.root, f"{ledger_id}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"账本 {ledger_id} 不存在")
        return read_json(path)

    def list(self):
        if not os.path.isdir(self.root):
            return []
        return sorted(
            f[:-5] for f in os.listdir(self.root)
            if f.startswith("ledger") and f.endswith(".json")
        )
