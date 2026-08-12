"""阶段九 代码健康度收口的回归用例。

覆盖 D-1（手术排班两段事务）、D-2（并发建全域基金池）、D-3（假日期）、
D-4（接种禁忌无解除路径）与 P0-2（时钟统一）。

每条用例都对应一次**实测复现过的**故障，不是照着代码补的形式化断言。
"""
import ast
import os

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import models
from app.clock import now_aware, now_naive, today_str
from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def seed_org_patient(client, admin_headers):
    org = client.post(
        "/api/organizations",
        json={"name": "阶段九县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin_headers,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "阶段九测试", "id_card": "320000199001019876", "gender": "male",
              "birth_date": "1990-01-01", "phone": "13800001234"},
        headers=admin_headers,
    ).json()
    return org["id"], patient["id"]


# ---------------------------------------------------------------- P0-2 时钟

def test_全平台不得再出现裸时间函数():
    """`app/` 下只有 `clock.py` 允许直接调 datetime.now / utcnow。

    四套时钟并存已经真炸过一次（阶段二 monitor 拿 aware 比 naive 直接
    TypeError）。这条扫描是防它复发的唯一手段——新写的代码不走 clock，
    这里会红。
    """
    root = os.path.join(os.path.dirname(__file__), "..", "app")
    offenders = []
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py") or name == "clock.py":
                continue
            path = os.path.join(dirpath, name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr not in {"now", "utcnow"}:
                    continue
                owner = node.func.value
                if isinstance(owner, ast.Name) and owner.id == "datetime":
                    offenders.append(f"{os.path.relpath(path, root)}:{node.lineno}")
    assert offenders == [], f"这些位置应改用 app.clock：{offenders}"


def test_落库时刻一律naive():
    """`models.utcnow` 是近两百处列默认值的来源，它必须是 naive。"""
    assert models.utcnow().tzinfo is None
    assert now_naive().tzinfo is None
    assert now_aware().tzinfo is not None


# ---------------------------------------------------------------- D-3 假日期

@pytest.mark.parametrize(
    "path,extra,field",
    [
        ("/api/checkups", {"exam_date": "2026-02-31"}, "exam_date"),
        ("/api/medwaste", {"waste_type": "infectious", "weight_kg": 1.0,
                           "collected_date": "2026-13-01"}, "collected_date"),
    ],
)
def test_日历上不存在的日期在入口就被拦下(client, admin_headers, seed_org_patient, path, extra, field):
    """D-3：正则只管形状不管日历，`2026-02-31` 曾能入库。

    入库之后各处统计 strptime 失败就 continue——实测该派驻整条从下沉指标里
    消失了。这违反平台"异常数据要单独报出来"的一贯原则，所以改成入口拦截。
    """
    org_id, patient_id = seed_org_patient
    payload = {"org_id": org_id, "patient_id": patient_id, **extra}
    resp = client.post(path, json=payload, headers=admin_headers)
    assert resp.status_code == 422, resp.text
    assert field in str(resp.json())


def test_合法日期照常放行(client, admin_headers, seed_org_patient):
    org_id, patient_id = seed_org_patient
    resp = client.post(
        "/api/checkups",
        json={"patient_id": patient_id, "org_id": org_id, "exam_date": "2026-02-28"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text


def test_闰年2月29日不被误杀(client, admin_headers, seed_org_patient):
    """2024 是闰年——把校验写成"2月最多28天"的实现会在这里露馅。"""
    org_id, patient_id = seed_org_patient
    resp = client.post(
        "/api/checkups",
        json={"patient_id": patient_id, "org_id": org_id, "exam_date": "2024-02-29"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------- D-4 禁忌解除

def test_暂时禁忌解除后可以接种(client, admin_headers, seed_org_patient):
    """D-4：原先登记后永久硬拦截，退了热也再打不了这支疫苗，只能改数据库。"""
    org_id, patient_id = seed_org_patient
    created = client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": patient_id, "vaccine_code": "HepB", "reason": "发热",
              "contra_type": "temporary", "valid_until": "2099-12-31"},
        headers=admin_headers,
    )
    assert created.status_code == 201, created.text
    contra_id = created.json()["id"]

    blocked = client.post(
        "/api/vaccination/records",
        json={"patient_id": patient_id, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗",
              "dose_no": 1, "vaccinated_date": today_str(), "org_id": org_id},
        headers=admin_headers,
    )
    assert blocked.status_code == 409

    lifted = client.post(
        f"/api/vaccination/contraindications/{contra_id}/lift",
        json={"lift_reason": "体温已恢复正常"},
        headers=admin_headers,
    )
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["status"] == "lifted"
    assert lifted.json()["blocking"] is False

    ok = client.post(
        "/api/vaccination/records",
        json={"patient_id": patient_id, "vaccine_code": "HepB", "vaccine_name": "乙肝疫苗",
              "dose_no": 1, "vaccinated_date": today_str(), "org_id": org_id},
        headers=admin_headers,
    )
    assert ok.status_code == 201, ok.text


def test_过期的暂时禁忌自动不再拦截(client, admin_headers, seed_org_patient):
    """过期按日期现算，不靠定时任务改状态——那会让失效时刻取决于任务跑没跑。"""
    org_id, patient_id = seed_org_patient
    client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": patient_id, "vaccine_code": "MMR", "reason": "急性期",
              "contra_type": "temporary", "valid_until": "2020-01-01"},
        headers=admin_headers,
    )
    check = client.get(
        "/api/vaccination/pre-check",
        params={"patient_id": patient_id, "vaccine_code": "MMR"},
        headers=admin_headers,
    )
    assert check.json()["allowed"] is True
    # 过期的不并进拦截项，但也不能悄悄丢掉
    assert len(check.json()["inactive_contraindications"]) == 1


def test_暂时禁忌必须给有效期(client, admin_headers, seed_org_patient):
    _org_id, patient_id = seed_org_patient
    resp = client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": patient_id, "vaccine_code": "HepA", "reason": "发热",
              "contra_type": "temporary"},
        headers=admin_headers,
    )
    assert resp.status_code == 422


