# 集团合并工作底稿系统（固定合成会计口径）

仅两家全资实体 P/S、一种货币、一个期间。账户固定为
`cash / receivable / inventory / payable / capital / revenue / cost`，
统一保存“借减贷”有符号整数余额（`signed = 借 − 贷`）。
**本系统只实现题目给定的合成口径，不实现现实会计准则或税务制度。**

## 1. 目录结构

| 路径 | 职责 |
| --- | --- |
| `group_consol/accounts.py` | 固定账户表、借减贷余额约定 |
| `group_consol/errors.py` | 稳定错误码（validation/conflict/pending/snapshot_conflict） |
| `group_consol/jsoncodec.py` | 规范 JSON、`Fraction` 的 num/den 持久化（绝不取整） |
| `group_consol/ledger.py` | 独立实体账簿：原子入账、连续 seq、稳定身份复读、局部校验、前缀查询 |
| `group_consol/consolidation.py` | 合并服务：切面冻结、错时待核、逐发票三类抵销、不可变底稿 |
| `group_consol/verifier.py` | 独立核验：直读 SQLite 原始表重算，不调用被核验系统的汇总函数 |
| `group_consol/clearing.py` | 银行清算包事实：唯一归包、双边凭证/最终 heads 核对、收付台三态查询、独立账重排 |
| `group_consol/binding.py` | 日结装册：分日红线枚举、跨日调节表生命周期、两套择优与配额卡点诊断 |
| `group_consol/archive.py` | 档案室：只对选中版本开表/转记/销表，表号不复用，封册可重开、凭证可追溯 |
| `plans/operations.json` | 68 步 JSON 操作计划（全部要求场景的端到端演示） |
| `scripts/run_plan.py` | 计划执行器，逐条断言 |
| `scripts/run_case.py` | 随题 `case.json` 的原样复现（含两种结算口径对比） |
| `scripts/run_clearing_demo.py` | 清算包/调节表/装册端到端演示（三种装册、跨日一表、等额未齐、配额卡点） |
| `tests/test_ledger_and_merge.py` | 单元测试（负利润、有理分数、回滚、不可变性等） |
| `tests/test_clearing_binding.py` | 清算装册单元测试（跨票/重复/缺侧/未平、表生命周期、择优、重开追溯） |
| `tests/stress_test.py` | 40 发票 / 每实体 200 凭证 / 20 声明的容量测试 |

运行：

```bash
python3.11 -m unittest tests.test_ledger_and_merge -v
python3.11 -m unittest tests.test_clearing_binding -v
python3.11 scripts/run_plan.py plans/operations.json
python3.11 scripts/run_case.py
python3.11 scripts/run_clearing_demo.py
python3.11 tests/stress_test.py
```

## 2. 单体账（EntityLedger）

### 入账与原子性
- `post(journal_id, entity, lines, document=None)` 在单个 `BEGIN IMMEDIATE`
  事务内完成全部校验与写入；任一校验失败 `ROLLBACK`，**整笔拒绝、不残留、不跳号**。
- 行支持 `[account, debit, credit]` 列表（身份为位置序号，数组顺序有意义）或带
  `line_id` 的字典；金额必须是非 bool 非负整数，每行恰一侧为正，总借=总贷且大于 0；
  未知账户、重复行身份整笔拒绝。
- `(entity, journal_id)` 为实体内稳定身份：规范 JSON（含行顺序）持久化，
  同规范重投返回 `status=replayed` 与原 seq，异文抛 `conflict`。
- 按实体分别分配连续 `seq`；`gl(entity, prefix)`、`journals(entity, prefix)`、
  `voucher(entity, seq)` 提供任意已提交前缀的总账与原始凭证。重开同一 SQLite
  文件即恢复全部历史前缀，正常重开不丢旧前缀。

