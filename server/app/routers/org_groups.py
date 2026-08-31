"""机构协作分组：机构之间的横向分组（片区 / 专科联盟 / 网格 / 其他）。

设计取舍见 `OrgGroup` 的模型注释，核心一句：分组与 `parent_id` 正交，
**不是第二棵机构树**。

统计侧的接入方式是 `deps.resolve_org_scope()`——统计接口加一个 `group_id`
可选参数即可，不必各自实现分组展开。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Organization, OrgGroup, OrgGroupMember

router = APIRouter(
    prefix="/api/org-groups", tags=["机构协作分组"], dependencies=[Depends(get_current_user)]
)

GROUP_TYPE_NAMES = {"zone": "片区/分片", "alliance": "专科联盟", "grid": "网格", "other": "其他"}


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    group_type: str = Field(default="zone", pattern="^(zone|alliance|grid|other)$")
    # 可空：网格化管理常常没有"牵头单位"这一说
    lead_org_id: int | None = None
    note: str = Field(default="", max_length=256)


class GroupUpdate(BaseModel):
    name: str | None = None
    lead_org_id: int | None = None
    note: str | None = None
    active: bool | None = None


class MemberIn(BaseModel):
    org_id: int


def _group_out(g: OrgGroup, member_count: int | None = None) -> dict:
    out = {
        "id": g.id,
        "name": g.name,
        "group_type": g.group_type,
        "group_type_name": GROUP_TYPE_NAMES.get(g.group_type, g.group_type),
        "lead_org_id": g.lead_org_id,
        "note": g.note,
        "active": g.active,
    }
    if member_count is not None:
        out["member_count"] = member_count
    return out


class OrgGroupOut(BaseModel):
    """字段与顺序精确镜像 `_group_out(g)`——更新回执与 of-org 行，**不带成员数键**。

    键集合按端点固定、不随数据变（创建/列表恒带 member_count，更新/of-org 恒不带），
    故拆两个模型而不是"可选字段 + exclude_unset"。
    """

    id: int
    name: str
    group_type: str
    group_type_name: str
    #: 牵头机构可空：网格化管理常常没有"牵头单位"这一说
    lead_org_id: int | None
    note: str
    active: bool


class OrgGroupWithCountOut(OrgGroupOut):
    """`_group_out(g, member_count)`：创建回执与列表行，多一个恒在的成员数（键序最后）。"""

    member_count: int


@router.post("", status_code=201, response_model=OrgGroupWithCountOut, dependencies=[Depends(require_admin)])
def create_group(body: GroupIn, db: Session = Depends(get_db)):
    if body.lead_org_id is not None and db.get(Organization, body.lead_org_id) is None:
        raise HTTPException(status_code=404, detail="牵头机构不存在")
    group = OrgGroup(**body.model_dump())
    db.add(group)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同名分组已存在") from None
    return _group_out(group, 0)


@router.get("", response_model=list[OrgGroupWithCountOut])
def list_groups(
    group_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    query = db.query(OrgGroup)
    if group_type:
        query = query.filter(OrgGroup.group_type == group_type)
    if active is not None:
        query = query.filter(OrgGroup.active.is_(active))
    groups = query.order_by(OrgGroup.id).limit(200).all()
    # 成员数一次查回来，避免 N+1
    counts: dict[int, int] = {}
    for m in db.query(OrgGroupMember).all():
        counts[m.group_id] = counts.get(m.group_id, 0) + 1
    return [_group_out(g, counts.get(g.id, 0)) for g in groups]


@router.patch("/{group_id}", response_model=OrgGroupOut, dependencies=[Depends(require_admin)])
def update_group(group_id: int, body: GroupUpdate, db: Session = Depends(get_db)):
    group = _get(db, group_id)
    changes = body.model_dump(exclude_unset=True)
    if "lead_org_id" in changes and changes["lead_org_id"] is not None:
        if db.get(Organization, changes["lead_org_id"]) is None:
            raise HTTPException(status_code=404, detail="牵头机构不存在")
    for field, value in changes.items():
        if value is not None:
            setattr(group, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="同名分组已存在") from None
    return _group_out(group)


class OrgGroupMemberRowOut(BaseModel):
    org_id: int
    org_name: str
    level: str
    #: 入组时刻（DateTime 列的 isoformat 字符串）
    joined_at: str


@router.get("/{group_id}/members", response_model=list[OrgGroupMemberRowOut])
def list_members(group_id: int, db: Session = Depends(get_db)):
    _get(db, group_id)
    rows = db.query(OrgGroupMember).filter(OrgGroupMember.group_id == group_id).all()
    org_names = {o.id: o for o in db.query(Organization).all()}
    return [
        {
            "org_id": m.org_id,
            "org_name": org_names[m.org_id].name if m.org_id in org_names else "",
            "level": org_names[m.org_id].level if m.org_id in org_names else "",
            "joined_at": m.joined_at.isoformat(),
        }
        for m in rows
    ]


class OrgGroupMemberAddOut(BaseModel):
    group_id: int
    org_id: int


@router.post(
    "/{group_id}/members", status_code=201, response_model=OrgGroupMemberAddOut,
    dependencies=[Depends(require_admin)],
)
def add_member(group_id: int, body: MemberIn, db: Session = Depends(get_db)):
    """把机构加入分组。同一机构可属于多个分组，故这里不排斥它已在别的分组里。"""
    _get(db, group_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    db.add(OrgGroupMember(group_id=group_id, org_id=body.org_id))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该机构已在本分组内") from None
    return {"group_id": group_id, "org_id": body.org_id}


class OrgGroupMemberRemoveOut(BaseModel):
    removed: bool


@router.delete(
    "/{group_id}/members/{org_id}", response_model=OrgGroupMemberRemoveOut,
    dependencies=[Depends(require_admin)],
)
def remove_member(group_id: int, org_id: int, db: Session = Depends(get_db)):
    member = (
        db.query(OrgGroupMember)
        .filter(OrgGroupMember.group_id == group_id, OrgGroupMember.org_id == org_id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="该机构不在本分组内")
    db.delete(member)
    db.commit()
    return {"removed": True}


@router.get("/of-org/{org_id}", response_model=list[OrgGroupOut])
def groups_of_org(org_id: int, db: Session = Depends(get_db)):
    """某机构归属的全部分组。一家机构可以既在某片区，又在某专科联盟。

    第九轮横向隔离**明确不设限**：分组归属是组织架构拓扑，转诊、调拨、
    统计口径都要引用，且不含任何经营或诊疗数据。"""
    if db.get(Organization, org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    group_ids = [
        m.group_id for m in db.query(OrgGroupMember).filter(OrgGroupMember.org_id == org_id).all()
    ]
    if not group_ids:
        return []
    return [
        _group_out(g)
        for g in db.query(OrgGroup).filter(OrgGroup.id.in_(group_ids)).order_by(OrgGroup.id).all()
    ]


class OrgGroupCoverageOrgOut(BaseModel):
    org_id: int
    org_name: str
    level: str


class OrgGroupCoverageOut(BaseModel):
    group_type: str
    group_type_name: str
    groups: int
    orgs_total: int
    orgs_grouped: int
    ungrouped: list[OrgGroupCoverageOrgOut]
    note: str


@router.get("/coverage", response_model=OrgGroupCoverageOut)
def coverage(group_type: str = "zone", db: Session = Depends(get_db)):
    """分组覆盖情况：哪些机构还没被任何该类型的分组收进去。

    分组统计有一个天然陷阱：**未入组机构的数据会从分组视图里凭空消失**。
    先把未入组清单摆出来，比事后追问"为什么各片区加起来对不上全县"要好。
    """
    groups = db.query(OrgGroup).filter(OrgGroup.group_type == group_type).all()
    group_ids = [g.id for g in groups]
    grouped: set[int] = set()
    if group_ids:
        grouped = {
            m.org_id
            for m in db.query(OrgGroupMember)
            .filter(OrgGroupMember.group_id.in_(group_ids))
            .all()
        }
    orgs = db.query(Organization).all()
    ungrouped = [{"org_id": o.id, "org_name": o.name, "level": o.level}
                 for o in orgs if o.id not in grouped]
    return {
        "group_type": group_type,
        "group_type_name": GROUP_TYPE_NAMES.get(group_type, group_type),
        "groups": len(groups),
        "orgs_total": len(orgs),
        "orgs_grouped": len(grouped),
        "ungrouped": ungrouped,
        "note": "未入组机构不会出现在任何分组视图里；按分组统计前请确认此清单为空，"
                "否则分组之和不等于全域总数",
    }


def _get(db: Session, group_id: int) -> OrgGroup:
    group = db.get(OrgGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="机构分组不存在")
    return group
