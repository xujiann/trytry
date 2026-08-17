"""旧 QC 引擎跨机构收口回归：非全域账号只看本机构范围内的质控违规明细。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


def test_run_checks_scoped_by_visible_org(client, admin):
    a = client.post("/api/organizations", json={"name": "质控甲院", "org_type": "township", "level": "township"}, headers=admin).json()
    b = client.post("/api/organizations", json={"name": "质控乙院", "org_type": "township", "level": "township"}, headers=admin).json()
    client.post("/api/users", json={"username": "dq_op_a", "password": "pw123456", "full_name": "甲经办", "role": "operator", "org_id": a["id"]}, headers=admin)
    client.post("/api/users", json={"username": "dq_op_b", "password": "pw123456", "full_name": "乙经办", "role": "operator", "org_id": b["id"]}, headers=admin)
    op_a = _login(client, "dq_op_a", "pw123456")
    op_b = _login(client, "dq_op_b", "pw123456")
    pat = client.post("/api/patients", json={"name": "质控居民", "id_card": "320000199009091234"}, headers=admin).json()

    # 甲院一条违规就诊（接诊医师为空），直接落库
    from app.database import SessionLocal
    from app.models import Encounter
    with SessionLocal() as db:
        db.add(Encounter(patient_id=pat["id"], org_id=a["id"], doctor_name="", diagnosis_name="高血压"))
        db.commit()

    # 建一条"接诊医师必填"规则（管理员）
    r = client.post("/api/dataquality/rules", json={
        "code": "REQ-DOC-SCOPE", "name": "接诊医师必填", "target_table": "encounters",
        "rule_type": "required", "severity": "warn", "config": {"field": "doctor_name"}}, headers=admin)
    assert r.status_code == 201, r.text

    def items(headers):
        return client.get("/api/dataquality/run?rule_code=REQ-DOC-SCOPE", headers=headers).json()["items"]

    # 全域 admin 看得到甲院违规
    assert any(v["table"] == "encounters" for v in items(admin))
    # 甲院经办看得到本院违规
    assert len(items(op_a)) >= 1
    # 乙院经办看不到甲院违规（跨机构收口）
    assert items(op_b) == []
    # summary 同样按机构收口：乙院经办的 encounters 违规计数为 0
    b_summary = client.get("/api/dataquality/summary", headers=op_b).json()
    assert b_summary["by_table"].get("encounters", 0) == 0

    # 慢病随访(followups)经 ChronicPatient.managed_by_org_id 收口：
    # 甲院一个高血压档案 + 一条缺 sbp/dbp 的随访 → QC013 违规，乙院看不到。
    from app.models import ChronicPatient, FollowUp
    with SessionLocal() as db:
        cp = ChronicPatient(patient_id=pat["id"], disease="hypertension", level=1, managed_by_org_id=a["id"])
        db.add(cp); db.flush()
        db.add(FollowUp(chronic_id=cp.id))  # 无 sbp/dbp → 高血压随访缺指标
        db.commit()

    def qc013(headers):
        return client.get("/api/dataquality/run?rule_code=QC013", headers=headers).json()["items"]

    assert any(v["table"] == "followups" for v in qc013(admin))   # 全域看得到
    assert len(qc013(op_a)) >= 1                                    # 甲院看得到本院
    assert qc013(op_b) == []                                       # 乙院看不到甲院随访违规
