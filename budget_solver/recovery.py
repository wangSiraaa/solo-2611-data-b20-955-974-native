"""收单员：纸票容错复原与辨错。

纸票有实际非空栏（空栏不算错）、容错上限 ``k``（k≤2）与一个待记线性合计
（票面变量的仿射式）。本模块枚举至多 k 栏的误抄位置，把“未误抄栏必须等于纸值”
作为观测方程，与票面等式**联合**求解（单栏未定也不允许各挑各的自由参数），
得到全部相容复原——每个复原是一个票面仿射子空间（基点 + 联合零空间）。

在容错上限内没有任何相容复原时直接判退（``rejected``），不发询证。否则给出：

* 哪些票面栏在全部复原中已经唯一；
* 已填栏哪些必错（每个相容复原都与纸值不符）、哪些可能错；
* 每个误抄集合下的相容复原（基点 + 自由方向 + 该子空间内的合计）；
* 合计是否已在全部复原中固定——固定即可直接记账，否则进入询证规划。
"""

from itertools import combinations
from fractions import Fraction

from .errors import ValidationError
from .linspace import affine_value, form_direction, solve_affine, substitute
from .rationals import qstr


def parse_form(raw):
    """JSON 边界的仿射式：{"constant": "..", "terms": {票面变量: 系数}}。"""
    if not isinstance(raw, dict):
        raise ValidationError("合计算式必须是对象", ["total_form"])
    terms = raw.get("terms", {})
    if not isinstance(terms, dict):
        raise ValidationError("合计算式 terms 必须是对象", ["total_form.terms"])
    return {
        "constant": Fraction(raw.get("constant", 0)),
        "terms": {var: Fraction(coef) for var, coef in terms.items()},
    }


def form_to_json(form):
    return {
        "constant": qstr(form["constant"]),
        "terms": {var: qstr(coef) for var, coef in sorted(form["terms"].items())},
    }


def _subspace_key(solution):
    point = tuple(sorted((v, qstr(f)) for v, f in solution["point"].items()))
    null = tuple(
        tuple(sorted((v, qstr(f)) for v, f in vec.items() if f != 0))
        for vec in solution["nullspace"]
    )
    return point, null


def _face_system(ticket):
    return [
        {
            "coeffs": {v: Fraction(c) for v, c in eq["coeffs"].items()},
            "rhs": Fraction(eq["rhs"]),
            "origin": ("face_eq", eq["eq_id"]),
        }
        for eq in ticket["face_equations"]
    ]


def feasible_recoveries(ticket, paper, k):
    """枚举全部互异的相容复原子空间。

    返回 ``[{"error_cells": [...], "solution": solve_affine 结果}]``，按误抄栏数
    与栏名稳定排序，子空间按基点/零空间规范化去重。
    """
    face_vars = ticket["face_variables"]
    filled = sorted(paper)
    unknown = [v for v in filled if v not in face_vars]
    if unknown:
        raise ValidationError(f"纸票含非票面栏：{unknown}", unknown)
    if not isinstance(k, int) or isinstance(k, bool) or not 0 <= k <= 2:
        raise ValidationError("容错 k 必须是 0、1 或 2", [f"k={k!r}"])
    base_system = _face_system(ticket)
    found = {}
    for size in range(min(k, len(filled)) + 1):
        for error_cells in combinations(filled, size):
            error_set = set(error_cells)
            equations = list(base_system)
            for var in filled:
                if var in error_set:
                    continue
                equations.append(
                    {"coeffs": {var: Fraction(1)}, "rhs": paper[var],
                     "origin": ("paper", var)}
                )
            solved = solve_affine(equations, face_vars)
            if solved["status"] != "consistent":
                continue
            key = _subspace_key(solved)
            if key in found:
                # 同一子空间的更小误抄集合优先保留（少错优先）
                if len(error_cells) < len(found[key]["error_cells"]):
                    found[key]["error_cells"] = list(error_cells)
                continue
            found[key] = {"error_cells": list(error_cells), "solution": solved}
    return sorted(
        found.values(),
        key=lambda item: (len(item["error_cells"]), item["error_cells"]),
    )


def _total_in_subspace(form, solution):
    point_total = affine_value(form, solution["point"])
    directions = []
    for vec in solution["nullspace"]:
        slope = form_direction(form, vec)
        if slope != 0:
            directions.append(
                {"null_vector": {v: qstr(f) for v, f in vec.items() if f != 0},
                 "total_slope": qstr(slope)}
            )
    return point_total, directions


