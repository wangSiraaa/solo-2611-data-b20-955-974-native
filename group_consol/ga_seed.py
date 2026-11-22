"""把题目给定的一张内部发票经“原入账入口”（EntityLedger.post）真实入账。

P 记卖方四行、S 记买方两行，文档镜像同票；这正是 group_consol 的局部校验口径，
不绕过任何校验。返回两侧 heads，供后续按 heads 取镜像核对。
"""

from __future__ import annotations

from group_consol.ledger import EntityLedger


SALE_DOC = {
    "type": "internal_sale",
    "invoice_id": "i1",
    "seller": "P",
    "buyer": "S",
    "sku": "X",
    "q": 4,
    "T": 80,
    "C": 40,
}


def seed_invoice(ledger: EntityLedger, invoice: dict, p_id: str = "seller-i1",
                 s_id: str = "buyer-i1") -> dict:
    """经原入账入口提交一张内部发票的双侧镜像。invoice 含 invoice_id/seller/buyer/sku/q/T/C。"""
    q, t, c = invoice["q"], invoice["T"], invoice["C"]
    doc_common = {
        "type": "internal_sale",
        "invoice_id": invoice["invoice_id"],
        "seller": invoice["seller"],
        "buyer": invoice["buyer"],
        "sku": invoice["sku"],
        "q": q,
        "T": t,
        "C": c,
    }
    seller_lines = [
        ["receivable", t, 0],
        ["revenue", 0, t],
        ["cost", c, 0],
        ["inventory", 0, c],
    ]
    buyer_lines = [
        ["inventory", t, 0],
        ["payable", 0, t],
    ]
    ledger.post(p_id, invoice["seller"], seller_lines, {**doc_common, "side": "seller"})
    ledger.post(s_id, invoice["buyer"], buyer_lines, {**doc_common, "side": "buyer"})
    return ledger.heads()


def seed_opening(ledger: EntityLedger, entity: str, cash: int, inventory: int = 0,
                 journal_id: str = "opening") -> None:
    lines = []
    if cash:
        lines.append(["cash", cash, 0])
    if inventory:
        lines.append(["inventory", inventory, 0])
    lines.append(["capital", 0, cash + inventory])
    ledger.post(journal_id, entity, lines)
