# ADR-0024：`secondments` 一张表两套端点的收口 —— 并附五处取证到的缺陷（其中三处实测复现）

- 状态：Proposed
- 日期：2026-09-18
- 决策者：待定（需产品 + 运维共同裁定；涉及国家监测指标取数口径）
- 相关：P1-56（待裁定登记）、P1-11～P1-15（三套并行子域）、ADR-0003（并行子域收敛策略）、
  ADR-0006（"只搬不改"重构与零漂移守卫）、D-3（日期入参治理，`app/datetypes.py`）、
  `tests/test_orphan_endpoints.py::KNOWN_ORPHANS`、`tests/test_refactor_drift_guards.py`

## Problem（问题）

### 形状：同一张表，两套端点，两个页面各调一套

`secondments`（`models/hr.py:75`）被两个路由模块各写了一套：

| | `admin_mgmt`（人财物页调） | `staffing`（人员下沉调度页调） |
|---|---|---|
| 建档 | `POST /api/mgmt/secondments`（`:132`） | `POST /api/staffing/secondments`（`:159`） |
| 台账 | 无列表端点 | `GET /api/staffing/secondments`（`:196`，18 键行） |
| 结束 | `POST /api/mgmt/secondments/{id}/end`（`:160`） | `POST /api/staffing/secondments/{id}/end`（`:225`） |
| 统计 | `GET /api/mgmt/secondments/stats`（`:185`，两个计数） | `GET /api/staffing/dispatch-stats`（`:270`，国家监测口径） |

前端：人财物页 `pages-clinical.js:2019/2104` 调 mgmt 那套（只调建档与两键统计）；
人员下沉调度页 `pages-mgmt.js:1715/1716/1759/1763` 调 staffing 那套。
**两套写的是同一张表**（都是 `db.get(Secondment, id)`），不是两张表。

### 这不是"代码重复不好看"，是校验会分叉——而分叉处落在国家监测指标上

两套的**结束**端点逐行对照（`admin_mgmt.py:164-183` vs `staffing.py:227-249`）：

| | `mgmt:end_secondment` | `staffing:end_secondment` |
|---|---|---|
| `end_date` 入参 | 裸 `str`，**必填**，无格式校验 | 裸 `str \| None`，留空取业务日期，**同样无格式校验** |
| 不早于开始日 | **无** | `if finish < row.start_date: 422`（`:240-241`） |
| 归属校验 | 有（`assert_obj_org_writable(..., org_attr="from_org_id")`，`:174`） | 有（同一句，`:236`） |
| 员工状态回写 | `if employee: employee.status = "active"`（`:179-180`，**无条件**） | `if employee is not None and employee.status == "seconded"`（`:244-245`，**有前置条件**） |
| 回执 | `{id, end_date}` 两键 | 完整 `SecondmentOut`（18 键） |

> ⚠️ **更正 P1-56 的两处说法**。该条曾把 mgmt 整体描述为"严格更弱"，逐行读码后：
> 1. **归属校验这一轴两者完全相同**，两条 `end` 都有 `assert_obj_org_writable`。
> 2. **日期格式校验两者同样没有**（详见下一节实测）。mgmt 真正更弱的是四处：
>    "不早于开始日"这一道、员工状态回写的条件、判重的真源、以及回执形状。
>
> 另有两处**共有**的缺口，不该记在任何一套头上，但应另行登记：
> **两条 `create` 都没有归属校验**——`mgmt:second_employee`（`:137`）与
> `staffing:create_secondment`（`:161`）都不收 `user` 参数。`admin_mgmt.py` 里
> 其余写端点有 6 处 `assert_obj_org_writable` + 5 处 `assert_org_writable`，
> `staffing` 的另两个写端点（`:236`/`:258`）也都有——**两个文件各自跟自己不一致**。
>
> 读侧也有一处顺带记下（不属本 ADR 要裁的范围，但收口时会一并碰到）：
> `GET /api/mgmt/secondments/stats`（`:185-190`）**既没有 `require_roles` 也没有机构过滤**，
> 两个 `count` 直接扫全表——任何已登录账号读到的都是全平台在派/累计数。
> `staffing` 的两条读端点（`:196`、`:270`）同样无角色守卫，且 `resolve_org_scope`
> 只按**调用方传入的** `group_id`/`to_org_id` 收口，不按调用方身份。

