"""阶段九·五 第二批：既有模块的子功能缺口。

④病理标本、⑤会诊费用与统计、⑮缺药登记履约与黑名单、㉓老年健康统计、
⑳课程录制与直播反馈、㉞项目管理。
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
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "第二批县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def township(client, admin):
    return client.post(
        "/api/organizations",
        json={"name": "第二批卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()


@pytest.fixture(scope="module")
def patient(client, admin):
    return client.post(
        "/api/patients",
        json={"name": "第二批患者", "id_card": "320000195001011234", "birth_date": "1950-01-01"},
        headers=admin,
    ).json()


# ================================================================ ④ 病理标本


@pytest.fixture(scope="module")
def path_request(client, admin, org, patient):
    return client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "pathology",
              "item_code": "P001", "item_name": "组织病理学检查"},
        headers=admin,
    ).json()


def test_病理标本不复用检验样本流转(client, admin, org, patient, path_request):
    """检验是采样→冷链转运→中心核收，病理是核收→取材→制片→阅片，
    节点数与含义都不同。原先 advance_sample 对病理直接 422，整段没有状态机。"""
    old_way = client.post(
        f"/api/exams/{path_request['id']}/sample/advance", headers=admin
    )
    assert old_way.status_code == 422  # 检验那套仍然不接病理

    specimen = client.post(
        "/api/pathology/specimens",
        json={"request_id": path_request["id"], "site": "胃窦",
              "excised_at": "2026-08-12T09:00:00", "fixed_at": "2026-08-12T09:20:00",
              "fixative": "10%中性福尔马林"},
        headers=admin,
    )
    assert specimen.status_code == 201, specimen.text
    assert specimen.json()["status"] == "pending"
    assert specimen.json()["cold_ischemia_minutes"] == 20


def test_检验申请不能建病理标本(client, admin, org, patient):
    lab = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"], "center_type": "lab",
              "item_code": "L001", "item_name": "血常规"},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/pathology/specimens", json={"request_id": lab["id"]}, headers=admin
    )
    assert resp.status_code == 422


def test_标本核收后依次取材制片阅片(client, admin, path_request):
    specimen = client.post(
        "/api/pathology/specimens",
        json={"request_id": path_request["id"], "site": "胃体"}, headers=admin
    ).json()
    sid = specimen["id"]
    # 未核收不能直接推进
    assert client.post(f"/api/pathology/specimens/{sid}/advance", json={},
                       headers=admin).status_code == 409
    assert client.post(f"/api/pathology/specimens/{sid}/receive",
                       json={"received_by": "病理技师"}, headers=admin).status_code == 200
    for expected, body in [("embedded", {"block_count": 3}), ("slided", {"slide_count": 6}),
                           ("read", {})]:
        resp = client.post(f"/api/pathology/specimens/{sid}/advance", json=body, headers=admin)
        assert resp.json()["status"] == expected, resp.text
    assert client.post(f"/api/pathology/specimens/{sid}/advance", json={},
                       headers=admin).status_code == 409


def test_拒收只能发生在核收环节(client, admin, path_request):
    """已经开始取材制片了才说标本不合格，晚了。"""
    specimen = client.post(
        "/api/pathology/specimens", json={"request_id": path_request["id"]}, headers=admin
    ).json()
    sid = specimen["id"]
    client.post(f"/api/pathology/specimens/{sid}/receive",
                json={"received_by": "技师"}, headers=admin)
    resp = client.post(f"/api/pathology/specimens/{sid}/reject",
                       json={"reject_reason": "标本量不足"}, headers=admin)
    assert resp.status_code == 409


def test_拒收的标本不可再推进(client, admin, path_request):
    specimen = client.post(
        "/api/pathology/specimens", json={"request_id": path_request["id"]}, headers=admin
    ).json()
    sid = specimen["id"]
    rejected = client.post(f"/api/pathology/specimens/{sid}/reject",
                           json={"reject_reason": "未加固定液"}, headers=admin)
    assert rejected.json()["status"] == "rejected"
    assert client.post(f"/api/pathology/specimens/{sid}/advance", json={},
                       headers=admin).status_code == 409


def test_冷缺血时间未填全的单列不拿当前时间凑数(client, admin, path_request):
    client.post(
        "/api/pathology/specimens",
        json={"request_id": path_request["id"], "site": "未记时间", "excised_at": "2026-08-12T10:00:00"},
        headers=admin,
    )
    stats = client.get("/api/pathology/specimen-stats", headers=admin).json()
    assert stats["cold_ischemia"]["unmeasured"] >= 1
    assert stats["cold_ischemia"]["measured"] >= 1
    assert stats["rejected"] >= 1


# ================================================================ ⑤ 会诊费用


@pytest.fixture(scope="module")
def consultation(client, admin, org, township, patient):
    c = client.post(
        "/api/consultations",
        json={"patient_id": patient["id"], "from_org_id": township["id"],
              "to_org_id": org["id"], "question": "胸片异常请会诊"},
        headers=admin,
    ).json()
    client.post(f"/api/consultations/{c['id']}/accept",
                json={"expert_name": "张主任"}, headers=admin)
    client.post(f"/api/consultations/{c['id']}/complete",
                json={"opinion": "建议进一步 CT"}, headers=admin)
    return c


def test_未完成的会诊不可计费(client, admin, org, township, patient):
    pending = client.post(
        "/api/consultations",
        json={"patient_id": patient["id"], "from_org_id": township["id"],
              "to_org_id": org["id"], "question": "待受理"},
        headers=admin,
    ).json()
    resp = client.post(f"/api/consultations/{pending['id']}/fee",
                       json={"fee": 100}, headers=admin)
    assert resp.status_code == 409


def test_零元会诊与未计费是两回事(client, admin, consultation):
    """本院内部会诊常不计费，用 fee_settled 区分而不是拿 0 当哨兵。"""
    before = client.get("/api/consultations/stats", headers=admin).json()
    assert before["fee"]["unsettled_count"] >= 1

    resp = client.post(f"/api/consultations/{consultation['id']}/fee",
                       json={"fee": 0, "fee_note": "医共体内部会诊不计费"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["fee"] == 0
    assert resp.json()["fee_settled"] is True

    after = client.get("/api/consultations/stats", headers=admin).json()
    assert after["fee"]["settled_count"] == before["fee"]["settled_count"] + 1


def test_会诊评分均值不把未评价当0分(client, admin, consultation):
    """把未评价当 0 分，会让评价率越低分数越难看，
    最后逼出来的是催评分而不是改服务。"""
    client.post(f"/api/consultations/{consultation['id']}/rate",
                json={"rating": 5}, headers=admin)
    stats = client.get("/api/consultations/stats", headers=admin).json()
    assert stats["rating"]["avg"] == 5.0  # 不是 5/(已完成数)
    assert stats["rating"]["unrated_count"] >= 0


# ================================================================ ⑮ 缺药登记


def test_缺药登记履约与黑名单(client, admin, township, patient):
    shortage = client.post(
        "/api/medication/shortages",
        json={"org_id": township["id"], "patient_id": patient["id"],
              "drug_code": "D100", "drug_name": "某降压药", "quantity": 2},
        headers=admin,
    )
    assert shortage.status_code == 201, shortage.text
    sid = shortage.json()["id"]
    # 药没到就说"未取药"是冤枉人
    assert client.post(f"/api/medication/shortages/{sid}/close",
                       json={"result": "no_show"}, headers=admin).status_code == 409
    for _ in range(2):
        client.post(f"/api/medication/shortages/{sid}/advance", headers=admin)
    closed = client.post(f"/api/medication/shortages/{sid}/close",
                         json={"result": "no_show", "reason": "多次联系未取"}, headers=admin)
    assert closed.status_code == 200
    assert closed.json()["status"] == "no_show"

    # 加入缺药黑名单后不能再登记，但预约不受影响（两个域互不干扰）
    client.post("/api/appointments/blacklist",
                json={"patient_id": patient["id"], "reason": "两次登记未取药",
                      "domain": "shortage"}, headers=admin)
    blocked = client.post(
        "/api/medication/shortages",
        json={"org_id": township["id"], "patient_id": patient["id"],
              "drug_code": "D101", "drug_name": "另一种药"},
        headers=admin,
    )
    assert blocked.status_code == 403
    # 按机构报缺（无患者）不受黑名单影响
    assert client.post(
        "/api/medication/shortages",
        json={"org_id": township["id"], "drug_code": "D102", "drug_name": "机构报缺"},
        headers=admin,
    ).status_code == 201


def test_两个黑名单域互不影响(client, admin, township, patient):
    """同一人可以只在缺药黑名单里而不在预约黑名单里。"""
    entries = client.get("/api/appointments/blacklist", headers=admin).json()
    domains = {e["domain"] for e in entries if e["patient_id"] == patient["id"]}
    assert domains == {"shortage"}
    slot = client.post(
        "/api/appointments/slots",
        json={"org_id": township["id"], "resource_type": "outpatient",
              "resource_name": "李医生门诊", "slot_date": "2026-09-01",
              "slot_time": "09:00", "capacity": 5},
        headers=admin,
    ).json()
    booked = client.post(
        "/api/appointments",
        json={"slot_id": slot["id"], "patient_id": patient["id"]}, headers=admin
    )
    assert booked.status_code == 201, booked.text


def test_缺药黑名单可以解除(client, admin, patient, township):
    resp = client.delete(
        f"/api/appointments/blacklist/{patient['id']}",
        params={"domain": "shortage"}, headers=admin,
    )
    assert resp.status_code == 200
    ok = client.post(
        "/api/medication/shortages",
        json={"org_id": township["id"], "patient_id": patient["id"],
              "drug_code": "D103", "drug_name": "解除后再登记"},
        headers=admin,
    )
    assert ok.status_code == 201


def test_履约率分母只含已判定的登记(client, admin):
    """药还没到就算进分母，等于拿采购周期长短去背履约率的锅。"""
    stats = client.get("/api/medication/shortages/stats", headers=admin).json()
    assert stats["no_show"] == 1 and stats["collected"] == 0
    assert stats["fulfillment_rate_pct"] == 0.0
    assert stats["in_transit"] >= 2  # 在途的不进分母


# ================================================================ ㉓ 老年健康


def test_老年统计按人不按评估条数(client, admin, patient):
    """同一人评三次不该在失能构成里占三个坑。"""
    for score in (30, 50, 100):
        client.post(
            "/api/eldercare/assessments",
            json={"patient_id": patient["id"], "adl_score": score,
                  "assessed_date": "2026-08-12"},
            headers=admin,
        )
    stats = client.get("/api/eldercare/stats", headers=admin).json()
    assert stats["assessed_people"] == 1
    assert stats["assessment_records"] == 3
    # 取最近一次（adl=100 → 能力完好）
    assert stats["by_care_level"] == {"能力完好": 1}
    assert stats["disabled_count"] == 0


def test_认知筛查未做的单列不按0分并入(client, admin):
    stats = client.get("/api/eldercare/stats", headers=admin).json()
    assert stats["cognitive"]["unscreened"] == 1
    assert stats["cognitive"]["avg_score"] is None
    assert stats["tcm_constitution"]["not_done"] == 1


# ================================================================ ⑳ 直播


@pytest.fixture(scope="module")
def live(client, admin):
    session = client.post(
        "/api/education/live-sessions",
        json={"title": "县域病理读片会", "speaker": "王主任", "planned_at": "2026-09-01 14:00"},
        headers=admin,
    ).json()
    client.post(f"/api/education/live-sessions/{session['id']}/review",
                params={"approve": True}, headers=admin)
    return session


def test_未结束的直播不能挂回放也不能反馈(client, admin, live):
    """排期中的直播挂上回放，学员点进去是空的，比没有回放更糟。"""
    assert client.post(f"/api/education/live-sessions/{live['id']}/recording",
                       json={"recording_url": "https://x/rec.mp4"},
                       headers=admin).status_code == 409
    assert client.post(f"/api/education/live-sessions/{live['id']}/feedback",
                       json={"rating": 5}, headers=admin).status_code == 409


def test_直播结束后可挂回放与反馈且反馈按覆盖(client, admin, live):
    client.post(f"/api/education/live-sessions/{live['id']}/finish", headers=admin)
    assert client.post(f"/api/education/live-sessions/{live['id']}/recording",
                       json={"recording_url": "https://x/rec.mp4"},
                       headers=admin).status_code == 200

    first = client.post(f"/api/education/live-sessions/{live['id']}/feedback",
                        json={"rating": 3, "comment": "一般"}, headers=admin).json()
    assert first["updated"] is False
    second = client.post(f"/api/education/live-sessions/{live['id']}/feedback",
                         json={"rating": 5, "comment": "再听一遍觉得很好"}, headers=admin).json()
    assert second["updated"] is True

    fb = client.get(f"/api/education/live-sessions/{live['id']}/feedback", headers=admin).json()
    assert fb["count"] == 1  # 一人一场一条，不累计
    assert fb["avg_rating"] == 5.0
    listed = client.get("/api/education/live-sessions", headers=admin).json()
    assert any(s["recording_url"] for s in listed)


# ================================================================ ㉞ 项目管理


def test_结项必须把进度报到100(client, admin, org):
    """允许"已完成但进度 60%"，那份进度数就再也没人信了。"""
    project = client.post(
        "/api/projects",
        json={"org_id": org["id"], "name": "县域影像中心扩建", "owner_name": "李主任",
              "start_date": "2026-01-01", "due_date": "2026-06-30", "budget_amount": 500000},
        headers=admin,
    )
    assert project.status_code == 201, project.text
    pid = project.json()["id"]
    bad = client.patch(f"/api/projects/{pid}",
                       json={"status": "done", "progress_pct": 60}, headers=admin)
    assert bad.status_code == 422
    # 做不完走中止，不要求进度满格
    ok = client.patch(f"/api/projects/{pid}",
                      json={"status": "suspended", "progress_pct": 60}, headers=admin)
    assert ok.status_code == 200


def test_计划完成日期不得早于开始日期(client, admin, org):
    resp = client.post(
        "/api/projects",
        json={"org_id": org["id"], "name": "日期倒挂项目",
              "start_date": "2026-06-01", "due_date": "2026-01-01"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_里程碑逾期按日期现算且可撤销完成(client, admin, org):
    project = client.post(
        "/api/projects",
        json={"org_id": org["id"], "name": "带里程碑的项目", "due_date": "2026-12-31"},
        headers=admin,
    ).json()
    m1 = client.post(f"/api/projects/{project['id']}/milestones",
                     json={"name": "完成招标", "due_date": "2020-01-01"}, headers=admin).json()
    assert m1["overdue"] is True

    done = client.post(f"/api/projects/milestones/{m1['id']}/done",
                       json=None, params={"done_date": "2026-08-12"}, headers=admin).json()
    assert done["done"] is True and done["overdue"] is False  # 完成了就不再算逾期
    reopened = client.post(f"/api/projects/milestones/{m1['id']}/reopen", headers=admin).json()
    assert reopened["done"] is False and reopened["overdue"] is True


def test_平均进度只算在办项目(client, admin, org):
    """把已完成的 100% 混进去，项目一多平均进度就永远好看。"""
    p = client.post(
        "/api/projects", json={"org_id": org["id"], "name": "已完成项目"}, headers=admin
    ).json()
    client.patch(f"/api/projects/{p['id']}",
                 json={"status": "done", "progress_pct": 100}, headers=admin)
    stats = client.get("/api/projects/stats/overview", headers=admin).json()
    assert stats["by_status"]["done"]["count"] == 1
    # 在办项目的进度都是 0，若把已完成的 100 混进来均值就不是 0 了
    assert stats["avg_progress_pct_active"] == 0.0


def test_项目详情一次取回里程碑不逐条查(client, admin, org):
    """P0-1 的教训：列表接口不在循环里逐条查子表。"""
    from sqlalchemy import event

    from app.database import engine

    counter = {"n": 0}

    def listener(*args, **kwargs):
        counter["n"] += 1

    for _ in range(5):
        p = client.post(
            "/api/projects", json={"org_id": org["id"], "name": "批量项目"}, headers=admin
        ).json()
        client.post(f"/api/projects/{p['id']}/milestones",
                    json={"name": "节点"}, headers=admin)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        client.get("/api/projects", headers=admin)
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    assert counter["n"] <= 6, f"列表查询数 {counter['n']} 偏高，疑似 N+1"
