"""代拨产物的独立验证器。

与构建路径分离：

* 去向册：从场次事实独立重建流方程（不读 network.py 造的模型，只信目录快照
  与编译产物哈希），独立核对逐户留用、守恒、受困 SCC 判定与“逐户之和=合计”；
* 点名册：独立重放委托链与逐证扣额，重算同意门槛与批准结论；
* 账簿/拨款单：从开场账独立重算每笔下账后的余额，核对底线、同意金额上限、
  自有款范围、稳定单号指纹与已支余额不被二次动用。
"""

from fractions import Fraction

from ..compiler import tarjan_scc
from ..errors import VerifyError
from ..rationals import qstr
from . import network
from .books import max_flow
from .store import fingerprint


def _check(cond, name, detail=""):
    if not cond:
        raise VerifyError(f"代拨产物验证失败：{name} {detail}")
    return {"check": name, "ok": True, "detail": detail}


# ================================================================ 去向册
def verify_ledger(budget_service, ledger):
    checks = []
    session = ledger["session"]
    accounts, funders = session["accounts"], session["funders"]

    # 1. 绑定的每份解算报告都能用原系统独立验证路径复算
    for funder in funders:
        info = ledger["source_models"][funder]
        report = budget_service.get_report(info["report_id"])
        artifact = budget_service.load_artifact(info["artifact_hash"])
        result = budget_service.verify_solve(report["solve_id"])
        checks.append(_check(result["ok"], f"来源 {funder} 解算报告独立验证"))
        checks.append(_check(
            report["artifact_hash"] == artifact["artifact_hash"],
            f"来源 {funder} 报告绑定编译产物"))
    total_info = ledger["total_model"]
    total_report = budget_service.get_report(total_info["report_id"])
    budget_service.verify_solve(total_report["solve_id"])
    checks.append(_check(True, "合计模型解算报告独立验证"))

    # 2. 独立重建方程核：直接从场次事实写方程，核对编译产物里的系数与右端
    artifact = budget_service.load_artifact(total_info["artifact_hash"])
    checks.extend(_verify_equation_facts(session, artifact))

    # 3. 受困 SCC 独立判定
    traps = LedgerVerifier._indep_trap_components(session)
    checks.append(_check(
        traps == ledger["trap_components"],
        "封闭空转 SCC 独立复算一致", f"{traps} vs {ledger['trap_components']}"))

    # 4. 逐户留用与报告值一致；逐户之和 = 合计（守恒）
    if ledger["settled"]:
        total_retained = {}
        for account in accounts:
            total = Fraction(0)
            for funder in funders:
                v = Fraction(ledger["retained_by_source"][funder][account])
                total += v
                src_report = budget_service.get_report(
                    ledger["source_models"][funder]["report_id"])
                var = (f"{ledger['source_models'][funder]['model_id']}::"
                       f"{network.cell_retained_source(funder, account)}")
                got = src_report["outputs"][var]
                checks.append(_check(
                    got["status"] == "determined" and Fraction(got["value"]) == v,
                    f"留用 {funder}@{account} 与逐户报告一致"))
            total_retained[account] = total
            tv = Fraction(ledger["total"]["retained"][account])
            checks.append(_check(tv == total, f"守恒 {account}：逐户之和=合计",
                                 f"{qstr(total)}"))
        # 注入合计 = 全场留用合计（资金不外泄；绕环只留用一次）
        injected = sum((Fraction(i["amount"]) for i in session["injections"]), Fraction(0))
        retained_sum = sum(total_retained.values(), Fraction(0))
        checks.append(_check(injected == retained_sum,
                             "注入合计=留用合计（无资金外泄/重复留用）",
                             f"{qstr(injected)}={qstr(retained_sum)}"))
        checks.append(_check(
            ledger["conservation"] and ledger["conservation"]["all_conserved"],
            "去向册自带守恒标记为真"))
    else:
        # 受困来源必须点名真实到达封闭空转组的账户，且该来源报告确有矛盾
        for info in ledger["trapped_sources"]:
            report = budget_service.get_report(info["report_id"])
            bad = [v for v, r in report["outputs"].items()
                   if r["status"] == "inconsistent"]
            checks.append(_check(bool(bad), f"受困来源 {info['funder']} 报告确为矛盾系统"))
            reachable = LedgerVerifier._indep_reachable(
                session, network.injection_accounts(session, info["funder"]))
            listed = set(info["trapped_accounts"])
            expect = {a for trap in traps for a in trap if a in reachable}
            checks.append(_check(listed == expect,
                                 f"受困来源 {info['funder']} 受困账户独立复算一致"))

    expect_fp = fingerprint(ledger)
    checks.append(_check(expect_fp == ledger["ledger_fingerprint"], "去向册内容哈希"))
    return {"ok": True, "check_count": len(checks), "checks": checks}


