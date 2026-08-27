"""紧密型县域医共体信息化平台 —— 第一期基础平台服务。

已实现模块（对应《县域医共体信息化平台建设规划》第一期）：
- 统一认证（JWT 令牌，角色权限）
- 医共体成员单位（机构）管理
- 患者主索引 EMPI / 电子健康卡号
- 统一编码字典（诊断、药品、耗材、收费"四统一"）
- 双向转诊状态流转
"""
import asyncio
import contextlib
import json
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from .config import settings
from .database import Base, SessionLocal, engine
from .monitor import metrics as monitor_metrics
from .models import User
from .routers import (
    access_logs,
    admin_mgmt,
    appointments,
    attachments,
    auth,
    billing,
    blood,
    certs,
    checkups,
    chronic,
    consents,
    consultations,
    contracts,
    dataquality,
    cssd,
    dictionaries,
    dispense,
    drgs,
    education,
    homevisits,
    eldercare,
    emergency,
    encounters,
    accounting,
    analytics,
    clinical_docs,
    cost,
    esb,
    followups,
    jobs as jobs_router,
    materials,
    credentials,
    monitor,
    disease_programs,
    fund,
    notifications,
    staffing,
    org_groups,
    outpatient_docs,
    workflows,
    exams,
    infectious,
    inpatient,
    insurance,
    integration,
    knowledge,
    labqc,
    maternal,
    medication,
    medwaste,
    metrics,
    organizations,
    patients,
    performance,
    pharmacy,
    pathology,
    portal,
    prescriptions,
    projects,
    printing,
    publichealth,
    quality,
    rbac,
    referrals,
    rules,
    surgery,
    surveillance,
    reports,
    resources,
    surveys,
    tcm,
    tcm_heritage,
    telemedicine,
    todos,
    triage,
    users,
    vaccination,
    vaccine_supply,
)
from . import ws
from .spd import register_spd, seed_spd
from .audit_chain import audit_entry_hash
from .deps import token_for_audit
from .models import AuditLog
from .security import decode_token, hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
    # ADR-0002：生产环境停用 create_all，结构变更统一走 alembic（部署产物在启动前
    # 执行 `alembic upgrade heads`）。create_all 只建"不存在的表"、不改列——漏写迁移
    # 时开发 SQLite 看起来正常、生产 PG 上线才炸（历史已发生过）。开发/测试仍保留
    # 零配置起库。
    if not settings.is_production:
        Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == "admin").first() is None:
            # 公网部署时通过 MEDPLAT_ADMIN_PASSWORD 指定初始密码，避免默认口令暴露
            initial_password = settings.admin_password
            db.add(
                User(
                    username="admin",
                    password_hash=hash_password(initial_password),
                    full_name="平台管理员",
                    role="admin",
                )
            )
            db.commit()
        # M5 整改：字典基础数据（CodeSystem）启动时种子化，读路径不再产生写副作用
        from .models import CodeSystem
        from .routers.dictionaries import SYSTEM_CODES

        existing_codes = {code for (code,) in db.query(CodeSystem.code).all()}
        for code, name in SYSTEM_CODES.items():
            if code not in existing_codes:
                db.add(CodeSystem(code=code, name=name))
        db.commit()
        # 块3：标准字典种子扩充——常用 ICD-10 诊断 100 条 + 常用药品 50 条（幂等）
        from .dict_seed import SEED_COMMON_DRUGS, SEED_ICD10_DIAGNOSES
        from .models import CodeEntry

        for system_code, seed_entries in (
            ("diagnosis", SEED_ICD10_DIAGNOSES),
            ("drug", SEED_COMMON_DRUGS),
        ):
            system = db.query(CodeSystem).filter(CodeSystem.code == system_code).first()
            if system is None:  # pragma: no cover - 上一块刚种过，查不到只可能是被并发删了
                continue
            existing_entries = {
                code
                for (code,) in db.query(CodeEntry.code)
                .filter(CodeEntry.system_id == system.id)
                .all()
            }
            for code, name in seed_entries:
                if code not in existing_entries:
                    db.add(CodeEntry(system_id=system.id, code=code, name=name))
        db.commit()
        # 深化轮：绩效指标目录种子化（现有5维权重入表，管理层可调）
        from .models import PerformanceIndicator
        from .routers.performance import DEFAULT_INDICATORS

        existing_keys = {key for (key,) in db.query(PerformanceIndicator.key).all()}
        for key, meta in DEFAULT_INDICATORS.items():
            if key not in existing_keys:
                db.add(
                    PerformanceIndicator(
                        key=key, name=meta["name"], weight=meta["weight"], active=True
                    )
                )
        db.commit()
        # 深化轮：法定传染病目录种子化（甲类2小时/乙丙类24小时报告时限）
        from .models import InfectiousDisease
        from .routers.infectious import SEED_DISEASES

        existing_diseases = {code for (code,) in db.query(InfectiousDisease.code).all()}
        for seed in SEED_DISEASES:
            if seed["code"] not in existing_diseases:
                db.add(InfectiousDisease(**seed))
        db.commit()
        # 块2：审方规则库种子化（50 条常用药品规则，幂等；已存在编码不覆盖本地调整）
        from .data.drug_rules_seed import SEED_DRUG_RULES
        from .models import DrugRule

        existing_rules = {code for (code,) in db.query(DrugRule.drug_code).all()}
        for seed in SEED_DRUG_RULES:
            if seed["drug_code"] not in existing_rules:
                db.add(DrugRule(**seed))
        db.commit()
        # 块1：慢病病种目录种子化（8 个县域重点病种，含分级规则/指导要点/随访周期）
        from .chronic_seed import SEED_CHRONIC_DISEASE_TYPES
        from .models import ChronicDiseaseType

        existing_chronic_types = {code for (code,) in db.query(ChronicDiseaseType.code).all()}
        for seed in SEED_CHRONIC_DISEASE_TYPES:
            if seed["code"] not in existing_chronic_types:
                db.add(ChronicDiseaseType(**seed))
        db.commit()
        # M12/块3：DRG 分组目录种子化（62 个县域常见组 + QY 兜底组，admin 可调权）
        from .data.drg_groups_seed import FALLBACK_DRG_GROUP, SEED_DRG_GROUPS
        from .models import DrgGroup

        existing_drgs = {code for (code,) in db.query(DrgGroup.code).all()}
        for seed in [*SEED_DRG_GROUPS, FALLBACK_DRG_GROUP]:
            if seed["code"] not in existing_drgs:
                db.add(DrgGroup(**seed))
        db.commit()
        # 块3：数据质控规则种子化（15 条，幂等；已存在编码不覆盖本地调整）
        from .data.qc_rules_seed import SEED_QC_RULES
        from .models import QcRule

        existing_qc = {code for (code,) in db.query(QcRule.code).all()}
        for qc_rule in SEED_QC_RULES:
            if qc_rule["code"] not in existing_qc:
                db.add(QcRule(**qc_rule))
        db.commit()
        # 块2：病历环节质控规则种子化（12 条，幂等；已存在编码不覆盖本地调整）
        from .data.record_qc_rules_seed import SEED_RECORD_QC_RULES
        from .models import RecordQcRule

        existing_mrqc = {code for (code,) in db.query(RecordQcRule.code).all()}
        for record_qc_rule in SEED_RECORD_QC_RULES:
            if record_qc_rule["code"] not in existing_mrqc:
                db.add(RecordQcRule(**record_qc_rule))
        db.commit()
        # T3.1：会计科目种子（医院会计制度常用一级科目，幂等；不覆盖本地调整）
        from .data.account_subjects_seed import SEED_ACCOUNT_SUBJECTS
        from .models import AccountSubject

        existing_subjects = {code for (code,) in db.query(AccountSubject.code).all()}
        for subject in SEED_ACCOUNT_SUBJECTS:
            if subject["code"] not in existing_subjects:
                db.add(AccountSubject(**subject))
        db.commit()
        # 全域慢专病子系统：病种规则、管理目标、筛查量表、考核指标、积分规则、
        # 随访方案与问卷、报告模板（幂等，按编码不覆盖现场调过的参数）；
        # 子系统未启用时这一步什么都不做
        seed_spd(db)
        # E2 个保法：知情同意文本按场景各种一版默认文本（幂等只增，不覆盖现场修订）
        from .routers.consents import seed_consent_texts

        seed_consent_texts(db)
        # T1.1：把代码中注册的定时任务同步进库（幂等，不覆盖运维调过的参数）
        from . import jobs as _jobs  # noqa: F401 - 导入即完成任务注册
        from .scheduler import scheduler_loop, sync_registry

        sync_registry(db)
        # PII 检索索引自检（启动期探一次；日常由 jobs.pii_index_health 定时跑）。
        # 放在启动期是因为索引破损的两条来路都发生在**部署那一刻**：迁移拿默认
        # 密钥算索引、或库已加密后重跑迁移把密文行整片跳过——等到 24 小时后的
        # 定时任务才发现，中间这一天已经重复建档了。
        # **告警不拒启**的取舍写在 jobs.report_pii_index_health 的 docstring 里
        # （与 config.py "多实例无 Redis 拒启" 的差别一并写在那儿）。
        # 探针自身绝不可打断启动：查不动（表还没建/权限不足）只记日志。
        try:
            _jobs.report_pii_index_health(db)
        except Exception:  # noqa: BLE001 - 自检是旁路，失败不能拖垮启动
            logging.getLogger("medplat.jobs").warning(
                "[PII] 启动期检索索引自检未能完成，本次跳过（定时任务会再探）", exc_info=True
            )
        # 阶段十一：内置六角色预置 + 权限点从路由表自动登记（幂等）。
        # 手工维护的权限点清单与真实接口的偏差，是这类系统最难查的问题之一。
        from .routers.rbac import seed_builtin_roles, sync_permissions

        seed_builtin_roles(db)
        sync_permissions(app, db)
    finally:
        db.close()

    # 后台调度循环随应用启停；测试用 TestClient 也会走到这里，
    # 但首个 tick 在 30 秒后，单测早已结束，不会产生干扰。
    scheduler_task = asyncio.create_task(scheduler_loop())
    try:
        yield
    finally:
        scheduler_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler_task


