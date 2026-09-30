"""补登分娩与已记的产前筛查对日期：先录筛查、后补登早于它的分娩原先照收（P2-1115，第三十二批「跨对象的业务时间先后」扫描 B2-3）。

P2-233 定下「日期晚于这一胎分娩日期的，不可能是这一胎的产前筛查」，但只在 `create_screening` 一侧判——「先有分娩、后录筛查」
的次序才拦得住。次序一反：没登记分娩的旧档案（上一胎外院分娩）先收进本次妊娠 9-10 的高风险 NIPT、被标成高危，随后补登
上一胎 2025-12-20 的分娩照样 201，上一胎带着这一胎的筛查和高危标记结案，新一胎仍是「正常」。同一个 `add_delivery` 对已记的
产前检查早已这么判（P2-1020：「分娩日期早于已记的产前检查」409）。

修法：`add_delivery` 在与访视互判的同一个临界区里把已记的筛查日期也对一遍，晚于分娩日的 409（与产前检查同一句）；
`create_screening` 的判定与写入一并圈进这份档案的 `serialized_on`——只圈分娩那一边挡不住筛查这一边（P2-1020 跟进同一个理由），
两路同时落下时各自读到「还没有对方」、都放行，倒挂照样进库。没登记分娩时记产后访视那一支随 P2-1035 待裁定，本文件不涉及。
"""
import itertools
import threading

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21115 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21115 孕妇", "id_card": f"33010619950505{next(_CARDS) + 1115:04d}", "gender": "女"}).json()["id"]
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _screen(client, admin, record, day, result="low_risk", screen_type="nipt"):
    return client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": screen_type, "screen_date": day, "result": result})


def _deliver(client, admin, org, record, day):
    return client.post(f"/api/maternal/records/{record}/delivery", headers=admin, json={"org_id": org, "delivery_date": day})


def _row(client, admin, record):
    return next(r for r in client.get("/api/maternal/records", headers=admin).json() if r["id"] == record)


def test_先录筛查_后补登早于它的分娩_409_档案不变(client, admin, org):
    record = _record(client, admin)
    screened = _screen(client, admin, record, "2026-09-10", result="high_risk")
    assert screened.status_code == 201 and screened.json()["flagged_high_risk"] is True, screened.text
    before = _row(client, admin, record)
    late = _deliver(client, admin, org, record, "2025-12-20")
    assert late.status_code == 409, late.text   # 修前 201：上一胎带着本次妊娠的高风险筛查结案
    assert late.json() == {"detail": "分娩日期 2025-12-20 早于已记的产前筛查 2026-09-10"}
    assert _row(client, admin, record) == before   # 状态仍是在册、没有分娩记录
    assert before["status"] == "registered" and before["has_delivery"] is False
    assert client.get(f"/api/maternal/records/{record}/delivery", headers=admin).status_code == 404


def test_筛查不晚于分娩日的照收(client, admin, org):
    record = _record(client, admin)
    assert _screen(client, admin, record, "2026-05-10", screen_type="down").status_code == 201
    assert _screen(client, admin, record, "2026-09-20", screen_type="ultrasound").status_code == 201   # 分娩当天的照收
    delivered = _deliver(client, admin, org, record, "2026-09-20")
    assert delivered.status_code == 201, delivered.text
    assert _row(client, admin, record)["has_delivery"] is True


def test_存量筛查日期按日历读_读不成的不参与(client, admin, org):
    """与同一处对访视的口径一致（`legacy_date`）：P1-61 之前的「2026/9/25」照样按日历比，「不详」这类读不成的不拦。"""
    from app.database import SessionLocal
    from app.models import PrenatalScreening, User

    record, other = _record(client, admin), _record(client, admin)
    with SessionLocal() as db:
        by = db.query(User.id).filter(User.username == "admin").scalar()
        for record_id, raw in ((record, "2026/9/25"), (other, "不详")):
            db.add(PrenatalScreening(record_id=record_id, screen_type="nipt", screen_date=raw, created_by=by))
        db.commit()
    legacy = _deliver(client, admin, org, record, "2026-09-20")
    assert legacy.status_code == 409 and "早于已记的产前筛查 2026-09-25" in legacy.json()["detail"], legacy.text
    assert _deliver(client, admin, org, other, "2026-09-20").status_code == 201


def test_分娩登记与晚于它的筛查同时落下_只进一条(client, admin, org, monkeypatch):
    """两个线程把交错钉成确定的时序（与 `test_spd_path_start_race.py` 同一个写法）：分娩这一路查完已记的筛查、写入之前停住；
    筛查那一路整个跑一遍；再放分娩这一路。筛查不进临界区时，它读到「还没登记分娩」、201 落库，分娩随后也 201——
    库里又是上一胎分娩之后挂着本次妊娠的筛查；圈进去之后筛查在临界区外等分娩提交，进来按分娩日判 409。"""
    from app.routers import maternal

    record = _record(client, admin)
    real_insert = maternal.insert_or_conflict
    checked, release = threading.Event(), threading.Event()

    def paused_insert(*args, **kwargs):
        if not checked.is_set():   # 分娩这一路：判定都过了、写入之前停住
            checked.set()
            release.wait(timeout=10)
        return real_insert(*args, **kwargs)

    monkeypatch.setattr(maternal, "insert_or_conflict", paused_insert)
    results = {}

    def deliver():
        results["delivery"] = _deliver(client, admin, org, record, "2025-12-20").status_code

    def screen():
        results["screening"] = _screen(client, admin, record, "2026-09-10", result="high_risk").status_code

    first = threading.Thread(target=deliver)
    first.start()
    assert checked.wait(timeout=10)
    second = threading.Thread(target=screen)
    second.start()
    second.join(timeout=1.5)   # 修前筛查此刻已经落库；修后它在临界区外等
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert results == {"delivery": 201, "screening": 409}, results   # 修前 {201, 201}
    assert client.get("/api/maternal/screenings", headers=admin, params={"record_id": record}).json() == []
    assert _row(client, admin, record)["high_risk"] is False
