"""用血页入库、申请两张表单的血型、成分下拉不再缺省落在第一项（P2-1468，第四十三批扫描 AG1-2）。

修前两个下拉都不给空项，浏览器缺省选中第一项：不动它直接提交就是「A」「红细胞」，后端照收（血型、成分只查取值范围）——
医师忘了改血型，申请就成了 A 型（发血又不核对受血者血型，见 AG1-1，照样发得出去）；血库经办忘了改，B 型血记进 A 型的账。

修法照消毒供应申领机构（P2-1443）：两张表单的血型、成分下拉首项为空（「请选择血型」「请选择成分」）、`required`，没选
浏览器不让交。后端判定不动：血型、成分本就必填，绕过页面不送的照旧 422、不落库。页面函数原样拿到 node 里跑（夹具见
`tests/blood_page.py`）。
"""
import shutil

import pytest

from blood_page import form_selects, render

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

BLOOD_TYPES = [("", "请选择血型", False), ("A", "A", False), ("B", "B", False), ("AB", "AB", False), ("O", "O", False)]
COMPONENTS = [("", "请选择成分", False), ("rbc", "红细胞", False), ("plasma", "血浆", False), ("platelet", "血小板", False)]


@needs_node
@pytest.mark.parametrize("role, form, other", [("operator", "bs-form", "br-form"), ("doctor", "br-form", "bs-form")])
def test_血型成分下拉首项为空且必选(role, form, other):
    """血库经办的入库表单、医师的用血申请表单：两个下拉都以空项开头、没有预选项（浏览器就落在空项上）、`required`。"""
    html = render(role)
    got = form_selects(html, form)
    # 修前：(False, [("A", "A", False), …]) 与 (False, [("rbc", "红细胞", False), …])——不动下拉交上去就是 A 型红细胞
    assert got["blood_type"] == (True, BLOOD_TYPES), got
    assert got["component"] == (True, COMPONENTS), got
    assert f'id="{other}"' not in html   # 两张表单仍按角色各画各的


@needs_node
def test_平台管理员两张表单都画_同一套空项():
    html = render("admin")
    for form in ("bs-form", "br-form"):
        assert form_selects(html, form) == {"blood_type": (True, BLOOD_TYPES), "component": (True, COMPONENTS)}, form


def test_不选血型成分_后端不替人缺省(client, admin):
    """空项交不出去之外，绕过页面不送血型 / 成分的，后端也不替人落成 A / 红细胞：422，库存与申请都不落。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21468 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21468 患者", "id_card": "330127197304051468"}).json()["id"]
    stocks = client.get("/api/blood/stocks", headers=admin).json()
    for body in ({"component": "rbc", "quantity_ml": 400}, {"blood_type": "B", "quantity_ml": 400}):
        resp = client.post("/api/blood/stocks", headers=admin, json=body)
        assert resp.status_code == 422, (body, resp.text)
    assert client.get("/api/blood/stocks", headers=admin).json() == stocks
    requests = client.get("/api/blood/requests", headers=admin).json()
    for missing in ("blood_type", "component"):
        body = {"patient_id": patient, "org_id": org, "blood_type": "B", "component": "plasma", "quantity_ml": 200}
        body.pop(missing)
        resp = client.post("/api/blood/requests", headers=admin, json=body)
        assert resp.status_code == 422, (body, resp.text)
    assert client.get("/api/blood/requests", headers=admin).json() == requests
