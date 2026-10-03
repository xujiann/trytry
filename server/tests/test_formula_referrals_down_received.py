"""绩效公式变量「期间下转接收人次」只数接收方接了的下转（P2-1194，第三十四批「跨机构协作的两端」扫描 L2-10）。

`build_variable_index` 的 `referrals_down` 按 `to_org_id` 数期内的下转单、不看状态：被基层退回的、至今待接诊的都算作「接收」
——变量名写的是「接收」，居民端文案里「已接收」就是 accepted（`portal.py` 的平台转诊标签）。期末综合绩效报告与自定义公式
都取它，排名跟着虚高。修前实测（scan34 l2/r2）：县医院下转甲镇两张（一张被退回、一张至今待接诊），甲镇
`down_recv=2.0`。

修法：只数已接诊（accepted）、已结案（completed）的下转。「期间上转人次」计数不改——它数的是发起的上转申请、含被退回的，
与上报 #7「基层向上级机构转诊申请数」同一口径——变量说明里写明。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import Referral, User
from app.routers.analytics import build_variable_index

PERIOD = "2026-09"
IN_PERIOD, OUT_OF_PERIOD = datetime(2026, 9, 10, 9), datetime(2026, 10, 10, 9)


@pytest.fixture(scope="module")
def orgs(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P1194 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P1194 甲镇卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1194 患者", "id_card": "330106196005051194"}).json()["id"]
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()

        def refer(direction, status, at=IN_PERIOD):
            src, dst = (county, town) if direction == "down" else (town, county)
            db.add(Referral(patient_id=patient, from_org_id=src, to_org_id=dst, direction=direction, status=status,
                            reason=f"P1194 {direction} {status}", created_by=creator, created_at=at))

        for status in ("pending", "rejected", "accepted", "completed"):   # 县医院下转甲镇：待接诊、被退回、已接诊、已结案
            refer("down", status)
        refer("down", "accepted", OUT_OF_PERIOD)   # 期外的不算
        for status in ("pending", "rejected", "accepted"):   # 甲镇上转县医院：被退回的照算申请数
            refer("up", status)
        db.commit()
    return {"county": county, "town": town}


def test_下转接收人次不数被退回与待接诊的(orgs):
    with SessionLocal() as db:
        town = build_variable_index(db, PERIOD)[orgs["town"]]
    assert town["referrals_down"] == 2.0   # 修前 4.0：被退回、待接诊的也算「接收」
    assert town["referrals_up"] == 3.0   # 计数不改：发起的上转申请数，含被退回的


def test_期末综合绩效报告同一口径(client, admin, orgs):
    for key, expr in (("p1194_down", "referrals_down"), ("p1194_up", "referrals_up")):
        made = client.post("/api/analytics/formulas", headers=admin, json={
            "key": key, "name": key, "expression": expr, "weight": 0})
        assert made.status_code == 201, made.text
    report = client.get("/api/analytics/performance-report", headers=admin, params={"period": PERIOD})
    assert report.status_code == 200, report.text
    row = next(r for r in report.json()["orgs"] if r["org_id"] == orgs["town"])
    assert {i["key"]: i["value"] for i in row["items"]} == {"p1194_down": 2.0, "p1194_up": 3.0}   # 修前 down 4.0


def test_变量说明写明两个口径(client, admin):
    rows = client.get("/api/analytics/formula-variables", headers=admin).json()
    described = {r["name"]: r["description"] for r in rows}
    assert "被退回" in described["referrals_up"] and "#7" in described["referrals_up"]
    assert "已接诊" in described["referrals_down"] and "待接诊" in described["referrals_down"]
