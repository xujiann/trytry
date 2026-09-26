"""特病申报人能审核自己的申报——表上连申报人都没记（P2-399）。

审核接口写着「申报（operator/doctor）与审核（director）职责分离，杜绝自报自批」，可角色守卫只分得开这两类角色：
管理员、复制了两类权限点的自定义角色照样自己报、自己批，200；而 `special_disease_apps` 原先只有患者、病种、状态、
理由，谁报的、谁批的都不落，接口想比也没处比。修后迁移 `d8f2a6c4b1e3` 补两列，申报记申报人、审核记审核人，本人
审核 403（同双通道 P2-398）；存量申报没记申报人、不回填，比不出的照原样放行。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import SpecialDiseaseApp


@pytest.fixture(scope="module")
def world(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2399 患者", "id_card": "330127197309092399"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2399_dir", "password": "passw0rd1", "role": "director", "full_name": "另一位主任"})
    assert created.status_code == 201, created.text
    return {"patient": patient, "director": login(client, "p2399_dir", "passw0rd1"), "director_id": created.json()["id"]}


def _apply(client, admin, world, disease):
    created = client.post("/api/insurance/special-diseases", headers=admin, json={
        "patient_id": world["patient"], "disease_name": disease, "reason": "规律血透"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _row(app_id):
    with SessionLocal() as db:
        row = db.get(SpecialDiseaseApp, app_id)
        return row.status, row.created_by, row.reviewed_by


def _admin_id(client, admin):
    return next(u["id"] for u in client.get("/api/users", headers=admin).json() if u["username"] == "admin")


def test_申报人审核自己的申报_403_仍待审(client, admin, world):
    app_id = _apply(client, admin, world, "P2399 尿毒症透析")
    got = client.post(f"/api/insurance/special-diseases/{app_id}/review", headers=admin, params={"approve": True})
    assert (got.status_code, got.json()["detail"]) == (403, "不得审核本人提出的特病申报"), got.text   # 修前 200
    assert _row(app_id) == ("applied", _admin_id(client, admin), None)


def test_别人审核照常_记下审核人(client, admin, world):
    app_id = _apply(client, admin, world, "P2399 器官移植抗排异")
    got = client.post(f"/api/insurance/special-diseases/{app_id}/review", headers=world["director"],
                      params={"approve": False})
    assert got.status_code == 200 and got.json()["status"] == "rejected", got.text
    assert _row(app_id) == ("rejected", _admin_id(client, admin), world["director_id"])


def test_存量申报没记申报人_照原样放行(client, admin, world):
    with SessionLocal() as db:   # 迁移前落的申报：申报人一列为空（不回填）
        legacy = SpecialDiseaseApp(patient_id=world["patient"], disease_name="P2399 存量病种", reason="迁移前")
        db.add(legacy)
        db.commit()
        app_id = legacy.id
    got = client.post(f"/api/insurance/special-diseases/{app_id}/review", headers=admin, params={"approve": True})
    assert got.status_code == 200 and got.json()["status"] == "approved", got.text
