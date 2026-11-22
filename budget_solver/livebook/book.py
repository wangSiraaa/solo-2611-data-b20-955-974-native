"""单向换挡实况册：触点组登记、盘面枚举与机械师查询（可重开、不可变）。

实况册是挂在一份**冻结编译产物**上的机械台账：

* 机械师申报每个触点组接管的真实输入格（起值/终值）、必须先动的前置组；
  组内原子动作只发生一次、各组接管的输入格互不重叠；未申报格与跨模型钉选
  完全沿用冻结模型；
* 起值与冻结模型不符、组间前置成环、超限（>10 组 / >8 针 / >12 照片）一律
  整册退回，不落盘；
* 一个盘面 = 已动组集合；只有前置全部已动的组才能继续开动，因此可达盘面构成
  偏序 DAG，物理上允许经过不可检定的盘面（只是该盘面不能读数）。
"""

import os
from fractions import Fraction

from ..compiler import var_name
from ..errors import BudgetError, NotFoundError, ValidationError
from ..rationals import parse_rational, qstr
from ..util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic
from .plates import needle_closure_union, needle_readings, plate_input_values

MAX_GROUPS = 10
MAX_NEEDLES = 8
MAX_PHOTOS = 12


class LiveBookError(BudgetError):
    """实况册登记/查询类业务错误。"""

    def __init__(self, message, errors=None):
        super().__init__(message)
        self.errors = errors or []


def _require(cond, message, errors):
    if not cond:
        errors.append(message)


def _find_cycle(prereq):
    """对前置图做迭代 DFS，返回一条环（节点序列），无环返回 None。"""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {g: WHITE for g in prereq}
    for root in sorted(prereq):
        if color[root] != WHITE:
            continue
        stack = [(root, iter(sorted(prereq[root])))]
        path = [root]
        color[root] = GRAY
        while stack:
            node, it = stack[-1]
            advanced = False
            for nxt in it:
                if nxt not in color:
                    continue  # 未登记前置已在别处报错
                if color[nxt] == GRAY:
                    idx = path.index(nxt)
                    return path[idx:] + [nxt]
                if color[nxt] == WHITE:
                    color[nxt] = GRAY
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(prereq[nxt]))))
                    advanced = True
                    break
            if advanced:
                continue
            color[node] = BLACK
            stack.pop()
            path.pop()
    return None


def _enumerate_plates(book):
    """枚举全部可达盘面：已动集合 S 可达当且仅当 S 内每组的前置也在 S 内。"""
    prereq = {g["group_id"]: set(g["requires"]) for g in book["groups"]}
    all_groups = sorted(prereq)
    reachable = []
    for mask in range(1 << len(all_groups)):
        chosen = {g for i, g in enumerate(all_groups) if mask & (1 << i)}
        ok = all(prereq[g] <= chosen for g in chosen)
        if ok:
            reachable.append(tuple(sorted(chosen)))
    reachable.sort(key=lambda s: (len(s), s))
    return [{"plate_id": f"P{i:02d}", "moved_groups": list(s)}
            for i, s in enumerate(reachable)]


def _find_plate(plates, plate_id):
    for plate in plates:
        if plate["plate_id"] == plate_id:
            return plate
    raise LiveBookError(f"盘面 {plate_id} 不可达或不存在")


