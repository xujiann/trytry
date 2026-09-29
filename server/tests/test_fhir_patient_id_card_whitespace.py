"""FHIR 入站建档的证件号去首尾空白：上游 CHAR 定长列补的尾随空格、换行原样入库，同一个人另建一份主档（P2-790，第二十一批
「同一个值的不同写法」扫描 N3-4；HL7 一侧 PID-3 早就去了，P2-723）。

`parse_fhir_patient` 原先取 `identifier.value` 原样：`'110101…1239 '` 过得了「至少 15 位」，按证件号幂等建档认不出是
已有的那位，另建一份（新健康卡号）；之后的检查、随访、医保各挂各的档。修后去首尾空白再判、再建档。
全角数字的证件号、15 位老证号与 18 位新证号算不算同一个人，另行登记。
"""
import pytest

from app.database import SessionLocal

ID_CARD_SYSTEM = "urn:oid:2.16.156.10011.1.3"


def _fhir(client, admin, value, name="P2790 张三"):
    return client.post("/api/integration/fhir/Patient", headers=admin, json={
        "resourceType": "Patient", "identifier": [{"system": ID_CARD_SYSTEM, "value": value}],
        "name": [{"text": name}], "gender": "male", "birthDate": "1977-07-07"})


@pytest.mark.parametrize("value", ["330102197707072790 ", "330102197707072790\n", " 330102197707072790"],
                         ids=["尾随空格", "换行", "前导空格"])
def test_证件号带首尾空白_认得出是已有的那位(client, admin, value):
    web = client.post("/api/patients", headers=admin, json={"name": "P2790 张三", "id_card": "330102197707072790"})
    assert web.status_code == 201, web.text
    resp = _fhir(client, admin, value)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["created"] is False, body   # 修前 True：另建一份主档
    assert body["patient"]["ehc_no"] == web.json()["ehc_no"]


def test_新建的档案_证件号不带空白(client, admin):
    from app.models import Patient

    resp = _fhir(client, admin, "  33010219770707279X \n", name="P2790 李四")
    assert resp.status_code == 201 and resp.json()["created"] is True, resp.text
    with SessionLocal() as db:
        stored = db.query(Patient).filter(Patient.ehc_no == resp.json()["patient"]["ehc_no"]).one().id_card
    assert stored == "33010219770707279X"   # 修前原样带着空白入库


def test_只有空白的标识当没有(client, admin):
    resp = _fhir(client, admin, "                  ")
    assert resp.status_code == 422 and resp.json()["detail"] == "identifier 中缺少有效身份证号", resp.text
