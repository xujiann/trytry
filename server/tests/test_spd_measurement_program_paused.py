"""召回中 / 脱管的档案，不写病种的监测值照样按这个病种判级（P2-1675，第四十九批扫描 AM1-2）。

修前：`service.measure_program_for`（不写病种时推断挂哪个病种判级，P1-138）只在 `status == "active"` 的档案里找。召回登记
之后，居民自报不写病种的推断不出、挂空串，空串没有管理目标可比，一律判「正常」。扫描实测（`am1/r3_recall_measure.py`）：
收缩压 185 召回前判 high、召回后判 normal（手机上提示「已保存，指标正常」），同一个值写上病种编码又判 high——显式病种走
`enrollment_for`，召回中的本来就算。医护端单条录入走同一个帮手，同样挂空串、判 normal。

P2-1050 定过脱管 / 召回中的档案「还在、等着恢复，不是没有签约」（`ENROLLMENT_PAUSED_STATUSES`）。修法：在管档案里按现有
规则（有界目标优先、按建档先后）找不到的，再按同一规则在脱管 / 召回中的档案里找；已结束的（死亡、迁出、排除）仍不取。
只改判级挂哪个病种：召回中的异常值派不派处置任务（P2-512 待裁定）不动——医护端派任务另有「在管」闸门，照旧不派。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
PHONE = "13900016750"


def _idcard(body17):
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return body17 + "10X98765432"[sum(int(a) * b for a, b in zip(body17, weights)) % 11]


def _set_status(enrollment_id, status):
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        db.get(SpdEnrollment, enrollment_id).status = status
        db.commit()


def _disposal_tasks(patient_id):
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter(SpdTask.patient_id == patient_id,
                                        SpdTask.title.like("指标异常处置%")).count()


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21675 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    # 两个病种都给收缩压配了有界目标，界不同：185 在甲下判 high、在乙下判 normal——看得出挂的是哪个
    for code, high in (("p21675_a", 140), ("p21675_b", 200)):
        program = client.post(f"{B}/programs", headers=admin, json={
            "code": code, "name": f"{code} 病种", "category": "chronic", "stages": [{"key": "treat", "name": "治疗期"}]})
        assert program.status_code == 201, program.text
        target = client.post(f"{B}/programs/{program.json()['id']}/targets", headers=admin, json={
            "stage": "", "metric": "bp_sys", "metric_name": "收缩压", "target_high": high, "unit": "mmHg"})
        assert target.status_code == 201, target.text
    patients = {}
    for n, key in enumerate(("recalled", "lost", "excluded", "migrated", "both")):
        made = client.post("/api/patients", headers=admin, json={
            "name": f"P21675 居民{n}", "id_card": _idcard(f"3301061972030{n:04d}"), "gender": "女",
            **({"phone": PHONE} if key == "recalled" else {})})
        assert made.status_code in (200, 201), made.text
        patients[key] = made.json()["id"]

    def enroll(patient, program):
        resp = client.post(f"{B}/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org})
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def lifecycle(enrollment, event):
        resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": event, "reason": "P21675"})
        assert resp.status_code == 200, resp.text

    lifecycle(enroll(patients["recalled"], "hypertension"), "recall")
    _set_status(enroll(patients["lost"], "hypertension"), "lost")
    lifecycle(enroll(patients["excluded"], "hypertension"), "exclude")
    _set_status(enroll(patients["migrated"], "hypertension"), "migrated")
    # 并存：先建档的乙召回中，后建档的甲在管——取在管的甲（按建档先后混在一起排就挂到乙、判 normal）
    lifecycle(enroll(patients["both"], "p21675_b"), "recall")
    enroll(patients["both"], "p21675_a")

    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    resident = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code})
    assert resident.status_code == 200 and resident.json()["bound"], resident.text
    return {**patients, "resident": {"Authorization": f"Bearer {resident.json()['access_token']}"}}


def _staff(client, admin, patient, value=185):
    resp = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": patient, "metric": "bp_sys", "value": value, "unit": "mmHg"})
    assert resp.status_code == 201, resp.text
    return resp.json()["program_code"], resp.json()["level"]


def test_召回后居民自报不写病种_与显式带病种同判high(client, world):
    def report(**extra):
        resp = client.post("/api/portal/spd/measurements", headers=world["resident"], json={
            "metric": "bp_sys", "value": 185, "unit": "mmHg", **extra})
        assert resp.status_code == 201, resp.text
        return resp.json()["level"]

    assert report() == "high"   # 修前 normal
    assert report(program_code="hypertension") == "high"   # 显式带病种修前就是 high
    mine = client.get("/api/portal/spd/measurements", headers=world["resident"]).json()
    assert [m["level"] for m in mine] == ["high", "high"]


def test_召回中脱管的医护单条录入挂这个病种_派任务照旧不派(client, admin, world):
    for key in ("recalled", "lost"):
        assert _staff(client, admin, world[key]) == ("hypertension", "high"), key   # 修前 ("", "normal")
        assert _disposal_tasks(world[key]) == 0, key   # 不在管的不派处置任务（「在管」闸门），与修前一样


def test_排除迁出后照旧不推断(client, admin, world):
    for key in ("excluded", "migrated"):
        assert _staff(client, admin, world[key]) == ("", "normal"), key


def test_在管与暂停并存时取在管的(client, admin, world):
    assert _staff(client, admin, world["both"]) == ("p21675_a", "high")
    assert _disposal_tasks(world["both"]) == 1   # 挂在管的甲：异常照派处置任务
