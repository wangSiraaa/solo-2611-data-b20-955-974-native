"""代拨去向册子系统的端到端断言（题目全部手算场景）。

运行：PYTHONPATH=. python -m tests.test_allocation
"""

import shutil
import tempfile
from fractions import Fraction

from budget_solver.allocation.service import AllocationService
from budget_solver.errors import (
    AllocationInfeasibleError,
    AllocationValidationError,
    LedgerNotSettledError,
    OrderRejectedError,
)

PASS = []


def check(cond, label):
    if not cond:
        raise AssertionError(label)
    PASS.append(label)


def base_session(*, ratio_d1_x="1/2", cred_cap="1", x_need="3",
                 session_id="S1", own_balance="1"):
    """题面基准场：D1 出 4（按来源 1/2 转 Y），D2 出 2 全留 X；X、Y 全数留用。"""
    return {
        "session_id": session_id,
        "name": "基准代拨场",
        "accounts": ["X", "Y"],
        "funders": ["D1", "D2"],
        "injections": [
            {"funder": "D1", "account": "X", "amount": "4"},
            {"funder": "D2", "account": "X", "amount": "2"},
        ],
        "transfers": [
            {"from": "X", "to": "Y", "ratio": "0",
             "ratios": {"D1": ratio_d1_x, "D2": "0"}},
        ],
        "agenda": [
            {"case_id": "CX", "account": "X", "purpose": "X新用途",
             "need": x_need, "reserve": "1", "threshold": "3/4"},
            {"case_id": "CY", "account": "Y", "purpose": "Y新用途",
             "need": "2", "reserve": "0", "threshold": "1"},
        ],
        "signatures": [
            {"funder": "D1", "case_id": "CX", "vote": "yes"},
            {"funder": "D1", "case_id": "CY", "vote": "yes"},
        ],
        "delegations": [{"funder": "D2", "delegate": "D1"}],
        "credentials": [{"funder": "D2", "case_scope": ["CX", "CY"], "cap": cred_cap}],
        "own_funds": [{"fund_id": "F1", "balance": own_balance,
                       "case_scope": ["CX", "CY"]}],
    }


def fresh():
    tmp = tempfile.mkdtemp(prefix="alo-test-")
    return AllocationService(tmp), tmp


def build_baseline(service, **kw):
    session = service.register_session(base_session(**kw))
    ledger = service.build_ledger(session["session_id"])
    roll = service.build_rollcall(ledger["ledger_id"])
    return session, ledger, roll


# ----------------------------------------------------------- 1. 去向册手算
def test_ledger_hand_values():
    service, tmp = fresh()
    try:
        _, ledger, _ = build_baseline(service)
        check(ledger["settled"], "基准场全部出资户结清")
        check(ledger["retained_by_source"]["D1"] == {"X": "2", "Y": "2"},
              "D1 最终留用 X=2、Y=2（绕环只留用一次）")
        check(ledger["retained_by_source"]["D2"] == {"X": "2", "Y": "0"},
              "D2 的钱全数留 X，不被 X→Y 比例转走")
        check(ledger["total"]["retained"] == {"X": "4", "Y": "2"},
              "合计留用 X=4、Y=2")
        check(ledger["total"]["inflow"] == {"X": "6", "Y": "2"},
              "总流入格：X 注入 6、Y 收 D1 转来 2")
        check(ledger["conservation"]["all_conserved"], "逐户之和=合计守恒")
        result = service.verify_ledger(ledger["ledger_id"])
        check(result["ok"], f"去向册独立验证通过（{result['check_count']} 项）")
    finally:
        shutil.rmtree(tmp)


