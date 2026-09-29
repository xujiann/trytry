"""医生移动端「今日随访 / 今日复诊」只数还没做完的（P2-734，第十九批「逆操作是否撤净」扫描 K2-8）。

两个数原先只按执行人 + 日期计，不看状态：死亡收尾（或手工移除）之后，同一屏上待办 0、今日任务 0，今日随访、今日复诊
仍各是 1。同一个 dict 里的今日任务、上面的到期计数都只数未结束的。修法：两处加上开放状态过滤。
"""
import pytest

from app import clock

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2734 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2734_doc", "password": "passw0rd1", "full_name": "P2734 张医生", "role": "doctor",
        "org_id": org})
    assert created.status_code in (200, 201), created.text
    doctor = _login(client, "p2734_doc")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2734 王老汉", "id_card": "330102195001012734"}).json()["id"]
    client.post("/api/encounters", headers=doctor, json={"patient_id": patient, "org_id": org, "diagnosis_name": "高血压"})
    enrollment = client.post(f"{B}/enrollments", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org,
        "doctor_user_id": created.json()["id"]})
    assert enrollment.status_code == 201, enrollment.text
    rule = client.post(f"{B}/followup-rules", headers=doctor, json={
        "code": "P2734_FR", "name": "P2734 高血压随访", "scene": "outpatient", "program_code": "hypertension",
        "points": [0], "questionnaire_code": "q_chronic"})
    assert rule.status_code == 201, rule.text
    today = clock.today().isoformat()
    plan = client.post(f"{B}/followup-plans", headers=doctor, json={
        "patient_id": patient, "rule_id": rule.json()["id"], "org_id": org,
        "executor_id": created.json()["id"], "base_date": today})
    assert plan.status_code in (200, 201), plan.text
    revisit = client.post(f"{B}/revisits", headers=doctor, json={
        "patient_id": patient, "program_code": "hypertension", "plan_date": today,
        "doctor_user_id": created.json()["id"]})
    assert revisit.status_code == 201, revisit.text
    return {"doctor": doctor, "enrollment": enrollment.json()["id"]}


def _calendar(client, world):
    return client.get(f"{B}/workbench/doctor-mobile", headers=world["doctor"]).json()["calendar"]


def test_死亡收尾之后今日随访复诊归零(client, world):
    before = _calendar(client, world)
    assert (before["followups"], before["revisits"]) == (1, 1), before
    closed = client.post(f"{B}/enrollments/{world['enrollment']}/lifecycle", headers=world["doctor"],
                         json={"event": "death", "reason": "病故"})
    assert closed.status_code == 200, closed.text
    after = _calendar(client, world)
    assert (after["followups"], after["revisits"]) == (0, 0), after   # 修前 (1, 1)：已移除的照数
