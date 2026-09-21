"""运行监控台（浙江省指南 #47）：运行环境概览、调用统计、节点状态。

限管理员。监控数据本身不敏感，但它会暴露内部路径、错误详情与实例拓扑，
对外开放没有好处。

采集口径与"进程内 vs 集群"的取舍见 `app/monitor.py` 顶部说明；
每个接口的响应都带 `scope` 字段，避免把单实例数据当成全局数据看。
"""
import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..clock import now_naive
from ..database import get_db
from ..deps import require_admin
from ..monitor import INSTANCE_ID, STARTED_AT, cluster_snapshot, metrics
from ..monitor import heartbeat as monitor_heartbeat
from ..monitor import known_instances
from ..models import JobRun, ScheduledJob
# 调度器把 next_run_at 存为 naive UTC；用 models.utcnow()（aware）去比会直接
# TypeError。这里复用调度器自己的时钟函数，口径永远跟着它走。
from ..state_store import _redis_client

router = APIRouter(prefix="/api/monitor", tags=["运行监控"], dependencies=[Depends(require_admin)])


# ============================================================ 响应契约
#
# 本模块的响应多为**条件形状**（探活成功/失败、配没配 Redis），
# 一律「可选字段 + response_model_exclude_unset」，字段顺序按各分支的
# 字面量排，使 `exclude_unset`（按**声明顺序**输出）逐字对上每一种。


class DatabaseProbeOut(BaseModel):
    """探活成功出 `{connected, latency_ms, dialect}`，失败出
    `{connected, error, dialect}`——`dialect` 两边都在且都在末尾，
    所以 `latency_ms`/`error` 夹在中间这个顺序能同时满足两条。
    """

    connected: bool
    latency_ms: float | None = None
    # 只回错误**类型**不回原文：连接串常带主机名与账号，不该进监控页
    error: str | None = None
    dialect: str


class RedisProbeOut(BaseModel):
    """三种分支：没配（note）、连上（latency_ms）、连不上（error）。"""

    configured: bool
    connected: bool
    note: str | None = None
    latency_ms: float | None = None
    error: str | None = None


class JobFailureOut(BaseModel):
    name: str
    at: str
    status: str
    message: str


class SchedulerOut(BaseModel):
    jobs_total: int
    jobs_enabled: int
    # 到点未跑：可能是调度线程死了，也可能是这一轮刚好还没轮到，只报事实
    overdue_jobs: list[str]
    recent_failures: list[JobFailureOut]


class OverviewOut(BaseModel):
    scope: str
    instance_id: str
    uptime_seconds: int
    environment: str
    database: DatabaseProbeOut
    redis: RedisProbeOut
    scheduler: SchedulerOut


class ModuleStatOut(BaseModel):
    module: str
    count: int
    avg_duration_ms: float


class ApiStatsOut(BaseModel):
    """`metrics.snapshot()` 的键 + 四个后补的键。

    配了 Redis 时 `snapshot.update(cluster)` **只覆盖同名键**（不改键序），
    随后多插一个 `counter_scope`；未配时没有这个键。所以它排在 `scope` 之后、
    `instance_id` 之前——正是两条分支各自的字面顺序。
    """

    total_requests: int
    avg_duration_ms: float
    # 三个宽键：状态类/状态码/慢请求与错误样本，取值都由流量决定
    by_status_class: dict[str, int]
    by_status_code: dict[int, int]
    top_modules: list[ModuleStatOut]
    # 明细样本仍为**本实例**（合并会抹掉"哪台机器慢"）
    slow_samples: list[dict]
    error_samples: list[dict]
    scope: str
    counter_scope: str | None = None
    instance_id: str
    slow_threshold_ms: float


class NodesOut(BaseModel):
    """未配 Redis 时出 `{scope, instance_id, instances(null), note}`，
    配了则只出 `{scope, instances}`——后者缺的两个键夹在中间，
    按这个声明顺序两条分支都对得上。
    """

    scope: str
    instance_id: str | None = None
    instances: list[dict] | None
    note: str | None = None


