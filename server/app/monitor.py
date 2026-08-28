"""运行监控采集（浙江省指南 #47 监控管理 / #46 日志图形化）。

**无 Redis 时这份数据是进程内的，不是全集群的。** 计数器随进程启停清零，
多实例部署下每个实例只看得见自己。这不是偷懒——把调用统计写进数据库，
等于给每个请求加一次写库；引入 Prometheus 又要求县域机房再运维一套系统。
折中办法是：进程内计数，配了 Redis 就顺带注册实例心跳，让"有几个节点、
各自活着没有"这件事可见。

**配了 `MEDPLAT_REDIS_URL` 时计数同步累加进 Redis hash**（P1-24c）：
每请求一次 pipeline（hincrby/hincrbyfloat），监控台读 hash 得到跨实例、
跨重启的集群口径总量。慢请求与错误**样本**仍留在进程内——样本是定位用的
少量明细，跨实例合并不增加信息量，反而把"哪台机器慢"抹掉了。

响应里始终带上 `scope` 字段说明口径。监控数据最怕的不是不准，
是**看起来像全局的其实只是一台**——那会让人在扩容后误判流量掉了一半。
"""
import logging
import os
import threading
import time
import uuid
from collections import Counter, defaultdict, deque

from .state_store import _redis_client

logger = logging.getLogger("medplat.monitor")

# 实例标识：进程启动时生成，同一台机器重启即换号（重启本身就是要被看见的事件）
INSTANCE_ID = f"{os.uname().nodename}-{uuid.uuid4().hex[:8]}"
STARTED_AT = time.time()

# 慢请求样本与错误样本的保留条数。够定位问题即可，不做全量留存——
# 那是日志系统的事，不是监控台的事。
SAMPLE_SIZE = 50
# 慢请求阈值（毫秒）
SLOW_MS = 1000.0
# 实例心跳在 Redis 中的存活时间：超过即认为该节点已下线
HEARTBEAT_TTL = 90


class ApiMetrics:
    """接口调用计数：总量、状态分布、按模块聚合、慢请求与错误样本。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._reset_counters()

    def _reset_counters(self) -> None:
        """清零所有计数器。不碰 `_lock`——reset() 是持锁调用的，
        在锁内换掉锁对象会让同时等锁的线程落到两把不同的锁上。"""
        self.total = 0
        self.duration_sum = 0.0
        self.by_status_class: Counter = Counter()   # "2xx"/"4xx"/"5xx"
        self.by_module: Counter = Counter()         # /api/exams/... -> exams
        # 累加的是毫秒（float），Counter 的值类型是 int，故用 defaultdict
        self.module_duration: defaultdict[str, float] = defaultdict(float)
        self.by_status_code: Counter = Counter()
        self.slow: deque = deque(maxlen=SAMPLE_SIZE)
        self.errors: deque = deque(maxlen=SAMPLE_SIZE)

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        module = _module_of(path)
        _record_cluster(module, status, duration_ms)
        with self._lock:
            self.total += 1
            self.duration_sum += duration_ms
            self.by_status_class[f"{status // 100}xx"] += 1
            self.by_status_code[status] += 1
            self.by_module[module] += 1
            self.module_duration[module] += duration_ms
            if duration_ms >= SLOW_MS:
                self.slow.append(
                    {"method": method, "path": path, "duration_ms": duration_ms, "at": _now()}
                )
            if status >= 400:
                self.errors.append(
                    {"method": method, "path": path, "status": status, "at": _now()}
                )

    def snapshot(self) -> dict:
        with self._lock:
            total = self.total
            return {
                "total_requests": total,
                "avg_duration_ms": round(self.duration_sum / total, 2) if total else 0.0,
                "by_status_class": dict(self.by_status_class),
                "by_status_code": dict(sorted(self.by_status_code.items())),
                "top_modules": [
                    {
                        "module": m,
                        "count": c,
                        "avg_duration_ms": round(self.module_duration[m] / c, 2),
                    }
                    for m, c in self.by_module.most_common(15)
                ],
                "slow_samples": list(self.slow)[::-1],
                "error_samples": list(self.errors)[::-1],
            }

    def reset(self) -> None:
        """仅供测试与手动清零；生产没有清零入口，避免有人靠清零把错误率洗白。"""
        with self._lock:
            self._reset_counters()


def _module_of(path: str) -> str:
    """/api/mgmt/assets/3 -> mgmt/assets；/api/exams/12/report -> exams。

    mgmt 与 portal 下挂了十几个子模块，只取第二段会让它们全挤成一行。
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2 or parts[0] != "api":
        return path or "/"
    if parts[1] in ("mgmt", "portal") and len(parts) > 2:
        return f"{parts[1]}/{parts[2]}"
    return parts[1]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


