"""报告段落取数：注册表 + 内置渲染器 + 指标段落。

P1-3 之前段落渲染是路由文件里的一串 if——模板可以随便建，但建了新段落等于
建了个空壳。现在：

1. **注册表**：`register_section(key, fn)`，与数据源采集器同一形状。实施期要加
   县本地的段落，注册一个函数即可，不改路由与调度；
2. **指标段落**：`{"key": "indicator", "indicator_code": "referral_closure_rate"}`
   直接复用考核指标库的取数口径（`routers/assess.py::collect_metrics` + 公式求值）。
   **报表与考核必须同源**——两套口径各算各的，是"报告数字和考核对不上"的根源，
   这里从结构上堵死；
3. 未注册的 key 返回"未配置取数口径"，不让整份报告失败。

渲染器签名：`(db, section: dict, org_id: int | None, period: str) -> dict`，
返回体必带 `key` / `title` / `type`（text|table|chart）。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Callable

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import (
    SpdCandidate,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdIndicator,
    SpdPointAccount,
    SpdReferralCase,
    SpdScore,
    SpdScreening,
    SpdTask,
    SpdTeam,
    SpdVillageDoctor,
)
from .platform import User

SectionRenderer = Callable[[Session, dict, "int | None", str], dict]

_SECTIONS: dict[str, SectionRenderer] = {}


def register_section(key: str, renderer: SectionRenderer) -> None:
    _SECTIONS[key] = renderer


def registered_sections() -> list[str]:
    return sorted(_SECTIONS)


def compose_section(db: Session, section: dict, org_id: int | None, period: str) -> dict:
    key = section.get("key", "")
    renderer = _SECTIONS.get(key)
    if renderer is None:
        return {"key": key, "title": section.get("title", key),
                "type": section.get("type", "text"),
                "note": f"该段落未配置取数口径（可用：{'、'.join(registered_sections())}）"}
    return renderer(db, section, org_id, period)


def default_period_label(period: str, today: date | None = None) -> str:
    today = today or date.today()
    if period == "weekly":
        return f"{today.isocalendar().year}年第{today.isocalendar().week}周"
    if period == "monthly":
        return today.strftime("%Y年%m月")
    return today.isoformat()


# ---------------------------------------------------------------- 内置渲染器


def _head(section: dict, kind: str) -> dict:
    return {"key": section.get("key", ""), "title": section.get("title", section.get("key", "")),
            "type": kind}


def _task_query(db: Session, org_id: int | None):
    query = db.query(SpdTask)
    if org_id is not None:
        query = query.filter(SpdTask.org_id == org_id)
    return query


def _summary(db, section, org_id, period):
    enroll_query = db.query(SpdEnrollment).filter(SpdEnrollment.status == "active")
    if org_id is not None:
        enroll_query = enroll_query.filter(SpdEnrollment.org_id == org_id)
    task_query = _task_query(db, org_id)
    open_tasks = task_query.filter(
        SpdTask.status.in_(["pending", "claimed", "doing", "submitted", "overdue"])
    ).count()
    overdue = task_query.filter(SpdTask.status == "overdue").count()
    enrolled = enroll_query.count()
    return {
        **_head(section, "text"),
        "text": f"在管患者 {enrolled} 人，待办任务 {open_tasks} 条，其中超期 {overdue} 条。",
        "metrics": {"enrolled": enrolled, "open_tasks": open_tasks, "overdue": overdue},
    }


def _todo(db, section, org_id, period):
    rows = (
        _task_query(db, org_id).filter(SpdTask.status.in_(["pending", "claimed", "doing"]))
        .order_by(SpdTask.due_date).limit(20).all()
    )
    return {**_head(section, "table"), "columns": ["任务", "类型", "截止", "优先级"],
            "rows": [[r.title, r.task_type, r.due_date, r.priority] for r in rows]}


def _alert(db, section, org_id, period):
    rows = _task_query(db, org_id).filter(SpdTask.status == "overdue").limit(20).all()
    return {**_head(section, "table"), "columns": ["超期任务", "类型", "截止日期"],
            "rows": [[r.title, r.task_type, r.due_date] for r in rows]}


def _workload(db, section, org_id, period):
    rows = (
        _task_query(db, org_id).filter(SpdTask.status == "done")
        .with_entities(SpdTask.task_type, func.count(SpdTask.id))
        .group_by(SpdTask.task_type).all()
    )
    return {**_head(section, "table"), "columns": ["任务类型", "完成数"],
            "rows": [[t, c] for t, c in rows]}


def _followup_trend(db, section, org_id, period):
    since = date.today() - timedelta(days=30)
    query = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.planned_at >= since.isoformat())
    if org_id is not None:
        query = query.filter(SpdFollowupRecord.org_id == org_id)
    buckets: dict[str, dict] = {}
    for row in query.all():
        entry = buckets.setdefault(row.planned_at[:7], {"total": 0, "done": 0})
        entry["total"] += 1
        if row.status == "done":
            entry["done"] += 1
    return {
        **_head(section, "chart"),
        "series": [
            {"label": label, "total": v["total"], "done": v["done"],
             "rate": round(v["done"] / v["total"] * 100, 1) if v["total"] else 0.0}
            for label, v in sorted(buckets.items())
        ],
    }


#: 考核对象 → 该对象归属机构的解析方式。`spd_scores` 上没有 org_id 列
#: （考核对象可以是机构、团队、村医、医师四类），所以按 object_type 分派去查。
#:
#: 口径取"**恰好属于该机构**"而不是"该机构及其下级"——与本模块其余段落一致
#: （`_screening` 等都是 `X.org_id == org_id`）。报表段落之间口径必须一样，
#: 否则同一份报告里两段数字对不上，比少一段更难查。
_SCORE_OBJECT_ORG = {
    # 考核对象就是机构本身：object_id 即 org_id
    "org": lambda db, org_id: [org_id],
    "team": lambda db, org_id: [
        t.id for t in db.query(SpdTeam.id).filter(SpdTeam.org_id == org_id)
    ],
    "village_doctor": lambda db, org_id: [
        v.user_id for v in db.query(SpdVillageDoctor.user_id).filter(
            SpdVillageDoctor.user_id.in_(
                db.query(User.id).filter(User.org_id == org_id)
            )
        )
    ],
    "doctor": lambda db, org_id: [
        u.id for u in db.query(User.id).filter(User.org_id == org_id)
    ],
}


def _score(db, section, org_id, period):
    """考核排名段落。

    修的两处：渲染器签名收了 `org_id` 与 `period`，这里**两个都没用上**——
    于是任何机构、任何周期的报告，这一段都是同一份"最近 20 条"。
    机构报告里印着别家机构的排名，季度报告里印着上个月的分数。

    - `period` 过滤没有歧义：报告是按周期出的。
    - `org_id` 过滤要按 `object_type` 分派（`spd_scores` 上没有 org_id 列，
      考核对象有机构/团队/村医/医师四类），见 `_SCORE_OBJECT_ORG`。
    """
    query = db.query(SpdScore)
    if period:
        query = query.filter(SpdScore.period == period)
    if org_id is not None:
        clauses = []
        for object_type, resolve in _SCORE_OBJECT_ORG.items():
            ids = resolve(db, org_id)
            if ids:
                clauses.append(
                    (SpdScore.object_type == object_type) & SpdScore.object_id.in_(ids)
                )
        if not clauses:
            # 该机构名下没有任何可考核对象——返回空表，而不是退回全域数据
            return {**_head(section, "table"), "columns": ["对象", "周期", "得分", "排名"],
                    "rows": []}
        condition = clauses[0]
        for extra in clauses[1:]:
            condition = condition | extra
        query = query.filter(condition)
    rows = query.order_by(SpdScore.id.desc()).limit(20).all()
    return {**_head(section, "table"), "columns": ["对象", "周期", "得分", "排名"],
            "rows": [[r.object_name, r.period, r.total_score, r.rank] for r in rows]}


def _screening(db, section, org_id, period):
    """筛查转化：筛出多少、疑似多少、进目标池多少、纳管多少。"""
    query = db.query(SpdScreening)
    if org_id is not None:
        query = query.filter(SpdScreening.org_id == org_id)
    screened = query.count()
    suspect = query.filter(SpdScreening.result == "suspect").count()
    cand_query = db.query(SpdCandidate)
    enroll_query = db.query(SpdEnrollment).filter(SpdEnrollment.status == "active")
    if org_id is not None:
        cand_query = cand_query.filter(SpdCandidate.org_id == org_id)
        enroll_query = enroll_query.filter(SpdEnrollment.org_id == org_id)
    return {
        **_head(section, "table"),
        "columns": ["环节", "人数"],
        "rows": [["累计筛查", screened], ["疑似", suspect],
                 ["目标池（待签约）", cand_query.filter(SpdCandidate.status == "target").count()],
                 ["已纳管", enroll_query.count()]],
    }


def _referral(db, section, org_id, period):
    """转诊闭环：与 /api/spd/referrals-stats/closure 同一分母口径（剔除撤回与退回）。"""
    query = db.query(SpdReferralCase)
    if org_id is not None:
        query = query.filter(SpdReferralCase.initiator_org_id == org_id)
    by_status = dict(
        query.with_entities(SpdReferralCase.status, func.count(SpdReferralCase.id))
        .group_by(SpdReferralCase.status).all()
    )
    total = sum(by_status.values())
    denominator = total - by_status.get("withdrawn", 0) - by_status.get("rejected", 0)
    closed = by_status.get("closed", 0)
    return {
        **_head(section, "table"),
        "columns": ["指标", "数值"],
        "rows": [["转诊总量", total], ["计入闭环分母", denominator], ["已闭环", closed],
                 ["闭环率", f"{round(closed / denominator * 100, 1) if denominator else 0.0}%"]],
    }


def _points(db, section, org_id, period):
    query = db.query(SpdPointAccount)
    if org_id is not None:
        query = query.filter(SpdPointAccount.org_id == org_id)
    rows = query.order_by(SpdPointAccount.balance.desc()).limit(10).all()
    return {**_head(section, "table"), "columns": ["用户ID", "余额", "累计获得", "累计兑换"],
            "rows": [[r.user_id, r.balance, r.earned, r.used] for r in rows]}


def _indicator(db, section, org_id, period):
    """指标段落：直接走考核指标库的取数与公式——报表与考核同源。

    段落配置：`{"key": "indicator", "indicator_code": "...", "title": "..."}`。
    对象按段落所属报告的机构（org_id 为空则全域视角不支持——考核口径是按对象算的，
    没有对象就没有数）。
    """
    from ..formula import FormulaError, evaluate as eval_formula
    from .routers.assess import collect_metrics

    code = section.get("indicator_code", "")
    indicator = (
        db.query(SpdIndicator)
        .filter(SpdIndicator.code == code, SpdIndicator.active.is_(True))
        .order_by(SpdIndicator.id.desc())
        .first()
    )
    if indicator is None:
        return {**_head(section, "text"), "note": f"指标 {code} 不存在或已停用"}
    if org_id is None:
        return {**_head(section, "text"),
                "note": "指标段落需要报告绑定机构（考核口径按对象取数）"}
    # 这里的 period 是模板的频率关键字（daily/weekly/monthly），考核取数要的是
    # 具体周期值（2026-08 / 2026-Q3 / 2026）；段落可用 "period" 显式指定，
    # 否则按当月取——日报/周报看的也是本月累计口径
    period_value = section.get("period") or date.today().strftime("%Y-%m")
    metrics = collect_metrics(db, indicator, "org", org_id, period_value)
    try:
        value = (
            eval_formula(indicator.formula, metrics)
            if indicator.formula else float(metrics.get("total", 0))
        )
    except FormulaError as exc:
        return {**_head(section, "text"), "note": f"公式求值失败：{exc}"}
    return {
        **_head(section, "text"),
        "text": f"{indicator.name}：{round(value, 2)}"
                + (f"（目标 {indicator.target_value}）" if indicator.target_value else ""),
        "metrics": metrics, "value": round(value, 2),
        "indicator_code": indicator.code,
    }


for _key, _fn in (
    ("summary", _summary), ("todo", _todo), ("alert", _alert), ("workload", _workload),
    ("quality", _followup_trend), ("trend", _followup_trend), ("score", _score),
    ("screening", _screening), ("referral", _referral), ("points", _points),
    ("indicator", _indicator),
):
    register_section(_key, _fn)
