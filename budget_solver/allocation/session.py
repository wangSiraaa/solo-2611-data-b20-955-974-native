"""代拨场次的 JSON 边界：结构校验与规范化（全程精确有理数）。

一场（session）固定下列事实：

* ``accounts``：至多 8 个代拨账户（受款户）；
* ``funders``：至多 6 个具名出资户，每个出资户多处注入的合计必须为正；
* ``injections``：出资户对账户的直接注入（真实输入格来源）；
* ``transfers``：账户间约定比例转拨，各转出方比例之和 ≤1，余数本站留用；
* ``agenda``：至多 6 项新用途案，按登记顺序即固定点名议程；
* ``signatures`` / ``delegations`` / ``credentials``：本人签字、唯一委托链、
  按原出资户登记的委托证（限定可签案与全场证额）；
* ``own_funds``：至多 6 个基金会自有款账户（真实余额、可贴项目范围）。

规范化后的负载把全部数值转成 :func:`budget_solver.rationals.qstr` 规范分数字符串，
后续模块只消费规范化结果。
"""

import re

from fractions import Fraction

from ..errors import AllocationValidationError
from ..rationals import parse_rational, qstr

MAX_ACCOUNTS = 8
MAX_FUNDERS = 6
MAX_CASES = 6
MAX_OWN_FUNDS = 6

_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-]{0,31}$")
_VOTES = {"yes", "no"}


def _require(cond, message, errors):
    if not cond:
        errors.append(message)


def _check_id(value, label, errors):
    if not isinstance(value, str) or not _ID_RE.match(value or ""):
        errors.append(f"{label} 必须是 1-32 位字母/数字/下划线/连字符标识：{value!r}")
        return False
    return True


def _nonneg(value, field, errors):
    try:
        frac = parse_rational(value, field=field)
    except Exception as exc:  # ValidationError 已是 BudgetError
        errors.append(str(exc))
        return None
    if frac < 0:
        errors.append(f"{field} 必须非负：{value!r}")
        return None
    return frac


