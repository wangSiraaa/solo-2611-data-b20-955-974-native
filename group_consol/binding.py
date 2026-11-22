"""日结装册：分日红线、跨日调节表生命周期与两套排法优化。

核心规则（对应题目档案室/银行口径）：
1. 每日给 P、S 两本册各一条末序红线（前缀号），逐日分别不减；末日两册都必须
   到达最终 heads（全部原单入册）。每日可一侧无新凭证（末序与前日相同）。
2. 整包入一日是惯例；也容许为某个确定清算包开一张跨日调节表。只要某道日界把该包
   截在中间（当日末仍未双边全收），自第一次跨界起必须为它专属开一张表，滚入其后
   所有仍未收齐的日报，直到双边全收那天注销。不能每天换号、不能把另一个包接进旧表。
3. 即使两侧当日已入金额相等，只要原单未收齐也必须有表。每日随表登记本包未对齐
   差额＝当日两侧“截至该日累计已进金额”之差的绝对值；全日所有未清表差额之和受
   档案室日差额额度约束，不能跨包互相抵消。
4. 整期原表数受银行配额约束；同一包跨多天只耗一张。测算用的排法不占表号，只有实际
   选中装册的版本才在 archive 里开表/转记/销表，注销表号不重新分配。

优化：
- 问一（配额内求最省移动）：在原表配额与每日差额额度内，最小化总移动
  （逐日逐侧 |实际末序−旧版末序| 之和），同值先省整期原表数。
- 问二（给定总移动量求最低额度）：在总移动上限内最小化所需日差额额度
  （max 每日未清差额和），同额度先省原表，仍受原表配额。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .clearing import ENTITIES, ClearingFact, Package, Voucher
from .errors import ValidationError

MAX_DAYS = 8
MAX_PACKAGES = 6
MAX_VOUCHERS_PER_SIDE = 18


@dataclass(frozen=True)
class DaySpec:
    """档案室给的一天：旧版末序与两册各自允许重排的末序闭区间。"""

    day: int
    old: dict[str, int]
    allowed: dict[str, tuple[int, int]]


@dataclass(frozen=True)
class PackageDayRow:
    """某包某日随册/随表登记的一行。"""

    day: int
    package_id: str
    prefix: str
    P_vouchers: tuple[str, ...]
    S_vouchers: tuple[str, ...]
    P_cum_amount: int
    S_cum_amount: int
    diff: int                       # 两侧累计已进金额之差的绝对值
    complete: bool                  # 当日末是否双边全收
    table_open: bool                # 当日是否开表（首次跨界）
    table_carried: bool             # 当日是否滚入旧表
    table_closed: bool              # 当日是否注销
    under_table: bool               # 当日是否处在该包专属表覆盖下

    def to_dict(self) -> dict:
        return {
            "day": self.day, "package_id": self.package_id, "prefix": self.prefix,
            "P_vouchers": list(self.P_vouchers), "S_vouchers": list(self.S_vouchers),
            "P_cum_amount": self.P_cum_amount, "S_cum_amount": self.S_cum_amount,
            "diff": self.diff, "complete": self.complete,
            "table_open": self.table_open, "table_carried": self.table_carried,
            "table_closed": self.table_closed, "under_table": self.under_table,
        }


@dataclass(frozen=True)
class TableLife:
    """一张包专属调节表的完整生命周期（测算形态，未占表号；装册时才编号）。"""

    package_id: str
    prefix: str
    open_day: int
    close_day: int
    days: tuple[int, ...]
    daily_diff: dict[int, int]

    @property
    def span_days(self) -> int:
        return self.close_day - self.open_day + 1

    def to_dict(self) -> dict:
        return {"package_id": self.package_id, "prefix": self.prefix,
                "open_day": self.open_day, "close_day": self.close_day,
                "days": list(self.days), "daily_diff": dict(sorted(self.daily_diff.items()))}


@dataclass(frozen=True)
class Plan:
    """一套可装册排法（末序红线 + 调节明细 + 全部计量）。"""

    cuts: tuple[dict[str, int], ...]          # 每日两册末序
    rows: tuple[PackageDayRow, ...]
    tables: tuple[TableLife, ...]
    movement: int
    table_count: int
    daily_open_diff_sum: tuple[int, ...]      # 每日所有未清表差额之和（长度=天数）
    required_daily_quota: int                 # max(每日未清差额和)

    def cut(self, day: int) -> dict[str, int]:
        return self.cuts[day - 1]

    def red_lines(self) -> list[dict]:
        return [{"day": d, **self.cuts[d - 1]} for d in range(1, len(self.cuts) + 1)]

    def rows_of_day(self, day: int) -> list[PackageDayRow]:
        return [r for r in self.rows if r.day == day]

    def rows_of_package(self, package_id: str) -> list[PackageDayRow]:
        return [r for r in self.rows if r.package_id == package_id]

    def package_report(self, fact: ClearingFact, day: Optional[int] = None) -> list[dict]:
        """逐包列 P、S 已入原单、两侧已入金额与未进原单（可只看某日截面）。"""
        out: list[dict] = []
        for pid, pkg in fact.packages.items():
            full_p = {v.voucher_id for v in pkg.vouchers.get("P", ())}
            full_s = {v.voucher_id for v in pkg.vouchers.get("S", ())}
            if day is None:
                in_p = set(pkg.side_voucher_ids("P"))
                in_s = set(pkg.side_voucher_ids("S"))
                p_amt, s_amt = pkg.side_amount("P"), pkg.side_amount("S")
            else:
                row = next(r for r in self.rows if r.day == day and r.package_id == pid)
                in_p, in_s = set(row.P_vouchers), set(row.S_vouchers)
                p_amt, s_amt = row.P_cum_amount, row.S_cum_amount
            out.append({
                "package_id": pid, "prefix": pkg.prefix,
                "P_entered": sorted(in_p), "S_entered": sorted(in_s),
                "P_amount": p_amt, "S_amount": s_amt,
                "P_missing_vouchers": sorted(full_p - in_p),
                "S_missing_vouchers": sorted(full_s - in_s),
                "final_head": pkg.final_head,
            })
        return out

    def to_dict(self) -> dict:
        return {
            "red_lines": self.red_lines(),
            "movement": self.movement,
            "table_count": self.table_count,
            "required_daily_quota": self.required_daily_quota,
            "daily_open_diff_sum": list(self.daily_open_diff_sum),
            "tables": [t.to_dict() for t in self.tables],
            "rows": [r.to_dict() for r in self.rows],
        }


class PlanBuilder:
    """在一份已核清算事实与一组日界规格上枚举、评估并选优装册排法。"""

    def __init__(self, fact: ClearingFact, day_specs: list[DaySpec]):
        fact.assert_signable()
        if not (1 <= len(day_specs) <= MAX_DAYS):
            raise ValidationError(f"日数必须在 1..{MAX_DAYS}")
        if len(fact.packages) > MAX_PACKAGES:
            raise ValidationError(f"清算包数不得超过 {MAX_PACKAGES}")
        for entity in ENTITIES:
            n = sum(1 for v in fact.vouchers if v.entity == entity)
            if n > MAX_VOUCHERS_PER_SIDE:
                raise ValidationError(f"实体 {entity} 后续凭证不得超过 {MAX_VOUCHERS_PER_SIDE}")
        self.fact = fact
        self.specs = day_specs
        self.days = len(day_specs)
        self.heads = {e: sum(1 for v in fact.vouchers if v.entity == e) for e in ENTITIES}
        self._validate_specs()
        # 每实体按 seq 排好的凭证，便于按前缀切
        self._by_entity = {
            e: sorted((v for v in fact.vouchers if v.entity == e), key=lambda v: v.seq)
            for e in ENTITIES
        }

    def _validate_specs(self) -> None:
        for idx, spec in enumerate(self.specs, start=1):
            if spec.day != idx:
                raise ValidationError("DaySpec.day 必须从 1 连续编号")
            for e in ENTITIES:
                lo, hi = spec.allowed[e]
                if not (0 <= lo <= hi):
                    raise ValidationError(f"第 {idx} 日实体 {e} 允许区间非法")
                if hi > self.heads[e]:
                    raise ValidationError(
                        f"第 {idx} 日实体 {e} 区间上界 {hi} 超过最终 head {self.heads[e]}")
        last = self.specs[-1]
        for e in ENTITIES:
            if last.allowed[e] != (self.heads[e], self.heads[e]):
                raise ValidationError(
                    f"末日实体 {e} 必须固定在最终 head {self.heads[e]}",
                    {"entity": e, "required_head": self.heads[e],
                     "got": last.allowed[e]})

    # ---------------------------------------------------------- cut evaluation
    def _entered_at(self, entity: str, cut: int) -> list[Voucher]:
        return self._by_entity[entity][:cut]

    def _package_cum(self, cut: dict[str, int], pkg: Package) -> dict[str, list[Voucher]]:
        """截至当日末序，本包两侧实际已入的凭证。

        必须同时限定包号：同额的别的包原单即使已入册，也绝不能算进本包截面。
        """
        out: dict[str, list[Voucher]] = {}
        for e in ENTITIES:
            own_ids = {v.voucher_id for v in pkg.vouchers.get(e, ())}
            out[e] = [
                v for v in self._entered_at(e, cut[e])
                if v.package_id == pkg.package_id and v.voucher_id in own_ids
            ]
        return out

    def evaluate(self, cuts: list[dict[str, int]]) -> Plan:
        """把一套末序红线完整评估为 Plan（含表生命周期、差额、移动量）。"""
        self._check_cuts(cuts)
        rows: list[PackageDayRow] = []
        open_since: dict[str, int] = {}
        table_rows: dict[str, list[PackageDayRow]] = {}

        for d in range(1, self.days + 1):
            cut = cuts[d - 1]
            for pid, pkg in self.fact.packages.items():
                entered = self._package_cum(cut, pkg)
                p_amt = sum(v.amount for v in entered["P"])
                s_amt = sum(v.amount for v in entered["S"])
                # 完整原单全集来自已核事实；当日截面必须两侧原单全到且金额达 head，
                # 不能因金额凑巧相等就判收齐（同额别的原单不算本包）。
                full_p = {v.voucher_id for v in pkg.vouchers.get("P", ())}
                full_s = {v.voucher_id for v in pkg.vouchers.get("S", ())}
                got_p = {v.voucher_id for v in entered["P"]}
                got_s = {v.voucher_id for v in entered["S"]}
                complete = (got_p == full_p and got_s == full_s
                            and p_amt == pkg.final_head and s_amt == pkg.final_head)
                was_open = pid in open_since
                # 日界把包截在中间：当日末未双边全收 => 必须处于表下
                under_table = was_open or not complete
                table_open = (not was_open) and (not complete)
                table_carried = was_open and not complete
                table_closed = was_open and complete
                if table_open:
                    open_since[pid] = d
                    table_rows[pid] = []
                row = PackageDayRow(
                    day=d, package_id=pid, prefix=pkg.prefix,
                    P_vouchers=tuple(v.voucher_id for v in entered["P"]),
                    S_vouchers=tuple(v.voucher_id for v in entered["S"]),
                    P_cum_amount=p_amt, S_cum_amount=s_amt,
                    diff=abs(p_amt - s_amt), complete=complete,
                    table_open=table_open, table_carried=table_carried,
                    table_closed=table_closed, under_table=under_table,
                )
                rows.append(row)
                if under_table:
                    table_rows[pid].append(row)
                if table_closed:
                    del open_since[pid]
        if open_since:
            raise ValidationError("末日仍有未注销调节表（末日未达双边全收）")

        movement = sum(
            abs(cuts[d - 1][e] - self.specs[d - 1].old[e])
            for d in range(1, self.days + 1) for e in ENTITIES
        )
        daily_sum = [
            sum(r.diff for r in rows if r.day == d and r.under_table)
            for d in range(1, self.days + 1)
        ]
        lives = tuple(
            TableLife(package_id=pid, prefix=self.fact.packages[pid].prefix,
                      open_day=rs[0].day, close_day=rs[-1].day,
                      days=tuple(r.day for r in rs),
                      daily_diff={r.day: r.diff for r in rs})
            for pid, rs in sorted(table_rows.items())
        )
        return Plan(
            cuts=tuple(dict(c) for c in cuts), rows=tuple(rows), tables=lives,
            movement=movement, table_count=len(lives),
            daily_open_diff_sum=tuple(daily_sum),
            required_daily_quota=max(daily_sum, default=0),
        )

    def _check_cuts(self, cuts: list[dict[str, int]]) -> None:
        if len(cuts) != self.days:
            raise ValidationError("末序红线天数与日界规格不一致")
        prev = {e: 0 for e in ENTITIES}
        for d, (cut, spec) in enumerate(zip(cuts, self.specs), start=1):
            if set(cut) != set(ENTITIES):
                raise ValidationError(f"第 {d} 日红线必须同时给 P、S")
            for e in ENTITIES:
                lo, hi = spec.allowed[e]
                if not (lo <= cut[e] <= hi):
                    raise ValidationError(
                        f"第 {d} 日实体 {e} 末序 {cut[e]} 越出允许区间 [{lo},{hi}]",
                        {"code": "out_of_range", "day": d, "entity": e,
                         "cut": cut[e], "allowed": [lo, hi]})
                if cut[e] < prev[e]:
                    raise ValidationError(f"第 {d} 日实体 {e} 末序较前日回落")
                prev[e] = cut[e]
        for e in ENTITIES:
            if cuts[-1][e] != self.heads[e]:
                raise ValidationError(f"末日实体 {e} 未达最终 head")

    # ------------------------------------------------------------- enumeration
    def enumerate_plans(
        self,
        move_cap: Optional[int] = None,
        quota_cap: Optional[int] = None,
        table_cap: Optional[int] = None,
    ) -> list[Plan]:
        """枚举全部合规排法，可按三道配额预筛（仅用于诊断/展示，不占表号）。"""
        found: list[Plan] = []
        cuts: list[dict] = [dict() for _ in range(self.days)]

        def backtrack(d: int, prev: dict[str, int]) -> None:
            if d > self.days:
                plan = self.evaluate(cuts)
                if move_cap is not None and plan.movement > move_cap:
                    return
                if quota_cap is not None and plan.required_daily_quota > quota_cap:
                    return
                if table_cap is not None and plan.table_count > table_cap:
                    return
                found.append(plan)
                return
            spec = self.specs[d - 1]
            for p in range(max(spec.allowed["P"][0], prev["P"]),
                           spec.allowed["P"][1] + 1):
                for s in range(max(spec.allowed["S"][0], prev["S"]),
                               spec.allowed["S"][1] + 1):
                    cuts[d - 1] = {"P": p, "S": s}
                    backtrack(d + 1, {"P": p, "S": s})

        backtrack(1, {e: 0 for e in ENTITIES})
        return found

    # ------------------------------------------------------------- two questions
    def best_under_quotas(self, table_quota: int, daily_quota: int) -> Plan:
        """问一：在原表配额与日差额额度内，总移动最少；同值先省原表数。"""
        if table_quota < 0 or daily_quota < 0:
            raise ValidationError("配额必须非负")
        best: Optional[Plan] = None
        best_key = None
        for plan in self.enumerate_plans(
            quota_cap=daily_quota, table_cap=table_quota
        ):
            key = (plan.movement, plan.table_count, plan.required_daily_quota)
            if best is None or key < best_key:
                best, best_key = plan, key
        if best is None:
            raise ValidationError(
                "配额内无可装册排法", self.diagnose(table_quota, daily_quota))
        return best

    def minimum_quota_for_movement(self, move_budget: int, table_quota: int) -> Plan:
        """问二：总移动不超预算时，最低须申请多少日差额额度；同额度先省原表。"""
        if move_budget < 0 or table_quota < 0:
            raise ValidationError("移动预算与表配额必须非负")
        best: Optional[Plan] = None
        best_key = None
        for plan in self.enumerate_plans(move_cap=move_budget, table_cap=table_quota):
            key = (plan.required_daily_quota, plan.table_count, plan.movement)
            if best is None or key < best_key:
                best, best_key = plan, key
        if best is None:
            raise ValidationError(
                "给定总移动量下无可装册排法", self.diagnose(table_quota, None, move_budget))
        return best

    # -------------------------------------------------------------- diagnostics
    def diagnose(
        self,
        table_quota: Optional[int],
        daily_quota: Optional[int],
        move_budget: Optional[int] = None,
    ) -> dict:
        """按给定三道配额定位卡点。单项是否短缺，要在“其余约束仍生效”的前提下判断，
        否则会把联合约束误报成单项短缺（例如移动<=1 时最低额度本来就是 10）。
        """
        all_plans = self.enumerate_plans()
        if not all_plans:
            return {"code": "no_feasible_cut",
                    "reason": "允许区间/不减/末日最终 heads 之下不存在合规日界"}

        def reachable(**caps) -> list[Plan]:
            return self.enumerate_plans(
                move_cap=caps.get("move_cap"),
                quota_cap=caps.get("quota_cap"),
                table_cap=caps.get("table_cap"))

        blockers: list[dict] = []
        # 日差额额度是否够：固定移动预算与表配额，看可达最低额度
        if daily_quota is not None:
            pool = reachable(move_cap=move_budget, table_cap=table_quota)
            need = min((p.required_daily_quota for p in pool), default=None)
            if need is None:
                blockers.append({"code": "no_plan_under_other_constraints",
                                 "constraints": {"move_budget": move_budget,
                                                 "table_quota": table_quota}})
            elif need > daily_quota:
                blockers.append({"code": "daily_quota_short", "cap": daily_quota,
                                 "minimum_required": need})
        # 表配额是否够：固定移动预算与日差额额度
        if table_quota is not None:
            pool = reachable(move_cap=move_budget, quota_cap=daily_quota)
            need = min((p.table_count for p in pool), default=None)
            if need is not None and need > table_quota:
                blockers.append({"code": "table_quota_short", "cap": table_quota,
                                 "minimum_required": need})
        # 移动预算是否够：固定表配额与日差额额度
        if move_budget is not None:
            pool = reachable(quota_cap=daily_quota, table_cap=table_quota)
            need = min((p.movement for p in pool), default=None)
            if need is not None and need > move_budget:
                blockers.append({"code": "move_budget_short", "cap": move_budget,
                                 "minimum_required": need})

        if not blockers:
            blockers.append({"code": "joint_constraints",
                             "reason": "单项配额各自够，但无法在同一套排法同时满足"})
        return {
            "code": "blocked",
            "blockers": blockers,
            "unconstrained_minimum": {
                "movement": min(p.movement for p in all_plans),
                "tables": min(p.table_count for p in all_plans),
                "daily_quota": min(p.required_daily_quota for p in all_plans),
            },
        }
