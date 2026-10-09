"""统一规则编码拒收停用路由寻址不到的写法：含「/」的、全是「.」的新建 422（P2-1737，第五十一批扫描 AO3-6）。

`RuleIn.key` 原先只要求非空白：`rx/elderly`、`.`、`..` 都建得出来。停用 `DELETE /api/rules/{key}` 用缺省 str 转换器只配
一段，页面 `encodeURIComponent` 编出的 `%2F` 在路由匹配前被解回「/」；「.」「..」在客户端就被规范化掉——扫描实测
（`r4_key_slash.py`）拦截级的 `rx/elderly` 建 201、停用 404，age=70 试算照样命中、blocked=true，停用是这里唯一的止损手段。
修后新建只拒这两种写法，其余照收（照抄绩效公式编码 P2-1706 的正则）；路由模板不改（改成 `{key:path}` 会牵动权限点编码，
随 P2-1505 定）；存量带「/」的只能按 `deactivate_rule` docstring 里的 SQL 停。
"""
from urllib.parse import quote

import pytest

from app.database import SessionLocal
from app.models import RuleDefinition


def _create(client, admin, key):
    return client.post("/api/rules", headers=admin, json={
        "key": key, "name": f"P21737 {key}", "domain": "prescription", "condition": "age >= 65", "severity": "error"})


@pytest.mark.parametrize("key", ["rx/elderly", "/", "a/../b", "..", ".", "...", " . ", "\u200b.\u200b"])
def test_停用寻址不到的编码_新建422且不落库(client, admin, key):
    got = _create(client, admin, key)
    assert got.status_code == 422, got.text   # 修前 201，停用 404
    with SessionLocal() as db:
        assert db.query(RuleDefinition).filter(RuleDefinition.key == key).count() == 0


@pytest.mark.parametrize("key", ["Rx.Elderly", "rx-elderly", "rx_elderly", ".hidden", "老年 用药"])
def test_其余编码照收_页面那样编码后照常停用(client, admin, key):
    created = _create(client, admin, key)
    assert created.status_code == 201, created.text
    got = client.delete("/api/rules/" + quote(key, safe=""), headers=admin)   # 页面停用按钮用 encodeURIComponent
    assert got.status_code == 200, got.text
    assert got.json() == {"key": key, "active": False}
    assert {r["key"]: r["active"] for r in client.get("/api/rules", headers=admin).json()}[key] is False
    hits = client.post("/api/rules/evaluate", headers=admin, json={
        "domain": "prescription", "variables": {"age": 70}}).json()["hits"]
    assert key not in {h["key"] for h in hits}   # 停了就不再拦


def test_空白编码照旧422(client, admin):
    for key in ("", "   ", "\u200b"):
        assert _create(client, admin, key).status_code == 422, key
