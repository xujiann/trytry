# API_MAP.md — 接口地图

> 881 个 HTTP 端点的分布、鉴权、契约与一致性现状。仅描述现状（AS-IS）。
> 来源：`server/app/routers/`（80 文件）+ `server/app/spd/routers/`（9 文件）+ `main.py` 注册。

---

## 1. 总量

| 指标 | 值 |
|---|---|
| HTTP 端点总数 | **881**（不含 4 个静态/健康检查 + 1 WebSocket） |
| 方法分布 | GET 425 / POST 390 / PATCH 50 / DELETE 15 / **PUT 1** |
| 路由文件 | 89（平台 80 + spd 9） |
| WebSocket | `/ws/notifications`（首帧鉴权 + `?token=` 兼容） |

## 2. 按域分组

| 域 | 文件数 | 端点数 | 主要前缀 |
|---|---:|---:|---|
| A 基础平台与主数据 | 15 | 66 | `/api/auth` `/api/patients` `/api/rbac` `/api/dictionaries` `/api`(users) |
| B 临床诊疗 | 19 | 164 | `/api/exams` `/api/inpatient` `/api/prescriptions` `/api/pharmacy` `/api/surgery` |
| C 公卫/慢病/妇幼 | 12 | 102 | `/api/chronic` `/api/maternal` `/api/vaccine-supply` `/api/education` |
| D 运营与质量管理 | 14 | 131 | `/api/mgmt` `/api/quality` `/api/workflows` `/api/materials` |
| E 财务医保 | 6 | 57 | `/api/billing` `/api/fund` `/api/insurance` `/api/accounting` |
| F 决策分析 | 2 | 15 | `/api/metrics` `/api/analytics` |
| G 集成对接 | 2 | 18 | `/api/esb` `/api/integration` |
| H 居民端 | 1 | 32 | `/api/portal` |
| I 补漏/杂项 | 3 | 57 | `/api/tcm` `/api/cssd` `/api/education` `/api`(裸) |
| J 慢专病子系统 | 9 | **239** | `/api/spd` `/api/portal/spd` |

## 3. 单文件 Top（行/端点比揭示"厚薄"）

| 文件 | 行数 | 端点 | 行/端点 | 前缀 |
|---|---:|---:|---:|---|
| `spd/routers/config.py` | 1547 | 58 | 27 | `/api/spd` |
| `routers/gapfill.py` | 1123 | 34 | 33 | **6 个不同前缀** |
| `routers/admin_mgmt.py` | 844 | 34 | 25 | `/api/mgmt` |
| `routers/portal.py` | 1332 | 32 | 42 | `/api/portal` |
| `spd/routers/care.py` | 1186 | 31 | 38 | `/api/spd` |
| `spd/routers/followup.py` | 1122 | 30 | 37 | `/api/spd` |
| `spd/routers/population.py` | 1397 | 29 | 48 | `/api/spd` |
| `spd/routers/workbench.py` | 956 | 8 | **120** | `/api/spd`（单端点塞大量逻辑） |
| `routers/analytics.py` | 683 | 10 | 68 | `/api/analytics` |
| `routers/billing.py` | 820 | 15 | 55 | `/api/billing` |

## 4. 前缀重叠（同一 URL 空间多文件瓜分）

| 前缀 | 文件 |
|---|---|
| `/api/tcm` | tcm.py + gapfill.tcm_router |
| `/api/cssd` | cssd.py + gapfill.cssd_router + service_extras.py |
| `/api/education` | education.py + gapfill.edu_router |
| `/api/maternal` | maternal.py + gapfill.maternal_router |
| `/api/performance` | performance.py + gapfill.perf_router（**鉴权分裂：director vs 仅登录**） |
| `/api/inpatient` | inpatient.py + clinical_docs.py |
| `/api`（裸） | encounters.py + users.py + service_extras.py |

> `gapfill.py` 与 `service_extras.py` 是按验收条目号（⑭⑥⑳㉑㉔㉟⑨）分区的"倾倒场"，各塞 7 个无关业务，挂到已有前缀。

## 5. 鉴权现状

| 类别 | 数量 | 说明 |
|---|---|---|
| 完全无认证端点 | 11 | login / esb(自有header鉴权) / portal 验证码登录·授权 / 价格公示·健康科普(合理) / 遗留免登录查档案(风险) |
| 有登录无角色守卫的写端点 | 30 | 10 个 portal(另有 scope 校验)，其余多为自助操作 |
| ESB 独立凭据 | — | `X-Esb-Endpoint`+`X-Esb-Token`，库内存散列，`secrets.token_urlsafe(24)` |

