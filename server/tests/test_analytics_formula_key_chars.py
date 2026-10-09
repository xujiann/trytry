"""绩效公式编码拒收停用路由寻址不到的写法：含「/」的、全是「.」的新建 422（P2-1706，第五十批扫描 AN3-6）。

`FormulaIn.key` 原先只要求非空白：含「/」的（`up/down`）、「.」「..」都建得出来。停用 `DELETE /formulas/{key}` 用缺省
str 转换器只配一段，页面 `encodeURIComponent` 编出的 `%2F` 在路由匹配前被解回「/」；「.」「..」在客户端就被规范化掉——
修前实测 `up/down`、`..` 停用都是 404 `Not Found`，公式清单里两条仍是启用，一直参与期末综合绩效报告的计分。
修后新建只拒这两种写法，其余照收（大写、点号、连字符照旧）。路由模板不改：改成 `{key:path}` 会牵动权限点编码，
随 P2-1505 定；存量带「/」的只能按 `deactivate_formula` docstring 里的 SQL 停。
"""
import pytest

from app.database import SessionLocal
from app.models import PerformanceFormula


def _create(client, admin, key):
    return client.post("/api/analytics/formulas", headers=admin, json={
        "key": key, "name": f"P21706 {key}", "expression": "referrals_up * 10", "weight": 50})


@pytest.mark.parametrize("key", ["up/down", "/", "a/../b", "..", ".", "...", " . ", "​.​"])
def test_停用寻址不到的编码_新建422且不落库(client, admin, key):
    got = _create(client, admin, key)
    assert got.status_code == 422, got.text   # 修前 201，停用 404
    with SessionLocal() as db:
        assert db.query(PerformanceFormula).filter(PerformanceFormula.key == key).count() == 0


@pytest.mark.parametrize("key", ["x", "up_rate", "Up_Rate", "rx.rej", "ref-up", ".hidden"])
def test_其余编码照收_照常停用(client, admin, key):
    created = _create(client, admin, key)
    assert created.status_code == 201, created.text
    got = client.delete(f"/api/analytics/formulas/{key}", headers=admin)
    assert got.status_code == 200, got.text
    rows = {f["key"]: f["active"] for f in client.get("/api/analytics/formulas", headers=admin).json()}
    assert rows[key] is False


def test_空白编码照旧422(client, admin):
    for key in ("", "   ", "​"):
        assert _create(client, admin, key).status_code == 422, key
