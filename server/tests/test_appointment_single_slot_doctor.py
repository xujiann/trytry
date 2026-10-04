"""管理端「发布号源」单条表单挂得上医师（P2-1299，第三十八批扫描 AB2-2）。

修前：预约页的单条发布表单（`core.js` 的 `#slot-form`）没有医师框，提交也不送 `employee_id`；同页的批量排班表单有
「医师ID（可选）」。`SlotCreate` 收 `employee_id`（注释「门诊号源挂医师档案」），寻医（`find_doctors`）与离职 / 调走的
拦截（`book_slot`）都只认 `employee_id`，两条部分唯一索引分挂医师 / 不挂医师——单条发布的门诊号一律不挂医师。
开发库实测（按页面实际发的请求体）：和批量排的王主任同一天同一时段两条都 201 并存（容量 5 + 5 = 10，放号量翻倍）；
第二天单条发布的号寻医数不到；王主任登记离职后，约批量排的号 409「该医师已离职」，约单条发布的号照样 201。

修法：单条表单照批量表单加「医师ID（可选）」，同一个写法、填了才送；后端不改（同时段已有挂医师的号、再发不挂医师的
要不要 409 另议）。静态用例钉页面；接口用例按页面现在发的请求体走一遍，钉住带上医师之后的三件事。
"""
from datetime import timedelta
from pathlib import Path

from conftest import business_today
from jssrc import strip_comments

CORE = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8"))

#: 批量排班一直这么送医师：填了才送，空框不送（后端 `employee_id: int | None = None`）
SEND_EMPLOYEE = '...(f.get("employee_id") ? { employee_id: Number(f.get("employee_id")) } : {})'


def _between(start_marker: str, end_marker: str) -> str:
    start = CORE.index(start_marker)
    return CORE[start:CORE.index(end_marker, start)]


def test_单条发布表单有医师框_提交照批量的写法带上医师():
    form = _between('<form class="inline" id="slot-form">', "</form>")
    assert '<input name="employee_id" type="number" placeholder="医师ID（可选）"' in form   # 修前单条表单没有这个框
    single = _between('$("#slot-form").onsubmit', '$("#slot-batch-form").onsubmit')
    assert SEND_EMPLOYEE in single                                                        # 修前提交体里没有 employee_id
    batch = _between('$("#slot-batch-form").onsubmit', '$("#doctor-form").onsubmit')
    assert SEND_EMPLOYEE in batch, "两张表单同一个写法：批量那句是照抄的源头"


def test_单条带医师发布_不与批量排的并存_寻医计入_离职后约不上(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21299 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    doctor = client.post("/api/mgmt/employees", headers=admin, json={
        "org_id": org, "name": "P21299王主任", "title": "主任医师"}).json()["id"]
    d1 = (business_today() + timedelta(days=1)).isoformat()
    d2 = (business_today() + timedelta(days=2)).isoformat()
    batch = client.post("/api/appointments/slots/batch", headers=admin, json={
        "org_id": org, "date_from": d1, "date_to": d1,
        "templates": [{"resource_type": "outpatient", "resource_name": "王主任专家门诊", "employee_id": doctor,
                       "slot_time": "09:00-10:00", "capacity": 5}]})
    assert batch.status_code == 201 and batch.json()["created"] == 1, batch.text

    # 页面单条发布现在发的请求体：原有六个键 + 填了的医师。行尾注的「原先」是页面不送医师时同一请求的结果（开发库实测）
    page_body = {"org_id": org, "resource_type": "outpatient", "resource_name": "王主任专家门诊", "slot_date": d1,
                 "slot_time": "09:00-10:00", "capacity": 5, "employee_id": doctor}
    same = client.post("/api/appointments/slots", headers=admin, json=page_body)
    assert same.status_code == 409 and "号源已存在" in same.json()["detail"], same.text   # 原先 201、两条并存
    listed = client.get("/api/appointments/slots", headers=admin, params={"org_id": org, "slot_date": d1}).json()
    assert sum(s["capacity"] for s in listed) == 5                                       # 原先 10

    single = client.post("/api/appointments/slots", headers=admin, json=dict(page_body, slot_date=d2))
    assert single.status_code == 201 and single.json()["employee_id"] == doctor, single.text   # 原先 None
    (row,) = client.get("/api/appointments/doctors", headers=admin, params={"keyword": "P21299王主任"}).json()
    assert row["available_slots"] == 2                                                   # 原先 1：单条发布的数不到
    assert single.json()["id"] in [s["slot_id"] for s in row["next_slots"]]

    left = client.post(f"/api/mgmt/employees/{doctor}/changes", headers=admin, json={
        "change_type": "leave", "effective_date": business_today().isoformat()})
    assert left.status_code == 201, left.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21299 患者", "id_card": "330782198701011299"}).json()["id"]
    booked = client.post("/api/appointments", headers=admin, json={"slot_id": single.json()["id"], "patient_id": patient})
    assert booked.status_code == 409 and "已离职" in booked.json()["detail"], booked.text   # 原先照约 201