app = FastAPI(
    title="县域医共体信息化平台",
    version="0.1.0",
    description="第一期：基础平台与数据中心（统一认证、机构、EMPI、编码字典、双向转诊）",
    lifespan=lifespan,
    # 生产关闭 API 文档面：/docs /redoc /openapi.json 会把 881 个端点的完整
    # 结构（含入参形状与鉴权盲区）展示给任何能连到服务的人，是越权探测的现成
    # 地图。开发/演示环境保留默认路径。附录/契约生成脚本走的是
    # `app.openapi()`（进程内调用），不依赖这三个 HTTP 路由，不受影响。
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(patients.router)
app.include_router(consents.router)
app.include_router(dictionaries.router)
app.include_router(referrals.router)
app.include_router(encounters.router)
app.include_router(exams.router)
app.include_router(prescriptions.router)
app.include_router(pharmacy.router)
app.include_router(dispense.router)
app.include_router(chronic.router)
app.include_router(infectious.router)
app.include_router(consultations.router)
app.include_router(contracts.router)
app.include_router(appointments.router)
app.include_router(cssd.router)
app.include_router(medwaste.router)
app.include_router(performance.router)
app.include_router(metrics.router)
app.include_router(portal.router)
app.include_router(access_logs.router)
app.include_router(users.router)
app.include_router(emergency.router)
app.include_router(telemedicine.router)
app.include_router(tcm.router)
app.include_router(medication.router)
app.include_router(inpatient.router)
app.include_router(billing.router)
app.include_router(blood.router)
app.include_router(certs.router)
app.include_router(checkups.router)
app.include_router(insurance.router)
app.include_router(integration.router)
app.include_router(knowledge.router)
app.include_router(education.router)
app.include_router(eldercare.router)
app.include_router(maternal.router)
app.include_router(vaccination.router)
app.include_router(vaccine_supply.router)
app.include_router(pathology.router)
app.include_router(tcm_heritage.router)
app.include_router(resources.router)
app.include_router(rbac.router)
app.include_router(projects.router)
app.include_router(surveillance.router)
app.include_router(publichealth.router)
app.include_router(quality.router)
app.include_router(labqc.router)
app.include_router(drgs.router)
app.include_router(admin_mgmt.router)
app.include_router(attachments.router)
app.include_router(reports.router)
app.include_router(printing.router)
app.include_router(dataquality.router)
# 块1：集成平台底座 ESB（接入方注册/消息队列/流程编排/统计）
app.include_router(esb.router)
app.include_router(jobs_router.router)
app.include_router(clinical_docs.router)
app.include_router(surgery.router)
app.include_router(followups.router)
app.include_router(accounting.router)
app.include_router(cost.router)
app.include_router(materials.router)
app.include_router(analytics.router)
app.include_router(notifications.router)
app.include_router(monitor.router)
app.include_router(credentials.router)
app.include_router(outpatient_docs.router)
app.include_router(org_groups.router)
app.include_router(fund.router)
app.include_router(staffing.router)
app.include_router(disease_programs.router)
app.include_router(rules.router)
app.include_router(workflows.router)
app.include_router(workflows.service_router)
# 块4：细目补齐（中药制剂/消毒成本/课件与实训/产前筛查/绩效整改/上门服务）
app.include_router(performance.improvement_router)  # ADR-0006：从 gapfill 搬回业务前缀
app.include_router(homevisits.router)  # ADR-0006：原 gapfill.home_router
# ADR-0006：倾倒场 service_extras 已拆解，20 个端点回到各自业务前缀；
# 满意度与智能导诊不隶属任何既有业务域，新建两个模块（见各自 docstring）。
app.include_router(surveys.router)
app.include_router(triage.router)
# 全域慢专病子系统：装卸是一个动作，由 MEDPLAT_SPD_ENABLED 控制（见 app/spd/）
register_spd(app)
app.include_router(todos.router)
app.include_router(ws.router)

ACCESS_LOGGER_NAME = "medplat.access"
_access_logger = logging.getLogger(ACCESS_LOGGER_NAME)
#: 全部 medplat.* 日志的共同祖先。handler 挂在**这一层**，不是挂在 access 上。
_root_logger = logging.getLogger("medplat")


class _MedplatFormatter(logging.Formatter):
    """一份 handler 承接全部 `medplat.*` 日志，输出统一为一行 JSON。

    访问日志的 message 本身**已经是**那一行 JSON，原样透出（逐字节不变）；
    其余日志（审计失败、任务、告警、支付、短信……）格成同构的一行，
    好让同一个采集器一把解析，而不是一个文件里两种格式。

    `exc_info` 必须带上：`_write_audit` 里那句"审计写失败，本条审计丢失"
    正是靠 traceback 才能查出是库抖动还是锁超时，而它恰恰是**丢了一条审计**
    时唯一的凭据。
    """

    def format(self, record: logging.LogRecord) -> str:
        if record.name == ACCESS_LOGGER_NAME:
            return record.getMessage()
        row = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            row["traceback"] = self.formatException(record.exc_info)
        return json.dumps(row, ensure_ascii=False)


def _configure_logging() -> None:
    """全部 `medplat.*` 日志的输出（A10）：stdout 恒开；`MEDPLAT_LOG_FILE` 非空时另附轮转文件。

    容器部署 stdout 由 docker/journald 收集即可；裸机/等保 6 个月留存场景
    设 `MEDPLAT_LOG_FILE` 落轮转文件（大小与份数由 `MEDPLAT_LOG_ROTATE_MAX_MB`
    / `MEDPLAT_LOG_ROTATE_BACKUPS` 控制），目录不存在则自动创建。
    幂等：已有 handler 时不重复附加（reload/多次 import 不会写两遍）。

    **handler 挂在 `medplat` 而不是 `medplat.access`。** 原实现只配了 access 一个
    logger，另外 17 个（audit / jobs / alerting / payments / sms / wechat / …）
    没有任何 handler，一路 propagate 到 root——root 也没配，于是落到
    `logging.lastResort`：**INFO 整个被丢掉，ERROR 只去 stderr、进不了那个文件**。
    也就是说运维配了 `MEDPLAT_LOG_FILE`、以为拿到了 6 个月留存，实际只留下了访问日志；
    偏偏"审计写失败、本条审计丢失"这种**必须留存**的记录就在被丢掉的那一半里。

    只挂一份 handler、让 access 往上 propagate，是为了**别让两个
    RotatingFileHandler 指着同一个文件**：各自独立翻滚会互相改名对方的文件，
    轮转一次就丢一段日志。
    """
    if _root_logger.handlers:
        return
    formatter = _MedplatFormatter()
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    _root_logger.addHandler(stream_handler)
    if settings.log_file:
        log_path = Path(settings.log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            log_path,
            maxBytes=settings.log_rotate_max_mb * 1024 * 1024,
            backupCount=settings.log_rotate_backups,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _root_logger.addHandler(file_handler)
    _root_logger.setLevel(logging.INFO)
    # `medplat` 的 propagate 保持默认的 True——这 17 个 logger 本来就往 root 传，
    # 改成 False 属于本次修复不需要的行为变更（CLAUDE.md 第 1 条），还会让
    # 8 个测试文件里的 caplog 收不到记录。挂上 handler 之后 `logging.lastResort`
    # 不再触发（它只在整条链上一个 handler 都没有时才兜底），双打的问题本来就没有。
    #
    # access 自己不留 handler，交给上面那一份——**这一条是本次唯一的行为变更**：
    # 它以前 propagate=False，现在要靠往上传才拿得到 handler。
    # 级别单独钉住，免得有人调高 medplat 的级别时把请求日志一起关掉。
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = True


_configure_logging()


@app.middleware("http")
async def security_headers_middleware(request, call_next):
    """安全响应头：等保整改基线（防 MIME 嗅探/点击劫持/来源泄露）。"""
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    # CSP（阶段十一）：管理端是 build-free 的内联脚本页面，故必须放行
    # 'unsafe-inline'——不放行页面直接白屏。真正要挡的是**外部来源**：
    # default-src 'self' 之后，任何第三方脚本、iframe、远程表单提交都进不来，
    # 这才是 XSS 被利用时的主要外泄通道。
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "form-action 'self'; "
        "base-uri 'self'; "
        "object-src 'none'",
    )
    return response


def _log_access(
    request, request_id: str, status_code: int, duration_ms: float, error: str = ""
) -> None:
    """写一条结构化访问日志。`error` 仅在未捕获异常时带上异常类名。

    形参叫 `status_code` 而不是 `status`：模块里 `status` 已经是 fastapi 那个常量模块
    （health 用它取 503），同名会把它遮住。命名也与同文件的 `_write_audit` 一致。
    """
    if not settings.log_json:
        return
    row = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "request_id": request_id,
        "method": request.method,
        "path": request.url.path,
        "status": status_code,
        "duration_ms": duration_ms,
    }
    if error:
        row["error"] = error
    _access_logger.info(json.dumps(row, ensure_ascii=False))


