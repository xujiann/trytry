"""慢专病召回成功自动恢复在管，却不记「恢复」生命周期事件；已结束的召回还能再登记、把结论盖掉（P2-501，第九批「留痕承诺」
扫描 W3-3）。

`SpdLifecycleEvent` 写着「排除 / 迁出 / 死亡 / 召回 / 恢复，逐条留痕」：走生命周期「恢复」的会记一条，`POST
/api/spd/recalls/{id}/progress` 登记「已召回」自动恢复在管的一条不记——生命周期记录里只看得到被召回，看不到什么时候、
谁把它恢复的。页面对「已召回 / 召回失败」早就不给「登记进度」，接口还收：已召回的再登记成「召回失败」，「已重新纳管」
的结论被盖掉，档案却还在管。

修法：召回成功自动恢复时同一事务记一条「恢复」（缘由「召回成功」、结果、登记人）；已结束的召回再登记 409。
"""
import pytest

B = "/api/spd"
PROGRAM = "p2501_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2501 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2501 高血压", "category": "chronic"}).status_code == 201
    return {"org": org, "n": 0}


def _recalled(client, admin, world):
    """一份在管档案，登记召回，返回 (档案, 召回)。"""
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdRecall

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2501 患者{world['n']}", "id_card": f"33028119800101{world['n']:03d}X"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=world["org"], status="active")
        db.add(enrollment)
        db.commit()
        enrollment_id = enrollment.id
    resp = client.post(f"{B}/enrollments/{enrollment_id}/lifecycle", headers=admin,
                       json={"event": "recall", "reason": "失访三个月"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        recall = db.query(SpdRecall).filter(SpdRecall.enrollment_id == enrollment_id).one()
        return enrollment_id, recall.id


def _events(enrollment_id):
    from app.database import SessionLocal
    from app.spd.models import SpdLifecycleEvent

    with SessionLocal() as db:
        return [(e.event, e.reason, e.detail, e.operator_id is not None) for e in db.query(SpdLifecycleEvent).filter(
            SpdLifecycleEvent.enrollment_id == enrollment_id).order_by(SpdLifecycleEvent.id)]


def test_召回成功恢复在管_记一条恢复事件(client, admin, world):
    enrollment, recall = _recalled(client, admin, world)
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                       json={"status": "returned", "contact_note": "电话联系上，本周回来复诊", "result": "已重新纳管"})
    assert resp.status_code == 200, resp.text
    assert client.get(f"{B}/enrollments/{enrollment}", headers=admin).json()["status"] == "active"
    assert _events(enrollment) == [
        ("recall", "失访三个月", "", True),
        ("resume", "召回成功", "已重新纳管", True),   # 修前没有这一条
    ]


def test_已结束的召回不能再登记(client, admin, world):
    enrollment, recall = _recalled(client, admin, world)
    assert client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                       json={"status": "returned", "result": "已重新纳管"}).status_code == 200
    again = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                        json={"status": "failed", "result": "改判失败"})
    assert again.status_code == 409, again.text   # 修前 200：结论被改成「召回失败」，档案却还在管
    rows = {r["id"]: r for r in client.get(f"{B}/recalls", headers=admin, params={"limit": 200}).json()}
    assert (rows[recall]["status"], rows[recall]["result"]) == ("returned", "已重新纳管")

    _, failed = _recalled(client, admin, world)
    assert client.post(f"{B}/recalls/{failed}/progress", headers=admin,
                       json={"status": "failed", "result": "空号"}).status_code == 200
    reopen = client.post(f"{B}/recalls/{failed}/progress", headers=admin, json={"status": "pending"})
    assert reopen.status_code == 409, reopen.text   # 要再召回走生命周期另起一条


def test_进行中的召回照常逐次登记(client, admin, world):
    _, recall = _recalled(client, admin, world)
    for note in ("第一次没接", "第二次约了时间"):
        resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                           json={"status": "contacted", "contact_note": note})
        assert resp.status_code == 200, resp.text
    rows = {r["id"]: r for r in client.get(f"{B}/recalls", headers=admin, params={"limit": 200}).json()}
    assert [c["note"] for c in rows[recall]["contacts"]] == ["第一次没接", "第二次约了时间"]
