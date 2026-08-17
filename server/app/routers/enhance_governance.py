"""S16 医共体治理架构：章程 / 理事会·管委会 / 编制周转池（县管乡用）。

治理是**联合体自身**的组织事务，是一项 consortium 级的管理职能，不落到任何单个
成员机构名下——章程一份、治理机构一套、编制周转池全县统筹。因此这里的可见性
与 fund 模块同档：只有全域角色（admin/director）看得见、写得动，**清单一律是
全联合体视图，不做 scope_org_list 机构横向切分**（切分反而把管理职能关掉了）。

写入的角色档：
- 章程（create/activate）归 admin——章程是根本制度，收口到系统管理员；
- 治理机构、成员、编制周转调配归 director（管委会/业务院长）；admin 天然通过。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models.core import Employee, Organization
from ..models.enhance_governance import (
    ConsortiumCharter,
    GovernanceBody,
    GovernanceMember,
    StaffPool,
)

router = APIRouter(
    prefix="/api/governance",
    tags=["医共体治理架构"],
    dependencies=[Depends(get_current_user)],
)


# ============================================================ 序列化


def _charter_out(c: ConsortiumCharter) -> dict:
    return {
        "id": c.id,
        "title": c.title,
        "version": c.version,
        "content": c.content,
        "effective_date": c.effective_date,
        "active": c.active,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _body_out(b: GovernanceBody) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "body_type": b.body_type,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _member_out(m: GovernanceMember) -> dict:
    return {
        "id": m.id,
        "body_id": m.body_id,
        "member_name": m.member_name,
        "title": m.title,
        "org_id": m.org_id,
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _pool_out(p: StaffPool) -> dict:
    return {
        "id": p.id,
        "employee_id": p.employee_id,
        "member_name": p.member_name,
        "from_org_id": p.from_org_id,
        "to_org_id": p.to_org_id,
        "assign_type": p.assign_type,
        "start_date": p.start_date,
        "end_date": p.end_date,
        "status": p.status,
        "created_at": p.created_at.isoformat() if p.created_at else None,
    }


# ============================================================ 章程


class CharterIn(BaseModel):
    title: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=16)
    content: str = Field(default="", max_length=4000)
    effective_date: str = Field(default="", max_length=10)


@router.post("/charters", status_code=201, dependencies=[Depends(require_roles("admin"))])
def create_charter(body: CharterIn, db: Session = Depends(get_db)):
    """新增章程版本。(title, version) 唯一，撞了给 409——章程修订换版是常态，
    但同一版本不该录两份。"""
    charter = ConsortiumCharter(
        title=body.title,
        version=body.version,
        content=body.content,
        effective_date=body.effective_date,
    )
    insert_or_conflict(db, charter, detail="同名同版本的章程已存在")
    return _charter_out(charter)


@router.get("/charters", dependencies=[Depends(require_roles("director"))])
def list_charters(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(ConsortiumCharter)
    if active is not None:
        query = query.filter(ConsortiumCharter.active == active)
    return [_charter_out(c) for c in query.order_by(ConsortiumCharter.id.desc()).all()]


@router.patch("/charters/{charter_id}/activate", dependencies=[Depends(require_roles("admin"))])
def activate_charter(charter_id: int, db: Session = Depends(get_db)):
    """把某版本设为现行章程：先把其余全部熄灭，再点亮这一份——全局仅一份现行。
    先熄再亮而不是反过来，避免中间态出现两份现行。"""
    charter = db.get(ConsortiumCharter, charter_id)
    if charter is None:
        raise HTTPException(status_code=404, detail="章程不存在")
    db.query(ConsortiumCharter).filter(ConsortiumCharter.id != charter_id).update(
        {ConsortiumCharter.active: False}
    )
    charter.active = True
    db.commit()
    db.refresh(charter)
    return _charter_out(charter)


# ============================================================ 治理机构（理事会/管委会/监事会）


class BodyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    body_type: str = Field(pattern="^(council|committee|supervisory)$")


@router.post("/bodies", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_body(body: BodyIn, db: Session = Depends(get_db)):
    obj = GovernanceBody(name=body.name, body_type=body.body_type)
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _body_out(obj)


@router.get("/bodies", dependencies=[Depends(require_roles("director"))])
def list_bodies(db: Session = Depends(get_db)):
    return [
        _body_out(b)
        for b in db.query(GovernanceBody).order_by(GovernanceBody.id.desc()).all()
    ]


class MemberIn(BaseModel):
    member_name: str = Field(min_length=1, max_length=64)
    title: str = Field(default="", max_length=32)
    org_id: int


@router.post(
    "/bodies/{body_id}/members",
    status_code=201,
    dependencies=[Depends(require_roles("director"))],
)
def add_member(body_id: int, body: MemberIn, db: Session = Depends(get_db)):
    if db.get(GovernanceBody, body_id) is None:
        raise HTTPException(status_code=404, detail="治理机构不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="所代表的机构不存在")
    member = GovernanceMember(
        body_id=body_id,
        member_name=body.member_name,
        title=body.title,
        org_id=body.org_id,
    )
    db.add(member)
    db.commit()
    db.refresh(member)
    return _member_out(member)


@router.get("/bodies/{body_id}/members", dependencies=[Depends(require_roles("director"))])
def list_members(body_id: int, db: Session = Depends(get_db)):
    if db.get(GovernanceBody, body_id) is None:
        raise HTTPException(status_code=404, detail="治理机构不存在")
    rows = (
        db.query(GovernanceMember)
        .filter(GovernanceMember.body_id == body_id)
        .order_by(GovernanceMember.id)
        .all()
    )
    return [_member_out(m) for m in rows]


# ============================================================ 编制周转池·县管乡用


class StaffPoolIn(BaseModel):
    employee_id: int | None = None
    member_name: str = Field(default="", max_length=64)
    from_org_id: int
    to_org_id: int
    assign_type: str = Field(pattern="^(county_manage_town_use|rotation|support)$")
    start_date: str = Field(default="", max_length=10)
    end_date: str = Field(default="", max_length=10)


@router.post("/staff-pool", status_code=201, dependencies=[Depends(require_roles("director"))])
def create_staff_assignment(body: StaffPoolIn, db: Session = Depends(get_db)):
    """一次编制周转调配：从 from_org 调到 to_org。校验两端机构存在（404），
    且不能调到同一家（422）——原地调配没有意义，多半是录入手误。"""
    if body.from_org_id == body.to_org_id:
        raise HTTPException(status_code=422, detail="调出与调入机构不能相同")
    if db.get(Organization, body.from_org_id) is None:
        raise HTTPException(status_code=404, detail="调出机构不存在")
    if db.get(Organization, body.to_org_id) is None:
        raise HTTPException(status_code=404, detail="调入机构不存在")
    if body.employee_id is not None and db.get(Employee, body.employee_id) is None:
        raise HTTPException(status_code=404, detail="职工不存在")
    obj = StaffPool(
        employee_id=body.employee_id,
        member_name=body.member_name,
        from_org_id=body.from_org_id,
        to_org_id=body.to_org_id,
        assign_type=body.assign_type,
        start_date=body.start_date,
        end_date=body.end_date,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return _pool_out(obj)


@router.get("/staff-pool", dependencies=[Depends(require_roles("director"))])
def list_staff_assignments(
    status: str | None = None,
    to_org_id: int | None = None,
    db: Session = Depends(get_db),
):
    """编制周转清单：consortium 级管理视图，全联合体可见，不做机构横向切分。"""
    query = db.query(StaffPool)
    if status:
        query = query.filter(StaffPool.status == status)
    if to_org_id is not None:
        query = query.filter(StaffPool.to_org_id == to_org_id)
    return [_pool_out(p) for p in query.order_by(StaffPool.id.desc()).all()]


@router.patch("/staff-pool/{pool_id}/end", dependencies=[Depends(require_roles("director"))])
def end_staff_assignment(pool_id: int, db: Session = Depends(get_db)):
    obj = db.get(StaffPool, pool_id)
    if obj is None:
        raise HTTPException(status_code=404, detail="编制周转记录不存在")
    obj.status = "ended"
    db.commit()
    db.refresh(obj)
    return _pool_out(obj)
