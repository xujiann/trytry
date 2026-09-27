"""失控点处理登记不看是不是刚被别人登记过：先登记的原因、纠正措施与处理人被后到的整段盖掉（P2-450）。

`POST /api/labqc/measurements/{id}/handle` 原先判「已处理」、赋值、commit，UPDATE 只有 `WHERE id = ?`：两位技师对同一个
失控点各点一次「登记处理」，两路都 200，库里留下后写的那份原因与措施、处理人记成后到的那位——而按顺序点第二下是 409
「该失控点已处理，勿重复登记」。失控处理记录是室内质控的追溯证据，先登记的那份不该被悄悄换掉。

修法：处理登记走条件翻转（`concurrency.move_row`，`WHERE handled IS FALSE`），抢输的一路 409、与顺序调用同一句。
这里把「判过了、还没写」钉成确定的时序：机构归属校验之后、写入之前，让另一位技师先登记并提交。
"""
import pytest

from conftest import login

from app.database import SessionLocal


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2450 检验科医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/users", headers=admin, json={
        "username": "p2450_lab", "password": "pass123456", "role": "doctor", "org_id": org})
    doc = login(client, "p2450_lab", "pass123456")
    lot = client.post("/api/labqc/lots", headers=doc, json={
        "org_id": org, "item_code": "K", "item_name": "血清钾", "lot_no": "P2450-L1", "target_value": 5.0, "sd": 0.5})
    assert lot.status_code == 201, lot.text
    return {"doc": doc, "lot": lot.json()["id"]}


def _out_of_control(client, world):
    point = client.post(f"/api/labqc/lots/{world['lot']}/measurements", headers=world["doc"], json={"value": 7.0})
    assert point.status_code == 201 and point.json()["out_of_control"], point.text
    return point.json()["id"]


def _meanwhile(monkeypatch, mid):
    """机构归属校验之后（「已处理」判定、写入之前），另一位技师登记处理并提交。"""
    from app.models import QcMeasurement
    from app.routers import labqc

    real, fired = labqc.assert_obj_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(QcMeasurement, mid)
                row.handled, row.handle_reason = True, "质控品复溶超时失效"
                row.corrective_action, row.handled_by = "更换新支质控品复测", "技师甲"
                other.commit()
        return result

    monkeypatch.setattr(labqc, "assert_obj_org_writable", racing)
    return fired


def _row(mid):
    from app.models import QcMeasurement

    with SessionLocal() as db:
        row = db.get(QcMeasurement, mid)
        return row.handled, row.handle_reason, row.corrective_action, row.handled_by


def test_两位技师同时登记_后到的409_先登记的那份不被盖掉(client, world, monkeypatch):
    mid = _out_of_control(client, world)
    fired = _meanwhile(monkeypatch, mid)
    resp = client.post(f"/api/labqc/measurements/{mid}/handle", headers=world["doc"],
                       json={"reason": "仪器漂移", "corrective_action": "重新定标"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "该失控点已处理，勿重复登记"}
    assert _row(mid) == (True, "质控品复溶超时失效", "更换新支质控品复测", "技师甲")   # 修前被「仪器漂移」一份盖掉


def test_按顺序登记照旧(client, world):
    mid = _out_of_control(client, world)
    first = client.post(f"/api/labqc/measurements/{mid}/handle", headers=world["doc"],
                        json={"reason": "仪器漂移", "corrective_action": "重新定标"})
    assert first.status_code == 200, first.text
    assert (first.json()["handled"], first.json()["handle_reason"]) == (True, "仪器漂移")
    assert first.json()["handled_by"]
    again = client.post(f"/api/labqc/measurements/{mid}/handle", headers=world["doc"],
                        json={"reason": "x", "corrective_action": "y"})
    assert (again.status_code, again.json()) == (409, {"detail": "该失控点已处理，勿重复登记"})
