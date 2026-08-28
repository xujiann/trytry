"""全域慢专病 · 各端工作台与区域统计。

招标文件把同一批数据按九个使用者的视角切了九遍——平台管理端看配置健康度，
卫健管理端看区域运行，专家端看临床质量，中心端看调度，团队看执行，
医生移动端看"今天该我干什么"。**数据同源、视角不同**，所以这里只做聚合，
不新增任何写接口：一个端的数字与另一个端对不上，是这类系统最伤信任的问题。

每个工作台一个端点，返回该端首屏要的全部卡片，而不是让前端并发拉十个接口——
移动端在乡镇的网络下，十个往返和一个往返是完全不同的体验。
"""
from typing import Any

from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...database import get_db
from ...deps import get_current_user, require_roles, resolve_business_date, row_dict
from ..platform import Organization, Patient, User
from ..models import (
    SpdAssessment,
    SpdCandidate,
    SpdCaseReport,
    SpdCenter,
    SpdConsult,
    SpdDataSource,
    SpdEnrollment,
    SpdFollowupRecord,
    SpdIntervention,
    SpdLifecycleEvent,
    SpdMeasurement,
    SpdPathInstance,
    SpdPathTemplate,
    SpdProgram,
    SpdRevisit,
    SpdReferralCase,
    SpdScale,
    SpdScore,
    SpdScreening,
    SpdServiceApply,
    SpdTask,
    SpdTeam,
    SpdTeamMember,
    SpdVillageDoctor,
)
from ..service import sweep_overdue
from ...visibility import stats_org_ids, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·工作台"],
    dependencies=[Depends(get_current_user)],
)

OPEN_STATUSES = ("pending", "claimed", "doing", "submitted", "overdue")


def _scope(db: Session, user: User, org_id: int | None, stats: bool = True) -> list[int] | None:
    """本次统计要看哪些机构：显式指定优先（仍受可见性约束），否则按角色范围。"""
    allowed = stats_org_ids(db, user) if stats else visible_org_ids(db, user)
    if org_id is None:
        return allowed
    if allowed is not None and org_id not in allowed:
        return []
    return [org_id]


def _apply_scope(query, column, orgs: list[int] | None):
    if orgs is None:
        return query
    return query.filter(column.in_(orgs or [0]))


def _enroll_stats(db: Session, orgs: list[int] | None, program_code: str = "") -> dict:
    query = _apply_scope(db.query(SpdEnrollment), SpdEnrollment.org_id, orgs)
    if program_code:
        query = query.filter(SpdEnrollment.program_code == program_code)
    active = query.filter(SpdEnrollment.status == "active")
    month_start = date.today().replace(day=1).isoformat()
    return {
        "enrolled": active.count(),
        "high_risk": active.filter(
            SpdEnrollment.risk_level.in_(["high", "very_high"])
        ).count(),
        "new_this_month": query.filter(
            SpdEnrollment.created_at >= f"{month_start} 00:00:00"
        ).count(),
        "archived": active.filter(SpdEnrollment.archived.is_(True)).count(),
        "by_risk": dict(
            active.with_entities(SpdEnrollment.risk_level, func.count(SpdEnrollment.id))
            .group_by(SpdEnrollment.risk_level).all()
        ),
        "by_program": dict(
            active.with_entities(SpdEnrollment.program_code, func.count(SpdEnrollment.id))
            .group_by(SpdEnrollment.program_code).all()
        ),
        "by_status": dict(
            query.with_entities(SpdEnrollment.status, func.count(SpdEnrollment.id))
            .group_by(SpdEnrollment.status).all()
        ),
    }


def _task_stats(
    db: Session, orgs: list[int] | None, assignee_id: int | None = None,
    program_code: str = "", today: date | None = None,
) -> dict:
    today = today or date.today()
    query = _apply_scope(db.query(SpdTask), SpdTask.org_id, orgs)
    if assignee_id is not None:
        query = query.filter(SpdTask.assignee_id == assignee_id)
    if program_code:
        query = query.filter(SpdTask.program_code == program_code)
    open_query = query.filter(SpdTask.status.in_(OPEN_STATUSES))
    return {
        "open": open_query.count(),
        "overdue": query.filter(SpdTask.status == "overdue").count(),
        "due_today": open_query.filter(SpdTask.due_date == today.isoformat()).count(),
        "escalated": query.filter(
            SpdTask.escalated.is_(True), SpdTask.status.in_(OPEN_STATUSES)
        ).count(),
        "done_total": query.filter(SpdTask.status == "done").count(),
        "by_type": dict(
            open_query.with_entities(SpdTask.task_type, func.count(SpdTask.id))
            .group_by(SpdTask.task_type).all()
        ),
    }


def _followup_stats(db: Session, orgs: list[int] | None, today: date | None = None) -> dict:
    today = today or date.today()
    query = _apply_scope(db.query(SpdFollowupRecord), SpdFollowupRecord.org_id, orgs)
    total = query.count()
    done = query.filter(SpdFollowupRecord.status == "done").count()
    return {
        "total": total, "done": done,
        "completion_rate": round(done / total * 100, 1) if total else 0.0,
        "overdue": query.filter(
            SpdFollowupRecord.status == "planned",
            SpdFollowupRecord.planned_at < today.isoformat(),
        ).count(),
        "abnormal": query.filter(
            SpdFollowupRecord.abnormal_level.in_(["mid", "high"])
        ).count(),
    }


