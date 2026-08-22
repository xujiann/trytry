"""全域慢专病 · 配置域：重点慢专病中心与机构树。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""
from typing import Any


from fastapi import Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....database import get_db
from ....deps import require_roles
from ...platform import Organization
from ...models import (
    SpdCenter,
    SpdProgram,
    SpdTeam,
)
from ._base import CONFIG_ROLES, router


# ============================================================ 重点慢专病中心


class CenterIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(min_length=1, max_length=32)
    lead_org_id: int | None = None
    lead_dept: str = Field(default="", max_length=64)
    leader_user_id: int | None = None
    org_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)


def _center_out(c: SpdCenter) -> dict:
    return {
        "id": c.id, "code": c.code, "name": c.name, "program_code": c.program_code,
        "lead_org_id": c.lead_org_id, "lead_dept": c.lead_dept,
        "leader_user_id": c.leader_user_id, "org_ids": c.org_ids or [],
        "team_ids": c.team_ids or [], "version": c.version, "status": c.status,
    }


@router.post("/centers", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_center(body: CenterIn, db: Session = Depends(get_db)):
    if db.query(SpdProgram).filter(SpdProgram.code == body.program_code).first() is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    center = SpdCenter(**body.model_dump())
    db.add(center)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该中心编码已存在") from None
    return _center_out(center)


@router.get("/centers")
def list_centers(program_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdCenter)
    if program_code:
        query = query.filter(SpdCenter.program_code == program_code)
    return [_center_out(c) for c in query.order_by(SpdCenter.id).limit(200).all()]


@router.patch("/centers/{center_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_center(center_id: int, body: dict, db: Session = Depends(get_db)):
    center = db.get(SpdCenter, center_id)
    if center is None:
        raise HTTPException(status_code=404, detail="专病中心不存在")
    for key in ("name", "lead_org_id", "lead_dept", "leader_user_id", "org_ids", "team_ids",
                "status", "version"):
        if key in body:
            setattr(center, key, body[key])
    db.commit()
    return _center_out(center)


# ============================================================ 机构树（三级）


@router.get("/org-tree")
def org_tree(db: Session = Depends(get_db)):
    """县—乡—村三级机构树，附各机构的慢专病团队数与在管人数。

    平台管理端 #5 要求"统一组织层级用于患者归属、任务派发、逐级转诊、
    团队授权和考核"——所以这棵树不只是机构名称，还要带上这几项的实际数量，
    否则配置的人无法判断改动会影响到谁。
    """
    # 拆包后这里要多一级：`config/` 是子包，`..models` 会解析成
    # app.spd.routers.models（不存在）。此前没有任何用例调过这个端点，
    # 于是它在主干上一直是 500——覆盖率缺口藏起来的正是这类。
    from ...models import SpdEnrollment

    orgs = db.query(Organization).order_by(Organization.id).all()
    team_counts: dict[int, int] = {}
    for (org_id,) in db.query(SpdTeam.org_id).filter(SpdTeam.active.is_(True)).all():
        team_counts[org_id] = team_counts.get(org_id, 0) + 1
    enroll_counts: dict[int, int] = {}
    for (org_id,) in (
        db.query(SpdEnrollment.org_id).filter(SpdEnrollment.status == "active").all()
    ):
        enroll_counts[org_id] = enroll_counts.get(org_id, 0) + 1

    nodes: dict[int, dict[str, Any]] = {
        o.id: {
            "id": o.id, "name": o.name, "org_type": o.org_type, "level": o.level,
            "parent_id": o.parent_id, "team_count": team_counts.get(o.id, 0),
            "enrolled": enroll_counts.get(o.id, 0), "children": [],
        }
        for o in orgs
    }
    roots: list[dict[str, Any]] = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots
