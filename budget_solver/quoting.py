"""数量报价曲线与采购额度核对（纯标准库、全程 :class:`fractions.Fraction`）。

两个可交付物，都建立在原 put-model / snapshot / compile 的冻结作用域之上，
原 solve / query 的固定订量路径完全不受影响：

1. **报价曲线** :func:`build_quote_curve`
   报价员指定一个真实输入格 ``q`` 与一个根输出格作单价。订量输入方程
   ``q = 点值`` 被符号参数化——系数矩阵原样保留，变化只进入方程右端——然后按
   SCC 拓扑分块做带 ``q`` 参数列的精确 RREF，由实际方程推出单价关于 q 的精确
   仿射关系 ``p(q) = a + b·q``，并把闭包内格分作四类目录：

   * ``unique_whole_domain`` 整个域都有唯一单价（负价子段另列为不可报价）；
   * ``partial_consistency``  循环块留下 ``0 = c0 + c1·q`` 约束，只有个别订量相容；
   * ``multi_price``          闭包有自由格且单价表达式含 q 以外的自由参数；
   * ``fully_contradictory``  约束与 q 无关且右端非零，任何订量都矛盾。

   选中的闭包允许存在自由格，只要单价对同一 q 仍唯一即可报价；可报价还要求
   单价非负。所有结论附来源方程（含格哈希）与 λ 组合证书。

2. **采购额度核对** :func:`evaluate_budget`
   价格表是互不重叠、覆盖所查数量域的整数闭区间，每档一个固定整单回扣 R，
   实付 ``max(0, q·p(q) − R)``。档内毛利 ``g(q)=b·q²+a·q`` 是二次式，可付集合
   用精确分数二分在单调半段上求整数边界，跨档合并为极大连续整数段；报价域上
   限十亿件、回扣档至多 30 个，全程不枚举订量、不引入浮点。

曲线与预算报告均不可变、带内容哈希、随数据目录落盘，可重开复读；新预算重算
只产生新预算报告并指回同一条曲线（原合同），旧报告原样保留。
"""

import os
from fractions import Fraction

from .errors import NotFoundError, ValidationError
from .rationals import parse_rational, qstr
from .solver import dependency_closure, rref_augmented
from .util import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now_iso,
    write_json_atomic,
)

CURVE_SCHEMA_VERSION = 1
BUDGET_SCHEMA_VERSION = 1
MAX_DOMAIN_SIZE = 10**9
MAX_REBATE_TIERS = 30

# 每个订量的状态
ST_QUOTABLE = "quotable"            # 唯一单价且非负，可报价
ST_NEGATIVE = "negative_price"      # 唯一单价但为负，不可报价区段
ST_NON_UNIQUE = "non_unique"        # 同一订量多个单价
ST_INCONSISTENT = "inconsistent"    # 该订量方程不相容

# 曲线目录四分类
KIND_UNIQUE = "unique_whole_domain"
KIND_PARTIAL = "partial_consistency"
KIND_MULTI = "multi_price"
KIND_CONTRA = "fully_contradictory"


# ============================================================ 精确整数小工具
def floor_frac(frac):
    """有理数向下取整（Python // 对负数也是地板除）。"""
    return frac.numerator // frac.denominator