# ------------------------------------------------- 2. 点名册：门槛/委托证扣额
def test_rollcall_threshold_and_credentials():
    service, tmp = fresh()
    try:
        _, _, roll = build_baseline(service)
        cases = {c["case_id"]: c for c in roll["cases"]}
        check(cases["CX"]["approved"] and cases["CY"]["approved"], "两案都获批")
        check(cases["CX"]["yes_amount"] == "3" and cases["CX"]["yes_share"] == "3/4",
              "X 案同意 3（D1 亲签 2 + D2 委托签 1），份额 3/4")
        check(cases["CY"]["yes_amount"] == "2" and cases["CY"]["yes_share"] == "1",
              "Y 案同意 2/2")
        d2 = next(r for r in cases["CX"]["records"] if r["funder"] == "D2")
        check(d2["mode"] == "delegated" and d2["signer"] == "D1"
              and d2["signed"] == "1" and d2["abstained"] == "1",
              "D2 权益 2、证额 1：只交签 1，其余 1 弃权（反对者的钱划不走）")
        audit = roll["credential_audit"]["D2"]
        check(audit["total_deducted"] == "1" and audit["remaining_after_session"] == "0",
              "委托证两案共用证额，CX 扣 1 后 CY 无证可扣（否决案也扣的口径在此为已用尽）")
        d2_y = next(r for r in cases["CY"]["records"] if r["funder"] == "D2")
        check(d2_y["mode"] == "abstain" and d2_y["signed"] == "0",
              "CY 上 D2 在 Y 本就无权益，弃权不影响 Y 案（D1 2/2 足额）")
        check(service.verify_rollcall(roll["ledger_id"], roll["roll_id"])["ok"],
              "点名册独立验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 3. 两种合法拨法 + 重开核余额
def test_two_legal_payment_books_reopen():
    service, tmp = fresh()
    try:
        _, ledger, roll = build_baseline(service)
        order_cy = [{
            "case_id": "CY", "account": "Y",
            "redirects": [{"funder": "D1", "amount": "2"}],
            "own_fund_inputs": [],
        }]

        # 拨法 A：X 转获签原款 3（D1 2 + D2 1），留旧款 1，自有款不动
        book_a = service.open_book(ledger["ledger_id"], roll["roll_id"], label="拨法A")
        plan_a_x = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"},
                          {"funder": "D2", "amount": "1"}],
            "own_fund_inputs": [],
        }]
        order_a = service.issue_order(book_a["book_id"], plan_a_x)
        service.issue_order(book_a["book_id"], order_cy)
        state_a = service.book_state(book_a["book_id"])
        check(state_a["original_balances"]["D1"]["X"] == "0"
              and state_a["original_balances"]["D2"]["X"] == "1",
              "拨法A 后 X 旧用途：D1=0、D2=1，合计 1（恰守底线）")
        check(state_a["own_fund_balances"]["F1"] == "1", "拨法A 自有款 1 分文未动")
        check(service.close_book(book_a["book_id"])["ok"], "拨法A 获批案全部足额")
        # 次日 X 原用途能否再付 2：只剩 1，不能
        can_pay_2_a = Fraction(state_a["original_balances"]["D1"]["X"]) \
            + Fraction(state_a["original_balances"]["D2"]["X"]) >= 2
        check(not can_pay_2_a, "拨法A 次日 X 旧用途付不出 2")

        # 拨法 B：只转 D1 的 2，另贴自有款 1；留旧款 2，自有款清零
        book_b = service.open_book(ledger["ledger_id"], roll["roll_id"], label="拨法B")
        plan_b_x = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"}],
            "own_fund_inputs": [{"fund_id": "F1", "amount": "1"}],
        }]
        service.issue_order(book_b["book_id"], plan_b_x)
        service.issue_order(book_b["book_id"], order_cy)
        state_b = service.book_state(book_b["book_id"])
        check(state_b["original_balances"]["D1"]["X"] == "0"
              and state_b["original_balances"]["D2"]["X"] == "2",
              "拨法B 后 X 旧用途留 2（D1=0、D2=2）")
        check(state_b["own_fund_balances"]["F1"] == "0", "拨法B 自有款清零")
        can_pay_2_b = Fraction(state_b["original_balances"]["D1"]["X"]) \
            + Fraction(state_b["original_balances"]["D2"]["X"]) >= 2
        check(can_pay_2_b, "拨法B 次日 X 旧用途恰好能再付 2（与拨法A 结论相反）")
        check(service.verify_book(book_a["book_id"])["ok"]
              and service.verify_book(book_b["book_id"])["ok"],
              "两本账簿独立重放验证通过")

        # 重开：新服务实例读同一目录，余额与单号指纹不变
        reopened = AllocationService(tmp)
        ra = reopened.book_state(book_a["book_id"])
        rb = reopened.book_state(book_b["book_id"])
        check(ra == state_a and rb == state_b, "重开后两本账簿余额完全一致")
        check(reopened.get_order(order_a["order_id"])["order_fingerprint"]
              == order_a["order_fingerprint"], "重开后拨款单稳定单号指纹不变")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 4. 稳定单号：同号复读/异文退回
