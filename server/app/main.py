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
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
    consultations,
    contracts,
    dataquality,
    cssd,
    dictionaries,
    drgs,
    education,
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
    gapfill,
    infectious,
    inpatient,
    insurance,
    integration,
    knowledge,
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
    service_extras,
    tcm,
    tcm_heritage,
    telemedicine,
    todos,
    users,
    vaccination,
    vaccine_supply,
)
from . import ws
from .spd import register_spd, seed_spd
from .audit_chain import audit_entry_hash
from .models import AuditLog
from .security import decode_token, hash_password


@asynccontextmanager
async def lifespan(_: FastAPI):
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
        for seed in SEED_QC_RULES:
            if seed["code"] not in existing_qc:
                db.add(QcRule(**seed))
        db.commit()
        # 块2：病历环节质控规则种子化（12 条，幂等；已存在编码不覆盖本地调整）
        from .data.record_qc_rules_seed import SEED_RECORD_QC_RULES
        from .models import RecordQcRule

        existing_mrqc = {code for (code,) in db.query(RecordQcRule.code).all()}
        for seed in SEED_RECORD_QC_RULES:
            if seed["code"] not in existing_mrqc:
                db.add(RecordQcRule(**seed))
        db.commit()
        # T3.1：会计科目种子（医院会计制度常用一级科目，幂等；不覆盖本地调整）
        from .data.account_subjects_seed import SEED_ACCOUNT_SUBJECTS
        from .models import AccountSubject

        existing_subjects = {code for (code,) in db.query(AccountSubject.code).all()}
        for seed in SEED_ACCOUNT_SUBJECTS:
            if seed["code"] not in existing_subjects:
                db.add(AccountSubject(**seed))
        db.commit()
        # 全域慢专病子系统：病种规则、管理目标、筛查量表、考核指标、积分规则、
        # 随访方案与问卷、报告模板（幂等，按编码不覆盖现场调过的参数）；
        # 子系统未启用时这一步什么都不做
        seed_spd(db)
        # T1.1：把代码中注册的定时任务同步进库（幂等，不覆盖运维调过的参数）
        from . import jobs as _jobs  # noqa: F401 - 导入即完成任务注册
        from .scheduler import scheduler_loop, sync_registry

        sync_registry(db)
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
)

app.include_router(auth.router)
app.include_router(organizations.router)
app.include_router(patients.router)
app.include_router(dictionaries.router)
app.include_router(referrals.router)
app.include_router(encounters.router)
app.include_router(exams.router)
app.include_router(prescriptions.router)
app.include_router(pharmacy.router)
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
app.include_router(gapfill.tcm_router)
app.include_router(gapfill.cssd_router)
app.include_router(gapfill.edu_router)
app.include_router(gapfill.maternal_router)
app.include_router(gapfill.perf_router)
app.include_router(gapfill.home_router)
app.include_router(service_extras.router)
# 全域慢专病子系统：装卸是一个动作，由 MEDPLAT_SPD_ENABLED 控制（见 app/spd/）
register_spd(app)
app.include_router(todos.router)
app.include_router(ws.router)

_access_logger = logging.getLogger("medplat.access")
if not _access_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _access_logger.addHandler(_handler)
    _access_logger.setLevel(logging.INFO)
    _access_logger.propagate = False


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


@app.middleware("http")
async def request_log_middleware(request, call_next):
    """结构化 JSON 请求日志：method/path/status/耗时/追踪ID（X-Request-ID 透传或生成）。"""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    response = await call_next(request)
    duration_ms = round((time.perf_counter() - start) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    # 监控计数：进程内，随进程启停清零（见 app/monitor.py 的取舍说明）
    monitor_metrics.record(request.method, request.url.path, response.status_code, duration_ms)
    if settings.log_json:
        _access_logger.info(
            json.dumps(
                {
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                },
                ensure_ascii=False,
            )
        )
    return response


_AUDITED_METHODS = {"POST", "PATCH", "PUT", "DELETE"}
# 登录请求不落审计（避免与口令尝试混淆，登录安全事件由专用日志承担）
_AUDIT_EXEMPT = {"/api/auth/login"}


# 审计链临界区锁：取上一条 hash → 插入 → 计算本条 hash 必须串行，
# 否则两个并发写请求读到同一个 prev，生成两条 prev_hash 相同的记录，
# verify_chain 会误报断链。审计是低频旁路，串行代价可忽略。
# - 进程内：threading.Lock 保证同进程内单线程写链（SQLite 单文件足够）；
# - 跨进程（多 worker）：PostgreSQL 上再叠一层事务级 advisory lock，让所有 worker
#   的审计写彼此串行；SQLite 无此机制但写事务本就串行化，进程内锁已足够。
_audit_lock = threading.Lock()
# 固定的 advisory lock 键（任意常量，全局唯一即可）
_AUDIT_ADVISORY_KEY = 848_3821


def _write_audit(request, status_code: int) -> None:
    """审计落库：记录的是「写操作请求尝试」（含失败/异常），与业务事务解耦。"""
    username, user_id = "", None
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        claims = decode_token(auth[7:])
        if claims:
            username = claims.get("sub", "")
    db = SessionLocal()
    try:
        if username:
            user = db.query(User).filter(User.username == username).first()
            user_id = user.id if user else None
        with _audit_lock:
            # 多 worker 跨进程串行：PG 上取事务级 advisory lock（commit 时自动释放），
            # 保证任一时刻只有一个审计写在读 prev→插入，杜绝跨进程 prev_hash 相同。
            if db.bind.dialect.name == "postgresql":
                db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _AUDIT_ADVISORY_KEY})
            # 防篡改哈希链：本条 hash = MAC(密钥, 上一条 hash + id + 时间 + 本条内容)。
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
            db.add(entry)
            db.flush()  # 取得自增 id 与 created_at，纳入哈希
            entry.entry_hash = audit_entry_hash(
                prev,
                entry.id,
                entry.created_at.isoformat() if entry.created_at else "",
                entry.username,
                entry.method,
                entry.path,
                entry.status_code,
            )
            db.commit()
    finally:
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
def health():
    """健康检查：附带数据库连通性探测。"""
    db_status = "ok"
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - 任何数据库异常均判定为不可用
        db_status = "error"
    payload = {
        "status": "ok" if db_status == "ok" else "degraded",
        "service": "medplat",
        "version": app.version,
        "database": db_status,
    }
    return payload


@app.get("/metrics", include_in_schema=False)
def metrics_export():
    """Prometheus 文本格式指标导出（内网抓取）。

    默认关闭：仅当 MEDPLAT_METRICS_EXPORT=true 时返回，否则 404——它会暴露内部
    模块名与流量，须部署在内网/受网络策略保护时再开。无鉴权（Prometheus 抓取端
    通常不带 JWT），安全性由网络边界与开关共同保证。
    """
    if not settings.metrics_export:
        raise HTTPException(status_code=404, detail="Not Found")
    from .monitor import prometheus_text

    return PlainTextResponse(prometheus_text(), media_type="text/plain; version=0.0.4")


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
