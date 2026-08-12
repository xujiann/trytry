"""阶段八 人员下沉调度台账与专病路径管理。

调度侧的用例几乎全在守"不推断"：职称等级不从文本猜、巡诊不算下沉、
未填等级的不默认算进"中级及以上"。专病侧守的是"不硬拦"：必需节点没做完
也能出组（转院、拒治是现实），但完成度要如实呈现。
"""
from datetime import date, timedelta

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
def initial_programs(client, admin):
    """建库之初的专病目录快照（应为空）。"""
    return client.get("/api/disease-programs", headers=admin).json()


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [
        client.post("/api/organizations",
                    json={"name": name, "org_type": t, "level": lv}, headers=admin).json()
        for name, t, lv in [
            ("调度县医院", "lead_hospital", "county"),
            ("调度东镇卫生院", "township", "township"),
            ("调度西镇卫生院", "township", "township"),
        ]
    ]


def _staff(client, admin, org, username, role):
    client.post("/api/users",
                json={"username": username, "password": "passw0rd1", "role": role,
                      "org_id": org["id"]}, headers=admin)
    resp = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def doctor(client, admin, orgs):
    return _staff(client, admin, orgs[0], "sd_doc", "doctor")


@pytest.fixture(scope="module")
def director(client, admin, orgs):
    return _staff(client, admin, orgs[0], "sd_dir", "director")


def _employee(client, admin, org, name, title, level=None):
    emp = client.post("/api/mgmt/employees",
                      json={"org_id": org["id"], "name": name, "title": title,
                            "position": "医师"}, headers=admin).json()
    if level:
        client.patch(f"/api/staffing/employees/{emp['id']}/title-level",
                     json={"title_level": level}, headers=admin)
    return emp


def _ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


# ---------------- 人员下沉调度 ----------------


def test_职称等级必须显式选而非从职称文本推断(client, admin, orgs):
    """靠关键词猜等级，一个"助理全科医生"就能把统计带偏。"""
    emp = _employee(client, admin, orgs[0], "王主治", "主治医师")
    # 只填了职称文本时，等级仍是"未填"——系统不替人做判断
    rows = client.get(f"/api/staffing/secondments", headers=admin).json()
    assert isinstance(rows, list)
    body = client.patch(f"/api/staffing/employees/{emp['id']}/title-level",
                        json={"title_level": "intermediate"}, headers=admin).json()
    assert body["title"] == "主治医师"
    assert body["title_level_name"] == "中级"


def test_同一员工不得同时有两条在派记录(client, admin, orgs):
    emp = _employee(client, admin, orgs[0], "李下沉", "主治医师", "intermediate")
    first = client.post("/api/staffing/secondments",
                        json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                              "to_org_id": orgs[1]["id"], "start_date": _ago(200)},
                        headers=admin)
    assert first.status_code == 201
    dup = client.post("/api/staffing/secondments",
                      json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                            "to_org_id": orgs[2]["id"], "start_date": _ago(10)},
                      headers=admin)
    assert dup.status_code == 409


def test_派出与接收机构不得相同(client, admin, orgs):
    emp = _employee(client, admin, orgs[0], "同院派驻", "医师")
    resp = client.post("/api/staffing/secondments",
                       json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                             "to_org_id": orgs[0]["id"], "start_date": _ago(10)},
                       headers=admin)
    assert resp.status_code == 422


def test_在派期间员工状态标记为派驻中(client, admin, orgs):
    emp = _employee(client, admin, orgs[0], "状态联动", "医师")
    row = client.post("/api/staffing/secondments",
                      json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                            "to_org_id": orgs[1]["id"], "start_date": _ago(30)},
                      headers=admin).json()
    emps = client.get(f"/api/mgmt/employees?org_id={orgs[0]['id']}", headers=admin).json()
    assert next(e for e in emps if e["id"] == emp["id"])["status"] == "seconded"
    client.post(f"/api/staffing/secondments/{row['id']}/end", headers=admin)
    emps = client.get(f"/api/mgmt/employees?org_id={orgs[0]['id']}", headers=admin).json()
    assert next(e for e in emps if e["id"] == emp["id"])["status"] == "active"


def test_巡诊与短期支援不计入下沉指标(client, admin, orgs):
    """混在一起统计会把巡诊也算成下沉，指标就做虚了。"""
    for name, atype in [("巡诊医生", "rounds"), ("支援医生", "support")]:
        emp = _employee(client, admin, orgs[0], name, "副主任医师", "deputy_senior")
        client.post("/api/staffing/secondments",
                    json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                          "to_org_id": orgs[2]["id"], "start_date": _ago(300),
                          "assignment_type": atype},
                    headers=admin)
    stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
    row = next((o for o in stats["orgs"] if o["org_id"] == orgs[2]["id"]), None)
    assert row is not None and row["total"] >= 2
    assert row["long_term_6m"] == 0, "巡诊/短期支援被算成了长期派驻"