def test_order_stable_id_rules():
    service, tmp = fresh()
    try:
        _, ledger, roll = build_baseline(service)
        book = service.open_book(ledger["ledger_id"], roll["roll_id"])
        plan = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"}],
            "own_fund_inputs": [{"fund_id": "F1", "amount": "1"}],
        }]
        first = service.issue_order(book["book_id"], plan, order_id="order9")
        again = service.issue_order(book["book_id"], plan, order_id="order9")
        check(again["order_fingerprint"] == first["order_fingerprint"],
              "同号同内容幂等复读，不重复下账")
        check(service.book_state(book["book_id"])["own_fund_balances"]["F1"] == "0",
              "复读不产生第二笔扣款")
        # 同号异文退回
        bad = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "1"}],
            "own_fund_inputs": [{"fund_id": "F1", "amount": "2"}],
        }]
        try:
            service.issue_order(book["book_id"], bad, order_id="order9")
            raise AssertionError("同号异文必须退回")
        except OrderRejectedError:
            pass
        # dry-run 不占款、不占单号
        before = service.list_orders()
        dry = service.issue_order(
            book["book_id"],
            [{"case_id": "CY", "account": "Y",
              "redirects": [{"funder": "D1", "amount": "2"}],
              "own_fund_inputs": []}], dry_run=True)
        check(dry["dry_run"] and service.list_orders() == before,
              "另一种测算 dry-run 不占单号、不动已支余额")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 5. 底线与同意金额的硬约束
