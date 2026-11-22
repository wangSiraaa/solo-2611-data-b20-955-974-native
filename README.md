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

# 夜班：楼号认楼、实物钥台账、冻结清点单
python3 -m address_delivery recognize-building --db /tmp/a.db P2
python3 -m address_delivery key-transfer --db /tmp/a.db handover T1 K 总柜 H -5
python3 -m address_delivery key-transfer --db /tmp/a.db receipt  T1 K 总柜 H -4
python3 -m address_delivery key-ownership --db /tmp/a.db K 0
python3 -m address_delivery freeze-cutoff --db /tmp/a.db night0 0
python3 -m address_delivery cutoff --db /tmp/a.db night0
```

测试：`python3 -m unittest discover -s tests`（129 项，覆盖下述全部场景）。

## 结构

| 模块 | 职责 |
| --- | --- |
| `normalization.py` | 仅 NFC + 首尾去空白；身份键构造 |
| `catalog.py` | 城市/区/街道稳定 ID、作用域名称表、完整父链解析、覆盖片 |
| `delivery.py` | 独立投递作业库：尝试、原子提交、身份冻结、历史 |
| `buildings.py` | 从已冻结的成功作业认楼；号段可限单双号，命中两幢交门房 |
| `keys.py` | 实物钥事件台账：交出/签收、在途、争议停借、营业截点冻结清点单 |
| `night.py` | 有向街图排路（步行/占钥双目标、钥匙预算）与签单台 |
| `service.py` | 共享一个连接的组合入口（目录 + 作业 + 楼 + 钥 + 夜班同一事务） |
| `input.py` | CSV 与 `address-input/v1` 的结构校验 |
| `cli.py` | 命令行 |

各库彼此独立、各自只建自己的表；可只打开目录库或只打开作业库。夜班三库
（`buildings`/`keys`/`night`）是纯增量，**原有柜面行为不变**。

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

## 认楼（`buildings.py`）

楼号不是从地址文本猜的，而是从**已成功提交并冻结的作业**认：稳定街道 ID +
整数门牌 + 冻结站点（投递身份）。同街道同门牌、站点不同的两件仍可分属不同楼。

- 楼号段 `lo..hi` 端点包含，`parity` 可为 `any`/`odd`/`even`。
- 楼可钉 `station_id`：只有冻结到该站点的作业才能认成这幢楼——这正是同址不同
  站分楼的依据。
- 结果四态：`unique`（认到唯一一幢）、`two_buildings`（命中两幢，交门房复查，
  绝不自动指派）、`unmatched`（无号段覆盖）、`no_job`（该件没有成功冻结作业，
  未成功作业不得补交认楼）。

## 实物钥台账（`keys.py`）

每把钥有唯一刻号 `key_code` 与期初持有人。一次跨柜移交共用一个 `transfer_id`，
由原持有人记**交出（handover）**、实际接收方记**签收（receipt）**两张签名；
同一移交号核对刻号、双方与时刻先后（签收不早于交出）。台账是只追加的事件日志，
归属与清点单都由回放日志得到：

- **在途**：交出后、签收前，两边都不能再借；签收后才重新可借。
- **争议停借**：同一移交号出现互相矛盾的已签记录、从无权一方（含在途时）交出、
  缺交单的收据，都会在争议起点起停借；证据只追加，删任何一方都无法恢复。
- 一次交出尚无收据只是仍在途，不是争议。
- 相同记录重送按幂等处理，仍是同一笔；各钥分别独立清点。
- **冻结清点单**（营业截点）复制每个钥在某时刻的状态；此后旧交接史不再追加进
  这张单，同号重冻返回原单；可凭一端先送达的旧记录重制（在途钥冻结为在途、不可
  借）。`ownership_at(key, moment)` 单查某时刻实物归属及其证据依据。

## 夜班排路与签用（`night.py`）

`NightPlanner` 在有向街图（边长为正整数分钟）上做**标号化 Pareto 穷举**，规模
至多 6 件 / 10 结点 / 3 钥 / 24 分钟。约束与口径：

- 邮差从站出发、打烊前回站；到合格入口交件、借还钥、等待均不耗分钟；同刻可按
  顺序交多件；包裹可用 `after` 指定先后。
- 入口有适用件类别、半开整数可交时段 `[lo,hi)` 与所在结点；`side` 便门须持指定
  实物钥，`staffed` 值守门不持钥。
- 钥只能在冻结清点单认定的柜点借、还回同柜；每把独占，一次借用记半开区间
  `[出柜, 还柜)` 的整数离柜分钟，且不得与其他班次的半开占用重叠。
- 主任三问：`shortest_walk()`（最少走路，再省占钥）、`least_key_minutes()`
  （最少占钥，再省走路）、`shortest_walk_with_budget(n)`（占钥 ≤ n 时的最短可行
  路）；`director_report()` 一次给出，`frontier()` 给出完整 Pareto 前沿。**不存在
  “离柜一分钟折多少步”的统一换算**，两项各自独立计。
- `NightDesk` 是持久化签单台：拟排不占钥；签单在一个事务内占用全部区间，失败一
  把都不占；与已签区间相撞的新签被 `rejected_conflict` 拒绝；重送同单幂等回放、
  不发第二份钥；在途/争议钥在签单台被 `rejected_not_borrowable` 拒绝。

### 题面小街的手算结论（测试 `test_night_narrative.py` 固化）

H 为站与值房，D 便门往返各 1 分钟、S 同楼值守门往返各 3 分钟，一件、两门全晚
开、打烊第 8 分钟、刻号 K 由总柜交 H 且 H 已签收：

| 情形 | 可行最优 |
| --- | --- |
| 两门皆开 | 走 D 步行 2 分钟、占 K `[0,2)` 两分钟；或走 S 往返 6 分钟、不占 K。两张单都报主任 |
| 仅撤 H 收据、保留交出 | K 在途不可借，只剩 6 分钟值守门路线 |
| 原清点单 + 值守门当晚关闭 | 只能走 2 分钟便门并借 K |
| 首趟实签后再签冲突单 | 冲突单 `rejected_conflict` 且不占任何钥；重签同单只回放 |