> 下面按**缺陷**编号（共五处，前三处有实测复现）；
> 文末「为什么现在决策」按**两套之间的分叉**编号（共四次）——两套编号不是一回事。

### 实测一：一条 `end` 请求就能让一名医师从国家监测指标里消失

`mgmt` 的 `end_date` 是未校验的裸 `str` 查询参数，于是（本 ADR 实跑，SQLite 全新库）：

```
POST /api/mgmt/secondments/1/end?end_date=完全不是日期
  → 200 {"id": 1, "end_date": "完全不是日期"}

GET /api/staffing/secondments  → 同一条记录读回：
  {"end_date": "完全不是日期", "ongoing": false, "days": 251}
```

同一行数据自相矛盾：**台账说它已结束（`ongoing: false`），天数却还在按"算到今天"增长**
（`_days()` 在 `staffing.py:130-132` 把解析失败**吞掉**并回落成 `today`）。

更要紧的是监测指标那一头：

```
GET /api/staffing/dispatch-stats → {"invalid_date_records": 1, ...}
```

`dispatch_stats`（`staffing.py:324` 与 `:331`，两处 `invalid_date += 1; continue`）
对日期解析失败的记录**跳过**——于是**一位真的派驻满半年的中级医师，会因为有人把结束日填错，
而从「中级及以上医师派驻 6 个月以上人数」里整个消失**。

### 实测二：`staffing` 那条也收非法日期——它那道比较不是日期校验

把同一组值打到 `staffing` 自己的 `end`（`start_date=2026-01-10`，全新库实跑）：

| 写入 `end_date` | 字典序 vs `start_date` | `staffing:end` 的结果 |
|---|---|---|
| `完全不是日期` | 大于 | **200，原样落库** |
| `2026-13-99` | 大于 | **200，原样落库** |
| `abc` | 大于 | **200，原样落库** |
| `2026-02-31` | 大于 | **200，原样落库**（形状合法、日历不存在） |
| `!!!` | 小于 | 422 `结束日期不得早于开始日期` |

`finish < row.start_date` 是一道**字典序比较**，不是日期校验。
它挡下的只是"排在开始日之前"的那一类串——**非法日期里字典序更大的那一半照过不误**。

**所以"两套端点的分叉"在日期这一轴上不是"一套有校验一套没有"，而是"两套都没有"。**
这反而把问题指向更上游：**D-3 的日期治理漏掉了查询参数**（见下）。

### 实测三：那个"防止无声消失"的计数器，盲区恰好是 `mgmt` 独有的那一半

`invalid_date_records` 存在的理由写在代码注释里（`staffing.py:321-323`）：

> **一条记录无声无息地从指标里消失，比指标少一个人更难查**——所以单独报出来，不 continue 掉。

但 P1-2 那次性能优化在它前面加了一道 SQL 预筛（`staffing.py:295-296`）：

```python
.filter(Secondment.start_date <= year_end_str)
.filter((Secondment.end_date == "") | (Secondment.end_date >= year_start_str))
```

非法 `end_date` 能否被计数器看见，取决于它的字典序落在**年初字符串**的哪一侧。
同一个全新库、逐条写入后读计数器：

| 写入的 `end_date` | 字典序 vs `2026-01-01` | `invalid_date_records` |
|---|---|---|
| `完全不是日期` | 大于 | 4 → **5**（看见了） |
| `!!!` | 小于 | 5 → **5**（**没看见**） |
| `9999-99-99` | 大于 | 5 → **6**（看见了） |
| `0000-00-00` | 小于 | 6 → **6**（**没看见**） |

把实测二与实测三并排看，结论很尖锐：

> `staffing` 那道字典序比较挡住的，**恰好就是计数器看不见的那一半**
> （字典序小于开始日 ⊂ 字典序小于年初）。
> 于是"**写得进、看不见、查不出**"这一形态，**今天只能经 `mgmt` 那条端点产生**。
> 而"能被计数器看见"的那一形态，两条入口都能产生。

这不是设计，是两个各自合理的局部决定叠出来的：一道为了"结束日不能早于开始日"的业务比较，
一道为了性能的 SQL 预筛，谁都没打算承担日期校验。

### 今天的可达性，与"给它接个入口"的直接后果

