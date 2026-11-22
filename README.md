# 精确预算循环引用解算系统

纯 Python 3.11 标准库实现（无第三方依赖、无浮点、无 `eval`、不使用 Excel 缓存值）。
模型格要么是精确有理数输入，要么是“常数 + Σ 有理系数 × 另一格”的线性公式；
跨模型引用使用永久 `model_id` + `cell_id`。系统显式禁止：两个未知格相乘、
任意代码执行、float 输入。循环引用被当作线性方程组的正常情形处理——可能唯一
可解、冗余但有自由变量、或互相矛盾。

## 一键复跑

```bash
./run_tests.sh
# 或分步
python3 -m tests.test_service   # 51 项断言：手算小例 + 六类规定场景 + 重开
python3 -m tests.test_daily     # 48 项断言：分日回款 + 现金柜 + 三种情形 + 补款旧账
python3 -m tests.test_scale     # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli       # CLI 子进程端到端
python3 -m tests.demo_daily     # 分日回款 / 现金柜现场演示
```

## 架构与数据流

```
JSON 模型 ──put-model──▶ 目录 Catalog（修订/快照，文件持久化、可重开）
                              │ snapshot（递归钉住外部输入版本）
                              ▼
                       编译器 Compiler
              引用核对 → 规范化线性方程 → 依赖图 → 迭代 Tarjan SCC
                              │ 自包含编译产物（带内容哈希、落盘）
                              ▼
                       解算器 SolverService
       按输出依赖闭包 + SCC 拓扑分块精确 RREF（Fraction，带行溯源）
              内容寻址缓存（随服务落盘）→ 三段式解算报告
                              │
                              ▼
              验证器 Verifier（独立从快照重建方程/图，独立分块消元）
       确定值代回全部原方程；参数表达式多组实例化；矛盾 λ 组合重算
```

### 模块

| 文件 | 职责 |
| --- | --- |
| `budget_solver/rationals.py` | 精确有理数边界：拒绝 float、分子分母 ≤12 位、规范分数串 |
| `budget_solver/catalog.py` | 模型目录：创建/修订/固定快照；永久 id 与改名分离；整次修订原子拒绝 |
| `budget_solver/compiler.py` | 快照作用域展开、方程规范化（含自环系数 `1-k`）、依赖图、迭代 Tarjan SCC；声明 `horizon` 时按日展开、跨日滞后单列 |
| `budget_solver/solver.py` | SCC 分块精确 RREF、跨块自由变量传播、矛盾证书、内容缓存、报告发布；分日报告附逐日产品与问题来源 |
| `budget_solver/verifier.py` | 独立验证路径：重建事实、独立分块求解、代回/证书/哈希核对；分日产物独立重放滞后与派生的外部入账扰动 |
| `budget_solver/cashier.py` | 现金柜：请付日历 + 短存单报价的精确投资排程（整数放大穷举）、配置与账本持久化 |
| `budget_solver/service.py` | 可重开门面：目录面/编译面/解算面/验证面 + 分日回款/现金柜面 |
| `budget_solver/cli.py` | `put-model` `snapshot` `compile` `solve` `query` `explain` `verify-*` `daily-collections` `impact` `put-cashier` `plan-cashier` `get-ledger` |

## 关键设计决策

### 1. 目录、快照与永久身份

* `model_id` 永久不变；`name` 是可改显示名，改名只产生新修订，不动身份。
* 每次 `put-model` 在落盘前完成全部引用核对：目标模型/格不存在、修订会让别的
  模型的入向引用悬挂，都会抛 `ReferenceRejectedError` 且**不写任何文件**。
* 快照不可变，包含完整公式、格哈希和每个外部模型钉住的快照 id；递归固定保证
  跨模型输入版本可复现。外部模型改名/更正后重新固定下游时，会钉住外部新 HEAD，
  旧快照与旧报告原样保留。

### 2. 编译器：实际依赖与强连通分量

* 输入格方程退化为 `x = value`；公式格 `x = c + Σ kᵢrᵢ` 规范为
  `x - Σ kᵢrᵢ = c`。自引用合法，自指系数与单位项**相加**（`1-k`），回归测试
  `test_self_reference_cell` 锁定该行为。
* 依赖图只保留真实引用边；迭代 Tarjan（无递归深度限制）输出拓扑序 SCC，
  自环单格也标记为循环块。

### 3. 解算器：分块精确消元（题面允许二选一，这里选依赖分解 + 循环块求解）

* 对每个所选输出取实际依赖闭包，按 SCC 拓扑逐块求解；块内做带**参数列**和
  **行溯源**的精确分数 RREF。
