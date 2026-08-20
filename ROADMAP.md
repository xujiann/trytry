# ROADMAP

> 今天做什么，从这里挑。每日工作流见 `docs/日常开发工作流.md`。
> 排序：先安全止血（🔴），再让 CI 变真，再逐块治理/重构。**别自己另起题目——路线没有的，先加进来。**
> 来源与依据：`docs/TECH_DEBT.md`、`docs/模块分级_KEEP_IMPROVE_REFACTOR_REPLACE.md`、`docs/adr/`。

状态：☐ 待办 · ◐ 进行中 · ✅ 完成

---

## Now（本周期，优先）

### 🔴 安全止血（P0，独立于分级，尽快）
- ☐ `render.yaml` / `docker-compose.yml` 默认口令与守卫：改熵/长度校验，去掉 `admin123`/`change-me-in-production` 默认值（关联 ADR-0002 部署侧）。
- ✅ `routers/portal.py:168` `debug_code` 回显收紧为显式开关（新增 `sms_debug_echo` 默认关 + 生产双重门；回归测试 `test_portal_auth.py` 两条）。
- ☐ 打印 4 端点 + `attachments` 下载补 `assert_patient_visible`/`assert_obj_org_writable` 与 `AccessLog`。**需先定口径**：附件按 owner_type 分类（exam_report 患者 / adverse_event 机构 / course_material 全员）。
- ✅ `spd/routers/referral.py` 转诊审核补机构层级校验（口径 1：按机构树 `parent_id`，见 ADR-0004）：`review` 仅本单当前机构的**直接上级**可推进，全域角色放行；回归 `test_referral_review_requires_parent_org`。**后续（另案）**：① 机构树 `parent_id` 缺陷可见 —— ✅ `GET /api/organizations/tree-health` 体检接口（列 orphans + broken_chains + `referral_ready`，`test_org_tree_health.py`）；☐ 运行期"补齐"parent_id（需真实机构关系，属数据/运维口径，不自动种子）；② ✅ `_NEXT` 已收敛为村→乡→县三级（ADR-0005，存量 station_reviewed 兼容续走）；③ ✅ 机构校验已推广到 `arrive`/`down`/`receive-followup`（`_assert_holds_case`：本单当前持有机构才能操作，全域放行；回归 `test_referral_arrive_down_receive_require_current_org`）。
- ✅ `static/pages-mgmt.js` 会计科目等 `<option>` XSS 转义（含就近同类 4 处：会计科目 code/name、监测域 domain×2、流程定义 key）。

### 一行/小修（童子军级，碰到即修）
- ✅ `routers/monitor.py:79` `"success"` → `"succeeded"`（带回归测试 `test_monitor_overview.py`）。
- ⚠️ `spd/reporting.py:147` `_score` 忽略 `org_id`：**需先定口径**——`spd_scores` 无 org_id 列，考核对象按 object_type/object_id 归属机构的语义要读 assess.py 定，属"需业务决策"，不是一行小修。
- ✅ `scheduler.py` `_release_lock` 校验持有者，防误删他实例锁（token 所有权 + Lua 比对删，回归测试 `test_scheduler_lock.py`）。
- ✅ 调度双跑收窄：① 锁续期心跳（每 TTL/4 秒「仍持有才续期」，`_RENEW_LUA` 原子比对+expire；实例崩溃心跳停、锁 ≤TTL 自愈；易主即停并告警；续期失败 5s 快速重试）——超 TTL 长任务不再丢锁；② `tick()` 拿锁后**重新确认到期**——锁只保证不重叠、保证不了不重复，陈旧到期快照会让已被他实例跑过的任务再跑一遍。回归 `test_scheduler_lock.py` 四条。
- ☐ 手工触发端点 `routers/jobs.py` 的 `run_job` **不走执行锁**：与调度执行可并发同一任务（既有设计，非本轮引入）。定位为运维强制重跑口子，需决策是加锁校验还是文档化告警。/review 提出。
- ☐ 给 CI 加 Redis service，真跑 `_release_lock` 的 Lua 路径（现仅假 redis 验逻辑）。
- ☐ 迁移-模型**列级** parity 门：真 PG 上 `upgrade heads` 后对比 inspector 与 Base.metadata 的列集合（建表级已有 `test_模型表零漂移` 兜住；列漂移是 ADR-0002 已知残余缺口）。/review 提出。
- ✅ 机构树体检增报**层级错位**：`tree-health` 新增 `broken_chains`（转诊阶梯上父子层级不相邻的机构，带期望层级与自根到叶链路）与 `max_depth`（信息项）；`referral_ready` 收紧为"无孤儿且无错位"。判据是层级相邻而非链路长度：市级四层合法不误报、county/city 之上的挂载（县疾控、市协作院）不参与判定，而 county→村室→村室 三层照报。回归 `test_org_tree_health.py` 七条，改函数级 client 消除顺序耦合。
- ✅ 加固 `test_monitor_overview.py`：每条用例先清空 `job_runs` 再播种，seeded 行不会被别的失败记录挤出 limit-5 窗口（断言改精确相等）；另补一条钉住"只返回最近 5 条、按 id 倒序"的窗口语义。已验证单跑/乱序均绿，且把端点 limit 改小会让新用例转红。

