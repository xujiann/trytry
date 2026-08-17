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
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
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
}

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
}


def _row_out(log: AccessLog, org_name: str = "", patient_name: str = "") -> dict:
    return {
        "id": log.id,
        "viewer": log.username,
        "viewer_org_id": log.org_id,
        "viewer_org_name": org_name,
        "patient_id": log.patient_id,
        "patient_name": patient_name,
        "resource": log.resource,
        "resource_name": RESOURCE_NAMES.get(log.resource, log.resource),
        "basis": log.basis,
        "basis_name": BASIS_NAMES.get(log.basis, log.basis),
        "at": log.created_at.isoformat() if log.created_at else "",
    }


def _decorate(db: Session, rows: list[AccessLog]) -> list[dict]:
    """把机构名、患者名一次查出来贴上，避免每行一次查询（N+1）。"""
    org_ids = {r.org_id for r in rows if r.org_id}
    patient_ids = {r.patient_id for r in rows}
    orgs = {
        o.id: o.name
        for o in db.query(Organization).filter(Organization.id.in_(org_ids)).all()
    } if org_ids else {}
    patients = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
    } if patient_ids else {}
    return [_row_out(r, orgs.get(r.org_id, ""), patients.get(r.patient_id, "")) for r in rows]


@router.get("", dependencies=[Depends(require_roles("director"))])
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
    # 日期段按 created_at 的日界过滤；留痕存的是 naive datetime
    if start:
        query = query.filter(AccessLog.created_at >= f"{start} 00:00:00")
    if end:
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


@router.get("/mine")
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


@router.get("/stats", dependencies=[Depends(require_roles("director"))])
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
    rows = query.group_by(AccessLog.basis).all()
    by_basis = [
        {"basis": b, "basis_name": BASIS_NAMES.get(b, b), "count": n} for b, n in rows
    ]
    return {
        "total": sum(n for _, n in rows),
        "by_basis": sorted(by_basis, key=lambda x: -x["count"]),
    }
