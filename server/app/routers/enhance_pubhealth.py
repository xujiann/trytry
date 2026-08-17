"""E4 公共卫生短板补全接口。

覆盖四块县域公卫短板：
- 严重精神障碍患者管理与随访（0-5 级危险性评估、关锁、用药依从性）；
- 结核病患者管理、督导服药（DOT）随访、密切接触者筛查；
- 卫生监督协管巡查（食品/饮用水/学校/职业/放射）；
- 公卫 12 类项目全域目录、机构完成度登记与覆盖率看板。

横向隔离（A案）：
- 精神障碍/结核患者及其随访是高敏患者关联数据，按患者可见性 + 机构归属双重收口；
- 卫生监督为机构自有数据，按机构可见范围过滤；
- 公卫项目目录是 admin 全域目录，不做机构隔离；完成度登记按机构归属校验，
  看板按调用者可见机构范围聚合。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field

from ..database import get_db
from ..deps import get_current_user, require_roles, paginate
from ..models import (
    User,
    Patient,
    Organization,
    PsychPatient,
    PsychFollowup,
    TbPatient,
    TbFollowup,
    TbContact,
    HealthSupervision,
    PublicHealthProject,
    PublicHealthProjectStat,
)
from ..visibility import (
    assert_org_writable,
    assert_org_visible,
    scope_org_list,
    scope_patient_list,
    assert_obj_org_writable,
    assert_obj_org_visible,
    assert_patient_visible,
    visible_org_ids,
)

router = APIRouter(prefix="/api", tags=["公卫短板"], dependencies=[Depends(get_current_user)])

# 精神障碍/结核患者关联数据的敏感读留痕资源名
_RESOURCE = "pubhealth"

_PSYCH_WRITE = Depends(require_roles("doctor", "public_health"))
_TB_WRITE = Depends(require_roles("doctor", "public_health"))
_SUPERVISION_WRITE = Depends(require_roles("public_health", "operator"))
_STAT_WRITE = Depends(require_roles("public_health"))


# ==================== 严重精神障碍患者管理 ====================


class PsychPatientCreate(BaseModel):
    patient_id: int
    org_id: int
    diagnosis: str = Field(default="", max_length=128)
    danger_level: str = Field(default="0", pattern="^[0-5]$")
    guardian: str = Field(default="", max_length=64)
    guardian_phone: str = Field(default="", max_length=32)
    locked: bool = False
    status: str = Field(default="in_manage", max_length=16)


class PsychPatientOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    diagnosis: str
    danger_level: str
    guardian: str
    guardian_phone: str
    locked: bool
    status: str

    model_config = {"from_attributes": True}


class PsychFollowupCreate(BaseModel):
    followup_date: str = Field(default="", max_length=10)
    medication_adherence: str = Field(default="", pattern="^(good|partial|none|)$")
    stability: str = Field(default="", max_length=16)
    note: str = Field(default="", max_length=512)


class PsychFollowupOut(BaseModel):
    id: int
    psych_id: int
    org_id: int
    followup_date: str
    medication_adherence: str
    stability: str
    note: str

    model_config = {"from_attributes": True}


@router.post("/psych/patients", response_model=PsychPatientOut, status_code=201, dependencies=[_PSYCH_WRITE])
def create_psych_patient(
    body: PsychPatientCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """严重精神障碍建档：只能以本机构名义建档（全域角色不限）。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    obj = PsychPatient(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/psych/patients", response_model=list[PsychPatientOut])
def list_psych_patients(
    org_id: int | None = None,
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """精神障碍患者清单：高敏，按患者可见性/机构范围收口。"""
    query = db.query(PsychPatient)
    if org_id is not None:
        query = query.filter(PsychPatient.org_id == org_id)
    query = scope_patient_list(db, user, query, PsychPatient, patient_id, _RESOURCE)
    return query.order_by(PsychPatient.id.desc()).limit(200).all()


@router.post("/psych/patients/{psych_id}/followups", response_model=PsychFollowupOut, status_code=201, dependencies=[_PSYCH_WRITE])
def add_psych_followup(
    psych_id: int,
    body: PsychFollowupCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parent = db.get(PsychPatient, psych_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="精神障碍档案不存在")
    assert_obj_org_writable(db, user, parent)
    obj = PsychFollowup(psych_id=psych_id, org_id=parent.org_id, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/psych/patients/{psych_id}/followups", response_model=list[PsychFollowupOut])
def list_psych_followups(
    psych_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parent = db.get(PsychPatient, psych_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="精神障碍档案不存在")
    assert_obj_org_visible(db, user, parent)
    return (
        db.query(PsychFollowup)
        .filter(PsychFollowup.psych_id == psych_id)
        .order_by(PsychFollowup.id.desc())
        .all()
    )


# ==================== 结核病患者管理 ====================


class TbPatientCreate(BaseModel):
    patient_id: int
    org_id: int
    tb_type: str = Field(default="", max_length=32)
    treatment_start: str = Field(default="", max_length=10)
    regimen: str = Field(default="", max_length=64)
    status: str = Field(default="in_treatment", max_length=16)


class TbPatientOut(BaseModel):
    id: int
    patient_id: int
    org_id: int
    tb_type: str
    treatment_start: str
    regimen: str
    status: str

    model_config = {"from_attributes": True}


class TbFollowupCreate(BaseModel):
    followup_date: str = Field(default="", max_length=10)
    taken: bool = False
    sputum_result: str = Field(default="", max_length=16)
    note: str = Field(default="", max_length=512)


class TbFollowupOut(BaseModel):
    id: int
    tb_id: int
    org_id: int
    followup_date: str
    taken: bool
    sputum_result: str
    note: str

    model_config = {"from_attributes": True}


class TbContactCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    relation: str = Field(default="", max_length=32)
    screened: bool = False
    result: str = Field(default="", max_length=64)


class TbContactOut(BaseModel):
    id: int
    tb_id: int
    org_id: int
    name: str
    relation: str
    screened: bool
    result: str

    model_config = {"from_attributes": True}


@router.post("/tb/patients", response_model=TbPatientOut, status_code=201, dependencies=[_TB_WRITE])
def create_tb_patient(
    body: TbPatientCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    obj = TbPatient(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/tb/patients", response_model=list[TbPatientOut])
def list_tb_patients(
    org_id: int | None = None,
    patient_id: int | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(TbPatient)
    if org_id is not None:
        query = query.filter(TbPatient.org_id == org_id)
    query = scope_patient_list(db, user, query, TbPatient, patient_id, _RESOURCE)
    return query.order_by(TbPatient.id.desc()).limit(200).all()


@router.post("/tb/patients/{tb_id}/followups", response_model=TbFollowupOut, status_code=201, dependencies=[_TB_WRITE])
def add_tb_followup(
    tb_id: int,
    body: TbFollowupCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """结核督导服药（DOT）留痕。"""
    parent = db.get(TbPatient, tb_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="结核病档案不存在")
    assert_obj_org_writable(db, user, parent)
    obj = TbFollowup(tb_id=tb_id, org_id=parent.org_id, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/tb/patients/{tb_id}/followups", response_model=list[TbFollowupOut])
def list_tb_followups(
    tb_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parent = db.get(TbPatient, tb_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="结核病档案不存在")
    assert_obj_org_visible(db, user, parent)
    return (
        db.query(TbFollowup)
        .filter(TbFollowup.tb_id == tb_id)
        .order_by(TbFollowup.id.desc())
        .all()
    )


@router.post("/tb/patients/{tb_id}/contacts", response_model=TbContactOut, status_code=201, dependencies=[_TB_WRITE])
def add_tb_contact(
    tb_id: int,
    body: TbContactCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """密切接触者筛查登记。"""
    parent = db.get(TbPatient, tb_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="结核病档案不存在")
    assert_obj_org_writable(db, user, parent)
    obj = TbContact(tb_id=tb_id, org_id=parent.org_id, **body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/tb/patients/{tb_id}/contacts", response_model=list[TbContactOut])
def list_tb_contacts(
    tb_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    parent = db.get(TbPatient, tb_id)
    if parent is None:
        raise HTTPException(status_code=404, detail="结核病档案不存在")
    assert_obj_org_visible(db, user, parent)
    return (
        db.query(TbContact)
        .filter(TbContact.tb_id == tb_id)
        .order_by(TbContact.id.desc())
        .all()
    )


# ==================== 卫生监督协管巡查 ====================

_SUPERVISION_DOMAINS = {"food", "water", "school", "occupation", "radiation"}


class SupervisionCreate(BaseModel):
    org_id: int
    domain: str
    target: str = Field(min_length=1, max_length=128)
    inspect_date: str = Field(default="", max_length=10)
    findings: str = Field(default="", max_length=512)
    passed: bool = True


class SupervisionOut(BaseModel):
    id: int
    org_id: int
    domain: str
    target: str
    inspect_date: str
    findings: str
    passed: bool

    model_config = {"from_attributes": True}


@router.post("/health-supervision", response_model=SupervisionOut, status_code=201, dependencies=[_SUPERVISION_WRITE])
def create_supervision(
    body: SupervisionCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    assert_org_writable(db, user, body.org_id)
    if body.domain not in _SUPERVISION_DOMAINS:
        raise HTTPException(status_code=422, detail=f"未知监督领域: {body.domain}")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    obj = HealthSupervision(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/health-supervision", response_model=list[SupervisionOut])
def list_supervision(
    org_id: int | None = None,
    domain: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(HealthSupervision)
    if domain:
        query = query.filter(HealthSupervision.domain == domain)
    query = scope_org_list(db, user, query, HealthSupervision, org_id)
    return query.order_by(HealthSupervision.id.desc()).limit(200).all()


# ==================== 公卫 12 类项目 ====================


class ProjectCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="", max_length=32)
    active: bool = True


class ProjectOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    active: bool

    model_config = {"from_attributes": True}


@router.post("/pubhealth-projects", response_model=ProjectOut, status_code=201, dependencies=[Depends(require_roles("admin"))])
def create_project(body: ProjectCreate, db: Session = Depends(get_db)):
    """公卫 12 类项目全域目录建项（admin）。"""
    if db.query(PublicHealthProject).filter(PublicHealthProject.code == body.code).first():
        raise HTTPException(status_code=409, detail="项目编码已存在")
    obj = PublicHealthProject(**body.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@router.get("/pubhealth-projects", response_model=list[ProjectOut])
def list_projects(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(PublicHealthProject)
    if active is not None:
        query = query.filter(PublicHealthProject.active.is_(active))
    return query.order_by(PublicHealthProject.id).all()


class ProjectStatUpsert(BaseModel):
    project_id: int
    org_id: int
    year: str = Field(min_length=4, max_length=4)
    target_count: int = Field(default=0, ge=0)
    done_count: int = Field(default=0, ge=0)


class ProjectStatOut(BaseModel):
    id: int
    project_id: int
    org_id: int
    year: str
    target_count: int
    done_count: int

    model_config = {"from_attributes": True}


@router.post("/pubhealth-projects/stats", response_model=ProjectStatOut, status_code=201, dependencies=[_STAT_WRITE])
def upsert_project_stat(
    body: ProjectStatUpsert,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """机构项目完成度登记：按 (project_id, org_id, year) 幂等更新目标/完成数。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(PublicHealthProject, body.project_id) is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    stat = (
        db.query(PublicHealthProjectStat)
        .filter(
            PublicHealthProjectStat.project_id == body.project_id,
            PublicHealthProjectStat.org_id == body.org_id,
            PublicHealthProjectStat.year == body.year,
        )
        .first()
    )
    if stat is None:
        stat = PublicHealthProjectStat(**body.model_dump())
        db.add(stat)
    else:
        stat.target_count = body.target_count
        stat.done_count = body.done_count
    db.commit()
    db.refresh(stat)
    return stat


@router.get("/pubhealth-projects/dashboard")
def project_dashboard(
    year: str,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """项目完成度看板：按调用者可见机构范围聚合各项目目标/完成数与覆盖率。

    覆盖率 coverage_pct = 完成数 / 目标数 * 100，目标为 0 时置 0（防除零）。
    """
    allowed = visible_org_ids(db, user)  # None=全域
    stat_q = db.query(PublicHealthProjectStat).filter(PublicHealthProjectStat.year == year)
    if allowed is not None:
        if not allowed:
            stat_q = stat_q.filter(PublicHealthProjectStat.org_id.in_([-1]))
        else:
            stat_q = stat_q.filter(PublicHealthProjectStat.org_id.in_(allowed))
    agg: dict[int, dict[str, int]] = {}
    for s in stat_q.all():
        bucket = agg.setdefault(s.project_id, {"target": 0, "done": 0})
        bucket["target"] += s.target_count
        bucket["done"] += s.done_count
    out = []
    for p in db.query(PublicHealthProject).order_by(PublicHealthProject.id).all():
        bucket = agg.get(p.id, {"target": 0, "done": 0})
        target, done = bucket["target"], bucket["done"]
        coverage = round(done / target * 100, 2) if target > 0 else 0
        out.append(
            {
                "project_id": p.id,
                "code": p.code,
                "name": p.name,
                "category": p.category,
                "target_count": target,
                "done_count": done,
                "coverage_pct": coverage,
            }
        )
    return {"year": year, "projects": out}
