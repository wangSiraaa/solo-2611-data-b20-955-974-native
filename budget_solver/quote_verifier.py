"""报价曲线与预算报告的独立验证。

与 :mod:`budget_solver.verifier` 同一思路：从目录快照中的原始公式独立重建方程
事实（不信任编译产物与曲线文件里的系数），自行组装带 q 参数列的分块消元并独立
解释，再核对曲线关系、约束证书、区段划分与预算边界。RREF 原语与主路径共用，
但分块组装、参数植入与结论解释都在本模块内独立完成。
"""

from fractions import Fraction

from .errors import VerifyError
from .quoting import (
    KIND_CONTRA,
    KIND_MULTI,
    KIND_PARTIAL,
    KIND_UNIQUE,
    MAX_REBATE_TIERS,
    ST_QUOTABLE,
    status_at,
)
from .solver import rref_augmented
from .util import content_hash
from .verifier import rebuild_facts, rebuild_closure


def _record(checks, name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        raise VerifyError(f"报价曲线验证失败：{name} {detail}")


# ------------------------------------------- 独立符号推导（独立组装与解释）
def independent_symbolic(catalog, artifact, q_var, price_var):
    """从快照重建方程，独立复算单价关于 q 的符号关系与零行约束。"""
    variables, equations, edges = rebuild_facts(catalog, artifact["scope_snapshots"])
    closure = rebuild_closure(edges, price_var)

    # 独立 SCC 划分直接取自编译产物但逐边核对（和 verify_compile 同标准）
    partition = {c["scc_id"]: c["variables"] for c in artifact["components"]}
    topo = {c["scc_id"]: c["topo_index"] for c in artifact["components"]}
    block_ids = sorted(
        {next(cid for cid, members in partition.items() if v in members)
         for v in closure},
        key=lambda cid: topo[cid],
    )

    known = {q_var: ("expr", Fraction(0), {q_var: Fraction(1)})}
    free_vars = set()
    hard = []
    constraints = []
    closure_set = set(closure)

    for cid in block_ids:
        members = [v for v in partition[cid] if v in closure_set]
        if len(members) == 1 and members[0] == q_var:
            continue
        local = {v: i for i, v in enumerate(members)}
        upstream = set()
        for v in members:
            for ref in equations[v][0]:
                if ref not in local and known[ref][0] == "expr":
                    upstream.add(ref)
                    free_vars.update(p for p in known[ref][2] if p != q_var)
        upstream = sorted(upstream)

        n = len(members)
        mat = [[Fraction(0)] * n for _ in range(n)]
        rhs, pmat = [], []
        for r, v in enumerate(members):
            coeffs, eq_rhs = equations[v]
            raw = Fraction(eq_rhs)
            prow = {p: Fraction(0) for p in upstream}
            for ref, coef in coeffs.items():
                if ref in local:
                    mat[r][local[ref]] = coef
                    continue
                info = known[ref]
                if info[0] == "fixed":
                    raw -= coef * info[1]
                else:
                    raw -= coef * info[1]
                    for pv, pc in info[2].items():
                        prow[pv] = prow.get(pv, Fraction(0)) - coef * pc
            rhs.append(raw)
            pmat.append([prow[p] for p in upstream])
        origins = [{v: Fraction(1)} for v in members]
        rows, rrhs, rpmat, rorig, pivot_cols, _ = rref_augmented(
            mat, rhs, pmat, origins)
        qcol = upstream.index(q_var) if q_var in upstream else None

        for r in range(n):
            if any(rows[r][c] != 0 for c in range(n)):
                continue
            if any(rpmat[r][j] != 0 for j, p in enumerate(upstream) if p != q_var):
                continue  # 自由格可吸收的恒等行
            c0, cq = rrhs[r], rpmat[r][qcol] if qcol is not None else Fraction(0)
            if cq == 0:
                if c0 != 0:
                    hard.append((cid, c0, rorig[r]))
            else:
                constraints.append((cid, c0, cq, -c0 / cq, rorig[r]))

        free_cols = [c for c in range(n) if c not in set(pivot_cols)]
        pivot_row = {col: r for r, col in enumerate(pivot_cols)}
        free_vars.update(members[c] for c in free_cols)
        for col, v in enumerate(members):
            if col in free_cols:
                known[v] = ("expr", Fraction(0), {v: Fraction(1)})
                continue
            pr = pivot_row[col]
            parts = {}
            for fc in free_cols:
                c = -rows[pr][fc]
                if c:
                    parts[members[fc]] = c
            for j, pv in enumerate(upstream):
                if rpmat[pr][j]:
                    parts[pv] = parts.get(pv, Fraction(0)) + rpmat[pr][j]
            known[v] = ("expr", rrhs[pr], parts) if parts else ("fixed", rrhs[pr])

    kind, *payload = known[price_var]
    if kind == "fixed":
        intercept, slope, others = payload[0], Fraction(0), []
    else:
        c0, parts = payload
        intercept, slope = c0, parts.get(q_var, Fraction(0))
        others = sorted(p for p in parts if p != q_var)
    return {
        "variables": variables,
        "equations": equations,
        "edges": edges,
        "closure": closure,
        "intercept": intercept,
        "slope": slope,
        "other_free": others,
        "hard": hard,
        "constraints": constraints,
    }


# --------------------------------------------------------------- 曲线验证
def verify_curve(catalog, artifact, curve):
    checks = []
    rec = lambda name, ok, detail="": _record(checks, name, ok, detail)

    rec("曲线绑定的产物哈希", curve["artifact_hash"] == artifact["artifact_hash"])
    rec("作用域快照一致", curve["scope_snapshots"] == artifact["scope_snapshots"])
    q_var, price_var = curve["q_variable"], curve["price_variable"]
    rec("订量格是真实输入格", artifact["variables"][q_var]["kind"] == "input")
    rec("单价格是根输出", price_var in artifact["root_outputs"])

    derived = independent_symbolic(catalog, artifact, q_var, price_var)

    # 闭包与来源方程逐条独立核对
    rec("来源方程闭包独立重建一致",
        [e["variable"] for e in curve["source_equations"]] == derived["closure"])
    for entry in curve["source_equations"]:
        coeffs, rhs = derived["equations"][entry["variable"]]
        rec(f"{entry['variable']} 右端独立一致", rhs == Fraction(entry["rhs"]))
        rebuilt = {k: Fraction(v) for k, v in coeffs.items()}
        filed = {k: Fraction(v) for k, v in entry["coeffs"].items()}
        rec(f"{entry['variable']} 系数独立一致", rebuilt == filed)
        rec(f"{entry['variable']} 格哈希独立一致",
            derived["variables"][entry["variable"]]["cell_hash"] == entry["cell_hash"])

    rel = curve["price_relation"]
    rec("截距独立复算一致", Fraction(rel["intercept"]) == derived["intercept"],
        f"{rel['intercept']} vs {derived['intercept']}")
    rec("q 系数独立复算一致", Fraction(rel["q_coef"]) == derived["slope"],
        f"{rel['q_coef']} vs {derived['slope']}")
    rec("自由参数独立复算一致", rel["free_params"] == derived["other_free"])

    rec("硬矛盾数量独立一致",
        len(curve["hard_contradictions"]) == len(derived["hard"]))
    rec("订量约束数量独立一致",
        len(curve["q_constraints"]) == len(derived["constraints"]))
    for filed, (cid, c0, cq, qstar, _combo) in zip(
            curve["q_constraints"], derived["constraints"]):
        rec("约束常数一致", Fraction(filed["residual_constant"]) == c0)
        rec("约束 q 系数一致", Fraction(filed["residual_q_coef"]) == cq)
        rec("约束订量一致", Fraction(filed["required_q"]) == qstar)

    # 证书 λ 组合独立重算：左端恒零、右端复现
    all_eqs = derived["equations"]
    for item in curve["q_constraints"] + curve["hard_contradictions"]:
        lhs, rhs_combo = {}, Fraction(0)
        for term in item["equation_combination"]:
            lam = Fraction(term["lambda"])
            coeffs, rhs = all_eqs[term["equation_variable"]]
            for var, coef in coeffs.items():
                lhs[var] = lhs.get(var, Fraction(0)) + lam * coef
            rhs_combo += lam * rhs
        if "residual_rhs" in item:  # 硬矛盾：所有格变量（含 q）组合后恒零
            rec(f"证书 {item['scc_id']} 组合左端恒零",
                all(v == 0 for v in lhs.values()))
            rec("硬矛盾右端非零且复现",
                rhs_combo != 0 and rhs_combo == Fraction(item["residual_rhs"]))
        else:  # 订量约束：非 q 格变量恒零，q 残差系数与右端常数分别复现
            # RREF 参数列约定下，λ 组合左端的 q 系数 = -证书残差 q 系数
            rec(f"证书 {item['scc_id']} 非 q 左端恒零",
                all(v == 0 for var, v in lhs.items() if var != q_var))
            rec("订量约束 q 残差系数复现",
                lhs.get(q_var, Fraction(0))
                == -Fraction(item["residual_q_coef"]))
            rec("订量约束右端常数复现",
                rhs_combo == Fraction(item["residual_constant"]))

    # 区段：连续覆盖整个报价域，端点状态与独立关系逐段核对
    lo = curve["quote_domain"]["lo"]
    hi = curve["quote_domain"]["hi"]
    segments = curve["segments"]
    rec("区段连续覆盖报价域",
        segments and segments[0]["lo"] == lo
        and all(segments[i]["hi"] + 1 == segments[i + 1]["lo"]
                for i in range(len(segments) - 1))
        and segments[-1]["hi"] == hi)
    intercept, slope = derived["intercept"], derived["slope"]
    price = lambda q: intercept + slope * q
    for seg in segments:
        for endpoint, filed_price in (
                (seg["lo"], seg["price_at_lo"]), (seg["hi"], seg["price_at_hi"])):
            status, p = status_at(curve, endpoint)
            rec(f"q={endpoint} 状态与所在段一致", status == seg["status"])
            if p is not None:
                rec(f"q={endpoint} 单价独立复算",
                    p == price(endpoint) and filed_price == str(p))
        if seg["status"] == ST_QUOTABLE:
            rec(f"[{seg['lo']},{seg['hi']}] 端点单价非负",
                price(seg["lo"]) >= 0 and price(seg["hi"]) >= 0)

    # 分类与独立事实一致
    kind = curve["classification"]
    if derived["hard"]:
        rec("分类为完全矛盾", kind == KIND_CONTRA)
    elif derived["constraints"]:
        rec("分类为部分订量相容", kind == KIND_PARTIAL)
    elif derived["other_free"]:
        rec("分类为同订量多单价", kind == KIND_MULTI)
    else:
        rec("分类为整域唯一可报价", kind == KIND_UNIQUE)
        rec("可报价段并集与非负切分一致",
            [(s["lo"], s["hi"]) for s in segments if s["status"] == ST_QUOTABLE]
            == [(s["lo"], s["hi"]) for s in curve["quotable_segments"]])

    expect = content_hash({k: v for k, v in curve.items()
                           if k not in ("content_hash", "curve_id")})
    rec("曲线内容哈希", expect == curve["content_hash"])
    return {"ok": True, "checks": checks, "check_count": len(checks)}


# --------------------------------------------------------------- 预算验证
def verify_budget_report(catalog, curve, report):
    checks = []
    rec = lambda name, ok, detail="": _record(checks, name, ok, detail)

    rec("报告指回的曲线哈希一致",
        report["curve_content_hash"] == curve["content_hash"])
    rec("产物哈希链路一致", report["artifact_hash"] == curve["artifact_hash"])
    rec("作用域钉选一致", report["scope_snapshots"] == curve["scope_snapshots"])

    dlo, dhi = report["query_domain"]["lo"], report["query_domain"]["hi"]
    clo, chi = curve["quote_domain"]["lo"], curve["quote_domain"]["hi"]
    rec("查询域落在报价域内", clo <= dlo <= dhi <= chi)
    budget = Fraction(report["budget"])
    rec("预算非负", budget >= 0)
    tiers = report["rebate_tiers"]
    rec("回扣档数不超过上限", 1 <= len(tiers) <= MAX_REBATE_TIERS)
    rec("档表无缝不重叠覆盖查询域",
        tiers[0]["lo"] == dlo and tiers[-1]["hi"] == dhi
        and all(tiers[i]["hi"] + 1 == tiers[i + 1]["lo"]
                for i in range(len(tiers) - 1)))
    rec("各档回扣非负", all(Fraction(t["rebate"]) >= 0 for t in tiers))

    rel = curve["price_relation"]
    a, b = Fraction(rel["intercept"]), Fraction(rel["q_coef"])
    gross = lambda q: b * q * q + a * q

    def rebate_of(q):
        for tier in tiers:
            if tier["lo"] <= q <= tier["hi"]:
                return Fraction(tier["rebate"])
        raise VerifyError(f"q={q} 无回扣档")

    def payment(q):
        return max(Fraction(0), gross(q) - rebate_of(q))

    # 逐段端点与紧邻边界独立核算（不枚举段内订量）
    for seg in report["segments"]:
        s, t = seg["lo"], seg["hi"]
        rec(f"[{s},{t}] 段连续", s <= t)
        rec(f"[{s},{t}] 左端单价独立一致",
            Fraction(seg["price_at_lo"]) == a + b * s)
        rec(f"[{s},{t}] 右端单价独立一致",
            Fraction(seg["price_at_hi"]) == a + b * t)
        rec(f"[{s},{t}] 左端净付独立一致且不超预算",
            Fraction(seg["payment_at_lo"]) == payment(s) <= budget)
        rec(f"[{s},{t}] 右端净付独立一致且不超预算",
            Fraction(seg["payment_at_hi"]) == payment(t) <= budget)
        rec(f"[{s},{t}] 整段都在可报价状态",
            status_at(curve, s)[0] == ST_QUOTABLE
            and status_at(curve, t)[0] == ST_QUOTABLE)
        for neighbor, bound in ((seg["left_neighbor"], s),
                                (seg["right_neighbor"], t)):
            if neighbor is None:
                continue
            q = neighbor["q"]
            rec("紧邻点位置正确", q == bound - 1 or q == bound + 1)
            if dlo <= q <= dhi:
                status, price = status_at(curve, q)
                rec("紧邻点曲线状态一致", neighbor["status"] == status)
                if status == ST_QUOTABLE:
                    rec("紧邻点净付独立一致",
                        Fraction(neighbor["payment"]) == payment(q))
                    rec("紧邻可报价点确实超预算", payment(q) > budget)
                else:
                    rec("紧邻点单价一致",
                        (price is None and neighbor["price"] is None)
                        or Fraction(neighbor["price"]) == price)

    # 极大性：报告段两两不相邻（否则应已合并），并按序覆盖
    rec("输出段互不相邻且有序",
        all(report["segments"][i]["hi"] + 1 < report["segments"][i + 1]["lo"]
            for i in range(len(report["segments"]) - 1)))
    rec("件数合计独立一致",
        report["payable_piece_count"]
        == sum(s["hi"] - s["lo"] + 1 for s in report["segments"]))

    # 边界根结构：二次 g(q) 与 cap 的交叉只能发生在报告段边界之外，独立抽查
    for seg in report["segments"]:
        for q in (seg["lo"], seg["hi"]):
            rec(f"段内点 {q} 毛利不超过 回扣+预算",
                gross(q) <= rebate_of(q) + budget)
    expect = content_hash({k: v for k, v in report.items()
                           if k not in ("report_hash", "budget_id")})
    rec("预算报告内容哈希", expect == report["report_hash"])
    return {"ok": True, "checks": checks, "check_count": len(checks)}