def _referral_stats(db: Session, orgs: list[int] | None) -> dict:
    query = db.query(SpdReferralCase)
    if orgs is not None:
        query = query.filter(
            SpdReferralCase.initiator_org_id.in_(orgs or [0])
            | SpdReferralCase.current_org_id.in_(orgs or [0])
        )
    by_status = row_dict(
        query.with_entities(SpdReferralCase.status, func.count(SpdReferralCase.id))
        .group_by(SpdReferralCase.status).all()
    )
    total = sum(by_status.values())
    denominator = total - by_status.get("withdrawn", 0) - by_status.get("rejected", 0)
    return {
        "total": total,
        "open": total - sum(
            by_status.get(s, 0) for s in ("closed", "rejected", "withdrawn")
        ),
        "closed": by_status.get("closed", 0),
        "closure_rate": round(by_status.get("closed", 0) / denominator * 100, 1)
        if denominator else 0.0,
        "effective_visits": query.filter(SpdReferralCase.effective_visit.is_(True)).count(),
        "by_status": by_status,
    }


def _path_stats(db: Session, orgs: list[int] | None) -> dict:
    enroll_ids = [
        e.id for e in _apply_scope(db.query(SpdEnrollment), SpdEnrollment.org_id, orgs).all()
    ]
    query = db.query(SpdPathInstance).filter(
        SpdPathInstance.enrollment_id.in_(enroll_ids or [0])
    )
    total = query.count()
    completed = query.filter(SpdPathInstance.status == "completed").count()
    return {
        "total": total, "running": query.filter(SpdPathInstance.status == "running").count(),
        "completed": completed,
        "completion_rate": round(completed / total * 100, 1) if total else 0.0,
    }


# ============================================================ 响应契约
#
# 模型集中放在**所有端点之前**（`response_model=` 在导入时求值，与 `spd/config`
# 同一布局）。字段与顺序**精确镜像**各 handler 的当前出参——治理不得改响应字节
# （CLAUDE.md 第 7 条），逐字节取证见 tests/test_spd_workbench_contract.py 与
# 套件级捕获（tests/capture_plugin.py）。
#
# 三条口径贯穿全部模型：
# - `by_xxx` 分组字典的**键随数据而变**（风险等级/病种码/任务类型/状态），故是宽键
#   dict；其中 `by_org` / `team_patients` 的键是机构/团队 **id（int）**——JSON 里
#   必然是字符串键，但契约按真实键型 `dict[int, int]` 声明，序列化结果不变；
# - 各类比率（completion_rate 等）**恒为 float**：有数据走除法、无数据兜底 `0.0`，
#   两条分支都是 float；
# - 时间戳在 handler 里已 `isoformat()` 成串，契约一律 `str`。


class SweepOut(BaseModel):
    """进工作台先跑一次超期扫描（service.sweep_overdue）的回执：本次置为超期的数量。"""

    overdue: int
    escalated: int
    revisits: int
    followups: int


class EnrollStatsOut(BaseModel):
    enrolled: int
    high_risk: int
    new_this_month: int
    archived: int
    by_risk: dict[str, int]
    by_program: dict[str, int]
    by_status: dict[str, int]


class TaskStatsOut(BaseModel):
    open: int
    overdue: int
    due_today: int
    escalated: int
    done_total: int
    by_type: dict[str, int]


class FollowupStatsOut(BaseModel):
    total: int
    done: int
    completion_rate: float
    overdue: int
    abnormal: int


class ReferralStatsOut(BaseModel):
    total: int
    open: int
    closed: int
    closure_rate: float
    effective_visits: int
    by_status: dict[str, int]


class PathStatsOut(BaseModel):
    total: int
    running: int
    completed: int
    completion_rate: float


class AdminAlertsOut(BaseModel):
    overdue_tasks: int
    overdue_followups: int
    pending_review_screenings: int
    pending_applies: int
    pending_migrations: int
    swept: SweepOut


class TrackOut(BaseModel):
    programs: int
    enrolled: int


class ParallelTracksOut(BaseModel):
    chronic: TrackOut
    specialty: TrackOut


class ConfigHealthOut(BaseModel):
    programs: int
    active_programs: int
    # 没配纳入规则的病种**码**清单——不是数量：管理端要点名整改
    programs_without_rules: list[str]
    published_paths: int
    draft_paths: int
    published_scales: int
    teams: int
    village_doctors: int


class DataSourceHealthOut(BaseModel):
    total: int
    failed: int
    delayed: int
    stale_over_24h: int
    # Float 列的均值，round 后仍是 float（无数据源时兜底 0.0）
    avg_success_rate: float


class AdminWorkbenchOut(BaseModel):
    alerts: AdminAlertsOut
    parallel_tracks: ParallelTracksOut
    config_health: ConfigHealthOut
    data_sources: DataSourceHealthOut
    enrollment: EnrollStatsOut
    tasks: TaskStatsOut


class HcCoreOut(BaseModel):
    registered_patients: int
    screened: int
    suspect: int
    candidates: int
    enrolled: int
    self_managed: int
    service_persons: int
    service_times: int
    screening_conversion_rate: float
    updated_at: str


class LevelSliceOut(BaseModel):
    orgs: int
    enrolled: int
    teams: int


class HcCenterOut(BaseModel):
    id: int
    code: str
    name: str
    program_code: str
    status: str
    orgs: int
    teams: int


class HcScoreOut(BaseModel):
    object_name: str
    period: str
    # Float 列：整数分也是 100.0
    total_score: float
    rank: int


class HealthCommissionWorkbenchOut(BaseModel):
    core: HcCoreOut
    enrollment: EnrollStatsOut
    tasks: TaskStatsOut
    followups: FollowupStatsOut
    referrals: ReferralStatsOut
    paths: PathStatsOut
    # 键是层级中文名（县级/乡级/村级），由 handler 的 level_names 映射而来
    by_level: dict[str, LevelSliceOut]
    centers: list[HcCenterOut]
    scores: list[HcScoreOut]


class RegionMeasurementsOut(BaseModel):
    total: int
    normal: int


class RegionStatsOut(BaseModel):
    total: int
    by_program: dict[str, int]
    by_risk: dict[str, int]
    by_stage: dict[str, int]
    # 键是机构 id（int）——JSON 序列化成字符串键，与裸 dict 的输出一致
    by_org: dict[int, int]
    age_distribution: dict[str, int]
    gender_distribution: dict[str, int]
    referrals: ReferralStatsOut
    paths: PathStatsOut
    followups: FollowupStatsOut
    measurements: RegionMeasurementsOut