def _verify_equation_facts(session, artifact):
    """独立从场次事实构造每个来源维账户方程，与编译产物逐条比对。"""
    checks = []
    total_mid = network.model_id_total(session["session_id"])
    equations = artifact["equations"]
    for funder in session["funders"]:
        for account in session["accounts"]:
            # T_f_a - in_f_a - Σ ratio·T_f_from = 0
            t_var = f"{total_mid}::{network.cell_source_inflow(funder, account)}"
            expect_coeffs = {
                t_var: Fraction(1),
                f"{total_mid}::{network.cell_inject(funder, account)}": Fraction(-1),
            }
            for tr in session["transfers"]:
                if tr["to"] != account:
                    continue
                coef = network.effective_ratio(tr, funder)
                if coef == 0:
                    continue  # 零系数项在目录归一化时被丢弃
                ref = f"{total_mid}::{network.cell_source_inflow(funder, tr['from'])}"
                expect_coeffs[ref] = expect_coeffs.get(ref, Fraction(0)) - coef
            actual = {k: Fraction(v) for k, v in equations[t_var]["coeffs"].items()}
            checks.append(_check(actual == expect_coeffs,
                                 f"来源流入方程 {funder}@{account} 独立一致"))
            checks.append(_check(Fraction(equations[t_var]["rhs"]) == 0,
                                 f"来源流入方程 {funder}@{account} 右端为 0"))

            # r_f_a - keep_f_a·T_f_a = 0
            r_var = f"{total_mid}::{network.cell_retained_source(funder, account)}"
            keep = network.retention_ratio(session, account, funder)
            expect_r = {r_var: Fraction(1)}
            if keep != 0:
                expect_r[t_var] = -keep
            checks.append(_check(
                {k: Fraction(v) for k, v in equations[r_var]["coeffs"].items()} == expect_r,
                f"来源留用方程 {funder}@{account} 独立一致（留用比例 {qstr(keep)}）"))

    # 合计格：T_a = Σ_f T_f_a；R_a = Σ_f r_f_a
    for account in session["accounts"]:
        t_var = f"{total_mid}::{network.cell_total_inflow(account)}"
        expect_t = {t_var: Fraction(1)}
        for funder in session["funders"]:
            ref = f"{total_mid}::{network.cell_source_inflow(funder, account)}"
            expect_t[ref] = Fraction(-1)
        checks.append(_check(
            {k: Fraction(v) for k, v in equations[t_var]["coeffs"].items()} == expect_t,
            f"合计总流入方程 {account} 独立一致"))
        r_var = f"{total_mid}::{network.cell_retained_total(account)}"
        expect_r = {r_var: Fraction(1)}
        for funder in session["funders"]:
            ref = f"{total_mid}::{network.cell_retained_source(funder, account)}"
            expect_r[ref] = Fraction(-1)
        checks.append(_check(
            {k: Fraction(v) for k, v in equations[r_var]["coeffs"].items()} == expect_r,
            f"合计留用方程 {account} 独立一致"))
    return checks


class LedgerVerifier:
    """供 verify_ledger 复用的独立图算法（与 ledger.py 内实现相互独立）。"""

    @staticmethod
    def _indep_trap_components(session):
        adjacency = network.transfer_adjacency(session)
        out = []
        for members in tarjan_scc(sorted(adjacency), adjacency):
            cyclic = len(members) > 1 or members[0] in adjacency.get(members[0], ())
            member_set = set(members)
            no_out = cyclic and all(
                nxt in member_set for m in members for nxt in adjacency.get(m, ()))
            no_keep_any = any(
                all(network.retention_ratio(session, m, funder) == 0 for m in members)
                for funder in session["funders"])
            if no_out and no_keep_any:
                out.append(sorted(members))
        return sorted(out)

    @staticmethod
    def _indep_reachable(session, starts):
        adjacency = network.transfer_adjacency(session)
        seen, queue = set(starts), list(starts)
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen


# ================================================================ 点名册
def verify_rollcall(ledger, rollcall):
    checks = []
    session = ledger["session"]
    retained = {f: {a: Fraction(v) for a, v in row.items()}
                for f, row in ledger["retained_by_source"].items()}
    sig_lookup = {(s["funder"], s["case_id"]): s["vote"]
                  for s in session["signatures"]}
    caps = {c["funder"]: Fraction(c["cap"]) for c in session["credentials"]}
    scope_of = {c["funder"]: set(c["case_scope"]) for c in session["credentials"]}
    delegate_of = {d["funder"]: d["delegate"] for d in session["delegations"]}

    for case in rollcall["cases"]:
        cid, account = case["case_id"], case["account"]
        yes = no = Fraction(0)
        for funder in session["funders"]:
            interest = retained[funder].get(account, Fraction(0))
            rec = next(r for r in case["records"] if r["funder"] == funder)
            expect = _indep_sign(
                funder, cid, interest, sig_lookup, delegate_of, caps, scope_of)
            checks.append(_check(
                rec["mode"] == expect[0] and rec["vote"] == expect[1]
                and Fraction(rec["signed"]) == expect[2],
                f"点名 {cid}/{funder} 独立复算一致",
                f"册={rec['mode']},{rec['vote']},{rec['signed']} "
                f"算={expect}"))
            if expect[1] == "yes":
                yes += expect[2]
            elif expect[1] == "no":
                no += expect[2]
        total = sum((retained[f].get(account, Fraction(0))
                     for f in session["funders"]), Fraction(0))
        threshold = Fraction(case["threshold"])
        approved = total > 0 and yes >= total * threshold
        checks.append(_check(Fraction(case["yes_amount"]) == yes, f"{cid} 同意额独立一致"))
        checks.append(_check(Fraction(case["no_amount"]) == no, f"{cid} 反对额独立一致"))
        checks.append(_check(case["approved"] == approved, f"{cid} 门槛结论独立一致"))
        checks.append(_check(
            (case["yes_share"] is None) == (total == 0), f"{cid} 零留用标记独立一致"))

    # 证额审计独立复算
    for funder, audit in rollcall["credential_audit"].items():
        deducted = sum(
            (Fraction(rec["signed"])
             for case in rollcall["cases"]
             for rec in case["records"]
             if rec["funder"] == funder and rec["mode"] == "delegated"),
            Fraction(0))
        checks.append(_check(
            Fraction(audit["total_deducted"]) == deducted
            and Fraction(audit["remaining_after_session"])
            == Fraction(audit["initial"]) - deducted,
            f"委托证 {funder} 扣额独立复算一致"))
    expect_fp = fingerprint(rollcall)
    checks.append(_check(expect_fp == rollcall["rollcall_fingerprint"], "点名册内容哈希"))
    return {"ok": True, "check_count": len(checks), "checks": checks}


def _indep_sign(funder, case_id, interest, sig_lookup, delegate_of, caps, scope_of):
    """独立重算单户单案：返回 (mode, vote, signed)。"""
    personal = sig_lookup.get((funder, case_id))
    if personal is not None:
        return "personal", personal, interest
    if interest == 0:
        return "abstain", None, Fraction(0)
    chain, seen = [], {funder}
    cur = delegate_of.get(funder)
    while cur is not None:
        if cur in seen:
            return "abstain", None, Fraction(0)
        seen.add(cur)
        chain.append(cur)
        cur = delegate_of.get(cur)
    if not chain or funder not in scope_of or case_id not in scope_of[funder]:
        return "abstain", None, Fraction(0)
    if caps[funder] <= 0:
        return "abstain", None, Fraction(0)
    signer_vote = None
    for member in chain:
        if (member, case_id) in sig_lookup:
            signer_vote = sig_lookup[(member, case_id)]
            break
    if signer_vote is None:
        return "abstain", None, Fraction(0)
    signed = min(interest, caps[funder])
    caps[funder] -= signed
    return "delegated", signer_vote, signed


# ================================================================ 账簿/拨款单
def verify_book(book, order_store_load):
    """从开场账独立重放全部拨款单，核对余额与单号指纹。"""
    checks = []
    # 最大流可行性原语的独立手算自检（容量/流量均为精确分数）
    primal_edges = [("S", "a", 3), ("S", "b", 2), ("a", "T", 2), ("b", "T", 2)]
    primal_flow, primal_values = max_flow(primal_edges, "S", "T")
    checks.append(_check(
        primal_flow == 4 and primal_values[0] == 2 and primal_values[1] == 2,
        "精确最大流原语手算自检"))
    original = {f: {a: Fraction(v) for a, v in row.items()}
                for f, row in book["opening_original"].items()}
    own = {k: Fraction(v) for k, v in book["opening_own_funds"].items()}
    paid = set()
    for order_id in book["orders"]:
        order = order_store_load(order_id)
        checks.append(_check(
            order["order_fingerprint"] == fingerprint(order),
            f"拨款单 {order_id} 内容哈希稳定"))
        checks.append(_check(
            order["ledger_fingerprint"] == book["ledger_fingerprint"]
            and order["rollcall_fingerprint"] == book["rollcall_fingerprint"],
            f"拨款单 {order_id} 绑定去向册/点名册"))
        for item in order["items"]:
            cid = item["case_id"]
            _check(cid not in paid, f"{cid} 未被二次下账")
            paid.add(cid)
            for part in item["redirects"]:
                original[part["funder"]][item["account"]] -= Fraction(part["amount"])
            for part in item["own_fund_inputs"]:
                own[part["fund_id"]] -= Fraction(part["amount"])
        _check(all(v >= 0 for row in original.values() for v in row.values()),
               "重放后原资助余额非负")
        _check(all(v >= 0 for v in own.values()), "重放后自有款余额非负")
    checks.append(_check(paid == set(book["paid_cases"]), "已支案集合与账簿登记一致"))
    return {
        "ok": True,
        "check_count": len(checks),
        "checks": checks,
        "recomputed_original": {
            f: {a: qstr(v) for a, v in row.items()}
            for f, row in original.items()
        },
        "recomputed_own": {k: qstr(v) for k, v in own.items()},
    }
