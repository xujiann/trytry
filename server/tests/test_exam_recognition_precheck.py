"""开单前互认预检与建单同一套判定：预检说「可互认」的，建单就收得下（P2-145）。

建单侧（`accept_recognition_of`）要求被互认的报告与新申请同一中心类型，且目录的互认范围把**报告的申请机构**
也算进去（县域互认不收县域外机构出的报告）。预检却只取最近一份同项目报告，目录也只拿新申请的机构去判；
页面调预检时连中心类型、申请机构都不带。于是：
- 最近那份若出自县域外机构（或是别的中心类型），页面照样弹「可互认」，医生选了互认，建单 422；
- 更早一份本可互认的报告被它挡住，这张单只能不互认、重复检查。

修法：预检逐份看 30 天内的同项目报告，取第一份按建单口径可互认的；页面把中心类型与申请机构一并送去预检。
"""
import pytest

E = "/api/exams"


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name, level, org_type):
        return client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": org_type, "level": level}).json()

    town = org("P2145 卫生院", "township", "township")
    city = org("P2145 市医院", "city", "lead_hospital")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2145 患者", "id_card": "330106197212121450", "gender": "女"}).json()["id"]
    for body in ({"item_code": "P2145-CT", "item_name": "胸部CT(P2145)", "center_type": "imaging",
                  "mutual_scope": "county"},
                 {"item_code": "P2145-US", "item_name": "腹部彩超(P2145)", "center_type": "imaging",
                  "mutual_scope": "city"}):
        created = client.post(f"{E}/recognition-items", headers=admin, json=body)
        assert created.status_code == 201, created.text
    return {"town": town, "city": city, "patient": patient}


def _reported(client, admin, world, org, item_code, center_type):
    req = client.post(E, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": org["id"], "center_type": center_type,
        "item_code": item_code, "item_name": item_code})
    assert req.status_code == 201, req.text
    client.post(f"{E}/{req.json()['id']}/claim", headers=admin)
    report = client.post(f"{E}/{req.json()['id']}/report", headers=admin, json={"conclusion": f"{org['name']} 的报告"})
    assert report.status_code in (200, 201), report.text
    return req.json()["id"]


def _check(client, admin, world, item_code, center_type):
    resp = client.get(f"{E}/recognition-check", headers=admin, params={
        "patient_id": world["patient"], "item_code": item_code, "center_type": center_type,
        "from_org_id": world["town"]["id"]})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _accept(client, admin, world, item_code, center_type, source_id):
    return client.post(E, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["town"]["id"], "center_type": center_type,
        "item_code": item_code, "item_name": item_code, "accept_recognition_of": source_id})


def test_最近一份出自县域外机构_预检给县域内那份_建单收得下(client, admin, world):
    inside = _reported(client, admin, world, world["town"], "P2145-CT", "imaging")
    _reported(client, admin, world, world["city"], "P2145-CT", "imaging")   # 更近的一份，县域外
    check = _check(client, admin, world, "P2145-CT", "imaging")
    assert check["recognizable"] is True
    assert check["request_id"] == inside   # 修前给的是县域外那份，建单 422「该项目互认范围为县域…」
    accepted = _accept(client, admin, world, "P2145-CT", "imaging", check["request_id"])
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["status"] == "recognized"


def test_最近一份是别的中心类型_预检给同中心那份(client, admin, world):
    same = _reported(client, admin, world, world["town"], "P2145-US", "imaging")
    _reported(client, admin, world, world["town"], "P2145-US", "ecg")
    check = _check(client, admin, world, "P2145-US", "imaging")
    assert check["request_id"] == same   # 修前给的是心电那份，建单 422「互认必须是同一中心类型的检查项目」
    assert _accept(client, admin, world, "P2145-US", "imaging", check["request_id"]).status_code == 201


def test_没有一份可互认的_预检不弹(client, admin, world):
    """只有县域外机构出的报告：不提示互认（修前提示，建单 422）。"""
    other = client.post("/api/patients", headers=admin, json={
        "name": "P2145 患者二", "id_card": "330106197212121469", "gender": "男"}).json()["id"]
    _reported(client, admin, {**world, "patient": other}, world["city"], "P2145-CT", "imaging")
    assert _check(client, admin, {**world, "patient": other}, "P2145-CT", "imaging") == {"recognizable": False}