def normalize_session(payload):
    """校验并归一化场次负载；任何结构/数值错误一次性抛出，不产生部分结果。"""
    errors = []
    if not isinstance(payload, dict):
        raise AllocationValidationError("场次负载必须是 JSON 对象", ["payload"])

    session_id = payload.get("session_id")
    _check_id(session_id, "session_id", errors)
    name = payload.get("name", session_id if isinstance(session_id, str) else "")
    if not isinstance(name, str):
        errors.append("name 必须是字符串")
        name = str(name)

    accounts = payload.get("accounts")
    funders = payload.get("funders")
    _require(isinstance(accounts, list) and 1 <= len(accounts) <= MAX_ACCOUNTS,
             f"accounts 必须是 1..{MAX_ACCOUNTS} 个账户的数组", errors)
    _require(isinstance(funders, list) and 1 <= len(funders) <= MAX_FUNDERS,
             f"funders 必须是 1..{MAX_FUNDERS} 个出资户的数组", errors)
    accounts = list(accounts) if isinstance(accounts, list) else []
    funders = list(funders) if isinstance(funders, list) else []
    for acc in accounts:
        _check_id(acc, "账户标识", errors)
    for funder in funders:
        _check_id(funder, "出资户标识", errors)
    if len(set(accounts)) != len(accounts):
        errors.append("accounts 含重复标识")
    if len(set(funders)) != len(funders):
        errors.append("funders 含重复标识")
    account_set, funder_set = set(accounts), set(funders)

    # ------------------------------------------------------------- 直接注入
    injections = []
    inject_total = {f: 0 for f in funders}
    for index, item in enumerate(payload.get("injections", [])):
        field = f"injections[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        funder, account = item.get("funder"), item.get("account")
        if funder not in funder_set:
            errors.append(f"{field}.funder {funder!r} 不在 funders 中")
        if account not in account_set:
            errors.append(f"{field}.account {account!r} 不在 accounts 中")
        amount = _nonneg(item.get("amount"), f"{field}.amount", errors)
        if amount is not None and funder in inject_total:
            inject_total[funder] += amount
        if amount is not None:
            injections.append(
                {"funder": funder, "account": account, "amount": qstr(amount)}
            )
    for funder, total in inject_total.items():
        _require(total > 0, f"出资户 {funder} 各注入合计必须为正（可为多处注入）", errors)

    # ------------------------------------------------------------- 比例转拨
    # 转拨合同按来源生效：ratio 是缺省比例，ratios 可逐出资户覆盖（同一转出户
    # 对不同来源的钱可约定不同转拨比例）。每个转出方对每个来源的比例和 ≤1。
    transfers = []
    ratio_sum = {a: 0 for a in accounts}
    source_ratio_sum = {(a, f): 0 for a in accounts for f in funders}
    for index, item in enumerate(payload.get("transfers", [])):
        field = f"transfers[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        src, dst = item.get("from"), item.get("to")
        if src not in account_set:
            errors.append(f"{field}.from {src!r} 不在 accounts 中")
        if dst not in account_set:
            errors.append(f"{field}.to {dst!r} 不在 accounts 中")
        if src is not None and src == dst:
            errors.append(f"{field} 不允许账户向自身转拨：{src}")
        ratio = _nonneg(item.get("ratio"), f"{field}.ratio", errors)
        per_source = {}
        raw_ratios = item.get("ratios", {})
        if raw_ratios and not isinstance(raw_ratios, dict):
            errors.append(f"{field}.ratios 必须是 出资户->比例 的对象")
            raw_ratios = {}
        for funder, raw_value in raw_ratios.items():
            if funder not in funder_set:
                errors.append(f"{field}.ratios 含未知出资户 {funder!r}")
                continue
            value = _nonneg(raw_value, f"{field}.ratios.{funder}", errors)
            if value is not None:
                if value > 1:
                    errors.append(f"{field}.ratios.{funder} 超过 1")
                per_source[funder] = qstr(value)
        if ratio is not None:
            if ratio > 1:
                errors.append(f"{field}.ratio 超过 1：{item.get('ratio')!r}")
            if src in ratio_sum:
                ratio_sum[src] += ratio
            # 逐来源有效比例：有覆盖用覆盖值，否则用缺省比例
            for funder in funders:
                effective = Fraction(per_source[funder]) if funder in per_source \
                    else ratio
                source_ratio_sum[(src, funder)] += effective
            transfers.append({
                "from": src, "to": dst, "ratio": qstr(ratio),
                "ratios": per_source,
            })
    for src, total in ratio_sum.items():
        _require(total <= 1, f"转出方 {src} 缺省比例之和 {total} 超过 1（余数本站留用）",
                 errors)
    for (src, funder), total in source_ratio_sum.items():
        _require(total <= 1,
                 f"转出方 {src} 对来源 {funder} 的有效比例之和 {total} 超过 1",
                 errors)

    # ---------------------------------------------------------------- 议程
    agenda_raw = payload.get("agenda", [])
    _require(isinstance(agenda_raw, list) and len(agenda_raw) <= MAX_CASES,
             f"agenda 至多 {MAX_CASES} 项且必须是数组", errors)
    agenda = []
    case_ids, case_accounts = set(), set()
    for index, item in enumerate(agenda_raw if isinstance(agenda_raw, list) else []):
        field = f"agenda[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        case_id = item.get("case_id")
        account = item.get("account")
        _check_id(case_id, f"{field}.case_id", errors)
        if account not in account_set:
            errors.append(f"{field}.account {account!r} 不在 accounts 中")
        if case_id in case_ids:
            errors.append(f"议程案号 {case_id} 重复")
        if account in case_accounts:
            errors.append(f"账户 {account} 申报了多项新用途（每户至多一项）")
        case_ids.add(case_id)
        case_accounts.add(account)
        need = _nonneg(item.get("need"), f"{field}.need", errors)
        reserve = _nonneg(item.get("reserve", 0), f"{field}.reserve", errors)
        threshold = _nonneg(item.get("threshold"), f"{field}.threshold", errors)
        if threshold is not None and threshold > 1:
            errors.append(f"{field}.threshold 是同意比例，必须 ≤ 1")
        purpose = item.get("purpose", "")
        if not isinstance(purpose, str):
            errors.append(f"{field}.purpose 必须是字符串")
            purpose = str(purpose)
        if need is not None and reserve is not None and threshold is not None:
            agenda.append(
                {
                    "case_id": case_id,
                    "account": account,
                    "purpose": purpose,
                    "need": qstr(need),
                    "reserve": qstr(reserve),
                    "threshold": qstr(threshold),
                    "agenda_index": index,
                }
            )

    # ---------------------------------------------------------------- 签字
    signatures = []
    for index, item in enumerate(payload.get("signatures", [])):
        field = f"signatures[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        funder, case_id, vote = item.get("funder"), item.get("case_id"), item.get("vote")
        if funder not in funder_set:
            errors.append(f"{field}.funder {funder!r} 不在 funders 中")
        if case_id not in case_ids:
            errors.append(f"{field}.case_id {case_id!r} 不在议程中")
        if vote not in _VOTES:
            errors.append(f"{field}.vote 必须是 yes 或 no：{vote!r}")
        signatures.append({"funder": funder, "case_id": case_id, "vote": vote})
    # 同一出资户对同一案至多一份本人签字
    sig_pairs = [(s["funder"], s["case_id"]) for s in signatures]
    if len(set(sig_pairs)) != len(sig_pairs):
        errors.append("同一出资户对同一案存在重复本人签字")

    # ---------------------------------------------------------------- 委托
    delegations = []
    delegate_of = {}
    for index, item in enumerate(payload.get("delegations", [])):
        field = f"delegations[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        funder, delegate = item.get("funder"), item.get("delegate")
        if funder not in funder_set:
            errors.append(f"{field}.funder {funder!r} 不在 funders 中")
        if delegate not in funder_set:
            errors.append(f"{field}.delegate {delegate!r} 不在 funders 中")
        if funder == delegate:
            errors.append(f"{field} 出资户不能委托本人：{funder}")
        if funder in delegate_of:
            errors.append(f"出资户 {funder} 存在多条委托（委托链必须唯一）")
        delegate_of[funder] = delegate
        delegations.append({"funder": funder, "delegate": delegate})
    # 委托图回环直接拒绝（回环在投票语义里另有“未遇签字弃权”处理，这里拒绝
    # 结构性回环，保证“唯一委托链”可被线性追溯）
    for start in delegate_of:
        seen = {start}
        cur = delegate_of.get(start)
        while cur is not None:
            if cur in seen:
                errors.append(f"委托链在 {cur} 处回环（出资户 {start} 的链不唯一收口）")
                break
            seen.add(cur)
            cur = delegate_of.get(cur)

    # ---------------------------------------------------------------- 委托证
    credentials = []
    cred_owner = set()
    for index, item in enumerate(payload.get("credentials", [])):
        field = f"credentials[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        funder = item.get("funder")
        if funder not in funder_set:
            errors.append(f"{field}.funder {funder!r} 不在 funders 中")
        scope = item.get("case_scope", [])
        if not isinstance(scope, list) or not scope:
            errors.append(f"{field}.case_scope 必须是非空案号数组")
            scope = []
        for case_id in scope:
            if case_id not in case_ids:
                errors.append(f"{field}.case_scope 含议程外案号 {case_id!r}")
        cap = _nonneg(item.get("cap"), f"{field}.cap", errors)
        if funder in cred_owner:
            errors.append(f"出资户 {funder} 登记了多张委托证；请合并证额与可签范围")
        cred_owner.add(funder)
        if cap is not None and scope:
            credentials.append(
                {
                    "funder": funder,
                    "case_scope": sorted(set(scope)),
                    "cap": qstr(cap),
                }
            )

    # ---------------------------------------------------------------- 自有款
    own_funds = []
    fund_ids = set()
    for index, item in enumerate(payload.get("own_funds", [])):
        field = f"own_funds[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{field} 必须是对象")
            continue
        fund_id = item.get("fund_id")
        _check_id(fund_id, f"{field}.fund_id", errors)
        if fund_id in fund_ids:
            errors.append(f"自有款账户 {fund_id} 重复")
        fund_ids.add(fund_id)
        balance = _nonneg(item.get("balance"), f"{field}.balance", errors)
        scope = item.get("case_scope", [])
        if not isinstance(scope, list) or not scope:
            errors.append(f"{field}.case_scope 必须是非空案号数组")
            scope = []
        for case_id in scope:
            if case_id not in case_ids:
                errors.append(f"{field}.case_scope 含议程外案号 {case_id!r}")
        if len(own_funds) >= MAX_OWN_FUNDS:
            errors.append(f"自有款账户至多 {MAX_OWN_FUNDS} 个")
        if balance is not None and scope:
            own_funds.append(
                {
                    "fund_id": fund_id,
                    "balance": qstr(balance),
                    "case_scope": sorted(set(scope)),
                }
            )

    if errors:
        raise AllocationValidationError("代拨场次校验失败，整次拒绝", sorted(set(errors)))

    return {
        "schema_version": 1,
        "session_id": session_id,
        "name": name,
        "accounts": accounts,
        "funders": funders,
        "injections": injections,
        "transfers": transfers,
        "agenda": agenda,
        "signatures": signatures,
        "delegations": delegations,
        "credentials": credentials,
        "own_funds": own_funds,
    }
