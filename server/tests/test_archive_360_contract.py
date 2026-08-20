"""`GET /api/archive/{ehc_no}` 的响应契约（接口标准棘轮下一块）。

这是全平台聚合度最高的一个接口，一次调用拿到一个人的就诊、检查、慢病、处方、
结算、体检。给它补 `response_model` 的前提是**响应字节一个也不能变**
（CLAUDE.md §11：治理不得改响应字节）——所以先有这份特征化网：
逐段钉住键集合与取值类型，再加契约，加完这份网必须照样绿。
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
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    """一个有全套记录的患者：就诊 / 检查报告 / 慢病 / 处方 / 结算 / 体检。"""
    org = client.post(
        "/api/organizations",
        json={"name": "全景县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "arc_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"]},
        headers=admin,
    )
    doc = client.post(
        "/api/auth/login", json={"username": "arc_doc", "password": "pass123456"}
    ).json()
    doc = {"Authorization": f"Bearer {doc['access_token']}"}
    patient = client.post(
        "/api/patients",
        json={"name": "全景患者", "id_card": "330281199101016006", "gender": "男",
              "birth_date": "1991-01-01"},
        headers=admin,
    ).json()

    client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"],
              "encounter_type": "outpatient", "diagnosis_name": "高血压"},
        headers=doc,
    )
    req = client.post(
        "/api/exams",
        json={"patient_id": patient["id"], "from_org_id": org["id"],
              "center_type": "imaging", "item_code": "CT", "item_name": "胸部CT"},
        headers=doc,
    ).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=doc)
    client.post(
        f"/api/exams/{req['id']}/report",
        json={"conclusion": "未见异常", "critical": False}, headers=doc,
    )
    client.post(
        "/api/chronic",
        json={"patient_id": patient["id"], "disease": "hypertension",
              "managed_by_org_id": org["id"]},
        headers=doc,
    )
    client.post(
        "/api/prescriptions",
        json={"patient_id": patient["id"], "org_id": org["id"], "diagnosis_name": "高血压",
              "items": [{"drug_code": "D1", "drug_name": "氨氯地平",
                         "daily_dose": 5.0, "days": 30}]},
        headers=doc,
    )
    # 体检与结算这两段也要有数据——空列表什么都钉不住，
    # 建模写错了（少字段/错字段）也不会有人报。
    client.post(
        "/api/users",
        json={"username": "arc_op", "password": "pass123456", "role": "operator",
              "org_id": org["id"]},
        headers=admin,
    )
    op = client.post(
        "/api/auth/login", json={"username": "arc_op", "password": "pass123456"}
    ).json()
    op = {"Authorization": f"Bearer {op['access_token']}"}

    checkup = client.post(
        "/api/checkups",
        json={"patient_id": patient["id"], "org_id": org["id"],
              "package_name": "常规体检", "exam_date": "2026-03-01",
              "abnormal_items": "血压偏高"},
        headers=doc,
    )
    assert checkup.status_code == 201, checkup.text

    encounter = client.post(
        "/api/encounters",
        json={"patient_id": patient["id"], "org_id": org["id"],
              "encounter_type": "outpatient", "diagnosis_name": "复诊"},
        headers=doc,
    ).json()
    # 结算要有未结清的费用明细才做得出来——先建收费项目、再记一笔明细
    item = client.post(
        "/api/billing/charge-items",
        json={"code": "ARC001", "name": "全景诊查费", "category": "treatment", "price": 50.0},
        headers=admin,
    )
    assert item.status_code == 201, item.text
    detail = client.post(
        "/api/billing/details",
        json={"patient_id": patient["id"], "encounter_id": encounter["id"],
              "item_code": "ARC001", "quantity": 2},
        headers=op,
    )
    assert detail.status_code == 201, detail.text
    settlement = client.post(
        "/api/billing/settlements",
        json={"bill_type": "outpatient", "encounter_id": encounter["id"],
              "insurance_pay": 30.0},
        headers=op,
    )
    assert settlement.status_code == 201, settlement.text

    return {"patient": patient, "org": org, "doc": doc}


#: 顶层键与它们的类型——逐字钉住。
TOP_LEVEL = {
    "section_limit": int,
    "has_more": dict,
    "patient": dict,
    "encounters": list,
    "exam_reports": list,
    "chronic_diseases": list,
    "prescriptions": list,
    "settlements": list,
    "physical_exams": list,
}

SECTION_KEYS = {
    "has_more": {"encounters", "exam_reports", "prescriptions", "checkups", "settlements"},
    "patient": {"ehc_no", "name", "gender", "birth_date"},
}

ROW_KEYS = {
    "encounters": {"id", "org_id", "encounter_type", "diagnosis_name", "summary"},
    "exam_reports": {"id", "request_id", "conclusion", "critical"},
    "chronic_diseases": {"id", "disease", "level", "next_due"},
    "prescriptions": {"id", "diagnosis_name", "status"},
    "settlements": {"id", "bill_type", "total_amount", "insurance_pay", "self_pay", "created_at"},
    "physical_exams": {"id", "exam_date", "package_name", "has_abnormal", "abnormal_items"},
}


@pytest.fixture(scope="module")
def archive(client, world):
    resp = client.get(
        f"/api/archive/{world['patient']['ehc_no']}", headers=world["doc"]
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_顶层键集合与类型不变(archive):
    assert set(archive) == set(TOP_LEVEL), "顶层键集合变了——这是破坏性变更"
    for key, kind in TOP_LEVEL.items():
        assert isinstance(archive[key], kind), f"{key} 的类型变了"


@pytest.mark.parametrize("section", sorted(SECTION_KEYS))
def test_子对象键集合不变(archive, section):
    assert set(archive[section]) == SECTION_KEYS[section]


@pytest.mark.parametrize("section", sorted(ROW_KEYS))
def test_列表行键集合不变(archive, section):
    rows = archive[section]
    if not rows:
        pytest.skip(f"{section} 本轮没有数据，键集合由有数据的段覆盖")
    for row in rows:
        assert set(row) == ROW_KEYS[section], f"{section} 的行键集合变了"


def test_有数据的段确实有数据(archive):
    """空列表什么都钉不住——保证这份网真的在检查东西。"""
    for section in ROW_KEYS:
        assert archive[section], f"{section} 是空的，这份特征化网没覆盖到它"


def test_每一段都有数据_这份网才算覆盖全(archive):
    """六段都要有数据，否则某段的建模写错了也没人报。"""
    for section in ROW_KEYS:
        assert archive[section], f"{section} 是空的"


def test_has_more在数据不多时全为False(archive):
    assert archive["has_more"] == dict.fromkeys(SECTION_KEYS["has_more"], False)


def test_patient段取值正确(archive, world):
    assert archive["patient"]["name"] == "全景患者"
    assert archive["patient"]["gender"] == "男"
    assert archive["patient"]["birth_date"] == "1991-01-01"
    assert archive["patient"]["ehc_no"] == world["patient"]["ehc_no"]