必须说清楚：**这条路径今天只能经 API 直连触及。**

- `mgmt:end` **没有界面**——它正是孤儿端点欠账仅剩 4 条之一。
- `staffing` 页的「结束派驻」按钮（`pages-mgmt.js:1763`）**不送 `end_date`**，
  后端回落成业务今天，恒合法。

**但这正是本 ADR 必须在"接入口"之前落定的原因**：按孤儿端点的规则，
这条端点要么同批带界面、要么写豁免。**一旦给它接一个带日期输入框的入口，
这条路径就从"要直连 API"变成"页面上点两下"**——而人财物页现有的派驻表单
`start_date` 恰恰就是个手打的 `<input placeholder="开始日期 YYYY-MM-DD">`
（`pages-clinical.js:2040`，不是 `type="date"`）。

### 上游：D-3 的日期治理只覆盖了 body 字段，没覆盖查询参数

`app/datetypes.py` 的模块 docstring（`:4-6`）写着：

> 入库之后各处统计 `strptime` 解析失败就 `continue` 或返回 0——
> **实测用假日期建的一条派驻，整条从下沉指标里消失了。**

**D-3 唯一举的反面例子就是派驻**，而改法写得很清楚：「在入口就挡下来」。
但 D-3 那一轮改的是**模型层的 body 字段**（`SecondmentCreate.start_date: DateStr`、
`SecondmentIn.start_date: DateStr` / `end_date: OptionalDateStr` 都已经换过了），
**查询参数不在它的射程内**——两条 `end` 的 `end_date` 都是查询参数，都留在裸 `str`。

月度期间那一头是有配套的：CLAUDE.md §4 明写"body 字段用 `datetypes.PeriodStr`、
**查询参数用 `deps.require_month`**"，并有 `test_periodstr_single_source.py` 盯着。
**日期这一头没有对应的 `require_date`**——`deps.py` 里只有 `require_month`
（`:288`）与 `resolve_business_date`（`:331`）。`test_datestr_single_source.py`
盯的也是 body 形状。

**而且这个缺口是平台级的，不是本案两条。** AST 扫全部路由函数签名，
名字含 `date` 且注解为 `str` / `str | None` 的参数共 **27 处**（`app/routers/` +
`app/spd/routers/`），分布在 `appointments` / `billing` / `certs` / `clinical_docs` /
`medwaste` / `portal` / `resources` / `surgery` / `spd.care` / `spd.followup` /
`spd.referral` 等模块。各自的处理方式不统一——例如 `billing.py:1584`
`run_reconciliation(date: str)` 是**就地** `strptime` + 422（第三种写法），
多数只读参数则根本不校验。

本 ADR **只核了与本案直接相关的两条**（两条 `end`，是 27 处里**把值写进日期列**
且完全不校验的），以及随手抽查的 `billing` 那条。其余 24 处没有逐条核，
**已作为一件独立的事登记为 P1-58**（`docs/TECH_DEBT.md`），不夹在本 ADR 的落地批次里。

**这解释了缺陷为什么活下来：不是没人想到，是治理的覆盖面差了一类入参——
而 `deps.require_date` 这个对称物一旦补上，这 27 处才有一个共同的收口。**

### 第四处：`mgmt:end` 会把已离职的员工改回"在岗"

这一处不在任何已登记条目里，是本 ADR 逐行对照时发现的：

```python
# admin_mgmt.py:178-180
employee = db.get(Employee, record.employee_id)
if employee:
    employee.status = "active"          # ← 无条件
```

`staffing` 那条是有前置条件的（`:244-245`，只在 `status == "seconded"` 时才回写）。
而 `employees.status` 的取值里有 `"left"`（离职，由 `admin_mgmt.py:646`
`create_employee_change` 的 `change_type == "leave"` 写入）。

于是顺序如果是「派驻中 → 登记离职 → 结束派驻」，经 `mgmt` 那条走，
**员工状态被改回 `active`**；经 `staffing` 那条走则保持 `left`。

后果落在另一个统计上：`analytics.py:436-443` 的机构医师数按
`Employee.status == "active"`（`:439`）+ 岗位关键词计数——**一个已离职的医师会重新算进在岗医师数**。

同一个动作、同一张表、两条端点、两种状态机——这又是一处"校验会分叉"
（按两套之间的分叉计，它是第三次；见下文「为什么现在决策」）。

