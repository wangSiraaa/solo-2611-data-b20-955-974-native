"""代拨去向册：编译等式核、逐出资户解算与受困来源识别。

去向册是场次资金的唯一事实底稿，绑定：

* 冻结作用域（逐户/合计模型的快照 id 与编译产物哈希）；
* 每个出资户在每个受款户的最终留用精确金额（逐户模型解出，可回查来源）；
* 合计模型的总流入/总留用，以及逐户相加 = 合计的守恒核对；
* 受困来源：资金进入“不外拨且不留用”的封闭循环时，逐户系统出现
  ``0 = 非零`` 矛盾（编译器的 SCC 先圈出循环，解算器给矛盾证书），
  该来源不能结清；无来款的空转组只产生零参数解，不牵连其他来源。

只有本场列入的出资户全部结清，去向册才允许进入点名/批签。
"""

from fractions import Fraction

from ..compiler import tarjan_scc
from ..errors import LedgerNotSettledError
from ..rationals import qstr
from . import network
from .store import ArtifactStore

LEDGER_PREFIX = "ledger"


class LedgerService:
    def __init__(self, budget_service, store_root):
        self.budget = budget_service
        self.store = ArtifactStore(store_root, "代拨去向册", LEDGER_PREFIX,
                                   fingerprint_field="ledger_fingerprint")

    # ----------------------------------------------------------- 模型发布
    def _publish_model(self, payload):
        put = self.budget.put_model(payload)
        snap = self.budget.snapshot(payload["model_id"], label=payload["name"])
        artifact, _ = self.budget.compile(snap["snap_id"], label=payload["name"])
        report = self.budget.solve(artifact)
        return {
            "model_id": payload["model_id"],
            "rev_id": put["rev_id"],
            "snap_id": snap["snap_id"],
            "artifact_hash": artifact["artifact_hash"],
            "report_id": report["solve_id"],
            "report_hash": report["report_hash"],
            "artifact": artifact,
            "report": report,
        }

    @staticmethod
    def _report_values(report):
        values = {}
        for variable, result in report["outputs"].items():
            if result["status"] == "determined":
                values[variable] = Fraction(result["value"])
        return values

    # ------------------------------------------------- 封闭循环（空转组）识别
    @staticmethod
    def _trap_components(session):
        """找出对任一来源“不外拨且不留用”的封闭循环 SCC。

        不外拨：SCC 没有通向组外的正比例边（任一来源口径）；不留用：组内每个
        账户对该来源的有效留用比例 1−Σ来源转出比例 = 0。
        """
        adjacency = network.transfer_adjacency(session)
        sccs = tarjan_scc(sorted(adjacency), adjacency)
        traps = []
        for members in sccs:
            is_cyclic = len(members) > 1 or members[0] in adjacency.get(members[0], ())
            if not is_cyclic:
                continue
            member_set = set(members)
            no_out = all(nxt in member_set for m in members for nxt in adjacency.get(m, ()))
            no_keep_any = any(
                all(network.retention_ratio(session, m, funder) == 0 for m in members)
                for funder in session["funders"])
            if no_out and no_keep_any:
                traps.append(sorted(members))
        return traps

    @staticmethod
    def _reachable(session, start_accounts):
        adjacency = network.transfer_adjacency(session)
        seen = set(start_accounts)
        queue = list(start_accounts)
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for nxt in adjacency.get(node, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    queue.append(nxt)
        return seen

    def _source_outcome(self, session, funder, published):
        """逐出资户解算：返回 (状态, 留用明细, 流入明细, 受困信息)。"""
        entry = published[funder]
        report = entry["report"]
        inconsistent = [v for v, r in report["outputs"].items()
                        if r["status"] == "inconsistent"]
        values = self._report_values(report)
        retained, inflow = {}, {}
        for account in session["accounts"]:
            r_var = f"{entry['model_id']}::{network.cell_retained_source(funder, account)}"
            t_var = f"{entry['model_id']}::{network.cell_source_inflow(funder, account)}"
            retained[account] = values.get(r_var)
            inflow[account] = values.get(t_var)

        if not inconsistent:
            return "settled", retained, inflow, None

        # 有矛盾：找出该来源的钱实际能到达的封闭空转组，即受困账户
        starts = network.injection_accounts(session, funder)
        reachable = self._reachable(session, starts)
        trapped_accounts = []
        for trap in self._trap_components(session):
            if any(acc in reachable for acc in trap):
                trapped_accounts.extend(trap)
        trapped_accounts = sorted(set(trapped_accounts))
        first = report["outputs"][inconsistent[0]]
        cert = first.get("contradictions", [])
        info = {
            "funder": funder,
            "trapped_accounts": trapped_accounts,
            "inconsistent_outputs": sorted(inconsistent),
            "contradiction_certificates": cert,
            "report_id": entry["report_id"],
        }
        return "trapped", retained, inflow, info

    # --------------------------------------------------------------- 建册
    def build_ledger(self, session):
        sid = session["session_id"]
        # 1. 逐出资户模型：真实输入格 + 总流入格 + 逐户留用格，全部走编译入口
        source_published = {}
        source_outcomes = {}
        for funder in session["funders"]:
            source_published[funder] = self._publish_model(
                network.build_source_model(session, funder))
        # 2. 合计模型
        total_published = self._publish_model(network.build_total_model(session))
        total_report = total_published["report"]
        total_values = self._report_values(total_report)
        total_inconsistent = any(r["status"] == "inconsistent"
                                 for r in total_report["outputs"].values())

        traps = self._trap_components(session)
        retained_by_source = {}
        inflow_by_source = {}
        trapped_sources = []
        for funder in session["funders"]:
            status, retained, inflow, info = self._source_outcome(
                session, funder, source_published)
            retained_by_source[funder] = retained
            inflow_by_source[funder] = inflow
            if status == "trapped":
                trapped_sources.append(info)

        # 3. 守恒核对（仅对已结清来源与合计确定格）；受困时合计系统本身无解
        conservation = None
        if not trapped_sources and not total_inconsistent:
            checks = []
            for account in session["accounts"]:
                sum_r = sum(
                    (retained_by_source[f][account] or Fraction(0)
                     for f in session["funders"]),
                    Fraction(0))
                total_r = total_values.get(
                    f"{total_published['model_id']}::{network.cell_retained_total(account)}")
                total_t = total_values.get(
                    f"{total_published['model_id']}::{network.cell_total_inflow(account)}")
                checks.append({
                    "account": account,
                    "sum_of_sources_retained": qstr(sum_r),
                    "total_retained": qstr(total_r) if total_r is not None else None,
                    "total_inflow": qstr(total_t) if total_t is not None else None,
                    "conserved": total_r is not None and total_r == sum_r,
                })
            conservation = {
                "all_conserved": all(c["conserved"] for c in checks),
                "per_account": checks,
            }

        settled = not trapped_sources and not total_inconsistent
        # 零留用账户（空转组无来款等）显式登记，不阻断其他来源
        zero_retention_accounts = sorted(
            account for account in session["accounts"]
            if all((retained_by_source[f].get(account) or Fraction(0)) == 0
                   for f in session["funders"])
        )

        ledger = {
            "artifact_kind": "allocation_ledger",
            "schema_version": 1,
            "session_id": sid,
            "session": session,
            "settled": settled,
            "trap_components": traps,
            "trapped_sources": trapped_sources,
            "zero_retention_accounts": zero_retention_accounts,
            "source_models": {
                funder: {k: v for k, v in entry.items() if k not in ("artifact", "report")}
                for funder, entry in source_published.items()
            },
            "total_model": {k: v for k, v in total_published.items()
                            if k not in ("artifact", "report")},
            "retained_by_source": {
                funder: {a: (qstr(v) if v is not None else None)
                         for a, v in row.items()}
                for funder, row in retained_by_source.items()
            },
            "inflow_by_source": {
                funder: {a: (qstr(v) if v is not None else None)
                         for a, v in row.items()}
                for funder, row in inflow_by_source.items()
            },
            "total": {
                "inflow": {
                    a: (qstr(total_values.get(
                        f"{total_published['model_id']}::{network.cell_total_inflow(a)}"))
                        if total_values.get(
                            f"{total_published['model_id']}::{network.cell_total_inflow(a)}")
                        is not None else None)
                    for a in session["accounts"]
                },
                "retained": {
                    a: (qstr(total_values.get(
                        f"{total_published['model_id']}::{network.cell_retained_total(a)}"))
                        if total_values.get(
                            f"{total_published['model_id']}::{network.cell_retained_total(a)}")
                        is not None else None)
                    for a in session["accounts"]
                },
            },
            "conservation": conservation,
        }
        ledger_id = self.store.next_id()
        return self.store.save(ledger_id, ledger)

    def require_settled(self, ledger):
        if not ledger["settled"]:
            sources = [info["funder"] for info in ledger["trapped_sources"]]
            raise LedgerNotSettledError(
                "去向册存在不能结清的受困来源，本场不得进入点名/批签",
                [{"funder": f,
                  "trapped_accounts": next(
                      i["trapped_accounts"] for i in ledger["trapped_sources"]
                      if i["funder"] == f)}
                 for f in sources],
            )

    def get_ledger(self, ledger_id):
        return self.store.load(ledger_id)

    def list_ledgers(self):
        return self.store.list_ids()
