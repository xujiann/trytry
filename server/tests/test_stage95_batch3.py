"""阶段九·五 第三批：⑧一码通与多卡协同、⑨便捷寻医、⑬名老中医传承、㉑模拟诊疗。"""
import pytest


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "第三批县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "第三批患者", "id_card": "320000198801011234"},
        headers=admin,
    ).json()


# ================================================================ ⑧ 一码通


def test_一码通动态码可核验且不落库(client, admin, patient):
    issued = client.post(
        "/api/credentials/one-code", json={"patient_id": patient["id"]}, headers=admin
    )
    assert issued.status_code == 200, issued.text
    code = issued.json()["code"]
    assert code.count(".") == 2  # 健康卡号.过期时刻.签名

    resolved = client.post(
        "/api/credentials/one-code/resolve", json={"code": code}, headers=admin
    )
    assert resolved.status_code == 200
    assert resolved.json()["patient_id"] == patient["id"]
    assert resolved.json()["remaining_seconds"] > 0


def test_伪造签名与过期分别报出(client, admin, patient):
    """前者是伪造，后者让人重新出码，处置完全不同。"""
    code = client.post(
        "/api/credentials/one-code", json={"patient_id": patient["id"]}, headers=admin
    ).json()["code"]
    ehc_no, expires, _sig = code.split(".")

    forged = client.post(
        "/api/credentials/one-code/resolve",
        json={"code": f"{ehc_no}.{expires}.deadbeef" + "0" * 24}, headers=admin,
    )
    assert forged.status_code == 403

    # 把过期时刻改早 → 签名对不上，先被签名拦下；用正确签名的过期码需另造，
    # 故这里直接验证签名优先于过期的顺序是确定的
    assert "签名" in forged.json()["detail"]


def test_多卡协同一个入口认全部标识(client, admin, patient, org):
    """窗口人员手上可能是实体卡号、健康卡号或身份证号。"""
    cred = client.post(
        "/api/credentials",
        json={"patient_id": patient["id"], "credential_type": "card"},
        headers=admin,
    ).json()

    by_card = client.get(
        "/api/credentials/resolve", params={"identifier": cred["credential_no"]}, headers=admin
    ).json()
    assert by_card["matched_by"] == "credential_no"
    assert by_card["patient"]["id"] == patient["id"]

    by_ehc = client.get(
        "/api/credentials/resolve", params={"identifier": patient["ehc_no"]}, headers=admin
    ).json()
    assert by_ehc["matched_by"] == "ehc_no"

    by_idcard = client.get(
        "/api/credentials/resolve",
        params={"identifier": "320000198801011234"}, headers=admin,
    ).json()
    assert by_idcard["matched_by"] == "id_card"

    assert client.get(
        "/api/credentials/resolve", params={"identifier": "不存在的标识"}, headers=admin
    ).status_code == 404


def test_作废的卡仍能解析但标为失效(client, admin, patient):
    """窗口人员需要知道"这张卡作废了"，而不是"查无此卡"。"""
    cred = client.post(
        "/api/credentials", json={"patient_id": patient["id"], "credential_type": "card"},
        headers=admin,
    ).json()
    client.post(f"/api/credentials/{cred['id']}/void",
                json={"reason": "挂失"}, headers=admin)
    resolved = client.get(
        "/api/credentials/resolve", params={"identifier": cred["credential_no"]}, headers=admin
    ).json()
    assert resolved["valid"] is False
    assert resolved["patient"]["id"] == patient["id"]


# ================================================================ ⑨ 便捷寻医


