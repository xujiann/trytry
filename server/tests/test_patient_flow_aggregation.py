"""patient-flow 聚合下推（工程包 P2）的特征化测试。

/api/analytics/patient-flow 此前把 OutboundVisit **全行取回**内存里逐行数
分层/数转诊/加金额——县外就诊表按年增长，月报把历史全量搬一遍。改成
数据库内聚合之前，先把端点的完整响应 JSON 锁死（CLAUDE.md §11：治理不得
改响应字节），改写必须在这张网全绿的前提下进行。

数据刻意覆盖的边界：
- 期前/期内/期后（end 为**排他**边界）的县外就诊；
- 三个机构层级（city/province/other）都有记录，分层计数逐一可辨；
- 关联转诊单与自行外出并存（有序转诊率的分子分母）；
- 县内就诊（Encounter）同样有期外记录，须被日期过滤排除；
- 金额带小数，验证 round 口径不变。

第二张网锁"查询形状"：对 outbound_visits 发出的查询必须带聚合（GROUP BY），
把实现改回"全行取回"时特征化仍绿、这里必须变红——两张网合起来才证明
改写等价且真聚合（同 test_analytics_aggregation_characterization.py 的方法）。
"""
from contextlib import contextmanager
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event

from conftest import reset_database

from app.database import SessionLocal, engine
from app.main import app
from app.models import Encounter, Organization, OutboundVisit, Patient, Referral, User

START, END = "2026-05-01", "2026-06-01"


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """直接落库造数：特征化需要精确控制跨月时间戳，走接口做不到。"""
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org_a = Organization(name="流向特征化甲医院", org_type="lead_hospital", level="county")
        org_b = Organization(name="流向特征化乙医院", org_type="township", level="township")
        db.add_all([org_a, org_b])
        db.flush()

        patient = Patient(ehc_no="EHC-FLOW-0001", name="流向特征化患者", id_card="330382199202024321")
        db.add(patient)
        db.flush()

        referral = Referral(
            patient_id=patient.id, from_org_id=org_a.id, to_org_id=org_b.id,
            direction="up", status="completed", created_by=admin_id,
        )
        db.add(referral)
        db.flush()

        def enc(created):
            db.add(Encounter(patient_id=patient.id, org_id=org_a.id, doctor_name="流向医师",
                             diagnosis_name="流向诊断", created_at=created))

        enc(datetime(2026, 5, 2, 9, 0))
        enc(datetime(2026, 5, 31, 23, 0))
        enc(datetime(2026, 5, 15, 12, 0))
        enc(datetime(2026, 4, 30, 23, 59))  # 期前，须被排除
        enc(datetime(2026, 6, 1, 0, 0))     # 期后（end 排他），须被排除

        def out(visit_date, level, amount, referral_id=None):
            db.add(OutboundVisit(
                patient_id=patient.id, visit_date=visit_date,
                external_org_name="县外某院", external_org_level=level,
                visit_type="outpatient", total_amount=amount,
                insurance_pay=0, referral_id=referral_id, created_by=admin_id,
            ))

        out("2026-05-03", "city", 1200.50, referral.id)   # 有序转出
        out("2026-05-10", "city", 800.25)                 # 自行外出
        out("2026-05-20", "province", 5000.00, referral.id)
        out("2026-05-28", "other", 300.30)
        out("2026-04-28", "city", 999.99)                 # 期前，须被排除
        out("2026-06-01", "province", 888.88)             # 期后（end 排他），须被排除

        db.commit()
    finally:
        db.close()
    return True


EXPECTED = {
    "inside_visits": 3,
    "outside_visits": 4,
    "total_visits": 7,
    "county_visit_rate_pct": round(3 * 100 / 7, 2),
    "outbound_rate_pct": round(100 - round(3 * 100 / 7, 2), 2),
    "referred_outbound": 2,
    "ordered_referral_rate_pct": 50.0,
    "outside_by_level": {"city": 2, "province": 1, "other": 1},
    "outside_amount": round(1200.50 + 800.25 + 5000.00 + 300.30, 2),
}


def test_patient_flow_characterization(client, admin, world):
    resp = client.get(f"/api/analytics/patient-flow?start={START}&end={END}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == EXPECTED


def test_patient_flow_no_range_characterization(client, admin, world):
    """不带日期即全量：期外记录也计入（6 条县外、5 条县内）。"""
    resp = client.get("/api/analytics/patient-flow", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["inside_visits"] == 5
    assert body["outside_visits"] == 6
    assert body["outside_by_level"] == {"city": 3, "province": 2, "other": 1}
    assert body["referred_outbound"] == 2
    assert body["outside_amount"] == round(
        1200.50 + 800.25 + 5000.00 + 300.30 + 999.99 + 888.88, 2
    )


def test_patient_flow_empty_range_characterization(client, admin, world):
    """空区间：全部为零、比率不除零。"""
    resp = client.get(
        "/api/analytics/patient-flow?start=2020-01-01&end=2020-02-01", headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "inside_visits": 0,
        "outside_visits": 0,
        "total_visits": 0,
        "county_visit_rate_pct": 0.0,
        "outbound_rate_pct": 0.0,
        "referred_outbound": 0,
        "ordered_referral_rate_pct": 0.0,
        "outside_by_level": {},
        "outside_amount": 0.0,
    }


# ===========================================================================
# 性能形状断言：聚合必须发生在数据库内（非空洞：改回全行取回即红）
# ===========================================================================


@contextmanager
def _capture_sql():
    statements: list[str] = []

    def before(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", before)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", before)


def test_patient_flow_aggregates_in_database(client, admin, world):
    with _capture_sql() as statements:
        assert client.get(
            f"/api/analytics/patient-flow?start={START}&end={END}", headers=admin
        ).status_code == 200
    touching = [s for s in statements if "outbound_visits" in s]
    assert touching, "未捕获到访问 outbound_visits 的查询（用例失效，请检查抓取方式）"
    ungrouped = [s for s in touching if "GROUP BY" not in s]
    assert not ungrouped, (
        "以下访问 outbound_visits 的查询没有 GROUP BY（明细被整表拉进内存聚合）：\n"
        + "\n---\n".join(ungrouped)
    )
