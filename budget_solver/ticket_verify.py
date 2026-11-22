"""票版与签账回执的独立验证器（与发行/签账走不同代码路径）。

不读票版发行时保存的消元结果，而是从编译产物的原始方程出发，独立重建闭包方程、
独立消元，再核对：

* 闭包变量集、票面/内页划分与发行记录一致；
* 联合闭包本身相容（矛盾闭包不该有票版）；
* 发行的每条票面等式都在闭包方程行空间内且右端一致（健全性）；
* 票面等式的投影行空间恰好等于闭包消去内页后的行空间（完备性，不多不少）；
* 内页枢轴表达式代回原方程残差为 0；
* issue_hash 重算一致。

签账回执另核：回签三重匹配、票面残差为 0、误抄栏数在容错内、合计可由复原赋值
独立重算得到。
"""

from fractions import Fraction

from .errors import VerifyError
from .linspace import rref_traced, row_rank, in_span
from .linspace import solve_affine
from .solver import dependency_closure
from .util import content_hash


def _record(checks, name, ok, detail=""):
    checks.append({"check": name, "ok": bool(ok), "detail": detail})
    if not ok:
        raise VerifyError(f"票版验证失败：{name} {detail}")


def _independent_elimination(artifact, ticket):
    """从编译产物原方程独立重建闭包并消元，返回独立消元产物。"""
    face_vars = ticket["face_variables"]
    closure = set()
    for v in face_vars:
        closure.update(dependency_closure(artifact, v))
    closure = sorted(closure)
    if closure != ticket["closure"]:
        raise VerifyError("票版闭包与独立重建闭包不一致")
    inner = [v for v in closure if v not in face_vars]
    order = inner + face_vars
    index = {v: i for i, v in enumerate(order)}
    matrix, rhs, origins = [], [], []
    for v in closure:
        eq = artifact["equations"][v]
        row = [Fraction(0)] * len(order)
        for ref, coef in eq["coeffs"].items():
            row[index[ref]] = Fraction(coef)
        matrix.append(row)
        rhs.append(Fraction(eq["rhs"]))
        origins.append(v)
    rows, rrhs, traced, pivots = rref_traced(matrix, rhs, origins)
    n_inner = len(inner)
    face_rows, face_rhs = [], []
    for r, row in enumerate(rows):
        if all(row[c] == 0 for c in range(n_inner)):
            if any(row[c] != 0 for c in range(n_inner, len(order))):
                face_rows.append([row[c] for c in range(n_inner, len(order))])
                face_rhs.append(rrhs[r])
    return order, inner, face_vars, rows, rrhs, pivots, face_rows, face_rhs


def verify_ticket(artifact, ticket):
    checks = []
    if ticket["artifact_hash"] != artifact["artifact_hash"]:
        raise VerifyError("票版绑定的编译产物哈希不符")
    order, inner, face_vars, rows, rrhs, pivots, face_rows, face_rhs = \
        _independent_elimination(artifact, ticket)
    n_inner = len(inner)
    n_face = len(face_vars)

    # 1) 联合闭包必须相容
    probe = solve_affine(
        [
            {
                "coeffs": {order[c]: Fraction(rows[r][c]) for c in range(len(order))
                           if rows[r][c] != 0},
                "rhs": rrhs[r],
                "origin": r,
            }
            for r in range(len(rows))
        ],
        order,
    )
    _record(checks, "联合闭包独立复算相容", probe["status"] == "consistent")

    # 2) 发行票面等式逐条独立复算：必须落在独立消元的票面行空间内
    face_index = {v: i for i, v in enumerate(face_vars)}
    issued_vectors, issued_rhs = [], []
    for eq in ticket["face_equations"]:
        vec = [Fraction(0)] * n_face
        for var, coef in eq["coeffs"].items():
            vec[face_index[var]] = Fraction(coef)
        issued_vectors.append(vec)
        issued_rhs.append(Fraction(eq["rhs"]))
    for i, (vec, rhs_v) in enumerate(zip(issued_vectors, issued_rhs)):
        _record(
            checks, f"票面等式 {i} 行空间归属（健全性）",
            in_span(vec, face_rows, n_face),
        )
        # 右端归属：齐次嵌入 [系数 | -rhs] 必须落在 [投影行 | -投影右端] 行空间
        homo_space = [list(r) + [-rr] for r, rr in zip(face_rows, face_rhs)]
        homo_row = list(vec) + [-rhs_v]
        _record(
            checks, f"票面等式 {i} 右端一致",
            in_span(homo_row, homo_space, n_face + 1), f"rhs={rhs_v}")

    # 3) 完备性：两个行空间必须相等（秩相等 + 发行行属于独立行空间）
    rank_issued = row_rank(issued_vectors, n_face)
    rank_independent = row_rank(face_rows, n_face)
    _record(
        checks, "票面等式投影行空间恰好完备（不多不少）",
        rank_issued == rank_independent,
        f"issued={rank_issued} independent={rank_independent}",
    )

    # 4) 内页枢轴行：系数向量与右端都必须源自原方程。
    # 右端归属用齐次嵌入判定：[系数 | -rhs] 必须落在原方程 [A | -b] 的行空间内。
    eq_map = artifact["equations"]
    raw_rows = [
        [Fraction(eq_map[v]["coeffs"].get(col, 0)) for col in order]
        for v in ticket["closure"]
    ]
    raw_rhs = [Fraction(eq_map[v]["rhs"]) for v in ticket["closure"]]
    homo_space = [list(r) + [-rr] for r, rr in zip(raw_rows, raw_rhs)]
    for row in ticket["inner_pivot_rows"]:
        full_vec = [Fraction(0)] * len(order)
        for var, coef in row["inner_coeffs"].items():
            full_vec[order.index(var)] = Fraction(coef)
        for var, coef in row["face_coeffs"].items():
            full_vec[order.index(var)] = Fraction(coef)
        _record(
            checks, f"内页枢轴 {row['pivot_variable']} 系数源自原方程行空间",
            in_span(full_vec, raw_rows, len(order)),
        )
        homo_row = full_vec + [-Fraction(row["rhs"])]
        _record(
            checks, f"内页枢轴 {row['pivot_variable']} 右端可由原方程组合重算",
            in_span(homo_row, homo_space, len(order) + 1),
            f"rhs={row['rhs']}",
        )

    # 5) issue_hash 重算
    expect = content_hash(
        {k: v for k, v in ticket.items() if k != "issue_hash"}
    )
    _record(checks, "票版 issue_hash 一致", expect == ticket["issue_hash"])
    return {"ok": True, "check_count": len(checks), "checks": checks}


