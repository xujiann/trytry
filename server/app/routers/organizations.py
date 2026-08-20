from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import Organization
from ..schemas import OrganizationCreate, OrganizationOut

router = APIRouter(prefix="/api/organizations", tags=["机构管理"])

#: 顶层机构层级——parent_id 为空是正常的（机构树的根）。其余层级缺 parent_id 即"孤儿"。
_ROOT_LEVELS = {"county", "city"}


class OrgTreeIssue(BaseModel):
    id: int
    name: str
    level: str
    org_type: str
    parent_id: int | None = None


class OrgTreeHealthOut(BaseModel):
    total: int
    roots: int
    orphans: list[OrgTreeIssue]
    referral_ready: bool


def _issue(o: Organization) -> dict:
    return {"id": o.id, "name": o.name, "level": o.level,
            "org_type": o.org_type, "parent_id": o.parent_id}


@router.post("", response_model=OrganizationOut, status_code=201, dependencies=[Depends(require_admin)])
def create_organization(body: OrganizationCreate, db: Session = Depends(get_db)):
    if db.query(Organization).filter(Organization.name == body.name).first():
        raise HTTPException(status_code=409, detail="机构已存在")
    if body.parent_id is not None and db.get(Organization, body.parent_id) is None:
        raise HTTPException(status_code=404, detail="上级机构不存在")
    org = insert_or_conflict(db, Organization(**body.model_dump()), "机构已存在")
    return org


@router.get("/tree-health", response_model=OrgTreeHealthOut, dependencies=[Depends(require_admin)])
def org_tree_health(db: Session = Depends(get_db)):
    """机构树体检（运维用）：找出会卡住转诊分级审核的机构树缺陷。

    ADR-0004 起，转诊分级审核按机构树 `parent_id` 逐级上收——非顶层机构若缺
    `parent_id`（orphans），其发起/经手的转诊单只有全域角色能推进，非全域账号一律
    403。这里把这类机构一次列清，供运维在越权校验"咬人"前先把机构树建好。

    - `roots`：合法的树根（顶层 county/city 且无 `parent_id`）；
    - `orphans`：非顶层（非 county/city）却缺 `parent_id` 的机构——正是会被 403 的那批；
    - `referral_ready`：`orphans` 为空即机构树满足分级转诊校验。

    （不查 `parent_id` 悬挂/成环：`parent_id` 有外键约束、建机构时校验父机构存在、
    且父机构一经设定不可改，正常写入路径产生不了这类损坏，查了也永远为空。）
    """
    orgs = db.query(Organization).order_by(Organization.id).all()
    roots = 0
    orphans: list[dict] = []
    for o in orgs:
        if o.parent_id is not None:
            continue
        if o.level in _ROOT_LEVELS:
            roots += 1          # 顶层无父 = 合法树根
        else:
            orphans.append(_issue(o))  # 非顶层无父 = 孤儿，会卡转诊审核
    return {
        "total": len(orgs),
        "roots": roots,
        "orphans": orphans,
        "referral_ready": not orphans,
    }


@router.get("", response_model=list[OrganizationOut], dependencies=[Depends(get_current_user)])
def list_organizations(level: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Organization)
    if level:
        query = query.filter(Organization.level == level)
    return query.order_by(Organization.id).all()
