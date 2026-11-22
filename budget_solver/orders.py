"""总馆收发处：询证订单、回签状态机与签账。

一份订单绑定**一张真正的票**（票号 + 发行版哈希）、纸票、容错与待记合计算式。
问项在看答案以前一次确定：:meth:`OrderDesk.dispatch` 之后套餐不可更改，同一馆的
问项一起递送、收费按项分别计，只在首次发出时计费一次。

回签 :meth:`OrderDesk.receive_reply` 必须带票号、发行版与问项号，并与实际发出的
同项匹配；答数碰巧相容也不能接用另一张票的回签（票号/发行版不符直接拒收）。
同项重复回答按幂等处理（不二次寄送收费），与首答冲突则登记矛盾、订单不签账。
全部回答到齐后再与纸票做**联合相容性**检查：

* 联合相容 → 合计必唯一（套餐判别已保证），给出可核对的完整复原并签账；
* 联合矛盾 → 订单置 ``contradicted``，附 λ 冲突依据，不能入账；
* 只回了部分 → 保留 ``awaiting_replies`` 状态，不提前签账。

复读已发询证（再次 dispatch）或记账回执（再收回答）不产生二次收费或重复入账。
"""

import os
from fractions import Fraction

from .errors import NotFoundError, ValidationError
from .inquiry import InquiryCatalog
from .linspace import solve_affine
from .rationals import qstr
from .recovery import (
    feasible_recoveries,
    paper_mismatch_count,
    parse_form,
    residual_of_face,
)
from .util import (
    content_hash,
    ensure_dir,
    read_json,
    utc_now_iso,
    write_json_atomic,
)


def _fraction_map(mp):
    return {k: Fraction(v) for k, v in mp.items()}


def _total_at(form, assignment):
    total = form["constant"]
    for var, coef in form["terms"].items():
        total += coef * assignment.get(var, Fraction(0))
    return total


def _form_slope(form, null_vector):
    return sum(coef * null_vector.get(var, 0)
               for var, coef in form["terms"].items())


def intersect_replies(solution, reply_eqs):
    """把回签等式与一个可行复原子空间（基点 + 零空间）求交。

    子空间写作 ``x = point + N·p``；回签 ``c·x = b`` 代入为关于自由参数 p 的
    仿射系统。相容时把 p 的基点/零空间映回票面变量，返回票面变量上的相交子空间
    （同 :func:`solve_affine` 形状）；矛盾返回 None。
    """
    point = solution["point"]
    nullspace = solution["nullspace"]
    face_vars = solution["variables"]
    param_vars = [f"p{j}" for j in range(len(nullspace))]
    equations = []
    for eq in reply_eqs:
        # c·(point + N·p) = b  =>  c·N·p = b - c·point
        rhs = Fraction(eq["rhs"]) - _total_at(
            {"constant": 0, "terms": eq["coeffs"]}, point)
        coeffs = {}
        for j, vec in enumerate(nullspace):
            slope = sum(Fraction(coef) * vec.get(var, 0)
                        for var, coef in eq["coeffs"].items())
            if slope != 0:
                coeffs[param_vars[j]] = slope
        equations.append(
            {"coeffs": coeffs, "rhs": rhs, "origin": eq.get("origin")})
    solved = solve_affine(equations, param_vars)
    if solved["status"] == "inconsistent":
        return None
    new_point = dict(point)
    for var in face_vars:
        for j, vec in enumerate(nullspace):
            new_point[var] += vec.get(var, 0) * solved["point"][param_vars[j]]
    new_null = []
    for pvec in solved["nullspace"]:
        mapped = {}
        for var in face_vars:
            val = sum(vec.get(var, 0) * pvec.get(f"p{j}", 0)
                      for j, vec in enumerate(nullspace))
            if val != 0:
                mapped[var] = val
        new_null.append(mapped)
    return {
        "status": "consistent",
        "variables": list(face_vars),
        "point": new_point,
        "nullspace": new_null,
        "free_vars": [],
        "pivot_vars": list(face_vars),
    }


