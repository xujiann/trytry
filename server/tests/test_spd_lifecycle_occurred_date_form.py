"""慢专病生命周期表单补「发生日期」（P2-864，第二十三批「页面表单提交的字段与取值 vs 后端请求模型」扫描 Y1-13）。

`LifecycleIn.occurred_at` 早就收（缺省今天）；页面表单没有这一项，补登的死亡 / 迁出一律记成登记当天——原因里写着
「9 月 20 日在家中去世」，事件日期却是今天。修后表单加日期框，留空照旧按今天。
"""
from pathlib import Path

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-spd.js").read_text(encoding="utf-8")


def test_生命周期表单有发生日期():
    start = PAGE.index('<form class="inline" id="spd-life-form">')
    assert '<input name="occurred_at" type="date"' in PAGE[start:PAGE.index("</form>", start)]   # 修前没有


def test_按页面送的发生日期_照记(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2864 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2864 患者", "id_card": "330102194001012864"}).json()["id"]
    enrolled = client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrolled.status_code == 201, enrolled.text
    died = client.post(f"/api/spd/enrollments/{enrolled.json()['id']}/lifecycle", headers=admin, json={
        "event": "death", "reason": "9 月 20 日在家中去世", "occurred_at": "2026-09-20"})
    assert died.status_code == 200, died.text
    from app.database import SessionLocal
    from app.spd.models import SpdLifecycleEvent

    with SessionLocal() as db:
        got = [e.occurred_at for e in db.query(SpdLifecycleEvent).filter(
            SpdLifecycleEvent.enrollment_id == enrolled.json()["id"], SpdLifecycleEvent.event == "death")]
    assert got == ["2026-09-20"]
