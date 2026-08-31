"""统计接口 SQL 聚合化（生产整改 A3）的特征化测试。

/api/analytics/efficiency 与 /api/analytics/drug-use 此前把历史全量 Admission、
整月 BillDetail / PrescriptionItem 拉进内存逐行聚合。改成数据库内聚合之前，
先用一批**跨机构、跨月份、含边界日期**的数据把两个端点的完整响应 JSON 锁死
（CLAUDE.md §11：治理不得改响应字节）——聚合化改写必须在这张网全绿的前提下进行。

数据刻意覆盖的边界：
- 入院在期前、出院在期内（占用床日只算重叠段）；
- 期内入院、仍在院（discharged_at 为空，占用算到期末）；
- 全程在期外的住院（不得出现在任何数字里）；
- 当日入出院（住院日/床日按 1 天下限计）；
- 出院在期后的在院跨期病例（占用算重叠段、不计出院人次）；
- 账单/处方/病案首页各有期外记录（必须被日期过滤排除）；
- DDD=0 的抗菌药（计未覆盖数）、停用的抗菌药规则（不参与）、非抗菌药（不参与）；
- 一家无任何业务数据的机构（efficiency 不出行、drug-use 出全零行）。
"""
from contextlib import contextmanager
from datetime import datetime

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import (
    Admission,
    Bed,
    BillDetail,
    CaseSummary,
    ChargeItem,
    DrugRule,
    Employee,
    Encounter,
    Organization,
    Patient,
    Prescription,
    PrescriptionItem,
    User,
    Ward,
)

PERIOD = "2026-05"


