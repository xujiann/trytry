"""「谁看过我的档案」被居民自己的调阅刷屏：打开十几次「我的档案」，别家医生真正的调阅就被挤出最近 50 条（P2-360）。

居民端每读一次自己的档案都写一条 AccessLog（`resident:{账号}`、依据「本人」），打开一次「我的档案」就是三条；
`GET /api/access-logs/mine` 只按患者筛，这些本人调阅全列在里面。手机页取最近 50 条、空态写「还没有人调阅过您的档案」
——十几次之后窗口里全是自己，空态也永远出不来。修法：不列这个账号自己的调阅；代管家属与医护的照列，留痕本身不动。
"""
import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import SmsCode

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2360 居民", "id_card": "330424199003032360", "phone": "13700112360"}).json()
    with SessionLocal() as db:
        db.query(SmsCode).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": "13700112360", "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login",
                        json={"phone": "13700112360", "code": code}).json()["access_token"]
    return {"patient": patient, "me": {"Authorization": f"Bearer {token}"}}


def test_本人调阅不列_医护调阅照列(client, admin, world):
    for _ in range(3):
        assert client.get("/api/portal/me/archive", headers=world["me"]).status_code == 200   # 本人调阅，留痕照写
    assert client.get(f"/api/archive/{world['patient']['ehc_no']}", headers=admin).status_code == 200
    rows = client.get("/api/access-logs/mine", headers=world["me"]).json()
    assert [r["viewer"] for r in rows] == ["admin"]          # 修前还有三条 resident:{本账号}
    from app.models import AccessLog

    with SessionLocal() as db:   # 留痕本身不动
        assert db.query(AccessLog).filter(AccessLog.patient_id == world["patient"]["id"],
                                          AccessLog.username.like("resident:%")).count() >= 3
