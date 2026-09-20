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
def h(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest.fixture(scope="module")
def base(client, h):
    org = client.post("/api/organizations", json={"name": "县医院", "org_type": "lead_hospital", "level": "county"}, headers=h).json()
    patient = client.post("/api/patients", json={"name": "周强", "id_card": "320981199103034444"}, headers=h).json()
    return {"org": org, "patient": patient}


def test_report_template_and_amend(client, h, base):
    assert client.post("/api/exams/templates", json={"center_type": "imaging", "name": "胸部DR正常模板", "content": "两肺纹理清晰"}, headers=h).status_code == 201
    assert len(client.get("/api/exams/templates?center_type=imaging", headers=h).json()) == 1
    req = client.post("/api/exams", json={"patient_id": base["patient"]["id"], "from_org_id": base["org"]["id"], "center_type": "imaging", "item_code": "DR", "item_name": "胸部DR"}, headers=h).json()
    client.post(f"/api/exams/{req['id']}/claim", headers=h)
    rep = client.post(f"/api/exams/{req['id']}/report", json={"conclusion": "初步结论"}, headers=h).json()
    amended = client.patch(f"/api/exams/reports/{rep['id']}", json={"conclusion": "修订：未见异常"}, headers=h).json()
    assert amended["conclusion"].startswith("修订")


def test_cssd_request_fulfill(client, h, base):
    req = client.post("/api/cssd/requests", json={"org_id": base["org"]["id"], "item_name": "换药包", "quantity": 5}, headers=h).json()
    batch = client.post("/api/cssd/batches", json={"batch_no": "B-01", "center_org_id": base["org"]["id"], "item_name": "换药包", "quantity": 20}, headers=h).json()
    # 未灭菌批次不可发放
    assert client.post(f"/api/cssd/requests/{req['id']}/fulfill?batch_id={batch['id']}", headers=h).status_code == 409
    client.post(f"/api/cssd/batches/{batch['id']}/advance", headers=h)
    done = client.post(f"/api/cssd/requests/{req['id']}/fulfill?batch_id={batch['id']}", headers=h).json()
    assert done["status"] == "fulfilled"


def test_expert_blacklist_survey_triage(client, h, base):
    assert client.post("/api/consultations/experts", json={"name": "张主任", "org_id": base["org"]["id"], "specialty": "心内科"}, headers=h).status_code == 201
    assert len(client.get("/api/consultations/experts?available=true", headers=h).json()) == 1

    slot = client.post("/api/appointments/slots", json={"org_id": base["org"]["id"], "resource_type": "outpatient", "resource_name": "门诊", "slot_date": "2026-09-01", "capacity": 5}, headers=h).json()
    client.post("/api/appointments/blacklist", json={"patient_id": base["patient"]["id"], "reason": "多次爽约"}, headers=h)
    assert client.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": base["patient"]["id"]}, headers=h).status_code == 403
    client.delete(f"/api/appointments/blacklist/{base['patient']['id']}", headers=h)
    assert client.post("/api/appointments", json={"slot_id": slot["id"], "patient_id": base["patient"]["id"]}, headers=h).status_code == 201

    client.post("/api/surveys", json={"target_type": "contract", "target_id": 1, "patient_id": base["patient"]["id"], "score": 5}, headers=h)
    client.post("/api/surveys", json={"target_type": "contract", "target_id": 1, "patient_id": base["patient"]["id"], "score": 4}, headers=h)
    stats = client.get("/api/surveys/stats?target_type=contract", headers=h).json()
    assert stats[0]["avg_score"] == 4.5

    triage = client.post("/api/triage/suggest", json=["胸痛", "心悸"], headers=h).json()
    assert triage["recommendations"][0]["department"] == "心血管内科"
    assert triage["emergency_hint"] is True


def test_health_articles_editorial_list(client, h):
    """编制侧清单要能看见草稿——居民端那条只出 published，看不见草稿就没法发布。

    并且**不能**靠给居民端那条加参数来实现：它免登录，多一个 status 参数
    就等于把未发布的稿子挂到公网上。
    """
    draft = client.post(
        "/api/education/articles",
        json={"title": "流感季防护", "category": "infectious", "content": "戴口罩"},
        headers=h,
    ).json()
    rows = client.get("/api/education/articles", headers=h).json()
    mine = [r for r in rows if r["id"] == draft["id"]]
    assert mine and mine[0]["status"] == "draft", "草稿在编制侧清单里看不见"
    assert mine[0]["title"] == "流感季防护"

    # 免登录那条仍然只出已发布的
    assert all(a["id"] != draft["id"] for a in client.get("/api/portal/health-articles").json())

    client.post(f"/api/education/articles/{draft['id']}/publish", headers=h)
    published = client.get("/api/education/articles?status=published", headers=h).json()
    assert any(r["id"] == draft["id"] for r in published)
    assert all(r["status"] == "published" for r in published)

    # 无宣教编制权限的角色拿不到（含草稿的清单不是谁都能看）
    client.post(
        "/api/users",
        json={"username": "edu_outsider", "password": "pw123456", "role": "pharmacist"},
        headers=h,
    )
    tok = client.post(
        "/api/auth/login", json={"username": "edu_outsider", "password": "pw123456"}
    ).json()["access_token"]
    denied = client.get("/api/education/articles", headers={"Authorization": f"Bearer {tok}"})
    assert denied.status_code == 403


def test_health_articles_portal(client, h):
    a = client.post("/api/education/articles", json={"title": "高血压饮食指南", "category": "chronic", "content": "限盐限油"}, headers=h).json()
    # 未发布不可见。判的是"这一篇不在里面"而不是"整个列表是空的"：
    # 后者的前提是同模块里在它之前没人发布过任何文章，靠的是测试执行顺序——
    # 这个仓库装着 pytest-randomly，换个顺序它就红，而红的原因与它要守的
    # 「草稿不外露」毫无关系。
    assert all(x["id"] != a["id"] for x in client.get("/api/portal/health-articles").json())
    client.post(f"/api/education/articles/{a['id']}/publish", headers=h)
    articles = client.get("/api/portal/health-articles?category=chronic").json()
    assert articles[0]["title"] == "高血压饮食指南"
