# DEPENDENCY_MAP.md — 依赖地图

> 内部模块依赖、循环依赖、子系统耦合、外部依赖。仅描述现状（AS-IS）。

---

## 1. 外部运行时依赖（`requirements.txt`，13 项）

| 依赖 | 用途 | 备注 |
|---|---|---|
| fastapi / uvicorn | Web 框架与 ASGI 服务器 | 单 worker |
| sqlalchemy | ORM | 单一 Base |
| pydantic / pydantic-settings | 校验与配置 | |
| python-multipart | 表单/上传 | |
| alembic | 迁移 | 双 head 分支 |
| psycopg2-binary | PostgreSQL 驱动 | |
| redis | 分布式状态（可选） | 未配置退化进程内存 |
| **pytest / pytest-cov** | 测试 | **被打进生产镜像** |
| httpx | 测试客户端 / 外呼 | |
| qrcode | 就诊凭据二维码 | |

**特征**：全部 `>=` 下界、无上界、无 lockfile。**缺失但被使用**：`playwright`（e2e）、`pytest-xdist`（并行）、`pytest-asyncio`（异步测试）均不在清单。**前端零依赖**（免构建）。

外部服务集成均为"双通道"（默认 stub / 生产真实现）：SMS（console/http）、微信（mock/official）、外呼（manual/http）、支付网关（Mock/待接）。

## 2. 内部依赖分层（理想 vs 实际）

**理想方向**（自底向上）：
```
database / config / clock / datetypes  (底座)
      ▲
concurrency / events / state_store / audit_chain / gmcrypto / privacy  (基础设施)
      ▲
deps / visibility / formula / rules / notify / ws / sms / wechat  (横切)
      ▲
models  (数据)
      ▲
schemas  (契约)
      ▲
routers  (接口/业务)  ←── 应无反向依赖
      ▲
main  (装配)
```

**实际偏离**：种子常量寄生在路由文件里，`main.py` 的 lifespan 反向 import：
- `main.py:130` `from .routers.dictionaries import SYSTEM_CODES`
- `main.py:158` `from .routers.performance import DEFAULT_INDICATORS`
- `main.py:171` `from .routers.infectious import SEED_DISEASES`
- `main.py:243` `from .routers.rbac import seed_builtin_roles, sync_permissions`

## 3. 循环依赖（存在，靠导入顺序 + 延迟 import 压住）

### 3.1 模型层导入环

```
app/models.py ──(文件末尾 line 3950: from .spd.models import *)──▶ app/spd/models.py
                                                                       │
app/models.py ◀──(spd/models.py:47: from ..models import Money, utcnow)┘
```
仅靠"import 写在 `models.py` 文件末尾"这一位置约定断开。上移即崩。由 `test_spd_boundary.py` 把例外钉死在 `Money`/`utcnow` 两个名字。

### 3.2 子系统适配层反向依赖平台路由（方向倒置 service→router）

```
spd/platform.py:49  from ..routers.attachments import register_owner, store_upload
spd/platform.py:51  from ..routers.portal import accessible_patient, current_resident
```
靠 `register_spd()` 把 9 个路由 import 全部推迟到函数体内执行来规避初始化期循环。

### 3.3 子系统内部分层倒置

```
spd/jobs.py:132     from .routers.care import dispatch_edu_push       (定时任务依赖路由)
spd/reporting.py:211 from .routers.assess import collect_metrics      (报告层依赖路由)
```
两个函数是领域服务，却定义在路由文件里，方向与子系统自立的规矩相反。

### 3.4 路由互相 import

**模块顶层（14 处）**：
```
access_logs.py:25   from .portal import current_resident_patient
esb.py:30-31        from .integration import parse_fhir_patient, parse_hl7v2_patient
                    from .patients import create_patient_idempotent
fund.py:44          from .performance import org_scorecards
integration.py:26   from .chronic import _evaluate_level        # ← import 私有函数
portal.py:72-74     from .appointments import book_slot, release_appointment
                    from .chronic import guidance_for
publichealth.py:19  from .chronic import guidance_for
reports.py:34       from .performance import org_scorecards
```

**函数体内延迟 import（循环规避标记，40 处，路由↔路由 3 处）**：
```
inpatient.py:292    from .drgs import assign_drg_group        ┐
inpatient.py:339    from .followups import create_task        ├ discharge() 一个函数
inpatient.py:379    from .billing import unsettled_amount     ┘ 延迟 import 3 个兄弟路由
```

