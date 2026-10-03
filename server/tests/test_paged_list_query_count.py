"""分页清单的 SQL 条数不随页大小涨：逐行再查关联改成按页一次 IN（P2-1157，第三十三批扫描 A4-12）。

修前七个分页清单逐行再查关联（N+1）：处方清单的明细是懒加载、慢专病路径实例每行三次 `db.get`（模板 / 纳管档案 /
患者）、高值耗材每行查患者与手术、专病入组每行两次路径记录加一次专病目录、慢专病转诊与县外就诊每行查患者、发药记录
每行查明细；押金不足预警的余额与未结费用已在库里算好，每行还再查余额、未结、患者三次。扫描实测一页 100～200 行变成
100～600 条 SQL（处方 10 → 200 行：13 → 203 条）。

照 `test_spd_perf.py` 的写法数 SQL 条数：同一接口页大小 2 与 12 两种，条数必须一样（修前每多一行多 1～3 条）。
各清单的行与原实现逐行出参（单条出参那条路没动，仍逐行 `db.get`）逐字段一致。
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
    Deposit,
    DiseaseEnrollment,
    DiseasePathRecord,
    DiseaseProgram,
    DispenseItem,
    DispenseRecord,
    DrugBatch,
    HighValueConsumable,
    Organization,
    OutboundVisit,
    Patient,
    Prescription,
    PrescriptionItem,
    SurgeryRequest,
    User,
    Ward,
)
from app.routers import analytics, billing, disease_programs, dispense, materials
from app.schemas import PrescriptionOut
from app.spd.models import SpdEnrollment, SpdPathInstance, SpdPathTemplate, SpdProgram, SpdReferralCase
from app.spd.routers import referral as spd_referral
from app.spd.routers import tasks as spd_tasks

N = 12
SMALL, BIG = 2, N
LISTS = [
    "/api/prescriptions",
    "/api/spd/path-instances",
    "/api/materials/consumables",
    "/api/disease-programs/enrollments",
    "/api/spd/referrals",
    "/api/analytics/outbound-visits",
    "/api/dispense",
    "/api/billing/deposits/alerts",
]


@contextmanager
def count_sql():
    counter = {"n": 0}

    def _tick(conn, cursor, statement, parameters, context, executemany):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", _tick)
    try:
        yield counter
    finally:
        event.remove(engine, "before_cursor_execute", _tick)


@pytest.fixture(scope="module")
def world(client, admin):
    """每个清单各 N 行，关联的患者 / 模板 / 手术 / 批号各不相同或轮换着用，逐行查询躲不进身份映射。"""
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P21157 卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        patients = [Patient(name=f"P21157 患者{i}", id_card=f"33012719900102{i:04d}", ehc_no=f"EHC-P21157-{i}")
                    for i in range(N)]
        db.add_all(patients)
        db.flush()
        ward = Ward(org_id=org.id, name="P21157 内科")
        db.add(ward)
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=str(i + 1)) for i in range(N)]
        db.add_all(beds)
        db.flush()
        admissions = [Admission(patient_id=p.id, org_id=org.id, ward_id=ward.id, bed_id=b.id, status="admitted",
                                created_by=creator) for p, b in zip(patients, beds)]
        db.add_all(admissions)
        db.flush()
        surgeries = [SurgeryRequest(admission_id=a.id, patient_id=a.patient_id, org_id=org.id,
                                    surgery_name=f"P21157 手术{i}", created_by=creator)
                     for i, a in enumerate(admissions[:3])]
        db.add_all(surgeries)
        batches = [DrugBatch(org_id=org.id, drug_code="P21157D", batch_no=f"B{i}", expire_date="2027-12-31")
                   for i in range(2)]
        db.add_all(batches)
        spd_program = db.query(SpdProgram).order_by(SpdProgram.id).first()
        templates = [SpdPathTemplate(program_id=spd_program.id, code=f"P21157T{i}", name=f"P21157 路径{i}",
                                     scene=("outpatient", "home", "followup")[i], status="published")
                     for i in range(3)]
        db.add_all(templates)
        program = DiseaseProgram(code="P21157DP", name="P21157 专病", org_id=org.id, path_nodes=[
            {"key": "eval", "name": "首次评估", "required": True},
            {"key": "rev", "name": "复查", "required": True},
            {"key": "edu", "name": "宣教", "required": False},
        ])
        db.add(program)
        db.flush()
        for i, (p, a) in enumerate(zip(patients, admissions)):
            at = datetime(2026, 9, 1, 8, i)   # 出参里印的时刻都钉死：同一份数据修前修后两次跑出来逐字节可比
            rx = Prescription(patient_id=p.id, org_id=org.id, diagnosis_name="高血压", status="auto_passed",
                              created_by=creator)
            db.add(rx)
            db.flush()
            db.add_all([PrescriptionItem(prescription_id=rx.id, drug_code=f"D{i}{k}", drug_name=f"药{k}",
                                         daily_dose=1.0 + k, days=3) for k in range(2)])
            record = DispenseRecord(prescription_id=rx.id, org_id=org.id, dispensed_by=creator, status="dispensed",
                                    created_at=at)
            db.add(record)
            db.flush()
            db.add_all([DispenseItem(dispense_id=record.id, batch_id=batches[k].id, drug_code="P21157D",
                                     drug_name="发药", quantity=k + 1) for k in range(2)])
            enrollment = SpdEnrollment(patient_id=p.id, program_code=spd_program.code, org_id=org.id)
            db.add(enrollment)
            db.flush()
            db.add(SpdPathInstance(enrollment_id=enrollment.id, template_id=templates[i % 3].id,
                                   template_code=templates[i % 3].code, current_node_key="n1", current_stage="s1",
                                   started_at=at))
            db.add(HighValueConsumable(barcode=f"P21157-{i}", name="支架", org_id=org.id, status="used",
                                       used_patient_id=p.id, used_surgery_id=surgeries[i % 3].id if i % 4 else None,
                                       used_at=at))
            disease = DiseaseEnrollment(program_id=program.id, patient_id=p.id, org_id=org.id, enrolled_at="2026-09-01")
            db.add(disease)
            db.flush()
            db.add_all([DiseasePathRecord(enrollment_id=disease.id, node_key=key, performed_at="2026-09-02",
                                          operator_name="医生", result="好")
                        for key in ("eval", "edu", "rev")[: i % 4]])
            db.add(SpdReferralCase(patient_id=p.id, program_code=spd_program.code, initiator_org_id=org.id,
                                   current_org_id=org.id, status=("submitted", "accepted", "closed")[i % 3],
                                   created_at=at))
            db.add(OutboundVisit(patient_id=p.id, visit_date="2026-09-01", external_org_name="省医院",
                                 created_by=creator))
            db.add(Deposit(admission_id=a.id, amount=50 + i, deposit_type="prepay"))
            db.add(BillDetail(patient_id=p.id, admission_id=a.id, item_code="P21157B", item_name="床位费",
                              unit_price=100, amount=100 + i * 3, created_by=creator))
        db.commit()


@pytest.mark.parametrize("path", LISTS)
def test_分页清单的SQL条数与页大小无关(client, admin, world, path):
    counts = {}
    for limit in (SMALL, BIG):
        client.get(path, headers=admin, params={"limit": limit})   # 预热（首次会建缓存等）
        with count_sql() as sql:
            resp = client.get(path, headers=admin, params={"limit": limit})
        assert resp.status_code == 200, resp.text
        assert len(resp.json()) == limit, path   # 数据确实够两种页大小
        counts[limit] = sql["n"]
    assert counts[BIG] == counts[SMALL], (
        f"{path}：页大小 {SMALL} → {BIG}，SQL 从 {counts[SMALL]} 条涨到 {counts[BIG]} 条——又逐行查关联了"
    )


def _rows(client, admin, path):
    resp = client.get(path, headers=admin, params={"limit": N})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _as_response(model, value) -> dict:
    """照 FastAPI 出参那一步（按属性读、嵌套对象同样按属性读）过一遍响应模型，再落成 JSON 形状。"""
    return model.model_validate(value, from_attributes=True).model_dump(mode="json")


def test_清单行与原实现逐行出参一致(client, admin, world):
    """单条出参那条路（逐行 `db.get` / 逐行查明细）没动，拿它当原实现逐行对照；键序一并比。"""
    with SessionLocal() as db:
        admin_user = db.query(User).filter(User.username == "admin").one()
        expected = {
            "/api/prescriptions": (PrescriptionOut, lambda i: db.get(Prescription, i)),
            "/api/spd/path-instances": (spd_tasks.PathInstanceOut,
                                        lambda i: spd_tasks._instance_out(db, db.get(SpdPathInstance, i))),
            "/api/materials/consumables": (materials.ConsumableOut,
                                           lambda i: materials._consumable_out(db, db.get(HighValueConsumable, i))),
            "/api/disease-programs/enrollments": (
                disease_programs.DiseaseEnrollmentOut,
                lambda i: disease_programs._enrollment_out(db.get(DiseaseEnrollment, i), db)),
            "/api/spd/referrals": (spd_referral.ReferralCaseRowOut, lambda i: {
                **spd_referral._case_out(db, db.get(SpdReferralCase, i)),
                "actions": spd_referral._case_actions(db, admin_user, db.get(SpdReferralCase, i))}),
            "/api/analytics/outbound-visits": (analytics.OutboundVisitOut,
                                               lambda i: analytics._outbound_out(db, db.get(OutboundVisit, i))),
            "/api/dispense": (dispense.DispenseOut, lambda i: dispense._dispense_out(db, db.get(DispenseRecord, i))),
        }
        for path, (model, build) in expected.items():
            rows = _rows(client, admin, path)
            want = [_as_response(model, build(row["id"])) for row in rows]
            assert [list(row) for row in rows] == [list(row) for row in want], path
            assert rows == want, path
        # 押金预警没有单条出参：照原实现的循环体逐行算（余额、未结各查一次，患者 `db.get`）
        alerts = _rows(client, admin, "/api/billing/deposits/alerts")
        want = []
        for row in alerts:
            admission = db.get(Admission, row["admission_id"])
            balance = billing.deposit_balance(db, admission.id)
            unsettled = billing.unsettled_amount(db, admission.id)
            patient = db.get(Patient, admission.patient_id)
            want.append(_as_response(billing.DepositAlertOut, {
                "admission_id": admission.id, "patient_id": admission.patient_id,
                "patient_name": patient.name if patient else "", "org_id": admission.org_id,
                "balance": balance, "unsettled": unsettled, "gap": round(balance - unsettled, 2),
            }))
        assert alerts == want
        assert [a["gap"] for a in alerts] == sorted(a["gap"] for a in alerts)   # 最缺钱的排最前（原口径）
