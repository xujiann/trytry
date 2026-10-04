"""手术间名前带上所属医院：「手术已安排」站内信、居民端「我的手术」、管理端排班下拉（P2-1402，第四十一批「手术与麻醉闭环」
扫描 AE2-5 的显示一半）。

撮合的用途就是基层把手术病人排进县医院的空台（`resources.match_operating_rooms` 的说明；排班不比手术间的机构，
`test_secondary_body_id_guard.py` 登记为按设计跨机构）。可手术间名只在一家医院里唯一（`(org_id, name)` 唯一）：扫描实测乡
卫生院经办把本院病人排进县医院 2号手术间，居民端「医院」一栏是申请方乡卫生院、「手术间」只写「2号手术间」，站内信也只写
「2号手术间」——照着去的是乡卫生院；管理员 / 管理层的排班下拉列着全县的手术间，两家的「1号手术间」分不清。

修后三处都写「所属医院 · 手术间名」（`surgery.room_labels` / 页面 `roomLabel`），本院的同样带上：同一间手术间不管谁排的叫法一致，站内信原先连
医院都不写。跨机构排台的授权与流程（占别家手术间要不要对方确认、归属方能不能排 / 释放）是另一条待裁定，这里不动。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import SmsCode

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
DAY = "2031-07-01"
COUNTY, TOWN = "P21402 县人民医院", "P21402 乡卫生院"


def _resident(client, phone):
    """居民按手机号登录（登录即按手机号实名绑定档案）。"""
    with SessionLocal() as db:   # 同一号码的冷却期不挡本模块
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs, staff = {}, {}
    for key, name, org_type, level in (("county", COUNTY, "lead_hospital", "county"),
                                       ("town", TOWN, "township", "township")):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": name, "org_type": org_type, "level": level}).json()["id"]
        for role in ("doctor", "operator"):
            username = f"p21402_{key}_{role}"
            assert client.post("/api/users", headers=admin, json={
                "username": username, "password": "passw0rd1", "role": role, "org_id": orgs[key],
                "full_name": f"{name}{role}"}).status_code in (200, 201)
            token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()
            staff[f"{key}_{role}"] = {"Authorization": f"Bearer {token['access_token']}"}
    rooms = {key: client.post("/api/surgery/rooms", headers=admin, json={
        "org_id": orgs[key], "name": name}).json()["id"] for key, name in (("county", "2号手术间"), ("town", "1号手术间"))}
    return {"orgs": orgs, "staff": staff, "rooms": rooms, "seq": iter(range(1, 50))}


def _scheduled(client, admin, world, home, room, name, start, end):
    """在 `home` 那家住院、由那家医师提申请、管理员审批、那家经办排进 `room`；返回 (居民端头, 申请号)。"""
    n = next(world["seq"])
    ward = client.post("/api/inpatient/wards", headers=admin, json={
        "org_id": world["orgs"][home], "name": f"P21402 病区{n}"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P21402-{n}"}).json()["id"]
    phone = f"1390214{n:04d}"
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21402 患者{n}", "id_card": f"3301061975070{n:05d}", "phone": phone}).json()["id"]
    resident = _resident(client, phone)   # 先绑定居民账户，排班时的站内信才投得到
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "外伤"}).json()["id"]
    req = client.post("/api/surgery/requests", headers=world["staff"][f"{home}_doctor"], json={
        "admission_id": admission, "surgery_name": name})
    assert req.status_code == 201, req.text
    rid = req.json()["id"]
    assert client.post(f"/api/surgery/requests/{rid}/approve", headers=admin, json={"approved": True}).status_code == 200
    sched = client.post(f"/api/surgery/requests/{rid}/schedule", headers=world["staff"][f"{home}_operator"], json={
        "room_id": world["rooms"][room], "scheduled_date": DAY, "start_time": start, "end_time": end})
    assert sched.status_code == 201, sched.text
    return resident, rid


def _seen_by_resident(client, resident, rid, surgery_name):
    notices = [n["body"] for n in client.get("/api/portal/me/notifications", headers=resident).json()
               if n["title"] == f"手术已安排：{surgery_name}"]
    mine = next(s for s in client.get("/api/portal/me/surgeries", headers=resident).json() if s["id"] == rid)
    return notices, mine["org_name"], mine["room_name"]


def test_乡卫生院排进县医院的手术间_站内信与居民端带县医院名(client, admin, world):
    resident, rid = _scheduled(client, admin, world, "town", "county", "清创缝合术", "08:00", "12:00")
    notices, org_name, room_name = _seen_by_resident(client, resident, rid, "清创缝合术")
    # 修前：「2031-07-01 08:00-12:00，2号手术间。……」、居民端「医院 乡卫生院 / 手术间 2号手术间」——照着去的是乡卫生院
    assert notices == [f"{DAY} 08:00-12:00，{COUNTY} · 2号手术间。请遵医嘱做好术前准备。"]
    assert (org_name, room_name) == (TOWN, f"{COUNTY} · 2号手术间")


def test_本院手术间同样带医院名(client, admin, world):
    resident, rid = _scheduled(client, admin, world, "county", "county", "甲状腺切除术", "13:00", "15:00")
    notices, org_name, room_name = _seen_by_resident(client, resident, rid, "甲状腺切除术")
    assert notices == [f"{DAY} 13:00-15:00，{COUNTY} · 2号手术间。请遵医嘱做好术前准备。"]   # 修前站内信不写医院
    assert (org_name, room_name) == (COUNTY, f"{COUNTY} · 2号手术间")
    resident, rid = _scheduled(client, admin, world, "town", "town", "体表肿物切除术", "09:00", "10:00")
    assert _seen_by_resident(client, resident, rid, "体表肿物切除术") == (
        [f"{DAY} 09:00-10:00，{TOWN} · 1号手术间。请遵医嘱做好术前准备。"], TOWN, f"{TOWN} · 1号手术间")


def test_管理端排班下拉的手术间带所属医院():
    source = (STATIC / "pages-mgmt.js").read_text(encoding="utf-8")
    start = source.index("async function renderSurgery(")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert 'api("/api/organizations")' in body   # 机构名从机构清单取，后端出参不动
    assert "const roomLabel = (r) => (orgName[r.org_id] ? `${orgName[r.org_id]} · ${r.name}` : r.name);" in body
    modal = body[body.index('spdModal("手术排班"'):]
    modal = modal[:modal.index("]);")]
    # 修前 `label: r.name`：管理员 / 管理层的下拉列着全县的手术间，两家的「1号手术间」分不清。选项文字由 spdModal 过 esc()
    assert "options: rooms.map((r) => ({ value: r.id, label: roomLabel(r) })) }," in modal
    spd = (STATIC / "pages-spd.js").read_text(encoding="utf-8")
    assert "${esc(o.label)}</option>" in spd