@app.middleware("http")
async def request_log_middleware(request, call_next):
    """结构化 JSON 请求日志：method/path/status/耗时/追踪ID（X-Request-ID 透传或生成）。

    **未捕获异常必须走同一条记账路径。** 原实现只在 `call_next` 正常返回后才计数、
    才写日志、才回 X-Request-ID；而未捕获异常会从 `await call_next` 一路抛到
    Starlette 最外层的 ServerErrorMiddleware，把这三件事整个跳过。后果不是"少一条
    日志"，是**监控台的错误率在全站 500 时显示 0%**——`by_status_class` 里根本没有
    这次调用，看板越红的时候越干净，恰好在最需要它的时刻骗人。
    （`audit_middleware` 早就 try/except 兜住了写操作的留痕，这里是把同一条口径
    补到监控与访问日志上；`_unhandled_exception_handler` 补的是响应头。）
    """
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    # 挂到 request.state 上，好让 ServerErrorMiddleware 那层的异常处理器也拿得到
    # ——异常一抛，本函数的局部变量就跟着栈一起没了。
    request.state.request_id = request_id
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        duration_ms = round((time.perf_counter() - start) * 1000, 2)
        monitor_metrics.record(request.method, request.url.path, 500, duration_ms)
        _log_access(request, request_id, 500, duration_ms, error=type(exc).__name__)
        raise
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    # 监控计数：进程内，随进程启停清零（见 app/monitor.py 的取舍说明）
    monitor_metrics.record(request.method, request.url.path, response.status_code, duration_ms)
    _log_access(request, request_id, response.status_code, duration_ms)
    return response


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request, exc):
    """给未捕获异常的 500 响应补上 X-Request-ID，**响应体逐字节不变**。

    没有这个头，运维拿到用户报的"页面报错了"就只有一个时间点：日志里那一刻的
    几十条记录挨个看。有了它，用户截图上的 ID 直接 grep 到那一条（含上面补的
    `error` 字段与耗时）。

    体和状态码照抄 Starlette `ServerErrorMiddleware.error_response` 的默认值
    （`PlainTextResponse("Internal Server Error", 500)`），只加头不改字节——
    500 的响应体也是对外行为，治理不得改响应字节（CLAUDE.md §11）。
    ServerErrorMiddleware 发完响应仍会重新抛出异常，所以 uvicorn 的 traceback
    与 TestClient 的 `raise_server_exceptions` 行为都不受影响。
    """
    return PlainTextResponse(
        "Internal Server Error",
        status_code=500,
        headers={"X-Request-ID": getattr(request.state, "request_id", "")},
    )


_AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# 登录请求不落审计（避免与口令尝试混淆，登录安全事件由专用日志承担）
_AUDIT_EXEMPT = {"/api/auth/login"}

_audit_logger = logging.getLogger("medplat.audit")

# ---- 审计链尾的串行化（工程包 P2） ----
# "读链尾 → 算哈希 → 插入"三步不是原子的：并发下两条记录读到同一个链尾，
# prev_hash 相同、哈希链分叉，`GET /api/audit/verify` 报断链。
# - PostgreSQL（多 worker/多实例的生产形态）：用**事务级咨询锁**串行化——
#   `pg_advisory_xact_lock` 在同一事务内持有、commit/rollback 自动释放，
#   跨进程有效且不会泄漏锁；
# - SQLite（开发/测试，单文件单写者）：进程内全局 threading.Lock 即可——
#   SQLite 生产已被 config 拒启，不存在跨进程并发写的合法形态。
_AUDIT_SQLITE_LOCK = threading.Lock()
#: 咨询锁键：任意约定常量，只须全平台唯一（0x41554449 = ASCII "AUDI"）
_AUDIT_PG_LOCK_KEY = 0x41554449
_AUDIT_PG_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:key)")


def _write_audit(request, status_code: int) -> None:
    """审计落库：记录的是「写操作请求尝试」（含失败/异常），与业务事务解耦。

    故障隔离（工程包 P2）：审计写失败（库抖动/锁超时/磁盘满）时 rollback +
    logger.error 后**吞掉异常，不拖垮业务响应**——业务写已经成功，为一条
    旁路留痕把响应打成 500 只会让现场更糟。取舍与读留痕（AccessLog 降级）
    一致：丢失的审计记入错误日志，由日志告警兜底其完整性。

    **操作主体的取令牌口径复用 deps.token_for_audit**（header 优先、缺失时回落
    业务端/居民端会话 Cookie）：G3 之后浏览器走 Cookie 会话、不再发
    Authorization 头，只认该头会把三套前端的写操作全部记成 anonymous。
    居民端令牌记成 `resident:{account_id}`（令牌 sub 本身就是这个值，见
    portal._issue_token），既可辨识又不把手机号一类 PII 抄进审计表。
    """
    username, user_id = "", None
    try:
        token = token_for_audit(request)
        claims = decode_token(token) if token else None
    except Exception:  # noqa: BLE001 - 见 docstring：取令牌失败不拖垮业务，按 anonymous 留痕
        claims = None
        _audit_logger.error("审计取令牌失败，本条按 anonymous 留痕", exc_info=True)
    is_resident = False
    if claims:
        username = claims.get("sub", "")
        is_resident = claims.get("scope") == "portal"
    try:
        db = SessionLocal()
    except Exception:  # noqa: BLE001 - 见 docstring：审计失败不拖垮业务
        _audit_logger.error("审计会话创建失败，本条审计丢失", exc_info=True)
        return
    try:
        # 居民账户不在 users 表内，其 sub（resident:{id}）不参与 users 查库——
        # 与 deps.get_current_user 拒收 scope=portal 同一顾虑：别让一个叫
        # "resident:1" 的业务账号撞上居民主体。
        if username and not is_resident:
            user = db.query(User).filter(User.username == username).first()
            user_id = user.id if user else None

        def _append_to_chain() -> None:
            # 防篡改哈希链：本条 hash = MAC(密钥, 上一条 hash + 本条内容)。
            # 串行取上一条：审计写入是低频旁路，为它上并发优化不值当，
            # 而链的正确性依赖顺序（串行化机制见上方注释）。
            if db.get_bind().dialect.name == "postgresql":
                db.execute(_AUDIT_PG_LOCK_SQL, {"key": _AUDIT_PG_LOCK_KEY})
            prev = (
                db.query(AuditLog.entry_hash)
                .order_by(AuditLog.id.desc())
                .limit(1)
                .scalar()
            ) or ""
            entry = AuditLog(
                user_id=user_id,
                username=username or "anonymous",
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                prev_hash=prev,
            )
            entry.entry_hash = audit_entry_hash(
                prev, entry.username, entry.method, entry.path, entry.status_code
            )
            db.add(entry)
            db.commit()

        if db.get_bind().dialect.name == "postgresql":
            _append_to_chain()
        else:
            with _AUDIT_SQLITE_LOCK:
                _append_to_chain()
    except Exception:  # noqa: BLE001 - 见 docstring：审计失败不拖垮业务
        with contextlib.suppress(Exception):
            db.rollback()
        _audit_logger.error(
            "审计写入失败（业务响应不受影响，本条审计丢失）：%s %s -> %s",
            request.method, request.url.path, status_code, exc_info=True,
        )
    finally:
        with contextlib.suppress(Exception):  # 连 close 都抛时也不迁怒业务响应
            db.close()


