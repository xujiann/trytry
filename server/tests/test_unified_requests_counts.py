"""统一申请单的总数、按状态 / 按类型计数覆盖全部命中项；按状态筛在库里筛；互认的检查单算「已完成」（P2-162）。

接口说明写「total/统计口径覆盖全部命中项，items 只返回前 limit 条」。实现却是五类各取最新 limit 条、再在内存里
按类型 / 状态筛、再数：某一类超过 limit 条，total 就封顶；落在最新 limit 条之外的「待处理」按状态筛也看不见。
检查单的 recognized（互认既往结果）不在映射表里，原码进了「按状态」计数，按「已完成」筛也筛不到。
"""
from datetime import datetime, timedelta

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import Consultation, ExamRequest, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2162 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2162 患者", "id_card": "330106196606061621"}).json()["id"]
    base = datetime(2026, 9, 1, 8, 0)
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id

        def exam(status, minutes):
            return ExamRequest(patient_id=patient, from_org_id=org, center_type="lab", item_code="P2162",
                               item_name="血常规", status=status, created_by=operator,
                               created_at=base + timedelta(minutes=minutes))

        # 最早一张待诊断，之后两张已报告、一张互认；再一条会诊
        db.add_all([exam("pending", 0), exam("reported", 10), exam("reported", 20), exam("recognized", 30),
                    Consultation(patient_id=patient, from_org_id=org, to_org_id=org, question="P2162 会诊",
                                 created_by=operator, created_at=base + timedelta(minutes=40))])
        db.commit()
    return {"patient": patient}


def _get(client, admin, world, query=""):
    resp = client.get(f"/api/service-requests?patient_id={world['patient']}{query}", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_计数覆盖全部命中项_不封顶在limit(client, admin, world):
    body = _get(client, admin, world, "&limit=2")
    # 修前 total=3、by_type={exam: 2, …}：每类只取最新 2 条再数
    assert (body["total"], body["returned"], body["truncated"]) == (5, 2, True)
    assert body["by_type"] == {"exam": 4, "consultation": 1}
    assert body["by_status"] == {"pending": 2, "done": 3}
    assert [i["request_type"] for i in body["items"]] == ["consultation", "exam"]   # 仍是合起来最新的 2 条


def test_落在最新limit条之外的待处理按状态筛得到(client, admin, world):
    body = _get(client, admin, world, "&request_type=exam&status=pending&limit=2")
    assert [(i["raw_status"], i["status"]) for i in body["items"]] == [("pending", "pending")]   # 修前 []
    assert (body["total"], body["by_status"], body["by_type"]) == (1, {"pending": 1}, {"exam": 1})


def test_互认的检查单算已完成(client, admin, world):
    body = _get(client, admin, world)
    recognized = [i for i in body["items"] if i["raw_status"] == "recognized"]
    assert [i["status"] for i in recognized] == ["done"]   # 修前 "recognized"
    assert "recognized" not in body["by_status"]
    done = _get(client, admin, world, "&status=done")
    assert sorted(i["raw_status"] for i in done["items"]) == ["recognized", "reported", "reported"]


def test_筛不存在的类型或状态照旧空(client, admin, world):
    for query in ("&request_type=nosuch", "&status=nosuch", "&request_type=exam&status=booked"):
        body = _get(client, admin, world, query)
        assert (body["total"], body["items"], body["by_status"], body["by_type"]) == (0, [], {}, {}), query
