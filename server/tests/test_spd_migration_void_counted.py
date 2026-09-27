"""已不再生效的迁出不算「待确认迁入」、清单不给「确认迁入」按钮（P2-592，第十二批扫描 Z2-2 / Z1-2）。

确认接口早就挡住了已不再生效的迁出（患者已死亡 P1-111；原档案已迁出 / 已排除 / 已结案 P2-527，409「这次迁出不再
生效」），可工作台两处「待确认迁入」只除了死亡、照数其余三种，生命周期清单也照样画「待确认 / 确认迁入」——点下去才 409。
"""
from pathlib import Path

import pytest

B = "/api/spd"
PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2592 {tag}院", "org_type": "township", "level": "township"}).json()["id"] for tag in "甲乙丙"]
    events = {}
    for n, tag in enumerate(("排除", "改迁", "照常")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2592 {tag}", "id_card": f"33012719800202592{n}"}).json()["id"]
        enrollment = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": "hypertension", "org_id": orgs[0]})
        assert enrollment.status_code == 201, enrollment.text
        eid = enrollment.json()["id"]
        moved = client.post(f"{B}/enrollments/{eid}/lifecycle", headers=admin, json={
            "event": "migrate", "reason": "搬迁", "target_org_id": orgs[1]})
        assert moved.status_code == 200, moved.text
        events[tag] = moved.json()["event_id"]
        if tag == "排除":   # 迁出登记之后档案被排除
            excluded = client.post(f"{B}/enrollments/{eid}/lifecycle", headers=admin, json={
                "event": "exclude", "reason": "误纳"})
            assert excluded.status_code == 200, excluded.text
        if tag == "改迁":   # 又登记迁往丙院，丙院先确认了
            again = client.post(f"{B}/enrollments/{eid}/lifecycle", headers=admin, json={
                "event": "migrate", "reason": "再搬", "target_org_id": orgs[2]})
            assert client.post(f"{B}/lifecycle-events/{again.json()['event_id']}/confirm",
                               headers=admin).status_code == 200
    return events


def test_不再生效的迁出_清单写明原因_工作台不算待确认(client, admin, world):
    rows = {r["id"]: r for r in client.get(f"{B}/lifecycle-events", params={"event": "migrate", "limit": 100},
                                           headers=admin).json()}
    assert rows[world["排除"]]["void_reason"] == "原档案已排除，这次迁出不再生效"   # 与确认接口的 409 同一句
    assert rows[world["改迁"]]["void_reason"] == "原档案已迁出，这次迁出不再生效"
    assert rows[world["照常"]]["void_reason"] == ""
    for tag in ("排除", "改迁"):
        got = client.post(f"{B}/lifecycle-events/{world[tag]}/confirm", headers=admin)
        assert (got.status_code, got.json()["detail"]) == (409, rows[world[tag]]["void_reason"])
    unconfirmed = [r for r in rows.values() if not r["confirmed"] and not r["void_reason"]]
    admin_wb = client.get(f"{B}/workbench/admin", headers=admin).json()
    center_wb = client.get(f"{B}/workbench/center", headers=admin).json()
    assert admin_wb["alerts"]["pending_migrations"] == len(unconfirmed)   # 修前多算那两条
    assert center_wb["lifecycle"]["pending_migrations"] == len(unconfirmed)


def test_清单只给还能确认的迁出画确认按钮():
    start = PAGE.index("const drawLifecycle = async () => {")
    body = PAGE[start:PAGE.index("};", start)]
    assert 'v.confirmed ? "—" : v.void_reason ? esc(v.void_reason)' in body   # 修前 `v.confirmed ? "—" : 按钮`
