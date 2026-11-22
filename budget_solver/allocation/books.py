"""拨款账簿与正式拨款单。

账簿从同一开场账（去向册 + 点名册）建账，维护两类余额：

* 原资助：每个原出资户留在每个（旧用途）受款户的余额，只可在该户对应获批案
  的点名同意金额以内转用，且转用后该户旧用途余额不得低于登记底线；
* 自有款：各基金会自有款账户的真实余额，多案共用，只可贴入允许范围内的项目。

正式拨款单用稳定单号绑定去向册、点名册和实际分款明细：同号同内容幂等复读，
同号异文退回；dry-run 测算不占款、不占单号，另起单也不能再动已支余额。
"""

from fractions import Fraction

from ..errors import AllocationInfeasibleError, NotFoundError, OrderRejectedError
from ..rationals import qstr
from .store import ArtifactStore

BOOK_PREFIX = "book"
ORDER_PREFIX = "order"


# ============================================================ 精确最大流（容量为 Fraction）
def max_flow(capacity_edges, source, sink):
    """Edmonds-Karp，容量与流量均为 Fraction；显式边表 + 反向指针。

    平行边各自独立计量，零容量边也不影响其他边的流量回收。
    返回 (最大流量, 每条输入边的流量)，流量顺序与 ``capacity_edges`` 对齐。
    """
    graph = {}  # node -> [edge_index...]
    edges = []  # [u, v, residual_cap]；反向边 i^1 成对存放

    def add_edge(u, v, cap):
        index = len(edges)
        edges.append([u, v, Fraction(cap)])
        edges.append([v, u, Fraction(0)])
        graph.setdefault(u, []).append(index)
        graph.setdefault(v, []).append(index + 1)
        return index

    forward_index = [add_edge(u, v, cap) for u, v, cap in capacity_edges]

    def augment():
        parent_edge = {}
        queue = [source]
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for ei in graph.get(node, ()):
                _, nxt, residual = edges[ei]
                if nxt in parent_edge or nxt == source or residual <= 0:
                    continue
                parent_edge[nxt] = ei
                if nxt == sink:
                    return parent_edge
                queue.append(nxt)
        return None

    flow = Fraction(0)
    while True:
        parent_edge = augment()
        if parent_edge is None:
            break
        bottleneck = None
        node = sink
        while node != source:
            ei = parent_edge[node]
            cap = edges[ei][2]
            bottleneck = cap if bottleneck is None else min(bottleneck, cap)
            node = edges[ei][0]
        node = sink
        while node != source:
            ei = parent_edge[node]
            edges[ei][2] -= bottleneck
            edges[ei ^ 1][2] += bottleneck
            node = edges[ei][0]
        flow += bottleneck
    edge_flows = [edges[i ^ 1][2] for i in forward_index]
    return flow, edge_flows