class ProgramCoverageOut(BaseModel):
    program_code: str
    program_name: str
    category: str
    version: str
    has_include_rules: bool
    stages: int
    path_templates: int
    published_paths: int
    scales: int
    enrolled: int


class ExpertCenterOut(BaseModel):
    id: int
    name: str
    program_code: str
    lead_dept: str
    status: str
    version: str


class AssessmentOverviewOut(BaseModel):
    total: int
    by_risk: dict[str, int]


class ExpertWorkbenchOut(BaseModel):
    programs: list[ProgramCoverageOut]
    centers: list[ExpertCenterOut]
    enrollment: EnrollStatsOut
    paths: PathStatsOut
    referrals: ReferralStatsOut
    assessments: AssessmentOverviewOut
    org_coverage: int


class CenterTodoOut(BaseModel):
    mine: TaskStatsOut
    all: TaskStatsOut
    unassigned: int
    swept: SweepOut


class CandidatePoolOut(BaseModel):
    suspect: int
    target: int
    unassigned: int
    excluded: int
    pending_review: int


class MonthlyOut(BaseModel):
    new_enrollments: int
    done_tasks: int


class LifecycleOut(BaseModel):
    dead: int
    migrated: int
    excluded: int
    pending_migrations: int
    recalling: int


class CaseReportsPendingOut(BaseModel):
    pending: int


class CenterWorkbenchOut(BaseModel):
    todo: CenterTodoOut
    pool: CandidatePoolOut
    enrollment: EnrollStatsOut
    monthly: MonthlyOut
    referrals: ReferralStatsOut
    lifecycle: LifecycleOut
    case_reports: CaseReportsPendingOut
    teams: int


class TeamBriefOut(BaseModel):
    id: int
    name: str
    level: str
    org_id: int
    program_codes: list[str]


class TeamPatientsOut(BaseModel):
    managed: int
    new_this_month: int
    high_risk: int
    by_risk: dict[str, int]


class TeamPlansOut(BaseModel):
    pending_assess: int
    pending_target: int
    pending_path: int
    due_followups: int
    due_revisits: int


class TeamAlertsOut(BaseModel):
    abnormal_measure: int
    referrals: int
    recall: int
    dead: int


class PackagesOut(BaseModel):
    bound: int
    total_items: int
    used_items: int
    usage_rate: float


class TeamWorkbenchOut(BaseModel):
    """三个角色共用端点的**条件键**：case_manager 多 packages+consults、expert 多
    team_patients+paths、member 多 interventions。键**整个不出现**（配
    `response_model_exclude_unset=True`），不是 null——声明成带默认值的普通可选
    字段会给每个响应注入 null，即改字节（docs/接口标准与治理.md 陷阱二）。"""

    role: str
    teams: list[TeamBriefOut]
    patients: TeamPatientsOut
    tasks: TaskStatsOut
    plans: TeamPlansOut
    alerts: TeamAlertsOut
    # —— 以下为角色条件键，声明顺序 = 各角色分支的写入顺序 ——
    packages: PackagesOut | None = None
    consults: int | None = None
    # 键是团队 id（int）
    team_patients: dict[int, int] | None = None
    paths: PathStatsOut | None = None
    interventions: int | None = None


class MobileTeamOut(BaseModel):
    id: int
    name: str
    level: str


class MobileUserOut(BaseModel):
    id: int
    name: str
    role: str
    org_id: int | None
    member_roles: list[str]
    teams: list[MobileTeamOut]
    is_village_doctor: bool
    village: str
    township: str


class MobileCalendarOut(BaseModel):
    today: str
    followups: int
    revisits: int
    tasks: int


class MobileReferralsOut(BaseModel):
    pending_review: int
    pending_accept: int
    pending_receive: int
    mine: int
    overdue: int


class MobilePatientsOut(BaseModel):
    mine: int
    village: int
    org: int


class MobileAlertsOut(BaseModel):
    escalated_tasks: int
    case_reports: int
    high_risk_screenings: int


class PointsOut(BaseModel):
    balance: int
    earned: int


class PerformanceOut(BaseModel):
    period: str
    # Float 列
    total_score: float
    rank: int
    # JSON 列：分项明细行，形状由考核计分器决定
    detail: list[dict[str, Any]]


class DoctorMobileWorkbenchOut(BaseModel):
    user: MobileUserOut
    todo: TaskStatsOut
    calendar: MobileCalendarOut
    referrals: MobileReferralsOut
    patients: MobilePatientsOut
    alerts: MobileAlertsOut
    points: PointsOut
    # 没有考核结果时为 null——键永远在，不是条件键
    performance: PerformanceOut | None


class CatalogProgramOut(BaseModel):
    code: str
    name: str
    category: str
    # JSON 列：阶段定义 [{"key":..,"name":..}]，字段随配置而变
    stages: list[dict[str, Any]]
    active: bool


class CatalogTeamOut(BaseModel):
    id: int
    name: str
    level: str
    org_id: int


class CatalogScaleOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    program_code: str


class CatalogCenterOut(BaseModel):
    id: int
    code: str
    name: str
    program_code: str


class CatalogPathTemplateOut(BaseModel):
    id: int
    code: str
    name: str
    program_id: int
    scene: str
    status: str


class SpdCatalogOut(BaseModel):
    """带 Spd 前缀是去重名：`rules.CatalogOut` 已占用短名，OpenAPI 遇重名会把
    双方都改写成 `app__routers__...` 的长限定名，连带弄乱对方的规格书。"""

    programs: list[CatalogProgramOut]
    teams: list[CatalogTeamOut]
    scales: list[CatalogScaleOut]
    centers: list[CatalogCenterOut]
    path_templates: list[CatalogPathTemplateOut]


