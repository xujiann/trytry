"""医师调往别院后，旧机构放出的号照样约得上（P2-497，第九批「终止类收尾」扫描 W1-7）。

放号时（单条与批量）早就判「医师不属于该机构」（422）；约号只拦离职（409「该医师已离职」）。登记调动只改医师的
机构，旧机构放出的号还挂着——管理端代约与居民自助预约都 201，约上的是一个医师已经不在那家坐诊的号，寻医清单还把
这些号算在医师**新**机构名下。

修法：约号（`book_slot`，两条入口共用）在离职那句旁边补上「医师已不属于放号机构」→ 409。已约上的怎么处置（取消 /
通知）是 W1-8 的口径，不在这里。
"""
import pytest

PHONE = "13900024970"
ID_CARD = "330281199002024970"


@pytest.fixture(scope="module")
def world(client, admin):
    def org(name):
        return client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": "township", "level": "township"}).json()["id"]

    old, new = org("P2497 原卫生院"), org("P2497 调入卫生院")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2497 居民", "id_card": ID_CARD, "gender": "女", "birth_date": "1990-02-02", "phone": PHONE}).json()["id"]
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    resident = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": "P2497 居民", "id_card": ID_CARD}, headers=resident)
    assert bound.status_code in (200, 409), bound.text
    return {"old": old, "new": new, "patient": patient, "resident": resident}


def _doctor_with_slot(client, admin, world, name, day):
    doctor = client.post("/api/mgmt/employees", headers=admin, json={
        "org_id": world["old"], "name": name, "position": "主治医师"}).json()["id"]
    slot = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": world["old"], "resource_type": "outpatient", "resource_name": "内科", "employee_id": doctor,
        "slot_date": day, "slot_time": "09:00-10:00", "capacity": 5})
    assert slot.status_code == 201, slot.text
    return doctor, slot.json()["id"]


def _transfer(client, admin, world, doctor):
    r = client.post(f"/api/mgmt/employees/{doctor}/changes", headers=admin,
                    json={"change_type": "transfer", "to_org_id": world["new"]})
    assert r.status_code == 201 and r.json()["employee_org_id"] == world["new"], r.text


def test_调走之后旧机构的号约不上_两条入口一个口径(client, admin, world):
    doctor, slot = _doctor_with_slot(client, admin, world, "P2497 调走的医师", "2031-02-03")
    _transfer(client, admin, world, doctor)
    staff = client.post("/api/appointments", headers=admin, json={"slot_id": slot, "patient_id": world["patient"]})
    assert staff.status_code == 409 and "已调离" in staff.json()["detail"], staff.text   # 修前 201
    self_book = client.post("/api/portal/me/appointments", headers=world["resident"], json={"slot_id": slot})
    assert self_book.status_code == 409 and "已调离" in self_book.json()["detail"], self_book.text   # 修前 201
    # 调走的医师在新机构照常放号、照常约得上
    fresh = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": world["new"], "resource_type": "outpatient", "resource_name": "内科", "employee_id": doctor,
        "slot_date": "2031-02-04", "slot_time": "09:00-10:00", "capacity": 5})
    assert fresh.status_code == 201, fresh.text
    booked = client.post("/api/appointments", headers=admin,
                         json={"slot_id": fresh.json()["id"], "patient_id": world["patient"]})
    assert booked.status_code == 201, booked.text


def test_没调走的照常约(client, admin, world):
    _, slot = _doctor_with_slot(client, admin, world, "P2497 在岗的医师", "2031-02-05")
    booked = client.post("/api/portal/me/appointments", headers=world["resident"], json={"slot_id": slot})
    assert booked.status_code == 201, booked.text