**鉴权结构缺口**：
- 权限点只覆盖写方法（GET 无法授权给自定义角色）；
- `require_admin` 不认权限点（配了不生效）；
- 未被 `require_roles` 守卫的读接口对自定义角色完全放开；
- 同前缀两套鉴权基线：`/api/performance` 在 performance.py 限 director，在 gapfill.perf_router 仅需登录。

**横向越权盲区**（按 id 取对象再取患者，绕过可见性且无留痕）：
`GET /api/print/{exam-reports,prescriptions,exam-requests,certs}/{id}`、`GET /api/attachments/{id}`、`GET /api/prescriptions`、`GET /api/chronic`、`GET /api/vaccine-supply/batches/{id}/recipients`。

## 6. 响应契约现状

| 指标 | 值 |
|---|---|
| `response_model=` 覆盖 | **124/881（14%）**，29/89 文件 |
| `schemas.py` 类 | 56（仅被 19/89 文件 import，只覆盖第一期 15 模块） |
| 路由内联 `BaseModel` | **309** |
| 手写 `_xxx_out()` 序列化 | **93**（名字撞车语义各异：`_rule_out`×4、`_template_out`×4、`_task_out`×3） |

**后果**：86% 端点在 OpenAPI 中响应体为空 schema；spd 的 239 端点几乎全部如此，外部对接方拿不到响应契约。

## 7. 响应信封一致性

| 项 | 现状 |
|---|---|
| 列表返回 | 225 处裸 `return [...]` / 58 处 `paginate()`（总数走 `X-Total-Count` 头）/ 少量 `{"total":...}` / 0 处 `{"items":...}` |
| 动作响应键 | 各自发明：`created`×7 / `removed`×3 / `deleted`×2 / `bound`×2 / `success`×3 / `logged_out`×2 |
| `X-Total-Count` | 三种来源（paginate 自动 / 手写 / 大量不设） |
| 分页 | `paginate` 仅 32/89 文件；210 处直接 `.limit()` 不走 paginate（会随数据量静默截断，如 `referrals.py:44` limit(200)） |

## 8. 状态流转风格分裂

| 风格 | 例子 |
|---|---|
| RESTful PATCH | `PATCH /api/referrals/{id}/status`（`_ALLOWED_TRANSITIONS` 字典） |
| RPC 动词 | `POST /api/spd/referrals/{id}/{review\|arrive\|down\|withdraw}` |
| 状态子资源 | `POST /api/spd/candidates/{id}/status` |

**RPC 动词泛滥**：80 个 `POST /{id}/<verb>` 端点（reprice/rotate-token/cancel-enroll/urge/escalate…），而 PATCH 仅 50、DELETE 仅 15，PUT 仅 1（`printing.py:387` 孤例）。

## 9. 命名一致性

| 项 | 评价 |
|---|---|
| 路径大小写 | ✅ 全英文 kebab-case，无拼音，复数集合，216 处一致 |
| tag | ✅ 全中文无重复 |
| docstring | ❌ 11 处位置错误（写在 `assert_org_writable` 之后不再是 docstring，OpenAPI 无描述） |
| 响应键 | ❌ 无统一信封 |

## 10. 跨路由重复实现（接口层）

| 重复项 | 证据 |
|---|---|
| 两套转诊接口 | `/api/referrals`(3 端点) vs `/api/spd/referrals*`(14 端点)。**写侧仍是两套**（ADR-0003 方案 C 待立项）；**读侧已聚合**：`GET /api/portal/me/referrals/all` 并成一份、居民端两个页面均取自它，两份互不相交列表的用户可感知孤岛已消除（ADR-0003 方案 B）|
| 三套随访接口 | `/api/chronic/{id}/followups` vs `/api/followups`(6) vs `/api/spd/followup-*`(11) |
| 两套专病目录 | `/api/disease-programs`(9) vs `/api/spd/programs`（创建逻辑逐字相同含报错文案） |
| SpdScreening 疑似判定 | 医护端 `high` 才算 vs 居民自查 `mid` 就算（阈值不一致） |
| 量表查询逻辑 | 3 文件逐字重复（population/portal/care） |
| 患者/机构查找 | `db.get(Patient,...)` 64 次、`"患者不存在"` 46 次、`"机构不存在"` 74 次 |
| 统计口径 | 54 个 stats/summary/overview 端点分散 30+ 文件，`reports.py:59` 与 `exams.py:494` 逐字相同 |

## 11. 已确认的接口 Bug

| Bug | 位置 |
|---|---|
| `/api/monitor/overview` 把每次成功当失败（`"success"` vs 写入端 `"succeeded"`） | `monitor.py:79` |
| SPD 报告"考核得分"段落忽略 org_id，各机构收到相同全域数据 | `spd/reporting.py:147` |
| `region_stats` 接收 `period` 参数从不使用（API 契约上的假参数） | `spd/routers/workbench.py:394` |

*本文件仅描述现状，未对任何代码进行修改。*
