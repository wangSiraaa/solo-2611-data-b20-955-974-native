"""盘面理想示数求解：在冻结编译产物上替换输入格取值，按原 SCC 分块精确求解。

一个“盘面”由已动作触点组集合决定：被任何已动组接管的输入格取其终值，其余输入
格保持冻结模型的起值。求解时：

* 方程结构、系数、SCC 划分完全沿用编译产物（现场不重新编译模型）；
* 只有输入格方程 ``x = value`` 的右端常数按盘面替换，公式格一律不动；
* 对登记指针实际依赖闭包的**并集**联立求解，保证共享格只解一次、矛盾被联合发现；
* 联合矛盾 => 盘面不可检定（物理上仍可经过，只是不读数）；
  仅个别指针非唯一 => 只禁用那些指针，其余指针照常作读数。
"""

from fractions import Fraction

from ..rationals import qstr
from ..solver import rref_augmented


def plate_input_values(artifact, book, moved_groups):
    """根据已动组集合计算该盘面每个输入格的精确取值。"""
    groups = {g["group_id"]: g for g in book["groups"]}
    values = {}
    # 起值：编译产物里输入格方程的冻结右端
    for vname, var in artifact["variables"].items():
        if var["kind"] == "input":
            values[vname] = Fraction(artifact["equations"][vname]["rhs"])
    for gid in moved_groups:
        for assignment in groups[gid]["cells"]:
            vname = assignment["variable"]
            values[vname] = Fraction(assignment["to"])
    return values


def needle_closure_union(artifact, needle_variables):
    """登记指针实际依赖闭包的并集（沿编译产物真实引用边 BFS）。"""
    edges = {}
    for edge in artifact["dependency_edges"]:
        edges.setdefault(edge["from"], []).append(edge["to"])
    seen = set()
    queue = []
    for needle in needle_variables:
        if needle not in seen:
            seen.add(needle)
            queue.append(needle)
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for nxt in edges.get(node, ()):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return sorted(seen)


def solve_plate(artifact, closure, input_values):
    """在给定盘面上对闭包联立精确求解（SCC 拓扑分块 + 分数 RREF）。

    返回 dict：
      ``status``        -- "consistent" | "inconsistent"
      ``classified``    -- 变量 -> ("fixed", Fraction) | ("expr", const, {参数: 系数})
      ``free_variables``-- 自由参数变量名
      ``contradictions``-- 矛盾行溯源（与解算器报告同构的 λ 组合证书）
    """
    closure_set = set(closure)
    comp_of = artifact["var_component"]
    components = {c["scc_id"]: c for c in artifact["components"]}
    block_ids = []
    seen_blocks = set()
    for v in closure:
        cid = comp_of[v]
        if cid not in seen_blocks:
            seen_blocks.add(cid)
            block_ids.append(cid)
    block_ids.sort(key=lambda cid: components[cid]["topo_index"])

    known = {}
    free_params = set()
    contradictions = []

    for cid in block_ids:
        members = [v for v in components[cid]["variables"] if v in closure_set]
        local_index = {v: i for i, v in enumerate(members)}
        upstream_params = set()
        for v in members:
            for ref_var in artifact["equations"][v]["coeffs"]:
                if ref_var in local_index:
                    continue
                info = known[ref_var]
                if info[0] == "expr":
                    upstream_params.add(ref_var)
                    free_params.update(info[2].keys())
        upstream_params = sorted(upstream_params)
        free_params.update(upstream_params)

        n = len(members)
        matrix = [[Fraction(0)] * n for _ in range(n)]
        const_rhs = []
        param_rows = []
        for r, v in enumerate(members):
            eq = artifact["equations"][v]
            if artifact["variables"][v]["kind"] == "input":
                # 盘面替换：输入格方程 x = 盘面值（系数结构仍是单位行）
                rhs_const = input_values[v]
            else:
                rhs_const = Fraction(eq["rhs"])
            prow = {p: Fraction(0) for p in upstream_params}
            for ref_var, coef_str in eq["coeffs"].items():
                coef = Fraction(coef_str)
                if ref_var in local_index:
                    matrix[r][local_index[ref_var]] = coef
                    continue
                info = known[ref_var]
                if info[0] == "fixed":
                    rhs_const -= coef * info[1]
                else:
                    _, c0, parts = info
                    rhs_const -= coef * c0
                    for pv, pc in parts.items():
                        prow[pv] = prow.get(pv, Fraction(0)) - coef * pc
            const_rhs.append(rhs_const)
            param_rows.append([prow[p] for p in upstream_params])

        origins = [{v: Fraction(1)} for v in members]
        rows, rrhs, pmat, rorig, pivot_cols, bad = rref_augmented(
            matrix, const_rhs, param_rows, origins
        )
        if bad:
            for r in bad:
                combo = [
                    {"equation_variable": ev, "lambda": qstr(lam)}
                    for ev, lam in sorted(rorig[r].items()) if lam != 0
                ]
                contradictions.append(
                    {
                        "residual_lhs": "0",
                        "residual_rhs": qstr(rrhs[r]),
                        "scc_id": cid,
                        "equation_combination": combo,
                    }
                )
            return {
                "status": "inconsistent",
                "classified": {v: {"status": "inconsistent"} for v in closure},
                "free_variables": sorted(free_params),
                "contradictions": contradictions,
            }

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

    return {
        "status": "consistent",
        "classified": known,
        "free_variables": sorted(free_params),
        "contradictions": [],
    }


def needle_readings(artifact, book, plate, line=None):
    """计算一个盘面的全部登记针读数分类。

    返回 ``(plate_record, readable)``：``plate_record`` 可直接入实况册，
    ``readable`` 为 ``{针变量: Fraction 理想示数}``，只含唯一确定的针。
    """
    moved = tuple(plate["moved_groups"])
    input_values = plate_input_values(artifact, book, moved)
    needles = book["needles"]
    closure = needle_closure_union(artifact, [n["variable"] for n in needles])
    result = solve_plate(artifact, closure, input_values)

    record = {
        "plate_id": plate["plate_id"],
        "moved_groups": list(moved),
        "closure": closure,
        "status": result["status"],
        "needles": {},
    }
    readable = {}
    if result["status"] == "inconsistent":
        for needle in needles:
            record["needles"][needle["variable"]] = {
                "status": "unverifiable_plate",
                "reason": "盘面联合矛盾，整盘不可检定",
            }
        record["contradictions"] = result["contradictions"]
        return record, readable

    for needle in needles:
        vname = needle["variable"]
        info = result["classified"][vname]
        if info[0] == "fixed":
            value = info[1]
            record["needles"][vname] = {"status": "determined", "value": qstr(value)}
            readable[vname] = value
        else:
            _, const, parts = info
            record["needles"][vname] = {
                "status": "non_unique",
                "reason": "该针在本盘面不唯一，禁用其作读数",
                "expression": {
                    "constant": qstr(const),
                    "param_terms": [
                        {"of_variable": pv, "coef": qstr(pc)}
                        for pv, pc in sorted(parts.items())
                    ],
                },
            }
    return record, readable
