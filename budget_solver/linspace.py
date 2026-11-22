"""通用有理数仿射线性代数（票面/询证共用的底座）。

与 :mod:`budget_solver.solver` 中按 SCC 分块的消元不同，本模块面对的是已经切好
的小型仿射系统（票面至多六个永久格，询证至多四项），因此直接在整组变量上做带
*行溯源* 的 Gauss-Jordan 消元，并提供：

* :func:`solve_affine` —— 仿射系统的参数解（基点 + 零空间基）或矛盾 λ 证书；
* :func:`rref_traced` —— 每行记录它由哪些原始行按什么 λ 组合而来；
* :func:`nullspace` / :func:`row_rank` / :func:`in_span` —— 零空间、秩、行空间判定；
* :func:`substitute` —— 赋值代回方程求残差。

全程 :class:`fractions.Fraction`，无浮点。溯源 token 可以是任意可哈希对象
（解算器用格变量名，票面用方程编号/观测栏名），证书按 token 回传。
"""

from fractions import Fraction


def rref_traced(matrix, rhs, origins):
    """带行溯源的精确 Gauss-Jordan 消元。

    ``matrix`` 为 n×m 系数行，``rhs`` 为 n 个常数右端，``origins`` 为每行的溯源
    token。返回 ``(rows, rhs, traced, pivot_cols)``，其中 ``traced[r]`` 是结果行
    相对原始行的 λ 组合 ``{token: Fraction}``。
    """
    rows = [list(row) for row in matrix]
    rhs = list(rhs)
    traced = [{token: Fraction(1)} for token in origins]
    n_rows = len(rows)
    n_cols = n_rows and len(rows[0]) or 0
    pivot_cols = []
    pivot_row = 0
    for col in range(n_cols):
        candidates = [r for r in range(pivot_row, n_rows) if rows[r][col] != 0]
        if not candidates:
            continue
        swap = candidates[0]
        if swap != pivot_row:
            rows[pivot_row], rows[swap] = rows[swap], rows[pivot_row]
            rhs[pivot_row], rhs[swap] = rhs[swap], rhs[pivot_row]
            traced[pivot_row], traced[swap] = traced[swap], traced[pivot_row]
        pivot = rows[pivot_row][col]
        rows[pivot_row] = [v / pivot for v in rows[pivot_row]]
        rhs[pivot_row] = rhs[pivot_row] / pivot
        traced[pivot_row] = {k: v / pivot for k, v in traced[pivot_row].items()}
        for r in range(n_rows):
            if r == pivot_row or rows[r][col] == 0:
                continue
            factor = rows[r][col]
            rows[r] = [a - factor * b for a, b in zip(rows[r], rows[pivot_row])]
            rhs[r] = rhs[r] - factor * rhs[pivot_row]
            merged = dict(traced[r])
            for k, v in traced[pivot_row].items():
                merged[k] = merged.get(k, Fraction(0)) - factor * v
            traced[r] = {k: v for k, v in merged.items() if v != 0}
        pivot_cols.append(col)
        pivot_row += 1
        if pivot_row == n_rows:
            break
    return rows, rhs, traced, pivot_cols


def solve_affine(equations, variables):
    """求解仿射方程组。

    ``equations`` 为 ``[{"coeffs": {var: Fraction}, "rhs": Fraction, "origin": token}]``；
    方程形状为 ``Σ coeffs·var = rhs``。

    相容时返回::

        {"status": "consistent", "variables": [...], "point": {var: F},
         "nullspace": [{var: F}, ...], "free_vars": [...], "pivot_vars": [...]}

    基点把自由变量取 0；零空间每个基向量对应一个自由变量（该自由位为 1）。
    矛盾时返回 ``{"status": "inconsistent", "certificate": [(token, lambda)...],
    "residual_rhs": F}``，证书行满足左端恒零、右端为非零残差。
    """
    index = {v: i for i, v in enumerate(variables)}
    matrix, rhs, origins = [], [], []
    for eq in equations:
        row = [Fraction(0)] * len(variables)
        for var, coef in eq["coeffs"].items():
            if coef != 0:
                row[index[var]] = coef
        matrix.append(row)
        rhs.append(Fraction(eq["rhs"]))
        origins.append(eq.get("origin"))
    rows, rrhs, traced, pivot_cols = rref_traced(matrix, rhs, origins)
    pivot_set = set(pivot_cols)
    free_cols = [c for c in range(len(variables)) if c not in pivot_set]
    for r, row in enumerate(rows):
        if all(v == 0 for v in row) and rrhs[r] != 0:
            return {
                "status": "inconsistent",
                "variables": list(variables),
                "certificate": sorted(traced[r].items(), key=lambda kv: str(kv[0])),
                "residual_rhs": rrhs[r],
            }
    point = {v: Fraction(0) for v in variables}
    pivot_row_of = {col: r for r, col in enumerate(pivot_cols)}
    for col, r in pivot_row_of.items():
        point[variables[col]] = rrhs[r]
    nullspace = []
    for fc in free_cols:
        vector = {variables[fc]: Fraction(1)}
        for col, r in pivot_row_of.items():
            if rows[r][fc] != 0:
                vector[variables[col]] = -rows[r][fc]
        nullspace.append(vector)
    return {
        "status": "consistent",
        "variables": list(variables),
        "point": point,
        "nullspace": nullspace,
        "free_vars": [variables[c] for c in free_cols],
        "pivot_vars": [variables[c] for c in pivot_cols],
    }


def nullspace(matrix_rows, n_cols):
    """矩阵的零空间基（行向量输入，返回右零空间基，形状同 :func:`solve_affine`）。"""
    zeros_rhs = [Fraction(0)] * len(matrix_rows)
    rows, _, _, pivot_cols = rref_traced(
        [list(r) for r in matrix_rows], zeros_rhs, list(range(len(matrix_rows)))
    )
    pivot_set = set(pivot_cols)
    basis = []
    for fc in range(n_cols):
        if fc in pivot_set:
            continue
        vector = {fc: Fraction(1)}
        for r, col in enumerate(pivot_cols):
            if rows[r][fc] != 0:
                vector[col] = -rows[r][fc]
        basis.append(vector)
    return basis


def row_rank(rows, n_cols):
    """行秩。"""
    if not rows:
        return 0
    _, _, _, pivot_cols = rref_traced(
        [list(r) for r in rows], [Fraction(0)] * len(rows), list(range(len(rows)))
    )
    return len(pivot_cols)


def in_span(vector, basis, n_cols):
    """``vector`` 是否属于 ``basis`` 张成的行空间（按秩比较判定）。"""
    base_rows = [list(r) for r in basis]
    rank_before = row_rank(base_rows, n_cols)
    rank_after = row_rank(base_rows + [list(vector)], n_cols)
    return rank_before == rank_after


def substitute(coeffs, assignment):
    """把赋值代回 ``Σ coeffs·var``，缺省变量按 0 计。"""
    return sum(Fraction(coef) * assignment.get(var, Fraction(0))
               for var, coef in coeffs.items())


def affine_value(form, assignment):
    """求仿射式 ``constant + Σ terms·var`` 在赋值下的值。"""
    total = Fraction(form.get("constant", 0))
    for var, coef in form["terms"].items():
        total += Fraction(coef) * assignment.get(var, Fraction(0))
    return total


def form_direction(form, null_vector):
    """仿射式沿零空间方向的方向导数（0 表示该式在这个自由方向上不变）。"""
    return sum(Fraction(coef) * null_vector.get(var, Fraction(0))
               for var, coef in form["terms"].items())