# ============================================================ 平台管理端


@router.get("/workbench/admin", response_model=AdminWorkbenchOut,
            dependencies=[Depends(require_roles("director"))])
def admin_workbench(
    today: str | None = None, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """平台管理端（运行中枢）工作台（#1/#15/#21）。

    这个端看的是**配置与运行健康度**：超期任务弹窗、数据源接入状态、
    慢病与专病两条业务线的并行运行情况、配置项的完备程度。
    """
    business_day = resolve_business_date(today)
    swept = sweep_overdue(db, business_day)
    db.commit()
    orgs = _scope(db, user, None)

    programs = db.query(SpdProgram).all()
    chronic = [p for p in programs if p.category == "chronic"]
    specialty = [p for p in programs if p.category == "specialty"]
    sources = db.query(SpdDataSource).filter(SpdDataSource.active.is_(True)).all()
    stale_cutoff = now_naive() - timedelta(hours=24)

    return {
        "alerts": {
            "overdue_tasks": _apply_scope(
                db.query(SpdTask), SpdTask.org_id, orgs
            ).filter(SpdTask.status == "overdue").count(),
            "overdue_followups": _apply_scope(
                db.query(SpdFollowupRecord), SpdFollowupRecord.org_id, orgs
            ).filter(
                SpdFollowupRecord.status == "planned",
                SpdFollowupRecord.planned_at < business_day.isoformat(),
            ).count(),
            "pending_review_screenings": _apply_scope(
                db.query(SpdScreening), SpdScreening.org_id, orgs
            ).filter(
                SpdScreening.result == "suspect", SpdScreening.reviewed.is_(False)
            ).count(),
            "pending_applies": db.query(SpdServiceApply).filter(
                SpdServiceApply.status == "pending"
            ).count(),
            "pending_migrations": db.query(SpdLifecycleEvent).filter(
                SpdLifecycleEvent.event == "migrate",
                SpdLifecycleEvent.confirmed.is_(False),
            ).count(),
            "swept": swept,
        },
        "parallel_tracks": {
            "chronic": {
                "programs": len(chronic),
                "enrolled": _apply_scope(
                    db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
                ).filter(
                    SpdEnrollment.status == "active",
                    SpdEnrollment.program_code.in_([p.code for p in chronic] or [""]),
                ).count(),
            },
            "specialty": {
                "programs": len(specialty),
                "enrolled": _apply_scope(
                    db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
                ).filter(
                    SpdEnrollment.status == "active",
                    SpdEnrollment.program_code.in_([p.code for p in specialty] or [""]),
                ).count(),
            },
        },
        "config_health": {
            "programs": len(programs),
            "active_programs": sum(1 for p in programs if p.active),
            "programs_without_rules": [
                p.code for p in programs if not (p.include_rules or [])
            ],
            "published_paths": db.query(SpdPathTemplate).filter(
                SpdPathTemplate.status == "published"
            ).count(),
            "draft_paths": db.query(SpdPathTemplate).filter(
                SpdPathTemplate.status == "draft"
            ).count(),
            "published_scales": db.query(SpdScale).filter(
                SpdScale.status == "published"
            ).count(),
            "teams": db.query(SpdTeam).filter(SpdTeam.active.is_(True)).count(),
            "village_doctors": db.query(SpdVillageDoctor).filter(
                SpdVillageDoctor.active.is_(True)
            ).count(),
        },
        "data_sources": {
            "total": len(sources),
            "failed": sum(1 for s in sources if s.status == "failed"),
            "delayed": sum(1 for s in sources if s.status == "delayed"),
            "stale_over_24h": sum(
                1 for s in sources if s.last_sync_at is None or s.last_sync_at < stale_cutoff
            ),
            "avg_success_rate": round(
                sum(s.success_rate for s in sources) / len(sources), 2
            ) if sources else 0.0,
        },
        "enrollment": _enroll_stats(db, orgs),
        "tasks": _task_stats(db, orgs, today=business_day),
    }


# ============================================================ 卫健管理端


@router.get("/workbench/health-commission", response_model=HealthCommissionWorkbenchOut,
            dependencies=[Depends(require_roles("director"))])
def health_commission_workbench(
    program_code: str = "",
    org_id: int | None = None,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """卫健管理端（决策监管中枢）工作台（#1~#3/#10/#11）。

    区域视角：注册/入组/纳管/服务人次核心指标、按机构与病种的结构分布、
    三级机构服务能力、专病中心运行、考核结果概览。
    """
    business_day = resolve_business_date(today)
    orgs = _scope(db, user, org_id)
    enroll = _enroll_stats(db, orgs, program_code)
    tasks = _task_stats(db, orgs, program_code=program_code, today=business_day)

    org_rows = db.query(Organization).all()
    level_names = {"county": "县级", "township": "乡级", "village": "村级"}
    by_level: dict[str, dict] = {
        key: {"orgs": 0, "enrolled": 0, "teams": 0} for key in level_names
    }
    org_level = {o.id: o.level for o in org_rows}
    for org in org_rows:
        if org.level in by_level:
            by_level[org.level]["orgs"] += 1
    for org_key, count in (
        _apply_scope(db.query(SpdEnrollment), SpdEnrollment.org_id, orgs)
        .filter(SpdEnrollment.status == "active")
        .with_entities(SpdEnrollment.org_id, func.count(SpdEnrollment.id))
        .group_by(SpdEnrollment.org_id).all()
    ):
        level = org_level.get(org_key)
        if level in by_level:
            by_level[level]["enrolled"] += count
    for org_key, count in (
        db.query(SpdTeam.org_id, func.count(SpdTeam.id))
        .filter(SpdTeam.active.is_(True)).group_by(SpdTeam.org_id).all()
    ):
        level = org_level.get(org_key)
        if level in by_level:
            by_level[level]["teams"] += count

    screening_query = _apply_scope(db.query(SpdScreening), SpdScreening.org_id, orgs)
    screened = screening_query.count()
    suspect = screening_query.filter(SpdScreening.result == "suspect").count()
    centers = db.query(SpdCenter).all()

    return {
        "core": {
            "registered_patients": db.query(Patient).count(),
            "screened": screened,
            "suspect": suspect,
            "candidates": _apply_scope(
                db.query(SpdCandidate), SpdCandidate.org_id, orgs
            ).filter(SpdCandidate.status == "target").count(),
            "enrolled": enroll["enrolled"],
            "self_managed": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(
                SpdEnrollment.status == "active", SpdEnrollment.risk_level == "low"
            ).count(),
            "service_persons": db.query(SpdTask.patient_id).filter(
                SpdTask.status == "done"
            ).distinct().count(),
            "service_times": db.query(SpdTask).filter(SpdTask.status == "done").count(),
            "screening_conversion_rate": round(suspect / screened * 100, 1) if screened else 0.0,
            "updated_at": now_naive().isoformat(),
        },
        "enrollment": enroll,
        "tasks": tasks,
        "followups": _followup_stats(db, orgs, business_day),
        "referrals": _referral_stats(db, orgs),
        "paths": _path_stats(db, orgs),
        "by_level": {level_names[k]: v for k, v in by_level.items()},
        "centers": [
            {"id": c.id, "code": c.code, "name": c.name, "program_code": c.program_code,
             "status": c.status, "orgs": len(c.org_ids or []),
             "teams": len(c.team_ids or [])}
            for c in centers
        ],
        "scores": [
            {"object_name": s.object_name, "period": s.period,
             "total_score": s.total_score, "rank": s.rank}
            for s in db.query(SpdScore).order_by(SpdScore.rank).limit(20).all()
        ],
    }


@router.get("/stats/region", response_model=RegionStatsOut)
def region_stats(
    program_code: str = "",
    org_id: int | None = None,
    period: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """区域慢专病结构分析（卫健端 #2）：患者结构、分级诊疗、路径执行、服务包。

    与工作台分开是因为这是**可下钻的分析页**，参数组合多、返回体大，
    塞进工作台会让首屏变慢。
    """
    orgs = _scope(db, user, org_id)
    enroll_query = _apply_scope(db.query(SpdEnrollment), SpdEnrollment.org_id, orgs).filter(
        SpdEnrollment.status == "active"
    )
    if program_code:
        enroll_query = enroll_query.filter(SpdEnrollment.program_code == program_code)
    enrollments = enroll_query.limit(20000).all()

    patients = {
        p.id: p
        for p in db.query(Patient).filter(
            Patient.id.in_([e.patient_id for e in enrollments] or [0])
        )
    }
    age_buckets = {"0-17": 0, "18-44": 0, "45-59": 0, "60-74": 0, "75+": 0, "未知": 0}
    gender = {"男": 0, "女": 0, "未知": 0}
    today = date.today()
    for enrollment in enrollments:
        patient = patients.get(enrollment.patient_id)
        if patient is None:
            age_buckets["未知"] += 1
            continue
        gender[patient.gender if patient.gender in gender else "未知"] += 1
        try:
            born = date.fromisoformat(patient.birth_date)
            age = today.year - born.year - (
                (today.month, today.day) < (born.month, born.day)
            )
        except (ValueError, TypeError):
            age_buckets["未知"] += 1
            continue
        key = ("0-17" if age < 18 else "18-44" if age < 45 else "45-59" if age < 60
               else "60-74" if age < 75 else "75+")
        age_buckets[key] += 1

    return {
        "total": len(enrollments),
        "by_program": dict(
            enroll_query.with_entities(
                SpdEnrollment.program_code, func.count(SpdEnrollment.id)
            ).group_by(SpdEnrollment.program_code).all()
        ),
        "by_risk": dict(
            enroll_query.with_entities(
                SpdEnrollment.risk_level, func.count(SpdEnrollment.id)
            ).group_by(SpdEnrollment.risk_level).all()
        ),
        "by_stage": dict(
            enroll_query.with_entities(
                SpdEnrollment.stage, func.count(SpdEnrollment.id)
            ).group_by(SpdEnrollment.stage).all()
        ),
        "by_org": dict(
            enroll_query.with_entities(
                SpdEnrollment.org_id, func.count(SpdEnrollment.id)
            ).group_by(SpdEnrollment.org_id).all()
        ),
        "age_distribution": age_buckets,
        "gender_distribution": gender,
        "referrals": _referral_stats(db, orgs),
        "paths": _path_stats(db, orgs),
        "followups": _followup_stats(db, orgs),
        "measurements": {
            "total": db.query(SpdMeasurement).count(),
            "normal": db.query(SpdMeasurement).filter(
                SpdMeasurement.level == "normal"
            ).count(),
        },
    }


# ============================================================ 专病专家端


@router.get("/workbench/expert", response_model=ExpertWorkbenchOut,
            dependencies=[Depends(require_roles("doctor", "director"))])
def expert_workbench(
    program_code: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """专病专家端（临床指导中枢）工作台（#4）。

    专家关心的是**标准落地得怎么样**：目标/路径/量表配了没有、基层执行到哪一步、
    分中心运行状态、年龄分层与签约纳管的大盘。
    """
    orgs = _scope(db, user, None)
    programs = db.query(SpdProgram).filter(SpdProgram.active.is_(True)).all()
    if program_code:
        programs = [p for p in programs if p.code == program_code]

    coverage = []
    for program in programs:
        templates = (
            db.query(SpdPathTemplate)
            .filter(SpdPathTemplate.program_id == program.id)
            .all()
        )
        coverage.append({
            "program_code": program.code, "program_name": program.name,
            "category": program.category, "version": program.version,
            "has_include_rules": bool(program.include_rules),
            "stages": len(program.stages or []),
            "path_templates": len(templates),
            "published_paths": sum(1 for t in templates if t.status == "published"),
            "scales": db.query(SpdScale).filter(
                SpdScale.program_code == program.code, SpdScale.status == "published"
            ).count(),
            "enrolled": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(
                SpdEnrollment.program_code == program.code,
                SpdEnrollment.status == "active",
            ).count(),
        })

    return {
        "programs": coverage,
        "centers": [
            {"id": c.id, "name": c.name, "program_code": c.program_code,
             "lead_dept": c.lead_dept, "status": c.status, "version": c.version}
            for c in db.query(SpdCenter).all()
        ],
        "enrollment": _enroll_stats(db, orgs, program_code),
        "paths": _path_stats(db, orgs),
        "referrals": _referral_stats(db, orgs),
        "assessments": {
            "total": db.query(SpdAssessment).count(),
            "by_risk": row_dict(
                db.query(SpdAssessment.risk_level, func.count(SpdAssessment.id))
                .group_by(SpdAssessment.risk_level).all()
            ),
        },
        "org_coverage": _apply_scope(
            db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
        ).with_entities(SpdEnrollment.org_id).distinct().count(),
    }


# ============================================================ 全程管理中心端


@router.get("/workbench/center", response_model=CenterWorkbenchOut)
def center_workbench(
    program_code: str = "",
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """全程管理中心端（统筹调度中枢）工作台（#1/#10/#13）。

    调度视角：统一待办入口 + 目标池待分发 + 在途转诊 + 超期升级 + 死亡/迁出待确认。
    """
    business_day = resolve_business_date(today)
    swept = sweep_overdue(db, business_day)
    db.commit()
    orgs = _scope(db, user, None, stats=False)
    month_start = date.today().replace(day=1).isoformat()

    return {
        "todo": {
            "mine": _task_stats(db, orgs, assignee_id=user.id, program_code=program_code,
                                today=business_day),
            "all": _task_stats(db, orgs, program_code=program_code, today=business_day),
            "unassigned": _apply_scope(db.query(SpdTask), SpdTask.org_id, orgs).filter(
                SpdTask.assignee_id.is_(None), SpdTask.status == "pending"
            ).count(),
            "swept": swept,
        },
        "pool": {
            "suspect": _apply_scope(
                db.query(SpdCandidate), SpdCandidate.org_id, orgs
            ).filter(SpdCandidate.status == "suspect").count(),
            "target": _apply_scope(
                db.query(SpdCandidate), SpdCandidate.org_id, orgs
            ).filter(SpdCandidate.status == "target").count(),
            "unassigned": _apply_scope(
                db.query(SpdCandidate), SpdCandidate.org_id, orgs
            ).filter(
                SpdCandidate.status == "target", SpdCandidate.team_id.is_(None)
            ).count(),
            "excluded": _apply_scope(
                db.query(SpdCandidate), SpdCandidate.org_id, orgs
            ).filter(SpdCandidate.status == "excluded").count(),
            "pending_review": _apply_scope(
                db.query(SpdScreening), SpdScreening.org_id, orgs
            ).filter(
                SpdScreening.result == "suspect", SpdScreening.reviewed.is_(False)
            ).count(),
        },
        "enrollment": _enroll_stats(db, orgs, program_code),
        "monthly": {
            "new_enrollments": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.created_at >= f"{month_start} 00:00:00").count(),
            "done_tasks": _apply_scope(db.query(SpdTask), SpdTask.org_id, orgs).filter(
                SpdTask.status == "done", SpdTask.finished_at >= f"{month_start} 00:00:00"
            ).count(),
        },
        "referrals": _referral_stats(db, orgs),
        "lifecycle": {
            "dead": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.status == "dead").count(),
            "migrated": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.status == "migrated").count(),
            "excluded": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.status == "excluded").count(),
            "pending_migrations": db.query(SpdLifecycleEvent).filter(
                SpdLifecycleEvent.event == "migrate",
                SpdLifecycleEvent.confirmed.is_(False),
            ).count(),
            "recalling": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.status == "recalled").count(),
        },
        "case_reports": {
            "pending": _apply_scope(
                db.query(SpdCaseReport), SpdCaseReport.org_id, orgs
            ).filter(SpdCaseReport.status == "pending").count(),
        },
        "teams": db.query(SpdTeam).filter(SpdTeam.active.is_(True)).count(),
    }


