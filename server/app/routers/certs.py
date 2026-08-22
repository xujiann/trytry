"""法定医学证明（浙#7、㉔出生医学证明签发）：出生/死亡医学证明签发与出生缺陷儿登记。"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..concurrency import insert_with_retry
from ..visibility import assert_org_writable, log_patient_access, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ChildRecord, MedicalCert, Organization, Patient, User
from ..datetypes import DateStr
from ..privacy import mask_id_card, mask_phone
from .reports import _csv_response

router = APIRouter(prefix="/api/certs", tags=["法定医学证明"], dependencies=[Depends(get_current_user)])

_PREFIX = {"birth": "B", "death": "D", "defect": "Q"}
_TYPE_NAMES = {"birth": "出生医学证明", "death": "死亡医学证明", "defect": "出生缺陷儿登记"}


class CertCreate(BaseModel):
    cert_type: str = Field(pattern="^(birth|death|defect)$")
    name: str = Field(min_length=1)
    gender: str = "未知"
    event_date: DateStr
    detail: str = ""
    org_id: int
    patient_id: int | None = None
    child_id: int | None = None


class CertIssueOut(BaseModel):
    """签发结果的响应契约。字段与原手拼 dict 一一对应，保持响应向后兼容。"""
    id: int
    cert_type: str
    cert_type_name: str
    cert_no: str
    name: str
    event_date: str


class CertOut(BaseModel):
    """证明列表行的响应契约。"""
    id: int
    cert_type: str
    cert_no: str
    name: str
    gender: str
    event_date: str
    detail: str
    org_id: int


@router.post(
    "",
    status_code=201,
    response_model=CertIssueOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 证明签发/缺陷登记
)
def issue_cert(
    body: CertCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="签发机构不存在")
    if body.cert_type == "death":
        if body.patient_id is None:
            raise HTTPException(status_code=422, detail="死亡医学证明须关联患者档案")
        if db.get(Patient, body.patient_id) is None:
            raise HTTPException(status_code=404, detail="患者不存在")
        if not body.detail:
            raise HTTPException(status_code=422, detail="死亡医学证明须填写死因诊断")
    if body.cert_type == "defect" and not body.detail:
        raise HTTPException(status_code=422, detail="出生缺陷儿登记须填写缺陷诊断")
    if body.child_id is not None and db.get(ChildRecord, body.child_id) is None:
        raise HTTPException(status_code=404, detail="儿童档案不存在")
    # 证明编号：类型前缀 + 年份 + 6位顺序号
    # 与医废追溯码同型：编号是服务端 COUNT+1 算出来的，并发下会算出同一个。
    # 重试取号是服务端的事，不该让签发证明的人重来一遍。
    def _build() -> MedicalCert:
        seq = (
            db.query(func.count(MedicalCert.id))
            .filter(MedicalCert.cert_type == body.cert_type)
            .scalar()
            or 0
        ) + 1
        cert_no = f"{_PREFIX[body.cert_type]}{date.today().year}{seq:06d}"
        return MedicalCert(cert_no=cert_no, created_by=user.id, **body.model_dump())

    cert = insert_with_retry(db, _build)
    return {
        "id": cert.id,
        "cert_type": cert.cert_type,
        "cert_type_name": _TYPE_NAMES[cert.cert_type],
        "cert_no": cert.cert_no,
        "name": cert.name,
        "event_date": cert.event_date,
    }


@router.get("", response_model=list[CertOut])
def list_certs(
    cert_type: str | None = None, patient_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(MedicalCert)
    if cert_type:
        query = query.filter(MedicalCert.cert_type == cert_type)
    query = scope_patient_list(db, user, query, MedicalCert, patient_id, "cert")
    return [
        {
            "id": c.id,
            "cert_type": c.cert_type,
            "cert_no": c.cert_no,
            "name": c.name,
            "gender": c.gender,
            "event_date": c.event_date,
            "detail": c.detail,
            "org_id": c.org_id,
        }
        for c in query.order_by(MedicalCert.id.desc()).limit(200).all()
    ]


# ---------- 工程包 I1：法定上报导出（死因报告卡） ----------


class DeathReportCardOut(BaseModel):
    """死因报告卡导出契约（按人口死亡登记系统字段集，平台已存字段）。

    身份证号/电话按调用者角色脱敏（H1 口径：非 admin 掩码）。
    """

    cert_id: int
    cert_no: str
    name: str
    gender: str
    id_card: str
    phone: str
    birth_date: str
    death_date: str
    cause_of_death: str
    org_id: int
    org_name: str
    issued_by: str
    issued_at: str


def _death_card(
    cert: MedicalCert, patient: Patient | None, org_name: str, issued_by: str, user: User
) -> dict:
    admin = user.role == "admin"
    id_card = patient.id_card if patient else ""
    phone = patient.phone if patient else ""
    return {
        "cert_id": cert.id,
        "cert_no": cert.cert_no,
        "name": cert.name,
        "gender": cert.gender,
        "id_card": id_card if admin else mask_id_card(id_card),
        "phone": phone if admin else mask_phone(phone),
        "birth_date": patient.birth_date if patient else "",
        "death_date": cert.event_date,
        "cause_of_death": cert.detail,
        "org_id": cert.org_id,
        "org_name": org_name,
        "issued_by": issued_by,
        "issued_at": cert.created_at.isoformat(),
    }


@router.get(
    "/death-report-cards/export.csv",
    response_model=str,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def export_death_report_cards_csv(
    date_from: str | None = None,
    date_to: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """死因报告卡批量导出（CSV，按死亡日期 event_date 过滤）。

    **报送方式说明**：平台与县疾控/人口死亡登记系统无直连专线，本导出供
    **手工网报**（录入人口死亡信息登记管理系统）或交换前置机对接使用。
    身份证号/电话按调用者角色脱敏（非 admin 掩码）；每张卡的患者档案
    调阅均落 AccessLog（可问责口径）。
    """
    query = db.query(MedicalCert).filter(MedicalCert.cert_type == "death")
    if date_from:
        query = query.filter(MedicalCert.event_date >= date_from)
    if date_to:
        query = query.filter(MedicalCert.event_date <= date_to)
    certs = query.order_by(MedicalCert.id).limit(2000).all()
    org_names = {o.id: o.name for o in db.query(Organization).all()}
    usernames = {u.id: u.username for u in db.query(User).all()}
    patients = {
        p.id: p
        for p in db.query(Patient)
        .filter(Patient.id.in_([c.patient_id for c in certs if c.patient_id]))
        .all()
    }
    rows = []
    for cert in certs:
        patient = patients.get(cert.patient_id) if cert.patient_id else None
        if cert.patient_id:
            log_patient_access(db, user, cert.patient_id, "death_report_card", "export")
        card = _death_card(
            cert, patient, org_names.get(cert.org_id, ""), usernames.get(cert.created_by, ""), user
        )
        rows.append(
            [
                card["cert_no"], card["name"], card["gender"], card["id_card"],
                card["birth_date"], card["death_date"], card["cause_of_death"],
                card["org_name"], card["issued_by"], card["issued_at"],
            ]
        )
    return _csv_response(
        "death_report_cards.csv",
        ["证明编号", "姓名", "性别", "身份证号", "出生日期", "死亡日期", "死因诊断",
         "签发机构", "签发人", "签发时间"],
        rows,
    )


@router.get(
    "/{cert_id}/death-report-card",
    response_model=DeathReportCardOut,
    dependencies=[Depends(require_roles("director"))],  # 法定上报导出=管理层
)
def death_report_card(
    cert_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """单张死因报告卡（JSON，按人口死亡登记系统字段集导出平台已存字段）。

    **报送方式说明**：导出供手工网报（人口死亡信息登记管理系统）或县疾控
    前置机对接，平台不直连该系统。死亡证明必联患者档案（签发时强制），
    本接口按患者维度留痕（AccessLog）；身份证号/电话按角色脱敏。
    """
    cert = db.get(MedicalCert, cert_id)
    if cert is None:
        raise HTTPException(status_code=404, detail="证明不存在")
    if cert.cert_type != "death":
        raise HTTPException(status_code=422, detail="该证明不是死亡医学证明，无死因报告卡")
    patient = db.get(Patient, cert.patient_id) if cert.patient_id else None
    if cert.patient_id:
        log_patient_access(db, user, cert.patient_id, "death_report_card", "export")
    org = db.get(Organization, cert.org_id)
    issuer = db.get(User, cert.created_by)
    return _death_card(
        cert, patient, org.name if org else "", issuer.username if issuer else "", user
    )


@router.get("/stats", response_model=dict[str, int])
def cert_stats(db: Session = Depends(get_db)):
    """签发统计：按证明类型计数（上报省平台为对接项）。"""
    rows = db.query(MedicalCert.cert_type, func.count(MedicalCert.id)).group_by(MedicalCert.cert_type).all()
    return {t: n for t, n in rows}
