"""把代拨网络翻译成 budget_solver 的线性模型负载。

代拨合同按**每个出资户各自的资金流**履约（同一转出比例对途经该户的每个来源
分别生效），因此逐户模型与合计模型都显式带出来源维：

* 真实输入格 ``in_<funder>_<account>``：具名出资户对该户的直接注入；
* 来源总流入格 ``T_<funder>_<account>`` = 该来源对本站的直接注入
  + Σ 其他账户按约定比例转来的、属于该来源的流入；
* 来源留用格 ``r_<funder>_<account>`` = 该来源总流入 × 本站留用比例
  （留用比例 = 1 − 本站各转出比例之和，余数本站留用）。

资金绕环多圈时，线性方程使每个来源在每个站只留用一次；合计模型是逐户模型
的逐格加总（总流入 ``T_<account>``、合计留用 ``R_<account>``），
“逐户解算之和 = 合计解算”由 :mod:`allocation.verifier` 独立复算。
"""

from fractions import Fraction

from ..rationals import qstr

MODEL_PREFIX = "ALO"


def model_id_total(session_id):
    return f"{MODEL_PREFIX}_T_{session_id}"


def model_id_source(session_id, funder):
    return f"{MODEL_PREFIX}_S_{session_id}_{funder}"


def cell_inject(funder, account):
    return f"in_{funder}_{account}"


def cell_source_inflow(funder, account):
    return f"T_{funder}_{account}"


def cell_total_inflow(account):
    return f"T_{account}"


def cell_retained_source(funder, account):
    return f"r_{funder}_{account}"


def cell_retained_total(account):
    return f"R_{account}"


def effective_ratio(transfer, funder):
    """某转拨边对某来源资金的实际比例（ratios 覆盖优先，否则用缺省 ratio）。"""
    return Fraction(transfer["ratios"].get(funder, transfer["ratio"]))


def retention_ratio(session, account, funder=None):
    """本站留用比例 = 1 − 各转出比例之和（非负，校验已保证）。

    给定 ``funder`` 时按该来源的有效比例计算；否则给缺省比例口径（仅图分析用）。
    """
    if funder is None:
        used = sum((Fraction(t["ratio"]) for t in session["transfers"]
                    if t["from"] == account), Fraction(0))
    else:
        used = sum((effective_ratio(t, funder) for t in session["transfers"]
                    if t["from"] == account), Fraction(0))
    return Fraction(1) - used


def _source_inflow_terms(session, funder, account):
    """某来源在某户的总流入：直接注入 + 上游按来源比例转来的同来源流入。"""
    terms = [{"coef": "1", "cell_id": cell_inject(funder, account)}]
    for transfer in session["transfers"]:
        if transfer["to"] != account:
            continue
        terms.append({
            "coef": qstr(effective_ratio(transfer, funder)),
            "cell_id": cell_source_inflow(funder, transfer["from"]),
        })
    return terms


def build_source_model(session, funder):
    """逐出资户模型：只有该来源的真实注入输入格，得到其资金在各受款户的留用。"""
    accounts = session["accounts"]
    injections = {(i["funder"], i["account"]): Fraction(i["amount"])
                  for i in session["injections"]}
    cells = {}
    for account in accounts:
        cells[cell_inject(funder, account)] = {
            "kind": "input",
            "value": qstr(injections.get((funder, account), Fraction(0)))}
    for account in accounts:
        cells[cell_source_inflow(funder, account)] = {
            "kind": "formula", "constant": "0",
            "terms": _source_inflow_terms(session, funder, account)}
        cells[cell_retained_source(funder, account)] = {
            "kind": "formula", "constant": "0",
            "terms": [{"coef": qstr(retention_ratio(session, account, funder)),
                       "cell_id": cell_source_inflow(funder, account)}]}
    return {
        "model_id": model_id_source(session["session_id"], funder),
        "name": f"代拨来源流-{funder}-{session['name']}",
        "description": f"出资户 {funder} 的可回查来源账：注入→转拨→最终留用",
        "cells": cells,
        "outputs": [cell_source_inflow(funder, a) for a in accounts]
                   + [cell_retained_source(funder, a) for a in accounts],
    }


def build_total_model(session):
    """合计模型：全部来源的输入格 + 逐来源流入/留用格 + 合计 T_/R_ 格。"""
    accounts = session["accounts"]
    funders = session["funders"]
    injections = {(i["funder"], i["account"]): Fraction(i["amount"])
                  for i in session["injections"]}
    cells = {}
    for funder in funders:
        for account in accounts:
            cells[cell_inject(funder, account)] = {
                "kind": "input",
                "value": qstr(injections.get((funder, account), Fraction(0)))}
    for funder in funders:
        for account in accounts:
            cells[cell_source_inflow(funder, account)] = {
                "kind": "formula", "constant": "0",
                "terms": _source_inflow_terms(session, funder, account)}
            cells[cell_retained_source(funder, account)] = {
                "kind": "formula", "constant": "0",
                "terms": [{"coef": qstr(retention_ratio(session, account, funder)),
                           "cell_id": cell_source_inflow(funder, account)}]}
    for account in accounts:
        cells[cell_total_inflow(account)] = {
            "kind": "formula", "constant": "0",
            "terms": [{"coef": "1", "cell_id": cell_source_inflow(f, account)}
                      for f in funders]}
        cells[cell_retained_total(account)] = {
            "kind": "formula", "constant": "0",
            "terms": [{"coef": "1", "cell_id": cell_retained_source(f, account)}
                      for f in funders]}
    return {
        "model_id": model_id_total(session["session_id"]),
        "name": f"代拨合计流-{session['name']}",
        "description": "全部出资户注入合计的逐来源流入/留用与合计等式核",
        "cells": cells,
        "outputs": [cell_total_inflow(a) for a in accounts]
                   + [cell_retained_total(a) for a in accounts],
    }


def transfer_adjacency(session):
    """转拨图邻接表（仅保留正比例边），供循环 SCC/受困识别。"""
    adjacency = {a: [] for a in session["accounts"]}
    for transfer in session["transfers"]:
        if Fraction(transfer["ratio"]) > 0:
            adjacency[transfer["from"]].append(transfer["to"])
    return {a: sorted(set(v)) for a, v in adjacency.items()}


def injection_accounts(session, funder):
    return sorted({i["account"] for i in session["injections"] if i["funder"] == funder})
