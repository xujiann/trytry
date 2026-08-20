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

#: 分级转诊的机构层级阶梯（ADR-0005：村→乡镇→区市县三级）：键为机构自身层级，
#: 值为其父机构**允许**的层级。逐级校验一次上收一层（`_assert_review_authority`
#: 按 `parent_id` 找直接上级），所以真正决定转诊对不对的是**父子层级是否相邻**，
#: 不是链路有几个节点——county→村室→村室 只有三层却已经错位：那张单子的
#: "卫生院审核"会由一家村卫生室完成，县级医院从未经手，闭环统计跟着失真。
#:
#: 只登记转诊链**从其出发向上走**的两级（village / township）。county 与 city
#: 是链路终点，其上怎么挂与转诊无关：市级协作医院挂在县院之下、县级公卫机构挂在
#: 县院之下，都是真实的医共体形态，不该因此判故障——收窄到这两条，既堵住错位，
#: 也不对不参与转诊的机构指手画脚。
_PARENT_LEVELS: dict[str, set[str]] = {
    "village": {"township"},
    "township": {"county", "city"},
}


class OrgTreeIssue(BaseModel):
    id: int
    name: str
    level: str
    org_type: str
    parent_id: int | None = None


class OrgTreeBrokenChain(BaseModel):
    id: int
    name: str
    level: str
    parent_id: int
    parent_level: str
    expected_parent_levels: list[str]
    chain: list[str]


class OrgTreeHealthOut(BaseModel):
    total: int
    roots: int
    max_depth: int
    orphans: list[OrgTreeIssue]
    broken_chains: list[OrgTreeBrokenChain]
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
    - `broken_chains`：转诊链上父子层级不相邻的机构（ADR-0005 三级阶梯，见
      `_PARENT_LEVELS`）。判据是**层级相邻**而非链路长度：city→county→township→village
      四层是合法的市级牵头架构，而 county→村室→村室 只有三层却已经错位——那张单子的
      "卫生院审核"会由一家村卫生室完成、县级医院从未经手，环节名与实际处理机构对不上，
      闭环统计跟着失真。只校验 village/township 两级的父机构（转诊链从其出发向上走），
      county/city 之上如何挂载与转诊无关，不误判；
    - `max_depth`：最深链路层数，纯信息项（供运维一眼看出树的形状），不参与判定；
    - `referral_ready`：`orphans` 与 `broken_chains` 都为空，才算满足分级转诊口径。

    （不查 `parent_id` 悬挂：`parent_id` 有外键约束、建机构时校验父机构存在、
    且父机构一经设定不可改，正常写入路径产生不了这类损坏，查了也永远为空。
    成环同理不可达，但链路回溯仍带环保护——万一有环也只是链路截断，不会转圈。）
    """
    orgs = db.query(Organization).order_by(Organization.id).all()
    by_id = {o.id: o for o in orgs}
    roots = 0
    orphans: list[dict] = []
    broken: list[dict] = []
    max_depth = 0
    for o in orgs:
        # 自底向上走到根，得出本机构的链路（根在前）与层数
        chain: list[str] = []
        seen: set[int] = set()
        node = o
        while node is not None and node.id not in seen:
            seen.add(node.id)
            chain.append(node.name)
            node = by_id.get(node.parent_id) if node.parent_id is not None else None
        chain.reverse()
        max_depth = max(max_depth, len(chain))

        if o.parent_id is None:
            if o.level in _ROOT_LEVELS:
                roots += 1          # 顶层无父 = 合法树根
            else:
                orphans.append(_issue(o))  # 非顶层无父 = 孤儿，会卡转诊审核
            continue

        parent = by_id.get(o.parent_id)
        allowed = _PARENT_LEVELS.get(o.level)
        if parent is None or allowed is None:
            # 父机构查不到（外键保证不会发生），或本机构层级不在转诊阶梯上
            # （county/city 是链路终点，其上如何挂载与转诊无关）——不误判
            continue
        if parent.level not in allowed:
            broken.append(
                {
                    "id": o.id, "name": o.name, "level": o.level,
                    "parent_id": parent.id, "parent_level": parent.level,
                    "expected_parent_levels": sorted(allowed),
                    "chain": chain,
                }
            )
    return {
        "total": len(orgs),
        "roots": roots,
        "max_depth": max_depth,
        "orphans": orphans,
        "broken_chains": broken,
        "referral_ready": not orphans and not broken,
    }


@router.get("", response_model=list[OrganizationOut], dependencies=[Depends(get_current_user)])
def list_organizations(level: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Organization)
    if level:
        query = query.filter(Organization.level == level)
    return query.order_by(Organization.id).all()