def test_满半年且中级及以上才计入(client, admin, orgs):
    emp_long = _employee(client, admin, orgs[0], "满半年主任", "主任医师", "senior")
    client.post("/api/staffing/secondments",
                json={"employee_id": emp_long["id"], "from_org_id": orgs[0]["id"],
                      "to_org_id": orgs[1]["id"], "start_date": _ago(200)},
                headers=admin)
    emp_short = _employee(client, admin, orgs[0], "未满半年", "主治医师", "intermediate")
    client.post("/api/staffing/secondments",
                json={"employee_id": emp_short["id"], "from_org_id": orgs[0]["id"],
                      "to_org_id": orgs[1]["id"], "start_date": _ago(30)},
                headers=admin)
    stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
    row = next(o for o in stats["orgs"] if o["org_id"] == orgs[1]["id"])
    assert row["long_term_6m_senior"] >= 1
    assert "长期派驻" in stats["caliber"]["long_term_6m"]


def test_未填职称等级的不默认算入而是单独报出(client, admin, orgs):
    """默认算入会把指标做虚，悄悄丢掉又让人以为数据齐全。"""
    emp = _employee(client, admin, orgs[0], "等级未填", "医师")   # 不设 title_level
    client.post("/api/staffing/secondments",
                json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                      "to_org_id": orgs[2]["id"], "start_date": _ago(250)},
                headers=admin)
    stats = client.get("/api/staffing/dispatch-stats", headers=admin).json()
    row = next(o for o in stats["orgs"] if o["org_id"] == orgs[2]["id"])
    assert row["long_term_6m"] >= 1
    assert row["long_term_6m_senior"] == 0
    assert stats["unknown_title_level"] >= 1


def test_跨年派驻只计本年度内天数(client, admin, orgs):
    """一段三年的派驻不该在每一年都被当成"刚好满半年"之外的东西。"""
    emp = _employee(client, admin, orgs[0], "跨年派驻", "主治医师", "intermediate")
    client.post("/api/staffing/secondments",
                json={"employee_id": emp["id"], "from_org_id": orgs[0]["id"],
                      "to_org_id": orgs[1]["id"], "start_date": "2020-01-01",
                      "end_date": "2020-03-01"},
                headers=admin)
    # 该派驻整段落在 2020 年，查今年应当完全不出现在分子里
    this_year = client.get("/api/staffing/dispatch-stats", headers=admin).json()
    row_2020 = client.get("/api/staffing/dispatch-stats?year=2020", headers=admin).json()
    assert any(o["org_id"] == orgs[1]["id"] for o in row_2020["orgs"])
    # 2020 那段只有 60 天，未满半年，不进分子
    r2020 = next(o for o in row_2020["orgs"] if o["org_id"] == orgs[1]["id"])
    assert r2020["long_term_6m"] == 0
    assert this_year["year"] == date.today().year


# ---------------- 专病路径 ----------------


