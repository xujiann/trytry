"""审方按药品编码找规则认得出同一个编码的不同写法：小写、首尾空白、全角都不再「系统审通过」（P1-218，第二十一批「同一个值
的不同写法」扫描 N3-1）。

`_active_rule` 原先按 `drug_code ==` 原样取规则，取不到就当「规则库里没有」、这味药一项都不判；药品编码在开方页是手输框。
实测：华法林 30mg（上限 10mg）编码写 `B01AA03` 转药师审，写 `b01aa03` / `'B01AA03 '` / `' B01AA03'` / `Ｂ０１ＡＡ０３` 全部系统审
通过；华法林配小写的阿司匹林 `b01ac06` 不报相互作用；同一张方里 `B01AA03` 与 `b01aa03` 并存不算同方重复。

修法：比对时两侧都过 `texttypes.code_key`（全角转半角、去首尾空白、大写）——原样命中的照旧，只在对不上时多认一种写法，
审方只会更严。落库的编码不动（规范写法怎么定、统计与发药按编码关联的另行登记）。
"""
import pytest

from conftest import login

from app.texttypes import code_key

WARFARIN, ASPIRIN = "P1218W01", "P1218A01"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1218 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/users", headers=admin, json={
        "username": "p1218_doc", "password": "passw0rd1", "full_name": "P1218 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1218 患者", "id_card": "330102195001011218"}).json()["id"]
    for body in ({"drug_code": WARFARIN, "max_daily_dose": 10, "dose_unit": "mg", "interactions": ASPIRIN,
                  "contraindicated_diagnoses": "消化道出血"},
                 {"drug_code": ASPIRIN, "max_daily_dose": 300, "dose_unit": "mg"}):
        created = client.post("/api/prescriptions/rules", headers=admin, json=body)
        assert created.status_code == 201, created.text
    return {"org": org, "patient": patient, "doctor": login(client, "p1218_doc", "passw0rd1")}


def _rx(client, world, items, diagnosis="心房颤动"):
    resp = client.post("/api/prescriptions", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "diagnosis_name": diagnosis, "items": items})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_比对键_全角空白大小写归一():
    assert code_key(" ｂ０１ａａ０３ ") == code_key("B01AA03") == "B01AA03"


@pytest.mark.parametrize("code", [WARFARIN.lower(), WARFARIN + " ", " " + WARFARIN, "Ｐ１２１８Ｗ０１"],
                         ids=["小写", "尾随空格", "前导空格", "全角"])
def test_写法不同的编码照样按规则审_超量转药师(client, world, code):
    rx = _rx(client, world, [{"drug_code": code, "drug_name": "华法林", "daily_dose": 30}])
    assert rx["status"] == "pending_review", rx   # 修前 auto_passed
    assert "超过上限" in rx["review_comment"]


def test_相互作用两侧按比对键认(client, world):
    rx = _rx(client, world, [{"drug_code": WARFARIN, "drug_name": "华法林", "daily_dose": 3},
                             {"drug_code": ASPIRIN.lower(), "drug_name": "阿司匹林", "daily_dose": 100}])
    assert rx["status"] == "pending_review" and "药物相互作用" in rx["review_comment"], rx   # 修前 auto_passed


def test_同一张方里大小写不同的同一个编码算同方重复(client, world):
    rx = _rx(client, world, [{"drug_code": WARFARIN, "drug_name": "华法林", "daily_dose": 3},
                             {"drug_code": WARFARIN.lower(), "drug_name": "华法林钠片", "daily_dose": 2}])
    assert rx["status"] == "pending_review" and "同方重复药品" in rx["review_comment"], rx   # 修前 auto_passed


def test_禁忌诊断对写法不同的编码照样判(client, world):
    rx = _rx(client, world, [{"drug_code": WARFARIN.lower(), "drug_name": "华法林", "daily_dose": 3}], diagnosis="消化道出血")
    assert rx["status"] == "pending_review" and "禁忌诊断" in rx["review_comment"], rx   # 修前 auto_passed


def test_原样编码照旧_规则外的药照旧系统审通过(client, world):
    ok = _rx(client, world, [{"drug_code": WARFARIN, "drug_name": "华法林", "daily_dose": 3}])
    assert ok["status"] == "auto_passed", ok
    unknown = _rx(client, world, [{"drug_code": "P1218-NORULE", "drug_name": "维生素C", "daily_dose": 300}])
    assert unknown["status"] == "auto_passed", unknown
