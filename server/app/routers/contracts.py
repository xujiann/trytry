"""家庭医生签约：协议管理、服务包、履约记录。"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ContractService, FamilyDoctorContract, Organization, Patient
from ..schemas import ContractCreate, ContractOut, ContractServiceCreate, ContractServiceOut

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
    contract = FamilyDoctorContract(**body.model_dump())
    db.add(contract)
    db.commit()
    db.refresh(contract)
    return contract


@router.get("", response_model=list[ContractOut])
def list_contracts(org_id: int | None = None, patient_id: int | None = None, db: Session = Depends(get_db)):
    query = db.query(FamilyDoctorContract)
    if org_id is not None:
        query = query.filter(FamilyDoctorContract.org_id == org_id)
    if patient_id is not None:
        query = query.filter(FamilyDoctorContract.patient_id == patient_id)
    return query.order_by(FamilyDoctorContract.id.desc()).limit(500).all()


@router.post(
    "/{contract_id}/terminate",
    response_model=ContractOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2
)
def terminate(contract_id: int, db: Session = Depends(get_db)):
    contract = db.get(FamilyDoctorContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
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
def record_service(contract_id: int, body: ContractServiceCreate, db: Session = Depends(get_db)):
    contract = db.get(FamilyDoctorContract, contract_id)
    if contract is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
    if contract.status != "active":
        raise HTTPException(status_code=409, detail="已解约协议不可记录履约")
    service = ContractService(contract_id=contract_id, **body.model_dump())
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


@router.get("/{contract_id}/services", response_model=list[ContractServiceOut])
def list_services(contract_id: int, db: Session = Depends(get_db)):
    if db.get(FamilyDoctorContract, contract_id) is None:
        raise HTTPException(status_code=404, detail="签约协议不存在")
    return (
        db.query(ContractService)
        .filter(ContractService.contract_id == contract_id)
        .order_by(ContractService.id.desc())
        .all()
    )
