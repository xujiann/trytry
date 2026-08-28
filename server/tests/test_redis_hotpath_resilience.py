"""Redis 出网在**请求热路径**上的三道防线：超时、连接复用、熔断。

`monitor._record_cluster` 挂在 `main.py` 的 request_log_middleware 上，**每个请求跑一次**，
而它记的只是一条计数。原实现三样都没有，于是：

- **超时是吃 redis-py 默认值的**，而那个值随版本变（`requirements.txt` 只钉
  `redis>=5.0`，全仓库无 lockfile）：redis-py **5.0.0 默认 `None`**——没有超时的
  socket 不是"慢"，是"挂着"，`except Exception` 一行都接不到；redis-py **8.1.0
  默认 5 秒**——实测把 URL 指向一个丢包地址，一次调用阻塞 **5.01 秒**，六次连着 30 秒。
  两个版本都实测过。装出哪种行为取决于 pip 当时解析到什么，这本身就是不能接受的；
- **没有复用**，每请求一次 `from_url` = 一个新连接池 + 一次三次握手（实测两次调用
  `client is client2` 为 False），健康时是吞吐天花板，不健康时是每请求付一次完整超时；
- **没有熔断**，即使有了超时，Redis 整段不可达时每个请求仍要付 0.3 秒（20ms 的请求变
  320ms，慢 16 倍）；而**推荐的生产形态恰恰强制配 Redis**（多实例不配会拒启），
  这条路一定会被踩到。

这三条都是"沉默的慢"——不报错、不掉数据、只是全站变慢，最难从日志里看出来，
所以用例盯得死一些：不只断言"配上了"，还断言**去掉任何一道就必须变红**。
"""
import sys
import time
import types

import pytest

from app import monitor, state_store

#: 熔断冷却的上界。取 5 分钟：再长的话 Redis 恢复了监控台还看不见数，
#: 运维会以为集群计数坏了。数值本身可以调，"有界"这件事不能没有。
MAX_COOLDOWN_SECONDS = 300


class RecordingRedis:
    """记录构造参数与调用的最小桩；`fail=True` 时所有出网调用抛异常（模拟 Redis 不可达）。"""

    def __init__(self, url, kwargs, fail=False):
        self.url = url
        self.kwargs = kwargs
        self.fail = fail
        self.executed = 0

    def pipeline(self):
        return self

    def hincrby(self, *_a):
        return self

    def hincrbyfloat(self, *_a):
        return self

    def execute(self):
        self.executed += 1
        if self.fail:
            raise ConnectionError("Redis 不可达")


