"""双通道申报人能审核自己的申报：自己报、自己批，200（P2-398）。

物资采购审批（「申请人不得自批——与手术审批、双通道申报同一口径」）与手术审批（「与双通道申报同一口径」）都拿双通道
当口径的出处，可 `review_dual_channel` 一直没比申报人（申报时记了 `created_by`）。角色守卫只分得开「经办 / 医师报、
管理层审」，同时带两种权限的账号（管理员、复制了两类权限点的自定义角色）照样自报自批。修后与那两处同一个位置、
同一种回话：状态判完、翻转之前比申报人，本人 403，申报仍待审。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def world(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2398 患者", "id_card": "330127197309092398"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2398_dir", "password": "passw0rd1", "role": "director", "full_name": "另一位主任"})
    assert created.status_code == 201, created.text
    return {"patient": patient, "director": login(client, "p2398_dir", "passw0rd1")}


def _apply(client, admin, world, drug):
    created = client.post("/api/insurance/dual-channel", headers=admin, json={
        "patient_id": world["patient"], "drug_name": drug, "reason": "门诊用药"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _status(client, admin, app_id):
    return next(r["status"] for r in client.get("/api/insurance/dual-channel", headers=admin).json() if r["id"] == app_id)


def test_申报人审核自己的申报_403_仍待审(client, admin, world):
    app_id = _apply(client, admin, world, "P2398 甲药")
    got = client.post(f"/api/insurance/dual-channel/{app_id}/review", headers=admin, params={"approve": True})
    assert (got.status_code, got.json()["detail"]) == (403, "不得审核本人提出的双通道申报"), got.text   # 修前 200
    assert _status(client, admin, app_id) == "pending"


def test_别人审核照常(client, admin, world):
    app_id = _apply(client, admin, world, "P2398 乙药")
    got = client.post(f"/api/insurance/dual-channel/{app_id}/review", headers=world["director"],
                      params={"approve": False, "comment": "资料不全"})
    assert got.status_code == 200 and got.json()["status"] == "rejected", got.text