metrics = ApiMetrics()

# 集群计数的 Redis hash 键（P1-24c）。字段与进程内计数一一对应：
# totals{total, duration_ms} / status_class{2xx..} / status_code{200..} /
# module_count{exams..} / module_duration{exams..}
_METRICS_PREFIX = "medplat:metrics:"


#: 集群计数的出网超时：这条路在**每个请求**的主路径上（见 main.py 的
#: request_log_middleware），而它记的只是一条计数。丢一条无所谓，拖慢请求是事故，
#: 所以给一个远比默认值短的超时。
_CLUSTER_TIMEOUT = 0.3

#: 熔断：连续失败到阈值就停一段时间不再尝试。
#:
#: 光有超时不够——Redis 整段不可达时，每个请求仍要付一次 0.3 秒（20ms 的请求变成
#: 320ms，慢 16 倍），而**推荐的生产形态恰恰强制配 Redis**（多实例无 Redis 会拒启），
#: 所以这条路一定会被踩到。熔断之后一次故障最多付几次超时，之后归零。
#:
#: 时钟用 `time.monotonic()` 而不是 `time.time()`：后者会被 NTP 校时、
#: 运维改系统时间往前后拨，那会让熔断器要么一直开着几小时、要么立刻放行。
#: 口径与 `alerting.py` 的冷却一致（那里本来就用的 monotonic）。
#: 本文件其余地方的 `time.time()` 是**给人看的墙钟**（实例启动时刻、样本时间戳），
#: 两者不是一回事，别顺手统一。
_BREAKER_THRESHOLD = 3
_BREAKER_COOLDOWN_SECONDS = 30.0
_breaker_lock = threading.Lock()
_breaker_failures = 0
_breaker_open_until = 0.0

#: `MEDPLAT_REDIS_URL` 写错导致建客户端失败时，只记第一条（见 `_record_cluster`）。
_client_error_logged = False


def _log_client_error_once() -> None:
    global _client_error_logged
    with _breaker_lock:
        if _client_error_logged:
            return
        _client_error_logged = True
    logger.error(
        "MEDPLAT_REDIS_URL 无法建立客户端，集群计数将一直为空（本条只记一次）",
        exc_info=True,
    )


def _breaker_allows() -> bool:
    with _breaker_lock:
        return time.monotonic() >= _breaker_open_until


def _breaker_record(ok: bool) -> None:
    global _breaker_failures, _breaker_open_until
    with _breaker_lock:
        if ok:
            _breaker_failures = 0
            return
        _breaker_failures += 1
        if _breaker_failures >= _BREAKER_THRESHOLD:
            _breaker_open_until = time.monotonic() + _BREAKER_COOLDOWN_SECONDS
            _breaker_failures = 0


def _breaker_reset() -> None:
    """仅供测试：把熔断器与"已记过一次"标记恢复到初始状态，免得用例之间互相影响。

    熔断器是**进程级全局**、冷却按真实的 30 秒算——任何一个用例把它推开，
    后面的用例就会拿到空的集群计数，而报错信息会指向 Redis 接线而不是泄漏的全局状态。
    故 `conftest.py` 里按 autouse 每个用例复位一次。
    """
    global _breaker_failures, _breaker_open_until, _client_error_logged
    with _breaker_lock:
        _breaker_failures = 0
        _breaker_open_until = 0.0
        _client_error_logged = False


