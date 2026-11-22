# 地址目录与独立投递作业库

Python 3.11 + SQLite（标准库 `sqlite3`），只处理合成地址。实现位于
`address_delivery/`，测试位于 `tests/`，不修改 `materials/707`。

## 快速开始

```bash
# 初始化 r1 目录与覆盖
python3 -m address_delivery init --db /tmp/a.db --materials materials/707

# 直接读 CSV（结构错误时整批拒绝）
python3 -m address_delivery import --db /tmp/a.db materials/707/addresses.csv

# 或接通给定客户端的 address-input/v1 输出
python3 materials/707/csv_client.py materials/707/addresses.csv \
  | python3 -m address_delivery ingest --db /tmp/a.db -

python3 -m address_delivery apply-revision --db /tmp/a.db materials/707/revision.json
python3 -m address_delivery retry --db /tmp/a.db P1 P3 P4
python3 -m address_delivery replay --db /tmp/a.db P2
python3 -m address_delivery submit --db /tmp/a.db P7 C1 西 东路 2

# 查询入口
python3 -m address_delivery resolve --db /tmp/a.db C1 同区 东路 2
python3 -m address_delivery explain --db /tmp/a.db P3
python3 -m address_delivery job --db /tmp/a.db P2
python3 -m address_delivery catalog --db /tmp/a.db street --id S2
```

测试：`python3 -m unittest discover -s tests`（131 项，覆盖下述全部场景）。

## 结构

| 模块 | 职责 |
| --- | --- |
| `normalization.py` | 仅 NFC + 首尾去空白；身份键构造 |
| `catalog.py` | 城市/区/街道稳定 ID、作用域名称表、完整父链解析、覆盖片 |
| `delivery.py` | 独立投递作业库：尝试、原子提交、身份冻结、历史 |
| `service.py` | 共享一个连接的组合入口（目录只读 + 作业写入同一事务） |
| `input.py` | CSV 与 `address-input/v1` 的结构校验 |
| `readers.py` | 合成读机：混淆组、校样校准、同表解码、矛盾停发凭单 |
| `labels.py` | 标签排版、共享区眉、明字印次、逐柜台校验、格数/印次规划 |
| `cli.py` | 命令行 |

**目录与作业彼此独立**：`Catalog` 只建目录表，`DeliveryStore` 只建
`jobs`/`attempts`；可以只打开目录库完成查询与批量解析，不需要任何作业表
（见 `tests/test_catalog.py::test_catalog_works_without_delivery_tables`）。

## 关键语义

### 名称作用域与解析

- 名称/别名分别存在 `district_names`、`street_names`，**只在声明的父级内有效**。
- 解析始终走完整父链：先按 `city_id` 限定区文本得到区集合，再只在这些区内
  按街道文本取候选。**不会先任选一个区**。
- 候选数 1 = `unique`；0 = `unknown`；>1 = `ambiguous`，并列出全部并列候选。
- 规范化仅 NFC + trim。`学  园`（两空格）与 `学 园`（一空格）是不同名称；
  同名文本可合法指向多个实体，**不是目录错误**。
- 停用街道仍进入候选集、仍可按稳定 ID 查询、仍解释历史名称；只有“新派送”
  一步拒绝。

### 覆盖与站点

- 命中条件：`lo <= n <= hi` 且 `any` 或奇偶匹配，**端点包含**。
- 同一门牌命中**多个不同站点** → `failed_station_ambiguous`。
- **同一站点的重叠覆盖片不构成歧义**（P3 在 r1 命中 K1+K2 均为 A → 不歧义；
  另命中 K3=X 才歧义）。

### 提交与身份冻结

作业在同一 `BEGIN IMMEDIATE` 事务内：查冻结记录 → 解析地址 → 查覆盖 →
写 `jobs`/`attempts`。成功需要：地址唯一、街道有效、站点唯一。

- 保存原文本、稳定街道/区、门牌、站点、目录版本、实际解释（候选+覆盖）。
- **首次成功后身份冻结**：再次提交内容相同（城市 ID + NFC/trim 的区/街道文本
  + 整数门牌）→ 原样复读当时结果，不重新解析目录；内容不同 → `rejected_identity_conflict`，
  已存作业不被改动。来源文件/行号不参与身份。
- 业务失败**不生成**已提交作业；失败包裹可重试并反映当前目录。
- 已成功的包裹即使改名、停用或覆盖修订，也不重派；旧失败无法覆盖已成功记录。

### 输入契约

- CSV 列名须**恰好**为 `parcel_id,city_id,district_text,street_text,house_no`；
  缺列/多列/字段数错/门牌非“正 ASCII 十进制整数” → 整批拒绝，且无任何副作用
  （测试含失败后重开验证）。
- `address-input/v1` 被直接接收，容忍新增字段，保留现有字段含义。
- 带引号逗号（`"学,园"`）、分解重音（`Café路`）按 NFC 正确匹配；
  内部空白不做合并。

## 更新与缓存失效取舍

- **按父链求交**：每次解析用 3 条索引查询（区名、街道名、区集合），
  规模 ≤5 城/50 街道时开销可忽略，且天然无缓存失效问题。
- **作用域索引**：`district_names(text)`、`street_names(text)`、`streets(district_id)`、
  `coverages(street_id)` 支撑“名称→实体”与“父链求交”。解析总是在当前事务内读，
  修订提交后**无需显式失效**。
- 未引入跨修订的内存缓存：目录极小而变化会改变候选集与站点，缓存收益低、
  失效风险高。若未来目录变大，可在 `catalog_meta.version` 上做键控缓存，
  版本变更即整体失效；已提交作业因冻结身份不受影响。
