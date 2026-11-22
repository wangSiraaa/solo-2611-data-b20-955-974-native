"""档案室装册与调节表号簿（只对实际选中的版本动手，测算排法绝不占号）。

不可变与可复读约定，对齐单体账只追加口径：
1. bind() 是唯一会签发装册副本的动作：对一套选定 Plan 分配整期表号，开表、逐日
   滚入、注销全部落进不可变记录。再 bind 另一套排法得到独立档案，旧册不改写。
2. 表号按 (open_day, package_id) 顺序从配额池分配；同一包跨多天只耗一张；
   注销后的表号永久保留、不能重新分配给别的包或二次开表。
3. 每日装两册（P/S 各一本），副本只记原单体凭证号与末序红线，原单体凭证与已封
   历史册保持可复读；重开档案可沿凭证号反查它经过的开表/转记/销表手续。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .binding import Plan
from .clearing import ENTITIES, ClearingFact
from .errors import ConflictError, ValidationError


@dataclass(frozen=True)
class SealedVolume:
    """一日一册的装册副本：末序红线 + 当日实际入册的原凭证号（按相对 seq）。"""

    archive_id: str
    day: int
    entity: str
    head: int
    voucher_ids: tuple[str, ...]

    def to_dict(self) -> dict:
        return {"archive_id": self.archive_id, "day": self.day, "entity": self.entity,
                "head": self.head, "voucher_ids": list(self.voucher_ids)}


@dataclass(frozen=True)
class TableRecord:
    """一张已签发调节表的完整台账（表号一经分配不复用）。"""

    table_no: str
    package_id: str
    prefix: str
    open_day: int
    close_day: int
    status: str                       # open / closed
    daily_entries: tuple[dict, ...]   # 每日随表登记：凭证、累计金额、差额、动作

    def to_dict(self) -> dict:
        return {"table_no": self.table_no, "package_id": self.package_id,
                "prefix": self.prefix, "open_day": self.open_day,
                "close_day": self.close_day, "status": self.status,
                "daily_entries": list(self.daily_entries)}


class BoundArchive:
    """一次选定装册的不可变档案。表号池大小＝银行原表配额，用满即拒。"""

    def __init__(self, archive_id: str, fact: ClearingFact, plan: Plan,
                 table_quota: int):
        if not isinstance(table_quota, int) or isinstance(table_quota, bool) \
                or table_quota < 0:
            raise ValidationError("原表配额必须是非负整数")
        if plan.table_count > table_quota:
            raise ValidationError(
                f"本排法需 {plan.table_count} 张原表，超过配额 {table_quota}",
                {"code": "table_quota_short", "required": plan.table_count,
                 "cap": table_quota})
        self.archive_id = archive_id
        self.fact = fact
        self.plan = plan
        self.table_quota = table_quota
        self.volumes: list[SealedVolume] = []
        self.tables: dict[str, TableRecord] = {}
        # package_id -> table_no；表号分配顺序固定可复现
        self._package_table: dict[str, str] = {}
        self._build()

    def _build(self) -> None:
        # 1) 分配表号（仅给真正开表的包；测算排法从未进入这里，故不占号）
        for idx, life in enumerate(sorted(self.plan.tables,
                                          key=lambda t: (t.open_day, t.package_id)),
                                   start=1):
            table_no = f"R{idx:03d}"
            self._package_table[life.package_id] = table_no

        # 2) 逐日封两册：只引用原凭证号，不复制分录。册内列截至当日累计前缀，
        #    前日已入凭证在新册中属历史复读；另用 first_seen 记录每张凭证首次入册日。
        prev_head = {e: 0 for e in ENTITIES}
        first_seen: dict[tuple[str, str], int] = {}
        for d in range(1, len(self.plan.cuts) + 1):
            cut = self.plan.cut(d)
            for e in ENTITIES:
                entered = [v.voucher_id for v in self.fact.vouchers
                           if v.entity == e and v.seq <= cut[e]]
                if cut[e] < prev_head[e]:
                    raise ValidationError("装册末序回落，违反不减约束")
                vol = SealedVolume(self.archive_id, d, e, cut[e], tuple(entered))
                self.volumes.append(vol)
                for vid in entered:
                    first_seen.setdefault((e, vid), d)
                prev_head[e] = cut[e]

        # 3) 表台账：开表/滚入/注销，每日带差额与原单
        for life in sorted(self.plan.tables, key=lambda t: (t.open_day, t.package_id)):
            table_no = self._package_table[life.package_id]
            entries = []
            for d in life.days:
                row = next(r for r in self.plan.rows
                           if r.day == d and r.package_id == life.package_id)
                if row.table_open:
                    action = "open"
                elif row.table_closed:
                    action = "close"
                else:
                    action = "carry"
                entries.append({
                    "day": d, "action": action, "diff": row.diff,
                    "P_vouchers": list(row.P_vouchers),
                    "S_vouchers": list(row.S_vouchers),
                    "P_cum_amount": row.P_cum_amount,
                    "S_cum_amount": row.S_cum_amount,
                })
            self.tables[table_no] = TableRecord(
                table_no=table_no, package_id=life.package_id, prefix=life.prefix,
                open_day=life.open_day, close_day=life.close_day,
                status="closed", daily_entries=tuple(entries))

        # 4) 封册完整性：末日累计册必须与已核事实的原单全集逐号一致，
        #    且每张凭证都有唯一首次入册日（历史复读不算重复装册）。
        for e in ENTITIES:
            final_vol = max((v for v in self.volumes if v.entity == e),
                            key=lambda v: v.day)
            if len(final_vol.voucher_ids) != len(set(final_vol.voucher_ids)):
                raise ValidationError(f"实体 {e} 末日册内存在重复凭证")
            actual = sorted(final_vol.voucher_ids)
            expected = sorted(v.voucher_id for v in self.fact.vouchers if v.entity == e)
            if actual != expected:
                raise ValidationError(
                    f"实体 {e} 装册原单与已核清算不一致",
                    {"code": "volume_mismatch", "entity": e,
                     "sealed": actual, "expected": expected})
            first_days = {vid: d for (en, vid), d in first_seen.items() if en == e}
            if sorted(first_days) != expected:
                raise ValidationError(f"实体 {e} 存在凭证首次入册遗漏")

    # ---------------------------------------------------------------- queries
    def table_of(self, package_id: str) -> Optional[TableRecord]:
        return self.tables.get(self._package_table.get(package_id, ""))

    def table_no_of(self, package_id: str) -> Optional[str]:
        return self._package_table.get(package_id)

    def volumes_of_day(self, day: int) -> list[SealedVolume]:
        return [v for v in self.volumes if v.day == day]

    def reopen(self) -> dict:
        """重开档案：完整复读封册与表台账，证明历史册未被改写。"""
        return {
            "archive_id": self.archive_id,
            "volumes": [v.to_dict() for v in self.volumes],
            "tables": [t.to_dict() for t in self.tables.values()],
            "red_lines": self.plan.red_lines(),
        }

    def trace_voucher(self, entity: str, voucher_id: str) -> dict:
        """沿原单体凭证号反查：它入了哪日益哪本册、经过哪些表的哪日转记。"""
        if entity not in ENTITIES:
            raise ValidationError("实体必须是 P/S")
        sealed_day = next((vol.day for vol in self.volumes
                           if vol.entity == entity and voucher_id in vol.voucher_ids), None)
        if sealed_day is None:
            raise ValidationError(f"凭证 {entity}/{voucher_id} 未在本档案装册")
        touches = []
        for table in self.tables.values():
            for entry in table.daily_entries:
                ids = entry[f"{entity}_vouchers"]
                if voucher_id in ids:
                    touches.append({"table_no": table.table_no,
                                    "package_id": table.package_id,
                                    "day": entry["day"], "action": entry["action"]})
        return {"entity": entity, "voucher_id": voucher_id,
                "sealed_day": sealed_day, "table_touches": touches}

    def table_numbers_used(self) -> list[str]:
        return sorted(self.tables)


class ArchiveRegistry:
    """管理多次装册：各档案独立不可变；表号只在各自档案内分配，跨档案重开不串号。"""

    def __init__(self):
        self._archives: dict[str, BoundArchive] = {}

    def bind(self, archive_id: str, fact: ClearingFact, plan: Plan,
             table_quota: int) -> BoundArchive:
        if archive_id in self._archives:
            existing = self._archives[archive_id]
            if existing.plan.to_dict() != plan.to_dict():
                raise ConflictError(f"档案号 {archive_id} 已绑定不同排法，异文拒绝")
            return existing
        archive = BoundArchive(archive_id, fact, plan, table_quota)
        self._archives[archive_id] = archive
        return archive

    def get(self, archive_id: str) -> BoundArchive:
        if archive_id not in self._archives:
            raise ValidationError(f"未知档案 {archive_id}")
        return self._archives[archive_id]

    def list_archives(self) -> list[str]:
        return sorted(self._archives)