def _record_cluster(module: str, status: int, duration_ms: float) -> None:
    """把一次调用累加进 Redis hash（未配置 Redis 时空操作）。

    一次 pipeline 六个 incr，单个往返；Redis 抖动不该影响业务请求，
    失败静默丢这一条——监控计数丢一条无所谓，请求变慢或报错才是事故。

    这段注释的**意图**一直是对的，但原实现兑现不了：超时是吃 redis-py 的默认值的，
    而那个值随版本变（`requirements.txt` 只钉 `redis>=5.0`，无 lockfile）——
    redis-py 5.0 默认 `None`（永久挂起，`except Exception` 接不到任何东西），
    8.1 默认 5 秒（实测把 URL 指向丢包地址，一次调用阻塞 **5.01 秒**）。
    而这段跑在事件循环上、**每个请求一次**：Redis 一丢包，全站降到 5 秒一个请求。
    现在靠三件事兑现：复用客户端（不再每请求建连）、短超时、以及熔断。
    """
    try:
        redis = _redis_client(_CLUSTER_TIMEOUT)
    except Exception:  # noqa: BLE001 - 见下
        # **建客户端本身也会抛。** `MEDPLAT_REDIS_URL` 是直接读 os.environ 的
        # （绕过 Settings 校验，见 TECH_DEBT P2-25），把 `redis://` 敲成 `http://`
        # 或端口敲成非数字，`from_url` 当场抛 ValueError。原先这一句在 try 外面，
        # 于是这个异常会一路穿过 `metrics.record` → 请求中间件 → 最外层：
        # **一个环境变量的错别字让每一个请求（含 /api/health）都变成 500**，
        # 与本函数"Redis 抖动不该影响业务请求"的承诺正相反。
        #
        # 这是配置错误、不会自愈，所以不进熔断（熔断管的是网络抖动），
        # 而是**只记一次**——每请求记一条会把日志刷爆，一条都不记则运维永远
        # 不知道集群计数其实一直是空的。
        _log_client_error_once()
        return
    if redis is None:
        return
    # 熔断判断放在"有没有配 Redis"之后：默认部署根本没配，这时本函数是纯空操作，
    # 不该让每个请求都白付一次加锁。
    if not _breaker_allows():
        return
    try:
        pipe = redis.pipeline()
        pipe.hincrby(f"{_METRICS_PREFIX}totals", "total", 1)
        pipe.hincrbyfloat(f"{_METRICS_PREFIX}totals", "duration_ms", duration_ms)
        pipe.hincrby(f"{_METRICS_PREFIX}status_class", f"{status // 100}xx", 1)
        pipe.hincrby(f"{_METRICS_PREFIX}status_code", str(status), 1)
        pipe.hincrby(f"{_METRICS_PREFIX}module_count", module, 1)
        pipe.hincrbyfloat(f"{_METRICS_PREFIX}module_duration", module, duration_ms)
        pipe.execute()
    except Exception:  # Redis 抖动不该影响业务请求
        _breaker_record(False)
    else:
        _breaker_record(True)


def cluster_snapshot() -> dict | None:
    """集群计数快照（与 `ApiMetrics.snapshot` 的计数字段同形状，不含样本）。

    未配置 Redis 或读取失败时返回 None，调用方回落进程内口径——
    与 `known_instances` 同一哲学：无从得知就明说，不拿本实例冒充集群。
    """
    redis = _redis_client()
    if redis is None:
        return None
    try:
        totals = redis.hgetall(f"{_METRICS_PREFIX}totals")
        status_class = redis.hgetall(f"{_METRICS_PREFIX}status_class")
        status_code = redis.hgetall(f"{_METRICS_PREFIX}status_code")
        module_count = redis.hgetall(f"{_METRICS_PREFIX}module_count")
        module_duration = redis.hgetall(f"{_METRICS_PREFIX}module_duration")
    except Exception:  # pragma: no cover
        return None
    total = int(totals.get("total", 0) or 0)
    duration_sum = float(totals.get("duration_ms", 0) or 0)
    counts = {m: int(c) for m, c in module_count.items()}
    return {
        "total_requests": total,
        "avg_duration_ms": round(duration_sum / total, 2) if total else 0.0,
        "by_status_class": {k: int(v) for k, v in status_class.items()},
        "by_status_code": {
            int(k): int(v) for k, v in sorted(status_code.items(), key=lambda kv: int(kv[0]))
        },
        "top_modules": [
            {
                "module": m,
                "count": c,
                "avg_duration_ms": round(float(module_duration.get(m, 0) or 0) / c, 2),
            }
            for m, c in sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:15]
        ],
    }


def heartbeat() -> None:
    """向 Redis 注册本实例心跳（未配置 Redis 时静默跳过）。"""
    redis = _redis_client()
    if redis is None:
        return
    try:
        redis.setex(
            f"medplat:instance:{INSTANCE_ID}",
            HEARTBEAT_TTL,
            str(int(STARTED_AT)),
        )
    except Exception:  # pragma: no cover - Redis 抖动不该影响业务请求
        pass


def known_instances() -> list[dict] | None:
    """集群内存活实例清单；未配置 Redis 时返回 None（表示"无从得知"）。

    返回 None 而不是 [本实例]——后者会让单机与"多实例但没配 Redis"
    看起来一模一样，而这两种情况的处置完全不同。
    """
    redis = _redis_client()
    if redis is None:
        return None
    try:
        keys = redis.keys("medplat:instance:*")
        rows = []
        for key in keys:
            started = redis.get(key)
            instance = key.split(":", 2)[-1]
            rows.append(
                {
                    "instance_id": instance,
                    "self": instance == INSTANCE_ID,
                    "uptime_seconds": int(time.time() - int(started)) if started else None,
                }
            )
        return sorted(rows, key=lambda r: r["instance_id"])
    except Exception:  # pragma: no cover
        return None