### 内部文档的局部校验（绝不为合并拒绝单边）
- 内部销货/采购：卖方分录必须恰为
  `借receivable T / 贷revenue T / 借cost C / 贷inventory C`；买方镜像恰为
  `借inventory T / 贷payable T`；seller/buyer 为 P/S 不同实体，q/T/C 正整数。
  只校验本实体文档与本实体分录，**不要求对侧已提交**；同实体一张发票只可一次。
- 内部收付款（`type=internal_payment`，引用 `invoice_id`，side 为
  `receipt`/`settlement`）：只能由该发票对应侧登记，分录固定为
  `借cash/贷receivable` 或 `借payable/贷cash`，**累计金额不超过该侧原始 T**；
  各笔身份与来源逐行保留，双方可错时提交。
- 普通外部/期初凭证 `document=null`，独立入账，合并侧无法阻止单体客户提交。

> 随题材料的结算凭证是 `document=null` 普通凭证。系统照单受理；发布时由
> **余额守卫**（应收/付总账余额必须等于逐发票未清偿额）识别“总账已结清、逐发票
> 索引无来源”的情形并返回 `balance_guard` 待核，而不是静默凑平。需要逐发票核对
> 时，改用带 `invoice_id` 引用的内部收付款凭证（见 `scripts/run_case.py` 切面 B2）。

## 3. 合并服务（ConsolidationService）

### 切面与幂等
- `publish(input_id, heads, stock_version)` 以
  `{P 前缀, S 前缀, 存货声明版本}` 为**不可变输入切面**，规范文本落盘。
- 同 input_id 同切面重试 → 复读首次结论（published 返回同一份 statement；
  pending 再抛同一批待核明细）；异文 → `snapshot_conflict`。
- 新单体提交、新声明只产生新前缀/新版本，**旧底稿永久不变**。

### 发布前逐发票核对（问题全部结构化，绝不按金额凑平）
`missing_side`（缺镜像）、`invoice_mismatch`（side/主体/q/T/C 异文）、
`payment_mismatch`（两侧累计收付不一致，按 invoice_id 逐张比对，同额不同发票
不能交叉配对）、`balance_guard`（总账与逐发票索引不符）、
`stock_missing/stock_extra/stock_out_of_range`（声明必须含全部需声明发票，
r=0 也不能省略，且 0≤r≤q）。待核结论同样落盘并可复读。

### 三类抵销（分开列示，各自平衡）
对每张双侧齐全的发票：
1. **内部往来**：仅未清偿部分 `借 payable / 贷 receivable (T−累计收付)`；
   货款全额结清时本项为空，**不影响**第 3 项。
2. **内部销售**：`借 revenue T / 贷 cost T`。
3. **未实现存货利润**：`URP = (T−C) × r/q`（`Fraction` 约分）。
   URP>0：`借 cost / 贷 inventory`；**T<C 时 URP 为负，用相反方向表达，不截零**；
   URP=0（含 r=0）不出分录并在 `skipped` 标注，而不是被截断。

底稿输出：两实体原始合计 → combined → 每条抵销（含来源凭证 seq/声明版本）→
抵销总额 → 最终余额；并自检每条分录平衡、抵销合计为零、最终总借贷为零。
外部凭证不按 invoice_id 全局过滤，全部保留在原始合计中。

### 查询
- `statement(input_id)`：已发布底稿；`pending(input_id)`：待核明细。
- `sources(input_id, entry_id=None)`：逐项来源，解析出真实原始凭证或声明条目，
  可从合并数字一路追到原始分录。

## 4. 独立核验（verifier.py）

核验者**不调用** `gl()`、`_reconcile()` 等被核验系统自己的函数，而是：
1. 直接查 `journal_lines`/`journals` 原始表，按 seq 前缀自行累加七账户余额；
2. 自行重建内部发票、逐笔收付、存货声明索引，重跑镜像/收付/覆盖核对；
3. 按明示公式 `(T−C)×r/q` 手算每张发票的三类抵销，与底稿逐条比对方向与金额；
4. 验证最终余额 = 原始合计 + 抵销、所有分数保持 num/den、总借贷为零、
   来源引用均指向真实凭证/声明、单体凭证数与前缀一致（证明未被改写）。