class OrderDesk:
    """询证订单的持久化门面；构造即重开。"""

    def __init__(self, order_dir, ticket_loader):
        self.order_dir = os.path.abspath(order_dir)
        ensure_dir(self.order_dir)
        self._seq_path = os.path.join(self.order_dir, "seq.json")
        self._ticket_loader = ticket_loader  # callable(ticket_id) -> ticket dict

    # ------------------------------------------------------------- 基础持久化
    def _next_order_id(self):
        seq = read_json(self._seq_path) if os.path.exists(self._seq_path) else {"next": 1}
        order_id = f"ord{seq['next']}"
        seq["next"] += 1
        write_json_atomic(self._seq_path, seq)
        return order_id

    def _path(self, order_id):
        return os.path.join(self.order_dir, f"{order_id}.json")

    def get_order(self, order_id):
        path = self._path(order_id)
        if not os.path.exists(path):
            raise NotFoundError(f"订单 {order_id} 不存在")
        return read_json(path)

    def list_orders(self):
        if not os.path.isdir(self.order_dir):
            return []
        return sorted(
            f[:-5] for f in os.listdir(self.order_dir)
            if f.startswith("ord") and f.endswith(".json")
        )

    def _save(self, order):
        write_json_atomic(self._path(order["order_id"]), order)

    def _load_ticket(self, order):
        return self._ticket_loader(order["ticket_id"])

    # --------------------------------------------------------------- 开单
    def open_order(self, ticket, paper_raw, total_form_raw, k, analysis,
                   catalog_items, budget, max_branches):
        """据辨错结论开单：合计已固定直接记账，否则登记待询证上下文。"""
        paper = {
            v: (f if isinstance(f, Fraction) else Fraction(f))
            for v, f in (paper_raw or {}).items()
        }
        total_form = parse_form(total_form_raw)
        # 目录在开单时即校验并固化进订单
        catalog = InquiryCatalog(ticket, catalog_items)
        order_id = self._next_order_id()
        order = {
            "order_kind": "inquiry_order",
            "order_id": order_id,
            "created_at": utc_now_iso(),
            "ticket_id": ticket["ticket_id"],
            "issue_hash": ticket["issue_hash"],
            "artifact_hash": ticket["artifact_hash"],
            "paper": {v: qstr(f) for v, f in sorted(paper.items())},
            "tolerance_k": k,
            "total_form": {
                "constant": qstr(total_form["constant"]),
                "terms": {v: qstr(c) for v, c in sorted(total_form["terms"].items())},
            },
            "budget": qstr(Fraction(budget)),
            "max_branches": max_branches,
            "catalog": catalog.to_json(),
            "analysis_status": analysis["status"],
            "plan": None,
            "replies": [],
            "events": [],
        }
        if analysis["status"] == "fixed":
            order["status"] = "booked_direct"
            order["booking"] = self._booking_record(
                ticket, paper, k, total_form, extra_eqs=[],
                replies=[], plan=None,
            )
            order["events"].append(
                {"at": utc_now_iso(), "event": "book_direct",
                 "total": order["booking"]["booked_total"]})
        elif analysis["status"] == "ambiguous":
            order["status"] = "draft"
        else:
            raise ValidationError(
                f"纸票已被退回（{analysis['status']}），不能开询证订单",
                [order_id],
            )
        self._save(order)
        return order

    # --------------------------------------------------------------- 发单
    def dispatch(self, order_id, chosen_item_ids):
        """看答案以前一次确定套餐；重复发单被拒，不二次计费。"""
        order = self.get_order(order_id)
        if order["status"] != "draft":
            raise ValidationError(
                f"订单 {order_id} 状态为 {order['status']}，不能再发询证",
                [order_id],
            )
        ticket = self._load_ticket(order)
        catalog = InquiryCatalog(ticket, order["catalog"])
        chosen_ids = list(dict.fromkeys(chosen_item_ids))
        valid = {it["item_id"] for it in catalog.items}
        unknown = [i for i in chosen_ids if i not in valid]
        if unknown:
            raise ValidationError(f"问项不在本月目录：{unknown}", unknown)
        if not chosen_ids:
            raise ValidationError("套餐至少含一个问项", ["item_ids"])
        budget = Fraction(order["budget"])
        chosen = [catalog.get(i) for i in chosen_ids]
        fee = sum((it["fee"] for it in chosen), Fraction(0))
        branches = sorted({it["branch"] for it in chosen})
        if fee > budget:
            raise ValidationError(
                f"套餐费用 {qstr(fee)} 超过本单经费 {qstr(budget)}",
                ["fee", qstr(fee)],
            )
        if len(branches) > order["max_branches"]:
            raise ValidationError(
                f"套餐需跑 {len(branches)} 馆，超过上限 {order['max_branches']}",
                ["branches", str(len(branches))],
            )
        order["plan"] = {
            "item_ids": chosen_ids,
            "branches": branches,
            "total_fee": qstr(fee),
            "fee_per_item": {it["item_id"]: qstr(it["fee"]) for it in chosen},
            "dispatched_at": utc_now_iso(),
        }
        order["status"] = "awaiting_replies"
        order["events"].append(
            {"at": utc_now_iso(), "event": "dispatch",
             "item_ids": chosen_ids, "branches": branches, "fee": qstr(fee)})
        self._save(order)
        return order

    # --------------------------------------------------------------- 回签
    def receive_reply(self, order_id, ticket_id, issue_hash, item_id, value):
        """收一份回签；票号/发行版/问项号三重核对，幂等且不二次计费。"""
        order = self.get_order(order_id)
        if ticket_id != order["ticket_id"]:
            raise ValidationError(
                f"回签票号 {ticket_id} 与订单绑定的 {order['ticket_id']} 不符，拒收",
                [ticket_id],
            )
        if issue_hash != order["issue_hash"]:
            raise ValidationError(
                "回签发行版与本单票版不一致，即便答数相容也不能接用",
                [issue_hash],
            )
        if order["plan"] is None or item_id not in order["plan"]["item_ids"]:
            raise ValidationError(
                f"问项号 {item_id} 不在本单实际发出的套餐中，拒收", [item_id]
            )
        answer = value if isinstance(value, Fraction) else Fraction(value)
        existing = next((r for r in order["replies"] if r["item_id"] == item_id), None)
        if existing is not None:
            if Fraction(existing["value"]) == answer:
                order["events"].append(
                    {"at": utc_now_iso(), "event": "duplicate_reply_ignored",
                     "item_id": item_id})
                self._save(order)
                return order  # 幂等：不重复计费、不重复入账
            order["events"].append(
                {"at": utc_now_iso(), "event": "conflicting_reply",
                 "item_id": item_id, "first": existing["value"],
                 "repeat": qstr(answer)})
            order["status"] = "contradicted"
            order["contradiction"] = {
                "reason": "same_item_conflicting_answers",
                "item_id": item_id,
                "first_value": existing["value"],
                "repeat_value": qstr(answer),
            }
            self._save(order)
            return order
        order["replies"].append(
            {
                "item_id": item_id,
                "value": qstr(answer),
                "received_at": utc_now_iso(),
                "ticket_id": ticket_id,
                "issue_hash": issue_hash,
            }
        )
        order["events"].append(
            {"at": utc_now_iso(), "event": "reply_received",
             "item_id": item_id, "value": qstr(answer)})
        if len(order["replies"]) == len(order["plan"]["item_ids"]):
            self._finalize(order)
        self._save(order)
        return order

    # --------------------------------------------------------- 到齐后联合核账
    def _reply_equations(self, ticket, order):
        catalog = InquiryCatalog(ticket, order["catalog"])
        equations = []
        for reply in order["replies"]:
            item = catalog.get(reply["item_id"])
            equations.append(
                {
                    "coeffs": dict(item["form"]["terms"]),
                    "rhs": Fraction(reply["value"]) - item["form"]["constant"],
                    "origin": ("reply", reply["item_id"]),
                }
            )
        return equations

    def _finalize(self, order):
        ticket = self._load_ticket(order)
        paper = _fraction_map(order["paper"])
        total_form = parse_form(order["total_form"])
        extra_eqs = self._reply_equations(ticket, order)
        booking = self._booking_record(
            ticket, paper, order["tolerance_k"], total_form,
            extra_eqs=extra_eqs, replies=order["replies"], plan=order["plan"],
        )
        if booking is None:
            order["status"] = "contradicted"
            order["contradiction"] = {
                "reason": "replies_jointly_incompatible_with_paper",
                "detail": self._joint_certificate(
                    ticket, paper, order["tolerance_k"], extra_eqs),
            }
            order["events"].append(
                {"at": utc_now_iso(), "event": "contradicted",
                 "detail": order["contradiction"]["reason"]})
            return
        order["status"] = "booked"
        order["booking"] = booking
        order["events"].append(
            {"at": utc_now_iso(), "event": "book",
             "total": booking["booked_total"]})

    def _booking_record(self, ticket, paper, k, total_form, extra_eqs, replies, plan):
        """联合相容则产出唯一合计与可核对复原；矛盾返回 None。

        对每个 k 内容错可行的复原子空间，把回签等式与该子空间求交；相交后合计
        必须在所有候选子空间上取同一个唯一值。空回签（直接记账）时合计唯一性
        已由辨错阶段保证。
        """
        face_vars = ticket["face_variables"]
        recoveries = feasible_recoveries(ticket, paper, k)
        candidates = []
        for item in recoveries:
            joint = intersect_replies(item["solution"], extra_eqs)
            if joint is None:
                continue
            point_total = _total_at(total_form, joint["point"])
            for vec in joint["nullspace"]:
                if _form_slope(total_form, vec) != 0:
                    return None  # 套餐判别本应杜绝；防御性拦下不唯一合计
            candidates.append((item, joint, point_total))
        if not candidates:
            return None
        totals = {total for _, _, total in candidates}
        if len(totals) != 1:
            return None
        total = next(iter(totals))
        item, joint, _ = candidates[0]
        assignment = joint["point"]
        record = {
            "booked_total": qstr(total),
            "error_cells": item["error_cells"],
            "face_assignment": {v: qstr(assignment[v]) for v in face_vars},
            "free_directions": [
                {v: qstr(f) for v, f in vec.items() if f != 0}
                for vec in joint["nullspace"]
            ],
            "mismatch_count": paper_mismatch_count(paper, assignment),
            "face_residuals": residual_of_face(ticket, assignment),
            "reply_checks": [
                {
                    "item_id": reply["item_id"],
                    "ticket_id": reply["ticket_id"],
                    "issue_hash": reply["issue_hash"],
                    "value": reply["value"],
                    "matches_issued": True,
                }
                for reply in replies
            ],
            "plan_item_ids": plan["item_ids"] if plan else [],
            "plan_fee": plan["total_fee"] if plan else "0",
        }
        record["booking_hash"] = content_hash(record)
        return record

    def _joint_certificate(self, ticket, paper, k, extra_eqs):
        """联合矛盾依据：先看票面等式与回签是否自身矛盾（给 λ 证书）；否则逐
        子空间说明回签与哪个容错复原不兼容。"""
        face_vars = ticket["face_variables"]
        equations = [
            {"coeffs": {v: Fraction(c) for v, c in eq["coeffs"].items()},
             "rhs": Fraction(eq["rhs"]), "origin": ("face_eq", eq["eq_id"])}
            for eq in ticket["face_equations"]
        ]
        equations.extend(extra_eqs)
        solved = solve_affine(equations, face_vars)
        if solved["status"] == "inconsistent":
            return {
                "reason": "face_and_replies_inconsistent",
                "certificate": [
                    {"origin": list(token), "lambda": qstr(lam)}
                    for token, lam in solved["certificate"]
                ],
                "residual_rhs": qstr(solved["residual_rhs"]),
            }
        recoveries = feasible_recoveries(ticket, paper, k)
        detail = []
        for item in recoveries:
            if intersect_replies(item["solution"], extra_eqs) is None:
                detail.append({"error_cells": item["error_cells"],
                               "reason": "replies_incompatible_with_subspace"})
        return {"reason": "no_recovery_within_tolerance_matches_replies",
                "subspace_failures": detail}
