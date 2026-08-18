# ROADMAP

> 今天做什么，从这里挑。每日工作流见 `docs/日常开发工作流.md`。
> 排序：先安全止血（🔴），再让 CI 变真，再逐块治理/重构。**别自己另起题目——路线没有的，先加进来。**
> 来源与依据：`docs/TECH_DEBT.md`、`docs/模块分级_KEEP_IMPROVE_REFACTOR_REPLACE.md`、`docs/adr/`。

状态：☐ 待办 · ◐ 进行中 · ✅ 完成

---

## Now（本周期，优先）

### 🔴 安全止血（P0，独立于分级，尽快）
- ☐ `render.yaml` / `docker-compose.yml` 默认口令与守卫：改熵/长度校验，去掉 `admin123`/`change-me-in-production` 默认值（关联 ADR-0002 部署侧）。
- ☐ `routers/portal.py:168` `debug_code` 回显收紧为显式开关。
- ☐ 打印 4 端点 + `attachments` 下载补 `assert_patient_visible`/`assert_obj_org_writable` 与 `AccessLog`。
- ☐ `spd/routers/referral.py:393` 转诊审核补机构层级校验。
- ☐ `static/pages-mgmt.js:248` 会计科目 `<option>` XSS 转义。

### 一行/小修（童子军级，碰到即修）
- ✅ `routers/monitor.py:79` `"success"` → `"succeeded"`（带回归测试 `test_monitor_overview.py`）。
- ⚠️ `spd/reporting.py:147` `_score` 忽略 `org_id`：**需先定口径**——`spd_scores` 无 org_id 列，考核对象按 object_type/object_id 归属机构的语义要读 assess.py 定，属"需业务决策"，不是一行小修。
- ✅ `scheduler.py` `_release_lock` 校验持有者，防误删他实例锁（token 所有权 + Lua 比对删，回归测试 `test_scheduler_lock.py`）。
- ☐ 调度锁更深一层：任务跑过 TTL(300s) 仍会双跑（token 修的是"误删"，不是"双跑"）——需锁续期/心跳或任务时限。/review 提出。
- ☐ 给 CI 加 Redis service，真跑 `_release_lock` 的 Lua 路径（现仅假 redis 验逻辑）。
- ☐ 加固 `test_monitor_overview.py`：recent_failures 取 limit-5，理论上可能被其它失败记录挤掉 seeded 行（当前因间隔≥300s 安全）。/review 提出。

### 工具链
- ☐ `make verify` 的 lint 步会因 8 处**存量** lint 欠账（`tests/test_spd_integration.py` 等未改文件）中止，挡住无关改动的自检。方案二选一：lint 只查改动文件，或先一次性清掉这 8 处存量（不夹带进功能提交）。

### 让 CI 变真（关联 ADR-0002）
- ☐ 覆盖率门禁去 `|| true`，转阻断；集成/迁移门在真 PG 上转阻断。
- ☐ 生产禁用 `create_all`、部署产物补 `alembic upgrade heads`（**先把 ADR-0002 从 Proposed 推进到 Accepted**）。

## Next（治理逐块推进，只进不退）

- ◐ 接口契约棘轮：按 `docs/接口标准与治理.md` 逐块迁移。已治理 8 模块（+checkups/certs/knowledge），基线 **757→749**；下一批 notifications / todos / eldercare / blood / infectious。
- ☐ `created_at` 欠账迁移：按 `docs/数据模型治理.md` 背包清单，从台账/流水表（`admissions` 等）起，基线 52 往下走。
- ☐ 三套并行子域：先做 ADR-0003 的**读侧聚合**——消除居民端两套 `referrals` 数据孤岛（先补三套特征化网）。

## Later（C 类重构，逐块 + 先补网）

- ☐ 拆倾倒场 `gapfill.py` / `service_extras.py` 回业务前缀（消 6 组前缀重叠 + 鉴权分裂）。
- ☐ 统计簇 `analytics/metrics/reports/performance` 合并口径。
- ☐ God 文件 `models.py`(3950) / `spd/routers/config.py`(1547) 分域拆包。
- ☐ 前端 89 个 render 抽 `panel()`/`crudPage()` 组件；合并三套 `$`/`esc`/`api`。

## 待决策（先 ADR，后动手）

- ☐ ADR-0002 生产迁移停用 create_all（Proposed → 待批）。
- ☐ ADR-0003 三套并行子域收敛（Proposed → 待批；先做读侧聚合那步）。

---

## Done（已完成，倒序）

- ✅ 童子军法则写入 CLAUDE.md §12。
- ✅ ADR 体系建立（`docs/adr/`：README + 模板 + 0001/0002/0003）。
- ✅ 核心数据不可变定义 + 守卫（`test_core_data_invariants.py`）。
- ✅ 数据模型治理：SCHEMA 输出、核心表冻结、created_at 棘轮。
- ✅ 接口标准治理棘轮 + 样板迁移（checkups）。
- ✅ 特征化测试指引 + spd/rules 样板网。
- ✅ 模块分级 A/B/C/D。
- ✅ 六项能力（build/lint/typecheck/unit/integration/smoke）+ Makefile + CI 接入。
- ✅ AS-IS 架构审计 + 六张地图。
