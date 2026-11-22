"""印务处：票面发行与校版（票版校验关系）。

票版从一份**指定冻结快照的编译产物**上发行：

1. 从至多六个永久格（票面格 ``face_vars``）出发，合并它们在实际钉选作用域中的
   方程依赖闭包；闭包内其余格称内页格；
2. 先整体求解闭包：联合闭包矛盾则**不能发版**（带回原方程 λ 组合证书）；
3. 在有理数上消去内页格（带行溯源的 Gauss-Jordan），只剩票面仿射等式
   ``Σ aᵢ·xᵢ = b``；内页枢轴行保留下来，供校版员把票面补写成完整解；
4. 发行的校验关系满足两条对偶性质：
   * 健全性——任一合法内页填写（闭包完整解）投到票面后必然满足全部票面等式；
   * 完备性——任一通过票面等式的票面值都能补成同一闭包的完整解
     （自由内页/自由票面取 0 即得一个基点，零空间给出全部参数方向）。

校版员通过 :meth:`TicketOffice.check_face` 一次联合核查一组票面值：相容则拿到
内页补写与参数方向，矛盾则拿到票面等式/观测行的 λ 组合冲突依据。单栏各自未定
时，系统只允许它们共享同一组联合自由参数，不能各选各的。

票版整体带内容哈希并落盘，重开后可凭原票号继续校版/回签。
"""

import os
from fractions import Fraction

from .errors import BudgetError, NotFoundError, ValidationError
from .linspace import rref_traced, solve_affine
from .rationals import qstr
from .solver import dependency_closure
from .util import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now_iso,
    write_json_atomic,
)

TICKET_SCHEMA_VERSION = 1
MAX_FACE_CELLS = 6


def _fmap_str(mp):
    return {k: qstr(v) for k, v in mp.items() if v != 0}


def _parse_fmap(mp):
    return {k: Fraction(v) for k, v in mp.items()}


