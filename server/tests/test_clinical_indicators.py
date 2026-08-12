"""医疗质量三维指标与用药结构分析（指南 #14 / #48 / #49 / #52）。

这批指标会被写进考核，所以用例盯的不是"接口通不通"，而是**口径对不对**：
分母该排除谁、没采集的数据会不会被当成不合格、DDD 没维护会不会悄悄漏算。
一个算错的治愈率比没有治愈率更危险。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor(client, admin, org):
    client.post(
        "/api/users",
        json={"username": "qi_doc", "password": "passw0rd1", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "qi_doc", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def director(client, admin, org):
    client.post(
        "/api/users",
        json={"username": "qi_dir", "password": "passw0rd1", "role": "director", "org_id": org["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "qi_dir", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "质量指标县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


def _patient(client, admin, name, id_card):
    return client.post(
        "/api/patients", json={"name": name, "id_card": id_card}, headers=admin
    ).json()


def _journey(client, admin, org, bed, patient, admit_dx, discharge_dx, outcome,
             total_cost=1000, drug_cost=300):
    """走一次完整住院：入院→出院病案首页→出院。返回 admission。"""
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": bed["ward_id"], "bed_id": bed["id"],
              "doctor_name": "主治", "diagnosis_name": admit_dx},
        headers=admin,
    ).json()
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/case-summary",
        json={"discharge_diagnosis": discharge_dx, "outcome": outcome,
              "total_cost": total_cost, "drug_cost": drug_cost},
        headers=admin,
    )
    client.post(f"/api/inpatient/admissions/{adm['id']}/discharge", headers=admin)
    return adm


@pytest.fixture(scope="module")
def cohort(client, admin, org):
    """三份出院病例：诊断一致治愈、诊断不一致好转、诊断一致死亡。"""
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "指标病区"}, headers=admin
    ).json()
    beds = [
        client.post(
            "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": no}, headers=admin
        ).json()
        for no in ("Q1", "Q2", "Q3")
    ]
    p1 = _patient(client, admin, "指标甲", "331782198001011231")
    p2 = _patient(client, admin, "指标乙", "331782198001011232")
    p3 = _patient(client, admin, "指标丙", "331782198001011233")
    # 括号内是治疗方式补充，归一化后应判为"符合"
    _journey(client, admin, org, beds[0], p1, "社区获得性肺炎", "社区获得性肺炎（抗感染后）", "治愈")
    _journey(client, admin, org, beds[1], p2, "腹痛待查", "急性阑尾炎", "好转")
    _journey(client, admin, org, beds[2], p3, "急性心肌梗死", "急性心肌梗死", "死亡")
    return {"ward": ward, "beds": beds, "patients": [p1, p2, p3]}


def test_diagnosis_match_normalizes_parenthetical(client, admin, org, cohort):
    """括号内的治疗补充不该被当成另一个诊断。

    3 例中 2 例入出院诊断一致（其中一例带括号补充），符合率应为 66.67%。
    """
    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    match = next(i for i in data["indicators"] if i["key"] == "admit_discharge_match")
    assert match["denominator"] == 3
    assert match["numerator"] == 2
    assert match["rate_pct"] == 66.67


def test_cure_and_mortality_from_case_summary(client, admin, org, cohort):
    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    cure = next(i for i in data["indicators"] if i["key"] == "cure_improve")
    death = next(i for i in data["indicators"] if i["key"] == "mortality")
    assert (cure["numerator"], cure["denominator"]) == (2, 3)   # 治愈 + 好转
    assert (death["numerator"], death["denominator"]) == (1, 3)
    assert death["rate_pct"] == 33.33


def test_每项指标都带分子分母与口径(client, admin, org, cohort):
    """光给百分比没法核对，也看不出样本量小到不该看。"""
    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    for item in data["indicators"]:
        assert {"numerator", "denominator", "rate_pct", "caliber", "dimension"} <= set(item)
        assert item["caliber"]


def test_未采集的手术诊断不进分母(client, admin, org, cohort, director, doctor):
    """术前术后诊断留空是"未采集"，不是"不符合"——不能拉低符合率。"""
    ward, beds = cohort["ward"], cohort["beds"]
    patient = _patient(client, admin, "手术甲", "331782198001011241")
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "QS1"}, headers=admin
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "外科", "diagnosis_name": "急性阑尾炎"},
        headers=admin,
    ).json()
    room = client.post(
        "/api/surgery/rooms", json={"org_id": org["id"], "name": "指标手术间"}, headers=admin
    ).json()

    def run_surgery(preop, postop, start="09:00", end="10:00"):
        req = client.post(
            "/api/surgery/requests",
            json={"admission_id": adm["id"], "surgery_name": "阑尾切除术", "incision_level": "II",
                  "anesthesia_type": "general", "urgency": "elective", "surgeon_name": "外科",
                  "planned_date": "2026-08-12"},
            headers=admin,
        ).json()
        client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True},
                    headers=director)
        client.post(
            f"/api/surgery/requests/{req['id']}/schedule",
            json={"room_id": room["id"], "scheduled_date": "2026-08-12",
                  "start_time": start, "end_time": end},
            headers=admin,
        )
        body = {"actual_surgery_name": "阑尾切除术", "outcome": "治愈"}
        if preop:
            body |= {"preop_diagnosis": preop, "postop_diagnosis": postop}
        return client.post(f"/api/surgery/requests/{req['id']}/record", json=body, headers=doctor)

    assert run_surgery("急性阑尾炎", "急性阑尾炎（化脓性）").status_code == 201
    # 换时段——同手术间同时段会被并发占用校验挡下（409），那是对的
    assert run_surgery("", "", start="11:00", end="12:00").status_code == 201  # 未采集

    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    match = next(i for i in data["indicators"] if i["key"] == "preop_postop_match")
    assert match["denominator"] == 1        # 只有填了两项诊断的那台进分母
    assert match["numerator"] == 1
    assert match["uncollected"] == 1        # 未采集单独报出来，不藏着
    assert match["rate_pct"] == 100.0


def test_未判定的抢救不进分母(client, admin, org, doctor):
    """"没下结论"和"没救过来"必须分开，否则成功率被系统性算低。"""
    cases = []
    for symptom in ("胸痛A", "胸痛B", "胸痛C"):
        case = client.post(
            "/api/emergency/cases",
            json={"location": "某村", "symptom": symptom, "dest_org_id": org["id"]},
            headers=admin,
        ).json()
        for _ in range(2):  # dispatched → en_route → arrived
            client.post(f"/api/emergency/cases/{case['id']}/advance", headers=admin)
        cases.append(case)
    client.post(f"/api/emergency/cases/{cases[0]['id']}/rescue-outcome",
                json={"rescue_outcome": "success"}, headers=doctor)
    client.post(f"/api/emergency/cases/{cases[1]['id']}/rescue-outcome",
                json={"rescue_outcome": "failed"}, headers=doctor)
    # 第三例不判定

    data = client.get(f"/api/quality/clinical-indicators?org_id={org['id']}", headers=admin).json()
    rescue = next(i for i in data["indicators"] if i["key"] == "rescue_success")
    assert rescue["denominator"] == 2      # 未判定的那例被排除
    assert rescue["numerator"] == 1
    assert rescue["rate_pct"] == 50.0


def test_未到院不可判定抢救转归(client, admin, org, doctor):
    case = client.post(
        "/api/emergency/cases",
        json={"location": "路上", "symptom": "转运中", "dest_org_id": org["id"]},
        headers=admin,
    ).json()
    resp = client.post(f"/api/emergency/cases/{case['id']}/rescue-outcome",
                       json={"rescue_outcome": "success"}, headers=doctor)
    assert resp.status_code == 409


def test_抢救转归限医师(client, admin, org):
    case = client.post(
        "/api/emergency/cases",
        json={"location": "某村", "symptom": "越权测试", "dest_org_id": org["id"]},
        headers=admin,
    ).json()
    for _ in range(2):
        client.post(f"/api/emergency/cases/{case['id']}/advance", headers=admin)
    client.post(
        "/api/users",
        json={"username": "qi_op", "password": "passw0rd1", "role": "operator", "org_id": org["id"]},
        headers=admin,
    )
    op = client.post("/api/auth/login", json={"username": "qi_op", "password": "passw0rd1"}).json()
    resp = client.post(
        f"/api/emergency/cases/{case['id']}/rescue-outcome",
        json={"rescue_outcome": "success"},
        headers={"Authorization": f"Bearer {op['access_token']}"},
    )
    assert resp.status_code == 403


def test_period_格式错误返回422(client, admin):
    assert client.get("/api/quality/clinical-indicators?period=2026", headers=admin).status_code == 422


# ---------------- 用药结构：药占比与抗菌药物使用强度 ----------------


def test_住院药占比取自病案首页(client, admin, org, cohort):
    period = _current_period()
    data = client.get(f"/api/analytics/drug-use?period={period}&org_id={org['id']}",
                      headers=admin).json()
    row = data["orgs"][0]
    # cohort 三例各 1000 元总费用 300 元药费，加上手术用例那一例（未开病案首页则不计）
    assert row["inpatient_total"] > 0
    assert row["inpatient_drug_ratio_pct"] == pytest.approx(
        row["inpatient_drug"] / row["inpatient_total"] * 100, abs=0.01)


def test_未维护ddd的抗菌药计入未覆盖而不是按零算(client, admin, org, cohort):
    """DDD 没维护就明说没维护。悄悄跳过会让强度值偏低且无人察觉。"""
    patient = cohort["patients"][0]
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "CEF-NO-DDD", "max_daily_dose": 4000, "antibiotic": True, "ddd": 0},
        headers=admin,
    )
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "CEF-WITH-DDD", "max_daily_dose": 4000, "antibiotic": True, "ddd": 2},
        headers=admin,
    )
    for code in ("CEF-NO-DDD", "CEF-WITH-DDD"):
        client.post(
            "/api/prescriptions",
            json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "肺炎",
                  "items": [{"drug_code": code, "drug_name": code, "daily_dose": 2, "days": 5}]},
            headers=admin,
        )
    period = _current_period()
    data = client.get(f"/api/analytics/drug-use?period={period}&org_id={org['id']}",
                      headers=admin).json()
    row = data["orgs"][0]
    assert row["ddd_uncovered_items"] == 1          # 未维护的那条被报出来
    assert row["antibiotic_ddds"] == pytest.approx(5.0)  # 2×5÷2，只算维护了 DDD 的
    assert row["bed_days"] > 0
    assert row["antibiotic_intensity"] == pytest.approx(5.0 * 100 / row["bed_days"], abs=0.01)


def test_ddd量纲填错会被告警而不是静静地报出天文数字(client, admin, org, cohort):
    """DDD 填成 3（g）而处方按 mg 记，强度会差三个数量级。

    单位程序校验不了，但数量级有常识：国家控制目标 40 DDDs/百人天，
    破 200 基本只有填错单位这一种解释。宁可在同一份响应里说它可疑，
    也不能让一个天文数字被原样抄进上报表。
    """
    patient = cohort["patients"][1]
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "CEF-WRONG-UNIT", "max_daily_dose": 4000,
              "dose_unit": "mg", "antibiotic": True, "ddd": 3},   # 应为 3000
        headers=admin,
    )
    client.post(
        "/api/prescriptions",
        json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "肺炎",
              "items": [{"drug_code": "CEF-WRONG-UNIT", "drug_name": "头孢呋辛",
                         "daily_dose": 3000, "days": 7}]},
        headers=admin,
    )
    period = _current_period()
    data = client.get(f"/api/analytics/drug-use?period={period}&org_id={org['id']}",
                      headers=admin).json()
    row = data["orgs"][0]
    assert row["antibiotic_intensity"] > 200
    # 用例里的收治人天只有个位数，强度本来就不可解释——此时不该报"单位填错"，
    # 该报"样本不足"。两种原因指向完全不同的处置动作，不能混为一谈。
    assert row["intensity_unstable"] is True
    assert not data["warnings"]


def test_样本足够时量纲异常才告警(client, admin, org, cohort, monkeypatch):
    """把最小样本量临时压到 1，模拟真实体量下的量纲错误。"""
    from app.routers import analytics

    monkeypatch.setattr(analytics, "MIN_BED_DAYS_FOR_INTENSITY", 1)
    period = _current_period()
    data = client.get(f"/api/analytics/drug-use?period={period}&org_id={org['id']}",
                      headers=admin).json()
    assert data["warnings"], "量纲异常必须随结果一起返回，不能只在数字里"
    assert "DDD" in data["warnings"][0]


def test_口径随结果一起返回(client, admin, org):
    period = _current_period()
    data = client.get(f"/api/analytics/drug-use?period={period}", headers=admin).json()
    assert "drug_ratio" in data["caliber"] and "antibiotic_intensity" in data["caliber"]


def _current_period():
    from datetime import datetime

    return datetime.now().strftime("%Y-%m")
