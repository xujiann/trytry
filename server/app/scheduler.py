"""定时任务基座（T1.1）。

此前平台没有任何调度能力：慢病随访超期、医废滞留、合同到期、制剂临期、
日终对账、验证码清理——所有"到点该发生的事"都得有人手动调接口。这里补上
最小可用的调度器。

设计取舍：

- **间隔而非 cron**：平台的定时需求都是"每 N 分钟/小时扫一遍"，不需要日历语义，
  为此引入 cron 解析器不划算。
- **到期时刻落库**（`ScheduledJob.next_run_at`）而不是纯内存计时器：进程重启后
  不会漏跑，也不会因为重启而把所有任务挤到同一时刻。
- **多实例只跑一次**：配置 Redis 时用 `SET NX EX` 抢执行锁；未配置时退化为
  单实例语义，这与登出黑名单、防爆破锁定的既有约定一致（详见 README 生产硬化）。
- **任务实现留在代码里**，库里只存调度参数与状态：任务是代码资产，不该能被
  改库改出一个不存在的实现。

新增任务：写一个 `def job(db) -> tuple[int, str]` 的函数，用 `@register` 注册。
返回 (处理对象数, 结果摘要)。抛异常即记为失败，不影响其他任务与调度循环。
"""
import asyncio
import logging
import secrets
import time
from datetime import timedelta
from typing import Callable

from sqlalchemy.orm import Session

from .clock import now_naive
from .database import SessionLocal
from .models import JobRun, ScheduledJob
from .state_store import _redis_client

logger = logging.getLogger("medplat.scheduler")

# 调度循环的心跳：每 30 秒查一次有没有到期任务
TICK_SECONDS = 30
# 执行锁的持有时长：单个任务跑不过这么久，否则锁会被别的实例抢走
LOCK_TTL_SECONDS = 300

# 释放锁的 Lua：仅当锁的值仍等于本实例的 token 才删，比对与删除在一条命令里原子完成。
# 防的是：任务跑过了 TTL → 锁过期 → 别的实例抢到并重设 → 本实例收尾时把别人的锁删掉。
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

JobFunc = Callable[[Session], tuple[int, str]]


class JobSpec:
    def __init__(self, name: str, title: str, interval_seconds: int, func: JobFunc) -> None:
        self.name = name
        self.title = title
        self.interval_seconds = interval_seconds
        self.func = func


REGISTRY: dict[str, JobSpec] = {}


def register(name: str, title: str, interval_seconds: int):
    """任务注册装饰器。"""

    def wrapper(func: JobFunc) -> JobFunc:
        REGISTRY[name] = JobSpec(name, title, interval_seconds, func)
        return func

    return wrapper


def sync_registry(db: Session) -> None:
    """把代码中注册的任务同步进库（幂等）。

    只补新任务、不覆盖已有行——管理员在库里调过的 interval/enabled 是运维决策，
    重启不该把它冲掉。
    """
    existing = {j.name for j in db.query(ScheduledJob).all()}
    for spec in REGISTRY.values():
        if spec.name in existing:
            continue
        db.add(
            ScheduledJob(
                name=spec.name,
                title=spec.title,
                interval_seconds=spec.interval_seconds,
                next_run_at=now_naive() + timedelta(seconds=spec.interval_seconds),
            )
        )
    db.commit()


def _acquire_lock(name: str) -> str | None:
    """多实例互斥：拿到锁返回本次持有的唯一 token，未拿到返回 None。

    无 Redis 时恒返回 token（单实例语义）。token 供释放时校验持有者，避免锁 TTL
    过期被别的实例抢走后，本实例的释放误删他人的锁。
    """
    token = secrets.token_hex(16)
    redis = _redis_client()
    if redis is None:
        return token
    ok = redis.set(f"medplat:joblock:{name}", token, nx=True, ex=LOCK_TTL_SECONDS)
    return token if ok else None


def _release_lock(name: str, token: str) -> None:
    """仅当锁仍由本实例持有（值等于 token）时才释放，用 Lua 保证比对与删除原子。"""
    redis = _redis_client()
    if redis is None:
        return
    redis.eval(_RELEASE_LUA, 1, f"medplat:joblock:{name}", token)


def run_job(db: Session, name: str, trigger: str = "scheduled") -> JobRun:
    """执行一个任务并留痕。异常被捕获记为 failed，不向外抛。"""
    spec = REGISTRY[name]
    started = time.monotonic()
    affected, message, status = 0, "", "succeeded"
    try:
        affected, message = spec.func(db)
    except Exception as exc:  # noqa: BLE001 - 单个任务失败不应拖垮调度器
        db.rollback()
        status, message = "failed", f"{type(exc).__name__}: {exc}"[:1000]
        logger.exception("[SCHEDULER] 任务 %s 执行失败", name)
    run = JobRun(
        job_name=name,
        trigger=trigger,
        status=status,
        message=message[:1000],
        affected=affected,
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    db.add(run)
    job = db.query(ScheduledJob).filter(ScheduledJob.name == name).first()
    if job is not None:
        now = now_naive()
        job.last_run_at = now
        job.last_status = status
        job.next_run_at = now + timedelta(seconds=job.interval_seconds)
    db.commit()
    db.refresh(run)
    return run


def due_jobs(db: Session) -> list[str]:
    """到期且启用的任务名；只认代码里真实注册过的实现。"""
    now = now_naive()
    rows = (
        db.query(ScheduledJob)
        .filter(
            ScheduledJob.enabled.is_(True),
            (ScheduledJob.next_run_at.is_(None)) | (ScheduledJob.next_run_at <= now),
        )
        .all()
    )
    return [j.name for j in rows if j.name in REGISTRY]


def tick() -> int:
    """跑一轮：执行所有到期任务，返回执行条数。供调度循环与测试共用。"""
    executed = 0
    with SessionLocal() as db:
        names = due_jobs(db)
    for name in names:
        token = _acquire_lock(name)
        if token is None:
            continue  # pragma: no cover - 需多实例：别的实例正持有该任务的锁
        try:
            with SessionLocal() as db:
                run_job(db, name)
            executed += 1
        finally:
            _release_lock(name, token)
    return executed


async def scheduler_loop() -> None:
    """后台调度循环：由 lifespan 启动，随应用关闭而取消。"""
    while True:
        try:
            await asyncio.to_thread(tick)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 循环本身绝不能因单次异常退出
            logger.exception("[SCHEDULER] 调度轮次异常")
        await asyncio.sleep(TICK_SECONDS)
