"""没领取、直接出报告的检查单不记领取机构：中心医师写完报告打不开自己写的那份（P2-257）。

共享诊断中心与患者的服务关系靠 `exam_requests.claimed_org_id` 成立（模型列注释：原先中心医师「写完报告却打不开自己写的
那份报告」，加这一列就是为了堵它）。领取（`/claim`）在同一条原子 UPDATE 里记下它；可出报告接口也收待领取（pending）的
单子——医生移动端待领取的卡片上就摆着「出报告」——这条路不记领取人与中心机构，于是中心医师直接出的报告，打印、挂影像
附件一律 403。修法：从待领取直接出报告时与领取同一句记下领取人与中心机构。
"""
import pytest
from fastapi.testclient import TestClient

from app.database import SessionLocal
from app.main import app
from app.models import ExamRequest
from conftest import login, reset_database


def _claimed(request_id):
    with SessionLocal() as db:
        row = db.get(ExamRequest, request_id)
        return row.claimed_org_id, row.claimed_by


@pytest.fixture()
def client():
    """函数级：跨机构 403 断言与共享库的数据有顺序耦合（同 test_print_attachment_visibility）。"""
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def world(client):
    admin = login(client, "admin", "admin123")
    out = {"admin": admin}
    for tag in ("甲", "乙", "丙"):
        org = client.post("/api/organizations", headers=admin, json={
            "name": f"P2257 {tag}县医院", "org_type": "lead_hospital", "level": "county"}).json()
        client.post("/api/users", headers=admin, json={
            "username": f"p2257_{tag}", "password": "pass123456", "role": "doctor", "org_id": org["id"]})
        out[tag] = {"org": org["id"], "doc": login(client, f"p2257_{tag}", "pass123456")}
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2257 患者", "id_card": "330281199202022257"}).json()
    client.post("/api/encounters", headers=out["甲"]["doc"], json={
        "patient_id": patient["id"], "org_id": out["甲"]["org"], "visit_type": "outpatient"})
    out["patient"] = patient["id"]
    return out


def test_中心医师从待领取直接出报告_打得开自己写的那份(client, world):
    req = client.post("/api/exams", headers=world["甲"]["doc"], json={
        "patient_id": world["patient"], "from_org_id": world["甲"]["org"],
        "center_type": "imaging", "item_code": "CT", "item_name": "胸部CT"}).json()
    assert req["status"] == "pending"
    report = client.post(f"/api/exams/{req['id']}/report", headers=world["乙"]["doc"],
                         json={"conclusion": "未见异常", "critical": False})
    assert report.status_code == 201, report.text
    rid = report.json()["id"]
    printed = client.get(f"/api/print/exam-reports/{rid}", headers=world["乙"]["doc"])
    assert printed.status_code == 200, printed.text   # 修前 403：中心医师打不开自己写的报告
    assert _claimed(req["id"]) == (world["乙"]["org"], "p2257_乙")
    # 与本单无关的第三方照旧进不来
    assert client.get(f"/api/print/exam-reports/{rid}", headers=world["丙"]["doc"]).status_code == 403


def test_先领取再出报告的_领取人照旧不被改写(client, world):
    req = client.post("/api/exams", headers=world["甲"]["doc"], json={
        "patient_id": world["patient"], "from_org_id": world["甲"]["org"],
        "center_type": "imaging", "item_code": "MR", "item_name": "头颅MR"}).json()
    assert client.post(f"/api/exams/{req['id']}/claim", headers=world["乙"]["doc"]).status_code == 200
    report = client.post(f"/api/exams/{req['id']}/report", headers=world["admin"],
                         json={"conclusion": "未见异常", "critical": False})
    assert report.status_code == 201, report.text
    assert _claimed(req["id"]) == (world["乙"]["org"], "p2257_乙")   # admin 出的报告不改写领取人
