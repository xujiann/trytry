"""中医药服务：⑬智能辅诊（体质辨识+辨证推荐）、⑭共享中药房、㉑适宜技术库。"""
from typing import Any
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from ..concurrency import insert_or_conflict
from ..database import get_db
from ..deps import get_current_user, require_admin, require_roles, resolve_business_date
from ..models import (
    Organization,
    Patient,
    TcmDispenseOrder,
    TcmFormula,
    TcmPreparationBatch,
    TcmTechnique,
    User,
)
from datetime import date, timedelta
from ..datetypes import DateStr
from ..visibility import assert_obj_org_writable, assert_org_writable

router = APIRouter(prefix="/api/tcm", tags=["中医药服务"], dependencies=[Depends(get_current_user)])

# ---------- ⑬ 中医智能辅诊（规则知识库） ----------

# 王琦九体质全集（平和质 + 8 种偏颇体质）
CONSTITUTIONS = {
    "qi_deficiency": {"name": "气虚质", "advice": "补中益气，忌过劳；食疗：山药、大枣、黄芪炖鸡", "formula": "补中益气汤"},
    "yang_deficiency": {"name": "阳虚质", "advice": "温阳散寒，注意保暖；食疗：羊肉、生姜、桂圆", "formula": "金匮肾气丸"},
    "yin_deficiency": {"name": "阴虚质", "advice": "滋阴降火，忌辛辣熬夜；食疗：银耳、百合、枸杞", "formula": "六味地黄丸"},
    "phlegm_damp": {"name": "痰湿质", "advice": "健脾化痰，控制体重；食疗：薏米、赤小豆、冬瓜", "formula": "二陈汤"},
    "damp_heat": {"name": "湿热质", "advice": "清热利湿，忌烟酒辛辣肥甘；食疗：绿豆、苦瓜、马齿苋", "formula": "甘露消毒丹"},
    "blood_stasis": {"name": "血瘀质", "advice": "活血化瘀，规律运动防久坐；食疗：山楂、黑木耳、玫瑰花茶", "formula": "血府逐瘀汤"},
    "qi_stagnation": {"name": "气郁质", "advice": "疏肝解郁，调畅情志多社交；食疗：陈皮、佛手、合欢花茶", "formula": "逍遥散"},
    "special": {"name": "特禀质", "advice": "益气固表抗过敏，规避过敏原（花粉/尘螨/异种蛋白）；食疗：固表粥", "formula": "玉屏风散"},
    "balanced": {"name": "平和质", "advice": "起居有常，饮食有节，坚持运动", "formula": ""},
}

SYNDROME_KB: list[dict[str, Any]] = [
    {"symptoms": {"乏力", "气短", "自汗"}, "syndrome": "气虚证", "formula": "四君子汤", "techniques": ["艾灸足三里", "穴位贴敷"]},
    {"symptoms": {"畏寒", "肢冷", "腰膝酸软"}, "syndrome": "阳虚证", "formula": "金匮肾气丸", "techniques": ["督脉灸", "隔姜灸"]},
    {"symptoms": {"口干", "盗汗", "五心烦热"}, "syndrome": "阴虚证", "formula": "六味地黄丸", "techniques": ["耳穴压豆"]},
    {"symptoms": {"头晕", "胸闷", "肢体困重"}, "syndrome": "痰湿证", "formula": "半夏白术天麻汤", "techniques": ["拔罐", "刮痧"]},
    {"symptoms": {"恶寒重", "发热轻", "无汗", "流清涕", "头身疼痛"}, "syndrome": "感冒风寒证", "formula": "荆防败毒散", "techniques": ["艾灸大椎", "拔罐"]},
    {"symptoms": {"发热重", "咽喉肿痛", "流黄涕", "口渴", "咳黄痰"}, "syndrome": "感冒风热证", "formula": "银翘散", "techniques": ["刮痧", "少商放血"]},
    {"symptoms": {"胃脘冷痛", "喜温喜按", "食少", "便溏", "泛吐清水"}, "syndrome": "脾胃虚寒证", "formula": "理中丸", "techniques": ["隔姜灸中脘", "穴位贴敷足三里"]},
    {"symptoms": {"胁肋胀痛", "情志抑郁", "善太息", "嗳气", "月经不调"}, "syndrome": "肝郁气滞证", "formula": "柴胡疏肝散", "techniques": ["针刺太冲", "耳穴压豆"]},
    {"symptoms": {"刺痛固定", "夜间痛甚", "面色晦暗", "唇舌紫暗", "肌肤甲错"}, "syndrome": "血瘀证", "formula": "血府逐瘀汤", "techniques": ["刺络放血", "拔罐"]},
    {"symptoms": {"口苦", "身重困倦", "小便短黄", "大便黏滞", "面垢油光"}, "syndrome": "湿热证", "formula": "三仁汤", "techniques": ["刮痧", "拔罐"]},
    {"symptoms": {"心悸", "失眠", "多梦", "健忘", "食少乏力"}, "syndrome": "心脾两虚证", "formula": "归脾汤", "techniques": ["艾灸神门", "耳穴压豆"]},
    {"symptoms": {"关节疼痛", "屈伸不利", "遇寒加重", "关节肿胀"}, "syndrome": "风寒湿痹证", "formula": "蠲痹汤", "techniques": ["温针灸", "中药熏洗"]},
    {"symptoms": {"眩晕", "耳鸣", "急躁易怒", "面红目赤", "头胀痛"}, "syndrome": "肝阳上亢证", "formula": "天麻钩藤饮", "techniques": ["针刺风池", "耳尖放血"]},
]