def analyze_paper(ticket, paper_raw, total_form_raw, k):
    """纸票辨错主入口，产出结构化判读（见模块文档）。"""
    paper = {}
    for var, value in (paper_raw or {}).items():
        paper[var] = value if isinstance(value, Fraction) else Fraction(value)
    total_form = parse_form(total_form_raw)
    bad_refs = [v for v in total_form["terms"] if v not in ticket["face_variables"]]
    if bad_refs:
        raise ValidationError(f"合计算式引用了非票面栏：{bad_refs}", bad_refs)
    face_vars = ticket["face_variables"]

    recoveries = feasible_recoveries(ticket, paper, k)
    if not recoveries:
        return {
            "status": "rejected",
            "ticket_id": ticket["ticket_id"],
            "issue_hash": ticket["issue_hash"],
            "tolerance_k": k,
            "paper": {v: qstr(f) for v, f in sorted(paper.items())},
            "total_form": form_to_json(total_form),
            "message": f"容错上限 {k} 栏内无任何相容复原，整票退回，不发询证",
        }

    # 误抄集合的并/交：必错 = 每个可行误抄集合都含该栏；可能错 = 存在集合含该栏
    error_union = set()
    error_intersection = set(recoveries[0]["error_cells"])
    for item in recoveries:
        cells = set(item["error_cells"])
        error_union |= cells
        error_intersection &= cells
    filled = sorted(paper)
    must_error = sorted(v for v in filled if v in error_intersection)
    possibly_error = sorted(v for v in filled if v in error_union and v not in error_intersection)
    confirmed = sorted(
        v for v in filled if v not in error_union
    )  # 每个复原都与纸值一致的已填栏

    # 唯一栏：在全部子空间中方向为零且基点值相同
    def is_fixed_everywhere(var):
        values = set()
        for item in recoveries:
            sol = item["solution"]
            if any(vec.get(var, 0) != 0 for vec in sol["nullspace"]):
                return False
            values.add(sol["point"][var])
        return len(values) == 1

    unique_faces = {
        var: qstr(recoveries[0]["solution"]["point"][var])
        for var in face_vars if is_fixed_everywhere(var)
    }

    detailed = []
    total_values = set()
    total_fixed = True
    for item in recoveries:
        sol = item["solution"]
        point_total, directions = _total_in_subspace(total_form, sol)
        total_values.add(point_total)
        if directions:
            total_fixed = False
        detailed.append(
            {
                "error_cells": item["error_cells"],
                "face_point": {v: qstr(sol["point"][v]) for v in face_vars},
                "free_directions": [
                    {v: qstr(f) for v, f in vec.items() if f != 0}
                    for vec in sol["nullspace"]
                ],
                "free_variables": sol["free_vars"],
                "total_at_point": qstr(point_total),
                "total_free_directions": directions,
            }
        )
    if len(total_values) > 1:
        total_fixed = False
    status = "fixed" if total_fixed else "ambiguous"
    result = {
        "status": status,
        "ticket_id": ticket["ticket_id"],
        "issue_hash": ticket["issue_hash"],
        "tolerance_k": k,
        "paper": {v: qstr(f) for v, f in sorted(paper.items())},
        "blank_faces": [v for v in face_vars if v not in paper],
        "total_form": form_to_json(total_form),
        "unique_faces": unique_faces,
        "must_error_filled": must_error,
        "possibly_error_filled": possibly_error,
        "confirmed_filled": confirmed,
        "compatible_recoveries": detailed,
        "recovery_count": len(detailed),
    }
    if total_fixed:
        result["booked_total"] = qstr(next(iter(total_values)))
    else:
        result["possible_total_values"] = sorted(qstr(v) for v in total_values)
    return result


def paper_mismatch_count(paper, assignment):
    """赋值相对纸票非空栏的误抄栏数（空栏不计）。"""
    return sum(1 for var, value in paper.items() if assignment.get(var) != value)


def residual_of_face(ticket, assignment):
    """代回票面等式求残差，供恢复/回签独立核对。"""
    out = []
    for eq in ticket["face_equations"]:
        coeffs = {v: Fraction(c) for v, c in eq["coeffs"].items()}
        residual = substitute(coeffs, assignment) - Fraction(eq["rhs"])
        out.append({"eq_id": eq["eq_id"], "residual": qstr(residual)})
    return out
