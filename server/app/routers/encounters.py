"""就诊记录与患者360视图（健康档案汇聚）。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import events
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, row_dict
from ..models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    Organization,
    Patient,
    PhysicalExam,
    Prescription,
    Settlement,
    User,
)
from ..visibility import assert_org_writable, assert_patient_visible, visible_org_ids
from .checkups import _abnormal_item_names, abnormal_text as checkup_abnormal_text  # 体检异常项与体检清单同一口径（P2-1197）
from .dispense import prescription_not_reversed   # 已退药与用药画像同一判据（P2-1198）
from .patients import find_by_ehc_no
from .prescriptions import PRESCRIPTION_STATUS_NAMES
from ..schemas import EncounterCreate, EncounterOut

#: `encounters.encounter_type` → 中文（§13「状态文案取自后端」）：驾驶舱下钻明细原先把 outpatient 原样印出来（P2-646）
ENCOUNTER_TYPE_NAMES = {"outpatient": "门诊", "inpatient": "住院"}

router = APIRouter(prefix="/api", tags=["就诊与健康档案"], dependencies=[Depends(get_current_user)])


@router.post(
    "/encounters",
    response_model=EncounterOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 就诊记录=医疗岗
)
def create_encounter(
    body: EncounterCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    # P0-35：机构由请求声明，原先只查它存不存在——乙院医生以甲院名义建就诊 201，进甲院门诊量，
    # 还凭空给甲院造出一条「就诊过」的服务关系（可见性判定首先看它）。
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    encounter = Encounter(**body.model_dump())
    db.add(encounter)
    db.flush()
    events.publish(db, events.ENCOUNTER_CREATED, {
        "encounter_id": encounter.id,
        "patient_id": encounter.patient_id,
        "org_id": encounter.org_id,
        "encounter_type": encounter.encounter_type,
        "diagnosis_code": encounter.diagnosis_code or "",
        "diagnosis_name": encounter.diagnosis_name or "",
    })
    db.commit()
    db.refresh(encounter)
    return _encounters_out(db, [encounter])[0]


def _encounters_out(db: Session, encounters: list[Encounter]) -> list[dict]:
    """就诊行出参：登记回执与就诊清单都从这里出（同形）。

    末尾两键是 P2-1631 加的（只增键）：就诊时刻与患者姓名——门急诊文书、门诊病历按手输的就诊号定位，接诊页原先只有编号，
    认不出是哪天、哪位的就诊。姓名按这一批的患者号取一次（与住院行 `inpatient._admissions_out` 的 P2-1335 同一写法），
    不逐行查库：清单一页最多 500 行。姓名不是加密列，PII 加密开态下照旧直读。
    """
    if not encounters:
        return []
    patients = row_dict(
        db.query(Patient.id, Patient.name).filter(Patient.id.in_({e.patient_id for e in encounters})).all()
    )
    return [
        {
            "patient_id": e.patient_id,
            "org_id": e.org_id,
            "doctor_name": e.doctor_name,
            "encounter_type": e.encounter_type,
            "diagnosis_code": e.diagnosis_code,
            "diagnosis_name": e.diagnosis_name,
            "summary": e.summary,
            "id": e.id,
            "created_at": e.created_at.isoformat(),
            "patient_name": patients.get(e.patient_id, ""),
        }
        for e in encounters
    ]


@router.get("/encounters", response_model=list[EncounterOut])
def list_encounters(
    response: Response,
    patient_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """就诊记录列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。

    第九轮：**不带 patient_id 时只返回本机构的就诊记录**。原先返回全表——
    任何一个登录账号翻一页就拿到全县的就诊记录，这是最直接的横向越权。
    带了 patient_id 则走患者可见性判定（并留痕）。
    """
    query = db.query(Encounter)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="encounter")
        query = query.filter(Encounter.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(Encounter.org_id.in_(orgs))
    # 按就诊时刻倒序、编号兜底（P2-846）：导入的历史就诊 created_at 是就诊日期、编号却排在上线之后，原先按编号倒序，
    # 第一页全是几年前的导入记录
    rows = paginate(query.order_by(Encounter.created_at.desc(), Encounter.id.desc()), response, offset, limit)
    return _encounters_out(db, rows)   # 行上带就诊时刻与姓名（P2-1631）


# 360 视图每类记录的返回上限：与居民端 portal._build_archive 保持一致。
# 一位管了十年的慢病患者能攒出数百条就诊与上千条处方，全量返回体积不可控（T6.4）。
ARCHIVE_SECTION_LIMIT = 50


def _section(query, limit: int = ARCHIVE_SECTION_LIMIT) -> tuple[list, bool]:
    """取最近 limit 条，并判断是否还有更多（多取一条来判定，不额外做 count）。

    「最近」按业务时刻排（P2-846）：就诊、处方、结算的 created_at 就是就诊 / 开方 / 结算时刻（存量导入照写业务日期，
    `scripts/import_legacy.py`），编号只是入库先后——上线后再导入的历史记录编号更大，原先按编号倒序，前 50 条全是几年前
    的导入记录，真正最近的那次被截掉。编号只做同一时刻的兜底。"""
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


# ---------------------------------------------------------------------------
# 全景 360 视图的响应契约（CLAUDE.md §11：每个端点声明 response_model）
# ---------------------------------------------------------------------------
#
# 字段与函数末尾那个 dict **一一对应、顺序一致**——治理不得改响应字节，
# 由 `tests/test_archive_360_contract.py` 的特征化网守住。
# 各段单独建模而不是 `dict[str, Any]`：写成 Any 等于没声明契约，
# 而这个接口恰恰是最需要契约的那个——它一次吐出一个人的全部诊疗信息。


class ArchiveHasMore(BaseModel):
    encounters: bool
    exam_reports: bool
    prescriptions: bool
    checkups: bool
    settlements: bool


class ArchivePatient(BaseModel):
    ehc_no: str
    name: str
    gender: str
    birth_date: str


class ArchiveEncounter(BaseModel):
    id: int
    org_id: int
    encounter_type: str
    diagnosis_name: str
    summary: str
    #: 就诊时刻（P2-1196，只加键）：本段正是按它截取的（P2-846），原先不给，看的人看不到排序依据；
    #: 与同一响应里结算段的 `created_at` 同一写法（`isoformat()`）
    created_at: str


class ArchiveExamReport(BaseModel):
    id: int
    request_id: int
    conclusion: str
    critical: bool
    #: 以下三个键是 P2-1196 加的（只加键）：原先只有结论与「是否危急值」，分不清是哪项检查、哪天出的、危急值处置了没有。
    #: 检查项目取自申请单；报告时刻同结算段的写法；危急值闭环状态给码（""=非危急值 / notified / acknowledged / resolved，
    #: 存量危急报告的空串等同 notified）——文案各端自有一套，医生移动端是接收方视角的 `CRITICAL_TAGS`（见 exams.CRITICAL_STATUS_NAMES）
    item_name: str
    reported_at: str
    critical_status: str


class ArchiveChronic(BaseModel):
    id: int
    disease: str
    level: int
    next_due: str


class ArchivePrescription(BaseModel):
    id: int
    diagnosis_name: str
    status: str
    #: 开方时刻（P2-1196，只加键）：同就诊段
    created_at: str
    #: 以下两个键是 P2-1198 加的（只加键；行保留，开过这张方是事实）：处方状态是审方结论，退药冲销不动它——原先退掉的那张
    #: 照原审方状态列出，医生当它还在吃（用药画像早按 P2-624 排除了）；药师退回的只给英文码。中文名表外的码原样回显
    status_name: str
    dispense_reversed: bool


class ArchiveSettlement(BaseModel):
    id: int
    bill_type: str
    total_amount: float
    insurance_pay: float
    self_pay: float
    created_at: str


class ArchivePhysicalExam(BaseModel):
    id: int
    exam_date: str
    package_name: str
    has_abnormal: bool
    abnormal_items: str
    #: 给人看的异常项（P2-1197，只加键）：汇总串选填，只在分项上标了异常的体检 `abnormal_items` 是空串，360 原先只给它——
    #: 「有异常」却看不出哪项。与体检清单、异常清单、打印件同一口径（`checkups.abnormal_text`，P2-422）
    abnormal_text: str


class Archive360Out(BaseModel):
    section_limit: int
    has_more: ArchiveHasMore
    patient: ArchivePatient
    encounters: list[ArchiveEncounter]
    exam_reports: list[ArchiveExamReport]
    chronic_diseases: list[ArchiveChronic]
    prescriptions: list[ArchivePrescription]
    settlements: list[ArchiveSettlement]
    physical_exams: list[ArchivePhysicalExam]


@router.get("/archive/{ehc_no}", response_model=Archive360Out)
def patient_360_view(
    ehc_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """患者全景360视图：档案、就诊、检查检验报告、慢病、处方一屏汇聚。

    各段取最近 ARCHIVE_SECTION_LIMIT 条，`has_more` 标明是否被截断；
    需要完整清单时走各自的分页列表接口。

    第九轮：这是全平台聚合度最高的一个接口——一次调用拿到一个人的就诊、
    检查、慢病、处方。**它此前对任何登录账号开放**，实测乙镇卫生院的医生
    凭 ehc_no 就能看甲县医院患者的全部诊疗信息。现在须有业务关系并留痕。
    """
    patient = find_by_ehc_no(db, ehc_no)   # 手输卡号的小写、首尾空白也认（P2-791）
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assert_patient_visible(db, user, patient.id, resource="archive_360")
    encounters, encounters_more = _section(
        db.query(Encounter).filter(Encounter.patient_id == patient.id)
        .order_by(Encounter.created_at.desc(), Encounter.id.desc())
    )
    # 检查项目在申请单上（P2-1196）：本来就 join 申请单判患者，顺带取出项目名，不逐行再查
    reports, reports_more = _section(
        db.query(ExamReport, ExamRequest.item_name)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
    )
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient.id).all()
    prescriptions, prescriptions_more = _section(
        db.query(Prescription).filter(Prescription.patient_id == patient.id)
        .order_by(Prescription.created_at.desc(), Prescription.id.desc())
    )
    # 已退药的（P2-1198）：判据照用量统计那一句（`dispense.prescription_not_reversed`，P2-624），这一段一条 SQL 取回
    reversed_prescriptions: set[int] = set()
    if prescriptions:
        reversed_prescriptions = {
            rx_id
            for (rx_id,) in db.query(Prescription.id)
            .filter(Prescription.id.in_([p.id for p in prescriptions]), ~prescription_not_reversed())
        }
    checkups, checkups_more = _section(
        db.query(PhysicalExam).filter(PhysicalExam.patient_id == patient.id).order_by(PhysicalExam.id.desc())
    )
    checkup_abnormal_names = _abnormal_item_names(db, [e.id for e in checkups])   # 一条 SQL 取回（P2-1197）
    # 医疗费用记录（指南 #2 病历概要要求的第三类内容）。
    # 取结算单而非费用明细：明细是一次就诊几十上百条，塞进 360 视图会把真正
    # 该被看见的临床信息挤下去；要看明细走 /api/billing/details。
    settlements, settlements_more = _section(
        db.query(Settlement)
        .filter(Settlement.patient_id == patient.id)
        .order_by(Settlement.created_at.desc(), Settlement.id.desc())
    )
    return {
        "section_limit": ARCHIVE_SECTION_LIMIT,
        "has_more": {
            "encounters": encounters_more,
            "exam_reports": reports_more,
            "prescriptions": prescriptions_more,
            "checkups": checkups_more,
            "settlements": settlements_more,
        },
        "patient": {
            "ehc_no": patient.ehc_no,
            "name": patient.name,
            "gender": patient.gender,
            "birth_date": patient.birth_date,
        },
        "encounters": [
            {
                "id": e.id,
                "org_id": e.org_id,
                "encounter_type": e.encounter_type,
                "diagnosis_name": e.diagnosis_name,
                "summary": e.summary,
                "created_at": e.created_at.isoformat(),
            }
            for e in encounters
        ],
        "exam_reports": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "conclusion": r.conclusion,
                "critical": r.critical,
                "item_name": item_name,
                "reported_at": r.reported_at.isoformat(),
                "critical_status": r.critical_status,
            }
            for r, item_name in reports
        ],
        "chronic_diseases": [
            {"id": c.id, "disease": c.disease, "level": c.level, "next_due": c.next_due} for c in chronic
        ],
        "prescriptions": [
            {
                "id": p.id,
                "diagnosis_name": p.diagnosis_name,
                "status": p.status,
                "created_at": p.created_at.isoformat(),
                "status_name": PRESCRIPTION_STATUS_NAMES.get(p.status, p.status),
                "dispense_reversed": p.id in reversed_prescriptions,
            }
            for p in prescriptions
        ],
        "settlements": [
            {
                "id": s.id,
                "bill_type": s.bill_type,
                "total_amount": s.total_amount,
                "insurance_pay": s.insurance_pay,
                "self_pay": s.self_pay,
                "created_at": s.created_at.isoformat(),
            }
            for s in settlements
        ],
        "physical_exams": [
            {
                "id": e.id,
                "exam_date": e.exam_date,
                "package_name": e.package_name,
                "has_abnormal": e.has_abnormal,
                "abnormal_items": e.abnormal_items,
                "abnormal_text": checkup_abnormal_text(e.abnormal_items, checkup_abnormal_names.get(e.id, [])),
            }
            for e in checkups
        ],
    }