### 第五处：两套的「一人一条在派」判的不是同一张表

| | `mgmt:second_employee`（`:137-143`） | `staffing:create_secondment`（`:161-184`） |
|---|---|---|
| 判重依据 | `employee.status == "seconded"`（**employees 表的一个状态列**） | 查 `secondments` 表有无 `end_date == ""` 的行 |
| 拒绝方式 | 409「该员工已在派驻中」 | 409「该员工尚有未结束的派驻（自 …）」 |

两套用的是**不同的真源**，而 `employees.status` 这个真源是可以被别的端点改掉的：
`create_employee_change` 在 `change_type == "hire"` 时把 `status` 直接置回 `"active"`
（`admin_mgmt.py:647-648`），**完全不看该员工有没有未结束的派驻**。
于是「派驻中 → 登记入职 → 再经 mgmt 建档」这个**纯顺序**的调用序列，
就能给同一个人建出**第二条未结束的派驻记录**——不需要并发。
同样的序列打到 `staffing` 的建档会被 409 挡下，因为它查的是派驻行本身。

`secondments` 表在模型层与库层都**没有唯一约束**（`models/hr.py:75-95`），
所以并发下两条路都能建重；但上面这条是顺序可达的，性质不同。

顺带一提，`Secondment` 特意**没有 status 列**（`hr.py:89` 的列注释：
"不额外加 status 字段——两处表达同一件事，早晚会打架"），在派与否由 `end_date` 空串表达。
这个决定是对的，而 `mgmt` 那条建档恰恰绕过了它、去读另一张表的状态列——
**"两处表达同一件事"的问题没有出现在表设计上，出现在了两套端点之间。**

### 平台在自己的输出里给了一个不成立的保证

`dispatch_stats` 的响应里带着这一行（`staffing.py:366`）：

```python
# 存量数据里日期非法的条数（新数据已在入口拦下）。同样是单列而不丢弃。
"invalid_date_records": invalid_date,
```

**"新数据已在入口拦下"是假的**——实测二证明连 `staffing` 自己的入口都没拦下
（`完全不是日期` / `2026-13-99` / `abc` / `2026-02-31` 全部 200 落库）。
这句话写下时大概只核了**建档**那一侧的 body 字段（那一侧确实被 D-3 改过），
而**结束**那一侧的查询参数从来不校验。

这是本仓库反复撞见的同一形状（`batch_tasks` 的注释、`clock.py` 的 docstring、
ADR 索引、tcm_heritage 页那句"作答在医师端 H5 完成"）：**写下来之后没人回头核**。
区别在于这一次那句话不只在注释里，它**印在 API 响应里给运维看**。

### 顺带两处（不影响裁定，但会影响落地方案的代价）

1. **前端常量是反向跨文件依赖的**：`ASSIGN_TYPES` 定义在 `pages-mgmt.js:1706`
   （下沉调度页），却被 `pages-clinical.js:2041`（人财物页）用着。
   **"把人财物页的派驻表单退役"会先让另一个页面红**，除非常量先搬家。
2. **`TITLE_LEVELS`（`pages-mgmt.js:1707-1709`）是死代码**：全仓 0 处引用，
   台账用的是后端返回的 `r.title_level_name`。顺手删（童子军法则）。

另有一处口径表述问题：`LONG_TERM_DAYS = 183` 与 `days_in_year >= 183` 用的是
**日期差**而非自然日计数，因此"满 183 天"实际需要 **184 个自然日**
（`2026-01-01` 起算，首次达标是 `2026-07-03`；整年 `01-01`～`12-31` 只算 364）。
不影响达标判定的正确性，但 `caliber.long_term_6m` 那句
"当年在派满 183 天"是**出现在 API 响应里、给运维看的口径说明**，
应当写明是按日期差计。

### 取证方式（三处实测怎么跑的）

三处"实测"都是在全新 SQLite 库上经 `TestClient` 真调接口跑出来的，不是读码推断：
建机构与员工 → 经 `staffing` 建派驻 → 分别打两条 `end` → 回读台账与 `dispatch-stats`。
实测三的盲区是逐条写入后读计数器**变或不变**来确认的（写 `完全不是日期` 计数器 +1，
再写 `!!!` 计数器不动），而不是从 SQL 推断的。