def test_长期禁忌解除前始终拦截(client, admin_headers, seed_org_patient):
    org_id, patient_id = seed_org_patient
    client.post(
        "/api/vaccination/contraindications",
        json={"patient_id": patient_id, "vaccine_code": "BCG", "reason": "免疫缺陷"},
        headers=admin_headers,
    )
    resp = client.post(
        "/api/vaccination/records",
        json={"patient_id": patient_id, "vaccine_code": "BCG", "vaccine_name": "卡介苗",
              "dose_no": 1, "vaccinated_date": today_str(), "org_id": org_id},
        headers=admin_headers,
    )
    assert resp.status_code == 409


def test_每个拦人接口都要有放开的路(client, admin_headers):
    """D-1/D-2/D-4 是同一类问题：拦住了却没留放开的路。

    这条把规矩钉在用例里：凡是"登记后会阻断后续业务"的对象，
    平台必须提供解除/停用/移除的接口。
    """
    from app.main import app

    paths = set(app.openapi()["paths"])
    # (拦人的登记接口, 放开的接口)
    pairs = [
        ("/api/appointments/blacklist", "/api/appointments/blacklist/{patient_id}"),
        ("/api/vaccination/contraindications", "/api/vaccination/contraindications/{contra_id}/lift"),
        ("/api/prescriptions/rules", "/api/prescriptions/rules/{drug_code}"),
    ]
    for block, unblock in pairs:
        assert block in paths, block
        assert unblock in paths, f"{block} 拦得住却放不开：缺 {unblock}"


# ---------------------------------------------------------------- 用药规则停用