def ceil_frac(frac):
    return -((-frac.numerator) // frac.denominator)


# ============================================================ 符号报价关系
def derive_symbolic_relation(artifact, q_var, price_var):
    """把 q 符号化后对单价闭包分块消元，返回精确符号关系与约束证书。

    与 ``solver.solve_closure`` 的差别只有两处：

    * q 自身的输入方程不参与消元，q 从一开始就是符号参数（右端可变、系数固定）；
    * 零行 ``0 = c0 + cq·q`` 不立即判死：cq≠0 时是“仅 q=-c0/cq 相容”的订量约束，
      cq=0 且 c0≠0 才是与订量无关的硬矛盾；残差落在真正自由格上的零行是恒等行。
    """
    variables = artifact["variables"]
    if q_var not in variables:
        raise ValidationError(f"订量格 {q_var} 不在编译产物作用域内", [q_var])
    if variables[q_var]["kind"] != "input":
        raise ValidationError(
            f"订量格 {q_var} 必须是真实输入格（kind=input），不能是公式格", [q_var]
        )
    if price_var not in artifact["root_outputs"]:
        raise ValidationError(
            f"单价格 {price_var} 必须是编译产物的根输出格", [price_var]
        )
    closure = dependency_closure(artifact, price_var)
    closure_set = set(closure)
    comp_of = artifact["var_component"]
    components = {c["scc_id"]: c for c in artifact["components"]}
    block_ids = sorted(
        {comp_of[v] for v in closure},
        key=lambda cid: components[cid]["topo_index"],
    )

    # known[v] = ("fixed", F) | ("expr", const, {param_var: coef})
    # q 先种成恒等参数表达式；它的单点输入方程随后整块跳过。
    known = {q_var: ("expr", Fraction(0), {q_var: Fraction(1)})}
    free_params = set()
    hard_contradictions = []
    q_constraints = []

    for cid in block_ids:
        members = [v for v in components[cid]["variables"] if v in closure_set]
        if len(members) == 1 and members[0] == q_var:
            continue  # 订量输入方程 q=点值 被符号参数取代（只改右端，不改系数）
        local_index = {v: i for i, v in enumerate(members)}

        upstream_params = set()
        for v in members:
            for ref_var in artifact["equations"][v]["coeffs"]:
                if ref_var in local_index:
                    continue
                info = known[ref_var]
                if info[0] == "expr":
                    upstream_params.add(ref_var)
                    free_params.update(p for p in info[2] if p != q_var)
        upstream_params = sorted(upstream_params)

        n = len(members)
        matrix = [[Fraction(0)] * n for _ in range(n)]
        const_rhs = []
        param_rows = []
        for r, v in enumerate(members):
            eq = artifact["equations"][v]
            rhs_const = Fraction(eq["rhs"])
            prow = {p: Fraction(0) for p in upstream_params}
            for ref_var, coef_str in eq["coeffs"].items():
                coef = Fraction(coef_str)
                if ref_var in local_index:
                    matrix[r][local_index[ref_var]] = coef
                    continue
                kind, *payload = known[ref_var]
                if kind == "fixed":
                    rhs_const -= coef * payload[0]
                else:
                    _, c0, parts = known[ref_var]
                    rhs_const -= coef * c0
                    for pv, pc in parts.items():
                        prow[pv] = prow.get(pv, Fraction(0)) - coef * pc
            const_rhs.append(rhs_const)
            param_rows.append([prow[p] for p in upstream_params])

        origins = [{v: Fraction(1)} for v in members]
        rows, rrhs, pmat, rorig, pivot_cols, _bad = rref_augmented(
            matrix, const_rhs, param_rows, origins
        )
        qcol = upstream_params.index(q_var) if q_var in upstream_params else None

        for r in range(n):
            if any(rows[r][c] != 0 for c in range(n)):
                continue
            # 残差落在真正自由格（非 q）上的零行可被自由格吸收，是恒等行
            free_residual = any(
                pmat[r][j] != 0
                for j, p in enumerate(upstream_params)
                if p != q_var
            )
            if free_residual:
                continue
            c0 = rrhs[r]
            cq = pmat[r][qcol] if qcol is not None else Fraction(0)
            combo = [
                {"equation_variable": ev, "lambda": qstr(lam)}
                for ev, lam in sorted(rorig[r].items())
                if lam != 0
            ]
            if cq == 0:
                if c0 != 0:  # 0 = 非零常数：与订量无关，任何 q 都矛盾
                    hard_contradictions.append(
                        {
                            "scc_id": cid,
                            "residual_lhs": "0",
                            "residual_rhs": qstr(c0),
                            "equation_combination": combo,
                        }
                    )
            else:  # 0 = c0 + cq·q：只有 q = -c0/cq 相容
                qstar = -c0 / cq
                q_constraints.append(
                    {
                        "scc_id": cid,
                        "residual_lhs": "0",
                        "residual_constant": qstr(c0),
                        "residual_q_coef": qstr(cq),
                        "required_q": qstr(qstar),
                        "required_q_is_integer": qstar.denominator == 1,
                        "equation_combination": combo,
                    }
                )

        pivot_set = set(pivot_cols)
        free_cols = [c for c in range(n) if c not in pivot_set]
        pivot_row_of = {col: r for r, col in enumerate(pivot_cols)}
        free_params.update(members[c] for c in free_cols)
        for col, v in enumerate(members):
            if col in free_cols:
                known[v] = ("expr", Fraction(0), {v: Fraction(1)})
                continue
            pr = pivot_row_of[col]
            parts = {}
            for fc in free_cols:
                c = -rows[pr][fc]
                if c != 0:
                    parts[members[fc]] = c
            for j, pv in enumerate(upstream_params):
                c = pmat[pr][j]
                if c != 0:
                    parts[pv] = parts.get(pv, Fraction(0)) + c
            if not parts:
                known[v] = ("fixed", rrhs[pr])
            else:
                known[v] = ("expr", rrhs[pr], parts)

    kind, *payload = known[price_var]
    if kind == "fixed":
        intercept, slope, other_free = payload[0], Fraction(0), []
    else:
        c0, parts = payload
        intercept = c0
        slope = parts.get(q_var, Fraction(0))
        other_free = sorted(p for p in parts if p != q_var)

    required_values = sorted(
        {Fraction(item["required_q"]) for item in q_constraints}
    )
    return {
        "closure": closure,
        "q_in_closure": q_var in closure_set,
        "frozen_q_value": artifact["equations"][q_var]["rhs"],
        "free_variables": sorted(free_params),
        "hard_contradictions": hard_contradictions,
        "q_constraints": q_constraints,
        "required_q_values": [qstr(v) for v in required_values],
        "price_intercept": intercept,
        "price_slope": slope,
        "price_other_free_params": other_free,
        "source_equations": [
            {
                "variable": v,
                "coeffs": artifact["equations"][v]["coeffs"],
                "rhs": artifact["equations"][v]["rhs"],
                "cell_hash": artifact["equations"][v]["cell_hash"],
            }
            for v in closure
        ],
    }


# ============================================================ 区段构建（不枚举）
def _segment(lo, hi, status, price_at, reason, extra=None):
    record = {
        "lo": lo,
        "hi": hi,
        "status": status,
        "price_at_lo": qstr(price_at(lo)) if price_at else None,
        "price_at_hi": qstr(price_at(hi)) if price_at else None,
        "reason": reason,
    }
    if extra:
        record.update(extra)
    return record


def _affine_segments(lo, hi, intercept, slope):
    """唯一仿射单价 p=a+bq 按非负性切极大整数段；线性不等式至多一个分界。"""
    price = lambda q: intercept + slope * q
    if slope == 0:
        status = ST_QUOTABLE if intercept >= 0 else ST_NEGATIVE
        reason = None if intercept >= 0 else "单价为负，不可报价"
        return [_segment(lo, hi, status, price, reason)]
    threshold = -intercept / slope
    if slope > 0:  # p≥0 ⟺ q ≥ 阈值
        boundary = ceil_frac(threshold)
        neg_hi = min(hi, boundary - 1)
        quote_lo = max(lo, boundary)
        segments = []
        if lo <= neg_hi:
            segments.append(_segment(lo, neg_hi, ST_NEGATIVE, price, "单价为负，不可报价"))
        if quote_lo <= hi:
            segments.append(_segment(quote_lo, hi, ST_QUOTABLE, price, None))
        return segments
    # slope < 0：p≥0 ⟺ q ≤ 阈值
    boundary = floor_frac(threshold)
    quote_hi = min(hi, boundary)
    neg_lo = max(lo, boundary + 1)
    segments = []
    if lo <= quote_hi:
        segments.append(_segment(lo, quote_hi, ST_QUOTABLE, price, None))
    if neg_lo <= hi:
        segments.append(_segment(neg_lo, hi, ST_NEGATIVE, price, "单价为负，不可报价"))
    return segments


def _classify_curve(symbolic, lo, hi):
    """返回 (curve_kind, segments, quotable_segments, 单点相容信息)。"""
    intercept = symbolic["price_intercept"]
    slope = symbolic["price_slope"]
    other_free = symbolic["price_other_free_params"]
    price = lambda q: intercept + slope * q

    # 1. 硬矛盾：整个域都不相容
    if symbolic["hard_contradictions"]:
        return (
            KIND_CONTRA,
            [_segment(lo, hi, ST_INCONSISTENT, None, "存在与订量无关的矛盾方程 0=非零")],
            [],
            None,
        )

    # 2. 订量约束：相容集合至多一个有理数点
    if symbolic["q_constraints"]:
        required = [Fraction(v) for v in symbolic["required_q_values"]]
        unique_star = required[0] if len(set(required)) == 1 else None
        star_int = (
            unique_star
            if unique_star is not None and unique_star.denominator == 1 else None
        )
        star_q = int(star_int) if star_int is not None and lo <= star_int <= hi else None
        segments = []
        if star_q is None:
            segments.append(
                _segment(
                    lo, hi, ST_INCONSISTENT, None,
                    "循环块要求 q 取域内不存在的整数（或要求互相冲突的订量）",
                    extra={"required_q": symbolic["required_q_values"]},
                )
            )
            return KIND_PARTIAL, segments, [], None
        if lo <= star_q - 1:
            segments.append(
                _segment(lo, star_q - 1, ST_INCONSISTENT, None,
                         f"仅 q={star_q} 满足循环块相容约束",
                         extra={"required_q": qstr(star_int)}))
        if other_free:
            segments.append(
                _segment(star_q, star_q, ST_NON_UNIQUE, None,
                         "方程相容但单价含自由格，同一订量有多个单价",
                         extra={"required_q": qstr(star_int)}))
        else:
            pstar = price(star_q)
            status = ST_QUOTABLE if pstar >= 0 else ST_NEGATIVE
            reason = None if pstar >= 0 else "单价为负，不可报价"
            segments.append(
                _segment(star_q, star_q, status, price, reason,
                         extra={"required_q": qstr(star_int)}))
        if star_q + 1 <= hi:
            segments.append(
                _segment(star_q + 1, hi, ST_INCONSISTENT, None,
                         f"仅 q={star_q} 满足循环块相容约束",
                         extra={"required_q": qstr(star_int)}))
        quotable = [s for s in segments if s["status"] == ST_QUOTABLE]
        return KIND_PARTIAL, segments, quotable, star_q

    # 3. 无约束但单价不唯一
    if other_free:
        return (
            KIND_MULTI,
            [_segment(lo, hi, ST_NON_UNIQUE, None,
                      "单价表达式含自由格：" + ",".join(other_free))],
            [],
            None,
        )

    # 4. 整个域唯一仿射可报价（负价子段内部再切）
    segments = _affine_segments(lo, hi, intercept, slope)
    quotable = [s for s in segments if s["status"] == ST_QUOTABLE]
    return KIND_UNIQUE, segments, quotable, None


def validate_domain(lo, hi):
    if not isinstance(lo, int) or not isinstance(hi, int) or isinstance(lo, bool):
        raise ValidationError("报价域端点必须是整数", [f"[{lo}, {hi}]"])
    if lo < 1:
        raise ValidationError("整件订量必须从 1 开始", [f"lo={lo}"])
    if hi < lo:
        raise ValidationError("报价域 hi 不能小于 lo", [f"[{lo}, {hi}]"])
    if hi - lo + 1 > MAX_DOMAIN_SIZE:
        raise ValidationError(
            f"报价域至多 {MAX_DOMAIN_SIZE} 个整件数", [f"size={hi - lo + 1}"]
        )


def build_quote_curve(artifact, q_var, price_var, lo, hi, label=None):
    """纯函数：由编译产物构造一条可持久化的数量报价曲线（不落盘）。"""
    validate_domain(lo, hi)
    symbolic = derive_symbolic_relation(artifact, q_var, price_var)
    kind, segments, quotable, star_q = _classify_curve(symbolic, lo, hi)
    curve = {
        "curve_kind": "quote_curve",
        "schema_version": CURVE_SCHEMA_VERSION,
        "label": label,
        "artifact_hash": artifact["artifact_hash"],
        "root_model_id": artifact["root_model_id"],
        "root_snap_id": artifact["root_snap_id"],
        "scope_snapshots": artifact["scope_snapshots"],
        "q_variable": q_var,
        "price_variable": price_var,
        "frozen_q_value": symbolic["frozen_q_value"],
        "quote_domain": {"lo": lo, "hi": hi},
        "q_in_price_closure": symbolic["q_in_closure"],
        "classification": kind,
        "price_relation": {
            # 单价 = intercept + slope * q；仅在唯一分类下对每个 q 成立
            "intercept": qstr(symbolic["price_intercept"]),
            "q_coef": qstr(symbolic["price_slope"]),
            "unique": kind in (KIND_UNIQUE, KIND_PARTIAL)
                      and not symbolic["price_other_free_params"],
            "free_params": symbolic["price_other_free_params"],
        },
        "free_variables": symbolic["free_variables"],
        "hard_contradictions": symbolic["hard_contradictions"],
        "q_constraints": symbolic["q_constraints"],
        "required_q_values": symbolic["required_q_values"],
        "consistent_single_q": star_q,
        "segments": segments,
        "quotable_segments": [
            {"lo": s["lo"], "hi": s["hi"],
             "price_at_lo": s["price_at_lo"], "price_at_hi": s["price_at_hi"]}
            for s in quotable
        ],
        "source_equations": symbolic["source_equations"],
        "created_at": utc_now_iso(),
    }
    curve["content_hash"] = content_hash(
        {k: v for k, v in curve.items() if k != "content_hash"}
    )
    return curve


def status_at(curve, q):
    """O(1) 判定某整数订量在曲线上的状态与单价（供预算核对紧邻原因使用）。"""
    dlo = curve["quote_domain"]["lo"]
    dhi = curve["quote_domain"]["hi"]
    if not (dlo <= q <= dhi):
        return ST_INCONSISTENT, None
    kind = curve["classification"]
    if kind == KIND_CONTRA:
        return ST_INCONSISTENT, None
    if kind == KIND_MULTI:
        return ST_NON_UNIQUE, None
    if kind == KIND_PARTIAL:
        if curve["consistent_single_q"] != q:
            return ST_INCONSISTENT, None
        if curve["price_relation"]["free_params"]:
            return ST_NON_UNIQUE, None
    intercept = Fraction(curve["price_relation"]["intercept"])
    slope = Fraction(curve["price_relation"]["q_coef"])
    price = intercept + slope * q
    if price < 0:
        return ST_NEGATIVE, price
    return ST_QUOTABLE, price


# ============================================================ 预算核对：二次整数边界
def _last_increasing(l, u, gross, cap):
    """g 在 [l,u] 单调递增：返回满足 g(q)≤cap 的最大整数 q，没有则 None。"""
    if gross(l) > cap:
        return None
    lo, hi = l, u
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if gross(mid) <= cap:
            lo = mid
        else:
            hi = mid - 1
    return lo


def _first_decreasing(l, u, gross, cap):
    """g 在 [l,u] 单调递减：返回满足 g(q)≤cap 的最小整数 q，没有则 None。"""
    if gross(u) > cap:
        return None
    lo, hi = l, u
    while lo < hi:
        mid = (lo + hi) // 2
        if gross(mid) <= cap:
            hi = mid
        else:
            lo = mid + 1
    return lo


def quadratic_payable_runs(l, u, intercept, slope, cap):
    """求整数区间内 ``g(q)=b·q²+a·q ≤ cap`` 的全部极大连续段。

    二次函数只有一个顶点，按顶点切成两个单调半段后用精确分数二分找边界，
    不做逐点枚举，也不使用平方根或浮点。
    """
    def gross(q):
        return slope * q * q + intercept * q

    if slope == 0:
        if intercept >= 0:  # 可报价段上 p=a≥0，g 随 q 单调不降
            last = _last_increasing(l, u, gross, cap)
            return [(l, last)] if last is not None else []
        first = _first_decreasing(l, u, gross, cap)
        return [(first, u)] if first is not None else []

    vertex_split = floor_frac(-intercept / (2 * slope))
    runs = []
    if slope > 0:  # 凸：顶点左侧递减、右侧递增，可付集是中间连续区间
        left_l, left_u = l, min(u, vertex_split)
        if left_l <= left_u:
            first = _first_decreasing(left_l, left_u, gross, cap)
            if first is not None:
                runs.append((first, left_u))
        right_l, right_u = max(l, vertex_split + 1), u
        if right_l <= right_u:
            last = _last_increasing(right_l, right_u, gross, cap)
            if last is not None:
                runs.append((right_l, last))
    else:  # 凹：顶点左侧递增、右侧递减，可付集是左右两个尾巴
        left_l, left_u = l, min(u, vertex_split)
        if left_l <= left_u:
            last = _last_increasing(left_l, left_u, gross, cap)
            if last is not None:
                runs.append((left_l, last))
        right_l, right_u = max(l, vertex_split + 1), u
        if right_l <= right_u:
            first = _first_decreasing(right_l, right_u, gross, cap)
            if first is not None:
                runs.append((first, right_u))
    return runs


def _merge_runs(runs):
    """把可能相邻的整数段合并为极大连续段。"""
    merged = []
    for s, t in sorted(runs):
        if merged and s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], t))
        else:
            merged.append((s, t))
    return merged


