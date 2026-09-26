"""数据源的状态文案取自后端：停用的显示「停用」，不再显示最近一次同步留下的「正常」（P2-174）。

运行中枢的数据源表只看 `status` 一列：`running` 正常、`delayed` 延迟、其余一律「异常」。页面上「编辑 → 停用」改的是
`active`，`status` 不动——停用的数据源照旧显示「正常」；`status=stopped`（列注释：停用）又落进「其余一律异常」，
标红当成故障。§13 要求状态文案取自后端：出参补 `status_name`，页面照它显示。
"""
import os

import pytest

B = "/api/spd"
STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(scope="module")
def sources(client, admin):
    ids = {}
    for code in ("P2174_A", "P2174_B"):
        resp = client.post(f"{B}/data-sources", headers=admin, json={
            "code": code, "name": f"P2174 {code}", "source_type": "HIS", "freq_minutes": 60})
        assert resp.status_code == 201, resp.text
        ids[code] = resp.json()["id"]
    return ids


def _listed(client, admin):
    return {s["code"]: s for s in client.get(f"{B}/data-sources", headers=admin).json()}


def test_停用的显示停用_不显示最近一次同步的正常(client, admin, sources):
    assert _listed(client, admin)["P2174_A"]["status_name"] == "正常"
    patched = client.patch(f"{B}/data-sources/{sources['P2174_A']}", headers=admin, json={"active": False})
    assert patched.status_code == 200, patched.text
    row = _listed(client, admin)["P2174_A"]
    assert (row["active"], row["status"], row["status_name"]) == (False, "running", "停用")


def test_状态码逐个对上列注释的文案(client, admin, sources):
    for status, name in (("stopped", "停用"), ("failed", "异常"), ("delayed", "延迟"), ("running", "正常")):
        resp = client.patch(f"{B}/data-sources/{sources['P2174_B']}", headers=admin, json={"status": status})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status_name"] == name, status   # 修前没有这一项，页面把 stopped 标成红色「异常」


def test_页面照后端文案显示_不自己按状态码猜():
    with open(os.path.join(STATIC, "pages-spd.js"), encoding="utf-8") as fh:
        source = fh.read()
    assert "${esc(src.status_name)}" in source
    assert "'<span class=\"tag red\">异常</span>'}" not in source