class TicketOffice:
    """票版发行/校版的持久化门面；构造即重开。"""

    def __init__(self, catalog, artifact_dir, ticket_dir):
        self.catalog = catalog
        self.artifact_dir = os.path.abspath(artifact_dir)
        self.ticket_dir = os.path.abspath(ticket_dir)
        ensure_dir(self.ticket_dir)

    # ------------------------------------------------------------- 路径/读取
    def _path(self, ticket_id):
        return os.path.join(self.ticket_dir, f"{ticket_id}.json")

    def get_ticket(self, ticket_id):
        path = self._path(ticket_id)
        if not os.path.exists(path):
            raise NotFoundError(f"票版 {ticket_id} 不存在")
        return read_json(path)

    def list_tickets(self):
        if not os.path.isdir(self.ticket_dir):
            return []
        return sorted(
            f[:-5] for f in os.listdir(self.ticket_dir) if f.endswith(".json")
        )

    # --------------------------------------------------------------- 发行
    def issue(self, artifact, face_vars, ticket_id, label=None):
        """在编译产物上发行票版；联合闭包矛盾时抛 :class:`TicketRejectedError`。"""
        if not isinstance(face_vars, (list, tuple)) or not face_vars:
            raise ValidationError("票面格至少 1 个", ["face_vars"])
        face_vars = list(dict.fromkeys(face_vars))  # 去重保序
        if len(face_vars) > MAX_FACE_CELLS:
            raise ValidationError(
                f"票面永久格至多 {MAX_FACE_CELLS} 个，实际 {len(face_vars)} 个",
                [f"face_count={len(face_vars)}"],
            )
        unknown = [v for v in face_vars if v not in artifact["variables"]]
        if unknown:
            raise ValidationError(f"票面格不在编译产物中：{unknown}", unknown)
        if os.path.exists(self._path(ticket_id)):
            raise ValidationError(f"票号 {ticket_id} 已存在，票号不可复用", [ticket_id])

        # 1) 合并实际钉选作用域中的方程依赖闭包
        closure_set = set()
        for v in face_vars:
            closure_set.update(dependency_closure(artifact, v))
        closure = sorted(closure_set)
        face_set = set(face_vars)
        inner_vars = [v for v in closure if v not in face_set]
        var_order = inner_vars + face_vars  # 消元列序：先内页后票面
        index = {v: i for i, v in enumerate(var_order)}

        raw_eqs = [artifact["equations"][v] for v in closure]
        matrix, rhs, origins = [], [], []
        for eq in raw_eqs:
            row = [Fraction(0)] * len(var_order)
            for ref, coef_str in eq["coeffs"].items():
                row[index[ref]] = Fraction(coef_str)
            matrix.append(row)
            rhs.append(Fraction(eq["rhs"]))
            origins.append(eq["lhs_variable"])

        # 2) 联合闭包整体相容性：矛盾则不能发版
        closure_eqs = [
            {"coeffs": dict(zip(var_order, row)), "rhs": rrhs, "origin": origin}
            for row, rrhs, origin in zip(matrix, rhs, origins)
        ]
        probe = solve_affine(closure_eqs, var_order)
        if probe["status"] == "inconsistent":
            raise TicketRejectedError(
                f"联合闭包矛盾，票版 {ticket_id} 不能发行",
                {
                    "closure": closure,
                    "certificate": [
                        {"equation_variable": token, "lambda": qstr(lam)}
                        for token, lam in probe["certificate"]
                    ],
                    "residual_rhs": qstr(probe["residual_rhs"]),
                },
            )

        # 3) 消去内页格（同一套带溯源消元，保留全部行供补写/独立核对）
        rows, rrhs, traced, pivot_cols = rref_traced(matrix, rhs, origins)
        n_inner = len(inner_vars)
        face_equations, pivot_rows = [], []
        for r, row in enumerate(rows):
            inner_part = {inner_vars[c]: row[c] for c in range(n_inner) if row[c] != 0}
            face_part = {
                var_order[c]: row[c] for c in range(n_inner, len(var_order))
                if row[c] != 0
            }
            if not inner_part:
                if face_part:  # 0 = 0 的冗余行直接丢弃
                    face_equations.append(
                        {
                            "eq_id": f"F{len(face_equations)}",
                            "coeffs": _fmap_str(face_part),
                            "rhs": qstr(rrhs[r]),
                            "derived_from": [
                                {"equation_variable": token, "lambda": qstr(lam)}
                                for token, lam in sorted(
                                    traced[r].items(), key=lambda kv: str(kv[0])
                                )
                            ],
                        }
                    )
            else:
                pivot_var = next(
                    (var_order[c] for c in pivot_cols
                     if c < n_inner and row[c] == 1),
                    None,
                )
                pivot_rows.append(
                    {
                        "pivot_variable": pivot_var,
                        "inner_coeffs": _fmap_str(inner_part),
                        "face_coeffs": _fmap_str(face_part),
                        "rhs": qstr(rrhs[r]),
                        "derived_from": [
                            {"equation_variable": token, "lambda": qstr(lam)}
                            for token, lam in sorted(
                                traced[r].items(), key=lambda kv: str(kv[0])
                            )
                        ],
                    }
                )

        ticket = {
            "ticket_kind": "affine_face_issue",
            "schema_version": TICKET_SCHEMA_VERSION,
            "ticket_id": ticket_id,
            "label": label,
            "created_at": utc_now_iso(),
            "artifact_hash": artifact["artifact_hash"],
            "root_model_id": artifact["root_model_id"],
            "root_snap_id": artifact["root_snap_id"],
            "scope_snapshots": artifact["scope_snapshots"],
            "face_variables": list(face_vars),
            "inner_variables": inner_vars,
            "closure": closure,
            "face_equations": face_equations,
            "inner_pivot_rows": pivot_rows,
            "source_equations": [
                {
                    "variable": eq["lhs_variable"],
                    "coeffs": dict(eq["coeffs"]),
                    "rhs": eq["rhs"],
                    "cell_hash": eq["cell_hash"],
                }
                for eq in raw_eqs
            ],
        }
        ticket["issue_hash"] = content_hash(
            {k: v for k, v in ticket.items() if k != "issue_hash"}
        )
        write_json_atomic(self._path(ticket_id), ticket)
        return ticket

    # --------------------------------------------------------------- 校版
    def check_face(self, ticket_id, assignment):
        """联合核查一组票面值是否相容。

        ``assignment``: ``{票面变量: Fraction 或分数字符串}``，允许只给部分栏
        （缺省栏即留空，由联合自由参数决定）。相容返回票面参数解与内页补写；
        矛盾返回 λ 组合冲突依据。
        """
        ticket = self.get_ticket(ticket_id)
        face_vars = ticket["face_variables"]
        values = {}
        for var, value in assignment.items():
            if var not in face_vars:
                raise ValidationError(f"{var} 不是票版 {ticket_id} 的票面栏", [var])
            values[var] = value if isinstance(value, Fraction) else Fraction(value)

        equations = []
        for eq in ticket["face_equations"]:
            equations.append(
                {
                    "coeffs": _parse_fmap(eq["coeffs"]),
                    "rhs": Fraction(eq["rhs"]),
                    "origin": ("face_eq", eq["eq_id"]),
                }
            )
        for var, value in sorted(values.items()):
            equations.append(
                {"coeffs": {var: Fraction(1)}, "rhs": value,
                 "origin": ("paper", var)}
            )
        solved = solve_affine(equations, face_vars)
        if solved["status"] == "inconsistent":
            return {
                "status": "inconsistent",
                "ticket_id": ticket_id,
                "issue_hash": ticket["issue_hash"],
                "checked_faces": sorted(values),
                "certificate": [
                    {"origin": list(token), "lambda": qstr(lam)}
                    for token, lam in solved["certificate"]
                ],
                "residual_rhs": qstr(solved["residual_rhs"]),
            }

        face_point = solved["point"]
        face_nullspace = solved["nullspace"]
        # 内页补写：自由内页取 0；RREF 枢轴行中除自身枢轴外，其余枢位列均为 0，
        # 故可直接逐行算出内页值（票面代入基点、自由内页为 0）。
        inner_point = {v: Fraction(0) for v in ticket["inner_variables"]}
        for row in ticket["inner_pivot_rows"]:
            total = Fraction(row["rhs"])
            for var, coef in _parse_fmap(row["face_coeffs"]).items():
                total -= coef * face_point[var]
            for var, coef in _parse_fmap(row["inner_coeffs"]).items():
                if var != row["pivot_variable"]:
                    total -= coef * inner_point[var]
            inner_point[row["pivot_variable"]] = total
        return {
            "status": "consistent",
            "ticket_id": ticket_id,
            "issue_hash": ticket["issue_hash"],
            "checked_faces": sorted(values),
            "face_solution": {
                "point": {v: qstr(face_point[v]) for v in face_vars},
                "nullspace": [
                    {v: qstr(vec.get(v, 0)) for v in face_vars if vec.get(v, 0) != 0}
                    for vec in face_nullspace
                ],
                "free_variables": solved["free_vars"],
            },
            "inner_completion": {
                "point": {v: qstr(inner_point[v]) for v in sorted(inner_point)},
            },
            "full_closure_point": {
                **{v: qstr(face_point[v]) for v in face_vars},
                **{v: qstr(inner_point[v]) for v in sorted(inner_point)},
            },
        }


class TicketRejectedError(BudgetError):
    """联合闭包矛盾或票面构造非法，不能发版；携带矛盾证书明细。"""

    def __init__(self, message, detail=None):
        super().__init__(message)
        self.detail = detail or {}
