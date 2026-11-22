"""计量室发行引擎：照片在各挡位线上可解释的全部增益区段与整段签发。

计算全部在一维直线上做精确闭区间运算（:mod:`intervals`），保留端点、含孤立点，
不做浮点采样，也不以铭牌两端代替整段承诺。

状态空间是“照片顺序 × 可达盘面”的两层 DAG，并强制**单向换挡**物理约束：

* 每张照片在每个可读盘面上给出一个增益闭区间（各看清针约束与挡位线正增益
  区间的交集）；同照共用盘面；
* 触点组只动一次、不可回退，因此相邻照片的已动集合只能单调增长——允许同态
  （同一盘面连拍）或漏拍（跳过若干盘面），不允许倒退；
* DP 按照片顺序推进，状态记录到达该盘面时仍成立的增益并集；终态合并即该线
  能解释的**全部**增益区段（孤立点自动保留）；
* 同一增益在不同照片上允许换用不同解释（不同盘面序列），整段签发只要求每个
  增益各自存在一条从起点盘面到终点照片的合法链。
"""

import os
from fractions import Fraction

from ..errors import NotFoundError, ValidationError
from ..rationals import qstr
from ..util import content_hash, ensure_dir, read_json, utc_now_iso, write_json_atomic
from .intervals import (
    Interval,
    intersect_unions,
    linear_band,
    merge_intervals,
    uncovered_witness,
    union_contains_point,
    union_contains_segment,
)
from .plates import needle_readings


