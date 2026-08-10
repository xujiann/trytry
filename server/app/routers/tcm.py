"""中医药服务：⑬智能辅诊（体质辨识+辨证推荐）、⑭共享中药房、㉑适宜技术库。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles
from ..models import Organization, Patient, TcmDispenseOrder, TcmTechnique

router = APIRouter(prefix="/api/tcm", tags=["中医药服务"], dependencies=[Depends(get_current_user)])

# ---------- ⑬ 中医智能辅诊（规则知识库） ----------

CONSTITUTIONS = {
    "qi_deficiency": {"name": "气虚质", "advice": "补中益气，忌过劳；食疗：山药、大枣、黄芪炖鸡", "formula": "补中益气汤"},
    "yang_deficiency": {"name": "阳虚质", "advice": "温阳散寒，注意保暖；食疗：羊肉、生姜、桂圆", "formula": "金匮肾气丸"},
    "yin_deficiency": {"name": "阴虚质", "advice": "滋阴降火，忌辛辣熬夜；食疗：银耳、百合、枸杞", "formula": "六味地黄丸"},
    "phlegm_damp": {"name": "痰湿质", "advice": "健脾化痰，控制体重；食疗：薏米、赤小豆、冬瓜", "formula": "二陈汤"},
    "balanced": {"name": "平和质", "advice": "起居有常，饮食有节，坚持运动", "formula": ""},
}

SYNDROME_KB = [
    {"symptoms": {"乏力", "气短", "自汗"}, "syndrome": "气虚证", "formula": "四君子汤", "techniques": ["艾灸足三里", "穴位贴敷"]},
    {"symptoms": {"畏寒", "肢冷", "腰膝酸软"}, "syndrome": "阳虚证", "formula": "金匮肾气丸", "techniques": ["督脉灸", "隔姜灸"]},
    {"symptoms": {"口干", "盗汗", "五心烦热"}, "syndrome": "阴虚证", "formula": "六味地黄丸", "techniques": ["耳穴压豆"]},
    {"symptoms": {"头晕", "胸闷", "肢体困重"}, "syndrome": "痰湿证", "formula": "半夏白术天麻汤", "techniques": ["拔罐", "刮痧"]},
]


class ConstitutionBody(BaseModel):
    # 各体质维度得分 0-100，取最高者；均低于40判平和质
    scores: dict[str, int]


@router.post("/constitution")
def identify_constitution(body: ConstitutionBody):
    valid = {k: v for k, v in body.scores.items() if k in CONSTITUTIONS and k != "balanced"}
    if not valid:
        raise HTTPException(status_code=422, detail="缺少有效的体质维度得分")
    top_key, top_score = max(valid.items(), key=lambda kv: kv[1])
    key = top_key if top_score >= 40 else "balanced"
    return {"constitution": CONSTITUTIONS[key]["name"], "score": top_score, **CONSTITUTIONS[key]}


class DiagnoseBody(BaseModel):
    symptoms: list[str] = Field(min_length=1)


@router.post("/assist-diagnosis")
def assist_diagnosis(body: DiagnoseBody):
    """智能辨证：按症状匹配度推荐证型、方剂与适宜技术。"""
    given = set(body.symptoms)
    ranked = sorted(
        (
            {
                "syndrome": kb["syndrome"],
                "matched": sorted(kb["symptoms"] & given),
                "match_count": len(kb["symptoms"] & given),
                "formula": kb["formula"],
                "techniques": kb["techniques"],
            }
            for kb in SYNDROME_KB
        ),
        key=lambda r: r["match_count"],
        reverse=True,
    )
    hits = [r for r in ranked if r["match_count"] > 0]
    return {"recommendations": hits[:3], "note": "辅助建议仅供参考，须由中医师最终辨证"}


# ---------- ⑭ 共享中药房 ----------

_DISPENSE_FLOW = {"ordered": "dispensed", "dispensed": "decocted", "decocted": "delivering", "delivering": "delivered"}
_NO_DECOCT_FLOW = {"ordered": "dispensed", "dispensed": "delivering", "delivering": "delivered"}


class DispenseCreate(BaseModel):
    patient_id: int
    from_org_id: int
    herbs: str = Field(min_length=1)
    doses: int = Field(default=1, ge=1)
    decoct: bool = True


class DispenseOut(DispenseCreate):
    id: int
    status: str

    model_config = {"from_attributes": True}


@router.post(
    "/dispense-orders",
    response_model=DispenseOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor"))],  # H2: 代煎建单属处方性质，限医师
)
def create_order(body: DispenseCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.from_org_id) is None:
        raise HTTPException(status_code=404, detail="下单机构不存在")
    order = TcmDispenseOrder(**body.model_dump())
    db.add(order)
    db.commit()
    db.refresh(order)
    return order


@router.get("/dispense-orders", response_model=list[DispenseOut])
def list_orders(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(TcmDispenseOrder)
    if status:
        query = query.filter(TcmDispenseOrder.status == status)
    return query.order_by(TcmDispenseOrder.id.desc()).limit(200).all()


@router.post(
    "/dispense-orders/{order_id}/advance",
    response_model=DispenseOut,
    dependencies=[Depends(require_roles("operator", "pharmacist"))],  # H2: 煎药/配送流转
)
def advance_order(order_id: int, db: Session = Depends(get_db)):
    order = db.get(TcmDispenseOrder, order_id)
    if order is None:
        raise HTTPException(status_code=404, detail="中药订单不存在")
    flow = _DISPENSE_FLOW if order.decoct else _NO_DECOCT_FLOW
    next_status = flow.get(order.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {order.status} 已是终态")
    order.status = next_status
    db.commit()
    db.refresh(order)
    return order


# ---------- ㉑ 中医药适宜技术库 ----------


class TechniqueCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    category: str = ""
    indication: str = ""
    description: str = ""


class TechniqueOut(TechniqueCreate):
    id: int

    model_config = {"from_attributes": True}


@router.post("/techniques", response_model=TechniqueOut, status_code=201, dependencies=[Depends(require_admin)])
def create_technique(body: TechniqueCreate, db: Session = Depends(get_db)):
    if db.query(TcmTechnique).filter(TcmTechnique.name == body.name).first():
        raise HTTPException(status_code=409, detail="该技术已入库")
    technique = TcmTechnique(**body.model_dump())
    db.add(technique)
    db.commit()
    db.refresh(technique)
    return technique


@router.get("/techniques", response_model=list[TechniqueOut])
def list_techniques(keyword: str = "", db: Session = Depends(get_db)):
    query = db.query(TcmTechnique)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter((TcmTechnique.name.like(like)) | (TcmTechnique.indication.like(like)))
    return query.order_by(TcmTechnique.id).limit(200).all()
