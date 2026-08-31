# ADR-0018：冻结核心表 admissions 补 created_at 列（created_at 欠账收官）

- 状态：**Accepted**（实现随本 ADR 同批落地；动的是冻结核心表，按《数据模型治理》流程留档）
- 日期：2026-08-31
- 决策者：仓库所有者
- 相关：`docs/数据模型治理.md`（核心表冻结与"新表必须带 created_at"棘轮）、
  `tests/test_schema_governance.py`（`FROZEN_CORE_COLUMNS` 快照与
  `BASELINE_MISSING_CREATED_AT` 棘轮）、迁移范式先例 `f9e8d7c6b5a4`
  （疫苗接种补 created_at，常量回填→撤默认，52 表同一口径）

## Problem（问题）

`created_at`（行写入时间）棘轮清偿到只剩 2 张表：`blood_stocks`（小型
upsert 台账，当时按低价值降级留置）与 `admissions`。admissions 一直留着，
不是因为不需要——它有 `admitted_at`（入院业务时间）但没有行写入时间，
**补录历史住院时二者不同**，审计"这条记录什么时候进的库"只能靠猜——而是
因为它在核心表冻结清单里（`users`/`organizations`/`patients`/`encounters`/
`admissions`），改列必须先过 ADR，此前没人走这一步。现在其余 52 张表全部
补齐，这 2 张成了"棘轮永远归不了零"的尾巴。

## Options（可选方案）

- **方案 A（维持现状）**：豁免名单永久保留 2 张。
- **方案 B（补列，常量哨兵回填，选定）**：两表各加 `created_at DateTime NOT NULL`，
  历史行回填哨兵 `1970-01-01 00:00:00`，回填后撤 `server_default`——与已完成的
  52 张表逐字同一范式。
- **方案 C（admissions 用 admitted_at 回填）**：历史行 `created_at = admitted_at`。

## Advantages（各方案优点）

| 方案 | 优点 |
|---|---|
| A | 零改动 |
| B | 与 52 表同一口径，哨兵值一眼可辨"此行早于该列存在"，不伪造精度；迁移形状是数据安全测试的 A 档（纯加列，无 UPDATE 业务数据） |
| C | 历史行的值语义上更接近真实写入时间 |

## Disadvantages（各方案缺点）

| 方案 | 缺点 |
|---|---|
| A | 棘轮豁免永久化——"只减不增"的豁免留着两个别人效仿的口子 |
| B | 历史行的 created_at 是哨兵不是真值（但这是**诚实**的：真值已不可考） |
| C | 用业务时间**冒充**写入时间——补录场景恰恰二者不同，这正是要加这列的原因；且回填既有列衍生值属"迁移替业务做决定"的边缘（CLAUDE.md §4），52 表先例也从未这么做 |

## Migration cost（迁移成本）

一条平台链迁移（两表各 add_column + batch 撤默认，`downgrade()` 对称
drop）；两个模型各加一行（`default=utcnow`，声明位置=迁移追加位置，
与 parity 测试口径一致）；`FROZEN_CORE_COLUMNS` 的 admissions 快照 +1 列
（引用本 ADR）；`BASELINE_MISSING_CREATED_AT` 2 → 0；`scripts/dump_schema.py`
重生成。响应字节零变化（无端点出参含该列）。

## Risk（风险）

- **做的风险**：几乎为零——纯加列不动存量数据，两端方言一致（SQLite 不能
  ADD COLUMN 带非常量默认，故用常量哨兵，先例已验证 52 次）；冻结快照同步
  更新后守卫恢复咬合。
- **不做的风险**：棘轮带着永久豁免运行，"新表必须带 created_at"的号召力
  打折；admissions 的写入时间维度继续缺失。

## Recommendation（结论）

**方案 B**。C 的"更好看的历史值"是伪造精度，A 是把例外常态化；B 让棘轮
真正归零，且每一行历史数据都诚实地写着"我不知道自己何时入库"。
