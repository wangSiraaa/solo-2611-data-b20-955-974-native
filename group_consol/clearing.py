"""银行清算包与单体收付凭证的事实核对（不含日界与调节表，那是 binding 的职责）。

口径（对应题目“按真实本侧 receipt / settlement、凭证身份和金额核对”）：
1. 银行给的每个清算包号只属一张双边内部发票（prefix 即实体前缀/发票对身份）；
   任一实体可在同一包分几笔记，但每笔凭证必须唯一归包——跨票、重复归属都不是可签事实。
2. 一包要成为可签清算事实，两侧都必须有凭证，且两侧合计等于该包最终 heads 金额。
   缺失、跨票、重复、最终未平一律结构化报错，绝不按金额凑平。
3. 两侧累计相等不代表收的是同一批包：收付台只能逐包问“全未进 / 双边全收 / 已开头未收齐”。

本模块只做纯内存事实模型与核对，单体凭证身份（voucher_id/seq）原样保留，
不复制会计分录、不改写单体账；序号是“从两边建票以后”的相对提交序号。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ValidationError

ENTITIES = ("P", "S")
# P 是卖方收 receipt，S 是买方付 settlement
SIDES = {"P": "receipt", "S": "settlement"}


@dataclass(frozen=True)
class Voucher:
    """一笔单体后续收付凭证。seq 为该实体建票后的相对序号（1 起，连续）。"""

    entity: str
    seq: int
    voucher_id: str          # 原单体凭证号，装册与重开都按它复读
    package_id: str          # 银行清算包号（=唯一归包身份）
    prefix: str              # 实体前缀/发票对身份，如 "A"、"B"
    amount: int

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "seq": self.seq,
            "voucher_id": self.voucher_id,
            "side": SIDES[self.entity],
            "package_id": self.package_id,
            "prefix": self.prefix,
            "amount": self.amount,
        }


@dataclass
class Package:
    """一个银行清算包：只属一张票（prefix），最终 heads 是两侧各自必须达到的金额。"""

    package_id: str
    prefix: str
    final_head: int
    vouchers: dict[str, list[Voucher]] = field(default_factory=dict)

    def side_amount(self, entity: str) -> int:
        return sum(v.amount for v in self.vouchers.get(entity, ()))

    def side_voucher_ids(self, entity: str) -> list[str]:
        return [v.voucher_id for v in self.vouchers.get(entity, ())]

    def entered_entities(self) -> tuple[str, ...]:
        return tuple(e for e in ENTITIES if self.vouchers.get(e))

    def is_complete(self) -> bool:
        """双边全收：两侧都有凭证、各自合计等于最终 head，且本包专属原单全部到齐。

        只比金额会把“同额别的包/别的原单”误算收齐——两侧累计相等不代表同一批原单。
        """
        for entity in ENTITIES:
            side = self.vouchers.get(entity)
            if not side or self.side_amount(entity) != self.final_head:
                return False
        return True


class ClearingFact:
    """一组已核对的清算包事实。构造即核对，任何非法归属整组拒绝并给结构化明细。"""

    def __init__(
        self,
        packages: dict[str, tuple[str, int]],
        vouchers: list[Voucher],
    ):
        """packages: {package_id: (prefix, final_head)}；vouchers: 两侧全部后续凭证。"""
        self.packages: dict[str, Package] = {}
        for pid, (prefix, head) in sorted(packages.items()):
            if not isinstance(head, int) or isinstance(head, bool) or head <= 0:
                raise ValidationError(f"清算包 {pid} 的最终 head 必须是正整数")
            if not prefix:
                raise ValidationError(f"清算包 {pid} 缺发票前缀")
            self.packages[pid] = Package(pid, prefix, head)

        self.vouchers: list[Voucher] = []
        seen_identity: set[tuple[str, str]] = set()       # (entity, voucher_id) 唯一
        seen_seq: set[tuple[str, int]] = set()            # (entity, seq) 连续唯一
        for v in vouchers:
            self._validate_voucher(v, seen_identity, seen_seq)
            self.packages[v.package_id].vouchers.setdefault(v.entity, []).append(v)
            self.vouchers.append(v)

        # 每实体 seq 必须从 1 连续（允许一侧无凭证）
        for entity in ENTITIES:
            seqs = sorted(v.seq for v in self.vouchers if v.entity == entity)
            if seqs != list(range(1, len(seqs) + 1)):
                raise ValidationError(
                    f"实体 {entity} 的相对序号必须从 1 连续",
                    {"entity": entity, "seqs": seqs},
                )
        self.vouchers.sort(key=lambda v: (v.entity, v.seq))
        self._issues = self._verify_final()

    # ------------------------------------------------------------- validation
    def _validate_voucher(
        self,
        v: Voucher,
        seen_identity: set[tuple[str, str]],
        seen_seq: set[tuple[str, int]],
    ) -> None:
        if v.entity not in ENTITIES:
            raise ValidationError(f"凭证 {v.voucher_id} 的实体必须是 P/S")
        if not isinstance(v.amount, int) or isinstance(v.amount, bool) or v.amount <= 0:
            raise ValidationError(f"凭证 {v.voucher_id} 金额必须是非 bool 正整数")
        if not isinstance(v.seq, int) or isinstance(v.seq, bool) or v.seq <= 0:
            raise ValidationError(f"凭证 {v.voucher_id} 的 seq 必须是正整数")
        if v.package_id not in self.packages:
            raise ValidationError(
                f"凭证 {v.voucher_id} 归入不存在的清算包 {v.package_id}",
                {"code": "unknown_package", "voucher_id": v.voucher_id,
                 "package_id": v.package_id},
            )
        pkg = self.packages[v.package_id]
        if v.prefix != pkg.prefix:
            raise ValidationError(
                f"凭证 {v.voucher_id} 前缀 {v.prefix} 与包 {v.package_id} 专属发票 "
                f"{pkg.prefix} 不一致（跨票归属）",
                {"code": "cross_invoice", "voucher_id": v.voucher_id,
                 "package_id": v.package_id, "voucher_prefix": v.prefix,
                 "package_prefix": pkg.prefix},
            )
        identity = (v.entity, v.voucher_id)
        if identity in seen_identity:
            raise ValidationError(
                f"实体 {v.entity} 凭证号 {v.voucher_id} 重复归包",
                {"code": "duplicate_voucher", "entity": v.entity,
                 "voucher_id": v.voucher_id},
            )
        if (v.entity, v.seq) in seen_seq:
            raise ValidationError(
                f"实体 {v.entity} 相对序号 {v.seq} 重复",
                {"code": "duplicate_seq", "entity": v.entity, "seq": v.seq},
            )
        seen_identity.add(identity)
        seen_seq.add((v.entity, v.seq))

    def _verify_final(self) -> list[dict]:
        """最终 heads 核对：缺侧、未平均结构化列出；同额不同包不得交叉凑平。"""
        issues: list[dict] = []
        for pid, pkg in self.packages.items():
            for entity in ENTITIES:
                amount = pkg.side_amount(entity)
                if not pkg.vouchers.get(entity):
                    issues.append({"code": "missing_side", "package_id": pid,
                                   "prefix": pkg.prefix, "entity": entity})
                elif amount != pkg.final_head:
                    issues.append({"code": "not_balanced", "package_id": pid,
                                   "prefix": pkg.prefix, "entity": entity,
                                   "entered": amount, "final_head": pkg.final_head})
        return issues

    # ------------------------------------------------------------------ facts
    def signable(self) -> bool:
        """无任何缺失/跨票/重复/最终未平，才是可签清算事实。"""
        return not self._issues

    def issues(self) -> list[dict]:
        return [dict(i) for i in self._issues]

    def assert_signable(self) -> None:
        if self._issues:
            raise ValidationError("存在不可签的清算包，不能作为清算事实装册",
                                  {"issues": self._issues})

    def package(self, package_id: str) -> Package:
        if package_id not in self.packages:
            raise ValidationError(f"未知清算包 {package_id}")
        return self.packages[package_id]

    def prefixes(self) -> list[str]:
        return sorted({pkg.prefix for pkg in self.packages.values()})

    # ------------------------------------------------------------ cashier desk
    def desk_query(self, prefix: str) -> dict:
        """收付台单查一对前缀：全未进 / 双边全收 / 已开头未收齐，逐包列原单与金额。"""
        rows = {"prefix": prefix, "all_not_entered": [], "both_complete": [],
                "started_incomplete": []}
        for pid, pkg in sorted(self.packages.items()):
            if pkg.prefix != prefix:
                continue
            entered = pkg.entered_entities()
            entry = {
                "package_id": pid,
                "P_vouchers": pkg.side_voucher_ids("P"),
                "S_vouchers": pkg.side_voucher_ids("S"),
                "P_amount": pkg.side_amount("P"),
                "S_amount": pkg.side_amount("S"),
                "P_missing_amount": pkg.final_head - pkg.side_amount("P"),
                "S_missing_amount": pkg.final_head - pkg.side_amount("S"),
            }
            if not entered:
                rows["all_not_entered"].append(entry)
            elif pkg.is_complete():
                rows["both_complete"].append(entry)
            else:
                rows["started_incomplete"].append(entry)
        return rows

    # ------------------------------------------------------------- re-sorting
    def resubmit_order(self, entity: str, voucher_ids: list[str]) -> "ClearingFact":
        """从同一已核清算重排：只换某实体独立账中的提交顺序，凭证身份/金额/归包不变。

        重排后相对 seq 按新顺序重新编号；原单体凭证保持可复读，这是另一份独立账。
        """
        if entity not in ENTITIES:
            raise ValidationError("重排实体必须是 P/S")
        old = [v for v in self.vouchers if v.entity == entity]
        if sorted(voucher_ids) != sorted(v.voucher_id for v in old):
            raise ValidationError("重排必须恰含该实体全部原凭证，不得增删或换包")
        if len(set(voucher_ids)) != len(voucher_ids):
            raise ValidationError("重排顺序含重复凭证号")
        by_id = {v.voucher_id: v for v in old}
        reordered = [
            Voucher(entity, idx, by_id[vid].voucher_id, by_id[vid].package_id,
                    by_id[vid].prefix, by_id[vid].amount)
            for idx, vid in enumerate(voucher_ids, start=1)
        ]
        kept = [v for v in self.vouchers if v.entity != entity]
        packages = {pid: (pkg.prefix, pkg.final_head) for pid, pkg in self.packages.items()}
        return ClearingFact(packages, kept + reordered)