class BookService:
    def __init__(self, store_root):
        # 账簿随拨款单追加而变化，不声明固定业务指纹；其开场账事实由
        # opening_fingerprint 单独钉住，重写只更新通用 content_hash
        self.book_store = ArtifactStore(store_root, "拨款账簿", BOOK_PREFIX)
        # 拨款单目录挂在账簿目录下，由 book 管理生命周期
        self.order_store = ArtifactStore(
            f"{store_root.rstrip('/')}-orders", "正式拨款单", ORDER_PREFIX,
            fingerprint_field="order_fingerprint")

    # --------------------------------------------------------------- 建账
    def open_book(self, ledger, rollcall, label=None):
        session = ledger["session"]
        retained = {f: {a: Fraction(v) for a, v in row.items()}
                    for f, row in ledger["retained_by_source"].items()}
        opening = {
            "artifact_kind": "allocation_book",
            "schema_version": 1,
            "session_id": session["session_id"],
            "label": label,
            "ledger_id": ledger.get("ledger_id"),
            "ledger_fingerprint": ledger["ledger_fingerprint"],
            "rollcall_id": rollcall.get("roll_id"),
            "rollcall_fingerprint": rollcall["rollcall_fingerprint"],
            "opening_original": {
                f: {a: qstr(v) for a, v in row.items()}
                for f, row in retained.items()
            },
            "opening_own_funds": {f["fund_id"]: f["balance"] for f in session["own_funds"]},
            "own_fund_scope": {f["fund_id"]: list(f["case_scope"])
                               for f in session["own_funds"]},
            "orders": [],
            "paid_cases": [],
        }
        book_id = self.book_store.next_id()
        return self.book_store.save(book_id, opening)

    def get_book(self, book_id):
        return self.book_store.load(book_id)

    def list_books(self):
        return self.book_store.list_ids()

    # ------------------------------------------------------- 当前余额（重开可核）
    def current_state(self, book):
        """从开场账与已支拨款单重算全部余额，分开查每个原出资户/自有款余数。"""
        ledger_session_original = book["opening_original"]
        original = {f: {a: Fraction(v) for a, v in row.items()}
                    for f, row in ledger_session_original.items()}
        own = {k: Fraction(v) for k, v in book["opening_own_funds"].items()}
        per_case_redirect = {case: {} for case in book["paid_cases"]}
        per_case_own = {case: {} for case in book["paid_cases"]}
        for order_id in book["orders"]:
            order = self.order_store.load(order_id)
            for item in order["items"]:
                case_id = item["case_id"]
                for part in item["redirects"]:
                    original[part["funder"]][item["account"]] -= Fraction(part["amount"])
                    per_case_redirect.setdefault(case_id, {})
                    per_case_redirect[case_id][part["funder"]] = (
                        per_case_redirect[case_id].get(part["funder"], Fraction(0))
                        + Fraction(part["amount"]))
                for part in item["own_fund_inputs"]:
                    own[part["fund_id"]] -= Fraction(part["amount"])
                    per_case_own.setdefault(case_id, {})
                    per_case_own[case_id][part["fund_id"]] = (
                        per_case_own[case_id].get(part["fund_id"], Fraction(0))
                        + Fraction(part["amount"]))
        return {
            "original_balances": {f: {a: qstr(v) for a, v in row.items()}
                                  for f, row in original.items()},
            "own_fund_balances": {k: qstr(v) for k, v in own.items()},
            "redirect_by_case": {
                case: {f: qstr(v) for f, v in row.items()}
                for case, row in per_case_redirect.items()
            },
            "own_input_by_case": {
                case: {k: qstr(v) for k, v in row.items()}
                for case, row in per_case_own.items()
            },
        }

    # ------------------------------------------------------- 计划校验（不占款）
    def _validate_plan(self, book, rollcall, items, state):
        session_cases = {c["case_id"]: c for c in rollcall["cases"]}
        errors = []
        seen_cases = set()
        original = {f: {a: Fraction(v) for a, v in row.items()}
                    for f, row in state["original_balances"].items()}
        own = {k: Fraction(v) for k, v in state["own_fund_balances"].items()}
        # 本单内的临时扣减，保证一单多案/多笔之间不超额
        for item in items:
            case_id = item.get("case_id")
            case = session_cases.get(case_id)
            if case is None:
                errors.append(f"案号 {case_id!r} 不在点名册中")
                continue
            if not case["approved"]:
                errors.append(f"案 {case_id} 未获批准，不得拨款")
            if case_id in book["paid_cases"] or case_id in seen_cases:
                errors.append(f"案 {case_id} 已在本账簿支过或本单重复（不能再动已支余额）")
            seen_cases.add(case_id)
            account = case["account"]
            if item.get("account") != account:
                errors.append(f"案 {case_id} 的受款户应为 {account}")
            redirect_total = own_total = Fraction(0)
            for part in item.get("redirects", []):
                funder, amount = part.get("funder"), Fraction(part.get("amount", 0))
                if funder not in original:
                    errors.append(f"案 {case_id} 转用了点名册外出资户 {funder}")
                    continue
                # 只可动用点名册内该出资户对此案的同意金额
                rec = next((r for r in case["records"]
                            if r["funder"] == funder and r["vote"] == "yes"), None)
                yes_cap = Fraction(rec["signed"]) if rec else Fraction(0)
                if amount > yes_cap:
                    errors.append(
                        f"案 {case_id} 转用 {funder} {qstr(amount)} 超过其同意金额 "
                        f"{qstr(yes_cap)}（反对者/弃权部分不能划走）")
                available = original[funder].get(account, Fraction(0))
                if amount > available:
                    errors.append(
                        f"案 {case_id} 转用 {funder} 超过其在 {account} 的可用余额 "
                        f"{qstr(available)}")
                original[funder][account] = available - amount
                redirect_total += amount
            for part in item.get("own_fund_inputs", []):
                fund_id, amount = part.get("fund_id"), Fraction(part.get("amount", 0))
                if fund_id not in own:
                    errors.append(f"案 {case_id} 使用了不存在的自有款账户 {fund_id}")
                    continue
                if case_id not in book["own_fund_scope"].get(fund_id, []):
                    errors.append(f"自有款 {fund_id} 不允许贴入项目 {case_id}")
                if amount > own[fund_id]:
                    errors.append(
                        f"案 {case_id} 贴入 {fund_id} 超过其余额 {qstr(own[fund_id])}")
                own[fund_id] -= amount
                own_total += amount
            if redirect_total + own_total != Fraction(case["need"]):
                errors.append(
                    f"案 {case_id} 到款 {qstr(redirect_total + own_total)} "
                    f"不等于新用途所需 {case['need']}")
            reserve = Fraction(case["reserve"])
            left = sum((original[f].get(account, Fraction(0))
                        for f in original), Fraction(0))
            if left < reserve:
                errors.append(
                    f"案 {case_id} 拨付后 {account} 旧用途余额 {qstr(left)} "
                    f"低于底线 {qstr(reserve)}")
        if errors:
            raise OrderRejectedError("拨款计划被拒", sorted(set(errors)))
        return seen_cases

    # ------------------------------------------------- 可行性测算（dry-run）
    def check_feasible(self, book, rollcall):
        """对全部获批未支案做精确可行性测算，返回自动方案或指出不能同时兑现的批次。"""
        state = self.current_state(book)
        feasible, plan, infeasible = self._auto_plan(book, rollcall, state)
        return {
            "feasible": feasible,
            "plan": plan,
            "infeasible_cases": infeasible,
            "projected_state": self._project(state, plan) if feasible else None,
        }

    def _auto_plan(self, book, rollcall, state):
        """优先用足获签原款（最小化自有款占用），再用最大流核自有款能否补齐。"""
        original = {f: {a: Fraction(v) for a, v in row.items()}
                    for f, row in state["original_balances"].items()}
        own = {k: Fraction(v) for k, v in state["own_fund_balances"].items()}
        approved = [c for c in rollcall["cases"]
                    if c["approved"] and c["case_id"] not in book["paid_cases"]]
        plan = []
        own_demand = {c["case_id"]: Fraction(0) for c in approved}
        infeasible = []

        for case in approved:
            account = case["account"]
            need = Fraction(case["need"])
            reserve = Fraction(case["reserve"])
            total_left = sum((original[f].get(account, Fraction(0))
                              for f in original), Fraction(0))
            # 不破底线的原款上限；若开场余额已低于底线（异常数据），钳为 0
            redirect_cap = max(Fraction(0), min(need, total_left - reserve))
            redirects = []
            used = Fraction(0)
            for rec in case["records"]:
                if rec["vote"] != "yes" or used >= redirect_cap:
                    continue
                funder = rec["funder"]
                take = min(Fraction(rec["signed"]),
                           original[funder].get(account, Fraction(0)),
                           redirect_cap - used)
                if take > 0:
                    redirects.append({"funder": funder, "amount": qstr(take)})
                    original[funder][account] -= take
                    used += take
            gap = need - used
            own_demand[case["case_id"]] = gap
            plan.append({
                "case_id": case["case_id"],
                "account": account,
                "redirects": redirects,
                "own_fund_inputs": [],  # 由最大流填充
            })
            if gap < 0:  # 理论不会发生，防御
                infeasible.append({"case_id": case["case_id"], "reason": "测算内部错误"})

        # 自有款运输网络：源 -> 自有款账户 -> 案 -> 汇
        edges = []
        fund_nodes, case_nodes = [], []
        for fund_id, balance in own.items():
            node = f"F::{fund_id}"
            fund_nodes.append((fund_id, node))
            edges.append(("SRC", node, balance))
        fund_case_edges = {}  # (fund_id, case_id) -> edges 下标
        for case in approved:
            node = f"C::{case['case_id']}"
            case_nodes.append((case["case_id"], node))
            edges.append((node, "SNK", own_demand[case["case_id"]]))
        for fund_id, fnode in fund_nodes:
            for case_id, cnode in case_nodes:
                if case_id in book["own_fund_scope"].get(fund_id, []):
                    fund_case_edges[(fund_id, case_id)] = len(edges)
                    edges.append((fnode, cnode, None))  # 容量受两端约束
        # None 容量用一个足够大的精确上界替代：总需求之和
        big = sum(own_demand.values(), Fraction(0)) or Fraction(1)
        edges = [(u, v, big if cap is None else cap) for u, v, cap in edges]
        total_demand = sum(own_demand.values(), Fraction(0))
        flow, edge_flows = max_flow(edges, "SRC", "SNK")
        if flow < total_demand:
            short = []
            for case_id, cnode in case_nodes:
                sink_edge_index = next(
                    i for i, (u, v, _) in enumerate(edges)
                    if v == "SNK" and u == cnode)
                # 案节点收到的自有款 = 案->汇 边需求容量减去其剩余残量
                received = own_demand[case_id] - edge_flows[sink_edge_index]
                if received < own_demand[case_id]:
                    short.append({
                        "case_id": case_id,
                        "reason": "自有款余额或可贴范围不足",
                        "shortfall": qstr(own_demand[case_id] - received),
                    })
            infeasible.extend(short)
            return False, None, infeasible

        for (fund_id, case_id), edge_index in sorted(fund_case_edges.items()):
            amount = edge_flows[edge_index]
            if amount > 0:
                item = next(p for p in plan if p["case_id"] == case_id)
                item["own_fund_inputs"].append(
                    {"fund_id": fund_id, "amount": qstr(amount)})
        return True, plan, []

    @staticmethod
    def _project(state, plan):
        original = {f: dict(row) for f, row in
                    ((f, {a: Fraction(v) for a, v in row.items()})
                     for f, row in state["original_balances"].items())}
        own = {k: Fraction(v) for k, v in state["own_fund_balances"].items()}
        for item in plan:
            account = item["account"]
            for part in item["redirects"]:
                original[part["funder"]][account] -= Fraction(part["amount"])
            for part in item["own_fund_inputs"]:
                own[part["fund_id"]] -= Fraction(part["amount"])
        return {
            "original_balances": {f: {a: qstr(v) for a, v in row.items()}
                                  for f, row in original.items()},
            "own_fund_balances": {k: qstr(v) for k, v in own.items()},
        }

    # --------------------------------------------------------------- 下账
    def issue_order(self, book, rollcall, items, order_id=None, dry_run=False):
        # 同号同内容幂等复读：只有该单号确实已发布过才短路，绝不二次下账；
        # 单号尚不存在时按新单正常校验签发
        if order_id is not None and not dry_run:
            try:
                existing = self.order_store.load(order_id)
            except NotFoundError:
                existing = None
            if existing is not None:
                if existing["items"] != items \
                        or existing["book_id"] != book.get("book_id"):
                    raise OrderRejectedError(
                        f"稳定单号 {order_id} 已绑定不同内容（同号异文退回）",
                        [order_id])
                return existing
        state = self.current_state(book)
        seen_cases = self._validate_plan(book, rollcall, items, state)
        projected = self._project(state, items)
        if dry_run:
            return {
                "dry_run": True,
                "would_pay_cases": sorted(seen_cases),
                "projected_state": projected,
                "note": "测算不占款、不占单号、不动已支余额",
            }
        order_id = order_id or self.order_store.next_id()
        order = {
            "artifact_kind": "allocation_order",
            "schema_version": 1,
            "order_id": order_id,
            "book_id": book.get("book_id"),
            "session_id": book["session_id"],
            "ledger_fingerprint": book["ledger_fingerprint"],
            "rollcall_fingerprint": book["rollcall_fingerprint"],
            "items": items,
            "resulting_state": projected,
        }
        saved = self.order_store.save(order_id, order)
        # 回写账簿：账簿是随拨款单追加的可变产物，走专用 rewrite 重算哈希
        book["orders"].append(order_id)
        book["paid_cases"] = sorted(set(book["paid_cases"]) | seen_cases)
        self.book_store.rewrite(book["book_id"], book)
        return saved

    def get_order(self, order_id):
        return self.order_store.load(order_id)

    def list_orders(self):
        return self.order_store.list_ids()

    def assert_all_approved_funded(self, book, rollcall):
        """批签收口：所有获批案必须已足额到款，否则指出未兑现批次。"""
        missing = [c["case_id"] for c in rollcall["cases"]
                   if c["approved"] and c["case_id"] not in book["paid_cases"]]
        if missing:
            raise AllocationInfeasibleError(
                "以下获批项目未在本账簿兑现，不能同时足额到款", missing)
        return {"ok": True, "funded_cases": list(book["paid_cases"])}