def test_停用的用药规则不再参与审方(client, admin_headers, seed_org_patient):
    org_id, patient_id = seed_org_patient
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "TESTX", "max_daily_dose": 10, "dose_unit": "mg"},
        headers=admin_headers,
    )
    over = {"patient_id": patient_id, "org_id": org_id, "diagnosis_name": "感冒",
            "items": [{"drug_code": "TESTX", "drug_name": "测试药", "daily_dose": 999, "days": 1}]}
    first = client.post("/api/prescriptions", json=over, headers=admin_headers)
    assert first.json()["status"] == "pending_review"

    assert client.delete("/api/prescriptions/rules/TESTX", headers=admin_headers).status_code == 200
    second = client.post("/api/prescriptions", json=over, headers=admin_headers)
    # 规则停用后按"未维护"处理，不再因超量转药师审
    assert second.json()["status"] == "auto_passed"
    # 停用不删行：仍能查得到
    listed = client.get(
        "/api/prescriptions/rules", params={"include_inactive": True}, headers=admin_headers
    ).json()
    assert any(r["drug_code"] == "TESTX" and r["active"] is False for r in listed)


# ---------------------------------------------------------------- D-1 单事务

def test_排班中断不留下不可恢复的死结(client, admin_headers, seed_org_patient, monkeypatch):
    """D-1：排班记录、申请状态、患者通知必须同进同出。

    原先分两次 commit：排班已落库、申请状态却停在 approved，重排被唯一约束
    挡回 409，填术中记录又要求 scheduled——这台手术只能改数据库才能救。
    这里让第二段（发通知）抛异常，断言库里什么都没留下，且重排能成功。
    """
    from app.routers import surgery as surgery_router

    org_id, patient_id = seed_org_patient
    room = client.post(
        "/api/surgery/rooms", json={"org_id": org_id, "name": "阶段九手术间"}, headers=admin_headers
    ).json()
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org_id, "name": "阶段九外科病区"},
        headers=admin_headers,
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "S9-01"},
        headers=admin_headers,
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient_id, "ward_id": ward["id"], "bed_id": bed["id"],
              "doctor_name": "外科医生", "diagnosis_name": "急性阑尾炎"},
        headers=admin_headers,
    )
    assert admission.status_code == 201, admission.text
    # 医师提申请、管理层审批（申请人不得自批，故用两个账号）
    client.post(
        "/api/users",
        json={"username": "s9_doc", "password": "passw0rd1", "role": "doctor", "org_id": org_id},
        headers=admin_headers,
    )
    doc = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "s9_doc", "password": "passw0rd1"}
        ).json()["access_token"]
    }
    req = client.post(
        "/api/surgery/requests",
        json={"admission_id": admission.json()["id"], "surgery_name": "阑尾切除术"},
        headers=doc,
    ).json()
    client.post(f"/api/surgery/requests/{req['id']}/approve", json={"approved": True},
                headers=admin_headers)

    def boom(*args, **kwargs):
        raise RuntimeError("消息投递失败")

    monkeypatch.setattr(surgery_router, "notify_patient", boom)
    with pytest.raises(RuntimeError):
        client.post(
            f"/api/surgery/requests/{req['id']}/schedule",
            json={"room_id": room["id"], "scheduled_date": "2026-09-01",
                  "start_time": "09:00", "end_time": "11:00"},
            headers=admin_headers,
        )
    monkeypatch.undo()

    # 中断后：申请仍可排班，排班记录没有半吊子地留在库里
    retry = client.post(
        f"/api/surgery/requests/{req['id']}/schedule",
        json={"room_id": room["id"], "scheduled_date": "2026-09-01",
              "start_time": "09:00", "end_time": "11:00"},
        headers=admin_headers,
    )
    assert retry.status_code == 201, retry.text
    after = client.get("/api/surgery/requests", params={"admission_id": admission.json()["id"]},
                       headers=admin_headers).json()
    assert after[0]["status"] == "scheduled"


# ---------------------------------------------------------------- D-2 并发建池

