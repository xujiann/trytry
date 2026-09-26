"""按批号反查「仍在外面」的量对这一批全部发药求和，不是只加截断后的清单（P2-197）。

`batch_dispense_trace` 的清单截到最新 1000 行（P1-49 的行数上限），`total_dispensed` 却是在这 1000 行上加出来的：
一个 2 万片的批次每方 14 片、发出 1400 行，页面印「仍在外面（不含冲销）共 14000」而实际是 19600，没有任何截断
提示——召回时要追回的正是这个数。疫苗那一侧（`vaccine_supply` 受种者清单）早就按实际接种人次求。
"""
import pytest

from app.database import SessionLocal
from app.models import DispenseItem, DispenseRecord, DrugBatch, Organization, Patient, Prescription, User


@pytest.fixture(scope="module")
def batch_id(client):
    """直接落库造 1002 张已发药（其中 1 张冲销）：走接口要开方、审方、发药各一千多次。"""
    db = SessionLocal()
    try:
        admin_id = db.query(User.id).filter(User.username == "admin").scalar()
        org = Organization(name="P2197 县医院", org_type="lead_hospital", level="county")
        patient = Patient(ehc_no="EHC-P2197-01", name="P2197 患者", id_card="330106198808081547")
        db.add_all([org, patient])
        db.flush()
        batch = DrugBatch(org_id=org.id, drug_code="P2197-AMX", batch_no="AMX-2409", expire_date="2028-09-30",
                          quantity=20000)
        db.add(batch)
        db.flush()
        for n in range(1002):
            rx = Prescription(patient_id=patient.id, org_id=org.id, created_by=admin_id)
            db.add(rx)
            db.flush()
            record = DispenseRecord(prescription_id=rx.id, org_id=org.id, dispensed_by=admin_id,
                                    status="reversed" if n == 0 else "dispensed")
            db.add(record)
            db.flush()
            db.add(DispenseItem(dispense_id=record.id, batch_id=batch.id, drug_code="P2197-AMX",
                                drug_name="阿莫西林胶囊", quantity=14))
        db.commit()
        return batch.id
    finally:
        db.close()


def test_仍在外面的量按全部发药行求和_冲销的不算(client, admin, batch_id):
    trace = client.get(f"/api/pharmacy/batches/{batch_id}/dispenses", headers=admin)
    assert trace.status_code == 200, trace.text
    body = trace.json()
    assert len(body["dispenses"]) == 1000                      # 清单行数上限不变（P1-49）
    assert body["total_dispensed"] == 1001 * 14                # 修前只加最新 1000 行：14000
