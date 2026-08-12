"""统一规则引擎（T5.1）。

平台里已有四套各自实现的规则：审方规则、数据质控规则、病历质控规则、绩效指标。
这里**不做大爆炸式重写**——那要动 450 项回归测试里的相当一部分，风险远大于收益。
采取兼容并行：

- **统一目录**：`GET /api/rules/catalog` 把四套既有规则以只读方式并入同一视图，
  管理员第一次能在一个地方看全平台所有生效规则；
- **统一引擎**：新增规则用统一的条件 DSL（`app/rules.py`）录入并求值，
  不必再改代码加分支；
- 既有规则继续按原路运行，迁移是后续可选动作，不是本轮前提。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_admin
from ..models import DrugRule, PerformanceIndicator, QcRule, RecordQcRule, RuleDefinition
from ..rules import RuleError, evaluate_condition, validate_condition

router = APIRouter(prefix="/api/rules", tags=["统一规则引擎"], dependencies=[Depends(get_current_user)])

# 各规则域的可用变量与样例值：样例值同时用于录入时试算，
# 因此必须覆盖真实类型（数值 / 字符串 / 布尔），否则校验放行的条件运行时才炸。
DOMAIN_VARIABLES: dict[str, dict] = {
    "prescription": {
        "daily_dose": 500.0,
        "max_daily_dose": 1000.0,
        "days": 7.0,
        "age": 40.0,
        "drug_code": "METFORMIN",
        "diagnosis_name": "2型糖尿病",
        "is_pregnant": False,
        "renal_impairment": False,
    },
    "medical_record": {
        "chief_complaint": "咳嗽3天",
        "present_illness": "患者3天前受凉后出现咳嗽",
        "past_history": "无特殊",
        "physical_exam": "双肺呼吸音清",
        "diagnosis_basis": "结合症状体征",
        "treatment_plan": "对症治疗",
        "qc_score": 100.0,
    },
    "exam": {
        "center_type": "imaging",
        "item_code": "DR-CHEST",
        "critical": False,
        "turnaround_hours": 4.0,
    },
    "org_performance": {
        "encounters": 100.0,
        "referrals_up": 5.0,
        "referrals_down": 3.0,
        "chronic_patients": 50.0,
        "avg_length_of_stay": 7.5,
        "bed_occupancy_rate_pct": 82.0,
    },
}


@router.get("/domains")
def list_domains():
    """规则域与可用变量清单：管理员写条件时的字段字典。"""
    return [
        {
            "domain": domain,
            "variables": [
                {"name": name, "sample": value, "type": type(value).__name__}
                for name, value in sorted(variables.items())
            ],
        }
        for domain, variables in sorted(DOMAIN_VARIABLES.items())
    ]


class RuleIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=128)
    domain: str = Field(min_length=1, max_length=24)
    condition: str = Field(min_length=1, max_length=512)
    message: str = ""
    severity: str = Field(default="warning", pattern="^(info|warning|error)$")
    deduct_points: int = Field(default=0, ge=0, le=100)


def _rule_out(r: RuleDefinition) -> dict:
    return {
        "id": r.id,
        "key": r.key,
        "name": r.name,
        "domain": r.domain,
        "condition": r.condition,
        "message": r.message,
        "severity": r.severity,
        "deduct_points": r.deduct_points,
        "active": r.active,
    }


@router.post("", status_code=201, dependencies=[Depends(require_admin)])
def create_rule(body: RuleIn, db: Session = Depends(get_db)):
    """录入统一规则。条件在录入时就用样例值试算，非法直接 422。"""
    if body.domain not in DOMAIN_VARIABLES:
        raise HTTPException(
            status_code=422, detail=f"未知规则域，可选：{'、'.join(sorted(DOMAIN_VARIABLES))}"
        )
    try:
        validate_condition(body.condition, DOMAIN_VARIABLES[body.domain])
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    rule = RuleDefinition(**body.model_dump())
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="规则编码已存在") from None
    db.refresh(rule)
    return _rule_out(rule)


@router.get("")
def list_rules(domain: str | None = None, db: Session = Depends(get_db)):
    query = db.query(RuleDefinition)
    if domain:
        query = query.filter(RuleDefinition.domain == domain)
    return [_rule_out(r) for r in query.order_by(RuleDefinition.key).all()]


@router.delete("/{key}", dependencies=[Depends(require_admin)])
def deactivate_rule(key: str, db: Session = Depends(get_db)):
    """停用规则（不物理删除：历史判定结果要能解释当时的口径）。"""
    rule = db.query(RuleDefinition).filter(RuleDefinition.key == key).first()
    if rule is None:
        raise HTTPException(status_code=404, detail="规则不存在")
    rule.active = False
    db.commit()
    return {"key": key, "active": False}


class EvaluateIn(BaseModel):
    domain: str
    variables: dict


@router.post("/evaluate")
def evaluate_rules(body: EvaluateIn, db: Session = Depends(get_db)):
    """按域求值：返回命中的规则、总扣分与是否存在拦截级命中。

    缺失的变量用域样例补齐——业务侧新增字段前，旧规则不该因为少一个变量就报错；
    单条规则求值失败记 error 并跳过，不影响其他规则（一条写坏的规则不能让
    整个审方/质控停摆）。
    """
    if body.domain not in DOMAIN_VARIABLES:
        raise HTTPException(status_code=422, detail="未知规则域")
    variables = {**DOMAIN_VARIABLES[body.domain], **body.variables}
    rules = (
        db.query(RuleDefinition)
        .filter(RuleDefinition.domain == body.domain, RuleDefinition.active.is_(True))
        .order_by(RuleDefinition.key)
        .all()
    )
    hits, errors = [], []
    for rule in rules:
        try:
            if evaluate_condition(rule.condition, variables):
                hits.append(
                    {
                        "key": rule.key,
                        "name": rule.name,
                        "severity": rule.severity,
                        "message": rule.message or rule.name,
                        "deduct_points": rule.deduct_points,
                    }
                )
        except RuleError as exc:
            errors.append({"key": rule.key, "error": str(exc)})
    return {
        "domain": body.domain,
        "evaluated": len(rules),
        "hits": hits,
        "errors": errors,
        "total_deduction": sum(h["deduct_points"] for h in hits),
        "blocked": any(h["severity"] == "error" for h in hits),
    }


@router.get("/catalog")
def rule_catalog(db: Session = Depends(get_db)):
    """全平台规则总目录。

    把四套既有规则以只读方式并入同一视图——管理员第一次能在一个地方看全
    平台所有生效规则。`engine` 字段标明该规则由统一引擎求值还是由既有模块
    内部实现：目录是统一的，执行路径暂时不是，不在这里含糊其辞。
    """
    entries = [
        {
            "source": "unified",
            "engine": "unified",
            "domain": r.domain,
            "key": r.key,
            "name": r.name,
            "detail": r.condition,
            "active": r.active,
        }
        for r in db.query(RuleDefinition).order_by(RuleDefinition.key).all()
    ]
    entries += [
        {
            "source": "prescription",
            "engine": "legacy",
            "domain": "prescription",
            "key": r.drug_code,
            "name": f"{r.drug_code} 用药规则",
            "detail": f"最大日剂量 {r.max_daily_dose}{r.dose_unit}",
            "active": True,
        }
        for r in db.query(DrugRule)
        .filter(DrugRule.active.is_(True))
        .order_by(DrugRule.drug_code)
        .limit(500)
        .all()
    ]
    entries += [
        {
            "source": "data_quality",
            "engine": "legacy",
            "domain": "data_quality",
            "key": r.code,
            "name": r.name,
            "detail": f"{r.rule_type} on {r.target_table}",
            "active": r.active,
        }
        for r in db.query(QcRule).order_by(QcRule.code).all()
    ]
    entries += [
        {
            "source": "record_quality",
            "engine": "legacy",
            "domain": "medical_record",
            "key": r.code,
            "name": r.name,
            "detail": f"{r.rule} on {r.check_field}",
            "active": r.active,
        }
        for r in db.query(RecordQcRule).order_by(RecordQcRule.code).all()
    ]
    entries += [
        {
            "source": "performance",
            "engine": "legacy",
            "domain": "org_performance",
            "key": r.key,
            "name": r.name,
            "detail": f"权重 {r.weight}",
            "active": r.active,
        }
        for r in db.query(PerformanceIndicator).order_by(PerformanceIndicator.key).all()
    ]
    by_source: dict[str, int] = {}
    for entry in entries:
        by_source[entry["source"]] = by_source.get(entry["source"], 0) + 1
    return {"total": len(entries), "by_source": by_source, "entries": entries}