def _install_fake_redis(monkeypatch, *, fail=False):
    """装一套假的 `redis` 模块，返回 (module, created)——created 收集每次真正建出来的客户端。"""
    created: list[RecordingRedis] = []
    module = types.ModuleType("redis")

    class Redis:
        @staticmethod
        def from_url(url, decode_responses=False, **kwargs):
            client = RecordingRedis(url, dict(kwargs, decode_responses=decode_responses), fail=fail)
            created.append(client)
            return client

    module.Redis = Redis
    monkeypatch.setitem(sys.modules, "redis", module)
    monkeypatch.setenv("MEDPLAT_REDIS_URL", "redis://cache.example:6379/0")
    return module, created


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例都从干净的客户端缓存与熔断器出发——这两样都是模块级的。"""
    monkeypatch.setattr(state_store, "_CLIENTS", {})
    monitor._breaker_reset()
    yield
    monitor._breaker_reset()


# ---------------------------------------------------------------- 超时

def test_客户端必须带出网超时():
    """没有超时的 socket 不是"慢"，是"挂着"——内核 TCP 超时 5 秒，且不抛异常。"""
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        client = state_store._redis_client(0.3)
    assert client.kwargs["socket_timeout"] == 0.3, "读写超时必须传给客户端，否则丢包时永久挂起"
    assert client.kwargs["socket_connect_timeout"] == 0.3, (
        "连接超时必须单独传：连不上和连上后不回包是两种阻塞，只设一个仍会挂 5 秒"
    )


def test_默认超时不为空():
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        client = state_store._redis_client()
    assert client.kwargs["socket_timeout"] == state_store.DEFAULT_REDIS_TIMEOUT
    assert state_store.DEFAULT_REDIS_TIMEOUT > 0


def test_连接取用前探活():
    """连接池里放久了的连接对端可能已经断了，取用时才发现——那笔失败会算到某个倒霉请求头上。"""
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        client = state_store._redis_client()
    assert client.kwargs["health_check_interval"] > 0


def test_热路径超时远小于默认超时():
    """监控计数丢一条无所谓，拖慢请求是事故——这条路的超时必须比后台任务短一个量级。"""
    assert monitor._CLUSTER_TIMEOUT < state_store.DEFAULT_REDIS_TIMEOUT / 10, (
        f"热路径超时 {monitor._CLUSTER_TIMEOUT}s 相对默认 "
        f"{state_store.DEFAULT_REDIS_TIMEOUT}s 不够短，Redis 抖动会直接变成请求变慢"
    )


def test_record_cluster用的是热路径超时而非默认值(monkeypatch):
    """光定义一个短超时常量没用，得确认热路径真的把它传下去了。"""
    asked: list[float] = []
    monkeypatch.setattr(
        monitor, "_redis_client", lambda timeout=state_store.DEFAULT_REDIS_TIMEOUT: (
            asked.append(timeout) or None
        )
    )
    monitor._record_cluster("exams", 200, 12.0)
    assert asked == [monitor._CLUSTER_TIMEOUT]


# ---------------------------------------------------------------- 复用

def test_同一超时复用同一个客户端():
    """每次 from_url 都新建连接池 + 一条 TCP 连接；每请求建一次是纯浪费。"""
    with pytest.MonkeyPatch.context() as mp:
        _, created = _install_fake_redis(mp)
        first = state_store._redis_client(0.3)
        second = state_store._redis_client(0.3)
    assert first is second
    assert len(created) == 1, f"同一 (url, timeout) 建了 {len(created)} 个客户端，连接池没有复用"


def test_不同超时不共用客户端():
    """超时是连接池级别的属性：热路径的 0.3 秒不能让后台任务也变成 0.3 秒（反之亦然）。"""
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        hot = state_store._redis_client(0.3)
        background = state_store._redis_client(5.0)
    assert hot is not background
    assert hot.kwargs["socket_timeout"] == 0.3
    assert background.kwargs["socket_timeout"] == 5.0


def test_换掉redis模块不会拿到旧客户端():
    """缓存键含 redis 模块身份。否则"同一 URL、换一套 redis 实现"会拿到作废的客户端——
    测试里换桩会串味，生产里热替换/重载模块同理。"""
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        first = state_store._redis_client(0.3)
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp)
        second = state_store._redis_client(0.3)
    assert first is not second, "换了一套 redis 实现却拿回旧客户端，缓存键漏了模块身份"


def test_未配置redis时返回None(monkeypatch):
    monkeypatch.delenv("MEDPLAT_REDIS_URL", raising=False)
    assert state_store._redis_client() is None


def test_建客户端抛异常不得逃逸(monkeypatch):
    """`MEDPLAT_REDIS_URL` 直接读 os.environ（绕过 Settings 校验，TECH_DEBT P2-25），
    把 `redis://` 敲成 `http://`、端口敲成非数字，`from_url` 当场抛 ValueError。
    这个异常若从 `_record_cluster` 逃出去，会一路穿过 `metrics.record` → 请求中间件：
    **一个环境变量的错别字让每一个请求（含 /api/health）都变成 500**。"""
    def _boom(timeout=state_store.DEFAULT_REDIS_TIMEOUT):
        raise ValueError("Redis URL must specify one of the following schemes")

    monkeypatch.setattr(monitor, "_redis_client", _boom)
    monitor._record_cluster("exams", 200, 12.0)  # 不抛即通过


def test_坏URL只记一条错误日志(monkeypatch, caplog):
    """配置错误不会自愈：每请求记一条会刷爆日志，一条不记则运维永远不知道
    集群计数其实一直是空的。只记第一条。"""
    import logging

    def _boom(timeout=state_store.DEFAULT_REDIS_TIMEOUT):
        raise ValueError("bad url")

    monkeypatch.setattr(monitor, "_redis_client", _boom)
    with caplog.at_level(logging.ERROR, logger="medplat.monitor"):
        for _ in range(5):
            monitor._record_cluster("exams", 200, 12.0)
    hits = [r for r in caplog.records if "MEDPLAT_REDIS_URL" in r.getMessage()]
    assert len(hits) == 1, f"应恰好记 1 条，实际 {len(hits)} 条"


def test_未配置redis时不碰熔断锁(monkeypatch):
    """默认部署没配 Redis，此时本函数是纯空操作，不该让每个请求都白付一次加锁。"""
    calls = []
    monkeypatch.setattr(monitor, "_redis_client", lambda *a, **kw: None)
    real_allows = monitor._breaker_allows
    monkeypatch.setattr(monitor, "_breaker_allows", lambda: calls.append(1) or real_allows())
    monitor._record_cluster("exams", 200, 12.0)
    assert not calls, "没配 Redis 也去查了熔断器——热路径上白付一次加锁"


# ---------------------------------------------------------------- 熔断

def test_连续失败到阈值后停止出网():
    """Redis 整段不可达时，只该付前几次超时，之后归零——而不是每个请求都付。"""
    with pytest.MonkeyPatch.context() as mp:
        _, created = _install_fake_redis(mp, fail=True)
        for _ in range(monitor._BREAKER_THRESHOLD + 5):
            monitor._record_cluster("exams", 200, 12.0)
        client = created[0]
    assert client.executed == monitor._BREAKER_THRESHOLD, (
        f"熔断后仍在出网（{client.executed} 次 > 阈值 {monitor._BREAKER_THRESHOLD}），"
        "Redis 不可达期间每个请求都要付一次超时"
    )


def test_熔断冷却后自动重试(monkeypatch):
    """熔断不是永久拉闸——Redis 恢复了要能自己回来，不能等重启。"""
    with pytest.MonkeyPatch.context() as mp:
        _, created = _install_fake_redis(mp, fail=True)
        for _ in range(monitor._BREAKER_THRESHOLD + 2):
            monitor._record_cluster("exams", 200, 12.0)
        client = created[0]
        assert client.executed == monitor._BREAKER_THRESHOLD  # 已熔断
        # 冷却窗必须是**有界**的。这条断言不能省：下面拨表用的是常量本身，
        # 若只写 `time() + _BREAKER_COOLDOWN_SECONDS + 1`，把常量改成 1e9
        # （即"永久拉闸"）用例照样绿——变异验证抓出来的，测试自我拆台。
        assert 0 < monitor._BREAKER_COOLDOWN_SECONDS <= MAX_COOLDOWN_SECONDS, (
            f"冷却 {monitor._BREAKER_COOLDOWN_SECONDS}s 过长，Redis 恢复后集群计数要停很久"
        )
        # 把时钟拨到冷却之后。整个 time 模块换成桩，不能就地改 time.monotonic——
        # `monitor.time` 就是全局 time 模块本身，改它等于改所有人的时钟（还会自递归）。
        later = time.monotonic() + monitor._BREAKER_COOLDOWN_SECONDS + 1
        mp.setattr(monitor, "time", types.SimpleNamespace(monotonic=lambda: later))
        monitor._record_cluster("exams", 200, 12.0)
    assert client.executed == monitor._BREAKER_THRESHOLD + 1, "冷却期满后没有重试，熔断成了永久拉闸"


def test_成功会清零失败计数():
    """零星失败不该累积成熔断——三次分散的抖动和连续三次不可达是两回事。"""
    assert monitor._BREAKER_THRESHOLD >= 2, "阈值为 1 时本用例无从区分'连续'与'零星'"
    with pytest.MonkeyPatch.context() as mp:
        _, created = _install_fake_redis(mp, fail=True)
        monitor._record_cluster("exams", 200, 12.0)
        client = created[0]
        # 差一次到阈值 → 一次成功 → 再差一次到阈值：若成功没清零，累计早已越过阈值
        for _ in range(monitor._BREAKER_THRESHOLD - 2):
            monitor._record_cluster("exams", 200, 12.0)
        client.fail = False
        monitor._record_cluster("exams", 200, 12.0)  # 一次成功
        client.fail = True
        for _ in range(monitor._BREAKER_THRESHOLD - 1):
            monitor._record_cluster("exams", 200, 12.0)
        before = client.executed
        monitor._record_cluster("exams", 200, 12.0)
    assert client.executed == before + 1, "成功没有清零失败计数，零星抖动会攒成熔断"


def test_熔断期间调用方无感():
    """熔断是内部行为：`_record_cluster` 无论通不通都不该往上抛。"""
    with pytest.MonkeyPatch.context() as mp:
        _install_fake_redis(mp, fail=True)
        for _ in range(monitor._BREAKER_THRESHOLD + 3):
            monitor._record_cluster("exams", 200, 12.0)  # 不抛即通过


def test_熔断用单调钟():
    """`time.time()` 会被 NTP 校时/人工改表往前后拨——那会让熔断器要么一直开着几小时、
    要么立刻放行。口径与 `alerting.py` 的冷却一致（那里本来就用 monotonic）。"""
    import inspect

    for fn in (monitor._breaker_allows, monitor._breaker_record):
        source = inspect.getsource(fn)
        assert "time.time()" not in source, f"{fn.__name__} 用了墙钟，改系统时间会让熔断失准"
        assert "monotonic" in source


def test_熔断不影响非热路径读取():
    """`cluster_snapshot` 是监控台点一下才跑的，不在请求热路径上，
    不该被热路径的熔断器连坐——否则 Redis 恢复后监控台要等冷却才看得见数。"""
    import inspect

    source = inspect.getsource(monitor.cluster_snapshot)
    assert "_breaker_allows" not in source