def _probe_database(db: Session) -> dict:
    start = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        return {
            "connected": True,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "dialect": db.bind.dialect.name if db.bind is not None else "",
        }
    except Exception as exc:  # pragma: no cover - 探活失败路径依赖外部故障
        # 只回错误类型不回原文：连接串常带主机名与账号，不该进监控页
        return {"connected": False, "error": type(exc).__name__, "dialect": ""}


def _probe_redis() -> dict:
    redis = _redis_client()
    if redis is None:
        # 没配 Redis 不是故障，是单实例部署的正常形态；说清后果即可
        return {
            "configured": False,
            "connected": False,
            "note": "未配置 Redis：登出黑名单、防爆破锁定、限流与任务抢锁均为进程内生效，"
                    "多实例部署必须配置",
        }
    start = time.perf_counter()
    try:
        redis.ping()
        return {
            "configured": True,
            "connected": True,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
        }
    except Exception as exc:  # pragma: no cover
        return {"configured": True, "connected": False, "error": type(exc).__name__}


@router.get("/overview", response_model=OverviewOut,
            response_model_exclude_unset=True)
def overview(db: Session = Depends(get_db)):
    """运行环境概览：版本、实例、启动时长、依赖连通性、调度器状态。"""
    monitor_heartbeat()
    now = now_naive()
    jobs = db.query(ScheduledJob).all()
    overdue = [
        j.name
        for j in jobs
        if j.enabled and j.next_run_at is not None and j.next_run_at < now
    ]
    recent_failures = (
        db.query(JobRun)
        .filter(JobRun.status != "succeeded")
        .order_by(JobRun.id.desc())
        .limit(5)
        .all()
    )
    return {
        "scope": "本实例（调用统计与启动时长为进程内数据）",
        "instance_id": INSTANCE_ID,
        "uptime_seconds": int(time.time() - STARTED_AT),
        "environment": settings.environment,
        "database": _probe_database(db),
        "redis": _probe_redis(),
        "scheduler": {
            "jobs_total": len(jobs),
            "jobs_enabled": sum(1 for j in jobs if j.enabled),
            # 到点未跑：可能是调度线程死了，也可能是这一轮刚好还没轮到，
            # 所以只报事实不下结论
            "overdue_jobs": overdue,
            "recent_failures": [
                {"name": r.job_name, "at": r.created_at.isoformat(),
                 "status": r.status, "message": r.message}
                for r in recent_failures
            ],
        },
    }


@router.get("/api-stats", response_model=ApiStatsOut,
            response_model_exclude_unset=True)
def api_stats():
    """接口调用统计：总量、状态分布、模块 TOP、慢请求与错误样本。

    配了 Redis（P1-24c）：计数字段取集群 hash 汇总（跨实例、跨重启），并附
    `counter_scope: "cluster"`；慢请求/错误样本仍为本实例（定位用明细，合并
    反而抹掉"哪台机器慢"）。未配 Redis：进程内口径，输出与引入集群计数前
    逐字节一致——`scope` 文案本身就是 process 口径的标注，不另加字段。
    """
    snapshot = metrics.snapshot()
    cluster = cluster_snapshot()
    if cluster is None:
        snapshot["scope"] = "本实例自启动以来（进程重启即清零）"
    else:
        snapshot.update(cluster)
        snapshot["scope"] = "集群（Redis 计数汇总，跨实例跨重启；慢请求与错误样本仍为本实例）"
        snapshot["counter_scope"] = "cluster"
    snapshot["instance_id"] = INSTANCE_ID
    snapshot["slow_threshold_ms"] = 1000.0
    return snapshot


@router.get("/nodes", response_model=NodesOut,
            response_model_exclude_unset=True)
def nodes():
    """集群节点状态。

    未配置 Redis 时无从得知有几个节点，此时明确返回 `unknown`，
    不拿"本实例"冒充"全集群"——单机部署与多实例漏配 Redis 的处置完全不同。
    """
    monitor_heartbeat()
    instances = known_instances()
    if instances is None:
        return {
            "scope": "unknown",
            "instance_id": INSTANCE_ID,
            "instances": None,
            "note": "未配置 Redis，无法发现同集群其他实例；单实例部署可忽略此项",
        }
    return {"scope": "集群（Redis 心跳，90 秒内有心跳视为存活）", "instances": instances}