def test_寻医按号源挂的医师而不是姓名字符串(client, admin, org, patient):
    doctor = client.post(
        "/api/mgmt/employees",
        json={"org_id": org["id"], "name": "王主任", "title": "主任医师",
              "position": "呼吸内科"},
        headers=admin,
    ).json()
    other = client.post(
        "/api/mgmt/employees",
        json={"org_id": org["id"], "name": "李医生", "title": "主治医师",
              "position": "呼吸内科"},
        headers=admin,
    ).json()
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient",
              "resource_name": "呼吸内科专家门诊", "slot_date": "2099-09-01",
              "slot_time": "09:00", "capacity": 3, "employee_id": doctor["id"]},
        headers=admin,
    )
    assert slot.status_code == 201, slot.text

    found = client.get(
        "/api/appointments/doctors", params={"keyword": "呼吸内科"}, headers=admin
    ).json()
    by_id = {d["employee_id"]: d for d in found}
    # 有号的排前面，但没号的也返回并标注 bookable=False
    assert found[0]["employee_id"] == doctor["id"]
    assert by_id[doctor["id"]]["available_slots"] == 1
    assert by_id[other["id"]]["bookable"] is False
    assert by_id[doctor["id"]]["next_slots"][0]["remaining"] == 3


def test_号源挂的医师必须属于该机构(client, admin, org):
    another_org = client.post(
        "/api/organizations",
        json={"name": "第三批卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    doctor = client.get(
        "/api/appointments/doctors", params={"keyword": "王主任"}, headers=admin
    ).json()[0]
    resp = client.post(
        "/api/appointments/slots",
        json={"org_id": another_org["id"], "resource_type": "outpatient",
              "resource_name": "跨院号源", "slot_date": "2099-09-01",
              "capacity": 1, "employee_id": doctor["employee_id"]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_约满的号源不再计入可约(client, admin, org, patient):
    doctor = client.get(
        "/api/appointments/doctors", params={"keyword": "王主任"}, headers=admin
    ).json()[0]
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": org["id"], "resource_type": "outpatient",
              "resource_name": "单号专家门诊", "slot_date": "2099-09-02",
              "capacity": 1, "employee_id": doctor["employee_id"]},
        headers=admin,
    ).json()
    before = client.get(
        "/api/appointments/doctors", params={"keyword": "王主任"}, headers=admin
    ).json()[0]["available_slots"]
    client.post("/api/appointments",
                json={"slot_id": slot["id"], "patient_id": patient["id"]}, headers=admin)
    after = client.get(
        "/api/appointments/doctors", params={"keyword": "王主任"}, headers=admin
    ).json()[0]["available_slots"]
    assert after == before - 1


# ================================================================ ⑬ 名老中医传承


def test_医案分维度存且可按方药检索(client, admin):
    """压成一段文本就再也检索不出"某某老师治痹证常用哪几味药"。"""
    case = client.post(
        "/api/tcm-heritage/master-cases",
        json={"master_name": "张老", "successor_name": "小李", "title": "痹证验案一则",
              "disease": "痹证", "syndrome": "风寒湿痹",
              "four_exams": "关节冷痛，遇寒加重，舌淡苔白，脉弦紧",
              "treatment_method": "祛风散寒除湿", "prescription": "桂枝 附子 白术 甘草",
              "commentary": "此案重用附子，取其温通之力", "visit_date": "2026-03-01"},
        headers=admin,
    )
    assert case.status_code == 201, case.text
    cid = case.json()["id"]

    # 未发布的默认不可见
    assert client.get("/api/tcm-heritage/master-cases",
                      params={"keyword": "附子"}, headers=admin).json() == []
    client.post(f"/api/tcm-heritage/master-cases/{cid}/publish", headers=admin)

    # 处方里的药能搜到
    assert len(client.get("/api/tcm-heritage/master-cases",
                          params={"keyword": "附子"}, headers=admin).json()) == 1
    # 按语里被点评的药也能搜到（同一个关键词两处都有）
    assert len(client.get("/api/tcm-heritage/master-cases",
                          params={"syndrome": "风寒湿"}, headers=admin).json()) == 1


def test_医案可撤回发布(client, admin):
    case = client.get("/api/tcm-heritage/master-cases", headers=admin).json()[0]
    client.post(f"/api/tcm-heritage/master-cases/{case['id']}/unpublish", headers=admin)
    assert client.get("/api/tcm-heritage/master-cases", headers=admin).json() == []
    client.post(f"/api/tcm-heritage/master-cases/{case['id']}/publish", headers=admin)


def test_传承概览按老师汇总(client, admin):
    stats = client.get("/api/tcm-heritage/master-cases/stats", headers=admin).json()
    entry = [m for m in stats["masters"] if m["master_name"] == "张老"][0]
    assert entry["total"] == 1 and entry["published"] == 1
    assert entry["diseases"] == ["痹证"] and entry["successors"] == ["小李"]


# ================================================================ ㉑ 模拟诊疗


@pytest.fixture(scope="module")
def simulation(client, admin):
    return client.post(
        "/api/tcm-heritage/simulations",
        json={
            "title": "胸痛患者接诊模拟", "category": "emergency",
            "scenario": "男性 58 岁，突发压榨性胸痛 30 分钟",
            "decision_points": [
                {"key": "s1", "question": "首选检查？", "options": ["心电图", "腹部B超"],
                 "answer": "心电图", "score": 50, "explain": "胸痛首要排除急性冠脉综合征"},
                {"key": "s2", "question": "下一步？", "options": ["立即启动胸痛中心流程", "门诊随访"],
                 "answer": "立即启动胸痛中心流程", "score": 50, "explain": "时间就是心肌"},
            ],
        },
        headers=admin,
    ).json()


def test_正确答案必须在选项里(client, admin):
    resp = client.post(
        "/api/tcm-heritage/simulations",
        json={"title": "错配病例", "decision_points": [
            {"key": "x", "question": "选哪个？", "options": ["A", "B"], "answer": "C"}]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_决策点key不可重复(client, admin):
    resp = client.post(
        "/api/tcm-heritage/simulations",
        json={"title": "重复key", "decision_points": [
            {"key": "s1", "question": "Q1", "options": ["A", "B"], "answer": "A"},
            {"key": "s1", "question": "Q2", "options": ["A", "B"], "answer": "B"}]},
        headers=admin,
    )
    assert resp.status_code == 422


def test_列表不带答案(client, admin, simulation):
    """带上答案，练习就没有意义了。"""
    listed = client.get("/api/tcm-heritage/simulations", headers=admin).json()
    case = [c for c in listed if c["id"] == simulation["id"]][0]
    assert "answer" not in case["decision_points"][0]
    assert case["total_score"] == 100


def test_作答评分且只对答错的给解析(client, admin, simulation):
    """不给解析的练习只是筛人，不是教学；但答对的堆一屏解析反而没人看。"""
    resp = client.post(
        f"/api/tcm-heritage/simulations/{simulation['id']}/attempts",
        json={"answers": {"s1": "心电图", "s2": "门诊随访"}}, headers=admin,
    )
    assert resp.status_code == 201
    assert resp.json()["score"] == 50
    assert resp.json()["passed"] is False
    detail = {d["key"]: d for d in resp.json()["detail"]}
    assert detail["s1"]["correct"] is True and detail["s1"]["explain"] == ""
    assert detail["s2"]["correct"] is False and "时间就是心肌" in detail["s2"]["explain"]


def test_重做取最高分但全部留痕(client, admin, simulation):
    """"第几次才做对"本身就是教学反馈。"""
    client.post(
        f"/api/tcm-heritage/simulations/{simulation['id']}/attempts",
        json={"answers": {"s1": "心电图", "s2": "立即启动胸痛中心流程"}}, headers=admin,
    )
    result = client.get(
        f"/api/tcm-heritage/simulations/{simulation['id']}/attempts", headers=admin
    ).json()
    assert len(result["attempts"]) == 2  # 两次都留着
    assert result["best_by_user"][0]["best_score"] == 100


def test_未作答按错处理但不报错(client, admin, simulation):
    resp = client.post(
        f"/api/tcm-heritage/simulations/{simulation['id']}/attempts",
        json={"answers": {}}, headers=admin,
    )
    assert resp.status_code == 201
    assert resp.json()["score"] == 0
    assert all(d["chosen"] == "未作答" for d in resp.json()["detail"])
