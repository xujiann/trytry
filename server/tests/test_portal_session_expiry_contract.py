"""P2-22：居民端靠**中文文案**判断掉线，后端改一句话就静默失效。

原实现是两段配合出来的：

    // api()
    if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
    // authApi
    if (/401|登录状态无效|请先登录|已退出登录|账户不存在/.test(err.message)) { 清本地; 回登录页 }

**状态码在 api() 里就地丢了**，于是 authApi 只能拿人看的文案去反推机器该做的事。
两个后果：

1. 后端把 `detail` 改一个字（"请先登录" → "登录已过期，请重新登录"），这条正则
   不再命中——居民端**不清本地登录态、也不回登录页**，只弹一句看不懂的错误，
   而且**没有任何一处会报这个失效**：它不抛异常、不变红、不写日志。
2. 正则里那个 `401` 分支形同虚设——它只在后端**不给 detail** 时才命中
   （那时消息才是 `请求失败(401)`）；只要给了 detail，状态码早就没了。
   也就是说「按状态码兜底」这层保险，实际上从来没生效过。

修法是把状态码挂在错误上、只认它。**这不是发明新约定，是把异类拉齐**：
管理端 `core.js:35` 与医生端 `doctor.js:30` 本来就是先判 `resp.status === 401`，
`shared.js` 的注释也早写着三套的 401 处理时机各不相同。居民端是唯一一个按文案判的。

本文件两条守卫，一条守前端形状、一条守后端契约——**跨层的假设必须两头都钉**，
只钉一头的话，另一头改了照样静默失效（这正是原缺陷的成因）。
"""
import ast
import os
import re

PORTAL_JS = os.path.join(
    os.path.dirname(__file__), "..", "app", "static", "m", "m.js"
)
PORTAL_PY = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "portal.py"
)

#: 原正则里那几个中文片段。它们**本身没问题**（后端确实这么回），
#: 有问题的是拿它们当判定依据。
_COPY_PROBES = ("登录状态无效", "请先登录", "已退出登录", "账户不存在")


def _strip_comments(src: str) -> str:
    """去掉 `//` 行注释与 `/* */` 块注释——**注释不是代码**。

    这条不是预防性的：本文件第一版就栽在这儿。修完之后我在 `authApi` 的注释里
    原样引了旧正则来说明改了什么，于是守卫在**注释里**看见了那几个中文片段，
    当场判定「又按文案判了」。同一形状本轮已经是第六次
    （越权扫描漏 `require_admin`、分页棘轮把 docstring 当代码、收口统计漏
    `assert_patient_visible`、分类漏别名归属列、分类漏局部辅助函数、这次）。
    仓库里 `test_frontend_panel_component.py:336` 早有同名同实现的helper，
    也是被同一个坑逼出来的。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return "\n".join(re.sub(r"//.*$", "", line) for line in src.splitlines())


def test_居民端不得再按文案判断掉线():
    """前端形状：`authApi` 的掉线分支必须认状态码，不许认文案。"""
    src = _strip_comments(open(PORTAL_JS, encoding="utf-8").read())
    body = src[src.index("async function authApi") :]
    body = body[: body.index("\n}")]
    assert "err.status === 401" in body, (
        "authApi 的掉线判定没有按状态码——改回按文案判，后端改一句话就会静默失效"
    )
    for probe in _COPY_PROBES:
        assert f"/{probe}" not in body and f"|{probe}" not in body, (
            f"authApi 又拿中文文案 {probe!r} 当判定依据了；文案是给人看的，不是接口契约"
        )


def test_api必须把状态码挂在错误上():
    """`api()` 丢掉状态码，上层就只剩文案可用——这是原缺陷的根。"""
    src = _strip_comments(open(PORTAL_JS, encoding="utf-8").read())
    body = src[src.index("async function api(") :]
    body = body[: body.index("\n}")]
    assert "err.status = resp.status" in body, "api() 又把状态码丢了"


def test_居民端登录失效一律回401而不是403():
    """后端契约：`current_resident` 的每一条失败路径都必须是 401。

    前端现在按状态码判掉线，所以「哪些情况算掉线」这件事从文案挪到了状态码上。
    其中任何一条改成 403（或别的码），居民端就不会清本地登录态、不会回登录页
    ——**和原来改文案的后果一模一样，只是换了个地方失效**。故两头都钉。

    用 AST 读而不是跑 HTTP：这样连「账户已停用」这种要造脏数据才走得到的
    分支也一并覆盖，不必为每条 401 造一次场景。
    """
    tree = ast.parse(open(PORTAL_PY, encoding="utf-8").read())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "current_resident"
    )
    raises = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Raise) and "HTTPException" in ast.unparse(n)
    ]
    assert raises, "current_resident 里一条 raise 都没有？扫描退化了"
    bad = [ast.unparse(n) for n in raises if "401" not in ast.unparse(n)]
    assert not bad, (
        "current_resident 有失败路径不是 401，居民端的掉线处理会漏掉它：\n  "
        + "\n  ".join(bad)
    )


def test_三套前端的401判定都不靠文案():
    """撞见一处就按形状全量扫（本仓库既有规矩）。

    管理端与医生端本来就是先判 `resp.status === 401`，这条钉住它们别退回去，
    顺带说明居民端此前是**唯一**的异类。
    """
    base = os.path.join(os.path.dirname(__file__), "..", "app", "static")
    for rel in ("core.js", os.path.join("m", "doctor.js")):
        src = open(os.path.join(base, rel), encoding="utf-8").read()
        assert re.search(r"resp\.status === 401", src), (
            f"{rel} 的 401 判定不再按状态码了"
        )