def test_并发建全域基金池只会成功一个(client, admin_headers):
    """D-2：唯一约束含可空列时对 NULL 不生效，实测 6 线程并发建出过 2 个池。

    应用层查重是 check-then-act，中间有竞态窗口；兜底必须在数据库，
    故加了部分唯一索引 `uq_fund_pool_global`。
    """
    import threading

    client.post(
        "/api/users",
        json={"username": "s9_dir", "password": "passw0rd1", "role": "director"},
        headers=admin_headers,
    )
    director = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "s9_dir", "password": "passw0rd1"}
        ).json()["access_token"]
    }
    codes: list[int] = []
    lock = threading.Lock()

    def create():
        resp = client.post(
            "/api/fund/pools",
            json={"year": 2031, "insurance_type": "resident", "total_amount": 1000000},
            headers=director,
        )
        with lock:
            codes.append(resp.status_code)

    threads = [threading.Thread(target=create) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert codes.count(201) == 1, f"应只建出一个池，实际响应码 {codes}"
    pools = client.get("/api/fund/pools", params={"year": 2031}, headers=admin_headers).json()
    assert len([p for p in pools if p["org_group_id"] is None]) == 1


# ---------------------------------------------------------------- P0-1 N+1

def _count_queries(fn):
    from sqlalchemy import event

    from app.database import engine

    counter = {"n": 0}

    def listener(*args, **kwargs):
        counter["n"] += 1

    event.listen(engine, "before_cursor_execute", listener)
    try:
        fn()
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    return counter["n"]


def test_专病统计的查询数不随入组数增长(client, admin_headers, seed_org_patient):
    """P0-1：原先循环里逐条调 `_completion`，实测 50 条入组 → 103 次 SQL（2N+3）。"""
    org_id, _patient_id = seed_org_patient
    program = client.post(
        "/api/disease-programs",
        json={"code": "S9DM", "name": "阶段九专病", "org_id": org_id,
              "path_nodes": [{"key": "eval", "name": "首次评估", "required": True},
                             {"key": "review", "name": "复查", "required": True}]},
        headers=admin_headers,
    )
    assert program.status_code == 201, program.text
    pid = program.json()["id"]

    def enroll(n: int):
        for i in range(n):
            patient = client.post(
                "/api/patients",
                json={"name": f"专病{i}", "id_card": f"32000019900101{i:04d}"},
                headers=admin_headers,
            ).json()
            client.post(
                f"/api/disease-programs/{pid}/enrollments",
                json={"patient_id": patient["id"], "org_id": org_id},
                headers=admin_headers,
            )

    enroll(3)
    before = _count_queries(
        lambda: client.get(f"/api/disease-programs/{pid}/stats", headers=admin_headers)
    )
    enroll(12)
    after = _count_queries(
        lambda: client.get(f"/api/disease-programs/{pid}/stats", headers=admin_headers)
    )
    assert after == before, f"查询数随入组数增长：{before} -> {after}"


def test_下沉统计的查询数不随派驻数增长(client, admin_headers, seed_org_patient):
    """P1-2：原先 Employee 与 Secondment 整表进内存再在 Python 里筛。"""
    org_id, _patient_id = seed_org_patient
    target = client.post(
        "/api/organizations",
        json={"name": "阶段九受援卫生院", "org_type": "township", "level": "township"},
        headers=admin_headers,
    ).json()

    def dispatch(n: int):
        for i in range(n):
            emp = client.post(
                "/api/mgmt/employees",
                json={"org_id": org_id, "name": f"下沉医师{i}", "title": "主治医师"},
                headers=admin_headers,
            ).json()
            client.post(
                "/api/staffing/secondments",
                json={"employee_id": emp["id"], "from_org_id": org_id,
                      "to_org_id": target["id"], "start_date": "2026-01-01",
                      "assignment_type": "long_term"},
                headers=admin_headers,
            )

    dispatch(2)
    before = _count_queries(
        lambda: client.get("/api/staffing/dispatch-stats", params={"year": 2026},
                           headers=admin_headers)
    )
    dispatch(10)
    after = _count_queries(
        lambda: client.get("/api/staffing/dispatch-stats", params={"year": 2026},
                           headers=admin_headers)
    )
    assert after == before, f"查询数随派驻数增长：{before} -> {after}"
