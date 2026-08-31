"""阶段十 统一资源与排程撮合中心 + DRG 事前提示与事中预警。"""
from datetime import date, timedelta

import pytest


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "阶段十县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


# ================================================================ 通用资源


def test_新登记资源一律是草稿(client, admin, org):
    """直接发布意味着还没核对完就能被申请到，填错的代价是有人白跑一趟。"""
    resp = client.post(
        "/api/resources",
        json={"org_id": org["id"], "resource_type": "logistics", "code": "LG001",
              "name": "标本转运工勤", "capacity": 3, "unit": "人次"},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "draft"

    catalog = client.get(
        "/api/resources/catalog", params={"resource_kind": "general"}, headers=admin
    ).json()
    assert catalog["items"][0]["usable"] is False  # 草稿不可用


def test_发布与撤回都留痕且可逆(client, admin, org):
    resource = client.get("/api/resources", headers=admin).json()[0]
    published = client.post(f"/api/resources/{resource['id']}/publish", headers=admin)
    assert published.json()["status"] == "published"
    assert client.post(f"/api/resources/{resource['id']}/publish",
                       headers=admin).status_code == 409

    withdrawn = client.post(
        f"/api/resources/{resource['id']}/withdraw",
        json={"reason": "设备检修"}, headers=admin,
    )
    assert withdrawn.json()["status"] == "withdrawn"
    assert withdrawn.json()["withdraw_reason"] == "设备检修"
    # 撤回可逆：重新发布后理由清空
    again = client.post(f"/api/resources/{resource['id']}/publish", headers=admin)
    assert again.json()["status"] == "published" and again.json()["withdraw_reason"] == ""


def test_同机构资源编码唯一(client, admin, org):
    body = {"org_id": org["id"], "resource_type": "equipment", "code": "EQ001",
            "name": "移动DR"}
    assert client.post("/api/resources", json=body, headers=admin).status_code == 201
    assert client.post("/api/resources", json=body, headers=admin).status_code == 409


def test_统一资源视图聚合五类而不替代(client, admin, org):
    """与统一申请单中心同一个做法：各类资源保留自己的领域表。"""
    client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient",
              "resource_name": "内科门诊", "slot_date": "2099-10-01",
              "slot_time": "09:00", "capacity": 5},
        headers=admin,
    )
    client.post("/api/surgery/rooms", json={"org_id": org["id"], "name": "阶段十手术间"},
                headers=admin)
    catalog = client.get("/api/resources/catalog", headers=admin).json()
    kinds = set(catalog["by_kind"])
    assert {"slot", "or_room", "general"} <= kinds
    slot = [i for i in catalog["items"] if i["kind"] == "slot"][0]
    # available 按各类自己的规则算，号源看余量
    assert slot["available"] == 5 and slot["unit"] == "个"
    room = [i for i in catalog["items"] if i["kind"] == "or_room"][0]
    # 手术间没有"余量"这个概念，如实返回 None 而不是硬凑一个数
    assert room["available"] is None and room["usable"] is True


# ================================================================ 排程撮合


def test_号源撮合按最早可用排序且不自动选(client, admin, org):
    """最早与最近常不是同一家，选哪个是患者的事。"""
    far = client.post(
        "/api/organizations",
        json={"name": "阶段十远处卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    today = date.today()
    client.post(
        "/api/appointments/slots",
        json={"org_id": far["id"], "resource_type": "outpatient", "resource_name": "全科门诊",
              "slot_date": (today + timedelta(days=1)).isoformat(), "capacity": 2},
        headers=admin,
    )
    client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient", "resource_name": "全科门诊",
              "slot_date": (today + timedelta(days=5)).isoformat(), "capacity": 9},
        headers=admin,
    )
    matched = client.get(
        "/api/resources/match/slots", params={"keyword": "全科"}, headers=admin
    ).json()
    assert [c["org_id"] for c in matched["candidates"]] == [far["id"], org["id"]]
    # 余量更多的那家排在后面——排序键是最早日期，不是余量
    assert matched["candidates"][0]["remaining_total"] == 2
    assert "不自动选" in matched["caliber"]


def test_约满的号源不进撮合(client, admin, org):
    patient = client.post(
        "/api/patients", json={"name": "撮合患者", "id_card": "320000199501011234"},
        headers=admin,
    ).json()
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "lab", "resource_name": "抽血窗口",
              "slot_date": (date.today() + timedelta(days=2)).isoformat(), "capacity": 1},
        headers=admin,
    ).json()
    before = client.get(
        "/api/resources/match/slots", params={"resource_type": "lab"}, headers=admin
    ).json()
    assert before["candidates"]
    client.post("/api/appointments",
                json={"slot_id": slot["id"], "patient_id": patient["id"]}, headers=admin)
    after = client.get(
        "/api/resources/match/slots", params={"resource_type": "lab"}, headers=admin
    ).json()
    assert after["candidates"] == []


