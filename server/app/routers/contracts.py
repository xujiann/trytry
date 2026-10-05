"""家庭医生签约：协议管理、服务包、履约记录。"""
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from .. import clock
from ..concurrency import insert_or_conflict
from ..visibility import assert_obj_org_writable, assert_patient_visible, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, row_dict
from ..models import ContractService, FamilyDoctorContract, Organization, Patient, User
from ..schemas import ContractCreate, ContractOut, ContractRowOut, ContractServiceCreate, ContractServiceOut

router = APIRouter(prefix="/api/contracts", tags=["家庭医生签约"], dependencies=[Depends(get_current_user)])


@router.post(
    "",
    response_model=ContractOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 家医签约
)
def sign(body: ContractCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    # 签约日期不得晚于今天（P2-1548，与接种日期 P2-1304 / 发病日期 P2-454 同一句）：记的是已经签下的那一天，原先只查格式——
    # 填成 2099-01-01 照样 201、当即「履约中」，这份将来才生效的协议上照样记履约。同机构曾解约的再签走下面「重新激活」那条路，
    # 改写的也是这个日期，所以判在两条路之前。留空照旧不判（能不能留空随签约期 P2-1037 另定）
    if body.signed_date and body.signed_date > clock.today().isoformat():
        raise HTTPException(status_code=422, detail=f"签约日期（{body.signed_date}）不得晚于今天")
    existing = (
        db.query(FamilyDoctorContract)
        .filter(
            FamilyDoctorContract.patient_id == body.patient_id,
            FamilyDoctorContract.org_id == body.org_id,
        )
        .first()
    )
    if existing:
        if existing.status == "active":
            raise HTTPException(status_code=409, detail="该居民已在本机构签约")
        # 曾解约的重新激活并更新协议内容
        existing.status = "active"
        existing.doctor_name = body.doctor_name
        existing.package = body.package
        existing.signed_date = body.signed_date
        db.commit()
        db.refresh(existing)
        return existing
    # 并发下两个请求都查不到既有签约就都去插；撞了唯一约束，
    # 结论与上面那条查重一致——该居民已在本机构签约。
    return insert_or_conflict(
        db, FamilyDoctorContract(**body.model_dump()), "该居民已在本机构签约"
    )


def _contract_rows(db: Session, contracts: list[FamilyDoctorContract]) -> list[dict]:
    """清单行配上患者姓名与机构名（P2-1547）：签约页原先只印患者号、机构号，认人还得拿编号回档案里对。

    按这一批的患者号、机构号各取一次，不逐行查库（照 `surveys.list_surveys` 的 P2-1153）；姓名不是加密列，PII 加密
    开态下照旧直读。只给清单（已按 `scope_patient_list` 收口）：签约与解约的回执照旧不带——签约接口不判调用方与这位
    患者的关系（P1-45，待裁定），回执要是带姓名，任意一个患者号签一下就能查出是谁。
    """
    patient_ids = {c.patient_id for c in contracts}
    org_ids = {c.org_id for c in contracts}
    patients = (
        row_dict(db.query(Patient.id, Patient.name).filter(Patient.id.in_(patient_ids)).all())
        if patient_ids
        else {}
    )
    orgs = (
        row_dict(db.query(Organization.id, Organization.name).filter(Organization.id.in_(org_ids)).all())
        if org_ids
        else {}
    )
    return [
        {
            "patient_id": c.patient_id,
            "org_id": c.org_id,
            "doctor_name": c.doctor_name,
            "package": c.package,
            "signed_date": c.signed_date,
            "id": c.id,
            "status": c.status,
            "patient_name": patients.get(c.patient_id, ""),
            "org_name": orgs.get(c.org_id, ""),
        }
        for c in contracts
    ]


@router.get("", response_model=list[ContractRowOut])
def list_contracts(
    response: Response,
    org_id: int | None = None,
    patient_id: int | None = None,
    # 取值照签约状态列注释（active / terminated），写错 422，不静默当成「不筛」（P2-1547）
    status: str | None = Query(default=None, pattern="^(active|terminated)$"),
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """签约清单：家医签约页那张表，「记录履约 / 解约 / 履约记录」只摆在这张表的行上。

    按状态筛、行上带患者姓名与机构名（P2-1547）：原先只收 `org_id` / `patient_id`、按编号倒序缺省 500 条，`?status=`
    被静默忽略，页面不带参数只取这一页、行上只印两个编号——签约过 500 份，越早签的越先挤出这一页，该续约、解约的那批
    在页面上没有行。页面现在按状态、按患者号查，总数读 X-Total-Count。`status` 叠在可见范围（`scope_patient_list`）
    之后，只收窄、不绕过它；姓名与机构名按这一页一批取，分页与既有字段不变（新字段只加在末尾）。
    """
    query = db.query(FamilyDoctorContract)
    if org_id is not None:
        query = query.filter(FamilyDoctorContract.org_id == org_id)
    query = scope_patient_list(db, user, query, FamilyDoctorContract, patient_id, "contract")
    if status:
        query = query.filter(FamilyDoctorContract.status == status)
    rows = paginate(query.order_by(FamilyDoctorContract.id.desc()), response, offset, limit)
    return _contract_rows(db, rows)


@router.post(
    "/{contract_id}/terminate",
    response_model=ContractOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2
)
def terminate(contract_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contract = db.get(FamilyDoctorContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
    assert_obj_org_writable(db, user, contract)
    if contract.status != "active":
        raise HTTPException(status_code=409, detail="协议已解约")
    contract.status = "terminated"
    db.commit()
    db.refresh(contract)
    return contract


@router.post(
    "/{contract_id}/services",
    response_model=ContractServiceOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 履约记录
)
def record_service(contract_id: int, body: ContractServiceCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    contract = db.get(FamilyDoctorContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
    assert_obj_org_writable(db, user, contract)
    if contract.status != "active":
        raise HTTPException(status_code=409, detail="已解约协议不可记录履约")
    service = ContractService(contract_id=contract_id, **body.model_dump())
    db.add(service)
    db.commit()
    db.refresh(service)
    return _service_out(service)


def _service_out(service: ContractService) -> dict:
    return {"service_type": service.service_type, "note": service.note, "id": service.id,
            "contract_id": service.contract_id,
            "created_at": service.created_at.isoformat() if service.created_at else ""}


@router.get("/{contract_id}/services", response_model=list[ContractServiceOut])
def list_services(
    contract_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    contract = db.get(FamilyDoctorContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
    # 服务记录挂在签约协议上，按协议患者做可见性判定 + 留痕
    assert_patient_visible(db, user, contract.patient_id, resource="contract")
    return [
        _service_out(s)
        for s in db.query(ContractService)
        .filter(ContractService.contract_id == contract_id)
        .order_by(ContractService.id.desc())
        .all()
    ]
