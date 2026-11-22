"""海损理算与码头提货（虚构展船，合成口径，不处理真实保险法规）。

本模块只做题目给定的机制：

1. **真实核对**：从 ``group_consol`` 已入账的 P/S 单体账簿按指定 heads 取同票两侧
   内部发票，逐字核对 ``invoice_id / seller / buyer / sku / q / T / C``；缺一侧或镜像
   有差异即返回结构化待核，绝不按金额凑平。
2. **船上交接**：按 ``(时刻, 同刻序号)`` 生效的交接单，可一单同时转多个闭件段；交出
   人须持有各段全部实件，段内重叠或部分无权则整单退回。解析对到档顺序不敏感。
3. **共同海损**：实际牺牲件段已知；多份互斥裁定草案各只认可其中一部分。认可件按
   担险人计入补偿池；每户分摊基数 = 实际获救货值 + 获认可牺牲值；船方另有非负获救
   船值。按基数占比分摊补偿池，``净欠 = 应摊 − 应获偿``。总基数为零且池为空时全零。
4. **提货/担保**：整箱提货单；留箱、冻结现金、联保函按
   ``同一户先留箱→再现金→最后函`` 顺序覆盖其正净欠。放行后未提箱只保本人债务；联保
   函全场只有一个可赔付总额，且只能按同一份实际裁定赔付，绝不把互斥草案拼起来收费。

金额一律用 :class:`fractions.Fraction`（整数分），持久化时用 num/den，绝不取整。
"""

from __future__ import annotations

import sqlite3
from fractions import Fraction
from typing import Iterable, Optional

from .errors import LedgerError, ValidationError, PendingError
from .jsoncodec import dumps, loads, frac_to_json, json_to_frac
from .ledger import EntityLedger
from .segs import (
    Segments,
    normalize,
    has_overlap,
    count,
    subtract,
    intersect,
    is_subset,
    within,
)

MAX_BOXES = 12
MAX_DRAFTS = 8


class ReleaseError(LedgerError):
    """提货/担保冲突（重复释放、跨货主、含已损货、担保不足等）。"""

    code = "release_error"


def _seg_json(segs: Iterable) -> list:
    return [[lo, hi] for lo, hi in normalize(segs)]


def _seg_load(raw: Iterable) -> Segments:
    return [(int(a), int(b)) for a, b in raw]


def _ceil_fen(frac: Fraction) -> int:
    """把有理金额向上取整到整数分（用于判断整数现金/保函是否够用）。"""
    q, r = divmod(frac.numerator, frac.denominator)
    return q if r == 0 else q + 1


def _floor_fen(frac: Fraction) -> int:
    return frac.numerator // frac.denominator


def _jsonable(obj):
    """递归把 Fraction 转成 num/den JSON 标量，list/dict 原样结构。"""
    if isinstance(obj, Fraction):
        return frac_to_json(obj)
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_jsonable(v) for v in obj]
    return obj


def _restore_frac(value, fields):
    """把记录中指定字段由 num/den 还原为 Fraction（若为数字）。"""
    for f in fields:
        if f in value and value[f] is not None:
            value[f] = json_to_frac(value[f])
    return value


