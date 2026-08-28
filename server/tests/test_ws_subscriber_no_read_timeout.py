"""订阅循环不得拿带读超时的客户端（跨版本回归）。

**这条是被自己的改动逼出来的。** 给 `_redis_client` 统一设 `socket_timeout` 之后，
`ws._ensure_subscriber` 拿到的也是那个带读超时的共用客户端——而
`pubsub.listen()` 是**长阻塞读**，频道安静多久就得阻塞多久。

它扛不扛得住读超时**随 redis-py 版本变**，两版都实测过（起一个假 Redis，
订阅上之后安静 9 秒再发一条广播）：

| redis-py | `PubSub.parse_response(block=True)` | 结果 |
|---|---|---|
| 8.1.0 | 显式传 `timeout=None`，关掉这次读的超时 | 安静 9 秒后仍收到广播 |
| 5.0.1 | `Connection.read_response` **没有 timeout 形参** | 安静 5 秒即 TimeoutError |

`requirements.txt` 只钉 `redis>=5.0`、全仓库无 lockfile，**两版都装得出来**。
在 5.x 上后果是：`_subscriber_loop` 的 `except` 记一条 warning 就退出线程、
`_subscriber_started` 置回 False，此后**跨实例广播静默丢失**——危急值提醒、
缺药推送都走这条总线，而"没收到推送"几乎不会有人报障。

所以订阅那一路显式要 `timeout=None`。这条用例不依赖装了哪个版本：
它钉的是**我们自己的调用形态**，那才是我们能控制的东西。
"""
import ast
import inspect
import pathlib
import textwrap

import pytest

from app import state_store, ws


def test_订阅循环取的是不带读超时的客户端():
    """核心：`_ensure_subscriber` 必须显式要 `timeout=None`。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ws.ConnectionManager._ensure_subscriber)))
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_redis_client"
    ]
    assert calls, "没找到 _redis_client 调用，用例的识别方式已失效"
    for call in calls:
        kwargs = {kw.arg: kw.value for kw in call.keywords}
        assert "timeout" in kwargs, (
            "订阅循环用了默认超时的客户端——redis-py 5.x 上会在频道安静 5 秒后打死订阅线程"
        )
        assert isinstance(kwargs["timeout"], ast.Constant) and kwargs["timeout"].value is None, (
            f"订阅循环的 timeout 必须是 None，实际 {ast.dump(kwargs['timeout'])}"
        )


def test_无超时客户端的连接超时仍然有界():
    """不设读超时 ≠ 什么都不设。连不上和连上后不回包是两码事，
    前者没有任何理由无限等下去。"""
    src = inspect.getsource(state_store._redis_client)
    assert "socket_connect_timeout=DEFAULT_REDIS_TIMEOUT if timeout is None else timeout" in src, (
        "timeout=None 时连接超时被一并关掉了——那就退回了'挂着'的老问题"
    )


def test_无超时客户端与热路径客户端不是同一个(monkeypatch):
    """两者的超时不同，必须是两个连接池；共用会把其中一方的超时强加给另一方。"""
    import sys
    import types

    created = []
    module = types.ModuleType("redis")

    class Redis:
        @staticmethod
        def from_url(url, decode_responses=False, **kwargs):
            client = types.SimpleNamespace(kwargs=kwargs, close=lambda: None)
            created.append(client)
            return client

    module.Redis = Redis
    monkeypatch.setitem(sys.modules, "redis", module)
    monkeypatch.setenv("MEDPLAT_REDIS_URL", "redis://cache.example:6379/0")
    monkeypatch.setattr(state_store, "_CLIENTS", {})

    subscriber = state_store._redis_client(timeout=None)
    hot = state_store._redis_client(0.3)
    assert subscriber is not hot
    assert subscriber.kwargs["socket_timeout"] is None
    assert subscriber.kwargs["socket_connect_timeout"] == state_store.DEFAULT_REDIS_TIMEOUT
    assert hot.kwargs["socket_timeout"] == 0.3


def test_广播发布仍然带超时():
    """只有订阅循环是例外。`broadcast` 里那次 `publish` 在推送路径上，
    照样要有超时——别把例外顺手扩大成规矩。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(ws.ConnectionManager.broadcast)))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_redis_client"
        ):
            kwargs = {kw.arg for kw in node.keywords}
            assert "timeout" not in kwargs or not node.keywords, (
                "broadcast 不该关掉读超时——它不是长阻塞读"
            )


@pytest.mark.parametrize("marker", ["5.0.1", "8.1.0"])
def test_两个版本的实测结论写在代码里(marker):
    """把实测结论钉在注释里：下一个人看到"共用客户端 + 短超时"时，
    得能当场看懂为什么订阅是例外，而不是把它"顺手统一"回去。"""
    src = pathlib.Path(state_store.__file__).read_text("utf-8")
    assert marker in src, f"state_store 里缺 redis-py {marker} 的实测记录"
