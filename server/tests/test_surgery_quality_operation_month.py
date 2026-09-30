"""手术质量指标按做手术的那天归月（P2-1075，第三十一批「统计与报表按哪个日期归期」扫描 C1-3）。

诊断符合率、并发症发生率、非计划重返手术室率原先按术中记录的**录入时刻**筛期间；术后随访却从做手术那天起算
（`surgery._operation_day`：开始时刻 → 排班日 → 今天，P2-896）。8-31 夜里做的手术 9-30 补录：随访到期按 8-31 起算，
质量指标却记在 9 月——带 `period` 查 8 月是 (0, 0)，9 月是 (1, 1)。P2-201 当时写的是「同按做手术的月份归」，
用例拿录入时刻当手术时刻，没有照出这一层。修后三项同按 `surgery.operation_day` 归月，开始时刻与排班日都读不成的
按录入时刻（原口径）兜底。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import (Admission, Bed, OperatingRoom, Organization, Patient, SurgeryRecord, SurgeryRequest,
                        SurgerySchedule, User, Ward)


@pytest.fixture(scope="module")
def org_id(client):
    """直接落库：要把手术那天、排班日与录入时刻放到不同月份。"""
    with SessionLocal() as db:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P21075 县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-P21075-01", name="P21075 患者", id_card="330106195606061575")
        db.add_all([org, patient])
        db.flush()
        ward = Ward(org_id=org.id, name="P21075 外科")
        room = OperatingRoom(org_id=org.id, name="P21075 手术间")
        db.add_all([ward, room])
        db.flush()
        bed = Bed(ward_id=ward.id, bed_no="1")
        db.add(bed)
        db.flush()
        adm = Admission(patient_id=patient.id, org_id=org.id, ward_id=ward.id, bed_id=bed.id, status="admitted",
                        admitted_at=datetime(2026, 8, 20, 9, 0), created_by=admin_id)
        db.add(adm)
        db.flush()
        cases = (
            # 术式, 非计划重返, 排班日, 开始时刻, 录入时刻, 并发症
            ("再探查术", True, "2026-08-31", "2026-08-31 21:30", datetime(2026, 9, 30, 9, 0), "切口渗血"),   # 8 月做、9 月补录
            ("疝修补术", False, "2026-08-15", "", datetime(2026, 9, 2, 9, 0), ""),                            # 没填开始时刻：排班日 8 月
            ("阑尾切除术", False, None, "", datetime(2026, 9, 5, 9, 0), ""),                                  # 都没有：按录入时刻 9 月
        )
        for i, (name, unplanned, scheduled, start_at, entered, complications) in enumerate(cases):
            req = SurgeryRequest(admission_id=adm.id, patient_id=patient.id, org_id=org.id, surgery_name=name,
                                 unplanned_return=unplanned, status="completed", created_by=admin_id,
                                 created_at=datetime(2026, 8, 1, 9, 0))
            db.add(req)
            db.flush()
            if scheduled:
                db.add(SurgerySchedule(request_id=req.id, room_id=room.id, scheduled_date=scheduled,
                                       start_time=f"0{8 + i}:00", end_time=f"0{8 + i}:30", created_by=admin_id))
            db.add(SurgeryRecord(request_id=req.id, actual_surgery_name=name, start_at=start_at, complications=complications,
                                 preop_diagnosis="阑尾炎", postop_diagnosis="阑尾炎", created_by=admin_id,
                                 created_at=entered))
        db.commit()
        return org.id


def _indicators(client, admin, org_id, period):
    body = client.get(f"/api/quality/clinical-indicators?period={period}&org_id={org_id}", headers=admin).json()
    return {i["key"]: (i["numerator"], i["denominator"]) for i in body["indicators"]}


def test_按做手术那天归月_补录的不跑到录入的月份(client, admin, org_id):
    august = _indicators(client, admin, org_id, "2026-08")
    assert august["unplanned_return"] == (1, 2)         # 修前 (0, 0)：两台 8 月做的都记到了 9 月
    assert august["surgery_complication"] == (1, 2)
    assert august["preop_postop_match"] == (2, 2)
    september = _indicators(client, admin, org_id, "2026-09")
    assert september["unplanned_return"] == (0, 1)      # 修前 (1, 3)
    assert september["surgery_complication"] == (0, 1)


def test_全期照旧(client, admin, org_id):
    body = client.get(f"/api/quality/clinical-indicators?org_id={org_id}", headers=admin).json()
    rows = {i["key"]: (i["numerator"], i["denominator"]) for i in body["indicators"]}
    assert rows["unplanned_return"] == (1, 3) and rows["surgery_complication"] == (1, 3)


def test_做手术那天的取法():
    from app.routers.surgery import operation_day

    assert operation_day("2026-08-31 21:30", "2026-09-01") == "2026-08-31"   # 开始时刻优先
    assert operation_day("", "2026/8/15") == "2026-08-15"                    # 排班日按日历读存量写法
    assert operation_day("", None) is None and operation_day("", "待定") is None
