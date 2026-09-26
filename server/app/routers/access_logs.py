"""敏感读留痕的查询接口（第十轮 P0）。

第九轮建了 `access_logs` 表，每次调阅患者档案都记下"谁、什么时候、凭什么、
看了谁"——但**只写没有读**。一张答不出问题的合规日志等于没建：真出了
调阅纠纷，数据在库里却调不出来。对比写审计 `audit_logs` 有 list/export/
verify/stats 四个读接口，读留痕一个都没有。

这个模块补上两个视角：

- **监管视角**（director/admin）：按患者/调阅人/机构/依据/时间段筛，用于合规
  排查——"上个月谁查过这个人的档案""这个账号都查过谁"；
- **患者视角**（居民端）：本人查"哪些机构、谁，在什么时候看过我的档案"。
  这是《个人信息保护法》给患者的知情权，不只是给监管看的。

**查这张表的动作本身也留痕**：调阅"谁看过某患者档案"同样是在看这个患者的
隐私。判准是**是否指向可识别的个人**：清单与统计只要按患者聚焦就记，
不带患者定位的全表浏览与全局聚合不记（也没有 patient_id 可记）。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_date, require_roles
from ..models import AccessLog, Organization, Patient, User
from .portal import current_resident_patient

router = APIRouter(prefix="/api/access-logs", tags=["敏感读留痕"])

# basis 的可读名——事后翻日志的人未必记得每个英文词的含义
BASIS_NAMES = {
    "global": "全域角色",
    "self": "本人",
    "encounter": "本机构就诊",
    "service": "本机构服务记录",
    "contract": "家庭医生签约",
    "referral": "转诊",
    "authorization": "患者授权",
    "recognition": "结果互认查询",
    "consent_admin": "授权办理",
    "export": "出站导出",
    "delegate": "家庭代管",
}

# resource 的可读名。居民在"谁看过我的档案"里读到的就是这一列，词表缺一个，
# 居民看到的就是 `inpatient_order` 这样的英文码（P2-42：曾只有 14 个词，源码里
# 实际写入的有 80 多个）。**每个写进 AccessLog 的词都得在这里有名字**——由
# `tests/test_access_log_resource_names.py` 从源码推导全部写入点来盯，新增留痕点
# 漏配名字即红。词表只增不删：库里的存量行还带着旧词（如 `exam`）。
RESOURCE_NAMES = {
    "archive": "健康档案",
    "archive_360": "患者360全景",
    "encounter": "就诊记录",
    "exam": "检查检验",
    "exam_recognition": "互认查询",
    "medication": "用药画像",
    "vaccination": "接种史",
    "publichealth": "公卫提醒",
    "treatment": "处置记录",
    "authorization": "调阅授权",
    "fhir_export": "FHIR导出",
    "consumable": "耗材使用",
    "unified_requests": "统一待办",
    "access_log_view": "调阅记录查询",
    # —— 以下为 P2-42 补齐 ——
    # 门急诊与住院
    "appointment": "预约记录",
    "credential": "就诊凭据",
    "consultation": "远程会诊",
    "medical_record": "就诊病历",
    "consent": "知情同意",
    "correction": "档案更正 / 注销申请",
    "doc_completeness": "门急诊文书完整性",
    "outpatient_nursing": "门急诊护理记录",
    "admission": "住院记录",
    "inpatient_order": "住院医嘱",
    "progress_note": "病程记录",
    "nursing_record": "住院护理记录",
    "vital_sign": "体征记录",
    "document_completeness": "住院文书完整性",
    "case_summary": "病案首页",
    "surgery": "手术安排",
    "surgery_record": "术中记录",
    "emergency": "急救记录",
    "exam_report_revision": "报告修订记录",
    "exam_critical_action": "危急值处置记录",
    "checkup": "健康体检",
    "checkup:items": "体检分项结果",
    "cert": "医学证明",
    "death_report_card": "死因报告卡",
    "referral": "转诊记录",
    # 费用
    "bill": "费用账单",
    "billing": "费用明细与结算",
    "admission_bill": "住院费用清单",
    "deposit": "住院押金",
    "insurance": "医保结算",
    "referral_cert": "转诊证明",
    "special_disease": "特殊病种申报",
    "dual_channel": "双通道用药申报",
    # 公卫与慢病
    "contract": "家医签约",
    "followup": "随访记录",
    "chronic": "慢病管理",
    "disease_program": "专病管理",
    "enrollment": "疾病管理档案",
    "home_visit": "上门服务",
    "eldercare": "老年人能力评估",
    "maternal": "妇女保健",
    "aefi": "疑似预防接种异常反应",
    # 打印（打印件出了系统就收不回来，单列一类）
    "print:case_summary": "打印病案首页",
    "print:cert": "打印医学证明",
    "print:checkup": "打印体检报告",
    "print:consent": "打印知情同意书",
    "print:discharge": "打印出院小结",
    "print:exam_report": "打印检查检验报告",
    "print:exam_request": "打印检查检验申请单",
    "print:inp_bill": "打印住院费用清单",
    "print:prescription": "打印处方",
    "print:referral": "打印转诊单",
    "print:settlement": "打印结算单",
    "print:vaccine_cert": "打印接种证明",
    # 慢专病子系统（spd_ 前缀沿用 spd 路由的既有词表）
    "spd_home": "慢专病首页",
    "spd_archive": "慢专病全周期档案",
    "spd_profile": "慢专病360档案",
    "spd_journey": "慢专病全流程视图",
    "spd_enrollment": "慢专病纳管档案",
    "spd_path": "慢专病临床路径",
    "spd_screening": "慢专病筛查",
    "spd_assessment": "慢专病评估",
    "spd_measurement": "慢专病监测指标",
    "spd_followup": "慢专病随访",
    "spd_calendar": "慢专病健康日历",
    "spd_call": "慢专病随访呼叫",
    "spd_revisit": "慢专病复诊计划",
    "spd_intervention": "慢专病干预方案",
    "spd_health_rx": "慢专病健康处方",
    "spd_edu": "慢专病健康宣教",
    "spd_task": "慢专病健康任务",
    "spd_case_report": "慢专病异常上报",
    "spd_consult": "慢专病在线咨询",
    "spd_referral": "慢专病转诊",
    "spd_apply": "慢专病服务申请",
}

# 附件留痕的 resource 由 `attachments._resource()` 拼成 `att:{owner_type}:{action}`，
# 不是定值，按两段分别取名。只有 scope="patient" 的业务域会写 AccessLog，
# 守卫逐个核对它们都在这里有名字（含子系统装载时注册进来的 `spd_task`）。
# 子系统的词也放在平台这里、而不是随注册带进来：留痕行比子系统的装卸活得久——
# spd 关掉以后，居民的历史调阅记录照样要显示人话。
ATTACHMENT_OWNER_NAMES = {
    "exam_report": "检查报告",
    "consultation": "会诊",
    "referral": "转诊",
    "spd_task": "慢专病任务",
}
ATTACHMENT_ACTION_NAMES = {"download": "下载", "upload": "上传", "list": "清单"}


def resource_name(resource: str) -> str:
    """resource 的可读名；认不出的原样返回（旧行、以后新增的词都不会因此报错）。"""
    if resource in RESOURCE_NAMES:
        return RESOURCE_NAMES[resource]
    prefix, _, rest = resource.partition(":")
    owner, _, action = rest.partition(":")
    if prefix == "att" and owner in ATTACHMENT_OWNER_NAMES and action in ATTACHMENT_ACTION_NAMES:
        return f"{ATTACHMENT_OWNER_NAMES[owner]}附件{ATTACHMENT_ACTION_NAMES[action]}"
    return resource


class AccessLogOut(BaseModel):
    """监管清单与患者视角 `/mine` 共用的 `_row_out()` 形状（11 键）。

    `viewer_org_id` 是键恒在值可空（居民端/平台账号不挂机构记 null）；
    `at` 是 isoformat **或空串**（created_at 缺省兜底），不是 null。
    """

    id: int
    viewer: str
    viewer_org_id: int | None
    viewer_org_name: str
    patient_id: int
    patient_name: str
    resource: str
    resource_name: str
    basis: str
    basis_name: str
    at: str


class BasisCountOut(BaseModel):
    basis: str
    basis_name: str
    count: int


class AccessLogStatsOut(BaseModel):
    total: int
    by_basis: list[BasisCountOut]


def _row_out(log: AccessLog, org_name: str = "", patient_name: str = "") -> dict:
    return {
        "id": log.id,
        "viewer": log.username,
        "viewer_org_id": log.org_id,
        "viewer_org_name": org_name,
        "patient_id": log.patient_id,
        "patient_name": patient_name,
        "resource": log.resource,
        "resource_name": resource_name(log.resource),
        "basis": log.basis,
        "basis_name": BASIS_NAMES.get(log.basis, log.basis),
        "at": log.created_at.isoformat() if log.created_at else "",
    }


def _decorate(db: Session, rows: list[AccessLog]) -> list[dict]:
    """把机构名、患者名一次查出来贴上，避免每行一次查询（N+1）。"""
    org_ids = {r.org_id for r in rows if r.org_id}
    patient_ids = {r.patient_id for r in rows}
    orgs: dict[int | None, str] = {
        o.id: o.name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}
    # 键声明成 `int | None`：`r.org_id` / `r.patient_id` 是可空外键，
    # 拿 None 去 .get() 运行期本来就是"取不到、走默认值"，类型上也该说得通。
    patients: dict[int | None, str] = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
    } if patient_ids else {}
    return [_row_out(r, orgs.get(r.org_id, ""), patients.get(r.patient_id, "")) for r in rows]


@router.get("", response_model=list[AccessLogOut],
            dependencies=[Depends(require_roles("director"))])
def list_access_logs(
    response: Response,
    patient_id: int | None = None,
    username: str | None = None,
    org_id: int | None = None,
    basis: str | None = None,
    start: str | None = None,
    end: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """合规排查（director/admin）。

    只读，不改留痕语义。**针对单个患者的查询也留痕**——查"谁看过这个人"
    本身在看这个人的隐私。全表/按调阅人浏览不记（无单一患者可记，
    且那更接近监管巡检而非针对性调阅）。
    """
    query = db.query(AccessLog)
    if patient_id is not None:
        query = query.filter(AccessLog.patient_id == patient_id)
    if username:
        query = query.filter(AccessLog.username == username)
    if org_id is not None:
        query = query.filter(AccessLog.org_id == org_id)
    if basis:
        query = query.filter(AccessLog.basis == basis)
    # 日期段按 created_at 的日界过滤；留痕存的是 naive datetime。
    # 拼时间戳之前先校验（P1-58）：`2026-02-30` 拼成的串在真 PG 上转 timestamp 失败，
    # 此前是 500——稽核员在自由文本框里敲错一位，看到的是"服务器内部错误"。
    if start:
        start = require_date(start, field="start")
        query = query.filter(AccessLog.created_at >= f"{start} 00:00:00")
    if end:
        end = require_date(end, field="end")
        query = query.filter(AccessLog.created_at <= f"{end} 23:59:59")

    rows = paginate(query.order_by(AccessLog.id.desc()), response, offset, limit)
    result = _decorate(db, rows)

    if patient_id is not None:
        # 针对某个患者的调阅记录查询——记一笔，basis 用查询者的角色依据
        _log_view(db, user, patient_id)
    return result


def _log_view(db: Session, user: User, patient_id: int) -> None:
    """给"查询某患者的调阅记录"这个动作留痕。走独立会话，与读事务解耦
    （同 visibility._write_access_log 的理由：读接口不该顺带开写事务）。"""
    from ..clock import now_naive
    from ..database import SessionLocal

    s = SessionLocal()
    try:
        s.add(
            AccessLog(
                user_id=user.id,
                username=user.username,
                org_id=user.org_id,
                patient_id=patient_id,
                resource="access_log_view",
                basis="global",  # 本接口限 director/admin，均为全域角色
                created_at=now_naive(),
            )
        )
        s.commit()
    except Exception:  # pragma: no cover - 留痕失败不阻断查询
        s.rollback()
    finally:
        s.close()


@router.get("/mine", response_model=list[AccessLogOut])
def my_access_logs(
    response: Response,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    patient: Patient = Depends(current_resident_patient),
):
    """患者视角（居民端）：谁、哪家机构、什么时候看过我的档案。

    《个保法》第 44 条：个人有权知悉其个人信息的处理情况。这条接口把它落到
    实处——居民自己就能看到自己的档案被谁调阅过、凭什么。

    只返回**本人**的记录（按绑定的 patient_id 过滤，绕不开）；不显示调阅人
    的机构内部账号名细节之外的东西，够回答"谁看过我"即可。
    """
    query = (
        db.query(AccessLog)
        .filter(AccessLog.patient_id == patient.id)
        .order_by(AccessLog.id.desc())
    )
    rows = paginate(query, response, offset, limit)
    return _decorate(db, rows)


@router.get("/stats", response_model=AccessLogStatsOut,
            dependencies=[Depends(require_roles("director"))])
def access_log_stats(
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按依据汇总：一段时间内各类调阅的构成，看跨机构调阅（转诊/授权）占比
    是否异常。可选按患者聚焦某一个人——聚焦即指向可识别的个人，同样自我留痕。"""
    from sqlalchemy import func

    query = db.query(AccessLog.basis, func.count(AccessLog.id))
    if patient_id is not None:
        if db.get(Patient, patient_id) is None:
            raise HTTPException(status_code=404, detail="患者不存在")
        _log_view(db, user, patient_id)
        query = query.filter(AccessLog.patient_id == patient_id)
    rows = query.group_by(AccessLog.basis).order_by(AccessLog.basis).all()
    by_basis = [
        {"basis": b, "basis_name": BASIS_NAMES.get(b, b), "count": n} for b, n in rows
    ]
    return {
        "total": sum(n for _, n in rows),
        "by_basis": sorted(by_basis, key=lambda x: -x["count"]),
    }