* 上游唯一确定的格直接代入常数；上游未确定的格成为参数并沿 DAG 向下游传播，
  因此“系统有自由度但某输出被唯一确定”能正确分类（如 `t = u - v + 2/7` 恒为
  `2/7`，而 `u=v=p` 保留为参数）。
* 块内出现 `0 = 非零常数` 即无解：溯源行给出原方程的整数 λ 组合作为矛盾证书，
  同时保留参数残差列。绝不把“不收敛”当作无解——精确消元不存在收敛问题，也
  不会返回上一轮数值。
* 闭包按 `(变量, 格哈希)` 集合做 SHA-256 内容寻址缓存：输入修订/系数变更改变
  格哈希 → 指纹变化 → 受影响闭包重算，未受影响闭包命中；缓存与报告随数据目录
  持久化，重开服务后继续生效。旧报告永不重写。

**算法权衡（实测）**：早期对整个闭包做稠密 RREF，300 格规模下分数膨胀到
4300+ 位整数、单次解算约 12 秒；改 SCC 分块后块矩阵有界（循环块 4 格为主），
同规模解算约 0.6 秒，确定值最大约 1 万位（输入仍严格 12 位，增长只发生在消元
过程中）。代价是需要显式维护跨块参数表达式与参数残差列。

### 4. 三段式发布与可核查性

报告把结果分成 `determined`（规范分数）、`undetermined_parameters`
（常数 + 命名参数 `p_<model>::<cell>` 的表达式）、`contradictions`
（λ 组合证书 + 来源方程）三段，并固定目录快照描述、作用域快照、解算配置与
全部内容哈希。

### 5. 独立验证（不是同一套代码自证）

`verify-compile` 与 `verify-solve` 直接从目录快照里的原始公式重建方程和引用
图，用验证器自带的独立分块组装与 RREF 解释复算：

* 编译产物：变量集、逐条方程系数/右端、引用边、SCC 循环标记、哈希；
* 确定值：赋入闭包内全部原方程，残差必须全为 0；
* 参数表达式：6 组参数实例化符号代回，目标方程恒等成立；
* 矛盾证书：按报告 λ 重算原方程组合，左端恒零、右端非零且与证书一致。

## 手算案例（tests/scenarios.py，全部有独立断言）

| 案例 | 手算结论 |
| --- | --- |
| 两格唯一循环 `x=½+¼y; y=⅓+½x` | `x=y=2/3` |
| 冗余一致循环 `a=b=c` | `a,b,c` 自由；`r=a-b+5=5` 固定 |
| 矛盾循环 `a=b+1; b=a+1` | 相加得 `0=2`，输出失败并附来源 |
| 自由但输出固定 `u=v; t=u-v+2/7` | `u` 参数化，`t=2/7` 固定 |
| 跨模型改名 | 永久 id 不变；旧报告哈希/数值不变，新快照钉新 HEAD |
| 输入更正循环 `p=k+q/2; q=k-p/4` | `p=4k/3, q=2k/3`；k=6→8,4；k=12→16,8；旧报告保留 |
| 单格自环 `z=⅓+¼z` | `(¾)z=⅓ => z=4/9` |

## CLI 用法

```bash
python3 -m budget_solver.cli --data-root ./data put-model model.json
python3 -m budget_solver.cli --data-root ./data snapshot M_ID --label v1
python3 -m budget_solver.cli --data-root ./data compile s1
python3 -m budget_solver.cli --data-root ./data solve <artifact-hash-or-path>
python3 -m budget_solver.cli --data-root ./data query solve1 'M_ID::cell'
python3 -m budget_solver.cli --data-root ./data explain solve1 'M_ID::cell'
python3 -m budget_solver.cli --data-root ./data verify-compile <artifact>
python3 -m budget_solver.cli --data-root ./data verify-solve solve1

# 分日回款 + 现金柜
python3 -m budget_solver.cli --data-root ./data daily-collections solve1 --cell x
python3 -m budget_solver.cli --data-root ./data impact solve1 M_COLLECT in 1 --delta 1
python3 -m budget_solver.cli --data-root ./data put-cashier cashier.json
python3 -m budget_solver.cli --data-root ./data plan-cashier CASH_MAIN solve1
python3 -m budget_solver.cli --data-root ./data get-ledger ledger1
```

模型 JSON 示例：

