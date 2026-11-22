# 精确预算循环引用解算系统

纯 Python 3.11 标准库实现（无第三方依赖、无浮点、无 `eval`、不使用 Excel 缓存值）。
模型格要么是精确有理数输入，要么是“常数 + Σ 有理系数 × 另一格”的线性公式；
跨模型引用使用永久 `model_id` + `cell_id`。系统显式禁止：两个未知格相乘、
任意代码执行、float 输入。循环引用被当作线性方程组的正常情形处理——可能唯一
可解、冗余但有自由变量、或互相矛盾。

在固定快照的解算链路之外，系统另提供两条可交付链路，复用同一份编译产物：

* **数量报价曲线**（`quoting.py`）：把一个真实输入格符号化为订量 q，由实际方程
  推出单价关于 q 的精确仿射关系，目录区分整域唯一 / 部分订量相容 / 同订量多单价
  / 完全矛盾，负价子段列为不可报价；
* **采购额度核对**（同模块）：在互不重叠覆盖查询域的整数回扣档上，用精确分数
  二分求二次净付的整数边界，输出既能唯一报价又能付款的极大连续整数段，报价域
  可达十亿件、回扣档至多 30 个，全程不枚举订量。

## 一键复跑

```bash
./run_tests.sh
# 或分步
python3 -m tests.test_service   # 48 项断言：手算小例 + 六类规定场景 + 重开
python3 -m tests.test_quoting   # 70 项断言：报价曲线四分类 + 预算核对 + 十亿件
python3 -m tests.test_scale     # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli       # CLI 子进程端到端（含报价/预算/独立验证）
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
| `budget_solver/compiler.py` | 快照作用域展开、方程规范化（含自环系数 `1-k`）、依赖图、迭代 Tarjan SCC |
| `budget_solver/solver.py` | SCC 分块精确 RREF、跨块自由变量传播、矛盾证书、内容缓存、报告发布 |
| `budget_solver/quoting.py` | 订量符号化报价曲线、四分类目录、二次整数边界预算核对、曲线/预算落盘 |
| `budget_solver/verifier.py` | 独立验证路径：重建事实、独立分块求解、代回/证书/哈希核对 |
| `budget_solver/quote_verifier.py` | 报价曲线与预算报告的独立验证：独立符号推导、λ 证书复算、边界核算 |
| `budget_solver/service.py` | 可重开门面：目录面/编译面/解算面/验证面/报价面/预算面 |
| `budget_solver/cli.py` | `put-model` `snapshot` `compile` `solve` `query` `explain` `verify-*` `quote-*` `budget-*` |

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

## 数量报价曲线（报价员视角）

报价员在**冻结作用域**（一份编译产物）里指定：

* `q_variable`：一个真实**输入格**作订量 q（公式格作订量会被拒绝）；
* `price_variable`：一个根**输出格**作单价；
* 整数报价域 `[lo, hi]`，`lo ≥ 1`、至多十亿个整件数。

### 符号化推导：系数固定，变化只进右端

订量输入方程原本是 `q = 点值`（题面要求把冻结点值设 0）。曲线构建时该方程被
符号参数取代——**系数矩阵原样保留，只有右端随 q 变化**，随后沿 SCC 拓扑做带
q 参数列的精确分块 RREF。单价闭包里的格因此被精确表达为 `常数 + q 系数·q
(+ 自由格参数)`。只把一次解出来的旧单价向前沿用，会把固定费率（q 系数为 0）
和联动费率混在一起；这里由实际方程推出整条 `p(q) = a + b·q`，闭包允许有自由
格，只要单价对同一 q 仍唯一。

### 目录四分类（每条曲线都交代可用订量与来源方程）

| 分类 | 判定 | 每订量状态 |
| --- | --- | --- |
| `unique_whole_domain` | 无零行约束、单价不含 q 以外自由参数 | 逐段 `quotable` / `negative_price`（负价不可报价） |
| `partial_consistency` | 循环块留下 `0 = c0 + c1·q`，相容集合至多一个整数点 | 该点 `quotable`/`negative_price`/`non_unique`，其余 `inconsistent` |
| `multi_price` | 无约束但单价表达式含自由格 | 全域 `non_unique` |
| `fully_contradictory` | 零行 `0 = 非零常数`，与 q 无关 | 全域 `inconsistent` |

约束与矛盾都附 λ 组合证书（来源方程变量 + 有理系数），曲线整体带内容哈希，
`source_equations` 列出闭包全部方程、系数、右端与格哈希。`quote-at` 可单独求
某个数量的单价，并回传 `scope_snapshots`——外部模型的**实际钉选版本**随曲线
冻结，单点查询原样带出。

### 题目主例（手算）

冻结 q=0，循环式 `x = 2 + q/4 + x/2` 推出 `x(q) = 4 + q/2`，报价域 1..8 整域
唯一且非负。价格表 1–3 件不返、4–8 件返 15、预算 12：

* 实付 `max(0, q·x(q) − 当档回扣)`；
* 可买极大连续段 **[1,2] 与 [4,4]**：第三件 33/2 = **16.5** 超预算，第四件
  24 − 15 = **9** 可付；
* 仅把 q 的系数改成 1/2：关系变 `x(q)=4+q`，原点单价仍为 4，但只剩 **[1,2]**；
* 恢复原模型、仅把返 15 起点改为 3：可买 **[1,4]**。两版编译产物均保留，重开
  数据目录后曲线、预算报告与外部钉选版本原样复读。

## 采购额度核对（采购员视角）

输入一份已发布曲线 + 预算规格：

```json
{
  "domain": [1, 8],
  "budget": "12",
  "tiers": [
    {"lo": 1, "hi": 3, "rebate": "0"},
    {"lo": 4, "hi": 8, "rebate": "15"}
  ]
}
```

档表必须互不重叠、无缝覆盖所查数量域（落在报价域内），至多 30 档，回扣与
预算非负。档内毛利 `g(q) = q·p(q) = b·q² + a·q` 是二次式，净付
`max(0, g(q) − R) ≤ B` 等价于 `g(q) ≤ R + B`。二次函数只有一个顶点：按顶点
切成两个单调半段，用**精确分数二分**求满足不等式的整数边界（无平方根、无
浮点、无逐点枚举），再跨档合并相邻段。

输出每个极大连续整数段附：

* 端点订量、件数、端点单价与端点净付；
* 紧邻未入段点的**原因**：`over_budget`（可报价但超预算）、`curve_negative_price`、
  `curve_non_unique`、`curve_inconsistent` 或 `outside_query_domain`，并带该点
  单价/净付/回扣，便于核查“能买四件为什么不能买三件”。

预算可能一次跨过回扣断点：每个 (可报价段 × 回扣档) 矩形独立求边界后统一合并，
既不枚举订量，也不假设单一全局分界。旧曲线可用新预算重算：每次核对产生新的
不可变预算报告，`curve_id` / `curve_content_hash` 指回原合同，旧报告永不重写。

### 部分订量相容的真实循环例

`a = b + 3`、`b = a − 2q + 1` 两式相加得 `2q = 4`：**只有 q=2 相容**，其余订量
矛盾；单价格 `c = a − b + 6` 在 q=2 唯一为 9。曲线分类 `partial_consistency`，
预算核对只可能输出单点段 [2,2]，紧邻点原因标注 `curve_inconsistent`。

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

# 数量报价曲线与采购额度核对
python3 -m budget_solver.cli --data-root ./data quote-curve <artifact> \
    'M_ID::q' 'M_ID::price' --lo 1 --hi 1000000000 --label 报价曲线
python3 -m budget_solver.cli --data-root ./data list-curves
python3 -m budget_solver.cli --data-root ./data get-curve curve1
python3 -m budget_solver.cli --data-root ./data quote-at curve1 37
python3 -m budget_solver.cli --data-root ./data budget-check curve1 rebate-spec.json
python3 -m budget_solver.cli --data-root ./data list-budget-reports
python3 -m budget_solver.cli --data-root ./data get-budget-report budget1
python3 -m budget_solver.cli --data-root ./data verify-curve curve1
python3 -m budget_solver.cli --data-root ./data verify-budget budget1
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

## 持久化布局

```
<data-root>/
  catalog/catalog.json              # 序列与 HEAD 指针；models/<id>/revs|snaps
  artifacts/<hash12>.json           # 不可变编译产物
  reports/solveN.json + seq.json    # 不可变解算报告
  cache/index.json + closure-*.json # 内容寻址闭包缓存（可重开）
  curves/curveN.json + seq.json     # 不可变报价曲线（含来源方程、证书、内容哈希）
  budgets/budgetN.json + seq.json   # 不可变预算核对报告（指回 curve 合同）
```

所有 JSON 经临时文件 + `os.replace` 原子写入。曲线与预算报告的序号 id 不参与
内容寻址；内容哈希按去掉 id 的键集计算，独立验证器按同一键集复算。
