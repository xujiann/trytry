"""处方按开方那一刻审方用的那一版规则判读（P2-577，第十一批「落库快照 vs 现查配置」扫描 Y3-1）。

审方页写着「规则改过什么、什么时候不再生效，处方点评复核时要回溯得到」，停用确认框写的是「此后」；处方点评要点、
抗菌药物使用强度、规则可审张数却都拿现行规则判读历史处方。实测（修前）头孢 2 g/日 × 5 天，开方时系统审通过：
- 规则把单位纠正为 mg（上限 4000、DDD 2000）后，点评显示「2.0mg」，当月 DDDs 从 5.0 变成 0.01；
- 上限收紧到 1 后，这张当时审过的方被标「日剂量超限」；
- 停用后成了「规则未维护」，DDDs 与未覆盖数双双归零（药占比页写的是「不悄悄丢掉」），可审张数少一张。

修后开方时把审方用的上限、单位、抗菌标记、DDD 记在明细上，三处都按它判读；没有快照的（开方时没有生效规则，或快照列
上线前开的）照旧按现行生效规则。
"""
import pytest

from app import clock
from app.database import SessionLocal
from app.models import Prescription, PrescriptionItem

CODE = "P2577-CEF"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2577 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2577 患者", "id_card": "330127196001012577"}).json()["id"]
    rule = client.post("/api/prescriptions/rules", headers=admin, json={
        "drug_code": CODE, "max_daily_dose": 4, "dose_unit": "g", "antibiotic": True, "ddd": 2,
        "review_points": "P2577 疗程不超过 7 天"})
    assert rule.status_code == 201, rule.text
    rx = client.post("/api/prescriptions", headers=admin, json={"patient_id": patient, "org_id": org, "items": [
        {"drug_code": CODE, "drug_name": "P2577 头孢唑林", "daily_dose": 2, "days": 5}]})
    assert rx.status_code == 201 and rx.json()["status"] == "auto_passed", rx.text
    return {"org": org, "patient": patient, "rx": rx.json()["id"]}


def _points(client, admin, rx_id: int) -> dict:
    resp = client.get(f"/api/prescriptions/{rx_id}/review-points", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ddds(client, admin, org: int) -> tuple[float, int]:
    resp = client.get(f"/api/analytics/drug-use?period={clock.today().isoformat()[:7]}", headers=admin)
    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["orgs"] if r["org_id"] == org)
    return row["antibiotic_ddds"], row["ddd_uncovered_items"]


def _covered(client, admin, org: int) -> int:
    resp = client.get("/api/performance/orgs", headers=admin,
                      params={"period": clock.today().isoformat()[:7], "org_id": org})
    assert resp.status_code == 200, resp.text
    card = next(c for c in resp.json()["scorecards"] if c["org_id"] == org)
    return card["detail"]["rx_pass"]["rule_covered"]


def _import(client, admin, **rule):
    resp = client.post("/api/prescriptions/rules/import", headers=admin, json=[{
        "drug_code": CODE, "antibiotic": True, "review_points": "P2577 疗程不超过 7 天", **rule}])
    assert resp.status_code == 200, resp.text


def _assert_as_prescribed(client, admin, world):
    item = _points(client, admin, world["rx"])["items"][0]
    assert (item["max_daily_dose"], item["dose_unit"], item["dose_exceeded"], item["no_rule"]) == (4.0, "g", False, False)
    assert _ddds(client, admin, world["org"]) == (5.0, 0)   # 2 g × 5 天 ÷ DDD 2 g
    assert _covered(client, admin, world["org"]) == 1


def test_开方当时的判读(client, admin, world):
    _assert_as_prescribed(client, admin, world)
    assert _points(client, admin, world["rx"])["items"][0]["review_points"] == "P2577 疗程不超过 7 天"


def test_规则纠正单位之后_历史处方仍按开方时的单位判读(client, admin, world):
    _import(client, admin, max_daily_dose=4000, dose_unit="mg", ddd=2000)
    _assert_as_prescribed(client, admin, world)   # 修前显示 4000mg、DDDs 0.01


def test_上限收紧之后_当时审过的方不被标超限(client, admin, world):
    _import(client, admin, max_daily_dose=1, dose_unit="g", ddd=2)
    _assert_as_prescribed(client, admin, world)   # 修前 dose_exceeded=True


def test_停用之后_不成规则未维护_不从强度与可审张数里消失(client, admin, world):
    off = client.delete(f"/api/prescriptions/rules/{CODE}", headers=admin)
    assert off.status_code == 200, off.text
    try:
        _assert_as_prescribed(client, admin, world)   # 修前 no_rule=True、DDDs 0、可审 0
        # 点评要点是给点评人看的文字，取规则行现在的写法（停用了行也还在）
        assert _points(client, admin, world["rx"])["items"][0]["review_points"] == "P2577 疗程不超过 7 天"
        # 停用「此后」生效：新开的方按规则未维护
        again = client.post("/api/prescriptions", headers=admin, json={
            "patient_id": world["patient"], "org_id": world["org"], "items": [
                {"drug_code": CODE, "drug_name": "P2577 头孢唑林", "daily_dose": 2, "days": 1}]})
        assert again.status_code == 201, again.text
        assert _points(client, admin, again.json()["id"])["items"][0]["no_rule"] is True
    finally:
        client.post(f"/api/prescriptions/rules/{CODE}/reactivate", headers=admin)


def test_没有快照的明细照旧按现行生效规则判读(client, admin, world):
    with SessionLocal() as db:   # 快照列上线前开的处方
        rx = Prescription(patient_id=world["patient"], org_id=world["org"], status="auto_passed",
                          created_by=1)
        db.add(rx)
        db.flush()
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code=CODE, drug_name="P2577 存量头孢",
                                daily_dose=3, days=1))
        db.commit()
        rx_id = rx.id
    _import(client, admin, max_daily_dose=2, dose_unit="g", ddd=2)
    item = _points(client, admin, rx_id)["items"][0]
    assert (item["max_daily_dose"], item["dose_unit"], item["dose_exceeded"]) == (2.0, "g", True)
