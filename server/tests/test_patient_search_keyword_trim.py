"""患者检索的关键词去首尾空白：证件号、姓名、卡号前后多一个空格就 0 条（P2-1725，第五十一批扫描 AO2-6）。

`search_patients` 原先拿关键词原样比：完整证件号命中 1 条，同一个号首尾带一个空格就是 0 条；`李检索 `、`EHC… ` 同样 0 条——
窗口以为此人没建过档。同文件按卡号取档（`find_by_ehc_no`，P2-791）、同页「按卡号精确查」早就去首尾空白。修后后端去首尾空白
（去完为空按没传）；页面检索框提交前 trim()。加密开、关两态证件号走的是两条路（开态只认索引等值），两态都测。
"""
import shutil
from urllib.parse import quote

import pytest
from patients_page import run

from app.config import settings

CARD = "330782199205051725"


@pytest.fixture(params=[False, True], ids=["加密关", "加密开"])
def enc(request, monkeypatch):
    monkeypatch.setattr(settings, "pii_encryption_enabled", request.param)
    return request.param


@pytest.fixture(scope="module")
def made(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "李检索", "id_card": CARD})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _search(client, admin, keyword):
    resp = client.get("/api/patients", headers=admin, params={"keyword": keyword})
    assert resp.status_code == 200, resp.text
    return resp


@pytest.mark.parametrize("pad", [" {} ", "{} ", " {}", "\t{}　"], ids=["两头", "尾随", "前导", "制表与全角空格"])
@pytest.mark.parametrize("field", ["id_card", "name", "ehc_no"], ids=["证件号", "姓名", "健康卡号"])
def test_关键词首尾带空白_照样命中(client, admin, made, enc, field, pad):
    hits = _search(client, admin, pad.format(made[field] if field != "id_card" else CARD)).json()
    assert [p["id"] for p in hits] == [made["id"]], hits   # 修前 0 条


def test_只有空白的关键词等于没传(client, admin, made, enc):
    blank, plain = _search(client, admin, " 　 "), _search(client, admin, "")
    assert blank.json() == plain.json() and blank.headers["x-total-count"] == plain.headers["x-total-count"]
    assert made["id"] in [p["id"] for p in blank.json()]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_页面检索框提交前去首尾空白(client, admin, made):
    steps = """
      await renderPatients();
      await submitForm("#patient-search", { keyword: "  李检索 \\u3000" });
      return htmlOf("#patient-table");
    """
    html, requests = run(client, admin, steps)
    assert requests[-1] == ("GET", f"/api/patients?keyword={quote('李检索', safe='')}"), requests   # 修前带着 %20 送
    assert made["ehc_no"] in html