**唯一做对的解耦**：`events.py` 事件总线（`inpatient.py:364`/`encounters.py:40` publish，spd subscribers 订阅，单向）——但全库只用了 2 次。

## 4. 主 app ↔ spd 子系统耦合

### 4.1 反向依赖（主 app → spd）：仅 4 处装卸性质，无循环业务依赖 ✅

```
main.py:105   from .spd import register_spd, seed_spd   (import)
main.py:235   seed_spd(db)                              (种子)
main.py:350   register_spd(app)                         (注册路由/订阅/任务)
models.py:3950 from .spd.models import *                (Base.metadata 注册)
config.py:66-80 5 个 spd_* 配置项                        (非 import，命名空间占用)
```
单向性由 `test_spd_boundary.py:103-120` 的 AST 扫描守住（`PLATFORM_TOUCHPOINTS` 只放行 models.py + main.py）。

### 4.2 正向依赖（spd → 主 app）

| 类别 | 数量 | 说明 |
|---|---|---|
| 模型/路由访问 | 收口在 `platform.py` | 由 AST 测试严格守住（正确处理相对 import 层级） |
| 基础设施直连 | **7 模块 × 9 路由 = 40+ 处** | clock/database/deps/datetypes/visibility/concurrency/formula 被各路由直连，不经 platform.py |
| 跨域外键 | 76 处 | users 36 / organizations 22 / patients 18 |

> **"platform.py 是唯一依赖入口"名不副实**：实为"模型/路由"唯一入口，基础设施仍被直连。

### 4.3 耦合强度评价

| 维度 | 强度 | 说明 |
|---|---|---|
| 代码耦合 | 低-中 | 模型访问收口，基础设施直连 |
| 共享基础设施 | 高 | 同进程/同 DB 连接池/同 Base.metadata/同认证 |
| 业务耦合 | 低 | 平台不知道子系统存在 |
| 边界成本（未记录） | — | `/api/rules/catalog` 等平台"统一"视图结构上永久无法覆盖 spd_* |

## 5. 命名冲突（依赖歧义源）

| 名称 | 冲突对象 |
|---|---|
| `spd/platform.py` | 遮蔽标准库 `platform`（当前无 `import platform`，定时炸弹） |
| `spd/rules.py` vs `app/rules.py` | 各有一个同名不同类的 `RuleError` |
| `spd/jobs.py` vs `app/jobs.py` | |
| `spd/models.py` vs `app/models.py` | `from ..models` 在不同深度含义相反 |
| `spd/routers/config.py` vs `app/config.py` | |
| `spd/routers/portal.py` vs `app/routers/portal.py` | |

## 6. 规则引擎依赖碎片（同一职责抽象 6 次）

| # | 模块 | 形式 | 使用方 |
|---|---|---|---|
| 1 | `app/formula.py` | AST 数值求值 | analytics / fund |
| 2 | `app/rules.py` | AST 条件 DSL | **仅 routers/rules.py 1 处** |
| 3 | `spd/rules.py` | 结构化 JSON 条件 | spd 纳入/排除/转诊/问卷 |
| 4 | `quality.py:438` `_check_record_rule` | 硬编码 if/elif | 病历质控 |
| 5 | `dataquality.py:295` `_EXECUTORS` | 执行器字典 | 数据质控 |
| 6 | `prescriptions.py:186` | 硬编码 | 审方规则 |

`app/rules.py` 宣称统一四套规则，实际一套都没迁移，只新增了第 5 套；spd 是第 6 套。

## 7. 前端依赖（加载顺序即依赖）

```
index.html: <script> 顺序 →
  core.js (公共层, 但含 15 个页面函数)
  pages-clinical.js / pages-mgmt.js / pages-spd.js / pages-public.js
  app.js (PAGES 注册表, 必须最后加载)
```
**分层倒置**：`core.js`（第一个加载）调用 `pages-clinical.js:194/203` 定义的 `formJson`/`postAction`——运行时侥幸工作（调用发生在全部脚本加载后），依赖方向反了。三套前端（管理/居民/医生）各自实现 `$`/`esc`/`api`，改一处另两处不跟。

*本文件仅描述现状，未对任何代码进行修改。*
