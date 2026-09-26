"""召回记录不随死亡收尾：死者名下的召回照旧「待联系」，还能登记「已重新纳管」（P2-260）。

召回（生命周期事件 recall）把档案置为召回中并建一条召回记录（待联系）；召回中的患者可以再登记死亡——死亡结案
（`close_open_work`）收掉任务、路径、干预、复诊与随访，召回记录不在其中：召回清单上死者照旧挂着「待联系」，召回进度
接口还能把它登成「已重新纳管」（档案已死亡，只是没被重新激活）。修法：登记死亡时把未结束的召回置为召回失败并写明
原因（回执的收尾计数里带上）；死者的召回不再收进度登记（409）。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2260 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _recalled(client, admin, world):
    """一份召回中的档案与它的召回记录编号。"""
    from app.database import SessionLocal
    from app.spd.models import SpdRecall

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2260 患者{world['n']}", "id_card": f"33012719751111{world['n']:04d}"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]}).json()["id"]
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                       json={"event": "recall", "reason": "失访三个月"})
    assert resp.status_code == 200, resp.text
    with SessionLocal() as db:
        return enrollment, db.query(SpdRecall).filter_by(enrollment_id=enrollment).one().id


def _recall(recall_id):
    from app.database import SessionLocal
    from app.spd.models import SpdRecall

    with SessionLocal() as db:
        r = db.get(SpdRecall, recall_id)
        return r.status, r.result, r.closed_at is not None


def test_登记死亡把未结束的召回收尾_回执带上条数(client, admin, world):
    enrollment, recall = _recalled(client, admin, world)
    client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                json={"status": "contacted", "contact_note": "电话未接通"})
    death = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                        json={"event": "death", "reason": "家属告知"})
    assert death.status_code == 200, death.text
    assert death.json()["closed"]["recalls"] == 1
    assert _recall(recall) == ("failed", "患者已登记死亡，召回终止", True)   # 修前 contacted：死者还在召回清单上


def test_死者的召回不再收进度登记(client, admin, world):
    enrollment, recall = _recalled(client, admin, world)
    client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": "death", "reason": "病故"})
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                       json={"status": "returned", "result": "已重新纳管"})
    assert resp.status_code == 409, resp.text   # 修前 200：死者的召回登成「已重新纳管」
    assert _recall(recall)[0] == "failed"


def test_活着的照旧登记进度(client, admin, world):
    _enrollment, recall = _recalled(client, admin, world)
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                       json={"status": "contacted", "contact_note": "已电话联系"})
    assert resp.status_code == 200 and resp.json()["status"] == "contacted", resp.text
