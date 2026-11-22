"""应用门面：把目录、编译器、解算器、验证器组装成可重开的数据服务。

外部客户的三个独立面：
  * 目录面 get/list/snapshot —— 不依赖编译与解算即可查询版本；
  * 编译面 compile / load_artifact —— 产物自包含、可独立检查引用与方程；
  * 解算面 solve / query / explain —— 真实消费编译产物与目录快照。
"""

import os
from fractions import Fraction

from .catalog import Catalog
from .cashier import CashierStore, LedgerStore, plan_cashier
from .compiler import Compiler, daily_var
from .errors import NotFoundError, ValidationError
from .rationals import parse_rational, qstr
from .solver import SolverService
from .verifier import verify_compile as _verify_compile
from .verifier import verify_solve as _verify_solve


class BudgetService:
    def __init__(self, data_root):
        self.data_root = os.path.abspath(data_root)
        self.catalog = Catalog(os.path.join(self.data_root, "catalog"))
        self.artifact_dir = os.path.join(self.data_root, "artifacts")
        self.report_dir = os.path.join(self.data_root, "reports")
        self.cache_dir = os.path.join(self.data_root, "cache")
        self.compiler = Compiler(self.catalog, self.artifact_dir)
        self.solver = SolverService(
            self.catalog, self.artifact_dir, self.report_dir, self.cache_dir
        )
        self.cashiers = CashierStore(os.path.join(self.data_root, "cashiers"))
        self.ledgers = LedgerStore(os.path.join(self.data_root, "ledgers"))

    # ------------------------------------------------------------- 目录面
    def put_model(self, payload):
        return self.catalog.put_model(payload)

    def list_models(self):
        return self.catalog.list_models()

    def get_model(self, model_id):
        return self.catalog.get_model_meta(model_id)

    def list_revisions(self, model_id):
        return self.catalog.list_revisions(model_id)

    def snapshot(self, model_id, label=None):
        return self.catalog.take_snapshot(model_id, label=label)

    def list_snapshots(self, model_id):
        return self.catalog.list_snapshots(model_id)

    def get_snapshot(self, snap_id):
        return self.catalog.get_snapshot(snap_id)

    def catalog_descriptor(self):
        return self.catalog.catalog_snapshot_descriptor()

    # ------------------------------------------------------------- 编译面
    def compile(self, root_snap_id, label=None):
        artifact, path = self.compiler.compile_snapshot(root_snap_id, label=label)
        return artifact, path

    def load_artifact(self, path_or_digest):
        if os.path.exists(path_or_digest):
            return Compiler.load_artifact(path_or_digest)
        digest = path_or_digest.split(":", 1)[-1]
        path = os.path.join(self.artifact_dir, f"{digest[:12]}.json")
        if not os.path.exists(path):
            raise NotFoundError(f"编译产物 {path_or_digest} 找不到")
        return Compiler.load_artifact(path)

    # ------------------------------------------------------------- 解算面
    def solve(self, artifact, solve_config=None):
        return self.solver.solve(artifact, solve_config)

    def query(self, solve_id, variable):
        return self.solver.query(solve_id, variable)

    def explain(self, solve_id, variable):
        return self.solver.explain(solve_id, variable)

    def get_report(self, solve_id):
        return self.solver.get_report(solve_id)

    def list_reports(self):
        if not os.path.isdir(self.report_dir):
            return []
        return sorted(
            f[:-5]
            for f in os.listdir(self.report_dir)
            if f.startswith("solve") and f.endswith(".json")
        )

    # ------------------------------------------------------------- 验证面
    def verify_compile(self, artifact):
        return _verify_compile(self.catalog, artifact)

    def verify_solve(self, solve_id):
        report = self.get_report(solve_id)
        artifact = self.load_artifact(report["artifact_hash"])
        return _verify_solve(self.catalog, artifact, report)

    # ------------------------------------------------------- 分日回款产品面
    def compile_daily(self, root_snap_id, label=None):
        """显式要求按日编译（根快照必须声明 horizon）。"""
        artifact, path = self.compiler.compile_daily_snapshot(
            root_snap_id, label=label
        )
        return artifact, path

    def daily_collections(self, report, source="root", cell_id=None):
        """从分日解算报告提取各日精确回款，并核对各日是否为唯一确定值。

        可选 ``cell_id`` 指定“选定回款格”；此时只依据该格判定，其他输出格的
        矛盾/自由不会阻断（问题仍随 ``unrelated_issues`` 返回，标明来源）。
        """
        if report.get("outputs") and "horizon" not in report:
            raise ValidationError(
                "该报告不是分日回款报告", ["report"]
            )
        horizon = report["horizon"]
        cells = report.get("daily_products") or {}
        if not cells:
            raise NotFoundError("分日报告中没有每日回款格")
        problems = []
        days_status = {}
        result = {}
        if source == "root":
            chosen = report["root_model_id"]
        else:
            if source not in cells:
                target_issues = report.get("daily_dependency_issues", {})
                raise ValidationError(
                    f"报告作用域内没有模型 {source}",
                    target_issues.get(source, [f"source={source}"]),
                )
            chosen = source
        if chosen not in cells:
            raise NotFoundError(f"模型 {chosen} 不在分日报告中")
        if cell_id is not None:
            if cell_id not in cells[chosen]:
                raise NotFoundError(f"模型 {chosen} 中没有回款格 {cell_id}")
            selected_cells = {cell_id: cells[chosen][cell_id]}
        else:
            declared = (report.get("model_outputs") or {}).get(chosen) or []
            if declared:
                selected_cells = {
                    c: cells[chosen][c] for c in declared if c in cells[chosen]
                }
            else:
                selected_cells = cells[chosen]
        # 其他输出格的问题只记录来源，不参与阻断判定
        unrelated = []
        for other_cell, by_day in sorted(cells[chosen].items()):
            if other_cell in selected_cells:
                continue
            for day in range(1, horizon + 1):
                info = by_day[str(day)]
                if info["status"] != "determined":
                    unrelated.append(
                        {
                            "cell_id": other_cell,
                            "day": day,
                            "status": info["status"],
                            "contradictions": info.get("contradictions"),
                        }
                    )
        for cell, by_day in sorted(selected_cells.items()):
            for day in range(1, horizon + 1):
                info = by_day[str(day)]
                days_status.setdefault(str(day), {})[cell] = info["status"]
                if info["status"] == "determined":
                    continue
                if info["status"] == "parametric":
                    problems.append(
                        {
                            "day": day,
                            "cell_id": cell,
                            "status": "undetermined",
                            "free_variables": info.get("free_variables", []),
                            "expression": info.get("expression"),
                        }
                    )
                else:
                    problems.append(
                        {
                            "day": day,
                            "cell_id": cell,
                            "status": "inconsistent",
                            "contradictions": info.get("contradictions"),
                            "source": info.get("source"),
                        }
                    )
        if problems:
            return {
                "source": chosen,
                "status": "undetermined" if all(
                    p["status"] == "undetermined" for p in problems
                ) else "contradiction",
                "collections": None,
                "days": days_status,
                "problems": problems,
                "unrelated_issues": unrelated,
            }
        if len(selected_cells) != 1:
            raise ValidationError(
                "模型有多个输出格，请用 cell_id 指定回款格",
                sorted(selected_cells),
            )
        (cell_id, by_day), = selected_cells.items()
        for day in range(1, horizon + 1):
            result[str(day)] = by_day[str(day)]["value"]
        return {
            "source": chosen,
            "cell_id": cell_id,
            "status": "ok",
            "collections": result,
            "days": days_status,
            "problems": [],
            "unrelated_issues": unrelated,
        }

    def collections_by_cell(self, report, model_id, cell_id):
        """某个具体回款格逐日状态（不要求它唯一，供诊断）。"""
        horizon = report["horizon"]
        by_day = (report.get("daily_products") or {}).get(model_id, {}).get(cell_id)
        if by_day is None:
            raise NotFoundError(f"报告中没有回款格 {model_id}::{cell_id}")
        return {str(d): by_day[str(d)] for d in range(1, horizon + 1)}

    def impact(self, report, model_id, cell_id, add_day, delta="1",
               output_model=None, output_cell=None):
        """把指定一天的某外部入账增加 delta，重解并报告各日回款的变化量。

        ``model_id::cell_id`` 是外部入账格；``output_model::output_cell`` 是
        要观察的回款格（缺省取根模型唯一的输出格）。公式与快照都不改，只在
        派生产物里把该日右端加 delta。
        """
        if add_day < 1 or add_day > report["horizon"]:
            raise ValidationError("add_day 超出日历", ["add_day"])
        artifact = self.load_artifact(report["artifact_hash"])
        vocab = report.get("model_vocabulary") or {}
        if model_id not in vocab:
            raise ValidationError(
                f"模型 {model_id} 不在报告作用域内", [f"model={model_id}"]
            )
        cell = vocab[model_id]["cells"].get(cell_id)
        if cell is None:
            raise ValidationError(
                f"模型 {model_id} 中没有格 {cell_id}", [f"cell={cell_id}"]
            )
        if cell["kind"] != "input":
            raise ValidationError(
                f"{model_id}::{cell_id} 不是外部入账格，不能施加扰动",
                [f"cell={cell_id}"],
            )
        output_model, output_cell = self._pick_output_cell(
            report, output_model, output_cell
        )
        perturbed = self._perturbed_artifact(
            artifact, model_id, cell_id, add_day, delta
        )
        new_report = self.solver.solve(perturbed)
        before_days = self._cell_days(report, output_model, output_cell)
        after_days = self._cell_days(new_report, output_model, output_cell)
        changed = []
        for day in range(1, report["horizon"] + 1):
            old = before_days.get(str(day))
            new = after_days.get(str(day))
            if old is None or new is None:
                continue
            if old["status"] != "determined" or new["status"] != "determined":
                changed.append(
                    {
                        "day": day,
                        "status": "undetermined",
                        "before_status": old["status"],
                        "after_status": new["status"],
                    }
                )
                continue
            diff = Fraction(new["value"]) - Fraction(old["value"])
            changed.append(
                {
                    "day": day,
                    "before": old["value"],
                    "after": new["value"],
                    "delta": qstr(diff),
                }
            )
        affected = [row["day"] for row in changed if row.get("delta") not in (None, "0")]
        return {
            "input": f"{model_id}::{cell_id}",
            "output": f"{output_model}::{output_cell}",
            "added_on_day": add_day,
            "delta": qstr(parse_rational(delta, field="delta")),
            "affected_days": affected,
            "per_day": changed,
            "perturbed_report_id": new_report["solve_id"],
        }

    def _pick_output_cell(self, report, model_id, cell_id):
        cells = report.get("daily_products") or {}
        model_id = model_id or report["root_model_id"]
        if model_id not in cells:
            raise NotFoundError(f"报告中没有模型 {model_id} 的逐日输出")
        if cell_id is None:
            declared = (report.get("model_outputs") or {}).get(model_id) or []
            candidates = [c for c in declared if c in cells[model_id]]
            if not candidates:
                candidates = list(cells[model_id])
            if len(candidates) != 1:
                raise ValidationError(
                    "模型有多个回款格，请指定 output_cell", candidates
                )
            return model_id, candidates[0]
        if cell_id not in cells[model_id]:
            raise NotFoundError(f"报告中没有回款格 {model_id}::{cell_id}")
        return model_id, cell_id

    def _cell_days(self, report, model_id, cell_id):
        by_day = (report.get("daily_products") or {}).get(model_id, {}).get(cell_id)
        if by_day is None:
            return {}
        return by_day

    def _perturbed_artifact(self, artifact, model_id, cell_id, day, delta):
        """复制产物并把某模型某输入格第 day 日的外部入账加上 delta。"""
        import copy

        delta = parse_rational(delta, field="delta")
        clone = copy.deepcopy(artifact)
        target = daily_var(model_id, cell_id, day)
        if target not in clone["equations"]:
            raise ValidationError(
                f"产物中没有 {target}", [target]
            )
        eq = clone["equations"][target]
        if clone["variables"][target]["kind"] != "input":
            raise ValidationError(f"{target} 不是输入格", [target])
        eq["rhs"] = qstr(Fraction(eq["rhs"]) + delta)
        # 记录派生扰动，供独立验证器在重建快照事实后重放同一右端修正。
        clone["perturbation"] = {
            "model_id": model_id,
            "cell_id": cell_id,
            "day": day,
            "delta": qstr(delta),
            "kind": "external_input_rhs_add",
        }
        # 同一天同一输入格的其他日不受影响；给产物加盐，使缓存指纹与原始
        # 产物区分开（原快照与格哈希不动）。
        clone["cache_salt"] = (
            f"rhs+{qstr(delta)}@{model_id}::{cell_id}@{day}"
        )
        # 扰动只改动右端；为保持内容寻址一致，重新计算哈希。
        from .util import content_hash

        clone["artifact_hash"] = content_hash(
            {k: v for k, v in clone.items() if k != "artifact_hash"}
        )
        from .util import write_json_atomic

        path = os.path.join(
            self.artifact_dir, f"{clone['artifact_hash'][7:19]}.json"
        )
        write_json_atomic(path, clone)
        return clone

    # --------------------------------------------------------- 现金柜面
    def put_cashier(self, payload):
        return self.cashiers.put(payload)

    def list_cashiers(self):
        return self.cashiers.list()

    def get_cashier(self, cashier_id, rev_id=None):
        return self.cashiers.get(cashier_id, rev_id=rev_id)

    def plan_cashier(self, cashier_id, collections, cashier_rev=None):
        config = self.cashiers.get(cashier_id, rev_id=cashier_rev)
        return plan_cashier(config, collections)

    def execute_cashier(self, cashier_id, solve_id, source="root",
                        cashier_rev=None, cell_id=None):
        """把解算报告的各日确定回款交给现金柜，出具并落盘投资安排。"""
        report = self.get_report(solve_id)
        extracted = self.daily_collections(report, source=source, cell_id=cell_id)
        if extracted["status"] != "ok":
            return {
                "feasible": False,
                "status": extracted["status"],
                "reason": "回款不是各日可确定的非负金额，现金柜拒收",
                "problems": extracted["problems"],
            }
        config = self.cashiers.get(cashier_id, rev_id=cashier_rev)
        if config["horizon"] != report["horizon"]:
            raise ValidationError(
                "现金柜日历与回款报告日历不一致",
                [f"cashier horizon={config['horizon']}",
                 f"report horizon={report['horizon']}"],
            )
        plan = plan_cashier(config, extracted["collections"])
        body = {
            "cashier_id": cashier_id,
            "cashier_rev_id": config["rev_id"],
            "cashier_hash": config["record_hash"],
            "solve_id": solve_id,
            "report_hash": report["report_hash"],
            "root_snap_id": report["root_snap_id"],
            "collections_source": source,
            "collections_cell": extracted.get("cell_id"),
            "collections": extracted["collections"],
            "unrelated_issues": extracted.get("unrelated_issues", []),
            "plan": plan,
        }
        return self.ledgers.publish(body)

    def get_ledger(self, ledger_id):
        return self.ledgers.get(ledger_id)

    def list_ledgers(self):
        return self.ledgers.list()
