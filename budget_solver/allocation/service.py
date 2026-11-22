"""代拨用途变更门面：组装去向册、点名册、拨款账簿三个面，数据可重开。

目录布局（挂在既有 budget 数据根之下，旧预算公式与已发报告原样保留）::

    <root>/allocation/
      sessions/<session_id>.json      # 规范化场次（冻结输入事实）
      ledgers/  + ledgers-orders 不需要
      rollcalls/
      books/    + books-orders/       # 正式拨款单
"""

import os

from ..service import BudgetService
from ..util import ensure_dir, read_json, write_json_atomic
from .books import BookService
from .ledger import LedgerService
from .rollcall import RollcallService
from .session import normalize_session
from . import verifier as allocation_verifier


class AllocationService:
    def __init__(self, data_root):
        self.data_root = os.path.abspath(data_root)
        self.alloc_root = os.path.join(self.data_root, "allocation")
        ensure_dir(self.alloc_root)
        # 复用既有模型/快照/编译/解算/验证全套设施
        self.budget = BudgetService(data_root)
        self.session_dir = os.path.join(self.alloc_root, "sessions")
        self.ledger_service = LedgerService(
            self.budget, os.path.join(self.alloc_root, "ledgers"))
        self.rollcall_service = RollcallService(
            os.path.join(self.alloc_root, "rollcalls"))
        self.book_service = BookService(os.path.join(self.alloc_root, "books"))
        ensure_dir(self.session_dir)

    # ------------------------------------------------------------- 场次面
    def register_session(self, payload):
        session = normalize_session(payload)
        path = os.path.join(self.session_dir, f"{session['session_id']}.json")
        if os.path.exists(path):
            existing = read_json(path)
            if existing != session:
                from ..errors import AllocationValidationError

                raise AllocationValidationError(
                    f"场次 {session['session_id']} 已存在且内容不同（场次不可变）",
                    [session["session_id"]])
            return existing
        write_json_atomic(path, session)
        return session

    def get_session(self, session_id):
        path = os.path.join(self.session_dir, f"{session_id}.json")
        if not os.path.exists(path):
            from ..errors import NotFoundError

            raise NotFoundError(f"场次 {session_id} 不存在")
        return read_json(path)

    def list_sessions(self):
        if not os.path.isdir(self.session_dir):
            return []
        return sorted(f[:-5] for f in os.listdir(self.session_dir) if f.endswith(".json"))

    # ------------------------------------------------------------- 去向册面
    def build_ledger(self, session_id):
        session = self.get_session(session_id)
        return self.ledger_service.build_ledger(session)

    def get_ledger(self, ledger_id):
        return self.ledger_service.get_ledger(ledger_id)

    def list_ledgers(self):
        return self.ledger_service.list_ledgers()

    def verify_ledger(self, ledger_id):
        ledger = self.get_ledger(ledger_id)
        return allocation_verifier.verify_ledger(self.budget, ledger)

    # ------------------------------------------------------------- 点名册面
    def build_rollcall(self, ledger_id):
        ledger = self.get_ledger(ledger_id)
        self.ledger_service.require_settled(ledger)
        return self.rollcall_service.build_rollcall(ledger)

    def get_rollcall(self, roll_id):
        return self.rollcall_service.get_rollcall(roll_id)

    def list_rollcalls(self):
        return self.rollcall_service.list_rollcalls()

    def verify_rollcall(self, ledger_id, roll_id):
        ledger = self.get_ledger(ledger_id)
        rollcall = self.get_rollcall(roll_id)
        return allocation_verifier.verify_rollcall(ledger, rollcall)

    # ------------------------------------------------------------- 拨款面
    def open_book(self, ledger_id, roll_id, label=None):
        ledger = self.get_ledger(ledger_id)
        rollcall = self.get_rollcall(roll_id)
        return self.book_service.open_book(ledger, rollcall, label=label)

    def get_book(self, book_id):
        return self.book_service.get_book(book_id)

    def list_books(self):
        return self.book_service.list_books()

    def book_state(self, book_id):
        return self.book_service.current_state(self.get_book(book_id))

    def check_feasible(self, book_id):
        book = self.get_book(book_id)
        rollcall = self.get_rollcall(book["rollcall_id"])
        return self.book_service.check_feasible(book, rollcall)

    def issue_order(self, book_id, items, order_id=None, dry_run=False):
        book = self.get_book(book_id)
        rollcall = self.get_rollcall(book["rollcall_id"])
        return self.book_service.issue_order(
            book, rollcall, items, order_id=order_id, dry_run=dry_run)

    def get_order(self, order_id):
        return self.book_service.get_order(order_id)

    def list_orders(self):
        return self.book_service.list_orders()

    def verify_book(self, book_id):
        book = self.get_book(book_id)
        return allocation_verifier.verify_book(book, self.book_service.get_order)

    def close_book(self, book_id):
        book = self.get_book(book_id)
        rollcall = self.get_rollcall(book["rollcall_id"])
        return self.book_service.assert_all_approved_funded(book, rollcall)
