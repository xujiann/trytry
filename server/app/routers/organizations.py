from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Organization
from ..schemas import OrganizationCreate, OrganizationOut

router = APIRouter(prefix="/api/organizations", tags=["机构管理"])


@router.post("", response_model=OrganizationOut, status_code=201, dependencies=[Depends(require_admin)])
def create_organization(body: OrganizationCreate, db: Session = Depends(get_db)):
    if db.query(Organization).filter(Organization.name == body.name).first():
        raise HTTPException(status_code=409, detail="机构已存在")
    if body.parent_id is not None and db.get(Organization, body.parent_id) is None:
        raise HTTPException(status_code=404, detail="上级机构不存在")
    org = Organization(**body.model_dump())
    db.add(org)
    db.commit()
    db.refresh(org)
    return org


@router.get("", response_model=list[OrganizationOut], dependencies=[Depends(get_current_user)])
def list_organizations(level: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Organization)
    if level:
        query = query.filter(Organization.level == level)
    return query.order_by(Organization.id).all()
