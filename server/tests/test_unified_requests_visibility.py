"""统一申请单中心不带患者号时按调用方收口（P0-33）。

`GET /api/service-requests` 一次把预约、检查、会诊、用血、手术五类单据聚合出来。函数里写着
「聚合视图更要守：它是最省事的一个越权入口」，守卫却只包在 `if patient_id` 里——不带患者号
（页面默认就不带）时五类单据按全域吐出来，带患者姓名。2026-09-24 动态探针实测：一家与任何
患者都没有关系的新机构，四个角色都拿到了别家患者的在办单据。

照各单据自己清单的口径（如预约清单的 `scope_patient_list`）：只见本机构服务过的患者；全域角色
不过滤。本机构经手的单据本身就构成服务关系——下面专门钉一条"只靠这张单子和患者有关系"的情形。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import ExamRequest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def ur_world(client):
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "统一申请甲院"), ("b", "统一申请乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p033_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    seq = itertools.count(1)

    def new_patient() -> int:
        n = next(seq)
        return client.post("/api/patients",
                           json={"name": f"统一申请患者{n}", "id_card": f"3200001985050566{70 + n}"},
                           headers=admin).json()["id"]

    def new_exam(patient_id: int) -> int:
        db = SessionLocal()
        try:
            row = ExamRequest(patient_id=patient_id, from_org_id=orgs["a"], center_type="lab",
                              item_code=f"UR{next(seq)}", item_name="血常规", created_by=1)
            db.add(row)
            db.commit()
            return row.id
        finally:
            db.close()

    return {"admin": admin, "doc_a": _login(client, "p033_doc_a"), "doc_b": _login(client, "p033_doc_b"),
            "new_patient": new_patient, "new_exam": new_exam}


def _exam_ids(client, headers, query=""):
    r = client.get(f"/api/service-requests?limit=500{query}", headers=headers)
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"] if i["request_type"] == "exam"}


def test_不带患者号时无关机构看不到别家患者的单据(client, ur_world):
    """甲院开的检查单只靠这张单子与患者有关系（没有就诊记录）：甲院照样看得到。"""
    pid = ur_world["new_patient"]()
    eid = ur_world["new_exam"](pid)
    assert eid not in _exam_ids(client, ur_world["doc_b"]), "乙院看得见甲院患者的在办单据（带姓名）"
    assert eid in _exam_ids(client, ur_world["doc_a"])
    assert eid in _exam_ids(client, ur_world["admin"])


def test_按患者号查仍然判定(client, ur_world):
    pid = ur_world["new_patient"]()
    ur_world["new_exam"](pid)
    r = client.get(f"/api/service-requests?patient_id={pid}", headers=ur_world["doc_b"])
    assert r.status_code == 403, r.text
    assert _exam_ids(client, ur_world["doc_a"], f"&patient_id={pid}")
