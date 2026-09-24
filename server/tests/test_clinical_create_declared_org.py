"""诊疗行为不得以别家机构名义新建（P0-35 第一批）。

建就诊、开处方、开检查申请、开代煎单，四个端点都由请求体声明「这张单子是哪家机构开的」
（`org_id` / `from_org_id`），端点只查这家机构存不存在、不查调用方是不是这家的。2026-09-24
实测：乙院医生以甲院名义建就诊、开处方、开检查、开代煎，四条全 201——

- 就诊记录进甲院的门诊量，还凭空给甲院造了一条「就诊过」的服务关系（可见性判定首先看它）；
- 处方挂在甲院名下进审方与发药，检查单与代煎单进甲院的待办与费用。

照 `visibility.assert_org_writable` 的既定口径：非全域角色只能以本机构名义写，「写永远不能宽」。
`db.get(Organization, …)` 只查存在不查归属，这正是 `tests/test_body_declared_org_write_guard.py`
量出来的那一族；本批四条修完从候选里划掉。
"""
import itertools

import pytest

from app.database import SessionLocal
from app.models import Encounter, ExamRequest, Prescription, TcmDispenseOrder


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def clinic_world(client):
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "声明机构甲院"), ("b", "声明机构乙院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
        r = client.post("/api/users",
                        json={"username": f"p035a_doc_{key}", "password": "pw123456", "full_name": f"{name}医生",
                              "role": "doctor", "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    patient = client.post("/api/patients", json={"name": "声明机构患者", "id_card": "320000198909096671"},
                          headers=admin).json()
    return {"orgs": orgs, "patient_id": patient["id"],
            "h": {k: _login(client, f"p035a_doc_{k}") for k in orgs}}


_seq = itertools.count(1)

# (接口, 请求体里声明机构的字段, 落库的表, 表上的机构列, 其余请求体)
CASES = [
    ("/api/encounters", "org_id", Encounter, "org_id",
     lambda: {"encounter_type": "outpatient", "diagnosis_name": "上呼吸道感染"}),
    ("/api/prescriptions", "org_id", Prescription, "org_id",
     lambda: {"diagnosis_name": "高血压", "items": [{"drug_code": "D-AMLO", "drug_name": "氨氯地平",
                                                    "daily_dose": 5, "days": 7}]}),
    ("/api/exams", "from_org_id", ExamRequest, "from_org_id",
     lambda: {"center_type": "lab", "item_code": f"P035-{next(_seq)}", "item_name": "血常规"}),
    ("/api/tcm/dispense-orders", "from_org_id", TcmDispenseOrder, "from_org_id",
     lambda: {"herbs": "黄芪 30g", "doses": 3}),
]
IDS = ["就诊", "处方", "检查申请", "代煎单"]


def _count(model, col, org_id, patient_id) -> int:
    db = SessionLocal()
    try:
        return db.query(model).filter(getattr(model, col) == org_id, model.patient_id == patient_id).count()
    finally:
        db.close()


@pytest.mark.parametrize("url, field, model, col, extra", CASES, ids=IDS)
def test_不得以别家机构名义新建(client, clinic_world, url, field, model, col, extra):
    a, pid = clinic_world["orgs"]["a"], clinic_world["patient_id"]
    before = _count(model, col, a, pid)
    r = client.post(url, json={"patient_id": pid, field: a, **extra()}, headers=clinic_world["h"]["b"])
    assert r.status_code == 403, (url, r.text)
    assert "机构名义" in r.json()["detail"], f"只认机构守卫的 403（角色 403 不算数）：{r.text}"
    assert _count(model, col, a, pid) == before, f"{url} 被拒却落了库"


@pytest.mark.parametrize("url, field, model, col, extra", CASES, ids=IDS)
def test_以本机构名义照常新建(client, clinic_world, url, field, model, col, extra):
    a, pid = clinic_world["orgs"]["a"], clinic_world["patient_id"]
    before = _count(model, col, a, pid)
    r = client.post(url, json={"patient_id": pid, field: a, **extra()}, headers=clinic_world["h"]["a"])
    assert r.status_code == 201, (url, r.text)
    assert _count(model, col, a, pid) == before + 1