@app.middleware("http")
async def audit_middleware(request, call_next):
    """M2 整改：try/except 包住 call_next——业务路由抛未捕获异常（500）时
    同样落审计（status_code=500）后重新抛出，保证写操作全量留痕。"""
    path = request.url.path
    audited = (
        request.method in _AUDITED_METHODS
        and path.startswith("/api/")
        and path not in _AUDIT_EXEMPT
    )
    try:
        response = await call_next(request)
    except Exception:
        if audited:
            _write_audit(request, 500)
        raise
    if audited:
        _write_audit(request, response.status_code)
    return response


@app.get("/api/health", tags=["平台"])
def health(response: Response):
    """健康检查：附带数据库连通性探测。

    **库不通时回 503，而不是 200 带一句 degraded。** 探针看的是状态码，不是响应体：
    `Dockerfile` 的 HEALTHCHECK 写的就是 `.raise_for_status()`，负载均衡与
    k8s 探针同理。原实现永远回 200，等于那条 HEALTHCHECK **从设计之初就没生效过**
    ——一个连不上库的实例会一直留在轮询里，把流量吸进去再全部报错，
    而这正是探针存在的唯一理由。

    响应体逐字节不变（`status` 仍是 "degraded"，字段不增不减），改的只有状态码；
    `start.sh` 的启动等待循环不用 raise_for_status，只判"HTTP 通不通"，不受影响。
    """
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 任何数据库异常均判定为不可用
        db_status = "error"
    if db_status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    payload = {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "medplat",
        "version": app.version,
        "database": db_status,
    }
    return payload


_STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(_STATIC_DIR / "index.html")


@app.get("/m", include_in_schema=False)
def mobile_index():
    """居民端移动版 H5 入口。"""
    return FileResponse(_STATIC_DIR / "m" / "index.html")


@app.get("/m/doctor", include_in_schema=False)
def mobile_doctor():
    """块4：医生移动工作台入口（页内登录后可用，接口鉴权仍由 JWT 承担）。"""
    return FileResponse(_STATIC_DIR / "m" / "doctor.html")
