"""没登记分娩（外院分娩）的档案，产后访视之后再记「产前检查」照收（P2-1303，第三十八批扫描 AB4-5）。

`add_visit` 判「产前检查不得晚于这一胎结束」时只取登记了的分娩（`_delivered_on`）：本院只记了 09-20 产后访视的档案，
09-25 记「产前检查 40 周 150/95」201，已分娩的档案被标成高危「妊娠期高血压可能」；同一份档案同一天的产前筛查
（`create_screening` 用 `_pregnancy_ended_on`，没登记分娩时退到最早一次产后访视）却 409；这条错档的产检随后还把补登
09-19 的分娩挡住（409「分娩日期早于已记的产前检查 2026-09-25」）。`add_visit` 的注释自己写着与 P2-233 同一条规矩。

修法：产前检查的上界改用与筛查同一个帮手 `_pregnancy_ended_on`，报错文案照筛查那句「晚于这一胎的{依据} {日期}」；
登记了分娩的档案依据仍是分娩日期，文案逐字不变（P2-1020 的用例照绿）。错日期产后访视的更正入口属 P2-1035（待裁定）。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P21303 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21303 孕妇", "id_card": f"33010619950505{next(_CARDS) + 1303:04d}", "gender": "女"}).json()["id"]
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient, "lmp": "2025-12-10"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _visit(client, admin, record, **body):
    return client.post(f"/api/maternal/records/{record}/visits", headers=admin, json=body)


def _row(client, admin, record):
    return next(r for r in client.get("/api/maternal/records", headers=admin).json() if r["id"] == record)


def test_没登记分娩_晚于产后访视的产前检查409且不标高危(client, admin, org):
    record = _record(client, admin)
    assert _visit(client, admin, record, visit_type="postpartum", visit_date="2026-09-20").status_code == 201
    late = _visit(client, admin, record, visit_type="prenatal", gest_week=40, bp="150/95", visit_date="2026-09-25")
    assert late.status_code == 409, late.text   # 修前 201，已分娩的档案被标成高危
    assert late.json() == {"detail": "产前检查日期 2026-09-25 晚于这一胎的产后访视日期 2026-09-20，"
                                     "不是这一胎的产前检查：补录孕期的检查请填当时的检查日期"}
    row = _row(client, admin, record)
    assert row["high_risk"] is False and row["risk_factors"] == "", row
    # 不填日期按今天算：今天晚于 09-20 的产后访视，同样不是这一胎的产前检查
    undated = _visit(client, admin, record, visit_type="prenatal", gest_week=38, bp="150/95")
    assert undated.status_code == 409 and "晚于这一胎的产后访视日期 2026-09-20" in undated.json()["detail"], undated.text
    assert _row(client, admin, record)["high_risk"] is False
    # 与同一天的产前筛查同一个口径
    screening = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "ultrasound", "screen_date": "2026-09-25", "result": "high_risk"})
    assert screening.status_code == 409 and "产后访视日期 2026-09-20" in screening.json()["detail"], screening.text


def test_没登记分娩_孕期里的产前检查照收_补登分娩不再被错档的产检挡住(client, admin, org):
    record = _record(client, admin)
    assert _visit(client, admin, record, visit_type="postpartum", visit_date="2026-09-20").status_code == 201
    assert _visit(client, admin, record, visit_type="prenatal", gest_week=40, bp="150/95",
                  visit_date="2026-09-25").status_code == 409
    early = _visit(client, admin, record, visit_type="prenatal", gest_week=39, bp="150/95", visit_date="2026-09-19")
    assert early.status_code == 201, early.text   # 孕期里的检查照收，高血压照常标高危
    assert early.json()["high_risk"] is True
    # 修前那条 09-25 的「产前检查」落了库，补登 09-19 的分娩 409「早于已记的产前检查 2026-09-25」
    delivery = client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                           json={"org_id": org, "delivery_date": "2026-09-19"})
    assert delivery.status_code == 201, delivery.text


def test_登记了分娩的档案_依据仍是分娩日期_文案不变(client, admin, org):
    record = _record(client, admin)
    assert client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                       json={"org_id": org, "delivery_date": "2026-09-20"}).status_code == 201
    late = _visit(client, admin, record, visit_type="prenatal", gest_week=40, bp="150/95", visit_date="2026-09-25")
    assert late.status_code == 409, late.text
    assert late.json() == {"detail": "产前检查日期 2026-09-25 晚于这一胎的分娩日期 2026-09-20，"
                                     "不是这一胎的产前检查：补录孕期的检查请填当时的检查日期"}
    assert _row(client, admin, record)["high_risk"] is False
    assert _visit(client, admin, record, visit_type="prenatal", gest_week=39, visit_date="2026-09-20").status_code == 201
