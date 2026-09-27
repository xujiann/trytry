"""居民端首页的服务包列当前与历史的（P2-557，第十一批「居民端 vs 医护端」扫描 Y2-5）。

需求对照表居民端 #20：「查看当前及历史服务包」，实现栏写 `GET /api/portal/spd/home`（`packages`）。首页原先只列在管档案上
绑定中的——包被解绑、档案结案之后居民端就看不到了，医护端的绑定记录照旧在。修后全列，绑定中的在前，带状态文案。
"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import SmsCode
from app.spd import models as S

PHONE = "13913305570"


@pytest.fixture(scope="module")
def auth(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2557 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2557 居民", "id_card": "330277199001012557", "phone": PHONE}).json()["id"]
    with SessionLocal() as db:
        packages = [S.SpdServicePackage(code=f"P2557-{n}", name=f"P2557 {n}包", program_code="") for n in "甲乙丙"]
        db.add_all(packages)
        active = S.SpdEnrollment(patient_id=patient, program_code="hypertension", status="active", org_id=org)
        closed = S.SpdEnrollment(patient_id=patient, program_code="diabetes", status="completed", org_id=org)
        db.add_all([active, closed])
        db.flush()
        for enrollment, package, status in ((active, packages[0], "bound"), (active, packages[1], "unbound"),
                                            (closed, packages[2], "bound")):
            db.add(S.SpdPackageBinding(enrollment_id=enrollment.id, package_id=package.id, status=status,
                                       items=[{"code": "fu", "total": 4, "used": 1}], period_end="2026-12-31"))
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"Authorization": f"Bearer {token}"}


def test_首页服务包列当前与历史的_绑定中的在前(client, auth):
    resp = client.get("/api/portal/spd/home", headers=auth)
    assert resp.status_code == 200, resp.text
    rows = [(p["name"], p["program_code"], p["status"], p["status_name"]) for p in resp.json()["packages"]]
    # 修前只有「甲」：解绑的「乙」、结案档案上的「丙」都看不到
    assert rows == [("P2557 丙包", "diabetes", "bound", "绑定中"), ("P2557 甲包", "hypertension", "bound", "绑定中"),
                    ("P2557 乙包", "hypertension", "unbound", "已解绑")]
