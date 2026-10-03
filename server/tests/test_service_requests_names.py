"""统一申请单中心给一页配姓名只按本页的患者号取（P2-1153，第三十三批扫描 A4-9）。

`GET /api/service-requests` 原先为了给一页（缺省 200 条）配患者姓名，把整张患者表的（id, 姓名）读进内存，本页一条都没有也照读：
患者 6 万时返回 0 行、取回 60,004 行、365 ms（扫描实测）。随访中心的 `followups._name_maps` 早就只按本页的患者号用 `in_()` 取。

修后照 `_name_maps` 的写法按本页的患者号取，本页为空就不查；机构名照旧整表取（机构表是几十行的量，`_name_maps` 也是这么取的）。
响应不变（姓名不是加密列，PII 加密开态下照旧直读）。

- 特征化：两位有单据的患者（患者号与单据号错开）、两类单据、截断的一页与空页，每一条的姓名、机构名与原算法（整表对照）
  逐项相同——改前改后都绿；
- 缺陷：多建一批没有单据的患者，请求取回的行数不变（修前每个患者一行，本页为空也一样）。
"""
from datetime import datetime, timedelta

import pytest
from sqlalchemy import event, insert

from app.database import SessionLocal, engine
from app.models import Consultation, ExamRequest, Organization, Patient, User


@pytest.fixture(scope="module")
def world(client, admin):
    """五位患者，只有后两位有单据：单据号（1、2）与患者号错开，按错了号取姓名的写法才现形。"""
    base = datetime(2026, 9, 1, 8, 0)
    with SessionLocal() as db:
        org = Organization(name="申请单姓名卫生院", org_type="township", level="township")
        patients = [Patient(ehc_no=f"EHC-SRNAME-{i}", name=f"申请单患者{i}", id_card=f"33038219750505{i:04d}")
                    for i in range(5)]
        db.add_all([org, *patients])
        db.flush()
        operator = db.query(User.id).filter(User.username == "admin").scalar()
        for i, patient in enumerate(patients[3:]):
            db.add(ExamRequest(patient_id=patient.id, from_org_id=org.id, center_type="lab", item_code=f"SR{i}",
                               item_name="血常规", created_by=operator, created_at=base + timedelta(minutes=i)))
            db.add(Consultation(patient_id=patient.id, from_org_id=org.id, to_org_id=org.id, question=f"会诊{i}",
                                created_by=operator, created_at=base + timedelta(minutes=10 + i)))
        db.commit()
        return {"org": org.id, "patients": [p.id for p in patients]}


def _get(client, admin, params: dict) -> dict:
    resp = client.get("/api/service-requests", params=params, headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.parametrize("params", [{}, {"limit": 3}, {"request_type": "exam"}, {"request_type": "blood"}])
def test_特征化_每条的姓名与机构名与原算法相同(client, admin, world, params):
    body = _get(client, admin, params)
    with SessionLocal() as db:   # 修前的写法：整张患者表、整张机构表的（id, 名称）读出来对
        patient_names = {pid: name for pid, name in db.query(Patient.id, Patient.name).all()}
        org_names = {oid: name for oid, name in db.query(Organization.id, Organization.name).all()}
    for item in body["items"]:
        assert item["patient_name"] == patient_names.get(item["patient_id"], "")
        assert item["org_name"] == org_names.get(item["org_id"], "")


def test_特征化_姓名配得上(client, admin, world):
    body = _get(client, admin, {"limit": 3})
    assert [(i["request_type"], i["patient_name"], i["org_name"]) for i in body["items"]] == [
        ("consultation", "申请单患者4", "申请单姓名卫生院"),
        ("consultation", "申请单患者3", "申请单姓名卫生院"),
        ("exam", "申请单患者4", "申请单姓名卫生院"),
    ]
    assert _get(client, admin, {"request_type": "blood"})["items"] == []


def _rows_fetched(client, admin, params: dict) -> int:
    """这一个请求里全部 SELECT 取回的行数：拦下每条语句与参数，请求结束后在同一份数据上重放一遍数行数。"""
    seen: list[tuple] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            seen.append((statement, parameters))

    event.listen(engine, "before_cursor_execute", capture)
    try:
        _get(client, admin, params)
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert seen, "没拦到任何 SELECT（用例失效，请检查拦截方式）"
    with engine.connect() as conn:
        return sum(len(conn.exec_driver_sql(statement, parameters).fetchall()) for statement, parameters in seen)


@pytest.mark.parametrize("params", [{}, {"request_type": "blood"}])   # 有单据的一页、空页
def test_患者翻几番_取回行数不变(client, admin, world, params):
    before = _rows_fetched(client, admin, params)
    with SessionLocal() as db:
        start = db.query(Patient).count()
        db.execute(insert(Patient), [
            {"ehc_no": f"EHC-SRMORE-{start + i}", "name": f"没有单据的患者{start + i}",
             "id_card": f"33038219850303{start + i:04d}"} for i in range(120)])
        db.commit()
    after = _rows_fetched(client, admin, params)
    assert after == before, f"多了 120 个没有单据的患者，请求多取回了 {after - before} 行——还在把整张患者表读进内存配姓名"
