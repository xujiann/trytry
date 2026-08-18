# MODULE_MAP.md — 模块地图

> 后端模块、业务域、子系统与前端资产的清单与职责。仅描述现状（AS-IS）。
> 规模：`server/app` 46,727 行；其中 `spd/` 13,593 行（约 29%）。

---

## 1. 顶层布局

```
server/app/
├── main.py            入口/装配（路由注册 + lifespan + 中间件）        517 行
├── config.py          配置（MEDPLAT_* / pydantic-settings）           109 行
├── database.py        唯一 Base + engine
├── models.py          全部平台 ORM（187 表）                          3950 行
├── schemas.py         Pydantic 出入参（仅第一期 15 模块）             427 行
├── deps.py            认证/鉴权/分页/机构范围（横切）                  216 行
├── visibility.py      横向数据隔离                                     461 行
├── security.py / gmcrypto.py / audit_chain.py / privacy.py            安全底座
├── concurrency.py     并发原子写原语（7 个）                          195 行
├── scheduler.py / jobs.py / clock.py / state_store.py                 调度与状态
├── events.py / notify.py / ws.py / sms.py / wechat.py / monitor.py    事件与通知
├── formula.py / rules.py                                              规则/表达式工具
├── data/              6 个种子模块（drug_rules/drg/qc/record_qc/account_subjects）
├── routers/           80 个平台路由                                   25,059 行
├── spd/               慢专病子系统（独立包）                          13,593 行
└── static/            免构建 SPA（管理端 + m/ 居民端 + m/doctor）      9,531 行
```

## 2. 横切基础设施模块（被路由广泛依赖）

| 模块 | 职责 | 质量/覆盖 |
|---|---|---|
| `deps.py` | `get_current_user` / `require_roles` / `require_admin` / `paginate` / `resolve_org_scope` | 核心；paginate 仅 32/89 路由使用 |
| `visibility.py` | 机构三档可见性 + 患者六类关系推导 + AccessLog 留痕 | 注释诚实；仅 48/89 路由接入 |
| `concurrency.py` | `insert_or_conflict` / `upsert_unique` / `add_amount` / `take_amount` / `claim_quota` 等 7 个原子写 | 高质量，模块文档记录 3 次真实事故 |
| `events.py` | 同进程/同事务/同步发布订阅，2 个已知事件 | 白名单防拼错；仅 2 处 publish |
| `audit_chain.py` | 审计哈希链 MAC | 与 JWT 复用同一密钥；可被末尾截断 |
| `clock.py` | naive UTC 统一时间源 | 记录了 aware/naive 混用事故 |
| `state_store.py` | Redis/内存双态（黑名单/防爆破/限流/任务锁） | `MEDPLAT_REDIS_URL` 绕过 Settings |
| `formula.py` | AST 白名单数值求值 | 绩效公式/基金分配 |
| `rules.py` | 条件 DSL（基于 formula） | **仅 1 处使用**，宣称统一四套规则但一套没迁 |
| `privacy.py` | 身份证/手机脱敏（仅出口） | admin 唯一豁免 |

## 3. 平台业务域（80 路由，按域归组）

| 域 | 代表路由文件 | 前缀 |
|---|---|---|
| 基础平台与主数据 | auth, users, rbac, patients, dictionaries, organizations, org_groups | `/api/auth` `/api/patients` `/api/rbac` … |
| 临床诊疗 | encounters, exams, inpatient, clinical_docs, prescriptions, pharmacy, surgery, pathology, medication, telemedicine, tcm | `/api/exams` `/api/inpatient` … |
| 公卫/慢病/妇幼 | chronic, followups, maternal, vaccination, vaccine_supply, infectious, surveillance, eldercare, education, publichealth | `/api/chronic` `/api/maternal` … |
| 运营与质量管理 | admin_mgmt, staffing, quality, dataquality, workflows, materials, cssd, medwaste, projects, credentials | `/api/mgmt` `/api/quality` … |
| 财务医保 | billing, fund, insurance, accounting, cost, drgs | `/api/billing` `/api/fund` … |
| 决策分析 | metrics, analytics, reports, performance | `/api/metrics` `/api/analytics` |
| 集成对接 | esb, integration, rules | `/api/esb` `/api/integration` |
| 居民端 | portal | `/api/portal` |
| 通用能力 | attachments, notifications, todos, printing, knowledge, monitor, access_logs, jobs, certs, checkups, blood, emergency, consultations, contracts, appointments | 各自前缀 |
| **补漏/杂项（倾倒场）** | **gapfill, service_extras** | 挂到已有前缀 |