def _validate_rebate_spec(curve, spec):
    if not isinstance(spec, dict):
        raise ValidationError("回扣价格表必须是 JSON 对象", ["spec"])
    domain = spec.get("domain")
    if not (isinstance(domain, list) and len(domain) == 2
            and all(isinstance(v, int) and not isinstance(v, bool) for v in domain)):
        raise ValidationError("domain 必须是两个整数构成的数组 [lo, hi]", [str(domain)])
    dlo, dhi = domain
    clo = curve["quote_domain"]["lo"]
    chi = curve["quote_domain"]["hi"]
    if not (clo <= dlo <= dhi <= chi):
        raise ValidationError(
            f"所查数量域 [{dlo},{dhi}] 必须落在报价域 [{clo},{chi}] 内", [str(domain)])
    budget = parse_rational(spec.get("budget"), field="budget")
    if budget < 0:
        raise ValidationError("预算不能为负", [qstr(budget)])
    raw_tiers = spec.get("tiers")
    if not isinstance(raw_tiers, list) or not raw_tiers:
        raise ValidationError("tiers 必须是非空数组")
    if len(raw_tiers) > MAX_REBATE_TIERS:
        raise ValidationError(f"回扣档至多 {MAX_REBATE_TIERS} 个", [str(len(raw_tiers))])
    tiers = []
    for index, raw in enumerate(raw_tiers):
        if not isinstance(raw, dict):
            raise ValidationError(f"第 {index} 档必须是对象", [])
        lo = raw.get("lo")
        hi = raw.get("hi")
        if not (isinstance(lo, int) and isinstance(hi, int)
                and not isinstance(lo, bool) and not isinstance(hi, bool)):
            raise ValidationError(f"第 {index} 档端点必须是整数", [str(raw)])
        rebate = parse_rational(raw.get("rebate", 0), field=f"tiers[{index}].rebate")
        if rebate < 0:
            raise ValidationError(f"第 {index} 档回扣不能为负", [qstr(rebate)])
        tiers.append({"lo": lo, "hi": hi, "rebate": rebate})
    prev_hi = None
    for index, tier in enumerate(tiers):
        if tier["lo"] > tier["hi"]:
            raise ValidationError(f"第 {index} 档 lo>hi", [str(tier)])
        if prev_hi is None:
            if tier["lo"] != dlo:
                raise ValidationError(f"首档必须从查询域起点 {dlo} 开始", [str(tier)])
        elif tier["lo"] != prev_hi + 1:
            raise ValidationError(
                f"第 {index} 档与上一档不连续（必须互不重叠且无缝覆盖）",
                [f"prev_hi={prev_hi}", str(tier)])
        prev_hi = tier["hi"]
    if tiers[-1]["hi"] != dhi:
        raise ValidationError(f"末档必须结束于查询域终点 {dhi}", [str(tiers[-1])])
    return dlo, dhi, budget, tiers