# ============================================================ 服务团队端（专家 / 成员 / 个案管理师）


def _my_team_ids(db: Session, user: User) -> list[int]:
    return [
        m.team_id
        for m in db.query(SpdTeamMember)
        .filter(SpdTeamMember.user_id == user.id, SpdTeamMember.active.is_(True))
        .all()
    ]


@router.get("/workbench/team", response_model=TeamWorkbenchOut,
            response_model_exclude_unset=True)
def team_workbench(
    role: str = Query(default="member", pattern="^(expert|member|case_manager)$"),
    program_code: str = "",
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """服务团队三个端共用一个工作台端点，用 `role` 区分侧重。

    专家端看团队整体与路径分配，成员端看本人待办与患者规模，
    个案管理师端看在管患者、服务包消耗与待衔接事项——三者的数据源完全相同，
    做成三个端点只会让"同一个数字在三处算法不同"。
    """
    business_day = resolve_business_date(today)
    orgs = _scope(db, user, None, stats=False)
    team_ids = _my_team_ids(db, user)

    mine_query = db.query(SpdEnrollment).filter(SpdEnrollment.status == "active")
    if role == "case_manager":
        mine_query = mine_query.filter(SpdEnrollment.manager_user_id == user.id)
    elif role == "member":
        mine_query = mine_query.filter(SpdEnrollment.doctor_user_id == user.id)
    else:
        mine_query = mine_query.filter(SpdEnrollment.team_id.in_(team_ids or [0]))
    if program_code:
        mine_query = mine_query.filter(SpdEnrollment.program_code == program_code)

    month_start = date.today().replace(day=1).isoformat()
    my_patients = [e.patient_id for e in mine_query.limit(5000).all()]

    # 待评估 / 待入径都用一条 IN 查询取"已有的"，再在内存里做差集。
    # 之前是逐患者 first()——在管 5000 人时进一次工作台要打一万条 SQL
    assessed = {
        pid for (pid,) in db.query(SpdAssessment.patient_id)
        .filter(SpdAssessment.patient_id.in_(my_patients or [0]))
        .distinct().all()
    }
    pending_assess = [pid for pid in my_patients if pid not in assessed]
    pathed = {
        pid for (pid,) in db.query(SpdEnrollment.patient_id)
        .join(SpdPathInstance, SpdPathInstance.enrollment_id == SpdEnrollment.id)
        .filter(SpdEnrollment.patient_id.in_(my_patients or [0]))
        .distinct().all()
    }

    out: dict[str, Any] = {
        "role": role,
        "teams": [
            {"id": t.id, "name": t.name, "level": t.level, "org_id": t.org_id,
             "program_codes": t.program_codes or []}
            for t in db.query(SpdTeam).filter(SpdTeam.id.in_(team_ids or [0])).all()
        ],
        "patients": {
            "managed": mine_query.count(),
            "new_this_month": mine_query.filter(
                SpdEnrollment.created_at >= f"{month_start} 00:00:00"
            ).count(),
            "high_risk": mine_query.filter(
                SpdEnrollment.risk_level.in_(["high", "very_high"])
            ).count(),
            "by_risk": row_dict(
                mine_query.with_entities(
                    SpdEnrollment.risk_level, func.count(SpdEnrollment.id)
                ).group_by(SpdEnrollment.risk_level).all()
            ),
        },
        "tasks": _task_stats(db, orgs, assignee_id=user.id, program_code=program_code,
                             today=business_day),
        "plans": {
            "pending_assess": len(pending_assess),
            "pending_target": mine_query.filter(SpdEnrollment.stage == "").count(),
            "pending_path": sum(1 for pid in my_patients if pid not in pathed),
            "due_followups": db.query(SpdFollowupRecord).filter(
                SpdFollowupRecord.patient_id.in_(my_patients or [0]),
                SpdFollowupRecord.status == "planned",
                SpdFollowupRecord.planned_at <= business_day.isoformat(),
            ).count(),
            "due_revisits": db.query(SpdRevisit).filter(
                SpdRevisit.patient_id.in_(my_patients or [0]),
                SpdRevisit.status == "planned",
                SpdRevisit.plan_date <= business_day.isoformat(),
            ).count(),
        },
        "alerts": {
            "abnormal_measure": db.query(SpdMeasurement).filter(
                SpdMeasurement.patient_id.in_(my_patients or [0]),
                SpdMeasurement.level.in_(["high", "low"]),
                SpdMeasurement.measured_at >= f"{month_start} 00:00:00",
            ).count(),
            "referrals": db.query(SpdReferralCase).filter(
                SpdReferralCase.patient_id.in_(my_patients or [0]),
                SpdReferralCase.status.notin_(["closed", "rejected", "withdrawn"]),
            ).count(),
            "recall": mine_query.filter(SpdEnrollment.status == "recalled").count(),
            "dead": db.query(SpdEnrollment).filter(
                SpdEnrollment.patient_id.in_(my_patients or [0]),
                SpdEnrollment.status == "dead",
            ).count(),
        },
    }

    if role == "case_manager":
        from ..models import SpdPackageBinding

        bindings = (
            db.query(SpdPackageBinding)
            .join(SpdEnrollment, SpdEnrollment.id == SpdPackageBinding.enrollment_id)
            .filter(
                SpdEnrollment.manager_user_id == user.id,
                SpdPackageBinding.status == "bound",
            )
            .all()
        )
        total = sum(
            int(i.get("total", 0)) for b in bindings for i in (b.items or [])
        )
        used = sum(int(i.get("used", 0)) for b in bindings for i in (b.items or []))
        out["packages"] = {
            "bound": len(bindings), "total_items": total, "used_items": used,
            "usage_rate": round(used / total * 100, 1) if total else 0.0,
        }
        out["consults"] = db.query(SpdConsult).filter(
            SpdConsult.doctor_id == user.id, SpdConsult.status == "open"
        ).count()
    if role == "expert":
        out["team_patients"] = row_dict(
            db.query(SpdEnrollment.team_id, func.count(SpdEnrollment.id))
            .filter(
                SpdEnrollment.team_id.in_(team_ids or [0]),
                SpdEnrollment.status == "active",
            )
            .group_by(SpdEnrollment.team_id).all()
        )
        out["paths"] = _path_stats(db, orgs)
    if role == "member":
        out["interventions"] = db.query(SpdIntervention).filter(
            SpdIntervention.owner_id == user.id,
            SpdIntervention.status.in_(["planned", "doing"]),
        ).count()
    return out


# ============================================================ 医生移动端


@router.get("/workbench/doctor-mobile", response_model=DoctorMobileWorkbenchOut)
def doctor_mobile_workbench(
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """医生移动端工作台（#1/#2/#3/#19/#21）。

    按角色层级给不同的待办：村医看签约与随访，卫生院看审核与承接，
    县级看接收与督办，管理者看预警与绩效。角色从**团队成员身份**推出来，
    而不是从 `users.role`——同一个人在县医院是医师、在专病团队里是专家。
    """
    business_day = resolve_business_date(today)
    orgs = _scope(db, user, None, stats=False)
    team_ids = _my_team_ids(db, user)
    memberships = (
        db.query(SpdTeamMember)
        .filter(SpdTeamMember.user_id == user.id, SpdTeamMember.active.is_(True))
        .all()
    )
    member_roles = sorted({m.member_role for m in memberships})
    is_village = "village_doctor" in member_roles
    village_profile = (
        db.query(SpdVillageDoctor).filter(SpdVillageDoctor.user_id == user.id).first()
    )

    my_tasks = _task_stats(db, orgs, assignee_id=user.id, today=business_day)
    referral_query = db.query(SpdReferralCase).filter(
        SpdReferralCase.status.notin_(["closed", "rejected", "withdrawn"])
    )
    if orgs is not None:
        referral_query = referral_query.filter(
            SpdReferralCase.initiator_org_id.in_(orgs or [0])
            | SpdReferralCase.current_org_id.in_(orgs or [0])
        )

    out: dict[str, Any] = {
        "user": {
            "id": user.id, "name": user.full_name or user.username, "role": user.role,
            "org_id": user.org_id, "member_roles": member_roles,
            "teams": [
                {"id": t.id, "name": t.name, "level": t.level}
                for t in db.query(SpdTeam).filter(SpdTeam.id.in_(team_ids or [0])).all()
            ],
            "is_village_doctor": is_village,
            "village": village_profile.village if village_profile else "",
            "township": village_profile.township if village_profile else "",
        },
        "todo": my_tasks,
        "calendar": {
            "today": business_day.isoformat(),
            "followups": db.query(SpdFollowupRecord).filter(
                SpdFollowupRecord.executor_id == user.id,
                SpdFollowupRecord.planned_at == business_day.isoformat(),
            ).count(),
            "revisits": db.query(SpdRevisit).filter(
                SpdRevisit.doctor_user_id == user.id,
                SpdRevisit.plan_date == business_day.isoformat(),
            ).count(),
            "tasks": db.query(SpdTask).filter(
                SpdTask.assignee_id == user.id,
                SpdTask.due_date == business_day.isoformat(),
                SpdTask.status.in_(OPEN_STATUSES),
            ).count(),
        },
        "referrals": {
            "pending_review": referral_query.filter(
                SpdReferralCase.status.in_(["submitted", "station_reviewed"])
            ).count(),
            "pending_accept": referral_query.filter(
                SpdReferralCase.status == "township_reviewed"
            ).count(),
            "pending_receive": referral_query.filter(
                SpdReferralCase.status == "down_referred"
            ).count(),
            "mine": db.query(SpdReferralCase).filter(
                SpdReferralCase.initiator_id == user.id
            ).count(),
            "overdue": referral_query.filter(
                SpdReferralCase.created_at < now_naive() - timedelta(hours=48)
            ).count(),
        },
        "patients": {
            "mine": db.query(SpdEnrollment).filter(
                SpdEnrollment.doctor_user_id == user.id,
                SpdEnrollment.status == "active",
            ).count(),
            "village": db.query(SpdEnrollment).filter(
                SpdEnrollment.village_doctor_id == user.id,
                SpdEnrollment.status == "active",
            ).count() if is_village else 0,
            "org": _apply_scope(
                db.query(SpdEnrollment), SpdEnrollment.org_id, orgs
            ).filter(SpdEnrollment.status == "active").count(),
        },
        "alerts": {
            "escalated_tasks": _apply_scope(db.query(SpdTask), SpdTask.org_id, orgs).filter(
                SpdTask.escalated.is_(True), SpdTask.status.in_(OPEN_STATUSES)
            ).count(),
            "case_reports": _apply_scope(
                db.query(SpdCaseReport), SpdCaseReport.org_id, orgs
            ).filter(SpdCaseReport.status == "pending").count(),
            "high_risk_screenings": _apply_scope(
                db.query(SpdScreening), SpdScreening.org_id, orgs
            ).filter(
                SpdScreening.risk_level == "high", SpdScreening.reviewed.is_(False)
            ).count(),
        },
    }

    from ..models import SpdPointAccount

    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user.id).first()
    out["points"] = {
        "balance": account.balance if account else 0,
        "earned": account.earned if account else 0,
    }
    latest_score = (
        db.query(SpdScore)
        .filter(SpdScore.object_type.in_(["doctor", "village_doctor"]),
                SpdScore.object_id == user.id)
        .order_by(SpdScore.id.desc())
        .first()
    )
    out["performance"] = (
        {"period": latest_score.period, "total_score": latest_score.total_score,
         "rank": latest_score.rank, "detail": latest_score.detail or []}
        if latest_score else None
    )
    return out


# ============================================================ 病种与目录（各端共用的下拉数据）


@router.get("/catalog", response_model=SpdCatalogOut)
def catalog(db: Session = Depends(get_db)):
    """各端下拉框共用的目录数据：病种、团队、量表、服务包、专病中心。

    合成一个接口是有意的：每个端的筛选栏都要这五样，分开就是五次往返。
    """
    return {
        "programs": [
            {"code": p.code, "name": p.name, "category": p.category,
             "stages": p.stages or [], "active": p.active}
            for p in db.query(SpdProgram).order_by(SpdProgram.id).limit(100).all()
        ],
        "teams": [
            {"id": t.id, "name": t.name, "level": t.level, "org_id": t.org_id}
            for t in db.query(SpdTeam).filter(SpdTeam.active.is_(True))
            .order_by(SpdTeam.id).limit(300).all()
        ],
        "scales": [
            {"id": s.id, "code": s.code, "name": s.name, "category": s.category,
             "program_code": s.program_code}
            for s in db.query(SpdScale).filter(SpdScale.status == "published")
            .order_by(SpdScale.id).limit(200).all()
        ],
        "centers": [
            {"id": c.id, "code": c.code, "name": c.name, "program_code": c.program_code}
            for c in db.query(SpdCenter).order_by(SpdCenter.id).limit(100).all()
        ],
        "path_templates": [
            {"id": t.id, "code": t.code, "name": t.name, "program_id": t.program_id,
             "scene": t.scene, "status": t.status}
            for t in db.query(SpdPathTemplate)
            .filter(SpdPathTemplate.status == "published")
            .order_by(SpdPathTemplate.id).limit(300).all()
        ],
    }