@pytest.fixture(scope="module")
def world(client):
    """直接落库造数：特征化需要精确控制跨月时间戳，走接口做不到。"""
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        a = Organization(name="特征化甲县医院", org_type="lead_hospital", level="county")
        b = Organization(name="特征化乙卫生院", org_type="township", level="township")
        c = Organization(name="特征化丙空机构", org_type="township", level="township")
        db.add_all([a, b, c])
        db.flush()

        patient = Patient(ehc_no="EHC-CHAR-0001", name="特征化患者", id_card="330382199001015678")
        db.add(patient)
        db.flush()

        ward_a = Ward(org_id=a.id, name="特征化A病区")
        ward_b = Ward(org_id=b.id, name="特征化B病区")
        db.add_all([ward_a, ward_b])
        db.flush()
        beds = [
            Bed(ward_id=ward_a.id, bed_no="A1"),
            Bed(ward_id=ward_a.id, bed_no="A2"),
            Bed(ward_id=ward_a.id, bed_no="A3"),
            Bed(ward_id=ward_b.id, bed_no="B1"),
            Bed(ward_id=ward_b.id, bed_no="B2"),
        ]
        db.add_all(beds)
        db.flush()

        db.add_all([
            Employee(org_id=a.id, name="甲内科", position="内科医师", status="active"),
            Employee(org_id=a.id, name="甲外科", position="外科医生", status="active"),
            Employee(org_id=a.id, name="甲护士", position="护士", status="active"),
            Employee(org_id=a.id, name="甲离职", position="内科医师", status="left"),
            Employee(org_id=b.id, name="乙全科", position="全科医师", status="active"),
        ])

        def adm(org_id, bed, admitted, discharged):
            row = Admission(
                patient_id=patient.id, org_id=org_id, ward_id=bed.ward_id, bed_id=bed.id,
                status="discharged" if discharged else "admitted",
                admitted_at=admitted, discharged_at=discharged, created_by=admin_id,
            )
            db.add(row)
            db.flush()
            return row

        # 期前入院、期内出院：占用重叠 5/1→5/5=4 天；住院日(日期级)=15；床日(时刻级)=14
        adm1 = adm(a.id, beds[0], datetime(2026, 4, 20, 10, 0), datetime(2026, 5, 5, 9, 0))
        # 期内入院、仍在院：占用 5/10→6/1=22 天，不计出院
        adm(a.id, beds[1], datetime(2026, 5, 10, 8, 0), None)
        # 全程期外（3 月），任何数字都不该出现它
        adm3 = adm(a.id, beds[2], datetime(2026, 3, 1, 9, 0), datetime(2026, 3, 10, 9, 0))
        # 当日入出院：住院日与床日都按下限 1 天
        adm4 = adm(b.id, beds[3], datetime(2026, 5, 15, 12, 0), datetime(2026, 5, 15, 18, 0))
        # 期后入院（6/2），应被 admitted_at < 期末 过滤
        adm(b.id, beds[4], datetime(2026, 6, 2, 8, 0), None)
        # 期末跨期：占用 5/30→6/1=2 天；出院在期后不计出院人次
        adm(a.id, beds[0], datetime(2026, 5, 30, 8, 0), datetime(2026, 6, 3, 9, 0))

        def enc(org_id, created):
            row = Encounter(patient_id=patient.id, org_id=org_id, doctor_name="特征化医师",
                            diagnosis_name="特征化诊断", created_at=created)
            db.add(row)
            db.flush()
            return row

        enc_a1 = enc(a.id, datetime(2026, 5, 2, 9, 0))
        enc(a.id, datetime(2026, 5, 12, 9, 0))
        enc(a.id, datetime(2026, 5, 31, 23, 0))
        enc(a.id, datetime(2026, 4, 30, 23, 0))   # 期前
        enc(a.id, datetime(2026, 6, 1, 10, 0))    # 期后
        enc_b1 = enc(b.id, datetime(2026, 5, 20, 9, 0))

        # ---- drug-use：收费目录 / 账单明细 / 病案首页 / 抗菌药规则与处方 ----
        db.add_all([
            ChargeItem(code="CHAR-DRUG-A", name="特征化药品A", category="drug", price=10),
            ChargeItem(code="CHAR-TRT-B", name="特征化治疗B", category="treatment", price=20),
        ])

        def bill(encounter, code, amount, created):
            db.add(BillDetail(patient_id=patient.id, encounter_id=encounter.id,
                              item_code=code, item_name=code, unit_price=amount,
                              quantity=1, amount=amount, created_by=admin_id,
                              created_at=created))

        bill(enc_a1, "CHAR-DRUG-A", 30.5, datetime(2026, 5, 2, 10, 0))
        bill(enc_a1, "CHAR-TRT-B", 20.0, datetime(2026, 5, 2, 10, 5))
        bill(enc_a1, "CHAR-DRUG-A", 99.0, datetime(2026, 4, 2, 10, 0))  # 期外
        bill(enc_b1, "CHAR-DRUG-A", 10.0, datetime(2026, 5, 20, 10, 0))

        db.add_all([
            CaseSummary(admission_id=adm1.id, discharge_diagnosis="特征化",
                        total_cost=1000.0, drug_cost=300.0,
                        created_at=datetime(2026, 5, 5, 9, 30)),
            CaseSummary(admission_id=adm4.id, discharge_diagnosis="特征化",
                        total_cost=500.0, drug_cost=100.0,
                        created_at=datetime(2026, 5, 15, 18, 30)),
            CaseSummary(admission_id=adm3.id, discharge_diagnosis="特征化",
                        total_cost=777.0, drug_cost=111.0,
                        created_at=datetime(2026, 3, 10, 9, 30)),  # 期外
        ])

        db.add_all([
            DrugRule(drug_code="CHAR-ABX-1", max_daily_dose=4, antibiotic=True, active=True, ddd=1.0),
            DrugRule(drug_code="CHAR-ABX-0", max_daily_dose=4, antibiotic=True, active=True, ddd=0),
            DrugRule(drug_code="CHAR-ABX-X", max_daily_dose=4, antibiotic=True, active=False, ddd=2.0),
            DrugRule(drug_code="CHAR-NORM", max_daily_dose=4, antibiotic=False, active=True, ddd=1.0),
        ])

        def rx(org_id, created, items):
            row = Prescription(patient_id=patient.id, org_id=org_id,
                               created_by=admin_id, created_at=created)
            db.add(row)
            db.flush()
            for code, daily, days in items:
                db.add(PrescriptionItem(prescription_id=row.id, drug_code=code,
                                        drug_name=code, daily_dose=daily, days=days))

        rx(a.id, datetime(2026, 5, 3, 9, 0), [
            ("CHAR-ABX-1", 2.0, 3),   # 6.0 DDDs
            ("CHAR-ABX-0", 1.0, 5),   # DDD 未维护 → 未覆盖 +1
            ("CHAR-ABX-X", 8.0, 8),   # 规则停用 → 不参与
            ("CHAR-NORM", 8.0, 8),    # 非抗菌药 → 不参与
        ])
        rx(b.id, datetime(2026, 5, 8, 9, 0), [("CHAR-ABX-1", 0.5, 4)])  # 2.0 DDDs
        rx(a.id, datetime(2026, 4, 1, 9, 0), [("CHAR-ABX-1", 9.0, 9)])  # 期外

        db.commit()
        return {"a": a.id, "b": b.id, "c": c.id}
    finally:
        db.close()


