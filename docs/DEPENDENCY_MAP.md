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

（2026-10-05 按 82dbc09 重列：对 `server/app/routers/*.py` 做 AST 扫描，取 `from .<兄弟路由> import …`。此前写的「模块顶层 14 处 / 函数体内延迟 import 40 处、路由↔路由 3 处」是更早的快照，没写日期与 commit。）

**模块顶层（61 处）**：
```
access_logs.py:27      from .portal import current_resident, current_resident_patient
analytics.py:40        from .dispense import prescription_not_reversed
analytics.py:41        from .organizations import ORG_LEVEL_NAMES
certs.py:20            from .printing import CERT_DATE_LABELS
certs.py:21            from .reports import _csv_response
consents.py:49         from .prescriptions import CHILD_AGE_LIMIT, _age_of
cost.py:36             from .admin_mgmt import DEPT_CATEGORY_NAMES
dataquality.py:42      from .exams import CRITICAL_STATUS_NAMES
dispense.py:45         from .prescriptions import PRESCRIPTION_STATUS_NAMES
encounters.py:22       from .checkups import _abnormal_item_names, abnormal_text as checkup_abnormal_text
encounters.py:23       from .dispense import prescription_not_reversed
encounters.py:24       from .patients import find_by_ehc_no
encounters.py:25       from .prescriptions import PRESCRIPTION_STATUS_NAMES
esb.py:41              from .integration import parse_fhir_patient, parse_hl7v2_patient
esb.py:42              from .patients import create_patient_idempotent
fund.py:47             from .performance import DEFAULT_VOLUME_CAP, org_scorecards
infectious.py:15       from .reports import _csv_response
insurance.py:28        from .referrals import STATUS_LABELS as REFERRAL_STATUS_LABELS
integration.py:66      from .chronic import FIELD_DISEASE, _evaluate_level
integration.py:67      from .encounters import create_encounter
integration.py:68      from .dataquality import id_card_invalid_reason
integration.py:69      from .exams import EXAM_REQUEST_STATUS_NAMES, submit_report
integration.py:70      from .inpatient import AdmissionCreate, _mark_discharged, _release_bed, create_admission, spawn_discharge_followup
integration.py:72      from .patients import create_patient_idempotent, id_card_match
medication.py:16       from .dispense import prescription_not_reversed, q_dispensable_shortage
metrics.py:33          from .dispense import q_dispensable_shortage
metrics.py:34          from .encounters import ENCOUNTER_TYPE_NAMES
metrics.py:35          from .exams import CRITICAL_STATUS_NAMES, EXAM_REQUEST_STATUS_NAMES
metrics.py:36          from .infectious import INFECTIOUS_CATEGORY_NAMES
metrics.py:37          from .medwaste import WASTE_STATUS_NAMES, WASTE_TYPES
metrics.py:38          from .medwaste import overdue_condition as medwaste_overdue_condition
metrics.py:39          from .prescriptions import PRESCRIPTION_STATUS_NAMES
metrics.py:40          from .printing import CENTER_NAMES, REFERRAL_DIRECTION_NAMES
metrics.py:41          from .referrals import STATUS_LABELS as REFERRAL_STATUS_NAMES
org_groups.py:18       from .organizations import ORG_LEVEL_NAMES
pathology.py:29        from .exams import EXAM_REQUEST_STATUS_NAMES
pharmacy.py:64         from .dispense import _claim_batch, _fefo_batches, _required_quantity, batch_available, batch_dispensable, broadcast_if_crossed, broadcast_shortage, dispensable_by_drug, prescription_not_reversed, q_dispensable_shortage, q_stock_dispensable
portal.py:102          from .appointments import book_slot, release_appointment
portal.py:103          from .auth import record_login_event
portal.py:106          from .billing import CHARGE_CATEGORY_NAMES, DEPOSIT_METHODS, DEPOSIT_TYPES, deposit_balance, self_pay_outstanding
portal.py:107          from .consents import SCENE_PATTERN, ConsentOut, CorrectionOut, active_text_version, consent_out, correction_out, has_active_delegate_consent, require_guardian_for_minor, validate_correction_changes
portal.py:118          from .notifications import NotificationOut, UnreadCountOut, notification_out
portal.py:119          from .chronic import guidance_for
portal.py:120          from .education import ARTICLE_CATEGORY_NAMES
portal.py:121          from .surgery import room_labels
printing.py:106        from .referrals import STATUS_LABELS as REFERRAL_STATUS_NAMES
printing.py:110        from .exams import EXAM_REQUEST_STATUS_NAMES, EXAM_SAMPLE_STATUS_NAMES
printing.py:111        from .prescriptions import PRESCRIPTION_STATUS_NAMES
printing.py:112        from .consents import GUARDIAN_RELATION_NAMES
printing.py:113        from .checkups import abnormal_text as checkup_abnormal_text
printing.py:114        from .drgs import drg_label
publichealth.py:22     from .chronic import guidance_for
publichealth.py:23     from .vaccination import _effective_contraindications
quality.py:55          from .surgery import operation_day
reports.py:36          from .performance import DEFAULT_VOLUME_CAP, org_scorecards
resources.py:44        from .surgery import room_occupancy
surgery.py:29          from .followups import FOLLOWUP_TITLE_MAX
telemedicine.py:15     from .dispense import prescription_not_reversed
telemedicine.py:16     from .prescriptions import PRESCRIPTION_STATUS_NAMES
todos.py:21            from .dispense import q_dispensable_shortage
workflows.py:39        from .blood import COMPONENT_NAMES
```

多数是同一口径只在一处定义的取值表与判定帮手（状态中文名、`prescription_not_reversed`、`q_dispensable_shortage` 等，修「前后端 / 多处各维护一份」时收拢过来的）；被引最多的是 `dispense`、`prescriptions`、`exams`。import 下划线开头私有名字的 8 处：certs.py ← reports._csv_response；consents.py ← prescriptions._age_of；encounters.py ← checkups._abnormal_item_names；infectious.py ← reports._csv_response；integration.py ← chronic._evaluate_level；integration.py ← inpatient._mark_discharged/_release_bed；pharmacy.py ← dispense._claim_batch/_fefo_batches/_required_quantity；publichealth.py ← vaccination._effective_contraindications。

**函数体内延迟 import（routers 目录共 13 处，路由↔路由 5 处，都在 `inpatient.py`）**：
```
inpatient.py:165       from .followups import DISCHARGE_FOLLOWUP_DAYS, create_task # spawn_discharge_followup()
inpatient.py:551       from .drgs import assign_drg_group                   # create_case_summary()
inpatient.py:570       from .drgs import drg_label                          # get_case_summary()
inpatient.py:629       from .followups import DISCHARGE_FOLLOWUP_DAYS       # discharge_admission()
inpatient.py:660       from .billing import unsettled_amount                # _assert_billing_settled()
```

**唯一做对的解耦**：`events.py` 事件总线（`encounters.py:53` / `inpatient.py:317` 发 `ENCOUNTER_CREATED`，`inpatient.py:643` / `integration.py:1002` 发 `ADMISSION_DISCHARGED`，spd subscribers 订阅，单向）——全库只有这 4 处发布。

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
