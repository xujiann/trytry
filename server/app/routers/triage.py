"""智能导诊：症状匹配推荐科室，急症提示急诊。

自 `service_extras.py`（倾倒场）搬出（ADR-0006）。**新建模块**：导诊是患者到院
前的入口环节，不隶属门诊/急诊/预约中的任何一个。

知识库 `_TRIAGE_KB` 是硬编码的六组症状——这是**现状不是设计**，做成可配置属于
另一件事（届时应落表并配管理端，别在这里悄悄加分支）。

路径一字未改：`POST /api/triage/suggest`。
"""
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user

router = APIRouter(
    prefix="/api/triage", tags=["智能导诊"], dependencies=[Depends(get_current_user)]
)


_TRIAGE_KB = [
    ({"胸痛", "胸闷", "心悸"}, "心血管内科", True),
    ({"咳嗽", "咳痰", "气喘", "发热"}, "呼吸内科", False),
    ({"腹痛", "腹泻", "呕吐", "反酸"}, "消化内科", False),
    ({"头晕", "头痛", "肢体麻木"}, "神经内科", True),
    ({"尿频", "尿急", "血尿"}, "泌尿外科", False),
    ({"皮疹", "瘙痒"}, "皮肤科", False),
]


@router.post("/suggest")
def triage_suggest(symptoms: list[str], db: Session = Depends(get_db)):
    """智能导诊：症状匹配推荐科室，急症症状提示急诊。"""
    given = set(symptoms)
    candidates: list[dict[str, Any]] = [
        {"department": dept, "matched": sorted(kb & given), "urgent": urgent}
        for kb, dept, urgent in _TRIAGE_KB
        if kb & given
    ]
    ranked = sorted(candidates, key=lambda r: len(r["matched"]), reverse=True)
    return {
        "recommendations": ranked[:3] or [{"department": "全科门诊", "matched": [], "urgent": False}],
        "emergency_hint": any(r["urgent"] for r in ranked[:1]),
    }
