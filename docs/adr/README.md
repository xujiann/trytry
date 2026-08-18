# 架构决策记录（ADR）

架构级变更在这里留档：一份决策一个文件，讲清**为什么这么定**，而不是只记"改了什么"。
配合 `CLAUDE.md §9` 使用。

## 什么改动需要 ADR

- 新增/移除子系统，改依赖方向；
- 换认证 / 迁移 / 加密机制；
- 动数据模型顶层决策（金额/日期/枚举策略、核心表结构、核心数据定义）；
- 破坏公共接口兼容性；
- 引入新的运行时依赖或部署形态。

> 触到"冻结"或"不可变定义"守卫（`test_schema_governance.py` / `test_core_data_invariants.py`）
> 变红时，正是需要写 ADR 的信号——先 ADR，再改快照/白名单。

## 每份 ADR 的结构（固定七段）

见 `0000-template.md`。七段：**problem / options / advantages / disadvantages /
migration cost / risk / recommendation**。

## 状态

`Proposed`（待评审）→ `Accepted`（采纳）/ `Rejected`（否决）→ 可被后续 ADR `Superseded`（取代）。
已采纳的 ADR 是**共享契约**，改它要再写一份新 ADR，不在原文上抹改结论。

## 编号规则

四位递增：`0001`、`0002`…；文件名 `NNNN-简短标题.md`。模板是 `0000`。

## 目录

| 编号 | 标题 | 状态 |
|---|---|---|
| [0001](0001-核心数据不可变定义与核心表冻结.md) | 核心数据不可变定义与核心表冻结 | Accepted |
| [0002](0002-生产迁移统一走alembic停用createall.md) | 生产迁移统一走 alembic，停用 create_all 建表 | Proposed |
| [0003](0003-三套并行子域的收敛策略.md) | 三套并行子域（慢病/专病/慢专病）的收敛策略 | Proposed |