# 标准化简表计分（参照《中医体质分类与判定》转化分算法）：
# 每一体质维度若干条目，逐条按 1-5 计分；
# 原始分 = 条目分之和；转化分 = (原始分 - 条目数) / (条目数 × 4) × 100
CONSTITUTION_JUDGE_THRESHOLD = 40  # 偏颇体质：转化分≥40 判定"是"
CONSTITUTION_TENDENCY_THRESHOLD = 30  # 30-39 判定"倾向是"


class ConstitutionBody(BaseModel):
    # 方式一：各体质维度转化分 0-100（已自行换算）
    scores: dict[str, int] | None = None
    # 方式二：标准化简表原始条目得分（每条 1-5），由平台按转化分公式计分
    answers: dict[str, list[int]] | None = None


def _transformed_scores(answers: dict[str, list[int]]) -> dict[str, int]:
    scores: dict[str, int] = {}
    for key, items in answers.items():
        if key not in CONSTITUTIONS:
            continue
        if not items or any(not 1 <= v <= 5 for v in items):
            raise HTTPException(status_code=422, detail=f"体质 {key} 的条目得分须为 1-5 分")
        raw = sum(items)
        scores[key] = round((raw - len(items)) / (len(items) * 4) * 100)
    return scores


@router.get("/constitution/spec")
def constitution_spec():
    """标准化简表计分说明（转化分算法与判定阈值）。"""
    return {
        "method": "中医体质分类与判定标准化简表",
        "item_scoring": "每一体质维度若干条目，按症状出现频度 1-5 分（没有=1 … 总是=5）逐条计分",
        "raw_score": "原始分 = 该维度各条目得分之和",
        "transformed_score": "转化分 = (原始分 - 条目数) / (条目数 × 4) × 100",
        "judge": {
            "positive": f"偏颇体质转化分 ≥ {CONSTITUTION_JUDGE_THRESHOLD} 判定为该体质",
            "tendency": f"转化分 {CONSTITUTION_TENDENCY_THRESHOLD}-{CONSTITUTION_JUDGE_THRESHOLD - 1} 判定为倾向体质",
            "balanced": f"各偏颇体质转化分均 < {CONSTITUTION_JUDGE_THRESHOLD} 判定为平和质",
        },
        "constitutions": [
            {"key": k, "name": v["name"]} for k, v in CONSTITUTIONS.items()
        ],
    }


