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
python3 -m tests.test_service     # 51 项断言：手算小例 + 六类规定场景 + 重开
python3 -m tests.test_tickets     # 74 项断言：票面发行/校版/辨错/询证/回签状态机
python3 -m tests.test_scale       # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli         # 原 CLI 子进程端到端
python3 -m tests.demo_ticket_cli  # 票版/询证/带原单号回签的 CLI 端到端
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
| `budget_solver/verifier.py` | 独立验证路径：重建事实、独立分块求解、代回/证书/哈希核对 |
| `budget_solver/service.py` | 可重开门面：目录面/编译面/解算面/验证面 |
| `budget_solver/cli.py` | `put-model` `snapshot` `compile` `solve` `query` `explain` `verify-*` |
| `budget_solver/linspace.py` | 通用有理数仿射代数：带行溯源 Gauss-Jordan、仿射参数解、零空间/行空间判定 |
| `budget_solver/tickets.py` | 印务处：至多六个永久格发行票面仿射等式、校版员联合核查与内页补写 |
| `budget_solver/recovery.py` | 收单员：k≤2 容错复原枚举、唯一/必错/可能错辨错、合计固定性判断 |
| `budget_solver/inquiry.py` | 询证目录（至多四项）与套餐判别：同答不同款反例、经费/分馆两限规划 |
| `budget_solver/orders.py` | 收发处：询证订单状态机、回签三重核对、幂等收费、联合相容签账 |
| `budget_solver/ticket_verify.py` | 票版与签账回执的独立验证（独立重建消元，不信任发行产物） |

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

## 票面发行、容错辨错与询证签账

在不改动原模型、单输出 `query`、`explain` 的前提下，系统在编译产物之上再叠一层
“印务—收单—收发”业务。所有运算仍在有理数上，票版、订单、回执均内容寻址、落盘、
可重开。

### 6. 印务处发行票版（`tickets.py`）

从**指定冻结快照的编译产物**选至多六个永久格作为票面格：

1. 合并各票面格在实际钉选作用域中的方程依赖闭包，闭包内其余格为内页格；
2. 先整体求解闭包——联合闭包矛盾则**拒绝发版**，回原方程的整数 λ 组合证书；
3. 带行溯源消去内页格，只剩票面仿射等式 `Σ aᵢ·xᵢ = b`，内页枢轴行随票版保存；
4. 两条对偶性质由独立验证器（`ticket_verify.py`）复核：
   * 健全性：任一合法内页填写投影到票面后必然满足全部票面等式（发行行落在独立
     重建的投影行空间内，右端经 λ 组合重算一致）；
   * 完备性：发行等式的投影行空间与独立消元得到的行空间**秩相等**，因此任何通过
     票面的票面值都能补成同一闭包的完整解（自由内页/票面取 0 即基点）。

校版员 `check-face` 一次**联合**核查一组票面值：单栏各自未定时只允许共享同一组
联合自由参数，不能各挑各的；相容返回票面参数解与内页补写，矛盾返回票面等式/
观测栏的 λ 组合与非零残差。

### 7. 收单员容错辨错（`recovery.py`）

纸票给实际非空栏（空栏不算错）、容错 `k`（k≤2）与一个票面线性合计。系统枚举
至多 k 栏的误抄位置，把未误抄栏等于纸值作为观测方程与票面等式联合求解，按
基点/零空间规范化去重，得到全部相容复原子空间，并输出：

* `unique_faces`——在全部复原中都固定的栏（含空栏）；
* `must_error_filled` / `possibly_error_filled`——必错（每个复原都偏离纸值）与
  可能错的已填栏；
* 每个误抄集合下的具体复原（基点 + 自由方向 + 该子空间内合计）；
* 容错内无任何复原 → `rejected`，整票退回、不发询证；
* 合计在全部复原中固定 → `fixed` 并直接给 `booked_total`；否则 `ambiguous`。

### 8. 询证套餐与回签状态机（`inquiry.py` / `orders.py`）

本月询证目录至多四项，每项有名称、承办分馆、收费和一条票面线性算式。套餐判别
在有理数上做**耦合仿射系统**：取两个子空间拷贝 x、x′（同一子空间也取两份独立
自由参数），令套餐每项回答相等；若系统相容且合计差不恒为零，就产出“答数相同、
合计不同”的两张有效票反例，套餐不充分。规划在 `budget` 与 `max_branches` 两限内
枚举全部非空套餐，按少馆→少费排序；无可用套餐时阻塞并附反例。

订单（`orders.py`）绑定一张真正的票（票号 + 发行版哈希）：

* 问项在看答案以前一次 `dispatch` 确定，同一馆一批递送、按项分别计费，**只在
  首次发出时收费一次**；重复发单直接拒绝；
* 回签必须带票号、发行版、问项号并与实际发出的同项匹配——票号/发行版不符即便
  答数碰巧相容也拒收；同项重复回答幂等（不重复收费/入账），同项冲突回答置
  `contradicted`；
* 回答到齐后与每个 k 内容错复原做子空间求交：联合相容且合计唯一才 `booked`，
  给可核对的完整复原（票面残差全 0、误抄栏数、回签核对清单、回执哈希）；联合
  矛盾附 λ/子空间依据且不入账；只回部分则保留 `awaiting_replies`。

### 9. 持久化布局（新增）

```
<data-root>/
  tickets/<ticket_id>.json     # 票版：票面等式、内页枢轴、闭包原方程、issue_hash
  orders/ordN.json + seq.json  # 询证订单：套餐、回签、事件流、签账回执/矛盾依据
```

### 10. 新增 CLI 入口

```bash
python3 -m budget_solver.cli --data-root ./data issue-ticket <artifact> TK1 \
    --face M::A --face M::B --face M::C --label 三栏票版
python3 -m budget_solver.cli --data-root ./data verify-ticket TK1
python3 -m budget_solver.cli --data-root ./data check-face TK1 face.json
python3 -m budget_solver.cli --data-root ./data analyze-paper TK1 paper.json
python3 -m budget_solver.cli --data-root ./data plan-inquiries TK1 plan.json
python3 -m budget_solver.cli --data-root ./data open-order TK1 plan.json
python3 -m budget_solver.cli --data-root ./data dispatch-inquiries ord1 items.json
python3 -m budget_solver.cli --data-root ./data receive-reply ord1 reply.json
python3 -m budget_solver.cli --data-root ./data verify-booking ord1
```

`paper.json` 形如 `{"paper": {"M::A": "4"}, "total_form": {"terms": {"M::C": "1"}}, "k": 1}`；
`reply.json` 形如 `{"ticket_id": "TK1", "issue_hash": "sha256:...", "item_id": "Q1", "value": "21"}`。

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
```

所有 JSON 经临时文件 + `os.replace` 原子写入。