```json
{
  "model_id": "M_UNIQUE",
  "name": "两格唯一循环",
  "cells": {
    "x": {"kind": "formula", "constant": "1/2",
          "terms": [{"coef": "1/4", "cell_id": "y"}]},
    "y": {"kind": "formula", "constant": "1/3",
          "terms": [{"coef": "1/2", "cell_id": "x"}]},
    "d": {"kind": "input", "value": "1000/3"}
  },
  "outputs": ["x", "y"]
}
```

跨模型 term 增加 `"model_id": "OTHER_ID"`。项形状本身（单个有理系数 × 单个格
引用）从结构上排除了“两个未知格相乘”。

## 分日回款产品（隔日按比例入账）

在原有快照/编译/解算路径上增加“按日展开”，不新建第二套账务系统：

* 顶层 `horizon` 声明日历天数（最多 20）；快照因此变为逐日快照。
* 输入格可带 `"daily": {"1": "8", "2": "0", ...}` 表示该日外部入账；未列出的日
  沿用快照的 `value`。
* term 可带 `"lag": 1`：引用“前一天”的同名格；未带（或 `0`）仍是当天。
  旧式快照（无 `horizon`/`lag`/`daily`）行为完全不变。
* `day0_seed` 给出第 1 日所需的前日垫底值（键可为 `CELL` 或 `MODEL_ID::CELL`），
  实际计算日从 1 起；第 1 日若引用前日而缺垫底值，编译直接报错并指出来源。
* **当天的环与隔日项分离**：`lag=1` 项被编译成指向“前一天变量名”的边并登记在
  `temporal_edges`，绝不并入当天 SCC；同一天的循环仍在当天联立求解。

```json
{
  "model_id": "M_COLLECT",
  "name": "分日回款",
  "horizon": 3,
  "day0_seed": {"x": "0"},
  "cells": {
    "in": {"kind": "input", "value": "0",
           "daily": {"1": "8", "2": "0", "3": "0"}},
    "x":  {"kind": "formula", "constant": "0",
           "terms": [{"coef": "1", "cell_id": "in"},
                     {"coef": "1/2", "cell_id": "x", "lag": 1}]}
  },
  "outputs": ["x"]
}
```

`daily-collections` 只把各日**唯一确定**的输出签为回款定数；参数化格给表达式与
自由参数名，矛盾格给 λ 证书与来源方程。`impact` 把指定外部入账某日 `+1`，
只改派生产物的右端（带 `perturbation` 标记与缓存盐），返回逐日变化；原快照与
原报告不动。不相关表的矛盾/自由只记入来源，不阻断选定回款格。补录早期入账后
重出一版，旧报告、旧账本与旧快照原样保留、可重开复读。

## 现金柜（请付日历 + 短存单）

`put-cashier` 接收 `horizon`、`opening_cash`、`mandatory`（每日必付）与至多 16
张 `deposits`（`issue_day` / `maturity_day` / `principal` / `payout`）。`plan-cashier`
把解算报告的逐日确定回款交给现金柜：

* 每日顺序：先收当日回款 + 当日到期存单本息，再付必付款，余额非负才可继续；
  随后用剩余现金购买当日发行的任意几张存单。
* 每张只能整张购买一次、不可中途变现；不允许向未来借钱。
* 目标为最大化最终实现收益总和；最多 16 张 → 穷举 2^16 个购买组合，内部用
  各金额分母的 LCM 放大成整数精确比较，选定后用 `Fraction` 重建逐日账本。
* 输出选中单号、逐日收支、最低日末余额；若所有组合都付不清，给出从哪一天起
  再也没有可行资金安排。相同回款日历可交给不同现金柜，账本分别落盘。

三种规定情形（`tests/test_daily.py` 与 `tests/demo_daily.py` 可复跑）：

| 情形 | 回款 | 选中 | 收益 |
| --- | --- | --- | --- |
| 基准 | 8 / 4 / 2 | 仅 B | 1 |
| 滞后系数改 1 | 8 / 8 / 8 | A | 2 |
| 基准回款、B 兑付额改 9 | 8 / 4 / 2 | 仅 B | 4 |

## 持久化布局

```
<data-root>/
  catalog/catalog.json              # 序列与 HEAD 指针；models/<id>/revs|snaps
  artifacts/<hash12>.json           # 不可变编译产物（含分日产物）
  reports/solveN.json + seq.json    # 不可变解算报告
  cache/index.json + closure-*.json # 内容寻址闭包缓存（可重开）
  cashiers/index.json + configs/<id>/<rev>.json  # 现金柜请付日历与报价（版本化）
  ledgers/ledgerN.json + seq.json   # 不可变投资安排账本（可重开复读）
```

所有 JSON 经临时文件 + `os.replace` 原子写入。
