"""智能导诊：不读库的 handler 不许挂着死的 `db` 参数（童子军级回归）。

`triage_suggest` 曾写着 `db: Session = Depends(get_db)` 却一次都没用到，像是
"将来知识库落表"的占位（模块 docstring 明写硬编码知识库是现状不是设计）。

**把代价说准**：它并没有多借一条连接。FastAPI 的依赖默认 `use_cache=True`，
而路由级的 `get_current_user` 本身就依赖 `get_db`，同一请求里只解析一次——
实测带与不带那个参数，一次请求都只开 1 条 Session。所以这是**清死参数**，
不是省连接；下面的断言也只盯 handler 自己的签名，不去断言"整条链路不碰库"
（那是假的，鉴权真的要读 users）。

行为面（命中科室、无命中回落全科门诊、急症提示）已由
`test_service_extras_split_contract.py` 与 `test_extras.py` 钉住。
"""
import inspect

from fastapi.params import Depends as DependsParam

from app.database import get_db
from app.deps import get_current_user
from app.routers import triage


def _depends_calls(func) -> set:
    """handler 签名里**直接**声明的依赖可调用对象。"""
    return {
        p.default.dependency
        for p in inspect.signature(func).parameters.values()
        if isinstance(p.default, DependsParam) and p.default.dependency is not None
    }


def test_导诊handler不声明未使用的数据库会话():
    assert get_db not in _depends_calls(triage.triage_suggest), (
        "triage_suggest 又挂上了 get_db——它一行库都不读，是个死参数。"
        "知识库真落表时，连 Depends 带查询一起加。"
    )


def test_守卫本身不是空的():
    """同一把尺子量一个**确实声明了** `get_db` 的函数，确认它量得出来。

    否则 `_depends_calls` 写错（拿错字段、判错类型）时上面那条恒绿。
    """
    assert get_db in _depends_calls(get_current_user)