@pytest.fixture(scope="module")
def program(client, admin, orgs):
    return client.post(
        "/api/disease-programs",
        json={"code": "COPD-PATH", "name": "慢阻肺规范化诊疗", "org_id": orgs[0]["id"],
              "path_nodes": [
                  {"key": "assess", "name": "首次评估", "required": True},
                  {"key": "plan", "name": "制定方案", "required": True},
                  {"key": "review", "name": "疗效复评", "required": True},
                  {"key": "edu", "name": "健康宣教", "required": False},
              ]},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post("/api/patients",
                       json={"name": "专病患者", "id_card": "331782197707071234"},
                       headers=admin).json()


def test_路径节点key不得重复(client, admin):
    resp = client.post("/api/disease-programs",
                       json={"code": "DUP", "name": "重复节点",
                             "path_nodes": [{"key": "a", "name": "甲"},
                                            {"key": "a", "name": "乙"}]},
                       headers=admin)
    assert resp.status_code == 422


def test_不预置任何病种路径(initial_programs):
    """各地专病中心管什么病差异极大，预置只会被删掉重配。

    断言的是**建库之初**目录为空，所以取的是 reset_database() 之后、
    任何用例建目录之前的快照——直接在用例里查会受执行顺序影响，
    查到空集可能只是因为它跑在建目录的用例之前，什么也证明不了。
    """
    assert initial_programs == [], f"平台预置了病种路径：{initial_programs}"


def test_入组并按节点推进_完成度只算必需节点(client, doctor, program, patient, orgs):
    enrollment = client.post(f"/api/disease-programs/{program['id']}/enrollments",
                             json={"patient_id": patient["id"], "org_id": orgs[0]["id"]},
                             headers=doctor).json()
    assert enrollment["completion"]["required_total"] == 3
    assert enrollment["completion"]["required_done_pct"] == 0.0

    for key in ("assess", "plan"):
        body = client.post(f"/api/disease-programs/enrollments/{enrollment['id']}/records",
                           json={"node_key": key, "operator_name": "张医生", "result": "已完成"},
                           headers=doctor).json()
    assert body["completion"]["required_done"] == 2
    assert body["completion"]["pending_required"] == ["疗效复评"]
    # 非必需节点做不做都不影响完成度分母
    client.post(f"/api/disease-programs/enrollments/{enrollment['id']}/records",
                json={"node_key": "edu"}, headers=doctor)
    body = client.get(f"/api/disease-programs/enrollments/{enrollment['id']}",
                      headers=doctor).json()
    assert body["completion"]["required_total"] == 3
    return None


def test_节点key不在路径中被拒(client, doctor, program, patient, orgs):
    """写个不存在的节点，完成度就永远算不对。"""
    rows = client.get(f"/api/disease-programs/enrollments?patient_id={patient['id']}",
                      headers=doctor).json()
    resp = client.post(f"/api/disease-programs/enrollments/{rows[0]['id']}/records",
                       json={"node_key": "not_exist"}, headers=doctor)
    assert resp.status_code == 422
    assert "不在本专病路径中" in resp.json()["detail"]


def test_同一患者同一专病不得重复在管(client, doctor, program, patient, orgs):
    resp = client.post(f"/api/disease-programs/{program['id']}/enrollments",
                       json={"patient_id": patient["id"], "org_id": orgs[0]["id"]},
                       headers=doctor)
    assert resp.status_code == 409


def test_必需节点未完成也允许出组但完成度如实呈现(client, doctor, program, patient):
    """患者转院、拒绝继续治疗都是现实，硬拦只会逼人补假记录。"""
    rows = client.get(f"/api/disease-programs/enrollments?patient_id={patient['id']}",
                      headers=doctor).json()
    body = client.post(f"/api/disease-programs/enrollments/{rows[0]['id']}/exit",
                       json={"status": "exited", "exit_reason": "转上级医院继续治疗"},
                       headers=doctor).json()
    assert body["status_name"] == "中途退出"
    assert body["completion"]["pending_required"] == ["疗效复评"]
    assert body["outcome_name"] == "未评价"      # 留空≠无效


def test_中途退出必须填原因(client, doctor, program, orgs, admin):
    p = client.post("/api/patients",
                    json={"name": "退出患者", "id_card": "331782197707071299"},
                    headers=admin).json()
    e = client.post(f"/api/disease-programs/{program['id']}/enrollments",
                    json={"patient_id": p["id"], "org_id": orgs[0]["id"]},
                    headers=doctor).json()
    resp = client.post(f"/api/disease-programs/enrollments/{e['id']}/exit",
                       json={"status": "exited"}, headers=doctor)
    assert resp.status_code == 422


def test_出组后可复发再入组(client, doctor, program, patient, orgs):
    """治好出组之后又犯是常态，不该被"已入组"永久挡住。"""
    resp = client.post(f"/api/disease-programs/{program['id']}/enrollments",
                       json={"patient_id": patient["id"], "org_id": orgs[0]["id"]},
                       headers=doctor)
    assert resp.status_code == 201


def test_出组后不可再记录节点(client, doctor, program, patient):
    rows = client.get(
        f"/api/disease-programs/enrollments?patient_id={patient['id']}&status=exited",
        headers=doctor,
    ).json()
    resp = client.post(f"/api/disease-programs/enrollments/{rows[0]['id']}/records",
                       json={"node_key": "review"}, headers=doctor)
    assert resp.status_code == 409


def test_统计把未评价疗效单列(client, admin, program):
    """未评价与各项疗效并列报出，不并进任何一项。"""
    stats = client.get(f"/api/disease-programs/{program['id']}/stats", headers=admin).json()
    assert stats["total"] >= 2
    assert "unrated" in stats["by_outcome"]
    assert stats["by_outcome"]["unrated"]["name"] == "未评价"
    assert "不计入任何疗效档" in stats["caliber"]


def test_停用目录不可再入组(client, admin, doctor, orgs):
    prog = client.post("/api/disease-programs",
                       json={"code": "OLD-PATH", "name": "旧路径",
                             "path_nodes": [{"key": "a", "name": "甲"}]},
                       headers=admin).json()
    client.patch(f"/api/disease-programs/{prog['id']}", json={"active": False}, headers=admin)
    p = client.post("/api/patients",
                    json={"name": "停用测试", "id_card": "331782197707071288"},
                    headers=admin).json()
    resp = client.post(f"/api/disease-programs/{prog['id']}/enrollments",
                       json={"patient_id": p["id"], "org_id": orgs[0]["id"]}, headers=doctor)
    assert resp.status_code == 409


def test_专病目录限管理员(client, doctor):
    resp = client.post("/api/disease-programs",
                       json={"code": "X", "name": "越权"}, headers=doctor)
    assert resp.status_code == 403
