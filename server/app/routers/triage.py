"""智能导诊：症状匹配推荐科室，急症提示急诊。

自 `service_extras.py`（倾倒场）搬出（ADR-0006）。**新建模块**：导诊是患者到院
前的入口环节，不隶属门诊/急诊/预约中的任何一个。

知识库 `_TRIAGE_KB` 是硬编码的六组症状——这是**现状不是设计**，做成可配置属于
另一件事（届时应落表并配管理端，别在这里悄悄加分支）。相应地，handler **不接
`db` 参数**：原先那个 `db: Session = Depends(get_db)` 一行库都不读，是个死参数。

顺带把它的代价说准，免得下一个人照着错的理由再加回来：它**并没有**多借一条
连接。FastAPI 的依赖默认 `use_cache=True`，而路由级的 `get_current_user` 本身
就依赖 `get_db`，同一请求里只解析一次——实测带与不带那个参数，一次请求都只开
1 条 Session。所以去掉它是**清死参数**，不是省连接。等知识库真落表时，连
`Depends` 带查询一起加，那时它才对应一次真实读库。

路径一字未改：`POST /api/triage/suggest`。
"""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from ..deps import get_current_user

router = APIRouter(
    prefix="/api/triage", tags=["智能导诊"], dependencies=[Depends(get_current_user)]
)


class TriageRecommendationOut(BaseModel):
    department: str
    # 命中的症状词；无命中时回落项给的是空列表
    matched: list[str]
    urgent: bool


class TriageSuggestOut(BaseModel):
    """导诊建议。一条都没匹配上时**不返回空列表**，而是回落到「全科门诊」——
    让居民永远拿得到一个可去的地方，比诚实地回一个空数组有用。"""

    recommendations: list[TriageRecommendationOut]
    emergency_hint: bool


_TRIAGE_KB = [
    ({"胸痛", "胸闷", "心悸"}, "心血管内科", True),
    ({"咳嗽", "咳痰", "气喘", "发热"}, "呼吸内科", False),
    ({"腹痛", "腹泻", "呕吐", "反酸"}, "消化内科", False),
    ({"头晕", "头痛", "肢体麻木"}, "神经内科", True),
    ({"尿频", "尿急", "血尿"}, "泌尿外科", False),
    ({"皮疹", "瘙痒"}, "皮肤科", False),
]


@router.post("/suggest", response_model=TriageSuggestOut)
def triage_suggest(symptoms: list[str]):
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