### 为什么现在决策

1. **这是第四次了。**
   - 第一次 `assignment_type`：`mgmt` 的建档不收它，人财物页派出去的巡诊与短期支援
     一律按列默认落成 `long_term`，被算进监测指标（2026-09-16 已修，回归用例
     `test_admin_mgmt_contract.py::test_派驻类型可显式指定_不再一律落成长期派驻`）。
   - 第二次 `end_date` 的"不早于开始日"：`staffing` 有，`mgmt` 没有。
   - 第三次 `employee.status` 的回写条件：`staffing` 有，`mgmt` 没有。
   - 第四次 判重的真源：`staffing` 查派驻行，`mgmt` 读 `employees.status`——
     而后者可被「登记入职」改掉，于是顺序调用就能建出第二条未结束派驻。

   **逐个补校验的走法追不上分叉的速度**——两天内已数出四处，
   而且后两处（离职被改回在岗、判重真源不同）到今天都没人登记过。
2. `POST /api/mgmt/secondments/{id}/end` 是孤儿端点欠账**仅剩 4 条之一**，
   且是唯一一条"不是没做界面、是不该再做一遍"。198 条已全部接通，它卡在这里等裁定。
   而**接入口这个动作本身会把上面那条路径从 API 直连降到两次点击**（见前）。

### 约束

- **豁免名单是硬上限且已满**：`test_orphan_endpoints.py:214`
  `assert len(EXEMPT_PATHS) <= 7 and len(EXEMPT_MODULES) <= 1`，当前**正好 7 条**。
  任何"把 mgmt 端点标废弃并移入豁免"的方案都先撞这面墙。
- **端点面有零漂移快照**：`test_refactor_drift_guards.py` 快照了全部 `method + path`。
  删或改端点会让它红——按其 docstring，更新快照"必须是**刻意的**……要说清为什么该增该减"。
  这正是 ADR 的用途，不是障碍。
- **孤儿名单里那段注释会跟着变**：`test_orphan_endpoints.py:155-169` 现在写的是
  "所以不接，也不豁免……该怎么收口是架构裁定"。本 ADR 一旦有裁定结论，那段注释要同步改。
- **公共接口向后兼容**（CLAUDE.md §1.7）：两套端点的响应形状不同，
  任何收口都不得改变现有响应字节，除非明确批准破坏性变更。
- **前端常量的反向依赖**（见上）：动 `mgmt` 那套的界面要先处理 `ASSIGN_TYPES` 的位置。

## Options（可选方案）

- **方案 A｜维持现状**：两套都留着，只把这次数出的四处差异逐条补进 `mgmt`。
- **方案 B｜并到 `staffing`，`mgmt` 三条标废弃并移入豁免**：
  人财物页表单改调 `staffing` 的端点；`mgmt` 三条标 `deprecated=True`。
- **方案 C｜并到 `staffing`，删掉 `mgmt` 三条**：一并改零漂移快照。
- **方案 D｜`mgmt` 三条改成薄转发**：路径、守卫、响应形状**一个字节不变**，
  函数体改为调用两者共提的一个 `app/services/` 实现，只保留响应形状的适配。
  前端一行不改。
- **方案 E｜并到 `mgmt`**：把 `staffing` 的台账与监测口径搬进 `mgmt`，删掉 `staffing`。

## Advantages（各方案优点）

| 方案 | 优点 |
|---|---|
| A | 改动最小，今天就能做完；不碰任何闸门 |
| B | 意图最清楚（"这套不推荐了"）；`mgmt` 端点仍在，老调用方不受影响 |
| C | 最彻底，此后只有一套代码、一套校验；孤儿欠账直接少一条 |
| D | **唯一一个同时做到三件事的**：单一实现（校验不再分叉）、响应字节不变（不破坏兼容）、**前端与两道闸门都不用动**（端点仍在且仍被调用，既不占豁免额度，也不改零漂移快照） |
| E | 人财物页是"一页看全人财物"的定位，合并到那里符合该页叙事 |

## Disadvantages（各方案缺点）

