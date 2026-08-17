"""S14 EMPI 跨机构主索引合并（merge/link）。

建档期只按身份证号去重（patients.py）。运行期发现的重复主档（录错身份证、
无证儿童临时档、多头建档）需要管理员把重复档 link 到主档，并可 revert。

`Patient` 无 org_id——它是全县主索引，"本机构的患者"这种东西不存在（见
visibility.py）。因此主索引合并是**平台级身份动作**，不是机构范围内的操作，
写操作一律限管理员（`require_roles("admin")`），不走机构可见性那套。

## revert 设计：design (b) —— revert 即物理删除该 link 行

唯一约束 `uq_merge_duplicate` 钉在 `duplicate_patient_id` 上、与 status 无关，
故保留行改 status 会让该重复档永远无法重新合并。选 (b)：revert 直接删除该行，
把 `duplicate_patient_id` 让出来，从而"误并→撤销→重新并对"能走通；并发兜底
仍由 DB 唯一约束 + `insert_or_conflict` 保证。代价是 revert 后该 link_id 不再
存在，二次 revert 返回 404（而非 409 "已撤销"）——本实现下"存在一行"恒等价于
"一条生效合并"，不存在可被观测到的 reverted 中间态。详见 models/enhance_empi.py。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import Patient, PatientMergeLink, User
from ..visibility import assert_patient_visible

router = APIRouter(
    prefix="/api/empi",
    tags=["EMPI主索引合并"],
    dependencies=[Depends(get_current_user)],
)


# ---------------------------------------------------------------- schemas
class MergeIn(BaseModel):
    primary_patient_id: int
    duplicate_patient_id: int
    reason: str = Field(default="", max_length=256)


def _link_out(link: PatientMergeLink) -> dict:
    return {
        "id": link.id,
        "primary_patient_id": link.primary_patient_id,
        "duplicate_patient_id": link.duplicate_patient_id,
        "reason": link.reason,
        "status": link.status,
        "created_by": link.created_by,
        "created_at": link.created_at,
    }


# ---------------------------------------------------------------- endpoints
@router.post(
    "/merge",
    status_code=201,
    dependencies=[Depends(require_roles("admin"))],
)
def merge_patients(
    body: MergeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """把重复档合并（link）到主档。管理员限定。

    校验：两档都存在(404)；主档≠重复档(422)；主档本身不是别处的重复档(422)；
    重复档尚未被合并——并发下由唯一约束兜底(409)。
    """
    if db.get(Patient, body.primary_patient_id) is None:
        raise HTTPException(status_code=404, detail="主档患者不存在")
    if db.get(Patient, body.duplicate_patient_id) is None:
        raise HTTPException(status_code=404, detail="重复档患者不存在")
    if body.primary_patient_id == body.duplicate_patient_id:
        raise HTTPException(status_code=422, detail="主档与重复档不能是同一条")

    # 主档本身已被合并为别人的重复档：再把别的档并到它上面会形成"指向重复档"的
    # 错链（resolve 只跟一跳，链一深就解不到规范档）。直接挡在入口。
    if (
        db.query(PatientMergeLink.id)
        .filter(PatientMergeLink.duplicate_patient_id == body.primary_patient_id)
        .first()
        is not None
    ):
        raise HTTPException(status_code=422, detail="主档本身已被合并")

    # 反向也要挡：拟并入的"重复档"若已是别处合并的**主档**（其下挂着重复档），
    # 再把它并到 C 会形成 A→B→C 的两跳链，而 resolve 只跟一跳，resolve(A) 会停在 B。
    # 先撤销其下合并、或直接以 C 为主档重并，才不留断链。
    if (
        db.query(PatientMergeLink.id)
        .filter(PatientMergeLink.primary_patient_id == body.duplicate_patient_id)
        .first()
        is not None
    ):
        raise HTTPException(status_code=422, detail="该档已是其他合并的主档，需先撤销其下合并")

    link = PatientMergeLink(
        primary_patient_id=body.primary_patient_id,
        duplicate_patient_id=body.duplicate_patient_id,
        reason=body.reason,
        status="merged",
        created_by=user.id,
    )
    # 唯一约束在 duplicate_patient_id 上：该重复档已被合并则回滚给可读 409。
    insert_or_conflict(db, link, detail="该重复档已被合并，请先撤销后再操作")
    return _link_out(link)


@router.post(
    "/links/{link_id}/revert",
    dependencies=[Depends(require_roles("admin"))],
)
def revert_merge(
    link_id: int,
    db: Session = Depends(get_db),
):
    """撤销一条合并（design (b)：删除该 link 行，释放 duplicate 以便重新合并）。

    404 表示该合并不存在或已被撤销（撤销即删除，无可观测的 reverted 中间态）。
    """
    link = db.get(PatientMergeLink, link_id)
    if link is None:
        raise HTTPException(status_code=404, detail="合并记录不存在或已撤销")
    db.delete(link)
    db.commit()
    return {"reverted": True}


@router.get(
    "/links",
    dependencies=[Depends(require_roles("admin"))],
)
def list_links(
    patient_id: int | None = None,
    db: Session = Depends(get_db),
):
    """列出合并关系。给 patient_id 时列出该患者作为主档或重复档的所有合并。"""
    query = db.query(PatientMergeLink)
    if patient_id is not None:
        query = query.filter(
            (PatientMergeLink.primary_patient_id == patient_id)
            | (PatientMergeLink.duplicate_patient_id == patient_id)
        )
    rows = query.order_by(PatientMergeLink.id.desc()).all()
    return [_link_out(r) for r in rows]


@router.get("/resolve/{patient_id}")
def resolve_patient(
    patient_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """解析到规范主档：若 patient_id 是生效的重复档，返回其主档（跟一跳）；否则返回自身。

    返回姓名/电子健康卡号属患者标识，须先过患者可见性——否则任一登录账号可用
    `resolve` 枚举全县主索引。全域角色恒可见；非全域仅限有业务关系的患者。
    """
    assert_patient_visible(db, user, patient_id)
    link = (
        db.query(PatientMergeLink)
        .filter(PatientMergeLink.duplicate_patient_id == patient_id)
        .first()
    )
    canonical_id = link.primary_patient_id if link is not None else patient_id
    canonical = db.get(Patient, canonical_id)
    if canonical is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    return {
        "input_id": patient_id,
        "canonical_id": canonical_id,
        "canonical": {
            "id": canonical.id,
            "ehc_no": canonical.ehc_no,
            "name": canonical.name,
        },
    }
