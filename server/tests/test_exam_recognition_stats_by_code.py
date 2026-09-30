"""互认统计按项目编码分组、名称取互认目录名（P2-1081，第三十一批「编码体系与术语」扫描 C4-11）。

原先按（编码, 手填名称）分组：同一个 DR-CHEST 写成「胸部DR」「胸片」「DR胸部正位」就拆成三行各计 1 次，页面取前 10
画图时同一个项目占三格、次数也看不出来。传染病统计早按「按编码分组、名称取目录名」修过（P2-159 / P2-943）。修后
按编码一行，目录内的印目录名，目录外的取手填名称里的一个（min，结果确定）。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamRequest, RecognitionItem, User


@pytest.fixture(scope="module")
def recognized(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21081 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    pid = client.post("/api/patients", headers=admin, json={
        "name": "P21081 患者", "id_card": "330281197001012081"}).json()["id"]
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        db.add(RecognitionItem(item_code="DR-P21081", item_name="胸部DR(P21081目录名)", center_type="imaging"))
        for code, name in (("DR-P21081", "胸片"), ("DR-P21081", "DR胸部正位"), ("DR-P21081", "胸部DR"),
                           ("US-P21081", "腹部B超"), ("US-P21081", "B超腹部")):
            db.add(ExamRequest(patient_id=pid, from_org_id=org, center_type="imaging", item_code=code, item_name=name,
                               status="recognized", created_by=admin_id))
        db.commit()


def test_同一编码一行_名称取目录名(client, admin, recognized):
    stats = client.get("/api/exams/recognition-stats", headers=admin).json()
    mine = [r for r in stats["by_item"] if r["item_code"].endswith("-P21081")]
    assert mine == [
        {"item_code": "DR-P21081", "item_name": "胸部DR(P21081目录名)", "recognized_count": 3},   # 修前三行各 1
        {"item_code": "US-P21081", "item_name": "B超腹部", "recognized_count": 2},                 # 目录外：min
    ]
