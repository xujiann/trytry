"""远程会诊受理的专家与界面同一个规矩：库里有可排班的专家就只能从他们里选（P2-764，第二十批「停用 / 注销 / 作废的对象仍在
被用」扫描 M3-8）。

界面（`core.js` 受理会诊）只列可排班的专家、库为空才退回手填，注释写着「后端 accept 只收字符串不校验，于是统计里
"谁接得多"永远是一笔糊涂账」。后端原先确实照收：页面打开之后专家被设成暂停排班，旧页面照样能选他受理；直接调接口填
任意名字也 200，「谁接得多 / 评分」按名字分组，里面混进不存在的人。
"""
import pytest

from app.database import SessionLocal
from app.models import Consultation, ConsultExpert

B = "/api/consultations"


@pytest.fixture(scope="module")
def world(client, admin):
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2764 {name}", "org_type": otype, "level": level}).json()["id"]
        for name, otype, level in (("申请卫生院", "township", "township"), ("受邀县医院", "lead_hospital", "county"))]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2764 会诊患者", "id_card": "330106197003032764"}).json()["id"]
    return {"from": orgs[0], "to": orgs[1], "patient": patient}


def _consultation(client, admin, world):
    created = client.post(B, headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["from"], "to_org_id": world["to"], "question": "P2764 会诊"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _accept(client, admin, cid, name):
    return client.post(f"{B}/{cid}/accept", headers=admin, json={"expert_name": name})


def _status(cid):
    with SessionLocal() as db:
        row = db.get(Consultation, cid)
        return row.status, row.expert_name


def test_专家库为空_手填照收(client, admin, world):
    cid = _consultation(client, admin, world)
    resp = _accept(client, admin, cid, "手填的李主任")
    assert resp.status_code == 200, resp.text
    assert _status(cid) == ("accepted", "手填的李主任")


def test_库里有可排班的专家_暂停排班的409_库外名字422_可排班的照常(client, admin, world):
    for name, available in (("P2764 王主任", False), ("P2764 赵主任", True)):
        assert client.post(f"{B}/experts", headers=admin, json={
            "name": name, "org_id": world["to"], "specialty": "心内科", "available": available}).status_code == 201
    cid = _consultation(client, admin, world)
    paused = _accept(client, admin, cid, "P2764 王主任")
    assert paused.status_code == 409, paused.text   # 修前 200：旧页面上照选暂停排班的专家
    assert paused.json()["detail"] == "该专家已暂停排班，不能受理"
    outsider = _accept(client, admin, cid, "不存在的专家")
    assert outsider.status_code == 422, outsider.text   # 修前 200：统计里混进不存在的人
    assert outsider.json()["detail"] == "受理专家须从专家库里可排班的专家中选"
    assert _status(cid) == ("applied", "")
    ok = _accept(client, admin, cid, "P2764 赵主任")
    assert ok.status_code == 200, ok.text
    assert _status(cid) == ("accepted", "P2764 赵主任")


def test_库里的专家都暂停排班_与界面一样退回手填(client, admin, world):
    with SessionLocal() as db:
        db.query(ConsultExpert).update({"available": False})
        db.commit()
    cid = _consultation(client, admin, world)
    resp = _accept(client, admin, cid, "临时请来的孙主任")
    assert resp.status_code == 200, resp.text
    assert _status(cid) == ("accepted", "临时请来的孙主任")
