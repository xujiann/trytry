"""待办中心各节的计数不再截在 100 条上（P1-85）。

`GET /api/todos` 的五节（待药师审处方、待诊断申请、缺药预警、未闭环危急值、待确认危急值）都是
`.limit(100).all()` 取行、再 `count = len(rows)`，`total` 是这几个截断计数之和。于是：

- 药房压着 250 张待审处方，铃铛下拉里写「待药师审处方（100）」；
- 医生移动端顶栏「待办 N」、各节角标同样被截——积压越严重，显示得越像「还好」；
- 与「就这么多」长得一模一样。

P2-8 那一族「计数算在截断样本上」的又一例（转诊超时预警在第四批修过同一个形状）。修法照那一例：
计数走 `count()`，预览列表保留 100 条上限（网页只显示每节前 5 条、移动端前 20 条，预览就是预览）。
可见性口径一概不动：审方按设计跨机构（集中审方），待诊断申请随「谁算共享中心」待裁定，
医生的待确认危急值照旧只列本机构申请单上的（P0-40）。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import DrugStock, ExamReport, ExamRequest, Organization, Patient, Prescription, User

N = 105  # 超过预览上限 100


def _login(client, username, password="pw123456"):
    token = client.post("/api/auth/login", json={"username": username, "password": password}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _sections(client, headers):
    r = client.get("/api/todos", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    return body, {item["type"]: item for item in body["items"]}


@pytest.fixture(scope="module")
def world(client, admin):
    with SessionLocal() as db:
        org = Organization(name="待办计数卫生院", org_type="township", level="township")
        db.add(org)
        db.flush()
        creator = db.query(User).filter(User.username == "admin").one().id
        patient = Patient(ehc_no="EHC-TODO-1", name="待办计数患者", id_card="320000199202021234",
                          gender="女", birth_date="1992-02-02")
        db.add(patient)
        db.commit()
        org_id, pid = org.id, patient.id
    r = client.post("/api/users", json={"username": "p185_doc", "password": "pw123456", "full_name": "p185_doc",
                                        "role": "doctor", "org_id": org_id}, headers=admin)
    assert r.status_code == 201, r.text
    return {"org": org_id, "patient": pid, "creator": creator, "doc": _login(client, "p185_doc")}


def test_特征化_不超过预览上限时计数就是列表长度(client, admin, world):
    for headers in (admin, world["doc"]):
        body, sections = _sections(client, headers)
        for item in sections.values():
            assert item["count"] == len(item["list"]) <= 100
        assert body["total"] == sum(item["count"] for item in sections.values())


@pytest.fixture(scope="module")
def bulk(client, admin, world):
    """每一节都灌 `N` 条：待审处方、待诊断申请、低于阈值的库存、未确认的危急值（挂在本院申请单上）。"""
    before_admin = {k: v["count"] for k, v in _sections(client, admin)[1].items()}
    before_doc = {k: v["count"] for k, v in _sections(client, world["doc"])[1].items()}
    org, pid, creator = world["org"], world["patient"], world["creator"]
    with SessionLocal() as db:
        db.execute(insert(Prescription), [
            {"patient_id": pid, "org_id": org, "created_by": creator, "status": "pending_review",
             "diagnosis_name": f"待办计数{i}"} for i in range(N)])
        db.execute(insert(DrugStock), [
            {"org_id": org, "drug_code": f"TODO{i:03d}", "drug_name": f"待办计数药{i}", "quantity": 1, "threshold": 10}
            for i in range(N)])
        db.execute(insert(ExamRequest), [
            {"patient_id": pid, "from_org_id": org, "center_type": "lab", "item_code": f"T{i}",
             "item_name": f"待办计数检验{i}", "status": "pending", "created_by": creator} for i in range(N)])
        db.execute(insert(ExamRequest), [
            {"patient_id": pid, "from_org_id": org, "center_type": "lab", "item_code": f"C{i}",
             "item_name": f"待办计数危急{i}", "status": "reported", "created_by": creator} for i in range(N)])
        critical_requests = [rid for (rid,) in db.query(ExamRequest.id).filter(ExamRequest.item_code.like("C%"),
                                                                                ExamRequest.from_org_id == org)]
        db.execute(insert(ExamReport), [
            {"request_id": rid, "conclusion": "待办计数：血钾 6.8", "critical": True, "critical_status": "notified"}
            for rid in critical_requests])
        db.commit()
    return {"admin": before_admin, "doc": before_doc}


@pytest.mark.parametrize("kind", ["prescription_review", "exam_diagnosis", "stock_shortage", "critical_report"])
def test_管理员各节计数是全部而不是预览那一页(client, admin, world, bulk, kind):
    body, sections = _sections(client, admin)
    item = sections[kind]
    assert item["count"] == bulk["admin"][kind] + N, f"{kind} 的计数被截在预览上限上了"
    assert len(item["list"]) == 100, "预览列表照旧最多 100 条"
    assert body["total"] == sum(i["count"] for i in sections.values())


def test_医生的待确认危急值计数也是全部(client, world, bulk):
    body, sections = _sections(client, world["doc"])
    item = sections["critical_ack"]
    assert item["count"] == bulk["doc"]["critical_ack"] + N, "待确认危急值计数被截在预览上限上了"
    assert len(item["list"]) == 100
    assert body["total"] == sum(i["count"] for i in sections.values())