def _rebate_at(tiers, q):
    for tier in tiers:
        if tier["lo"] <= q <= tier["hi"]:
            return tier["rebate"]
    raise NotFoundError(f"订量 {q} 不在任何回扣档内")


def evaluate_budget(curve, spec, label=None):
    """纯函数：在报价曲线上按回扣价格表与预算求可买极大连续整数段。"""
    dlo, dhi, budget, tiers = _validate_rebate_spec(curve, spec)
    intercept = Fraction(curve["price_relation"]["intercept"])
    slope = Fraction(curve["price_relation"]["q_coef"])
    relation_unique = curve["price_relation"]["unique"]

    raw_runs = []
    if relation_unique:
        for qseg in curve["quotable_segments"]:
            for tier in tiers:
                l = max(tier["lo"], qseg["lo"])
                u = min(tier["hi"], qseg["hi"])
                if l > u:
                    continue
                cap = tier["rebate"] + budget  # 净付≤预算 ⟺ 毛利≤回扣+预算
                raw_runs.extend(
                    quadratic_payable_runs(l, u, intercept, slope, cap))
    payable_runs = _merge_runs(raw_runs)

    def gross(q):
        return slope * q * q + intercept * q

    def net_payment(q):
        return max(Fraction(0), gross(q) - _rebate_at(tiers, q))

    def neighbor_reason(q):
        if q < dlo or q > dhi:
            return {"q": q, "status": "outside_query_domain",
                    "reason": "超出采购查询域", "price": None, "payment": None}
        status, price = status_at(curve, q)
        if status != ST_QUOTABLE:
            reason_map = {
                ST_NEGATIVE: "curve_negative_price",
                ST_NON_UNIQUE: "curve_non_unique",
                ST_INCONSISTENT: "curve_inconsistent",
            }
            return {"q": q, "status": status, "reason": reason_map[status],
                    "price": qstr(price) if price is not None else None,
                    "payment": None}
        payment = net_payment(q)
        return {
            "q": q,
            "status": ST_QUOTABLE,
            "reason": "over_budget" if payment > budget else "unexpected_payable",
            "price": qstr(price),
            "payment": qstr(payment),
            "rebate": qstr(_rebate_at(tiers, q)),
        }

    segments = []
    for s, t in payable_runs:
        segments.append({
            "lo": s,
            "hi": t,
            "piece_count": t - s + 1,
            "price_at_lo": qstr(intercept + slope * s),
            "price_at_hi": qstr(intercept + slope * t),
            "payment_at_lo": qstr(net_payment(s)),
            "payment_at_hi": qstr(net_payment(t)),
            "left_neighbor": neighbor_reason(s - 1) if s > dlo else None,
            "right_neighbor": neighbor_reason(t + 1) if t < dhi else None,
        })

    report = {
        "report_kind": "budget_check_report",
        "schema_version": BUDGET_SCHEMA_VERSION,
        "label": label,
        "curve_id": None,  # 由持久化服务补
        "curve_content_hash": curve["content_hash"],
        "artifact_hash": curve["artifact_hash"],
        "root_snap_id": curve["root_snap_id"],
        "scope_snapshots": curve["scope_snapshots"],
        "q_variable": curve["q_variable"],
        "price_variable": curve["price_variable"],
        "query_domain": {"lo": dlo, "hi": dhi},
        "budget": qstr(budget),
        "rebate_tiers": [
            {"lo": t["lo"], "hi": t["hi"], "rebate": qstr(t["rebate"])} for t in tiers
        ],
        "price_relation": curve["price_relation"],
        "curve_classification": curve["classification"],
        "segments": segments,
        "payable_piece_count": sum(item["piece_count"] for item in segments),
        "empty_reason": None if relation_unique and curve["quotable_segments"]
        else "报价曲线上没有唯一且非负的可报价订量",
        "created_at": utc_now_iso(),
    }
    return report