def _efficiency_row(world, org_id, **kw):
    base = {"org_id": org_id, "org_name": "", "beds": 0, "discharges": 0,
            "occupied_bed_days": 0, "avg_length_of_stay": 0.0, "bed_turnover": 0.0,
            "bed_occupancy_rate_pct": 0.0, "visits": 0, "doctors": 0,
            "visits_per_doctor_per_day": 0.0}
    base.update(kw)
    return base


def expected_efficiency(world):
    # 2026-05 共 31 天。甲：占用 4+22+2=28 床日；出院 1 人 15 天；诊疗 3 人次；医师 2。
    # 乙：占用 1；出院 1 人 1 天；诊疗 1；医师 1。丙无数据不出行。
    a = _efficiency_row(
        world, world["a"], org_name="特征化甲县医院", beds=3, discharges=1,
        occupied_bed_days=28, avg_length_of_stay=15.0, bed_turnover=0.33,
        bed_occupancy_rate_pct=round(28 * 100 / (3 * 31), 2), visits=3, doctors=2,
        visits_per_doctor_per_day=round(3 / 2 / 31, 2),
    )
    b = _efficiency_row(
        world, world["b"], org_name="特征化乙卫生院", beds=2, discharges=1,
        occupied_bed_days=1, avg_length_of_stay=1.0, bed_turnover=0.5,
        bed_occupancy_rate_pct=round(1 * 100 / (2 * 31), 2), visits=1, doctors=1,
        visits_per_doctor_per_day=round(1 / 1 / 31, 2),
    )
    return [a, b]  # 按 visits 降序


