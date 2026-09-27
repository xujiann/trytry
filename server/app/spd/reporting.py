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
from typing import Any, Callable

from sqlalchemy import ColumnElement, false, func, or_, select
from sqlalchemy.orm import Session

from .. import clock
from .models import (
    SpdCandidate,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdPointAccount,
    SpdReferralCase,
    SpdScore,
    SpdScreening,
    SpdTask,
    SpdTeam,
    SpdVillageDoctor,
)
from .platform import User
from .service import TASK_OPEN_STATUSES, task_overdue

SectionRenderer = Callable[[Session, dict, "int | None", str], dict]

#: 报告模板的频率（`spd_report_templates.period`）：渲染器收到的 `period` 是它们之一，不是考核 / 统计的周期值
REPORT_FREQUENCIES = ("daily", "weekly", "monthly", "custom")

_SECTIONS: dict[str, SectionRenderer] = {}
_SECTION_NAMES: dict[str, str] = {}


def register_section(key: str, renderer: SectionRenderer, name: str = "") -> None:
    """`name` 是管理端建模板时这个段落的显示名（也作段落的默认标题）；不给就用 key。"""
    _SECTIONS[key] = renderer
    _SECTION_NAMES[key] = name or key


def registered_sections() -> list[str]:
    return sorted(_SECTIONS)


def section_options() -> list[dict]:
    """建报告模板可选的段落（码 + 名称，按注册顺序）：`GET /api/spd/meta` 的 `report_sections`，前端不另抄段落表。"""
    return [{"key": key, "name": _SECTION_NAMES[key]} for key in _SECTIONS]


def compose_section(db: Session, section: dict, org_id: int | None, period: str) -> dict:
    key = section.get("key", "")
    renderer = _SECTIONS.get(key)
    if renderer is None:
        return {"key": key, "title": section.get("title", key),
                "type": section.get("type", "text"),
                "note": f"该段落未配置取数口径（可用：{'、'.join(registered_sections())}）"}
    return renderer(db, section, org_id, period)


def default_period_label(period: str, today: date | None = None) -> str:
    today = today or clock.today()
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
    open_tasks = task_query.filter(SpdTask.status.in_(TASK_OPEN_STATUSES)).count()
    overdue = task_query.filter(task_overdue(clock.today().isoformat())).count()   # 含扫描间隙里过期的（P2-549）
    enrolled = enroll_query.count()
    return {
        **_head(section, "text"),
        "text": f"在管患者 {enrolled} 人，待办任务 {open_tasks} 条，其中超期 {overdue} 条。",
        "metrics": {"enrolled": enrolled, "open_tasks": open_tasks, "overdue": overdue},
    }


def _todo(db, section, org_id, period):
    # 超期的另列一表（「超期预警」）：扫描间隙里已过截止日的也归那边（P2-549），同一条任务不在两张表里各出现一次。
    # 取数与「总体概览」的待办数同一个范围（`TASK_OPEN_STATUSES`，与工作台同口径）减去超期的（P2-602）：原先只取在手的，
    # 待审核的概览里算进「待办任务 N 条」、两张表都不列；列进来的标题后面注明「待审核」
    rows = (
        _task_query(db, org_id).filter(SpdTask.status.in_(TASK_OPEN_STATUSES),
                                       ~task_overdue(clock.today().isoformat()))
        .order_by(SpdTask.due_date).limit(20).all()
    )
    return {**_head(section, "table"), "columns": ["任务", "类型", "截止", "优先级"],
            "rows": [[f"{r.title}（待审核）" if r.status == "submitted" else r.title,
                      r.task_type, r.due_date, r.priority] for r in rows]}


def _alert(db, section, org_id, period):
    rows = (
        _task_query(db, org_id).filter(task_overdue(clock.today().isoformat()))
        .order_by(SpdTask.due_date, SpdTask.id).limit(20).all()
    )
    return {**_head(section, "table"), "columns": ["超期任务", "类型", "截止日期"],
            "rows": [[r.title, r.task_type, r.due_date] for r in rows]}


