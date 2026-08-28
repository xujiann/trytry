"""智能导诊的入口与瘦身（功能指引 ⑨ 收口）。

后端 `/api/triage/suggest` 早已存在且生而全契约，但**没有任何前端调用它**
——第四轮审阅记为"未接入但合理"，一挂就是几轮。现接进预约诊疗页的
「智能导诊台」面板（导诊→预约本就是分诊工位的同一串动作）。

顺手清掉登记过的小账：`triage_suggest` 签名里挂着 `Depends(get_db)` 却一次
都没用——每次调用白白从连接池借还一条连接，高峰期还占 `db_pool_timeout`
的名额。
"""
import inspect

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers import triage


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:   # 进上下文才跑 lifespan，admin 账号在那里种
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_症状命中给出科室与急症提示(client, admin):
    body = client.post("/api/triage/suggest", json=["胸痛", "心悸"], headers=admin).json()
    assert body["recommendations"][0]["department"] == "心血管内科"
    assert body["recommendations"][0]["urgent"] is True
    assert body["emergency_hint"] is True


def test_零命中回落全科门诊而不是空列表(client, admin):
    """居民永远拿得到一个可去的地方，比诚实地回空数组有用（模块 docstring 口径）。"""
    body = client.post("/api/triage/suggest", json=["不存在的症状"], headers=admin).json()
    assert body["recommendations"] == [
        {"department": "全科门诊", "matched": [], "urgent": False}
    ]
    assert body["emergency_hint"] is False


def test_前端入口已接上():
    """村医二维码 404 的教训：验可达，不只验代码存在——面板、调用、结果容器都得在。"""
    from pathlib import Path

    core = (Path(__file__).resolve().parent.parent / "app" / "static" / "core.js").read_text("utf-8")
    assert "api/triage/suggest" in core, "预约诊疗页没有调导诊接口，端点仍是孤儿"
    assert "triage-form" in core and "triage-result" in core
    assert "智能导诊台" in core


def test_不再白借数据库连接():
    """签名里不得再出现从未使用的 db 依赖——那不是预留，是每请求一次的连接池税。"""
    params = inspect.signature(triage.triage_suggest).parameters
    assert "db" not in params, (
        "triage_suggest 又挂上了 db 依赖；知识库仍是模块常量时不该借连接，"
        "真要落表请连同查询代码一起来"
    )