| 方案 | 缺点 |
|---|---|
| A | **这正是三套并行子域（P1-11～P1-15）当年的走法**：对齐一次，下次改动再分叉一次。两天内已分叉四次，这条路已被实测证伪 |
| B | **撞豁免上限**（已满 7 条），得先决定提高上限还是让某条豁免退场——而上限是上一轮刻意收紧的；且 `POST /api/mgmt/secondments` 会因前端改调而变成新孤儿；还要先把 `ASSIGN_TYPES` 搬出 `pages-mgmt.js` |
| C | 破坏性变更（§1.7）：虽然只有自家 SPA 在调，但端点面快照要改、要有人签字；若有未知的外部调用方（对接网关、脚本）会直接 404 |
| D | `admin_mgmt` 与 `staffing` 都要依赖新的 `app/services/`（需确认不成环）；两套响应形状的适配层要有测试钉住；"端点还在但只是壳"要写清楚，否则下一个人会以为还有两套实现 |
| E | 工作量最大：`staffing` 带着 `LONG_TERM_DAYS`、职称等级、跨年天数截取这一整套监测口径，搬家风险高而收益只是"少一个前缀"；且 `staffing` 的界面是齐的（机构下拉、`type="date"`、职称等级维护、台账与结束按钮），搬过去要重做 |

## Migration cost（迁移成本）

| 方案 | 后端 | 前端 | 闸门/快照 | 量级 |
|---|---|---|---|---|
| A | 四处对齐：`end_date` 校验、不早于开始日、状态回写加条件、判重真源 | 0 | 0（端点不变） | 半天 |
| B | 三条加 `deprecated=True` | 人财物页表单改目标地址 + `ASSIGN_TYPES` 搬家 | **豁免上限要先裁定**；新孤儿要处理 | 半天 + 一次口径决定 |
| C | 删三条 | 同上 | 零漂移快照要改并签字；孤儿名单 −1；`:155-169` 注释改写 | 半天 |
| D | 三条函数体改为转发；抽一个共用实现（`app/services/`，CLAUDE.md §3 指定的位置） | **0** | **0**（`:155-169` 注释要改写） | 半天 |
| E | 搬四条端点 + 监测口径 | 两页都要改 | 零漂移快照大改 | 2–3 天 |

**所有方案共同的一步（建议无论选哪个都先做，见 Recommendation 第一步）**：
把日期校验补到**两条** `end` 上，把状态回写的条件补齐，并各带一条回归用例。

## Risk（风险）

**做了的风险**

| 方案 | 风险 | 缓解 |
|---|---|---|
| 第一步 | 补 `DateStr` 后，正在用非法日期调用的既有脚本会开始收 422 | 这正是目的；但要先跑一次 `dispatch-stats` 看 `invalid_date_records`，非零说明确有此类调用方或存量数据 |
| 第一步 | 状态回写加条件后，"离职后结束派驻"不再把人改回在岗——若有人依赖旧行为把误标离职的人改回来，会失去这条野路子 | 正规路径本来就有：`POST /api/mgmt/employees/{id}/changes` 的 `change_type=hire`（`admin_mgmt.py:648`）。在 docstring 里指明 |
| B/C | 存量数据里已有非法 `end_date` | 收口前先跑 `dispatch-stats`；非零则先出逐条清单人工处置，**不要在迁移里批量改**（CLAUDE.md §4：迁移不得静默改动存量业务数据） |
| C | 未知外部调用方 404 | 先查网关/对接方；本仓库端点面只服务自家三端，风险低但要确认 |
| D | 跨模块 import 成环 | 共用实现放 `app/services/`，两个路由都依赖它、互不依赖；`docs/DEPENDENCY_MAP.md` 同步 |
| D | "壳"被误读成"还有两套实现" | 转发函数的 docstring 写明它是壳、真源在哪；ADR 编号写进注释 |

**不做（方案 A 或什么都不做）的风险**

- **指标继续可被损坏，且最坏的一种是静默的**：`dispatch-stats` 把非法日期记录
  `continue` 跳过，而字典序小于年初的那一类**连计数器都看不见**——
  运维看到 `invalid_date_records: 0` 也不能说明没丢人。
- **离职员工被改回在岗**，直接污染 `analytics.py:439` 的在岗医师数，
  且这一处**今天没有任何用例或闸门盯着**。
- **第五次分叉几乎必然**：两天四次的实测速率摆在这里。
- **孤儿端点的规则会推着人把这条端点接上界面**，而在补校验之前，
  接上界面等于把上面那条路径从"要直连 API"降到"页面上点两下"。
