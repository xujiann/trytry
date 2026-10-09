"""「恢复管理」不认 P2-1050 的「另有一份召回中的档案」：同一病种一份在管、一份召回中（P2-1574，第四十六批扫描 AJ3-3）。

建档那边（P2-1050）同病种有一份脱管 / 召回中的档案就不另建——另建一份在管的，原来那份的召回永远结不了。恢复在管与召回成功共用的
`_reactivate` 却只查「另有一份在管」（`_active_elsewhere_detail`）。修前实测：丙院的档案排除 → 乙院建档 → 乙院登记召回 → 丙院新建
档 409「有一份召回中的档案」（对照）→ 丙院恢复旧档案 200：档案成了「丙院在管 + 乙院召回中」，乙院登记「已召回」409「已在丙院在管」，
那份召回永远登不成，乙院工作台「召回中」恒为 1，居民端同一病种既「在管」又「召回中」。

修法：`_reactivate` 再判「同病种另有一份脱管 / 召回中的档案」（与建档同一个帮手 `service.paused_enrollment`，排除要恢复的这份
自己——召回成功恢复的那份本身就是召回中），有就 409 并点名档案号与机构。没有别的档案时恢复、召回成功照常。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdEnrollment, SpdLifecycleEvent, SpdRecall

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = {}
    for key, name in (("b", "P21574 乙卫生院"), ("c", "P21574 丙卫生院")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]
    return {"orgs": orgs, "n": 0}


def _patient(client, admin, world):
    world["n"] += 1
    made = client.post("/api/patients", headers=admin, json={
        "name": f"P21574 居民{world['n']}", "id_card": f"33010619720101{1574 + world['n']:04d}",
        "birth_date": "1972-01-01"})
    assert made.status_code in (200, 201), made.text
    return made.json()["id"]


def _enroll(client, admin, patient, org):
    resp = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _event(client, admin, enrollment, event, reason):
    return client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": event, "reason": reason})


def _statuses(patient):
    with SessionLocal() as db:
        return [(e.id, e.status) for e in db.query(SpdEnrollment).filter_by(patient_id=patient).order_by(SpdEnrollment.id)]


def _resume_events(enrollment):
    with SessionLocal() as db:
        return db.query(SpdLifecycleEvent).filter_by(enrollment_id=enrollment, event="resume").count()


def _recall_of(enrollment):
    with SessionLocal() as db:
        return db.query(SpdRecall.id).filter_by(enrollment_id=enrollment).order_by(SpdRecall.id.desc()).first()[0]


def test_别家召回中_恢复旧档案409_那份召回照常登记已召回(client, admin, world):
    patient = _patient(client, admin, world)
    old = _enroll(client, admin, patient, world["orgs"]["c"])
    assert _event(client, admin, old, "exclude", "外出务工").status_code == 200
    recalled = _enroll(client, admin, patient, world["orgs"]["b"])
    assert _event(client, admin, recalled, "recall", "三个月未随访").status_code == 200

    resume = _event(client, admin, old, "resume", "回乡复诊")
    assert resume.status_code == 409, resume.text   # 修前 200：一份在管、一份召回中
    assert resume.json() == {"detail": f"该患者此病种在「P21574 乙卫生院」有一份召回中的档案（档案 #{recalled}），"
                                       "不能再把这份档案恢复在管"}
    assert _statuses(patient) == [(old, "excluded"), (recalled, "recalled")]
    assert _resume_events(old) == 0   # 不留一条「恢复」事件

    returned = client.post(f"{B}/recalls/{_recall_of(recalled)}/progress", headers=admin,
                           json={"status": "returned", "contact_note": "电话联系到"})
    assert returned.status_code == 200, returned.text   # 修前 409「已在丙卫生院在管」，召回永远登不成
    assert _statuses(patient) == [(old, "excluded"), (recalled, "active")]


def test_没有别的档案_恢复照常(client, admin, world):
    patient = _patient(client, admin, world)
    enrollment = _enroll(client, admin, patient, world["orgs"]["c"])
    assert _event(client, admin, enrollment, "exclude", "外出务工").status_code == 200
    resume = _event(client, admin, enrollment, "resume", "回乡复诊")
    assert resume.status_code == 200 and resume.json()["enrollment"]["status"] == "active", resume.text
    assert _resume_events(enrollment) == 1


def test_召回中的这份自己_恢复与召回成功照常(client, admin, world):
    """要恢复的这份本身就是召回中：「另有」一份不算它自己。"""
    patient = _patient(client, admin, world)
    enrollment = _enroll(client, admin, patient, world["orgs"]["b"])
    assert _event(client, admin, enrollment, "recall", "失访").status_code == 200
    resume = _event(client, admin, enrollment, "resume", "自行来诊")
    assert resume.status_code == 200 and resume.json()["enrollment"]["status"] == "active", resume.text

    other = _patient(client, admin, world)
    recalled = _enroll(client, admin, other, world["orgs"]["b"])
    assert _event(client, admin, recalled, "recall", "失访").status_code == 200
    returned = client.post(f"{B}/recalls/{_recall_of(recalled)}/progress", headers=admin,
                           json={"status": "returned", "contact_note": "已联系"})
    assert returned.status_code == 200, returned.text
    assert _statuses(other) == [(recalled, "active")]
