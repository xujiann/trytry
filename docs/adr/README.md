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
| [0002](0002-生产迁移统一走alembic停用createall.md) | 生产迁移统一走 alembic，停用 create_all 建表 | Accepted |
| [0003](0003-三套并行子域的收敛策略.md) | 三套并行子域（慢病/专病/慢专病）的收敛策略 | Accepted |
| [0004](0004-转诊分级审核按机构树校验推进权限.md) | 转诊分级审核按机构树（parent_id）校验推进权限 | Accepted |
| [0005](0005-转诊链路收敛为三级.md) | spd 转诊链路收敛为村→乡镇→区市县三级 | Accepted |
| [0006](0006-倾倒场路由回归业务前缀.md) | 倾倒场路由（gapfill/service_extras）回归业务前缀 | Accepted |
| [0007](0007-统计簇口径合并.md) | 统计簇（analytics/metrics/reports/performance）口径合并 | Accepted |
| [0008](0008-God文件分域拆包.md) | God 文件（models.py / spd config.py）分域拆包 | Accepted |
| [0009](0009-前端组件抽取与工具函数合并.md) | 前端 render 组件抽取与三套工具函数合并 | Accepted |
| [0010](0010-患者档案注销列与个保法行权通道.md) | 患者档案注销列（deactivated_at）与个保法行权通道 | Accepted |
| [0011](0011-用户账号安全列与登录留痕.md) | 用户核心表安全四列与登录留痕（等保整改 E1） | Accepted |
| [0012](0012-PII列加密与检索索引.md) | PII 列加密存储与 HMAC 检索索引 | Accepted |
| [0013](0013-药品可用汇总的口径与批次同改约束.md) | 药品可用汇总的口径与批次同改约束 | Accepted |
| [0014](0014-病种目录收敛为单一权威表.md) | 病种目录收敛为单一权威表（ADR-0003 方案 C 第二阶段） | **Proposed** |
| [0015](0015-打印件防伪验真.md) | 打印件防伪验真：签名令牌二维码 + 公开最小披露核验 | Accepted |
| [0016](0016-审计落库移出事件循环.md) | 审计落库移出事件循环（await run_in_threadpool） | Accepted |
| [0017](0017-运行时依赖锁定.md) | 运行时依赖锁定（requirements.lock 钉版快照） | Accepted |
| [0018](0018-admissions补created_at列.md) | 冻结核心表 admissions 补 created_at（欠账收官） | Accepted |
