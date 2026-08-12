"""运行监控台（浙江省指南 #47）：运行环境概览、调用统计、节点状态。

限管理员。监控数据本身不敏感，但它会暴露内部路径、错误详情与实例拓扑，
对外开放没有好处。

采集口径与"进程内 vs 集群"的取舍见 `app/monitor.py` 顶部说明；
每个接口的响应都带 `scope` 字段，避免把单实例数据当成全局数据看。
"""
import time

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import settings
from ..clock import now_naive
from ..database import get_db
from ..deps import require_admin
from ..monitor import INSTANCE_ID, STARTED_AT, metrics
from ..monitor import heartbeat as monitor_heartbeat
from ..monitor import known_instances
from ..models import JobRun, ScheduledJob
# 调度器把 next_run_at 存为 naive UTC；用 models.utcnow()（aware）去比会直接
# TypeError。这里复用调度器自己的时钟函数，口径永远跟着它走。
from ..state_store import _redis_client

router = APIRouter(prefix="/api/monitor", tags=["运行监控"], dependencies=[Depends(require_admin)])


def _probe_database(db: Session) -> dict:
    start = time.perf_counter()
    try:
        db.execute(text("SELECT 1"))
        return {
            "connected": True,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "dialect": db.bind.dialect.name,
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


@router.get("/overview")
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
        .filter(JobRun.status != "success")
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


@router.get("/api-stats")
def api_stats():
    """接口调用统计：总量、状态分布、模块 TOP、慢请求与错误样本。"""
    snapshot = metrics.snapshot()
    snapshot["scope"] = "本实例自启动以来（进程重启即清零）"
    snapshot["instance_id"] = INSTANCE_ID
    snapshot["slow_threshold_ms"] = 1000.0
    return snapshot


@router.get("/nodes")
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
