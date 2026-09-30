"""居民端「疾病管理档案」里慢专病一栏的「下次随访」恒显示「待安排」（P2-973，第二十七批「冗余的汇总列 / 派生字段与明细
不同步」扫描 G3-2）。

聚合列表（`/api/portal/me/enrollments/all`）的慢专病一源调 `enrollment_feed_item` 时不传 `next_followup_due`，恒为空串，居民端
按空值显示「待安排」；平台一源传了、慢专病自己的首页（`/api/portal/spd/home`）读的也是档案的 `next_followup_at`——同一个病种，
平台慢病卡片显示日期、慢专病卡片显示「待安排」。`enrollment_feed_item` 的 docstring 写着「统一条目形状……省得两边字段名
慢慢长歪」。

修法：慢专病条目带上档案的下次随访，只给在管的（与首页只列在管同一口径；结案、迁出时这一列不清，原样给会露出过期日期）。
"""
import pytest

from app.database import SessionLocal

PHONE = "13900029730"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2973 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2973 居民", "id_card": "330106196404040073", "birth_date": "1964-04-04", "phone": PHONE})
    assert patient.status_code in (200, 201), patient.text
    with SessionLocal() as db:
        for program, status in (("hypertension", "active"), ("diabetes", "migrated")):
            db.add(SpdEnrollment(patient_id=patient.json()["id"], org_id=org, program_code=program, risk_level="mid",
                                 stage="管理期", status=status, next_followup_at="2026-12-29"))
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code})
    assert token.status_code == 200, token.text
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


def test_在管档案的下次随访与慢专病首页一致_非在管的留空(client, world):
    items = client.get("/api/portal/me/enrollments/all", headers=world).json()
    spd = {i["program_code"]: i["next_followup_due"] for i in items if i["source"] == "spd"}
    assert spd == {"hypertension": "2026-12-29", "diabetes": ""}   # 修前在管的也是 ""
    home = client.get("/api/portal/spd/home", headers=world).json()
    assert {p["program_code"]: p["next_followup_at"] for p in home["programs"]} == {"hypertension": "2026-12-29"}
