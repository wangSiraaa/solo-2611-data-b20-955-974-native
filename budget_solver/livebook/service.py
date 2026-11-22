"""单向换挡实况册 + 计量发行的应用门面。

挂在原有 :class:`~budget_solver.service.BudgetService` 之上，沿用模型登记、
快照、编译与单点 query；自身持久化在 ``<data-root>/livebook/`` 下，可整体重开，
现场不重新编译模型。
"""

import os

from ..errors import NotFoundError
from .book import LiveBook
from .engine import (
    IssuanceStore,
    certify_segment,
    compute_line_issue,
    explain_gain,
    issue,
    plate_ideal_values,
    station_recheck,
)
from .photos import PhotoSetStore
from .plates import needle_readings


class LiveBookService:
    def __init__(self, budget_service):
        self.budget = budget_service
        self.root = os.path.join(budget_service.data_root, "livebook")
        self.books = LiveBook(budget_service, os.path.join(self.root, "books"))
        self.photosets = PhotoSetStore(self.books, os.path.join(self.root, "photosets"))
        self.issuances = IssuanceStore(os.path.join(self.root, "issuances"))

    # ---------------------------------------------------------- 机械师面
    def register_book(self, payload):
        return self.books.register_book(payload)

    def list_books(self):
        return self.books.list_books()

    def get_book(self, book_ref):
        return self.books.get_book(book_ref)

    def plates(self, book_ref):
        return self.books.plates(book_ref)

    def movable_next(self, book_ref, plate_id):
        return self.books.movable_next(book_ref, plate_id)

    def inspect_plate(self, book_ref, plate_id):
        return self.books.inspect_plate(book_ref, plate_id)

    def inspect_needle(self, book_ref, plate_id, variable):
        return self.books.inspect_needle(book_ref, plate_id, variable)

    # ---------------------------------------------------------- 照片/线面
    def register_lines(self, book_ref, lines):
        return self.photosets.register_lines(book_ref, lines)

    def register_photoset(self, book_ref, payload):
        return self.photosets.register_photoset(book_ref, payload)

    def get_photoset(self, ps_ref):
        return self.photosets.get_photoset(ps_ref)

    def list_photosets(self):
        return self.photosets.list_photosets()

    def _require_lines(self, book_id, lines_override=None):
        if lines_override is not None:
            return self.photosets.register_lines(book_id, lines_override)
        return self.photosets.get_lines(book_id)

    # ---------------------------------------------------------- 计量发行面
    def issue(self, ps_ref, lines_override=None):
        photoset = self.get_photoset(ps_ref)
        lines_payload = self._require_lines(photoset["book_id"], lines_override)
        return issue(self.issuances, self.books, photoset, lines_payload)

    def get_issuance(self, is_ref):
        return self.issuances.get_issuance(is_ref)

    def explain_gain(self, is_ref, line_id, gain):
        issuance = self.get_issuance(is_ref)
        photoset = self.get_photoset(issuance["photoset_uid"])
        # 重开发行物：从落盘区间重建工作态，保证现场不重新编译也能重查
        working = self._rebuild_working(issuance, photoset, line_id)
        issuance["_working"] = {line_id: working}
        return explain_gain(issuance, line_id, gain, photoset, self.books)

    def certify(self, is_ref, line_id, lo, hi):
        issuance = self._load_with_working(is_ref)
        return certify_segment(issuance, line_id, lo, hi)

    def station_recheck(self, is_ref, line_id, gains, sheet_budget):
        issuance = self._load_with_working(is_ref)
        photoset = self.get_photoset(issuance["photoset_uid"])
        return station_recheck(
            issuance, line_id, gains, sheet_budget, photoset, self.books)

    # ------------------------------------------------ 同一次单向切换判定
    def common_run_check(self, ps_ref, line_id, lines_override=None):
        photoset = self.get_photoset(ps_ref)
        lines_payload = self._require_lines(photoset["book_id"], lines_override)
        lines_by_id = {ln["line_id"]: ln for ln in lines_payload["lines"]}
        if line_id not in lines_by_id:
            raise NotFoundError(f"挡位线 {line_id} 未登记")
        # 结构判定：照片物理来源板卡线混杂时，整趟一条线的前提即不成立
        sources = {p.get("source_line") for p in photoset["photos"] if p.get("source_line")}
        if len(sources) > 1:
            return {
                "feasible": False,
                "line_id": line_id,
                "reason": "照片物理来源挡位线混杂，整趟只能使用同一条线",
                "source_lines": sorted(sources),
            }
        # 同线假设：重算该线 DP，终态非空即存在一条单向链解释整组照片
        book = self.books.get_book(photoset["book_id"])
        artifact = self.books._artifact_for(book)
        plates = self.books.plates(book["book_id"])
        records = {}
        for plate in plates:
            rec, _ = needle_readings(artifact, book, plate)
            records[rec["plate_id"]] = rec
        readable = plate_ideal_values(records)
        data = compute_line_issue(
            photoset["photos"], plates, readable, lines_by_id[line_id])
        feasible = bool(data["explainable_gains"])
        return {
            "feasible": feasible,
            "line_id": line_id,
            "explainable_gains_if_common_line": data["explainable_gains"],
            "reason": "存在单向盘面链同时解释全部照片" if feasible
                      else "没有任何单向盘面链能在该线上同时解释全部照片",
        }

    # ------------------------------------------------ 工作态重建（重开）
    def _load_with_working(self, is_ref):
        issuance = self.get_issuance(is_ref)
        photoset = self.get_photoset(issuance["photoset_uid"])
        book = self.books.get_book(photoset["book_id"])
        artifact = self.books._artifact_for(book)
        plates = self.books.plates(book["book_id"])
        records = {}
        for plate in plates:
            rec, _ = needle_readings(artifact, book, plate)
            records[rec["plate_id"]] = rec
        readable = plate_ideal_values(records)
        issuance["_working"] = {}
        for line_id, public in issuance["lines"].items():
            line = public["line"]
            issuance["_working"][line_id] = compute_line_issue(
                photoset["photos"], plates, readable, line)
        return issuance

    def _rebuild_working(self, issuance, photoset, line_id):
        book = self.books.get_book(photoset["book_id"])
        artifact = self.books._artifact_for(book)
        plates = self.books.plates(book["book_id"])
        records = {}
        for plate in plates:
            rec, _ = needle_readings(artifact, book, plate)
            records[rec["plate_id"]] = rec
        readable = plate_ideal_values(records)
        line = issuance["lines"][line_id]["line"]
        return compute_line_issue(photoset["photos"], plates, readable, line)
