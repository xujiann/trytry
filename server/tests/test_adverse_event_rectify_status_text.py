"""已整改的不良事件再点「登记整改」，提示「须先审核后方可登记整改」（P2-1570，第四十六批扫描 AJ2-8）。

修前（scan46 aj2 `r1_adverse.py` 第 7 步实测）：`rectify_adverse_event` 只要状态不是「已审核」一律 409「须先审核后方可登记
整改」。事件明明已审核、已整改，提示却让人以为审核没通过——两人同时开着页面、一人已经整改时，另一人点按钮看到的就是这句。
同文件审核那条路径早就按状态给文案（「当前状态 已整改 不可审核」，P2-74）。

修后：「已上报」时照旧提示须先审核；其余状态用「当前状态 {状态名} 不可登记整改」，状态名取 `ADVERSE_EVENT_STATUS_NAMES`。
状态码仍是 409，整改内容一个字不动。
"""
import pytest

from app.database import SessionLocal
from app.models import AdverseEvent
from conftest import login

E = "/api/quality/adverse-events"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21570 县医院", "org_type": "lead_hospital", "level": "county"})
    assert org.status_code in (200, 201), org.text
    heads = {}
    for username, role in (("p21570_doc", "doctor"), ("p21570_dir", "director"), ("p21570_op", "operator")):
        made = client.post("/api/users", headers=admin, json={
            "username": username, "password": "passw0rd1", "role": role, "org_id": org.json()["id"]})
        assert made.status_code in (200, 201), made.text
        heads[role] = login(client, username, "passw0rd1")
    return {"org": org.json()["id"], **heads}


def _report(client, world):
    made = client.post(E, headers=world["doctor"], json={
        "org_id": world["org"], "event_type": "fall", "level": "II", "description": "P21570 夜间跌倒"})
    assert made.status_code == 201, made.text
    return made.json()["id"]


def test_已整改的再登记整改_409_文案说清当前状态(client, world):
    eid = _report(client, world)
    assert client.post(f"{E}/{eid}/review", headers=world["director"], json={"note": "属实"}).status_code == 200
    first = client.post(f"{E}/{eid}/rectify", headers=world["operator"], json={"note": "加床栏"})
    assert first.status_code == 200 and first.json()["status"] == "rectified", first.text
    again = client.post(f"{E}/{eid}/rectify", headers=world["director"], json={"note": "改措施"})
    assert again.status_code == 409, again.text
    assert again.json() == {"detail": "当前状态 已整改 不可登记整改"}   # 修前「须先审核后方可登记整改」
    with SessionLocal() as db:   # 挡下时整改内容一个字不动
        event = db.get(AdverseEvent, eid)
        assert (event.status, event.rectify_note) == ("rectified", "加床栏")


def test_已上报未审核的_照旧提示须先审核(client, world):
    eid = _report(client, world)
    got = client.post(f"{E}/{eid}/rectify", headers=world["operator"], json={"note": "过早"})
    assert got.status_code == 409 and got.json() == {"detail": "须先审核后方可登记整改"}, got.text
