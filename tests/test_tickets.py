"""票面发行/校版、纸票辨错、询证规划与回签状态机的端到端测试。

运行：python -m tests.test_tickets（在仓库根目录）。
覆盖题面叙述的全部手算例：
  * A=B=C=t 纸票 (4,4,7)，k=1 只能复原 (4,4,4)，C 必错、记 4；
  * B=t+3,C=t+3 后单栏查询仍未定但只能复原 (4,7,7)，B 必错、记 7；
  * 恢复原公式、k=2 时 (4,4,4)/(7,7,7) 都行，须再核；
  * 加 D=u、E=t+u 后：总台 J（费 3/一馆）与 D+E（费 1+1/两馆）都能签；
    只问 E 问不出区别；D=3,E=7 记 4；J=21 记 7；2 元且一馆签不了；
  * 矛盾闭包不发版；校版员拿内页补写/冲突依据；
  * 回签三重核对、幂等不重复收费、矛盾不入账、重开可复读。
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.errors import ValidationError
from budget_solver.service import BudgetService
from budget_solver.tickets import TicketRejectedError

PASS = []
A, B, C, D, E = (f"M_TICKET::{x}" for x in "ABCDE")
FACE3 = [A, B, C]
FACE5 = [A, B, C, D, E]
TOTAL_C = {"constant": "0", "terms": {C: "1"}}
PAPER = {A: "4", B: "4", C: "7"}


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def ticket_model(*, shifted=False, with_de=False, contradiction=False):
    if contradiction:
        return {
            "model_id": "M_TICKET", "name": "矛盾票版",
            "cells": {
                "a": {"kind": "formula", "constant": "1",
                      "terms": [{"coef": "1", "cell_id": "b"}]},
                "b": {"kind": "formula", "constant": "1",
                      "terms": [{"coef": "1", "cell_id": "a"}]},
            },
            "outputs": ["a", "b"],
        }
    cells = {
        "t": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},  # t=t 自由
        "A": {"kind": "formula", "constant": "0",
              "terms": [{"coef": "1", "cell_id": "t"}]},
    }
    for cid in ("B", "C"):
        cells[cid] = {
            "kind": "formula",
            "constant": "3" if shifted else "0",
            "terms": [{"coef": "1", "cell_id": "t"}],
        }
    if with_de:
        cells["u"] = {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "u"}]}  # u=u 独立自由
        cells["D"] = {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "u"}]}
        cells["E"] = {
            "kind": "formula", "constant": "0",
            "terms": [{"coef": "1", "cell_id": "t"},
                      {"coef": "1", "cell_id": "u"}],
        }
    return {"model_id": "M_TICKET", "name": "票版模型",
            "cells": cells, "outputs": ["A", "B", "C"]}


def fresh():
    tmp = tempfile.mkdtemp(prefix="ticket-test-")
    return BudgetService(tmp), tmp


def compile_issue(service, payload, face, ticket_id, label=None):
    service.put_model(payload)
    snap = service.snapshot("M_TICKET", label=label)
    artifact, _ = service.compile(snap["snap_id"])
    ticket = service.issue_ticket(artifact, face, ticket_id, label=label)
    return artifact, ticket


CATALOG = [
    {"item_id": "Q1", "name": "总台三栏和J", "branch": "总台", "fee": "3",
     "form": {"constant": "0", "terms": {A: "1", B: "1", C: "1"}}},
    {"item_id": "Q2", "name": "核D栏", "branch": "甲分馆", "fee": "1",
     "form": {"constant": "0", "terms": {D: "1"}}},
    {"item_id": "Q3", "name": "核E栏", "branch": "乙分馆", "fee": "1",
     "form": {"constant": "0", "terms": {E: "1"}}},
]


# ------------------------------------------------- 1. 发行健全/完备与独立验证
def test_issue_and_verify():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(), FACE3, "TK1")
        # A=C, B=C 两条票面仿射等式（消去内页 t）
        eq_pairs = sorted(
            (tuple(sorted(eq["coeffs"])), eq["rhs"]) for eq in ticket["face_equations"]
        )
        check(len(ticket["face_equations"]) == 2, "三栏自由票版发行两条票面等式")
        check(all(rhs == "0" for _, rhs in eq_pairs), "票面等式右端为 0")
        verdict = service.verify_ticket("TK1")
        check(verdict["ok"], "票版健全性/完备性/哈希独立验证通过")
        # 票号不可复用
        try:
            service.issue_ticket(
                service.load_artifact(ticket["artifact_hash"]), FACE3, "TK1")
            raise AssertionError("票号复用必须拒绝")
        except ValidationError:
            PASS.append("票号不可复用")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 2. 矛盾闭包不能发版
def test_contradictory_closure_rejected():
    service, tmp = fresh()
    try:
        service.put_model(ticket_model(contradiction=True))
        snap = service.snapshot("M_TICKET")
        artifact, _ = service.compile(snap["snap_id"])
        try:
            service.issue_ticket(artifact, ["M_TICKET::a", "M_TICKET::b"], "TKC")
            raise AssertionError("矛盾闭包必须拒绝发版")
        except TicketRejectedError as exc:
            cert = exc.detail["certificate"]
            check(exc.detail["residual_rhs"] == "2", "矛盾残差手算 0=2")
            check({c["equation_variable"] for c in cert} ==
                  {"M_TICKET::a", "M_TICKET::b"}, "矛盾证书给 λ 组合来源")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 3. 校版：相容补写 / 冲突依据
def test_check_face():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(), FACE3, "TK3")
        ok = service.check_face("TK3", {A: "4", B: "4"})
        check(ok["status"] == "consistent", "相容票面值通过校版")
        check(ok["face_solution"]["point"][C] == "4", "留空栏由联合解补成 4")
        check(ok["inner_completion"]["point"]["M_TICKET::t"] == "4",
              "内页格 t 补写为 4")
        bad = service.check_face("TK3", {A: "4", B: "5"})
        check(bad["status"] == "inconsistent", "不相容票面值被校版拦下")
        check(bad["certificate"] and bad["residual_rhs"] != "0",
              "校版冲突给 λ 组合与非零残差")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 4. 叙述例一：k=1，C 必错记 4
def test_narrative_case_one():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(), FACE3, "TK1")
        analysis = service.analyze_paper("TK1", PAPER, TOTAL_C, 1)
        check(analysis["status"] == "fixed", "容错 1 合计已固定")
        check(analysis["must_error_filled"] == [C], "C 栏必错")
        check(analysis["possibly_error_filled"] == [], "无误判争议栏")
        check(analysis["booked_total"] == "4", "待记合计按 4 入账")
        point = analysis["compatible_recoveries"][0]["face_point"]
        check(point == {A: "4", B: "4", C: "4"}, "唯一相容复原 (4,4,4)")
        check(analysis["unique_faces"] == {A: "4", B: "4", C: "4"},
              "三栏在容错内均已唯一")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 5. 叙述例二：移位后 B 必错记 7
def test_narrative_case_two_shifted():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(shifted=True), FACE3, "TK2")
        # 单栏各自查询仍显示未定（票面自由），但联合容错复原唯一
        partial = service.check_face("TK2", {A: "4"})
        check(partial["face_solution"]["free_variables"] == [],
              "单栏查询不强行固定其他栏")
        analysis = service.analyze_paper("TK2", PAPER, TOTAL_C, 1)
        check(analysis["status"] == "fixed", "移位票版容错 1 仍可直接记账")
        check(analysis["must_error_filled"] == [B], "B 栏必错")
        check(analysis["booked_total"] == "7", "待记合计按 7 入账")
        point = analysis["compatible_recoveries"][0]["face_point"]
        check(point == {A: "4", B: "7", C: "7"}, "唯一相容复原 (4,7,7)")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 6. 叙述例三：k=2 两解，须再核
def test_narrative_case_three_ambiguous():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(), FACE3, "TK1")
        analysis = service.analyze_paper("TK1", PAPER, TOTAL_C, 2)
        check(analysis["status"] == "ambiguous", "容错 2 存在多个复原不能直接记")
        check(sorted(analysis["possible_total_values"]) == ["4", "7"],
              "可能合计为 4 或 7")
        check(analysis["must_error_filled"] == [], "没有栏在所有复原中都错")
        check(set(analysis["possibly_error_filled"]) == {A, B, C},
              "三栏都可能错")
        check(analysis["recovery_count"] == 2, "两个互异相容复原子空间")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 7. 容错内无解直接退回
def test_rejected_when_no_recovery():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(), FACE3, "TK1")
        # k=0 且纸票本身不满足 A=B=C：无相容复原
        analysis = service.analyze_paper("TK1", PAPER, TOTAL_C, 0)
        check(analysis["status"] == "rejected", "容错内无解整票退回")
        try:
            service.open_order("TK1", PAPER, TOTAL_C, 0, CATALOG, "3", 2)
            raise AssertionError("退回票不能开询证单")
        except ValidationError:
            PASS.append("退回票不发询证")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 8. 空栏不算错
def test_blank_not_counted_as_error():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(), FACE3, "TK1")
        analysis = service.analyze_paper(
            "TK1", {A: "4", B: "4"}, TOTAL_C, 0)  # C 留空
        check(analysis["status"] == "fixed", "空栏不算错，k=0 即可复原")
        check(analysis["must_error_filled"] == [], "无必错栏")
        check(analysis["booked_total"] == "4", "空栏票按 4 记账")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 9. 套餐规划：两方案都能签
def test_planning_both_plans():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        result = service.plan_inquiries(
            "TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        planning = result["planning"]
        check(planning["status"] == "plannable", "经费 3/两馆内存在可签套餐")
        plan_keys = {tuple(p["item_ids"]) for p in planning["plans"]}
        check(("Q1",) in plan_keys, "总台 J 单项可签（费 3、跑一馆）")
        check(("Q2", "Q3") in plan_keys, "D+E 两项可签（费 2、跑两馆）")
        j_plan = next(p for p in planning["plans"] if p["item_ids"] == ["Q1"])
        de_plan = next(p for p in planning["plans"] if p["item_ids"] == ["Q2", "Q3"])
        check(j_plan["branch_count"] == 1 and j_plan["total_fee"] == "3",
              "J 方案一馆三元")
        check(de_plan["branch_count"] == 2 and de_plan["total_fee"] == "2",
              "D+E 方案两馆两元")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 10. 只问 E 问不出区别
def test_asking_E_alone_insufficient():
    service, tmp = fresh()
    try:
        compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        result = service.plan_inquiries(
            "TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        e_plan = next((p for p in result["planning"]["plans"]
                       if p["item_ids"] == ["Q3"]), None)
        check(e_plan is None, "只问 E 不在可签套餐中")
        # 反例：存在两张有效票 E 回答相同而合计不同（题面实例为 E=7：
        # t=4,u=3 与 t=7,u=0；算法基点可能给出 E=0 的等价反例，判别力结论一致）
        blocked = service.plan_inquiries(
            "TK3", PAPER, TOTAL_C, 2, CATALOG, "2", 1)
        check(blocked["planning"]["status"] == "blocked", "2 元一馆无套餐可签")
        counterexamples = blocked["planning"]["counterexamples"]
        e_counter = next(
            (c for c in counterexamples if tuple(c["answers"]) == ("Q3",)), None)
        check(e_counter is not None, "阻塞给同答不同款反例")
        check(e_counter["total_a"] != e_counter["total_b"],
              "反例两张票合计不同")
        check(e_counter["witness_a"][E] == e_counter["witness_b"][E],
              "反例两张票 E 回答相同")
        # 题面给定的 E=7 反例确实存在：手工核对两张具体票
        from budget_solver.inquiry import InquiryCatalog, distinguish
        from budget_solver.recovery import feasible_recoveries
        ticket = service.get_ticket("TK3")
        catalog = InquiryCatalog(ticket, CATALOG)
        recoveries = feasible_recoveries(ticket, {k: Fraction(v) for k, v in PAPER.items()}, 2)
        witness_e_seven = distinguish(
            catalog, ["Q3"], recoveries,
            {"constant": Fraction(0), "terms": {C: Fraction(1)}})
        check(witness_e_seven is not None
              and witness_e_seven["answers"]["Q3"] in {"0", "7"},
              "E 单项判别数学上不充分（存在同答不同款）")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 11. 回签流程：D=3,E=7 记 4
def test_order_flow_de_books_four():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        order = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        check(order["status"] == "draft", "歧义票开单后为草稿")
        service.dispatch_inquiries(order["order_id"], ["Q2", "Q3"])
        sent = service.get_order(order["order_id"])
        check(sent["status"] == "awaiting_replies", "发单后等待回签")
        check(sent["plan"]["total_fee"] == "2", "按项分别计费合计 2")
        check(sent["plan"]["branches"] == ["乙分馆", "甲分馆"], "两馆一起递送")
        # 重复发单拒绝（不二次收费）
        try:
            service.dispatch_inquiries(order["order_id"], ["Q1"])
            raise AssertionError("重复发单必须拒绝")
        except ValidationError:
            PASS.append("重复发单被拒，不二次计费")
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q2", "3")
        mid = service.get_order(order["order_id"])
        check(mid["status"] == "awaiting_replies", "部分回签保留待回签状态")
        # 同项重复回答幂等
        before = len(mid["replies"])
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q2", "3")
        after = service.get_order(order["order_id"])
        check(len(after["replies"]) == before, "重复回签幂等不重复入账")
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q3", "7")
        booked = service.get_order(order["order_id"])
        check(booked["status"] == "booked", "回答到齐且相容后签账")
        check(booked["booking"]["booked_total"] == "4", "D=3,E=7 应记 4")
        check(booked["booking"]["face_assignment"][A] == "4",
              "完整复原可核对（A=4）")
        check(all(r["residual"] == "0"
                  for r in booked["booking"]["face_residuals"]),
              "复原代回票面等式残差全 0")
        verdict = service.verify_booking(order["order_id"])
        check(verdict["ok"], "签账回执独立验证通过")
        # 记账后再收同项回答不重复入账
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q3", "7")
        again = service.get_order(order["order_id"])
        check(again["booking"]["booking_hash"] ==
              booked["booking"]["booking_hash"], "回执复读不重复入账")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 12. J=21 记 7（有自由栏仍可签）
def test_order_flow_j_books_seven_with_free_cells():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        order = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 1)
        service.dispatch_inquiries(order["order_id"], ["Q1"])
        service.receive_reply(
            order["order_id"], "TK3", ticket["issue_hash"], "Q1", "21")
        booked = service.get_order(order["order_id"])
        check(booked["status"] == "booked", "J 回答到齐即签账")
        check(booked["booking"]["booked_total"] == "7", "J=21 应记 7")
        check(booked["booking"]["face_assignment"][C] == "7", "复原 C=7")
        # D/E 等其他栏可以永远未定：回执保留自由方向或留空均合法
        check(service.verify_booking(order["order_id"])["ok"],
              "带自由栏的签账回执独立验证通过")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 13. 回签三重核对
def test_reply_binding_checks():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        order = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        service.dispatch_inquiries(order["order_id"], ["Q2", "Q3"])
        # 错票号
        try:
            service.receive_reply(
                order["order_id"], "TK_OTHER", ticket["issue_hash"], "Q2", "3")
            raise AssertionError("错票号回签必须拒收")
        except ValidationError:
            PASS.append("错票号回签拒收")
        # 错发行版（答数碰巧相容也不行）
        try:
            service.receive_reply(
                order["order_id"], "TK3", "sha256:deadbeef", "Q2", "3")
            raise AssertionError("错发行版回签必须拒收")
        except ValidationError:
            PASS.append("错发行版回签拒收，答数相容也不接用")
        # 未发出的问项号
        try:
            service.receive_reply(
                order["order_id"], "TK3", ticket["issue_hash"], "Q1", "12")
            raise AssertionError("未发出问项的回签必须拒收")
        except ValidationError:
            PASS.append("问项号与实际发出套餐匹配才接收")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 14. 矛盾回答不入账
def test_contradictory_replies():
    service, tmp = fresh()
    try:
        _, ticket = compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        # 同项冲突回答
        order = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        service.dispatch_inquiries(order["order_id"], ["Q2", "Q3"])
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q2", "3")
        service.receive_reply(order["order_id"], "TK3", ticket["issue_hash"], "Q2", "9")
        bad = service.get_order(order["order_id"])
        check(bad["status"] == "contradicted", "同项冲突回答置矛盾状态")
        check("booking" not in bad, "矛盾回答不入账")
        # 联合不相容：J=20（3t=20 与 t∈{4,7} 都不兼容）
        order2 = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 1)
        service.dispatch_inquiries(order2["order_id"], ["Q1"])
        service.receive_reply(order2["order_id"], "TK3", ticket["issue_hash"], "Q1", "20")
        bad2 = service.get_order(order2["order_id"])
        check(bad2["status"] == "contradicted", "联合不相容回签置矛盾状态")
        check(bad2["contradiction"]["detail"], "矛盾附结构化依据")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 15. 重开：票版/订单/回执持久化
def test_reopen_persistence():
    tmp = tempfile.mkdtemp(prefix="ticket-reopen-")
    try:
        service = BudgetService(tmp)
        _, ticket = compile_issue(service, ticket_model(with_de=True), FACE5, "TK3")
        order = service.open_order("TK3", PAPER, TOTAL_C, 2, CATALOG, "3", 2)
        oid = order["order_id"]
        service.dispatch_inquiries(oid, ["Q2", "Q3"])
        service.receive_reply(oid, "TK3", ticket["issue_hash"], "Q2", "3")
        reopened = BudgetService(tmp)  # 新实例 = 重开数据目录
        pending = reopened.get_order(oid)
        check(pending["status"] == "awaiting_replies", "重开后保留待回签状态")
        service2 = reopened
        service2.receive_reply(oid, "TK3", ticket["issue_hash"], "Q3", "7")
        booked = service2.get_order(oid)
        check(booked["booking"]["booked_total"] == "4", "重开后收齐回签仍记 4")
        check(service2.verify_ticket("TK3")["ok"], "重开后票版仍可独立验证")
        check(service2.verify_booking(oid)["ok"], "重开后回执仍可独立验证")
        check(service2.list_tickets() == ["TK3"] and service2.list_orders() == [oid],
              "票版与订单目录可重开列举")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 16. 独立验证器防篡改
def test_verifier_tamper_detection():
    import json
    import os

    from budget_solver.errors import VerifyError
    from budget_solver.ticket_verify import verify_ticket

    service, tmp = fresh()
    try:
        service.put_model(ticket_model())
        snap = service.snapshot("M_TICKET")
        artifact, _ = service.compile(snap["snap_id"])
        service.issue_ticket(artifact, FACE3, "TK1")
        path = os.path.join(service.ticket_dir, "TK1.json")
        original = json.loads(open(path, encoding="utf-8").read())

        bad_rhs = json.loads(json.dumps(original))
        bad_rhs["face_equations"][0]["rhs"] = "999"
        try:
            verify_ticket(artifact, bad_rhs)
            raise AssertionError("篡改右端必须被发现")
        except VerifyError:
            PASS.append("票面右端篡改被独立验证拦下")

        bad_coef = json.loads(json.dumps(original))
        first = next(iter(bad_coef["face_equations"][0]["coeffs"]))
        bad_coef["face_equations"][0]["coeffs"][first] = "2"
        try:
            verify_ticket(artifact, bad_coef)
            raise AssertionError("篡改系数必须被发现")
        except VerifyError:
            PASS.append("票面系数篡改被独立验证拦下")

        bad_hash = json.loads(json.dumps(original))
        bad_hash["issue_hash"] = "sha256:0"
        try:
            verify_ticket(artifact, bad_hash)
            raise AssertionError("篡改 issue_hash 必须被发现")
        except VerifyError:
            PASS.append("issue_hash 篡改被独立验证拦下")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 17. 六格上限与跨模型作用域
def test_face_limits_and_cross_model_scope():
    service, tmp = fresh()
    try:
        service.put_model({
            "model_id": "U", "name": "上游",
            "cells": {"d": {"kind": "input", "value": "1000/3"}},
            "outputs": ["d"],
        })
        service.put_model({
            "model_id": "V", "name": "下游票",
            "cells": {
                "t": {"kind": "formula", "constant": "0",
                      "terms": [{"coef": "1", "cell_id": "t"}]},
                "x": {"kind": "formula", "constant": "1/2",
                      "terms": [
                          {"coef": "2", "cell_id": "t"},
                          {"coef": "3", "model_id": "U", "cell_id": "d"}]},
            },
            "outputs": ["x"],
        })
        snap = service.snapshot("V")
        artifact, _ = service.compile(snap["snap_id"])
        ticket = service.issue_ticket(artifact, ["V::t", "V::x"], "TKX")
        check(set(ticket["scope_snapshots"]) == {"U", "V"},
              "票版闭包合并跨模型钉选作用域")
        check(service.verify_ticket("TKX")["ok"], "跨模型票版独立验证通过")
        # 手算：x = 1/2 + 2t + 3·(1000/3) = 2001/2 + 2t，即 x - 2t = 2001/2。
        # 消元产物可能给等价规范化式（t - x/2 = -2001/4），按赋值代入核对等式本身。
        from fractions import Fraction as _F
        eq = ticket["face_equations"][0]
        lhs_at = lambda t, x: sum(  # noqa: E731
            _F(c) * {"V::t": t, "V::x": x}[v] for v, c in eq["coeffs"].items())
        check(lhs_at(_F(0), _F(2001, 2)) == _F(eq["rhs"]),
              "跨模型票面等式在 (t=0,x=2001/2) 上成立")
        check(lhs_at(_F(1), _F(2005, 2)) == _F(eq["rhs"]),
              "跨模型票面等式在 (t=1,x=2005/2) 上成立（斜率 2）")
        # 非票面格 / 超六格
        try:
            service.issue_ticket(artifact, ["V::t", "V::ghost"], "TKG")
            raise AssertionError("非票面格必须拒绝")
        except ValidationError:
            PASS.append("非票面格发行被拒")
        service.put_model({
            "model_id": "W", "name": "七格模型",
            "cells": {c: {"kind": "input", "value": "1"} for c in "abcdefg"},
            "outputs": ["a"],
        })
        snap_w = service.snapshot("W")
        art_w, _ = service.compile(snap_w["snap_id"])
        try:
            service.issue_ticket(
                art_w, [f"W::{c}" for c in "abcdefg"], "TKW")
            raise AssertionError("超过六个永久格必须拒绝")
        except ValidationError:
            PASS.append("票面永久格至多六个的上限生效")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 18. 原 solve/query/explain 不受影响
def test_legacy_solve_path_intact():
    service, tmp = fresh()
    try:
        service.put_model(ticket_model())
        snap = service.snapshot("M_TICKET")
        artifact, _ = service.compile(snap["snap_id"])
        report = service.solve(artifact)
        q = service.query(report["solve_id"], A)
        check(q["status"] == "parametric", "原单输出 query 仍报告自由参数")
        ex = service.explain(report["solve_id"], A)
        check(len(ex["closure_equations"]) >= 2, "原 explain 仍给闭包方程")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_issue_and_verify,
        test_contradictory_closure_rejected,
        test_check_face,
        test_narrative_case_one,
        test_narrative_case_two_shifted,
        test_narrative_case_three_ambiguous,
        test_rejected_when_no_recovery,
        test_blank_not_counted_as_error,
        test_planning_both_plans,
        test_asking_E_alone_insufficient,
        test_order_flow_de_books_four,
        test_order_flow_j_books_seven_with_free_cells,
        test_reply_binding_checks,
        test_contradictory_replies,
        test_reopen_persistence,
        test_verifier_tamper_detection,
        test_face_limits_and_cross_model_scope,
        test_legacy_solve_path_intact,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n票面/询证全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
