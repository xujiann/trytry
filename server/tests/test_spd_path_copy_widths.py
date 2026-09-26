"""复制路径模板时派生的名称 / 版本号超列宽：真 PG 上即 500；传一串空格照存成空白（P2-314）。

界面上的「复制」什么都不传（`POST …/copy {}`），名称与版本全靠派生：原名 61 字以上加「(副本)」超过名称列宽 64，
自定义版本号 14 字以上加「-r2」超过版本列宽 16——真 PG 上撞列宽即 500（开发库照存超长值）。直接调接口传一串空格，
`body.name or …` 当成填了，存成空白名称。

修法：派生名称截原名让出后缀（名称是给人看的默认值）；派生版本号超宽 422 请人指定（版本号参与「编码 + 版本」唯一约束，
截了会撞别的版本）；传了空白的按没传。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def program(client, admin):
    created = client.post(f"{B}/programs", headers=admin, json={"code": "P2314_PG", "name": "P2314 病种"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _template(client, admin, program, code, name, version):
    created = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program, "code": code, "name": name, "version": version})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_长名称派生副本名_截原名让出后缀_不超列宽(client, admin, program):
    name = "路" * 64
    tpl = _template(client, admin, program, "P2314_A", name, "v1")
    copied = client.post(f"{B}/path-templates/{tpl}/copy", headers=admin, json={})
    assert copied.status_code == 201, copied.text
    assert copied.json()["name"] == "路" * 60 + "(副本)" and len(copied.json()["name"]) == 64   # 修前 68 字
    assert copied.json()["version"] == "v2"


def test_长版本号推不出来_422请人指定_指定了照收(client, admin, program):
    tpl = _template(client, admin, program, "P2314_B", "P2314 路径", "2026Q3-release1")   # 15 字，加 -r2 超宽
    derived = client.post(f"{B}/path-templates/{tpl}/copy", headers=admin, json={})
    assert derived.status_code == 422, derived.text   # 修前开发库照存 18 字、真 PG 撞列宽 500
    assert "请指定新版本号" in derived.json()["detail"]
    given = client.post(f"{B}/path-templates/{tpl}/copy", headers=admin, json={"version": "2026Q4"})
    assert given.status_code == 201 and given.json()["version"] == "2026Q4", given.text


def test_传一串空格按没传(client, admin, program):
    tpl = _template(client, admin, program, "P2314_C", "P2314 糖尿病路径", "v3")
    copied = client.post(f"{B}/path-templates/{tpl}/copy", headers=admin, json={"name": "   ", "version": "  ", "code": " "})
    assert copied.status_code == 201, copied.text
    body = copied.json()
    assert (body["name"], body["version"], body["code"]) == ("P2314 糖尿病路径(副本)", "v4", "P2314_C")   # 修前存成空白
