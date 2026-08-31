"""块4：医生移动工作台——路由可达、静态资源与工作台所依赖的接口链路。"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def setup(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "医生工作台卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "m_doctor", "password": "pass123456", "role": "doctor", "org_id": org["id"]},
        headers=admin,
    )
    patient = client.post(
        "/api/patients",
        json={"name": "工作台患者", "id_card": "320981198203034321", "gender": "女"},
        headers=admin,
    ).json()
    return {"org": org, "doctor": login(client, "m_doctor", "pass123456"), "patient": patient}


def test_doctor_page_and_static_assets_served(client):
    page = client.get("/m/doctor")
    assert page.status_code == 200
    assert "医生工作台" in page.text
    # 页面引用的静态资源均可达
    for asset in ("/static/m/doctor.js", "/static/m/doctor.css", "/static/m/m.css"):
        resp = client.get(asset)
        assert resp.status_code == 200, asset
    # 居民端入口不受影响
    assert client.get("/m").status_code == 200


def test_doctor_todos_inbox(client, setup):
    todos = client.get("/api/todos", headers=setup["doctor"]).json()
    assert todos["role"] == "doctor"
    types = {i["type"] for i in todos["items"]}
    assert {"exam_diagnosis", "critical_ack"} <= types


def test_critical_ack_and_resolve_flow(client, admin, setup):
    """工作台危急值：出报告→待确认→确认接收→处置反馈闭环。"""
    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org"]["id"],
            "center_type": "lab",
            "item_code": "LAB-K",
            "item_name": "血钾",
        },
        headers=setup["doctor"],
    ).json()
    report = client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "血钾 7.2mmol/L", "critical": True},
        headers=setup["doctor"],
    ).json()
    assert report["critical_status"] == "notified"

    # 待办中出现待确认危急值
    todos = client.get("/api/todos", headers=setup["doctor"]).json()
    ack_item = next(i for i in todos["items"] if i["type"] == "critical_ack")
    assert any(r["id"] == report["id"] for r in ack_item["list"])

    acked = client.post(f"/api/exams/reports/{report['id']}/acknowledge", headers=setup["doctor"])
    assert acked.status_code == 200 and acked.json()["critical_status"] == "acknowledged"
    resolved = client.post(
        f"/api/exams/reports/{report['id']}/resolve",
        json={"note": "已联系患者急诊复查并降钾治疗"},
        headers=setup["doctor"],
    )
    assert resolved.status_code == 200 and resolved.json()["critical_status"] == "resolved"
    actions = client.get(
        f"/api/exams/reports/{report['id']}/critical-actions", headers=setup["doctor"]
    ).json()
    assert len(actions) >= 2


def test_pending_exam_requests_claim(client, setup):
    """工作台待审检查申请：按状态取单并领取。"""
    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org"]["id"],
            "center_type": "imaging",
            "item_code": "IMG-CT-CHEST",
            "item_name": "胸部CT",
        },
        headers=setup["doctor"],
    ).json()
    pending = client.get("/api/exams?status=pending", headers=setup["doctor"]).json()
    assert any(r["id"] == req["id"] for r in pending)

    claimed = client.post(f"/api/exams/{req['id']}/claim", headers=setup["doctor"])
    assert claimed.status_code == 200 and claimed.json()["status"] == "diagnosing"
    assert any(
        r["id"] == req["id"] for r in client.get("/api/exams?status=diagnosing", headers=setup["doctor"]).json()
    )


def test_chronic_followup_entry_from_workbench(client, setup):
    """工作台慢病随访：病种目录取指标定义 → 录入随访 → 即时分级。"""
    types = client.get("/api/chronic/disease-types?active=true", headers=setup["doctor"]).json()
    hypertension = next(t for t in types if t["code"] == "hypertension")
    keys = [m["key"] for m in hypertension["level_rules"]["metrics"]]
    assert keys == ["sbp", "dbp"]

    chronic = client.post(
        "/api/chronic",
        json={
            "patient_id": setup["patient"]["id"],
            "disease": "hypertension",
            "managed_by_org_id": setup["org"]["id"],
        },
        headers=setup["doctor"],
    ).json()
    result = client.post(
        f"/api/chronic/{chronic['id']}/followups",
        json={"sbp": 168, "dbp": 104, "metrics": {}},
        headers=setup["doctor"],
    ).json()
    assert result["level"] == 3
    assert result["refer_up_suggested"] is True
    assert result["next_due"]


def test_patient_archive_quick_lookup(client, setup):
    archive = client.get(
        f"/api/archive/{setup['patient']['ehc_no']}", headers=setup["doctor"]
    )
    assert archive.status_code == 200
    data = archive.json()
    assert data["patient"]["name"] == "工作台患者"
    assert any(c["disease"] == "hypertension" for c in data["chronic_diseases"])
    assert client.get("/api/archive/EHCNOTEXIST", headers=setup["doctor"]).status_code == 404


def test_workbench_apis_require_authentication(client):
    for path in ("/api/todos", "/api/exams/critical", "/api/chronic/disease-types"):
        assert client.get(path).status_code == 401, path


def test_doctor_mobile_has_round_and_surgery_tabs(client):
    """查房与手术是移动场景（医生查房不带电脑），页签与表单必须在页面上。"""
    page = client.get("/m/doctor").text
    for tab in ("round", "surgery"):
        assert f'data-tab="{tab}"' in page, f"缺少页签：{tab}"
        assert f'id="tab-{tab}"' in page
    # 查房：病程书写 + 体征录入
    assert 'id="round-note"' in page and 'id="round-vital"' in page
    assert 'id="rv-temp"' in page and 'id="rv-pulse"' in page
    # 手术：排班与待填术中记录两个区块
    assert 'id="surgery-schedule"' in page and 'id="surgery-requests"' in page


def test_doctor_mobile_tabs_registered_in_js(client):
    js = client.get("/static/m/doctor.js").text
    assert "round: loadRound" in js and "surgery: loadSurgery" in js
    # 启动调用必须在文件末尾，否则新页签用到的模块级状态还在 TDZ
    assert js.rstrip().endswith("switchTab(currentTab());")


def test_round_tab_backend_flow(client, admin, setup):
    """查房页用到的三个接口串起来：写病程 → 录体征 → 完整性自查反映变化。"""
    org = client.post(
        "/api/organizations",
        json={"name": "查房演示院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "查房病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "R01"}, headers=admin
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "查房患者", "id_card": "331682199001011234"},
        headers=admin,
    ).json()
    adm = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "查房医师", "diagnosis_name": "肺炎"},
        headers=admin,
    ).json()

    before = client.get(
        f"/api/inpatient/admissions/{adm['id']}/document-completeness", headers=setup["doctor"]
    ).json()
    assert before["complete"] is False

    assert client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "first", "content": "患者因发热咳嗽入院，胸片示右下肺斑片影"},
        headers=setup["doctor"],
    ).status_code == 201
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/progress-notes",
        json={"note_type": "daily", "content": "体温已降至37.2℃，继续抗感染"},
        headers=setup["doctor"],
    )
    # 移动端只填了体温，其余字段不传 —— 应落库为 null 而不是 0
    client.post(
        f"/api/inpatient/admissions/{adm['id']}/vitals",
        json={"measured_at": "2026-08-12 08:00", "temperature": 37.2},
        headers=setup["doctor"],
    )
    vitals = client.get(
        f"/api/inpatient/admissions/{adm['id']}/vitals", headers=setup["doctor"]
    ).json()
    assert vitals[0]["temperature"] == 37.2 and vitals[0]["pulse"] is None

    after = client.get(
        f"/api/inpatient/admissions/{adm['id']}/document-completeness", headers=setup["doctor"]
    ).json()
    assert after["missing"] == ["缺护理记录"]  # 护理由护士记，医生查房不负责