- 修订原子：任一操作失败则整个修订回滚（版本号也不推进）。

## 标签印刷与合成读机（扩展）

在已成功包裹的冻结稳定街道/门牌之上，增加了两个可选层（不改变上面的目录/作业
语义）：

| 模块 | 职责 |
| --- | --- |
| `readers.py` | 合成光学读机：等大混淆组、逐格校样、同一套一一配对、矛盾停发凭单 |
| `labels.py` | 页面/行排版、共享区眉、明字压印（印次）、逐柜台语义校验、格数/印次规划 |

### 读机校准

- 每台读机有一组等大的“纸面字 / 输出字”。整台机器采用**同一套一一配对**，
  组外字符照原样识读。状态存在 `Reader` 对象里（JSON 可载入）。
- **校样**把已知纸面文字的每一格与观测对齐；观测可为 `{"exact":…}`、
  `{"choices":[…]}` 或 `{"missing":true}`。所有校样共同约束同一套配对。
- 全部配对都无法解释校样 → **矛盾**：`Reader.tables()` 为空、
  `credential()["credential_issued"]` 为假，`require_calibrated()` 抛
  `ReaderContradiction`。校正一张校样（`replace_proof`）即重发凭单。
- `Reader.indistinguishable_pairs(paper)` 给出维修员查询：
  哪两种完整读法在现有校样下仍分不清。
- 同一行内区眉与街名**必须用同一套配对**解释（`consistent_decodings` /
  `diagnose`）：一个纸面字不会在一行里读成两个字。`pair_is_possible` 可判别
  “逐格看似可能、但不可能出自同一台机器”的字符串对。

### 版面与明字

- 一页一个共享区眉，每行“街名 + 分隔符 + 十进制门牌”；整页普通纸**按 NFC 码点
  计格**，区眉计一次，每行照收。页内行按既有门牌顺序排列。
- 只用该稳定街道或其所属区在**指定读者版**实际登记的正名/别名。
- **明字印头**对指定纸面字压一次，该格在**所有**读机上都照字面识读，不受旧字表
  影响；每处压一次耗一个印次。共用区眉只需压一次；若拆成多页则每页重压。
- 页面对**每个收件柜台**都要在它还可能的**每套字表**下唯一认到本行的目标稳定街道。
  识别走完整父链（城市→区→街），且区眉与街名同一套字表；与投递业务层不同，
  印刷行带固定门牌，故**用有效站点排除同名街**：对若干同名的候选街道，只要某条对
  该门牌没有唯一站点，就不能是目标。只有两条同名街都各有唯一站点时才仍算歧义。
  `resolve_row` 与此一致。
- 输出含真实版面纸张、明字印位置，以及每个柜台逐字表的**回读凭单**
  （`receipts`，`affirm_receipt`）。每页 `advice` 说明该页明字是否划算：
  与“同几行的零印次最长名版面”比较，`cells_saved` 即压印省下的格数。

### 规划

- `plan_deck(jobs, designated, recipients, max_impressions=…)`：
  总格数最少（同等格数再省印次）。`plan_min_impressions(..., target_cells=…)`：
  在已付格数内用最少印次（同等印次再省格）；`(None,-1)` 表示已付格数不够。
- 规划按既有门牌顺序把**同区连续行**分页，枚举每个正名/别名与明字子集，
  返回格数/印次 Pareto 前沿。共享区眉把多行并到一页；短名逐行贪心可能需更多
  页或更多印次，故会输给“共享长区眉”的整体排法（测试 `GreedyTrapTests`）。
- 命令行：`reader-calibrate`、`reader-confusions`、`diagnose`、`plan`
  （`--max-impressions` 或 `--borrow-for` 已付格数）。

### 手算例（S 街 2 号）

东区 S 与西区 T 街名同为“杉”；东区区名“东/新城”，西区只用“西”。读机 A 已校准，
读机 B 的“东/西”可能原样也可能互换。每页够放 5 格：

| 情形 | 版面 | 格数 | 印次 |
| --- | --- | --- | --- |
| B，短“东”压一次 | 东̲ 杉/2 | 4 | 1 |
| B，长“新城”不压 | 新城 杉/2 | 5 | 0 |
| B 加校样“纸面西确读西”后短名 | 东 杉/2 | 4 | 0 |
| 同一 B 原校样、印头全借走 | 新城 杉/2 | 5 | 0 |

两行共区眉：B 两行（东区杉、楠）共享“东”压一次为 7 格/1 印次；不压用“新城”8 格/0 印次。
逐格看似可能但不可能出自同机的字符串对：读机把 `甲→子、乙→丑` 或对调，但绝不会
`甲→子、乙→子`（`pair_is_possible(...,"子","子") == False`）。

## 材料核对结果

| 包裹 | r1 | r2 后 |
| --- | --- | --- |
| P1 | 两个街道候选（S1/S2）→ 歧义 | 仍歧义（不因停用/覆盖猜地址） |
| P2 | S3 → A 提交 | 复读 r1 冻结事实 |
| P3 | 门牌 9 命中 A/X → 站点歧义 | K3 删除后 → A 提交 |
| P4 | 街道未知 | 别名“东部支路” → S1 提交 |
| P5 | 引号逗号 → S3 提交 | 复读 |
| P6 | 分解重音 NFC → S3 提交 | 复读 |
| P7 | — | 西/东路 = 停用 S2 → 拒绝新派送 |

最终已提交：P2/P5/P6（版本 1）+ P3/P4（版本 2）；P1、P7 无作业。