def _workload(db, section, org_id, period):
    rows = (
        _task_query(db, org_id).filter(SpdTask.status == "done")
        .with_entities(SpdTask.task_type, func.count(SpdTask.id))
        .group_by(SpdTask.task_type)
        .order_by(SpdTask.task_type).all()
    )
    return {**_head(section, "table"), "columns": ["任务类型", "完成数"],
            "rows": [[t, c] for t, c in rows]}


def _followup_trend(db, section, org_id, period):
    # 近 30 天、到今天为止（P2-293）：随访计划一次排出多个时间点，原先不设上界——排在未来的随访全是「未完成」，
    # 未来的月份以 0% 进图，本月的完成率也被还没到日子的那些拉低
    today = clock.today()
    # 两端都含，「近 30 天」是今天往前数 29 天（P2-546）：原先减 30、连首带尾 31 个日历日——与症候群监测、资源排班的
    # `end - (days - 1)` 不是一把尺子
    since = today - timedelta(days=29)
    query = db.query(SpdFollowupRecord).filter(SpdFollowupRecord.planned_at >= since.isoformat(),
                                               SpdFollowupRecord.planned_at <= today.isoformat())
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
#: 考核对象类型 → 中文（取值见 `SpdIndicator.object_type` / 考核方案的对象类型）；指标段落拒收非机构指标时用
_OBJECT_TYPE_NAMES = {"org": "机构", "team": "团队", "doctor": "医师", "village_doctor": "村医"}

_SCORE_OBJECT_ORG: dict[str, Callable[[list[int]], Any]] = {   # 给出 `object_id` 的取值范围：机构编号表或子查询
    # 考核对象就是机构本身：object_id 即 org_id
    "org": lambda org_ids: list(org_ids),
    "team": lambda org_ids: select(SpdTeam.id).where(SpdTeam.org_id.in_(org_ids)),
    "village_doctor": lambda org_ids: select(SpdVillageDoctor.user_id).where(
        SpdVillageDoctor.user_id.in_(select(User.id).where(User.org_id.in_(org_ids)))
    ),
    "doctor": lambda org_ids: select(User.id).where(User.org_id.in_(org_ids)),
}


def score_in_orgs(org_ids: list[int]) -> ColumnElement[bool]:
    """考核分里「考核对象属于这些机构」的条件：按 object_type 分派（见 `_SCORE_OBJECT_ORG`）。

    报告的考核排名段落与卫健工作台的考核排名共用（P2-551）；机构名下没有任何考核对象时条件恒不成立——出空表，
    不退回全域数据。
    """
    return or_(*(
        (SpdScore.object_type == object_type) & SpdScore.object_id.in_(resolve(org_ids))
        for object_type, resolve in _SCORE_OBJECT_ORG.items()
    ))


def latest_plan_period_scores(query):
    """一方案一周期：范围内最近算出的那一次考核（方案 + 周期）。不同方案、不同周期的名次放在一张表里没有意义。"""
    latest = query.order_by(SpdScore.id.desc()).with_entities(SpdScore.plan_id, SpdScore.period).first()
    if latest is None:
        return query.filter(false())
    return query.filter(SpdScore.plan_id == latest[0], SpdScore.period == latest[1])


