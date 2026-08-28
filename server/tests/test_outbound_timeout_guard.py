"""出网调用必须带超时（AST 静态防复发）。

**吃库的默认超时等于没有超时口径**：`state_store._redis_client` 就是这么踩的——
注释写着"Redis 抖动不该影响业务请求，失败静默丢这一条"，实现里却一个超时都没显式设，
于是行为完全取决于装到了哪个版本的 redis-py（`requirements.txt` 只钉 `redis>=5.0`，
全仓库无 lockfile）：**5.0.0 默认 `None`**（没有超时的 socket 不是"慢"，是"挂着"，
那个 `except` 从来接不到任何东西），**8.1.0 默认 5 秒**（实测把 URL 指向丢包地址，
一次调用阻塞 5.01 秒）。而它跑在事件循环上、**每个请求一次**。

仓库里出网的地方不多，但每加一处就多一次踩坑机会，而且这类缺陷**不报错、
不掉数据、只是全站变慢**，最难从日志里看出来。所以静态钉住：
凡是 httpx 的出网调用、凡是新建 Redis 客户端，都必须显式带超时。

豁免名单只许变小，且必须写明理由。
"""
import ast
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parent.parent / "app"

#: httpx 的出网方法。`stream` 也算——流式读同样会挂在没有超时的 socket 上。
HTTPX_CALLS = {"get", "post", "put", "patch", "delete", "head", "options", "request", "stream"}
#: 建长期客户端的类；超时是连接池级属性，必须在这里定死。
HTTPX_CLIENTS = {"Client", "AsyncClient"}

#: 豁免：`(相对路径, 行号)` → 理由。只许变少。
EXEMPTIONS: dict[tuple[str, int], str] = {}


def _iter_files():
    for path in sorted(APP.rglob("*.py")):
        yield path, ast.parse(path.read_text("utf-8"), filename=str(path))


def _kwargs(node: ast.Call) -> set[str]:
    return {kw.arg for kw in node.keywords if kw.arg}


def _rel(path: Path) -> str:
    return str(path.relative_to(APP.parent))


def _collect(predicate, required: set[str]) -> list[str]:
    """返回所有"命中 predicate 却没带齐 required 关键字"的调用点描述。"""
    offenders = []
    for path, tree in _iter_files():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not predicate(node):
                continue
            missing = required - _kwargs(node)
            if not missing:
                continue
            if EXEMPTIONS.get((_rel(path), node.lineno)):
                continue
            offenders.append(f"{_rel(path)}:{node.lineno} 缺 {sorted(missing)}")
    return offenders


def _is_httpx_call(node: ast.Call) -> bool:
    """匹配 `httpx.get(...)` 一类；`client.get(...)` 不匹配（超时在建客户端时定）。"""
    func = node.func
    return (
        isinstance(func, ast.Attribute)
        and func.attr in HTTPX_CALLS
        and isinstance(func.value, ast.Name)
        and func.value.id == "httpx"
    )


def _is_httpx_client(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr in HTTPX_CLIENTS:
        return isinstance(func.value, ast.Name) and func.value.id == "httpx"
    return isinstance(func, ast.Name) and func.id in HTTPX_CLIENTS


def _is_redis_from_url(node: ast.Call) -> bool:
    func = node.func
    return isinstance(func, ast.Attribute) and func.attr == "from_url"


def test_httpx出网调用必须带timeout():
    offenders = _collect(_is_httpx_call, {"timeout"})
    assert not offenders, "以下出网调用没有超时，网络黑洞时会无限挂起：\n  " + "\n  ".join(offenders)


def test_httpx客户端必须带timeout():
    offenders = _collect(_is_httpx_client, {"timeout"})
    assert not offenders, "以下 httpx 客户端没有超时：\n  " + "\n  ".join(offenders)


def test_redis客户端必须带读写与连接超时():
    """两个都要：连不上和连上后不回包是两种阻塞，只设一个仍会挂满内核 TCP 超时。"""
    offenders = _collect(_is_redis_from_url, {"socket_timeout", "socket_connect_timeout"})
    assert not offenders, "以下 Redis 客户端没有超时：\n  " + "\n  ".join(offenders)


def test_守卫本身能抓到东西():
    """自检：识别函数确实认得出真实调用形状，别写成一个永远扫不到东西的空守卫。

    这一条是被自己坑过才加的——一个匹配不上任何节点的 AST 守卫永远是绿的，
    看着像在守，其实什么都没守。
    """
    tree = ast.parse(
        "import httpx\n"
        "httpx.post(url, json=x)\n"
        "httpx.Client(base_url=u)\n"
        "redis.Redis.from_url(u)\n"
    )
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    assert sum(_is_httpx_call(c) for c in calls) == 1
    assert sum(_is_httpx_client(c) for c in calls) == 1
    assert sum(_is_redis_from_url(c) for c in calls) == 1


def test_豁免名单只许变小():
    assert len(EXEMPTIONS) == 0, (
        f"出网超时豁免应为 0 项，现有 {len(EXEMPTIONS)} 项；"
        "新增豁免须在此写明理由，且总数只许变少"
    )


@pytest.mark.parametrize(
    "module,method",
    [("app/sms.py", "post"), ("app/payments.py", "post"), ("app/wechat.py", "get")],
)
def test_已知出网点确实被扫到(module, method):
    """反向自检：守卫的分母不能悄悄缩水到 0（比如有人把 `import httpx` 改成
    `from httpx import post`，识别函数就再也匹配不上了，而测试照样绿）。"""
    path = APP.parent / module
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    hits = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call) and _is_httpx_call(n) and n.func.attr == method
    ]
    assert hits, f"{module} 里的 httpx.{method} 没有被守卫扫到，识别函数已经失效"