class VoyageDesk:
    """航次理算台：发票核对、交接、裁定、理赔与提货。"""

    def __init__(self, ledger: EntityLedger):
        self.ledger = ledger
        self.conn: sqlite3.Connection = ledger.conn
        self._create_schema()
        self.invoices: dict[str, dict] = {}
        self.voyages: dict[str, dict] = {}
        self.handovers: list[dict] = []
        self.drafts: dict[str, dict] = {}
        self.guarantees: dict[str, dict] = {}
        self.claims: dict[str, dict] = {}
        self.settlements: dict[str, dict] = {}
        self.rulings: dict[str, str] = {}
        self._load()

    # ------------------------------------------------------------- schema
    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS ga_invoices (
                invoice_id TEXT PRIMARY KEY,
                payload    TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_voyages (
                voyage_id TEXT PRIMARY KEY,
                payload   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_handovers (
                ord       INTEGER PRIMARY KEY AUTOINCREMENT,
                voyage_id TEXT NOT NULL,
                payload   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_drafts (
                draft_id TEXT PRIMARY KEY,
                payload  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_guarantees (
                guarantee_id TEXT PRIMARY KEY,
                payload      TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_claims (
                claim_id TEXT PRIMARY KEY,
                payload  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_settlements (
                claim_id TEXT PRIMARY KEY,
                payload  TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ga_rulings (
                voyage_id TEXT PRIMARY KEY,
                draft_id  TEXT NOT NULL
            );
            """
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    def _load(self) -> None:
        for row in self.conn.execute("SELECT invoice_id, payload FROM ga_invoices"):
            self.invoices[row[0]] = loads(row[1])
        for row in self.conn.execute("SELECT voyage_id, payload FROM ga_voyages"):
            self.voyages[row[0]] = loads(row[1])
        self.handovers = [
            loads(r[0])
            for r in self.conn.execute(
                "SELECT payload FROM ga_handovers ORDER BY voyage_id, ord"
            )
        ]
        for tbl, store, key in (
            ("ga_drafts", self.drafts, "draft_id"),
            ("ga_guarantees", self.guarantees, "guarantee_id"),
            ("ga_claims", self.claims, "claim_id"),
        ):
            for row in self.conn.execute(f"SELECT {key}, payload FROM {tbl}"):
                store[row[0]] = loads(row[1])
        # 还原可能为分数的字段
        for claim in self.claims.values():
            _restore_frac(claim, ("guarantee_draw", "frozen_cash"))
            for h in claim.get("history", []):
                _restore_frac(h, ("guarantee_draw", "cash_frozen"))
        for g in self.guarantees.values():
            for d in g.get("drawn", []):
                d["amount"] = json_to_frac(d["amount"])
        for row in self.conn.execute("SELECT claim_id, payload FROM ga_settlements"):
            self.settlements[row[0]] = loads(row[1])
        for row in self.conn.execute("SELECT voyage_id, draft_id FROM ga_rulings"):
            self.rulings[row[0]] = row[1]

    def _save(self, table: str, key_col: str, key: str, payload: dict) -> None:
        self.conn.execute(
            f"INSERT INTO {table} ({key_col}, payload) VALUES (?, ?) "
            f"ON CONFLICT({key_col}) DO UPDATE SET payload = excluded.payload",
            (key, dumps(_jsonable(payload))),
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")

    # ================================================================ 发票核对
    def register_invoice(self, heads: dict) -> dict:
        """按两侧 heads 取同票镜像并真实核对；缺侧/异文返回结构化待核。"""
        if not isinstance(heads, dict) or set(heads) != {"P", "S"}:
            raise ValidationError("heads 必须恰好包含 P/S 两个前缀")
        pair: dict[str, dict] = {}
        for entity in ("P", "S"):
            for inv in self.ledger.internal_invoices(entity, heads[entity]):
                pair.setdefault(inv["invoice_id"], {})[entity] = inv

        issues: list[dict] = []
        for invoice_id in sorted(pair):
            sides = pair[invoice_id]
            if "P" not in sides or "S" not in sides:
                issues.append({
                    "issue_type": "missing_side",
                    "details": {"invoice_id": invoice_id,
                                "missing": [e for e in ("P", "S") if e not in sides]},
                })
                continue
            p, s = sides["P"], sides["S"]
            diffs = {f: {"P": p[f], "S": s[f]}
                     for f in ("seller", "buyer", "sku", "q", "T", "C") if p[f] != s[f]}
            if p["side"] == s["side"]:
                diffs["side"] = {"P": p["side"], "S": s["side"]}
            if diffs:
                issues.append({"issue_type": "invoice_mismatch",
                               "details": {"invoice_id": invoice_id, "fields": diffs}})
                continue
            if p["side"] != "seller" or s["side"] != "buyer":
                issues.append({"issue_type": "invoice_mismatch",
                               "details": {"invoice_id": invoice_id, "field": "roles",
                                           "P": p["side"], "S": s["side"]}})
                continue
            record = {
                "invoice_id": invoice_id,
                "seller": p["seller"], "buyer": p["buyer"], "sku": p["sku"],
                "q": p["q"], "T": p["T"], "C": p["C"],
                "seller_head": heads["P"], "buyer_head": heads["S"],
                "P_seq": p["journal_seq"], "S_seq": s["journal_seq"],
            }
            self.invoices[invoice_id] = record
            self._save("ga_invoices", "invoice_id", invoice_id, record)
        if issues:
            raise PendingError(f"发票核对有 {len(issues)} 项待核", {"issues": issues})
        return {"validated": sorted(self.invoices)}

    # ================================================================ 航次
    def create_voyage(self, voyage_id: str, ship_value: int, invoice_ids: list[str]) -> dict:
        if voyage_id in self.voyages:
            raise ValidationError(f"航次 {voyage_id} 已存在")
        if isinstance(ship_value, bool) or not isinstance(ship_value, int) or ship_value < 0:
            raise ValidationError("船方获救船值必须是非 bool 非负整数")
        if not invoice_ids:
            raise ValidationError("航次至少登记一张发票")
        for iid in invoice_ids:
            if iid not in self.invoices:
                raise ValidationError(f"发票 {iid} 尚未通过核对")
        record = {"voyage_id": voyage_id, "ship_value": ship_value,
                  "invoice_ids": list(invoice_ids)}
        self.voyages[voyage_id] = record
        self._save("ga_voyages", "voyage_id", voyage_id, record)
        return record

    # ================================================================ 船上交接
    def add_handover(self, voyage_id: str, transfer_id: str, time: int, seq: int,
                     invoice_id: str, segments: Iterable, holder_from: str,
                     holder_to: str) -> dict:
        """登记一张交接单（到档顺序无关，解析时按 (time, seq) 排序生效）。"""
        if voyage_id not in self.voyages:
            raise ValidationError(f"未知航次 {voyage_id}")
        if invoice_id not in self.voyages[voyage_id]["invoice_ids"]:
            raise ValidationError(f"发票 {invoice_id} 不属于航次 {voyage_id}")
        for v, label in ((time, "时刻"), (seq, "同刻序号")):
            if isinstance(v, bool) or not isinstance(v, int) or v < 0:
                raise ValidationError(f"交接{label}必须是非 bool 非负整数")
        if any(h["transfer_id"] == transfer_id for h in self.handovers):
            raise ValidationError(f"交接单 {transfer_id} 重复登记")
        segs = _seg_load(segments)
        # 段内重叠不在登记时拒绝：保留原段次序，由 holdings() 作为“整单退回”
        # 处理（不得预先 normalize，否则重叠信息被吞掉）。
        record = {
            "voyage_id": voyage_id, "transfer_id": transfer_id,
            "time": time, "seq": seq, "invoice_id": invoice_id,
            "segments": [[lo, hi] for lo, hi in segs],
            "from": holder_from, "to": holder_to,
        }
        self.handovers.append(record)
        self.conn.execute(
            "INSERT INTO ga_handovers (voyage_id, payload) VALUES (?, ?)",
            (voyage_id, dumps(record)),
        )
        if self.conn.in_transaction:
            self.conn.execute("COMMIT")
        return record

    def holdings(self, voyage_id: str) -> tuple[dict[str, dict[str, Segments]], list[dict]]:
        """按 (time, seq) 应用交接单，返回 (持仓, 被退回的交接单)。

        单张交接单段重叠、超出 1..q、或交出人不持有某段全部实件，整单退回，不改变持仓。
        """
        voyage = self.voyages[voyage_id]
        result: dict[str, dict[str, Segments]] = {}
        for iid in voyage["invoice_ids"]:
            q = self.invoices[iid]["q"]
            result[iid] = {self.invoices[iid]["seller"]: [(1, q)]}

        records = sorted(
            (h for h in self.handovers if h["voyage_id"] == voyage_id),
            key=lambda h: (h["time"], h["seq"]),
        )
        rejected: list[dict] = []
        for h in records:
            iid = h["invoice_id"]
            segs = _seg_load(h["segments"])
            q = self.invoices[iid]["q"]
            reason = None
            if not segs:
                reason = "空交接段"
            elif has_overlap(segs):
                reason = "交接段内部重叠"
            elif not within(segs, 1, q):
                reason = f"超出件号范围 1..{q}"
            elif not is_subset(segs, result[iid].get(h["from"], [])):
                reason = "交出人并非持有各段全部实件"
            if reason:
                rejected.append({"transfer_id": h["transfer_id"], "reason": reason})
                continue
            result[iid][h["from"]] = subtract(result[iid].get(h["from"], []), segs)
            result[iid][h["to"]] = normalize(result[iid].get(h["to"], []) + segs)
        return result, rejected

    # ================================================================ 裁定草案
    def register_draft(self, draft_id: str, voyage_id: str, sacrifice: dict,
                       recognize: Optional[dict] = None) -> dict:
        """登记一份裁定草案：实际牺牲段 sacrifice，及本草案认可子段 recognize。"""
        if draft_id in self.drafts:
            raise ValidationError(f"草案 {draft_id} 已存在")
        if len(self.drafts) >= MAX_DRAFTS:
            raise ValidationError(f"一次至多 {MAX_DRAFTS} 份草案")
        if voyage_id not in self.voyages:
            raise ValidationError(f"未知航次 {voyage_id}")
        voyage = self.voyages[voyage_id]
        sac_norm: dict[str, Segments] = {}
        rec_norm: dict[str, Segments] = {}
        for iid, segs in sacrifice.items():
            if iid not in voyage["invoice_ids"]:
                raise ValidationError(f"发票 {iid} 不属于航次 {voyage_id}")
            q = self.invoices[iid]["q"]
            raw = _seg_load(segs)
            if has_overlap(raw):
                raise ValidationError(f"牺牲段 {iid} 内部重叠")
            norm = normalize(raw)
            if not within(norm, 1, q):
                raise ValidationError(f"牺牲段 {iid} 超出 1..{q}")
            sac_norm[iid] = norm
        if recognize is None:
            rec_norm = {iid: list(s) for iid, s in sac_norm.items()}
        else:
            for iid, segs in recognize.items():
                if iid not in sac_norm:
                    raise ValidationError(f"草案认可 {iid} 但该票无牺牲段")
                norm = normalize(_seg_load(segs))
                if not is_subset(norm, sac_norm[iid]):
                    raise ValidationError(f"认可段 {iid} 超出实际牺牲段")
                rec_norm[iid] = norm
        record = {
            "draft_id": draft_id, "voyage_id": voyage_id,
            "sacrifice": {iid: _seg_json(s) for iid, s in sac_norm.items()},
            "recognize": {iid: _seg_json(s) for iid, s in rec_norm.items()},
        }
        self.drafts[draft_id] = record
        self._save("ga_drafts", "draft_id", draft_id, record)
        return record

    def drafts_for(self, voyage_id: str) -> list[str]:
        return sorted(d["draft_id"] for d in self.drafts.values()
                      if d["voyage_id"] == voyage_id)

    # ================================================================ 共同海损
    def compute_ga(self, draft_id: str) -> dict:
        """按草案算各户净欠（应摊 − 应获偿）、补偿池、分摊基数与船方。"""
        draft = self.drafts[draft_id]
        voyage = self.voyages[draft["voyage_id"]]
        holdings_map, _ = self.holdings(draft["voyage_id"])

        salvage: dict[str, Fraction] = {}
        owed: dict[str, Fraction] = {}
        for iid in voyage["invoice_ids"]:
            inv = self.invoices[iid]
            unit = Fraction(inv["C"], inv["q"])
            sac = _seg_load(draft["sacrifice"].get(iid, []))
            rec = _seg_load(draft["recognize"].get(iid, []))
            for holder, held in holdings_map[iid].items():
                kept = subtract(held, sac)
                recognized = intersect(held, rec)
                salvage[holder] = salvage.get(holder, Fraction(0)) + count(kept) * unit
                owed[holder] = owed.get(holder, Fraction(0)) + count(recognized) * unit

        pool = sum(owed.values(), Fraction(0))
        base = {h: salvage.get(h, Fraction(0)) + owed.get(h, Fraction(0))
                for h in set(salvage) | set(owed)}
        ship_value = Fraction(voyage["ship_value"])
        total = sum(base.values(), Fraction(0)) + ship_value

        contribution: dict[str, Fraction] = {}
        net: dict[str, Fraction] = {}
        if total == 0:
            if pool != 0:
                raise ValidationError("总基数为零但补偿池非空，理算不成立")
            contribution = {h: Fraction(0) for h in base}
            net = {h: Fraction(0) for h in base}
            ship_contrib = Fraction(0)
        else:
            for h in base:
                contribution[h] = base[h] / total * pool
                net[h] = contribution[h] - owed.get(h, Fraction(0))
            ship_contrib = ship_value / total * pool

        return {
            "draft_id": draft_id, "voyage_id": draft["voyage_id"],
            "pool": pool, "total_base": total,
            "base": base,
            "salvage": {h: salvage.get(h, Fraction(0)) for h in base},
            "owed": {h: owed.get(h, Fraction(0)) for h in base},
            "contribution": contribution,
            "net": net,
            "ship_value": ship_value,
            "ship_contribution": ship_contrib,
            "ship_net": ship_contrib,
        }

    # ================================================================ 联保函
    def create_guarantee(self, guarantee_id: str, limit: int, members: Iterable[str]) -> dict:
        if guarantee_id in self.guarantees:
            raise ValidationError(f"联保函 {guarantee_id} 已存在")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ValidationError("联保函赔付总额必须是非 bool 非负整数")
        members = sorted(set(members))
        if not members:
            raise ValidationError("联保函至少涵盖一个申请人")
        record = {"guarantee_id": guarantee_id, "limit": limit, "members": members,
                  "drawn": []}
        self.guarantees[guarantee_id] = record
        self._save("ga_guarantees", "guarantee_id", guarantee_id, record)
        return record

    # ================================================================ 提货申请
    def create_claim(self, claim_id: str, voyage_id: str, applicant: str,
                     boxes: list[dict], cash_cap: int,
                     guarantee_id: Optional[str] = None) -> dict:
        """登记整箱提货申请。每箱整箱提取、不得跨货主、不得含已损货。"""
        if claim_id in self.claims:
            raise ValidationError(f"提货申请 {claim_id} 已存在")
        if voyage_id not in self.voyages:
            raise ValidationError(f"未知航次 {voyage_id}")
        if len(boxes) > MAX_BOXES:
            raise ValidationError(f"一次至多 {MAX_BOXES} 箱")
        if isinstance(cash_cap, bool) or not isinstance(cash_cap, int) or cash_cap < 0:
            raise ValidationError("现金上限必须是非 bool 非负整数")
        if guarantee_id is not None:
            g = self.guarantees.get(guarantee_id)
            if g is None:
                raise ValidationError(f"未知联保函 {guarantee_id}")
            if applicant not in g["members"]:
                raise ValidationError(f"联保函 {guarantee_id} 不覆盖 {applicant}")

        holdings_map, _ = self.holdings(voyage_id)
        applicant_held = [hold.get(applicant, []) for hold in holdings_map.values()]

        norm_boxes = []
        seen_ids = set()
        for b in boxes:
            bid = b["box_id"]
            if bid in seen_ids:
                raise ValidationError(f"箱号 {bid} 重复")
            seen_ids.add(bid)
            raw = _seg_load(b["segments"])
            if has_overlap(raw):
                raise ValidationError(f"箱 {bid} 内部重叠")
            segs = normalize(raw)
            if not segs:
                raise ValidationError(f"箱 {bid} 为空")
            # 必须整体由申请人持有：不得跨货主
            if not any(is_subset(segs, held) for held in applicant_held):
                raise ReleaseError(f"箱 {bid} 跨越货主或含非申请人持有件", {"box_id": bid})
            # 不得含任何草案中的实际牺牲件
            for draft in self.drafts.values():
                if draft["voyage_id"] != voyage_id:
                    continue
                for iid, sac in draft["sacrifice"].items():
                    if intersect(segs, _seg_load(sac)):
                        raise ReleaseError(f"箱 {bid} 含已损货", {"box_id": bid})
            nv = b.get("net_value")
            if nv is None:
                raise ValidationError(f"箱 {bid} 缺少留港可担保净值 net_value")
            if isinstance(nv, bool) or not isinstance(nv, int) or nv < 0:
                raise ValidationError("箱净值必须是非 bool 非负整数")
            norm_boxes.append({
                "box_id": bid,
                "segments": _seg_json(segs),
                "priority": int(b.get("priority", 0)),
                "net_value": nv,
            })

        record = {
            "claim_id": claim_id, "voyage_id": voyage_id, "applicant": applicant,
            "boxes": norm_boxes, "cash_cap": cash_cap,
            "guarantee_id": guarantee_id,
            "status": "open",
            "released": [], "kept": [], "kept_value": 0,
            "frozen_cash": 0, "guarantee_draw": 0,
            "draft_id": None, "history": [],
        }
        self.claims[claim_id] = record
        self._save("ga_claims", "claim_id", claim_id, record)
        return record

    # ================================================================ 险责册
    def liability_register(self, voyage_id: str, invoice_id: str,
                           segments: Iterable) -> dict:
        """货代独立出具的“事故当刻险责册”：核一段件号、过户来源、担险人及认可价值。

        - holder：事故当刻持有该段全部实件的担险人（段被拆散持有则分片列示）；
        - provenance：按 (time, seq) 影响该段的全部交接单（from→to）；
        - recognized_value：件数 × C/q（集团认可价值，Fraction）。
        """
        if voyage_id not in self.voyages:
            raise ValidationError(f"未知航次 {voyage_id}")
        if invoice_id not in self.voyages[voyage_id]["invoice_ids"]:
            raise ValidationError(f"发票 {invoice_id} 不属于航次 {voyage_id}")
        inv = self.invoices[invoice_id]
        target = normalize(_seg_load(segments))
        if not within(target, 1, inv["q"]):
            raise ValidationError(f"查询段超出 1..{inv['q']}")
        held, _ = self.holdings(voyage_id)

        slices = []
        remaining = list(target)
        for holder, holder_segs in held[invoice_id].items():
            piece = intersect(target, holder_segs)
            if piece:
                remaining = subtract(remaining, piece)
                slices.append({"holder": holder, "segments": _seg_json(piece),
                               "pieces": count(piece)})
        if remaining:
            raise ReleaseError("查询段含已损失件或未被持有", {"missing": _seg_json(remaining)})

        provenance = []
        for h in sorted((h for h in self.handovers if h["voyage_id"] == voyage_id),
                        key=lambda h: (h["time"], h["seq"])):
            if h["invoice_id"] != invoice_id:
                continue
            if intersect(target, _seg_load(h["segments"])):
                provenance.append({"transfer_id": h["transfer_id"], "time": h["time"],
                                   "seq": h["seq"], "from": h["from"], "to": h["to"],
                                   "segments": h["segments"]})

        unit = Fraction(inv["C"], inv["q"])
        return {
            "voyage_id": voyage_id, "invoice_id": invoice_id,
            "query_segments": _seg_json(target),
            "pieces": count(target),
            "unit_value": frac_to_json(unit),
            "recognized_value": frac_to_json(unit * count(target)),
            "holders": slices,
            "provenance": provenance,
        }

    # ---------------------------------------------------- 放行评估/执行
    @staticmethod
    def _coverage_options(w: Fraction, keep_value: int, cash_cap: int) -> list[tuple[int, Fraction]]:
        """给定留箱值，枚举 (现金, 保函) 组合，按先现金后函。

        现金为整数分、可为 0..min(上限, ⌊所需⌋) 的任意值；余额（精确有理数，含小数分）
        由函覆盖。因此“全冻现金”和“全用函”都会出现（所需为整数时），供同分时码头自选。
        """
        residual = w - keep_value
        if residual <= 0:
            return [(0, Fraction(0))]
        top = min(cash_cap, _floor_fen(residual))
        out: list[tuple[int, Fraction]] = []
        for cash in range(top + 1):
            after = residual - cash
            out.append((cash, after if after > 0 else Fraction(0)))
        return out

    def _guarantee_remaining_for_draft(self, guarantee_id: str, draft_id: str,
                                       exclude_claim: str) -> Fraction:
        """同一份裁定下的联保函余额。

        已按实际裁定赔付的款项永远占用额度；同一草案内的模拟抽款也占用；
        另一草案的模拟抽款不占用——互斥草案绝不能相加。
        """
        g = self.guarantees[guarantee_id]
        used = Fraction(0)
        for d in g["drawn"]:
            if d["claim_id"] == exclude_claim and not d.get("settled"):
                continue
            if d.get("settled") or d["draft_id"] == draft_id:
                used += json_to_frac(d["amount"])
        return Fraction(g["limit"]) - used

    def _settled_guarantee_use(self) -> dict[str, Fraction]:
        """各联保函已按真实裁定赔付、永久占用额度的金额。"""
        used: dict[str, Fraction] = {}
        for gid, g in self.guarantees.items():
            s = Fraction(0)
            for d in g["drawn"]:
                if d.get("settled"):
                    s += json_to_frac(d["amount"])
            used[gid] = s
        return used

    def _underwrite_group(self, voyage_id: str, draft_id: str, ga: dict,
                          override: Optional[dict] = None) -> dict:
        """把本航次全部尚未结清的放货申请人放在同一份裁定下合并核保。

        - 每户可得担保 = 留箱净值 + 冻结现金 + 本裁定下联保函抽取；
        - 已放行未结清者按现有留箱/冻款现值，当前候选者用 override；
        - 同一张函在本裁定下的抽款合计（含已永久赔付部分）不超过可赔付总额；
        - 不同裁定下的模拟抽款绝不相加（互斥，只有一份是实际裁定）。
        """
        override = override or {}
        settled_used = self._settled_guarantee_use()
        per_guarantee: dict[str, Fraction] = {gid: used for gid, used in settled_used.items()}
        problems: list[dict] = []
        claims = [c for c in self.claims.values()
                  if c["voyage_id"] == voyage_id and c["status"] != "settled"]
        for c in claims:
            cid = c["claim_id"]
            w = ga["net"].get(c["applicant"], Fraction(0))
            if cid == override.get("claim_id"):
                kv = Fraction(override["keep_value"])
                cash = Fraction(override["cash_frozen"])
                draw = Fraction(override["guarantee_draw"])
            elif c["status"] == "open":
                # 已申请未放行：全部箱仍在港，可整体作保；现金未冻、函未抽
                kv = Fraction(sum(b["net_value"] for b in c["boxes"]))
                cash = Fraction(0)
                need = w - kv
                draw = Fraction(_ceil_fen(need)) if need > 0 else Fraction(0)
            else:
                # 已放行未结清：按其现值（在港作保箱值 + 冻结现金）重算函需
                kv = Fraction(c.get("kept_value", 0))
                cash = Fraction(c.get("frozen_cash", 0))
                need = w - kv - cash
                draw = Fraction(_ceil_fen(need)) if need > 0 else Fraction(0)
            covered = kv + cash + draw
            if w > 0 and covered < w:
                problems.append({"claim_id": cid, "applicant": c["applicant"],
                                 "net": frac_to_json(w), "secured": frac_to_json(covered)})
                continue
            if draw > 0:
                if not c["guarantee_id"]:
                    problems.append({"claim_id": cid, "applicant": c["applicant"],
                                     "reason": "无联保函却需函覆盖"})
                    continue
                gid = c["guarantee_id"]
                if c["applicant"] not in self.guarantees[gid]["members"]:
                    problems.append({"claim_id": cid, "applicant": c["applicant"],
                                     "reason": "联保函不覆盖该申请人"})
                    continue
                per_guarantee[gid] = per_guarantee.get(gid, Fraction(0)) + draw
        for gid, need in per_guarantee.items():
            if need > self.guarantees[gid]["limit"]:
                problems.append({"guarantee_id": gid, "reason": "同裁定抽款超过函额",
                                 "need": frac_to_json(need),
                                 "limit": self.guarantees[gid]["limit"]})
        return {"ok": not problems, "problems": problems,
                "per_guarantee": {g: frac_to_json(v) for g, v in per_guarantee.items()}}

    def evaluate_release(self, claim_id: str, draft_id: str) -> dict:
        """在指定草案下求最高总优先分的放行方案（含同分组合）。

        每个候选方案都要把本航次全部未结清申请人放在同一份裁定下合并核保：
        本人方案用候选的留箱/现金/函，其他人用其现有保全并按本裁定重算函需，
        同一张联保函合计不超过函额。互斥草案之间绝不相加。
        """
        claim = self.claims.get(claim_id)
        if claim is None:
            raise ValidationError(f"未知提货申请 {claim_id}")
        ga = self.compute_ga(draft_id)
        applicant = claim["applicant"]
        w = ga["net"].get(applicant, Fraction(0))
        available = [b for b in claim["boxes"] if b["box_id"] not in claim["released"]]

        options: list[dict] = []
        last_problems: list = []
        n = len(available)
        for mask in range(1 << n):
            keep = [available[i] for i in range(n) if mask & (1 << i)]
            release = [available[i] for i in range(n) if not (mask & (1 << i))]
            priority = sum(b["priority"] for b in release)
            keep_value = sum(b["net_value"] for b in keep)
            for cash, draw in self._coverage_options(w, keep_value, claim["cash_cap"]):
                under = self._underwrite_group(
                    claim["voyage_id"], draft_id, ga,
                    override={"claim_id": claim_id, "keep_value": keep_value,
                              "cash_frozen": cash, "guarantee_draw": draw},
                )
                if under["ok"]:
                    options.append({
                        "keep": sorted(b["box_id"] for b in keep),
                        "release": sorted(b["box_id"] for b in release),
                        "priority": priority,
                        "keep_value": keep_value,
                        "cash_frozen": cash,
                        "guarantee_draw": draw,
                    })
                elif not last_problems:
                    last_problems = under["problems"]

        if not options:
            return {
                "claim_id": claim_id, "draft_id": draft_id,
                "applicant": applicant, "applicant_net": frac_to_json(w),
                "max_priority": None, "options": [], "all_options": [],
                "all_best": [], "infeasible": True,
                "problems": last_problems,
            }
        best = max(o["priority"] for o in options)
        best_opts = sorted(
            (o for o in options if o["priority"] == best),
            key=lambda o: (len(o["keep"]), o["cash_frozen"], o["guarantee_draw"],
                           o["release"]),
        )
        return {
            "claim_id": claim_id, "draft_id": draft_id,
            "applicant": applicant, "applicant_net": frac_to_json(w),
            "max_priority": best, "options": best_opts, "all_options": options,
            "all_best": [o for o in options if o["priority"] == best],
            "infeasible": False, "problems": [],
        }

    def release(self, claim_id: str, draft_id: str, keep: Optional[list[str]] = None,
                cash_frozen: Optional[int] = None,
                guarantee_draw: Optional[int] = None) -> dict:
        """按评估实际放行；不指定则取同分中最省留箱/现金/函的一组。"""
        claim = self.claims[claim_id]
        # 已放行时：完全相同的调用按原单复读，不重复收费、不重复冻结
        if claim["status"] in ("released", "settled") and claim["history"]:
            last = claim["history"][-1]
            if last.get("action") == "release" and last.get("draft_id") == draft_id:
                keep_ok = keep is None or sorted(keep) == sorted(last.get("keep", []))
                cash_ok = cash_frozen is None or cash_frozen == last.get("cash_frozen")
                draw_ok = guarantee_draw is None or guarantee_draw == last.get("guarantee_draw")
                if keep_ok and cash_ok and draw_ok:
                    return {
                        "claim_id": claim_id, "draft_id": draft_id,
                        "priority": sum(b["priority"] for b in claim["boxes"]
                                        if b["box_id"] in last.get("release", [])),
                        "released": list(last.get("release", [])),
                        "in_port": list(last.get("keep", [])),
                        "cash_frozen": last.get("cash_frozen"),
                        "guarantee_draw": last.get("guarantee_draw"),
                        "kept_value": claim.get("kept_value", 0),
                        "status": "replayed",
                    }
        evaluation = self.evaluate_release(claim_id, draft_id)
        if evaluation["infeasible"]:
            raise ReleaseError("本草案下无法足额担保，不能放行", evaluation)
        if keep is None and cash_frozen is None and guarantee_draw is None:
            chosen = evaluation["options"][0]
        else:
            chosen = next(
                (o for o in evaluation["all_options"]
                 if sorted(o["keep"]) == sorted(keep or [])
                 and (cash_frozen is None or o["cash_frozen"] == cash_frozen)
                 and (guarantee_draw is None or o["guarantee_draw"] == guarantee_draw)),
                None,
            )
            if chosen is None:
                raise ReleaseError("指定的留箱/现金/保函组合不可行或非本草案", evaluation)

        new_release = [b for b in chosen["release"] if b not in claim["released"]]
        if not new_release and claim["released"]:
            # 完全相同的复读：按原单复读，而不是重复收费
            last = claim["history"][-1] if claim["history"] else None
            if (last is not None and last.get("action") == "release"
                    and last.get("draft_id") == draft_id
                    and sorted(last.get("release", [])) == sorted(chosen["release"])
                    and sorted(last.get("keep", [])) == sorted(chosen["keep"])
                    and last.get("cash_frozen") == chosen["cash_frozen"]
                    and last.get("guarantee_draw") == chosen["guarantee_draw"]):
                return {
                    "claim_id": claim_id, "draft_id": draft_id,
                    "priority": chosen["priority"],
                    "released": chosen["release"], "in_port": chosen["keep"],
                    "cash_frozen": chosen["cash_frozen"],
                    "guarantee_draw": chosen["guarantee_draw"],
                    "kept_value": chosen["keep_value"],
                    "status": "replayed",
                }
            raise ReleaseError("重复放货：这些箱已在先前放行", {"claim_id": claim_id})
        for other in self.claims.values():
            if other["claim_id"] == claim_id:
                continue
            clash = set(other["released"]) & set(chosen["release"])
            if clash:
                raise ReleaseError("重复放货：箱号已被其他申请放行",
                                   {"box_ids": sorted(clash)})

        claim["released"] = sorted(set(claim["released"]) | set(chosen["release"]))
        claim["kept"] = sorted(chosen["keep"])
        claim["kept_value"] = chosen["keep_value"]
        claim["frozen_cash"] = chosen["cash_frozen"]
        claim["guarantee_draw"] = chosen["guarantee_draw"]
        claim["draft_id"] = draft_id
        claim["status"] = "released"
        claim["history"].append({
            "draft_id": draft_id, "action": "release",
            "release": chosen["release"], "keep": chosen["keep"],
            "cash_frozen": chosen["cash_frozen"], "guarantee_draw": chosen["guarantee_draw"],
        })
        self._save("ga_claims", "claim_id", claim_id, claim)

        if claim["guarantee_id"] and chosen["guarantee_draw"] > 0:
            g = self.guarantees[claim["guarantee_id"]]
            # 覆盖本申请此前的模拟抽款，只保留本次方案
            g["drawn"] = [d for d in g["drawn"]
                          if not (d["claim_id"] == claim_id and not d.get("settled"))]
            g["drawn"].append({"claim_id": claim_id, "draft_id": draft_id,
                               "amount": chosen["guarantee_draw"]})
            self._save("ga_guarantees", "guarantee_id", claim["guarantee_id"], g)

        return {
            "claim_id": claim_id, "draft_id": draft_id,
            "priority": chosen["priority"],
            "released": chosen["release"], "in_port": chosen["keep"],
            "cash_frozen": chosen["cash_frozen"],
            "guarantee_draw": chosen["guarantee_draw"],
            "kept_value": chosen["keep_value"],
        }

    # ================================================================ 结算
    def settle(self, claim_id: str, draft_id: str) -> dict:
        """按实际收到的裁定据实结算：列净欠、留箱担保、现金实扣/应退、余缺向函索取。

        同一户顺序：先留箱，再现金，最后函。此前另一份模拟草案不得动用一分钱。
        """
        claim = self.claims.get(claim_id)
        if claim is None:
            raise ValidationError(f"未知提货申请 {claim_id}")
        voyage_id = claim["voyage_id"]
        # 同一航次只认一份实际裁定；另一份模拟草案不得动用一分钱
        locked = self.rulings.get(voyage_id)
        if locked is not None and locked != draft_id:
            raise ReleaseError("本航次已按另一份裁定结算，不能跨草案动用款项", {
                "voyage_id": voyage_id, "actual": locked, "attempted": draft_id})
        ga = self.compute_ga(draft_id)
        applicant = claim["applicant"]
        w = ga["net"].get(applicant, Fraction(0))

        kept_value = sum(
            {b["box_id"]: b for b in claim["boxes"]}[bid]["net_value"]
            for bid in claim["kept"]
        )
        frozen = claim["frozen_cash"]

        box_used = Fraction(0)
        cash_used = 0
        guarantee_used = Fraction(0)
        if w > 0:
            box_used = min(Fraction(kept_value), w)
            after_box = w - box_used
            if after_box > 0:
                # 现金为整数分，绝不超出所需（避免多扣），余额交函
                cash_used = min(frozen, _floor_fen(after_box))
                after_cash = after_box - cash_used
                if after_cash > 0:
                    guarantee_used = after_cash
            covered = box_used + cash_used + guarantee_used
            if covered < w:
                raise ReleaseError("实际裁定下担保不足，不能结算",
                                   {"short": frac_to_json(w - covered)})
            if guarantee_used > 0:
                if claim["guarantee_id"] is None:
                    raise ReleaseError("无联保函却需向保函索取")
                g = self.guarantees[claim["guarantee_id"]]
                # 同一份实际裁定下，本函剩余额度（含已永久赔付占用）必须够付
                remaining = self._guarantee_remaining_for_draft(
                    claim["guarantee_id"], draft_id, claim_id)
                if guarantee_used > remaining:
                    raise ReleaseError("本裁定下联保函额度不足", {
                        "guarantee_id": claim["guarantee_id"],
                        "need": frac_to_json(guarantee_used),
                        "remaining": frac_to_json(remaining)})

        refund = frozen - cash_used
        statement = {
            "claim_id": claim_id, "draft_id": draft_id, "applicant": applicant,
            "per_household_net": {h: frac_to_json(v) for h, v in ga["net"].items()},
            "ship_net": frac_to_json(ga["ship_net"]),
            "pool": frac_to_json(ga["pool"]),
            "total_base": frac_to_json(ga["total_base"]),
            "applicant_net": frac_to_json(w),
            "secured_by_kept_boxes": frac_to_json(box_used),
            "kept_boxes": list(claim["kept"]),
            "cash_frozen": frozen,
            "cash_deducted": cash_used,
            "cash_refund": refund,
            "guarantee_claim": frac_to_json(guarantee_used),
            "guarantee_held_by": claim["guarantee_id"] if guarantee_used > 0 else None,
            "cash_held_by": "port" if cash_used > 0 else None,
            "in_port": list(claim["kept"]),
            "released_out": list(claim["released"]),
            "drafts_considered": sorted({h["draft_id"] for h in claim["history"]}),
        }
        claim["status"] = "settled"
        claim["settled_draft"] = draft_id
        self._save("ga_claims", "claim_id", claim_id, claim)
        self.settlements[claim_id] = statement
        self._save("ga_settlements", "claim_id", claim_id, statement)
        if voyage_id not in self.rulings:
            self.rulings[voyage_id] = draft_id
            self.conn.execute(
                "INSERT INTO ga_rulings (voyage_id, draft_id) VALUES (?, ?)",
                (voyage_id, draft_id),
            )
            if self.conn.in_transaction:
                self.conn.execute("COMMIT")

        if claim["guarantee_id"] and guarantee_used > 0:
            g = self.guarantees[claim["guarantee_id"]]
            # 结算把本申请所有未结算的模拟抽款清除，只保留这份实际裁定的真实赔付
            g["drawn"] = [d for d in g["drawn"]
                          if not (d["claim_id"] == claim_id and not d.get("settled"))]
            g["drawn"].append({"claim_id": claim_id, "draft_id": draft_id,
                               "amount": frac_to_json(guarantee_used), "settled": True})
            self._save("ga_guarantees", "guarantee_id", claim["guarantee_id"], g)
        elif claim["guarantee_id"]:
            # 本申请按实际裁定无需用函：清掉它此前的模拟抽款
            g = self.guarantees[claim["guarantee_id"]]
            g["drawn"] = [d for d in g["drawn"]
                          if not (d["claim_id"] == claim_id and not d.get("settled"))]
            self._save("ga_guarantees", "guarantee_id", claim["guarantee_id"], g)
        return statement

    # ================================================================ 查询
    def guarantee_usage(self, guarantee_id: str) -> dict:
        g = self.guarantees[guarantee_id]
        settled = Fraction(0)
        per_draft: dict[str, Fraction] = {}
        for d in g["drawn"]:
            amt = d["amount"]
            amt = amt if isinstance(amt, Fraction) else json_to_frac(amt)
            per_draft[d["draft_id"]] = per_draft.get(d["draft_id"], Fraction(0)) + amt
            if d.get("settled"):
                settled += amt
        return {
            "guarantee_id": guarantee_id, "limit": g["limit"],
            "settled_used": frac_to_json(settled),
            "per_draft": {k: frac_to_json(v) for k, v in per_draft.items()},
            "members": g["members"],
        }