# ============================================================ 可重开持久化服务
class QuoteService:
    """报价曲线与预算报告的落盘服务；构造即重开。"""

    def __init__(self, catalog, artifact_dir, curve_dir, budget_dir):
        self.catalog = catalog
        self.artifact_dir = os.path.abspath(artifact_dir)
        self.curve_dir = os.path.abspath(curve_dir)
        self.budget_dir = os.path.abspath(budget_dir)
        ensure_dir(self.curve_dir)
        ensure_dir(self.budget_dir)

    def _curve_path(self, curve_id):
        return os.path.join(self.curve_dir, f"{curve_id}.json")

    def _budget_path(self, budget_id):
        return os.path.join(self.budget_dir, f"{budget_id}.json")

    @staticmethod
    def _next_seq(directory, prefix):
        seq_path = os.path.join(directory, "seq.json")
        seq = read_json(seq_path) if os.path.exists(seq_path) else {"next": 1}
        item_id = f"{prefix}{seq['next']}"
        seq["next"] += 1
        write_json_atomic(seq_path, seq)
        return item_id

    # ------------------------------------------------------------- 报价曲线
    def create_curve(self, artifact, q_var, price_var, lo, hi, label=None):
        curve = build_quote_curve(artifact, q_var, price_var, lo, hi, label=label)
        curve_id = self._next_seq(self.curve_dir, "curve")
        curve["curve_id"] = curve_id
        # 哈希在放入 id 前计算；id 不参与内容寻址，重开后按 id 稳定复读
        write_json_atomic(self._curve_path(curve_id), curve)
        return curve

    def get_curve(self, curve_id):
        path = self._curve_path(curve_id)
        if not os.path.exists(path):
            raise NotFoundError(f"报价曲线 {curve_id} 不存在")
        return read_json(path)

    def list_curves(self):
        if not os.path.isdir(self.curve_dir):
            return []
        ids = sorted(
            (f[:-5] for f in os.listdir(self.curve_dir)
             if f.startswith("curve") and f.endswith(".json")),
            key=lambda cid: int(cid[5:]),
        )
        return [self.get_curve(cid) for cid in ids]

    def quote_at(self, curve_id, q):
        """单独求某个数量的单价；回传外部模型实际钉选版本。"""
        if not isinstance(q, int) or isinstance(q, bool):
            raise ValidationError("订量必须是整数", [str(q)])
        curve = self.get_curve(curve_id)
        dlo = curve["quote_domain"]["lo"]
        dhi = curve["quote_domain"]["hi"]
        if not (dlo <= q <= dhi):
            raise ValidationError(
                f"订量 {q} 超出曲线报价域 [{dlo},{dhi}]", [str(q)])
        status, price = status_at(curve, q)
        return {
            "curve_id": curve_id,
            "curve_content_hash": curve["content_hash"],
            "q": q,
            "status": status,
            "unit_price": qstr(price) if price is not None else None,
            "price_relation": curve["price_relation"],
            "q_variable": curve["q_variable"],
            "price_variable": curve["price_variable"],
            "frozen_q_value": curve["frozen_q_value"],
            # 外部模型的实际钉选版本随曲线冻结，单点查询原样带出
            "scope_snapshots": curve["scope_snapshots"],
            "source_equations": curve["source_equations"],
        }

    # ------------------------------------------------------------- 预算核对
    def check_budget(self, curve_id, spec, label=None):
        curve = self.get_curve(curve_id)
        report = evaluate_budget(curve, spec, label=label)
        budget_id = self._next_seq(self.budget_dir, "budget")
        report["budget_id"] = budget_id
        report["curve_id"] = curve_id
        # id 是持久化序号，不参与内容寻址；验证器按同一键集复算
        report["report_hash"] = content_hash(
            {k: v for k, v in report.items() if k not in ("report_hash", "budget_id")}
        )
        write_json_atomic(self._budget_path(budget_id), report)
        return report

    def get_budget_report(self, budget_id):
        path = self._budget_path(budget_id)
        if not os.path.exists(path):
            raise NotFoundError(f"预算核对报告 {budget_id} 不存在")
        return read_json(path)

    def list_budget_reports(self):
        if not os.path.isdir(self.budget_dir):
            return []
        ids = sorted(
            (f[:-5] for f in os.listdir(self.budget_dir)
             if f.startswith("budget") and f.endswith(".json")),
            key=lambda bid: int(bid[6:]),
        )
        return [self.get_budget_report(bid) for bid in ids]