## 4. 慢专病子系统（spd）内部结构

```
spd/
├── __init__.py     register_spd(app) 装卸入口：注册9路由+导入jobs+订阅事件+种子   77 行
├── platform.py     对平台模型/路由的唯一适配层（再导出 + 承载"平台数据形状"）    213 行
├── service.py      领域服务层：build_facts/spawn_task/start_path/advance_path/    509 行
│                   award_points/close_open_work/sweep_overdue（跨路由复用的6动作）
├── rules.py        纯函数规则求值（validate/evaluate/screen/judge_level/score_scale/grade） 277 行
├── collectors.py   数据采集器注册表（collect_publichealth 真实 / collect_internal 只count） 196 行
├── subscribers.py  2 个事件订阅（出院派生随访 / 就诊识别疑似）                    159 行
├── callcenter.py   外呼 Provider 协议 + manual/http 双实现                        84 行
├── reporting.py    报告段落渲染注册表（11 渲染器，与考核指标同源）                252 行
├── jobs.py         4 个定时任务                                                  158 行
├── seed.py         8 病种/量表/指标/积分/随访方案/报告模板 幂等种子              561 行
├── models.py       59 张 spd_* 表                                                1398 行
└── routers/        9 个路由（8674 行）
    ├── config.py       配置域：16 类实体 CRUD（病种/目标/路径/量表/宣教/团队/村医/设备…）1547 行
    ├── population.py    人群域：筛查→目标池→纳管→生命周期/分组/服务包            1397 行
    ├── care.py         服务域：监测/评估/干预/宣教/复诊/上报/健康处方/在线咨询    1186 行
    ├── followup.py     随访+报告（自陈为全院通用能力）                           1122 行
    ├── assess.py       考核域：指标库/考核计分/积分商城                          1099 行
    ├── portal.py       居民端（/api/portal/spd）                                956 行
    ├── workbench.py    只读聚合看板                                             956 行
    ├── tasks.py        路径与统一任务                                           808 行
    └── referral.py     转诊域：规则/病例/步骤                                   632 行
```

**职责边界**：8 个路由共用 `/api/spd`，1 个用 `/api/portal/spd`。内部按领域分层（platform/service/rules/collectors 是好的抽象），但存在两处分层倒置（`jobs.py`/`reporting.py` 反向 import `routers/`）。

## 5. 前端资产（3 套独立 SPA，代码零复用）

| 入口 | 文件 | 行数 | 机制 |
|---|---|---|---|
| 管理端 `/` | `core.js` | 1089 | 公共层（含 15 个页面函数）+ `PAGES` 注册表 hash 路由 |
| | `app.js` | 127 | `PAGES` 96 项数组（group 标题 + 页面项混装），必须最后加载 |
| | `pages-clinical.js` | 1840 | 临床页面 render |
| | `pages-mgmt.js` | 1801 | 管理页面 render |
| | `pages-spd.js` | 1289 | 慢专病页面 render |
| | `pages-public.js` | 926 | 公卫页面 render |
| 居民端 `/m` | `m/m.js` | 1113 | `switchTab` 切 6 tab，独立 `$`/`esc`/`api` |
| 医生端 `/m/doctor` | `m/doctor.js` | 701 | 七页签，token 存 sessionStorage |

**共同模板**：89 个 `renderX` 手抄"面板+表单+表格+msg+onsubmit+route()"，无 `panel()`/`crudPage()` 抽象。三套前端各自实现 `$`/`esc`/`api`（逐字相同）。

## 6. 模块边界评价

| 评价 | 说明 |
|---|---|
| ✅ spd 单向依赖 | 主 app → spd 仅 4 处装卸依赖，无循环业务依赖，AST 测试守住 |
| ✅ 横切抽象到位 | deps/visibility/concurrency/events 抽象正确 |
| ⚠️ 抽象推广不足 | paginate 32/89、visibility 48/89、events 2 处、rules 1/5 |
| ⚠️ 无领域层 | 业务逻辑内联路由（db.query 1003 次） |
| ❌ 通用能力困在子系统 | followup 随访/报告随 spd 一起被卸载 |
| ❌ 倾倒场文件 | gapfill/service_extras 按验收条目号分区，7 业务塞一文件 |
| ❌ URL 空间不可见分层 | 8 个 spd 文件共用 `/api/spd`，213 端点平铺 |

*本文件仅描述现状，未对任何代码进行修改。*
