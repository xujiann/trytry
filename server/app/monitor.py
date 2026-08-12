"""运行监控采集（浙江省指南 #47 监控管理 / #46 日志图形化）。

**这份数据是进程内的，不是全集群的。** 计数器随进程启停清零，多实例部署下
每个实例只看得见自己。这不是偷懒——把调用统计写进数据库，等于给每个请求
加一次写库；引入 Prometheus 又要求县域机房再运维一套系统。折中办法是：
进程内计数，配了 Redis 就顺带注册实例心跳，让"有几个节点、各自活着没有"
这件事可见，而每个节点的调用明细各查各的。

响应里始终带上 `scope` 字段说明这一点。监控数据最怕的不是不准，
是**看起来像全局的其实只是一台**——那会让人在扩容后误判流量掉了一半。
"""
import os
import threading
import time
import uuid
from collections import Counter, deque

from .state_store import _redis_client

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
        self.total = 0
        self.duration_sum = 0.0
        self.by_status_class: Counter = Counter()   # "2xx"/"4xx"/"5xx"
        self.by_module: Counter = Counter()         # /api/exams/... -> exams
        self.module_duration: Counter = Counter()
        self.by_status_code: Counter = Counter()
        self.slow: deque = deque(maxlen=SAMPLE_SIZE)
        self.errors: deque = deque(maxlen=SAMPLE_SIZE)

    def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
        module = _module_of(path)
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
            self.__init__()


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