def test_手术间撮合给出空档且与排班同一套冲突规则(client, admin, org):
    """两处判定不一致，撮合说能排、排班说冲突，比没有撮合更糟。"""
    room = client.get("/api/surgery/rooms", params={"org_id": org["id"]},
                      headers=admin).json()[0]
    # 造一台已排的手术占住 10:00-12:00
    ward = client.post("/api/inpatient/wards",
                       json={"org_id": org["id"], "name": "阶段十外科"}, headers=admin).json()
    bed = client.post("/api/inpatient/beds",
                      json={"ward_id": ward["id"], "bed_no": "S10-1"}, headers=admin).json()
    patient = client.post(
        "/api/patients", json={"name": "手术患者", "id_card": "320000199601011234"},
        headers=admin,
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "diagnosis_name": "胆囊结石"},
        headers=admin,
    ).json()
    client.post("/api/users",
                json={"username": "s10_doc", "password": "passw0rd1", "role": "doctor",
                      "org_id": org["id"]}, headers=admin)
    doc = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "s10_doc", "password": "passw0rd1"}
    ).json()["access_token"]}
    req = client.post("/api/surgery/requests",
                      json={"admission_id": adm["id"], "surgery_name": "胆囊切除术"},
                      headers=doc).json()
    client.post(f"/api/surgery/requests/{req['id']}/approve",
                json={"approved": True}, headers=admin)
    client.post(f"/api/surgery/requests/{req['id']}/schedule",
                json={"room_id": room["id"], "scheduled_date": "2099-10-08",
                      "start_time": "10:00", "end_time": "12:00"}, headers=admin)

    # 请求 09:00-13:00 的窗口：该手术间冲突，空档为 09:00-10:00 与 12:00-13:00
    matched = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": org["id"], "scheduled_date": "2099-10-08",
                "start_time": "09:00", "end_time": "13:00"},
        headers=admin,
    ).json()
    entry = [r for r in matched["rooms"] if r["room_id"] == room["id"]][0]
    assert entry["available"] is False
    assert entry["conflicts"] == [{"start_time": "10:00", "end_time": "12:00"}]
    assert entry["gaps"] == [{"start_time": "09:00", "end_time": "10:00"},
                             {"start_time": "12:00", "end_time": "13:00"}]

    # 不相交的窗口不算冲突（14:00-16:00）
    later = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": org["id"], "scheduled_date": "2099-10-08",
                "start_time": "14:00", "end_time": "16:00"},
        headers=admin,
    ).json()
    assert [r for r in later["rooms"] if r["room_id"] == room["id"]][0]["available"] is True


def test_撮合时间窗颠倒被拦下(client, admin, org):
    resp = client.get(
        "/api/resources/match/or-rooms",
        params={"org_id": org["id"], "start_time": "15:00", "end_time": "09:00"},
        headers=admin,
    )
    assert resp.status_code == 422


# ================================================================ DRG


def test_事前提示给候选组而非结论(client, admin):
    """入院时诊断未定，报一个确定的组会让人照着组去写诊断。"""
    resp = client.post(
        "/api/drgs/pre-check", json={"diagnosis": "急性阑尾炎", "operation": "阑尾切除术"},
        headers=admin,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["matched"] is True
    assert len(body["candidates"]) >= 1
    assert body["weight_range"]["min"] <= body["weight_range"]["max"]
    assert "候选组而非结论" in body["caliber"]


def test_事前未命中不落兜底组(client, admin):
    """兜底组是出院入组时保证每个病例都有归属用的，事前拿它当预测毫无信息量。"""
    resp = client.post(
        "/api/drgs/pre-check", json={"diagnosis": "十分罕见的不存在疾病XYZ"}, headers=admin
    ).json()
    assert resp["matched"] is False
    assert resp["candidates"] == []
    assert resp["weight_range"] is None


def test_事中预警样本不足时不预警但单列(client, admin, org):
    """3 个病例算出来的"均值"，预警的是噪声不是问题；
    但不提的话，看的人会以为这些病例没问题。"""
    alerts = client.get("/api/drgs/in-stay-alerts", params={"org_id": org["id"]},
                        headers=admin).json()
    # 上面造的那台手术病例还没填病案首页 → 计入 ungrouped 而不是静默丢弃
    assert alerts["ungrouped_in_stay"] >= 1
    assert alerts["alerts"] == []
    assert "insufficient_baseline" in alerts