class _LiveBookReader:
    """加载/盘面/指针查询逻辑，供 :class:`LiveBook` 继承。"""

    def get_book(self, book_ref):
        if book_ref in self._index["books"]:
            lb_id = self._index["books"][book_ref]["livebook_id"]
        else:
            lb_id = book_ref
        path = os.path.join(self.root_dir, f"{lb_id}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"实况册 {book_ref} 不存在")
        return read_json(path)

    def list_books(self):
        return [dict(meta, book_id=bid) for bid, meta in sorted(self._index["books"].items())]

    def _artifact_for(self, book):
        return self.service.load_artifact(book["artifact_hash"])

    def plates(self, book_ref):
        return _enumerate_plates(self.get_book(book_ref))

    def movable_next(self, book_ref, plate_id):
        """机械师查询：指定盘面上可继续开动的下一组（前置都已动作、自身未动）。"""
        book = self.get_book(book_ref)
        plate = _find_plate(_enumerate_plates(book), plate_id)
        moved = set(plate["moved_groups"])
        prereq = {g["group_id"]: set(g["requires"]) for g in book["groups"]}
        return sorted(g for g in prereq if g not in moved and prereq[g] <= moved)

    def inspect_plate(self, book_ref, plate_id):
        """独查指定盘面：每根针的状态、理想示数及所据原方程。"""
        book = self.get_book(book_ref)
        artifact = self._artifact_for(book)
        plate = _find_plate(_enumerate_plates(book), plate_id)
        record, _ = needle_readings(artifact, book, plate)
        record["input_values"] = {
            v: qstr(val) for v, val in
            sorted(plate_input_values(artifact, book, plate["moved_groups"]).items())
        }
        record["basis_equations"] = self._basis_equations(artifact, record["closure"])
        record["movable_next"] = self.movable_next(book_ref, plate_id)
        return record

    def inspect_needle(self, book_ref, plate_id, variable):
        """独查某根针：读数分类、实际依赖闭包与逐条原方程。"""
        record = self.inspect_plate(book_ref, plate_id)
        if variable not in record["needles"]:
            raise LiveBookError(f"指针 {variable} 未在实况册登记")
        artifact = self._artifact_for(self.get_book(book_ref))
        closure = needle_closure_union(artifact, [variable])
        return {
            "plate_id": plate_id,
            "variable": variable,
            "needle": record["needles"][variable],
            "actual_dependencies": closure,
            "basis_equations": self._basis_equations(artifact, closure),
        }

    @staticmethod
    def _basis_equations(artifact, closure):
        return [
            {
                "variable": v,
                "kind": artifact["variables"][v]["kind"],
                "coeffs": artifact["equations"][v]["coeffs"],
                "frozen_rhs": artifact["equations"][v]["rhs"],
            }
            for v in closure
        ]

    def all_plate_readings(self, book_ref):
        """预计算全部可达盘面的读数分类（发行物计算的前置步骤）。"""
        book = self.get_book(book_ref)
        artifact = self._artifact_for(book)
        records = [needle_readings(artifact, book, plate)[0]
                   for plate in _enumerate_plates(book)]
        return book, artifact, records


class LiveBook(_LiveBookReader):
    """一册绑定编译产物哈希的单向换挡实况册。"""

    def __init__(self, service, root_dir):
        self.service = service
        self.root_dir = os.path.abspath(root_dir)
        ensure_dir(self.root_dir)
        self._books_path = os.path.join(self.root_dir, "books.json")
        if os.path.exists(self._books_path):
            self._index = read_json(self._books_path)
        else:
            self._index = {"seq": 0, "books": {}}
            self._flush()

    def _flush(self):
        write_json_atomic(self._books_path, self._index)

    def _next_id(self, prefix):
        self._index["seq"] += 1
        return f"{prefix}{self._index['seq']}"

    # ----------------------------------------------------------- 登记校验
    def register_book(self, payload):
        errors = []
        book_id = payload.get("book_id")
        _require(isinstance(book_id, str) and book_id, "book_id 必须是非空字符串", errors)
        artifact_ref = payload.get("artifact")
        if not isinstance(artifact_ref, str) or not artifact_ref:
            errors.append("artifact 必须提供编译产物路径或内容哈希")
            artifact = None
        else:
            try:
                artifact = self.service.load_artifact(artifact_ref)
            except NotFoundError:
                artifact = None
                errors.append(f"编译产物 {artifact_ref} 找不到")
        groups_raw = payload.get("groups", [])
        needles_raw = payload.get("needles", [])
        _require(isinstance(groups_raw, list) and groups_raw, "groups 必须是非空数组", errors)
        _require(isinstance(needles_raw, list) and needles_raw, "needles 必须是非空数组", errors)
        _require(len(groups_raw) <= MAX_GROUPS, f"一趟最多 {MAX_GROUPS} 组触点", errors)
        _require(len(needles_raw) <= MAX_NEEDLES, f"一趟最多 {MAX_NEEDLES} 个指针", errors)

        groups, group_ids, owned_cells = [], set(), set()
        if artifact is not None:
            input_vars = {
                v for v, meta in artifact["variables"].items() if meta["kind"] == "input"
            }
            # 第一遍：结构、起终值、格归属
            for i, g in enumerate(groups_raw):
                gid = g.get("group_id")
                _require(isinstance(gid, str) and gid, f"第 {i} 组缺 group_id", errors)
                if gid in group_ids:
                    errors.append(f"组 {gid} 重复登记")
                group_ids.add(gid)
                cells = []
                for j, item in enumerate(g.get("cells", [])):
                    cell_id = item.get("cell_id")
                    model_id = item.get("model_id", artifact["root_model_id"])
                    _require(isinstance(cell_id, str) and cell_id,
                             f"组 {gid} 第 {j} 格缺 cell_id", errors)
                    vname = var_name(model_id, cell_id) if cell_id else None
                    if vname is not None:
                        if vname not in artifact["variables"]:
                            errors.append(f"组 {gid} 申报格 {vname} 不在冻结编译产物内")
                        elif artifact["variables"][vname]["kind"] != "input":
                            errors.append(f"组 {gid} 只能接管输入格，{vname} 是公式格")
                        elif vname in owned_cells:
                            errors.append(f"输入格 {vname} 被多个组接管，组间不得重叠")
                        else:
                            owned_cells.add(vname)
                    start = to = None
                    try:
                        start = parse_rational(item["start"], field=f"组 {gid}.{cell_id}.start")
                        to = parse_rational(item["to"], field=f"组 {gid}.{cell_id}.to")
                    except KeyError:
                        errors.append(f"组 {gid} 格 {cell_id} 必须同时给 start 与 to")
                    except ValidationError as exc:
                        errors.append(f"组 {gid} 格 {cell_id} 起终值非法：{exc}")
                    if vname is not None and start is not None and vname in input_vars:
                        frozen = Fraction(artifact["equations"][vname]["rhs"])
                        if start != frozen:
                            errors.append(
                                f"组 {gid} 格 {vname} 起值 {qstr(start)} 与冻结模型 "
                                f"{qstr(frozen)} 不符，整册退回"
                            )
                    if vname is not None:
                        cells.append({
                            "variable": vname,
                            "model_id": model_id,
                            "cell_id": cell_id,
                            "start": qstr(start) if start is not None else None,
                            "to": qstr(to) if to is not None else None,
                        })
                groups.append({
                    "group_id": gid,
                    "cells": cells,
                    "requires": list(g.get("requires", [])),
                })

            # 第二遍：前置引用与成环
            prereq = {}
            for g in groups:
                gid = g["group_id"]
                reqs = g["requires"]
                _require(isinstance(reqs, list), f"组 {gid} 的 requires 必须是数组", errors)
                for req in reqs:
                    if req not in group_ids:
                        errors.append(f"组 {gid} 的前置组 {req} 未登记")
                    if req == gid:
                        errors.append(f"组 {gid} 不能以自身为前置")
                prereq[gid] = set(reqs)
            cycle = _find_cycle(prereq)
            if cycle:
                errors.append("组间前置成环，整册退回：" + " -> ".join(cycle))

            # 指针登记
            needles = []
            needle_vars = set()
            for i, nd in enumerate(needles_raw):
                cell_id = nd.get("cell_id")
                model_id = nd.get("model_id", artifact["root_model_id"])
                label = nd.get("label", cell_id)
                vname = var_name(model_id, cell_id) if cell_id else None
                if vname is None or vname not in artifact["variables"]:
                    errors.append(f"第 {i} 个指针 {vname} 不在冻结编译产物内")
                    continue
                if vname in needle_vars:
                    errors.append(f"指针 {vname} 重复登记")
                needle_vars.add(vname)
                needles.append({"variable": vname, "label": label})
        else:
            groups = [{"group_id": g.get("group_id"), "cells": [],
                       "requires": list(g.get("requires", []))} for g in groups_raw]
            needles = []

        if errors:
            raise LiveBookError("实况册登记被退回", sorted(set(errors)))

        book = {
            "book_id": book_id,
            "artifact_hash": artifact["artifact_hash"],
            "root_model_id": artifact["root_model_id"],
            "groups": sorted(groups, key=lambda g: g["group_id"]),
            "needles": needles,
            "created_at": utc_now_iso(),
        }
        plates = _enumerate_plates(book)
        book["plate_count"] = len(plates)
        book["content_hash"] = content_hash(
            {k: v for k, v in book.items() if k != "content_hash"}
        )
        lb_id = self._next_id("lb")
        record = {"livebook_id": lb_id, **book}
        write_json_atomic(os.path.join(self.root_dir, f"{lb_id}.json"), record)
        self._index["books"][book_id] = {
            "livebook_id": lb_id,
            "artifact_hash": artifact["artifact_hash"],
            "content_hash": record["content_hash"],
            "plate_count": len(plates),
            "created_at": record["created_at"],
        }
        self._flush()
        return record
