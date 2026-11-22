"""点名册：按固定议程逐案核票，委托链追溯与委托证逐张扣额。

规则（题面固化）：

* 本人对某案的同意/反对只代表本人实际留在该户的份额；
* 没有本人签字时，沿该出资户的**唯一委托链**找本案签字人；
* 委托证按原出资户登记：同一出资户的各案按议程共用其证额，不同出资户
  各自扣；每张证限定可签哪些案、全场可签金额；
* 途中无证、超范围或回环未遇签字 → 权益全弃权；
* 有签字时，只将 ``min(实际权益, 途经各证剩额)`` 交签，其余弃权；
* 途经证按实际签出的同意/反对金额逐张扣额，否决案也扣；
* 对每户以全部留用额为分母核同意门槛，达到才批；零留用的案不划拨。
"""

from fractions import Fraction

from ..rationals import qstr
from .store import ArtifactStore

ROLLCALL_PREFIX = "roll"


class RollcallService:
    def __init__(self, store_root):
        self.store = ArtifactStore(store_root, "点名册", ROLLCALL_PREFIX,
                                   fingerprint_field="rollcall_fingerprint")

    # ------------------------------------------------- 委托链追溯（单案单户）
    @staticmethod
    def _delegate_chain(session, funder):
        """返回 funder 的唯一委托链（不含 funder 本人，按委托顺序）。"""
        delegate_of = {d["funder"]: d["delegate"] for d in session["delegations"]}
        chain = []
        seen = {funder}
        cur = delegate_of.get(funder)
        while cur is not None:
            if cur in seen:  # 结构校验已拒绝回环，这里防御性处理
                return chain, True
            seen.add(cur)
            chain.append(cur)
            cur = delegate_of.get(cur)
        return chain, False

    def _sign_one(self, session, funder, case, interest, caps, sig_lookup):
        """核算一个出资户对一个案的交签结果，并就地扣减途经证剩额。"""
        case_id = case["case_id"]
        personal = sig_lookup.get((funder, case_id))
        if personal is not None:
            # 本人签字：只代表本人实际留在此户的份额，不涉及委托证
            return {
                "funder": funder, "case_id": case_id,
                "mode": "personal", "signer": funder, "vote": personal,
                "interest": qstr(interest), "signed": qstr(interest),
                "abstained": "0", "credentials_used": [],
            }
        if interest == 0:
            return {
                "funder": funder, "case_id": case_id,
                "mode": "abstain", "signer": None, "vote": None,
                "interest": "0", "signed": "0", "abstained": "0",
                "credentials_used": [], "reason": "该户在此受款户无实际权益",
            }

        chain, cyclic = self._delegate_chain(session, funder)
        if cyclic:
            return self._abstain(funder, case_id, interest, [], "委托链回环未遇签字")
        if not chain:
            return self._abstain(funder, case_id, interest, [], "无本人签字且无委托链")

        # 途经证：委托链上每一跳都要持有原出资户的有效证（限定案范围 + 剩额）
        cred = next((c for c in session["credentials"] if c["funder"] == funder), None)
        if cred is None:
            return self._abstain(funder, case_id, interest, chain, "途中无证")
        if case_id not in cred["case_scope"]:
            return self._abstain(funder, case_id, interest, chain,
                                 f"委托证不可签本案（允许 {sorted(cred['case_scope'])}）")
        remaining = caps[funder]
        if remaining <= 0:
            return self._abstain(funder, case_id, interest, chain, "委托证额已用尽")

        signer = None
        for member in chain:
            vote = sig_lookup.get((member, case_id))
            if vote is not None:
                signer = member
                break
        if signer is None:
            return self._abstain(funder, case_id, interest, chain, "委托链未遇本案签字人")

        signed = min(interest, remaining)
        caps[funder] = remaining - signed  # 同一出资户各案共用证额，逐案扣
        return {
            "funder": funder, "case_id": case_id,
            "mode": "delegated", "signer": signer, "vote": vote,
            "interest": qstr(interest), "signed": qstr(signed),
            "abstained": qstr(interest - signed),
            "credentials_used": [
                {"funder": funder, "case_scope": list(cred["case_scope"]),
                 "deducted": qstr(signed), "remaining_after": qstr(caps[funder])}
            ],
        }

    @staticmethod
    def _abstain(funder, case_id, interest, chain, reason):
        return {
            "funder": funder, "case_id": case_id,
            "mode": "abstain", "signer": None, "vote": None,
            "interest": qstr(interest), "signed": "0",
            "abstained": qstr(interest), "credentials_used": [],
            "delegate_chain": chain, "reason": reason,
        }

    # --------------------------------------------------------------- 建册
    def build_rollcall(self, ledger):
        session = ledger["session"]
        if not ledger["settled"]:
            from ..errors import LedgerNotSettledError

            raise LedgerNotSettledError(
                "去向册未结清，不能建立点名册",
                [i["funder"] for i in ledger["trapped_sources"]],
            )
        retained = {f: {a: Fraction(v) for a, v in row.items()}
                    for f, row in ledger["retained_by_source"].items()}
        sig_lookup = {(s["funder"], s["case_id"]): s["vote"]
                      for s in session["signatures"]}
        caps = {c["funder"]: Fraction(c["cap"]) for c in session["credentials"]}
        cap_audit = {c["funder"]: {"initial": c["cap"], "case_scope": list(c["case_scope"])}
                     for c in session["credentials"]}

        cases = []
        for case in session["agenda"]:  # 登记顺序即固定点名议程
            account = case["account"]
            total_retained = sum(
                (retained[f].get(account, Fraction(0)) for f in session["funders"]),
                Fraction(0))
            records = []
            yes = no = Fraction(0)
            for funder in session["funders"]:
                interest = retained[funder].get(account, Fraction(0))
                record = self._sign_one(
                    session, funder, case, interest, caps, sig_lookup)
                records.append(record)
                if record["vote"] == "yes":
                    yes += Fraction(record["signed"])
                elif record["vote"] == "no":
                    no += Fraction(record["signed"])
            threshold = Fraction(case["threshold"])
            approved = total_retained > 0 and total_retained * threshold <= yes
            cases.append({
                "case_id": case["case_id"],
                "agenda_index": case["agenda_index"],
                "account": account,
                "purpose": case["purpose"],
                "need": case["need"],
                "reserve": case["reserve"],
                "threshold": case["threshold"],
                "total_retained": qstr(total_retained),
                "yes_amount": qstr(yes),
                "no_amount": qstr(no),
                "yes_share": qstr(yes / total_retained) if total_retained > 0 else None,
                "approved": approved,
                "zero_retention_no_allocation": total_retained == 0,
                "records": records,
            })

        for funder, info in cap_audit.items():
            info["remaining_after_session"] = qstr(caps[funder])
            info["total_deducted"] = qstr(Fraction(info["initial"]) - caps[funder])

        rollcall = {
            "artifact_kind": "allocation_rollcall",
            "schema_version": 1,
            "session_id": session["session_id"],
            "ledger_id": ledger.get("ledger_id"),
            "ledger_fingerprint": ledger["ledger_fingerprint"],
            "agenda_order": [c["case_id"] for c in session["agenda"]],
            "cases": cases,
            "credential_audit": cap_audit,
            "approved_cases": [c["case_id"] for c in cases if c["approved"]],
            "rejected_cases": [c["case_id"] for c in cases if not c["approved"]],
        }
        roll_id = self.store.next_id()
        return self.store.save(roll_id, rollcall)

    def get_rollcall(self, roll_id):
        return self.store.load(roll_id)

    def list_rollcalls(self):
        return self.store.list_ids()