def verify_booking(artifact, ticket, order):
    """签账回执独立核对（含直接记账与询证后签账两条路径）。"""
    checks = []
    booking = order.get("booking")
    if not booking:
        raise VerifyError(f"订单 {order['order_id']} 尚未签账")
    if order["ticket_id"] != ticket["ticket_id"]:
        raise VerifyError("回执票号与票版不符")

    assignment = {v: Fraction(f) for v, f in booking["face_assignment"].items()}
    # 票面等式残差全 0
    for eq in ticket["face_equations"]:
        lhs = sum(Fraction(c) * assignment.get(v, 0)
                  for v, c in eq["coeffs"].items())
        _record(
            checks, f"复原代回票面 {eq['eq_id']} 残差为 0",
            lhs == Fraction(eq["rhs"]), f"{lhs} vs {eq['rhs']}",
        )
    # 误抄栏数在容错内（空栏不计），且与回执自报栏数一致
    paper = {v: Fraction(f) for v, f in order["paper"].items()}
    mismatches = sum(1 for v, f in paper.items() if assignment.get(v) != f)
    _record(
        checks, "复原误抄栏数在容错上限内",
        mismatches <= order["tolerance_k"],
        f"mismatches={mismatches} k={order['tolerance_k']}",
    )
    _record(checks, "回执自报误抄栏数与独立复算一致",
            mismatches == booking["mismatch_count"],
            f"{mismatches} vs {booking['mismatch_count']}")
    # 回签三重匹配且答数与复原算式一致
    plan_ids = set(order["plan"]["item_ids"]) if order.get("plan") else set()
    for reply in order.get("replies", []):
        _record(checks, f"回签 {reply['item_id']} 票号匹配",
                reply["ticket_id"] == ticket["ticket_id"])
        _record(checks, f"回签 {reply['item_id']} 发行版匹配",
                reply["issue_hash"] == ticket["issue_hash"])
        _record(checks, f"回签 {reply['item_id']} 属实际发出套餐",
                reply["item_id"] in plan_ids)
        item = next(it for it in order["catalog"]
                    if it["item_id"] == reply["item_id"])
        form = item["form"]
        expected = Fraction(form.get("constant", 0)) + sum(
            Fraction(c) * assignment.get(v, 0)
            for v, c in form["terms"].items()
        )
        _record(
            checks, f"回签 {reply['item_id']} 答数与复原算式一致",
            expected == Fraction(reply["value"]),
            f"{expected} vs {reply['value']}",
        )
    # 合计独立重算
    form = order["total_form"]
    total = Fraction(form.get("constant", 0)) + sum(
        Fraction(c) * assignment.get(v, 0) for v, c in form["terms"].items()
    )
    _record(checks, "待记合计由复原独立重算一致",
            total == Fraction(booking["booked_total"]),
            f"{total} vs {booking['booked_total']}")
    _record(checks, "booking_hash 一致",
            booking["booking_hash"] == content_hash(
                {k: v for k, v in booking.items() if k != "booking_hash"}))
    return {"ok": True, "check_count": len(checks), "checks": checks,
            "booked_total": booking["booked_total"]}
