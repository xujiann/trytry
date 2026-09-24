"""报告修订史与危急值处置轨迹两个读接口的归属校验与留痕（P0-21）。

`GET /api/exams/reports/{id}/revisions` 与 `GET /api/exams/reports/{id}/critical-actions`
原先连调用方身份都不收：乙院医生（与该患者毫无业务关系）按报告号就能读甲院报告的历次前结论、
前所见与危急值处置轨迹（轨迹文字里带着结论原文）——2026-09-24 实测 200。

同一个文件的写侧早就收了：`revise_report` 的注释写着"`ExamReport` 自己不带 org_id/patient_id，
归属隔一跳在 `exam_requests.patient_id` 上，所以走患者可见性"——读侧没跟，又是 P0-19 / P0-20
那个"写收了、读没收"的形状（P1-69 隔跳名单里的两条）。

两个方向都钉：无关机构 403；有业务关系的照常 200 并留下 `AccessLog`；报告号不存在照旧 404。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def exam_world(client):
    """甲院开单、出危急值报告并修订一次；乙院一名医师与该患者毫无关系。"""
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations",
                    json={"name": "报告轨迹甲院", "org_type": "lead_hospital", "level": "county"},
                    headers=admin).json()
    b = client.post("/api/organizations",
                    json={"name": "报告轨迹乙院", "org_type": "township", "level": "township"},
                    headers=admin).json()
    for uname, org in (("ert_doc_a", a), ("ert_doc_b", b)):
        client.post("/api/users",
                    json={"username": uname, "password": "pw123456", "full_name": uname,
                          "role": "doctor", "org_id": org["id"]},
                    headers=admin)
    doc_a, doc_b = _login(client, "ert_doc_a"), _login(client, "ert_doc_b")
    patient = client.post("/api/patients",
                          json={"name": "报告轨迹患者", "id_card": "320000199404045670"},
                          headers=admin).json()
    client.post("/api/encounters",
                json={"patient_id": patient["id"], "org_id": a["id"], "encounter_type": "outpatient"},
                headers=doc_a)
    req = client.post("/api/exams",
                      json={"patient_id": patient["id"], "from_org_id": a["id"], "center_type": "lab",
                            "item_code": "ERT-K", "item_name": "血钾"},
                      headers=doc_a)
    assert req.status_code == 201, req.text
    report = client.post(f"/api/exams/{req.json()['id']}/report",
                         json={"conclusion": "血钾 7.1 危急", "critical": True, "reported_by": "检验科"},
                         headers=doc_a)
    assert report.status_code in (200, 201), report.text
    rid = report.json()["id"]
    amended = client.patch(f"/api/exams/reports/{rid}",
                           json={"conclusion": "复核后仍危急：血钾 6.9", "reason": "数值复核"},
                           headers=doc_a)
    assert amended.status_code == 200, amended.text
    return {"doc_a": doc_a, "doc_b": doc_b, "patient_id": patient["id"], "report_id": rid}


READS = {
    "revisions": ("revisions", "exam_report_revision"),
    "critical_actions": ("critical-actions", "exam_critical_action"),
}


@pytest.mark.parametrize("name", sorted(READS))
def test_无关机构按报告号读修订史与危急值轨迹403(client, exam_world, name):
    path, _ = READS[name]
    r = client.get(f"/api/exams/reports/{exam_world['report_id']}/{path}", headers=exam_world["doc_b"])
    assert r.status_code == 403, (name, r.status_code, r.text)


@pytest.mark.parametrize("name", sorted(READS))
def test_有业务关系的照常能读且留痕(client, exam_world, name):
    path, resource = READS[name]

    def count():
        db = SessionLocal()
        try:
            return db.query(AccessLog).filter(
                AccessLog.patient_id == exam_world["patient_id"], AccessLog.resource == resource
            ).count()
        finally:
            db.close()

    before = count()
    r = client.get(f"/api/exams/reports/{exam_world['report_id']}/{path}", headers=exam_world["doc_a"])
    assert r.status_code == 200, (name, r.text)
    assert r.json(), name  # 修订一次、危急值有轨迹：真读到了东西
    assert count() == before + 1, f"{name} 放行了却没留痕"


@pytest.mark.parametrize("name", sorted(READS))
def test_报告号不存在照旧404(client, exam_world, name):
    path, _ = READS[name]
    assert client.get(f"/api/exams/reports/987654/{path}", headers=exam_world["doc_b"]).status_code == 404