### 工具链
- ✅ 清掉 8 处存量 lint（5 未用 import + 2 无占位 f-string + 1 未用变量改显式 assert），`make lint` 归零；`make verify` 的 typecheck 步改为渐进式 warning 不阻断——verify 现可用。
- ◐ mypy 存量 **187 → 139**（CI 口径，清掉 48 处 / 9 个文件；纯注解与推断收敛，零行为改动）：循环变量同名不同类型改名（`main.py`/`spd/seed.py` 种子块、`formula.py` 一元分支）、累加毫秒的 `module_duration` 由 `Counter`（值 int）换成 `defaultdict(float)`、混值字典就地标注 `dict[str, Any]`、`loinc_code` 形参补 `| None`、`deps.resolve_org_ids` 合并守卫让 mypy 能收窄。顺带两处就近修：`ApiMetrics.reset` 原本在持锁时调 `__init__` **把锁对象本身换掉**（改为抽 `_reset_counters()` 只清计数器）、`portal_logout` 补 `credentials is None` 显式 401 兜底（原依赖 `current_resident` 先行拦截的隐式不变量）。剩余 139 处以 ORM 相关为主（`dict(list[Row[...]])` 需标注、`db.get()/first()` 返回 `X | None` 未收窄），可继续逐块推进。
- ✅ **lint 转阻断**（CI 实测 ruff 0 项）；`requirements-dev.txt` 给 ruff/mypy 钉上界——本仓库无 lockfile，门一旦阻断就必须可复现（实测 mypy 2.3 报 139 处、1.19 在同一份代码上报 187 处，版本飘一下结论就变）。
- ✅ **mypy 环境探针 `scripts/check_mypy_env.py`（阻断）**：`ignore_missing_imports=true` 的代价是——mypy 解析不到的库会被**静默当成 Any**，依赖它的代码全部「通过」。本轮真踩到：开发机的 `mypy` 是 `uv tool install` 的隔离环境（没有 SQLAlchemy），同一份代码本地报 **41** 处、CI 报 **187** 处，差的 146 处全是 ORM 相关。探针先 `reveal_type` 探 sqlalchemy/pydantic，是 Any 就直接失败并给修复指引，杜绝再拿假绿下结论。
- ☐ **typecheck 转阻断**：待存量 139 清零后再切（`make verify` 与 CI 现均为 warning）。
- ☐ **CI 解释器与生产对齐**：生产镜像是 `python:3.12-slim`（根与 `server/` 两个 Dockerfile）、`pyproject.toml` 的 ruff `target-version`/mypy `python_version` 也是 3.12，但两个 CI job 都装 **3.11**——测的和发的不是同一个解释器。切换需带全量 3.12 实测，单独一个 PR 做。/review 提出。

### 让 CI 变真（关联 ADR-0002）
- ✅ 覆盖率门禁转阻断（落地实测 87%，门槛 70%）；集成/迁移门在真 PG 上转阻断。
- ✅ 生产禁用 `create_all`（lifespan 环境分支 + 守卫测试 `test_adr0002_create_all_guard.py`）、`start.sh` 启动前 `alembic upgrade heads`、README 修正复数（ADR-0002 已 Accepted）。

## Next（治理逐块推进，只进不退）

- ◐ 接口契约棘轮：按 `docs/接口标准与治理.md` 逐块迁移。已治理 11 模块（+checkups/certs/knowledge/notifications/infectious/dictionaries），基线 **757→742**；下一批候选 encounters（仅剩 `/archive/360` 视图，嵌套响应需谨慎建模）/ performance（`/orgs` 评分卡嵌套，float 折算需钉值）。
- ✅ `created_at` 欠账迁移**收官**：52 → **2**（16 个迁移批次，平台链+spd 链，全部常量默认回填→batch 撤默认范式，全程响应字节不变）。仅剩有意留置 2 张：`blood_stocks`（小型 upsert 表，价值低降级）、`admissions`（核心表，改列需先 ADR）。此后新表一律带 created_at（棘轮基线=2 顶住）。
- ☐ 测试隔离（既有 flake，非本轮引入）：部分用例在 `pytest -k` 子集下失败（`test_stage4_drgs::test_drg_stats_cmi_and_group_costs` KeyError、`test_modules::test_portal_identity_verification` IndexError），整模块/全量套件下通过——跨模块共享状态/顺序依赖，需修隔离（模块级 fixture 复用了共享库）。
- ☐ 三套并行子域：先做 ADR-0003 的**读侧聚合**——消除居民端两套 `referrals` 数据孤岛（先补三套特征化网）。

## Later（C 类重构，逐块 + 先补网）

- ☐ 拆倾倒场 `gapfill.py` / `service_extras.py` 回业务前缀（消 6 组前缀重叠 + 鉴权分裂）。
- ☐ 统计簇 `analytics/metrics/reports/performance` 合并口径。
- ☐ God 文件 `models.py`(3950) / `spd/routers/config.py`(1547) 分域拆包。
- ☐ 前端 89 个 render 抽 `panel()`/`crudPage()` 组件；合并三套 `$`/`esc`/`api`。

## 待决策（先 ADR，后动手）

- ✅ ADR-0002 生产迁移停用 create_all（Accepted，已实施）。
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