## 5. 成本与一致性取舍

- 统一 SQLite、串行事务、发布时完整重算：规模上限（40 发票 / 每实体 200 凭证 /
  20 声明）下发布+独立核验为毫秒级（见压力测试输出），换取实现简单与可审计性。
- 单体账只追加：旧前缀天然保留，任何合并失败/重算都不可能改动历史凭证；
  代价是存储只增不减，题目规模下可忽略。
- 底稿冻结的是切面（前缀号+声明版本）而非数据快照：单体表只追加，前缀号内的
  内容由稳定身份与复读规则保证不变；这样发布无需复制原始凭证，索引可直接复用，
  成本低且一致性由 append-only 约束兜底。
- 错时提交：源端无阻塞地接受单边凭证，冲突全部推迟到发布时一次性输出待核清单；
  重复发布同切面是 O(1) 的持久化复读，不重算；异文切面直接拒绝，不产生第二份底稿。

## 6. 银行清算包与日结装册（clearing / binding / archive）

### 清算事实（clearing.py，纯内存、不改单体账）
- 每个清算包号只属一张双边内部发票（prefix）；一包可分多笔记，但每笔凭证
  `(实体, 凭证号)` 必须唯一归包。跨票（前缀与包专属发票不符）、凭证/序号重复、
  未知包一律结构化拒绝（`cross_invoice / duplicate_voucher / unknown_package`）。
- 可签事实要求两侧都有凭证且各自合计等于该包最终 head；缺侧 `missing_side`、
  未平 `not_balanced`。同额不同包不得交叉凑平。
- `desk_query(prefix)` 回答收付台三态：全未进 / 双边全收 / 已开头未收齐，并给两侧
  已入原单、已入金额与未进金额；两侧累计相等不代表同一批包。
- `resubmit_order` 从同一已核清算生成独立账：只换某实体提交顺序、重排相对 seq，
  凭证身份/金额/归包不变，原账保持可复读。

### 装册排法与调节表（binding.py）
- 每日给 P、S 末序红线，逐日分别不减，末日固定在最终 heads，每日可一侧无新凭证；
  红线越界或回落直接拒绝。
- 包在某日末未双边全收（按该包专属原单全集判定，非金额凑平）即被日界截住：
  首次跨界开包专属表，其后未齐之日滚入，双边全收当日注销；同包跨多天只耗一张，
  不换号、不接别的包。两侧当日累计等额但原单未齐仍开表（差额 0，占表不占额度）。
- 每日随表登记本包差额＝两侧“截至当日累计已进金额”之差绝对值；全日未清表差额和
  的峰值即须申请的日差额额度，跨包不抵消。
- 问一 `best_under_quotas`：表配额与日差额额度内最小总移动，同值先省表数；
  问二 `minimum_quota_for_movement`：移动预算内最低日差额额度，同额度先省表。
  `diagnose` 在其余约束仍生效的前提下分项定位 `*_quota_short / move_budget_short`。

### 档案室（archive.py，只对选中版本占号）
- 测算排法只产生 `TableLife`，不分配表号；`ArchiveRegistry.bind` 才对选定 Plan
  按 (开表日, 包号) 从配额池编号（R001…），开表/滚入/注销落不可变台账。
- 注销表号在本档案内不复用；不同档案各自独立号簿、互不串号；同档案号异文 `conflict`。
- 每日封 P/S 两册只引用原凭证号与末序，单体凭证与历史册可复读；`reopen` 复读全部
  封册与表台账，`trace_voucher` 沿原凭证号反查首次入册日与经过的开表/转记/销表手续。
- 容量约束：至多 8 日、6 清算包、每侧至多 18 笔后续凭证，超限构造即拒。
