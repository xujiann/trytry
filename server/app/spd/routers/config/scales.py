"""全域慢专病 · 配置域：评估量表、宣教素材、服务包、标签。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""
from typing import Any
from secrets import token_urlsafe

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....database import get_db
from ....patchtypes import UNSET
from ....deps import paginate, require_roles, keyword_like
from ....numtypes import INT4_MAX, INT4_MIN, MONEY_MAX, MoneyFloat
from ....texttypes import NON_BLANK
from ...models import (
    SpdEduMaterial,
    SpdScale,
    SpdServicePackage,
    SpdTag,
)
from ...rules import scale_option_label_problem, scale_overlap_problem, scale_problem
from ...service import MEDIA_TYPE_NAMES, PACKAGE_ITEM_NAME_MAX, SCALE_ADVICE_MAX, package_items_ok, unknown_program
from ._base import CONFIG_ROLES, SvgResponse, _qr_svg, router


# ============================================================ 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class ScaleOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    program_code: str
    version: str
    status: str
    # 题目数组与评分规则：形状由题型与量表设计决定，宽类型如实反映
    items: list[dict[str, Any]]
    scoring: dict[str, Any]
    # 未发布时是空串（未生成令牌），不是 null
    qr_token: str
    owner_team_id: int | None


class EduMaterialOut(BaseModel):
    id: int
    code: str
    title: str
    program_code: str
    media_type: str
    media_type_name: str
    content: str
    media_url: str
    dept: str
    active: bool


class ServicePackageOut(BaseModel):
    """服务包。`price` 是 `Money`（`Numeric`）列——整数价读回来是 int，
    声明成 float 会把「200 元」变成「200.0 元」。"""

    id: int
    code: str
    name: str
    program_code: str
    price: int | float
    period_days: int
    items: list[dict[str, Any]]
    active: bool


class TagOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    color: str
    active: bool


class TagBriefOut(BaseModel):
    """列表里的标签**没有** `active`——列表本身已按 active 过滤，再回一个
    恒为 true 的字段没有意义。与新建的返回形状不同，故是两个模型而不是继承。"""

    id: int
    code: str
    name: str
    category: str
    category_name: str
    color: str


# ============================================================ 评估量表


class ScaleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    category: str = Field(default="risk", pattern="^(risk|stage|rehab|screen)$")
    program_code: str = Field(default="", max_length=32)
    version: str = Field(default="v1", max_length=16)
    items: list[dict] = Field(default_factory=list)
    scoring: dict = Field(default_factory=dict)
    owner_team_id: int | None = Field(default=None, ge=INT4_MIN, le=INT4_MAX)


def _scale_out(s: SpdScale) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "category": s.category,
        "program_code": s.program_code, "version": s.version, "status": s.status,
        "items": s.items or [], "scoring": s.scoring or {}, "qr_token": s.qr_token,
        "owner_team_id": s.owner_team_id,
    }


def _check_item_keys(items: list[dict]) -> None:
    """建量表与改量表同一句（P1-94：改量表原先不查）：题目 key 重复，评分时后一题会盖掉前一题。"""
    keys = [i.get("key") for i in items]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="量表题目 key 不得重复")


def _check_scale(items: list, scoring: dict) -> None:
    """建 / 改 / 发布量表同一句（P2-80）：会让作答 500、或题目计不进分的配置，写库前拦下；评分分段重叠的同样拦下（P2-887，
    压线的分落进哪一段取决于书写顺序）；同一题选项标签重复的同样拦下（P2-1275，评分时后写的分值盖掉先写的）。"""
    problem = (scale_problem(items, scoring) or scale_option_label_problem(items) or scale_overlap_problem(scoring)
               or _scale_advice_problem(scoring))
    if problem:
        raise HTTPException(status_code=422, detail=f"量表配置非法：{problem}")


def _scale_advice_problem(scoring: dict) -> str:
    """评分分段的「建议」装得进评估 / 筛查记录的建议列（P2-1048）：原先不查长度，超长的量表照建、照发布，评估、筛查登记、
    居民自查落到那一段在生产库上即 500。只在建 / 改 / 发布时拦；存量量表照常作答，写记录时按列宽截断。"""
    for rng in (scoring or {}).get("ranges", []) or []:
        advice = rng.get("advice", "") if isinstance(rng, dict) else ""
        if isinstance(advice, str) and len(advice) > SCALE_ADVICE_MAX:
            return f"评分分段的建议不超过 {SCALE_ADVICE_MAX} 字（这一段 {len(advice)} 字）"
    return ""


@router.post("/scales", response_model=ScaleOut, status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_scale(body: ScaleIn, db: Session = Depends(get_db)):
    _check_item_keys(body.items)
    _check_scale(body.items, body.scoring)
    program_problem = unknown_program(db, body.program_code)  # 病种编码先查在不在（P1-120）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    scale = SpdScale(**body.model_dump(), status="draft")
    db.add(scale)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该量表编码与版本已存在") from None
    return _scale_out(scale)


@router.get("/scales", response_model=list[ScaleOut])
def list_scales(
    response: Response,
    category: str | None = None,
    program_code: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdScale)
    if category:
        query = query.filter(SpdScale.category == category)
    if program_code:
        query = query.filter(SpdScale.program_code == program_code)
    if status:
        query = query.filter(SpdScale.status == status)
    return [_scale_out(s) for s in paginate(query.order_by(SpdScale.id), response, offset, limit)]


@router.get("/scales/{scale_id}", response_model=ScaleOut)
def get_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    return _scale_out(scale)


class ScalePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    items: list[dict] = Field(default=UNSET)
    scoring: dict = Field(default=UNSET)
    category: str = Field(default=UNSET, pattern="^(risk|stage|rehab|screen)$")
    owner_team_id: int | None = Field(default=None, ge=INT4_MIN, le=INT4_MAX)


@router.patch("/scales/{scale_id}", response_model=ScaleOut,
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_scale(scale_id: int, body: ScalePatch, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    changes = body.model_dump(exclude_unset=True)
    if scale.status == "published" and ("items" in changes or "scoring" in changes):
        raise HTTPException(status_code=409, detail="已发布量表不可改题目或评分，请新建版本")
    # 发布过的（令牌只在首次发布时生成、从不清空）停用之后照样不许改（P2-151）：原先「停用 → 改题 → 再发布」绕过上一句，
    # 同一版本号、同一张二维码背后换了题目与评分，按原题算分的历次评估与现在的量表对不上
    if scale.qr_token and ("items" in changes or "scoring" in changes):
        raise HTTPException(status_code=409, detail="该量表发布过（现已停用），不可改题目或评分，请新建版本")
    if "items" in changes:
        _check_item_keys(changes["items"])
    if "items" in changes or "scoring" in changes:   # 题目与评分合起来查（P2-80）
        _check_scale(changes.get("items", scale.items or []), changes.get("scoring", scale.scoring or {}))
    for key, value in changes.items():
        setattr(scale, key, value)
    db.commit()
    return _scale_out(scale)


@router.post("/scales/{scale_id}/publish", response_model=ScaleOut,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def publish_scale(scale_id: int, db: Session = Depends(get_db)):
    """发布并生成二维码令牌——量表要"经审核发布"后才允许被评估引用。"""
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if not scale.items:
        raise HTTPException(status_code=422, detail="量表没有题目，不能发布")
    _check_scale(scale.items, scale.scoring or {})   # 修前存下的草稿：改好再发布（P2-80）
    scale.status = "published"
    if not scale.qr_token:
        scale.qr_token = token_urlsafe(12)
    db.commit()
    return _scale_out(scale)


@router.get("/scales/{scale_id}/qr.svg", response_class=SvgResponse)
def scale_qr(scale_id: int, request: Request, db: Session = Depends(get_db)):
    """量表评估二维码（成员端 #8）：扫码直达居民端自查页并预选该量表。

    编码的是**页面地址**（`/m/#scale=<token>`）而不是 API 地址——扫码的人
    要看到的是问卷，不是一段 JSON。令牌失效（量表停用）时页面自然回落到
    量表列表，码不用重印。

    只给筛查类量表出码（P2-366）：居民端自查页只列筛查量表、答卷只记筛查——风险评估 / 分期 / 康复量表的码
    扫进去找不到那张问卷，原先悄悄预选了列表里第一个筛查问卷，居民以为答的是扫的那张，答卷记成另一个病种的筛查。
    居民扫码自评（`SpdAssessment.channel = self`）要不要做另行待裁定。
    """
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if scale.status != "published" or not scale.qr_token:
        raise HTTPException(status_code=409, detail="量表未发布，先发布生成令牌")
    if scale.category != "screen":
        raise HTTPException(status_code=422, detail="只有筛查类量表能出居民自查二维码：居民端只做筛查自查，"
                                                    "其他类别的量表请由医护人员在评估中录入")
    url = f"{str(request.base_url).rstrip('/')}/m/#scale={scale.qr_token}"
    return SvgResponse(content=_qr_svg(url))


@router.post("/scales/{scale_id}/disable", response_model=ScaleOut,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def disable_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    scale.status = "disabled"
    db.commit()
    return _scale_out(scale)


# ============================================================ 宣教素材


class EduIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    title: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
    program_code: str = Field(default="", max_length=32)
    media_type: str = Field(default="text", pattern="^(text|audio|video)$")
    content: str = Field(default="", max_length=8192)
    media_url: str = Field(default="", max_length=256)
    dept: str = Field(default="", max_length=64)


@router.post("/edu-materials", response_model=EduMaterialOut, status_code=201,
             dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def create_edu(body: EduIn, db: Session = Depends(get_db)):
    program_problem = unknown_program(db, body.program_code)  # 病种编码先查在不在（P1-120）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    material = SpdEduMaterial(**body.model_dump())
    db.add(material)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该宣教编码已存在") from None
    return _edu_out(material)


def _edu_out(m: SpdEduMaterial) -> dict:
    return {
        "id": m.id, "code": m.code, "title": m.title, "program_code": m.program_code,
        "media_type": m.media_type, "media_type_name": MEDIA_TYPE_NAMES.get(m.media_type, m.media_type),
        "content": m.content, "media_url": m.media_url,
        "dept": m.dept, "active": m.active,
    }


@router.get("/edu-materials", response_model=list[EduMaterialOut])
def list_edu(
    response: Response,
    program_code: str | None = None,
    media_type: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdEduMaterial).filter(SpdEduMaterial.active.is_(True))
    if program_code:
        query = query.filter(SpdEduMaterial.program_code == program_code)
    if media_type:
        query = query.filter(SpdEduMaterial.media_type == media_type)
    if keyword:
        query = query.filter(keyword_like(SpdEduMaterial.title, keyword))
    rows = paginate(query.order_by(SpdEduMaterial.id.desc()), response, offset, limit)
    return [_edu_out(m) for m in rows]


class EduPatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    title: str = Field(default=UNSET, min_length=1, max_length=128, pattern=NON_BLANK)
    content: str = Field(default=UNSET, max_length=8192)
    media_url: str = Field(default=UNSET, max_length=256)
    media_type: str = Field(default=UNSET, pattern="^(text|audio|video)$")
    dept: str = Field(default=UNSET, max_length=64)
    active: bool = Field(default=UNSET)
    program_code: str = Field(default=UNSET, max_length=32)


@router.patch("/edu-materials/{material_id}", response_model=EduMaterialOut,
              dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def update_edu(material_id: int, body: EduPatch, db: Session = Depends(get_db)):
    material = db.get(SpdEduMaterial, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="宣教素材不存在")
    # 病种编码先查在不在（P1-120）；与现值相同的不再查（P1-121）
    program_problem = unknown_program(db, body.model_dump(exclude_unset=True).get("program_code") or "",
                                      already=material.program_code)
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(material, key, value)
    db.commit()
    return _edu_out(material)


# ============================================================ 服务包


class PackageIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    program_code: str = Field(default="", max_length=32)
    price: MoneyFloat = Field(default=0, ge=0, le=MONEY_MAX)
    period_days: int = Field(default=365, ge=1, le=3650)
    items: list[dict] = Field(default_factory=list)


def _package_out(p: SpdServicePackage) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "program_code": p.program_code,
        "price": p.price, "period_days": p.period_days, "items": p.items or [],
        "active": p.active,
    }


def _check_package_items(items: list[dict]) -> None:
    """建 / 改服务包同一句（P2-82）：原先改服务包不查项目，次数写成文字存得进去、之后每次绑定都 500；
    建服务包时同样的文字次数在这一句里 `int()` 抛错，也是 500。报错文案沿用原文一字不改。

    项目编码不许重复（P2-631）：扣减按编码找项目，原先同一编码两条（BP 2 次、BP 3 次）照收——剩余次数按各条相加显示 5，
    扣减却只认第一条，扣满 2 次之后「剩余次数不足」，另外 3 次永远用不上。"""
    if not package_items_ok(items):
        raise HTTPException(status_code=422, detail="服务包项目须有编码且次数大于0")
    codes = [str(item["code"]) for item in items]
    # 项目名写进扣减流水（P2-1048）：原先不查长度，超长的服务包照建、照绑，扣减一次在生产库上即 500
    too_long = [str(item["code"]) for item in items if len(str(item.get("name", ""))) > PACKAGE_ITEM_NAME_MAX]
    if too_long:
        raise HTTPException(status_code=422,
                            detail=f"服务包项目名称不超过 {PACKAGE_ITEM_NAME_MAX} 字：{'、'.join(too_long)}")
    repeated = sorted({code for code in codes if codes.count(code) > 1})
    if repeated:
        raise HTTPException(status_code=422,
                            detail=f"服务包项目编码重复：{'、'.join(repeated)}（同一项目请合成一条、次数相加）")


@router.post("/service-packages", response_model=ServicePackageOut, status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_package(body: PackageIn, db: Session = Depends(get_db)):
    _check_package_items(body.items)
    program_problem = unknown_program(db, body.program_code)  # 病种编码先查在不在（P1-120）
    if program_problem:
        raise HTTPException(status_code=404, detail=program_problem)
    package = SpdServicePackage(**body.model_dump())
    db.add(package)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该服务包编码已存在") from None
    return _package_out(package)


@router.get("/service-packages", response_model=list[ServicePackageOut])
def list_packages(
    response: Response,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdServicePackage)
    if program_code:
        query = query.filter(SpdServicePackage.program_code == program_code)
    rows = paginate(query.order_by(SpdServicePackage.id), response, offset, limit)
    return [_package_out(p) for p in rows]


class PackagePatch(BaseModel):
    """改档与建档同一套约束（P1-94）：原先收裸 dict、照单全收。不传即不改；不可空的列显式传 null 是 422。"""

    name: str = Field(default=UNSET, min_length=1, max_length=64, pattern=NON_BLANK)
    price: MoneyFloat = Field(default=UNSET, ge=0, le=MONEY_MAX)
    period_days: int = Field(default=UNSET, ge=1, le=3650)
    items: list[dict] = Field(default=UNSET)
    active: bool = Field(default=UNSET)


@router.patch("/service-packages/{package_id}", response_model=ServicePackageOut,
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_package(package_id: int, body: PackagePatch, db: Session = Depends(get_db)):
    package = db.get(SpdServicePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    changes = body.model_dump(exclude_unset=True)
    if "items" in changes:   # 与建服务包同一句（P2-82）
        _check_package_items(changes["items"])
    for key, value in changes.items():
        setattr(package, key, value)
    db.commit()
    return _package_out(package)


# ============================================================ 标签


# 标签类别的中文名（P2-74 ②）：界面新建时给这三项，接口不强制（`TagIn.category` 只限长度），
# 表外的值原样回显，与其它码表同一口径。
TAG_CATEGORY_NAMES = {"patient": "患者标签", "risk": "风险标签", "service": "服务标签"}


class TagIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    category: str = Field(default="patient", max_length=32)
    color: str = Field(default="", max_length=16)


@router.post("/tags", response_model=TagOut, status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_tag(body: TagIn, db: Session = Depends(get_db)):
    tag = SpdTag(**body.model_dump())
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该标签编码已存在") from None
    return {"id": tag.id, "code": tag.code, "name": tag.name, "category": tag.category,
            "color": tag.color, "active": tag.active}


@router.get("/tags", response_model=list[TagBriefOut])
def list_tags(category: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdTag).filter(SpdTag.active.is_(True))
    if category:
        query = query.filter(SpdTag.category == category)
    return [
        {"id": t.id, "code": t.code, "name": t.name, "category": t.category,
         "category_name": TAG_CATEGORY_NAMES.get(t.category, t.category), "color": t.color}
        for t in query.order_by(SpdTag.id).limit(300).all()
    ]
