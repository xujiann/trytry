"""数据质控「结束不得早于开始」把没填的结束日期判成违规（P2-234）。

`_check_datetime_order` 自己写着「结束为空视为进行中，不判违规」，却只认 `None`。平台的日期多是 `String(10)`、缺省
空串（`next_due`、各种 `*_date`）：空的结束日期按字符串比 `"" < "2026-…"` 恒真。实测（修前代码）：在慢病管理表上配
「下次随访日不得早于建档时间」（起 `created_at`、止 `next_due`），还没排下次随访的每一位都报「next_due（）早于
created_at（…）」，真早于建档时间的那条淹没在一片误报里。修法：起止任一为空（None 或空白串）都跳过，与
`date_not_future` 的判空同一个 `_is_blank`。
"""
import itertools

import pytest

_SEQ = itertools.count(1)


@pytest.fixture()
def rule(client, admin):
    code = f"P2234_{next(_SEQ)}"
    resp = client.post("/api/dataquality/rules", headers=admin, json={
        "code": code, "name": "下次随访日不得早于建档时间", "target_table": "chronic_patients", "rule_type": "logic",
        "config": {"check": "datetime_order", "start_field": "created_at", "end_field": "next_due"},
        "severity": "warn"})
    assert resp.status_code == 201, resp.text
    yield code
    client.delete(f"/api/dataquality/rules/{resp.json()['id']}", headers=admin)   # 别把这条规则留给别的用例的汇总


def test_没排下次随访的不算违规_真早于建档的照报(client, admin, rule):
    from app.database import SessionLocal
    from app.models import ChronicPatient

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2234 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2234 慢病", "id_card": "330127196808082234"}).json()["id"]
    with SessionLocal() as db:
        pending = ChronicPatient(patient_id=patient, disease="hypertension", managed_by_org_id=org, next_due="")
        wrong = ChronicPatient(patient_id=patient, disease="diabetes", managed_by_org_id=org, next_due="2000-01-01")
        db.add_all([pending, wrong])
        db.commit()
        ids = {"pending": pending.id, "wrong": wrong.id}
    run = client.get(f"/api/dataquality/run?rule_code={rule}&limit=1000", headers=admin)
    assert run.status_code == 200, run.text
    hit = {item["record_id"] for item in run.json()["items"]}
    assert ids["wrong"] in hit                    # 真早于建档时间的照报
    assert ids["pending"] not in hit, run.json()  # 修前：next_due（）早于 created_at（…）
