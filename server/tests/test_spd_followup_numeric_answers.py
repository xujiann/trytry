"""慢专病随访问卷的数值题作答按题目校验：读不成数的、监测指标目录里 0 / 负数的一律 422（P2-711，第十八批「数值入参
的符号与业务上下界」扫描 V3-2）。

医护执行（`execute_followup`）与居民自助作答（`self_answer_followup`）都把 `answers` 原样交给 `grade_abnormal`：异常
规则按数比（`rules._as_number`），读不成数就判不命中。种子「慢病随访问卷」的收缩压是数值题，配 ≥180「立即上转评估」、
≥160「两周内复诊」：收缩压答 0、-185、「185/110」都照收，一条不命中、判「无异常」、不派处置任务；同一个指标键
`bp_sys` 走监测录入，0 早就 422（P1-101）。两端的数值题都是文本框，读不成数就原样交字符串。

修法：两处作答在办结之前按问卷题目校验——数值题的作答必须读得成有限的数；题目编码在监测指标目录里的（收缩压、
空腹血糖……）再过一遍 `measure_value_problem`（与监测录入同一句）。没作答的题、非数值题不管；失访不看作答。
"""
import pytest

from app import clock

B = "/api/spd"
P = "/api/portal/spd"
PROGRAM = "p2711_htn"
PHONE = "13900027110"
ID_CARD = "330281199104042711"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2711 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2711 居民", "id_card": ID_CARD, "gender": "男", "birth_date": "1991-04-04",
        "phone": PHONE}).json()["id"]
    assert client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2711 高血压", "category": "chronic"}).status_code == 201
    questionnaire = client.post(f"{B}/questionnaires", headers=admin, json={
        "code": "p2711_q", "name": "P2711 随访问卷", "scene": "outpatient",
        "items": [{"key": "bp_sys", "title": "自测收缩压", "type": "number"},
                  {"key": "note", "title": "其他情况", "type": "text"}],
        "abnormal_rules": [{"when": {"field": "bp_sys", "op": ">=", "value": 180}, "level": "high",
                            "action": "立即上转评估"}]})
    assert questionnaire.status_code == 201, questionnaire.text
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P2711 居民", "id_card": ID_CARD}, headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient, "ph": ph}


def _record(world):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        record = SpdFollowupRecord(patient_id=world["patient"], program_code=PROGRAM, questionnaire_code="p2711_q",
                                   org_id=world["org"], planned_at=clock.today().isoformat(), status="planned")
        db.add(record)
        db.commit()
        return record.id


def _status(record_id):
    from app.database import SessionLocal
    from app.spd.models import SpdFollowupRecord

    with SessionLocal() as db:
        return db.get(SpdFollowupRecord, record_id).status


@pytest.mark.parametrize("answer", [0, -185, "185/110"])
def test_医护执行_收缩压答0_负数_读不成数_422且不办结(client, admin, world, answer):
    rid = _record(world)
    resp = client.post(f"{B}/followup-records/{rid}/execute", headers=admin,
                       json={"answers": {"bp_sys": answer, "note": "无"}, "channel": "phone"})
    assert resp.status_code == 422, resp.text   # 修前 200、判「无异常」、不派「立即上转评估」
    assert "自测收缩压" in resp.json()["detail"]
    assert _status(rid) == "planned"
    ok = client.post(f"{B}/followup-records/{rid}/execute", headers=admin,
                     json={"answers": {"bp_sys": 185}, "channel": "phone"})
    assert ok.status_code == 200 and ok.json()["abnormal_level"] == "high", ok.text


def test_居民自助作答同一口径_读得成数的文本照收(client, world):
    rid = _record(world)
    resp = client.post(f"{P}/followups/{rid}/self-answer", headers=world["ph"],
                       json={"patient_id": world["patient"], "answers": {"bp_sys": 0}})
    assert resp.status_code == 422, resp.text   # 修前 200、判「无异常」
    assert _status(rid) == "planned"
    ok = client.post(f"{P}/followups/{rid}/self-answer", headers=world["ph"],
                     json={"patient_id": world["patient"], "answers": {"bp_sys": "185"}})
    assert ok.status_code == 200 and ok.json()["abnormal_level"] == "high", ok.text


def test_没作答的数值题与失访不查(client, admin, world):
    rid = _record(world)
    resp = client.post(f"{B}/followup-records/{rid}/execute", headers=admin,
                       json={"answers": {"note": "拒答血压"}, "channel": "phone"})
    assert resp.status_code == 200 and resp.json()["abnormal_level"] == "none", resp.text
    gone = client.post(f"{B}/followup-records/{_record(world)}/execute", headers=admin,
                       json={"answers": {"bp_sys": "没接通"}, "unreachable": True})
    assert gone.status_code == 200 and gone.json()["status"] == "unreachable", gone.text
