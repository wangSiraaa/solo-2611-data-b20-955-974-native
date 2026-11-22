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
python3 -m tests.test_livebook       # 72 项断言：单向换挡实况册 + 精确计量发行（题面八情景）
python3 -m tests.test_scale          # 20 模型 / 300 格 / 1000 引用规模与计时
python3 -m tests.demo_cli            # 原 CLI 子进程端到端
python3 -m tests.demo_livebook_cli   # 实况册 lb-* CLI 子进程端到端
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

## 单向换挡实况册与计量发行（`budget_solver/livebook/`）

在不改动原解算内核的前提下，叠加一层面向“电桥换挡照片”的应用，沿用模型登记、
快照、编译与单点 query，现场不重新编译模型。全程 `Fraction` + 一维闭区间，无浮点、
无采样、不以铭牌两端代替整段承诺。

### 概念与数据流

```
冻结编译产物（方程/系数/SCC 不变）
  │  输入格方程 x=value 的右端按盘面替换，公式格一律不动
  ▼
实况册 LiveBook：触点组（起/终值、前置）+ 指针登记
  │  起值不符/前置成环/组间接管格重叠/超限（10 组、8 针、12 照片、3 线）整册退回
  ▼
盘面 = 已动组集合（前置闭包），枚举为偏序 DAG；物理上可经过不可检定盘面
  │  对指针实际依赖闭包的并集联立 SCC 分块 RREF：
  │  联合矛盾→整盘不可检定；个别针非唯一→只禁用该针，其余照常读数
  ▼
照片（顺序、闭读数区间、端点可同）+ 板卡挡位线 b = u·g + v（正增益区间）
  │  每针约束 lo ≤ (x+u)·g + v ≤ hi 解成精确线性带，与线区间逐针取交
  ▼
发行引擎：照片顺序×盘面 两层 DAG，强制已动集合单调（可同态/漏拍、不可倒退）
  │  逐状态维护增益闭区间并集；终态合并 = 该线能解释的全部增益区段（孤立点保留）
  ▼
整段签发（区间包含判定，退签给段内精确有理增益）/ 增益见证与预印动作单 /
驻点复检最小集合覆盖（最少动作单数、其余现场重查）/ 同一次单向切换判定
```

| 文件 | 职责 |
| --- | --- |
| `livebook/intervals.py` | 闭区间、并集交并、线性带 `lo≤αg+β≤hi` 精确求解、整段覆盖与拒签见证 |
| `livebook/plates.py` | 盘面输入替换、指针闭包并集、联立 RREF 与读数分类（确定/非唯一/不可检定） |
| `livebook/book.py` | 实况册登记校验、前置成环检测、可达盘面枚举、机械师盘面/指针独查 |
| `livebook/photos.py` | 板卡挡位线（≤3，落盘）与有序照片集（≤12，闭区间读数）登记 |
| `livebook/engine.py` | 单调 DP 发行、整段签发、增益见证、动作单线性化、驻点复检集合覆盖 |
| `livebook/service.py` | 门面与重开：从落盘区间重建工作态，重查无需重新编译 |
| `livebook/cli.py` | `lb-register/lb-plates/lb-next/lb-inspect/lb-needle/lb-lines/lb-photos/lb-issue/lb-certify/lb-explain/lb-recheck/lb-common` |

### 题面台架手算结论（`tests/test_livebook.py` 全部锁定）

冻结模型 `X=1+A, Y=1+B`（A=B=0），甲挡 `b=1/2`、乙挡 `b=0`，两线 `g∈[1,4]`：

1. 照片 `X∈[4,5],Y∈[2,3]`：甲挡发行 `g∈[7/4,9/4]`（只动 A 的盘面），铭牌 `[7/4,2]` 整段可签；
2. 机械先后改为 B 先 A：甲挡无可解释增益，原铭牌退签（见证 `g=7/4`）；
3. A 先 B 不变、甲铭牌下限放宽到 `3/2`：`g=3/2` 即拒签证据；
4. 照片仅 `X=4` 走乙挡：只发行孤立点 `g=2`（P01）与 `g=4`（P00），`g=3` 无解，`[2,4]` 不能整段签；
5. 照片改为 `X∈[2,4]`：乙挡全段 `[1,4]` 可签，但没有任何单一拍摄位置覆盖整段；
6. 驻点复检 `g=1,2,4`：最少两张动作单；夹一张须现场重查、夹两张全照单做；
7. 前两张照片各自单验成立，但来源板卡线混杂，不能来自同一次单向切换。
