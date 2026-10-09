"""交接班不填交班人就整条无署名（P2-1769，第五十二批扫描 AP3-6）。

`POST /api/inpatient/handovers` 的交班人、接班人、内容三项缺省空串、原样落库，出参与清单又不带录入账号；同文件病程、护理、
体征的署名留空都取登录人，门急诊护理同形已修（P2-1308）。修前实测：交班人、接班人全空的三条交接班都 201，清单
`'from_staff': '', 'to_staff': ''`、页面印「— → —」，事后谁交的，界面和接口出参里都查不到。

修法：交班人留空取登录人的姓名、账号没填姓名的取用户名（`user.full_name or user.username`）；填了的照存。接班人是否必填、要不要
接班人确认不在本条（随 P2-280）。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21769 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21769 内科病区"}).json()["id"]
    users = {}
    for username, full_name in (("p21769_nurse", "P21769 王护士"), ("p21769_noname", "")):
        created = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": "operator", "org_id": org, "full_name": full_name})
        assert created.status_code in (200, 201), created.text
        users[username] = login(client, username, "passw0rd1")
    return {"ward": ward, **users}


def _handover(client, headers, ward, **fields):
    created = client.post("/api/inpatient/handovers", headers=headers, json={
        "ward_id": ward, "shift": "day", "handover_date": "2026-10-09", **fields})
    assert created.status_code == 201, created.text
    rows = client.get("/api/inpatient/handovers", headers=headers, params={"ward_id": ward}).json()
    return next(r for r in rows if r["id"] == created.json()["id"])


def test_不填交班人存登录人的姓名_清单读回同值(client, world):
    row = _handover(client, world["p21769_nurse"], world["ward"], content="P21769 3床发热")
    assert row["from_staff"] == "P21769 王护士"   # 修前空串，清单印「— → —」
    assert row["to_staff"] == ""   # 接班人留空照旧（随 P2-280）


def test_账号没填姓名的取用户名(client, world):
    row = _handover(client, world["p21769_noname"], world["ward"])
    assert row["from_staff"] == "p21769_noname"


def test_填了交班人的照存(client, world):
    row = _handover(client, world["p21769_nurse"], world["ward"], from_staff="P21769 李护士", to_staff="P21769 赵护士")
    assert (row["from_staff"], row["to_staff"]) == ("P21769 李护士", "P21769 赵护士")


def test_新建回执的键不变(client, world):
    created = client.post("/api/inpatient/handovers", headers=world["p21769_nurse"], json={
        "ward_id": world["ward"], "shift": "night", "handover_date": "2026-10-09"})
    assert set(created.json()) == {"id", "ward_id", "shift", "handover_date", "patient_count", "critical_count"}
