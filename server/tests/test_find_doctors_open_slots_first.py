"""便捷寻医先按「有没有余号」排、再取前 200 位（P2-178）。

寻医写「只列还有余号的医师排在前面，但没号的也一并返回并标注」；实现先按编号取前 200 位职工、再在这 200 位里把
有号的排前——全县职工过 200 位（不带关键字、不限机构时必然），编号靠后的医师有号也不在清单里，编号靠前、没号的
倒占着位置。
"""
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app import clock
    from app.database import SessionLocal
    from app.models import AppointmentSlot, Employee

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2178 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    with SessionLocal() as db:
        db.add_all([Employee(org_id=org, name=f"P2178 职工{i}", title="护师") for i in range(200)])   # 先建的 200 位，没挂号源
        doctor = Employee(org_id=org, name="P2178 王主任", title="主任医师")
        db.add(doctor)
        db.flush()
        db.add(AppointmentSlot(org_id=org, employee_id=doctor.id, resource_type="outpatient", resource_name="心内科",
                               slot_date=(clock.today() + timedelta(days=1)).isoformat(), slot_time="09:00", capacity=3))
        db.commit()
        return {"org": org, "doctor": doctor.id}


def test_有号的医师编号靠后也排在清单最前(client, admin, seeded):
    rows = client.get(f"/api/appointments/doctors?org_id={seeded['org']}", headers=admin).json()
    assert len(rows) == 200
    assert (rows[0]["employee_id"], rows[0]["available_slots"], rows[0]["bookable"]) == (seeded["doctor"], 1, True)   # 修前不在清单里
    assert all(not r["bookable"] for r in rows[1:])   # 没号的照旧一并返回并标注
