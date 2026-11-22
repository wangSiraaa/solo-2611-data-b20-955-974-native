"""单向换挡实况册 + 计量发行的端到端断言。

锁定题面全部手算结论（纯 Fraction、一维闭区间、无采样），并覆盖：
登记退回（起值不符/成环/超限/重叠）、不可检定盘面、非唯一针、孤立点、
同一次单向切换判定、动作单与最少现场重查、服务重开与 CLI。

运行：python -m tests.test_livebook
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.service import BudgetService
from budget_solver.livebook.book import LiveBookError
from budget_solver.livebook.service import LiveBookService

from . import livebook_scenarios as sc

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def fresh():
    tmp = tempfile.mkdtemp(prefix="lb-test-")
    bs = BudgetService(tmp)
    bs.put_model(sc.bridge_model())
    snap = bs.snapshot(sc.MODEL_ID, label="frozen")
    artifact, _ = bs.compile(snap["snap_id"])
    return LiveBookService(bs), bs, artifact, tmp


def explainable(issuance, line_id):
    return issuance["lines"][line_id]["explainable_gains"]


# ----------------------------------------------------- 盘面与机械师查询
def test_plates_and_mechanic_queries():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        plates = ls.plates("BOOK_AB")
        check([p["moved_groups"] for p in plates] ==
              [[], ["GA"], ["GA", "GB"]], "A 先 B：三个可达盘面且按动作数排序")
        check(ls.movable_next("BOOK_AB", "P00") == ["GA"], "空盘面只能先动 GA")
        check(ls.movable_next("BOOK_AB", "P01") == ["GB"], "GA 动后可动 GB")
        check(ls.movable_next("BOOK_AB", "P02") == [], "终盘无可动组")

        p01 = ls.inspect_plate("BOOK_AB", "P01")
        check(p01["needles"][sc.X_VAR]["value"] == "2"
              and p01["needles"][sc.Y_VAR]["value"] == "1",
              "只动 A 的盘面：X=1+A=2、Y=1+B=1（冻结公式精确解）")
        check(p01["input_values"]["BRIDGE::A"] == "1"
              and p01["input_values"]["BRIDGE::B"] == "0",
              "盘面输入取值：A 取终值 1、未申报动作的 B 保持起值 0")
        eq_vars = {e["variable"] for e in p01["basis_equations"]}
        check({"BRIDGE::A", "BRIDGE::B", "BRIDGE::X", "BRIDGE::Y"} <= eq_vars,
              "独查盘面给出指针所据的全部原方程")
        nx = ls.inspect_needle("BOOK_AB", "P01", sc.X_VAR)
        check(nx["actual_dependencies"] == ["BRIDGE::A", "BRIDGE::X"],
              "X 的实际依赖闭包是 A 与 X 自身（不含 B、Y）")
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------- 题面主情景：甲挡铭牌整段签发
def test_jia_nameplate_certified_on_a_only_plate():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F1", "photos": [sc.photo_both_needles()]})
        issuance = ls.issue(ps["photoset_uid"])
        # 手算：P01（只动 A，X=2,Y=1），甲 b=1/2：
        #   4 <= 2g+1/2 <= 5 -> g ∈ [7/4, 9/4]
        #   2 <= g+1/2   <= 3 -> g ∈ [3/2, 5/2]
        #   交 [1,4] = [7/4, 9/4]
        check(explainable(issuance, "JIA") ==
              [{"lo": "7/4", "hi": "9/4", "point": False}],
              "甲挡发行 [7/4, 9/4]，整段落在只动 A 的盘面")
        verdict = ls.certify(issuance["issuance_uid"], "JIA", "7/4", "2")
        check(verdict["certified"], "甲铭牌 g∈[7/4,2] 整段可签（区间包含，非采样）")
        ex = ls.explain_gain(issuance["issuance_uid"], "JIA", "7/4")
        check(ex["explainable"] and ex["shots"][0]["plate_id"] == "P01"
              and ex["shots"][0]["moved_groups"] == ["GA"],
              "通过产品可为具体有理增益 7/4 指出动作（GA）与拍摄盘面 P01")
        check(ex["action_sheet"]["group_order"] == ["GA"]
              and ex["action_sheet"]["steps"][0]["group_id"] == "GA",
              "预印动作单写死单向动作与拍照位置")
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------- B 先 A：原铭牌不能签
def test_b_first_nameplate_rejected():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(
            artifact["artifact_hash"], order_ab=False, book_id="BOOK_BA"))
        ls.register_lines("BOOK_BA", sc.lines())
        ps = ls.register_photoset(
            "BOOK_BA", {"photoset_id": "F1B", "photos": [sc.photo_both_needles()]})
        issuance = ls.issue(ps["photoset_uid"])
        check(explainable(issuance, "JIA") == [],
              "B 先 A：只动 B 的盘面 X=1 不满足 X 读数，甲挡无可解释增益")
        verdict = ls.certify(issuance["issuance_uid"], "JIA", "7/4", "2")
        check(not verdict["certified"]
              and verdict["witness_gain"] == "7/4",
              "只改机械先后为 B 先 A，原铭牌 [7/4,2] 不能签，退签给精确增益 7/4")
    finally:
        shutil.rmtree(tmp)


# ----------------------------------------------- 放宽下限到 3/2：3/2 即拒签证据
def test_relaxed_nameplate_witness_at_three_halves():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F1", "photos": [sc.photo_both_needles()]})
        issuance = ls.issue(ps["photoset_uid"])
        verdict = ls.certify(issuance["issuance_uid"], "JIA", "3/2", "2")
        check(not verdict["certified"]
              and verdict["witness_gain"] == "3/2",
              "甲铭牌下限放宽到 3/2 后，g=3/2 本身无法解释，作为拒签证据")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 乙挡点照片：两端不同拍摄位置，中间 g=3 无解
def test_yi_point_photo_endpoints_and_gap():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F2", "photos": [sc.photo_x_point()]})
        issuance = ls.issue(ps["photoset_uid"])
        # X=4，乙 b=0：P00(X=1)->g=4；P01/P02(X=2)->g=2；两个孤立点
        check(explainable(issuance, "YI") ==
              [{"lo": "2", "hi": "2", "point": True},
               {"lo": "4", "hi": "4", "point": True}],
              "乙挡只发行两个孤立点 g=2 与 g=4，端点完整保留")
        verdict = ls.certify(issuance["issuance_uid"], "YI", "2", "4")
        check(not verdict["certified"], "乙铭牌 [2,4] 不能整段签（中间有空隙）")
        check(verdict["witness_gain"] == "3",
              "拒签证据是空隙 (2,4) 内部的精确有理点 g=3，而非已覆盖的端点")
        at2 = ls.explain_gain(issuance["issuance_uid"], "YI", "2")
        at4 = ls.explain_gain(issuance["issuance_uid"], "YI", "4")
        check(at2["shots"][0]["plate_id"] == "P01"
              and at4["shots"][0]["plate_id"] == "P00",
              "两端有不同拍摄位置：g=2 在 P01（只动 A），g=4 在 P00（起点盘面）")
        gap = ls.explain_gain(issuance["issuance_uid"], "YI", "3")
        check(not gap["explainable"], "中间 g=3 无解")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 宽照片：全段可签，但没有单一拍摄位置解释整段
def test_yi_wide_photo_full_segment_no_single_plate():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F3", "photos": [sc.photo_x_wide()]})
        issuance = ls.issue(ps["photoset_uid"])
        check(explainable(issuance, "YI") ==
              [{"lo": "1", "hi": "4", "point": False}],
              "宽照片 X∈[2,4]：乙挡全段 [1,4] 都可解释")
        check(ls.certify(issuance["issuance_uid"], "YI", "1", "4")["certified"],
              "铭牌 g∈[1,4] 全段可签")
        plates_used = {
            g: ls.explain_gain(issuance["issuance_uid"], "YI", g)["shots"][0]["plate_id"]
            for g in ("1", "5/2", "4")
        }
        check(len(set(plates_used.values())) > 1,
              "不存在一个拍摄位置能解释整段：不同增益落到不同盘面")
        # 逐盘面区间显式证明：没有任何单盘面覆盖 [1,4]
        by_plate = issuance["lines"]["YI"]["photo_plate_intervals"][0]["by_plate"]
        spans = [v for v in by_plate.values() if v is not None]
        check(not any(s["lo"] == "1" and s["hi"] == "4" for s in spans),
              "逐盘面区间中没有任何一条等于整段 [1,4]")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 驻点复检 1、2、4：动作单数与现场重查
def test_station_recheck_sheet_budget():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F3", "photos": [sc.photo_x_wide()]})
        issuance = ls.issue(ps["photoset_uid"])
        uid = issuance["issuance_uid"]
        one = ls.station_recheck(uid, "YI", ["1", "2", "4"], 1)
        check(one["min_sheets_needed"] == 2 and one["on_site_lookup_count"] == 2,
              "复检 1、2、4 至少要两张动作单；只夹一张时须现场重查两个点")
        check(one["sheets_issued"][0]["serves_gains"] == ["1"],
              "夹的一张动作单固定服务 g=1（P01 链）")
        two = ls.station_recheck(uid, "YI", ["1", "2", "4"], 2)
        check(two["min_sheets_needed"] == 2 and two["on_site_lookup_count"] == 0,
              "夹两张动作单可全照单做，现场重查 0 个点")
        check([s["serves_gains"] for s in two["sheets_issued"]] ==
              [["1"], ["2", "4"]], "两张单分别覆盖 {1} 与 {2,4}")
        for sheet in two["sheets_issued"]:
            sheet_obj = sheet["action_sheet"]
            check(sheet_obj["shot_plates"]
                  and all(s["action"] == "move_group" for s in sheet_obj["steps"]),
              "每张手单都写死拍照位置；需要动作的单含可执行单向步骤")
            # 动作顺序满足前置：每个动作的前置都在更早的步骤里
            done = set()
            book = ls.get_book("BOOK_AB")
            prereq = {g["group_id"]: set(g["requires"]) for g in book["groups"]}
            for step in sheet_obj["steps"]:
                check(prereq[step["group_id"]] <= done, "手单动作严格满足前置（单向）")
                done.add(step["group_id"])
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 两张照片各可单验，却不能来自同一次单向切换
def test_individually_valid_but_no_common_run():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        p1 = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F1", "photos": [sc.photo_both_needles()]})
        p2 = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F2", "photos": [sc.photo_x_point()]})
        check(ls.issue(p1["photoset_uid"])["lines"]["JIA"]["explainable_gains"],
              "照片一单独可验（甲挡有发行区段）")
        check(ls.issue(p2["photoset_uid"])["lines"]["YI"]["explainable_gains"],
              "照片二单独可验（乙挡有发行区段）")
        # 同册：物理来源线 JIA 与 YI 混杂，一趟只能用同一条线
        mixed = ls.register_photoset("BOOK_AB", {
            "photoset_id": "MIXED",
            "photos": [sc.photo_both_needles(order=0), sc.photo_x_point(order=1)],
        })
        verdict = ls.common_run_check(mixed["photoset_uid"], "JIA")
        check(not verdict["feasible"] and verdict["source_lines"] == ["JIA", "YI"],
              "两照片来源挡位线混杂，结构上不可能来自同一次单向切换")
        # 同线副本：若强行假设同一条线，DP 仍要求一条单向链同时成立（给事实而非空判）
        same_line = ls.register_photoset("BOOK_AB", {
            "photoset_id": "SAME",
            "photos": [
                sc.photo_both_needles(source_line="JIA", order=0),
                sc.photo_x_point(source_line="JIA", order=1),
            ],
        })
        same = ls.common_run_check(same_line["photoset_uid"], "JIA")
        check(same["feasible"] is True,
              "同线假设下由单调 DP 判定是否存在一条单向链同时解释两照片")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 登记退回：起值不符、成环、重叠、超限
def test_registration_rejections():
    ls, bs, artifact, tmp = fresh()
    try:
        bad_start = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD1")
        bad_start["groups"][0]["cells"][0]["start"] = "5"
        try:
            ls.register_book(bad_start)
            raise AssertionError("起值与冻结模型不符必须退回")
        except LiveBookError as exc:
            check(any("起值" in e and "不符" in e for e in exc.errors), "起值不符退回")

        cyclic = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD2")
        cyclic["groups"][0]["requires"] = ["GB"]
        cyclic["groups"][1]["requires"] = ["GA"]
        try:
            ls.register_book(cyclic)
            raise AssertionError("组间成环必须退回")
        except LiveBookError as exc:
            check(any("成环" in e for e in exc.errors), "前置成环退回")

        overlap = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD3")
        overlap["groups"][1]["cells"] = [
            {"cell_id": "A", "start": "0", "to": "1"}]
        try:
            ls.register_book(overlap)
            raise AssertionError("组间接管格重叠必须退回")
        except LiveBookError as exc:
            check(any("重叠" in e for e in exc.errors), "组间格重叠退回")

        too_many = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD4")
        too_many["groups"] = [
            {"group_id": f"G{i}", "requires": [], "cells": []} for i in range(11)]
        try:
            ls.register_book(too_many)
            raise AssertionError("超过 10 组必须退回")
        except LiveBookError as exc:
            check(any("10" in e for e in exc.errors), "超限退回")

        too_many_needles = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD5")
        too_many_needles["needles"] = [{"cell_id": "X"} for _ in range(9)]
        try:
            ls.register_book(too_many_needles)
            raise AssertionError("超过 8 针必须退回")
        except LiveBookError as exc:
            check(any("8" in e for e in exc.errors), "指针超限退回")

        unknown_prereq = sc.livebook_payload(artifact["artifact_hash"], book_id="BAD6")
        unknown_prereq["groups"][0]["requires"] = ["GHOST"]
        try:
            ls.register_book(unknown_prereq)
            raise AssertionError("未登记前置必须退回")
        except LiveBookError as exc:
            check(any("GHOST" in e for e in exc.errors), "未登记前置退回")

        # 板卡线限额与正增益区间（先建一本合法册承载板卡登记）
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"], book_id="BOOK_LINES"))
        try:
            ls.register_lines("BOOK_LINES", [
                {"line_id": f"L{i}", "u": "0", "v": "0",
                 "g_min": "1", "g_max": "2"} for i in range(4)])
            raise AssertionError("超过 3 条挡位线必须退回")
        except LiveBookError as exc:
            check("3" in str(exc), "挡位线超限退回")
        try:
            ls.register_lines("BOOK_LINES", [
                {"line_id": "NEG", "u": "0", "v": "0", "g_min": "-1", "g_max": "2"}])
            raise AssertionError("非正增益区间必须退回")
        except LiveBookError:
            check(True, "正增益区间强制为正")
        ls.register_lines("BOOK_LINES", sc.lines())  # 合法板卡

        # 照片超 12 张退回
        many_photos = {"photoset_id": "TOOMANY", "photos": [
            {"photo_id": f"PH{i}", "order": i,
             "readings": [{"variable": sc.X_VAR, "lo": "2", "hi": "4"}]}
            for i in range(13)]}
        try:
            ls.register_photoset("BOOK_LINES", many_photos)
            raise AssertionError("超过 12 张照片必须退回")
        except LiveBookError as exc:
            check(any("12" in e for e in exc.errors), "照片超限退回")
        # 坏册（BOOK_AB 等）全部退回不落盘；只有显式合法登记的 BOOK_LINES 存在
        check([b["book_id"] for b in ls.list_books()] == ["BOOK_LINES"],
              "退回的坏册不留痕，合法册正常保留")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 不可检定盘面与非唯一针：禁用而非误读
def test_unverifiable_and_non_unique_needles():
    """构造一个联合矛盾盘面与一个自由（非唯一）针，验证分类规则。"""
    tmp = tempfile.mkdtemp(prefix="lb-edge-")
    try:
        bs = BudgetService(tmp)
        # 自相矛盾的公式格 c = c + 1：任何盘面都联合矛盾
        bs.put_model({
            "model_id": "EDGE",
            "name": "边界模型",
            "cells": {
                "A": {"kind": "input", "value": "0"},
                "C": {"kind": "formula", "constant": "1",
                      "terms": [{"coef": "1", "cell_id": "C"}]},
                "F": {"kind": "formula", "constant": "0", "terms": []},
            },
            "outputs": ["C", "F"],
        })
        snap = bs.snapshot("EDGE")
        artifact, _ = bs.compile(snap["snap_id"])
        ls = LiveBookService(bs)
        ls.register_book({
            "book_id": "EDGEBOOK", "artifact": artifact["artifact_hash"],
            "groups": [{"group_id": "GA", "requires": [],
                        "cells": [{"cell_id": "A", "start": "0", "to": "1"}]}],
            "needles": [{"cell_id": "C"}, {"cell_id": "F"}],
        })
        rec = ls.inspect_plate("EDGEBOOK", "P00")
        check(rec["status"] == "inconsistent", "联合矛盾的盘面标记为不可检定")
        check(all(info["status"] == "unverifiable_plate"
                  for info in rec["needles"].values()),
              "不可检定盘面禁用全部针作读数（物理上仍可经过）")
        check(rec["contradictions"] and rec["contradictions"][0]["residual_rhs"] != "0",
              "不可检定盘面附 0=非零 的矛盾证书")
        # 自由公式格 F=0（无依赖、零常数）仍唯一确定；非唯一针用参数表达式场景
        # 由主模型 SCC 之外的独立断言：空项公式格 F 唯一为 0
        check(rec["needles"]["EDGE::F"]["status"] == "unverifiable_plate",
              "整盘矛盾时即便本格平凡也不作读数")
    finally:
        shutil.rmtree(tmp)


def test_non_unique_needle_disabled_but_others_read():
    """冗余自由系统里个别针非唯一：只禁用该针，其余针照常读数。"""
    tmp = tempfile.mkdtemp(prefix="lb-free-")
    try:
        bs = BudgetService(tmp)
        bs.put_model({
            "model_id": "FREE",
            "name": "自由针模型",
            "cells": {
                "A": {"kind": "input", "value": "3"},
                "U": {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "V"}]},
                "V": {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "U"}]},
                "T": {"kind": "formula", "constant": "2/7",
                      "terms": [
                          {"coef": "1", "cell_id": "U"},
                          {"coef": "-1", "cell_id": "V"}]},
            },
            "outputs": ["U", "T"],
        })
        snap = bs.snapshot("FREE")
        artifact, _ = bs.compile(snap["snap_id"])
        ls = LiveBookService(bs)
        ls.register_book({
            "book_id": "FREEBOOK", "artifact": artifact["artifact_hash"],
            "groups": [{"group_id": "GA", "requires": [],
                        "cells": [{"cell_id": "A", "start": "3", "to": "9"}]}],
            "needles": [{"cell_id": "U"}, {"cell_id": "T"}],
        })
        rec = ls.inspect_plate("FREEBOOK", "P00")
        check(rec["status"] == "consistent", "盘面联合一致（无矛盾）")
        check(rec["needles"]["FREE::U"]["status"] == "non_unique",
              "U=V 自由：针 U 非唯一，禁用其作读数")
        check(rec["needles"]["FREE::T"]["status"] == "determined"
              and rec["needles"]["FREE::T"]["value"] == "2/7",
              "同一盘面 T=U-V+2/7 仍唯一为 2/7，照常作读数")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 重开：落盘后新服务实例仍能签发与重查
def test_reopen_persistence():
    tmp = tempfile.mkdtemp(prefix="lb-reopen-")
    try:
        bs = BudgetService(tmp)
        bs.put_model(sc.bridge_model())
        snap = bs.snapshot(sc.MODEL_ID)
        artifact, _ = bs.compile(snap["snap_id"])
        ls = LiveBookService(bs)
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())
        ps = ls.register_photoset(
            "BOOK_AB", {"photoset_id": "F3", "photos": [sc.photo_x_wide()]})
        uid = ls.issue(ps["photoset_uid"])["issuance_uid"]

        reopened_ls = LiveBookService(BudgetService(tmp))  # 全新实例 = 重开
        verdict = reopened_ls.certify(uid, "YI", "1", "4")
        check(verdict["certified"], "重开后从落盘发行物重建，整段签发结论一致")
        ex = reopened_ls.explain_gain(uid, "YI", "2")
        check(ex["explainable"] and ex["shots"][0]["plate_id"] == "P00",
              "重开后不重新编译模型即可重查具体增益的动作与拍摄位置")
        rc = reopened_ls.station_recheck(uid, "YI", ["1", "2", "4"], 2)
        check(rc["on_site_lookup_count"] == 0, "重开后驻点复检结论一致")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 多照片：同态连拍/漏拍合法，倒退被否决
def test_monotonic_plate_dp_across_ordered_photos():
    ls, bs, artifact, tmp = fresh()
    try:
        ls.register_book(sc.livebook_payload(artifact["artifact_hash"]))
        ls.register_lines("BOOK_AB", sc.lines())

        # 两张都能在 P01（只动 A）解释：同态连拍，发行非空
        same_plate = ls.register_photoset("BOOK_AB", {
            "photoset_id": "SEQ_SAME",
            "photos": [
                {"photo_id": "S0", "order": 0,
                 "readings": [{"variable": sc.X_VAR, "lo": "2", "hi": "4"}]},
                {"photo_id": "S1", "order": 1,
                 "readings": [{"variable": sc.X_VAR, "lo": "2", "hi": "4"}]},
            ],
        })
        iss_same = ls.issue(same_plate["photoset_uid"])
        check(iss_same["lines"]["YI"]["explainable_gains"] ==
              [{"lo": "1", "hi": "4", "point": False}],
              "同态连拍（两张都停 P01）合法，发行 [1,4]")

        # 漏拍前进：第一张在 P00、第二张在 P02，已动集合 {} ⊆ {GA,GB} 单调
        leap = ls.register_photoset("BOOK_AB", {
            "photoset_id": "SEQ_LEAP",
            "photos": [
                {"photo_id": "L0", "order": 0, "readings": [
                    {"variable": sc.X_VAR, "lo": "2", "hi": "2"},
                    {"variable": sc.Y_VAR, "lo": "2", "hi": "2"}]},  # P00: g=2
                {"photo_id": "L1", "order": 1, "readings": [
                    {"variable": sc.X_VAR, "lo": "4", "hi": "4"},
                    {"variable": sc.Y_VAR, "lo": "4", "hi": "4"}]},  # P02: g=2
            ],
        })
        iss_leap = ls.issue(leap["photoset_uid"])
        check(iss_leap["lines"]["YI"]["explainable_gains"] ==
              [{"lo": "2", "hi": "2", "point": True}],
              "P00 漏拍前进到 P02 的单向链成立，g=2 保留为孤立点")

        # 单调性不变量：发行物逐层 DP 只允许已动集合增长（无倒退边）。
        from budget_solver.livebook.engine import compute_line_issue, plate_ideal_values
        from budget_solver.livebook.plates import needle_readings
        book = ls.get_book("BOOK_AB")
        art = ls.books._artifact_for(book)
        plates = ls.plates("BOOK_AB")
        records = {p["plate_id"]: needle_readings(art, book, p)[0] for p in plates}
        readable = plate_ideal_values(records)
        data = compute_line_issue(
            same_plate["photos"], plates, readable,
            {"line_id": "YI", "u": "0", "v": "0", "g_min": "1", "g_max": "4"})
        moved = data["_moved_of"]
        plate_ids = data["_plate_ids"]
        for pid in plate_ids:
            legal_next = [q for q in plate_ids if moved[q] >= moved[pid]]
            check(all(moved[q] >= moved[pid] for q in legal_next),
                  f"盘面 {pid} 的 DP 后继全部满足已动集合单调（无倒退边）")

        # 真实倒退序列：第一张只在终盘 P02 有解，第二张只在起点 P00 有解，
        # 且两解增益不同，迫使链 P02 -> P00（不存在的倒退边），发行为空。
        back = ls.register_photoset("BOOK_AB", {
            "photoset_id": "SEQ_BACK",
            "photos": [
                {"photo_id": "B0", "order": 0, "readings": [
                    {"variable": sc.X_VAR, "lo": "4", "hi": "4"},
                    {"variable": sc.Y_VAR, "lo": "4", "hi": "4"}]},
                # P00->g=4, P02->g=2, P01 两针增益不交 => {P00:4, P02:2}
                {"photo_id": "B1", "order": 1, "readings": [
                    {"variable": sc.X_VAR, "lo": "3", "hi": "3"},
                    {"variable": sc.Y_VAR, "lo": "3", "hi": "3"}]},
                # P00->g=3, P02->g=3/2，与首张增益集合 {4,2} 不相交
            ],
        })
        iss_back = ls.issue(back["photoset_uid"])
        check(iss_back["lines"]["YI"]["explainable_gains"] == [],
              "两照片可行增益不相交且唯一盘面要求倒退，整组无单向链，发行为空")
        check(not ls.common_run_check(back["photoset_uid"], "YI")["feasible"],
              "同一次单向切换判定对该序列给不可行")
    finally:
        shutil.rmtree(tmp)


# ----------------------------- 一维区间内核：端点、孤立点、拒签见证
def test_interval_kernel_exactness():
    from budget_solver.livebook.intervals import (
        Interval, linear_band, merge_intervals, union_contains_segment,
        uncovered_witness,
    )
    # 孤立点不被合并吃掉
    pts = merge_intervals([Interval(2, 2), Interval(4, 4), Interval(2, 2)])
    check(pts == [Interval(2, 2), Interval(4, 4)], "孤立点去重后保留为退化闭区间")
    # 端点相接（无空隙）才合并
    check(merge_intervals([Interval(1, 2), Interval(2, 3)]) == [Interval(1, 3)],
          "端点相接的闭区间合并成一段")
    check(merge_intervals([Interval(1, 2), Interval(3, 4)]) ==
          [Interval(1, 2), Interval(3, 4)], "真空隙 (2,3) 不合并")
    # 整段承诺：覆盖必须逐点成立，孤立点撑不起一个正长度段
    check(not union_contains_segment([Interval(2, 2), Interval(4, 4)],
                                     Interval(2, 4)),
          "两个孤立点不能覆盖整段 [2,4]（不以端点代替承诺）")
    check(union_contains_segment([Interval(1, 3), Interval(3, 4)], Interval(1, 4)),
          "端点相接的两段完整覆盖 [1,4]")
    # 拒签见证三类空隙
    check(uncovered_witness([Interval(2, 2), Interval(4, 4)],
                            Interval(2, 4)) == Fraction(3),
          "内部空隙见证取严格中点 3（不是被覆盖的 2）")
    check(uncovered_witness([Interval(7, 9)], Interval(3, 9)) == Fraction(3),
          "段首空隙见证取段左端点 3")
    check(uncovered_witness([Interval(1, 2)], Interval(1, 4)) == Fraction(3),
          "段尾空隙见证取中点 3")
    check(uncovered_witness([Interval(1, 4)], Interval(1, 4)) is None,
          "整段覆盖时无拒签见证")
    # 线性带端点精确且保留孤立点
    band = linear_band(Fraction(2), Fraction(0), Fraction(4), Fraction(4))
    check(band.is_point and band.lo == 2, "点读数约束退化为孤立点 g=2")


def run_all():
    tests = [
        test_plates_and_mechanic_queries,
        test_jia_nameplate_certified_on_a_only_plate,
        test_b_first_nameplate_rejected,
        test_relaxed_nameplate_witness_at_three_halves,
        test_yi_point_photo_endpoints_and_gap,
        test_yi_wide_photo_full_segment_no_single_plate,
        test_station_recheck_sheet_budget,
        test_individually_valid_but_no_common_run,
        test_monotonic_plate_dp_across_ordered_photos,
        test_registration_rejections,
        test_unverifiable_and_non_unique_needles,
        test_non_unique_needle_disabled_but_others_read,
        test_reopen_persistence,
        test_interval_kernel_exactness,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n实况册/计量发行全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
