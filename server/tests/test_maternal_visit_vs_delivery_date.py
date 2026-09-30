"""孕产妇访视与这一胎登记了的分娩日期互不核对：产后访视能早于分娩、产前检查能晚于分娩（P2-1020，第二十九批「日期算术」扫描 E4-2）。

原先 `add_visit` 不看分娩日、`add_delivery` 不看已记的访视：分娩 09-20 之后记一条 08-20 的「产后访视」201、随即结案 200；
分娩之后记「产前检查 40 周 150/95」201，已分娩的档案被标成高危「妊娠期高血压可能」。P2-233 已经为筛查定下「日期晚于这一胎
分娩日期的，不可能是这一胎的产前筛查」，结案的文案也写「须完成产后访视（分娩后）」。

修法：登记了分娩的，产后访视早于分娩日 409、产前检查晚于分娩日 409（没填访视日期的按今天算）；补登分娩时与已记的、填了
日期的访视对一遍；结案只认分娩日及以后的产后访视。与末次月经 / 预产期 / 孕周对不对得上、没登记分娩时产后访视的下界，
要产科定口径，另行待裁定。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21020 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21020 孕妇", "id_card": f"33010619940404{next(_CARDS) + 1020:04d}", "gender": "女"}).json()["id"]
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _visit(client, admin, record, **body):
    return client.post(f"/api/maternal/records/{record}/visits", headers=admin, json=body)


def _deliver(client, admin, org, record, day):
    return client.post(f"/api/maternal/records/{record}/delivery", headers=admin, json={"org_id": org, "delivery_date": day})


def test_登记了分娩_早于分娩日的产后访视与晚于分娩日的产前检查都拒收(client, admin, org):
    record = _record(client, admin)
    assert _deliver(client, admin, org, record, "2026-09-20").status_code == 201
    early = _visit(client, admin, record, visit_type="postpartum", visit_date="2026-08-20")
    assert early.status_code == 409, early.text   # 修前 201，随即就能结案
    late = _visit(client, admin, record, visit_type="prenatal", gest_week=40, bp="150/95", visit_date="2026-09-25")
    assert late.status_code == 409, late.text     # 修前 201，已分娩的档案被标成高危
    rows = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in rows if r["id"] == record)["high_risk"] is False
    undated = _visit(client, admin, record, visit_type="prenatal", gest_week=38)   # 没填日期按今天，晚于分娩
    assert undated.status_code == 409 and "请填当时的检查日期" in undated.json()["detail"]
    # 分娩当天的产后访视、孕期里的产前检查照收
    assert _visit(client, admin, record, visit_type="postpartum", visit_date="2026-09-20").status_code == 201
    assert _visit(client, admin, record, visit_type="prenatal", gest_week=36, visit_date="2026-09-01").status_code == 201


def test_补登分娩日期与已记的访视倒挂_409(client, admin, org):
    record = _record(client, admin)
    assert _visit(client, admin, record, visit_type="prenatal", gest_week=39, visit_date="2026-09-18").status_code == 201
    too_early = _deliver(client, admin, org, record, "2026-09-10")
    assert too_early.status_code == 409 and "早于已记的产前检查 2026-09-18" in too_early.json()["detail"]
    other = _record(client, admin)
    assert _visit(client, admin, other, visit_type="postpartum", visit_date="2026-09-25").status_code == 201
    too_late = _deliver(client, admin, org, other, "2026-09-28")
    assert too_late.status_code == 409 and "晚于已记的产后访视 2026-09-25" in too_late.json()["detail"]
    assert _deliver(client, admin, org, other, "2026-09-22").status_code == 201


def test_结案只认分娩日及以后的产后访视(client, admin, org):
    from app.database import SessionLocal
    from app.models import MaternalVisit

    record = _record(client, admin)
    assert _deliver(client, admin, org, record, "2026-09-20").status_code == 201
    with SessionLocal() as db:   # 存量：修之前记进去的、日期早于分娩的产后访视
        db.add(MaternalVisit(record_id=record, visit_type="postpartum", visit_date="2026-08-20"))
        db.commit()
    blocked = client.post(f"/api/maternal/records/{record}/close", headers=admin)
    assert blocked.status_code == 409 and "都早于分娩日期" in blocked.json()["detail"]   # 修前 200
    assert _visit(client, admin, record, visit_type="postpartum", visit_date="2026-09-27").status_code == 201
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 200