def _score(db, section, org_id, period):
    """考核排名段落。

    修的两处：渲染器签名收了 `org_id` 与 `period`，这里**两个都没用上**——
    于是任何机构、任何周期的报告，这一段都是同一份"最近 20 条"。
    机构报告里印着别家机构的排名，季度报告里印着上个月的分数。

    - `period`：收到的多半是模板频率关键字（见下方 P2-103 的注释），取范围内最近一期；段落可写明周期。
    - `org_id` 过滤要按 `object_type` 分派（`spd_scores` 上没有 org_id 列，
      考核对象有机构/团队/村医/医师四类），见 `_SCORE_OBJECT_ORG`。
    """
    query = db.query(SpdScore)
    if org_id is not None:
        # 该机构名下没有任何可考核对象时条件恒不成立——出空表，而不是退回全域数据
        query = query.filter(score_in_orgs([org_id]))
    # 周期（P2-103）：真正的调用方（定时推送、手工生成）传进来的是模板的频率关键字，不是考核周期值——原先拿它去和
    # 分数的周期（2026Q1 / 2026-08）等值比，推送出去的每一份报告这一段都是空的。段落写明了周期的按段落的；传进来的
    # 就是周期值的照用；频率关键字则取范围内最近算出的一期——只出一期，不混周期
    period_value = section.get("period") or ("" if period in REPORT_FREQUENCIES else period)
    if not period_value:
        latest = query.order_by(SpdScore.id.desc()).with_entities(SpdScore.period).first()
        period_value = latest[0] if latest else ""
    if period_value:
        query = query.filter(SpdScore.period == period_value)
    # 同一周期可能有几套方案各算一遍：只出最近算的那一套（P2-551），两套方案的名次混在一张表里没有意义
    latest_plan = query.order_by(SpdScore.id.desc()).with_entities(SpdScore.plan_id).first()
    if latest_plan is not None:
        query = query.filter(SpdScore.plan_id == latest_plan[0])
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
        .group_by(SpdReferralCase.status)
        .order_by(SpdReferralCase.status).all()
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

    「同源」两处原先没做到（P2-529）：版本取编号最大的那一版、不看生效日期——下个月才生效的新口径（连同它的名称与
    公式）印进本月报告，与本期正式考核分对不上；按团队 / 医师 / 村医计分的指标也按机构汇总出一个数，那是任何一份
    考核分里都没有的数。现在版本与计分共用 `effective_versions`，本期没生效的写明「尚未生效」；不按机构计分的指标
    写明不能引用、不出数。
    """
    from ..formula import FormulaError, evaluate as eval_formula
    from .routers.assess import collect_metrics, effective_versions

    code = section.get("indicator_code", "")
    # 这里的 period 是模板的频率关键字（daily/weekly/monthly），考核取数要的是
    # 具体周期值（2026-08 / 2026-Q3 / 2026）；段落可用 "period" 显式指定，
    # 否则按当月取——日报/周报看的也是本月累计口径
    period_value = section.get("period") or clock.today().strftime("%Y-%m")
    try:
        chosen, not_yet = effective_versions(db, [code], period_value)
    except ValueError as exc:
        return {**_head(section, "text"), "note": f"段落的考核周期写错了：{exc}"}
    indicator = chosen.get(code)
    if indicator is None:
        return {**_head(section, "text"),
                "note": f"指标 {code} 在本期（{period_value}）尚未生效" if code in not_yet
                else f"指标 {code} 不存在或已停用"}
    if org_id is None:
        return {**_head(section, "text"),
                "note": "指标段落需要报告绑定机构（考核口径按对象取数）"}
    if indicator.object_type != "org":
        return {**_head(section, "text"),
                "note": f"指标 {code} 按{_OBJECT_TYPE_NAMES.get(indicator.object_type, indicator.object_type)}"
                        "计分，报告按机构出、不能引用：按机构汇总出来的数没有对应的考核分"}
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
        "indicator_code": indicator.code, "version": indicator.version,
    }


for _key, _fn, _name in (
    ("summary", _summary, "总体概览"), ("todo", _todo, "待办任务"), ("alert", _alert, "超期预警"),
    ("workload", _workload, "服务工作量"),
    # 这两个是同一张随访完成趋势图：种子的周报叫「服务质量」、月报叫「运行趋势」，两个码都得留着
    ("quality", _followup_trend, "服务质量"), ("trend", _followup_trend, "运行趋势"),
    ("score", _score, "考核排名"), ("screening", _screening, "筛查情况"), ("referral", _referral, "转诊闭环"),
    ("points", _points, "村医积分"), ("indicator", _indicator, "考核指标"),
):
    register_section(_key, _fn, _name)