@router.post("/constitution")
def identify_constitution(body: ConstitutionBody):
    """体质辨识：支持转化分直报（scores）或标准化简表逐条计分（answers）。"""
    if body.answers:
        scores = _transformed_scores(body.answers)
    elif body.scores:
        # L-12 整改：转化分直报须在 0-100 界内
        if any(not 0 <= v <= 100 for v in body.scores.values()):
            raise HTTPException(status_code=422, detail="转化分须为 0-100 的整数")
        scores = dict(body.scores)
    else:
        raise HTTPException(status_code=422, detail="须提供 scores（转化分）或 answers（简表条目得分）")
    valid = {k: v for k, v in scores.items() if k in CONSTITUTIONS and k != "balanced"}
    if not valid:
        raise HTTPException(status_code=422, detail="缺少有效的体质维度得分")
    top_key, top_score = max(valid.items(), key=lambda kv: kv[1])
    key = top_key if top_score >= CONSTITUTION_JUDGE_THRESHOLD else "balanced"
    tendencies = sorted(
        k
        for k, v in valid.items()
        if CONSTITUTION_TENDENCY_THRESHOLD <= v < CONSTITUTION_JUDGE_THRESHOLD and k != key
    )
    return {
        "constitution": CONSTITUTIONS[key]["name"],
        "score": top_score,
        "transformed_scores": valid,
        "tendencies": [CONSTITUTIONS[k]["name"] for k in tendencies],
        **CONSTITUTIONS[key],
    }


class DiagnoseBody(BaseModel):
    symptoms: list[str] = Field(min_length=1)


@router.post("/assist-diagnosis")
def assist_diagnosis(body: DiagnoseBody):
    """智能辨证：按症状匹配度推荐证型、方剂与适宜技术。"""
    given = set(body.symptoms)
    candidates: list[dict[str, Any]] = [
        {
            "syndrome": kb["syndrome"],
            "matched": sorted(kb["symptoms"] & given),
            "match_count": len(kb["symptoms"] & given),
            "formula": kb["formula"],
            "techniques": kb["techniques"],
        }
        for kb in SYNDROME_KB
    ]
    ranked = sorted(candidates, key=lambda r: r["match_count"], reverse=True)
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
    technique = insert_or_conflict(db, TcmTechnique(**body.model_dump()), "该技术已入库")
    return technique


@router.get("/techniques", response_model=list[TechniqueOut])
def list_techniques(keyword: str = "", db: Session = Depends(get_db)):
    query = db.query(TcmTechnique)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter((TcmTechnique.name.like(like)) | (TcmTechnique.indication.like(like)))
    return query.order_by(TcmTechnique.id).limit(200).all()


# ===========================================================================
# ⑭ 中药制剂管理（配方 / 批次 / 效期）
# ===========================================================================


DOSAGE_FORMS = {
    "pill": "丸剂",
    "powder": "散剂",
    "paste": "膏剂",
    "granule": "颗粒剂",
    "decoction": "合剂/汤剂",
}


class FormulaCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1)
    dosage_form: str = Field(default="decoction", pattern="^(pill|powder|paste|granule|decoction)$")
    composition: str = ""
    process: str = ""
    indication: str = ""
    shelf_life_months: int = Field(default=12, ge=1, le=120)


def _formula_out(f: TcmFormula) -> dict:
    return {
        "id": f.id,
        "code": f.code,
        "name": f.name,
        "dosage_form": f.dosage_form,
        "dosage_form_name": DOSAGE_FORMS.get(f.dosage_form, f.dosage_form),
        "composition": f.composition,
        "process": f.process,
        "indication": f.indication,
        "shelf_life_months": f.shelf_life_months,
        "active": f.active,
    }


@router.post(
    "/formulas",
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist", "doctor"))],  # 制剂配方由药师/中医师维护
)
def create_formula(body: FormulaCreate, db: Session = Depends(get_db)):
    """新增中药制剂配方（编码唯一）。"""
    if db.query(TcmFormula).filter(TcmFormula.code == body.code).first():
        raise HTTPException(status_code=409, detail="制剂编码已存在")
    formula = insert_or_conflict(db, TcmFormula(**body.model_dump()), "制剂编码已存在")
    return _formula_out(formula)


