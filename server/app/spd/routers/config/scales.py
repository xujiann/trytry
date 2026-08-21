"""全域慢专病 · 配置域：评估量表、宣教素材、服务包、标签。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""

from secrets import token_urlsafe

from fastapi import Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....database import get_db
from ....deps import paginate, require_roles
from ...models import (
    SpdEduMaterial,
    SpdScale,
    SpdServicePackage,
    SpdTag,
)
from ._base import CONFIG_ROLES, _qr_svg, router


# ============================================================ 评估量表


class ScaleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="risk", pattern="^(risk|stage|rehab|screen)$")
    program_code: str = Field(default="", max_length=32)
    version: str = Field(default="v1", max_length=16)
    items: list[dict] = Field(default_factory=list)
    scoring: dict = Field(default_factory=dict)
    owner_team_id: int | None = None


def _scale_out(s: SpdScale) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "category": s.category,
        "program_code": s.program_code, "version": s.version, "status": s.status,
        "items": s.items or [], "scoring": s.scoring or {}, "qr_token": s.qr_token,
        "owner_team_id": s.owner_team_id,
    }


@router.post("/scales", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_scale(body: ScaleIn, db: Session = Depends(get_db)):
    keys = [i.get("key") for i in body.items]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="量表题目 key 不得重复")
    scale = SpdScale(**body.model_dump(), status="draft")
    db.add(scale)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该量表编码与版本已存在") from None
    return _scale_out(scale)


@router.get("/scales")
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


@router.get("/scales/{scale_id}")
def get_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    return _scale_out(scale)


@router.patch("/scales/{scale_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_scale(scale_id: int, body: dict, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if scale.status == "published" and ("items" in body or "scoring" in body):
        raise HTTPException(status_code=409, detail="已发布量表不可改题目或评分，请新建版本")
    for key in ("name", "items", "scoring", "category", "owner_team_id"):
        if key in body:
            setattr(scale, key, body[key])
    db.commit()
    return _scale_out(scale)


@router.post("/scales/{scale_id}/publish", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def publish_scale(scale_id: int, db: Session = Depends(get_db)):
    """发布并生成二维码令牌——量表要"经审核发布"后才允许被评估引用。"""
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if not scale.items:
        raise HTTPException(status_code=422, detail="量表没有题目，不能发布")
    scale.status = "published"
    if not scale.qr_token:
        scale.qr_token = token_urlsafe(12)
    db.commit()
    return _scale_out(scale)


@router.get("/scales/{scale_id}/qr.svg")
def scale_qr(scale_id: int, request: Request, db: Session = Depends(get_db)):
    """量表评估二维码（成员端 #8）：扫码直达居民端自查页并预选该量表。

    编码的是**页面地址**（`/m/#scale=<token>`）而不是 API 地址——扫码的人
    要看到的是问卷，不是一段 JSON。令牌失效（量表停用）时页面自然回落到
    量表列表，码不用重印。
    """
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if scale.status != "published" or not scale.qr_token:
        raise HTTPException(status_code=409, detail="量表未发布，先发布生成令牌")
    url = f"{str(request.base_url).rstrip('/')}/m/#scale={scale.qr_token}"
    return Response(content=_qr_svg(url), media_type="image/svg+xml")


@router.post("/scales/{scale_id}/disable", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def disable_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    scale.status = "disabled"
    db.commit()
    return _scale_out(scale)


# ============================================================ 宣教素材


class EduIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=128)
    program_code: str = Field(default="", max_length=32)
    media_type: str = Field(default="text", pattern="^(text|audio|video)$")
    content: str = Field(default="", max_length=8192)
    media_url: str = Field(default="", max_length=256)
    dept: str = Field(default="", max_length=64)


@router.post("/edu-materials", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def create_edu(body: EduIn, db: Session = Depends(get_db)):
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
        "media_type": m.media_type, "content": m.content, "media_url": m.media_url,
        "dept": m.dept, "active": m.active,
    }


@router.get("/edu-materials")
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
        query = query.filter(SpdEduMaterial.title.contains(keyword))
    rows = paginate(query.order_by(SpdEduMaterial.id.desc()), response, offset, limit)
    return [_edu_out(m) for m in rows]


@router.patch("/edu-materials/{material_id}",
              dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def update_edu(material_id: int, body: dict, db: Session = Depends(get_db)):
    material = db.get(SpdEduMaterial, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="宣教素材不存在")
    for key in ("title", "content", "media_url", "media_type", "dept", "active", "program_code"):
        if key in body:
            setattr(material, key, body[key])
    db.commit()
    return _edu_out(material)


# ============================================================ 服务包


class PackageIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(default="", max_length=32)
    price: float = Field(default=0, ge=0)
    period_days: int = Field(default=365, ge=1, le=3650)
    items: list[dict] = Field(default_factory=list)


def _package_out(p: SpdServicePackage) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "program_code": p.program_code,
        "price": p.price, "period_days": p.period_days, "items": p.items or [],
        "active": p.active,
    }


@router.post("/service-packages", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_package(body: PackageIn, db: Session = Depends(get_db)):
    for item in body.items:
        if not item.get("code") or int(item.get("times", 0)) <= 0:
            raise HTTPException(status_code=422, detail="服务包项目须有编码且次数大于0")
    package = SpdServicePackage(**body.model_dump())
    db.add(package)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该服务包编码已存在") from None
    return _package_out(package)


@router.get("/service-packages")
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


@router.patch("/service-packages/{package_id}",
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_package(package_id: int, body: dict, db: Session = Depends(get_db)):
    package = db.get(SpdServicePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    for key in ("name", "price", "period_days", "items", "active"):
        if key in body:
            setattr(package, key, body[key])
    db.commit()
    return _package_out(package)


# ============================================================ 标签


class TagIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="patient", max_length=32)
    color: str = Field(default="", max_length=16)


@router.post("/tags", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
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


@router.get("/tags")
def list_tags(category: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdTag).filter(SpdTag.active.is_(True))
    if category:
        query = query.filter(SpdTag.category == category)
    return [
        {"id": t.id, "code": t.code, "name": t.name, "category": t.category, "color": t.color}
        for t in query.order_by(SpdTag.id).limit(300).all()
    ]
