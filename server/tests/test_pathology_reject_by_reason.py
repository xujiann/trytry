"""病理拒收原因收成闭集「为了按原因分解统计」，统计里却只有一个拒收总数（P2-271）。

`reject_specimen` 只收五个标准原因，注释写着「自由文本的拒收原因无法按原因分解统计——拒收率恰恰要按原因看才有管理价值」；
可质控统计（`GET /api/pathology/specimen-stats`）只给拒收数与拒收率，从没按原因分解过——收成闭集图的那一步没做。
修法：统计加 `rejected_by_reason`，标准项按固定顺序恒在、没有的记 0；修之前录进的自由文本原因排在后面；页面列出分解。
"""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routers.pathology import REJECT_REASONS
from conftest import login, reset_database


@pytest.fixture(scope="module")
def client():
    reset_database()   # 统计是全库口径，干净库才钉得住精确计数
    with TestClient(app) as c:
        yield c


def test_拒收按原因分解_标准项恒在_存量自由文本排在后面(client):
    from app.database import SessionLocal
    from app.models import PathologySpecimen

    admin = login(client, "admin", "admin123")
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2271 病理医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2271 患者", "id_card": "330881199303037271"}).json()["id"]
    rid = client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": org, "center_type": "pathology",
        "item_code": "P2271", "item_name": "组织病理学检查"}).json()["id"]
    for reason in ("未加固定液", "未加固定液", "标识不清"):
        sid = client.post("/api/pathology/specimens", headers=admin, json={"request_id": rid}).json()["id"]
        assert client.post(f"/api/pathology/specimens/{sid}/reject", headers=admin,
                           json={"reject_reason": reason}).status_code == 200
    with SessionLocal() as db:   # 收成闭集之前录进的自由文本原因
        db.add(PathologySpecimen(request_id=rid, specimen_no="PS-P2271-LEGACY", status="rejected",
                                 reject_reason="容器渗漏"))
        db.commit()
    body = client.get("/api/pathology/specimen-stats", headers=admin).json()
    assert body["rejected"] == 4
    assert body["rejected_by_reason"] == {**{r: 0 for r in REJECT_REASONS}, "未加固定液": 2, "标识不清": 1,
                                          "容器渗漏": 1}   # 修前没有这一项
    assert list(body["rejected_by_reason"])[:len(REJECT_REASONS)] == REJECT_REASONS


def test_页面列出拒收原因分解():
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index("async function renderPathology()")
    body = source[start: source.index("\nasync function ", start + 1)]
    assert "rejected_by_reason" in body and "拒收原因：" in body
