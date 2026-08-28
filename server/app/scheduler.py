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
import contextlib
import logging
import secrets
import threading
import time
from datetime import timedelta
from typing import Callable, Iterator

from sqlalchemy.orm import Session

from .alerting import send_alert
from .clock import now_naive
from .database import SessionLocal
from .models import JobRun, ScheduledJob
from .state_store import _redis_client

logger = logging.getLogger("medplat.scheduler")

# 调度循环的心跳：每 30 秒查一次有没有到期任务
TICK_SECONDS = 30
# 执行锁的 TTL：不是"任务必须跑完"的时限，而是**实例崩溃后锁的自愈时间**——
# 任务执行期间由续期心跳把 TTL 不断顶回去（见 _LockKeeper），实例挂了续期停止，
# 锁最多 TTL 秒后过期，别的实例才接手。
LOCK_TTL_SECONDS = 300
# 续期间隔取 TTL 的四分之一：连丢两次心跳后第三次仍在过期前，留一次余量
# （取三分之一时第三次恰好落在过期点上，实际只容一次丢失）。
RENEW_INTERVAL_SECONDS = LOCK_TTL_SECONDS // 4
# 续期失败后的快速重试间隔：不等满一个心跳周期，抢在 TTL 耗尽前多试几次
RENEW_RETRY_SECONDS = 5

# 释放锁的 Lua：仅当锁的值仍等于本实例的 token 才删，比对与删除在一条命令里原子完成。
# 防的是：任务跑过了 TTL → 锁过期 → 别的实例抢到并重设 → 本实例收尾时把别人的锁删掉。
_RELEASE_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('del', KEYS[1]) else return 0 end"
)

# 续期的 Lua：仍持有（值等于 token）才把 TTL 顶回去，比对与 expire 原子完成。
# 防的是：锁已被别的实例接管后，本实例的续期把**别人的锁**续了命。
_RENEW_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] then "
    "return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"
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


def _renew_lock(name: str, token: str) -> bool:
    """仍持有则把锁 TTL 顶回 LOCK_TTL_SECONDS；返回是否仍持有。无 Redis 恒 True。"""
    redis = _redis_client()
    if redis is None:
        return True
    return bool(
        redis.eval(_RENEW_LUA, 1, f"medplat:joblock:{name}", token, LOCK_TTL_SECONDS)
    )


class _LockKeeper:
    """任务执行期间的锁续期心跳（watchdog）。

    修的洞：任务跑超 LOCK_TTL 时锁过期，别的实例抢到同一任务**并发执行**——
    token 所有权（上一轮修的）只保证"不误删他人的锁"，保证不了"不双跑"。
    续期心跳让锁在任务存活期间一直有效；实例崩溃则心跳随之停止，
    锁最多 TTL 秒后过期，由别的实例自然接管——自愈语义不变。

    若某次续期发现锁已易主（长时间 GC 停顿/网络分区后迟到），无法安全中止
    正在跑的任务，只能记日志并停止心跳——此时确有并发窗口，但已从
    "任何超 TTL 的任务必双跑"收窄到"心跳连续丢失 TTL 秒以上"。
    """

    def __init__(self, name: str, token: str, interval: float = RENEW_INTERVAL_SECONDS) -> None:
        self._name = name
        self._token = token
        self._interval = interval
        self._stop = threading.Event()
        # 复用同一个客户端并设超时：每次心跳新建连接既浪费（长任务会攒下几十条
        # 无人回收的连接），又会在网络黑洞时无限阻塞——心跳就此静默停摆。
        # 超时由 `_redis_client` 统一设（DEFAULT_REDIS_TIMEOUT=5s，与这里原先
        # 就地 setdefault 的值一致）。**不能再在这里改 connection_kwargs**：
        # 客户端现在是全局复用的，改它等于替所有调用方改超时。
        self._redis = _redis_client()
        self._thread = threading.Thread(
            target=self._run, name=f"joblock-keeper-{name}", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _renew_once(self) -> bool:
        """仍持有则顶回 TTL。用 keeper 自己的客户端，避免每次心跳新建连接。"""
        if self._redis is None:
            return True
        return bool(
            self._redis.eval(
                _RENEW_LUA, 1, f"medplat:joblock:{self._name}", self._token, LOCK_TTL_SECONDS
            )
        )

    def _run(self) -> None:
        wait = self._interval
        while not self._stop.wait(wait):
            wait = self._interval
            try:
                if not self._renew_once():
                    logger.warning(
                        "[SCHEDULER] 任务 %s 的执行锁已易主，停止续期（存在并发窗口）",
                        self._name,
                    )
                    return
            except Exception:  # noqa: BLE001 - 续期失败不打断任务，快速重试抢在 TTL 前
                logger.exception("[SCHEDULER] 任务 %s 锁续期异常，%ss 后重试",
                                 self._name, RENEW_RETRY_SECONDS)
                wait = min(RENEW_RETRY_SECONDS, self._interval)

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)


#: 进程内互斥。Redis 锁挡的是**跨实例**并发，挡不住同进程内的两条路径——
#: 调度循环跑在 `asyncio.to_thread` 的线程里，手工触发跑在同步端点的线程池里，
#: 二者天然可以重叠。而 `MEDPLAT_REDIS_URL` 不配是默认部署形态，那时
#: `_acquire_lock` 恒返回 token（"单实例语义"），若只靠它，这把锁在默认配置下
#: 等于不存在。所以两层都要：进程内一把、跨实例一把。
_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(name: str) -> threading.Lock:
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(name, threading.Lock())


@contextlib.contextmanager
def job_lock(name: str) -> Iterator[str | None]:
    """任务执行锁：拿到则 yield token 并在执行期间续期，拿不到 yield None。

    抽出来给**调度循环与手工触发共用**。此前只有调度循环走锁，`/api/jobs/{name}/run`
    直接调 `run_job`——手工补跑会和正在跑的同一个调度任务并发。多数任务是"扫一遍然后
    改状态"，并发跑轻则重复发通知、重则把同一批单子处理两次。

    **两层锁**：先抢进程内的 `threading.Lock`（挡同进程内调度线程与请求线程的重叠，
    这在不配 Redis 的默认部署里是唯一起作用的一层），再抢 Redis 锁（挡跨实例）。
    两层都非阻塞：抢不到就 yield None，由调用方决定是跳过还是报 409。
    """
    local = _local_lock(name)
    if not local.acquire(blocking=False):
        yield None
        return
    try:
        token = _acquire_lock(name)
        if token is None:
            yield None
            return
        keeper = None
        try:
            # 无 Redis 即单实例语义，锁不会过期，不必起心跳线程
            if _redis_client() is not None:
                keeper = _LockKeeper(name, token)
                keeper.start()
            yield token
        finally:
            if keeper is not None:
                keeper.stop()
            _release_lock(name, token)
    finally:
        local.release()


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
        # 工程包 P2：任务失败主动外发告警（kind 带任务名，互不占用冷却）
        send_alert(f"job_failed:{name}", f"定时任务 {spec.title}（{name}）执行失败：{message}")
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
        with job_lock(name) as token:
            if token is None:
                continue  # pragma: no cover - 需多实例：别的实例正持有该任务的锁
            # 拿到锁后**重新确认到期**：names 是本轮开头的快照，别的实例可能已把
            # 这个任务跑完并把 next_run_at 推到未来（长任务持锁期间尤其容易发生）。
            # 只靠锁只能保证"不重叠"，保证不了"不重复"。
            with SessionLocal() as db:
                if name not in due_jobs(db):
                    continue
            with SessionLocal() as db:
                run_job(db, name)
            executed += 1
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
