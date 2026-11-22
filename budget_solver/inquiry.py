"""询证规划：在经费与分馆上限内选出足以签账的问项套餐。

本月询证目录至多四项，每项有名称、承办分馆、收费（有理数）和一条**票面线性
算式**（只许引用票面变量）。同一分馆的一批问项一起递送，收费按项分别计。
问项必须在看答案以前一次确定。

一套问项足以签账，当且仅当：对任意两张都能解释当前纸票的真实复原（即可行复原
子空间中的任意点），只要这套问项的所有回答相同，两张票的待记合计就必须相同。
原票其他栏可以永远未定。判定在有理数上做耦合仿射系统：取两个拷贝 x、x′，
令每项回答相等且两拷贝各自落在可行子空间内；若该耦合系统相容且合计在其上不恒
等，则存在“答数相同、合计不同”的两张有效票，套餐不充分，返回这对反例。
"""

from itertools import combinations
from fractions import Fraction

from .errors import ValidationError
from .linspace import affine_value, solve_affine
from .rationals import qstr

MAX_CATALOG_ITEMS = 4


class InquiryCatalog:
    """本月固定询证业务目录（至多四项），构造时做形状与票面作用域校验。"""

    def __init__(self, ticket, items):
        face_vars = set(ticket["face_variables"])
        if not isinstance(items, (list, tuple)) or not items:
            raise ValidationError("询证目录至少一项", ["inquiry_items"])
        if len(items) > MAX_CATALOG_ITEMS:
            raise ValidationError(
                f"本月固定询证业务至多 {MAX_CATALOG_ITEMS} 项",
                [f"item_count={len(items)}"],
            )
        ids = set()
        self.items = []
        for raw in items:
            for key in ("item_id", "name", "branch", "fee", "form"):
                if key not in raw:
                    raise ValidationError(f"询证项缺字段 {key}", [str(raw)])
            item_id = raw["item_id"]
            if item_id in ids:
                raise ValidationError(f"询证项号重复：{item_id}", [item_id])
            ids.add(item_id)
            form = {
                "constant": Fraction(raw["form"].get("constant", 0)),
                "terms": {
                    var: Fraction(coef)
                    for var, coef in raw["form"].get("terms", {}).items()
                },
            }
            bad = [v for v in form["terms"] if v not in face_vars]
            if bad:
                raise ValidationError(
                    f"询证项 {item_id} 引用了非票面栏：{bad}", [item_id]
                )
            fee = Fraction(raw["fee"])
            if fee < 0:
                raise ValidationError(f"询证项 {item_id} 收费不能为负", [item_id])
            self.items.append(
                {
                    "item_id": item_id,
                    "name": raw["name"],
                    "branch": raw["branch"],
                    "fee": fee,
                    "form": form,
                }
            )

    def to_json(self):
        return [
            {
                "item_id": it["item_id"],
                "name": it["name"],
                "branch": it["branch"],
                "fee": qstr(it["fee"]),
                "form": {
                    "constant": qstr(it["form"]["constant"]),
                    "terms": {v: qstr(c) for v, c in sorted(it["form"]["terms"].items())},
                },
            }
            for it in sorted(self.items, key=lambda it: it["item_id"])
        ]

    def get(self, item_id):
        for it in self.items:
            if it["item_id"] == item_id:
                return it
        raise ValidationError(f"询证项 {item_id} 不在本月目录中", [item_id])


def _build_copy_system(solution, suffix):
    """把一个仿射子空间显式展开为拷贝变量上的定义方程。

    子空间 ``x = point + N·p`` 写成等式 ``x_suffix = point + Σ_j null_j·p_suffix_j``，
    耦合求解时自由参数 p 作为独立变量参与，两拷贝各有自己的参数列，天然保留
    “同一子空间内也可能取不同自由参数”的判别能力。
    """
    equations = []
    face_vars = solution["variables"]
    point = solution["point"]
    nullspace = solution["nullspace"]
    param_names = [f"p_{suffix}_{j}" for j in range(len(nullspace))]
    for var in face_vars:
        coeffs = {f"{var}_{suffix}": Fraction(1)}
        rhs = point[var]
        for j, vec in enumerate(nullspace):
            if vec.get(var, 0) != 0:
                coeffs[param_names[j]] = -vec[var]
        equations.append({"coeffs": coeffs, "rhs": rhs,
                          "origin": ("copy", suffix, var)})
    return equations, param_names


