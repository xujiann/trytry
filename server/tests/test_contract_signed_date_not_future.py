"""家医签约的签约日期不得晚于今天（P2-1548，第四十五批扫描 AI1-3 的 clear 那半）。

修前：`contracts.sign` 的签约日期只校验格式（`OptionalDateStr`）——扫描实测（`r3_services.py`）签约日填 2099-01-01：201、
状态当即「履约中」，这份将来才生效的协议上记履约照样 201。同机构曾解约的再签走「重新激活既有行」那条路（改写那一行的
签约日期），同样不判。

修法：照接种日期（`vaccination.py` 的 P2-1304）、发病日期（P2-454）同一句——非空且晚于业务日的 422，判在新建与重新激活
两条路之前；留空照旧（能不能留空随签约期 P2-1037 另定）。服务日期、履约人两列要加列、要业务拍板，不在本条。
"""
from datetime import date

import pytest

from app.database import SessionLocal
from app.models import FamilyDoctorContract
from conftest import freeze_business_date

#: 冻住的业务日：判据与被判对象取同一个「今天」，用例不怕跨午夜
TODAY = date(2026, 3, 1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21548 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = []
    for i in range(4):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P21548 居民{i}", "id_card": f"33010219700301{1548 + i:04d}"})
        assert resp.status_code == 201, resp.text
        patients.append(resp.json()["id"])
    return {"org": org, "patients": patients}


def _sign(client, admin, world, who, signed_date, doctor="P21548 家庭医生"):
    return client.post("/api/contracts", headers=admin, json={
        "patient_id": world["patients"][who], "org_id": world["org"], "doctor_name": doctor,
        "signed_date": signed_date})


def test_签约日期晚于今天_422_不落库(client, admin, world):
    with freeze_business_date(TODAY):
        resp = _sign(client, admin, world, 0, "2026-03-02")
    assert resp.status_code == 422, resp.text   # 修前 201，状态当即「履约中」
    assert resp.json()["detail"] == "签约日期（2026-03-02）不得晚于今天"
    assert client.get("/api/contracts", headers=admin, params={"patient_id": world["patients"][0]}).json() == []


def test_签约日期是今天或留空_照旧201(client, admin, world):
    with freeze_business_date(TODAY):
        today = _sign(client, admin, world, 1, "2026-03-01")
        blank = _sign(client, admin, world, 2, "")
    assert today.status_code == 201 and today.json()["signed_date"] == "2026-03-01", today.text
    assert blank.status_code == 201 and blank.json()["signed_date"] == "", blank.text


def test_同机构重签带将来日期_同样422_既有协议一个字不改(client, admin, world):
    """曾解约的再签走「重新激活既有行」那条路（`uq_contract_patient_org`）：将来日期照样挡，挡下时那一行不动；今天照旧能重签。"""
    with freeze_business_date(TODAY):
        first = _sign(client, admin, world, 3, "2026-02-01")
        assert first.status_code == 201, first.text
        cid = first.json()["id"]
        assert client.post(f"/api/contracts/{cid}/terminate", headers=admin).status_code == 200
        future = _sign(client, admin, world, 3, "2026-03-02", doctor="P21548 另一位医生")
        assert future.status_code == 422, future.text   # 修前 201：已解约的那一行被改回履约中、签约日期改成将来
        assert future.json()["detail"] == "签约日期（2026-03-02）不得晚于今天"
        with SessionLocal() as db:
            row = db.get(FamilyDoctorContract, cid)
            assert (row.status, row.signed_date, row.doctor_name) == ("terminated", "2026-02-01", "P21548 家庭医生")
        again = _sign(client, admin, world, 3, "2026-03-01", doctor="P21548 另一位医生")
    # 今天照旧能重签；重签是改写既有行还是新起一行待裁定（P1-45 / P2-1037），这里不钉
    assert again.status_code == 201 and again.json()["signed_date"] == "2026-03-01", again.text