def test_reserve_and_consent_caps():
    service, tmp = fresh()
    try:
        _, ledger, roll = build_baseline(service)
        book = service.open_book(ledger["ledger_id"], roll["roll_id"])
        # 试图转走 D2 的 2（其只同意 1）：必须拒
        over_consent = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"},
                          {"funder": "D2", "amount": "2"}],
            "own_fund_inputs": [],
        }]
        try:
            service.issue_order(book["book_id"], over_consent)
            raise AssertionError("超过同意金额的转用必须拒")
        except OrderRejectedError as exc:
            check(any("同意金额" in d for d in exc.errors), "拒绝明细指出同意金额上限")
        # 试图把旧款打到低于底线 1：转 4 必拒
        under_reserve = [{
            "case_id": "CX", "account": "X",
            "redirects": [{"funder": "D1", "amount": "2"},
                          {"funder": "D2", "amount": "1"},
                          {"funder": "D1", "amount": "1"}],
            "own_fund_inputs": [],
        }]
        try:
            service.issue_order(book["book_id"], under_reserve)
            raise AssertionError("突破旧用途底线必须拒")
        except OrderRejectedError:
            pass
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 6. 变体一：D1 留 X 比例改 1/4（3/4 转 Y）
def test_variant_ratio_quarter_rejects_x():
    service, tmp = fresh()
    try:
        # 题面：D1 给 X 的比例 1/2 -> 1/4，即留 X 1/4、余下 3/4 给 Y
        _, ledger, roll = build_baseline(service, ratio_d1_x="3/4")
        # D1：X 留 4·1/4=1、Y 收 3；D2：X 留 2。X 合计留用 3，同意 D1 1 + D2 1 = 2
        check(ledger["retained_by_source"]["D1"] == {"X": "1", "Y": "3"},
              "留 X 1/4：D1 留 X=1、Y=3")
        cases = {c["case_id"]: c for c in roll["cases"]}
        check(not cases["CX"]["approved"] and cases["CX"]["yes_share"] == "2/3",
              "X 仅获 2/3 < 3/4 门槛而否决（题面结论）")
        check(cases["CY"]["approved"], "Y 案 D1 3/3 仍通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 7. 变体二：委托证额改 0
def test_variant_zero_credential_rejects_x():
    service, tmp = fresh()
    try:
        _, _, roll = build_baseline(service, cred_cap="0")
        cases = {c["case_id"]: c for c in roll["cases"]}
        check(not cases["CX"]["approved"] and cases["CX"]["yes_share"] == "1/2",
              "证额 0：D2 全弃权，X 仅 D1 的 2/4=1/2 而否决")
        check(cases["CY"]["approved"], "Y 案 D1 2/2 仍通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 8. 变体三：X 新项目要 5，最多筹 4
def test_variant_need_five_infeasible():
    service, tmp = fresh()
    try:
        _, ledger, roll = build_baseline(service, x_need="5")
        cases = {c["case_id"]: c for c in roll["cases"]}
        check(cases["CX"]["approved"], "投票照过（门槛只看留用份额，与金额无关）")
        book = service.open_book(ledger["ledger_id"], roll["roll_id"])
        result = service.check_feasible(book["book_id"])
        check(not result["feasible"], "need=5 测算不可行")
        short = result["infeasible_cases"][0]
        check(short["case_id"] == "CX" and short["shortfall"] == "1",
              "X 最多筹 4（原款可转 3 + 自有款 1），缺口 1")
        try:
            service.close_book(book["book_id"])
            raise AssertionError("未兑现批次必须阻止批签收口")
        except AllocationInfeasibleError as exc:
            check("CX" in exc.errors, "收口指出不能同时兑现的项目")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 9. 受困来源：封闭循环资金无法结清
def test_trapped_source_in_closed_cycle():
    service, tmp = fresh()
    try:
        session = {
            "session_id": "STRAP",
            "name": "封闭循环受困场",
            "accounts": ["A", "B", "C"],
            "funders": ["D1", "D2"],
            "injections": [
                {"funder": "D1", "account": "A", "amount": "3"},
                {"funder": "D2", "account": "C", "amount": "2"},
            ],
            # A、B 全额互转：不外拨、不留用的封闭循环；C 全额留用
            "transfers": [
                {"from": "A", "to": "B", "ratio": "1"},
                {"from": "B", "to": "A", "ratio": "1"},
            ],
            "agenda": [],
            "signatures": [],
            "delegations": [],
            "credentials": [],
            "own_funds": [],
        }
        registered = service.register_session(session)
        ledger = service.build_ledger(registered["session_id"])
        check(not ledger["settled"], "存在受困来源，去向册不得结清")
        check(ledger["trap_components"] == [["A", "B"]], "A/B 被识别为封闭空转 SCC")
        trapped = {t["funder"]: t for t in ledger["trapped_sources"]}
        check("D1" in trapped and trapped["D1"]["trapped_accounts"] == ["A", "B"],
              "D1 的钱确有进入封闭循环，指出受困账户 A、B，不能结清")
        check("D2" not in trapped, "D2 资金留在 C，不受空转组牵连")
        check(ledger["retained_by_source"]["D2"] == {"A": "0", "B": "0", "C": "2"},
              "D2 照常结清留 C=2")
        try:
            service.build_rollcall(ledger["ledger_id"])
            raise AssertionError("未结清去向册不得进入点名")
        except LedgerNotSettledError as exc:
            check(exc.errors[0]["trapped_accounts"] == ["A", "B"],
                  "阻断信息携带受困账户")
        check(service.verify_ledger(ledger["ledger_id"])["ok"],
              "受困去向册的矛盾证书与受困判定独立验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 10. 无来款空转组不牵连其他来源
def test_idle_cycle_does_not_block():
    service, tmp = fresh()
    try:
        session = {
            "session_id": "SIDLE",
            "name": "无来款空转场",
            "accounts": ["A", "B", "C"],
            "funders": ["D1"],
            "injections": [{"funder": "D1", "account": "C", "amount": "5"}],
            "transfers": [
                {"from": "A", "to": "B", "ratio": "1"},
                {"from": "B", "to": "A", "ratio": "1"},
            ],
            "agenda": [],
            "signatures": [],
            "delegations": [],
            "credentials": [],
            "own_funds": [],
        }
        registered = service.register_session(session)
        ledger = service.build_ledger(registered["session_id"])
        check(ledger["settled"], "空转组无来款，本场唯一来源照常结清")
        check(ledger["trap_components"] == [["A", "B"]], "空转 SCC 仍被显式登记")
        check(set(ledger["zero_retention_accounts"]) >= {"A", "B"},
              "A、B 零留用显式标注，不牵连 C")
        check(ledger["retained_by_source"]["D1"]["C"] == "5", "D1 全额留 C=5")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 11. 输入校验：比例和/浮点/规模
def test_session_validation():
    service, tmp = fresh()
    try:
        bad = base_session()
        # 同一转出方对 D1 再挂一条转出边，逐来源有效比例和超过 1
        bad["transfers"].append(
            {"from": "X", "to": "Y", "ratio": "0", "ratios": {"D1": "3/4"}})
        try:
            service.register_session(bad)
            raise AssertionError("逐来源比例和超 1 必须拒绝")
        except AllocationValidationError:
            pass
        bad_float = base_session()
        bad_float["injections"][0]["amount"] = 0.5
        try:
            service.register_session(bad_float)
            raise AssertionError("float 必须拒绝")
        except AllocationValidationError:
            pass
        bad_inject = base_session()
        bad_inject["injections"] = [
            {"funder": "D1", "account": "X", "amount": "0"},
            {"funder": "D2", "account": "X", "amount": "2"},
        ]
        try:
            service.register_session(bad_inject)
            raise AssertionError("出资户注入合计为 0 必须拒绝")
        except AllocationValidationError:
            pass
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 12. 多圈绕环只留用一次
def test_multi_loop_retained_once():
    service, tmp = fresh()
    try:
        session = {
            "session_id": "SLOOP",
            "name": "多圈绕环场",
            "accounts": ["A", "B"],
            "funders": ["D1"],
            "injections": [{"funder": "D1", "account": "A", "amount": "8"}],
            # 每站各留 1/2、转 1/2：几何级数收敛，两站各留 8（总和=注入 8）
            "transfers": [
                {"from": "A", "to": "B", "ratio": "1/2"},
                {"from": "B", "to": "A", "ratio": "1/2"},
            ],
            "agenda": [],
            "signatures": [],
            "delegations": [],
            "credentials": [],
            "own_funds": [],
        }
        registered = service.register_session(session)
        ledger = service.build_ledger(registered["session_id"])
        check(ledger["settled"], "多圈绕环系统唯一可解并结清")
        # 手算：T_A = 8 + T_B/2；T_B = T_A/2 => T_A=32/3、T_B=16/3，
        # 各站留用一半：A=16/3、B=8/3，合计 8 恰等于注入（绕多圈只留用一次）
        check(ledger["retained_by_source"]["D1"] == {"A": "16/3", "B": "8/3"},
              "多圈绕环留用 A=16/3、B=8/3，合计等于注入 8（只留用一次）")
        total = sum(Fraction(v) for v in ledger["retained_by_source"]["D1"].values())
        check(total == 8, "留用合计=注入合计，资金无外泄也无重复留用")
        check(service.verify_ledger(ledger["ledger_id"])["ok"],
              "多圈绕环去向册独立验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 13. 多跳委托链：反对也扣证、超范围弃权
def test_multihop_delegation_and_no_vote_deduction():
    service, tmp = fresh()
    try:
        session = {
            "session_id": "SMULTI",
            "name": "多跳委托场",
            "accounts": ["PA", "PB", "PC"],
            "funders": ["D1", "D2", "D3"],
            "injections": [
                {"funder": "D1", "account": "PA", "amount": "4"},
                {"funder": "D2", "account": "PA", "amount": "4"},
                {"funder": "D3", "account": "PA", "amount": "4"},
                {"funder": "D1", "account": "PB", "amount": "4"},
                {"funder": "D2", "account": "PB", "amount": "4"},
                {"funder": "D3", "account": "PB", "amount": "4"},
                {"funder": "D1", "account": "PC", "amount": "4"},
                {"funder": "D2", "account": "PC", "amount": "4"},
                {"funder": "D3", "account": "PC", "amount": "4"},
            ],
            "transfers": [],
            "agenda": [
                {"case_id": "CA", "account": "PA", "need": "1", "reserve": "0",
                 "threshold": "1/2"},
                {"case_id": "CB", "account": "PB", "need": "1", "reserve": "0",
                 "threshold": "1/2"},
                {"case_id": "CC", "account": "PC", "need": "1", "reserve": "0",
                 "threshold": "1/2"},
            ],
            # D3 -> D2 -> D1，D1 对三案签字（CA/CB 赞成、CC 反对）
            "signatures": [
                {"funder": "D1", "case_id": "CA", "vote": "yes"},
                {"funder": "D1", "case_id": "CB", "vote": "yes"},
                {"funder": "D1", "case_id": "CC", "vote": "no"},
            ],
            "delegations": [
                {"funder": "D3", "delegate": "D2"},
                {"funder": "D2", "delegate": "D1"},
            ],
            "credentials": [
                # D2 的证只能签 CA/CC（不含 CB），全场证额 5；
                # D3 的证只能签 CA，全场证额 1
                {"funder": "D2", "case_scope": ["CA", "CC"], "cap": "5"},
                {"funder": "D3", "case_scope": ["CA"], "cap": "1"},
            ],
            "own_funds": [],
        }
        registered = service.register_session(session)
        ledger = service.build_ledger(registered["session_id"])
        check(ledger["settled"], "多跳场结清，每户留 P=4")
        roll = service.build_rollcall(ledger["ledger_id"])
        cases = {c["case_id"]: c for c in roll["cases"]}
        rec = lambda cid, f: next(r for r in cases[cid]["records"] if r["funder"] == f)

        # CA：D2、D3 都沿 D3->D2->D1 链找到 D1 的赞成
        check(rec("CA", "D3")["mode"] == "delegated"
              and rec("CA", "D3")["signer"] == "D1"
              and rec("CA", "D3")["signed"] == "1"
              and rec("CA", "D3")["abstained"] == "3",
              "多跳链 D3 证额 1：权益 4 只交签 1、弃权 3")
        check(rec("CA", "D2")["signed"] == "4", "D2 证额充足，权益 4 全额交赞成签")
        # CB：D2、D3 的证都不含 CB -> 全弃权；只有 D1 本人 4 票赞成
        check(rec("CB", "D3")["mode"] == "abstain"
              and "不可签本案" in rec("CB", "D3")["reason"],
              "超范围：D3 在 CB 全弃权")
        check(rec("CB", "D2")["mode"] == "abstain",
              "D2 的证不可签 CB，全弃权（不消耗证额）")
        check(cases["CB"]["yes_amount"] == "4", "CB 仅 D1 本人 4 票")
        # CC：D1 反对；D2 沿链交反对，证额 CA 后只剩 1，故只签反对 1、弃权 3；
        # 否决案照样扣证。D3 证不含 CC，全弃权。
        check(rec("CC", "D2")["vote"] == "no" and rec("CC", "D2")["signed"] == "1"
              and rec("CC", "D2")["abstained"] == "3",
              "反对签字受证额封顶：D2 只交反对 1，否决案也扣证")
        check(rec("CC", "D3")["mode"] == "abstain", "D3 证不含 CC，弃权不扣")
        audit_d2 = roll["credential_audit"]["D2"]
        check(audit_d2["total_deducted"] == "5"
              and audit_d2["remaining_after_session"] == "0",
              "D2 证额 5：CA 扣 4 后剩 1，CC 再扣 1 用尽（两案共用证额）")
        audit_d3 = roll["credential_audit"]["D3"]
        check(audit_d3["total_deducted"] == "1"
              and audit_d3["remaining_after_session"] == "0",
              "D3 证额 1 只在 CA 扣过，CB/CC 超范围不扣")
        check(service.verify_rollcall(ledger["ledger_id"], roll["roll_id"])["ok"],
              "多跳委托点名册独立验证通过")
    finally:
        shutil.rmtree(tmp)


# --------------------------------------------- 14. 自有款范围受限 + 多案共用余额
def test_own_fund_scope_and_shared_balance():
    service, tmp = fresh()
    try:
        session = {
            "session_id": "SFUND",
            "name": "自有款范围场",
            "accounts": ["P", "Q"],
            "funders": ["D1"],
            "injections": [
                {"funder": "D1", "account": "P", "amount": "10"},
                {"funder": "D1", "account": "Q", "amount": "10"},
            ],
            "transfers": [],
            "agenda": [
                # P 案：D1 不签字（无同意原款可转），需 3，只能靠自有款
                {"case_id": "CP", "account": "P", "need": "3", "reserve": "10",
                 "threshold": "0"},
                # Q 案：D1 赞成，但底线 10 锁死原款，需 2 也只能靠自有款
                {"case_id": "CQ", "account": "Q", "need": "2", "reserve": "10",
                 "threshold": "0"},
            ],
            "signatures": [{"funder": "D1", "case_id": "CQ", "vote": "yes"}],
            "delegations": [],
            "credentials": [],
            # F1 只能贴 P，余额 3；F2 只能贴 Q，余额 1（Q 需要 2，不足）
            "own_funds": [
                {"fund_id": "F1", "balance": "3", "case_scope": ["CP"]},
                {"fund_id": "F2", "balance": "1", "case_scope": ["CQ"]},
            ],
        }
        registered = service.register_session(session)
        ledger = service.build_ledger(registered["session_id"])
        roll = service.build_rollcall(ledger["ledger_id"])
        check(set(roll["approved_cases"]) == {"CP", "CQ"}, "门槛 0：两案都获批")
        book = service.open_book(ledger["ledger_id"], roll["roll_id"])
        result = service.check_feasible(book["book_id"])
        check(not result["feasible"], "F2 余额/范围不足，整体不可同时兑现")
        short = {c["case_id"]: c for c in result["infeasible_cases"]}
        check("CQ" in short and short["CQ"]["shortfall"] == "1",
              "Q 缺口 1；F1 虽有 3 但不允许贴 Q，范围隔离生效")

        # 新开一场把 F2 补足 2：两案可同时兑现，多案共用各自余额
        session2 = dict(session)
        session2["session_id"] = "SFUND2"
        session2["own_funds"] = [
            {"fund_id": "F1", "balance": "3", "case_scope": ["CP"]},
            {"fund_id": "F2", "balance": "2", "case_scope": ["CQ"]},
        ]
        service.register_session(session2)
        ledger2 = service.build_ledger("SFUND2")
        roll2 = service.build_rollcall(ledger2["ledger_id"])
        book2 = service.open_book(ledger2["ledger_id"], roll2["roll_id"])
        result2 = service.check_feasible(book2["book_id"])
        check(result2["feasible"], "F2 补足后两案同时可行")
        proj = result2["projected_state"]
        check(proj["own_fund_balances"] == {"F1": "0", "F2": "0"},
              "自有款按范围分别贴入，余数各自可查")
        check(proj["original_balances"]["D1"] == {"P": "10", "Q": "10"},
              "原款被底线锁死，分文未转")
    finally:
        shutil.rmtree(tmp)


def run_all():
    tests = [
        test_ledger_hand_values,
        test_rollcall_threshold_and_credentials,
        test_two_legal_payment_books_reopen,
        test_order_stable_id_rules,
        test_reserve_and_consent_caps,
        test_variant_ratio_quarter_rejects_x,
        test_variant_zero_credential_rejects_x,
        test_variant_need_five_infeasible,
        test_trapped_source_in_closed_cycle,
        test_idle_cycle_does_not_block,
        test_session_validation,
        test_multi_loop_retained_once,
        test_multihop_delegation_and_no_vote_deduction,
        test_own_fund_scope_and_shared_balance,
    ]
    for test in tests:
        test()
        print(f"  ✓ {test.__name__}")
    print(f"\n代拨子系统全部 {len(PASS)} 项断言通过。")


if __name__ == "__main__":
    run_all()