@router.get("/formulas")
def list_formulas(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(TcmFormula)
    if active is not None:
        query = query.filter(TcmFormula.active.is_(active))
    return [_formula_out(f) for f in query.order_by(TcmFormula.id.desc()).limit(200).all()]


class BatchCreate(BaseModel):
    formula_id: int
    batch_no: str = Field(min_length=1, max_length=32)
    org_id: int
    quantity: int = Field(ge=1)
    unit: str = "剂"
    produced_date: DateStr
    # 不传则按配方有效期（月）自动推算
    expire_date: str = ""


def _batch_out(b: TcmPreparationBatch, today: str) -> dict:
    return {
        "id": b.id,
        "formula_id": b.formula_id,
        "batch_no": b.batch_no,
        "org_id": b.org_id,
        "quantity": b.quantity,
        "unit": b.unit,
        "produced_date": b.produced_date,
        "expire_date": b.expire_date,
        "status": b.status,
        "expired": bool(b.expire_date and b.expire_date < today),
    }


@router.post(
    "/preparation-batches",
    status_code=201,
    dependencies=[Depends(require_roles("pharmacist", "operator"))],
)
def create_batch(body: BatchCreate, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    """制剂投料生产建批次：效期缺省按配方有效期（月）自动推算。"""
    formula = db.get(TcmFormula, body.formula_id)
    if formula is None:
        raise HTTPException(status_code=404, detail="制剂配方不存在")
    if not formula.active:
        raise HTTPException(status_code=409, detail="配方已停用，不可投产")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="生产机构不存在")
    if db.query(TcmPreparationBatch).filter(TcmPreparationBatch.batch_no == body.batch_no).first():
        raise HTTPException(status_code=409, detail="批号已存在")
    expire_date = body.expire_date
    if not expire_date:
        produced = date.fromisoformat(body.produced_date)
        expire_date = (produced + timedelta(days=30 * formula.shelf_life_months)).isoformat()
    if expire_date <= body.produced_date:
        raise HTTPException(status_code=422, detail="效期须晚于生产日期")
    batch = insert_or_conflict(db, TcmPreparationBatch(
            **body.model_dump(exclude={"expire_date"}), expire_date=expire_date, created_by=user.id
        ), "批号已存在")
    return _batch_out(batch, date.today().isoformat())


@router.get("/preparation-batches")
def list_batches(
    formula_id: int | None = None,
    status: str | None = None,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    business_date = resolve_business_date(today).isoformat()
    query = db.query(TcmPreparationBatch)
    if formula_id is not None:
        query = query.filter(TcmPreparationBatch.formula_id == formula_id)
    if status:
        query = query.filter(TcmPreparationBatch.status == status)
    return [
        _batch_out(b, business_date)
        for b in query.order_by(TcmPreparationBatch.id.desc()).limit(200).all()
    ]


@router.get("/preparation-batches/expiring")
def expiring_batches(days: int = 30, today: str | None = None, db: Session = Depends(get_db)):
    """效期预警：N 天内到期或已过期的未召回批次。"""
    business_date = resolve_business_date(today)
    cutoff = (business_date + timedelta(days=max(days, 0))).isoformat()
    rows = (
        db.query(TcmPreparationBatch)
        .filter(
            TcmPreparationBatch.status != "recalled",
            TcmPreparationBatch.expire_date != "",
            TcmPreparationBatch.expire_date <= cutoff,
        )
        .order_by(TcmPreparationBatch.expire_date)
        .all()
    )
    return [_batch_out(b, business_date.isoformat()) for b in rows]


@router.post(
    "/preparation-batches/{batch_id}/release",
    dependencies=[Depends(require_roles("pharmacist", "operator"))],
)
def release_batch(batch_id: int, today: str | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批次发放：过期批次禁止发放（效期管控）。"""
    batch = db.get(TcmPreparationBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    assert_obj_org_writable(db, user, batch)
    if batch.status != "produced":
        raise HTTPException(status_code=409, detail=f"批次当前状态 {batch.status} 不可发放")
    business_date = resolve_business_date(today).isoformat()
    if batch.expire_date and batch.expire_date < business_date:
        raise HTTPException(status_code=409, detail="批次已过效期，禁止发放")
    batch.status = "released"
    db.commit()
    db.refresh(batch)
    return _batch_out(batch, business_date)