def distinguish(catalog, chosen_ids, recoveries, total_form):
    """套餐判别：返回 None 表示足以签账，否则返回一对反例复原。

    ``recoveries`` 为 :func:`budget_solver.recovery.feasible_recoveries` 的产物。
    对每两个（可同一）可行子空间构造耦合系统：两拷贝都在各自子空间内、所有问项
    答数相等；若相容且合计差不恒为零，则该系统的一个基点解即“同答不同款”反例。
    """
    chosen = [catalog.get(i) for i in chosen_ids]
    for ai, item_a in enumerate(recoveries):
        for bi in range(ai, len(recoveries)):
            item_b = recoveries[bi]
            equations = []
            sys_a, pa = _build_copy_system(item_a["solution"], "a")
            sys_b, pb = _build_copy_system(item_b["solution"], "b")
            equations.extend(sys_a)
            equations.extend(sys_b)
            params = pa + pb
            # 回答相等：form(x_a) = form(x_b)，常数项在相减时消去
            for item in chosen:
                coeffs = {}
                for var, coef in item["form"]["terms"].items():
                    coeffs[f"{var}_a"] = coeffs.get(f"{var}_a", Fraction(0)) + coef
                    coeffs[f"{var}_b"] = coeffs.get(f"{var}_b", Fraction(0)) - coef
                equations.append(
                    {"coeffs": coeffs, "rhs": Fraction(0),
                     "origin": ("answer", item["item_id"])}
                )
            face_vars = list(item_a["solution"]["variables"])
            all_vars = [f"{v}_a" for v in face_vars] + [f"{v}_b" for v in face_vars] + params
            coupled = solve_affine(equations, all_vars)
            if coupled["status"] != "consistent":
                continue
            # 合计差在耦合系统上是否恒为零
            total_diff = {}
            for var, coef in total_form["terms"].items():
                total_diff[f"{var}_a"] = total_diff.get(f"{var}_a", Fraction(0)) + coef
                total_diff[f"{var}_b"] = total_diff.get(f"{var}_b", Fraction(0)) - coef
            point_diff = sum(coef * coupled["point"].get(v, 0)
                             for v, coef in total_diff.items())
            slope_nonzero = any(
                sum(coef * vec.get(v, 0) for v, coef in total_diff.items()) != 0
                for vec in coupled["nullspace"]
            )
            if point_diff != 0 or slope_nonzero:
                witness_a = {
                    v: coupled["point"][f"{v}_a"] for v in face_vars
                }
                witness_b = {
                    v: coupled["point"][f"{v}_b"] for v in face_vars
                }
                return {
                    "subspace_pair": [ai, bi],
                    "error_cells_pair": [item_a["error_cells"], item_b["error_cells"]],
                    "answers": {
                        item["item_id"]: qstr(
                            affine_value(item["form"], witness_a))
                        for item in chosen
                    },
                    "witness_a": {v: qstr(f) for v, f in sorted(witness_a.items())},
                    "witness_b": {v: qstr(f) for v, f in sorted(witness_b.items())},
                    "total_a": qstr(affine_value(total_form, witness_a)),
                    "total_b": qstr(affine_value(total_form, witness_b)),
                }
    return None


def _plan_stats(catalog, chosen_ids):
    chosen = [catalog.get(i) for i in chosen_ids]
    fee = sum((it["fee"] for it in chosen), Fraction(0))
    branches = sorted({it["branch"] for it in chosen})
    return fee, branches


def enumerate_plans(catalog, recoveries, total_form, budget, max_branches):
    """枚举两限内的套餐并按是否足以签账分类，给出可签套餐（少馆/少费排序）。"""
    budget = Fraction(budget)
    feasible, blocked = [], []
    ids = [it["item_id"] for it in catalog.items]
    for size in range(1, len(ids) + 1):
        for combo in combinations(ids, size):
            fee, branches = _plan_stats(catalog, combo)
            within_limits = fee <= budget and len(branches) <= max_branches
            counterexample = distinguish(catalog, list(combo), recoveries, total_form)
            record = {
                "item_ids": list(combo),
                "branches": branches,
                "branch_count": len(branches),
                "total_fee": qstr(fee),
                "within_limits": within_limits,
                "sufficient": counterexample is None,
                "counterexample": counterexample,
            }
            if within_limits and counterexample is None:
                feasible.append(record)
            else:
                blocked.append(record)
    # 经办人可在少跑馆和少付费之间选：先按馆数再按费用稳定排序
    feasible.sort(key=lambda r: (r["branch_count"], Fraction(r["total_fee"]),
                                 r["item_ids"]))
    return feasible, blocked


def recommend(catalog, recoveries, total_form, budget, max_branches):
    """主入口：返回可签套餐清单；无可用套餐时给阻塞依据（含同答不同款反例）。"""
    feasible, blocked = enumerate_plans(
        catalog, recoveries, total_form, budget, max_branches
    )
    if feasible:
        cheapest = min(feasible, key=lambda r: Fraction(r["total_fee"]))
        fewest = feasible[0]  # 已按馆数优先排序
        return {
            "status": "plannable",
            "budget": qstr(Fraction(budget)),
            "max_branches": max_branches,
            "plans": feasible,
            "fewest_branches_plan": fewest,
            "cheapest_plan": cheapest,
        }
    # 阻塞：优先给“两限内但问不出区别”的反例
    within = [r for r in blocked if r["within_limits"] and r["counterexample"]]
    over_limit = [r for r in blocked if not r["within_limits"]]
    return {
        "status": "blocked",
        "budget": qstr(Fraction(budget)),
        "max_branches": max_branches,
        "message": "两限内没有足以签账的套餐",
        "counterexamples": [r["counterexample"] for r in within],
        "plans_over_limits": [
            {
                "item_ids": r["item_ids"],
                "branches": r["branches"],
                "total_fee": r["total_fee"],
                "sufficient": r["sufficient"],
            }
            for r in over_limit
        ],
    }
