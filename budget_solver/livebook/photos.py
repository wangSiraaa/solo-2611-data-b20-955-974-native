"""照片集与挡位线板卡登记。

照片是有顺序的实况证据：每张只对看得清的针报一个**闭**读数区间（端点可相同，
即精确点读数）。同一台仪器的板卡给至多三条真实挡位线，每条线在线路规定的正
增益区间 ``[g_min, g_max]`` 上满足零偏 ``b = u·g + v``（u、v、区间端点都是已知
有理数）；整趟使用同一条线、同一个实数增益 g。
"""

import os

from ..errors import NotFoundError, ValidationError
from ..rationals import parse_rational, qstr
from ..util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic
from .book import MAX_PHOTOS, LiveBookError
from .intervals import Interval

MAX_LINES = 3


class PhotoSetStore:
    """照片集与板卡挡位线的持久化登记（挂在实况册目录下）。"""

    def __init__(self, live_book, root_dir):
        self.live_book = live_book
        self.root_dir = os.path.abspath(root_dir)
        ensure_dir(self.root_dir)
        self._index_path = os.path.join(self.root_dir, "photosets.json")
        self._lines_path = os.path.join(self.root_dir, "lines.json")
        if os.path.exists(self._index_path):
            self._index = read_json(self._index_path)
        else:
            self._index = {"seq": 0, "photosets": {}}
            self._flush()
        if os.path.exists(self._lines_path):
            self._lines = read_json(self._lines_path)
        else:
            self._lines = {}
            self._flush_lines()

    def _flush(self):
        write_json_atomic(self._index_path, self._index)

    def _flush_lines(self):
        write_json_atomic(self._lines_path, self._lines)

    def _next_id(self, prefix):
        self._index["seq"] += 1
        return f"{prefix}{self._index['seq']}"

    # ------------------------------------------------------------- 挡位线
    def register_lines(self, book_ref, lines):
        """登记板卡挡位线（至多三条），落盘并返回规范化结构（重开可见）。"""
        book = self.live_book.get_book(book_ref)
        if not isinstance(lines, list) or not lines:
            raise ValidationError("lines 必须是非空数组")
        if len(lines) > MAX_LINES:
            raise LiveBookError(f"每台仪器板卡至多 {MAX_LINES} 条真实挡位线")
        normalized = []
        ids = set()
        for i, line in enumerate(lines):
            lid = line.get("line_id")
            if not isinstance(lid, str) or not lid:
                raise ValidationError(f"第 {i} 条线缺 line_id")
            if lid in ids:
                raise LiveBookError(f"挡位线 {lid} 重复")
            ids.add(lid)
            u = parse_rational(line["u"], field=f"线 {lid}.u")
            v = parse_rational(line["v"], field=f"线 {lid}.v")
            g_min = parse_rational(line["g_min"], field=f"线 {lid}.g_min")
            g_max = parse_rational(line["g_max"], field=f"线 {lid}.g_max")
            if g_min <= 0 or g_max <= 0:
                raise LiveBookError(f"挡位线 {lid} 必须落在正增益区间（端点为正）")
            if g_min > g_max:
                raise LiveBookError(f"挡位线 {lid} 的增益区间上下确界颠倒")
            normalized.append({
                "line_id": lid,
                "u": qstr(u), "v": qstr(v),
                "g_min": qstr(g_min), "g_max": qstr(g_max),
                "gain_interval": Interval(g_min, g_max).to_json(),
            })
        payload = {
            "book_id": book["book_id"],
            "artifact_hash": book["artifact_hash"],
            "lines": normalized,
            "created_at": utc_now_iso(),
        }
        # 同一册重复登记板卡 => 整体替换（板卡更正），旧册数据保持不变
        self._lines[book["book_id"]] = payload
        self._flush_lines()
        return payload

    def get_lines(self, book_id):
        if book_id not in self._lines:
            raise NotFoundError(f"尚未为实况册 {book_id} 登记挡位线")
        return self._lines[book_id]

    # ------------------------------------------------------------- 照片集
    def register_photoset(self, book_ref, payload):
        book = self.live_book.get_book(book_ref)
        needle_vars = {n["variable"] for n in book["needles"]}
        photos = payload.get("photos", [])
        if not isinstance(photos, list) or not photos:
            raise ValidationError("photos 必须是非空数组")
        errors = []
        if len(photos) > MAX_PHOTOS:
            errors.append(f"一趟最多 {MAX_PHOTOS} 张照片")
        normalized = []
        orders = []
        for i, photo in enumerate(photos):
            pid = photo.get("photo_id", f"PH{i}")
            order = photo.get("order", i)
            if not isinstance(order, int) or isinstance(order, bool):
                errors.append(f"照片 {pid} 的 order 必须是整数")
                order = i
            orders.append(order)
            readings = []
            seen_needles = set()
            for rd in photo.get("readings", []):
                vname = rd["variable"]
                if vname not in needle_vars:
                    errors.append(f"照片 {pid} 读了未登记指针 {vname}")
                    continue
                if vname in seen_needles:
                    errors.append(f"照片 {pid} 对指针 {vname} 重复读数")
                seen_needles.add(vname)
                lo = parse_rational(rd["lo"], field=f"照片 {pid}.{vname}.lo")
                hi = parse_rational(rd["hi"], field=f"照片 {pid}.{vname}.hi")
                if lo > hi:
                    errors.append(f"照片 {pid} 指针 {vname} 读数区间下界>上界")
                readings.append({"variable": vname, "lo": qstr(lo), "hi": qstr(hi)})
            if not readings:
                errors.append(f"照片 {pid} 至少要读清一根针")
            normalized.append({
                "photo_id": pid, "order": order,
                "source_line": photo.get("source_line"),  # 物理来源板卡线，可空
                "readings": readings,
            })
        if len(set(orders)) != len(orders):
            errors.append("照片 order 必须互不相同")
        normalized.sort(key=lambda p: p["order"])
        if errors:
            raise LiveBookError("照片集登记被退回", sorted(set(errors)))
        record = {
            "photoset_id": payload.get("photoset_id", "PS"),
            "book_id": book["book_id"],
            "artifact_hash": book["artifact_hash"],
            "photos": normalized,
            "created_at": utc_now_iso(),
        }
        record["content_hash"] = content_hash(record)
        ps_id = self._next_id("ps")
        record["photoset_uid"] = ps_id
        write_json_atomic(os.path.join(self.root_dir, f"{ps_id}.json"), record)
        self._index["photosets"][ps_id] = {
            "photoset_id": record["photoset_id"],
            "book_id": book["book_id"],
            "photo_count": len(normalized),
            "content_hash": record["content_hash"],
        }
        self._flush()
        return record

    def get_photoset(self, ps_ref):
        if ps_ref in self._index["photosets"]:
            ps_id = ps_ref
        else:
            hits = [uid for uid, m in self._index["photosets"].items()
                    if m["photoset_id"] == ps_ref]
            if not hits:
                raise NotFoundError(f"照片集 {ps_ref} 不存在")
            ps_id = hits[0]
        return read_json(os.path.join(self.root_dir, f"{ps_id}.json"))

    def list_photosets(self):
        return [dict(meta, uid=uid) for uid, meta in sorted(self._index["photosets"].items())]
