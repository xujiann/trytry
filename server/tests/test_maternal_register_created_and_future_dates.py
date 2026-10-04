"""孕产妇建册重提时回执不说「没有新建」；分娩日期、末次月经能填将来（P2-1305，第三十八批扫描 AB4-7 的 clear 部分）。

①建册 `register` 遇这位妇女在册（未结案）的档案就原样返回（一孕一册、建册幂等，P1-140），回执与新建一模一样、不带
`created`：按 B 超校正预产期重提建册（末次月经 03-29、预产期 2027-01-03、G3P1）201，回的还是 03-01 / 12-06 / G1P0，
页面整页重画、表单清空，像是改好了。照患者建档 P2-1244 的做法：回执只加 `created`（新建 true、命中 false），状态码照旧
201；建册页命中时说明「已有在册档案，本次填写未写入」，不整页重画。
②分娩登记 `delivery_date: DateStr` 没有上界：10-04 敲成 11-04 照收，产后访视（不得早于分娩日，P2-1020）与结案都被挡到
那天以后，分娩记录又改不了；末次月经同样能填将来。修后两者晚于今天的 422，与出生日期 P2-713 / 发病日期 P2-454 同一句
（只在这两个字段上按字段修，「事件日期普遍不拦将来」属 P2-443，待裁定）。

末次月经 / 预产期 / 孕产次与分娩记录的更正入口要业务拍板，不在本条。
"""
import itertools
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import business_today

PAGE = (Path(__file__).resolve().parents[1] / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")
#: `MaternalOut` 九键的原有次序，`created` 只追加在末尾
RECORD_KEYS = ["patient_id", "lmp", "edc", "gravidity", "parity", "high_risk", "risk_factors", "id", "status"]
_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21305 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _woman(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "P21305 孕妇", "id_card": f"33010619960606{next(_CARDS) + 1305:04d}", "gender": "女",
        "birth_date": "1996-06-06"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _rows(client, admin, patient):
    return [r for r in client.get("/api/maternal/records", headers=admin).json() if r["patient_id"] == patient]


def test_首次建册created_true_重提建册created_false且回执是原值(client, admin):
    patient = _woman(client, admin)
    first = client.post("/api/maternal/records", headers=admin, json={
        "patient_id": patient, "lmp": "2026-03-01", "edc": "2026-12-06"})
    assert first.status_code == 201, first.text
    body = first.json()
    assert list(body) == RECORD_KEYS + ["created"]   # 只加字段，原九键次序不动
    assert body["created"] is True                   # 修前没有这个键
    # 按 B 超校正预产期重提建册：状态码照旧 201，回的是原档案，本次所填一概没写进去
    again = client.post("/api/maternal/records", headers=admin, json={
        "patient_id": patient, "lmp": "2026-03-29", "edc": "2027-01-03", "gravidity": 3, "parity": 1})
    assert again.status_code == 201, again.text
    assert again.json() == {**body, "created": False}, again.text   # 修前回执与新建一模一样
    assert [(r["id"], r["lmp"], r["edc"], r["gravidity"], r["parity"]) for r in _rows(client, admin, patient)] == [
        (body["id"], "2026-03-01", "2026-12-06", 1, 0)]


def test_并发抢输的一路_回执同样created_false(client, admin, monkeypatch):
    """两路同时建册都查不到在册档案、都去插：抢输的一路撞「在册唯一」返回赢家那本，同样是命中、不是新建。"""
    from app.database import SessionLocal
    from app.models import MaternalRecord
    from app.routers import maternal

    patient = _woman(client, admin)
    real_insert = maternal.insert_if_absent

    def lose_the_race(db, obj):   # 另一路先一步建成了这本
        with SessionLocal() as other:
            other.add(MaternalRecord(patient_id=patient, lmp="2026-02-02", edc="2026-11-09", gravidity=2))
            other.commit()
        return real_insert(db, obj)

    monkeypatch.setattr(maternal, "insert_if_absent", lose_the_race)
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient, "lmp": "2026-03-01"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert (body["created"], body["lmp"], body["edc"], body["gravidity"]) == (False, "2026-02-02", "2026-11-09", 2)
    assert [r["id"] for r in _rows(client, admin, patient)] == [body["id"]]


def test_末次月经晚于今天_422_今天照收(client, admin):
    patient = _woman(client, admin)
    today = business_today()
    tomorrow = (today + timedelta(days=1)).isoformat()
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient, "lmp": tomorrow})
    assert resp.status_code == 422, resp.text   # 修前 201
    assert resp.json() == {"detail": f"末次月经（{tomorrow}）不得晚于今天"}
    assert _rows(client, admin, patient) == []
    # 预产期本来就在将来，不拦
    ok = client.post("/api/maternal/records", headers=admin, json={
        "patient_id": patient, "lmp": today.isoformat(), "edc": (today + timedelta(days=280)).isoformat()})
    assert ok.status_code == 201 and ok.json()["created"] is True, ok.text


def test_分娩日期晚于今天_422_档案不动_今天照收(client, admin, org):
    patient = _woman(client, admin)
    record = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient}).json()["id"]
    today = business_today().isoformat()
    tomorrow = (business_today() + timedelta(days=1)).isoformat()
    resp = client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                       json={"org_id": org, "delivery_date": tomorrow})
    assert resp.status_code == 422, resp.text   # 修前 201：产后访视与结案都被挡到那天以后，分娩记录又改不了
    assert resp.json() == {"detail": f"分娩日期（{tomorrow}）不得晚于今天"}
    (row,) = _rows(client, admin, patient)
    assert (row["status"], row["has_delivery"]) == ("registered", False)
    ok = client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                     json={"org_id": org, "delivery_date": today})
    assert ok.status_code == 201, ok.text
    # 分娩当天的产后访视（不填日期按今天）照收，随后即可结案
    visit = client.post(f"/api/maternal/records/{record}/visits", headers=admin, json={"visit_type": "postpartum"})
    assert visit.status_code == 201, visit.text
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 200


def test_建册页按created区分提示_命中不当成建好了():
    """页面据 `created` 提示：命中时说明「已有在册档案，本次填写未写入」、不整页重画（修前 postAction 成功即重画，不看回执）。"""
    handler = PAGE[PAGE.index('$("#mat-form").onsubmit'):]
    handler = handler[:handler.index('$("#child-form").onsubmit')]
    assert 'postAction("/api/maternal/records"' not in handler
    hit = handler.index("if (r.created === false) {")
    branch = handler[hit:handler.index("return;", hit)]
    assert "已有在册档案" in branch and "本次填写未写入" in branch
    assert "route()" not in branch                                    # 命中不重画：填的还在表单里、提示不被冲掉
    assert handler.index("route()", hit) > handler.index("return;", hit)   # 新建那一支照旧整页重画
    assert "body.high_risk = e.target.high_risk.checked;" in handler   # P2-857 的高危两项照送
