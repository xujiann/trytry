"""成人健康体检记录（浙#5）：体检登记（含分项结果）、总检、异常清单与患者体检史。

B2 扩展：登记接口新增可选 `items` 分项列表（逐项测值/参考范围/异常标志），
汇总字段 `summary`/`abnormal_items` 原样保留（存量调用方不传分项照常可用）；
分项录完后由总检医师出总检结论（`/checkups/{id}/review`）。
既有列表/异常清单响应字节不变（特征化测试钉住）。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..datetypes import DateStr
from ..texttypes import NON_BLANK
from ..models import CheckupItem, Organization, Patient, PhysicalExam, User

router = APIRouter(prefix="/api/checkups", tags=["健康体检"], dependencies=[Depends(get_current_user)])


class CheckupItemIn(BaseModel):
    item_code: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    item_name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    result_value: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    unit: str = Field(default="", max_length=16)
    ref_range: str = Field(default="", max_length=64)
    abnormal: bool = False


class CheckupItemOut(CheckupItemIn):
    id: int
    checkup_id: int
    # 出参不带「不能只填空格」（P1-109）：修之前存进去的纯空白行要原样读出来，而不是让整个清单 500
    item_code: str = Field(min_length=1, max_length=64)
    item_name: str = Field(min_length=1, max_length=128)
    result_value: str = Field(min_length=1, max_length=64)

    model_config = {"from_attributes": True}


class CheckupBase(BaseModel):
    patient_id: int
    org_id: int
    package_name: str = Field(default="常规体检", max_length=128)
    exam_date: DateStr
    summary: str = Field(default="", max_length=1024)
    abnormal_items: str = Field(default="", max_length=512)


class CheckupCreate(CheckupBase):
    # B2：分项结果（可选）。不传 = 存量的纯汇总录入，行为不变。
    items: list[CheckupItemIn] = []


class CheckupOut(CheckupBase):
    """列表/登记响应契约：与分项扩展前的输出一一对应（不含 items，字节不变）。"""

    id: int
    has_abnormal: bool
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    exam_date: str

    model_config = {"from_attributes": True}


class CheckupListOut(CheckupOut):
    """清单行：比登记回执多一个「总检了没有」（P2-409）。登记回执的键集合有特征化用例钉着
    （`test_checkup_items_review`），只加在清单上；结论全文仍只在总检回执与打印件里。
    `abnormal_text` 见 `abnormal_text()`（P2-422）。"""

    reviewed: bool
    abnormal_text: str


class AbnormalCheckupOut(BaseModel):
    """异常清单行的响应契约。字段与原手拼 dict 一一对应，保持响应向后兼容；`abnormal_text` 是后加的（P2-422）。"""
    id: int
    patient_id: int
    exam_date: str
    abnormal_items: str
    abnormal_text: str


def abnormal_text(summary: str, abnormal_item_names: list[str]) -> str:
    """给人看的「异常项」：汇总的异常项串是选填的，没写时列出标了异常的分项（与打印件同一口径，P2-205）。

    异常口径本就是「汇总异常串非空 **或** 任一分项异常」（`create_checkup`）——只按分项标了异常的体检，原先在
    异常清单与体检清单上是一个空的红标签，看不出哪项异常（P2-422）。`abnormal_items` 仍原样回显录入的汇总串。
    """
    return summary or "、".join(abnormal_item_names)


def _abnormal_item_names(db: Session, exam_ids: list[int]) -> dict[int, list[str]]:
    """一页体检各自标了异常的分项名（按录入顺序），一条 SQL 取回。"""
    names: dict[int, list[str]] = {}
    if not exam_ids:
        return names
    rows = (
        db.query(CheckupItem.checkup_id, CheckupItem.item_name)
        .filter(CheckupItem.checkup_id.in_(exam_ids), CheckupItem.abnormal.is_(True))
        .order_by(CheckupItem.id)
        .all()
    )
    for checkup_id, item_name in rows:
        names.setdefault(checkup_id, []).append(item_name)
    return names


@router.post(
    "",
    response_model=CheckupOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # 体检报告录入
)
def create_checkup(body: CheckupCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    payload = body.model_dump(exclude={"items"})
    # 异常口径：汇总异常串非空 或 任一分项异常
    has_abnormal = bool(body.abnormal_items.strip()) or any(i.abnormal for i in body.items)
    exam = PhysicalExam(**payload, has_abnormal=has_abnormal)
    db.add(exam)
    db.flush()
    for item in body.items:
        db.add(CheckupItem(checkup_id=exam.id, **item.model_dump()))
    db.commit()
    db.refresh(exam)
    return exam


@router.get("", response_model=list[CheckupListOut])
def list_checkups(
    response: Response,
    patient_id: int | None = None,
    has_abnormal: bool | None = None,
    reviewed: bool | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(PhysicalExam)
    query = scope_patient_list(db, user, query, PhysicalExam, patient_id, "checkup")
    if has_abnormal is not None:
        query = query.filter(PhysicalExam.has_abnormal.is_(has_abnormal))
    # 按「总检了没有」筛（P2-409）：页面要把没总检的单独取一遍、排在最前——清单只回最新 200 条，挤出窗口的
    # 那次体检原先就再没有一行给「总检」。总检接口要求结论非空，「两列均空 = 尚未总检」（模型注释），按结论判
    if reviewed is not None:
        query = query.filter(
            (PhysicalExam.final_conclusion != "") if reviewed else (PhysicalExam.final_conclusion == "")
        )
    exams = paginate(query.order_by(PhysicalExam.id.desc()), response, offset, limit)
    names = _abnormal_item_names(db, [e.id for e in exams])
    return [
        {**CheckupOut.model_validate(e).model_dump(), "reviewed": bool(e.final_conclusion),
         "abnormal_text": abnormal_text(e.abnormal_items, names.get(e.id, []))}
        for e in exams
    ]


@router.get("/abnormal", response_model=list[AbnormalCheckupOut])
def abnormal_checkups(db: Session = Depends(get_db)):
    """异常项清单：供慢病筛查建档与随访干预衔接（医防协同）。"""
    exams = (
        db.query(PhysicalExam)
        .filter(PhysicalExam.has_abnormal.is_(True))
        .order_by(PhysicalExam.id.desc())
        .limit(200)
        .all()
    )
    names = _abnormal_item_names(db, [e.id for e in exams])
    return [
        {
            "id": e.id,
            "patient_id": e.patient_id,
            "exam_date": e.exam_date,
            "abnormal_items": e.abnormal_items,
            "abnormal_text": abnormal_text(e.abnormal_items, names.get(e.id, [])),
        }
        for e in exams
    ]


# ---------- B2：分项查询与总检 ----------


def _get_checkup(db: Session, checkup_id: int) -> PhysicalExam:
    exam = db.get(PhysicalExam, checkup_id)
    if exam is None:
        raise HTTPException(status_code=404, detail="体检记录不存在")
    return exam


@router.get("/{checkup_id}/items", response_model=list[CheckupItemOut])
def list_checkup_items(checkup_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """一次体检的分项结果（患者可见性校验 + 留痕，与体检史同一口径）。"""
    exam = _get_checkup(db, checkup_id)
    assert_patient_visible(db, user, exam.patient_id, resource="checkup:items")
    return (
        db.query(CheckupItem)
        .filter(CheckupItem.checkup_id == exam.id)
        .order_by(CheckupItem.id)
        .all()
    )


class CheckupReviewIn(BaseModel):
    final_conclusion: str = Field(min_length=1, max_length=1024, pattern=NON_BLANK)
    # 空串 = 以当前登录医师署名
    final_doctor: str = Field(default="", max_length=64)


class CheckupReviewOut(CheckupOut):
    final_conclusion: str
    final_doctor: str


@router.post(
    "/{checkup_id}/review",
    response_model=CheckupReviewOut,
    dependencies=[Depends(require_roles("doctor"))],  # 总检=医师职责（公卫岗只录入）
)
def review_checkup(
    checkup_id: int,
    body: CheckupReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """总检：分项/汇总录完后由总检医师出结论。重复总检按覆盖（复核改结论）。"""
    exam = _get_checkup(db, checkup_id)
    assert_obj_org_writable(db, user, exam)
    exam.final_conclusion = body.final_conclusion
    exam.final_doctor = body.final_doctor or (user.full_name or user.username)
    db.commit()
    db.refresh(exam)
    return exam
