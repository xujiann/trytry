"""干预「办结」时填的「患者反馈」整格覆盖居民在手机上交的反馈（P2-961，第二十七批「丢失更新」扫描 G1-2）。

`spd_interventions.feedback` 只有一列，居民端（`portal.intervention_feedback`）与医护端改档（`care.update_intervention`）都整格写：
居民报的「吃药后头晕，早上量血压 95/60」被医护电话随访后办结时写的一句话整段替换，库里别处没有留存；反过来医护先写、
居民后交也一样。

修法：两处都追加（`service.feedback_appended`，与执行随访追加结果 P2-291 同一口径，不加来源标注——两处记的都是患者的反馈），
在同一行的临界区里 refresh 之后追加；第一段原样存（只一方写过的与修前一字不差），超出列宽时留最新的。
"""
import pytest
from test_portal_services import login

from app.database import SessionLocal

PHONE = "13700029610"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P2961 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p2961_prog", "name": "P2961 病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P2961 居民", "id_card": "330106196702020063", "phone": PHONE}).json()
    enroll = client.post("/api/spd/enrollments", headers=admin,
                         json={"patient_id": patient["id"], "program_code": "p2961_prog", "org_id": org})
    assert enroll.status_code == 201, enroll.text
    return {"patient": patient, "resident": login(client, PHONE)}


def _intervene(client, admin, world, goal):
    resp = client.post("/api/spd/interventions", headers=admin, json={
        "patient_ids": [world["patient"]["id"]], "program_code": "p2961_prog", "goal": goal, "content": "每日步行"})
    assert resp.status_code == 201, resp.text
    items = client.get("/api/portal/spd/interventions", headers=world["resident"]).json()
    return next(i for i in items if i["goal"] == goal)["id"]


def _feedback(intervention_id):
    from app.spd.models import SpdIntervention

    with SessionLocal() as db:
        return db.get(SpdIntervention, intervention_id).feedback


def test_居民先反馈_医护办结再填_两段都在(client, admin, world):
    iid = _intervene(client, admin, world, "P2961 居民先")
    said = client.post(f"/api/portal/spd/interventions/{iid}/feedback", headers=world["resident"],
                       json={"feedback": "吃药后头晕，早上量血压 95/60"})
    assert said.status_code == 200, said.text
    done = client.patch(f"/api/spd/interventions/{iid}", headers=admin,
                        json={"status": "done", "feedback": "电话随访：按时服药"})
    assert done.status_code == 200, done.text
    assert _feedback(iid) == "吃药后头晕，早上量血压 95/60；电话随访：按时服药"   # 修前只剩医护那句


def test_医护先写_居民后交_两段都在(client, admin, world):
    iid = _intervene(client, admin, world, "P2961 医护先")
    assert client.patch(f"/api/spd/interventions/{iid}", headers=admin,
                        json={"feedback": "已电话提醒减盐"}).status_code == 200
    said = client.post(f"/api/portal/spd/interventions/{iid}/feedback", headers=world["resident"],
                       json={"feedback": "这周开始少放盐了"})
    assert said.status_code == 200, said.text
    assert _feedback(iid) == "已电话提醒减盐；这周开始少放盐了"   # 修前只剩居民那句


def test_超出列宽_留最新的():
    from app.spd.service import feedback_appended

    merged = feedback_appended("旧" * 510, "今天血压 150/95")
    assert len(merged) == 512 and merged.endswith("旧；今天血压 150/95")