class IssuanceStore:
    """发行物持久化（不可变，随服务可重开）。"""

    def __init__(self, root_dir):
        self.root_dir = os.path.abspath(root_dir)
        ensure_dir(self.root_dir)
        self._index_path = os.path.join(self.root_dir, "issuances.json")
        if os.path.exists(self._index_path):
            self._index = read_json(self._index_path)
        else:
            self._index = {"seq": 0, "issuances": {}}
            self._flush()

    def _flush(self):
        write_json_atomic(self._index_path, self._index)

    def _next_id(self, prefix):
        self._index["seq"] += 1
        return f"{prefix}{self._index['seq']}"

    def save_issuance(self, record):
        is_id = self._next_id("is")
        record["issuance_uid"] = is_id
        write_json_atomic(os.path.join(self.root_dir, f"{is_id}.json"), record)
        self._index["issuances"][is_id] = {
            "photoset_uid": record["photoset_uid"],
            "line_count": len(record["lines"]),
            "content_hash": record["content_hash"],
            "created_at": record["created_at"],
        }
        self._flush()
        return record

    def get_issuance(self, is_ref):
        if is_ref in self._index["issuances"]:
            is_id = is_ref
        else:
            is_id = is_ref
        path = os.path.join(self.root_dir, f"{is_id}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"发行物 {is_ref} 不存在")
        return read_json(path)


# ============================================================== 盘面可读性
def plate_ideal_values(plate_records_by_id):
    """返回 {盘面: {针变量: Fraction}|None}；None 表示盘面联合矛盾、不可检定。"""
    readable = {}
    for plate_id, record in plate_records_by_id.items():
        if record["status"] != "consistent":
            readable[plate_id] = None
            continue
        vals = {}
        for vname, info in record["needles"].items():
            if info["status"] == "determined":
                vals[vname] = Fraction(info["value"])
        readable[plate_id] = vals
    return readable


def photo_plate_interval(photo, plate_id, readable, line):
    """一张照片在一个盘面上、一条挡位线下可解释的增益闭区间（无解为 None）。

    板卡线给零偏 ``b = u·g + v``，针读数约束（x 为该盘面理想示数）::

        lo <= g·x + b <= hi  即  lo <= (x + u)·g + v <= hi

    所以这是 alpha = x + u、beta = v 的线性带；各看清针约束与挡位线正增益
    区间联立取交。针在该盘面非唯一则禁拍。
    """
    vals = readable.get(plate_id)
    if vals is None:
        return None
    u = Fraction(line["u"])
    v = Fraction(line["v"])
    result = Interval(Fraction(line["g_min"]), Fraction(line["g_max"]))
    for rd in photo["readings"]:
        needle = rd["variable"]
        if needle not in vals:
            return None
        hit = linear_band(
            vals[needle] + u, v, Fraction(rd["lo"]), Fraction(rd["hi"])
        )
        if hit is None:
            return None
        result = result.intersect(hit)
        if result is None:
            return None
    return result


def _plate_moved(plates, plate_id):
    return set(next(p for p in plates if p["plate_id"] == plate_id)["moved_groups"])


# ============================================================== DP 发行
def compute_line_issue(photos, plates, readable, line):
    """单条挡位线的完整 DP，返回可解释并集与逐层工作结构。"""
    plate_ids = [p["plate_id"] for p in plates]
    moved_of = {pid: _plate_moved(plates, pid) for pid in plate_ids}
    # 合法单向后继：prev 已动集合 ⊆ cur 已动集合（含同态）
    successors = {
        pid: [q for q in plate_ids if moved_of[q] >= moved_of[pid]]
        for pid in plate_ids
    }
    per = []
    for photo in photos:
        per.append({
            pid: photo_plate_interval(photo, pid, readable, line)
            for pid in plate_ids
        })

    # dp[pid]：处理完当前照片、末张落在 pid 时仍成立的增益闭区间并集
    dp = {
        pid: ([per[0][pid]] if per[0][pid] is not None else [])
        for pid in plate_ids
    }
    dp_layers = [{pid: [iv.to_json() for iv in dp[pid]] for pid in plate_ids}]
    for k in range(1, len(photos)):
        new_dp = {}
        for pid in plate_ids:
            here = per[k][pid]
            if here is None:
                new_dp[pid] = []
                continue
            incoming = merge_intervals(
                iv for prev in plate_ids if pid in successors[prev]
                for iv in dp[prev]
            )
            new_dp[pid] = intersect_unions([incoming, [here]]) if incoming else []
        dp = new_dp
        dp_layers.append({pid: [iv.to_json() for iv in dp[pid]] for pid in plate_ids})

    explainable = merge_intervals(iv for pid in plate_ids for iv in dp[pid])
    return {
        "line": line,
        "explainable_gains": [iv.to_json() for iv in explainable],
        "photo_plate_intervals": [
            {
                "photo_id": photos[k]["photo_id"],
                "by_plate": {
                    pid: (per[k][pid].to_json() if per[k][pid] is not None else None)
                    for pid in plate_ids
                },
            }
            for k in range(len(photos))
        ],
        "dp_layers": dp_layers,
        "_per": per,
        "_plate_ids": plate_ids,
        "_plates": plates,
        "_readable": readable,
        "_moved_of": moved_of,
    }


def issue(store, live_book, photoset, lines_payload):
    """对照片集在每条挡位线上发行全部可解释增益区段。"""
    book = live_book.get_book(photoset["book_id"])
    artifact = live_book._artifact_for(book)
    plates = live_book.plates(book["book_id"])
    plate_records = {}
    for plate in plates:
        record, _ = needle_readings(artifact, book, plate)
        plate_records[record["plate_id"]] = record
    readable = plate_ideal_values(plate_records)
    photos = photoset["photos"]

    working = {}
    public_lines = {}
    for line in lines_payload["lines"]:
        data = compute_line_issue(photos, plates, readable, line)
        working[line["line_id"]] = data
        public_lines[line["line_id"]] = {
            "line": line,
            "explainable_gains": data["explainable_gains"],
            "photo_plate_intervals": data["photo_plate_intervals"],
            "dp_layers": data["dp_layers"],
        }

    record = {
        "report_kind": "gain_issuance",
        "photoset_uid": photoset["photoset_uid"],
        "photoset_id": photoset["photoset_id"],
        "book_id": book["book_id"],
        "artifact_hash": book["artifact_hash"],
        "plate_records": {pid: _strip_plate(rec) for pid, rec in plate_records.items()},
        "lines": public_lines,
        "created_at": utc_now_iso(),
    }
    record["content_hash"] = content_hash(
        {k: v for k, v in record.items() if k != "content_hash"}
    )
    saved = store.save_issuance(record)
    saved["_working"] = working  # 同进程见证/动作单复用，不随 JSON 落盘
    return saved


def _strip_plate(record):
    out = {
        "plate_id": record["plate_id"],
        "moved_groups": record["moved_groups"],
        "status": record["status"],
        "needles": record["needles"],
    }
    if record.get("contradictions"):
        out["contradictions"] = record["contradictions"]
    return out


# ============================================================== 见证/解释
def explain_gain(issuance, line_id, gain, photoset, live_book):
    """为某个具体有理增益指出一条可执行的动作与拍摄位置链（不读机械原模型）。

    只消费发行物里的逐照片×盘面区间与盘面台账，现场无需重新编译。
    """
    gain = Fraction(gain)
    data = _line_working(issuance, line_id)
    per, plate_ids, plates, readable, moved_of = (
        data["_per"], data["_plate_ids"], data["_plates"], data["_readable"], data["_moved_of"])
    photos = photoset["photos"]
    chain = []
    # 自后向前：每层选一个包含 gain 且满足单向单调的盘面
    for k in range(len(photos) - 1, -1, -1):
        candidates = [
            pid for pid in plate_ids
            if per[k][pid] is not None and per[k][pid].contains(gain)
        ]
        if not candidates:
            return {"explainable": False, "gain": qstr(gain), "line_id": line_id}
        if not chain:
            pick = candidates[0]
        else:
            later = chain[-1]
            valid = [pid for pid in candidates if moved_of[later] >= moved_of[pid]]
            if not valid:
                return {"explainable": False, "gain": qstr(gain), "line_id": line_id}
            pick = valid[0]
        chain.append(pick)
    chain.reverse()
    return {
        "explainable": True,
        "gain": qstr(gain),
        "line_id": line_id,
        "shots": [
            {
                "photo_id": photos[k]["photo_id"],
                "order": photos[k]["order"],
                "plate_id": chain[k],
                "moved_groups": sorted(moved_of[chain[k]]),
                "ideal_values": {
                    rd["variable"]: qstr(readable[chain[k]][rd["variable"]])
                    for rd in photos[k]["readings"]
                },
            }
            for k in range(len(photos))
        ],
        "action_sheet": build_action_sheet(
            live_book, live_book.get_book(photoset["book_id"]), chain, plates),
    }


def build_action_sheet(live_book, book, plate_chain, plates):
    """把拍摄盘面链展开成一张预印动作单：前置合法的拓扑线性化 + 拍照位置。

    动作单写死一趟动作顺序与每次拍照前应到达的盘面；同一单可供多个增益照做，
    只要这些增益在该链上都成立。
    """
    prereq = {g["group_id"]: set(g["requires"]) for g in book["groups"]}
    final_moved = set(_plate(plates, plate_chain[-1])["moved_groups"])
    ordered = []
    remaining = set(final_moved)
    while remaining:
        ready = sorted(g for g in remaining if prereq[g] <= set(ordered))
        if not ready:
            raise ValidationError("动作线性化失败：前置无法满足")
        ordered.append(ready[0])
        remaining.discard(ready[0])
    steps, done, shot_after = [], set(), {}
    for k, pid in enumerate(plate_chain):
        target = set(_plate(plates, pid)["moved_groups"])
        while done < target:
            nxt = next(g for g in ordered if g not in done and prereq[g] <= done)
            done.add(nxt)
            steps.append({"step": len(steps) + 1, "action": "move_group", "group_id": nxt})
        shot_after[k] = len(steps)
    return {
        "group_order": ordered,
        "steps": steps,
        "shot_plates": [
            {"after_step": shot_after[k], "plate_id": pid}
            for k, pid in enumerate(plate_chain)
        ],
    }


def _plate(plates, plate_id):
    return next(p for p in plates if p["plate_id"] == plate_id)


# ============================================================== 整段签发
def certify_segment(issuance, line_id, lo, hi):
    """客户铭牌段 [lo, hi] 能否整段签发；退签给段内一个无法解释的精确增益。"""
    data = _line_working(issuance, line_id)
    segment = Interval(Fraction(lo), Fraction(hi))
    unions = [Interval.from_json(obj) for obj in data["explainable_gains"]]
    g_min, g_max = Fraction(data["line"]["g_min"]), Fraction(data["line"]["g_max"])
    if segment.lo < g_min or segment.hi > g_max:
        witness = segment.lo if segment.lo < g_min else segment.hi
        return {
            "certified": False, "line_id": line_id, "segment": segment.to_json(),
            "witness_gain": qstr(witness),
            "reason": "铭牌段超出挡位线正增益区间",
        }
    if union_contains_segment(unions, segment):
        return {
            "certified": True, "line_id": line_id, "segment": segment.to_json(),
            "explainable_cover": data["explainable_gains"],
        }
    return {
        "certified": False, "line_id": line_id, "segment": segment.to_json(),
        "witness_gain": qstr(uncovered_witness(unions, segment)),
        "reason": "段内存在无法解释的精确增益，拒签证据如上",
        "explainable_cover": data["explainable_gains"],
    }


# ============================================================== 驻点复检/动作单
def station_recheck(issuance, line_id, gains, sheet_budget, photoset, live_book):
    """给定可夹带动作单数，求最少需现场重查几个点，并交可执行手单。

    一张动作单写死一趟动作与拍照位置，服务所有共用该盘面链的复检增益；覆盖不
    了的点由驻点员当场从发行物重查。复检点 ≤4，穷举单子子集求最小集合覆盖。
    """
    data = _line_working(issuance, line_id)
    points = [Fraction(g) for g in gains]
    point_info = {}
    for g in points:
        key = qstr(g)
        if not union_contains_point(
                [Interval.from_json(o) for o in data["explainable_gains"]], g):
            point_info[key] = {"explainable": False}
            continue
        explanation = explain_gain(issuance, line_id, g, photoset, live_book)
        point_info[key] = {
            "explainable": True,
            "chain": tuple(s["plate_id"] for s in explanation["shots"]),
            "explanation": explanation,
        }
    explainable_points = [q for q in point_info if point_info[q]["explainable"]]
    chains = {}
    for q in explainable_points:
        chains.setdefault(point_info[q]["chain"], []).append(q)
    candidates = [
        {"chain": chain, "covers": sorted(covered, key=Fraction)}
        for chain, covered in chains.items()
    ]
    best = None
    target = set(explainable_points)
    for mask in range(1, 1 << len(candidates)):
        chosen = [candidates[i] for i in range(len(candidates)) if mask & (1 << i)]
        covered = {q for c in chosen for q in c["covers"]}
        if covered == target and (best is None or len(chosen) < len(best)):
            best = chosen
    min_sheets = len(best) if best is not None else (1 if explainable_points else 0)
    chosen_sheets = (best or [])[:sheet_budget]
    sheet_records, covered_by_sheets = [], set()
    book = live_book.get_book(photoset["book_id"])
    for c in chosen_sheets:
        chain = list(c["chain"])
        sheet_records.append({
            "serves_gains": c["covers"],
            "chain_plates": chain,
            "action_sheet": build_action_sheet(live_book, book, chain, data["_plates"]),
        })
        covered_by_sheets.update(c["covers"])
    on_site = sorted(target - covered_by_sheets, key=Fraction)
    return {
        "line_id": line_id,
        "recheck_gains": [qstr(g) for g in points],
        "min_sheets_needed": min_sheets,
        "sheet_budget": sheet_budget,
        "sheets_issued": sheet_records,
        "on_site_lookups": on_site,
        "on_site_lookup_count": len(on_site),
        "unexplainable_points": [
            qstr(g) for g in points if not point_info[qstr(g)]["explainable"]
        ],
    }


def _line_working(issuance, line_id):
    working = issuance.get("_working")
    if working is None:
        raise NotFoundError("发行物工作态仅在同进程可用；请经服务门面重新发行")
    if line_id not in working:
        raise NotFoundError(f"发行物中没有挡位线 {line_id}")
    return working[line_id]
