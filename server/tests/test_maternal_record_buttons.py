"""孕产妇档案清单按事实给「分娩登记」「结案」（P2-594，第十二批「按钮 vs 状态机」扫描 Z2-5）。

页面原先只看档案状态：产后访视先录（分娩在别处、记录后补）就把档案推到「已分娩」，「分娩登记」按钮随之消失，分娩
记录从此录不进来——接口其实照收；分娩登记了、产后访视还没做，「结案」按钮已经亮着，点下去 409（P2-211）。清单现在
多带「有没有分娩记录」「有没有产后访视」两件事实，页面按它们给按钮。
"""
from pathlib import Path

import pytest

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2594 分娩医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    records = {}
    for n, tag in enumerate(("先访视", "先分娩")):
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P2594 {tag}", "id_card": f"33028119950501594{n}", "gender": "女"}).json()["id"]
        records[tag] = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient}).json()["id"]
    return {"org": org, **records}


def _row(client, admin, record_id):
    (row,) = [r for r in client.get("/api/maternal/records", headers=admin).json() if r["id"] == record_id]
    return row


def _deliver(client, admin, world, record_id):
    return client.post(f"/api/maternal/records/{record_id}/delivery", headers=admin, json={
        "org_id": world["org"], "delivery_date": "2026-09-18"})


def _postpartum(client, admin, record_id):
    return client.post(f"/api/maternal/records/{record_id}/visits", headers=admin, json={"visit_type": "postpartum"})


def test_产后访视先录的_清单写明还没有分娩记录_补登照收(client, admin, world):
    record = world["先访视"]
    assert _postpartum(client, admin, record).status_code == 201
    row = _row(client, admin, record)
    assert (row["status"], row["has_delivery"], row["has_postpartum"]) == ("delivered", False, True)   # 修前没有这两键
    assert _deliver(client, admin, world, record).status_code == 201
    assert _row(client, admin, record)["has_delivery"] is True


def test_分娩登记了还没产后访视的_清单写明_结案要等访视(client, admin, world):
    record = world["先分娩"]
    assert _deliver(client, admin, world, record).status_code == 201
    row = _row(client, admin, record)
    assert (row["status"], row["has_delivery"], row["has_postpartum"]) == ("delivered", True, False)
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 409
    assert _postpartum(client, admin, record).status_code == 201
    assert _row(client, admin, record)["has_postpartum"] is True
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 200


def test_页面按事实给按钮():
    start = PAGE.index("async function renderMaternal() {")
    body = PAGE[start:PAGE.index('$("#mat-form").onsubmit', start)]
    assert "${!r.has_delivery" in body   # 修前 `r.status === "registered"`：产后访视先录按钮就没了
    assert 'r.status === "delivered" && r.has_postpartum' in body   # 修前只看 delivered：没访视也亮
    assert 'r.status === "registered" ? `<button class="btn secondary" data-delivery' not in body
