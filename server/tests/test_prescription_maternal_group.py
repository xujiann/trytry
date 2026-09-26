"""审方特殊人群「孕产妇」按在册（未结案）的孕产档案认（P2-120）。

审方规则的特殊人群 `pregnant` 显示为「孕产妇」，推断函数的说明也写「按在册孕产记录」，实现却只认孕期
（`status == 'registered'`）：一分娩（产后访视或分娩登记把档案推到 `delivered`），到产后访视结案之前的整个
产褥期 / 哺乳期都不算孕产妇——开他汀、利伐沙班照样「系统审通过」，而这两味的说明书哺乳期同样禁用（ACEI / ARB
哺乳期也要权衡）。
此前这条人群一条用例都没有（儿童、老年各有一条）。

修法：在册 = 未结案，孕期与已分娩未结案都算；结案的历次档案不算。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2120 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    rule = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": "P2120-STATIN", "max_daily_dose": 80, "special_groups": "pregnant"})
    assert rule.status_code == 201, rule.text
    return {"org": org}


def _woman(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "审方孕产妇", "id_card": f"33010619920404{next(_CARDS):04d}", "gender": "女", "birth_date": "1992-04-04"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _record(client, admin, patient_id, *, deliver=False, close=False):
    record = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient_id}).json()
    if deliver or close:
        client.post(f"/api/maternal/records/{record['id']}/visits", headers=admin, json={"visit_type": "postpartum"})
    if close:
        assert client.post(f"/api/maternal/records/{record['id']}/close", headers=admin).json()["status"] == "closed"
    return record


def _prescribe(client, admin, world, patient_id):
    resp = client.post("/api/prescriptions", headers=admin, json={
        "patient_id": patient_id, "org_id": world["org"],
        "items": [{"drug_code": "P2120-STATIN", "drug_name": "阿托伐他汀钙片", "daily_dose": 20}]})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_孕期的开孕产妇慎用药_转药师审(client, admin, world):
    patient = _woman(client, admin)
    _record(client, admin, patient)
    rx = _prescribe(client, admin, world, patient)
    assert rx["status"] == "pending_review"
    assert "阿托伐他汀钙片 对孕产妇需慎用" in rx["review_comment"]


def test_已分娩未结案的产妇_同样转药师审(client, admin, world):
    patient = _woman(client, admin)
    _record(client, admin, patient, deliver=True)
    rx = _prescribe(client, admin, world, patient)
    assert rx["status"] == "pending_review", rx   # 修前 auto_passed：产褥期 / 哺乳期不算孕产妇
    assert "对孕产妇需慎用" in rx["review_comment"]


def test_产后结案的不再算孕产妇(client, admin, world):
    patient = _woman(client, admin)
    _record(client, admin, patient, close=True)
    rx = _prescribe(client, admin, world, patient)
    assert (rx["status"], rx["review_comment"]) == ("auto_passed", "")


def test_上一胎已结案_这一胎在册_按这一胎算孕产妇(client, admin, world):
    """与一孕一册（P1-140）衔接：结案的历次档案不挡在册的这一本。"""
    patient = _woman(client, admin)
    _record(client, admin, patient, close=True)
    _record(client, admin, patient)
    rx = _prescribe(client, admin, world, patient)
    assert rx["status"] == "pending_review" and "对孕产妇需慎用" in rx["review_comment"]