def test_efficiency_characterization(client, admin, world):
    resp = client.get(f"/api/analytics/efficiency?period={PERIOD}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == expected_efficiency(world)


def test_efficiency_org_filter_characterization(client, admin, world):
    resp = client.get(
        f"/api/analytics/efficiency?period={PERIOD}&org_id={world['b']}", headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == expected_efficiency(world)[1:]


def _drug_row(org_id, org_name, **kw):
    base = {"org_id": org_id, "org_name": org_name, "inpatient_total": 0.0,
            "inpatient_drug": 0.0, "inpatient_drug_ratio_pct": 0.0,
            "outpatient_total": 0.0, "outpatient_drug": 0.0,
            "outpatient_drug_ratio_pct": 0.0, "antibiotic_ddds": 0.0, "bed_days": 0,
            "antibiotic_intensity": 0.0, "ddd_uncovered_items": 0,
            "intensity_unstable": False}
    base.update(kw)
    return base


def expected_drug_use(world):
    # 甲：病案首页 1000/300；门诊 50.5/30.5；DDDs 6.0；床日 14（14天23小时截断）；
    #     强度 6.0*100/14=42.86；未覆盖 1；样本不足标记 True。
    # 乙：病案首页 500/100；门诊 10/10；DDDs 2.0；床日 1（当日入出院下限）；强度 200。
    # 丙：无数据全零行。
    a = _drug_row(
        world["a"], "特征化甲县医院", inpatient_total=1000.0, inpatient_drug=300.0,
        inpatient_drug_ratio_pct=30.0, outpatient_total=50.5, outpatient_drug=30.5,
        outpatient_drug_ratio_pct=round(30.5 / 50.5 * 100, 2), antibiotic_ddds=6.0,
        bed_days=14, antibiotic_intensity=round(6.0 * 100 / 14, 2),
        ddd_uncovered_items=1, intensity_unstable=True,
    )
    b = _drug_row(
        world["b"], "特征化乙卫生院", inpatient_total=500.0, inpatient_drug=100.0,
        inpatient_drug_ratio_pct=20.0, outpatient_total=10.0, outpatient_drug=10.0,
        outpatient_drug_ratio_pct=100.0, antibiotic_ddds=2.0, bed_days=1,
        antibiotic_intensity=200.0, intensity_unstable=True,
    )
    c = _drug_row(world["c"], "特征化丙空机构")
    return {
        "period": PERIOD,
        "caliber": {
            "drug_ratio": "住院取病案首页药品费/总费用；门诊按收费目录 category=drug 归集明细",
            "antibiotic_intensity": "Σ(日剂量×天数÷DDD)×100 ÷ 收治人天（出院者占用总床日）",
        },
        "warnings": [],
        "orgs": [a, b, c],
    }


def test_drug_use_characterization(client, admin, world):
    resp = client.get(f"/api/analytics/drug-use?period={PERIOD}", headers=admin)
    assert resp.status_code == 200, resp.text
    assert resp.json() == expected_drug_use(world)


def test_drug_use_org_filter_characterization(client, admin, world):
    resp = client.get(
        f"/api/analytics/drug-use?period={PERIOD}&org_id={world['b']}", headers=admin
    )
    assert resp.status_code == 200, resp.text
    expected = expected_drug_use(world)
    expected["orgs"] = [expected["orgs"][1]]
    assert resp.json() == expected


# ===========================================================================
# 性能形状断言：聚合必须发生在数据库内
# ===========================================================================
# 特征化用例锁"响应字节"，这里锁"查询形状"：两个端点对明细大表（admissions /
# bill_details / prescription_items）发出的每条 SQL 都必须带 GROUP BY——
# 把聚合改回"拉全行进内存逐行算"时，上面的特征化仍然绿（行为没变），
# 而这里必须变红（性能形状破坏）。两张网合起来才证明改写是等价且真聚合的。


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


def _assert_grouped(statements, table):
    touching = [s for s in statements if f"FROM {table}" in s or f"JOIN {table}" in s]
    assert touching, f"未捕获到访问 {table} 的查询（用例失效，请检查抓取方式）"
    ungrouped = [s for s in touching if "GROUP BY" not in s]
    assert not ungrouped, (
        f"以下访问 {table} 的查询没有 GROUP BY（明细被整表拉进内存聚合）：\n"
        + "\n---\n".join(ungrouped)
    )


def test_efficiency_aggregates_in_database(client, admin, world):
    with _capture_sql() as statements:
        assert client.get(
            f"/api/analytics/efficiency?period={PERIOD}", headers=admin
        ).status_code == 200
    _assert_grouped(statements, "admissions")
    _assert_grouped(statements, "employees")


def test_drug_use_aggregates_in_database(client, admin, world):
    with _capture_sql() as statements:
        assert client.get(
            f"/api/analytics/drug-use?period={PERIOD}", headers=admin
        ).status_code == 200
    _assert_grouped(statements, "admissions")
    _assert_grouped(statements, "bill_details")
    _assert_grouped(statements, "prescription_items")
