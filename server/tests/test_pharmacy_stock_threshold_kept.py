"""补货不再把缺药预警阈值抹成 0（P1-146）。

`POST /api/pharmacy/stocks` 是入库 / 补货的那条路径，也是全平台唯一写 `DrugStock.threshold` 的地方。原先入参
阈值缺省 0、每次照写，页面又把阈值框预填 0 并照送——配好阈值 100 的药，补一次货阈值就成了 0，库存掉到 10 也
不出缺药预警（预警、待办、调拨广播、供应风险全靠这个数）。现在阈值不传 = 不改，新建的库存记 0；页面留空不送。
"""
import os

import pytest

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P1146 乡卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _stock_in(client, admin, org, quantity, **extra):
    resp = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": org, "drug_code": "C08CA01", "drug_name": "氨氯地平片", "quantity": quantity, **extra})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


def test_补货不传阈值_配好的阈值留着_缺药照样预警(client, admin, org):
    assert _stock_in(client, admin, org, 5)["threshold"] == 0          # 新建不传阈值记 0
    assert _stock_in(client, admin, org, 5, threshold=100)["threshold"] == 100
    assert _stock_in(client, admin, org, 20)["threshold"] == 100      # 修前补一次货阈值成 0
    alerts = client.get(f"/api/pharmacy/alerts?org_id={org}", headers=admin).json()
    assert [(a["drug_code"], a["quantity"], a["threshold"]) for a in alerts] == [("C08CA01", 30, 100)]


def test_明确传阈值照改_包括改成0(client, admin, org):
    assert _stock_in(client, admin, org, 0, threshold=40)["threshold"] == 40
    assert _stock_in(client, admin, org, 0, threshold=0)["threshold"] == 0


def test_入库表单阈值框不预填0_留空不送():
    with open(os.path.join(STATIC, "core.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index('id="stock-form"')
    form = source[start:source.index("</form>", start)]
    field = form[form.index('name="threshold"'):]
    field = field[:field.index(">")]
    assert 'value="0"' not in field                                    # 修前预填 0
    handler = source[source.index('$("#stock-form").onsubmit'):]
    handler = handler[:handler.index('$("#batch-form").onsubmit')]
    assert 'threshold: Number(f.get("threshold"))' not in handler      # 修前照送
    assert 'if (f.get("threshold") !== "") body.threshold' in handler
