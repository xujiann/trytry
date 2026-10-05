"""通用资源的单位建好就改不了：PATCH 回 200 却不改（P2-1508，第四十四批扫描 AH3-10 改单位的那一半）。

改档入参 `resources.ResourceUpdate` 原先只有名称、容量、位置、联系方式、备注，没有 `unit`；`update_resource` 只认入参模型里的键，
不认识的被 pydantic 丢掉——建档时单位误填成「人」，`PATCH {"unit": "台"}` 照回 200、单位还是「人」，页面编辑框里也没有这一项。
扫描实测：改单位 200，回执 `unit` 仍是「人」。

修法：`ResourceUpdate` 补 `unit`（约束照建档的 `ResourceIn`：最长 16 字，可空串），编辑框加「单位（留空不改）」一项、留空不送。
「通用设备挂不挂物资台账、有领域表的资源不进这张表怎么拦」要业务拍板，不在本条。
"""
import re
from pathlib import Path

import pytest

from app.routers.resources import ResourceIn, ResourceUpdate

R = "/api/resources"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def resource(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21508 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    created = client.post(R, headers=admin, json={"org_id": org, "resource_type": "equipment", "code": "P21508-EQ",
                                                  "name": "P21508 便携投影仪", "unit": "人", "location": "设备科"})
    assert created.status_code == 201, created.text
    return created.json()


def _read_back(client, admin, rid):
    return next(r for r in client.get(R, headers=admin).json() if r["id"] == rid)


def test_PATCH单位_读回新值(client, admin, resource):
    patched = client.patch(f"{R}/{resource['id']}", headers=admin, json={"unit": "台"})
    assert (patched.status_code, patched.json()["unit"]) == (200, "台"), patched.text   # 修前 200 却还是「人」
    assert _read_back(client, admin, resource["id"])["unit"] == "台"


def test_不带单位的PATCH照旧不动它(client, admin, resource):
    before = _read_back(client, admin, resource["id"])
    patched = client.patch(f"{R}/{resource['id']}", headers=admin, json={"location": "设备科二楼"})
    assert patched.status_code == 200, patched.text
    assert _read_back(client, admin, resource["id"]) == {**before, "location": "设备科二楼"}


def test_单位的约束与建档同口径_超长422且不改(client, admin, resource):
    assert ResourceUpdate.model_fields["unit"].metadata == ResourceIn.model_fields["unit"].metadata   # 最长 16 字
    before = _read_back(client, admin, resource["id"])
    too_long = client.patch(f"{R}/{resource['id']}", headers=admin, json={"unit": "台" * 17})
    assert too_long.status_code == 422, too_long.text
    assert _read_back(client, admin, resource["id"]) == before


def test_编辑框覆盖改档入参的每一项_留空不送():
    """按改档入参派生：入参有的键，编辑框里都有一项、提交时都按「填了才送」带上——以后入参再加一项，页面没跟上即红。"""
    assert "unit" in ResourceUpdate.model_fields   # 修前入参没有这一项
    source = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderResources()")
    page = source[start:source.index("\nasync function ", start + 1)]
    edit = page[page.index("if (d.rsedit) {"):]
    edit = edit[:edit.index("if (done) route();")]
    fields = set(re.findall(r'\{ name: "(\w+)", label:', edit))
    sent = set(re.findall(r"if \(picked\.(\w+)\) body\.\1 = picked\.\1;", edit))
    assert fields == sent == set(ResourceUpdate.model_fields), (fields, sent)
    assert "单位" in page[page.index('<p class="desc">编辑改的是'):][:200]   # 说明文字跟着列上单位
