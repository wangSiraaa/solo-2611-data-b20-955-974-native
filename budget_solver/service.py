"""应用门面：把目录、编译器、解算器、验证器组装成可重开的数据服务。

外部客户的三个独立面：
  * 目录面 get/list/snapshot —— 不依赖编译与解算即可查询版本；
  * 编译面 compile / load_artifact —— 产物自包含、可独立检查引用与方程；
  * 解算面 solve / query / explain —— 真实消费编译产物与目录快照。
"""

import os

from .catalog import Catalog
from .compiler import Compiler
from .errors import NotFoundError
from .inquiry import InquiryCatalog, recommend as recommend_plans
from .orders import OrderDesk
from .recovery import analyze_paper, feasible_recoveries
from .solver import SolverService
from .tickets import TicketOffice
from .ticket_verify import verify_booking as _verify_booking
from .ticket_verify import verify_ticket as _verify_ticket
from .verifier import verify_compile as _verify_compile
from .verifier import verify_solve as _verify_solve


class BudgetService:
    def __init__(self, data_root):
        self.data_root = os.path.abspath(data_root)
        self.catalog = Catalog(os.path.join(self.data_root, "catalog"))
        self.artifact_dir = os.path.join(self.data_root, "artifacts")
        self.report_dir = os.path.join(self.data_root, "reports")
        self.cache_dir = os.path.join(self.data_root, "cache")
        self.ticket_dir = os.path.join(self.data_root, "tickets")
        self.order_dir = os.path.join(self.data_root, "orders")
        self.compiler = Compiler(self.catalog, self.artifact_dir)
        self.solver = SolverService(
            self.catalog, self.artifact_dir, self.report_dir, self.cache_dir
        )
        self.ticket_office = TicketOffice(
            self.catalog, self.artifact_dir, self.ticket_dir
        )
        self.order_desk = OrderDesk(self.order_dir, self.ticket_office.get_ticket)

    # ------------------------------------------------------------- 目录面
    def put_model(self, payload):
        return self.catalog.put_model(payload)

    def list_models(self):
        return self.catalog.list_models()

    def get_model(self, model_id):
        return self.catalog.get_model_meta(model_id)

    def list_revisions(self, model_id):
        return self.catalog.list_revisions(model_id)

    def snapshot(self, model_id, label=None):
        return self.catalog.take_snapshot(model_id, label=label)

    def list_snapshots(self, model_id):
        return self.catalog.list_snapshots(model_id)

    def get_snapshot(self, snap_id):
        return self.catalog.get_snapshot(snap_id)

    def catalog_descriptor(self):
        return self.catalog.catalog_snapshot_descriptor()

    # ------------------------------------------------------------- 编译面
    def compile(self, root_snap_id, label=None):
        artifact, path = self.compiler.compile_snapshot(root_snap_id, label=label)
        return artifact, path

    def load_artifact(self, path_or_digest):
        if os.path.exists(path_or_digest):
            return Compiler.load_artifact(path_or_digest)
        digest = path_or_digest.split(":", 1)[-1]
        path = os.path.join(self.artifact_dir, f"{digest[:12]}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"编译产物 {path_or_digest} 找不到")
        return Compiler.load_artifact(path)

    # ------------------------------------------------------------- 解算面
    def solve(self, artifact, solve_config=None):
        return self.solver.solve(artifact, solve_config)

    def query(self, solve_id, variable):
        return self.solver.query(solve_id, variable)

    def explain(self, solve_id, variable):
        return self.solver.explain(solve_id, variable)

    def get_report(self, solve_id):
        return self.solver.get_report(solve_id)

    def list_reports(self):
        if not os.path.isdir(self.report_dir):
            return []
        return sorted(
            f[:-5]
            for f in os.listdir(self.report_dir)
            if f.startswith("solve") and f.endswith(".json")
        )

    # ------------------------------------------------------------- 验证面
    def verify_compile(self, artifact):
        return _verify_compile(self.catalog, artifact)

    def verify_solve(self, solve_id):
        report = self.get_report(solve_id)
        artifact = self.load_artifact(report["artifact_hash"])
        return _verify_solve(self.catalog, artifact, report)

    # ------------------------------------------------------------- 票版面
    def issue_ticket(self, artifact_or_hash, face_vars, ticket_id, label=None):
        """从指定冻结快照的编译产物发行票面校验关系。"""
        artifact = self._as_artifact(artifact_or_hash)
        return self.ticket_office.issue(artifact, face_vars, ticket_id, label=label)

    def get_ticket(self, ticket_id):
        return self.ticket_office.get_ticket(ticket_id)

    def list_tickets(self):
        return self.ticket_office.list_tickets()

    def check_face(self, ticket_id, assignment):
        """校版员独立核查一组票面值，拿内页补写或冲突依据。"""
        return self.ticket_office.check_face(ticket_id, assignment)

    def verify_ticket(self, ticket_id):
        ticket = self.ticket_office.get_ticket(ticket_id)
        artifact = self.load_artifact(ticket["artifact_hash"])
        return _verify_ticket(artifact, ticket)

    # ------------------------------------------------------------- 收单面
    def analyze_paper(self, ticket_id, paper, total_form, k):
        """纸票辨错：唯一栏/必错栏/可能错栏/相容复原/合计是否固定。"""
        ticket = self.ticket_office.get_ticket(ticket_id)
        return analyze_paper(ticket, paper, total_form, k)

    def plan_inquiries(self, ticket_id, paper, total_form, k,
                       catalog_items, budget, max_branches):
        """辨错 + 询证套餐规划：合计已固定则直接提示记账，否则给两限内套餐。"""
        ticket = self.ticket_office.get_ticket(ticket_id)
        analysis = analyze_paper(ticket, paper, total_form, k)
        if analysis["status"] != "ambiguous":
            return {"analysis": analysis, "planning": None}
        catalog = InquiryCatalog(ticket, catalog_items)
        recoveries = feasible_recoveries(ticket, _as_fractions(paper), k)
        planning = recommend_plans(
            catalog, recoveries, _parse_total_form(total_form),
            budget, max_branches,
        )
        return {"analysis": analysis, "planning": planning}

    # ------------------------------------------------------------- 订单面
    def open_order(self, ticket_id, paper, total_form, k,
                   catalog_items, budget, max_branches):
        ticket = self.ticket_office.get_ticket(ticket_id)
        analysis = analyze_paper(ticket, paper, total_form, k)
        return self.order_desk.open_order(
            ticket, paper, total_form, k, analysis,
            catalog_items, budget, max_branches,
        )

    def dispatch_inquiries(self, order_id, item_ids):
        return self.order_desk.dispatch(order_id, item_ids)

    def receive_reply(self, order_id, ticket_id, issue_hash, item_id, value):
        return self.order_desk.receive_reply(
            order_id, ticket_id, issue_hash, item_id, value)

    def get_order(self, order_id):
        return self.order_desk.get_order(order_id)

    def list_orders(self):
        return self.order_desk.list_orders()

    def verify_booking(self, order_id):
        order = self.order_desk.get_order(order_id)
        ticket = self.ticket_office.get_ticket(order["ticket_id"])
        artifact = self.load_artifact(ticket["artifact_hash"])
        return _verify_booking(artifact, ticket, order)

    # ------------------------------------------------------------- 内部辅助
    def _as_artifact(self, artifact_or_hash):
        if isinstance(artifact_or_hash, dict):
            return artifact_or_hash
        return self.load_artifact(artifact_or_hash)


def _as_fractions(paper):
    from fractions import Fraction

    return {v: (f if isinstance(f, Fraction) else Fraction(f))
            for v, f in (paper or {}).items()}


def _parse_total_form(total_form):
    from fractions import Fraction

    return {
        "constant": Fraction(total_form.get("constant", 0)),
        "terms": {v: Fraction(c) for v, c in total_form.get("terms", {}).items()},
    }