- 平台继续在 `dispatch-stats` 的响应里对运维说"新数据已在入口拦下"这句不成立的话。

## Recommendation（建议）

**建议分两步，且第一步立刻做、不等裁定。**

### 第一步（不需要裁定，建议立即执行）

1. **补日期校验到两条 `end` 上**（不是只补 `mgmt` 那条——实测二证明两条都没有）。
   建议顺着 D-3 已有的形状做：在 `deps.py` 加一个 `require_date`
   （与 `require_month` 对称，内部调 `datetypes` 的校验，**不再写第二份日期正则**），
   两条 `end` 的 `end_date` 都改用它；并补一条与
   `test_periodstr_single_source.py` 同形的用例，把"日期查询参数不得裸 `str`"钉住。
2. **把状态回写的条件补到 `mgmt:end`**（`status == "seconded"` 才回写 `active`），
   与 `staffing` 对齐；回归用例覆盖"派驻中 → 离职 → 结束派驻"这条顺序。
3. **把 `dispatch_stats` 的两句话改成实情**：`:366` 那句"（新数据已在入口拦下）"
   在 1 做完之后可以重新为真，但要写明**它只覆盖这两条端点**；
   `caliber.long_term_6m` 写明 183 天是按日期差计。
4. 顺手删 `TITLE_LEVELS` 死代码（童子军法则，单独一个提交）。

**判重真源那一处（第五处分叉）建议不放进第一步。** 把 `mgmt` 的建档改成查派驻行，
会让某些今天能建成的顺序开始收 409——那是**行为变更**（CLAUDE.md §1.1「保持既有行为」），
该跟着第二步一起走，由共用实现天然消掉，而不是在补缺陷的批次里夹带。
在此之前建议先跑一条只读盘点：`SELECT employee_id, COUNT(*) FROM secondments
WHERE end_date = '' GROUP BY employee_id HAVING COUNT(*) > 1` —— 存量里已经有几个人重了，
这个数会直接影响第二步该怎么收（是拒还是先清）。

**理由**：这些缺陷今天就能触发、会让真实医师从国家监测指标里消失或让离职者回到在岗数，
它们的修复**不依赖"两套怎么合并"这个更大问题的答案**。

### 第二步（需要裁定）：建议方案 D（薄转发）

理由：

1. **它是唯一同时满足三个硬约束的方案**——单一实现（校验不再分叉，这是本 ADR 的根本诉求）、
   响应字节不变（§1.7 向后兼容）、**两道闸门都不用动**（不占已满的豁免额度，
   不改零漂移快照）。B 撞上限、C 要破坏兼容、A 已被两天四次分叉证伪。
2. 它把"要不要保留 `/api/mgmt/` 这个前缀"与"校验能不能分叉"**解耦**了：
   前者是产品口径问题，可以慢慢议；后者是正确性问题，现在就能关掉。
3. 共用实现放 `app/services/` 正好是 CLAUDE.md §3 指定的位置
   （"若你要抽服务层，放 `app/services/`，别再往路由里堆"），
   顺带为这个仓库长期缺的服务层开一个有真实理由的头——而不是为了抽而抽。

**若裁定者更看重"少一个前缀"的整洁**，则选 C 而不是 B：
B 的代价（提高一个刻意收紧的上限）买到的只是"端点还在但没人用"，
而 C 至少把事情做完。**B 是三个方案里性价比最低的一个。**

### 落地顺序

第一步（补校验 + 状态条件 + 回归用例）→ 裁定 → 第二步（抽 `app/services/secondments.py`，
两个路由都改为调它，响应形状各自适配）→ `POST /api/mgmt/secondments/{id}/end`
从 `KNOWN_ORPHANS` 划掉（此时它可以安全地有入口：两条路的校验已经统一，
日期输入也已经被 `require_date` 挡住，不再是"同一个动作两条校验不同的路"）
→ 同步改写 `test_orphan_endpoints.py:155-169` 那段旁注。

**需要谁批准**：产品（`/api/mgmt/` 前缀的去留、两个页面的职责划分）
+ 运维（存量非法 `end_date` 的处置方式）。
本 ADR 只给建议，不替人做决定；采纳前 `KNOWN_ORPHANS` 里那条与其旁注保持原样。
