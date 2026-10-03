"""医生 360 的处方段标出已退药、给出状态中文名（P2-1198，第三十四批「患者全景与时间轴」扫描 L4-6）。

退药冲销只把发药记录置 reversed、药回库房，处方表不动（它的状态是审方结论）。用药画像、用量统计早按
`dispense.prescription_not_reversed` 把退掉的处方排除（P2-624），360 处方段漏了：修前处方 auto_passed → 发药 →
整方退药（「患者过敏」）之后，360 仍是 `{"diagnosis_name": "高血压", "status": "auto_passed"}`，医生当它还在吃；
同一患者的用药画像却已是 `drugs: []`。药师退回的处方也只给英文码 `rejected`。

修后处方行保留（开过这张方是事实），只加键：`dispense_reversed`（判据照用量统计那一句，不另写）与状态中文名
`status_name`（`PRESCRIPTION_STATUS_NAMES`，表外的码原样回显）。未退药的照旧。
"""
import pytest

from app.database import SessionLocal
from app.models import Prescription, User


def _ok(resp):
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


@pytest.fixture(scope="module")
def world(client, admin):
    org = _ok(client.post("/api/organizations", headers=admin, json={
        "name": "P21198 药房卫生院", "org_type": "township", "level": "township"}))["id"]
    patient = _ok(client.post("/api/patients", headers=admin, json={
        "name": "P21198 赵大伯", "id_card": "330102197202021198"}))
    for code, name in (("P21198-AMLO", "氨氯地平片"), ("P21198-METF", "二甲双胍片")):
        _ok(client.post("/api/pharmacy/stocks", headers=admin, json={
            "org_id": org, "drug_code": code, "drug_name": name, "quantity": 1000}))

    def prescribe(code, name, diagnosis):
        return _ok(client.post("/api/prescriptions", headers=admin, json={
            "patient_id": patient["id"], "org_id": org, "diagnosis_name": diagnosis,
            "items": [{"drug_code": code, "drug_name": name, "daily_dose": 5, "days": 30}]}))

    reversed_rx = prescribe("P21198-AMLO", "氨氯地平片", "高血压")
    assert reversed_rx["status"] == "auto_passed"
    dispensed = _ok(client.post("/api/dispense", headers=admin, json={"prescription_id": reversed_rx["id"]}))
    _ok(client.post(f"/api/dispense/{dispensed['id']}/reverse", headers=admin, json={"reason": "患者过敏，整方退药"}))
    kept_rx = prescribe("P21198-METF", "二甲双胍片", "2型糖尿病")
    _ok(client.post("/api/dispense", headers=admin, json={"prescription_id": kept_rx["id"]}))
    undispensed_rx = prescribe("P21198-METF", "二甲双胍片", "2型糖尿病复诊")
    with SessionLocal() as db:   # 药师退回的处方：直接落库造状态（审方链路不是本条要测的）
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        rejected = Prescription(patient_id=patient["id"], org_id=org, diagnosis_name="高血压", status="rejected",
                                created_by=admin_id)
        db.add(rejected)
        db.commit()
        rejected_id = rejected.id
    view = client.get(f"/api/archive/{patient['ehc_no']}", headers=admin)
    assert view.status_code == 200, view.text
    return {"rows": {row["id"]: row for row in view.json()["prescriptions"]},
            "reversed": reversed_rx["id"], "kept": kept_rx["id"], "undispensed": undispensed_rx["id"],
            "rejected": rejected_id}


def test_整方退药后_360那一行带已退药标记与中文状态名(world):
    row = world["rows"][world["reversed"]]   # 行保留
    assert row["dispense_reversed"] is True   # 修前没有这个键，照原审方状态列出，医生当它还在吃
    assert row["status"] == "auto_passed"   # 既有键不变：处方状态仍是审方结论
    assert row["status_name"] == "系统审通过"
    assert row["diagnosis_name"] == "高血压"


def test_未退药的照旧_没发过药的不算退药(world):
    for key in ("kept", "undispensed"):
        row = world["rows"][world[key]]
        assert row["dispense_reversed"] is False, key
        assert (row["status"], row["status_name"]) == ("auto_passed", "系统审通过"), key


def test_药师退回的处方给中文状态名(world):
    row = world["rows"][world["rejected"]]
    assert (row["status"], row["status_name"]) == ("rejected", "已退回")   # 修前只给英文码
    assert row["dispense_reversed"] is False
