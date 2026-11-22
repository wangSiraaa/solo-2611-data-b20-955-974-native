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
python3 -m tests.test_service        # 51 项断言：手算小例 + 六类规定场景 + 重开
python3 -m tests.test_allocation     # 69 项断言：代拨去向册/点名册/拨款单全链
python3 -m tests.test_scale          # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli            # 原预算 CLI 子进程端到端
python3 -m tests.demo_allocation_cli # 代拨用途变更 CLI 端到端
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

## 代拨去向册与用途变更（budget_solver/allocation）

在既有模型/快照/编译/解算设施之上实现“循环代拨的资助改用途”，旧预算公式与
已发报告原样保留、继续可读；代拨模型以 `ALO_` 为 model_id 前缀与业务模型同根共存。

### 三层产物与数据流

```
场次 JSON（账户/出资户/注入/按来源转拨比例/议程/签字/委托/委托证/自有款）
   │ register-session（严格校验：比例和≤1、注入合计为正、规模上限、委托链无环）
   ▼
去向册 Ledger（ledgerN）
   逐出资户模型 + 合计模型，全部走 put-model → snapshot → compile → solve：
     in_<funder>_<acc> 真实输入格；T_<funder>_<acc> 来源总流入格；
     r_<funder>_<acc> 来源留用格（留用比例 = 1−Σ来源转出比例）
   逐户解算给出每个出资户最终留在每个受款户的精确金额与可回查来源；
   封闭空转 SCC（不外拨且不留用）+ 来源可达性 → 受困来源判定；
   全部来源结清才放行点名。
   ▼
点名册 Rollcall（rollN）
   按固定议程逐案：本人签字代表本人实际留此户份额；无签字沿唯一委托链找签字人；
   委托证按原出资户登记、各案共用证额、限定可签案；途中无证/超范围/未遇签字全弃权，
   有签字只交 min(权益, 证剩额)；途经证按实签同意/反对逐张扣（否决也扣）；
   以全部留用额为分母核门槛，零留用案不划拨。
   ▼
拨款账簿 Book（bookN）+ 正式拨款单 Order（orderN）
   原资助只可在点名同意金额内转用且不破旧用途底线；自有款按范围贴入、多案共用余额；
   稳定单号：同号同内容幂等复读、同号异文退回；dry-run 测算不占款；
   实付后可分开查每个原出资户旧用途余额、转入新用途金额、各自有款余数。
```

### 关键设计决策

* **转拨合同按来源履约。** 同一转出户对不同出资户的钱可约定不同比例
  （`transfers[].ratios` 覆盖缺省 `ratio`）。题面 D1 的钱按 1/2 转 Y、D2 的钱
  全留 X，正是两条来源流；若错误地用账户统一比例，D2 的钱会被误转。
* **绕环多圈只留用一次。** 留用是线性方程 `r = keep·T`，资金在环上每经过一站
  只扣一次留用；手算两站各留 1/2 的 8 元注入，解为 A=16/3、B=8/3，合计恰为 8。
* **受困与空转分离。** “不外拨且不留用”的封闭 SCC 由编译器 SCC 圈定；只有当
  某来源的钱真实可达该组时，逐户系统出现 `0=非零` 矛盾，该来源不能结清；
  无来款的空转组只产生零参数解，不牵连其他来源。验证器独立重建 SCC 与矛盾证书。
* **同意票不等于可划走反对者的钱。** 转用上限是该出资户对此案的实签同意金额；
  委托证额不足时只签 `min(权益, 证剩)`、其余弃权，并逐案扣证。
* **可行性用精确分数最大流。** 先用足获签原款（受底线约束），再把自有款运输
  问题建成 源→自有款（真实余额）→案（可贴范围）→汇（需求缺口），
  Edmonds-Karp 容量全为 Fraction，给出可执行分款或指出缺口批次。

### 手算案例（tests/test_allocation.py，69 项断言）

| 场景 | 手算结论 |
| --- | --- |
| 基准场 D1 出4 / D2 出2 | D1 留 X2Y2、D2 留 X2；CX 同意 3/4、CY 2/2 均获批 |
| 委托证额 1 | D2 权益 2 只签 1、弃权 1；证两案共用，CX 扣尽后 CY 无额 |
| 两种合法拨法 | A：转原款3留旧款1、自有款不动；B：转D1的2+贴自有款1、留旧款2；次日再付2结论相反 |
| 稳定单号 | 同号同内容幂等不二次下账；同号异文退回；dry-run 不占单号不动余额 |
| D1 留 X 比例 1/2→1/4 | X 仅获 2/3 < 3/4 否决 |
| 委托证额改 0 | D2 全弃权，X 仅 2/4=1/2 否决 |
| X 新项目 need=5 | 投票照过，但原款最多转 3 + 自有款 1 = 4，缺口 1 不可同时兑现 |
| 封闭循环 A↔B 全额互转 | D1 受困 A/B 不能结清、阻断点名；D2 留 C 不受牵连 |
| 无来款空转组 | A/B 零留用显式登记，D1 全额留 C，本场照常结清 |

### CLI 用法（与原 budget CLI 并列，互不影响）

```bash
python3 -m budget_solver.allocation.cli --data-root ./data register-session session.json
python3 -m budget_solver.allocation.cli --data-root ./data build-ledger S1
python3 -m budget_solver.allocation.cli --data-root ./data verify-ledger ledger1
python3 -m budget_solver.allocation.cli --data-root ./data build-rollcall ledger1
python3 -m budget_solver.allocation.cli --data-root ./data open-book ledger1 roll1 --label A
python3 -m budget_solver.allocation.cli --data-root ./data check-feasible book1
python3 -m budget_solver.allocation.cli --data-root ./data issue-order book1 order.json
python3 -m budget_solver.allocation.cli --data-root ./data book-state book1   # 重开核余额
```

### 持久化布局（挂在数据根的 allocation/ 下）

```
<root>/allocation/
  sessions/<session_id>.json   # 规范化场次（不可变）
  ledgers/ledgerN.json         # 去向册（绑定快照/产物/报告哈希、逐户留用、受困判定）
  rollcalls/rollN.json         # 点名册（逐案票额、委托链审计、证额扣减）
  books/bookN.json             # 拨款账簿（开场账 + 已支单号，随单追加）
  books-orders/orderN.json     # 正式拨款单（不可变，绑定三册指纹与分款明细）
```
