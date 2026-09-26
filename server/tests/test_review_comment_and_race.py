"""直播审核与医保双通道审核：意见参数不设上限（真 PG 撞列宽 500），审核翻转是锁外读改写（P2-312）。

两处都是 `POST …/review?approve=…&comment=…`：意见是查询参数、裸 `str`，落进 256 / 512 字的列——长了在真 PG 上撞列宽即 500，
开发库照存；状态是「读到待审 → 赋值 → commit」，两位主任同时审，一个批准、一个驳回，后提交的把先提交的结论改掉，
审核意见也跟着换。

修法：意见 `Query(max_length=列宽)`（超长 422）；审核与「还待审」压进同一条 UPDATE，后到的一路与顺序请求同一句 409。
"""
import pytest

from app.database import SessionLocal


@pytest.fixture(scope="module")
def world(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2312 患者", "id_card": "330127197309092312"}).json()["id"]
    return {"patient": patient}


def _live(client, admin):
    created = client.post("/api/education/live-sessions", headers=admin, json={"title": "P2312 高血压管理直播"})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def _dual(client, admin, world):
    created = client.post("/api/insurance/dual-channel", headers=admin, json={
        "patient_id": world["patient"], "drug_name": "P2312 靶向药", "reason": "门诊用药"})
    assert created.status_code in (200, 201), created.text
    return created.json()["id"]


def test_意见超过列宽422_不撞库(client, admin, world):
    live = client.post(f"/api/education/live-sessions/{_live(client, admin)}/review", headers=admin,
                       params={"approve": True, "comment": "长" * 257})
    assert live.status_code == 422, live.text   # 修前真 PG 撞列宽 500（开发库照存）
    dual = client.post(f"/api/insurance/dual-channel/{_dual(client, admin, world)}/review", headers=admin,
                       params={"approve": True, "comment": "长" * 513})
    assert dual.status_code == 422, dual.text
    ok = client.post(f"/api/education/live-sessions/{_live(client, admin)}/review", headers=admin,
                     params={"approve": True, "comment": "长" * 256})
    assert ok.status_code == 200, ok.text


def test_两人同时审直播_后到的409_不改掉先到的结论(client, admin, monkeypatch):
    from app.models import LiveSession
    from sqlalchemy.orm import Session

    session_id = _live(client, admin)
    real_get = Session.get

    def racing_get(self, model, ident, *args, **kwargs):
        row = real_get(self, model, ident, *args, **kwargs)
        if model is LiveSession and ident == session_id:
            with SessionLocal() as other:   # 这一路读到「待审」之后，另一位主任先驳回了
                other_row = real_get(other, LiveSession, session_id)
                other_row.status, other_row.review_comment = "rejected", "时间冲突"
                other.commit()
        return row

    monkeypatch.setattr(Session, "get", racing_get)
    got = client.post(f"/api/education/live-sessions/{session_id}/review", headers=admin,
                      params={"approve": True, "comment": "同意排期"})
    monkeypatch.undo()
    assert got.status_code == 409 and got.json()["detail"] == "该申请已审核", got.text   # 修前 200
    with SessionLocal() as db:
        row = db.get(LiveSession, session_id)
        assert (row.status, row.review_comment) == ("rejected", "时间冲突")   # 修前被改成 approved / 同意排期
